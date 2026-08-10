#!/usr/bin/env python3
"""Small Alpaca paper-data recorder used by the optional Compose lane.

The recorder writes normalized, append-only CSV rows for configured US equity
symbols. It has no order methods and never mutates trading state. A failed
authenticated read exits non-zero so the service health check cannot report a
fresh but empty dataset.

Storage is partitioned by New York session date under ``sessions/`` next to the
nominal dataset path, with a durable sidecar index holding the watermark, the
per-symbol last bar, a time-bounded dedup window and the option contracts held
open for continued sampling. A cycle therefore costs O(new rows) instead of
rescanning the corpus. The index is a cache with a corruption check: partition
sizes are verified on load and a mismatch rebuilds it from the partitions.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ImportError:  # minimal health/recovery shell
    def load_dotenv(path, override=False):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if override or key not in os.environ:
                os.environ[key.strip()] = value.strip().strip('"\'')

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.alpaca_provider import AlpacaProvider  # noqa: E402
from agent.alpaca_session import (  # noqa: E402
    AlpacaError,
    normalize_calendar_day,
)
from agent.instruments import (  # noqa: E402
    validate_equity_symbol,
    validate_option_symbol,
)

from deploy.recorder_market import (  # noqa: E402
    FIELDS,
    MAX_OPTION_LIMIT,
    _call_market_data,
    _call_options,
    _call_quotes,
    _event_key,
    _feed,
    _iso,
    _number,
    _option_rank,
    _option_right,
    _option_rows,
    _options_feed,
    _point_in_time,
    _rows,
    _timeframe,
    _timestamp,
    _underlying_price,
    _value,
)


def _validate_dataset_row(row: dict) -> tuple[str, str, datetime]:
    event = str(row.get("event_type") or "").strip().lower()
    timestamp = _timestamp(row.get("timestamp") or row.get("as_of"))
    if timestamp is None:
        raise RuntimeError("recorder dataset row has an invalid timestamp")
    try:
        if event in {"bar", "bar_1m", "quote"}:
            symbol = validate_equity_symbol(row.get("symbol"))
        elif event in {"option", "option_snapshot"}:
            underlying = validate_equity_symbol(row.get("underlying"))
            symbol = validate_option_symbol(
                row.get("contract") or row.get("symbol"),
                underlying=underlying, expiration=row.get("expiration"),
                strike=row.get("strike"))
        else:
            raise ValueError(f"unsupported recorder event_type {event!r}")
    except ValueError as exc:
        raise RuntimeError(f"invalid recorder dataset row: {exc}") from exc
    return event, symbol, timestamp


NEW_YORK = ZoneInfo("America/New_York")
INDEX_NAME = ".recorder-index.json"
INDEX_SCHEMA = "recorder-index.v1"
PARTITION_DIR = "sessions"
# The recorder only ever asks the provider for ``watermark - 1 minute`` onwards,
# so a row it can legally receive is at most one minute older than the
# watermark. The dedup window is fifteen minutes: an order of magnitude of
# headroom over that overlap, and small enough that the index stays tiny. A row
# older than the window cannot be checked against durable keys, so it is a hard
# failure rather than a silent append -- replaying an old key is impossible, not
# merely unlikely.
DEDUP_HORIZON = timedelta(minutes=15)


def _session_date(value: datetime) -> date:
    return value.astimezone(NEW_YORK).date()


def _corpus_root(output: Path) -> Path:
    return output.parent


def _partition_path(output: Path, day: date) -> Path:
    return _corpus_root(output) / PARTITION_DIR / f"market-{day.isoformat()}.csv"


def corpus_partitions(output: Path) -> list[Path]:
    """Return the session partitions of a corpus, oldest session first."""
    directory = _corpus_root(output) / PARTITION_DIR
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("market-*.csv") if path.is_file())


def iter_corpus_rows(output: Path):
    """Stream every recorded row: legacy flat file first, then partitions."""
    sources = ([output] if output.exists() and output.stat().st_size else [])
    sources.extend(corpus_partitions(output))
    for source in sources:
        try:
            with source.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fields = set(reader.fieldnames or ())
                required = {"event_key", "event_type", "symbol", "timestamp"}
                if not required.issubset(fields):
                    raise RuntimeError(
                        f"recorder dataset has invalid header; missing {sorted(required - fields)}")
                for row in reader:
                    if None in row:
                        raise RuntimeError("recorder dataset contains a malformed CSV row")
                    yield row
        except (OSError, csv.Error) as exc:
            raise RuntimeError(f"cannot read recorder dataset {source}: {exc}") from exc


def _scan_corpus(output: Path) -> dict:
    """Rebuild durable state by reading the corpus once. Recovery path only."""
    keys: dict[str, str] = {}
    watermark: datetime | None = None
    latest_bars: dict[str, str] = {}
    for row in iter_corpus_rows(output):
        key = str(row.get("event_key") or "").strip()
        if not key:
            raise RuntimeError("recorder dataset row has no event_key")
        event, symbol, parsed = _validate_dataset_row(row)
        if key in keys:
            raise RuntimeError(f"recorder dataset repeats event_key {key}")
        keys[key] = parsed.isoformat()
        if watermark is None or parsed > watermark:
            watermark = parsed
        if event in {"bar", "bar_1m"}:
            previous = latest_bars.get(symbol)
            if previous is None or parsed.isoformat() > previous:
                latest_bars[symbol] = parsed.isoformat()
    index = {"schema": INDEX_SCHEMA,
             "watermark": watermark.isoformat() if watermark else None,
             "latest_bars": latest_bars, "recent_keys": keys,
             "option_pins": {}, "partitions": _partition_sizes(output)}
    return _prune_index(index)


def _partition_sizes(output: Path) -> dict[str, int]:
    return {path.name: path.stat().st_size for path in corpus_partitions(output)}


def _prune_index(index: dict) -> dict:
    """Drop dedup keys and option pins that fell out of their bounded window."""
    watermark = _timestamp(index.get("watermark"))
    if watermark is not None:
        floor = (watermark - DEDUP_HORIZON).isoformat()
        index["recent_keys"] = {key: value for key, value in index["recent_keys"].items()
                                if value >= floor}
    return index


def _load_index(output: Path) -> dict | None:
    """Return the sidecar index when it demonstrably matches the partitions."""
    path = _corpus_root(output) / INDEX_NAME
    if not path.is_file():
        return None
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        return None
    for name in ("latest_bars", "recent_keys", "option_pins", "partitions"):
        if not isinstance(index.get(name), dict):
            return None
    # A cycle appends rows and then rewrites the index; a crash between the two
    # leaves the index short. Byte sizes catch exactly that and force a rebuild.
    if index["partitions"] != _partition_sizes(output):
        return None
    if output.exists() and output.stat().st_size:
        return None
    return _prune_index(index)


def _save_index(output: Path, index: dict) -> None:
    root = _corpus_root(output)
    root.mkdir(parents=True, exist_ok=True)
    index = {**index, "schema": INDEX_SCHEMA, "partitions": _partition_sizes(output)}
    temporary = root / (INDEX_NAME + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(index, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, root / INDEX_NAME)


def _existing_state(output: Path) -> tuple[set[str], datetime | None, dict[str, datetime]]:
    """Durable state for one cycle: dedup window, watermark, per-symbol bars."""
    index = _load_index(output) or _scan_corpus(output)
    latest_bars = {symbol: _timestamp(value)
                   for symbol, value in index["latest_bars"].items()}
    return (set(index["recent_keys"]), _timestamp(index.get("watermark")),
            {symbol: value for symbol, value in latest_bars.items() if value is not None})


def _existing_keys(output: Path) -> set[str]:
    """Every durable key. Operational inspection only; a cycle never calls it."""
    return {str(row.get("event_key") or "") for row in iter_corpus_rows(output)}


class CalendarCache:
    """Cache the Alpaca trading calendar so continuity never fetches per cycle.

    The calendar is the authority on holidays and early closes. When it is
    unavailable the recorder keeps the conservative regular-session heuristic
    rather than inventing sessions, and retries at most once per local day.
    """

    def __init__(self, provider, *, span_days: int = 14) -> None:
        self.provider = provider
        self.span_days = int(span_days)
        self.days: dict[date, object] = {}
        self.covered: tuple[date, date] | None = None
        self.available: bool | None = None
        self.fetches = 0
        self._failed_on: date | None = None

    def _refresh(self, day: date) -> None:
        method = getattr(self.provider, "calendar", None)
        if not callable(method):
            self.available = False
            return
        start = day - timedelta(days=self.span_days)
        end = day + timedelta(days=self.span_days)
        try:
            rows = method(start=start, end=end) or []
        except (AlpacaError, TypeError, ValueError, OSError):
            self.available = False
            self._failed_on = day
            return
        self.fetches += 1
        self.days = {}
        for row in rows:
            try:
                session = normalize_calendar_day(row)
            except (TypeError, ValueError):
                continue
            self.days[session.date] = session
        self.covered = (start, end)
        self.available = True

    def session(self, day: date):
        """Return the calendar day, or ``None`` when the market is shut."""
        if self.available is False and self._failed_on == day:
            return None
        if self.covered is None or not (self.covered[0] <= day <= self.covered[1]):
            self._refresh(day)
        if self.available is not True:
            return None
        return self.days.get(day)

    def known(self, day: date) -> bool:
        self.session(day)
        return self.available is True


def _regular_session_gap(previous: datetime, current: datetime,
                         calendar: CalendarCache | None = None) -> bool:
    """Is the hole between two bars a real gap inside a scheduled session?"""
    before = previous.astimezone(NEW_YORK)
    after = current.astimezone(NEW_YORK)
    if before.date() != after.date():
        return False
    if current - previous <= timedelta(minutes=5):
        return False
    if calendar is not None and calendar.known(before.date()):
        session = calendar.session(before.date())
        if session is None:  # scheduled holiday: silence is correct
            return False
        open_minute = session.open.hour * 60 + session.open.minute
        close_minute = session.close.hour * 60 + session.close.minute
    else:
        if before.weekday() >= 5:
            return False
        open_minute = 9 * 60 + 30
        close_minute = 16 * 60
    before_minute = before.hour * 60 + before.minute
    after_minute = after.hour * 60 + after.minute
    return (open_minute <= before_minute <= close_minute and
            open_minute <= after_minute <= close_minute)


def _verify_bar_continuity(rows: list[dict], latest_bars: dict[str, datetime],
                           now: datetime, symbols: list[str],
                           calendar: CalendarCache | None = None) -> None:
    by_symbol: dict[str, list[datetime]] = {symbol: [] for symbol in symbols}
    for row in rows:
        if row.get("event_type") != "bar_1m":
            continue
        symbol = validate_equity_symbol(row.get("symbol"))
        parsed = _timestamp(row.get("timestamp"))
        if parsed is not None:
            by_symbol.setdefault(symbol, []).append(parsed)
    for symbol in symbols:
        previous = latest_bars.get(symbol)
        if previous is None:
            continue
        fresh = sorted({item for item in by_symbol.get(symbol, ()) if item > previous})
        if not fresh:
            if _regular_session_gap(previous, now, calendar):
                raise RuntimeError(f"recorder bar continuity gap for {symbol}")
            continue
        for current in fresh:
            if _regular_session_gap(previous, current, calendar):
                raise RuntimeError(f"recorder bar continuity gap for {symbol}")
            previous = current


def _migrate_header(output: Path) -> None:
    """Upgrade a pre-dedup CSV before appending rows with ``event_key``."""
    if not output.exists() or output.stat().st_size == 0:
        return
    try:
        with output.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if "event_key" in (reader.fieldnames or []):
                return
            required = {"event_type", "symbol", "timestamp"}
            fields = set(reader.fieldnames or ())
            if not required.issubset(fields):
                raise RuntimeError(
                    f"legacy recorder dataset has invalid header; missing {sorted(required - fields)}")
            rows = list(reader)
            if any(None in row for row in rows):
                raise RuntimeError("legacy recorder dataset contains a malformed CSV row")
    except (OSError, csv.Error) as exc:
        raise RuntimeError(f"cannot migrate recorder dataset {output}: {exc}") from exc
    migrated = []
    for row in rows:
        row = {str(key): value for key, value in row.items() if key is not None}
        _validate_dataset_row(row)
        row["event_key"] = _event_key(row.get("event_type", ""),
                                       row.get("symbol", ""),
                                       row.get("timestamp", ""))
        migrated.append({field: row.get(field, "") for field in FIELDS})
    temporary = output.with_suffix(output.suffix + ".migrate")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(migrated)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)


def _append_partitions(output: Path, rows: list[dict]) -> None:
    """Append rows to their session partitions, creating headers as needed."""
    by_day: dict[date, list[dict]] = {}
    for row in rows:
        parsed = _timestamp(row.get("timestamp"))
        if parsed is None:
            raise RuntimeError("recorder row has an invalid timestamp")
        by_day.setdefault(_session_date(parsed), []).append(row)
    for day in sorted(by_day):
        path = _partition_path(output, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            if fresh:
                writer.writeheader()
            writer.writerows({field: row.get(field, "") for field in FIELDS}
                             for row in by_day[day])
            handle.flush()
            os.fsync(handle.fileno())


def migrate_corpus(output: Path) -> int:
    """Split a legacy single-file corpus into durable session partitions.

    The legacy file is validated in full before anything is written and is kept
    beside the corpus as ``*.migrated`` afterwards, so a first run after upgrade
    either partitions the whole corpus or leaves it untouched.
    """
    _migrate_header(output)
    if not output.exists() or output.stat().st_size == 0:
        return 0
    rows: list[dict] = []
    seen: set[str] = set()
    for row in iter_corpus_rows(output):
        key = str(row.get("event_key") or "").strip()
        if not key:
            raise RuntimeError("recorder dataset row has no event_key")
        _validate_dataset_row(row)
        if key in seen:  # the migration is the one chance to restore uniqueness
            continue
        seen.add(key)
        rows.append(row)
    if corpus_partitions(output):
        raise RuntimeError(
            f"cannot migrate {output}: session partitions already exist")
    _append_partitions(output, rows)
    os.replace(output, output.with_name(output.name + ".migrated"))
    _save_index(output, _scan_corpus(output))
    return len(rows)


def record_once(provider: AlpacaProvider, symbols: list[str], output: Path,
                *, feed: str | None = None, config: dict | None = None,
                include_options: bool | None = None, option_limit: int = 5,
                option_hold: timedelta = timedelta(minutes=180),
                calendar: CalendarCache | None = None) -> int:
    symbols = [validate_equity_symbol(symbol) for symbol in symbols]
    if not symbols:
        raise ValueError("at least one US equity symbol is required")
    now = datetime.now(timezone.utc)
    if include_options is None:
        classes = (config or {}).get("universe", {}).get("asset_classes", [])
        include_options = any(str(value).lower() in {"us_option", "option"}
                              for value in classes)
    _corpus_root(output).mkdir(parents=True, exist_ok=True)
    migrate_corpus(output)
    index = _load_index(output) or _scan_corpus(output)
    seen = set(index["recent_keys"])
    watermark = _timestamp(index.get("watermark"))
    latest_bars = {symbol: parsed for symbol, parsed in
                   ((key, _timestamp(value))
                    for key, value in index["latest_bars"].items())
                   if parsed is not None}
    if watermark is not None and watermark > now + timedelta(seconds=5):
        raise RuntimeError("recorder dataset watermark is in the future")
    # Resume from durable data rather than a fixed three-minute lookback. The
    # one-minute overlap makes retries safe while the event key removes dupes.
    start = (watermark - timedelta(minutes=1)
             if watermark is not None else now - timedelta(minutes=3))
    pins = {contract: value for contract, value in index["option_pins"].items()
            if str(value) > now.isoformat()}
    rows = list(_rows(provider, symbols, now, feed=feed, config=config,
                      include_options=bool(include_options),
                      option_limit=option_limit, start=start,
                      option_pins=frozenset(pins)))
    if not rows:
        raise RuntimeError("Alpaca returned no point-in-time bars or quotes")
    _verify_bar_continuity(rows, latest_bars, now, symbols, calendar)
    horizon = None if watermark is None else watermark - DEDUP_HORIZON
    unique_rows = []
    for row in rows:
        parsed = _timestamp(row.get("timestamp"))
        if parsed is None:
            raise RuntimeError("recorder row has an invalid timestamp")
        if horizon is not None and parsed < horizon:
            # Outside the durable dedup window uniqueness cannot be proven, so
            # the row is refused rather than risking a replayed key.
            raise RuntimeError(
                "recorder received rows older than the dedup window")
        key = row["event_key"]
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
        index["recent_keys"][key] = parsed.isoformat()
        if watermark is None or parsed > watermark:
            watermark = parsed
        if row.get("event_type") in {"bar", "bar_1m"}:
            symbol = str(row.get("symbol") or "")
            if index["latest_bars"].get(symbol, "") < parsed.isoformat():
                index["latest_bars"][symbol] = parsed.isoformat()
    for row in rows:
        # Any sampled contract stays sampled for the hold horizon, so a
        # simulated trade opened on it keeps quotes through its exit.
        if row.get("event_type") == "option_snapshot":
            pins[str(row.get("contract") or row.get("symbol"))] = (
                now + option_hold).isoformat()
    index["option_pins"] = pins
    if not unique_rows:
        _save_index(output, index)
        return 0
    index["watermark"] = watermark.isoformat() if watermark else None
    _append_partitions(output, unique_rows)
    _save_index(output, _prune_index(index))
    return len(unique_rows)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="record Alpaca paper market data")
    p.add_argument("--out", default="runtime/research/recorded")
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--once", action="store_true")
    p.add_argument("--config", default="config.yaml")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    env_file = os.getenv("ALPACA_AGENT_SECRETS_FILE")
    if env_file:
        load_dotenv(env_file, override=False)
    from main import load_cfg
    cfg = load_cfg(args.config)
    symbols = list(cfg.get("universe", {}).get("symbols") or [])
    if not symbols:
        raise SystemExit("config.universe.symbols is empty")
    provider = AlpacaProvider(cfg)
    output = Path(args.out) / "market.csv"
    option_limit = max(1, min(MAX_OPTION_LIMIT,
                              int(os.getenv("ALPACA_RECORDER_OPTION_LIMIT", "10"))))
    option_hold = timedelta(minutes=max(1, int(
        os.getenv("ALPACA_RECORDER_OPTION_HOLD_MINUTES", "180"))))
    include_options = any(str(value).lower() in {"us_option", "option"}
                          for value in cfg.get("universe", {}).get("asset_classes", []))
    calendar = CalendarCache(provider)
    while True:
        count = record_once(provider, symbols, output, config=cfg,
                            include_options=include_options,
                            option_limit=option_limit,
                            option_hold=option_hold, calendar=calendar)
        print(f"recorded {count} Alpaca rows to {output}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
