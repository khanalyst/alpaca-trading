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

A manifest hash freezes text only.  The v1 hypotheses showed why that is not
enough: their decision rule and the measuring code changed after sealed
sessions had been examined, and the hash could not see it (review finding
D1-01).  The v2 hypotheses therefore also register a digest of the
measurement instrument itself (:func:`instrument_identity`), and
:func:`evaluate` refuses a v2 decision whenever the running code differs.
They also define a countable session, decide each look exactly once into an
append-only look record, anchor the outcome where it could be traded, and
apply an after-cost hurdle to a confidence bound rather than to a point
estimate.  v1 is kept exactly as sealed so its stored results reproduce.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import date, datetime, time, timezone
from functools import lru_cache
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import rule_variant_id, validate_rule_spec

from .costs import diagnostic_backfill_policy
from .edge_ledger_store import content_hash
from .gates import PROTOCOL_QUALIFICATION_MIN_CLUSTERS
from .signal_quality import measure_signal_quality
from .stats import student_t_isf, student_t_sf


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

# --- v2 ---------------------------------------------------------------------
# Both v1 sealed windows are compromised (D1-01): the decision rule and the
# instrument changed after sealed sessions were examined, and every session up
# to V2_LAST_EXAMINED_SESSION has since been examined.  v2 registers the same
# two subjects on a fresh window and freezes the instrument as well as the
# text.  The dates and digests are in the registration block below.
PREREGISTRATION_SCHEMA_V2 = "preregistered-hypothesis.v2"
EVALUATION_SCHEMA_V2 = "preregistered-evaluation.v2"
LOOK_RECORD_SCHEMA = "preregistered-look-record.v1"
INSTRUMENT_IDENTITY_SCHEMA = "preregistered-instrument-identity.v1"
VWAP_REVERSION_HYPOTHESIS_V2 = "vwap-reversion-control-adjusted.v2"
OPENING_RANGE_FADE_HYPOTHESIS_V2 = "opening-range-fade-control-adjusted.v2"
V2_LOOK_SESSIONS = (30, 60)
# The repository's own static round trip at registration (research/costs.py):
# 2 x (4 bps spread / 2 + 6 bps adverse slippage + 0.5 bps fee) = 17 bps.
V2_ROUND_TRIP_COST_BPS = 17.0
V2_OUTCOME_ANCHOR = "next_bar_open"
V2_SENSITIVITY_ANCHOR = "skip_one_bar_open"
V2_DIAGNOSTIC_ANCHOR = "signal_close"
V2_HORIZON_CLOCK = "wall_clock"
# The configured 24-ETF universe at registration (config.yaml).
V2_UNIVERSE = (
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLI", "XLP",
    "XLY", "XLU", "XLB", "XLRE", "VTI", "VO", "VB", "EFA", "EEM", "TLT",
    "HYG", "GLD", "SLV", "SMH",
)
# A session counts only when it is a full regular session in the exact
# calendar, the recorder was up for nearly all of it, and most of the universe
# printed for at least half of it.  IEX is a thin venue, so the per-symbol
# floor is deliberately modest; the recorder floor is what catches an outage.
V2_MIN_SYMBOL_COVERAGE = 0.50
V2_MIN_COVERED_SYMBOLS = 18
V2_MIN_RECORDER_COVERAGE = 0.95
_NEW_YORK = ZoneInfo("America/New_York")
# The code whose identity is registered: everything these definitions reach
# through module-level names inside the research and agent packages.
_INSTRUMENT_ROOTS = (
    ("research.signal_quality", "measure_signal_quality"),
    ("research.preregistered", "_evaluate_v2"),
)
_INSTRUMENT_PACKAGES = ("research", "agent")
# === v2 registration literals ================================================
# Everything that is fixed at the moment v2 is published lives in this one
# block.  ``V2_SEALED_FROM`` must be the first full session after the
# registration is published (pushed), and later than every examined session.
# The instrument literals are the output of :func:`v2_registration_literals`
# on the published tree; ``evaluate`` recomputes the identity of the running
# code and refuses a v2 decision if it differs.  Once a sealed session has
# been examined, never change anything here to make a test pass: register a
# new version instead.  Every test and manifest reads these constants.
V2_LAST_EXAMINED_SESSION = "2026-10-23"
V2_REGISTERED_ON = "2026-10-28"
V2_SEALED_FROM = "2026-10-29"
V2_INSTRUMENT_COMPONENTS: dict[str, str] = {
    "agent.contracts.market_context":
        "fd6c1459b0d17d0d284d43d1b85d8a2561a5808b5efd647981b2ed36aa60589f",
    "agent.contracts.rule":
        "911d8bf3804557cd522bb0a13668d81cc00484c217460fc6162b60e4972e1df4",
    "agent.instruments":
        "13b3926e7f2bdcd92302ff6ca84153832ecde7a0e3e47028fe76ffde6eebb8f2",
    "research.edge_ledger_store":
        "62b0b03f6327b79d4654c96f966150258e61879c5acbe31c58449cfde08d77b4",
    "research.market_data":
        "10ef56f25e3be61e354f399b7c5980771eff7869a9a6cd944df9ddc21220d8c5",
    "research.maturity":
        "92b61078283b186fab6b9636070e013e2ab2a0e1987bc249fc2236e543d9be7b",
    "research.preregistered":
        "6b09ab54cba73f00f1d77ea46dc45c554759fcd67dfbe77195ba84bb38b42e05",
    "research.signal_quality":
        "7e8a64e054f2c4c2f9c2728746864d01c84edad556882af66990513c8776546b",
    "research.stats":
        "c990b10d4044a587bdc309b90c722b4f3f1daecfc40a0f8af382be8e845e4906",
}
V2_INSTRUMENT_DIGEST = (
    "a809497f623a21e04159dc0db20e6f3e1009dd78827e30308416d831e21b88da")
