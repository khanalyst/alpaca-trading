"""A bounded, data-only adapter for autonomous rule proposals.

This module is intentionally separate from :mod:`agent.brain`.  It accepts a
small, aggregate diagnosis and asks an optional provider for a replacement
``rule_spec``; it never evaluates, writes, or imports model output.  Provider
SDKs are imported lazily so the research package remains usable offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import math
import os
from queue import Empty, Queue
import re
from threading import Thread
from typing import Any, Callable, Mapping

from agent.contracts.rule import (rule_spec_hash, rule_variant_id,
                                  validate_rule_spec)


PROPOSAL_SCHEMA = "llm-rule-proposal.v1"
DISCOVERY_SCHEMA = "llm-edge-discovery.v1"
TUNING_SCHEMA = "llm-variant-tuning.v1"
DEFAULT_RESPONSE_BYTES = 16_384
DEFAULT_ATTEMPTS = 2
DEFAULT_TIMEOUT_SECONDS = 20.0
# A tuning reply proposes the variants of one hypothesis, so it is bounded by
# the same ``MAX_VARIANTS`` the factory itself accepts.
MAX_TUNED_VARIANTS = 8

# The prompt is part of the evidence fingerprint.  Keep it stable and make
# the output boundary explicit for providers that do not support JSON schema.
SYSTEM_PROMPT = """You propose bounded replacement rule strategies for an audited
research process.  Return one JSON object and nothing else, exactly:
{"schema":"llm-rule-proposal.v1","rule_spec":{...}}
The rule_spec must use only the finite rule-strategy.v1 grammar.  Never return
markdown, Python/source code, executable instructions, credentials, market
rows, or fields outside schema and rule_spec.
"""

# Discovery is the other half of the loop: seeding a free research slot with a
# genuinely new hypothesis rather than repairing a family that just failed.
# The grammar is the same audited one, so a discovered edge is validated by
# exactly the gates every other candidate faces.
DISCOVERY_SYSTEM_PROMPT = """You propose new bounded intraday edge hypotheses for
an audited research process.  Return one JSON object and nothing else, exactly:
{"schema":"llm-edge-discovery.v1","rule_spec":{...},"thesis":"..."}
The rule_spec must use only the finite rule-strategy grammar.  Set
"schema":"rule-strategy.v2" inside rule_spec to use the wider grammar, which
adds: "confirmations" (a list of extra filters from trend/volume/volatility,
all of which must hold), "entry_after_minutes" and "entry_before_minutes"
(the minutes-from-09:30-New-York window in which a signal may fire), and
"min_atr_bps"/"max_atr_bps" (the volatility regime the rule is allowed to
trade).  Use them to express a conditional edge, not just retuned numbers.
Propose something structurally different from the already-tried and
already-proved rules you are shown; a near-duplicate is rejected.  "thesis" is
one plain sentence, at most 240 characters, saying why the edge should exist.
Never return markdown, Python/source code, executable instructions,
credentials, market rows, or fields outside schema, rule_spec and thesis.
"""

# Tuning is the third request, and the only one that changes numbers rather
# than structure.  It exists so parameter search can be driven by what earlier
# attempts actually taught, instead of by a fixed mutation table; the reply
# must say *why* for every variant, and that reason is later graded against
# the gate the variant earned.  It cannot widen the grammar, skip a gate, or
# touch a variant that has already been proved.
TUNING_SYSTEM_PROMPT = """You tune the parameters of one bounded intraday rule
strategy for an audited research process.  Return one JSON object and nothing
else, exactly:
{"schema":"llm-variant-tuning.v1","variants":[{"rule_spec":{...},"reason":"..."}]}
Each rule_spec must use only the finite rule-strategy grammar and must keep the
same "family" as the root strategy you are given: you are tuning it, not
replacing it.  Set "schema":"rule-strategy.v2" inside a rule_spec to also use
the conditional fields (confirmations, entry_after_minutes,
entry_before_minutes, min_atr_bps, max_atr_bps).
You are given the root strategy, the diagnosis of how it failed on fit data
only, and the graded lessons from earlier attempts: what was tried, the reason
given for trying it, and what the gates then said.  Use the lessons.  Do not
repeat a change whose recorded outcome was already a failure for the same
reason.  "reason" is one plain sentence, at most 240 characters, naming the
parameter you changed and the diagnosed problem it should fix, so it can be
graded against the result later.
Never return markdown, Python/source code, executable instructions,
credentials, market rows, or fields outside schema, variants, rule_spec and
reason.
"""

_FORBIDDEN_KEYS = {
    "source", "code", "python", "javascript", "typescript", "shell",
    "exec", "execute", "eval", "command", "raw", "raw_rows", "rows",
    "market_rows", "market_data", "ohlcv", "api_key", "apikey", "token",
    "secret", "password", "credential", "credentials",
}
_RESPONSE_KEYS = frozenset(("schema", "rule_spec"))
_DISCOVERY_RESPONSE_KEYS = frozenset(("schema", "rule_spec", "thesis"))
_TUNING_RESPONSE_KEYS = frozenset(("schema", "variants"))
_TUNED_VARIANT_KEYS = frozenset(("rule_spec", "reason"))
MAX_THESIS_CHARS = 240
MAX_REASON_CHARS = 240


def canonical_json(value: Any) -> str:
    """Return the finite, deterministic JSON representation used for hashes."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=_json_default)


