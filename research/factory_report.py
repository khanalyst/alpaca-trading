"""Readable narrative of what autonomous research actually did.

The factory and edge ledgers already record everything: each hypothesis, every
variant's full gate with its named checks, the diagnosis behind each mutation,
why a family was retired and after how many variants, and the provider/prompt
hashes behind an LLM proposal.  None of it was readable — ``factory status``
returned hypothesis rows and three counts, and the rest sat inside JSON blobs
no command opened.

This module is the reader.  It is strictly derived: it opens the ledgers
read-only, never writes, and computes nothing it cannot show the evidence for.
Where a number came from a gate, the gate's own hash is reported beside it, so
a claim in this report can always be traced back to the immutable row it came
from.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from agent.contracts.rule import RULE_FAMILIES
from .gates import gate_dependence_report
from .edge_ledger_store import DEFAULT_DB_PATH, VEHICLES, content_hash
from .factory_ledger import dependence_policy_digest
from .stats import cross_family_dependence_report

REPORT_SCHEMA = "factory-report.v1"
DEPENDENCE_POLICY_REPORT_SCHEMA = "dependence-policy.v1"

# Origins where the model actually authored the hypothesis.  Everything else
# is deterministic, even when a provider was asked first and refused.
_LLM_ORIGINS = frozenset(("llm_discovery", "llm_replacement",
                          "reseed_after_proof"))

# Ordered so the narrative reads as a lifecycle rather than an alphabet.
_CHECK_LABELS = {
    "fit_structurally_adequate": "fit sample big enough",
    "heldout_structurally_adequate": "held-out sample big enough",
    "separated": "fit and held-out do not overlap",
    "actual_control_available": "a real matched baseline existed",
    "fit_delta_positive": "beat the baseline in fit",
    "heldout_delta_positive": "beat the baseline held-out",
    "heldout_delta_lcb_positive": "held-out edge survives its lower bound",
    "heldout_p_significant": "held-out result is significant",
    "falsification": "beat a placebo/sign-flipped null",
    "heldout_net_pnl_positive": "made money held-out",
    "heldout_expectancy_positive": "positive expectancy per trade",
    "null_control_available": "a randomized-entry null existed",
    "null_control_delta_positive": "beat randomized entry timing",
    "walk_forward_majority_positive": "positive in most walk-forward folds",
    "qualification_net_positive": "made money in the sealed final window",
    "qualification_delta_positive": "beat the baseline in the sealed window",
    "family_fdr_significant": "survives multiple-testing within its family",
    "global_fdr_significant": "survives multiple-testing across the cycle",
    "cluster_fdr_significant": "survives frozen dependence-cluster multiplicity",
}


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, (bool, str, bytes)) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _risk_summary_value(section: Any) -> float | None:
    """Read the representative value from a structured fit-risk summary."""
    if isinstance(section, Mapping):
        # ``median`` is stable and resistant to one unusually large plan.  A
        # mean-only summary is still supported for older/compact diagnostics.
        for key in ("median", "mean", "p50", "value"):
            value = _number(section.get(key))
            if value is not None:
                return value
        return None
    return _number(section)


def _fit_risk_metrics(
        fit_diagnostics: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalize the versioned fit risk summary for human-readable output.

    Current diagnostics name the configured pre-cap budget explicitly and
    retain ``intended`` / ``delivered_to_intended`` as compatibility aliases.
    Accept both shapes without inventing a planned value when it is not
    persisted.
    """
    if not isinstance(fit_diagnostics, Mapping):
        return None
    risk = fit_diagnostics.get("risk")
    if not isinstance(risk, Mapping):
        return None

    def first(*names: str) -> Any:
        for name in names:
            if name in risk and risk.get(name) is not None:
                return risk.get(name)
        return None

    configured = first("configured", "configured_budget", "risk_budget",
                       "budget", "intended")
    planned = first("planned", "effective")
    delivered = first("capped_delivered", "delivered", "actual",
                      "effective_delivered")
    ratio_name = next((name for name in (
        "delivered_to_planned", "delivered_to_configured",
        "delivered_to_intended", "delivery_ratio")
                       if name in risk and risk.get(name) is not None), None)
    ratio = risk.get(ratio_name) if ratio_name is not None else None
    if configured is None and planned is None and delivered is None and ratio is None:
        return None
    return {"configured": _risk_summary_value(configured),
            "planned": _risk_summary_value(planned),
            "delivered": _risk_summary_value(delivered),
            "ratio": _risk_summary_value(ratio),
            "ratio_label": ("delivered/planned"
                             if ratio_name == "delivered_to_planned" else
                             "delivered/configured")}


def _fit_signal_quality_metrics(
        fit_diagnostics: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return compact conditional-return horizons for narrative rendering."""
    if not isinstance(fit_diagnostics, Mapping):
        return []
    quality = fit_diagnostics.get("signal_quality")
    metrics = quality.get("horizon_metrics") if isinstance(quality, Mapping) else None
    if not isinstance(metrics, Mapping):
        return []
    result = []
    for label, item in metrics.items():
        if not isinstance(item, Mapping):
            continue
        count = _number(item.get("candidate_count"))
        if count is None or count <= 0:
            continue
        result.append({
            "horizon": str(label), "count": int(count),
            "mean_bps": _number(item.get("mean_forward_return_bps")),
            "control_delta_bps": _number(item.get("candidate_minus_control_bps")),
            # A mean with no error term is what let a 47-trade replay read as a
            # finding.  Never render one of these numbers without its t.
            "control_delta_t": _number(item.get("candidate_minus_control_t_stat")),
            "after_cost_bps": _number(item.get("mean_after_hurdle_bps")),
            "after_cost_t": _number(item.get("after_hurdle_t_stat")),
        })
    return result


def _fit_path_telemetry_metrics(
        fit_diagnostics: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return compact grouped MFE/MAE diagnostics for report rendering."""
    if not isinstance(fit_diagnostics, Mapping):
        return []
    section = fit_diagnostics.get("path_telemetry")
    groups = section.get("groups") if isinstance(section, Mapping) else None
    if not isinstance(groups, Sequence):
        return []
    result: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, Mapping) or not group.get("count"):
            continue
        def median_value(name: str) -> float | None:
            metric = group.get(name)
            if not isinstance(metric, Mapping):
                return _number(metric)
            return _number(metric.get("median"))
        result.append({
            "target_r": _number(group.get("target_r")),
            "max_hold_bars": group.get("max_hold_bars"),
            "exit_reason": str(group.get("exit_reason") or "unknown"),
            "count": int(group.get("count") or 0),
            "censored": int(group.get("right_censored") or 0),
            "gapped": int(group.get("gapped") or 0),
            "mfe_bps": median_value("mfe_bps"),
            "mae_bps": median_value("mae_bps"),
            "mfe_r": median_value("mfe_r"),
            "mae_r": median_value("mae_r"),
        })
    return result


