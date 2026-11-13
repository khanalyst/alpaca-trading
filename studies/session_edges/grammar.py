"""The hypothesis grammar of the discovery engine.

A hypothesis is data, never code: ``{"family": ..., "symbol": ..., "params": {...}}``.
Each family names one session-level effect with a published or structural
reason to exist, a bounded set of parameters and a small search grid. The
engine may combine families, parameters and instruments freely; it cannot
invent a signal, and neither can a model proposing hypotheses.

``executable`` says whether the forward runner can trade the family as it is
simulated here. A research-only family can be confirmed and recorded, and
needs its own live decision function before it can be traded.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from studies.session_edges import strategies as rules
from studies.session_edges.data import MINUTES, DailyBar, Session
from studies.session_edges.strategies import DayResult

MINUTE, DAILY = "minute", "daily"
SIMULATION_VERSION = "session-causal-v2"
FLAT_MINUTE = 388        # intraday legs leave at 15:58, as the forward runner does
NO_TARGET = 1000.0
Simulator = Callable[[Sequence[Any]], list[DayResult]]
Value = float | int | str
Context = Mapping[str, Mapping[Any, float] | Sequence[Any]]  # daily series or an explicit session calendar
DAY_TRADER, SWING_TRADER, OPTIONS_TRADER = "day trader", "swing trader", "options trader"


class SpecError(ValueError):
    """A hypothesis is outside the grammar."""


@dataclass(frozen=True, slots=True)
class Parameter:
    """One tunable: a numeric range or a closed set of choices, and the values the grid searches."""

    grid: tuple[Value, ...]
    low: float | None = None
    high: float | None = None
    integer: bool = False

    def validate(self, name: str, value: Any) -> Value:
        if self.low is None:
            if isinstance(value, bool) or value not in self.grid:
                raise SpecError(f"{name} must be one of {list(self.grid)}")
            return self.grid[self.grid.index(value)]     # 10 and 10.0 are one choice with one id
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SpecError(f"{name} must be a number")
        if not self.low <= float(value) <= float(self.high):
            raise SpecError(f"{name} must be between {self.low} and {self.high}")
        if self.integer:
            if float(value) != int(value):
                raise SpecError(f"{name} must be a whole number")
            return int(value)
        return float(value)


@dataclass(frozen=True, slots=True)
class Family:
    """One effect: its data, its parameters, how to simulate it, and where it comes from."""

    name: str
    data: str
    executable: bool
    parameters: Mapping[str, Parameter]
    build: Callable[[Mapping[str, Value], Context], Simulator]
    thesis: str
    runner_rule: str | None = None      # the forward runner's rule that trades it, if any
    desk: str = DAY_TRADER              # whose playbook the signal comes from
    needs: tuple[str, ...] = ()         # context series the signal reads, e.g. ("VIX",)
    signal: str = ""                    # the signal in one line, for the catalogue
    volume: bool = False                # whether the signal needs traded volume (not on an index proxy)

    def defaults(self) -> dict[str, Value]:
        return {name: parameter.grid[0] for name, parameter in self.parameters.items()}


def _sides(value: Value) -> tuple[str, ...]:
    return ("long", "short") if value == "both" else (str(value),)


def noise_band_rules(params: Mapping[str, Value]) -> rules.NoiseBandRules:
    step = int(params["decision_step_minutes"])
    return rules.NoiseBandRules(lookback_sessions=int(params["lookback_sessions"]),
                                decision_step_minutes=step, first_decision_minute=step,
                                last_decision_minute=360, band_multiple=float(params["band_multiple"]),
                                sides=_sides(params["sides"]), flat_minute=FLAT_MINUTE)


def midday_trend_rules(params: Mapping[str, Value]) -> rules.NoiseBandRules:
    minute = int(params["decision_minute"])
    return rules.NoiseBandRules(lookback_sessions=14, decision_step_minutes=30, first_decision_minute=minute,
                                last_decision_minute=minute, band_multiple=float(params["threshold"]),
                                sides=_sides(params["sides"]), flat_minute=FLAT_MINUTE)


def close_reversion_rules(params: Mapping[str, Value]) -> rules.CloseReversionRules:
    return rules.CloseReversionRules(decision_minute=380, enter_below=float(params["enter_below"]),
                                     exit_above=float(params["exit_above"]),
                                     max_sessions=int(params["max_sessions"]))


def opening_range_rules(params: Mapping[str, Value]) -> rules.OpeningRangeRules:
    return rules.OpeningRangeRules(minutes=int(params["minutes"]), target_r=float(params["target_r"]),
                                   sides=_sides(params["sides"]))


_REVERSION_PARAMETERS = {
    "enter_below": Parameter((0.20, 0.10, 0.15, 0.25, 0.30), 0.02, 0.45),
    "exit_above": Parameter((0.50, 0.60), 0.30, 0.95),
    "max_sessions": Parameter((5, 1, 3), 1, 10, integer=True),
}

FAMILIES: dict[str, Family] = {family.name: family for family in (
    # ---- day-trader signals: one-minute sessions, flat by the close -------
    Family(
        "noise_band_momentum", MINUTE, True,
        {"lookback_sessions": Parameter((14, 7, 30), 5, 60, integer=True),
         "decision_step_minutes": Parameter((30, 60)),
         "band_multiple": Parameter((1.0, 1.25, 1.5), 0.5, 2.5),
         "sides": Parameter(("both", "long"))},
        lambda p, c: (lambda sessions: rules.simulate_noise_band(sessions, noise_band_rules(p))),
        "Dealers hedging index options and leveraged funds rebalancing must trade with the day's move, "
        "so a move larger than usual for that time of day tends to continue "
        "(Zarattini, Aziz and Barbon, 2024; Baltussen et al., 2021).",
        runner_rule="noise_band_momentum", desk=DAY_TRADER,
        signal="price outside the open plus or minus its usual move for this time of day, checked on a clock"),
    Family(
        "midday_trend", MINUTE, True,
        {"decision_minute": Parameter((180, 120, 240)),
         "threshold": Parameter((1.0, 1.5), 0.5, 2.5),
         "sides": Parameter(("both", "long"))},
        lambda p, c: (lambda sessions: rules.simulate_noise_band(sessions, midday_trend_rules(p))),
        "A session that is already an outlier by midday is a trend day, and trend days tend to close "
        "near their extreme as hedgers and late participants chase.",
        runner_rule="noise_band_momentum", desk=DAY_TRADER,
        signal="one check at 11:30, 12:30 or 13:30 of the same noise band, held to the end of day"),
    Family(
        "opening_range_breakout", MINUTE, False,
        {"minutes": Parameter((5, 15, 30)),
         "target_r": Parameter((10.0, NO_TARGET)),
         "sides": Parameter(("both", "long"))},
        lambda p, c: (lambda sessions: rules.simulate_opening_range(sessions, opening_range_rules(p))),
        "Overnight orders are worked at the open, so the first candle shows the day's imbalance and "
        "its direction persists (Zarattini and Aziz, 2023). Needs a resting stop order, which the "
        "runner does not send; recorded only.",
        desk=DAY_TRADER, signal="direction of the first 5, 15 or 30 minutes; stop at the far side of that range"),
    Family(
        "prior_day_break", MINUTE, False,
        {"mode": Parameter(("follow", "fade")),
         "narrow_days": Parameter((0, 4, 7)),
         "sides": Parameter(("both", "long"))},
        lambda p, c: (lambda sessions: rules.simulate_prior_day_break(
            sessions, mode=str(p["mode"]), narrow_days=int(p["narrow_days"]), sides=_sides(p["sides"]),
            flat_minute=FLAT_MINUTE)),
        "Yesterday's high and low are where stops and breakout orders sit. After a narrow day the "
        "break tends to run (volatility contraction then expansion, Crabel, 1990); otherwise it often "
        "fails, which is the fade.",
        desk=DAY_TRADER, signal="first one-minute close beyond yesterday's high or low, optionally only after a narrow-range day"),
    Family(
        "gap_open", MINUTE, False,
        {"mode": Parameter(("fade", "follow")),
         "threshold": Parameter((1.0, 0.5, 1.5), 0.0, 3.0),
         "exit_minute": Parameter((60, FLAT_MINUTE))},
        lambda p, c: (lambda sessions: rules.simulate_gap(
            sessions, mode=str(p["mode"]), threshold=float(p["threshold"]), exit_minute=int(p["exit_minute"]))),
        "A large overnight gap either exhausts the overnight move (fade to fill) or marks new "
        "information that keeps being priced (gap and go).",
        desk=DAY_TRADER, signal="overnight gap larger than usual, traded from 09:31 for an hour or to the end of day"),
    Family(
        "vwap_trend", MINUTE, False,
        {"step": Parameter((30, 15, 60)),
         "sides": Parameter(("both", "long"))},
        lambda p, c: (lambda sessions: rules.simulate_vwap_trend(
            sessions, step=int(p["step"]), sides=_sides(p["sides"]), flat_minute=FLAT_MINUTE)),
        "Institutions benchmark to VWAP, so price above it means buyers are paying up and in control "
        "(Zarattini and Aziz, 2023). Needs traded volume, so it is searched on SIP bars only.",
        desk=DAY_TRADER, volume=True,
        signal="price above or below the session VWAP, re-checked every 15, 30 or 60 minutes"),
    Family(
        "last_hour", MINUTE, False,
        {"mode": Parameter(("follow", "fade")),
         "threshold": Parameter((0.0, 1.0), 0.0, 2.5)},
        lambda p, c: (lambda sessions: rules.simulate_last_hour(
            sessions, mode=str(p["mode"]), threshold=float(p["threshold"]), flat_minute=FLAT_MINUTE)),
        "End-of-day hedging and fund flows push in the direction of the day's move, or the move is "
        "exhausted and reverses (after Gao, Han, Li and Zhou, 2018).",
        desk=DAY_TRADER, signal="the day's move at 15:00, followed or faded into the close"),
    # ---- swing signals: hold overnight for one to five sessions -----------
    Family(
        "close_weakness_reversion", MINUTE, True, _REVERSION_PARAMETERS,
        lambda p, c: (lambda sessions: rules.simulate_close_reversion(sessions, close_reversion_rules(p))),
        "Late-day selling by funds that must cut risk pushes a broad equity ETF below fair value; "
        "the pressure lifts overnight and the next sessions recover it (Pagonidis, 2013).",
        runner_rule="close_weakness_reversion", desk=SWING_TRADER,
        signal="price in the bottom part of the day's range at 15:50; exit once it is back in the top part"),
    Family(
        "close_weakness_reversion_daily", DAILY, False, _REVERSION_PARAMETERS,
        lambda p, c: (lambda bars: rules.simulate_close_reversion_daily(bars, close_reversion_rules(p))),
        "The same economic premise, using a completed daily bar and executing at the next open. "
        "This is a distinct research strategy, not evidence for the runner's 15:50 rule.",
        desk=SWING_TRADER,
        signal="completed close in the bottom of the daily range; buy next open, exit next open after recovery"),
    Family(
        "rsi_reversion_daily", DAILY, False,
        {"period": Parameter((2, 3)),
         "enter_below": Parameter((10.0, 5.0, 15.0), 1.0, 40.0),
         "exit_above": Parameter((50.0, 70.0), 30.0, 95.0)},
        lambda p, c: (lambda bars: rules.simulate_rsi_reversion_daily(
            bars, period=int(p["period"]), enter_below=float(p["enter_below"]),
            exit_above=float(p["exit_above"]))),
        "Two or three days of concentrated selling in an index is usually liquidity demand, not "
        "news, and is paid back (Connors and Alvarez, 2008).",
        desk=SWING_TRADER, signal="completed 2- or 3-day RSI oversold; enter/exit at the next open"),
    Family(
        "down_streak_daily", DAILY, False,
        {"streak": Parameter((3, 2, 4)),
         "max_sessions": Parameter((3, 1, 5))},
        lambda p, c: (lambda bars: rules.simulate_down_streak_daily(
            bars, streak=int(p["streak"]), max_sessions=int(p["max_sessions"]))),
        "Consecutive lower closes in an index exhaust short-term sellers; the first bounce is the trade.",
        desk=SWING_TRADER, signal="2, 3 or 4 lower closes; buy next open, exit next open after a higher close"),
    Family(
        "overnight_hold", MINUTE, False,
        {"condition": Parameter(("any", "weak_close", "down_day"))},
        lambda p, c: (lambda sessions: rules.simulate_overnight_hold(sessions, condition=str(p["condition"]))),
        "Equity returns accrue overnight, when risk is held and few can trade (Lou, Polk and Skouras, "
        "2019), more so after a weak day.",
        desk=SWING_TRADER, signal="buy at 15:58, sell at 09:31, every day or only after a weak day"),
    Family(
        "turn_of_month_daily", DAILY, False,
        {"enter_days_before": Parameter((1, 2)),
         "hold_days_after": Parameter((3, 4))},
        lambda p, c: (lambda bars: rules.simulate_turn_of_month_daily(
            bars, enter_days_before=int(p["enter_days_before"]), hold_days_after=int(p["hold_days_after"]),
            calendar=tuple(c["NYSE_CALENDAR"]))),
        "Salary, pension and fund inflows arrive around the month end and are invested at once.",
        desk=SWING_TRADER, needs=("NYSE_CALENDAR",),
        signal="exchange-calendar month-end close decision; execute next open, leave next open after the holding window"),
    # ---- options-trader signals, traded in the underlying ------------------
    Family(
        "vix_spike_daily", DAILY, False,
        {"spike": Parameter((0.15, 0.10, 0.20, 0.30), 0.05, 1.0),
         "max_sessions": Parameter((5, 3, 10))},
        lambda p, c: (lambda bars: rules.simulate_vix_spike_daily(
            bars, c["VIX"], spike=float(p["spike"]), max_sessions=int(p["max_sessions"]))),
        "A jump in implied volatility is hedgers paying up for protection; when the fear fades, "
        "volatility sellers return and the index recovers. The options desk sells the spike; this "
        "is the same trade in shares.",
        desk=OPTIONS_TRADER, needs=("VIX",),
        signal="VIX closes 10 to 30% above its 10-day average; buy the next open, exit when VIX is back below it"),
    Family(
        "vix_term_structure_daily", DAILY, False,
        {"mode": Parameter(("buy_backwardation", "hold_contango")),
         "threshold": Parameter((1.0, 0.95, 1.05), 0.8, 1.3)},
        lambda p, c: (lambda bars: rules.simulate_vix_term_structure_daily(
            bars, c["VIX"], c["VIX3M"], mode=str(p["mode"]), threshold=float(p["threshold"]))),
        "One-month volatility above three-month volatility means protection is being bought in a "
        "hurry, which marks capitulation; the normal upward slope marks a market that pays for "
        "carrying risk.",
        desk=OPTIONS_TRADER, needs=("VIX", "VIX3M"),
        signal="ratio of 1-month to 3-month implied volatility: buy inversion, or hold only while it slopes up"),
    Family(
        "expiration_week_daily", DAILY, False,
        {"enter": Parameter(("prior_friday", "monday")),
         "leave": Parameter(("friday", "thursday"))},
        lambda p, c: (lambda bars: rules.simulate_expiration_week_daily(
            bars, enter=str(p["enter"]), leave=str(p["leave"]))),
        "In the week of the monthly expiration, dealers' hedges decay with the options and their "
        "buy-backs support the index (Stivers and Sun, 2013).",
        desk=OPTIONS_TRADER, signal="calendar decision at a completed close; enter/exit next open around expiration week"),
)}


def normalise(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a hypothesis and return its canonical form (defaults filled, values typed)."""
    if not isinstance(spec, Mapping):
        raise SpecError("a hypothesis must be an object")
    unknown = set(spec) - {"family", "symbol", "params", "thesis"}
    if unknown:
        raise SpecError(f"unknown fields: {sorted(unknown)}")
    family = FAMILIES.get(str(spec.get("family")))
    if family is None:
        raise SpecError(f"unknown family {spec.get('family')!r}; choose one of {sorted(FAMILIES)}")
    symbol = str(spec.get("symbol") or "").strip()
    if not symbol or len(symbol) > 40:
        raise SpecError("symbol is required")
    given = spec.get("params") or {}
    if not isinstance(given, Mapping):
        raise SpecError("params must be an object")
    extra = set(given) - set(family.parameters)
    if extra:
        raise SpecError(f"{family.name} has no parameters {sorted(extra)}")
    params = family.defaults()
    for name, value in given.items():
        params[name] = family.parameters[name].validate(name, value)
    return {"family": family.name, "symbol": symbol, "params": {name: params[name] for name in sorted(params)}}


