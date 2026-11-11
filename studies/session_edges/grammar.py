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
FLAT_MINUTE = 388        # intraday legs leave at 15:58, as the forward runner does
NO_TARGET = 1000.0
Simulator = Callable[[Sequence[Any]], list[DayResult]]
Value = float | int | str


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
    build: Callable[[Mapping[str, Value]], Simulator]
    thesis: str
    runner_rule: str | None = None      # the forward runner's rule that trades it, if any

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
    Family(
        "noise_band_momentum", MINUTE, True,
        {"lookback_sessions": Parameter((14, 7, 30), 5, 60, integer=True),
         "decision_step_minutes": Parameter((30, 60)),
         "band_multiple": Parameter((1.0, 1.25, 1.5), 0.5, 2.5),
         "sides": Parameter(("both", "long"))},
        lambda p: (lambda sessions: rules.simulate_noise_band(sessions, noise_band_rules(p))),
        "Index-level intraday momentum outside a time-of-day noise band, driven by hedging flows "
        "(Zarattini, Aziz and Barbon, 2024; Baltussen et al., 2021).",
        runner_rule="noise_band_momentum"),
    Family(
        "midday_trend", MINUTE, True,
        {"decision_minute": Parameter((180, 120, 240)),
         "threshold": Parameter((1.0, 1.5), 0.5, 2.5),
         "sides": Parameter(("both", "long"))},
        lambda p: (lambda sessions: rules.simulate_noise_band(sessions, midday_trend_rules(p))),
        "A single midday check of the same noise band: a session already trending tends to close in "
        "that direction.",
        runner_rule="noise_band_momentum"),
    Family(
        "close_weakness_reversion", MINUTE, True, _REVERSION_PARAMETERS,
        lambda p: (lambda sessions: rules.simulate_close_reversion(sessions, close_reversion_rules(p))),
        "A close near the day's low in a broad equity ETF tends to be followed by a recovery "
        "(Pagonidis, 2013).",
        runner_rule="close_weakness_reversion"),
    Family(
        "opening_range_breakout", MINUTE, False,
        {"minutes": Parameter((5, 15, 30)),
         "target_r": Parameter((10.0, NO_TARGET)),
         "sides": Parameter(("both", "long"))},
        lambda p: (lambda sessions: rules.simulate_opening_range(sessions, opening_range_rules(p))),
        "The direction of the first candle persists for the session (Zarattini and Aziz, 2023). "
        "Needs a resting stop order, which the runner does not send; shadow only."),
    Family(
        "last_hour", MINUTE, False,
        {"mode": Parameter(("follow", "fade")),
         "threshold": Parameter((0.0, 1.0), 0.0, 2.5)},
        lambda p: (lambda sessions: rules.simulate_last_hour(
            sessions, mode=str(p["mode"]), threshold=float(p["threshold"]), flat_minute=FLAT_MINUTE)),
        "The day's move so far continues or reverses into the close (after Gao, Han, Li and Zhou, 2018)."),
    Family(
        "gap_open", MINUTE, False,
        {"mode": Parameter(("fade", "follow")),
         "threshold": Parameter((1.0, 0.5, 1.5), 0.0, 3.0),
         "exit_minute": Parameter((60, FLAT_MINUTE))},
        lambda p: (lambda sessions: rules.simulate_gap(
            sessions, mode=str(p["mode"]), threshold=float(p["threshold"]), exit_minute=int(p["exit_minute"]))),
        "A large overnight gap is faded or followed after the open."),
    Family(
        "overnight_hold", MINUTE, False,
        {"condition": Parameter(("any", "weak_close", "down_day"))},
        lambda p: (lambda sessions: rules.simulate_overnight_hold(sessions, condition=str(p["condition"]))),
        "Equity returns accrue overnight (Lou, Polk and Skouras, 2019), more so after a weak day."),
    Family(
        "close_weakness_reversion_daily", DAILY, True, _REVERSION_PARAMETERS,
        lambda p: (lambda bars: rules.simulate_close_reversion_daily(bars, close_reversion_rules(p))),
        "The close-weakness rule on daily bars, decided at the close itself; traded by the runner's "
        "15:50 form of the same rule.",
        runner_rule="close_weakness_reversion"),
    Family(
        "rsi_reversion_daily", DAILY, False,
        {"period": Parameter((2, 3)),
         "enter_below": Parameter((10.0, 5.0, 15.0), 1.0, 40.0),
         "exit_above": Parameter((50.0, 70.0), 30.0, 95.0)},
        lambda p: (lambda bars: rules.simulate_rsi_reversion_daily(
            bars, period=int(p["period"]), enter_below=float(p["enter_below"]),
            exit_above=float(p["exit_above"]))),
        "Short-horizon oversold readings in index ETFs mean-revert (Connors and Alvarez, 2008)."),
    Family(
        "down_streak_daily", DAILY, False,
        {"streak": Parameter((3, 2, 4)),
         "max_sessions": Parameter((3, 1, 5))},
        lambda p: (lambda bars: rules.simulate_down_streak_daily(
            bars, streak=int(p["streak"]), max_sessions=int(p["max_sessions"]))),
        "Several consecutive lower closes in an index ETF tend to be followed by a bounce."),
    Family(
        "turn_of_month_daily", DAILY, False,
        {"enter_days_before": Parameter((1, 2)),
         "hold_days_after": Parameter((3, 4))},
        lambda p: (lambda bars: rules.simulate_turn_of_month_daily(
            bars, enter_days_before=int(p["enter_days_before"]), hold_days_after=int(p["hold_days_after"]))),
        "Month-end and month-start flows lift equity indices around the turn of the month."),
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
    canonical = json.dumps(normalise(spec), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def simulator(spec: Mapping[str, Any]) -> Simulator:
    canonical = normalise(spec)
    return FAMILIES[canonical["family"]].build(canonical["params"])


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
                            "thesis": family.thesis, "parameters": parameters}
    return out


__all__ = ["DAILY", "FAMILIES", "FLAT_MINUTE", "MINUTE", "MINUTES", "DailyBar", "Family", "Parameter",
           "Session", "SpecError", "describe", "enumerate_family", "neighbours", "normalise", "simulator",
           "spec_id"]
