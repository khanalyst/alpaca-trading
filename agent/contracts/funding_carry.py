"""Funding carry: take the side the crowd is paying to be on the other of.

Mechanism: funding is the price of leverage. When positioning is crowded,
the crowd pays continuously to hold, and the payer is identifiable - the
leveraged long in a persistently positive-funding regime, or the leveraged
short when it is negative. The return source is the carry itself rather than
a directional forecast, which is the reason to prefer it over the momentum
contract: it does not need to be right about direction to be paid.

What it is NOT is the momentum strategy's `funding_squeeze` setup. That one
uses extreme funding as a REVERSAL trigger and holds for hours. This holds
the funding-receiving side for days and collects settlements, so its
horizon is set by how long the crowding persists, not by a price target.

Two guards matter. Funding must be extreme relative to the instrument's own
recent history, not merely nonzero, because a coin's average funding is a
fixed effect rather than a signal. And the position must not be fighting an
aligned trend on both higher timeframes: carry does not pay enough to fund
being run over.
"""

from __future__ import annotations

from . import finite as _finite, register


def setup_evidence(snapshot: dict, cfg: dict) -> dict:
    block = cfg["strategy"]
    atr = max(_finite(snapshot.get("atr_1h_pct"), 0.0) or 0.0, 0.0)
    ema_distance = _finite(snapshot.get("ema20_1h_dist_pct"), 0.0) or 0.0
    long_extension = max(0.0, ema_distance / atr) if atr > 0 else None
    short_extension = max(0.0, -ema_distance / atr) if atr > 0 else None

    funding = _finite(snapshot.get("funding_rate_pct"))
    interval = _finite(snapshot.get("funding_interval_hours"))
    if interval is None or interval <= 0:
        interval = 8.0
    # Normalized so a 4h contract is not held to a bar twice as strict in
    # economic terms as an 8h one.
    funding_8h = funding * (8.0 / interval) if funding is not None else None
    percentile = _finite(snapshot.get("funding_percentile_30"))
    samples = int(max(0.0, _finite(snapshot.get("funding_samples_30"), 0.0)
                      or 0.0))
    basis = _finite(snapshot.get("perp_index_basis_pct"))
    trend_1h = snapshot.get("trend_1h")
    trend_4h = snapshot.get("trend_4h")

    floor = float(block["funding_extreme_pct_per_8h"])
    min_percentile = float(block.get("carry_percentile", 80.0))
    min_samples = int(block.get("carry_min_samples", 20))

    context = (samples >= min_samples and percentile is not None
               and basis is not None and funding_8h is not None)

    # Positive funding: longs pay shorts, so the carry accrues to the short.
    # Refuse when both higher timeframes trend up - the carry is small and a
    # trend that is paying to continue can keep paying for a long time.
    short_ = (
        context
        and funding_8h >= floor
        and percentile >= min_percentile
        and not (trend_1h == "up" and trend_4h == "up")
    )
    long_ = (
        context
        and funding_8h <= -floor
        and percentile <= (100 - min_percentile)
        and not (trend_1h == "down" and trend_4h == "down")
    )
    return {
        "carry": {"long": long_, "short": short_},
        "extension_atr": {
            "long": round(long_extension, 2)
            if long_extension is not None else None,
            "short": round(short_extension, 2)
            if short_extension is not None else None,
        },
        "hard_no_chase_atr": float(block["hard_max_entry_extension_atr"]),
        "funding_8h_equivalent_pct": (
            round(funding_8h, 4) if funding_8h is not None else None),
    }


register("funding-carry", setup_evidence)
