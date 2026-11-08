"""Inference for daily strategy returns.

* Newey-West (1987) HAC standard errors for a mean and for regression alpha;
  the default lag is ``floor(4 (n/100)^(2/9))``.
* Probabilistic and deflated Sharpe ratios (Bailey and Lopez de Prado 2012,
  2014): the chance the true Sharpe beats the best of ``N`` null trials.
* Probability of backtest overfitting by combinatorially symmetric
  cross-validation (Bailey, Borwein, Lopez de Prado and Zhu 2017).
* Hansen's (2005) superior predictive ability test and White's (2000) reality
  check, on the Politis and Romano (1994) stationary bootstrap.
* Holm (1979) and Benjamini-Yekutieli (2001) adjustments.

Everything is numpy only; the normal distribution comes from the stdlib.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Sequence

import numpy as np

NORMAL = NormalDist()
EULER_GAMMA = 0.5772156649015329
TRADING_DAYS = 252.0


def nw_lags(n: int) -> int:
    return max(1, int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))))


def hac_mean(x: np.ndarray, lags: int | None = None) -> tuple[float, float, float]:
    """Mean, its Newey-West standard error, and the t statistic."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3:
        return float("nan"), float("nan"), float("nan")
    lags = nw_lags(n) if lags is None else lags
    u = x - x.mean()
    s = u @ u / n
    for lag in range(1, min(lags, n - 1) + 1):
        s += 2.0 * (1.0 - lag / (lags + 1.0)) * (u[lag:] @ u[:-lag]) / n
    se = math.sqrt(max(s, 0.0) / n)
    mean = float(x.mean())
    return mean, se, (mean / se if se > 0 else float("nan"))


def hac_alpha(y: np.ndarray, b: np.ndarray, lags: int | None = None) -> dict[str, float]:
    """OLS ``y = alpha + beta b + e`` with a Newey-West covariance."""
    y, b = np.asarray(y, float), np.asarray(b, float)
    n = len(y)
    if n < 10 or np.var(b) <= 0:
        mean, se, t = hac_mean(y, lags)
        return {"alpha": mean, "beta": 0.0, "alpha_se": se, "alpha_t": t, "r2": 0.0}
    X = np.column_stack([np.ones(n), b])
    xtx_inv = np.linalg.inv(X.T @ X)
    coef = xtx_inv @ X.T @ y
    e = y - X @ coef
    g = X * e[:, None]
    lags = nw_lags(n) if lags is None else lags
    s = g.T @ g / n
    for lag in range(1, min(lags, n - 1) + 1):
        gamma = g[lag:].T @ g[:-lag] / n
        s += (1.0 - lag / (lags + 1.0)) * (gamma + gamma.T)
    cov = n * xtx_inv @ s @ xtx_inv
    se = math.sqrt(max(cov[0, 0], 0.0))
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return {"alpha": float(coef[0]), "beta": float(coef[1]), "alpha_se": se,
            "alpha_t": float(coef[0] / se) if se > 0 else float("nan"),
            "r2": float(1.0 - (e @ e) / ss_tot) if ss_tot > 0 else 0.0}


def moments(x: np.ndarray) -> tuple[float, float, float, float]:
    """Mean, standard deviation, skewness and (non-excess) kurtosis."""
    x = np.asarray(x, float)
    mean, sd = float(x.mean()), float(x.std(ddof=1)) if len(x) > 1 else 0.0
    if sd <= 0:
        return mean, sd, 0.0, 3.0
    z = (x - mean) / sd
    return mean, sd, float(np.mean(z ** 3)), float(np.mean(z ** 4))


def sharpe(x: np.ndarray) -> float:
    mean, sd, _, _ = moments(x)
    return mean / sd if sd > 0 else 0.0


