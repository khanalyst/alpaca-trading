"""Cost-aware fade of a volatility-scaled deviation from the session VWAP.

The simulator is causal by construction:

* VWAP, volatility and the deviation score read completed bars only;
* the day type reads bars that ended by 10:00 plus completed prior sessions;
* a signal on a completed bar is acted on at the next bar's open;
* the first qualifying signal consumes the symbol-session even when its entry
  is refused, as in the live runtime.

Exits are resolved on bar highs and lows from the entry bar onwards. When one
bar touches both the stop and the target, the stop is assumed to come first.
A level fills at its price unless the bar opens beyond it, when it fills at
the open.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import product

from studies.liquid_reversion.bars import Bar, ny_instant
from studies.liquid_reversion.costs import (DEFAULT_NOTIONAL, DEFAULT_SLIPPAGE_BPS_PER_SIDE,
                                            round_trip_bps)
from studies.liquid_reversion.daytype import (DRIVE_GRID, GAP_GRID, LOOKBACK, MIN_HISTORY, DayType,
                                              DayTypeTracker, is_range_day)

ENTRY_GRID = (1.0, 1.5, 2.0)
COST_MULTIPLE_GRID = (1.5, 2.0)
STOP_GRID = (1.0, 1.5)
MAD_TO_SIGMA = 1.4826
LONG, SHORT = "long", "short"

VariantKey = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class Rules:
    """The fixed, preregistered rules; none of these is searched."""

    horizon_minutes: int = 60
    entry_start: time = time(10, 0)
    entry_end: time = time(15, 0)
    flat_time: time = time(15, 50)
    sigma_sessions: int = 5
    min_sigma_returns: int = 30
    lookback: int = LOOKBACK
    min_history: int = MIN_HISTORY
    notional: float = DEFAULT_NOTIONAL
    slippage_bps_per_side: float = DEFAULT_SLIPPAGE_BPS_PER_SIDE


RULES = Rules()


@dataclass(frozen=True, slots=True, order=True)
class Params:
    """One point of the searched grid."""

    k_drive: float
    k_gap: float
    k_entry: float
    cost_multiple: float
    stop_mult: float

    @property
    def variant_key(self) -> VariantKey:
        """The part of the grid that shapes a trade once the day is known to be a range day."""
        return (self.k_entry, self.cost_multiple, self.stop_mult)

    def as_dict(self) -> dict[str, float | str]:
        return {name: ("inf" if math.isinf(value) else value)
                for name, value in (("k_drive", self.k_drive), ("k_gap", self.k_gap),
                                    ("k_entry", self.k_entry), ("cost_multiple", self.cost_multiple),
                                    ("stop_mult", self.stop_mult))}


GRID: tuple[Params, ...] = tuple(Params(*values) for values in product(
    DRIVE_GRID, GAP_GRID, ENTRY_GRID, COST_MULTIPLE_GRID, STOP_GRID))
SMOKE_PARAMS = Params(k_drive=0.8, k_gap=1.5, k_entry=1.5, cost_multiple=1.5, stop_mult=1.5)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A completed bar scored for a possible signal; ``signal_ts`` is the bar's end."""

    index: int
    signal_ts: datetime
    z: float
    move_bps: float
    cost_bps: float
    vwap: float
    sigma_bps: float


