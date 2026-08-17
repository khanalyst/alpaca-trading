"""Autonomous, parallel strategy research with isolated simulated accounts.

Seven bounded hypotheses are evaluated in separate worker processes by
default.  A worker may mutate only the audited rule grammar; it cannot write
or execute source code.  Mutations are diagnosed from the chronological fit
partition and judged on untouched held-out or later forward data.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
import json
import math
import os
from pathlib import Path
import random
import sqlite3
from typing import Any, Mapping, Sequence
import uuid

from agent.contracts.rule import (RULE_FAMILIES, RULE_SCHEMA_V1, RULE_SCHEMA_V2,
                                  V2_DEFAULT_EXTENSIONS, hold_deadline,
                                  rule_semantic_distance,
                                  rule_semantic_signature, rule_variant_id,
                                  validate_rule_spec)
from .costs import (BAR, QUOTE, CostModel, ReplayPolicy, SQLiteQuoteIndex,
                    index_quotes, quote_fill, replay_policy_for_mode)
from .edge_lab import (
    DEFAULT_DB_PATH, EdgeLedger, _read_discovery_rows, content_hash,
)
from .edge_ledger_store import provenance_hash
# One randomized-entry null control serves both research lanes; it lives in
# the shared discovery helpers and is re-exported here for its callers.
from .edge_discovery_core import (corpus_slice, null_control_account,
                                  validate_worker_projection)
from .factory_ledger import (
    ACTIVE_HYPOTHESIS_STATES, FACTORY_SCHEMA, FACTORY_STATUSES, FactoryError,
    FactoryLedger, deferred_fdr, experiment_identity, experiment_provenance,
)
from .gates import (chronological_split, heldout_separation,
                    expectancy_rejection_report,
                    matched_cluster_test, matched_pairs, max_drawdown_of,
                    performance_floor, placebo_null_distribution,
                    RETIREMENT_CONFIDENCE, RETIREMENT_MIN_SESSIONS,
                    RETIREMENT_MIN_USEFUL_R,
                    falsification_gate,
                    qualification_report as _qualification_report,
                    sample_counts, seal_final_window,
                    structural_floor, verified_gate_envelope,
                    walk_forward_report)
from .llm_strategy import (DISCOVERY_SCHEMA, LESSON_REF_CHARS, PROPOSAL_SCHEMA,
                           TUNING_SCHEMA, ProposalResult, RuleProposalAdapter,
                           _tuning_reason_check)
from .stats import benjamini_hochberg, stable_seed
from .factory_core import (
    DEFAULT_STRATEGIES, DEFAULT_VARIANTS, MAX_STRATEGIES, MAX_VARIANTS,
    NOTIONAL_CAP_PCT, StrategyHypothesis, _falsification, _hypothesis_id, _option_at, _safe_variant,
    _session, _simulate_trade, _thesis, _visible, diagnose, discovery_hypothesis,
    coordinate_mutation_pool, initial_hypotheses, interaction_mutation_pool,
    mutate_from_diagnosis, mutate_with_reasons, mutation_reason,
    replacement_hypothesis, simulate_account, spec_delta, template_hypothesis,
)


DEFAULT_WORKERS = 2
MAX_WORKERS = 16
# Exact semantic aliases are always removed. Near-duplicate suppression is a
# transparent policy knob for model-authored proposals; deterministic
# parameter sweeps remain a complete search regardless of this value.
NEAR_DUPLICATE_DISTANCE = 0.001
# Rotation makes generation exhaustion recoverable without removing the cap.
# A slot may be reseeded with a fresh family at most ``MAX_ROTATIONS`` times,
# each rotation granting one further ``max_generations`` mutation budget, and
# at most ``ROTATION_BUDGET`` rotations may happen in one cycle.  Hypotheses
# per slot therefore stay bounded by
# ``max_generations * (MAX_ROTATIONS + 1)`` for the life of the ledger.
ROTATION_BUDGET = 1
MAX_ROTATIONS = 2
# How much graded history a tuning prompt carries.  The adapter bounds the
# payload at 8 KiB anyway; this keeps the brief recent and readable rather
# than letting it drift toward that ceiling.
LESSON_BRIEF_LIMIT = 8
LESSON_BRIEF_BYTES = 6_000
# The cross-family digest is an aggregate over many more rows than the brief,
# because its whole value is that one family's eight attempts are not the only
# evidence a slot has.
SHARED_LEARNING_SAMPLE = 400
SHARED_LEARNING_ROWS = 12
# Below this, a "trend" is one or two attempts and reporting it as a pattern
# would be worse than reporting nothing.
SHARED_LEARNING_MIN_ATTEMPTS = 3
# Hypothesis-level lessons are graded by the best variant the hypothesis
# produced; the root is an ordinary null-tested candidate and consumes
# multiplicity alongside its mutations.
HYPOTHESIS_LESSON_KINDS = ("discovery", "reseed", "replacement", "rotation")


def _slot_rotations(factory: FactoryLedger, vehicle: str, slot: int) -> int:
    """Count the bounded family rotations a slot has already spent."""
    return factory.slot_event_count(vehicle, slot, status="retired",
                                    flag="rotation")


def _slot_reseeds(factory: FactoryLedger, vehicle: str, slot: int) -> int:
    """Count the successful reseeds a slot has already been granted.

    A reseed follows a *proved* edge, not a failure, so it is counted apart
    from the failure-recovery rotation budget and never consumes it.
    """
    return factory.slot_event_count(vehicle, slot, status="validated",
                                    flag="reseed")


def _slot_families(factory: FactoryLedger, vehicle: str, slot: int) -> set[str]:
    return factory.slot_families(vehicle, slot)


def _proved_families(edge: EdgeLedger, vehicle: str) -> list[str]:
    """Families already carrying a deployed edge in this vehicle."""
    families: set[str] = set()
    for candidate in edge.status(vehicle=vehicle):
        if candidate.get("status") not in {"validated", "champion"}:
            continue
        try:
            config = json.loads(candidate.get("config_json") or "{}")
        except json.JSONDecodeError:
            continue
        spec = (config.get("strategy") or {}).get("rule_spec")
        if isinstance(spec, Mapping) and spec.get("family"):
            families.add(str(spec["family"]))
    return sorted(families)


def _discovery_context(*, slot: int, reason: str, previous: Mapping[str, Any],
                       tried_families: set[str], proved_families: Sequence[str],
                       diagnostic: Mapping[str, Any] | None = None,
                       seeded_this_cycle: Sequence[Mapping[str, Any]] = (),
                       lessons: Sequence[Mapping[str, Any]] = (),
                       shared: Mapping[str, Any] | None = None) -> dict:
    """Build the small aggregate brief a discovery proposal is given."""
    context: dict[str, Any] = {
        "slot": int(slot),
        "reason": str(reason),
        "previous_family": str(previous.get("family") or ""),
        "tried_families": sorted(tried_families),
        "proved_families": sorted(proved_families),
        "available_families": list(RULE_FAMILIES),
    }
    if diagnostic:
        context["last_diagnosis"] = dict(diagnostic)
    # Slots are seeded one after another inside a single cycle.  Without this
    # every slot of a fresh ledger receives an identical brief, so a model that
    # answers consistently returns the same hypothesis every time and all but
    # the first are discarded as duplicates — paying for N proposals and using
    # one.  Telling each slot what its siblings just took is what makes the
    # parallel width real rather than nominal.
    if seeded_this_cycle:
        context["already_seeded_this_cycle"] = [
            {"slot": int(item["slot"]), "family": str(item["family"])}
            for item in seeded_this_cycle]
    if lessons:
        context["lessons"] = list(lessons)
    # What every slot has learned, not only this one.  A fresh slot has no
    # history of its own and this is the only evidence it can reason from.
    if shared and shared.get("graded_attempts"):
        context["shared_learning"] = dict(shared)
    return context


def _trim_lessons(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Compact graded lessons into the aggregate brief a prompt may carry."""
    brief: list[dict] = []
    for row in rows:
        outcome = row.get("outcome") or {}
        brief.append({
            # The short handle a proposal must cite to say what it learned
            # from.  Truncated because the brief is a prompt, and resolvable
            # because a citation that does not resolve is not a citation.
            "id": str(row.get("lesson_id") or "")[:LESSON_REF_CHARS],
            "family": row.get("family"),
            "tried": row.get("changed") or {},
            "reason": row.get("reason"),
            "proposed_by": row.get("source"),
            "verdict": (outcome.get("classification") or
                        ("passed" if outcome.get("passed") else
                         "underpowered" if outcome.get("underpowered") else
                         "inconclusive")),
            "heldout_delta": outcome.get("heldout_delta"),
            "failed_checks": list(outcome.get("failed_checks") or [])[:4],
        })
        # Trim from the oldest end rather than failing the request: a brief
        # that outgrew its bound should lose history, not become no history.
        while brief and len(json.dumps(brief, default=str).encode(
                "utf-8")) > LESSON_BRIEF_BYTES:
            brief.pop(0)
    return brief


def _failed_variants(factory: FactoryLedger, *, vehicle: str,
                     family: str | None = None) -> frozenset[str]:
    """Parameter sets a graded lesson already proved do not work."""
    try:
        return frozenset(factory.failed_variant_ids(vehicle=vehicle,
                                                    family=family))
    except (sqlite3.Error, ValueError, KeyError):
        return frozenset()


def _variant_keys(spec: Mapping[str, Any]) -> set[str]:
    """Exact and semantic ids used by all discovery lanes."""
    normalized = validate_rule_spec(spec)
    return {rule_variant_id(normalized),
            f"semantic:{rule_semantic_signature(normalized)}"}


def _variant_seen(spec: Mapping[str, Any], seen: set[str]) -> bool:
    return bool(_variant_keys(spec) & set(seen))


def _near_duplicate_distance(config: Mapping[str, Any]) -> float:
    """Resolve the bounded semantic-neighborhood policy for one LLM lane."""
    try:
        value = float(config.get("near_duplicate_distance",
                                NEAR_DUPLICATE_DISTANCE))
    except (TypeError, ValueError, OverflowError):
        return NEAR_DUPLICATE_DISTANCE
    if not math.isfinite(value):
        return NEAR_DUPLICATE_DISTANCE
    return max(0.0, min(1.0, value))


def _semantic_duplicate(spec: Mapping[str, Any],
                        candidates: Sequence[Mapping[str, Any]], *,
                        near_distance: float) -> bool:
    """Whether *spec* aliases an executable rule already in *candidates*.

    Exact semantic aliases are rejected even when the configured neighborhood
    is zero.  The family guard is intentional: a distance of ``1.0`` is the
    cross-family sentinel, and a broad threshold must never collapse distinct
    rule families.
    """
    normalized = validate_rule_spec(spec)
    threshold = max(0.0, min(1.0, float(near_distance)))
    for candidate in candidates:
        try:
            other = validate_rule_spec(candidate)
        except (TypeError, ValueError, KeyError):
            continue
        if normalized.get("family") != other.get("family"):
            continue
        if rule_semantic_distance(normalized, other) <= threshold:
            return True
    return False


def _mark_variant(spec: Mapping[str, Any], seen: set[str]) -> None:
    seen.update(_variant_keys(spec))


def _failed_variant(spec: Mapping[str, Any], failed: set[str]) -> bool:
    """Match exact ids plus the v1/v2 no-op grammar alias."""
    normalized = validate_rule_spec(spec)
    if _variant_keys(normalized) & failed:
        return True
    schema = normalized.get("schema")
    if schema == RULE_SCHEMA_V1:
        alias = dict(normalized, schema=RULE_SCHEMA_V2, **V2_DEFAULT_EXTENSIONS)
    elif (schema == RULE_SCHEMA_V2 and
          all(normalized.get(name) == value
              for name, value in V2_DEFAULT_EXTENSIONS.items())):
        alias = {key: value for key, value in normalized.items()
                 if key not in V2_DEFAULT_EXTENSIONS}
        alias["schema"] = RULE_SCHEMA_V1
    else:
        alias = None
    return alias is not None and rule_variant_id(alias) in failed


def _lesson_brief(factory: FactoryLedger, *, vehicle: str,
                  family: str | None = None,
                  limit: int = LESSON_BRIEF_LIMIT) -> list[dict]:
    """The graded reason history a proposal is allowed to learn from.

    Scoped to one family when tuning it, because the specific attempts on that
    idea are what a parameter change should answer.  The cross-family view is
    a separate, aggregated digest — see :func:`shared_learning`.
    """
    try:
        rows = factory.lessons(vehicle=vehicle, family=family,
                               graded_only=True, limit=int(limit))
    except (sqlite3.Error, ValueError, KeyError):
        # The brief is an enrichment.  A ledger written before lessons existed
        # must degrade to "no history", never to a failed research cycle.
        return []
    return _trim_lessons(rows)


def _refinement_history(factory: FactoryLedger, *, vehicle: str,
                        hypothesis_id: str, limit: int = 512) -> list[dict]:
    """Return the full measured coordinate history used for interactions.

    The model prompt stays deliberately small, but selecting which good
    one-field values to preserve must not forget an early winner merely because
    it fell outside the eight-row prompt brief.
    """
    try:
        rows = factory.lessons(
            vehicle=vehicle, hypothesis_id=hypothesis_id,
            graded_only=True, limit=max(1, int(limit)))
    except (sqlite3.Error, ValueError, KeyError):
        return []
    history = []
    for row in rows:
        outcome = row.get("outcome") or {}
        history.append({
            "id": str(row.get("lesson_id") or "")[:LESSON_REF_CHARS],
            "changed": row.get("changed") or {},
            "heldout_delta": outcome.get("heldout_delta"),
            "classification": outcome.get("classification"),
        })
    return history


