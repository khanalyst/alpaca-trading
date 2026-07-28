"""Download OKX historical data for offline strategy research.

Produces the layout consumed by the research backtests:

    manifest.json
    swap/<SYMBOL>.csv     timestamp_ms,open,high,low,close,volume,quote_volume
    spot/<SYMBOL>.csv     timestamp_ms,open,high,low,close,volume
    funding/<SYMBOL>.csv  timestamp_ms,funding_rate
    oi/<SYMBOL>.csv       timestamp_ms,oi_usd

`volume` is the base-asset volume (OKX ``volCcy`` for swaps, which already
applies the contract multiplier) so that relative-volume indicators match the
live agent. `quote_volume` is OKX ``volCcyQuote`` and lets a backtest rebuild
the historical 24h turnover ranking the live universe filter uses.

Only public endpoints are used; no API key is required. Downloads are
resumable: a symbol whose CSV already covers the requested window is skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

BASE = "https://www.okx.com"
BAR_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
# OKX's open-interest history reaches back roughly 60 days regardless of the
# window asked for. Used to decide whether a cached file is complete for the
# span that can actually be served.
OI_RETENTION_MS = 60 * 86_400_000

# OKX publishes per-endpoint IP rate limits. Stay comfortably inside them so a
# long download never trips a ban: history-candles is 20 requests / 2s.
CANDLE_SLEEP = 0.0
OTHER_SLEEP = 0.0

_RATE_LOCK = threading.Lock()
_RATE_STATE = {"next": 0.0}
_MIN_INTERVAL = 1.0 / 9.0   # 9 req/s, inside OKX's 20 req / 2s IP budget


def rate_limit() -> None:
    """Global token bucket shared by every worker thread.

    Per-worker sleeps cannot respect a per-IP limit: N workers multiply the
    request rate by N and trigger 429 backoff storms that are far slower
    than simply pacing every request through one budget.
    """
    with _RATE_LOCK:
        now = time.monotonic()
        wait = _RATE_STATE["next"] - now
        if wait > 0:
            time.sleep(wait)
            now = _RATE_STATE["next"]
        _RATE_STATE["next"] = max(now, _RATE_STATE["next"]) + _MIN_INTERVAL


class Downloader:
    def __init__(self, retries: int = 6):
        self.session = requests.Session()
        self.retries = retries

    def get(self, path: str, params: dict, sleep: float) -> list:
        last_error = "unknown"
        for attempt in range(self.retries):
            try:
                rate_limit()
                response = self.session.get(
                    f"{BASE}{path}", params=params, timeout=30)
                if response.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("code")) != "0":
                    last_error = f"code={payload.get('code')} {payload.get('msg')}"
                    time.sleep(0.5 * (attempt + 1))
                    continue
                time.sleep(sleep)
                return payload.get("data") or []
            except Exception as exc:  # network/JSON errors are retryable
                last_error = str(exc)[:200]
                time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"{path} failed after {self.retries} tries: {last_error}")

    # ------------------------------------------------------------- candles

    def candles(self, inst_id: str, bar: str, start_ms: int,
                end_ms: int) -> pd.DataFrame:
        """Page backwards through history-candles from end_ms to start_ms."""
        duration = BAR_MS[bar]
        rows: dict[int, list] = {}
        cursor = end_ms
        stalled = 0
        while cursor > start_ms:
            data = self.get(
                "/api/v5/market/history-candles",
                {"instId": inst_id, "bar": bar,
                 "after": str(cursor), "limit": "100"},
                CANDLE_SLEEP,
            )
            if not data:
                break
            oldest = cursor
            for row in data:
                ts = int(row[0])
                if ts < start_ms:
                    continue
                # confirm flag (index 8) == "1" means the bar is complete
                if len(row) > 8 and str(row[8]) == "0":
                    continue
                rows[ts] = row
                oldest = min(oldest, ts)
            if oldest >= cursor:
                stalled += 1
                if stalled >= 3:
                    break
                cursor -= duration * 100
            else:
                stalled = 0
                cursor = oldest
        if not rows:
            return pd.DataFrame(
                columns=["timestamp_ms", "open", "high", "low", "close",
                         "volume", "quote_volume"])
        frame = pd.DataFrame(
            [rows[ts] for ts in sorted(rows)],
        ).iloc[:, :8]
        ncols = frame.shape[1]
        names = ["timestamp_ms", "open", "high", "low", "close",
                 "vol_contracts", "volume", "quote_volume"][:ncols]
        frame.columns = names
        for column in names:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["timestamp_ms"] = frame["timestamp_ms"].astype("int64")
        if "quote_volume" not in frame:
            frame["quote_volume"] = frame["volume"] * frame["close"]
        keep = ["timestamp_ms", "open", "high", "low", "close",
                "volume", "quote_volume"]
        return frame[keep].dropna().sort_values("timestamp_ms").reset_index(
            drop=True)

    # ------------------------------------------------------------- funding

    def funding(self, inst_id: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        rows: dict[int, float] = {}
        cursor = end_ms
        stalled = 0
        while cursor > start_ms:
            data = self.get(
                "/api/v5/public/funding-rate-history",
                {"instId": inst_id, "after": str(cursor), "limit": "100"},
                OTHER_SLEEP,
            )
            if not data:
                break
            oldest = cursor
            for row in data:
                ts = int(row["fundingTime"])
                if ts < start_ms:
                    continue
                rows[ts] = float(row["fundingRate"])
                oldest = min(oldest, ts)
            if oldest >= cursor:
                stalled += 1
                if stalled >= 3:
                    break
                cursor -= 8 * 3_600_000 * 100
            else:
                stalled = 0
                cursor = oldest
        frame = pd.DataFrame(
            sorted(rows.items()), columns=["timestamp_ms", "funding_rate"])
        return frame

    # ------------------------------------------------------ open interest

    def open_interest(self, inst_id: str, start_ms: int,
                      end_ms: int) -> pd.DataFrame:
        """Hourly OI history. Retention is shorter than candle history."""
        rows: dict[int, float] = {}
        cursor = end_ms
        stalled = 0
        while cursor > start_ms:
            data = self.get(
                "/api/v5/rubik/stat/contracts/open-interest-history",
                {"instId": inst_id, "period": "1H",
                 "end": str(cursor), "limit": "100"},
                OTHER_SLEEP,
            )
            if not data:
                break
            oldest = cursor
            for row in data:
                ts = int(row[0])
                if ts < start_ms:
                    continue
                rows[ts] = float(row[3])  # OI notional in USD
                oldest = min(oldest, ts)
            if oldest >= cursor:
                stalled += 1
                if stalled >= 3:
                    break
                cursor -= 3_600_000 * 100
            else:
                stalled = 0
                cursor = oldest
        return pd.DataFrame(
            sorted(rows.items()), columns=["timestamp_ms", "oi_usd"])


def ccxt_symbol(inst_id: str) -> str:
    base = inst_id.replace("-USDT-SWAP", "")
    return f"{base}/USDT:USDT"


def covered(path: Path, start_ms: int, end_ms: int, tolerance: int) -> bool:
    if not path.exists():
        return False
    try:
        frame = pd.read_csv(path)
    except Exception:
        return False
    if frame.empty:
        return False
    return (int(frame["timestamp_ms"].min()) <= start_ms + tolerance
            and int(frame["timestamp_ms"].max()) >= end_ms - tolerance)


def download_symbol(inst_id: str, out: Path, start_ms: int, end_ms: int,
                    want_spot: bool, want_oi: bool) -> dict:
    api = Downloader()
    symbol = ccxt_symbol(inst_id)
    stem = symbol.replace("/", "_").replace(":", "_")
    result = {"symbol": symbol, "inst_id": inst_id}

    swap_path = out / "swap" / f"{stem}.csv"
    if covered(swap_path, start_ms, end_ms, 4 * BAR_MS["15m"]):
        swap = pd.read_csv(swap_path)
    else:
        swap = api.candles(inst_id, "15m", start_ms, end_ms)
        swap_path.parent.mkdir(parents=True, exist_ok=True)
        swap.to_csv(swap_path, index=False)
    result["swap_bars"] = len(swap)
    if not swap.empty:
        result["swap_first"] = int(swap["timestamp_ms"].min())
        result["swap_last"] = int(swap["timestamp_ms"].max())

    funding_path = out / "funding" / f"{stem}.csv"
    if covered(funding_path, start_ms, end_ms, 24 * 3_600_000):
        funding = pd.read_csv(funding_path)
    else:
        funding = api.funding(inst_id, start_ms, end_ms)
        funding_path.parent.mkdir(parents=True, exist_ok=True)
        funding.to_csv(funding_path, index=False)
    result["funding_rows"] = len(funding)

    if want_spot:
        spot_path = out / "spot" / f"{stem}.csv"
        if covered(spot_path, start_ms, end_ms, 4 * BAR_MS["15m"]):
            spot = pd.read_csv(spot_path)
        else:
            # Only the perp/index basis needs spot, and not every swap has a
            # listed spot pair (OKX answers 51001). Losing basis for one
            # instrument is a missing column; aborting is a missing dataset.
            try:
                spot = api.candles(
                    inst_id.replace("-SWAP", ""), "15m", start_ms, end_ms)
                spot_path.parent.mkdir(parents=True, exist_ok=True)
                spot.to_csv(spot_path, index=False)
            except Exception as exc:                       # noqa: BLE001
                print(f"[warn] {inst_id}: no spot history ({str(exc)[:80]}); "
                      f"perp/index basis unavailable for it", flush=True)
                spot = pd.DataFrame()
        result["spot_bars"] = len(spot)

    if want_oi:
        oi_path = out / "oi" / f"{stem}.csv"
        # Coverage check, not a bare existence check. OKX serves only about
        # 60 days of open interest whatever window is requested, so the file
        # can never span a long request - but it must still cover the part
        # OKX can actually serve. Reusing any existing file regardless of its
        # span means a short earlier download (a smoke test, an interrupted
        # run) is silently reused forever, and open interest is precisely the
        # series a deleveraging hypothesis cannot be tested without.
        oi_start = max(start_ms, end_ms - OI_RETENTION_MS)
        if covered(oi_path, oi_start, end_ms, 6 * 3_600_000):
            oi = pd.read_csv(oi_path)
        else:
            oi = api.open_interest(inst_id, start_ms, end_ms)
            oi_path.parent.mkdir(parents=True, exist_ok=True)
            oi.to_csv(oi_path, index=False)
        result["oi_rows"] = len(oi)
        if not oi.empty:
            result["oi_first"] = int(oi["timestamp_ms"].min())

    print(f"[done] {inst_id:22s} {json.dumps(result)}", flush=True)
    return result


def discover_symbols(min_volume_usd: float, limit: int,
                     extra: list[str]) -> list[str]:
    api = Downloader()
    instruments = {
        row["instId"]: row
        for row in api.get("/api/v5/public/instruments",
                           {"instType": "SWAP"}, OTHER_SLEEP)
    }
    tickers = api.get("/api/v5/market/tickers",
                      {"instType": "SWAP"}, OTHER_SLEEP)
    rows = []
    for ticker in tickers:
        inst_id = ticker["instId"]
        market = instruments.get(inst_id)
        if not market:
            continue
        # Mirror the live universe filter: linear, USDT-settled, live, and
        # OKX instCategory 1 (crypto) - not tokenized equities/commodities.
        if (market.get("settleCcy") != "USDT"
                or market.get("ctType") != "linear"
                or market.get("state") != "live"
                or str(market.get("instCategory")) != "1"):
            continue
        try:
            volume = float(ticker["volCcy24h"]) * float(ticker["last"])
        except (TypeError, ValueError):
            continue
        if volume < min_volume_usd:
            continue
        rows.append((volume, inst_id))
    rows.sort(reverse=True)
    selected = [inst_id for _, inst_id in rows[:limit]]
    for inst_id in extra:
        if inst_id not in selected:
            selected.append(inst_id)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--min-volume-usd", type=float, default=30e6)
    parser.add_argument("--max-symbols", type=int, default=26)
    parser.add_argument("--extra", default="")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--no-spot", action="store_true")
    parser.add_argument("--no-oi", action="store_true")
    args = parser.parse_args()

    end_ms = int(time.time() * 1000) // BAR_MS["15m"] * BAR_MS["15m"]
    start_ms = end_ms - args.days * 86_400_000

    extra = [s for s in args.extra.split(",") if s.strip()]
    if args.symbols.strip():
        inst_ids = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        inst_ids = discover_symbols(
            args.min_volume_usd, args.max_symbols, extra)
    print(f"symbols ({len(inst_ids)}): {inst_ids}", flush=True)
    print(
        f"window: {datetime.fromtimestamp(start_ms/1000, timezone.utc)} -> "
        f"{datetime.fromtimestamp(end_ms/1000, timezone.utc)}", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)
    def safe_download(inst_id: str) -> dict:
        """Never let one instrument abort the download.

        pool.map() propagates the first exception and discards every other
        result, so a single delisted pair or transient 51001 used to cost the
        entire dataset - and the caller would then score whatever stale data
        was already on disk, silently, believing it had refreshed.
        """
        try:
            return download_symbol(
                inst_id, args.out, start_ms, end_ms,
                not args.no_spot, not args.no_oi)
        except Exception as exc:                           # noqa: BLE001
            print(f"[FAILED] {inst_id}: {str(exc)[:160]}", flush=True)
            return {"symbol": inst_id, "inst_id": inst_id,
                    "error": str(exc)[:300]}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(safe_download, inst_ids))

    failed = [r for r in results if r.get("error")]
    if failed:
        print(f"\n{len(failed)} of {len(results)} instruments failed:",
              flush=True)
        for row in failed:
            print(f"  {row['inst_id']}: {row['error'][:120]}", flush=True)

    usable = [r for r in results if r.get("swap_bars", 0) > 5000]
    manifest = {
        "source": "OKX public v5 REST (market/history-candles, "
                  "public/funding-rate-history, "
                  "rubik/stat/contracts/open-interest-history)",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "requested_start_ms": start_ms,
        "requested_end_ms": end_ms,
        "bar": "15m",
        "symbols": sorted(r["symbol"] for r in usable),
        "all_results": results,
        "note": (
            "Symbol discovery used the CURRENT live instrument list, so "
            "instruments delisted before the download date are absent. "
            "This is a survivorship caveat that must be stated in any "
            "result derived from this dataset."
        ),
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(
        {"symbols_usable": len(usable), "out": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
