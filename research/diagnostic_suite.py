"""A frozen inventory survey, independent of authorizing research readiness.

Reuse existing rule replay and the shared IBR runtime diagnostic. Never
generate/tune hypotheses, open ledgers, emit proofs, or turn missing observations
into negative returns. Historical IBR replay remains a separate comparison.
"""
from __future__ import annotations

import argparse
import atexit
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Mapping

from agent.config import load_config
from agent.contracts.risk_geometry import required_stop_distance_bps
from agent.contracts.rule import MIN_STOP_DISTANCE_BPS
from agent.variants import load_registry
from .cost_counterfactual import _code_bundle_files, _code_bundle_hash
from .cost_rerun import run_cost_calibration, write_immutable_evidence
from .costs import CostModel, ReplayPolicy, diagnostic_backfill_policy, static_cost_config
from .diagnostic_shadow import _policy_config, build_diagnostic_cohort
from .edge_discovery_core import (
    _effective_ibr_config, _opportunity_rows, _read_discovery_rows,
)
from .edge_ledger_store import content_hash
from .factory_core import diagnose, simulate_account
from .fit_diagnostics import measure_fit_diagnostics
from .ibr import replay_ibr
from .ibr_diagnostic import run_offline_forward_ibr
from .mechanism_cohort import mechanism_cohort
from .quote_costs import cost_resolver_setup, reprice_ibr_result
from .source_validation import validate_source

SCHEMA = "complete-diagnostic-suite.v1"
EXPECTED_ARMS = 43
IBR_UNMAPPED_FILTERS = (
    "min_relative_volume", "min_ibr_width_atr", "max_ibr_width_atr",
    "max_ibr_width_pct", "atr_period", "max_entry_extension_r",
    "stale_minutes", "max_spread_bps",
)
_WORKER: dict[str, Any] = {}


def admission_preflight(runtime_config: Mapping[str, Any]) -> dict:
    """Report configuration feasibility before replay, without rewriting plans."""
    policy = ReplayPolicy.from_config(runtime_config)
    scenario = policy.stressed_cost_scenario_bps
    ratio = policy.max_stressed_cost_to_risk_ratio
    required = (None if scenario is None or ratio is None else
                required_stop_distance_bps(scenario, ratio))
    fixed = CostModel.from_config(static_cost_config(runtime_config), vehicle="equity")
    return {
        "schema": "diagnostic-admission-preflight.v1",
        "scenario_bps": scenario, "max_cost_to_risk_ratio": ratio,
        "rule_grammar_stop_floor_bps": float(MIN_STOP_DISTANCE_BPS),
        "fixed_scenario_minimum_stop_bps": (
            required if required is not None and math.isfinite(required) else None),
        "no_finite_stop_admissible": required is not None and not math.isfinite(required),
        "grammar_floor_below_fixed_scenario_requirement": (
            required is not None and float(MIN_STOP_DISTANCE_BPS) < required),
        "calibrated_scenario_enabled": policy.stressed_cost_calibration_enabled,
        "static_bar_round_trip_cost_bps": 2 * (fixed.entry_cost_bps + fixed.fee_bps),
        "costs_measured": False,
        "scope": "fixed_configuration_only_actual_tick_geometry_measured_per_signal",
        "plan_mutation": False, "runtime_config_changed": False, "authorizing": False,
    }


