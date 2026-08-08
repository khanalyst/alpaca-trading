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
import hashlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


FIELDS = (
    "event_key", "observed_at", "provider", "feed", "event_type", "symbol",
    "timestamp", "open", "high", "low", "close", "volume", "bid",
    "ask", "last",
)


def _value(value):
    return "" if value is None else str(value)


def _timeframe():
    try:
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        return TimeFrame(1, TimeFrameUnit.Minute)
    except ImportError:
        return "1Min"


def _feed(config: dict | None = None) -> str:
    """Resolve a feed using the same environment-first precedence as provider."""
    broker = (config or {}).get("broker") if isinstance(config, dict) else {}
    if not isinstance(broker, dict):
        broker = {}
    data = (config or {}).get("data") if isinstance(config, dict) else {}
    if not isinstance(data, dict):
        data = {}
    value = (os.getenv("ALPACA_DATA_FEED") or os.getenv("ALPACA_STOCK_FEED")
             or broker.get("data_feed") or data.get("feed") or "iex")
    return str(value).strip().lower() or "iex"


def _call_market_data(method, symbols, *, start, end, feed):
    """Pass ``feed`` to newer providers while supporting tiny test fakes."""
    attempts = (
        {"timeframe": _timeframe(), "start": start, "end": end, "feed": feed},
        {"timeframe": _timeframe(), "start": start, "end": end},
        {"start": start, "end": end, "feed": feed},
        {"start": start, "end": end},
    )
    last_error = None
    for kwargs in attempts:
        try:
            return method(symbols, **kwargs)
        except TypeError as exc:
            last_error = exc
    raise last_error  # type: ignore[misc]


def _call_quotes(method, symbols, *, start, end, feed):
    for kwargs in ({"start": start, "end": end, "feed": feed},
                   {"start": start, "end": end}):
        try:
            return method(symbols, **kwargs)
        except TypeError:
            continue
    return method(symbols, start=start, end=end)


def _event_key(event_type: str, symbol: str, timestamp: object) -> str:
    """Return a stable key used to suppress overlapping API windows."""
    raw = "|".join((str(event_type), str(symbol).upper(), _value(timestamp)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rows(provider: AlpacaProvider, symbols: list[str], now: datetime,
          *, feed: str | None = None):
    start = now - timedelta(minutes=3)
    # The provider owns environment/config precedence and canonicalization.
    # ``feed`` remains an explicit test seam; production callers leave it unset.
    feed = str(feed if feed is not None else
               getattr(provider, "data_feed", None) or _feed()).strip().lower() or "iex"
    bars = _call_market_data(provider.bars, symbols, start=start, end=now,
                              feed=feed)
    quotes = _call_quotes(provider.quotes, symbols, start=start, end=now,
                          feed=feed)
    observed = now.isoformat()
    for symbol, values in bars.items():
        for bar in values:
            timestamp = bar.timestamp.isoformat()
            yield {
                "event_key": _event_key("bar_1m", symbol, timestamp),
                "observed_at": observed, "provider": "alpaca",
                "feed": feed,
                "event_type": "bar_1m", "symbol": symbol,
                "timestamp": timestamp, "open": _value(bar.open),
                "high": _value(bar.high), "low": _value(bar.low),
                "close": _value(bar.close), "volume": _value(bar.volume),
                "bid": "", "ask": "", "last": "",
            }
    for symbol, values in quotes.items():
        for quote in values:
            timestamp = _value(quote.timestamp)
            yield {
                "event_key": _event_key("quote", symbol, timestamp),
                "observed_at": observed, "provider": "alpaca",
                "feed": feed,
                "event_type": "quote", "symbol": symbol,
                "timestamp": timestamp, "open": "", "high": "",
                "low": "", "close": "", "volume": "", "bid": _value(quote.bid),
                "ask": _value(quote.ask), "last": _value(quote.last),
            }


def _existing_keys(output: Path) -> set[str]:
    if not output.exists() or output.stat().st_size == 0:
        return set()
    keys: set[str] = set()
    try:
        with output.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                key = str(row.get("event_key") or "").strip()
                if key:
                    keys.add(key)
                elif row.get("event_type") and row.get("symbol"):
                    # Migrate legacy rows in-place without duplicating them.
                    keys.add(_event_key(row["event_type"], row["symbol"],
                                        row.get("timestamp", "")))
    except (OSError, csv.Error):
        return set()
    return keys


def _migrate_header(output: Path) -> None:
    """Upgrade a pre-dedup CSV before appending rows with ``event_key``."""
    if not output.exists() or output.stat().st_size == 0:
        return
    try:
        with output.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if "event_key" in (reader.fieldnames or []):
                return
            rows = list(reader)
    except (OSError, csv.Error):
        return
    migrated = []
    for row in rows:
        row = {str(key): value for key, value in row.items() if key is not None}
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
                *, feed: str | None = None) -> int:
    now = datetime.now(timezone.utc)
    rows = list(_rows(provider, symbols, now, feed=feed))
    if not rows:
        raise RuntimeError("Alpaca returned no bars or quotes")
    output.parent.mkdir(parents=True, exist_ok=True)
    _migrate_header(output)
    new_file = not output.exists() or output.stat().st_size == 0
    seen = _existing_keys(output)
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
    while True:
        count = record_once(provider, symbols, output)
        print(f"recorded {count} Alpaca rows to {output}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
