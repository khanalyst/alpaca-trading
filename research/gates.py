"""Policy-neutral acceptance checks for deterministic research results.

The checks operate on already normalized, vehicle-local rows.  They do not
know how a signal was generated and never combine equity and option returns.

Every statistic persisted by :func:`verified_gate_envelope` is recomputable
from the evidence the envelope itself carries: matched deltas, their cluster
labels, the draw counts, and the seeds.  Re-verification therefore repeats the
analysis instead of re-hashing a recorded conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from statistics import mean, median
import math
from typing import Any, Iterable, Mapping, Sequence

from .costs import (
    CostError, CostModel, QUOTE as QUOTE_FILL,
    RESTING_BRACKET, validate_resting_bracket_fill,
    STRESSED_COST_BASIS, STRESSED_COST_SCHEMA,
    risk_unit_report as _risk_unit_report,
)
from .stats import (
    DEFAULT_BOOTSTRAP_DRAWS, DEFAULT_NULL_DRAWS,
    benjamini_yekutieli,
    clustered_mde_power, clustered_mde_power_report, effective_breadth_report,
    moving_block_cluster_bootstrap_lower_bound,
    paired_cluster_sign_flip, sign_flip_null_statistics, stable_seed,
)

# Publicly expose the report from the gate module as well: callers constructing
# a proof need not know whether the economics calculation lives beside the
# cost model or beside the acceptance checks.  Apply the same authorizing
# projection as the statistical gates when provenance-bearing rows are passed.
def risk_unit_report(rows: Iterable[Mapping], *, vehicle: str,
                     costs: Any = None, config: Mapping | None = None,
                     min_cost_coverage: float = 1.0,
                     equity_feed: str = "iex") -> dict:
    return _risk_unit_report(
        _authorizing_rows(rows, vehicle=vehicle, equity_feed=equity_feed),
        vehicle=vehicle,
        costs=costs, config=config, min_cost_coverage=min_cost_coverage,
        equity_feed=equity_feed)


GATE_ENVELOPE_SCHEMA = "verified-research-gate.v3"
LEGACY_GATE_ENVELOPE_SCHEMA_V2 = "verified-research-gate.v2"
SUPPORTED_GATE_ENVELOPE_SCHEMAS = frozenset({
    LEGACY_GATE_ENVELOPE_SCHEMA_V2, GATE_ENVELOPE_SCHEMA,
})
FDR_BATCH_SCHEMA = "research-fdr-batch.v1"
FDR_BATCH_METHOD = "benjamini_yekutieli"
FDR_BATCH_P_VALUE_SOURCES = frozenset({"gate", "selection_window_gate"})
RETIREMENT_CONFIDENCE = .95
RETIREMENT_MIN_SESSIONS = 30
RETIREMENT_MIN_USEFUL_R = .05
GATE_REQUIRED_CHECKS = frozenset({
    "actual_control_available", "actual_control_adequate", "fit_delta_positive",
    "heldout_delta_positive", "heldout_p_significant",
    "heldout_delta_lcb_positive", "heldout_net_pnl_positive",
    "heldout_expectancy_positive", "falsification", "separated",
    "walk_forward_available", "walk_forward_adequate",
    "walk_forward_majority_positive", "null_control_available",
    "null_control_delta_positive", "fit_floor_adequate", "heldout_floor_adequate",
    "family_fdr_significant", "global_fdr_significant",
    "multiple_testing_batch_bound",
    "cumulative_fdr_significant", "qualification_available",
    "qualification_net_positive", "qualification_delta_positive",
    "max_drawdown_supported",
    "risk_unit_adequate", "fill_quality_adequate", "cost_stress_adequate",
    "qualification_floor_adequate", "qualification_confidence_supported",
    "qualification_drawdown_supported",
})
# Exact required-check contract emitted by the v2 builder.  It is retained
# only to verify quarantined historical evidence under its original semantics;
# current runs must use v3 and its complete multiple-testing batch binding.
LEGACY_GATE_REQUIRED_CHECKS_V2 = frozenset({
    "actual_control_available", "fit_delta_positive",
    "heldout_delta_positive", "heldout_p_significant",
    "heldout_delta_lcb_positive", "heldout_net_pnl_positive",
    "heldout_expectancy_positive", "falsification", "separated",
    "walk_forward_available", "walk_forward_adequate",
    "walk_forward_majority_positive", "null_control_available",
    "null_control_delta_positive", "fit_floor_adequate",
    "heldout_floor_adequate", "family_fdr_significant",
    "global_fdr_significant", "cumulative_fdr_significant",
    "qualification_available", "qualification_net_positive",
    "qualification_delta_positive", "max_drawdown_supported",
    "risk_unit_adequate", "fill_quality_adequate",
    "cost_stress_adequate", "qualification_floor_adequate",
    "qualification_confidence_supported",
    "qualification_drawdown_supported",
})
CLUSTER_SECONDS = 86_400
LOWER_BOUND_CONFIDENCE = .95
SERIAL_BLOCK_LENGTH = 5
OPTION_MAX_QUOTE_AGE_SECONDS = 30.0
# Immutable authorizing evidence floors.  The historical ``QUALIFICATION_*``
# names above remain public compatibility knobs for local statistical fixtures,
# but durable envelopes are checked against these protocol constants.  Keeping
# the protocol separate means a compact diagnostic test cannot lower an
# authorizing proof by monkeypatching a helper default or by forging the
# ``minimums`` object persisted in an envelope.
PROTOCOL_BACKTEST_MIN_TRADES = 100
PROTOCOL_BACKTEST_MIN_SESSIONS = 30
PROTOCOL_BACKTEST_MIN_CLUSTERS = 30
PROTOCOL_SHADOW_MIN_TRADES = 150
PROTOCOL_SHADOW_MIN_SESSIONS = 30
PROTOCOL_SHADOW_MIN_CLUSTERS = 30
PROTOCOL_QUALIFICATION_MIN_TRADES = 100
PROTOCOL_QUALIFICATION_MIN_SESSIONS = 30
PROTOCOL_QUALIFICATION_MIN_CLUSTERS = 30
# A null comparison cannot authorize from one or two lucky matches.  These
# floors are intentionally stricter than a mere ``available`` flag and are
# checked against both paired coverage and absolute count.
NULL_CONTROL_MIN_MATCHED = 30
NULL_CONTROL_MIN_COVERAGE = 0.80
# Actual-baseline controls use the same minimum paired evidence contract as
# randomized null controls.  Keeping this explicit (rather than deriving it
# from a caller's trade floor) prevents a 30-candidate/5-control comparison
# from looking authorizing merely because a local diagnostic requested five
# trades.
ACTUAL_CONTROL_MIN_MATCHED = 30
ACTUAL_CONTROL_MIN_COVERAGE = 0.80
QUALIFICATION_MAX_DRAWDOWN_R = 10.0
# Readable aliases for callers that want to display the protocol without
# depending on the internal naming scheme.  Enforcement uses ``PROTOCOL_*``
# directly so these compatibility names cannot weaken a durable check.
BACKTEST_MIN_TRADES = PROTOCOL_BACKTEST_MIN_TRADES
BACKTEST_MIN_SESSIONS = PROTOCOL_BACKTEST_MIN_SESSIONS
BACKTEST_MIN_CLUSTERS = PROTOCOL_BACKTEST_MIN_CLUSTERS
SHADOW_MIN_TRADES = PROTOCOL_SHADOW_MIN_TRADES
SHADOW_MIN_SESSIONS = PROTOCOL_SHADOW_MIN_SESSIONS
SHADOW_MIN_CLUSTERS = PROTOCOL_SHADOW_MIN_CLUSTERS
QUALIFICATION_MIN_TRADES = PROTOCOL_QUALIFICATION_MIN_TRADES
QUALIFICATION_MIN_SESSIONS = PROTOCOL_QUALIFICATION_MIN_SESSIONS
QUALIFICATION_MIN_CLUSTERS = PROTOCOL_QUALIFICATION_MIN_CLUSTERS
# Qualification observations are source evidence carried outside the run's
# fit/held-out rows.  Keep both storage and verification bounded so a malformed
# recorder row cannot inflate a proof envelope without limit.
QUALIFICATION_MAX_ROWS = 10_000
QUALIFICATION_MAX_BYTES = 2_000_000
COST_STRESS_SCENARIOS_BPS = (9.0, 15.0, 25.0, 50.0)
COST_STRESS_REQUIRED_BPS = 25.0
# New gate envelopes use the single preregistered held-out paired statistic
# for both the named significance and falsification checks.  The falsification
# gate still has positive-effect, null-degeneracy, scale, and independently
# seeded replication-integrity guards; this marker lets old envelopes that
# recorded a second empirical null-tail p-value remain replayable without
# giving new runs two chances at significance.
FALSIFICATION_P_VALUE_SOURCE = "heldout_paired_cluster_sign_flip"
FALSIFICATION_INDEPENDENT_P_VALUE_SOURCE = "independent_placebo_null_tail"
FALSIFICATION_INDEPENDENT_METHOD = "independent_empirical_null_tail"


def fdr_batch_evidence(*, candidate_id: str,
                       family_name: str,
                       family_candidate_key: str,
                       global_candidate_key: str,
                       family_values: Mapping[str, Mapping[str, Any]],
                       global_values: Mapping[str, Any],
                       alpha: float,
                       p_value_source: str = "gate",
                       cluster_name: str | None = None,
                       cluster_candidate_key: str | None = None,
                       cluster_values: Mapping[str, Mapping[str, Any]] | None = None,
                       policy_hash: str | None = None) -> dict:
    """Build complete, replayable BY evidence for one candidate decision.

    Scalar q-values are not sufficient evidence: changing the other p-values
    in a family can change the candidate's adjusted value.  This record keeps
    every raw p-value in each relevant scope and the deterministic BY result,
    while binding the target keys and the source statistic used by the gate.
    """
    nominal = float(alpha)
    source = str(p_value_source)
    if source not in FDR_BATCH_P_VALUE_SOURCES:
        raise ValueError("unsupported FDR batch p-value source")
    normalized_families = {
        str(name): {str(key): value for key, value in values.items()}
        for name, values in family_values.items()
    }
    normalized_global = {str(key): value for key, value in global_values.items()}
    family_results = {
        name: benjamini_yekutieli(values, alpha=nominal)
        for name, values in normalized_families.items()
    }
    body: dict[str, Any] = {
        "schema": FDR_BATCH_SCHEMA,
        "method": FDR_BATCH_METHOD,
        "alpha": nominal,
        "p_value_source": source,
        "candidate_id": str(candidate_id),
        "family_name": str(family_name),
        "family_candidate_key": str(family_candidate_key),
        "global_candidate_key": str(global_candidate_key),
        "family_values": normalized_families,
        "family_results": family_results,
        "global_values": normalized_global,
        "global_results": benjamini_yekutieli(
            normalized_global, alpha=nominal),
    }
    if cluster_values is not None:
        if (cluster_name is None or cluster_candidate_key is None or
                policy_hash is None or not str(policy_hash).strip()):
            raise ValueError(
                "cluster FDR evidence requires target keys and a policy hash")
        normalized_clusters = {
            str(name): {str(key): value for key, value in values.items()}
            for name, values in cluster_values.items()
        }
        body.update({
            "cluster_name": str(cluster_name),
            "cluster_candidate_key": str(cluster_candidate_key),
            "cluster_values": normalized_clusters,
            "cluster_results": {
                name: benjamini_yekutieli(values, alpha=nominal)
                for name, values in normalized_clusters.items()
            },
        })
        body["policy_hash"] = str(policy_hash)
    return body


def _fdr_batch_matches(batch: Mapping | None, *, statistics: Mapping,
                       checks: Mapping, provenance: Mapping,
                       candidate_id: str | None,
                       cluster_multiple_tests: Mapping | None = None) -> bool:
    """Recompute and bind a complete multiple-testing batch to one gate."""
    if not isinstance(batch, Mapping) or not isinstance(statistics, Mapping):
        return False
    if (batch.get("schema") != FDR_BATCH_SCHEMA or
            batch.get("method") != FDR_BATCH_METHOD or
            batch.get("p_value_source") not in FDR_BATCH_P_VALUE_SOURCES):
        return False
    if candidate_id is None or batch.get("candidate_id") != str(candidate_id):
        return False
    try:
        alpha = float(statistics.get("alpha"))
        if (not math.isfinite(alpha) or not 0.0 < alpha <= 1.0 or
                not _close_number(batch.get("alpha"), alpha)):
            return False
        family_values = batch.get("family_values")
        family_results = batch.get("family_results")
        global_values = batch.get("global_values")
        global_results = batch.get("global_results")
        if (not isinstance(family_values, Mapping) or not family_values or
                not isinstance(family_results, Mapping) or
                not isinstance(global_values, Mapping) or not global_values or
                not isinstance(global_results, Mapping)):
            return False
        expected_family: dict[str, dict] = {}
        for name, values in family_values.items():
            if not isinstance(name, str) or not isinstance(values, Mapping) or not values:
                return False
            expected_family[name] = benjamini_yekutieli(
                dict(values), alpha=alpha)
        expected_global = benjamini_yekutieli(dict(global_values), alpha=alpha)
        if expected_family != dict(family_results) or \
                expected_global != dict(global_results):
            return False
        family_name = batch.get("family_name")
        family_key = batch.get("family_candidate_key")
        global_key = batch.get("global_candidate_key")
        if (not isinstance(family_name, str) or
                not isinstance(family_key, str) or
                not isinstance(global_key, str)):
            return False
        target_family_values = family_values.get(family_name)
        target_family_results = family_results.get(family_name)
        if (not isinstance(target_family_values, Mapping) or
                not isinstance(target_family_results, Mapping) or
                family_key not in target_family_values or
                family_key not in target_family_results or
                global_key not in global_values or global_key not in global_results):
            return False
        family_result = target_family_results[family_key]
        global_result = global_results[global_key]
        if not isinstance(family_result, Mapping) or not isinstance(global_result, Mapping):
            return False
        family_raw = target_family_values[family_key]
        global_raw = global_values[global_key]
        if not _close_number(family_raw, global_raw):
            return False
        expected_source_p = (statistics.get("p_value")
                             if batch.get("p_value_source") == "gate" else
                             provenance.get("selection_raw_p_value"))
        if expected_source_p is None or not _close_number(family_raw, expected_source_p):
            return False
        family_q = float(statistics.get("family_q_value"))
        global_q = float(statistics.get("q_value"))
        if (not _close_number(family_result.get("p_adjusted"), family_q) or
                not _close_number(global_result.get("p_adjusted"), global_q) or
                ("family_fdr_significant" in checks and
                 bool(checks.get("family_fdr_significant")) !=
                 bool(family_result.get("significant"))) or
                ("global_fdr_significant" in checks and
                 bool(checks.get("global_fdr_significant")) !=
                 bool(global_result.get("significant")))):
            return False
        cluster_required = (
            "cluster_fdr_significant" in checks or
            statistics.get("cluster_q_value") is not None)
        if cluster_required:
            cluster_name = batch.get("cluster_name")
            cluster_key = batch.get("cluster_candidate_key")
            cluster_values = batch.get("cluster_values")
            cluster_results = batch.get("cluster_results")
            if (not isinstance(cluster_name, str) or
                    not isinstance(cluster_key, str) or
                    not isinstance(cluster_values, Mapping) or not cluster_values or
                    not isinstance(cluster_results, Mapping)):
                return False
            expected_clusters: dict[str, dict] = {}
            for name, values in cluster_values.items():
                if not isinstance(name, str) or not isinstance(values, Mapping) or not values:
                    return False
                expected_clusters[name] = benjamini_yekutieli(
                    dict(values), alpha=alpha)
            if expected_clusters != dict(cluster_results):
                return False
            target_cluster_values = cluster_values.get(cluster_name)
            target_cluster_results = cluster_results.get(cluster_name)
            if (not isinstance(target_cluster_values, Mapping) or
                    not isinstance(target_cluster_results, Mapping) or
                    cluster_key not in target_cluster_values or
                    cluster_key not in target_cluster_results):
                return False
            cluster_result = target_cluster_results[cluster_key]
            if (not isinstance(cluster_result, Mapping) or
                    not _close_number(target_cluster_values[cluster_key], family_raw) or
                    not _close_number(cluster_result.get("p_adjusted"),
                                      statistics.get("cluster_q_value")) or
                    bool(checks.get("cluster_fdr_significant")) !=
                    bool(cluster_result.get("significant"))):
                return False
            # The raw cluster map is meaningful only under the frozen
            # dependence policy that produced it.  Bind the batch to the
            # separately persisted target correction so a re-signed proof
            # cannot swap policy identities while preserving the same q-value.
            declared_policy = (
                cluster_multiple_tests.get("policy_hash")
                if isinstance(cluster_multiple_tests, Mapping) else None)
            batch_policy = batch.get("policy_hash")
            if (not isinstance(declared_policy, str) or not declared_policy or
                    not isinstance(batch_policy, str) or not batch_policy or
                    batch_policy != declared_policy):
                return False
        elif any(key in batch for key in (
                "cluster_name", "cluster_candidate_key", "cluster_values",
                "cluster_results")):
            return False
        return True
    except (TypeError, ValueError, OverflowError):
        return False

# A gate's booleans often share one underlying source statistic.  Keeping this
# map explicit makes that overlap visible to reports without changing any
# authorizing decision.  Paths are relative to a verified gate envelope.
_GATE_SOURCE_STATISTICS = {
    "actual_control_available": ("control.matched", "control.available"),
    "fit_delta_positive": ("fit_control.mean_delta",),
    "heldout_delta_positive": ("control.mean_delta",),
    "heldout_delta_lcb_positive": ("control.mean_delta_lcb",),
    "heldout_p_significant": ("statistics.p_value", "statistics.alpha"),
    "family_fdr_significant": ("statistics.family_q_value", "statistics.alpha"),
    "global_fdr_significant": ("statistics.q_value", "statistics.alpha"),
    "cumulative_fdr_significant": (
        "online_fdr.p_value", "online_fdr.allocated_alpha"),
    "heldout_net_pnl_positive": ("performance.heldout_net_pnl",),
    "heldout_expectancy_positive": ("performance.heldout_expectancy",),
    "falsification": (
        "statistics.p_value", "statistics.alpha",
        "falsification.observed_mean", "falsification.zero_placebo",
        "falsification.distinct", "falsification.ratio",
        "falsification.minimum_ratio",
    ),
    "separated": ("separation.overlap_sessions", "separation.passes"),
    "walk_forward_available": ("walk_forward.available",),
    "walk_forward_adequate": ("walk_forward.adequate",),
    "walk_forward_majority_positive": ("walk_forward.majority_positive",),
    "null_control_available": ("null_control.available", "null_control.matched"),
    "null_control_delta_positive": ("null_control.mean_delta",),
    "qualification_available": ("qualification.available",),
    "qualification_net_positive": ("qualification.net_pnl",),
    "qualification_delta_positive": ("qualification.mean_delta",),
    "fit_floor_adequate": ("floors.fit.adequate",),
    "heldout_floor_adequate": ("floors.heldout.adequate",),
    "qualification_floor_adequate": ("floors.qualification.adequate",),
    "max_drawdown_supported": ("performance.max_drawdown",),
    "risk_unit_adequate": ("risk_unit_report.adequate",),
    "fill_quality_adequate": ("fill_quality.adequate",),
    "cost_stress_adequate": ("cost_stress.adequate",),
}
_LEGACY_FALSIFICATION_SOURCE_STATISTICS = (
    "falsification.p_value", "falsification.alpha",
    "falsification.observed_mean", "falsification.zero_placebo",
    "falsification.distinct", "falsification.ratio",
    "falsification.minimum_ratio",
)

# A replay row is useful diagnostic evidence even when it cannot authorize a
# statistical conclusion.  Keep the projection schema deliberately small and
# deterministic: callers persist the raw rows separately and this summary
# records exactly how many rows entered each authorizing calculation.
AUTHORIZATION_PROJECTION_SCHEMA = "authorization-projection.v1"


def _has_fill_metadata(row: Mapping[str, Any]) -> bool:
    return any(row.get(name) is not None for name in (
        "entry_fill_source", "exit_fill_source", "entry_feed", "exit_feed",
        "entry_provider", "exit_provider", "entry_quote_age_seconds",
        "exit_quote_age_seconds", "entry_option_feed", "exit_option_feed",
        "exit_fill_schema", "exit_fill_claim"))


def _finite_age(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and 0.0 <= float(value) <=
            OPTION_MAX_QUOTE_AGE_SECONDS)


def _authorization_exclusion_reason(row: Mapping[str, Any], *, vehicle: str,
                                    strict: bool,
                                    equity_feed: str = "iex") -> str | None:
    """Return a stable reason when one replay row cannot authorize.

    ``strict=False`` is retained solely for old summary-only fixtures that
    predate fill provenance.  Production replay/factory callers pass
    ``strict=True``; the first explicit fill field therefore opts a cohort
    into the same strict quality contract as :func:`fill_source_summary`.
    """
    if (str(row.get("evidence_mode") or "").strip().lower() ==
            "diagnostic_historical_backfill"):
        return "diagnostic_historical_backfill"
    if row.get("directional_authorizing") is False:
        # A censored hold-discontinuity outcome is useful audit telemetry but
        # cannot enter any directional effect, floor, or control calculation.
        return "diagnostic_directional_outcome"
    if row.get("no_trade") is True:
        return "no_trade"
    if row.get("vehicle", vehicle) != vehicle:
        return "vehicle_mismatch"
    if not strict and not _has_fill_metadata(row):
        return None
    entry_source = str(row.get("entry_fill_source") or "").strip().lower()
    exit_source = str(row.get("exit_fill_source") or "").strip().lower()
    if exit_source == RESTING_BRACKET:
        return validate_resting_bracket_fill(row, equity_feed=equity_feed)
    if entry_source != QUOTE_FILL or exit_source != QUOTE_FILL:
        return "non_authorizing_fill_source"
    if not _finite_age(row.get("entry_quote_age_seconds")) or not _finite_age(
            row.get("exit_quote_age_seconds")):
        return "stale_or_missing_quote_age"
    entry_feed = str(row.get("entry_feed", row.get("entry_option_feed")) or "").strip().lower()
    exit_feed = str(row.get("exit_feed", row.get("exit_option_feed")) or "").strip().lower()
    required_feed = equity_feed if vehicle == "equity" else "opra"
    if entry_feed != required_feed or exit_feed != required_feed:
        return "non_authorizing_feed"
    if vehicle == "equity":
        if not str(row.get("entry_provider") or "").strip() or not str(
                row.get("exit_provider") or "").strip():
            return "missing_quote_provider"
    # Option rows produced by IBR also carry contract-feed aliases.  If
    # present, they must agree with the executable OPRA identity rather than
    # allowing an indicative contract to masquerade as a quote.
    if vehicle == "option":
        for name in ("entry_option_feed", "exit_option_feed"):
            value = row.get(name)
            if value is not None and str(value).strip().lower() != "opra":
                return "non_authorizing_feed"
    return None


def authorization_projection(rows: Iterable[Mapping], *, vehicle: str,
                              strict: bool = True,
                              equity_feed: str = "iex") -> dict:
    """Project raw replay rows into authorizing-quality executed evidence.

    The returned ``eligible`` list is the only list that should feed floors,
    paired inference, walk-forward, performance, retirement, risk/cost stress,
    or qualification.  ``raw`` and ``excluded`` remain available for audit;
    no replay observation is silently discarded.
    """
    if vehicle not in {"equity", "option"}:
        raise ValueError("vehicle must be equity or option")
    equity_feed = str(equity_feed or "").strip().lower().replace("-", "_")
    if equity_feed == "delayed":
        equity_feed = "delayed_sip"
    if equity_feed not in {"iex", "sip", "delayed_sip"}:
        raise ValueError("equity_feed must be iex, sip, or delayed_sip")
    raw = [dict(row) for row in rows if isinstance(row, Mapping)]
    eligible: list[dict] = []
    excluded: list[dict] = []
    reasons: dict[str, int] = {}
    for index, row in enumerate(raw):
        reason = _authorization_exclusion_reason(row, vehicle=vehicle,
                                                 strict=bool(strict),
                                                 equity_feed=equity_feed)
        if reason is None:
            eligible.append(row)
            continue
        reasons[reason] = reasons.get(reason, 0) + 1
        excluded.append({
            "index": index,
            "opportunity_id": row.get("opportunity_id"),
            "session_date": row.get("session_date"),
            "reason": reason,
        })
    return {
        "schema": AUTHORIZATION_PROJECTION_SCHEMA,
        "vehicle": vehicle,
        "equity_feed": equity_feed,
        "strict": bool(strict),
        "raw": raw,
        "eligible": eligible,
        "excluded": excluded,
        "counts": {"raw": len(raw), "eligible": len(eligible),
                   "excluded": len(excluded)},
        "reasons": dict(sorted(reasons.items())),
    }


def _projection_summary(projection: Mapping[str, Any], *,
                        include_equity_feed: bool = True) -> dict:
    keys = ["schema", "vehicle"]
    if include_equity_feed:
        keys.append("equity_feed")
    keys.extend(("strict", "counts", "reasons", "excluded"))
    return {key: projection.get(key) for key in keys}


def _authorizing_rows(rows: Iterable[Mapping], *, vehicle: str,
                      equity_feed: str = "iex") -> list[dict]:
    """Use strict projection when replay provenance is present.

    Small historical unit fixtures predate fill metadata and remain useful for
    diagnostics.  Real replay cohorts opt into strict eligibility as soon as
    any fill field is present; this keeps direct gate helpers backward-auditable
    without allowing a production row to bypass the quality boundary.
    """
    raw = [dict(row) for row in rows if isinstance(row, Mapping)]
    strict = any(_has_fill_metadata(row) for row in raw)
    return authorization_projection(
        raw, vehicle=vehicle, strict=True,
        equity_feed=equity_feed)["eligible"] \
        if strict else raw


def protocol_minimums(lane: str) -> dict[str, int]:
    """Return the immutable minimum sample sizes for an authorizing lane.

    ``backtest`` covers offline/factory evidence.  ``shadow`` is the
    parity-matched live-shadow authorization lane and therefore carries the
    larger executed-trade floor.  The returned mapping is a fresh dictionary
    so callers cannot mutate the code-owned protocol constants.
    """
    normalized = str(lane).strip().lower()
    if normalized == "backtest":
        return {"trades": PROTOCOL_BACKTEST_MIN_TRADES,
                "sessions": PROTOCOL_BACKTEST_MIN_SESSIONS,
                "clusters": PROTOCOL_BACKTEST_MIN_CLUSTERS}
    if normalized == "shadow":
        return {"trades": PROTOCOL_SHADOW_MIN_TRADES,
                "sessions": PROTOCOL_SHADOW_MIN_SESSIONS,
                "clusters": PROTOCOL_SHADOW_MIN_CLUSTERS}
    raise ValueError("lane must be backtest or shadow")


def validate_protocol_floor(*, lane: str, min_trades: int,
                            min_sessions: int) -> dict[str, int]:
    """Reject caller-configured floors below the authorizing protocol.

    This helper is intentionally independent from :func:`structural_floor`:
    local diagnostics may request smaller samples, while production/API write
    boundaries must fail before replay or persistence begins.
    """
    required = protocol_minimums(lane)
    values = {"trades": min_trades, "sessions": min_sessions}
    for name, value in values.items():
        if (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{name} must be an integer")
        if int(value) < required[name]:
            raise ValueError(
                f"{name} must be >= {required[name]} for {lane} authorization")
    return required


def _floor_minimums_meet(report: Mapping[str, Any] | None, *, lane: str) -> bool:
    """Whether a persisted fit/held-out floor declares the protocol minimum."""
    if not isinstance(report, Mapping):
        return False
    minimums = report.get("minimums")
    if not isinstance(minimums, Mapping):
        return False
    try:
        required = protocol_minimums(lane)
        return all(
            (isinstance(minimums.get(name), int) and
             not isinstance(minimums.get(name), bool) and
             minimums.get(name) >= value)
            for name, value in required.items())
    except (TypeError, ValueError, OverflowError):
        return False


def _qualification_minimums_meet(minimums: Mapping[str, Any] | None) -> bool:
    if not isinstance(minimums, Mapping):
        return False
    try:
        return all(
            (isinstance(minimums.get(name), int) and
             not isinstance(minimums.get(name), bool) and
             minimums.get(name) >= value)
            for name, value in (
                ("trades", PROTOCOL_QUALIFICATION_MIN_TRADES),
                ("sessions", PROTOCOL_QUALIFICATION_MIN_SESSIONS),
                ("clusters", PROTOCOL_QUALIFICATION_MIN_CLUSTERS)))
    except (TypeError, ValueError, OverflowError):
        return False


class SealedWindowError(RuntimeError):
    """Raised when sealed final-qualification data is used more than once."""


@dataclass(frozen=True)
class AcceptanceFloor:
    min_trades: int = 100
    min_sessions: int = 10
    min_net_pnl: float = 0.0
    max_drawdown: float | None = None
    min_clusters: int = 0

    def check(self, trades: Iterable[Mapping], *, vehicle: str,
              equity_feed: str = "iex") -> dict:
        rows = _authorizing_rows(
            trades, vehicle=vehicle, equity_feed=equity_feed)
        rows = [row for row in rows if row.get("vehicle", vehicle) == vehicle]
        # Discovery materializes zero-outcome opportunities to avoid
        # survivorship bias.  They remain part of the session/control sample,
        # but are not executed trades and must not satisfy a trade floor.
        executed = [row for row in rows if row.get("no_trade") is not True]
        sessions = {row.get("session_date") for row in rows
                    if row.get("session_date") is not None}
        net = sum(float(row.get("net_pnl", 0.0)) for row in rows)
        drawdown = max_drawdown_of(rows)
        clusters = len({row.get("cluster", row.get("session_date")) for row in rows
                        if row.get("cluster", row.get("session_date")) is not None})
        checks = {
            "trades": len(executed) >= self.min_trades,
            "sessions": len(sessions) >= self.min_sessions,
            "net_pnl": net >= self.min_net_pnl,
            "clusters": clusters >= self.min_clusters,
        }
        if self.max_drawdown is not None:
            checks["max_drawdown"] = drawdown <= float(self.max_drawdown)
        structural_checks = {
            "trades": checks["trades"],
            "sessions": checks["sessions"],
            "clusters": checks["clusters"],
        }
        performance_checks = {key: value for key, value in checks.items()
                              if key not in structural_checks}
        return {
            "vehicle": vehicle, "trades": len(executed),
            "sessions": len(sessions), "net_pnl": net,
            "max_drawdown": drawdown, "clusters": clusters,
            "structural_passes": all(structural_checks.values()),
            "performance_passes": all(performance_checks.values()),
            "passes": all(checks.values()), "checks": checks,
            "structural_checks": structural_checks,
            "performance_checks": performance_checks,
        }


def chronological_split(rows: Sequence[Mapping], *, fit_fraction: float = .6,
                        require_order: bool = False) -> tuple[list, list]:
    """Split whole trading sessions across one chronological boundary."""
    if not 0 < fit_fraction < 1:
        raise ValueError("fit_fraction must be between zero and one")
    original = list(rows)
    key = lambda row: (_session_key(row), str(row.get("entry_timestamp", "")),
                       str(row.get("opportunity_id", "")))
    if require_order and any(key(left) > key(right)
                             for left, right in zip(original, original[1:])):
        raise ValueError("rows must already be chronological")
    ordered = sorted(original, key=key)
    sessions = sorted({_session_key(row) for row in ordered})
    if len(sessions) < 2:
        return ordered, []
    cut = max(1, min(len(sessions) - 1, int(len(sessions) * fit_fraction)))
    fit_sessions = set(sessions[:cut])
    return ([row for row in ordered if _session_key(row) in fit_sessions],
            [row for row in ordered if _session_key(row) not in fit_sessions])


def _session_key(row: Mapping) -> str:
    return str(row.get("session_date") or row.get("entry_timestamp") or
               row.get("opportunity_id") or "")


def structural_floor(rows: Iterable[Mapping], *, vehicle: str,
                     min_trades: int, min_sessions: int,
                     min_clusters: int = 0, required: bool = True,
                     available_sessions: int | None = None,
                     universe_size: int | None = None,
                     signal_opportunities: int | None = None,
                     target_total: int | None = None,
                     equity_feed: str = "iex") -> dict:
    """Report structural adequacy without treating profitability as sample size.

    Profitability is deliberately absent here.  ``AcceptanceFloor.min_net_pnl``
    is exercised by :func:`performance_floor`, which is a separate, mandatory
    gate check rather than a sample-size statement.
    """
    materialized = _authorizing_rows(
        rows, vehicle=vehicle, equity_feed=equity_feed)
    report = AcceptanceFloor(
        min_trades=min_trades, min_sessions=min_sessions,
        min_clusters=min_clusters,
    ).check(materialized, vehicle=vehicle, equity_feed=equity_feed)
    report["minimums"] = {"trades": int(min_trades),
                          "sessions": int(min_sessions),
                          "clusters": int(min_clusters)}
    report["required"] = bool(required)
    report["adequate"] = bool(report["structural_passes"] if required else True)
    report["feasibility"] = floor_feasibility(
        materialized, vehicle=vehicle, min_trades=min_trades,
        min_sessions=min_sessions, min_clusters=min_clusters,
        available_sessions=available_sessions, universe_size=universe_size,
        signal_opportunities=signal_opportunities, target_total=target_total,
        equity_feed=equity_feed)
    return report


def floor_feasibility(rows: Iterable[Mapping], *, vehicle: str,
                      min_trades: int, min_sessions: int,
                      min_clusters: int = 0,
                      available_sessions: int | None = None,
                      universe_size: int | None = None,
                      signal_opportunities: int | None = None,
                      target_total: int | None = None,
                      equity_feed: str = "iex") -> dict:
    """Classify whether a floor is impossible, underpowered or inconclusive.

    A failed structural floor is not automatically a negative result.  The
    potential sample is estimated from all rows (including explicit
    ``no_trade`` opportunities), while callers may provide a stronger bound
    from the full session/universe inventory.  This report is deliberately
    descriptive and never lowers a configured floor.
    """
    materialized = _authorizing_rows(
        rows, vehicle=vehicle, equity_feed=equity_feed)
    materialized = [row for row in materialized if row.get("vehicle", vehicle) == vehicle]
    sessions = {str(_session_key(row)) for row in materialized if _session_key(row)}
    clusters = {str(row.get("cluster") or _session_key(row))
                for row in materialized if row.get("cluster") or _session_key(row)}
    observed_trades = sum(1 for row in materialized if row.get("no_trade") is not True)
    capacity_explicit = (available_sessions is not None or universe_size is not None or
                          signal_opportunities is not None or target_total is not None)
    available = len(sessions) if available_sessions is None else max(0, int(available_sessions))
    opportunities = (len(materialized) if signal_opportunities is None
                     else max(0, int(signal_opportunities)))
    if target_total is not None:
        opportunities = max(opportunities, int(target_total))
    if universe_size is not None and available:
        opportunities = max(opportunities, int(universe_size) * available)
    impossible_reasons = []
    if available < int(min_sessions):
        impossible_reasons.append("available_sessions_below_minimum")
    if opportunities < int(min_trades):
        impossible_reasons.append("available_opportunities_below_minimum")
    if min_clusters and len(clusters) < int(min_clusters) and available < int(min_clusters):
        impossible_reasons.append("available_clusters_below_minimum")
    structural_pass = (observed_trades >= int(min_trades) and
                        len(sessions) >= int(min_sessions) and
                        len(clusters) >= int(min_clusters))
    net_pnl = sum(float(row.get("net_pnl", 0.0)) for row in materialized)
    if impossible_reasons and capacity_explicit:
        status = "impossible"
    elif structural_pass and net_pnl < 0:
        status = "negative"
    elif structural_pass:
        status = "inconclusive"
    else:
        status = "underpowered"
    return {
        "status": status,
        "classification": status,
        "observed": {"trades": observed_trades, "sessions": len(sessions),
                      "clusters": len(clusters)},
        "available": {"trades": opportunities, "sessions": available,
                      "clusters": max(len(clusters), available)},
        "minimums": {"trades": int(min_trades), "sessions": int(min_sessions),
                     "clusters": int(min_clusters)},
        "shortfall": {"trades": max(0, int(min_trades) - observed_trades),
                      "sessions": max(0, int(min_sessions) - len(sessions)),
                      "clusters": max(0, int(min_clusters) - len(clusters))},
        "reasons": impossible_reasons or (["sample_below_floor"]
                   if not structural_pass else (["negative_performance"]
                   if net_pnl < 0 else ["floor_met_but_performance_untested"])),
        "net_pnl": net_pnl,
        "adequate": bool(structural_pass),
    }


def performance_floor(rows: Iterable[Mapping], *, vehicle: str,
                      min_net_pnl: float = 0.0,
                      min_expectancy: float = 0.0,
                      equity_feed: str = "iex") -> dict:
    """Require absolute, after-cost profitability rather than only a delta.

    A variant that loses money on unseen data has no edge to validate, however
    favourably it compares with the parent specification it was mutated from.
    """
    report = AcceptanceFloor(min_trades=0, min_sessions=0,
                             min_net_pnl=float(min_net_pnl)).check(
                                 rows, vehicle=vehicle,
                                 equity_feed=equity_feed)
    trades = int(report["trades"])
    net = float(report["net_pnl"])
    expectancy = net / trades if trades else None
    return {
        "vehicle": vehicle, "net_pnl": net, "trades": trades,
        "expectancy": expectancy,
        "min_net_pnl": float(min_net_pnl),
        "min_expectancy": float(min_expectancy),
        "net_pnl_positive": bool(trades > 0 and net > float(min_net_pnl)),
        "expectancy_positive": bool(expectancy is not None and
                                    expectancy > float(min_expectancy)),
    }


def expectancy_rejection_report(
        rows: Iterable[Mapping], *, vehicle: str,
        minimum_useful_r: float = RETIREMENT_MIN_USEFUL_R,
        confidence: float = RETIREMENT_CONFIDENCE,
        min_sessions: int = RETIREMENT_MIN_SESSIONS,
        draws: int = DEFAULT_BOOTSTRAP_DRAWS,
        seed: int | None = None,
        block_length: int | None = None,
        equity_feed: str = "iex") -> dict:
    """Test whether a useful after-cost expectancy has been ruled out.

    Promotion asks whether the lower bound is positive.  Retirement is the
    opposite scientific question: whether the *upper* bound is already below
    a preregistered minimum economically useful edge.  Whole sessions are
    resampled so correlated intraday trades never masquerade as independent
    evidence.  R multiples are preferred; legacy rows without a risk anchor
    remain auditable in P&L units against a zero minimum.
    """
    selected = [row for row in _authorizing_rows(
                rows, vehicle=vehicle, equity_feed=equity_feed)
                if row.get("vehicle", vehicle) == vehicle and
                row.get("no_trade") is not True]
    r_values: list[float] = []
    r_complete = bool(selected)
    for row in selected:
        try:
            value = float(row.get("r_multiple"))
        except (TypeError, ValueError, OverflowError):
            r_complete = False
            break
        if not math.isfinite(value):
            r_complete = False
            break
        r_values.append(value)
    unit = "r_multiple" if r_complete else "net_pnl"
    values = (r_values if r_complete else
              [float(row.get("net_pnl", 0.0)) for row in selected])
    clusters = [str(row.get("cluster") or _session_key(row)) for row in selected]
    session_count = len({cluster for cluster in clusters if cluster})
    resolved_block_length = (min(SERIAL_BLOCK_LENGTH, max(1, session_count))
                             if block_length is None else int(block_length))
    bound = moving_block_cluster_bootstrap_lower_bound(
        values, clusters, confidence=float(confidence), draws=int(draws),
        seed=seed, block_length=resolved_block_length,
        min_clusters=max(2, int(min_sessions)))
    threshold = float(minimum_useful_r) if r_complete else 0.0
    upper = bound.get("upper_bound")
    sufficient = bool(bound.get("available") and
                      session_count >= int(min_sessions))
    rejects = bool(sufficient and upper is not None and
                   float(upper) <= threshold)
    return {
        # Preserve the report's historical public method label; the nested
        # bootstrap records the chronology-aware implementation explicitly.
        "method": "cluster_bootstrap_upper_equivalence_bound",
        "bootstrap_method": bound.get("method"),
        "unit": unit,
        "minimum_useful_expectancy": threshold,
        "confidence": float(confidence),
        "mean": bound.get("mean"),
        "upper_bound": upper,
        "lower_bound": bound.get("lower_bound"),
        "trades": len(values),
        "sessions": session_count,
        "sessions_required": int(min_sessions),
        "sample_sufficient": sufficient,
        "rejects_minimum_useful_edge": rejects,
        "draws": bound.get("draws"),
        "seed": bound.get("seed"),
        "block_length": bound.get("block_length"),
        "bootstrap": bound,
    }


def paired_delta(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *,
                 vehicle: str, equity_feed: str = "iex") -> dict:
    """Compare matched vehicle-local rows without pooling unmatched outcomes."""
    left = [row for row in _authorizing_rows(
            candidate, vehicle=vehicle, equity_feed=equity_feed)
            if row.get("vehicle", vehicle) == vehicle]
    right = [row for row in _authorizing_rows(
             baseline, vehicle=vehicle, equity_feed=equity_feed)
             if row.get("vehicle", vehicle) == vehicle]
    def unique(rows: Iterable[Mapping]) -> dict:
        by_key: dict = {}
        duplicates: set = set()
        for row in rows:
            key = row.get("opportunity_id", row.get("entry_timestamp"))
            if key in by_key:
                duplicates.add(key)
            else:
                by_key[key] = row
        for key in duplicates:
            by_key.pop(key, None)
        return by_key

    left_by_key = unique(left)
    right_by_key = unique(right)
    deltas = []
    r_deltas = []
    for key, row in left_by_key.items():
        other = right_by_key.get(key)
        if other is not None:
            deltas.append(float(row.get("net_pnl", 0.0)) - float(other.get("net_pnl", 0.0)))
            left_r = _risk_multiple(row)
            right_r = _risk_multiple(other)
            if left_r is not None and right_r is not None:
                r_deltas.append(left_r - right_r)
    return {"vehicle": vehicle, "matched": len(deltas),
            "mean_delta": mean(deltas) if deltas else None,
            "deltas": deltas,
            "r_deltas": r_deltas,
            "r_matched": len(r_deltas),
            "mean_r_delta": mean(r_deltas) if r_deltas else None,
            "risk_normalized_unit": "R" if r_deltas else None}


def _unique_by_match_key(rows: Iterable[Mapping], vehicle: str, *,
                         equity_feed: str = "iex") -> dict[str, Mapping]:
    """Index vehicle-local rows by comparison key, dropping ambiguous keys."""
    rows = _authorizing_rows(
        rows, vehicle=vehicle, equity_feed=equity_feed)
    values: dict[str, Mapping] = {}
    duplicates: set[str] = set()
    for row in rows:
        if row.get("vehicle", vehicle) != vehicle:
            continue
        key = _match_key(row, vehicle)
        if not key or key in values:
            duplicates.add(key)
        else:
            values[key] = row
    for key in duplicates:
        values.pop(key, None)
    return values


def matched_pairs(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *,
                  vehicle: str, equity_feed: str = "iex") -> dict:
    """Return the matched candidate-minus-baseline deltas and their clusters."""
    left = _unique_by_match_key(candidate, vehicle, equity_feed=equity_feed)
    right = _unique_by_match_key(baseline, vehicle, equity_feed=equity_feed)
    matched: list[tuple[float | None, str, Mapping, Mapping]] = []
    for key in sorted(left):
        other = right.get(key)
        if other is None:
            continue
        row = left[key]
        stamp = row.get("entry_timestamp") or row.get("session_date")
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            # Legacy diagnostic rows may carry a naive timestamp.  Never let
            # the host timezone alter cluster order; interpret that legacy
            # representation deterministically as UTC.  Authorizing market
            # rows are normalized with explicit offsets before reaching here.
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = parsed.timestamp()
        except (TypeError, ValueError):
            timestamp = None
        matched.append((timestamp, key, row, other))
    # Moving-block inference is meaningful only in market chronology.  Match
    # keys are symbol-major (vehicle:symbol:session) and therefore cannot be
    # used as the resampling order when a family spans multiple symbols.
    matched.sort(key=lambda item: (
        item[0] is None, item[0] if item[0] is not None else 0.0, item[1]))
    keys: list[str] = []
    deltas: list[float] = []
    r_deltas: list[float] = []
    r_clusters: list[int] = []
    clusters: list[int] = []
    stamps: list[float] = []
    for index, (timestamp, key, row, other) in enumerate(matched):
        if timestamp is None:
            timestamp = float(index * CLUSTER_SECONDS)
        keys.append(key)
        stamps.append(timestamp)
        clusters.append(int(timestamp // CLUSTER_SECONDS))
        deltas.append(float(row.get("net_pnl", 0.0)) -
                      float(other.get("net_pnl", 0.0)))
        left_r = _risk_multiple(row)
        right_r = _risk_multiple(other)
        if left_r is not None and right_r is not None:
            r_deltas.append(left_r - right_r)
            r_clusters.append(clusters[-1])
    return {"vehicle": vehicle, "keys": keys, "deltas": deltas,
            "clusters": clusters, "timestamps": stamps,
            "matched": len(deltas), "r_deltas": r_deltas,
            "r_clusters": r_clusters, "r_matched": len(r_deltas)}


def matched_cluster_test(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *,
                         vehicle: str, seed: int = 20260728,
                         confidence: float = LOWER_BOUND_CONFIDENCE,
                         iterations: int = 20_000,
                         min_matched: int = ACTUAL_CONTROL_MIN_MATCHED,
                         min_coverage: float = ACTUAL_CONTROL_MIN_COVERAGE,
                         equity_feed: str = "iex") -> dict:
    """Test matched opportunity deltas with deterministic session clustering."""
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise ValueError("iterations must be a positive integer")
    # The statistic and the adequacy report consume the same evidence twice.
    # Materialize one-shot iterables once so a generator cannot appear matched
    # to the test and empty to the authorization floor.
    candidate = [dict(row) for row in candidate]
    baseline = [dict(row) for row in baseline]
    pairs = matched_pairs(
        candidate, baseline, vehicle=vehicle, equity_feed=equity_feed)
    triples = [(stamp, delta, 0.0) for stamp, delta
               in zip(pairs["timestamps"], pairs["deltas"])]
    result = paired_cluster_sign_flip(triples, cluster_seconds=CLUSTER_SECONDS,
                                       iterations=iterations, seed=seed)
    cluster_count = len(set(pairs["clusters"]))
    block_length = min(SERIAL_BLOCK_LENGTH, max(1, cluster_count))
    bound = moving_block_cluster_bootstrap_lower_bound(
        pairs["deltas"], pairs["clusters"], confidence=confidence,
        block_length=block_length)
    result["matched"] = pairs["matched"]
    result["matched_ids_hash"] = _content_hash(pairs["keys"])
    result["deltas"] = list(pairs["deltas"])
    result["delta_clusters"] = list(pairs["clusters"])
    result["r_deltas"] = list(pairs.get("r_deltas", ()))
    result["r_delta_clusters"] = list(pairs.get("r_clusters", ()))
    result["r_matched"] = int(pairs.get("r_matched", 0))
    result["mean_delta"] = (sum(pairs["deltas"]) / pairs["matched"]
                            if pairs["matched"] else None)
    result["mean_delta_lcb"] = bound["lower_bound"]
    result["lower_bound"] = {key: bound[key] for key in (
        "method", "available", "confidence", "draws", "seed",
        "block_length", "clusters", "observations")}
    result["actual_control"] = True
    result["available"] = bool(pairs["matched"])
    adequacy = paired_control_adequacy(
        candidate, baseline, vehicle=vehicle,
        min_matched=min_matched, min_coverage=min_coverage,
        equity_feed=equity_feed)
    result["paired_adequacy"] = adequacy
    result["coverage"] = adequacy["coverage"]
    result["adequate"] = bool(adequacy["adequate"])
    r_values = result["r_deltas"]
    result["mean_r_delta"] = (sum(r_values) / len(r_values)
                               if r_values else None)
    if r_values:
        r_bound = moving_block_cluster_bootstrap_lower_bound(
            r_values, pairs["r_clusters"], confidence=confidence,
            block_length=min(SERIAL_BLOCK_LENGTH,
                             max(1, len(set(pairs["r_clusters"])))),
            min_clusters=max(2, len(set(pairs["r_clusters"]))))
        result["r_delta_lcb"] = r_bound.get("lower_bound")
        result["r_lower_bound"] = {
            key: r_bound.get(key) for key in (
                "method", "available", "confidence", "draws", "seed",
                "block_length", "clusters", "observations")}
    else:
        result["r_delta_lcb"] = None
        result["r_lower_bound"] = {"method": "moving_block_cluster_bootstrap",
                                    "available": False,
                                    "confidence": confidence,
                                    "draws": DEFAULT_BOOTSTRAP_DRAWS,
                                    "seed": None,
                                    "block_length": 1,
                                    "clusters": 0, "observations": 0}
    return result


def paired_control_adequacy(candidate: Iterable[Mapping], control: Iterable[Mapping], *,
                            vehicle: str, min_matched: int = NULL_CONTROL_MIN_MATCHED,
                            min_coverage: float = NULL_CONTROL_MIN_COVERAGE,
                            equity_feed: str = "iex") -> dict:
    """Check minimum paired count and coverage for a control arm.

    ``matched_cluster_test`` remains a descriptive statistic.  This separate
    authorizing predicate prevents a positive null delta from passing on a
    tiny, selectively matched subset of candidate opportunities.
    """
    candidate = [dict(row) for row in candidate]
    control = [dict(row) for row in control]
    candidate_rows = [row for row in _authorizing_rows(
        candidate, vehicle=vehicle, equity_feed=equity_feed)
                      if row.get("vehicle", vehicle) == vehicle and
                      row.get("no_trade") is not True]
    control_rows = [row for row in _authorizing_rows(
        control, vehicle=vehicle, equity_feed=equity_feed)
                    if row.get("vehicle", vehicle) == vehicle and
                    row.get("no_trade") is not True]
    # Match exactly the same executed-row universe used by the denominator.
    # This matters for legacy/diagnostic rows without fill metadata, where the
    # projection intentionally retains no-trade observations for audit.
    pairs = matched_pairs(candidate_rows, control_rows, vehicle=vehicle,
                          equity_feed=equity_feed)
    # Coverage answers the authorizing question: "what fraction of the
    # candidate trades being tested has a matched control?"  Extra trades in
    # a more-active baseline are outside that candidate estimand and cannot
    # make an otherwise complete comparison inadequate.  The inverse failure
    # remains closed: if the candidate has 30 trades and only five controls,
    # coverage is still 5/30, and the absolute matched-pair floor also fails.
    denominator = len(candidate_rows)
    coverage = pairs["matched"] / denominator if denominator else 0.0
    count_ok = pairs["matched"] >= int(min_matched)
    coverage_ok = coverage >= float(min_coverage)
    return {
        "matched": int(pairs["matched"]),
        "candidate_count": len(candidate_rows),
        "control_count": len(control_rows),
        "coverage": coverage,
        "minimum_matched": int(min_matched),
        "minimum_coverage": float(min_coverage),
        "count_adequate": bool(count_ok),
        "coverage_adequate": bool(coverage_ok),
        "adequate": bool(count_ok and coverage_ok),
    }


def _legacy_v2_paired_control_adequacy(
        candidate: Iterable[Mapping], control: Iterable[Mapping], *,
        vehicle: str, min_matched: int, min_coverage: float,
        equity_feed: str) -> dict:
    """Reproduce the v2 symmetric-coverage rule for audit verification only.

    v2's descriptive matcher retained ``no_trade`` rows in its matched
    statistic, while its adequacy denominator counted executed rows only.
    Keep that asymmetric historical quirk here so re-verification neither
    rewrites old evidence nor silently upgrades it to the v3 estimand.
    """
    candidate = [dict(row) for row in candidate]
    control = [dict(row) for row in control]
    pairs = matched_pairs(candidate, control, vehicle=vehicle,
                          equity_feed=equity_feed)
    candidate_rows = [row for row in _authorizing_rows(
        candidate, vehicle=vehicle, equity_feed=equity_feed)
                      if row.get("vehicle", vehicle) == vehicle and
                      row.get("no_trade") is not True]
    control_rows = [row for row in _authorizing_rows(
        control, vehicle=vehicle, equity_feed=equity_feed)
                    if row.get("vehicle", vehicle) == vehicle and
                    row.get("no_trade") is not True]
    denominator = max(len(candidate_rows), len(control_rows))
    coverage = pairs["matched"] / denominator if denominator else 0.0
    return {
        "matched": int(pairs["matched"]),
        "candidate_count": len(candidate_rows),
        "control_count": len(control_rows),
        "coverage": coverage,
        "minimum_matched": int(min_matched),
        "minimum_coverage": float(min_coverage),
        "count_adequate": pairs["matched"] >= int(min_matched),
        "coverage_adequate": coverage >= float(min_coverage),
        "adequate": (pairs["matched"] >= int(min_matched) and
                     coverage >= float(min_coverage)),
    }


def matched_effective_breadth(candidate: Iterable[Mapping],
                              baseline: Iterable[Mapping], *,
                              vehicle: str,
                              equity_feed: str = "iex") -> dict:
    """Measure cross-symbol breadth without treating it as extra sample size.

    Statistical independence is still earned from chronological session
    clusters.  This diagnostic records whether those session-level deltas are
    one common factor repeated across symbols or genuinely broader evidence;
    it prevents claims about cross-sectional breadth from being inferred from
    a raw trade count.
    """
    left = _unique_by_match_key(candidate, vehicle, equity_feed=equity_feed)
    right = _unique_by_match_key(baseline, vehicle, equity_feed=equity_feed)
    observations = []
    for key in sorted(left):
        other = right.get(key)
        if other is None:
            continue
        row = left[key]
        symbol = (row.get("underlying_symbol") if vehicle == "option" else None) or \
            row.get("symbol") or row.get("underlying_symbol")
        session = row.get("session_date") or row.get("cluster")
        if symbol is None or session is None:
            continue
        observations.append({
            "session": str(session), "symbol": str(symbol),
            "delta": float(row.get("net_pnl", 0.0)) -
                     float(other.get("net_pnl", 0.0)),
        })
    return effective_breadth_report(observations)


def cost_stress_report(rows: Iterable[Mapping], *, vehicle: str,
                       risk_report: Mapping,
                       equity_feed: str = "iex") -> dict:
    """Reprice realized replay P&L under preregistered entry-notional shocks.

    Source rows already contain P&L after the configured model.  Each stress
    scenario therefore subtracts only the incremental cost above the model's
    recorded round trip.  Scenario bps are charged once against entry
    notional; listed options additionally include two per-contract fee sides.
    The 25 bps scenario is the authorization veto; 9, 15 and 50 bps remain
    persisted diagnostics so sensitivity is visible rather than selected
    after seeing the result.
    """
    if vehicle not in {"equity", "option"}:
        raise ValueError("vehicle must be equity or option")
    model = CostModel.from_dict((risk_report or {}).get("cost_model") or {})
    base_by_id = {
        str(item.get("opportunity_id")): item.get("round_trip_cost")
        for item in (risk_report or {}).get("observations", ())
        if isinstance(item, Mapping)
    }
    executed = [dict(row) for row in _authorizing_rows(
                rows, vehicle=vehicle, equity_feed=equity_feed)
                if isinstance(row, Mapping) and
                row.get("vehicle", vehicle) == vehicle and
                row.get("no_trade") is not True]
    scenarios = []
    for scenario_bps in COST_STRESS_SCENARIOS_BPS:
        stressed_values = []
        missing = []
        for index, row in enumerate(executed):
            def number(*names, default=None):
                for name in names:
                    raw = row.get(name)
                    if raw is None:
                        continue
                    try:
                        value = float(raw)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if math.isfinite(value):
                        return value
                return default

            entry = number("entry_price", "entry_reference", "plan_entry")
            exit_price = number("exit_price", "exit_reference", default=entry)
            quantity = number("quantity", "contracts", default=1.0)
            multiplier = number(
                "contract_multiplier", "multiplier",
                default=100.0 if vehicle == "option" else 1.0)
            net = number("net_pnl")
            opportunity_id = str(row.get("opportunity_id", index))
            if (entry is None or exit_price is None or quantity is None or
                    multiplier is None or net is None or entry <= 0 or
                    quantity <= 0 or multiplier <= 0):
                missing.append(opportunity_id)
                continue
            base = base_by_id.get(opportunity_id)
            try:
                base_cost = float(base) if base is not None else model.round_trip_cost(
                    entry, exit_price, quantity, multiplier, vehicle=vehicle,
                    executable_quotes=(
                        row.get("entry_fill_source") == QUOTE_FILL and
                        row.get("exit_fill_source") == QUOTE_FILL))
            except (CostError, TypeError, ValueError, OverflowError):
                missing.append(opportunity_id)
                continue
            notional = abs(entry) * abs(quantity) * abs(multiplier)
            stressed_cost = notional * float(scenario_bps) / 10_000.0
            if vehicle == "option":
                stressed_cost += abs(quantity) * 2.0 * \
                    model.option_fee_per_contract_side
            stressed_values.append(net - max(0.0, stressed_cost - base_cost))
        net_pnl = sum(stressed_values)
        scenarios.append({
            "entry_notional_bps": float(scenario_bps),
            # Compatibility alias for v1 readers.  The machine-readable basis
            # below removes the historical ambiguity in this field name.
            "round_trip_bps": float(scenario_bps),
            "stress_basis_schema": STRESSED_COST_SCHEMA,
            "stress_basis": dict(STRESSED_COST_BASIS),
            "trades": len(stressed_values),
            "missing_opportunities": missing,
            "net_pnl": net_pnl,
            "expectancy": (net_pnl / len(stressed_values)
                           if stressed_values else None),
            "positive": bool(stressed_values and not missing and net_pnl > 0),
        })
    required = next(
        item for item in scenarios
        if item["entry_notional_bps"] == COST_STRESS_REQUIRED_BPS)
    return {
        "schema": "cost-stress-report.v1", "vehicle": vehicle,
        "stress_basis_schema": STRESSED_COST_SCHEMA,
        "stress_basis": dict(STRESSED_COST_BASIS),
        "required_entry_notional_bps": COST_STRESS_REQUIRED_BPS,
        # Compatibility alias retained for existing signed envelopes/readers.
        "required_round_trip_bps": COST_STRESS_REQUIRED_BPS,
        "scenario_bps": list(COST_STRESS_SCENARIOS_BPS),
        "scenarios": scenarios,
        "adequate": bool(required["positive"]),
    }


def placebo_null_distribution(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *,
                              vehicle: str, draws: int = DEFAULT_NULL_DRAWS,
                              seed: int | None = None,
                              equity_feed: str = "iex") -> dict:
    """Draw a seeded cluster sign-flip null distribution for matched deltas.

    ``placebo`` is the null *distribution* of the mean delta, not a single
    reflection of the observations.  The seed is derived from the matched
    content itself, so the same evidence always reproduces the same draws.
    """
    pairs = matched_pairs(
        candidate, baseline, vehicle=vehicle, equity_feed=equity_feed)
    null = sign_flip_null_statistics(pairs["deltas"], pairs["clusters"],
                                     draws=draws, seed=seed)
    return {"method": "seeded_cluster_sign_flip_null",
            "available": bool(null["available"]) and len(pairs["deltas"]) >= 2,
            "observed": list(pairs["deltas"]),
            "clusters": list(pairs["clusters"]),
            "placebo": list(null["statistics"]),
            "draws": int(null["draws"]), "seed": int(null["seed"]),
            "cluster_count": int(null["clusters"]),
            "p_value": float(null["p_value"]),
            "assignments_hash": _content_hash({"keys": pairs["keys"],
                                               "clusters": pairs["clusters"],
                                               "draws": int(null["draws"]),
                                               "seed": int(null["seed"])})}


# Historical name retained for the discovery facades; the semantics are the
# seeded null distribution above, not a single deterministic sign pattern.
deterministic_placebo_deltas = placebo_null_distribution


def _match_key(row: Mapping, vehicle: str) -> str:
    explicit = row.get("comparison_id")
    if explicit:
        return str(explicit)
    symbol = row.get("symbol")
    session = row.get("session_date")
    if symbol and session:
        return f"{row.get('vehicle', vehicle)}:{symbol}:{session}"
    return str(row.get("opportunity_id") or row.get("entry_timestamp") or "")


def _risk_multiple(row: Mapping[str, Any]) -> float | None:
    """Return one finite risk-normalized outcome when the row carries it.

    ``r_multiple`` is preferred because it is the replay's canonical risk
    unit.  Older rows often persist only P&L and ``risk_usd``; those remain
    useful for compatibility and are normalized here without changing the
    dollar statistic.  Invalid or non-positive risk anchors are omitted from
    the R effect rather than silently treated as zero.
    """
    raw = row.get("r_multiple")
    if raw is None:
        raw = row.get("net_pnl")
        risk = row.get("risk_usd")
        if risk is None:
            return None
        try:
            risk_value = float(risk)
            raw_value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(risk_value) or risk_value <= 0:
            return None
        value = raw_value / risk_value
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    return value if math.isfinite(value) else None


def placebo_ratio(observed: Sequence[float], placebo: Sequence[float]) -> float | None:
    """Return the observed mean over the mean absolute null draw."""
    if not placebo:
        return None
    baseline = mean(abs(float(value)) for value in placebo)
    return mean(float(value) for value in observed) / baseline if baseline else None


def _event_timestamp(row: Mapping[str, Any]) -> tuple[int, float, str]:
    """Stable chronology key for realized/marked equity events."""
    for name in ("realized_timestamp", "exit_timestamp", "timestamp",
                 "observed_at", "entry_timestamp", "session_date"):
        raw = row.get(name)
        if raw is None:
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                return (0, value, str(row.get("opportunity_id") or ""))
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return (0, parsed.timestamp(), str(row.get("opportunity_id") or ""))
        except (TypeError, ValueError, OverflowError):
            continue
    return (1, 0.0, str(row.get("opportunity_id") or ""))


def _equity_mark(row: Mapping[str, Any]) -> float | None:
    """Find an absolute account-equity/mark field, if the replay supplied one."""
    for name in ("account_equity", "equity", "equity_mark", "mark_equity",
                 "portfolio_equity", "ending_equity", "cash", "balance"):
        raw = row.get(name)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            return value
    return None


def max_drawdown_of(rows_or_values: Iterable[Mapping] | Iterable[float]) -> float:
    """Maximum chronological peak-to-trough loss.

    Trade rows contribute realized P&L at their exit/realization timestamp,
    so symbol-major input order cannot change the result.  When an account
    equity or intraday mark is present it is consumed as an absolute equity
    event; realized deltas continue from that mark.  Bare numeric iterables
    retain their historical sequential semantics.
    """
    materialized = list(rows_or_values)
    if not materialized:
        return 0.0
    if not all(isinstance(item, Mapping) for item in materialized):
        events: list[tuple[float, bool, tuple[int, float, str]]] = []
        for index, item in enumerate(materialized):
            try:
                value = float(item)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                events.append((value, False, (0, float(index), str(index))))
    else:
        events = []
        for index, row in enumerate(materialized):
            mark = _equity_mark(row)
            timestamp = _event_timestamp(row)
            if timestamp[0] != 0:
                # Untimestamped legacy rows retain their supplied chronology;
                # opportunity ids are identifiers, not an ordering signal.
                timestamp = (timestamp[0], float(index), timestamp[2])
            if mark is not None:
                events.append((mark, True, timestamp))
                continue
            raw = row.get("net_pnl", row.get("return_value", 0.0))
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                events.append((value, False, timestamp))
        events.sort(key=lambda item: item[2])
    peak = equity = drawdown = 0.0
    marked_equity = False
    for value, absolute, _ in events:
        if absolute:
            equity = value
            marked_equity = True
        else:
            equity += value
        if not marked_equity and equity > peak:
            peak = equity
        else:
            peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return float(drawdown) if math.isfinite(drawdown) else 0.0


def heldout_separation(fit: Sequence[Mapping], heldout: Sequence[Mapping]) -> dict:
    """Require one chronological boundary with no shared session."""
    fit_sessions = {str(row.get("session_date")) for row in fit if row.get("session_date") is not None}
    held_sessions = {str(row.get("session_date")) for row in heldout if row.get("session_date") is not None}
    fit_keys = [(str(row.get("session_date", "")), str(row.get("entry_timestamp", ""))) for row in fit]
    held_keys = [(str(row.get("session_date", "")), str(row.get("entry_timestamp", ""))) for row in heldout]
    boundary_ok = bool(fit_keys and held_keys and max(fit_keys) < min(held_keys))
    return {"fit": len(fit), "heldout": len(heldout),
            "overlap_sessions": sorted(fit_sessions & held_sessions),
            "passes": boundary_ok and not (fit_sessions & held_sessions)}


def falsification_gate(observed: Sequence[float], placebo: Sequence[float], *,
                       alpha: float = .05, minimum_ratio: float = 1.0,
                       preregistered_p_value: float | None = None,
                       independent_p_value: float | None = None,
                       independent_method: str | None = None,
                       independent_result_hash: str | None = None,
                       require_independent: bool = False) -> dict:
    """Place the observed mean delta inside a genuine null distribution.

    ``placebo`` is a sample of null mean deltas.  The decision is the
    preregistered paired-test p-value when supplied; the empirical null tail
    remains a reproducible falsification diagnostic and the ratio is reported
    for scale.  A separately seeded stream can be required as integrity
    evidence, but it is not treated as an independent market sample.
    """
    def finite_values(values: Sequence[float], label: str) -> list[float]:
        result = []
        for raw in values:
            if isinstance(raw, bool):
                raise ValueError(f"{label} values must be finite numbers")
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{label} values must be finite numbers") from exc
            if not math.isfinite(value):
                raise ValueError(f"{label} values must be finite numbers")
            result.append(value)
        return result

    draws = finite_values(placebo, "placebo")
    observed_values = finite_values(observed, "observed")
    observed_mean = mean(observed_values) if observed_values else 0.0
    placebo_mean = mean(draws) if draws else 0.0
    ratio = placebo_ratio(observed_values, draws)
    zero_placebo = bool(draws and all(abs(value) <= 1e-15 for value in draws))
    degenerate = bool(draws and max(draws) - min(draws) <= 1e-15)
    tolerance = 1e-15 * max(1.0, abs(observed_mean))
    extreme = sum(1 for value in draws if value >= observed_mean - tolerance)
    empirical_p_value = (extreme + 1) / (len(draws) + 1) if draws else 1.0
    independent_empirical_p = empirical_p_value
    if independent_p_value is not None:
        if isinstance(independent_p_value, bool):
            raise ValueError("independent_p_value must be a finite probability")
        try:
            independent_empirical_p = float(independent_p_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "independent_p_value must be a finite probability") from exc
        if (not math.isfinite(independent_empirical_p) or
                not 0.0 <= independent_empirical_p <= 1.0):
            raise ValueError("independent_p_value must be in [0,1]")
    independent_method = (str(independent_method).strip()
                          if independent_method is not None else "")
    independent_result_hash = (str(independent_result_hash).strip()
                               if independent_result_hash is not None else "")
    independent_supplied = bool(
        independent_p_value is not None and independent_method and
        independent_result_hash)
    primary_p_value = None
    if preregistered_p_value is not None:
        if isinstance(preregistered_p_value, bool):
            raise ValueError("preregistered_p_value must be a finite probability")
        try:
            primary_p_value = float(preregistered_p_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                "preregistered_p_value must be a finite probability") from exc
        if not math.isfinite(primary_p_value) or not 0.0 <= primary_p_value <= 1.0:
            raise ValueError("preregistered_p_value must be in [0,1]")
    if require_independent and not independent_supplied:
        # Current proofs require the second seeded calculation to be sealed so
        # verification can reproduce it, even though it is not a second
        # authorizing p-value.
        p_value = 1.0
        p_value_source = "missing_independent_result"
    elif primary_p_value is not None:
        # A differently seeded Monte Carlo stream over the same deltas is a
        # reproducibility/integrity check, not independent market evidence.
        # Keep the preregistered paired test as the sole authorizing p-value so
        # the gate does not accidentally impose an approximately alpha-squared
        # hurdle on one held-out sample.
        p_value = primary_p_value
        p_value_source = FALSIFICATION_P_VALUE_SOURCE
    elif independent_supplied:
        p_value = independent_empirical_p
        p_value_source = FALSIFICATION_INDEPENDENT_P_VALUE_SOURCE
    else:
        p_value = empirical_p_value
        p_value_source = "empirical_null_tail"
    positive_mean = observed_mean > 0
    ratio_adequate = ratio is not None and ratio >= minimum_ratio
    p_significant = p_value <= float(alpha)
    independent_pass = (not require_independent) or independent_supplied
    return {"observed_mean": observed_mean, "placebo_mean": placebo_mean,
            "ratio": ratio, "available": bool(draws),
            "draws": len(draws), "p_value": p_value, "alpha": float(alpha),
            "p_value_source": p_value_source,
            "primary_p_value": primary_p_value,
            "independent_p_value": (float(independent_empirical_p)
                                     if independent_supplied else None),
            "independent_method": independent_method or None,
            "independent_result_hash": independent_result_hash or None,
            "independent_supplied": independent_supplied,
            "minimum_ratio": float(minimum_ratio),
            "positive_mean": positive_mean,
            "ratio_adequate": ratio_adequate,
            "p_significant": p_significant,
            "zero_placebo": zero_placebo,
            "distinct": not degenerate,
            "passes": bool(draws) and not zero_placebo and not degenerate and
            positive_mean and p_significant and ratio_adequate and independent_pass}


def sample_counts(rows: Iterable[Mapping], *, vehicle: str,
                  equity_feed: str = "iex") -> dict:
    selected = [row for row in _authorizing_rows(
                rows, vehicle=vehicle, equity_feed=equity_feed)
                if row.get("vehicle", vehicle) == vehicle]
    return {
        "rows": len(selected),
        "trades": len([row for row in selected if row.get("no_trade") is not True]),
        "sessions": len({_session_key(row) for row in selected if _session_key(row)}),
        "clusters": len({str(row.get("cluster") or _session_key(row)) for row in selected
                         if row.get("cluster") or _session_key(row)}),
    }


def walk_forward_report(candidate: Sequence[Mapping], baseline: Sequence[Mapping], *,
                        vehicle: str, folds: int = 3,
                        min_fit_sessions: int = 1,
                        min_test_sessions: int = 1,
                        min_test_trades: int = 1,
                        min_matched: int | None = None,
                        min_coverage: float = ACTUAL_CONTROL_MIN_COVERAGE,
                        requested_min_sessions: int | None = None,
                        equity_feed: str = "iex") -> dict:
    """Return deterministic rolling-origin *forward stability* evidence.

    The rules are fixed; no refit is implied.  Each test fold is a contiguous
    multi-session block and its fit window contains only earlier sessions.
    Fold adequacy is reported separately from positivity so an empty or
    underpowered fold can never be mistaken for a negative result.  The
    requested aggregate session floor and the effective per-fold floor are
    persisted in the result; the aggregate floor is always enforced.
    """
    if int(folds) < 2:
        raise ValueError("walk-forward requires at least two folds")
    candidate = _authorizing_rows(
        candidate, vehicle=vehicle, equity_feed=equity_feed)
    baseline = _authorizing_rows(
        baseline, vehicle=vehicle, equity_feed=equity_feed)
    ordered = sorted(candidate, key=lambda row: (_session_key(row),
                                                 str(row.get("entry_timestamp", ""))))
    sessions = sorted({_session_key(row) for row in ordered if _session_key(row)})
    count = int(folds)
    fit_min = max(1, int(min_fit_sessions))
    requested_floor = (max(1, int(requested_min_sessions))
                       if requested_min_sessions is not None else
                       max(1, count * max(1, int(min_test_sessions))))
    requested_test_min = max(1, int(min_test_sessions))
    # The aggregate floor is explicit, while each fold receives the largest
    # feasible effective minimum after reserving the initial fit history.
    # Thus a 30-session, 3-fold floor uses nine test sessions per fold after a
    # one-session fit and still rejects a 29-session corpus at the aggregate
    # boundary.
    # Never let a per-fold request make the documented aggregate floor
    # mathematically unreachable (30 sessions / 3 folds => 9 after one fit).
    # The aggregate requirement below remains authoritative.
    test_min = max(1, min(requested_test_min,
                          max(0, requested_floor - fit_min) // count))
    trade_min = max(1, int(min_test_trades))
    matched_min = (trade_min if min_matched is None else max(0, int(min_matched)))
    # Keep an initial fit history, then divide the remaining sessions into
    # deterministic contiguous test blocks.  ``divmod`` makes the earliest
    # blocks receive the extra session and is stable across processes.
    tail = sessions[fit_min:]
    if (len(sessions) < requested_floor or
            len(sessions) < fit_min + count * test_min or len(tail) < count):
        return {"available": False, "folds": count, "tested_folds": 0,
                "positive_folds": 0, "adequate_folds": 0,
                "majority_positive": False, "adequate": False,
                "sessions": len(sessions), "fit_sessions_required": fit_min,
                "test_sessions_required": test_min,
                "requested_min_sessions": requested_floor,
                "effective_min_sessions": requested_floor,
                "requested_test_sessions": requested_test_min,
                "effective_test_sessions": test_min,
                "requested_min_test_sessions": requested_test_min,
                "effective_min_test_sessions": test_min,
                "effective_total_sessions": requested_floor,
                "min_sessions_requested": requested_floor,
                "min_sessions_effective": requested_floor,
                "minimum_sessions_requested": requested_floor,
                "minimum_sessions_effective": requested_floor,
                "aggregate_session_floor_met": False,
                "results": [], "fold_reports": []}
    block_count = min(count, len(tail))
    base, extra = divmod(len(tail), block_count)
    blocks = []
    cursor = 0
    for index in range(block_count):
        width = base + (1 if index < extra else 0)
        blocks.append(tail[cursor:cursor + width])
        cursor += width
    results = []
    for index, block in enumerate(blocks):
        test_sessions = set(block)
        fit_sessions = set(sessions[:sessions.index(block[0])])
        test_rows = [row for row in ordered if _session_key(row) in test_sessions]
        base_rows = [row for row in baseline if _session_key(row) in test_sessions]
        pairs = matched_pairs(
            test_rows, base_rows, vehicle=vehicle,
            equity_feed=equity_feed)
        control_adequacy = paired_control_adequacy(
            test_rows, base_rows, vehicle=vehicle,
            min_matched=matched_min, min_coverage=min_coverage,
            equity_feed=equity_feed)
        delta = (sum(pairs["deltas"]) / pairs["matched"]) if pairs["matched"] else None
        net = sum(float(row.get("net_pnl", 0.0)) for row in test_rows)
        test_trades = sum(1 for row in test_rows if row.get("no_trade") is not True)
        adequate = bool(len(fit_sessions) >= fit_min and
                        len(test_sessions) >= test_min and
                        test_trades >= trade_min and pairs["matched"] >= trade_min)
        # The fold's matched baseline is an authorizing requirement, not a
        # descriptive statistic.  Existing direct helper callers retain their
        # historical small-sample default (``min_matched`` omitted), while
        # discovery/proof callers pass the protocol minimum explicitly.
        adequate = bool(adequate and control_adequacy["adequate"])
        results.append({"fold": index, "fit_sessions": len(fit_sessions),
                        "test_sessions": sorted(test_sessions),
                        "matched": pairs["matched"], "test_trades": test_trades,
                        "control_adequacy": control_adequacy,
                        "mean_delta": delta, "net_pnl": net,
                        "adequate": adequate,
                        "adequacy_reason": ("ok" if adequate else
                            "fit_or_test_floor_not_met"),
                        "positive": bool(adequate and delta is not None and
                                          delta > 0 and net > 0)})
    adequate_results = [item for item in results if item["adequate"]]
    positive = sum(1 for item in adequate_results if item["positive"])
    aggregate_adequate = len(sessions) >= requested_floor
    return {"available": bool(adequate_results) and aggregate_adequate, "folds": count,
            "tested_folds": len(results), "adequate_folds": len(adequate_results),
            "positive_folds": positive,
            "majority_positive": bool(adequate_results and aggregate_adequate and
                                       positive * 2 > len(adequate_results)),
            "adequate": bool(len(adequate_results) == len(results) and results and
                              aggregate_adequate),
            "sessions": len(sessions), "fit_sessions_required": fit_min,
            "test_sessions_required": test_min, "test_trades_required": trade_min,
            "min_matched": matched_min, "min_coverage": float(min_coverage),
            "requested_min_sessions": requested_floor,
            "effective_min_sessions": requested_floor,
            "requested_test_sessions": requested_test_min,
            "effective_test_sessions": test_min,
            "requested_min_test_sessions": requested_test_min,
            "effective_min_test_sessions": test_min,
            "effective_total_sessions": requested_floor,
            "min_sessions_requested": requested_floor,
            "min_sessions_effective": requested_floor,
            "minimum_sessions_requested": requested_floor,
            "minimum_sessions_effective": requested_floor,
            "aggregate_session_floor_met": aggregate_adequate,
            "method": "fixed-rule_rolling-origin_forward-stability",
            "results": results, "fold_reports": results}


def qualification_report(rows: Sequence[Mapping], baseline: Sequence[Mapping], *,
                         vehicle: str, sessions: Sequence[str],
                         candidate_id: str | None = None,
                         preselected: bool = False,
                         min_trades: int = QUALIFICATION_MIN_TRADES,
                         min_sessions: int = QUALIFICATION_MIN_SESSIONS,
                         min_clusters: int = QUALIFICATION_MIN_CLUSTERS,
                         confidence: float = LOWER_BOUND_CONFIDENCE,
                         max_drawdown: float | None = None,
                         max_drawdown_r: float = QUALIFICATION_MAX_DRAWDOWN_R,
                         draws: int = DEFAULT_BOOTSTRAP_DRAWS,
                         seed: int | None = None,
                         block_length: int | None = None,
                         equity_feed: str = "iex") -> dict:
    """Score a sealed final window for one preselected candidate.

    Qualification is post-selection evidence.  Callers that searched a
    qualification window across variants must leave ``preselected`` false;
    such a report remains diagnostic and cannot authorize a passing envelope.
    """
    if (isinstance(min_trades, bool) or int(min_trades) < 0 or
            isinstance(min_sessions, bool) or int(min_sessions) < 0 or
            isinstance(min_clusters, bool) or int(min_clusters) < 0):
        raise ValueError("qualification minimums must be non-negative integers")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0.0 < confidence < 1.0:
        raise ValueError("qualification confidence must be between zero and one")
    if max_drawdown is not None:
        max_drawdown = float(max_drawdown)
        if not math.isfinite(max_drawdown) or max_drawdown < 0:
            raise ValueError("qualification max_drawdown must be finite and non-negative")
    max_drawdown_r = float(max_drawdown_r)
    if not math.isfinite(max_drawdown_r) or max_drawdown_r <= 0:
        raise ValueError("qualification max_drawdown_r must be finite and positive")
    raw_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    raw_baseline = [dict(row) for row in baseline if isinstance(row, Mapping)]
    strict_projection = any(_has_fill_metadata(row) for row in
                            [*raw_rows, *raw_baseline])
    candidate_projection = authorization_projection(
        raw_rows, vehicle=vehicle, strict=strict_projection,
        equity_feed=equity_feed)
    baseline_projection = authorization_projection(
        raw_baseline, vehicle=vehicle, strict=strict_projection,
        equity_feed=equity_feed)
    rows = candidate_projection["eligible"]
    baseline = baseline_projection["eligible"]
    drawdown_limit_source = "explicit_usd" if max_drawdown is not None else None
    if max_drawdown is None:
        risk_values = []
        for row in rows:
            raw_risk = row.get("risk_usd")
            if isinstance(raw_risk, bool):
                continue
            try:
                risk_value = float(raw_risk)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(risk_value) and risk_value > 0:
                risk_values.append(risk_value)
        if risk_values:
            max_drawdown = float(median(risk_values)) * max_drawdown_r
            drawdown_limit_source = "median_risk_usd_times_r"
    if not rows or not sessions:
        return {"available": False, "sessions": list(sessions), "net_pnl": 0.0,
                "matched": 0, "mean_delta": None, "trades": 0,
                "net_positive": False, "delta_positive": False,
                "clusters": 0, "delta_lcb": None,
                "minimums": {"trades": int(min_trades), "sessions": int(min_sessions),
                              "clusters": int(min_clusters)},
                "confidence": confidence, "confidence_supported": False,
                "delta_bootstrap": {
                    "method": "moving_block_cluster_bootstrap",
                    "available": False, "confidence": confidence,
                    "draws": max(1, int(draws)), "seed": seed,
                    "block_length": (min(SERIAL_BLOCK_LENGTH, 1)
                                     if block_length is None else int(block_length)),
                    "clusters": 0, "observations": 0,
                },
                "max_drawdown": None, "drawdown_supported": False,
                "max_drawdown_limit": max_drawdown,
                "max_drawdown_limit_source": drawdown_limit_source,
                "max_drawdown_limit_r": (max_drawdown_r
                                         if drawdown_limit_source ==
                                         "median_risk_usd_times_r" else None),
                "drawdown_within_limit": False,
                "drawdown_limit_required": True,
                "control_adequacy": {
                    "matched": 0, "candidate_count": 0, "control_count": 0,
                    "coverage": 0.0,
                    "minimum_matched": max(1, int(min_trades)),
                    "minimum_coverage": ACTUAL_CONTROL_MIN_COVERAGE,
                    "count_adequate": False, "coverage_adequate": False,
                    "adequate": False,
                },
                "adequate": False,
                "authorization_projection": {
                    "candidate": _projection_summary(candidate_projection),
                    "baseline": _projection_summary(baseline_projection),
                },
                "post_selection": {"preselected": bool(preselected),
                                    "candidate_id": candidate_id}}
    declared = tuple(str(item) for item in sessions)
    declared_set = set(declared)
    if (len(declared_set) != len(declared) or
            any(not item for item in declared_set)):
        raise ValueError("qualification sessions must be unique, non-empty strings")
    pairs = matched_pairs(
        rows, baseline, vehicle=vehicle, equity_feed=equity_feed)
    control_adequacy = paired_control_adequacy(
        rows, baseline, vehicle=vehicle,
        # Qualification's declared trade floor remains the local descriptive
        # floor; the non-negotiable protection here is matched coverage.  A
        # 30-candidate/5-control arm therefore still fails (16.7% coverage),
        # while compact legacy diagnostic windows remain verifiable.
        min_matched=max(1, int(min_trades)),
        min_coverage=ACTUAL_CONTROL_MIN_COVERAGE,
        equity_feed=equity_feed)
    absolute = performance_floor(
        rows, vehicle=vehicle, equity_feed=equity_feed)
    delta = (sum(pairs["deltas"]) / pairs["matched"]) if pairs["matched"] else None
    clusters = len({str(row.get("cluster") or _session_key(row))
                    for row in rows if row.get("vehicle", vehicle) == vehicle})
    resolved_block_length = (min(SERIAL_BLOCK_LENGTH, max(1, clusters))
                             if block_length is None else int(block_length))
    delta_bound = moving_block_cluster_bootstrap_lower_bound(
        pairs["deltas"], pairs["clusters"], confidence=confidence,
        draws=int(draws), seed=seed, block_length=resolved_block_length,
        min_clusters=max(2, int(min_clusters)))
    delta_lcb = delta_bound.get("lower_bound")
    observed_drawdown = max_drawdown_of(rows)
    confidence_supported = bool(delta_lcb is not None and float(delta_lcb) > 0)
    drawdown_supported = bool(math.isfinite(float(observed_drawdown)))
    drawdown_within_limit = bool(
        drawdown_supported and max_drawdown is not None and
        float(observed_drawdown) <= max_drawdown)
    candidate_sessions = {
        str(row.get("session_date") or "") for row in rows}
    baseline_sessions = {
        str(row.get("session_date") or "") for row in baseline}
    adequate = bool(
        absolute["trades"] >= int(min_trades) and
        len(candidate_sessions) >= int(min_sessions) and
        clusters >= int(min_clusters) and
        control_adequacy["adequate"] and
        drawdown_within_limit)
    # The final window is intentionally outside the run's fit/held-out trades.
    # Carry the source observations and their independent digests in the
    # signed envelope so a proof can be re-verified after it leaves memory.
    # This also makes tampering detectable even when the summary numbers are
    # changed consistently with one another.
    candidate_observations = [dict(row) for row in rows]
    baseline_observations = [dict(row) for row in baseline]
    if len(candidate_observations) + len(baseline_observations) > QUALIFICATION_MAX_ROWS:
        raise ValueError("qualification observations exceed row limit")
    # ``sessions`` is supplied by the replay window and can include a
    # no-trade/refused opportunity.  Such rows are diagnostic only, so the
    # authorizing observations may cover a strict subset of the declaration.
    if (not candidate_sessions.issubset(declared_set) or
            not baseline_sessions.issubset(declared_set) or
            "" in candidate_sessions or "" in baseline_sessions):
        raise ValueError(
            "qualification observation sessions do not match declared sessions")
    serialized = json.dumps(
        {"candidate": candidate_observations, "baseline": baseline_observations},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=str).encode("utf-8")
    if len(serialized) > QUALIFICATION_MAX_BYTES:
        raise ValueError("qualification observations exceed serialized byte limit")
    candidate_digest = _content_hash(candidate_observations)
    baseline_digest = _content_hash(baseline_observations)
    effective_sessions = tuple(sorted(candidate_sessions))
    return {"available": True, "sessions": list(effective_sessions),
            "net_pnl": absolute["net_pnl"], "trades": absolute["trades"],
            "matched": pairs["matched"], "mean_delta": delta,
            "control_adequacy": control_adequacy,
            "r_matched": pairs.get("r_matched", 0),
            "mean_r_delta": (sum(pairs.get("r_deltas", ())) /
                              pairs["r_matched"]
                              if pairs.get("r_matched") else None),
            "net_positive": bool(absolute["net_pnl_positive"]),
            "delta_positive": bool(delta is not None and delta > 0),
            "clusters": clusters,
            "minimums": {"trades": int(min_trades), "sessions": int(min_sessions),
                          "clusters": int(min_clusters)},
            "adequate": adequate,
            "confidence": confidence,
            "delta_lcb": delta_lcb,
            "confidence_supported": confidence_supported,
            "delta_bootstrap": delta_bound,
            "max_drawdown": observed_drawdown,
            "max_drawdown_limit": max_drawdown,
            "max_drawdown_limit_source": drawdown_limit_source,
            "max_drawdown_limit_r": (max_drawdown_r
                                     if drawdown_limit_source ==
                                     "median_risk_usd_times_r" else None),
            "drawdown_supported": drawdown_supported,
            "drawdown_within_limit": drawdown_within_limit,
            "drawdown_limit_required": True,
            "candidate_observations": candidate_observations,
            "baseline_observations": baseline_observations,
            "candidate_observation_digest": candidate_digest,
            "baseline_observation_digest": baseline_digest,
            # Short aliases make the binding explicit to downstream proof
            # consumers while retaining the descriptive field names above.
            "candidate_digest": candidate_digest,
            "baseline_digest": baseline_digest,
            "observation_schema": "qualification-observations.v1",
            "raw_candidate_observations": raw_rows,
            "raw_baseline_observations": raw_baseline,
            "authorization_projection": {
                "candidate": _projection_summary(candidate_projection),
                "baseline": _projection_summary(baseline_projection),
            },
            "post_selection": {"preselected": bool(preselected),
                                "candidate_id": (str(candidate_id)
                                                 if candidate_id is not None else None)}}


@dataclass
class SealedQualificationWindow:
    """Final-qualification data that may be released exactly once.

    Selection, mutation and diagnosis are given the development payload only.
    This object owns the remaining sessions and refuses to be copied,
    serialized, or shipped to a worker process, so the qualification window
    cannot be consumed by accident even if a caller forgets the convention.
    """

    session_dates: tuple[str, ...]
    digest: str
    reason: str = ""
    released: bool = False
    _payload: Any = field(default=None, repr=False)

    def release(self, *, reason: str) -> Any:
        if self.released:
            raise SealedWindowError(
                "the final qualification window has already been consumed")
        if not str(reason).strip():
            raise SealedWindowError("releasing a sealed window requires a reason")
        self.released = True
        self.reason = str(reason)
        payload, self._payload = self._payload, None
        return payload

    def __getstate__(self):
        raise SealedWindowError(
            "a sealed qualification window cannot be serialized or sent to a worker")

    def __deepcopy__(self, memo):
        raise SealedWindowError("a sealed qualification window cannot be copied")

    def __repr__(self) -> str:
        return (f"SealedQualificationWindow(sessions={len(self.session_dates)}, "
                f"digest={self.digest[:12]!r}, released={self.released})")


def seal_final_window(items: Sequence[Any], *, session_of, fraction: float = .2,
                      min_sessions: int = 1) -> tuple[list, SealedQualificationWindow]:
    """Split off the latest sessions into a sealed final-qualification window."""
    if not 0 < float(fraction) < 1:
        raise ValueError("fraction must be between zero and one")
    ordered = list(items)
    sessions = sorted({str(session_of(item)) for item in ordered})
    reserved = max(int(min_sessions), int(len(sessions) * float(fraction)))
    if len(sessions) <= reserved:
        # Not enough history to seal anything without emptying development.
        return ordered, SealedQualificationWindow((), _content_hash([]))
    sealed_sessions = set(sessions[len(sessions) - reserved:])
    development = [item for item in ordered
                   if str(session_of(item)) not in sealed_sessions]
    qualification = [item for item in ordered
                     if str(session_of(item)) in sealed_sessions]
    return development, SealedQualificationWindow(
        tuple(sorted(sealed_sessions)),
        _content_hash(sorted(sealed_sessions)),
        _payload=qualification)


def fill_source_summary(rows: Iterable[Mapping], *, vehicle: str,
                        equity_feed: str = "iex") -> dict:
    """Report what actually priced the fills behind a result.

    ``entry_fill_source`` is recorded per row but was never aggregated, so a
    finished proof could not say whether it rested on recorded executable
    quotes or on bar prints marked up by the modelled half-spread.  Those are
    materially different evidence and a proof has to state which it is.

    ``dominant_reject_reason`` names why unpriced opportunities were refused.
    When a corpus prices *nothing*, that reason is the difference between "no
    edge here" and "this corpus cannot be evaluated at all".
    """
    equity_feed = str(equity_feed or "").strip().lower().replace("-", "_")
    if equity_feed == "delayed":
        equity_feed = "delayed_sip"
    if equity_feed not in {"iex", "sip", "delayed_sip"}:
        raise ValueError("equity_feed must be iex, sip, or delayed_sip")
    local = [row for row in rows if row.get("vehicle", vehicle) == vehicle]
    executed = [row for row in local if row.get("no_trade") is not True]
    no_trade = [row for row in local if row.get("no_trade") is True]
    no_signal = [row for row in no_trade
                 if row.get("execution_disposition") == "no_signal"]
    refused = [row for row in no_trade
               if (row.get("execution_disposition") == "refused" or
                   (not row.get("execution_disposition") and
                    row.get("reject_reason")))]
    # Legacy no-trade rows with neither a disposition nor a reason remain
    # fail-closed. New factory rows never enter this bucket because the replay
    # contract requires an explicit terminal disposition.
    unclassified = [row for row in no_trade
                    if (row.get("execution_disposition") not in {
                            "no_signal", "refused"} and
                        not row.get("reject_reason"))]
    execution_opportunities = len(executed) + len(refused) + len(unclassified)
    sources: dict[str, int] = {}
    for row in executed:
        name = str(row.get("entry_fill_source") or "unknown")
        sources[name] = sources.get(name, 0) + 1
    rejects: dict[str, int] = {}
    for row in local:
        if row.get("no_trade") is not True:
            continue
        reason = row.get("reject_reason")
        if reason:
            rejects[str(reason)] = rejects.get(str(reason), 0) + 1
    dominant = max(rejects.items(), key=lambda item: (item[1], item[0]))[0] if rejects else None
    quoted = sources.get(QUOTE_FILL, 0)
    # A proof can only authorize on executable, point-in-time pricing.  Bar
    # fallback remains useful for explicitly labelled diagnostic backtests,
    # but it is never adequate for a passing envelope.
    if vehicle == "equity":
        def _equity_quote_leg(row: Mapping, leg: str) -> bool:
            source = str(row.get(f"{leg}_fill_source") or "").strip().lower()
            feed = str(row.get(f"{leg}_feed") or "").strip().lower()
            provider = str(row.get(f"{leg}_provider") or "").strip()
            age = row.get(f"{leg}_quote_age_seconds")
            return (source == QUOTE_FILL and feed == equity_feed and bool(provider) and
                    isinstance(age, (int, float)) and not isinstance(age, bool) and
                    math.isfinite(float(age)) and
                    0 <= float(age) <= OPTION_MAX_QUOTE_AGE_SECONDS)

        # Equity evidence is bound to the envelope's explicit feed identity.
        # A resting bracket exit is the one deliberate non-quote exception:
        # its entry remains a fresh executable quote, while the exit claim is
        # validated against the exact-feed bar and is charged full modeled
        # adverse cost by the replay lanes.
        def _equity_resting_leg(row: Mapping) -> bool:
            return (str(row.get("entry_fill_source") or "").strip().lower() ==
                    QUOTE_FILL and
                    validate_resting_bracket_fill(
                        row, equity_feed=equity_feed) is None)

        quality_adequate = bool(
            executed and all(
                _equity_resting_leg(row) if str(
                    row.get("exit_fill_source") or "").strip().lower() ==
                RESTING_BRACKET else all(_equity_quote_leg(row, leg)
                                         for leg in ("entry", "exit"))
                for row in executed))
    else:
        # Options are priced from snapshots rather than the equity quote
        # source field.  Require finite ages and explicit OPRA provenance on
        # both legs; malformed/legacy or indicative rows remain diagnostic.
        quality_adequate = bool(executed and all(
            isinstance(row.get("entry_quote_age_seconds"), (int, float)) and
            math.isfinite(float(row.get("entry_quote_age_seconds"))) and
            0 <= float(row.get("entry_quote_age_seconds")) <=
            OPTION_MAX_QUOTE_AGE_SECONDS and
            isinstance(row.get("exit_quote_age_seconds"), (int, float)) and
            math.isfinite(float(row.get("exit_quote_age_seconds"))) and
            0 <= float(row.get("exit_quote_age_seconds")) <=
            OPTION_MAX_QUOTE_AGE_SECONDS and
            str(row.get("entry_feed", row.get("entry_option_feed")) or "").strip().lower() == "opra" and
            str(row.get("exit_feed", row.get("exit_option_feed")) or "").strip().lower() == "opra" and
            str(row.get("entry_fill_source") or "").strip().lower() == QUOTE_FILL and
            str(row.get("exit_fill_source") or "").strip().lower() == QUOTE_FILL
            for row in executed))
    return {
        "vehicle": vehicle,
        "opportunities": len(local),
        "execution_opportunities": execution_opportunities,
        "no_signal": len(no_signal),
        "refused": len(refused),
        "unclassified_refusals": len(unclassified),
        "executed": len(executed),
        "sources": dict(sorted(sources.items())),
        "quoted_fraction": (quoted / len(executed)) if executed else None,
        "reject_reasons": dict(sorted(rejects.items())),
        "dominant_reject_reason": dominant,
        # Nothing priced, and every refusal shares one cause: this is a
        # data-shape mismatch, not an edgeless corpus.
        "priced_nothing": bool(execution_opportunities and not executed),
        "adequate": quality_adequate,
    }


ARM_EVIDENCE_SCHEMA = "gate-arm-evidence.v1"


def _numeric_summary(values: Sequence[float], *, missing: int = 0) -> dict:
    """Return a bounded, deterministic summary for quote ages or money.

    Quote provenance is optional on diagnostic rows.  Missing values are
    counted explicitly instead of being converted to zero, which would make a
    sparse quote corpus look as fresh as a dense one.
    """
    clean = [float(value) for value in values
             if isinstance(value, (int, float)) and not isinstance(value, bool)
             and math.isfinite(float(value))]
    result = {
        "count": len(clean),
        "missing": int(missing),
        "min": min(clean) if clean else None,
        "max": max(clean) if clean else None,
        "mean": (sum(clean) / len(clean)) if clean else None,
    }
    if clean:
        ordered = sorted(clean)
        result["median"] = ordered[len(ordered) // 2]
    else:
        result["median"] = None
    return result


def _arm_key_index(
        rows: Iterable[Mapping], *, vehicle: str,
        ) -> tuple[dict[str, Mapping], list[str]]:
    """Index authorizing rows by the same key used by paired inference."""
    values: dict[str, Mapping] = {}
    duplicates: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or row.get("vehicle", vehicle) != vehicle:
            continue
        key = _match_key(row, vehicle)
        if not key:
            continue
        if key in values:
            duplicates.add(key)
        else:
            values[key] = row
    for key in duplicates:
        values.pop(key, None)
    return values, sorted(duplicates)


def _arm_pairing(candidate: Mapping[str, Any], other: Mapping[str, Any], *,
                 candidate_name: str, other_name: str) -> dict:
    left = set(candidate.get("_unique_match_keys", ()))
    right = set(other.get("_unique_match_keys", ()))
    matched = sorted(left & right)
    dropped_candidate = sorted(left - right)
    dropped_other = sorted(right - left)
    candidate_count = len(left)
    other_count = len(right)
    matched_count = len(matched)
    # ``paired_coverage`` is the fraction of the smaller available arm that
    # can actually be paired.  The directional ratios make an asymmetric
    # sparse/null arm visible without treating missing rows as zero P&L.
    smaller = min(candidate_count, other_count)
    paired = (matched_count / smaller) if smaller else 0.0
    candidate_ratio = (matched_count / candidate_count) if candidate_count else 0.0
    other_ratio = (matched_count / other_count) if other_count else 0.0
    if not candidate_count and not other_count:
        reason = "no_eligible_rows"
    elif not candidate_count or not other_count:
        reason = "one_arm_has_no_eligible_rows"
    elif not matched_count:
        reason = "no_matched_match_keys"
    elif matched_count == candidate_count == other_count:
        reason = "full_pair_coverage"
    else:
        reason = "partial_pair_coverage"
    return {
        "candidate_arm": candidate_name,
        "other_arm": other_name,
        "matched": matched_count,
        "matched_match_keys": matched,
        "matched_keys": matched,
        "dropped_match_keys": {
            candidate_name: dropped_candidate,
            other_name: dropped_other,
        },
        "dropped_keys": {
            candidate_name: dropped_candidate,
            other_name: dropped_other,
        },
        "candidate_coverage": candidate_ratio,
        "other_coverage": other_ratio,
        "paired_coverage": paired,
        "coverage_ratio": paired,
        # This is diagnostic evidence only.  It mirrors the existing paired
        # control invariant (at least one matched observation) and is not a
        # new arbitrary promotion floor.
        "adequate": bool(matched_count),
        "adequacy_reason": reason,
    }


def arm_evidence_report(*, candidate: Iterable[Mapping],
                        baseline: Iterable[Mapping] = (),
                        null: Iterable[Mapping] = (), vehicle: str,
                        projections: Mapping[str, Mapping[str, Any]] | None = None,
                        equity_feed: str = "iex") -> dict:
    """Persist explainable evidence diagnostics for candidate/control arms.

    ``authorization_projection`` remains the authorizing boundary.  This
    report deliberately carries both its raw and eligible views, plus the
    refused reasons and pair-key coverage needed to explain a sparse quote
    corpus.  It is safe for failed/underpowered reports and never authorizes a
    proof by itself.
    """
    if vehicle not in {"equity", "option"}:
        raise ValueError("vehicle must be equity or option")
    raw_arms = {
        "candidate": [dict(row) for row in candidate if isinstance(row, Mapping)],
        "baseline": [dict(row) for row in baseline if isinstance(row, Mapping)],
        "null": [dict(row) for row in null if isinstance(row, Mapping)],
    }
    projections = dict(projections or {})
    arms: dict[str, dict[str, Any]] = {}
    for name, raw in raw_arms.items():
        projection = projections.get(name)
        if not isinstance(projection, Mapping):
            strict = any(_has_fill_metadata(row) for row in raw)
            projection = authorization_projection(
                raw, vehicle=vehicle, strict=strict,
                equity_feed=equity_feed)
        eligible = [dict(row) for row in projection.get("eligible", ())
                    if isinstance(row, Mapping)]
        executed_raw = [row for row in raw if row.get("no_trade") is not True]
        executed_eligible = [row for row in eligible if row.get("no_trade") is not True]
        pairs: dict[str, int] = {}
        for row in executed_raw:
            entry = str(row.get("entry_fill_source") or "unknown").strip().lower()
            exit_ = str(row.get("exit_fill_source") or "unknown").strip().lower()
            key = f"{entry}->{exit_}"
            pairs[key] = pairs.get(key, 0) + 1
        age_summaries: dict[str, dict] = {}
        for leg in ("entry", "exit"):
            ages = []
            missing = 0
            for row in executed_raw:
                value = row.get(f"{leg}_quote_age_seconds")
                if (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(float(value))):
                    ages.append(float(value))
                else:
                    missing += 1
            age_summaries[leg] = _numeric_summary(ages, missing=missing)
        def total(rows: Sequence[Mapping], name: str) -> float:
            return sum(float(row.get(name, 0.0) or 0.0) for row in rows
                       if isinstance(row.get(name, 0.0), (int, float)) and
                       not isinstance(row.get(name, 0.0), bool) and
                       math.isfinite(float(row.get(name, 0.0) or 0.0)))
        unique, duplicates = _arm_key_index(executed_eligible, vehicle=vehicle)
        reasons = dict(sorted((str(key), int(value)) for key, value in
                              (projection.get("reasons") or {}).items()))
        reject_reasons: dict[str, int] = {}
        for row in raw:
            if row.get("no_trade") is True and row.get("reject_reason"):
                key = str(row["reject_reason"])
                reject_reasons[key] = reject_reasons.get(key, 0) + 1
        arms[name] = {
            "schema": ARM_EVIDENCE_SCHEMA,
            "vehicle": vehicle,
            "counts": {"raw": len(raw), "executed": len(executed_raw),
                        "eligible": len(eligible),
                        "eligible_executed": len(executed_eligible),
                        "excluded": len(projection.get("excluded", ()))},
            "excluded_reasons": reasons,
            "reject_reasons": dict(sorted(reject_reasons.items())),
            "fill_source_pairs": dict(sorted(pairs.items())),
            "quote_age_seconds": age_summaries,
            # ``totals`` describes every executed replay row, including
            # diagnostic-only bar/delayed fills.  The explicit eligible totals
            # keep authorizing economics separate for downstream consumers.
            "totals": {name: total(executed_raw, name)
                       for name in ("gross_pnl", "costs", "net_pnl")},
            "eligible_totals": {name: total(executed_eligible, name)
                                for name in ("gross_pnl", "costs", "net_pnl")},
            "gross_pnl": total(executed_raw, "gross_pnl"),
            "costs": total(executed_raw, "costs"),
            "net_pnl": total(executed_raw, "net_pnl"),
            "eligible_gross_pnl": total(executed_eligible, "gross_pnl"),
            "eligible_costs": total(executed_eligible, "costs"),
            "eligible_net_pnl": total(executed_eligible, "net_pnl"),
            "match_keys": sorted(unique),
            "duplicate_match_keys": duplicates,
            "_unique_match_keys": sorted(unique),
        }
    pairing = {
        "candidate_vs_baseline": _arm_pairing(
            arms["candidate"], arms["baseline"],
            candidate_name="candidate", other_name="baseline"),
        "candidate_vs_null": _arm_pairing(
            arms["candidate"], arms["null"],
            candidate_name="candidate", other_name="null"),
    }
    # The private index is an implementation detail; all persisted key sets
    # remain explicit in ``match_keys`` and the pair reports.
    for arm in arms.values():
        arm.pop("_unique_match_keys", None)
    return {"schema": ARM_EVIDENCE_SCHEMA, "vehicle": vehicle,
            "arms": arms, "pairing": pairing}


def gate_dependence_report(envelope: Mapping | None = None, *,
                           gate: Mapping | None = None,
                           checks: Mapping | None = None) -> dict:
    """Report shared source statistics behind gate booleans.

    This is a policy-neutral explanation of *what was reused* by the gate,
    not a new independence test.  A verified envelope (or a containing gate
    record) is accepted; each check is mapped to the source paths that produce
    its statistic, and paths used by multiple checks are grouped explicitly.
    Missing paths remain visible with ``None`` values so a sparse diagnostic
    envelope cannot be mistaken for a complete authorizing explanation.
    """
    candidate = envelope if isinstance(envelope, Mapping) else gate
    if isinstance(candidate, Mapping) and isinstance(candidate.get("verified_gate"), Mapping):
        candidate = candidate["verified_gate"]
    source = dict(candidate or {}) if isinstance(candidate, Mapping) else {}
    reported_checks = checks if isinstance(checks, Mapping) else source.get("checks")
    if not isinstance(reported_checks, Mapping):
        reported_checks = source.get("checks_without_family")
    if not isinstance(reported_checks, Mapping):
        reported_checks = {}

    def lookup(path: str):
        value: Any = source
        for part in path.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return None
            value = value[part]
        return value

    check_dependencies: dict[str, dict[str, Any]] = {}
    statistic_usage: dict[str, dict[str, Any]] = {}
    source_usage: dict[str, list[str]] = {}
    for name in sorted(str(key) for key in reported_checks):
        paths = tuple(_GATE_SOURCE_STATISTICS.get(name, ()))
        if (name == "falsification" and
                lookup("falsification.p_value_source") !=
                FALSIFICATION_P_VALUE_SOURCE and
                lookup("falsification.independent_supplied") is not True):
            paths = _LEGACY_FALSIFICATION_SOURCE_STATISTICS
        elif name == "falsification" and lookup(
                "falsification.independent_supplied") is True:
            paths = ("falsification.independent_p_value",
                     "falsification.independent_method",
                     "falsification.independent_result_hash",
                     "falsification.alpha")
        if not paths:
            # Unknown/additional checks are still represented, but are not
            # assigned an invented statistic or policy meaning.
            check_dependencies[name] = {
                "decision": bool(reported_checks[name]),
                "source_statistics": [], "known": False,
            }
            continue
        details = {
            "decision": bool(reported_checks[name]),
            "source_statistics": list(paths), "known": True,
            "values": {path: lookup(path) for path in paths},
        }
        check_dependencies[name] = details
        for path in paths:
            item = statistic_usage.setdefault(path, {
                "checks": [], "value": lookup(path),
            })
            item["checks"].append(name)
            root = path.split(".", 1)[0]
            source_usage.setdefault(root, []).append(name)
    for item in statistic_usage.values():
        item["checks"] = sorted(set(item["checks"]))
    for names in source_usage.values():
        names[:] = sorted(set(names))
    shared = {
        path: item for path, item in sorted(statistic_usage.items())
        if len(item["checks"]) > 1
    }
    source_counts = {}
    for name in ("fit_source", "heldout_source", "fit_baseline_source",
                 "heldout_baseline_source", "null_source"):
        rows = source.get(name)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            source_counts[name] = len(rows)
    return {
        "schema": "gate-dependence-report.v1",
        "available": bool(check_dependencies),
        "policy_neutral": True,
        "checks": check_dependencies,
        "check_dependencies": check_dependencies,
        "source_statistics": dict(sorted(statistic_usage.items())),
        "source_statistic_dependencies": dict(sorted(statistic_usage.items())),
        "shared_source_statistics": shared,
        "shared_statistics": shared,
        "dependent_checks": [item["checks"] for item in shared.values()],
        "dependence_pairs": [item["checks"] for item in shared.values()],
        "source_usage": dict(sorted(source_usage.items())),
        "source_counts": source_counts,
        "known_checks": sum(1 for item in check_dependencies.values()
                             if item.get("known")),
        "unknown_checks": sum(1 for item in check_dependencies.values()
                               if not item.get("known")),
    }


# Names used by report consumers that focus on source/statistic provenance.
gate_source_statistic_report = gate_dependence_report
source_statistic_report = gate_dependence_report
gate_source_dependence_report = gate_dependence_report
source_statistic_dependence_report = gate_dependence_report


def _fill_quality_adequate(fit: Sequence[Mapping], heldout: Sequence[Mapping],
                           *, vehicle: str, lane: str,
                           equity_feed: str = "iex") -> bool:
    partitions = [heldout] if lane == "shadow" else [fit, heldout]
    summaries = [fill_source_summary(
        rows, vehicle=vehicle, equity_feed=equity_feed)
        for rows in partitions]
    # An empty fit partition is normal for shadow; an empty held-out partition
    # is not evidence.  Every partition with opportunities must be executable
    # quote/snapshot evidence, never bar-only fallback.
    return bool(summaries and all(
        summary.get("adequate") is True for summary in summaries))


def unevaluable_reason(gates: Iterable[Mapping]) -> str | None:
    """Why a run tested nothing, when that is a data problem not a result.

    A cycle where every lane materialised opportunities and executed no trades
    has not evaluated a single hypothesis.  Reported as "nothing passed the
    gates" it is indistinguishable from a genuine negative, so a corpus the
    replay cannot price at all -- bars with no recorded quotes under a strict
    market-data policy, most often a backfill run without ``--quotes`` -- looks
    exactly like weeks of healthy, edgeless research.

    Returns the shared refusal reason when that is what happened, else None.
    """
    reasons: set[str] = set()
    saw_opportunity = False
    for gate in gates:
        if not isinstance(gate, Mapping):
            continue
        quality = gate.get("fill_quality")
        if not isinstance(quality, Mapping):
            nested = gate.get("verified_gate")
            quality = (nested.get("fill_quality")
                       if isinstance(nested, Mapping) else None)
        if not isinstance(quality, Mapping):
            continue
        for partition in ("fit", "heldout"):
            summary = quality.get(partition)
            if not isinstance(summary, Mapping):
                continue
            raw_execution_opportunities = summary.get("execution_opportunities")
            execution_opportunities = (
                int(summary.get("opportunities") or 0)
                if raw_execution_opportunities is None else
                int(raw_execution_opportunities or 0))
            if execution_opportunities <= 0:
                continue
            saw_opportunity = True
            if int(summary.get("executed") or 0) > 0:
                # Something priced, so the corpus is evaluable and any
                # remaining failure is a research verdict.
                return None
            reason = summary.get("dominant_reject_reason")
            if reason:
                reasons.add(str(reason))
    if not saw_opportunity:
        return None
    if not reasons:
        return "every opportunity was refused without a recorded reason"
    return "; ".join(sorted(reasons))


def verified_gate_envelope(*, lane: str, vehicle: str,
                           fit: Sequence[Mapping], heldout: Sequence[Mapping],
                           fit_baseline: Sequence[Mapping] = (),
                           heldout_baseline: Sequence[Mapping] = (),
                           null_source: Sequence[Mapping] = (),
                           fit_raw: Sequence[Mapping] | None = None,
                           heldout_raw: Sequence[Mapping] | None = None,
                           fit_baseline_raw: Sequence[Mapping] | None = None,
                           heldout_baseline_raw: Sequence[Mapping] | None = None,
                           null_raw: Sequence[Mapping] | None = None,
                           fit_floor: Mapping, heldout_floor: Mapping,
                           control: Mapping, p_value: float, q_value: float,
                           alpha: float,
                           falsification: Mapping, separation: Mapping,
                           checks: Mapping[str, bool], passes: bool,
                           performance: Mapping | None = None,
                           family_q_value: float | None = None,
                           cluster_q_value: float | None = None,
                           cluster_multiple_tests: Mapping | None = None,
                           fdr_batch: Mapping | None = None,
                           walk_forward: Mapping | None = None,
                           retirement: Mapping | None = None,
                           qualification: Mapping | None = None,
                           null_control: Mapping | None = None,
                           fit_control: Mapping | None = None,
                           online_fdr: Mapping | None = None,
                           provenance: Mapping | None = None,
                           candidate_id: str | None = None,
                           costs: Any = None,
                           risk_unit: Mapping | None = None,
                           risk_unit_report: Mapping | None = None,
                           equity_feed: str = "iex") -> dict:
    """Build the immutable, content-addressed gate decision persisted per run.

    ``q_value`` is the cycle-global false-discovery q; ``family_q_value`` is
    the family-local one.  Both are persisted so a proof states exactly which
    correction authorized it.
    """
    equity_feed = str(equity_feed or "").strip().lower().replace("-", "_")
    if equity_feed == "delayed":
        equity_feed = "delayed_sip"
    if equity_feed not in {"iex", "sip", "delayed_sip"}:
        raise ValueError("equity_feed must be iex, sip, or delayed_sip")
    # ``fit``/``heldout`` are retained as compatibility inputs for callers
    # that already projected rows.  The raw variants are the audit source;
    # when omitted, the inputs themselves are treated as raw and projected
    # here.  Every arm uses the same strict boundary.
    fit_source_raw = [dict(row) for row in (fit if fit_raw is None else fit_raw)]
    heldout_source_raw = [dict(row) for row in (heldout if heldout_raw is None else heldout_raw)]
    fit_baseline_source_raw = [dict(row) for row in (
        fit_baseline if fit_baseline_raw is None else fit_baseline_raw)]
    heldout_baseline_source_raw = [dict(row) for row in (
        heldout_baseline if heldout_baseline_raw is None else heldout_baseline_raw)]
    null_source_raw = [dict(row) for row in (
        null_source if null_raw is None else null_raw)]
    # Legacy summary-only fixtures have no fill provenance at all.  Keep them
    # auditable and recomputable while production replay rows (which always
    # carry fill metadata) take the strict protocol path.
    all_raw = [*fit_source_raw, *heldout_source_raw,
               *fit_baseline_source_raw, *heldout_baseline_source_raw,
               *null_source_raw]
    strict_projection = any(_has_fill_metadata(row) for row in all_raw)
    projections = {
        "fit": authorization_projection(fit_source_raw, vehicle=vehicle,
                                         strict=strict_projection,
                                         equity_feed=equity_feed),
        "heldout": authorization_projection(heldout_source_raw, vehicle=vehicle,
                                             strict=strict_projection,
                                             equity_feed=equity_feed),
        "fit_baseline": authorization_projection(
            fit_baseline_source_raw, vehicle=vehicle, strict=strict_projection,
            equity_feed=equity_feed),
        "heldout_baseline": authorization_projection(
            heldout_baseline_source_raw, vehicle=vehicle, strict=strict_projection,
            equity_feed=equity_feed),
        "null": authorization_projection(null_source_raw, vehicle=vehicle,
                                          strict=strict_projection,
                                          equity_feed=equity_feed),
    }
    fit = projections["fit"]["eligible"]
    heldout = projections["heldout"]["eligible"]
    fit_baseline = projections["fit_baseline"]["eligible"]
    heldout_baseline = projections["heldout_baseline"]["eligible"]
    null_source = projections["null"]["eligible"]
    # Malformed callers must produce an unverifiable envelope, not an
    # AttributeError that escapes the durable proof boundary.
    falsification = dict(falsification) if isinstance(falsification, Mapping) else {}
    separation = dict(separation) if isinstance(separation, Mapping) else {}
    reported = dict(performance or {}) if isinstance(performance, Mapping) else {}
    reported.setdefault("heldout_delta", control.get("mean_delta"))
    reported.setdefault("heldout_delta_lcb", control.get("mean_delta_lcb"))
    if "heldout_r_delta" not in reported and control.get("mean_r_delta") is not None:
        reported["heldout_r_delta"] = control.get("mean_r_delta")
    reported.setdefault("max_drawdown", max_drawdown_of(heldout))
    derived = {str(key): bool(value) for key, value in checks.items()}
    control = dict(control) if isinstance(control, Mapping) else {}
    control_adequacy = paired_control_adequacy(
        heldout, heldout_baseline, vehicle=vehicle,
        min_matched=ACTUAL_CONTROL_MIN_MATCHED,
        min_coverage=ACTUAL_CONTROL_MIN_COVERAGE,
        equity_feed=equity_feed)
    control["paired_adequacy"] = control_adequacy
    control["coverage"] = control_adequacy["coverage"]
    control["adequate"] = bool(control_adequacy["adequate"])
    derived["actual_control_available"] = bool(
        control.get("actual_control") is True and control.get("available") is True and
        isinstance(control.get("matched"), int) and int(control.get("matched")) > 0 and
        control_adequacy.get("adequate") is True)
    derived["actual_control_adequate"] = bool(control_adequacy.get("adequate"))
    delta = control.get("mean_delta")
    lcb = control.get("mean_delta_lcb")
    derived["heldout_delta_positive"] = bool(delta is not None and float(delta) > 0)
    derived["heldout_delta_lcb_positive"] = bool(lcb is not None and float(lcb) > 0)
    fit_summary = dict(fit_control or {})
    derived["fit_delta_positive"] = bool(
        lane == "shadow" or (fit_summary.get("mean_delta") is not None and
                              float(fit_summary["mean_delta"]) > 0))
    derived["heldout_p_significant"] = float(p_value) <= float(alpha)
    derived["heldout_net_pnl_positive"] = bool(
        float(reported.get("heldout_net_pnl", sum(float(row.get("net_pnl", 0.0))
                                                    for row in heldout))) > 0)
    trades = sample_counts(
        heldout, vehicle=vehicle, equity_feed=equity_feed)["trades"]
    net = float(reported.get("heldout_net_pnl", sum(float(row.get("net_pnl", 0.0))
                                                     for row in heldout)))
    derived["heldout_expectancy_positive"] = bool(trades and net / trades > 0)
    derived["falsification"] = bool((falsification or {}).get("passes") is True)
    if falsification and (heldout_source_raw or heldout_baseline_source_raw):
        # New production proofs must carry method/result provenance distinct
        # from the primary paired p-value.  Legacy summary-only envelopes keep
        # their historical replay semantics.
        p_value_source = str(falsification.get("p_value_source") or "")
        independent_hash = str(
            falsification.get("independent_result_hash") or "")
        primary_hash = str(falsification.get("assignments_hash") or "")
        independent_ok = bool(
            falsification.get("independent_supplied") is True and
            falsification.get("independent_method") ==
            FALSIFICATION_INDEPENDENT_METHOD and
            independent_hash and primary_hash and
            independent_hash != primary_hash and
            p_value_source == FALSIFICATION_P_VALUE_SOURCE)
        derived["falsification"] = bool(derived["falsification"] and independent_ok)
        if not independent_ok:
            # Keep the persisted decision and its check synchronized.  This
            # is a diagnostic veto, not an exception, so legacy evidence can
            # still be replayed and audited without authorizing.
            falsification["passes"] = False
    derived["separated"] = bool((separation or {}).get("passes") is True)
    walk = dict(walk_forward or {})
    derived["walk_forward_available"] = bool(walk.get("available"))
    derived["walk_forward_adequate"] = bool(walk.get("adequate"))
    derived["walk_forward_majority_positive"] = bool(walk.get("majority_positive"))
    null = dict(null_control or {})
    null_min_matched = max(
        NULL_CONTROL_MIN_MATCHED,
        int(null.get("minimum_matched", NULL_CONTROL_MIN_MATCHED)))
    null_min_coverage = max(
        NULL_CONTROL_MIN_COVERAGE,
        float(null.get("minimum_coverage", NULL_CONTROL_MIN_COVERAGE)))
    null_adequacy = paired_control_adequacy(
        heldout, null_source, vehicle=vehicle,
        min_matched=null_min_matched,
        min_coverage=null_min_coverage,
        equity_feed=equity_feed)
    # Persisted adequacy is recomputed from source rows; a forged summary flag
    # cannot turn a thin null arm into an authorizing control.
    null_raw_available = bool(
        null.get("raw_available",
                 null.get("available", null.get("actual_control", False))))
    null["paired_adequacy"] = null_adequacy
    null["raw_available"] = null_raw_available
    null["available"] = bool(null_raw_available and null_adequacy["adequate"])
    null["coverage"] = null_adequacy["coverage"]
    null["adequate"] = bool(null_adequacy["adequate"])
    null["minimum_matched"] = null_min_matched
    null["minimum_coverage"] = null_min_coverage
    derived["null_control_available"] = bool(null["available"])
    null_delta = null.get("mean_delta")
    null_p = null.get("p_value")
    derived["null_control_delta_positive"] = bool(
        derived["null_control_available"] and
        null_delta is not None and null_p is not None and
        float(null_delta) > 0 and float(null_p) <= float(alpha))
    # A caller-supplied ``adequate`` flag is not authoritative.  The persisted
    # floor must itself declare the immutable protocol minimum for this lane;
    # otherwise a compact diagnostic report could be promoted by merely
    # changing its summary booleans.
    derived["fit_floor_adequate"] = bool(
        (fit_floor or {}).get("adequate") and
        _floor_minimums_meet(fit_floor, lane=lane))
    derived["heldout_floor_adequate"] = bool(
        (heldout_floor or {}).get("adequate") and
        _floor_minimums_meet(heldout_floor, lane=lane))
    stats = {"q_value": q_value, "family_q_value":
             q_value if family_q_value is None else family_q_value,
             "alpha": alpha}
    if cluster_q_value is not None:
        stats["cluster_q_value"] = float(cluster_q_value)
    derived["family_fdr_significant"] = float(stats["family_q_value"]) <= float(alpha)
    derived["global_fdr_significant"] = float(q_value) <= float(alpha)
    online = dict(online_fdr or {})
    online_required = online.get("required", True) is not False
    online_p = online.get("p_value")
    online_alpha = online.get("allocated_alpha")
    if online_required:
        derived["cumulative_fdr_significant"] = bool(
            online.get("decision") is True and online_p is not None and
            online_alpha is not None and float(online_p) <= float(online_alpha))
    else:
        # Historical and offline-forward proofs are screens, not deployments.
        # They may defer the one cumulative test to the strictly newer,
        # parity-matched live-shadow boundary, but must say so explicitly.
        derived["cumulative_fdr_significant"] = bool(
            online.get("status") == "deferred_to_live_shadow" and
            online.get("tested") is False and online.get("decision") is False)
    qual = dict(qualification or {})
    derived["qualification_available"] = bool(qual.get("available"))
    qual_minimums = qual.get("minimums") if isinstance(qual.get("minimums"), Mapping) else {}
    qualification_minimums_valid = _qualification_minimums_meet(qual_minimums)
    derived["qualification_floor_adequate"] = bool(
        qual.get("available") and qual.get("adequate") and
        qualification_minimums_valid)
    derived["qualification_net_positive"] = bool(
        qual.get("available") and qual.get("adequate") and qual.get("net_positive"))
    derived["qualification_delta_positive"] = bool(
        qual.get("available") and qual.get("adequate") and
        qual.get("delta_positive"))
    derived["qualification_confidence_supported"] = bool(
        qual.get("available") and qual.get("adequate") and
        qual.get("confidence_supported"))
    derived["qualification_drawdown_supported"] = bool(
        qual.get("available") and qual.get("adequate") and
        qual.get("drawdown_supported") and qual.get("drawdown_within_limit", True))
    if qual.get("available") and qual.get("drawdown_limit_required") is True:
        derived["qualification_drawdown_supported"] = bool(
            derived["qualification_drawdown_supported"] and
            isinstance(qual.get("max_drawdown_limit"), (int, float)) and
            not isinstance(qual.get("max_drawdown_limit"), bool) and
            math.isfinite(float(qual.get("max_drawdown_limit"))))
    derived["max_drawdown_supported"] = bool(
        isinstance(reported.get("max_drawdown"), (int, float)) and
        math.isfinite(float(reported.get("max_drawdown"))))
    # Persist the complete risk-unit calculation.  Callers may supply a
    # precomputed report, but verification always rebuilds it from source rows
    # and the model captured in the report.
    supplied_risk = risk_unit_report if risk_unit_report is not None else risk_unit
    if supplied_risk is None:
        try:
            supplied_risk = _risk_unit_report(
                [*fit, *heldout], vehicle=vehicle, costs=costs,
                equity_feed=equity_feed)
        except (CostError, TypeError, ValueError, OverflowError):
            supplied_risk = {"schema": "risk-unit-report.v1", "vehicle": vehicle,
                             "adequate": False, "observations": []}
    risk = dict(supplied_risk) if isinstance(supplied_risk, Mapping) else {}
    derived["risk_unit_adequate"] = bool(risk.get("adequate"))
    derived["fill_quality_adequate"] = _fill_quality_adequate(
        fit, heldout, vehicle=vehicle, lane=str(lane),
        equity_feed=equity_feed)
    try:
        stress = cost_stress_report(
            [*fit, *heldout], vehicle=vehicle, risk_report=risk,
            equity_feed=equity_feed)
    except (CostError, TypeError, ValueError, OverflowError):
        stress = {"schema": "cost-stress-report.v1", "vehicle": vehicle,
                  "stress_basis_schema": STRESSED_COST_SCHEMA,
                  "stress_basis": dict(STRESSED_COST_BASIS),
                  "required_entry_notional_bps": COST_STRESS_REQUIRED_BPS,
                  "required_round_trip_bps": COST_STRESS_REQUIRED_BPS,
                  "scenarios": [], "adequate": False}
    derived["cost_stress_adequate"] = bool(stress.get("adequate"))
    try:
        breadth = matched_effective_breadth(
            heldout, heldout_baseline, vehicle=vehicle,
            equity_feed=equity_feed)
    except (TypeError, ValueError, OverflowError):
        breadth = {
            "method": "symmetric_correlation_eigenvalue_participation_ratio",
            "available": False, "effective_breadth": None,
            "reason": "invalid_matched_breadth_evidence",
        }
    fit_sessions = {str(row.get("session_date") or "") for row in fit_source_raw}
    heldout_sessions = {str(row.get("session_date") or "") for row in heldout_source_raw}
    fit_null_raw = [row for row in null_source_raw
                    if str(row.get("session_date") or "") in fit_sessions]
    heldout_null_raw = [row for row in null_source_raw
                        if str(row.get("session_date") or "") in heldout_sessions]
    # A null arm is generated for the held-out comparison in current factory
    # lanes.  Keep the fit projection empty unless it explicitly contains fit
    # session keys, so its counts cannot imply evidence that was never replayed.
    fit_null_projection = (authorization_projection(
        fit_null_raw, vehicle=vehicle, strict=strict_projection,
        equity_feed=equity_feed)
        if fit_null_raw else {"eligible": [], "excluded": [], "reasons": {}})
    heldout_null_projection = (authorization_projection(
        heldout_null_raw, vehicle=vehicle, strict=strict_projection,
        equity_feed=equity_feed)
        if heldout_null_raw else {"eligible": [], "excluded": [], "reasons": {}})
    arm_diagnostics = {
        "fit": arm_evidence_report(
            candidate=fit_source_raw, baseline=fit_baseline_source_raw,
            null=fit_null_raw, vehicle=vehicle, equity_feed=equity_feed,
            projections={"candidate": projections["fit"],
                         "baseline": projections["fit_baseline"],
                         "null": fit_null_projection}),
        "heldout": arm_evidence_report(
            candidate=heldout_source_raw, baseline=heldout_baseline_source_raw,
            null=heldout_null_raw, vehicle=vehicle, equity_feed=equity_feed,
            projections={"candidate": projections["heldout"],
                         "baseline": projections["heldout_baseline"],
                         "null": heldout_null_projection}),
        "all": arm_evidence_report(
            candidate=[*fit_source_raw, *heldout_source_raw],
            baseline=[*fit_baseline_source_raw, *heldout_baseline_source_raw],
            null=null_source_raw, vehicle=vehicle, equity_feed=equity_feed,
            projections={
                "candidate": authorization_projection(
                    [*fit_source_raw, *heldout_source_raw],
                    vehicle=vehicle, strict=strict_projection,
                    equity_feed=equity_feed),
                "baseline": authorization_projection(
                    [*fit_baseline_source_raw, *heldout_baseline_source_raw],
                    vehicle=vehicle, strict=strict_projection,
                    equity_feed=equity_feed),
                "null": projections["null"],
            }),
    }
    statistics = {
        "p_value": float(p_value), "q_value": float(q_value),
        "family_q_value": float(q_value if family_q_value is None
                                 else family_q_value),
        **({"cluster_q_value": float(cluster_q_value)}
           if cluster_q_value is not None else {}),
        "alpha": float(alpha),
    }
    batch = dict(fdr_batch) if isinstance(fdr_batch, Mapping) else {}
    derived["multiple_testing_batch_bound"] = _fdr_batch_matches(
        batch, statistics=statistics, checks=derived,
        provenance=(provenance if isinstance(provenance, Mapping) else {}),
        candidate_id=candidate_id,
        cluster_multiple_tests=(cluster_multiple_tests
                                if isinstance(cluster_multiple_tests, Mapping)
                                else None))
    # Required checks are the minimum schema, not a licence to ignore an
    # additional veto supplied by a caller.  A passing envelope must agree
    # with every persisted boolean decision, exactly as durable proof
    # verification does.
    effective_passes = bool(
        passes and
        # Equity proofs may authorize either configured real-time feed.  The
        # row projections above enforce exact entry/exit feed parity; delayed
        # SIP remains diagnostic-only and therefore cannot pass this boundary.
        (vehicle != "equity" or equity_feed in {"iex", "sip"}) and
        all(derived.get(key, False) for key in GATE_REQUIRED_CHECKS) and
        all(derived.values())
    )
    body: dict[str, Any] = {
        "schema": GATE_ENVELOPE_SCHEMA,
        "lane": str(lane),
        "vehicle": str(vehicle),
        "equity_feed": equity_feed,
        "counts": {
            "fit": sample_counts(fit, vehicle=vehicle, equity_feed=equity_feed),
            "heldout": sample_counts(
                heldout, vehicle=vehicle, equity_feed=equity_feed),
            "total": sample_counts(
                [*fit, *heldout], vehicle=vehicle, equity_feed=equity_feed),
        },
        # Source rows make critical conclusions independently recomputable
        # after persistence; callers must not ship a summary-only pass.
        "fit_source": fit_source_raw,
        "heldout_source": heldout_source_raw,
        "fit_baseline_source": fit_baseline_source_raw,
        "heldout_baseline_source": heldout_baseline_source_raw,
        "null_source": null_source_raw,
        "floors": {"fit": dict(fit_floor), "heldout": dict(heldout_floor)},
        # What priced these fills, so a persisted proof states its own
        # evidence quality instead of leaving it unauditable.
        "fill_quality": {
            "fit": fill_source_summary(
                fit_source_raw, vehicle=vehicle, equity_feed=equity_feed),
            "heldout": fill_source_summary(
                heldout_source_raw, vehicle=vehicle,
                equity_feed=equity_feed),
        },
        "authorization_projection": {
            name: _projection_summary(value) for name, value in projections.items()
        },
        "arm_diagnostics": arm_diagnostics,
        "fit_control": fit_summary,
        "control": dict(control),
        "statistics": statistics,
        "fdr_batch": batch,
        "cluster_multiple_tests": (dict(cluster_multiple_tests)
                                    if isinstance(cluster_multiple_tests, Mapping)
                                    else {}),
        "performance": reported,
        "falsification": dict(falsification),
        "separation": dict(separation),
        "walk_forward": dict(walk_forward or {}),
        "retirement": dict(retirement or {}),
        "qualification": dict(qualification or {}),
        "risk_unit_report": risk,
        "cost_stress": stress,
        "effective_breadth": breadth,
        "null_control": dict(null),
        "online_fdr": online,
        "provenance": dict(provenance or {}),
        "candidate_id": (str(candidate_id) if candidate_id is not None else None),
        "checks": derived,
        "passes": effective_passes,
    }
    return {**body, "content_hash": _content_hash(body)}


def verify_gate_envelope(envelope: Mapping) -> bool:
    try:
        if not isinstance(envelope, Mapping):
            return False
        schema = envelope.get("schema")
        if schema not in SUPPORTED_GATE_ENVELOPE_SCHEMAS:
            return False
        legacy_v2 = schema == LEGACY_GATE_ENVELOPE_SCHEMA_V2
        required_checks = (LEGACY_GATE_REQUIRED_CHECKS_V2
                           if legacy_v2 else GATE_REQUIRED_CHECKS)
        has_equity_feed = "equity_feed" in envelope
        # Envelopes created before feed binding used SIP.  Rebuild those exact
        # historical semantics; never reinterpret an omitted field as IEX.
        equity_feed = str(
            envelope.get("equity_feed") if has_equity_feed else "sip"
        ).strip().lower().replace("-", "_")
        if equity_feed == "delayed":
            equity_feed = "delayed_sip"
        if equity_feed not in {"iex", "sip", "delayed_sip"}:
            return False
        qualification = envelope.get("qualification")
        if not isinstance(qualification, Mapping):
            return False
        if qualification.get("available"):
            candidate = qualification.get("candidate_observations")
            baseline = qualification.get("baseline_observations")
            if (not isinstance(candidate, Sequence) or isinstance(candidate, (str, bytes)) or
                    not isinstance(baseline, Sequence) or isinstance(baseline, (str, bytes)) or
                    not all(isinstance(row, Mapping) for row in candidate) or
                    not all(isinstance(row, Mapping) for row in baseline) or
                    qualification.get("observation_schema") != "qualification-observations.v1" or
                    qualification.get("candidate_observation_digest") != _content_hash(candidate) or
                    qualification.get("baseline_observation_digest") != _content_hash(baseline) or
                    qualification.get("candidate_digest") != qualification.get(
                        "candidate_observation_digest") or
                    qualification.get("baseline_digest") != qualification.get(
                        "baseline_observation_digest")):
                return False
            if (len(candidate) + len(baseline) > QUALIFICATION_MAX_ROWS or
                    len(json.dumps(
                        {"candidate": candidate, "baseline": baseline},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                        allow_nan=False, default=str).encode("utf-8")) >
                    QUALIFICATION_MAX_BYTES):
                return False
            # Rebuild from the raw sealed observations when present.  The
            # authorizing observations above are the strict projection of
            # those rows; feeding them back as raw input would silently drop
            # refused/no-trade rows and make the persisted projection summary
            # and raw audit payload unverifiable.  Older envelopes may not
            # carry raw observations, in which case the projected observations
            # remain the only available source and preserve compatibility.
            raw_candidate = qualification.get("raw_candidate_observations", candidate)
            raw_baseline = qualification.get("raw_baseline_observations", baseline)
            if (not isinstance(raw_candidate, Sequence) or
                    isinstance(raw_candidate, (str, bytes)) or
                    not isinstance(raw_baseline, Sequence) or
                    isinstance(raw_baseline, (str, bytes)) or
                    not all(isinstance(row, Mapping)
                            for row in [*raw_candidate, *raw_baseline])):
                return False
            expected = qualification_report(
                raw_candidate, raw_baseline, vehicle=str(envelope.get("vehicle")),
                sessions=qualification.get("sessions") or (),
                candidate_id=((qualification.get("post_selection") or {}).get("candidate_id")
                              if isinstance(qualification.get("post_selection"), Mapping) else None),
                preselected=bool((qualification.get("post_selection") or {}).get("preselected"))
                if isinstance(qualification.get("post_selection"), Mapping) else False,
                min_trades=int((qualification.get("minimums") or {}).get(
                    "trades", PROTOCOL_QUALIFICATION_MIN_TRADES)),
                min_sessions=int((qualification.get("minimums") or {}).get(
                    "sessions", PROTOCOL_QUALIFICATION_MIN_SESSIONS)),
                min_clusters=int((qualification.get("minimums") or {}).get(
                    "clusters", PROTOCOL_QUALIFICATION_MIN_CLUSTERS)),
                confidence=float(qualification.get("confidence", LOWER_BOUND_CONFIDENCE)),
                max_drawdown=(
                    None if qualification.get("max_drawdown_limit_source") ==
                    "median_risk_usd_times_r" else
                    qualification.get("max_drawdown_limit")),
                max_drawdown_r=float(
                    qualification.get("max_drawdown_limit_r")
                    if qualification.get("max_drawdown_limit_r") is not None
                    else QUALIFICATION_MAX_DRAWDOWN_R),
                draws=int(((qualification.get("delta_bootstrap") or {}).get(
                    "draws", DEFAULT_BOOTSTRAP_DRAWS))),
                seed=((qualification.get("delta_bootstrap") or {}).get("seed")
                      if isinstance((qualification.get("delta_bootstrap") or {}).get(
                          "seed"), int) else None),
                block_length=int(((qualification.get("delta_bootstrap") or {}).get(
                    "block_length", SERIAL_BLOCK_LENGTH))),
                equity_feed=equity_feed)
            if not has_equity_feed:
                for projection in (expected.get("authorization_projection") or {}).values():
                    if isinstance(projection, dict):
                        projection.pop("equity_feed", None)
            for key in ("sessions", "net_pnl", "trades", "matched", "mean_delta",
                        "control_adequacy", "r_matched", "mean_r_delta",
                        "net_positive", "delta_positive", "clusters", "minimums",
                        "adequate", "confidence", "delta_lcb",
                        "confidence_supported", "max_drawdown",
                        "max_drawdown_limit", "max_drawdown_limit_source",
                        "max_drawdown_limit_r", "drawdown_supported",
                        "drawdown_within_limit", "drawdown_limit_required",
                        "authorization_projection",
                        "raw_candidate_observations", "raw_baseline_observations"):
                # Newly sealed reports carry explicit adequacy/limit fields;
                # omitted fields in pre-v2 qualification payloads retain their
                # historical verification semantics.
                if key in qualification and expected.get(key) != qualification.get(key):
                    return False
            if "delta_bootstrap" in qualification and (
                    expected.get("delta_bootstrap") != qualification.get(
                        "delta_bootstrap")):
                return False
        body = {key: value for key, value in envelope.items() if key != "content_hash"}
        statistics = envelope.get("statistics")
        checks = envelope.get("checks")
        if (not isinstance(checks, Mapping) or
                not all(isinstance(value, bool) for value in checks.values())):
            return False
        if envelope.get("passes") and (
                not required_checks.issubset(set(checks)) or
                not all(bool(checks.get(key)) for key in required_checks) or
                not all(checks.values())):
            return False
        if envelope.get("passes"):
            provenance = envelope.get("provenance")
            if (not isinstance(provenance, Mapping) or
                    any(not provenance.get(key) for key in (
                        "dataset_hash", "config_hash", "code_hash", "provenance_hash"))):
                return False
        if envelope.get("passes") and qualification.get("available"):
            post = qualification.get("post_selection")
            if (not isinstance(post, Mapping) or post.get("preselected") is not True or
                    (envelope.get("candidate_id") is not None and
                     post.get("candidate_id") != envelope.get("candidate_id"))):
                return False
        null_control = envelope.get("null_control")
        if envelope.get("passes"):
            if (not isinstance(null_control, Mapping) or
                    null_control.get("kind") not in {"randomized_entry_null", "null_control"} or
                    not isinstance(null_control.get("available"), bool) or
                    not isinstance(null_control.get("matched"), int) or
                    not isinstance(null_control.get("mean_delta"), (int, float))):
                return False
        sources_fit = envelope.get("fit_source")
        sources_held = envelope.get("heldout_source")
        baseline_fit = envelope.get("fit_baseline_source")
        baseline_held = envelope.get("heldout_baseline_source")
        null_source = envelope.get("null_source")
        if (not isinstance(sources_fit, Sequence) or isinstance(sources_fit, (str, bytes)) or
                not isinstance(sources_held, Sequence) or isinstance(sources_held, (str, bytes)) or
                not isinstance(baseline_fit, Sequence) or isinstance(baseline_fit, (str, bytes)) or
                not isinstance(baseline_held, Sequence) or isinstance(baseline_held, (str, bytes)) or
                not isinstance(null_source, Sequence) or isinstance(null_source, (str, bytes)) or
                not all(isinstance(row, Mapping) for row in [
                    *sources_fit, *sources_held, *baseline_fit, *baseline_held,
                    *null_source])):
            return False
        vehicle = str(envelope.get("vehicle") or "")
        if (has_equity_feed and envelope.get("passes") and
                vehicle == "equity" and equity_feed not in {"iex", "sip"}):
            return False
        projection_payload = envelope.get("authorization_projection")
        if not isinstance(projection_payload, Mapping):
            return False
        projection_strict = any(_has_fill_metadata(row) for row in [
            *sources_fit, *sources_held, *baseline_fit, *baseline_held,
            *null_source])
        source_projections = {
            "fit": authorization_projection(sources_fit, vehicle=vehicle,
                                             strict=projection_strict,
                                             equity_feed=equity_feed),
            "heldout": authorization_projection(sources_held, vehicle=vehicle,
                                                 strict=projection_strict,
                                                 equity_feed=equity_feed),
            "fit_baseline": authorization_projection(
                baseline_fit, vehicle=vehicle, strict=projection_strict,
                equity_feed=equity_feed),
            "heldout_baseline": authorization_projection(
                baseline_held, vehicle=vehicle, strict=projection_strict,
                equity_feed=equity_feed),
            "null": authorization_projection(null_source, vehicle=vehicle,
                                              strict=projection_strict,
                                              equity_feed=equity_feed),
        }
        if projection_payload != {
                name: _projection_summary(
                    value, include_equity_feed=has_equity_feed)
                for name, value in source_projections.items()}:
            return False
        sources_fit_eligible = source_projections["fit"]["eligible"]
        sources_held_eligible = source_projections["heldout"]["eligible"]
        baseline_fit_eligible = source_projections["fit_baseline"]["eligible"]
        baseline_held_eligible = source_projections["heldout_baseline"]["eligible"]
        null_eligible = source_projections["null"]["eligible"]
        expected_counts = {
            "fit": sample_counts(
                sources_fit_eligible, vehicle=vehicle, equity_feed=equity_feed),
            "heldout": sample_counts(
                sources_held_eligible, vehicle=vehicle,
                equity_feed=equity_feed),
            "total": sample_counts(
                [*sources_fit_eligible, *sources_held_eligible],
                vehicle=vehicle, equity_feed=equity_feed),
        }
        if envelope.get("counts") != expected_counts:
            return False
        floors = envelope.get("floors")
        if not isinstance(floors, Mapping):
            return False
        lane = str(envelope.get("lane") or "")
        # A durable pass must carry protocol-compliant minimums in every
        # persisted fit/held-out floor and in the sealed qualification report.
        # This is checked independently of the producer's summary booleans so
        # a forged compact envelope cannot authorize after re-verification.
        if envelope.get("passes") and any(
                not _floor_minimums_meet(floors.get(name), lane=lane)
                for name in ("fit", "heldout")):
            return False
        if envelope.get("passes") and not _qualification_minimums_meet(
                qualification.get("minimums")):
            return False
        for name, source in (("fit", sources_fit), ("heldout", sources_held)):
            report = floors.get(name)
            if not isinstance(report, Mapping):
                return False
            feasibility = report.get("feasibility")
            minimums = report.get("minimums")
            if not isinstance(feasibility, Mapping) or not isinstance(minimums, Mapping):
                return False
            try:
                recomputed_floor = structural_floor(
                    source_projections[name]["eligible"], vehicle=vehicle,
                    min_trades=int(minimums.get("trades")),
                    min_sessions=int(minimums.get("sessions")),
                    min_clusters=int(minimums.get("clusters")),
                    required=bool(report.get("required", True)),
                    equity_feed=equity_feed)
                recomputed_feasibility = floor_feasibility(
                    source_projections[name]["eligible"], vehicle=vehicle,
                    min_trades=int(minimums.get("trades")),
                    min_sessions=int(minimums.get("sessions")),
                    min_clusters=int(minimums.get("clusters")),
                    equity_feed=equity_feed)
            except (TypeError, ValueError, OverflowError):
                return False
            # A floor is source-derived even when it is underpowered or
            # negative.  Compare the complete deterministic report, not only
            # its adequacy label; otherwise re-signing a changed net_pnl can
            # preserve the same status and slip through a diagnostic gate.
            for key in (
                    "vehicle", "trades", "sessions", "net_pnl", "max_drawdown",
                    "clusters", "structural_passes", "performance_passes",
                    "passes", "checks", "structural_checks", "performance_checks",
                    "minimums", "required", "adequate", "feasibility"):
                if report.get(key) != recomputed_floor.get(key):
                    return False
            if (feasibility.get("status") != recomputed_feasibility.get("status") or
                    bool(feasibility.get("adequate")) != bool(recomputed_feasibility.get("adequate"))):
                return False
        risk = envelope.get("risk_unit_report")
        if not isinstance(risk, Mapping) or risk.get("schema") != "risk-unit-report.v1":
            return False
        try:
            report_model = CostModel.from_dict(risk.get("cost_model") or {})
            rebuilt_risk = _risk_unit_report(
                [*sources_fit_eligible, *sources_held_eligible], vehicle=vehicle,
                costs=report_model,
                min_cost_coverage=float(risk.get("minimum_cost_coverage", 1.0)),
                equity_feed=equity_feed)
        except (CostError, TypeError, ValueError, OverflowError):
            return False
        risk_keys = ["vehicle", "rows", "adequate_rows", "total_risk_usd",
                    "total_round_trip_cost", "mean_risk_usd",
                    "mean_round_trip_cost", "risk_unit_usd",
                    "round_trip_cost_usd", "cost_to_risk_ratio",
                    "failed_opportunities", "observations", "adequate"]
        if has_equity_feed:
            risk_keys.append("equity_feed")
        for key in risk_keys:
            if rebuilt_risk.get(key) != risk.get(key):
                return False
        expected_fill_quality = {
            "fit": fill_source_summary(
                sources_fit, vehicle=vehicle, equity_feed=equity_feed),
            "heldout": fill_source_summary(
                sources_held, vehicle=vehicle, equity_feed=equity_feed),
        }
        if envelope.get("fill_quality") != expected_fill_quality:
            return False
        if "arm_diagnostics" in envelope:
            source_fit_sessions = {str(row.get("session_date") or "")
                                   for row in sources_fit}
            source_heldout_sessions = {str(row.get("session_date") or "")
                                       for row in sources_held}
            raw_null_fit = [row for row in null_source
                            if str(row.get("session_date") or "") in source_fit_sessions]
            raw_null_heldout = [row for row in null_source
                                if str(row.get("session_date") or "") in source_heldout_sessions]
            expected_arm_diagnostics = {
                "fit": arm_evidence_report(
                    candidate=sources_fit, baseline=baseline_fit,
                    null=raw_null_fit, vehicle=vehicle,
                    equity_feed=equity_feed,
                    projections={"candidate": source_projections["fit"],
                                 "baseline": source_projections["fit_baseline"],
                                     "null": authorization_projection(
                                     raw_null_fit, vehicle=vehicle,
                                     strict=projection_strict,
                                     equity_feed=equity_feed)}),
                "heldout": arm_evidence_report(
                    candidate=sources_held, baseline=baseline_held,
                    null=raw_null_heldout, vehicle=vehicle,
                    equity_feed=equity_feed,
                    projections={"candidate": source_projections["heldout"],
                                 "baseline": source_projections["heldout_baseline"],
                                     "null": authorization_projection(
                                     raw_null_heldout, vehicle=vehicle,
                                     strict=projection_strict,
                                     equity_feed=equity_feed)}),
                "all": arm_evidence_report(
                    candidate=[*sources_fit, *sources_held],
                    baseline=[*baseline_fit, *baseline_held],
                    null=null_source, vehicle=vehicle,
                    equity_feed=equity_feed,
                    projections={
                        "candidate": authorization_projection(
                            [*sources_fit, *sources_held],
                            vehicle=vehicle, strict=projection_strict,
                            equity_feed=equity_feed),
                        "baseline": authorization_projection(
                            [*baseline_fit, *baseline_held],
                            vehicle=vehicle, strict=projection_strict,
                            equity_feed=equity_feed),
                        "null": source_projections["null"],
                    }),
            }
            if envelope.get("arm_diagnostics") != expected_arm_diagnostics:
                return False
        expected_stress = cost_stress_report(
            [*sources_fit_eligible, *sources_held_eligible], vehicle=vehicle,
            risk_report=risk, equity_feed=equity_feed)
        if envelope.get("cost_stress") != expected_stress:
            return False
        expected_breadth = matched_effective_breadth(
            sources_held_eligible, baseline_held_eligible, vehicle=vehicle,
            equity_feed=equity_feed)
        if envelope.get("effective_breadth") != expected_breadth:
            return False
        performance = envelope.get("performance")
        if isinstance(performance, Mapping):
            expected_performance = performance_floor(
                sources_held_eligible, vehicle=vehicle,
                equity_feed=equity_feed)
            for key in ("heldout_net_pnl", "heldout_expectancy"):
                if key in performance:
                    source_key = key.removeprefix("heldout_")
                    if not _close_number(expected_performance.get(source_key),
                                         performance.get(key)):
                        return False
            if "heldout_r_delta" in performance:
                expected_effect = matched_cluster_test(
                    sources_held_eligible, baseline_held_eligible,
                    vehicle=vehicle, iterations=int(
                        (envelope.get("control") or {}).get("resamples") or 20_000),
                    equity_feed=equity_feed)
                if not _close_number(expected_effect.get("mean_r_delta"),
                                     performance.get("heldout_r_delta")):
                    return False
            if (performance.get("heldout_delta") is not None and
                    sources_held and
                    baseline_held):
                expected_delta = matched_cluster_test(
                    sources_held_eligible, baseline_held_eligible,
                    vehicle=vehicle, iterations=int(
                        (envelope.get("control") or {}).get("resamples") or 20_000),
                    equity_feed=equity_feed)
                if not _close_number(expected_delta.get("mean_delta"),
                                     performance.get("heldout_delta")):
                    return False
        if isinstance(performance, Mapping) and "max_drawdown" in performance:
            try:
                reported_drawdown = float(performance["max_drawdown"])
                if not (_close_number(reported_drawdown, max_drawdown_of(sources_held_eligible)) or
                        _close_number(reported_drawdown,
                                      max_drawdown_of([*sources_fit_eligible, *sources_held_eligible]))):
                    return False
            except (TypeError, ValueError, OverflowError):
                return False
        retirement = envelope.get("retirement")
        if retirement is not None:
            if not isinstance(retirement, Mapping):
                return False
            if retirement:
                retirement_bootstrap = retirement.get("bootstrap")
                retirement_bootstrap = (retirement_bootstrap
                                        if isinstance(retirement_bootstrap, Mapping)
                                        else {})
                expected_retirement = expectancy_rejection_report(
                    sources_held_eligible, vehicle=vehicle,
                    confidence=float(retirement.get(
                        "confidence", RETIREMENT_CONFIDENCE)),
                    min_sessions=int(retirement.get(
                        "sessions_required", RETIREMENT_MIN_SESSIONS)),
                    draws=int(retirement_bootstrap.get(
                        "draws", DEFAULT_BOOTSTRAP_DRAWS)),
                    seed=(int(retirement_bootstrap["seed"])
                          if isinstance(retirement_bootstrap.get("seed"), int)
                          and not isinstance(retirement_bootstrap.get("seed"), bool)
                          else None),
                    block_length=int(retirement_bootstrap.get(
                        "block_length", SERIAL_BLOCK_LENGTH)),
                    equity_feed=equity_feed)
                walk = envelope.get("walk_forward") or {}
                negative_folds = [item for item in walk.get("results", ())
                                  if item.get("adequate") and
                                  float(item.get("net_pnl", 0.0)) <= 0.0]
                expected_retirement["negative_forward_folds"] = len(
                    negative_folds)
                expected_retirement["independent_negative_windows"] = [
                    list(item.get("test_sessions") or ())
                    for item in negative_folds]
                expected_retirement["multi_window_negative"] = (
                    len(negative_folds) >= 2)
                if dict(retirement) != expected_retirement:
                    return False
        statistics = envelope.get("statistics")
        if isinstance(statistics, Mapping) and isinstance(checks, Mapping):
            alpha = float(statistics.get("alpha"))
            global_q = float(statistics.get("q_value"))
            family_q = float(statistics.get("family_q_value"))
            if not (math.isfinite(alpha) and 0.0 < alpha <= 1.0 and
                    math.isfinite(global_q) and 0.0 <= global_q <= 1.0 and
                    math.isfinite(family_q) and 0.0 <= family_q <= 1.0):
                return False
            if legacy_v2:
                # v2 predates complete multiple-testing batch evidence.  Its
                # scalar q-values and decision flags remain recomputable only
                # to the extent the historical envelope recorded them, so the
                # proof is audit-readable but is never eligible to authorize a
                # current replay epoch.
                if ("fdr_batch" in envelope or
                        "multiple_testing_batch_bound" in checks or
                        "actual_control_adequate" in checks):
                    return False
            else:
                batch_matches = _fdr_batch_matches(
                    envelope.get("fdr_batch"), statistics=statistics,
                    checks=checks,
                    provenance=(envelope.get("provenance")
                                if isinstance(envelope.get("provenance"), Mapping)
                                else {}),
                    candidate_id=envelope.get("candidate_id"),
                    cluster_multiple_tests=(
                        envelope.get("cluster_multiple_tests")
                        if isinstance(envelope.get("cluster_multiple_tests"), Mapping)
                        else None))
                if (bool(checks.get("multiple_testing_batch_bound")) != batch_matches or
                        (envelope.get("passes") and not batch_matches)):
                    return False
            cluster_tests = envelope.get("cluster_multiple_tests")
            if cluster_tests:
                if not isinstance(cluster_tests, Mapping):
                    return False
                cluster_q = statistics.get("cluster_q_value")
                if (not isinstance(cluster_q, (int, float)) or
                        isinstance(cluster_q, bool) or
                        not math.isfinite(float(cluster_q)) or
                        not 0.0 <= float(cluster_q) <= 1.0 or
                        bool(checks.get("cluster_fdr_significant")) !=
                        (float(cluster_q) <= alpha)):
                    return False
            if ("family_fdr_significant" in checks and
                    bool(checks["family_fdr_significant"]) != (family_q <= alpha)):
                return False
            if ("global_fdr_significant" in checks and
                    bool(checks["global_fdr_significant"]) != (global_q <= alpha)):
                return False
            online = envelope.get("online_fdr")
            if not isinstance(online, Mapping):
                return False
            if envelope.get("passes"):
                if online.get("required", True) is False:
                    if (online.get("status") != "deferred_to_live_shadow" or
                            online.get("tested") is not False or
                            online.get("decision") is not False or
                            online.get("p_value") is not None or
                            online.get("allocated_alpha") is not None):
                        return False
                else:
                    allocated = float(online.get("allocated_alpha"))
                    online_p = float(online.get("p_value"))
                    online_method = str(online.get("method") or "")
                    expected_online_p = (float(statistics.get("p_value"))
                                         if "raw_p" in online_method else
                                         global_q)
                    if (online.get("decision") is not True or
                            not math.isfinite(allocated) or allocated <= 0 or
                            not math.isfinite(online_p) or online_p > allocated or
                            not _close_number(online_p, expected_online_p)):
                        return False
                    if "raw_p" in online_method and (
                            online.get("p_value_kind") != "raw_confirmatory" or
                            not _close_number(online.get("raw_p_value"), online_p)):
                        return False
            # Rebuild code-owned decision flags from persisted source
            # evidence.  The v3 builder is also useful for v2 audit, but its
            # two new checks and tightened falsification/control semantics are
            # translated back to the exact historical v2 contract below.
            rebuilt = verified_gate_envelope(
                lane=str(envelope.get("lane") or ""), vehicle=vehicle,
                fit=sources_fit, heldout=sources_held,
                fit_baseline=baseline_fit, heldout_baseline=baseline_held,
                null_source=null_source,
                fit_raw=sources_fit, heldout_raw=sources_held,
                fit_baseline_raw=baseline_fit,
                heldout_baseline_raw=baseline_held,
                null_raw=null_source,
                fit_floor=floors.get("fit") or {},
                heldout_floor=floors.get("heldout") or {},
                fit_control=envelope.get("fit_control") or {},
                control=envelope.get("control") or {},
                p_value=float(statistics.get("p_value")), q_value=global_q,
                family_q_value=family_q, alpha=alpha,
                cluster_q_value=(statistics.get("cluster_q_value")
                                 if isinstance(statistics.get("cluster_q_value"),
                                                (int, float)) and
                                 not isinstance(statistics.get("cluster_q_value"), bool)
                                 else None),
                cluster_multiple_tests=(cluster_tests
                                        if isinstance(cluster_tests, Mapping)
                                        else None),
                fdr_batch=(envelope.get("fdr_batch")
                           if isinstance(envelope.get("fdr_batch"), Mapping)
                           else None),
                falsification=envelope.get("falsification") or {},
                separation=envelope.get("separation") or {},
                checks=checks, passes=bool(envelope.get("passes")),
                performance=performance,
                walk_forward=envelope.get("walk_forward") or {},
                retirement=envelope.get("retirement") or {},
                qualification=qualification,
                null_control=envelope.get("null_control") or {},
                online_fdr=online,
                provenance=envelope.get("provenance") or {},
                candidate_id=envelope.get("candidate_id"),
                risk_unit_report=risk,
                equity_feed=equity_feed,
            )
            rebuilt_checks = dict(rebuilt.get("checks") or {})
            rebuilt_passes = rebuilt.get("passes")
            if legacy_v2:
                rebuilt_checks.pop("actual_control_adequate", None)
                rebuilt_checks.pop("multiple_testing_batch_bound", None)
                legacy_control = envelope.get("control") or {}
                rebuilt_checks["actual_control_available"] = bool(
                    legacy_control.get("actual_control") is True and
                    legacy_control.get("available") is True and
                    isinstance(legacy_control.get("matched"), int) and
                    int(legacy_control.get("matched")) > 0)
                rebuilt_checks["falsification"] = bool(
                    (envelope.get("falsification") or {}).get("passes") is True)
                legacy_null = envelope.get("null_control") or {}
                legacy_null_adequacy = _legacy_v2_paired_control_adequacy(
                    sources_held, null_source, vehicle=vehicle,
                    min_matched=int(legacy_null.get(
                        "minimum_matched", NULL_CONTROL_MIN_MATCHED)),
                    min_coverage=float(legacy_null.get(
                        "minimum_coverage", NULL_CONTROL_MIN_COVERAGE)),
                    equity_feed=equity_feed)
                rebuilt_checks["null_control_available"] = bool(
                    legacy_null.get("available",
                                    legacy_null.get("actual_control", False)) and
                    legacy_null_adequacy["adequate"])
                rebuilt_checks["null_control_delta_positive"] = bool(
                    rebuilt_checks["null_control_available"] and
                    legacy_null.get("mean_delta") is not None and
                    legacy_null.get("p_value") is not None and
                    float(legacy_null["mean_delta"]) > 0 and
                    float(legacy_null["p_value"]) <= alpha)
                legacy_qualification = envelope.get("qualification") or {}
                rebuilt_checks["qualification_drawdown_supported"] = bool(
                    legacy_qualification.get("available") and
                    legacy_qualification.get("adequate") and
                    legacy_qualification.get("drawdown_supported") and
                    legacy_qualification.get("drawdown_within_limit", True))
                rebuilt_passes = bool(
                    envelope.get("passes") and
                    (vehicle != "equity" or equity_feed in {"iex", "sip"}) and
                    all(rebuilt_checks.get(key, False)
                        for key in LEGACY_GATE_REQUIRED_CHECKS_V2) and
                    all(rebuilt_checks.values()))
            elif not has_equity_feed:
                # The pre-binding schema authorized the historical SIP view.
                # Rebuild its exact pre-feed-binding decision; omission is
                # compatibility, never an inference of the shipped feed.
                rebuilt_passes = bool(
                    envelope.get("passes") and
                    all(rebuilt_checks.get(key, False)
                        for key in GATE_REQUIRED_CHECKS) and
                    all(rebuilt_checks.values()))
            if (rebuilt_checks != dict(checks) or
                    rebuilt_passes != envelope.get("passes")):
                return False
        # Recompute source-derived controls for both passing and diagnostic
        # v2 envelopes.  A failed/underpowered decision may legitimately have
        # an empty summary, so only reports that carry statistical evidence are
        # required to match their deterministic source reconstruction.
        def _has_statistical_evidence(report: Mapping) -> bool:
            # A failed diagnostic may intentionally record that no pairs were
            # available even when raw source arms are retained for audit.  An
            # explicit zero/None/empty-delta report is an unavailable result,
            # not stale statistical evidence to reconcile against the source.
            if (report.get("matched") == 0 and
                    report.get("mean_delta") is None and
                    report.get("deltas") == []):
                return False
            return any(key in report for key in (
                "mean_delta", "mean_delta_lcb", "p_value",
                "deltas", "delta_clusters"))

        def _compare_statistical_report(expected: Mapping,
                                        reported: Mapping) -> bool:
            if not _has_statistical_evidence(reported):
                return True
            for key in (
                    "matched", "mean_delta", "mean_delta_lcb", "p_value",
                    "matched_ids_hash", "deltas", "delta_clusters",
                    "paired_adequacy", "adequate", "coverage", "r_deltas",
                    "r_delta_clusters", "r_matched", "mean_r_delta",
                    "r_delta_lcb",
                    "lower_bound", "available",
                    "method", "exact", "resamples", "seed", "clusters",
                    "cluster_seconds", "paired_n", "observed_mean"):
                if key not in reported:
                    continue
                left, right = expected.get(key), reported.get(key)
                if isinstance(left, (int, float)) and not isinstance(left, bool):
                    if not _close_number(left, right):
                        return False
                elif left != right:
                    return False
            return True

        control = envelope.get("control") or {}
        if not isinstance(control, Mapping):
            return False
        if (_has_statistical_evidence(control) and sources_held and
                baseline_held):
            control_iterations = int(control.get("resamples") or 20_000)
            control_adequacy = (control.get("paired_adequacy")
                                if isinstance(control.get("paired_adequacy"), Mapping)
                                else {})
            expected_control = matched_cluster_test(
                sources_held_eligible, baseline_held_eligible, vehicle=vehicle,
                iterations=control_iterations,
                min_matched=max(ACTUAL_CONTROL_MIN_MATCHED, int(
                    control_adequacy.get(
                        "minimum_matched", ACTUAL_CONTROL_MIN_MATCHED))),
                min_coverage=max(ACTUAL_CONTROL_MIN_COVERAGE, float(
                    control_adequacy.get(
                        "minimum_coverage", ACTUAL_CONTROL_MIN_COVERAGE))),
                equity_feed=equity_feed)
            if not _compare_statistical_report(expected_control, control):
                return False
        fit_control = envelope.get("fit_control") or {}
        if not isinstance(fit_control, Mapping):
            return False
        # Shadow envelopes intentionally carry a prior-backtest fit summary;
        # it is not derivable from their empty fit source and remains a
        # diagnostic provenance record rather than an authorizing statistic.
        if (envelope.get("lane") == "backtest" and
                _has_statistical_evidence(fit_control) and sources_fit and
                baseline_fit):
            fit_iterations = int(fit_control.get("resamples") or 20_000)
            fit_adequacy = (fit_control.get("paired_adequacy")
                            if isinstance(fit_control.get("paired_adequacy"), Mapping)
                            else {})
            expected_fit_control = matched_cluster_test(
                sources_fit_eligible, baseline_fit_eligible, vehicle=vehicle,
                iterations=fit_iterations,
                min_matched=max(ACTUAL_CONTROL_MIN_MATCHED, int(
                    fit_adequacy.get(
                        "minimum_matched", ACTUAL_CONTROL_MIN_MATCHED))),
                min_coverage=max(ACTUAL_CONTROL_MIN_COVERAGE, float(
                    fit_adequacy.get(
                        "minimum_coverage", ACTUAL_CONTROL_MIN_COVERAGE))),
                equity_feed=equity_feed)
            if not _compare_statistical_report(expected_fit_control,
                                               fit_control):
                return False
        if (_has_statistical_evidence(null_control) and sources_held and
                null_source):
            null_iterations = int(null_control.get("resamples") or 20_000)
            if legacy_v2:
                # v2's matched statistic had no v3 adequacy/risk fields.  It
                # did persist the symmetric adequacy report added around that
                # statistic, so reconstruct that report with the historical
                # no-trade denominator and compare it when present.
                expected_null = matched_cluster_test(
                    sources_held_eligible, null_eligible, vehicle=vehicle,
                    iterations=null_iterations, equity_feed=equity_feed)
                persisted_adequacy = null_control.get("paired_adequacy")
                if isinstance(persisted_adequacy, Mapping):
                    min_matched = int(persisted_adequacy.get(
                        "minimum_matched", null_control.get(
                            "minimum_matched", NULL_CONTROL_MIN_MATCHED)))
                    min_coverage = float(persisted_adequacy.get(
                        "minimum_coverage", null_control.get(
                            "minimum_coverage", NULL_CONTROL_MIN_COVERAGE)))
                    expected_null["paired_adequacy"] = (
                        _legacy_v2_paired_control_adequacy(
                            sources_held, null_source, vehicle=vehicle,
                            min_matched=min_matched,
                            min_coverage=min_coverage,
                            equity_feed=equity_feed))
            else:
                expected_null = matched_cluster_test(
                    sources_held_eligible, null_eligible, vehicle=vehicle,
                    iterations=null_iterations,
                    min_matched=max(NULL_CONTROL_MIN_MATCHED, int(null_control.get(
                        "minimum_matched", NULL_CONTROL_MIN_MATCHED))),
                    min_coverage=max(NULL_CONTROL_MIN_COVERAGE, float(
                        null_control.get(
                            "minimum_coverage", NULL_CONTROL_MIN_COVERAGE))),
                    equity_feed=equity_feed)
            # ``null_control.available`` is the authorizing availability
            # (paired adequacy applied), while the descriptive matched test's
            # ``available`` only means at least one pair.  Compare the latter
            # to ``raw_available`` when the enriched arm carries both fields.
            if "raw_available" in null_control:
                expected_null["available"] = bool(null_control.get("raw_available"))
                null_report_for_compare = dict(null_control)
                null_report_for_compare["available"] = bool(
                    null_control.get("raw_available"))
            else:
                null_report_for_compare = null_control
            if not _compare_statistical_report(expected_null, null_report_for_compare):
                return False
        if sources_fit and sources_held:
            expected_separation = (
                heldout_separation(sources_fit_eligible, sources_held_eligible)
                if lane == "backtest" else
                {"fit": 0, "heldout": len(sources_held_eligible),
                 "overlap_sessions": [], "passes": bool(sources_held_eligible),
                 "mode": "new_data"})
            separation = envelope.get("separation")
            if not isinstance(separation, Mapping):
                return False
            for key, value in expected_separation.items():
                if key in separation and separation.get(key) != value:
                    return False
        falsification = envelope.get("falsification")
        if (sources_held and baseline_held and
                isinstance(falsification, Mapping) and falsification):
            draws = int(falsification.get("draws") or DEFAULT_NULL_DRAWS)
            placebo = placebo_null_distribution(
                sources_held_eligible, baseline_held_eligible,
                vehicle=vehicle, draws=draws,
                equity_feed=equity_feed)
            independent_mode = bool(
                falsification.get("independent_supplied") is True or
                falsification.get("p_value_source") ==
                FALSIFICATION_INDEPENDENT_P_VALUE_SOURCE)
            independent_placebo = None
            expected_independent = None
            if independent_mode:
                independent_draws = int(
                    falsification.get("independent_draws") or draws)
                independent_seed = int(falsification.get("independent_seed"))
                independent_placebo = placebo_null_distribution(
                    sources_held_eligible, baseline_held_eligible,
                    vehicle=vehicle, draws=independent_draws,
                    seed=independent_seed, equity_feed=equity_feed)
                if (independent_placebo["assignments_hash"] ==
                        placebo["assignments_hash"]):
                    return False
                if (falsification.get("independent_result_hash") !=
                        independent_placebo["assignments_hash"] or
                        falsification.get("independent_assignments_hash") !=
                        independent_placebo["assignments_hash"]):
                    return False
                expected_independent = falsification_gate(
                    independent_placebo["observed"],
                    independent_placebo["placebo"],
                    alpha=float(falsification.get("alpha", .05)))
            expected_falsification = {
                **falsification_gate(
                    placebo["observed"], placebo["placebo"],
                    alpha=float(falsification.get("alpha", .05)),
                    preregistered_p_value=(
                        control.get("p_value")
                        if (independent_mode or
                            falsification.get("p_value_source") ==
                            FALSIFICATION_P_VALUE_SOURCE) else None),
                    independent_p_value=(expected_independent["p_value"]
                                         if expected_independent is not None else None),
                    independent_method=(FALSIFICATION_INDEPENDENT_METHOD
                                        if independent_mode else None),
                    independent_result_hash=(
                        independent_placebo["assignments_hash"]
                        if independent_placebo is not None else None),
                    require_independent=independent_mode),
                "method": placebo["method"],
                "assignments_hash": placebo["assignments_hash"],
                "observations": len(placebo["observed"]),
                "draws": int(placebo["draws"]),
                "seed": int(placebo["seed"]),
                "clusters": int(placebo["cluster_count"]),
                **({
                    "independent_draws": int(independent_placebo["draws"]),
                    "independent_seed": int(independent_placebo["seed"]),
                    "independent_assignments_hash":
                        independent_placebo["assignments_hash"],
                } if independent_placebo is not None else {}),
            }
            for key, expected in expected_falsification.items():
                if key not in falsification:
                    continue
                reported = falsification.get(key)
                if isinstance(expected, (int, float)) and not isinstance(expected, bool):
                    if not _close_number(expected, reported):
                        return False
                elif reported != expected:
                    return False
        walk = envelope.get("walk_forward") or {}
        if not isinstance(walk, Mapping):
            return False
        if walk:
            expected_walk = walk_forward_report(
                sources_held_eligible, baseline_held_eligible, vehicle=vehicle,
                folds=int(walk.get("folds", 3)),
                min_fit_sessions=int(walk.get("fit_sessions_required", 1)),
                min_test_sessions=int(walk.get("test_sessions_required", 1)),
                min_test_trades=int(walk.get("test_trades_required", 1)),
                min_matched=int(walk.get("min_matched", 1)),
                min_coverage=float(walk.get(
                    "min_coverage", ACTUAL_CONTROL_MIN_COVERAGE)),
                requested_min_sessions=int(walk.get(
                    "requested_min_sessions", walk.get(
                        "effective_min_sessions", 1))),
                equity_feed=equity_feed)
            for key in ("available", "adequate", "majority_positive",
                        "tested_folds", "adequate_folds", "positive_folds"):
                if expected_walk.get(key) != walk.get(key):
                    return False
        if (_has_statistical_evidence(control) and control.get("deltas") and
                sources_held and baseline_held):
            recomputed = recompute_gate_statistics(envelope)
            if not recomputed.get("available"):
                return False
            if (not _close_number(recomputed.get("mean_delta"),
                                  control.get("mean_delta")) or
                    isinstance(statistics, Mapping) and
                    not _close_number(recomputed.get("p_value"),
                                      statistics.get("p_value"))):
                return False
            performance = envelope.get("performance") or {}
            if ("heldout_delta_lcb" in performance and
                    not _close_number(recomputed.get("mean_delta_lcb"),
                                      performance.get("heldout_delta_lcb"))):
                return False
        return bool(
            schema in SUPPORTED_GATE_ENVELOPE_SCHEMAS and
            envelope.get("lane") in {"backtest", "shadow"} and
            envelope.get("vehicle") in {"equity", "option"} and
            isinstance(envelope.get("passes"), bool) and
            isinstance(envelope.get("counts"), Mapping) and
            isinstance(envelope.get("floors"), Mapping) and
            isinstance(envelope.get("fit_control"), Mapping) and
            isinstance(envelope.get("control"), Mapping) and
            isinstance(envelope.get("statistics"), Mapping) and
            ((legacy_v2 and "fdr_batch" not in envelope) or
             (not legacy_v2 and isinstance(envelope.get("fdr_batch"), Mapping))) and
            isinstance(envelope.get("walk_forward"), Mapping) and
            isinstance(envelope.get("qualification"), Mapping) and
            isinstance(envelope.get("cost_stress"), Mapping) and
            isinstance(envelope.get("effective_breadth"), Mapping) and
            isinstance(envelope.get("null_control"), Mapping) and
            isinstance(envelope.get("online_fdr"), Mapping) and
            envelope.get("content_hash") == _content_hash(body))
    except (TypeError, ValueError, OverflowError):
        return False


def recompute_gate_statistics(envelope: Mapping) -> dict:
    """Recompute the envelope's statistics from its own source observations.

    The persisted matched deltas, cluster labels, draw counts and seeds are
    sufficient to reproduce the control p-value, the lower confidence bound
    and the falsification decision.  Re-verification compares these against
    the recorded conclusions rather than trusting them.
    """
    control = envelope.get("control")
    if not isinstance(control, Mapping):
        return {"available": False, "reason": "control evidence is missing"}
    deltas = control.get("deltas")
    clusters = control.get("delta_clusters")
    if not isinstance(deltas, Sequence) or not isinstance(clusters, Sequence) or \
            isinstance(deltas, (str, bytes)) or isinstance(clusters, (str, bytes)):
        return {"available": False, "reason": "matched delta evidence is missing"}
    if len(deltas) != len(clusters):
        return {"available": False, "reason": "matched delta evidence is inconsistent"}
    values: list[float] = []
    for item in deltas:
        if isinstance(item, bool):
            return {"available": False, "reason": "matched delta evidence is invalid"}
        try:
            value = float(item)
        except (TypeError, ValueError):
            return {"available": False, "reason": "matched delta evidence is invalid"}
        if not math.isfinite(value):
            return {"available": False, "reason": "matched delta evidence is invalid"}
        values.append(value)
    triples = [(float(cluster) * CLUSTER_SECONDS, value, 0.0)
               for cluster, value in zip(clusters, values)]
    sign_flip = paired_cluster_sign_flip(triples, cluster_seconds=CLUSTER_SECONDS)
    falsification = envelope.get("falsification")
    draws = None
    seed = None
    if isinstance(falsification, Mapping):
        draws = falsification.get("draws")
        seed = falsification.get("seed")
    null = sign_flip_null_statistics(
        values, [str(cluster) for cluster in clusters],
        draws=int(draws) if isinstance(draws, int) and draws > 0 else DEFAULT_NULL_DRAWS,
        seed=int(seed) if isinstance(seed, int) and not isinstance(seed, bool) else None)
    lower = control.get("lower_bound")
    confidence = LOWER_BOUND_CONFIDENCE
    bootstrap_draws = DEFAULT_BOOTSTRAP_DRAWS
    bootstrap_seed = None
    bootstrap_block_length = min(
        SERIAL_BLOCK_LENGTH, max(1, len({str(cluster) for cluster in clusters})))
    if isinstance(lower, Mapping):
        if isinstance(lower.get("confidence"), (int, float)) and \
                not isinstance(lower.get("confidence"), bool):
            confidence = float(lower["confidence"])
        if isinstance(lower.get("draws"), int) and not isinstance(lower.get("draws"), bool) \
                and lower["draws"] > 0:
            bootstrap_draws = int(lower["draws"])
        if isinstance(lower.get("seed"), int) and not isinstance(lower.get("seed"), bool):
            bootstrap_seed = int(lower["seed"])
        if isinstance(lower.get("block_length"), int) and \
                not isinstance(lower.get("block_length"), bool) and \
                lower["block_length"] > 0:
            bootstrap_block_length = int(lower["block_length"])
    bound = moving_block_cluster_bootstrap_lower_bound(
        values, [str(cluster) for cluster in clusters], confidence=confidence,
        draws=bootstrap_draws, seed=bootstrap_seed,
        block_length=bootstrap_block_length)
    shared_preregistered_p = bool(
        isinstance(falsification, Mapping) and
        falsification.get("p_value_source") == FALSIFICATION_P_VALUE_SOURCE)
    independent_mode = bool(
        isinstance(falsification, Mapping) and
        (falsification.get("independent_supplied") is True or
         falsification.get("p_value_source") ==
         FALSIFICATION_INDEPENDENT_P_VALUE_SOURCE))
    independent_null = null
    if independent_mode:
        independent_draws = falsification.get("independent_draws")
        independent_seed = falsification.get("independent_seed")
        if (not isinstance(independent_draws, int) or
                isinstance(independent_draws, bool) or independent_draws < 1 or
                not isinstance(independent_seed, int) or
                isinstance(independent_seed, bool)):
            return {"available": False,
                    "reason": "independent falsification evidence is missing"}
        independent_null = sign_flip_null_statistics(
            values, [str(cluster) for cluster in clusters],
            draws=independent_draws, seed=independent_seed)
    independent_empirical = falsification_gate(
        values, independent_null["statistics"],
        alpha=float(falsification.get("alpha", .05))
        if isinstance(falsification, Mapping) else .05)
    decision = falsification_gate(
        values, null["statistics"],
        alpha=float(falsification.get("alpha", .05))
        if isinstance(falsification, Mapping) and
        isinstance(falsification.get("alpha"), (int, float)) and
        not isinstance(falsification.get("alpha"), bool) else .05,
        preregistered_p_value=(float(sign_flip["p_value"])
                               if shared_preregistered_p else None),
        independent_p_value=(independent_empirical["p_value"]
                             if independent_mode else None),
        independent_method=(str(falsification.get("independent_method"))
                            if independent_mode and isinstance(falsification, Mapping)
                            and falsification.get("independent_method") else None),
        independent_result_hash=(str(falsification.get("independent_result_hash"))
                                 if independent_mode and isinstance(falsification, Mapping)
                                 and falsification.get("independent_result_hash") else None),
        require_independent=independent_mode)
    return {
        "available": True,
        "matched": len(values),
        "mean_delta": (sum(values) / len(values)) if values else None,
        "p_value": float(sign_flip["p_value"]),
        "mean_delta_lcb": bound["lower_bound"],
        "falsification_p_value": float(decision["p_value"]),
        "falsification_passes": bool(decision["passes"]),
    }


def _content_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _close_number(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    try:
        if left is None or right is None:
            return left is None and right is None
        return abs(float(left) - float(right)) <= tolerance * max(
            1.0, abs(float(left)), abs(float(right)))
    except (TypeError, ValueError, OverflowError):
        return False


__all__ = ["AcceptanceFloor", "ARM_EVIDENCE_SCHEMA", "CLUSTER_SECONDS", "GATE_ENVELOPE_SCHEMA",
           "LEGACY_GATE_ENVELOPE_SCHEMA_V2", "SUPPORTED_GATE_ENVELOPE_SCHEMAS",
           "LEGACY_GATE_REQUIRED_CHECKS_V2",
           "FDR_BATCH_SCHEMA", "FDR_BATCH_METHOD", "fdr_batch_evidence",
           "GATE_REQUIRED_CHECKS", "AUTHORIZATION_PROJECTION_SCHEMA",
           "FALSIFICATION_P_VALUE_SOURCE",
           "FALSIFICATION_INDEPENDENT_P_VALUE_SOURCE",
           "FALSIFICATION_INDEPENDENT_METHOD",
           "authorization_projection", "arm_evidence_report", "gate_dependence_report",
           "gate_source_statistic_report", "source_statistic_report",
           "gate_source_dependence_report", "source_statistic_dependence_report",
           "fill_source_summary", "floor_feasibility",
           "unevaluable_reason",
           "protocol_minimums", "validate_protocol_floor",
           "PROTOCOL_BACKTEST_MIN_TRADES", "PROTOCOL_BACKTEST_MIN_SESSIONS",
           "PROTOCOL_BACKTEST_MIN_CLUSTERS", "PROTOCOL_SHADOW_MIN_TRADES",
           "PROTOCOL_SHADOW_MIN_SESSIONS", "PROTOCOL_SHADOW_MIN_CLUSTERS",
           "PROTOCOL_QUALIFICATION_MIN_TRADES",
           "PROTOCOL_QUALIFICATION_MIN_SESSIONS",
           "PROTOCOL_QUALIFICATION_MIN_CLUSTERS",
           "BACKTEST_MIN_TRADES", "BACKTEST_MIN_SESSIONS",
           "BACKTEST_MIN_CLUSTERS", "SHADOW_MIN_TRADES",
           "SHADOW_MIN_SESSIONS", "SHADOW_MIN_CLUSTERS",
           "LOWER_BOUND_CONFIDENCE", "SealedQualificationWindow",
           "COST_STRESS_REQUIRED_BPS", "COST_STRESS_SCENARIOS_BPS",
           "QUALIFICATION_MIN_TRADES", "QUALIFICATION_MIN_SESSIONS",
           "QUALIFICATION_MIN_CLUSTERS",
           "QUALIFICATION_MAX_BYTES", "QUALIFICATION_MAX_ROWS",
           "SealedWindowError", "chronological_split", "cost_stress_report",
           "deterministic_placebo_deltas", "falsification_gate",
           "heldout_separation", "matched_cluster_test", "matched_effective_breadth",
           "paired_control_adequacy", "NULL_CONTROL_MIN_MATCHED",
           "NULL_CONTROL_MIN_COVERAGE",
           "ACTUAL_CONTROL_MIN_MATCHED", "ACTUAL_CONTROL_MIN_COVERAGE",
           "QUALIFICATION_MAX_DRAWDOWN_R",
           "clustered_mde_power_report", "clustered_mde_power",
           "matched_pairs",
           "max_drawdown_of", "paired_delta", "performance_floor",
           "expectancy_rejection_report", "RETIREMENT_CONFIDENCE",
           "RETIREMENT_MIN_SESSIONS", "RETIREMENT_MIN_USEFUL_R",
           "placebo_null_distribution", "placebo_ratio", "qualification_report",
           "recompute_gate_statistics", "risk_unit_report", "sample_counts", "seal_final_window",
           "structural_floor", "verified_gate_envelope", "verify_gate_envelope",
           "walk_forward_report"]
