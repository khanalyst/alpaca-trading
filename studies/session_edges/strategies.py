"""The registered session-level rules, their decision functions and their simulators.

Every rule is taken from published research and its parameters are fixed
here, not searched. Each simulator calls the same small decision function the
forward runner calls, so the study and the live shadow cannot drift apart:

* :func:`noise_band_target` - intraday momentum outside a time-of-day noise
  band (Zarattini, Aziz and Barbon, 2024);
* :func:`close_reversion_action` - buy a weak close, sell the recovery, using
  the internal bar strength (Pagonidis, 2013);
* :func:`simulate_opening_range` - first-candle opening range breakout
  (Zarattini and Aziz, 2023);
* :func:`simulate_first_last` and :func:`simulate_overnight` - two further
  published effects kept as reproducible diagnostics.

A simulator returns one :class:`DayResult` per session: the gross return on
the position's notional at unit exposure and the number of round trips. Costs
are charged afterwards, per round trip, by :mod:`studies.session_edges.stats`.
Decisions read completed bars only and fill at the open of the next bar.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from studies.session_edges.data import MINUTES, DailyBar, Session, previous_close

LONG, SHORT, FLAT = 1, -1, 0
BOTH_SIDES = ("long", "short")


@dataclass(frozen=True, slots=True)
class Fill:
    """One completed round trip inside a session."""

    side: int
    entry_minute: int
    entry: float
    exit_minute: int
    exit: float

    @property
    def gross(self) -> float:
        return self.side * (self.exit / self.entry - 1.0)


@dataclass(frozen=True, slots=True)
class DayResult:
    """A session's gross return at unit exposure and the round trips that produced it."""

    day: date
    gross: float = 0.0
    round_trips: int = 0
    fills: tuple[Fill, ...] = field(default=())


# --------------------------------------------------------------------------
# Intraday momentum outside the noise band
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NoiseBandRules:
    """Fixed rules of the noise-band momentum strategy; none of these is searched."""

    lookback_sessions: int = 14
    decision_step_minutes: int = 30
    first_decision_minute: int = 30
    last_decision_minute: int = 360
    band_multiple: float = 1.0
    use_previous_close: bool = True
    sides: tuple[str, ...] = BOTH_SIDES
    # Minute index whose open is the end-of-day exit. MINUTES means the last
    # bar's close (the published rule); 388 is a 15:58 market order.
    flat_minute: int = MINUTES

    def decision_minutes(self) -> tuple[int, ...]:
        return tuple(range(self.first_decision_minute, self.last_decision_minute + 1,
                           self.decision_step_minutes))


NOISE_BAND = NoiseBandRules()


def noise_band_sigma(history: Sequence[Session], minute: int, lookback: int) -> float | None:
    """Mean absolute move from the open to ``minute`` over the last ``lookback`` sessions.

    ``minute`` counts minutes from the open, so the move is read at the close
    of bar ``minute - 1``. Returns ``None`` with fewer than ``lookback`` sessions.
    """
    if lookback <= 0 or len(history) < lookback or not 1 <= minute <= MINUTES:
        return None
    total = 0.0
    for session in history[-lookback:]:
        total += abs(session.close[minute - 1] / session.open[0] - 1.0)
    return total / lookback


def noise_band_bounds(open_price: float, prior_close: float | None, sigma: float,
                      band_multiple: float = 1.0) -> tuple[float, float]:
    """Upper and lower noise boundaries; an overnight gap widens the band on its own side."""
    upper_reference = open_price if prior_close is None else max(open_price, prior_close)
    lower_reference = open_price if prior_close is None else min(open_price, prior_close)
    return (upper_reference * (1.0 + band_multiple * sigma),
            lower_reference * (1.0 - band_multiple * sigma))


def noise_band_target(position: int, price: float, upper: float, lower: float,
                      sides: Sequence[str] = BOTH_SIDES) -> int:
    """The position to hold after one decision: +1 long, -1 short, 0 flat.

    Outside the band the rule follows the move; back inside it, an open
    position is closed. This is the whole decision, shared by the simulator
    and the forward runner.
    """
    if price > upper and "long" in sides:
        return LONG
    if price < lower and "short" in sides:
        return SHORT
    if position == LONG and price < upper:
        return FLAT
    if position == SHORT and price > lower:
        return FLAT
    return position


