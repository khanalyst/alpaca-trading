"""Cost the maker-execution redesign before anyone builds it.

Maker execution is the top-ranked lever in the edge-discovery report: 0.02%
per side against 0.05% taker is 0.06% of round-trip saving, set against
measured gross spreads of 0.05-0.3%. On paper it flips several results.

Paper is not the question. Resting an order buys the fee saving and sells two
things:

1. **Fills you do not get.** If price runs away, the trade never happens - and
   the trades that run away immediately are disproportionately the winners.
2. **Adverse selection.** You fill precisely when price comes back to you,
   which is correlated with the move going against you.

Both are measurable from bar data, and both are *subtractions* from the fee
saving. This decomposes the difference into three parts so the redesign can
be judged on the net rather than on the headline 0.06%:

    taker on every signal                     <- what the agent does today
    taker on the subset a maker would fill    <- isolates selection
    maker on that same subset                 <- adds the price/fee benefit

A second section tests the other recommendation - widening the universe -
rather than applying it on the strength of an argument.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.edge_lab import (  # noqa: E402
    COST_SCENARIOS, Contract, Costs, build_trades, derive_levels,
    evidence_masks, load_dataset, non_overlapping, research_gate, summarize,
    universe_membership,
)

pd.set_option("display.width", 210)
pd.set_option("display.max_columns", 40)

TAKER_FEE = 0.05
MAKER_FEE = 0.02


def signals(frames, membership, contract: Contract):
    """Every admissible setup, as (symbol, bar index, direction, stop, take)."""
    out = []
    for symbol, frame in frames.items():
        df = frame.df
        masks = evidence_masks(df, contract)
        live = membership[symbol] & (
            df["trend_4h"].to_numpy(object) != None)  # noqa: E711
        for setup in ("trend_continuation", "range_breakout"):
            for direction in ("long", "short"):
                mask = masks[f"{setup}_{direction}"] & live
                ext = masks[f"extension_{direction}"]
                mask &= ~np.isnan(ext) & (
                    ext <= contract.hard_max_entry_extension_atr)
                mask &= research_gate(df, contract, direction)
                stop, take = derive_levels(df, contract, direction, "fixed_rr")
                mask &= (~np.isnan(stop) & (stop >= contract.min_stop_pct)
                         & (stop <= contract.max_stop_pct)
                         & ~np.isnan(take) & (take <= 50.0))
                idx = np.where(mask)[0]
                if len(idx):
                    out.append((symbol, idx, direction, stop[idx], take[idx]))
    return out


def run_from_entry(frame, entry_idx, entry_price, direction, stop_pct,
                   take_pct, fee_per_side, exit_slip, stop_slip,
                   max_hold_bars):
    """Simulate stop/target/timeout from an already-determined entry fill."""
    n = len(frame.df)
    sign = 1.0 if direction == "long" else -1.0
    high = frame.df["high"].to_numpy(float)
    low = frame.df["low"].to_numpy(float)
    close = frame.df["close"].to_numpy(float)
    open_ = frame.df["open"].to_numpy(float)

    stop_level = entry_price * (1 - stop_pct / 100 * sign)
    take_level = entry_price * (1 + take_pct / 100 * sign)
    horizon = np.arange(max_hold_bars)
    grid = entry_idx[:, None] + horizon[None, :]
    valid = grid < n
    clipped = np.clip(grid, 0, n - 1)
    win_high, win_low, win_open = high[clipped], low[clipped], open_[clipped]

    if direction == "long":
        stop_hit = (win_low <= stop_level[:, None]) & valid
        take_hit = (win_high >= take_level[:, None]) & valid
    else:
        stop_hit = (win_high >= stop_level[:, None]) & valid
        take_hit = (win_low <= take_level[:, None]) & valid
    big = max_hold_bars + 10
    first_stop = np.where(stop_hit.any(1), stop_hit.argmax(1), big)
    first_take = np.where(take_hit.any(1), take_hit.argmax(1), big)
    stopped = first_stop <= first_take
    took = (first_take < first_stop) & (first_take < big)
    last_valid = valid.sum(1) - 1
    offset = np.where(stopped & (first_stop < big), first_stop,
                      np.where(took, first_take, last_valid))
    exit_idx = np.minimum(entry_idx + offset, n - 1)
    reason = np.where(stopped & (first_stop < big), "stop",
                      np.where(took, "take_profit", "max_hold"))
    gap_open = win_open[np.arange(len(offset)), offset]
    stop_fill = (np.minimum(stop_level, gap_open) if direction == "long"
                 else np.maximum(stop_level, gap_open))
    raw_exit = np.where(reason == "stop", stop_fill,
                        np.where(reason == "take_profit", take_level,
                                 close[exit_idx]))
    slip = np.where(reason == "stop", stop_slip, exit_slip)
    exit_price = raw_exit * (1 - slip / 100 * sign)
    gross = sign * (exit_price / entry_price - 1) * 100
    fees = fee_per_side * (1 + exit_price / entry_price)
    net = gross - fees
    denom = np.maximum(stop_pct + 2 * fee_per_side + stop_slip, 0.05)
    return pd.DataFrame({
        "entry_ts": frame.ts[entry_idx], "net_pct": net,
        "r_multiple": net / denom, "exit_reason": reason,
        "bars_held": exit_idx - entry_idx + 1,
    })


def maker_study(frames, membership, contract: Contract,
                wait_bars: int, exit_slip: float, stop_slip: float,
                fill_margin_bps: float = 0.0) -> dict[str, pd.DataFrame]:
    """`fill_margin_bps` is how far price must trade THROUGH the limit.

    Bar data cannot see queue position, and a bar whose low merely touches a
    price fills almost nobody resting at it. Requiring penetration is a crude
    stand-in for "enough volume traded at this level to clear the queue ahead
    of you". Zero margin is the optimistic bound; larger margins are
    progressively more honest and progressively less flattering.
    """
    taker_all, taker_filled, maker_filled = [], [], []
    for symbol, idx, direction, stop_pct, take_pct in signals(
            frames, membership, contract):
        frame = frames[symbol]
        n = len(frame.df)
        close = frame.df["close"].to_numpy(float)
        open_ = frame.df["open"].to_numpy(float)
        high = frame.df["high"].to_numpy(float)
        low = frame.df["low"].to_numpy(float)
        sign = 1.0 if direction == "long" else -1.0

        keep = idx < n - contract.max_hold_bars - wait_bars - 2
        idx, stop_pct, take_pct = idx[keep], stop_pct[keep], take_pct[keep]
        if len(idx) == 0:
            continue

        # Taker execution crosses the spread at the next bar's open.
        taker_entry = open_[idx + 1] * (1 + exit_slip / 100 * sign)
        taker_all.append(run_from_entry(
            frame, idx + 1, taker_entry, direction, stop_pct, take_pct,
            TAKER_FEE, exit_slip, stop_slip, contract.max_hold_bars))

        # Maker execution rests at the signal close; a long fills only after
        # price trades down to the limit (adverse selection).
        limit = close[idx]
        trigger = limit * (1 - fill_margin_bps / 10_000 * sign)
        filled = np.zeros(len(idx), dtype=bool)
        fill_bar = np.full(len(idx), -1)
        for step in range(1, wait_bars + 1):
            bar = idx + step
            touched = (low[bar] <= trigger) if direction == "long" \
                else (high[bar] >= trigger)
            fresh = touched & ~filled
            fill_bar[fresh] = bar[fresh]
            filled |= touched
        if not filled.any():
            continue
        entry_idx = fill_bar[filled]
        maker_filled.append(run_from_entry(
            frame, entry_idx, limit[filled], direction,
            stop_pct[filled], take_pct[filled], MAKER_FEE,
            exit_slip, stop_slip, contract.max_hold_bars))
        # Reusing the same signals with taker fills isolates selection from
        # execution price.
        taker_filled.append(run_from_entry(
            frame, idx[filled] + 1,
            open_[idx[filled] + 1] * (1 + exit_slip / 100 * sign),
            direction, stop_pct[filled], take_pct[filled], TAKER_FEE,
            exit_slip, stop_slip, contract.max_hold_bars))

    return {
        "taker_all_signals": pd.concat(taker_all, ignore_index=True),
        "taker_on_fillable_subset": pd.concat(taker_filled, ignore_index=True),
        "maker_on_fillable_subset": pd.concat(maker_filled, ignore_index=True),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", type=Path,
                        default=Path("runtime/research/maker_study.json"))
    args = parser.parse_args()

    frames = load_dataset(args.data)
    membership = universe_membership(frames)
    contract = Contract(max_hold_bars=192, fixed_reward_risk=3.0)
    report = {}

    print("=" * 100)
    print("A. MAKER vs TAKER - decomposing the 0.06% fee saving")
    print("=" * 100)
    rows = []
    for wait_bars in (1, 2, 4, 8):
        results = maker_study(frames, membership, contract, wait_bars,
                              exit_slip=0.05, stop_slip=0.15)
        base = len(results["taker_all_signals"])
        subset = len(results["maker_on_fillable_subset"])
        for label, trades in results.items():
            stats = summarize(trades, bootstrap=False)
            rows.append({
                "wait_bars": wait_bars,
                "wait_minutes": wait_bars * 15,
                "variant": label,
                "trades": stats["trades"],
                "fill_rate_pct": (100.0 if label == "taker_all_signals"
                                  else round(subset / max(base, 1) * 100, 1)),
                "win_rate_pct": round(stats["win_rate_pct"], 2),
                "expectancy_pct": round(stats["expectancy_pct"], 4),
                "profit_factor": round(stats["profit_factor"], 3),
                "monthly_t": round(stats.get("monthly_t_stat") or 0, 2),
            })
    frame = pd.DataFrame(rows)
    print(frame.to_string(index=False))
    report["maker"] = frame.to_dict("records")

    print("\n--- the decomposition, per wait window ---")
    for wait in sorted(frame["wait_bars"].unique()):
        block = frame[frame["wait_bars"] == wait].set_index("variant")
        base = block.loc["taker_all_signals", "expectancy_pct"]
        sub = block.loc["taker_on_fillable_subset", "expectancy_pct"]
        mak = block.loc["maker_on_fillable_subset", "expectancy_pct"]
        fill = block.loc["maker_on_fillable_subset", "fill_rate_pct"]
        print(f"\n  wait {wait * 15:>3} min   fill rate {fill:>5.1f}%")
        print(f"    taker, every signal            {base:+.4f}%")
        print(f"    taker, only fillable signals   {sub:+.4f}%   "
              f"selection effect {sub - base:+.4f}%")
        print(f"    maker, same signals            {mak:+.4f}%   "
              f"price+fee effect {mak - sub:+.4f}%")
        print(f"    NET vs today                   {mak - base:+.4f}%")

    print("\n" + "=" * 100)
    print("B. UNIVERSE BREADTH - test the recommendation before applying it")
    print("=" * 100)
    rows = []
    for top_n in (10, 15, 20, 30):
        member = universe_membership(frames, top_n=top_n)
        live = sum(int(m.sum()) for m in member.values())
        for cost_name in ("frictionless", "base"):
            trades = non_overlapping(build_trades(
                frames, member, contract, COST_SCENARIOS[cost_name]))
            stats = summarize(trades, bootstrap=False)
            rows.append({
                "top_n": top_n,
                "universe_symbol_bars": live,
                "costs": cost_name,
                "trades": stats["trades"],
                "win_rate_pct": round(stats["win_rate_pct"], 2),
                "expectancy_pct": round(stats["expectancy_pct"], 4),
                "expectancy_r": round(stats["expectancy_r"], 4),
                "profit_factor": round(stats["profit_factor"], 3),
                "monthly_t": round(stats.get("monthly_t_stat") or 0, 2),
            })
    breadth = pd.DataFrame(rows)
    print(breadth.to_string(index=False))
    report["breadth"] = breadth.to_dict("records")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
