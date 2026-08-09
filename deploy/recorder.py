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


def _timestamp(value) -> datetime | None:
    """Parse an aware timestamp without inventing missing point-in-time data."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None and value.utcoffset() is not None else None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


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
    for raw_underlying in symbols:
        underlying = validate_equity_symbol(raw_underlying)
        spot = _underlying_price(quotes.get(underlying) or quotes.get(str(underlying).upper()))
        candidates = _call_options(
            method, underlying, now=now, underlying_price=spot,
            feed=option_feed, min_dte=min_dte, max_dte=max_dte) or []
        selected: dict[str, list[dict]] = {"call": [], "put": []}
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            right = _option_right(candidate)
            raw_symbol = str(candidate.get("symbol") or "").strip().upper()
            expiration = candidate.get("expiration") or candidate.get("expiration_date")
            strike = candidate.get("strike") or candidate.get("strike_price")
            try:
                symbol = validate_option_symbol(
                    raw_symbol, underlying=underlying,
                    expiration=expiration, strike=strike)
                candidate_underlying = validate_equity_symbol(
                    candidate.get("underlying") or
                    candidate.get("underlying_symbol") or underlying)
            except ValueError:
                continue
            if candidate_underlying != underlying:
                continue
            timestamp = candidate.get("timestamp") or candidate.get("quote_ts")
            multiplier = candidate.get("multiplier") or candidate.get("contract_size")
            parsed_timestamp = _timestamp(timestamp)
            if (right not in selected or not symbol or parsed_timestamp is None or
                    parsed_timestamp > now + timedelta(seconds=5)):
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
            candidate = dict(candidate)
            candidate["symbol"] = symbol
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
                    "underlying": underlying,
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
          include_options: bool = False, option_limit: int = 5,
          start: datetime | None = None):
    start = start or now - timedelta(minutes=3)
    if start.tzinfo is None or start.utcoffset() is None or start > now:
        raise ValueError("recorder start must be an aware timestamp at or before now")
    # The provider owns environment/config precedence and canonicalization.
    # ``feed`` remains an explicit test seam; production callers leave it unset.
    feed = str(feed if feed is not None else
               getattr(provider, "data_feed", None) or _feed()).strip().lower() or "iex"
    symbols = [validate_equity_symbol(symbol) for symbol in symbols]
    bars = _call_market_data(provider.bars, symbols, start=start, end=now,
                              feed=feed)
    quotes = _call_quotes(provider.quotes, symbols, start=start, end=now,
                          feed=feed)
    observed = now.isoformat()
    for raw_symbol, values in bars.items():
        symbol = validate_equity_symbol(raw_symbol)
        for bar in values:
            timestamp = _iso(getattr(bar, "timestamp", None))
            if not _point_in_time(timestamp):
                raise RuntimeError(f"bar {symbol!r} has no point-in-time timestamp")
            if _timestamp(timestamp) > now + timedelta(seconds=5):
                raise RuntimeError(f"bar {symbol!r} timestamp is in the future")
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
    for raw_symbol, values in quotes.items():
        symbol = validate_equity_symbol(raw_symbol)
        for quote in values:
            timestamp = _iso(getattr(quote, "timestamp", None))
            if not _point_in_time(timestamp):
                continue
            if _timestamp(timestamp) > now + timedelta(seconds=5):
                raise RuntimeError(f"quote {symbol!r} timestamp is in the future")
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
