"""Opening-range breakout on stocks in play, against the same rule on ETFs.

The question: is the intraday program's failure a property of intraday trading,
or of what it trades?  Every one of its 82 arms trades 24 broad ETFs, the most
arbitraged instruments in the market.  Day traders who make money trade
*stocks in play*: names with news and unusual volume that morning.

The rule follows the summaries of Zarattini, Barbon and Aziz (2024), "A
Profitable Day Trading Strategy for the U.S. Equity Market":

* eligible: open above $5, 14-day average volume of at least 1,000,000 shares,
  14-day ATR above $0.50;
* in play: first five-minute volume divided by its 14-day average for that
  same five minutes, at least 1.0, the top ``TOP_N`` by that ratio;
* direction: the first five-minute candle (up candle: long; down: short;
  a doji is skipped);
* entry: a stop order at that candle's high (long) or low (short), from 09:35;
* stop: 10% of the 14-day ATR from the entry; otherwise flat at the close.

Three universes run through the identical code: stocks in play, ``TOP_N``
eligible stocks drawn at random each day (same filters, no in-play ranking),
and the repository's 24 ETFs.

Bars are five-minute.  When one bar spans both the entry trigger and the stop,
the stop is assumed to be hit (the conservative order).  A gap through a level
fills at the bar's open.  Costs are charged per side in bps, plus one tick of
slippage on the stop-order entry and the stop exit, plus the SEC fee on sells.

Diagnostic only.  Yahoo serves five-minute bars for the last 60 calendar days,
so the test has about 45 sessions, and the universe comes from today's listing
(stocks that were delisted inside the window are missing).  A decision needs
years of SIP minute bars, which Alpaca serves from 2016.

    python -m studies.stocks_in_play.study --cache runtime/stocks-in-play --out studies/stocks_in_play/results
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import math
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import numpy as np

NY = ZoneInfo("America/New_York")
SCREENER = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=60d"
ETFS = ("SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLI", "XLP", "XLY",
        "XLU", "XLB", "XLRE", "VTI", "VO", "VB", "EFA", "EEM", "TLT", "HYG", "GLD",
        "SLV", "SMH")
BARS_PER_SESSION = 78
LOOKBACK = 14
TOP_N = 20
MIN_PRICE = 5.0
MIN_ADV = 1_000_000.0
MIN_ATR = 0.50
STOP_ATR = 0.10
TICK = 0.01
SEC_FEE = 20.60 / 1e6
COST_BPS_PER_SIDE = (0.0, 2.5, 5.0)
RISK_PER_TRADE = 0.01
MAX_LEVERAGE = 4.0
SEED = 20261109


# --------------------------------------------------------------------------
# data


def _get(url: str, attempts: int = 4) -> Any:
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except Exception as error:  # retried, then raised
            last = error
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def universe(prefilter_volume: float = 500_000.0) -> list[str]:
    """Today's US common stocks above $5 trading at least ``prefilter_volume`` shares."""
    rows = _get(SCREENER)["data"]["rows"]

    def num(value: Any) -> float:
        try:
            return float(str(value).replace("$", "").replace(",", ""))
        except ValueError:
            return 0.0
    out = [r["symbol"].strip() for r in rows
           if num(r.get("lastsale")) > MIN_PRICE and num(r.get("volume")) >= prefilter_volume
           and r["symbol"].strip().isalpha()]
    return sorted(set(out) | set(ETFS))


def fetch(symbol: str, cache: Path) -> dict[str, Any] | None:
    path = cache / f"{symbol}.json.gz"
    if path.exists():
        return json.loads(gzip.decompress(path.read_bytes()))
    try:
        result = _get(CHART.format(symbol=urllib.parse.quote(symbol)))["chart"]["result"][0]
    except Exception:
        return None
    quote = result["indicators"]["quote"][0]
    bars: dict[str, list[list[float]]] = {}
    for i, stamp in enumerate(result.get("timestamp") or []):
        values = [quote[k][i] for k in ("open", "high", "low", "close", "volume")]
        if any(v is None for v in values):
            continue
        when = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).astimezone(NY)
        slot = (when.hour * 60 + when.minute - 570) // 5
        if 0 <= slot < BARS_PER_SESSION:
            bars.setdefault(when.date().isoformat(), []).append([slot, *map(float, values)])
    data = {"symbol": symbol, "bars": bars}
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(data).encode()))
    return data