def shared_learning(factory: FactoryLedger, *, vehicle: str,
                    limit: int = SHARED_LEARNING_SAMPLE) -> dict:
    """What every strategy has learned, aggregated across all of them.

    A per-family brief can only say what happened to *this* idea.  Some of what
    research learns is not about one idea at all — that widening a stop tends
    to help across families, that a live paper trial has never yet agreed with
    a replay, that one direction of change keeps failing everywhere.  Sharing
    that is what lets a slot benefit from the other ten.

    It is deliberately an aggregate of *outcomes*, never of market data: each
    entry is a parameter name, how often changing it in a given direction
    passed, and over how many graded attempts.  Nothing here can carry a
    held-out or sealed observation into a proposal, because nothing here is
    derived from one.
    """
    try:
        rows = factory.lessons(vehicle=vehicle, graded_only=True,
                               limit=int(limit))
    except (sqlite3.Error, ValueError, KeyError):
        return {"graded_attempts": 0, "parameters": [], "families": [],
                "live_trials": {"run": 0, "failed": 0}}
    parameters: dict[tuple[str, str], dict[str, int]] = {}
    families: dict[str, dict[str, int]] = {}
    trials = {"run": 0, "failed": 0}
    for row in rows:
        outcome = row.get("outcome") or {}
        passed = bool(outcome.get("passed"))
        underpowered = bool(outcome.get("underpowered"))
        if row.get("kind") == "trial":
            trials["run"] += 1
            trials["failed"] += 0 if passed else 1
        family = str(row.get("family") or "unknown")
        bucket = families.setdefault(family, {"attempts": 0, "passed": 0})
        bucket["attempts"] += 1
        bucket["passed"] += 1 if passed else 0
        if underpowered:
            # An underpowered attempt says nothing about the parameter; only
            # about the sample.  Counting it would manufacture a trend.
            continue
        for name, change in (row.get("changed") or {}).items():
            direction = _direction(change)
            if direction is None:
                continue
            entry = parameters.setdefault((str(name), direction),
                                          {"attempts": 0, "passed": 0})
            entry["attempts"] += 1
            entry["passed"] += 1 if passed else 0
    ranked = [
        {"parameter": name, "direction": direction,
         "attempts": stats["attempts"], "passed": stats["passed"]}
        for (name, direction), stats in parameters.items()
        if stats["attempts"] >= SHARED_LEARNING_MIN_ATTEMPTS
    ]
    ranked.sort(key=lambda item: (-item["attempts"], item["parameter"]))
    return {
        "graded_attempts": len(rows),
        "parameters": ranked[:SHARED_LEARNING_ROWS],
        "families": sorted(
            ({"family": name, **stats} for name, stats in families.items()),
            key=lambda item: (-item["passed"], -item["attempts"], item["family"])
        )[:SHARED_LEARNING_ROWS],
        "live_trials": trials,
    }


def _direction(change: Any) -> str | None:
    """Whether a recorded delta raised, lowered, or switched a value."""
    if not isinstance(change, Mapping) or {"from", "to"} - set(change):
        return None
    before, after = change.get("from"), change.get("to")
    if isinstance(before, bool) or isinstance(after, bool):
        return None
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        if after > before:
            return "raised"
        if after < before:
            return "lowered"
        return None
    return "switched" if before != after else None


def _seed_slot(previous: Mapping[str, Any], *, generation: int,
               not_before: str | None, existing_variant_ids: set[str],
               tried_families: set[str], context: Mapping[str, Any],
               llm_enabled: bool, config: Mapping[str, Any],
               adapter: RuleProposalAdapter | None = None,
               existing_specs: Sequence[Mapping[str, Any]] = ()
               ) -> tuple[StrategyHypothesis | None, ProposalResult | None, str]:
    """Seed a free slot: LLM discovery first, deterministic ladder second.

    The proposal is only ever a *seed*.  Whatever produced it, the resulting
    hypothesis is registered as ``queued`` and has to earn ``backtest_passed``
    and then a strictly later forward shadow pass through exactly the same
    gates as a deterministic one, so an LLM cannot shorten the evidence path.
    """
    vehicle = str(previous["vehicle"])
    slot = int(previous["slot"])
    near_distance = _near_duplicate_distance(config)
    # ``previous`` is deliberately included even when it is a genesis
    # template that has not been registered.  Otherwise a model can return
    # the template with a different content hash and masquerade as discovery.
    comparison_specs = list(existing_specs)
    previous_spec = previous.get("rule_spec")
    if isinstance(previous_spec, Mapping):
        comparison_specs.append(previous_spec)
    proposal: ProposalResult | None = None
    if llm_enabled:
        selected = adapter or RuleProposalAdapter(
            provider=str(config.get("provider") or "openai"),
            model=str(config.get("model") or ""),
            max_attempts=int(config.get("max_attempts", 1)),
            timeout_seconds=float(config.get("timeout_seconds", 30)),
            max_response_bytes=int(config.get("max_response_bytes", 16_384)),
            max_total_calls=int(config.get("max_total_calls", 64)),
        )
        discover = getattr(selected, "discover", None)
        try:
            # An injected seam without ``discover``, or one that raises, means
            # no proposal — not a failed research cycle. Seeding must always
            # produce a hypothesis, so any provider trouble degrades to the
            # deterministic ladder rather than stopping the night's work.
            proposal = (discover(vehicle=vehicle, slot=slot,
                                 context=dict(context))
                        if callable(discover) else None)
            if proposal is not None and not isinstance(proposal, ProposalResult):
                proposal = ProposalResult(False, schema=DISCOVERY_SCHEMA,
                                          error="provider returned malformed discovery result")
        except Exception as exc:  # noqa: BLE001 - degraded, never fatal
            proposal = ProposalResult(False, schema=DISCOVERY_SCHEMA,
                                      error=f"{type(exc).__name__}: provider discovery unavailable")
        if (proposal is not None and proposal.success and
                proposal.rule_spec is not None and proposal.variant_id):
            try:
                spec = validate_rule_spec(proposal.rule_spec)
                if proposal.variant_id != rule_variant_id(spec):
                    raise ValueError("discovery variant id does not match normalized spec")
                duplicate = (_variant_seen(spec, existing_variant_ids) or
                             _semantic_duplicate(spec, comparison_specs,
                                                 near_distance=near_distance))
            except Exception as exc:  # malformed success degrades to ladder
                duplicate = True
                proposal = ProposalResult(False, schema=DISCOVERY_SCHEMA,
                                          error=f"{type(exc).__name__}: malformed discovery",
                                          evidence=dict(proposal.evidence))
            if not duplicate:
                return StrategyHypothesis(
                    _hypothesis_id(vehicle, slot, generation, spec), slot, generation,
                    vehicle, str(spec["family"]),
                    # The model's own one-sentence rationale is recorded as the
                    # thesis when it supplied one; it is displayed text, never an
                    # instruction, and the falsification stays machine-generated.
                    proposal.thesis or _thesis(spec), _falsification(spec), spec,
                    str(previous["hypothesis_id"]), not_before), proposal, "llm_discovery"
    # The deterministic helper predates semantic aliases and only knows exact
    # variant ids. Feed each semantically duplicate result back as an exact
    # exclusion so the bounded ladder advances rather than re-registering it.
    # No fuzzy distance is applied here: the deterministic ladder is the
    # complete audited search space and must not lose neighboring points.
    deterministic_seen = set(existing_variant_ids)
    seeded = None
    for _ in range(MAX_VARIANTS * 4):
        candidate = discovery_hypothesis(
            previous, generation=generation, not_before=not_before,
            existing_variant_ids=deterministic_seen,
            tried_families=tried_families)
        if candidate is None:
            break
        if not _variant_seen(candidate.rule_spec, existing_variant_ids):
            seeded = candidate
            break
        deterministic_seen.add(rule_variant_id(candidate.rule_spec))
    if seeded is None:
        return None, proposal, "exhausted"
    return seeded, proposal, "deterministic_discovery"


@dataclass(frozen=True)
class TunedVariant:
    """One variant the orchestrator decided to evaluate, and why.

    ``builds_on`` is the short citation of the graded lesson the proposal
    reasoned from, present only when a model authored it against a non-empty
    brief.  It is resolved to a real lesson id before storage, so an
    unresolvable citation never becomes a link in the chain.
    """

    rule_spec: dict[str, Any]
    reason: str
    source: str
    builds_on: str | None = None


def _adapter(config: Mapping[str, Any],
             adapter: RuleProposalAdapter | None) -> RuleProposalAdapter:
    return adapter or RuleProposalAdapter(
        provider=str(config.get("provider") or "openai"),
        model=str(config.get("model") or ""),
        max_attempts=int(config.get("max_attempts", 1)),
        timeout_seconds=float(config.get("timeout_seconds", 30)),
        max_response_bytes=int(config.get("max_response_bytes", 16_384)),
        max_total_calls=int(config.get("max_total_calls", 64)),
    )


def _tuned_variants(hypothesis: Mapping[str, Any], diagnostic: Mapping[str, Any],
                    *, count: int, vehicle: str, llm_enabled: bool,
                    config: Mapping[str, Any],
                    adapter: RuleProposalAdapter | None,
                    lessons: Sequence[Mapping[str, Any]] = (),
                    already_failed: frozenset[str] = frozenset(),
                    shared: Mapping[str, Any] | None = None,
                    existing_specs: Sequence[Mapping[str, Any]] = (),
                    refinement_lessons: Sequence[Mapping[str, Any]] = (),
                    refinement_state: dict[str, Any] | None = None,
                    ) -> tuple[list[TunedVariant], ProposalResult | None]:
    """Choose this hypothesis's variants, each with the reason it was chosen.

    The root is always variant zero.  It is tested against an independent
    randomized-entry null and consumes the same cycle-wide multiplicity as
    every mutation; it is not a free self-control that can never pass.

    With the LLM lane off this walks the deterministic coordinate, interaction,
    and confirmation phases. With it on, the model may propose the remaining
    variants from the diagnosis *and the graded outcomes of earlier reasons*,
    and anything it does not supply — or supplies as a duplicate, as a repeat
    of a recorded failure, or without citing what it learned from — is topped
    up from the same deterministic table.  A tuned variant is not trusted more
    than a mutated one: both are content-addressed and both face every gate.
    """
    root = validate_rule_spec(hypothesis["rule_spec"])
    failed = set(already_failed)
    near_distance = _near_duplicate_distance(config)
    coordinate = coordinate_mutation_pool(root, diagnostic)
    remaining_coordinate = [item for item in coordinate
                            if not _failed_variant(item[0], failed)]
    interactions = interaction_mutation_pool(
        root, refinement_lessons if refinement_lessons else lessons)
    remaining_interactions = [item for item in interactions
                               if not _failed_variant(item[0], failed)]
    if remaining_coordinate:
        phase = "coordinate"
        pool = remaining_coordinate
    elif remaining_interactions:
        phase = "interaction"
        pool = remaining_interactions
    else:
        # A final unchanged replay is a confirmation stage, not another search
        # point.  It lets the retirement rule demand a fresh gate after every
        # bounded coordinate and interaction candidate has been consumed.
        phase = "confirmatory"
        pool = [(root, "Unchanged confirmatory replay after the bounded "
                       "coordinate and interaction neighborhoods were exhausted.")]
    if refinement_state is not None:
        refinement_state.update({
            "phase": phase,
            "coordinate_total": len(coordinate),
            "coordinate_remaining_before": len(remaining_coordinate),
            "interaction_total": len(interactions),
            "interaction_remaining_before": len(remaining_interactions),
            "pool_variant_ids": [rule_variant_id(item[0]) for item in pool],
        })
    if not llm_enabled or phase == "confirmatory":
        return ([TunedVariant(spec, reason, "deterministic", None)
                 for spec, reason in pool[:int(count)]], None)
    selected = _adapter(config, adapter)
    tune = getattr(selected, "tune", None)
    proposal: ProposalResult | None = None
    try:
        # An injected seam without ``tune``, or one that raises, means no
        # proposal — never a failed cycle.
        # The diagnosis says how this hypothesis failed; the shared digest says
        # what every other slot has found out about the same parameters.
        diagnosis = {**dict(diagnostic), "refinement_phase": phase,
                     "coordinate_candidates_remaining": len(remaining_coordinate),
                     "interaction_candidates_remaining": len(remaining_interactions)}
        if shared and shared.get("graded_attempts"):
            diagnosis["shared_learning"] = dict(shared)
        proposal = (tune(vehicle=vehicle, slot=int(hypothesis["slot"]),
                         rule_spec=root, diagnosis=diagnosis,
                         count=max(1, int(count) - 1), lessons=list(lessons))
                    if callable(tune) else None)
        if proposal is not None and not isinstance(proposal, ProposalResult):
            proposal = ProposalResult(False, schema=TUNING_SCHEMA,
                                      error="provider returned malformed tuning result")
    except Exception as exc:  # noqa: BLE001 - degraded, never fatal
        proposal = ProposalResult(False, schema=TUNING_SCHEMA,
                                  error=f"{type(exc).__name__}: provider tuning unavailable")
    chosen: list[TunedVariant] = []
    seen: set[str] = set()
    deterministic_seed = pool[0] if pool else None
    if deterministic_seed is not None:
        chosen.append(TunedVariant(
            deterministic_seed[0], deterministic_seed[1], "deterministic", None))
        _mark_variant(deterministic_seed[0], seen)
    if (proposal is not None and proposal.success and
            isinstance(proposal.variants, (list, tuple))):
        for entry in proposal.variants:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("rule_spec"), Mapping):
                continue
            if len(chosen) >= int(count):
                break
            try:
                normalized_entry = validate_rule_spec(entry["rule_spec"])
                canonical_id = rule_variant_id(normalized_entry)
                if entry.get("variant_id") and str(entry["variant_id"]) != canonical_id:
                    continue
                variant_id = canonical_id
                _tuning_reason_check(
                    str(entry.get("reason") or ""), root, normalized_entry,
                    {**dict(diagnostic), "refinement_phase": phase}, lessons)
            except (TypeError, ValueError, KeyError):
                continue
            # Re-proposing a parameter set a graded lesson already recorded as
            # an adequate failure is not a new experiment; the ledger has that
            # answer.  Dropping it here is what makes "learn from the lessons"
            # a property of the system rather than a request in a prompt.
            if (variant_id in seen or _failed_variant(entry["rule_spec"], failed) or
                    _variant_seen(entry["rule_spec"], seen)):
                continue
            # Only provider-authored candidates are fuzzy-filtered. Include
            # persisted hypotheses/candidates, the current root, and earlier
            # accepted proposals in this cycle; deterministic pool entries
            # below intentionally retain every neighboring point.
            if _semantic_duplicate(
                    normalized_entry,
                    [*existing_specs, root,
                     *(item.rule_spec for item in chosen)],
                    near_distance=near_distance):
                continue
            _mark_variant(normalized_entry, seen)
            chosen.append(TunedVariant(normalized_entry, str(entry.get("reason") or "provider tuning"),
                                       "llm", entry.get("builds_on")))
    for spec, reason in pool:
        if len(chosen) >= int(count):
            break
        if _failed_variant(spec, failed) or _variant_seen(spec, seen):
            continue
        # The deterministic ladder is an explicit bounded search space; keep
        # its neighboring points as coverage. Near-duplicate suppression is
        # applied to model aliases above, where it prevents paying for
        # semantically redundant provider output.
        _mark_variant(spec, seen)
        chosen.append(TunedVariant(spec, reason, "deterministic", None))
    return chosen, proposal


