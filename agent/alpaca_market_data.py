"""Read-only market and options boundary for AlpacaProvider.

The mixin is intentionally provider-neutral at import time. Helper forwarders
resolve through the facade lazily so legacy monkeypatch seams remain intact.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .alpaca_domain import Bar, OptionContract, OptionSnapshot, Quote
from .alpaca_session import AlpacaError


def _facade_helper(name: str):
    # Deliberately import only at call time: alpaca_provider imports this mixin.
    from . import alpaca_provider
    return getattr(alpaca_provider, name)


def _require_executable_option_feed(feed: str) -> str:
    """Require OPRA before option data can enter an executable path.

    Alpaca's indicative stream is useful for display/diagnostics, but it is
    not a two-sided executable quote entitlement.  A candidate returned from
    this boundary is consumed by runtime sizing and therefore must never be
    sourced from that stream or silently downgraded to it.
    """
    canonical = str(feed or "").strip().lower()
    if canonical != "opra":
        raise AlpacaError(
            "indicative option data is non-executable; OPRA entitlement is "
            "required for option candidates")
    return canonical


def parse_occ_symbol(*args, **kwargs):
    return _facade_helper("parse_occ_symbol")(*args, **kwargs)


def validate_equity_symbol(*args, **kwargs):
    return _facade_helper("validate_equity_symbol")(*args, **kwargs)


def validate_option_symbol(*args, **kwargs):
    return _facade_helper("validate_option_symbol")(*args, **kwargs)


def _canonical_feed(*args, **kwargs):
    return _facade_helper("_canonical_feed")(*args, **kwargs)


def _sdk_feed(*args, **kwargs):
    return _facade_helper("_sdk_feed")(*args, **kwargs)


def _mapping(*args, **kwargs):
    return _facade_helper("_mapping")(*args, **kwargs)


def _value(*args, **kwargs):
    return _facade_helper("_value")(*args, **kwargs)


def _dt(*args, **kwargs):
    return _facade_helper("_dt")(*args, **kwargs)


def _decimal_or_none(*args, **kwargs):
    return _facade_helper("_decimal_or_none")(*args, **kwargs)


def _optional_decimal(*args, **kwargs):
    return _facade_helper("_optional_decimal")(*args, **kwargs)


def _first(*args, **kwargs):
    return _facade_helper("_first")(*args, **kwargs)


def _text(*args, **kwargs):
    return _facade_helper("_text")(*args, **kwargs)


def normalize_bar(*args, **kwargs):
    return _facade_helper("normalize_bar")(*args, **kwargs)


def normalize_quote(*args, **kwargs):
    return _facade_helper("normalize_quote")(*args, **kwargs)


def _option_row_contract(*args, **kwargs):
    return _facade_helper("_option_row_contract")(*args, **kwargs)


def _validate_finite_values(*args, **kwargs):
    return _facade_helper("_validate_finite_values")(*args, **kwargs)


class AlpacaMarketDataMixin:
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
            # A trading client that simply lacks this endpoint is a capability
            # gap, not a request failure.  Report it exactly as the ImportError
            # path below does, so option_candidates' documented chain-only
            # fallback recognizes both spellings of the same condition.
            getter = getattr(self.session.trading, "get_option_contracts", None)
            if getter is None:
                raise AlpacaError(
                    "alpaca-py option-contract request support is unavailable")
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
            # Never retry without ``feed``: an SDK signature that cannot
            # carry OPRA would otherwise silently fall back to an indicative
            # stream while the normalized row still claims executable data.
            try:
                request = OptionChainRequest(**request_kwargs)
            except TypeError as exc:
                raise AlpacaError(
                    "option chain request cannot carry the required OPRA feed: "
                    f"{exc}") from exc
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
        _require_executable_option_feed(requested_feed)
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
