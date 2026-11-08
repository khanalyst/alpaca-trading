"""Per-symbol round-trip cost in bps, the repository's ``per_symbol_v2`` schedule.

One round trip is charged:

* one full spread: the larger of one tick and the symbol's reference spread
  (the Corwin-Schultz high-low median of September 2026);
* 0.5 bps of slippage on each leg;
* the SEC Section 31 fee and the FINRA trading activity fee on the sell leg.

The tables are copied from ``research/costs.py`` (``REFERENCE_SPREADS_BPS`` and
``REFERENCE_PRICES``) so this package does not import the research tree; a
test pins the copy to the source.  The fee arithmetic is the stdlib-only
``studies.liquid_reversion.costs``.

Auction orders (market-on-open and market-on-close) execute at the auction
price, which is the official open or close, so a full spread per round trip
is conservative for them.  The bars' open and close are not guaranteed to be
the auction prints, which is why the reports also give the break-even cost.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

from studies.liquid_reversion import costs as fee_model

COST_MODEL = "per_symbol_v2"
SLIPPAGE_BPS_PER_SIDE = 0.5
REFERENCE_SPREADS_BPS: Mapping[str, float] = {
    "DIA": 0.744, "EEM": 1.138, "EFA": 0.69, "GLD": 1.694, "HYG": 0.626,
    "IWM": 1.281, "QQQ": 1.294, "SLV": 2.832, "SMH": 2.165, "SPY": 0.971,
    "TLT": 0.796, "VB": 0.54, "VO": 0.833, "VTI": 0.776, "XLB": 1.434,
    "XLE": 2.288, "XLF": 1.551, "XLI": 1.206, "XLK": 1.463, "XLP": 1.179,
    "XLRE": 1.121, "XLU": 1.773, "XLV": 1.274, "XLY": 1.276,
}
REFERENCE_PRICES: Mapping[str, float] = {
    "DIA": 527.7, "EEM": 67.15, "EFA": 106.87, "GLD": 401.79, "HYG": 79.14,
    "IWM": 291.92, "QQQ": 715.35, "SLV": 59.7, "SMH": 556.74, "SPY": 765.15,
    "TLT": 81.86, "VB": 296.05, "VO": 81.45, "VTI": 377.02, "XLB": 52.14,
    "XLE": 64.43, "XLF": 57.4, "XLI": 172.67, "XLK": 185.94, "XLP": 84.53,
    "XLRE": 43.76, "XLU": 42.6, "XLV": 168.91, "XLY": 114.16,
}
# A symbol outside the reference table is charged this conservative spread.
FALLBACK_SPREAD_BPS = 5.0
FALLBACK_PRICE = 50.0


def round_trip_bps(symbol: str, *, stress: float = 1.0) -> float:
    """Expected round-trip cost of ``symbol`` at its reference price, in bps."""
    price = REFERENCE_PRICES.get(symbol, FALLBACK_PRICE)
    spread = REFERENCE_SPREADS_BPS.get(symbol, FALLBACK_SPREAD_BPS)
    return fee_model.round_trip_bps(price, spread_bps=spread,
                                    slippage_bps_per_side=SLIPPAGE_BPS_PER_SIDE, stress=stress)


def one_way_vector(symbols: Sequence[str], *, stress: float = 1.0) -> np.ndarray:
    """Cost of trading one unit of weight one way, as a return fraction per symbol."""
    return np.array([round_trip_bps(s, stress=stress) / 2.0 / 1e4 for s in symbols])


def schedule(symbols: Sequence[str]) -> dict[str, float]:
    return {s: round(round_trip_bps(s), 4) for s in symbols}
