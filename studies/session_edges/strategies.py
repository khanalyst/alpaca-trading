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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from studies.session_edges.data import MAX_SESSION_GAP_DAYS, MINUTES, DailyBar, Session, previous_close

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
    # Mark-to-market strategies charge a half round trip on each actual
    # entry/exit, including an entry still open at the end of the sample.
    cost_round_trips: float | None = None


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
    # Match the runner's 15:58 order. A final-close diagnostic must explicitly
    # request MINUTES and has a different execution assumption.
    flat_minute: int = 388

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
        if not session.has_observed(0, minute):
            return None
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
        if not _observed_exit(session, rules.flat_minute):
            out.append(DayResult(session.day))
            continue
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
            if not session.has_observed(0, minute + 1):
                continue
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
    last_mark: float | None = None
    for index, session in enumerate(sessions):
        gross = 0.0
        trips = 0
        cost_round_trips = 0.0
        if index and (session.day - sessions[index - 1].day).days > MAX_SESSION_GAP_DAYS:
            if in_position:
                raise ValueError("missing sessions interrupt an owned swing position; replay cannot erase exposure")
            in_position = False
            last_mark = None
        available = session.has_observed(0, decide + 1)
        strength = internal_bar_strength(max(session.high[:decide]), min(session.low[:decide]),
                                         session.close[decide - 1]) if available else None
        if in_position:
            held += 1
            action = close_reversion_action(True, held, strength, rules) if available else HOLD
            if action == EXIT and last_mark is not None:
                gross = session.open[decide] / last_mark - 1.0
                trips = 1
                cost_round_trips += 0.5
                in_position = False
                last_mark = None
            elif session.has_observed(MINUTES - 1) and last_mark is not None:
                gross = session.close[-1] / last_mark - 1.0
                last_mark = session.close[-1]
        elif available and close_reversion_action(False, 0, strength, rules) == ENTER:
            in_position = True
            held = 0
            cost_round_trips += 0.5
            last_mark = session.open[decide]
            if session.has_observed(MINUTES - 1):
                gross = session.close[-1] / last_mark - 1.0
                last_mark = session.close[-1]
        out.append(DayResult(session.day, gross, trips, cost_round_trips=cost_round_trips))
    return out


def simulate_close_reversion_daily(bars: Sequence[DailyBar],
                                   rules: CloseReversionRules = CLOSE_REVERSION) -> list[DayResult]:
    """Decide using a completed daily bar and trade at the next available open.

    This full-day signal is a distinct strategy from the 15:50 minute rule;
    neither its information set nor its execution price is interchangeable.
    """
    wanted: list[bool] = []
    in_position = False
    held = 0
    for index, bar in enumerate(bars):
        if in_position:
            held += 1
        strength = internal_bar_strength(bar.high, bar.low, bar.close)
        action = close_reversion_action(in_position, held, strength, rules)
        if action == EXIT:
            in_position = False
        elif action == ENTER:
            in_position = True
            held = 0
        wanted.append(in_position)
    return _next_open_results(bars, wanted)


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
        # Every intervening bar is required to know whether a stop/target hit.
        if not session.has_observed(0, MINUTES):
            out.append(DayResult(session.day))
            continue
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
# Further session-level families searched by the discovery engine
# --------------------------------------------------------------------------

def _observed_exit(session: Session, flat_minute: int) -> bool:
    return session.has_observed(MINUTES - 1 if flat_minute >= MINUTES else flat_minute)


def _flat_exit(session: Session, flat_minute: int) -> tuple[int, float]:
    if flat_minute >= MINUTES:
        return MINUTES, session.close[-1]
    return flat_minute, session.open[flat_minute]


