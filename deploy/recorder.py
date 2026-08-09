#!/usr/bin/env python3
"""Small Alpaca paper-data recorder used by the optional Compose lane.

The recorder writes normalized, append-only CSV rows for configured US equity
symbols. It has no order methods and never mutates trading state. A failed
authenticated read exits non-zero so the service health check cannot report a
fresh but empty dataset.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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
from agent.instruments import (  # noqa: E402
    validate_equity_symbol,
    validate_option_symbol,
)

from deploy.recorder_market import (  # noqa: E402
    FIELDS,
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


def _existing_state(output: Path) -> tuple[set[str], datetime | None, dict[str, datetime]]:
    if not output.exists() or output.stat().st_size == 0:
        return set(), None, {}
    keys: set[str] = set()
    latest: datetime | None = None
    latest_bars: dict[str, datetime] = {}
    try:
        with output.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or ())
            required = {"event_key", "event_type", "symbol", "timestamp"}
            if not required.issubset(fields):
                raise RuntimeError(
                    f"recorder dataset has invalid header; missing {sorted(required - fields)}")
            for row in reader:
                if None in row:
                    raise RuntimeError("recorder dataset contains a malformed CSV row")
                key = str(row.get("event_key") or "").strip()
                if key:
                    keys.add(key)
                else:
                    raise RuntimeError("recorder dataset row has no event_key")
                event, symbol, parsed = _validate_dataset_row(row)
                if latest is None or parsed > latest:
                    latest = parsed
                if event in {"bar", "bar_1m"} and (
                        symbol not in latest_bars or parsed > latest_bars[symbol]):
                    latest_bars[symbol] = parsed
    except (OSError, csv.Error) as exc:
        raise RuntimeError(f"cannot read recorder dataset {output}: {exc}") from exc
    return keys, latest, latest_bars


def _existing_keys(output: Path) -> set[str]:
    """Compatibility wrapper used by tests and operational inspection."""
    return _existing_state(output)[0]


def _regular_session_gap(previous: datetime, current: datetime) -> bool:
    zone = ZoneInfo("America/New_York")
    before = previous.astimezone(zone)
    after = current.astimezone(zone)
    if before.date() != after.date() or before.weekday() >= 5:
        return False
    open_minute = 9 * 60 + 30
    close_minute = 16 * 60
    before_minute = before.hour * 60 + before.minute
    after_minute = after.hour * 60 + after.minute
    return (open_minute <= before_minute <= close_minute and
            open_minute <= after_minute <= close_minute and
            current - previous > timedelta(minutes=5))


def _verify_bar_continuity(rows: list[dict], latest_bars: dict[str, datetime],
                           now: datetime, symbols: list[str]) -> None:
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
            if _regular_session_gap(previous, now):
                raise RuntimeError(f"recorder bar continuity gap for {symbol}")
            continue
        for current in fresh:
            if _regular_session_gap(previous, current):
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


def record_once(provider: AlpacaProvider, symbols: list[str], output: Path,
                *, feed: str | None = None, config: dict | None = None,
                include_options: bool | None = None, option_limit: int = 5) -> int:
    symbols = [validate_equity_symbol(symbol) for symbol in symbols]
    if not symbols:
        raise ValueError("at least one US equity symbol is required")
    now = datetime.now(timezone.utc)
    if include_options is None:
        classes = (config or {}).get("universe", {}).get("asset_classes", [])
        include_options = any(str(value).lower() in {"us_option", "option"}
                              for value in classes)
    output.parent.mkdir(parents=True, exist_ok=True)
    _migrate_header(output)
    seen, watermark, latest_bars = _existing_state(output)
    if watermark is not None and watermark > now + timedelta(seconds=5):
        raise RuntimeError("recorder dataset watermark is in the future")
    # Resume from durable data rather than a fixed three-minute lookback. The
    # one-minute overlap makes retries safe while the event key removes dupes.
    start = (watermark - timedelta(minutes=1)
             if watermark is not None else now - timedelta(minutes=3))
    rows = list(_rows(provider, symbols, now, feed=feed, config=config,
                      include_options=bool(include_options),
                      option_limit=option_limit, start=start))
    if not rows:
        raise RuntimeError("Alpaca returned no point-in-time bars or quotes")
    _verify_bar_continuity(rows, latest_bars, now, symbols)
    new_file = not output.exists() or output.stat().st_size == 0
    unique_rows = []
    for row in rows:
        key = row["event_key"]
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    if not unique_rows:
        return 0
    with output.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(unique_rows)
        handle.flush()
        os.fsync(handle.fileno())
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
    option_limit = max(1, min(5, int(os.getenv("ALPACA_RECORDER_OPTION_LIMIT", "5"))))
    include_options = any(str(value).lower() in {"us_option", "option"}
                          for value in cfg.get("universe", {}).get("asset_classes", []))
    while True:
        count = record_once(provider, symbols, output, config=cfg,
                            include_options=include_options,
                            option_limit=option_limit)
        print(f"recorded {count} Alpaca rows to {output}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