@dataclass(frozen=True, slots=True)
class Trade:
    """One simulated round trip.

    ``signal_ts`` is the end of the signal bar (when the signal became known);
    ``entry_ts`` is the start of the entry bar, whose open is the fill.
    """

    symbol: str
    session: date
    side: str
    signal_ts: datetime
    entry_ts: datetime
    entry: float
    exit_ts: datetime
    exit: float
    reason: str
    gross_bps: float
    cost_bps: float
    net_bps: float
    target: float
    stop: float
    z: float

    def net_at(self, stress: float) -> float:
        """Net bps with the cost multiplied by ``stress``."""
        return self.gross_bps - stress * self.cost_bps

    def as_dict(self) -> dict[str, object]:
        return {"symbol": self.symbol, "session": self.session.isoformat(), "side": self.side,
                "signal_ts": self.signal_ts.isoformat(), "entry_ts": self.entry_ts.isoformat(),
                "entry": round(self.entry, 4), "exit_ts": self.exit_ts.isoformat(),
                "exit": round(self.exit, 4), "reason": self.reason,
                "gross_bps": round(self.gross_bps, 3), "cost_bps": round(self.cost_bps, 3),
                "net_bps": round(self.net_bps, 3), "target": round(self.target, 4),
                "stop": round(self.stop, 4), "z": round(self.z, 3)}


@dataclass(frozen=True, slots=True)
class Refusal:
    """A first qualifying signal whose entry was refused; it still consumes the session."""

    symbol: str
    session: date
    signal_ts: datetime
    reason: str
    z: float


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """What the grid needs from one symbol-session."""

    symbol: str
    session: date
    day_type: DayType
    variants: Mapping[VariantKey, Trade | Refusal | None]


def vwap_series(bars: Sequence[Bar]) -> list[float | None]:
    """Session VWAP after each completed bar: cumulative typical price x volume over volume."""
    out: list[float | None] = []
    price_volume = volume = 0.0
    for bar in bars:
        price_volume += (bar.high + bar.low + bar.close) / 3.0 * bar.volume
        volume += bar.volume
        out.append(price_volume / volume if volume > 0 else None)
    return out


def abs_log_returns(bars: Sequence[Bar]) -> list[float]:
    """Absolute log returns between adjacent bars of one session (no overnight return)."""
    return [abs(math.log(current.close / previous.close))
            for previous, current in zip(bars, bars[1:]) if previous.end == current.ts]


def robust_sigma_bps(sorted_abs_returns: Sequence[float], interval_seconds: int) -> float | None:
    """Per-minute volatility: 1.4826 x median absolute log return, in bps.

    Returns over longer bars are scaled to one minute by dividing by the square
    root of the bar length in minutes.
    """
    count = len(sorted_abs_returns)
    if not count:
        return None
    middle = count // 2
    centre = (sorted_abs_returns[middle] if count % 2
              else 0.5 * (sorted_abs_returns[middle - 1] + sorted_abs_returns[middle]))
    sigma = MAD_TO_SIGMA * centre * 1e4 / math.sqrt(interval_seconds / 60.0)
    return sigma if sigma > 0 else None


def sigma_series(bars: Sequence[Bar], prior_returns: Iterable[float], *,
                 min_returns: int = RULES.min_sigma_returns) -> list[float | None]:
    """Robust per-minute volatility after each completed bar.

    The pool is the prior sessions' returns plus this session's returns up to
    and including the bar.
    """
    pool = sorted(prior_returns)
    out: list[float | None] = []
    for index, bar in enumerate(bars):
        if index and bars[index - 1].end == bar.ts:
            bisect.insort(pool, abs(math.log(bar.close / bars[index - 1].close)))
        out.append(robust_sigma_bps(pool, bar.interval_seconds) if len(pool) >= min_returns else None)
    return out