def _fit_target_hold_reachability(
        fit_diagnostics: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the compact fit-only target/hold diagnostic for rendering."""
    if not isinstance(fit_diagnostics, Mapping):
        return None
    section = fit_diagnostics.get("target_hold_reachability")
    if not isinstance(section, Mapping):
        return None
    configured = section.get("configured")
    configured = configured if isinstance(configured, Mapping) else {}
    recommendation = section.get("recommendation")
    recommendation = recommendation if isinstance(recommendation, Mapping) else None
    return {
        "total": int(section.get("total") or 0),
        "usable": int(section.get("usable") or 0),
        "censored": int(section.get("censored") or 0),
        "expiry_count": int(section.get("expiry_count") or 0),
        "unreachable_count": int(section.get("unreachable_count") or 0),
        "unreachable_rate": _number(section.get("unreachable_rate")),
        "status": str(section.get("status") or "unknown"),
        "target_r": _number(configured.get("target_r")),
        "max_hold_bars": configured.get("max_hold_bars"),
        "recommendation": ({
            "target_r": _number(recommendation.get("target_r")),
            "max_hold_bars": recommendation.get("max_hold_bars"),
        } if recommendation is not None else None),
    }


def _dependence_policy_row(row: Mapping[str, Any]) -> dict:
    """Decode and hash-check one frozen policy without opening a writable ledger."""
    try:
        source_cycles = json.loads(row.get("source_cycles_json") or "[]")
        cluster_map = json.loads(row.get("cluster_map_json") or "{}")
        evidence = json.loads(row.get("evidence_json") or "{}")
        cutoff = float(row.get("cutoff"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"verified_persisted": False, "reason": "malformed_policy"}
    body = {"schema": str(row.get("schema") or DEPENDENCE_POLICY_REPORT_SCHEMA),
            "version": 1, "target_cycle_id": str(row.get("target_cycle_id") or ""),
            "vehicle": str(row.get("vehicle") or ""), "cutoff": cutoff,
            "source_cycles": source_cycles, "cluster_map": cluster_map,
            "evidence": evidence}
    return {**body, "policy_id": row.get("policy_id"),
            "policy_hash": row.get("policy_hash"),
            "policy_digest": dependence_policy_digest(body),
            "verified_persisted": content_hash(body) == str(row.get("policy_hash") or "")}


def _failed_checks(gate: Mapping[str, Any]) -> list[str]:
    """Named reasons a variant did not pass, in lifecycle order."""
    envelope = gate.get("verified_gate")
    checks = (envelope.get("checks") if isinstance(envelope, Mapping) else None)
    if not isinstance(checks, Mapping):
        checks = gate.get("checks_without_family")
    if not isinstance(checks, Mapping):
        return []
    return [_CHECK_LABELS.get(name, name)
            for name in _CHECK_LABELS if checks.get(name) is False]


def _variant_row(record: Mapping[str, Any]) -> dict:
    """One variant's performance, without dragging its trade rows along."""
    gate = record.get("gate") if isinstance(record.get("gate"), Mapping) else {}
    account = (record.get("account")
               if isinstance(record.get("account"), Mapping) else {})
    diagnosis = (record.get("diagnostic")
                 if isinstance(record.get("diagnostic"), Mapping) else {})
    statistics = gate.get("global_multiple_tests")
    q_value = (_number(statistics.get("p_adjusted"))
               if isinstance(statistics, Mapping) else None)
    verified = gate.get("verified_gate")
    spec = record.get("rule_spec") if isinstance(record.get("rule_spec"), Mapping) else {}
    return {
        "variant_id": record.get("variant_id"),
        "family": spec.get("family"),
        "rule_schema": spec.get("schema"),
        "lane": record.get("mode"),
        "evaluated": {"start": record.get("evaluation_start"),
                      "end": record.get("evaluation_end")},
        "trades": account.get("trades"),
        "net_pnl": _number(account.get("realized_pnl")),
        "max_drawdown": _number(account.get("max_drawdown")),
        "fit_trades": gate.get("fit_trades"),
        "heldout_trades": gate.get("heldout_trades"),
        "heldout_net_pnl": _number(gate.get("heldout_net_pnl")),
        "heldout_expectancy": _number(gate.get("heldout_expectancy")),
        "heldout_delta": _number((gate.get("test") or {}).get("mean_delta")
                                 if isinstance(gate.get("test"), Mapping) else None),
        "heldout_delta_lcb": _number(gate.get("heldout_delta_lcb")),
        "p_raw": _number(gate.get("p_raw")),
        "q_value": q_value,
        "cluster_q_value": _number((gate.get("cluster_multiple_tests") or {}).get(
            "p_adjusted") if isinstance(gate.get("cluster_multiple_tests"), Mapping)
            else None),
        "cluster_multiple_tests": (dict(gate.get("cluster_multiple_tests") or {})
                                    if isinstance(gate.get("cluster_multiple_tests"), Mapping)
                                    else {}),
        "confidence": _number(gate.get("confidence")),
        "is_root": bool(gate.get("is_root")),
        "null_control": gate.get("null_control"),
        "passes": bool(gate.get("passes")),
        "classification": record.get("classification") or (
            "proved" if gate.get("passes") else
            "underpowered" if not (gate.get("sample_adequate") and
                                    gate.get("heldout_sample_adequate")) else
            "adequate_inconclusive"),
        "underpowered": not (gate.get("sample_adequate") and
                             gate.get("heldout_sample_adequate")),
        "failed_checks": _failed_checks(gate),
        "primary_failure": diagnosis.get("primary_failure"),
        "win_rate": _number(diagnosis.get("win_rate")),
        "profit_factor": _number(diagnosis.get("profit_factor")),
        # Fit-only observability.  The persisted diagnostic is compact and
        # deliberately contains no account trade rows or held-out data.
        "fit_diagnostics": (diagnosis.get("fit_diagnostics")
                             if isinstance(diagnosis.get("fit_diagnostics"), Mapping)
                             else None),
        # Explain which source statistics are shared by multiple gate checks;
        # this is diagnostic provenance and does not alter the verdict.
        "gate_dependence": gate_dependence_report(
            verified if isinstance(verified, Mapping) else gate),
        "gate_hash": gate.get("gate_hash"),
        # Why this variant was tried, and who decided to try it.  Fixed before
        # the gate beside it was computed, so the pair reads as a prediction
        # and its result rather than as a summary written afterwards.
        "reason": record.get("reason"),
        "proposed_by": record.get("proposed_by"),
    }


def _origin(events: Sequence[Mapping[str, Any]],
            parent_events: Sequence[Mapping[str, Any]],
            hypothesis_id: str, generation: int) -> dict:
    """Where this hypothesis came from, and who proposed it.

    A genesis slot has no ancestor, so its seeding decision is recorded on the
    hypothesis itself; every other origin is recorded on the ancestor that was
    replaced. Own events are checked first so genesis is never mistaken for a
    template when a model actually proposed it.
    """
    for event in reversed(events):
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping) or payload.get("seeded") is not True:
            continue
        origin: dict[str, Any] = {"kind": str(payload.get("source") or "template"),
                                  "reason": event.get("reason")}
        evidence = payload.get("llm_evidence")
        if isinstance(evidence, Mapping):
            attempt_errors = evidence.get("attempt_errors")
            if not isinstance(attempt_errors, (list, tuple)):
                attempt_errors = []
            origin["llm"] = {
                "provider": evidence.get("provider"),
                "model": evidence.get("model"),
                "attempts": evidence.get("attempts"),
                "request_hash": evidence.get("request_hash"),
                "response_hash": evidence.get("raw_response_hash"),
                "prompt_hash": evidence.get("system_prompt_hash"),
                "config_hash": evidence.get("config_hash"),
                "response_schema_hash": evidence.get("response_schema_hash"),
                "grammar_schema_hash": evidence.get("grammar_schema_hash"),
                "max_total_calls": evidence.get("max_total_calls"),
                "calls_used": evidence.get("calls_used"),
                "calls_remaining": evidence.get("calls_remaining"),
                "auth_circuit_open": evidence.get("auth_circuit_open"),
                "attempt_errors": [str(item)[:240] for item in attempt_errors[:3]],
                "attempt_evidence": list(evidence.get("attempt_evidence") or ())[:3],
            }
        if payload.get("llm_error"):
            origin["llm_error"] = payload["llm_error"]
        return origin
    for event in reversed(parent_events):
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        successor = (payload.get("replacement_hypothesis_id") or
                     payload.get("successor_hypothesis_id"))
        if successor != hypothesis_id:
            continue
        evidence = payload.get("llm_evidence")
        source = payload.get("source")
        if payload.get("rotation") is True:
            kind = "rotation"
        elif payload.get("reseed") is True:
            kind = "reseed_after_proof"
        elif isinstance(evidence, Mapping) and evidence.get("kind") != "discovery":
            kind = "llm_replacement"
        else:
            kind = "replacement"
        origin: dict[str, Any] = {"kind": kind, "reason": event.get("reason")}
        if source:
            origin["source"] = source
        if isinstance(evidence, Mapping):
            attempt_errors = evidence.get("attempt_errors")
            if not isinstance(attempt_errors, (list, tuple)):
                attempt_errors = []
            origin["llm"] = {
                "provider": evidence.get("provider"),
                "model": evidence.get("model"),
                "attempts": evidence.get("attempts"),
                "request_hash": evidence.get("request_hash"),
                "response_hash": evidence.get("raw_response_hash"),
                "prompt_hash": evidence.get("system_prompt_hash"),
                "config_hash": evidence.get("config_hash"),
                "response_schema_hash": evidence.get("response_schema_hash"),
                "grammar_schema_hash": evidence.get("grammar_schema_hash"),
                "max_total_calls": evidence.get("max_total_calls"),
                "calls_used": evidence.get("calls_used"),
                "calls_remaining": evidence.get("calls_remaining"),
                "auth_circuit_open": evidence.get("auth_circuit_open"),
                "attempt_errors": [str(item)[:240] for item in attempt_errors[:3]],
                "attempt_evidence": list(evidence.get("attempt_evidence") or ())[:3],
            }
        if payload.get("llm_error"):
            origin["llm_error"] = payload["llm_error"]
        return origin
    return {"kind": "template" if generation == 0 else "seed"}


