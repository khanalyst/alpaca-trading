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
from datetime import date, datetime, time, timedelta
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
# Conservative listed-option broker/exchange fee floor per contract per side.
# Configuration may override this, but a default zero would systematically
# overstate option expectancy relative to equity.
DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE = 0.05
# Mirrors the checked `execution` block; these are caps, never expectations.
RUNTIME_MAX_SPREAD_BPS = 100.0
RUNTIME_MAX_SLIPPAGE_BPS = 50.0

CONFIG_BLOCK = "costs"


class CostError(ValueError):
    """Raised for a malformed or internally inconsistent cost model."""


@dataclass(frozen=True)
class ReplayPolicy:
    """Point-in-time and portfolio limits shared by replay lanes.

    The runtime owns these values.  A research caller can pass the validated
    runtime config (or this value object) through the optional ``policy`` hook;
    omitted policy retains the historical fixture behaviour for compatibility.
    """

    max_market_data_age_seconds: float = 30.0
    options_min_dte: int = 7
    options_max_dte: int = 60
    options_max_spread_pct: float = 10.0
    risk_per_trade_pct: float = 0.5
    latest_entry_time: time | None = None
    force_flat_time: time | None = None
    max_concurrent_positions: int | None = None
    max_position_notional_pct: float | None = None
    max_gross_exposure_pct: float | None = None
    max_open_risk_pct: float | None = None
    daily_loss_limit_pct: float | None = None
    strict_market_data: bool = True

    def __post_init__(self) -> None:
        age = float(self.max_market_data_age_seconds)
        if not math.isfinite(age) or age < 0:
            raise CostError("max_market_data_age_seconds must be finite and non-negative")
        if int(self.options_min_dte) != self.options_min_dte or self.options_min_dte < 0:
            raise CostError("options_min_dte must be a non-negative integer")
        if int(self.options_max_dte) != self.options_max_dte or self.options_max_dte < self.options_min_dte:
            raise CostError("options_max_dte must be an integer >= options_min_dte")
        spread = float(self.options_max_spread_pct)
        if not math.isfinite(spread) or spread < 0:
            raise CostError("options_max_spread_pct must be finite and non-negative")
        for name in ("max_concurrent_positions",):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or int(value) != value or int(value) < 1):
                raise CostError(f"{name} must be a positive integer when supplied")
        for name in ("risk_per_trade_pct", "max_position_notional_pct", "max_gross_exposure_pct",
                     "max_open_risk_pct", "daily_loss_limit_pct"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
                raise CostError(f"{name} must be finite and non-negative when supplied")
        if not isinstance(self.strict_market_data, bool):
            raise CostError("strict_market_data must be true or false")

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_market_data_age_seconds": float(self.max_market_data_age_seconds),
            "options_min_dte": int(self.options_min_dte),
            "options_max_dte": int(self.options_max_dte),
            "options_max_spread_pct": float(self.options_max_spread_pct),
            "risk_per_trade_pct": float(self.risk_per_trade_pct),
            "latest_entry_time": (None if self.latest_entry_time is None else
                                   self.latest_entry_time.isoformat()),
            "force_flat_time": (None if self.force_flat_time is None else
                                 self.force_flat_time.isoformat()),
            "max_concurrent_positions": self.max_concurrent_positions,
            "max_position_notional_pct": self.max_position_notional_pct,
            "max_gross_exposure_pct": self.max_gross_exposure_pct,
            "max_open_risk_pct": self.max_open_risk_pct,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "strict_market_data": self.strict_market_data,
        }

    @classmethod
    def from_config(cls, config: Mapping | None) -> "ReplayPolicy":
        """Read limits from the same validated runtime config blocks."""
        source = dict(config or {})
        execution = source.get("execution") or {}
        risk = source.get("risk") or {}
        strategy = source.get("strategy") or {}
        session = source.get("session") or {}
        if not all(isinstance(block, Mapping) for block in (execution, risk, strategy, session)):
            raise CostError("runtime policy blocks must be mappings")
        latest = strategy.get("latest_entry_time")
        if latest is not None and not isinstance(latest, time):
            try:
                latest = time.fromisoformat(str(latest))
            except ValueError as exc:
                raise CostError("strategy.latest_entry_time must be HH:MM") from exc
        force = strategy.get("force_flat_time")
        if force is None:
            minutes = session.get("force_flat_minutes_before_close")
            # The runtime session close is 16:00 ET; callers that provide only
            # the minute offset get the same force-flat wall clock.
            if minutes is not None:
                try:
                    force = (datetime.combine(date.today(), time(16, 0)) -
                             timedelta(minutes=int(minutes))).time()
                except (TypeError, ValueError):
                    raise CostError("session.force_flat_minutes_before_close must be an integer")
        elif not isinstance(force, time):
            try:
                force = time.fromisoformat(str(force))
            except ValueError as exc:
                raise CostError("force_flat_time must be HH:MM") from exc
        return cls(
            max_market_data_age_seconds=float(execution.get("max_market_data_age_seconds", 30.0)),
            options_min_dte=int(risk.get("options_min_dte", 7)),
            options_max_dte=int(risk.get("options_max_dte", 60)),
            options_max_spread_pct=float(risk.get("options_max_spread_pct", 10.0)),
            risk_per_trade_pct=float(risk.get("risk_per_trade_pct", 0.5)),
            latest_entry_time=latest,
            force_flat_time=force,
            max_concurrent_positions=(None if risk.get("max_concurrent_positions") is None else int(risk["max_concurrent_positions"])),
            max_position_notional_pct=(None if risk.get("max_position_notional_pct") is None else float(risk["max_position_notional_pct"])),
            max_gross_exposure_pct=(None if risk.get("max_gross_exposure_pct") is None else float(risk["max_gross_exposure_pct"])),
            max_open_risk_pct=(None if risk.get("max_total_open_risk_pct",
                                                risk.get("max_open_risk_pct")) is None else
                               float(risk.get("max_total_open_risk_pct",
                                              risk.get("max_open_risk_pct")))),
            daily_loss_limit_pct=(None if risk.get("daily_loss_limit_pct") is None else float(risk["daily_loss_limit_pct"])),
        )


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
    # Optional listed-option fee charged once per contract per side.  Kept
    # after the original fields so positional CostModel(...) callers remain
    # backward-compatible.
    option_fee_per_contract_side: float = DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE
    # Alias accepted for broker schedules that call the per-side amount simply
    # ``option_fee_per_contract``.
    option_fee_per_contract: float | None = None
    provenance: str = "default"

    def __post_init__(self) -> None:
        for name in ("spread_bps", "slippage_bps", "fee_bps",
                     "option_fee_per_contract_side",
                     "max_spread_bps", "max_slippage_bps"):
            object.__setattr__(self, name, _bps(getattr(self, name), name))
        if self.option_fee_per_contract is not None:
            object.__setattr__(self, "option_fee_per_contract",
                               _bps(self.option_fee_per_contract,
                                    "option_fee_per_contract"))
            object.__setattr__(self, "option_fee_per_contract_side",
                               self.option_fee_per_contract)
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise CostError("provenance must be a non-empty string")
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
             multiplier: float = 1.0, *, vehicle: str = "equity") -> float:
        """Both-side notional fees plus optional per-contract option fees."""
        notional = (abs(float(entry_price)) + abs(float(exit_price))) * \
            float(quantity) * float(multiplier)
        total = notional * self.fee_bps / 10_000.0
        if vehicle == "option":
            total += float(quantity) * 2.0 * self.option_fee_per_contract_side
        return total

    def as_dict(self) -> dict:
        return {"spread_bps": self.spread_bps, "slippage_bps": self.slippage_bps,
                "fee_bps": self.fee_bps,
                "option_fee_per_contract_side": self.option_fee_per_contract_side,
                "option_fee_per_contract": self.option_fee_per_contract_side,
                "entry_cost_bps": self.entry_cost_bps,
                "max_spread_bps": self.max_spread_bps,
                "max_slippage_bps": self.max_slippage_bps,
                "provenance": self.provenance}

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
        unknown = sorted(set(block) - {"spread_bps", "slippage_bps", "fee_bps",
                                       "option_fee_per_contract_side",
                                       "option_fee_per_contract", "provenance"})
        if unknown:
            raise CostError(f"{CONFIG_BLOCK} has unknown field(s): {', '.join(unknown)}")
        execution = source.get("execution") or {}
        if not isinstance(execution, Mapping):
            raise CostError("execution must be a mapping")
        return cls(
            spread_bps=block.get("spread_bps", DEFAULT_SPREAD_BPS),
            slippage_bps=block.get("slippage_bps", DEFAULT_SLIPPAGE_BPS),
            fee_bps=block.get("fee_bps", DEFAULT_FEE_BPS),
            option_fee_per_contract_side=block.get(
                "option_fee_per_contract_side", DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE),
            option_fee_per_contract=block.get("option_fee_per_contract"),
            max_spread_bps=execution.get("max_spread_bps", RUNTIME_MAX_SPREAD_BPS),
            max_slippage_bps=execution.get("max_slippage_bps", RUNTIME_MAX_SLIPPAGE_BPS),
            provenance=str(block.get("provenance", "default" if not block else "config")),
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
               at: datetime, side: str, max_age_seconds: float | None = 30.0,
               session_date: date | None = None) -> float | None:
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
        if session_date is not None and getattr(quote, "session_date", None) != session_date:
            continue
        best = quote
    if best is None:
        return None
    age = (at - best.timestamp).total_seconds()
    limit = 30.0 if max_age_seconds is None else float(max_age_seconds)
    if age < 0 or age > limit:
        return None
    price = float(best.ask if side == "buy" else best.bid)
    return price if math.isfinite(price) and price > 0 else None


__all__ = [
    "BAR", "CONFIG_BLOCK", "CostError", "CostModel", "DEFAULT_FEE_BPS",
    "DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE", "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_SPREAD_BPS", "QUOTE",
    "RUNTIME_MAX_SLIPPAGE_BPS", "RUNTIME_MAX_SPREAD_BPS", "ReplayPolicy",
    "index_quotes", "quote_fill",
]