def _json_default(value: Any) -> Any:
    # Inputs are deliberately JSON-like.  ``str`` is useful for a date or an
    # enum supplied by a caller, while still keeping hashes deterministic.
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _finite(value: Any, *, path: str = "value") -> Any:
    """Validate a JSON-like value and reject NaN/Infinity and unsafe fields."""

    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized_key = re.sub(
                r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
            if (normalized_key in _FORBIDDEN_KEYS or
                    any(token in normalized_key.split("_") for token in (
                        "source", "code", "python", "javascript", "shell",
                        "exec", "execute", "eval", "command", "rows",
                        "ohlcv", "secret", "token", "password",
                        "credential", "credentials")) or
                    normalized_key in {"api_key", "market_data", "raw_rows"}):
                raise ValueError(f"{path}.{key} is not permitted")
            result[key] = _finite(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_finite(item, path=f"{path}[{index}]")
                for index, item in enumerate(value)]
    raise ValueError(f"{path} must contain JSON-compatible values")


def _safe_diagnosis(value: Mapping[str, Any], *,
                    label: str = "diagnosis") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an aggregate mapping")
    result = _finite(value, path=label)
    # A bounded aggregate diagnosis should not become an unbounded prompt.
    encoded = canonical_json(result).encode("utf-8")
    if len(encoded) > 8_192:
        raise ValueError(f"{label} exceeds the 8192-byte aggregate bound")
    return result


def _safe_lessons(value: Any) -> list[dict[str, Any]]:
    """Bound the graded history a tuning request is allowed to carry.

    Lessons are the feedback half of the loop, so they grow with every cycle.
    The same aggregate bound the diagnosis obeys applies here: a prompt that
    grew without limit would eventually be the whole ledger.
    """

    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError("lessons must be a sequence of aggregate mappings")
    result = [_finite(dict(item), path=f"lessons[{index}]")
              for index, item in enumerate(value)]
    if len(canonical_json(result).encode("utf-8")) > 8_192:
        raise ValueError("lessons exceed the 8192-byte aggregate bound")
    return result


def _safe_text(value: Any, *, label: str, limit: int) -> str:
    """Accept one short plain-text rationale; it is evidence, never a command."""

    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = " ".join(value.split())
    if not text:
        raise ValueError(f"{label} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{label} exceeds {limit} characters")
    if "```" in text:
        raise ValueError(f"{label} must not contain markdown")
    return text


def _safe_thesis(value: Any) -> str:
    return _safe_text(value, label="thesis", limit=MAX_THESIS_CHARS)


def _safe_reason(value: Any) -> str:
    """The stated rationale for one tuned variant.

    This is the half of the loop that makes tuning reviewable: it is stored
    beside the variant it justifies and later graded against the gate that
    variant earned, so a reason that keeps preceding failures is visible as
    such rather than being rediscovered every cycle.
    """

    return _safe_text(value, label="reason", limit=MAX_REASON_CHARS)


def _safe_tuned_variants(value: Any, *, limit: int) -> list[dict[str, Any]]:
    """Validate the ``variants`` list of a tuning reply, before any grammar."""

    if not isinstance(value, (list, tuple)):
        raise ValueError("variants must be a JSON array")
    if not value:
        raise ValueError("variants must not be empty")
    if len(value) > limit:
        raise ValueError(f"variants exceeds the {limit}-entry bound")
    parsed: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise ValueError(f"variants[{index}] must be an object")
        unknown = set(entry) - _TUNED_VARIANT_KEYS
        missing = _TUNED_VARIANT_KEYS - set(entry)
        if unknown:
            raise ValueError(
                f"variants[{index}] has unknown field(s): {', '.join(sorted(unknown))}")
        if missing:
            raise ValueError(
                f"variants[{index}] is missing field(s): {', '.join(sorted(missing))}")
        if not isinstance(entry["rule_spec"], Mapping):
            raise ValueError(f"variants[{index}].rule_spec must be an object")
        # Catch source/credential keys before the grammar's generic unknown
        # field error, preserving an explicit safety failure for callers.
        _finite(entry["rule_spec"], path=f"variants[{index}].rule_spec")
        parsed.append({"rule_spec": dict(entry["rule_spec"]),
                       "reason": _safe_reason(entry["reason"])})
    return parsed


def _raw_text(value: Any) -> str:
    """Extract text from common SDK response shapes without trusting it."""

    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return canonical_json(value)
    output_text = getattr(value, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    # Responses API: output -> content -> text.
    output = getattr(value, "output", None)
    if output:
        pieces: list[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, Mapping):
                content = item.get("content")
            for block in content or ():
                text = getattr(block, "text", None)
                if text is None and isinstance(block, Mapping):
                    text = block.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        if pieces:
            return "".join(pieces)
    # Chat Completions and Anthropic Messages response shapes.
    choices = getattr(value, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        text = getattr(message, "content", None)
        if isinstance(text, str):
            return text
    content = getattr(value, "content", None)
    if content:
        pieces = []
        for block in content:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, Mapping):
                text = block.get("text")
            if isinstance(text, str):
                pieces.append(text)
        if pieces:
            return "".join(pieces)
    raise ValueError("provider response did not contain text")


def _parse_response(value: Any, *, max_bytes: int,
                    schema: str = PROPOSAL_SCHEMA,
                    keys: frozenset[str] = _RESPONSE_KEYS,
                    spec_key: str | None = "rule_spec"
                    ) -> tuple[dict[str, Any], str]:
    raw = _raw_text(value)
    raw_bytes = raw.encode("utf-8")
    if len(raw_bytes) > max_bytes:
        raise ValueError(f"provider response exceeds {max_bytes}-byte cap")
    # Fences are rejected rather than stripped: silently accepting prose or
    # markdown would make the contract less auditable.
    if "```" in raw:
        raise ValueError("markdown fenced responses are not permitted")
    try:
        parsed = json.loads(raw, parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant {token}")))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("provider response is not strict JSON") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("provider response must be a JSON object")
    unknown = set(parsed) - keys
    missing = keys - set(parsed)
    if unknown:
        raise ValueError(f"proposal response has unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"proposal response is missing field(s): {', '.join(sorted(missing))}")
    if parsed.get("schema") != schema:
        raise ValueError(f"proposal schema must be {schema!r}")
    if spec_key is not None:
        if not isinstance(parsed.get(spec_key), Mapping):
            raise ValueError(f"proposal {spec_key} must be an object")
        # Catch source/code keys before the rule validator's more general
        # unknown field error, preserving an explicit safety failure for
        # callers.
        _finite(parsed[spec_key], path=spec_key)
    return dict(parsed), raw


def _invoke(call: Callable[..., Any], system_prompt: str,
            request: Mapping[str, Any], timeout: float | None = None) -> Any:
    signature = None
    try:
        signature = inspect.signature(call)
    except (TypeError, ValueError):
        pass
    kwargs: dict[str, Any] = {}
    args: tuple[Any, ...] = ()
    if signature is not None:
        parameters = signature.parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD
                             for p in parameters.values())
        if accepts_kwargs or "system_prompt" in parameters:
            kwargs["system_prompt"] = system_prompt
        elif "system" in parameters:
            kwargs["system"] = system_prompt
        if accepts_kwargs or "request" in parameters:
            kwargs["request"] = request
        elif "prompt" in parameters:
            kwargs["prompt"] = canonical_json(request)
        if timeout is not None and (accepts_kwargs or "timeout" in parameters):
            kwargs["timeout"] = timeout
        if not kwargs:
            if any(p.kind == inspect.Parameter.VAR_POSITIONAL
                   for p in parameters.values()):
                args = (system_prompt, request, timeout)
                positional = []
            else:
                positional = [p for p in parameters.values() if p.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            if positional:
                if len(positional) >= 3:
                    args = (system_prompt, request, timeout)
                else:
                    args = (system_prompt, request) if len(positional) >= 2 else (request,)
    else:
        args = (system_prompt, request)
    return call(*args, **kwargs)


def _call_with_timeout(call: Callable[..., Any], timeout: float,
                       system_prompt: str, request: Mapping[str, Any]) -> Any:
    """Invoke an injected seam with a hard wait bound.

    Seams commonly use ``(system_prompt, request)``, ``(request)`` or keyword
    arguments.  We inspect the signature once and never pass provider keys.
    """

    result: Queue[tuple[bool, Any]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            result.put_nowait((True, _invoke(
                call, system_prompt, request, timeout)))
        except Exception as exc:  # noqa: BLE001 - returned to proposal loop
            result.put_nowait((False, exc))

    # A daemon thread prevents a misbehaving injected/provider call from
    # keeping the research process alive after the configured timeout. Real
    # SDK calls also receive the same network timeout.
    worker = Thread(target=invoke, daemon=True, name="rule-proposal-provider")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"provider call exceeded {timeout:g}s")
    try:
        ok, value = result.get_nowait()
    except Empty as exc:
        raise RuntimeError("provider call returned no result") from exc
    if ok:
        return value
    raise value


@dataclass(frozen=True)
class ProposalResult:
    """Plain result returned by :meth:`RuleProposalAdapter.propose`."""

    success: bool
    error: str | None = None
    schema: str = PROPOSAL_SCHEMA
    rule_spec: dict[str, Any] | None = None
    variant_id: str | None = None
    spec_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    # Discovery proposals carry a one-sentence rationale.  It is recorded as
    # evidence and shown to operators; nothing reads it as an instruction.
    thesis: str | None = None
    # Tuning proposals carry one entry per variant: the normalized spec, its
    # content-addressed id, and the stated reason for the change.  The reason
    # is graded against that variant's gate afterwards, so it is evidence in
    # exactly the same sense as the thesis.
    variants: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.success


class RuleProposalAdapter:
    """Bounded provider adapter for ``llm-rule-proposal.v1`` proposals."""

    def __init__(self, provider: str = "openai", *, model: str = "",
                 caller: Callable[..., Any] | None = None,
                 system_prompt: str | None = None,
                 discovery_prompt: str | None = None,
                 tuning_prompt: str | None = None,
                 max_attempts: int = DEFAULT_ATTEMPTS,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
                 client: Any = None):
        provider = str(provider).lower().strip()
        if provider not in {"openai", "anthropic"}:
            raise ValueError("provider must be openai or anthropic")
        if max_attempts < 1 or max_attempts > 3:
            raise ValueError("max_attempts must be between 1 and 3")
        if (not math.isfinite(float(timeout_seconds)) or timeout_seconds <= 0 or
                timeout_seconds > 120):
            raise ValueError("timeout_seconds must be finite, positive, and <= 120")
        if max_response_bytes < 1_024 or max_response_bytes > 65_536:
            raise ValueError("max_response_bytes is outside its bounded range")
        self.provider = provider
        self.model = str(model)
        self.caller = caller
        self.client = client
        self.system_prompt = str(system_prompt or SYSTEM_PROMPT)
        self.discovery_prompt = str(discovery_prompt or DISCOVERY_SYSTEM_PROMPT)
        self.tuning_prompt = str(tuning_prompt or TUNING_SYSTEM_PROMPT)
        self.max_attempts = int(max_attempts)
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)

    def _lazy_client(self) -> Any:
        if self.client is not None:
            return self.client
        env_name = "OPENAI_API_KEY" if self.provider == "openai" else "ANTHROPIC_API_KEY"
        api_key = os.getenv(env_name)
        if not api_key:
            raise RuntimeError("LLM credentials are unavailable")
        if self.provider == "openai":
            from openai import OpenAI  # optional dependency, imported lazily
            kwargs = {"api_key": api_key, "max_retries": 0}
            base_url = os.getenv("OPENAI_BASE_URL")
            if base_url:
                kwargs["base_url"] = base_url
            self.client = OpenAI(**kwargs)
        else:
            from anthropic import Anthropic  # optional dependency
            kwargs = {"api_key": api_key, "max_retries": 0}
            base_url = os.getenv("ANTHROPIC_BASE_URL")
            if base_url:
                kwargs["base_url"] = base_url
            self.client = Anthropic(**kwargs)
        return self.client

    @staticmethod
    def _schema(name: str = PROPOSAL_SCHEMA) -> dict[str, Any]:
        # OpenAI Responses API JSON schema; Anthropic accepts the same schema
        # under ``output_config.format`` on versions supporting structured
        # outputs.  additionalProperties is deliberately false.
        if name == TUNING_SCHEMA:
            return {
                "type": "object", "additionalProperties": False,
                "required": ["schema", "variants"],
                "properties": {
                    "schema": {"type": "string", "const": name},
                    "variants": {
                        "type": "array", "minItems": 1,
                        "maxItems": MAX_TUNED_VARIANTS,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["rule_spec", "reason"],
                            "properties": {
                                "rule_spec": {"type": "object",
                                              "additionalProperties": True},
                                "reason": {"type": "string",
                                           "maxLength": MAX_REASON_CHARS},
                            }}}}}
        properties: dict[str, Any] = {
            "schema": {"type": "string", "const": name},
            "rule_spec": {"type": "object", "additionalProperties": True},
        }
        required = ["schema", "rule_spec"]
        if name == DISCOVERY_SCHEMA:
            properties["thesis"] = {"type": "string", "maxLength": MAX_THESIS_CHARS}
            required.append("thesis")
        return {"type": "object", "additionalProperties": False,
                "required": required, "properties": properties}

    def _provider_call(self, system_prompt: str,
                       request: Mapping[str, Any], timeout: float,
                       schema_name: str = PROPOSAL_SCHEMA) -> Any:
        if self.caller is not None:
            # The outer proposal loop applies the hard timeout. Keeping this
            # seam direct also makes fake callers easy to inspect in tests.
            return _invoke(self.caller, system_prompt, request, timeout)
        client = self._lazy_client()
        complete = getattr(client, "complete", None)
        if callable(complete):
            return _invoke(complete, system_prompt, request, timeout)
        request_text = canonical_json(request)
        if self.provider == "openai":
            # Current OpenAI Responses API structured output shape.
            response = client.responses.create(
                model=self.model, input=[
                    {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                    {"role": "user", "content": [{"type": "input_text", "text": request_text}]},
                ],
                text={"format": {"type": "json_schema", "name": "llm_rule_proposal",
                                  "strict": True,
                                  "schema": self._schema(schema_name)}},
                timeout=timeout,
            )
            return _raw_text(response)
        response = client.messages.create(
            model=self.model, max_tokens=1200, temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": request_text}],
            output_config={"format": {"type": "json_schema",
                                      "schema": self._schema(schema_name)}},
            timeout=timeout,
        )
        return _raw_text(response)

    def propose(self, vehicle: str, generation: int,
                prior_validated_rule_spec: Mapping[str, Any],
                diagnosis: Mapping[str, Any]) -> ProposalResult:
        """Request and validate one bounded replacement proposal."""

        try:
            if vehicle not in {"equity", "option"}:
                raise ValueError("vehicle must be equity or option")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                raise ValueError("generation must be a non-negative integer")
            prior = validate_rule_spec(_finite(prior_validated_rule_spec,
                                               path="prior_validated_rule_spec"))
            safe_diagnosis = _safe_diagnosis(diagnosis)
            request = {"vehicle": vehicle, "generation": generation,
                       "prior_validated_rule_spec": prior,
                       "diagnosis": safe_diagnosis}
            request_hash = content_hash(request)
            system_hash = content_hash(self.system_prompt)
        except Exception as exc:
            return ProposalResult(False, error=str(exc), evidence={
                "provider": self.provider,
                "model": self.model,
                "system_prompt_hash": content_hash(self.system_prompt),
            })

        errors: list[str] = []
        raw_hash: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw_value = _call_with_timeout(
                    self._provider_call, self.timeout_seconds,
                    self.system_prompt, request)
                # Hash the received representation even when strict parsing
                # subsequently rejects it.  Evidence never stores the raw
                # response itself.
                raw_hash = content_hash(_raw_text(raw_value))
                parsed, raw = _parse_response(raw_value,
                                              max_bytes=self.max_response_bytes)
                normalized = validate_rule_spec(parsed["rule_spec"])
                spec_hash = rule_spec_hash(normalized)
                variant = rule_variant_id(normalized)
                raw_hash = content_hash(raw)
                evidence = {
                    "provider": self.provider,
                    "model": self.model,
                    "system_prompt_hash": system_hash,
                    "request_hash": request_hash,
                    "raw_response_hash": raw_hash,
                    "normalized_spec_hash": spec_hash,
                    "spec_id": spec_hash,
                    "variant_id": variant,
                    "attempts": attempt,
                }
                return ProposalResult(True, schema=PROPOSAL_SCHEMA,
                                      rule_spec=normalized, variant_id=variant,
                                      spec_id=spec_hash, evidence=evidence)
            except Exception as exc:
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
        evidence = {"provider": self.provider, "model": self.model,
                    "system_prompt_hash": system_hash,
                    "request_hash": request_hash, "attempts": self.max_attempts}
        if raw_hash is not None:
            evidence["raw_response_hash"] = raw_hash
        return ProposalResult(False, error="; ".join(errors), evidence=evidence)

    def discover(self, vehicle: str, slot: int,
                 context: Mapping[str, Any]) -> ProposalResult:
        """Request one bounded *new* edge hypothesis for a free research slot.

        Discovery differs from :meth:`propose` only in what it is given and
        what it is asked for: no prior rule to repair, and an explicit brief to
        avoid what has already been tried or proved.  The output is validated
        by the same grammar and, once registered, faces the same gates as every
        other candidate, so a discovered edge is never trusted more than a
        deterministic one.
        """

        prompt = self.discovery_prompt
        try:
            if vehicle not in {"equity", "option"}:
                raise ValueError("vehicle must be equity or option")
            if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
                raise ValueError("slot must be a non-negative integer")
            safe_context = _safe_diagnosis(context, label="context")
            request = {"vehicle": vehicle, "slot": slot, "context": safe_context}
            request_hash = content_hash(request)
            system_hash = content_hash(prompt)
        except Exception as exc:
            return ProposalResult(False, error=str(exc),
                                  schema=DISCOVERY_SCHEMA,
                                  evidence={"provider": self.provider,
                                            "model": self.model,
                                            "kind": "discovery",
                                            "system_prompt_hash": content_hash(prompt)})

        errors: list[str] = []
        raw_hash: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw_value = _call_with_timeout(
                    self._discovery_call, self.timeout_seconds, prompt, request)
                raw_hash = content_hash(_raw_text(raw_value))
                parsed, raw = _parse_response(
                    raw_value, max_bytes=self.max_response_bytes,
                    schema=DISCOVERY_SCHEMA, keys=_DISCOVERY_RESPONSE_KEYS)
                normalized = validate_rule_spec(parsed["rule_spec"])
                thesis = _safe_thesis(parsed["thesis"])
                spec_hash = rule_spec_hash(normalized)
                variant = rule_variant_id(normalized)
                raw_hash = content_hash(raw)
                evidence = {
                    "provider": self.provider,
                    "model": self.model,
                    "kind": "discovery",
                    "system_prompt_hash": system_hash,
                    "request_hash": request_hash,
                    "raw_response_hash": raw_hash,
                    "normalized_spec_hash": spec_hash,
                    "spec_id": spec_hash,
                    "variant_id": variant,
                    "rule_schema": normalized["schema"],
                    "attempts": attempt,
                }
                return ProposalResult(True, schema=DISCOVERY_SCHEMA,
                                      rule_spec=normalized, variant_id=variant,
                                      spec_id=spec_hash, evidence=evidence,
                                      thesis=thesis)
            except Exception as exc:
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
        evidence = {"provider": self.provider, "model": self.model,
                    "kind": "discovery", "system_prompt_hash": system_hash,
                    "request_hash": request_hash, "attempts": self.max_attempts}
        if raw_hash is not None:
            evidence["raw_response_hash"] = raw_hash
        return ProposalResult(False, error="; ".join(errors),
                              schema=DISCOVERY_SCHEMA, evidence=evidence)

    def _discovery_call(self, system_prompt: str, request: Mapping[str, Any],
                        timeout: float) -> Any:
        # Keeps the three-argument seam ``_call_with_timeout`` introspects while
        # binding the discovery structured-output schema.
        return self._provider_call(system_prompt, request, timeout,
                                   DISCOVERY_SCHEMA)

    def tune(self, vehicle: str, slot: int, rule_spec: Mapping[str, Any],
             diagnosis: Mapping[str, Any], *, count: int,
             lessons: Any = ()) -> ProposalResult:
        """Request bounded *parameter* variants of one hypothesis, with reasons.

        This is the third request and the only one that changes numbers rather
        than structure.  It exists because the deterministic alternative is a
        fixed table: one hand-written response per diagnosed failure mode, with
        an arithmetic sweep behind it.  Tuning lets the search be driven by
        what earlier attempts actually taught, and requires the model to say
        *why* for each variant so that reason can be graded against the gate
        the variant subsequently earns.

        The family may not change — that is discovery's job, not tuning's —
        and every returned spec is normalized by the same grammar and faces
        exactly the same gates, so a tuned variant is never trusted more than
        a mutated one.
        """

        prompt = self.tuning_prompt
        try:
            if vehicle not in {"equity", "option"}:
                raise ValueError("vehicle must be equity or option")
            if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
                raise ValueError("slot must be a non-negative integer")
            if (isinstance(count, bool) or not isinstance(count, int) or
                    not 1 <= count <= MAX_TUNED_VARIANTS):
                raise ValueError(
                    f"count must be between 1 and {MAX_TUNED_VARIANTS}")
            root = validate_rule_spec(_finite(rule_spec, path="rule_spec"))
            request = {"vehicle": vehicle, "slot": slot,
                       "variants_requested": int(count),
                       "family": root["family"], "root_rule_spec": root,
                       "diagnosis": _safe_diagnosis(diagnosis),
                       "lessons": _safe_lessons(lessons)}
            request_hash = content_hash(request)
            system_hash = content_hash(prompt)
        except Exception as exc:
            return ProposalResult(False, error=str(exc), schema=TUNING_SCHEMA,
                                  evidence={"provider": self.provider,
                                            "model": self.model,
                                            "kind": "tuning",
                                            "system_prompt_hash": content_hash(prompt)})

        errors: list[str] = []
        raw_hash: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw_value = _call_with_timeout(
                    self._tuning_call, self.timeout_seconds, prompt, request)
                raw_hash = content_hash(_raw_text(raw_value))
                parsed, raw = _parse_response(
                    raw_value, max_bytes=self.max_response_bytes,
                    schema=TUNING_SCHEMA, keys=_TUNING_RESPONSE_KEYS,
                    spec_key=None)
                entries = _safe_tuned_variants(parsed["variants"], limit=count)
                variants: list[dict[str, Any]] = []
                seen: set[str] = set()
                for index, entry in enumerate(entries):
                    normalized = validate_rule_spec(entry["rule_spec"])
                    if normalized["family"] != root["family"]:
                        raise ValueError(
                            f"variants[{index}] changed family; tuning may not "
                            "replace the hypothesis")
                    variant = rule_variant_id(normalized)
                    # A repeated spec is a duplicate, not a contract breach:
                    # keep the first and let the caller top up the remainder.
                    if variant in seen:
                        continue
                    seen.add(variant)
                    variants.append({"rule_spec": normalized,
                                     "variant_id": variant,
                                     "reason": entry["reason"]})
                if not variants:
                    raise ValueError("tuning reply contained no usable variant")
                raw_hash = content_hash(raw)
                evidence = {
                    "provider": self.provider,
                    "model": self.model,
                    "kind": "tuning",
                    "system_prompt_hash": system_hash,
                    "request_hash": request_hash,
                    "raw_response_hash": raw_hash,
                    "root_variant_id": rule_variant_id(root),
                    "family": root["family"],
                    "requested": int(count),
                    "returned": len(variants),
                    "lessons_supplied": len(request["lessons"]),
                    "attempts": attempt,
                }
                return ProposalResult(True, schema=TUNING_SCHEMA,
                                      rule_spec=root, evidence=evidence,
                                      variants=tuple(variants))
            except Exception as exc:
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
        evidence = {"provider": self.provider, "model": self.model,
                    "kind": "tuning", "system_prompt_hash": system_hash,
                    "request_hash": request_hash,
                    "requested": int(count),
                    "lessons_supplied": len(request["lessons"]),
                    "attempts": self.max_attempts}
        if raw_hash is not None:
            evidence["raw_response_hash"] = raw_hash
        return ProposalResult(False, error="; ".join(errors),
                              schema=TUNING_SCHEMA, evidence=evidence)

    def _tuning_call(self, system_prompt: str, request: Mapping[str, Any],
                     timeout: float) -> Any:
        return self._provider_call(system_prompt, request, timeout,
                                   TUNING_SCHEMA)


# Friendly aliases for callers that prefer proposal-oriented naming.
LLMRuleProposalAdapter = RuleProposalAdapter
LLMStrategy = RuleProposalAdapter
RuleProposalResult = ProposalResult


def propose_rule(*args: Any, adapter: RuleProposalAdapter | None = None,
                 **kwargs: Any) -> ProposalResult:
    """Small functional seam for callers that do not need a long-lived adapter."""

    selected = adapter or RuleProposalAdapter()
    return selected.propose(*args, **kwargs)


def discover_rule(*args: Any, adapter: RuleProposalAdapter | None = None,
                  **kwargs: Any) -> ProposalResult:
    """Functional seam for one-shot discovery proposals."""

    selected = adapter or RuleProposalAdapter()
    return selected.discover(*args, **kwargs)


def tune_rule(*args: Any, adapter: RuleProposalAdapter | None = None,
              **kwargs: Any) -> ProposalResult:
    """Functional seam for one-shot parameter tuning proposals."""

    selected = adapter or RuleProposalAdapter()
    return selected.tune(*args, **kwargs)


__all__ = [
    "DISCOVERY_SCHEMA", "DISCOVERY_SYSTEM_PROMPT", "MAX_REASON_CHARS",
    "MAX_THESIS_CHARS", "MAX_TUNED_VARIANTS",
    "PROPOSAL_SCHEMA", "SYSTEM_PROMPT", "TUNING_SCHEMA", "TUNING_SYSTEM_PROMPT",
    "ProposalResult", "RuleProposalResult",
    "RuleProposalAdapter", "LLMRuleProposalAdapter", "LLMStrategy", "canonical_json",
    "content_hash", "discover_rule", "propose_rule", "tune_rule",
]
