"""Second-pass edge search: tails, conditioning, and implementable form.

The first pass scored linear quintile spreads and found nothing that cleared
both false-discovery control and taker cost. Three things could still hide a
real edge, and each is tested here:

1. **Tails.** A quintile averages away the extremes. Real dislocations may
   live in the top and bottom few percent.
2. **Conditioning.** A predictor can work only in a regime or a session and
   be diluted to nothing when pooled.
3. **Implementable form.** Quantile spreads assume you trade every name every
   bar. The agent holds at most 3 positions and rebalances on a cycle, so a
   candidate has to survive being expressed that way, with a stop, inside a
   48h maximum hold.

Everything is scored in-sample, confirmed out-of-sample, and reported with
the number of hypotheses tested.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.edge_lab import load_dataset, universe_membership  # noqa: E402
from research.signal_lab import (  # noqa: E402
    ROUND_TRIP_MAKER_PCT, ROUND_TRIP_TAKER_PCT, add_cross_sectional,
    build_panel, month_t_stat,
)

pd.set_option("display.width", 230)
pd.set_option("display.max_columns", 40)

CANDIDATES = [
    "rev_1b", "rev_4b", "rev_16b", "rev_96b", "rev_ema",
    "residrev_4b", "residrev_16b", "residrev_96b",
    "ret_4b", "ret_16b", "ret_96b", "ret_192b",
    "atr_ratio", "range_pos", "close_loc", "consec_bars",
    "rel_vol", "vol_z", "amihud", "bar_range_atr", "gap",
    "funding_8h",
    "xs_rev_4b", "xs_rev_16b", "xs_rev_96b",
    "xs_residrev_4b", "xs_residrev_16b", "xs_residrev_96b",
    "xs_ret_16b", "xs_vol_z", "xs_funding_8h",
]
HORIZONS = (4, 8, 16, 32, 64, 96, 192)


def tail_spread(frame: pd.DataFrame, feature: str, horizon: int,
                tail_pct: float) -> dict | None:
    column = frame[feature]
    target = frame[f"fwd_{horizon}"]
    ok = column.notna() & target.notna()
    if ok.sum() < 3000:
        return None
    sub = frame.loc[ok]
    lo = sub[feature].quantile(tail_pct / 100)
    hi = sub[feature].quantile(1 - tail_pct / 100)
    if not np.isfinite(lo) or not np.isfinite(hi) or lo >= hi:
        return None
    top = sub[sub[feature] >= hi]
    bottom = sub[sub[feature] <= lo]
    if len(top) < 300 or len(bottom) < 300:
        return None
    spread = np.concatenate([
        top[f"fwd_{horizon}"].to_numpy(float),
        -bottom[f"fwd_{horizon}"].to_numpy(float)])
    months = np.concatenate([top["month"].to_numpy(),
                             bottom["month"].to_numpy()])
    gross = float(spread.mean())
    return {
        "feature": feature, "horizon_bars": horizon,
        "horizon_hours": horizon * 0.25, "tail_pct": tail_pct,
        "trades": int(len(spread)), "gross_pct": gross,
        "net_taker_pct": gross - ROUND_TRIP_TAKER_PCT,
        "t_stat": month_t_stat(spread, months),
        "hit_rate_pct": float((spread > 0).mean() * 100),
    }


def scan_tails(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in CANDIDATES:
        if feature not in frame:
            continue
        for horizon in HORIZONS:
            for tail in (10.0, 5.0, 2.0):
                result = tail_spread(frame, feature, horizon, tail)
                if result:
                    rows.append(result)
    return pd.DataFrame(rows)


def scan_conditioned(frame: pd.DataFrame) -> pd.DataFrame:
    """Same quintile spread, but inside one regime or session at a time."""
    rows = []
    frame = frame.copy()
    frame["session"] = pd.cut(
        frame["hour"], bins=[-1, 6, 12, 20, 23],
        labels=["asia", "eu", "us", "late"])
    for column, values in (("regime", frame["regime"].dropna().unique()),
                           ("session", frame["session"].dropna().unique())):
        for value in values:
            slice_ = frame[frame[column] == value]
            if len(slice_) < 20000:
                continue
            for feature in CANDIDATES:
                if feature not in slice_:
                    continue
                for horizon in (16, 32, 64, 96, 192):
                    result = tail_spread(slice_, feature, horizon, 10.0)
                    if result:
                        result["condition"] = f"{column}={value}"
                        rows.append(result)
    return pd.DataFrame(rows)


def top_n_portfolio(panel: pd.DataFrame, feature: str, horizon: int,
                    n_side: int, rebalance_bars: int,
                    cost_pct: float) -> dict | None:
    """The implementable form: long the best N, short the worst N, held.

    Rebalancing every `rebalance_bars` and holding `horizon` bars mirrors what
    the agent can actually do with a small number of concurrent positions.
    """
    needed = [feature, f"fwd_{horizon}", "ts", "month", "symbol"]
    sub = panel[needed].dropna()
    if sub.empty:
        return None
    bars = np.sort(sub["ts"].unique())
    chosen = bars[::rebalance_bars]
    sub = sub[sub["ts"].isin(chosen)]
    if len(sub) < 2000:
        return None
    grouped = sub.groupby("ts", observed=True)
    legs = []
    months = []
    for ts, block in grouped:
        if len(block) < 2 * n_side:
            continue
        ordered = block.sort_values(feature)
        shorts = ordered.head(n_side)[f"fwd_{horizon}"].to_numpy(float)
        longs = ordered.tail(n_side)[f"fwd_{horizon}"].to_numpy(float)
        legs.append(np.concatenate([longs, -shorts]).mean() - cost_pct)
        months.append(block["month"].iloc[0])
    if len(legs) < 50:
        return None
    values = np.array(legs)
    per_year = 365.25 * 24 / (rebalance_bars * 0.25)
    return {
        "feature": feature, "horizon_hours": horizon * 0.25,
        "n_side": n_side, "rebalance_hours": rebalance_bars * 0.25,
        "rebalances": int(len(values)),
        "net_pct_per_rebalance": float(values.mean()),
        "t_stat": month_t_stat(values, np.array(months)),
        "hit_rate_pct": float((values > 0).mean() * 100),
        "approx_annual_pct": float(values.mean() * per_year),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", type=Path,
                        default=Path("runtime/research/deep_edge.json"))
    args = parser.parse_args()

    frames = load_dataset(args.data)
    membership = universe_membership(frames)
    panel = build_panel(frames, membership)
    panel = add_cross_sectional(panel, (
        "rev_4b", "rev_16b", "rev_96b",
        "residrev_4b", "residrev_16b", "residrev_96b",
        "ret_16b", "vol_z", "funding_8h"))

    start, end = panel["ts"].min(), panel["ts"].max()
    cut = start + (end - start) * 0.6
    purge = 192 * 900_000
    is_panel = panel[panel["ts"] <= cut]
    oos_panel = panel[panel["ts"] > cut + purge]
    print(f"panel {len(panel):,} rows | IS {len(is_panel):,} | "
          f"OOS {len(oos_panel):,}")

    print("\n" + "=" * 110)
    print("A. EXTREME TAILS - does the edge live in the top/bottom few percent?")
    print("=" * 110)
    is_tails = scan_tails(is_panel)
    oos_tails = scan_tails(oos_panel)
    merged = is_tails.merge(
        oos_tails, on=["feature", "horizon_bars", "tail_pct"],
        suffixes=("_is", "_oos"))
    print(f"hypotheses tested: {len(merged)}")
    cols = ["feature", "horizon_hours_is", "tail_pct", "trades_is",
            "gross_pct_is", "t_stat_is", "gross_pct_oos", "t_stat_oos",
            "net_taker_pct_oos"]
    print("\n--- top 15 in-sample, with their out-of-sample result ---")
    print(merged.sort_values("gross_pct_is", ascending=False).head(15)[cols]
          .to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    both = merged[(merged["net_taker_pct_is"] > 0)
                  & (merged["net_taker_pct_oos"] > 0)
                  & (merged["t_stat_is"] > 2) & (merged["t_stat_oos"] > 2)]
    print(f"\n--- clear taker cost AND t>2 in BOTH halves: {len(both)} ---")
    print(both[cols].to_string(index=False,
                               float_format=lambda v: f"{v:.4f}")
          if not both.empty else "none")

    print("\n" + "=" * 110)
    print("B. CONDITIONING - does anything work inside a regime or session?")
    print("=" * 110)
    is_cond = scan_conditioned(is_panel)
    oos_cond = scan_conditioned(oos_panel)
    if not is_cond.empty and not oos_cond.empty:
        cmerged = is_cond.merge(
            oos_cond, on=["feature", "horizon_bars", "condition"],
            suffixes=("_is", "_oos"))
        print(f"hypotheses tested: {len(cmerged)}")
        ccols = ["condition", "feature", "horizon_hours_is", "trades_is",
                 "gross_pct_is", "t_stat_is", "gross_pct_oos", "t_stat_oos",
                 "net_taker_pct_oos"]
        print("\n--- top 15 in-sample ---")
        print(cmerged.sort_values("gross_pct_is", ascending=False).head(15)[
            ccols].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
        cboth = cmerged[(cmerged["net_taker_pct_is"] > 0)
                        & (cmerged["net_taker_pct_oos"] > 0)
                        & (cmerged["t_stat_is"] > 2)
                        & (cmerged["t_stat_oos"] > 2)]
        print(f"\n--- clear taker cost AND t>2 in BOTH halves: {len(cboth)} ---")
        print(cboth[ccols].to_string(index=False,
                                     float_format=lambda v: f"{v:.4f}")
              if not cboth.empty else "none")
    else:
        cmerged = pd.DataFrame()

    print("\n" + "=" * 110)
    print("C. IMPLEMENTABLE FORM - long best N / short worst N, agent-sized")
    print("=" * 110)
    rows = []
    for feature in CANDIDATES:
        if feature not in panel:
            continue
        for horizon, rebalance in ((16, 16), (32, 32), (64, 64),
                                   (96, 96), (192, 192)):
            for n_side in (1, 2):
                is_result = top_n_portfolio(
                    is_panel, feature, horizon, n_side, rebalance,
                    ROUND_TRIP_TAKER_PCT)
                oos_result = top_n_portfolio(
                    oos_panel, feature, horizon, n_side, rebalance,
                    ROUND_TRIP_TAKER_PCT)
                if not is_result or not oos_result:
                    continue
                rows.append({
                    "feature": feature,
                    "hold_h": horizon * 0.25, "n_side": n_side,
                    "is_net_pct": is_result["net_pct_per_rebalance"],
                    "is_t": is_result["t_stat"],
                    "is_annual_pct": is_result["approx_annual_pct"],
                    "oos_net_pct": oos_result["net_pct_per_rebalance"],
                    "oos_t": oos_result["t_stat"],
                    "oos_annual_pct": oos_result["approx_annual_pct"],
                })
    portfolio = pd.DataFrame(rows)
    if not portfolio.empty:
        print(f"hypotheses tested: {len(portfolio)}")
        print("\n--- top 15 by in-sample net, with out-of-sample ---")
        print(portfolio.sort_values("is_net_pct", ascending=False).head(15)
              .to_string(index=False, float_format=lambda v: f"{v:.4f}"))
        good = portfolio[(portfolio["is_net_pct"] > 0)
                         & (portfolio["oos_net_pct"] > 0)
                         & (portfolio["is_t"] > 1.5)
                         & (portfolio["oos_t"] > 1.5)]
        print(f"\n--- positive net in BOTH halves with t>1.5 in both: "
              f"{len(good)} ---")
        print(good.to_string(index=False, float_format=lambda v: f"{v:.4f}")
              if not good.empty else "none")

    payload = {
        "tails_tested": int(len(merged)),
        "tails_surviving": int(len(both)),
        "conditioned_tested": int(len(cmerged)),
        "portfolio_tested": int(len(portfolio)) if not portfolio.empty else 0,
        "tails_top": merged.sort_values(
            "gross_pct_is", ascending=False).head(30)[cols].to_dict("records"),
        "portfolio_top": (portfolio.sort_values("oos_net_pct", ascending=False)
                          .head(30).to_dict("records")
                          if not portfolio.empty else []),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
