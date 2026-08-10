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


RULE_SCHEMA = "rule-strategy.v1"
RULE_FAMILIES = (
    "opening_range_breakout",
    "opening_range_fade",
    "momentum_continuation",
    "mean_reversion",
    "trend_pullback",
    "volatility_breakout",
    "volume_breakout",
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


class RuleSpecError(ValueError):
    """Raised when an autonomous rule leaves the audited grammar."""


def validate_rule_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuleSpecError("rule_spec must be a mapping")
    unknown = sorted(set(value) - set(DEFAULT_RULE_SPEC))
    if unknown:
        raise RuleSpecError(f"rule_spec has unknown field(s): {', '.join(unknown)}")
    spec = dict(DEFAULT_RULE_SPEC)
    spec.update(value)
    if spec.get("schema") != RULE_SCHEMA:
        raise RuleSpecError(f"rule_spec.schema must be {RULE_SCHEMA!r}")
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


def _confirmation(direction: str, rows: Sequence[Any], spec: Mapping[str, Any]) -> bool:
    kind = spec["confirmation"]
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

    if direction is None or not _allowed(direction, spec) or not _confirmation(direction, bars, spec):
        return None
    atr = _atr(bars, spec["atr_period"])
    if atr is None:
        return None
    distance = max(atr * spec["stop_atr"], close * .0005)
    stop = close - distance if direction == "long" else close + distance
    target = close + distance * spec["target_r"] if direction == "long" else close - distance * spec["target_r"]
    stamp = _timestamp(current)
    if stamp is None:
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
        "schema": RULE_SCHEMA,
        "family": spec["family"],
        "rule_spec_hash": rule_spec_hash(spec),
        "signal_ts": snapshot.get("signal_ts"),
        "entry_price": snapshot.get("entry_price", snapshot.get("price")),
        "stop_price": snapshot.get("stop_price"),
        "target_price": snapshot.get("target_price"),
    }


__all__ = [
    "BAR_SECONDS", "CONFIRMATIONS", "DEFAULT_RULE_SPEC", "RULE_FAMILIES",
    "RULE_SCHEMA", "RuleSpecError", "evaluate_rule_signal",
    "generate_rule_signal", "hold_deadline", "rule_spec_hash",
    "rule_variant_id", "setup_evidence", "validate_rule_spec",
]


register("rule", setup_evidence)
