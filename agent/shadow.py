"""Durable, isolated parameter-variant paper portfolios.

Every configured variant observes the same real-time snapshot but owns its
cash, positions, exposure, cooldowns, losses and circuit-breaker state. The
evaluator can persist research state but has no exchange client and no order
method. Failures are contained by the engine after live decisions commit.
"""

from __future__ import annotations

import hashlib
import math
import tempfile
import time
import uuid
from dataclasses import dataclass, field

from research.findings import (FindingsStore, _content_hash,
                               resolve_store_path, variant_identity_hash)

from . import state as runtime_state, strategy
from .forward_models import require_validated
from .risk import RiskEngine
from .variants import Variant, apply


@dataclass
class ShadowRecord:
    """One attributable forward observation or paper action."""

    variant_id: str
    symbol: str
    signal_ts: int | None
    outcome: str
    direction: str | None = None
    setup_type: str | None = None
    reason: str | None = None
    stop_pct: float | None = None
    take_pct: float | None = None
    notional: float | None = None
    proposal_id: str | None = None
    paper_trade_id: str | None = None
    paper_action: str | None = None
    portfolio_status: str | None = None
    equity_usdt: float | None = None

    def as_event(self) -> dict:
        return {
            "variant_id": self.variant_id,
            "symbol": self.symbol,
            "signal_ts": self.signal_ts,
            "outcome": self.outcome,
            "direction": self.direction,
            "setup_type": self.setup_type,
            "reason": self.reason,
            "stop_pct": self.stop_pct,
            "take_pct": self.take_pct,
            "notional": self.notional,
            "proposal_id": self.proposal_id,
            "paper_trade_id": self.paper_trade_id,
            "paper_action": self.paper_action,
            "portfolio_status": self.portfolio_status,
            "equity_usdt": self.equity_usdt,
        }


@dataclass
class ShadowBudget:
    limit_ms: float
    started: float = field(default_factory=time.monotonic)
    overran: bool = False

    def spent_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000.0

    def exhausted(self) -> bool:
        if self.limit_ms <= 0:
            return False
        if self.spent_ms() >= self.limit_ms:
            self.overran = True
            return True
        return False


