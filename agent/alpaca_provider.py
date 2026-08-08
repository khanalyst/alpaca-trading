"""Small injectable boundary around official :mod:`alpaca-py` clients.

No client is constructed, and no request is sent, until an authenticated
method is called.  Tests can pass fakes implementing the same methods; this is
intentional because importing alpaca-py must never be required for config and
clock checks.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from dataclasses import replace
from typing import Any

from .alpaca_domain import (Account, Asset, Bar, CalendarDay, MarketClock, Order,
                            OrderRequest, OptionContract, OptionSnapshot, Position,
                            Quote)
from .alpaca_session import normalize_calendar_day, paper_env_guard

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


class AlpacaError(RuntimeError):
    """Base provider failure."""


class CredentialsError(AlpacaError):
    """Authenticated operation requested without usable credentials."""


class PaperModeError(AlpacaError):
    """The runtime refuses live Alpaca endpoints unconditionally."""


class IdempotencyConflict(AlpacaError):
    """A client order id already belongs to a different order request."""


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
    )


class AlpacaSession:
    """Lazy construction and endpoint guard for alpaca-py clients."""

    def __init__(self, *, api_key: str | None = None, secret_key: str | None = None,
                 paper: bool = True, allow_live: bool = False,
                 trading_client: Any = None, stock_data_client: Any = None,
                 option_data_client: Any = None) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("ALPACA_API_KEY")
        self.secret_key = secret_key if secret_key is not None else os.getenv("ALPACA_SECRET_KEY")
        try:
            paper_env_guard()
        except ValueError as exc:
            raise PaperModeError(str(exc)) from exc
        if not isinstance(paper, bool) or not isinstance(allow_live, bool):
            raise ValueError("paper and allow_live must be booleans")
        if not paper or allow_live:
            raise PaperModeError("live Alpaca trading is disabled; paper=true and allow_live=false are required")
        self.paper = True
        # Kept as a compatibility attribute for callers that inspect it; it
        # can never enable a live endpoint.
        self.allow_live = False
        self._trading = trading_client
        self._stock_data = stock_data_client
        self._option_data = option_data_client

    def require_credentials(self) -> None:
        if not self.api_key or not self.secret_key:
            raise CredentialsError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required for authenticated actions")

    @classmethod
    def from_env(cls, *, paper: bool = True, allow_live: bool = False, **kwargs):
        try:
            paper_env_guard()
        except ValueError as exc:
            raise PaperModeError(str(exc)) from exc
        return cls(api_key=os.getenv("ALPACA_API_KEY"), secret_key=os.getenv("ALPACA_SECRET_KEY"), paper=paper, allow_live=allow_live, **kwargs)

    @property
    def endpoint(self) -> str:
        return "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"

    @property
    def trading(self):
        if self._trading is None:
            self.require_credentials()
            try:
                from alpaca.trading.client import TradingClient
            except ImportError as exc:
                raise AlpacaError("alpaca-py is not installed; install it for broker actions") from exc
            self._trading = TradingClient(self.api_key, self.secret_key, paper=self.paper)
        return self._trading

    @property
    def stock_data(self):
        if self._stock_data is None:
            self.require_credentials()
            try:
                from alpaca.data.historical import StockHistoricalDataClient
            except ImportError as exc:
                raise AlpacaError("alpaca-py is not installed; install it for market data") from exc
            self._stock_data = StockHistoricalDataClient(self.api_key, self.secret_key)
        return self._stock_data

    @property
    def option_data(self):
        if self._option_data is None:
            self.require_credentials()
            try:
                from alpaca.data.historical import OptionHistoricalDataClient
            except ImportError as exc:
                raise AlpacaError("alpaca-py is not installed; install it for option data") from exc
            self._option_data = OptionHistoricalDataClient(self.api_key, self.secret_key)
        return self._option_data


class AlpacaProvider:
    """Normalized account, asset, market-data and order operations."""

    def __init__(self, config: Mapping[str, Any] | None = None,
                 session: AlpacaSession | None = None, **clients: Any) -> None:
        cfg = dict(config or {})
        mode = str(cfg.get("mode") or cfg.get("broker", {}).get("mode") or "paper").lower()
        if mode == "demo":
            mode = "paper"
        if mode != "paper":
            raise PaperModeError("only paper mode is supported")
        broker = cfg.get("broker") if isinstance(cfg.get("broker"), Mapping) else {}
        data_cfg = cfg.get("data") if isinstance(cfg.get("data"), Mapping) else {}
        paper_value = broker.get("paper", True)
        allow_live_value = broker.get("allow_live", cfg.get("allow_live", False))
        if not isinstance(paper_value, bool) or not isinstance(allow_live_value, bool):
            raise ValueError("broker.paper and broker.allow_live must be booleans")
        if not paper_value or allow_live_value:
            raise PaperModeError("broker.paper must be true and allow_live must be false")
        # Endpoint overrides are deliberately not accepted: TradingClient's
        # paper flag is the sole endpoint selector and is pinned true.
        if broker.get("endpoint"):
            raise PaperModeError("broker endpoint overrides are disabled")
        import_env_feed = os.getenv("ALPACA_DATA_FEED") or os.getenv("ALPACA_STOCK_FEED")
        import_env_option_feed = os.getenv("ALPACA_OPTIONS_FEED")
        configured_data_feed = broker.get("data_feed") if "data_feed" in broker else data_cfg.get("feed", "iex")
        configured_options_feed = broker.get("options_feed") if "options_feed" in broker else data_cfg.get("options_feed", "indicative")
        self.data_feed = _canonical_feed(import_env_feed or configured_data_feed)
        self.options_feed = _canonical_feed(import_env_option_feed or configured_options_feed, options=True)
        paper = True
        allow_live = False
        self.session = session or AlpacaSession(
            api_key=broker.get("api_key"), secret_key=broker.get("secret_key"),
            paper=paper, allow_live=allow_live,
            trading_client=clients.get("trading_client"),
            stock_data_client=clients.get("stock_data_client"),
            option_data_client=clients.get("option_data_client"))
        self._seen_requests: dict[str, OrderRequest] = {}
        self._submitted_orders: dict[str, Order] = {}

    @property
    def paper(self) -> bool:
        return self.session.paper

    def clock(self) -> MarketClock:
        try:
            value = self.session.trading.get_clock()
            timestamp = _dt(_value(value, "timestamp"))
            if timestamp is None:
                raise ValueError("clock timestamp is missing")
            return MarketClock(timestamp, bool(_value(value, "is_open", False)), _dt(_value(value, "next_open")), _dt(_value(value, "next_close")))
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"clock request failed: {exc}") from exc

    def calendar(self, start=None, end=None) -> list[CalendarDay]:
        try:
            from alpaca.trading.requests import GetCalendarRequest
            kwargs = {}
            if start is not None: kwargs["start"] = start
            if end is not None: kwargs["end"] = end
            request = GetCalendarRequest(**kwargs)
            try:
                rows = self.session.trading.get_calendar(request)
            except TypeError:
                rows = self.session.trading.get_calendar(start=start, end=end)
        except ImportError:
            try:
                rows = self.session.trading.get_calendar(start=start, end=end)
            except TypeError:
                try:
                    rows = self.session.trading.get_calendar()
                except Exception as exc:  # noqa: BLE001
                    raise AlpacaError(f"calendar request failed: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise AlpacaError(f"calendar request failed: {exc}") from exc
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"calendar request failed: {exc}") from exc
        return [normalize_calendar_day(row) for row in (rows or [])]

    def assets(self, *, asset_class: str = "us_equity", status: str = "active") -> list[Asset]:
        try:
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass, AssetStatus
            asset_class_value = getattr(AssetClass, str(asset_class).upper(), asset_class)
            status_value = getattr(AssetStatus, str(status).upper(), status)
            request = GetAssetsRequest(asset_class=asset_class_value, status=status_value)
            rows = self.session.trading.get_all_assets(request)
        except ImportError:
            try:
                rows = self.session.trading.get_all_assets()
            except Exception as exc:  # noqa: BLE001
                raise AlpacaError(f"asset request failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"asset request failed: {exc}") from exc
        return [Asset.from_sdk(row) for row in rows]

    def option_contracts(self, underlying_symbol: str | None = None, **kwargs) -> list[OptionContract]:
        try:
            from alpaca.trading.requests import GetOptionContractsRequest
            try:
                from alpaca.trading.enums import AssetStatus
            except ImportError:
                AssetStatus = None
            try:
                from alpaca.trading.enums import ContractType
            except ImportError:
                ContractType = None
            try:
                from alpaca.trading.enums import ExerciseStyle
            except ImportError:
                ExerciseStyle = None
            request_kwargs = dict(kwargs)
            if underlying_symbol:
                request_kwargs["underlying_symbol"] = underlying_symbol
            if AssetStatus is not None and request_kwargs.get("status") is not None:
                request_kwargs["status"] = getattr(AssetStatus, str(request_kwargs["status"]).upper(), request_kwargs["status"])
            if ContractType is not None and request_kwargs.get("type") is not None:
                request_kwargs["type"] = getattr(ContractType, str(request_kwargs["type"]).upper(), request_kwargs["type"])
            if ExerciseStyle is not None and request_kwargs.get("style") is not None:
                request_kwargs["style"] = getattr(ExerciseStyle, str(request_kwargs["style"]).upper(), request_kwargs["style"])
            request = GetOptionContractsRequest(**request_kwargs)
            getter = getattr(self.session.trading, "get_option_contracts")
            rows = getter(request)
            # API response is a paginated object in alpaca-py 0.43.x.
            if hasattr(rows, "option_contracts"):
                rows = rows.option_contracts
            elif hasattr(rows, "data"):
                rows = rows.data
        except ImportError:
            getter = getattr(self.session.trading, "get_option_contracts", None)
            if getter is None:
                raise AlpacaError("alpaca-py option-contract request support is unavailable")
            try:
                rows = getter(underlying_symbol=underlying_symbol, **kwargs) if underlying_symbol else getter(**kwargs)
            except Exception as exc:  # noqa: BLE001
                raise AlpacaError(f"option contract request failed: {exc}") from exc
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"option contract request failed: {exc}") from exc
        if isinstance(rows, Mapping):
            normalized_rows = []
            for symbol, row in rows.items():
                if isinstance(row, Mapping):
                    item = dict(row)
                    item.setdefault("symbol", symbol)
                    normalized_rows.append(item)
                else:
                    normalized_rows.append(row)
            rows = normalized_rows
        return [OptionContract.from_sdk(row) for row in (rows or [])]

    def account(self) -> Account:
        try:
            value = self.session.trading.get_account()
            return Account(id=_value(value, "id"), status=_text(_value(value, "status")), equity=Decimal(str(_value(value, "equity", 0))), cash=Decimal(str(_value(value, "cash", 0))), buying_power=Decimal(str(_value(value, "buying_power", 0))), currency=_text(_value(value, "currency"), "usd").upper(), pattern_day_trader=bool(_value(value, "pattern_day_trader", False)))
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"account request failed: {exc}") from exc

    def positions(self) -> list[Position]:
        try:
            rows = self.session.trading.get_all_positions()
            return [Position(symbol=str(_value(row, "symbol", "")).upper(), qty=Decimal(str(_value(row, "qty", 0))), side=_text(_value(row, "side"), "long"), market_value=Decimal(str(_value(row, "market_value"))) if _value(row, "market_value") is not None else None, avg_entry_price=Decimal(str(_value(row, "avg_entry_price"))) if _value(row, "avg_entry_price") is not None else None, current_price=Decimal(str(_value(row, "current_price"))) if _value(row, "current_price") is not None else None, unrealized_pl=Decimal(str(_value(row, "unrealized_pl"))) if _value(row, "unrealized_pl") is not None else None, raw=dict(row) if isinstance(row, Mapping) else {}) for row in rows]
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"positions request failed: {exc}") from exc

    def orders(self, *, status: str | None = None, client_order_id: str | None = None) -> list[Order]:
        try:
            # get_order_by_client_id is exact and avoids pulling an unbounded
            # order history when reconciling one id after a timeout.
            if client_order_id and hasattr(self.session.trading, "get_order_by_client_id"):
                try:
                    row = self.session.trading.get_order_by_client_id(client_order_id=client_order_id)
                except TypeError:
                    row = self.session.trading.get_order_by_client_id(client_order_id)
                return [self._order(row)] if row is not None else []
            from alpaca.trading.requests import GetOrdersRequest
            try:
                from alpaca.trading.enums import QueryOrderStatus
            except ImportError:
                QueryOrderStatus = None
            kwargs = {}
            if status:
                kwargs["status"] = getattr(QueryOrderStatus, str(status).upper(), status) if QueryOrderStatus else status
            # The trading API's order filter is bounded by ``limit`` and does
            # not expose the data API's page-token pagination.  Keep this
            # reconciliation query within the documented maximum; exact
            # idempotency lookups use ``get_order_by_client_id`` above.
            request_kwargs = dict(kwargs)
            request_kwargs.setdefault("limit", 500)
            response = self.session.trading.get_orders(GetOrdersRequest(**request_kwargs))
            rows = getattr(response, "orders", getattr(response, "data", response))
            if isinstance(rows, Mapping):
                rows = rows.get("orders", rows.values())
            rows = list(rows or [])
        except ImportError:
            try:
                rows = self.session.trading.get_orders(**({"status": status} if status else {}))
            except TypeError:
                rows = self.session.trading.get_orders()
            if status:
                rows = [row for row in rows if _text(_value(row, "status", "")) == str(status).lower()]
            if client_order_id:
                rows = [row for row in rows if _value(row, "client_order_id") == client_order_id]
        except TypeError:
            # Injected fakes often implement the older kwargs shape even when
            # alpaca-py is present in the test environment.
            rows = self.session.trading.get_orders(status=status)
            if client_order_id:
                rows = [row for row in rows if _value(row, "client_order_id") == client_order_id]
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"orders request failed: {exc}") from exc
        return [self._order(row) for row in rows]

    def _order(self, row: Any) -> Order:
        return Order(id=str(_value(row, "id", "")), symbol=str(_value(row, "symbol", "")).upper(), qty=Decimal(str(_value(row, "qty", 0))), side=_text(_value(row, "side")), status=_text(_value(row, "status")), type=_text(_value(row, "type"), "market"), time_in_force=_text(_value(row, "time_in_force"), "day"), client_order_id=_value(row, "client_order_id"), filled_qty=Decimal(str(_value(row, "filled_qty", 0))), filled_avg_price=Decimal(str(_value(row, "filled_avg_price"))) if _value(row, "filled_avg_price") is not None else None, submitted_at=_dt(_value(row, "submitted_at")), updated_at=_dt(_value(row, "updated_at")), raw=dict(row) if isinstance(row, Mapping) else {})

    def submit_order(self, request: OrderRequest) -> Order:
        cid = request.client_order_id or f"alpaca-{uuid.uuid4().hex[:24]}"
        if request.client_order_id != cid:
            request = replace(request, client_order_id=cid)
        prior = self._seen_requests.get(cid)
        if prior and prior != request:
            raise IdempotencyConflict(f"client_order_id {cid!r} was already used for a different request")
        cached = self._submitted_orders.get(cid)
        if cached is not None:
            return cached
        self._seen_requests[cid] = request
        # A provider-side lookup makes retries safe across process restarts.
        existing = self.orders(client_order_id=cid)
        if existing:
            self._submitted_orders[cid] = existing[0]
            return existing[0]
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
            side = OrderSide.BUY if request.side == "buy" else OrderSide.SELL
            tif = getattr(TimeInForce, request.time_in_force.upper())
            cls = MarketOrderRequest if request.type == "market" else LimitOrderRequest
            qty = int(request.qty) if request.qty == request.qty.to_integral_value() else float(request.qty)
            kwargs = {"symbol": request.symbol, "qty": qty, "side": side, "time_in_force": tif, "client_order_id": cid}
            if request.limit_price is not None:
                kwargs["limit_price"] = float(request.limit_price)
            if request.position_intent is not None:
                # alpaca-py accepts a PositionIntent enum on option orders;
                # importing it lazily keeps stock-only installs usable.
                try:
                    from alpaca.trading.enums import PositionIntent
                    kwargs["position_intent"] = getattr(PositionIntent, request.position_intent.upper())
                except (ImportError, AttributeError):
                    kwargs["position_intent"] = request.position_intent
            sdk_request = cls(**kwargs)
        except ImportError:
            # Injectable fakes should not need alpaca-py installed.  A fake
            # may accept our normalized request directly.
            try:
                result = self._order(self.session.trading.submit_order(request))
            except AlpacaError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise AlpacaError(f"submit order failed: {exc}") from exc
        else:
            try:
                result = self._order(self.session.trading.submit_order(sdk_request))
            except AlpacaError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise AlpacaError(f"submit order failed: {exc}") from exc
        self._submitted_orders[cid] = result
        return result

    def cancel_order(self, order_id: str) -> None:
        try:
            self.session.trading.cancel_order_by_id(order_id)
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"cancel order failed: {exc}") from exc

    def cancel_all_orders(self) -> None:
        try:
            self.session.trading.cancel_orders()
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"cancel orders failed: {exc}") from exc

    def bars(self, symbols, timeframe="1Day", start=None, end=None, feed: str | None = None) -> dict[str, list[Bar]]:
        requested_feed = _canonical_feed(feed or self.data_feed)
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            try:
                from alpaca.data.timeframe import TimeFrameUnit
            except ImportError:
                TimeFrameUnit = None
            from alpaca.data.enums import DataFeed
            if isinstance(timeframe, str):
                normalized = timeframe.strip().lower()
                if normalized in {"1m", "1min"}:
                    timeframe = getattr(TimeFrame, "Minute", timeframe)
                elif normalized in {"1day", "1d"}:
                    timeframe = getattr(TimeFrame, "Day", timeframe)
                elif normalized == "5m" and TimeFrameUnit is not None:
                    timeframe = TimeFrame(5, TimeFrameUnit.Minute)
            request_kwargs = {"symbol_or_symbols": symbols, "timeframe": timeframe}
            if start is not None: request_kwargs["start"] = start
            if end is not None: request_kwargs["end"] = end
            # The canonical configured feed is sent on every request and
            # echoed into normalized metadata for audit/replay consumers.
            request_kwargs["feed"] = _sdk_feed(requested_feed)
            request = StockBarsRequest(**request_kwargs)
        except ImportError:
            request = {"symbols": symbols, "timeframe": timeframe, "start": start, "end": end, "feed": requested_feed}
        try:
            response = self.session.stock_data.get_stock_bars(request)
            data = getattr(response, "data", response)
            return {str(symbol).upper(): [normalize_bar(row, str(symbol), requested_feed) for row in rows] for symbol, rows in (data or {}).items()}
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"stock bars request failed: {exc}") from exc

    def quotes(self, symbols, start=None, end=None, feed: str | None = None) -> dict[str, list[Quote]]:
        requested_feed = _canonical_feed(feed or self.data_feed)
        try:
            from alpaca.data.requests import StockQuotesRequest
            from alpaca.data.enums import DataFeed
            request_kwargs = {"symbol_or_symbols": symbols, "feed": _sdk_feed(requested_feed)}
            if start is not None: request_kwargs["start"] = start
            if end is not None: request_kwargs["end"] = end
            request = StockQuotesRequest(**request_kwargs)
        except ImportError:
            request = {"symbols": symbols, "start": start, "end": end, "feed": requested_feed}
        try:
            response = self.session.stock_data.get_stock_quotes(request)
            data = getattr(response, "data", response)
            return {str(symbol).upper(): [normalize_quote(row, str(symbol), requested_feed) for row in rows] for symbol, rows in (data or {}).items()}
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"stock quotes request failed: {exc}") from exc

    def option_chain(self, underlying_symbol: str, start=None, end=None,
                     feed: str | None = None, **kwargs):
        requested_feed = _canonical_feed(feed or self.options_feed, options=True)
        try:
            # OptionHistoricalDataClient.get_option_chain in alpaca-py 0.43.5
            # takes the underlying symbol and feed as a request object only
            # via OptionChainRequest in releases that provide it.
            from alpaca.data.requests import OptionChainRequest
            request_kwargs = {"underlying_symbol": underlying_symbol,
                              "feed": _sdk_feed(requested_feed, options=True)}
            request_kwargs.update(kwargs)
            if start is not None: request_kwargs["start"] = start
            if end is not None: request_kwargs["end"] = end
            try:
                request = OptionChainRequest(**request_kwargs)
            except TypeError:
                # Older alpaca-py builds did not expose an options feed field;
                # retain the canonical metadata while using their request shape.
                request_kwargs.pop("feed", None)
                request = OptionChainRequest(**request_kwargs)
        except ImportError:
            request = {"underlying_symbol": underlying_symbol, "start": start,
                       "end": end, "feed": requested_feed, **kwargs}
        try:
            response = self.session.option_data.get_option_chain(request)
            return response.data if hasattr(response, "data") else response
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"option chain request failed: {exc}") from exc

    def option_snapshots(self, underlying_symbol: str, feed: str | None = None,
                         **kwargs) -> list[OptionSnapshot]:
        """Return normalized option snapshots when SDK data is mapping-like."""
        requested_feed = _canonical_feed(feed or self.options_feed, options=True)
        raw = self.option_chain(underlying_symbol, feed=requested_feed, **kwargs)
        if isinstance(raw, Mapping):
            rows = []
            for key, value in raw.items():
                if isinstance(value, Mapping):
                    row = dict(value); row.setdefault("symbol", key); rows.append(row)
                else:
                    rows.append(value)
        else:
            rows = raw
        result = []
        for row in rows or []:
            symbol = str(_value(row, "symbol", "")).upper()
            quote = _value(row, "latest_quote", _value(row, "quote", row))
            contract = None
            contract_source = _value(row, "contract") or row
            try:
                if _value(contract_source, "underlying_symbol") or _value(contract_source, "underlying"):
                    contract_data = dict(contract_source) if isinstance(contract_source, Mapping) else vars(contract_source)
                    contract_data.setdefault("symbol", symbol)
                    contract = OptionContract.from_sdk(contract_data)
            except (TypeError, ValueError):
                contract = None
            def dec(name):
                value = _value(quote, name)
                return Decimal(str(value)) if value is not None else None
            result.append(OptionSnapshot(symbol=symbol, contract=contract, bid=dec("bid_price"), ask=dec("ask_price"), bid_size=dec("bid_size"), ask_size=dec("ask_size"), last=dec("last_price"), timestamp=_dt(_value(quote, "timestamp", _value(row, "timestamp"))), volume=dec("volume"), open_interest=dec("open_interest"), feed=requested_feed, greeks=_value(row, "greeks", {}) or {}))
        return result

    def trade_updates(self, callback):
        """Connect an injected stream callback; reconciliation remains REST-backed."""
        stream = getattr(self.session.trading, "trade_updates", None) or getattr(self.session.trading, "stream", None)
        if stream is None:
            raise AlpacaError("trade-update stream is unavailable")
        if callable(stream):
            return stream(callback)
        subscribe = getattr(stream, "subscribe_trade_updates", None)
        if callable(subscribe):
            return subscribe(callback)
        raise AlpacaError("trade-update stream is unavailable")

    def reconcile_orders(self, client_order_ids=None) -> list[Order]:
        """REST reconciliation hook after a stream disconnect or timeout."""
        rows = self.orders()
        if client_order_ids is None:
            return rows
        wanted = set(client_order_ids)
        return [row for row in rows if row.client_order_id in wanted]

    def reconcile(self) -> dict[str, list[Any]]:
        """REST-backed startup/retry reconciliation snapshot."""
        return {"positions": self.positions(), "orders": self.orders()}


# Clearer public name for new callers; ``AlpacaProvider`` remains canonical.
AlpacaBroker = AlpacaProvider
