"""Causal intraday context derived from the same completed minute snapshot.

This is a price-path regime hypothesis, not a news, daily trend, or market-wide
regime classifier. No bars are synthesized across a missing observation.
"""
from __future__ import annotations

from datetime import datetime, time
import hashlib
import json
import math
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

FEATURE_VERSION = "intraday-context.v1"


def completed_context(rows: Sequence[Mapping], *, minutes: int,
                      lookback: int) -> tuple[dict | None, str]:
    """Use exactly N whole, adjacent regular-session buckets ending by as-of.

    Input timestamps name the start of a completed one-minute bar. The final
    minute's end is the information cutoff. Incomplete higher-timeframe bars
    never enter the feature or its content digest.
    """
    if not rows:
        return None, "context_unavailable"
    cutoff = float(rows[-1]["timestamp"]) + 60.0
    zone = ZoneInfo("America/New_York")
    local = datetime.fromtimestamp(cutoff - 60, zone)
    opened = datetime.combine(local.date(), time(9, 30), tzinfo=zone).timestamp()
    width = minutes * 60
    end = opened + math.floor((cutoff - opened) / width) * width
    start = end - lookback * width
    if start < opened or end > opened + 390 * 60:
        return None, "context_window_incomplete"
    selected = [dict(row) for row in rows
                if start <= float(row["timestamp"]) < end]
    expected = [start + i * 60 for i in range(lookback * minutes)]
    if [float(row["timestamp"]) for row in selected] != expected:
        return None, "context_window_not_contiguous"
    if any(not math.isfinite(float(row[key])) or
           (float(row[key]) <= 0 if key != "volume" else float(row[key]) < 0)
           for row in selected for key in ("open", "high", "low", "close", "volume")):
        return None, "context_values_invalid"
    buckets = []
    for offset in range(0, len(selected), minutes):
        group = selected[offset:offset + minutes]
        buckets.append({"timestamp": group[0]["timestamp"],
                        "open": group[0]["open"],
                        "high": max(row["high"] for row in group),
                        "low": min(row["low"] for row in group),
                        "close": group[-1]["close"],
                        "volume": sum(row["volume"] for row in group)})
    prices = [float(buckets[0]["open"]),
              *(float(row["close"]) for row in buckets)]
    moves = [b - a for a, b in zip(prices, prices[1:])]
    path = sum(abs(move) for move in moves)
    net = prices[-1] - prices[0]
    payload = {"feature_version": FEATURE_VERSION, "minutes": minutes,
               "lookback": lookback, "start_ts": start, "end_ts": end,
               "source_minutes": selected}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return {"feature_version": FEATURE_VERSION, "snapshot_id": digest,
            "timeframe_minutes": minutes, "lookback_bars": lookback,
            "start_ts": start, "end_ts": end, "asof_ts": cutoff,
            "efficiency": abs(net) / path if path else 0.0,
            "direction": "long" if net > 0 else "short" if net < 0 else "flat",
            "return_bps": net / prices[0] * 10_000, "bars": buckets}, "passed"
