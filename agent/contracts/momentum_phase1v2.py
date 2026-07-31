"""The momentum phase1-v2 evidence contract.

Moved verbatim out of ``agent.strategy`` when the strategy register landed.
The logic is unchanged and must stay that way: ``research/edge_lab.py``
carries an independently written vectorized copy, and
``research/validate_features.py`` asserts the two agree bar for bar. Any edit
here that is not mirrored there turns every backtest into a measurement of a
strategy the agent does not run.

Status: T0_REJECTED. Across 115,929 signals this contract showed a 45.6-47.3%
directional hit rate at every horizon from 15 minutes to 24 hours. It is
retained as the benchmark null that new strategies are scored against, and it
is blocked from live capital by the tier gate in agent/config.py.
"""

from __future__ import annotations

from . import finite as _finite, register


def setup_evidence(snapshot: dict, cfg: dict) -> dict:
    """Return auditable minimum evidence for each recognised setup archetype."""
    block = cfg["strategy"]
    atr = max(_finite(snapshot.get("atr_1h_pct"), 0.0) or 0.0, 0.0)
    ema_distance = _finite(snapshot.get("ema20_1h_dist_pct"), 0.0) or 0.0
    long_extension = max(0.0, ema_distance / atr) if atr > 0 else None
    short_extension = max(0.0, -ema_distance / atr) if atr > 0 else None

    trend_15m = snapshot.get("trend_15m")
    trend_1h = snapshot.get("trend_1h")
    trend_4h = snapshot.get("trend_4h")
    range_position = _finite(snapshot.get("range_pos_pct"))
    relative_volume = _finite(snapshot.get("relative_volume_1h"))
    momentum = _finite(snapshot.get("mom_1h_pct"), 0.0) or 0.0
    fast_momentum = _finite(snapshot.get("mom_15m_pct"), 0.0) or 0.0
    funding = _finite(snapshot.get("funding_rate_pct"))
    # Funding intervals differ per instrument (OKX runs 8h and 4h contracts
    # side by side), so a raw per-interval rate is not comparable across
    # symbols: a 4h contract charges the same rate twice as often. Normalize
    # to an 8h equivalent before applying an absolute threshold. An unknown
    # or nonsensical interval falls back to treating the rate as already 8h.
    funding_interval = _finite(snapshot.get("funding_interval_hours"))
    if funding_interval is None or funding_interval <= 0:
        funding_interval = 8.0
    funding_8h = (
        funding * (8.0 / funding_interval) if funding is not None else None)
    funding_percentile = _finite(snapshot.get("funding_percentile_30"))
    funding_samples = int(
        max(0.0, _finite(snapshot.get("funding_samples_30"), 0.0) or 0.0))
    basis = _finite(snapshot.get("perp_index_basis_pct"))
    open_interest = _finite(snapshot.get("open_interest_musd"))
    range_threshold = float(block["breakout_range_threshold_pct"])
    min_relative_volume = float(block["breakout_min_relative_volume"])
    funding_extreme = float(block["funding_extreme_pct_per_8h"])

    # --- batch 6.4: which variable separates a breakout from a continuation
    #
    # The two contracts overlap: a symbol in a strong aligned uptrend is
    # almost always in the top of its 24h range with elevated volume, so both
    # fire and which label a trade receives depends on which word the model
    # chose rather than on a difference in the market. Attributing
    # performance by setup_type then splits one phenomenon across two rows.
    #
    # What it should be separated BY is an open question, and the plan and
    # the edge hypotheses disagree:
    #
    #   trend_alignment    6.4 as originally specified. A breakout is a
    #                      transition OUT of chop, so it requires the absence
    #                      of prior multi-timeframe alignment.
    #   volatility_regime  The real partition is compression versus
    #                      expansion, and trend alignment is a correlated
    #                      proxy for it. A breakout from a compressed base is
    #                      a real event; a "break" when volatility is already
    #                      elevated is an ordinary excursion in a wide
    #                      distribution.
    #   none               Neither. The shipped behaviour, kept as the
    #                      default so this batch changes nothing until a
    #                      discriminator is chosen deliberately.
    #
    # Making it configurable is what lets the corpus decide instead of the
    # argument. Baking one in and then testing the other would filter the
    # population on a correlated variable first, which is precisely what
    # makes the regime test impossible to run cleanly afterwards.
    discriminator = str(block.get("breakout_discriminator") or "none")
    compression_max = float(
        block.get("breakout_compression_max_atr_ratio") or 1.0)
    atr_ratio = _finite(snapshot.get("atr_1h_ratio"))

    aligned_long = trend_1h == "up" and trend_4h == "up"
    aligned_short = trend_1h == "down" and trend_4h == "down"
    if discriminator == "trend_alignment":
        breakout_allowed_long = not aligned_long
        breakout_allowed_short = not aligned_short
    elif discriminator == "volatility_regime":
        # Compressed base. Unknown volatility does not qualify: a missing
        # measurement must not read as "compressed".
        compressed = atr_ratio is not None and atr_ratio <= compression_max
        breakout_allowed_long = compressed
        breakout_allowed_short = compressed
    else:
        breakout_allowed_long = True
        breakout_allowed_short = True

    breakout_long = (
        breakout_allowed_long
        and range_position is not None
        and relative_volume is not None
        and range_position >= range_threshold
        and relative_volume >= min_relative_volume
        and momentum > 0
        and fast_momentum > 0
        and snapshot.get("fresh_breakout_long") is True
        and trend_1h != "down"
    )
    breakout_short = (
        breakout_allowed_short
        and range_position is not None
        and relative_volume is not None
        and range_position <= 100 - range_threshold
        and relative_volume >= min_relative_volume
        and momentum < 0
        and fast_momentum < 0
        and snapshot.get("fresh_breakout_short") is True
        and trend_1h != "up"
    )
    continuation_long = (
        trend_1h == "up"
        and trend_4h == "up"
        and trend_15m != "down"
        and momentum > 0
        and fast_momentum > 0
    )
    continuation_short = (
        trend_1h == "down"
        and trend_4h == "down"
        and trend_15m != "up"
        and momentum < 0
        and fast_momentum < 0
    )
    squeeze_context_available = (
        funding_samples >= 10
        and funding_percentile is not None
        and basis is not None
        and open_interest is not None
        and open_interest > 0
    )
    squeeze_long = (
        funding_8h is not None
        and funding_8h <= -funding_extreme
        and squeeze_context_available
        and funding_percentile <= 25
        and basis <= 0
        and trend_1h != "down"
        and snapshot.get("price_stabilized_long") is True
        and fast_momentum > 0
    )
    squeeze_short = (
        funding_8h is not None
        and funding_8h >= funding_extreme
        and squeeze_context_available
        and funding_percentile >= 75
        and basis >= 0
        and trend_1h != "up"
        and snapshot.get("price_stabilized_short") is True
        and fast_momentum < 0
    )
    return {
        "trend_continuation": {
            "long": continuation_long,
            "short": continuation_short,
        },
        "range_breakout": {
            "long": breakout_long,
            "short": breakout_short,
        },
        "funding_squeeze": {
            "long": squeeze_long,
            "short": squeeze_short,
        },
        "extension_atr": {
            "long": round(long_extension, 2)
            if long_extension is not None else None,
            "short": round(short_extension, 2)
            if short_extension is not None else None,
        },
        "hard_no_chase_atr": float(block["hard_max_entry_extension_atr"]),
        # Surfaced so a squeeze decision can be audited against the value the
        # contract actually compared, not the raw per-interval rate.
        "funding_8h_equivalent_pct": (
            round(funding_8h, 4) if funding_8h is not None else None),
        "funding_extreme_threshold_per_8h": funding_extreme,
    }


register("momentum", setup_evidence)
