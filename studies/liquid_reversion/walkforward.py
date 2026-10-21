"""Rolling walk-forward selection on the small grid, and the out-of-sample evaluation.

Each window selects, on its training sessions only, the grid point with the
highest session-clustered t-statistic of net bps among those with at least
``min_trades`` trades, then applies it unchanged to the following test
sessions. Test windows are disjoint, so the concatenated test trades form one
out-of-sample record.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist, fmean, median, stdev

from studies.liquid_reversion.power import (SessionSummary, observed_power, power_table, session_totals,
                                            summarise_sessions)
from studies.liquid_reversion.strategy import GRID, Params, Refusal, Trade

TRAIN_SESSIONS = 504
TEST_SESSIONS = 126
MIN_TRAIN_TRADES = 60
PERMUTATION_DRAWS = 10_000
PERMUTATION_SEED = 20261019
EULER_GAMMA = 0.5772156649015329
_NORMAL = NormalDist()


@dataclass(frozen=True, slots=True)
class Window:
    """One train block and the test block that follows it."""

    index: int
    train: tuple[date, ...]
    test: tuple[date, ...]


def rolling_windows(sessions: Sequence[date], *, train: int = TRAIN_SESSIONS, test: int = TEST_SESSIONS,
                    step: int | None = None) -> list[Window]:
    """Rolling windows over sorted session dates; the step defaults to the test length.

    The last test block may be shorter than ``test`` so that every session
    after the first training block is tested exactly once.
    """
    ordered = list(sessions)
    if ordered != sorted(set(ordered)):
        raise ValueError("sessions must be sorted and unique")
    if train <= 0 or test <= 0:
        raise ValueError("train and test lengths must be positive")
    step = test if step is None else step
    windows: list[Window] = []
    start = 0
    while start + train < len(ordered):
        windows.append(Window(len(windows), tuple(ordered[start:start + train]),
                              tuple(ordered[start + train:start + train + test])))
        start += step
    check_windows(windows)
    return windows


def check_windows(windows: Sequence[Window]) -> None:
    """Raise unless each test block follows its training block and test blocks are disjoint and in order."""
    for window in windows:
        if not window.train or not window.test:
            raise ValueError(f"window {window.index} has an empty block")
        if list(window.train) != sorted(set(window.train)) or list(window.test) != sorted(set(window.test)):
            raise ValueError(f"window {window.index} is not in chronological order")
        if window.train[-1] >= window.test[0]:
            raise ValueError(f"window {window.index}: training does not end before testing starts")
    for earlier, later in zip(windows, windows[1:]):
        if earlier.test[-1] >= later.test[0]:
            raise ValueError(f"test blocks {earlier.index} and {later.index} overlap or are out of order")
        if earlier.train[0] >= later.train[0]:
            raise ValueError(f"training blocks {earlier.index} and {later.index} are out of order")


def clustered_t(trades: Sequence[Trade], *, stress: float = 1.0) -> float | None:
    """t-statistic of mean net bps per trade with a session-clustered (CR1) standard error."""
    values = [trade.net_at(stress) for trade in trades]
    count = len(values)
    if count < 2:
        return None
    mean = math.fsum(values) / count
    residuals: dict[date, float] = {}
    for trade, value in zip(trades, values):
        residuals[trade.session] = residuals.get(trade.session, 0.0) + value - mean
    clusters = len(residuals)
    if clusters < 2:
        return None
    variance = clusters / (clusters - 1) * math.fsum(r * r for r in residuals.values()) / count ** 2
    return mean / math.sqrt(variance) if variance > 0 else None


def sign_flip_p(session_values: Sequence[float], *, draws: int = PERMUTATION_DRAWS,
                seed: int = PERMUTATION_SEED) -> float | None:
    """One-sided p-value for a positive mean session P&L, flipping the sign of whole sessions.

    Under the null each session's P&L is symmetric about zero. The p-value is
    ``(1 + hits) / (1 + draws)`` where a hit is a random flip whose total is at
    least the observed total.
    """
    values = [float(value) for value in session_values]
    if not values:
        return None
    observed = math.fsum(values)
    tolerance = 1e-9 * max(1.0, math.fsum(abs(value) for value in values))
    rng = random.Random(seed)
    hits = 0
    for _ in range(draws):
        total = 0.0
        for value in values:
            total += value if rng.random() < 0.5 else -value
        if total >= observed - tolerance:
            hits += 1
    return (hits + 1) / (draws + 1)


def daily_series(trades: Iterable[Trade], sessions: Sequence[date], *, stress: float = 1.0) -> list[float]:
    """Net bps per session over ``sessions``, with zero on sessions without a trade."""
    totals = session_totals(trades, stress=stress)
    return [totals.get(day, 0.0) for day in sessions]


def sharpe_moments(series: Sequence[float]) -> tuple[float, float, float] | None:
    """Per-session Sharpe ratio, skewness and (non-excess) kurtosis of a series."""
    if len(series) < 3:
        return None
    mean = fmean(series)
    deviation = stdev(series)
    if deviation <= 0:
        return None
    count = len(series)
    second = math.fsum((x - mean) ** 2 for x in series) / count
    third = math.fsum((x - mean) ** 3 for x in series) / count
    fourth = math.fsum((x - mean) ** 4 for x in series) / count
    return mean / deviation, third / second ** 1.5, fourth / second ** 2


def deflated_sharpe(sharpe: float, *, observations: int, skewness: float, kurtosis: float,
                    trial_sharpes: Sequence[float]) -> float | None:
    """Bailey and Lopez de Prado (2014): probability that the selected Sharpe beats chance.

    The benchmark is the expected maximum Sharpe ratio of ``N`` unskilled
    trials with the observed variance of trial Sharpe ratios. Trials here are
    correlated grid points, so ``N`` overstates the number of independent
    trials and the result is conservative.
    """
    trials = list(trial_sharpes)
    if len(trials) < 2 or observations < 2:
        return None
    count = len(trials)
    spread = stdev(trials)
    expected_max = spread * ((1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / count)
                             + EULER_GAMMA * _NORMAL.inv_cdf(1 - 1 / (count * math.e)))
    denominator = 1 - skewness * sharpe + (kurtosis - 1) / 4 * sharpe ** 2
    if denominator <= 0:
        return None
    return _NORMAL.cdf((sharpe - expected_max) * math.sqrt(observations - 1) / math.sqrt(denominator))


@dataclass(frozen=True, slots=True)
class Selection:
    """The grid point chosen on one training block, with what it did there."""

    window: Window
    params: Params | None
    train_t: float | None
    train_trades: int
    eligible: int
    train_sharpe: float | None
    deflated_sharpe: float | None


def select_params(trades_by_params: Mapping[Params, Sequence[Trade]], window: Window, *,
                  grid: Sequence[Params] = GRID, min_trades: int = MIN_TRAIN_TRADES) -> Selection:
    """Highest training t among grid points with at least ``min_trades`` trades; ties keep grid order."""
    train = frozenset(window.train)
    best: tuple[Params, float, int] | None = None
    eligible = 0
    sharpes: dict[Params, float] = {}
    moments: dict[Params, tuple[float, float, float]] = {}
    for params in grid:
        sample = [trade for trade in trades_by_params.get(params, ()) if trade.session in train]
        found = sharpe_moments(daily_series(sample, window.train))
        if found is not None:
            sharpes[params], moments[params] = found[0], found
        if len(sample) < min_trades:
            continue
        t_stat = clustered_t(sample)
        if t_stat is None:
            continue
        eligible += 1
        if best is None or t_stat > best[1]:
            best = (params, t_stat, len(sample))
    if best is None:
        return Selection(window, None, None, 0, eligible, None, None)
    chosen = best[0]
    deflated = None
    if chosen in moments:
        sharpe, skewness, kurtosis = moments[chosen]
        deflated = deflated_sharpe(sharpe, observations=len(window.train), skewness=skewness,
                                   kurtosis=kurtosis, trial_sharpes=list(sharpes.values()))
    return Selection(window, chosen, best[1], best[2], eligible, sharpes.get(chosen), deflated)


@dataclass(frozen=True, slots=True)
class WalkForward:
    """Every window's selection and the concatenated out-of-sample record."""

    windows: tuple[Window, ...]
    selections: tuple[Selection, ...]
    trades: tuple[Trade, ...]
    refusals: tuple[Refusal, ...]
    test_sessions: tuple[date, ...]


