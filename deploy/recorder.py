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
from datetime import date, datetime, timedelta, timezone
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
    "contract",
    "timestamp", "as_of", "open", "high", "low", "close", "volume", "bid",
    "ask", "last", "underlying", "expiration", "strike", "right",
    "multiplier", "bid_size", "ask_size", "open_interest",
    "underlying_price",
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


def _options_feed(config: dict | None = None) -> str:
    """Resolve the options feed with the provider's environment precedence."""
    broker = (config or {}).get("broker") if isinstance(config, dict) else {}
    if not isinstance(broker, dict):
        broker = {}
    value = (os.getenv("ALPACA_OPTIONS_FEED") or broker.get("options_feed")
             or "indicative")
    return str(value).strip().lower() or "indicative"


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


def _call_options(method, symbol, *, now, underlying_price, feed,
                  min_dte, max_dte):
    """Call option candidate providers while keeping tiny fakes usable."""
    attempts = (
        {"now": now, "underlying_price": underlying_price, "feed": feed,
         "min_dte": min_dte, "max_dte": max_dte},
        {"now": now, "underlying_price": underlying_price, "feed": feed},
        {"now": now, "underlying_price": underlying_price},
        {"now": now, "feed": feed},
        {},
    )
    last_error = None
    for kwargs in attempts:
        try:
            return method(symbol, **kwargs)
        except TypeError as exc:
            last_error = exc
    raise last_error  # type: ignore[misc]


