"""Autonomous, parallel strategy research with isolated simulated accounts.

Seven bounded hypotheses are evaluated in separate worker processes by
default.  A worker may mutate only the audited rule grammar; it cannot write
or execute source code.  Mutations are diagnosed from the chronological fit
partition and judged on untouched held-out or later forward data.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
import json
import math
import os
from pathlib import Path
import random
import sqlite3
from typing import Any, Mapping, Sequence
import uuid

from agent.contracts.rule import (RULE_FAMILIES, hold_deadline, rule_variant_id,
                                  validate_rule_spec)
from .costs import BAR, QUOTE, CostModel, index_quotes, quote_fill
from .edge_lab import (
    DEFAULT_DB_PATH, EdgeLedger, _read_discovery_rows, content_hash,
)
# One randomized-entry null control serves both research lanes; it lives in
# the shared discovery helpers and is re-exported here for its callers.
from .edge_discovery_core import corpus_slice, null_control_account
from .factory_ledger import (
    ACTIVE_HYPOTHESIS_STATES, FACTORY_SCHEMA, FACTORY_STATUSES, FactoryError,
    FactoryLedger,
)
from .gates import (chronological_split, heldout_separation,
                    matched_cluster_test, matched_pairs, max_drawdown_of,
                    performance_floor, placebo_null_distribution,
                    falsification_gate,
                    qualification_report as _qualification_report,
                    sample_counts, seal_final_window,
                    structural_floor, verified_gate_envelope,
                    walk_forward_report)
from .llm_strategy import (DISCOVERY_SCHEMA, TUNING_SCHEMA, ProposalResult,
                           RuleProposalAdapter)
from .stats import benjamini_hochberg, stable_seed
from .factory_core import (
    DEFAULT_STRATEGIES, DEFAULT_VARIANTS, MAX_STRATEGIES, MAX_VARIANTS,
    NOTIONAL_CAP_PCT, StrategyHypothesis, _falsification, _hypothesis_id, _option_at, _safe_variant,
    _session, _simulate_trade, _thesis, _visible, diagnose, discovery_hypothesis,
    initial_hypotheses, mutate_from_diagnosis, mutate_with_reasons,
    mutation_reason, replacement_hypothesis, simulate_account, spec_delta,
    template_hypothesis,
)


DEFAULT_WORKERS = 7
MAX_WORKERS = 16
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
# Hypothesis-level lessons are graded by the best variant the hypothesis
# produced, not by the root, which by construction cannot beat itself.
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
                       lessons: Sequence[Mapping[str, Any]] = ()) -> dict:
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
    return context


def _trim_lessons(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Compact graded lessons into the aggregate brief a prompt may carry."""
    brief: list[dict] = []
    for row in rows:
        outcome = row.get("outcome") or {}
        brief.append({
            "family": row.get("family"),
            "tried": row.get("changed") or {},
            "reason": row.get("reason"),
            "proposed_by": row.get("source"),
            "verdict": ("passed" if outcome.get("passed") else
                        "underpowered" if outcome.get("underpowered") else
                        "failed"),
            "heldout_delta": outcome.get("heldout_delta"),
            "failed_checks": list(outcome.get("failed_checks") or [])[:4],
        })
        # Trim from the oldest end rather than failing the request: a brief
        # that outgrew its bound should lose history, not become no history.
        while brief and len(json.dumps(brief, default=str).encode(
                "utf-8")) > LESSON_BRIEF_BYTES:
            brief.pop(0)
    return brief


def _lesson_brief(factory: FactoryLedger, *, vehicle: str,
                  family: str | None = None,
                  limit: int = LESSON_BRIEF_LIMIT) -> list[dict]:
    """The graded reason history a proposal is allowed to learn from."""
    try:
        rows = factory.lessons(vehicle=vehicle, family=family,
                               graded_only=True, limit=int(limit))
    except (sqlite3.Error, ValueError, KeyError):
        # The brief is an enrichment.  A ledger written before lessons existed
        # must degrade to "no history", never to a failed research cycle.
        return []
    return _trim_lessons(rows)


