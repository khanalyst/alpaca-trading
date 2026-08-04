"""Independent edge laboratory for the phase1-v3 momentum strategy.

This is a second, deliberately independent implementation of the strategy's
signal surface, written to answer one question: **is there a tradable edge
here at all, and if so where does it live?**

Design rules that keep it honest:

1. Every vectorized feature is cross-checked bar-by-bar against the
   authoritative `agent.market` / `agent.strategy` implementations on random
   samples. A mismatch is a hard failure, not a warning.
2. Nothing may read a bar that had not closed at decision time. Entries
   happen at the open of the bar AFTER the signal bar.
3. The tradable universe is reconstructed point-in-time from trailing 24h
   quote volume, the way the live agent rebuilds it hourly. A fixed
   hand-picked symbol list would bake in survivorship.
4. Every hypothesis is measured against explicit null baselines (random
   timing, random direction, inverted signal) so "positive expectancy" is
   never confused with "better than nothing".
5. Parameter searches are scored out-of-sample on a purged walk-forward
   split, and reported with the number of hypotheses tested so the reader
   can discount for multiple comparisons.

Usage:
    python research/edge_lab.py --data runtime/research/data --stage all
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import zlib
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

BAR_MS = 900_000
HOUR_MS = 3_600_000
FOUR_HOUR_MS = 4 * HOUR_MS
DAY_BARS = 96
EWM_WINDOW = 120          # live agent fetches cycle.candles=120 completed bars
MIN_HISTORY = 60          # universe.min_history_candles


# Indicators

def windowed_ewm_last(
    values: np.ndarray,
    alpha: float,
    window: int = EWM_WINDOW,
    first_override: np.ndarray | None = None,
) -> np.ndarray:
    """Exact `ewm(adjust=False)` value at the end of each fixed-size window.

    The live agent recomputes EMAs from a rolling 120-bar fetch every cycle,
    so its EMA is a *windowed* EWM, not an infinite-history one. Reproducing
    that exactly matters: an infinite-history EMA drifts away from the live
    value and silently changes which bars satisfy the trend contract.
    """
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) < window:
        return out
    q = 1.0 - alpha
    weights = np.empty(window, dtype=float)
    weights[0] = q ** (window - 1)
    weights[1:] = alpha * (q ** np.arange(window - 2, -1, -1, dtype=float))
    rolled = np.convolve(values, weights[::-1], mode="valid")
    if first_override is not None:
        starts = np.arange(len(rolled))
        rolled += weights[0] * (
            np.asarray(first_override, dtype=float)[starts] - values[starts])
    out[window - 1:] = rolled
    return out


def aggregate_bars(df: pd.DataFrame, duration_ms: int) -> pd.DataFrame:
    work = df.copy()
    work["bucket"] = (work["timestamp_ms"] // duration_ms) * duration_ms
    agg = {
        "open": ("open", "first"), "high": ("high", "max"),
        "low": ("low", "min"), "close": ("close", "last"),
        "volume": ("volume", "sum"),
    }
    if "quote_volume" in work:
        agg["quote_volume"] = ("quote_volume", "sum")
    return (
        work.groupby("bucket", sort=True, as_index=False).agg(**agg)
        .rename(columns={"bucket": "timestamp_ms"})
    )


def trend_labels(close: np.ndarray) -> np.ndarray:
    e20 = windowed_ewm_last(close, 2 / 21)
    e50 = windowed_ewm_last(close, 2 / 51)
    trend = np.full(len(close), None, dtype=object)
    ok = ~(np.isnan(e20) | np.isnan(e50))
    trend[ok] = "flat"
    trend[ok & (close > e20) & (e20 > e50)] = "up"
    trend[ok & (close < e20) & (e20 < e50)] = "down"
    return trend, e20


def atr_percent(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    high = frame["high"].to_numpy(float)
    low = frame["low"].to_numpy(float)
    close = frame["close"].to_numpy(float)
    previous = np.roll(close, 1)
    previous[0] = np.nan
    true_range = np.maximum.reduce([
        high - low, np.abs(high - previous), np.abs(low - previous)])
    true_range[0] = high[0] - low[0]
    atr = windowed_ewm_last(true_range, 1 / 14, first_override=high - low)
    atr_pct = atr / close * 100
    baseline = (
        pd.Series(atr_pct).shift(1).rolling(50, min_periods=40).median()
        .to_numpy(float))
    ratio = np.divide(atr_pct, baseline,
                      out=np.full(len(atr_pct), np.nan), where=baseline > 0)
    return atr_pct, ratio


# Symbol frames

class SymbolFrame:
    """All point-in-time features for one instrument, indexed by 15m bar."""

    def __init__(self, symbol: str, swap: pd.DataFrame,
                 spot: pd.DataFrame | None, funding: pd.DataFrame,
                 oi: pd.DataFrame | None, btc_1h: pd.DataFrame | None):
        self.symbol = symbol
        swap = swap.sort_values("timestamp_ms").reset_index(drop=True)
        self.fast = swap
        n = len(swap)
        close = swap["close"].to_numpy(float)
        high = swap["high"].to_numpy(float)
        low = swap["low"].to_numpy(float)
        open_ = swap["open"].to_numpy(float)
        self.ts = swap["timestamp_ms"].to_numpy(np.int64)

        one = aggregate_bars(swap, HOUR_MS)
        four = aggregate_bars(swap, FOUR_HOUR_MS)
        trend15, _ = trend_labels(close)
        trend1h, ema20_1h = trend_labels(one["close"].to_numpy(float))
        trend4h, _ = trend_labels(four["close"].to_numpy(float))
        atr1h, atr_ratio = atr_percent(one)

        # Map each 15m bar to the newest 1h/4h bar that had CLOSED by then.
        signal_end = self.ts + BAR_MS
        map_one = np.searchsorted(
            one["timestamp_ms"].to_numpy(np.int64) + HOUR_MS,
            signal_end, side="right") - 1
        map_four = np.searchsorted(
            four["timestamp_ms"].to_numpy(np.int64) + FOUR_HOUR_MS,
            signal_end, side="right") - 1
        self.map_one, self.map_four = map_one, map_four

        def pull(values, mapping):
            out = np.full(n, np.nan, dtype=float)
            ok = mapping >= 0
            out[ok] = np.asarray(values, dtype=float)[mapping[ok]]
            return out

        def pull_obj(values, mapping):
            out = np.full(n, None, dtype=object)
            ok = mapping >= 0
            out[ok] = np.asarray(values, dtype=object)[mapping[ok]]
            return out

        data = {}
        data["timestamp_ms"] = self.ts
        data["open"], data["high"] = open_, high
        data["low"], data["close"] = low, close
        data["trend_15m"] = trend15
        data["trend_1h"] = pull_obj(trend1h, map_one)
        data["trend_4h"] = pull_obj(trend4h, map_four)
        data["atr_1h_pct"] = np.round(pull(atr1h, map_one), 2)
        data["atr_1h_ratio"] = np.round(pull(atr_ratio, map_one), 2)
        data["signal_1h_ts"] = pull(
            one["timestamp_ms"].to_numpy(float), map_one)
        ema20 = pull(ema20_1h, map_one)
        data["ema20_1h_dist_pct"] = np.round((close - ema20) / ema20 * 100, 2)

        series_close = pd.Series(close)
        data["mom_15m_pct"] = np.round(
            series_close.pct_change().to_numpy(float) * 100, 3)
        data["mom_1h_pct"] = np.round(
            series_close.pct_change(4).to_numpy(float) * 100, 2)
        data["signal_candle_return_pct"] = np.round(
            (close - open_) / open_ * 100, 3)

        s_high, s_low = pd.Series(high), pd.Series(low)
        prior_high = s_high.shift(1).rolling(20).max().to_numpy(float)
        prior_low = s_low.shift(1).rolling(20).min().to_numpy(float)
        data["fresh_breakout_long"] = (close > prior_high)
        data["fresh_breakout_short"] = (close < prior_low)
        data["price_stabilized_long"] = (
            (low >= np.roll(low, 1)) & (close > open_))
        data["price_stabilized_short"] = (
            (high <= np.roll(high, 1)) & (close < open_))
        data["price_stabilized_long"][0] = False
        data["price_stabilized_short"][0] = False

        volume = pd.Series(swap["volume"].to_numpy(float))
        hour_volume = volume.rolling(4).sum()
        normal = hour_volume.shift(4).rolling(20).median()
        data["relative_volume_1h"] = np.round(
            (hour_volume / normal).to_numpy(float), 2)

        swing_low = s_low.rolling(20).min().to_numpy(float)
        swing_high = s_high.rolling(20).max().to_numpy(float)
        data["swing_low_pct"] = np.round((close - swing_low) / close * 100, 2)
        data["swing_high_pct"] = np.round((swing_high - close) / close * 100, 2)

        high_24 = s_high.rolling(DAY_BARS).max().to_numpy(float)
        low_24 = s_low.rolling(DAY_BARS).min().to_numpy(float)
        span = high_24 - low_24
        data["range_pos_pct"] = np.round(
            np.divide(close - low_24, span,
                      out=np.full(n, np.nan), where=span > 0) * 100, 0)

        # Trailing 24h quote turnover drives the live universe ranking.
        quote = swap.get("quote_volume")
        if quote is None:
            quote = swap["volume"] * swap["close"]
        data["qv_24h_usd"] = (
            pd.Series(np.asarray(quote, dtype=float))
            .rolling(DAY_BARS).sum().to_numpy(float))

        # Use the funding rate in force at the signal bar's close.
        if funding is None or funding.empty:
            funding = pd.DataFrame(
                {"timestamp_ms": pd.Series(dtype="int64"),
                 "funding_rate": pd.Series(dtype="float64")})
        funding = funding.sort_values("timestamp_ms").reset_index(drop=True)
        f_ts = funding["timestamp_ms"].to_numpy(np.int64)
        f_rate = funding["funding_rate"].to_numpy(float)
        self.funding_ts, self.funding_rates = f_ts, f_rate
        f_index = np.searchsorted(f_ts, signal_end, side="right") - 1
        rate_pct = np.full(n, np.nan)
        pct_rank = np.full(n, np.nan)
        samples = np.zeros(n, dtype=int)
        next_minutes = np.full(n, np.nan)
        interval_hours = np.full(n, 8.0)
        if len(f_ts):
            roll = pd.Series(f_rate)
            # Percentile of the current rate inside its own trailing 30
            # settlements, matching agent.market._funding_history_context.
            # The live agent compares raw history against the *rounded*
            # current rate; reproduce that or borderline ties disagree.
            ranks = np.full(len(f_rate), np.nan)
            scaled = f_rate * 100
            for i in range(len(f_rate)):
                window = scaled[max(0, i - 29):i + 1]
                ranks[i] = (
                    np.sum(window <= round(float(scaled[i]), 4))
                    / len(window) * 100)
            counts = roll.rolling(30, min_periods=1).count().to_numpy(float)
            ok = f_index >= 0
            rate_pct[ok] = f_rate[f_index[ok]] * 100
            pct_rank[ok] = ranks[f_index[ok]]
            samples[ok] = counts[f_index[ok]].astype(int)
            # Derive the next settlement from the current/previous observed
            # interval. Looking up f_ts[f_index + 1] makes the last retained
            # bar change when future funding rows are removed, which is a
            # direct lookahead leak at a truncated corpus boundary.
            interval_ms = np.full(len(f_ts), 8 * HOUR_MS, dtype=float)
            if len(f_ts) > 1:
                observed = np.diff(f_ts).astype(float)
                positive = observed[observed > 0]
                default_interval = (float(np.median(positive))
                                    if len(positive) else 8 * HOUR_MS)
                interval_ms[0] = default_interval
                interval_ms[1:] = np.where(observed > 0, observed,
                                           default_interval)
            next_ts = f_ts + interval_ms
            next_minutes[ok] = np.maximum(
                0.0, (next_ts[f_index[ok]] - signal_end[ok]) / 60_000)
            interval_hours[ok] = interval_ms[f_index[ok]] / HOUR_MS
        data["funding_rate_pct"] = np.round(rate_pct, 4)
        data["funding_percentile_30"] = np.round(pct_rank, 1)
        data["funding_samples_30"] = samples
        data["next_funding_minutes"] = np.round(next_minutes, 1)
        data["funding_interval_hours"] = np.round(interval_hours, 2)
        # Cumulative funding paid by a long, indexed by 15m bar, so a hold's
        # funding cost is a difference of two array lookups.
        #
        # OKX serves only ~97 days of funding-rate history, far less than the
        # candle history. Outside that window measured funding is simply
        # absent, so a "measured" run silently charges nothing. Record the
        # covered span and a per-symbol median rate so the caller can either
        # restrict to covered trades or apply a calibrated constant instead
        # of pretending funding was zero for two years.
        settle_bar = np.searchsorted(self.ts, f_ts, side="left")
        cumulative = np.zeros(n + 1)
        for bar, rate in zip(settle_bar, f_rate):
            if 0 <= bar <= n:
                cumulative[bar] += rate
        self.cum_funding = np.cumsum(cumulative)[:n + 1] * 100
        self.funding_covered_from = int(f_ts[0]) if len(f_ts) else None
        self.funding_covered_to = int(f_ts[-1]) if len(f_ts) else None
        self.funding_median_pct = (
            float(np.median(f_rate) * 100) if len(f_rate) else 0.0)
        self.funding_interval_h = (
            float(np.median(np.diff(f_ts)) / HOUR_MS)
            if len(f_ts) > 2 else 8.0)

        # Perpetual/index basis from spot.
        if spot is not None and not spot.empty:
            spot_close = (spot.sort_values("timestamp_ms")
                          .set_index("timestamp_ms")["close"])
            aligned = spot_close.reindex(self.ts).to_numpy(float)
            data["perp_index_basis_pct"] = (close - aligned) / aligned * 100
        else:
            data["perp_index_basis_pct"] = np.full(n, np.nan)

        # Hourly open interest, forward-filled from the last known value.
        if oi is not None and not oi.empty:
            oi = oi.sort_values("timestamp_ms")
            oi_ts = oi["timestamp_ms"].to_numpy(np.int64)
            oi_usd = oi["oi_usd"].to_numpy(float)
            idx = np.searchsorted(oi_ts, signal_end, side="right") - 1
            value = np.full(n, np.nan)
            ok = idx >= 0
            value[ok] = oi_usd[idx[ok]] / 1e6
            data["open_interest_musd"] = np.round(value, 2)
        else:
            data["open_interest_musd"] = np.full(n, np.nan)

        # BTC beta on 1h returns (72 observations; at least 24 required).
        beta = np.full(n, np.nan)
        corr_samples = np.zeros(n, dtype=int)
        if btc_1h is not None and symbol != "BTC/USDT:USDT":
            own = one.set_index("timestamp_ms")["close"].pct_change()
            bench = btc_1h.set_index("timestamp_ms")["close"].pct_change()
            joined = pd.concat([own.rename("s"), bench.rename("b")],
                               axis=1, join="inner")
            cov = joined["s"].rolling(72, min_periods=24).cov(joined["b"])
            var = joined["b"].rolling(72, min_periods=24).var()
            count = joined.notna().all(axis=1).rolling(72).sum()
            beta_1h = (cov / var).reindex(
                one["timestamp_ms"].to_numpy()).to_numpy(float)
            count_1h = count.reindex(
                one["timestamp_ms"].to_numpy()).to_numpy(float)
            beta = pull(beta_1h, map_one)
            mapped_count = pull(count_1h, map_one)
            corr_samples = np.nan_to_num(mapped_count).astype(int)
        elif symbol == "BTC/USDT:USDT":
            beta = np.ones(n)
            corr_samples = np.full(n, 72, dtype=int)
        data["beta_btc_1h_72"] = np.round(beta, 2)
        data["corr_btc_samples"] = corr_samples

        self.df = pd.DataFrame(data)
        self._add_regime()

    def _add_regime(self) -> None:
        """Vectorized copy of agent.market.classify_regime."""
        d = self.df
        atr_ratio = np.nan_to_num(d["atr_1h_ratio"].to_numpy(float), nan=0.0)
        atr = np.maximum(
            np.nan_to_num(d["atr_1h_pct"].to_numpy(float), nan=0.0), 0.01)
        mom = np.nan_to_num(d["mom_1h_pct"].to_numpy(float), nan=0.0)
        one = d["trend_1h"].to_numpy(object)
        four = d["trend_4h"].to_numpy(object)
        regime = np.full(len(d), "transition", dtype=object)
        choppy = (one == "flat") & (np.abs(mom) < atr * 0.25)
        regime[choppy] = "choppy"
        regime[(one == "down") & (four == "down")] = "trend_down"
        regime[(one == "up") & (four == "up")] = "trend_up"
        regime[atr_ratio >= 1.75] = "high_volatility"
        d["regime"] = regime


# Evidence layer

@dataclass(frozen=True)
class Contract:
    """Tunable copy of the phase1-v3 evidence contract and stop/target rules."""
    breakout_range_threshold_pct: float = 85.0
    breakout_min_relative_volume: float = 1.0
    # 8h-equivalent, matching agent/strategy.py after the interval-
    # normalization fix. Kept in sync deliberately: validate_features.py
    # compares these masks against the authoritative implementation.
    funding_extreme_pct_per_8h: float = 0.01
    hard_max_entry_extension_atr: float = 2.5
    min_stop_atr_multiple: float = 1.0
    structure_buffer_atr_multiple: float = 0.15
    fixed_reward_risk: float = 2.0
    extended_reward_risk: float = 3.0
    max_hold_bars: int = DAY_BARS
    # Optional research-only filters, all disabled at the shipped defaults.
    require_trend_15m_aligned: bool = False
    min_atr_ratio: float | None = None
    max_atr_ratio: float | None = None
    min_relative_volume: float | None = None
    max_extension_atr: float | None = None
    allowed_regimes: tuple[str, ...] | None = None
    allowed_hours_utc: tuple[int, ...] | None = None
    require_funding_tailwind: bool = False
    min_stop_pct: float = 0.2
    max_stop_pct: float = 15.0

    # StrategyContract protocol
    # build_trades talks to a contract through these three methods and the
    # four scalars below, so a new strategy is a new class rather than an
    # edit to the shared trade builder. Momentum delegates to the
    # module-level functions it has always used, which keeps its behaviour
    # byte-identical and keeps validate_features.py meaningful.
    default_setups: tuple[str, ...] = ("trend_continuation", "range_breakout")

    def masks(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        return evidence_masks(df, self)

    def gate(self, df: pd.DataFrame, direction: str) -> np.ndarray:
        return research_gate(df, self, direction)

    def levels(self, df: pd.DataFrame, direction: str,
               exit_policy: str) -> tuple[np.ndarray, np.ndarray]:
        return derive_levels(df, self, direction, exit_policy)

    @classmethod
    def from_config(cls, cfg: dict) -> "Contract":
        """Build the research contract from a validated live config.

        The defaults above are a snapshot, and snapshots drift: config.yaml
        moved to a 3R target while these defaults still read 2.0, so anything
        comparing the two was comparing different strategies. Sourcing the
        tunables from the config is what makes validate_features.py a check
        on the shipped configuration rather than on a stale copy of it.
        """
        strategy = cfg["strategy"]
        risk = cfg["risk"]
        return cls(
            breakout_range_threshold_pct=float(
                strategy["breakout_range_threshold_pct"]),
            breakout_min_relative_volume=float(
                strategy["breakout_min_relative_volume"]),
            funding_extreme_pct_per_8h=float(
                strategy["funding_extreme_pct_per_8h"]),
            hard_max_entry_extension_atr=float(
                strategy["hard_max_entry_extension_atr"]),
            min_stop_atr_multiple=float(strategy["min_stop_atr_multiple"]),
            structure_buffer_atr_multiple=float(
                strategy["structure_buffer_atr_multiple"]),
            fixed_reward_risk=float(strategy["fixed_reward_risk"]),
            extended_reward_risk=float(strategy["extended_reward_risk"]),
            # 15m signal bars: four per hour.
            max_hold_bars=int(round(float(risk["max_hold_hours"]) * 4)),
        )


@dataclass(frozen=True)
class FlushFadeContract:
    """Fade forced deleveraging; stand aside for new positioning.

    Implements the StrategyContract protocol, so build_trades drives it with
    the same simulator, the same null machinery and the same cost scenarios
    that measured the momentum contract. Nothing about the comparison is
    special-cased in the candidate's favour.

    The distinction the whole hypothesis rests on is open interest. An
    adverse move while OI FALLS is existing positions being closed - often
    forcibly, by a liquidation engine that sells at market regardless of
    price. That flow is price-insensitive and finite, so it overshoots. An
    adverse move while OI RISES is new money taking a side, which carries no
    such reason to revert. Both look identical on a price chart.

    Parameters are pre-registered in research/hypotheses/flush-fade.yaml
    before any result is produced; see that file for the hypothesis count
    that any reported number must be discounted against.
    """

    # Lookback for the flush, in 15m bars. 16 bars = 4 hours: long enough to
    # contain a cascade, short enough that OI change still refers to it.
    lookback_bars: int = 16
    # How large the adverse move must be, in ATRs. Below this it is noise.
    min_move_atr: float = 1.5
    # How much open interest must have fallen across the same window, in
    # percent. This is the gate that separates deleveraging from positioning.
    min_oi_drop_pct: float = 1.0
    # Participation floor: a flush without volume is drift, not forced flow.
    min_relative_volume: float = 1.2
    # Stop and target, in ATR multiples. Wide stop because the entry is
    # deliberately inside a volatile move.
    stop_atr_multiple: float = 1.5
    reward_risk: float = 2.0
    max_hold_bars: int = 96          # 24h, matching the registered ceiling
    min_stop_pct: float = 0.2
    max_stop_pct: float = 15.0
    hard_max_entry_extension_atr: float = 1e9   # extension is not this
    default_setups: tuple[str, ...] = ("flush_reversion",)

    def _features(self, df: pd.DataFrame):
        close = pd.Series(df["close"].to_numpy(float))
        atr = df["atr_1h_pct"].to_numpy(float)
        move_pct = close.pct_change(self.lookback_bars).to_numpy(float) * 100
        with np.errstate(divide="ignore", invalid="ignore"):
            move_atr = np.where(atr > 0, move_pct / atr, np.nan)
        oi = pd.Series(df["open_interest_musd"].to_numpy(float))
        oi_change = oi.pct_change(self.lookback_bars).to_numpy(float) * 100
        rel_vol = df["relative_volume_1h"].to_numpy(float)
        return move_atr, oi_change, rel_vol

    def masks(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        move_atr, oi_change, rel_vol = self._features(df)
        deleveraging = (
            ~np.isnan(oi_change) & (oi_change <= -self.min_oi_drop_pct)
            & ~np.isnan(rel_vol) & (rel_vol >= self.min_relative_volume))
        # Fade the direction of the flush: buy the forced selling.
        long_ = deleveraging & ~np.isnan(move_atr) & (
            move_atr <= -self.min_move_atr)
        short_ = deleveraging & ~np.isnan(move_atr) & (
            move_atr >= self.min_move_atr)
        zero = np.zeros(len(df), dtype=float)
        return {
            "flush_reversion_long": long_,
            "flush_reversion_short": short_,
            # Extension from the EMA20 is a momentum-chasing concept; this
            # strategy is deliberately entering an extended move, so the
            # shared extension filter must not bind. Zero always passes.
            "extension_long": zero,
            "extension_short": zero,
        }

    def gate(self, df: pd.DataFrame, direction: str) -> np.ndarray:
        return np.ones(len(df), dtype=bool)

    def levels(self, df: pd.DataFrame, direction: str,
               exit_policy: str) -> tuple[np.ndarray, np.ndarray]:
        atr = df["atr_1h_pct"].to_numpy(float)
        stop = atr * self.stop_atr_multiple
        return np.round(stop, 6), np.round(stop * self.reward_risk, 6)


@dataclass(frozen=True)
class TrendMultidayContract:
    """Multi-day trend continuation, mirroring agent/contracts/trend_multiday.

    The reason to test trend at this horizon rather than the 15m one is
    arithmetic: round-trip cost is roughly 15% of a typical intraday move and
    roughly 1% of a multi-day one. The same effect can survive at one
    timescale and cannot at the other.
    """

    min_range_pos_pct: float = 55.0
    max_atr_ratio: float = 2.0
    stop_atr_multiple: float = 2.0
    reward_risk: float = 3.0
    max_hold_bars: int = 1344              # 14 days of 15m bars
    min_stop_pct: float = 0.2
    max_stop_pct: float = 15.0
    hard_max_entry_extension_atr: float = 1e9
    default_setups: tuple[str, ...] = ("trend_follow",)

    def masks(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        trend_1h = df["trend_1h"].to_numpy(object)
        trend_4h = df["trend_4h"].to_numpy(object)
        momentum = np.nan_to_num(df["mom_1h_pct"].to_numpy(float))
        ratio = df["atr_1h_ratio"].to_numpy(float)
        range_pos = df["range_pos_pct"].to_numpy(float)
        calm = ~np.isnan(ratio) & (ratio <= self.max_atr_ratio)
        long_ = ((trend_1h == "up") & (trend_4h == "up") & (momentum > 0)
                 & calm & ~np.isnan(range_pos)
                 & (range_pos >= self.min_range_pos_pct))
        short_ = ((trend_1h == "down") & (trend_4h == "down")
                  & (momentum < 0) & calm & ~np.isnan(range_pos)
                  & (range_pos <= 100 - self.min_range_pos_pct))
        zero = np.zeros(len(df), dtype=float)
        return {"trend_follow_long": long_, "trend_follow_short": short_,
                "extension_long": zero, "extension_short": zero}

    def gate(self, df: pd.DataFrame, direction: str) -> np.ndarray:
        return np.ones(len(df), dtype=bool)

    def levels(self, df: pd.DataFrame, direction: str,
               exit_policy: str) -> tuple[np.ndarray, np.ndarray]:
        stop = df["atr_1h_pct"].to_numpy(float) * self.stop_atr_multiple
        return np.round(stop, 6), np.round(stop * self.reward_risk, 6)


@dataclass(frozen=True)
class FundingCarryContract:
    """Hold the funding-receiving side while positioning stays crowded.

    Mirrors agent/contracts/funding_carry. The return source is the carry,
    not a directional forecast, so the entry asks about funding relative to
    the instrument's own history rather than about price.

    Note what the simulator does and does not capture: it charges funding
    through Costs.include_funding, so the carry is priced. What it cannot do
    is model the position being held purely for carry with no price target,
    which is why the reward:risk here is wide rather than tight.
    """

    funding_extreme_pct_per_8h: float = 0.01
    percentile: float = 80.0
    min_samples: int = 20
    stop_atr_multiple: float = 3.0
    reward_risk: float = 2.0
    max_hold_bars: int = 960               # 10 days
    min_stop_pct: float = 0.2
    max_stop_pct: float = 15.0
    hard_max_entry_extension_atr: float = 1e9
    default_setups: tuple[str, ...] = ("carry",)

    def masks(self, df: pd.DataFrame) -> dict[str, np.ndarray]:
        funding = df["funding_rate_pct"].to_numpy(float)
        interval = df["funding_interval_hours"].to_numpy(float)
        interval = np.where((interval > 0) & ~np.isnan(interval), interval, 8.0)
        funding_8h = funding * (8.0 / interval)
        percentile = df["funding_percentile_30"].to_numpy(float)
        samples = np.nan_to_num(df["funding_samples_30"].to_numpy(float))
        trend_1h = df["trend_1h"].to_numpy(object)
        trend_4h = df["trend_4h"].to_numpy(object)
        context = (samples >= self.min_samples) & ~np.isnan(percentile) \
            & ~np.isnan(funding_8h)
        both_up = (trend_1h == "up") & (trend_4h == "up")
        both_down = (trend_1h == "down") & (trend_4h == "down")
        short_ = (context & (funding_8h >= self.funding_extreme_pct_per_8h)
                  & (percentile >= self.percentile) & ~both_up)
        long_ = (context & (funding_8h <= -self.funding_extreme_pct_per_8h)
                 & (percentile <= 100 - self.percentile) & ~both_down)
        zero = np.zeros(len(df), dtype=float)
        return {"carry_long": long_, "carry_short": short_,
                "extension_long": zero, "extension_short": zero}

    def gate(self, df: pd.DataFrame, direction: str) -> np.ndarray:
        return np.ones(len(df), dtype=bool)

    def levels(self, df: pd.DataFrame, direction: str,
               exit_policy: str) -> tuple[np.ndarray, np.ndarray]:
        stop = df["atr_1h_pct"].to_numpy(float) * self.stop_atr_multiple
        return np.round(stop, 6), np.round(stop * self.reward_risk, 6)


def evidence_masks(df: pd.DataFrame, c: Contract) -> dict[str, np.ndarray]:
    """Vectorized agent.strategy.setup_evidence (+ optional research gates)."""
    atr = np.maximum(np.nan_to_num(df["atr_1h_pct"].to_numpy(float)), 0.0)
    ema_dist = np.nan_to_num(df["ema20_1h_dist_pct"].to_numpy(float))
    with np.errstate(divide="ignore", invalid="ignore"):
        ext_long = np.where(atr > 0, np.maximum(0.0, ema_dist / atr), np.nan)
        ext_short = np.where(atr > 0, np.maximum(0.0, -ema_dist / atr), np.nan)

    t15 = df["trend_15m"].to_numpy(object)
    t1h = df["trend_1h"].to_numpy(object)
    t4h = df["trend_4h"].to_numpy(object)
    range_pos = df["range_pos_pct"].to_numpy(float)
    rel_vol = df["relative_volume_1h"].to_numpy(float)
    mom = np.nan_to_num(df["mom_1h_pct"].to_numpy(float))
    fast_mom = np.nan_to_num(df["mom_15m_pct"].to_numpy(float))
    funding = df["funding_rate_pct"].to_numpy(float)
    f_pct = df["funding_percentile_30"].to_numpy(float)
    f_samples = df["funding_samples_30"].to_numpy(float)
    basis = df["perp_index_basis_pct"].to_numpy(float)
    oi = df["open_interest_musd"].to_numpy(float)

    have_range = ~np.isnan(range_pos)
    have_vol = ~np.isnan(rel_vol)
    breakout_long = (
        have_range & have_vol
        & (range_pos >= c.breakout_range_threshold_pct)
        & (rel_vol >= c.breakout_min_relative_volume)
        & (mom > 0) & (fast_mom > 0)
        & df["fresh_breakout_long"].to_numpy(bool)
        & (t1h != "down"))
    breakout_short = (
        have_range & have_vol
        & (range_pos <= 100 - c.breakout_range_threshold_pct)
        & (rel_vol >= c.breakout_min_relative_volume)
        & (mom < 0) & (fast_mom < 0)
        & df["fresh_breakout_short"].to_numpy(bool)
        & (t1h != "up"))
    continuation_long = (
        (t1h == "up") & (t4h == "up") & (t15 != "down")
        & (mom > 0) & (fast_mom > 0))
    continuation_short = (
        (t1h == "down") & (t4h == "down") & (t15 != "up")
        & (mom < 0) & (fast_mom < 0))

    # Normalize funding to an 8h equivalent before the absolute test, so a 4h
    # contract is not held to a bar twice as strict in economic terms.
    interval = df["funding_interval_hours"].to_numpy(float)
    interval = np.where(np.isnan(interval) | (interval <= 0), 8.0, interval)
    funding_8h = funding * (8.0 / interval)

    squeeze_ctx = (
        (f_samples >= 10) & ~np.isnan(f_pct) & ~np.isnan(basis)
        & ~np.isnan(oi) & (oi > 0))
    squeeze_long = (
        ~np.isnan(funding_8h)
        & (funding_8h <= -c.funding_extreme_pct_per_8h) & squeeze_ctx
        & (f_pct <= 25) & (basis <= 0) & (t1h != "down")
        & df["price_stabilized_long"].to_numpy(bool) & (fast_mom > 0))
    squeeze_short = (
        ~np.isnan(funding_8h)
        & (funding_8h >= c.funding_extreme_pct_per_8h) & squeeze_ctx
        & (f_pct >= 75) & (basis >= 0) & (t1h != "up")
        & df["price_stabilized_short"].to_numpy(bool) & (fast_mom < 0))

    return {
        "trend_continuation_long": continuation_long,
        "trend_continuation_short": continuation_short,
        "range_breakout_long": breakout_long,
        "range_breakout_short": breakout_short,
        "funding_squeeze_long": squeeze_long,
        "funding_squeeze_short": squeeze_short,
        "extension_long": ext_long,
        "extension_short": ext_short,
    }


def research_gate(df: pd.DataFrame, c: Contract, direction: str) -> np.ndarray:
    """Optional extra filters layered on top of the shipped contract."""
    n = len(df)
    keep = np.ones(n, dtype=bool)
    if c.require_trend_15m_aligned:
        want = "up" if direction == "long" else "down"
        keep &= df["trend_15m"].to_numpy(object) == want
    ratio = df["atr_1h_ratio"].to_numpy(float)
    if c.min_atr_ratio is not None:
        keep &= ~np.isnan(ratio) & (ratio >= c.min_atr_ratio)
    if c.max_atr_ratio is not None:
        keep &= ~np.isnan(ratio) & (ratio <= c.max_atr_ratio)
    if c.min_relative_volume is not None:
        rel = df["relative_volume_1h"].to_numpy(float)
        keep &= ~np.isnan(rel) & (rel >= c.min_relative_volume)
    if c.max_extension_atr is not None:
        key = "extension_long" if direction == "long" else "extension_short"
        ext = evidence_masks(df, c)[key]
        keep &= ~np.isnan(ext) & (ext <= c.max_extension_atr)
    if c.allowed_regimes is not None:
        keep &= np.isin(df["regime"].to_numpy(object),
                        np.array(c.allowed_regimes, dtype=object))
    if c.allowed_hours_utc is not None:
        hours = ((df["timestamp_ms"].to_numpy(np.int64) // HOUR_MS) % 24)
        keep &= np.isin(hours, np.array(c.allowed_hours_utc))
    if c.require_funding_tailwind:
        funding = np.nan_to_num(df["funding_rate_pct"].to_numpy(float))
        keep &= (funding < 0) if direction == "long" else (funding > 0)
    return keep


def derive_levels(df: pd.DataFrame, c: Contract, direction: str,
                  exit_policy: str) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized agent.strategy.build_setup_plan stop/target derivation."""
    atr = df["atr_1h_pct"].to_numpy(float)
    minimum = atr * c.min_stop_atr_multiple
    structure = (df["swing_low_pct"] if direction == "long"
                 else df["swing_high_pct"]).to_numpy(float)
    stop = np.maximum(minimum, structure + atr * c.structure_buffer_atr_multiple)
    if exit_policy == "extended_rr":
        take = stop * c.extended_reward_risk
    elif exit_policy == "structure_target":
        opposite = (df["swing_high_pct"] if direction == "long"
                    else df["swing_low_pct"]).to_numpy(float)
        take = np.maximum(stop * c.fixed_reward_risk,
                          np.nan_to_num(opposite))
    else:
        take = stop * c.fixed_reward_risk
    return np.round(stop, 6), np.round(take, 6)