# === end of v2 registration literals =========================================


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


def _v2_shared() -> dict[str, Any]:
    """Return the v2 sections the two hypotheses share, written out once.

    Everything here is part of each manifest's hash.  The subject, claim and
    rationale stay specific to each hypothesis and are written out below.
    """
    return {
        "primary_endpoint": {
            "instrument": "research.signal_quality.measure_signal_quality",
            "metric": "candidate_minus_control_bps",
            "horizon_minutes": PRIMARY_HORIZON_MINUTES,
            "outcome_anchor": V2_OUTCOME_ANCHOR,
            "horizon_clock": V2_HORIZON_CLOCK,
            "sensitivity_anchor": V2_SENSITIVITY_ANCHOR,
            "diagnostic_anchor": V2_DIAGNOSTIC_ANCHOR,
            "anchor_rationale": (
                "the runtime acts on a completed signal bar and enters on the "
                "next bar, never at the signal bar's own last print, so the "
                "outcome runs from the open of the bar that starts when the "
                "signal bar ends; the one-bar-skip variant allows a full "
                "minute of decision latency and the signal-close figure is "
                "kept only as a diagnostic of bid-ask bounce"),
            "horizon_rule": (
                "the close of the bar that ends exactly 60 minutes after the "
                "entry instant; missing minutes in between move neither end, "
                "and a missing entry or horizon bar leaves the candidate "
                "without an outcome"),
            "control": (
                "same instrument, same session minute, other complete sessions "
                "of the same look window, with the candidate's own anchor and "
                "clock"),
            "control_tiers": [REGISTERED_CONTROL_TIER],
            "unmatched_candidates": (
                "a candidate with no control in the registered tier is left "
                "out of the statistics and counts as unmatched for coverage"),
            "inference_unit": "session",
            "statistics": [
                "cr1 session-clustered t against Student-t with G-1 df",
                "one-sided cluster sign-flip over whole sessions"],
        },
        "sealed_window": {
            "sessions_on_or_after": V2_SEALED_FROM,
            "rationale": (
                f"registered on {V2_REGISTERED_ON}, when every session up to "
                f"{V2_LAST_EXAMINED_SESSION} had been examined; the window "
                f"starts at {V2_SEALED_FROM}, the first full session after the "
                "registration was published, so no analysis can have seen it"),
            "control_pool": "restricted to the complete sessions of the look window",
        },
        "session_definition": {
            "calendar": (
                "exact Alpaca calendar metadata: every bar of the date carries "
                "the same session_open and session_close"),
            "regular_session": {"open": "09:30", "close": "16:00",
                                "timezone": "America/New_York"},
            "early_close": "excluded; it is not a regular session",
            "completed": (
                "the session date is before the evaluation date in New York "
                "and its close is at or before the evaluation instant, so the "
                "session in progress never counts"),
            "universe": list(V2_UNIVERSE),
            "min_symbol_coverage": V2_MIN_SYMBOL_COVERAGE,
            "min_covered_symbols": V2_MIN_COVERED_SYMBOLS,
            "min_recorder_coverage": V2_MIN_RECORDER_COVERAGE,
            "coverage_rule": (
                "a universe symbol is covered when it has a bar in at least "
                "min_symbol_coverage of the calendar minutes; a session counts "
                "when at least min_covered_symbols symbols are covered and at "
                "least min_recorder_coverage of its minutes carry a bar for "
                "some universe symbol"),
            "excluded_sessions": (
                "dropped entirely: they enter no look window, no control pool "
                "and no statistic"),
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
            {"look": 1, "sessions": V2_LOOK_SESSIONS[0],
             "alpha_one_sided": LOOK_ALPHA_ONE_SIDED},
            {"look": 2, "sessions": V2_LOOK_SESSIONS[1],
             "alpha_one_sided": LOOK_ALPHA_ONE_SIDED},
        ],
        "look_rule": (
            "each look uses exactly the first N complete sealed sessions in "
            "date order and is decided once: its look number, session list "
            "digest, instrument digest and result are appended to an "
            "append-only look record, and every later run must reproduce that "
            "record rather than decide again; a run that cannot reproduce it "
            "reports a conflict and decides nothing; alpha is split 0.025 per "
            "look, 0.05 overall, and no look follows a pass or futility"),
        "decision_rule": {
            "round_trip_cost_bps": V2_ROUND_TRIP_COST_BPS,
            "cost_origin": (
                "the static schedule in research/costs.py at registration: "
                "4 bps spread, 6 bps adverse slippage and 0.5 bps fee per side, "
                "2 x (2 + 6 + 0.5) = 17 bps per round trip"),
            "pass": (
                "the one-sided (1 - alpha) CR1 lower confidence bound of the "
                "mean control-adjusted delta is above round_trip_cost_bps AND "
                "the one-sided session sign-flip test of the deltas less "
                "round_trip_cost_bps has p < alpha"),
            "futility": (
                "the one-sided (1 - alpha) CR1 upper confidence bound is below "
                "round_trip_cost_bps: retire the hypothesis; decided on the "
                "registered-tier statistics, independently of how many "
                "candidates lacked a registered-tier control"),
            "inconclusive": (
                "neither; at look 1 continue to look 2, at look 2 the claim is "
                "not established and the hypothesis is retired"),
            "underpowered_control": (
                f"fewer than {MIN_MATCHED_CONTROLS} matched controls or "
                f"coverage below {MIN_CONTROL_COVERAGE:.0%} counts as "
                "inconclusive"),
            "before_first_look": "descriptive only; no decision and no early stop",
        },
        "instrument_identity": {
            "schema": INSTRUMENT_IDENTITY_SCHEMA,
            "method": (
                "sha256 over the exact source text of every module-level "
                "definition reachable from the roots through module-level "
                "names, following imports inside the research and agent "
                "packages"),
            "roots": [f"{module}:{name}" for module, name in _INSTRUMENT_ROOTS],
            "components": dict(V2_INSTRUMENT_COMPONENTS),
            "digest": V2_INSTRUMENT_DIGEST,
            "enforcement": (
                "evaluate() recomputes the identity of the running code and "
                "refuses any decision when it differs; the statistics are "
                "still reported as a diagnostic"),
        },
        "authorizes": (
            "a pass earns entry to the existing proof pipeline; it never "
            "authorizes trading, sizing, or a configuration change"),
    }


