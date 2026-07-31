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
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from research.findings import (FindingsStore, _content_hash,
                               resolve_store_path, variant_identity_hash)

from . import (hypotheses, registry as strategy_registry,
               state as runtime_state, strategy)
from .forward_models import require_validated
from .risk import RiskEngine
from .variants import (Variant, apply, declared_research_setting, from_record)


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


def _variant_runtime(variant: Variant, base_cfg: dict) -> tuple[dict, object, dict]:
    cfg = apply(variant, base_cfg, allow_shadow_strategy=True)
    model = require_validated(variant.strategy_id)
    provenance = {
        "variant_definition_hash": variant_identity_hash(variant),
        "strategy_config_version": runtime_state.experiment_fingerprint(cfg),
        "experiment_config": runtime_state.experiment_fingerprint_material(cfg),
        "code_version": runtime_state.code_fingerprint(),
        "forward_model_id": model.model_id,
        "forward_model_assumptions_hash": _content_hash(model.as_dict()),
    }
    return cfg, model, provenance


def _identity_bundle(baseline: dict, candidate: dict) -> tuple[dict, dict]:
    code_keys = {
        "code_version", "forward_model_id", "forward_model_assumptions_hash"}
    config_keys = {
        "variant_definition_hash", "strategy_config_version",
        "experiment_config"}
    return (
        {
            "baseline": {key: baseline[key] for key in sorted(code_keys)},
            "candidate": {key: candidate[key] for key in sorted(code_keys)},
        },
        {
            "baseline": {key: baseline[key] for key in sorted(config_keys)},
            "candidate": {key: candidate[key] for key in sorted(config_keys)},
        },
    )