def _outcome(events: Sequence[Mapping[str, Any]], status: str) -> dict:
    """What finally happened to this hypothesis, and on what evidence."""
    for event in reversed(events):
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            payload = {}
        if event.get("status") == "retired":
            diagnosis = payload.get("diagnostic") or {}
            return {
                "kind": ("recentered" if payload.get("recentered") or
                          payload.get("mode") == "recenter" else
                          "rotated" if payload.get("rotation") else "retired"),
                "reason": event.get("reason"),
                "variants_tested": payload.get("tested_variants"),
                "variants_intended": payload.get("expected_variants"),
                "primary_failure": (diagnosis.get("primary_failure")
                                    if isinstance(diagnosis, Mapping) else None),
                "replacement_hypothesis_id": payload.get("replacement_hypothesis_id"),
                "replacement_variant_id": payload.get("replacement_variant_id"),
                # Retirement is only legal against adequately powered failed
                # gates; their hashes are the proof it was.
                "failed_gate_hashes": payload.get("verified_gate_hashes") or [],
                "from_variant_id": payload.get("from_variant_id"),
                "to_variant_id": payload.get("to_variant_id"),
                "fit_score": payload.get("fit_score"),
                "fit_score_source": payload.get("fit_score_source"),
                "closure_mode": payload.get("mode"),
            }
        if event.get("status") == "bounded_space_exhausted":
            return {"kind": "bounded_space_exhausted",
                    "reason": event.get("reason"),
                    "search_state": payload.get("search_state") or {},
                    "max_generations": payload.get("generation_cap")}
        if event.get("status") == "validated" and payload.get("passing"):
            return {"kind": "proved", "reason": event.get("reason"),
                    "proved_variants": payload.get("passing"),
                    "successor_hypothesis_id": payload.get("successor_hypothesis_id"),
                    "successor_source": payload.get("source")}
        if event.get("status") in {"pending_generation_limit",
                                   "pending_llm_replacement"}:
            return {"kind": event["status"], "reason": event.get("reason"),
                    "failure": payload.get("failure"),
                    "llm_error": payload.get("error"),
                    "max_generations": payload.get("max_generations"),
                    "rotations_used": payload.get("rotations_used")}
    return {"kind": status or "active"}