def build_inventory(runtime_config: Mapping[str, Any], *, code_hash: str) -> dict:
    """Resolve existing identities; registration is not strategy evaluation."""
    shadow = build_diagnostic_cohort(runtime_config, code_identity=code_hash)
    mechanism = mechanism_cohort()
    arms = [{"variant_id": arm["variant_id"], "strategy_id": "rule",
             "family": arm["family"], "role": arm["role"],
             "cohort": "diagnostic_shadow", "rule_spec": arm["rule_spec"]}
            for arm in shadow["arms"]]
    arms.extend({"variant_id": arm["variant_id"], "strategy_id": "rule",
                 "family": arm["rule_spec"]["family"], "role": str(arm["arm"]),
                 "cohort": mechanism["cohort_id"], "rule_spec": arm["rule_spec"]}
                for family in mechanism["families"] for arm in family["arms"])
    registry = load_registry(Path(__file__).with_name("variants.yaml"))
    arms.extend({"variant_id": item.variant_id, "strategy_id": "ibr",
                 "family": "ibr", "role": "registered", "cohort": "ibr_registry",
                 "overrides": dict(item.overrides), "hypothesis": item.hypothesis}
                for item in registry.values()
                if item.strategy_id == "ibr" and "equity" in item.vehicles)
    identities = [arm["variant_id"] for arm in arms]
    if len(arms) != EXPECTED_ARMS or len(set(identities)) != EXPECTED_ARMS:
        raise ValueError("diagnostic inventory must contain 43 distinct existing equity arms")
    return {"schema": "diagnostic-inventory.v1", "arms": arms,
            "registered_arms": len(arms), "inventory_hash": content_hash(arms),
            "cohort_counts": dict(sorted(Counter(arm["cohort"] for arm in arms).items())),
            "diagnostic_only": True, "authorizing": False}


def _close_worker() -> None:
    close = getattr(_WORKER.get("quotes"), "close", None)
    if callable(close):
        close()
    _WORKER.clear()


def _initialize_worker(data: str, runtime: dict, source_hash: str,
                       starting_cash: float, full_fit_diagnostics: bool) -> None:
    _close_worker()
    policy = diagnostic_backfill_policy(ReplayPolicy.from_config(runtime))
    raw, bars, snapshots, quotes = _read_discovery_rows(
        data, require_provenance=True, expected_equity_feed=policy.equity_feed,
        expected_provider=policy.equity_provider)
    if content_hash(raw) != source_hash:
        close = getattr(quotes, "close", None)
        if callable(close):
            close()
        raise ValueError("diagnostic source changed after the inventory was frozen")
    _WORKER.update(runtime=runtime, policy=policy, bars=bars,
                   snapshots=list(snapshots.values()), quotes=quotes,
                   cost_setup=cost_resolver_setup(runtime, vehicle="equity"),
                   starting_cash=starting_cash,
                   full_fit_diagnostics=full_fit_diagnostics)
    atexit.register(_close_worker)


def _ibr_dispositions(rows: list[dict]) -> list[dict]:
    """Add missing legacy categories without altering any replay outcome."""
    no_signal = {"no_breakout", "past_latest_entry_time"}
    data_missing = {"incomplete_opening_range", "opening_range_gap",
                    "no_post_range_bars", "bar_outside_exact_session",
                    "breakout_on_final_bar", "entry_bar_not_adjacent",
                    "breakout_not_visible", "no_entry_bar_after_signal"}
    for row in rows:
        if row.get("no_trade") is not True:
            continue
        reason = row.get("reject_reason")
        if reason in no_signal or not reason:
            row.update(execution_disposition="no_signal", signal_opportunity=False)
        else:
            row.update(execution_disposition="refused",
                       signal_opportunity=reason not in data_missing)
    return rows


def classify(diagnostic: Mapping[str, Any]) -> str:
    """Outcome availability precedes the sign of a point estimate."""
    if not diagnostic.get("executed_trades"):
        if diagnostic.get("signal_execution_rejection_count"):
            return "execution_blocked"
        if diagnostic.get("data_rejection_count"):
            return "missing_data"
        if diagnostic.get("no_signal_count") and not diagnostic.get("unclassified_no_trade_count"):
            return "no_signal"
        return "insufficient_observations"
    if diagnostic.get("evidence_status") == "insufficient_trade_sample":
        return "underpowered"
    expectancy = diagnostic.get("measured_net_expectancy")
    if not isinstance(expectancy, (int, float)) or not math.isfinite(expectancy):
        return "insufficient_observations"
    return ("positive_point_estimate" if expectancy > 0 else
            "negative_point_estimate" if expectancy < 0 else "flat_point_estimate")


