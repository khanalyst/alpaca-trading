"""Universe selection and market snapshot construction.

New entries are restricted to account-enabled, active, linear USDT perpetual
swaps whose OKX private instrument metadata classifies the base asset as
crypto.  Candidates are ranked by 24h quote volume, then history-qualified
before they occupy one of the configured universe slots.
"""

import logging
import math
import time

import numpy as np
import pandas as pd

from .exchange import OKX_CRYPTO_INSTRUMENT_CATEGORY

log = logging.getLogger("market")


def quote_volume_usd(ticker: dict, market: dict) -> float:
    """Return a derivative ticker's 24h quote turnover in USD.

    CCXT's OKX adapter deliberately leaves ``quoteVolume`` empty for swaps
    and exposes OKX ``vol24h`` as ``baseVolume``.  ``vol24h`` is a contract
    count, not base-asset volume, so multiplying it by price without applying
    ``contractSize`` can overstate liquidity by orders of magnitude.

    Prefer OKX's raw ``volCcy24h`` (base-asset volume for linear USDT swaps),
    then fall back to contracts * contract size * price.  A future adapter
    that supplies a real quoteVolume remains the first choice.
    """
    last = float(ticker.get("last") or ticker.get("close") or 0)
    direct = ticker.get("quoteVolume")
    if direct not in (None, ""):
        value = float(direct or 0)
        return value if math.isfinite(value) and value >= 0 else 0.0

    info = ticker.get("info") or {}
    base_ccy_volume = info.get("volCcy24h")
    if base_ccy_volume not in (None, "") and last > 0:
        value = float(base_ccy_volume) * last
        return value if math.isfinite(value) and value >= 0 else 0.0

    contracts = float(ticker.get("baseVolume") or 0)
    contract_size = float(market.get("contractSize") or 0)
    value = contracts * contract_size * last
    return value if math.isfinite(value) and value >= 0 else 0.0


def _funding_interval_hours(rate: dict) -> float | None:
    current = rate.get("fundingTimestamp")
    next_funding = rate.get("nextFundingTimestamp")
    if current not in (None, "") and next_funding not in (None, ""):
        hours = (float(next_funding) - float(current)) / 3_600_000
        if hours > 0:
            return round(hours, 2)
    interval = str(rate.get("interval") or "").strip().lower()
    if interval.endswith("h"):
        try:
            hours = float(interval[:-1])
            return round(hours, 2) if hours > 0 else None
        except ValueError:
            pass
    return None


# ------------------------------------------------------------- indicators

def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _json_safe(value):
    """Replace non-finite numeric market data before it reaches the LLM.

    Python's json encoder otherwise emits NaN/Infinity tokens, which are not
    valid JSON and can make provider behavior dependent on malformed exchange
    data. Preserve the shape of the snapshot and mark unavailable values null.
    """
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # down == 0 makes rs undefined: a window with no down moves is maximal
    # strength (RSI 100), and a window with no movement at all is neutral -
    # NaN here would otherwise leak into the model's snapshot JSON.
    out = out.mask(down == 0, 100.0)
    return out.mask((down == 0) & (up == 0), 50.0)


def atr_pct(df: pd.DataFrame, n: int = 14) -> float:
    return float(atr_pct_series(df, n).iloc[-1])