def _tuning_events(events: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Expose provider tuning evidence recorded on testing events."""
    output: list[dict] = []
    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping) or "evidence" not in payload:
            continue
        evidence = payload.get("evidence")
        if not isinstance(evidence, Mapping) or evidence.get("kind") != "tuning":
            continue
        item = {
            "created_at": event.get("created_at"),
            "schema": payload.get("schema"),
            "success": bool(payload.get("success")),
            "tuned_variants": payload.get("tuned_variants", 0),
            "evidence": dict(evidence),
        }
        if payload.get("error"):
            item["error"] = str(payload["error"])[:500]
        output.append(item)
    return output


def _fit_events(events: Sequence[Mapping[str, Any]]) -> dict:
    """Return the latest fit alias audit without exposing raw observations."""
    for event in reversed(events):
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        if not (payload.get("fit_diagnostics") or
                payload.get("excluded_behavior_aliases") or
                payload.get("behavior_aliases")):
            continue
        diagnostics = payload.get("fit_diagnostics")
        return {
            "diagnostics": dict(diagnostics) if isinstance(diagnostics, Mapping) else {},
            "behavior_aliases": dict(payload.get("behavior_aliases") or {}),
            "excluded_behavior_aliases": list(
                payload.get("excluded_behavior_aliases") or ()),
            "proposed_behavior_aliases": list(
                payload.get("proposed_behavior_aliases") or ()),
        }
    return {"diagnostics": {}, "behavior_aliases": {},
            "excluded_behavior_aliases": [], "proposed_behavior_aliases": []}


def _screen_events(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the latest compact fit-only signal-quality screen audit.

    Screened variants intentionally have no account/gate row.  Keep their
    status visible without treating them as replayed work or an edge result.
    The projection accepts only aggregate counts, the primary horizon summary,
    and content digests; raw observations and held-out fields never enter the
    report.
    """
    for event in reversed(events):
        payload = event.get("payload") or {}
        section = payload.get("signal_quality_screen") \
            if isinstance(payload, Mapping) else None
        # The factory event writer stores the compact section as the payload
        # itself.  Accept the nested form as a compatibility path for callers
        # that wrap event results before persistence, but never inspect raw
        # worker rows here.
        if (section is None and isinstance(payload, Mapping) and
                payload.get("schema") == "signal-quality-screen.v1"):
            section = payload
        if not isinstance(section, Mapping):
            continue
        variants = section.get("variants")
        if not isinstance(variants, Mapping):
            variants = {}
        compact: list[dict[str, Any]] = []
        primary_rows: list[dict[str, Any]] = []
        for raw_id, raw in sorted(variants.items(), key=lambda item: str(item[0])):
            if not isinstance(raw, Mapping):
                continue
            item: dict[str, Any] = {
                "variant_id": str(raw_id),
                "status": str(raw.get("status") or "unknown"),
                "reason": str(raw.get("reason") or ""),
                "digest": (str(raw.get("digest"))
                           if raw.get("digest") else None),
            }
            primary = raw.get("primary_horizon")
            if isinstance(primary, Mapping):
                try:
                    horizon = int(primary.get("horizon_minutes"))
                    candidate_count = int(primary.get("candidate_count"))
                    matched_count = int(primary.get("matched_count"))
                    coverage = float(primary.get("matched_coverage"))
                    delta = float(primary.get("candidate_minus_control_bps"))
                except (TypeError, ValueError, OverflowError):
                    primary = None
                else:
                    if (horizon <= 0 or candidate_count < 0 or matched_count < 0 or
                            matched_count > candidate_count or
                            not math.isfinite(coverage) or
                            not math.isfinite(delta)):
                        primary = None
            if isinstance(primary, Mapping):
                item["primary_horizon"] = {
                    "horizon_minutes": horizon,
                    "candidate_count": candidate_count,
                    "matched_count": matched_count,
                    "matched_coverage": coverage,
                    "candidate_minus_control_bps": delta,
                }
                primary_rows.append(item["primary_horizon"])
            compact.append(item)
        skipped_statuses = {"complete_zero_actionable_signal",
                            "complete_nonpositive_control"}
        skipped = sum(item["status"] in skipped_statuses for item in compact)
        aggregate_primary: dict[str, Any] | None = None
        if primary_rows:
            horizons = sorted({item["horizon_minutes"] for item in primary_rows})
            candidate_count = sum(item["candidate_count"] for item in primary_rows)
            matched_count = sum(item["matched_count"] for item in primary_rows)
            weighted_delta = sum(
                item["candidate_minus_control_bps"] * item["candidate_count"]
                for item in primary_rows)
            aggregate_primary = {
                "horizon_minutes": horizons[0] if len(horizons) == 1 else None,
                "horizons": horizons,
                "candidate_count": candidate_count,
                "matched_count": matched_count,
                "matched_coverage": (matched_count / candidate_count
                                      if candidate_count else None),
                "candidate_minus_control_bps": (
                    weighted_delta / candidate_count if candidate_count else None),
                "variant_count": len(primary_rows),
            }
        return {
            "schema": str(section.get("schema") or "signal-quality-screen.v1"),
            "scope": str(section.get("scope") or "fit_only"),
            "diagnostic_only": section.get("diagnostic_only") is True,
            "authorizing": section.get("authorizing") is True,
            "status": str(section.get("status") or "unknown"),
            "reason": str(section.get("reason") or ""),
            "variant_count": len(compact),
            "skipped_count": skipped,
            "digest": (str(section.get("digest"))
                       if section.get("digest") else None),
            "primary_horizon": aggregate_primary,
            "variants": compact,
        }
    return {}


def build_report(db_path: str | Path = DEFAULT_DB_PATH, *,
                 vehicle: str | None = None, slot: int | None = None) -> dict:
    """Assemble the full discovery narrative from the two ledgers."""
    path = Path(db_path)
    if vehicle is not None and vehicle not in VEHICLES:
        raise ValueError("vehicle must be equity or option")
    if not path.is_file():
        return {"schema": REPORT_SCHEMA, "available": False,
                "reason": "ledger not created", "db_path": str(path),
                "vehicles": []}
    with closing(_connect(path)) as db:
        tables = {str(row[0]) for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "factory_hypotheses" not in tables:
            return {"schema": REPORT_SCHEMA, "available": False,
                    "reason": "no factory lineage recorded", "db_path": str(path),
                    "vehicles": []}
        frozen_policies: dict[str, list[dict]] = {name: [] for name in VEHICLES}
        if "factory_dependence_policies" in tables:
            for row in db.execute(
                    "SELECT * FROM factory_dependence_policies "
                    "ORDER BY created_at,policy_id"):
                policy = _dependence_policy_row(dict(row))
                frozen_policies.setdefault(str(row["vehicle"]), []).append(policy)
        hypotheses = [dict(row) for row in db.execute(
            """SELECT h.*, (SELECT status FROM factory_events e
                            WHERE e.hypothesis_id=h.hypothesis_id
                            ORDER BY e.created_at DESC, e.event_id DESC LIMIT 1)
               AS status FROM factory_hypotheses h
               ORDER BY h.vehicle, h.slot, h.generation""")]
        events: dict[str, list[dict]] = {}
        for row in db.execute(
                "SELECT * FROM factory_events ORDER BY created_at, event_id"):
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json")) or {}
            events.setdefault(str(item["hypothesis_id"]), []).append(item)
        accounts: dict[str, list[dict]] = {}
        closures: dict[str, list[dict]] = {}
        if "factory_variant_closures" in tables:
            for row in db.execute(
                    "SELECT * FROM factory_variant_closures ORDER BY created_at,closure_id"):
                item = dict(row)
                item["evidence"] = _loads(item.pop("evidence_json")) or {}
                closures.setdefault(str(item["hypothesis_id"]), []).append(item)
        # Session-level candidate statistics are reduced to family vectors for
        # the cross-family dependence diagnostic.  Raw rows never leave this
        # reader; the resulting report contains only aggregates/correlations.
        family_vectors: dict[str, list[tuple[str, str, float]]] = {}
        for row in db.execute(
                """SELECT hypothesis_id, result_json, created_at
                   FROM factory_accounts ORDER BY created_at, account_id"""):
            record = _loads(row["result_json"])
            if isinstance(record, Mapping):
                hypothesis_id = str(row["hypothesis_id"])
                accounts.setdefault(hypothesis_id, []).append(_variant_row(record))
                spec = record.get("rule_spec")
                family = (spec.get("family") if isinstance(spec, Mapping)
                          else record.get("family"))
                gate = record.get("gate")
                verified = (gate.get("verified_gate")
                            if isinstance(gate, Mapping) else None)
                source_rows = (verified.get("heldout_source")
                               if isinstance(verified, Mapping) else None)
                if not isinstance(source_rows, Sequence):
                    source_rows = (gate.get("heldout_source")
                                   if isinstance(gate, Mapping) else None)
                if family and isinstance(source_rows, Sequence):
                    vehicle_name = str(record.get("vehicle") or "equity")
                    bucket = family_vectors.setdefault(vehicle_name, [])
                    for source_row in source_rows:
                        if not isinstance(source_row, Mapping):
                            continue
                        session = source_row.get("session_date")
                        value = source_row.get("net_pnl")
                        if value is None:
                            value = source_row.get("delta")
                        number = _number(value)
                        if session is not None and number is not None:
                            bucket.append((str(family), str(session), number))
        deployed: dict[tuple[str, str], str] = {}
        if {"candidates", "candidate_state"}.issubset(tables):
            for row in db.execute(
                    """SELECT c.variant_id, c.vehicle, s.status
                       FROM candidates c JOIN candidate_state s
                         ON s.candidate_id=c.candidate_id"""):
                deployed[(str(row["variant_id"]), str(row["vehicle"]))] = str(
                    row["status"])
        cycles = int(db.execute(
            "SELECT COUNT(*) FROM factory_cycles").fetchone()[0]) \
            if "factory_cycles" in tables else 0
        # The graded reason history: what was tried, why, and what the gates
        # then said.  Read here so the narrative can show the learning loop
        # rather than only the current state.
        lessons: dict[str, list[dict]] = {}
        if {"factory_lessons", "factory_lesson_outcomes"}.issubset(tables):
            parent_columns = {str(item["name"]) for item in
                              db.execute("PRAGMA table_info(factory_lessons)")}
            outcome_columns = {str(item["name"]) for item in
                               db.execute(
                                   "PRAGMA table_info(factory_lesson_outcomes)")}
            parent = ("l.parent_lesson_id" if "parent_lesson_id" in parent_columns
                      else "NULL AS parent_lesson_id")
            classification = ("o.classification" if
                              "classification" in outcome_columns else
                              "CASE WHEN o.passed=1 THEN 'proved' "
                              "WHEN o.underpowered=1 THEN 'underpowered' "
                              "ELSE 'legacy_unclassified' END AS classification")
            fit_delta = ("o.fit_delta" if "fit_delta" in outcome_columns
                         else "NULL AS fit_delta")
            reasons: dict[str, str] = {}
            for row in db.execute(
                    f"""SELECT l.lesson_id, {parent}, l.vehicle, l.family,
                              l.kind, l.source, l.reason, l.evidence_json,
                              l.changed_json, l.variant_id, l.created_at,
                              o.passed, o.underpowered, {classification},
                              {fit_delta},
                              o.heldout_delta,
                              o.q_value, o.failed_checks_json, o.outcome_id
                       FROM factory_lessons l
                       LEFT JOIN factory_lesson_outcomes o
                         ON o.lesson_id=l.lesson_id
                       ORDER BY l.created_at DESC, l.lesson_id DESC"""):
                graded = row["outcome_id"] is not None
                reasons[str(row["lesson_id"])] = str(row["reason"])
                lessons.setdefault(str(row["vehicle"]), []).append({
                    "lesson_id": row["lesson_id"],
                    # The lesson this proposal reasoned from.  An unbroken
                    # chain is the difference between a search that learns and
                    # one that restarts every cycle.
                    "parent_lesson_id": row["parent_lesson_id"],
                    "family": row["family"], "kind": row["kind"],
                    "proposed_by": row["source"], "reason": row["reason"],
                    "variant_id": row["variant_id"],
                    "changed": _loads(row["changed_json"]) or {},
                    "evidence": _loads(row["evidence_json"]) or {},
                    "graded": graded,
                    "verdict": None if not graded else row["classification"],
                    "fit_delta": _number(row["fit_delta"]),
                    "heldout_delta": _number(row["heldout_delta"]),
                    "q_value": _number(row["q_value"]),
                    "failed_checks": [
                        _CHECK_LABELS.get(name, name) for name in
                        (_loads(row["failed_checks_json"]) or [])],
                })
            # Resolved after the sweep so a parent recorded in the same cycle
            # is available regardless of row order.
            for rows in lessons.values():
                for item in rows:
                    item["built_on"] = reasons.get(str(item["parent_lesson_id"]))

    report_vehicles = []
    for name in VEHICLES:
        if vehicle is not None and name != vehicle:
            continue
        local = [item for item in hypotheses if item["vehicle"] == name
                 and (slot is None or int(item["slot"]) == int(slot))]
        if not local:
            continue
        slots: dict[int, list[dict]] = {}
        for item in local:
            hypothesis_id = str(item["hypothesis_id"])
            own = events.get(hypothesis_id, [])
            parent = events.get(str(item["parent_hypothesis_id"] or ""), [])
            variants = accounts.get(hypothesis_id, [])
            own_closures = closures.get(hypothesis_id, [])
            fit_audit = _fit_events(own)
            screen_audit = _screen_events(own)
            for row in variants:
                row["ledger_status"] = deployed.get(
                    (str(row["variant_id"]), name))
            slots.setdefault(int(item["slot"]), []).append({
                "hypothesis_id": hypothesis_id,
                "generation": int(item["generation"]),
                "family": item["family"],
                "status": item["status"],
                "rule_schema": (_loads(item["spec_json"]) or {}).get("schema"),
                "rule_spec": _loads(item["spec_json"]),
                # For an LLM-discovered hypothesis this is the model's own
                # one-sentence rationale; otherwise it is generated from the
                # spec. Either way it is text, never an instruction.
                "thesis": item["thesis"],
                "falsification": item["falsification"],
                "parent_hypothesis_id": item["parent_hypothesis_id"],
                "not_before": item["not_before"],
                "origin": _origin(own, parent, hypothesis_id,
                                  int(item["generation"])),
                "variants": variants,
                "fit_diagnostics": fit_audit["diagnostics"],
                # A signal-quality screen is fit-only, non-authorizing work;
                # keep it distinct from replay variants and their gates.
                "signal_quality_screen": screen_audit,
                "screened_variant_count": int(screen_audit.get(
                    "variant_count", 0)),
                "screened_out_variant_count": int(screen_audit.get(
                    "skipped_count", 0)),
                "tested_for_signal_quality": bool(screen_audit),
                "risk_summary": (
                    fit_audit["diagnostics"].get("risk")
                    if isinstance(fit_audit["diagnostics"], Mapping) else None),
                "behavior_aliases": fit_audit["behavior_aliases"],
                "excluded_behavior_aliases": fit_audit[
                    "excluded_behavior_aliases"],
                "proposed_behavior_aliases": fit_audit[
                    "proposed_behavior_aliases"],
                "tuning": _tuning_events(own),
                "variants_tested": len(variants),
                "variant_closures": own_closures,
                "search_state": next((dict((event.get("payload") or {}).get("search_state"))
                                       for event in reversed(own)
                                       if isinstance((event.get("payload") or {}).get("search_state"), Mapping)),
                                      None),
                "outcome": _outcome(own, str(item["status"] or "")),
            })
        active = sum(1 for item in local if item["status"] in {
            "queued", "testing", "backtest_passed", "pending_generation_limit",
            "pending_llm_replacement"})
        proved = [row for rows in slots.values() for item in rows
                  for row in item["variants"]
                  if row["ledger_status"] in {"validated", "champion"}]
        local_lessons = [item for item in lessons.get(name, [])
                         if slot is None or any(
                             item["variant_id"] == row["variant_id"]
                             for rows in slots.values() for entry in rows
                             for row in entry["variants"])]
        graded = [item for item in local_lessons if item["graded"]]
        tested_families = {str(item["family"])
                           for rows in slots.values() for item in rows
                           if item["variants"] or item.get("signal_quality_screen")}
        cross_family = cross_family_dependence_report(family_vectors.get(name, ()))
        vehicle_search_state = {
            str(slot_id): entry["search_state"]
            for slot_id, entries in slots.items()
            for entry in entries
            if entry.get("search_state") is not None
        }
        report_vehicles.append({
            "vehicle": name,
            "dependence_policies": frozen_policies.get(name, []),
            "dependence_policy": (frozen_policies.get(name) or [])[-1]
            if frozen_policies.get(name) else None,
            "cross_family_dependence": cross_family,
            "search_state": vehicle_search_state,
            "search_exhausted": bool(vehicle_search_state) and all(
                str(state.get("state")) == "bounded_space_exhausted"
                for state in vehicle_search_state.values()),
            "summary": {
                "slots": len(slots),
                "active_slots": active,
                "hypotheses": len(local),
                "variants_tested": sum(len(item["variants"])
                                       for rows in slots.values()
                                       for item in rows),
                "screened_variants": sum(int(item.get(
                    "screened_variant_count", 0))
                    for rows in slots.values() for item in rows),
                "screened_out_variants": sum(int(item.get(
                    "screened_out_variant_count", 0))
                    for rows in slots.values() for item in rows),
                "families_explored": sorted(tested_families),
                "families_untested": [family for family in RULE_FAMILIES
                                      if family not in tested_families],
                "proved_variants": [row["variant_id"] for row in proved],
                "classifications": {
                    label: sum(1 for rows in slots.values() for item in rows
                               for row in item["variants"]
                               if row.get("classification") == label)
                    for label in (
                        "proved", "adequate_negative_rejection",
                        "adequate_negative_inconclusive",
                        "adequate_inconclusive", "budget_exhausted", "underpowered",
                        "execution_blocked", "qualification_unavailable")
                },
                "retired_hypotheses": sum(
                    1 for rows in slots.values() for item in rows
                    if item["outcome"]["kind"] in {"retired", "rotated", "recentered"}),
                # Authored by the model, not merely asked of it.  A rejected
                # proposal still records provider evidence, so counting the
                # presence of that evidence reported a deterministic template
                # as an LLM-seeded hypothesis.
                "llm_seeded_hypotheses": sum(
                    1 for rows in slots.values() for item in rows
                    if item["origin"]["kind"] in {
                        "llm_discovery", "llm_replacement"} or
                    (item["origin"]["kind"] == "reseed_after_proof" and
                     str(item["origin"].get("source") or "").startswith("llm"))),
                "llm_proposals_rejected": sum(
                    1 for rows in slots.values() for item in rows
                    if item["origin"].get("llm_error")),
                "reasons_recorded": len(local_lessons),
                "reasons_graded": len(graded),
                "llm_reasons_that_passed": sum(
                    1 for item in graded
                    if item["proposed_by"] == "llm" and item["verdict"] == "passed"),
                "llm_tuned_variants": sum(
                    1 for item in local_lessons
                    if item["proposed_by"] == "llm" and item["kind"] == "tuning"),
                "llm_reseeds": sum(
                    1 for rows in slots.values() for item in rows
                    if item["origin"]["kind"] == "reseed_after_proof" and
                    str(item["origin"].get("source") or "").startswith("llm")),
                # Proposals that named the earlier result they reasoned from,
                # rather than starting from nothing.
                "reasons_built_on_a_prior_lesson": sum(
                    1 for item in local_lessons if item.get("parent_lesson_id")),
                "cross_family_dependence": cross_family,
                "dependence_policy_hash": ((frozen_policies.get(name) or [])[-1].get(
                    "policy_hash") if frozen_policies.get(name) else None),
            },
            "lessons": local_lessons,
            "slots": [{"slot": key, "generations": slots[key]}
                      for key in sorted(slots)],
        })
    return {"schema": REPORT_SCHEMA, "available": True, "db_path": str(path),
            "cycles": cycles, "vehicles": report_vehicles}


# How many graded reasons the rendered narrative shows before it becomes a log
# rather than a report.  ``build_report`` returns all of them either way.
_LESSON_ROWS = 25


def _changed_phrase(changed: Mapping[str, Any]) -> str:
    """Render a recorded parameter delta the way the ledger stored it."""
    parts = []
    for key, value in sorted(changed.items()):
        if isinstance(value, Mapping) and {"from", "to"} <= set(value):
            parts.append(f"{key} {value['from']}→{value['to']}")
        else:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def _fmt(value: Any, digits: int = 4) -> str:
    number = _number(value)
    if number is None:
        return "—" if value is None else str(value)
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".") or "0"


def _variant_classification(variant: Mapping[str, Any]) -> str:
    """Return the persisted classification, with a legacy compatibility fallback."""
    classification = variant.get("classification")
    if classification not in (None, ""):
        return str(classification)
    if variant.get("passes"):
        return "proved"
    if variant.get("underpowered"):
        return "underpowered"
    return "fail"


def render_text(report: Mapping[str, Any]) -> str:
    """Render the report as a plain-text narrative."""
    if not report.get("available"):
        return (f"No research lineage yet ({report.get('reason')}).\n"
                f"Ledger: {report.get('db_path')}\n")
    out: list[str] = []
    add = out.append
    add(f"Autonomous research report  ({report['db_path']})")
    add(f"cycles run: {report.get('cycles', 0)}")
    for vehicle in report["vehicles"]:
        summary = vehicle["summary"]
        add("")
        add("=" * 78)
        add(f"VEHICLE: {vehicle['vehicle']}")
        add("=" * 78)
        add(f"  slots {summary['slots']} ({summary['active_slots']} still searching)"
            f" | hypotheses {summary['hypotheses']}"
            f" | variants tested {summary['variants_tested']}")
        add(f"  families explored: {', '.join(summary['families_explored'])}")
        add("  families not yet tested: " +
            (", ".join(summary.get("families_untested", ())) or "none"))
        dependence = vehicle.get("cross_family_dependence") or {}
        if dependence.get("available"):
            diag = dependence.get("dependence") or {}
            add("  cross-family dependence: "
                f"{dependence.get('complete_session_count', 0)} shared sessions, "
                f"mean |r| {_fmt(diag.get('mean_absolute_correlation'))}, "
                f"strong pairs {len(diag.get('strong_pairs') or ())}")
        else:
            add("  cross-family dependence: unavailable "
                f"({dependence.get('reason', 'not enough complete sessions')})")
        policy = vehicle.get("dependence_policy") or {}
        if policy:
            evidence = policy.get("evidence") or {}
            add("  frozen dependence policy: "
                f"{'verified' if policy.get('verified_persisted') else 'unverified'}, "
                f"hash {policy.get('policy_hash') or '—'}, "
                f"source cycles {len(policy.get('source_cycles') or ())}, "
                f"clusters {len(set((policy.get('cluster_map') or {}).values()))}, "
                f"floor {evidence.get('minimum_complete_sessions', '—')} sessions / "
                f"{evidence.get('minimum_prior_cycles', '—')} cycles")
        else:
            add("  frozen dependence policy: unavailable (no persisted policy)")
        add("  verdict classes: " +
            json.dumps(summary.get("classifications", {}), sort_keys=True))
        add(f"  hypotheses proposed by the LLM: {summary['llm_seeded_hypotheses']}"
            f" ({summary['llm_proposals_rejected']} proposal(s) refused)")
        add(f"  hypotheses retired/rotated: {summary['retired_hypotheses']}")
        add(f"  search exhausted: {vehicle.get('search_exhausted', False)}")
        if vehicle.get("search_state"):
            for slot_id, state in sorted(vehicle["search_state"].items()):
                eligible_attempts = state.get(
                    "eligible_confirmatory_attempts",
                    # Compatibility with reports written before the explicit
                    # eligible/total accounting names were introduced.
                    state.get("confirmatory_attempts", 0))
                account_attempts = state.get("account_attempts_total")
                account_suffix = (f"; account attempts total {account_attempts}"
                                  if account_attempts is not None else "")
                add(f"  slot {slot_id} search state: {state.get('state')}"
                    f" (coordinate {state.get('coordinate_remaining')}/"
                    f"{state.get('coordinate_total')}, interaction "
                    f"{state.get('interaction_remaining')}/"
                    f"{state.get('interaction_total')}, closed "
                    f"{state.get('closed_count')}, eligible confirmatory "
                    f"attempts {eligible_attempts}/"
                    f"{state.get('confirmatory_budget')}"
                    f"{account_suffix})")
        add(f"  variants the model tuned: {summary['llm_tuned_variants']}"
            f" | reasons recorded {summary['reasons_recorded']}"
            f" ({summary['reasons_graded']} graded,"
            f" {summary['reasons_built_on_a_prior_lesson']} built on an"
            f" earlier lesson)")
        add(f"  LLM reseeds after proof: {summary['llm_reseeds']}")
        proved = summary["proved_variants"]
        add(f"  PROVED EDGES: {', '.join(proved) if proved else 'none yet'}")
        if vehicle.get("lessons"):
            add("")
            add("  WHAT IT TRIED, WHY, AND WHAT HAPPENED")
            for item in vehicle["lessons"][:_LESSON_ROWS]:
                verdict = item["verdict"] or "not yet graded"
                add(f"    [{verdict}] {item['kind']} by {item['proposed_by']}"
                    f" ({item['family']})")
                add(f"      reason: {item['reason']}")
                if item.get("built_on"):
                    add(f"      built on: {item['built_on']}")
                if item["changed"]:
                    add(f"      changed: {_changed_phrase(item['changed'])}")
                if item["verdict"] is not None:
                    add(f"      held-out delta {_fmt(item['heldout_delta'])}"
                        f"  q {_fmt(item['q_value'])}")
                if item["failed_checks"]:
                    add("      failed: " + "; ".join(item["failed_checks"][:4]))
        for entry in vehicle["slots"]:
            add("")
            add(f"  {'-' * 74}")
            add(f"  SLOT {entry['slot']}")
            for item in entry["generations"]:
                add("")
                origin = item["origin"]
                label = origin["kind"].replace("_", " ")
                add(f"    gen {item['generation']}  {item['family']}"
                    f"  [{item['status']}]  via {label}")
                add(f"      id      {item['hypothesis_id']}")
                add(f"      grammar {item['rule_schema']}")
                add(f"      thesis  {item['thesis']}")
                add(f"      refuted if: {item['falsification']}")
                llm = origin.get("llm")
                if llm:
                    add(f"      proposed by {llm.get('provider')}/{llm.get('model')}"
                        f" in {llm.get('attempts')} attempt(s)")
                    add(f"        prompt {str(llm.get('prompt_hash'))[:16]}…"
                        f"  request {str(llm.get('request_hash'))[:16]}…"
                        f"  response {str(llm.get('response_hash'))[:16]}…")
                for tuning in item.get("tuning", ()):
                    evidence = tuning.get("evidence") or {}
                    add(f"      tuning {'accepted' if tuning.get('success') else 'rejected'}"
                        f" ({tuning.get('tuned_variants', 0)} variant(s))"
                        f" schema {str(evidence.get('response_schema_hash'))[:16]}…"
                        f" config {str(evidence.get('config_hash'))[:16]}…")
                    add(f"        request {str(evidence.get('request_hash'))[:16]}…"
                        f" response {str(evidence.get('raw_response_hash'))[:16]}…")
                    for error in (evidence.get('attempt_errors') or ())[:3]:
                        add(f"        tuning attempt: {str(error)[:240]}")
                if origin.get("llm_error"):
                    add(f"      LLM proposal rejected: {origin['llm_error']}")
                if item["variants"]:
                    add(f"      variants tested: {item['variants_tested']}")
                screen = item.get("signal_quality_screen") or {}
                if screen:
                    primary = screen.get("primary_horizon") or {}
                    primary_text = ""
                    if primary:
                        primary_text = (
                            f"; primary horizon {primary.get('horizon_minutes') or
                            primary.get('horizons')}m n={primary.get('candidate_count')}"
                            f" matched={primary.get('matched_count')}"
                            f" coverage {_fmt(primary.get('matched_coverage'))}"
                            f" vs-null {_fmt(primary.get('candidate_minus_control_bps'), 2)}bps")
                    add(
                        "      fit signal-quality screen only: "
                        f"{screen.get('status')} ({screen.get('reason')}); "
                        f"variants {screen.get('variant_count', 0)}, "
                        f"skipped full replay {screen.get('skipped_count', 0)}"
                        f"{primary_text}; digest "
                        f"{str(screen.get('digest') or '')[:16]}…")
                fit_diagnostics = item.get("fit_diagnostics") or {}
                if fit_diagnostics:
                    first = fit_diagnostics.get("first_signal") or {}
                    aliases = item.get("behavior_aliases") or {}
                    excluded = item.get("excluded_behavior_aliases") or []
                    proposed = item.get("proposed_behavior_aliases") or []
                    add(f"      fit first-signal rate {_fmt(first.get('rate'))}"
                        f" | eligible-prefix rate {_fmt((fit_diagnostics.get('eligible_prefix') or {}).get('rate'))}")
                    add(f"      fit behavior aliases {len(aliases.get('full_aliases') or ())}"
                        f" | proposed canonicalization {len(proposed)}"
                        f" | excluded before replay {len(excluded)}")
                    risk_metrics = _fit_risk_metrics(fit_diagnostics)
                    if risk_metrics:
                        line = (
                            "      fit risk configured pre-cap budget median USD "
                            f"{_fmt(risk_metrics['configured'], 2)}")
                        if risk_metrics["planned"] is not None:
                            line += (" | planned median USD "
                                     f"{_fmt(risk_metrics['planned'], 2)}")
                        line += (" | capped delivered median USD "
                                 f"{_fmt(risk_metrics['delivered'], 2)}"
                                 f" | {risk_metrics['ratio_label']} "
                                 f"{_fmt(risk_metrics['ratio'])}")
                        add(line)
                    signal_quality = _fit_signal_quality_metrics(fit_diagnostics)
                    if signal_quality:
                        rendered = ", ".join(
                            f"{item['horizon']} n={item['count']} mean "
                            f"{_fmt(item['mean_bps'], 2)}bps vs-null "
                            f"{_fmt(item['control_delta_bps'], 2)}bps "
                            f"(t {_fmt(item['control_delta_t'], 2)}) after-cost "
                            f"{_fmt(item['after_cost_bps'], 2)}bps "
                            f"(t {_fmt(item['after_cost_t'], 2)})"
                            for item in signal_quality)
                        add(f"      fit conditional forward returns {rendered}")
                    path_metrics = _fit_path_telemetry_metrics(fit_diagnostics)
                    if path_metrics:
                        rendered = "; ".join(
                            f"{item['target_r']}R/{item['max_hold_bars']}b/{item['exit_reason']} "
                            f"n={item['count']} MFE {_fmt(item['mfe_bps'], 2)}bps "
                            f"MAE {_fmt(item['mae_bps'], 2)}bps "
                            f"({_fmt(item['mfe_r'], 2)}R/{_fmt(item['mae_r'], 2)}R), "
                            f"censored {item['censored']} gaps {item['gapped']}"
                            for item in path_metrics)
                        add(f"      fit path excursions {rendered}")
                    reachability = _fit_target_hold_reachability(fit_diagnostics)
                    if reachability:
                        recommendation = reachability.get("recommendation")
                        suffix = (f" -> propose {recommendation['target_r']}R/"
                                  f"{recommendation['max_hold_bars']}b"
                                  if recommendation else "")
                        add(
                            "      fit target/hold reachability "
                            f"{reachability['target_r']}R/"
                            f"{reachability['max_hold_bars']}b: "
                            f"usable {reachability['usable']}/"
                            f"{reachability['total']}, censored "
                            f"{reachability['censored']}, expiry "
                            f"{reachability['expiry_count']}, unreachable "
                            f"{reachability['unreachable_count']} "
                            f"({_fmt(reachability['unreachable_rate'])}); "
                            f"status {reachability['status']}{suffix}")
                for variant in item["variants"]:
                    verdict = _variant_classification(variant)
                    add(f"        - {variant['variant_id']}  [{verdict}]"
                        f"  lane={variant['lane']}")
                    add(f"            trades {variant['trades']}"
                        f" (fit {variant['fit_trades']} / held-out {variant['heldout_trades']})"
                        f"  net {_fmt(variant['net_pnl'], 2)}"
                        f"  maxDD {_fmt(variant['max_drawdown'], 2)}")
                    add(f"            held-out delta {_fmt(variant['heldout_delta'])}"
                        f"  lcb {_fmt(variant['heldout_delta_lcb'])}"
                        f"  q {_fmt(variant['q_value'])}"
                        f"  win {_fmt(variant['win_rate'])}")
                    if variant["reason"]:
                        add(f"            tried because ({variant['proposed_by']}):"
                            f" {variant['reason']}")
                    if variant["ledger_status"]:
                        add(f"            ledger status: {variant['ledger_status']}")
                    if variant["primary_failure"] and variant["primary_failure"] != "none":
                        add(f"            diagnosis: {variant['primary_failure']}")
                    if variant["failed_checks"]:
                        add("            failed: " +
                            "; ".join(variant["failed_checks"]))
                outcome = item["outcome"]
                add(f"      outcome: {outcome['kind'].replace('_', ' ')}"
                    f" — {outcome.get('reason') or ''}".rstrip(" —"))
                if outcome.get("variants_tested") is not None:
                    add(f"        after {outcome['variants_tested']} of"
                        f" {outcome.get('variants_intended')} intended variants"
                        f" failed adequately powered gates")
                if outcome.get("primary_failure"):
                    add(f"        dominant failure mode: {outcome['primary_failure']}")
                if outcome.get("replacement_hypothesis_id"):
                    add(f"        replaced by {outcome['replacement_hypothesis_id']}")
                if outcome.get("proved_variants"):
                    add(f"        proved: {', '.join(outcome['proved_variants'])}")
                if outcome.get("successor_hypothesis_id"):
                    add(f"        slot reseeded with {outcome['successor_hypothesis_id']}"
                        f" ({outcome.get('successor_source')})")
                if outcome.get("llm_error"):
                    add(f"        waiting on a valid LLM proposal: {outcome['llm_error']}")
    add("")
    return "\n".join(out) + "\n"


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the report as Markdown for sharing or archiving."""
    if not report.get("available"):
        return f"# Research report\n\nNo lineage yet ({report.get('reason')}).\n"
    out = [f"# Autonomous research report", "",
           f"- Ledger: `{report['db_path']}`", f"- Cycles run: {report.get('cycles', 0)}"]
    for vehicle in report["vehicles"]:
        summary = vehicle["summary"]
        out += ["", f"## {vehicle['vehicle']}", "",
                f"- Slots: {summary['slots']} ({summary['active_slots']} still searching)",
                f"- Hypotheses: {summary['hypotheses']}, variants tested: {summary['variants_tested']}",
                f"- Families explored: {', '.join(summary['families_explored'])}",
                f"- Families not yet tested: "
                f"{', '.join(summary.get('families_untested', ())) or 'none'}",
                f"- Cross-family dependence: "
                f"{('available' if (vehicle.get('cross_family_dependence') or {}).get('available') else 'unavailable')}",
                f"- Frozen dependence policy: "
                f"{('verified' if (vehicle.get('dependence_policy') or {}).get('verified_persisted') else 'unavailable')}",
                f"- Verdict classes: {json.dumps(summary.get('classifications', {}), sort_keys=True)}",
                f"- Proposed by the LLM: {summary['llm_seeded_hypotheses']}"
                f" ({summary['llm_proposals_rejected']} refused)",
                f"- Retired or rotated: {summary['retired_hypotheses']}",
                f"- Search exhausted: {vehicle.get('search_exhausted', False)}",
                f"- Variants the model tuned: {summary['llm_tuned_variants']}",
                f"- LLM reseeds after proof: {summary['llm_reseeds']}",
                f"- Reasons recorded: {summary['reasons_recorded']}"
                f" ({summary['reasons_graded']} graded)",
                f"- Proved edges: {', '.join(summary['proved_variants']) or 'none yet'}"]
        if vehicle.get("search_state"):
            for slot_id, state in sorted(vehicle["search_state"].items()):
                eligible_attempts = state.get(
                    "eligible_confirmatory_attempts",
                    state.get("confirmatory_attempts", 0))
                account_attempts = state.get("account_attempts_total")
                account_suffix = (f"; account attempts total {account_attempts}"
                                  if account_attempts is not None else "")
                out.append(
                    f"- Slot {slot_id} search state: {state.get('state')}; "
                    f"eligible confirmatory attempts {eligible_attempts}/"
                    f"{state.get('confirmatory_budget')}"
                    f"{account_suffix}")
        if vehicle.get("lessons"):
            out += ["", "#### What it tried, why, and what happened", "",
                    "| verdict | kind | by | reason | built on | changed |"
                    " held-out Δ |",
                    "| --- | --- | --- | --- | --- | --- | --- |"]
            for item in vehicle["lessons"][:_LESSON_ROWS]:
                out.append(
                    f"| {item['verdict'] or 'ungraded'} | {item['kind']} |"
                    f" {item['proposed_by']} | {item['reason']} |"
                    f" {item.get('built_on') or '—'} |"
                    f" {_changed_phrase(item['changed'])} |"
                    f" {_fmt(item['heldout_delta'])} |")
        for entry in vehicle["slots"]:
            out += ["", f"### Slot {entry['slot']}"]
            for item in entry["generations"]:
                out += ["", f"#### gen {item['generation']} — {item['family']} "
                            f"(`{item['status']}`, via {item['origin']['kind']})", "",
                        f"> {item['thesis']}", ""]
                if item["variants"]:
                    out += ["| variant | lane | trades | held-out Δ | lcb | q | verdict |",
                            "| --- | --- | --- | --- | --- | --- | --- |"]
                    for variant in item["variants"]:
                        classification = _variant_classification(variant)
                        verdict = ("**pass**" if classification == "proved"
                                   else classification)
                        out.append(
                            f"| `{variant['variant_id']}` | {variant['lane']} |"
                            f" {variant['trades']} | {_fmt(variant['heldout_delta'])} |"
                            f" {_fmt(variant['heldout_delta_lcb'])} |"
                            f" {_fmt(variant['q_value'])} | {verdict} |")
                screen = item.get("signal_quality_screen") or {}
                if screen:
                    primary = screen.get("primary_horizon") or {}
                    primary_text = ""
                    if primary:
                        primary_text = (
                            f"; primary horizon {primary.get('horizon_minutes') or
                            primary.get('horizons')}m, n={primary.get('candidate_count')}, "
                            f"matched={primary.get('matched_count')}, coverage "
                            f"{_fmt(primary.get('matched_coverage'))}, vs-null "
                            f"{_fmt(primary.get('candidate_minus_control_bps'), 2)} bps")
                    out.append(
                        "- Fit signal-quality screen only: "
                        f"{screen.get('status')} ({screen.get('reason')}); "
                        f"variants {screen.get('variant_count', 0)}, skipped full replay "
                        f"{screen.get('skipped_count', 0)}{primary_text}; digest "
                        f"`{str(screen.get('digest') or '')[:16]}…`")
                fit_diagnostics = item.get("fit_diagnostics") or {}
                if fit_diagnostics:
                    first = fit_diagnostics.get("first_signal") or {}
                    aliases = item.get("behavior_aliases") or {}
                    excluded = item.get("excluded_behavior_aliases") or []
                    proposed = item.get("proposed_behavior_aliases") or []
                    out.append(
                        f"- Fit first-signal rate: {_fmt(first.get('rate'))}; "
                        f"eligible-prefix rate: {_fmt((fit_diagnostics.get('eligible_prefix') or {}).get('rate'))}; "
                        f"full aliases: {len(aliases.get('full_aliases') or ())}; "
                        f"proposed canonicalization: {len(proposed)}; "
                        f"excluded before replay: {len(excluded)}")
                    risk_metrics = _fit_risk_metrics(fit_diagnostics)
                    if risk_metrics:
                        line = (
                            "- Fit risk configured pre-cap budget median USD: "
                            f"{_fmt(risk_metrics['configured'], 2)}")
                        if risk_metrics["planned"] is not None:
                            line += ("; planned median USD: "
                                     f"{_fmt(risk_metrics['planned'], 2)}")
                        line += ("; capped delivered median USD: "
                                 f"{_fmt(risk_metrics['delivered'], 2)}; "
                                 f"{risk_metrics['ratio_label']}: "
                                 f"{_fmt(risk_metrics['ratio'])}")
                        out.append(line)
                    signal_quality = _fit_signal_quality_metrics(fit_diagnostics)
                    if signal_quality:
                        rendered = "; ".join(
                            f"{item['horizon']} n={item['count']}, mean "
                            f"{_fmt(item['mean_bps'], 2)} bps, vs-null "
                            f"{_fmt(item['control_delta_bps'], 2)} bps "
                            f"(t {_fmt(item['control_delta_t'], 2)}), after-cost "
                            f"{_fmt(item['after_cost_bps'], 2)} bps "
                            f"(t {_fmt(item['after_cost_t'], 2)})"
                            for item in signal_quality)
                        out.append(f"- Fit conditional forward returns: {rendered}")
                    path_metrics = _fit_path_telemetry_metrics(fit_diagnostics)
                    if path_metrics:
                        rendered = "; ".join(
                            f"{item['target_r']}R/{item['max_hold_bars']}b/{item['exit_reason']} "
                            f"n={item['count']}, MFE {_fmt(item['mfe_bps'], 2)} bps, "
                            f"MAE {_fmt(item['mae_bps'], 2)} bps "
                            f"({_fmt(item['mfe_r'], 2)}R/{_fmt(item['mae_r'], 2)}R), "
                            f"censored {item['censored']}, gaps {item['gapped']}"
                            for item in path_metrics)
                        out.append(f"- Fit path excursions: {rendered}")
                    reachability = _fit_target_hold_reachability(fit_diagnostics)
                    if reachability:
                        recommendation = reachability.get("recommendation")
                        suffix = (f"; propose {recommendation['target_r']}R/"
                                  f"{recommendation['max_hold_bars']}b"
                                  if recommendation else "")
                        out.append(
                            "- Fit target/hold reachability: "
                            f"{reachability['target_r']}R/"
                            f"{reachability['max_hold_bars']}b; usable "
                            f"{reachability['usable']}/{reachability['total']}; "
                            f"censored {reachability['censored']}; expiry "
                            f"{reachability['expiry_count']}; unreachable "
                            f"{reachability['unreachable_count']} "
                            f"({_fmt(reachability['unreachable_rate'])}); "
                            f"status {reachability['status']}{suffix}")
                outcome = item["outcome"]
                out += ["", f"**Outcome:** {outcome['kind'].replace('_', ' ')}"
                            f" — {outcome.get('reason') or ''}"]
                for tuning in item.get("tuning", ()):
                    evidence = tuning.get("evidence") or {}
                    out.append(
                        f"- Tuning {'accepted' if tuning.get('success') else 'rejected'}"
                        f" ({tuning.get('tuned_variants', 0)} variants); "
                        f"schema `{str(evidence.get('response_schema_hash'))[:16]}…`, "
                        f"config `{str(evidence.get('config_hash'))[:16]}…`, "
                        f"request `{str(evidence.get('request_hash'))[:16]}…`, "
                        f"response `{str(evidence.get('raw_response_hash'))[:16]}…`")
                    for error in (evidence.get('attempt_errors') or ())[:3]:
                        out.append(f"  - Tuning attempt: {str(error)[:240]}")
                if outcome.get("variants_tested") is not None:
                    out.append(f" (after {outcome['variants_tested']} variants)")
    return "\n".join(out) + "\n"


DEFAULT_REPORT_ROOT = Path("research/results/factory")


def write_report(db_path: str | Path = DEFAULT_DB_PATH, *,
                 vehicle: str | None = None,
                 output_root: str | Path = DEFAULT_REPORT_ROOT) -> Path | None:
    """Archive the narrative as Markdown, and return where it landed.

    The report was previously reachable only by running a command by hand.  On
    the documented headless topology nobody ever does, so the discovery
    narrative — the one artifact that explains what research has been doing —
    was written and never read.  Writing it under ``research/results`` puts it
    exactly where the read-only dashboard already lists Markdown reports.

    The path is stable per vehicle so a cycle overwrites its own predecessor
    rather than accumulating one file per night; the ledger, not this file, is
    the durable record.
    """
    report = build_report(db_path, vehicle=vehicle)
    if not report.get("available"):
        return None
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{vehicle or 'all'}.md"
    # Write-then-rename so the dashboard never serves a half-written report.
    staging = target.with_suffix(".md.tmp")
    staging.write_text(render_markdown(report), encoding="utf-8")
    staging.replace(target)
    return target


__all__ = ["DEFAULT_REPORT_ROOT", "REPORT_SCHEMA", "build_report",
           "render_markdown", "render_text", "write_report"]
