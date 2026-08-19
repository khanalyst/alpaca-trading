"""Market-entry collection and risk helpers for the orchestration engine.

This mixin intentionally contains the engine's market-entry/risk seam without
importing Engine, keeping the facade's dependency graph acyclic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping

from . import state
from .alpaca_domain import OrderRequest
from .alpaca_provider import AlpacaError
from .execution_lifecycle import _plain, _value
from .instruments import validate_equity_symbol

log = logging.getLogger("engine")


def _equity_price_increment(price: Decimal) -> Decimal:
    """Return Alpaca's equity sub-penny increment for one price."""
    return Decimal("0.01") if price >= Decimal("1") else Decimal("0.0001")


def _quantize_equity_price(value: Any, *, rounding: str) -> Decimal:
    price = Decimal(str(value))
    if not price.is_finite() or price <= 0:
        raise ValueError("equity order price must be finite and positive")
    rounded = price.quantize(_equity_price_increment(price), rounding=rounding)
    # A sub-dollar price can cross the $1 boundary when rounded upward.  Its
    # resulting broker-bound representation must then obey the two-decimal
    # increment required at or above $1 rather than retaining four decimals.
    if price < Decimal("1") <= rounded:
        rounded = rounded.quantize(Decimal("0.01"), rounding=rounding)
    return rounded


def _quantize_equity_bracket(decision: Mapping[str, Any]) -> dict[str, Any]:
    """Move bracket legs toward entry onto broker-valid equity price ticks.

    Rounding toward entry is deliberately conservative: it cannot increase the
    authored stop risk or overstate the attainable target.  Risk sizing then
    consumes these exact broker-bound prices rather than a higher-precision
    geometry the broker would reject or normalize differently.
    """
    out = dict(decision)
    if out.get("stop_price") is None or out.get("target_price") is None:
        return out
    direction = str(out.get("direction") or "").lower()
    try:
        entry = Decimal(str(out.get("entry_price")))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError("equity entry price is unavailable for tick rounding") from exc
    if not entry.is_finite() or entry <= 0:
        raise ValueError("equity entry price is unavailable for tick rounding")
    if direction == "long":
        stop = _quantize_equity_price(out["stop_price"], rounding=ROUND_CEILING)
        target = _quantize_equity_price(out["target_price"], rounding=ROUND_FLOOR)
        valid = stop < entry < target
    elif direction == "short":
        stop = _quantize_equity_price(out["stop_price"], rounding=ROUND_FLOOR)
        target = _quantize_equity_price(out["target_price"], rounding=ROUND_CEILING)
        valid = target < entry < stop
    else:
        raise ValueError("equity direction is unavailable for tick rounding")
    if not valid:
        raise ValueError("tick-rounded bracket legs do not straddle entry")
    distance = abs(entry - stop)
    out.update(stop_price=float(stop), target_price=float(target),
               stop_distance=float(distance),
               stop_loss_pct=float(distance / entry * Decimal("100")),
               take_profit_pct=float(abs(target - entry) / entry * Decimal("100")))
    return out


def _equity_entry_reference(decision: Mapping[str, Any], row: Mapping[str, Any]) -> float | None:
    """Return the current executable quote side or the authored fallback.

    Some signal producers author absolute stop/target legs without repeating
    an entry price.  Risk sizing has always allowed the market snapshot to
    supply that price; tick rounding must use the same fail-closed runtime
    evidence instead of rejecting the signal before risk vetting.
    """
    quote = row.get("quote") if isinstance(row, Mapping) else None
    direction = str(decision.get("direction") or "").lower()
    if isinstance(quote, Mapping) and direction in {"long", "short"}:
        raw = (quote.get("ask", quote.get("ask_price")) if direction == "long" else
               quote.get("bid", quote.get("bid_price")))
        try:
            rounded = _quantize_equity_price(
                raw, rounding=(ROUND_CEILING if direction == "long" else ROUND_FLOOR))
            return float(rounded)
        except (TypeError, ValueError, ArithmeticError, OverflowError):
            pass
    authored = decision.get("entry_price")
    try:
        value = float(authored)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value == value and abs(value) != float("inf") and value > 0 else None