# Trade simulation

@dataclass(frozen=True)
class Costs:
    """All-in round-trip execution assumptions, in percent."""
    name: str
    fee_per_side: float = 0.05
    entry_slippage: float = 0.05
    exit_slippage: float = 0.05
    stop_slippage: float = 0.15
    include_funding: bool = True
    # "measured" uses only real settlements (absent before the ~97-day
    # window OKX serves); "median" charges the symbol's median rate outside
    # it so a two-year run is not silently funding-free.
    funding_model: str = "median"


COST_SCENARIOS = {
    "frictionless": Costs("frictionless", 0.0, 0.0, 0.0, 0.0, False),
    "optimistic": Costs("optimistic", 0.02, 0.01, 0.01, 0.05, True),
    "base": Costs("base", 0.05, 0.05, 0.05, 0.15, True),
    "realistic_alt": Costs("realistic_alt", 0.05, 0.10, 0.10, 0.25, True),
    "stress": Costs("stress", 0.05, 0.35, 0.10, 0.35, True),
    # Account-rate scenario. agent/market.py uses the exchange account's
    # per-side rate when available and the configured rate as its fallback.
    # Keep `base` unchanged because committed reports identify it by name.
    "account_taker": Costs("account_taker", 0.25, 0.05, 0.05, 0.15, True),
}