def _vwap_reversion_manifest_v2() -> dict[str, Any]:
    body = {
        "schema": PREREGISTRATION_SCHEMA_V2,
        "hypothesis_id": VWAP_REVERSION_HYPOTHESIS_V2,
        "registered_on": V2_REGISTERED_ON,
        "supersedes": {
            "hypothesis_id": VWAP_REVERSION_HYPOTHESIS,
            "manifest_hash": (
                "d2e56d80bdb0e367bcceebbe4792bf4a00aee815eb442e62f043ee2e40b7c48e"),
            "why": (
                "review finding D1-01: v1's decision rule and measurement code "
                "changed after sealed sessions had been examined, so its "
                "sealed window no longer tests the hypothesis as sealed"),
        },
        "claim": (
            "On the configured 24-ETF universe, entering at the open of the bar "
            "after the frozen session-VWAP reversion rule fires is followed over "
            "the next 60 minutes by a return, in the rule's own direction, that "
            "beats a clock-matched control by more than the repository's own "
            "17.0 bps round-trip cost, as a one-sided lower confidence bound."),
        "why_this_hypothesis": (
            "vwap_reversion led five in-sample measurements in the September "
            "2026 audit and kept its sign on the v1 sealed sessions (+3.5 bps "
            "control-adjusted over 16 sessions, not significant).  Those "
            "sessions are now examined, so only a fresh window can test it."),
        "subject": {
            "variant_id": _SUBJECT_ID,
            "rule_spec": _frozen(_SUBJECT_SPEC, _SUBJECT_ID),
            "provenance": (
                "diagnostic-shadow slot 7 baseline; already evaluated forward "
                "by the deployed shadow service"),
            "selection_note": "the same frozen subject as v1",
        },
        **_v2_shared(),
        "secondary_descriptive": [
            {"name": "mirror", "variant_id": _MIRROR_ID,
             "rule_spec": _frozen(_MIRROR_SPEC, _MIRROR_ID),
             "expectation": "negative 60-minute control-adjusted delta"},
            {"name": "subject_120m", "horizon_minutes": SECONDARY_HORIZON_MINUTES},
            {"name": "skip_one_bar_60m", "outcome_anchor": V2_SENSITIVITY_ANCHOR},
            {"name": "signal_close_60m", "outcome_anchor": V2_DIAGNOSTIC_ANCHOR},
        ],
        "multiplicity": (
            "one of two registered v2 hypotheses, each with its own alpha; "
            "neither is re-tested on the other's behalf"),
    }
    return {**body, "manifest_hash": content_hash(body)}


def _opening_range_fade_manifest_v2() -> dict[str, Any]:
    body = {
        "schema": PREREGISTRATION_SCHEMA_V2,
        "hypothesis_id": OPENING_RANGE_FADE_HYPOTHESIS_V2,
        "registered_on": V2_REGISTERED_ON,
        "supersedes": {
            "hypothesis_id": OPENING_RANGE_FADE_HYPOTHESIS,
            "manifest_hash": (
                "a0ae21f5ffd765142f2f4b92c47e33f652e0fb3213f1fd8d5a6d82c3915c2b9e"),
            "why": (
                "review finding D1-01: v1's decision rule and measurement code "
                "changed after sealed sessions had been examined, so its "
                "sealed window no longer tests the hypothesis as sealed"),
        },
        "claim": (
            "On the configured 24-ETF universe, entering at the open of the bar "
            "after the frozen opening-range fade rule fires is followed over the "
            "next 60 minutes by a return, in the rule's own direction, that "
            "beats a clock-matched control by more than the repository's own "
            "17.0 bps round-trip cost, as a one-sided lower confidence bound."),
        "why_this_hypothesis": (
            "opening_range_fade was the only catalogue arm positive after "
            "realistic costs in the September 2026 audit, and kept its sign on "
            "the v1 sealed sessions at about a tenth of its in-sample size "
            "(+2.0 bps over 14 sessions, not significant).  Those sessions are "
            "now examined, so only a fresh window can test it."),
        "subject": {
            "variant_id": _ORF_SUBJECT_ID,
            "rule_spec": _frozen(_ORF_SUBJECT_SPEC, _ORF_SUBJECT_ID),
            "provenance": (
                "diagnostic-shadow opening_range_fade baseline and the "
                "incumbent of deploy/paper-orf.config.json"),
            "selection_note": "the same frozen subject as v1",
        },
        **_v2_shared(),
        "secondary_descriptive": [
            {"name": "mirror", "variant_id": _ORF_MIRROR_ID,
             "rule_spec": _frozen(_ORF_MIRROR_SPEC, _ORF_MIRROR_ID),
             "expectation": "negative 60-minute control-adjusted delta"},
            {"name": "subject_120m", "horizon_minutes": SECONDARY_HORIZON_MINUTES},
            {"name": "skip_one_bar_60m", "outcome_anchor": V2_SENSITIVITY_ANCHOR},
            {"name": "signal_close_60m", "outcome_anchor": V2_DIAGNOSTIC_ANCHOR},
            {"name": "paper_trial",
             "profile": "deploy/paper-orf.config.json",
             "note": ("trade-level evidence on real paper fills under the "
                      "paper trial's own floors; never part of this decision")},
        ],
        "multiplicity": (
            "one of two registered v2 hypotheses, each with its own alpha; "
            "neither is re-tested on the other's behalf"),
    }
    return {**body, "manifest_hash": content_hash(body)}


