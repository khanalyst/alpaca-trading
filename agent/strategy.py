"""IBR strategy boundary and setup idempotency helpers."""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Mapping
from datetime import datetime, timedelta, time as dt_time, timezone
from zoneinfo import ZoneInfo

from .contracts import finite as _finite
from .contracts.ibr import build_ibr_range
from .contracts.rule import (BAR_SECONDS, RULE_SCHEMA_V3, RuleSpecError, hold_deadline,
                             MIN_STOP_DISTANCE_FRACTION, rule_variant_id,
                             validate_rule_spec)

from .registry import (baseline_variant_id, contract_for_variant,
                       validate_contract_config)

SETUP_TYPES = {"ibr_breakout", "range_breakout", "rule_signal"}


def _hash(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()).hexdigest()[:24]


def identity(cfg: Mapping) -> tuple[str, str]:
    strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
    return str(strategy.get("id") or "ibr"), str(strategy.get("version") or "v1")


def variant_identity(cfg: Mapping) -> str:
    strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
    return str(strategy.get("variant_id") or baseline_variant_id(str(strategy.get("id") or "ibr")))


def _range_for_snapshot(snapshot: Mapping, cfg: Mapping) -> dict | None:
    raw = snapshot.get("ibr_range") or snapshot.get("ibr") or snapshot.get("range")
    if isinstance(raw, Mapping):
        try:
            high = float(raw.get("high", raw.get("ibr_high")))
            low = float(raw.get("low", raw.get("ibr_low")))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(high) or not math.isfinite(low) or high <= low:
            return None
        out = dict(raw); out.update({"high": high, "low": low, "width": high - low})
        return out
    bars = snapshot.get("bars") or snapshot.get("candles")
    if isinstance(bars, (list, tuple)):
        return build_ibr_range(bars, config=cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {})
    high = _finite(snapshot.get("ibr_high", snapshot.get("range_high")))
    low = _finite(snapshot.get("ibr_low", snapshot.get("range_low")))
    if high is None or low is None or high <= low:
        return None
    return {"high": high, "low": low, "width": high - low,
            "session": snapshot.get("session") or snapshot.get("ibr_session"),
            "complete": bool(snapshot.get("ibr_complete", True)),
            "range_end_ts": snapshot.get("range_end_ts")}