# Historical scenario names remain stable so committed reports are reproducible.
DEFAULT_COST_SCENARIO = "account_taker"


def simulate(frame: SymbolFrame, idx: np.ndarray, direction: str,
             stop_pct: np.ndarray, take_pct: np.ndarray,
             costs: Costs, max_hold_bars: int) -> pd.DataFrame:
    """Vectorized forward simulation of entries at bar `idx + 1` open.

    Ambiguity rule: when a bar's range spans both the stop and the target,
    the stop is assumed to fill first. Without tick data that is the only
    non-optimistic choice.
    """
    n = len(frame.df)
    entry_idx = idx + 1
    keep = entry_idx < n - 1
    entry_idx, stop_pct, take_pct = (
        entry_idx[keep], stop_pct[keep], take_pct[keep])
    signal_idx = idx[keep]
    if len(entry_idx) == 0:
        return pd.DataFrame()

    sign = 1.0 if direction == "long" else -1.0
    open_ = frame.df["open"].to_numpy(float)
    high = frame.df["high"].to_numpy(float)
    low = frame.df["low"].to_numpy(float)
    close = frame.df["close"].to_numpy(float)

    entry = open_[entry_idx] * (1 + costs.entry_slippage / 100 * sign)
    stop_level = entry * (1 - stop_pct / 100 * sign)
    take_level = entry * (1 + take_pct / 100 * sign)

    horizon = np.arange(max_hold_bars)
    grid = entry_idx[:, None] + horizon[None, :]
    valid = grid < n
    grid_clipped = np.clip(grid, 0, n - 1)
    win_high, win_low = high[grid_clipped], low[grid_clipped]
    win_open = open_[grid_clipped]

    if direction == "long":
        stop_hit = (win_low <= stop_level[:, None]) & valid
        take_hit = (win_high >= take_level[:, None]) & valid
    else:
        stop_hit = (win_high >= stop_level[:, None]) & valid
        take_hit = (win_low <= take_level[:, None]) & valid

    big = max_hold_bars + 10
    first_stop = np.where(stop_hit.any(1), stop_hit.argmax(1), big)
    first_take = np.where(take_hit.any(1), take_hit.argmax(1), big)
    # Stop-first on a tie (same bar) is the conservative assumption.
    stopped = first_stop <= first_take
    took = (first_take < first_stop) & (first_take < big)
    last_valid = valid.sum(1) - 1
    offset = np.where(stopped & (first_stop < big), first_stop,
                      np.where(took, first_take, last_valid))
    exit_idx = np.minimum(entry_idx + offset, n - 1)

    reason = np.where(stopped & (first_stop < big), "stop",
                      np.where(took, "take_profit", "max_hold"))
    gap_open = win_open[np.arange(len(offset)), offset]
    if direction == "long":
        stop_fill = np.minimum(stop_level, gap_open)
    else:
        stop_fill = np.maximum(stop_level, gap_open)
    raw_exit = np.where(reason == "stop", stop_fill,
                        np.where(reason == "take_profit", take_level,
                                 close[exit_idx]))
    slip = np.where(reason == "stop", costs.stop_slippage, costs.exit_slippage)
    exit_price = raw_exit * (1 - slip / 100 * sign)

    gross_pct = sign * (exit_price / entry - 1) * 100
    fee_pct = costs.fee_per_side * (1 + exit_price / entry)
    if costs.include_funding:
        paid = frame.cum_funding[exit_idx] - frame.cum_funding[entry_idx]
        if costs.funding_model == "median":
            # Outside the ~97-day measured window, charge the symbol's own
            # median rate per elapsed settlement interval rather than zero.
            covered = frame.funding_covered_from
            outside = (np.ones(len(entry_idx), dtype=bool) if covered is None
                       else frame.ts[entry_idx] < covered)
            hours = (exit_idx - entry_idx) * 0.25
            intervals = np.ceil(hours / max(frame.funding_interval_h, 0.5))
            paid = np.where(outside,
                            intervals * frame.funding_median_pct, paid)
        funding_pct = -sign * paid
    else:
        funding_pct = np.zeros(len(entry_idx))
    net_pct = gross_pct - fee_pct + funding_pct

    # R is measured against the planned all-in stop loss the risk engine
    # sizes from, so R is comparable across stop widths and cost scenarios.
    denom = stop_pct + 2 * costs.fee_per_side + costs.entry_slippage \
        + costs.stop_slippage
    denom = np.maximum(denom, 0.05)

    return pd.DataFrame({
        "symbol": frame.symbol,
        "direction": direction,
        "signal_idx": signal_idx,
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "signal_ts": frame.ts[signal_idx],
        "entry_ts": frame.ts[entry_idx],
        "exit_ts": frame.ts[exit_idx] + BAR_MS,
        "entry_price": entry,
        "stop_price": stop_level,
        "take_price": take_level,
        "exit_price": exit_price,
        "stop_pct": stop_pct,
        "take_pct": take_pct,
        "exit_reason": reason,
        "gross_pct": gross_pct,
        "fees_pct": fee_pct,
        "funding_pct": funding_pct,
        "net_pct": net_pct,
        "r_multiple": net_pct / denom,
        "bars_held": exit_idx - entry_idx + 1,
    })