def simulate_last_hour(sessions: Sequence[Session], *, mode: str = "follow", threshold: float = 0.0,
                       decision_minute: int = 330, lookback_sessions: int = 14,
                       flat_minute: int = 388) -> list[DayResult]:
    """Follow or fade the day's move so far at ``decision_minute`` and hold to the end of day.

    ``threshold`` is in multiples of the usual absolute move from the open to
    that time of day; zero trades every session.
    """
    if mode not in ("follow", "fade"):
        raise ValueError("mode must be follow or fade")
    out: list[DayResult] = []
    for index, session in enumerate(sessions):
        if not session.has_observed(0, decision_minute + 1) or not _observed_exit(session, flat_minute):
            out.append(DayResult(session.day))
            continue
        history = sessions[max(0, index - lookback_sessions):index]
        sigma = noise_band_sigma(history, decision_minute, lookback_sessions)
        move = session.close[decision_minute - 1] / session.open[0] - 1.0
        if sigma is None or move == 0 or abs(move) <= threshold * sigma:
            out.append(DayResult(session.day))
            continue
        side = (LONG if move > 0 else SHORT) * (1 if mode == "follow" else -1)
        exit_minute, exit_price = _flat_exit(session, flat_minute)
        fill = Fill(side, decision_minute, session.open[decision_minute], exit_minute, exit_price)
        out.append(DayResult(session.day, fill.gross, 1, (fill,)))
    return out


def simulate_gap(sessions: Sequence[Session], *, mode: str = "fade", threshold: float = 1.0,
                 exit_minute: int = 60, entry_minute: int = 1, lookback_sessions: int = 20) -> list[DayResult]:
    """Follow or fade the overnight gap when it exceeds ``threshold`` standard deviations of recent gaps."""
    if mode not in ("follow", "fade"):
        raise ValueError("mode must be follow or fade")
    out: list[DayResult] = []
    gaps: list[float] = []
    for index, session in enumerate(sessions):
        prior = previous_close(sessions, index)
        if prior is None or not session.has_observed(0) or not session.has_observed(entry_minute):
            out.append(DayResult(session.day))
            continue
        gap = session.open[0] / prior - 1.0
        recent = gaps[-lookback_sessions:]
        gaps.append(gap)
        if len(recent) < lookback_sessions:
            out.append(DayResult(session.day))
            continue
        mean = math.fsum(recent) / len(recent)
        spread = math.sqrt(math.fsum((value - mean) ** 2 for value in recent) / (len(recent) - 1))
        if not spread > 0 or abs(gap) <= threshold * spread or not _observed_exit(session, exit_minute):
            out.append(DayResult(session.day))
            continue
        side = (LONG if gap > 0 else SHORT) * (1 if mode == "follow" else -1)
        leave, price = _flat_exit(session, exit_minute)
        fill = Fill(side, entry_minute, session.open[entry_minute], leave, price)
        out.append(DayResult(session.day, fill.gross, 1, (fill,)))
    return out


def simulate_overnight_hold(sessions: Sequence[Session], *, condition: str = "any",
                            strength_below: float = 0.20, enter_minute: int = 388,
                            exit_minute: int = 1) -> list[DayResult]:
    """Buy near the close and sell just after the next open, optionally only after a weak day.

    ``condition`` is ``any``, ``weak_close`` (internal bar strength below
    ``strength_below``) or ``down_day`` (price below the day's open), read at
    the close of the bar before ``enter_minute``.
    """
    if condition not in ("any", "weak_close", "down_day"):
        raise ValueError("unknown overnight condition")
    out: list[DayResult] = []
    holding = False
    last_mark: float | None = None
    for index, session in enumerate(sessions):
        gross = 0.0
        trips = 0
        cost_round_trips = 0.0
        if index and (session.day - sessions[index - 1].day).days > MAX_SESSION_GAP_DAYS:
            if holding:
                raise ValueError("missing sessions interrupt an owned overnight position; replay cannot erase exposure")
            holding = False
            last_mark = None
        if holding:
            if session.has_observed(exit_minute) and last_mark is not None:
                gross += session.open[exit_minute] / last_mark - 1.0
                trips = 1
                cost_round_trips += 0.5
                holding = False
                last_mark = None
            else:
                if session.has_observed(MINUTES - 1) and last_mark is not None:
                    gross += session.close[-1] / last_mark - 1.0
                    last_mark = session.close[-1]
                out.append(DayResult(session.day, gross, trips, cost_round_trips=cost_round_trips))
                continue
        if not session.has_observed(0, enter_minute + 1):
            out.append(DayResult(session.day, gross, trips, cost_round_trips=cost_round_trips))
            continue
        price = session.close[enter_minute - 1]
        if condition == "any":
            wanted = True
        elif condition == "down_day":
            wanted = price < session.open[0]
        else:
            wanted = internal_bar_strength(max(session.high[:enter_minute]), min(session.low[:enter_minute]),
                                           price) < strength_below
        if wanted:
            holding = True
            cost_round_trips += 0.5
            last_mark = session.open[enter_minute]
            if session.has_observed(MINUTES - 1):
                gross += session.close[-1] / last_mark - 1.0
                last_mark = session.close[-1]
        out.append(DayResult(session.day, gross, trips, cost_round_trips=cost_round_trips))
    return out


