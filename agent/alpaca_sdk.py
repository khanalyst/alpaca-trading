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
    # Missing equity metadata is intentionally the zero-cost IEX lane.  The
    # caller may still request SIP explicitly; this fallback is only for
    # genuinely absent defaults and must not be used to relabel a response.
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        raw_value = "opra" if options else "iex"
    raw = str(raw_value).strip().lower().replace("-", "_")
    # Enum reprs from older alpaca-py releases can arrive as
    # ``DataFeed.SIP``/``OptionsFeed.OPRA`` rather than their value.  Treat the
    # final enum component as the wire name while retaining strict validation.
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
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
            "ask_price", "bid_size", "ask_size", "last_price", "greeks",
            "feed", "underlying_price", "underlying_last",
            "id", "qty", "side", "status", "time_in_force", "client_order_id",
            "filled_qty", "filled_avg_price", "submitted_at", "updated_at",
            "limit_price", "stop_price", "position_intent")
            if hasattr(value, name)}


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(f"invalid decimal value {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"decimal value {value!r} must be finite")
    return result


def _first(obj: Any, *names: str) -> Any:
    """Return the first non-null field across SDK/mapping aliases."""
    for name in names:
        value = _value(obj, name)
        if value is not None:
            return value
    return None


def normalize_quote(value: Any, symbol: str | None = None, feed: str | None = None) -> Quote:
    bid_raw = _value(value, "bid_price", _value(value, "bid"))
    ask_raw = _value(value, "ask_price", _value(value, "ask"))
    last_raw = _value(value, "last_price", _value(value, "last"))
    # A requested feed is an annotation only when the provider omitted feed
    # metadata.  Explicit response metadata always wins so SIP can never be
    # relabelled as IEX (or vice versa) at this boundary.
    response_feed = _value(value, "feed")
    effective_feed = response_feed if response_feed not in (None, "") else feed
    return Quote(
        symbol=str(_value(value, "symbol", symbol) or symbol or "").upper(),
        timestamp=_dt(_value(value, "timestamp")),
        bid=_decimal_or_none(bid_raw),
        ask=_decimal_or_none(ask_raw),
        bid_size=_decimal_or_none(_value(value, "bid_size")),
        ask_size=_decimal_or_none(_value(value, "ask_size")),
        last=_decimal_or_none(last_raw),
        feed=_canonical_feed(effective_feed, options=False),
    )


def normalize_bar(value: Any, symbol: str | None = None, feed: str | None = None) -> Bar:
    open_price = _decimal_or_none(_value(value, "open"))
    high_price = _decimal_or_none(_value(value, "high"))
    low_price = _decimal_or_none(_value(value, "low"))
    close_price = _decimal_or_none(_value(value, "close"))
    if any(price is None for price in (open_price, high_price, low_price, close_price)):
        raise ValueError("bar OHLC values are required")
    response_feed = _value(value, "feed")
    effective_feed = response_feed if response_feed not in (None, "") else feed
    return Bar(
        symbol=str(_value(value, "symbol", symbol) or symbol or "").upper(),
        timestamp=_dt(_value(value, "timestamp")) or datetime.min,
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=_decimal_or_none(_value(value, "volume", 0)),
        trade_count=_value(value, "trade_count"),
        vwap=_decimal_or_none(_value(value, "vwap")),
        feed=_canonical_feed(effective_feed, options=False),
        atr=_decimal_or_none(_value(value, "atr")),
    )
