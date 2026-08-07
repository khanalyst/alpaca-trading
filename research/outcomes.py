"""What actually happened next, with a hard no-lookahead guarantee.

Given a setup plan - entry, stop, target, direction, signal timestamp - this
decides what the trade would have done, using only price data that existed
strictly after the decision was made.

Three rules here are not configurable, and each exists because the
convenient alternative manufactures edge out of nothing:

**No lookahead, enforced by raising.** The resolver may only read bars whose
open is at or after both ``signal_ts + signal_bar_duration`` and the actual
decision/entry timestamp. Reading the signal bar itself would let the outcome
peek at the candle the decision was made from; reading the minutes between a
bar close and a later decision would credit movement that happened before the
trade existed. This is checked in code rather than trusted, because a
lookahead bug produces numbers that are precise, plausible, internally
consistent and wrong - with no error and no failing test.

**Same-bar ties resolve as a stop.** When one 1-minute bar's range spans both
the stop and the target, the order of the two touches is genuinely unknown.
Resolving the ambiguity favourably is the single easiest way to invent a
strategy that works. So it resolves against the trade, always, and this is
never made configurable: an option to be optimistic is an option somebody
eventually takes.

**Costs are modelled symmetrically and match the live engine.** The same
two-sided taker fee, the same recorded spread, the same stop-slippage
reservation, and funding applied per interval held and direction-aware. A
replay that models zero costs is not comparable to a demo record that pays
them, and the comparison is the entire job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


TIMEFRAME_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
}


class LookaheadError(AssertionError):
    """Raised when the resolver is handed a bar it must not be able to see."""


@dataclass(frozen=True)
class SetupPlan:
    symbol: str
    direction: str                 # "long" | "short"
    entry_price: float
    stop_pct: float                # distance from entry, positive percent
    take_pct: float                # distance from entry, positive percent
    signal_ts: int                 # epoch ms, the signal bar's OPEN time
    entry_ts: int | None = None    # epoch ms, actual decision/entry time
    signal_timeframe: str = "15m"
    spread_pct: float = 0.0
    funding_rate_pct: float = 0.0
    funding_interval_hours: float = 8.0
    # The 0.25% default is the taker fee per side; CostModel charges both sides
    # of a round trip. Callers may override it with a per-symbol rate.
    taker_fee_pct_per_side: float = 0.25

    @property
    def first_visible_ts(self) -> int:
        """The earliest bar the resolver is permitted to read."""
        signal_close = int(self.signal_ts) + TIMEFRAME_MS[self.signal_timeframe]
        return max(signal_close, int(self.entry_ts or signal_close))

    def stop_price(self) -> float:
        move = self.entry_price * self.stop_pct / 100.0
        return (self.entry_price - move if self.direction == "long"
                else self.entry_price + move)

    def take_price(self) -> float:
        move = self.entry_price * self.take_pct / 100.0
        return (self.entry_price + move if self.direction == "long"
                else self.entry_price - move)


@dataclass
class Outcome:
    result: str                    # target | stop | timeout | no_data
    exit_ts: int | None = None
    exit_price: float | None = None
    bars_held: int = 0
    mae_pct: float = 0.0           # maximum adverse excursion, positive
    mfe_pct: float = 0.0           # maximum favourable excursion, positive
    gross_pct: float = 0.0
    cost_pct: float = 0.0
    net_pct: float = 0.0
    r_multiple: float = 0.0
    risk_pct: float = 0.0
    tie_broken: bool = False
    costs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CostModel:
    """Mirrors ``RiskEngine``'s arithmetic so the two tiers are comparable."""

    expected_stop_slippage_pct: float = 0.15
    max_order_book_slippage_pct: float = 0.35

    def risk_pct(self, plan: SetupPlan, funding_intervals: float) -> float:
        """Reproduce ``estimated_loss_pct``: the R denominator the agent used.

        Sizing reserves entry slippage, stop slippage, spread and two sides of
        fee inside this figure, and ``risk_usd`` is derived from it. Dividing
        a replayed return by a bare stop distance instead would make every
        replayed variant look about a third better than the live record - the
        exact bias findings.md warns about in section 9.1.
        """
        adverse_funding = self.adverse_funding_pct(plan, funding_intervals)
        return (plan.stop_pct
                + 2 * plan.taker_fee_pct_per_side
                + max(0.0, plan.spread_pct)
                + self.expected_stop_slippage_pct
                + self.max_order_book_slippage_pct
                + adverse_funding)

    @staticmethod
    def adverse_funding_pct(plan: SetupPlan, intervals: float) -> float:
        """Funding is a cost only when it is paid, and only by one side."""
        rate = plan.funding_rate_pct
        adverse = max(0.0, rate) if plan.direction == "long" \
            else max(0.0, -rate)
        return adverse * max(0.0, intervals)

    def realised(self, plan: SetupPlan, result: str,
                 hours_held: float) -> dict:
        """Costs actually incurred on this trade, in percent of notional."""
        interval = max(0.0, plan.funding_interval_hours)
        intervals = math.floor(hours_held / interval) if interval > 0 else 0.0
        fees = 2 * plan.taker_fee_pct_per_side
        spread = max(0.0, plan.spread_pct)
        slippage = (self.expected_stop_slippage_pct if result == "stop"
                    else 0.0)
        funding = self.adverse_funding_pct(plan, intervals)
        return {
            "fees_pct": fees, "spread_pct": spread,
            "slippage_pct": slippage, "funding_pct": funding,
            "funding_intervals": intervals,
            "total_pct": fees + spread + slippage + funding,
        }


