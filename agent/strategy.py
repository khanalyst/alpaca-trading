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
from .contracts.ibr import (IBRConfig, build_ibr_range,
                            evaluate_exit, evaluate_ibr_breakout,
                            setup_evidence as _ibr_evidence)

# Public strategy-level aliases for replay/test clients.
construct_ibr_range = build_ibr_range
build_initial_breakout_range = build_ibr_range
check_ibr_breakout = evaluate_ibr_breakout
from .registry import (baseline_variant_id, contract_for_variant, spec_for,
                       validate_contract_config)

SETUP_TYPES = {"ibr_breakout", "range_breakout"}
INVALIDATION_ANCHORS = {"structure", "range"}
EXIT_POLICIES = {"fixed_rr", "force_flat", "fixed_target_r"}
EXECUTION_CHOICES = {"normal", "retry_smaller"}
SETUP_STATUSES = {"proposed", "risk_rejected", "attempted", "execution_rejected", "opened", "closed"}


def _hash(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()).hexdigest()[:24]


def identity(cfg: Mapping) -> tuple[str, str]:
    strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
    return str(strategy.get("id") or "ibr"), str(strategy.get("version") or "v1")


def variant_identity(cfg: Mapping) -> str:
    strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
    return str(strategy.get("variant_id") or baseline_variant_id(str(strategy.get("id") or "ibr")))


def signal_probe(decision: Mapping, symbol_snapshot: Mapping, cfg: Mapping) -> dict | None:
    ts = _finite(symbol_snapshot.get("signal_ts"))
    symbol = decision.get("symbol")
    if ts is None or ts < 0 or not isinstance(symbol, str) or not symbol:
        return None
    direction = decision.get("direction")
    if direction not in {"long", "short"}:
        return None
    strategy_id, version = identity(cfg)
    setup_type = str(decision.get("setup_type") or "ibr_breakout")
    session = str(symbol_snapshot.get("session") or symbol_snapshot.get("ibr_session") or "")
    key = _hash({"strategy_id": strategy_id, "version": version,
                 "variant_id": variant_identity(cfg), "symbol": symbol,
                 "direction": direction, "setup_type": setup_type, "session": session})
    return {"strategy_id": strategy_id, "strategy_version": version,
            "variant_id": variant_identity(cfg), "setup_id": _hash({"key": key, "signal_ts": ts}),
            "setup_key": key, "setup_type": setup_type, "symbol": symbol,
            "direction": direction, "signal_ts": ts, "session": session}


def setup_evidence(snapshot: Mapping, cfg: Mapping) -> dict:
    return _ibr_evidence(snapshot, cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {})


def enrich_snapshot(snapshot: dict, cfg: Mapping) -> dict:
    snapshot["setup_evidence"] = setup_evidence(snapshot, cfg)
    return snapshot


def evidence_fingerprint(snapshot: Mapping) -> str:
    return _hash({"session": snapshot.get("session") or snapshot.get("ibr_session"),
                 "ibr_high": snapshot.get("ibr_high") or (snapshot.get("ibr_range") or {}).get("high"),
                 "ibr_low": snapshot.get("ibr_low") or (snapshot.get("ibr_range") or {}).get("low"),
                 "relative_volume": snapshot.get("relative_volume"),
                 "signal_ts": snapshot.get("signal_ts")})


def compact_entry_evidence(snapshot: Mapping, market_context: Mapping | None = None) -> dict:
    keys = ("signal_ts", "session", "ibr_session", "ibr_high", "ibr_low",
            "range_width", "relative_volume", "spread_bps", "halted",
            "entry_price", "stop_price", "target_price", "force_flat_at")
    out = {key: snapshot.get(key) for key in keys if key in snapshot}
    if isinstance(snapshot.get("ibr_range"), Mapping):
        out["ibr_range"] = dict(snapshot["ibr_range"])
    out["evidence_fingerprint"] = evidence_fingerprint(snapshot)
    if isinstance(market_context, Mapping):
        out["market_context"] = {key: market_context.get(key) for key in ("session", "timestamp") if key in market_context}
    return out


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


