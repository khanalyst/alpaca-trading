"""Session-clustered statistics for a daily net-return series.

The unit of evidence is the session, never the trade: trades inside one
session share that session's market move, so counting them as independent
overstates the sample. Returns are arithmetic (not compounded) fractions of
the position's notional; drawdowns are therefore additive.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import date
from statistics import NormalDist, fmean, stdev

from studies.liquid_reversion.walkforward import deflated_sharpe, sharpe_moments, sign_flip_p
from studies.session_edges.strategies import DayResult

SESSIONS_PER_YEAR = 252
NEWEY_WEST_LAGS = 5
BOOTSTRAP_BLOCK = 20
BOOTSTRAP_DRAWS = 2_000
BOOTSTRAP_SEED = 20261110
SIGN_FLIP_DRAWS = 5_000
_NORMAL = NormalDist()


def net_series(results: Sequence[DayResult], cost_bps: float) -> list[float]:
    """Per-session net return: gross minus ``cost_bps`` per round trip."""
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("round-trip cost must be finite and nonnegative")
    charge = cost_bps / 10_000.0
    out: list[float] = []
    for result in results:
        explicit = getattr(result, "cost_round_trips", None)
        charged = result.round_trips if explicit is None else explicit
        if not math.isfinite(result.gross) or not math.isfinite(charged) or charged < 0:
            raise ValueError("daily returns and executed-side costs must be finite and valid")
        out.append(result.gross - charged * charge)
    return out


def newey_west_t(series: Sequence[float], lags: int = NEWEY_WEST_LAGS) -> float | None:
    """t-statistic of the mean with a Bartlett-kernel (Newey-West) variance."""
    count = len(series)
    if count < 20:
        return None
    mean = fmean(series)
    centred = [value - mean for value in series]
    variance = math.fsum(value * value for value in centred) / count
    for lag in range(1, min(lags, count - 1) + 1):
        weight = 1.0 - lag / (lags + 1.0)
        covariance = math.fsum(centred[i] * centred[i - lag] for i in range(lag, count)) / count
        variance += 2.0 * weight * covariance
    if not variance > 0:
        return None
    return mean / math.sqrt(variance / count)


def one_sided_p(t_value: float | None) -> float:
    """Normal-approximation upper-tail p, with unavailable evidence charged p=1."""
    if t_value is None or not math.isfinite(t_value):
        return 1.0
    return 0.5 * math.erfc(t_value / math.sqrt(2.0))


def regression_alpha(net: Sequence[float], benchmark: Sequence[float], *,
                     lags: int = NEWEY_WEST_LAGS) -> dict[str, float | None]:
    """OLS intercept and HAC t, including uncertainty in the fitted exposure.

    The intercept's influence is ``residual * (1 - mean_x*(x-mean_x)/var_x)``.
    Applying the mean's Bartlett HAC estimator to this influence gives the
    sandwich intercept standard error. Treating a fitted beta as known would
    understate uncertainty. Degenerate designs/residuals fail closed.
    """
    missing = {"beta": None, "alpha_bps_per_session": None, "alpha_t": None,
               "alpha_p_one_sided": 1.0}
    count = len(net)
    if count < 20 or count != len(benchmark):
        return missing
    if not all(math.isfinite(value) for value in (*net, *benchmark)):
        return missing
    mean_y, mean_x = fmean(net), fmean(benchmark)
    variance_x = math.fsum((x - mean_x) ** 2 for x in benchmark) / count
    if variance_x <= 0:
        return missing
    beta = math.fsum((x - mean_x) * (y - mean_y) for y, x in zip(net, benchmark)) / (count * variance_x)
    alpha = mean_y - beta * mean_x
    residual = [y - alpha - beta * x for y, x in zip(net, benchmark)]
    if max(abs(value) for value in residual) < 1e-12:
        t_value = None
    else:
        influence = [error * (1.0 - mean_x * (x - mean_x) / variance_x)
                     for error, x in zip(residual, benchmark)]
        t_value = newey_west_t([alpha + value for value in influence], lags=lags)
    return {"beta": beta, "alpha_bps_per_session": alpha * 1e4, "alpha_t": t_value,
            "alpha_p_one_sided": one_sided_p(t_value)}


def max_drawdown(series: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the cumulative (additive) return; zero or negative."""
    peak = 0.0
    total = 0.0
    worst = 0.0
    for value in series:
        total += value
        peak = max(peak, total)
        worst = min(worst, total - peak)
    return worst


