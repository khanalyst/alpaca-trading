"""Bounded, data-only rule strategies shared by research and paper execution.

The autonomous research loop may create and mutate these specifications, but
it cannot create Python source.  Every accepted field has a finite range and
every signal is evaluated from completed bars only.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
import hashlib
import json
import math
from statistics import mean, pstdev
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import register


RULE_SCHEMA_V1 = "rule-strategy.v1"
RULE_SCHEMA_V2 = "rule-strategy.v2"
RULE_SCHEMAS = (RULE_SCHEMA_V1, RULE_SCHEMA_V2)
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
)
CONFIRMATIONS = ("none", "trend", "volume", "volatility")
SIDES = ("both", "long", "short")
BAR_SECONDS = 60.0

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
_EXTRA_CONFIRMATIONS = tuple(name for name in CONFIRMATIONS if name != "none")
MAX_CONFIRMATIONS = len(_EXTRA_CONFIRMATIONS)

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
}


# Families whose statistics accumulate from the session open.  A missing bar
# anywhere earlier in the session changes what they compute, so their feature
# window is the whole session prefix rather than a bounded trailing count.
SESSION_ACCUMULATING_FAMILIES = frozenset(("vwap_reversion", "vwap_trend"))
# Trailing completed bars each remaining family reads, beyond the prefix
# ``evaluate_rule_signal`` always consumes.  Opening-anchored families are
# absent on purpose: their window is selected by clock time and is already
# guarded by an explicit minimum-bar count, not by adjacency.
_FAMILY_FEATURE_BARS = {
    "momentum_continuation": lambda spec: spec["lookback"] + 2,
    "mean_reversion": lambda spec: spec["lookback"],
    "trend_pullback": lambda spec: spec["slow_lookback"],
    "volatility_breakout": lambda spec: spec["lookback"] + 1,
    "volume_breakout": lambda spec: spec["lookback"] + 1,
    "range_expansion": lambda spec: spec["lookback"] + 1,
}
_CONFIRMATION_FEATURE_BARS = {
    "trend": lambda spec: spec["slow_lookback"],
    "volume": lambda spec: spec["lookback"] + 1,
    "volatility": lambda spec: spec["atr_period"] + 1,
}


def feature_window_bars(value: Mapping[str, Any]) -> int | None:
    """Trailing completed bars :func:`evaluate_rule_signal` reads for a spec.

    ``None`` means the family accumulates from the session open and has no
    bounded trailing window.

    Replay uses this to require adjacency over exactly the bars a signal is
    computed from.  A fixed lookback silently stretched across an outage is a
    different statistic than the one the spec names, so the bars it reads must
    be consecutive — but a minute missing *after* that window cannot change the
    signal, and deleting the observation for it would discard good evidence.
    """
    spec = validate_rule_spec(value)
    family = str(spec["family"])
    if family in SESSION_ACCUMULATING_FAMILIES:
        return None
    # The prefix ``evaluate_rule_signal`` consumes before dispatching a family.
    needed = max(spec["lookback"] + 1, spec["atr_period"] + 1)
    resolve = _FAMILY_FEATURE_BARS.get(family)
    if resolve is not None:
        needed = max(needed, resolve(spec))
    confirmations = {str(spec.get("confirmation") or "none")}
    confirmations.update(str(item) for item in spec.get("confirmations") or ())
    for kind in confirmations:
        resolve = _CONFIRMATION_FEATURE_BARS.get(kind)
        if resolve is not None:
            needed = max(needed, resolve(spec))
    return int(needed)


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
    if spec.get("schema") == RULE_SCHEMA_V2:
        for name, default in V2_DEFAULT_EXTENSIONS.items():
            current = spec.get(name, default)
            if current != default:
                effective[name] = current
    return json.dumps(effective, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def rule_semantic_distance(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    """Return a small deterministic normalized distance between two rules.

    The metric is intentionally transparent (no fuzzy model): categorical
    executable changes cost one, and numeric changes are normalized by their
    audited grammar span.  A distance of zero means semantic equivalence.
    """
    a = validate_rule_spec(left)
    b = validate_rule_spec(right)
    if a["family"] != b["family"]:
        return 1.0
    distance = 0.0
    dimensions = 0
    bounds = {**_BOUNDS, **_V2_BOUNDS}
    fields = _semantic_fields(a) | _semantic_fields(b)
    for name, default in V2_DEFAULT_EXTENSIONS.items():
        if a.get(name, default) != default or b.get(name, default) != default:
            fields.add(name)
    for name in sorted(fields):
        av, bv = a.get(name), b.get(name)
        dimensions += 1
        if name == "confirmations":
            distance += 0.0 if set(av or ()) == set(bv or ()) else 1.0
        elif name in bounds and isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            low, high, _ = bounds[name]
            span = max(float(high) - float(low), 1.0)
            distance += min(1.0, abs(float(av) - float(bv)) / span)
        else:
            distance += 0.0 if av == bv else 1.0
    return distance / max(dimensions, 1)


def rule_spec_json_schema(schema: str | None = None) -> dict[str, Any]:
    """Expose the complete provider-facing JSON grammar and audited bounds."""
    schemas = [schema] if schema is not None else list(RULE_SCHEMAS)
    if any(item not in RULE_SCHEMAS for item in schemas):
        raise RuleSpecError(f"unknown rule schema: {schema!r}")

    def one(name: str) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "schema": {"type": "string", "const": name},
            "family": {"type": "string", "enum": list(RULE_FAMILIES)},
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
        if name == RULE_SCHEMA_V2:
            properties.update({
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
        return {"type": "object", "additionalProperties": False,
                "required": required, "properties": properties}

    if len(schemas) == 1:
        return one(schemas[0])
    return {"oneOf": [one(name) for name in schemas]}


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


def validate_rule_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuleSpecError("rule_spec must be a mapping")
    schema = value.get("schema", RULE_SCHEMA_V1)
    if schema not in RULE_SCHEMAS:
        raise RuleSpecError(
            f"rule_spec.schema must be one of {', '.join(map(repr, RULE_SCHEMAS))}")
    permitted = set(DEFAULT_RULE_SPEC)
    if schema == RULE_SCHEMA_V2:
        permitted |= set(V2_DEFAULT_EXTENSIONS)
    unknown = sorted(set(value) - permitted)
    if unknown:
        # A v1 spec naming a v2 field is a version error, not a typo: say so.
        extensions = [name for name in unknown if name in V2_DEFAULT_EXTENSIONS]
        if extensions:
            raise RuleSpecError(
                f"rule_spec field(s) {', '.join(extensions)} require "
                f"schema {RULE_SCHEMA_V2!r}")
        raise RuleSpecError(f"rule_spec has unknown field(s): {', '.join(unknown)}")
    spec = dict(DEFAULT_RULE_SPEC)
    if schema == RULE_SCHEMA_V2:
        spec.update(V2_DEFAULT_EXTENSIONS)
    spec.update(value)
    spec["schema"] = schema
    if spec.get("family") not in RULE_FAMILIES:
        raise RuleSpecError(f"unsupported rule family: {spec.get('family')!r}")
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
    if schema != RULE_SCHEMA_V2:
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
    return spec


def hold_deadline(entry_ts: Any, spec: Mapping[str, Any], *,
                  force_flat_ts: Any = None) -> float | None:
    """Absolute epoch second by which a bounded rule position must be flat.

    *entry_ts* is the opening timestamp of the entry bar, which is the bar
    after the signal bar.  The position is held for at most `max_hold_bars`
    further one-minute bars, so the deadline is the close of the last
    permitted bar.  Research simulation and live execution share this one
    definition; a session force-flat always clamps it.  A spec without
    `max_hold_bars` has no time exit.
    """
    held = spec.get("max_hold_bars") if isinstance(spec, Mapping) else None
    if held is None:
        return None
    lower, upper, _ = _BOUNDS["max_hold_bars"]
    if isinstance(held, bool) or not isinstance(held, int):
        raise RuleSpecError("rule_spec.max_hold_bars must be an integer")
    if not lower <= held <= upper:
        raise RuleSpecError(
            f"rule_spec.max_hold_bars must be between {lower:g} and {upper:g}")
    start = _epoch(entry_ts, "entry timestamp")
    deadline = start + (held + 1) * BAR_SECONDS
    if force_flat_ts is not None:
        deadline = min(deadline, _epoch(force_flat_ts, "force-flat timestamp"))
    return deadline


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
    if spec.get("schema") != RULE_SCHEMA_V2:
        return True
    elapsed = _session_minutes(stamp)
    return (float(spec["entry_after_minutes"]) <= elapsed <
            float(spec["entry_before_minutes"]))


def _within_volatility_band(spec: Mapping[str, Any], atr: float,
                            close: float) -> bool:
    if spec.get("schema") != RULE_SCHEMA_V2:
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
    return bool(atr and close > 0 and atr / close * 10_000 >= spec["compression_bps"])


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


def evaluate_rule_signal(rows: Sequence[Any], value: Mapping[str, Any]) -> dict | None:
    """Evaluate one completed-bar prefix and return a deterministic signal."""
    spec = validate_rule_spec(value)
    bars = list(rows)
    needed = max(spec["lookback"] + 1, spec["atr_period"] + 1)
    if len(bars) < needed:
        return None
    current = bars[-1]
    close = _number(current, "close")
    opened = _number(current, "open", close)
    if close <= 0:
        return None
    closes = [_number(row, "close") for row in bars]
    threshold = spec["threshold_bps"] / 10_000.0
    direction: str | None = None
    family = spec["family"]

    if family.startswith("opening_range_"):
        zone = ZoneInfo("America/New_York")
        stamp = _timestamp(current)
        if stamp is None:
            return None
        local_day = stamp.astimezone(zone).date()
        start = datetime.combine(local_day, time(9, 30), tzinfo=zone)
        end = start.timestamp() + spec["range_minutes"] * 60
        opening = [row for row in bars if (ts := _timestamp(row)) is not None and
                   ts.astimezone(zone).date() == local_day and
                   start.timestamp() <= ts.timestamp() < end]
        if stamp.timestamp() < end or len(opening) < max(2, spec["range_minutes"] // 2):
            return None
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
            return None
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
    elif family in {"vwap_reversion", "vwap_trend"}:
        session = _session_prefix(bars, current)
        if len(session) < spec["lookback"] + 1:
            return None
        vwap = _vwap(session)
        if vwap is None:
            return None
        deviation = close / vwap - 1
        if family == "vwap_reversion":
            # Stretched away from the session's own average price, expected to
            # revert toward it.
            if deviation <= -max(threshold, 1e-9):
                direction = "long"
            elif deviation >= max(threshold, 1e-9):
                direction = "short"
        else:
            # Trading with a session average that is itself advancing: the
            # trend statistic is the session's fair value, not a moving average
            # of price, so this is not `trend_pullback` under another name.
            earlier = _vwap(session[:-spec["lookback"]])
            if earlier is None:
                return None
            if close > vwap and vwap > earlier * (1 + threshold):
                direction = "long"
            elif close < vwap and vwap < earlier * (1 - threshold):
                direction = "short"
    elif family == "range_expansion":
        previous = bars[-spec["lookback"] - 1:-1]
        ranges = [_number(row, "high") - _number(row, "low") for row in previous]
        average_range = mean(ranges) if ranges else 0.0
        current_range = _number(current, "high") - _number(current, "low")
        # `volume_multiplier` is the bounded expansion factor here: the same
        # "multiple of a rolling average" parameter, applied to range.
        if (average_range > 0 and
                current_range >= average_range * spec["volume_multiplier"]):
            if close > opened * (1 + threshold):
                direction = "long"
            elif close < opened * (1 - threshold):
                direction = "short"
    elif family == "opening_drive":
        zone = ZoneInfo("America/New_York")
        stamp = _timestamp(current)
        if stamp is None:
            return None
        session = _session_prefix(bars, current)
        start = datetime.combine(stamp.astimezone(zone).date(), time(9, 30),
                                 tzinfo=zone)
        end = start.timestamp() + spec["range_minutes"] * 60
        opening = [row for row in session
                   if (ts := _timestamp(row)) is not None and
                   start.timestamp() <= ts.timestamp() < end]
        if (stamp.timestamp() < end or
                len(opening) < max(2, spec["range_minutes"] // 2)):
            return None
        first = _number(opening[0], "open", _number(opening[0], "close"))
        last = _number(opening[-1], "close")
        # Net displacement over the opening window, continued rather than
        # faded. Unlike the opening-range families this needs no level break:
        # a session can drive without ever printing a clean range.
        drive = last / first - 1 if first > 0 else 0.0
        if drive > threshold and close > opened:
            direction = "long"
        elif drive < -threshold and close < opened:
            direction = "short"

    if direction is None or not _allowed(direction, spec) or not _confirmations_pass(
            direction, bars, spec):
        return None
    atr = _atr(bars, spec["atr_period"])
    if atr is None:
        return None
    # The v2 entry window and volatility band are the last entry-side gates.
    # They are evaluated from the same completed-bar prefix as the signal, so a
    # spec that passes here in research passes here at runtime.
    if not _within_volatility_band(spec, atr, close):
        return None
    distance = max(atr * spec["stop_atr"], close * .0005)
    stop = close - distance if direction == "long" else close + distance
    target = close + distance * spec["target_r"] if direction == "long" else close - distance * spec["target_r"]
    stamp = _timestamp(current)
    if stamp is None:
        return None
    if not _within_entry_window(spec, stamp):
        return None
    return {
        "direction": direction,
        "setup_type": f"rule_{family}",
        "family": family,
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


def generate_rule_signal(symbol: str, bars: Sequence[Any], *, config: Mapping[str, Any],
                         now: datetime | None = None) -> dict | None:
    strategy = config.get("strategy", config) if isinstance(config, Mapping) else {}
    spec = strategy.get("rule_spec") if isinstance(strategy, Mapping) else None
    if not isinstance(spec, Mapping):
        return None
    signal = evaluate_rule_signal(bars, spec)
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
    return {
        "schema": spec["schema"],
        "family": spec["family"],
        "rule_spec_hash": rule_spec_hash(spec),
        "signal_ts": snapshot.get("signal_ts"),
        "entry_price": snapshot.get("entry_price", snapshot.get("price")),
        "stop_price": snapshot.get("stop_price"),
        "target_price": snapshot.get("target_price"),
    }


__all__ = [
    "BAR_SECONDS", "CONFIRMATIONS", "DEFAULT_RULE_SPEC", "MAX_CONFIRMATIONS",
    "RULE_FAMILIES", "RULE_SCHEMA", "RULE_SCHEMAS", "RULE_SCHEMA_V1",
           "RULE_SCHEMA_V2", "SESSION_MINUTES", "V2_DEFAULT_EXTENSIONS",
           "EXECUTABLE_RULE_FIELDS", "SESSION_ACCUMULATING_FAMILIES",
           "feature_window_bars", "rule_semantic_signature",
           "rule_semantic_distance", "rule_spec_json_schema",
    "RuleSpecError", "evaluate_rule_signal",
    "generate_rule_signal", "hold_deadline", "rule_spec_hash",
    "rule_variant_id", "setup_evidence", "validate_rule_spec",
]


register("rule", setup_evidence)