# Statistics

def block_bootstrap_ci(values: np.ndarray, months: np.ndarray,
                       draws: int = 4000, seed: int = 11) -> list[float]:
    """Month-clustered bootstrap CI: crypto trades are strongly autocorrelated
    within a regime, so an i.i.d. trade bootstrap would understate the error."""
    if len(values) == 0:
        return [float("nan"), float("nan")]
    keys = np.unique(months)
    grouped = [values[months == key] for key in keys]
    if len(keys) < 2:
        return [float("nan"), float("nan")]
    rng = np.random.default_rng(seed)
    means = np.empty(draws)
    for i in range(draws):
        picks = rng.integers(0, len(grouped), len(grouped))
        means[i] = np.concatenate([grouped[p] for p in picks]).mean()
    return [float(np.percentile(means, 2.5)),
            float(np.percentile(means, 97.5))]


def summarize(trades: pd.DataFrame, label: str = "",
              bootstrap: bool = True) -> dict:
    if trades is None or trades.empty:
        return {"label": label, "trades": 0}
    net = trades["net_pct"].to_numpy(float)
    r = trades["r_multiple"].to_numpy(float)
    wins, losses = net[net > 0], net[net < 0]
    months = pd.to_datetime(
        trades["entry_ts"], unit="ms", utc=True).dt.strftime("%Y-%m").to_numpy()
    out = {
        "label": label,
        "trades": int(len(trades)),
        "win_rate_pct": float((net > 0).mean() * 100),
        "expectancy_r": float(r.mean()),
        "expectancy_pct": float(net.mean()),
        "median_pct": float(np.median(net)),
        "profit_factor": (float(wins.sum() / -losses.sum())
                          if len(losses) and losses.sum() != 0 else None),
        "total_r": float(r.sum()),
        "avg_hold_h": float(trades["bars_held"].mean() * 0.25),
        "tp_rate_pct": float((trades["exit_reason"] == "take_profit").mean() * 100),
        "stop_rate_pct": float((trades["exit_reason"] == "stop").mean() * 100),
        "timeout_rate_pct": float((trades["exit_reason"] == "max_hold").mean() * 100),
        "months": int(len(np.unique(months))),
    }
    if bootstrap:
        ci = block_bootstrap_ci(r, months)
        out["expectancy_r_ci95"] = ci
        out["significant"] = bool(
            ci[0] == ci[0] and (ci[0] > 0 or ci[1] < 0))
    # Standard error using month blocks, for a t-like read on the mean.
    per_month = pd.Series(r).groupby(months).mean()
    if len(per_month) > 2:
        out["monthly_mean_r"] = float(per_month.mean())
        out["monthly_t_stat"] = float(
            per_month.mean() / (per_month.std(ddof=1) / math.sqrt(len(per_month))))
        out["positive_months_pct"] = float((per_month > 0).mean() * 100)
    return out