def annualised_sharpe(series: Sequence[float]) -> float | None:
    if len(series) < 3:
        return None
    deviation = stdev(series)
    if not deviation > 0:
        return None
    return fmean(series) / deviation * math.sqrt(SESSIONS_PER_YEAR)


def block_bootstrap_sharpe(series: Sequence[float], *, block: int = BOOTSTRAP_BLOCK,
                           draws: int = BOOTSTRAP_DRAWS, seed: int = BOOTSTRAP_SEED
                           ) -> dict[str, float] | None:
    """Moving-block bootstrap of the annualised Sharpe ratio: 95% interval and P(Sharpe <= 0)."""
    count = len(series)
    if count < 4 * block:
        return None
    rng = random.Random(seed)
    blocks = count // block
    values = list(series)
    samples: list[float] = []
    for _ in range(draws):
        resampled: list[float] = []
        for _ in range(blocks):
            start = rng.randrange(0, count - block + 1)
            resampled.extend(values[start:start + block])
        sharpe = annualised_sharpe(resampled)
        if sharpe is not None:
            samples.append(sharpe)
    if not samples:
        return None
    samples.sort()

    def quantile(q: float) -> float:
        return samples[min(len(samples) - 1, max(0, int(q * len(samples))))]

    return {"low": quantile(0.025), "high": quantile(0.975),
            "p_not_positive": sum(1 for sample in samples if sample <= 0) / len(samples)}


def sessions_for_power(mean: float, deviation: float, *, alpha: float = 0.05, power: float = 0.80) -> int | None:
    """Sessions needed to detect a per-session ``mean`` (one-sided) given its standard deviation."""
    if not (mean > 0 and deviation > 0):
        return None
    z = _NORMAL.inv_cdf(1 - alpha) + _NORMAL.inv_cdf(power)
    return math.ceil((z * deviation / mean) ** 2)


def rolling_sum_quantiles(series: Sequence[float], window: int,
                          quantiles: Sequence[float] = (0.01, 0.05, 0.50)) -> dict[str, float] | None:
    """Quantiles of the cumulative return over every ``window`` consecutive sessions, in percent.

    These are the bands a forward run of ``window`` sessions is compared with:
    a result below the low quantiles is unusual for the rule as it was tested.
    """
    count = len(series)
    if window <= 0 or count < 2 * window:
        return None
    total = math.fsum(series[:window])
    sums = [total]
    for index in range(window, count):
        total += series[index] - series[index - window]
        sums.append(total)
    sums.sort()
    return {f"p{round(q * 100):02d}": sums[min(len(sums) - 1, int(q * len(sums)))] * 100 for q in quantiles}


def summarise(days: Sequence[date], net: Sequence[float], round_trips: Sequence[int] | None = None,
              *, gross: Sequence[float] | None = None) -> dict[str, object] | None:
    """Headline statistics of one daily series; ``None`` with fewer than 20 sessions."""
    count = len(net)
    if count < 20 or len(days) != count:
        return None
    mean = fmean(net)
    deviation = stdev(net)
    trips = sum(round_trips) if round_trips is not None else None
    active = [value for value in net if value != 0]
    out: dict[str, object] = {
        "sessions": count,
        "first": days[0].isoformat(),
        "last": days[-1].isoformat(),
        "net_bps_per_session": mean * 1e4,
        "annual_return_pct": mean * SESSIONS_PER_YEAR * 100,
        "annual_volatility_pct": deviation * math.sqrt(SESSIONS_PER_YEAR) * 100,
        "sharpe": annualised_sharpe(net),
        "t_newey_west": newey_west_t(net),
        "max_drawdown_pct": max_drawdown(net) * 100,
        "share_of_active_sessions_positive": (sum(1 for value in active if value > 0) / len(active)
                                              if active else None),
    }
    if trips is not None:
        out["round_trips"] = trips
        out["round_trips_per_session"] = trips / count
        if gross is not None and trips:
            out["gross_bps_per_round_trip"] = math.fsum(gross) / trips * 1e4
    return out


