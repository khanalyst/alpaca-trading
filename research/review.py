"""Research-only explanation and bounded next-selection review.

This module never imports the engine, exchange, risk state, or live prompt.
The deterministic outcome is already immutable before a review begins; the
model can explain it and nominate one registered research setting, but it
cannot alter the verdict or authorize execution.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

from agent import brain, registry as strategy_registry, variants

from .findings import FindingsStore, _content_hash


MAX_RAW_RESPONSE_CHARS = 16_000
MAX_REVIEW_TOKENS = 1_200

HYPOTHESIS_PREDICATE_FIELD_BOUNDS = {
    # These are exact snapshot keys persisted in the immutable llm_input
    # corpus and consumed through research.corpus. They are not necessarily
    # present in a terminal outcome's aggregate evidence.
    "mom_1h_pct": {"minimum": -100.0, "maximum": 100.0},
    "relative_volume_1h": {"minimum": 0.0, "maximum": 1_000.0},
    "oi_change_4h_pct": {"minimum": -100.0, "maximum": 10_000.0},
    "funding_rate_pct": {"minimum": -100.0, "maximum": 100.0},
    "funding_percentile_30": {"minimum": 0.0, "maximum": 100.0},
    "perp_index_basis_pct": {"minimum": -100.0, "maximum": 100.0},
    "range_pos_pct": {"minimum": 0.0, "maximum": 100.0},
    "atr_1h_ratio": {"minimum": 0.0, "maximum": 1_000.0},
    "spread_pct": {"minimum": 0.0, "maximum": 100.0},
}
HYPOTHESIS_PREDICATE_OPERATORS = ("gt", "gte", "lt", "lte")
HYPOTHESIS_DIRECTIONS = ("long", "short", "both")
HYPOTHESIS_COST_TREATMENTS = (
    "taker_round_trip",
    "maker_entry_taker_exit",
    "realized_funding_plus_fees",
)
HYPOTHESIS_DRAFT_FIELDS = {
    "title", "strategy_id", "mechanism", "payer", "falsifier",
    "predicate", "horizon_hours", "cost_treatment", "evidence_needed",
}
HYPOTHESIS_PREDICATE_FIELDS = {"field", "operator", "value", "direction"}

# research/gates.py::has_mechanism refuses a registered hypothesis whose
# mechanism or falsification runs under 40 characters, on the grounds that a
# claim that thin cannot name a payer or an observation. A model-proposed
# draft is held to the same floor, and additionally to the phrasing test
# below: "try it and see" proposes an action rather than a cause or a test,
# which is precisely the guess this repository exists to refuse. Such a draft
# is rejected at parse time so it is never stored as a proposal at all.
HYPOTHESIS_SUBSTANTIVE_MIN_CHARS = 40
HYPOTHESIS_SUBSTANTIVE_FIELDS = ("mechanism", "falsifier")
HYPOTHESIS_CAUSAL_FIELDS = ("mechanism", "payer", "falsifier")
HYPOTHESIS_NON_MECHANISM_PATTERNS = (
    r"\band see\b",
    r"\bsee what happens\b",
    r"\bsee if\b",
    r"\bfind out\b",
    r"\btry (?:it|this|that|out)\b",
    r"\blook at the (?:numbers|results)\b",
)

RESEARCH_REVIEW_SYSTEM = """You are reviewing a completed research-only
shadow experiment. The deterministic verdict in the request is final. You
must not set, revise, soften, or override it. Explain why the persisted facts
support WORKED, FAILED, or INCONCLUSIVE, and state the material limitations.

You may nominate at most one next research_selection from the supplied
registered catalog. It is research-only: it cannot change the live/demo
strategy, risk, capital, positions, or orders. Do not repeat terminal exact
variants, existing edge candidates, pending selections, or active assignments.

You may also return one hypothesis_draft. It is a NON-EXECUTABLE declarative
draft only. It must be manually reviewed and registered in a later change
before any experiment can use it. It creates no Variant or selection, changes
no configuration or tier, and has no live, demo, order, strategy-generation,
SQL, code, or execution authority. Use only the registered strategy IDs,
predicate fields and bounds, operators, directions, and cost treatments in
the request. Predicate fields are exact snapshot keys from the persisted,
immutable llm_input corpus consumed through research.corpus; do not claim they
are necessarily present in the terminal outcome aggregates.

A draft must state a cause and a test, not an action to try. The mechanism
must say who pays and why they keep paying; the falsifier must name the
observation that would kill the claim. A draft whose mechanism, payer, or
falsifier merely proposes trying something and seeing the result is rejected
and never stored.

