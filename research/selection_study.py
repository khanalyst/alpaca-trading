"""What changes in selection or trade management produce better trades?

The entry signal has no measurable directional edge, so nothing here can
create one. What these levers *can* do is change the shape of the payoff:
which of the available candidates gets taken, when a position is abandoned,
and how a winner is harvested. Those are real decisions the agent makes every
cycle, and some of them are demonstrably better than others.

Three families, all mechanical and all testable on existing data:

A. **Trade management** - breakeven stops, ATR trailing stops, partial
   take-profit, and giving up on a position that has gone nowhere. These
   redistribute the payoff rather than add information, so expectancy and win
   rate are reported together: a change that raises one while lowering the
   other is a preference, not an improvement.

B. **Selection among simultaneous candidates.** The agent may hold 3
   positions and routinely has more candidates than slots. Today it ranks
   breakouts first, then by relative volume. That ordering was never tested.

C. **Skip rules.** Conditions under which taking no trade beats taking the
   best available one.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.edge_lab import (  # noqa: E402
    COST_SCENARIOS, Contract, Costs, derive_levels, evidence_masks,
    load_dataset, research_gate, summarize, universe_membership,
)

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)


def candidates(frames, membership, contract: Contract):
    """Every admissible setup with the context needed to rank or skip it."""
    rows = []
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
                if len(idx) == 0:
                    continue
                rows.append(pd.DataFrame({
                    "symbol": symbol, "idx": idx, "ts": frame.ts[idx],
                    "direction": direction, "setup": setup,
                    "stop_pct": stop[idx], "take_pct": take[idx],
                    "atr_pct": df["atr_1h_pct"].to_numpy(float)[idx],
                    "atr_ratio": df["atr_1h_ratio"].to_numpy(float)[idx],
                    "rel_vol": df["relative_volume_1h"].to_numpy(float)[idx],
                    "ext_atr": ext[idx],
                    "qv24": df["qv_24h_usd"].to_numpy(float)[idx],
                    "regime": df["regime"].to_numpy(object)[idx],
                }))
    return pd.concat(rows, ignore_index=True).sort_values(
        ["ts", "symbol"]).reset_index(drop=True)


def simulate_managed(frame, idx, direction, stop_pct, take_pct, costs: Costs,
                     max_hold_bars: int, *, breakeven_at_r: float | None = None,
                     trail_atr: float | None = None,
                     partial_at_r: float | None = None,
                     give_up_bars: int | None = None,
                     atr_pct: np.ndarray | None = None) -> pd.DataFrame:
    """Bar-by-bar simulation supporting management overlays.

    Within a bar the pessimistic ordering is used throughout: the stop is
    checked before the target, and a stop that has been moved up is checked
    at its new level from the bar after it moved - never on the same bar that
    justified moving it.
    """
    n = len(frame.df)
    open_ = frame.df["open"].to_numpy(float)
    high = frame.df["high"].to_numpy(float)
    low = frame.df["low"].to_numpy(float)
    close = frame.df["close"].to_numpy(float)
    sign = 1.0 if direction == "long" else -1.0

    entry_idx = idx + 1
    keep = entry_idx < n - max_hold_bars - 2
    entry_idx, stop_pct, take_pct = (
        entry_idx[keep], stop_pct[keep], take_pct[keep])
    if atr_pct is not None:
        atr_pct = atr_pct[keep]
    if len(entry_idx) == 0:
        return pd.DataFrame()

    entry = open_[entry_idx] * (1 + costs.entry_slippage / 100 * sign)
    risk = entry * stop_pct / 100
    stop_level = entry - sign * risk
    take_level = entry + sign * take_pct / 100 * entry
    fee = costs.fee_per_side

    out_net, out_reason, out_bars = [], [], []
    for k in range(len(entry_idx)):
        start = int(entry_idx[k])
        stop = stop_level[k]
        best = entry[k]
        realised = 0.0          # percent already banked by a partial exit
        remaining = 1.0
        moved = False
        took_partial = False
        reason = "max_hold"
        exit_price = close[min(start + max_hold_bars - 1, n - 1)]
        exit_bar = min(start + max_hold_bars - 1, n - 1)

        for step in range(max_hold_bars):
            bar = start + step
            if bar >= n:
                break
            hit_stop = (low[bar] <= stop) if direction == "long" \
                else (high[bar] >= stop)
            hit_take = (high[bar] >= take_level[k]) if direction == "long" \
                else (low[bar] <= take_level[k])
            if hit_stop:
                fill = (min(stop, open_[bar]) if direction == "long"
                        else max(stop, open_[bar]))
                exit_price, exit_bar = fill, bar
                reason = "breakeven_stop" if moved else "stop"
                break
            if hit_take:
                exit_price, exit_bar = take_level[k], bar
                reason = "take_profit"
                break
            # Measure favourable excursion at the close (conservative).
            excursion = sign * (close[bar] - entry[k]) / risk[k]
            if partial_at_r is not None and not took_partial \
                    and excursion >= partial_at_r:
                banked = sign * (close[bar] - entry[k]) / entry[k] * 100
                realised += 0.5 * (banked - fee)
                remaining = 0.5
                took_partial = True
            if breakeven_at_r is not None and not moved \
                    and excursion >= breakeven_at_r:
                # Breakeven must cover round-trip costs; otherwise scratch is a loss.
                stop = entry[k] * (1 + sign * 2 * fee / 100)
                moved = True
            if trail_atr is not None and atr_pct is not None:
                best = max(best, high[bar]) if direction == "long" \
                    else min(best, low[bar])
                trailed = best * (1 - sign * trail_atr * atr_pct[k] / 100)
                stop = max(stop, trailed) if direction == "long" \
                    else min(stop, trailed)
            if give_up_bars is not None and step >= give_up_bars \
                    and excursion <= 0:
                exit_price, exit_bar = close[bar], bar
                reason = "gave_up"
                break

        slip = (costs.stop_slippage if reason in ("stop", "breakeven_stop")
                else costs.exit_slippage)
        filled = exit_price * (1 - slip / 100 * sign)
        gross = sign * (filled / entry[k] - 1) * 100
        net = realised + remaining * (gross - fee) - fee
        out_net.append(net)
        out_reason.append(reason)
        out_bars.append(exit_bar - start + 1)

    denom = np.maximum(stop_pct + 2 * fee + costs.stop_slippage, 0.05)
    return pd.DataFrame({
        "entry_ts": frame.ts[entry_idx], "net_pct": np.array(out_net),
        "r_multiple": np.array(out_net) / denom,
        "exit_reason": out_reason, "bars_held": out_bars,
    })


def run_variant(frames, membership, contract, costs, **overlay) -> pd.DataFrame:
    chunks = []
    for symbol, group in candidates(
            frames, membership, contract).groupby("symbol"):
        frame = frames[symbol]
        for direction, sub in group.groupby("direction"):
            chunk = simulate_managed(
                frame, sub["idx"].to_numpy(), direction,
                sub["stop_pct"].to_numpy(float),
                sub["take_pct"].to_numpy(float), costs,
                contract.max_hold_bars,
                atr_pct=sub["atr_pct"].to_numpy(float), **overlay)
            if not chunk.empty:
                chunks.append(chunk)
    if not chunks:
        return pd.DataFrame()
    trades = pd.concat(chunks, ignore_index=True).sort_values("entry_ts")
    return trades.reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", type=Path,
                        default=Path("runtime/research/selection_study.json"))
    args = parser.parse_args()

    frames = load_dataset(args.data)
    membership = universe_membership(frames)
    contract = Contract(max_hold_bars=192, fixed_reward_risk=3.0)
    costs = COST_SCENARIOS["base"]
    report = {}

    print("=" * 108)
    print("A. TRADE MANAGEMENT - does changing how trades are exited help?")
    print("=" * 108)
    variants = {
        "shipped (fixed stop + 3R target)": {},
        "breakeven stop at +1R": {"breakeven_at_r": 1.0},
        "breakeven stop at +1.5R": {"breakeven_at_r": 1.5},
        "trail 2.0 ATR": {"trail_atr": 2.0},
        "trail 3.0 ATR": {"trail_atr": 3.0},
        "half off at +1.5R": {"partial_at_r": 1.5},
        "half off at +1.5R, breakeven at +1R": {
            "partial_at_r": 1.5, "breakeven_at_r": 1.0},
        "give up after 8h if not green": {"give_up_bars": 32},
        "give up after 16h if not green": {"give_up_bars": 64},
    }
    rows = []
    for label, overlay in variants.items():
        trades = run_variant(frames, membership, contract, costs, **overlay)
        stats = summarize(trades, bootstrap=False)
        mix = trades["exit_reason"].value_counts(normalize=True) * 100
        rows.append({
            "variant": label, "trades": stats["trades"],
            "win_rate_pct": round(stats["win_rate_pct"], 2),
            "expectancy_pct": round(stats["expectancy_pct"], 4),
            "profit_factor": round(stats["profit_factor"], 3),
            "monthly_t": round(stats.get("monthly_t_stat") or 0, 2),
            "avg_hold_h": round(stats["avg_hold_h"], 1),
            "stopped_pct": round(float(mix.get("stop", 0)), 1),
            "tp_pct": round(float(mix.get("take_profit", 0)), 1),
        })
    management = pd.DataFrame(rows)
    print(management.to_string(index=False))
    report["management"] = management.to_dict("records")

    print("\n" + "=" * 108)
    print("B. SELECTION - when more candidates fire than there are slots, "
          "which one?")
    print("=" * 108)
    pool = candidates(frames, membership, contract)
    outcomes = []
    for symbol, group in pool.groupby("symbol"):
        frame = frames[symbol]
        for direction, sub in group.groupby("direction"):
            chunk = simulate_managed(
                frame, sub["idx"].to_numpy(), direction,
                sub["stop_pct"].to_numpy(float),
                sub["take_pct"].to_numpy(float), costs,
                contract.max_hold_bars)
            if chunk.empty:
                continue
            keep = sub.iloc[:len(chunk)].reset_index(drop=True)
            for column in ("net_pct", "r_multiple", "exit_reason",
                           "bars_held"):
                keep[column] = chunk[column].to_numpy()
            keep["cost_ratio"] = (
                2 * costs.fee_per_side + costs.stop_slippage) / keep["stop_pct"]
            outcomes.append(keep)
    pool = pd.concat(outcomes, ignore_index=True)
    crowd = pool.groupby("ts")["symbol"].transform("size")
    pool["crowding"] = crowd
    contested = pool[pool["crowding"] >= 2]
    print(f"bars with more than one candidate: "
          f"{contested['ts'].nunique():,} of {pool['ts'].nunique():,}")

    rules = {
        "shipped: breakout first, then relative volume": (
            ["setup", "rel_vol"], [True, False]),
        "highest relative volume": (["rel_vol"], [False]),
        "lowest volatility (atr_ratio)": (["atr_ratio"], [True]),
        "highest volatility (atr_ratio)": (["atr_ratio"], [False]),
        "least extended from EMA20": (["ext_atr"], [True]),
        "most liquid (24h turnover)": (["qv24"], [False]),
        "widest stop (cost is a smaller share)": (["stop_pct"], [False]),
        "tightest stop": (["stop_pct"], [True]),
    }
    rows = []
    for label, (keys, ascending) in rules.items():
        picks = (contested.sort_values(keys, ascending=ascending)
                 .groupby("ts").head(1))
        stats = summarize(picks.rename(columns={"ts": "entry_ts"}),
                          bootstrap=False)
        rows.append({
            "rule": label, "trades": stats["trades"],
            "win_rate_pct": round(stats["win_rate_pct"], 2),
            "expectancy_pct": round(stats["expectancy_pct"], 4),
            "profit_factor": round(stats["profit_factor"], 3),
            "monthly_t": round(stats.get("monthly_t_stat") or 0, 2),
        })
    rows.append({"rule": "take every candidate (no selection)",
                 "trades": len(contested),
                 "win_rate_pct": round(
                     float((contested["net_pct"] > 0).mean() * 100), 2),
                 "expectancy_pct": round(
                     float(contested["net_pct"].mean()), 4),
                 "profit_factor": None, "monthly_t": None})
    selection = pd.DataFrame(rows).sort_values(
        "expectancy_pct", ascending=False)
    print(selection.to_string(index=False))
    report["selection"] = selection.to_dict("records")

    print("\n" + "=" * 108)
    print("C. SKIP RULES - is taking no trade better than the best available?")
    print("=" * 108)
    rows = []
    for label, mask in {
        "all candidates": pool["net_pct"].notna(),
        "only when 1-2 candidates fire (quiet)": pool["crowding"] <= 2,
        "only when 5+ fire (market-wide move)": pool["crowding"] >= 5,
        "skip high_volatility regime": pool["regime"] != "high_volatility",
        "only trend_up / trend_down regimes": pool["regime"].isin(
            ["trend_up", "trend_down"]),
        "skip cheapest-stop quartile (worst cost ratio)": (
            pool["cost_ratio"] <= pool["cost_ratio"].quantile(0.75)),
        "only stops wider than the median": (
            pool["stop_pct"] >= pool["stop_pct"].median()),
    }.items():
        sub = pool[mask]
        if len(sub) < 500:
            continue
        stats = summarize(sub.rename(columns={"ts": "entry_ts"}),
                          bootstrap=False)
        rows.append({
            "filter": label, "trades": stats["trades"],
            "share_of_all_pct": round(len(sub) / len(pool) * 100, 1),
            "win_rate_pct": round(stats["win_rate_pct"], 2),
            "expectancy_pct": round(stats["expectancy_pct"], 4),
            "profit_factor": round(stats["profit_factor"], 3),
            "monthly_t": round(stats.get("monthly_t_stat") or 0, 2),
        })
    skips = pd.DataFrame(rows).sort_values("expectancy_pct", ascending=False)
    print(skips.to_string(index=False))
    report["skip_rules"] = skips.to_dict("records")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
