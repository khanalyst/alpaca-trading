"""Small injectable boundary around official :mod:`alpaca-py` clients.

No client is constructed, and no request is sent, until an authenticated
method is called.  Tests can pass fakes implementing the same methods; this is
intentional because importing alpaca-py must never be required for config and
clock checks.
"""

from __future__ import annotations

import json
import math
import os
import uuid
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
from .alpaca_market_data import AlpacaMarketDataMixin
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


_PROTECTIVE_LEG_TYPES = {"limit", "stop", "stop_limit"}
# Submission still rejects stop orders (see OrderRequest); these are the types
# a broker reports back for the child legs of a bracket the broker owns.
_BROKER_ORDER_TYPES = {"market", "limit", "stop", "stop_limit"}

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


def _order_lookup_not_found(exc: Exception) -> bool:
    """Recognize Alpaca's exact missing-order response without hiding 404s."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status_code", None)
    message = str(getattr(exc, "message", "") or "")
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    for value in getattr(exc, "args", ()):
        if isinstance(value, Mapping):
            code = value.get("code", code)
            message = str(value.get("message", message) or message)
    try:
        payload = json.loads(str(exc))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, Mapping):
        code = payload.get("code", code)
        message = str(payload.get("message", message) or message)
    if str(code or "") == "40410000":
        return True
    return str(status or "") == "404" and "order not found" in message.lower()


class AlpacaProvider(AlpacaMarketDataMixin):
    """Normalized account, asset, market-data and order operations."""

    def __init__(self, config: Mapping[str, Any] | None = None,
                 session: AlpacaSession | None = None, **clients: Any) -> None:
        cfg = dict(config or {})
        mode = str(cfg.get("mode") or cfg.get("broker", {}).get("mode") or "paper").lower()
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


    def account(self) -> Account:
        try:
            value = self.session.trading.get_account()
            pattern_day_trader = _value(value, "pattern_day_trader", False)
            # Alpaca removed this field from GET /v2/account on 2026-07-06.
            # Treat an omitted/null value as the conservative paper-account
            # default while retaining strict validation for malformed values.
            if pattern_day_trader is None:
                pattern_day_trader = False
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
                    try:
                        row = self.session.trading.get_order_by_client_id(
                            client_order_id=client_order_id)
                    except TypeError:
                        row = self.session.trading.get_order_by_client_id(
                            client_order_id)
                except Exception as exc:  # noqa: BLE001
                    if _order_lookup_not_found(exc):
                        return []
                    raise
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
        if order_type not in _BROKER_ORDER_TYPES:
            raise AlpacaError("broker order type must be market, limit, stop or stop_limit")
        tif = _text(_value(row, "time_in_force", None)).strip()
        if not tif:
            raise AlpacaError("broker order time_in_force is required")
        if tif != "day":
            raise AlpacaError("broker order time_in_force must be day")
        filled_avg_price = _optional_decimal(_value(row, "filled_avg_price"), "broker order filled_avg_price")
        raw_legs = _value(row, "legs", None) or ()
        if isinstance(raw_legs, Mapping):
            raw_legs = [raw_legs]
        return Order(id=order_id, symbol=symbol, qty=qty, side=side,
                     status=status, type=order_type, time_in_force=tif,
                     client_order_id=_value(row, "client_order_id"),
                     filled_qty=filled_qty, filled_avg_price=filled_avg_price,
                     submitted_at=_dt(_value(row, "submitted_at")),
                     updated_at=_dt(_value(row, "updated_at")),
                     legs=tuple(self._order_leg(leg) for leg in raw_legs),
                     raw=_mapping(row))

    def _order_leg(self, row: Any) -> dict[str, Any]:
        """Normalize one bracket child leg; no SDK object crosses upward."""
        leg_id = str(_value(row, "id", "") or "").strip()
        if not leg_id:
            raise AlpacaError("broker order leg id is required")
        status = _text(_value(row, "status", None)).strip()
        if not status:
            raise AlpacaError("broker order leg status is required")
        leg_type = _text(_value(row, "type", None)).strip()
        if leg_type not in _PROTECTIVE_LEG_TYPES:
            raise AlpacaError("broker order leg type must be limit, stop or stop_limit")
        side = _text(_value(row, "side", None)).strip()
        if side not in {"buy", "sell"}:
            raise AlpacaError("broker order leg side must be buy or sell")
        # A stop-priced leg is the protective stop; the remaining limit leg is
        # the take-profit target.  The role is what execution reasons about.
        stop_price = _optional_decimal(_value(row, "stop_price"), "broker order leg stop_price")
        return {
            "id": leg_id,
            "symbol": validate_instrument(_value(row, "symbol", "")),
            "side": side, "type": leg_type, "status": status,
            "role": "stop" if leg_type in {"stop", "stop_limit"} or stop_price is not None else "target",
            "qty": _optional_decimal(_value(row, "qty"), "broker order leg qty"),
            "filled_qty": _optional_decimal(_value(row, "filled_qty"),
                                            "broker order leg filled_qty"),
            "filled_avg_price": _optional_decimal(
                _value(row, "filled_avg_price"), "broker order leg filled_avg_price"),
            "limit_price": _optional_decimal(_value(row, "limit_price"),
                                             "broker order leg limit_price"),
            "stop_price": stop_price,
        }

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
        existing_class = _text(_value(raw, "order_class", None)).lower() or "simple"
        if (request.order_class or "simple") != existing_class:
            raise IdempotencyConflict("client_order_id belongs to a different order class")
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
            if request.order_class == "bracket":
                from alpaca.trading.enums import OrderClass
                from alpaca.trading.requests import StopLossRequest, TakeProfitRequest
                kwargs["order_class"] = OrderClass.BRACKET
                kwargs["take_profit"] = TakeProfitRequest(limit_price=float(request.take_profit))
                kwargs["stop_loss"] = StopLossRequest(stop_price=float(request.stop_loss))
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
