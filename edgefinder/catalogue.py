"""The hypothesis catalogue: every family, its mechanism and its parameter grid.

A family turns a :class:`~edgefinder.data.Panel` into two decision arrays over
the candidate's symbols, shape ``(T, S)``:

* ``open_w[t]``  weights held during session ``t``, set at the opening auction
  of ``t``;
* ``close_w[t]`` weights held from the close of ``t`` to the open of ``t+1``,
  set at the closing auction of ``t``.

Causality rule, enforced by tests that perturb the future: both decisions on
day ``t`` may use closes, highs, lows and volumes through ``t-1`` only, plus
the exchange calendar (which is published in advance).  A market-on-close
order must reach Alpaca before 15:50 New York time, before the close of ``t``
exists, so the close of ``t`` is never available to a decision made on ``t``.

``ready[t]`` is False until the family's look-back is filled; statistics only
use ready days.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from edgefinder.data import Panel, UNIVERSE

EQUITY_INDEX = ("SPY", "QQQ", "IWM")


@dataclass(frozen=True)
class Candidate:
    family: str
    symbols: tuple[str, ...]
    params: tuple[tuple[str, Any], ...] = ()

    @property
    def id(self) -> str:
        sym = "+".join(self.symbols) if len(self.symbols) <= 3 else f"universe{len(self.symbols)}"
        par = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.family}:{sym}" + (f":{par}" if par else "")

    @property
    def p(self) -> dict[str, Any]:
        return dict(self.params)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "family": self.family, "symbols": list(self.symbols),
                "params": {k: v for k, v in self.params}}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Candidate":
        return cls(data["family"], tuple(data["symbols"]), tuple(sorted(data["params"].items())))


Decision = tuple[np.ndarray, np.ndarray, np.ndarray]


@dataclass(frozen=True)
class Family:
    name: str
    mechanism: str
    references: tuple[str, ...]
    execution: str
    build: Callable[[Panel, Candidate], Decision]
    benchmark_only: bool = False
    caveats: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# causal feature helpers (feature[t] uses data through t only; callers lag)


def lag(x: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if k < len(x):
        out[k:] = x[:len(x) - k]
    return out


def rolling_mean(x: np.ndarray, window: int) -> np.ndarray:
    """Mean of the last ``window`` values; NaN unless all are finite."""
    out = np.full_like(x, np.nan, dtype=float)
    finite = np.isfinite(x)
    values = np.where(finite, x, 0.0)
    csum = np.cumsum(values, axis=0)
    ccount = np.cumsum(finite, axis=0)
    csum = np.vstack([np.zeros((1,) + x.shape[1:]), csum])
    ccount = np.vstack([np.zeros((1,) + x.shape[1:]), ccount])
    if len(x) >= window:
        total = csum[window:] - csum[:-window]
        count = ccount[window:] - ccount[:-window]
        out[window - 1:] = np.where(count == window, total / window, np.nan)
    return out


def rolling_std(x: np.ndarray, window: int) -> np.ndarray:
    mean = rolling_mean(x, window)
    mean_sq = rolling_mean(x * x, window)
    var = np.maximum(mean_sq - mean * mean, 0.0) * window / max(window - 1, 1)
    return np.sqrt(var)


def trailing_return(tri: np.ndarray, lookback: int) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return tri / lag(tri, lookback) - 1.0


def trailing_rf(panel: Panel, lookback: int) -> np.ndarray:
    growth = np.cumprod(1.0 + panel.rf_cc)
    return growth / lag(growth[:, None], lookback)[:, 0] - 1.0


def rsi(tri: np.ndarray, period: int) -> np.ndarray:
    """Wilder's RSI on the total-return index, column by column."""
    out = np.full_like(tri, np.nan, dtype=float)
    change = tri - lag(tri, 1)
    for j in range(tri.shape[1]):
        gain = loss = None
        count = 0
        for t in range(tri.shape[0]):
            c = change[t, j]
            if not np.isfinite(c):
                gain = loss = None
                count = 0
                continue
            up, down = max(c, 0.0), max(-c, 0.0)
            if gain is None:
                gain, loss, count = up, down, 1
            elif count < period:
                gain = (gain * count + up) / (count + 1)
                loss = (loss * count + down) / (count + 1)
                count += 1
            else:
                gain = (gain * (period - 1) + up) / period
                loss = (loss * (period - 1) + down) / period
            if count >= period:
                out[t, j] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def _columns(panel: Panel, symbols: Sequence[str]) -> list[int]:
    index = panel.index
    return [index[s] for s in symbols]


