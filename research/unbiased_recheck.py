"""Re-test the candidate with the time-selection bias removed.

The placebo in validate_candidate.py exposed a flaw in the tail method: the
top and bottom decile thresholds were computed on the *pooled* distribution
across all time. `range_pos` moves together across instruments - in a strong
up-market nearly everything sits near its 24h high - so the pooled top decile
preferentially samples bullish moments and the pooled bottom decile bearish
ones. Long-top / short-bottom then harvests market timing rather than
cross-sectional selection, which is why a randomly shuffled signal still
scored +0.45% at 48h with t=2.4.

The fix is to rank **within each timestamp**, so both legs always come from
the same moment in time and market direction cancels by construction. This
re-runs the candidate, and the placebo, that way.
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
    ROUND_TRIP_TAKER_PCT, build_panel, month_t_stat,
)

pd.set_option("display.width", 200)
HORIZONS = (16, 32, 64, 96, 192)


def paired_spread(panel: pd.DataFrame, signal: str, horizon: int,
                  condition: str | None, label: str) -> dict:
    """Long the best / short the worst *within the same timestamp*.

    Both legs are drawn from one moment, so any market-wide move affects them
    equally and cancels. Whatever is left is genuine cross-sectional skill.
    """
    sub = panel
    if condition is not None:
        sub = sub[sub["regime"] == condition]
    sub = sub[sub[signal].notna() & sub[f"fwd_{horizon}"].notna()]
    if sub.empty:
        return {"label": label, "pairs": 0}
    values, months = [], []
    for ts, block in sub.groupby("ts", observed=True):
        if len(block) < 4:            # need real breadth to rank against
            continue
        ordered = block.sort_values(signal)
        long_leg = float(ordered.iloc[-1][f"fwd_{horizon}"])
        short_leg = float(ordered.iloc[0][f"fwd_{horizon}"])
        values.append((long_leg - short_leg) / 2)
        months.append(block["month"].iloc[0])
    if len(values) < 200:
        return {"label": label, "pairs": len(values)}
    array = np.array(values)
    return {
        "label": label,
        "pairs": int(len(array)),
        "gross_pct": float(array.mean()),
        "net_taker_pct": float(array.mean()) - ROUND_TRIP_TAKER_PCT,
        "t_stat": month_t_stat(array, np.array(months)),
        "hit_rate_pct": float((array > 0).mean() * 100),
    }


def non_overlapping(panel: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Keep one observation per horizon-length block so trades are independent."""
    bars = np.sort(panel["ts"].unique())[::horizon]
    return panel[panel["ts"].isin(bars)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", type=Path,
                        default=Path("runtime/research/unbiased_recheck.json"))
    args = parser.parse_args()

    frames = load_dataset(args.data)
    membership = universe_membership(frames)
    panel = build_panel(frames, membership)

    start, end = panel["ts"].min(), panel["ts"].max()
    cut = start + (end - start) * 0.6
    purge = 192 * 900_000
    halves = {
        "in_sample": panel[panel["ts"] <= cut],
        "out_of_sample": panel[panel["ts"] > cut + purge],
        "full": panel,
    }

    rng = np.random.default_rng(23)
    for name, half in list(halves.items()):
        shuffled = half.copy()
        shuffled["range_pos_shuffled"] = (
            shuffled.groupby("ts", observed=True)["range_pos"]
            .transform(lambda s: rng.permutation(s.to_numpy())))
        halves[name] = shuffled

    report = {}
    for condition, title in ((("trend_up"), "conditioned on regime=trend_up"),
                             (None, "unconditional")):
        print("=" * 100)
        print(f"TIMESTAMP-PAIRED SPREAD - {title}")
        print("=" * 100)
        rows = []
        for horizon in HORIZONS:
            for name, half in halves.items():
                independent = non_overlapping(half, horizon)
                for signal, kind in (("range_pos", "real"),
                                     ("range_pos_shuffled", "placebo")):
                    result = paired_spread(
                        independent, signal, horizon, condition,
                        f"{name}/{kind}")
                    if result.get("pairs", 0) < 200:
                        continue
                    rows.append({"horizon_h": horizon * 0.25,
                                 "half": name, "signal": kind, **result})
        frame = pd.DataFrame(rows)
        if frame.empty:
            print("insufficient independent observations")
            continue
        show = ["horizon_h", "half", "signal", "pairs", "gross_pct",
                "net_taker_pct", "t_stat", "hit_rate_pct"]
        print(frame[show].to_string(
            index=False, float_format=lambda v: f"{v:.4f}"))
        report[title] = frame[show].to_dict("records")

        real = frame[frame["signal"] == "real"]
        winners = real[(real["net_taker_pct"] > 0) & (real["t_stat"] > 2)]
        print(f"\nreal-signal cells clearing taker cost with t>2: "
              f"{len(winners)} of {len(real)}")
        print()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