def signal_candidates(bars: Sequence[Bar], *, day: date, prior_returns: Iterable[float],
                      rules: Rules = RULES, spread_bps: float | None = None) -> list[Candidate]:
    """Score every completed bar whose end lies in [entry_start, entry_end).

    Each score reads only that bar and earlier ones: cumulative VWAP, robust
    volatility of the session so far plus prior sessions, and the round-trip
    cost at the bar's close.
    """
    vwaps = vwap_series(bars)
    sigmas = sigma_series(bars, prior_returns, min_returns=rules.min_sigma_returns)
    first = ny_instant(day, rules.entry_start)
    last = ny_instant(day, rules.entry_end)
    horizon = math.sqrt(rules.horizon_minutes)
    out: list[Candidate] = []
    for index, bar in enumerate(bars):
        finished = bar.end
        if finished < first:
            continue
        if finished >= last:
            break
        vwap, sigma = vwaps[index], sigmas[index]
        if vwap is None or sigma is None:
            continue
        z = (bar.close - vwap) / vwap * 1e4 / (sigma * horizon)
        move = abs(vwap - bar.close) / bar.close * 1e4
        cost = round_trip_bps(bar.close, notional=rules.notional, spread_bps=spread_bps,
                              slippage_bps_per_side=rules.slippage_bps_per_side)
        out.append(Candidate(index, finished, z, move, cost, vwap, sigma))
    return out


def first_signal(candidates: Sequence[Candidate], k_entry: float,
                 cost_multiple: float) -> Candidate | None:
    """The first candidate far enough from VWAP, in sigma and in multiples of cost."""
    for candidate in candidates:
        if abs(candidate.z) >= k_entry and candidate.move_bps >= cost_multiple * candidate.cost_bps:
            return candidate
    return None


def plan_entry(bars: Sequence[Bar], signal: Candidate, *, day: date,
               rules: Rules = RULES) -> int | str:
    """The entry bar's index, or the reason the entry is refused.

    Only the next bar's start and open are read: the fill is that open, and it
    must come before ``entry_end`` and on the near side of the frozen target.
    """
    index = signal.index + 1
    if index >= len(bars):
        return "no_next_bar"
    entry_bar = bars[index]
    if entry_bar.ts >= ny_instant(day, rules.entry_end):
        return "entry_after_cutoff"
    direction = -1 if signal.z > 0 else 1
    if direction * (signal.vwap - entry_bar.open) <= 0:
        return "through_target"
    return index


def exit_deadline(entry_ts: datetime, day: date, rules: Rules = RULES) -> tuple[datetime, str]:
    """The end of the last bar the trade may hold through, and the exit label used there."""
    hold_end = entry_ts + timedelta(minutes=rules.horizon_minutes)
    flat = ny_instant(day, rules.flat_time)
    return (hold_end, "time") if hold_end <= flat else (flat, "flat")


def simulate_exit(bars: Sequence[Bar], start: int, *, direction: int, target: float, stop: float,
                  deadline: datetime, deadline_reason: str) -> tuple[float, datetime, str]:
    """Walk bars from the entry bar to the deadline and return (price, time, reason).

    Per bar: a gap beyond a level fills at the open; otherwise the stop is
    checked before the target (stop-first ties); otherwise the bar that ends
    at the deadline exits at its close. If that bar is missing, the first
    later bar's open is used; if the data ends first, the last close.
    """
    last = bars[start]
    for bar in bars[start:]:
        if bar.end > deadline:
            return bar.open, bar.ts, deadline_reason
        last = bar
        if direction > 0:
            if bar.open <= stop:
                return bar.open, bar.ts, "stop_gap"
            if bar.open >= target:
                return bar.open, bar.ts, "target_gap"
            if bar.low <= stop:
                return stop, bar.end, "stop"
            if bar.high >= target:
                return target, bar.end, "target"
        else:
            if bar.open >= stop:
                return bar.open, bar.ts, "stop_gap"
            if bar.open <= target:
                return bar.open, bar.ts, "target_gap"
            if bar.high >= stop:
                return stop, bar.end, "stop"
            if bar.low <= target:
                return target, bar.end, "target"
        if bar.end == deadline:
            return bar.close, bar.end, deadline_reason
    return last.close, last.end, "data_end"