def atr_pct_series(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    return atr / df["close"] * 100


def _closed_ohlcv(ex, symbol: str, timeframe: str, limit: int) -> list:
    """Fetch completed candles only; indicators should not use a live bar."""
    raw = ex.retry(ex.x.fetch_ohlcv, symbol, timeframe, None, limit + 1) or []
    if raw:
        try:
            duration_ms = int(ex.x.parse_timeframe(timeframe) * 1000)
        except Exception:
            duration_ms = {"15m": 900_000, "1h": 3_600_000,
                           "4h": 14_400_000}.get(timeframe, 0)
        if duration_ms and int(raw[-1][0]) + duration_ms > int(time.time() * 1000):
            raw = raw[:-1]
    return raw[-limit:]


def classify_regime(snap: dict) -> str:
    """Describe current conditions without deciding whether to trade them."""
    atr_ratio = float(snap.get("atr_1h_ratio") or 0)
    if atr_ratio >= 1.75:
        return "high_volatility"
    one, four = snap.get("trend_1h"), snap.get("trend_4h")
    if one == four == "up":
        return "trend_up"
    if one == four == "down":
        return "trend_down"
    atr = max(float(snap.get("atr_1h_pct") or 0), 0.01)
    if one == "flat" and abs(float(snap.get("mom_1h_pct") or 0)) < atr * 0.25:
        return "choppy"
    return "transition"


# ------------------------------------------------------------- universe

def _history_eligibility(ex, symbol: str, cfg: dict) -> tuple[bool, str, dict]:
    """Verify the same completed-candle minimum required by snapshots."""
    minimum = int(cfg["universe"]["min_history_candles"])
    counts = {}
    for timeframe in cfg["cycle"]["timeframes"]:
        try:
            rows = _closed_ohlcv(ex, symbol, timeframe, minimum)
        except Exception as exc:
            return (
                False,
                f"history_unavailable_{timeframe}: "
                f"{str(exc).replace(chr(10), ' ')[:160]}",
                counts,
            )
        counts[timeframe] = len(rows)
        if len(rows) < minimum:
            return False, f"insufficient_{timeframe}_history", counts
    return True, "selected", counts


def select_universe(ex, cfg: dict) -> tuple[list[str], dict]:
    """Build a crypto-only universe and a durable selection audit payload."""
    u = cfg["universe"]
    account = ex.account_swap_instruments(refresh=True)
    tickers = ex.retry(ex.x.fetch_tickers)
    candidates = []
    for symbol, ticker in tickers.items():
        market = ex.x.markets.get(symbol)
        if (not market or not market.get("swap")
                or market.get("settle") != "USDT"):
            continue
        quote_volume = quote_volume_usd(ticker, market)
        if quote_volume < float(u["min_24h_quote_volume_usd"]):
            continue
        candidates.append((symbol, quote_volume, market))
    candidates.sort(key=lambda row: row[1], reverse=True)

    selected: list[str] = []
    records = []
    denylist = set(u.get("denylist") or [])
    for rank, (symbol, quote_volume, market) in enumerate(candidates, start=1):
        record = {
            "rank": rank,
            "symbol": symbol,
            "quote_volume_usd": round(float(quote_volume), 2),
            "selected": False,
        }
        instrument = account.get(symbol)
        if not market.get("active", True):
            reason = "public_market_inactive"
        elif market.get("linear") is not True:
            reason = "not_linear"
        elif symbol in denylist:
            reason = "denylisted"
        elif instrument is None:
            reason = "not_available_to_account"
        elif str(instrument.get("instType") or "") != "SWAP":
            reason = "account_instrument_not_swap"
        elif str(instrument.get("settleCcy") or "") != "USDT":
            reason = "account_instrument_not_usdt_settled"
        elif str(instrument.get("state") or "") != "live":
            reason = (
                "account_instrument_"
                + str(instrument.get("state") or "state_unknown")
            )
        elif str(instrument.get("instCategory") or "") \
                != OKX_CRYPTO_INSTRUMENT_CATEGORY:
            reason = (
                "non_crypto_category_"
                + str(instrument.get("instCategory") or "unknown")
            )
        elif len(selected) >= int(u["top_n"]):
            reason = "ranked_below_top_n"
        else:
            eligible, reason, counts = _history_eligibility(
                ex, symbol, cfg)
            record["history_candles"] = counts
            if eligible:
                selected.append(symbol)
                record["selected"] = True
        record["reason"] = reason
        records.append(record)

    audit = {
        "selected": selected,
        "top_n": int(u["top_n"]),
        "min_24h_quote_volume_usd": float(
            u["min_24h_quote_volume_usd"]),
        "min_history_candles": int(u["min_history_candles"]),
        "candidates": records,
    }
    if not selected:
        log.warning(
            "Universe is empty after crypto/account/history eligibility checks")
    return selected, audit


def build_universe(ex, cfg: dict) -> list[str]:
    """Compatibility wrapper returning only selected symbols."""
    return select_universe(ex, cfg)[0]


# ------------------------------------------------------------- snapshot

def symbol_snapshot(ex, symbol: str, cfg: dict,
                    benchmark_returns: pd.Series | None = None) -> dict:
    tfs = cfg["cycle"]["timeframes"]
    n = cfg["cycle"]["candles"]
    minimum = int(cfg["universe"]["min_history_candles"])
    ticker = ex.retry(ex.x.fetch_ticker, symbol)

    frames: dict[str, pd.DataFrame] = {}
    for tf in tfs:
        raw = _closed_ohlcv(ex, symbol, tf, n)
        if not raw or len(raw) < minimum:
            raise ValueError(f"insufficient {tf} history for {symbol}")
        frames[tf] = pd.DataFrame(
            raw, columns=["ts", "open", "high", "low", "close", "vol"]
        )

    last = float(ticker.get("last") or frames[tfs[0]]["close"].iloc[-1])
    market = ex.x.market(symbol)
    snap = {
        "price": last,
        "chg_24h_pct": round(float(ticker.get("percentage") or 0), 2),
        "vol_24h_musd": round(quote_volume_usd(ticker, market) / 1e6, 1),
    }
    bid = float(ticker.get("bid") or 0)
    ask = float(ticker.get("ask") or 0)
    if ask >= bid > 0:
        mid = (ask + bid) / 2
        snap["spread_pct"] = round((ask - bid) / mid * 100, 4)
    else:
        snap["spread_pct"] = None
    try:
        fr = ex.retry(ex.x.fetch_funding_rate, symbol)
        snap["funding_rate_pct"] = round(
            float(fr.get("fundingRate") or 0) * 100, 4
        )
        snap["funding_interval_hours"] = _funding_interval_hours(fr)
        next_funding = fr.get("nextFundingTimestamp")
        snap["next_funding_minutes"] = (
            round(max(0.0, (float(next_funding) - time.time() * 1000)
                      / 60_000), 1)
            if next_funding not in (None, "") else None
        )
    except Exception:
        snap["funding_rate_pct"] = None
        snap["funding_interval_hours"] = None
        snap["next_funding_minutes"] = None

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
    rsi_1h = float(rsi(df_1h["close"]).iloc[-1])
    snap["rsi_1h"] = round(rsi_1h, 1) if math.isfinite(rsi_1h) else None
    atr_history = atr_pct_series(df_1h)
    current_atr = float(atr_history.iloc[-1])
    baseline_atr = float(atr_history.iloc[-51:-1].median())
    snap["atr_1h_pct"] = round(current_atr, 2)
    snap["atr_1h_ratio"] = round(current_atr / baseline_atr, 2) \
        if baseline_atr > 0 else None

    df_fast = frames.get("15m", frames[tfs[0]])
    snap["mom_1h_pct"] = round(
        float(df_fast["close"].pct_change(4).iloc[-1] * 100), 2
    )
    recent_volume = float(df_fast["vol"].tail(4).sum())
    prior_windows = df_fast["vol"].iloc[:-4].rolling(4).sum().tail(20)
    normal_volume = float(prior_windows.median())
    snap["relative_volume_1h"] = round(recent_volume / normal_volume, 2) \
        if normal_volume > 0 else None

    if benchmark_returns is not None:
        symbol_returns = df_1h.set_index("ts")["close"].pct_change().dropna()
        aligned = pd.concat(
            [symbol_returns.rename("symbol"),
             benchmark_returns.rename("benchmark")],
            axis=1, join="inner",
        ).dropna().tail(30)
        if len(aligned) >= 10:
            corr = aligned["symbol"].corr(aligned["benchmark"])
            snap["corr_btc_1h_30"] = round(float(corr), 2) \
                if pd.notna(corr) else None
        else:
            snap["corr_btc_1h_30"] = None

    # Structure anchors: recent swing extremes (last 20 fast-frame candles,
    # ~5h on 15m) and distance from the 1h EMA20, so the model can place
    # stops beyond real levels instead of blind ATR multiples.
    lows = df_fast["low"].tail(20)
    highs = df_fast["high"].tail(20)
    snap["swing_low_pct"] = round((last - float(lows.min())) / last * 100, 2)
    snap["swing_high_pct"] = round((float(highs.max()) - last) / last * 100, 2)
    e20_1h = float(ema(df_1h["close"], 20).iloc[-1])
    snap["ema20_1h_dist_pct"] = round((last - e20_1h) / e20_1h * 100, 2)

    hi = float(ticker.get("high") or 0)
    lo = float(ticker.get("low") or 0)
    if hi > lo > 0:
        snap["range_pos_pct"] = round((last - lo) / (hi - lo) * 100, 0)
    # Sanitize every field, not just indicators with known edge cases. Ticker,
    # funding and OHLCV adapters can all surface non-finite numeric values.
    snap = _json_safe(snap)
    snap["regime"] = classify_regime(snap)
    return snap


def market_snapshot(ex, symbols: list[str], cfg: dict) -> dict:
    benchmark = "BTC/USDT:USDT"
    benchmark_returns = None
    context = {"benchmark": benchmark, "regime": None}
    try:
        raw = _closed_ohlcv(ex, benchmark, "1h", cfg["cycle"]["candles"])
        frame = pd.DataFrame(
            raw, columns=["ts", "open", "high", "low", "close", "vol"]
        )
        benchmark_returns = frame.set_index("ts")["close"].pct_change().dropna()
    except Exception as e:
        log.warning("benchmark context failed: %s", e)

    benchmark_snapshot = None
    try:
        # Broad-market context must still exist when BTC itself did not make
        # the configured top-volume universe. Keep it out of the tradable
        # symbol map unless it was actually selected.
        benchmark_snapshot = symbol_snapshot(
            ex, benchmark, cfg, benchmark_returns)
        context.update({
            "regime": benchmark_snapshot.get("regime"),
            "atr_1h_ratio": benchmark_snapshot.get("atr_1h_ratio"),
            "relative_volume_1h": benchmark_snapshot.get("relative_volume_1h"),
            "mom_1h_pct": benchmark_snapshot.get("mom_1h_pct"),
        })
    except Exception as e:
        log.warning("benchmark snapshot failed: %s", e)

    out = {"_market_context": context}
    for sym in symbols:
        try:
            if sym == benchmark and benchmark_snapshot is not None:
                out[sym] = benchmark_snapshot
            else:
                out[sym] = symbol_snapshot(ex, sym, cfg, benchmark_returns)
        except Exception as e:
            log.warning("snapshot failed for %s: %s", sym, e)
    return out if len(out) > 1 else {}
