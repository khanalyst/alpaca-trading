"""Fade forced deleveraging; stand aside for new positioning.

Mechanism: a liquidation engine sells at market regardless of price. The
trader being liquidated has no discretion - their margin ran out - so the
flow is price-insensitive, mechanically finite, and overshoots. Whoever
supplies liquidity into it is compensated. Open interest is what separates
the two cases that look identical on a price chart: price falling while OI
FALLS is existing longs being closed, price falling while OI RISES is new
shorts being opened.

STATUS: T0_REJECTED. Tested offline on 337 trades across 8 instruments and
60 days of open-interest coverage: -0.288% per trade at base costs and
-0.043% frictionless, so negative before costs rather than cost-killed.
Random entry timing beat it and the inverted variant was also negative,
which rules out a sign error.

It is nevertheless implemented and shadowed on purpose. A strategy measured
negative is the most useful kind of forward control there is: if the shadow
pipeline is working, this one should keep losing, and if it suddenly starts
winning that is evidence about the pipeline rather than about the market.
The register's tier gate keeps it away from capital regardless.
"""

from __future__ import annotations

from . import finite as _finite, register


def setup_evidence(snapshot: dict, cfg: dict) -> dict:
    block = cfg["strategy"]
    atr = max(_finite(snapshot.get("atr_1h_pct"), 0.0) or 0.0, 0.0)
    ema_distance = _finite(snapshot.get("ema20_1h_dist_pct"), 0.0) or 0.0
    long_extension = max(0.0, ema_distance / atr) if atr > 0 else None
    short_extension = max(0.0, -ema_distance / atr) if atr > 0 else None

    # The live snapshot carries a 4h open-interest change from the same
    # per-instrument endpoint the research dataset uses, so this field means
    # the same thing here as it does in the backtest.
    oi_change = _finite(snapshot.get("oi_change_4h_pct"))
    relative_volume = _finite(snapshot.get("relative_volume_1h"))
    # 1h momentum stands in for the 4h move the research contract measured
    # over 16 bars; both are "how far has this run recently", in ATR terms.
    momentum = _finite(snapshot.get("mom_1h_pct"))
    move_atr = (momentum / atr) if (atr > 0 and momentum is not None) else None

    min_move = float(block.get("flush_min_move_atr", 1.5))
    min_drop = float(block.get("flush_min_oi_drop_pct", 1.0))
    min_volume = float(block.get("flush_min_relative_volume", 1.2))

    deleveraging = (
        oi_change is not None and oi_change <= -min_drop
        and relative_volume is not None and relative_volume >= min_volume
    )
    # Fade the direction of the flush: buy the forced selling.
    long_ = (deleveraging and move_atr is not None
             and move_atr <= -min_move)
    short_ = (deleveraging and move_atr is not None
              and move_atr >= min_move)
    return {
        "flush_reversion": {"long": long_, "short": short_},
        "extension_atr": {
            "long": round(long_extension, 2)
            if long_extension is not None else None,
            "short": round(short_extension, 2)
            if short_extension is not None else None,
        },
        "hard_no_chase_atr": float(block["hard_max_entry_extension_atr"]),
        "oi_change_4h_pct": oi_change,
    }


register("flush-fade", setup_evidence)