def _llm_replacement(previous: Mapping[str, Any], diagnostic: Mapping[str, Any], *,
                     config: Mapping[str, Any], max_generations: int,
                     not_before: str | None,
                     existing_variant_ids: set[str],
                     adapter: RuleProposalAdapter | None = None,
                     lessons: Sequence[Mapping[str, Any]] = (),
                     failed_variant_ids: frozenset[str] = frozenset(),
                     tried_families: Sequence[str] = (),
                     existing_specs: Sequence[Mapping[str, Any]] = ()
                     ) -> tuple[StrategyHypothesis | None, ProposalResult | None, str | None]:
    generation = int(previous["generation"]) + 1
    if generation >= int(max_generations):
        return None, None, "generation_limit"
    selected = adapter or RuleProposalAdapter(
        provider=str(config.get("provider") or "openai"),
        model=str(config.get("model") or ""),
        max_attempts=int(config.get("max_attempts", 1)),
        timeout_seconds=float(config.get("timeout_seconds", 30)),
        max_response_bytes=int(config.get("max_response_bytes", 16_384)),
        max_total_calls=int(config.get("max_total_calls", 64)),
    )
    enriched = dict(diagnostic)
    enriched["lessons"] = list(lessons)
    enriched["already_failed_variant_ids"] = sorted(str(item) for item in failed_variant_ids)
    enriched["tried_families"] = sorted(str(item) for item in tried_families)
    try:
        proposal = selected.propose(
            vehicle=str(previous["vehicle"]), generation=generation,
            prior_validated_rule_spec=previous["rule_spec"], diagnosis=enriched)
    except Exception as exc:  # provider failures are a safe degraded lane
        proposal = ProposalResult(
            False, schema=PROPOSAL_SCHEMA,
            error=f"{type(exc).__name__}: provider replacement unavailable")
    if not isinstance(proposal, ProposalResult):
        proposal = ProposalResult(False, schema=PROPOSAL_SCHEMA,
                                  error="provider returned malformed replacement result")
    if not proposal.success or proposal.rule_spec is None or not proposal.variant_id:
        return None, proposal, "llm_proposal_failed"
    try:
        proposed_spec = validate_rule_spec(proposal.rule_spec)
        if proposal.variant_id != rule_variant_id(proposed_spec):
            raise ValueError("replacement variant id does not match normalized spec")
    except Exception as exc:
        return None, ProposalResult(False, schema=PROPOSAL_SCHEMA,
                                    error=f"{type(exc).__name__}: malformed replacement"), \
            "llm_proposal_failed"
    near_distance = _near_duplicate_distance(config)
    comparison_specs = list(existing_specs)
    previous_spec = previous.get("rule_spec")
    if isinstance(previous_spec, Mapping):
        comparison_specs.append(previous_spec)
    if (_failed_variant(proposed_spec, set(failed_variant_ids)) or
            _variant_seen(proposed_spec, existing_variant_ids) or
            _semantic_duplicate(proposed_spec, comparison_specs,
                                near_distance=near_distance)):
        return None, proposal, "duplicate_llm_variant"
    spec = proposed_spec
    vehicle = str(previous["vehicle"])
    slot = int(previous["slot"])
    hypothesis = StrategyHypothesis(
        _hypothesis_id(vehicle, slot, generation, spec), slot, generation,
        vehicle, str(spec["family"]), _thesis(spec), _falsification(spec),
        spec, str(previous["hypothesis_id"]), not_before,
    )
    return hypothesis, proposal, None


def _llm_lineage_evidence(factory: FactoryLedger,
                          hypothesis: Mapping[str, Any]) -> dict | None:
    parent = hypothesis.get("parent_hypothesis_id")
    if not parent:
        return None
    try:
        parent_events = factory.events(str(parent))
    except KeyError:
        # Lineage evidence is a nice-to-have annotation. An unresolvable
        # ancestor must not abort a research cycle that is otherwise valid.
        return None
    for event in reversed(parent_events):
        payload = event.get("payload") or {}
        if (payload.get("replacement_hypothesis_id") == hypothesis.get("hypothesis_id") and
                isinstance(payload.get("llm_evidence"), Mapping)):
            return {"schema": payload.get("proposal_schema"),
                    "evidence": dict(payload["llm_evidence"]),
                    "replacement_hypothesis_id": hypothesis.get("hypothesis_id")}
    return None


def _seed_reason(source: str, seed: Any, proposal: ProposalResult | None) -> str:
    """The recorded rationale for putting this hypothesis in this slot."""
    if source == "llm_discovery" and proposal is not None and proposal.thesis:
        return str(proposal.thesis)
    if source == "template":
        return (f"Genesis template for slot {int(seed.slot)}: the "
                f"{str(seed.family).replace('_', ' ')} family at its own defaults.")
    return (f"Deterministic discovery: {str(seed.family).replace('_', ' ')} is "
            "the next structure this slot has not tried.")


def _record_seed_lesson(factory: FactoryLedger, seed: Any, *, vehicle: str,
                        kind: str, source: str, proposal: ProposalResult | None,
                        diagnostic: Mapping[str, Any] | None = None) -> None:
    """Record why a slot was given this hypothesis, before it is evaluated."""
    try:
        factory.record_lesson(
            seed.hypothesis_id, vehicle=vehicle, family=seed.family,
            variant_id=rule_variant_id(seed.rule_spec), kind=kind,
            source="llm" if source.startswith("llm") else "deterministic",
            reason=_seed_reason(source, seed, proposal),
            changed={"family": seed.family,
                     "rule_schema": seed.rule_spec["schema"]},
            diagnosis=dict(diagnostic or {}),
            evidence=dict(proposal.evidence) if proposal is not None else {})
    except (FactoryError, KeyError, sqlite3.Error):
        # A lesson is an annotation on work that already happened.  Failing to
        # write one must never cost the research cycle its actual result.
        pass


def _ensure_slots(factory: FactoryLedger, edge: EdgeLedger, *, vehicle: str,
                  strategies: int, existing_variant_ids: set[str],
                  llm_enabled: bool, llm_config: Mapping[str, Any],
                  adapter: RuleProposalAdapter | None,
                  existing_specs: Sequence[Mapping[str, Any]] | None = None
                  ) -> tuple[list[dict], list[dict]]:
    """Give every configured slot an active hypothesis before scheduling.

    Returns ``(seeded, revived)``.  A *seeded* slot never held a hypothesis —
    a fresh ledger, or a slot added by raising ``strategies``.  A *revived*
    slot held one and lost it, which is the interesting case: a ledger written
    before slots were reseeded on success, or a reseed that could not be built.
    Both are losses of research capacity and both are recoverable without
    touching a single deployed edge, but only the second says something went
    wrong, so they are reported apart.
    """
    latest = factory.slot_latest(vehicle)
    active_slots = {int(item["slot"]) for item in factory.active(vehicle)}
    if existing_specs is None:
        known_specs: list[Mapping[str, Any]] = []
        for item in factory.hypotheses(vehicle=vehicle):
            spec = item.get("rule_spec")
            if isinstance(spec, Mapping):
                known_specs.append(spec)
        for candidate in edge.status(vehicle=vehicle):
            if candidate.get("strategy_id") != "rule":
                continue
            try:
                config = json.loads(candidate.get("config_json") or "{}")
                spec = (config.get("strategy") or {}).get("rule_spec")
                if isinstance(spec, Mapping):
                    validate_rule_spec(spec)
                    known_specs.append(spec)
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
    else:
        # The orchestrator passes a mutable list so accepted proposals from
        # earlier slots in this cycle are visible to later discovery calls.
        # Copy immutable test seams without making them a required contract.
        known_specs = (existing_specs if isinstance(existing_specs, list)
                       else list(existing_specs))
    seeded_slots: list[dict] = []
    revived: list[dict] = []
    # Slots are seeded sequentially, so each proposal can be told what the
    # earlier ones in this same cycle already took.  Without it every slot of a
    # fresh ledger gets an identical brief.
    cycle_seeds: list[dict] = [
        {"slot": int(item["slot"]), "family": str(item["family"])}
        for item in factory.active(vehicle)]
    lessons = _lesson_brief(factory, vehicle=vehicle)
    shared = shared_learning(factory, vehicle=vehicle)
    for slot in range(int(strategies)):
        if slot in active_slots:
            continue
        previous = latest.get(slot)
        if previous is None:
            # Genesis. The template is the fallback, not the only option: an
            # empty slot is exactly where a discovery proposal is most useful,
            # and seeding every deployment from the same templates would
            # make "the LLM discovers edges" false on the very first cycle.
            seed = template_hypothesis(slot, vehicle=vehicle)
            source = "template"
            genesis_proposal: ProposalResult | None = None
            if llm_enabled:
                proposed, genesis_proposal, proposed_source = _seed_slot(
                    {"vehicle": vehicle, "slot": slot,
                     "family": seed.family, "rule_spec": seed.rule_spec,
                     "hypothesis_id": seed.hypothesis_id},
                    generation=0, not_before=None,
                    existing_variant_ids=existing_variant_ids,
                    existing_specs=known_specs,
                    tried_families=set(),
                    context=_discovery_context(
                        slot=slot, reason="fresh_slot", previous={
                            "family": seed.family},
                        tried_families={
                            str(item["family"]) for item in latest.values()},
                        proved_families=_proved_families(edge, vehicle),
                        seeded_this_cycle=cycle_seeds, lessons=lessons,
                        shared=shared),
                    llm_enabled=True, config=llm_config, adapter=adapter)
                if (proposed is not None and
                        proposed_source == "llm_discovery"):
                    # The template only supplied the brief; it was never
                    # registered, so it is not an ancestor.  Leaving it as the
                    # parent would dangle a foreign key at a hypothesis that
                    # does not exist and break every lineage read afterwards.
                    seed = replace(proposed, parent_hypothesis_id=None)
                    source = proposed_source
            if _variant_seen(seed.rule_spec, existing_variant_ids):
                continue
        else:
            tried = factory.slot_families(vehicle, slot)
            seed, genesis_proposal, source = _seed_slot(
                previous, generation=factory.next_generation(vehicle, slot),
                not_before=previous.get("not_before"),
                existing_variant_ids=existing_variant_ids,
                existing_specs=known_specs,
                tried_families=tried,
                context=_discovery_context(
                    slot=slot, reason="slot_had_no_active_hypothesis",
                    previous=previous, tried_families=tried,
                    proved_families=_proved_families(edge, vehicle),
                    seeded_this_cycle=cycle_seeds, lessons=lessons,
                    shared=shared),
                llm_enabled=llm_enabled, config=llm_config, adapter=adapter)
            if seed is None:
                continue
        factory.register(seed)
        _mark_variant(seed.rule_spec, existing_variant_ids)
        known_specs.append(seed.rule_spec)
        cycle_seeds.append({"slot": int(seed.slot), "family": str(seed.family)})
        _record_seed_lesson(factory, seed, vehicle=vehicle, kind="discovery",
                            source=source, proposal=genesis_proposal)
        if previous is None:
            # A genesis slot has no ancestor to carry its provenance, so the
            # seeding decision is recorded on the hypothesis itself. Without
            # this an LLM-discovered first hypothesis is indistinguishable
            # from a template in every later lineage read.
            payload: dict[str, Any] = {"seeded": True, "source": source}
            if genesis_proposal is not None:
                payload["proposal_schema"] = genesis_proposal.schema
                payload["llm_evidence"] = genesis_proposal.evidence
                if not genesis_proposal.success:
                    payload["llm_error"] = genesis_proposal.error
            factory.event(seed.hypothesis_id, "queued",
                          f"slot seeded via {source}", payload)
            seeded_slots.append({**asdict(seed), "source": source})
            continue
        revive_payload: dict[str, Any] = {
            "reseed": True, "source": source,
            "successor_hypothesis_id": seed.hypothesis_id,
            "successor_variant_id": rule_variant_id(seed.rule_spec),
        }
        if genesis_proposal is not None:
            revive_payload.update({
                "proposal_schema": genesis_proposal.schema,
                "llm_evidence": genesis_proposal.evidence,
            })
            if not genesis_proposal.success:
                revive_payload["llm_error"] = genesis_proposal.error
        factory.event(
            previous["hypothesis_id"], str(previous.get("status") or "validated"),
            "idle slot revived with a new hypothesis", revive_payload)
        revived.append({**asdict(seed), "source": source})
    return seeded_slots, revived


def _task_corpus(payload: Mapping[str, Any]) -> tuple[list, list, list]:
    """Resolve one task's books, re-reading the corpus where it has a path.

    A recorded corpus is re-read by the worker that needs it instead of being
    sliced into every task dict and copied into every process.  The descriptor
    carries the orchestrator's own three predicates, so the books are the same
    objects in the same order and every hash computed from them is unchanged.
    An in-memory corpus has nothing to re-read and still travels with the task.
    """
    corpus = payload.get("corpus")
    if corpus is None:
        return (list(payload["bars"]), list(payload["snapshots"]),
                list(payload["quotes"]))
    options = {
        "after": corpus["after"], "until": corpus["until"],
        "exclude": corpus["exclude"],
    }
    if corpus.get("projection_digest") is not None:
        options["expected_digest"] = corpus["projection_digest"]
    if corpus.get("quote_descriptor") is None:
        return corpus_slice(corpus["source"], **options)
    return corpus_slice(corpus["source"],
                        quote_descriptor=corpus["quote_descriptor"],
                        **options)


