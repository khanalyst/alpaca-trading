"""Normal-approximation power analysis for a session-level mean, and session P&L helpers.

The session is the unit of inference: trades on the same day share the same
market path, so they are summed into one session P&L before any test.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist, fmean, stdev
from typing import Protocol

_NORMAL = NormalDist()


class TradeLike(Protocol):
    session: date
    gross_bps: float
    cost_bps: float


def _critical(alpha: float, power: float, one_sided: bool) -> float:
    if not 0 < alpha < 1 or not 0 < power < 1:
        raise ValueError("alpha and power must lie strictly between 0 and 1")
    tail = alpha if one_sided else alpha / 2.0
    return _NORMAL.inv_cdf(1.0 - tail) + _NORMAL.inv_cdf(power)


def required_sessions(effect_bps: float, sd_bps: float, alpha: float = 0.05, power: float = 0.80,
                      one_sided: bool = True) -> int:
    """Sessions needed to detect a true mean of ``effect_bps`` per session.

    ``ceil(((z_(1-alpha) + z_power) x sd / effect) ** 2)``, with ``alpha/2`` when two-sided.
    """
    if effect_bps <= 0 or sd_bps <= 0:
        raise ValueError("effect and sd must be positive")
    return math.ceil((_critical(alpha, power, one_sided) * sd_bps / effect_bps) ** 2)


def minimum_detectable_effect(n: int, sd_bps: float, alpha: float = 0.05, power: float = 0.80,
                              one_sided: bool = True) -> float:
    """Smallest true session mean detectable with ``n`` sessions: ``(z_a + z_b) x sd / sqrt(n)``."""
    if n <= 0 or sd_bps <= 0:
        raise ValueError("n and sd must be positive")
    return _critical(alpha, power, one_sided) * sd_bps / math.sqrt(n)


def rule_required_sessions(effect_bps: float, cost_bps: float, sd_bps: float, trades_per_session: float,
                           alpha: float = 0.05, power: float = 0.80) -> int | None:
    """Traded sessions for the whole preregistered rule to pass with ``power``.

    Besides a one-sided test of the net session mean, the rule needs the
    session mean to stay above zero at twice the cost.  That is the same as a
    net mean per trade above the mean cost per trade, so passing needs the
    net session mean above ``max(z_alpha x se, cost x m)``.  With ``m`` trades
    per traded session this takes both ``n >= ((z_a + z_b) x sd / (e x m))**2``
    and ``n >= (z_b x sd / ((e - cost) x m))**2``.  A true net effect at or
    below the cost never passes, at any sample size: None.
    """
    if sd_bps <= 0 or trades_per_session <= 0 or cost_bps < 0:
        raise ValueError("sd and trades per session must be positive and cost non-negative")
    if effect_bps <= cost_bps:
        return None
    z_power = _NORMAL.inv_cdf(power)
    test = (_critical(alpha, power, True) * sd_bps / (effect_bps * trades_per_session)) ** 2
    margin = (max(z_power, 0.0) * sd_bps / ((effect_bps - cost_bps) * trades_per_session)) ** 2
    return math.ceil(max(test, margin))


def rule_minimum_detectable_effect(n: int, sd_bps: float, trades_per_session: float, cost_bps: float,
                                   alpha: float = 0.05, power: float = 0.80) -> float:
    """Smallest true net effect per trade the whole rule detects with ``n`` traded sessions.

    ``max(z_alpha x se, cost) + z_power x se``, with ``se = sd / sqrt(n) / m`` per trade.
    """
    if n <= 0 or sd_bps <= 0 or trades_per_session <= 0 or cost_bps < 0:
        raise ValueError("n, sd and trades per session must be positive and cost non-negative")
    _critical(alpha, power, True)
    se = sd_bps / math.sqrt(n) / trades_per_session
    return max(_NORMAL.inv_cdf(1.0 - alpha) * se, cost_bps) + _NORMAL.inv_cdf(power) * se


def design_effect(cluster_size: float, icc: float) -> float:
    """Variance inflation of a mean over clusters of correlated trades: ``1 + (m - 1) x icc``."""
    if cluster_size < 1 or not -1 <= icc <= 1:
        raise ValueError("cluster size must be at least 1 and icc within [-1, 1]")
    return 1.0 + (cluster_size - 1.0) * icc


def session_totals(trades: Iterable[TradeLike], *, stress: float = 1.0) -> dict[date, float]:
    """Net bps summed per session (one position's notional per trade), in date order."""
    totals: dict[date, float] = {}
    for trade in trades:
        totals[trade.session] = totals.get(trade.session, 0.0) + trade.gross_bps - stress * trade.cost_bps
    return dict(sorted(totals.items()))


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Mean, sd and count of session P&L over the sessions that traded."""

    mean: float | None
    sd: float | None
    count: int
    trades: int

    @property
    def trades_per_session(self) -> float | None:
        return self.trades / self.count if self.count else None


def summarise_sessions(trades: Iterable[TradeLike], *, stress: float = 1.0) -> SessionSummary:
    """Summarise the session P&L of a trade list."""
    trades = list(trades)
    totals = list(session_totals(trades, stress=stress).values())
    return SessionSummary(mean=fmean(totals) if totals else None,
                          sd=stdev(totals) if len(totals) > 1 else None,
                          count=len(totals), trades=len(trades))


def power_table(*, sd_bps: float, trades_per_session: float, traded_share: float,
                effects_bps: Iterable[float] = (0.5, 1.0, 2.0, 3.0), sessions_for_mde: int = 2500,
                alpha: float = 0.05, power: float = 0.80, one_sided: bool = True,
                cost_bps_per_trade: float | None = None) -> dict[str, object]:
    """Sessions needed per true per-trade effect, and the MDE for a given number of sessions.

    A per-trade effect ``e`` is a session effect of ``e x trades_per_session``.
    Required traded sessions are converted to calendar sessions by dividing by
    the share of sessions that trade.  The ``traded_sessions`` and MDE columns
    are for the significance test alone.  With ``cost_bps_per_trade`` the
    ``rule_*`` columns add the 2x-cost check of the preregistered rule, which
    is what a decision run must pass.
    """
    if not 0 < traded_share <= 1 or trades_per_session <= 0:
        raise ValueError("traded share must lie in (0, 1] and trades per session be positive")
    rows: list[dict[str, float | int | None]] = []
    for effect in effects_bps:
        traded = required_sessions(effect * trades_per_session, sd_bps, alpha, power, one_sided)
        row: dict[str, float | int | None] = {
            "effect_bps_per_trade": effect, "traded_sessions": traded,
            "calendar_sessions": math.ceil(traded / traded_share)}
        if cost_bps_per_trade is not None:
            rule = rule_required_sessions(effect, cost_bps_per_trade, sd_bps, trades_per_session,
                                          alpha, power)
            row["rule_traded_sessions"] = rule
            row["rule_calendar_sessions"] = None if rule is None else math.ceil(rule / traded_share)
        rows.append(row)
    traded_available = max(1, math.floor(sessions_for_mde * traded_share))
    mde_session = minimum_detectable_effect(traded_available, sd_bps, alpha, power, one_sided)
    mde: dict[str, float | int] = {"calendar_sessions": sessions_for_mde, "traded_sessions": traded_available,
                                   "session_bps": mde_session, "per_trade_bps": mde_session / trades_per_session}
    if cost_bps_per_trade is not None:
        mde["rule_per_trade_bps"] = rule_minimum_detectable_effect(
            traded_available, sd_bps, trades_per_session, cost_bps_per_trade, alpha, power)
    return {"alpha": alpha, "power": power, "one_sided": one_sided, "sd_bps": sd_bps,
            "trades_per_session": trades_per_session, "traded_share": traded_share,
            "cost_bps_per_trade": cost_bps_per_trade, "rows": rows, "mde": mde}


def observed_power(summary: SessionSummary, *, sessions_available: int, mean_trade_bps: float | None,
                   alpha: float = 0.05, power: float = 0.80) -> Mapping[str, object]:
    """Sessions needed to detect the observed effect, and the MDE for the sessions available."""
    if summary.sd is None or not summary.count or summary.trades_per_session is None:
        return {"note": "too few traded sessions to estimate a session-level sd"}
    traded_share = summary.count / sessions_available if sessions_available else 1.0
    out: dict[str, object] = {
        "sd_bps": summary.sd, "trades_per_session": summary.trades_per_session,
        "traded_share": traded_share, "sessions_available": sessions_available,
        "mde_per_trade_bps": minimum_detectable_effect(summary.count, summary.sd, alpha, power)
        / summary.trades_per_session}
    if mean_trade_bps is not None and mean_trade_bps > 0:
        traded = required_sessions(mean_trade_bps * summary.trades_per_session, summary.sd, alpha, power)
        out["observed_effect_bps_per_trade"] = mean_trade_bps
        out["traded_sessions_needed"] = traded
        out["calendar_sessions_needed"] = math.ceil(traded / traded_share) if traded_share else None
    else:
        out["observed_effect_bps_per_trade"] = mean_trade_bps
        out["traded_sessions_needed"] = None
        out["note"] = "the observed effect is not positive, so no sample size detects it"
    return out
