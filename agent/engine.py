"""Deterministic, mode-scoped orchestration for proved strategies."""

from __future__ import annotations

import json
import hashlib
import logging
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from . import state
from .alpaca_domain import OrderRequest
from .alpaca_provider import AlpacaError, AlpacaProvider
from .alpaca_session import SessionPolicy, trading_env_guard
from .brain import DecisionBrain
from .contracts.ibr import generate_ibr_signal
from .contracts.rule import generate_rule_signal
from .market import MarketData
from .risk import RiskEngine
from .strategy import build_setup_plan
from .instruments import (validate_asset_class, validate_equity_symbol,
                          validate_instrument)
from .execution_lifecycle import (
    ExecutionLifecycleMixin,
    _FILLED_ORDER_STATUSES,
    _TERMINAL_ORDER_STATUSES,
    _plain,
    _value,
)
from .runtime_control import RuntimeControlMixin
from .startup_edge_policy import StartupEdgePolicyMixin

log = logging.getLogger("engine")


class Engine(ExecutionLifecycleMixin, RuntimeControlMixin, StartupEdgePolicyMixin):
    def __init__(self, cfg: dict, light: bool = False, *, provider=None,
                 market_data: MarketData | None = None, brain=None):
        self.cfg = cfg
        broker = cfg.get("broker", {}) if isinstance(cfg, Mapping) else {}
        self.mode = str(cfg.get("mode", "paper")).lower()
        if self.mode == "demo":
            self.mode = "paper"
        if self.mode not in {"paper", "live"}:
            raise AlpacaError("mode must be paper or live")
        expected_paper = self.mode == "paper"
        paper = broker.get("paper", expected_paper)
        allow_live = broker.get("allow_live", False)
        if (self.mode == "paper" and (paper is not True or allow_live is not False)):
            raise AlpacaError("paper mode requires paper=true and allow_live=false")
        if (self.mode == "live" and (paper is not False or allow_live is not True)):
            raise AlpacaError("live mode requires paper=false and allow_live=true")
        try:
            trading_env_guard(paper=paper, allow_live=allow_live)
        except ValueError as exc:
            raise AlpacaError(str(exc)) from exc
        strategy_cfg = cfg.get("strategy", {}) if isinstance(cfg.get("strategy"), Mapping) else {}
        self._edge_selection_mode = str(
            strategy_cfg.get("selection_mode") or
            ("specific" if self.mode == "live" else "all_proved"))
        if self.mode == "live":
            requested = str(strategy_cfg.get("variant_id") or "").strip()
            if (not requested or requested.lower() == "auto" or
                    self._edge_selection_mode != "specific"):
                raise AlpacaError("live mode requires one named specific strategy variant")
        research_cfg = cfg.get("research", {}) if isinstance(cfg, Mapping) else {}
        if self.mode == "live" and (
                not isinstance(research_cfg, Mapping) or
                research_cfg.get("enabled") is not True or
                research_cfg.get("require_validated_variant") is not True):
            raise AlpacaError("live mode requires the validated research edge gate")
        self._edge_base_cfg = deepcopy(cfg)
        self._edge_requested_variant = str(
            cfg.get("strategy", {}).get("variant_id") or "")
        self._edge_db_path = research_cfg.get("db_path") if isinstance(research_cfg, Mapping) else None
        self._edge_required = bool(
            isinstance(research_cfg, Mapping) and research_cfg.get("enabled", False) and
            research_cfg.get("require_validated_variant", True))
        self._edge_record: dict | None = None
        self._edge_records: list[dict] = []
        self._edge_configs: list[tuple[dict | None, dict]] = []
        self._edge_pinned_candidate_id: str | None = None
        self._edge_pinned_config_hash: str | None = None
        self._edge_pinned_runtime_cfg: dict | None = None
        self._edge_error: str | None = None
        if isinstance(research_cfg, Mapping) and research_cfg.get("enabled", False):
            try:
                from .edge import (apply_variant, resolve_validated_variant,
                                   resolve_validated_variants)
                if self.mode == "live" or self._edge_selection_mode == "specific":
                    record = resolve_validated_variant(
                        cfg, db_path=self._edge_db_path or None)
                    records = [record] if record is not None else []
                else:
                    records = resolve_validated_variants(
                        cfg, db_path=self._edge_db_path or None)
                self._edge_records = records
                self._edge_record = records[0] if records else None
                self._edge_configs = [(record, apply_variant(cfg, record))
                                      for record in records]
                if len(self._edge_configs) == 1:
                    self.cfg = self._edge_configs[0][1]
                if self.mode == "live" and len(records) == 1:
                    self._edge_pinned_candidate_id = str(records[0].get("candidate_id") or "")
                    self._edge_pinned_config_hash = str(records[0].get("config_hash") or "")
                    self._edge_pinned_runtime_cfg = deepcopy(self._edge_configs[0][1])
                if not records and self._edge_required:
                    vehicle = str(cfg.get("strategy", {}).get("execution_mode", "shares"))
                    self._edge_error = f"no latest-passing validated edge for {vehicle}"
                elif not records:
                    self._edge_configs = [(None, cfg)]
            except Exception as exc:  # noqa: BLE001
                self._edge_error = f"edge resolution failed: {exc}"
        else:
            self._edge_configs = [(None, cfg)]
        self.provider = provider or AlpacaProvider(cfg)
        provider_paper = getattr(self.provider, "paper", None)
        if (not isinstance(provider_paper, bool) or
                provider_paper is not expected_paper):
            raise AlpacaError(f"provider is not {self.mode} scoped")
        session_cfg = cfg.get("session", {})
        self.market = market_data or MarketData(self.provider, SessionPolicy(**session_cfg))
        llm_cfg = cfg.get("llm", {})
        llm_enabled = bool(llm_cfg.get("enabled", False)) if isinstance(llm_cfg, Mapping) else False
        self.brain = brain or (DecisionBrain(llm_cfg) if llm_enabled and not light else None)
        self.risk = RiskEngine(self._edge_base_cfg)
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
        self._startup_cleanup_checked = False
        self._assets: dict[str, Any] = {}
        try:
            state.configure_runtime(self.mode)
            state.ensure_ready()
            self._runtime_state = state.load_state()
            state.write_heartbeat("starting", run_id=self.run_id)
            self._state_ready = True
        except Exception as exc:  # noqa: BLE001
            log.warning("state startup unavailable: %s", exc)
        if not light:
            if not self._acquire_lock():
                self._preflight_error = "runtime lock held during startup reconciliation"
                log.warning(self._preflight_error)
            else:
                try:
                    self.preflight()
                    self.reconcile()
                    self._enforce_intraday_cleanup(
                        self._preflight.get("clock") if self._preflight else None,
                        reason="startup_reconciliation", force=True)
                except Exception as exc:  # noqa: BLE001
                    # A REST reconciliation failure must prevent entries, but
                    # it need not make a status/check command unusable.
                    log.warning("startup reconciliation unavailable: %s", exc)
                    self._preflight_error = str(exc)
                finally:
                    self._release_lock()

    def check(self, authenticated: bool = False) -> dict[str, Any]:
        result = {
            "mode": self.mode, "paper": self.mode == "paper",
            "credentials_configured": bool(self.provider.session.api_key and self.provider.session.secret_key),
            "authenticated": False,
            "edge_required": self._edge_required,
            "edge_ready": self._edge_record is not None,
            "edge_error": self._edge_error,
            "variant_id": (self._edge_record or {}).get("variant_id"),
            "variant_ids": [record.get("variant_id") for record in self._edge_records],
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
        edge_cfg = cfg or self.cfg
        strategy = edge_cfg.get("strategy", {})
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
        except AlpacaError:
            # A bounded/direct cycle does not have ``run``'s outer finally to
            # publish a truthful terminal state.  Close this runtime here
            # while preserving the exception for callers.  A persistent run
            # is finalized by ``run`` so it can flatten first.
            if not self._persistent_lock:
                self.close()
            raise
        finally:
            if temporary:
                # ``close`` already releases this handle on the error path;
                # avoid attempting to unlock a closed file object.
                if self._lock_handle is not None:
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
        try:
            now = self._validated_clock_timestamp(clock)
        except Exception as exc:  # noqa: BLE001
            return self._fail_closed("broker_clock_invalid", exc)
        try:
            reconciliation = self.reconcile()
            positions = reconciliation.get("positions", []) if isinstance(reconciliation, Mapping) else []
        except Exception as exc:  # noqa: BLE001
            self._reconciled = False
            return self._fail_closed("cycle_reconciliation_failed", exc)
        try:
            for position in positions:
                self._required_number(
                    _value(position, "market_value", None),
                    f"position {str(_value(position, 'symbol', '')).upper()} market_value")
        except Exception as exc:  # noqa: BLE001
            return self._fail_closed("position_exposure_invalid", exc)
        if not self._inside_regular_session(clock):
            try:
                self._enforce_intraday_cleanup(
                    clock, reason="outside_regular_session")
            except Exception as exc:  # noqa: BLE001
                return {"action": "hold", "reason": "intraday_cleanup_failed",
                        "error": str(exc)}
            return {"action": "force_flat", "reason": "outside_regular_session",
                    "closed": True, "residual": _plain(self.provider.positions())}
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
            # considered, even if the broker has not filled it yet.
            return {"action": "close", **monitored}
        if not self._refresh_edge():
            return {"action": "hold", "reason": self._edge_error or
                    "validated edge champion is required"}
        if _value(clock, "is_open", None) is not True or not self.market.can_enter(now):
            return {"action": "hold", "reason": "outside_regular_session"}
        if not self._latest_entry_allowed(now):
            return {"action": "hold", "reason": "latest_entry_time_passed"}
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
        try:
            account = self.provider.account()
        except Exception as exc:  # noqa: BLE001
            return self._fail_closed("account_unavailable", exc)
        try:
            daily_pnl, daily_stop = self._update_daily_risk(account, now)
        except Exception as exc:  # noqa: BLE001
            return self._fail_closed("daily_risk_state_invalid", exc)
        if daily_stop:
            complete = self.flatten_all("daily_loss_limit")
            self._event("daily_loss_limit", {"daily_pnl": daily_pnl,
                                              "flatten_complete": complete})
            try:
                residual = _plain(self.provider.positions())
            except Exception as exc:  # noqa: BLE001
                return self._fail_closed("post_risk_positions_unavailable", exc)
            return {"action": "day_stopped", "daily_pnl": daily_pnl,
                    "closed": complete, "residual": residual}
        try:
            positions = self.provider.positions()
        except Exception as exc:  # noqa: BLE001
            return self._fail_closed("post_risk_positions_unavailable", exc)
        portfolio_data = portfolio or {"account": _plain(account), "positions": _plain(positions)}
        placed = []; signals = []; placed_keys: set[tuple[str, str]] = set()
        planned_risk = 0.0; planned_notional = 0.0
        risk_cfg = self._edge_base_cfg.get("risk", {})
        try:
            gross = sum(abs(self._required_number(
                _value(position, "market_value", None),
                f"position {str(_value(position, 'symbol', '')).upper()} market_value"))
                    for position in positions)
        except Exception as exc:  # noqa: BLE001
            return self._fail_closed("position_exposure_invalid", exc)
        runtime = state.load_state()
        if not isinstance(runtime, Mapping):
            return self._fail_closed("durable_risk_state_invalid",
                                    AlpacaError("durable runtime state is malformed"))
        active = runtime.get("active_trades", {})
        if active is None:
            active = {}
        if not isinstance(active, Mapping):
            return self._fail_closed("durable_risk_state_invalid",
                                    AlpacaError("active trade state is malformed"))
        orders_state = runtime.get("orders", {})
        if orders_state is None:
            orders_state = {}
        if not isinstance(orders_state, Mapping):
            return self._fail_closed("durable_risk_state_invalid",
                                    AlpacaError("order state is malformed"))
        pending_keys: set[tuple[str, str]] = set()
        pending_underlyings: set[str] = set()
        pending_risk = 0.0; pending_notional = 0.0
        try:
            open_risk = 0.0
            if isinstance(active, Mapping):
                for active_symbol, item in active.items():
                    if not isinstance(item, Mapping):
                        raise AlpacaError(f"active trade {active_symbol} is malformed")
                    risk_usd = self._required_number(
                        item.get("risk_usd"), f"active trade {active_symbol} risk_usd")
                    notional = self._required_number(
                        item.get("notional"), f"active trade {active_symbol} notional")
                    if risk_usd < 0 or notional < 0:
                        raise AlpacaError(f"active trade {active_symbol} risk is negative")
                    open_risk += risk_usd
            for item in orders_state.values():
                if not isinstance(item, Mapping):
                    raise AlpacaError("durable order row is malformed")
                if str(item.get("status", "")).lower() in _TERMINAL_ORDER_STATUSES:
                    continue
                action = str(item.get("action", "submit")).lower()
                if action in {"flatten", "close"}:
                    continue
                plan = item.get("risk_plan")
                if not isinstance(plan, Mapping):
                    raise AlpacaError("pending order risk_plan is malformed")
                risk_usd = self._required_number(
                    plan.get("risk_usd"), "pending order risk_usd")
                notional = self._required_number(
                    plan.get("notional"), "pending order notional")
                if risk_usd < 0 or notional < 0:
                    raise AlpacaError("pending order risk is negative")
                underlying = str(plan.get("underlying_symbol") or
                                 item.get("symbol") or "").upper()
                if not underlying:
                    raise AlpacaError("pending order underlying symbol is missing")
                direction = str(plan.get("direction") or
                                ("long" if item.get("side") == "buy" else "short"))
                pending_keys.add((underlying, direction))
                pending_underlyings.add(underlying)
                pending_risk += risk_usd
                pending_notional += notional
        except Exception as exc:  # noqa: BLE001
            return self._fail_closed("durable_risk_state_invalid", exc)
        held_underlyings = {
            str(item.get("underlying_symbol") or item.get("symbol") or "").upper()
            for item in active.values() if isinstance(item, Mapping)
        } if isinstance(active, Mapping) else set()
        pending_position_count = sum(
            1 for underlying in pending_underlyings
            if underlying not in held_underlyings)
        edge_configs = self._edge_configs or ([(None, self._edge_base_cfg)]
                                               if not self._edge_required else [])
        for edge_record, edge_cfg in edge_configs:
            if not self._latest_entry_allowed(now, edge_cfg):
                continue
            edge_minutes = int(edge_cfg.get("strategy", {}).get(
                "force_flat_minutes_before_close",
                edge_cfg.get("session", {}).get("force_flat_minutes_before_close", 10)))
            edge_force_flat_at = (session_close - timedelta(minutes=max(0, edge_minutes))
                                  if session_close is not None else force_flat_at)
            for symbol in symbols:
                row = rows.get(symbol)
                if not row:
                    continue
                bars = row.get("bars", [])
                if str(edge_cfg.get("strategy", {}).get("id")) == "rule":
                    signal = generate_rule_signal(symbol, bars, config=edge_cfg, now=now)
                else:
                    signal = generate_ibr_signal(
                        symbol, bars, config=edge_cfg.get("strategy", {}), now=now)
                if signal is None:
                    continue
                signal = dict(signal)
                if edge_force_flat_at is not None:
                    signal["force_flat_at"] = edge_force_flat_at.isoformat()
                    signal["force_flat_ts"] = edge_force_flat_at.timestamp()
                signal.update({"symbol": symbol,
                               "relative_volume": signal.get("relative_volume"),
                               "spread_bps": row.get("spread_bps"),
                               "stale": bool(row.get("stale", False)),
                               "quote_stale": bool(row.get("quote_stale", False))})
                signal_entry = signal.get("entry_price")
                plan_snapshot = {
                    **row,
                    # Strategy planning and execution slippage must share the
                    # signal's reference entry, not the current quote ask.
                    "price": signal_entry,
                    "entry_price": signal_entry,
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
                plan, why = build_setup_plan(signal, plan_snapshot, edge_cfg)
                if plan is None:
                    self._event("setup_reject", {"symbol": symbol, "reason": why})
                    continue
                signals.append(plan)
                if not self._llm_allows(plan, row, portfolio_data):
                    continue
                key = (symbol, str(plan.get("direction") or ""))
                if key in placed_keys or symbol in pending_underlyings:
                    self._event("entry_duplicate_blocked", {
                        "symbol": symbol, "direction": key[1],
                        "variant_id": (edge_record or {}).get("variant_id")})
                    continue
                try:
                    sized = self._risk_order(
                        symbol, plan, row, account, positions, now, cfg=edge_cfg)
                except AlpacaError as exc:
                    return self._fail_closed("durable_risk_state_invalid", exc)
                if sized is None:
                    continue
                request, risk_plan = sized
                try:
                    candidate_risk = self._required_number(
                        risk_plan.get("risk_usd"), "planned risk_usd")
                    candidate_notional = self._required_number(
                        risk_plan.get("notional"), "planned notional")
                    if candidate_risk < 0 or candidate_notional < 0:
                        raise AlpacaError("planned risk or notional is negative")
                except Exception as exc:  # noqa: BLE001
                    return self._fail_closed("planned_risk_invalid", exc)
                max_positions = int(risk_cfg.get("max_concurrent_positions", 1) or 1)
                if (len(positions) + pending_position_count + len(placed) >=
                        max_positions):
                    self._event("risk_reject", {"symbol": symbol,
                                                  "reason": "max concurrent positions reached"})
                    continue
                equity = self._number(_value(account, "equity", 0)) or 0
                risk_cap = self._number(risk_cfg.get(
                    "max_open_risk_pct", risk_cfg.get("max_total_open_risk_pct")))
                if (risk_cap is not None and open_risk + pending_risk + planned_risk +
                        candidate_risk >
                        equity * risk_cap / 100.0 + 1e-9):
                    self._event("risk_reject", {"symbol": symbol,
                                                  "reason": "max open risk cap reached"})
                    continue
                gross_cap = self._number(risk_cfg.get("max_gross_exposure_pct"))
                if (gross_cap is not None and gross + pending_notional + planned_notional +
                        candidate_notional >
                        equity * gross_cap / 100.0 + 1e-9):
                    self._event("risk_reject", {"symbol": symbol,
                                                  "reason": "max gross exposure reached"})
                    continue
                if self._client_order_pending(request.client_order_id):
                    self._event("entry_pending", {"symbol": request.symbol,
                                                    "client_order_id": request.client_order_id})
                    continue
                try:
                    order = self.provider.submit_order(request)
                    placed.append(order); placed_keys.add(key)
                    pending_keys.add((symbol, str(risk_plan.get("direction") or "")))
                    pending_underlyings.add(symbol)
                    planned_risk += candidate_risk
                    planned_notional += candidate_notional
                    try:
                        self._record_open_order(request, order, risk_plan)
                    except Exception as exc:  # noqa: BLE001
                        self._reconciled = False
                        self._preflight_error = (
                            "post-submit durability failure; broker reconciliation required")
                        try:
                            state.commit({"operator_pause": True},
                                         transition=(state.RUNNING, state.PAUSED))
                            state.write_heartbeat(
                                "degraded", run_id=self.run_id,
                                reason="post_submit_durability_failure")
                        except Exception:  # noqa: BLE001
                            pass
                        raise AlpacaError(
                            f"{self._preflight_error}: {exc}") from exc
                except AlpacaError:
                    raise
        try: state.write_heartbeat("running", run_id=self.run_id, orders=len(placed))
        except Exception: pass
        return {"action": "decide", "orders": placed, "signals": signals}