# Loading

def load_dataset(root: Path, min_bars: int = 8000) -> dict[str, SymbolFrame]:
    """Load every symbol with enough history.

    The manifest is written only when a download finishes, so discovery falls
    back to the files actually on disk. That keeps analysis reproducible from
    a directory alone and lets a partial download be inspected.
    """
    swap_dir = root / "swap"
    symbols = sorted(
        path.stem.replace("_USDT_USDT", "/USDT:USDT")
        for path in swap_dir.glob("*.csv"))

    def read(folder: str, symbol: str) -> pd.DataFrame | None:
        stem = symbol.replace("/", "_").replace(":", "_")
        path = root / folder / f"{stem}.csv"
        if not path.exists():
            return None
        frame = pd.read_csv(path)
        return frame if not frame.empty else None

    btc_swap = read("swap", "BTC/USDT:USDT")
    btc_1h = aggregate_bars(btc_swap, HOUR_MS) if btc_swap is not None else None

    frames: dict[str, SymbolFrame] = {}
    for symbol in symbols:
        swap = read("swap", symbol)
        if swap is None or len(swap) < min_bars:
            continue
        frames[symbol] = SymbolFrame(
            symbol, swap, read("spot", symbol), read("funding", symbol),
            read("oi", symbol), btc_1h)
    return frames


