"""Preregistered hypotheses: frozen before the data, decided by fixed rules.

Every other lane in this repository searches.  Search is how a hypothesis is
found, and it is also why a searched result cannot confirm itself: the same
sessions that suggested the idea are the ones that would have to test it.
This module is the other half.  A hypothesis registered here is fixed in
code, content-hashed, and evaluated only on sessions that no analysis had
seen when it was written.  Its decision rule, its looks, its alpha and its
economic hurdle are stated in the manifest, not chosen after the fact.

A pass authorizes nothing about trading.  It earns the hypothesis a slot in
the existing proof pipeline (held-out replay, controls, FDR, live-shadow
parity, paper trial), which remains the only route to money.

Changing anything in a manifest changes its hash; ``tests/research/
test_preregistered.py`` pins the hash, so an edit fails the suite and has to
be made as a new hypothesis version rather than silently rewriting this one.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.contracts.rule import rule_variant_id, validate_rule_spec

from .costs import diagnostic_backfill_policy
from .edge_ledger_store import content_hash
from .gates import PROTOCOL_QUALIFICATION_MIN_CLUSTERS
from .signal_quality import measure_signal_quality
from .stats import student_t_sf


PREREGISTRATION_SCHEMA = "preregistered-hypothesis.v1"
EVALUATION_SCHEMA = "preregistered-evaluation.v1"
VWAP_REVERSION_HYPOTHESIS = "vwap-reversion-control-adjusted.v1"

# Frozen literals, not derivations: a derived spec would silently follow any
# later change to the family templates.  ``_frozen`` fails closed if the
# grammar ever resolves these to a different content identity.
_SUBJECT_ID = "rule.vwap-reversion.ab97bfe87ed566de"
_SUBJECT_SPEC: dict[str, Any] = {
    "atr_period": 14, "breakeven_r": None, "compression_bps": 45.0,
    "confirmation": "none", "confirmations": [], "entry_after_minutes": 0,
    "entry_before_minutes": 390, "exit_before_minutes": None,
    "family": "vwap_reversion", "lookback": 20, "max_atr_bps": 5000.0,
    "max_hold_bars": 90, "min_atr_bps": 0.0, "range_minutes": 15,
    "schema": "rule-strategy.v4", "side": "both", "slow_lookback": 40,
    "stop_atr": 1.0, "target_lookback": 20, "target_mode": "fixed_r",
    "target_r": 1.5, "threshold_bps": 25.0, "trailing_stop_r": None,
    "volume_multiplier": 1.25, "zscore": 1.25,
}
_MIRROR_ID = "rule.vwap-trend.349de232c3d7a18c"
_MIRROR_SPEC: dict[str, Any] = {
    "atr_period": 14, "breakeven_r": None, "compression_bps": 45.0,
    "confirmation": "volume", "confirmations": [], "entry_after_minutes": 0,
    "entry_before_minutes": 390, "exit_before_minutes": None,
    "family": "vwap_trend", "lookback": 15, "max_atr_bps": 5000.0,
    "max_hold_bars": 90, "min_atr_bps": 0.0, "range_minutes": 15,
    "schema": "rule-strategy.v4", "side": "both", "slow_lookback": 40,
    "stop_atr": 1.0, "target_lookback": 20, "target_mode": "fixed_r",
    "target_r": 2.0, "threshold_bps": 8.0, "trailing_stop_r": None,
    "volume_multiplier": 1.25, "zscore": 1.25,
}

OPENING_RANGE_FADE_HYPOTHESIS = "opening-range-fade-control-adjusted.v1"
_ORF_SUBJECT_ID = "rule.opening-range-fade.d5785d9e70b56def"
_ORF_SUBJECT_SPEC: dict[str, Any] = {
    "atr_period": 14, "breakeven_r": None, "compression_bps": 45.0,
    "confirmation": "none", "confirmations": [], "entry_after_minutes": 0,
    "entry_before_minutes": 390, "exit_before_minutes": None,
    "family": "opening_range_fade", "lookback": 15, "max_atr_bps": 5000.0,
    "max_hold_bars": 90, "min_atr_bps": 0.0, "range_minutes": 20,
    "schema": "rule-strategy.v4", "side": "both", "slow_lookback": 40,
    "stop_atr": 1.0, "target_lookback": 20, "target_mode": "fixed_r",
    "target_r": 1.5, "threshold_bps": 8.0, "trailing_stop_r": None,
    "volume_multiplier": 1.25, "zscore": 1.25,
}
_ORF_MIRROR_ID = "rule.opening-range-breakout.0eb200d3136d80ee"
_ORF_MIRROR_SPEC: dict[str, Any] = {
    "atr_period": 14, "breakeven_r": None, "compression_bps": 45.0,
    "confirmation": "volume", "confirmations": [], "entry_after_minutes": 0,
    "entry_before_minutes": 390, "exit_before_minutes": None,
    "family": "opening_range_breakout", "lookback": 15, "max_atr_bps": 5000.0,
    "max_hold_bars": 90, "min_atr_bps": 0.0, "range_minutes": 15,
    "schema": "rule-strategy.v4", "side": "both", "slow_lookback": 40,
    "stop_atr": 1.0, "target_lookback": 20, "target_mode": "fixed_r",
    "target_r": 2.0, "threshold_bps": 5.0, "trailing_stop_r": None,
    "volume_multiplier": 1.25, "zscore": 1.25,
}

PRIMARY_HORIZON_MINUTES = 60
SECONDARY_HORIZON_MINUTES = 120
ECONOMIC_HURDLE_BPS = 3.0
LOOK_ALPHA_ONE_SIDED = 0.025
MIN_MATCHED_CONTROLS = 30
MIN_CONTROL_COVERAGE = 0.80
# The sealed manifest registers the control as the same instrument at the same
# session minute on *other* sessions.  signal_quality falls back to looser
# tiers when a corpus is too small to supply that; a look may only decide on
# the registered tier.
REGISTERED_CONTROL_TIER = "cross_session_same_session_minute"


class PreregistrationError(ValueError):
    """Raised when a registered hypothesis cannot be resolved as frozen."""


def _frozen(spec: Mapping[str, Any], expected_id: str) -> dict[str, Any]:
    normalized = validate_rule_spec(dict(spec))
    resolved = rule_variant_id(normalized)
    if resolved != expected_id:
        raise PreregistrationError(
            f"frozen specification now resolves to {resolved}, not "
            f"{expected_id}; register a new hypothesis version instead")
    return normalized


def _vwap_reversion_manifest() -> dict[str, Any]:
    first_look = int(PROTOCOL_QUALIFICATION_MIN_CLUSTERS)
    body = {
        "schema": PREREGISTRATION_SCHEMA,
        "hypothesis_id": VWAP_REVERSION_HYPOTHESIS,
        "registered_on": "2026-09-22",
        "claim": (
            "On the configured 24-ETF universe, bars on which the frozen "
            "session-VWAP reversion rule fires are followed over the next 60 "
            "minutes by a return, in the rule's own direction, that exceeds a "
            "clock-matched control by at least 3.0 bps on average."),
        "why_this_hypothesis": (
            "vwap_reversion led five in-sample measurements in the September "
            "2026 audit (docs/audit-2026-09-21/SIGNAL-VALUE.md), and its mirror "
            "vwap_trend was the worst arm in the catalogue.  The family was "
            "chosen because of those results, which is exactly why it may only "
            "be confirmed on sessions they did not use."),
        "subject": {
            "variant_id": _SUBJECT_ID,
            "rule_spec": _frozen(_SUBJECT_SPEC, _SUBJECT_ID),
            "provenance": (
                "diagnostic-shadow slot 7 baseline; already evaluated forward "
                "by the deployed shadow service, so no new runtime arm is needed"),
            "selection_note": (
                "the registered baseline, not the in-sample-best of its pair"),
        },
        "primary_endpoint": {
            "instrument": "research.signal_quality.measure_signal_quality",
            "metric": "candidate_minus_control_bps",
            "horizon_minutes": PRIMARY_HORIZON_MINUTES,
            "horizon_rationale": (
                "inside the frozen arm's own 90-bar hold, so its bracket could "
                "capture it; 120 minutes would exceed the hold"),
            "control": (
                "same instrument, same session minute, other sessions of the "
                "same look window"),
            "inference_unit": "session",
            "statistics": [
                "cr1 session-clustered t against Student-t with G-1 df",
                "one-sided cluster sign-flip over whole sessions"],
        },
        "sealed_window": {
            "sessions_after": "2026-09-21",
            "rationale": (
                "2026-09-21 is the last session any analysis in this work "
                "examined; later sessions were unseen when this was written"),
            "control_pool": "restricted to the sessions of the look window",
        },
        "data": {
            "decision_provider": "alpaca",
            "decision_feed": "iex",
            "decision_source_mode": "forward_observed",
            "feed_rationale": (
                "the deployment computes session VWAP from its own feed; a "
                "result on another feed is a replication, not the decision"),
            "other_feeds": "descriptive replication only, never decision-bearing",
        },
        "looks": [
            {"look": 1, "sessions": first_look,
             "alpha_one_sided": LOOK_ALPHA_ONE_SIDED},
            {"look": 2, "sessions": 2 * first_look,
             "alpha_one_sided": LOOK_ALPHA_ONE_SIDED},
        ],
        "look_rule": (
            "each look uses exactly the first N sealed sessions in date order, "
            "so a result cannot be improved by choosing when to look; alpha is "
            "split 0.025 per look, 0.05 overall"),
        "decision_rule": {
            "economic_hurdle_bps": ECONOMIC_HURDLE_BPS,
            "hurdle_origin": (
                "the kill threshold written in FINDINGS.md at commit dc156ca, "
                "before any control-adjusted measurement existed"),
            "pass": (
                "cr1 one-sided p < alpha AND sign-flip one-sided p < alpha AND "
                "mean control-adjusted delta >= economic_hurdle_bps"),
            "futility": (
                "one-sided test that the mean is below the hurdle has p < alpha: "
                "retire the hypothesis"),
            "inconclusive": (
                "neither; at look 1 continue to look 2, at look 2 the claim is "
                "not established and the hypothesis is retired"),
            "underpowered_control": (
                f"fewer than {MIN_MATCHED_CONTROLS} matched controls or coverage "
                f"below {MIN_CONTROL_COVERAGE:.0%} counts as inconclusive"),
            "before_first_look": "descriptive only; no decision and no early stop",
        },
        "secondary_descriptive": [
            {"name": "mirror", "variant_id": _MIRROR_ID,
             "rule_spec": _frozen(_MIRROR_SPEC, _MIRROR_ID),
             "expectation": "negative 60-minute control-adjusted delta"},
            {"name": "subject_120m", "horizon_minutes": SECONDARY_HORIZON_MINUTES},
        ],
        "authorizes": (
            "a pass earns entry to the existing proof pipeline; it never "
            "authorizes trading, sizing, or a configuration change"),
    }
    return {**body, "manifest_hash": content_hash(body)}


def _opening_range_fade_manifest() -> dict[str, Any]:
    # Same instrument, endpoint, looks and decision rule as the VWAP
    # hypothesis, so the two can be read side by side.  Everything that is
    # specific to this claim is written out here rather than inherited.
    first_look = int(PROTOCOL_QUALIFICATION_MIN_CLUSTERS)
    body = {
        "schema": PREREGISTRATION_SCHEMA,
        "hypothesis_id": OPENING_RANGE_FADE_HYPOTHESIS,
        "registered_on": "2026-09-23",
        "claim": (
            "On the configured 24-ETF universe, bars on which the frozen "
            "opening-range fade rule fires are followed over the next 60 "
            "minutes by a return, in the rule's own direction, that exceeds a "
            "clock-matched control by at least 3.0 bps on average."),
        "why_this_hypothesis": (
            "opening_range_fade was the only catalogue arm whose bracket "
            "replay stayed positive after realistic costs in the September "
            "2026 audit (docs/audit-2026-09-21/forward-costs-2026-09-22.json: "
            "+10.2 bps per trade over 59 trades, session-clustered t +2.57), "
            "and its 60-minute control-adjusted delta was +19.5 bps "
            "(clustered t +2.95, df 17).  It was chosen because of those "
            "in-sample results and it lost on 2026-09-22, the one later "
            "session seen, which is why it may only be confirmed on sessions "
            "none of them used."),
        "subject": {
            "variant_id": _ORF_SUBJECT_ID,
            "rule_spec": _frozen(_ORF_SUBJECT_SPEC, _ORF_SUBJECT_ID),
            "provenance": (
                "diagnostic-shadow opening_range_fade baseline, already "
                "evaluated forward by the deployed shadow service, and the "
                "incumbent of deploy/paper-orf.config.json"),
            "selection_note": (
                "the registered baseline, not the in-sample-best of its pair"),
        },
        "primary_endpoint": {
            "instrument": "research.signal_quality.measure_signal_quality",
            "metric": "candidate_minus_control_bps",
            "horizon_minutes": PRIMARY_HORIZON_MINUTES,
            "horizon_rationale": (
                "inside the frozen arm's own 90-bar hold, so its bracket could "
                "capture it"),
            "control": (
                "same instrument, same session minute, other sessions of the "
                "same look window"),
            "inference_unit": "session",
            "statistics": [
                "cr1 session-clustered t against Student-t with G-1 df",
                "one-sided cluster sign-flip over whole sessions"],
        },
        "sealed_window": {
            "sessions_after": "2026-09-23",
            "rationale": (
                "registered while the 2026-09-23 session was trading; no bar "
                "of that session had been fetched, but it is excluded so the "
                "window cannot contain a session that was under way when the "
                "rules were written"),
            "control_pool": "restricted to the sessions of the look window",
        },
        "data": {
            "decision_provider": "alpaca",
            "decision_feed": "iex",
            "decision_source_mode": "forward_observed",
            "feed_rationale": (
                "the deployment trades on its own feed; a result on another "
                "feed is a replication, not the decision"),
            "other_feeds": "descriptive replication only, never decision-bearing",
        },
        "looks": [
            {"look": 1, "sessions": first_look,
             "alpha_one_sided": LOOK_ALPHA_ONE_SIDED},
            {"look": 2, "sessions": 2 * first_look,
             "alpha_one_sided": LOOK_ALPHA_ONE_SIDED},
        ],
        "look_rule": (
            "each look uses exactly the first N sealed sessions in date order, "
            "so a result cannot be improved by choosing when to look; alpha is "
            "split 0.025 per look, 0.05 overall"),
        "decision_rule": {
            "economic_hurdle_bps": ECONOMIC_HURDLE_BPS,
            "hurdle_origin": (
                "the same kill threshold as vwap-reversion-control-adjusted.v1, "
                "written at commit dc156ca"),
            "pass": (
                "cr1 one-sided p < alpha AND sign-flip one-sided p < alpha AND "
                "mean control-adjusted delta >= economic_hurdle_bps"),
            "futility": (
                "one-sided test that the mean is below the hurdle has p < alpha: "
                "retire the hypothesis"),
            "inconclusive": (
                "neither; at look 1 continue to look 2, at look 2 the claim is "
                "not established and the hypothesis is retired"),
            "underpowered_control": (
                f"fewer than {MIN_MATCHED_CONTROLS} matched controls, coverage "
                f"below {MIN_CONTROL_COVERAGE:.0%}, or any control outside the "
                f"registered cross-session tier counts as inconclusive"),
            "before_first_look": "descriptive only; no decision and no early stop",
        },
        "secondary_descriptive": [
            {"name": "mirror", "variant_id": _ORF_MIRROR_ID,
             "rule_spec": _frozen(_ORF_MIRROR_SPEC, _ORF_MIRROR_ID),
             "expectation": "negative 60-minute control-adjusted delta"},
            {"name": "subject_120m", "horizon_minutes": SECONDARY_HORIZON_MINUTES},
            {"name": "paper_trial",
             "profile": "deploy/paper-orf.config.json",
             "trial_id": "paper-orf-baseline-20260923-v1",
             "note": ("trade-level evidence on real paper fills under the "
                      "paper trial's own floors; never part of this decision")},
        ],
        "multiplicity": (
            "a second registered hypothesis with its own alpha; the two are "
            "reported separately and neither is re-tested on the other's "
            "behalf"),
        "authorizes": (
            "a pass earns entry to the existing proof pipeline; it never "
            "authorizes trading, sizing, or a configuration change"),
    }
    return {**body, "manifest_hash": content_hash(body)}


_REGISTRY = {VWAP_REVERSION_HYPOTHESIS: _vwap_reversion_manifest,
             OPENING_RANGE_FADE_HYPOTHESIS: _opening_range_fade_manifest}
PREREGISTERED_HYPOTHESES = tuple(_REGISTRY)


def preregistration(hypothesis_id: str = VWAP_REVERSION_HYPOTHESIS) -> dict[str, Any]:
    try:
        build = _REGISTRY[str(hypothesis_id)]
    except KeyError as exc:
        raise PreregistrationError(
            f"unknown preregistered hypothesis: {hypothesis_id!r}") from exc
    return build()


def _session_of(bar: Any) -> date:
    return bar.identity.session_date


def _horizon(result: Mapping[str, Any], minutes: int) -> Mapping[str, Any]:
    metrics = result.get("horizon_metrics") or {}
    item = metrics.get(f"{int(minutes)}m")
    return item if isinstance(item, Mapping) else {}


def _summary(metric: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_count": metric.get("candidate_count"),
        "matched_count": metric.get("matched_count"),
        "session_clusters": metric.get("session_clusters"),
        "delta_bps": metric.get("candidate_minus_control_bps"),
        "cluster_stderr_bps": metric.get(
            "candidate_minus_control_cluster_stderr_bps"),
        "cluster_t_stat": metric.get("candidate_minus_control_cluster_t_stat"),
        "cluster_df": metric.get("candidate_minus_control_cluster_df"),
        "sign_flip_p_value": metric.get(
            "candidate_minus_control_cluster_sign_flip_p_value"),
        "event_t_stat": metric.get("candidate_minus_control_t_stat"),
        "control_matching_counts": dict(
            metric.get("control_matching_counts") or {}),
    }


def look_outcome(metric: Mapping[str, Any], *, alpha: float,
                 hurdle_bps: float) -> dict[str, Any]:
    """Apply the registered decision rule to one look's primary endpoint."""
    summary = _summary(metric)
    candidates = summary["candidate_count"] or 0
    matched = summary["matched_count"] or 0
    if (matched < MIN_MATCHED_CONTROLS or not candidates or
            matched / candidates < MIN_CONTROL_COVERAGE):
        return {**summary, "outcome": "inconclusive",
                "reason": "underpowered_control"}
    tiers = {tier for tier, count in summary["control_matching_counts"].items()
             if count}
    if tiers and tiers != {REGISTERED_CONTROL_TIER}:
        # Conformance with the sealed manifest, added after sealing: this can
        # only turn a pass into inconclusive, never the reverse.
        return {**summary, "outcome": "inconclusive",
                "reason": "control_tier_not_registered"}
    mean, error = summary["delta_bps"], summary["cluster_stderr_bps"]
    df, flip_p = summary["cluster_df"], summary["sign_flip_p_value"]
    if mean is None or not error or not df or flip_p is None:
        return {**summary, "outcome": "inconclusive",
                "reason": "clustered_inference_unavailable"}
    mean, error, df = float(mean), float(error), float(df)
    p_zero = student_t_sf(mean / error, df)
    p_below_hurdle = 1.0 - student_t_sf((mean - float(hurdle_bps)) / error, df)
    detail = {**summary, "cr1_p_one_sided": p_zero,
              "p_mean_below_hurdle": p_below_hurdle,
              "alpha_one_sided": float(alpha), "hurdle_bps": float(hurdle_bps)}
    if p_zero < alpha and float(flip_p) < alpha and mean >= hurdle_bps:
        return {**detail, "outcome": "pass", "reason": "all_pass_conditions_met"}
    if p_below_hurdle < alpha:
        return {**detail, "outcome": "futility",
                "reason": "mean_credibly_below_hurdle"}
    return {**detail, "outcome": "inconclusive", "reason": "neither_rule_met"}