def _fit_status(*, requested: bool, applicable: bool) -> dict:
    if not requested:
        return {
            "status": "not_requested", "requested": False,
            "applicable": applicable, "complete_prefix_audit": False,
            "reason": "optional full-prefix fit diagnostics were not requested",
            "interpretation": (
                "omission is neither negative evidence nor a complete-prefix claim"),
            "authorizing": False,
        }
    if not applicable:
        return {
            "status": "not_applicable", "requested": True,
            "applicable": False, "complete_prefix_audit": False,
            "reason": "full-prefix fit diagnostics are defined for rule arms only",
            "authorizing": False,
        }
    return {
        "status": "measured", "requested": True, "applicable": True,
        "complete_prefix_audit": True, "scope": "full_prefix",
        "authorizing": False,
    }


def _evaluate_arm(arm: dict) -> dict:
    state = _WORKER
    setup = state["cost_setup"]
    fit = None
    fit_status = _fit_status(
        requested=state["full_fit_diagnostics"],
        applicable=arm["strategy_id"] == "rule")
    if arm["strategy_id"] == "rule":
        account = simulate_account(
            state["bars"], state["snapshots"], arm["rule_spec"], vehicle="equity",
            account_id="diagnostic-suite:" + arm["variant_id"],
            starting_cash=state["starting_cash"], costs=setup.model,
            cost_resolver=setup.resolver, quotes=state["quotes"], policy=state["policy"])
        rows = account["rows"]
        if state["full_fit_diagnostics"]:
            fit = measure_fit_diagnostics(
                state["bars"], arm["rule_spec"], account_rows=rows, costs=setup.model,
                vehicle="equity", risk_config=state["runtime"].get("risk"),
                policy=state["policy"])
        parity = {"engine": "rule_account", "quantity_model": "isolated_risk_sized_account",
                  "scope": "shared_rule_replay_under_diagnostic_data_policy"}
    else:
        cfg, _effective = _effective_ibr_config(
            state["runtime"], arm["overrides"], vehicle="equity",
            close_confirmed=True, policy=state["policy"])
        replay = replay_ibr(state["bars"], config=cfg, vehicle="equity", quotes=state["quotes"])
        replay = reprice_ibr_result(replay, resolver=setup.resolver, vehicle="equity")
        rows = _ibr_dispositions(_opportunity_rows(replay, state["bars"], "equity"))
        parity = {"engine": "legacy_ibr_replay", "runtime_parity": "partial",
                  "unmapped_runtime_filters": list(IBR_UNMAPPED_FILTERS),
                  "quantity_model": "fixed_shares", "shares": cfg.quantity,
                  "scope": "existing_ibr_research_lane_not_full_runtime_validation"}
    summary = diagnose(rows, starting_cash=state["starting_cash"], diagnostic_only=True)
    return {**arm, "vehicle": "equity", "outcome": classify(summary),
            "diagnostic": summary, "fit_diagnostics": fit,
            "fit_diagnostics_status": fit_status, "replay_scope": parity,
            "rows": rows, "diagnostic_only": True, "authorizing": False,
            "eligible": False, "edge_proven": False}


def _cost_status(data: str, runtime: dict, quote_count: int) -> dict:
    if not quote_count:
        return {"status": "unavailable", "reason": "no_contemporaneous_quotes",
                "quotes": 0, "authorizing": False, "activation_allowed": False}
    try:
        result = run_cost_calibration(data, runtime_config=runtime)
    except (ValueError, RuntimeError) as exc:
        return {"status": "unavailable", "reason": str(exc), "quotes": quote_count,
                "authorizing": False, "activation_allowed": False}
    return {"status": "measured_diagnostic", "report": result,
            "authorizing": False, "activation_allowed": False}