def probabilistic_sharpe(sr: float, benchmark_sr: float, n: int, skew: float,
                         kurt: float) -> float:
    """P(true per-period Sharpe > ``benchmark_sr``), non-normal returns."""
    denominator = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if n < 3 or denominator <= 0:
        return float("nan")
    return NORMAL.cdf((sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denominator))


def expected_max_sharpe(sharpe_variance: float, trials: float) -> float:
    """Expected maximum per-period Sharpe of ``trials`` independent null trials."""
    if trials <= 1 or sharpe_variance <= 0:
        return 0.0
    return math.sqrt(sharpe_variance) * (
        (1.0 - EULER_GAMMA) * NORMAL.inv_cdf(1.0 - 1.0 / trials)
        + EULER_GAMMA * NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e)))


def deflated_sharpe(x: np.ndarray, *, trials: float, sharpe_variance: float) -> float:
    mean, sd, skew, kurt = moments(x)
    sr = mean / sd if sd > 0 else 0.0
    return probabilistic_sharpe(sr, expected_max_sharpe(sharpe_variance, trials), len(x),
                                skew, kurt)


def effective_trials(matrix: np.ndarray) -> float:
    """``rho + (1 - rho) N`` with the mean pairwise correlation ``rho``."""
    k = matrix.shape[1]
    if k < 2:
        return 1.0
    sd = matrix.std(axis=0)
    keep = sd > 0
    corr = np.corrcoef(matrix[:, keep].T)
    m = corr.shape[0]
    rho = (corr.sum() - m) / (m * (m - 1)) if m > 1 else 0.0
    rho = float(np.clip(rho, 0.0, 1.0))
    return rho + (1.0 - rho) * k


def max_drawdown(returns: np.ndarray) -> float:
    wealth = np.cumprod(1.0 + np.asarray(returns, float))
    peak = np.maximum.accumulate(np.concatenate([[1.0], wealth]))[1:]
    return float(np.min(wealth / peak - 1.0)) if len(wealth) else 0.0


def one_sided_p(t: float) -> float:
    return 1.0 - NORMAL.cdf(t) if math.isfinite(t) else 1.0


def holm(p: Sequence[float]) -> list[float]:
    p = np.asarray(p, float)
    order = np.argsort(p)
    m = len(p)
    adjusted = np.empty(m)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p[i]))
        adjusted[i] = running
    return adjusted.tolist()


def benjamini_yekutieli(p: Sequence[float]) -> list[float]:
    p = np.asarray(p, float)
    m = len(p)
    if m == 0:
        return []
    c = float(np.sum(1.0 / np.arange(1, m + 1)))
    order = np.argsort(p)[::-1]
    adjusted = np.empty(m)
    running = 1.0
    for position, i in enumerate(order):
        rank = m - position
        running = min(running, p[i] * m * c / rank)
        adjusted[i] = min(1.0, running)
    return adjusted.tolist()


# --------------------------------------------------------------------------
# stationary bootstrap, SPA and reality check


def stationary_bootstrap_means(d: np.ndarray, draws: int, mean_block: float,
                               seed: int) -> np.ndarray:
    """Bootstrap means of every column of ``d`` (n x K), circular blocks."""
    d = np.asarray(d, float)
    n, k = d.shape
    rng = np.random.default_rng(seed)
    doubled = np.vstack([d, d])
    csum = np.vstack([np.zeros((1, k)), np.cumsum(doubled, axis=0)])
    p = 1.0 / mean_block
    out = np.empty((draws, k))
    for b in range(draws):
        lengths = []
        total = 0
        while total < n:
            chunk = rng.geometric(p, size=max(8, int(1.5 * (n - total) * p) + 8))
            lengths.extend(chunk.tolist())
            total = sum(lengths)
        lengths = np.minimum(np.array(lengths), n)
        ends = np.cumsum(lengths)
        cut = int(np.searchsorted(ends, n))
        lengths = lengths[:cut + 1].copy()
        lengths[-1] -= int(ends[cut] - n)
        starts = rng.integers(0, n, size=len(lengths))
        sums = (csum[starts + lengths] - csum[starts]).sum(axis=0)
        out[b] = sums / n
    return out