def _seed_slot(previous: Mapping[str, Any], *, generation: int,
               not_before: str | None, existing_variant_ids: set[str],
               tried_families: set[str], context: Mapping[str, Any],
               llm_enabled: bool, config: Mapping[str, Any],
               adapter: RuleProposalAdapter | None = None
               ) -> tuple[StrategyHypothesis | None, ProposalResult | None, str]:
    """Seed a free slot: LLM discovery first, deterministic ladder second.

    The proposal is only ever a *seed*.  Whatever produced it, the resulting
    hypothesis is registered as ``queued`` and has to earn ``backtest_passed``
    and then a strictly later forward shadow pass through exactly the same
    gates as a deterministic one, so an LLM cannot shorten the evidence path.
    """
    vehicle = str(previous["vehicle"])
    slot = int(previous["slot"])
    proposal: ProposalResult | None = None
    if llm_enabled:
        selected = adapter or RuleProposalAdapter(
            provider=str(config.get("provider") or "openai"),
            model=str(config.get("model") or ""),
            max_attempts=int(config.get("max_attempts", 1)),
            timeout_seconds=float(config.get("timeout_seconds", 30)),
            max_response_bytes=int(config.get("max_response_bytes", 16_384)),
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
        except Exception as exc:  # noqa: BLE001 - degraded, never fatal
            proposal = ProposalResult(False, schema=DISCOVERY_SCHEMA,
                                      error=f"{type(exc).__name__}: {exc}")
        if (proposal is not None and proposal.success and
                proposal.rule_spec is not None and proposal.variant_id and
                proposal.variant_id not in existing_variant_ids):
            spec = validate_rule_spec(proposal.rule_spec)
            return StrategyHypothesis(
                _hypothesis_id(vehicle, slot, generation, spec), slot, generation,
                vehicle, str(spec["family"]),
                # The model's own one-sentence rationale is recorded as the
                # thesis when it supplied one; it is displayed text, never an
                # instruction, and the falsification stays machine-generated.
                proposal.thesis or _thesis(spec), _falsification(spec), spec,
                str(previous["hypothesis_id"]), not_before), proposal, "llm_discovery"
    seeded = discovery_hypothesis(
        previous, generation=generation, not_before=not_before,
        existing_variant_ids=existing_variant_ids, tried_families=tried_families)
    if seeded is None:
        return None, proposal, "exhausted"
    return seeded, proposal, "deterministic_discovery"


def _adapter(config: Mapping[str, Any],
             adapter: RuleProposalAdapter | None) -> RuleProposalAdapter:
    return adapter or RuleProposalAdapter(
        provider=str(config.get("provider") or "openai"),
        model=str(config.get("model") or ""),
        max_attempts=int(config.get("max_attempts", 1)),
        timeout_seconds=float(config.get("timeout_seconds", 30)),
        max_response_bytes=int(config.get("max_response_bytes", 16_384)),
    )


def _tuned_variants(hypothesis: Mapping[str, Any], diagnostic: Mapping[str, Any],
                    *, count: int, vehicle: str, llm_enabled: bool,
                    config: Mapping[str, Any],
                    adapter: RuleProposalAdapter | None,
                    lessons: Sequence[Mapping[str, Any]] = ()
                    ) -> tuple[list[tuple[dict, str, str]], ProposalResult | None]:
    """Choose this hypothesis's variants, each with the reason it was chosen.

    The root is always variant zero and is never replaced: its matched control
    is itself, so it cannot pass, and it is the null calibration the rest of
    the hypothesis is measured against.

    With the LLM lane off this is exactly the previous deterministic mutation,
    unchanged spec-for-spec.  With it on, the model may propose the remaining
    variants from the diagnosis *and the graded outcomes of earlier reasons*,
    and anything it does not supply — or supplies as a duplicate — is topped up
    from the same deterministic table.  A tuned variant is not trusted more
    than a mutated one: both are content-addressed and both face every gate.
    """
    root = validate_rule_spec(hypothesis["rule_spec"])
    if not llm_enabled:
        return ([(spec, reason, "deterministic")
                 for spec, reason in mutate_with_reasons(root, diagnostic, count)],
                None)
    # A wider deterministic pool than ``count`` so that topping up still has
    # unused candidates left after the model's proposals claim some ids.
    pool = mutate_with_reasons(root, diagnostic, MAX_VARIANTS)
    selected = _adapter(config, adapter)
    tune = getattr(selected, "tune", None)
    proposal: ProposalResult | None = None
    try:
        # An injected seam without ``tune``, or one that raises, means no
        # proposal — never a failed cycle.
        proposal = (tune(vehicle=vehicle, slot=int(hypothesis["slot"]),
                         rule_spec=root, diagnosis=dict(diagnostic),
                         count=max(1, int(count) - 1), lessons=list(lessons))
                    if callable(tune) else None)
    except Exception as exc:  # noqa: BLE001 - degraded, never fatal
        proposal = ProposalResult(False, schema=TUNING_SCHEMA,
                                  error=f"{type(exc).__name__}: {exc}")
    chosen: list[tuple[dict, str, str]] = [(root, pool[0][1], "deterministic")]
    seen = {rule_variant_id(root)}
    if proposal is not None and proposal.success:
        for entry in proposal.variants:
            if len(chosen) >= int(count):
                break
            if entry["variant_id"] in seen:
                continue
            seen.add(entry["variant_id"])
            chosen.append((entry["rule_spec"], entry["reason"], "llm"))
    for spec, reason in pool[1:]:
        if len(chosen) >= int(count):
            break
        if rule_variant_id(spec) in seen:
            continue
        seen.add(rule_variant_id(spec))
        chosen.append((spec, reason, "deterministic"))
    return chosen, proposal


def _llm_replacement(previous: Mapping[str, Any], diagnostic: Mapping[str, Any], *,
                     config: Mapping[str, Any], max_generations: int,
                     not_before: str | None,
                     existing_variant_ids: set[str],
                     adapter: RuleProposalAdapter | None = None
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
    )
    proposal = selected.propose(
        vehicle=str(previous["vehicle"]), generation=generation,
        prior_validated_rule_spec=previous["rule_spec"], diagnosis=diagnostic)
    if not proposal.success or proposal.rule_spec is None or not proposal.variant_id:
        return None, proposal, "llm_proposal_failed"
    if proposal.variant_id in existing_variant_ids:
        return None, proposal, "duplicate_llm_variant"
    spec = validate_rule_spec(proposal.rule_spec)
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
                  adapter: RuleProposalAdapter | None
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
    seeded_slots: list[dict] = []
    revived: list[dict] = []
    # Slots are seeded sequentially, so each proposal can be told what the
    # earlier ones in this same cycle already took.  Without it every slot of a
    # fresh ledger gets an identical brief.
    cycle_seeds: list[dict] = [
        {"slot": int(item["slot"]), "family": str(item["family"])}
        for item in factory.active(vehicle)]
    lessons = _lesson_brief(factory, vehicle=vehicle)
    for slot in range(int(strategies)):
        if slot in active_slots:
            continue
        previous = latest.get(slot)
        if previous is None:
            # Genesis. The template is the fallback, not the only option: an
            # empty slot is exactly where a discovery proposal is most useful,
            # and seeding every deployment from the same seven templates would
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
                    tried_families=set(),
                    context=_discovery_context(
                        slot=slot, reason="fresh_slot", previous={
                            "family": seed.family},
                        tried_families={
                            str(item["family"]) for item in latest.values()},
                        proved_families=_proved_families(edge, vehicle),
                        seeded_this_cycle=cycle_seeds, lessons=lessons),
                    llm_enabled=True, config=llm_config, adapter=adapter)
                if (proposed is not None and
                        proposed_source == "llm_discovery"):
                    # The template only supplied the brief; it was never
                    # registered, so it is not an ancestor.  Leaving it as the
                    # parent would dangle a foreign key at a hypothesis that
                    # does not exist and break every lineage read afterwards.
                    seed = replace(proposed, parent_hypothesis_id=None)
                    source = proposed_source
            if rule_variant_id(seed.rule_spec) in existing_variant_ids:
                continue
        else:
            tried = factory.slot_families(vehicle, slot)
            seed, genesis_proposal, source = _seed_slot(
                previous, generation=factory.next_generation(vehicle, slot),
                not_before=previous.get("not_before"),
                existing_variant_ids=existing_variant_ids,
                tried_families=tried,
                context=_discovery_context(
                    slot=slot, reason="slot_had_no_active_hypothesis",
                    previous=previous, tried_families=tried,
                    proved_families=_proved_families(edge, vehicle),
                    seeded_this_cycle=cycle_seeds, lessons=lessons),
                llm_enabled=llm_enabled, config=llm_config, adapter=adapter)
            if seed is None:
                continue
        factory.register(seed)
        existing_variant_ids.add(rule_variant_id(seed.rule_spec))
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
        factory.event(
            previous["hypothesis_id"], str(previous.get("status") or "validated"),
            "idle slot revived with a new hypothesis",
            {"reseed": True, "source": source,
             "successor_hypothesis_id": seed.hypothesis_id,
             "successor_variant_id": rule_variant_id(seed.rule_spec)})
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
    return corpus_slice(corpus["source"], after=corpus["after"],
                        until=corpus["until"], exclude=corpus["exclude"])


