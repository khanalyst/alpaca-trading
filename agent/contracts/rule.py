"""Bounded, data-only rule strategies shared by research and paper execution.

The autonomous research loop may create and mutate these specifications, but
it cannot create Python source.  Every accepted field has a finite range and
every signal is evaluated from completed bars only.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import register


RULE_SCHEMA_V1 = "rule-strategy.v1"
RULE_SCHEMA_V2 = "rule-strategy.v2"
RULE_SCHEMA_V3 = "rule-strategy.v3"
RULE_SCHEMA_V4 = "rule-strategy.v4"
RULE_SCHEMAS = (RULE_SCHEMA_V1, RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4)
# ``RULE_SCHEMA`` remains the v1 name so existing callers, stored specs, and
# content hashes are untouched.  v2 is a strict superset reached only by
# writing its schema string explicitly.
RULE_SCHEMA = RULE_SCHEMA_V1
# The signal primitives autonomous research may compose. Families are appended,
# never reordered or removed: a stored spec names its family by string, so the
# order affects only which family a deterministic rotation reaches next.
RULE_FAMILIES = (
    "opening_range_breakout",
    "opening_range_fade",
    "momentum_continuation",
    "mean_reversion",
    "trend_pullback",
    "volatility_breakout",
    "volume_breakout",
    # Session-anchored primitives. Both research and runtime supply one
    # session's completed bars, and each of these re-derives its session from
    # the bars' own local dates, so a mixed history cannot leak across days.
    "vwap_reversion",
    "vwap_trend",
    "range_expansion",
    "opening_drive",
    "cross_sectional_residual",
)
CONFIRMATIONS = ("none", "trend", "volume", "volatility")
SIDES = ("both", "long", "short")
BAR_SECONDS = 60.0
CROSS_SECTIONAL_BENCHMARK = "SPY"
# The residual family is a directional, single-leg equity signal relative to
# SPY.  It is not a market-neutral hedge and therefore must not pool symbols
# whose price process is not comparable to a broad equity ETF.  Keep this
# policy bounded and data-only: callers may carry an explicit symbol allowlist
# in the rule spec, while the fallback list preserves legacy specs that did not
# carry eligibility metadata.
CROSS_SECTIONAL_MAX_ELIGIBLE_SYMBOLS = 64
CROSS_SECTIONAL_DEFAULT_ELIGIBLE_SYMBOLS = frozenset({
    "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLI",
    "XLP", "XLY", "XLU", "XLB", "XLRE", "VTI", "VO", "VB", "EFA",
    "EEM", "SMH",
})
# A bracket that is tighter than the configured minimum can be consumed by
# ordinary spread/slippage before it has a chance to express the strategy's
# thesis.  This is deliberately an execution-time floor, not a grammar field:
# keeping it out of ``DEFAULT_RULE_SPEC`` preserves every legacy rule hash and
# variant id while making both research and runtime signals use the same floor.
MIN_STOP_DISTANCE_BPS = 30.0
MIN_STOP_DISTANCE_FRACTION = MIN_STOP_DISTANCE_BPS / 10_000.0

DEFAULT_RULE_SPEC: dict[str, Any] = {
    "schema": RULE_SCHEMA,
    "family": "momentum_continuation",
    "side": "both",
    "lookback": 15,
    "slow_lookback": 40,
    "range_minutes": 15,
    "threshold_bps": 12.0,
    "compression_bps": 45.0,
    "zscore": 1.25,
    "volume_multiplier": 1.25,
    "atr_period": 14,
    "stop_atr": 1.0,
    "target_r": 2.0,
    "max_hold_bars": 90,
    "confirmation": "none",
}

_BOUNDS = {
    "lookback": (3, 120, int),
    "slow_lookback": (5, 240, int),
    "range_minutes": (3, 120, int),
    "threshold_bps": (0.0, 500.0, float),
    "compression_bps": (1.0, 2_000.0, float),
    "zscore": (0.25, 5.0, float),
    "volume_multiplier": (0.25, 10.0, float),
    "atr_period": (3, 100, int),
    "stop_atr": (0.2, 10.0, float),
    "target_r": (0.25, 10.0, float),
    "max_hold_bars": (1, 390, int),
}

# v2 widens what a hypothesis can *express* without widening what it can *do*.
# Every extension is an entry-side predicate over the same completed-bar prefix
# the v1 grammar already sees, so research and runtime remain one evaluator and
# the broker-side bracket that protects a position is unchanged.  Nothing here
# can alter sizing, exits, or order placement.
SESSION_MINUTES = 390
V2_DEFAULT_EXTENSIONS: dict[str, Any] = {
    # Additional confirmation filters; every listed filter must also pass.
    "confirmations": [],
    # Minutes after the 09:30 New York open during which entries may signal.
    "entry_after_minutes": 0,
    "entry_before_minutes": SESSION_MINUTES,
    # Volatility regime band, as ATR expressed in basis points of price.
    "min_atr_bps": 0.0,
    "max_atr_bps": 5_000.0,
}
_V2_BOUNDS = {
    "entry_after_minutes": (0, SESSION_MINUTES - 1, int),
    "entry_before_minutes": (1, SESSION_MINUTES, int),
    "min_atr_bps": (0.0, 2_000.0, float),
    "max_atr_bps": (1.0, 5_000.0, float),
}
# v3 is the first exit-side grammar extension.  ``None`` is deliberately the
# neutral default: merely upgrading a stored rule to v3 does not move a stop,
# while a finite value arms a move to the actual fill price after a completed
# close reaches that many units of the position's initial fill-to-stop risk.
V3_DEFAULT_EXTENSIONS: dict[str, Any] = {
    "breakeven_r": None,
}
_V3_BOUNDS = {
    "breakeven_r": (0.0, 10.0, float),
}
# v4 is an equity-only, auditable exit extension.  Defaults are deliberately
# neutral so a v4 writer can preserve the fixed-R v3 geometry while making the
# target/ratchet/deadline policy explicit in the persisted plan.
V4_DEFAULT_EXTENSIONS: dict[str, Any] = {
    "target_mode": "fixed_r",
    "target_lookback": 20,
    "trailing_stop_r": None,
    "exit_before_minutes": None,
}
_V4_TARGET_MODES = ("fixed_r", "session_vwap", "rolling_mean")
_V4_BOUNDS = {
    "target_lookback": (2, 120, int),
    "trailing_stop_r": (0.0, 10.0, float),
    "exit_before_minutes": (1, SESSION_MINUTES - 1, int),
}
_EXTRA_CONFIRMATIONS = tuple(name for name in CONFIRMATIONS if name != "none")
MAX_CONFIRMATIONS = len(_EXTRA_CONFIRMATIONS)

# Durable, cross-lane exit causes.  Runtime keeps its historical operational
# close reasons (``before_close``, ``max_hold``, ``exit_before``) and research
# keeps the long-standing ``exit_reason`` aliases (``time``, ``exit_before``),
# but both publish one of these values as ``canonical_exit_reason``.
EXIT_REASON_STOP = "stop"
EXIT_REASON_TARGET = "target"
EXIT_REASON_MAX_HOLD = "max_hold"
EXIT_REASON_THESIS_DEADLINE = "thesis_deadline"
EXIT_REASON_SESSION_FORCE_FLAT = "session_force_flat"
EXIT_REASON_DATA_DISCONTINUITY = "data_discontinuity"
EXIT_REASON_UNKNOWN = "unknown"
CANONICAL_EXIT_REASONS = frozenset({
    EXIT_REASON_STOP,
    EXIT_REASON_TARGET,
    EXIT_REASON_MAX_HOLD,
    EXIT_REASON_THESIS_DEADLINE,
    EXIT_REASON_SESSION_FORCE_FLAT,
    EXIT_REASON_DATA_DISCONTINUITY,
    EXIT_REASON_UNKNOWN,
})

# Fields that affect the executable signal or its bounded exits.  ``schema``
# is deliberately not part of this set: v1 and v2 are aliases when every v2
# extension is at its documented no-op default.
EXECUTABLE_RULE_FIELDS = tuple(name for name in DEFAULT_RULE_SPEC
                               if name != "schema")
_COMMON_EXECUTABLE_FIELDS = frozenset(
    ("family", "side", "stop_atr", "target_r", "max_hold_bars", "atr_period",
     "confirmation"))
_FAMILY_EXECUTABLE_FIELDS = {
    "opening_range_breakout": ("range_minutes", "threshold_bps"),
    "opening_range_fade": ("range_minutes", "threshold_bps"),
    "momentum_continuation": ("lookback", "threshold_bps"),
    "mean_reversion": ("lookback", "zscore"),
    "trend_pullback": ("lookback", "slow_lookback", "threshold_bps"),
    "volatility_breakout": ("lookback", "threshold_bps", "compression_bps"),
    "volume_breakout": ("lookback", "threshold_bps", "volume_multiplier"),
    "vwap_reversion": ("lookback", "threshold_bps"),
    "vwap_trend": ("lookback", "threshold_bps"),
    "range_expansion": ("lookback", "threshold_bps", "volume_multiplier"),
    "opening_drive": ("range_minutes", "threshold_bps"),
    "cross_sectional_residual": ("lookback", "threshold_bps"),
}


# Families whose statistics accumulate from the session open.  A missing bar
# anywhere earlier in the session changes what they compute, so their feature
# window is the whole session prefix rather than a bounded trailing count.
SESSION_ACCUMULATING_FAMILIES = frozenset(("vwap_reversion", "vwap_trend"))
# Opening-range and opening-drive predicates retain a fixed session-open
# anchor even after their trailing ATR/confirmation window has rolled onward.
# Treating them as an ordinary trailing-only statistic can silently compute an
# opening range across a missing early minute.  The current continuity API is
# a single interval, so the safe representation is the full session prefix.
OPENING_ANCHORED_FAMILIES = frozenset((
    "opening_range_breakout", "opening_range_fade", "opening_drive"))
# The causal prefix each family reads before it can evaluate its predicate.
# These counts are intentionally family-specific: a mean-reversion rule uses
# exactly ``lookback`` closes, while momentum/breakout predicates compare the
# current close with a prior lookback and therefore consume ``lookback + 1``
# bars.  Opening/VWAP families retain session-anchored continuity below, but
# still have an exact causal maturity count here.
_FAMILY_FEATURE_BARS = {
    "opening_range_breakout": lambda spec: spec["range_minutes"] + 1,
    "opening_range_fade": lambda spec: spec["range_minutes"] + 1,
    # The predicate compares the current close with closes[-lookback - 1]
    # (and the immediately preceding close), so a lookback of N consumes
    # exactly N + 1 completed bars.  Counting an additional bar here delayed
    # factory evaluation past a valid first signal while the runtime evaluator
    # correctly emitted it.
    "momentum_continuation": lambda spec: spec["lookback"] + 1,
    "mean_reversion": lambda spec: spec["lookback"],
    "trend_pullback": lambda spec: spec["slow_lookback"],
    "volatility_breakout": lambda spec: spec["lookback"] + 1,
    "volume_breakout": lambda spec: spec["lookback"] + 1,
    "vwap_reversion": lambda spec: spec["lookback"] + 1,
    "vwap_trend": lambda spec: spec["lookback"] + 1,
    "range_expansion": lambda spec: spec["lookback"] + 1,
    "opening_drive": lambda spec: spec["range_minutes"] + 1,
    "cross_sectional_residual": lambda spec: spec["lookback"] + 1,
}
_CONFIRMATION_FEATURE_BARS = {
    "trend": lambda spec: spec["slow_lookback"],
    "volume": lambda spec: spec["lookback"] + 1,
    "volatility": lambda spec: spec["atr_period"] + 1,
}


def _causal_maturity_bars(spec: Mapping[str, Any]) -> int:
    """Return the exact completed-bar prefix required by a normalized spec."""
    family = str(spec["family"])
    resolve = _FAMILY_FEATURE_BARS.get(family)
    # ``validate_rule_spec`` bounds families to the table above.  Keep a
    # conservative fallback for forward-compatible callers that add a family
    # before adding its explicit dependency declaration.
    needed = int(resolve(spec) if resolve is not None
                 else spec["lookback"] + 1)
    # ATR is an active evaluator dependency for every family, even where it is
    # wider or narrower than the family's own predicate window.
    needed = max(needed, int(spec["atr_period"]) + 1)
    confirmations = {str(spec.get("confirmation") or "none")}
    confirmations.update(str(item) for item in spec.get("confirmations") or ())
    for kind in confirmations:
        resolve = _CONFIRMATION_FEATURE_BARS.get(kind)
        if resolve is not None:
            needed = max(needed, int(resolve(spec)))
    # A rolling-mean v4 target is frozen from the final T completed closes.
    # Other target modes do not consume ``target_lookback`` as a trailing
    # dependency (session VWAP is session anchored instead).
    if (spec.get("target_mode") == "rolling_mean" and
            spec.get("target_lookback") is not None):
        needed = max(needed, int(spec["target_lookback"]))
    return max(1, int(needed))


def causal_maturity_bars(value: Mapping[str, Any]) -> int:
    """Return the exact completed-bar prefix required for causal evaluation.

    This is the single dependency contract shared by the executable evaluator,
    replay maturity, and runtime gates.  It deliberately does not add an
    unrelated ``lookback + 1`` requirement to every family.
    """
    return _causal_maturity_bars(validate_rule_spec(value))


def feature_window_bars(value: Mapping[str, Any]) -> int | None:
    """Trailing completed bars :func:`evaluate_rule_signal` reads for a spec.

    ``None`` means the family or its active target retains a session-open
    anchor and cannot be represented by one bounded trailing window.

    Replay uses this to require adjacency over exactly the bars a signal is
    computed from.  A fixed lookback silently stretched across an outage is a
    different statistic than the one the spec names, so the bars it reads must
    be consecutive — but a minute missing *after* that window cannot change the
    signal, and deleting the observation for it would discard good evidence.
    """
    spec = validate_rule_spec(value)
    family = str(spec["family"])
    if (family in SESSION_ACCUMULATING_FAMILIES | OPENING_ANCHORED_FAMILIES or
            spec.get("target_mode") == "session_vwap"):
        return None
    return _causal_maturity_bars(spec)


def _semantic_fields(spec: Mapping[str, Any]) -> set[str]:
    fields = set(_COMMON_EXECUTABLE_FIELDS)
    fields.update(_FAMILY_EXECUTABLE_FIELDS.get(str(spec["family"]), ()))
    # Confirmation predicates activate their own input parameters even when
    # the base family's signal does not use them.
    confirmations = {str(spec.get("confirmation") or "none")}
    confirmations.update(str(item) for item in spec.get("confirmations") or ())
    if "trend" in confirmations:
        fields.update(("lookback", "slow_lookback"))
    if "volume" in confirmations:
        fields.add("volume_multiplier")
    if "volatility" in confirmations:
        fields.update(("atr_period", "compression_bps"))
    if spec.get("breakeven_r") is not None:
        fields.add("breakeven_r")
    if spec.get("family") == "cross_sectional_residual" and \
            "eligible_symbols" in spec:
        fields.add("eligible_symbols")
    return fields


def rule_semantic_signature(value: Mapping[str, Any]) -> str:
    """Return the canonical executable identity of a rule.

    Content hashes remain immutable storage ids and therefore retain the
    authored grammar version.  This signature is the behaviour-level id used
    for de-duplication: an omitted v2 extension (v1) and an explicit v2
    extension at its no-op default collapse to the same identity, while every
    family-relevant executable field remains represented.
    """
    spec = validate_rule_spec(value)
    effective = {name: spec[name] for name in _semantic_fields(spec)
                 if name in spec}
    if spec.get("schema") in {RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        for name, default in V2_DEFAULT_EXTENSIONS.items():
            current = spec.get(name, default)
            if current != default:
                effective[name] = current
    if spec.get("schema") in {RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        for name, default in V3_DEFAULT_EXTENSIONS.items():
            current = spec.get(name, default)
            if current != default:
                effective[name] = current
    if spec.get("schema") == RULE_SCHEMA_V4:
        for name, default in V4_DEFAULT_EXTENSIONS.items():
            current = spec.get(name, default)
            if current != default:
                effective[name] = current
    return json.dumps(effective, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def rule_semantic_distance(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    """Return a small deterministic normalized distance between two rules.

    The metric is intentionally transparent (no fuzzy model): categorical
    executable changes cost one. Continuous numeric changes use a
    relative/local scale instead of the full grammar span, while discrete
    integer coordinates retain their audited span. This keeps an economically
    meaningful proportional nudge visible without expanding integer aliases.
    A distance of zero means semantic equivalence.
    """
    a = validate_rule_spec(left)
    b = validate_rule_spec(right)
    if a["family"] != b["family"]:
        return 1.0
    distance = 0.0
    dimensions = 0
    # A grammar-wide span makes ordinary local changes on continuous axes (for
    # example a 20% threshold adjustment) look negligible merely because the
    # field's legal maximum is large. Use the values being compared as the
    # local scale for floats, while retaining the audited span for discrete
    # integer coordinates such as lookback. Validation above remains the
    # source of truth for legal ranges.
    bounds = {**_BOUNDS, **_V2_BOUNDS, **_V3_BOUNDS, **_V4_BOUNDS}
    fields = _semantic_fields(a) | _semantic_fields(b)
    for name, default in V2_DEFAULT_EXTENSIONS.items():
        if a.get(name, default) != default or b.get(name, default) != default:
            fields.add(name)
    for name, default in V3_DEFAULT_EXTENSIONS.items():
        if a.get(name, default) != default or b.get(name, default) != default:
            fields.add(name)
    for name, default in V4_DEFAULT_EXTENSIONS.items():
        if a.get(name, default) != default or b.get(name, default) != default:
            fields.add(name)
    for name in sorted(fields):
        av, bv = a.get(name), b.get(name)
        dimensions += 1
        if name == "confirmations":
            distance += 0.0 if set(av or ()) == set(bv or ()) else 1.0
        elif name in bounds and isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            low, high, _ = bounds[name]
            if isinstance(av, int) and isinstance(bv, int):
                scale = max(float(high) - float(low), 1.0)
            else:
                scale = max(abs(float(av)), abs(float(bv)), 1.0)
            distance += min(1.0, abs(float(av) - float(bv)) / scale)
        else:
            distance += 0.0 if av == bv else 1.0
    return distance / max(dimensions, 1)


def rule_spec_json_schema(schema: str | None = None) -> dict[str, Any]:
    """Expose the complete provider-facing JSON grammar and audited bounds."""
    schemas = [schema] if schema is not None else list(RULE_SCHEMAS)
    if any(item not in RULE_SCHEMAS for item in schemas):
        raise RuleSpecError(f"unknown rule schema: {schema!r}")

    def one(name: str) -> dict[str, Any]:
        common_properties: dict[str, Any] = {
            "schema": {"type": "string", "const": name},
            "side": {"type": "string", "enum": list(SIDES)},
            "lookback": {"type": "integer", "minimum": 3, "maximum": 120},
            "slow_lookback": {"type": "integer", "minimum": 5, "maximum": 240},
            "range_minutes": {"type": "integer", "minimum": 3, "maximum": 120},
            "threshold_bps": {"type": "number", "minimum": 0.0, "maximum": 500.0},
            "compression_bps": {"type": "number", "minimum": 1.0, "maximum": 2000.0},
            "zscore": {"type": "number", "minimum": 0.25, "maximum": 5.0},
            "volume_multiplier": {"type": "number", "minimum": 0.25, "maximum": 10.0},
            "atr_period": {"type": "integer", "minimum": 3, "maximum": 100},
            "stop_atr": {"type": "number", "minimum": 0.2, "maximum": 10.0},
            "target_r": {"type": "number", "minimum": 0.25, "maximum": 10.0},
            "max_hold_bars": {"type": "integer", "minimum": 1, "maximum": 390},
            "confirmation": {"type": "string", "enum": list(CONFIRMATIONS)},
        }
        required = list(DEFAULT_RULE_SPEC)
        if name in {RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
            common_properties.update({
                "confirmations": {"type": "array", "maxItems": MAX_CONFIRMATIONS,
                                  "uniqueItems": True,
                                  "items": {"type": "string", "enum": list(_EXTRA_CONFIRMATIONS)}},
                "entry_after_minutes": {"type": "integer", "minimum": 0,
                                         "maximum": SESSION_MINUTES - 1},
                "entry_before_minutes": {"type": "integer", "minimum": 1,
                                          "maximum": SESSION_MINUTES},
                "min_atr_bps": {"type": "number", "minimum": 0.0, "maximum": 2000.0},
                "max_atr_bps": {"type": "number", "minimum": 1.0, "maximum": 5000.0},
            })
            required += list(V2_DEFAULT_EXTENSIONS)
        if name in {RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
            common_properties["breakeven_r"] = {
                "type": ["number", "null"], "minimum": 0.0, "maximum": 10.0,
            }
            required += list(V3_DEFAULT_EXTENSIONS)
        if name == RULE_SCHEMA_V4:
            common_properties.update({
                # Strict structured-output providers require every property
                # to be listed in ``required``.  These v4 fields remain
                # optional to callers through nullable provider values; the
                # validator fills the documented defaults when a provider
                # returns null (see ``validate_rule_spec`` below).
                "target_mode": {"anyOf": [
                    {"type": "string", "enum": list(_V4_TARGET_MODES)},
                    {"type": "null"},
                ]},
                "target_lookback": {"type": ["integer", "null"],
                                    "minimum": 2, "maximum": 120},
                "trailing_stop_r": {"type": ["number", "null"],
                                     "exclusiveMinimum": 0.0,
                                     "maximum": 10.0},
                "exit_before_minutes": {"type": ["integer", "null"],
                                         "minimum": 1,
                                         "maximum": SESSION_MINUTES - 1},
            })
            # v4 exit policy is backward-compatible when omitted: fixed-R,
            # the bounded default lookback, no trailing ratchet, and no thesis
            # deadline are the neutral defaults.  The provider-facing schema
            # represents omission as null because strict providers do not
            # permit an optional property; validation fills these defaults.
        def branch(family: dict[str, Any], *, eligible: bool) -> dict[str, Any]:
            properties = {**common_properties, "family": family}
            if eligible:
                # Optional to normal callers, but represented as required and
                # nullable for strict provider schemas.  The property lives
                # only on this branch so non-cross-sectional providers cannot
                # emit a field runtime rejects.
                properties["eligible_symbols"] = {
                    "type": ["array", "null"],
                    "maxItems": CROSS_SECTIONAL_MAX_ELIGIBLE_SYMBOLS,
                    "uniqueItems": True,
                    "items": {"type": "string",
                              "pattern": "^[A-Za-z][A-Za-z0-9._-]{0,14}$"},
                }
            branch_required = list(required)
            if eligible:
                branch_required.append("eligible_symbols")
            if name == RULE_SCHEMA_V4:
                branch_required.extend(V4_DEFAULT_EXTENSIONS)
            return {"type": "object", "additionalProperties": False,
                    "required": branch_required, "properties": properties}

        non_cross = [family for family in RULE_FAMILIES
                     if family != "cross_sectional_residual"]
        return {"oneOf": [
            branch({"type": "string", "enum": non_cross}, eligible=False),
            branch({"type": "string", "const": "cross_sectional_residual"},
                   eligible=True),
        ]}

    if len(schemas) == 1:
        return one(schemas[0])
    # Flatten the per-schema family branches so a provider validating the
    # union never has to interpret nested ``oneOf`` wrappers.
    return {"oneOf": [branch
                       for name in schemas
                       for branch in one(name)["oneOf"]]}


class RuleSpecError(ValueError):
    """Raised when an autonomous rule leaves the audited grammar."""


def _validate_confirmations(value: Any) -> list[str]:
    """Normalize the v2 confirmation list to a deterministic, bounded set."""

    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise RuleSpecError("rule_spec.confirmations must be a list of filter names")
    if len(value) > MAX_CONFIRMATIONS:
        raise RuleSpecError(
            f"rule_spec.confirmations accepts at most {MAX_CONFIRMATIONS} filters")
    selected: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item not in _EXTRA_CONFIRMATIONS:
            raise RuleSpecError(
                "rule_spec.confirmations entries must be "
                f"{', '.join(_EXTRA_CONFIRMATIONS)}")
        selected.add(item)
    # Order carries no meaning — every listed filter must pass — so the
    # canonical form is sorted and duplicate-free.  That keeps one logical
    # rule from hashing to several distinct variant ids.
    return sorted(selected)


def _validate_eligible_symbols(value: Any) -> list[str]:
    """Normalize a bounded symbol allowlist carried by a rule spec."""

    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise RuleSpecError("rule_spec.eligible_symbols must be a list of symbols")
    if len(value) > CROSS_SECTIONAL_MAX_ELIGIBLE_SYMBOLS:
        raise RuleSpecError(
            "rule_spec.eligible_symbols accepts at most "
            f"{CROSS_SECTIONAL_MAX_ELIGIBLE_SYMBOLS} symbols")
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise RuleSpecError("rule_spec.eligible_symbols entries must be strings")
        symbol = item.strip().upper()
        if (not symbol or len(symbol) > 15 or not symbol[0].isalpha() or
                any(not (char.isalnum() or char in "._-") for char in symbol)):
            raise RuleSpecError(
                "rule_spec.eligible_symbols entries must be valid symbols")
        result.add(symbol)
    return sorted(result)


def validate_rule_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuleSpecError("rule_spec must be a mapping")
    schema = value.get("schema", RULE_SCHEMA_V1)
    if schema not in RULE_SCHEMAS:
        raise RuleSpecError(
            f"rule_spec.schema must be one of {', '.join(map(repr, RULE_SCHEMAS))}")
    permitted = set(DEFAULT_RULE_SPEC) | {"eligible_symbols"}
    if schema in {RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        permitted |= set(V2_DEFAULT_EXTENSIONS)
    if schema in {RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        permitted |= set(V3_DEFAULT_EXTENSIONS)
    if schema == RULE_SCHEMA_V4:
        permitted |= set(V4_DEFAULT_EXTENSIONS)
    unknown = sorted(set(value) - permitted)
    if unknown:
        # A v1 spec naming a v2 field is a version error, not a typo: say so.
        v3_extensions = [name for name in unknown if name in V3_DEFAULT_EXTENSIONS]
        if v3_extensions:
            raise RuleSpecError(
                f"rule_spec field(s) {', '.join(v3_extensions)} require "
                f"schema {RULE_SCHEMA_V3!r}")
        v2_extensions = [name for name in unknown if name in V2_DEFAULT_EXTENSIONS]
        if v2_extensions:
            raise RuleSpecError(
                f"rule_spec field(s) {', '.join(v2_extensions)} require "
                f"schema {RULE_SCHEMA_V2!r}")
        v4_extensions = [name for name in unknown if name in V4_DEFAULT_EXTENSIONS]
        if v4_extensions:
            raise RuleSpecError(
                f"rule_spec field(s) {', '.join(v4_extensions)} require "
                f"schema {RULE_SCHEMA_V4!r}")
        raise RuleSpecError(f"rule_spec has unknown field(s): {', '.join(unknown)}")
    spec = dict(DEFAULT_RULE_SPEC)
    if schema in {RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        spec.update(V2_DEFAULT_EXTENSIONS)
    if schema in {RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        spec.update(V3_DEFAULT_EXTENSIONS)
    if schema == RULE_SCHEMA_V4:
        spec.update(V4_DEFAULT_EXTENSIONS)
    # Strict provider schemas encode legacy-optional properties as required
    # nullable values.  Treat null as omission for fields whose documented
    # defaults preserve the old caller contract.  ``breakeven_r`` and the two
    # genuinely nullable exit controls intentionally remain present as null.
    provided = dict(value)
    for optional_name in ("eligible_symbols", "target_mode", "target_lookback"):
        if provided.get(optional_name, object()) is None:
            provided.pop(optional_name, None)
    spec.update(provided)
    spec["schema"] = schema
    if spec.get("family") not in RULE_FAMILIES:
        raise RuleSpecError(f"unsupported rule family: {spec.get('family')!r}")
    if ("eligible_symbols" in spec and
            spec.get("family") != "cross_sectional_residual"):
        raise RuleSpecError(
            "rule_spec.eligible_symbols is only valid for "
            "cross_sectional_residual")
    if spec.get("side") not in SIDES:
        raise RuleSpecError("rule_spec.side must be both, long, or short")
    if spec.get("confirmation") not in CONFIRMATIONS:
        raise RuleSpecError(
            "rule_spec.confirmation must be none, trend, volume, or volatility")
    for name, (lower, upper, cast) in _BOUNDS.items():
        raw = spec.get(name)
        if isinstance(raw, bool):
            raise RuleSpecError(f"rule_spec.{name} has an invalid type")
        if cast is int and not isinstance(raw, int):
            raise RuleSpecError(f"rule_spec.{name} must be an integer")
        try:
            number = cast(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuleSpecError(f"rule_spec.{name} must be numeric") from exc
        if not math.isfinite(float(number)) or not lower <= number <= upper:
            raise RuleSpecError(
                f"rule_spec.{name} must be between {lower:g} and {upper:g}")
        spec[name] = number
    if spec["slow_lookback"] <= spec["lookback"]:
        raise RuleSpecError("rule_spec.slow_lookback must exceed lookback")
    if schema == RULE_SCHEMA_V1:
        if "eligible_symbols" in spec:
            spec["eligible_symbols"] = _validate_eligible_symbols(
                spec["eligible_symbols"])
        return spec
    spec["confirmations"] = _validate_confirmations(spec["confirmations"])
    for name, (lower, upper, cast) in _V2_BOUNDS.items():
        raw = spec.get(name)
        if isinstance(raw, bool):
            raise RuleSpecError(f"rule_spec.{name} has an invalid type")
        if cast is int and not isinstance(raw, int):
            raise RuleSpecError(f"rule_spec.{name} must be an integer")
        try:
            number = cast(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuleSpecError(f"rule_spec.{name} must be numeric") from exc
        if not math.isfinite(float(number)) or not lower <= number <= upper:
            raise RuleSpecError(
                f"rule_spec.{name} must be between {lower:g} and {upper:g}")
        spec[name] = number
    if spec["entry_before_minutes"] <= spec["entry_after_minutes"]:
        raise RuleSpecError(
            "rule_spec.entry_before_minutes must exceed entry_after_minutes")
    if spec["max_atr_bps"] <= spec["min_atr_bps"]:
        raise RuleSpecError("rule_spec.max_atr_bps must exceed min_atr_bps")
    if schema == RULE_SCHEMA_V2:
        if "eligible_symbols" in spec:
            spec["eligible_symbols"] = _validate_eligible_symbols(
                spec["eligible_symbols"])
        return spec
    breakeven = spec.get("breakeven_r")
    if breakeven is None:
        if "eligible_symbols" in spec:
            spec["eligible_symbols"] = _validate_eligible_symbols(
                spec["eligible_symbols"])
    else:
        if isinstance(breakeven, bool):
            raise RuleSpecError("rule_spec.breakeven_r has an invalid type")
        lower, upper, cast = _V3_BOUNDS["breakeven_r"]
        try:
            breakeven = cast(breakeven)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuleSpecError("rule_spec.breakeven_r must be numeric or null") from exc
        if not math.isfinite(breakeven) or not lower <= breakeven <= upper:
            raise RuleSpecError(
                f"rule_spec.breakeven_r must be between {lower:g} and {upper:g}, or null")
        if breakeven >= float(spec["target_r"]):
            raise RuleSpecError("rule_spec.breakeven_r must be below target_r")
        spec["breakeven_r"] = breakeven
    if "eligible_symbols" in spec:
        spec["eligible_symbols"] = _validate_eligible_symbols(
            spec["eligible_symbols"])
    if schema == RULE_SCHEMA_V3:
        return spec
    # v4 exit policy fields are validated only after all v2/v3 extensions so
    # old schemas retain their exact normalized maps and content hashes.
    target_mode = spec.get("target_mode")
    if target_mode not in _V4_TARGET_MODES:
        raise RuleSpecError(
            "rule_spec.target_mode must be fixed_r, session_vwap, or rolling_mean")
    for name, (lower, upper, cast) in _V4_BOUNDS.items():
        raw = spec.get(name)
        if raw is None and name in {"trailing_stop_r", "exit_before_minutes"}:
            continue
        if isinstance(raw, bool):
            raise RuleSpecError(f"rule_spec.{name} has an invalid type")
        if cast is int and not isinstance(raw, int):
            raise RuleSpecError(f"rule_spec.{name} must be an integer")
        try:
            number = cast(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuleSpecError(f"rule_spec.{name} must be numeric") from exc
        valid_range = (lower < number <= upper
                       if name == "trailing_stop_r" else lower <= number <= upper)
        if not math.isfinite(float(number)) or not valid_range:
            raise RuleSpecError(
                f"rule_spec.{name} must be between {lower:g} and {upper:g}, or null")
        spec[name] = number
    return spec


def thesis_exit_deadline(entry_ts: Any, spec: Mapping[str, Any]) -> float | None:
    """Return the v4 thesis deadline anchored to the entry session.

    This is intentionally separate from the safety force-flat deadline.  A
    v4 plan persists both timestamps so an operator can distinguish a thesis
    exit from a regular-session safety flatten even when they happen to share
    a wall-clock minute.
    """
    minutes = spec.get("exit_before_minutes") if isinstance(spec, Mapping) else None
    if minutes is None:
        return None
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise RuleSpecError("rule_spec.exit_before_minutes must be an integer or null")
    lower, upper, _ = _V4_BOUNDS["exit_before_minutes"]
    if not lower <= minutes <= upper:
        raise RuleSpecError(
            f"rule_spec.exit_before_minutes must be between {lower:g} and {upper:g}, or null")
    start = _epoch(entry_ts, "entry timestamp")
    zone = ZoneInfo("America/New_York")
    local = datetime.fromtimestamp(start, timezone.utc).astimezone(zone)
    close = datetime.combine(local.date(), time(16, 0), tzinfo=zone)
    return close.timestamp() - minutes * 60.0


def canonical_exit_reason(reason: Any, *,
                          discontinuity: bool = False) -> str:
    """Normalize research and runtime exit aliases to one durable cause."""
    if discontinuity:
        return EXIT_REASON_DATA_DISCONTINUITY
    normalized = str(reason or "").strip().lower().replace("-", "_")
    aliases = {
        "stop": EXIT_REASON_STOP,
        "target": EXIT_REASON_TARGET,
        "time": EXIT_REASON_MAX_HOLD,
        "time_expiry": EXIT_REASON_MAX_HOLD,
        "max_hold": EXIT_REASON_MAX_HOLD,
        "exit_before": EXIT_REASON_THESIS_DEADLINE,
        "thesis_deadline": EXIT_REASON_THESIS_DEADLINE,
        "before_close": EXIT_REASON_SESSION_FORCE_FLAT,
        "force_flat": EXIT_REASON_SESSION_FORCE_FLAT,
        "session_force_flat": EXIT_REASON_SESSION_FORCE_FLAT,
        "discontinuity": EXIT_REASON_DATA_DISCONTINUITY,
        "data_discontinuity": EXIT_REASON_DATA_DISCONTINUITY,
    }
    return aliases.get(normalized, EXIT_REASON_UNKNOWN)


def exit_deadline(entry_ts: Any, spec: Mapping[str, Any], *,
                  force_flat_ts: Any = None) -> dict[str, Any] | None:
    """Return the earliest bounded exit deadline and its canonical cause.

    Equal timestamps use runtime precedence: the session safety flatten wins,
    then the authored thesis deadline, then the ordinary maximum hold.
    """
    held = spec.get("max_hold_bars") if isinstance(spec, Mapping) else None
    candidates: list[tuple[float, int, str]] = []
    if held is not None:
        lower, upper, _ = _BOUNDS["max_hold_bars"]
        if isinstance(held, bool) or not isinstance(held, int):
            raise RuleSpecError("rule_spec.max_hold_bars must be an integer")
        if not lower <= held <= upper:
            raise RuleSpecError(
                f"rule_spec.max_hold_bars must be between {lower:g} and {upper:g}")
        start = _epoch(entry_ts, "entry timestamp")
        candidates.append((start + (held + 1) * BAR_SECONDS, 2,
                           EXIT_REASON_MAX_HOLD))
    thesis = thesis_exit_deadline(entry_ts, spec)
    if thesis is not None:
        candidates.append((thesis, 1, EXIT_REASON_THESIS_DEADLINE))
    if force_flat_ts is not None:
        candidates.append((_epoch(force_flat_ts, "force-flat timestamp"), 0,
                           EXIT_REASON_SESSION_FORCE_FLAT))
    if not candidates:
        return None
    timestamp, _priority, reason = min(candidates, key=lambda item: (item[0], item[1]))
    return {"timestamp": timestamp, "reason": reason}


def hold_deadline(entry_ts: Any, spec: Mapping[str, Any], *,
                  force_flat_ts: Any = None) -> float | None:
    """Absolute epoch second by which a bounded rule position must be flat.

    *entry_ts* is the opening timestamp of the entry bar, which is the bar
    after the signal bar.  The position is held for at most `max_hold_bars`
    further one-minute bars, so the deadline is the close of the last
    permitted bar.  Research simulation and live execution share this one
    definition; a session force-flat always clamps it.  A spec without any
    bounded exit has no deadline.
    """
    resolved = exit_deadline(entry_ts, spec, force_flat_ts=force_flat_ts)
    return None if resolved is None else float(resolved["timestamp"])


def _epoch(value: Any, field: str) -> float:
    if isinstance(value, datetime):
        value = value.timestamp()
    if isinstance(value, bool):
        raise RuleSpecError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuleSpecError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise RuleSpecError(f"{field} must be finite")
    return number


def _exit_number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise RuleSpecError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuleSpecError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "finite and positive" if positive else "finite"
        raise RuleSpecError(f"{field} must be {qualifier}")
    return number


def initialize_exit_state(direction: str, entry_price: Any, stop_price: Any,
                          target_price: Any, *, breakeven_r: Any = None,
                          trailing_stop_r: Any = None,
                          target_mode: str = "fixed_r",
                          target_lookback: Any = None,
                          exit_before_ts: Any = None) -> dict[str, Any]:
    """Return the canonical durable state for one bounded rule exit.

    The entry is the actual fill anchor.  The authored/resting initial stop is
    retained separately from the active stop so a later breakeven amendment
    never rewrites the risk unit that armed it.
    """
    direction = str(direction or "").lower()
    if direction not in {"long", "short"}:
        raise RuleSpecError("exit direction must be long or short")
    entry = _exit_number(entry_price, "exit entry_price", positive=True)
    stop = _exit_number(stop_price, "exit stop_price", positive=True)
    target = _exit_number(target_price, "exit target_price", positive=True)
    if ((direction == "long" and not stop < target) or
            (direction == "short" and not target < stop)):
        raise RuleSpecError("exit stop/entry/target geometry is invalid")
    if breakeven_r is not None:
        breakeven = _exit_number(breakeven_r, "exit breakeven_r")
        lower, upper, _cast = _V3_BOUNDS["breakeven_r"]
        if not lower <= breakeven <= upper:
            raise RuleSpecError(
                f"exit breakeven_r must be between {lower:g} and {upper:g}, or null")
    else:
        breakeven = None
    if target_mode not in _V4_TARGET_MODES:
        raise RuleSpecError(
            "exit target_mode must be fixed_r, session_vwap, or rolling_mean")
    if target_lookback is not None:
        if isinstance(target_lookback, bool) or not isinstance(target_lookback, int):
            raise RuleSpecError("exit target_lookback must be an integer")
        low, high, _ = _V4_BOUNDS["target_lookback"]
        if not low <= target_lookback <= high:
            raise RuleSpecError("exit target_lookback is outside its bounded range")
    if trailing_stop_r is not None:
        trailing = _exit_number(trailing_stop_r, "exit trailing_stop_r")
        low, high, _ = _V4_BOUNDS["trailing_stop_r"]
        if not low < trailing <= high:
            raise RuleSpecError(
                f"exit trailing_stop_r must be between {low:g} and {high:g}, or null")
    else:
        trailing = None
    deadline = None if exit_before_ts is None else _epoch(
        exit_before_ts, "exit_before timestamp")
    result = {
        "direction": direction,
        "entry_price": entry,
        "initial_stop_price": stop,
        "active_stop_price": stop,
        "target_price": target,
        "initial_risk": abs(entry - stop),
        "breakeven_r": breakeven,
        "breakeven_armed_at": None,
        "breakeven_armed_epoch": None,
        "entry_bar_pending": True,
        "last_completed_bar_at": None,
        "last_completed_bar_epoch": None,
    }
    # Do not widen the v1-v3 durable state shape for callers that do not opt
    # into a v4 exit field.  v4 plans always pass at least target_lookback or
    # an explicit target mode, making the policy auditable on disk.
    if (trailing is not None or target_mode != "fixed_r" or
            target_lookback is not None or deadline is not None):
        result.update({"trailing_stop_r": trailing, "target_mode": target_mode,
                       "target_lookback": target_lookback,
                       "exit_before_ts": deadline})
    return result


def breakeven_stop_price(entry_price: Any, direction: str) -> float:
    """Return the broker-valid equity tick nearest entry without crossing it."""
    direction = str(direction or "").lower()
    if direction not in {"long", "short"}:
        raise RuleSpecError("breakeven direction must be long or short")
    entry = Decimal(str(_exit_number(
        entry_price, "breakeven entry_price", positive=True)))
    increment = Decimal("0.01") if entry >= Decimal("1") else Decimal("0.0001")
    rounding = ROUND_FLOOR if direction == "long" else ROUND_CEILING
    price = entry.quantize(increment, rounding=rounding)
    if entry < Decimal("1") <= price:
        price = price.quantize(Decimal("0.01"), rounding=rounding)
    if price <= 0:
        raise RuleSpecError("breakeven stop price must be positive")
    return float(price)


def _exit_bar_time(row: Any) -> tuple[float, float]:
    opened = _timestamp(row)
    if opened is None:
        raise RuleSpecError("completed exit bar timestamp is unavailable")
    opened_epoch = opened.timestamp()
    raw_end = _value(row, "end", None)
    if raw_end is None:
        end_epoch = opened_epoch + BAR_SECONDS
    else:
        if isinstance(raw_end, datetime):
            ended = raw_end if raw_end.tzinfo else raw_end.replace(tzinfo=timezone.utc)
        else:
            try:
                text = str(raw_end)
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                ended = datetime.fromisoformat(text)
                if ended.tzinfo is None:
                    ended = ended.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError) as exc:
                raise RuleSpecError("completed exit bar end is invalid") from exc
        end_epoch = ended.timestamp()
    if not math.isfinite(end_epoch) or end_epoch <= opened_epoch:
        raise RuleSpecError("completed exit bar end must follow its open")
    return opened_epoch, end_epoch


def completed_bar_exit_transition(state: Mapping[str, Any], bar: Any) -> dict[str, Any]:
    """Advance one exit from one completed OHLC bar without side effects.

    Ordering is the executable contract: gap checks first, then the intrabar
    range with stop winning an unknowable two-sided path, then (only if still
    open) a completed-close breakeven arm whose new stop applies next bar.
    """
    if not isinstance(state, Mapping):
        raise RuleSpecError("exit state must be a mapping")
    direction = str(state.get("direction") or "").lower()
    entry = _exit_number(state.get("entry_price"), "exit entry_price", positive=True)
    initial_stop = _exit_number(
        state.get("initial_stop_price", state.get("stop_price")),
        "exit initial_stop_price", positive=True)
    active_stop = _exit_number(
        state.get("active_stop_price", initial_stop),
        "exit active_stop_price", positive=True)
    target = _exit_number(state.get("target_price"), "exit target_price", positive=True)
    entry_pending = bool(state.get("entry_bar_pending", False))
    if direction == "long":
        valid = initial_stop < target and active_stop < target
    elif direction == "short":
        valid = target < initial_stop and target < active_stop
    else:
        valid = False
    if not valid:
        raise RuleSpecError("exit state geometry is invalid")
    initial_risk = abs(entry - initial_stop)
    breakeven = state.get("breakeven_r")
    if breakeven is not None:
        breakeven = _exit_number(breakeven, "exit breakeven_r")
        lower, upper, _cast = _V3_BOUNDS["breakeven_r"]
        if not lower <= breakeven <= upper:
            raise RuleSpecError(
                f"exit breakeven_r must be between {lower:g} and {upper:g}, or null")
    trailing_raw = state.get("trailing_stop_r")
    if trailing_raw is not None:
        trailing = _exit_number(trailing_raw, "exit trailing_stop_r")
        lower, upper, _cast = _V4_BOUNDS["trailing_stop_r"]
        if not lower < trailing <= upper:
            raise RuleSpecError(
                f"exit trailing_stop_r must be between {lower:g} and {upper:g}, or null")
    else:
        trailing = None
    target_mode = str(state.get("target_mode") or "fixed_r")
    if target_mode not in _V4_TARGET_MODES:
        raise RuleSpecError("exit target_mode is invalid")
    opened_epoch, end_epoch = _exit_bar_time(bar)
    previous_epoch = state.get("last_completed_bar_epoch")
    if previous_epoch is not None:
        previous_epoch = _exit_number(previous_epoch, "last completed bar epoch")
        if end_epoch < previous_epoch - 1e-9:
            raise RuleSpecError("completed exit bars are out of order")
        if abs(end_epoch - previous_epoch) <= 1e-9:
            return {"state": dict(state), "exit": None,
                    "stop_changed": False, "duplicate": True}
    opened = _exit_number(_value(bar, "open"), "completed exit bar open", positive=True)
    high = _exit_number(_value(bar, "high"), "completed exit bar high", positive=True)
    low = _exit_number(_value(bar, "low"), "completed exit bar low", positive=True)
    close = _exit_number(_value(bar, "close"), "completed exit bar close", positive=True)
    if low > min(opened, close) or high < max(opened, close) or high < low:
        raise RuleSpecError("completed exit bar OHLC is invalid")

    updated = dict(state)
    updated.update({
        "direction": direction, "entry_price": entry,
        "initial_stop_price": initial_stop, "active_stop_price": active_stop,
        "target_price": target, "initial_risk": initial_risk,
        "breakeven_r": breakeven,
        "entry_bar_pending": False,
        "last_completed_bar_at": datetime.fromtimestamp(
            end_epoch, timezone.utc).isoformat(),
        "last_completed_bar_epoch": end_epoch,
    })
    if ("trailing_stop_r" in state or "target_mode" in state or
            "target_lookback" in state or "exit_before_ts" in state):
        updated.update({"trailing_stop_r": trailing, "target_mode": target_mode,
                        "target_lookback": state.get("target_lookback"),
                        "exit_before_ts": state.get("exit_before_ts")})
    if direction == "long":
        gap_stop, gap_target = opened <= active_stop, opened >= target
        hit_stop, hit_target = low <= active_stop, high >= target
    else:
        gap_stop, gap_target = opened >= active_stop, opened <= target
        hit_stop, hit_target = high >= active_stop, low <= target
    exit_row = None
    if state.get("entry_bar_pending", False):
        if direction == "long":
            fill_stop, fill_target = entry <= active_stop, entry >= target
        else:
            fill_stop, fill_target = entry >= active_stop, entry <= target
        if fill_stop or fill_target:
            exit_row = {
                "reason": "stop" if fill_stop else "target",
                "price": entry, "gapped": True, "entry_gap": True,
                "tie_broken": bool(fill_stop and fill_target),
                "bar_start_epoch": opened_epoch, "bar_end_epoch": end_epoch,
            }
    if exit_row is None and initial_risk <= 0:
        raise RuleSpecError("exit initial risk must be positive")
    if exit_row is None and (gap_stop or gap_target):
        reason = "stop" if gap_stop else "target"
        exit_row = {
            "reason": reason, "price": opened, "gapped": True,
            "entry_gap": False,
            "tie_broken": bool(gap_stop and gap_target),
            "bar_start_epoch": opened_epoch, "bar_end_epoch": end_epoch,
        }
    elif exit_row is None and (hit_stop or hit_target):
        reason = "stop" if hit_stop else "target"
        exit_row = {
            "reason": reason,
            "price": active_stop if hit_stop else target,
            "gapped": False, "entry_gap": False,
            "tie_broken": bool(hit_stop and hit_target),
            "bar_start_epoch": opened_epoch, "bar_end_epoch": end_epoch,
        }
    stop_changed = False
    if exit_row is None and breakeven is not None and not state.get(
            "breakeven_armed_epoch"):
        trigger = (entry + initial_risk * breakeven if direction == "long" else
                   entry - initial_risk * breakeven)
        reached = close >= trigger if direction == "long" else close <= trigger
        if reached:
            updated["active_stop_price"] = breakeven_stop_price(entry, direction)
            updated["breakeven_armed_at"] = datetime.fromtimestamp(
                end_epoch, timezone.utc).isoformat()
            updated["breakeven_armed_epoch"] = end_epoch
            stop_changed = active_stop != updated["active_stop_price"]
    # A trailing stop is derived only from this completed close and is applied
    # to the *next* bar.  Ratchets are monotone and never cross the frozen
    # target; stop/target tie and gap semantics above therefore remain intact.
    if exit_row is None and trailing is not None:
        candidate = (close - initial_risk * trailing if direction == "long" else
                     close + initial_risk * trailing)
        increment = Decimal("0.01") if candidate >= 1.0 else Decimal("0.0001")
        rounding = ROUND_FLOOR if direction == "long" else ROUND_CEILING
        candidate = float(Decimal(str(candidate)).quantize(increment,
                                                           rounding=rounding))
        if direction == "long":
            candidate = min(candidate, target - max(target * 1e-12, 1e-9))
            if candidate > float(updated["active_stop_price"]):
                updated["active_stop_price"] = candidate
                stop_changed = True
        else:
            candidate = max(candidate, target + max(target * 1e-12, 1e-9))
            if candidate < float(updated["active_stop_price"]):
                updated["active_stop_price"] = candidate
                stop_changed = True
    return {"state": updated, "exit": exit_row,
            "stop_changed": stop_changed, "duplicate": False}


def rule_vehicle_executable(spec: Mapping[str, Any], vehicle: str) -> bool:
    """Whether research/runtime possess a parity-safe execution path."""
    normalized = validate_rule_spec(spec)
    if normalized["family"] == "cross_sectional_residual":
        return str(vehicle or "").lower() in {"equity", "share", "shares"}
    return not (normalized["schema"] in {RULE_SCHEMA_V3, RULE_SCHEMA_V4} and
                str(vehicle or "").lower() in {"option", "options"})


def rule_spec_hash(value: Mapping[str, Any]) -> str:
    spec = validate_rule_spec(value)
    return hashlib.sha256(json.dumps(
        spec, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def rule_variant_id(value: Mapping[str, Any]) -> str:
    spec = validate_rule_spec(value)
    family = str(spec["family"]).replace("_", "-")
    return f"rule.{family}.{rule_spec_hash(spec)[:16]}"


def _value(row: Any, name: str, default=None):
    return row.get(name, default) if isinstance(row, Mapping) else getattr(row, name, default)


def _number(row: Any, name: str, default=0.0) -> float:
    try:
        value = float(_value(row, name, default))
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _timestamp(row: Any) -> datetime | None:
    raw = _value(row, "timestamp", _value(row, "ts"))
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _row_symbol(row: Any) -> str | None:
    raw = _value(row, "symbol")
    if raw in (None, ""):
        return None
    return str(raw).strip().upper() or None


def _bar_session(stamp: datetime) -> str:
    return stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _context_rows(bars_by_symbol: Mapping[str, Sequence[Any]] | None,
                  symbol: str) -> tuple[Any, ...] | None:
    if not isinstance(bars_by_symbol, Mapping):
        return None
    matches = [value for key, value in bars_by_symbol.items()
               if str(key).strip().upper() == symbol]
    if len(matches) != 1:
        return None
    rows = matches[0]
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        return None
    return tuple(rows)


def _context_digest(benchmark_rows: Sequence[Any]) -> str:
    payload = {
        "schema": "cross-sectional-market-context.v1",
        "benchmark_symbol": CROSS_SECTIONAL_BENCHMARK,
        "bars": [[
            _timestamp(row).astimezone(timezone.utc).isoformat(),
            _number(row, "close"),
        ] for row in benchmark_rows],
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def cross_sectional_symbol_eligibility(
        symbol: str | None, *, rows: Sequence[Any] = (),
        spec: Mapping[str, Any] | None = None,
        benchmark_symbol: str = CROSS_SECTIONAL_BENCHMARK) -> dict[str, Any]:
    """Classify whether a residual-family subject is comparable to SPY.

    The result is descriptive and deterministic; callers still evaluate the
    ordinary single-leg signal and execution gates. The default set is the
    shipped equity-ETF universe with the known rates/credit and metals
    exposures excluded. An authored allowlist can narrow that set, but cannot
    expand it; unknown symbols therefore fail closed.
    """

    normalized_spec = validate_rule_spec(spec) if spec is not None else None
    subject = str(symbol or "").strip().upper()
    benchmark = str(benchmark_symbol or CROSS_SECTIONAL_BENCHMARK).strip().upper()
    result: dict[str, Any] = {
        "schema": "cross-sectional-eligibility.v1",
        "symbol": subject,
        "benchmark_symbol": benchmark,
        "eligible": False,
        "status": "ineligible",
        "reason": "subject_symbol_missing" if not subject else None,
        "source": "none",
    }
    if not subject:
        return result
    if subject == benchmark:
        result.update(reason="benchmark_self_reference", source="benchmark_policy")
        return result
    allowlist = ((normalized_spec or {}).get("eligible_symbols")
                 if normalized_spec is not None else None)
    if allowlist is not None and subject not in set(allowlist):
        result.update(reason="symbol_not_in_spec_eligibility", source="spec_allowlist")
        return result

    if subject not in CROSS_SECTIONAL_DEFAULT_ELIGIBLE_SYMBOLS:
        result.update(reason="symbol_not_in_default_eligibility",
                      source="default_universe")
        return result
    result.update(eligible=True, status="eligible",
                  reason="eligible_equity_etf",
                  source=("spec_allowlist" if allowlist is not None
                          else "default_universe"))
    return result


def rule_behavior_identity(value: Mapping[str, Any], *,
                           market_context_digest: str | None = None) -> str:
    """Return executable identity, including context for relative rules."""
    spec = validate_rule_spec(value)
    if spec["family"] != "cross_sectional_residual":
        return rule_variant_id(spec)
    payload = {
        "schema": "rule-behavior.v1",
        "rule": json.loads(rule_semantic_signature(spec)),
        "benchmark_symbol": CROSS_SECTIONAL_BENCHMARK,
        "market_context_digest": str(market_context_digest or "missing"),
    }
    digest = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return f"rule.cross-sectional-residual.behavior.{digest[:24]}"


def _cross_sectional_direction(
        bars: Sequence[Any], spec: Mapping[str, Any], *,
        bars_by_symbol: Mapping[str, Sequence[Any]] | None,
        symbol: str | None) -> tuple[str | None, str, dict[str, Any]]:
    """Evaluate one exact, synchronized symbol/SPY completed-bar window."""
    metadata: dict[str, Any] = {
        "benchmark_symbol": CROSS_SECTIONAL_BENCHMARK,
    }
    needed = int(spec["lookback"]) + 1
    subject = list(bars[-needed:])
    subject_symbol = str(symbol or _row_symbol(subject[-1]) or "").strip().upper()
    if not subject_symbol:
        return None, "subject_symbol_missing", metadata
    eligibility = cross_sectional_symbol_eligibility(
        subject_symbol, rows=subject, spec=spec)
    metadata["eligibility"] = eligibility
    if not eligibility["eligible"]:
        return None, f"subject_context_ineligible:{eligibility['reason']}", metadata
    if any(row_symbol not in (None, subject_symbol)
           for row_symbol in (_row_symbol(row) for row in subject)):
        return None, "subject_context_misaligned", metadata
    if any(_number(row, "interval_seconds", 60) != 60.0
           for row in subject):
        return None, "subject_context_misaligned", metadata
    subject_stamps = [_timestamp(row) for row in subject]
    if any(stamp is None for stamp in subject_stamps):
        return None, "subject_context_misaligned", metadata
    stamps = [stamp.astimezone(timezone.utc) for stamp in subject_stamps
              if stamp is not None]
    if (len(stamps) != needed or
            len({_bar_session(stamp) for stamp in stamps}) != 1 or
            any(right - left != timedelta(minutes=1)
                for left, right in zip(stamps, stamps[1:]))):
        return None, "subject_context_misaligned", metadata

    benchmark_source = (tuple(subject)
                        if subject_symbol == CROSS_SECTIONAL_BENCHMARK else
                        _context_rows(bars_by_symbol,
                                      CROSS_SECTIONAL_BENCHMARK))
    if not benchmark_source:
        return None, "benchmark_context_missing", metadata
    parsed: list[tuple[datetime, Any]] = []
    for row in benchmark_source:
        stamp = _timestamp(row)
        if stamp is None:
            return None, "benchmark_context_misaligned", metadata
        row_symbol = _row_symbol(row)
        if row_symbol not in (None, CROSS_SECTIONAL_BENCHMARK):
            return None, "benchmark_context_misaligned", metadata
        if _number(row, "interval_seconds", 60) != 60.0:
            return None, "benchmark_context_misaligned", metadata
        parsed.append((stamp.astimezone(timezone.utc), row))
    current_stamp = stamps[-1]
    eligible = [(stamp, row) for stamp, row in parsed if stamp <= current_stamp]
    same_session = [(stamp, row) for stamp, row in eligible
                    if _bar_session(stamp) == _bar_session(current_stamp)]
    if not same_session:
        return None, "benchmark_context_stale", metadata
    if any(right[0] <= left[0]
           for left, right in zip(same_session, same_session[1:])):
        return None, "benchmark_context_misaligned", metadata
    by_timestamp = {stamp: row for stamp, row in same_session}
    if len(by_timestamp) != len(same_session):
        return None, "benchmark_context_misaligned", metadata
    if same_session[-1][0] < current_stamp:
        return None, "benchmark_context_stale", metadata
    if any(stamp not in by_timestamp for stamp in stamps):
        return None, "benchmark_context_misaligned", metadata
    benchmark = [by_timestamp[stamp] for stamp in stamps]
    subject_start = _number(subject[0], "close")
    subject_end = _number(subject[-1], "close")
    benchmark_start = _number(benchmark[0], "close")
    benchmark_end = _number(benchmark[-1], "close")
    prices = (subject_start, subject_end, benchmark_start, benchmark_end)
    if any(price is None or price <= 0 for price in prices):
        return None, "cross_sectional_price_unavailable", metadata
    symbol_return = subject_end / subject_start - 1.0
    benchmark_return = benchmark_end / benchmark_start - 1.0
    residual = symbol_return - benchmark_return
    context_digest = _context_digest(benchmark)
    metadata.update({
        "subject_symbol": subject_symbol,
        "symbol_return": symbol_return,
        "benchmark_return": benchmark_return,
        "residual_return": residual,
        "market_context_digest": context_digest,
        "candidate_behavior_identity": rule_behavior_identity(
            spec, market_context_digest=context_digest),
    })
    threshold = float(spec["threshold_bps"]) / 10_000.0
    if residual > threshold:
        return "long", "passed", metadata
    if residual < -threshold:
        return "short", "passed", metadata
    return None, "residual_threshold_not_met", metadata


def _sma(values: Sequence[float], length: int) -> float:
    return mean(values[-length:])


def _atr(rows: Sequence[Any], period: int) -> float | None:
    if len(rows) < period + 1:
        return None
    values = []
    for previous, current in zip(rows[-period - 1:-1], rows[-period:]):
        high = _number(current, "high")
        low = _number(current, "low")
        previous_close = _number(previous, "close")
        values.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    result = mean(values) if values else 0.0
    return result if result > 0 and math.isfinite(result) else None


def _allowed(direction: str, spec: Mapping[str, Any]) -> bool:
    return spec["side"] == "both" or spec["side"] == direction


def _session_minutes(stamp: datetime) -> float:
    """Minutes from the 09:30 New York open to *stamp*, on its own local day."""

    zone = ZoneInfo("America/New_York")
    local = stamp.astimezone(zone)
    opened = datetime.combine(local.date(), time(9, 30), tzinfo=zone)
    return (local.timestamp() - opened.timestamp()) / 60.0


def _within_entry_window(spec: Mapping[str, Any], stamp: datetime) -> bool:
    if spec.get("schema") not in {RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        return True
    elapsed = _session_minutes(stamp)
    return (float(spec["entry_after_minutes"]) <= elapsed <
            float(spec["entry_before_minutes"]))


def session_minutes(stamp: datetime) -> float:
    """Minutes from the 09:30 New York open to *stamp*, on its own local day."""

    return _session_minutes(stamp)


def entry_window_bounds(value: Mapping[str, Any]) -> tuple[float, float]:
    """The minutes-from-open interval in which *value* may emit a signal.

    Diagnostics that need a matched null draw must restrict themselves to the
    bars a rule was actually allowed to enter on.  Exposing the same bounds
    ``_within_entry_window`` enforces keeps that null tied to the executable
    predicate instead of a copy that can drift away from it.
    """
    spec = validate_rule_spec(value)
    if spec.get("schema") not in {RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        return 0.0, float(SESSION_MINUTES)
    return (float(spec["entry_after_minutes"]),
            float(spec["entry_before_minutes"]))


def _within_volatility_band(spec: Mapping[str, Any], atr: float,
                            close: float) -> bool:
    if spec.get("schema") not in {RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        return True
    if close <= 0:
        return False
    bps = atr / close * 10_000
    return float(spec["min_atr_bps"]) <= bps <= float(spec["max_atr_bps"])


def _confirmations_pass(direction: str, rows: Sequence[Any],
                        spec: Mapping[str, Any]) -> bool:
    """Every confirmation the spec names must hold, v1 single or v2 list."""

    if not _confirmation(direction, rows, spec, spec["confirmation"]):
        return False
    for kind in spec.get("confirmations") or ():
        if not _confirmation(direction, rows, spec, kind):
            return False
    return True


def _confirmation(direction: str, rows: Sequence[Any], spec: Mapping[str, Any],
                  kind: str | None = None) -> bool:
    kind = spec["confirmation"] if kind is None else kind
    if kind == "none":
        return True
    closes = [_number(row, "close") for row in rows]
    if kind == "trend":
        if len(closes) < spec["slow_lookback"]:
            return False
        fast = _sma(closes, spec["lookback"])
        slow = _sma(closes, spec["slow_lookback"])
        return fast > slow if direction == "long" else fast < slow
    if kind == "volume":
        if len(rows) <= spec["lookback"]:
            return False
        prior = [_number(row, "volume") for row in rows[-spec["lookback"] - 1:-1]]
        return bool(prior and _number(rows[-1], "volume") >= mean(prior) * spec["volume_multiplier"])
    atr = _atr(rows, spec["atr_period"])
    close = closes[-1] if closes else 0.0
    # ``compression_bps`` is an upper bound everywhere: volatility
    # confirmation admits compressed (low-ATR) prefixes, matching the
    # volatility-breakout family's range gate.  The previous lower-bound
    # polarity made a single ``volatility`` confirmation contradict the
    # breakout predicate and silently selected expanded regimes instead.
    return bool(atr and close > 0 and atr / close * 10_000 <= spec["compression_bps"])


def _session_prefix(bars: Sequence[Any], current: Any) -> list[Any]:
    """The completed bars of *current*'s own New York session, in order.

    Session statistics reset daily. Research replays one session at a time and
    the runtime fetches from the session open, so this is normally the whole
    input — but deriving it from the bars' own dates means a longer history can
    never contaminate a session-anchored family.
    """
    stamp = _timestamp(current)
    if stamp is None:
        return []
    zone = ZoneInfo("America/New_York")
    day = stamp.astimezone(zone).date()
    prefix = []
    for row in bars:
        row_stamp = _timestamp(row)
        if row_stamp is not None and row_stamp.astimezone(zone).date() == day:
            prefix.append(row)
    return prefix


def _vwap(bars: Sequence[Any]) -> float | None:
    """Volume-weighted average typical price over *bars*, or ``None``."""
    weighted = 0.0
    volume = 0.0
    for row in bars:
        size = _number(row, "volume")
        if size <= 0:
            continue
        typical = (_number(row, "high") + _number(row, "low") +
                   _number(row, "close")) / 3.0
        weighted += typical * size
        volume += size
    if volume <= 0:
        return None
    result = weighted / volume
    return result if result > 0 and math.isfinite(result) else None


def frozen_target_reference(rows: Sequence[Any],
                            value: Mapping[str, Any]) -> float | None:
    """Freeze a v4 non-R target from the completed prefix available now."""

    spec = validate_rule_spec(value)
    target_mode = str(spec.get("target_mode") or "fixed_r")
    if target_mode == "fixed_r":
        return None
    bars = list(rows)
    if not bars:
        return None
    if target_mode == "session_vwap":
        return _vwap(_session_prefix(bars, bars[-1]))
    lookback = int(spec.get("target_lookback", 20))
    if len(bars) < lookback:
        return None
    result = mean(_number(row, "close") for row in bars[-lookback:])
    return result if result > 0 and math.isfinite(result) else None


def _complete_opening_window(bars: Sequence[Any], current: Any,
                             range_minutes: int) -> list[Any] | None:
    """Return the exact 09:30 one-minute opening window, or fail closed."""
    stamp = _timestamp(current)
    if stamp is None:
        return None
    zone = ZoneInfo("America/New_York")
    local_day = stamp.astimezone(zone).date()
    start = datetime.combine(local_day, time(9, 30), tzinfo=zone)
    minutes = int(range_minutes)
    if minutes < 1:
        return None
    expected = [start + timedelta(minutes=index) for index in range(minutes)]
    by_minute: dict[datetime, Any] = {}
    for row in bars:
        row_stamp = _timestamp(row)
        if row_stamp is None:
            continue
        local = row_stamp.astimezone(zone)
        if local.second or local.microsecond:
            continue
        if start <= local < start + timedelta(minutes=minutes):
            by_minute.setdefault(local, row)
    if any(point not in by_minute for point in expected):
        return None
    return [by_minute[point] for point in expected]


def _family_direction(bars: Sequence[Any], spec: Mapping[str, Any], *,
                      close: float, opened: float) -> tuple[str | None, str]:
    """Evaluate only the family predicate shared by execution and diagnostics."""
    current = bars[-1]
    closes = [_number(row, "close") for row in bars]
    threshold = spec["threshold_bps"] / 10_000.0
    direction: str | None = None
    family = spec["family"]
    reason = "family_predicate_not_met"

    if family.startswith("opening_range_"):
        zone = ZoneInfo("America/New_York")
        stamp = _timestamp(current)
        if stamp is None:
            return None, "timestamp_unavailable"
        local_day = stamp.astimezone(zone).date()
        start = datetime.combine(local_day, time(9, 30), tzinfo=zone)
        end = start.timestamp() + spec["range_minutes"] * 60
        if stamp.timestamp() < end:
            return None, "opening_window_incomplete"
        opening = _complete_opening_window(
            bars, current, int(spec["range_minutes"]))
        if opening is None:
            return None, "opening_window_undercovered"
        high = max(_number(row, "high") for row in opening)
        low = min(_number(row, "low") for row in opening)
        if family == "opening_range_breakout":
            if close > high * (1 + threshold):
                direction = "long"
            elif close < low * (1 - threshold):
                direction = "short"
        else:
            if _number(current, "high") > high * (1 + threshold) and close < high:
                direction = "short"
            elif _number(current, "low") < low * (1 - threshold) and close > low:
                direction = "long"
    elif family == "momentum_continuation":
        reference = closes[-spec["lookback"] - 1]
        move = close / reference - 1 if reference > 0 else 0.0
        if move > threshold and close > closes[-2]:
            direction = "long"
        elif move < -threshold and close < closes[-2]:
            direction = "short"
    elif family == "mean_reversion":
        window = closes[-spec["lookback"]:]
        average = mean(window)
        deviation = pstdev(window)
        score = (close - average) / deviation if deviation > 0 else 0.0
        if score <= -spec["zscore"]:
            direction = "long"
        elif score >= spec["zscore"]:
            direction = "short"
    elif family == "trend_pullback":
        if len(closes) < spec["slow_lookback"]:
            return None, "slow_lookback_unavailable"
        fast = _sma(closes, spec["lookback"])
        slow = _sma(closes, spec["slow_lookback"])
        near_fast = abs(close - fast) / fast <= max(threshold, .0005)
        if fast > slow and near_fast and close > opened:
            direction = "long"
        elif fast < slow and near_fast and close < opened:
            direction = "short"
    elif family == "volatility_breakout":
        previous = bars[-spec["lookback"] - 1:-1]
        high = max(_number(row, "high") for row in previous)
        low = min(_number(row, "low") for row in previous)
        width_bps = (high - low) / close * 10_000
        if width_bps <= spec["compression_bps"]:
            if close > high * (1 + threshold):
                direction = "long"
            elif close < low * (1 - threshold):
                direction = "short"
        else:
            reason = "compression_filter_failed"
    elif family == "volume_breakout":
        previous = bars[-spec["lookback"] - 1:-1]
        high = max(_number(row, "high") for row in previous)
        low = min(_number(row, "low") for row in previous)
        average_volume = mean(_number(row, "volume") for row in previous)
        volume_ok = average_volume > 0 and _number(current, "volume") >= (
            average_volume * spec["volume_multiplier"])
        if volume_ok and close > high * (1 + threshold):
            direction = "long"
        elif volume_ok and close < low * (1 - threshold):
            direction = "short"
        elif not volume_ok:
            reason = "family_volume_filter_failed"
    elif family in {"vwap_reversion", "vwap_trend"}:
        session = _session_prefix(bars, current)
        if len(session) < spec["lookback"] + 1:
            return None, "session_lookback_unavailable"
        vwap = _vwap(session)
        if vwap is None:
            return None, "vwap_unavailable"
        deviation = close / vwap - 1
        if family == "vwap_reversion":
            if deviation <= -max(threshold, 1e-9):
                direction = "long"
            elif deviation >= max(threshold, 1e-9):
                direction = "short"
        else:
            earlier = _vwap(session[:-spec["lookback"]])
            if earlier is None:
                return None, "earlier_vwap_unavailable"
            if close > vwap and vwap > earlier * (1 + threshold):
                direction = "long"
            elif close < vwap and vwap < earlier * (1 - threshold):
                direction = "short"
    elif family == "range_expansion":
        previous = bars[-spec["lookback"] - 1:-1]
        ranges = [_number(row, "high") - _number(row, "low") for row in previous]
        average_range = mean(ranges) if ranges else 0.0
        current_range = _number(current, "high") - _number(current, "low")
        if (average_range > 0 and
                current_range >= average_range * spec["volume_multiplier"]):
            if close > opened * (1 + threshold):
                direction = "long"
            elif close < opened * (1 - threshold):
                direction = "short"
        else:
            reason = "range_expansion_filter_failed"
    elif family == "opening_drive":
        zone = ZoneInfo("America/New_York")
        stamp = _timestamp(current)
        if stamp is None:
            return None, "timestamp_unavailable"
        session = _session_prefix(bars, current)
        start = datetime.combine(stamp.astimezone(zone).date(), time(9, 30),
                                 tzinfo=zone)
        end = start.timestamp() + spec["range_minutes"] * 60
        if stamp.timestamp() < end:
            return None, "opening_window_incomplete"
        opening = _complete_opening_window(
            session, current, int(spec["range_minutes"]))
        if opening is None:
            return None, "opening_window_undercovered"
        first = _number(opening[0], "open", _number(opening[0], "close"))
        last = _number(opening[-1], "close")
        drive = last / first - 1 if first > 0 else 0.0
        if drive > threshold and close > opened:
            direction = "long"
        elif drive < -threshold and close < opened:
            direction = "short"
    return direction, "passed" if direction is not None else reason


def _evaluate_rule_signal_staged(
        rows: Sequence[Any], value: Mapping[str, Any], *, trace: bool,
        bars_by_symbol: Mapping[str, Sequence[Any]] | None = None,
        symbol: str | None = None,
) -> tuple[dict | None, list[dict[str, Any]], dict[str, Any]]:
    """One evaluator for executable signals and non-authorizing stage traces."""
    spec = validate_rule_spec(value)
    bars = list(rows)
    stages: list[dict[str, Any]] = []
    family_metadata: dict[str, Any] = {}

    def stage(name: str, passed: bool, reason: str) -> bool:
        if trace:
            stages.append({"stage": name, "tested": True,
                           "passed": bool(passed), "reason": str(reason)})
        return passed

    needed = _causal_maturity_bars(spec)
    if not stage("minimum_prefix", len(bars) >= needed,
                 "passed" if len(bars) >= needed else "insufficient_prefix"):
        return None, stages, family_metadata
    current = bars[-1]
    close = _number(current, "close")
    opened = _number(current, "open", close)
    if not stage("positive_close", close > 0,
                 "passed" if close > 0 else "nonpositive_close"):
        return None, stages, family_metadata
    if spec["family"] == "cross_sectional_residual":
        direction, family_reason, family_metadata = _cross_sectional_direction(
            bars, spec, bars_by_symbol=bars_by_symbol, symbol=symbol)
    else:
        direction, family_reason = _family_direction(
            bars, spec, close=close, opened=opened)
    if not stage("family_predicate", direction is not None, family_reason):
        return None, stages, family_metadata
    assert direction is not None
    if not stage("side", _allowed(direction, spec),
                 "passed" if _allowed(direction, spec) else "direction_not_allowed"):
        return None, stages, family_metadata
    confirmations = [str(spec["confirmation"]),
                     *(str(item) for item in spec.get("confirmations") or ())]
    for kind in confirmations:
        passed = _confirmation(direction, bars, spec, kind)
        if not stage(f"confirmation:{kind}", passed,
                     "passed" if passed else "confirmation_failed"):
            return None, stages, family_metadata
    atr = _atr(bars, spec["atr_period"])
    if not stage("atr", atr is not None,
                 "passed" if atr is not None else "atr_unavailable"):
        return None, stages, family_metadata
    assert atr is not None
    volatility_ok = _within_volatility_band(spec, atr, close)
    if not stage("volatility_band", volatility_ok,
                 "passed" if volatility_ok else "volatility_band_failed"):
        return None, stages, family_metadata
    distance = max(atr * spec["stop_atr"],
                   close * MIN_STOP_DISTANCE_FRACTION)
    stop = close - distance if direction == "long" else close + distance
    target_mode = str(spec.get("target_mode") or "fixed_r")
    target_reference = frozen_target_reference(bars, spec)
    if target_mode == "fixed_r":
        target = (close + distance * spec["target_r"] if direction == "long" else
                  close - distance * spec["target_r"])
    else:
        target = float(target_reference) if target_reference is not None else None
        # A mean/VWAP target is frozen from the completed signal prefix.  If it
        # lies behind the entry it cannot be a valid one-leg take-profit, so
        # reject this signal rather than silently substituting an R target.
        if target is None or ((direction == "long" and target <= close) or
                              (direction == "short" and target >= close)):
            if not stage("target", False, "target_reference_unavailable_or_wrong_side"):
                return None, stages, family_metadata
    if not stage("target", target is not None and target > 0,
                 "passed" if target is not None and target > 0 else "target_invalid"):
        return None, stages, family_metadata
    stamp = _timestamp(current)
    if not stage("timestamp", stamp is not None,
                 "passed" if stamp is not None else "timestamp_unavailable"):
        return None, stages, family_metadata
    assert stamp is not None
    window_ok = _within_entry_window(spec, stamp)
    if not stage("entry_window", window_ok,
                 "passed" if window_ok else "entry_window_failed"):
        return None, stages, family_metadata
    result = {
        "direction": direction,
        "setup_type": f"rule_{spec['family']}",
        "family": spec["family"],
        "signal_ts": stamp.timestamp(),
        "signal_timestamp": stamp.isoformat(),
        "entry_price": close,
        "stop_price": stop,
        "target_price": target,
        "stop_distance": distance,
        "target_r": spec["target_r"],
        "max_hold_bars": spec["max_hold_bars"],
        "rule_spec_hash": rule_spec_hash(spec),
        "confidence": 1.0,
    }
    if spec["schema"] in {RULE_SCHEMA_V3, RULE_SCHEMA_V4}:
        result.update({"rule_schema": RULE_SCHEMA_V3,
                       "breakeven_r": spec.get("breakeven_r")})
    if spec["schema"] == RULE_SCHEMA_V4:
        result.update({"rule_schema": RULE_SCHEMA_V4,
                       "target_mode": target_mode,
                       "target_reference": target_reference,
                       "target_lookback": spec.get("target_lookback"),
                       "trailing_stop_r": spec.get("trailing_stop_r"),
                       "exit_before_minutes": spec.get("exit_before_minutes")})
    if spec["family"] == "cross_sectional_residual":
        result.update(family_metadata)
    stage("signal", True, "emitted")
    return result, stages, family_metadata


def evaluate_rule_signal(
        rows: Sequence[Any], value: Mapping[str, Any], *,
        bars_by_symbol: Mapping[str, Sequence[Any]] | None = None,
        symbol: str | None = None) -> dict | None:
    """Evaluate one completed-bar prefix and return a deterministic signal."""
    return _evaluate_rule_signal_staged(
        rows, value, trace=False, bars_by_symbol=bars_by_symbol,
        symbol=symbol)[0]


def evaluate_rule_signal_trace(rows: Sequence[Any],
                               value: Mapping[str, Any], *,
                               bars_by_symbol: Mapping[str, Sequence[Any]] | None = None,
                               symbol: str | None = None) -> dict[str, Any]:
    """Return compact, non-authorizing predicate telemetry for one prefix."""
    signal, stages, family_metadata = _evaluate_rule_signal_staged(
        rows, value, trace=True, bars_by_symbol=bars_by_symbol, symbol=symbol)
    result = {
        "schema": "rule-signal-trace.v1",
        "authorizing": False,
        "diagnostic_only": True,
        "signal": signal,
        "terminal_stage": stages[-1]["stage"] if stages else None,
        "stages": stages,
    }
    if validate_rule_spec(value)["family"] == "cross_sectional_residual":
        result["market_context"] = family_metadata
    return result


def evaluate_rule_signal_metadata(rows: Sequence[Any],
                                 value: Mapping[str, Any], *,
                                 bars_by_symbol: Mapping[str, Sequence[Any]] | None = None,
                                 symbol: str | None = None) -> dict | None:
    """Return non-authorizing metadata for one evaluated signal.

    This deliberately delegates signal authorization to
    :func:`evaluate_rule_signal`; the additional fields are an observability
    surface for fit diagnostics only.  They expose the ATR and the derived
    30-bps floor decision without changing the authored rule or runtime
    signal contract.
    """
    signal = evaluate_rule_signal(
        rows, value, bars_by_symbol=bars_by_symbol, symbol=symbol)
    if signal is None:
        return None
    spec = validate_rule_spec(value)
    bars = list(rows)
    close = float(signal.get("entry_price") or 0.0)
    atr = _atr(bars, spec["atr_period"])
    atr_bps = (float(atr) / close * 10_000.0
               if atr is not None and close > 0 else None)
    authored_distance = (float(atr) * float(spec["stop_atr"])
                         if atr is not None else None)
    floor_distance = close * MIN_STOP_DISTANCE_FRACTION if close > 0 else None
    return {
        **signal,
        "atr": atr,
        "atr_bps": atr_bps,
        "authored_stop_distance": authored_distance,
        "floor_distance": floor_distance,
        "floor_binding": bool(
            authored_distance is not None and floor_distance is not None and
            floor_distance >= authored_distance),
        "planned_stop_distance": signal.get("stop_distance"),
        "planned_target_distance": (
            abs(float(signal["target_price"]) - close)
            if signal.get("target_price") is not None else None),
        "planned_hold_bars": spec.get("max_hold_bars"),
    }


# Friendly diagnostic spelling retained as a compatibility alias.  Neither
# helper participates in runtime authorization or order placement.
rule_signal_metadata = evaluate_rule_signal_metadata


def generate_rule_signal(
        symbol: str, bars: Sequence[Any], *, config: Mapping[str, Any],
        now: datetime | None = None,
        bars_by_symbol: Mapping[str, Sequence[Any]] | None = None) -> dict | None:
    strategy = config.get("strategy", config) if isinstance(config, Mapping) else {}
    spec = strategy.get("rule_spec") if isinstance(strategy, Mapping) else None
    if not isinstance(spec, Mapping):
        return None
    normalized = validate_rule_spec(spec)
    if (normalized["family"] == "cross_sectional_residual" and
            str(strategy.get("execution_mode") or "").lower() not in
            {"share", "shares", "equity"}):
        return None
    signal = evaluate_rule_signal(
        bars, normalized, bars_by_symbol=bars_by_symbol, symbol=symbol)
    if signal is None:
        return None
    stamp = datetime.fromtimestamp(float(signal["signal_ts"]), timezone.utc)
    if now is not None:
        current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if stamp > current:
            return None
    signal["symbol"] = str(symbol).upper()
    signal["session"] = stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    return signal


def setup_evidence(snapshot: Mapping[str, Any], config: Mapping[str, Any]) -> dict:
    strategy = config.get("strategy", config) if isinstance(config, Mapping) else {}
    spec = validate_rule_spec(strategy.get("rule_spec") or {})
    evidence = {
        "schema": spec["schema"],
        "family": spec["family"],
        "rule_spec_hash": rule_spec_hash(spec),
        "signal_ts": snapshot.get("signal_ts"),
        "entry_price": snapshot.get("entry_price", snapshot.get("price")),
        "stop_price": snapshot.get("stop_price"),
        "target_price": snapshot.get("target_price"),
    }
    if spec["schema"] == RULE_SCHEMA_V4:
        evidence.update({
            "target_mode": snapshot.get("target_mode", spec.get("target_mode")),
            "target_reference": snapshot.get("target_reference"),
            "target_lookback": snapshot.get("target_lookback",
                                            spec.get("target_lookback")),
            "trailing_stop_r": snapshot.get("trailing_stop_r",
                                             spec.get("trailing_stop_r")),
            "exit_before_minutes": snapshot.get("exit_before_minutes",
                                                spec.get("exit_before_minutes")),
            "exit_before_ts": snapshot.get("exit_before_ts"),
        })
    if spec["family"] == "cross_sectional_residual":
        evidence.update({
            "benchmark_symbol": snapshot.get(
                "benchmark_symbol", CROSS_SECTIONAL_BENCHMARK),
            "market_context_digest": snapshot.get("market_context_digest"),
            "candidate_behavior_identity": snapshot.get(
                "candidate_behavior_identity"),
        })
    return evidence


__all__ = [
    "BAR_SECONDS", "CONFIRMATIONS", "CROSS_SECTIONAL_BENCHMARK",
    "CROSS_SECTIONAL_DEFAULT_ELIGIBLE_SYMBOLS",
    "CROSS_SECTIONAL_MAX_ELIGIBLE_SYMBOLS",
    "CANONICAL_EXIT_REASONS",
    "DEFAULT_RULE_SPEC", "MAX_CONFIRMATIONS",
    "EXIT_REASON_DATA_DISCONTINUITY", "EXIT_REASON_MAX_HOLD",
    "EXIT_REASON_SESSION_FORCE_FLAT", "EXIT_REASON_STOP",
    "EXIT_REASON_TARGET", "EXIT_REASON_THESIS_DEADLINE",
    "EXIT_REASON_UNKNOWN",
    "MIN_STOP_DISTANCE_BPS", "MIN_STOP_DISTANCE_FRACTION",
    "RULE_FAMILIES", "RULE_SCHEMA", "RULE_SCHEMAS", "RULE_SCHEMA_V1",
           "RULE_SCHEMA_V2", "RULE_SCHEMA_V3", "RULE_SCHEMA_V4", "SESSION_MINUTES",
           "V2_DEFAULT_EXTENSIONS", "V3_DEFAULT_EXTENSIONS", "V4_DEFAULT_EXTENSIONS",
           "EXECUTABLE_RULE_FIELDS", "SESSION_ACCUMULATING_FAMILIES",
           "OPENING_ANCHORED_FAMILIES",
           "entry_window_bounds", "session_minutes",
           "causal_maturity_bars", "feature_window_bars", "rule_behavior_identity",
           "cross_sectional_symbol_eligibility",
           "rule_semantic_signature",
           "rule_semantic_distance", "rule_spec_json_schema",
    "RuleSpecError", "breakeven_stop_price", "canonical_exit_reason",
    "completed_bar_exit_transition", "exit_deadline",
    "evaluate_rule_signal", "evaluate_rule_signal_trace",
    "evaluate_rule_signal_metadata", "frozen_target_reference",
    "initialize_exit_state",
    "rule_signal_metadata",
    "generate_rule_signal", "hold_deadline", "thesis_exit_deadline", "rule_spec_hash",
    "rule_variant_id", "rule_vehicle_executable", "setup_evidence",
    "validate_rule_spec",
]


register("rule", setup_evidence)
