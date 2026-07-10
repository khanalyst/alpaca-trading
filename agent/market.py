"""Universe selection and market snapshot construction.

Universe rule: only USDT-settled perpetual swaps ranked by 24h quote volume,
above a hard dollar floor. Newly listed coins are excluded twice over: they
rarely clear the volume floor, and the snapshot requires enough 4h candle
history to compute indicators.
"""

import logging

import numpy as np
import pandas as pd

log = logging.getLogger("market")


# ------------------------------------------------------------- indicators

def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr_pct(df: pd.DataFrame, n: int = 14) -> float:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    return float(atr.iloc[-1] / df["close"].iloc[-1] * 100)


# ------------------------------------------------------------- universe

def build_universe(ex, cfg: dict) -> list[str]:
    u = cfg["universe"]
    tickers = ex.retry(ex.x.fetch_tickers)
    rows = []
    for sym, t in tickers.items():
        m = ex.x.markets.get(sym)
        if not m or not m.get("swap") or m.get("settle") != "USDT":
            continue
        if not m.get("active", True):
            continue
        if sym in (u.get("denylist") or []):
            continue
        qv = t.get("quoteVolume")
        if qv is None:
            last = float(t.get("last") or 0)
            qv = last * float(t.get("baseVolume") or 0)
        qv = float(qv or 0)
        if qv >= u["min_24h_quote_volume_usd"]:
            rows.append((sym, qv))
    rows.sort(key=lambda r: r[1], reverse=True)
    universe = [s for s, _ in rows[: u["top_n"]]]
    if not universe:
        log.warning("Universe is empty; check min_24h_quote_volume_usd")
    return universe


# ------------------------------------------------------------- snapshot

def symbol_snapshot(ex, symbol: str, cfg: dict) -> dict:
    tfs = cfg["cycle"]["timeframes"]
    n = cfg["cycle"]["candles"]
    ticker = ex.retry(ex.x.fetch_ticker, symbol)

    frames: dict[str, pd.DataFrame] = {}
    for tf in tfs:
        raw = ex.retry(ex.x.fetch_ohlcv, symbol, tf, None, n)
        if not raw or len(raw) < 60:
            raise ValueError(f"insufficient {tf} history for {symbol}")
        frames[tf] = pd.DataFrame(
            raw, columns=["ts", "open", "high", "low", "close", "vol"]
        )

    last = float(ticker.get("last") or frames[tfs[0]]["close"].iloc[-1])
    snap = {
        "price": last,
        "chg_24h_pct": round(float(ticker.get("percentage") or 0), 2),
        "vol_24h_musd": round(float(ticker.get("quoteVolume") or 0) / 1e6, 1),
    }
    try:
        fr = ex.retry(ex.x.fetch_funding_rate, symbol)
        snap["funding_rate_pct"] = round(
            float(fr.get("fundingRate") or 0) * 100, 4
        )
    except Exception:
        snap["funding_rate_pct"] = None

    for tf, df in frames.items():
        close = df["close"]
        e20 = float(ema(close, 20).iloc[-1])
        e50 = float(ema(close, 50).iloc[-1])
        px = float(close.iloc[-1])
        if px > e20 > e50:
            snap[f"trend_{tf}"] = "up"
        elif px < e20 < e50:
            snap[f"trend_{tf}"] = "down"
        else:
            snap[f"trend_{tf}"] = "flat"

    df_1h = frames.get("1h", frames[tfs[-1]])
    snap["rsi_1h"] = round(float(rsi(df_1h["close"]).iloc[-1]), 1)
    snap["atr_1h_pct"] = round(atr_pct(df_1h), 2)

    df_fast = frames.get("15m", frames[tfs[0]])
    snap["mom_1h_pct"] = round(
        float(df_fast["close"].pct_change(4).iloc[-1] * 100), 2
    )

    hi = float(ticker.get("high") or 0)
    lo = float(ticker.get("low") or 0)
    if hi > lo > 0:
        snap["range_pos_pct"] = round((last - lo) / (hi - lo) * 100, 0)
    return snap


def market_snapshot(ex, symbols: list[str], cfg: dict) -> dict:
    out = {}
    for sym in symbols:
        try:
            out[sym] = symbol_snapshot(ex, sym, cfg)
        except Exception as e:
            log.warning("snapshot failed for %s: %s", sym, e)
    return out
