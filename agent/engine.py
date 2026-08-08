"""Deterministic, paper-only orchestration for the IBR strategy."""

from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
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
from .contracts.rule import generate_rule_signal
from .market import MarketData
from .risk import RiskEngine
from .strategy import build_setup_plan

log = logging.getLogger("engine")

_FILLED_ORDER_STATUSES = {"filled", "partially_filled"}
_TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "replaced", "stopped",
    "suspended", "failed", "not_found",
}


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
        research_cfg = cfg.get("research", {}) if isinstance(cfg, Mapping) else {}
        self._edge_base_cfg = deepcopy(cfg)
        self._edge_requested_variant = str(
            cfg.get("strategy", {}).get("variant_id") or "")
        self._edge_db_path = research_cfg.get("db_path") if isinstance(research_cfg, Mapping) else None
        self._edge_required = bool(
            isinstance(research_cfg, Mapping) and research_cfg.get("enabled", False) and
            research_cfg.get("require_validated_variant", True))
        self._edge_record: dict | None = None
        self._edge_error: str | None = None
        if isinstance(research_cfg, Mapping) and research_cfg.get("enabled", False):
            try:
                from .edge import apply_variant, resolve_validated_variant
                self._edge_record = resolve_validated_variant(
                    cfg, db_path=self._edge_db_path or None)
                if self._edge_record is not None:
                    cfg = apply_variant(cfg, self._edge_record)
                    self.cfg = cfg
                elif self._edge_required:
                    mode = str(cfg.get("strategy", {}).get("execution_mode", "shares"))
                    self._edge_error = f"no validated edge champion for {mode}"
            except Exception as exc:  # noqa: BLE001
                self._edge_error = f"edge resolution failed: {exc}"
        self.provider = provider or AlpacaProvider(cfg)
        if not self.provider.paper:
            raise AlpacaError("provider is not paper scoped")
        session_cfg = cfg.get("session", {})
        self.market = market_data or MarketData(self.provider, SessionPolicy(**session_cfg))
        llm_cfg = cfg.get("llm", {})
        llm_enabled = bool(llm_cfg.get("enabled", False)) if isinstance(llm_cfg, Mapping) else False
        self.brain = brain or (DecisionBrain(llm_cfg) if llm_enabled and not light else None)
        self.risk = RiskEngine(cfg)
        self.light = light
        self.running = False
        self.shutdown_reason: str | None = None
        self.run_id = f"run-{int(time.time() * 1000)}"
        self._runtime_state: dict = {}
        self._lock_handle = None
        self._persistent_lock = False
        self._state_ready = False
        self._preflight: dict[str, Any] | None = None
        self._preflight_error: str | None = None
        self._reconciled = False
        self._assets: dict[str, Any] = {}
        try:
            state.configure_runtime("paper")
            state.ensure_ready()
            self._runtime_state = state.load_state()
            state.write_heartbeat("starting", run_id=self.run_id)
            self._state_ready = True
        except Exception as exc:  # noqa: BLE001
            log.warning("state startup unavailable: %s", exc)
        if not light:
            try:
                self.preflight()
                self.reconcile()
            except Exception as exc:  # noqa: BLE001
                # A REST reconciliation failure must prevent entries, but it
                # need not make a status/check command unusable.
                log.warning("startup reconciliation unavailable: %s", exc)
                self._preflight_error = str(exc)

    def check(self, authenticated: bool = False) -> dict[str, Any]:
        result = {
            "mode": "paper", "paper": True,
            "credentials_configured": bool(self.provider.session.api_key and self.provider.session.secret_key),
            "authenticated": False,
            "edge_required": self._edge_required,
            "edge_ready": self._edge_record is not None,
            "edge_error": self._edge_error,
            "variant_id": (self._edge_record or {}).get("variant_id"),
            "edge_vehicle": (self._edge_record or {}).get("vehicle"),
        }
        if authenticated:
            preflight = self.preflight()
            result.update(authenticated=True, account=preflight["account"],
                          clock=preflight["clock"], endpoint=preflight["endpoint"],
                          data_feed=preflight["data_feed"],
                          options_feed=preflight["options_feed"])
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

    def _acquire_lock(self, *, persistent: bool = False) -> bool:
        if self._lock_handle is not None:
            self._persistent_lock = self._persistent_lock or persistent
            return True
        handle = state.acquire_run_lock()
        if handle is None:
            return False
        self._lock_handle = handle
        self._persistent_lock = persistent
        return True

    def _release_lock(self) -> None:
        if self._lock_handle is not None:
            state.release_run_lock(self._lock_handle)
        self._lock_handle = None
        self._persistent_lock = False

    def preflight(self) -> dict[str, Any]:
        """Authenticate and validate the paper endpoint before any order path."""
        if self._preflight is not None:
            return self._preflight
        if not self._state_ready:
            state.ensure_ready()
            self._state_ready = True
        if not getattr(self.provider, "paper", False):
            raise AlpacaError("provider is not paper scoped")
        session = getattr(self.provider, "session", None)
        api_key = getattr(session, "api_key", None)
        secret_key = getattr(session, "secret_key", None)
        if not api_key or not secret_key:
            raise AlpacaError("authenticated paper credentials are required before trading")
        endpoint = getattr(self.provider, "endpoint", None) or getattr(session, "endpoint", None)
        if endpoint is not None and "paper-api.alpaca.markets" not in str(endpoint):
            raise AlpacaError("paper endpoint validation failed")
        data_feed = str(getattr(self.provider, "data_feed", "iex")).lower()
        options_feed = str(getattr(self.provider, "options_feed", "indicative")).lower()
        if data_feed not in {"iex", "sip", "delayed_sip"}:
            raise AlpacaError(f"unsupported equity feed {data_feed!r}")
        if options_feed not in {"indicative", "opra"}:
            raise AlpacaError(f"unsupported options feed {options_feed!r}")
        assets_method = getattr(self.provider, "assets", None)
        assets_supported = callable(assets_method)
        try:
            account = self.provider.account()
            clock = self.provider.clock()
            self.market.refresh_calendar()
            assets = assets_method() if assets_supported else []
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"authenticated paper preflight failed: {exc}") from exc
        account_id = _value(account, "id")
        if not account_id:
            raise AlpacaError("paper account identity is unavailable")
        if str(_value(account, "status", "")).lower() not in {"active", "account_status.active"}:
            raise AlpacaError("paper account is not active")
        if (self._number(_value(account, "equity", None)) or 0) <= 0:
            raise AlpacaError("paper account equity is unavailable")
        if assets_supported:
            self._assets = {
                str(_value(asset, "symbol", "")).upper(): asset for asset in assets
                if _value(asset, "symbol", None)
            }
            invalid = [symbol for symbol in self._universe()
                       if symbol not in self._assets or
                       not bool(_value(self._assets[symbol], "tradable", False)) or
                       str(_value(self._assets[symbol], "status", "")).lower()
                       not in {"active", "asset_status.active"}]
            if invalid:
                raise AlpacaError(
                    "configured symbols are inactive or not tradable: " +
                    ", ".join(sorted(invalid)))
        fingerprint = state.account_fingerprint(
            "paper", f"{api_key}\0{account_id}")
        state.bind_account_identity(fingerprint)
        self._runtime_state = state.load_state()
        self._preflight = {"account": account, "clock": clock,
                           "endpoint": endpoint or "paper-api.alpaca.markets",
                           "data_feed": data_feed, "options_feed": options_feed,
                           "account_fingerprint": fingerprint}
        self._preflight_error = None
        try:
            state.commit({"runtime_mode": "paper", "account_fingerprint": fingerprint,
                          "preflight": {"endpoint": self._preflight["endpoint"],
                                         "data_feed": data_feed,
                                         "options_feed": options_feed,
                                         "authenticated": True}})
            state.log_equity(_value(account, "equity", None), "starting",
                             runtime_mode="paper", account_fingerprint=fingerprint,
                             run_id=self.run_id)
        except Exception as exc:  # noqa: BLE001
            self._state_ready = False
            raise AlpacaError(f"paper journal preflight write failed: {exc}") from exc
        return self._preflight

    def _ensure_order_ready(self) -> bool:
        if not self._state_ready:
            return False
        try:
            state.check_journal()
        except Exception as exc:  # noqa: BLE001
            self._state_ready = False
            self._preflight_error = f"paper journal unavailable: {exc}"
            return False
        runtime = state.load_state()
        if runtime.get("state") == state.KILLED or runtime.get("operator_pause") is True:
            self._preflight_error = runtime.get("kill_reason") or "operator_pause"
            return False
        try:
            self.preflight()
        except Exception as exc:  # noqa: BLE001
            self._preflight_error = str(exc)
            self._event("cycle_blocked", {"reason": "preflight_failed", "error": str(exc)})
            return False
        if not self._reconciled:
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001
                self._preflight_error = f"startup reconciliation failed: {exc}"
                self._event("cycle_blocked", {"reason": "reconciliation_failed", "error": str(exc)})
                return False
        return True

    def _refresh_edge(self) -> bool:
        """Refresh the selected vehicle champion before opening new risk."""
        research = self._edge_base_cfg.get("research", {})
        if not isinstance(research, Mapping) or not research.get("enabled", False):
            return True
        try:
            from .edge import apply_variant, resolve_validated_variant
            lookup = deepcopy(self._edge_base_cfg)
            if self._edge_requested_variant:
                lookup.setdefault("strategy", {})["variant_id"] = self._edge_requested_variant
            else:
                lookup.setdefault("strategy", {}).pop("variant_id", None)
            record = resolve_validated_variant(
                lookup, db_path=self._edge_db_path or None)
            if record is None:
                self._edge_record = None
                self._edge_error = "no validated edge champion for " + str(
                    lookup.get("strategy", {}).get("execution_mode", "shares"))
                if self._edge_required:
                    try:
                        runtime = state.load_state()
                        runtime["state"] = state.PAUSED
                        state.save_state(runtime)
                        state.write_heartbeat(
                            "paused", run_id=self.run_id,
                            reason="validated_edge_required",
                            edge_vehicle=lookup.get("strategy", {}).get(
                                "execution_mode", "shares"))
                    except Exception:  # noqa: BLE001
                        pass
                return not self._edge_required
            if (self._edge_record or {}).get("candidate_id") != record.get("candidate_id"):
                self.cfg = apply_variant(self._edge_base_cfg, record)
                self.risk = RiskEngine(self.cfg)
                self._event("edge_selected", {
                    "candidate_id": record.get("candidate_id"),
                    "variant_id": record.get("variant_id"),
                    "vehicle": record.get("vehicle"),
                })
            self._edge_record = record
            self._edge_error = None
            try:
                runtime = state.load_state()
                if (self.running and runtime.get("state") == state.PAUSED and
                        runtime.get("operator_pause") is not True):
                    runtime["state"] = state.RUNNING
                    state.save_state(runtime)
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception as exc:  # noqa: BLE001
            self._edge_record = None
            self._edge_error = f"edge resolution failed: {exc}"
            self._event("edge_resolution_failed", {"error": str(exc)})
            return not self._edge_required

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
            row = rows.get(symbol, {}) if isinstance(rows, Mapping) else {}
            row = dict(row) if isinstance(row, Mapping) else {}
            if row.get("stale") is True or row.get("quote_stale") is True:
                continue
            bars = row.get("bars") or row.get("candles") or []
            quotes = row.get("quotes") or row.get("quote") or []
            if isinstance(quotes, Mapping):
                quotes = [quotes]
            normalized_bars = [self._bar_mapping(item, symbol) for item in bars]
            normalized_bars = [item for item in normalized_bars if item.get("timestamp") is not None]
            normalized_bars.sort(key=lambda item: str(item.get("timestamp")))
            normalized_bars = [item for item in normalized_bars if self._completed(item, now)]
            quote = self._quote_mapping(quotes[-1], symbol) if quotes else {}
            if quote.get("stale") is True or quote.get("quote_stale") is True:
                continue
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
        current = state.load_state()
        prior = current.get("risk_day", {})
        if not isinstance(prior, Mapping) or prior.get("date") != day:
            prior = {"date": day, "start_equity": equity}
        start = self._number(prior.get("start_equity"))
        if start is None or start <= 0:
            start = equity
        daily_pnl = equity - start
        risk_day = {
            "date": day, "start_equity": start, "current_equity": equity,
            "daily_pnl": daily_pnl, "updated_ts": time.time(),
        }
        limit_pct = self._number(self.cfg.get("risk", {}).get("daily_loss_limit_pct"))
        hit = bool(limit_pct is not None and daily_pnl <= -(start * limit_pct / 100.0))
        risk_day["limit_hit"] = hit
        current["risk_day"] = risk_day
        if hit:
            current["state"] = state.DAY_STOPPED
        elif current.get("state") == state.DAY_STOPPED:
            current["state"] = state.RUNNING if self.running else state.PAUSED
        state.save_state(current)
        state.log_equity(
            equity, "running", runtime_mode="paper", run_id=self.run_id,
            account_fingerprint=current.get("account_fingerprint"))
        return daily_pnl, hit

    def _fail_closed(self, reason: str, error: Exception) -> dict[str, Any]:
        """Reduce existing paper exposure when a safety prerequisite fails."""
        self._event("cycle_blocked", {"reason": reason, "error": str(error)})
        try:
            positions = self.provider.positions()
        except Exception as position_error:  # noqa: BLE001
            return {"action": "hold", "reason": reason, "error": str(error),
                    "position_error": str(position_error), "residual_unknown": True}
        if not positions:
            return {"action": "hold", "reason": reason, "error": str(error)}
        try:
            complete = self.flatten_all(reason)
        except Exception as flatten_error:  # noqa: BLE001
            complete = False
            self._event("fail_closed_flatten_failed", {
                "reason": reason, "error": str(flatten_error)})
        try:
            residual = _plain(self.provider.positions())
            residual_unknown = False
        except Exception as residual_error:  # noqa: BLE001
            residual = []
            residual_unknown = True
            self._event("fail_closed_residual_unknown", {
                "reason": reason, "error": str(residual_error)})
        return {"action": "fail_closed", "reason": reason,
                "closed": complete, "residual": residual,
                "residual_unknown": residual_unknown}

    def _risk_order(self, symbol: str, signal: Mapping, row: Mapping,
                    account: Any, positions: list[Any], now: datetime):
        equity = self._number(_value(account, "equity", 0)) or 0
        mapped_positions = [_plain(position) for position in positions]
        gross = sum(abs(self._number(_value(position, "market_value", 0)) or 0) for position in positions)
        decision = dict(signal)
        decision["symbol"] = symbol
        strategy = self.cfg.get("strategy", {})
        profile = str(decision.get("execution_profile") or
                      strategy.get("execution_profile",
                                   strategy.get("execution_mode", "shares"))).lower()
        decision["execution_profile"] = "options" if profile in {"options", "option"} else "shares"
        asset = self._assets.get(symbol.upper())
        if (decision["execution_profile"] == "shares" and
                decision.get("direction") == "short" and asset is not None and
                not bool(_value(asset, "shortable", False))):
            self._event("risk_reject", {
                "symbol": symbol, "reason": "asset is not shortable"})
            return None
        if decision["execution_profile"] == "options":
            candidates = row.get("option_chain") or row.get("options") or []
            if not candidates:
                try:
                    quote = row.get("quote", {}) if isinstance(row.get("quote"), Mapping) else {}
                    bid = self._number(quote.get("bid", quote.get("bid_price")))
                    ask = self._number(quote.get("ask", quote.get("ask_price")))
                    spot = (bid + ask) / 2 if bid is not None and ask is not None else None
                    candidates = self.provider.option_candidates(
                        symbol, now=now, underlying_price=spot,
                        min_dte=int(self.cfg.get("risk", {}).get("options_min_dte", 7)),
                        max_dte=int(self.cfg.get("risk", {}).get("options_max_dte", 60)),
                    )
                    candidates = [_plain(item) for item in candidates]
                except (AttributeError, AlpacaError, TypeError, ValueError):
                    candidates = []
            decision["option_chain"] = candidates
        runtime = state.load_state()
        active = runtime.get("active_trades", {}) if isinstance(runtime, Mapping) else {}
        if any(isinstance(item, Mapping) and
               str(item.get("underlying_symbol", item.get("symbol", ""))).upper() == symbol
               for item in active.values() if isinstance(active, Mapping)):
            self._event("risk_reject", {"symbol": symbol, "reason": "already holding this underlying"})
            return None
        decision["daily_pnl"] = self._number(runtime.get("risk_day", {}).get("daily_pnl")) \
            if isinstance(runtime.get("risk_day"), Mapping) else None
        plan, why = self.risk.vet_open(
            decision, equity, mapped_positions, {symbol: row}, {}, gross,
            active_trades=active, now=now.timestamp())
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
                side, row.get("quote", {}), plan.get("entry_price"))
        except ValueError as exc:
            self._event("execution_reject", {"symbol": symbol, "reason": str(exc)})
            return None
        return OrderRequest(symbol, qty, side, type=order_type,
                            time_in_force=tif, limit_price=limit,
                            client_order_id=self._client_id("open", signal)), plan

    def _client_id(self, action: str, signal: Mapping, attempt: int = 0) -> str:
        prefix = str(self.cfg.get("execution", {}).get("client_order_id_prefix", "ibr"))
        setup = str(signal.get("setup_id") or signal.get("signal_ts") or int(time.time()))
        suffix = f"-{attempt}" if attempt else ""
        return f"{prefix}-{action}-{str(signal.get('symbol', '')).lower()}-{setup}{suffix}"[:48]

    def run_once(self, snapshot: dict | None = None, portfolio: dict | None = None) -> dict[str, Any]:
        """Run one safely gated cycle, taking a temporary process lock."""
        temporary = False
        if self._lock_handle is None:
            if not self._acquire_lock():
                self._event("cycle_blocked", {"reason": "runtime_lock_held"})
                return {"action": "hold", "reason": "runtime_lock_held"}
            temporary = True
        try:
            return self._run_once_impl(snapshot, portfolio)
        finally:
            if temporary:
                self._release_lock()

    def _run_once_impl(self, snapshot: dict | None = None, portfolio: dict | None = None) -> dict[str, Any]:
        if not self._ensure_order_ready():
            reason = self._preflight_error or "startup_reconciliation_required"
            try:
                state.write_heartbeat("degraded", run_id=self.run_id, reason=reason)
            except Exception:
                pass
            return {"action": "hold", "reason": reason}
        try:
            calendar = self.market.refresh_calendar()
            clock = self.market.clock()
        except Exception as exc:  # noqa: BLE001
            try: state.write_heartbeat("degraded", reason="calendar_or_clock_unavailable")
            except Exception: pass
            return self._fail_closed("calendar_or_clock_unavailable", exc)
        now = clock.timestamp
        try:
            reconciliation = self.reconcile()
            positions = reconciliation.get("positions", []) if isinstance(reconciliation, Mapping) else []
        except Exception as exc:  # noqa: BLE001
            self._reconciled = False
            return self._fail_closed("cycle_reconciliation_failed", exc)
        if self.market.should_force_flat(now):
            closed = self.flatten_all("before_close")
            return {"action": "force_flat", "closed": closed,
                    "residual": _plain(self.provider.positions())}
        monitored = self._monitor_positions(now, positions)
        if monitored.get("failed"):
            return {"action": "hold", "reason": "position_close_failed", **monitored}
        if monitored.get("closed"):
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001
                return {"action": "hold", "reason": "close_reconciliation_failed", "error": str(exc)}
            # A submitted close must be reconciled before any new exposure is
            # considered, even if the paper broker has not filled it yet.
            return {"action": "close", **monitored}
        if not self._refresh_edge():
            return {"action": "hold", "reason": self._edge_error or
                    "validated edge champion is required"}
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
        daily_pnl, daily_stop = self._update_daily_risk(account, now)
        if daily_stop:
            complete = self.flatten_all("daily_loss_limit")
            self._event("daily_loss_limit", {"daily_pnl": daily_pnl,
                                              "flatten_complete": complete})
            return {"action": "day_stopped", "daily_pnl": daily_pnl,
                    "closed": complete, "residual": _plain(self.provider.positions())}
        positions = self.provider.positions()
        portfolio_data = portfolio or {"account": _plain(account), "positions": _plain(positions)}
        placed = []; signals = []
        for symbol in symbols:
            row = rows.get(symbol)
            if not row:
                continue
            bars = row.get("bars", [])
            if str(self.cfg.get("strategy", {}).get("id")) == "rule":
                signal = generate_rule_signal(symbol, bars, config=self.cfg, now=now)
            else:
                signal = generate_ibr_signal(
                    symbol, bars, config=self.cfg.get("strategy", {}), now=now)
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
            signal.update({"symbol": symbol,
                           "relative_volume": signal.get("relative_volume"),
                           "spread_bps": row.get("spread_bps"),
                           "stale": bool(row.get("stale", False)),
                           "quote_stale": bool(row.get("quote_stale", False))})
            plan_snapshot = {
                **row,
                "price": self._number(row.get("quote", {}).get("ask")),
                "close": signal.get("entry_price"),
                "relative_volume": signal.get("relative_volume"),
                "spread_bps": row.get("spread_bps"),
                "stale": bool(row.get("stale", False)),
                "quote_stale": bool(row.get("quote_stale", False)),
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
            if self._client_order_pending(request.client_order_id):
                self._event("entry_pending", {"symbol": request.symbol,
                                                "client_order_id": request.client_order_id})
                continue
            try:
                order = self.provider.submit_order(request)
                placed.append(order)
                self._record_open_order(request, order, risk_plan)
            except AlpacaError:
                raise
        try: state.write_heartbeat("running", run_id=self.run_id, orders=len(placed))
        except Exception: pass
        return {"action": "decide", "orders": placed, "signals": signals}

    def _client_order_pending(self, client_order_id: str | None) -> bool:
        if not client_order_id:
            return False
        orders = state.load_state().get("orders", {})
        if not isinstance(orders, Mapping):
            return False
        return any(isinstance(item, Mapping) and
                   item.get("client_order_id") == client_order_id and
                   str(item.get("status", "")).lower() not in _TERMINAL_ORDER_STATUSES
                   for item in orders.values())

    def _record_open_order(self, request: OrderRequest, order: Any, risk_plan: Mapping) -> None:
        """Persist submission metadata; only a reported fill becomes a trade."""
        symbol = request.symbol.upper()
        order_id = str(getattr(order, "id", None) or request.client_order_id)
        status = str(getattr(order, "status", "submitted") or "submitted").lower()
        filled_qty = self._number(getattr(order, "filled_qty", None)) or 0.0
        if status == "filled" and filled_qty <= 0:
            filled_qty = float(request.qty)
        fill_price = self._number(getattr(order, "filled_avg_price", None))
        current = state.load_state()
        order_state = {
            "order_id": order_id, "symbol": symbol, "status": status,
            "client_order_id": request.client_order_id, "qty": str(request.qty),
            "filled_qty": filled_qty, "filled_avg_price": fill_price,
            "side": request.side, "type": request.type,
            "time_in_force": request.time_in_force,
            "position_intent": request.position_intent,
            "risk_plan": _plain(risk_plan), "fill_logged": False,
            "logged_filled_qty": 0.0,
            "updated_ts": time.time(),
        }
        current.setdefault("orders", {})[order_id] = order_state
        if filled_qty > 0 and status in _FILLED_ORDER_STATUSES:
            self._activate_filled_trade(current, order_state, filled_qty, fill_price)
        state.save_state(current)
        state.log_order(order, request, action="submit", run_id=self.run_id,
                        runtime_mode="paper", account_fingerprint=current.get("account_fingerprint"),
                        setup_id=risk_plan.get("setup_id"))
        self._event("order_submitted", {"symbol": request.symbol, "qty": str(request.qty),
                                         "status": status, "filled_qty": filled_qty,
                                         "client_order_id": request.client_order_id,
                                         "risk": risk_plan})

    def _activate_filled_trade(self, current: dict, order_state: dict,
                               filled_qty: float, fill_price: float | None) -> dict:
        """Create durable protection and one open-trade row for an actual fill."""
        plan = order_state.get("risk_plan", {})
        plan = plan if isinstance(plan, Mapping) else {}
        symbol = str(order_state.get("symbol", "")).upper()
        profile = str(plan.get("execution_profile", "shares"))
        underlying = str(plan.get("underlying_symbol") or
                         (symbol if profile == "shares" else "")).upper()
        direction = str(plan.get("direction") or
                        ("long" if order_state.get("side") == "buy" else "short"))
        instrument_entry = fill_price
        if instrument_entry is None:
            instrument_entry = self._number(
                plan.get("option", {}).get("debit") if profile == "options" and
                isinstance(plan.get("option"), Mapping) else plan.get("entry_price"))
        existing = current.get("active_trades", {}).get(symbol, {})
        existing = existing if isinstance(existing, Mapping) else {}
        trade = {
            "symbol": symbol, "underlying_symbol": underlying,
            "execution_profile": profile, "direction": direction,
            "position_side": "long" if order_state.get("side") == "buy" else "short",
            "qty": str(filled_qty), "entry_price": instrument_entry,
            "underlying_entry_price": plan.get("entry_price"),
            "opened_at": existing.get("opened_at", time.time()),
            "setup_type": plan.get("setup_type", "ibr"),
            "setup_id": plan.get("setup_id"), "order_id": order_state.get("order_id"),
            "status": "open", "stop_price": plan.get("underlying_stop_price", plan.get("stop_price")),
            "target_price": plan.get("underlying_target_price", plan.get("target_price")),
            "force_flat_at": plan.get("force_flat_at"), "risk_usd": plan.get("risk_usd"),
            "notional": plan.get("notional"), "variant_id": plan.get("variant_id"),
            "strategy_id": plan.get("strategy_id", self.cfg.get("strategy", {}).get("id")),
            "strategy_version": plan.get("strategy_version", self.cfg.get("strategy", {}).get("version")),
            "contract_multiplier": plan.get("contract_multiplier", 1),
        }
        current.setdefault("active_trades", {})[symbol] = trade
        current.setdefault("protection", {})[symbol] = {
            key: trade.get(key) for key in (
                "underlying_symbol", "stop_price", "target_price", "force_flat_at")
        }
        logged_qty = self._number(order_state.get("logged_filled_qty")) or 0.0
        if order_state.get("fill_logged") and "logged_filled_qty" not in order_state:
            # Older state files used a boolean only; avoid duplicating their
            # already-journaled fill during a rolling upgrade.
            logged_qty = filled_qty
        incremental_qty = max(0.0, filled_qty - logged_qty)
        if incremental_qty > 0:
            state.log_trade(
                symbol, order_state.get("side"), "open", incremental_qty,
                price=instrument_entry, notional=plan.get("notional"),
                risk_usd=plan.get("risk_usd"), order_id=order_state.get("order_id"),
                setup_id=plan.get("setup_id"), setup_type=plan.get("setup_type"),
                strategy_id=trade.get("strategy_id"),
                strategy_version=trade.get("strategy_version"),
                variant_id=trade.get("variant_id"), fill_status="filled",
                runtime_mode="paper", account_fingerprint=current.get("account_fingerprint"),
                run_id=self.run_id)
            order_state["fill_logged"] = True
            order_state["logged_filled_qty"] = filled_qty
        return trade

    def _close_position(self, position: Any, reason: str, *, attempt: int = 0) -> Any:
        symbol = str(_value(position, "symbol", "")).upper()
        qty = abs(Decimal(str(_value(position, "qty", 0))))
        if not symbol or qty <= 0:
            return None
        client_order_id = self._client_id(
            "close", {"symbol": symbol, "setup_id": reason}, attempt)
        close = getattr(self.provider, "close_position", None)
        if callable(close):
            try:
                result = close(symbol, qty=qty, client_order_id=client_order_id,
                               order_type="market", time_in_force="day")
            except TypeError:
                try:
                    result = close(symbol, qty=qty)
                except TypeError:
                    result = close(symbol)
            state.log_order(result, None, action="close", reason=reason,
                            symbol=symbol, qty=float(qty), runtime_mode="paper",
                            run_id=self.run_id,
                            client_order_id=client_order_id)
            return result
        side = "sell" if str(_value(position, "side", "long")).lower() in {"long", "buy"} else "buy"
        request = OrderRequest(symbol, qty, side, type="market", time_in_force="day",
                               client_order_id=client_order_id)
        result = self.provider.submit_order(request)
        state.log_order(result, request, action="close", reason=reason,
                        runtime_mode="paper", run_id=self.run_id)
        return result

    def _protection_price(self, trade: Mapping, position: Any,
                          now: datetime) -> float | None:
        """Return the underlying price used by both stock and option exits."""
        profile = str(trade.get("execution_profile", "shares")).lower()
        if profile != "options":
            direct = self._number(_value(position, "current_price",
                                         _value(position, "price", None)))
            if direct is not None and direct > 0:
                return direct
        underlying = str(trade.get("underlying_symbol") or
                         _value(position, "symbol", "")).upper()
        if not underlying:
            return None
        try:
            rows = self.market.stock_quotes(
                [underlying], start=now - timedelta(minutes=2), end=now)
            values = rows.get(underlying, []) if isinstance(rows, Mapping) else []
            quote = self._quote_mapping(values[-1], underlying) if values else {}
        except Exception:  # noqa: BLE001
            return None
        timestamp = self._timestamp(quote.get("timestamp"))
        maximum = float(self.cfg.get("execution", {}).get(
            "max_market_data_age_seconds", 30) or 30)
        if timestamp is None or timestamp > now or (now - timestamp).total_seconds() > maximum:
            return None
        bid = self._number(quote.get("bid", quote.get("bid_price")))
        ask = self._number(quote.get("ask", quote.get("ask_price")))
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return None
        return (bid + ask) / 2.0

    def _monitor_positions(self, now: datetime, positions: list[Any]) -> dict[str, Any]:
        """Evaluate persisted protection on every cycle and close safely."""
        runtime = state.load_state()
        active = runtime.get("active_trades", {}) if isinstance(runtime, Mapping) else {}
        force_flat = self.market.should_force_flat(now)
        closed = []
        failed = []
        changed = False
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            trade = active.get(symbol, {}) if isinstance(active, Mapping) else {}
            if isinstance(trade, Mapping) and trade.get("closing_order_id"):
                saved_order = runtime.get("orders", {}).get(
                    str(trade.get("closing_order_id")), {})
                saved_status = str(saved_order.get("status", "")).lower() \
                    if isinstance(saved_order, Mapping) else ""
                # Do not send another close while the prior one is live.  A
                # rejected/cancelled/not-found/filled terminal order may be
                # retried if the broker still reports residual quantity.
                if saved_status and saved_status not in _TERMINAL_ORDER_STATUSES:
                    continue
            price = self._protection_price(trade, position, now) if isinstance(trade, Mapping) else None
            direction = str(trade.get("direction", _value(position, "side", "long"))).lower()
            stop = self._number(trade.get("stop_price"))
            target = self._number(trade.get("target_price"))
            reason = None
            if force_flat:
                reason = "before_close"
            elif not trade or stop is None or target is None:
                reason = "protection_missing"
            elif price is None:
                reason = "protection_data_unavailable"
            elif price is not None and direction in {"long", "buy"}:
                if stop is not None and price <= stop:
                    reason = "stop"
                elif target is not None and price >= target:
                    reason = "target"
            elif price is not None:
                if stop is not None and price >= stop:
                    reason = "stop"
                elif target is not None and price <= target:
                    reason = "target"
            if reason:
                try:
                    prior_attempt = self._number(trade.get("closing_attempt")) \
                        if isinstance(trade, Mapping) else None
                    attempt = int(prior_attempt) + 1 if prior_attempt is not None else 0
                    order = self._close_position(position, reason, attempt=attempt)
                    closed.append({"symbol": symbol, "reason": reason})
                    if isinstance(trade, dict):
                        close_fill = self._number(getattr(order, "filled_avg_price", None))
                        close_order_id = str(getattr(order, "id", None) or
                                             self._client_id("close", {
                                                 "symbol": symbol, "reason": reason}))
                        trade.update({"status": "closing", "closing_reason": reason,
                                      "closing_price": close_fill,
                                      "closing_trigger_price": price,
                                      "closing_order_id": close_order_id,
                                      "closing_attempt": attempt,
                                      "updated_ts": time.time()})
                        active[symbol] = trade
                        runtime.setdefault("orders", {})[close_order_id] = {
                            "order_id": close_order_id, "symbol": symbol,
                            "status": str(getattr(order, "status", "submitted") or
                                          "submitted").lower(),
                            "client_order_id": getattr(order, "client_order_id", None),
                            "qty": str(_value(position, "qty", 0)),
                            "side": "sell" if str(_value(position, "side", "long")).lower()
                            in {"long", "buy"} else "buy",
                            "action": "close", "reason": reason,
                            "updated_ts": time.time(),
                        }
                        changed = True
                except Exception as exc:  # noqa: BLE001
                    failed.append({"symbol": symbol, "reason": reason,
                                   "error": str(exc)})
                    self._event("close_failed", {"symbol": symbol, "reason": reason,
                                                  "error": str(exc)})
        if changed:
            runtime["active_trades"] = active
            state.save_state(runtime)
        return {"closed": closed, "failed": failed, "force_flat": force_flat}

    def reconcile(self):
        if hasattr(self.provider, "reconcile"):
            result = self.provider.reconcile()
        else:
            result = {"positions": self.provider.positions(), "orders": self.provider.orders()}
        positions = result.get("positions", []) if isinstance(result, Mapping) else []
        broker_orders = result.get("orders", []) if isinstance(result, Mapping) else []
        current = state.load_state()
        previous = current.get("active_trades", {})
        previous = previous if isinstance(previous, Mapping) else {}
        order_state = current.get("orders", {})
        order_state = order_state if isinstance(order_state, dict) else {}
        by_id = {str(getattr(order, "id", "")): order for order in broker_orders
                 if getattr(order, "id", None)}
        by_client = {str(getattr(order, "client_order_id", "")): order
                     for order in broker_orders if getattr(order, "client_order_id", None)}
        for key, saved in list(order_state.items()):
            if not isinstance(saved, dict):
                continue
            broker_order = by_id.get(str(saved.get("order_id") or key)) or \
                by_client.get(str(saved.get("client_order_id") or ""))
            if broker_order is None:
                if str(saved.get("status", "")).lower() not in _TERMINAL_ORDER_STATUSES:
                    misses = int(saved.get("not_found_count", 0) or 0) + 1
                    saved.update({"not_found_count": misses,
                                  "updated_ts": time.time()})
                    # The order endpoint can lag a successful submission.
                    # Require repeated complete reconciliation misses before
                    # treating the id as terminal and eligible for retry.
                    if misses >= 3:
                        saved["status"] = "not_found"
                continue
            old_status = str(saved.get("status", ""))
            status = str(getattr(broker_order, "status", old_status) or old_status).lower()
            filled_qty = self._number(getattr(broker_order, "filled_qty", None)) or 0.0
            fill_price = self._number(getattr(broker_order, "filled_avg_price", None))
            # Broker order snapshots and position snapshots can settle in a
            # different order.  Never regress durable fill evidence to an
            # older accepted/new view returned by a lagging order endpoint.
            if old_status == "filled" and status != "filled":
                status = old_status
                filled_qty = self._number(saved.get("filled_qty")) or filled_qty
                fill_price = self._number(saved.get("filled_avg_price")) or fill_price
            elif (old_status == "partially_filled" and
                  status not in _FILLED_ORDER_STATUSES):
                status = old_status
                filled_qty = max(filled_qty,
                                 self._number(saved.get("filled_qty")) or 0.0)
                fill_price = self._number(saved.get("filled_avg_price")) or fill_price
            elif (old_status in _TERMINAL_ORDER_STATUSES and
                  status not in _TERMINAL_ORDER_STATUSES):
                status = old_status
            saved.update({"status": status, "filled_qty": filled_qty,
                          "filled_avg_price": fill_price, "not_found_count": 0,
                          "updated_ts": time.time()})
            logged_qty = self._number(saved.get("logged_filled_qty")) or 0.0
            if (filled_qty > 0 and status in _FILLED_ORDER_STATUSES and
                    saved.get("risk_plan") and
                    (not saved.get("fill_logged") or filled_qty > logged_qty)):
                self._activate_filled_trade(current, saved, filled_qty, fill_price)
            if status != old_status:
                state.log_order(
                    broker_order, None, action="reconcile", run_id=self.run_id,
                    runtime_mode="paper", account_fingerprint=current.get("account_fingerprint"),
                    setup_id=(saved.get("risk_plan") or {}).get("setup_id")
                    if isinstance(saved.get("risk_plan"), Mapping) else None)

        pending_by_symbol: dict[str, list[dict]] = {}
        for saved in order_state.values():
            if isinstance(saved, dict) and saved.get("risk_plan"):
                pending_by_symbol.setdefault(str(saved.get("symbol", "")).upper(), []).append(saved)
        for rows in pending_by_symbol.values():
            rows.sort(key=lambda item: float(item.get("updated_ts", 0) or 0), reverse=True)

        active = {}
        for position in positions:
            symbol = str(_value(position, "symbol", "")).upper()
            if not symbol:
                continue
            item = dict(previous.get(symbol, {}))
            pending = pending_by_symbol.get(symbol, [None])[0]
            if not item and isinstance(pending, dict):
                pending["status"] = "filled"
                pending["filled_qty"] = self._number(_value(position, "qty", 0)) or 0.0
                pending["filled_avg_price"] = self._number(
                    _value(position, "avg_entry_price", None))
                item = self._activate_filled_trade(
                    current, pending, pending["filled_qty"], pending["filled_avg_price"])
            if not item:
                # A broker position without local protection is reconciled as
                # unprotected; the same cycle's monitor closes it fail-closed.
                item = {"symbol": symbol, "underlying_symbol": symbol,
                        "execution_profile": "unknown", "opened_at": time.time(),
                        "status": "unprotected", "risk_usd": 0.0}
                self._event("unprotected_position", {"symbol": symbol})
            item.update({"symbol": symbol, "qty": str(_value(position, "qty", 0)),
                         "direction": str(_value(position, "side", "long")).lower(),
                         "current_price": str(_value(position, "current_price", "")),
                         "status": "closing" if item.get("closing_reason") else "open",
                         "updated_ts": time.time()})
            active[symbol] = item
        for symbol, trade in previous.items():
            if symbol in active or not isinstance(trade, Mapping):
                continue
            qty = self._number(trade.get("qty")) or 0.0
            entry = self._number(trade.get("entry_price"))
            exit_price = self._number(trade.get("closing_price"))
            closing_order = by_id.get(str(trade.get("closing_order_id") or ""))
            if exit_price is None and closing_order is not None:
                exit_price = self._number(getattr(closing_order, "filled_avg_price", None))
            multiplier = self._number(trade.get("contract_multiplier")) or 1.0
            realized = None
            sign = -1.0 if str(trade.get("position_side", "long")) == "short" else 1.0
            if entry is not None and exit_price is not None:
                realized = (exit_price - entry) * qty * multiplier * sign
            pnl_pct = ((exit_price - entry) / entry * 100.0 * sign
                       if entry and exit_price is not None else None)
            state.log_trade(
                symbol, "sell" if str(trade.get("position_side", "long")) == "long" else "buy",
                "close", qty, price=exit_price, reason=trade.get("closing_reason", "broker_reconcile"),
                realized_pnl_usd=realized, pnl_pct=pnl_pct,
                close_trigger=trade.get("closing_reason", "broker_reconcile"),
                setup_id=trade.get("setup_id"), setup_type=trade.get("setup_type"),
                strategy_id=trade.get("strategy_id"),
                strategy_version=trade.get("strategy_version"),
                variant_id=trade.get("variant_id"), runtime_mode="paper",
                account_fingerprint=current.get("account_fingerprint"), run_id=self.run_id)
            self._record_edge_outcome(trade, realized, pnl_pct, exit_price)
            current.setdefault("protection", {}).pop(symbol, None)
        current["active_trades"] = active
        current["orders"] = order_state
        current["last_reconciliation_ts"] = time.time()
        state.save_state(current)
        self._runtime_state = current
        self._reconciled = True
        self._event("rest_reconcile", {"positions": len(result.get("positions", [])) if isinstance(result, Mapping) else 0})
        return result

    def _record_edge_outcome(self, trade: Mapping, realized: float | None,
                             pnl_pct: float | None, exit_price: float | None) -> None:
        variant_id = trade.get("variant_id")
        if not variant_id or realized is None:
            return
        vehicle = "option" if str(trade.get("execution_profile")) == "options" else "equity"
        risk_usd = self._number(trade.get("risk_usd"))
        opened = self._number(trade.get("opened_at")) or time.time()
        outcome = {
            "variant_id": variant_id, "vehicle": vehicle,
            "opportunity_id": trade.get("setup_id") or
                              f"{trade.get('symbol')}:{opened:.6f}",
            "session_date": datetime.fromtimestamp(opened, timezone.utc).date().isoformat(),
            "net_pnl": realized, "pnl_pct": pnl_pct,
            "r_multiple": (realized / risk_usd if risk_usd and risk_usd > 0 else None),
            "entry_price": trade.get("entry_price"), "exit_price": exit_price,
            "reason": trade.get("closing_reason", "broker_reconcile"),
            "paper": True,
        }
        try:
            from .edge import record_paper_outcome
            record_paper_outcome(outcome, db_path=self._edge_db_path or None)
        except Exception as exc:  # noqa: BLE001
            self._event("edge_outcome_failed", {"error": str(exc),
                                                 "variant_id": variant_id})

    def flatten_all(self, reason: str = "operator") -> bool:
        temporary = False
        if self._lock_handle is None:
            if not self._acquire_lock():
                self._event("flatten_blocked", {"reason": "runtime_lock_held"})
                return False
            temporary = True
        try:
            return self._flatten_all_impl(reason)
        finally:
            if temporary:
                self._release_lock()

    def _flatten_all_impl(self, reason: str = "operator") -> bool:
        try:
            self.provider.cancel_all_orders()
        except Exception as exc:  # noqa: BLE001
            self._event("flatten_cancel_error", {"reason": str(exc)})
            return False
        submitted: dict[str, dict[str, Any]] = {}
        retryable = {"canceled", "cancelled", "expired", "rejected",
                     "failed", "not_found"}
        for poll in range(6):
            positions = self.provider.positions()
            if not positions:
                try:
                    self.reconcile()
                except Exception as exc:  # noqa: BLE001
                    self._event("flatten_reconcile_error", {"reason": str(exc)})
                    return False
                self._event("flatten_confirmed", {"reason": reason, "poll": poll})
                return True
            for position in positions:
                qty = abs(Decimal(str(_value(position, "qty", 0))))
                if qty <= 0:
                    continue
                symbol = str(_value(position, "symbol", "")).upper()
                prior = submitted.get(symbol)
                if prior is not None:
                    saved = state.load_state().get("orders", {}).get(
                        str(prior.get("order_id")), {})
                    status = str(saved.get("status", prior.get("status", ""))).lower() \
                        if isinstance(saved, Mapping) else str(prior.get("status", "")).lower()
                    # Never duplicate a live or filled close.  Only an
                    # explicitly failed terminal order earns a new client id.
                    if status not in retryable:
                        continue
                    close_attempt = int(prior.get("attempt", 0)) + 1
                else:
                    close_attempt = 0
                close = getattr(self.provider, "close_position", None)
                if callable(close):
                    client_order_id = self._client_id(
                        "flatten", {"symbol": symbol, "setup_id": reason},
                        attempt=close_attempt)
                    try:
                        order = close(symbol, qty=qty, client_order_id=client_order_id,
                                      order_type="market", time_in_force="day")
                    except TypeError:
                        try:
                            order = close(symbol, qty=qty)
                        except TypeError:
                            order = close(symbol)
                    state.log_order(order, None, action="flatten", reason=reason,
                                    symbol=symbol, qty=float(qty), runtime_mode="paper",
                                    run_id=self.run_id)
                else:
                    side = "sell" if str(_value(position, "side", "long")).lower() in {"long", "buy"} else "buy"
                    request = OrderRequest(symbol, qty, side, type="market", time_in_force="day",
                                           client_order_id=self._client_id(
                                               "flatten", {"symbol": symbol,
                                                           "setup_id": reason},
                                               close_attempt))
                    order = self.provider.submit_order(request)
                    state.log_order(order, request, action="flatten", reason=reason,
                                    runtime_mode="paper", run_id=self.run_id)
                order_id = str(getattr(order, "id", None) or client_order_id
                               if callable(close) else
                               getattr(order, "id", None) or request.client_order_id)
                status = str(getattr(order, "status", "submitted") or "submitted").lower()
                current = state.load_state()
                current.setdefault("orders", {})[order_id] = {
                    "order_id": order_id, "symbol": symbol,
                    "status": status,
                    "client_order_id": (client_order_id if callable(close)
                                        else request.client_order_id),
                    "qty": str(qty), "action": "flatten", "reason": reason,
                    "attempt": close_attempt, "updated_ts": time.time(),
                }
                state.save_state(current)
                submitted[symbol] = {"order_id": order_id, "status": status,
                                     "attempt": close_attempt}
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001
                self._event("flatten_reconcile_error", {"reason": str(exc)})
                return False
            if poll < 5:
                time.sleep(0.25)
        residual = self.provider.positions()
        self._event("flatten_incomplete", {"reason": reason, "residual": _plain(residual)})
        return not residual

    def request_shutdown(self, reason: str = "shutdown") -> None:
        self.shutdown_reason = reason
        self.running = False
        try:
            state.commit({"operator_pause": True}, transition=(state.RUNNING, state.PAUSED))
            state.write_heartbeat("pausing", run_id=self.run_id, reason=reason)
        except Exception:
            pass

    def close(self) -> None:
        """Release the process lock and leave a truthful terminal heartbeat."""
        self.running = False
        try:
            state.write_heartbeat("stopped", run_id=self.run_id,
                                  reason=self.shutdown_reason or "closed")
        except Exception:
            pass
        self._release_lock()

    def run(self, *, max_cycles: int | None = None) -> None:
        if not self._acquire_lock(persistent=True):
            raise AlpacaError("another paper runtime process already holds the run lock")
        runtime = state.load_state()
        if runtime.get("state") == state.KILLED:
            self._release_lock()
            raise AlpacaError(runtime.get("kill_reason") or "paper runtime is killed")
        runtime["state"] = state.RUNNING
        runtime["operator_pause"] = False
        state.save_state(runtime)
        if not self._ensure_order_ready():
            self._release_lock()
            raise AlpacaError(self._preflight_error or
                              "runtime readiness and startup reconciliation are required")
        self.running = True
        cycles = 0
        run_failure: BaseException | None = None
        interval = float(self.cfg.get("cycle", {}).get("interval_seconds", 60))
        try:
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
        except BaseException as exc:
            run_failure = exc
            raise
        finally:
            exit_reason = self.shutdown_reason or "run_exit"
            flatten_failure: AlpacaError | None = None
            try:
                complete = self._flatten_all_impl(exit_reason)
                if not complete:
                    state.write_heartbeat(
                        "degraded", run_id=self.run_id,
                        reason="shutdown_flatten_incomplete")
                    flatten_failure = AlpacaError(
                        "shutdown flatten incomplete; paper positions may remain")
            except Exception as exc:  # noqa: BLE001
                self._event("shutdown_flatten_failed", {
                    "reason": exit_reason, "error": str(exc)})
                flatten_failure = AlpacaError(
                    f"shutdown flatten failed; paper positions may remain: {exc}")
            self.close()
            if flatten_failure is not None and run_failure is None:
                raise flatten_failure
