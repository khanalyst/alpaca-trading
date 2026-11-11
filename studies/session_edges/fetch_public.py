"""Fetch the public diagnostic data the committed study results were computed on.

Usage, from the repository root::

    python -I studies/session_edges/fetch_public.py --out DIR \\
        --pairs nsxusd,spxusd --from-year 2010 --daily SPY,QQQ,IWM,DIA,VTI

* ``DIR/index``: index CFD one-minute archives, one zip per pair and year
  (one per month for the current year). Price only, no traded volume.
* ``DIR/daily``: adjusted daily bars, one ``<SYMBOL>.json.gz`` per symbol.

This is free, unauthenticated, diagnostic data. It is good enough to test a
price rule over many years, and it is not the deployment's feed: confirm any
finding on SIP bars (``studies/liquid_reversion/fetch_sip.py``) before acting
on it. No credential is read or sent.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import date
from pathlib import Path

ARCHIVE_PAGE = "https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair}/{year}"
ARCHIVE_POST = "https://www.histdata.com/get.php"
DAILY_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&period1=0"
             "&period2={end}&events=div,splits")
HEADERS = {"User-Agent": "Mozilla/5.0"}
PAIR_PATTERN = re.compile(r"[a-z]{6}")
SYMBOL_PATTERN = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
TOKEN_PATTERN = re.compile(r'id="tk" value="([0-9a-f]+)"')
RETRIES = 5
PAUSE_SECONDS = 1.5


def _get(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
         timeout: float = 120.0) -> bytes:
    error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            request = urllib.request.Request(url, data=data, headers={**HEADERS, **(headers or {})})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, OSError) as exc:
            error = exc
            time.sleep(PAUSE_SECONDS * (attempt + 2))
    raise RuntimeError(f"{url.split('?')[0]}: {type(error).__name__}")


def fetch_archive(out: Path, pair: str, year: int, month: int | None = None) -> str:
    """Download one index archive unless a complete copy is already present."""
    if not PAIR_PATTERN.fullmatch(pair):
        raise ValueError(f"unexpected pair {pair!r}")
    tag = f"{year}{month:02d}" if month else f"{year}"
    path = out / f"{pair}_{tag}.zip"
    if path.is_file() and path.stat().st_size > 1_000:
        return "present"
    page = ARCHIVE_PAGE.format(pair=pair, year=year) + (f"/{month}" if month else "")
    token = TOKEN_PATTERN.search(_get(page).decode("latin-1"))
    if token is None:
        return "not offered"
    form = urllib.parse.urlencode({"tk": token.group(1), "date": str(year), "datemonth": tag,
                                   "platform": "ASCII", "timeframe": "M1", "fxpair": pair.upper()}).encode()
    body = _get(ARCHIVE_POST, data=form, headers={"Referer": page,
                                                  "Content-Type": "application/x-www-form-urlencoded"})
    if body[:2] != b"PK":
        return "not an archive"
    path.write_bytes(body)
    return f"{len(body)} bytes"


def fetch_daily(out: Path, symbol: str) -> str:
    """Download one symbol's adjusted daily history."""
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError(f"unexpected symbol {symbol!r}")
    url = DAILY_URL.format(symbol=urllib.parse.quote(symbol), end=int(time.time()))
    result = (json.loads(_get(url, timeout=60.0)).get("chart") or {}).get("result") or []
    if not result:
        return "no data"
    quote = result[0]["indicators"]["quote"][0]
    adjusted = (result[0]["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    payload = {"symbol": symbol, "t": result[0].get("timestamp") or [], "o": quote.get("open"),
               "h": quote.get("high"), "l": quote.get("low"), "c": quote.get("close"),
               "v": quote.get("volume"), "adj": adjusted}
    with gzip.open(out / f"{symbol}.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return f"{len(payload['t'])} bars"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True)
    parser.add_argument("--pairs", default="nsxusd,spxusd", help="index archive prefixes; empty to skip")
    parser.add_argument("--from-year", type=int, default=2010)
    parser.add_argument("--daily", default="SPY,QQQ,IWM,DIA,VTI", help="daily symbols; empty to skip")
    args = parser.parse_args(argv)
    root = Path(args.out)
    today = date.today()
    index_dir, daily_dir = root / "index", root / "daily"
    for pair in [item.strip().lower() for item in args.pairs.split(",") if item.strip()]:
        index_dir.mkdir(parents=True, exist_ok=True)
        for year in range(args.from_year, today.year + 1):
            months = [None] if year < today.year else list(range(1, today.month + 1))
            for month in months:
                print(pair, year, month or "", fetch_archive(index_dir, pair, year, month), flush=True)
                time.sleep(PAUSE_SECONDS)
    for symbol in [item.strip().upper() for item in args.daily.split(",") if item.strip()]:
        daily_dir.mkdir(parents=True, exist_ok=True)
        print(symbol, fetch_daily(daily_dir, symbol), flush=True)
        time.sleep(PAUSE_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