def _diagnose_worker(payload: Mapping[str, Any]) -> dict:
    """Diagnose a hypothesis's root on its fit partition only.

    Split out of :func:`_worker` so the orchestrator holds a diagnosis *before*
    variants are chosen.  That ordering is what lets the parameter proposal be
    made centrally — every provider call stays in the parent process, so no
    adapter ever has to cross a process boundary — while the expensive replay
    stays parallel.  The fit cut is computed exactly as it was inside the
    worker, so the diagnosis is the same one the previous single pass produced.
    """
    hypothesis = dict(payload["hypothesis"])
    bars, snapshots, quotes = _task_corpus(payload)
    vehicle = str(payload["vehicle"])
    starting_cash = float(payload["starting_cash"])
    sessions = sorted({_session(bar) for bar in bars})
    cut = max(1, min(len(sessions) - 1, int(len(sessions) * .7))) if len(sessions) > 1 else len(sessions)
    fit_sessions = set(sessions[:cut])
    fit_bars = [bar for bar in bars if _session(bar) in fit_sessions]
    root_account = simulate_account(
        fit_bars, snapshots, hypothesis["rule_spec"], vehicle=vehicle,
        account_id=f"diagnostic:{hypothesis['hypothesis_id']}",
        starting_cash=starting_cash, costs=payload["costs"], quotes=quotes,
    )
    return {"hypothesis_id": str(hypothesis["hypothesis_id"]),
            "diagnostic": diagnose(root_account["rows"],
                                   starting_cash=starting_cash),
            "worker_pid": os.getpid()}


