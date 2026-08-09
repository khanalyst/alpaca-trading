"""Small, provider-neutral market-data model used by research.

The live data adapters are intentionally not imported here.  Adapters convert
their payloads at the boundary into these records and every replay consumes
only normalized records.  Keeping the provider/feed and point-in-time fields
on every event makes a result auditable without carrying a provider-specific
object through the research code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from agent.instruments import (
    reject_crypto,
    validate_asset_class,
    validate_equity_symbol,
    validate_option_symbol,
)


NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc


class NormalizationError(ValueError):
    """Raised when a provider payload cannot be made point-in-time safe."""


def _required(value: Any, name: str) -> Any:
    if value is None or value == "":
        raise NormalizationError(f"{name} is required")
    return value


def parse_timestamp(value: Any, *, name: str = "timestamp") -> datetime:
    """Parse an epoch or ISO timestamp and return an aware UTC datetime.

    Naive timestamps are rejected.  Assuming a timezone for an ambiguous
    provider timestamp is a silent session-boundary/look-ahead bug.
    """
    if isinstance(value, bool):
        raise NormalizationError(f"invalid {name}: {value!r}")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float, Decimal)):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise NormalizationError(f"invalid {name}: {value!r}") from exc
        if not math.isfinite(number):
            raise NormalizationError(f"invalid {name}: {value!r}")
        # Alpaca commonly emits nanoseconds while CSV exports use seconds or
        # milliseconds.  Magnitude is unambiguous for market dates.
        if abs(number) >= 1e17:
            number /= 1e9
        elif abs(number) >= 1e14:
            number /= 1e6
        elif abs(number) >= 1e11:
            number /= 1e3
        try:
            parsed = datetime.fromtimestamp(number, UTC)
        except (OSError, OverflowError, ValueError) as exc:
            raise NormalizationError(f"invalid {name}: {value!r}") from exc
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise NormalizationError(f"{name} is required")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise NormalizationError(f"invalid {name}: {value!r}") from exc
    else:
        raise NormalizationError(f"invalid {name}: {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NormalizationError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        result = float(_required(value, name))
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"{name} must be numeric") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise NormalizationError(f"{name} must be finite")
    if positive and result <= 0:
        raise NormalizationError(f"{name} must be positive")
    return result


def _field(payload: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


def _scoped_symbol(payload: Mapping[str, Any], value: Any, *, option: bool = False,
                   underlying: Any = None, expiration: Any = None,
                   strike: Any = None) -> str:
    """Enforce the repository-wide US equity/listed-option boundary."""
    try:
        reject_crypto(payload, "market event")
        asset_class = _field(payload, "asset_class", "class", "asset_type")
        if asset_class is not None:
            expected = "us_option" if option else "us_equity"
            if validate_asset_class(asset_class) != expected:
                raise ValueError(f"asset_class must be {expected}")
        return (validate_option_symbol(
                    value, underlying=underlying,
                    expiration=expiration, strike=strike) if option
                else validate_equity_symbol(value))
    except ValueError as exc:
        raise NormalizationError(str(exc)) from exc


@dataclass(frozen=True)
class EventIdentity:
    """Provenance common to all normalized records."""

    provider: str
    feed: str
    as_of: datetime
    observed_at: datetime
    session_date: date
    timezone: str = "America/New_York"
    schema: str = "market-event.v1"

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.feed.strip():
            raise NormalizationError("provider and feed are required")
        if self.as_of.tzinfo is None or self.observed_at.tzinfo is None:
            raise NormalizationError("as_of and observed_at must be timezone-aware")
        if self.as_of > self.observed_at:
            raise NormalizationError("as_of cannot be after observed_at")

    @property
    def as_of_ts(self) -> float:
        return self.as_of.timestamp()

    @property
    def provider_id(self) -> str:
        return self.provider

    @property
    def feed_id(self) -> str:
        return self.feed

    @property
    def as_of_timestamp(self) -> datetime:
        return self.as_of


def _identity(payload: Mapping[str, Any], *, provider: str | None = None,
              feed: str | None = None, timestamp: Any = None) -> EventIdentity:
    ts = parse_timestamp(timestamp if timestamp is not None else _field(
        payload, "timestamp", "ts", "t", "time"))
    observed = parse_timestamp(_field(payload, "observed_at", "received_at",
                                       "ingested_at", default=ts),
                               name="observed_at")
    as_of = parse_timestamp(_field(payload, "as_of", "asof", default=ts),
                            name="as_of")
    zone = str(_field(payload, "timezone", "tz", default="America/New_York"))
    try:
        local = ts.astimezone(ZoneInfo(zone))
    except Exception as exc:
        raise NormalizationError(f"invalid timezone: {zone!r}") from exc
    return EventIdentity(
        provider=str(_required(provider or _field(payload, "provider"), "provider")),
        feed=str(_required(feed or _field(payload, "feed", "feed_id"), "feed")),
        as_of=as_of,
        observed_at=observed,
        session_date=local.date(),
        timezone=zone,
        schema=str(_field(payload, "schema", "schema_version",
                          default="market-event.v1")),
    )


@dataclass(frozen=True)
class UnderlyingBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    identity: EventIdentity
    interval_seconds: int = 60
    atr: float | None = None

    def __post_init__(self) -> None:
        try:
            validate_equity_symbol(self.symbol)
        except ValueError as exc:
            raise NormalizationError(str(exc)) from exc
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise NormalizationError("bar timestamp must include a timezone")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(float(value)) for value in values):
            raise NormalizationError("bar values must be finite")
        if (self.high < max(self.open, self.close) or
                self.low > min(self.open, self.close) or self.high < self.low):
            raise NormalizationError("OHLC bounds are inconsistent")
        if self.volume < 0:
            raise NormalizationError("volume cannot be negative")
        if self.interval_seconds <= 0:
            raise NormalizationError("interval_seconds must be positive")

    @property
    def end(self) -> datetime:
        return self.timestamp + timedelta(seconds=self.interval_seconds)

    @property
    def session_date(self) -> date:
        return self.identity.session_date

    @property
    def provider(self) -> str:
        return self.identity.provider

    @property
    def ts(self) -> datetime:
        return self.timestamp

    @property
    def feed(self) -> str:
        return self.identity.feed

    @property
    def as_of(self) -> datetime:
        return self.identity.as_of

    @property
    def as_of_ts(self) -> float:
        return self.identity.as_of_ts

    @property
    def timezone(self) -> str:
        return self.identity.timezone


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float | None
    ask_size: float | None
    identity: EventIdentity

    def __post_init__(self) -> None:
        try:
            validate_equity_symbol(self.symbol)
        except ValueError as exc:
            raise NormalizationError(str(exc)) from exc
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise NormalizationError("quote timestamp must include a timezone")
        if not all(math.isfinite(float(value)) for value in (self.bid, self.ask)):
            raise NormalizationError("quote prices must be finite")
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise NormalizationError("quote must have 0 < bid <= ask")
        for name, value in (("bid_size", self.bid_size), ("ask_size", self.ask_size)):
            if value is not None and (not math.isfinite(float(value)) or value < 0):
                raise NormalizationError(f"{name} must be finite and non-negative")

    @property
    def session_date(self) -> date:
        return self.identity.session_date

    @property
    def ts(self) -> datetime:
        return self.timestamp

    @property
    def timezone(self) -> str:
        return self.identity.timezone

    @property
    def as_of(self) -> datetime:
        return self.identity.as_of

    @property
    def as_of_ts(self) -> float:
        return self.identity.as_of_ts


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    expiration: date
    strike: float
    right: str
    multiplier: int
    currency: str
    provider: str
    feed: str

    def __post_init__(self) -> None:
        try:
            validate_equity_symbol(self.underlying)
            validate_option_symbol(
                self.symbol, underlying=self.underlying,
                expiration=self.expiration, strike=self.strike)
        except ValueError as exc:
            raise NormalizationError(str(exc)) from exc
        if self.right.lower() not in {"call", "put", "c", "p"}:
            raise NormalizationError("option right must be call or put")
        if self.multiplier <= 0 or self.strike <= 0:
            raise NormalizationError("option multiplier and strike must be positive")
        if self.currency.upper() != "USD":
            raise NormalizationError("option currency must be USD")


@dataclass(frozen=True)
class OptionSnapshot:
    contract: OptionContract
    timestamp: datetime
    bid: float
    ask: float
    last: float | None
    underlying_price: float | None
    identity: EventIdentity

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise NormalizationError("option timestamp must include a timezone")
        numeric = [self.bid, self.ask]
        numeric.extend(value for value in (self.last, self.underlying_price)
                       if value is not None)
        if not all(math.isfinite(float(value)) for value in numeric):
            raise NormalizationError("option prices must be finite")
        if self.bid < 0 or self.ask < 0 or self.ask < self.bid:
            raise NormalizationError("option quote must have 0 <= bid <= ask")
        if self.last is not None and self.last < 0:
            raise NormalizationError("option last must be non-negative")
        if self.underlying_price is not None and self.underlying_price <= 0:
            raise NormalizationError("underlying price must be positive")

    @property
    def session_date(self) -> date:
        return self.identity.session_date

    @property
    def ts(self) -> datetime:
        return self.timestamp

    @property
    def timezone(self) -> str:
        return self.identity.timezone

    @property
    def as_of(self) -> datetime:
        return self.identity.as_of

    @property
    def as_of_ts(self) -> float:
        return self.identity.as_of_ts


def normalize_underlying_bar(payload: Mapping[str, Any], *, provider: str | None = None,
                              feed: str | None = None, interval_seconds: int = 60) -> UnderlyingBar:
    """Normalize an Alpaca-like OHLCV mapping into a :class:`UnderlyingBar`."""
    if interval_seconds <= 0:
        raise NormalizationError("interval_seconds must be positive")
    ts = parse_timestamp(_field(payload, "timestamp", "ts", "t", "time"))
    values = {name: _number(_field(payload, name, name[0]), name)
              for name in ("open", "high", "low", "close")}
    if values["high"] < max(values["open"], values["close"]) or values["low"] > min(values["open"], values["close"]):
        raise NormalizationError("OHLC bounds are inconsistent")
    volume = _number(_field(payload, "volume", "v", default=0), "volume")
    if volume < 0:
        raise NormalizationError("volume cannot be negative")
    return UnderlyingBar(
        symbol=_scoped_symbol(
            payload, _required(_field(payload, "symbol", "S"), "symbol")),
        timestamp=ts,
        **values,
        volume=volume,
        identity=_identity(payload, provider=provider, feed=feed, timestamp=ts),
        interval_seconds=interval_seconds,
    )


def normalize_quote(payload: Mapping[str, Any], *, provider: str | None = None,
                    feed: str | None = None) -> QuoteSnapshot:
    ts = parse_timestamp(_field(payload, "timestamp", "ts", "t", "time"))
    return QuoteSnapshot(
        symbol=_scoped_symbol(
            payload, _required(_field(payload, "symbol", "S"), "symbol")),
        timestamp=ts,
        bid=_number(_field(payload, "bid", "bp", "bid_price"), "bid"),
        ask=_number(_field(payload, "ask", "ap", "ask_price"), "ask"),
        bid_size=None if _field(payload, "bid_size", "bs") is None else _number(_field(payload, "bid_size", "bs"), "bid_size"),
        ask_size=None if _field(payload, "ask_size", "as") is None else _number(_field(payload, "ask_size", "as"), "ask_size"),
        identity=_identity(payload, provider=provider, feed=feed, timestamp=ts),
    )


def normalize_option_contract(payload: Mapping[str, Any], *, provider: str | None = None,
                              feed: str | None = None) -> OptionContract:
    expiration_raw = _required(_field(payload, "expiration", "expiry", "expiration_date"), "expiration")
    try:
        expiration = expiration_raw if isinstance(expiration_raw, date) and not isinstance(expiration_raw, datetime) else date.fromisoformat(str(expiration_raw)[:10])
    except ValueError as exc:
        raise NormalizationError("invalid option expiration") from exc
    right = str(_required(_field(payload, "right", "type", "option_type"), "right")).lower()
    underlying = _scoped_symbol(
        payload,
        _required(_field(payload, "underlying", "underlying_symbol"), "underlying"))
    symbol = _scoped_symbol(
        payload,
        _required(_field(payload, "symbol", "contract", "option_symbol"), "symbol"),
        option=True, underlying=underlying, expiration=expiration,
        strike=_field(payload, "strike", "strike_price"))
    try:
        multiplier = int(_field(payload, "multiplier", default=100))
    except (TypeError, ValueError, OverflowError) as exc:
        raise NormalizationError("option multiplier must be an integer") from exc
    return OptionContract(
        symbol=symbol,
        underlying=underlying,
        expiration=expiration,
        strike=_number(_field(payload, "strike", "strike_price"), "strike", positive=True),
        right={"c": "call", "p": "put"}.get(right, right),
        multiplier=multiplier,
        currency=str(_field(payload, "currency", default="USD")),
        provider=str(_required(provider or _field(payload, "provider"), "provider")),
        feed=str(_required(feed or _field(payload, "feed", "feed_id"), "feed")),
    )


def normalize_option_snapshot(payload: Mapping[str, Any], contract: OptionContract | None = None,
                              *, provider: str | None = None, feed: str | None = None) -> OptionSnapshot:
    ts = parse_timestamp(_field(payload, "timestamp", "ts", "t", "time"))
    if contract is None:
        contract = normalize_option_contract(payload, provider=provider, feed=feed)
    else:
        # A supplied contract must not let the payload bypass asset/symbol
        # scope checks. Optional identity fields, when present, must agree.
        try:
            reject_crypto(payload, "option snapshot")
            asset_class = _field(payload, "asset_class", "class", "asset_type")
            if asset_class is not None and validate_asset_class(asset_class) != "us_option":
                raise ValueError("asset_class must be us_option")
            raw_underlying = _field(payload, "underlying", "underlying_symbol")
            if raw_underlying is not None and validate_equity_symbol(raw_underlying) != contract.underlying:
                raise ValueError("option snapshot underlying does not match contract")
            raw_symbol = _field(payload, "symbol", "contract", "option_symbol")
            if (raw_symbol is not None and
                    validate_option_symbol(raw_symbol, underlying=contract.underlying) != contract.symbol):
                raise ValueError("option snapshot symbol does not match contract")
        except ValueError as exc:
            raise NormalizationError(str(exc)) from exc
    return OptionSnapshot(
        contract=contract,
        timestamp=ts,
        bid=_number(_field(payload, "bid", "bp", "bid_price", default=0), "bid"),
        ask=_number(_field(payload, "ask", "ap", "ask_price", default=0), "ask"),
        last=None if _field(payload, "last", "last_price", "p") is None else _number(_field(payload, "last", "last_price", "p"), "last"),
        underlying_price=None if _field(payload, "underlying_price", "underlying_last") is None else _number(_field(payload, "underlying_price", "underlying_last"), "underlying_price"),
        identity=_identity(payload, provider=provider or contract.provider, feed=feed or contract.feed, timestamp=ts),
    )


__all__ = [
    "EventIdentity", "NormalizationError", "OptionContract", "OptionSnapshot",
    "QuoteSnapshot", "UnderlyingBar", "normalize_option_contract",
    "normalize_option_snapshot", "normalize_quote",
    "normalize_underlying_bar",
    "parse_timestamp", "NEW_YORK", "UTC",
]
