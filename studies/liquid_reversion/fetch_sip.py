"""Fetch Alpaca Market Data v2 historical SIP bars into one gzip CSV per symbol and month.

Usage, from the repository root, with ``ALPACA_API_KEY`` and ``ALPACA_SECRET_KEY``
set in the environment::

    python -I studies/liquid_reversion/fetch_sip.py --out DIR --start 2016-01-01 \\
        --end 2026-10-16 --symbols SPY,QQQ,IWM,DIA,VTI

``python -m studies.liquid_reversion.fetch_sip`` takes the same arguments.

* Requests ``GET /v2/stocks/bars`` with ``feed=sip``, ``adjustment=raw`` and
  ``limit=10000``, following ``next_page_token`` until it is null.
* Keys travel only in the ``APCA-API-KEY-ID`` and ``APCA-API-SECRET-KEY``
  headers; they are never logged, printed or written.
* A client-side pacer keeps requests at or below 200 per minute; HTTP 429, 5xx
  and network errors are retried with bounded exponential back-off.
* Months are New York calendar months, so a session never spans two files. A
  month is written as final only when it is fully covered and has ended;
  anything else is written as ``.partial`` and fetched again on the next run.
* The free plan withholds the latest 15 minutes of SIP data, so every request
  ends at least 16 minutes before now.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # ``python -I path/to/fetch_sip.py`` puts neither the working directory nor
    # the script's folder on sys.path, so make the repository root importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse  # noqa: E402
import csv  # noqa: E402
import gzip  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
from collections.abc import Callable, Iterable, Mapping, Sequence  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402
from typing import Any, Protocol  # noqa: E402

from studies.liquid_reversion.bars import (ALPACA_CSV_COLUMNS, NEW_YORK, SYMBOL_PATTERN,  # noqa: E402
                                           TIMEFRAME_SECONDS, month_file_name, parse_utc)

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
CALENDAR_URL = "https://paper-api.alpaca.markets/v2/calendar"
KEY_ENV = "ALPACA_API_KEY"
SECRET_ENV = "ALPACA_SECRET_KEY"
DEFAULT_SYMBOLS = ("SPY", "QQQ", "IWM", "DIA", "VTI")
PAGE_LIMIT = 10_000
REQUESTS_PER_MINUTE = 200
SIP_DELAY = timedelta(minutes=16)
MAX_RETRIES = 6
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0
MAX_PAGES = 100_000
TIMEOUT_SECONDS = 30.0


class FetchError(RuntimeError):
    """A request failed for good, or a response was not what the API documents."""


class HttpSession(Protocol):
    def get(self, url: str, *, params: Mapping[str, Any], headers: Mapping[str, str],
            timeout: float) -> Any: ...


class Pacer:
    """Spaces requests evenly so that no more than ``per_minute`` start in any minute."""

    def __init__(self, per_minute: int = REQUESTS_PER_MINUTE, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.interval = 60.0 / per_minute
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        """Block until the next request may start."""
        if self._last is not None:
            delay = self._last + self.interval - self._clock()
            if delay > 0:
                self._sleep(delay)
        self._last = self._clock()


class AlpacaClient:
    """Authenticated, paced and retrying JSON GETs; the keys never leave the headers."""

    def __init__(self, session: HttpSession, *, key_id: str, secret_key: str, pacer: Pacer | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = MAX_RETRIES,
                 timeout: float = TIMEOUT_SECONDS) -> None:
        if not key_id or not secret_key:
            raise FetchError(f"set {KEY_ENV} and {SECRET_ENV} in the environment")
        self._session = session
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._pacer = pacer or Pacer(sleep=sleep)
        self._sleep = sleep
        self._max_retries = max_retries
        self._timeout = timeout
        self.requests = 0

    def __repr__(self) -> str:
        return "AlpacaClient(credentials hidden)"

    def get_json(self, url: str, params: Mapping[str, Any]) -> Any:
        """GET ``url`` and decode JSON, retrying 429, 5xx and network errors with back-off."""
        for attempt in range(self._max_retries + 1):
            self._pacer.wait()
            self.requests += 1
            last_attempt = attempt == self._max_retries
            try:
                response = self._session.get(url, params=dict(params), headers=self._headers,
                                             timeout=self._timeout)
            except OSError as error:  # requests' exceptions derive from OSError
                if last_attempt:
                    raise FetchError(f"network error from {url} after {attempt + 1} attempts: "
                                     f"{type(error).__name__}") from None
                self._sleep(backoff_seconds(attempt))
                continue
            status = int(response.status_code)
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise FetchError(f"{url} returned a body that is not JSON") from None
            if status == 429 or 500 <= status < 600:
                if last_attempt:
                    raise FetchError(f"HTTP {status} from {url} after {attempt + 1} attempts")
                headers = getattr(response, "headers", None) or {}
                self._sleep(backoff_seconds(attempt, headers.get("Retry-After")))
                continue
            # An error body can echo request headers (some proxies do), so the
            # credentials are scrubbed before any of it reaches a message or log.
            raise FetchError(f"HTTP {status} from {url}: {_brief(response, self._headers.values())}")
        raise FetchError("retry loop ended without a response")  # pragma: no cover


def backoff_seconds(attempt: int, retry_after: object = None) -> float:
    """Exponential back-off (1, 2, 4 ... seconds, capped), or a bounded ``Retry-After``."""
    try:
        hinted = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        hinted = None
    if hinted is not None and math.isfinite(hinted) and hinted >= 0:
        return min(hinted, BACKOFF_CAP_SECONDS)
    return min(BACKOFF_BASE_SECONDS * 2 ** attempt, BACKOFF_CAP_SECONDS)


def _brief(response: Any, secrets: Iterable[str] = ()) -> str:
    """The first 200 characters of a response body, with every secret replaced first."""
    text = str(getattr(response, "text", ""))
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return " ".join(text[:200].split()) or "no body"


def rfc3339(instant: datetime) -> str:
    """UTC instant in RFC 3339 form with a ``Z`` suffix."""
    return instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ny_midnight(day: date) -> datetime:
    """The UTC instant of midnight New York at the start of ``day``."""
    return datetime(day.year, day.month, day.day, tzinfo=NEW_YORK).astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class MonthPlan:
    """One New York calendar month and the part of it to request."""

    label: str
    month_start: datetime
    month_end: datetime
    fetch_start: datetime
    fetch_end: datetime

    @property
    def final(self) -> bool:
        """True when the whole month is requested; such a month has also ended."""
        return self.fetch_start == self.month_start and self.fetch_end == self.month_end


def plan_months(start: date, end: date, *, now: datetime) -> list[MonthPlan]:
    """Months overlapping [start, end] (inclusive dates), clipped to 16 minutes before ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if end < start:
        raise ValueError("end must not be before start")
    window_start = ny_midnight(start)
    window_end = min(ny_midnight(end + timedelta(days=1)), now.astimezone(timezone.utc) - SIP_DELAY)
    plans: list[MonthPlan] = []
    year, month = start.year, start.month
    while True:
        first = date(year, month, 1)
        following = date(year + month // 12, month % 12 + 1, 1)
        month_start, month_end = ny_midnight(first), ny_midnight(following)
        if month_start >= window_end:
            break
        fetch_start, fetch_end = max(month_start, window_start), min(month_end, window_end)
        if fetch_start < fetch_end:
            plans.append(MonthPlan(f"{year:04d}-{month:02d}", month_start, month_end, fetch_start, fetch_end))
        year, month = following.year, following.month
    return plans


def fetch_bars(client: AlpacaClient, symbols: Sequence[str], *, timeframe: str, start: datetime,
               end: datetime) -> dict[str, list[dict[str, Any]]]:
    """All bars for ``symbols`` in [start, end), following ``next_page_token`` until it is null."""
    params: dict[str, Any] = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": rfc3339(start),
                              "end": rfc3339(end), "feed": "sip", "adjustment": "raw", "limit": PAGE_LIMIT}
    out: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    token: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        query = dict(params) if token is None else {**params, "page_token": token}
        payload = client.get_json(BARS_URL, query)
        if not isinstance(payload, dict):
            raise FetchError("bars response is not a JSON object")
        pages = payload.get("bars") or {}
        if not isinstance(pages, dict):
            raise FetchError("bars response: 'bars' is not an object")
        for symbol, rows in pages.items():
            if symbol not in out or not isinstance(rows, list):
                raise FetchError(f"bars response: unexpected entry for {str(symbol)[:12]!r}")
            out[symbol].extend(rows)
        token = payload.get("next_page_token")
        if not token:
            return out
        if not isinstance(token, str) or token in seen:
            raise FetchError("bars response: page token repeated or malformed")
        seen.add(token)
    raise FetchError(f"more than {MAX_PAGES} pages for one month")