def _worker(payload: Mapping[str, Any]) -> dict:
    hypothesis = dict(payload["hypothesis"])
    bars, snapshots, quotes = _task_corpus(payload)
    vehicle = str(payload["vehicle"])
    mode = str(payload["mode"])
    starting_cash = float(payload["starting_cash"])
    costs = payload["costs"]
    diagnostic = dict(payload["diagnostic"])
    # The orchestrator decided which specs to evaluate — deterministically
    # mutated, model-tuned, or carried forward for forward validation — so the
    # worker only replays them.
    specs = [validate_rule_spec(item) for item in payload["specs"]]
    control_account = simulate_account(
        bars, snapshots, hypothesis["rule_spec"], vehicle=vehicle,
        account_id=f"control:{hypothesis['hypothesis_id']}:{uuid.uuid4().hex[:8]}",
        starting_cash=starting_cash, costs=costs, quotes=quotes,
    )
    variants = []
    null_rows: dict[str, list] = {}
    for spec in specs:
        variant_id = rule_variant_id(spec)
        account_id = f"sim:{hypothesis['hypothesis_id']}:{variant_id}:{vehicle}:{uuid.uuid4().hex[:8]}"
        account = simulate_account(
            bars, snapshots, spec, vehicle=vehicle, account_id=account_id,
            starting_cash=starting_cash, costs=costs, quotes=quotes,
        )
        null_rows[variant_id] = null_control_account(
            bars, snapshots, spec, vehicle=vehicle, reference_rows=account["rows"],
            account_id=f"null:{hypothesis['hypothesis_id']}:{variant_id}:{vehicle}",
            starting_cash=starting_cash, costs=costs, quotes=quotes)["rows"]
        variants.append({
            "variant_id": variant_id, "rule_spec": spec, "vehicle": vehicle,
            "account": account, "diagnostic": diagnose(account["rows"], starting_cash=starting_cash),
            "worker_pid": os.getpid(),
        })
    sessions = sorted({_session(bar) for bar in bars})
    return {"hypothesis": hypothesis, "mode": mode, "diagnostic": diagnostic,
            "evaluation_start": sessions[0] if sessions else None,
            "evaluation_end": sessions[-1] if sessions else None,
            "variants": sorted(variants, key=lambda item: item["variant_id"]),
            "control_rows": control_account["rows"], "null_rows": null_rows,
            "expected_variants": len(specs), "worker_pid": os.getpid()}