def build_trade(bars: Sequence[Bar], signal: Candidate, entry_index: int, *, symbol: str,
                day: date, stop_mult: float, rules: Rules = RULES,
                spread_bps: float | None = None) -> Trade:
    """Enter at the entry bar's open, target the frozen VWAP and resolve the exit."""
    entry_bar = bars[entry_index]
    entry = entry_bar.open
    direction = -1 if signal.z > 0 else 1
    target = signal.vwap
    stop = entry - direction * stop_mult * abs(entry - target)
    deadline, label = exit_deadline(entry_bar.ts, day, rules)
    price, when, reason = simulate_exit(bars, entry_index, direction=direction, target=target,
                                        stop=stop, deadline=deadline, deadline_reason=label)
    gross = direction * (price / entry - 1.0) * 1e4
    cost = round_trip_bps(entry, notional=rules.notional, spread_bps=spread_bps,
                          slippage_bps_per_side=rules.slippage_bps_per_side)
    return Trade(symbol, day, LONG if direction > 0 else SHORT, signal.signal_ts, entry_bar.ts, entry,
                 when, price, reason, gross, cost, gross - cost, target, stop, signal.z)


def session_variants(bars: Sequence[Bar], *, symbol: str, day: date, prior_returns: Iterable[float],
                     keys: Iterable[VariantKey], rules: Rules = RULES,
                     spread_bps: float | None = None) -> dict[VariantKey, Trade | Refusal | None]:
    """The outcome of each (k_entry, cost_multiple, stop_mult) on a range day.

    One trade per symbol-session: only the first qualifying signal is taken,
    and a refused entry still consumes the session.
    """
    candidates = signal_candidates(bars, day=day, prior_returns=prior_returns, rules=rules,
                                   spread_bps=spread_bps)
    signals: dict[tuple[float, float], Candidate | None] = {}
    entries: dict[int, int | str] = {}
    out: dict[VariantKey, Trade | Refusal | None] = {}
    for key in keys:
        k_entry, cost_multiple, stop_mult = key
        if (k_entry, cost_multiple) not in signals:
            signals[(k_entry, cost_multiple)] = first_signal(candidates, k_entry, cost_multiple)
        signal = signals[(k_entry, cost_multiple)]
        if signal is None:
            out[key] = None
            continue
        if signal.index not in entries:
            entries[signal.index] = plan_entry(bars, signal, day=day, rules=rules)
        planned = entries[signal.index]
        if isinstance(planned, str):
            out[key] = Refusal(symbol, day, signal.signal_ts, planned, signal.z)
        else:
            out[key] = build_trade(bars, signal, planned, symbol=symbol, day=day,
                                   stop_mult=stop_mult, rules=rules, spread_bps=spread_bps)
    return out


