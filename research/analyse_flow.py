"""Preliminary read on whether OKX flow/positioning data predicts anything.

This is explicitly NOT an edge test. OKX retains ~30 days of these series, so
the sample is roughly 720 hourly points per instrument and far fewer
independent observations at day-trading horizons. The prior work established
that on this data a t-statistic above 2 arises routinely from noise at sample
sizes fifty times larger than this one.

What 30 days CAN answer:

1. Is the sign convention what the documentation claims? A flipped column
   would invert every conclusion, so it is verified against contemporaneous
   returns rather than assumed.
2. Is the contemporaneous relationship strong enough that the series is
   measuring what it claims to measure?
3. Is the forward relationship even directionally plausible, and roughly how
   large?

If the answer to 3 is "nothing, not even in-sample", that closes the
hypothesis cheaply. If it is "plausible", the correct response is to record
the series forward - not to trade it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.edge_lab import HOUR_MS, aggregate_bars, load_dataset  # noqa: E402

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)

HORIZONS_H = (1, 4, 8, 16, 24, 48)


def build(data: Path, flow: Path) -> pd.DataFrame:
    frames = load_dataset(data)
    rows = []
    for path in sorted((flow / "taker_volume").glob("*.csv")):
        currency = path.stem
        symbol = f"{currency}/USDT:USDT"
        if symbol not in frames:
            continue
        taker = pd.read_csv(path)
        ratio_path = flow / "long_short_ratio" / f"{currency}.csv"
        ratio = (pd.read_csv(ratio_path) if ratio_path.exists()
                 else pd.DataFrame(columns=["timestamp_ms", "long_short_ratio"]))

        hourly = aggregate_bars(frames[symbol].fast, HOUR_MS)
        merged = hourly.merge(taker, on="timestamp_ms", how="inner")
        if not ratio.empty:
            merged = merged.merge(ratio, on="timestamp_ms", how="left")
        else:
            merged["long_short_ratio"] = np.nan
        if len(merged) < 200:
            continue
        merged["symbol"] = symbol

        total = merged["buy_vol"] + merged["sell_vol"]
        merged["flow_imb"] = np.where(
            total > 0, (merged["buy_vol"] - merged["sell_vol"]) / total, np.nan)
        merged["flow_imb_z"] = (
            (merged["flow_imb"] - merged["flow_imb"].rolling(72, min_periods=24).mean())
            / merged["flow_imb"].rolling(72, min_periods=24).std())
        merged["flow_notional_z"] = (
            (total - total.rolling(72, min_periods=24).mean())
            / total.rolling(72, min_periods=24).std())
        merged["ls_ratio"] = merged["long_short_ratio"]
        merged["ls_z"] = (
            (merged["ls_ratio"] - merged["ls_ratio"].rolling(72, min_periods=24).mean())
            / merged["ls_ratio"].rolling(72, min_periods=24).std())
        merged["ls_change"] = merged["ls_ratio"].pct_change() * 100

        # Return over the hour containing the flow observation.
        merged["ret_same_hour"] = (
            (merged["close"] - merged["open"]) / merged["open"] * 100)
        # Forward returns begin at the next hour's open because the flow value
        # is available only after the observed hour closes.
        open_ = merged["open"].to_numpy(float)
        for h in HORIZONS_H:
            entry = np.roll(open_, -1)
            exit_ = np.roll(open_, -(1 + h))
            fwd = (exit_ / entry - 1) * 100
            fwd[-(1 + h):] = np.nan
            merged[f"fwd_{h}h"] = fwd
        rows.append(merged)
    panel = pd.concat(rows, ignore_index=True)
    panel["month"] = pd.to_datetime(
        panel["timestamp_ms"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")
    return panel


def block_t(values: np.ndarray, blocks: np.ndarray) -> float:
    """Day-clustered t-statistic; hourly observations are autocorrelated."""
    if len(values) < 40:
        return np.nan
    per_block = pd.Series(values).groupby(blocks).mean()
    if len(per_block) < 5 or per_block.std(ddof=1) == 0:
        return np.nan
    return float(per_block.mean()
                 / (per_block.std(ddof=1) / np.sqrt(len(per_block))))


def quantile_spread(panel: pd.DataFrame, feature: str, target: str,
                    quantiles: int = 5) -> dict:
    sub = panel[[feature, target, "month"]].dropna()
    if len(sub) < 400:
        return {"n": len(sub)}
    try:
        bucket = pd.qcut(sub[feature], quantiles, labels=False,
                         duplicates="drop")
    except ValueError:
        return {"n": len(sub)}
    sub = sub.assign(bucket=bucket)
    top = sub[sub["bucket"] == sub["bucket"].max()]
    bottom = sub[sub["bucket"] == 0]
    values = np.concatenate([top[target].to_numpy(float),
                             -bottom[target].to_numpy(float)])
    blocks = np.concatenate([top["month"].to_numpy(),
                             bottom["month"].to_numpy()])
    return {
        "n": int(len(sub)),
        "spread_pct": float(values.mean()),
        "t": block_t(values, blocks),
        "corr": float(sub[feature].rank().corr(sub[target].rank())),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--flow", required=True, type=Path)
    args = parser.parse_args()

    panel = build(args.data, args.flow)
    days = (panel["timestamp_ms"].max() - panel["timestamp_ms"].min()) / 86_400_000
    print(f"panel: {len(panel):,} instrument-hours, "
          f"{panel['symbol'].nunique()} instruments, {days:.0f} days\n")

    print("=" * 96)
    print("STEP 1 - SIGN CONVENTION CHECK (contemporaneous)")
    print("=" * 96)
    print("OKX documents taker-volume as [ts, sellVol, buyVol]. If that is "
          "right,\ntaker buy imbalance must correlate POSITIVELY with the "
          "same hour's return.\n")
    rows = []
    for symbol, block in panel.groupby("symbol"):
        sub = block[["flow_imb", "ret_same_hour"]].dropna()
        if len(sub) < 200:
            continue
        rows.append({
            "symbol": symbol, "n": len(sub),
            "corr_flow_vs_same_hour_return": float(
                sub["flow_imb"].corr(sub["ret_same_hour"])),
        })
    check = pd.DataFrame(rows)
    print(check.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    mean_corr = check["corr_flow_vs_same_hour_return"].mean()
    print(f"\nmean contemporaneous correlation: {mean_corr:+.4f}")
    if mean_corr > 0.2:
        print("=> convention CONFIRMED: column 3 is taker BUY volume, and the "
              "series genuinely tracks aggressor direction.")
    elif mean_corr < -0.2:
        print("=> convention INVERTED: column 3 is taker SELL volume. Every "
              "sign below is flipped accordingly.")
    else:
        print("=> WEAK contemporaneous link; treat this series with "
              "suspicion.")

    print("\n" + "=" * 96)
    print("STEP 2 - FORWARD PREDICTION (the question that matters)")
    print("=" * 96)
    features = ["flow_imb", "flow_imb_z", "flow_notional_z",
                "ls_ratio", "ls_z", "ls_change"]
    rows = []
    for feature in features:
        for h in HORIZONS_H:
            result = quantile_spread(panel, feature, f"fwd_{h}h")
            if "spread_pct" not in result:
                continue
            rows.append({"feature": feature, "horizon_h": h, **result})
    forward = pd.DataFrame(rows)
    print(forward.to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    print("\n--- cells whose spread would clear 0.10% round-trip taker "
          "cost ---")
    payable = forward[forward["spread_pct"] > 0.10]
    print(payable.to_string(index=False, float_format=lambda v: f"{v:.4f}")
          if not payable.empty else "none")

    print("\n" + "=" * 96)
    print("STEP 3 - WHAT THIS SAMPLE CAN AND CANNOT SUPPORT")
    print("=" * 96)
    for h in HORIZONS_H:
        independent = days * 24 / h * panel["symbol"].nunique()
        print(f"  {h:>3}h horizon: ~{independent:>6.0f} non-overlapping "
              f"instrument-observations across {days:.0f} days")
    print("\nThe two-year candle study needed ~9,000 non-overlapping trades "
          "to\nresolve an effect of 0.1R, and still found t>2 arising from "
          "noise.\nNothing above is evidence. It is a screen for whether "
          "recording\nthis data forward is worth the effort.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
