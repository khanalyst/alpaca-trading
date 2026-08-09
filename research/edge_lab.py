"""Deterministic discovery and evaluation for Alpaca intraday edges."""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from .edge_ledger import (
    BACKTEST_PASSED, CANDIDATE, CHAMPION, DEFAULT_DB_PATH, DEMOTED, EdgeLedger,
    LANES, LIFECYCLE, PAPER_DEMOTION_MIN_OUTCOMES, PAPER_DEMOTION_R_FLOOR,
    RETIRED, SCHEMA_VERSION, SHADOW, VALIDATED, VEHICLES, canonical_json,
    content_hash, hash_config, hash_dataset, hash_file, hash_provenance,
    init_db, init_ledger, provenance_hash,
)
from .gates import (
    chronological_split, deterministic_placebo_deltas, falsification_gate,
    heldout_separation, matched_cluster_test, max_drawdown_of, paired_delta,
    sample_counts, structural_floor, verified_gate_envelope,
)
from .ibr import IBRConfig, replay_ibr
from .market_data import (
    OptionSnapshot, UnderlyingBar, normalize_option_snapshot,
    normalize_underlying_bar,
)
from .stats import benjamini_hochberg


class DiscoveryError(ValueError):
    """Raised when a discovery corpus cannot be evaluated safely."""


def _read_discovery_rows(data: str | Path | Sequence[Mapping]) -> tuple[list[dict], list[UnderlyingBar], dict[str, OptionSnapshot]]:
    """Load one normalized JSONL corpus, retaining bars and option quotes."""
    if isinstance(data, (str, Path)):
        source = Path(data)
        try:
            raw_rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise DiscoveryError(f"invalid discovery JSONL {source}: {exc}") from exc
    else:
        raw_rows = [dict(row) for row in data]
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise DiscoveryError("discovery rows must be JSON objects")
    bars: list[UnderlyingBar] = []
    snapshots: dict[str, OptionSnapshot] = {}
    for number, source_row in enumerate(raw_rows, 1):
        row = dict(source_row)
        kind = str(row.get("kind", "bar")).lower()
        provider = str(row.get("provider") or "alpaca")
        feed = str(row.get("feed") or ("opra" if "option" in kind else "sip"))
        try:
            if kind in {"bar", "underlying", "underlying_bar"}:
                bars.append(normalize_underlying_bar(row, provider=provider, feed=feed))
            elif kind in {"option", "option_snapshot", "option_quote"}:
                contract = row.get("contract")
                if isinstance(contract, Mapping):
                    flattened = dict(contract)
                    flattened.update({key: value for key, value in row.items()
                                      if key != "contract"})
                    row = flattened
                snap = normalize_option_snapshot(row, provider=provider, feed=feed)
                snapshots[f"{snap.timestamp.isoformat()}|{snap.contract.symbol}"] = snap
            # Quotes and metadata are retained in the dataset hash but are not
            # silently fed into an OHLC replay.
        except (TypeError, ValueError) as exc:
            raise DiscoveryError(f"row {number}: {exc}") from exc
    if not bars:
        raise DiscoveryError("discovery corpus contains no underlying bars")
    return raw_rows, bars, snapshots