def load(symbols: Sequence[str], cache: Path, workers: int = 8) -> dict[str, dict[str, Any]]:
    out = {}
    with cf.ThreadPoolExecutor(workers) as pool:
        for symbol, data in zip(symbols, pool.map(lambda s: fetch(s, cache), symbols)):
            if data and data["bars"]:
                out[symbol] = data
    return out


# --------------------------------------------------------------------------
# panel


def panel(raw: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Full sessions only (all 78 bars), as arrays (sessions, symbols, 78)."""
    days = sorted({d for v in raw.values() for d in v["bars"]})
    symbols = sorted(raw)
    shape = (len(days), len(symbols), BARS_PER_SESSION)
    o, h, l, c, v = (np.full(shape, np.nan, dtype=np.float32) for _ in range(5))
    pos = {d: i for i, d in enumerate(days)}
    for j, s in enumerate(symbols):
        for day, rows in raw[s]["bars"].items():
            i = pos[day]
            for slot, *values in rows:
                o[i, j, int(slot)], h[i, j, int(slot)], l[i, j, int(slot)], \
                    c[i, j, int(slot)], v[i, j, int(slot)] = values
    full = np.isfinite(c).all(axis=2)
    # A session that is short for every symbol (a half day) is dropped.
    keep = full.mean(axis=1) > 0.5
    return {"days": [d for d, k in zip(days, keep) if k], "symbols": symbols,
            "o": o[keep], "h": h[keep], "l": l[keep], "c": c[keep], "v": v[keep],
            "full": full[keep]}


def daily_features(p: dict[str, Any]) -> dict[str, np.ndarray]:
    """Per session and symbol, using only earlier sessions: ATR, ADV, first-bar volume."""
    high, low = np.nanmax(p["h"], axis=2), np.nanmin(p["l"], axis=2)
    close = p["c"][:, :, -1]
    volume = np.nansum(p["v"], axis=2)
    first_volume = p["v"][:, :, 0]
    prev_close = np.vstack([np.full((1, close.shape[1]), np.nan), close[:-1]])
    tr = np.nanmax(np.stack([high - low, np.abs(high - prev_close), np.abs(low - prev_close)]),
                   axis=0)
    tr[~p["full"]] = np.nan
    n = len(p["days"])
    atr, adv, base_first = (np.full(close.shape, np.nan) for _ in range(3))
    for t in range(LOOKBACK, n):
        window = slice(t - LOOKBACK, t)
        ok = p["full"][window].all(axis=0)
        atr[t] = np.where(ok, np.nanmean(tr[window], axis=0), np.nan)
        adv[t] = np.where(ok, np.nanmean(volume[window], axis=0), np.nan)
        base_first[t] = np.where(ok, np.nanmean(first_volume[window], axis=0), np.nan)
    return {"atr": atr, "adv": adv, "relvol": first_volume / base_first}


# --------------------------------------------------------------------------
# simulation


def trade(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, atr: float, *,
          stop_mode: str, order: str) -> dict[str, float] | None:
    """One session's opening-range breakout on five-minute bars; None if no entry.

    ``stop_mode``: ``atr10`` puts the stop 10% of the daily ATR from the entry
    (the paper's rule); ``range`` puts it at the other side of the first
    candle (the classic breakout stop).  ``order`` decides a bar that both
    triggers the entry and touches the stop: ``stop_first`` stops it out
    (pessimistic), ``entry_first`` only checks the stop from the next bar
    (optimistic).  The truth on one-minute data lies between the two.
    """
    first_open, first_close = o[0], c[0]
    if first_close > first_open:
        side, level, far = 1, h[0], l[0]
    elif first_close < first_open:
        side, level, far = -1, l[0], h[0]
    else:
        return None
    entry = stop = risk = None
    for k in range(1, BARS_PER_SESSION):
        if entry is None:
            triggered = h[k] >= level if side > 0 else l[k] <= level
            if not triggered:
                continue
            entry = (max(o[k], level) + TICK) if side > 0 else (min(o[k], level) - TICK)
            stop = entry - side * STOP_ATR * atr if stop_mode == "atr10" else far - side * TICK
            risk = abs(entry - stop)
            if risk <= 0:
                return None
            if order == "stop_first" and ((side > 0 and l[k] <= stop) or
                                          (side < 0 and h[k] >= stop)):
                return {"side": side, "entry": entry, "exit": stop - side * TICK,
                        "risk": risk, "stopped": 1.0}
            continue
        if (side > 0 and l[k] <= stop) or (side < 0 and h[k] >= stop):
            fill = (min(o[k], stop) if side > 0 else max(o[k], stop)) - side * TICK
            return {"side": side, "entry": entry, "exit": fill, "risk": risk, "stopped": 1.0}
    if entry is None:
        return None
    return {"side": side, "entry": entry, "exit": float(c[-1]), "risk": risk, "stopped": 0.0}


def run(p: dict[str, Any], f: dict[str, np.ndarray], mode: str, cost_bps: float, *,
        stop_mode: str = "atr10", order: str = "stop_first") -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    etf = {j for j, s in enumerate(p["symbols"]) if s in ETFS}
    rows = []
    for t in range(LOOKBACK, len(p["days"])):
        first_open = p["o"][t, :, 0]
        eligible = [j for j in range(len(p["symbols"]))
                    if p["full"][t, j] and math.isfinite(f["atr"][t, j])
                    and first_open[j] > MIN_PRICE and f["adv"][t, j] >= MIN_ADV
                    and f["atr"][t, j] > MIN_ATR]
        if mode == "in_play":
            ranked = [j for j in eligible if j not in etf and math.isfinite(f["relvol"][t, j])
                      and f["relvol"][t, j] >= 1.0]
            chosen = sorted(ranked, key=lambda j: -f["relvol"][t, j])[:TOP_N]
        elif mode == "random":
            pool = [j for j in eligible if j not in etf]
            chosen = rng.sample(pool, min(TOP_N, len(pool)))
        else:  # etf: every ETF with a full session, without the eligibility filters
            chosen = [j for j in etf if p["full"][t, j] and math.isfinite(f["atr"][t, j])]
        for j in chosen:
            result = trade(p["o"][t, j], p["h"][t, j], p["l"][t, j], p["c"][t, j],
                           float(f["atr"][t, j]), stop_mode=stop_mode, order=order)
            if result is None:
                continue
            entry = float(result["entry"])
            gross = result["side"] * (float(result["exit"]) - entry)
            sell_price = float(result["exit"]) if result["side"] > 0 else entry
            cost = (cost_bps / 1e4) * (entry + float(result["exit"])) + SEC_FEE * sell_price
            net = gross - cost
            # risk 1% of equity per trade, but never more than MAX_LEVERAGE / TOP_N
            # of equity in one position (the paper's 4x cap across 20 names)
            weight = min(RISK_PER_TRADE / (result["risk"] / entry), MAX_LEVERAGE / TOP_N)
            rows.append({"day": p["days"][t], "symbol": p["symbols"][j], "side": result["side"],
                         "r": net / result["risk"], "net_bps": 1e4 * net / entry,
                         "gross_bps": 1e4 * gross / entry, "weight": weight,
                         "pnl": weight * net / entry, "stopped": result["stopped"]})
    return rows


def summarise(rows: Sequence[dict[str, Any]], days: Sequence[str]) -> dict[str, Any]:
    if not rows:
        return {"trades": 0}
    r = np.array([x["r"] for x in rows])
    by_day: dict[str, list[float]] = {}
    for x in rows:
        by_day.setdefault(x["day"], []).append(x["pnl"])
    daily = np.array([sum(by_day.get(d, [])) for d in days])
    sd = daily.std(ddof=1)
    t = daily.mean() / (sd / math.sqrt(len(daily))) if sd > 0 else float("nan")
    wealth = np.cumprod(1 + daily)
    drawdown = float(np.min(wealth / np.maximum.accumulate(wealth) - 1))
    return {"trades": len(rows), "days_traded": len(by_day), "win_rate": float(np.mean(r > 0)),
            "mean_r": float(r.mean()), "median_r": float(np.median(r)),
            "mean_net_bps": float(np.mean([x["net_bps"] for x in rows])),
            "mean_gross_bps": float(np.mean([x["gross_bps"] for x in rows])),
            "stopped_share": float(np.mean([x["stopped"] for x in rows])),
            "mean_leverage": float(np.mean([sum(x["weight"] for x in rows if x["day"] == d)
                                            for d in by_day])),
            "daily_t": float(t), "daily_mean_pct": 100 * float(daily.mean()),
            "sharpe_annualised": float(daily.mean() / sd * math.sqrt(252)) if sd > 0 else None,
            "total_return_pct": 100 * float(wealth[-1] - 1), "max_drawdown_pct": 100 * drawdown}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, default=Path("runtime/stocks-in-play"))
    parser.add_argument("--out", type=Path, default=Path("studies/stocks_in_play/results"))
    parser.add_argument("--max-symbols", type=int, default=0, help="0 = all")
    args = parser.parse_args(argv)
    symbols = universe()
    if args.max_symbols:
        symbols = sorted(set(symbols[: args.max_symbols]) | set(ETFS))
    print(f"universe: {len(symbols)} symbols", flush=True)
    raw = load(symbols, args.cache)
    p = panel(raw)
    f = daily_features(p)
    test_days = p["days"][LOOKBACK:]
    print(f"loaded {len(p['symbols'])} symbols, {len(p['days'])} sessions, "
          f"{len(test_days)} test sessions {test_days[0]} .. {test_days[-1]}", flush=True)
    results: dict[str, Any] = {"universe_size": len(p["symbols"]), "sessions": len(p["days"]),
                               "test_sessions": len(test_days), "first": test_days[0],
                               "last": test_days[-1], "rules": {
                                   "top_n": TOP_N, "min_price": MIN_PRICE, "min_adv": MIN_ADV,
                                   "min_atr": MIN_ATR, "stop_atr": STOP_ATR,
                                   "lookback": LOOKBACK, "bar_minutes": 5}, "runs": {}}
    for stop_mode in ("atr10", "range"):
        for order in ("stop_first", "entry_first"):
            for cost in COST_BPS_PER_SIDE:
                for mode in ("in_play", "random", "etf"):
                    rows = run(p, f, mode, cost, stop_mode=stop_mode, order=order)
                    results["runs"][f"{mode}|{stop_mode}|{order}|{cost}"] = summarise(
                        rows, test_days)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(results, indent=1) + "\n")
    (args.out / "report.md").write_text(render(results))
    print(render(results))
    return 0


def render(res: dict[str, Any]) -> str:
    lines = ["# Opening-range breakout: stocks in play against ETFs", "",
             f"{res['test_sessions']} test sessions, {res['first']} to {res['last']}, "
             f"{res['universe_size']} symbols loaded, five-minute bars. Diagnostic only: about "
             f"two months of data, today's listing, no news filter. Rules in `study.py`.", "",
             "Each trade risks 1% of equity, capped at 4x leverage across the day's 20 names. "
             "`random` draws 20 eligible stocks a day without the in-play ranking; `etf` runs "
             "the 24 ETFs the intraday program trades. Two fill orders bound what one-minute "
             "bars would show: `stop_first` (a bar that triggers the entry and touches the "
             "stop stops out) and `entry_first` (the stop is checked from the next bar).", ""]
    for stop_mode, title in (("atr10", "Stop at 10% of daily ATR (the paper's rule)"),
                             ("range", "Stop at the other side of the first candle")):
        lines += [f"## {title}", "",
                  "| universe | fill order | cost a side | trades | win rate | mean R | "
                  "mean net bps | stopped | daily t | daily mean | Sharpe | total | "
                  "max drawdown |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for key, s in res["runs"].items():
            mode, sm, order, cost = key.split("|")
            if sm != stop_mode:
                continue
            if not s.get("trades"):
                lines.append(f"| {mode} | {order} | {cost} bps | 0 |" + " |" * 9)
                continue
            lines.append(
                f"| {mode} | {order} | {cost} bps | {s['trades']} | {100 * s['win_rate']:.0f}% | "
                f"{s['mean_r']:+.3f} | {s['mean_net_bps']:+.1f} | {100 * s['stopped_share']:.0f}% | "
                f"{s['daily_t']:+.2f} | {s['daily_mean_pct']:+.2f}% | "
                f"{(s['sharpe_annualised'] or 0):.2f} | {s['total_return_pct']:+.1f}% | "
                f"{s['max_drawdown_pct']:.1f}% |")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
