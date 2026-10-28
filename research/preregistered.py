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
import math
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
INSTRUMENT_IDENTITY_SCHEMA = "preregistered-instrument-identity.v2"
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
# The code whose identity is registered: every module these modules can
# import, at any depth, inside the repository's own packages.
_INSTRUMENT_ROOT_MODULES = ("research.preregistered", "research.signal_quality")
_INSTRUMENT_PACKAGES = ("research", "agent", "deploy")
# Built so that the marker text appears in this file only at the block.
_REGISTRATION_BEGIN = "# " + "=== v2 registration literals"
_REGISTRATION_END = "# " + "=== end of v2 registration literals"
# === v2 registration literals ================================================
# Everything that is fixed at the moment v2 is published lives in this one
# block.  ``V2_SEALED_FROM`` must be the first full session after the
# registration is published (pushed), and later than every examined session.
# The instrument and manifest literals are the output of
# :func:`v2_registration_literals` on the published tree; ``evaluate``
# recomputes the identity of the running code and the manifest hashes and
# refuses a v2 decision if either differs.  This block is the only part of
# this module outside the instrument identity.  Once a sealed session has
# been examined, never change anything here to make a test pass: register a
# new version instead.  Every test and manifest reads these constants.
V2_LAST_EXAMINED_SESSION = "2026-10-23"
V2_REGISTERED_ON = "2026-10-28"
V2_SEALED_FROM = "2026-10-29"
V2_INSTRUMENT_COMPONENTS: dict[str, str] = {
    "agent":
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "agent.alpaca_domain":
        "63f648a92c89a37d7af64bf7a044111a07d77d70d40fab535e3ff6395811afa6",
    "agent.alpaca_market_data":
        "f50f77cc9548ed2bea59f118e213a220c5bb9b9a452eac00468ead0e27dfece3",
    "agent.alpaca_provider":
        "a444bcba267200dec627a58cef31f2bc16597a2843841a25622f81638e7171da",
    "agent.alpaca_sdk":
        "e6dc6cc4a8a63952269bbd483f517140af28c671d47fd4701698b5b5d720d83b",
    "agent.alpaca_session":
        "99bf3c6f720075c036ef5e88474c9ba6fcba0b0e8a4ac16cc0a6072a9e74782d",
    "agent.config":
        "0d0ee869ef54d7591b3fcfaef31d11818f8221a41c4134d112dd4d1d4f108c4f",
    "agent.contracts":
        "78ee9d24545aeb6e710a2078cf8532f04bda0ef164bba3ab2cd5b0f9ffbf8606",
    "agent.contracts.ibr":
        "832e3270aeb16b33b7a4f97bcdc1c5054d255efd1f10b2c9bf38338d9eb6cf0e",
    "agent.contracts.market_context":
        "4944ca5e877d659fe60425b41c672d6f7dabeefafb8cd03b89db749e5a7516b7",
    "agent.contracts.risk_geometry":
        "a5af0cf5eb4db6d3d6fd26a8c7d0db53bca5c9e189ffa54b50032c421ece6dd9",
    "agent.contracts.rule":
        "88b6c195b3173f5b07b99c3ab672aab147a84416dddba2db221c4a9efb44c216",
    "agent.governance":
        "2faec8c3b178ad30255c56cf2b2504661b11aa689726666b3dd816de3ce5b1b0",
    "agent.instruments":
        "5f6f068767db7fceb5d2501c38ee20d6239254dfa49d5de2e39afcb83ea49fa4",
    "agent.journal":
        "388fa06ec79ca9b4ea577fee01d64f3894e0f1816a7a4cba34da105c95f3dd5e",
    "agent.order_timing":
        "0845e5da46a303ee2fa1b98a1bd672945e3f3cd13f1f7d03cb984bac38584712",
    "agent.paper_trial":
        "ea47fa68479a9be9944f36fb0dc89fb196034ebb4ae53de755ad84069b9d46e9",
    "agent.paper_trial_operator":
        "b2c0f793dfcd424042f8c99df65a7a02978d875c9d9182a0a671a3f61a94a006",
    "agent.registry":
        "9478e69d8a6586ef06a2ec52dfd2a0db23a50e29d9e7c3c7f57435f230353897",
    "agent.state":
        "f6384c743c896773f07b8321ac4a2ead42b2eea49511d8c383a61a8490edadc6",
    "agent.state_store":
        "c4276e4581d57545979e79379d951dee6ca62009c1a746680f06458e8f0a516e",
    "agent.variants":
        "44799208776154dfc96e6fa00c49fcfaa8bbc0a006cd00fc5d4abdc54ef5a568",
    "research":
        "bc65c6643fa0aa9b248e23ae8a2d65847182dcd707ce4dac12e3d9ee7671a5b4",
    "research.costs":
        "e862873d7969e910f6fc9b05ef92b1a96aeaea28094922e3bb14fdcdfc5a0037",
    "research.diagnostic_cohort_contract":
        "1b4926d0eeb56b1d9373758e2c624674b73156322cb1a6cfb0a83069e07c02e2",
    "research.diagnostic_shadow":
        "cd1ac019f416dfd2f8703327be1de006194c3821b5bd11ac8e57e2e601ada104",
    "research.edge_discovery_core":
        "eddc333e787b34a2be86fb5cbb2691822332578ff3aebff86fb3b24e7897a9ed",
    "research.edge_identity":
        "bfeafba2b4a3fb8cd970586f216aca98ec75676f470d09d3fd92c14010b9c4aa",
    "research.edge_lab":
        "66f53c8aaa82a8d56066f67ccf7b563c6d126d9b7fc446b30b9369057f9c7dec",
    "research.edge_ledger":
        "9fc781f94bb9eac728d1a6cdad1363fdbae81b1a9050540492a93035e9a88ddd",
    "research.edge_ledger_proof":
        "1382357c850c991a415355061d97959f15476d48efe07f9040cd2c2fc3d4ac22",
    "research.edge_ledger_store":
        "878628b48fa550e59ef6c0a48ec167bcf3a53e69978baa77298f938bf96b870a",
    "research.factory_core":
        "80b8a52fd0d469d5153a7cd7d5749f060683d0bf924f60464323a820656be887",
    "research.factory_ledger":
        "567240cbb77efb03b347b21ac89f836939123fc1e50f53525c203a3586659c98",
    "research.gates":
        "49681df3fe3b795326984d61e757b8d1621f86c07263f83037d52956ec014750",
    "research.ibr":
        "90f0b08b6a830a9b2d3b62f9379adaff02adb0344ec1eed9ab0c7b656f813f0d",
    "research.market_data":
        "a199a4364b8940fe19b7906344242faec2f89fd4df931a1babd86b027b184fad",
    "research.maturity":
        "0e37eba5522d7bc6f90d0642faf0260fb4e60e72dac322a5cae083a9ea28afde",
    "research.path_telemetry":
        "ce14697bf785c41f0fd1668a5299b93a392d5a2ffdf53d5da9b1c85721928665",
    "research.preregistered":
        "9f6743e7751a789d76883af04beb482eb7a9a446641a699602ef7cc9a7c56acf",
    "research.quote_costs":
        "c3c296f48207160b30a4bcb0e4549789846aa25b5e4d16e9e633f9fee8e9b335",
    "research.signal_quality":
        "9b4c7ebc29a7e75878922378c6639a8076f4a6c131c6eb4e2f6bc2fc888e5e1e",
    "research.source_validation":
        "ec97ee2220a8bfded56c69c4f3fba9b415af4cb49b953c847f84eaec26f00ef8",
    "research.stats":
        "3dc4c2df18bea3f760755ca7566289572b645209de3978d6799aa056800b8791",
    "research.stressed_cost_calibration":
        "8695d7d30b36a3fcdcb6044c0a72219849988d794b34e23852105b6d7348db84",
    "research.trial":
        "0f209ee5d7fd8ffaa029f8b094b2a2c4c53fee4ae2db19c95e1d20636feff1cc",
}
V2_INSTRUMENT_DIGEST = (
    "1dced3ad98f1aa089696c929773d5388e5ecd2993d2821cbd190108eb8325e31")