def _effective_ibr_config(base: Mapping | None, overrides: Mapping,
                          *, close_confirmed: bool = True) -> tuple[IBRConfig, dict]:
    """Build the replay config used by every variant from one immutable base."""
    source = dict(base or {})
    strategy = dict(source.get("strategy") or {})
    # The runtime contract's defaults are explicit here rather than inherited
    # from a notebook's replay defaults.
    strategy.setdefault("range_minutes", 15)
    strategy.setdefault("breakout_buffer_bps", 5.0)
    strategy.setdefault("target_r", 2.0)
    strategy.setdefault("range_stop", True)
    strategy.setdefault("stop_pct", .003)
    strategy.setdefault("target_pct", .006)
    for path, value in overrides.items():
        parts = str(path).split(".")
        if parts and parts[0] == "strategy":
            parts = parts[1:]
        node = strategy
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if parts:
            node[parts[-1]] = value
    research = dict(source.get("research") or {})
    execution = dict(source.get("execution") or {})
    cfg = IBRConfig(
        range_minutes=int(strategy.get("range_minutes", 15)),
        stop_pct=float(strategy.get("stop_pct", .003)),
        target_pct=float(strategy.get("target_pct", .006)),
        target_r=float(strategy["target_r"]) if strategy.get("target_r") is not None else None,
        range_stop=bool(strategy.get("range_stop", True)),
        breakout_buffer_bps=float(strategy.get("breakout_buffer_bps", 5.0)),
        spread_bps=float(research.get("spread_bps", strategy.get("spread_bps", 1.0))),
        slippage_bps=float(research.get("slippage_bps", execution.get("max_slippage_bps", 1.0))),
        fee_bps=float(research.get("fee_bps", .5)),
        close_confirmed=bool(close_confirmed),
        timezone=str((source.get("session") or {}).get("timezone", "America/New_York")),
    )
    effective = dict(source)
    effective["strategy"] = strategy
    effective["replay"] = {"close_confirmed": cfg.close_confirmed,
                            "range_stop": cfg.range_stop}
    return cfg, effective


def _opportunity_rows(result, bars: Sequence[UnderlyingBar], vehicle: str) -> list[dict]:
    """Materialize one row per symbol/session, including no-trade zeros."""
    zone = ZoneInfo("America/New_York")
    sessions = sorted({(bar.symbol, bar.timestamp.astimezone(zone).date()) for bar in bars})
    by_session = {(trade.symbol, trade.session_date): trade for trade in result.trades}
    rows: list[dict] = []
    for symbol, day in sessions:
        opportunity_id = f"ibr:{vehicle}:{symbol}:{day.isoformat()}"
        trade = by_session.get((symbol, day))
        if trade is None:
            rows.append({"vehicle": vehicle, "symbol": symbol,
                         "session_date": day.isoformat(),
                         "opportunity_id": opportunity_id, "net_pnl": 0.0,
                         "return_value": 0.0, "no_trade": True})
            continue
        row = {key: value for key, value in vars(trade).items()}
        row.update({"session_date": trade.session_date.isoformat(),
                    "opportunity_id": opportunity_id, "no_trade": False,
                    "return_value": float(trade.net_pnl)})
        for key, value in list(row.items()):
            if isinstance(value, (datetime, date)):
                row[key] = value.isoformat()
        rows.append(row)
    return rows