_REGISTRY = {VWAP_REVERSION_HYPOTHESIS: _vwap_reversion_manifest,
             OPENING_RANGE_FADE_HYPOTHESIS: _opening_range_fade_manifest,
             VWAP_REVERSION_HYPOTHESIS_V2: _vwap_reversion_manifest_v2,
             OPENING_RANGE_FADE_HYPOTHESIS_V2: _opening_range_fade_manifest_v2}
PREREGISTERED_HYPOTHESES = tuple(_REGISTRY)
V2_HYPOTHESES = (VWAP_REVERSION_HYPOTHESIS_V2, OPENING_RANGE_FADE_HYPOTHESIS_V2)
# Review finding D1-01, recorded so every v1 run says so.  The v1 manifests
# and their evaluation are unchanged so stored results reproduce.
COMPROMISED_HYPOTHESES = {
    VWAP_REVERSION_HYPOTHESIS: (
        "compromised (D1-01): the control-tier decision rule was added on "
        "2026-09-22 after the first sealed interim, and the instrument "
        "(research/signal_quality.py, agent/contracts/rule.py) changed on "
        "2026-10-07 and 2026-10-08 while sealed sessions were being examined; "
        f"superseded by {VWAP_REVERSION_HYPOTHESIS_V2} on {V2_REGISTERED_ON}"),
    OPENING_RANGE_FADE_HYPOTHESIS: (
        "compromised (D1-01): the instrument (research/signal_quality.py, "
        "agent/contracts/rule.py) changed on 2026-10-07 and 2026-10-08 while "
        "sealed sessions were being examined; superseded by "
        f"{OPENING_RANGE_FADE_HYPOTHESIS_V2} on {V2_REGISTERED_ON}"),
}


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
        # Conformance with the sealed manifest, added after sealing (review
        # finding D1-01).  It runs before the futility branch, so a single
        # fallback control turns a pass *or a futility* into inconclusive
        # (D1-02).  It is kept unchanged so v1 results reproduce; v2 instead
        # leaves such a candidate unmatched and decides futility on its own.
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


# --- v2: instrument identity --------------------------------------------------

def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _definition_source(source: str, node: ast.AST) -> str:
    parts = [ast.get_source_segment(source, item) or ""
             for item in getattr(node, "decorator_list", [])]
    parts.append(ast.get_source_segment(source, node) or "")
    return "\n".join(parts)


def _index_source(module_name: str, source: str, package: str,
                  ) -> tuple[dict[str, list[tuple[str, tuple[str, ...]]]],
                             dict[str, tuple[str, str]]]:
    """Index a module's top-level definitions and ``from`` imports.

    Each definition keeps its exact source text and the names it mentions;
    each imported name keeps the module and name it resolves to.
    """
    tree = ast.parse(source)
    definitions: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    imports: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            relative = "." * node.level + (node.module or "")
            try:
                base = (importlib.util.resolve_name(relative, package)
                        if node.level else str(node.module))
            except (ImportError, ValueError):
                continue
            for alias in node.names:
                if alias.name != "*":
                    imports[alias.asname or alias.name] = (base, alias.name)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [item.id for target in targets for item in ast.walk(target)
                     if isinstance(item, ast.Name)]
        else:
            continue
        referenced = tuple(sorted({item.id for item in ast.walk(node)
                                   if isinstance(item, ast.Name)}))
        segment = _definition_source(source, node)
        for name in names:
            definitions.setdefault(name, []).append((segment, referenced))
    return definitions, imports


@lru_cache(maxsize=None)
def _module_index(module_name: str):
    module = importlib.import_module(module_name)
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    package = module.__package__ or module_name.rpartition(".")[0]
    return _index_source(module_name, source, package)


def _code_closure(roots: Sequence[tuple[str, str]], *,
                  packages: Sequence[str], index: Any,
                  ) -> dict[tuple[str, str], list[str]]:
    """Return the source digests of every definition the roots reach.

    The walk follows module-level names and ``from`` imports inside
    ``packages``.  It is deliberately name-based and over-inclusive: a local
    variable that shares a module-level name pulls that definition in too,
    which can only make the identity stricter.  Attribute access through an
    imported module object is not followed.
    """
    entries: dict[tuple[str, str], list[str]] = {}
    seen: set[tuple[str, str]] = set()
    queue = [(str(module), str(name)) for module, name in roots]
    while queue:
        module_name, name = queue.pop()
        if (module_name, name) in seen:
            continue
        seen.add((module_name, name))
        if module_name.split(".")[0] not in packages:
            continue
        definitions, imports = index(module_name)
        if name in definitions:
            digests = []
            for segment, referenced in definitions[name]:
                digests.append(hashlib.sha256(segment.encode("utf-8")).hexdigest())
                queue.extend((module_name, item) for item in referenced
                             if item in definitions or item in imports)
            entries[(module_name, name)] = digests
        elif name in imports:
            target = imports[name]
            entries[(module_name, name)] = [f"import:{target[0]}:{target[1]}"]
            queue.append(target)
        else:
            entries[(module_name, name)] = ["unresolved"]
    return entries