def _attach_ibr_runtime(results: list[dict], forward: Mapping[str, Any]) -> None:
    """Keep legacy measurements visible without presenting them as runtime P&L."""
    measured = forward["arms"]
    expected = {arm["variant_id"] for arm in results if arm["strategy_id"] == "ibr"}
    if set(measured) != expected:
        raise ValueError("runtime IBR evidence does not match the frozen inventory")
    for result in results:
        if result["strategy_id"] != "ibr":
            continue
        runtime_arm = measured[result["variant_id"]]
        legacy = {key: result[key] for key in (
            "outcome", "diagnostic", "rows", "replay_scope", "diagnostic_only",
            "authorizing", "eligible", "edge_proven")}
        result.update({
            "legacy_comparison": legacy,
            "outcome": runtime_arm["outcome"],
            "diagnostic": runtime_arm["diagnostic"],
            "rows": runtime_arm["rows"],
            "replay_scope": dict(forward["replay_scope"]),
            "runtime_evidence": {key: value for key, value in runtime_arm.items()
                                 if key not in {"rows", "diagnostic", "outcome"}},
        })


def run_suite(data: str | Path, *, runtime_config: Mapping[str, Any],
              output: str | Path, workers: int = 2, starting_cash: float = 100_000.0,
              diagnostic_only: bool = False, full_fit_diagnostics: bool = False,
              ibr_max_events: int = 100_000,
              progress=None) -> dict:
    if diagnostic_only is not True:
        raise ValueError("the complete inventory requires explicit diagnostic_only=True")
    if not isinstance(full_fit_diagnostics, bool):
        raise ValueError("full_fit_diagnostics must be a boolean")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ValueError("workers must be an integer from 1 to 4")
    if isinstance(starting_cash, bool) or not math.isfinite(starting_cash) or starting_cash <= 0:
        raise ValueError("starting cash must be finite and positive")
    if (isinstance(ibr_max_events, bool) or not isinstance(ibr_max_events, int) or
            ibr_max_events <= 0):
        raise ValueError("ibr_max_events must be a positive integer")
    data = str(Path(data).resolve())
    destination = Path(output)
    manifest_path = destination.with_name(destination.name + ".manifest.json")
    if destination.exists() or manifest_path.exists():
        raise ValueError("diagnostic output or frozen manifest already exists; choose a new path")
    runtime = _policy_config(runtime_config)
    source = validate_source(data, diagnostic_only=True)
    if not source.get("content_hash"):
        raise ValueError("diagnostic source identity is unavailable")
    files = _code_bundle_files()
    code_hash = _code_bundle_hash(files)
    inventory = build_inventory(runtime, code_hash=code_hash)
    runtime_cohort = build_diagnostic_cohort(
        runtime, code_identity=code_hash, include_ibr=True)
    ibr_arms = [arm for arm in runtime_cohort["arms"] if arm["strategy_id"] == "ibr"]
    ibr_contract = {
        "mode": "shared_runtime_forward_observed_only",
        "cohort_identity": runtime_cohort["cohort_identity"],
        "arm_config_identities": {arm["variant_id"]: arm["config_identity"]
                                  for arm in ibr_arms},
        "max_events": ibr_max_events,
        "legacy_comparison": "fixed_share_partial_runtime_parity",
        "broker_equivalence": False, "authorizing": False,
    }
    preflight = admission_preflight(runtime)
    _initialize_worker(data, runtime, source["content_hash"], float(starting_cash),
                       full_fit_diagnostics)
    try:
        quotes = _WORKER["quotes"]
        count = getattr(quotes, "count", None)
        quote_count = count if isinstance(count, int) else len(quotes)
        corpus = {"bars": len(_WORKER["bars"]), "quotes": quote_count,
                  "symbols": sorted({bar.symbol for bar in _WORKER["bars"]}),
                  "sessions": sorted({str(bar.session_date) for bar in _WORKER["bars"]})}
        manifest = {**inventory, "source": source, "corpus": corpus,
                    "admission_preflight": preflight,
                    "runtime_config_hash": content_hash(runtime), "code_hash": code_hash,
                    "code_files": list(files), "starting_cash": float(starting_cash),
                    "full_fit_diagnostics": full_fit_diagnostics,
                    "ibr_runtime_contract": ibr_contract,
                    "replay_policy": _WORKER["policy"].as_dict(),
                    "frozen_at": datetime.now(timezone.utc).isoformat(),
                    "scope": "retrospective_diagnostic_not_untouched_holdout",
                    "selection": "none", "proofs": [], "fdr": False}
        destination.parent.mkdir(parents=True, exist_ok=True)
        frozen = write_immutable_evidence(manifest_path, manifest)
        results = []
        def accept(arm, result):
            results.append(result)
            if progress:
                progress(len(results), len(inventory["arms"]), arm["variant_id"])
        if workers == 1:
            for arm in inventory["arms"]:
                accept(arm, _evaluate_arm(arm))
        else:
            _close_worker()
            with ProcessPoolExecutor(
                    max_workers=workers, initializer=_initialize_worker,
                    initargs=(data, runtime, source["content_hash"], float(starting_cash),
                              full_fit_diagnostics)) as pool:
                pending = {pool.submit(_evaluate_arm, arm): arm for arm in inventory["arms"]}
                for future in as_completed(pending):
                    accept(pending[future], future.result())
        forward = run_offline_forward_ibr(
            data, runtime_config=runtime, arms=ibr_arms,
            starting_cash=float(starting_cash), source_report=source,
            max_events=ibr_max_events)
        _attach_ibr_runtime(results, forward)
        cost_status = _cost_status(data, runtime, quote_count)
        if _code_bundle_hash(_code_bundle_files()) != code_hash:
            raise ValueError("research code changed during the diagnostic run")
        if validate_source(data, diagnostic_only=True).get("content_hash") != source["content_hash"]:
            raise ValueError("research source changed during the diagnostic run")
        results.sort(key=lambda arm: arm["variant_id"])
        report = {"schema": SCHEMA, "status": "diagnostic_complete",
                  "diagnostic_only": True, "authorizing": False,
                  "manifest_hash": frozen["content_hash"], "inventory": inventory,
                  "source": source, "corpus": corpus, "code_hash": code_hash,
                  "runtime_config_hash": content_hash(runtime),
                  "full_fit_diagnostics": full_fit_diagnostics,
                  "ibr_runtime_contract": ibr_contract,
                  "ibr_runtime": {key: value for key, value in forward.items()
                                  if key != "arms"},
                  "cost_calibration": cost_status,
                  "admission_preflight": preflight,
                  "outcome_counts": dict(sorted(Counter(item["outcome"] for item in results).items())),
                  "legacy_comparison_outcome_counts": dict(sorted(Counter(
                      item["legacy_comparison"]["outcome"] for item in results
                      if "legacy_comparison" in item).items())),
                  "results": results, "eligible": [], "proofs": [], "fdr": False,
                  "portfolio_pnl": None,
                  "limitations": ["previously_examined_data_not_confirmatory",
                                  "overlapping_arms_not_independent_or_a_portfolio",
                                  "bar_fallback_not_executable_quote_evidence",
                                  "ibr_legacy_comparison_has_partial_runtime_parity",
                                  "offline_quotes_do_not_establish_broker_equivalence"],
                  "completed_at": datetime.now(timezone.utc).isoformat()}
        return write_immutable_evidence(destination, report)
    finally:
        _close_worker()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--agent-config", default="config.yaml")
    parser.add_argument("--out", required=True)
    parser.add_argument("--diagnostic-only", action="store_true", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--full-fit-diagnostics", action="store_true")
    parser.add_argument("--ibr-max-events", type=int, default=100_000,
                        help="bounded forward IBR input; excess is unavailable, never truncated")
    args = parser.parse_args(argv)
    def progress(done, total, variant):
        print(json.dumps({"phase": "diagnostic_inventory", "done": done,
                          "total": total, "variant_id": variant}), flush=True)
    result = run_suite(args.data, runtime_config=load_config(args.agent_config),
                       output=args.out, workers=args.workers, diagnostic_only=True,
                       full_fit_diagnostics=args.full_fit_diagnostics,
                       ibr_max_events=args.ibr_max_events,
                       progress=progress)
    print(json.dumps({"schema": SCHEMA, "status": result["status"],
                      "outcome_counts": result["outcome_counts"], "report": args.out,
                      "authorizing": False, "proofs": []}), flush=True)
    return 2  # Consistent with the existing nonauthorizing factory CLI.


if __name__ == "__main__":
    raise SystemExit(main())
