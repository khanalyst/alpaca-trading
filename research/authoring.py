"""Generate new candidate mechanisms from what the evidence has already killed.

The nightly reviewer explains one finished assignment and may nominate the
next setting from a fixed catalog. That closes a loop over settings but not
over ideas: when every registered mechanism has been falsified, there is
nothing left to nominate and the loop idles forever. Worse, the reviewer only
runs once an assignment terminates, so on a corpus where none has, it has
never run at all.

Authoring is deliberately a separate cadence. It reads what has been tried and
why each attempt died, asks for new mechanisms, and registers the ones that
survive validation into the staging store where they get shadow lanes. It does
not wait for a terminal outcome, because idea generation is what a starved
loop needs most and a terminal outcome is exactly what it does not have.

Nothing here can authorise capital. A registered mechanism enters at
``T1_HYPOTHESIS``; live requires ``T3_VALIDATED`` and a reviewed
content-addressed packet, which only a human signature produces.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent.contract_dsl import (MAX_CONDITIONS, OBSERVABLE_FIELDS,
                                SUPPORTED_PRIMITIVES,
                                ContractProposalError, validate)
from agent.staging import StagingError, StagingStore

MAX_PER_GENERATION = 8
MAX_RAW_RESPONSE_CHARS = 20_000

AUTHORING_SYSTEM = """\
You propose falsifiable trading mechanisms for a crypto perpetuals research \
system. You are not trading. Nothing you write reaches an exchange, changes \
configuration, or authorises capital; each accepted proposal becomes an \
isolated paper-traded arm whose results are measured against a baseline.

A mechanism is a claim about WHO LOSES MONEY TO US AND WHY. "Price tends to \
go up after X" is not a mechanism; it is a pattern, and patterns at this \
sample size are noise. State the participant on the other side, why they are \
transacting at a price that is bad for them, and why they cannot simply stop.

You may compare observed market fields or use one bounded deterministic signal \
primitive. The exact scalar fields available are \
supplied in the request; naming anything else is rejected. Operators are >, \
>=, < and <=. A threshold outside a field's observed range fires always or \
never, so it measures nothing while occupying a research lane for weeks.

Supported primitives are lagged_value and rolling_change over persisted \
execution_bars; percentile_rank over persisted funding/positioning percentile \
fields; volatility_filter over atr_1h_ratio; regime_filter over the persisted \
regime label; event_sequence over realized funding events; a two-field \
feature_interaction; order_book_imbalance; and liquidity_state. The staged \
runtime has one symbol row, so cross_sectional_rank is rejected until a full \
universe context is explicitly wired. Do not author exits, horizons, stops, \
targets, sizing, or network/file operations: the fixed neutral staged harness \
owns those.\

The request tells you which mechanisms have already been falsified and why. \
Do not repropose a killed mechanism with a cosmetic change: if crowded \
funding has been tested and failed, a slightly different funding percentile \
is the same claim. Propose a different payer.

Respond with JSON only:
{"proposals": [{"contract_id": "lowercase-id", "mechanism": "...", \
"payer": "...", "falsifier": "...", "direction": "long|short|both", \
"conditions": [{"field": "...", "op": ">=", "value": 0.0, \
"when_direction": "long|short (optional)"}], \
"primitives": [{"primitive": "rolling_change", "field": "close", \
"window": 3, "mode": "pct", "op": ">", "value": 0.5}], "notes": "..."}], \
"reasoning": "why these, given what has already failed"}

Each of mechanism, payer and falsifier must be a real sentence. A falsifier \
must name the observation that would end the claim, not an intention to look.\
"""


def build_request(store: StagingStore, *, history: dict | None = None,
                  max_proposals: int = 4, findings_store=None,
                  evidence: dict | None = None) -> dict:
    """Assemble the bounded research context a proposer needs.

    The staging store remains the source of registration state.  Evidence is
    read separately from the append-only findings store (or supplied by a
    caller that already loaded it), so authoring cannot mutate or reinterpret
    an immutable claim while preparing a prompt.
    """
    active = store.active()
    if evidence is None:
        from .authoring_context import build_evidence_context

        evidence = build_evidence_context(findings_store, history=history)
    return {
        "task": "propose_mechanisms",
        "max_proposals": min(int(max_proposals), MAX_PER_GENERATION),
        "generation": store.generation() + 1,
        "observable_fields": sorted(OBSERVABLE_FIELDS),
        "supported_primitives": list(SUPPORTED_PRIMITIVES),
        "max_conditions_per_contract": MAX_CONDITIONS,
        "already_staged": [
            {"contract_id": contract.contract_id,
             "mechanism": contract.mechanism,
             "payer": contract.payer}
            for contract in active
        ],
        # The point of the loop: a proposer that cannot see why the last
        # generation died will repropose it with a different threshold.
        "falsified": (history or {}).get("falsified", []),
        "inconclusive": (history or {}).get("inconclusive", []),
        "notes": (history or {}).get("notes", ""),
        "evidence": evidence,
    }


def parse_response(raw: str) -> tuple[list[dict], str]:
    """Decode the proposer's reply without trusting its shape."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty authoring response")
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"authoring response is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("authoring response must be a JSON object")
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("authoring response has no proposals list")
    return proposals, str(payload.get("reasoning") or "")