# Point-in-time universe

def universe_membership(frames: dict[str, SymbolFrame], top_n: int = 10,
                        min_qv_usd: float = 50e6,
                        refresh_bars: int = 4) -> dict[str, np.ndarray]:
    """Rebuild the live top-N-by-24h-turnover universe at each refresh.

    The live agent re-ranks hourly (universe.refresh_minutes=60) and holds
    that list until the next refresh, so membership is a step function.
    A symbol also needs `min_history_candles` completed bars to qualify.
    """
    all_ts = sorted({int(t) for frame in frames.values() for t in frame.ts})
    ts_index = {t: i for i, t in enumerate(all_ts)}
    grid = np.array(all_ts, dtype=np.int64)
    qv = np.full((len(frames), len(grid)), np.nan)
    order = sorted(frames)
    for row, symbol in enumerate(order):
        frame = frames[symbol]
        positions = np.array([ts_index[int(t)] for t in frame.ts])
        values = frame.df["qv_24h_usd"].to_numpy(float)
        # A symbol needs MIN_HISTORY completed bars on the slowest timeframe.
        # 4h with 120-bar EWM warmup dominates; require the 4h trend to exist.
        warm = frame.df["trend_4h"].to_numpy(object) != None  # noqa: E711
        values = np.where(warm, values, np.nan)
        qv[row, positions] = values

    member = np.zeros_like(qv, dtype=bool)
    for column in range(0, len(grid), refresh_bars):
        snapshot = qv[:, column]
        eligible = np.where(~np.isnan(snapshot) & (snapshot >= min_qv_usd))[0]
        if len(eligible) == 0:
            continue
        ranked = eligible[np.argsort(-snapshot[eligible])][:top_n]
        upper = min(column + refresh_bars, len(grid))
        member[np.ix_(ranked, np.arange(column, upper))] = True

    out = {}
    for row, symbol in enumerate(order):
        frame = frames[symbol]
        positions = np.array([ts_index[int(t)] for t in frame.ts])
        out[symbol] = member[row, positions]
    return out


