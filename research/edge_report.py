"""Run the full unbiased edge investigation and emit a reviewable report.

Stages
------
baseline    Shipped config vs explicit null baselines (random timing,
            random direction, inverted signal) across cost scenarios.
latency     Sensitivity to acting 0-3 bars after the signal bar closes,
            because a 300s cycle is not aligned to 15m bar boundaries.
conditional Where, if anywhere, expectancy is positive: regime, session,
            volatility, participation, extension, stop width, symbol.
sweep       Pre-registered parameter grid scored on a purged walk-forward
            split, reported with the number of hypotheses tested.
breakeven   Round-trip cost at which each variant's expectancy hits zero.
oracle      Upper bound on what a perfect trade selector could extract,
            which bounds what the LLM layer can possibly add.
portfolio   Full account simulation through the repository RiskEngine and
            circuit breakers on the point-in-time universe.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.edge_lab import (  # noqa: E402
    COST_SCENARIOS, DEFAULT_COST_SCENARIO, Contract, Costs, HOUR_MS,
    build_trades, evidence_masks, load_dataset, non_overlapping, simulate,
    summarize, universe_membership,
)

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 60)


def tag(trades: pd.DataFrame, frames: dict) -> pd.DataFrame:
    """Attach point-in-time context columns for conditional analysis."""
    if trades.empty:
        return trades
    out = trades.copy()
    extra = []
    for symbol, group in out.groupby("symbol"):
        df = frames[symbol].df
        idx = group["signal_idx"].to_numpy()
        extra.append(pd.DataFrame({
            "_row": group.index,
            "regime": df["regime"].to_numpy(object)[idx],
            "atr_1h_pct": df["atr_1h_pct"].to_numpy(float)[idx],
            "atr_1h_ratio": df["atr_1h_ratio"].to_numpy(float)[idx],
            "relative_volume_1h": df["relative_volume_1h"].to_numpy(float)[idx],
            "range_pos_pct": df["range_pos_pct"].to_numpy(float)[idx],
            "mom_1h_pct": df["mom_1h_pct"].to_numpy(float)[idx],
            "funding_rate_pct": df["funding_rate_pct"].to_numpy(float)[idx],
            "beta_btc": df["beta_btc_1h_72"].to_numpy(float)[idx],
            "qv_24h_usd": df["qv_24h_usd"].to_numpy(float)[idx],
            "ema20_1h_dist_pct": df["ema20_1h_dist_pct"].to_numpy(float)[idx],
        }))
    merged = pd.concat(extra).set_index("_row")
    out = out.join(merged)
    atr = out["atr_1h_pct"].replace(0, np.nan)
    out["extension_atr"] = np.where(
        out["direction"] == "long",
        out["ema20_1h_dist_pct"] / atr, -out["ema20_1h_dist_pct"] / atr)
    out["hour_utc"] = ((out["entry_ts"] // HOUR_MS) % 24).astype(int)
    out["session"] = pd.cut(
        out["hour_utc"], bins=[-1, 6, 12, 20, 23],
        labels=["asia_00_06", "eu_open_07_12", "us_13_20", "late_21_23"])
    out["month"] = pd.to_datetime(
        out["entry_ts"], unit="ms", utc=True).dt.strftime("%Y-%m")
    out["stop_bucket"] = pd.cut(
        out["stop_pct"], bins=[0, 0.75, 1.25, 2.0, 3.0, 100],
        labels=["<0.75", "0.75-1.25", "1.25-2.0", "2.0-3.0", ">3.0"])
    return out


def bucket_table(trades: pd.DataFrame, column: str,
                 min_trades: int = 60) -> pd.DataFrame:
    rows = []
    for value, group in trades.groupby(column, observed=True):
        if len(group) < min_trades:
            continue
        stats = summarize(group, str(value), bootstrap=False)
        rows.append(stats)
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    cols = ["label", "trades", "win_rate_pct", "expectancy_r",
            "expectancy_pct", "profit_factor", "monthly_t_stat",
            "positive_months_pct", "tp_rate_pct", "stop_rate_pct",
            "timeout_rate_pct", "avg_hold_h"]
    return frame[[c for c in cols if c in frame]].sort_values(
        "expectancy_r", ascending=False).round(3)


# Stages

def stage_baseline(frames, membership, out: dict) -> None:
    print("\n" + "=" * 78)
    print("STAGE 1 - SHIPPED CONFIG vs NULL BASELINES")
    print("=" * 78)
    contract = Contract()
    rows = []
    for cost_name in ("frictionless", "optimistic", "base", "realistic_alt",
                      "stress", "account_taker"):
        costs = COST_SCENARIOS[cost_name]
        variants = {
            "signal": dict(),
            "signal_flipped": dict(flip_direction=True),
            "null_random_direction": dict(random_direction=101),
            "null_random_timing": dict(random_timing=202),
        }
        for name, kwargs in variants.items():
            trades = build_trades(frames, membership, contract, costs,
                                  **kwargs)
            trades = non_overlapping(trades)
            stats = summarize(trades, f"{name}", bootstrap=(name == "signal"))
            stats["costs"] = cost_name
            stats["variant"] = name
            rows.append(stats)
    frame = pd.DataFrame(rows)
    cols = ["costs", "variant", "trades", "win_rate_pct", "expectancy_r",
            "expectancy_pct", "profit_factor", "monthly_t_stat",
            "positive_months_pct", "expectancy_r_ci95"]
    print(frame[[c for c in cols if c in frame]].to_string(
        index=False, float_format=lambda v: f"{v:.4f}"))
    out["baseline"] = frame.to_dict("records")


def stage_latency(frames, membership, out: dict) -> None:
    print("\n" + "=" * 78)
    print("STAGE 2 - DECISION LATENCY (300s cycle is not 15m-aligned)")
    print("=" * 78)
    contract = Contract()
    costs = COST_SCENARIOS[DEFAULT_COST_SCENARIO]
    rows = []
    for delay in (0, 1, 2, 3):
        shifted = {}
        # Delaying the entry by N bars is equivalent to shifting the signal
        # index forward while keeping the levels derived at signal time.
        trades = build_trades(frames, membership, contract, costs)
        if trades.empty:
            continue
        if delay:
            adjusted = []
            for symbol, group in trades.groupby("symbol"):
                frame = frames[symbol]
                idx = group["signal_idx"].to_numpy() + delay
                keep = idx < len(frame.df) - contract.max_hold_bars - 2
                for direction, sub in group[keep].groupby("direction"):
                    sub_idx = sub["signal_idx"].to_numpy() + delay
                    chunk = simulate(
                        frame, sub_idx, direction,
                        sub["stop_pct"].to_numpy(float),
                        sub["take_pct"].to_numpy(float),
                        costs, contract.max_hold_bars)
                    if not chunk.empty:
                        chunk["setup_type"] = sub["setup_type"].to_numpy()
                        adjusted.append(chunk)
            trades = (pd.concat(adjusted, ignore_index=True)
                      if adjusted else pd.DataFrame())
        trades = non_overlapping(trades)
        stats = summarize(trades, f"entry_delay_{delay}_bars", bootstrap=False)
        stats["delay_bars"] = delay
        stats["delay_minutes"] = delay * 15
        rows.append(stats)
    frame = pd.DataFrame(rows)
    cols = ["label", "delay_minutes", "trades", "win_rate_pct",
            "expectancy_r", "expectancy_pct", "profit_factor",
            "monthly_t_stat"]
    print(frame[[c for c in cols if c in frame]].to_string(
        index=False, float_format=lambda v: f"{v:.4f}"))
    out["latency"] = frame.to_dict("records")


def stage_forward(frames, membership, out: dict) -> None:
    """Does the setup predict direction at all, before any exit overlay?

    Stops and targets are a *management* choice layered on top of a
    directional forecast. If the forecast has no information, no exit rule
    can rescue it. This measures the raw sign-adjusted forward return after
    each signal, against the same-symbol unconditional drift, so the exit
    logic cannot mask or manufacture an edge.
    """
    print("\n" + "=" * 78)
    print("STAGE 0 - RAW DIRECTIONAL INFORMATION (no stops, no targets)")
    print("=" * 78)
    contract = Contract()
    horizons = (1, 2, 4, 8, 16, 32, 64, 96)
    rows = []
    for setup in ("trend_continuation", "range_breakout", "combined"):
        buckets = {h: [] for h in horizons}
        base_buckets = {h: [] for h in horizons}
        months_all = {h: [] for h in horizons}
        for symbol, frame in frames.items():
            df = frame.df
            close = df["close"].to_numpy(float)
            n = len(close)
            masks = evidence_masks(df, contract)
            warm = (membership[symbol]
                    & (df["trend_4h"].to_numpy(object) != None))  # noqa: E711
            for direction in ("long", "short"):
                sign = 1.0 if direction == "long" else -1.0
                if setup == "combined":
                    mask = (masks[f"trend_continuation_{direction}"]
                            | masks[f"range_breakout_{direction}"])
                else:
                    mask = masks[f"{setup}_{direction}"]
                mask = mask & warm
                idx = np.where(mask)[0]
                idx = idx[idx < n - max(horizons) - 2]
                if len(idx) == 0:
                    continue
                entry = close[idx]
                month = pd.to_datetime(
                    frame.ts[idx], unit="ms", utc=True).strftime("%Y-%m")
                for h in horizons:
                    fwd = (close[idx + h] / entry - 1) * 100 * sign
                    buckets[h].append(fwd)
                    months_all[h].append(month)
                # Unconditional same-symbol drift over the same horizon and
                # the same direction convention, on every warm bar.
                pool = np.where(warm)[0]
                pool = pool[pool < n - max(horizons) - 2]
                if len(pool):
                    for h in horizons:
                        base_buckets[h].append(
                            (close[pool + h] / close[pool] - 1) * 100 * sign)
        for h in horizons:
            if not buckets[h]:
                continue
            values = np.concatenate(buckets[h])
            months = np.concatenate(months_all[h])
            base = np.concatenate(base_buckets[h]) if base_buckets[h] else None
            per_month = pd.Series(values).groupby(months).mean()
            t_stat = (per_month.mean()
                      / (per_month.std(ddof=1) / np.sqrt(len(per_month)))
                      if len(per_month) > 2 else np.nan)
            rows.append({
                "setup": setup, "horizon_bars": h,
                "horizon_hours": h * 0.25,
                "signals": int(len(values)),
                "mean_fwd_pct": float(values.mean()),
                "baseline_drift_pct": (float(base.mean())
                                       if base is not None else np.nan),
                "excess_pct": (float(values.mean() - base.mean())
                               if base is not None else np.nan),
                "hit_rate_pct": float((values > 0).mean() * 100),
                "monthly_t": float(t_stat),
            })
    frame = pd.DataFrame(rows)
    print(frame.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    combined = frame[frame["setup"] == "combined"]
    best = combined.loc[combined["excess_pct"].idxmax()] \
        if not combined.empty else None
    if best is not None:
        print(f"\nBest combined horizon by excess return: "
              f"{best['horizon_hours']:.2f}h -> {best['excess_pct']:+.4f}% "
              f"excess, monthly t = {best['monthly_t']:.2f}")
        print("Round-trip taker cost at OKX standard rates is ~0.10%; an "
              "excess return below that cannot pay for the trade.")
    out["forward"] = frame.to_dict("records")


def stage_conditional(frames, membership, out: dict) -> None:
    print("\n" + "=" * 78)
    print("STAGE 3 - WHERE IS EXPECTANCY POSITIVE? (conditional slices)")
    print("=" * 78)
    contract = Contract()
    costs = COST_SCENARIOS[DEFAULT_COST_SCENARIO]
    trades = non_overlapping(build_trades(frames, membership, contract, costs))
    trades = tag(trades, frames)
    if trades.empty:
        print("no trades")
        return
    print(f"\nsample: {len(trades)} non-overlapping trades, "
          f"{trades['month'].nunique()} months, "
          f"{trades['symbol'].nunique()} symbols\n")

    trades["atr_ratio_bucket"] = pd.qcut(
        trades["atr_1h_ratio"], 5, duplicates="drop")
    trades["relvol_bucket"] = pd.qcut(
        trades["relative_volume_1h"], 5, duplicates="drop")
    trades["extension_bucket"] = pd.cut(
        trades["extension_atr"], bins=[-100, 0, 0.5, 1.0, 1.5, 2.5])
    trades["liquidity_bucket"] = pd.qcut(
        trades["qv_24h_usd"], 4, duplicates="drop")
    trades["funding_align"] = np.where(
        ((trades["direction"] == "long") & (trades["funding_rate_pct"] < 0))
        | ((trades["direction"] == "short") & (trades["funding_rate_pct"] > 0)),
        "tailwind", "headwind_or_flat")

    tables = {}
    for column in ("setup_type", "direction", "regime", "session",
                   "atr_ratio_bucket", "relvol_bucket", "extension_bucket",
                   "stop_bucket", "liquidity_bucket", "funding_align",
                   "symbol", "month"):
        table = bucket_table(trades, column,
                             min_trades=30 if column != "month" else 15)
        if table.empty:
            continue
        tables[column] = table.to_dict("records")
        print(f"--- by {column} ---")
        print(table.to_string(index=False))
        print()
    out["conditional"] = tables
    out["conditional_sample"] = int(len(trades))


def walk_forward_split(trades: pd.DataFrame, split: float = 0.6,
                       purge_days: int = 3) -> tuple[pd.Series, pd.Series]:
    if trades.empty:
        return pd.Series(dtype=bool), pd.Series(dtype=bool)
    start, end = trades["entry_ts"].min(), trades["entry_ts"].max()
    cut = start + (end - start) * split
    purge = purge_days * 86_400_000
    return trades["entry_ts"] <= cut, trades["entry_ts"] > cut + purge


def stage_sweep(frames, membership, out: dict) -> None:
    print("\n" + "=" * 78)
    print("STAGE 4 - PARAMETER SWEEP ON A PURGED WALK-FORWARD SPLIT")
    print("=" * 78)
    costs = COST_SCENARIOS[DEFAULT_COST_SCENARIO]
    grid = []
    for min_stop in (0.75, 1.0, 1.5, 2.0):
        for rr in (1.0, 1.5, 2.0, 3.0):
            for hold in (24, 48, 96):
                for policy in ("fixed_rr",):
                    grid.append(dict(min_stop_atr_multiple=min_stop,
                                     fixed_reward_risk=rr,
                                     max_hold_bars=hold, exit_policy=policy))
    for max_ext in (0.5, 1.0, 1.5):
        grid.append(dict(max_extension_atr=max_ext))
    for min_rv in (1.25, 1.5, 2.0):
        grid.append(dict(min_relative_volume=min_rv))
    for lo, hi in ((0.0, 1.0), (1.0, 1.75), (1.75, 9.0)):
        grid.append(dict(min_atr_ratio=lo, max_atr_ratio=hi))
    for regimes in (("trend_up", "trend_down"), ("high_volatility",),
                    ("choppy", "transition")):
        grid.append(dict(allowed_regimes=regimes))
    grid.append(dict(require_trend_15m_aligned=True))
    grid.append(dict(require_funding_tailwind=True))
    for policy in ("extended_rr", "structure_target"):
        grid.append(dict(exit_policy=policy))
    for setups in (("trend_continuation",), ("range_breakout",),
                   ("trend_continuation", "range_breakout",
                    "funding_squeeze")):
        grid.append(dict(setups=setups))
    # A stricter breakout: crypto 15m range breaks fail often, so demand a
    # more extreme range position before calling it a break.
    for threshold in (90.0, 95.0):
        grid.append(dict(breakout_range_threshold_pct=threshold))
    # Tighter no-chase boundaries than the shipped 2.5 ATR.
    for limit in (1.0, 1.5, 2.0):
        grid.append(dict(hard_max_entry_extension_atr=limit))
    # Short holds: if any edge exists it should be strongest right after the
    # signal, so a 2-6 hour horizon is the natural place to look for it.
    for hold in (8, 16):
        for rr in (1.0, 1.5):
            grid.append(dict(max_hold_bars=hold, fixed_reward_risk=rr))
    # The explicit contrarian hypothesis: fade the setup instead of following
    # it. Momentum and mean reversion cannot both be edges here, and the
    # frictionless baseline hinted the fade was no worse.
    grid.append(dict(_flip=True))
    grid.append(dict(_flip=True, fixed_reward_risk=1.0))
    grid.append(dict(_flip=True, max_hold_bars=16, fixed_reward_risk=1.0))

    rows = []
    for spec in grid:
        spec = dict(spec)
        policy = spec.pop("exit_policy", "fixed_rr")
        setups = spec.pop("setups", ("trend_continuation", "range_breakout"))
        flip = spec.pop("_flip", False)
        contract = replace(Contract(), **spec)
        trades = non_overlapping(build_trades(
            frames, membership, contract, costs, exit_policy=policy,
            setups=setups, flip_direction=flip))
        if trades.empty or len(trades) < 100:
            continue
        is_mask, oos_mask = walk_forward_split(trades)
        in_sample = summarize(trades[is_mask], bootstrap=False)
        out_sample = summarize(trades[oos_mask], bootstrap=False)
        rows.append({
            "params": json.dumps({**spec, "exit_policy": policy,
                                  "setups": list(setups),
                                  **({"flip": True} if flip else {})},
                                 default=str),
            "trades": len(trades),
            "is_trades": in_sample.get("trades", 0),
            "is_exp_r": in_sample.get("expectancy_r"),
            "is_pf": in_sample.get("profit_factor"),
            "oos_trades": out_sample.get("trades", 0),
            "oos_exp_r": out_sample.get("expectancy_r"),
            "oos_pf": out_sample.get("profit_factor"),
            "oos_t": out_sample.get("monthly_t_stat"),
            "full_exp_r": summarize(trades, bootstrap=False).get("expectancy_r"),
        })
    frame = pd.DataFrame(rows)
    if frame.empty:
        print("no variant produced enough trades")
        return
    frame = frame.sort_values("is_exp_r", ascending=False)
    print(f"\nhypotheses tested: {len(frame)}")
    print("\n--- top 12 by IN-SAMPLE expectancy, with their OOS result ---")
    print(frame.head(12).to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print("\n--- top 8 by OUT-OF-SAMPLE expectancy (for reference only) ---")
    print(frame.sort_values("oos_exp_r", ascending=False).head(8).to_string(
        index=False, float_format=lambda v: f"{v:.4f}"))
    oos = frame["oos_exp_r"].dropna()
    print(f"\nOOS expectancy across all {len(oos)} variants: "
          f"mean={oos.mean():.4f} median={oos.median():.4f} "
          f"max={oos.max():.4f} min={oos.min():.4f} "
          f"share_positive={100*(oos>0).mean():.1f}%")
    best_is = frame.iloc[0]
    print(f"\nHONEST NUMBER - best in-sample variant's OOS expectancy: "
          f"{best_is['oos_exp_r']:.4f} R over {best_is['oos_trades']} trades")
    out["sweep"] = frame.to_dict("records")


def stage_breakeven(frames, membership, out: dict) -> None:
    print("\n" + "=" * 78)
    print("STAGE 5 - COST BREAKEVEN")
    print("=" * 78)
    contract = Contract()
    rows = []
    for fee in (0.0, 0.01, 0.02, 0.03, 0.05):
        for slip in (0.0, 0.02, 0.05, 0.10):
            costs = Costs("probe", fee_per_side=fee, entry_slippage=slip,
                          exit_slippage=slip, stop_slippage=slip * 2,
                          include_funding=True)
            trades = non_overlapping(
                build_trades(frames, membership, contract, costs))
            stats = summarize(trades, bootstrap=False)
            rows.append({
                "fee_per_side_pct": fee, "slippage_pct": slip,
                "round_trip_cost_pct": 2 * fee + 2 * slip,
                "trades": stats.get("trades"),
                "expectancy_pct": stats.get("expectancy_pct"),
                "expectancy_r": stats.get("expectancy_r"),
                "profit_factor": stats.get("profit_factor"),
            })
    frame = pd.DataFrame(rows)
    print(frame.to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    positive = frame[frame["expectancy_pct"] > 0]
    if positive.empty:
        print("\nNo cost level tested - including zero - produced positive "
              "expectancy. The signal itself is the problem, not the costs.")
    else:
        print(f"\nMaximum round-trip cost that still leaves positive "
              f"expectancy: {positive['round_trip_cost_pct'].max():.3f}%")
    out["breakeven"] = frame.to_dict("records")


def stage_oracle(frames, membership, out: dict) -> None:
    print("\n" + "=" * 78)
    print("STAGE 6 - SELECTOR HEADROOM (what could the LLM add, at best?)")
    print("=" * 78)
    contract = Contract()
    costs = COST_SCENARIOS[DEFAULT_COST_SCENARIO]
    trades = non_overlapping(build_trades(frames, membership, contract, costs))
    trades = tag(trades, frames)
    if trades.empty:
        print("no trades")
        return
    rows = []
    r = trades["r_multiple"].to_numpy(float)
    for keep_pct in (100, 50, 25, 10, 5):
        k = max(1, int(len(r) * keep_pct / 100))
        best = np.sort(r)[-k:]
        rows.append({"selector": f"oracle_top_{keep_pct}pct",
                     "trades": k, "expectancy_r": float(best.mean())})
    # A random selector keeping the same fraction is the fair comparison.
    rng = np.random.default_rng(5)
    for keep_pct in (50, 25, 10):
        k = max(1, int(len(r) * keep_pct / 100))
        draws = [float(rng.choice(r, k, replace=False).mean())
                 for _ in range(200)]
        rows.append({"selector": f"random_keep_{keep_pct}pct",
                     "trades": k, "expectancy_r": float(np.mean(draws))})
    frame = pd.DataFrame(rows)
    print(frame.to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    # How good would a selector have to be? Interpolate between a random
    # keep (no skill) and the oracle keep (perfect foresight) at the same
    # selectivity, and report the share of oracle skill needed to break even.
    print("\n--- selector skill required to reach break-even ---")
    skill_rows = []
    for keep_pct in (75, 50, 25, 10):
        k = max(1, int(len(r) * keep_pct / 100))
        oracle_mean = float(np.sort(r)[-k:].mean())
        random_mean = float(r.mean())
        if oracle_mean <= random_mean:
            continue
        needed = (0.0 - random_mean) / (oracle_mean - random_mean)
        skill_rows.append({
            "keeps_pct_of_signals": keep_pct,
            "random_selector_r": round(random_mean, 4),
            "perfect_selector_r": round(oracle_mean, 4),
            "share_of_perfect_foresight_needed": round(needed * 100, 1),
        })
    if skill_rows:
        print(pd.DataFrame(skill_rows).to_string(index=False))
        print("The LLM sees only the same snapshot fields tested below. If "
              "none of them separates winners from losers out-of-sample, "
              "that share of foresight has no observable source.")
        out["selector_skill_required"] = skill_rows

    print("\n--- best single observable feature as a selector (IS -> OOS) ---")
    is_mask, oos_mask = walk_forward_split(trades)
    feature_rows = []
    features = ["atr_1h_ratio", "relative_volume_1h", "extension_atr",
                "mom_1h_pct", "range_pos_pct", "stop_pct", "beta_btc",
                "funding_rate_pct", "qv_24h_usd"]
    for feature in features:
        values = trades[feature].to_numpy(float)
        ok = ~np.isnan(values)
        for side in ("high", "low"):
            thresholds = np.nanpercentile(values[ok], [60, 70, 80, 90])
            for threshold in thresholds:
                mask = (values >= threshold) if side == "high" \
                    else (values <= threshold)
                sub_is = trades[mask & is_mask.to_numpy()]
                sub_oos = trades[mask & oos_mask.to_numpy()]
                if len(sub_is) < 80 or len(sub_oos) < 60:
                    continue
                feature_rows.append({
                    "feature": f"{feature} {'>=' if side=='high' else '<='} "
                               f"{threshold:.3f}",
                    "is_trades": len(sub_is),
                    "is_exp_r": float(sub_is["r_multiple"].mean()),
                    "oos_trades": len(sub_oos),
                    "oos_exp_r": float(sub_oos["r_multiple"].mean()),
                })
    if feature_rows:
        fframe = pd.DataFrame(feature_rows).sort_values(
            "is_exp_r", ascending=False)
        print(fframe.head(10).to_string(
            index=False, float_format=lambda v: f"{v:.4f}"))
        print(f"\ncorrelation between IS and OOS expectancy across "
              f"{len(fframe)} feature rules: "
              f"{fframe['is_exp_r'].corr(fframe['oos_exp_r']):.3f}")
        out["selector_rules"] = fframe.to_dict("records")
    out["oracle"] = frame.to_dict("records")


def stage_power(frames, membership, out: dict) -> None:
    """How long must a forward demo run before it can prove anything?

    A demo run is only evidence if it can distinguish a real edge from
    noise. That takes a number of trades set by the dispersion of trade
    outcomes, not by wall-clock comfort.
    """
    print("\n" + "=" * 78)
    print("STAGE 8 - STATISTICAL POWER OF A FORWARD DEMO RUN")
    print("=" * 78)
    contract = Contract()
    trades = non_overlapping(build_trades(
        frames, membership, contract, COST_SCENARIOS[DEFAULT_COST_SCENARIO]))
    if trades.empty:
        print("no trades")
        return
    r = trades["r_multiple"].to_numpy(float)
    sd = float(np.std(r, ddof=1))
    span_days = (trades["entry_ts"].max() - trades["entry_ts"].min()) / 86_400_000
    per_day = len(trades) / max(span_days, 1)
    # The agent holds at most 3 positions and takes the highest-priority
    # candidate per bar, so its live rate is far below the raw signal rate.
    agent_per_day = min(per_day, 3.0)
    print(f"observed R dispersion (sd): {sd:.3f}")
    print(f"raw signal rate: {per_day:.2f}/day; "
          f"agent-throttled estimate: {agent_per_day:.2f}/day")
    rows = []
    for effect in (0.05, 0.10, 0.20, 0.30):
        n = (1.96 + 0.84) ** 2 * (sd / effect) ** 2
        rows.append({
            "true_edge_r_per_trade": effect,
            "trades_needed_95pct_conf_80pct_power": int(np.ceil(n)),
            "days_at_agent_rate": int(np.ceil(n / max(agent_per_day, 0.01))),
            "months_at_agent_rate": round(n / max(agent_per_day, 0.01) / 30.4, 1),
        })
    frame = pd.DataFrame(rows)
    print(frame.to_string(index=False))
    print("\nA two-week demo produces roughly "
          f"{int(agent_per_day * 14)} trades. Read against the table above, "
          "that is far too few to separate a real edge from noise: a demo "
          "run of that length validates plumbing, not profitability.")
    out["power"] = {"sd_r": sd, "signals_per_day": per_day,
                    "agent_trades_per_day": agent_per_day,
                    "requirements": rows}


def stage_portfolio(frames, membership, out: dict) -> None:
    print("\n" + "=" * 78)
    print("STAGE 7 - ACCOUNT SIMULATION THROUGH THE REPOSITORY RISK ENGINE")
    print("=" * 78)
    from research.portfolio_sim import run_portfolio
    results = {}
    for cost_name in ("frictionless", "base", "account_taker"):
        metrics = run_portfolio(frames, membership, Contract(),
                                COST_SCENARIOS[cost_name])
        results[cost_name] = metrics
        print(f"\n--- {cost_name} ---")
        print(json.dumps(metrics, indent=2, default=str))
    out["portfolio"] = results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--stage", default="all")
    parser.add_argument("--out", type=Path,
                        default=Path("runtime/research/edge_report.json"))
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--min-qv", type=float, default=50e6)
    args = parser.parse_args()

    print("loading dataset ...")
    frames = load_dataset(args.data)
    print(f"symbols with usable history: {len(frames)}")
    for symbol, frame in sorted(frames.items()):
        first = pd.Timestamp(int(frame.ts[0]), unit="ms", tz="UTC")
        last = pd.Timestamp(int(frame.ts[-1]), unit="ms", tz="UTC")
        print(f"  {symbol:22s} {len(frame.df):6d} bars  "
              f"{first:%Y-%m-%d} -> {last:%Y-%m-%d}")

    print("\nrebuilding point-in-time universe ...")
    membership = universe_membership(
        frames, top_n=args.top_n, min_qv_usd=args.min_qv)
    counts = {s: int(m.sum()) for s, m in membership.items()}
    total_slots = sum(counts.values())
    print(f"symbol-bars inside the live universe: {total_slots:,}")
    for symbol, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        share = count / max(1, len(frames[symbol].df)) * 100
        print(f"  {symbol:22s} {count:7d} bars in universe ({share:5.1f}% of life)")

    out: dict = {"universe_bar_counts": counts}
    stages = {
        "forward": stage_forward, "baseline": stage_baseline,
        "latency": stage_latency, "conditional": stage_conditional,
        "sweep": stage_sweep, "breakeven": stage_breakeven,
        "oracle": stage_oracle, "power": stage_power,
        "portfolio": stage_portfolio,
    }
    wanted = list(stages) if args.stage == "all" else args.stage.split(",")
    for name in wanted:
        stages[name](frames, membership, out)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
