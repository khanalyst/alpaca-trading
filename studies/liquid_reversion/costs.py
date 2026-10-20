"""Round-trip cost of a marketable entry and exit, in bps of entry notional.

Components, summed and then multiplied by ``stress``:

* one full quoted spread, the larger of one tick ($0.01) and a measured spread;
* slippage on each of the two legs;
* the SEC Section 31 fee, on the sell leg only: $20.60 per million of sell
  notional from 4 April 2026 (SEC Fee Rate Advisory 2026-2);
* the FINRA Trading Activity Fee: $0.000195 per share sold, capped at $9.79
  per trade.

Alpaca charges no commission on API equity trades. A round trip has exactly
one sell leg (the exit of a long or the entry of a short), and its notional is
taken to equal the entry notional; the difference is the trade's own return,
a few bps of a 0.2 bps fee. Fees are not rounded up to the cent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TICK = 0.01
SEC_FEE_PER_MILLION = 20.60
FINRA_TAF_PER_SHARE = 0.000195
FINRA_TAF_CAP = 9.79
DEFAULT_NOTIONAL = 25_000.0
DEFAULT_SLIPPAGE_BPS_PER_SIDE = 0.5


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Round-trip components in bps of entry notional, before ``stress``."""

    spread_bps: float
    slippage_bps: float
    sec_fee_bps: float
    taf_bps: float
    stress: float

    @property
    def total_bps(self) -> float:
        return self.stress * (self.spread_bps + self.slippage_bps + self.sec_fee_bps + self.taf_bps)


def tick_bps(price: float) -> float:
    """One tick as bps of ``price``."""
    _positive(price, "price")
    return 1e4 * TICK / price


def sec_fee_usd(sell_notional: float) -> float:
    """Section 31 fee on a sale of ``sell_notional`` dollars."""
    return SEC_FEE_PER_MILLION * sell_notional / 1e6


def taf_usd(shares_sold: float) -> float:
    """FINRA Trading Activity Fee on one sale, with the per-trade cap."""
    return min(FINRA_TAF_PER_SHARE * shares_sold, FINRA_TAF_CAP)


def leg_fees_usd(side: str, price: float, shares: float) -> float:
    """Regulatory fees on one leg in dollars: a purchase pays none."""
    if side == "buy":
        return 0.0
    if side == "sell":
        return sec_fee_usd(price * shares) + taf_usd(shares)
    raise ValueError("side must be 'buy' or 'sell'")


def cost_breakdown(price: float, *, shares: float | None = None, notional: float = DEFAULT_NOTIONAL,
                   spread_bps: float | None = None,
                   slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
                   stress: float = 1.0) -> CostBreakdown:
    """Components of one round trip at ``price``; shares default to ``notional / price``."""
    _positive(price, "price")
    _positive(stress, "stress")
    if shares is None:
        _positive(notional, "notional")
        shares = notional / price
    _positive(shares, "shares")
    if slippage_bps_per_side < 0 or not math.isfinite(slippage_bps_per_side):
        raise ValueError("slippage must be finite and non-negative")
    if spread_bps is not None and (spread_bps < 0 or not math.isfinite(spread_bps)):
        raise ValueError("a measured spread must be finite and non-negative")
    entry_notional = price * shares
    spread = max(tick_bps(price), spread_bps or 0.0)
    # One sell leg per round trip; the buy leg pays no regulatory fee.
    return CostBreakdown(spread_bps=spread, slippage_bps=2.0 * slippage_bps_per_side,
                         sec_fee_bps=1e4 * sec_fee_usd(entry_notional) / entry_notional,
                         taf_bps=1e4 * taf_usd(shares) / entry_notional, stress=stress)


def round_trip_bps(price: float, *, shares: float | None = None, notional: float = DEFAULT_NOTIONAL,
                   spread_bps: float | None = None,
                   slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE,
                   stress: float = 1.0) -> float:
    """Round-trip cost in bps of entry notional for a marketable entry and exit."""
    return cost_breakdown(price, shares=shares, notional=notional, spread_bps=spread_bps,
                          slippage_bps_per_side=slippage_bps_per_side, stress=stress).total_bps


def _positive(value: float, name: str) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
