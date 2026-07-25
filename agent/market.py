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

from . import strategy
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


def _funding_history_context(ex, symbol: str,
                             current_rate_pct: float | None) -> dict:
    """Summarize recent funding without sending raw history to the model."""
    fetcher = getattr(ex.x, "fetch_funding_rate_history", None)
    if not callable(fetcher) or current_rate_pct is None:
        return {
            "funding_samples_30": 0,
            "funding_mean_30_pct": None,
            "funding_percentile_30": None,
            "funding_change_pct": None,
        }
    try:
        rows = ex.retry(fetcher, symbol, None, 30) or []
    except Exception:
        rows = []
    rates = []
    for row in rows[-30:]:
        value = row.get("fundingRate")
        if value in (None, ""):
            value = (row.get("info") or {}).get("fundingRate")
        try:
            number = float(value) * 100
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            rates.append(number)
    if not rates:
        return {
            "funding_samples_30": 0,
            "funding_mean_30_pct": None,
            "funding_percentile_30": None,
            "funding_change_pct": None,
        }
    percentile = (
        sum(rate <= current_rate_pct for rate in rates) / len(rates) * 100)
    return {
        "funding_samples_30": len(rates),
        "funding_mean_30_pct": round(float(np.mean(rates)), 4),
        "funding_percentile_30": round(percentile, 1),
        "funding_change_pct": round(current_rate_pct - rates[-1], 4),
    }


def _open_interest_usd(ex, symbol: str, last: float) -> float | None:
    fetcher = getattr(ex.x, "fetch_open_interest", None)
    if not callable(fetcher):
        return None
    try:
        row = ex.retry(fetcher, symbol) or {}
    except Exception:
        return None
    info = row.get("info") or {}
    for key in ("openInterestValue", "openInterestUsd", "oiUsd"):
        value = row.get(key)
        if value in (None, ""):
            value = info.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number >= 0:
            return number
    try:
        amount = float(row.get("openInterestAmount")
                       or info.get("oi") or 0)
        contract_size = float(
            ex.x.market(symbol).get("contractSize") or 1)
        value = amount * contract_size * last
    except (TypeError, ValueError, KeyError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


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
        funding_raw = fr.get("fundingRate")
        if funding_raw in (None, ""):
            raise ValueError("funding rate unavailable")
        current_funding = float(funding_raw) * 100
        if not math.isfinite(current_funding):
            raise ValueError("funding rate is not finite")
        snap["funding_rate_pct"] = round(current_funding, 4)
        snap["funding_interval_hours"] = _funding_interval_hours(fr)
        next_funding = fr.get("nextFundingTimestamp")
        snap["next_funding_minutes"] = (
            round(max(0.0, (float(next_funding) - time.time() * 1000)
                      / 60_000), 1)
            if next_funding not in (None, "") else None
        )
        mark = fr.get("markPrice") or (fr.get("info") or {}).get("markPx")
        index = fr.get("indexPrice") or (fr.get("info") or {}).get("idxPx")
        mark = float(mark) if mark not in (None, "") else None
        index = float(index) if index not in (None, "") else None
        snap["perp_index_basis_pct"] = (
            round((mark - index) / index * 100, 4)
            if mark is not None and index is not None and index > 0 else None
        )
        snap.update(_funding_history_context(
            ex, symbol, snap["funding_rate_pct"]))
    except Exception:
        snap["funding_rate_pct"] = None
        snap["funding_interval_hours"] = None
        snap["next_funding_minutes"] = None
        snap["perp_index_basis_pct"] = None
        snap.update(_funding_history_context(ex, symbol, None))

    fee_reader = getattr(ex, "taker_fee_pct", None)
    try:
        fee_pct = (
            float(fee_reader(symbol)) if callable(fee_reader)
            else float(cfg["trading_costs"]["taker_fee_pct_per_side"])
        )
        if not math.isfinite(fee_pct) or fee_pct < 0 or fee_pct > 1:
            raise ValueError("invalid fee rate")
        snap["taker_fee_pct_per_side"] = round(fee_pct, 6)
        snap["fee_rate_source"] = (
            "okx_account" if callable(fee_reader) else "configured_fallback")
    except Exception:
        snap["taker_fee_pct_per_side"] = float(
            cfg["trading_costs"]["taker_fee_pct_per_side"])
        snap["fee_rate_source"] = "configured_fallback"
    open_interest = _open_interest_usd(ex, symbol, last)
    snap["open_interest_musd"] = (
        round(open_interest / 1e6, 2) if open_interest is not None else None)

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
    snap["signal_ts"] = int(df_fast["ts"].iloc[-1])
    snap["mom_1h_pct"] = round(
        float(df_fast["close"].pct_change(4).iloc[-1] * 100), 2
    )
    recent_volume = float(df_fast["vol"].tail(4).sum())
    prior_windows = df_fast["vol"].iloc[:-4].rolling(4).sum().tail(20)
    normal_volume = float(prior_windows.median())
    snap["relative_volume_1h"] = round(recent_volume / normal_volume, 2) \
        if normal_volume > 0 else None

    snap.update({
        "corr_btc_1h_30": None,
        "corr_btc_1h_72_shrunk": None,
        "beta_btc_1h_72": None,
        "corr_btc_downside_1h_72": None,
        "corr_btc_samples": 0,
    })
    if benchmark_returns is not None:
        symbol_returns = df_1h.set_index("ts")["close"].pct_change().dropna()
        aligned = pd.concat(
            [symbol_returns.rename("symbol"),
             benchmark_returns.rename("benchmark")],
            axis=1, join="inner",
        ).dropna()
        short = aligned.tail(30)
        if len(short) >= 10:
            corr = short["symbol"].corr(short["benchmark"])
            snap["corr_btc_1h_30"] = round(float(corr), 2) \
                if pd.notna(corr) else None
        robust = aligned.tail(72)
        if len(robust) >= 24:
            raw_corr = robust["symbol"].corr(robust["benchmark"])
            variance = float(robust["benchmark"].var())
            covariance = float(
                robust["symbol"].cov(robust["benchmark"]))
            downside = robust[robust["benchmark"] < 0]
            downside_corr = (
                downside["symbol"].corr(downside["benchmark"])
                if len(downside) >= 10 else None
            )
            shrink = len(robust) / (len(robust) + 20)
            snap["corr_btc_1h_72_shrunk"] = (
                round(float(raw_corr) * shrink, 2)
                if pd.notna(raw_corr) else None)
            snap["beta_btc_1h_72"] = (
                round(covariance / variance, 2) if variance > 0 else None)
            snap["corr_btc_downside_1h_72"] = (
                round(float(downside_corr), 2)
                if downside_corr is not None and pd.notna(downside_corr)
                else None)
            snap["corr_btc_samples"] = len(robust)
        else:
            snap["corr_btc_samples"] = len(robust)

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
    strategy.enrich_snapshot(snap, cfg)
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