def spec_id(spec: Mapping[str, Any]) -> str:
    """Content hash of the canonical hypothesis: the same idea always has the same id."""
    canonical = json.dumps({"simulation_version": SIMULATION_VERSION, "spec": normalise(spec)},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def simulator(spec: Mapping[str, Any], context: Context | None = None) -> Simulator:
    canonical = normalise(spec)
    family = FAMILIES[canonical["family"]]
    missing = [name for name in family.needs if name not in (context or {})]
    if missing:
        raise SpecError(f"{family.name} needs the context series {missing}")
    return family.build(canonical["params"], context or {})


def enumerate_family(family: Family, symbol: str) -> Iterator[dict[str, Any]]:
    """Every grid point of one family on one instrument."""
    names = sorted(family.parameters)
    for values in itertools.product(*(family.parameters[name].grid for name in names)):
        yield {"family": family.name, "symbol": symbol, "params": dict(zip(names, values))}


def neighbours(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Hypotheses that differ from ``spec`` in exactly one parameter, by one grid step.

    A value that is not on the grid (a proposal) is compared with the grid
    values on either side of it.
    """
    canonical = normalise(spec)
    family = FAMILIES[canonical["family"]]
    out: list[dict[str, Any]] = []
    for name, parameter in family.parameters.items():
        current = canonical["params"][name]
        if parameter.low is None:
            choices = [value for value in parameter.grid if value != current]
        else:
            ordered = sorted(parameter.grid)
            below = [value for value in ordered if value < current]
            above = [value for value in ordered if value > current]
            choices = below[-1:] + above[:1]
        for value in choices:
            params = dict(canonical["params"])
            params[name] = value
            out.append({"family": family.name, "symbol": canonical["symbol"], "params": params})
    return out


def describe() -> dict[str, Any]:
    """The grammar as plain data, for documentation and for a model that proposes hypotheses."""
    out: dict[str, Any] = {}
    for family in FAMILIES.values():
        parameters = {}
        for name, parameter in family.parameters.items():
            if parameter.low is None:
                parameters[name] = {"choices": list(parameter.grid)}
            else:
                parameters[name] = {"min": parameter.low, "max": parameter.high,
                                    "integer": parameter.integer, "grid": list(parameter.grid)}
        out[family.name] = {"data": family.data, "tradable_by_runner": family.executable,
                            "simulation_version": SIMULATION_VERSION,
                            "desk": family.desk, "signal": family.signal, "needs": list(family.needs),
                            "needs_volume": family.volume,
                            "thesis": family.thesis, "parameters": parameters,
                            "variants": variant_count(family)}
    return out


def variant_count(family: Family) -> int:
    """How many grid variants one family has on one instrument."""
    count = 1
    for parameter in family.parameters.values():
        count *= len(parameter.grid)
    return count


def catalogue_markdown() -> str:
    """Every edge the engine searches: whose signal it is, its premise and its variants."""
    lines = ["| edge | desk | data | signal | premise | variants per instrument | what varies | runner can trade it |",
             "|---|---|---|---|---|---|---|---|"]
    for family in FAMILIES.values():
        varies = "; ".join(f"{name}: {', '.join(str(value) for value in parameter.grid)}"
                           for name, parameter in sorted(family.parameters.items()))
        needs = f" (needs {', '.join(family.needs)})" if family.needs else ""
        needs += " (needs volume)" if family.volume else ""
        lines.append(f"| `{family.name}` | {family.desk} | {family.data}{needs} | {family.signal} | "
                     f"{family.thesis} | {variant_count(family)} | {varies} | "
                     f"{'yes' if family.executable else 'no'} |")
    total = sum(variant_count(family) for family in FAMILIES.values())
    lines += ["", f"{len(FAMILIES)} edges, {total} variants per instrument when every data type is available."]
    return "\n".join(lines)


def catalogue_document() -> str:
    """The text of ``EDGES.md``: the catalogue, with how to read it."""
    return "\n".join([
        "# Edge catalogue", "",
        "Generated by `python -m studies.session_edges.grammar`; a test keeps this file in step with the code.",
        "",
        "An **edge** is a signal: one reason a price should move, turned into a rule. A **variant** is the",
        "same signal with different values (thresholds, windows, sides). The discovery engine tests every",
        "variant of every edge on every instrument it is given, so the number of hypotheses is edges times",
        "variants times instruments. The desk is whose playbook the signal comes from; every edge is traded",
        "in shares of the instrument, including the options-desk ones.", "",
        catalogue_markdown(), ""])


__all__ = ["DAILY", "DAY_TRADER", "FAMILIES", "FLAT_MINUTE", "MINUTE", "MINUTES", "OPTIONS_TRADER", "SIMULATION_VERSION",
           "SWING_TRADER", "Context", "DailyBar", "Family", "Parameter", "Session", "SpecError",
           "catalogue_document", "catalogue_markdown", "describe", "enumerate_family", "neighbours", "normalise", "simulator",
           "spec_id", "variant_count"]


if __name__ == "__main__":
    print(catalogue_document(), end="")