def _close_task_quotes(quotes: Any) -> None:
    """Close a worker-owned SQLite resolver without affecting list callers."""
    close = getattr(quotes, "close", None)
    if callable(close) and isinstance(quotes, SQLiteQuoteIndex):
        close()


def _diagnose_worker(payload: Mapping[str, Any]) -> dict:
    """Diagnose a hypothesis's root on its fit partition only.

    Split out of :func:`_worker` so the orchestrator holds a diagnosis *before*
    variants are chosen.  That ordering is what lets the parameter proposal be
    made centrally — every provider call stays in the parent process, so no
    adapter ever has to cross a process boundary — while the expensive replay
    stays parallel.  The fit cut is computed exactly as it was inside the
    worker, so the diagnosis is the same one the previous single pass produced.
    """
    bars, snapshots, quotes = _task_corpus(payload)
    try:
        hypothesis = dict(payload["hypothesis"])
        vehicle = str(payload["vehicle"])
        starting_cash = float(payload["starting_cash"])
        sessions = sorted({_session(bar) for bar in bars})
        cut = (max(1, min(len(sessions) - 1, int(len(sessions) * .7)))
               if len(sessions) > 1 else len(sessions))
        fit_sessions = set(sessions[:cut])
        fit_bars = [bar for bar in bars if _session(bar) in fit_sessions]
        root_account = simulate_account(
            fit_bars, snapshots, hypothesis["rule_spec"], vehicle=vehicle,
            account_id=f"diagnostic:{hypothesis['hypothesis_id']}",
            starting_cash=starting_cash, costs=payload["costs"], quotes=quotes,
            policy=payload.get("policy"),
        )
        return {"hypothesis_id": str(hypothesis["hypothesis_id"]),
                "diagnostic": diagnose(root_account["rows"],
                                       starting_cash=starting_cash),
                "worker_pid": os.getpid()}
    finally:
        _close_task_quotes(quotes)


def _worker(payload: Mapping[str, Any]) -> dict:
    bars, snapshots, quotes = _task_corpus(payload)
    try:
        hypothesis = dict(payload["hypothesis"])
        vehicle = str(payload["vehicle"])
        mode = str(payload["mode"])
        starting_cash = float(payload["starting_cash"])
        costs = payload["costs"]
        policy = payload.get("policy")
        diagnostic = dict(payload["diagnostic"])
        # The orchestrator decided which specs to evaluate — deterministically
        # mutated, model-tuned, or carried forward for forward validation — so
        # the worker only replays them.
        specs = [validate_rule_spec(item) for item in payload["specs"]]
        control_account = simulate_account(
            bars, snapshots, hypothesis["rule_spec"], vehicle=vehicle,
            account_id=f"control:{hypothesis['hypothesis_id']}:{uuid.uuid4().hex[:8]}",
            starting_cash=starting_cash, costs=costs, quotes=quotes,
            policy=policy,
        )
        variants = []
        null_rows: dict[str, list] = {}
        for spec in specs:
            variant_id = rule_variant_id(spec)
            account_id = f"sim:{hypothesis['hypothesis_id']}:{variant_id}:{vehicle}:{uuid.uuid4().hex[:8]}"
            account = simulate_account(
                bars, snapshots, spec, vehicle=vehicle, account_id=account_id,
                starting_cash=starting_cash, costs=costs, quotes=quotes,
                policy=policy,
            )
            null_rows[variant_id] = null_control_account(
                bars, snapshots, spec, vehicle=vehicle, reference_rows=account["rows"],
                account_id=f"null:{hypothesis['hypothesis_id']}:{variant_id}:{vehicle}",
                starting_cash=starting_cash, costs=costs, quotes=quotes,
                policy=policy)["rows"]
            variants.append({
                "variant_id": variant_id, "rule_spec": spec, "vehicle": vehicle,
                "account": account, "diagnostic": diagnose(account["rows"], starting_cash=starting_cash),
                "worker_pid": os.getpid(),
            })
        sessions = sorted({_session(bar) for bar in bars})
        return {"hypothesis": hypothesis, "mode": mode, "diagnostic": diagnostic,
                "refinement": dict(payload.get("refinement") or {}),
                "policy": policy,
                "evaluation_start": sessions[0] if sessions else None,
                "evaluation_end": sessions[-1] if sessions else None,
                "variants": sorted(variants, key=lambda item: item["variant_id"]),
                "control_rows": control_account["rows"], "null_rows": null_rows,
                "expected_variants": len(specs), "worker_pid": os.getpid()}
    finally:
        _close_task_quotes(quotes)


