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
from .market_entry_risk import MarketEntryRiskMixin

log = logging.getLogger("engine")


class Engine(ExecutionLifecycleMixin, RuntimeControlMixin, StartupEdgePolicyMixin,
             MarketEntryRiskMixin):
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
