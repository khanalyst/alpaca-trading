"""Look-ahead-safe proxy backtest for the phase1-v2 momentum strategy.

This deliberately does not replay the LLM. It measures:
1. the deterministic phase1-v2 setup contracts; and
2. an executable fixed-RR portfolio proxy using the repository RiskEngine.

Historical LLM replay would not be a clean economic test because the current
model can have training knowledge of the historical period. It would also
require tens of thousands of paid calls. The report therefore keeps the
deterministic evidence and the untested LLM contribution separate.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

BAR_MS = 15 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
FOUR_HOUR_MS = 4 * HOUR_MS
MAX_HOLD_BARS = 96
INITIAL_EQUITY = 100_000.0


@dataclass(frozen=True)
class Scenario:
    name: str
    fee_pct_per_side: float
    entry_slippage_pct: float
    ordinary_exit_slippage_pct: float
    stop_slippage_pct: float
    include_funding: bool = True


SCENARIOS = {
    "frictionless": Scenario(
        "frictionless", 0.0, 0.0, 0.0, 0.0, False,
    ),
    "base": Scenario(
        # Five basis points of adverse entry/exit execution is an explicit
        # scenario assumption because historical order books are unavailable.
        "base", 0.05, 0.05, 0.05, 0.15, True,
    ),
    "entry_cap": Scenario(
        # The entry executes at the configured 0.35% IOC boundary. This is a
        # capacity/execution stress, not a claim about average realized fills.
        "entry_cap", 0.05, 0.35, 0.05, 0.15, True,
    ),
}


def windowed_ewm_last(
    values: np.ndarray,
    alpha: float,
    window: int = 120,
    first_override: np.ndarray | None = None,
) -> np.ndarray:
    """Exact adjust=False EWM value at the end of each fixed-size window."""
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < window:
        return out
    q = 1.0 - alpha
    weights = np.empty(window, dtype=float)
    weights[0] = q ** (window - 1)
    powers = np.arange(window - 2, -1, -1, dtype=float)
    weights[1:] = alpha * (q ** powers)
    rolled = np.convolve(values, weights[::-1], mode="valid")
    if first_override is not None:
        starts = np.arange(len(rolled))
        rolled += weights[0] * (
            np.asarray(first_override, dtype=float)[starts] - values[starts]
        )
    out[window - 1:] = rolled
    return out


def aggregate_bars(df: pd.DataFrame, duration_ms: int) -> pd.DataFrame:
    work = df.copy()
    work["bucket"] = (work["timestamp_ms"] // duration_ms) * duration_ms
    return (
        work.groupby("bucket", sort=True, as_index=False)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .rename(columns={"bucket": "timestamp_ms"})
    )


def add_timeframe_features(df: pd.DataFrame, *, atr: bool = False) -> pd.DataFrame:
    out = df.copy()
    close = out["close"].to_numpy(float)
    e20 = windowed_ewm_last(close, 2 / 21)
    e50 = windowed_ewm_last(close, 2 / 51)
    trend = np.full(len(out), "flat", dtype=object)
    trend[(close > e20) & (e20 > e50)] = "up"
    trend[(close < e20) & (e20 < e50)] = "down"
    trend[np.isnan(e20) | np.isnan(e50)] = None
    out["ema20"] = e20
    out["ema50"] = e50
    out["trend"] = trend
    if atr:
        high = out["high"].to_numpy(float)
        low = out["low"].to_numpy(float)
        previous = np.roll(close, 1)
        previous[0] = np.nan
        true_range = np.maximum.reduce([
            high - low,
            np.abs(high - previous),
            np.abs(low - previous),
        ])
        true_range[0] = high[0] - low[0]
        current_atr = windowed_ewm_last(
            true_range, 1 / 14, first_override=high - low,
        )
        atr_pct = current_atr / close * 100
        baseline = (
            pd.Series(atr_pct).shift(1).rolling(50, min_periods=40).median()
            .to_numpy(float)
        )
        out["atr_pct"] = atr_pct
        out["atr_ratio"] = np.divide(
            atr_pct,
            baseline,
            out=np.full(len(out), np.nan),
            where=baseline > 0,
        )
    return out


def robust_beta(symbol_close: pd.Series, btc_close: pd.Series) -> pd.DataFrame:
    symbol_returns = symbol_close.pct_change()
    btc_returns = btc_close.pct_change()
    covariance = symbol_returns.rolling(72, min_periods=24).cov(btc_returns)
    variance = btc_returns.rolling(72, min_periods=24).var()
    raw_corr = symbol_returns.rolling(72, min_periods=24).corr(btc_returns)
    downside_symbol = symbol_returns.where(btc_returns < 0)
    downside_btc = btc_returns.where(btc_returns < 0)
    downside_corr = downside_symbol.rolling(
        72, min_periods=10,
    ).corr(downside_btc)
    samples = (
        symbol_returns.notna() & btc_returns.notna()
    ).rolling(72).sum()
    shrink = samples / (samples + 20)
    return pd.DataFrame({
        "beta": covariance / variance,
        "corr_shrunk": raw_corr * shrink,
        "downside_corr": downside_corr,
        "samples": samples,
    })


class HistoricalSymbol:
    def __init__(
        self,
        symbol: str,
        swap: pd.DataFrame,
        spot: pd.DataFrame,
        funding: pd.DataFrame,
        btc_1h: pd.DataFrame,
    ):
        self.symbol = symbol
        self.fast = add_timeframe_features(swap)
        self.one = add_timeframe_features(
            aggregate_bars(swap, HOUR_MS), atr=True,
        )
        self.four = add_timeframe_features(
            aggregate_bars(swap, FOUR_HOUR_MS),
        )
        self.spot = spot
        self.funding = funding

        fast = self.fast
        close = fast["close"]
        high = fast["high"]
        low = fast["low"]
        volume = fast["volume"]
        fast["mom_15m_pct"] = close.pct_change() * 100
        fast["mom_1h_pct"] = close.pct_change(4) * 100
        fast["signal_return_pct"] = (
            (close - fast["open"]) / fast["open"] * 100
        )
        fast["prior_high_20"] = high.shift(1).rolling(20).max()
        fast["prior_low_20"] = low.shift(1).rolling(20).min()
        fast["fresh_breakout_long"] = close > fast["prior_high_20"]
        fast["fresh_breakout_short"] = close < fast["prior_low_20"]
        fast["price_stabilized_long"] = (
            (low >= low.shift(1)) & (close > fast["open"])
        )
        fast["price_stabilized_short"] = (
            (high <= high.shift(1)) & (close < fast["open"])
        )
        rolling_hour_volume = volume.rolling(4).sum()
        normal_volume = rolling_hour_volume.shift(4).rolling(20).median()
        fast["relative_volume_1h"] = rolling_hour_volume / normal_volume
        fast["swing_low"] = low.rolling(20).min()
        fast["swing_high"] = high.rolling(20).max()
        fast["high_24h"] = high.rolling(96).max()
        fast["low_24h"] = low.rolling(96).min()

        signal_end = fast["timestamp_ms"].to_numpy(np.int64) + BAR_MS
        self.map_one = np.searchsorted(
            self.one["timestamp_ms"].to_numpy(np.int64) + HOUR_MS,
            signal_end,
            side="right",
        ) - 1
        self.map_four = np.searchsorted(
            self.four["timestamp_ms"].to_numpy(np.int64) + FOUR_HOUR_MS,
            signal_end,
            side="right",
        ) - 1

        beta = robust_beta(
            self.one.set_index("timestamp_ms")["close"],
            btc_1h.set_index("timestamp_ms")["close"],
        ).reindex(self.one["timestamp_ms"].to_numpy()).reset_index(drop=True)
        self.one["beta"] = beta["beta"].to_numpy()
        self.one["corr_shrunk"] = beta["corr_shrunk"].to_numpy()
        self.one["downside_corr"] = beta["downside_corr"].to_numpy()
        self.one["corr_samples"] = beta["samples"].to_numpy()

        spot_by_ts = spot.set_index("timestamp_ms")["close"]
        aligned_spot = spot_by_ts.reindex(
            fast["timestamp_ms"].to_numpy(),
        ).to_numpy(float)
        fast["basis_pct"] = (
            (fast["close"].to_numpy(float) - aligned_spot)
            / aligned_spot * 100
        )

        self.funding_ts = funding["timestamp_ms"].to_numpy(np.int64)
        self.funding_rates = funding["funding_rate"].to_numpy(float)
        self.funding_percentiles = np.full(len(funding), np.nan)
        self.funding_means = np.full(len(funding), np.nan)
        self.funding_changes = np.full(len(funding), np.nan)
        self.funding_samples = np.zeros(len(funding), dtype=int)
        for index, value in enumerate(self.funding_rates):
            start = max(0, index - 29)
            window = self.funding_rates[start:index + 1]
            self.funding_percentiles[index] = (
                np.sum(window <= value) / len(window) * 100
            )
            self.funding_means[index] = np.mean(window)
            self.funding_samples[index] = len(window)
            if index:
                self.funding_changes[index] = value - self.funding_rates[index - 1]

    def valid(self, index: int) -> bool:
        if index < 119 or index >= len(self.fast) - 1:
            return False
        one_index = int(self.map_one[index])
        four_index = int(self.map_four[index])
        return (
            one_index >= 119
            and four_index >= 119
            and math.isfinite(float(self.one.iloc[one_index]["atr_pct"]))
        )

    def snapshot(self, index: int, cfg: dict) -> dict | None:
        if not self.valid(index):
            return None
        fast = self.fast.iloc[index]
        one = self.one.iloc[int(self.map_one[index])]
        four = self.four.iloc[int(self.map_four[index])]
        price = float(fast["close"])
        high_24h = float(fast["high_24h"])
        low_24h = float(fast["low_24h"])
        signal_end = int(fast["timestamp_ms"]) + BAR_MS
        funding_index = int(np.searchsorted(
            self.funding_ts, signal_end, side="right",
        ) - 1)
        if funding_index >= 0:
            funding_pct = float(self.funding_rates[funding_index] * 100)
            funding_samples = int(self.funding_samples[funding_index])
            funding_percentile = float(
                self.funding_percentiles[funding_index])
            funding_mean = float(self.funding_means[funding_index] * 100)
            funding_change = (
                float(self.funding_changes[funding_index] * 100)
                if math.isfinite(self.funding_changes[funding_index])
                else None
            )
            if funding_index + 1 < len(self.funding_ts):
                next_funding = int(self.funding_ts[funding_index + 1])
                next_minutes = max(0.0, (next_funding - signal_end) / 60_000)
                interval_hours = (
                    next_funding - int(self.funding_ts[funding_index])
                ) / HOUR_MS
            else:
                next_minutes = None
                interval_hours = 8.0
        else:
            funding_pct = None
            funding_samples = 0
            funding_percentile = None
            funding_mean = None
            funding_change = None
            next_minutes = None
            interval_hours = 8.0

        range_position = (
            (price - low_24h) / (high_24h - low_24h) * 100
            if high_24h > low_24h > 0 else None
        )
        atr_pct = float(one["atr_pct"])
        atr_ratio = float(one["atr_ratio"])
        ema_distance = (price - float(one["ema20"])) / float(one["ema20"]) * 100
        sample_count = int(one["corr_samples"]) if pd.notna(
            one["corr_samples"]) else 0
        beta = float(one["beta"]) if pd.notna(one["beta"]) else None
        if self.symbol == "BTC/USDT:USDT":
            beta = 1.0
            sample_count = max(sample_count, 72)
        snap = {
            "price": price,
            "spread_pct": 0.05,
            "taker_fee_pct_per_side": float(
                cfg["trading_costs"]["taker_fee_pct_per_side"]),
            "fee_rate_source": "configured_backtest",
            "funding_rate_pct": funding_pct,
            "funding_interval_hours": interval_hours,
            "next_funding_minutes": next_minutes,
            "funding_samples_30": funding_samples,
            "funding_mean_30_pct": funding_mean,
            "funding_percentile_30": funding_percentile,
            "funding_change_pct": funding_change,
            "perp_index_basis_pct": float(fast["basis_pct"]),
            # Historical OI is unavailable. This intentionally prevents the
            # funding-squeeze evidence contract from passing.
            "open_interest_musd": None,
            "trend_15m": fast["trend"],
            "trend_1h": one["trend"],
            "trend_4h": four["trend"],
            "rsi_1h": None,
            "atr_1h_pct": round(atr_pct, 2),
            "atr_1h_ratio": round(atr_ratio, 2)
            if math.isfinite(atr_ratio) else None,
            "signal_ts": int(fast["timestamp_ms"]),
            "signal_1h_ts": int(one["timestamp_ms"]),
            "mom_1h_pct": round(float(fast["mom_1h_pct"]), 2),
            "mom_15m_pct": round(float(fast["mom_15m_pct"]), 3),
            "signal_candle_return_pct": round(
                float(fast["signal_return_pct"]), 3),
            "prior_range_high_20": float(fast["prior_high_20"]),
            "prior_range_low_20": float(fast["prior_low_20"]),
            "fresh_breakout_long": bool(fast["fresh_breakout_long"]),
            "fresh_breakout_short": bool(fast["fresh_breakout_short"]),
            "price_stabilized_long": bool(fast["price_stabilized_long"]),
            "price_stabilized_short": bool(fast["price_stabilized_short"]),
            "relative_volume_1h": round(
                float(fast["relative_volume_1h"]), 2),
            "swing_low_pct": round(
                (price - float(fast["swing_low"])) / price * 100, 2),
            "swing_high_pct": round(
                (float(fast["swing_high"]) - price) / price * 100, 2),
            "ema20_1h_dist_pct": round(ema_distance, 2),
            "range_pos_pct": round(range_position, 0)
            if range_position is not None else None,
            "corr_btc_1h_30": None,
            "corr_btc_1h_72_shrunk": (
                round(float(one["corr_shrunk"]), 2)
                if pd.notna(one["corr_shrunk"]) else None
            ),
            "beta_btc_1h_72": round(beta, 2) if beta is not None else None,
            "corr_btc_downside_1h_72": (
                round(float(one["downside_corr"]), 2)
                if pd.notna(one["downside_corr"]) else None
            ),
            "corr_btc_samples": sample_count,
        }
        from agent.market import classify_regime
        from agent.strategy import enrich_snapshot
        snap["regime"] = classify_regime(snap)
        return enrich_snapshot(snap, cfg)

    def funding_return_pct(self, entry_ts: int, exit_ts: int, direction: str) -> float:
        start = int(np.searchsorted(self.funding_ts, entry_ts, side="left"))
        stop = int(np.searchsorted(self.funding_ts, exit_ts, side="left"))
        if stop <= start:
            return 0.0
        sign = 1.0 if direction == "long" else -1.0
        # Positive funding is paid by longs and received by shorts.
        return float(-sign * np.sum(self.funding_rates[start:stop]) * 100)


def load_symbol(root: Path, filename: str) -> pd.DataFrame:
    return pd.read_csv(root / filename)


def load_history(data_root: Path) -> tuple[dict[str, HistoricalSymbol], dict]:
    manifest = json.loads((data_root / "manifest.json").read_text())
    symbols = sorted(manifest["symbols"])
    raw: dict[str, tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    for symbol in symbols:
        stem = symbol.replace("/", "_").replace(":", "_")
        swap = load_symbol(data_root, f"swap/{stem}.csv")
        spot = load_symbol(data_root, f"spot/{stem}.csv")
        funding = load_symbol(data_root, f"funding/{stem}.csv")
        raw[symbol] = (swap, spot, funding)
    btc_1h = aggregate_bars(raw["BTC/USDT:USDT"][0], HOUR_MS)
    history = {
        symbol: HistoricalSymbol(symbol, swap, spot, funding, btc_1h)
        for symbol, (swap, spot, funding) in raw.items()
    }
    return history, manifest


def build_candidates(
    history: dict[str, HistoricalSymbol],
    cfg: dict,
) -> tuple[list[dict], dict[int, list[dict]]]:
    from agent.strategy import (
        build_setup_plan,
        compact_entry_evidence,
        evidence_fingerprint,
    )

    symbols = sorted(history)
    candidate_rows: list[dict] = []
    by_index: dict[int, list[dict]] = defaultdict(list)
    common_length = min(len(history[symbol].fast) for symbol in symbols)
    for index in range(common_length - 1):
        for symbol in symbols:
            market = history[symbol]
            snap = market.snapshot(index, cfg)
            if snap is None:
                continue
            evidence = snap["setup_evidence"]
            for setup_type in ("range_breakout", "trend_continuation"):
                for direction in ("long", "short"):
                    if evidence[setup_type][direction] is not True:
                        continue
                    plans = {}
                    for exit_policy in (
                        "fixed_rr", "extended_rr", "structure_target",
                    ):
                        decision = {
                            "action": "open",
                            "symbol": symbol,
                            "direction": direction,
                            "confidence": 1.0,
                            "setup_type": setup_type,
                            "invalidation_anchor": "structure",
                            "exit_policy": exit_policy,
                            "execution_choice": "normal",
                            "reasoning": "deterministic evidence proxy",
                            "what_changed_since_last_loss": (
                                "objective evidence and completed 1h bar changed"
                            ),
                        }
                        plan, _why = build_setup_plan(decision, snap, cfg)
                        if plan is None:
                            plans = {}
                            break
                        plans[exit_policy] = {
                            "stop_pct": float(plan["stop_loss_pct"]),
                            "take_pct": float(plan["take_profit_pct"]),
                        }
                    if not plans:
                        continue
                    row = {
                        "index": index,
                        "signal_ts": int(snap["signal_ts"]),
                        "signal_1h_ts": int(snap["signal_1h_ts"]),
                        "symbol": symbol,
                        "direction": direction,
                        "setup_type": setup_type,
                        "setup_key": (
                            f"{symbol}|{direction}|{setup_type}"
                        ),
                        "evidence_fingerprint": evidence_fingerprint(snap),
                        "relative_volume_1h": snap["relative_volume_1h"],
                        "momentum_abs": abs(float(snap["mom_1h_pct"])),
                        "funding_rate_pct": snap["funding_rate_pct"],
                        "plans": plans,
                        "entry_evidence": compact_entry_evidence(snap),
                    }
                    candidate_rows.append(row)
                    by_index[index].append(row)
    return candidate_rows, by_index


def execution_fill(
    market: HistoricalSymbol,
    candidate: dict,
    scenario: Scenario,
    exit_policy: str,
) -> dict:
    index = int(candidate["index"]) + 1
    bar = market.fast.iloc[index]
    direction = candidate["direction"]
    sign = 1.0 if direction == "long" else -1.0
    entry_raw = float(bar["open"])
    entry = entry_raw * (
        1 + scenario.entry_slippage_pct / 100 * sign
    )
    stop_pct = float(candidate["plans"][exit_policy]["stop_pct"])
    take_pct = float(candidate["plans"][exit_policy]["take_pct"])
    stop = entry * (1 - stop_pct / 100 * sign)
    take = entry * (1 + take_pct / 100 * sign)
    exit_index = min(index + MAX_HOLD_BARS - 1, len(market.fast) - 1)
    exit_reason = "max_hold"
    exit_raw = float(market.fast.iloc[exit_index]["close"])
    slippage_pct = scenario.ordinary_exit_slippage_pct

    for cursor in range(index, exit_index + 1):
        current = market.fast.iloc[cursor]
        high = float(current["high"])
        low = float(current["low"])
        current_open = float(current["open"])
        if direction == "long":
            stop_hit = low <= stop
            take_hit = high >= take
        else:
            stop_hit = high >= stop
            take_hit = low <= take
        # With no lower-timeframe path, stop-first is the conservative,
        # non-optimistic assumption when both thresholds occur in one bar.
        if stop_hit:
            exit_index = cursor
            exit_reason = "stop"
            slippage_pct = scenario.stop_slippage_pct
            if direction == "long":
                exit_raw = min(stop, current_open)
            else:
                exit_raw = max(stop, current_open)
            break
        if take_hit:
            exit_index = cursor
            exit_reason = "take_profit"
            slippage_pct = scenario.ordinary_exit_slippage_pct
            # Do not credit favorable gap improvement without tick data.
            exit_raw = take
            break

    exit_price = exit_raw * (1 - slippage_pct / 100 * sign)
    gross_return_pct = sign * (exit_price / entry - 1) * 100
    fee_return_pct = scenario.fee_pct_per_side * (
        1 + exit_price / entry
    )
    entry_ts = int(market.fast.iloc[index]["timestamp_ms"])
    exit_ts = int(market.fast.iloc[exit_index]["timestamp_ms"]) + BAR_MS
    funding_return_pct = (
        market.funding_return_pct(entry_ts, exit_ts, direction)
        if scenario.include_funding else 0.0
    )
    net_return_pct = (
        gross_return_pct - fee_return_pct + funding_return_pct
    )
    estimated_risk_pct = (
        stop_pct
        + 2 * 0.05
        + 0.05
        + 0.15
        + 0.35
        + max(
            0.0,
            float(candidate.get("funding_rate_pct") or 0)
            * (1 if direction == "long" else -1),
        )
    )
    return {
        "signal_ts": int(candidate["signal_ts"]),
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_index": index,
        "exit_index": exit_index,
        "symbol": candidate["symbol"],
        "direction": direction,
        "setup_type": candidate["setup_type"],
        "exit_policy": exit_policy,
        "scenario": scenario.name,
        "entry_price": entry,
        "exit_price": exit_price,
        "stop_pct": stop_pct,
        "take_pct": take_pct,
        "exit_reason": exit_reason,
        "gross_return_pct": gross_return_pct,
        "fees_pct": fee_return_pct,
        "funding_pct": funding_return_pct,
        "net_return_pct": net_return_pct,
        "r_multiple": net_return_pct / estimated_risk_pct,
        "bars_held": exit_index - index + 1,
    }


def non_overlapping_event_trades(
    candidates: list[dict],
    history: dict[str, HistoricalSymbol],
    scenario: Scenario,
    exit_policy: str,
    setup_filter: str | None,
) -> list[dict]:
    rows = [
        row for row in candidates
        if setup_filter is None or row["setup_type"] == setup_filter
    ]
    rows.sort(key=lambda row: (
        row["index"],
        0 if row["setup_type"] == "range_breakout" else 1,
        -float(row["relative_volume_1h"] or 0),
        row["symbol"],
    ))
    available: dict[str, int] = defaultdict(int)
    used_signal: set[tuple[str, int]] = set()
    trades = []
    for candidate in rows:
        symbol = candidate["symbol"]
        signal_key = (symbol, int(candidate["index"]))
        if signal_key in used_signal or int(candidate["index"]) + 1 < available[symbol]:
            continue
        trade = execution_fill(
            history[symbol], candidate, scenario, exit_policy,
        )
        trades.append(trade)
        used_signal.add(signal_key)
        cooldown_bars = 4 if trade["net_return_pct"] < 0 else 3
        available[symbol] = int(trade["exit_index"]) + cooldown_bars
    return trades


def trade_metrics(trades: list[dict], *, seed: int = 7) -> dict:
    if not trades:
        return {
            "trades": 0,
            "win_rate_pct": None,
            "expectancy_r": None,
            "expectancy_r_ci95": [None, None],
            "profit_factor": None,
        }
    frame = pd.DataFrame(trades)
    pnl = frame["net_return_pct"].to_numpy(float)
    r_values = frame["r_multiple"].to_numpy(float)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    profit_factor = (
        float(wins.sum() / -losses.sum()) if len(losses) else math.inf
    )
    months = pd.to_datetime(
        frame["entry_ts"], unit="ms", utc=True,
    ).dt.tz_localize(None).dt.to_period("M").astype(str)
    grouped = {
        month: r_values[months == month]
        for month in sorted(months.unique())
    }
    rng = np.random.default_rng(seed)
    month_keys = list(grouped)
    boot = []
    if month_keys:
        for _ in range(4000):
            sampled = rng.choice(month_keys, len(month_keys), replace=True)
            values = np.concatenate([grouped[key] for key in sampled])
            boot.append(float(np.mean(values)))
    ci = (
        [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))]
        if boot else [None, None]
    )
    return {
        "trades": len(frame),
        "win_rate_pct": float(np.mean(pnl > 0) * 100),
        "expectancy_return_pct": float(np.mean(pnl)),
        "median_return_pct": float(np.median(pnl)),
        "expectancy_r": float(np.mean(r_values)),
        "expectancy_r_ci95": ci,
        "profit_factor": profit_factor,
        "average_hold_hours": float(np.mean(frame["bars_held"]) * 0.25),
        "take_profit_pct": float(
            np.mean(frame["exit_reason"] == "take_profit") * 100),
        "stop_pct": float(np.mean(frame["exit_reason"] == "stop") * 100),
        "max_hold_pct": float(
            np.mean(frame["exit_reason"] == "max_hold") * 100),
    }


def mark_equity(
    cash: float,
    positions: dict[str, dict],
    history: dict[str, HistoricalSymbol],
    index: int,
    price_field: str,
) -> float:
    unrealized = 0.0
    for symbol, position in positions.items():
        price = float(history[symbol].fast.iloc[index][price_field])
        sign = 1.0 if position["direction"] == "long" else -1.0
        unrealized += (
            (price - position["entry_price"]) * position["quantity"] * sign
        )
    return cash + unrealized


def portfolio_backtest(
    history: dict[str, HistoricalSymbol],
    candidates_by_index: dict[int, list[dict]],
    cfg: dict,
    scenario: Scenario,
    *,
    enforce_kill: bool = True,
) -> tuple[dict, list[dict], pd.DataFrame, dict]:
    from agent.risk import RiskEngine

    symbols = sorted(history)
    common_length = min(len(history[symbol].fast) for symbol in symbols)
    risk_engine = RiskEngine(cfg)
    cash = INITIAL_EQUITY
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_rows = []
    pending: list[dict] = []
    key_blocked_until: dict[str, int] = defaultdict(int)
    symbol_blocked_until: dict[str, int] = defaultdict(int)
    loss_memory: dict[str, dict] = {}
    rejected = defaultdict(int)
    killed = False
    kill_index = None
    high_water = INITIAL_EQUITY
    current_day = None
    day_start_equity = INITIAL_EQUITY
    day_stopped = False

    first_valid = min(
        (index for index, rows in candidates_by_index.items() if rows),
        default=0,
    )
    for index in range(first_valid + 1, common_length):
        ts = int(history[symbols[0]].fast.iloc[index]["timestamp_ms"])
        day = pd.Timestamp(ts, unit="ms", tz="UTC").date().isoformat()
        if day != current_day:
            current_day = day
            day_start_equity = mark_equity(
                cash, positions, history, index, "open",
            )
            day_stopped = False

        if not killed and pending:
            signal_index = index - 1
            snapshot_map = {
                symbol: history[symbol].snapshot(signal_index, cfg)
                for symbol in symbols
            }
            snapshot_map = {
                symbol: snap for symbol, snap in snapshot_map.items()
                if snap is not None
            }
            pending.sort(key=lambda row: (
                0 if row["setup_type"] == "range_breakout" else 1,
                -float(row["relative_volume_1h"] or 0),
                -float(row["momentum_abs"]),
                row["symbol"],
            ))
            seen_symbols = set()
            for candidate in pending:
                symbol = candidate["symbol"]
                if symbol in seen_symbols:
                    rejected["same_candle_lower_priority"] += 1
                    continue
                seen_symbols.add(symbol)
                if symbol in positions:
                    rejected["already_holding"] += 1
                    continue
                if ts < symbol_blocked_until[symbol]:
                    rejected["post_loss_cooldown"] += 1
                    continue
                if ts < key_blocked_until[candidate["setup_key"]]:
                    rejected["setup_cooldown"] += 1
                    continue
                previous_loss = loss_memory.get(candidate["setup_key"])
                if previous_loss:
                    if ts < previous_loss["closed_ts"] + 60 * 60 * 1000:
                        rejected["failed_thesis_time"] += 1
                        continue
                    if candidate["signal_1h_ts"] <= previous_loss["signal_1h_ts"]:
                        rejected["failed_thesis_fresh_hour"] += 1
                        continue
                    if (
                        candidate["evidence_fingerprint"]
                        == previous_loss["evidence_fingerprint"]
                    ):
                        rejected["failed_thesis_evidence"] += 1
                        continue
                equity = mark_equity(
                    cash, positions, history, index, "open",
                )
                positions_view = []
                active_trades = {}
                gross = 0.0
                for held_symbol, held in positions.items():
                    mark = float(history[held_symbol].fast.iloc[index]["open"])
                    held_notional = abs(mark * held["quantity"])
                    gross += held_notional
                    positions_view.append({
                        "symbol": held_symbol,
                        "side": held["direction"],
                        "notional": held_notional,
                    })
                    active_trades[held_symbol] = {
                        "risk_usd": held["risk_usd"],
                    }
                base_plan = candidate["plans"]["fixed_rr"]
                decision = {
                    "symbol": symbol,
                    "direction": candidate["direction"],
                    "confidence": 1.0,
                    "stop_loss_pct": base_plan["stop_pct"],
                    "take_profit_pct": base_plan["take_pct"],
                    "setup_type": candidate["setup_type"],
                    "strategy_id": "momentum",
                    "strategy_version": "phase1-v2",
                    "setup_id": (
                        f"{candidate['setup_key']}|{candidate['signal_ts']}"
                    ),
                    "setup_key": candidate["setup_key"],
                    "signal_ts": candidate["signal_ts"],
                    "exit_policy": "fixed_rr",
                    "invalidation_anchor": "structure",
                    "execution_choice": "normal",
                    "reasoning": "deterministic evidence proxy",
                }
                plan, why = risk_engine.vet_open(
                    decision,
                    equity,
                    positions_view,
                    snapshot_map,
                    {},
                    gross,
                    active_trades=active_trades,
                )
                if plan is None:
                    rejected[f"risk:{why}"] += 1
                    continue
                sign = 1.0 if candidate["direction"] == "long" else -1.0
                raw_open = float(history[symbol].fast.iloc[index]["open"])
                entry_price = raw_open * (
                    1 + sign * scenario.entry_slippage_pct / 100
                )
                quantity = float(plan["notional"]) / entry_price
                entry_fee = (
                    float(plan["notional"])
                    * scenario.fee_pct_per_side / 100
                )
                cash -= entry_fee
                positions[symbol] = {
                    "symbol": symbol,
                    "direction": candidate["direction"],
                    "setup_type": candidate["setup_type"],
                    "setup_key": candidate["setup_key"],
                    "signal_ts": candidate["signal_ts"],
                    "signal_1h_ts": candidate["signal_1h_ts"],
                    "evidence_fingerprint": candidate[
                        "evidence_fingerprint"],
                    "entry_index": index,
                    "entry_ts": ts,
                    "entry_price": entry_price,
                    "quantity": quantity,
                    "entry_notional": float(plan["notional"]),
                    "entry_equity": equity,
                    "entry_fee": entry_fee,
                    "risk_usd": float(plan["risk_usd"]),
                    "stop": entry_price * (
                        1 - sign * float(plan["sl_pct"]) / 100),
                    "take": entry_price * (
                        1 + sign * float(plan["tp_pct"]) / 100),
                    "stop_pct": float(plan["sl_pct"]),
                    "take_pct": float(plan["tp_pct"]),
                    "funding_usd": 0.0,
                }
        pending = []

        if not killed:
            for symbol, position in list(positions.items()):
                market = history[symbol]
                funding_match = np.where(market.funding_ts == ts)[0]
                if scenario.include_funding and len(funding_match):
                    rate = float(market.funding_rates[int(funding_match[0])])
                    mark = float(market.fast.iloc[index]["open"])
                    value = abs(mark * position["quantity"])
                    sign = 1.0 if position["direction"] == "long" else -1.0
                    funding_usd = -sign * value * rate
                    cash += funding_usd
                    position["funding_usd"] += funding_usd

            for symbol, position in list(positions.items()):
                market = history[symbol]
                bar = market.fast.iloc[index]
                direction = position["direction"]
                sign = 1.0 if direction == "long" else -1.0
                if direction == "long":
                    stop_hit = float(bar["low"]) <= position["stop"]
                    take_hit = float(bar["high"]) >= position["take"]
                else:
                    stop_hit = float(bar["high"]) >= position["stop"]
                    take_hit = float(bar["low"]) <= position["take"]
                reason = None
                raw_exit = None
                exit_slippage = scenario.ordinary_exit_slippage_pct
                if stop_hit:
                    reason = "stop"
                    exit_slippage = scenario.stop_slippage_pct
                    if direction == "long":
                        raw_exit = min(
                            position["stop"], float(bar["open"]))
                    else:
                        raw_exit = max(
                            position["stop"], float(bar["open"]))
                elif take_hit:
                    reason = "take_profit"
                    raw_exit = position["take"]
                elif index - position["entry_index"] + 1 >= MAX_HOLD_BARS:
                    reason = "max_hold"
                    raw_exit = float(bar["close"])
                if reason is None:
                    continue
                exit_price = float(raw_exit) * (
                    1 - sign * exit_slippage / 100
                )
                exit_notional = abs(exit_price * position["quantity"])
                exit_fee = (
                    exit_notional * scenario.fee_pct_per_side / 100
                )
                gross = (
                    (exit_price - position["entry_price"])
                    * position["quantity"] * sign
                )
                cash += gross - exit_fee
                pnl = (
                    gross
                    - position["entry_fee"]
                    - exit_fee
                    + position["funding_usd"]
                )
                exit_ts = ts + BAR_MS
                trade = {
                    "signal_ts": position["signal_ts"],
                    "entry_ts": position["entry_ts"],
                    "exit_ts": exit_ts,
                    "symbol": symbol,
                    "direction": direction,
                    "setup_type": position["setup_type"],
                    "scenario": scenario.name,
                    "exit_policy": "fixed_rr",
                    "exit_reason": reason,
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "notional_usd": position["entry_notional"],
                    "risk_usd": position["risk_usd"],
                    "gross_pnl_usd": gross,
                    "fees_usd": position["entry_fee"] + exit_fee,
                    "funding_usd": position["funding_usd"],
                    "net_pnl_usd": pnl,
                    "net_return_pct": (
                        pnl / position["entry_notional"] * 100),
                    "r_multiple": pnl / position["risk_usd"],
                    "bars_held": index - position["entry_index"] + 1,
                    "entry_equity": position["entry_equity"],
                }
                trades.append(trade)
                key_blocked_until[position["setup_key"]] = (
                    exit_ts + 45 * 60 * 1000
                )
                if pnl < 0:
                    symbol_blocked_until[symbol] = (
                        exit_ts + 45 * 60 * 1000
                    )
                    loss_memory[position["setup_key"]] = {
                        "closed_ts": exit_ts,
                        "signal_1h_ts": position["signal_1h_ts"],
                        "evidence_fingerprint": position[
                            "evidence_fingerprint"],
                    }
                del positions[symbol]

        equity_close = mark_equity(
            cash, positions, history, index, "close",
        )
        high_water = max(high_water, equity_close)
        drawdown_pct = (
            (high_water - equity_close) / high_water * 100
            if high_water > 0 else 0.0
        )
        day_pnl_pct = (
            (equity_close - day_start_equity) / day_start_equity * 100
            if day_start_equity > 0 else 0.0
        )
        if day_pnl_pct <= -float(cfg["risk"]["daily_loss_limit_pct"]):
            day_stopped = True

        if (
            enforce_kill
            and not killed
            and drawdown_pct >= float(cfg["risk"]["max_drawdown_pct"])
        ):
            for symbol, position in list(positions.items()):
                bar = history[symbol].fast.iloc[index]
                sign = 1.0 if position["direction"] == "long" else -1.0
                exit_price = float(bar["close"]) * (
                    1 - sign * scenario.ordinary_exit_slippage_pct / 100
                )
                exit_notional = abs(exit_price * position["quantity"])
                exit_fee = (
                    exit_notional * scenario.fee_pct_per_side / 100)
                gross = (
                    (exit_price - position["entry_price"])
                    * position["quantity"] * sign
                )
                cash += gross - exit_fee
                pnl = (
                    gross - position["entry_fee"] - exit_fee
                    + position["funding_usd"]
                )
                trades.append({
                    "signal_ts": position["signal_ts"],
                    "entry_ts": position["entry_ts"],
                    "exit_ts": ts + BAR_MS,
                    "symbol": symbol,
                    "direction": position["direction"],
                    "setup_type": position["setup_type"],
                    "scenario": scenario.name,
                    "exit_policy": "fixed_rr",
                    "exit_reason": "drawdown_kill",
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "notional_usd": position["entry_notional"],
                    "risk_usd": position["risk_usd"],
                    "gross_pnl_usd": gross,
                    "fees_usd": position["entry_fee"] + exit_fee,
                    "funding_usd": position["funding_usd"],
                    "net_pnl_usd": pnl,
                    "net_return_pct": (
                        pnl / position["entry_notional"] * 100),
                    "r_multiple": pnl / position["risk_usd"],
                    "bars_held": index - position["entry_index"] + 1,
                    "entry_equity": position["entry_equity"],
                })
            positions.clear()
            killed = True
            kill_index = index
            equity_close = cash

        equity_rows.append({
            "timestamp_ms": ts + BAR_MS,
            "equity": equity_close,
            "drawdown_pct": drawdown_pct,
            "day_pnl_pct": day_pnl_pct,
            "open_positions": len(positions),
            "day_stopped": day_stopped,
            "killed": killed,
        })
        if not killed and not day_stopped:
            pending = list(candidates_by_index.get(index, []))

    # Mark-to-market is not enough for a closed-period backtest. Flatten any
    # remaining positions at the final available close and include fees so
    # final equity reconciles exactly to realized trade PnL.
    if positions:
        index = common_length - 1
        ts = int(history[symbols[0]].fast.iloc[index]["timestamp_ms"])
        for symbol, position in list(positions.items()):
            bar = history[symbol].fast.iloc[index]
            sign = 1.0 if position["direction"] == "long" else -1.0
            exit_price = float(bar["close"]) * (
                1 - sign * scenario.ordinary_exit_slippage_pct / 100
            )
            exit_notional = abs(exit_price * position["quantity"])
            exit_fee = exit_notional * scenario.fee_pct_per_side / 100
            gross = (
                (exit_price - position["entry_price"])
                * position["quantity"] * sign
            )
            cash += gross - exit_fee
            pnl = (
                gross - position["entry_fee"] - exit_fee
                + position["funding_usd"]
            )
            trades.append({
                "signal_ts": position["signal_ts"],
                "entry_ts": position["entry_ts"],
                "exit_ts": ts + BAR_MS,
                "symbol": symbol,
                "direction": position["direction"],
                "setup_type": position["setup_type"],
                "scenario": scenario.name,
                "exit_policy": "fixed_rr",
                "exit_reason": "end_of_data",
                "entry_price": position["entry_price"],
                "exit_price": exit_price,
                "notional_usd": position["entry_notional"],
                "risk_usd": position["risk_usd"],
                "gross_pnl_usd": gross,
                "fees_usd": position["entry_fee"] + exit_fee,
                "funding_usd": position["funding_usd"],
                "net_pnl_usd": pnl,
                "net_return_pct": pnl / position["entry_notional"] * 100,
                "r_multiple": pnl / position["risk_usd"],
                "bars_held": index - position["entry_index"] + 1,
                "entry_equity": position["entry_equity"],
            })
        positions.clear()
        if equity_rows:
            high_water = max(
                high_water,
                max(float(row["equity"]) for row in equity_rows),
            )
            equity_rows[-1]["equity"] = cash
            equity_rows[-1]["open_positions"] = 0
            equity_rows[-1]["drawdown_pct"] = (
                (high_water - cash) / high_water * 100
                if high_water > 0 else 0.0
            )

    equity_frame = pd.DataFrame(equity_rows)
    metrics = portfolio_metrics(
        trades, equity_frame, INITIAL_EQUITY, killed, kill_index,
    )
    return metrics, trades, equity_frame, dict(sorted(rejected.items()))


def portfolio_metrics(
    trades: list[dict],
    equity: pd.DataFrame,
    initial_equity: float,
    killed: bool,
    kill_index: int | None,
) -> dict:
    if equity.empty:
        return {}
    final_equity = float(equity.iloc[-1]["equity"])
    peak = equity["equity"].cummax()
    drawdown = (peak - equity["equity"]) / peak
    daily = equity.copy()
    daily["date"] = pd.to_datetime(
        daily["timestamp_ms"], unit="ms", utc=True,
    ).dt.date
    daily_equity = daily.groupby("date")["equity"].last()
    daily_returns = daily_equity.pct_change().dropna()
    elapsed_days = max(
        1.0,
        (
            float(equity.iloc[-1]["timestamp_ms"])
            - float(equity.iloc[0]["timestamp_ms"])
        ) / 86_400_000,
    )
    total_return = final_equity / initial_equity - 1
    cagr = (
        (final_equity / initial_equity) ** (365.25 / elapsed_days) - 1
        if final_equity > 0 else -1.0
    )
    sharpe = (
        float(daily_returns.mean() / daily_returns.std() * math.sqrt(365))
        if len(daily_returns) > 2 and daily_returns.std() > 0 else None
    )
    trade_summary = trade_metrics(trades)
    trade_frame = pd.DataFrame(trades)
    return {
        "initial_equity_usd": initial_equity,
        "final_equity_usd": final_equity,
        "total_return_pct": total_return * 100,
        "cagr_pct": cagr * 100,
        "max_drawdown_pct": float(drawdown.max() * 100),
        "daily_sharpe": sharpe,
        "positive_months_pct": positive_months(equity),
        "killed_by_drawdown_guard": killed,
        "kill_index": kill_index,
        "net_realized_pnl_usd": (
            float(trade_frame["net_pnl_usd"].sum())
            if not trade_frame.empty else 0.0
        ),
        "fees_usd": (
            float(trade_frame["fees_usd"].sum())
            if not trade_frame.empty else 0.0
        ),
        "funding_usd": (
            float(trade_frame["funding_usd"].sum())
            if not trade_frame.empty else 0.0
        ),
        **trade_summary,
    }


def positive_months(equity: pd.DataFrame) -> float | None:
    if equity.empty:
        return None
    frame = equity.copy()
    frame["month"] = pd.to_datetime(
        frame["timestamp_ms"], unit="ms", utc=True,
    ).dt.tz_localize(None).dt.to_period("M")
    monthly = frame.groupby("month")["equity"].last().pct_change().dropna()
    return float(np.mean(monthly > 0) * 100) if len(monthly) else None


def segmented_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {}
    frame = pd.DataFrame(trades)
    frame["period"] = pd.to_datetime(
        frame["entry_ts"], unit="ms", utc=True,
    ).dt.strftime("%Y")
    output = {}
    for column in ("period", "symbol", "setup_type", "direction"):
        output[column] = {}
        for value, rows in frame.groupby(column):
            output[column][str(value)] = trade_metrics(
                rows.to_dict("records"))
    return output


def clean_json(value):
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def repository_commit(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def fmt(value, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def render_report(summary: dict) -> str:
    lines = [
        "# Phase1-v2 deterministic edge backtest",
        "",
        "## Scope",
        "",
        (
            f"- Period: {summary['period']['first_signal']} to "
            f"{summary['period']['last_bar']} UTC"
        ),
        f"- Instruments: {', '.join(summary['symbols'])}",
        (
            f"- Completed 15m bars per instrument: "
            f"{summary['data']['bars_per_symbol']:,}"
        ),
        (
            f"- Deterministic qualifying candidates: "
            f"{summary['candidate_count']:,}"
        ),
        "- Entry: next 15m bar open; maximum hold: 24 hours.",
        "- Same-bar SL+TP ambiguity: stop assumed first.",
        (
            "- Funding-squeeze setup: not tested because historical open "
            "interest is unavailable."
        ),
        "- LLM selector and LLM early closes: not replayed.",
        "",
        "## Non-overlapping setup event study",
        "",
        "| Setup | Exit | Costs | Trades | Win % | Exp R | 95% CI | PF |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["event_studies"]:
        metric = row["metrics"]
        ci = metric["expectancy_r_ci95"]
        lines.append(
            f"| {row['setup']} | {row['exit_policy']} | "
            f"{row['scenario']} | {metric['trades']} | "
            f"{fmt(metric['win_rate_pct'])} | "
            f"{fmt(metric['expectancy_r'], 3)} | "
            f"[{fmt(ci[0], 3)}, {fmt(ci[1], 3)}] | "
            f"{fmt(metric['profit_factor'], 3)} |"
        )
    lines.extend([
        "",
        "## Executable portfolio proxy (fixed 2R)",
        "",
        (
            "| Costs | Trades | Return % | CAGR % | Max DD % | Exp R | PF | "
            "Sharpe | Killed |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for scenario, result in summary["portfolio"].items():
        metric = result["metrics"]
        lines.append(
            f"| {scenario} | {metric.get('trades', 0)} | "
            f"{fmt(metric.get('total_return_pct'))} | "
            f"{fmt(metric.get('cagr_pct'))} | "
            f"{fmt(metric.get('max_drawdown_pct'))} | "
            f"{fmt(metric.get('expectancy_r'), 3)} | "
            f"{fmt(metric.get('profit_factor'), 3)} | "
            f"{fmt(metric.get('daily_sharpe'))} | "
            f"{metric.get('killed_by_drawdown_guard')} |"
        )
    lines.extend([
        "",
        "## Full-period diagnostic with drawdown kill disabled",
        "",
        (
            "This is not executable agent behavior; it shows whether the "
            "early kill hid a later recovery."
        ),
        "",
        "| Costs | Trades | Return % | Max DD % | Exp R | PF | Sharpe |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for scenario, result in summary["portfolio_no_kill"].items():
        metric = result["metrics"]
        lines.append(
            f"| {scenario} | {metric.get('trades', 0)} | "
            f"{fmt(metric.get('total_return_pct'))} | "
            f"{fmt(metric.get('max_drawdown_pct'))} | "
            f"{fmt(metric.get('expectancy_r'), 3)} | "
            f"{fmt(metric.get('profit_factor'), 3)} | "
            f"{fmt(metric.get('daily_sharpe'))} |"
        )
    verdict = summary["verdict"]
    lines.extend([
        "",
        "## Verdict",
        "",
        f"**{verdict['label']}**",
        "",
        verdict["reason"],
        "",
        "## Important limitations",
        "",
        (
            "1. This tests deterministic admissibility rules, not the "
            "complete agentic strategy. The LLM can accept, reject or close "
            "trades."
        ),
        (
            "2. The six-instrument fixed universe is not the live hourly "
            "top-10 volume universe, so universe-selection effects are absent."
        ),
        (
            "3. Historical order-book spread/depth and open interest are "
            "absent. Execution is tested through declared slippage scenarios "
            "instead."
        ),
        (
            "4. A current LLM deciding old data may know later history from "
            "training, so historical LLM replay would not be clean evidence."
        ),
        "5. This is retrospective research, not proof of future returns.",
        "",
    ])
    return "\n".join(lines)


def determine_verdict(summary: dict) -> dict:
    base = summary["portfolio"]["base"]["metrics"]
    stress = summary["portfolio"]["entry_cap"]["metrics"]
    base_ci = base.get("expectancy_r_ci95") or [None, None]
    robust = (
        (base.get("expectancy_r") or 0) > 0
        and (base.get("profit_factor") or 0) > 1
        and base_ci[0] is not None
        and base_ci[0] > 0
        and (stress.get("expectancy_r") or 0) > 0
        and (stress.get("profit_factor") or 0) > 1
        and not base.get("killed_by_drawdown_guard")
    )
    weak = (
        (base.get("expectancy_r") or 0) > 0
        and (base.get("profit_factor") or 0) > 1
        and not base.get("killed_by_drawdown_guard")
    )
    if robust:
        return {
            "label": "DETERMINISTIC PROXY EDGE OBSERVED",
            "reason": (
                "The fixed-RR portfolio remained positive after ordinary and "
                "entry-cap execution costs, and the month-clustered 95% "
                "expectancy interval remained above zero. This still does "
                "not establish the LLM selector's incremental edge."
            ),
        }
    if weak:
        return {
            "label": "WEAK / NOT YET ROBUST EDGE",
            "reason": (
                "The ordinary-cost proxy was positive, but uncertainty or "
                "execution stress failed the robustness threshold. Continue "
                "forward demo testing before risking live capital."
            ),
        }
    return {
        "label": "NO RELIABLE EDGE DEMONSTRATED",
        "reason": (
            "The ordinary-cost executable proxy did not satisfy positive "
            "expectancy, profit factor, uncertainty and drawdown criteria. "
            "The safety controls can limit losses, but they do not create a "
            "profitable trading edge by themselves."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=Path("."), type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument(
        "--output",
        default=Path("runtime/research/phase1-v2-backtest"),
        type=Path,
    )
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo))
    from agent.config import validate_config

    cfg = validate_config(yaml.safe_load(
        (args.repo / "config.yaml").read_text()))
    history, manifest = load_history(args.data)
    candidates, candidates_by_index = build_candidates(history, cfg)

    event_studies = []
    for setup in ("trend_continuation", "range_breakout", "combined"):
        setup_filter = None if setup == "combined" else setup
        for exit_policy in ("fixed_rr", "extended_rr", "structure_target"):
            for scenario_name in ("frictionless", "base", "entry_cap"):
                trades = non_overlapping_event_trades(
                    candidates,
                    history,
                    SCENARIOS[scenario_name],
                    exit_policy,
                    setup_filter,
                )
                study = {
                    "setup": setup,
                    "exit_policy": exit_policy,
                    "scenario": scenario_name,
                    "metrics": trade_metrics(trades),
                }
                if exit_policy == "fixed_rr":
                    study["segments"] = segmented_metrics(trades)
                event_studies.append(study)
                if setup == "combined" and exit_policy == "fixed_rr":
                    args.output.mkdir(parents=True, exist_ok=True)
                    pd.DataFrame(trades).to_csv(
                        args.output
                        / f"event_trades_fixed_rr_{scenario_name}.csv",
                        index=False,
                    )

    portfolio = {}
    for scenario_name in ("frictionless", "base", "entry_cap"):
        metrics, trades, equity, rejected = portfolio_backtest(
            history,
            candidates_by_index,
            cfg,
            SCENARIOS[scenario_name],
        )
        portfolio[scenario_name] = {
            "metrics": metrics,
            "segments": segmented_metrics(trades),
            "rejections": rejected,
        }
        args.output.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(trades).to_csv(
            args.output / f"portfolio_trades_{scenario_name}.csv",
            index=False,
        )
        equity.to_csv(
            args.output / f"portfolio_equity_{scenario_name}.csv",
            index=False,
        )

    portfolio_no_kill = {}
    for scenario_name in ("frictionless", "base", "entry_cap"):
        metrics, trades, equity, rejected = portfolio_backtest(
            history,
            candidates_by_index,
            cfg,
            SCENARIOS[scenario_name],
            enforce_kill=False,
        )
        portfolio_no_kill[scenario_name] = {
            "metrics": metrics,
            "segments": segmented_metrics(trades),
            "rejections": rejected,
        }
        pd.DataFrame(trades).to_csv(
            args.output / f"diagnostic_trades_no_kill_{scenario_name}.csv",
            index=False,
        )
        equity.to_csv(
            args.output / f"diagnostic_equity_no_kill_{scenario_name}.csv",
            index=False,
        )

    first_candidate = min(candidates, key=lambda row: row["signal_ts"])
    first_symbol = next(iter(history.values()))
    summary = {
        "methodology_version": "phase1-v2-proxy-backtest-v1",
        "repository_commit": repository_commit(args.repo),
        "symbols": sorted(history),
        "data": {
            "source": manifest["source"],
            "downloaded_at": manifest["downloaded_at"],
            "bars_per_symbol": len(first_symbol.fast),
            "manifest": manifest,
        },
        "period": {
            "first_signal": pd.Timestamp(
                first_candidate["signal_ts"], unit="ms", tz="UTC",
            ).isoformat(),
            "last_bar": pd.Timestamp(
                int(first_symbol.fast.iloc[-1]["timestamp_ms"]) + BAR_MS,
                unit="ms",
                tz="UTC",
            ).isoformat(),
        },
        "candidate_count": len(candidates),
        "event_studies": event_studies,
        "portfolio": portfolio,
        "portfolio_no_kill": portfolio_no_kill,
    }
    summary["verdict"] = determine_verdict(summary)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(
        json.dumps(clean_json(summary), indent=2, sort_keys=True) + "\n")
    (args.output / "REPORT.md").write_text(render_report(summary))
    print(json.dumps({
        "output": str(args.output),
        "candidate_count": len(candidates),
        "verdict": summary["verdict"],
        "portfolio": {
            key: value["metrics"] for key, value in portfolio.items()
        },
        "portfolio_no_kill": {
            key: value["metrics"]
            for key, value in portfolio_no_kill.items()
        },
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