def run_symbol(symbol: str, sessions: Iterable[tuple[date, Sequence[Bar]]], *,
               keys: Iterable[VariantKey], day_filter: Callable[[DayType], bool],
               rules: Rules = RULES, spread_bps: float | None = None,
               skip_sessions: frozenset[date] = frozenset()) -> Iterator[SessionOutcome]:
    """Stream one symbol's sessions in date order and score each against the variants.

    State carried between sessions is the day-type history, the prior
    sessions' returns for volatility and the prior close; all of it is updated
    only after a session has been scored. Sessions in ``skip_sessions`` (for
    example early closes) feed that history but are never traded.
    """
    keys = tuple(sorted(set(keys)))
    tracker = DayTypeTracker(lookback=rules.lookback, min_history=rules.min_history)
    history: deque[list[float]] = deque(maxlen=rules.sigma_sessions)
    prior_close: float | None = None
    previous_day: date | None = None
    for day, bars in sessions:
        if previous_day is not None and day <= previous_day:
            raise ValueError(f"{symbol}: sessions must be in strictly increasing date order")
        previous_day = day
        if not bars:
            continue
        if bars[0].symbol != symbol or bars[-1].symbol != symbol:
            raise ValueError(f"{symbol}: session {day} holds another symbol's bars")
        day_type = tracker.classify(day, bars, prior_close)
        variants: dict[VariantKey, Trade | Refusal | None] = {}
        if day not in skip_sessions and day_filter(day_type):
            prior = [value for returns in history for value in returns]
            variants = session_variants(bars, symbol=symbol, day=day, prior_returns=prior, keys=keys,
                                        rules=rules, spread_bps=spread_bps)
        yield SessionOutcome(symbol, day, day_type, variants)
        history.append(abs_log_returns(bars))
        prior_close = bars[-1].close


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Trades and refused entries for one parameter set, with every day type seen."""

    trades: tuple[Trade, ...]
    refusals: tuple[Refusal, ...]
    day_types: Mapping[tuple[str, date], DayType]


def simulate(sessions_by_symbol: Mapping[str, Mapping[date, Sequence[Bar]]], params: Params, *,
             rules: Rules = RULES, spreads: Mapping[str, float] | None = None,
             skip_sessions: frozenset[date] = frozenset()) -> SimulationResult:
    """Run one parameter set over every symbol."""
    result = simulate_grid(((symbol, sorted(days.items())) for symbol, days in sessions_by_symbol.items()),
                           (params,), rules=rules, spreads=spreads, skip_sessions=skip_sessions)
    return SimulationResult(tuple(result.trades[params]), tuple(result.refusals[params]),
                            result.day_types)


@dataclass
class GridResult:
    """Trades and refusals per grid point, plus the day types seen per symbol-session."""

    grid: tuple[Params, ...]
    trades: dict[Params, list[Trade]]
    refusals: dict[Params, list[Refusal]]
    day_types: dict[tuple[str, date], DayType] = field(default_factory=dict)

    def sessions(self) -> list[date]:
        """Every session date seen for any symbol, in order."""
        return sorted({day for _, day in self.day_types})

    def known_sessions(self) -> list[date]:
        """Session dates on which at least one symbol had a known day type."""
        return sorted({day for (_, day), kind in self.day_types.items() if kind.known})


def simulate_grid(streams: Iterable[tuple[str, Iterable[tuple[date, Sequence[Bar]]]]],
                  grid: Sequence[Params] = GRID, *, rules: Rules = RULES,
                  spreads: Mapping[str, float] | None = None,
                  skip_sessions: frozenset[date] = frozenset()) -> GridResult:
    """Score every grid point in one causal pass over each symbol's sessions.

    The day type gates a whole session, and the signal, entry and exit depend
    only on (k_entry, cost_multiple, stop_mult). Each session is therefore
    simulated once per distinct variant and shared by the grid points whose
    day-type thresholds admit it, which is identical to running each grid
    point on its own.
    """
    grid = tuple(grid)
    keys = {params.variant_key for params in grid}
    gates: dict[tuple[float, float], list[Params]] = {}
    for params in grid:
        gates.setdefault((params.k_drive, params.k_gap), []).append(params)

    def admits_any(day_type: DayType) -> bool:
        return any(is_range_day(day_type, k_drive, k_gap) for k_drive, k_gap in gates)

    result = GridResult(grid, {params: [] for params in grid}, {params: [] for params in grid})
    for symbol, sessions in streams:
        spread = (spreads or {}).get(symbol)
        for outcome in run_symbol(symbol, sessions, keys=keys, day_filter=admits_any, rules=rules,
                                  spread_bps=spread, skip_sessions=skip_sessions):
            result.day_types[(symbol, outcome.session)] = outcome.day_type
            if not outcome.variants:
                continue
            for (k_drive, k_gap), members in gates.items():
                if not is_range_day(outcome.day_type, k_drive, k_gap):
                    continue
                for params in members:
                    found = outcome.variants.get(params.variant_key)
                    if isinstance(found, Trade):
                        result.trades[params].append(found)
                    elif isinstance(found, Refusal):
                        result.refusals[params].append(found)
    for params in grid:
        result.trades[params].sort(key=lambda trade: (trade.session, trade.symbol))
        result.refusals[params].sort(key=lambda refusal: (refusal.session, refusal.symbol))
    return result
