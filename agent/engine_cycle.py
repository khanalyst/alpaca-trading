"""One complete market orchestration cycle for the engine facade.

The cycle mixin is intentionally independent from :class:`Engine`.  The
forwarding helpers resolve legacy callables through :mod:`agent.engine` at
call time, preserving existing patch seams without introducing an import
cycle.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from . import state
from .alpaca_provider import AlpacaError
from .execution_lifecycle import (
    _TERMINAL_ORDER_STATUSES,
    _plain,
    _value,
)


def generate_ibr_signal(*args, **kwargs):
    """Resolve the legacy engine facade alias at call time."""
    from . import engine
    return engine.generate_ibr_signal(*args, **kwargs)


def generate_rule_signal(*args, **kwargs):
    """Resolve the legacy engine facade alias at call time."""
    from . import engine
    return engine.generate_rule_signal(*args, **kwargs)


def build_setup_plan(*args, **kwargs):
    """Resolve the legacy engine facade alias at call time."""
    from . import engine
    return engine.build_setup_plan(*args, **kwargs)


class EngineCycleMixin:
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

    def _allocate_edges(self, edge_configs: list, *, free_slots: int) -> list:
        """Order and bound concurrently proved edges by evidence and correlation.

        Only ``all_proved`` paper selection allocates: a pinned ``specific``
        variant is one record and is returned untouched.  Allocation never
        admits anything the sequential path would have refused — it can only
        drop or reorder candidates, and every per-order risk check downstream
        still runs — so no configured cap can be exceeded here.
        """
        records = [record for record, _cfg in edge_configs]
        if (len(edge_configs) < 2 or self.mode == "live" or
                getattr(self, "_edge_selection_mode", "specific") == "specific" or
                any(record is None for record in records)):
            return edge_configs
        try:
            from .allocation import allocate
            result = allocate(records, free_slots=free_slots,
                              db_path=self._edge_db_path or None)
        except Exception as exc:  # noqa: BLE001
            # An unreadable evidence corpus is not a licence to trade every
            # candidate; keep the single best-ranked edge only.
            from .allocation import evidence_rank
            best = max(edge_configs, key=lambda item: evidence_rank(item[0]))
            self._event("allocation_failed", {"error": str(exc),
                                              "variant_id": best[0].get("variant_id")})
            return [best]
        by_candidate = {str(record.get("candidate_id") or ""): (record, cfg)
                        for record, cfg in edge_configs}
        for row in result["rejected"]:
            self._event("allocation_reject", dict(row))
        admitted = [by_candidate[str(record.get("candidate_id") or "")]
                    for record in result["admitted"]]
        self._event("allocation", {
            "free_slots": max(0, free_slots),
            "admitted": [record.get("variant_id") for record, _cfg in admitted],
            "rejected": [row.get("variant_id") for row in result["rejected"]]})
        return admitted

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
        edge_configs = self._allocate_edges(
            edge_configs,
            free_slots=int(risk_cfg.get("max_concurrent_positions", 1) or 1) -
            (len(positions) + pending_position_count))
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
                # Bind the exact immutable edge proof that authorized this
                # entry.  The ledger may advance to a newer verified run
                # before the position closes; deriving this later from the
                # candidate would silently attribute the outcome to the wrong
                # proof epoch.  Persist the snapshot alongside the order
                # before any broker submission can be recorded.
                if isinstance(edge_record, Mapping):
                    risk_plan = dict(risk_plan)
                    candidate_id = edge_record.get("candidate_id")
                    if candidate_id is not None:
                        risk_plan["candidate_id"] = str(candidate_id)
                    latest_proof = edge_record.get("latest_proof")
                    if isinstance(latest_proof, Mapping):
                        proof_run_id = latest_proof.get("run_id")
                        if proof_run_id is not None:
                            risk_plan["proof_run_id"] = str(proof_run_id)
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
