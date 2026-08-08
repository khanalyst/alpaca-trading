"""Deterministic, paper-only orchestration for the IBR strategy."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from . import state
from .alpaca_domain import OrderRequest
from .alpaca_provider import AlpacaError, AlpacaProvider
from .alpaca_session import SessionPolicy
from .brain import DecisionBrain
from .contracts.ibr import generate_ibr_signal
from .market import MarketData
from .risk import RiskEngine
from .strategy import build_setup_plan

log = logging.getLogger("engine")


def _plain(value):
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_plain(item) for item in value]
    if hasattr(value, "value"):
        return _plain(value.value)
    return value


def _value(obj: Any, name: str, default=None):
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


class Engine:
    def __init__(self, cfg: dict, light: bool = False, *, provider=None,
                 market_data: MarketData | None = None, brain=None):
        self.cfg = cfg
        # Config validation and AlpacaProvider both enforce this invariant;
        # keeping the assertion here prevents hand-built test configs from
        # accidentally selecting a non-paper client.
        broker = cfg.get("broker", {}) if isinstance(cfg, Mapping) else {}
        if str(cfg.get("mode", "paper")).lower() not in {"paper", "demo"}:
            raise AlpacaError("only paper mode is supported")
        if broker.get("paper", True) is not True or broker.get("allow_live", False):
            raise AlpacaError("paper=true and allow_live=false are required")
        self.provider = provider or AlpacaProvider(cfg)
        if not self.provider.paper:
            raise AlpacaError("provider is not paper scoped")
        session_cfg = cfg.get("session", {})
        self.market = market_data or MarketData(self.provider, SessionPolicy(**session_cfg))
        llm_cfg = cfg.get("llm", {})
        self.brain = brain or (None if light else DecisionBrain(llm_cfg))
        self.risk = RiskEngine(cfg)
        self.light = light
        self.running = False
        self.shutdown_reason: str | None = None
        self.run_id = f"run-{int(time.time() * 1000)}"
        self._runtime_state: dict = {}
        try:
            state.configure_runtime("paper")
            self._runtime_state = state.load_state()
            state.write_heartbeat("starting", run_id=self.run_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("state startup unavailable: %s", exc)
        if not light:
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001
                # A REST reconciliation failure must prevent entries, but it
                # need not make a status/check command unusable.
                log.warning("startup reconciliation unavailable: %s", exc)

    @property
    def ex(self):
        return self.provider

    @ex.setter
    def ex(self, value):
        self.provider = value
        if hasattr(self, "market"):
            self.market.provider = value

    def check(self, authenticated: bool = False) -> dict[str, Any]:
        result = {
            "mode": "paper", "paper": True,
            "credentials_configured": bool(self.provider.session.api_key and self.provider.session.secret_key),
            "authenticated": False,
        }
        if authenticated:
            account = self.provider.account()
            result.update(authenticated=True, account=account)
        return result

    def status(self) -> dict[str, Any]:
        result = self.check(False)
        try:
            result["clock"] = self.provider.clock()
            result["positions"] = self.provider.positions()
        except Exception as exc:  # noqa: BLE001
            result["auth_error"] = str(exc)
            result["positions"] = []
        return result

    def _event(self, kind: str, payload: Mapping | str) -> None:
        try:
            state.log_event(kind, json.dumps(_plain(payload), sort_keys=True, default=str))
        except Exception:  # noqa: BLE001
            log.debug("could not journal %s", kind, exc_info=True)

    def _order_for_decision(self, decision: Mapping, prefix: str) -> OrderRequest | None:
        """Compatibility helper; quantity from an LLM is intentionally ignored."""
        del decision, prefix
        return None

    @staticmethod
    def _bar_mapping(bar: Any, symbol: str) -> dict:
        row = _plain(bar)
        if not isinstance(row, Mapping):
            return {}
        out = dict(row)
        out["symbol"] = str(out.get("symbol") or symbol).upper()
        return out

    @staticmethod
    def _quote_mapping(quote: Any, symbol: str) -> dict:
        row = _plain(quote)
        return dict(row) if isinstance(row, Mapping) else {"symbol": symbol}

    def _universe(self) -> list[str]:
        universe = self.cfg.get("universe", {})
        rows = universe.get("symbols", []) if isinstance(universe, Mapping) else []
        deny = {str(item).upper() for item in universe.get("denylist", [])} if isinstance(universe, Mapping) else set()
        return [str(symbol).upper() for symbol in rows if str(symbol).strip() and str(symbol).upper() not in deny][:int(universe.get("max_symbols", 50) or 50)]

    def _collect(self, symbols: list[str], now: datetime, supplied: Mapping | None):
        """Fetch only configured symbols and completed one-minute bars."""
        if supplied is not None:
            raw = supplied
            if any(symbol in raw for symbol in symbols):
                rows = {symbol: raw.get(symbol, {}) for symbol in symbols}
            else:
                rows = {symbol: raw for symbol in symbols}
        else:
            end = now
            start = now - timedelta(hours=2)
            fetched_bars = self.market.stock_bars(symbols, timeframe="1m", start=start, end=end)
            fetched_quotes = self.market.stock_quotes(symbols, start=now - timedelta(minutes=2), end=end)
            rows = {
                symbol: {"bars": fetched_bars.get(symbol, []), "quotes": fetched_quotes.get(symbol, [])}
                for symbol in symbols
            }
        max_age = float(self.cfg.get("execution", {}).get("max_market_data_age_seconds", 30))
        result = {}
        for symbol in symbols:
            row = rows.get(symbol, {}) if isinstance(rows, Mapping) else {}
            row = dict(row) if isinstance(row, Mapping) else {}
            bars = row.get("bars") or row.get("candles") or []
            quotes = row.get("quotes") or row.get("quote") or []
            if isinstance(quotes, Mapping):
                quotes = [quotes]
            normalized_bars = [self._bar_mapping(item, symbol) for item in bars]
            normalized_bars = [item for item in normalized_bars if item.get("timestamp") is not None]
            normalized_bars.sort(key=lambda item: str(item.get("timestamp")))
            normalized_bars = [item for item in normalized_bars if self._completed(item, now)]
            quote = self._quote_mapping(quotes[-1], symbol) if quotes else {}
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
            result[symbol] = row
        return result

    @staticmethod
    def _number(value):
        try:
            number = float(value)
            return number if number == number and abs(number) != float("inf") else None
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _timestamp(value):
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            text = str(value).replace("Z", "+00:00")
            if text.isdigit():
                number = float(text); number /= 1000 if abs(number) > 100_000_000_000 else 1
                return datetime.fromtimestamp(number, timezone.utc)
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            return None

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

    def _risk_order(self, symbol: str, signal: Mapping, row: Mapping,
                    account: Any, positions: list[Any], now: datetime):
        equity = self._number(_value(account, "equity", 0)) or 0
        mapped_positions = [_plain(position) for position in positions]
        gross = sum(abs(self._number(_value(position, "market_value", 0)) or 0) for position in positions)
        decision = dict(signal)
        decision["symbol"] = symbol
        strategy = self.cfg.get("strategy", {})
        profile = str(strategy.get("execution_profile", strategy.get("execution_mode", "shares"))).lower()
        decision["execution_profile"] = "options" if profile in {"options", "option"} else "shares"
        if decision["execution_profile"] == "options":
            candidates = row.get("option_chain") or row.get("options") or []
            if not candidates:
                try:
                    candidates = [_plain(item) for item in self.provider.option_contracts(symbol)]
                except Exception:
                    candidates = []
            decision["option_chain"] = candidates
        plan, why = self.risk.vet_open(decision, equity, mapped_positions, {symbol: row}, {}, gross, now=now.timestamp())
        if plan is None:
            self._event("risk_reject", {"symbol": symbol, "reason": why})
            return None
        if decision["execution_profile"] == "options":
            option = plan.get("option", {})
            option_symbol = str(option.get("symbol") or "").upper()
            if not option_symbol:
                return None
            qty = Decimal(str(plan["contracts"]))
            return OrderRequest(option_symbol, qty, "buy", type="market", time_in_force="day",
                                client_order_id=self._client_id("open", signal), position_intent="buy_to_open"), plan
        qty = Decimal(str(plan.get("shares", 0)))
        if qty <= 0:
            return None
        side = "buy" if signal.get("direction") == "long" else "sell"
        return OrderRequest(symbol, qty, side, type="market", time_in_force="day",
                            client_order_id=self._client_id("open", signal)), plan

    def _client_id(self, action: str, signal: Mapping, attempt: int = 0) -> str:
        prefix = str(self.cfg.get("execution", {}).get("client_order_id_prefix", "ibr"))
        setup = str(signal.get("setup_id") or signal.get("signal_ts") or int(time.time()))
        suffix = f"-{attempt}" if attempt else ""
        return f"{prefix}-{action}-{str(signal.get('symbol', '')).lower()}-{setup}{suffix}"[:48]

    def run_once(self, snapshot: dict | None = None, portfolio: dict | None = None) -> dict[str, Any]:
        try:
            calendar = self.market.refresh_calendar()
            clock = self.market.clock()
        except Exception as exc:  # noqa: BLE001
            self._event("cycle_blocked", {"reason": "calendar_or_clock_unavailable", "error": str(exc)})
            try: state.write_heartbeat("degraded", reason="calendar_or_clock_unavailable")
            except Exception: pass
            return {"action": "hold", "reason": "calendar_or_clock_unavailable", "error": str(exc)}
        now = clock.timestamp
        if self.market.should_force_flat(now):
            closed = self.flatten_all("before_close")
            return {"action": "force_flat", "closed": closed}
        if not clock.is_open or not self.market.can_enter(now):
            return {"action": "hold", "reason": "outside_regular_session"}
        session = self.market.session(now)
        session_close = session.close if session is not None else None
        force_flat_at = None
        if session_close is not None:
            minutes = int(self.cfg.get("strategy", {}).get(
                "force_flat_minutes_before_close",
                self.cfg.get("session", {}).get("force_flat_minutes_before_close", 10),
            ))
            force_flat_at = session_close - timedelta(minutes=max(0, minutes))
        symbols = self._universe()
        rows = self._collect(symbols, now, snapshot)
        account = self.provider.account()
        positions = self.provider.positions()
        portfolio_data = portfolio or {"account": _plain(account), "positions": _plain(positions)}
        placed = []; signals = []
        for symbol in symbols:
            row = rows.get(symbol)
            if not row:
                continue
            bars = row.get("bars", [])
            signal = generate_ibr_signal(symbol, bars, config=self.cfg.get("strategy", {}), now=now)
            if signal is None:
                continue
            # Complete signal evidence must carry the latest quote controls.
            signal = dict(signal)
            # The broker calendar is authoritative for early closes.  Replace
            # the IBR contract's regular-session default with this day's
            # close before setup validation and journaling.
            if force_flat_at is not None:
                signal["force_flat_at"] = force_flat_at.isoformat()
                signal["force_flat_ts"] = force_flat_at.timestamp()
            signal.update({"symbol": symbol, "relative_volume": signal.get("relative_volume"), "spread_bps": row.get("spread_bps"), "stale": False, "quote_stale": False})
            plan_snapshot = {
                **row,
                "price": self._number(row.get("quote", {}).get("ask")),
                "close": signal.get("entry_price"),
                "relative_volume": signal.get("relative_volume"),
                "spread_bps": row.get("spread_bps"),
                "stale": False,
                "quote_stale": False,
                "signal_ts": signal.get("signal_ts"),
                "session": signal.get("session"),
                "ibr_range": {"high": signal.get("range_high"), "low": signal.get("range_low"),
                               "width": signal.get("range_width"),
                               "range_end_ts": float(signal.get("signal_ts", 0)) - 60,
                               "complete": True,
                               "force_flat_at": signal.get("force_flat_at"),
                               "force_flat_ts": signal.get("force_flat_ts")},
            }
            plan, why = build_setup_plan(signal, plan_snapshot, self.cfg)
            if plan is None:
                self._event("setup_reject", {"symbol": symbol, "reason": why})
                continue
            signals.append(plan)
            if not self._llm_allows(plan, row, portfolio_data):
                continue
            sized = self._risk_order(symbol, plan, row, account, positions, now)
            if sized is None:
                continue
            request, risk_plan = sized
            try:
                order = self.provider.submit_order(request)
                placed.append(order)
                self._event("order_submitted", {"symbol": request.symbol, "qty": str(request.qty), "client_order_id": request.client_order_id, "risk": risk_plan})
            except AlpacaError:
                raise
        try: state.write_heartbeat("running", run_id=self.run_id, orders=len(placed))
        except Exception: pass
        return {"action": "decide", "orders": placed, "signals": signals}

    def reconcile(self):
        if hasattr(self.provider, "reconcile"):
            result = self.provider.reconcile()
        else:
            result = {"positions": self.provider.positions(), "orders": self.provider.orders()}
        self._event("rest_reconcile", {"positions": len(result.get("positions", [])) if isinstance(result, Mapping) else 0})
        return result

    def flatten_all(self, reason: str = "operator") -> bool:
        try:
            self.provider.cancel_all_orders()
        except Exception as exc:  # noqa: BLE001
            self._event("flatten_cancel_error", {"reason": str(exc)})
        for attempt in range(3):
            positions = self.provider.positions()
            if not positions:
                self._event("flatten_confirmed", {"reason": reason, "attempt": attempt})
                return True
            for position in positions:
                qty = abs(Decimal(str(_value(position, "qty", 0))))
                if qty <= 0:
                    continue
                side = "sell" if str(_value(position, "side", "long")).lower() in {"long", "buy"} else "buy"
                request = OrderRequest(str(_value(position, "symbol", "")).upper(), qty, side,
                                       type="market", time_in_force="day",
                                       client_order_id=self._client_id("flatten", {"symbol": _value(position, "symbol", "")}, attempt))
                self.provider.submit_order(request)
            try:
                self.reconcile()
            except Exception:
                pass
        residual = self.provider.positions()
        self._event("flatten_incomplete", {"reason": reason, "residual": _plain(residual)})
        return not residual

    def request_shutdown(self, reason: str = "shutdown") -> None:
        self.shutdown_reason = reason
        self.running = False

    def run(self, *, max_cycles: int | None = None) -> None:
        self.running = True
        cycles = 0
        interval = float(self.cfg.get("cycle", {}).get("interval_seconds", 60))
        while self.running and (max_cycles is None or cycles < max_cycles):
            try:
                self.run_once()
            except AlpacaError:
                log.exception("Alpaca cycle failed; pausing safely")
                self.running = False
                try: state.write_heartbeat("degraded", reason="alpaca_error")
                except Exception: pass
                raise
            cycles += 1
            if self.running and (max_cycles is None or cycles < max_cycles):
                time.sleep(interval)
