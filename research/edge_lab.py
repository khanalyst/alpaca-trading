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


from .edge_discovery_core import (
    DiscoveryError, _read_discovery_rows, _effective_ibr_config,
    _opportunity_rows, _discover_gate, _finalize_gate,
)


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
