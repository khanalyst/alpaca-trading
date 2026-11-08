"""One engine for every candidate.

Weights come from :func:`edgefinder.catalogue.decide`:

* overnight leg of ``t``: ``w_on[t] = close_w[t-1]``;
* intraday leg of ``t``:  ``w_id[t] = open_w[t]``.

Daily excess return over bills, net of costs::

    sum_s  w_on * on  - max(w_on, 0) * rf_on
         + w_id * id  - max(w_id, 0) * rf_id
         - c_s * (|w_id - w_on| + |close_w - w_id|)

where ``c_s`` is half the symbol's round-trip cost, charged at each auction
where the weight changes.  A long position is funded from cash, so it gives up
the bill rate; a short's proceeds are assumed to earn nothing (Alpaca pays no
interest on them) while the account's own cash keeps earning the bill rate.

Weights are held constant between decisions, which assumes a rebalance back
to target each session.  For the monthly rotations that ignores a small drift
turnover; for single-fund rules it is exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from edgefinder import costs
from edgefinder.catalogue import Candidate, decide
from edgefinder.data import Panel


@dataclass
class Result:
    candidate: Candidate
    excess: np.ndarray          # net daily excess return (0 on days not ready)
    gross: np.ndarray           # the same before costs
    cost: np.ndarray            # costs charged that day
    raw: np.ndarray             # net return if idle cash earns nothing
    benchmark: np.ndarray       # equal-weight buy and hold of the candidate's funds, excess
    ready: np.ndarray           # bool, look-back filled
    exposure_on: np.ndarray     # gross overnight weight
    exposure_id: np.ndarray     # gross intraday weight
    turnover: np.ndarray        # one-way weight traded that day
    opened: np.ndarray          # positions opened that day (at either auction)
    entries: int                # number of positions opened (round trips)
    missing_legs: int           # weight held on a leg with no price (counted, not earned)

    @property
    def id(self) -> str:
        return self.candidate.id


def run(panel: Panel, candidate: Candidate, *, stress: float = 1.0) -> Result:
    open_w, close_w, ready = decide(panel, candidate)
    cols = [panel.index[s] for s in candidate.symbols]
    on, idr = panel.ret_on[:, cols], panel.ret_id[:, cols]
    w_on = np.zeros_like(close_w)
    w_on[1:] = close_w[:-1]
    w_id = open_w
    on_ok, id_ok = np.isfinite(on), np.isfinite(idr)
    missing = int(np.sum((w_on != 0) & ~on_ok) + np.sum((w_id != 0) & ~id_ok))
    on0, id0 = np.where(on_ok, on, 0.0), np.where(id_ok, idr, 0.0)
    w_on_eff, w_id_eff = np.where(on_ok, w_on, 0.0), np.where(id_ok, w_id, 0.0)
    rf_on, rf_id = panel.rf_on[:, None], panel.rf_id[:, None]
    market = w_on_eff * on0 + w_id_eff * id0
    funding = np.maximum(w_on_eff, 0.0) * rf_on + np.maximum(w_id_eff, 0.0) * rf_id
    one_way = costs.one_way_vector(candidate.symbols, stress=stress)[None, :]
    traded = np.abs(w_id - w_on) + np.abs(close_w - w_id)
    cost = (traded * one_way).sum(axis=1)
    gross = (market - funding).sum(axis=1)
    raw = market.sum(axis=1) - cost
    ready = ready & np.isfinite(panel.rf_rate)
    gross = np.where(ready, gross, 0.0)
    cost = np.where(ready, cost, 0.0)
    raw = np.where(ready, raw, 0.0)
    excess = gross - cost
    cc = panel.ret_cc[:, cols] - panel.rf_cc[:, None]
    with np.errstate(invalid="ignore"):
        bench = np.where(np.isfinite(cc), cc, np.nan)
        counts = np.isfinite(bench).sum(axis=1)
        benchmark = np.where(counts > 0, np.nansum(bench, axis=1) / np.maximum(counts, 1), 0.0)
    benchmark = np.where(ready, benchmark, 0.0)
    legs = np.empty((2 * len(panel.dates), len(cols)))
    legs[0::2], legs[1::2] = w_on, w_id
    entries = int(np.sum((legs[1:] != 0) & (legs[:-1] == 0)) + np.sum(legs[0] != 0))
    opened = (((w_id != 0) & (w_on == 0)).sum(axis=1)
              + ((close_w != 0) & (w_id == 0)).sum(axis=1)).astype(float)
    return Result(candidate, excess, gross, cost, raw, benchmark, ready,
                  np.abs(w_on_eff).sum(axis=1), np.abs(w_id_eff).sum(axis=1),
                  np.where(ready, traded.sum(axis=1), 0.0), np.where(ready, opened, 0.0),
                  entries, missing)


def run_all(panel: Panel, candidates: Sequence[Candidate], *, stress: float = 1.0) -> list[Result]:
    return [run(panel, c, stress=stress) for c in candidates]


# --------------------------------------------------------------------------
# hourly last-half-hour families (research only: not executable by the trader)


@dataclass(frozen=True)
class HourlyCandidate:
    family: str
    symbols: tuple[str, ...]

    @property
    def id(self) -> str:
        sym = "+".join(self.symbols) if len(self.symbols) <= 3 else f"universe{len(self.symbols)}"
        return f"{self.family}:{sym}"


HOURLY_FAMILIES = {
    "last_half_hour_momentum": (
        "The return from the previous close to 15:30 predicts the last half hour "
        "(hedging and rebalancing flows into the close).",
        ("Baltussen, Da, Lammers and Martens (2021), JFE 142(1)",
         "Gao, Han, Li and Zhou (2018), JFE 129(2)")),
    "first_hour_momentum": (
        "The first hour's return predicts the last half hour.",
        ("Gao, Han, Li and Zhou (2018), JFE 129(2)",)),
}


def hourly_candidates(symbols: Sequence[str]) -> list[HourlyCandidate]:
    out = [HourlyCandidate(f, (s,)) for f in HOURLY_FAMILIES for s in symbols]
    out += [HourlyCandidate(f, tuple(symbols)) for f in HOURLY_FAMILIES]
    return out


def run_hourly(hp, candidate: HourlyCandidate) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Daily net return, benchmark (long last half hour) and ready mask."""
    cols = [hp.symbols.index(s) for s in candidate.symbols]
    with np.errstate(invalid="ignore", divide="ignore"):
        if candidate.family == "last_half_hour_momentum":
            signal = hp.last_bar_open[:, cols] / hp.prev_close[:, cols] - 1.0
        else:
            signal = hp.first_hour_close[:, cols] / hp.prev_close[:, cols] - 1.0
        last = hp.close[:, cols] / hp.last_bar_open[:, cols] - 1.0
    ok = np.isfinite(signal) & np.isfinite(last)
    position = np.where(ok, np.sign(signal), 0.0)
    round_trip = 2.0 * costs.one_way_vector(candidate.symbols)[None, :]
    per_symbol = position * np.where(ok, last, 0.0) - np.abs(position) * round_trip
    counts = ok.sum(axis=1)
    net = np.where(counts > 0, per_symbol.sum(axis=1) / np.maximum(counts, 1), 0.0)
    bench = np.where(counts > 0, np.where(ok, last, 0.0).sum(axis=1) / np.maximum(counts, 1), 0.0)
    return net, bench, counts > 0
