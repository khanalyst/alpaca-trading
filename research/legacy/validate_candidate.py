"""Adversarial validation of the one candidate that survived the search.

Candidate: among instruments whose own 1h and 4h trends are both up
(`regime == trend_up`), go long the highest `range_pos_pct` (position inside
the trailing 24h high-low range) and short the lowest, held 16-48 hours.

The job here is to BREAK it, not to confirm it. A finding that survives a
search over ~2,000 hypotheses is exactly where a false positive is most
likely, so every test below is designed to make it fail:

- strip market beta out of the forward return;
- split the profit by leg and by quarter;
- express it the way the agent would actually trade it, with costs;
- re-run it with the signal shuffled, which must produce nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.edge_lab import load_dataset, universe_membership  # noqa: E402
from research.signal_lab import (  # noqa: E402
    ROUND_TRIP_TAKER_PCT, add_cross_sectional, build_panel, month_t_stat,
)

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)

HORIZONS = (64, 96, 192)          # 16h, 24h, 48h
TAIL_PCT = 10.0


def add_benchmark_forward(panel: pd.DataFrame, frames: dict) -> pd.DataFrame:
    """Attach BTC's forward return so market beta can be removed."""
    btc = frames["BTC/USDT:USDT"]
    open_ = btc.df["open"].to_numpy(float)
    lookup = {}
    for h in HORIZONS:
        entry = np.roll(open_, -1)
        exit_ = np.roll(open_, -(1 + h))
        fwd = (exit_ / entry - 1) * 100
        fwd[-(1 + h):] = np.nan
        lookup[h] = pd.Series(fwd, index=btc.ts)
    beta = np.clip(np.nan_to_num(panel["beta"].to_numpy(float), nan=1.0),
                   -3, 3)
    for h in HORIZONS:
        panel[f"btcfwd_{h}"] = panel["ts"].map(lookup[h])
        panel[f"resid_fwd_{h}"] = (
            panel[f"fwd_{h}"] - beta * panel[f"btcfwd_{h}"])
    return panel


def legs(frame: pd.DataFrame, target: str,
         tail_pct: float = TAIL_PCT) -> tuple[pd.DataFrame, pd.DataFrame]:
    sub = frame[frame["regime"] == "trend_up"]
    sub = sub[sub["range_pos"].notna() & sub[target].notna()]
    if sub.empty:
        return sub, sub
    lo = sub["range_pos"].quantile(tail_pct / 100)
    hi = sub["range_pos"].quantile(1 - tail_pct / 100)
    return sub[sub["range_pos"] >= hi], sub[sub["range_pos"] <= lo]


def spread_stats(frame: pd.DataFrame, target: str, label: str) -> dict:
    top, bottom = legs(frame, target)
    if top.empty or bottom.empty:
        return {"label": label, "trades": 0}
    values = np.concatenate([top[target].to_numpy(float),
                             -bottom[target].to_numpy(float)])
    months = np.concatenate([top["month"].to_numpy(),
                             bottom["month"].to_numpy()])
    return {
        "label": label,
        "trades": int(len(values)),
        "gross_pct": float(values.mean()),
        "net_taker_pct": float(values.mean()) - ROUND_TRIP_TAKER_PCT,
        "t_stat": month_t_stat(values, months),
        "long_leg_pct": float(top[target].mean()),
        "short_leg_pct": float(-bottom[target].mean()),
    }