def evaluate(bars: Sequence[Any], *,
             hypothesis_id: str = VWAP_REVERSION_HYPOTHESIS,
             decision_eligible: bool,
             eligibility_reasons: Sequence[str] = (),
             provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate a registered hypothesis on the sealed window of ``bars``.

    ``decision_eligible`` must come from the caller's source preflight, never
    from the bars' own defaults: a normalized bar reports
    ``forward_observed`` unless told otherwise, which is not evidence.
    """
    manifest = preregistration(hypothesis_id)
    boundary = date.fromisoformat(manifest["sealed_window"]["sessions_after"])
    spec = manifest["subject"]["rule_spec"]
    mirror = next(item for item in manifest["secondary_descriptive"]
                  if item["name"] == "mirror")
    sealed = [bar for bar in bars if _session_of(bar) > boundary]
    excluded = len(bars) - len(sealed)
    sessions = sorted({_session_of(bar) for bar in sealed})
    policy = None if decision_eligible else diagnostic_backfill_policy()
    horizons = (PRIMARY_HORIZON_MINUTES, SECONDARY_HORIZON_MINUTES)

    def measure(rows: Sequence[Any], rule: Mapping[str, Any]) -> Mapping[str, Any]:
        return measure_signal_quality(rows, rule, policy=policy, horizons=horizons)

    interim: dict[str, Any] = {"sessions": len(sessions),
                               "decision": "none_before_first_look"}
    if sealed:
        subject = measure(sealed, spec)
        interim.update({
            "primary_60m": _summary(_horizon(subject, PRIMARY_HORIZON_MINUTES)),
            "subject_120m": _summary(_horizon(subject, SECONDARY_HORIZON_MINUTES)),
            "mirror_60m": _summary(_horizon(
                measure(sealed, mirror["rule_spec"]), PRIMARY_HORIZON_MINUTES)),
        })

    looks: list[dict[str, Any]] = []
    outcome = "accruing"
    for look in manifest["looks"]:
        needed = int(look["sessions"])
        if len(sessions) < needed:
            looks.append({"look": look["look"], "sessions_required": needed,
                          "status": "not_reached"})
            continue
        window = set(sessions[:needed])
        subset = [bar for bar in sealed if _session_of(bar) in window]
        result = look_outcome(
            _horizon(measure(subset, spec), PRIMARY_HORIZON_MINUTES),
            alpha=float(look["alpha_one_sided"]),
            hurdle_bps=float(manifest["decision_rule"]["economic_hurdle_bps"]))
        looks.append({"look": look["look"], "sessions_required": needed,
                      "window_first": sessions[0].isoformat(),
                      "window_last": sessions[needed - 1].isoformat(),
                      "status": "evaluated", **result})
        if outcome == "accruing":
            if result["outcome"] in {"pass", "futility"}:
                outcome = result["outcome"]
            elif look["look"] == manifest["looks"][-1]["look"]:
                outcome = "not_established"

    return {
        "schema": EVALUATION_SCHEMA,
        "hypothesis_id": manifest["hypothesis_id"],
        "manifest_hash": manifest["manifest_hash"],
        "authorizing": False,
        "trading_authorized": False,
        "decision_eligible": bool(decision_eligible),
        "eligibility_reasons": list(eligibility_reasons),
        "sealed_boundary": boundary.isoformat(),
        "sealed_sessions": [item.isoformat() for item in sessions],
        "excluded_examined_bars": excluded,
        "interim": interim,
        "looks": looks,
        "decision": outcome if decision_eligible else "diagnostic_only",
        "rule_outcome_if_eligible": outcome,
        "provenance": dict(provenance or {}),
    }


def _load(path: Path, manifest: Mapping[str, Any]) -> tuple[list[Any], bool, list[str], dict]:
    """Read a corpus strictly if it can be decision-bearing, else labelled."""
    from .edge_discovery_core import _read_discovery_rows
    from .source_validation import SourceValidationError, validate_source

    data = manifest["data"]
    reasons: list[str] = []
    try:
        report = validate_source(path, diagnostic_only=False)
        _raw, bars, _options, _quotes = _read_discovery_rows(
            path, require_provenance=True,
            expected_equity_feed=data["decision_feed"],
            expected_provider=data["decision_provider"])
        eligible = True
    except SourceValidationError as exc:
        reasons.append(f"source preflight: {exc}")
        report = validate_source(path, diagnostic_only=True)
        _raw, bars, _options, _quotes = _read_discovery_rows(path)
        eligible = False
    except Exception as exc:  # feed/provider mismatch from the strict reader
        reasons.append(f"feed or provider: {exc}")
        report = validate_source(path, diagnostic_only=True)
        _raw, bars, _options, _quotes = _read_discovery_rows(path)
        eligible = False
    provenance = {"source": str(path),
                  "content_hash": report.get("content_hash"),
                  "source_mode_counts": report.get("source_mode_counts"),
                  "feeds": sorted({bar.identity.feed for bar in bars}),
                  "providers": sorted({bar.identity.provider for bar in bars})}
    return bars, eligible, reasons, provenance


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True,
                        help="normalized JSONL corpus or session partition directory")
    parser.add_argument("--out", required=True,
                        help="new report path; an existing file is never replaced")
    parser.add_argument("--hypothesis", default=VWAP_REVERSION_HYPOTHESIS,
                        choices=PREREGISTERED_HYPOTHESES)
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        parser.error(f"refusing to replace existing report {out}")
    manifest = preregistration(args.hypothesis)
    bars, eligible, reasons, provenance = _load(Path(args.data), manifest)
    report = evaluate(bars, hypothesis_id=args.hypothesis,
                      decision_eligible=eligible, eligibility_reasons=reasons,
                      provenance=provenance)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_name(out.name + ".partial")
    temporary.write_text(json.dumps(report, indent=1, sort_keys=True,
                                    default=str) + "\n", encoding="utf-8")
    temporary.replace(out)
    print(json.dumps({"hypothesis_id": report["hypothesis_id"],
                      "decision": report["decision"],
                      "decision_eligible": report["decision_eligible"],
                      "sealed_sessions": len(report["sealed_sessions"]),
                      "out": str(out)}, sort_keys=True))
    return 0


__all__ = [
    "ECONOMIC_HURDLE_BPS", "EVALUATION_SCHEMA", "OPENING_RANGE_FADE_HYPOTHESIS",
    "PREREGISTERED_HYPOTHESES",
    "PREREGISTRATION_SCHEMA", "PreregistrationError", "VWAP_REVERSION_HYPOTHESIS",
    "evaluate", "look_outcome", "main", "preregistration",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