def _default_force_flat(signal_ts: float, strategy: Mapping) -> str | None:
    try:
        zone = ZoneInfo(str(strategy.get("timezone") or "America/New_York"))
        local = datetime.fromtimestamp(signal_ts, timezone.utc).astimezone(zone)
        minutes = int(float(strategy.get("force_flat_minutes_before_close", 5)))
        return (datetime.combine(local.date(), dt_time(16, 0), tzinfo=zone)
                - timedelta(minutes=max(0, minutes))).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _build_rule_setup_plan(decision: Mapping, symbol_snapshot: Mapping,
                           cfg: Mapping):
    """Validate a content-addressed autonomous rule signal for paper risk."""
    strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
    try:
        spec = validate_rule_spec(strategy.get("rule_spec") or {})
    except ValueError as exc:
        return None, f"invalid autonomous rule: {exc}"
    variant_id = str(strategy.get("variant_id") or "")
    if variant_id != rule_variant_id(spec):
        return None, "autonomous rule variant does not match its content hash"
    try:
        contract = contract_for_variant("rule", variant_id)
        validate_contract_config(cfg)
    except (KeyError, ValueError) as exc:
        return None, f"strategy contract mismatch: {exc}"
    direction = decision.get("direction")
    if direction not in {"long", "short"}:
        return None, "setup direction is invalid"
    setup_type = str(decision.get("setup_type") or "rule_signal")
    if not setup_type.startswith("rule_"):
        return None, "autonomous setup type is not recognised"
    signal_ts = _finite(decision.get("signal_ts", symbol_snapshot.get("signal_ts")))
    if signal_ts is None or signal_ts < 0:
        return None, "completed signal candle timestamp is unavailable"
    if signal_ts > time.time() + 1.0:
        return None, "signal timestamp is in the future"
    entry = _finite(symbol_snapshot.get("price", decision.get("entry_price")))
    if entry is None or entry <= 0:
        entry = _finite(decision.get("entry_price"))
    stop = _finite(decision.get("stop_price"))
    target = _finite(decision.get("target_price"))
    if entry is None or entry <= 0 or stop is None or target is None:
        return None, "autonomous entry, stop, or target is unavailable"
    if (direction == "long" and not (stop < entry < target)) or (
            direction == "short" and not (target < entry < stop)):
        return None, "stop/target side validation failed"
    spread = _finite(symbol_snapshot.get("spread_bps"))
    max_spread = _finite(strategy.get("max_spread_bps"), 25.0) or 25.0
    if spread is None or spread > max_spread:
        return None, "spread is unavailable or too wide"
    if symbol_snapshot.get("stale") is not False or symbol_snapshot.get("quote_stale") is not False:
        return None, "market data freshness is unavailable"
    session = str(decision.get("session") or symbol_snapshot.get("session") or "")
    setup_key = _hash({"strategy_id": "rule", "strategy_version": "v1",
                       "variant_id": variant_id, "symbol": decision.get("symbol"),
                       "direction": direction, "setup_type": setup_type,
                       "session": session})
    force_flat_at = (decision.get("force_flat_at") or
                     symbol_snapshot.get("force_flat_at") or
                     _default_force_flat(signal_ts, strategy))
    try:
        force_flat_ts = datetime.fromisoformat(str(force_flat_at)).timestamp() \
            if force_flat_at else None
    except (TypeError, ValueError, OverflowError):
        force_flat_ts = None
    # The simulated entry is the bar after the signal bar; the runtime holds
    # for the same bounded number of bars beyond it.
    try:
        deadline_ts = hold_deadline(signal_ts + BAR_SECONDS, spec,
                                    force_flat_ts=force_flat_ts)
    except RuleSpecError as exc:
        return None, f"hold deadline is unavailable: {exc}"
    distance = abs(entry - stop)
    plan = dict(decision)
    plan.update({
        "max_hold_bars": int(spec["max_hold_bars"]),
        "hold_deadline_ts": deadline_ts,
        "strategy_id": "rule", "strategy_version": "v1",
        "variant_id": variant_id, "contract_hash": contract.semantic_hash,
        "setup_type": setup_type, "setup_key": setup_key,
        "setup_id": _hash({"setup_key": setup_key, "signal_ts": signal_ts}),
        "signal_ts": signal_ts, "session": session,
        "entry_price": entry, "stop_price": stop, "target_price": target,
        "stop_distance": distance, "target_r": float(spec["target_r"]),
        "stop_loss_pct": round(distance / entry * 100.0, 8),
        "take_profit_pct": round(abs(target - entry) / entry * 100.0, 8),
        "invalidation_anchor": "structure",
        "exit_policy": ("breakeven_then_target_r"
                        if spec.get("breakeven_r") is not None
                        else "fixed_target_r"),
        "execution_choice": str(decision.get("execution_choice") or "normal"),
        "force_flat": True, "force_flat_at": force_flat_at,
        "force_flat_ts": force_flat_ts,
        "force_flat_reason": "regular_session_close", "size_pct_equity": 0.0,
        "rule_spec_hash": decision.get("rule_spec_hash"),
    })
    if spec["schema"] == RULE_SCHEMA_V3:
        plan.update({
            "rule_schema": RULE_SCHEMA_V3,
            "breakeven_r": spec.get("breakeven_r"),
        })
    return plan, None