def implementable(panel: pd.DataFrame, horizon: int, n_side: int,
                  rebalance: int, cost: float,
                  long_only: bool = False) -> dict:
    """Trade it the way the agent could: N per side, rebalanced, with costs."""
    sub = panel[(panel["regime"] == "trend_up")
                & panel["range_pos"].notna()
                & panel[f"fwd_{horizon}"].notna()]
    if sub.empty:
        return {"rebalances": 0}
    bars = np.sort(panel["ts"].unique())[::rebalance]
    sub = sub[sub["ts"].isin(bars)]
    returns, months, widths = [], [], []
    for ts, block in sub.groupby("ts", observed=True):
        if len(block) < (n_side if long_only else 2 * n_side):
            continue
        ordered = block.sort_values("range_pos")
        longs = ordered.tail(n_side)[f"fwd_{horizon}"].to_numpy(float)
        if long_only:
            value = longs.mean() - cost
        else:
            shorts = ordered.head(n_side)[f"fwd_{horizon}"].to_numpy(float)
            value = np.concatenate([longs, -shorts]).mean() - cost
        returns.append(value)
        months.append(block["month"].iloc[0])
        widths.append(len(block))
    if len(returns) < 40:
        return {"rebalances": len(returns)}
    values = np.array(returns)
    per_year = 365.25 * 24 / (rebalance * 0.25)
    return {
        "rebalances": int(len(values)),
        "avg_candidates": float(np.mean(widths)),
        "net_pct_per_cycle": float(values.mean()),
        "t_stat": month_t_stat(values, np.array(months)),
        "hit_rate_pct": float((values > 0).mean() * 100),
        "approx_annual_pct": float(values.mean() * per_year),
        "worst_cycle_pct": float(values.min()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", type=Path,
                        default=Path("runtime/research/validate_candidate.json"))
    args = parser.parse_args()

    frames = load_dataset(args.data)
    membership = universe_membership(frames)
    panel = build_panel(frames, membership)
    panel = add_cross_sectional(panel, ("rev_16b",))
    panel = add_benchmark_forward(panel, frames)

    start, end = panel["ts"].min(), panel["ts"].max()
    cut = start + (end - start) * 0.6
    purge = 192 * 900_000
    halves = {
        "in_sample": panel[panel["ts"] <= cut],
        "out_of_sample": panel[panel["ts"] > cut + purge],
        "full": panel,
    }
    report: dict = {}

    print("=" * 105)
    print("TEST 1 - REPLICATION: raw forward return")
    print("=" * 105)
    rows = []
    for horizon in HORIZONS:
        for name, half in halves.items():
            rows.append({"horizon_h": horizon * 0.25,
                         **spread_stats(half, f"fwd_{horizon}", name)})
    raw = pd.DataFrame(rows)
    print(raw.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    report["raw"] = raw.to_dict("records")

    print("\n" + "=" * 105)
    print("TEST 2 - BETA-NEUTRAL: same trade, market exposure removed")
    print("=" * 105)
    print("If this was really a long-high-beta bet in a rising market, "
          "it dies here.")
    rows = []
    for horizon in HORIZONS:
        for name, half in halves.items():
            rows.append({
                "horizon_h": horizon * 0.25,
                **spread_stats(half, f"resid_fwd_{horizon}", name)})
    resid = pd.DataFrame(rows)
    print(resid.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    report["beta_neutral"] = resid.to_dict("records")

    print("\n" + "=" * 105)
    print("TEST 3 - STABILITY BY QUARTER (48h hold)")
    print("=" * 105)
    work = panel.copy()
    work["quarter"] = pd.to_datetime(
        work["ts"], unit="ms", utc=True).dt.to_period("Q").astype(str)
    rows = []
    for quarter, block in work.groupby("quarter"):
        stats = spread_stats(block, "fwd_192", quarter)
        neutral = spread_stats(block, "resid_fwd_192", quarter)
        rows.append({
            "quarter": quarter, "trades": stats.get("trades", 0),
            "gross_pct": stats.get("gross_pct"),
            "long_leg_pct": stats.get("long_leg_pct"),
            "short_leg_pct": stats.get("short_leg_pct"),
            "beta_neutral_pct": neutral.get("gross_pct"),
        })
    quarters = pd.DataFrame(rows)
    print(quarters.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    raw_pos = (quarters["gross_pct"] > 0).sum()
    neutral_pos = (quarters["beta_neutral_pct"] > 0).sum()
    print(f"\nquarters positive - raw: {raw_pos}/{len(quarters)}   "
          f"beta-neutral: {neutral_pos}/{len(quarters)}")
    report["quarters"] = quarters.to_dict("records")

    print("\n" + "=" * 105)
    print("TEST 4 - IMPLEMENTABLE FORM (agent-sized, after taker cost)")
    print("=" * 105)
    rows = []
    for horizon in HORIZONS:
        for n_side in (1, 2):
            for long_only in (False, True):
                for name in ("in_sample", "out_of_sample"):
                    result = implementable(
                        halves[name], horizon, n_side, horizon,
                        ROUND_TRIP_TAKER_PCT, long_only)
                    if result.get("rebalances", 0) < 40:
                        continue
                    rows.append({
                        "half": name, "hold_h": horizon * 0.25,
                        "n_side": n_side,
                        "mode": "long_only" if long_only else "long_short",
                        "cycles": result["rebalances"],
                        "net_pct_per_cycle": result["net_pct_per_cycle"],
                        "t_stat": result["t_stat"],
                        "hit_rate_pct": result["hit_rate_pct"],
                        "approx_annual_pct": result["approx_annual_pct"],
                    })
    portfolio = pd.DataFrame(rows)
    if portfolio.empty:
        print("no configuration produced enough rebalances")
    else:
        print(portfolio.to_string(index=False,
                                  float_format=lambda v: f"{v:.4f}"))
        pivot = portfolio.pivot_table(
            index=["hold_h", "n_side", "mode"], columns="half",
            values="net_pct_per_cycle")
        if {"in_sample", "out_of_sample"}.issubset(pivot.columns):
            ok = pivot[(pivot["in_sample"] > 0) & (pivot["out_of_sample"] > 0)]
            print("\n--- configurations positive in BOTH halves ---")
            print(ok.to_string(float_format=lambda v: f"{v:.4f}")
                  if not ok.empty else "none")
    report["implementable"] = portfolio.to_dict("records")

    print("\n" + "=" * 105)
    print("TEST 5 - PLACEBO: identical procedure on a shuffled signal")
    print("=" * 105)
    print("Shuffling range_pos across instruments within each timestamp "
          "destroys the signal and keeps everything else. Must be ~zero.")
    rng = np.random.default_rng(23)
    placebo = panel.copy()
    placebo["range_pos"] = placebo.groupby("ts", observed=True)["range_pos"] \
        .transform(lambda s: rng.permutation(s.to_numpy()))
    rows = []
    for horizon in HORIZONS:
        rows.append({"horizon_h": horizon * 0.25,
                     **spread_stats(placebo, f"fwd_{horizon}",
                                    "placebo_full")})
    placebo_frame = pd.DataFrame(rows)
    print(placebo_frame.to_string(index=False,
                                  float_format=lambda v: f"{v:.4f}"))
    report["placebo"] = placebo_frame.to_dict("records")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