def walk_forward(trades_by_params: Mapping[Params, Sequence[Trade]],
                 refusals_by_params: Mapping[Params, Sequence[Refusal]], sessions: Sequence[date], *,
                 grid: Sequence[Params] = GRID, train: int = TRAIN_SESSIONS, test: int = TEST_SESSIONS,
                 min_trades: int = MIN_TRAIN_TRADES) -> WalkForward:
    """Select on each training block and apply the choice, unchanged, to its test block."""
    windows = rolling_windows(sessions, train=train, test=test)
    selections: list[Selection] = []
    trades: list[Trade] = []
    refusals: list[Refusal] = []
    for window in windows:
        selection = select_params(trades_by_params, window, grid=grid, min_trades=min_trades)
        selections.append(selection)
        if selection.params is None:
            continue
        tested = frozenset(window.test)
        trades.extend(t for t in trades_by_params.get(selection.params, ()) if t.session in tested)
        refusals.extend(r for r in refusals_by_params.get(selection.params, ()) if r.session in tested)
    return WalkForward(tuple(windows), tuple(selections), tuple(trades), tuple(refusals),
                       tuple(day for window in windows for day in window.test))


def _rounded(value: float | None, places: int = 3) -> float | None:
    return None if value is None else round(value, places)


def _group(trades: Sequence[Trade], key: Callable[[Trade], str]) -> dict[str, dict[str, object]]:
    groups: dict[str, list[Trade]] = {}
    for trade in trades:
        groups.setdefault(key(trade), []).append(trade)
    out: dict[str, dict[str, object]] = {}
    for name in sorted(groups):
        members = groups[name]
        nets = [trade.net_bps for trade in members]
        out[name] = {"trades": len(members), "sessions": len({trade.session for trade in members}),
                     "net_bps_mean": _rounded(fmean(nets)),
                     "gross_bps_mean": _rounded(fmean(trade.gross_bps for trade in members)),
                     "win_rate": _rounded(sum(net > 0 for net in nets) / len(nets)),
                     "clustered_t": _rounded(clustered_t(members))}
    return out