class ShadowEvaluator:
    """Evaluate and paper-trade variants without access to an exchange."""

    def __init__(
            self, variants: list, base_cfg: dict, budget_ms: float = 0.0,
            *, store: FindingsStore | None = None,
            scope_key: str = "demo:unscoped",
            initial_balance_usdt: float = 10_000.0,
            max_failures: int = 3) -> None:
        self.budget_ms = float(budget_ms)
        self.scope_key = str(scope_key)
        self.initial_balance_usdt = float(initial_balance_usdt)
        self.max_failures = int(max_failures)
        # Direct construction is intentionally isolated. Runtime construction
        # supplies the durable configured store through build(); tests and
        # one-off callers get a private database instead of contaminating the
        # repository-wide findings store or one another.
        self._temporary_store = None
        if store is None:
            self._temporary_store = tempfile.TemporaryDirectory(
                prefix="okx-shadow-")
            store = FindingsStore(
                f"{self._temporary_store.name}/findings.db")
        self.store = store
        self._configs: dict[str, dict] = {}
        self._engines: dict[str, RiskEngine] = {}
        self._models = {}
        self._provenance: dict[str, dict] = {}
        self._variants: dict[str, Variant] = {}
        self.registration_errors: dict[str, str] = {}
        self.last_coverage: dict = {}

        for variant in variants:
            if not isinstance(variant, Variant):
                raise TypeError(
                    "shadow variants must be Variant instances; got "
                    f"{type(variant).__name__}")
            try:
                cfg = apply(variant, base_cfg)
                model = require_validated(variant.strategy_id)
                self.store.register(variant)
                experiment_config = (
                    runtime_state.experiment_fingerprint_material(cfg))
                provenance = {
                    "variant_definition_hash": variant_identity_hash(variant),
                    "strategy_config_version": (
                        runtime_state.experiment_fingerprint(cfg)),
                    "experiment_config": experiment_config,
                    "code_version": runtime_state.code_fingerprint(),
                    "forward_model_id": model.model_id,
                    "forward_model_assumptions_hash": _content_hash(
                        model.as_dict()),
                }
                # A configured candidate is enrolled only if its persisted
                # evidence belongs to this exact executable experiment.
                self.store.bind_paper_experiment(
                    self.scope_key, variant.variant_id, provenance,
                    self.initial_balance_usdt)
            except Exception as exc:                       # noqa: BLE001
                self.registration_errors[variant.variant_id] = str(exc)
                continue
            self._variants[variant.variant_id] = variant
            self._configs[variant.variant_id] = cfg
            self._engines[variant.variant_id] = RiskEngine(cfg)
            self._models[variant.variant_id] = model
            self._provenance[variant.variant_id] = provenance

    @property
    def variant_ids(self) -> list[str]:
        return sorted(self._configs)

    def held_symbols(self) -> list[str]:
        """Symbols required to mark or close local positions after reranking."""
        held = set()
        for variant_id in self.variant_ids:
            state = self.store.paper_portfolio_state(
                self.scope_key, variant_id) or {}
            held.update(str(position["symbol"])
                        for position in state.get("positions") or []
                        if position.get("symbol"))
        return sorted(held)

    def advance(
            self, snapshot: dict, now: float | None = None) -> list[ShadowRecord]:
        """Mark and resolve every account on every available market snapshot."""
        timestamp = time.time() if now is None else float(now)
        records: list[ShadowRecord] = []
        for variant_id in self.variant_ids:
            try:
                self._advance_account(
                    variant_id, snapshot or {}, timestamp, records)
            except Exception as exc:                       # noqa: BLE001
                self._record_failure(
                    variant_id, "portfolio_advance_failed", exc, timestamp)
        return records

    def evaluate(
            self, snapshot: dict, portfolio: dict | None = None,
            now: float | None = None, cycle_id: str | None = None,
            proposals: list[dict] | None = None,
            advance_accounts: bool = True) -> list:
        """Advance accounts and evaluate one common recorded proposal set.

        ``portfolio`` is accepted for compatibility but deliberately ignored:
        a live account view must never seed or mutate a variant account.
        """
        del portfolio
        timestamp = time.time() if now is None else float(now)
        budget = ShadowBudget(self.budget_ms)
        records = (self.advance(snapshot, timestamp)
                   if advance_accounts else [])
        recorded_opens = [dict(decision) for decision in (proposals or [])
                          if decision.get("action") == "open"]

        accounts: dict[str, tuple[dict, int]] = {}
        for variant_id in self.variant_ids:
            try:
                accounts[variant_id] = self.store.load_paper_portfolio(
                    self.scope_key, variant_id,
                    self.initial_balance_usdt, timestamp)
            except Exception as exc:                       # noqa: BLE001
                self._record_failure(
                    variant_id, "portfolio_load_failed", exc, timestamp)

        order = self.store.scheduler_order(self.scope_key, self.variant_ids)
        evaluated: list[str] = []
        skipped: list[str] = []
        for index, variant_id in enumerate(order):
            if budget.exhausted():
                skipped.extend(order[index:])
                break
            account = accounts.get(variant_id)
            if account is None:
                skipped.append(variant_id)
                continue
            state, version = account
            processed = 0
            try:
                pending_opens: list[dict] = []
                pending_decisions: list[dict] = []
                variant_records: list[ShadowRecord] = []
                for decision in recorded_opens:
                    record = self._evaluate_one(
                        variant_id, snapshot, decision, state, timestamp,
                        cycle_id, pending_opens)
                    variant_records.append(record)
                    if (record.proposal_id
                            and record.reason != "proposal already evaluated"):
                        pending_decisions.append(self._paper_decision(
                            variant_id, cycle_id, decision, record, timestamp))
                    processed += 1
                self._refresh_metrics(state, snapshot, timestamp)
                self._apply_circuit_breakers(
                    state, self._configs[variant_id], timestamp)
                self.store.commit_paper_portfolio(
                    self.scope_key, variant_id, state, version,
                    opened_trades=pending_opens,
                    decisions=pending_decisions, now=timestamp)
                records.extend(variant_records)
                if state.get("status") == "REVOKED":
                    qualification = self.store.qualification_status(
                        variant_id, self.scope_key)
                    if (qualification or {}).get("status") == "QUALIFIED":
                        self.store.revoke_variant(
                            variant_id,
                            {"source": "paper_portfolio",
                             "reason": state.get("revoked_reason")},
                            scope_key=self.scope_key)
                complete = processed == len(recorded_opens)
                (evaluated if complete and processed else skipped).append(
                    variant_id)
            except Exception as exc:                       # noqa: BLE001
                # A failed commit produced no durable learning, even if the
                # evaluator spent CPU on one or more symbols. Keep it least-
                # observed so the scheduler retries it promptly.
                skipped.append(variant_id)
                self._record_failure(
                    variant_id, "variant_evaluation_failed", exc, timestamp)
                records.append(ShadowRecord(
                    variant_id, "*", None, "vetoed",
                    reason=f"shadow error: {type(exc).__name__}: {exc}"))

        skipped.extend(
            variant_id for variant_id in order
            if variant_id not in evaluated and variant_id not in skipped)
        self.store.record_scheduler_cycle(
            self.scope_key, evaluated, skipped, timestamp)
        coverage = self.store.scheduler_coverage(self.scope_key)
        self.last_coverage = {
            "scope_key": self.scope_key,
            "scheduled": len(order),
            "evaluated": evaluated,
            "skipped": skipped,
            "coverage_pct": (len(evaluated) / len(order) * 100.0
                             if order else 100.0),
            "cumulative": coverage,
        }
        self.last_budget = budget
        return records

    def _advance_account(
            self, variant_id: str, snapshot: dict, now: float,
            records: list[ShadowRecord]) -> tuple[dict, int]:
        state, version = self.store.load_paper_portfolio(
            self.scope_key, variant_id, self.initial_balance_usdt, now)
        self._sync_qualification_stage(variant_id, state, now)
        cfg = self._configs[variant_id]
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if state.get("day") != today:
            state["day"] = today
            state["day_start_equity"] = float(state.get("equity_usdt") or 0)
            if state.get("status") == "DAY_STOPPED":
                state["status"] = state.get("resume_status") or "SHADOW"
        state["cooldowns"] = {
            symbol: until for symbol, until in
            (state.get("cooldowns") or {}).items()
            if float(until) > now
        }
        state["seen_proposals"] = {
            proposal_id: ts for proposal_id, ts in
            (state.get("seen_proposals") or {}).items()
            if now - float(ts) <= 7 * 86_400
        }

        kept = []
        closed_trades: list[dict] = []
        resolved_records: list[ShadowRecord] = []
        for position in list(state.get("positions") or []):
            row = snapshot.get(position["symbol"])
            observed = isinstance(row, dict)
            try:
                price = float((row or {}).get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if not math.isfinite(price) or price <= 0:
                observed = False
                state["unpriced_positions"] = int(
                    state.get("unpriced_positions") or 0) + 1
                if now < float(position["deadline_ts"]):
                    kept.append(position)
                    continue
                # A universe change or transient market-data failure may make
                # the symbol unavailable. The pre-registered horizon remains
                # binding: close at the last persisted mark, never carry the
                # position past its deadline indefinitely.
                price = float(
                    position.get("mark_price") or position["entry_price"])
                if not math.isfinite(price) or price <= 0:
                    raise ValueError(
                        f"{position['symbol']}: overdue position has no "
                        "valid persisted mark")
            result = (self._exit_reason(position, price, now)
                      if observed else "timeout")
            if result is None:
                position["mark_price"] = price
                kept.append(position)
                continue
            exit_price = self._exit_price(position, price, result)
            net_pnl, r_multiple = self._paper_pnl(
                position, exit_price, result)
            closed_trades.append({
                "trade_id": position["trade_id"], "exit_ts": now,
                "exit_price": exit_price, "result": result,
                "net_pnl_usd": net_pnl, "r_multiple": r_multiple})
            state["cash_usdt"] = float(state["cash_usdt"]) + net_pnl
            state["realized_pnl_usdt"] = (
                float(state.get("realized_pnl_usdt") or 0) + net_pnl)
            state.get("active_trades", {}).pop(position["symbol"], None)
            if net_pnl < 0:
                state["loss_count"] = int(state.get("loss_count") or 0) + 1
                state["consecutive_losses"] = int(
                    state.get("consecutive_losses") or 0) + 1
                cooldown = float(cfg["risk"]["cooldown_minutes_after_loss"])
                state.setdefault("cooldowns", {})[position["symbol"]] = (
                    now + cooldown * 60)
            else:
                state["win_count"] = int(state.get("win_count") or 0) + 1
                state["consecutive_losses"] = 0
            resolved_records.append(ShadowRecord(
                variant_id, position["symbol"], position.get("signal_ts"),
                "resolved", position.get("direction"),
                position.get("setup_type"), reason=result,
                notional=position.get("notional"),
                proposal_id=position.get("proposal_id"),
                paper_trade_id=position.get("trade_id"),
                paper_action="close"))
        state["positions"] = kept
        self._sync_qualification_stage(variant_id, state, now)
        self._refresh_metrics(state, snapshot, now)
        self._apply_circuit_breakers(state, cfg, now)
        version = self.store.commit_paper_portfolio(
            self.scope_key, variant_id, state, version,
            closed_trades=closed_trades, now=now)
        records.extend(resolved_records)
        return state, version

    @staticmethod
    def _exit_reason(position: dict, price: float, now: float) -> str | None:
        if position["direction"] == "long":
            if price <= float(position["stop_price"]):
                return "stop"
            if price >= float(position["take_price"]):
                return "target"
        else:
            if price >= float(position["stop_price"]):
                return "stop"
            if price <= float(position["take_price"]):
                return "target"
        if now >= float(position["deadline_ts"]):
            return "timeout"
        return None

    @staticmethod
    def _exit_price(position: dict, mark: float, result: str) -> float:
        if result == "stop":
            return float(position["stop_price"])
        if result == "target":
            return float(position["take_price"])
        return mark

    @staticmethod
    def _paper_pnl(
            position: dict, exit_price: float, result: str) -> tuple[float, float]:
        entry = float(position["entry_price"])
        sign = 1.0 if position["direction"] == "long" else -1.0
        gross_pct = sign * (float(exit_price) - entry) / entry * 100.0
        cost_pct = float(position.get("round_trip_cost_pct") or 0)
        if result != "stop":
            cost_pct = max(
                0.0, cost_pct - float(position.get("stop_slippage_pct") or 0))
        net_pct = gross_pct - cost_pct
        pnl = float(position["notional"]) * net_pct / 100.0
        risk = float(position.get("risk_usd") or 0)
        return pnl, (pnl / risk if risk > 0 else 0.0)

    def _evaluate_one(
            self, variant_id: str, snapshot: dict, recorded_decision: dict,
            state: dict, now: float, cycle_id: str | None,
            pending_opens: list[dict]) -> ShadowRecord:
        cfg = self._configs[variant_id]
        engine = self._engines[variant_id]
        decision = dict(recorded_decision)
        symbol = str(decision.get("symbol") or "")
        source_row = snapshot.get(symbol)
        if not isinstance(source_row, dict):
            return self._record(
                variant_id, symbol or "*", None, state, "vetoed",
                decision.get("direction"), decision.get("setup_type"),
                "recorded proposal symbol is absent from the common snapshot")
        row = dict(source_row)
        row["setup_evidence"] = strategy.setup_evidence(row, cfg)
        signal_ts = row.get("signal_ts")
        direction = decision.get("direction")
        setup_type = decision.get("setup_type")
        decision["signal_ts"] = signal_ts
        proposal_id = self._proposal_id(
            variant_id, cycle_id, symbol, signal_ts, str(direction),
            str(setup_type))
        if proposal_id in (state.get("seen_proposals") or {}):
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type,
                "proposal already evaluated", proposal_id=proposal_id)
        plan, why = strategy.build_setup_plan(decision, row, cfg)
        if plan is None:
            state.setdefault("seen_proposals", {})[proposal_id] = now
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type, why, proposal_id=proposal_id)
        merged = dict(decision, **{
            "stop_loss_pct": plan.get("stop_loss_pct"),
            "take_profit_pct": plan.get("take_profit_pct")})
        sized, veto = engine.vet_open(
            merged, float(state.get("equity_usdt") or 0),
            list(state.get("positions") or []), snapshot,
            dict(state.get("cooldowns") or {}),
            float(state.get("gross_notional") or 0),
            active_trades=dict(state.get("active_trades") or {}),
            now=now)
        state.setdefault("seen_proposals", {})[proposal_id] = now
        if state.get("status") not in {"SHADOW", "PAPER"}:
            veto = f"paper portfolio is {state.get('status')}"
            sized = None
        if sized is None:
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type, veto,
                plan.get("stop_loss_pct"), plan.get("take_profit_pct"),
                proposal_id=proposal_id)
        trade_id = self._open_paper_trade(
            variant_id, cycle_id, proposal_id, decision, sized,
            state, now, pending_opens)
        return self._record(
            variant_id, symbol, signal_ts, state, "proposed",
            direction, setup_type, None, sized.get("sl_pct"),
            sized.get("tp_pct"), sized.get("notional"), proposal_id,
            trade_id, "open")

    def _open_paper_trade(
            self, variant_id: str, cycle_id: str | None, proposal_id: str,
            decision: dict, sized: dict, state: dict, now: float,
            pending_opens: list[dict]) -> str:
        entry = float(sized["price"])
        stop_move = entry * float(sized["sl_pct"]) / 100.0
        take_move = entry * float(sized["tp_pct"]) / 100.0
        long = decision["direction"] == "long"
        position = {
            "proposal_id": proposal_id,
            "symbol": decision["symbol"],
            "direction": decision["direction"],
            "side": decision["direction"],
            "setup_type": decision.get("setup_type"),
            "signal_ts": decision.get("signal_ts"),
            "entry_ts": now,
            "entry_price": entry,
            "mark_price": entry,
            "notional": float(sized["notional"]),
            "risk_usd": float(sized["risk_usd"]),
            "stop_price": entry - stop_move if long else entry + stop_move,
            "take_price": entry + take_move if long else entry - take_move,
            "deadline_ts": (
                now + self._models[variant_id].horizon_hours * 3600),
            "round_trip_cost_pct": float(sized["estimated_cost_pct"]),
            "stop_slippage_pct": float(
                self._configs[variant_id]["trading_costs"]
                ["expected_stop_slippage_pct"]),
        }
        trade_id = uuid.uuid4().hex
        pending_opens.append({
            **position,
            "trade_id": trade_id,
            "scope_key": self.scope_key,
            "variant_id": variant_id,
            "cycle_id": cycle_id,
            "model_id": self._models[variant_id].model_id,
            "assumptions": {
                "forward_model": self._models[variant_id].as_dict(),
                "experiment_provenance": self._provenance[variant_id],
            },
        })
        position["trade_id"] = trade_id
        state.setdefault("positions", []).append(position)
        state.setdefault("active_trades", {})[position["symbol"]] = {
            "trade_id": trade_id,
            "risk_usd": position["risk_usd"],
            "opened_at": now,
            "direction": position["direction"],
        }
        return trade_id

    def _paper_decision(
            self, variant_id: str, cycle_id: str | None, proposal: dict,
            record: ShadowRecord, now: float) -> dict:
        """Persist the complete accept/veto action for paired inference."""
        confidence = proposal.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        return {
            "decision_id": uuid.uuid4().hex,
            "proposal_id": record.proposal_id,
            "scope_key": self.scope_key,
            "variant_id": variant_id,
            "cycle_id": cycle_id,
            "symbol": record.symbol,
            "direction": record.direction,
            "setup_type": record.setup_type,
            "signal_ts": record.signal_ts,
            "confidence": confidence,
            "decision_outcome": record.outcome.upper(),
            "reason": record.reason,
            "paper_trade_id": record.paper_trade_id,
            "model_id": self._models[variant_id].model_id,
            "assumptions": {
                "forward_model": self._models[variant_id].as_dict(),
                "experiment_provenance": self._provenance[variant_id],
            },
            "proposal": dict(proposal),
            "decision_ts": now,
        }

    @staticmethod
    def _proposal_id(
            variant_id: str, cycle_id: str | None, symbol: str,
            signal_ts: object, direction: str, setup_type: str) -> str:
        # Signal identity, not wall clock, makes repeated observations of the
        # same completed bar idempotent across process restarts.
        del cycle_id
        del variant_id
        raw = "\0".join(map(str, (
            symbol, signal_ts, direction, setup_type)))
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _sync_qualification_stage(
            self, variant_id: str, state: dict, now: float) -> None:
        """Move a forward-qualified edge into a clean isolated paper stage."""
        qualification = self.store.qualification_status(
            variant_id, self.scope_key)
        status = state.get("status") or "SHADOW"
        if qualification and qualification.get("status") == "REVOKED":
            state["status"] = "REVOKED"
            state["revoked_reason"] = (
                qualification.get("detail", {}).get("reason")
                or "edge qualification revoked")
            return
        if not qualification or qualification.get("status") != "QUALIFIED":
            return
        if status == "SHADOW":
            state["status"] = (
                "PAPER_PENDING" if state.get("positions") else "PAPER")
        if state.get("status") == "PAPER_PENDING" and state.get("positions"):
            return
        if state.get("status") not in {"PAPER", "PAPER_PENDING"}:
            return
        if state.get("paper_started_ts") is not None:
            state["status"] = "PAPER"
            return
        # Start paper on a flat, freshly rebased local account. Shadow history
        # remains in paper_trades and is excluded by paper_started_ts.
        state.update({
            "status": "PAPER",
            "resume_status": "PAPER",
            "paper_started_ts": now,
            "cash_usdt": self.initial_balance_usdt,
            "equity_usdt": self.initial_balance_usdt,
            "realized_pnl_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "high_water_mark": self.initial_balance_usdt,
            "day_start_equity": self.initial_balance_usdt,
            "gross_notional": 0.0,
            "net_notional": 0.0,
            "open_risk_usdt": 0.0,
            "cooldowns": {},
            "active_trades": {},
            "loss_count": 0,
            "consecutive_losses": 0,
            "win_count": 0,
            "max_drawdown_pct": 0.0,
            "failure_count": 0,
            "revoked_reason": None,
        })

    @staticmethod
    def _refresh_metrics(state: dict, snapshot: dict, now: float) -> None:
        unrealized = gross = net = open_risk = 0.0
        for position in state.get("positions") or []:
            row = snapshot.get(position["symbol"])
            mark = float((row or {}).get("price") or position["mark_price"])
            position["mark_price"] = mark
            entry = float(position["entry_price"])
            sign = 1.0 if position["direction"] == "long" else -1.0
            unrealized += (
                float(position["notional"]) * sign * (mark - entry) / entry)
            gross += abs(float(position["notional"]))
            net += (float(position["notional"])
                    if position["direction"] == "long"
                    else -float(position["notional"]))
            open_risk += float(position.get("risk_usd") or 0)
        state["unrealized_pnl_usdt"] = unrealized
        state["equity_usdt"] = float(state.get("cash_usdt") or 0) + unrealized
        state["gross_notional"] = gross
        state["net_notional"] = net
        state["open_risk_usdt"] = open_risk
        state["last_mark_ts"] = now

    @staticmethod
    def _apply_circuit_breakers(state: dict, cfg: dict, now: float) -> None:
        equity = float(state.get("equity_usdt") or 0)
        high = max(float(state.get("high_water_mark") or equity), equity)
        state["high_water_mark"] = high
        drawdown = ((high - equity) / high * 100.0 if high > 0 else 100.0)
        state["max_drawdown_pct"] = max(
            float(state.get("max_drawdown_pct") or 0), drawdown)
        if drawdown >= float(cfg["risk"]["max_drawdown_pct"]):
            state["status"] = "REVOKED"
            state["revoked_reason"] = f"max drawdown {drawdown:.2f}%"
        day_start = float(state.get("day_start_equity") or equity)
        day_pnl = ((equity - day_start) / day_start * 100.0
                   if day_start > 0 else -100.0)
        state["day_pnl_pct"] = day_pnl
        if (state.get("status") in {"SHADOW", "PAPER"}
                and day_pnl <= -float(cfg["risk"]["daily_loss_limit_pct"])):
            state["resume_status"] = state["status"]
            state["status"] = "DAY_STOPPED"
        if not math.isfinite(equity) or equity <= 0:
            state["status"] = "REVOKED"
            state["revoked_reason"] = "paper equity is non-positive or invalid"

    @staticmethod
    def _record(
            variant_id: str, symbol: str, signal_ts: int | None, state: dict,
            outcome: str, direction: str | None = None,
            setup_type: str | None = None, reason: str | None = None,
            stop_pct: float | None = None, take_pct: float | None = None,
            notional: float | None = None, proposal_id: str | None = None,
            paper_trade_id: str | None = None,
            paper_action: str | None = None) -> ShadowRecord:
        return ShadowRecord(
            variant_id, symbol, signal_ts, outcome, direction, setup_type,
            reason, stop_pct, take_pct, notional, proposal_id, paper_trade_id,
            paper_action, state.get("status"), state.get("equity_usdt"))

    def _record_failure(
            self, variant_id: str, kind: str, exc: Exception,
            now: float) -> None:
        try:
            state, version = self.store.load_paper_portfolio(
                self.scope_key, variant_id, self.initial_balance_usdt, now)
            state["failure_count"] = int(state.get("failure_count") or 0) + 1
            if state["failure_count"] >= self.max_failures:
                state["status"] = "REVOKED"
                state["revoked_reason"] = (
                    f"paper research failed {state['failure_count']} times; "
                    "manual reconciliation required")
            self.store.record_paper_failure(
                self.scope_key, variant_id, kind,
                {"error": f"{type(exc).__name__}: {exc}"}, now)
            self.store.save_paper_portfolio(
                self.scope_key, variant_id, state, version, now)
            if state.get("status") == "REVOKED":
                qualification = self.store.qualification_status(
                    variant_id, self.scope_key)
                if (qualification or {}).get("status") == "QUALIFIED":
                    self.store.revoke_variant(
                        variant_id,
                        {"source": "paper_failure",
                         "reason": state.get("revoked_reason")},
                        scope_key=self.scope_key)
        except Exception:                                  # noqa: BLE001
            pass


