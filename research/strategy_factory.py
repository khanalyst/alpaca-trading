"""Autonomous, parallel strategy research with isolated simulated accounts.

Seven bounded hypotheses are evaluated in separate worker processes by
default.  A worker may mutate only the audited rule grammar; it cannot write
or execute source code.  Mutations are diagnosed from the chronological fit
partition and judged on untouched held-out or later forward data.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import random
from typing import Any, Mapping, Sequence
import uuid

from agent.contracts.rule import hold_deadline, rule_variant_id, validate_rule_spec
from .costs import BAR, QUOTE, CostModel, index_quotes, quote_fill
from .edge_lab import (
    DEFAULT_DB_PATH, EdgeLedger, _read_discovery_rows, content_hash,
)
from .factory_ledger import (
    ACTIVE_HYPOTHESIS_STATES, FACTORY_SCHEMA, FACTORY_STATUSES, FactoryError,
    FactoryLedger,
)
from .gates import (chronological_split, heldout_separation,
                    matched_cluster_test, matched_pairs, max_drawdown_of,
                    performance_floor, placebo_null_distribution,
                    falsification_gate, sample_counts, seal_final_window,
                    structural_floor, verified_gate_envelope,
                    walk_forward_report)
from .llm_strategy import ProposalResult, RuleProposalAdapter
from .stats import benjamini_hochberg, stable_seed
from .factory_core import (
    DEFAULT_STRATEGIES, DEFAULT_VARIANTS, MAX_STRATEGIES, MAX_VARIANTS,
    NOTIONAL_CAP_PCT, StrategyHypothesis, _falsification, _hypothesis_id, _option_at, _safe_variant,
    _session, _simulate_trade, _thesis, _visible, diagnose, initial_hypotheses,
    mutate_from_diagnosis, replacement_hypothesis, simulate_account,
)


DEFAULT_WORKERS = 7
MAX_WORKERS = 16


def _llm_replacement(previous: Mapping[str, Any], diagnostic: Mapping[str, Any], *,
                     config: Mapping[str, Any], max_generations: int,
                     not_before: str | None,
                     existing_variant_ids: set[str],
                     adapter: RuleProposalAdapter | None = None
                     ) -> tuple[StrategyHypothesis | None, ProposalResult | None, str | None]:
    generation = int(previous["generation"]) + 1
    if generation >= int(max_generations):
        return None, None, "generation_limit"
    selected = adapter or RuleProposalAdapter(
        provider=str(config.get("provider") or "openai"),
        model=str(config.get("model") or ""),
        max_attempts=int(config.get("max_attempts", 1)),
        timeout_seconds=float(config.get("timeout_seconds", 30)),
        max_response_bytes=int(config.get("max_response_bytes", 16_384)),
    )
    proposal = selected.propose(
        vehicle=str(previous["vehicle"]), generation=generation,
        prior_validated_rule_spec=previous["rule_spec"], diagnosis=diagnostic)
    if not proposal.success or proposal.rule_spec is None or not proposal.variant_id:
        return None, proposal, "llm_proposal_failed"
    if proposal.variant_id in existing_variant_ids:
        return None, proposal, "duplicate_llm_variant"
    spec = validate_rule_spec(proposal.rule_spec)
    vehicle = str(previous["vehicle"])
    slot = int(previous["slot"])
    hypothesis = StrategyHypothesis(
        _hypothesis_id(vehicle, slot, generation, spec), slot, generation,
        vehicle, str(spec["family"]), _thesis(spec), _falsification(spec),
        spec, str(previous["hypothesis_id"]), not_before,
    )
    return hypothesis, proposal, None


def _llm_lineage_evidence(factory: FactoryLedger,
                          hypothesis: Mapping[str, Any]) -> dict | None:
    parent = hypothesis.get("parent_hypothesis_id")
    if not parent:
        return None
    for event in reversed(factory.events(str(parent))):
        payload = event.get("payload") or {}
        if (payload.get("replacement_hypothesis_id") == hypothesis.get("hypothesis_id") and
                isinstance(payload.get("llm_evidence"), Mapping)):
            return {"schema": payload.get("proposal_schema"),
                    "evidence": dict(payload["llm_evidence"]),
                    "replacement_hypothesis_id": hypothesis.get("hypothesis_id")}
    return None


def _null_row(symbol: str, day: str, opportunity: str, vehicle: str,
              reason: str | None = None) -> dict:
    row = {"vehicle": vehicle, "symbol": symbol, "session_date": day,
           "opportunity_id": opportunity, "net_pnl": 0.0, "return_value": 0.0,
           "no_trade": True}
    if reason:
        row["reject_reason"] = reason
    return row


def null_control_account(bars: Sequence[Any], snapshots: Sequence[Any],
                         spec: Mapping[str, Any], *, vehicle: str,
                         reference_rows: Sequence[Mapping], account_id: str,
                         starting_cash: float = 100_000.0, risk_pct: float = .5,
                         costs: CostModel | None = None,
                         quotes: Sequence[Any] | None = None) -> dict:
    """Replay a chance-entry null with the strategy's own exit and cost rules.

    The null keeps the candidate's session/symbol/direction distribution and
    its stop geometry, but chooses the entry bar at random.  A candidate that
    cannot beat this is timing nothing: comparing only against the parent
    specification measures relative improvement, not edge against chance.
    """
    model = costs or CostModel()
    quote_index = index_quotes(quotes)
    grouped: dict[tuple[str, str], list] = {}
    for bar in sorted(bars, key=lambda item: (item.timestamp, item.symbol)):
        grouped.setdefault((bar.symbol, bar.session_date.isoformat()), []).append(bar)
    references = {(str(row.get("symbol")), str(row.get("session_date"))): row
                  for row in reference_rows}
    rng = random.Random(stable_seed({"account": str(account_id),
                                     "spec": dict(spec),
                                     "sessions": sorted(references)}))
    cash = float(starting_cash)
    peak = cash
    drawdown = 0.0
    rows: list[dict] = []
    for key in sorted(references):
        symbol, day = key
        opportunity = f"null:{account_id}:{symbol}:{day}"
        reference = references[key]
        session_bars = grouped.get(key, [])
        if reference.get("no_trade") is True or len(session_bars) < 3:
            rows.append(_null_row(symbol, day, opportunity, vehicle))
            continue
        try:
            entry_underlying_ref = float(reference["underlying_entry"])
            distance = abs(entry_underlying_ref - float(reference["stop_price"]))
            direction = str(reference["direction"])
        except (KeyError, TypeError, ValueError):
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "reference trade lacks null-control geometry"))
            continue
        if not math.isfinite(distance) or distance <= 0 or direction not in {"long", "short"}:
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "reference trade lacks null-control geometry"))
            continue
        entry_index = rng.randrange(1, len(session_bars) - 1)
        entry_bar = session_bars[entry_index]
        if not _visible(entry_bar, entry_bar.end):
            rows.append(_null_row(symbol, day, opportunity, vehicle))
            continue
        entry_underlying = float(entry_bar.open)
        stop = (entry_underlying - distance if direction == "long" else
                entry_underlying + distance)
        target = (entry_underlying + distance * float(spec["target_r"])
                  if direction == "long" else
                  entry_underlying - distance * float(spec["target_r"]))
        deadline = hold_deadline(entry_bar.timestamp, spec)
        last_index = entry_index
        for probe in range(entry_index + 1, len(session_bars)):
            if session_bars[probe].end.timestamp() > deadline:
                break
            last_index = probe
        exit_bar = session_bars[last_index]
        exit_ref = float(exit_bar.close)
        exit_at = exit_bar.end
        boundary_exit = True
        for bar in session_bars[entry_index + 1:last_index + 1]:
            if not _visible(bar, bar.end):
                continue
            if direction == "long":
                gap_stop, gap_target = bar.open <= stop, bar.open >= target
                hit_stop, hit_target = bar.low <= stop, bar.high >= target
            else:
                gap_stop, gap_target = bar.open >= stop, bar.open <= target
                hit_stop, hit_target = bar.high >= stop, bar.low <= target
            # The null shares the candidate's exit rules, gap-through included.
            # Giving chance entries an exit realism the candidate does not have
            # would bias every delta measured against them.
            if gap_stop or gap_target:
                exit_ref, exit_bar, exit_at = float(bar.open), bar, bar.timestamp
                break
            if hit_stop or hit_target:
                exit_ref = stop if hit_stop else target
                exit_bar, exit_at, boundary_exit = bar, bar.end, False
                break
        entry_ref = entry_underlying
        entry_source = exit_source = BAR
        multiplier = 1
        risk_per_unit = distance
        if vehicle == "equity":
            side = "buy" if direction == "long" else "sell"
            quoted = quote_fill(quote_index, symbol=symbol,
                                at=entry_bar.timestamp, side=side)
            if quoted is not None:
                entry_ref, entry_source = quoted, QUOTE
            quoted_exit = (quote_fill(quote_index, symbol=symbol, at=exit_at,
                                      side="sell" if direction == "long" else "buy")
                           if boundary_exit else None)
            if quoted_exit is not None:
                exit_ref, exit_source = quoted_exit, QUOTE
        if vehicle == "option":
            entry_snap = _option_at(snapshots, symbol=symbol, day=entry_bar.session_date,
                                    direction=direction, cutoff=entry_bar.end)
            exit_snap = (None if entry_snap is None else
                         _option_at(snapshots, symbol=symbol, day=entry_bar.session_date,
                                    direction=direction, cutoff=exit_bar.end,
                                    contract_symbol=entry_snap.contract.symbol))
            if entry_snap is None or exit_snap is None:
                rows.append(_null_row(symbol, day, opportunity, vehicle))
                continue
            entry_ref = entry_snap.ask
            exit_ref = exit_snap.bid
            multiplier = entry_snap.contract.multiplier
            risk_per_unit = entry_ref * multiplier
        quantity = math.floor(max(0.0, cash * float(risk_pct) / 100.0) /
                              max(float(risk_per_unit), 1e-9))
        if vehicle == "equity":
            # A chance entry derives its own stop from its own fill, so the
            # plan anchor and the fill are the same price here.
            quantity = min(quantity, math.floor(
                max(0.0, cash * NOTIONAL_CAP_PCT / 100.0) /
                max(float(entry_underlying), 1e-9)))
        if quantity <= 0:
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "isolated account risk budget cannot fund one unit"))
            continue
        execution_direction = "long" if vehicle == "option" else direction
        executable = vehicle == "option"
        entry = model.execution_price(
            entry_ref, execution_direction, entry=True,
            executable_quote=executable or entry_source == QUOTE)
        exit_price = model.execution_price(
            exit_ref, execution_direction, entry=False,
            executable_quote=executable or exit_source == QUOTE)
        gross = ((exit_price - entry) if execution_direction == "long" else
                 (entry - exit_price)) * quantity * multiplier
        fees = model.fees(entry, exit_price, quantity, multiplier)
        net = gross - fees
        before = cash
        cash += net
        peak = max(peak, cash)
        drawdown = max(drawdown, peak - cash)
        rows.append({"vehicle": vehicle, "symbol": symbol, "session_date": day,
                     "opportunity_id": opportunity, "direction": direction,
                     "entry_timestamp": entry_bar.timestamp.isoformat(),
                     "exit_timestamp": exit_bar.end.isoformat(),
                     "quantity": quantity, "entry_price": entry,
                     "exit_price": exit_price, "gross_pnl": gross, "costs": fees,
                     "net_pnl": net,
                     "return_value": net / before if before > 0 else 0.0,
                     "no_trade": False})
    executed = [row for row in rows if row.get("no_trade") is not True]
    return {"account_id": account_id, "starting_cash": float(starting_cash),
            "ending_equity": cash, "realized_pnl": cash - float(starting_cash),
            "max_drawdown": drawdown, "trades": len(executed), "rows": rows}


def _worker(payload: Mapping[str, Any]) -> dict:
    hypothesis = dict(payload["hypothesis"])
    bars = list(payload["bars"])
    snapshots = list(payload["snapshots"])
    quotes = list(payload["quotes"])
    vehicle = str(payload["vehicle"])
    mode = str(payload["mode"])
    starting_cash = float(payload["starting_cash"])
    costs = payload["costs"]
    if mode == "backtest":
        sessions = sorted({_session(bar) for bar in bars})
        cut = max(1, min(len(sessions) - 1, int(len(sessions) * .7))) if len(sessions) > 1 else len(sessions)
        fit_sessions = set(sessions[:cut])
        fit_bars = [bar for bar in bars if _session(bar) in fit_sessions]
        root_account = simulate_account(
            fit_bars, snapshots, hypothesis["rule_spec"], vehicle=vehicle,
            account_id=f"diagnostic:{hypothesis['hypothesis_id']}",
            starting_cash=starting_cash, costs=costs, quotes=quotes,
        )
        diagnostic = diagnose(root_account["rows"], starting_cash=starting_cash)
        specs = mutate_from_diagnosis(
            hypothesis["rule_spec"], diagnostic, int(payload["variants_per_strategy"]))
    else:
        diagnostic = {"primary_failure": "forward_validation"}
        specs = [validate_rule_spec(item) for item in payload["existing_specs"]]
    control_account = simulate_account(
        bars, snapshots, hypothesis["rule_spec"], vehicle=vehicle,
        account_id=f"control:{hypothesis['hypothesis_id']}:{uuid.uuid4().hex[:8]}",
        starting_cash=starting_cash, costs=costs, quotes=quotes,
    )
    variants = []
    null_rows: dict[str, list] = {}
    for spec in specs:
        variant_id = rule_variant_id(spec)
        account_id = f"sim:{hypothesis['hypothesis_id']}:{variant_id}:{vehicle}:{uuid.uuid4().hex[:8]}"
        account = simulate_account(
            bars, snapshots, spec, vehicle=vehicle, account_id=account_id,
            starting_cash=starting_cash, costs=costs, quotes=quotes,
        )
        null_rows[variant_id] = null_control_account(
            bars, snapshots, spec, vehicle=vehicle, reference_rows=account["rows"],
            account_id=f"null:{hypothesis['hypothesis_id']}:{variant_id}:{vehicle}",
            starting_cash=starting_cash, costs=costs, quotes=quotes)["rows"]
        variants.append({
            "variant_id": variant_id, "rule_spec": spec, "vehicle": vehicle,
            "account": account, "diagnostic": diagnose(account["rows"], starting_cash=starting_cash),
            "worker_pid": os.getpid(),
        })
    sessions = sorted({_session(bar) for bar in bars})
    return {"hypothesis": hypothesis, "mode": mode, "diagnostic": diagnostic,
            "evaluation_start": sessions[0] if sessions else None,
            "evaluation_end": sessions[-1] if sessions else None,
            "variants": sorted(variants, key=lambda item: item["variant_id"]),
            "control_rows": control_account["rows"], "null_rows": null_rows,
            "expected_variants": len(specs), "worker_pid": os.getpid()}


def _qualification_report(rows: Sequence[Mapping], baseline: Sequence[Mapping], *,
                          vehicle: str, sessions: Sequence[str]) -> dict:
    """Score the sealed final window: go/no-go only, never diagnosis."""
    if not rows or not sessions:
        return {"available": False, "sessions": list(sessions), "net_pnl": 0.0,
                "matched": 0, "mean_delta": None, "trades": 0,
                "net_positive": False, "delta_positive": False}
    pairs = matched_pairs(rows, baseline, vehicle=vehicle)
    absolute = performance_floor(rows, vehicle=vehicle)
    delta = (sum(pairs["deltas"]) / pairs["matched"]) if pairs["matched"] else None
    return {"available": True, "sessions": list(sessions),
            "net_pnl": absolute["net_pnl"], "trades": absolute["trades"],
            "matched": pairs["matched"], "mean_delta": delta,
            "net_positive": bool(absolute["net_pnl_positive"]),
            "delta_positive": bool(delta is not None and delta > 0)}


def _gate(rows: Sequence[Mapping], baseline: Sequence[Mapping], *,
          vehicle: str, mode: str,
          min_trades: int, min_sessions: int, alpha: float,
          null_rows: Sequence[Mapping] = (),
          qualification: Mapping | None = None,
          folds: int = 3) -> dict:
    ordered = sorted(rows, key=lambda row: (str(row.get("session_date", "")),
                                             str(row.get("entry_timestamp", ""))))
    base_ordered = sorted(baseline, key=lambda row: (str(row.get("session_date", "")),
                                                      str(row.get("entry_timestamp", ""))))
    if mode == "shadow":
        fit, heldout, base_fit, base_heldout = [], ordered, [], base_ordered
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
        required=mode != "shadow")
    held_floor = structural_floor(
        heldout, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    overall_floor = structural_floor(
        ordered, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    fit_test = (matched_cluster_test(fit, base_fit, vehicle=vehicle) if mode != "shadow" else
                {"available": True, "actual_control": True, "matched": 0,
                 "mean_delta": None, "p_value": 1.0, "mode": "prior_backtest"})
    test = matched_cluster_test(heldout, base_heldout, vehicle=vehicle)
    placebo = placebo_null_distribution(heldout, base_heldout, vehicle=vehicle)
    falsification = {
        **falsification_gate(placebo["observed"], placebo["placebo"], alpha=alpha),
        "method": placebo["method"], "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
        "draws": int(placebo["draws"]), "seed": int(placebo["seed"]),
        "clusters": int(placebo["cluster_count"]),
    }
    separation = (heldout_separation(fit, heldout) if mode != "shadow" else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": bool(heldout), "mode": "new_data"})
    fit_net = sum(float(row.get("net_pnl", 0.0)) for row in fit)
    absolute = performance_floor(heldout, vehicle=vehicle)
    held_net = absolute["net_pnl"]
    held_sessions = {str(row.get("session_date") or "") for row in heldout}
    null_heldout = [row for row in null_rows
                    if str(row.get("session_date") or "") in held_sessions]
    null_test = matched_cluster_test(heldout, null_heldout, vehicle=vehicle)
    null_control = {"kind": "randomized_entry_null", "matched": null_test["matched"],
                    "available": bool(null_test["available"]),
                    "mean_delta": null_test["mean_delta"],
                    "mean_delta_lcb": null_test["mean_delta_lcb"],
                    "p_value": float(null_test["p_value"])}
    walk_forward = walk_forward_report(heldout, base_heldout, vehicle=vehicle,
                                       folds=folds)
    final = dict(qualification or {"available": False, "sessions": [],
                                   "net_positive": False, "delta_positive": False})
    lcb = test.get("mean_delta_lcb")
    checks = {
        "fit_structurally_adequate": bool(fit_floor["adequate"]),
        "heldout_structurally_adequate": bool(held_floor["adequate"]),
        "separated": bool(separation["passes"]),
        "actual_control_available": bool(test.get("available") and test.get("actual_control")),
        "fit_delta_positive": bool(mode == "shadow" or (
            fit_test.get("mean_delta") is not None and float(fit_test["mean_delta"]) > 0)),
        "heldout_delta_positive": bool(test.get("mean_delta") is not None and
                                        float(test["mean_delta"]) > 0),
        "heldout_delta_lcb_positive": bool(lcb is not None and float(lcb) > 0),
        "heldout_p_significant": float(test["p_value"]) <= float(alpha),
        "falsification": bool(falsification["passes"]),
        "heldout_net_pnl_positive": bool(absolute["net_pnl_positive"]),
        "heldout_expectancy_positive": bool(absolute["expectancy_positive"]),
        "null_control_available": bool(null_control["available"]),
        "null_control_delta_positive": bool(
            null_control["mean_delta"] is not None and
            float(null_control["mean_delta"]) > 0 and
            float(null_control["p_value"]) <= float(alpha)),
        "walk_forward_majority_positive": bool(walk_forward["available"] and
                                               walk_forward["majority_positive"]),
        "qualification_net_positive": bool(final.get("available") and
                                           final.get("net_positive")),
        "qualification_delta_positive": bool(final.get("available") and
                                             final.get("delta_positive")),
    }
    return {
        "passes_without_family": bool(all(checks.values())),
        "passes": False, "p_raw": float(test["p_value"]),
        "sample_adequate": bool(fit_floor["adequate"]),
        "heldout_sample_adequate": bool(held_floor["adequate"] and
                                        walk_forward["available"] and
                                        bool(final.get("available"))),
        "confidence": 1.0 - float(test["p_value"]),
        "floor": overall_floor, "fit_floor": fit_floor, "heldout_floor": held_floor,
        "fit_net_pnl": fit_net, "heldout_net_pnl": held_net,
        "heldout_expectancy": absolute["expectancy"],
        "heldout_performance": absolute,
        "fit_trades": sample_counts(fit, vehicle=vehicle)["trades"],
        "heldout_trades": sample_counts(heldout, vehicle=vehicle)["trades"],
        "heldout_delta_lcb": lcb,
        "max_drawdown": max_drawdown_of(ordered), "test": test,
        "fit_test": fit_test, "control": {**test, "kind": "matched_root_baseline"},
        "null_control": null_control, "walk_forward": walk_forward,
        "qualification": final,
        "falsification": falsification, "heldout_separation": separation,
        "checks_without_family": checks,
        "mode": mode, "alpha": float(alpha),
        "_fit_rows": fit, "_heldout_rows": heldout,
    }


def _existing_specs(edge: EdgeLedger, hypothesis_id: str, vehicle: str) -> list[dict]:
    specs = []
    for candidate in edge.status(vehicle=vehicle):
        if candidate.get("strategy_id") != "rule" or candidate.get("status") not in {
                "backtest_passed", "shadow", "validated", "champion"}:
            continue
        try:
            axes = json.loads(candidate.get("axes_json") or "{}")
            config = json.loads(candidate.get("config_json") or "{}")
        except json.JSONDecodeError:
            continue
        if axes.get("hypothesis_id") == hypothesis_id:
            spec = (config.get("strategy") or {}).get("rule_spec")
            if isinstance(spec, Mapping):
                specs.append(validate_rule_spec(spec))
    return specs


def run_factory(data: str | Path | Sequence[Mapping], *,
                db_path: str | Path = DEFAULT_DB_PATH, vehicle: str = "equity",
                strategies: int = DEFAULT_STRATEGIES,
                variants_per_strategy: int = DEFAULT_VARIANTS,
                workers: int = DEFAULT_WORKERS, starting_cash: float = 100_000.0,
                min_trades: int = 100, min_sessions: int = 10,
                alpha: float = .05, max_generations: int = 5,
                strategy_llm: Mapping[str, Any] | None = None,
                costs: CostModel | None = None,
                proposal_adapter: RuleProposalAdapter | None = None) -> dict:
    """Run one autonomous cycle and persist every account, diagnosis and edge."""
    if vehicle not in {"equity", "option"}:
        raise FactoryError("vehicle must be equity or option")
    if not 1 <= int(strategies) <= MAX_STRATEGIES:
        raise FactoryError(f"strategies must be between 1 and {MAX_STRATEGIES}")
    if not 2 <= int(variants_per_strategy) <= MAX_VARIANTS:
        raise FactoryError(f"variants_per_strategy must be between 2 and {MAX_VARIANTS}")
    if not 1 <= int(workers) <= MAX_WORKERS:
        raise FactoryError(f"workers must be between 1 and {MAX_WORKERS}")
    if starting_cash <= 0 or min_trades < 1 or min_sessions < 1:
        raise FactoryError("starting_cash, min_trades and min_sessions must be positive")
    if not 0 < alpha <= 1:
        raise FactoryError("alpha must be in (0,1]")
    model = costs or CostModel()
    llm_config = dict(strategy_llm or {})
    llm_enabled = bool(llm_config.get("enabled", False))
    if llm_enabled and not str(llm_config.get("model") or "").strip() and proposal_adapter is None:
        raise FactoryError("strategy LLM model is required when autonomous LLM replacement is enabled")
    raw_rows, bars, snapshot_map, quote_rows = _read_discovery_rows(data)
    dataset_hash = content_hash(raw_rows)
    factory = FactoryLedger(db_path)
    duplicate = factory.existing_cycle(dataset_hash, vehicle)
    if duplicate is not None:
        return {**duplicate, "duplicate": True}
    if not factory.hypotheses(vehicle=vehicle):
        for hypothesis in initial_hypotheses(strategies, vehicle=vehicle):
            factory.register(hypothesis)
    existing_variant_ids = {
        rule_variant_id(item["rule_spec"])
        for item in factory.hypotheses(vehicle=vehicle)
    }
    active = factory.active(vehicle)[:int(strategies)]
    if not active:
        return {"schema": FACTORY_SCHEMA, "status": "exhausted", "dataset_hash": dataset_hash,
                "vehicle": vehicle, "strategies": 0, "variants": 0, "accounts": 0}
    edge = EdgeLedger(db_path)
    tasks = []
    sealed_windows: dict[str, tuple[Any, list, list]] = {}
    snapshots = list(snapshot_map.values())
    quotes = list(quote_rows)
    for hypothesis in active:
        mode = "shadow" if hypothesis.get("status") == "backtest_passed" else "backtest"
        boundary = (factory.last_boundary(hypothesis["hypothesis_id"], vehicle)
                    if mode == "shadow" else hypothesis.get("not_before"))
        selected_bars = [bar for bar in bars if boundary is None or _session(bar) > boundary]
        selected_snapshots = [snap for snap in snapshots if boundary is None or snap.session_date.isoformat() > boundary]
        selected_quotes = [quote for quote in quotes
                           if boundary is None or quote.session_date.isoformat() > boundary]
        specs = _existing_specs(edge, hypothesis["hypothesis_id"], vehicle) if mode == "shadow" else []
        if mode == "shadow" and not specs:
            factory.event(hypothesis["hypothesis_id"], "backtest_passed",
                          "forward validation is waiting for a persisted eligible variant")
            continue
        if not selected_bars:
            factory.event(hypothesis["hypothesis_id"], hypothesis["status"],
                          "no unseen sessions; forward boundary was not consumed",
                          {"boundary": boundary, "dataset_hash": dataset_hash})
            continue
        factory.event(hypothesis["hypothesis_id"], "testing",
                      f"{mode} evaluation started", {"dataset_hash": dataset_hash})
        # The latest sessions are sealed before any worker is scheduled, so
        # mutation, diagnosis and selection are structurally unable to consume
        # the final qualification window.
        development_bars, sealed = seal_final_window(
            selected_bars, session_of=_session, fraction=.2)
        sealed_sessions = set(sealed.session_dates)
        sealed_windows[hypothesis["hypothesis_id"]] = (
            sealed,
            [snap for snap in selected_snapshots
             if snap.session_date.isoformat() in sealed_sessions],
            [quote for quote in selected_quotes
             if quote.session_date.isoformat() in sealed_sessions])
        tasks.append({
            "hypothesis": hypothesis, "bars": development_bars,
            "snapshots": [snap for snap in selected_snapshots
                          if snap.session_date.isoformat() not in sealed_sessions],
            "quotes": [quote for quote in selected_quotes
                       if quote.session_date.isoformat() not in sealed_sessions],
            "vehicle": vehicle, "mode": mode,
            "existing_specs": specs, "variants_per_strategy": variants_per_strategy,
            "starting_cash": starting_cash, "costs": model,
        })
    if not tasks:
        return {"schema": FACTORY_SCHEMA, "status": "waiting_for_new_data",
                "dataset_hash": dataset_hash, "vehicle": vehicle,
                "strategies": len(active), "variants": 0, "accounts": 0}

    max_workers = min(int(workers), len(tasks))
    worker_results = []
    worker_failures = []
    backend = "process"
    try:
        pool = ProcessPoolExecutor(max_workers=max_workers)
    except (OSError, PermissionError):
        # Some restricted containers disable POSIX semaphores.  Preserve
        # bounded parallel scheduling there; normal deployments use processes.
        pool = ThreadPoolExecutor(max_workers=max_workers)
        backend = "thread_fallback"
    with pool:
        futures = {pool.submit(_worker, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                worker_results.append(future.result())
            except Exception as exc:
                hypothesis = task["hypothesis"]
                resume_status = ("backtest_passed" if task["mode"] == "shadow" else "queued")
                factory.event(
                    hypothesis["hypothesis_id"], resume_status,
                    "worker failed; hypothesis requeued without a failure conclusion",
                    {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                )
                worker_failures.append({"hypothesis_id": hypothesis["hypothesis_id"],
                                        "error_type": type(exc).__name__})
    worker_results.sort(key=lambda item: (int(item["hypothesis"]["slot"]),
                                          str(item["hypothesis"]["hypothesis_id"])))
    for worker in worker_results:
        worker["variants"] = sorted(worker["variants"],
                                    key=lambda item: str(item["variant_id"]))

    # The sealed window is opened exactly once per hypothesis, here in the
    # orchestrator, after every worker has finished proposing and diagnosing.
    qualifications: dict[str, dict] = {}
    for worker in worker_results:
        hypothesis_id = str(worker["hypothesis"]["hypothesis_id"])
        sealed, sealed_snapshots, sealed_quotes = sealed_windows.get(
            hypothesis_id, (None, [], []))
        qualification_bars = (sealed.release(reason=f"final qualification {hypothesis_id}")
                              if sealed is not None and sealed.session_dates else None)
        sessions = tuple(sealed.session_dates) if sealed is not None else ()
        control_rows = (simulate_account(
            qualification_bars, sealed_snapshots, worker["hypothesis"]["rule_spec"],
            vehicle=vehicle, account_id=f"qualification:control:{hypothesis_id}",
            starting_cash=starting_cash, costs=model,
            quotes=sealed_quotes)["rows"] if qualification_bars else [])
        for variant in worker["variants"]:
            rows = (simulate_account(
                qualification_bars, sealed_snapshots, variant["rule_spec"],
                vehicle=vehicle,
                account_id=f"qualification:{hypothesis_id}:{variant['variant_id']}",
                starting_cash=starting_cash, costs=model,
                quotes=sealed_quotes)["rows"] if qualification_bars else [])
            qualifications[f"{hypothesis_id}:{variant['variant_id']}"] = (
                _qualification_report(rows, control_rows, vehicle=vehicle,
                                      sessions=sessions))

    variant_rows = []
    for worker in worker_results:
        hypothesis_id = str(worker["hypothesis"]["hypothesis_id"])
        for variant in worker["variants"]:
            gate = _gate(variant["account"]["rows"], vehicle=vehicle,
                         baseline=worker["control_rows"],
                         mode=worker["mode"], min_trades=min_trades,
                         min_sessions=min_sessions, alpha=alpha,
                         null_rows=(worker.get("null_rows") or {}).get(
                             variant["variant_id"], []),
                         qualification=qualifications.get(
                             f"{hypothesis_id}:{variant['variant_id']}"))
            variant_rows.append((worker, variant, gate))
    # Selection compares candidates across every family and lane in the cycle,
    # so the false-discovery correction that authorizes a champion has to be
    # global.  The family-local correction is retained as reported evidence.
    global_correction = benjamini_hochberg(
        {f"{owner['hypothesis']['hypothesis_id']}:{variant['variant_id']}": gate["p_raw"]
         for owner, variant, gate in variant_rows}, alpha=alpha)
    partitions: dict[str, tuple[list, list]] = {}
    for worker in worker_results:
        local_rows = [(variant, gate) for owner, variant, gate in variant_rows
                      if owner is worker]
        correction = benjamini_hochberg(
            {variant["variant_id"]: gate["p_raw"] for variant, gate in local_rows},
            alpha=alpha)
        for variant, gate in local_rows:
            family = correction[variant["variant_id"]]
            overall = global_correction[
                f"{worker['hypothesis']['hypothesis_id']}:{variant['variant_id']}"]
            checks = {**gate["checks_without_family"],
                      "family_fdr_significant": bool(family["significant"]),
                      "global_fdr_significant": bool(overall["significant"])}
            gate["multiple_tests"] = {**family, "method": "benjamini_hochberg",
                                      "scope": "family"}
            gate["global_multiple_tests"] = {**overall,
                                             "method": "benjamini_hochberg",
                                             "scope": "cycle_global"}
            gate["passes"] = bool(gate["passes_without_family"] and
                                  family["significant"] and overall["significant"])
            gate["confidence"] = 1.0 - float(overall["p_adjusted"])
            fit = gate.pop("_fit_rows")
            heldout = gate.pop("_heldout_rows")
            envelope = verified_gate_envelope(
                lane=worker["mode"], vehicle=vehicle, fit=fit, heldout=heldout,
                fit_floor=gate["fit_floor"], heldout_floor=gate["heldout_floor"],
                control=gate["control"], p_value=gate["p_raw"],
                q_value=overall["p_adjusted"],
                family_q_value=family["p_adjusted"], alpha=alpha,
                falsification=gate["falsification"],
                separation=gate["heldout_separation"], checks=checks,
                passes=gate["passes"],
                walk_forward=gate["walk_forward"],
                qualification=gate["qualification"],
                null_control=gate["null_control"],
                performance={"heldout_delta": gate["test"].get("mean_delta"),
                             "heldout_delta_lcb": gate["heldout_delta_lcb"],
                             "heldout_net_pnl": gate["heldout_net_pnl"],
                             "heldout_expectancy": gate["heldout_expectancy"],
                             "max_drawdown": gate["max_drawdown"]})
            gate["verified_gate"] = envelope
            gate["gate_hash"] = envelope["content_hash"]
            partitions[variant["account"]["account_id"]] = (fit, heldout)

    cycle_id = uuid.uuid4().hex
    summaries = []
    replacements = []
    pending = []
    for worker in worker_results:
        hypothesis = worker["hypothesis"]
        local = [(variant, gate) for owner, variant, gate in variant_rows
                 if owner is worker]
        adequate = [item for item in local
                    if item[1]["sample_adequate"] and
                    item[1]["heldout_sample_adequate"]]
        all_intended_adequate = bool(
            int(worker.get("expected_variants", 0)) > 0 and
            len(local) == int(worker.get("expected_variants", 0)) and
            len(adequate) == len(local))
        passing = [item for item in local if item[1]["passes"]]
        for variant, gate in local:
            result = {**variant, "evaluation_start": worker["evaluation_start"],
                      "evaluation_end": worker["evaluation_end"], "mode": worker["mode"],
                      "gate": gate}
            factory.add_account(cycle_id, hypothesis["hypothesis_id"], result)
            config = {"strategy": {"id": "rule", "version": "v1",
                                     "variant_id": variant["variant_id"],
                                     "rule_spec": variant["rule_spec"]}}
            candidate = edge.register_candidate(
                variant["variant_id"], strategy_id="rule", vehicle=vehicle,
                base_version="v1", hypothesis=hypothesis["thesis"], config=config,
                axes={"hypothesis_id": hypothesis["hypothesis_id"],
                      "slot": hypothesis["slot"], "generation": hypothesis["generation"],
                      "diagnostic": variant["diagnostic"],
                      "simulated_account_id": variant["account"]["account_id"]},
                dataset=raw_rows, code=Path(__file__),
                provenance={"factory": FACTORY_SCHEMA, "mode": worker["mode"],
                            "worker_pid": variant["worker_pid"]})
            lineage = _llm_lineage_evidence(factory, hypothesis)
            if lineage is not None:
                prior = [item for item in edge.evidence(candidate["candidate_id"])
                         if item.get("kind") == "llm_strategy_proposal"]
                if not prior:
                    edge.append_evidence(
                        candidate["candidate_id"], "llm_strategy_proposal", lineage)
            run = None
            if gate["sample_adequate"] and gate["heldout_sample_adequate"]:
                fit, held = partitions[variant["account"]["account_id"]]
                run = edge.append_run(
                    candidate["candidate_id"], lane=worker["mode"], vehicle=vehicle,
                    dataset=raw_rows, config=config, code=Path(__file__),
                    provenance={"factory": FACTORY_SCHEMA,
                                "simulated_account_id": variant["account"]["account_id"]},
                    fit=fit, heldout=held,
                    metrics={"gate": gate, "account": {k: v for k, v in variant["account"].items()
                                                       if k != "rows"},
                             "diagnostic": variant["diagnostic"],
                             "confidence": gate["confidence"],
                             "heldout_delta": gate["test"].get("mean_delta"),
                             "max_drawdown": gate["max_drawdown"]})
                for trade in variant["account"]["rows"]:
                    edge.append_trade(run["run_id"], trade)
                edge.record_verified_gate(run["run_id"], gate)
                edge.append_evidence(candidate["candidate_id"], "autonomous_diagnosis", {
                    "fit_diagnosis": worker["diagnostic"],
                    "variant_diagnosis": variant["diagnostic"], "gate": gate,
                }, run_id=run["run_id"])
                current = edge.candidate(candidate["candidate_id"])["status"]
                if gate["passes"] and worker["mode"] == "backtest" and current == "candidate":
                    edge.transition(candidate["candidate_id"], "backtest_passed",
                                    reason="autonomous held-out gate passed")
                elif gate["passes"] and worker["mode"] == "shadow":
                    if current == "backtest_passed":
                        edge.transition(candidate["candidate_id"], "shadow",
                                        reason="later unseen simulated paper gate passed")
                        current = "shadow"
                    if current == "shadow":
                        edge.transition(candidate["candidate_id"], "validated",
                                        reason="backtest and forward simulated paper gates passed")
                elif not gate["passes"] and current in {"candidate", "backtest_passed"}:
                    edge.transition(candidate["candidate_id"], "retired",
                                    reason="adequately powered autonomous gate failed")
                elif not gate["passes"] and current in {"shadow", "validated", "champion"}:
                    edge.transition(candidate["candidate_id"], "demoted",
                                    reason="latest autonomous gate failed mandatory checks")
            summaries.append({
                "hypothesis_id": hypothesis["hypothesis_id"],
                "candidate_id": candidate["candidate_id"],
                "variant_id": variant["variant_id"], "mode": worker["mode"],
                "evaluation_start": worker["evaluation_start"],
                "evaluation_end": worker["evaluation_end"],
                "account_id": variant["account"]["account_id"],
                "worker_pid": variant["worker_pid"], "gate": gate,
                "status": (edge.candidate(candidate["candidate_id"]) or {}).get("status"),
                "run_id": run.get("run_id") if run else None,
            })
        if passing:
            new_state = "validated" if worker["mode"] == "shadow" else "backtest_passed"
            factory.event(hypothesis["hypothesis_id"], new_state,
                          f"{len(passing)} autonomous variant(s) passed {worker['mode']}",
                          {"passing": [item[0]["variant_id"] for item in passing]})
        elif all_intended_adequate:
            aggregate = max((item[0]["diagnostic"] for item in local),
                            key=lambda value: abs(float(value.get("net_pnl", 0.0))))
            proposal = None
            replacement_error = None
            if llm_enabled:
                replacement, proposal, replacement_error = _llm_replacement(
                    hypothesis, aggregate, config=llm_config,
                    max_generations=max_generations,
                    not_before=worker["evaluation_end"],
                    existing_variant_ids=existing_variant_ids,
                    adapter=proposal_adapter)
            else:
                replacement = replacement_hypothesis(
                    hypothesis, aggregate, max_generations=max_generations,
                    not_before=worker["evaluation_end"])
            if replacement is not None:
                factory.register(replacement)
                existing_variant_ids.add(rule_variant_id(replacement.rule_spec))
                retirement_payload = {
                    "diagnostic": aggregate, "tested_variants": len(local),
                    "replacement_hypothesis_id": replacement.hypothesis_id,
                    "replacement_variant_id": rule_variant_id(replacement.rule_spec),
                }
                if proposal is not None:
                    retirement_payload.update({
                        "proposal_schema": proposal.schema,
                        "llm_evidence": proposal.evidence,
                    })
                    factory.event(
                        hypothesis["hypothesis_id"], "testing",
                        "LLM replacement proposal passed the bounded rule grammar",
                        retirement_payload)
                factory.retire_hypothesis(
                    hypothesis["hypothesis_id"], cycle_id=cycle_id,
                    expected_variants=int(worker.get("expected_variants", 0)),
                    reason=("LLM replacement registered after every intended variant failed"
                            if proposal is not None else
                            "deterministic replacement registered after every intended variant failed"),
                    payload=retirement_payload)
                replacements.append(asdict(replacement))
            elif llm_enabled and replacement_error != "generation_limit":
                detail = {
                    "diagnostic": aggregate,
                    "failure": replacement_error or "llm_proposal_failed",
                    "llm_evidence": proposal.evidence if proposal is not None else {},
                    "error": proposal.error if proposal is not None else None,
                }
                factory.event(
                    hypothesis["hypothesis_id"], "pending_llm_replacement",
                    "adequate failure proven; retirement waits for a valid LLM replacement",
                    detail)
                pending.append({"hypothesis_id": hypothesis["hypothesis_id"],
                                "reason": detail["failure"]})
            else:
                factory.event(
                    hypothesis["hypothesis_id"], "pending_generation_limit",
                    "all variants failed, but the generation cap leaves this slot pending explicit rotation",
                    {"diagnostic": aggregate, "max_generations": int(max_generations)},
                )
                pending.append({"hypothesis_id": hypothesis["hypothesis_id"],
                                "reason": "generation_limit"})
        else:
            factory.event(hypothesis["hypothesis_id"], hypothesis.get("status", "queued"),
                          "sample floor not met; observations were not treated as failure",
                          {"evaluated_variants": len(local), "adequate_variants": len(adequate),
                           "intended_variants": int(worker.get("expected_variants", 0))})

    validated = [row for row in summaries if row["status"] in {"validated", "champion"}]
    champion = None
    if validated:
        champion = edge.select_champion(vehicle=vehicle, min_confidence=1.0 - alpha,
                                        strategy_id="rule")
    result = {
        "schema": FACTORY_SCHEMA,
        "status": ("partial_worker_failure" if worker_failures else
                   "pending_replacement_capacity" if pending else "complete"),
        "cycle_id": cycle_id,
        "dataset_hash": dataset_hash, "vehicle": vehicle,
        "parallel_workers": max_workers,
        "parallel_backend": backend,
        "worker_pids": sorted({row["worker_pid"] for row in summaries}),
        "strategies": len(worker_results), "variants": len(summaries),
        "accounts": len(summaries), "results": summaries,
        "replacements": replacements,
        "pending": pending, "worker_failures": worker_failures,
        "strategy_llm": {"enabled": llm_enabled,
                         "provider": llm_config.get("provider") if llm_enabled else None,
                         "model": llm_config.get("model") if llm_enabled else None},
        "champion": ({key: champion.get(key) for key in
                      ("candidate_id", "variant_id", "strategy_id", "vehicle", "status")}
                     if champion else None),
    }
    if not worker_failures:
        factory.add_cycle(cycle_id, dataset_hash, vehicle, max_workers,
                          len(worker_results), len(summaries), result)
    return result


def factory_status(db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    return FactoryLedger(db_path).status()


__all__ = [
    "DEFAULT_STRATEGIES", "DEFAULT_VARIANTS", "DEFAULT_WORKERS", "FactoryError",
    "FactoryLedger", "StrategyHypothesis", "diagnose", "factory_status",
    "initial_hypotheses", "mutate_from_diagnosis", "replacement_hypothesis",
    "run_factory", "simulate_account",
]