def _gate(rows: Sequence[Mapping], baseline: Sequence[Mapping], *,
          vehicle: str, mode: str,
          min_trades: int, min_sessions: int, alpha: float,
          null_rows: Sequence[Mapping] = (),
          is_root: bool = False,
          qualification: Mapping | None = None,
          folds: int = 3) -> dict:
    # Roots are hypotheses, not mutations. Compare their exact rule to an
    # independent randomized-entry null; descendants retain the root-relative
    # baseline so a parameter effect remains attributable to that family.
    comparison_baseline = list(null_rows) if is_root and null_rows else list(baseline)
    ordered = sorted(rows, key=lambda row: (str(row.get("session_date", "")),
                                             str(row.get("entry_timestamp", ""))))
    base_ordered = sorted(comparison_baseline, key=lambda row: (str(row.get("session_date", "")),
                                                                  str(row.get("entry_timestamp", ""))))
    if mode == "shadow":
        fit, heldout, base_fit, base_heldout = [], ordered, [], base_ordered
    else:
        fit, heldout = chronological_split(ordered, fit_fraction=.7)
        fit_sessions = {str(row.get("session_date") or "") for row in fit}
        held_sessions = {str(row.get("session_date") or "") for row in heldout}
        base_fit = [row for row in base_ordered
                    if str(row.get("session_date") or "") in fit_sessions]
        base_heldout = [row for row in base_ordered
                       if str(row.get("session_date") or "") in held_sessions]
    fit_floor = structural_floor(
        fit, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions,
        required=mode != "shadow")
    held_floor = structural_floor(
        heldout, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    overall_floor = structural_floor(
        ordered, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    fit_test = (matched_cluster_test(fit, base_fit, vehicle=vehicle) if mode != "shadow" else
                {"available": True, "actual_control": True, "matched": 0,
                 "mean_delta": None, "p_value": 1.0, "mode": "prior_backtest"})
    test = matched_cluster_test(heldout, base_heldout, vehicle=vehicle)
    placebo = placebo_null_distribution(heldout, base_heldout, vehicle=vehicle)
    falsification = {
        **falsification_gate(placebo["observed"], placebo["placebo"], alpha=alpha),
        "method": placebo["method"], "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
        "draws": int(placebo["draws"]), "seed": int(placebo["seed"]),
        "clusters": int(placebo["cluster_count"]),
    }

    separation = (heldout_separation(fit, heldout) if mode != "shadow" else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": bool(heldout), "mode": "new_data"})
    fit_net = sum(float(row.get("net_pnl", 0.0)) for row in fit)
    absolute = performance_floor(heldout, vehicle=vehicle)
    held_net = absolute["net_pnl"]
    held_sessions = {str(row.get("session_date") or "") for row in heldout}
    null_heldout = [row for row in null_rows
                    if str(row.get("session_date") or "") in held_sessions]
    null_test = (test if is_root else
                 matched_cluster_test(heldout, null_heldout, vehicle=vehicle))
    null_control = {**null_test, "kind": "randomized_entry_null",
                    "available": bool(null_test["available"]),
                    "p_value": float(null_test["p_value"])}
    walk_forward = walk_forward_report(
        heldout, base_heldout, vehicle=vehicle, folds=folds,
        min_test_sessions=max(1, int(min_sessions) // max(1, int(folds))),
        # Forward-stability folds are diagnostics inside an already adequate
        # held-out partition. Requiring the full aggregate floor in every fold
        # would make the gate mathematically unreachable for ordinary corpora.
        min_test_trades=max(1, int(min_trades) // max(2, int(folds) * 2)))
    rejection = expectancy_rejection_report(heldout, vehicle=vehicle)
    negative_folds = [item for item in walk_forward.get("results", ())
                      if item.get("adequate") and
                      float(item.get("net_pnl", 0.0)) <= 0.0]
    rejection["negative_forward_folds"] = len(negative_folds)
    rejection["independent_negative_windows"] = [
        list(item.get("test_sessions") or ()) for item in negative_folds]
    rejection["multi_window_negative"] = len(negative_folds) >= 2
    final = dict(qualification or {
        "available": False, "sessions": [], "net_positive": False,
        "delta_positive": False,
        "post_selection": {"preselected": False, "candidate_id": None},
    })
    lcb = test.get("mean_delta_lcb")
    checks = {
        "fit_structurally_adequate": bool(fit_floor["adequate"]),
        "heldout_structurally_adequate": bool(held_floor["adequate"]),
        "separated": bool(separation["passes"]),
        "actual_control_available": bool(test.get("available") and test.get("actual_control")),
        "fit_delta_positive": bool(mode == "shadow" or (
            fit_test.get("mean_delta") is not None and float(fit_test["mean_delta"]) > 0)),
        "heldout_delta_positive": bool(test.get("mean_delta") is not None and
                                        float(test["mean_delta"]) > 0),
        "heldout_delta_lcb_positive": bool(lcb is not None and float(lcb) > 0),
        "heldout_p_significant": float(test["p_value"]) <= float(alpha),
        "falsification": bool(falsification["passes"]),
        "heldout_net_pnl_positive": bool(absolute["net_pnl_positive"]),
        "heldout_expectancy_positive": bool(absolute["expectancy_positive"]),
        "null_control_available": bool(null_control["available"]),
        "null_control_delta_positive": bool(
            null_control["mean_delta"] is not None and
            float(null_control["mean_delta"]) > 0 and
            float(null_control["p_value"]) <= float(alpha)),
        "walk_forward_majority_positive": bool(walk_forward["available"] and
                                               walk_forward["majority_positive"]),
        "walk_forward_adequate": bool(walk_forward.get("adequate")),
        "qualification_net_positive": bool(final.get("available") and
                                           final.get("net_positive")),
        "qualification_delta_positive": bool(final.get("available") and
                                             final.get("delta_positive")),
    }
    development_checks = {
        name: value for name, value in checks.items()
        if name not in {"qualification_net_positive",
                        "qualification_delta_positive"}
    }
    return {
        "passes_without_family": bool(all(checks.values())),
        "development_passes_without_family": bool(all(development_checks.values())),
        "passes": False, "p_raw": float(test["p_value"]),
        "sample_adequate": bool(fit_floor["adequate"]),
        "heldout_sample_adequate": bool(held_floor["adequate"] and
                                        walk_forward["available"] and
                                        walk_forward.get("adequate")),
        "confidence": 1.0 - float(test["p_value"]),
        "floor": overall_floor, "fit_floor": fit_floor, "heldout_floor": held_floor,
        "fit_net_pnl": fit_net, "heldout_net_pnl": held_net,
        "heldout_expectancy": absolute["expectancy"],
        "heldout_performance": absolute,
        "fit_trades": sample_counts(fit, vehicle=vehicle)["trades"],
        "heldout_trades": sample_counts(heldout, vehicle=vehicle)["trades"],
        "heldout_delta_lcb": lcb,
        "max_drawdown": max_drawdown_of(ordered), "test": test,
        "fit_test": fit_test, "control": {**test, "kind": (
            "randomized_entry_root_null" if is_root else "matched_root_baseline")},
        "null_control": null_control, "walk_forward": walk_forward,
        "retirement_evidence": rejection,
        "qualification": final,
        "falsification": falsification, "heldout_separation": separation,
        "checks_without_family": checks,
        "mode": mode, "alpha": float(alpha), "is_root": bool(is_root),
        "_fit_rows": fit, "_heldout_rows": heldout,
        "_fit_baseline_rows": base_fit,
        "_heldout_baseline_rows": base_heldout,
        "_null_rows": null_heldout,
    }


def _terminal_negative(local: Sequence[tuple[Mapping, Mapping]]) -> bool:
    """Only powered equivalence evidence across multiple windows may retire."""
    if not local:
        return False
    for _, gate in local:
        if not (gate.get("sample_adequate") and gate.get("heldout_sample_adequate")):
            return False
        rejection = gate.get("retirement_evidence") or {}
        if (rejection.get("rejects_minimum_useful_edge") is not True or
                rejection.get("multi_window_negative") is not True):
            return False
        net = gate.get("heldout_net_pnl")
        expectancy = gate.get("heldout_expectancy")
        try:
            if net is None or expectancy is None:
                return False
            # FDR, walk-forward, or qualification-only failures are
            # inconclusive when the unseen performance is positive.
            if float(net) > 0.0 or float(expectancy) > 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _gate_classification(gate: Mapping[str, Any]) -> str:
    """Name the scientific verdict without collapsing absence into failure."""
    if gate.get("passes") is True:
        return "proved"
    if not (gate.get("sample_adequate") and gate.get("heldout_sample_adequate")):
        return "underpowered"
    try:
        negative = (float(gate.get("heldout_net_pnl")) <= 0.0 and
                    float(gate.get("heldout_expectancy")) <= 0.0)
    except (TypeError, ValueError, OverflowError):
        negative = False
    if negative:
        return ("adequate_negative_rejection" if
                _terminal_negative([({}, gate)]) else
                "adequate_negative_inconclusive")
    return "adequate_inconclusive"


def _existing_specs(edge: EdgeLedger, hypothesis_id: str, vehicle: str) -> list[dict]:
    specs = []
    for candidate in edge.status(vehicle=vehicle):
        if candidate.get("strategy_id") != "rule" or candidate.get("status") not in {
                "backtest_passed", "shadow", "validated", "champion"}:
            continue
        try:
            axes = json.loads(candidate.get("axes_json") or "{}")
            config = json.loads(candidate.get("config_json") or "{}")
        except json.JSONDecodeError:
            continue
        if axes.get("hypothesis_id") == hypothesis_id:
            spec = (config.get("strategy") or {}).get("rule_spec")
            if isinstance(spec, Mapping):
                specs.append(validate_rule_spec(spec))
    return specs


def run_factory(data: str | Path | Sequence[Mapping], *,
                db_path: str | Path = DEFAULT_DB_PATH, vehicle: str = "equity",
                strategies: int = DEFAULT_STRATEGIES,
                variants_per_strategy: int = DEFAULT_VARIANTS,
                workers: int = DEFAULT_WORKERS, starting_cash: float = 100_000.0,
                min_trades: int = 100, min_sessions: int = 10,
                alpha: float = .05, max_generations: int = 5,
                max_rotations: int = MAX_ROTATIONS,
                rotation_budget: int = ROTATION_BUDGET,
                strategy_llm: Mapping[str, Any] | None = None,
                costs: CostModel | None = None,
                runtime_config: Mapping[str, Any] | None = None,
                proposal_adapter: RuleProposalAdapter | None = None,
                worker_data: str | Path | None = None,
                progress_callback: Any | None = None,
                backtest_bar_fallback: bool = False) -> dict:
    """Run one cycle and always release its parent-owned quote index."""
    resources: list[SQLiteQuoteIndex] = []
    try:
        return _run_factory(
            data, db_path=db_path, vehicle=vehicle, strategies=strategies,
            variants_per_strategy=variants_per_strategy, workers=workers,
            starting_cash=starting_cash, min_trades=min_trades,
            min_sessions=min_sessions, alpha=alpha,
            max_generations=max_generations, max_rotations=max_rotations,
            rotation_budget=rotation_budget, strategy_llm=strategy_llm,
            costs=costs, runtime_config=runtime_config,
            proposal_adapter=proposal_adapter, worker_data=worker_data,
            progress_callback=progress_callback,
            backtest_bar_fallback=backtest_bar_fallback, _resources=resources)
    finally:
        for resource in reversed(resources):
            resource.close()


def _run_factory(data: str | Path | Sequence[Mapping], *,
                db_path: str | Path = DEFAULT_DB_PATH, vehicle: str = "equity",
                strategies: int = DEFAULT_STRATEGIES,
                variants_per_strategy: int = DEFAULT_VARIANTS,
                workers: int = DEFAULT_WORKERS, starting_cash: float = 100_000.0,
                min_trades: int = 100, min_sessions: int = 10,
                alpha: float = .05, max_generations: int = 5,
                max_rotations: int = MAX_ROTATIONS,
                rotation_budget: int = ROTATION_BUDGET,
                strategy_llm: Mapping[str, Any] | None = None,
                costs: CostModel | None = None,
                runtime_config: Mapping[str, Any] | None = None,
                proposal_adapter: RuleProposalAdapter | None = None,
                worker_data: str | Path | None = None,
                progress_callback: Any | None = None,
                backtest_bar_fallback: bool = False,
                _resources: list[SQLiteQuoteIndex]) -> dict:
    """Run one autonomous cycle and persist every account, diagnosis and edge."""
    if vehicle not in {"equity", "option"}:
        raise FactoryError("vehicle must be equity or option")
    if not 1 <= int(strategies) <= MAX_STRATEGIES:
        raise FactoryError(f"strategies must be between 1 and {MAX_STRATEGIES}")
    if not 2 <= int(variants_per_strategy) <= MAX_VARIANTS:
        raise FactoryError(f"variants_per_strategy must be between 2 and {MAX_VARIANTS}")
    if not 1 <= int(workers) <= MAX_WORKERS:
        raise FactoryError(f"workers must be between 1 and {MAX_WORKERS}")
    if starting_cash <= 0 or min_trades < 1 or min_sessions < 1:
        raise FactoryError("starting_cash, min_trades and min_sessions must be positive")
    if not 0 < alpha <= 1:
        raise FactoryError("alpha must be in (0,1]")
    if int(max_rotations) < 0 or int(rotation_budget) < 0:
        raise FactoryError("max_rotations and rotation_budget must not be negative")
    if int(max_rotations) > MAX_ROTATIONS or int(rotation_budget) > ROTATION_BUDGET:
        raise FactoryError(
            f"rotation stays bounded: max_rotations<={MAX_ROTATIONS}, "
            f"rotation_budget<={ROTATION_BUDGET}")
    if not isinstance(backtest_bar_fallback, bool):
        raise FactoryError("backtest_bar_fallback must be true or false")
    worker_data_path = None
    if worker_data is not None:
        if not isinstance(data, (str, Path)):
            raise FactoryError("worker_data requires a file or directory data source")
        worker_data_path = Path(worker_data)
        if worker_data_path.is_dir():
            available = [item for item in worker_data_path.glob("*.jsonl")
                         if item.is_file() and item.stat().st_size > 0]
            if not available:
                raise FactoryError("worker_data directory contains no non-empty JSONL")
        elif (not worker_data_path.is_file() or
              worker_data_path.stat().st_size <= 0):
            raise FactoryError("worker_data must be a non-empty JSONL file")
    model = costs or CostModel()
    policy = ReplayPolicy.from_config(runtime_config)
    backtest_policy = replay_policy_for_mode(
        policy, "backtest", backtest_bar_fallback=backtest_bar_fallback)
    shadow_policy = replay_policy_for_mode(
        policy, "shadow", backtest_bar_fallback=backtest_bar_fallback)
    llm_config = dict(strategy_llm or {})
    llm_config.setdefault("near_duplicate_distance", NEAR_DUPLICATE_DISTANCE)
    llm_enabled = bool(llm_config.get("enabled", False))
    if llm_enabled and not str(llm_config.get("model") or "").strip() and proposal_adapter is None:
        raise FactoryError("strategy LLM model is required when autonomous LLM replacement is enabled")
    # One adapter owns the cycle's provider call budget and authentication
    # circuit. Constructing it once is what makes max_total_calls a run-wide
    # bound rather than one independent allowance per helper invocation.
    llm_adapter = proposal_adapter
    if llm_enabled and llm_adapter is None:
        llm_adapter = RuleProposalAdapter(
            provider=str(llm_config.get("provider") or "openai"),
            model=str(llm_config.get("model") or ""),
            max_attempts=int(llm_config.get("max_attempts", 1)),
            timeout_seconds=float(llm_config.get("timeout_seconds", 30)),
            max_response_bytes=int(llm_config.get("max_response_bytes", 16_384)),
            max_total_calls=int(llm_config.get("max_total_calls", 64)),
        )
    # A worker-data path is an optional bars+options replay view.  The parent
    # still normalizes and hashes the complete source, and forces one shared
    # SQLite quote index so child processes never rebuild/read quote rows.
    if worker_data_path is None:
        raw_rows, bars, snapshot_map, quote_rows = _read_discovery_rows(data)
    else:
        raw_rows, bars, snapshot_map, quote_rows = _read_discovery_rows(
            data, force_quote_index=isinstance(data, (str, Path)))
    parent_quote_index = (quote_rows if isinstance(quote_rows, SQLiteQuoteIndex)
                          else None)
    if parent_quote_index is not None:
        _resources.append(parent_quote_index)
    projection_digest = None
    if worker_data_path is not None:
        projection_digest = validate_worker_projection(
            worker_data_path, bars=bars, snapshots=snapshot_map)
    quote_descriptor = (parent_quote_index.descriptor()
                        if parent_quote_index is not None and worker_data_path is not None
                        else None)

    def _progress(phase: str, done: int, total: int) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(str(phase), int(done), int(total))
        except Exception:
            # Progress is an observability hook and must never alter research
            # outcomes or persistence semantics.
            return
    dataset_hash = content_hash(raw_rows)
    gate_assumptions = {
        "min_trades": int(min_trades), "min_sessions": int(min_sessions),
        "alpha": float(alpha), "qualification_fraction": .2,
        "walk_forward_folds": 3,
        "retirement_confidence": RETIREMENT_CONFIDENCE,
        "retirement_min_sessions": RETIREMENT_MIN_SESSIONS,
        "retirement_min_useful_r": RETIREMENT_MIN_USEFUL_R,
        "refinement_policy": "coordinate_then_bounded_interactions_v1",
    }
    llm_assumptions = {
        key: llm_config.get(key) for key in (
            "enabled", "provider", "model", "max_attempts", "timeout_seconds",
            "max_response_bytes", "max_total_calls", "near_duplicate_distance")
        if key in llm_config
    }
    experiment_config = {
        "schema": FACTORY_SCHEMA, "vehicle": vehicle,
        "strategies": int(strategies),
        "variants_per_strategy": int(variants_per_strategy),
        "starting_cash": float(starting_cash),
        "max_generations": int(max_generations),
        "max_rotations": int(max_rotations),
        "rotation_budget": int(rotation_budget),
        "costs": model.as_dict(),
        "replay_policy": policy.as_dict(),
        "replay_policies": {"backtest": backtest_policy.as_dict(),
                             "shadow": shadow_policy.as_dict()},
        "backtest_bar_fallback": backtest_bar_fallback,
        "gate": gate_assumptions, "strategy_llm": llm_assumptions,
    }
    identity_hashes = provenance_hash(
        dataset=raw_rows, config=experiment_config, code=Path(__file__),
        provenance={"factory": FACTORY_SCHEMA, "experiment": experiment_config})
    experiment_provenance_body = experiment_provenance(
        dataset=raw_rows, config=experiment_config,
        code=identity_hashes["code_hash"], cost=model.as_dict(),
        risk=policy.as_dict(), gate=gate_assumptions)
    experiment_provenance_body["replay_policies"] = {
        "backtest": backtest_policy.as_dict(),
        "shadow": shadow_policy.as_dict(),
    }
    experiment_provenance_body["backtest_bar_fallback"] = backtest_bar_fallback
    experiment_provenance_body["code_hash"] = identity_hashes["code_hash"]
    identity = experiment_identity(
        dataset_hash=dataset_hash, vehicle=vehicle,
        code_hash=identity_hashes["code_hash"],
        config_hash=identity_hashes["config_hash"], cost=model.as_dict(),
        risk=policy.as_dict(), gate=gate_assumptions,
        provenance=experiment_provenance_body)
    factory = FactoryLedger(db_path)
    duplicate = factory.existing_cycle(dataset_hash, vehicle, identity)
    if duplicate is not None:
        return {**duplicate, "duplicate": True}
    edge = EdgeLedger(db_path)
    # Seeding a fresh ledger and reviving an idle slot are the same operation:
    # give every configured slot an active hypothesis. Keeping one path means
    # genesis gets discovery too, instead of always starting from templates.
    existing_variant_ids: set[str] = set()
    existing_specs: list[Mapping[str, Any]] = []
    for item in factory.hypotheses(vehicle=vehicle):
        _mark_variant(item["rule_spec"], existing_variant_ids)
        existing_specs.append(item["rule_spec"])
    # Candidate/variant rows outlive a factory hypothesis (tuned variants are
    # deliberately not roots). Include them before discovery so an immutable
    # edge identity can never be re-registered under a new lineage.
    for candidate in edge.status(vehicle=vehicle):
        if candidate.get("strategy_id") != "rule":
            continue
        try:
            config = json.loads(candidate.get("config_json") or "{}")
            spec = (config.get("strategy") or {}).get("rule_spec")
            if isinstance(spec, Mapping):
                try:
                    _mark_variant(spec, existing_variant_ids)
                    existing_specs.append(validate_rule_spec(spec))
                except (TypeError, ValueError, KeyError):
                    continue
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    seeded, revived = _ensure_slots(
        factory, edge, vehicle=vehicle, strategies=strategies,
        existing_variant_ids=existing_variant_ids, llm_enabled=llm_enabled,
        llm_config=llm_config, adapter=llm_adapter,
        existing_specs=existing_specs)
    active = factory.active(vehicle)[:int(strategies)]
    if not active:
        return {"schema": FACTORY_SCHEMA, "status": "exhausted", "dataset_hash": dataset_hash,
                "vehicle": vehicle, "strategies": 0, "variants": 0, "accounts": 0,
                "seeded": seeded, "revived": revived}
    tasks = []
    sealed_windows: dict[str, tuple[Any, list, list]] = {}
    snapshots = list(snapshot_map.values())
    quote_resolver = callable(getattr(quote_rows, "quote_fill", None))
    quotes = quote_rows if quote_resolver else list(quote_rows)
    # A recorded corpus is re-read by each worker from its own descriptor; an
    # in-memory one has no path to re-read and still travels with the task.
    # ``corpus_end`` pins the window against an append-only recorder writing
    # further sessions while this cycle runs.
    # Absolute: a worker resolves the descriptor in its own process, and a
    # relative path is only the same file by coincidence of working directory.
    corpus_source = (str(worker_data_path.resolve()) if
                     (quote_descriptor is not None and worker_data_path is not None)
                     else (str(Path(data).resolve()) if isinstance(data, (str, Path))
                           else None))
    quote_sessions = []
    if quote_resolver:
        latest_quote_session = getattr(quote_rows, "max_session_date", None)
        if latest_quote_session is not None:
            quote_sessions.append(latest_quote_session.isoformat())
    else:
        quote_sessions = [quote.session_date.isoformat() for quote in quotes]
    corpus_end = max([_session(bar) for bar in bars] +
                     [snap.session_date.isoformat() for snap in snapshots] +
                     quote_sessions)
    for hypothesis in active:
        mode = "shadow" if hypothesis.get("status") == "backtest_passed" else "backtest"
        boundary = (factory.last_boundary(hypothesis["hypothesis_id"], vehicle)
                    if mode == "shadow" else hypothesis.get("not_before"))
        selected_bars = [bar for bar in bars if boundary is None or _session(bar) > boundary]
        selected_snapshots = [snap for snap in snapshots if boundary is None or snap.session_date.isoformat() > boundary]
        selected_quotes = (quotes if quote_resolver else
                           [quote for quote in quotes
                            if boundary is None or quote.session_date.isoformat() > boundary])
        specs = _existing_specs(edge, hypothesis["hypothesis_id"], vehicle) if mode == "shadow" else []
        if mode == "shadow" and not specs:
            factory.event(hypothesis["hypothesis_id"], "backtest_passed",
                          "forward validation is waiting for a persisted eligible variant")
            continue
        if not selected_bars:
            factory.event(hypothesis["hypothesis_id"], hypothesis["status"],
                          "no unseen sessions; forward boundary was not consumed",
                          {"boundary": boundary, "dataset_hash": dataset_hash})
            continue
        factory.event(hypothesis["hypothesis_id"], "testing",
                      f"{mode} evaluation started", {"dataset_hash": dataset_hash})
        # The latest sessions are sealed before any worker is scheduled, so
        # mutation, diagnosis and selection are structurally unable to consume
        # the final qualification window.
        development_bars, sealed = seal_final_window(
            selected_bars, session_of=_session, fraction=.2)
        sealed_sessions = set(sealed.session_dates)
        sealed_windows[hypothesis["hypothesis_id"]] = (
            sealed,
            [snap for snap in selected_snapshots
             if snap.session_date.isoformat() in sealed_sessions],
            (selected_quotes if quote_resolver else
             [quote for quote in selected_quotes
              if quote.session_date.isoformat() in sealed_sessions]))
        task = {
            "hypothesis": hypothesis, "vehicle": vehicle, "mode": mode,
            "existing_specs": specs, "variants_per_strategy": variants_per_strategy,
            "starting_cash": starting_cash, "costs": model,
            "policy": (shadow_policy if mode == "shadow" else backtest_policy),
            "backtest_bar_fallback": backtest_bar_fallback,
        }
        if corpus_source is None:
            task.update({
                "bars": development_bars,
                "snapshots": [snap for snap in selected_snapshots
                              if snap.session_date.isoformat() not in sealed_sessions],
                "quotes": ([quote for quote in selected_quotes
                            if quote.session_date.isoformat() not in sealed_sessions]
                           if not quote_resolver else selected_quotes),
            })
        elif quote_descriptor is not None and worker_data_path is not None:
            task["corpus"] = {
                "source": corpus_source, "after": boundary,
                "until": corpus_end, "exclude": sorted(sealed_sessions),
                "quote_descriptor": quote_descriptor,
                "projection_digest": projection_digest,
            }
        else:
            task["corpus"] = {"source": corpus_source, "after": boundary,
                              "until": corpus_end,
                              "exclude": sorted(sealed_sessions)}
        tasks.append(task)
    if not tasks:
        return {"schema": FACTORY_SCHEMA, "status": "waiting_for_new_data",
                "dataset_hash": dataset_hash, "vehicle": vehicle,
                "strategies": len(active), "variants": 0, "accounts": 0}

    max_workers = min(int(workers), len(tasks))
    worker_results = []
    worker_failures = []
    tuning_proposals: list[dict] = []
    # variant_id -> (reason, source), per hypothesis: what the orchestrator
    # decided and why, ready to be graded once the gates are known.
    proposals: dict[str, dict[str, tuple[str, str]]] = {}
    backend = "process"

    def _requeue(task: Mapping[str, Any], exc: BaseException, stage: str) -> None:
        hypothesis = task["hypothesis"]
        resume_status = ("backtest_passed" if task["mode"] == "shadow" else "queued")
        factory.event(
            hypothesis["hypothesis_id"], resume_status,
            f"{stage} failed; hypothesis requeued without a failure conclusion",
            {"error_type": type(exc).__name__, "error": str(exc)[:500],
             "stage": stage})
        worker_failures.append({"hypothesis_id": hypothesis["hypothesis_id"],
                                "error_type": type(exc).__name__,
                                "stage": stage})

    try:
        pool = ProcessPoolExecutor(max_workers=max_workers)
    except (OSError, PermissionError):
        # Some restricted containers disable POSIX semaphores.  Preserve
        # bounded parallel scheduling there; normal deployments use processes.
        pool = ThreadPoolExecutor(max_workers=max_workers)
        backend = "thread_fallback"
    with pool:
        # Phase one: diagnose each backtest hypothesis's root on fit data only.
        # Forward validation carries its variants forward and has nothing to
        # diagnose, so it skips straight to evaluation.
        diagnostics: dict[str, dict] = {}
        pending_diagnosis = [task for task in tasks if task["mode"] == "backtest"]
        failed_hypotheses: set[str] = set()
        _progress("diagnosing", 0, len(pending_diagnosis))
        diagnosis_done = 0
        if pending_diagnosis:
            futures = {pool.submit(_diagnose_worker, task): task
                       for task in pending_diagnosis}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    _requeue(task, exc, "diagnosis")
                    failed_hypotheses.add(str(task["hypothesis"]["hypothesis_id"]))
                    diagnosis_done += 1
                    _progress("diagnosing", diagnosis_done, len(pending_diagnosis))
                    continue
                diagnostics[str(result["hypothesis_id"])] = dict(
                    result["diagnostic"])
                diagnosis_done += 1
                _progress("diagnosing", diagnosis_done, len(pending_diagnosis))

        # Between the phases, and only in this process, the variants are
        # chosen.  Every provider call the factory makes lives here, so no
        # adapter is ever pickled into a worker.
        shared = shared_learning(factory, vehicle=vehicle)
        scheduled = []
        for task in tasks:
            hypothesis = task["hypothesis"]
            hypothesis_id = str(hypothesis["hypothesis_id"])
            if hypothesis_id in failed_hypotheses:
                continue
            if task["mode"] == "shadow":
                chosen = [TunedVariant(
                    spec, "Carried forward unchanged for forward validation; "
                          "a proved variant is never re-tuned.",
                    "carried_forward", None)
                    for spec in task["existing_specs"]]
                task["diagnostic"] = {"primary_failure": "forward_validation"}
            else:
                diagnostic = diagnostics.get(hypothesis_id)
                if diagnostic is None:
                    continue
                task["diagnostic"] = diagnostic
                family = str(hypothesis["family"])
                family_lessons = _lesson_brief(
                    factory, vehicle=vehicle, family=family)
                refinement_lessons = _refinement_history(
                    factory, vehicle=vehicle, hypothesis_id=hypothesis_id)
                refinement: dict[str, Any] = {}
                chosen, tuning = _tuned_variants(
                    hypothesis, diagnostic,
                    count=int(variants_per_strategy), vehicle=vehicle,
                    llm_enabled=llm_enabled, config=llm_config,
                    adapter=llm_adapter,
                    lessons=family_lessons,
                    refinement_lessons=refinement_lessons,
                    already_failed=_failed_variants(factory, vehicle=vehicle,
                                                    family=family),
                    shared=shared,
                    existing_specs=existing_specs,
                    refinement_state=refinement)
                task["refinement"] = refinement
                if tuning is not None:
                    entry = {"hypothesis_id": hypothesis_id,
                             "slot": int(hypothesis["slot"]),
                             "family": family,
                             "schema": tuning.schema,
                             "success": bool(tuning.success),
                             "evidence": dict(tuning.evidence),
                             "tuned_variants": sum(
                                 1 for item in chosen if item.source == "llm")}
                    if not tuning.success:
                        entry["error"] = tuning.error
                    tuning_proposals.append(entry)
                    factory.event(
                        hypothesis_id, "testing",
                        ("model-tuned variants accepted for this hypothesis"
                         if entry["tuned_variants"] else
                         "no model-tuned variant was usable; deterministic "
                         "mutation supplied every variant"),
                        {key: value for key, value in entry.items()
                         if key not in {"hypothesis_id", "slot", "family"}})
            task["specs"] = [item.rule_spec for item in chosen]
            # Make accepted specs visible to later model proposals in this
            # cycle.  This is deliberately after selection: deterministic
            # neighbors are retained, while a later LLM alias cannot claim a
            # fresh experiment against one already accepted by an earlier slot.
            existing_specs.extend(item.rule_spec for item in chosen)
            slot_proposals = proposals.setdefault(hypothesis_id, {})
            for item in chosen:
                variant_id = rule_variant_id(item.rule_spec)
                slot_proposals[variant_id] = (item.reason, item.source)
                if item.source == "carried_forward":
                    continue
                try:
                    factory.record_lesson(
                        hypothesis_id, vehicle=vehicle,
                        family=str(hypothesis["family"]), variant_id=variant_id,
                        kind="tuning",
                        source="llm" if item.source == "llm" else "deterministic",
                        reason=item.reason,
                        changed=spec_delta(hypothesis["rule_spec"],
                                           item.rule_spec),
                        diagnosis=task["diagnostic"],
                        parent_lesson_id=(
                            factory.resolve_lesson_ref(item.builds_on)
                            if item.builds_on else None))
                except (FactoryError, KeyError, sqlite3.Error):
                    # Losing an annotation must not lose the evaluation.
                    pass
            scheduled.append(task)

        # Phase two: replay every chosen variant in its own isolated account.
        futures = {pool.submit(_worker, task): task for task in scheduled}
        _progress("evaluating", 0, len(scheduled))
        evaluation_done = 0
        for future in as_completed(futures):
            task = futures[future]
            try:
                worker_results.append(future.result())
            except Exception as exc:
                _requeue(task, exc, "worker")
            evaluation_done += 1
            _progress("evaluating", evaluation_done, len(scheduled))
    worker_results.sort(key=lambda item: (int(item["hypothesis"]["slot"]),
                                          str(item["hypothesis"]["hypothesis_id"])))
    for worker in worker_results:
        worker["variants"] = sorted(worker["variants"],
                                    key=lambda item: str(item["variant_id"]))

    variant_rows = []
    aggregation_total = sum(len(worker["variants"]) for worker in worker_results)
    _progress("aggregating", 0, aggregation_total)
    aggregation_done = 0
    for worker in worker_results:
        for variant in worker["variants"]:
            gate = _gate(variant["account"]["rows"], vehicle=vehicle,
                         baseline=worker["control_rows"],
                         mode=worker["mode"], min_trades=min_trades,
                         min_sessions=min_sessions, alpha=alpha,
                         is_root=(variant["variant_id"] == rule_variant_id(
                             worker["hypothesis"]["rule_spec"])),
                         null_rows=(worker.get("null_rows") or {}).get(
                             variant["variant_id"], []))
            variant_rows.append((worker, variant, gate))
            aggregation_done += 1
            _progress("aggregating", aggregation_done, aggregation_total)
    # Selection compares candidates across every family and lane in the cycle,
    # so the false-discovery correction that authorizes a champion has to be
    # global.  The family-local correction is retained as reported evidence.
    # A root is a real hypothesis compared against an independent randomized
    # entry null. It is therefore a tested candidate and consumes multiplicity
    # exactly like a mutation; excluding it would grant a free hypothesis.
    candidate_rows = list(variant_rows)
    global_correction = benjamini_hochberg(
        {f"{owner['hypothesis']['hypothesis_id']}:{variant['variant_id']}": gate["p_raw"]
         for owner, variant, gate in candidate_rows}, alpha=alpha)
    for worker in worker_results:
        local_rows = [(variant, gate) for owner, variant, gate in variant_rows
                      if owner is worker]
        correction = benjamini_hochberg(
            {variant["variant_id"]: gate["p_raw"] for variant, gate in local_rows},
            alpha=alpha)
        for variant, gate in local_rows:
            family = correction.get(variant["variant_id"], {
                "p": gate["p_raw"], "p_adjusted": gate["p_raw"],
                "significant": float(gate["p_raw"]) <= float(alpha),
                "family_size": len(correction)})
            overall = global_correction.get(
                f"{worker['hypothesis']['hypothesis_id']}:{variant['variant_id']}", {
                    "p": gate["p_raw"], "p_adjusted": gate["p_raw"],
                    "significant": float(gate["p_raw"]) <= float(alpha),
                    "family_size": len(global_correction)})
            gate["multiple_tests"] = {**family, "method": "benjamini_hochberg",
                                      "scope": "family"}
            gate["global_multiple_tests"] = {**overall,
                                             "method": "benjamini_hochberg",
                                             "scope": "cycle_global"}
            gate["confidence"] = 1.0 - float(overall["p_adjusted"])

    # Rank only development-eligible candidates, then open one sealed window.
    # Cumulative alpha is deliberately not spent on historical or offline
    # forward evidence: the one confirmatory online test belongs to the later,
    # strictly-new parity-matched live-shadow boundary.
    def _selection_rank(item: tuple[Mapping, Mapping, Mapping]) -> tuple:
        owner, variant, gate = item
        overall = gate["global_multiple_tests"]
        lcb = gate.get("heldout_delta_lcb")
        return (
            0 if gate.get("development_passes_without_family") else 1,
            float(overall.get("p_adjusted", 1.0)),
            -float(lcb) if lcb is not None else float("inf"),
            -float(gate.get("heldout_net_pnl") or 0.0),
            int(owner["hypothesis"]["slot"]), str(variant["variant_id"]),
        )

    ranked = sorted(
        (item for item in variant_rows
         if item[2].get("development_passes_without_family") and
         item[2]["multiple_tests"].get("significant") and
         item[2]["global_multiple_tests"].get("significant")),
        key=_selection_rank)
    selected_test = ranked[0] if ranked else None
    selected_test_key = (None if selected_test is None else
                         f"{selected_test[0]['hypothesis']['hypothesis_id']}:"
                         f"{selected_test[1]['variant_id']}")
    confirmatory_scope = f"shadow-confirmation-v2:{vehicle}"
    cumulative = deferred_fdr(
        confirmatory_scope,
        (f"{identity['identity_hash']}:{selected_test_key}:live-shadow"
         if selected_test_key else f"{identity['identity_hash']}:no-selection"))
    qualification_target = selected_test

    qualification_key = None
    qualification_report: dict[str, Any] | None = None
    if qualification_target is not None:
        selected_worker, selected_variant, _selected_gate = qualification_target
        hypothesis_id = str(selected_worker["hypothesis"]["hypothesis_id"])
        selected_policy = selected_worker.get("policy", policy)
        sealed, sealed_snapshots, sealed_quotes = sealed_windows.get(
            hypothesis_id, (None, [], []))
        qualification_bars = (sealed.release(
            reason=f"post-selection qualification {selected_variant['variant_id']}")
            if sealed is not None and sealed.session_dates else None)
        sessions = tuple(sealed.session_dates) if sealed is not None else ()
        if qualification_bars:
            control_rows = simulate_account(
                qualification_bars, sealed_snapshots,
                selected_worker["hypothesis"]["rule_spec"], vehicle=vehicle,
                account_id=f"qualification:control:{hypothesis_id}",
                starting_cash=starting_cash, costs=model, quotes=sealed_quotes,
                policy=selected_policy)["rows"]
            root_variant_id = rule_variant_id(
                selected_worker["hypothesis"]["rule_spec"])
            baseline_rows = control_rows
            if selected_variant["variant_id"] == root_variant_id:
                baseline_rows = null_control_account(
                    qualification_bars, sealed_snapshots,
                    selected_worker["hypothesis"]["rule_spec"], vehicle=vehicle,
                    reference_rows=control_rows,
                    account_id=f"qualification:null:{hypothesis_id}",
                    starting_cash=starting_cash, costs=model,
                    quotes=sealed_quotes, policy=selected_policy)["rows"]
            candidate_rows = simulate_account(
                qualification_bars, sealed_snapshots,
                selected_variant["rule_spec"], vehicle=vehicle,
                account_id=(f"qualification:{hypothesis_id}:"
                            f"{selected_variant['variant_id']}"),
                starting_cash=starting_cash, costs=model, quotes=sealed_quotes,
                policy=selected_policy)["rows"]
            qualification_report = _qualification_report(
                candidate_rows, baseline_rows, vehicle=vehicle,
                sessions=sessions, candidate_id=selected_variant["variant_id"],
                preselected=True)
            qualification_key = selected_test_key

    partitions: dict[str, tuple[list, list]] = {}
    run_contexts: dict[str, tuple[dict, dict, dict]] = {}
    for worker, variant, gate in variant_rows:
        key = f"{worker['hypothesis']['hypothesis_id']}:{variant['variant_id']}"
        selected = key == qualification_key and qualification_report is not None
        final = (dict(qualification_report) if selected else {
            "available": False, "sessions": [], "net_pnl": 0.0,
            "matched": 0, "mean_delta": None, "trades": 0,
            "net_positive": False, "delta_positive": False,
            "post_selection": {"preselected": False,
                                "candidate_id": variant["variant_id"]},
        })
        gate["qualification"] = final
        gate["checks_without_family"]["qualification_net_positive"] = bool(
            final.get("available") and final.get("net_positive"))
        gate["checks_without_family"]["qualification_delta_positive"] = bool(
            final.get("available") and final.get("delta_positive"))
        family = gate["multiple_tests"]
        overall = gate["global_multiple_tests"]
        checks = {
            **gate["checks_without_family"],
            "family_fdr_significant": bool(family["significant"]),
            "global_fdr_significant": bool(overall["significant"]),
            "cumulative_fdr_significant": bool(
                selected and cumulative.get("required") is False and
                cumulative.get("status") == "deferred_to_live_shadow"),
        }
        gate["cumulative_multiple_tests"] = (
            dict(cumulative) if key == selected_test_key else
            {"scope": confirmatory_scope, "tested": False,
             "decision": False, "selected_test_id": selected_test_key})
        gate["post_selection"] = {
            "selected_test_id": selected_test_key,
            "qualified_variant_id": (variant["variant_id"] if selected else None),
            "qualification_consumed": bool(selected),
        }
        gate["passes_without_family"] = bool(
            all(gate["checks_without_family"].values()))
        gate["passes"] = bool(all(checks.values()))
        fit = gate.pop("_fit_rows")
        heldout = gate.pop("_heldout_rows")
        fit_baseline = gate.pop("_fit_baseline_rows", [])
        heldout_baseline = gate.pop("_heldout_baseline_rows", [])
        null_source = gate.pop("_null_rows", [])
        config = {"strategy": {"id": "rule", "version": "v1",
                                "variant_id": variant["variant_id"],
                                "rule_spec": variant["rule_spec"]}}
        worker_policy = worker.get(
            "policy", shadow_policy if worker["mode"] == "shadow" else backtest_policy)
        run_config = {**config, "replay_policy": worker_policy.as_dict(),
                      "replay_mode": worker["mode"],
                      "backtest_bar_fallback": backtest_bar_fallback}
        run_provenance = {
            "factory": FACTORY_SCHEMA,
            "experiment_identity": identity["identity_hash"],
            "hypothesis_id": worker["hypothesis"]["hypothesis_id"],
            "variant_id": variant["variant_id"],
            "costs": model.as_dict(),
            "replay_policy": worker_policy.as_dict(),
            "replay_mode": worker["mode"],
            "backtest_bar_fallback": backtest_bar_fallback,
            "replay_policies": {"backtest": backtest_policy.as_dict(),
                                 "shadow": shadow_policy.as_dict()},
            "gate": gate_assumptions,
            "cumulative_fdr": (dict(cumulative)
                               if key == selected_test_key else None),
        }
        envelope = verified_gate_envelope(
            lane=worker["mode"], vehicle=vehicle, fit=fit, heldout=heldout,
            fit_baseline=fit_baseline, heldout_baseline=heldout_baseline,
            null_source=null_source,
            fit_floor=gate["fit_floor"], heldout_floor=gate["heldout_floor"],
            fit_control=gate["fit_test"], control=gate["control"],
            p_value=gate["p_raw"],
            q_value=overall["p_adjusted"],
            family_q_value=family["p_adjusted"], alpha=alpha,
            falsification=gate["falsification"],
            separation=gate["heldout_separation"], checks=checks,
            passes=gate["passes"], walk_forward=gate["walk_forward"],
            retirement=gate.get("retirement_evidence"),
            qualification=gate["qualification"],
            null_control=gate["null_control"],
            online_fdr=(cumulative if key == selected_test_key else {}),
            provenance=provenance_hash(
                dataset=raw_rows, config=run_config, code=Path(__file__),
                provenance=run_provenance),
            candidate_id=variant["variant_id"],
            performance={"heldout_delta": gate["test"].get("mean_delta"),
                         "heldout_delta_lcb": gate["heldout_delta_lcb"],
                         "heldout_net_pnl": gate["heldout_net_pnl"],
                         "heldout_expectancy": gate["heldout_expectancy"],
                         "max_drawdown": gate["max_drawdown"]})
        gate["passes"] = bool(envelope["passes"])
        gate["verified_gate"] = envelope
        gate["gate_hash"] = envelope["content_hash"]
        gate["failed_checks"] = sorted(
            name for name, ok in envelope["checks"].items() if ok is False)
        run_contexts[variant["account"]["account_id"]] = (
            config, run_config, run_provenance)
        partitions[variant["account"]["account_id"]] = (fit, heldout)

    def _lesson_outcome(gate: Mapping[str, Any]) -> dict:
        """Grade one reason against the gate its variant actually earned."""
        statistics = gate.get("global_multiple_tests")
        return {
            "passed": bool(gate.get("passes")),
            "underpowered": not (gate.get("sample_adequate") and
                                 gate.get("heldout_sample_adequate")),
            "classification": _gate_classification(gate),
            "heldout_delta": (gate.get("test") or {}).get("mean_delta"),
            "heldout_net_pnl": gate.get("heldout_net_pnl"),
            "q_value": (statistics.get("p_adjusted")
                        if isinstance(statistics, Mapping) else None),
            "failed_checks": list(gate.get("failed_checks") or []),
            "gate_hash": gate.get("gate_hash"),
        }

    def _grade(hypothesis_id: str, variant_id: str, kind: str,
               gate: Mapping[str, Any]) -> None:
        try:
            factory.grade_lesson(hypothesis_id, variant_id, kind=kind,
                                 outcome=_lesson_outcome(gate))
        except (FactoryError, KeyError, sqlite3.Error):
            pass

    cycle_id = uuid.uuid4().hex
    summaries = []
    replacements = []
    rotations: list[dict] = []
    reseeds: list[dict] = []
    pending = []
    persist_total = sum(len(item.get("variants", ())) for item in worker_results)
    _progress("persisting", 0, persist_total)
    persisting_done = 0
    for worker in worker_results:
        hypothesis = worker["hypothesis"]
        local = [(variant, gate) for owner, variant, gate in variant_rows
                 if owner is worker]
        adequate = [item for item in local
                    if item[1]["sample_adequate"] and
                    item[1]["heldout_sample_adequate"]]
        all_intended_adequate = bool(
            int(worker.get("expected_variants", 0)) > 0 and
            len(local) == int(worker.get("expected_variants", 0)) and
            len(adequate) == len(local))
        passing = [item for item in local if item[1]["passes"]]
        if worker["mode"] == "shadow" and not all_intended_adequate:
            # A passing subset is not a passing shadow cycle.  Keep the gate
            # verdicts in the account report for diagnosis, but do not advance
            # the hypothesis lifecycle or reseed its slot.
            passing = []
        for variant, gate in local:
            reason, origin = proposals.get(
                str(hypothesis["hypothesis_id"]), {}).get(
                    variant["variant_id"], (None, None))
            result = {**variant, "evaluation_start": worker["evaluation_start"],
                      "evaluation_end": worker["evaluation_end"], "mode": worker["mode"],
                      "gate": gate, "reason": reason, "proposed_by": origin,
                      "classification": _gate_classification(gate)}
            factory.add_account(cycle_id, hypothesis["hypothesis_id"], result)
            # The reason was fixed before this gate existed; now it is graded
            # against it.  That pairing is what later prompts read back.
            _grade(str(hypothesis["hypothesis_id"]), variant["variant_id"],
                   "tuning", gate)
            config, run_config, run_provenance = run_contexts[
                variant["account"]["account_id"]]
            try:
                candidate = edge.register_candidate(
                    variant["variant_id"], strategy_id="rule", vehicle=vehicle,
                    base_version="v1", hypothesis=hypothesis["thesis"], config=config,
                    axes={"hypothesis_id": hypothesis["hypothesis_id"],
                          "slot": hypothesis["slot"], "generation": hypothesis["generation"],
                          "diagnostic": variant["diagnostic"],
                          "simulated_account_id": variant["account"]["account_id"]},
                    dataset=raw_rows, code=Path(__file__),
                    provenance=run_provenance)
            except (ValueError, sqlite3.Error):
                # A legacy ledger may already contain this immutable variant
                # under another hypothesis. Reuse that record rather than
                # crashing the cycle; semantic de-duplication prevents new
                # duplicates, while this keeps old ledgers recoverable.
                candidate = edge.candidate_by_variant(variant["variant_id"], vehicle)
                if candidate is None:
                    raise
            lineage = _llm_lineage_evidence(factory, hypothesis)
            if lineage is not None:
                prior = [item for item in edge.evidence(candidate["candidate_id"])
                         if item.get("kind") == "llm_strategy_proposal"]
                if not prior:
                    edge.append_evidence(
                        candidate["candidate_id"], "llm_strategy_proposal", lineage)
            run = None
            # Shadow variants share one forward boundary.  Do not persist a
            # proof run for an adequately-powered sibling while another
            # intended variant is underpowered: that would advance the
            # hypothesis boundary and make the underpowered variant skip the
            # same unseen observations on the next cycle.
            persist_run = (gate["sample_adequate"] and
                           gate["heldout_sample_adequate"] and
                           bool((gate.get("post_selection") or {}).get(
                               "qualification_consumed")) and
                           (worker["mode"] != "shadow" or all_intended_adequate))
            if persist_run:
                fit, held = partitions[variant["account"]["account_id"]]
                run = edge.append_run(
                    candidate["candidate_id"], lane=worker["mode"], vehicle=vehicle,
                    dataset=raw_rows, config=run_config, code=Path(__file__),
                    provenance=run_provenance,
                    fit=fit, heldout=held,
                    metrics={"gate": gate, "account": {k: v for k, v in variant["account"].items()
                                                       if k != "rows"},
                             "diagnostic": variant["diagnostic"],
                             "confidence": gate["confidence"],
                             "heldout_delta": gate["test"].get("mean_delta"),
                             "max_drawdown": gate["max_drawdown"]})
                for trade in variant["account"]["rows"]:
                    edge.append_trade(run["run_id"], trade)
                edge.record_verified_gate(run["run_id"], gate)
                edge.append_evidence(candidate["candidate_id"], "autonomous_diagnosis", {
                    "fit_diagnosis": worker["diagnostic"],
                    "variant_diagnosis": variant["diagnostic"], "gate": gate,
                }, run_id=run["run_id"])
                current = edge.candidate(candidate["candidate_id"])["status"]
                if gate["passes"] and worker["mode"] == "backtest" and current == "candidate":
                    edge.transition(candidate["candidate_id"], "backtest_passed",
                                    reason="autonomous held-out gate passed")
                elif gate["passes"] and worker["mode"] == "shadow":
                    if current in {"backtest_passed", "demoted"}:
                        edge.transition(candidate["candidate_id"], "shadow",
                                        reason="later unseen simulated paper gate passed")
                        current = "shadow"
                    # Offline forward replay is prerequisite stability data;
                    # the live-shadow ingester owns the validated transition.
                elif (not gate["passes"] and _terminal_negative([(variant, gate)]) and
                      current in {"candidate", "backtest_passed"}):
                    edge.transition(candidate["candidate_id"], "retired",
                                    reason="adequately powered autonomous gate was terminally negative")
                elif (not gate["passes"] and _terminal_negative([(variant, gate)]) and
                      current in {"shadow", "validated", "champion"}):
                    edge.transition(candidate["candidate_id"], "demoted",
                                    reason="latest autonomous gate was terminally negative")
                elif not gate["passes"]:
                    edge.append_event(
                        candidate_id=candidate["candidate_id"],
                        event_type="inconclusive_gate", actor="strategy_factory",
                        reason="mandatory gate failed without terminal negative evidence",
                        payload={"gate_hash": gate.get("gate_hash"),
                                 "failed_checks": gate.get("failed_checks") or []},)
            summaries.append({
                "hypothesis_id": hypothesis["hypothesis_id"],
                "family": hypothesis["family"],
                "candidate_id": candidate["candidate_id"],
                "variant_id": variant["variant_id"], "mode": worker["mode"],
                "evaluation_start": worker["evaluation_start"],
                "evaluation_end": worker["evaluation_end"],
                "account_id": variant["account"]["account_id"],
                "worker_pid": variant["worker_pid"], "gate": gate,
                "classification": _gate_classification(gate),
                "status": (edge.candidate(candidate["candidate_id"]) or {}).get("status"),
                "run_id": run.get("run_id") if run else None,
            })
            persisting_done += 1
            _progress("persisting", persisting_done, persist_total)
        # A hypothesis-level reason — why this slot was given this idea at all
        # — is answered by the best variant the idea produced, never by the
        # root. Roots are evaluated against a randomized-entry null, so they
        # can win legitimately; ranking uses the same evidence as every
        # mutation.
        if local:
            best = max(local, key=lambda item: (
                bool(item[1]["passes"]),
                float(item[1].get("heldout_delta_lcb") or float("-inf"))))[1]
            for kind in HYPOTHESIS_LESSON_KINDS:
                _grade(str(hypothesis["hypothesis_id"]),
                       rule_variant_id(hypothesis["rule_spec"]), kind, best)
        if passing:
            new_state = "testing" if worker["mode"] == "shadow" else "backtest_passed"
            factory.event(hypothesis["hypothesis_id"], new_state,
                          (f"{len(passing)} autonomous variant(s) passed offline shadow; "
                           "awaiting parity-matched live ingestion"
                           if worker["mode"] == "shadow" else
                           f"{len(passing)} autonomous variant(s) passed {worker['mode']}"),
                          {"passing": [item[0]["variant_id"] for item in passing]})
            # A shadow pass remains in ``testing`` until live-shadow parity
            # ingestion marks the hypothesis validated.  The next factory
            # cycle then sees the validated hypothesis as inactive and
            # deterministically reseeds the slot in _ensure_slots.
        elif (all_intended_adequate and _terminal_negative(local) and
              str((worker.get("refinement") or {}).get(
                  "phase") or "confirmatory") != "confirmatory"):
            refinement = dict(worker.get("refinement") or {})
            phase = str(refinement.get("phase") or "coordinate")
            tested_ids = {str(item[0]["variant_id"]) for item in local}
            remaining = [item for item in refinement.get("pool_variant_ids", ())
                         if str(item) not in tested_ids]
            factory.event(
                hypothesis["hypothesis_id"],
                hypothesis.get("status", "testing"),
                (f"{phase} refinement batch was terminally negative; "
                 "the bounded neighborhood continues before replacement"),
                {**refinement, "tested_variant_ids": sorted(tested_ids),
                 "remaining_in_phase_after": len(remaining),
                 "replacement_eligible": False})
        elif all_intended_adequate and _terminal_negative(local):
            # The best near-miss is the least negative/highest-P&L result, not
            # the result with the largest absolute loss.  Replacement and its
            # lesson should preserve what worked best in the exhausted lineage.
            aggregate = max((item[0]["diagnostic"] for item in local),
                            key=lambda value: float(value.get("net_pnl", float("-inf"))))
            proposal = None
            replacement_error = None
            # Each rotation and each reseed grants the slot one further
            # mutation budget, so a freshly seeded family is not born at the
            # generation cap.
            slot = int(hypothesis["slot"])
            rotations_used = _slot_rotations(factory, vehicle, slot)
            reseeds_used = _slot_reseeds(factory, vehicle, slot)
            generation_cap = int(max_generations) * (
                rotations_used + reseeds_used + 1)
            if llm_enabled:
                replacement, proposal, replacement_error = _llm_replacement(
                    hypothesis, aggregate, config=llm_config,
                    max_generations=generation_cap,
                    not_before=worker["evaluation_end"],
                    existing_variant_ids=existing_variant_ids,
                    adapter=llm_adapter,
                    lessons=_lesson_brief(factory, vehicle=vehicle),
                    failed_variant_ids=_failed_variants(factory, vehicle=vehicle),
                    tried_families=_slot_families(factory, vehicle, slot),
                    existing_specs=existing_specs)
                # A model alias is not a reason to strand a research slot. An
                # exact or near semantic duplicate falls back to the existing
                # deterministic replacement policy; malformed/provider
                # failures retain their explicit pending state for audit.
                if replacement is None and replacement_error == "duplicate_llm_variant":
                    replacement = replacement_hypothesis(
                        hypothesis, aggregate, max_generations=generation_cap,
                        not_before=worker["evaluation_end"])
                    proposal = None
                    replacement_error = (None if replacement is not None
                                         else "generation_limit")
            else:
                replacement = replacement_hypothesis(
                    hypothesis, aggregate, max_generations=generation_cap,
                    not_before=worker["evaluation_end"])
            if replacement is not None:
                factory.register(replacement)
                _mark_variant(replacement.rule_spec, existing_variant_ids)
                existing_specs.append(replacement.rule_spec)
                try:
                    factory.record_lesson(
                        replacement.hypothesis_id, vehicle=vehicle,
                        family=replacement.family,
                        variant_id=rule_variant_id(replacement.rule_spec),
                        kind="replacement",
                        source="llm" if proposal is not None else "deterministic",
                        reason=(
                            f"Replacement after every intended variant of "
                            f"{str(hypothesis['family']).replace('_', ' ')} "
                            f"failed with "
                            f"{aggregate.get('primary_failure') or 'no edge'}; "
                            f"moved to "
                            f"{str(replacement.family).replace('_', ' ')}."),
                        changed={"from_family": hypothesis["family"],
                                 "to_family": replacement.family},
                        diagnosis=aggregate,
                        evidence=dict(proposal.evidence) if proposal else {})
                except (FactoryError, KeyError, sqlite3.Error):
                    pass
                retirement_payload = {
                    "diagnostic": aggregate, "tested_variants": len(local),
                    "replacement_hypothesis_id": replacement.hypothesis_id,
                    "replacement_variant_id": rule_variant_id(replacement.rule_spec),
                }
                if proposal is not None:
                    retirement_payload.update({
                        "proposal_schema": proposal.schema,
                        "llm_evidence": proposal.evidence,
                    })
                    factory.event(
                        hypothesis["hypothesis_id"], "testing",
                        "LLM replacement proposal passed the bounded rule grammar",
                        retirement_payload)
                factory.retire_hypothesis(
                    hypothesis["hypothesis_id"], cycle_id=cycle_id,
                    expected_variants=int(worker.get("expected_variants", 0)),
                    reason=("LLM replacement registered after every intended variant failed"
                            if proposal is not None else
                            "deterministic replacement registered after every intended variant failed"),
                    payload=retirement_payload)
                replacements.append(asdict(replacement))
            elif llm_enabled and replacement_error != "generation_limit":
                detail = {
                    "diagnostic": aggregate,
                    "failure": replacement_error or "llm_proposal_failed",
                    "llm_evidence": proposal.evidence if proposal is not None else {},
                    "error": proposal.error if proposal is not None else None,
                }
                factory.event(
                    hypothesis["hypothesis_id"], "pending_llm_replacement",
                    "adequate failure proven; retirement waits for a valid LLM replacement",
                    detail)
                pending.append({"hypothesis_id": hypothesis["hypothesis_id"],
                                "reason": detail["failure"]})
            else:
                seed = None
                rotation_proposal: ProposalResult | None = None
                rotation_source = "deterministic_discovery"
                # Only a family that actually spent a mutation budget may be
                # rotated away; reseeding one that was never mutated would be
                # family churn rather than bounded exploration.
                if (int(hypothesis["generation"]) >= 1 and
                        rotations_used < int(max_rotations) and
                        len(rotations) < int(rotation_budget)):
                    tried = _slot_families(factory, vehicle, slot)
                    seed, rotation_proposal, rotation_source = _seed_slot(
                        hypothesis,
                        generation=factory.next_generation(vehicle, slot),
                        not_before=worker["evaluation_end"],
                        existing_variant_ids=existing_variant_ids,
                        existing_specs=existing_specs,
                        tried_families=tried,
                        context=_discovery_context(
                            slot=slot, reason="generation_budget_exhausted",
                            previous=hypothesis, tried_families=tried,
                            proved_families=_proved_families(edge, vehicle),
                            diagnostic=aggregate,
                            lessons=_lesson_brief(factory, vehicle=vehicle),
                            shared=shared),
                        llm_enabled=llm_enabled, config=llm_config,
                        adapter=llm_adapter)
                if seed is None:
                    factory.event(
                        hypothesis["hypothesis_id"], "pending_generation_limit",
                        "all variants failed, but the generation cap leaves this slot pending explicit rotation",
                        {"diagnostic": aggregate, "max_generations": generation_cap,
                         "rotations_used": rotations_used,
                         "max_rotations": int(max_rotations)},
                    )
                    pending.append({"hypothesis_id": hypothesis["hypothesis_id"],
                                    "reason": "generation_limit"})
                else:
                    factory.register(seed)
                    _mark_variant(seed.rule_spec, existing_variant_ids)
                    existing_specs.append(seed.rule_spec)
                    _record_seed_lesson(factory, seed, vehicle=vehicle,
                                        kind="rotation", source=rotation_source,
                                        proposal=rotation_proposal,
                                        diagnostic=aggregate)
                    rotation_payload = {
                        "diagnostic": aggregate, "tested_variants": len(local),
                        "rotation": True, "rotation_index": rotations_used,
                        "max_rotations": int(max_rotations),
                        "generation_cap": generation_cap,
                        "source": rotation_source,
                        "replacement_hypothesis_id": seed.hypothesis_id,
                        "replacement_variant_id": rule_variant_id(seed.rule_spec),
                        "replacement_rule_schema": seed.rule_spec["schema"],
                    }
                    if rotation_proposal is not None:
                        rotation_payload.update({
                            "proposal_schema": rotation_proposal.schema,
                            "llm_evidence": rotation_proposal.evidence,
                        })
                        if not rotation_proposal.success:
                            rotation_payload["llm_error"] = rotation_proposal.error
                    factory.retire_hypothesis(
                        hypothesis["hypothesis_id"], cycle_id=cycle_id,
                        expected_variants=int(worker.get("expected_variants", 0)),
                        reason="generation budget exhausted; slot rotated to a fresh family",
                        payload=rotation_payload)
                    rotations.append(asdict(seed))
        elif all_intended_adequate:
            # Adequate evidence can still be inconclusive: FDR, walk-forward,
            # or the sealed qualification window may fail while held-out
            # performance remains positive. Keep the hypothesis active so
            # those policy-only failures never become terminal retirement.
            factory.event(
                hypothesis["hypothesis_id"], hypothesis.get("status", "testing"),
                "adequate evidence was inconclusive; terminal retirement requires negative performance",
                {"evaluated_variants": len(local),
                 "intended_variants": int(worker.get("expected_variants", 0)),
                 "retirement_eligible": False})
        else:
            factory.event(hypothesis["hypothesis_id"], hypothesis.get("status", "queued"),
                          "sample floor not met; observations were not treated as failure",
                          {"evaluated_variants": len(local), "adequate_variants": len(adequate),
                           "intended_variants": int(worker.get("expected_variants", 0))})

    validated = [row for row in summaries if row["status"] in {"validated", "champion"}]
    champion = None
    if validated:
        champion = edge.select_champion(vehicle=vehicle, min_confidence=1.0 - alpha,
                                        strategy_id="rule")
    budget_evidence = {}
    if llm_adapter is not None:
        budget = getattr(llm_adapter, "_budget_evidence", None)
        if callable(budget):
            budget_evidence = dict(budget())
    classification_counts: dict[str, int] = {}
    for row in summaries:
        name = str(row.get("classification") or "unknown")
        classification_counts[name] = classification_counts.get(name, 0) + 1
    registered_families = sorted({str(item.get("family")) for item in
                                  factory.hypotheses(vehicle=vehicle)
                                  if item.get("family")})
    explored_families = sorted({str(item.get("family")) for item in summaries
                                if item.get("family")})
    untested_families = [item for item in RULE_FAMILIES
                         if item not in explored_families]
    result = {
        "schema": FACTORY_SCHEMA,
        "status": ("partial_worker_failure" if worker_failures else
                   "pending_replacement_capacity" if pending else "complete"),
        "cycle_id": cycle_id,
        "dataset_hash": dataset_hash, "vehicle": vehicle,
        "experiment_identity": identity,
        "experiment_provenance": experiment_provenance_body,
        "parallel_workers": max_workers,
        "parallel_backend": backend,
        "worker_pids": sorted({row["worker_pid"] for row in summaries}),
        "strategies": len(worker_results), "variants": len(summaries),
        "accounts": len(summaries), "results": summaries,
        "verdict": {
            "candidate_proved": bool(classification_counts.get("proved")),
            "classifications": classification_counts,
            "families_explored": explored_families,
            "families_registered": registered_families,
            "families_untested": untested_families,
            "interpretation": (
                "candidate_proved" if classification_counts.get("proved") else
                "no_candidate_proved"),
        },
        "replacements": replacements, "rotations": rotations,
        # Slots reseeded after proving an edge, slots seeded for the first
        # time, and slots revived because they had lost their hypothesis.
        # Together these keep parallel research capacity constant instead of
        # letting it decay toward ``exhausted``.
        "reseeds": reseeds, "seeded": seeded, "revived": revived,
        "active_slots": len(factory.active(vehicle)),
        "rotation_budget": int(rotation_budget), "max_rotations": int(max_rotations),
        "pending": pending, "worker_failures": worker_failures,
        "strategy_llm": {
            "enabled": llm_enabled,
            "provider": llm_config.get("provider") if llm_enabled else None,
            "model": llm_config.get("model") if llm_enabled else None,
            **budget_evidence,
        },
        # What the model was asked to tune, and how much of it survived
        # validation and de-duplication into an actual isolated account.
        "tuning": tuning_proposals,
        "post_selection": {
            "selected_test_id": selected_test_key,
            "qualified_test_id": qualification_key,
            "cumulative_fdr": cumulative,
        },
        "cumulative_fdr_state": factory.fdr_state(
            confirmatory_scope),
        "champion": ({key: champion.get(key) for key in
                      ("candidate_id", "variant_id", "strategy_id", "vehicle", "status")}
                     if champion else None),
    }
    if not worker_failures:
        factory.add_cycle(cycle_id, dataset_hash, vehicle, max_workers,
                          len(worker_results), len(summaries), result,
                          identity=identity,
                          provenance=experiment_provenance_body)
    return result


def factory_status(db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    return FactoryLedger(db_path).status()


__all__ = [
    "DEFAULT_STRATEGIES", "DEFAULT_VARIANTS", "DEFAULT_WORKERS", "FactoryError",
    "FactoryLedger", "StrategyHypothesis", "diagnose", "discovery_hypothesis",
    "factory_status", "initial_hypotheses", "mutate_from_diagnosis",
    "replacement_hypothesis", "run_factory", "simulate_account",
    "template_hypothesis",
]
