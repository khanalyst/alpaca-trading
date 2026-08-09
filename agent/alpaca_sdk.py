"""Small, dependency-free helpers for normalizing alpaca-py responses.

The provider keeps these names as direct aliases for backwards compatibility,
while this module owns feed constants, SDK enum conversion, and response
normalization.  Imports from :mod:`alpaca` remain inside the one helper that
needs them so policy/configuration imports never require alpaca-py.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from .alpaca_domain import Bar, Quote


EQUITY_FEEDS = {"iex", "sip", "delayed_sip"}
OPTION_FEEDS = {"indicative", "opra"}


def _canonical_feed(value: Any, *, options: bool = False) -> str:
    """Normalize configured feed names before SDK enum conversion."""
    raw_value = getattr(value, "value", value)
    raw = str(raw_value or ("indicative" if options else "iex")).strip().lower().replace("-", "_")
    aliases = {"delayed": "delayed_sip", "delayed_sip": "delayed_sip",
               "opra": "opra", "indicative": "indicative"}
    canonical = aliases.get(raw, raw)
    allowed = OPTION_FEEDS if options else EQUITY_FEEDS
    if canonical not in allowed:
        raise ValueError(f"unsupported {'option' if options else 'equity'} data feed {value!r}")
    return canonical


def _sdk_feed(value: str, *, options: bool = False):
    """Return alpaca-py DataFeed/OptionsFeed enum when installed."""
    try:
        from alpaca.data.enums import DataFeed
    except ImportError:
        return value
    if options:
        try:
            from alpaca.data.enums import OptionsFeed
        except ImportError:
            return value
        enum = OptionsFeed
    else:
        enum = DataFeed
    return getattr(enum, value.upper(), value)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(getattr(value, "value", value)).split(".")[-1].lower()


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        return dict(vars(value))
    except (TypeError, ValueError):
        return {name: getattr(value, name) for name in (
            "symbol", "contract", "underlying_symbol", "underlying",
            "expiration", "expiration_date", "expiry", "strike",
            "strike_price", "type", "right", "option_type", "multiplier",
            "contract_size", "volume", "open_interest", "latest_quote",
            "quote", "latest_trade", "last_trade", "daily_bar",
            "prev_daily_bar", "minute_bar", "timestamp", "bid_price",
            "ask_price", "bid_size", "ask_size", "last_price", "greeks")
            if hasattr(value, name)}


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _first(obj: Any, *names: str) -> Any:
    """Return the first non-null field across SDK/mapping aliases."""
    for name in names:
        value = _value(obj, name)
        if value is not None:
            return value
    return None


def normalize_quote(value: Any, symbol: str | None = None, feed: str | None = None) -> Quote:
    return Quote(
        symbol=str(_value(value, "symbol", symbol) or symbol or "").upper(),
        timestamp=_dt(_value(value, "timestamp")),
        bid=Decimal(str(_value(value, "bid_price", _value(value, "bid")))) if _value(value, "bid_price", _value(value, "bid")) is not None else None,
        ask=Decimal(str(_value(value, "ask_price", _value(value, "ask")))) if _value(value, "ask_price", _value(value, "ask")) is not None else None,
        bid_size=Decimal(str(_value(value, "bid_size"))) if _value(value, "bid_size") is not None else None,
        ask_size=Decimal(str(_value(value, "ask_size"))) if _value(value, "ask_size") is not None else None,
        last=Decimal(str(_value(value, "last_price", _value(value, "last")))) if _value(value, "last_price", _value(value, "last")) is not None else None,
        feed=_canonical_feed(feed or _value(value, "feed"), options=False),
    )


def normalize_bar(value: Any, symbol: str | None = None, feed: str | None = None) -> Bar:
    return Bar(
        symbol=str(_value(value, "symbol", symbol) or symbol or "").upper(),
        timestamp=_dt(_value(value, "timestamp")) or datetime.min,
        open=Decimal(str(_value(value, "open"))),
        high=Decimal(str(_value(value, "high"))),
        low=Decimal(str(_value(value, "low"))),
        close=Decimal(str(_value(value, "close"))),
        volume=Decimal(str(_value(value, "volume", 0))),
        trade_count=_value(value, "trade_count"),
        vwap=Decimal(str(_value(value, "vwap"))) if _value(value, "vwap") is not None else None,
        feed=_canonical_feed(feed or _value(value, "feed"), options=False),
        atr=_decimal_or_none(_value(value, "atr")),
    )
