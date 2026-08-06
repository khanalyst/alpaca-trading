"""Durable, isolated parameter-variant paper portfolios.

Every configured variant observes the same real-time snapshot but owns its
cash, positions, exposure, cooldowns, losses and circuit-breaker state. The
evaluator can persist research state but has no exchange client and no order
method. Failures are contained by the engine after live decisions commit.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import tempfile
import time
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from research.findings import (FindingsStore, _content_hash,
                               resolve_store_path, variant_identity_hash)

from . import (brain, hypotheses, registry as strategy_registry,
               state as runtime_state, strategy)
from . import staged_lane
from .forward_models import (STAGED_HARNESS,
                             _field as _row_field,
                             _finite as _row_finite,
                             require_complete_contract)
from .risk import RiskEngine
from .variants import (Variant, apply, declared_research_setting, from_record)

log = logging.getLogger(__name__)


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


STAGED_STRATEGY_ID = "staged"
DEFAULT_STAGING_STORE = "research/cache/staging.db"
MAX_SHADOW_WORKERS = 32
DEFAULT_ROTATION_BATCH_SIZE = 1
MAX_ROTATION_CANDIDATES = 8


def _resolve_staging_path(configured: str | Path | None) -> Path:
    """Resolve staging.db exactly as the research CLI resolves it."""
    path = Path(configured or DEFAULT_STAGING_STORE)
    return (path if path.is_absolute()
            else Path(__file__).resolve().parent.parent / path)


def _model_for(strategy_id: str):
    """Resolve the outcome contract, including the staged harness.

    Machine-authored mechanisms share one fixed harness rather than each
    carrying its own exits, so a difference between two of them is a
    difference in the mechanism. The harness is not in ``BY_STRATEGY``
    because it is a measurement instrument, not a registered strategy.
    """
    if str(strategy_id) == STAGED_STRATEGY_ID:
        return STAGED_HARNESS
    return require_complete_contract(strategy_id)


def _variant_runtime(variant: Variant, base_cfg: dict) -> tuple[dict, object, dict]:
    cfg = apply(variant, base_cfg, allow_shadow_strategy=True)
    model = _model_for(variant.strategy_id)
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
            # The default exceeds the minimum reachable confirmation window.
            rotation_min_duration_seconds: float = 10 * 86_400,
            rotation_min_observations: int = 100,
            rotation_batch_size: int = DEFAULT_ROTATION_BATCH_SIZE,
            rotation_hard_cap: int = MAX_ROTATION_CANDIDATES) -> None:
        self.budget_ms = float(budget_ms)
        self.base_cfg = base_cfg
        self.scope_key = str(scope_key)
        self.initial_balance_usdt = float(initial_balance_usdt)
        self.max_failures = int(max_failures)
        try:
            requested_workers = int(workers)
        except (TypeError, ValueError):
            requested_workers = 1
        # Bound per-lane CPU/network pressure. Worker threads only compute
        # isolated account packets; FindingsStore commits stay serialized in
        # ``evaluate`` below, after all workers return.
        self.workers = max(1, min(MAX_SHADOW_WORKERS, requested_workers))
        # Direct callers get an isolated store; build() supplies durable state.
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
        self._rotation_assignments: dict[str, dict] = {}
        self._active_rotation_ids: set[str] = set()
        self._retired_variant_ids: set[str] = set()
        self._rotation_batch_size = max(
            1, min(int(rotation_batch_size), MAX_ROTATION_CANDIDATES))
        self._rotation_hard_cap = max(
            1, min(int(rotation_hard_cap), MAX_ROTATION_CANDIDATES))
        self._rotation_batch_size = min(
            self._rotation_batch_size, self._rotation_hard_cap)
        # One shared baseline plus at most eight candidate accounts is the
        # resource envelope for a strategy rotation.
        self._rotation_resource_cap = 1 + self._rotation_hard_cap
        self._rotation_batch_id: str | None = None
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
        strategy_id = self._rotation_baseline.strategy_id
        previous_ids = {
            str(assignment.get("candidate_variant_id"))
            for assignment in self._rotation_assignments.values()
            if assignment.get("candidate_variant_id")
        }

        active_assignments: list[dict] = []
        active_many = getattr(self.store, "active_experiment_assignments", None)
        if callable(active_many):
            active_assignments = list(active_many(
                self.scope_key, strategy_id, now=timestamp) or [])
        else:
            assignment = self.store.active_experiment_assignment(
                self.scope_key, strategy_id, now=timestamp)
            if assignment is not None:
                active_assignments = [assignment]

        if not active_assignments:
            candidates = list(self._rotation_candidates.values())
            ensure_batch = getattr(self.store, "ensure_experiment_batch", None)
            if callable(ensure_batch):
                static_candidates = [
                    item for item in candidates
                    if str(item.get("source") or "static") == "static"]
                adaptive_candidates = [
                    item for item in candidates
                    if str(item.get("source") or "static") == "adaptive"]
                # Batched rotation is reserved for the pre-registered axis
                # catalog. A pending adaptive proposal still needs the
                # legacy one-arm path so it is not silently stranded while a
                # static catalog is absent; adaptive values are never mixed
                # into a static batch.
                # An explicitly accepted adaptive selection gets its own
                # single-arm turn ahead of untouched static settings. It is
                # still never mixed into the static multi-arm batch.
                if adaptive_candidates:
                    batch_candidates = adaptive_candidates
                    static_batch = False
                else:
                    batch_candidates = static_candidates or candidates
                    static_batch = bool(static_candidates)
                batch_size = (self._rotation_batch_size
                              if static_batch else 1)
                active_assignments = list(ensure_batch(
                    self.scope_key, strategy_id, baseline_id, batch_candidates,
                    minimum_duration_seconds=self._rotation_min_duration_seconds,
                    minimum_observations=self._rotation_min_observations,
                    target_candidates=batch_size,
                    hard_cap=(self._rotation_hard_cap
                              if static_batch else 1),
                    initial_balance_usdt=self.initial_balance_usdt,
                    pre_registered_only=static_batch,
                    now=timestamp) or [])
                # Legacy stores may contain an exact adaptive proposal but no
                # preregistered axis at all. Preserve that one-candidate
                # migration path without allowing adaptive arms into a normal
                # static batch.
                if (not active_assignments
                        and not static_batch):
                    prioritized = self.store.prioritized_experiment_candidates(
                        self.scope_key, strategy_id, candidates, now=timestamp)
                    assignment = self.store.ensure_experiment_assignment(
                        self.scope_key, strategy_id, baseline_id, prioritized,
                        minimum_duration_seconds=(
                            self._rotation_min_duration_seconds),
                        minimum_observations=self._rotation_min_observations,
                        initial_balance_usdt=self.initial_balance_usdt,
                        now=timestamp)
                    if assignment is not None:
                        active_assignments = [assignment]
            else:
                # Compatibility with pre-batch stores and test doubles.
                candidates = self.store.prioritized_experiment_candidates(
                    self.scope_key, strategy_id, candidates, now=timestamp)
                assignment = self.store.ensure_experiment_assignment(
                    self.scope_key, strategy_id, baseline_id, candidates,
                    minimum_duration_seconds=self._rotation_min_duration_seconds,
                    minimum_observations=self._rotation_min_observations,
                    initial_balance_usdt=self.initial_balance_usdt,
                    now=timestamp)
                if assignment is not None:
                    active_assignments = [assignment]

        valid: dict[str, dict] = {}
        for assignment in active_assignments:
            if assignment.get("status") in {"COMPLETED", "REJECTED"}:
                continue
            assignment_id = str(assignment.get("assignment_id") or "")
            candidate_id = str(assignment.get("candidate_variant_id") or "")
            if not assignment_id or not candidate_id:
                continue
            descriptor = self._candidate_for_assignment(assignment)
            if descriptor is None:
                self.store.reject_experiment_assignment(
                    assignment_id,
                    "assigned exact variant is unavailable after restart",
                    now=timestamp)
                continue
            if (assignment.get("code_identity") != descriptor["code_identity"]
                    or assignment.get("config_identity")
                    != descriptor["config_identity"]):
                self.store.reject_experiment_assignment(
                    assignment_id,
                    "code/config identity changed before assignment completed",
                    now=timestamp)
                continue
            try:
                self._enroll_variant(descriptor["variant"])
            except Exception as exc:                       # noqa: BLE001
                self.store.reject_experiment_assignment(
                    assignment_id,
                    f"assigned variant could not be enrolled: {exc}",
                    now=timestamp)
                continue
            valid.setdefault(assignment_id, assignment)

        current_ids = {
            str(assignment.get("candidate_variant_id"))
            for assignment in valid.values()
            if assignment.get("candidate_variant_id")
        }
        for variant_id in previous_ids - current_ids:
            if variant_id != baseline_id:
                self._retired_variant_ids.add(variant_id)
        self._rotation_assignments = valid
        ordered = sorted(
            valid.values(),
            key=lambda item: (
                int(item.get("batch_ordinal", 0)),
                str(item.get("assignment_id") or "")))
        self._rotation_assignment = ordered[0] if ordered else None
        self._rotation_batch_id = (
            str(ordered[0].get("batch_id"))
            if ordered and ordered[0].get("batch_id") else None)
        self._active_rotation_ids = {baseline_id} | current_ids

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
        proposals_by_variant: dict[str, list[dict]] = {}
        if self._rotation_enabled:
            active_assignments = list(self._rotation_assignments.values())
            candidate_assignments = {
                str(assignment["candidate_variant_id"]): assignment
                for assignment in active_assignments
            }
            baseline_id = self._rotation_baseline.variant_id
            # A shared baseline continues to see proposals while at least one
            # candidate collection window is open. Each candidate receives the
            # same decisions unless its own assignment is draining.
            baseline_open = any(
                assignment.get("status") != "DRAINING"
                for assignment in active_assignments)
            if not active_assignments:
                baseline_open = True
            proposals_by_variant[baseline_id] = (
                recorded_opens if baseline_open else [])
            for candidate_id, assignment in candidate_assignments.items():
                proposals_by_variant[candidate_id] = (
                    recorded_opens
                    if assignment.get("status") != "DRAINING" else [])
        else:
            proposals_by_variant = {
                variant_id: recorded_opens for variant_id in self.variant_ids}

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
            pending.append((variant_id, accounts[variant_id],
                            proposals_by_variant.get(variant_id,
                                                     recorded_opens)))

        # Workers never touch FindingsStore. They own one copied account state
        # and return a commit packet; SQLite writes and scheduler accounting
        # stay serialized here because the store is the durable isolation
        # boundary.
        if self.workers > 1 and len(pending) > 1:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(pending)),
                                    thread_name_prefix="shadow") as pool:
                results = list(pool.map(
                    lambda item: self._evaluate_variant(
                        item[0], item[1], snapshot, item[2],
                        timestamp, cycle_id), pending))
        else:
            results = [self._evaluate_variant(
                variant_id, account, snapshot, variant_proposals,
                timestamp, cycle_id)
                       for variant_id, account, variant_proposals in pending]

        decision_keys_by_variant: dict[str, set[str]] = {}
        for (variant_id, _account, variant_proposals), result in zip(
                pending, results):
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
                    if self._rotation_enabled:
                        reason = str(state.get("revoked_reason")
                                     or "paper portfolio revoked")
                        for assignment_id, assignment in list(
                                self._rotation_assignments.items()):
                            if variant_id not in {
                                    assignment["baseline_variant_id"],
                                    assignment["candidate_variant_id"]}:
                                continue
                            self._rotation_assignments[assignment_id] = (
                                self.store.reject_experiment_assignment(
                                    assignment_id, reason,
                                    detail={"variant_id": variant_id},
                                    now=timestamp))
                (evaluated if len(variant_records) == len(variant_proposals)
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
        if self._rotation_enabled and self._rotation_assignments:
            for assignment_id, assignment in list(
                    self._rotation_assignments.items()):
                baseline_keys = decision_keys_by_variant.get(
                    assignment["baseline_variant_id"], set())
                candidate_keys = decision_keys_by_variant.get(
                    assignment["candidate_variant_id"], set())
                comparable = sorted(baseline_keys & candidate_keys)
                if (assignment["status"] not in {"COMPLETED", "REJECTED"}
                        and comparable):
                    assignment = self.store.record_experiment_observations(
                        assignment_id, [{
                            "observation_key": key,
                            "observed_ts": timestamp,
                            "detail": {
                                "scope_key": self.scope_key,
                                "baseline_variant_id": assignment[
                                    "baseline_variant_id"],
                                "candidate_variant_id": assignment[
                                    "candidate_variant_id"],
                                "batch_id": assignment.get("batch_id"),
                            },
                        } for key in comparable], now=timestamp)
                if assignment["status"] not in {"COMPLETED", "REJECTED"}:
                    assignment = self.store.maybe_complete_experiment_assignment(
                        assignment_id, now=timestamp)
                self._rotation_assignments[assignment_id] = assignment
                if assignment["status"] in {"COMPLETED", "REJECTED"}:
                    candidate_id = assignment["candidate_variant_id"]
                    if candidate_id != assignment["baseline_variant_id"]:
                        self._retired_variant_ids.add(candidate_id)
            active = {
                str(assignment["candidate_variant_id"]): assignment
                for assignment in self._rotation_assignments.values()
                if assignment["status"] not in {"COMPLETED", "REJECTED"}
            }
            self._active_rotation_ids = {
                self._rotation_baseline.variant_id} | set(active)
            ordered = sorted(
                active.values(),
                key=lambda item: (
                    int(item.get("batch_ordinal", 0)),
                    str(item.get("assignment_id") or "")))
            self._rotation_assignment = ordered[0] if ordered else None
            if ordered and ordered[0].get("batch_id"):
                self._rotation_batch_id = str(ordered[0]["batch_id"])
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
            "experiment_assignments": list(self._rotation_assignments.values()),
            "experiment_batch_id": self._rotation_batch_id,
            "resource_cap": self._rotation_resource_cap
            if self._rotation_enabled else None,
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
                target_variant_id = decision.get("target_variant_id")
                if (target_variant_id is not None
                        and str(target_variant_id) != str(variant_id)):
                    continue
                record = self._evaluate_one(
                    variant_id, snapshot, decision, state, timestamp,
                    cycle_id, pending_opens)
                variant_records.append(record)
                if (record.proposal_id
                        and record.reason != "proposal already evaluated"):
                    pending_decisions.append(self._paper_decision(
                        variant_id, cycle_id, decision, record, timestamp))
            self._refresh_metrics(
                state, snapshot, timestamp, self._configs[variant_id])
            self._apply_circuit_breakers(
                state, self._configs[variant_id], timestamp)
            return (state, version, variant_records, pending_opens,
                    pending_decisions)
        except Exception as exc:                       # noqa: BLE001
            return exc

    @staticmethod
    def _execution_enrichment(row: dict | None) -> dict:
        return (dict((row or {}).get(brain.ENRICHMENT_KEY) or {})
                if isinstance(row, dict) else {})

    @classmethod
    def _observation_age(
            cls, row: dict, timestamp_key: str, now: float, cfg: dict,
            label: str) -> tuple[float | None, str | None]:
        enrichment = cls._execution_enrichment(row)
        raw = enrichment.get(timestamp_key)
        try:
            timestamp_ms = int(float(raw))
        except (TypeError, ValueError):
            return None, f"{label} has no valid exchange timestamp"
        now_ms = float(now) * 1000.0
        if timestamp_ms <= 0:
            return None, f"{label} exchange timestamp is invalid"
        if timestamp_ms > now_ms + 5_000:
            return None, f"{label} exchange timestamp is in the future"
        age_seconds = max(0.0, (now_ms - timestamp_ms) / 1000.0)
        max_age = float(cfg["execution"]["max_market_data_age_seconds"])
        if age_seconds > max_age:
            return age_seconds, (
                f"{label} is {age_seconds:.1f}s old at evaluation; limit is "
                f"{max_age:.1f}s")
        return age_seconds, None

    @classmethod
    def _fresh_directional_quote(
            cls, position: dict, row: dict, now: float,
            cfg: dict) -> tuple[float, dict] | None:
        age_seconds, error = cls._observation_age(
            row, "ticker_ts", now, cfg, "ticker")
        if error:
            return None
        enrichment = cls._execution_enrichment(row)
        key = ("ticker_best_bid" if position["direction"] == "long"
               else "ticker_best_ask")
        try:
            price = float(enrichment[key])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(price) or price <= 0:
            return None
        return price, {
            "ticker_ts": enrichment.get("ticker_ts"),
            "ticker_age_seconds_at_evaluation": age_seconds,
            "quote_side": key,
            "spread_in_exit_price": True,
        }

    @classmethod
    def _depth_entry_fill(
            cls, row: dict, direction: str, notional: float,
            cfg: dict, now: float) -> tuple[dict | None, str | None]:
        """Walk one observed OKX book; require a complete bounded fill."""
        enrichment = cls._execution_enrichment(row)
        if enrichment.get("book_observation_error"):
            return None, str(enrichment["book_observation_error"])
        book_age, freshness_error = cls._observation_age(
            row, "book_ts", now, cfg, "order book")
        if freshness_error:
            return None, freshness_error
        buying = direction == "long"
        levels = enrichment.get(
            "book_ask_levels" if buying else "book_bid_levels") or []
        try:
            mid = float(enrichment["book_mid"])
            contract_size = float(enrichment["book_contract_size"])
            requested_notional = float(notional)
        except (KeyError, TypeError, ValueError):
            return None, "observed order-book depth metadata is unavailable"
        if (not math.isfinite(mid) or mid <= 0
                or not math.isfinite(contract_size) or contract_size <= 0
                or not math.isfinite(requested_notional)
                or requested_notional <= 0):
            return None, "observed order-book depth metadata is invalid"
        requested = requested_notional / (mid * contract_size)
        max_slippage = float(
            cfg["execution"]["max_order_book_slippage_pct"])
        boundary = mid * (1 + max_slippage / 100.0) if buying else (
            mid * (1 - max_slippage / 100.0))
        remaining = requested
        filled = 0.0
        quote_cost = 0.0
        consumed = []
        for raw in levels:
            try:
                price, available = float(raw[0]), float(raw[1])
            except (TypeError, ValueError, IndexError):
                return None, "observed order book contains an invalid level"
            if (not math.isfinite(price) or not math.isfinite(available)
                    or price <= 0 or available < 0):
                return None, "observed order book contains an invalid level"
            inside = price <= boundary if buying else price >= boundary
            if not inside:
                break
            take = min(remaining, available)
            if take > 0:
                quote_cost += take * price
                filled += take
                remaining -= take
                consumed.append([price, take])
            if remaining <= max(1e-12, requested * 1e-9):
                break
        if remaining > max(1e-12, requested * 1e-9) or filled <= 0:
            available_notional = quote_cost * contract_size
            return None, (
                f"observed depth supports ${available_notional:.2f} of "
                f"${requested_notional:.2f} inside the {max_slippage:.4f}% cap")
        vwap = quote_cost / filled
        return {
            "order_type": "marketable_ioc_full_or_reject",
            "status": "filled",
            "book_ts": enrichment.get("book_ts"),
            "book_age_seconds": book_age,
            "requested_contracts": requested,
            "filled_contracts": filled,
            "contract_size": contract_size,
            "fill_price": vwap,
            "filled_notional": filled * contract_size * vwap,
            "mid": mid,
            "max_slippage_pct": max_slippage,
            "realized_slippage_pct": abs(vwap - mid) / mid * 100.0,
            "consumed_levels": consumed,
            "partial_fill_policy": "reject_unless_full_depth_is_observed",
        }, None

    @classmethod
    def _maker_entry_order(
            cls, row: dict, direction: str, notional: float,
            cfg: dict, now: float) -> tuple[dict | None, str | None]:
        """Register a passive probe; no same-snapshot fill is possible."""
        enrichment = cls._execution_enrichment(row)
        if enrichment.get("book_observation_error"):
            return None, str(enrichment["book_observation_error"])
        book_age, freshness_error = cls._observation_age(
            row, "book_ts", now, cfg, "order book")
        if freshness_error:
            return None, freshness_error
        buying = direction == "long"
        try:
            price = float(enrichment[
                "book_best_bid" if buying else "book_best_ask"])
            contract_size = float(enrichment["book_contract_size"])
            top_size = float(enrichment[
                "book_top_bid_size" if buying else "book_top_ask_size"])
            book_ts = int(float(enrichment["book_ts"]))
        except (KeyError, TypeError, ValueError):
            return None, "passive order-book metadata is unavailable"
        requested = float(notional) / (price * contract_size)
        if (not all(math.isfinite(value) and value > 0 for value in (
                price, contract_size, top_size, requested)) or book_ts <= 0):
            return None, "passive order-book metadata is invalid"
        if requested > top_size + max(1e-12, top_size * 1e-9):
            return None, (
                f"passive full-fill probe needs {requested:.8g} contracts but "
                f"the observed touch displays only {top_size:.8g}")
        return {
            "order_type": "post_only_full_or_unfilled",
            "status": "pending",
            "book_ts": book_ts,
            "book_age_seconds": book_age,
            "limit_price": price,
            "requested_contracts": requested,
            "top_size_at_submission": top_size,
            "contract_size": contract_size,
            "filled_notional": requested * contract_size * price,
            "penetration_bps": float(
                cfg["execution"]["paper_maker_fill_penetration_bps"]),
            "ttl_seconds": float(
                cfg["execution"]["paper_maker_order_ttl_seconds"]),
            "fill_rule": (
                "a 1m candle must open strictly after submission and the book "
                "observation, its full one-minute interval must end by order "
                "expiry, and it must penetrate beyond the submitted quote; "
                "touching the quote is not a fill"),
            "partial_fill_policy": "full_or_unfilled",
        }, None

    @classmethod
    def _entry_execution(
            cls, model, row: dict, direction: str, notional: float,
            cfg: dict, now: float) -> tuple[dict | None, str | None]:
        if model.entry_style == "top_book_maker":
            return cls._maker_entry_order(row, direction, notional, cfg, now)
        return cls._depth_entry_fill(row, direction, notional, cfg, now)

    @staticmethod
    def _rebase_entry_execution(
            execution: dict, model, row: dict, sized: dict,
            cfg: dict) -> tuple[dict | None, str | None]:
        """Attribute the exact observed fill without breaching its risk cap."""
        try:
            notional = float(execution["filled_notional"])
            leverage = float(sized["leverage"])
            allowed_risk = float(sized["risk_usd"])
            stop_pct = float(sized["sl_pct"])
            adverse_funding = float(sized.get("adverse_funding_pct") or 0.0)
        except (KeyError, TypeError, ValueError):
            return None, "exact fill risk metadata is unavailable"
        components = model.cost_components(row, sized, cfg)
        exact_loss_pct = stop_pct + adverse_funding + sum(
            float(components.get(key) or 0.0) for key in (
                "entry_fee_pct", "exit_fee_pct", "spread_pct",
                "entry_slippage_pct", "stop_slippage_pct"))
        exact_risk = notional * exact_loss_pct / 100.0
        if (not all(math.isfinite(value) and value > 0 for value in (
                notional, leverage, allowed_risk, exact_loss_pct, exact_risk))):
            return None, "exact fill risk attribution is invalid"
        tolerance = max(1e-9, allowed_risk * 1e-9)
        if exact_risk > allowed_risk + tolerance:
            return None, (
                f"exact depth-VWAP risk ${exact_risk:.4f} exceeds the "
                f"allowed ${allowed_risk:.4f} cap")
        rebased = dict(execution)
        rebased.update({
            "filled_notional": notional,
            "margin_usd": notional / leverage,
            "risk_usd": exact_risk,
            "risk_loss_pct": exact_loss_pct,
            "original_risk_cap_usd": allowed_risk,
            "sized_notional_usd": float(sized["notional"]),
            "cost_components": components,
        })
        return rebased, None

    @classmethod
    def _capture_funding_events(
            cls, position: dict, row: dict, now: float) -> None:
        enrichment = cls._execution_enrichment(row)
        existing = {
            int(event["timestamp_ms"]): event
            for event in position.get("funding_events") or []
            if isinstance(event, dict) and event.get("timestamp_ms") is not None
            and int(event["timestamp_ms"]) <= int(now * 1000)
        }
        entry_ms = int(float(
            position.get("fill_ts") or position["entry_ts"]) * 1000)
        for event in enrichment.get("realized_funding_events") or []:
            if event.get("status") != "realized":
                continue
            try:
                timestamp_ms = int(event["timestamp_ms"])
                rate_pct = float(event["rate_pct"])
            except (KeyError, TypeError, ValueError):
                continue
            if (entry_ms < timestamp_ms <= int(now * 1000)
                    and math.isfinite(rate_pct)):
                existing[timestamp_ms] = {
                    "timestamp_ms": timestamp_ms,
                    "rate_pct": rate_pct,
                    "source": str(event.get("source") or "unknown"),
                    "status": "realized",
                }
        position["funding_events"] = [
            existing[key] for key in sorted(existing)]

    @classmethod
    def _maker_fill_bar(
            cls, position: dict, row: dict) -> dict | None:
        order = position.get("entry_execution") or {}
        book_ts = int(order.get("book_ts") or 0)
        submitted_ms = int(float(position.get("entry_ts") or 0) * 1000)
        expires_ms = int(float(position.get("order_expires_ts") or 0) * 1000)
        quote = float(order["limit_price"])
        penetration = float(order.get("penetration_bps") or 0) / 10_000.0
        bars = cls._execution_enrichment(row).get("execution_bars") or []
        if expires_ms <= 0:
            return None
        for bar in sorted(bars, key=lambda item: int(item["timestamp_ms"])):
            ts = int(bar["timestamp_ms"])
            if ts <= max(book_ts, submitted_ms):
                continue
            if ts + 60_000 > expires_ms:
                break
            if position["direction"] == "long":
                filled = float(bar["low"]) < quote * (1 - penetration)
            else:
                filled = float(bar["high"]) > quote * (1 + penetration)
            if filled:
                return dict(bar)
        return None

    @classmethod
    def _execution_exit(
            cls, position: dict, row: dict, now: float, cfg: dict
            ) -> tuple[str, float, dict, bool, float] | None:
        """Resolve from observed 1m paths; stop wins an ambiguous bar."""
        enrichment = cls._execution_enrichment(row)
        bars = sorted(
            (dict(bar) for bar in enrichment.get("execution_bars") or []),
            key=lambda item: int(item["timestamp_ms"]),
        )
        entry_bar = int(position.get("entry_bar_ts_ms") or (
            math.floor(float(position.get("fill_ts")
                       or position["entry_ts"]) / 60.0) * 60_000))
        last_bar = int(position.get("last_execution_bar_ts_ms") or entry_bar)
        eligible = [bar for bar in bars
                    if int(bar["timestamp_ms"]) > last_bar
                    and int(bar["timestamp_ms"]) >= entry_bar]
        expected = last_bar + 60_000

        def invalid_coverage_close(detail: dict):
            quote = cls._fresh_directional_quote(position, row, now, cfg)
            if quote is None:
                position["data_status"] = "awaiting_fresh_market_data"
                return None
            price, quote_evidence = quote
            evidence = {
                "valid_for_inference": False,
                "reason": "1m execution coverage gap",
                "exit_ts": now,
                **detail,
                **quote_evidence,
            }
            return "execution_data_gap", price, evidence, False, now

        stop = float(position["stop_price"])
        target = float(position["take_price"])
        entry = float(position["entry_price"])
        leverage = max(1.0, float(position.get("leverage") or 1.0))
        insolvency = (entry * (1 - 1 / leverage)
                      if position["direction"] == "long"
                      else entry * (1 + 1 / leverage))
        observed_through = last_bar
        fill_bar_pending = bool(position.get("maker_fill_bar_pending"))
        for bar in eligible:
            ts = int(bar["timestamp_ms"])
            if ts > expected:
                return invalid_coverage_close({
                    "expected_next_bar_ms": expected,
                    "first_available_bar_ms": ts,
                    "coverage_kind": "internal_gap",
                })
            if ts < expected:
                continue
            open_ = float(bar["open"])
            high = float(bar["high"])
            low = float(bar["low"])
            is_fill_bar = fill_bar_pending and ts == entry_bar
            complete = bool(bar.get(
                "complete", ts + 60_000 <= int(now * 1000)))
            # OHLCV identifies the minute, not the intra-bar touch. Persist
            # the observed bar timestamp rather than the later polling cycle;
            # this is the conservative chronology available from the feed.
            exit_ts = ts / 1000.0
            evidence = {
                "bar": bar,
                "bar_conflict_policy": "stop_before_target",
                "gap_policy": "adverse_bar_open_when_beyond_stop",
                "exit_bar_timestamp_ms": ts,
                "exit_time_policy": "observed_bar_timestamp",
                "exit_ts": exit_ts,
            }
            if is_fill_bar:
                evidence["maker_fill_bar_policy"] = (
                    "stop touch is adverse; target-only touch is not credited")
            if position["direction"] == "long":
                if open_ <= insolvency:
                    position["last_execution_bar_ts_ms"] = ts
                    return "paper_insolvency", open_, evidence, False, exit_ts
                stop_hit, target_hit = low <= stop, high >= target
                if stop_hit:
                    position["last_execution_bar_ts_ms"] = ts
                    return "stop", min(stop, open_), evidence, True, exit_ts
                if target_hit and not is_fill_bar:
                    position["last_execution_bar_ts_ms"] = ts
                    return "target", target, evidence, True, exit_ts
            else:
                if open_ >= insolvency:
                    position["last_execution_bar_ts_ms"] = ts
                    return "paper_insolvency", open_, evidence, False, exit_ts
                stop_hit, target_hit = high >= stop, low <= target
                if stop_hit:
                    position["last_execution_bar_ts_ms"] = ts
                    return "stop", max(stop, open_), evidence, True, exit_ts
                if target_hit and not is_fill_bar:
                    position["last_execution_bar_ts_ms"] = ts
                    return "target", target, evidence, True, exit_ts
            observed_through = ts
            if not complete:
                break
            position["last_execution_bar_ts_ms"] = ts
            expected = ts + 60_000
            if is_fill_bar:
                position["maker_fill_bar_pending"] = False
                fill_bar_pending = False

        carry = cls._carry_normalisation_exit(position, row, now, cfg)
        if carry is not None:
            return carry
        if now < float(position["deadline_ts"]):
            return None
        observation_bar = math.floor(now / 60.0) * 60_000
        if observed_through < observation_bar:
            return invalid_coverage_close({
                "expected_next_bar_ms": observed_through + 60_000,
                "coverage_observation_bar_ms": observation_bar,
                "coverage_kind": "empty_or_trailing_gap",
            })
        quote = cls._fresh_directional_quote(position, row, now, cfg)
        if quote is None:
            position["data_status"] = "awaiting_fresh_market_data"
            return None
        price, quote_evidence = quote
        return "timeout", price, {
            **quote_evidence,
            "exit_ts": now,
            "timeout_after_complete_execution_path": True,
        }, True, now

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
            if not isinstance(row, dict):
                state["unpriced_positions"] = int(
                    state.get("unpriced_positions") or 0) + 1
                # Never turn an old persisted mark into a fabricated timeout
                # fill. Draining waits for fresh execution evidence.
                position["data_status"] = "awaiting_fresh_market_data"
                kept.append(position)
                continue

            result = None
            exit_ts = now
            if position.get("order_status") == "pending":
                fill_bar = self._maker_fill_bar(position, row)
                if fill_bar is not None:
                    fill_bar_ts = int(fill_bar["timestamp_ms"])
                    fill_ts = fill_bar_ts / 1000.0
                    position["order_status"] = "filled"
                    position["fill_ts"] = fill_ts
                    position["entry_bar_ts_ms"] = fill_bar_ts
                    # The fill bar is outcome evidence too. Keep it pending
                    # for one conservative pass instead of skipping directly
                    # to the following minute.
                    position["last_execution_bar_ts_ms"] = fill_bar_ts - 60_000
                    position["maker_fill_bar_pending"] = True
                    position["deadline_ts"] = (
                        fill_ts + float(position["horizon_hours"]) * 3600.0)
                    execution = dict(position.get("entry_execution") or {})
                    execution.update({
                        "status": "filled",
                        "fill_ts": fill_ts,
                        "fill_observed_ts": fill_ts,
                        "fill_detection_ts": now,
                        "fill_price": float(position["entry_price"]),
                        "fill_evidence_bar": fill_bar,
                    })
                    position["entry_execution"] = execution
                    position["data_status"] = "observed"
                    resolved_records.append(ShadowRecord(
                        variant_id, position["symbol"],
                        position.get("signal_ts"), "filled",
                        position.get("direction"),
                        position.get("setup_type"),
                        reason="maker quote penetrated on a later 1m bar",
                        notional=position.get("notional"),
                        proposal_id=position.get("proposal_id"),
                        paper_trade_id=position.get("trade_id"),
                        paper_action="fill"))
                elif now < float(position["order_expires_ts"]):
                    kept.append(position)
                    continue
                else:
                    result = "unfilled"
                    exit_price = float(position["entry_price"])
                    net_pnl = r_multiple = 0.0
                    valid_for_inference = False
                    exit_evidence = {
                        "entry_execution": position.get("entry_execution"),
                        "cancelled_at_ts": now,
                        "exit_ts": now,
                        "valid_for_inference": False,
                        "reason": "passive quote did not meet the fill rule",
                    }
            if result is None:
                resolved = self._execution_exit(position, row, now, cfg)
                if resolved is None:
                    self._capture_funding_events(position, row, now)
                    if position.get("data_status") != "awaiting_fresh_market_data":
                        position["data_status"] = (
                            "observed"
                            if self._fresh_directional_quote(
                                position, row, now, cfg) is not None
                            else "awaiting_fresh_market_data")
                    kept.append(position)
                    continue
                (result, exit_price, exit_evidence,
                 valid_for_inference, exit_ts) = resolved
                self._capture_funding_events(position, row, exit_ts)
                net_pnl, r_multiple = self._paper_pnl(
                    position, exit_price, result, exit_ts,
                    spread_in_exit_price=bool(
                        exit_evidence.get("spread_in_exit_price")))

            if result == "unfilled":
                state["unfilled_count"] = int(
                    state.get("unfilled_count") or 0) + 1
            elif not valid_for_inference:
                state["invalid_outcome_count"] = int(
                    state.get("invalid_outcome_count") or 0) + 1
            elif net_pnl < 0:
                state["loss_count"] = int(state.get("loss_count") or 0) + 1
                state["consecutive_losses"] = int(
                    state.get("consecutive_losses") or 0) + 1
                cooldown = float(cfg["risk"]["cooldown_minutes_after_loss"])
                state.setdefault("cooldowns", {})[position["symbol"]] = (
                    exit_ts + cooldown * 60)
            else:
                state["win_count"] = int(state.get("win_count") or 0) + 1
                state["consecutive_losses"] = 0

            execution_record = {
                "entry": position.get("entry_execution"),
                "fill_ts": position.get("fill_ts") or position.get("entry_ts"),
                "funding_events": position.get("funding_events") or [],
                "exit": exit_evidence,
            }
            closed_trades.append({
                "trade_id": position["trade_id"], "exit_ts": exit_ts,
                "exit_price": exit_price, "result": result,
                "net_pnl_usd": net_pnl, "r_multiple": r_multiple,
                "valid_for_inference": valid_for_inference,
                "execution": execution_record,
            })
            state["cash_usdt"] = float(state["cash_usdt"]) + net_pnl
            state["realized_pnl_usdt"] = (
                float(state.get("realized_pnl_usdt") or 0) + net_pnl)
            state.get("active_trades", {}).pop(position["symbol"], None)
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
        self._refresh_metrics(state, snapshot, now, cfg)
        self._apply_circuit_breakers(state, cfg, now)
        version = self.store.commit_paper_portfolio(
            self.scope_key, variant_id, state, version,
            closed_trades=closed_trades, now=now)
        records.extend(resolved_records)
        return state, version

    @classmethod
    def _carry_normalisation_exit(
            cls, position: dict, row: dict, now: float, cfg: dict
            ) -> tuple[str, float, dict, bool, float] | None:
        """Close a carry position once the carry it was opened for is spent.

        funding-carry states its own mechanism plainly: the return source is
        the funding it collects, not a directional forecast. Its contract none
        the less exited on a 2R price move, so the strategy was scored on
        whether price happened to travel far enough - the one thing it claims
        not to be forecasting - and a position could be closed at a profit
        while the carry it was opened for was still paying, or held while the
        carry had already gone.

        Reached only after the bar replay has found no stop, so a stop still
        wins, and only before the deadline check, so the ten-day timeout
        remains the outer bound.

        Fails closed in every direction. A position on any other exit policy,
        a missing or non-finite funding percentile, or an unavailable fresh
        quote all return None and leave the existing behaviour untouched: an
        absent observation must never manufacture an exit.
        """
        if str(position.get("exit_policy") or "") != "carry_until_normalised":
            return None
        threshold = position.get("carry_exit_funding_percentile")
        if threshold is None:
            return None
        percentile = _row_finite(
            _row_field(row, "funding_percentile_30"))
        if percentile is None or percentile > float(threshold):
            return None
        quote = cls._fresh_directional_quote(position, row, now, cfg)
        if quote is None:
            position["data_status"] = "awaiting_fresh_market_data"
            return None
        price, quote_evidence = quote
        return "funding_normalised", price, {
            **quote_evidence,
            "exit_ts": now,
            "funding_percentile_30": percentile,
            "carry_exit_funding_percentile": float(threshold),
        }, True, now



    @staticmethod
    def _paper_pnl(
            position: dict, exit_price: float, result: str,
            exit_ts: float, *,
            spread_in_exit_price: bool = False) -> tuple[float, float]:
        entry = float(position["entry_price"])
        sign = 1.0 if position["direction"] == "long" else -1.0
        gross_pct = sign * (float(exit_price) - entry) / entry * 100.0
        components = position.get("cost_components")
        if isinstance(components, dict):
            cost_pct = sum(float(components.get(key) or 0.0) for key in (
                "entry_fee_pct", "exit_fee_pct", "entry_slippage_pct"))
            if not spread_in_exit_price:
                cost_pct += float(components.get("spread_pct") or 0.0)
            if result in {"stop", "paper_insolvency"}:
                cost_pct += float(components.get("stop_slippage_pct") or 0.0)
            # Positive realized funding is paid by longs and received by
            # shorts. Current/predicted rates are never repeated forward.
            realized_rates = []
            for event in position.get("funding_events") or []:
                try:
                    rate = float(event["rate_pct"])
                    timestamp_ms = int(event["timestamp_ms"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (math.isfinite(rate)
                        and timestamp_ms <= int(float(exit_ts) * 1000)):
                    realized_rates.append(rate)
            cost_pct += sign * sum(realized_rates)
        else:
            # Preserve restart compatibility with earlier position records.
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
        _, ticker_error = self._observation_age(
            row, "ticker_ts", now, cfg, "ticker")
        if ticker_error:
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type,
                f"simulated order rejected: {ticker_error}",
                plan.get("stop_loss_pct"), plan.get("take_profit_pct"),
                proposal_id=proposal_id)
        entry_execution, execution_veto = self._entry_execution(
            model, row, str(direction), float(sized["notional"]), cfg, now)
        if entry_execution is None:
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type,
                f"simulated order rejected: {execution_veto}",
                plan.get("stop_loss_pct"), plan.get("take_profit_pct"),
                proposal_id=proposal_id)
        entry_execution, execution_veto = self._rebase_entry_execution(
            entry_execution, model, row, sized, cfg)
        if entry_execution is None:
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type,
                f"simulated order rejected: {execution_veto}",
                plan.get("stop_loss_pct"), plan.get("take_profit_pct"),
                proposal_id=proposal_id)
        current_margin = sum(float(position.get("margin_usd") or 0.0)
                             for position in state.get("positions") or [])
        proposed_margin = float(entry_execution["margin_usd"])
        margin_limit = (
            float(state.get("equity_usdt") or 0)
            * float(cfg["risk"]["max_margin_usage_pct"]) / 100.0)
        if current_margin + proposed_margin > margin_limit:
            return self._record(
                variant_id, symbol, signal_ts, state, "vetoed",
                direction, setup_type,
                "simulated order rejected: reserved margin limit reached",
                plan.get("stop_loss_pct"), plan.get("take_profit_pct"),
                proposal_id=proposal_id)
        trade_id = self._open_paper_trade(
            variant_id, cycle_id, proposal_id, decision, sized,
            row, state, now, pending_opens, entry_execution)
        pending_maker = entry_execution["status"] == "pending"
        return self._record(
            variant_id, symbol, signal_ts, state, "proposed",
            direction, setup_type,
            ("passive order submitted; fill is unresolved"
             if pending_maker else None), sized.get("sl_pct"),
            sized.get("tp_pct"), entry_execution.get("filled_notional"), proposal_id,
            trade_id, "submit" if pending_maker else "open")

    def _open_paper_trade(
            self, variant_id: str, cycle_id: str | None, proposal_id: str,
            decision: dict, sized: dict, source_row: dict, state: dict, now: float,
            pending_opens: list[dict], entry_execution: dict) -> str:
        pending_maker = entry_execution["status"] == "pending"
        entry = float(entry_execution.get("fill_price")
                      or entry_execution["limit_price"])
        notional = float(entry_execution["filled_notional"])
        stop_move = entry * float(sized["sl_pct"]) / 100.0
        take_move = entry * float(sized["tp_pct"]) / 100.0
        long = decision["direction"] == "long"
        model = self._models[variant_id]
        leverage = float(sized["leverage"])
        insolvency_distance_pct = 100.0 / leverage
        if float(sized["sl_pct"]) >= insolvency_distance_pct:
            raise ValueError(
                "paper stop is not inside the conservative insolvency proxy")
        cost_components = dict(entry_execution.get("cost_components") or
                               model.cost_components(
                                   source_row, sized,
                                   self._configs[variant_id]))
        entry_bar_ts = math.floor(now / 60.0) * 60_000
        horizon_hours = model.horizon_for(self._configs[variant_id])
        position = {
            "proposal_id": proposal_id,
            "symbol": decision["symbol"],
            "direction": decision["direction"],
            "side": decision["direction"],
            "setup_type": decision.get("setup_type"),
            "signal_ts": decision.get("signal_ts"),
            "entry_ts": now,
            "fill_ts": None if pending_maker else now,
            "order_status": "pending" if pending_maker else "filled",
            "entry_price": entry,
            "mark_price": entry,
            "notional": notional,
            "risk_usd": float(entry_execution["risk_usd"]),
            "leverage": leverage,
            "margin_usd": float(entry_execution["margin_usd"]),
            "stop_price": entry - stop_move if long else entry + stop_move,
            "take_price": entry + take_move if long else entry - take_move,
            # Both fixed at entry, from the contract, so the exit rule cannot
            # be revised once the outcome starts to be observable.
            "exit_policy": model.exit_policy,
            "carry_exit_funding_percentile": (
                model.carry_exit_funding_percentile),
            "horizon_hours": horizon_hours,
            "deadline_ts": now + horizon_hours * 3600,
            "order_expires_ts": (
                now + float(entry_execution.get("ttl_seconds") or 0.0)
                if pending_maker else None),
            "entry_bar_ts_ms": entry_bar_ts,
            "last_execution_bar_ts_ms": entry_bar_ts,
            "round_trip_cost_pct": float(sized["estimated_cost_pct"]),
            "stop_slippage_pct": float(
                self._configs[variant_id]["trading_costs"]
                ["expected_stop_slippage_pct"]),
            "cost_components": cost_components,
            "funding_events": [],
            "entry_execution": dict(entry_execution),
            "data_status": "pending_fill" if pending_maker else "observed",
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
                "entry_execution": entry_execution,
                "execution_policy": {
                    "bar_timeframe": "1m",
                    "bar_conflict_policy": "stop_before_target",
                    "missing_data_policy": "no_stale_fill",
                    "paper_insolvency_proxy_distance_pct": (
                        insolvency_distance_pct),
                },
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
            "reserved_margin_usdt": 0.0,
            "margin_usage_pct": 0.0,
            "cooldowns": {},
            "active_trades": {},
            "loss_count": 0,
            "consecutive_losses": 0,
            "win_count": 0,
            "unfilled_count": 0,
            "invalid_outcome_count": 0,
            "max_drawdown_pct": 0.0,
            "failure_count": 0,
            "revoked_reason": None,
        })

    @classmethod
    def _refresh_metrics(
            cls, state: dict, snapshot: dict, now: float, cfg: dict) -> None:
        unrealized = gross = net = open_risk = reserved_margin = 0.0
        fresh_mark_seen = False
        for position in state.get("positions") or []:
            row = snapshot.get(position["symbol"])
            quote = (cls._fresh_directional_quote(
                position, row, now, cfg) if isinstance(row, dict) else None)
            fresh_mark_seen = fresh_mark_seen or quote is not None
            mark = float(quote[0] if quote is not None else
                         position.get("mark_price") or position["entry_price"])
            position["mark_price"] = mark
            entry = float(position["entry_price"])
            sign = 1.0 if position["direction"] == "long" else -1.0
            if position.get("order_status") != "pending":
                unrealized += (
                    float(position["notional"]) * sign * (mark - entry) / entry)
            gross += abs(float(position["notional"]))
            net += (float(position["notional"])
                    if position["direction"] == "long"
                    else -float(position["notional"]))
            open_risk += float(position.get("risk_usd") or 0)
            reserved_margin += float(position.get("margin_usd") or 0)
        state["unrealized_pnl_usdt"] = unrealized
        state["equity_usdt"] = float(state.get("cash_usdt") or 0) + unrealized
        state["gross_notional"] = gross
        state["net_notional"] = net
        state["open_risk_usdt"] = open_risk
        state["reserved_margin_usdt"] = reserved_margin
        state["margin_usage_pct"] = (
            reserved_margin / float(state["equity_usdt"]) * 100.0
            if float(state["equity_usdt"]) > 0 else 100.0)
        if fresh_mark_seen or not state.get("positions"):
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
                 active_strategy_id: str,
                 llm_evaluator: ShadowEvaluator | None = None,
                 staged_evaluators: dict | None = None,
                 staged_contracts: list | None = None,
                 staged_store_path: str | Path | None = None,
                 staged_cfg: dict | None = None) -> None:
        if not evaluators:
            raise ValueError("strategy shadow coordinator needs an evaluator")
        self.evaluators = dict(evaluators)
        self.active_strategy_id = str(active_strategy_id)
        # LLM decisions use a separate scope from deterministic comparisons;
        # the two proposal sources must never be pooled into one verdict.
        self.llm_evaluator = llm_evaluator
        # Machine-authored mechanisms run in their own lane on one fixed
        # harness. Kept out of self.evaluators so a staged claim can never be
        # counted among the registered strategies' comparison arms.
        self.staged_evaluators = dict(staged_evaluators or {})
        self.staged_contracts = list(staged_contracts or [])
        # Retired staged lanes remain here until their local paper positions
        # are flat. They never receive new proposals. Keeping the evaluator
        # object (rather than rebuilding it) preserves the persisted account
        # identity and lets the normal exit model drain positions safely.
        self._staged_retired_evaluators: dict[str, ShadowEvaluator] = {}
        self._staged_refresh_errors: dict[str, str] = {}
        self._staged_store_path = (
            str(staged_store_path) if staged_store_path is not None else None)
        self._staged_cfg = deepcopy(staged_cfg) if staged_cfg is not None else None
        self.last_staged_refresh: dict = {
            "status": "not_configured",
            "active_contracts": sorted(
                str(contract.contract_id) for contract in self.staged_contracts),
            "added": [], "reused": [], "retired": [], "drained": [],
            "errors": {},
        }
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
        held = set()
        for evaluator in self.evaluators.values():
            held.update(evaluator.held_symbols())
        staged = list(self.staged_evaluators.items())
        staged.extend(self._staged_retired_evaluators.items())
        for contract_id, evaluator in staged:
            try:
                held.update(evaluator.held_symbols())
            except Exception as exc:                       # noqa: BLE001
                # One broken research lane must not hide symbols held by the
                # other lanes from the market-data fetch. The next refresh or
                # evaluation exposes the same failure in its report.
                self._staged_refresh_errors.setdefault(
                    str(contract_id),
                    f"held-symbol inspection failed: {type(exc).__name__}: {exc}")
        return sorted(held)

    def refresh_staged_lanes(self, now: float | None = None) -> dict:
        """Reload active staged contracts at a safe cycle boundary.

        The staging registry is read completely before any in-memory lane is
        changed. Existing evaluators are reused (and therefore keep their
        paper-account identity and local positions); contracts that vanished
        from the active registry move to a drain-only set until flat. New
        contracts get isolated evaluators. A failed read or enrollment leaves
        the previous lanes intact and is returned in ``errors`` rather than
        silently dropping a candidate.

        This method only changes shadow bookkeeping. It never touches the
        registered strategy evaluators or live order state. Call it once at a
        cycle boundary before ``advance``/``evaluate``.
        """
        timestamp = time.time() if now is None else float(now)
        current_ids = {
            str(contract.contract_id) for contract in self.staged_contracts}
        report = {
            "timestamp": timestamp,
            "status": "unchanged",
            "active_contracts": sorted(current_ids),
            "added": [],
            "reused": [],
            "retired": [],
            "draining": sorted(self._staged_retired_evaluators),
            "drained": [],
            "errors": {},
        }
        path = self._staged_store_path
        if not path:
            report["status"] = "not_configured"
            report["errors"]["__store__"] = (
                "staged contract store path is not configured")
            self.last_staged_refresh = report
            return dict(report)
        store_path = Path(path)
        if not store_path.is_file():
            report["status"] = "unavailable"
            report["errors"]["__store__"] = (
                f"staged contract store does not exist: {store_path}")
            self.last_staged_refresh = report
            return dict(report)
        try:
            from .staging import StagingStore

            active_contracts = StagingStore(store_path).active()
        except Exception as exc:                           # noqa: BLE001
            report["status"] = "error"
            report["errors"]["__store__"] = (
                f"{type(exc).__name__}: {exc}")
            self.last_staged_refresh = report
            return dict(report)

        # Build against a stable config snapshot. If a caller constructed a
        # coordinator manually, fall back to an existing staged evaluator's
        # config; no active lane means there is no safe config to invent.
        staged_cfg = deepcopy(self._staged_cfg)
        if staged_cfg is None:
            for evaluator in self.staged_evaluators.values():
                candidate_cfg = getattr(evaluator, "base_cfg", None)
                if isinstance(candidate_cfg, dict):
                    staged_cfg = deepcopy(candidate_cfg)
                    break
        old_evaluators = dict(self.staged_evaluators)
        active_ids = {str(contract.contract_id) for contract in active_contracts}
        next_evaluators: dict[str, ShadowEvaluator] = {}
        next_contracts = list(active_contracts)

        for contract in active_contracts:
            contract_id = str(contract.contract_id)
            existing = old_evaluators.get(contract_id)
            if existing is None:
                # Staging is append-only, but reusing a drain-only evaluator
                # here keeps the position/version invariant safe if a caller
                # restores an archived registry snapshot.
                existing = self._staged_retired_evaluators.pop(
                    contract_id, None)
            if existing is not None:
                next_evaluators[contract_id] = existing
                report["reused"].append(contract_id)
                self._staged_refresh_errors.pop(contract_id, None)
                continue
            if staged_cfg is None:
                report["errors"][contract_id] = (
                    "cannot enroll staged contract: no staged evaluator config")
                continue
            try:
                next_evaluators[contract_id] = _build_staged_evaluator(
                    contract, staged_cfg, self.scope_key, self.store)
                report["added"].append(contract_id)
                self._staged_refresh_errors.pop(contract_id, None)
            except Exception as exc:                         # noqa: BLE001
                # Keep the contract visible in ``staged_contracts`` so the
                # next evaluation reports it as unavailable and the next
                # refresh can retry it. Never silently discard a candidate.
                report["errors"][contract_id] = (
                    f"{type(exc).__name__}: {exc}")
                self._staged_refresh_errors[contract_id] = (
                    report["errors"][contract_id])

        for contract_id, evaluator in old_evaluators.items():
            if contract_id in active_ids:
                continue
            self._staged_retired_evaluators[contract_id] = evaluator
            report["retired"].append(contract_id)
            self._staged_refresh_errors.pop(contract_id, None)

        # Commit the complete active set only after the registry read and all
        # possible enrollments have been attempted. Existing positions remain
        # on their original evaluator; failed new lanes stay visible by id.
        self.staged_contracts = next_contracts
        self.staged_evaluators = next_evaluators
        report["active_contracts"] = sorted(
            str(contract.contract_id) for contract in next_contracts)
        report["draining"] = sorted(self._staged_retired_evaluators)
        report["status"] = (
            "partial" if report["errors"] else
            "changed" if report["added"] or report["retired"] else
            "unchanged")
        self.last_staged_refresh = report
        return dict(report)

    # Short alias for callers that use the lane name rather than the plural
    # coordinator terminology. Both names intentionally share one contract.
    def refresh_staged(self, now: float | None = None) -> dict:
        return self.refresh_staged_lanes(now=now)

    def refresh_staged_contracts(self, now: float | None = None) -> dict:
        """Compatibility alias for integrations naming the registry object."""
        return self.refresh_staged_lanes(now=now)

    def _mark_staged_drained(self, contract_id: str) -> None:
        """Keep refresh telemetry aligned after a drain reaches flat."""
        report = dict(self.last_staged_refresh)
        drained = list(report.get("drained") or [])
        if contract_id not in drained:
            drained.append(contract_id)
        report["drained"] = sorted(drained)
        report["draining"] = sorted(self._staged_retired_evaluators)
        self.last_staged_refresh = report

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
        records.extend(self._advance_staged_lanes(snapshot, timestamp))
        return records

    def _advance_staged_lanes(
            self, snapshot: dict, timestamp: float) -> list[ShadowRecord]:
        """Advance active and drain-only staged accounts without opening."""
        records: list[ShadowRecord] = []
        for contract_id, evaluator in list(self.staged_evaluators.items()):
            try:
                records.extend(evaluator.advance(snapshot, now=timestamp))
            except Exception as exc:                       # noqa: BLE001
                records.extend(self._isolate_failure(
                    evaluator, f"staged:{contract_id}", "advance",
                    exc, timestamp))
        for contract_id, evaluator in list(
                self._staged_retired_evaluators.items()):
            try:
                records.extend(evaluator.advance(snapshot, now=timestamp))
                if not evaluator.held_symbols():
                    self._staged_retired_evaluators.pop(contract_id, None)
                    self._mark_staged_drained(contract_id)
            except Exception as exc:                       # noqa: BLE001
                records.extend(self._isolate_failure(
                    evaluator, f"staged:{contract_id}", "drain",
                    exc, timestamp))
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
                # Cross-strategy comparisons use one deterministic proposal source.
                model = require_complete_contract(strategy_id)
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
        llm_lane = self._evaluate_llm_lane(
            snapshot, timestamp, cycle_id, proposals, advance_accounts)
        records.extend(llm_lane.pop("_records"))
        staged_lane_result = self._evaluate_staged_lane(
            snapshot, timestamp, cycle_id, advance_accounts)
        records.extend(staged_lane_result.pop("_records"))
        self.last_coverage = {
            "scope_key": self.scope_key,
            "strategies": coverage,
            "strategy_count": len(self.evaluators),
            "llm_lane": llm_lane or None,
            "staged_lane": staged_lane_result or None,
        }
        self.last_budget = aggregate_budget
        return records

    def _evaluate_staged_lane(
            self, snapshot: dict, timestamp: float, cycle_id: str | None,
            advance_accounts: bool) -> dict:
        """Evaluate machine-authored mechanisms on the shared harness.

        Each contract is given only its own proposals: two staged mechanisms
        are different claims, and letting one trade on another's signals
        would make every staged result unattributable.

        Isolated exactly like every other lane. A fault here is recorded and
        swallowed rather than costing the registered strategies their cycle,
        and coverage separates fired, declined and starved because a
        mechanism evaluated on a snapshot it could not read has not been
        tested and must not be recorded as having declined.
        """
        if (not self.staged_contracts
                and not self._staged_retired_evaluators):
            return {"_records": []}
        lane: dict = {
            "scope_key": f"{self.scope_key}:staged",
            "proposal_source": "staged_contract",
            "contracts": len(self.staged_contracts),
            "draining_contracts": sorted(
                self._staged_retired_evaluators),
            "refresh": dict(self.last_staged_refresh),
        }
        records: list = []
        per_contract: dict = {}
        errors: dict = {}
        proposals_total = 0
        for contract in self.staged_contracts:
            evaluator = self.staged_evaluators.get(contract.contract_id)
            if evaluator is None:
                errors[contract.contract_id] = self._staged_refresh_errors.get(
                    contract.contract_id,
                    "staged evaluator is unavailable after refresh")
                continue
            try:
                proposals = staged_lane.proposals_for(
                    [contract], snapshot, evaluator.base_cfg)
                proposals.extend(staged_lane.proposals_for(
                    [contract], snapshot, evaluator.base_cfg, baseline=True))
                proposals_total += len(proposals)
                per_contract.update(staged_lane.coverage(proposals))
                records.extend(evaluator.evaluate(
                    snapshot, now=timestamp, cycle_id=cycle_id,
                    proposals=proposals,
                    advance_accounts=advance_accounts))
            except Exception as exc:                       # noqa: BLE001
                errors[contract.contract_id] = f"{type(exc).__name__}: {exc}"
        # A direct caller may evaluate without first calling ``advance``. In
        # that mode drain retired accounts here; the engine's normal path
        # advances once before evaluating and passes ``advance_accounts=False``.
        if advance_accounts:
            for contract_id, evaluator in list(
                    self._staged_retired_evaluators.items()):
                try:
                    records.extend(evaluator.advance(
                        snapshot, now=timestamp))
                    if not evaluator.held_symbols():
                        self._staged_retired_evaluators.pop(contract_id, None)
                        self._mark_staged_drained(contract_id)
                        lane.setdefault("drained_contracts", []).append(
                            contract_id)
                except Exception as exc:                   # noqa: BLE001
                    errors[contract_id] = (
                        f"{type(exc).__name__}: {exc}")
        lane["proposals"] = proposals_total
        lane["per_contract"] = per_contract
        lane["refresh"] = dict(self.last_staged_refresh)
        if errors:
            lane["errors"] = errors
        lane["_records"] = records
        return lane

    def _evaluate_llm_lane(
            self, snapshot: dict, timestamp: float, cycle_id: str | None,
            proposals: list[dict] | None, advance_accounts: bool) -> dict:
        """Record what the analyst actually decided, on the same snapshot.

        This is the lane the planner learns from. It is deliberately not
        comparable to the deterministic lanes and lives in its own scope so
        no verdict can pool the two, but it sees the identical snapshot and
        timestamp, so the two readings of the same cycle stay alignable.

        Isolated like every other lane: a failure here must not cost the
        comparison its cycle.
        """
        if self.llm_evaluator is None:
            return {"_records": []}
        lane: dict = {
            "scope_key": self.llm_evaluator.scope_key,
            "strategy_id": self.active_strategy_id,
            "proposal_source": "recorded_llm",
            "proposals": len(proposals or []),
        }
        try:
            lane["_records"] = list(self.llm_evaluator.evaluate(
                snapshot, now=timestamp, cycle_id=cycle_id,
                proposals=list(proposals or []),
                advance_accounts=advance_accounts))
            lane["coverage"] = dict(self.llm_evaluator.last_coverage)
        except Exception as exc:                           # noqa: BLE001
            lane["error"] = f"{type(exc).__name__}: {exc}"
            lane["_records"] = list(self._isolate_failure(
                self.llm_evaluator, self.active_strategy_id,
                "evaluate-llm-lane", exc, timestamp))
        return lane


def _research_cfg(cfg: dict, strategy_id: str) -> dict:
    """Build an isolated strategy config without changing the live config."""
    if strategy_id == str(cfg["strategy"]["id"]):
        return cfg
    spec = strategy_registry.spec_for(strategy_id)
    model = require_complete_contract(strategy_id)
    out = deepcopy(cfg)
    block = dict(out["strategy"])
    block.update(spec.contract_params)
    block.update({
        "id": spec.id,
        "version": spec.version,
        "signal_timeframe": spec.signal_timeframe,
    })
    # Outcome semantics come only from the validated forward model.
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
            float(block.get("experiment_min_duration_days") or 10) * 86_400),
        rotation_min_observations=int(
            block.get("experiment_min_observations")
            or block.get("paper_min_closed_trades") or 100),
        rotation_batch_size=int(
            block.get("experiment_candidate_batch_size")
            or DEFAULT_ROTATION_BATCH_SIZE),
        rotation_hard_cap=MAX_ROTATION_CANDIDATES,
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

    # Explicit lists select one strategy; the wildcard selects all strategies.
    if "*" not in names:
        return _build_strategy_evaluator(
            cfg, registry, names, scope_key=resolved_scope,
            findings_store=findings_store)

    resolved_scope = (
        f"{resolved_scope}:feed-v{int(block.get('forward_feed_version') or 1)}")

    evaluators = {}
    for strategy_id in sorted(strategy_registry.REGISTRY):
        spec = strategy_registry.spec_for(strategy_id)
        # Keep the active strategy; skip other lanes that cannot reach the
        # real-time closed-trade floor.
        if not spec.realtime_eligible and strategy_id != str(
                cfg["strategy"]["id"]):
            continue
        strategy_cfg = _research_cfg(cfg, strategy_id)
        evaluator = _build_strategy_evaluator(
            strategy_cfg, registry, ["*"], scope_key=resolved_scope,
            findings_store=findings_store)
        if evaluator is not None:
            evaluators[strategy_id] = evaluator
    if not evaluators:
        return None
    active_id = str(cfg["strategy"]["id"])
    # Preserve analyst decisions in a sibling scope for learning history.
    llm_evaluator = _build_strategy_evaluator(
        _research_cfg(cfg, active_id), registry, ["*"],
        scope_key=f"{resolved_scope}:llm", findings_store=findings_store)
    staged_evaluators, staged_contracts = _build_staged_lane(
        cfg, resolved_scope, findings_store)
    return StrategyShadowCoordinator(
        evaluators, active_strategy_id=active_id,
        llm_evaluator=llm_evaluator,
        staged_evaluators=staged_evaluators,
        staged_contracts=staged_contracts,
        staged_store_path=_resolve_staging_path(block.get("staging_store")),
        staged_cfg=_staged_runtime_cfg(cfg))


def staged_variant_id(contract_id: str) -> str:
    """Namespace a contract as a variant id the registry format accepts.

    Hyphens are legal in a contract id and not in a variant id, and the
    ``staged.`` prefix keeps a machine-authored arm visibly distinct from a
    hand-registered one in every report that lists variants.
    """
    return staged_lane.candidate_variant_id(contract_id)


def staged_baseline_variant_id(contract_id: str) -> str:
    """Stable neutral baseline paired with one staged contract."""
    return staged_lane.baseline_variant_id(contract_id)


def _build_staged_lane(cfg: dict, resolved_scope: str,
                       findings_store: FindingsStore):
    """Enrol each machine-authored mechanism on the shared harness.

    One evaluator per contract, deliberately. ``ShadowEvaluator.evaluate``
    applies one common proposal set to every variant it holds, which is right
    for a baseline and its candidate but wrong here: two staged mechanisms
    are different claims, so sharing an evaluator would have each one trading
    on the other's signals and destroy attribution entirely.

    Absent or unreadable staging is not an error. The platform ran without
    this lane before it existed and must keep running if the store is gone.
    """
    block = cfg.get("research") or {}
    path = _resolve_staging_path(block.get("staging_store"))
    try:
        from .staging import StagingStore

        if not path or not Path(path).is_file():
            return {}, []
        contracts = StagingStore(path).active()
    except Exception as exc:                               # noqa: BLE001
        log.warning("staged mechanisms unavailable: %s", exc)
        return {}, []
    if not contracts:
        return {}, []
    # The config keeps a registered strategy.id because configuration
    # validation resolves it against the registry, and "staged" is a research
    # family rather than a registered strategy. Only the outcome parameters
    # are overridden, exactly as _research_cfg does for the other lanes. The
    # staged identity lives on the variant, which is what resolves the
    # harness and what every result is attributed to.
    staged_cfg = _staged_runtime_cfg(cfg)
    evaluators, enrolled = {}, []
    for contract in contracts:
        try:
            evaluators[contract.contract_id] = _build_staged_evaluator(
                contract, staged_cfg, resolved_scope, findings_store)
        except Exception as exc:                           # noqa: BLE001
            # One unbuildable mechanism must not cost the others their lane.
            log.warning("staged mechanism %s could not be enrolled: %s",
                        contract.contract_id, exc)
            continue
        enrolled.append(contract)
    return evaluators, enrolled


def _staged_runtime_cfg(cfg: dict) -> dict:
    """Copy runtime config and pin staged lanes to the shared harness."""
    staged_cfg = deepcopy(cfg)
    staged_cfg["strategy"] = {
        **dict(staged_cfg["strategy"]),
        "min_stop_atr_multiple": STAGED_HARNESS.stop_atr_multiple,
        "fixed_reward_risk": STAGED_HARNESS.reward_risk,
        "forward_horizon_hours": STAGED_HARNESS.horizon_hours,
    }
    return staged_cfg


def _build_staged_evaluator(
        contract: object, staged_cfg: dict, resolved_scope: str,
        findings_store: FindingsStore | None) -> ShadowEvaluator:
    """Build one isolated evaluator for one staged contract."""
    block = staged_cfg.get("research") or {}
    variant = Variant(
        variant_id=staged_variant_id(contract.contract_id),
        strategy_id=STAGED_STRATEGY_ID,
        base_version=STAGED_HARNESS.model_id,
        overrides={},
        hypothesis=contract.mechanism,
        status="candidate")
    baseline = Variant(
        variant_id=staged_baseline_variant_id(contract.contract_id),
        strategy_id=STAGED_STRATEGY_ID,
        base_version=STAGED_HARNESS.model_id,
        overrides={},
        hypothesis=("The neutral fixed-harness policy is the paired baseline "
                    "for this staged mechanism."),
        status="testing")
    return ShadowEvaluator(
        [variant, baseline], staged_cfg,
        budget_ms=float(block.get("shadow_budget_ms") or 0),
        store=findings_store,
        scope_key=f"{resolved_scope}:staged",
        initial_balance_usdt=float(
            block.get("paper_initial_balance_usdt") or 10_000),
        max_failures=int(block.get("paper_max_failures") or 3),
        workers=int(block.get("shadow_workers") or 1))