# Candidate builder

def _stable_seed(text: str) -> int:
    """Process-independent seed offset.

    Python randomizes str.__hash__ per process unless PYTHONHASHSEED is set,
    so seeding a null baseline with hash(symbol + setup) silently produced a
    DIFFERENT null on every run - and therefore a different verdict from the
    same command on the same data. crc32 is stable across processes and
    machines, which is what "reproduce with this command" requires.
    """
    return zlib.crc32(text.encode("utf-8")) % 10**6


SETUPS = ("trend_continuation", "range_breakout", "funding_squeeze")


def build_trades(frames: dict[str, SymbolFrame],
                 membership: dict[str, np.ndarray] | None,
                 contract, costs: Costs,
                 exit_policy: str = "fixed_rr",
                 setups: tuple[str, ...] | None = None,
                 flip_direction: bool = False,
                 random_timing: int | None = None,
                 random_direction: int | None = None) -> pd.DataFrame:
    """Produce one trade per qualifying (symbol, bar, setup, direction).

    ``contract`` is any object implementing the StrategyContract protocol
    (masks / gate / levels plus the four sizing scalars). Passing None for
    ``setups`` uses whatever the contract declares as its default.
    """
    if setups is None:
        setups = contract.default_setups
    chunks = []
    for symbol, frame in frames.items():
        df = frame.df
        masks = contract.masks(df)
        in_universe = (membership[symbol] if membership is not None
                       else np.ones(len(df), dtype=bool))
        # Every indicator must be warm. The 4h EMA window is the binding
        # constraint; before it fills, `trend_4h` is undefined and the
        # contract would silently evaluate against a missing timeframe.
        in_universe = in_universe & (
            df["trend_4h"].to_numpy(object) != None)  # noqa: E711
        for setup in setups:
            for direction in ("long", "short"):
                mask = masks[f"{setup}_{direction}"] & in_universe
                ext = masks[f"extension_{direction}"]
                mask &= ~np.isnan(ext) & (
                    ext <= contract.hard_max_entry_extension_atr)
                mask &= contract.gate(df, direction)
                stop, take = contract.levels(df, direction, exit_policy)
                mask &= ~np.isnan(stop) & (stop >= contract.min_stop_pct) \
                    & (stop <= contract.max_stop_pct)
                mask &= ~np.isnan(take) & (take <= 50.0)
                idx = np.where(mask)[0]
                if len(idx) == 0:
                    continue
                traded_direction = direction
                if flip_direction:
                    traded_direction = (
                        "short" if direction == "long" else "long")
                    stop, take = contract.levels(
                        df, traded_direction, exit_policy)
                if random_timing is not None:
                    rng = np.random.default_rng(
                        random_timing + _stable_seed(symbol + setup + direction))
                    high = len(df) - contract.max_hold_bars - 2
                    # Draw only from bars where the SAME level derivation is
                    # well defined, so the null differs from the signal in
                    # timing alone and not in data availability.
                    usable = (in_universe & ~np.isnan(stop)
                              & (stop >= contract.min_stop_pct)
                              & (stop <= contract.max_stop_pct)
                              & ~np.isnan(take) & (take <= 50.0))
                    pool = np.where(usable)[0]
                    pool = pool[pool < high]
                    if len(pool) == 0:
                        continue
                    idx = rng.choice(pool, size=min(len(idx), len(pool)),
                                     replace=len(pool) < len(idx))
                    idx.sort()
                if random_direction is not None:
                    rng = np.random.default_rng(
                        random_direction + _stable_seed(symbol + setup))
                    flip = rng.random(len(idx)) < 0.5
                else:
                    flip = None
                if flip is None:
                    chunk = simulate(
                        frame, idx, traded_direction, stop[idx], take[idx],
                        costs, contract.max_hold_bars)
                    if not chunk.empty:
                        chunk["setup_type"] = setup
                        chunk["signal_direction"] = direction
                        chunks.append(chunk)
                else:
                    for want, sub in (("long", idx[~flip]), ("short", idx[flip])):
                        if len(sub) == 0:
                            continue
                        s2, t2 = contract.levels(df, want, exit_policy)
                        chunk = simulate(frame, sub, want, s2[sub], t2[sub],
                                         costs, contract.max_hold_bars)
                        if not chunk.empty:
                            chunk["setup_type"] = setup
                            chunk["signal_direction"] = direction
                            chunks.append(chunk)
    if not chunks:
        return pd.DataFrame()
    trades = pd.concat(chunks, ignore_index=True)
    return trades.sort_values("entry_ts").reset_index(drop=True)


def non_overlapping(trades: pd.DataFrame,
                    cooldown_bars: int = 3) -> pd.DataFrame:
    """One live position per symbol, mirroring the agent's one-per-symbol rule.

    Overlapping event samples inflate apparent sample size massively: the same
    trend produces dozens of near-identical signals whose outcomes are almost
    the same trade counted many times.
    """
    if trades.empty:
        return trades
    keep = []
    busy: dict[str, int] = {}
    for row in trades.sort_values(
            ["entry_ts", "symbol"]).itertuples(index=True):
        if row.entry_idx < busy.get(row.symbol, -1):
            continue
        keep.append(row.Index)
        busy[row.symbol] = int(row.exit_idx) + cooldown_bars
    return trades.loc[keep].reset_index(drop=True)