def _gate(rows: Sequence[Mapping], baseline: Sequence[Mapping], *,
          vehicle: str, mode: str,
          min_trades: int, min_sessions: int, alpha: float,
          null_rows: Sequence[Mapping] = (),
          qualification: Mapping | None = None,
          folds: int = 3) -> dict:
    ordered = sorted(rows, key=lambda row: (str(row.get("session_date", "")),
                                             str(row.get("entry_timestamp", ""))))
    base_ordered = sorted(baseline, key=lambda row: (str(row.get("session_date", "")),
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
    null_test = matched_cluster_test(heldout, null_heldout, vehicle=vehicle)
    null_control = {"kind": "randomized_entry_null", "matched": null_test["matched"],
                    "available": bool(null_test["available"]),
                    "mean_delta": null_test["mean_delta"],
                    "mean_delta_lcb": null_test["mean_delta_lcb"],
                    "p_value": float(null_test["p_value"])}
    walk_forward = walk_forward_report(heldout, base_heldout, vehicle=vehicle,
                                       folds=folds)
    final = dict(qualification or {"available": False, "sessions": [],
                                   "net_positive": False, "delta_positive": False})
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
        "qualification_net_positive": bool(final.get("available") and
                                           final.get("net_positive")),
        "qualification_delta_positive": bool(final.get("available") and
                                             final.get("delta_positive")),
    }
    return {
        "passes_without_family": bool(all(checks.values())),
        "passes": False, "p_raw": float(test["p_value"]),
        "sample_adequate": bool(fit_floor["adequate"]),
        "heldout_sample_adequate": bool(held_floor["adequate"] and
                                        walk_forward["available"] and
                                        bool(final.get("available"))),
        "confidence": 1.0 - float(test["p_value"]),
        "floor": overall_floor, "fit_floor": fit_floor, "heldout_floor": held_floor,
        "fit_net_pnl": fit_net, "heldout_net_pnl": held_net,
        "heldout_expectancy": absolute["expectancy"],
        "heldout_performance": absolute,
        "fit_trades": sample_counts(fit, vehicle=vehicle)["trades"],
        "heldout_trades": sample_counts(heldout, vehicle=vehicle)["trades"],
        "heldout_delta_lcb": lcb,
        "max_drawdown": max_drawdown_of(ordered), "test": test,
        "fit_test": fit_test, "control": {**test, "kind": "matched_root_baseline"},
        "null_control": null_control, "walk_forward": walk_forward,
        "qualification": final,
        "falsification": falsification, "heldout_separation": separation,
        "checks_without_family": checks,
        "mode": mode, "alpha": float(alpha),
        "_fit_rows": fit, "_heldout_rows": heldout,
    }


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
                proposal_adapter: RuleProposalAdapter | None = None) -> dict:
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
    model = costs or CostModel()
    llm_config = dict(strategy_llm or {})
    llm_enabled = bool(llm_config.get("enabled", False))
    if llm_enabled and not str(llm_config.get("model") or "").strip() and proposal_adapter is None:
        raise FactoryError("strategy LLM model is required when autonomous LLM replacement is enabled")
    raw_rows, bars, snapshot_map, quote_rows = _read_discovery_rows(data)
    dataset_hash = content_hash(raw_rows)
    factory = FactoryLedger(db_path)
    duplicate = factory.existing_cycle(dataset_hash, vehicle)
    if duplicate is not None:
        return {**duplicate, "duplicate": True}
    edge = EdgeLedger(db_path)
    # Seeding a fresh ledger and reviving an idle slot are the same operation:
    # give every configured slot an active hypothesis. Keeping one path means
    # genesis gets discovery too, instead of always starting from templates.
    existing_variant_ids = {
        rule_variant_id(item["rule_spec"])
        for item in factory.hypotheses(vehicle=vehicle)
    }
    seeded, revived = _ensure_slots(
        factory, edge, vehicle=vehicle, strategies=strategies,
        existing_variant_ids=existing_variant_ids, llm_enabled=llm_enabled,
        llm_config=llm_config, adapter=proposal_adapter)
    active = factory.active(vehicle)[:int(strategies)]
    if not active:
        return {"schema": FACTORY_SCHEMA, "status": "exhausted", "dataset_hash": dataset_hash,
                "vehicle": vehicle, "strategies": 0, "variants": 0, "accounts": 0,
                "seeded": seeded, "revived": revived}
    tasks = []
    sealed_windows: dict[str, tuple[Any, list, list]] = {}
    snapshots = list(snapshot_map.values())
    quotes = list(quote_rows)
    # A recorded corpus is re-read by each worker from its own descriptor; an
    # in-memory one has no path to re-read and still travels with the task.
    # ``corpus_end`` pins the window against an append-only recorder writing
    # further sessions while this cycle runs.
    # Absolute: a worker resolves the descriptor in its own process, and a
    # relative path is only the same file by coincidence of working directory.
    corpus_source = (str(Path(data).resolve()) if isinstance(data, (str, Path))
                     else None)
    corpus_end = max([_session(bar) for bar in bars] +
                     [snap.session_date.isoformat() for snap in snapshots] +
                     [quote.session_date.isoformat() for quote in quotes])
    for hypothesis in active:
        mode = "shadow" if hypothesis.get("status") == "backtest_passed" else "backtest"
        boundary = (factory.last_boundary(hypothesis["hypothesis_id"], vehicle)
                    if mode == "shadow" else hypothesis.get("not_before"))
        selected_bars = [bar for bar in bars if boundary is None or _session(bar) > boundary]
        selected_snapshots = [snap for snap in snapshots if boundary is None or snap.session_date.isoformat() > boundary]
        selected_quotes = [quote for quote in quotes
                           if boundary is None or quote.session_date.isoformat() > boundary]
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
            [quote for quote in selected_quotes
             if quote.session_date.isoformat() in sealed_sessions])
        task = {
            "hypothesis": hypothesis, "vehicle": vehicle, "mode": mode,
            "existing_specs": specs, "variants_per_strategy": variants_per_strategy,
            "starting_cash": starting_cash, "costs": model,
        }
        if corpus_source is None:
            task.update({
                "bars": development_bars,
                "snapshots": [snap for snap in selected_snapshots
                              if snap.session_date.isoformat() not in sealed_sessions],
                "quotes": [quote for quote in selected_quotes
                           if quote.session_date.isoformat() not in sealed_sessions],
            })
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
                    continue
                diagnostics[str(result["hypothesis_id"])] = dict(
                    result["diagnostic"])

        # Between the phases, and only in this process, the variants are
        # chosen.  Every provider call the factory makes lives here, so no
        # adapter is ever pickled into a worker.
        scheduled = []
        for task in tasks:
            hypothesis = task["hypothesis"]
            hypothesis_id = str(hypothesis["hypothesis_id"])
            if hypothesis_id in failed_hypotheses:
                continue
            if task["mode"] == "shadow":
                chosen = [(spec, "Carried forward unchanged for forward "
                                 "validation; a proved variant is never re-tuned.",
                           "carried_forward")
                          for spec in task["existing_specs"]]
                task["diagnostic"] = {"primary_failure": "forward_validation"}
            else:
                diagnostic = diagnostics.get(hypothesis_id)
                if diagnostic is None:
                    continue
                task["diagnostic"] = diagnostic
                chosen, tuning = _tuned_variants(
                    hypothesis, diagnostic,
                    count=int(variants_per_strategy), vehicle=vehicle,
                    llm_enabled=llm_enabled, config=llm_config,
                    adapter=proposal_adapter,
                    lessons=_lesson_brief(factory, vehicle=vehicle,
                                          family=str(hypothesis["family"])))
                if tuning is not None:
                    entry = {"hypothesis_id": hypothesis_id,
                             "slot": int(hypothesis["slot"]),
                             "family": str(hypothesis["family"]),
                             "schema": tuning.schema,
                             "success": bool(tuning.success),
                             "evidence": dict(tuning.evidence),
                             "tuned_variants": sum(
                                 1 for _s, _r, origin in chosen if origin == "llm")}
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
            task["specs"] = [spec for spec, _reason, _origin in chosen]
            slot_proposals = proposals.setdefault(hypothesis_id, {})
            for spec, reason, origin in chosen:
                variant_id = rule_variant_id(spec)
                slot_proposals[variant_id] = (reason, origin)
                if origin == "carried_forward":
                    continue
                try:
                    factory.record_lesson(
                        hypothesis_id, vehicle=vehicle,
                        family=str(hypothesis["family"]), variant_id=variant_id,
                        kind="tuning",
                        source="llm" if origin == "llm" else "deterministic",
                        reason=reason,
                        changed=spec_delta(hypothesis["rule_spec"], spec),
                        diagnosis=task["diagnostic"])
                except (FactoryError, KeyError, sqlite3.Error):
                    # Losing an annotation must not lose the evaluation.
                    pass
            scheduled.append(task)

        # Phase two: replay every chosen variant in its own isolated account.
        futures = {pool.submit(_worker, task): task for task in scheduled}
        for future in as_completed(futures):
            task = futures[future]
            try:
                worker_results.append(future.result())
            except Exception as exc:
                _requeue(task, exc, "worker")
    worker_results.sort(key=lambda item: (int(item["hypothesis"]["slot"]),
                                          str(item["hypothesis"]["hypothesis_id"])))
    for worker in worker_results:
        worker["variants"] = sorted(worker["variants"],
                                    key=lambda item: str(item["variant_id"]))

    # The sealed window is opened exactly once per hypothesis, here in the
    # orchestrator, after every worker has finished proposing and diagnosing.
    qualifications: dict[str, dict] = {}
    for worker in worker_results:
        hypothesis_id = str(worker["hypothesis"]["hypothesis_id"])
        sealed, sealed_snapshots, sealed_quotes = sealed_windows.get(
            hypothesis_id, (None, [], []))
        qualification_bars = (sealed.release(reason=f"final qualification {hypothesis_id}")
                              if sealed is not None and sealed.session_dates else None)
        sessions = tuple(sealed.session_dates) if sealed is not None else ()
        control_rows = (simulate_account(
            qualification_bars, sealed_snapshots, worker["hypothesis"]["rule_spec"],
            vehicle=vehicle, account_id=f"qualification:control:{hypothesis_id}",
            starting_cash=starting_cash, costs=model,
            quotes=sealed_quotes)["rows"] if qualification_bars else [])
        for variant in worker["variants"]:
            rows = (simulate_account(
                qualification_bars, sealed_snapshots, variant["rule_spec"],
                vehicle=vehicle,
                account_id=f"qualification:{hypothesis_id}:{variant['variant_id']}",
                starting_cash=starting_cash, costs=model,
                quotes=sealed_quotes)["rows"] if qualification_bars else [])
            qualifications[f"{hypothesis_id}:{variant['variant_id']}"] = (
                _qualification_report(rows, control_rows, vehicle=vehicle,
                                      sessions=sessions))

    variant_rows = []
    for worker in worker_results:
        hypothesis_id = str(worker["hypothesis"]["hypothesis_id"])
        for variant in worker["variants"]:
            gate = _gate(variant["account"]["rows"], vehicle=vehicle,
                         baseline=worker["control_rows"],
                         mode=worker["mode"], min_trades=min_trades,
                         min_sessions=min_sessions, alpha=alpha,
                         null_rows=(worker.get("null_rows") or {}).get(
                             variant["variant_id"], []),
                         qualification=qualifications.get(
                             f"{hypothesis_id}:{variant['variant_id']}"))
            variant_rows.append((worker, variant, gate))
    # Selection compares candidates across every family and lane in the cycle,
    # so the false-discovery correction that authorizes a champion has to be
    # global.  The family-local correction is retained as reported evidence.
    global_correction = benjamini_hochberg(
        {f"{owner['hypothesis']['hypothesis_id']}:{variant['variant_id']}": gate["p_raw"]
         for owner, variant, gate in variant_rows}, alpha=alpha)
    partitions: dict[str, tuple[list, list]] = {}
    for worker in worker_results:
        local_rows = [(variant, gate) for owner, variant, gate in variant_rows
                      if owner is worker]
        correction = benjamini_hochberg(
            {variant["variant_id"]: gate["p_raw"] for variant, gate in local_rows},
            alpha=alpha)
        for variant, gate in local_rows:
            family = correction[variant["variant_id"]]
            overall = global_correction[
                f"{worker['hypothesis']['hypothesis_id']}:{variant['variant_id']}"]
            checks = {**gate["checks_without_family"],
                      "family_fdr_significant": bool(family["significant"]),
                      "global_fdr_significant": bool(overall["significant"])}
            gate["multiple_tests"] = {**family, "method": "benjamini_hochberg",
                                      "scope": "family"}
            gate["global_multiple_tests"] = {**overall,
                                             "method": "benjamini_hochberg",
                                             "scope": "cycle_global"}
            gate["passes"] = bool(gate["passes_without_family"] and
                                  family["significant"] and overall["significant"])
            gate["confidence"] = 1.0 - float(overall["p_adjusted"])
            fit = gate.pop("_fit_rows")
            heldout = gate.pop("_heldout_rows")
            envelope = verified_gate_envelope(
                lane=worker["mode"], vehicle=vehicle, fit=fit, heldout=heldout,
                fit_floor=gate["fit_floor"], heldout_floor=gate["heldout_floor"],
                control=gate["control"], p_value=gate["p_raw"],
                q_value=overall["p_adjusted"],
                family_q_value=family["p_adjusted"], alpha=alpha,
                falsification=gate["falsification"],
                separation=gate["heldout_separation"], checks=checks,
                passes=gate["passes"],
                walk_forward=gate["walk_forward"],
                qualification=gate["qualification"],
                null_control=gate["null_control"],
                performance={"heldout_delta": gate["test"].get("mean_delta"),
                             "heldout_delta_lcb": gate["heldout_delta_lcb"],
                             "heldout_net_pnl": gate["heldout_net_pnl"],
                             "heldout_expectancy": gate["heldout_expectancy"],
                             "max_drawdown": gate["max_drawdown"]})
            gate["verified_gate"] = envelope
            gate["gate_hash"] = envelope["content_hash"]
            gate["failed_checks"] = sorted(
                name for name, ok in checks.items() if ok is False)
            partitions[variant["account"]["account_id"]] = (fit, heldout)

    def _lesson_outcome(gate: Mapping[str, Any]) -> dict:
        """Grade one reason against the gate its variant actually earned."""
        statistics = gate.get("global_multiple_tests")
        return {
            "passed": bool(gate.get("passes")),
            "underpowered": not (gate.get("sample_adequate") and
                                 gate.get("heldout_sample_adequate")),
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
        for variant, gate in local:
            reason, origin = proposals.get(
                str(hypothesis["hypothesis_id"]), {}).get(
                    variant["variant_id"], (None, None))
            result = {**variant, "evaluation_start": worker["evaluation_start"],
                      "evaluation_end": worker["evaluation_end"], "mode": worker["mode"],
                      "gate": gate, "reason": reason, "proposed_by": origin}
            factory.add_account(cycle_id, hypothesis["hypothesis_id"], result)
            # The reason was fixed before this gate existed; now it is graded
            # against it.  That pairing is what later prompts read back.
            _grade(str(hypothesis["hypothesis_id"]), variant["variant_id"],
                   "tuning", gate)
            config = {"strategy": {"id": "rule", "version": "v1",
                                     "variant_id": variant["variant_id"],
                                     "rule_spec": variant["rule_spec"]}}
            candidate = edge.register_candidate(
                variant["variant_id"], strategy_id="rule", vehicle=vehicle,
                base_version="v1", hypothesis=hypothesis["thesis"], config=config,
                axes={"hypothesis_id": hypothesis["hypothesis_id"],
                      "slot": hypothesis["slot"], "generation": hypothesis["generation"],
                      "diagnostic": variant["diagnostic"],
                      "simulated_account_id": variant["account"]["account_id"]},
                dataset=raw_rows, code=Path(__file__),
                provenance={"factory": FACTORY_SCHEMA, "mode": worker["mode"],
                            "worker_pid": variant["worker_pid"]})
            lineage = _llm_lineage_evidence(factory, hypothesis)
            if lineage is not None:
                prior = [item for item in edge.evidence(candidate["candidate_id"])
                         if item.get("kind") == "llm_strategy_proposal"]
                if not prior:
                    edge.append_evidence(
                        candidate["candidate_id"], "llm_strategy_proposal", lineage)
            run = None
            if gate["sample_adequate"] and gate["heldout_sample_adequate"]:
                fit, held = partitions[variant["account"]["account_id"]]
                run = edge.append_run(
                    candidate["candidate_id"], lane=worker["mode"], vehicle=vehicle,
                    dataset=raw_rows, config=config, code=Path(__file__),
                    provenance={"factory": FACTORY_SCHEMA,
                                "simulated_account_id": variant["account"]["account_id"]},
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
                    if current == "backtest_passed":
                        edge.transition(candidate["candidate_id"], "shadow",
                                        reason="later unseen simulated paper gate passed")
                        current = "shadow"
                    if current == "shadow":
                        edge.transition(candidate["candidate_id"], "validated",
                                        reason="backtest and forward simulated paper gates passed")
                elif not gate["passes"] and current in {"candidate", "backtest_passed"}:
                    edge.transition(candidate["candidate_id"], "retired",
                                    reason="adequately powered autonomous gate failed")
                elif not gate["passes"] and current in {"shadow", "validated", "champion"}:
                    edge.transition(candidate["candidate_id"], "demoted",
                                    reason="latest autonomous gate failed mandatory checks")
            summaries.append({
                "hypothesis_id": hypothesis["hypothesis_id"],
                "candidate_id": candidate["candidate_id"],
                "variant_id": variant["variant_id"], "mode": worker["mode"],
                "evaluation_start": worker["evaluation_start"],
                "evaluation_end": worker["evaluation_end"],
                "account_id": variant["account"]["account_id"],
                "worker_pid": variant["worker_pid"], "gate": gate,
                "status": (edge.candidate(candidate["candidate_id"]) or {}).get("status"),
                "run_id": run.get("run_id") if run else None,
            })
        # A hypothesis-level reason — why this slot was given this idea at all
        # — is answered by the best variant the idea produced, never by the
        # root, whose own control is itself and which therefore cannot pass.
        if local:
            best = max(local, key=lambda item: (
                bool(item[1]["passes"]),
                float(item[1].get("heldout_delta_lcb") or float("-inf"))))[1]
            for kind in HYPOTHESIS_LESSON_KINDS:
                _grade(str(hypothesis["hypothesis_id"]),
                       rule_variant_id(hypothesis["rule_spec"]), kind, best)
        if passing:
            new_state = "validated" if worker["mode"] == "shadow" else "backtest_passed"
            factory.event(hypothesis["hypothesis_id"], new_state,
                          f"{len(passing)} autonomous variant(s) passed {worker['mode']}",
                          {"passing": [item[0]["variant_id"] for item in passing]})
            if worker["mode"] == "shadow":
                # A proved hypothesis leaves the active set for good: its
                # variant is deployed and must never be re-tuned.  The *slot*
                # is a unit of parallel research capacity, not a one-shot
                # licence, so it is immediately reseeded with a new hypothesis.
                # Without this the factory loses a worker on every success and
                # eventually reports ``exhausted`` with nothing left to search.
                slot = int(hypothesis["slot"])
                tried = _slot_families(factory, vehicle, slot)
                seed, proposal, source = _seed_slot(
                    hypothesis,
                    generation=factory.next_generation(vehicle, slot),
                    not_before=worker["evaluation_end"],
                    existing_variant_ids=existing_variant_ids,
                    tried_families=tried,
                    context=_discovery_context(
                        slot=slot, reason="slot_proved_an_edge",
                        previous=hypothesis, tried_families=tried,
                        proved_families=_proved_families(edge, vehicle)),
                    llm_enabled=llm_enabled, config=llm_config,
                    adapter=proposal_adapter)
                reseed_payload: dict[str, Any] = {
                    "reseed": True, "source": source,
                    "proved_variants": [item[0]["variant_id"] for item in passing],
                }
                if seed is None:
                    reseed_payload["reseed"] = False
                    factory.event(
                        hypothesis["hypothesis_id"], new_state,
                        "slot proved an edge but no unused successor hypothesis remains",
                        reseed_payload)
                else:
                    factory.register(seed)
                    existing_variant_ids.add(rule_variant_id(seed.rule_spec))
                    _record_seed_lesson(factory, seed, vehicle=vehicle,
                                        kind="reseed", source=source,
                                        proposal=proposal)
                    reseed_payload.update({
                        "successor_hypothesis_id": seed.hypothesis_id,
                        "successor_variant_id": rule_variant_id(seed.rule_spec),
                        "successor_family": seed.family,
                        "successor_rule_schema": seed.rule_spec["schema"],
                    })
                    if proposal is not None:
                        reseed_payload.update({
                            "proposal_schema": proposal.schema,
                            "llm_evidence": proposal.evidence,
                        })
                        if not proposal.success:
                            reseed_payload["llm_error"] = proposal.error
                    factory.event(
                        hypothesis["hypothesis_id"], new_state,
                        "slot proved an edge and was reseeded with a new hypothesis",
                        reseed_payload)
                    reseeds.append(asdict(seed))
        elif all_intended_adequate:
            aggregate = max((item[0]["diagnostic"] for item in local),
                            key=lambda value: abs(float(value.get("net_pnl", 0.0))))
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
                    adapter=proposal_adapter)
            else:
                replacement = replacement_hypothesis(
                    hypothesis, aggregate, max_generations=generation_cap,
                    not_before=worker["evaluation_end"])
            if replacement is not None:
                factory.register(replacement)
                existing_variant_ids.add(rule_variant_id(replacement.rule_spec))
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
                        tried_families=tried,
                        context=_discovery_context(
                            slot=slot, reason="generation_budget_exhausted",
                            previous=hypothesis, tried_families=tried,
                            proved_families=_proved_families(edge, vehicle),
                            diagnostic=aggregate),
                        llm_enabled=llm_enabled, config=llm_config,
                        adapter=proposal_adapter)
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
                    existing_variant_ids.add(rule_variant_id(seed.rule_spec))
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
    result = {
        "schema": FACTORY_SCHEMA,
        "status": ("partial_worker_failure" if worker_failures else
                   "pending_replacement_capacity" if pending else "complete"),
        "cycle_id": cycle_id,
        "dataset_hash": dataset_hash, "vehicle": vehicle,
        "parallel_workers": max_workers,
        "parallel_backend": backend,
        "worker_pids": sorted({row["worker_pid"] for row in summaries}),
        "strategies": len(worker_results), "variants": len(summaries),
        "accounts": len(summaries), "results": summaries,
        "replacements": replacements, "rotations": rotations,
        # Slots reseeded after proving an edge, slots seeded for the first
        # time, and slots revived because they had lost their hypothesis.
        # Together these keep parallel research capacity constant instead of
        # letting it decay toward ``exhausted``.
        "reseeds": reseeds, "seeded": seeded, "revived": revived,
        "active_slots": len(factory.active(vehicle)),
        "rotation_budget": int(rotation_budget), "max_rotations": int(max_rotations),
        "pending": pending, "worker_failures": worker_failures,
        "strategy_llm": {"enabled": llm_enabled,
                         "provider": llm_config.get("provider") if llm_enabled else None,
                         "model": llm_config.get("model") if llm_enabled else None},
        # What the model was asked to tune, and how much of it survived
        # validation and de-duplication into an actual isolated account.
        "tuning": tuning_proposals,
        "champion": ({key: champion.get(key) for key in
                      ("candidate_id", "variant_id", "strategy_id", "vehicle", "status")}
                     if champion else None),
    }
    if not worker_failures:
        factory.add_cycle(cycle_id, dataset_hash, vehicle, max_workers,
                          len(worker_results), len(summaries), result)
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