def _wilder_rsi(closes: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    gain = loss = 0.0
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        up, down = max(change, 0.0), max(-change, 0.0)
        if index <= period:
            gain += up / period
            loss += down / period
            if index < period:
                continue
        else:
            gain = (gain * (period - 1) + up) / period
            loss = (loss * (period - 1) + down) / period
        out[index] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def simulate_rsi_reversion_daily(bars: Sequence[DailyBar], *, period: int = 2, enter_below: float = 10.0,
                                 exit_above: float = 50.0, max_sessions: int = 5) -> list[DayResult]:
    """Read a completed close's RSI and enter/exit at the next available open."""
    rsi = _wilder_rsi([bar.close for bar in bars], period)
    wanted: list[bool] = []
    in_position = False
    held = 0
    for index, bar in enumerate(bars):
        value = rsi[index]
        if in_position:
            held += 1
            if (value is not None and value > exit_above) or held >= max_sessions:
                in_position = False
        elif value is not None and value < enter_below:
            in_position = True
            held = 0
        wanted.append(in_position)
    return _next_open_results(bars, wanted)


def simulate_down_streak_daily(bars: Sequence[DailyBar], *, streak: int = 3,
                               max_sessions: int = 3) -> list[DayResult]:
    """Decide after ``streak`` lower closes; execute entry/recovery exits at the next open."""
    wanted: list[bool] = []
    in_position = False
    held = 0
    run = 0
    for index, bar in enumerate(bars):
        change = bar.close - bars[index - 1].close if index else 0.0
        run = run + 1 if change < 0 else 0
        if in_position:
            held += 1
            if change > 0 or held >= max_sessions:
                in_position = False
        elif run >= streak:
            in_position = True
            held = 0
        wanted.append(in_position)
    return _next_open_results(bars, wanted)


def simulate_turn_of_month_daily(bars: Sequence[DailyBar], *, enter_days_before: int = 1,
                                 hold_days_after: int = 3,
                                 calendar: Sequence[date] | None = None) -> list[DayResult]:
    """Month-end calendar decisions, executed at the following available open.

    ``calendar`` must be an independently supplied exchange schedule, including
    the full relevant months. Deriving month end from future *available bars*
    would turn a truncated archive into a false month-end signal.
    """
    if calendar is None:
        raise ValueError("turn-of-month needs an explicit ex-ante exchange calendar")
    schedule = sorted(set(calendar))
    months: dict[tuple[int, int], list[date]] = {}
    for day in schedule:
        months.setdefault((day.year, day.month), []).append(day)
    positions = {day: (position, len(members) - 1 - position)
                 for members in months.values() for position, day in enumerate(members)}
    if any(bar.day not in positions for bar in bars):
        raise ValueError("exchange calendar does not cover every daily bar")
    # A subsequent calendar month establishes that the last month used by a
    # signal is not merely the end of a truncated schedule.
    complete_months = set(months) - {max(months)} if months else set()
    holding = False
    entry_month: tuple[int, int] | None = None
    wanted: list[bool] = []
    for bar in bars:
        position, from_end = positions[bar.day]
        month = (bar.day.year, bar.day.month)
        if holding and month != entry_month and position >= hold_days_after - 1:
            holding = False
        if month in complete_months and from_end == enter_days_before - 1:
            holding = True
            entry_month = month
        wanted.append(holding)
    return _next_open_results(bars, wanted)


# --------------------------------------------------------------------------
# Day-trader families: yesterday's levels and the session VWAP
# --------------------------------------------------------------------------

def simulate_prior_day_break(sessions: Sequence[Session], *, mode: str = "follow", narrow_days: int = 0,
                             sides: Sequence[str] = BOTH_SIDES, start_minute: int = 5,
                             last_entry_minute: int = 360, flat_minute: int = 388) -> list[DayResult]:
    """Trade the first one-minute close beyond yesterday's high or low, once per session.

    ``mode`` follows the break or fades it (the failed-breakout trade).
    ``narrow_days`` above zero trades only after a session whose range was the
    narrowest of that many sessions, the volatility-contraction setup. The
    first break uses up the session even when its side is not allowed.
    """
    if mode not in ("follow", "fade"):
        raise ValueError("mode must be follow or fade")
    highs = [max(session.high) for session in sessions]
    lows = [min(session.low) for session in sessions]
    out: list[DayResult] = []
    for index, session in enumerate(sessions):
        if previous_close(sessions, index) is None or not sessions[index - 1].has_observed(0, MINUTES):
            out.append(DayResult(session.day))
            continue
        level_high, level_low = highs[index - 1], lows[index - 1]
        if narrow_days > 0:
            if index < narrow_days:
                out.append(DayResult(session.day))
                continue
            if not all(sessions[i].has_observed(0, MINUTES) for i in range(index - narrow_days, index)):
                out.append(DayResult(session.day))
                continue
            widths = [highs[i] / lows[i] - 1.0 for i in range(index - narrow_days, index)]
            if widths[-1] > min(widths):
                out.append(DayResult(session.day))
                continue
        result = DayResult(session.day)
        if not _observed_exit(session, flat_minute):
            out.append(result)
            continue
        for minute in range(start_minute, last_entry_minute + 1):
            if not session.has_observed(0, minute + 1):
                continue
            price = session.close[minute - 1]
            if price > level_high:
                side = LONG
            elif price < level_low:
                side = SHORT
            else:
                continue
            if mode == "fade":
                side = -side
            if ("long" if side == LONG else "short") in sides:
                exit_minute, exit_price = _flat_exit(session, flat_minute)
                fill = Fill(side, minute, session.open[minute], exit_minute, exit_price)
                result = DayResult(session.day, fill.gross, 1, (fill,))
            break
        out.append(result)
    return out


def simulate_vwap_trend(sessions: Sequence[Session], *, step: int = 30, sides: Sequence[str] = BOTH_SIDES,
                        flat_minute: int = 388) -> list[DayResult]:
    """Long above the session VWAP, short below, re-checked every ``step`` minutes; flat at the end of day.

    Needs traded volume: a session without any is not traded, so data with no
    volume (an index proxy) produces no trades at all rather than a wrong VWAP.
    """
    out: list[DayResult] = []
    for session in sessions:
        if not math.fsum(session.volume) > 0 or not _observed_exit(session, flat_minute):
            out.append(DayResult(session.day))
            continue
        position = FLAT
        entry = 0.0
        entry_minute = 0
        fills: list[Fill] = []
        weighted = volume = 0.0
        done = 0
        for minute in range(step, flat_minute, step):
            if not session.has_observed(0, minute + 1):
                continue
            for bar in range(done, minute):
                size = session.volume[bar]
                weighted += (session.high[bar] + session.low[bar] + session.close[bar]) / 3.0 * size
                volume += size
            done = minute
            if not volume > 0:
                continue
            price, vwap = session.close[minute - 1], weighted / volume
            target = LONG if price > vwap else SHORT if price < vwap else position
            if ("long" if target == LONG else "short") not in sides and target != FLAT:
                target = FLAT
            if target == position:
                continue
            fill = session.open[minute]
            if position != FLAT:
                fills.append(Fill(position, entry_minute, entry, minute, fill))
            position, entry, entry_minute = target, fill, minute
        if position != FLAT:
            exit_minute, exit_price = _flat_exit(session, flat_minute)
            fills.append(Fill(position, entry_minute, entry, exit_minute, exit_price))
        out.append(DayResult(session.day, math.fsum(fill.gross for fill in fills), len(fills), tuple(fills)))
    return out


# --------------------------------------------------------------------------
# Options-trader families: implied volatility and the expiration calendar,
# traded in the underlying. Signals read at a close are acted on at the next
# open, because the volatility indices settle after the equity close.
# --------------------------------------------------------------------------

def _next_open_results(bars: Sequence[DailyBar], wanted: Sequence[bool]) -> list[DayResult]:
    """Daily results of being long from the open after ``wanted`` turns true to the open after it turns false."""
    out: list[DayResult] = []
    holding = False
    for index, bar in enumerate(bars):
        gross = 0.0
        trips = 0
        cost_round_trips = 0.0
        target = wanted[index - 1] if index else False
        if holding and not target:
            gross = bar.open / bars[index - 1].close - 1.0
            trips = 1
            cost_round_trips = 0.5
            holding = False
        elif holding:
            gross = bar.close / bars[index - 1].close - 1.0
        elif target:
            gross = bar.close / bar.open - 1.0
            holding = True
            cost_round_trips = 0.5
        out.append(DayResult(bar.day, gross, trips, cost_round_trips=cost_round_trips))
    return out


def simulate_vix_spike_daily(bars: Sequence[DailyBar], vix: Mapping[date, float], *, spike: float = 0.15,
                             average_days: int = 10, max_sessions: int = 5) -> list[DayResult]:
    """Buy after the VIX closes ``spike`` above its average of the prior ``average_days`` closes.

    The position is closed once the VIX is back below that average, or after
    ``max_sessions`` sessions.
    """
    wanted: list[bool] = []
    recent: list[float] = []
    holding = False
    held = 0
    for bar in bars:
        level = vix.get(bar.day)
        if level is None or len(recent) < average_days:
            if level is not None:
                recent.append(level)
            holding = False
            wanted.append(False)
            continue
        average = math.fsum(recent[-average_days:]) / average_days
        recent.append(level)
        if holding:
            held += 1
            if level < average or held >= max_sessions:
                holding = False
        elif level >= average * (1.0 + spike):
            holding = True
            held = 0
        wanted.append(holding)
    return _next_open_results(bars, wanted)


def simulate_vix_term_structure_daily(bars: Sequence[DailyBar], vix: Mapping[date, float],
                                      vix_3m: Mapping[date, float], *, mode: str = "buy_backwardation",
                                      threshold: float = 1.0, max_sessions: int = 10) -> list[DayResult]:
    """Trade the ratio of one-month to three-month implied volatility.

    ``buy_backwardation`` buys when the ratio closes above ``threshold``
    (acute stress) and sells when it is back below 1 or after ``max_sessions``.
    ``hold_contango`` is long whenever the ratio is below ``threshold``.
    """
    if mode not in ("buy_backwardation", "hold_contango"):
        raise ValueError("unknown term-structure mode")
    wanted: list[bool] = []
    holding = False
    held = 0
    for bar in bars:
        near, far = vix.get(bar.day), vix_3m.get(bar.day)
        if near is None or far is None or not far > 0:
            holding = False
            wanted.append(False)
            continue
        ratio = near / far
        if mode == "hold_contango":
            holding = ratio < threshold
        elif holding:
            held += 1
            if ratio < 1.0 or held >= max_sessions:
                holding = False
        elif ratio > threshold:
            holding = True
            held = 0
        wanted.append(holding)
    return _next_open_results(bars, wanted)


def is_expiration_week(day: date) -> bool:
    """Whether ``day`` is in the week of the monthly option expiration (the third Friday)."""
    friday = day.toordinal() + (4 - day.weekday())
    return day.weekday() < 5 and 15 <= date.fromordinal(friday).day <= 21


def simulate_expiration_week_daily(bars: Sequence[DailyBar], *, enter: str = "prior_friday",
                                   leave: str = "friday") -> list[DayResult]:
    """Calendar decisions at completed closes, executed at the next open.

    ``enter`` is the decision close before the week (``prior_friday``) or
    Monday's close; ``leave`` is Thursday's or Friday's decision close.
    """
    if enter not in ("prior_friday", "monday") or leave not in ("thursday", "friday"):
        raise ValueError("unknown expiration-week timing")
    last = 3 if leave == "thursday" else 4
    wanted = []
    for bar in bars:
        before_week = (enter == "prior_friday" and bar.day.weekday() == 4
                       and is_expiration_week(bar.day + timedelta(days=7)))
        within_week = is_expiration_week(bar.day) and bar.day.weekday() < last
        wanted.append(before_week or within_week)
    return _next_open_results(bars, wanted)


# --------------------------------------------------------------------------
# Diagnostics: effects that were tested and are reported, not traded
# --------------------------------------------------------------------------

def simulate_first_last(sessions: Sequence[Session], *, first_minutes: int = 30,
                        last_minutes: int = 30) -> list[DayResult]:
    """Gao, Han, Li and Zhou (2018): the first half hour's sign held over the last half hour."""
    out: list[DayResult] = []
    start = MINUTES - last_minutes
    for index, session in enumerate(sessions):
        if not session.has_observed(0, first_minutes) or not session.has_observed(start) or not _observed_exit(session, MINUTES):
            out.append(DayResult(session.day))
            continue
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
