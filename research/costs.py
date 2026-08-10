"""One executable-cost and fill model for every research lane.

Three lanes used to carry their own spread/slippage/fee numbers and their own
arithmetic, so a change in one silently disagreed with the others and none of
them agreed with the deployed runtime.  This module owns both: the expected
cost parameters and the formulas that spend them.  A lane may choose a
``CostModel``; it may not re-implement one.

Expected cost is not a rejection cap.  ``execution.max_slippage_bps`` is the
worst quoted slippage the runtime will *accept* before refusing to submit;
simulating at that number prices every fill as if it were the worst tolerable
one, and simulating without it prices every fill as if the cap did not exist.
The expected values below are the cost of a normal marketable fill in the
configured liquid US ETF universe; the caps are carried alongside only so a
model that expects a cost the runtime would reject fails closed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any, Iterable, Mapping, Sequence

# A quoted spread of ~2 bps covers a one- to two-cent book on the configured
# ETF universe including its less liquid members, not only the tightest name.
DEFAULT_SPREAD_BPS = 2.0
# A marketable entry and a broker-resident stop leg both execute through the
# book at whatever is resting when they arrive; a triggered stop in a moving
# market pays materially more than half the quoted spread.
DEFAULT_SLIPPAGE_BPS = 3.0
# Regulatory and exchange fees on notional, charged on both sides.
DEFAULT_FEE_BPS = 0.5
# Mirrors the checked `execution` block; these are caps, never expectations.
RUNTIME_MAX_SPREAD_BPS = 100.0
RUNTIME_MAX_SLIPPAGE_BPS = 50.0

CONFIG_BLOCK = "costs"


class CostError(ValueError):
    """Raised for a malformed or internally inconsistent cost model."""


def _bps(value: Any, name: str) -> float:
    # Booleans are not numbers and a numeric string is not a measurement; both
    # are configuration mistakes that must surface rather than be coerced.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CostError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise CostError(f"{name} must be finite and non-negative")
    return number


@dataclass(frozen=True)
class CostModel:
    """Expected per-fill cost, validated against the runtime's rejection caps."""

    spread_bps: float = DEFAULT_SPREAD_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    fee_bps: float = DEFAULT_FEE_BPS
    max_spread_bps: float = RUNTIME_MAX_SPREAD_BPS
    max_slippage_bps: float = RUNTIME_MAX_SLIPPAGE_BPS

    def __post_init__(self) -> None:
        for name in ("spread_bps", "slippage_bps", "fee_bps",
                     "max_spread_bps", "max_slippage_bps"):
            object.__setattr__(self, name, _bps(getattr(self, name), name))
        if self.spread_bps > self.max_spread_bps:
            raise CostError(
                f"expected spread {self.spread_bps} bps exceeds the runtime's "
                f"{self.max_spread_bps} bps rejection cap")
        # The runtime measures slippage against its own reference price and
        # refuses to submit past the cap.  A research model that expects more
        # than that is simulating fills the runtime would never take.
        if self.entry_cost_bps > self.max_slippage_bps:
            raise CostError(
                f"expected entry cost {self.entry_cost_bps} bps exceeds the "
                f"runtime's {self.max_slippage_bps} bps slippage cap")

    @property
    def entry_cost_bps(self) -> float:
        """Half the quoted spread plus adverse slippage, in basis points."""
        return self.spread_bps / 2.0 + self.slippage_bps

    def per_side_bps(self, *, executable_quote: bool = False) -> float:
        """Cost of one execution; an executable quote already includes spread."""
        return self.slippage_bps if executable_quote else self.entry_cost_bps

    def execution_price(self, reference: float, direction: str, *, entry: bool,
                        executable_quote: bool = False) -> float:
        """Move an execution reference adversely by one side's cost."""
        # Long buys at the ask and sells at the bid; short mirrors it.  An
        # option ``ask``/``bid`` or an equity quote is already executable, so
        # charging a modelled half-spread on top would bill the spread twice.
        sign = 1.0 if ((direction == "long") == entry) else -1.0
        rate = self.per_side_bps(executable_quote=executable_quote) / 10_000.0
        return float(reference) * (1.0 + sign * rate)

    def fees(self, entry_price: float, exit_price: float, quantity: float,
             multiplier: float = 1.0) -> float:
        """Both-side fees on the traded notional."""
        notional = (abs(float(entry_price)) + abs(float(exit_price))) * \
            float(quantity) * float(multiplier)
        return notional * self.fee_bps / 10_000.0

    def as_dict(self) -> dict:
        return {"spread_bps": self.spread_bps, "slippage_bps": self.slippage_bps,
                "fee_bps": self.fee_bps, "entry_cost_bps": self.entry_cost_bps,
                "max_spread_bps": self.max_spread_bps,
                "max_slippage_bps": self.max_slippage_bps}

    @classmethod
    def from_config(cls, config: Mapping | None) -> CostModel:
        """Build from the single ``costs`` block, capped by ``execution``.

        The caps are read from the same ``execution`` block the trader
        validates, so tightening the runtime's tolerance is immediately a
        research constraint rather than a number somebody remembers to copy.
        """
        source = dict(config or {})
        block = source.get(CONFIG_BLOCK) or {}
        if not isinstance(block, Mapping):
            raise CostError(f"{CONFIG_BLOCK} must be a mapping")
        unknown = sorted(set(block) - {"spread_bps", "slippage_bps", "fee_bps"})
        if unknown:
            raise CostError(f"{CONFIG_BLOCK} has unknown field(s): {', '.join(unknown)}")
        execution = source.get("execution") or {}
        if not isinstance(execution, Mapping):
            raise CostError("execution must be a mapping")
        return cls(
            spread_bps=block.get("spread_bps", DEFAULT_SPREAD_BPS),
            slippage_bps=block.get("slippage_bps", DEFAULT_SLIPPAGE_BPS),
            fee_bps=block.get("fee_bps", DEFAULT_FEE_BPS),
            max_spread_bps=execution.get("max_spread_bps", RUNTIME_MAX_SPREAD_BPS),
            max_slippage_bps=execution.get("max_slippage_bps", RUNTIME_MAX_SLIPPAGE_BPS),
        )


