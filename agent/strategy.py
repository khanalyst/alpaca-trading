"""Versioned momentum strategy contracts and setup idempotency.

The model still decides whether a candidate is worth trading and which setup
best explains it. This module owns the parts that must be reproducible:

- broad setup-contract checks (to prevent relabelling arbitrary trades);
- semantic stop/exit-policy conversion into deterministic percentages;
- an extreme no-chase boundary;
- stable setup IDs tied to the completed signal candle.
"""

from __future__ import annotations

import hashlib
import json
import math
import time


SETUP_TYPES = {
    "trend_continuation",
    "range_breakout",
    "funding_squeeze",
    "other",
}
INVALIDATION_ANCHORS = {"structure", "atr"}
EXIT_POLICIES = {"fixed_rr", "extended_rr", "structure_target"}
EXECUTION_CHOICES = {"normal", "retry_smaller"}
SETUP_STATUSES = {
    "proposed", "risk_rejected", "attempted", "execution_rejected",
    "opened", "closed",
}


def _finite(value, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _hash(payload: object) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def identity(cfg: dict) -> tuple[str, str]:
    block = cfg["strategy"]
    return str(block["id"]), str(block["version"])


def signal_probe(decision: dict, symbol_snapshot: dict,
                 cfg: dict) -> dict | None:
    """Build an idempotency identity before the full setup contract runs."""
    signal_ts = _finite(symbol_snapshot.get("signal_ts"))
    symbol = decision.get("symbol")
    direction = decision.get("direction")
    if (signal_ts is None or signal_ts < 0 or not isinstance(symbol, str)
            or not symbol or direction not in {"long", "short"}):
        return None
    strategy_id, strategy_version = identity(cfg)
    setup_type = str(decision.get("setup_type") or "invalid")[:80]
    setup_key = _hash({
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": symbol,
        "direction": direction,
        "setup_type": setup_type,
    })
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        # Rejected evaluations use a per-symbol/per-bar ID. Valid setup IDs
        # remain more specific and include their semantic setup key.
        "setup_id": _hash({
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "symbol": symbol,
            "signal_ts": signal_ts,
        }),
        "setup_key": setup_key,
        "setup_type": setup_type,
        "symbol": symbol,
        "direction": direction,
        "signal_ts": signal_ts,
    }


def setup_evidence(snapshot: dict, cfg: dict) -> dict:
    """Return compact, broad evidence for each recognised setup archetype."""
    block = cfg["strategy"]
    atr = max(_finite(snapshot.get("atr_1h_pct"), 0.0) or 0.0, 0.0)
    ema_distance = _finite(snapshot.get("ema20_1h_dist_pct"), 0.0) or 0.0
    long_extension = max(0.0, ema_distance / atr) if atr > 0 else None
    short_extension = max(0.0, -ema_distance / atr) if atr > 0 else None

    trends = [
        snapshot.get("trend_15m"),
        snapshot.get("trend_1h"),
        snapshot.get("trend_4h"),
    ]
    up = sum(value == "up" for value in trends)
    down = sum(value == "down" for value in trends)
    range_position = _finite(snapshot.get("range_pos_pct"))
    relative_volume = _finite(snapshot.get("relative_volume_1h"))
    momentum = _finite(snapshot.get("mom_1h_pct"), 0.0) or 0.0
    funding = _finite(snapshot.get("funding_rate_pct"))
    range_threshold = float(block["breakout_range_threshold_pct"])
    min_relative_volume = float(block["breakout_min_relative_volume"])
    funding_extreme = float(block["funding_extreme_pct"])

    breakout_long = (
        range_position is not None
        and relative_volume is not None
        and range_position >= range_threshold
        and relative_volume >= min_relative_volume
        and momentum > 0
    )
    breakout_short = (
        range_position is not None
        and relative_volume is not None
        and range_position <= 100 - range_threshold
        and relative_volume >= min_relative_volume
        and momentum < 0
    )
    return {
        "trend_continuation": {"long": up >= 2, "short": down >= 2},
        "range_breakout": {
            "long": breakout_long,
            "short": breakout_short,
        },
        "funding_squeeze": {
            "long": funding is not None and funding <= -funding_extreme,
            "short": funding is not None and funding >= funding_extreme,
        },
        "extension_atr": {
            "long": round(long_extension, 2)
            if long_extension is not None else None,
            "short": round(short_extension, 2)
            if short_extension is not None else None,
        },
        "hard_no_chase_atr": float(block["hard_max_entry_extension_atr"]),
    }


