"""Systematic search for a day-trading edge in OKX USDT perpetuals.

The phase1-v2 audit answered "does the shipped entry work?" (no). This asks a
different and larger question: **is there any signal in this data, at
day-trading horizons, strong enough to pay 0.10% round-trip taker cost?**

Method
------
1. Build a point-in-time panel of every instrument on every bar it would
   actually have been tradable (top-10 by trailing 24h turnover).
2. Compute a broad library of candidate predictors - reversal, momentum,
   volatility, participation, cross-sectional and residual-vs-BTC families -
   each using only completed bars.
3. Measure forward returns from the *next bar's open* to the open N bars
   later, so every number is executable.
4. Score every (feature, horizon) pair on the in-sample period only.
5. Take survivors to out-of-sample, and control the false discovery rate
   across the whole search, because testing hundreds of pairs guarantees
   good-looking noise.

Horizons are capped at 48 hours: this is a day-trading system, not a
swing-trading one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BAR_MS = 900_000
# 15m .. 48h. Anything longer stops being day trading.
HORIZONS = (1, 2, 4, 8, 16, 32, 64, 96, 192)
ROUND_TRIP_TAKER_PCT = 0.10      # Historical 5 bps-per-side scenario.
ROUND_TRIP_MAKER_PCT = 0.04      # Historical 2 bps-per-side scenario.


# Panel

def _zscore(frame: pd.DataFrame, column: str, window: int) -> pd.Series:
    grouped = frame.groupby("symbol", observed=True)[column]
    mean = grouped.transform(lambda s: s.rolling(window, min_periods=window // 2).mean())
    std = grouped.transform(lambda s: s.rolling(window, min_periods=window // 2).std())
    return (frame[column] - mean) / std.replace(0, np.nan)


def build_panel(frames: dict, membership: dict) -> pd.DataFrame:
    """One row per (bar, instrument) for bars where the instrument was live."""
    rows = []
    btc = frames.get("BTC/USDT:USDT")
    btc_ret = None
    if btc is not None:
        btc_close = pd.Series(btc.df["close"].to_numpy(float),
                              index=btc.ts)
        btc_ret = {h: btc_close.pct_change(h) for h in (4, 16, 96)}

    for symbol, frame in frames.items():
        df = frame.df
        mask = membership[symbol] & (
            df["trend_4h"].to_numpy(object) != None)  # noqa: E711
        if not mask.any():
            continue
        close = df["close"].to_numpy(float)
        open_ = df["open"].to_numpy(float)
        high = df["high"].to_numpy(float)
        low = df["low"].to_numpy(float)
        n = len(df)

        part = pd.DataFrame({
            "ts": frame.ts,
            "symbol": symbol,
            "open": open_,
            "close": close,
            "atr_pct": df["atr_1h_pct"].to_numpy(float),
            "atr_ratio": df["atr_1h_ratio"].to_numpy(float),
            "rel_vol": df["relative_volume_1h"].to_numpy(float),
            "range_pos": df["range_pos_pct"].to_numpy(float),
            "ema_dist": df["ema20_1h_dist_pct"].to_numpy(float),
            "funding": df["funding_rate_pct"].to_numpy(float),
            "funding_iv": df["funding_interval_hours"].to_numpy(float),
            "oi_musd": df["open_interest_musd"].to_numpy(float),
            "qv24": df["qv_24h_usd"].to_numpy(float),
            "beta": df["beta_btc_1h_72"].to_numpy(float),
            "trend_1h": df["trend_1h"].to_numpy(object),
            "trend_4h": df["trend_4h"].to_numpy(object),
            "regime": df["regime"].to_numpy(object),
            "in_universe": mask,
        })

        # Trailing-window returns, normalized by ATR where applicable.
        series = pd.Series(close)
        for bars, name in ((1, "1b"), (4, "4b"), (16, "16b"),
                           (96, "96b"), (192, "192b")):
            part[f"ret_{name}"] = series.pct_change(bars).to_numpy() * 100

        # Bar shape.
        span = np.where(high - low > 0, high - low, np.nan)
        part["close_loc"] = (close - low) / span
        part["bar_range_atr"] = (high - low) / close * 100 / part["atr_pct"]
        part["gap"] = (open_ - np.roll(close, 1)) / np.roll(close, 1) * 100
        part.loc[0, "gap"] = np.nan

        # Signed consecutive bars (+3 means three up bars).
        direction = np.sign(close - open_)
        changed = np.empty(n, dtype=bool)
        changed[0] = True
        changed[1:] = direction[1:] != direction[:-1]
        position = np.arange(n)
        run_start = np.maximum.accumulate(np.where(changed, position, 0))
        part["consec_bars"] = (position - run_start + 1) * direction

        # Illiquidity: price impact per unit of turnover.
        bar_quote = df["qv_24h_usd"].to_numpy(float) / 96
        part["amihud"] = np.abs(part["ret_1b"]) / np.where(
            bar_quote > 0, bar_quote / 1e6, np.nan)

        # Residual return versus BTC.
        if btc_ret is not None and symbol != "BTC/USDT:USDT":
            beta = np.clip(np.nan_to_num(part["beta"].to_numpy(float), nan=1.0),
                           -3, 3)
            for h, name in ((4, "4b"), (16, "16b"), (96, "96b")):
                bench = btc_ret[h].reindex(frame.ts).to_numpy() * 100
                part[f"resid_{name}"] = part[f"ret_{name}"] - beta * bench
        else:
            for name in ("4b", "16b", "96b"):
                part[f"resid_{name}"] = 0.0

        # Forward returns run from the next bar's open to a later open.
        for h in HORIZONS:
            entry = np.roll(open_, -1)
            exit_ = np.roll(open_, -(1 + h))
            fwd = (exit_ / entry - 1) * 100
            fwd[-(1 + h):] = np.nan
            part[f"fwd_{h}"] = fwd

        rows.append(part[mask])

    panel = pd.concat(rows, ignore_index=True)
    panel = panel.sort_values(["ts", "symbol"]).reset_index(drop=True)

    # Volatility-normalized reversal features.
    for name in ("1b", "4b", "16b", "96b"):
        panel[f"rev_{name}"] = -panel[f"ret_{name}"] / panel["atr_pct"]
        panel[f"residrev_{name}"] = (
            -panel[f"resid_{name}"] / panel["atr_pct"]
            if f"resid_{name}" in panel else np.nan)
    panel["rev_ema"] = -panel["ema_dist"] / panel["atr_pct"]
    panel["funding_8h"] = panel["funding"] * (
        8.0 / panel["funding_iv"].where(panel["funding_iv"] > 0, 8.0))
    panel["vol_z"] = _zscore(panel, "rel_vol", 96)
    panel["oi_chg_4b"] = panel.groupby("symbol", observed=True)["oi_musd"] \
        .transform(lambda s: s.pct_change(4)) * 100

    # Forced deleveraging versus new positioning (the OI-price quadrant).
    #
    # A price move with open interest FALLING is existing positions being
    # closed, often by a liquidation engine that sells at market regardless
    # of price - price-insensitive, finite, and prone to overshoot. A move
    # with open interest RISING is new money taking a side, which carries no
    # such reason to revert. The two are identical on a price chart, which is
    # why this pair of features is the whole flush-fade hypothesis.
    panel["oi_chg_16b"] = panel.groupby("symbol", observed=True)["oi_musd"] \
        .transform(lambda s: s.pct_change(16)) * 100
    move_atr = panel["ret_16b"] / panel["atr_pct"]
    flush = (panel["oi_chg_16b"] < -1.0) & (panel["vol_z"] > 1.0)
    build = (panel["oi_chg_16b"] > 1.0) & (panel["vol_z"] > 1.0)
    # NaN rather than 0.0 outside each condition. evaluate_pair drops NaN
    # rows, so each feature is scored only on the bars it claims to describe;
    # a zero fill would create a point mass that collapses the quantile
    # buckets and quietly makes the feature untestable.
    panel["flush_fade"] = np.where(flush, -move_atr, np.nan)
    panel["build_follow"] = np.where(build, move_atr, np.nan)

    panel["hour"] = ((panel["ts"] // 3_600_000) % 24).astype(int)
    panel["dow"] = (((panel["ts"] // 86_400_000) + 4) % 7).astype(int)
    panel["month"] = pd.to_datetime(
        panel["ts"], unit="ms", utc=True).dt.strftime("%Y-%m")
    return panel


# Cross-sectional ranks per timestamp across live instruments.

def add_cross_sectional(panel: pd.DataFrame,
                        columns: tuple[str, ...]) -> pd.DataFrame:
    for column in columns:
        ranked = panel.groupby("ts", observed=True)[column].rank(pct=True)
        count = panel.groupby("ts", observed=True)[column].transform("count")
        # A rank is only meaningful with enough instruments to rank against.
        panel[f"xs_{column}"] = np.where(count >= 5, ranked - 0.5, np.nan)
    return panel


# Evaluation

FEATURES = [
    # reversal / overextension
    "rev_1b", "rev_4b", "rev_16b", "rev_96b", "rev_ema",
    "residrev_4b", "residrev_16b", "residrev_96b",
    # momentum (sign-flipped reversal, kept explicit for readability)
    "ret_4b", "ret_16b", "ret_96b", "ret_192b",
    # bar shape / participation
    "close_loc", "bar_range_atr", "gap", "consec_bars",
    "rel_vol", "vol_z", "amihud", "atr_ratio", "range_pos",
    # carry / positioning
    "funding_8h", "oi_chg_4b", "oi_chg_16b",
    # forced deleveraging (flush-fade hypothesis)
    "flush_fade", "build_follow",
    # cross-sectional
    "xs_rev_4b", "xs_rev_16b", "xs_rev_96b",
    "xs_residrev_4b", "xs_residrev_16b", "xs_residrev_96b",
    "xs_ret_16b", "xs_vol_z", "xs_funding_8h",
]


def month_t_stat(values: np.ndarray, months: np.ndarray) -> float:
    if len(values) < 30:
        return np.nan
    per_month = pd.Series(values).groupby(months).mean()
    if len(per_month) < 3 or per_month.std(ddof=1) == 0:
        return np.nan
    return float(per_month.mean()
                 / (per_month.std(ddof=1) / np.sqrt(len(per_month))))


def evaluate_pair(panel: pd.DataFrame, feature: str, horizon: int,
                  quantiles: int = 5) -> dict | None:
    """Quantile long/short spread for one feature at one horizon.

    Top quantile is bought and bottom quantile sold, so a positive spread
    means the feature is a usable directional signal in the stated direction.
    """
    column = panel[feature]
    target = panel[f"fwd_{horizon}"]
    ok = column.notna() & target.notna()
    if ok.sum() < 2000:
        return None
    sub = panel.loc[ok, [feature, f"fwd_{horizon}", "month"]]
    try:
        bucket = pd.qcut(sub[feature], quantiles, labels=False,
                         duplicates="drop")
    except ValueError:
        return None
    if bucket.nunique() < quantiles:
        return None
    sub = sub.assign(bucket=bucket)
    top = sub[sub["bucket"] == quantiles - 1]
    bottom = sub[sub["bucket"] == 0]
    # Long the top quantile, short the bottom: the per-trade return of a
    # dollar-neutral pair is the average of the two legs.
    spread = np.concatenate([
        top[f"fwd_{horizon}"].to_numpy(float),
        -bottom[f"fwd_{horizon}"].to_numpy(float)])
    months = np.concatenate([
        top["month"].to_numpy(), bottom["month"].to_numpy()])
    gross = float(spread.mean())
    # Spearman without scipy: Pearson on the ranks.
    ic = float(sub[feature].rank().corr(sub[f"fwd_{horizon}"].rank()))
    return {
        "feature": feature,
        "horizon_bars": horizon,
        "horizon_hours": horizon * 0.25,
        "observations": int(len(sub)),
        "trades": int(len(spread)),
        "rank_ic": ic,
        "gross_pct_per_trade": gross,
        "net_taker_pct": gross - ROUND_TRIP_TAKER_PCT,
        "net_maker_pct": gross - ROUND_TRIP_MAKER_PCT,
        "t_stat": month_t_stat(spread, months),
        "hit_rate_pct": float((spread > 0).mean() * 100),
    }


def benjamini_hochberg(p_values: np.ndarray, alpha: float = 0.10) -> np.ndarray:
    """Return a boolean mask of discoveries controlling FDR at *alpha*."""
    order = np.argsort(p_values)
    ranked = p_values[order]
    m = len(p_values)
    thresholds = alpha * (np.arange(1, m + 1) / m)
    passed = ranked <= thresholds
    keep = np.zeros(m, dtype=bool)
    if passed.any():
        cutoff = np.max(np.where(passed)[0])
        keep[order[:cutoff + 1]] = True
    return keep


def two_sided_p(t_stat: float, dof: int = 20) -> float:
    if not np.isfinite(t_stat):
        return 1.0
    from math import erf, sqrt
    # Normal approximation is adequate at ~24 monthly blocks and keeps this
    # dependency-free; it is slightly anti-conservative, which the
    # out-of-sample requirement compensates for.
    return float(2 * (1 - 0.5 * (1 + erf(abs(t_stat) / sqrt(2)))))
