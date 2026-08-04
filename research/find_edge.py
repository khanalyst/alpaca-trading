"""Run the edge search and report what, if anything, survives.

Discovery happens on the in-sample period only. Everything that looks good
there is then re-measured out-of-sample, and the whole search is corrected
for false discovery rate, because scoring hundreds of feature/horizon pairs
will always produce good-looking noise.
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
    FEATURES, HORIZONS, ROUND_TRIP_MAKER_PCT, ROUND_TRIP_TAKER_PCT,
    add_cross_sectional, benjamini_hochberg, build_panel, evaluate_pair,
    month_t_stat, two_sided_p,
)

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)


def split(panel: pd.DataFrame, fraction: float = 0.6,
          purge_bars: int = 192) -> tuple[pd.Series, pd.Series]:
    start, end = panel["ts"].min(), panel["ts"].max()
    cut = start + (end - start) * fraction
    purge = purge_bars * 900_000
    return panel["ts"] <= cut, panel["ts"] > cut + purge


def scan(panel: pd.DataFrame, label: str) -> pd.DataFrame:
    rows = []
    for feature in FEATURES:
        if feature not in panel:
            continue
        for horizon in HORIZONS:
            result = evaluate_pair(panel, feature, horizon)
            if result:
                rows.append(result)
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["period"] = label
    frame["p_value"] = frame["t_stat"].apply(two_sided_p)
    return frame


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", type=Path,
                        default=Path("runtime/research/find_edge.json"))
    args = parser.parse_args()

    print("loading ...")
    frames = load_dataset(args.data)
    membership = universe_membership(frames)
    panel = build_panel(frames, membership)
    panel = add_cross_sectional(panel, (
        "rev_4b", "rev_16b", "rev_96b",
        "residrev_4b", "residrev_16b", "residrev_96b",
        "ret_16b", "vol_z", "funding_8h",
        # Timestamp-wise ranks remove common market direction from the signal.
        "flush_fade", "build_follow"))
    print(f"panel: {len(panel):,} tradable symbol-bars, "
          f"{panel['symbol'].nunique()} instruments, "
          f"{panel['month'].nunique()} months")
    print(f"instruments per bar (median): "
          f"{panel.groupby('ts').size().median():.0f}")

    is_mask, oos_mask = split(panel)
    print(f"in-sample: {is_mask.sum():,} rows  "
          f"out-of-sample: {oos_mask.sum():,} rows")

    print("\n" + "=" * 100)
    print("STEP 1 - IN-SAMPLE SCAN (discovery only; nothing is believed yet)")
    print("=" * 100)
    in_sample = scan(panel[is_mask], "in_sample")
    if in_sample.empty:
        print("no evaluable pairs")
        return 1
    tested = len(in_sample)
    print(f"feature/horizon pairs tested: {tested}")

    ranked = in_sample.sort_values("gross_pct_per_trade", ascending=False)
    cols = ["feature", "horizon_hours", "trades", "rank_ic",
            "gross_pct_per_trade", "net_taker_pct", "t_stat", "hit_rate_pct"]
    print("\n--- top 20 by gross long/short spread per trade ---")
    print(ranked.head(20)[cols].to_string(
        index=False, float_format=lambda v: f"{v:.4f}"))

    print(f"\n--- pairs whose GROSS spread even exceeds taker cost "
          f"({ROUND_TRIP_TAKER_PCT}%) ---")
    payable = ranked[ranked["net_taker_pct"] > 0]
    print(f"{len(payable)} of {tested}")
    if not payable.empty:
        print(payable.head(25)[cols].to_string(
            index=False, float_format=lambda v: f"{v:.4f}"))

    # Control the false discovery rate across the entire search.
    keep = benjamini_hochberg(in_sample["p_value"].to_numpy(), alpha=0.10)
    in_sample = in_sample.assign(fdr_pass=keep)
    survivors = in_sample[
        in_sample["fdr_pass"] & (in_sample["net_taker_pct"] > 0)]
    print(f"\n--- survive BOTH FDR(10%) and taker cost: {len(survivors)} ---")
    if not survivors.empty:
        print(survivors.sort_values("net_taker_pct", ascending=False)[cols]
              .to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    print("\n" + "=" * 100)
    print("STEP 2 - OUT-OF-SAMPLE CONFIRMATION")
    print("=" * 100)
    out_sample = scan(panel[oos_mask], "out_of_sample")
    merged = in_sample.merge(
        out_sample, on=["feature", "horizon_bars"], suffixes=("_is", "_oos"))
    merged["consistent_sign"] = (
        np.sign(merged["gross_pct_per_trade_is"])
        == np.sign(merged["gross_pct_per_trade_oos"]))
    correlation = merged["gross_pct_per_trade_is"].corr(
        merged["gross_pct_per_trade_oos"])
    print(f"IS vs OOS gross-spread correlation across {len(merged)} pairs: "
          f"{correlation:.3f}")
    print(f"sign consistency: {100 * merged['consistent_sign'].mean():.1f}%")

    show = ["feature", "horizon_hours_is", "gross_pct_per_trade_is",
            "t_stat_is", "gross_pct_per_trade_oos", "t_stat_oos",
            "net_taker_pct_oos", "consistent_sign"]
    confirmed = merged[
        merged["fdr_pass"]
        & (merged["net_taker_pct_is"] > 0)
        & (merged["net_taker_pct_oos"] > 0)
        & merged["consistent_sign"]]
    print(f"\n--- CONFIRMED: FDR-significant in-sample, profitable after "
          f"taker cost in BOTH halves, same sign: {len(confirmed)} ---")
    if confirmed.empty:
        print("none")
    else:
        print(confirmed.sort_values("net_taker_pct_oos", ascending=False)[show]
              .to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    print("\n--- best 15 out-of-sample regardless of in-sample status ---")
    print(merged.sort_values("gross_pct_per_trade_oos", ascending=False)
          .head(15)[show].to_string(
              index=False, float_format=lambda v: f"{v:.4f}"))

    print("\n" + "=" * 100)
    print("STEP 3 - HOW BIG IS THE COST BARRIER?")
    print("=" * 100)
    best_gross = merged["gross_pct_per_trade_oos"].max()
    print(f"best out-of-sample gross spread per trade: {best_gross:.4f}%")
    print(f"round-trip taker cost:                     "
          f"{ROUND_TRIP_TAKER_PCT:.4f}%")
    print(f"round-trip maker cost:                     "
          f"{ROUND_TRIP_MAKER_PCT:.4f}%")
    for name, cost in (("taker", ROUND_TRIP_TAKER_PCT),
                       ("maker", ROUND_TRIP_MAKER_PCT)):
        count = int((merged["gross_pct_per_trade_oos"] > cost).sum())
        print(f"  pairs whose OOS gross spread clears {name} cost: "
              f"{count} / {len(merged)}")

    payload = {
        "pairs_tested": int(tested),
        "panel_rows": int(len(panel)),
        "is_oos_correlation": float(correlation),
        "sign_consistency_pct": float(100 * merged["consistent_sign"].mean()),
        "confirmed": confirmed[show].to_dict("records"),
        "in_sample_top": ranked.head(40)[cols].to_dict("records"),
        "out_of_sample_top": merged.sort_values(
            "gross_pct_per_trade_oos", ascending=False).head(40)[
                show].to_dict("records"),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
