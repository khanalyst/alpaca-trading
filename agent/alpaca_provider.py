"""Small injectable boundary around official :mod:`alpaca-py` clients.

No client is constructed, and no request is sent, until an authenticated
method is called.  Tests can pass fakes implementing the same methods; this is
intentional because importing alpaca-py must never be required for config and
clock checks.
"""

from __future__ import annotations

import os
import uuid
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from dataclasses import replace
from typing import Any

from .alpaca_domain import (Account, Asset, Bar, CalendarDay, MarketClock, Order,
                            OrderRequest, OptionContract, OptionSnapshot, Position,
                            Quote, parse_occ_symbol)
from .alpaca_sdk import (EQUITY_FEEDS, OPTION_FEEDS, _canonical_feed,
                          _decimal_or_none, _dt, _first, _mapping, _sdk_feed,
                          _text, _value, normalize_bar, normalize_quote)
from .alpaca_session import (AlpacaError, AlpacaSession, CredentialsError,
                             PaperModeError, normalize_calendar_day,
                             trading_env_guard)
from .instruments import (reject_crypto, validate_asset_class,
                          validate_equity_symbol, validate_instrument,
                          validate_option_symbol)


class IdempotencyConflict(AlpacaError):
    """A client order id already belongs to a different order request."""


def _finite_decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    """Parse provider numerics without allowing NaN/Infinity to cross boundary."""
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _finite_decimal(value, field)


def _validate_finite_values(value: Any, field: str) -> Any:
    """Validate nested quote/greek payloads without changing their shape."""
    if isinstance(value, Mapping):
        return {key: _validate_finite_values(item, f"{field}.{key}")
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        normalized = [_validate_finite_values(item, f"{field}[{index}]")
                      for index, item in enumerate(value)]
        return type(value)(normalized)
    if isinstance(value, Decimal) and not value.is_finite():
        raise AlpacaError(f"{field} must be finite")
    if isinstance(value, float) and not math.isfinite(value):
        raise AlpacaError(f"{field} must be finite")
    return value


def _option_row_contract(row: Any, symbol: str) -> OptionContract:
    """Build a contract while retaining and checking all explicit metadata."""
    outer = _mapping(row)
    source = _mapping(_value(row, "contract")) if _value(row, "contract") is not None else {}
    source_symbol = source.get("symbol")
    if source_symbol not in (None, "") and str(source_symbol).strip().upper() != symbol:
        raise ValueError("contract symbol does not match option row symbol")
    merged = dict(outer)
    for key, value in source.items():
        if key in merged and merged[key] not in (None, "") and value not in (None, ""):
            left = _text(merged[key]) if key in {"option_type", "right", "type", "underlying_symbol", "underlying"} else str(merged[key])
            right = _text(value) if key in {"option_type", "right", "type", "underlying_symbol", "underlying"} else str(value)
            if left.lower() != right.lower():
                raise ValueError(f"contradictory option metadata {key}")
        merged.setdefault(key, value)
    merged["symbol"] = symbol
    return OptionContract.from_sdk(merged)


_TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "done", "closed",
    "done_for_day", "replaced", "stopped", "suspended", "failed", "not_found",
}


def _order_status_matches(value: Any, requested: str | None) -> bool:
    """Apply order status filters locally when a provider ignores them."""
    if requested in (None, "", "all"):
        return True
    status = _text(value).strip()
    if requested == "open":
        return status not in _TERMINAL_ORDER_STATUSES
    if requested == "closed":
        return status in _TERMINAL_ORDER_STATUSES
    return status == requested


