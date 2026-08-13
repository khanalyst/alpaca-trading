"""Provider-facing market-data acquisition and row projection for recorder.

This module contains the market-data boundary used by :mod:`deploy.recorder`.
The recorder facade imports these symbols directly so existing callers and
tests retain their historical names while durable CSV orchestration remains in
``recorder.py``.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date, datetime, timedelta

from agent.alpaca_provider import AlpacaProvider
from agent.instruments import validate_equity_symbol, validate_option_symbol


FIELDS = (
    "event_key", "observed_at", "provider", "feed", "event_type", "symbol",
    "contract",
    "timestamp", "as_of", "open", "high", "low", "close", "volume", "bid",
    "ask", "last", "underlying", "expiration", "strike", "right",
    "multiplier", "bid_size", "ask_size", "open_interest",
    "underlying_price",
)


# A five-contract sample is too narrow to survive a trade: a contract can drift
# out of the ranking mid-trade. The per-side cap is configurable up to
# MAX_OPTION_LIMIT, and pinned contracts are sampled on top of it up to
# MAX_OPTION_SAMPLE so the request volume stays bounded either way.
MAX_OPTION_LIMIT = 25
MAX_OPTION_SAMPLE = 50


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
                 *, feed: str, config: dict | None, limit: int,
                 pinned: frozenset[str] = frozenset()):
    """Project bounded provider candidates into append-only option events.

    ``pinned`` contracts are sampled in addition to the top ``limit`` per side.
    A contract that drifts out of the ranking mid-trade would otherwise lose its
    exit quote and destroy the observation.
    """
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
                    # Runtime option selection requires a strictly positive,
                    # two-sided market.  Do not spend recorder capacity on
                    # zero-bid/zero-ask rows that replay can never execute.
                    bid is None or ask is None or bid <= 0 or ask <= 0 or
                    ask < bid):
                continue
            candidate = dict(candidate)
            candidate["symbol"] = symbol
            selected[right].append(candidate)
        for right in selected:
            selected[right].sort(key=lambda item: _option_rank(item, spot))
            unique = []
            symbols_seen = set()
            held = []
            for candidate in selected[right]:
                symbol = str(candidate["symbol"]).upper()
                if symbol in symbols_seen:
                    continue
                symbols_seen.add(symbol)
                if len(unique) < limit:
                    unique.append(candidate)
                elif symbol in pinned and len(held) < MAX_OPTION_SAMPLE - limit:
                    held.append(candidate)
            unique.extend(held)
            for candidate in unique:
                timestamp = candidate.get("timestamp") or candidate.get("quote_ts")
                volume = candidate.get("volume")
                if volume is None:
                    volume = candidate.get("day_volume")
                open_interest = candidate.get("open_interest")
                if open_interest is None:
                    open_interest = candidate.get("oi")
                yield {
                    "event_key": _event_key("option_snapshot", candidate["symbol"], _iso(timestamp)),
                    "observed_at": now.isoformat(), "provider": "alpaca",
                    "feed": str(candidate.get("feed") or option_feed),
                    "event_type": "option_snapshot", "symbol": candidate["symbol"],
                    "contract": candidate["symbol"],
                    "timestamp": _iso(timestamp), "as_of": _iso(timestamp),
                    "open": "", "high": "",
                    "low": "", "close": "", "volume": _value(volume),
                    "bid": _value(candidate.get("bid")), "ask": _value(candidate.get("ask")),
                    "last": _value(candidate.get("last")),
                    "underlying": underlying,
                    "expiration": _iso(candidate.get("expiration") or candidate.get("expiration_date")),
                    "strike": _value(candidate.get("strike") or candidate.get("strike_price")),
                    "right": right,
                    "multiplier": _value(candidate.get("multiplier") or candidate.get("contract_size")),
                    "bid_size": _value(candidate.get("bid_size")),
                    "ask_size": _value(candidate.get("ask_size")),
                    "open_interest": _value(open_interest),
                    "underlying_price": _value(candidate.get("underlying_price")),
                }


def _rows(provider: AlpacaProvider, symbols: list[str], now: datetime,
          *, feed: str | None = None, config: dict | None = None,
          include_options: bool = False, option_limit: int = 5,
          start: datetime | None = None,
          option_pins: frozenset[str] = frozenset()):
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
            bar_start = _timestamp(timestamp)
            if bar_start > now + timedelta(seconds=5):
                raise RuntimeError(f"bar {symbol!r} timestamp is in the future")
            bar_complete = bar_start + timedelta(minutes=1)
            # Alpaca timestamps one-minute bars at their open.  Never freeze an
            # in-progress OHLC row under its immutable event key.
            if bar_complete > now:
                continue
            yield {
                "event_key": _event_key("bar_1m", symbol, timestamp),
                "observed_at": observed, "provider": "alpaca",
                "feed": feed,
                "event_type": "bar_1m", "symbol": symbol, "contract": "",
                "timestamp": timestamp, "as_of": bar_complete.isoformat(),
                "open": _value(bar.open),
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
            limit=max(1, min(MAX_OPTION_LIMIT, int(option_limit))),
            pinned=frozenset(str(item).upper() for item in option_pins))
