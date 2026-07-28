"""Retail positioning, relative to an instrument's own recent range.

Mechanism, as measured during edge discovery: within an instrument, when the
retail long/short account ratio rises relative to its OWN average, that
instrument tends to outperform over the next 16-48 hours. Note the direction
- this is not a naive "fade retail" trade, and the obvious fixed-effect
objection was tested and rejected: a coin's average long/short ratio
correlates -0.432 with its 30-day return, a headwind, and removing the fixed
effect by per-instrument demeaning made the signal STRONGER rather than
weaker.

That is why this contract triggers on the percentile within the instrument's
own recent history rather than on the raw level. The level is a fixed effect;
the deviation is the hypothesis.

Two limitations, both stated before any result rather than after:

- OKX serves this series per CURRENCY, not per instrument, so it aggregates
  across every contract for that coin. The same aggregation is why taker
  volume was rejected outright during edge discovery.
- Retention is roughly 30 days. The published result (+1.114% at 48h,
  t=2.72) rests on about 210 independent observations, and on this data a
  placebo has already reached t=2.60 from destroyed information. It is a
  reason to collect data, not to trade.
"""

from __future__ import annotations

from . import finite as _finite, register


def setup_evidence(snapshot: dict, cfg: dict) -> dict:
    block = cfg["strategy"]
    atr = max(_finite(snapshot.get("atr_1h_pct"), 0.0) or 0.0, 0.0)
    ema_distance = _finite(snapshot.get("ema20_1h_dist_pct"), 0.0) or 0.0
    long_extension = max(0.0, ema_distance / atr) if atr > 0 else None
    short_extension = max(0.0, -ema_distance / atr) if atr > 0 else None

    percentile = _finite(snapshot.get("long_short_percentile_30"))
    ratio = _finite(snapshot.get("long_short_ratio"))
    trend_1h = snapshot.get("trend_1h")

    high = float(block.get("ls_high_percentile", 80.0))
    low = float(block.get("ls_low_percentile", 20.0))

    available = percentile is not None and ratio is not None
    # Ratio ELEVATED versus its own history precedes outperformance, so the
    # elevated case is the long. Refuse to take it into an opposing 1h trend:
    # a 16-48h horizon is long enough for an active downtrend to matter.
    long_ = available and percentile >= high and trend_1h != "down"
    short_ = available and percentile <= low and trend_1h != "up"
    return {
        "positioning_fade": {"long": long_, "short": short_},
        "extension_atr": {
            "long": round(long_extension, 2)
            if long_extension is not None else None,
            "short": round(short_extension, 2)
            if short_extension is not None else None,
        },
        "hard_no_chase_atr": float(block["hard_max_entry_extension_atr"]),
        "long_short_percentile_30": percentile,
    }


register("ls-ratio-fade", setup_evidence)