class AlpacaProvider:
    """Normalized account, asset, market-data and order operations."""

    def __init__(self, config: Mapping[str, Any] | None = None,
                 session: AlpacaSession | None = None, **clients: Any) -> None:
        cfg = dict(config or {})
        mode = str(cfg.get("mode") or cfg.get("broker", {}).get("mode") or "paper").lower()
        if mode == "demo":
            mode = "paper"
        if mode not in {"paper", "live"}:
            raise PaperModeError("mode must be paper or live")
        broker = cfg.get("broker") if isinstance(cfg.get("broker"), Mapping) else {}
        data_cfg = cfg.get("data") if isinstance(cfg.get("data"), Mapping) else {}
        paper_value = broker.get("paper", mode == "paper")
        allow_live_value = broker.get("allow_live", cfg.get("allow_live", False))
        if not isinstance(paper_value, bool) or not isinstance(allow_live_value, bool):
            raise ValueError("broker.paper and broker.allow_live must be booleans")
        if mode == "paper" and (paper_value is not True or allow_live_value is not False):
            raise PaperModeError("paper mode requires broker.paper=true and allow_live=false")
        if mode == "live" and (paper_value is not False or allow_live_value is not True):
            raise PaperModeError("live mode requires broker.paper=false and allow_live=true")
        try:
            trading_env_guard(paper=paper_value, allow_live=allow_live_value)
        except ValueError as exc:
            raise PaperModeError(str(exc)) from exc
        # Endpoint overrides are deliberately not accepted: TradingClient's
        # paper flag is the sole endpoint selector and is pinned to the mode.
        if broker.get("endpoint"):
            raise PaperModeError("broker endpoint overrides are disabled")
        import_env_feed = os.getenv("ALPACA_DATA_FEED") or os.getenv("ALPACA_STOCK_FEED")
        import_env_option_feed = os.getenv("ALPACA_OPTIONS_FEED")
        configured_data_feed = broker.get("data_feed") if "data_feed" in broker else data_cfg.get("feed", "iex")
        configured_options_feed = broker.get("options_feed") if "options_feed" in broker else data_cfg.get("options_feed", "indicative")
        self.data_feed = _canonical_feed(import_env_feed or configured_data_feed)
        self.options_feed = _canonical_feed(import_env_option_feed or configured_options_feed, options=True)
        paper = paper_value
        allow_live = allow_live_value
        self.session = session or AlpacaSession(
            api_key=broker.get("api_key"), secret_key=broker.get("secret_key"),
            paper=paper, allow_live=allow_live,
            trading_client=clients.get("trading_client"),
            stock_data_client=clients.get("stock_data_client"),
            option_data_client=clients.get("option_data_client"))
        if self.session.paper is not paper:
            raise PaperModeError("injected Alpaca session endpoint does not match configured mode")
        self.mode = mode
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
            is_open = _value(value, "is_open", None)
            if not isinstance(is_open, bool):
                raise ValueError("clock is_open must be true or false")
            return MarketClock(timestamp, is_open, _dt(_value(value, "next_open")), _dt(_value(value, "next_close")))
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
        asset_class = validate_asset_class(asset_class)
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
        if underlying_symbol is not None:
            underlying_symbol = validate_equity_symbol(underlying_symbol)
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
            pattern_day_trader = _value(value, "pattern_day_trader", False)
            if not isinstance(pattern_day_trader, bool):
                raise ValueError("account pattern_day_trader must be true or false")
            return Account(id=_value(value, "id"), status=_text(_value(value, "status")), equity=Decimal(str(_value(value, "equity", 0))), cash=Decimal(str(_value(value, "cash", 0))), buying_power=Decimal(str(_value(value, "buying_power", 0))), currency=_text(_value(value, "currency"), "usd").upper(), pattern_day_trader=pattern_day_trader)
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"account request failed: {exc}") from exc

    def positions(self) -> list[Position]:
        try:
            rows = self.session.trading.get_all_positions()
            result = []
            for row in rows:
                reject_crypto(row, "position")
                asset_class = _value(row, "asset_class", _value(row, "class", None))
                if asset_class is None or not _text(asset_class):
                    raise ValueError("position asset_class is required")
                symbol = validate_instrument(
                    _value(row, "symbol", ""),
                    validate_asset_class(asset_class))
                qty = _finite_decimal(_value(row, "qty", None), "position qty", positive=True)
                side = _text(_value(row, "side", None))
                if side not in {"long", "short"}:
                    raise ValueError("position side must be long or short")
                result.append(Position(
                    symbol=symbol, qty=qty, side=side,
                    market_value=_optional_decimal(_value(row, "market_value"), "position market_value"),
                    avg_entry_price=_optional_decimal(_value(row, "avg_entry_price"), "position avg_entry_price"),
                    current_price=_optional_decimal(_value(row, "current_price"), "position current_price"),
                    unrealized_pl=_optional_decimal(_value(row, "unrealized_pl"), "position unrealized_pl"),
                    raw=dict(row) if isinstance(row, Mapping) else {}))
            return result
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"positions request failed: {exc}") from exc

    def orders(self, *, status: str | None = None, client_order_id: str | None = None) -> list[Order]:
        requested_status = _text(status).strip() if status is not None else None
        try:
            # get_order_by_client_id is exact and avoids pulling an unbounded
            # order history when reconciling one id after a timeout.
            if client_order_id and hasattr(self.session.trading, "get_order_by_client_id"):
                try:
                    row = self.session.trading.get_order_by_client_id(client_order_id=client_order_id)
                except TypeError:
                    row = self.session.trading.get_order_by_client_id(client_order_id)
                # Fakes and old SDKs occasionally ignore the lookup argument;
                # never return an unrelated order as an idempotency match.
                if row is None:
                    return []
                if _value(row, "client_order_id") != client_order_id:
                    raise IdempotencyConflict(
                        f"broker returned an unrelated order for client_order_id {client_order_id!r}")
                if not _order_status_matches(_value(row, "status", ""), requested_status):
                    return []
                return [self._order(row)]
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
                try:
                    rows = self.session.trading.get_orders()
                except Exception as exc:  # noqa: BLE001
                    raise AlpacaError(f"orders request failed: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise AlpacaError(f"orders request failed: {exc}") from exc
        except TypeError:
            # Injected fakes often implement the older kwargs shape even when
            # alpaca-py is present in the test environment.
            try:
                rows = self.session.trading.get_orders(**({"status": status} if status else {}))
            except TypeError:
                try:
                    rows = self.session.trading.get_orders()
                except Exception as exc:  # noqa: BLE001
                    raise AlpacaError(f"orders request failed: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise AlpacaError(f"orders request failed: {exc}") from exc
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"orders request failed: {exc}") from exc
        rows = list(rows or [])
        if (client_order_id and len(rows) >= 500 and
                not any(_value(row, "client_order_id") == client_order_id for row in rows)):
            raise AlpacaError(
                "bounded order history is full; requested client_order_id absence is unprovable")
        # Provider-side filters are hints only. Apply exact local filtering on
        # every SDK/fallback path before normalizing untrusted rows.
        rows = [row for row in rows if _order_status_matches(
            _value(row, "status", ""), requested_status)]
        if client_order_id:
            rows = [row for row in rows if _value(row, "client_order_id") == client_order_id]
        try:
            return [self._order(row) for row in rows]
        except AlpacaError:
            raise
        except ValueError as exc:
            # Keep the historical missing-asset-class diagnostic as a
            # ValueError, while all other malformed provider rows are
            # normalized to the provider boundary error type.
            if "asset_class is required" in str(exc):
                raise
            raise AlpacaError(f"malformed broker order: {exc}") from exc

    def _order(self, row: Any) -> Order:
        reject_crypto(row, "order")
        asset_class = _value(row, "asset_class", _value(row, "class", None))
        if asset_class is None or not _text(asset_class):
            raise ValueError("order asset_class is required")
        symbol = validate_instrument(
            _value(row, "symbol", ""),
            validate_asset_class(asset_class))
        order_id = str(_value(row, "id", "") or "").strip()
        if not order_id:
            raise AlpacaError("broker order id is required")
        status = _text(_value(row, "status", None)).strip()
        if not status:
            raise AlpacaError("broker order status is required")
        qty = _finite_decimal(_value(row, "qty", None), "broker order qty", positive=True)
        filled_qty = _finite_decimal(_value(row, "filled_qty", 0), "broker order filled_qty")
        if filled_qty < 0 or filled_qty > qty:
            raise AlpacaError("broker order filled_qty must be between zero and qty")
        side = _text(_value(row, "side", None)).strip()
        if side not in {"buy", "sell"}:
            raise AlpacaError("broker order side must be buy or sell")
        order_type = _text(_value(row, "type", None)).strip()
        if order_type not in {"market", "limit"}:
            raise AlpacaError("broker order type must be market or limit")
        tif = _text(_value(row, "time_in_force", None)).strip()
        if not tif:
            raise AlpacaError("broker order time_in_force is required")
        if tif != "day":
            raise AlpacaError("broker order time_in_force must be day")
        filled_avg_price = _optional_decimal(_value(row, "filled_avg_price"), "broker order filled_avg_price")
        return Order(id=order_id, symbol=symbol, qty=qty, side=side,
                     status=status, type=order_type, time_in_force=tif,
                     client_order_id=_value(row, "client_order_id"),
                     filled_qty=filled_qty, filled_avg_price=filled_avg_price,
                     submitted_at=_dt(_value(row, "submitted_at")),
                     updated_at=_dt(_value(row, "updated_at")),
                     raw=_mapping(row))

    def _verify_existing_order(self, request: OrderRequest, existing: Order) -> None:
        """Ensure an idempotency hit is the same economic order request."""
        if existing.symbol != request.symbol or existing.qty != request.qty:
            raise IdempotencyConflict("client_order_id belongs to a different symbol or quantity")
        if existing.side != request.side or existing.type != request.type:
            raise IdempotencyConflict("client_order_id belongs to a different side or order type")
        if existing.time_in_force != request.time_in_force:
            raise IdempotencyConflict("client_order_id belongs to a different time_in_force")
        raw = existing.raw or {}
        existing_limit = _value(raw, "limit_price")
        if request.limit_price is None:
            if existing.type == "limit" and existing_limit is not None:
                # A limit request can never be equivalent to an absent limit.
                raise IdempotencyConflict("client_order_id belongs to a different limit price")
        else:
            try:
                if existing_limit is None or _finite_decimal(existing_limit, "broker order limit_price") != request.limit_price:
                    raise IdempotencyConflict("client_order_id belongs to a different limit price")
            except ValueError as exc:
                raise IdempotencyConflict("client_order_id belongs to a malformed limit price") from exc
        requested_intent = request.position_intent
        existing_intent = _text(_value(raw, "position_intent", None)) or None
        if requested_intent != existing_intent:
            raise IdempotencyConflict("client_order_id belongs to a different position intent")

    def submit_order(self, request: OrderRequest) -> Order:
        validate_instrument(request.symbol)
        if request.time_in_force != "day":
            raise AlpacaError("order time_in_force must be day")
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
            self._verify_existing_order(request, existing[0])
            self._submitted_orders[cid] = existing[0]
            return existing[0]
        try:
            from alpaca.trading.enums import OrderSide, TimeInForce
            from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
            side = OrderSide.BUY if request.side == "buy" else OrderSide.SELL
            tif = getattr(TimeInForce, request.time_in_force.upper())
            cls = MarketOrderRequest if request.type == "market" else LimitOrderRequest
            qty = int(request.qty) if request.qty == request.qty.to_integral_value() else float(request.qty)
            kwargs = {"symbol": request.symbol, "qty": qty, "side": side, "time_in_force": tif, "client_order_id": cid,
                      "extended_hours": request.extended_hours}
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
        symbols = [validate_equity_symbol(symbol) for symbol in symbols]
        requested_symbols = set(symbols)
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
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"stock bars request construction failed: {exc}") from exc
        try:
            response = self.session.stock_data.get_stock_bars(request)
            data = getattr(response, "data", response)
            result: dict[str, list[Bar]] = {}
            for key, rows in (data or {}).items():
                key_symbol = str(key).upper()
                if key_symbol not in requested_symbols:
                    continue
                normalized_rows = []
                for row in rows or []:
                    row_symbol = str(_value(row, "symbol", key_symbol) or key_symbol).upper()
                    if row_symbol not in requested_symbols:
                        continue
                    normalized_rows.append(normalize_bar(row, row_symbol, requested_feed))
                result[key_symbol] = normalized_rows
            return result
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"stock bars request failed: {exc}") from exc

    def quotes(self, symbols, start=None, end=None, feed: str | None = None) -> dict[str, list[Quote]]:
        symbols = [validate_equity_symbol(symbol) for symbol in symbols]
        requested_symbols = set(symbols)
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
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"stock quotes request construction failed: {exc}") from exc
        try:
            response = self.session.stock_data.get_stock_quotes(request)
            data = getattr(response, "data", response)
            result: dict[str, list[Quote]] = {}
            for key, rows in (data or {}).items():
                key_symbol = str(key).upper()
                if key_symbol not in requested_symbols:
                    continue
                normalized_rows = []
                for row in rows or []:
                    row_symbol = str(_value(row, "symbol", key_symbol) or key_symbol).upper()
                    if row_symbol not in requested_symbols:
                        continue
                    normalized_rows.append(normalize_quote(row, row_symbol, requested_feed))
                result[key_symbol] = normalized_rows
            return result
        except AlpacaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"stock quotes request failed: {exc}") from exc

    def option_chain(self, underlying_symbol: str, start=None, end=None,
                     feed: str | None = None, **kwargs):
        underlying_symbol = validate_equity_symbol(underlying_symbol)
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
            request = None
            # alpaca-py has shipped several OptionChainRequest signatures.
            # Retry only compatible shape reductions; never leak constructor
            # TypeError to callers.
            attempts = [dict(request_kwargs)]
            for drop in (("feed",), ("feed", "start"), ("feed", "end"),
                         ("feed", "start", "end"), ("start", "end")):
                candidate = {k: v for k, v in request_kwargs.items() if k not in drop}
                if candidate not in attempts:
                    attempts.append(candidate)
            last_error = None
            for candidate in attempts:
                try:
                    request = OptionChainRequest(**candidate)
                    break
                except TypeError as exc:
                    last_error = exc
            if request is None:
                raise AlpacaError(f"option chain request construction failed: {last_error}")
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
        underlying_symbol = validate_equity_symbol(underlying_symbol)
        requested_feed = _canonical_feed(feed or self.options_feed, options=True)
        raw = self.option_chain(underlying_symbol, feed=requested_feed, **kwargs)
        if isinstance(raw, Mapping):
            rows = []
            for key, value in raw.items():
                row = _mapping(value)
                row.setdefault("symbol", key)
                rows.append(row)
        else:
            rows = raw
        result = []
        for row in rows or []:
            row = _mapping(row)
            contract_value = _value(row, "contract")
            symbol_value = _value(row, "symbol") or _value(contract_value, "symbol")
            symbol = str(symbol_value or "").strip().upper()
            if not symbol or symbol == "BAD":
                continue
            occ = parse_occ_symbol(symbol)
            if occ is not None and occ["underlying_symbol"] != underlying_symbol:
                continue
            explicit_underlying = _value(row, "underlying_symbol", _value(row, "underlying"))
            if explicit_underlying is not None and str(explicit_underlying).strip().upper() != underlying_symbol:
                continue
            contract = None
            try:
                contract = _option_row_contract(row, symbol)
            except (TypeError, ValueError) as exc:
                # Snapshot rows must carry a listed OCC contract identity;
                # malformed/quote-only keys are not eligible evidence.
                if occ is not None and ("decimal" in str(exc).lower() or
                                        "expiration" in str(exc).lower()):
                    raise AlpacaError(f"option snapshot contract is invalid: {exc}") from exc
                continue
            quote = _mapping(_value(row, "latest_quote", _value(row, "quote", row)))
            def dec(name):
                try:
                    return _optional_decimal(_value(quote, name), f"option {name}")
                except ValueError as exc:
                    raise AlpacaError(f"option snapshot {name} is invalid: {exc}") from exc
            timestamp_value = _value(quote, "timestamp", _value(row, "timestamp"))
            timestamp = _dt(timestamp_value)
            if timestamp_value is not None:
                if timestamp is None:
                    raise AlpacaError("option snapshot timestamp is invalid")
                if timestamp.tzinfo is None:
                    raise AlpacaError("option snapshot timestamp must be timezone-aware")
            result.append(OptionSnapshot(
                symbol=symbol, contract=contract, bid=dec("bid_price") if dec("bid_price") is not None else dec("bid"),
                ask=dec("ask_price") if dec("ask_price") is not None else dec("ask"),
                bid_size=dec("bid_size"), ask_size=dec("ask_size"),
                last=dec("last_price") if dec("last_price") is not None else dec("last"),
                timestamp=timestamp,
                volume=dec("volume") if dec("volume") is not None else dec("day_volume"),
                open_interest=dec("open_interest") if dec("open_interest") is not None else dec("oi"),
                feed=requested_feed,
                greeks=_validate_finite_values(_value(row, "greeks", {}) or {}, "option greeks"),
                underlying_price=dec("underlying_price") if dec("underlying_price") is not None else dec("underlying_last")))
        return result

    def option_candidates(self, underlying_symbol: str, *, now: datetime | float | None = None,
                          underlying_price: Decimal | float | None = None,
                          feed: str | None = None, min_dte: int | None = None,
                          max_dte: int | None = None, **kwargs) -> list[dict[str, Any]]:
        """Return auditable, risk-ready single-leg option candidates.

        The trading API's option-contract endpoint is the source of truth for
        identity (expiry, strike, right, multiplier).  The option-chain
        endpoint contributes the latest quote, volume/open-interest and quote
        timestamp.  Responses that contain only OCC symbols are parsed by
        :func:`parse_occ_symbol`; this keeps malformed or incomplete rows
        from being silently treated as eligible contracts.

        ``quote_age_seconds`` is measured against ``now`` (UTC) and is kept in
        the returned mapping so risk can fail closed on stale/future quotes.
        No order is submitted by this method.
        """
        underlying = validate_equity_symbol(underlying_symbol)
        requested_feed = _canonical_feed(feed or self.options_feed, options=True)
        if now is None:
            now_dt = datetime.now(timezone.utc)
        elif isinstance(now, datetime):
            if now.tzinfo is None:
                raise AlpacaError("option candidate now must be timezone-aware")
            now_dt = now
            now_dt = now_dt.astimezone(timezone.utc)
        else:
            try:
                value = float(now)
                if not math.isfinite(value):
                    raise ValueError("timestamp must be finite")
                if abs(value) > 100_000_000_000:
                    value /= 1000.0
                now_dt = datetime.fromtimestamp(value, timezone.utc)
            except (TypeError, ValueError, OverflowError) as exc:
                raise AlpacaError(f"option candidate now is invalid: {exc}") from exc

        metadata: dict[str, OptionContract] = {}
        try:
            contracts = self.option_contracts(underlying, **kwargs)
        except AlpacaError as exc:
            # Some paper fakes and older SDK builds expose only the chain. An
            # explicit capability error is the sole compatible fallback;
            # real provider/request failures remain fatal.
            if "request support is unavailable" not in str(exc).lower():
                raise
            contracts = []
        for contract in contracts:
            if (isinstance(contract, OptionContract) and
                    (not contract.underlying_symbol or contract.underlying_symbol == underlying)):
                metadata[contract.symbol.upper()] = contract

        # Fetch once; option_snapshots remains a convenient public normalized
        # view, but doing the flattening here avoids a second network request.
        raw = self.option_chain(underlying, feed=requested_feed, **kwargs)
        if isinstance(raw, Mapping):
            rows = []
            for key, value in raw.items():
                row = _mapping(value); row.setdefault("symbol", key); rows.append(row)
        else:
            rows = [_mapping(value) for value in (raw or [])]

        if underlying_price is None:
            for row in rows:
                value = (_value(_value(row, "latest_quote", _value(row, "quote", row)), "underlying_price") or
                         _value(row, "underlying_price") or _value(row, "underlying_last"))
                try:
                    underlying_price = _decimal_or_none(value)
                except ValueError as exc:
                    raise AlpacaError(f"option underlying price is invalid: {exc}") from exc
                if underlying_price is not None:
                    break
        try:
            spot = _decimal_or_none(underlying_price)
        except ValueError as exc:
            raise AlpacaError(f"option underlying price is invalid: {exc}") from exc
        candidates: list[dict[str, Any]] = []
        for raw_row in rows:
            row = _mapping(raw_row)
            contract_value = _value(row, "contract")
            symbol_value = _value(row, "symbol") or _value(contract_value, "symbol")
            symbol = str(symbol_value or "").strip().upper()
            if not symbol or symbol == "BAD":
                continue
            try:
                symbol = validate_option_symbol(symbol, underlying)
            except ValueError:
                continue
            metadata_contract = metadata.get(symbol)
            try:
                row_contract = _option_row_contract(row, symbol)
            except (TypeError, ValueError) as exc:
                # Contradictory explicit metadata is never repaired from OCC;
                # drop this row rather than selecting the wrong leg.
                if "decimal" in str(exc).lower() or "expiration" in str(exc).lower():
                    raise AlpacaError(f"option candidate contract is invalid: {exc}") from exc
                continue
            contract = metadata_contract or row_contract
            if metadata_contract is not None:
                if any((metadata_contract.underlying_symbol != row_contract.underlying_symbol,
                        metadata_contract.expiration_date != row_contract.expiration_date,
                        metadata_contract.strike_price != row_contract.strike_price,
                        metadata_contract.option_type != row_contract.option_type,
                        metadata_contract.contract_size != row_contract.contract_size)):
                    continue
            if contract.underlying_symbol and contract.underlying_symbol != underlying:
                continue
            quote = _mapping(_value(row, "latest_quote", _value(row, "quote", row)))
            daily_bar = _mapping(_value(row, "daily_bar", {}))
            timestamp_value = (_first(quote, "timestamp") if _first(quote, "timestamp") is not None
                               else _first(row, "timestamp"))
            timestamp = _dt(timestamp_value)
            if timestamp_value is None:
                continue
            if timestamp_value is not None:
                if timestamp is None:
                    raise AlpacaError("option candidate timestamp is invalid")
                if timestamp.tzinfo is None:
                    raise AlpacaError("option candidate timestamp must be timezone-aware")
            age = None if timestamp is None else (now_dt - timestamp).total_seconds()
            bid_raw = _first(quote, "bid_price", "bid")
            ask_raw = _first(quote, "ask_price", "ask")
            last_raw = _first(quote, "last_price", "last")
            volume_raw = _first(quote, "volume", "day_volume")
            oi_raw = _first(quote, "open_interest", "oi")
            if bid_raw is None: bid_raw = _first(row, "bid_price", "bid")
            if ask_raw is None: ask_raw = _first(row, "ask_price", "ask")
            if last_raw is None: last_raw = _first(row, "last_price", "last")
            if volume_raw is None: volume_raw = _first(row, "volume", "day_volume")
            if volume_raw is None: volume_raw = _first(daily_bar, "volume", "v")
            if oi_raw is None: oi_raw = _first(row, "open_interest", "oi")
            if oi_raw is None: oi_raw = contract.open_interest
            try:
                bid = _decimal_or_none(bid_raw)
                ask = _decimal_or_none(ask_raw)
                last = _decimal_or_none(last_raw)
                volume = _decimal_or_none(volume_raw)
                open_interest = _decimal_or_none(oi_raw)
                bid_size = _decimal_or_none(_first(quote, "bid_size") if _first(quote, "bid_size") is not None else _first(row, "bid_size"))
                ask_size = _decimal_or_none(_first(quote, "ask_size") if _first(quote, "ask_size") is not None else _first(row, "ask_size"))
            except ValueError as exc:
                raise AlpacaError(f"option candidate decimal is invalid: {exc}") from exc
            expiry = contract.expiration_date
            dte = None if expiry is None else (expiry - now_dt.date()).days
            row_out: dict[str, Any] = {
                "symbol": contract.symbol, "underlying_symbol": contract.underlying_symbol or underlying,
                "underlying": contract.underlying_symbol or underlying,
                "expiration": expiry.isoformat() if expiry else None,
                "expiration_date": expiry, "dte": dte,
                "strike": contract.strike_price, "strike_price": contract.strike_price,
                "type": contract.option_type, "right": contract.option_type,
                "option_type": contract.option_type, "multiplier": contract.multiplier,
                "contract_size": contract.contract_size,
                "bid": bid, "ask": ask, "last": last,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "quote_ts": timestamp, "timestamp": timestamp,
                "quote_age_seconds": age, "quote_stale": age is None or age < 0,
                "volume": volume, "open_interest": open_interest,
                "feed": requested_feed, "side": "buy", "strategy": "single",
                "position_intent": "buy_to_open",
            }
            if spot is not None and contract.strike_price is not None and spot > 0:
                distance = abs(float(contract.strike_price) - float(spot)) / float(spot)
                row_out["underlying_price"] = spot
                row_out["moneyness"] = float(contract.strike_price) / float(spot)
                row_out["moneyness_distance"] = distance
            candidates.append(row_out)
        if min_dte is not None:
            candidates = [row for row in candidates if row.get("dte") is not None and row["dte"] >= int(min_dte)]
        if max_dte is not None:
            candidates = [row for row in candidates if row.get("dte") is not None and row["dte"] <= int(max_dte)]
        return candidates

    def close_position(self, symbol: str, qty: Decimal | float | None = None, *,
                       client_order_id: str | None = None,
                       order_type: str = "market", time_in_force: str = "day") -> Order | None:
        """Submit one normalized closing order for an existing long position.

        The hook intentionally refuses short options and never uses the SDK's
        untyped ``close_position`` shortcut: an OCC symbol receives the
        explicit ``sell_to_close`` intent while stock positions receive a
        normal sell.  The caller can reconcile the returned order exactly as
        any other :class:`Order`.
        """
        wanted = validate_instrument(symbol)
        if str(time_in_force).lower() != "day":
            raise ValueError("close position time_in_force must be day")
        held = next((position for position in self.positions()
                     if str(position.symbol).upper() == wanted), None)
        if held is None:
            return None
        if qty is None:
            position_qty = held.qty
        else:
            try:
                position_qty = _finite_decimal(qty, "close position qty", positive=True)
            except ValueError as exc:
                raise AlpacaError(str(exc)) from exc
        if position_qty > held.qty:
            raise AlpacaError("close position qty exceeds held quantity")
        side = str(held.side).lower()
        if side == "long":
            close_side = "sell"
        elif side == "short":
            if parse_occ_symbol(wanted) is not None:
                raise AlpacaError("short option positions cannot be closed by the long-only hook")
            close_side = "buy"
        else:
            raise AlpacaError("held position side must be long or short")
        if parse_occ_symbol(wanted) is not None:
            intent = "sell_to_close"
        else:
            intent = None
        request = OrderRequest(wanted, position_qty, close_side, type=order_type,
                               time_in_force=time_in_force,
                               client_order_id=client_order_id,
                               position_intent=intent)
        return self.submit_order(request)

    def reconcile(self) -> dict[str, list[Any]]:
        """REST-backed startup/retry reconciliation snapshot."""
        return {"positions": self.positions(), "orders": self.orders(status="all")}