def _csv_number(value: object, *, required: bool) -> str:
    if value is None and not required:
        return ""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise FetchError(f"bars response: non-numeric field {value!r:.40}")
    return str(value) if isinstance(value, int) else repr(float(value))


def csv_rows(symbol: str, bars: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    """Validated CSV rows in time order; an exact repeat of a bar is dropped."""
    rows: dict[datetime, list[str]] = {}
    for bar in bars:
        if not isinstance(bar, Mapping):
            raise FetchError("bars response: a bar is not an object")
        stamp = bar.get("t")
        try:
            start = parse_utc(stamp)
        except ValueError:
            raise FetchError(f"bars response: bad timestamp {stamp!r:.40}") from None
        row = [symbol, str(stamp), *(_csv_number(bar.get(key), required=True) for key in ("o", "h", "l", "c", "v")),
               _csv_number(bar.get("n"), required=False), _csv_number(bar.get("vw"), required=False)]
        if start in rows and rows[start] != row:
            raise FetchError(f"bars response: two different {symbol} bars at {stamp}")
        rows[start] = row
    return [rows[start] for start in sorted(rows)]


def write_month_csv(path: Path, rows: Sequence[Sequence[str]]) -> None:
    """Write a gzip CSV atomically and byte-for-byte reproducibly (gzip mtime 0)."""
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed:
            with io.TextIOWrapper(packed, encoding="utf-8", newline="") as text:
                writer = csv.writer(text, lineterminator="\n")
                writer.writerow(ALPACA_CSV_COLUMNS)
                writer.writerows(rows)
    os.replace(temporary, path)


def fetch_to_directory(client: AlpacaClient, out: Path, *, symbols: Sequence[str], timeframe: str, start: date,
                       end: date, now: datetime, force: bool = False,
                       log: Callable[[str], None] = lambda message: None) -> dict[str, list[str]]:
    """Fetch every planned month; final months already on disk are skipped unless ``force``."""
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAME_SECONDS)}")
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, list[str]] = {"final": [], "partial": [], "skipped": []}
    for plan in plan_months(start, end, now=now):
        final_paths = {symbol: out / month_file_name(symbol, timeframe, plan.label) for symbol in symbols}
        wanted = [symbol for symbol in symbols
                  if force or not plan.final or not final_paths[symbol].is_file()]
        summary["skipped"].extend(final_paths[symbol].name for symbol in symbols if symbol not in wanted)
        if not wanted:
            continue
        fetched = fetch_bars(client, wanted, timeframe=timeframe, start=plan.fetch_start, end=plan.fetch_end)
        for symbol in wanted:
            rows = csv_rows(symbol, fetched[symbol])
            partial_path = out / month_file_name(symbol, timeframe, plan.label, partial=True)
            target = final_paths[symbol] if plan.final else partial_path
            write_month_csv(target, rows)
            if plan.final and partial_path.exists():
                partial_path.unlink()
            summary["final" if plan.final else "partial"].append(target.name)
            log(f"{symbol} {plan.label}: {len(rows)} bars, {'final' if plan.final else 'partial (will be re-fetched)'}")
    return summary


