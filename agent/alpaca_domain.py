"""Provider-neutral models used by the Alpaca runtime.

The application deliberately does not pass alpaca-py model objects through its
decision and risk layers.  These small immutable-ish records make those layers
easy to test with fakes and keep SDK version details at the provider boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping
import re

from .instruments import (validate_asset_class, validate_equity_symbol,
                          validate_instrument, validate_option_symbol)


_OCC_SYMBOL_RE = re.compile(r"^(?P<root>[A-Z0-9.]{1,8})(?P<expiry>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")


def parse_occ_symbol(symbol: Any) -> dict[str, Any] | None:
    """Parse an OCC option symbol into provider-neutral contract metadata.

    Alpaca normally supplies ``underlying_symbol``, expiry, strike and right
    as separate fields.  Historical chain responses and test fixtures often
    contain only the compact OCC symbol (for example
    ``SPY260821C00600000``), so parsing it at the boundary prevents risk code
    from guessing contract identity.  OCC strike values are expressed in
    thousandths of a dollar.
    """
    raw = str(symbol or "").strip().upper().replace(" ", "")
    match = _OCC_SYMBOL_RE.fullmatch(raw)
    if match is None:
        return None
    parts = match.groupdict()
    try:
        expiry = date(2000 + int(parts["expiry"][:2]), int(parts["expiry"][2:4]), int(parts["expiry"][4:]))
        strike = Decimal(parts["strike"]) / Decimal("1000")
    except (TypeError, ValueError):
        return None
    return {"symbol": raw, "underlying_symbol": parts["root"],
            "expiration_date": expiry, "strike_price": strike,
            "option_type": "call" if parts["right"] == "C" else "put",
            "contract_size": Decimal("100")}


def _object_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        return vars(value)
    except TypeError:
        # SDK model objects occasionally expose slots/properties only.
        return {name: getattr(value, name) for name in (
            "symbol", "asset_class", "class", "exchange", "status",
            "tradable", "underlying_symbol", "underlying",
            "expiration_date", "expiration", "strike_price", "strike",
            "option_type", "right", "type", "contract_size", "multiplier",
            "volume", "open_interest") if hasattr(value, name)}


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid decimal value {value!r}") from exc


def _text(value: Any, default: str = "") -> str:
    """Normalize SDK enums and ordinary strings without leaking enum reprs."""
    if value is None:
        return default
    raw = getattr(value, "value", value)
    return str(raw).split(".")[-1].lower()


def _boolean(value: Any, field: str, *, default: bool | None = None) -> bool:
    if value is None:
        if default is None:
            raise ValueError(f"asset {field} is required")
        return default
    if not isinstance(value, bool):
        raise ValueError(f"asset {field} must be true or false")
    return value


@dataclass(frozen=True)
class Asset:
    symbol: str
    asset_class: str = "us_equity"
    exchange: str | None = None
    status: str = "active"
    tradable: bool = True
    fractionable: bool = False
    shortable: bool = False
    easy_to_borrow: bool = False
    marginable: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        asset_class = validate_asset_class(self.asset_class)
        symbol = validate_instrument(self.symbol, asset_class)
        object.__setattr__(self, "asset_class", asset_class)
        object.__setattr__(self, "symbol", symbol)

    @property
    def is_option(self) -> bool:
        return self.asset_class.lower() in {"us_option", "option"}

    @classmethod
    def from_sdk(cls, value: Any) -> "Asset":
        if isinstance(value, cls):
            return value
        data = _object_mapping(value)
        symbol = str(data.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("asset is missing symbol")
        return cls(
            symbol=symbol,
            asset_class=_text(data.get("asset_class") or data.get("class")),
            exchange=_text(data.get("exchange"), "") or None,
            status=_text(data.get("status"), "active"),
            tradable=_boolean(data.get("tradable"), "tradable"),
            fractionable=_boolean(data.get("fractionable"), "fractionable", default=False),
            shortable=_boolean(data.get("shortable"), "shortable", default=False),
            easy_to_borrow=_boolean(data.get("easy_to_borrow"), "easy_to_borrow", default=False),
            marginable=_boolean(data.get("marginable"), "marginable", default=False),
            raw=dict(data) if isinstance(data, Mapping) else {},
        )


@dataclass(frozen=True)
class OptionContract(Asset):
    underlying_symbol: str = ""
    expiration_date: date | None = None
    strike_price: Decimal | None = None
    option_type: str = "call"
    contract_size: Decimal = Decimal("100")
    style: str = "american"
    volume: Decimal | None = None
    open_interest: Decimal | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        symbol = validate_option_symbol(
            self.symbol, self.underlying_symbol or None,
            expiration=self.expiration_date, strike=self.strike_price)
        object.__setattr__(self, "symbol", symbol)
        if self.underlying_symbol:
            object.__setattr__(self, "underlying_symbol",
                               validate_equity_symbol(self.underlying_symbol))

    @classmethod
    def from_sdk(cls, value: Any) -> "OptionContract":
        data = dict(_object_mapping(value))
        symbol = str(data.get("symbol") or "").strip().upper()
        # OCC parsing is a fallback only; explicit provider metadata always
        # wins so adjusted/non-standard contracts remain auditable.
        occ = parse_occ_symbol(symbol)
        if occ:
            for key, fallback in occ.items():
                data.setdefault(key, fallback)
        data.setdefault("asset_class", "us_option")
        # Option-chain snapshots often provide OCC identity and quotes without
        # repeating the contracts endpoint's tradable flag. A syntactically
        # valid listed OCC contract is the narrow safe fallback here; generic
        # equity assets still require explicit provider metadata.
        data.setdefault("tradable", True)
        base = Asset.from_sdk(data)
        raw_expiry = data.get("expiration_date") or data.get("expiration")
        expiry = raw_expiry if isinstance(raw_expiry, date) else (
            date.fromisoformat(str(raw_expiry)[:10]) if raw_expiry else None)
        # alpaca-py exposes ``type`` as OptionType.CALL/PUT while a number of
        # fixtures and API payloads use ``right``.  Normalize all forms at the
        # provider boundary so risk never compares enum reprs.
        option_type = _text(data.get("option_type") or data.get("right") or data.get("type"), "call")
        if option_type not in {"call", "put"}:
            raise ValueError("option_type must be call or put")
        if occ is not None and option_type != occ["option_type"]:
            raise ValueError("option_type does not match OCC symbol")
        return cls(
            **{k: getattr(base, k) for k in (
                "symbol", "exchange", "status", "tradable",
                "fractionable", "shortable", "easy_to_borrow", "marginable", "raw")},
            asset_class="us_option",
            underlying_symbol=str(data.get("underlying_symbol") or data.get("underlying") or (occ or {}).get("underlying_symbol") or "").upper(),
            expiration_date=expiry,
            strike_price=_decimal(data.get("strike_price") or data.get("strike")),
            option_type=option_type,
            contract_size=_decimal(data.get("contract_size") or data.get("size") or data.get("multiplier"), Decimal("100")) or Decimal("100"),
            style=_text(data.get("style"), "american"),
            volume=_decimal(data.get("volume") or data.get("day_volume")),
            open_interest=_decimal(data.get("open_interest") or data.get("open_interest")),
        )

    @property
    def right(self) -> str:
        return self.option_type

    @property
    def multiplier(self) -> Decimal:
        return self.contract_size

    @property
    def size(self) -> Decimal:
        return self.contract_size


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    last: Decimal | None = None
    feed: str = "iex"

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal("2")
        return self.last or self.bid or self.ask


@dataclass(frozen=True)
class OptionSnapshot:
    symbol: str
    contract: OptionContract | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    last: Decimal | None = None
    timestamp: datetime | None = None
    volume: Decimal | None = None
    open_interest: Decimal | None = None
    feed: str = "indicative"
    greeks: Mapping[str, Any] = field(default_factory=dict)
    underlying_price: Decimal | None = None

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal("2")
        return self.last or self.bid or self.ask


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")
    trade_count: int | None = None
    vwap: Decimal | None = None
    feed: str = "iex"
    # Wilder ATR computed by the market adapter from bars through this bar's
    # close.  It is deliberately optional at the provider boundary because
    # the provider returns raw OHLCV; :mod:`agent.market` attaches it without
    # ever consulting a future bar.
    atr: Decimal | None = None


@dataclass(frozen=True)
class Account:
    id: str | None
    status: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    currency: str = "USD"
    pattern_day_trader: bool = False


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: Decimal
    side: str
    market_value: Decimal | None = None
    avg_entry_price: Decimal | None = None
    current_price: Decimal | None = None
    unrealized_pl: Decimal | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", validate_instrument(self.symbol))


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    qty: Decimal
    side: str
    type: str = "market"
    time_in_force: str = "day"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str | None = None
    extended_hours: bool = False
    position_intent: str | None = None

    def __post_init__(self) -> None:
        symbol = validate_instrument(self.symbol)
        object.__setattr__(self, "symbol", symbol)
        side = _text(self.side).lower()
        if side not in {"buy", "sell"}:
            raise ValueError("order side must be buy or sell")
        object.__setattr__(self, "side", side)
        order_type = _text(self.type).lower()
        if order_type not in {"market", "limit"}:
            raise ValueError(f"unsupported order type {self.type!r}")
        object.__setattr__(self, "type", order_type)
        try:
            qty = Decimal(str(self.qty))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("order qty must be numeric") from exc
        if qty <= 0:
            raise ValueError("order qty must be positive")
        object.__setattr__(self, "qty", qty)
        # OCC option symbols are 21 characters (root + YYMMDD + C/P + strike)
        # but roots can be shorter/longer.  This deliberately errs on the
        # side of integer quantities whenever an OCC right is recognizable.
        if re.fullmatch(r"[A-Z0-9.]{1,8}\d{6}[CP]\d{8}", symbol) and qty != qty.to_integral_value():
            raise ValueError("option order qty must be an integer number of contracts")
        tif = _text(self.time_in_force).lower()
        object.__setattr__(self, "time_in_force", tif)
        if tif != "day":
            raise ValueError("time_in_force must be day")
        if order_type == "limit" and (self.limit_price is None or Decimal(str(self.limit_price)) <= 0):
            raise ValueError("limit orders require a positive limit_price")
        if self.position_intent is not None:
            intent = str(self.position_intent).lower()
            if intent not in {"buy_to_open", "buy_to_close", "sell_to_close"}:
                raise ValueError("only long option position intents are supported")
            object.__setattr__(self, "position_intent", intent)
        if self.client_order_id and len(self.client_order_id) > 48:
            raise ValueError("client_order_id must be at most 48 characters")


@dataclass(frozen=True)
class Order:
    id: str
    symbol: str
    qty: Decimal
    side: str
    status: str
    type: str
    time_in_force: str
    client_order_id: str | None = None
    filled_qty: Decimal = Decimal("0")
    filled_avg_price: Decimal | None = None
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class MarketClock:
    timestamp: datetime
    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None


@dataclass(frozen=True)
class CalendarDay:
    date: date
    open: datetime
    close: datetime