def enrich_snapshot(snapshot: dict, cfg: dict) -> dict:
    snapshot["setup_evidence"] = setup_evidence(snapshot, cfg)
    return snapshot


def build_setup_plan(decision: dict, symbol_snapshot: dict,
                     cfg: dict) -> tuple[dict | None, str | None]:
    """Validate one model-labelled setup and derive deterministic SL/TP."""
    direction = decision.get("direction")
    if direction not in {"long", "short"}:
        return None, "setup direction is invalid"
    setup_type = str(decision.get("setup_type") or "")
    if setup_type not in SETUP_TYPES:
        return None, "setup type is not recognised"
    if setup_type == "other" and (
            cfg["mode"] != "demo"
            or not cfg["strategy"]["allow_experimental_setups_in_demo"]):
        return None, "experimental setups are allowed only in demo mode"

    anchor = str(decision.get("invalidation_anchor") or "")
    if anchor not in INVALIDATION_ANCHORS:
        return None, "invalidation anchor is not recognised"
    if setup_type in {"trend_continuation", "range_breakout"} \
            and anchor != "structure":
        return None, f"{setup_type} requires a structure invalidation"

    exit_policy = str(decision.get("exit_policy") or "")
    if exit_policy not in EXIT_POLICIES:
        return None, "exit policy is not recognised"
    execution_choice = str(decision.get("execution_choice") or "normal")
    if execution_choice not in EXECUTION_CHOICES:
        return None, "execution choice is not recognised"

    evidence = symbol_snapshot.get("setup_evidence")
    if not isinstance(evidence, dict):
        evidence = setup_evidence(symbol_snapshot, cfg)
    contract = evidence.get(setup_type)
    if setup_type != "other" and (
            not isinstance(contract, dict) or contract.get(direction) is not True):
        return None, f"{setup_type} evidence contract is not met"

    extensions = evidence.get("extension_atr") or {}
    extension = _finite(extensions.get(direction))
    hard_limit = float(cfg["strategy"]["hard_max_entry_extension_atr"])
    if extension is None:
        return None, "entry extension cannot be measured"
    if extension > hard_limit:
        return None, (
            f"entry is {extension:.2f} ATR from the 1h EMA20; "
            f"hard no-chase limit is {hard_limit:.2f} ATR")

    signal_ts = _finite(symbol_snapshot.get("signal_ts"))
    atr = _finite(symbol_snapshot.get("atr_1h_pct"))
    if signal_ts is None or signal_ts < 0:
        return None, "completed signal candle timestamp is unavailable"
    if atr is None or atr <= 0:
        return None, "ATR is unavailable for deterministic stop placement"

    block = cfg["strategy"]
    minimum_stop = atr * float(block["min_stop_atr_multiple"])
    structure_field = (
        "swing_low_pct" if direction == "long" else "swing_high_pct")
    structure_distance = _finite(symbol_snapshot.get(structure_field))
    if anchor == "structure":
        if structure_distance is None or structure_distance < 0:
            return None, "structure invalidation distance is unavailable"
        stop_pct = max(
            minimum_stop,
            structure_distance
            + atr * float(block["structure_buffer_atr_multiple"]),
        )
    else:
        stop_pct = minimum_stop

    fixed_rr = float(block["fixed_reward_risk"])
    if exit_policy == "extended_rr":
        take_pct = stop_pct * float(block["extended_reward_risk"])
    elif exit_policy == "structure_target":
        target_field = (
            "swing_high_pct" if direction == "long" else "swing_low_pct")
        structure_target = _finite(symbol_snapshot.get(target_field), 0.0) or 0.0
        take_pct = max(stop_pct * fixed_rr, structure_target)
    else:
        take_pct = stop_pct * fixed_rr

    strategy_id, strategy_version = identity(cfg)
    setup_key = _hash({
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": decision.get("symbol"),
        "direction": direction,
        "setup_type": setup_type,
    })
    setup_id = _hash({
        "setup_key": setup_key,
        "signal_ts": signal_ts,
    })
    enriched = dict(decision)
    enriched.update({
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "setup_id": setup_id,
        "setup_key": setup_key,
        "setup_type": setup_type,
        "signal_ts": signal_ts,
        "invalidation_anchor": anchor,
        "exit_policy": exit_policy,
        "execution_choice": execution_choice,
        "stop_loss_pct": round(stop_pct, 6),
        "take_profit_pct": round(take_pct, 6),
        # Risk and leverage are deterministic. Explicitly discard any numeric
        # authority a malformed or legacy model response attempted to include.
        "size_pct_equity": 0.0,
        "leverage": int(cfg["risk"]["entry_leverage"]),
    })
    return enriched, None