BAR = "bar"
QUOTE = "quote"


def index_quotes(quotes: Iterable[Any] | None) -> dict[str, list]:
    """Group quote snapshots by symbol in chronological order."""
    grouped: dict[str, list] = {}
    for quote in quotes or ():
        grouped.setdefault(str(quote.symbol).upper(), []).append(quote)
    for rows in grouped.values():
        rows.sort(key=lambda item: item.timestamp)
    return grouped


def quote_fill(indexed: Mapping[str, Sequence[Any]] | None, *, symbol: str,
               at: datetime, side: str) -> float | None:
    """Return the executable side of the last quote visible at a fill instant.

    ``None`` means no quote was recorded for that instant; the caller must
    fall back to the bar and say so rather than inventing a price.
    """
    if not indexed:
        return None
    rows = indexed.get(str(symbol).upper())
    if not rows:
        return None
    best = None
    for quote in rows:
        if quote.timestamp > at:
            break
        identity = getattr(quote, "identity", None)
        if identity is None or identity.as_of > at:
            continue
        best = quote
    if best is None:
        return None
    price = float(best.ask if side == "buy" else best.bid)
    return price if math.isfinite(price) and price > 0 else None


__all__ = [
    "BAR", "CONFIG_BLOCK", "CostError", "CostModel", "DEFAULT_FEE_BPS",
    "DEFAULT_SLIPPAGE_BPS", "DEFAULT_SPREAD_BPS", "QUOTE",
    "RUNTIME_MAX_SLIPPAGE_BPS", "RUNTIME_MAX_SPREAD_BPS", "index_quotes",
    "quote_fill",
]