def _discover_gate(candidate: Sequence[Mapping], baseline: Sequence[Mapping], *,
                   vehicle: str, min_trades: int, min_sessions: int,
                   alpha: float, shadow: bool = False,
                   actual_control: bool = True,
                   control_kind: str = "matched_actual_baseline") -> dict:
    """Evaluate one chronological backtest or a genuinely new shadow sample.

    A backtest is split into fit/held-out partitions.  A shadow evaluation is
    already supplied as a later corpus, so every row is held out and there is
    deliberately no in-sample fit partition to accidentally reuse for a
    lifecycle transition.
    """
    ordered = sorted(candidate, key=lambda row: (str(row.get("session_date", "")),
                                                  str(row.get("entry_timestamp", ""))))
    base_ordered = sorted(baseline, key=lambda row: (str(row.get("session_date", "")),
                                                      str(row.get("entry_timestamp", ""))))
    if shadow:
        fit, heldout = [], ordered
        base_fit, base_heldout = [], base_ordered
    else:
        fit, heldout = chronological_split(ordered, fit_fraction=.7)
        fit_sessions = {str(row.get("session_date") or "") for row in fit}
        held_sessions = {str(row.get("session_date") or "") for row in heldout}
        base_fit = [row for row in base_ordered
                    if str(row.get("session_date") or "") in fit_sessions]
        base_heldout = [row for row in base_ordered
                       if str(row.get("session_date") or "") in held_sessions]
    fit_floor = structural_floor(
        fit, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions,
        required=not shadow)
    held_floor = structural_floor(
        heldout, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    overall_floor = structural_floor(
        ordered, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    separation = (heldout_separation(fit, heldout) if not shadow else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": bool(heldout), "mode": "new_data"})
    delta_all = paired_delta(ordered, baseline, vehicle=vehicle)
    delta_fit = (matched_cluster_test(fit, base_fit, vehicle=vehicle) if not shadow else
                 {"available": True, "actual_control": True, "matched": 0,
                  "mean_delta": None, "p_value": 1.0, "mode": "prior_backtest"})
    delta_held = matched_cluster_test(heldout, base_heldout, vehicle=vehicle)
    delta_fit["actual_control"] = bool(actual_control)
    delta_held["actual_control"] = bool(actual_control)
    placebo = deterministic_placebo_deltas(
        heldout, base_heldout, vehicle=vehicle)
    falsification = {
        **falsification_gate(placebo["observed"], placebo["placebo"]),
        "method": placebo["method"],
        "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
    }
    candidate_p = float(delta_held.get("p_value", 1.0))
    checks = {
        "fit_structurally_adequate": bool(fit_floor["adequate"]),
        "heldout_structurally_adequate": bool(held_floor["adequate"]),
        "separated": bool(separation["passes"]),
        "actual_control_available": bool(delta_held.get("available") and
                                         delta_held.get("actual_control")),
        "fit_delta_positive": bool(shadow or (
            delta_fit.get("mean_delta") is not None and float(delta_fit["mean_delta"]) > 0)),
        "heldout_delta_positive": bool(delta_held.get("mean_delta") is not None and
                                        float(delta_held["mean_delta"]) > 0),
        "heldout_p_significant": candidate_p <= float(alpha),
        "falsification": bool(falsification["passes"]),
    }
    passes_without_family = bool(
        all(checks.values()) and delta_all.get("mean_delta") is not None and
        float(delta_all["mean_delta"]) > 0)
    return {"vehicle": vehicle, "shadow": shadow,
            "alpha": float(alpha),
            "passes_without_family": passes_without_family,
            "candidate_p_raw": candidate_p,
            "floor": overall_floor, "fit_floor": fit_floor, "heldout_floor": held_floor,
            "heldout_separation": separation, "paired_baseline": delta_all,
            "fit_paired_baseline": delta_fit,
            "heldout_paired_baseline": delta_held,
            "control": {**delta_held, "kind": control_kind},
            "falsification": falsification,
            "checks_without_family": checks,
            "max_drawdown": max_drawdown_of(ordered),
            "fit_trades": sample_counts(fit, vehicle=vehicle)["trades"],
            "heldout_trades": sample_counts(heldout, vehicle=vehicle)["trades"],
            "fit_sessions": len({row.get("session_date") for row in fit}),
            "heldout_sessions": len({row.get("session_date") for row in heldout}),
            "_fit_rows": fit, "_heldout_rows": heldout}


def _finalize_gate(gate: dict, *, lane: str, family: Mapping) -> dict:
    checks = {**gate["checks_without_family"],
              "family_fdr_significant": bool(family.get("significant", False))}
    passes = bool(gate["passes_without_family"] and checks["family_fdr_significant"])
    gate["multiple_tests"] = {"candidate": dict(family),
                              "method": "benjamini_hochberg"}
    gate["passes"] = passes
    fit = gate.pop("_fit_rows")
    heldout = gate.pop("_heldout_rows")
    envelope = verified_gate_envelope(
        lane=lane, vehicle=gate["vehicle"], fit=fit, heldout=heldout,
        fit_floor=gate["fit_floor"], heldout_floor=gate["heldout_floor"],
        control=gate["control"], p_value=gate["candidate_p_raw"],
        q_value=float(family.get("p_adjusted", 1.0)), alpha=gate.get("alpha", 0.05),
        falsification=gate["falsification"], separation=gate["heldout_separation"],
        checks=checks, passes=passes,
        performance={"heldout_delta": gate["heldout_paired_baseline"].get("mean_delta"),
                     "max_drawdown": gate["max_drawdown"]})
    gate["verified_gate"] = envelope
    gate["gate_hash"] = envelope["content_hash"]
    return gate


def discover(data: str | Path | Sequence[Mapping], *, db_path: str | Path = DEFAULT_DB_PATH,
             vehicle: str = "equity", lane: str = "auto", config: Mapping | None = None,
             variants_path: str | Path | None = None, min_trades: int = 100,
             min_sessions: int = 10, alpha: float = .05) -> dict:
    """Run every bounded IBR variant on one normalized corpus.

    ``backtest`` writes only the fit/held-out replay and can advance a fresh
    candidate to ``backtest_passed``.  ``shadow`` requires an existing
    backtest-passed candidate and consumes only sessions strictly later than
    the latest persisted boundary.  ``auto`` chooses backtest for fresh arms
    and the same forward-only tail for already-qualified arms.  No result is
    invented for a missing trade or quote.
    """
    if vehicle not in VEHICLES:
        raise DiscoveryError("vehicle must be equity or option")
    if lane not in {"auto", "backtest", "shadow"}:
        raise DiscoveryError("lane must be auto, backtest, or shadow")
    raw_rows, bars, snapshots = _read_discovery_rows(data)
    from agent.variants import load_registry
    registry_path = variants_path or Path(__file__).with_name("variants.yaml")
    variants = load_registry(registry_path)
    selected = [variant for variant in variants.values()
                if variant.strategy_id == "ibr" and vehicle in variant.vehicles]
    baseline_variant = variants.get("ibr.baseline")
    if baseline_variant is None:
        raise DiscoveryError("research/variants.yaml must register ibr.baseline")
    if baseline_variant not in selected:
        selected.insert(0, baseline_variant)
    candidates = [variant for variant in selected
                  if variant.variant_id != baseline_variant.variant_id]
    ledger = EdgeLedger(db_path)
    code_path = Path(__file__)
    data_hash = content_hash(raw_rows)

    base_results: dict[str, list[dict]] = {}
    effective_configs: dict[str, dict] = {}
    for variant in selected:
        cfg, effective = _effective_ibr_config(config, variant.overrides)
        result = replay_ibr(bars, config=cfg, vehicle=vehicle,
                            option_snapshots=snapshots if vehicle == "option" else None)
        base_results[variant.variant_id] = _opportunity_rows(result, bars, vehicle)
        effective_configs[variant.variant_id] = effective

    def latest_boundary(record: Mapping | None) -> str | None:
        if not record:
            return None
        values = []
        for run in ledger.runs(record["candidate_id"]):
            value = run.get("heldout_end") or run.get("fit_end")
            if value:
                values.append(str(value))
        return max(values) if values else None

    existing = {variant.variant_id: ledger.candidate_by_variant(variant.variant_id, vehicle)
                for variant in selected}
    active = {"backtest_passed", "shadow", "validated", "champion"}
    if lane == "shadow":
        if existing.get(baseline_variant.variant_id) is None:
            raise DiscoveryError(
                "shadow requires a prior backtest baseline; run edge discover --lane backtest first")
        for variant in candidates:
            record = existing.get(variant.variant_id)
            if record is None:
                raise DiscoveryError(
                    f"shadow requires prior backtest candidate {variant.variant_id!r}")
            if record.get("status") not in active:
                raise DiscoveryError(
                    f"candidate {variant.variant_id!r} is {record.get('status')!r}; "
                    "shadow requires backtest_passed or later")

    baseline_rows = base_results[baseline_variant.variant_id]
    baseline_boundary = latest_boundary(existing.get(baseline_variant.variant_id))
    baseline_mode = "shadow" if (
        lane == "shadow" or (lane == "auto" and baseline_boundary is not None)
    ) else "backtest"

    def tail(rows: Sequence[Mapping], boundary: str | None) -> list[dict]:
        if boundary is None:
            return [dict(row) for row in rows]
        return [dict(row) for row in rows
                if str(row.get("session_date") or "") > str(boundary)]

    baseline_eval = tail(baseline_rows, baseline_boundary) if baseline_mode == "shadow" else baseline_rows
    if lane == "shadow" and not baseline_eval:
        raise DiscoveryError("shadow corpus contains no unseen sessions after the persisted boundary")

    modes: dict[str, str] = {}
    eval_rows: dict[str, list[dict]] = {}
    for variant in candidates:
        record = existing.get(variant.variant_id)
        if record is not None and record.get("status") in {"retired", "demoted"}:
            # A failed, adequately-powered evaluation closes this hypothesis
            # for every lane, including an explicitly requested backtest.
            mode = "skip"
        elif lane == "backtest":
            mode = "backtest"
        elif lane == "shadow":
            mode = "shadow"
        elif record is not None and record.get("status") in active:
            mode = "shadow"
        else:
            mode = "backtest"
        modes[variant.variant_id] = mode
        boundary = latest_boundary(record)
        rows = (tail(base_results[variant.variant_id], boundary)
                if mode == "shadow" else
                ([] if mode == "skip" else base_results[variant.variant_id]))
        eval_rows[variant.variant_id] = rows
        if lane == "shadow" and not rows:
            raise DiscoveryError(
                f"shadow corpus contains no unseen sessions for {variant.variant_id!r}")

    baseline_zero = [{**row, "net_pnl": 0.0, "return_value": 0.0}
                     for row in baseline_eval]
    gates = {}
    for variant in candidates:
        mode = modes[variant.variant_id]
        if mode == "skip":
            continue
        gates[variant.variant_id] = _discover_gate(
            eval_rows[variant.variant_id], baseline_eval,
            vehicle=vehicle, min_trades=min_trades,
            min_sessions=min_sessions, alpha=alpha, shadow=(mode == "shadow"))
    corrected = benjamini_hochberg(
        {variant_id: gate["candidate_p_raw"] for variant_id, gate in gates.items()}, alpha=alpha)

    # Attach the family decision before writing any shadow rows.  A failed or
    # under-powered forward check is not "consumed": the same unseen tail may
    # be reconsidered when the append-only recorder supplies more sessions.
    for variant in candidates:
        if modes[variant.variant_id] == "skip":
            continue
        gate = gates[variant.variant_id]
        family = corrected.get(variant.variant_id, {"p": gate["candidate_p_raw"],
                                                     "p_adjusted": 1.0,
                                                     "significant": False})
        _finalize_gate(gate, lane=modes[variant.variant_id], family=family)
    forward_success = any(
        modes[variant.variant_id] == "shadow" and
        gates.get(variant.variant_id, {}).get("passes", False)
        for variant in candidates)

    results = []
    baseline_record = ledger.register_candidate(
        baseline_variant.variant_id, vehicle=vehicle, strategy_id="ibr",
        base_version=baseline_variant.base_version, hypothesis=baseline_variant.hypothesis,
        config=effective_configs[baseline_variant.variant_id], dataset=raw_rows,
        code=code_path, provenance={"lane": lane, "vehicle": vehicle, "role": "baseline"},
        overrides=baseline_variant.overrides)
    baseline_gate = _discover_gate(
        baseline_eval, baseline_zero, vehicle=vehicle,
        min_trades=min_trades, min_sessions=min_sessions, alpha=alpha,
        shadow=(baseline_mode == "shadow"), actual_control=False,
        control_kind="synthetic_zero_reference")
    _finalize_gate(
        baseline_gate, lane=baseline_mode,
        family={"p": baseline_gate["candidate_p_raw"],
                "p_adjusted": baseline_gate["candidate_p_raw"],
                "significant": baseline_gate["candidate_p_raw"] <= alpha,
                "family_size": 1})
    baseline_run = None
    baseline_adequate = bool(
        baseline_gate.get("fit_floor", {}).get("adequate") and
        baseline_gate.get("heldout_floor", {}).get("adequate"))
    if baseline_eval and not baseline_adequate:
        ledger.append_event(
            candidate_id=baseline_record["candidate_id"],
            event_type="insufficient_data", actor="edge_lab",
            reason="baseline sample floor not met; no observations consumed",
            payload={"dataset_hash": data_hash, "mode": baseline_mode,
                     "rows": len(baseline_eval),
                     "trades": baseline_gate.get("floor", {}).get("trades", 0),
                     "sessions": baseline_gate.get("floor", {}).get("sessions", 0),
                     "boundary": baseline_boundary})
    if baseline_eval and baseline_adequate and (baseline_mode != "shadow" or forward_success):
        if baseline_mode == "shadow":
            baseline_fit, baseline_held = [], baseline_eval
        else:
            baseline_fit, baseline_held = chronological_split(baseline_eval, fit_fraction=.7)
        baseline_run = ledger.append_run(
            baseline_record["candidate_id"], lane=baseline_mode, vehicle=vehicle,
            dataset=raw_rows, config=effective_configs[baseline_variant.variant_id], code=code_path,
            provenance={"lane": lane, "vehicle": vehicle, "role": "baseline"},
            fit=baseline_fit, heldout=baseline_held,
            metrics={"gate": baseline_gate, "role": "baseline"})
        for row in baseline_eval:
            ledger.append_trade(baseline_run["run_id"], row)
        ledger.record_verified_gate(baseline_run["run_id"], baseline_gate)
        ledger.append_evidence(baseline_record["candidate_id"], "baseline_control",
                               baseline_gate, run_id=baseline_run["run_id"])

    for variant in candidates:
        mode = modes[variant.variant_id]
        rows = eval_rows[variant.variant_id]
        record = ledger.register_candidate(
            variant.variant_id, vehicle=vehicle, strategy_id="ibr",
            base_version=variant.base_version, hypothesis=variant.hypothesis,
            config=effective_configs[variant.variant_id], dataset=raw_rows,
            code=code_path, provenance={"lane": lane, "vehicle": vehicle},
            overrides=variant.overrides)
        if mode == "skip":
            results.append({"variant_id": variant.variant_id, "vehicle": vehicle,
                            "candidate_id": record["candidate_id"],
                            "status": record.get("status", "retired"),
                            "gate": None, "shadow_gate": None,
                            "run_id": None, "shadow_run_id": None,
                            "mode": mode, "unseen_sessions": None})
            continue
        gate = gates[variant.variant_id]
        family = gate["multiple_tests"]["candidate"]
        adequate = bool(gate.get("fit_floor", {}).get("adequate") and
                        gate.get("heldout_floor", {}).get("adequate"))
        run = None
        shadow_run = None
        status = record.get("status", "candidate")
        if not rows and mode == "shadow":
            ledger.append_event(
                candidate_id=record["candidate_id"], event_type="insufficient_data",
                actor="edge_lab", reason="no unseen sessions; no observations consumed",
                payload={"dataset_hash": data_hash, "mode": mode,
                         "rows": 0, "trades": 0, "sessions": 0,
                         "boundary": latest_boundary(record)})
        elif rows and not adequate:
            ledger.append_event(
                candidate_id=record["candidate_id"], event_type="insufficient_data",
                actor="edge_lab", reason="sample floor not met; no observations consumed",
                payload={"dataset_hash": data_hash, "mode": mode,
                         "rows": len(rows),
                         "trades": gate.get("floor", {}).get("trades", 0),
                         "sessions": gate.get("floor", {}).get("sessions", 0),
                         "heldout_trades": gate.get("heldout_floor", {}).get("trades", 0),
                         "heldout_sessions": gate.get("heldout_floor", {}).get("sessions", 0),
                         "boundary": latest_boundary(record)})
        elif rows:
            if mode == "shadow":
                fit_rows, heldout_rows = [], rows
            else:
                fit_rows, heldout_rows = chronological_split(rows, fit_fraction=.7)
            run = ledger.append_run(
                record["candidate_id"], lane=mode, vehicle=vehicle, dataset=raw_rows,
                config=effective_configs[variant.variant_id], code=code_path,
                provenance={"lane": lane, "vehicle": vehicle},
                fit=fit_rows, heldout=heldout_rows,
                metrics={"gate": gate, "confidence": 1.0 - family.get("p_adjusted", 1.0),
                         "heldout_delta": gate["heldout_paired_baseline"].get("mean_delta") or 0.0,
                         "max_drawdown": gate["max_drawdown"],
                         "heldout_trades": gate["heldout_trades"],
                         "role": "forward_shadow" if mode == "shadow" else "candidate"})
            for row in rows:
                ledger.append_trade(run["run_id"], row)
            ledger.record_verified_gate(run["run_id"], gate)
            ledger.append_evidence(record["candidate_id"],
                                   "shadow_gate" if mode == "shadow" else "gate_report",
                                   gate, run_id=run["run_id"])
        if adequate and gate["passes"] and mode == "backtest" and lane in {"backtest", "auto"} \
                and status == "candidate":
            ledger.transition(record["candidate_id"], "backtest_passed",
                              reason="pre-registered backtest gates passed")
            status = "backtest_passed"
        if adequate and gate["passes"] and mode == "shadow" and status in {"backtest_passed", "shadow"}:
            if status == "backtest_passed":
                ledger.transition(record["candidate_id"], "shadow",
                                  reason="later unseen shadow lane started")
                status = "shadow"
            shadow_run = run
            if status == "shadow":
                ledger.transition(record["candidate_id"], "validated",
                                  reason="later unseen shadow gates passed")
                status = "validated"
        elif adequate and not gate["passes"] and status not in {"retired", "demoted"}:
            target = "demoted" if status in {"shadow", "validated", "champion"} else "retired"
            ledger.transition(record["candidate_id"], target,
                              reason=f"{mode} evidence failed mandatory gates")
            status = target
        results.append({"variant_id": variant.variant_id, "vehicle": vehicle,
                        "candidate_id": record["candidate_id"], "status": status,
                        "gate": gate,
                        "shadow_gate": gate if mode == "shadow" else None,
                        "run_id": run["run_id"] if run else None,
                        "shadow_run_id": shadow_run["run_id"] if shadow_run else None,
                        "mode": mode, "unseen_sessions": len(rows) if mode == "shadow" else None})
    champion = ledger.select_champion(vehicle=vehicle) if lane in {"auto", "shadow"} else None
    return {"vehicle": vehicle, "lane": lane, "dataset_hash": data_hash,
            "variants": results, "family_correction": corrected,
            "baseline": {"candidate_id": baseline_record["candidate_id"],
                         "run_id": baseline_run["run_id"] if baseline_run else None,
                         "gate": baseline_gate, "mode": baseline_mode,
                         "unseen_sessions": len(baseline_eval) if baseline_mode == "shadow" else None},
            "champion": champion}


__all__ = ["BACKTEST_PASSED", "CANDIDATE", "CHAMPION", "DEFAULT_DB_PATH",
           "DEMOTED", "DiscoveryError", "EdgeLedger", "LANES", "LIFECYCLE",
           "RETIRED", "SHADOW", "VALIDATED", "VEHICLES", "canonical_json",
           "content_hash", "discover", "hash_config", "hash_dataset",
           "hash_file", "hash_provenance", "init_db", "init_ledger",
           "provenance_hash"]