def build_setup_plan(decision: Mapping, symbol_snapshot: Mapping,
                     cfg: Mapping, *, hypothesis_params: Mapping | None = None):
    """Validate one close-confirmed IBR signal and derive all exits."""
    direction = decision.get("direction")
    if direction not in {"long", "short"}:
        return None, "setup direction is invalid"
    setup_type = str(decision.get("setup_type") or "ibr_breakout")
    if setup_type not in SETUP_TYPES:
        return None, "setup type is not recognised"
    strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
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
    if symbol_snapshot.get("stale") is not False and symbol_snapshot.get("quote_stale") is not False:
        return None, "market data freshness is unavailable"
    if symbol_snapshot.get("stale") is True or symbol_snapshot.get("quote_stale") is True:
        return None, "market data is stale"
    stop = float(rng["low"] if direction == "long" else rng["high"])
    distance = abs(entry - stop)
    if distance <= 0:
        return None, "opposite-range stop distance is invalid"
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


def new_setup_record(plan: Mapping, cfg: Mapping, now: float | None = None) -> dict:
    current = float(time.time() if now is None else now)
    strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
    memory = _finite(strategy.get("setup_memory_hours"), 24.0) or 24.0
    return {"strategy_id": str(plan.get("strategy_id") or "ibr"), "strategy_version": str(plan.get("strategy_version") or "v1"),
            "variant_id": str(plan.get("variant_id") or variant_identity(cfg)), "setup_key": str(plan["setup_key"]),
            "setup_type": str(plan.get("setup_type") or "ibr_breakout"), "symbol": str(plan["symbol"]),
            "direction": str(plan["direction"]), "signal_ts": float(plan["signal_ts"]),
            "session": plan.get("session"), "first_seen_at": current, "last_seen_at": current,
            "blocked_until": 0.0, "expires_at": current + memory * 3600, "status": "proposed",
            "entry_evidence_fingerprint": plan.get("entry_evidence_fingerprint"), "outcome": None}


def mark_setup(record: dict, status: str, cfg: Mapping, *, now: float | None = None,
               apply_cooldown: bool = False, realized_pnl_usd: float | None = None) -> None:
    if status not in SETUP_STATUSES:
        raise ValueError(f"invalid setup status: {status}")
    current = float(time.time() if now is None else now); record["status"] = status; record["last_seen_at"] = current
    if status == "closed":
        record["closed_at"] = current
        record["outcome"] = "win" if (realized_pnl_usd or 0) > 0 else "loss" if (realized_pnl_usd or 0) < 0 else "flat"
        if realized_pnl_usd is not None: record["realized_pnl_usd"] = float(realized_pnl_usd)
    if apply_cooldown:
        strategy = cfg.get("strategy", cfg) if isinstance(cfg, Mapping) else {}
        minutes = _finite(strategy.get("setup_cooldown_minutes"), 0.0) or 0.0
        record["blocked_until"] = max(float(record.get("blocked_until") or 0), current + minutes * 60)
    record["expires_at"] = max(float(record.get("expires_at") or 0), current + 24 * 3600, float(record.get("blocked_until") or 0))


def semantic_block(records: Mapping, setup_key: str, now: float | None = None) -> dict | None:
    current = float(time.time() if now is None else now)
    matches = [record for record in records.values() if isinstance(record, Mapping) and record.get("setup_key") == setup_key and float(record.get("blocked_until") or 0) > current]
    return max(matches, key=lambda item: float(item.get("blocked_until") or 0)) if matches else None


def evaluated_signal(records: Mapping, plan: Mapping) -> dict | None:
    ts = _finite(plan.get("signal_ts"), -1)
    return next((record for record in records.values() if isinstance(record, Mapping) and record.get("symbol") == plan.get("symbol") and _finite(record.get("signal_ts"), -2) == ts), None)


def failed_thesis_reentry_reason(records: Mapping, plan: Mapping, cfg: Mapping, now: float | None = None) -> str | None:
    return None


def prune_records(records: Mapping, now: float | None = None) -> dict:
    current = float(time.time() if now is None else now)
    return {key: value for key, value in records.items() if isinstance(value, Mapping) and _finite(value.get("expires_at"), 0) > current}


def recent_setup_view(records: Mapping, now: float | None = None) -> list[dict]:
    current = float(time.time() if now is None else now); rows = []
    for setup_id, record in records.items():
        if not isinstance(record, Mapping) or _finite(record.get("expires_at"), 0) <= current: continue
        rows.append({"setup_id": setup_id, "symbol": record.get("symbol"), "direction": record.get("direction"), "setup_type": record.get("setup_type"), "status": record.get("status"), "signal_ts": record.get("signal_ts"), "outcome": record.get("outcome"), "closed_at": record.get("closed_at"), "retry_after_minutes": round(max(0, _finite(record.get("blocked_until"), 0) - current) / 60, 1)})
    return rows[-20:]
