"""Offline Alpaca intraday research tools.

The package accepts normalized market records only and never places orders.
"""

from .ibr import IBRConfig, IBRReplay, IBRResult, IBRTrade, replay_ibr
from .market_data import (
    EventIdentity,
    NormalizationError,
    OptionContract,
    OptionSnapshot,
    QuoteSnapshot,
    UnderlyingBar,
    normalize_option_snapshot,
    normalize_quote,
    normalize_underlying_bar,
)

__all__ = [
    "EventIdentity", "IBRConfig", "IBRReplay", "IBRResult", "IBRTrade",
    "NormalizationError", "OptionContract", "OptionSnapshot", "QuoteSnapshot",
    "UnderlyingBar", "normalize_option_snapshot", "normalize_quote",
    "normalize_underlying_bar", "replay_ibr",
]
