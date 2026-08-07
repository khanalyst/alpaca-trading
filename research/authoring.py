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
import hashlib
import time
from pathlib import Path

from agent.contract_dsl import (MAX_CONDITIONS, OBSERVABLE_FIELDS,
                                SUPPORTED_PRIMITIVES,
                                ContractProposalError, validate)
from agent.staging import (StagingCapacityError, StagingError,
                           StagingNoveltyError, StagingStore)

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


def prompt_version() -> str:
    """Content identity for the exact authoring instruction sent upstream."""
    return hashlib.sha256(AUTHORING_SYSTEM.encode("utf-8")).hexdigest()[:16]


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
    try:
        # A deterministic neighborhood is one mechanism, not three ideas.
        # Showing every configuration to the proposer would make the prompt
        # imply that threshold changes are novel mechanisms.
        root_ids = {
            str(row["contract_id"])
            for row in store.records(active_only=True)
            if row.get("parent_contract_id") is None
        }
        active = [contract for contract in active
                  if contract.contract_id in root_ids]
    except (AttributeError, TypeError):  # compatibility with small test doubles
        pass
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
                        *, generation: int, refinement_policy=None,
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
            from .staged_refinement import stage_initial_neighborhood

            family = stage_initial_neighborhood(
                store, proposal, generation=generation,
                policy=refinement_policy, now=now)
            contract = store.contract(family["root_contract_id"])
        except StagingNoveltyError as exc:
            rejected.append({
                "index": index,
                "contract_id": (proposal or {}).get("contract_id")
                if isinstance(proposal, dict) else None,
                "code": "NO_NOVEL_CANDIDATE",
                "reason": str(exc),
            })
            continue
        except StagingCapacityError as exc:
            rejected.append({
                "index": index,
                "contract_id": (proposal or {}).get("contract_id")
                if isinstance(proposal, dict) else None,
                "code": "CAPACITY",
                "reason": str(exc),
            })
            continue
        except (ContractProposalError, StagingError, ValueError) as exc:
            rejected.append({
                "index": index,
                "contract_id": (proposal or {}).get("contract_id")
                if isinstance(proposal, dict) else None,
                "code": "VALIDATION_FAILED",
                "reason": str(exc),
            })
            continue
        accepted.append({
            "contract_id": contract.contract_id,
            "direction": contract.direction,
            "conditions": contract.describe(),
            "mechanism_id": family["mechanism_id"],
            "registered_contract_ids": family["registered_contract_ids"],
            "configuration_count": family["configuration_count"],
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
                      refinement_policy=None,
                      now: float | None = None) -> dict:
    """One authoring pass with an immutable record for every model attempt."""
    from .provenance import record_authoring_attempt

    generation = store.generation() + 1
    if findings_store is None:
        findings_store = _findings_store_from_config(cfg)
    request = build_request(
        store, history=history, max_proposals=max_proposals,
        findings_store=findings_store, evidence=evidence)
    raw = None
    proposals: list = []
    returned_contract_ids: list[str] = []
    parser_status = "NOT_RUN"
    validation_status = "NOT_RUN"
    requested_ts = time.time() if now is None else float(now)
    configured = (cfg or {}).get("llm") or {}
    provider = str(getattr(author, "provider", None)
                   or configured.get("provider") or "unknown")
    model_id = str(getattr(author, "model", None)
                   or configured.get("model") or "unknown")
    author_prompt_version = str(
        getattr(author, "prompt_version", None) or prompt_version())
    error = None
    try:
        proposer = author or _default_author(cfg)
        provider = str(getattr(proposer, "provider", provider))
        model_id = str(getattr(proposer, "model", model_id))
        author_prompt_version = str(
            getattr(proposer, "prompt_version", None) or prompt_version())
        raw = proposer.complete(request)
        proposals, reasoning = parse_response(raw)
        parser_status = "SUCCEEDED"
    except Exception as exc:  # noqa: BLE001 - a nightly pass must not abort
        parser_status = "FAILED" if raw is not None else "NOT_RUN"
        error = f"{type(exc).__name__}: {exc}"
        result = {
            "status": "FAILED",
            "generation": generation,
            "error": error,
            "raw": (raw[:MAX_RAW_RESPONSE_CHARS] if isinstance(raw, str)
                    else None),
            "accepted_count": 0,
        }
    else:
        returned_contract_ids = [
            str(proposal.get("contract_id"))
            for proposal in proposals
            if isinstance(proposal, dict) and proposal.get("contract_id")]
        result = register_generation(
            store, proposals, generation=generation,
            refinement_policy=refinement_policy,
            now=time.time() if now is None else now)
        audit_rejections = list(result["rejected"])
        audit_rejections.extend({
            "index": index,
            "contract_id": (proposal or {}).get("contract_id")
            if isinstance(proposal, dict) else None,
            "code": "VALIDATION_FAILED",
            "reason": f"generation exceeds the {MAX_PER_GENERATION} proposal cap",
        } for index, proposal in enumerate(
            proposals[MAX_PER_GENERATION:], start=MAX_PER_GENERATION))
        validation_failures = [
            item for item in audit_rejections
            if item.get("code") == "VALIDATION_FAILED"]
        validation_status = (
            "PARTIAL" if result["accepted"] and validation_failures else
            "REJECTED" if validation_failures else "ACCEPTED")
        if result["accepted"]:
            result["status"] = "AUTHORED"
        else:
            rejection_codes = {
                str(item.get("code")) for item in audit_rejections}
            if (not proposals or
                    (rejection_codes
                     and rejection_codes <= {"NO_NOVEL_CANDIDATE"})):
                result["status"] = "NO_NOVEL_CANDIDATE"
            elif rejection_codes and rejection_codes <= {
                    "NO_NOVEL_CANDIDATE", "CAPACITY"}:
                result["status"] = "CAPACITY"
            else:
                result["status"] = "NOTHING_ACCEPTED"
        result["reasoning"] = reasoning
    if parser_status != "SUCCEEDED":
        audit_rejections = []
    attempt_error = error
    if validation_status == "REJECTED" and attempt_error is None:
        attempt_error = "no authored contract passed validation"
    completed_ts = time.time() if now is None else float(now)
    accepted_contract_ids = [
        str(item["contract_id"]) for item in result.get("accepted", [])]
    audit = record_authoring_attempt(
        store.path, generation=generation, requested_ts=requested_ts,
        completed_ts=completed_ts, provider=provider, model_id=model_id,
        prompt_version=author_prompt_version,
        request={"system": AUTHORING_SYSTEM, "user": request},
        context={key: value for key, value in request.items()
                 if key != "evidence"},
        evidence=request.get("evidence"), raw_response=raw,
        parser_status=parser_status, validation_status=validation_status,
        error=attempt_error, returned_contract_ids=returned_contract_ids,
        accepted_contract_ids=accepted_contract_ids,
        rejections=audit_rejections, result=result,
        status=("FAILED" if result["status"] in {
            "FAILED", "NOTHING_ACCEPTED"} else "SUCCEEDED"))
    result["attempt_id"] = audit["attempt_id"]
    result["request_hash"] = audit["request_hash"]
    result["context_hash"] = audit["context_hash"]
    result["evidence_hash"] = audit["evidence_hash"]
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
    proposer.prompt_version = prompt_version()
    return _SystemSwapped(proposer)


class _SystemSwapped:
    """Run the review adapter under the authoring system prompt."""

    def __init__(self, inner):
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.prompt_version = inner.prompt_version

    def complete(self, request: dict) -> str:
        return self.inner.complete(request)