def simulate_noise_band(sessions: Sequence[Session], rules: NoiseBandRules = NOISE_BAND) -> list[DayResult]:
    """Run the noise-band rule over ``sessions`` (date order)."""
    decisions = rules.decision_minutes()
    out: list[DayResult] = []
    for index, session in enumerate(sessions):
        history = sessions[max(0, index - rules.lookback_sessions):index]
        if len(history) < rules.lookback_sessions:
            out.append(DayResult(session.day))
            continue
        prior = previous_close(sessions, index) if rules.use_previous_close else None
        position = FLAT
        entry = 0.0
        entry_minute = 0
        fills: list[Fill] = []
        for minute in decisions:
            sigma = noise_band_sigma(history, minute, rules.lookback_sessions)
            if sigma is None:
                break
            upper, lower = noise_band_bounds(session.open[0], prior, sigma, rules.band_multiple)
            target = noise_band_target(position, session.close[minute - 1], upper, lower, rules.sides)
            if target == position:
                continue
            if minute >= min(rules.flat_minute, MINUTES):
                break
            fill = session.open[minute]
            if position != FLAT:
                fills.append(Fill(position, entry_minute, entry, minute, fill))
            position, entry, entry_minute = target, fill, minute
        if position != FLAT:
            if rules.flat_minute >= MINUTES:
                fills.append(Fill(position, entry_minute, entry, MINUTES, session.close[-1]))
            else:
                fills.append(Fill(position, entry_minute, entry, rules.flat_minute,
                                  session.open[rules.flat_minute]))
        out.append(DayResult(session.day, math.fsum(fill.gross for fill in fills), len(fills), tuple(fills)))
    return out


# --------------------------------------------------------------------------
# Close-weakness reversion (internal bar strength)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CloseReversionRules:
    """Fixed rules of the close-weakness reversion strategy."""

    decision_minute: int = 380        # 15:50 New York: late enough to see the day, early enough to trade
    enter_below: float = 0.20
    exit_above: float = 0.50
    max_sessions: int = 5


CLOSE_REVERSION = CloseReversionRules()
ENTER, EXIT, HOLD, STAND_ASIDE = "enter", "exit", "hold", "none"


def internal_bar_strength(high: float, low: float, price: float) -> float:
    """Where ``price`` sits in the session's range so far: 0 at the low, 1 at the high."""
    width = high - low
    if not width > 0:
        return 0.5
    return min(1.0, max(0.0, (price - low) / width))


def close_reversion_action(in_position: bool, sessions_held: int, strength: float,
                           rules: CloseReversionRules = CLOSE_REVERSION) -> str:
    """The action at the daily decision time.

    ``sessions_held`` counts decision times since the entry, including this
    one. A position is closed once the price has recovered into the upper part
    of the day's range, or after ``max_sessions`` sessions; flat, a close in
    the bottom of the range is bought. An exit and a new entry never share a day.
    """
    if in_position:
        if strength > rules.exit_above or sessions_held >= rules.max_sessions:
            return EXIT
        return HOLD
    return ENTER if strength < rules.enter_below else STAND_ASIDE


def simulate_close_reversion(sessions: Sequence[Session],
                             rules: CloseReversionRules = CLOSE_REVERSION) -> list[DayResult]:
    """Run the close-weakness rule on one-minute sessions, deciding at ``decision_minute``.

    The range and the price are read at the close of bar ``decision_minute - 1``
    and the order fills at the open of bar ``decision_minute``. Overnight
    returns use raw session closes: unadjusted data understates a position
    held over an ex-dividend date by the dividend, which is conservative.
    """
    decide = rules.decision_minute
    if not 1 <= decide < MINUTES:
        raise ValueError("decision_minute must leave one bar to fill on")
    out: list[DayResult] = []
    in_position = False
    held = 0
    for index, session in enumerate(sessions):
        high = max(session.high[:decide])
        low = min(session.low[:decide])
        strength = internal_bar_strength(high, low, session.close[decide - 1])
        fill = session.open[decide]
        last = session.close[-1]
        gross = 0.0
        trips = 0
        prior = previous_close(sessions, index)
        if in_position and prior is None:
            in_position = False      # the position cannot be marked across missing sessions
        if in_position:
            held += 1
            action = close_reversion_action(True, held, strength, rules)
            if action == EXIT:
                gross = fill / prior - 1.0
                trips = 1
                in_position = False
            else:
                gross = last / prior - 1.0
        elif close_reversion_action(False, 0, strength, rules) == ENTER:
            in_position = True
            held = 0
            gross = last / fill - 1.0
        out.append(DayResult(session.day, gross, trips))
    return out


