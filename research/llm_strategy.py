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
DEFAULT_RESPONSE_BYTES = 16_384
DEFAULT_ATTEMPTS = 2
DEFAULT_TIMEOUT_SECONDS = 20.0

# The prompt is part of the evidence fingerprint.  Keep it stable and make
# the output boundary explicit for providers that do not support JSON schema.
SYSTEM_PROMPT = """You propose bounded replacement rule strategies for an audited
research process.  Return one JSON object and nothing else, exactly:
{"schema":"llm-rule-proposal.v1","rule_spec":{...}}
The rule_spec must use only the finite rule-strategy.v1 grammar.  Never return
markdown, Python/source code, executable instructions, credentials, market
rows, or fields outside schema and rule_spec.
"""

_FORBIDDEN_KEYS = {
    "source", "code", "python", "javascript", "typescript", "shell",
    "exec", "execute", "eval", "command", "raw", "raw_rows", "rows",
    "market_rows", "market_data", "ohlcv", "api_key", "apikey", "token",
    "secret", "password", "credential", "credentials",
}
_RESPONSE_KEYS = frozenset(("schema", "rule_spec"))


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


def _safe_diagnosis(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("diagnosis must be an aggregate mapping")
    result = _finite(value, path="diagnosis")
    # A bounded aggregate diagnosis should not become an unbounded prompt.
    encoded = canonical_json(result).encode("utf-8")
    if len(encoded) > 8_192:
        raise ValueError("diagnosis exceeds the 8192-byte aggregate bound")
    return result


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


def _parse_response(value: Any, *, max_bytes: int) -> tuple[dict[str, Any], str]:
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
    unknown = set(parsed) - _RESPONSE_KEYS
    missing = _RESPONSE_KEYS - set(parsed)
    if unknown:
        raise ValueError(f"proposal response has unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"proposal response is missing field(s): {', '.join(sorted(missing))}")
    if parsed.get("schema") != PROPOSAL_SCHEMA:
        raise ValueError(f"proposal schema must be {PROPOSAL_SCHEMA!r}")
    if not isinstance(parsed.get("rule_spec"), Mapping):
        raise ValueError("proposal rule_spec must be an object")
    # Catch source/code keys before the rule validator's more general unknown
    # field error, preserving an explicit safety failure for callers.
    _finite(parsed["rule_spec"], path="rule_spec")
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

    @property
    def ok(self) -> bool:
        return self.success


class RuleProposalAdapter:
    """Bounded provider adapter for ``llm-rule-proposal.v1`` proposals."""

    def __init__(self, provider: str = "openai", *, model: str = "",
                 caller: Callable[..., Any] | None = None,
                 system_prompt: str | None = None,
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
    def _schema() -> dict[str, Any]:
        # OpenAI Responses API JSON schema; Anthropic accepts the same schema
        # under ``output_config.format`` on versions supporting structured
        # outputs.  additionalProperties is deliberately false.
        return {
            "type": "object", "additionalProperties": False,
            "required": ["schema", "rule_spec"],
            "properties": {
                "schema": {"type": "string", "const": PROPOSAL_SCHEMA},
                "rule_spec": {"type": "object", "additionalProperties": True},
            },
        }

    def _provider_call(self, system_prompt: str,
                       request: Mapping[str, Any], timeout: float) -> Any:
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
                                  "strict": True, "schema": self._schema()}},
                timeout=timeout,
            )
            return _raw_text(response)
        response = client.messages.create(
            model=self.model, max_tokens=1200, temperature=0,
            system=system_prompt,
            messages=[{"role": "user", "content": request_text}],
            output_config={"format": {"type": "json_schema", "schema": self._schema()}},
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


# Friendly aliases for callers that prefer proposal-oriented naming.
LLMRuleProposalAdapter = RuleProposalAdapter
LLMStrategy = RuleProposalAdapter
RuleProposalResult = ProposalResult


def propose_rule(*args: Any, adapter: RuleProposalAdapter | None = None,
                 **kwargs: Any) -> ProposalResult:
    """Small functional seam for callers that do not need a long-lived adapter."""

    selected = adapter or RuleProposalAdapter()
    return selected.propose(*args, **kwargs)


__all__ = [
    "PROPOSAL_SCHEMA", "SYSTEM_PROMPT", "ProposalResult", "RuleProposalResult",
    "RuleProposalAdapter", "LLMRuleProposalAdapter", "LLMStrategy", "canonical_json",
    "content_hash", "propose_rule",
]
