"""Frozen trader research: unchanged controls, new mechanisms, and no promotion.

Use a sealed recorder projection. Every arm is registered before evaluation;
replay rows, discontinuities, failed admissions and unavailable evidence survive.
This command never opens a broker, changes runtime configuration or emits proofs.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
from typing import Any, Mapping

from agent.config import load_config
from .cost_counterfactual import _code_bundle_files, _code_bundle_hash
from .cost_rerun import run_cost_calibration, write_immutable_evidence
from .diagnostic_suite import (build_inventory, admission_preflight,
    _initialize_worker, _close_worker, _evaluate_arm, _WORKER,
    _attach_ibr_runtime)
from .diagnostic_shadow import _policy_config, build_diagnostic_cohort
from .edge_ledger_store import content_hash
from .ibr_diagnostic import run_offline_forward_ibr
from .mechanism_cohort_v2 import mechanism_cohort
from .signal_quality import measure_signal_quality
from .source_validation import validate_source
from .trader_diagnostics import build_trader_diagnostics


def frozen_inventory(config: Mapping[str, Any], code_hash: str) -> dict:
    controls = build_inventory(config, code_hash=code_hash)
    mechanisms = mechanism_cohort()
    arms = list(controls["arms"])
    arms.extend({"variant_id": arm["variant_id"], "strategy_id": "rule",
                 "family": family["name"], "role": "new_mechanism",
                 "cohort": mechanisms["cohort_id"], "rule_spec": arm["rule_spec"]}
                for family in mechanisms["families"] for arm in family["arms"])
    if len(arms) != 55 or len({a["variant_id"] for a in arms}) != 55:
        raise ValueError("expected 43 unchanged controls and 12 distinct new arms")
    return {"arms": arms, "controls": controls, "mechanisms": mechanisms,
            "inventory_hash": content_hash(arms), "registered_arms": len(arms),
            "diagnostic_only": True, "authorizing": False, "selection": "none"}


def run(data: str | Path, output: str | Path, *, runtime_config: Mapping[str, Any],
        ibr_max_events: int = 1_000_000, signal_quality: bool = True,
        projection_manifest: str | Path | None = None, progress=None) -> dict:
    data = str(Path(data).resolve())
    destination = Path(output)
    if destination.exists():
        raise ValueError("choose a new immutable research output directory")
    if isinstance(ibr_max_events, bool) or not isinstance(ibr_max_events, int) or ibr_max_events < 1:
        raise ValueError("IBR event budget must be positive")
    runtime = _policy_config(runtime_config)
    source = validate_source(data, diagnostic_only=True)
    if not source.get("content_hash"):
        raise ValueError("source has no validated content identity")
    files = _code_bundle_files()
    code_hash = _code_bundle_hash(files)
    inventory = frozen_inventory(runtime, code_hash)
    projection = None
    raw_costs = None
    if projection_manifest is not None:
        manifest_path = Path(projection_manifest)
        projection = json.loads(manifest_path.read_text())
        body = dict(projection)
        digest = body.pop("manifest_hash", None)
        if (projection.get("status") != "complete" or
                projection.get("diagnostic_only") is not True or
                projection.get("authorizing") is not False or content_hash(body) != digest):
            raise ValueError("projection manifest is incomplete or invalid")
        if projection.get("config_hash") != content_hash(runtime_config):
            raise ValueError("projection runtime configuration differs")
        with Path(data).open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != projection.get("compact_sha256"):
                raise ValueError("projection data hash differs")
        artifacts = projection.get("artifacts") or {}
        raw_costs = {}
        for name in ("rawcostsfit.json", "validation.json", "calibration.json"):
            path = manifest_path.parent / name
            with path.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != artifacts.get(name):
                    raise ValueError("raw quote-cost artifact hash differs: " + name)
            raw_costs[name] = json.loads(path.read_text())
    now = datetime.now(timezone.utc)
    registration = {"schema": "trader-research-registration.v1", **inventory,
        "source": source, "runtime_config_hash": content_hash(runtime),
        "code_hash": code_hash, "code_files": list(files),
        "registered_at": now.isoformat(), "starting_cash": 100_000.0,
        "confirmation_start_not_before": (now.date() + timedelta(days=1)).isoformat(),
        "admission_preflight": admission_preflight(runtime),
        "scope": "previously_examined_retrospective_diagnostic",
        "confirmation_policy": "All comparisons stay frozen. Only unseen, accepted complete sessions may support confirmation under the unchanged holdout, power, FDR and broker-equivalence gates.",
        "automatic_winner_selection": False, "ibr_max_events": ibr_max_events,
        "signal_quality_requested": bool(signal_quality), "proofs": []}
    if projection is not None:
        registration["projection"] = projection
    destination.mkdir(parents=True)
    frozen = write_immutable_evidence(destination / "registration.json", registration)
    def emit(phase, **values):
        if progress:
            progress({"phase": phase, **values})
    emit("registered", arms=len(inventory["arms"]))
    # Calibration is measured independently; it does not lower the admission
    # gate or rewrite the protected runtime config.
    try:
        calibration = ({"schema": "unthinned-quote-calibration-run.v1",
            "diagnostic_only": True, "authorizing": False,
            "runtime_config_changed": False,
            "projection_manifest_hash": projection["manifest_hash"],
            "cost_schedule": raw_costs["rawcostsfit.json"],
            "validation_schedule": raw_costs["validation.json"],
            "stress_calibration": raw_costs["calibration.json"],
            "fit_sessions": projection["fit_sessions"],
            "validation_sessions": projection["validation_sessions"],
            "quote_rows": projection["raw_quote_counts"],
            "measurement_scope": "unthinned_raw_quotes"}
            if raw_costs is not None else run_cost_calibration(data, runtime_config=runtime))
    except (ValueError, RuntimeError) as exc:
        # Unavailable quote measurements must remain explicit. Source/feed
        # validation and the worker's strict input checks still run; this
        # fallback cannot change costs or admit an authorizing trade.
        calibration = {"schema": "stressed-cost-calibration-unavailable.v1",
            "status": "unavailable", "reason": str(exc),
            "diagnostic_only": True, "authorizing": False,
            "activation_allowed": False, "runtime_config_changed": False}
    cost_receipt = write_immutable_evidence(destination / "cost-calibration.json", calibration)
    emit("cost_calibration_complete")
    _initialize_worker(data, runtime, source["content_hash"], 100_000.0, False)
    components = []
    results = []
    decay_refs = []
    try:
        for index, arm in enumerate(inventory["arms"], 1):
            result = _evaluate_arm(arm)
            # Retain the separate legacy IBR comparison before the runtime
            # adapter attaches its stricter forward-observation measurement.
            results.append(result)
            component_name = "arm-" + content_hash(arm["variant_id"]) + ".json"
            receipt = write_immutable_evidence(
                destination / component_name,
                {"schema": "trader-research-arm.v1", "result": result,
                 "registration_hash": frozen["content_hash"],
                 "status": "unsealed_partial_diagnostic", "authorizing": False,
                 "diagnostic_only": True})
            components.append({"variant_id": arm["variant_id"],
                               "path": component_name,
                               "content_hash": receipt["content_hash"]})
            emit("arm_complete", done=index, total=len(inventory["arms"]), variant_id=arm["variant_id"])
        cohort = build_diagnostic_cohort(runtime, code_identity=code_hash, include_ibr=True)
        ibr = run_offline_forward_ibr(data, runtime_config=runtime,
            arms=[a for a in cohort["arms"] if a["strategy_id"] == "ibr"],
            source_report=source, starting_cash=100_000.0, max_events=ibr_max_events)
        write_immutable_evidence(destination / "ibr-runtime.json", ibr)
        _attach_ibr_runtime(results, ibr)
        rows = {r["variant_id"]: r["rows"] for r in results}
        meta = {r["variant_id"]: {"family": r["family"], "cohort": r["cohort"]} for r in results}
        trader = build_trader_diagnostics(rows, meta)
        trader_receipt = write_immutable_evidence(destination / "trader-diagnostics.json", trader)
        if signal_quality:
            for arm in inventory["arms"]:
                if arm["role"] != "new_mechanism":
                    continue
                emit("signal_quality", variant_id=arm["variant_id"])
                decay = measure_signal_quality(_WORKER["bars"], arm["rule_spec"],
                    policy=_WORKER["policy"], horizons=(5, 15, 30, 60))
                component_name = "signal-" + content_hash(arm["variant_id"]) + ".json"
                receipt = write_immutable_evidence(destination / component_name, decay)
                decay_refs.append({"variant_id": arm["variant_id"],
                    "path": component_name, "content_hash": receipt["content_hash"]})
        # A partial run can never be read as a sealed completed result.
        if _code_bundle_hash(_code_bundle_files()) != code_hash:
            raise ValueError("code changed during research")
        if validate_source(data, diagnostic_only=True).get("content_hash") != source["content_hash"]:
            raise ValueError("source changed during research")
        report = {"schema": "trader-research.v1", "status": "diagnostic_complete",
            "diagnostic_only": True, "authorizing": False, "eligible": [],
            "proofs": [], "selection": "none", "portfolio_pnl": None,
            "registration_hash": frozen["content_hash"], "source": source,
            "code_hash": code_hash, "runtime_config_hash": content_hash(runtime),
            "components": components, "signal_quality": decay_refs,
            "cost_calibration_hash": cost_receipt["content_hash"],
            "trader_diagnostics_hash": trader_receipt["content_hash"],
            "ibr_runtime": {k: v for k, v in ibr.items() if k != "arms"},
            "results": results,
            "outcome_counts": dict(Counter(r["outcome"] for r in results)),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "limitations": ["historical_diagnostics_are_not_untouched_validation",
                "rejected_sessions_and_censored_losses_retained",
                "quote_costs_do_not_measure_actual_fills_or_broker_equivalence",
                "overlapping_variants_are_not_independent_or_a_portfolio",
                "live_cadence_requires_a_fresh_market_session"]}
        receipt = write_immutable_evidence(destination / "result.json", report)
        emit("completed", output=str(destination), arms=len(results), authorizing=False)
        return receipt
    finally:
        _close_worker()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ibr-max-events", type=int, default=1_000_000)
    parser.add_argument("--skip-signal-quality", action="store_true")
    parser.add_argument("--projection-manifest", type=Path)
    args = parser.parse_args(argv)
    run(args.data, args.output, runtime_config=load_config(args.config),
        ibr_max_events=args.ibr_max_events, signal_quality=not args.skip_signal_quality,
        projection_manifest=args.projection_manifest,
        progress=lambda value: print(json.dumps(value, allow_nan=False), flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