def build_setup_plan(decision: Mapping, symbol_snapshot: Mapping,
                     cfg: Mapping, *, hypothesis_params: Mapping | None = None):
    """Validate one close-confirmed IBR signal and derive all exits."""
    strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
    if str(strategy.get("id")) == "rule":
        return _build_rule_setup_plan(decision, symbol_snapshot, cfg)
    direction = decision.get("direction")
    if direction not in {"long", "short"}:
        return None, "setup direction is invalid"
    setup_type = str(decision.get("setup_type") or "ibr_breakout")
    if setup_type not in SETUP_TYPES:
        return None, "setup type is not recognised"
    try:
        contract = contract_for_variant(str(strategy.get("id") or "ibr"), strategy.get("variant_id"))
        if strategy.get("variant_id"):
            validate_contract_config(cfg)
    except (KeyError, ValueError) as exc:
        return None, f"strategy contract mismatch: {exc}"
    rng = _range_for_snapshot(symbol_snapshot, cfg)
    if rng is None or rng.get("complete") is False:
        return None, "IBR range is incomplete"
    entry = _finite(symbol_snapshot.get("entry_price", symbol_snapshot.get("price")))
    if entry is None or entry <= 0:
        return None, "entry price is unavailable"
    signal_ts = _finite(symbol_snapshot.get("signal_ts"))
    if signal_ts is None or signal_ts < 0:
        return None, "completed signal candle timestamp is unavailable"
    if signal_ts > time.time() + 1.0:
        return None, "signal timestamp is in the future"
    end_ts = _finite(rng.get("range_end_ts"))
    if end_ts is not None and signal_ts <= end_ts:
        return None, "IBR signal must use the next completed bar"
    # If the market adapter did not provide a precomputed signal, enforce the
    # same close/buffer/relative-volume rule here from the snapshot fields.
    buffer_bps = _finite(strategy.get("breakout_buffer_bps"), 5.0) or 5.0
    buffer = entry * buffer_bps / 10000.0
    close = _finite(symbol_snapshot.get("close", entry)) or entry
    if direction == "long" and close <= float(rng["high"]) + buffer:
        return None, "IBR close did not break the upper range"
    if direction == "short" and close >= float(rng["low"]) - buffer:
        return None, "IBR close did not break the lower range"
    relative_volume = _finite(symbol_snapshot.get("relative_volume"))
    min_rv = _finite(strategy.get("min_relative_volume"), 1.0) or 1.0
    if relative_volume is None:
        return None, "relative volume is unavailable"
    if relative_volume < min_rv:
        return None, "relative volume is below the IBR threshold"
    if bool(symbol_snapshot.get("halt") or symbol_snapshot.get("halted")):
        return None, "symbol is halted"
    spread = _finite(symbol_snapshot.get("spread_bps"))
    max_spread = _finite(strategy.get("max_spread_bps"), 25.0) or 25.0
    if spread is None:
        return None, "spread is unavailable"
    if spread > max_spread:
        return None, "spread is too wide"
    if symbol_snapshot.get("stale") is not False or symbol_snapshot.get("quote_stale") is not False:
        return None, "market data freshness is unavailable"
    natural_stop = float(rng["low"] if direction == "long" else rng["high"])
    distance = max(abs(entry - natural_stop),
                   entry * MIN_STOP_DISTANCE_FRACTION)
    if distance <= 0:
        return None, "opposite-range stop distance is invalid"
    stop = entry - distance if direction == "long" else entry + distance
    extension_r = max(0.0, (entry - float(rng["high"])) / float(rng["width"]) if direction == "long" else (float(rng["low"]) - entry) / float(rng["width"]))
    extension_limit = _finite(strategy.get("max_entry_extension_r"), float("inf"))
    if extension_limit is not None and extension_r > extension_limit:
        return None, "entry extension exceeds configured limit"
    target_r = _finite(strategy.get("target_r"), 2.0) or 2.0
    target = entry + target_r * distance if direction == "long" else entry - target_r * distance
    if direction == "long" and not (stop < entry < target):
        return None, "long stop/target side validation failed"
    if direction == "short" and not (target < entry < stop):
        return None, "short stop/target side validation failed"
    session = str(symbol_snapshot.get("session") or symbol_snapshot.get("ibr_session") or rng.get("session") or "")
    strategy_id, version = identity(cfg)
    setup_key = _hash({"strategy_id": strategy_id, "strategy_version": version,
                       "variant_id": variant_identity(cfg), "symbol": decision.get("symbol"),
                       "direction": direction, "setup_type": "ibr_breakout", "session": session})
    force_flat_at = (decision.get("force_flat_at") or
                     symbol_snapshot.get("force_flat_at") or
                     _default_force_flat(signal_ts, strategy))
    force_flat_ts = None
    try:
        force_flat_ts = datetime.fromisoformat(str(force_flat_at)).timestamp() if force_flat_at else None
    except (TypeError, ValueError, OverflowError):
        force_flat_ts = None
    plan = dict(decision)
    plan.update({
        "strategy_id": strategy_id, "strategy_version": version,
        "variant_id": variant_identity(cfg), "contract_hash": contract.semantic_hash,
        "setup_type": "ibr_breakout", "setup_key": setup_key,
        "setup_id": _hash({"setup_key": setup_key, "signal_ts": signal_ts}),
        "signal_ts": signal_ts, "session": session,
        "entry_price": entry, "stop_price": stop, "target_price": target,
        "stop_distance": distance, "target_r": target_r,
        "stop_loss_pct": round(distance / entry * 100.0, 8),
        "take_profit_pct": round(abs(target - entry) / entry * 100.0, 8),
        "invalidation_anchor": "range", "exit_policy": "fixed_target_r",
        "execution_choice": str(decision.get("execution_choice") or "normal"),
        "entry_extension_r": extension_r, "range_high": float(rng["high"]),
        "range_low": float(rng["low"]), "range_width": float(rng["width"]),
        "force_flat": True, "force_flat_at": force_flat_at,
        "force_flat_ts": force_flat_ts,
        "force_flat_reason": "regular_session_close", "size_pct_equity": 0.0,
    })
    return plan, None