def _held(close_w: np.ndarray) -> np.ndarray:
    """A position kept through the open: ``open_w[t] = close_w[t-1]``."""
    out = np.zeros_like(close_w)
    out[1:] = close_w[:-1]
    return out


def _calendar(panel: Panel) -> list[dt.date]:
    return list(panel.dates) + [d for d in panel.future_dates if d > panel.dates[-1]]


def month_position(dates: Sequence[dt.date]) -> tuple[np.ndarray, np.ndarray]:
    """For each date: 1-based index from the month start and from the month end.

    The last month in ``dates`` may be incomplete; its from-end index is then
    unknown (0), so no turn-of-month decision is taken from a partial month.
    """
    n = len(dates)
    from_start = np.zeros(n, dtype=int)
    from_end = np.zeros(n, dtype=int)
    groups: dict[tuple[int, int], list[int]] = {}
    for i, d in enumerate(dates):
        groups.setdefault((d.year, d.month), []).append(i)
    last_key = (dates[-1].year, dates[-1].month) if dates else None
    for key, members in groups.items():
        for rank, i in enumerate(members, start=1):
            from_start[i] = rank
            if key != last_key:
                from_end[i] = len(members) - rank + 1
    return from_start, from_end


def holiday_eve(dates: Sequence[dt.date]) -> np.ndarray:
    """True where the next session is more than one business day away."""
    out = np.zeros(len(dates), dtype=bool)
    for i in range(len(dates) - 1):
        gap = np.busday_count(dates[i] + dt.timedelta(days=1), dates[i + 1])
        out[i] = gap > 0
    return out


# --------------------------------------------------------------------------
# families


def _sma_filter(tri_lag: np.ndarray, window: int) -> np.ndarray:
    return tri_lag > rolling_mean_lagged(tri_lag, window)


def rolling_mean_lagged(tri_lag: np.ndarray, window: int) -> np.ndarray:
    return rolling_mean(tri_lag, window)