class ShadowEvaluator:
    """Evaluate and paper-trade variants without access to an exchange."""

    def __init__(
            self, variants: list, base_cfg: dict, budget_ms: float = 0.0,
            *, store: FindingsStore | None = None,
            scope_key: str = "demo:unscoped",
            initial_balance_usdt: float = 10_000.0,
            max_failures: int = 3,
            workers: int = 1,
            rotation_baseline: Variant | None = None,
            rotation_candidates: list[dict] | None = None,
            rotation_min_duration_seconds: float = 3 * 86_400,
            rotation_min_observations: int = 100) -> None:
        self.budget_ms = float(budget_ms)
        self.base_cfg = base_cfg
        self.scope_key = str(scope_key)
        self.initial_balance_usdt = float(initial_balance_usdt)
        self.max_failures = int(max_failures)
        self.workers = max(1, int(workers))
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
        self._rotation_enabled = rotation_baseline is not None
        self._rotation_baseline = rotation_baseline
        self._rotation_candidates: dict[str, dict] = {}
        self._rotation_assignment: dict | None = None
        self._active_rotation_ids: set[str] = set()
        self._retired_variant_ids: set[str] = set()
        self._rotation_min_duration_seconds = float(
            rotation_min_duration_seconds)
        self._rotation_min_observations = int(rotation_min_observations)

        initial_variants = ([] if self._rotation_enabled else variants)
        for variant in initial_variants:
            if not isinstance(variant, Variant):
                raise TypeError(
                    "shadow variants must be Variant instances; got "
                    f"{type(variant).__name__}")
            try:
                self._enroll_variant(variant)
            except Exception as exc:                       # noqa: BLE001
                self.registration_errors[variant.variant_id] = str(exc)
        if self._rotation_enabled:
            assert rotation_baseline is not None
            try:
                self._enroll_variant(rotation_baseline)
                self._active_rotation_ids = {rotation_baseline.variant_id}
            except Exception as exc:                       # noqa: BLE001
                self.registration_errors[rotation_baseline.variant_id] = str(exc)
                raise
            for candidate in rotation_candidates or []:
                try:
                    self._add_rotation_candidate(candidate)
                except Exception as exc:                   # noqa: BLE001
                    variant = candidate.get("variant")
                    variant_id = getattr(variant, "variant_id", "unknown")
                    self.registration_errors[str(variant_id)] = str(exc)
            self._refresh_rotation(time.time())

    def _enroll_variant(self, variant: Variant) -> None:
        if variant.variant_id in self._variants:
            return
        cfg, model, provenance = _variant_runtime(variant, self.base_cfg)
        self.store.register(variant)
        # A configured candidate is enrolled only if its persisted evidence
        # belongs to this exact executable experiment.
        self.store.bind_paper_experiment(
            self.scope_key, variant.variant_id, provenance,
            self.initial_balance_usdt)
        self._variants[variant.variant_id] = variant
        self._configs[variant.variant_id] = cfg
        self._engines[variant.variant_id] = RiskEngine(cfg)
        self._models[variant.variant_id] = model
        self._provenance[variant.variant_id] = provenance

    @staticmethod
    def _declared_setting(
            variant: Variant, proposal: dict | None = None) -> dict | None:
        return declared_research_setting(variant, proposal)

    def _add_rotation_candidate(self, candidate: dict) -> None:
        variant = candidate.get("variant")
        if not isinstance(variant, Variant):
            raise TypeError("rotation candidate requires a Variant")
        if variant.strategy_id != self._rotation_baseline.strategy_id:
            raise ValueError("rotation candidate strategy does not match baseline")
        proposal = candidate.get("proposal")
        declared = self._declared_setting(variant, proposal)
        if declared is None:
            raise ValueError(
                "rotation candidate must declare exactly one setting axis")
        self.store.register(variant)
        _, _, candidate_provenance = _variant_runtime(variant, self.base_cfg)
        baseline_provenance = self._provenance[
            self._rotation_baseline.variant_id]
        code_identity, config_identity = _identity_bundle(
            baseline_provenance, candidate_provenance)
        descriptor = {
            **declared,
            "variant": variant,
            "variant_id": variant.variant_id,
            "source": str(candidate.get("source") or "static"),
            "priority": int(candidate.get("priority", 100)),
            "order_key": str(candidate.get("order_key") or variant.variant_id),
            "proposal_id": ((proposal or {}).get("proposal_id")),
            "code_identity": code_identity,
            "config_identity": config_identity,
        }
        descriptor["candidate_key"] = _content_hash({
            key: descriptor[key] for key in (
                "variant_id", "axis", "setting_id", "setting", "source",
                "code_identity", "config_identity")
        })
        self._rotation_candidates[variant.variant_id] = descriptor

    def _candidate_for_assignment(self, assignment: dict) -> dict | None:
        descriptor = self._rotation_candidates.get(
            assignment["candidate_variant_id"])
        if descriptor is not None:
            return descriptor
        stored = self.store.variant(assignment["candidate_variant_id"])
        if stored is None:
            return None
        proposal = (self.store.hypothesis_proposal(assignment["proposal_id"])
                    if assignment.get("proposal_id") else None)
        self._add_rotation_candidate({
            "variant": from_record(stored),
            "source": assignment["source"],
            "priority": 0 if assignment["source"] == "adaptive" else 100,
            "order_key": assignment["candidate_variant_id"],
            "proposal": proposal,
        })
        return self._rotation_candidates.get(
            assignment["candidate_variant_id"])

    def _refresh_rotation(self, timestamp: float) -> None:
        if not self._rotation_enabled:
            return
        baseline_id = self._rotation_baseline.variant_id
        assignment = self.store.active_experiment_assignment(
            self.scope_key, self._rotation_baseline.strategy_id, now=timestamp)
        if assignment is None:
            candidates = self.store.prioritized_experiment_candidates(
                self.scope_key, self._rotation_baseline.strategy_id,
                list(self._rotation_candidates.values()), now=timestamp)
            assignment = self.store.ensure_experiment_assignment(
                self.scope_key, self._rotation_baseline.strategy_id,
                baseline_id, candidates,
                minimum_duration_seconds=self._rotation_min_duration_seconds,
                minimum_observations=self._rotation_min_observations,
                now=timestamp)
        if assignment is None:
            self._rotation_assignment = None
            self._active_rotation_ids = {baseline_id}
            return
        descriptor = self._candidate_for_assignment(assignment)
        if descriptor is None:
            self.store.reject_experiment_assignment(
                assignment["assignment_id"],
                "assigned exact variant is unavailable after restart",
                now=timestamp)
            self._rotation_assignment = None
            self._active_rotation_ids = {baseline_id}
            return
        if (assignment["code_identity"] != descriptor["code_identity"]
                or assignment["config_identity"]
                != descriptor["config_identity"]):
            self.store.reject_experiment_assignment(
                assignment["assignment_id"],
                "code/config identity changed before assignment completed",
                now=timestamp)
            self._rotation_assignment = None
            self._active_rotation_ids = {baseline_id}
            return
        previous = (self._rotation_assignment or {}).get(
            "candidate_variant_id")
        current = assignment["candidate_variant_id"]
        if previous and previous != current and previous != baseline_id:
            self._retired_variant_ids.add(previous)
        self._enroll_variant(descriptor["variant"])
        self._rotation_assignment = assignment
        self._active_rotation_ids = {baseline_id, current}

    def _proposal_lifecycle(
            self, proposal: dict, status: str, detail: dict,
            timestamp: float) -> None:
        proposal_id = proposal.get("proposal_id")
        if proposal_id:
            self.store.append_proposal_event(
                str(proposal_id), status, detail, now=timestamp)

    def _reject_adaptive_proposal(
            self, proposal: dict, reason: str, timestamp: float) -> None:
        try:
            self._proposal_lifecycle(
                proposal, "REJECTED", {"reason": str(reason)}, timestamp)
        except Exception:                                  # noqa: BLE001
            pass

    def record_research_selection(
            self, selection: dict, attribution: dict) -> dict:
        validation_error = None
        if selection.get("validation_status") != "ACCEPTED":
            validation_error = str(
                selection.get("rejection_reason")
                or "research selection failed parser validation")
        requested_strategy = str(selection.get("strategy_id") or "")
        expected_strategy = str(self.base_cfg["strategy"]["id"])
        if validation_error is None and requested_strategy != expected_strategy:
            validation_error = (
                "research selection strategy does not match this coordinator "
                "lane")
        return self.store.record_research_selection(
            selection, list(self._rotation_candidates.values()),
            scope_key=self.scope_key,
            run_id=str(attribution.get("run_id") or "unknown-run"),
            cycle_id=attribution.get("cycle_id"),
            model_id=str(attribution.get("model_id") or "unknown-model"),
            prompt_version=str(
                attribution.get("prompt_version") or "unknown-prompt"),
            validation_error=validation_error)

    @property
    def variant_ids(self) -> list[str]:
        if self._rotation_enabled:
            return sorted(self._active_rotation_ids)
        return sorted(self._configs)

    def _advance_variant_ids(self) -> list[str]:
        if not self._rotation_enabled:
            return self.variant_ids
        return sorted(self._active_rotation_ids | self._retired_variant_ids)

    def held_symbols(self) -> list[str]:
        """Symbols required to mark or close local positions after reranking."""
        held = set()
        for variant_id in self._advance_variant_ids():
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
        for variant_id in self._advance_variant_ids():
            try:
                state, _ = self._advance_account(
                    variant_id, snapshot or {}, timestamp, records)
                if (variant_id in self._retired_variant_ids
                        and not state.get("positions")):
                    self._retired_variant_ids.discard(variant_id)
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
        for proposal in proposals or []:
            if proposal.get("action") == "research_proposal":
                from .variants import adaptive_hypothesis_variant
                try:
                    variant = adaptive_hypothesis_variant(
                        str(self.base_cfg["strategy"]["id"]),
                        str(self.base_cfg["strategy"]["version"]),
                        str(proposal["hypothesis_id"]),
                        str(proposal["setting_id"]), float(proposal["value"]))
                    expected = proposal.get("variant_id")
                    if expected and str(expected) != variant.variant_id:
                        raise ValueError(
                            "persisted adaptive proposal variant_id does not "
                            "match its exact value")
                    proposal["variant_id"] = variant.variant_id
                    if self._rotation_enabled:
                        self._add_rotation_candidate({
                            "variant": variant,
                            "source": "adaptive",
                            "priority": 0,
                            "order_key": (
                                f"{float(proposal.get('proposed_ts') or timestamp):020.6f}:"
                                f"{proposal.get('proposal_id') or variant.variant_id}"),
                            "proposal": proposal,
                        })
                    else:
                        self._enroll_variant(variant)
                except Exception as exc:                   # noqa: BLE001
                    self._reject_adaptive_proposal(
                        proposal, f"adaptive registration failed: {exc}",
                        timestamp)
        if self._rotation_enabled:
            self._refresh_rotation(timestamp)
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

        order = self.store.scheduler_order(
            self.scope_key, self._scheduled_variant_ids(proposals or []))
        evaluated: list[str] = []
        skipped: list[str] = []
        pending = []
        for index, variant_id in enumerate(order):
            if budget.exhausted():
                skipped.extend(order[index:])
                break
            if variant_id not in accounts:
                skipped.append(variant_id)
                continue
            pending.append((variant_id, accounts[variant_id]))

        # Workers never touch FindingsStore. They own one copied account state
        # and return a commit packet; SQLite writes and scheduler accounting
        # stay serialized here because the store is the durable isolation
        # boundary.
        if self.workers > 1 and len(pending) > 1:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(pending)),
                                    thread_name_prefix="shadow") as pool:
                results = list(pool.map(
                    lambda item: self._evaluate_variant(
                        item[0], item[1], snapshot, recorded_opens,
                        timestamp, cycle_id), pending))
        else:
            results = [self._evaluate_variant(
                variant_id, account, snapshot, recorded_opens,
                timestamp, cycle_id) for variant_id, account in pending]

        decision_keys_by_variant: dict[str, set[str]] = {}
        for variant_id, result in zip((item[0] for item in pending), results):
            try:
                if isinstance(result, Exception):
                    raise result
                state, version, variant_records, pending_opens, pending_decisions = result
                self.store.commit_paper_portfolio(
                    self.scope_key, variant_id, state, version,
                    opened_trades=pending_opens, decisions=pending_decisions,
                    now=timestamp)
                decision_keys_by_variant[variant_id] = {
                    str(decision["proposal_id"])
                    for decision in pending_decisions
                    if decision.get("proposal_id")}
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
                    if (self._rotation_enabled
                            and self._rotation_assignment is not None
                            and variant_id in {
                                self._rotation_assignment[
                                    "baseline_variant_id"],
                                self._rotation_assignment[
                                    "candidate_variant_id"],
                            }):
                        self._rotation_assignment = (
                            self.store.reject_experiment_assignment(
                                self._rotation_assignment["assignment_id"],
                                str(state.get("revoked_reason")
                                    or "paper portfolio revoked"),
                                detail={"variant_id": variant_id},
                                now=timestamp))
                (evaluated if len(variant_records) == len(recorded_opens)
                 and variant_records else skipped).append(variant_id)
            except Exception as exc:                       # noqa: BLE001
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
        if self._rotation_enabled and self._rotation_assignment is not None:
            assignment = self._rotation_assignment
            baseline_keys = decision_keys_by_variant.get(
                assignment["baseline_variant_id"], set())
            candidate_keys = decision_keys_by_variant.get(
                assignment["candidate_variant_id"], set())
            comparable = sorted(baseline_keys & candidate_keys)
            if (assignment["status"] not in {"COMPLETED", "REJECTED"}
                    and comparable):
                assignment = self.store.record_experiment_observations(
                    assignment["assignment_id"], [{
                        "observation_key": key,
                        "observed_ts": timestamp,
                        "detail": {
                            "scope_key": self.scope_key,
                            "baseline_variant_id": assignment[
                                "baseline_variant_id"],
                            "candidate_variant_id": assignment[
                                "candidate_variant_id"],
                        },
                    } for key in comparable], now=timestamp)
            assignment = self.store.maybe_complete_experiment_assignment(
                assignment["assignment_id"], now=timestamp)
            self._rotation_assignment = assignment
            if assignment["status"] in {"COMPLETED", "REJECTED"}:
                candidate_id = assignment["candidate_variant_id"]
                if candidate_id != assignment["baseline_variant_id"]:
                    self._retired_variant_ids.add(candidate_id)
                self._active_rotation_ids = {
                    assignment["baseline_variant_id"]}
        coverage = self.store.scheduler_coverage(self.scope_key)
        self.last_coverage = {
            "scope_key": self.scope_key,
            "scheduled": len(order),
            "evaluated": evaluated,
            "skipped": skipped,
            "coverage_pct": (len(evaluated) / len(order) * 100.0
                             if order else 100.0),
            "cumulative": coverage,
            "experiment_assignment": self._rotation_assignment,
        }
        self.last_budget = budget
        return records

    def _scheduled_variant_ids(self, proposals: list[dict]) -> list[str]:
        """Select at most one adaptive setting per strategy and cycle.

        Ordinary variants with no ``hypothesis_id`` remain tournament arms.
        Registered static hypothesis settings are not substitutes for an
        adaptive value: only the exact persisted ``variant_id`` is eligible.
        """
        if self._rotation_enabled:
            return self.variant_ids
        adaptive = {str(p.get("variant_id")) for p in proposals
                    if (p.get("action") == "research_proposal"
                        and p.get("variant_id"))}
        selected = []
        by_strategy = {}
        for variant_id in self.variant_ids:
            variant = self._variants[variant_id]
            if variant.hypothesis_id is None:
                selected.append(variant_id)
                continue
            if variant_id in adaptive:
                by_strategy.setdefault(variant.strategy_id, variant_id)
        selected.extend(by_strategy.values())
        return selected

    def _evaluate_variant(self, variant_id, account, snapshot, proposals,
                          timestamp, cycle_id):
        try:
            state, version = account
            pending_opens = []
            pending_decisions = []
            variant_records = []
            for decision in proposals:
                record = self._evaluate_one(
                    variant_id, snapshot, decision, state, timestamp,
                    cycle_id, pending_opens)
                variant_records.append(record)
                if (record.proposal_id
                        and record.reason != "proposal already evaluated"):
                    pending_decisions.append(self._paper_decision(
                        variant_id, cycle_id, decision, record, timestamp))
            self._refresh_metrics(state, snapshot, timestamp)
            self._apply_circuit_breakers(
                state, self._configs[variant_id], timestamp)
            return (state, version, variant_records, pending_opens,
                    pending_decisions)
        except Exception as exc:                       # noqa: BLE001
            return exc

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
                position, exit_price, result, now)
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
            position: dict, exit_price: float, result: str,
            exit_ts: float) -> tuple[float, float]:
        entry = float(position["entry_price"])
        sign = 1.0 if position["direction"] == "long" else -1.0
        gross_pct = sign * (float(exit_price) - entry) / entry * 100.0
        components = position.get("cost_components")
        if isinstance(components, dict):
            cost_pct = sum(float(components.get(key) or 0.0) for key in (
                "entry_fee_pct", "exit_fee_pct", "spread_pct",
                "entry_slippage_pct"))
            if result == "stop":
                cost_pct += float(components.get("stop_slippage_pct") or 0.0)
            rate = components.get("funding_rate_pct")
            interval = components.get("funding_interval_hours")
            next_minutes = components.get("next_funding_minutes")
            try:
                rate = float(rate)
                interval = float(interval)
                next_minutes = float(next_minutes)
            except (TypeError, ValueError):
                rate = interval = next_minutes = 0.0
            intervals = 0
            if (math.isfinite(rate) and math.isfinite(interval)
                    and math.isfinite(next_minutes) and interval > 0
                    and next_minutes >= 0):
                first = float(position["entry_ts"]) + next_minutes * 60.0
                if float(exit_ts) >= first:
                    intervals = 1 + math.floor(
                        max(0.0, float(exit_ts) - first)
                        / (interval * 3600.0))
            # Positive funding is paid by longs and received by shorts;
            # negative funding reverses that cash flow.
            cost_pct += sign * rate * intervals
        else:
            # Schema-3..10 positions remain restart-compatible.
            cost_pct = float(position.get("round_trip_cost_pct") or 0)
            if result != "stop":
                cost_pct = max(
                    0.0, cost_pct
                    - float(position.get("stop_slippage_pct") or 0))
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
        model = self._models[variant_id]
        decision = dict(recorded_decision)
        symbol = str(decision.get("symbol") or "")
        source_row = snapshot.get(symbol)
        if not isinstance(source_row, dict):
            return self._record(
                variant_id, symbol or "*", None, state, "vetoed",
                decision.get("direction"), decision.get("setup_type"),
                "recorded proposal symbol is absent from the common snapshot")
        row = dict(source_row)
        requested_signal_ts = decision.get("signal_ts")
        if decision.get("proposal_source") == "deterministic_contract":
            # A deterministic model owns its signal clock.  Preserve a missing
            # required timestamp as missing instead of borrowing another
            # timeframe's timestamp from the common row.
            row["signal_ts"] = requested_signal_ts
        elif requested_signal_ts is not None:
            row["signal_ts"] = requested_signal_ts
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
        refusal = str(decision.get("research_refusal_reason") or "").strip()
        if refusal:
            state.setdefault("seen_proposals", {})[proposal_id] = now
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type, refusal, proposal_id=proposal_id)
        variant = self._variants[variant_id]
        plan, why = strategy.build_setup_plan(
            decision, row, cfg,
            hypothesis_params=(variant.hypothesis_params
                               if variant.hypothesis_id == decision.get(
                                   "hypothesis_id") else None))
        if plan is None:
            state.setdefault("seen_proposals", {})[proposal_id] = now
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type, why, proposal_id=proposal_id)
        merged = dict(decision, **{
            "stop_loss_pct": plan.get("stop_loss_pct"),
            "take_profit_pct": plan.get("take_profit_pct")})
        entry_price = model.entry_price(row, str(direction))
        if entry_price is None:
            state.setdefault("seen_proposals", {})[proposal_id] = now
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type, "model entry price is unavailable",
                plan.get("stop_loss_pct"), plan.get("take_profit_pct"),
                proposal_id=proposal_id)
        risk_snapshot = dict(snapshot)
        risk_snapshot[symbol] = model.risk_row(row, str(direction))
        sized, veto = engine.vet_open(
            merged, float(state.get("equity_usdt") or 0),
            list(state.get("positions") or []), risk_snapshot,
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
            row, state, now, pending_opens)
        return self._record(
            variant_id, symbol, signal_ts, state, "proposed",
            direction, setup_type, None, sized.get("sl_pct"),
            sized.get("tp_pct"), sized.get("notional"), proposal_id,
            trade_id, "open")

    def _open_paper_trade(
            self, variant_id: str, cycle_id: str | None, proposal_id: str,
            decision: dict, sized: dict, source_row: dict, state: dict, now: float,
            pending_opens: list[dict]) -> str:
        entry = float(sized["price"])
        stop_move = entry * float(sized["sl_pct"]) / 100.0
        take_move = entry * float(sized["tp_pct"]) / 100.0
        long = decision["direction"] == "long"
        model = self._models[variant_id]
        cost_components = model.cost_components(
            source_row, sized, self._configs[variant_id])
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
                now + model.horizon_for(self._configs[variant_id]) * 3600),
            "round_trip_cost_pct": float(sized["estimated_cost_pct"]),
            "stop_slippage_pct": float(
                self._configs[variant_id]["trading_costs"]
                ["expected_stop_slippage_pct"]),
            "cost_components": cost_components,
        }
        trade_id = uuid.uuid4().hex
        pending_opens.append({
            **position,
            "trade_id": trade_id,
            "scope_key": self.scope_key,
            "variant_id": variant_id,
            "cycle_id": cycle_id,
            "model_id": model.model_id,
            "assumptions": {
                "forward_model": model.as_dict(),
                "cost_components": cost_components,
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
        del variant_id
        signal_identity = (signal_ts if signal_ts is not None
                           else f"cycle:{cycle_id or 'unscoped'}")
        raw = "\0".join(map(str, (
            symbol, signal_identity, direction, setup_type)))
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


class StrategyShadowCoordinator:
    """One isolated evaluator per strategy on one shared in-memory feed."""

    def __init__(self, evaluators: dict[str, ShadowEvaluator], *,
                 active_strategy_id: str) -> None:
        if not evaluators:
            raise ValueError("strategy shadow coordinator needs an evaluator")
        self.evaluators = dict(evaluators)
        self.active_strategy_id = str(active_strategy_id)
        self.store = next(iter(self.evaluators.values())).store
        self.scope_key = next(iter(self.evaluators.values())).scope_key
        self.last_coverage: dict = {}
        self.last_budget = ShadowBudget(0.0)
        self.last_cycle_by_strategy: dict[str, dict] = {}
        self.registration_errors = {
            strategy_id: dict(evaluator.registration_errors)
            for strategy_id, evaluator in self.evaluators.items()
            if evaluator.registration_errors
        }

    @property
    def strategy_ids(self) -> list[str]:
        return sorted(self.evaluators)

    @property
    def variant_ids(self) -> list[str]:
        return sorted({variant_id
                       for evaluator in self.evaluators.values()
                       for variant_id in evaluator.variant_ids})

    def held_symbols(self) -> list[str]:
        return sorted({symbol
                       for evaluator in self.evaluators.values()
                       for symbol in evaluator.held_symbols()})

    def record_research_selection(
            self, selection: dict, attribution: dict) -> dict:
        strategy_id = str(selection.get("strategy_id") or "")
        evaluator = self.evaluators.get(strategy_id)
        if evaluator is not None:
            return evaluator.record_research_selection(selection, attribution)
        validation_error = str(
            selection.get("rejection_reason")
            or "research selection strategy is not available to the coordinator")
        return self.store.record_research_selection(
            selection, [], scope_key=self.scope_key,
            run_id=str(attribution.get("run_id") or "unknown-run"),
            cycle_id=attribution.get("cycle_id"),
            model_id=str(attribution.get("model_id") or "unknown-model"),
            prompt_version=str(
                attribution.get("prompt_version") or "unknown-prompt"),
            validation_error=validation_error)

    @staticmethod
    def _isolate_failure(
            evaluator: ShadowEvaluator, strategy_id: str, phase: str,
            exc: Exception, timestamp: float) -> list[ShadowRecord]:
        """Charge a coordinator failure only to the affected strategy."""
        records = []
        for variant_id in evaluator.variant_ids:
            evaluator._record_failure(  # same-module isolation boundary
                variant_id, f"strategy_{phase}_failed", exc, timestamp)
            records.append(ShadowRecord(
                variant_id, "*", None, "vetoed",
                reason=(f"{strategy_id} {phase} error: "
                        f"{type(exc).__name__}: {exc}")))
        return records

    def advance(
            self, snapshot: dict, now: float | None = None) -> list[ShadowRecord]:
        timestamp = time.time() if now is None else float(now)
        records = []
        for strategy_id in self.strategy_ids:
            evaluator = self.evaluators[strategy_id]
            self.last_cycle_by_strategy[strategy_id] = {
                "timestamp": timestamp,
                "snapshot_identity": id(snapshot),
                "phase": "advance",
            }
            try:
                records.extend(evaluator.advance(snapshot, now=timestamp))
            except Exception as exc:                       # noqa: BLE001
                self.last_cycle_by_strategy[strategy_id]["error"] = (
                    f"{type(exc).__name__}: {exc}")
                records.extend(self._isolate_failure(
                    evaluator, strategy_id, "advance", exc, timestamp))
        return records

    def evaluate(
            self, snapshot: dict, portfolio: dict | None = None,
            now: float | None = None, cycle_id: str | None = None,
            proposals: list[dict] | None = None,
            advance_accounts: bool = True) -> list[ShadowRecord]:
        del portfolio
        timestamp = time.time() if now is None else float(now)
        aggregate_budget = ShadowBudget(0.0)
        records = []
        coverage = {}
        for strategy_id in self.strategy_ids:
            evaluator = self.evaluators[strategy_id]
            self.last_cycle_by_strategy[strategy_id] = {
                "timestamp": timestamp,
                "snapshot_identity": id(snapshot),
                "phase": "evaluate",
            }
            try:
                if strategy_id == self.active_strategy_id:
                    strategy_proposals = list(proposals or [])
                    source = "recorded_llm"
                else:
                    model = require_validated(strategy_id)
                    strategy_proposals = model.deterministic_proposals(
                        snapshot, evaluator.base_cfg)
                    source = "deterministic_contract"
                self.last_cycle_by_strategy[strategy_id].update({
                    "proposal_source": source,
                    "proposals": len(strategy_proposals),
                })
                records.extend(evaluator.evaluate(
                    snapshot, now=timestamp, cycle_id=cycle_id,
                    proposals=strategy_proposals,
                    advance_accounts=advance_accounts))
                coverage[strategy_id] = dict(evaluator.last_coverage)
                if getattr(evaluator, "last_budget", None) is not None:
                    aggregate_budget.overran = (
                        aggregate_budget.overran
                        or evaluator.last_budget.overran)
            except Exception as exc:                       # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                self.last_cycle_by_strategy[strategy_id]["error"] = message
                coverage[strategy_id] = {"error": message}
                records.extend(self._isolate_failure(
                    evaluator, strategy_id, "evaluate", exc, timestamp))
        self.last_coverage = {
            "scope_key": self.scope_key,
            "strategies": coverage,
            "strategy_count": len(self.evaluators),
        }
        self.last_budget = aggregate_budget
        return records


def _research_cfg(cfg: dict, strategy_id: str) -> dict:
    """Build an isolated strategy config without changing the live config."""
    if strategy_id == str(cfg["strategy"]["id"]):
        return cfg
    spec = strategy_registry.spec_for(strategy_id)
    model = require_validated(strategy_id)
    out = deepcopy(cfg)
    block = dict(out["strategy"])
    block.update(spec.contract_params)
    block.update({
        "id": spec.id,
        "version": spec.version,
        "signal_timeframe": spec.signal_timeframe,
    })
    # Outcome semantics belong to the validated forward model, not to the
    # active momentum config copied above or to the historical tournament's
    # contract-parameter namespace.
    block["min_stop_atr_multiple"] = model.stop_atr_multiple
    block["fixed_reward_risk"] = model.reward_risk
    block["forward_horizon_hours"] = model.horizon_hours
    out["strategy"] = block
    risk = dict(out["risk"])
    risk["max_hold_hours"] = min(
        float(risk["max_hold_hours"]), spec.max_hold_hours_ceiling)
    out["risk"] = risk
    costs = dict(out["trading_costs"])
    costs["expected_hold_hours"] = min(model.horizon_hours, 168.0)
    out["trading_costs"] = costs
    return out


def _build_strategy_evaluator(
        cfg: dict, variant_registry: dict, names: list[str], *,
        scope_key: str, findings_store: FindingsStore) -> ShadowEvaluator | None:
    from .variants import (baseline, baseline_variant_id, hypothesis_variants,
                           preregistered_variants)
    block = cfg.get("research") or {}
    strategy_id = str(cfg["strategy"]["id"])
    strategy_version = str(cfg["strategy"]["version"])
    generated = []
    if strategy_id == "momentum":
        generated.extend(hypothesis_variants(
            strategy_id, strategy_version))
    generated.extend(preregistered_variants(
        strategy_id, strategy_version))
    for variant in generated:
        variant_registry.setdefault(variant.variant_id, variant)
    if "*" in names:
        names = [
            name for name, variant in variant_registry.items()
            if (variant.strategy_id == strategy_id
                and variant.status in {"candidate", "testing"})
        ]
    names.extend(findings_store.qualified_variant_ids(scope_key))
    pending_adaptive = findings_store.pending_hypothesis_proposals(strategy_id)
    active = findings_store.active_experiment_assignment(
        scope_key, strategy_id)
    if not names and not pending_adaptive and active is None:
        return None
    baseline_id = baseline_variant_id(strategy_id)
    baseline_variant = variant_registry.get(baseline_id)
    if baseline_variant is None:
        baseline_variant = baseline(strategy_id, strategy_version)
        variant_registry[baseline_id] = baseline_variant
    rotation_candidates = []
    seen = set()
    for ordinal, name in enumerate(dict.fromkeys(names)):
        candidate = variant_registry.get(name)
        if (candidate is None or candidate.strategy_id != strategy_id
                or candidate.variant_id == baseline_id
                or candidate.variant_id in seen):
            continue
        # Schema-10 rotates exactly one declared axis. Multi-parameter YAML
        # bundles remain pre-registered but are not silently treated as one.
        if ShadowEvaluator._declared_setting(candidate) is None:
            continue
        seen.add(candidate.variant_id)
        rotation_candidates.append({
            "variant": candidate,
            "source": "static",
            "priority": 10,
            "order_key": f"{ordinal:08d}:{candidate.variant_id}",
        })
    for proposal in pending_adaptive:
        stored = findings_store.variant(str(proposal.get("variant_id") or ""))
        if stored is None or stored["variant_id"] in seen:
            continue
        candidate = from_record(stored)
        if candidate.strategy_id != strategy_id:
            continue
        seen.add(candidate.variant_id)
        rotation_candidates.append({
            "variant": candidate,
            "source": "adaptive",
            "priority": 0,
            "order_key": (
                f"{float(proposal['proposed_ts']):020.6f}:"
                f"{proposal['proposal_id']}"),
            "proposal": proposal,
        })
    return ShadowEvaluator(
        [], cfg,
        budget_ms=float(block.get("shadow_budget_ms", 0)),
        store=findings_store,
        scope_key=scope_key,
        initial_balance_usdt=float(
            block.get("paper_initial_balance_usdt") or 10_000),
        max_failures=int(block.get("paper_max_failures") or 3),
        workers=int(block.get("shadow_workers") or 1),
        rotation_baseline=baseline_variant,
        rotation_candidates=rotation_candidates,
        rotation_min_duration_seconds=(
            float(block.get("experiment_min_duration_days") or 3) * 86_400),
        rotation_min_observations=int(
            block.get("experiment_min_observations")
            or block.get("paper_min_closed_trades") or 100),
    )


def build(
        cfg: dict, registry: dict, *, scope_key: str | None = None,
        store: FindingsStore | None = None
        ) -> ShadowEvaluator | StrategyShadowCoordinator | None:
    block = cfg.get("research") or {}
    if not block.get("shadow_enabled"):
        return None
    names = list(block.get("shadow_variants") or [])
    configured_path = block.get("findings_store")
    findings_store = store or FindingsStore(
        resolve_store_path(configured_path))
    resolved_scope = scope_key or f"{cfg.get('mode', 'unknown')}:unscoped"

    # Explicit variant lists retain the original single-strategy API used by
    # focused tooling. The shipped wildcard is the end-to-end seven-strategy
    # research mode.
    if "*" not in names:
        return _build_strategy_evaluator(
            cfg, registry, names, scope_key=resolved_scope,
            findings_store=findings_store)

    resolved_scope = (
        f"{resolved_scope}:feed-v{int(block.get('forward_feed_version') or 1)}")

    evaluators = {}
    for strategy_id in sorted(strategy_registry.REGISTRY):
        strategy_cfg = _research_cfg(cfg, strategy_id)
        evaluator = _build_strategy_evaluator(
            strategy_cfg, registry, ["*"], scope_key=resolved_scope,
            findings_store=findings_store)
        if evaluator is not None:
            evaluators[strategy_id] = evaluator
    if not evaluators:
        return None
    return StrategyShadowCoordinator(
        evaluators, active_strategy_id=str(cfg["strategy"]["id"]))
