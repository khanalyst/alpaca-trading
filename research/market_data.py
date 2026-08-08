"""Small, provider-neutral market-data model used by research.

The live data adapters are intentionally not imported here.  Adapters convert
their payloads at the boundary into these records and every replay consumes
only normalized records.  Keeping the provider/feed and point-in-time fields
on every event makes a result auditable without carrying a provider-specific
object through the research code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo


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
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float, Decimal)):
        number = float(value)
        # Alpaca commonly emits nanoseconds while CSV exports use seconds or
        # milliseconds.  Magnitude is unambiguous for market dates.
        if abs(number) >= 1e17:
            number /= 1e9
        elif abs(number) >= 1e14:
            number /= 1e6
        elif abs(number) >= 1e11:
            number /= 1e3
        parsed = datetime.fromtimestamp(number, UTC)
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
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise NormalizationError("quote must have 0 < bid <= ask")

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
        if self.right.lower() not in {"call", "put", "c", "p"}:
            raise NormalizationError("option right must be call or put")
        if self.multiplier <= 0 or self.strike <= 0:
            raise NormalizationError("option multiplier and strike must be positive")


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
        if self.bid < 0 or self.ask < 0 or self.ask < self.bid:
            raise NormalizationError("option quote must have 0 <= bid <= ask")

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
        symbol=str(_required(_field(payload, "symbol", "S"), "symbol")),
        timestamp=ts,
        **values,
        volume=volume,
        identity=_identity(payload, provider=provider, feed=feed, timestamp=ts),
        interval_seconds=interval_seconds,
    )


# Concise aliases used by data adapters and notebooks.
normalize_bar = normalize_underlying_bar


def normalize_quote(payload: Mapping[str, Any], *, provider: str | None = None,
                    feed: str | None = None) -> QuoteSnapshot:
    ts = parse_timestamp(_field(payload, "timestamp", "ts", "t", "time"))
    return QuoteSnapshot(
        symbol=str(_required(_field(payload, "symbol", "S"), "symbol")), timestamp=ts,
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
    return OptionContract(
        symbol=str(_required(_field(payload, "symbol", "contract", "option_symbol"), "symbol")),
        underlying=str(_required(_field(payload, "underlying", "underlying_symbol"), "underlying")),
        expiration=expiration,
        strike=_number(_field(payload, "strike", "strike_price"), "strike", positive=True),
        right={"c": "call", "p": "put"}.get(right, right),
        multiplier=int(_field(payload, "multiplier", default=100)),
        currency=str(_field(payload, "currency", default="USD")),
        provider=str(_required(provider or _field(payload, "provider"), "provider")),
        feed=str(_required(feed or _field(payload, "feed", "feed_id"), "feed")),
    )


def normalize_option_snapshot(payload: Mapping[str, Any], contract: OptionContract | None = None,
                              *, provider: str | None = None, feed: str | None = None) -> OptionSnapshot:
    ts = parse_timestamp(_field(payload, "timestamp", "ts", "t", "time"))
    contract = contract or normalize_option_contract(payload, provider=provider, feed=feed)
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
    "normalize_bar", "normalize_option_snapshot", "normalize_quote",
    "normalize_underlying_bar",
    "parse_timestamp", "NEW_YORK", "UTC",
]