def build_overnight(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    tri1 = lag(panel.tri[:, cols])                 # known at decision time on t
    valid = np.isfinite(tri1)
    flt = c.p.get("filter", "none")
    if flt == "sma200":
        cond = _sma_filter(tri1, 200)
        ready = np.isfinite(rolling_mean(tri1, 200)).all(axis=1)
    else:
        cond = valid
        ready = valid.all(axis=1)
    close_w = np.where(cond & valid, 1.0, 0.0)
    return np.zeros_like(close_w), close_w, ready


def build_intraday(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    valid = np.isfinite(lag(panel.tri[:, cols]))
    open_w = np.where(valid, float(c.p.get("direction", 1)), 0.0)
    return open_w, np.zeros_like(open_w), valid.all(axis=1)


def build_weekday_overnight(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    valid = np.isfinite(lag(panel.tri[:, cols]))
    cal = _calendar(panel)
    weekday = int(c.p["weekday"])
    nxt = np.array([cal[i + 1].weekday() == weekday if i + 1 < len(cal) else False
                    for i in range(len(panel.dates))])
    close_w = np.where(valid & nxt[:, None], 1.0, 0.0)
    return np.zeros_like(close_w), close_w, valid.all(axis=1)


def build_trend_sma(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    tri1 = lag(panel.tri[:, cols])
    sma = rolling_mean(tri1, int(c.p["window"]))
    close_w = np.where(np.isfinite(sma) & (tri1 > sma), 1.0, 0.0)
    return _held(close_w), close_w, np.isfinite(sma).all(axis=1)


def build_tsmom(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    lookback = int(c.p["lookback"])
    mom = lag(trailing_return(panel.tri[:, cols], lookback))
    hurdle = lag(trailing_rf(panel, lookback)[:, None])
    close_w = np.where(np.isfinite(mom) & (mom > hurdle), 1.0, 0.0)
    return _held(close_w), close_w, np.isfinite(mom).all(axis=1)


def _month_starts(dates: Sequence[dt.date]) -> np.ndarray:
    out = np.zeros(len(dates), dtype=bool)
    for i in range(1, len(dates)):
        out[i] = dates[i].month != dates[i - 1].month
    return out


def build_xs_momentum(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    lookback, top, absolute = int(c.p["lookback"]), int(c.p["top"]), bool(c.p["absolute"])
    mom = lag(trailing_return(panel.tri[:, cols], lookback))
    hurdle = lag(trailing_rf(panel, lookback)[:, None])[:, 0]
    rebalance = _month_starts(panel.dates)
    close_w = np.zeros(mom.shape)
    current = np.zeros(len(cols))
    ready = np.zeros(len(panel.dates), dtype=bool)
    for t in range(len(panel.dates)):
        eligible = np.isfinite(mom[t])
        ready[t] = eligible.sum() >= max(top, 3)
        if rebalance[t] and ready[t]:
            scores = np.where(eligible, mom[t], -np.inf)
            chosen = np.argsort(-scores, kind="stable")[:top]
            current = np.zeros(len(cols))
            for j in chosen:
                if np.isfinite(scores[j]) and (not absolute or scores[j] > hurdle[t]):
                    current[j] = 1.0 / top
        close_w[t] = current
    return _held(close_w), close_w, ready


def build_dual_momentum(panel: Panel, c: Candidate) -> Decision:
    risk, alt, safe = c.symbols                     # e.g. SPY, EFA, TLT
    cols = _columns(panel, c.symbols)
    lookback = int(c.p["lookback"])
    mom = lag(trailing_return(panel.tri[:, cols], lookback))
    hurdle = lag(trailing_rf(panel, lookback)[:, None])[:, 0]
    rebalance = _month_starts(panel.dates)
    close_w = np.zeros(mom.shape)
    current = np.zeros(3)
    ready = np.isfinite(mom).all(axis=1)
    for t in range(len(panel.dates)):
        if rebalance[t] and ready[t]:
            current = np.zeros(3)
            if mom[t, 0] > hurdle[t]:
                current[0 if mom[t, 0] >= mom[t, 1] else 1] = 1.0
            else:
                current[2] = 1.0
        close_w[t] = current
    return _held(close_w), close_w, ready


def build_rsi2(panel: Panel, c: Candidate) -> Decision:
    """Connors-style reversal, entered and exited at the next opening auction."""
    cols = _columns(panel, c.symbols)
    tri = panel.tri[:, cols]
    r = lag(rsi(tri, 2))
    sma5 = lag(rolling_mean(tri, 5))
    sma200 = lag(rolling_mean(tri, 200))
    tri1 = lag(tri)
    threshold, flt = float(c.p["threshold"]), c.p.get("filter", "none")
    max_hold = int(c.p.get("max_hold", 10))
    open_w = np.zeros(tri.shape)
    for j in range(tri.shape[1]):
        held, age = False, 0
        for t in range(tri.shape[0]):
            if not np.isfinite(tri1[t, j]):
                held, age = False, 0
                continue
            if held:
                age += 1
                if (np.isfinite(sma5[t, j]) and tri1[t, j] > sma5[t, j]) or age >= max_hold:
                    held, age = False, 0
            if not held and np.isfinite(r[t, j]) and r[t, j] < threshold:
                if flt != "sma200" or (np.isfinite(sma200[t, j]) and tri1[t, j] > sma200[t, j]):
                    held, age = True, 0
            open_w[t, j] = 1.0 if held else 0.0
    ready = np.isfinite(sma200 if flt == "sma200" else r).all(axis=1)
    return open_w, open_w.copy(), ready


def build_turn_of_month(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    valid = np.isfinite(lag(panel.tri[:, cols]))
    cal = _calendar(panel)
    from_start, from_end = month_position(cal)
    pre, post = int(c.p["pre"]), int(c.p["post"])
    window = ((from_end >= 1) & (from_end <= pre)) | ((from_start >= 1) & (from_start <= post))
    n = len(panel.dates)
    in_session = window[:n]
    next_in = np.append(window[1:n + 1], False)[:n] if len(window) > n else np.append(window[1:n], False)
    open_w = np.where(valid & in_session[:, None], 1.0, 0.0)
    close_w = np.where(valid & next_in[:, None], 1.0, 0.0)
    return open_w, close_w, valid.all(axis=1)


def build_pre_holiday(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    valid = np.isfinite(lag(panel.tri[:, cols]))
    cal = _calendar(panel)
    eve = holiday_eve(cal)
    n = len(panel.dates)
    in_session = eve[:n]
    next_in = np.append(eve[1:n + 1], False)[:n] if len(eve) > n else np.append(eve[1:n], False)
    open_w = np.where(valid & in_session[:, None], 1.0, 0.0)
    close_w = np.where(valid & next_in[:, None], 1.0, 0.0)
    return open_w, close_w, valid.all(axis=1)


def build_vol_target(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    lookback, target = int(c.p["lookback"]), float(c.p["target"])
    band = float(c.p.get("band", 0.10))
    vol = lag(rolling_std(panel.ret_cc[:, cols], lookback)) * np.sqrt(252.0)
    desired = np.where(np.isfinite(vol) & (vol > 0), np.minimum(1.0, target / vol), 0.0)
    close_w = np.zeros(desired.shape)
    current = np.zeros(len(cols))
    for t in range(len(panel.dates)):
        move = np.abs(desired[t] - current) > band
        current = np.where(move | (desired[t] == 0), desired[t], current)
        close_w[t] = current
    return _held(close_w), close_w, np.isfinite(vol).all(axis=1)


def build_buy_and_hold(panel: Panel, c: Candidate) -> Decision:
    cols = _columns(panel, c.symbols)
    valid = np.isfinite(lag(panel.tri[:, cols]))
    close_w = np.where(valid, 1.0 / len(cols), 0.0)
    return _held(close_w), close_w, valid.all(axis=1)


FAMILIES: dict[str, Family] = {f.name: f for f in [
    Family("overnight",
           "Hold from the closing auction to the next opening auction. The equity premium "
           "accrues mostly overnight (risk borne while markets are shut; retail buying at "
           "the open and institutional selling during the day).",
           ("Cliff, Cooper and Gulen (2008)", "Lou, Polk and Skouras (2019), JFE 134(1)"),
           "buy market-on-close, sell market-on-open", build_overnight,
           caveats=("one round trip every held session: the result is only as good as the "
                    "assumption that auction orders receive the printed open and close",)),
    Family("intraday",
           "Hold (or short) from the opening auction to the closing auction: the mirror of "
           "the overnight leg.", ("Lou, Polk and Skouras (2019)",),
           "market-on-open, market-on-close", build_intraday,
           caveats=("a short needs a margin account and an easy-to-borrow fund",)),
    Family("weekday_overnight",
           "Overnight hold only into one weekday (the weekend effect and its relatives).",
           ("French (1980)",), "buy market-on-close, sell market-on-open",
           build_weekday_overnight),
    Family("trend_sma",
           "Hold while the total-return index is above its moving average, else cash "
           "(time-series trend following).", ("Faber (2007)", "Moskowitz, Ooi and Pedersen (2012)"),
           "market-on-close on signal changes", build_trend_sma),
    Family("tsmom",
           "Hold while the trailing return beats bills, else cash (time-series momentum).",
           ("Moskowitz, Ooi and Pedersen (2012), JFE 104(2)",),
           "market-on-close on signal changes", build_tsmom),
    Family("xs_momentum",
           "Each month hold the top funds by trailing return (cross-sectional momentum, "
           "optionally only those that beat bills).", ("Jegadeesh and Titman (1993)",
                                                      "Antonacci (2014)"),
           "market-on-close on the first session of the month", build_xs_momentum),
    Family("dual_momentum",
           "US stocks or international stocks by relative momentum, bonds when US stocks "
           "trail bills.", ("Antonacci (2014)",),
           "market-on-close on the first session of the month", build_dual_momentum),
    Family("rsi2",
           "Buy a short-term oversold fund (two-day RSI below a threshold), sell when it "
           "closes above its five-day mean (short-horizon reversal).",
           ("Connors and Alvarez (2008)",), "market-on-open entries and exits", build_rsi2,
           caveats=("the classic rule buys at the signal close; this causal version buys at "
                    "the next open and so gives up the overnight rebound",)),
    Family("turn_of_month",
           "Hold over the last trading days of a month and the first days of the next "
           "(pension and payroll flows).", ("Lakonishok and Smidt (1988)",
                                             "McConnell and Xu (2008)"),
           "market-on-close entry and exit", build_turn_of_month),
    Family("pre_holiday",
           "Hold the session before an exchange holiday.", ("Ariel (1990)",),
           "market-on-close entry and exit", build_pre_holiday),
    Family("vol_target",
           "Scale exposure to a volatility target from trailing realised volatility, capped "
           "at fully invested (volatility-managed portfolios).",
           ("Moreira and Muir (2017), JF 72(4)",),
           "market-on-close when the weight moves by more than the band", build_vol_target),
    Family("buy_and_hold", "Reference only: hold the fund.", (), "none",
           build_buy_and_hold, benchmark_only=True),
]}


def daily_candidates(symbols: Sequence[str] = UNIVERSE) -> list[Candidate]:
    """The full daily grid: every family times its parameters times its symbols."""
    out: list[Candidate] = []
    for s in symbols:
        one = (s,)
        out += [Candidate("overnight", one, (("filter", f),)) for f in ("none", "sma200")]
        out += [Candidate("intraday", one, (("direction", d),)) for d in (1, -1)]
        out += [Candidate("trend_sma", one, (("window", w),)) for w in (50, 200)]
        out += [Candidate("tsmom", one, (("lookback", k),)) for k in (126, 252)]
        out += [Candidate("rsi2", one, (("filter", f), ("max_hold", 10), ("threshold", x)))
                for x in (5, 10) for f in ("none", "sma200")]
        out += [Candidate("turn_of_month", one, (("post", post), ("pre", pre)))
                for pre, post in ((1, 3), (2, 4))]
        out.append(Candidate("pre_holiday", one))
        out += [Candidate("vol_target", one, (("band", 0.1), ("lookback", k), ("target", 0.1)))
                for k in (20, 63)]
    for s in [x for x in EQUITY_INDEX if x in symbols]:
        out += [Candidate("weekday_overnight", (s,), (("weekday", w),)) for w in range(5)]
    universe = tuple(symbols)
    for lookback, top, absolute in itertools.product((63, 126, 252), (1, 3, 6), (0, 1)):
        out.append(Candidate("xs_momentum", universe,
                             (("absolute", absolute), ("lookback", lookback), ("top", top))))
    if all(s in symbols for s in ("SPY", "EFA", "TLT")):
        out += [Candidate("dual_momentum", ("SPY", "EFA", "TLT"), (("lookback", k),))
                for k in (126, 252)]
    return out


def benchmarks(symbols: Sequence[str] = UNIVERSE) -> list[Candidate]:
    return [Candidate("buy_and_hold", (s,)) for s in symbols]


def decide(panel: Panel, candidate: Candidate) -> Decision:
    open_w, close_w, ready = FAMILIES[candidate.family].build(panel, candidate)
    return (np.nan_to_num(open_w), np.nan_to_num(close_w), np.asarray(ready, dtype=bool))