def resolve(plan: SetupPlan, bars: list, max_hold_hours: float = 24.0,
            costs: CostModel | None = None) -> Outcome:
    """Resolve one setup against 1-minute bars strictly after the signal bar.

    ``bars`` must be ascending and must not contain anything before the later
    of the signal bar's close and the actual decision/entry time. Handing this
    function an earlier bar is a programming error, not a data condition, so
    it raises rather than filtering: a resolver that quietly discarded
    lookahead would hide the bug that produced it.
    """
    costs = costs or CostModel()
    if plan.direction not in ("long", "short"):
        raise ValueError(f"unknown direction: {plan.direction}")
    if plan.stop_pct <= 0 or plan.take_pct <= 0:
        raise ValueError("stop_pct and take_pct must both be positive")

    floor_ts = plan.first_visible_ts
    previous_ts = None
    for bar in bars:
        bar_ts = int(bar.ts)
        if previous_ts is not None and bar_ts <= previous_ts:
            raise LookaheadError(
                f"{plan.symbol}: bars must be strictly ascending; "
                f"{bar_ts} follows {previous_ts}")
        previous_ts = bar_ts
        if bar_ts < floor_ts:
            raise LookaheadError(
                f"{plan.symbol}: bar at {bar.ts} precedes the first observable "
                f"post-entry bar ({floor_ts}); the resolver may not see it")

    if not bars:
        return Outcome(result="no_data")

    long = plan.direction == "long"
    entry = plan.entry_price
    stop = plan.stop_price()
    target = plan.take_price()
    deadline = floor_ts + int(max(0.0, max_hold_hours) * 3_600_000)

    mae = mfe = 0.0
    held = 0
    minute_ms = TIMEFRAME_MS["1m"]
    signal_close = int(plan.signal_ts) + TIMEFRAME_MS[plan.signal_timeframe]
    # Keep the one-minute grid on the signal bar's phase. Exchange timestamps
    # are normally epoch-aligned, while synthetic and migrated fixtures may
    # use a different but still internally consistent origin.
    expected_ts = signal_close + (
        (floor_ts - signal_close + minute_ms - 1) // minute_ms
    ) * minute_ms
    for bar in bars:
        if int(bar.ts) >= deadline:
            break
        # Missing minutes make elapsed time, excursions and funding unknowable.
        # Do not turn a sparse path into a shorter, cheaper completed trade.
        if int(bar.ts) != expected_ts:
            return Outcome(result="no_data")
        held += 1
        adverse = ((entry - bar.low) if long else (bar.high - entry))
        favourable = ((bar.high - entry) if long else (entry - bar.low))
        mae = max(mae, adverse / entry * 100.0)
        mfe = max(mfe, favourable / entry * 100.0)

        hit_stop = (bar.low <= stop) if long else (bar.high >= stop)
        hit_target = (bar.high >= target) if long else (bar.low <= target)

        if hit_stop and hit_target:
            # When both thresholds occur within one minute, order is unknown;
            # resolve against the trade with a fixed stop-first rule.
            return _finish(plan, costs, "stop", bar.close_ts, stop,
                           held, mae, mfe, tie=True)
        if hit_stop:
            return _finish(plan, costs, "stop", bar.close_ts, stop,
                           held, mae, mfe)
        if hit_target:
            return _finish(plan, costs, "target", bar.close_ts, target,
                           held, mae, mfe)
        expected_ts = int(bar.ts) + 60_000

    if held == 0:
        return Outcome(result="no_data")

    # Timeout exits at the bar close, matching the live max-hold force close.
    last = bars[held - 1]
    return _finish(plan, costs, "timeout", last.close_ts, last.close,
                   held, mae, mfe)


def _finish(plan: SetupPlan, costs: CostModel, result: str, exit_ts: int,
            exit_price: float, bars_held: int, mae: float, mfe: float,
            tie: bool = False) -> Outcome:
    entry = plan.entry_price
    gross = ((exit_price - entry) if plan.direction == "long"
             else (entry - exit_price)) / entry * 100.0
    hours = bars_held / 60.0
    realised = costs.realised(plan, result, hours)
    net = gross - realised["total_pct"]

    # Use the live engine's all-in stop-loss denominator so replayed and demo
    # R-multiples have the same meaning.
    risk_pct = costs.risk_pct(plan, realised["funding_intervals"])
    return Outcome(
        result=result, exit_ts=exit_ts, exit_price=exit_price,
        bars_held=bars_held, mae_pct=round(mae, 6), mfe_pct=round(mfe, 6),
        gross_pct=round(gross, 6), cost_pct=round(realised["total_pct"], 6),
        net_pct=round(net, 6), risk_pct=round(risk_pct, 6),
        r_multiple=round(net / risk_pct, 6) if risk_pct > 0 else 0.0,
        tie_broken=tie, costs=realised,
    )


def resolve_from_cache(plan: SetupPlan, cache, max_hold_hours: float = 24.0,
                       costs: CostModel | None = None) -> Outcome:
    """``resolve`` against a :class:`~research.prices.PriceCache`."""
    start = plan.first_visible_ts
    deadline = start + int(max(0.0, max_hold_hours) * 3_600_000)
    end = deadline + 60_000
    outcome = resolve(plan, cache.bars(plan.symbol, start, end),
                      max_hold_hours=max_hold_hours, costs=costs)
    # A stop/target is attributable once the continuous path reaches it. A
    # timeout, however, asserts that the whole holding window elapsed without
    # either event, so every minute through the deadline must be present.
    covers = getattr(cache, "covers", None)
    if (outcome.result == "timeout"
            and callable(covers)
            and not covers(plan.symbol, start, deadline)):
        return Outcome(result="no_data")
    return outcome