def by_year(days: Sequence[date], net: Sequence[float]) -> dict[int, float]:
    """Sum of net returns per calendar year, in percent of notional."""
    totals: dict[int, float] = {}
    for day, value in zip(days, net):
        totals[day.year] = totals.get(day.year, 0.0) + value
    return {year: totals[year] * 100 for year in sorted(totals)}


def evaluate(results: Sequence[DayResult], *, cost_bps: float, split: date,
             recent: date | None = None, trials: Sequence[float] = (),
             draws: int = SIGN_FLIP_DRAWS) -> dict[str, object]:
    """Full evidence block for one rule on one instrument.

    ``split`` separates the design period (before) from the period the rule
    was never looked at when it was chosen (on or after). ``trials`` are the
    per-session Sharpe ratios of every configuration examined alongside this
    one; with two or more the deflated Sharpe ratio is reported.
    """
    days = [result.day for result in results]
    net = net_series(results, cost_bps)
    gross = [result.gross for result in results]
    trips = [result.round_trips for result in results]

    def window(start: date | None, end: date | None) -> dict[str, object] | None:
        index = [i for i, day in enumerate(days)
                 if (start is None or day >= start) and (end is None or day < end)]
        if not index:
            return None
        return summarise([days[i] for i in index], [net[i] for i in index], [trips[i] for i in index],
                         gross=[gross[i] for i in index])

    years = by_year(days, net)
    out: dict[str, object] = {
        "cost_bps_per_round_trip": cost_bps,
        "all": window(None, None),
        "design": window(None, split),
        "holdout": window(split, None),
        "by_year_pct": years,
        "positive_years": sum(1 for value in years.values() if value > 0),
        "years": len(years),
        "sign_flip_p": sign_flip_p(net, draws=draws) if net else None,
        "bootstrap_sharpe": block_bootstrap_sharpe(net),
    }
    if recent is not None:
        out["recent"] = window(recent, None)
    moments = sharpe_moments(net)
    if moments is not None:
        sharpe, skewness, kurtosis = moments
        out["sessions_to_confirm_forward"] = sessions_for_power(fmean(net), stdev(net))
        if len(trials) >= 2:
            out["deflated_sharpe"] = deflated_sharpe(sharpe, observations=len(net), skewness=skewness,
                                                     kurtosis=kurtosis, trial_sharpes=trials)
            out["trials"] = len(trials)
    return out


def per_session_sharpe(results: Sequence[DayResult], cost_bps: float) -> float | None:
    """Per-session (not annualised) Sharpe ratio, the unit the deflated Sharpe ratio uses."""
    moments = sharpe_moments(net_series(results, cost_bps))
    return None if moments is None else moments[0]


def combine(series: Sequence[tuple[Sequence[date], Sequence[float], float]]) -> tuple[list[date], list[float]]:
    """Weighted sum of daily series on the sessions they all share."""
    if not series:
        return [], []
    shared = set(series[0][0])
    for days, _, _ in series[1:]:
        shared &= set(days)
    ordered = sorted(shared)
    lookup = [dict(zip(days, values)) for days, values, _ in series]
    weights = [weight for _, _, weight in series]
    return ordered, [math.fsum(weight * table[day] for table, weight in zip(lookup, weights))
                     for day in ordered]


def correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    mean_left, mean_right = fmean(left), fmean(right)
    cross = math.fsum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    spread_left = math.fsum((a - mean_left) ** 2 for a in left)
    spread_right = math.fsum((b - mean_right) ** 2 for b in right)
    if not (spread_left > 0 and spread_right > 0):
        return None
    return cross / math.sqrt(spread_left * spread_right)