def _event_key(event_type: str, symbol: str, timestamp: object) -> str:
    """Return a stable key used to suppress overlapping API windows."""
    raw = "|".join((str(event_type), str(symbol).upper(), _value(timestamp)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else _value(value)


def _point_in_time(value) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is not None and value.utcoffset() is not None
    if not isinstance(value, str) or not value.strip():
        return False
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _underlying_price(values) -> object:
    """Return the latest observed quote mid/last without inventing a price."""
    rows = sorted(values or (), key=lambda item: _iso(getattr(item, "timestamp", "")))
    for quote in reversed(rows):
        bid = _number(getattr(quote, "bid", None))
        ask = _number(getattr(quote, "ask", None))
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        last = _number(getattr(quote, "last", None))
        if last is not None:
            return last
    return None


def _option_right(candidate: dict) -> str:
    value = str(candidate.get("right") or candidate.get("option_type")
                or candidate.get("type") or "").strip().lower()
    return {"c": "call", "p": "put"}.get(value, value)


def _option_rank(candidate: dict, underlying_price: object) -> tuple:
    distance = _number(candidate.get("moneyness_distance"))
    if distance is None:
        strike = _number(candidate.get("strike") or candidate.get("strike_price"))
        spot = _number(underlying_price)
        distance = abs(strike - spot) / spot if strike and spot else float("inf")
    liquidity = sum(_number(candidate.get(key)) or 0.0
                    for key in ("volume", "open_interest", "bid_size", "ask_size"))
    ask = _number(candidate.get("ask"))
    bid = _number(candidate.get("bid"))
    spread = ask - bid if ask is not None and bid is not None else float("inf")
    return (distance, -liquidity, spread, str(candidate.get("symbol") or ""))


def _option_rows(provider, symbols: list[str], quotes: dict, now: datetime,
                 *, feed: str, config: dict | None, limit: int):
    """Project bounded provider candidates into append-only option events."""
    method = getattr(provider, "option_candidates", None)
    if not callable(method):
        return
    risk = (config or {}).get("risk") if isinstance(config, dict) else {}
    if not isinstance(risk, dict):
        risk = {}
    min_dte = int(risk.get("options_min_dte", 7) or 7)
    max_dte = int(risk.get("options_max_dte", 60) or 60)
    option_feed = _options_feed(config)
    for underlying in symbols:
        spot = _underlying_price(quotes.get(underlying) or quotes.get(str(underlying).upper()))
        candidates = _call_options(
            method, underlying, now=now, underlying_price=spot,
            feed=option_feed, min_dte=min_dte, max_dte=max_dte) or []
        selected: dict[str, list[dict]] = {"call": [], "put": []}
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            right = _option_right(candidate)
            symbol = str(candidate.get("symbol") or "").strip().upper()
            timestamp = candidate.get("timestamp") or candidate.get("quote_ts")
            expiration = candidate.get("expiration") or candidate.get("expiration_date")
            strike = candidate.get("strike") or candidate.get("strike_price")
            multiplier = candidate.get("multiplier") or candidate.get("contract_size")
            if (right not in selected or not symbol or timestamp is None or
                    not _point_in_time(timestamp)):
                continue
            bid = _number(candidate.get("bid")); ask = _number(candidate.get("ask"))
            strike_number = _number(strike); multiplier_number = _number(multiplier)
            try:
                expiry_date = expiration if isinstance(expiration, date) and not isinstance(expiration, datetime) else date.fromisoformat(str(expiration)[:10])
            except (TypeError, ValueError):
                expiry_date = None
            dte = None if expiry_date is None else (expiry_date - now.date()).days
            if (expiration is None or strike_number is None or strike_number <= 0 or
                    multiplier_number is None or multiplier_number <= 0 or
                    dte is None or dte < min_dte or dte > max_dte or
                    bid is None or ask is None or bid < 0 or ask < bid):
                continue
            selected[right].append(candidate)
        for right in selected:
            selected[right].sort(key=lambda item: _option_rank(item, spot))
            unique = []
            symbols_seen = set()
            for candidate in selected[right]:
                symbol = str(candidate["symbol"]).upper()
                if symbol in symbols_seen:
                    continue
                symbols_seen.add(symbol)
                unique.append(candidate)
                if len(unique) >= limit:
                    break
            for candidate in unique:
                timestamp = candidate.get("timestamp") or candidate.get("quote_ts")
                yield {
                    "event_key": _event_key("option_snapshot", candidate["symbol"], _iso(timestamp)),
                    "observed_at": now.isoformat(), "provider": "alpaca",
                    "feed": str(candidate.get("feed") or option_feed),
                    "event_type": "option_snapshot", "symbol": candidate["symbol"],
                    "contract": candidate["symbol"],
                    "timestamp": _iso(timestamp), "as_of": _iso(timestamp),
                    "open": "", "high": "",
                    "low": "", "close": "", "volume": _value(candidate.get("volume")),
                    "bid": _value(candidate.get("bid")), "ask": _value(candidate.get("ask")),
                    "last": _value(candidate.get("last")),
                    "underlying": candidate.get("underlying") or candidate.get("underlying_symbol") or underlying,
                    "expiration": _iso(candidate.get("expiration") or candidate.get("expiration_date")),
                    "strike": _value(candidate.get("strike") or candidate.get("strike_price")),
                    "right": right,
                    "multiplier": _value(candidate.get("multiplier") or candidate.get("contract_size")),
                    "bid_size": _value(candidate.get("bid_size")),
                    "ask_size": _value(candidate.get("ask_size")),
                    "open_interest": _value(candidate.get("open_interest")),
                    "underlying_price": _value(candidate.get("underlying_price")),
                }


def _rows(provider: AlpacaProvider, symbols: list[str], now: datetime,
          *, feed: str | None = None, config: dict | None = None,
          include_options: bool = False, option_limit: int = 5):
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
                "event_type": "bar_1m", "symbol": symbol, "contract": "",
                "timestamp": timestamp, "as_of": timestamp, "open": _value(bar.open),
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
                "event_type": "quote", "symbol": symbol, "contract": "",
                "timestamp": timestamp, "as_of": timestamp, "open": "", "high": "",
                "low": "", "close": "", "volume": "", "bid": _value(quote.bid),
                "ask": _value(quote.ask), "last": _value(quote.last),
            }
    if include_options:
        yield from _option_rows(
            provider, symbols, quotes, now, feed=feed, config=config,
            limit=max(1, min(5, int(option_limit))))


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
                *, feed: str | None = None, config: dict | None = None,
                include_options: bool | None = None, option_limit: int = 5) -> int:
    now = datetime.now(timezone.utc)
    if include_options is None:
        classes = (config or {}).get("universe", {}).get("asset_classes", [])
        include_options = any(str(value).lower() in {"us_option", "option"}
                              for value in classes)
    rows = list(_rows(provider, symbols, now, feed=feed, config=config,
                      include_options=bool(include_options),
                      option_limit=option_limit))
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