def fetch_calendar(client: AlpacaClient, start: date, end: date, *, url: str = CALENDAR_URL) -> list[dict[str, str]]:
    """Trading days with their regular open and close (New York) from the Trading API calendar."""
    payload = client.get_json(url, {"start": start.isoformat(), "end": end.isoformat()})
    if not isinstance(payload, list):
        raise FetchError("calendar response is not a list")
    out: list[dict[str, str]] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            raise FetchError("calendar response: an entry is not an object")
        try:
            day = date.fromisoformat(str(entry.get("date")))
            opening = datetime.strptime(str(entry.get("open")), "%H:%M").time()
            closing = datetime.strptime(str(entry.get("close")), "%H:%M").time()
        except ValueError:
            raise FetchError("calendar response: malformed date or time") from None
        out.append({"date": day.isoformat(), "open": opening.isoformat("minutes"),
                    "close": closing.isoformat("minutes")})
    return sorted(out, key=lambda entry: entry["date"])


def default_session() -> HttpSession:
    """A ``requests`` session; imported lazily so the study runs without it."""
    import requests

    return requests.Session()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch Alpaca historical SIP bars, one gzip CSV per symbol-month.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat, help="first date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=date.fromisoformat, help="last date (inclusive), YYYY-MM-DD")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--timeframe", choices=tuple(TIMEFRAME_SECONDS), default="1Min")
    parser.add_argument("--force", action="store_true", help="re-fetch final months already on disk")
    parser.add_argument("--calendar", action="store_true",
                        help="also write calendar.json from the Trading API calendar (marks early closes)")
    parser.add_argument("--calendar-url", default=CALENDAR_URL)
    return parser


def main(argv: Sequence[str] | None = None, *, session: HttpSession | None = None,
         environ: Mapping[str, str] | None = None, now: datetime | None = None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if environ is None else environ
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if not symbols or any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        print("--symbols must be a comma-separated list of tickers", file=sys.stderr)
        return 2
    key_id, secret_key = env.get(KEY_ENV, ""), env.get(SECRET_ENV, "")
    if not key_id or not secret_key:
        print(f"set {KEY_ENV} and {SECRET_ENV} in the environment", file=sys.stderr)
        return 2
    # The wall clock is read here, at the boundary, and passed into the logic.
    instant = datetime.now(timezone.utc) if now is None else now
    client = AlpacaClient(session or default_session(), key_id=key_id, secret_key=secret_key,
                          pacer=Pacer(clock=clock, sleep=sleep), sleep=sleep)
    try:
        summary = fetch_to_directory(client, args.out, symbols=symbols, timeframe=args.timeframe,
                                     start=args.start, end=args.end, now=instant, force=args.force,
                                     log=lambda message: print(message, file=sys.stderr))
        if args.calendar:
            last = min(args.end, (instant - SIP_DELAY).astimezone(NEW_YORK).date())
            days = fetch_calendar(client, args.start, last, url=args.calendar_url)
            (args.out / "calendar.json").write_text(json.dumps(days, indent=1) + "\n", encoding="utf-8")
    except FetchError as error:
        print(f"fetch failed: {error}", file=sys.stderr)
        return 1
    print(f"{len(summary['final'])} final and {len(summary['partial'])} partial files written, "
          f"{len(summary['skipped'])} already present, {client.requests} requests", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