def decision(session_mean: float | None, sign_flip: float | None,
             session_mean_2x: float | None) -> dict[str, object]:
    """The preregistered rule: session mean above zero, sign-flip p < 0.05, and above zero at 2x cost.

    ``inputs`` keeps the unrounded numbers the checks used, so the published
    verdict can be re-derived exactly.  The runner adds whether the rule
    applies at all (an out-of-sample run of the preregistered design).
    """
    checks = {"session_mean_above_zero": session_mean is not None and session_mean > 0,
              "sign_flip_p_below_0_05": sign_flip is not None and sign_flip < 0.05,
              "session_mean_at_2x_cost_above_zero": session_mean_2x is not None and session_mean_2x > 0}
    return {"checks": checks, "deploy_to_paper": all(checks.values()),
            "inputs": {"session_mean": session_mean, "sign_flip_p_one_sided": sign_flip,
                       "session_mean_at_2x_cost": session_mean_2x}}


def evaluate(trades: Sequence[Trade], *, sessions: Sequence[date], refusals: Sequence[Refusal] = (),
             draws: int = PERMUTATION_DRAWS, seed: int = PERMUTATION_SEED) -> dict[str, object]:
    """The headline numbers, cost stress at 1x and 2x, power and the decision rule."""
    trades = sorted(trades, key=lambda trade: (trade.session, trade.symbol))
    nets = [trade.net_bps for trade in trades]
    summary = summarise_sessions(trades)
    stressed: dict[float, tuple[SessionSummary, float | None]] = {}
    for stress in (1.0, 2.0):
        sessions_at = summarise_sessions(trades, stress=stress)
        p_value = sign_flip_p(list(session_totals(trades, stress=stress).values()), draws=draws, seed=seed)
        stressed[stress] = (sessions_at, p_value)
    p_value = stressed[1.0][1]
    out: dict[str, object] = {
        "trades": len(trades), "sessions_available": len(sessions), "sessions_traded": summary.count,
        "trades_per_traded_session": _rounded(summary.trades_per_session),
        "net_bps_per_trade": {"mean": _rounded(fmean(nets)) if nets else None,
                              "median": _rounded(median(nets)) if nets else None},
        "gross_bps_per_trade_mean": _rounded(fmean(t.gross_bps for t in trades)) if trades else None,
        "cost_bps_per_trade_mean": _rounded(fmean(t.cost_bps for t in trades)) if trades else None,
        "session_level": {"mean": _rounded(summary.mean), "sd": _rounded(summary.sd),
                          "count": summary.count},
        "clustered_t": _rounded(clustered_t(trades)),
        "sign_flip_p_one_sided": _rounded(p_value, 4),
        "permutation": {"draws": draws, "seed": seed, "unit": "session", "alternative": "mean above zero"},
        "win_rate": _rounded(sum(net > 0 for net in nets) / len(nets)) if nets else None,
        "exit_mix": dict(sorted(Counter(trade.reason for trade in trades).items())),
        "refused_entries": dict(sorted(Counter(refusal.reason for refusal in refusals).items())),
        "sides": dict(sorted(Counter(trade.side for trade in trades).items())),
        "by_symbol": _group(trades, lambda trade: trade.symbol),
        "by_year": _group(trades, lambda trade: str(trade.session.year)),
        "cost_stress": {
            f"{stress:g}x": {
                "net_bps_per_trade_mean": _rounded(fmean(t.net_at(stress) for t in trades)) if trades else None,
                "session_mean": _rounded(summary_at.mean),
                "clustered_t": _rounded(clustered_t(trades, stress=stress)),
                "sign_flip_p_one_sided": _rounded(p_at, 4)}
            for stress, (summary_at, p_at) in stressed.items()},
    }
    power = observed_power(summary, sessions_available=len(sessions),
                           mean_trade_bps=fmean(nets) if nets else None)
    out["power"] = {key: (_rounded(value) if isinstance(value, float) else value)
                    for key, value in power.items()}
    # The table is built from the unrounded measurements; only its outputs are
    # rounded. Rounding the sd, trade rate and traded share first moved the
    # published session counts (15,614 instead of 15,606 on the 1-minute smoke run).
    table = None
    share = power.get("traded_share")
    if "sd_bps" in power and isinstance(share, float) and 0 < share <= 1:
        table = power_table(sd_bps=power["sd_bps"], trades_per_session=power["trades_per_session"],
                            traded_share=share,
                            cost_bps_per_trade=fmean(t.cost_bps for t in trades) if trades else None)
    out["power_table"] = _round_floats(table, 4)
    out["decision"] = decision(summary.mean, p_value, stressed[2.0][0].mean)
    return out


def _round_floats(value: object, places: int) -> object:
    if isinstance(value, float):
        return value if math.isinf(value) else round(value, places)
    if isinstance(value, Mapping):
        return {key: _round_floats(item, places) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round_floats(item, places) for item in value]
    return value
