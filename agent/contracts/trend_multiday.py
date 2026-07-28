"""Multi-day trend continuation.

Mechanism: slow adoption flows and reflexive positioning make crypto trend
persist at horizons measured in days rather than minutes. The payer is the
mean-reversion seller who is early. The reason to prefer this timescale over
the 15m one is arithmetic, not taste: round-trip cost is roughly 15% of a
typical intraday move and roughly 1% of a multi-day one, so the same edge
survives at one horizon and cannot at the other.

Deliberately close to the momentum contract in structure, and deliberately
different in what it asks for: alignment that has persisted, not an impulse
that has just fired. The momentum contract's measured failure was buying the
local exhaustion of a completed 15m move; this one requires the 4h and 1h
trend to agree and asks nothing of the 15m bar at all.
"""

from __future__ import annotations

from . import finite as _finite, register


def setup_evidence(snapshot: dict, cfg: dict) -> dict:
    block = cfg["strategy"]
    atr = max(_finite(snapshot.get("atr_1h_pct"), 0.0) or 0.0, 0.0)
    ema_distance = _finite(snapshot.get("ema20_1h_dist_pct"), 0.0) or 0.0
    long_extension = max(0.0, ema_distance / atr) if atr > 0 else None
    short_extension = max(0.0, -ema_distance / atr) if atr > 0 else None

    trend_1h = snapshot.get("trend_1h")
    trend_4h = snapshot.get("trend_4h")
    momentum = _finite(snapshot.get("mom_1h_pct"), 0.0) or 0.0
    range_position = _finite(snapshot.get("range_pos_pct"))
    atr_ratio = _finite(snapshot.get("atr_1h_ratio"))

    min_range = float(block.get("trend_min_range_pos_pct", 55.0))
    max_atr_ratio = float(block.get("trend_max_atr_ratio", 2.0))

    # A volatility ceiling rather than a floor. Entering a multi-day hold
    # during a volatility spike means the stop, which is sized in ATR, is
    # widest exactly when the move is most likely to be a liquidation event
    # rather than a trend.
    calm_enough = atr_ratio is not None and atr_ratio <= max_atr_ratio

    long_ = (
        trend_1h == "up" and trend_4h == "up"
        and momentum > 0
        and calm_enough
        and range_position is not None and range_position >= min_range
    )
    short_ = (
        trend_1h == "down" and trend_4h == "down"
        and momentum < 0
        and calm_enough
        and range_position is not None
        and range_position <= (100 - min_range)
    )
    return {
        "trend_follow": {"long": long_, "short": short_},
        "extension_atr": {
            "long": round(long_extension, 2)
            if long_extension is not None else None,
            "short": round(short_extension, 2)
            if short_extension is not None else None,
        },
        "hard_no_chase_atr": float(block["hard_max_entry_extension_atr"]),
    }


register("trend-multiday", setup_evidence)
