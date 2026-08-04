"""Fetch OKX flow and positioning data - information OHLCV cannot contain.

Two years of candles told us the entry signal has no directional content. That
is not surprising: price history is the most-mined dataset in the market. The
interesting question is whether data that is *not* in the candles carries
anything, and OKX publishes three such series free:

- `taker-volume`      signed aggressor flow: who crossed the spread, and for
                      how much. This is order flow, the single most-studied
                      short-horizon predictor in microstructure.
- `long-short-account-ratio`  how retail accounts are positioned. A classic
                      contrarian input.
- `open-interest`     whether a move is being driven by new positions or by
                      existing ones closing.

**Retention is roughly 30 days on all three.** That is far too short to
establish an edge - it is barely long enough to see whether the sign is even
plausible. The point of fetching it is (a) a cheap preliminary read and
(b) to make the case for recording it forward, which `record_flow.py` does.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

BASE = "https://www.okx.com"
MIN_INTERVAL = 1.0 / 8.0
_last = {"t": 0.0}


def get(path: str, params: dict, retries: int = 5) -> list:
    for attempt in range(retries):
        wait = _last["t"] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last["t"] = time.monotonic()
        try:
            response = requests.get(f"{BASE}{path}", params=params, timeout=30)
            if response.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("code")) != "0":
                time.sleep(0.5 * (attempt + 1))
                continue
            return payload.get("data") or []
        except Exception:
            time.sleep(0.8 * (attempt + 1))
    return []


def page_back(path: str, params: dict, start_ms: int,
              columns: list[str]) -> pd.DataFrame:
    """Walk backwards through a rubik series until it stops returning data."""
    rows: dict[int, list] = {}
    cursor = None
    stalled = 0
    while True:
        query = dict(params)
        if cursor is not None:
            query["end"] = str(cursor)
        data = get(path, query)
        if not data:
            break
        oldest = cursor or (1 << 62)
        for row in data:
            ts = int(row[0])
            if ts < start_ms:
                continue
            rows[ts] = row
            oldest = min(oldest, ts)
        if cursor is not None and oldest >= cursor:
            stalled += 1
            if stalled >= 2:
                break
        else:
            stalled = 0
        if oldest <= start_ms:
            break
        cursor = oldest
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame([rows[ts] for ts in sorted(rows)])
    frame = frame.iloc[:, :len(columns)]
    frame.columns = columns
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["timestamp_ms"] = frame["timestamp_ms"].astype("int64")
    return frame.dropna().sort_values("timestamp_ms").reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--period", default="1H")
    parser.add_argument(
        "--currencies",
        default="BTC,ETH,SOL,XRP,DOGE,SHIB,PEPE,LTC,LINK,ADA,AVAX,UNI,AAVE,SUI")
    args = parser.parse_args()

    start_ms = int((time.time() - args.days * 86400) * 1000)
    args.out.mkdir(parents=True, exist_ok=True)
    for folder in ("taker_volume", "long_short_ratio"):
        (args.out / folder).mkdir(parents=True, exist_ok=True)

    summary = []
    for currency in [c.strip() for c in args.currencies.split(",") if c.strip()]:
        # OKX documents this series as ts, sellVol, buyVol. The ordering is
        # checked against same-period returns before interpreting the sign.
        taker = page_back(
            "/api/v5/rubik/stat/taker-volume",
            {"ccy": currency, "instType": "CONTRACTS",
             "period": args.period, "limit": "100"},
            start_ms, ["timestamp_ms", "sell_vol", "buy_vol"])
        if not taker.empty:
            taker.to_csv(args.out / "taker_volume" / f"{currency}.csv",
                         index=False)

        ratio = page_back(
            "/api/v5/rubik/stat/contracts/long-short-account-ratio",
            {"ccy": currency, "period": args.period, "limit": "100"},
            start_ms, ["timestamp_ms", "long_short_ratio"])
        if not ratio.empty:
            ratio.to_csv(args.out / "long_short_ratio" / f"{currency}.csv",
                         index=False)

        row = {
            "currency": currency,
            "taker_rows": len(taker),
            "ratio_rows": len(ratio),
            "first": (str(pd.Timestamp(int(taker["timestamp_ms"].min()),
                                       unit="ms", tz="UTC"))
                      if not taker.empty else None),
        }
        summary.append(row)
        print(json.dumps(row), flush=True)

    (args.out / "manifest.json").write_text(json.dumps({
        "source": "OKX public v5 rubik statistics",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "requested_days": args.days,
        "series": summary,
        "retention_note": (
            "OKX serves roughly 30 days of rubik statistics. This dataset "
            "cannot support an edge claim on its own; it is a preliminary "
            "read and a motivation for recording forward."),
    }, indent=2) + "\n")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
