"""Research-only explanation and bounded next-selection review.

This module never imports the engine, exchange, risk state, or live prompt.
The deterministic outcome is already immutable before a review begins; the
model can explain it and nominate one registered research setting, but it
cannot alter the verdict or authorize execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from agent import brain, registry as strategy_registry, variants

from .findings import FindingsStore, _content_hash


MAX_RAW_RESPONSE_CHARS = 16_000
MAX_REVIEW_TOKENS = 1_200

RESEARCH_REVIEW_SYSTEM = """You are reviewing a completed research-only
shadow experiment. The deterministic verdict in the request is final. You
must not set, revise, soften, or override it. Explain why the persisted facts
support WORKED, FAILED, or INCONCLUSIVE, and state the material limitations.

You may nominate at most one next research_selection from the supplied
registered catalog. It is research-only: it cannot change the live/demo
strategy, risk, capital, positions, or orders. Do not repeat terminal exact
variants, existing edge candidates, pending selections, or active assignments.

Return one JSON object with exactly these fields:
{
  "explanation": "20 to 4000 characters",
  "limitations": ["up to 8 concise limitations"],
  "next_selection": null OR {
    "strategy_id": "registered strategy",
    "variant_id": "optional exact registered single-axis variant",
    "reasoning": "10 to 1000 characters"
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
    }


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
    allowed = {"explanation", "limitations", "next_selection"}
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
        now: float | None = None) -> dict:
    """Process at most one outcome; failures remain pending for retry."""
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
            "outcome_id": outcome["outcome_id"], "review": review}
