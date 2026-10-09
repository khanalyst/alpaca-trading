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
import gc
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
from .fit_diagnostics import _fit_prefixes
from .source_validation import validate_source
from .trader_diagnostics import build_trader_diagnostics


_SIGNAL_QUALITY_HORIZONS = (5, 15, 30, 60)


def _valid_sha256(value: Any) -> bool:
    """Return whether a value is a canonical hexadecimal SHA-256 digest."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except (TypeError, ValueError):
        return False
    return True


def _additive_execution_arms(
        inventory: Mapping[str, Any], *,
        include_deadline_compatible: bool = False,
) -> list[Mapping[str, Any]]:
    """Validate the ordered additive suffix used by geometry runs.

    The first 55 arms are the sealed comparison inventory.  Additive arms
    must remain in manifest order and retain their manifest cohort identity;
    this prevents a reordered or cross-cohort arm from being measured under
    the wrong diagnostic label.
    """
    arms = inventory.get("arms")
    expected_total = 70 if include_deadline_compatible else 67
    if not isinstance(arms, list) or len(arms) != expected_total:
        raise ValueError(
            "additive geometry requires a " + str(expected_total) +
            "-arm inventory")

    def manifest_arms(key: str, role: str, start: int, count: int) -> list[Mapping[str, Any]]:
        cohort = inventory.get(key)
        families = cohort.get("families") if isinstance(cohort, Mapping) else None
        if not isinstance(families, list) or not isinstance(cohort, Mapping):
            raise ValueError("additive geometry is missing the " + key + " manifest")
        expected = [
            arm for family in families
            if isinstance(family, Mapping)
            for arm in (family.get("arms") or ())
            if isinstance(arm, Mapping)
        ]
        actual = arms[start:start + count]
        if len(expected) != count or len(actual) != count:
            raise ValueError(
                "additive geometry requires exactly " + str(count) +
                " ordered " + role + " execution arms")
        cohort_id = cohort.get("cohort_id")
        for expected_arm, actual_arm in zip(expected, actual):
            if (actual_arm.get("variant_id") != expected_arm.get("variant_id") or
                    actual_arm.get("role") != role or
                    actual_arm.get("cohort") != cohort_id or
                    actual_arm.get("rule_spec") != expected_arm.get("rule_spec") or
                    actual_arm.get("parent_variant_id") != expected_arm.get("parent_variant_id")):
                raise ValueError(
                    "additive geometry has an ordered " + role +
                    " arm or cohort binding mismatch")
            for metadata_key in ("geometry_delta", "timing_delta"):
                if metadata_key in expected_arm and actual_arm.get(metadata_key) != expected_arm[metadata_key]:
                    raise ValueError(
                        "additive geometry has a " + role + " " + metadata_key +
                        " mismatch")
        return actual

    cost_arms = manifest_arms(
        "cost_compatible_mechanisms", "cost_compatible_mechanism", 55, 12)
    deadline_arms = []
    if include_deadline_compatible:
        deadline_arms = manifest_arms(
            "deadline_compatible_mechanisms",
            "deadline_compatible_mechanism", 67, 3)
    suffix = [*cost_arms, *deadline_arms]
    suffix_ids = [str(arm.get("variant_id")) for arm in suffix]
    if len(set(suffix_ids)) != len(suffix_ids):
        raise ValueError("additive geometry execution arms must have unique variant IDs")
    return suffix


def _validate_signal_quality_result(
        result: Mapping[str, Any], expected_variant_id: str) -> None:
    """Validate the compact signal artifact before it becomes immutable."""
    if (result.get("variant_id") != expected_variant_id or
            result.get("measurement_scope") !=
            "previously_examined_retrospective_diagnostic" or
            result.get("authorizing") is not False or
            result.get("diagnostic_only") is not True or
            result.get("horizons") != list(_SIGNAL_QUALITY_HORIZONS)):
        raise ValueError(
            "signal-quality artifact failed variant, scope, horizon, or "
            "non-authorizing validation: " + str(expected_variant_id))


def frozen_inventory(config: Mapping[str, Any], code_hash: str, *,
                     include_cost_compatible: bool = False,
                     include_deadline_compatible: bool = False) -> dict:
    if include_deadline_compatible and not include_cost_compatible:
        raise ValueError(
            "deadline-compatible cohort requires the cost-compatible cohort")
    controls = build_inventory(config, code_hash=code_hash)
    mechanisms = mechanism_cohort()
    arms = list(controls["arms"])
    arms.extend({"variant_id": arm["variant_id"], "strategy_id": "rule",
                 "family": family["name"], "role": "new_mechanism",
                 "cohort": mechanisms["cohort_id"], "rule_spec": arm["rule_spec"]}
                for family in mechanisms["families"] for arm in family["arms"])
    if len(arms) != 55 or len({a["variant_id"] for a in arms}) != 55:
        raise ValueError("expected 43 unchanged controls and 12 distinct new arms")
    extra = {}
    if include_cost_compatible:
        from .mechanism_cost_cohort import mechanism_cost_cohort
        compatible = mechanism_cost_cohort()
        arms.extend({"variant_id": arm["variant_id"], "strategy_id": "rule",
                     "family": family["name"], "role": "cost_compatible_mechanism",
                     "cohort": compatible["cohort_id"], "rule_spec": arm["rule_spec"],
                     "parent_variant_id": arm["parent_variant_id"],
                     "geometry_delta": arm["geometry_delta"]}
                    for family in compatible["families"] for arm in family["arms"])
        if len(arms) != 67 or len({a["variant_id"] for a in arms}) != 67:
            raise ValueError("expected 55 frozen comparisons and 12 distinct cost-compatible arms")
        extra["cost_compatible_mechanisms"] = compatible
        if include_deadline_compatible:
            from .mechanism_deadline_cohort import mechanism_deadline_cohort
            deadline = mechanism_deadline_cohort()
            arms.extend({"variant_id": arm["variant_id"], "strategy_id": "rule",
                         "family": family["name"],
                         "role": "deadline_compatible_mechanism",
                         "cohort": deadline["cohort_id"],
                         "rule_spec": arm["rule_spec"],
                         "parent_variant_id": arm["parent_variant_id"],
                         "timing_delta": arm["timing_delta"]}
                        for family in deadline["families"] for arm in family["arms"])
            if len(arms) != 70 or len({a["variant_id"] for a in arms}) != 70:
                raise ValueError(
                    "expected 67 frozen comparisons and 3 distinct deadline-compatible arms")
            extra["deadline_compatible_mechanisms"] = deadline
        # Validate the complete suffix at the construction boundary as well
        # as when additive execution is selected.
        _additive_execution_arms(
            {"arms": arms, **extra},
            include_deadline_compatible=include_deadline_compatible)
    return {"arms": arms, "controls": controls, "mechanisms": mechanisms, **extra,
            "inventory_hash": content_hash(arms), "registered_arms": len(arms),
            "diagnostic_only": True, "authorizing": False, "selection": "none"}


def run(data: str | Path, output: str | Path, *, runtime_config: Mapping[str, Any],
        ibr_max_events: int = 1_000_000, signal_quality: bool = True,
        include_cost_compatible: bool = False,
        include_deadline_compatible: bool = False,
        evaluation_scope: str = "full_frozen_inventory",
        baseline_result: str | Path | None = None,
        projection_manifest: str | Path | None = None, progress=None) -> dict:
    data = str(Path(data).resolve())
    destination = Path(output)
    if destination.exists():
        raise ValueError("choose a new immutable research output directory")
    if include_deadline_compatible and not include_cost_compatible:
        raise ValueError(
            "deadline-compatible cohort requires include_cost_compatible=True")
    if isinstance(ibr_max_events, bool) or not isinstance(ibr_max_events, int) or ibr_max_events < 1:
        raise ValueError("IBR event budget must be positive")
    runtime = _policy_config(runtime_config)
    source = validate_source(data, diagnostic_only=True)
    if not source.get("content_hash"):
        raise ValueError("source has no validated content identity")
    files = _code_bundle_files()
    code_hash = _code_bundle_hash(files)
    inventory = frozen_inventory(
        runtime, code_hash,
        include_cost_compatible=include_cost_compatible,
        include_deadline_compatible=include_deadline_compatible)
    if evaluation_scope not in {"full_frozen_inventory", "additive_geometry"}:
        raise ValueError("unknown evaluation scope")
    if evaluation_scope == "additive_geometry" and not include_cost_compatible:
        raise ValueError("additive geometry requires the cost-compatible cohort")
    if evaluation_scope == "additive_geometry" and not signal_quality:
        raise ValueError("additive geometry requires signal_quality=True")
    execution_arms = (
        _additive_execution_arms(
            inventory,
            include_deadline_compatible=include_deadline_compatible)
        if include_cost_compatible else [])
    evaluated_arms = (inventory["arms"] if evaluation_scope == "full_frozen_inventory"
                      else execution_arms)
    baseline = None
    if baseline_result is not None:
        path = Path(baseline_result)
        previous = json.loads(path.read_text())
        body = dict(previous)
        digest = body.pop("content_hash", None)
        if (content_hash(body) != digest or
                previous.get("status") != "diagnostic_complete" or
                previous.get("authorizing") is not False or
                previous.get("diagnostic_only") is not True or
                previous.get("eligible") != [] or previous.get("proofs") != [] or
                previous.get("selection") != "none"):
            raise ValueError("baseline result is not sealed non-authorizing evidence")
        previous_source = previous.get("source")
        if (not isinstance(previous_source, Mapping) or
                previous_source.get("content_hash") != source["content_hash"] or
                previous.get("runtime_config_hash") != content_hash(runtime)):
            raise ValueError("baseline source or runtime policy differs")
        identity_fields = ("variant_id", "strategy_id", "family", "role", "cohort", "rule_spec", "overrides")
        old_arms = previous.get("results") or []
        if (not isinstance(old_arms, list) or len(old_arms) != 55 or
                any(not isinstance(arm, Mapping) for arm in old_arms) or
                [{k: arm.get(k) for k in identity_fields} for arm in old_arms] !=
                [{k: arm.get(k) for k in identity_fields} for arm in inventory["arms"][:55]]):
            raise ValueError("baseline differs from the 55 frozen comparisons")
        if any(not isinstance(arm, Mapping) or
               arm.get("authorizing") is not False or
               arm.get("diagnostic_only") is not True or
               arm.get("eligible") is not False
               for arm in old_arms):
            raise ValueError(
                "baseline inner results are not sealed non-authorizing "
                "diagnostic non-eligible evidence")
        baseline_code_hash = previous.get("code_hash")
        if (not _valid_sha256(baseline_code_hash) or
                baseline_code_hash == code_hash):
            raise ValueError(
                "baseline code_hash must be a valid SHA-256 digest from "
                "previous code")
        with path.open("rb") as handle:
            baseline = {"path": str(path.resolve()), "content_hash": digest,
                "sha256": hashlib.file_digest(handle, "sha256").hexdigest(),
                "code_hash": baseline_code_hash, "arms": 55,
                "scope": "sealed_previous_code_comparison_not_recomputed",
                "diagnostic_only": True, "authorizing": False}
    if evaluation_scope == "additive_geometry" and baseline is None:
        raise ValueError("additive geometry requires a verified frozen baseline result")
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
    registration["resource_policy"] = "rule_and_signal_worker_closed_before_strict_ibr"
    registration["signal_measurement_scope"] = "previously_examined_retrospective_diagnostic"
    registration["evaluation_scope"] = evaluation_scope
    registration["evaluated_variant_ids"] = [a["variant_id"] for a in evaluated_arms]
    registration["execution_variant_ids"] = [a["variant_id"] for a in evaluated_arms]
    registration["baseline_comparison"] = baseline
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
    signal_quality_arms = [arm for arm in inventory["arms"]
                           if arm["role"] in {
                               "new_mechanism", "cost_compatible_mechanism",
                               "deadline_compatible_mechanism"}]
    try:
        for index, arm in enumerate(evaluated_arms, 1):
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
            emit("arm_complete", done=index, total=len(evaluated_arms), variant_id=arm["variant_id"])
        if signal_quality:
            for arm in signal_quality_arms:
                emit("signal_quality", variant_id=arm["variant_id"])
                prefix = _fit_prefixes(_WORKER["bars"], arm["rule_spec"],
                                       policy=_WORKER["policy"])
                decay = measure_signal_quality(_WORKER["bars"], arm["rule_spec"],
                    policy=_WORKER["policy"], horizons=(5, 15, 30, 60),
                    precomputed_first_signals=prefix["first_signals"],
                    eligibility_provenance=prefix["eligibility_provenance"],
                    measurement_scope="previously_examined_retrospective_diagnostic")
                if not isinstance(decay, Mapping):
                    raise ValueError(
                        "signal-quality artifact is not a mapping: " +
                        str(arm["variant_id"]))
                _validate_signal_quality_result(decay, arm["variant_id"])
                component_name = "signal-" + content_hash(arm["variant_id"]) + ".json"
                receipt = write_immutable_evidence(destination / component_name, decay)
                decay_refs.append({"variant_id": arm["variant_id"],
                    "path": component_name, "content_hash": receipt["content_hash"]})
                del prefix, decay
        # The strict IBR worker builds its own replay corpus. Retaining the
        # rule quote index here doubled memory and killed the previous run.
        _close_worker()
        gc.collect()
        emit("rule_worker_released")
        if evaluation_scope == "full_frozen_inventory":
            cohort = build_diagnostic_cohort(runtime, code_identity=code_hash, include_ibr=True)
            ibr = run_offline_forward_ibr(data, runtime_config=runtime,
                arms=[a for a in cohort["arms"] if a["strategy_id"] == "ibr"],
                source_report=source, starting_cash=100_000.0, max_events=ibr_max_events)
            _attach_ibr_runtime(results, ibr)
        else:
            ibr = {"status": "not_recomputed", "arms": {},
                   "reason": "additive_geometry_scope_uses_sealed_previous_comparison",
                   "baseline_comparison": baseline, "authorizing": False,
                   "diagnostic_only": True}
        if evaluation_scope == "additive_geometry":
            evaluated_ids = [str(result.get("variant_id")) for result in results]
            expected_ids = [str(arm["variant_id"]) for arm in execution_arms]
            if evaluated_ids != expected_ids:
                raise ValueError(
                    "additive execution results do not match the ordered "
                    + str(len(execution_arms)) + "-arm inventory suffix")
        write_immutable_evidence(destination / "ibr-runtime.json", ibr)
        rows = {r["variant_id"]: r["rows"] for r in results}
        meta = {r["variant_id"]: {"family": r["family"], "cohort": r["cohort"]} for r in results}
        trader = build_trader_diagnostics(rows, meta)
        trader_receipt = write_immutable_evidence(destination / "trader-diagnostics.json", trader)
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
            "evaluation_scope": evaluation_scope,
            "registered_arms": len(inventory["arms"]), "evaluated_arms": len(results),
            "baseline_comparison": baseline,
            "not_recomputed_variant_ids": [a["variant_id"] for a in inventory["arms"]
                if a["variant_id"] not in {r["variant_id"] for r in results}],
            "not_recomputed_execution_variant_ids": [
                a["variant_id"] for a in inventory["arms"][:55]
                if a["variant_id"] not in {r["variant_id"] for r in results}],
            "not_recomputed_execution_count": sum(
                a["variant_id"] not in {r["variant_id"] for r in results}
                for a in inventory["arms"][:55]),
            "components": components, "signal_quality": decay_refs,
            "signal_quality_variant_ids": [ref["variant_id"] for ref in decay_refs],
            "signal_quality_count": len(decay_refs),
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
    parser.add_argument("--include-cost-compatible", action="store_true")
    parser.add_argument("--include-deadline-compatible", action="store_true")
    parser.add_argument("--evaluation-scope", choices=("full_frozen_inventory", "additive_geometry"),
                        default="full_frozen_inventory")
    parser.add_argument("--baseline-result", type=Path)
    parser.add_argument("--projection-manifest", type=Path)
    args = parser.parse_args(argv)
    run(args.data, args.output, runtime_config=load_config(args.config),
        ibr_max_events=args.ibr_max_events, signal_quality=not args.skip_signal_quality,
        include_cost_compatible=args.include_cost_compatible,
        include_deadline_compatible=args.include_deadline_compatible,
        evaluation_scope=args.evaluation_scope, baseline_result=args.baseline_result,
        projection_manifest=args.projection_manifest,
        progress=lambda value: print(json.dumps(value, allow_nan=False), flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