def instrument_identity(*, index: Any = None) -> dict[str, Any]:
    """Return the identity of the running measurement instrument.

    One digest per module over the source of every definition reached from
    :data:`_INSTRUMENT_ROOTS`, and one digest over those.  Editing code the
    instrument does not reach leaves it unchanged; editing anything it does
    reach, in any of these modules, changes it.
    """
    entries = _code_closure(_INSTRUMENT_ROOTS, packages=_INSTRUMENT_PACKAGES,
                            index=index or _module_index)
    components: dict[str, str] = {}
    for module in sorted({module for module, _name in entries}):
        items = sorted([name, entries[(owner, name)]]
                       for owner, name in entries if owner == module)
        components[module] = hashlib.sha256(_canonical_bytes(items)).hexdigest()
    return {"schema": INSTRUMENT_IDENTITY_SCHEMA,
            "roots": [f"{module}:{name}" for module, name in _INSTRUMENT_ROOTS],
            "definitions": len(entries), "components": components,
            "digest": hashlib.sha256(_canonical_bytes(components)).hexdigest()}


# --- v2: what counts as a session -------------------------------------------

def v2_session_quality(bars: Sequence[Any], definition: Mapping[str, Any], *,
                       first: date, as_of: datetime) -> dict[str, Any]:
    """Classify each date on or after ``first`` as a countable session or not.

    A countable session is a full regular session in the exact calendar
    carried by its bars, already closed at ``as_of`` and dated before it, with
    the registered recorder and per-symbol coverage.  Anything else is
    excluded with a reason, so a recorder outage, an early close or the
    session in progress can never be counted as one of a look's sessions.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = as_of.astimezone(timezone.utc)
    today = cutoff.astimezone(_NEW_YORK).date()
    universe = {str(symbol).upper() for symbol in definition["universe"]}
    regular = definition["regular_session"]
    opens_at = time.fromisoformat(str(regular["open"]))
    closes_at = time.fromisoformat(str(regular["close"]))
    by_day: dict[date, list[Any]] = {}
    for bar in bars:
        by_day.setdefault(_session_of(bar), []).append(bar)
    complete: list[str] = []
    excluded: Counter[str] = Counter()
    detail: list[dict[str, str]] = []

    def exclude(day: date, reason: str) -> None:
        excluded[reason] += 1
        if len(detail) < 400:
            detail.append({"session": day.isoformat(), "reason": reason})

    for day in sorted(by_day):
        if day < first:
            excluded["before_sealed_window"] += 1
            continue
        if day >= today:
            exclude(day, "current_or_future_date")
            continue
        rows = by_day[day]
        bounds = {(getattr(row, "session_open", None),
                   getattr(row, "session_close", None)) for row in rows}
        opened, closed = next(iter(bounds))
        if len(bounds) != 1 or opened is None or closed is None:
            exclude(day, "calendar_metadata_missing_or_conflicting")
            continue
        local_open, local_close = (opened.astimezone(_NEW_YORK),
                                   closed.astimezone(_NEW_YORK))
        if (local_open.date() != day or local_close.date() != day or
                local_open.time().replace(tzinfo=None) != opens_at):
            exclude(day, "not_a_regular_session")
            continue
        if local_close.time().replace(tzinfo=None) != closes_at:
            exclude(day, "early_or_irregular_close")
            continue
        if closed > cutoff:
            exclude(day, "session_not_closed")
            continue
        calendar_minutes = int((closed - opened).total_seconds() // 60)
        by_symbol: dict[str, set[int]] = {}
        recorded: set[int] = set()
        for row in rows:
            symbol = str(getattr(row, "symbol", "")).upper()
            stamp = getattr(row, "timestamp", None)
            if (symbol not in universe or not isinstance(stamp, datetime) or
                    int(getattr(row, "interval_seconds", 60) or 0) != 60 or
                    not opened <= stamp < closed):
                continue
            minute = int((stamp - opened).total_seconds() // 60)
            by_symbol.setdefault(symbol, set()).add(minute)
            recorded.add(minute)
        covered = sum(1 for minutes in by_symbol.values()
                      if len(minutes) >= float(definition["min_symbol_coverage"])
                      * calendar_minutes)
        if len(recorded) < float(definition["min_recorder_coverage"]) * calendar_minutes:
            exclude(day, "recorder_coverage_below_minimum")
            continue
        if covered < int(definition["min_covered_symbols"]):
            exclude(day, "symbol_coverage_below_minimum")
            continue
        complete.append(day.isoformat())
    return {"complete_sessions": complete,
            "excluded_counts": dict(sorted(excluded.items())),
            "excluded_sessions": detail,
            "as_of": cutoff.isoformat()}


# --- v2: decision rule -------------------------------------------------------

def _summary_v2(metric: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_summary(metric),
        "delta_hurdle_bps": metric.get("delta_hurdle_bps"),
        "after_cost_cluster_t_stat": metric.get(
            "candidate_minus_control_after_hurdle_cluster_t_stat"),
        "after_cost_sign_flip_p_value": metric.get(
            "candidate_minus_control_after_hurdle_sign_flip_p_value"),
        "candidate_mean_bps": metric.get("mean_forward_return_bps"),
        "candidate_mean_after_cost_bps": metric.get("mean_after_hurdle_bps"),
        "candidate_after_cost_cluster_t_stat": metric.get(
            "after_hurdle_cluster_t_stat"),
        "unavailable_reason_counts": dict(
            metric.get("unavailable_reason_counts") or {}),
    }


def look_outcome_v2(metric: Mapping[str, Any], *, alpha: float,
                    cost_bps: float) -> dict[str, Any]:
    """Apply the v2 decision rule to one look's primary endpoint.

    The hurdle is the round-trip cost, applied to a one-sided confidence
    bound of the executable control-adjusted delta rather than to its point
    estimate.  Only registered-tier controls are in ``metric`` (the
    instrument leaves other candidates unmatched), so there is no tier gate
    and futility is decided on the same statistics as a pass.
    """
    summary = _summary_v2(metric)
    candidates = summary["candidate_count"] or 0
    matched = summary["matched_count"] or 0
    if (matched < MIN_MATCHED_CONTROLS or not candidates or
            matched / candidates < MIN_CONTROL_COVERAGE):
        return {**summary, "outcome": "inconclusive",
                "reason": "underpowered_control"}
    mean, error = summary["delta_bps"], summary["cluster_stderr_bps"]
    df, flip_p = summary["cluster_df"], summary["after_cost_sign_flip_p_value"]
    if mean is None or not error or not df or flip_p is None:
        return {**summary, "outcome": "inconclusive",
                "reason": "clustered_inference_unavailable"}
    mean, error, df = float(mean), float(error), float(df)
    cost, level = float(cost_bps), float(alpha)
    critical = student_t_isf(level, df)
    t_cost = (mean - cost) / error
    detail = {**summary, "alpha_one_sided": level, "cost_bps": cost,
              "cr1_critical_t": critical,
              "delta_lower_bound_bps": mean - critical * error,
              "delta_upper_bound_bps": mean + critical * error,
              "cr1_p_above_cost": student_t_sf(t_cost, df),
              "cr1_p_below_cost": student_t_sf(-t_cost, df)}
    if detail["cr1_p_above_cost"] < level and float(flip_p) < level:
        return {**detail, "outcome": "pass",
                "reason": "lower_bound_clears_round_trip_cost"}
    if detail["cr1_p_below_cost"] < level:
        return {**detail, "outcome": "futility",
                "reason": "upper_bound_below_round_trip_cost"}
    return {**detail, "outcome": "inconclusive", "reason": "neither_rule_met"}


# --- v2: evaluation ---------------------------------------------------------

def _evaluate_v2(bars: Sequence[Any], manifest: Mapping[str, Any], *,
                 decision_eligible: bool, eligibility_reasons: Sequence[str],
                 provenance: Mapping[str, Any] | None,
                 look_records: Sequence[Mapping[str, Any]] | None,
                 as_of: datetime, policy: Any = None) -> dict[str, Any]:
    """Evaluate a v2 hypothesis; see :func:`evaluate`.

    The manifest is passed in rather than looked up so that this function,
    which is part of the registered instrument, never reaches the manifest
    that registers its own digest.  ``policy`` is the diagnostic replay
    policy, supplied only when the data are not decision-eligible; a
    decision-bearing run always measures with the default policy.
    """
    endpoint = manifest["primary_endpoint"]
    rule = manifest["decision_rule"]
    boundary = date.fromisoformat(manifest["sealed_window"]["sessions_on_or_after"])
    spec = manifest["subject"]["rule_spec"]
    mirror = next(item for item in manifest["secondary_descriptive"]
                  if item["name"] == "mirror")
    quality = v2_session_quality(bars, manifest["session_definition"],
                                 first=boundary, as_of=as_of)
    sessions = [date.fromisoformat(item) for item in quality["complete_sessions"]]
    counted = set(sessions)
    sealed = [bar for bar in bars if _session_of(bar) in counted]
    current = instrument_identity()
    registered = manifest["instrument_identity"]["digest"]
    refusals: list[str] = []
    if current["digest"] != registered:
        refusals.append("instrument_digest_mismatch")
    if look_records is None:
        refusals.append("look_record_store_missing")
    if decision_eligible:
        policy = None
    cost = float(rule["round_trip_cost_bps"])
    minutes = int(endpoint["horizon_minutes"])

    def measure(rows: Sequence[Any], rule_spec: Mapping[str, Any], anchor: str,
                horizons: Sequence[int]) -> Mapping[str, Any]:
        return measure_signal_quality(
            rows, rule_spec, policy=policy, horizons=horizons,
            outcome_anchor=anchor, horizon_clock=endpoint["horizon_clock"],
            control_tiers=endpoint["control_tiers"], delta_hurdle_bps=cost,
            cost_hurdle_bps=cost)

    interim: dict[str, Any] = {"sessions": len(sessions),
                               "decision": "none_before_first_look"}
    if sealed:
        subject = measure(sealed, spec, endpoint["outcome_anchor"],
                          (minutes, SECONDARY_HORIZON_MINUTES))
        interim.update({
            "primary_60m": _summary_v2(_horizon(subject, minutes)),
            "subject_120m": _summary_v2(_horizon(
                subject, SECONDARY_HORIZON_MINUTES)),
            "skip_one_bar_60m": _summary_v2(_horizon(measure(
                sealed, spec, endpoint["sensitivity_anchor"], (minutes,)),
                minutes)),
            "signal_close_60m": _summary_v2(_horizon(measure(
                sealed, spec, endpoint["diagnostic_anchor"], (minutes,)),
                minutes)),
            "mirror_60m": _summary_v2(_horizon(measure(
                sealed, mirror["rule_spec"], endpoint["outcome_anchor"],
                (minutes,)), minutes)),
        })

    recorded: dict[int, Mapping[str, Any]] = {}
    for item in look_records or ():
        if (isinstance(item, Mapping) and
                item.get("hypothesis_id") == manifest["hypothesis_id"]):
            recorded.setdefault(int(item.get("look", 0)), item)
    looks: list[dict[str, Any]] = []
    new_records: list[dict[str, Any]] = []
    conflicts: list[int] = []
    outcome = "accruing"
    for look in manifest["looks"]:
        number, needed = int(look["look"]), int(look["sessions"])
        prior = recorded.get(number)
        if outcome != "accruing":
            looks.append({"look": number, "sessions_required": needed,
                          "status": "not_needed_after_terminal_decision"})
            continue
        if len(sessions) < needed:
            entry: dict[str, Any] = {"look": number, "sessions_required": needed,
                                     "status": "not_reached"}
            if prior is not None:
                entry.update({"look_record_status": "conflict",
                              "look_record_mismatch": ["sessions_available"]})
                conflicts.append(number)
            looks.append(entry)
            continue
        window = sessions[:needed]
        window_set = set(window)
        subset = [bar for bar in sealed if _session_of(bar) in window_set]
        result = look_outcome_v2(
            _horizon(measure(subset, spec, endpoint["outcome_anchor"],
                             (minutes,)), minutes),
            alpha=float(look["alpha_one_sided"]), cost_bps=cost)
        session_list = [item.isoformat() for item in window]
        record = {
            "schema": LOOK_RECORD_SCHEMA,
            "hypothesis_id": manifest["hypothesis_id"],
            "manifest_hash": manifest["manifest_hash"],
            "look": number, "sessions_required": needed,
            "session_list": session_list,
            "session_list_digest": content_hash(session_list),
            "instrument_digest": current["digest"],
            "result": result, "result_digest": content_hash(result),
        }
        entry = {"look": number, "sessions_required": needed,
                 "window_first": session_list[0],
                 "window_last": session_list[-1],
                 "status": "evaluated", **result,
                 "session_list_digest": record["session_list_digest"],
                 "skip_one_bar_60m": _summary_v2(_horizon(measure(
                     subset, spec, endpoint["sensitivity_anchor"], (minutes,)),
                     minutes)),
                 "signal_close_60m": _summary_v2(_horizon(measure(
                     subset, spec, endpoint["diagnostic_anchor"], (minutes,)),
                     minutes))}
        if prior is not None:
            mismatch = [field for field in (
                "manifest_hash", "session_list_digest", "instrument_digest",
                "result_digest") if prior.get(field) != record[field]]
            decided = str((prior.get("result") or {}).get("outcome") or "")
            entry.update({"look_record_status": (
                              "conflict" if mismatch else "reproduced"),
                          "look_record_mismatch": mismatch,
                          "recorded_outcome": decided})
            if mismatch:
                conflicts.append(number)
        else:
            decided = result["outcome"]
            if decision_eligible and not refusals and not conflicts:
                entry["look_record_status"] = "recorded_now"
                new_records.append(record)
            else:
                entry["look_record_status"] = "not_recorded"
        looks.append(entry)
        if decided in {"pass", "futility"}:
            outcome = decided
        elif number == manifest["looks"][-1]["look"]:
            outcome = "not_established"

    if not decision_eligible:
        decision = "diagnostic_only"
    elif refusals:
        decision = "refused"
    elif conflicts:
        decision = "look_record_conflict"
    else:
        decision = outcome
    return {
        "schema": EVALUATION_SCHEMA_V2,
        "hypothesis_id": manifest["hypothesis_id"],
        "manifest_hash": manifest["manifest_hash"],
        "authorizing": False,
        "trading_authorized": False,
        "decision_eligible": bool(decision_eligible),
        "eligibility_reasons": list(eligibility_reasons),
        "decision_refusals": refusals,
        "instrument_identity": {
            "registered_digest": registered,
            "current_digest": current["digest"],
            "matches": current["digest"] == registered,
            "current_components": current["components"],
        },
        "sealed_from": boundary.isoformat(),
        "as_of": quality["as_of"],
        "session_quality": {key: quality[key] for key in (
            "excluded_counts", "excluded_sessions")},
        "sealed_sessions": [item.isoformat() for item in sessions],
        "excluded_bars": len(bars) - len(sealed),
        "interim": interim,
        "looks": looks,
        "look_records_to_append": new_records,
        "look_record_conflicts": conflicts,
        "decision": decision,
        "rule_outcome_if_eligible": outcome,
        "provenance": dict(provenance or {}),
    }


def evaluate(bars: Sequence[Any], *,
             hypothesis_id: str = VWAP_REVERSION_HYPOTHESIS,
             decision_eligible: bool,
             eligibility_reasons: Sequence[str] = (),
             provenance: Mapping[str, Any] | None = None,
             look_records: Sequence[Mapping[str, Any]] | None = None,
             as_of: datetime | None = None) -> dict[str, Any]:
    """Evaluate a registered hypothesis on the sealed window of ``bars``.

    ``decision_eligible`` must come from the caller's source preflight, never
    from the bars' own defaults: a normalized bar reports
    ``forward_observed`` unless told otherwise, which is not evidence.

    A v1 hypothesis is evaluated exactly as it was sealed and ignores
    ``look_records`` and ``as_of``.  A v2 hypothesis counts only complete
    sessions closed by ``as_of`` (default: now), reproduces any look already in
    ``look_records`` instead of deciding it again, returns the records a
    decision-bearing run must append, and decides nothing at all when the
    running instrument differs from the registered one or no look record
    store was supplied (``look_records=None``).
    """
    manifest = preregistration(hypothesis_id)
    if manifest["schema"] == PREREGISTRATION_SCHEMA_V2:
        moment = as_of if as_of is not None else datetime.now(timezone.utc)
        return _evaluate_v2(
            bars, manifest, decision_eligible=decision_eligible,
            eligibility_reasons=eligibility_reasons, provenance=provenance,
            look_records=look_records, as_of=moment,
            policy=None if decision_eligible else diagnostic_backfill_policy())
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


def v2_registration_literals() -> str:
    """Return the v2 registration literals for the code that is running now.

    The output is Python source to paste: the instrument block for this
    module, then the pins for ``tests/research/test_preregistered_v2.py``.
    The manifest hashes are those the manifests will have once the printed
    instrument literals are in place.  Print it with::

        python -B -c "from research.preregistered import v2_registration_literals as f; print(f())"
    """
    identity = instrument_identity()
    module = globals()
    saved = (module["V2_INSTRUMENT_COMPONENTS"], module["V2_INSTRUMENT_DIGEST"])
    try:
        module["V2_INSTRUMENT_COMPONENTS"] = dict(identity["components"])
        module["V2_INSTRUMENT_DIGEST"] = identity["digest"]
        hashes = {hypothesis: preregistration(hypothesis)["manifest_hash"]
                  for hypothesis in V2_HYPOTHESES}
    finally:
        module["V2_INSTRUMENT_COMPONENTS"], module["V2_INSTRUMENT_DIGEST"] = saved
    lines = ["# research/preregistered.py (registration block)",
             "V2_INSTRUMENT_COMPONENTS: dict[str, str] = {"]
    for name, digest in identity["components"].items():
        lines += [f'    "{name}":', f'        "{digest}",']
    lines += ["}", "V2_INSTRUMENT_DIGEST = (", f'    "{identity["digest"]}")', "",
              "# tests/research/test_preregistered_v2.py",
              "V2_VWAP_MANIFEST_HASH = (",
              f'    "{hashes[VWAP_REVERSION_HYPOTHESIS_V2]}")',
              "V2_ORF_MANIFEST_HASH = (",
              f'    "{hashes[OPENING_RANGE_FADE_HYPOTHESIS_V2]}")',
              "V2_INSTRUMENT_DIGEST = (", f'    "{identity["digest"]}")']
    return "\n".join(lines)


def read_look_records(stream: Any) -> list[dict[str, Any]]:
    """Parse an append-only JSONL look record; a malformed line is refused."""
    records: list[dict[str, Any]] = []
    for number, line in enumerate(stream, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PreregistrationError(
                f"look record line {number} is not JSON: {exc}") from exc
        if not isinstance(item, dict) or item.get("schema") != LOOK_RECORD_SCHEMA:
            raise PreregistrationError(
                f"look record line {number} is not a {LOOK_RECORD_SCHEMA} record")
        records.append(item)
    return records


def _evaluate_with_look_record(path: Path, bars: Sequence[Any], *,
                               hypothesis_id: str, decision_eligible: bool,
                               eligibility_reasons: Sequence[str],
                               provenance: Mapping[str, Any],
                               as_of: datetime | None) -> dict[str, Any]:
    """Evaluate under an exclusive lock and append any newly decided looks.

    The lock spans read, evaluation and append, so two concurrent runs cannot
    both decide the same look.  Records are only ever appended.
    """
    import fcntl
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.seek(0)
            records = read_look_records(stream)
            report = evaluate(bars, hypothesis_id=hypothesis_id,
                              decision_eligible=decision_eligible,
                              eligibility_reasons=eligibility_reasons,
                              provenance=provenance, look_records=records,
                              as_of=as_of)
            appended = report.get("look_records_to_append") or []
            if appended:
                stream.seek(0, os.SEEK_END)
                for record in appended:
                    stream.write(json.dumps(record, sort_keys=True,
                                            separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True,
                        help="normalized JSONL corpus or session partition directory")
    parser.add_argument("--out", required=True,
                        help="new report path; an existing file is never replaced")
    parser.add_argument("--hypothesis", default=VWAP_REVERSION_HYPOTHESIS,
                        choices=PREREGISTERED_HYPOTHESES)
    parser.add_argument("--look-records", default=None,
                        help=("append-only JSONL look record (required for v2 "
                              "hypotheses); a decided look is appended once and "
                              "reproduced by every later run"))
    parser.add_argument("--as-of", default=None,
                        help="evaluation instant for v2 (ISO 8601 with offset; default now)")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        parser.error(f"refusing to replace existing report {out}")
    manifest = preregistration(args.hypothesis)
    v2 = manifest["schema"] == PREREGISTRATION_SCHEMA_V2
    if v2 and not args.look_records:
        parser.error("v2 hypotheses require --look-records")
    as_of = None
    if args.as_of:
        try:
            as_of = datetime.fromisoformat(str(args.as_of).replace("Z", "+00:00"))
        except ValueError:
            parser.error("--as-of must be an ISO 8601 timestamp")
        if as_of.tzinfo is None:
            parser.error("--as-of must carry a UTC offset")
    if args.hypothesis in COMPROMISED_HYPOTHESES:
        print(f"warning: {args.hypothesis} is "
              f"{COMPROMISED_HYPOTHESES[args.hypothesis]}", file=sys.stderr)
    bars, eligible, reasons, provenance = _load(Path(args.data), manifest)
    if v2:
        report = _evaluate_with_look_record(
            Path(args.look_records), bars, hypothesis_id=args.hypothesis,
            decision_eligible=eligible, eligibility_reasons=reasons,
            provenance=provenance, as_of=as_of)
    else:
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
    "COMPROMISED_HYPOTHESES", "ECONOMIC_HURDLE_BPS", "EVALUATION_SCHEMA",
    "EVALUATION_SCHEMA_V2", "LOOK_RECORD_SCHEMA",
    "OPENING_RANGE_FADE_HYPOTHESIS", "OPENING_RANGE_FADE_HYPOTHESIS_V2",
    "PREREGISTERED_HYPOTHESES", "PREREGISTRATION_SCHEMA",
    "PREREGISTRATION_SCHEMA_V2", "PreregistrationError",
    "V2_HYPOTHESES", "VWAP_REVERSION_HYPOTHESIS", "VWAP_REVERSION_HYPOTHESIS_V2",
    "evaluate", "instrument_identity", "look_outcome", "look_outcome_v2",
    "main", "preregistration", "read_look_records", "v2_registration_literals",
    "v2_session_quality",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