def build(
        cfg: dict, registry: dict, *, scope_key: str | None = None,
        store: FindingsStore | None = None) -> ShadowEvaluator | None:
    block = cfg.get("research") or {}
    if not block.get("shadow_enabled"):
        return None
    names = list(block.get("shadow_variants") or [])
    if "*" in names:
        names = [
            name for name, variant in registry.items()
            if variant.status in {"candidate", "testing"}
        ]
    configured_path = block.get("findings_store")
    findings_store = store or FindingsStore(
        resolve_store_path(configured_path))
    names.extend(findings_store.qualified_variant_ids(scope_key or "*"))
    # Every parameter experiment is interpreted against its explicit baseline
    # in the same real-time market episodes.
    for name in list(names):
        candidate = registry.get(name)
        baseline_id = (f"{candidate.strategy_id}.baseline"
                       if candidate is not None else None)
        if baseline_id in registry:
            names.append(baseline_id)
    chosen = [registry[name] for name in dict.fromkeys(names)
              if name in registry]
    if not chosen:
        return None
    return ShadowEvaluator(
        chosen, cfg,
        budget_ms=float(block.get("shadow_budget_ms", 0)),
        store=findings_store,
        scope_key=(scope_key or f"{cfg.get('mode', 'unknown')}:unscoped"),
        initial_balance_usdt=float(
            block.get("paper_initial_balance_usdt") or 10_000),
        max_failures=int(block.get("paper_max_failures") or 3),
    )