Return one JSON object with these required fields and the one optional field
shown below; do not return any other field:
{
  "explanation": "20 to 4000 characters",
  "limitations": ["up to 8 concise limitations"],
  "next_selection": null OR {
    "strategy_id": "registered strategy",
    "variant_id": "optional exact registered single-axis variant",
    "reasoning": "10 to 1000 characters"
  },
  "hypothesis_draft": null OR OMITTED OR {
    "title": "5 to 120 characters",
    "strategy_id": "registered strategy from the request",
    "mechanism": "20 to 1000 characters, at least 40, stating the cause",
    "payer": "5 to 500 characters identifying the payer/return source",
    "falsifier": "20 to 1000 characters, at least 40, stating the "
                 "observation that would refute the claim",
    "predicate": {
      "field": "allowlisted persisted numeric field",
      "operator": "gt, gte, lt, or lte",
      "value": "finite number inside the field bounds",
      "direction": "long, short, or both"
    },
    "horizon_hours": "finite number from 1 to 336",
    "cost_treatment": "allowlisted cost treatment",
    "evidence_needed": "20 to 1000 characters"
  }
}
Do not return a verdict field or any execution instruction.
"""


def prompt_version() -> str:
    return hashlib.sha256(RESEARCH_REVIEW_SYSTEM.encode("utf-8")).hexdigest()[:16]


def _selection_variants() -> dict[str, variants.Variant]:
    path = Path(__file__).resolve().parent / "variants.yaml"
    registered = variants.load_registry(path)
    for strategy_id in sorted(strategy_registry.REGISTRY):
        spec = strategy_registry.spec_for(strategy_id)
        generated = []
        if strategy_id == "momentum":
            generated.extend(variants.hypothesis_variants(
                strategy_id, spec.version))
        generated.extend(variants.preregistered_variants(
            strategy_id, spec.version))
        for variant in generated:
            registered.setdefault(variant.variant_id, variant)
    eligible = {}
    catalog = variants.research_selection_catalog()
    ids = {str(item["variant_id"])
           for items in catalog.values() for item in items}
    for variant_id in sorted(ids):
        if variant_id in registered:
            eligible[variant_id] = registered[variant_id]
    return eligible


def _selection_candidates() -> list[dict]:
    candidates = []
    for strategy_id, items in variants.research_selection_catalog().items():
        for item in items:
            descriptor = {
                **item, "source": "static", "priority": 100,
                "order_key": str(item["variant_id"]),
            }
            descriptor["candidate_key"] = _content_hash({
                "strategy_id": strategy_id,
                "variant_id": descriptor["variant_id"],
                "axis": descriptor["axis"],
                "setting_id": descriptor["setting_id"],
                "setting": descriptor["setting"],
            })
            candidates.append(descriptor)
    return candidates


def build_review_request(store: FindingsStore, outcome: dict) -> dict:
    payload = outcome["payload"]
    catalog = variants.research_selection_catalog()
    return {
        "schema": "research_review_request.v1",
        "outcome": {
            "outcome_id": outcome["outcome_id"],
            "assignment_id": outcome["assignment_id"],
            "strategy_id": outcome["strategy_id"],
            "baseline_variant_id": outcome["baseline_variant_id"],
            "candidate_variant_id": outcome["candidate_variant_id"],
            "terminal_status": outcome["terminal_status"],
            "deterministic_verdict": outcome["verdict"],
            "reasons": payload["reasons"],
            "data_window": payload["data_window"],
            "baseline": payload["baseline"],
            "candidate": payload["candidate"],
            "paired": payload["paired"],
            "data_limitations": payload["data_limitations"],
            "feed_identity": payload["feed_identity"],
            "code_identity": payload["code_identity"],
            "config_identity": payload["config_identity"],
        },
        "history": store.research_history_context(
            outcome["scope_key"], limit=12),
        "registered_catalog": {
            strategy_id: [{
                "variant_id": item["variant_id"],
                "axis": item["axis"],
                "setting_id": item["setting_id"],
                "setting": item["setting"],
            } for item in items]
            for strategy_id, items in catalog.items()
        },
        "hypothesis_draft_contract": {
            "status": "NON_EXECUTABLE_DRAFT",
            "evidence_source": "immutable_llm_input_snapshot_corpus",
            "manual_registration_required": True,
            "creates_variant_or_selection": False,
            "execution_authority": False,
            "registered_strategy_ids": sorted(strategy_registry.REGISTRY),
            "required_fields": sorted(HYPOTHESIS_DRAFT_FIELDS),
            "predicate": {
                "required_fields": sorted(HYPOTHESIS_PREDICATE_FIELDS),
                "field_bounds": HYPOTHESIS_PREDICATE_FIELD_BOUNDS,
                "operators": list(HYPOTHESIS_PREDICATE_OPERATORS),
                "directions": list(HYPOTHESIS_DIRECTIONS),
            },
            "cost_treatments": list(HYPOTHESIS_COST_TREATMENTS),
            "substantive_min_chars": HYPOTHESIS_SUBSTANTIVE_MIN_CHARS,
            "rejects_action_without_cause": True,
        },
    }


def _normalized_text(raw: dict, field: str, minimum: int, maximum: int) -> str:
    value = raw.get(field)
    if not isinstance(value, str):
        raise ValueError(f"research hypothesis_draft {field} must be a string")
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise ValueError(
            f"research hypothesis_draft {field} must be {minimum} to "
            f"{maximum} characters")
    return value


def _normalized_number(value: object, field: str) -> float:
    if type(value) not in {int, float}:  # bool is deliberately not a number.
        raise ValueError(f"research hypothesis_draft {field} must be a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(
            f"research hypothesis_draft {field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"research hypothesis_draft {field} must be finite")
    return number


def _reject_unsubstantiated(draft: dict) -> None:
    """Refuse a draft that proposes an action instead of a cause and a test.

    Runs on the already length-checked text, so the declared bounds remain
    the first thing a too-short field is told about.
    """
    for field in HYPOTHESIS_CAUSAL_FIELDS:
        for pattern in HYPOTHESIS_NON_MECHANISM_PATTERNS:
            if re.search(pattern, draft[field], flags=re.IGNORECASE):
                raise ValueError(
                    f"research hypothesis_draft {field} proposes trying "
                    "something and seeing the result; it must state a cause, "
                    "a payer, or an observation that would refute the claim")
    for field in HYPOTHESIS_SUBSTANTIVE_FIELDS:
        if len(draft[field]) < HYPOTHESIS_SUBSTANTIVE_MIN_CHARS:
            raise ValueError(
                f"research hypothesis_draft {field} must be at least "
                f"{HYPOTHESIS_SUBSTANTIVE_MIN_CHARS} characters to state a "
                "mechanism or a falsifier")


def _parse_hypothesis_draft(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("research hypothesis_draft must be an object or null")
    if set(raw) != HYPOTHESIS_DRAFT_FIELDS:
        missing = sorted(HYPOTHESIS_DRAFT_FIELDS - set(raw))
        extra = sorted(set(raw) - HYPOTHESIS_DRAFT_FIELDS)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("extra " + ", ".join(extra))
        raise ValueError(
            "research hypothesis_draft must have exactly the declared fields"
            + (": " + "; ".join(detail) if detail else ""))

    strategy_id = _normalized_text(raw, "strategy_id", 1, 120)
    if strategy_id not in strategy_registry.REGISTRY:
        raise ValueError(
            "research hypothesis_draft strategy_id is not registered")

    predicate = raw["predicate"]
    if not isinstance(predicate, dict):
        raise ValueError("research hypothesis_draft predicate must be an object")
    if set(predicate) != HYPOTHESIS_PREDICATE_FIELDS:
        raise ValueError(
            "research hypothesis_draft predicate must have exactly field, "
            "operator, value, direction")
    field = predicate["field"]
    if not isinstance(field, str) or field not in HYPOTHESIS_PREDICATE_FIELD_BOUNDS:
        raise ValueError(
            "research hypothesis_draft predicate field is not allowlisted")
    operator = predicate["operator"]
    if operator not in HYPOTHESIS_PREDICATE_OPERATORS:
        raise ValueError(
            "research hypothesis_draft predicate operator is not allowlisted")
    direction = predicate["direction"]
    if direction not in HYPOTHESIS_DIRECTIONS:
        raise ValueError(
            "research hypothesis_draft predicate direction is not allowlisted")
    predicate_value = _normalized_number(
        predicate["value"], "predicate value")
    bounds = HYPOTHESIS_PREDICATE_FIELD_BOUNDS[field]
    if not bounds["minimum"] <= predicate_value <= bounds["maximum"]:
        raise ValueError(
            "research hypothesis_draft predicate value is outside the "
            f"declared bounds for {field}")

    horizon_hours = _normalized_number(raw["horizon_hours"], "horizon_hours")
    if not 1.0 <= horizon_hours <= 336.0:
        raise ValueError(
            "research hypothesis_draft horizon_hours must be 1 to 336")
    cost_treatment = raw["cost_treatment"]
    if cost_treatment not in HYPOTHESIS_COST_TREATMENTS:
        raise ValueError(
            "research hypothesis_draft cost_treatment is not allowlisted")

    draft = {
        "title": _normalized_text(raw, "title", 5, 120),
        "strategy_id": strategy_id,
        "mechanism": _normalized_text(raw, "mechanism", 20, 1_000),
        "payer": _normalized_text(raw, "payer", 5, 500),
        "falsifier": _normalized_text(raw, "falsifier", 20, 1_000),
        "predicate": {
            "field": field,
            "operator": operator,
            "value": predicate_value,
            "direction": direction,
        },
        "horizon_hours": horizon_hours,
        "cost_treatment": cost_treatment,
        "evidence_needed": _normalized_text(
            raw, "evidence_needed", 20, 1_000),
    }
    _reject_unsubstantiated(draft)
    return draft


def parse_review_response(raw_text: str) -> dict:
    text = str(raw_text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("research review response contains no JSON object")
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"research review response is invalid JSON: {exc}") \
            from exc
    if not isinstance(raw, dict):
        raise ValueError("research review response must be an object")
    allowed = {
        "explanation", "limitations", "next_selection", "hypothesis_draft",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            "research review response contains forbidden field(s): "
            + ", ".join(unknown))
    explanation = raw.get("explanation")
    if not isinstance(explanation, str):
        raise ValueError("research review explanation must be a string")
    explanation = explanation.strip()
    if not 20 <= len(explanation) <= 4_000:
        raise ValueError(
            "research review explanation must be 20 to 4000 characters")
    limitations = raw.get("limitations")
    if (not isinstance(limitations, list) or len(limitations) > 8
            or not all(isinstance(item, str) and 1 <= len(item.strip()) <= 500
                       for item in limitations)):
        raise ValueError(
            "research review limitations must be up to 8 non-empty strings")
    limitations = [item.strip() for item in limitations]
    next_selection = raw.get("next_selection")
    parsed_selection = None
    if next_selection is not None:
        parsed_selection = brain._parse_research_selection(  # noqa: SLF001
            next_selection, variants.research_selection_catalog())
        if parsed_selection["validation_status"] != "ACCEPTED":
            raise ValueError(
                "research review next_selection is invalid: "
                + parsed_selection["rejection_reason"])
    return {
        "explanation": explanation,
        "limitations": limitations,
        "next_selection": parsed_selection,
        "hypothesis_draft": _parse_hypothesis_draft(
            raw.get("hypothesis_draft")),
    }


class ResearchReviewLLM:
    """Small provider adapter with a prompt independent of live decisions."""

    def __init__(self, cfg: dict):
        llm = cfg["llm"]
        self.provider = str(llm["provider"])
        self.model = str(llm["model"])
        self.max_tokens = min(int(llm.get("max_tokens", MAX_REVIEW_TOKENS)),
                              MAX_REVIEW_TOKENS)
        self.prompt_version = prompt_version()
        self._no_temperature = brain.LLM._sampling_unsupported(self.model)
        if self.provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY missing for research review")
            from anthropic import Anthropic
            self.client = Anthropic()
        elif self.provider == "openai":
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY missing for research review")
            from openai import OpenAI
            self.client = OpenAI()
        else:
            raise ValueError(f"unknown research review provider {self.provider!r}")

    def complete(self, request: dict) -> str:
        user = json.dumps(request, sort_keys=True, separators=(",", ":"))
        if self.provider == "anthropic":
            kwargs = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": RESEARCH_REVIEW_SYSTEM,
                "messages": [{"role": "user", "content": user}],
            }
            if not self._no_temperature:
                kwargs["temperature"] = 0.0
            response = self.client.messages.create(**kwargs)
            return "".join(
                block.text for block in response.content
                if getattr(block, "type", "") == "text")
        kwargs = {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": RESEARCH_REVIEW_SYSTEM},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "prompt_cache_key": f"okx-research-review-{self.prompt_version}",
        }
        if not self._no_temperature:
            kwargs["temperature"] = 0.0
        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""


def process_pending_review(
        store: FindingsStore, cfg: dict, *, reviewer=None,
        now: float | None = None, outcome: dict | None = None) -> dict:
    """Process one outcome; failures remain pending for retry.

    ``outcome`` is an internal batch-loop escape hatch.  The original public
    behaviour remains unchanged when it is omitted: the oldest pending
    outcome is selected from the store.
    """
    if outcome is None:
        store.ensure_terminal_experiment_outcomes()
        outcome = store.pending_experiment_outcome()
    if outcome is None:
        return {"status": "IDLE", "processed": 0}
    requested_ts = time.time() if now is None else float(now)
    request = build_review_request(store, outcome)
    configured = cfg.get("llm") or {}
    provider = str(getattr(reviewer, "provider", None)
                   or configured.get("provider") or "unknown")
    model_id = str(getattr(reviewer, "model", None)
                   or configured.get("model") or "unknown")
    review_prompt_version = str(
        getattr(reviewer, "prompt_version", None) or prompt_version())
    raw = None
    try:
        active_reviewer = reviewer or ResearchReviewLLM(cfg)
        provider = str(active_reviewer.provider)
        model_id = str(active_reviewer.model)
        review_prompt_version = str(active_reviewer.prompt_version)
        raw = active_reviewer.complete(request)
        parsed = parse_review_response(raw)
    except Exception as exc:  # noqa: BLE001
        attempt = store.record_experiment_review_failure(
            outcome["outcome_id"], provider=provider, model_id=model_id,
            prompt_version=review_prompt_version, request=request,
            raw_response=(raw[:MAX_RAW_RESPONSE_CHARS] if raw else None),
            parse_error=f"{type(exc).__name__}: {exc}",
            requested_ts=requested_ts,
            completed_ts=time.time() if now is None else float(now))
        return {"status": "RETRY_PENDING", "processed": 1,
                "outcome_id": outcome["outcome_id"], "attempt": attempt}

    selection_id = None
    next_selection = parsed["next_selection"]
    limitations = list(parsed["limitations"])
    if next_selection is not None:
        review_run_id = f"research-review:{outcome['outcome_id']}"
        existing = store.research_selection_by_attribution(
            outcome["scope_key"], review_run_id, outcome["outcome_id"])
        try:
            if existing is None:
                for variant in _selection_variants().values():
                    if store.variant(variant.variant_id) is None:
                        store.register(variant)
                existing = store.record_research_selection(
                    next_selection, _selection_candidates(),
                    scope_key=outcome["scope_key"], run_id=review_run_id,
                    cycle_id=outcome["outcome_id"], model_id=model_id,
                    prompt_version=review_prompt_version,
                    now=requested_ts)
            selection_id = existing["selection_id"]
        except Exception as exc:  # noqa: BLE001
            limitations.append(
                f"Next research selection was persisted in this review but "
                f"could not enter the selection ledger: {exc}")
    review = store.record_experiment_review_success(
        outcome["outcome_id"], provider=provider, model_id=model_id,
        prompt_version=review_prompt_version, request=request,
        raw_response=raw[:MAX_RAW_RESPONSE_CHARS], response=parsed,
        explanation=parsed["explanation"], limitations=limitations,
        next_selection=next_selection, selection_id=selection_id,
        requested_ts=requested_ts,
        completed_ts=time.time() if now is None else float(now))
    return {"status": "REVIEWED", "processed": 1,
            "outcome_id": outcome["outcome_id"], "review": review,
            "hypothesis_draft": parsed["hypothesis_draft"]}


def process_pending_reviews(
        store: FindingsStore, cfg: dict, *, max_reviews: int = 8,
        reviewer=None, now: float | None = None) -> dict:
    """Process a bounded snapshot of pending terminal outcomes.

    Each outcome is attempted at most once per invocation.  A provider/parse
    failure is persisted by :func:`process_pending_review` and does not stop
    later outcomes in this batch; an unexpected per-item exception is also
    captured so the nightly loop remains nonfatal and the item stays pending.
    """
    try:
        limit = int(max_reviews)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_reviews must be a positive integer") from exc
    if limit <= 0:
        raise ValueError("max_reviews must be a positive integer")

    store.ensure_terminal_experiment_outcomes()
    pending = [
        outcome for outcome in store.experiment_outcomes()
        if store.experiment_review(outcome["outcome_id"]) is None
    ][:limit]

    results = []
    for outcome in pending:
        try:
            result = process_pending_review(
                store, cfg, reviewer=reviewer, now=now, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 - isolate one bad item
            result = {
                "status": "FAILED",
                "processed": 1,
                "outcome_id": outcome["outcome_id"],
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(result)

    reviewed = sum(item.get("status") == "REVIEWED" for item in results)
    retry_pending = sum(
        item.get("status") == "RETRY_PENDING" for item in results)
    failed = sum(item.get("status") == "FAILED" for item in results)
    if not results:
        status = "IDLE"
    elif reviewed == len(results):
        status = "REVIEWED"
    elif retry_pending == len(results):
        status = "RETRY_PENDING"
    else:
        status = "PARTIAL"
    return {
        "status": status,
        "processed": len(results),
        "reviewed": reviewed,
        "retry_pending": retry_pending,
        "failed": failed,
        "max_reviews": limit,
        "results": results,
    }