V2_MANIFEST_HASHES: dict[str, str] = {
    "vwap-reversion-control-adjusted.v2":
        "d0003f117e8e47a5b15653f645d0fa2ab72463e1d289ef118406c77774eb6b93",
    "opening-range-fade-control-adjusted.v2":
        "22b7ca355aa42b72c766c5d56d829ba5f14618a010d29aceb4ce75caeab7c13f",
}
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
            "universe_rule": (
                "bars of any other symbol are dropped before anything is "
                "measured, so a change to the recorder's symbol list cannot "
                "change the tested population"),
            "evaluation_instant": (
                "an evaluation instant later than the wall clock is refused, "
                "so a session still trading can never be counted"),
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
            "record rather than decide again; a run that cannot reproduce it, "
            "a record whose result does not match its own digest, or a second "
            "record for the same look is a conflict that decides nothing; "
            "alpha is split 0.025 per look, 0.05 overall, and no look follows "
            "a pass or futility"),
        "disclosure_rule": (
            "no statistic computed on sealed sessions is reported before a "
            "look is decided and recorded: interim runs, refused runs, "
            "diagnostic runs and runs that hit a look-record conflict report "
            "session counts only, and a later look is not evaluated until the "
            "earlier one is recorded"),
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
                "round_trip_cost_bps has p < alpha AND the candidate's own "
                "mean return less round_trip_cost_bps has a CR1 t statistic "
                "above the same critical value, so a pass cannot coexist with "
                "a money-losing executable return"),
            "futility": (
                "on a look that meets the control floor below, the one-sided "
                "(1 - alpha) CR1 upper confidence bound is below "
                "round_trip_cost_bps: retire the hypothesis; an underpowered "
                "look is inconclusive whatever its bounds"),
            "inconclusive": (
                "neither; at look 1 continue to look 2, at look 2 the claim is "
                "not established and the hypothesis is retired"),
            "underpowered_control": (
                f"fewer than {MIN_MATCHED_CONTROLS} matched controls or "
                f"coverage below {MIN_CONTROL_COVERAGE:.0%} counts as "
                "inconclusive"),
            "before_first_look": (
                "nothing is computed or reported; no decision and no early stop"),
        },
        "instrument_identity": {
            "schema": INSTRUMENT_IDENTITY_SCHEMA,
            "method": (
                "sha256 over the exact source of every module the root modules "
                "can import, at any depth and inside function bodies, within "
                "the research, agent and deploy packages, with each package "
                "__init__; this module's registration literal block is the "
                "only text left out"),
            "roots": list(_INSTRUMENT_ROOT_MODULES),
            "components": dict(V2_INSTRUMENT_COMPONENTS),
            "digest": V2_INSTRUMENT_DIGEST,
            "enforcement": (
                "evaluate() recomputes the identity of the running code and "
                "the manifest hash and refuses any decision when either "
                "differs; a refused run reports no statistics.  Decisions are "
                "made from a checkout of the registration commit"),
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


def _registration_free_source(module_name: str, source: str) -> str:
    """The module's source without the v2 registration literal block.

    The block holds the identity of everything else, so it cannot be part of
    that identity.  Every other byte of the module is.
    """
    if module_name != __name__:
        return source
    begin = source.find(_REGISTRATION_BEGIN)
    finish = source.find(_REGISTRATION_END)
    if begin < 0 or finish < begin:
        raise PreregistrationError("the v2 registration block markers are missing")
    finish = source.index("\n", finish) + 1
    return source[:begin] + source[finish:]


def _imported_modules(source: str, package: str) -> set[str]:
    """Every module an import statement anywhere in *source* may load.

    Imports inside function bodies count: a reader imported lazily is as much
    a part of the instrument as one imported at the top.  ``from x import y``
    contributes ``x`` and, in case ``y`` is a submodule, ``x.y``.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            relative = "." * node.level + (node.module or "")
            try:
                base = (importlib.util.resolve_name(relative, package)
                        if node.level else str(node.module))
            except (ImportError, ValueError):
                continue
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names
                         if alias.name != "*")
    return found


def _module_source(module_name: str) -> tuple[str, str] | None:
    """Return (source, package) for a pure-Python module, else ``None``."""
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError, AttributeError):
        return None
    if spec is None or not spec.origin or not str(spec.origin).endswith(".py"):
        return None
    package = (module_name if spec.submodule_search_locations is not None
               else module_name.rpartition(".")[0])
    return Path(spec.origin).read_text(encoding="utf-8"), package


def _module_closure(roots: Sequence[str], *, packages: Sequence[str],
                    read: Any) -> dict[str, str]:
    """Return the exact source of every module the roots can import.

    The walk follows every import statement, at any depth in a module, that
    stays inside ``packages``, and adds each package's ``__init__`` on the
    way.  It hashes whole modules on purpose: a module-level statement, a
    constant mutated at import time or a lazily imported reader can change a
    measurement as surely as a function body can (review of D1-01).
    """
    sources: dict[str, str] = {}
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in sources or name.split(".")[0] not in packages:
            continue
        found = read(name)
        if found is None:
            continue
        source, package = found
        sources[name] = _registration_free_source(name, source)
        parts = name.split(".")
        queue.extend(".".join(parts[:end]) for end in range(1, len(parts)))
        queue.extend(_imported_modules(source, package))
    return sources


def instrument_identity(*, read: Any = None) -> dict[str, Any]:
    """Return the identity of the running measurement instrument.

    One sha256 per module over its exact source (this module's registration
    block excepted) for every module in :func:`_module_closure` of
    :data:`_INSTRUMENT_ROOT_MODULES`, and one digest over those.  Any edit to
    any of those modules, even a comment, changes it.  A v2 decision is
    therefore made from a checkout of the registration commit (see
    docs/preregistered-hypotheses.md); the running tree is not expected to
    keep matching as the repository moves on.
    """
    sources = _module_closure(_INSTRUMENT_ROOT_MODULES,
                              packages=_INSTRUMENT_PACKAGES,
                              read=read or _module_source)
    components = {name: hashlib.sha256(sources[name].encode("utf-8")).hexdigest()
                  for name in sorted(sources)}
    return {"schema": INSTRUMENT_IDENTITY_SCHEMA,
            "roots": list(_INSTRUMENT_ROOT_MODULES),
            "modules": len(components), "components": components,
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
    estimate, and the candidate's own return less that cost must clear the
    same critical value.  Only registered-tier controls are in ``metric``
    (the instrument leaves other candidates unmatched), so there is no tier
    gate; an underpowered look is inconclusive before either rule is read.
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
    candidate_t = summary.get("candidate_after_cost_cluster_t_stat")
    detail["candidate_after_cost_clears"] = (
        candidate_t is not None and math.isfinite(float(candidate_t)) and
        float(candidate_t) > critical)
    if (detail["cr1_p_above_cost"] < level and float(flip_p) < level and
            detail["candidate_after_cost_clears"]):
        return {**detail, "outcome": "pass",
                "reason": "lower_bound_clears_round_trip_cost"}
    if detail["cr1_p_below_cost"] < level:
        return {**detail, "outcome": "futility",
                "reason": "upper_bound_below_round_trip_cost"}
    return {**detail, "outcome": "inconclusive", "reason": "neither_rule_met"}


# --- v2: evaluation ---------------------------------------------------------

def _utc_now() -> datetime:
    """The wall clock an evaluation instant may not exceed."""
    return datetime.now(timezone.utc)


def _evaluate_v2(bars: Sequence[Any], manifest: Mapping[str, Any], *,
                 decision_eligible: bool, eligibility_reasons: Sequence[str],
                 provenance: Mapping[str, Any] | None,
                 look_records: Sequence[Mapping[str, Any]] | None,
                 as_of: datetime, policy: Any = None,
                 refused_because: Sequence[str] = ()) -> dict[str, Any]:
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
    universe = {str(symbol).upper()
                for symbol in manifest["session_definition"]["universe"]}
    received = len(bars)
    bars = [bar for bar in bars
            if str(getattr(bar, "symbol", "")).upper() in universe]
    quality = v2_session_quality(bars, manifest["session_definition"],
                                 first=boundary, as_of=as_of)
    sessions = [date.fromisoformat(item) for item in quality["complete_sessions"]]
    counted = set(sessions)
    sealed = [bar for bar in bars if _session_of(bar) in counted]
    current = instrument_identity()
    registered = manifest["instrument_identity"]["digest"]
    refusals: list[str] = list(refused_because)
    if current["digest"] != registered:
        refusals.append("instrument_digest_mismatch")
    if as_of.astimezone(timezone.utc) > _utc_now():
        refusals.append("as_of_in_future")
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

    # Nothing measured on sealed sessions is reported before a look is
    # decided and recorded (the manifest's disclosure rule).
    interim: dict[str, Any] = {"sessions": len(sessions),
                               "decision": "none_before_first_look",
                               "statistics": "withheld"}

    recorded: dict[int, list[Mapping[str, Any]]] = {}
    for item in look_records or ():
        if (isinstance(item, Mapping) and
                item.get("hypothesis_id") == manifest["hypothesis_id"]):
            recorded.setdefault(int(item.get("look", 0)), []).append(item)
    looks: list[dict[str, Any]] = []
    new_records: list[dict[str, Any]] = []
    conflicts: list[int] = []
    outcome = "accruing"
    withheld = False
    for look in manifest["looks"]:
        number, needed = int(look["look"]), int(look["sessions"])
        priors = recorded.get(number, [])
        if outcome != "accruing":
            looks.append({"look": number, "sessions_required": needed,
                          "status": "not_needed_after_terminal_decision"})
            continue
        if withheld:
            looks.append({"look": number, "sessions_required": needed,
                          "status": "blocked_until_earlier_look_is_recorded"})
            continue
        if len(sessions) < needed:
            entry: dict[str, Any] = {"look": number, "sessions_required": needed,
                                     "status": "not_reached"}
            if priors:
                entry.update({"look_record_status": "conflict",
                              "look_record_mismatch": ["sessions_available"]})
                conflicts.append(number)
            looks.append(entry)
            continue
        window = sessions[:needed]
        session_list = [item.isoformat() for item in window]
        entry = {"look": number, "sessions_required": needed,
                 "window_first": session_list[0],
                 "window_last": session_list[-1],
                 "session_list_digest": content_hash(session_list)}
        if not decision_eligible or refusals or conflicts:
            # Neither recordable nor reproducible here: measure nothing.
            entry.update({"status": "withheld", "look_record_status": "not_recorded"})
            looks.append(entry)
            withheld = True
            continue
        window_set = set(window)
        subset = [bar for bar in sealed if _session_of(bar) in window_set]
        result = look_outcome_v2(
            _horizon(measure(subset, spec, endpoint["outcome_anchor"],
                             (minutes,)), minutes),
            alpha=float(look["alpha_one_sided"]), cost_bps=cost)
        record = {
            "schema": LOOK_RECORD_SCHEMA,
            "hypothesis_id": manifest["hypothesis_id"],
            "manifest_hash": manifest["manifest_hash"],
            "look": number, "sessions_required": needed,
            "session_list": session_list,
            "session_list_digest": entry["session_list_digest"],
            "instrument_digest": current["digest"],
            "result": result, "result_digest": content_hash(result),
        }
        if priors:
            prior = priors[0]
            mismatch = [field for field in (
                "manifest_hash", "session_list_digest", "instrument_digest",
                "result_digest") if prior.get(field) != record[field]]
            try:
                intact = content_hash(prior.get("result")) == prior.get("result_digest")
            except (TypeError, ValueError):
                intact = False
            if not intact:
                mismatch.append("recorded_result_does_not_match_its_digest")
            if len(priors) > 1:
                mismatch.append("duplicate_look_record")
            entry.update({"look_record_status": (
                              "conflict" if mismatch else "reproduced"),
                          "look_record_mismatch": mismatch})
        else:
            mismatch = []
            entry["look_record_status"] = "recorded_now"
            new_records.append(record)
        if mismatch:
            conflicts.append(number)
            entry["status"] = "withheld"
            looks.append(entry)
            withheld = True
            continue
        # Recorded now or reproduced exactly: the recomputed result is the
        # recorded one, so it is the decision and may be reported.
        entry.update({
            "status": "evaluated", **result,
            "skip_one_bar_60m": _summary_v2(_horizon(measure(
                subset, spec, endpoint["sensitivity_anchor"], (minutes,)),
                minutes)),
            "signal_close_60m": _summary_v2(_horizon(measure(
                subset, spec, endpoint["diagnostic_anchor"], (minutes,)),
                minutes)),
            "mirror_60m": _summary_v2(_horizon(measure(
                subset, mirror["rule_spec"], endpoint["outcome_anchor"],
                (minutes,)), minutes))})
        looks.append(entry)
        if result["outcome"] in {"pass", "futility"}:
            outcome = result["outcome"]
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
        "bars_outside_universe": received - len(bars),
        "excluded_bars": len(bars) - len(sealed),
        "interim": interim,
        "looks": looks,
        "look_records_to_append": new_records,
        "look_record_conflicts": conflicts,
        "decision": decision,
        "rule_outcome_if_eligible": "withheld" if withheld else outcome,
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
        moment = as_of if as_of is not None else _utc_now()
        # The manifest is built by code in this module; its hash is pinned
        # in the registration block so an edited rule cannot decide a look.
        pinned = V2_MANIFEST_HASHES.get(hypothesis_id)
        return _evaluate_v2(
            bars, manifest, decision_eligible=decision_eligible,
            eligibility_reasons=eligibility_reasons, provenance=provenance,
            look_records=look_records, as_of=moment,
            policy=None if decision_eligible else diagnostic_backfill_policy(),
            refused_because=(() if manifest["manifest_hash"] == pinned
                             else ("manifest_hash_mismatch",)))
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
    """Return the body of the v2 registration block for the running tree.

    It is Python source: the dates as they stand, then the instrument
    identity of the running code and the manifest hashes the manifests have
    with that identity in place.  The instrument does not read the block, so
    one pass is enough.  Print it with::

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
    lines = [f'V2_LAST_EXAMINED_SESSION = "{V2_LAST_EXAMINED_SESSION}"',
             f'V2_REGISTERED_ON = "{V2_REGISTERED_ON}"',
             f'V2_SEALED_FROM = "{V2_SEALED_FROM}"',
             "V2_INSTRUMENT_COMPONENTS: dict[str, str] = {"]
    for name, digest in identity["components"].items():
        lines += [f'    "{name}":', f'        "{digest}",']
    lines += ["}", "V2_INSTRUMENT_DIGEST = (", f'    "{identity["digest"]}")',
              "V2_MANIFEST_HASHES: dict[str, str] = {"]
    for hypothesis, digest in hashes.items():
        lines += [f'    "{hypothesis}":', f'        "{digest}",']
    lines.append("}")
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
    both decide the same look.  Records are only ever appended.  A final line
    without its newline is the remains of an append that crashed before it
    was acknowledged; it is dropped under the lock (deciding that look again
    is deterministic) rather than left to make every later run fail.
    """
    import fcntl
    import io
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            stream.seek(0)
            content = stream.read()
            dropped = bool(content) and not content.endswith("\n")
            if dropped:
                content = content[:content.rfind("\n") + 1]
                stream.truncate(len(content.encode("utf-8")))
                stream.flush()
                os.fsync(stream.fileno())
            records = read_look_records(io.StringIO(content))
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
    if dropped:
        report["look_record_unterminated_line_dropped"] = True
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