def simulate_close_reversion_daily(bars: Sequence[DailyBar],
                                   rules: CloseReversionRules = CLOSE_REVERSION) -> list[DayResult]:
    """The same rule on daily bars, deciding and trading at the close itself.

    This is the long-history form (decades of daily data); it is optimistic by
    the last ten minutes of each session relative to the minute simulator.
    """
    out: list[DayResult] = []
    in_position = False
    held = 0
    for index, bar in enumerate(bars):
        gross = 0.0
        trips = 0
        if in_position:
            held += 1
            gross = bar.close / bars[index - 1].close - 1.0
        strength = internal_bar_strength(bar.high, bar.low, bar.close)
        action = close_reversion_action(in_position, held, strength, rules)
        if action == EXIT:
            in_position = False
            trips = 1
        elif action == ENTER:
            in_position = True
            held = 0
        out.append(DayResult(bar.day, gross, trips))
    return out


# --------------------------------------------------------------------------
# Opening range breakout
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OpeningRangeRules:
    """Fixed rules of the first-candle opening range breakout."""

    minutes: int = 5
    target_r: float = 10.0
    sides: tuple[str, ...] = BOTH_SIDES


OPENING_RANGE = OpeningRangeRules()


def simulate_opening_range(sessions: Sequence[Session],
                           rules: OpeningRangeRules = OPENING_RANGE) -> list[DayResult]:
    """Trade in the direction of the first candle, stop at its far extreme, flat at the close.

    Stops and targets are resolved on one-minute highs and lows from the entry
    bar on; a bar that touches both is a stop, and a bar that opens through a
    level fills at its open.
    """
    width = rules.minutes
    if not 1 <= width < MINUTES - 1:
        raise ValueError("opening range must leave a bar to enter on")
    out: list[DayResult] = []
    for session in sessions:
        first_open, first_close = session.open[0], session.close[width - 1]
        if first_close == first_open:
            out.append(DayResult(session.day))
            continue
        side = LONG if first_close > first_open else SHORT
        if ("long" if side == LONG else "short") not in rules.sides:
            out.append(DayResult(session.day))
            continue
        entry = session.open[width]
        stop = min(session.low[:width]) if side == LONG else max(session.high[:width])
        risk = side * (entry - stop)
        if not risk > 0:
            out.append(DayResult(session.day))
            continue
        target = entry + side * rules.target_r * risk
        exit_price, exit_minute = session.close[-1], MINUTES
        for minute in range(width, MINUTES):
            o, h, l = session.open[minute], session.high[minute], session.low[minute]
            if side == LONG:
                if l <= stop:
                    exit_price, exit_minute = (o if o < stop else stop), minute
                    break
                if h >= target:
                    exit_price, exit_minute = target, minute
                    break
            else:
                if h >= stop:
                    exit_price, exit_minute = (o if o > stop else stop), minute
                    break
                if l <= target:
                    exit_price, exit_minute = target, minute
                    break
        fill = Fill(side, width, entry, exit_minute, exit_price)
        out.append(DayResult(session.day, fill.gross, 1, (fill,)))
    return out


# --------------------------------------------------------------------------
# Diagnostics: effects that were tested and are reported, not traded
# --------------------------------------------------------------------------

def simulate_first_last(sessions: Sequence[Session], *, first_minutes: int = 30,
                        last_minutes: int = 30) -> list[DayResult]:
    """Gao, Han, Li and Zhou (2018): the first half hour's sign held over the last half hour."""
    out: list[DayResult] = []
    start = MINUTES - last_minutes
    for index, session in enumerate(sessions):
        reference = previous_close(sessions, index) or session.open[0]
        first = session.close[first_minutes - 1] / reference - 1.0
        if first == 0:
            out.append(DayResult(session.day))
            continue
        side = LONG if first > 0 else SHORT
        fill = Fill(side, start, session.open[start], MINUTES, session.close[-1])
        out.append(DayResult(session.day, fill.gross, 1, (fill,)))
    return out


def simulate_overnight(bars: Sequence[DailyBar]) -> list[DayResult]:
    """Long from each close to the next open (the overnight return), on adjusted daily bars."""
    out: list[DayResult] = []
    for index, bar in enumerate(bars):
        if index == 0:
            out.append(DayResult(bar.day))
            continue
        out.append(DayResult(bar.day, bar.open / bars[index - 1].close - 1.0, 1))
    return out