def register_generation(store: StagingStore, proposals: list,
                        *, generation: int,
                        now: float | None = None) -> dict:
    """Validate each proposal independently and keep the ones that hold.

    One malformed proposal must not discard the rest of the generation: the
    proposer is being asked for several ideas precisely because most will be
    wrong, and refusing the batch would make a single bad field name cost a
    night of generation.
    """
    accepted, rejected = [], []
    for index, proposal in enumerate(proposals[:MAX_PER_GENERATION]):
        try:
            contract = store.register(proposal, generation=generation, now=now)
        except (ContractProposalError, StagingError) as exc:
            rejected.append({
                "index": index,
                "contract_id": (proposal or {}).get("contract_id")
                if isinstance(proposal, dict) else None,
                "reason": str(exc),
            })
            continue
        accepted.append({
            "contract_id": contract.contract_id,
            "direction": contract.direction,
            "conditions": contract.describe(),
        })
    return {
        "generation": generation,
        "accepted": accepted,
        "rejected": rejected,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
    }


def author_generation(store: StagingStore, cfg: dict, *, author=None,
                      history: dict | None = None, max_proposals: int = 4,
                      findings_store=None, evidence: dict | None = None,
                      now: float | None = None) -> dict:
    """One authoring pass. Provider failure is recorded, never fatal."""
    generation = store.generation() + 1
    if findings_store is None:
        findings_store = _findings_store_from_config(cfg)
    request = build_request(
        store, history=history, max_proposals=max_proposals,
        findings_store=findings_store, evidence=evidence)
    raw = None
    try:
        proposer = author or _default_author(cfg)
        raw = proposer.complete(request)
        proposals, reasoning = parse_response(raw)
    except Exception as exc:  # noqa: BLE001 - a nightly pass must not abort
        return {
            "status": "FAILED",
            "generation": generation,
            "error": f"{type(exc).__name__}: {exc}",
            "raw": (raw[:MAX_RAW_RESPONSE_CHARS] if isinstance(raw, str)
                    else None),
            "accepted_count": 0,
        }
    result = register_generation(
        store, proposals, generation=generation,
        now=time.time() if now is None else now)
    result["status"] = "AUTHORED" if result["accepted"] else "NOTHING_ACCEPTED"
    result["reasoning"] = reasoning
    return result


def _findings_store_from_config(cfg: dict):
    """Best-effort read-only evidence source for the nightly author path."""
    try:
        from .findings import FindingsStore, resolve_store_path

        research_cfg = (cfg or {}).get("research") or {}
        if not research_cfg:
            return None
        configured = research_cfg.get("findings_store")
        if not configured:
            return None
        path = resolve_store_path(configured)
        if not Path(path).is_file():
            return None
        return FindingsStore(path)
    except Exception:  # noqa: BLE001 - evidence must never block authoring
        return None


def _default_author(cfg: dict):
    from .review import ResearchReviewLLM

    proposer = ResearchReviewLLM(cfg)
    # Same provider adapter, different instruction. Reusing the client keeps
    # one place where credentials and model routing are resolved.
    proposer.system_override = AUTHORING_SYSTEM
    return _SystemSwapped(proposer)


class _SystemSwapped:
    """Run the review adapter under the authoring system prompt."""

    def __init__(self, inner):
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model

    def complete(self, request: dict) -> str:
        from . import review as review_mod

        original = review_mod.RESEARCH_REVIEW_SYSTEM
        review_mod.RESEARCH_REVIEW_SYSTEM = AUTHORING_SYSTEM
        try:
            return self.inner.complete(request)
        finally:
            review_mod.RESEARCH_REVIEW_SYSTEM = original
