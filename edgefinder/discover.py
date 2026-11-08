"""The automated search: every candidate, one protocol, decided in advance.

Protocol (the constants below are the whole decision rule):

1. Back-test every daily candidate over each fund's full history; statistics
   start at ``STATS_START`` (decimal ticks everywhere, so today's cost model
   applies to every evaluated day).
2. Discovery window ``STATS_START .. DISCOVERY_END``.  The edge measure is
   the information ratio of alpha against equal-weight buy and hold of the
   candidate's own funds (Newey-West), net of per-symbol costs.  Earning the
   market's premium is not an edge; beating it per unit of risk taken is.
3. Multiple testing over the whole catalogue in discovery: Holm and
   Benjamini-Yekutieli adjusted p-values, Hansen's SPA test, White's reality
   check, the deflated Sharpe ratio and PBO by CSCV.
4. The ten best discovery candidates (by alpha IR, at least five years of
   history) are confirmed once on the untouched holdout window
   ``DISCOVERY_END+1 .. last bar``.  The holdout p-values are Holm-adjusted
   across those ten.  The tradeable edge is the best-ranked candidate that
   passes every confirmation test.
5. A walk-forward re-runs the selection every January with only past data
   and holds the pick for the year; its concatenated returns are the honest
   estimate of what "pick the best edge automatically" earns.
6. The hourly last-half-hour families run separately on the two years of
   hourly bars, split in half by date.  They are research only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from edgefinder import SCHEMA, backtest, catalogue, costs, data, stats
from edgefinder.backtest import Result
from edgefinder.catalogue import FAMILIES, Candidate

STATS_START = dt.date(2001, 6, 1)
DISCOVERY_END = dt.date(2015, 12, 31)
MIN_DISCOVERY_YEARS = 5.0
CONFIRM_TOP = 10
CONFIRM_ALPHA = 0.05
MIN_BREAKEVEN_MULTIPLE = 2.0
MIN_DSR = 0.95
WALK_FORWARD_FIRST_YEAR = 2007
WALK_FORWARD_MIN_YEARS = 3.0
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_BLOCK = 20.0
SEED = 20261108


@dataclass
class Window:
    name: str
    start: dt.date
    end: dt.date

    def mask(self, dates: Sequence[dt.date]) -> np.ndarray:
        return np.array([self.start <= d <= self.end for d in dates])


def window_metrics(result: Result, panel: data.Panel, window: Window) -> dict[str, Any]:
    mask = result.ready & window.mask(panel.dates)
    days = int(mask.sum())
    if days < 60:
        return {"days": days, "insufficient": True}
    x, b = result.excess[mask], result.benchmark[mask]
    rf = panel.rf_cc[mask]
    mean, se, t = stats.hac_mean(x)
    fit = stats.hac_alpha(x, b)
    resid = x - fit["beta"] * b
    resid_sd = float(np.std(resid - resid.mean(), ddof=1))
    sd = float(np.std(x, ddof=1))
    years = days / stats.TRADING_DAYS
    gross_mean, cost_mean = float(result.gross[mask].mean()), float(result.cost[mask].mean())
    trades = float(result.opened[mask].sum())
    dates = [d for d, ok in zip(panel.dates, mask) if ok]
    by_year = stats.yearly(dates, x, np.ones(days, bool))
    alpha_ex_best = None
    if len(by_year) >= 3:
        best = max(by_year, key=by_year.get)
        keep = np.array([d.year != best for d in dates])
        alpha_ex_best = stats.hac_alpha(x[keep], b[keep])["alpha"] * stats.TRADING_DAYS
    bench_sd = float(np.std(b, ddof=1))
    return {
        "days": days, "years": round(years, 2),
        "start": dates[0].isoformat(), "end": dates[-1].isoformat(),
        "ann_excess": mean * stats.TRADING_DAYS,
        "ann_vol": sd * math.sqrt(stats.TRADING_DAYS),
        "sharpe": (mean / sd * math.sqrt(stats.TRADING_DAYS)) if sd > 0 else 0.0,
        "t_excess": t,
        "alpha": fit["alpha"] * stats.TRADING_DAYS, "beta": fit["beta"], "alpha_t": fit["alpha_t"],
        "alpha_ir": (fit["alpha"] / resid_sd * math.sqrt(stats.TRADING_DAYS)) if resid_sd > 0 else 0.0,
        "benchmark_ann_excess": float(b.mean()) * stats.TRADING_DAYS,
        "benchmark_sharpe": (float(b.mean()) / bench_sd * math.sqrt(stats.TRADING_DAYS))
        if bench_sd > 0 else 0.0,
        "max_drawdown": stats.max_drawdown(x + rf),
        "benchmark_max_drawdown": stats.max_drawdown(b + rf),
        "cagr_with_bills": float(np.prod(1.0 + x + rf) ** (1.0 / years) - 1.0),
        "cagr_cash_earns_nothing": float(np.prod(1.0 + result.raw[mask]) ** (1.0 / years) - 1.0),
        "exposure_overnight": float(result.exposure_on[mask].mean()),
        "exposure_intraday": float(result.exposure_id[mask].mean()),
        "turnover_per_year": float(result.turnover[mask].sum() / years),
        "trades_per_year": trades / years,
        "net_bps_per_trade": (1e4 * float(x.sum()) / trades) if trades else None,
        "gross_bps_per_day": 1e4 * gross_mean, "cost_bps_per_day": 1e4 * cost_mean,
        "breakeven_cost_multiple": (gross_mean / cost_mean) if cost_mean > 0 else None,
        "positive_years": (sum(v > 0 for v in by_year.values()) / len(by_year)) if by_year else None,
        "worst_year": min(by_year.values()) if by_year else None,
        "alpha_without_best_year": alpha_ex_best,
        "yearly_excess": {str(k): v for k, v in sorted(by_year.items())},
    }


def _residual_matrix(results: Sequence[Result], mask: np.ndarray) -> np.ndarray:
    columns = []
    for r in results:
        m = r.ready & mask
        x, b = r.excess, r.benchmark
        beta = stats.hac_alpha(x[m], b[m])["beta"] if m.sum() > 60 else 0.0
        columns.append(np.where(m, x - beta * b, 0.0)[mask])
    return np.column_stack(columns)


def walk_forward(results: Sequence[Result], panel: data.Panel, *,
                 first_year: int = WALK_FORWARD_FIRST_YEAR) -> dict[str, Any]:
    dates = panel.dates
    years = sorted({d.year for d in dates if d.year >= first_year})
    picks, xs, bs, rfs, used_dates = [], [], [], [], []
    for year in years:
        train = Window("train", STATS_START, dt.date(year - 1, 12, 31))
        test = Window("test", dt.date(year, 1, 1), dt.date(year, 12, 31))
        tmask, smask = train.mask(dates), test.mask(dates)
        best, best_ir = None, -np.inf
        for r in results:
            m = r.ready & tmask
            if m.sum() < WALK_FORWARD_MIN_YEARS * stats.TRADING_DAYS:
                continue
            fit = stats.hac_alpha(r.excess[m], r.benchmark[m])
            resid = r.excess[m] - fit["beta"] * r.benchmark[m]
            sd = resid.std(ddof=1)
            ir = fit["alpha"] / sd if sd > 0 else -np.inf
            if ir > best_ir:
                best, best_ir = r, ir
        if best is None:
            continue
        m = smask
        xs.append(np.where(best.ready[m], best.excess[m], 0.0))
        bs.append(np.where(best.ready[m], best.benchmark[m], 0.0))
        rfs.append(panel.rf_cc[m])
        used_dates += [d for d, ok in zip(dates, m) if ok]
        picks.append({"year": year, "pick": best.id,
                      "train_alpha_ir": best_ir * math.sqrt(stats.TRADING_DAYS),
                      "year_excess": float(np.prod(1.0 + xs[-1]) - 1.0)})
    if not xs:
        return {"picks": []}
    x, b, rf = np.concatenate(xs), np.concatenate(bs), np.concatenate(rfs)
    mean, _, t = stats.hac_mean(x)
    fit = stats.hac_alpha(x, b)
    sd = x.std(ddof=1)
    n_years = len(x) / stats.TRADING_DAYS
    spy = _buy_and_hold_excess(panel, "SPY", used_dates)
    return {
        "first_year": years[0], "last_year": years[-1], "days": len(x),
        "ann_excess": mean * stats.TRADING_DAYS, "sharpe": mean / sd * math.sqrt(stats.TRADING_DAYS),
        "t_excess": t, "alpha": fit["alpha"] * stats.TRADING_DAYS, "alpha_t": fit["alpha_t"],
        "beta": fit["beta"], "max_drawdown": stats.max_drawdown(x + rf),
        "cagr_with_bills": float(np.prod(1.0 + x + rf) ** (1.0 / n_years) - 1.0),
        "spy_sharpe_same_days": spy.get("sharpe"), "spy_cagr_same_days": spy.get("cagr"),
        "distinct_picks": len({p["pick"] for p in picks}), "picks": picks,
    }


def _buy_and_hold_excess(panel: data.Panel, symbol: str, dates: Sequence[dt.date]) -> dict:
    if symbol not in panel.symbols:
        return {}
    j = panel.index[symbol]
    wanted = set(dates)
    m = np.array([d in wanted for d in panel.dates]) & np.isfinite(panel.ret_cc[:, j])
    x = panel.ret_cc[m, j] - panel.rf_cc[m]
    total = panel.ret_cc[m, j]
    years = m.sum() / stats.TRADING_DAYS
    return {"sharpe": float(x.mean() / x.std(ddof=1) * math.sqrt(stats.TRADING_DAYS)),
            "cagr": float(np.prod(1.0 + total) ** (1.0 / years) - 1.0)}


def confirm(rank: int, cand: dict[str, Any], holm_p: float) -> dict[str, Any]:
    oos, disc = cand["holdout"], cand["discovery"]
    tests = {
        "holdout_alpha_significant": holm_p < CONFIRM_ALPHA,
        "holdout_sharpe_positive": (oos.get("sharpe") or 0) > 0,
        "holdout_breakeven_cost_at_least_2x": (oos.get("breakeven_cost_multiple") or 0)
        >= MIN_BREAKEVEN_MULTIPLE,
        "discovery_deflated_sharpe": (cand.get("deflated_sharpe") or 0) >= MIN_DSR,
        "holdout_alpha_survives_best_year_removed": (oos.get("alpha_without_best_year") or -1) > 0,
    }
    passed = sum(tests.values())
    if passed == len(tests):
        verdict = "confirmed"
    elif (oos.get("alpha_t") or 0) >= 1.0 and (oos.get("sharpe") or 0) > 0:
        verdict = "weak"
    else:
        verdict = "rejected"
    return {"discovery_rank": rank, "id": cand["id"], "holdout_alpha_p_holm": holm_p,
            "tests": tests, "verdict": verdict}


def caveats_for(cand: dict[str, Any]) -> list[str]:
    """Automatic warnings, so a reader sees the weak spots next to the number."""
    out: list[str] = []
    disc, oos, full = cand["discovery"], cand["holdout"], cand["full"]
    family = FAMILIES[cand["family"]]
    out += list(family.caveats)
    be = oos.get("breakeven_cost_multiple")
    if be is not None and be < 3:
        out.append(f"holdout gross edge covers only {be:.1f}x the modelled cost; a cost "
                   f"error of that size erases it")
    if disc.get("alpha_ir") and oos.get("alpha_ir") is not None and \
            oos["alpha_ir"] < 0.5 * disc["alpha_ir"]:
        out.append(f"holdout alpha IR {oos['alpha_ir']:.2f} is less than half the discovery "
                   f"IR {disc['alpha_ir']:.2f} (decay or selection bias)")
    yearly = oos.get("yearly_excess") or {}
    recent = [v for k, v in sorted(yearly.items())][-3:]
    if recent and sum(recent) < 0:
        out.append("the last three holdout years sum to a loss")
    if (oos.get("positive_years") or 1) < 0.6:
        out.append(f"only {100 * oos['positive_years']:.0f}% of holdout years are positive")
    if (full.get("max_drawdown") or 0) < -0.25:
        out.append(f"full-sample drawdown {100 * full['max_drawdown']:.0f}% (with bills)")
    if (oos.get("beta") or 0) > 0.7:
        out.append(f"beta {oos['beta']:.2f} to buy and hold: most of the return is market exposure")
    if (oos.get("trades_per_year") or 0) > 150:
        out.append(f"{oos['trades_per_year']:.0f} round trips a year: every trade is a short-term "
                   f"gain for tax, and an auction-price shortfall repeats daily")
    if cand.get("holdout_2x_cost", {}).get("alpha", 1) <= 0:
        out.append("holdout alpha is not positive at twice the modelled cost")
    return out


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True, timeout=10).stdout.strip()
    except Exception:
        return None


def scan(panel: data.Panel, *, draws: int = BOOTSTRAP_DRAWS,
         discovery_end: dt.date = DISCOVERY_END) -> dict[str, Any]:
    candidates = catalogue.daily_candidates([s for s in data.UNIVERSE if s in panel.symbols])
    results = backtest.run_all(panel, candidates)
    bench_results = backtest.run_all(panel, catalogue.benchmarks(
        [s for s in data.UNIVERSE if s in panel.symbols]))
    last = panel.dates[-1]
    windows = {"discovery": Window("discovery", STATS_START, discovery_end),
               "holdout": Window("holdout", discovery_end + dt.timedelta(days=1), last),
               "full": Window("full", STATS_START, last)}
    rows = []
    for r in results:
        row = {"id": r.id, "family": r.candidate.family, "symbols": list(r.candidate.symbols),
               "params": dict(r.candidate.params), "missing_legs": r.missing_legs}
        for name, w in windows.items():
            row[name] = window_metrics(r, panel, w)
        rows.append(row)

    disc_mask = windows["discovery"].mask(panel.dates)
    eligible = [i for i, row in enumerate(rows)
                if row["discovery"].get("years", 0) >= MIN_DISCOVERY_YEARS]
    # multiple testing over the whole catalogue (discovery)
    p_disc = [stats.one_sided_p(row["discovery"].get("alpha_t", float("nan"))) for row in rows]
    for row, p, h, by in zip(rows, p_disc, stats.holm(p_disc), stats.benjamini_yekutieli(p_disc)):
        row["discovery"].update(alpha_p=p, alpha_p_holm=h, alpha_p_by=by)
    residual = _residual_matrix(results, disc_mask)
    daily_ir = residual.mean(axis=0) / np.where(residual.std(axis=0) > 0, residual.std(axis=0), np.inf)
    eligible_ir = daily_ir[eligible]
    sharpe_var = float(np.var(eligible_ir, ddof=1))
    n_trials = len(rows)
    n_eff = stats.effective_trials(residual[:, eligible])
    for i, row in enumerate(rows):
        m = results[i].ready & disc_mask
        if m.sum() > 60:
            x = residual[:, i][m[disc_mask]]
            row["deflated_sharpe"] = stats.deflated_sharpe(x, trials=n_trials,
                                                           sharpe_variance=sharpe_var)
            row["deflated_sharpe_effective_trials"] = stats.deflated_sharpe(
                x, trials=n_eff, sharpe_variance=sharpe_var)
    spa_alpha = stats.spa_test(residual[:, eligible], draws=draws, mean_block=BOOTSTRAP_BLOCK,
                               seed=SEED)
    excess = np.column_stack([np.where(r.ready, r.excess, 0.0)[disc_mask] for r in results])
    spa_cash = stats.spa_test(excess[:, eligible], draws=draws, mean_block=BOOTSTRAP_BLOCK,
                              seed=SEED + 1)
    pbo = stats.pbo_cscv(residual[:, eligible])

    ranked = sorted(eligible, key=lambda i: rows[i]["discovery"]["alpha_ir"], reverse=True)
    top = ranked[:CONFIRM_TOP]
    p_hold = [stats.one_sided_p(rows[i]["holdout"].get("alpha_t", float("nan"))) for i in top]
    holm_hold = stats.holm(p_hold)
    confirmations = []
    for rank, (i, hp) in enumerate(zip(top, holm_hold), start=1):
        stressed = backtest.run(panel, results[i].candidate, stress=2.0)
        rows[i]["holdout_2x_cost"] = {k: window_metrics(stressed, panel, windows["holdout"]).get(k)
                                      for k in ("alpha", "alpha_t", "sharpe", "ann_excess")}
        rows[i]["discovery_rank"] = rank
        confirmations.append(confirm(rank, rows[i], hp))
    for i in top:
        rows[i]["caveats"] = caveats_for(rows[i])
    chosen = next((c for c in confirmations if c["verdict"] == "confirmed"), None)

    # rank stability between windows (all eligible candidates with a holdout)
    both = [i for i in eligible if not rows[i]["holdout"].get("insufficient")]
    a = np.array([rows[i]["discovery"]["alpha_ir"] for i in both])
    h = np.array([rows[i]["holdout"]["alpha_ir"] for i in both])
    rank_corr = float(np.corrcoef(np.argsort(np.argsort(a)), np.argsort(np.argsort(h)))[0, 1])

    families = {}
    for name in FAMILIES:
        members = [i for i in eligible if rows[i]["family"] == name]
        if not members:
            continue
        best = max(members, key=lambda i: rows[i]["discovery"]["alpha_ir"])
        hold = [rows[i]["holdout"].get("alpha_ir") for i in members
                if not rows[i]["holdout"].get("insufficient")]
        families[name] = {
            "members": len(members), "best": rows[best]["id"],
            "best_discovery_alpha_ir": rows[best]["discovery"]["alpha_ir"],
            "best_holdout_alpha_ir": rows[best]["holdout"].get("alpha_ir"),
            "median_holdout_alpha_ir": float(np.median(hold)) if hold else None,
            "share_positive_holdout_alpha": float(np.mean([v > 0 for v in hold])) if hold else None,
            "mechanism": FAMILIES[name].mechanism,
            "references": list(FAMILIES[name].references),
            "execution": FAMILIES[name].execution,
        }
    benchmarks = {r.id: {name: {k: window_metrics(r, panel, w).get(k)
                                for k in ("ann_excess", "ann_vol", "sharpe", "max_drawdown",
                                          "cagr_with_bills", "years")}
                         for name, w in windows.items()}
                  for r in bench_results}
    wf = walk_forward(results, panel)
    return {
        "schema": SCHEMA, "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "code_commit": _git_commit(), "data_digest": panel.digest(),
        "data_last_date": last.isoformat(), "data_sources": panel.sources,
        "protocol": {"stats_start": STATS_START.isoformat(),
                     "discovery_end": discovery_end.isoformat(),
                     "min_discovery_years": MIN_DISCOVERY_YEARS, "confirm_top": CONFIRM_TOP,
                     "confirm_alpha": CONFIRM_ALPHA,
                     "min_breakeven_multiple": MIN_BREAKEVEN_MULTIPLE, "min_dsr": MIN_DSR,
                     "bootstrap_draws": draws, "bootstrap_mean_block": BOOTSTRAP_BLOCK,
                     "seed": SEED, "cost_model": costs.COST_MODEL,
                     "round_trip_bps": costs.schedule(panel.symbols)},
        "catalogue": {"candidates": len(rows), "eligible_for_selection": len(eligible),
                      "families": len(families)},
        "multiple_testing": {
            "trials": n_trials, "effective_trials": n_eff,
            "discovery_sharpe_variance_daily": sharpe_var,
            "spa_alpha": spa_alpha.__dict__ | {"best": rows[eligible[spa_alpha.best]]["id"]},
            "spa_vs_cash": spa_cash.__dict__ | {"best": rows[eligible[spa_cash.best]]["id"]},
            "pbo": pbo.__dict__,
            "significant_holm": [rows[i]["id"] for i in eligible
                                 if rows[i]["discovery"]["alpha_p_holm"] < 0.05],
            "significant_by": [rows[i]["id"] for i in eligible
                               if rows[i]["discovery"]["alpha_p_by"] < 0.05],
            "discovery_to_holdout_rank_correlation": rank_corr,
        },
        "confirmations": confirmations,
        "selected": chosen["id"] if chosen else None,
        "walk_forward": wf, "families": families, "benchmarks": benchmarks,
        "candidates": rows,
    }


def scan_hourly(hp: data.HourlyPanel) -> dict[str, Any]:
    cands = backtest.hourly_candidates(hp.symbols)
    half = len(hp.dates) // 2
    split = hp.dates[half]
    rows = []
    for c in cands:
        net, bench, ready = backtest.run_hourly(hp, c)
        row = {"id": c.id, "family": c.family}
        for name, sel in (("discovery", np.arange(len(hp.dates)) < half),
                          ("holdout", np.arange(len(hp.dates)) >= half)):
            m = ready & sel
            if m.sum() < 60:
                row[name] = {"days": int(m.sum()), "insufficient": True}
                continue
            mean, _, t = stats.hac_mean(net[m])
            fit = stats.hac_alpha(net[m], bench[m])
            sd = net[m].std(ddof=1)
            row[name] = {"days": int(m.sum()), "mean_bps_per_day": 1e4 * mean, "t": t,
                         "sharpe": mean / sd * math.sqrt(stats.TRADING_DAYS) if sd > 0 else 0.0,
                         "alpha_bps": 1e4 * fit["alpha"], "alpha_t": fit["alpha_t"],
                         "beta": fit["beta"]}
        rows.append(row)
    p = [stats.one_sided_p(r["discovery"].get("t", float("nan"))) for r in rows]
    for r, by in zip(rows, stats.benjamini_yekutieli(p)):
        r["discovery"]["p_by"] = by
    best = max(rows, key=lambda r: r["discovery"].get("t", -np.inf))
    return {"sessions": len(hp.dates), "first": hp.dates[0].isoformat(),
            "last": hp.dates[-1].isoformat(), "holdout_from": split.isoformat(),
            "families": {k: {"mechanism": v[0], "references": list(v[1])}
                         for k, v in backtest.HOURLY_FAMILIES.items()},
            "best_discovery": best["id"], "candidates": rows}


def _round(value: Any, places: int = 6) -> Any:
    if isinstance(value, float):
        return round(value, places) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _round(v, places) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round(v, places) for v in value]
    if isinstance(value, (np.floating,)):
        return _round(float(value), places)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, default=data.DEFAULT_CACHE)
    parser.add_argument("--out", type=Path, default=Path("edgefinder/results"))
    parser.add_argument("--refresh", action="store_true", help="refetch data even if cached")
    parser.add_argument("--no-hourly", action="store_true")
    parser.add_argument("--draws", type=int, default=BOOTSTRAP_DRAWS)
    args = parser.parse_args(argv)

    symbols = list(data.UNIVERSE)
    raw = data.fetch_daily(symbols, args.cache, refresh=args.refresh)
    panel = data.build_panel(raw, symbols)
    print(f"panel: {len(panel.dates)} sessions {panel.dates[0]} .. {panel.dates[-1]}, "
          f"{len(symbols)} funds", flush=True)
    result = scan(panel, draws=args.draws)
    result["data_quality"] = data.quality_report(raw, panel)
    if not args.no_hourly:
        hourly_raw = {s: data.fetch_yahoo_hourly(s, args.cache, refresh=args.refresh)
                      for s in symbols}
        result["hourly"] = scan_hourly(data.build_hourly_panel(hourly_raw, symbols))
    args.out.mkdir(parents=True, exist_ok=True)
    rounded = _round(result)
    (args.out / "scan.json").write_text(json.dumps(rounded, indent=1, sort_keys=False) + "\n",
                                        encoding="utf-8")
    from edgefinder import report
    report.write(rounded, args.out)
    print(f"selected: {result['selected']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