@dataclass
class SPAResult:
    statistic: float
    p_consistent: float
    p_upper: float
    p_reality_check: float
    best: int
    draws: int
    mean_block: float


def spa_test(d: np.ndarray, *, draws: int = 2000, mean_block: float = 20.0,
             seed: int = 20261108) -> SPAResult:
    """Null: no column of ``d`` (performance minus benchmark) has a positive mean."""
    d = np.asarray(d, float)
    n, _ = d.shape
    means = d.mean(axis=0)
    boot = stationary_bootstrap_means(d, draws, mean_block, seed)
    omega = np.sqrt(n) * boot.std(axis=0)
    omega = np.where(omega > 0, omega, np.inf)
    studentised = np.sqrt(n) * means / omega
    stat = max(0.0, float(np.max(studentised)))
    threshold = -math.sqrt(2.0 * math.log(max(math.log(n), 1.0 + 1e-9)))
    mu_c = np.where(studentised <= threshold, means, 0.0)
    centred = boot - means[None, :]
    t_c = np.maximum(0.0, np.max(np.sqrt(n) * (centred + mu_c) / omega, axis=1))
    t_u = np.maximum(0.0, np.max(np.sqrt(n) * centred / omega, axis=1))
    rc_stat = float(np.max(np.sqrt(n) * means))
    rc = np.max(np.sqrt(n) * centred, axis=1)
    return SPAResult(stat, float(np.mean(t_c >= stat)), float(np.mean(t_u >= stat)),
                     float(np.mean(rc >= rc_stat)), int(np.argmax(studentised)), draws, mean_block)


# --------------------------------------------------------------------------
# probability of backtest overfitting (CSCV)


@dataclass
class PBOResult:
    pbo: float
    combinations: int
    blocks: int
    median_logit: float
    oos_loss_share: float       # share of splits where the IS winner loses OOS


def pbo_cscv(matrix: np.ndarray, *, blocks: int = 16) -> PBOResult:
    """Rank the in-sample Sharpe winner out of sample over all half splits."""
    m = np.asarray(matrix, float)
    n, k = m.shape
    size = n // blocks
    m = m[: size * blocks]
    shaped = m.reshape(blocks, size, k)
    sums, squares = shaped.sum(axis=1), (shaped ** 2).sum(axis=1)
    combos = np.array(list(itertools.combinations(range(blocks), blocks // 2)))
    full_s, full_q = sums.sum(axis=0), squares.sum(axis=0)
    is_s = sums[combos].sum(axis=1)
    is_q = squares[combos].sum(axis=1)
    count = size * (blocks // 2)

    def ratio(s: np.ndarray, q: np.ndarray) -> np.ndarray:
        mean = s / count
        var = np.maximum(q / count - mean ** 2, 1e-18)
        return mean / np.sqrt(var)

    is_sr = ratio(is_s, is_q)
    oos_sr = ratio(full_s[None, :] - is_s, full_q[None, :] - is_q)
    winner = np.argmax(is_sr, axis=1)
    oos_of_winner = oos_sr[np.arange(len(combos)), winner]
    rank = (oos_sr < oos_of_winner[:, None]).sum(axis=1) + 1
    omega = rank / (k + 1.0)
    logit = np.log(omega / (1.0 - omega))
    return PBOResult(float(np.mean(logit <= 0)), len(combos), blocks, float(np.median(logit)),
                     float(np.mean(oos_of_winner <= 0)))


def yearly(dates: Sequence, x: np.ndarray, mask: np.ndarray) -> dict[int, float]:
    out: dict[int, float] = {}
    for d, value, ok in zip(dates, x, mask):
        if ok:
            out[d.year] = (1.0 + out.get(d.year, 0.0)) * (1.0 + value) - 1.0
    return out