def new_setup_record(plan: dict, cfg: dict,
                     now: float | None = None) -> dict:
    current = float(now if now is not None else time.time())
    return {
        "strategy_id": str(plan["strategy_id"]),
        "strategy_version": str(plan["strategy_version"]),
        "setup_key": str(plan["setup_key"]),
        "setup_type": str(plan["setup_type"]),
        "symbol": str(plan["symbol"]),
        "direction": str(plan["direction"]),
        "signal_ts": float(plan["signal_ts"]),
        "first_seen_at": current,
        "last_seen_at": current,
        "blocked_until": 0.0,
        "expires_at": (
            current + float(cfg["strategy"]["setup_memory_hours"]) * 3600),
        "status": "proposed",
    }


def mark_setup(record: dict, status: str, cfg: dict,
               *, now: float | None = None,
               apply_cooldown: bool = False) -> None:
    if status not in SETUP_STATUSES:
        raise ValueError(f"invalid setup status: {status}")
    current = float(now if now is not None else time.time())
    record["status"] = status
    record["last_seen_at"] = current
    if apply_cooldown:
        record["blocked_until"] = max(
            float(record.get("blocked_until") or 0),
            current + float(cfg["strategy"]["setup_cooldown_minutes"]) * 60,
        )
    record["expires_at"] = max(
        float(record.get("expires_at") or 0),
        current + float(cfg["strategy"]["setup_memory_hours"]) * 3600,
        float(record.get("blocked_until") or 0),
    )


def semantic_block(records: dict, setup_key: str,
                   now: float | None = None) -> dict | None:
    current = float(now if now is not None else time.time())
    matches = [
        record for record in records.values()
        if isinstance(record, dict)
        and record.get("setup_key") == setup_key
        and float(record.get("blocked_until") or 0) > current
    ]
    return max(matches, key=lambda item: float(item["blocked_until"])) \
        if matches else None


def evaluated_signal(records: dict, plan: dict) -> dict | None:
    """Find an earlier evaluation of this symbol's completed signal bar.

    A model cannot evade per-candle idempotency by changing its setup label or
    flipping direction between cycles. A newly completed signal candle remains
    a fresh opportunity for the model to reason about.
    """
    signal_ts = float(plan["signal_ts"])
    for record in records.values():
        if not isinstance(record, dict):
            continue
        if (
            record.get("strategy_id") == plan.get("strategy_id")
            and record.get("strategy_version") == plan.get("strategy_version")
            and record.get("symbol") == plan.get("symbol")
            and float(record.get("signal_ts") or -1) == signal_ts
        ):
            return record
    return None


def prune_records(records: dict, now: float | None = None) -> dict:
    current = float(now if now is not None else time.time())
    return {
        setup_id: record
        for setup_id, record in records.items()
        if isinstance(record, dict)
        and float(record.get("expires_at") or 0) > current
    }


def recent_setup_view(records: dict, now: float | None = None) -> list[dict]:
    current = float(now if now is not None else time.time())
    rows = []
    for setup_id, record in records.items():
        if not isinstance(record, dict):
            continue
        expires_at = float(record.get("expires_at") or 0)
        if expires_at <= current:
            continue
        blocked_until = float(record.get("blocked_until") or 0)
        rows.append({
            "setup_id": setup_id,
            "symbol": record.get("symbol"),
            "direction": record.get("direction"),
            "setup_type": record.get("setup_type"),
            "status": record.get("status"),
            "signal_ts": record.get("signal_ts"),
            "retry_after_minutes": round(
                max(0.0, blocked_until - current) / 60, 1),
            "_last_seen_at": float(record.get("last_seen_at") or 0),
        })
    rows.sort(key=lambda row: row["_last_seen_at"])
    for row in rows:
        row.pop("_last_seen_at", None)
    return rows[-20:]