class MarketEntryRiskMixin:
    def _event(self, kind: str, payload: Mapping | str) -> None:
        try:
            state.log_event(kind, json.dumps(_plain(payload), sort_keys=True, default=str))
        except Exception:  # noqa: BLE001
            log.debug("could not journal %s", kind, exc_info=True)

    @staticmethod
    def _bar_mapping(bar: Any, symbol: str) -> dict:
        row = _plain(bar)
        if not isinstance(row, Mapping):
            return {}
        out = dict(row)
        out["symbol"] = validate_equity_symbol(out.get("symbol") or symbol)
        return out

    @staticmethod
    def _quote_mapping(quote: Any, symbol: str) -> dict:
        row = _plain(quote)
        result = dict(row) if isinstance(row, Mapping) else {"symbol": symbol}
        result["symbol"] = validate_equity_symbol(result.get("symbol") or symbol)
        return result

    def _universe(self) -> list[str]:
        universe = self.cfg.get("universe", {})
        rows = universe.get("symbols", []) if isinstance(universe, Mapping) else []
        deny = {validate_equity_symbol(item) for item in universe.get("denylist", [])} if isinstance(universe, Mapping) else set()
        symbols = [validate_equity_symbol(symbol) for symbol in rows]
        return [symbol for symbol in symbols if symbol not in deny][
            :int(universe.get("max_symbols", 50) or 50)]

    def _collect(self, symbols: list[str], now: datetime, supplied: Mapping | None):
        """Fetch only configured symbols and completed one-minute bars."""
        if self._timestamp(now) is None:
            return {}
        if supplied is not None:
            if not isinstance(supplied, Mapping):
                return {}
            raw = supplied
            if any(symbol in raw for symbol in symbols):
                rows = {symbol: raw.get(symbol, {}) for symbol in symbols}
            else:
                rows = {symbol: raw for symbol in symbols}
        else:
            end = now
            # The IBR contract always needs the completed opening range, even
            # late in the session.  A rolling two-hour window silently made
            # every afternoon cycle incapable of producing a signal.
            session = self.market.session(now)
            start = session.open if session is not None else now - timedelta(hours=8)
            fetched_bars = self.market.stock_bars(symbols, timeframe="1m", start=start, end=end)
            fetched_quotes = self.market.stock_quotes(symbols, start=now - timedelta(minutes=2), end=end)
            rows = {
                symbol: {"bars": fetched_bars.get(symbol, []), "quotes": fetched_quotes.get(symbol, [])}
                for symbol in symbols
            }
        max_age = float(self.cfg.get("execution", {}).get("max_market_data_age_seconds", 30))
        result = {}
        for symbol in symbols:
            raw_row = rows.get(symbol, {}) if isinstance(rows, Mapping) else {}
            if not isinstance(raw_row, Mapping):
                continue
            row = dict(raw_row)
            # Freshness flags are safety-critical.  Treating a string such as
            # ``"false"`` as valid would allow an untrusted row through.
            invalid_freshness = False
            for flag in ("stale", "quote_stale"):
                if flag in row and not isinstance(row.get(flag), bool):
                    invalid_freshness = True
                    break
            if invalid_freshness or row.get("stale") is True or row.get("quote_stale") is True:
                continue
            bars = row.get("bars") or row.get("candles") or []
            quotes = row.get("quotes") or row.get("quote") or []
            if isinstance(quotes, (str, bytes)) or not isinstance(quotes, (list, tuple, set)):
                quotes = [quotes]
            elif isinstance(quotes, set):
                quotes = list(quotes)
            try:
                normalized_bars = [self._bar_mapping(item, symbol) for item in (bars or [])]
            except (TypeError, ValueError, AttributeError):
                # One malformed symbol/bar row must not abort the cycle.
                continue
            normalized_bars = [item for item in normalized_bars
                               if item.get("symbol") == symbol and
                               self._timestamp(item.get("timestamp")) is not None]
            normalized_bars.sort(
                key=lambda item: self._timestamp(item.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc))
            normalized_bars = [item for item in normalized_bars if self._completed(item, now)]
            normalized_quotes = []
            for item in quotes or []:
                try:
                    quote_item = self._quote_mapping(item, symbol)
                except (TypeError, ValueError, AttributeError):
                    continue
                if quote_item.get("symbol") != symbol:
                    continue
                quote_flags_invalid = any(
                    flag in quote_item and not isinstance(quote_item.get(flag), bool)
                    for flag in ("stale", "quote_stale"))
                if quote_flags_invalid or quote_item.get("stale") is True or quote_item.get("quote_stale") is True:
                    continue
                quote_ts = self._timestamp(quote_item.get("timestamp"))
                if quote_ts is None:
                    continue
                normalized_quotes.append((quote_ts, quote_item))
            # Provider order is not an ordering contract; choose the newest
            # valid, aware quote rather than trusting the final list item.
            if not normalized_quotes:
                continue
            _quote_ts, quote = max(normalized_quotes, key=lambda item: item[0])
            quote_ts = self._timestamp(quote.get("timestamp"))
            # Future-dated quotes are look-ahead and must be rejected rather
            # than having their negative age clamped to zero below.
            if quote_ts is None or quote_ts > now or (now - quote_ts).total_seconds() > max_age:
                continue
            bid = self._number(quote.get("bid_price", quote.get("bid")))
            ask = self._number(quote.get("ask_price", quote.get("ask")))
            if bid is None or ask is None or bid <= 0 or ask < bid:
                continue
            spread_bps = (ask - bid) / ((ask + bid) / 2) * 10000
            age = max(0.0, (now - quote_ts).total_seconds())
            for item in normalized_bars:
                item["bid"] = bid; item["ask"] = ask
                item["spread_bps"] = spread_bps; item["data_age_seconds"] = age
            row["bars"] = normalized_bars
            row["quote"] = quote
            row["spread_bps"] = spread_bps
            row["data_age_seconds"] = age
            row["stale"] = False
            row["quote_stale"] = False
            result[symbol] = row
        return result

    @staticmethod
    def _number(value):
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
            return number if number == number and abs(number) != float("inf") else None
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def _required_number(cls, value: Any, field: str) -> float:
        number = cls._number(value)
        if number is None:
            raise AlpacaError(f"{field} measurement is malformed")
        return number

    @staticmethod
    def _timestamp(value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                return None
            return value
        if isinstance(value, bool):
            return None
        try:
            text = str(value).replace("Z", "+00:00")
            try:
                number = float(text)
            except (TypeError, ValueError):
                number = None
            if number is not None and number == number and abs(number) != float("inf"):
                number /= 1000 if abs(number) > 100_000_000_000 else 1
                return datetime.fromtimestamp(number, timezone.utc)
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    def _wall_clock(self) -> datetime:
        return datetime.now(timezone.utc)

    def _validated_clock_timestamp(self, clock: Any) -> datetime:
        timestamp = self._timestamp(_value(clock, "timestamp", None))
        if timestamp is None:
            raise AlpacaError("broker clock timestamp is missing")
        current = self._wall_clock()
        age = (current - timestamp.astimezone(timezone.utc)).total_seconds()
        maximum = float(self._edge_base_cfg.get("execution", {}).get(
            "max_market_data_age_seconds", 30) or 30)
        if age < 0:
            raise AlpacaError("broker clock timestamp is in the future")
        if age > maximum:
            raise AlpacaError("broker clock timestamp is stale")
        return timestamp

    def _completed(self, bar: Mapping, now: datetime) -> bool:
        ts = self._timestamp(bar.get("timestamp"))
        return bool(ts is not None and ts + timedelta(minutes=1) <= now and ts <= now)

    def _llm_allows(self, signal: Mapping, snapshot: Mapping, portfolio: Mapping) -> bool:
        if self.brain is None:
            return True
        try:
            response = self.brain.decide({"signals": [_plain(signal)], "snapshot": _plain(snapshot)}, _plain(portfolio))
        except Exception as exc:  # noqa: BLE001
            self._event("llm_veto", {"symbol": signal.get("symbol"), "reason": str(exc)})
            return False
        decisions = response.get("decisions", []) if isinstance(response, Mapping) else []
        matching = [row for row in decisions if isinstance(row, Mapping) and str(row.get("symbol", "")).upper() == str(signal.get("symbol", "")).upper()]
        for row in matching:
            action = str(row.get("action", "hold")).lower()
            if row.get("veto") is True or action in {"hold", "close", "reject", "veto"}:
                return False
            expected = "buy" if signal.get("direction") == "long" else "sell"
            if action in {"buy", "sell"} and action != expected:
                return False
        return True

    def _entry_execution(self, side: str, quote: Mapping,
                         reference_price: Any) -> tuple[str, str, Decimal | None]:
        """Apply configured entry type/TIF and reject excessive quote slippage."""
        execution = self.cfg.get("execution", {})
        order_type = str(execution.get("order_type", "market")).lower()
        time_in_force = str(execution.get("time_in_force", "day")).lower()
        bid = self._number(quote.get("bid", quote.get("bid_price")))
        ask = self._number(quote.get("ask", quote.get("ask_price")))
        executable = ask if side == "buy" else bid
        reference = self._number(reference_price)
        if executable is None or executable <= 0 or reference is None or reference <= 0:
            raise ValueError("entry quote or reference price is unavailable")
        adverse = max(0.0, executable - reference) if side == "buy" else max(0.0, reference - executable)
        slippage_bps = adverse / reference * 10_000.0
        maximum = float(execution.get("max_slippage_bps", 50) or 0)
        if slippage_bps > maximum:
            raise ValueError(
                f"quoted entry slippage {slippage_bps:.2f} bps exceeds {maximum:.2f} bps")
        limit_price = Decimal(str(executable)) if order_type == "limit" else None
        return order_type, time_in_force, limit_price

    def _update_daily_risk(self, account: Any, now: datetime) -> tuple[float, bool]:
        """Persist day-start equity and enforce the configured daily loss cap."""
        equity = self._number(_value(account, "equity", None))
        if equity is None or equity <= 0:
            raise AlpacaError("account equity measurement is unavailable")
        session = self.market.session(now)
        day = session.date.isoformat() if session is not None else now.date().isoformat()
        limit_pct = self._number(self.cfg.get("risk", {}).get("daily_loss_limit_pct"))
        outcome: dict[str, Any] = {}
        def update(current: dict) -> dict:
            prior = current.get("risk_day", {})
            if prior in (None, {}):
                prior = {"date": day, "start_equity": equity}
            elif not isinstance(prior, Mapping):
                raise AlpacaError("durable risk_day state is malformed")
            else:
                prior_date = prior.get("date")
                if not isinstance(prior_date, str):
                    raise AlpacaError("same-day risk date is malformed")
                try:
                    parsed_date = date.fromisoformat(prior_date)
                except (TypeError, ValueError):
                    raise AlpacaError("same-day risk date is malformed") from None
                if parsed_date.isoformat() != prior_date:
                    raise AlpacaError("same-day risk date is malformed")
                if prior_date != day:
                    prior = {"date": day, "start_equity": equity}
            if isinstance(prior, Mapping) and prior.get("date") == day:
                # A same-day baseline is durable risk evidence.  Replacing a
                # malformed value with current equity would erase a loss and
                # silently bypass the daily stop.
                start = self._number(prior.get("start_equity"))
                if start is None or start <= 0:
                    raise AlpacaError("same-day risk baseline is malformed")
            start = self._number(prior.get("start_equity"))
            if start is None or start <= 0:
                start = equity
            daily_pnl = equity - start
            hit = bool(limit_pct is not None and
                       daily_pnl <= -(start * limit_pct / 100.0))
            current["risk_day"] = {
                "date": day, "start_equity": start, "current_equity": equity,
                "daily_pnl": daily_pnl, "updated_ts": time.time(),
                "limit_hit": hit,
            }
            if hit:
                current["state"] = state.DAY_STOPPED
            elif current.get("state") == state.DAY_STOPPED:
                current["state"] = state.RUNNING if self.running else state.PAUSED
            outcome.update(daily_pnl=daily_pnl, hit=hit)
            return current
        current = state.update_state(update)
        state.log_equity(
            equity, "running", runtime_mode=self.mode, run_id=self.run_id,
            account_fingerprint=current.get("account_fingerprint"))
        return float(outcome["daily_pnl"]), bool(outcome["hit"])

    def _fail_closed(self, reason: str, error: Exception) -> dict[str, Any]:
        """Reduce existing exposure when a safety prerequisite fails."""
        self._event("cycle_blocked", {"reason": reason, "error": str(error)})
        positions = None
        position_error = None
        try:
            positions = self.provider.positions()
        except Exception as exc:  # noqa: BLE001
            # A failed preliminary poll is not proof of no exposure.  Still
            # issue cancel/flatten so the broker gets a best-effort safety
            # command, then report the unknown state and pause below.
            position_error = exc
        try:
            complete = self.flatten_all(reason)
        except Exception as flatten_error:  # noqa: BLE001
            complete = False
            self._event("fail_closed_flatten_failed", {
                "reason": reason, "error": str(flatten_error)})
        pause_reasons = {
            "account_unavailable", "post_risk_positions_unavailable",
            "daily_risk_state_invalid", "position_exposure_invalid",
            "durable_risk_state_invalid", "planned_risk_invalid",
        }
        if (not complete or position_error is not None or
                reason in pause_reasons):
            try:
                state.commit({"operator_pause": True},
                             transition=(state.RUNNING, state.PAUSED))
            except Exception:  # noqa: BLE001
                pass
        try:
            residual = _plain(self.provider.positions())
            residual_unknown = False
        except Exception as residual_error:  # noqa: BLE001
            residual = []
            residual_unknown = True
            self._event("fail_closed_residual_unknown", {
                "reason": reason, "error": str(residual_error)})
        if (positions is not None and not positions and complete and
                not residual_unknown and reason not in pause_reasons):
            return {"action": "hold", "reason": reason, "error": str(error),
                    "closed": True, "residual": residual}
        result = {"action": "fail_closed", "reason": reason,
                "closed": complete, "residual": residual,
                "residual_unknown": residual_unknown}
        if position_error is not None:
            result["position_error"] = str(position_error)
        return result

    def _risk_order(self, symbol: str, signal: Mapping, row: Mapping,
                    account: Any, positions: list[Any], now: datetime,
                    cfg: Mapping | None = None):
        equity = self._number(_value(account, "equity", 0)) or 0
        mapped_positions = [_plain(position) for position in positions]
        gross = sum(abs(value) for value in (
            self._required_number(_value(position, "market_value", None),
                                  f"position {str(_value(position, 'symbol', '')).upper()} market_value")
            for position in positions))
        decision = dict(signal)
        decision["symbol"] = symbol
        authored_entry_reference = self._number(decision.get("entry_price"))
        if ("entry_price" in decision and decision.get("entry_price") is not None and
                authored_entry_reference is None):
            self._event("risk_reject", {
                "symbol": symbol, "reason": "authored entry price is invalid"})
            return None
        edge_cfg = cfg or self.cfg
        strategy = edge_cfg.get("strategy", {})
        profile = str(decision.get("execution_profile") or
                      strategy.get("execution_profile",
                                   strategy.get("execution_mode", "shares"))).lower()
        decision["execution_profile"] = "options" if profile in {"options", "option"} else "shares"
        if decision["execution_profile"] == "shares":
            try:
                entry_reference = _equity_entry_reference(decision, row)
                if entry_reference is not None:
                    decision["entry_price"] = entry_reference
                decision = _quantize_equity_bracket(decision)
            except (ValueError, ArithmeticError) as exc:
                self._event("execution_reject", {
                    "symbol": symbol, "reason": str(exc)})
                return None
        asset = self._assets.get(symbol.upper())
        if (decision["execution_profile"] == "shares" and
                decision.get("direction") == "short" and asset is not None and
                not bool(_value(asset, "shortable", False))):
            self._event("risk_reject", {
                "symbol": symbol, "reason": "asset is not shortable"})
            return None
        if decision["execution_profile"] == "options":
            configured_option_feed = str(
                edge_cfg.get("broker", {}).get("options_feed") or
                edge_cfg.get("data", {}).get("options_feed") or
                getattr(self.provider, "options_feed", "") or ""
            ).strip().lower()
            if configured_option_feed != "opra":
                self._event("risk_reject", {
                    "symbol": symbol,
                    "reason": "indicative option feed is non-executable; OPRA entitlement required",
                })
                return None
            candidates = (row.get("option_chain") or row.get("options") or
                          row.get("option_snapshots") or [])
            if isinstance(candidates, Mapping):
                candidates = [candidates]
            if not candidates:
                try:
                    quote = row.get("quote", {}) if isinstance(row.get("quote"), Mapping) else {}
                    bid = self._number(quote.get("bid", quote.get("bid_price")))
                    ask = self._number(quote.get("ask", quote.get("ask_price")))
                    spot = (bid + ask) / 2 if bid is not None and ask is not None else None
                    candidates = self.provider.option_candidates(
                        symbol, now=now, underlying_price=spot,
                        feed=(edge_cfg.get("broker", {}).get("options_feed") or
                              edge_cfg.get("data", {}).get("options_feed") or
                              getattr(self.provider, "options_feed", None)),
                        min_dte=int(edge_cfg.get("risk", {}).get("options_min_dte", 7)),
                        max_dte=int(edge_cfg.get("risk", {}).get("options_max_dte", 60)),
                    )
                    candidates = [_plain(item) for item in candidates]
                except (AttributeError, AlpacaError, TypeError, ValueError):
                    candidates = []
            decision["option_chain"] = candidates
        runtime = state.load_state()
        if not isinstance(runtime, Mapping):
            raise AlpacaError("durable runtime state is malformed")
        active = runtime.get("active_trades", {})
        if active is None:
            active = {}
        if not isinstance(active, Mapping):
            raise AlpacaError("active trade state is malformed")
        orders_state = runtime.get("orders", {})
        if orders_state is None:
            orders_state = {}
        if not isinstance(orders_state, Mapping):
            raise AlpacaError("order state is malformed")
        if any(isinstance(item, Mapping) and
               str(item.get("underlying_symbol", item.get("symbol", ""))).upper() == symbol
               for item in active.values() if isinstance(active, Mapping)):
            self._event("risk_reject", {"symbol": symbol, "reason": "already holding this underlying"})
            return None
        decision["daily_pnl"] = self._number(runtime.get("risk_day", {}).get("daily_pnl")) \
            if isinstance(runtime.get("risk_day"), Mapping) else None
        plan, why = self.risk.vet_open(
            decision, equity, mapped_positions, {symbol: row}, {}, gross,
            active_trades=active, now=now.timestamp(), cost_cfg=edge_cfg)
        if plan is None:
            self._event("risk_reject", {"symbol": symbol, "reason": why})
            return None
        buying_power = self._number(_value(account, "buying_power", None))
        if buying_power is None or buying_power <= 0 or float(plan.get("notional", 0) or 0) > buying_power:
            self._event("risk_reject", {"symbol": symbol, "reason": "insufficient buying power"})
            return None
        plan.update({key: signal.get(key) for key in (
            "setup_id", "setup_type", "strategy_id", "strategy_version",
            "variant_id", "signal_ts", "force_flat_at") if signal.get(key) is not None})
        plan["underlying_symbol"] = symbol
        if decision["execution_profile"] == "options":
            option = plan.get("option", {})
            option_symbol = str(option.get("symbol") or "").upper()
            if not option_symbol:
                return None
            qty = Decimal(str(plan["contracts"]))
            try:
                order_type, tif, limit = self._entry_execution(
                    "buy", option, option.get("debit", option.get("ask")))
            except ValueError as exc:
                self._event("execution_reject", {"symbol": option_symbol, "reason": str(exc)})
                return None
            return OrderRequest(option_symbol, qty, "buy", type=order_type,
                                time_in_force=tif, limit_price=limit,
                                client_order_id=self._client_id("open", signal),
                                position_intent="buy_to_open"), plan
        qty = Decimal(str(plan.get("shares", 0)))
        if qty <= 0:
            return None
        side = "buy" if signal.get("direction") == "long" else "sell"
        try:
            order_type, tif, limit = self._entry_execution(
                side, row.get("quote", {}),
                authored_entry_reference if authored_entry_reference is not None
                else plan.get("entry_price"))
            if limit is not None:
                limit = _quantize_equity_price(
                    limit, rounding=(ROUND_CEILING if side == "buy" else ROUND_FLOOR))
        except ValueError as exc:
            self._event("execution_reject", {"symbol": symbol, "reason": str(exc)})
            return None
        # A shares order's instrument is the underlying, so the instrument-level
        # stop/target are the same prices the bracket legs must carry.  The
        # separate underlying_* fields only diverge for the option profile,
        # which cannot be bracketed at all.
        stop_loss = self._number(plan.get("stop_price"))
        take_profit = self._number(plan.get("target_price"))
        try:
            if stop_loss is None or take_profit is None:
                raise ValueError("equity entry requires a stop and target for broker protection")
            request = OrderRequest(
                symbol, qty, side, type=order_type, time_in_force=tif,
                limit_price=limit, client_order_id=self._client_id("open", signal),
                order_class="bracket", take_profit=Decimal(str(take_profit)),
                stop_loss=Decimal(str(stop_loss)))
        except (ValueError, ArithmeticError) as exc:
            # A bracket the broker would reject must never be downgraded to an
            # unprotected entry; report it and take no position.
            self._event("execution_reject", {"symbol": symbol, "reason": str(exc)})
            return None
        return request, plan

    def _client_id(self, action: str, signal: Mapping, attempt: int = 0) -> str:
        execution = self.cfg.get("execution", {})
        prefix = str(execution.get("client_order_id_prefix", "ibr")) \
            if isinstance(execution, Mapping) else "ibr"
        setup = signal.get("setup_id") or signal.get("signal_ts")
        if setup is None:
            # Never use wall-clock entropy: retries and process restarts must
            # derive the same id from the same signal content.
            try:
                payload = json.dumps(_plain(signal), sort_keys=True,
                                     separators=(",", ":"), default=str,
                                     allow_nan=False)
            except (TypeError, ValueError):
                payload = repr(sorted((str(key), repr(value))
                                      for key, value in signal.items()))
            setup = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        setup = str(setup)
        suffix = f"-{attempt}" if attempt else ""
        tail = f"-{action}-{str(signal.get('symbol', '')).lower()}-{setup}"
        # Alpaca caps client ids at 48 chars.  Truncate only the core so the
        # attempt suffix remains visible and distinct for retries.
        prefix_budget = max(0, 48 - len(tail) - len(suffix))
        if prefix_budget + len(tail) + len(suffix) > 48:
            tail = tail[-max(0, 48 - len(suffix)):]
        return f"{prefix[:prefix_budget]}{tail}{suffix}"
