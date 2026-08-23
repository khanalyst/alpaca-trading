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
from threading import Lock, Thread
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from agent.contracts.rule import (RULE_SCHEMA_V1, RULE_SCHEMA_V2, RULE_SCHEMA_V3,
                                  rule_spec_hash, rule_variant_id,
                                  rule_spec_json_schema, validate_rule_spec)


PROPOSAL_SCHEMA = "llm-rule-proposal.v1"
DISCOVERY_SCHEMA = "llm-edge-discovery.v1"
TUNING_SCHEMA = "llm-variant-tuning.v1"
PREFLIGHT_SCHEMA = "llm-provider-preflight.v1"
DEFAULT_RESPONSE_BYTES = 16_384
DEFAULT_ATTEMPTS = 2
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_TOTAL_CALLS = 64
# Research proposals use one pinned sampling policy across providers.
RESEARCH_SAMPLING_TEMPERATURE = 0
# A tuning reply proposes the variants of one hypothesis, so it is bounded by
# the same ``MAX_VARIANTS`` the factory itself accepts.
MAX_TUNED_VARIANTS = 8
_TUNING_ZERO_AXIS_LIMITS = {
    "threshold_bps": 10.0,
    "min_atr_bps": 15.0,
    "entry_after_minutes": 60.0,
}
_AUTH_ERROR_TOKENS = ("authentication", "authorization", "unauthorized",
                      "forbidden", "invalid api key", "invalid_api_key",
                      "credentials are unavailable", "credential unavailable")
_PROVIDER_CONFIG_ERROR_TOKENS = (
    "deploymentnotfound", "deployment not found", "modelnotfound",
    "model not found", "invalid deployment", "unknown deployment",
)

# The prompt is part of the evidence fingerprint.  Keep it stable and make
# the output boundary explicit for providers that do not support JSON schema.
SYSTEM_PROMPT = """You propose bounded replacement rule strategies for an audited
research process.  Return one JSON object and nothing else, exactly:
{"schema":"llm-rule-proposal.v1","rule_spec":{...}}
The rule_spec must include every required key of the explicit grammar; schema
is never inferred. The request includes a vehicle: equity may use
rule-strategy.v1, rule-strategy.v2, or rule-strategy.v3 (with nullable
"breakeven_r" in v3), while options may use only executable v1/v2 and never
"breakeven_r". The vehicle-specific provider schema and validator are
authoritative.
All numeric bounds and enum values are exactly those shown in the provider JSON
schema. Never return
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
The rule_spec must use only the finite rule-strategy grammar. Set
"schema":"rule-strategy.v2" to use its wider entry predicates, which add
"confirmations" (a list of extra filters from trend/volume/volatility,
all of which must hold), "entry_after_minutes" and "entry_before_minutes"
(the minutes-from-09:30-New-York window in which a signal may fire), and
"min_atr_bps"/"max_atr_bps" (the volatility regime the rule is allowed to
trade). Use them to express a conditional edge, not just retuned numbers.
rule-strategy.v3 extends v2 with equity-only exit control via nullable
"breakeven_r". The request vehicle controls versions: equity may use v1/v2/v3;
options remain on executable v1/v2 and never use "breakeven_r". The
vehicle-specific provider schema and validator are authoritative.
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
TUNING_SYSTEM_PROMPT = """You tune the numeric parameters of one bounded
intraday rule strategy for an audited research process.  Return one JSON object
and nothing else, exactly:
{"schema":"llm-variant-tuning.v1","variants":[
  {"rule_spec":{...},"reason":"...","builds_on":"<lesson id>"}]}

WHAT YOU MAY CHANGE.  You are given a fully normalized root rule_spec. Return
the full normalized key set exactly (omitting a key is a contract failure).
Copy it and change only VALUES of its existing fields, within the grammar's
bounds. You may not add, remove or rename a field, you may not change "family",
and you may not change
"schema".  The signals themselves — the families, the confirmation filters, and
how each is computed from the bars — are fixed code that you are tuning, not
designing.  You cannot introduce a new signal, indicator or data source, and a
reply that tries to is rejected outright.  Changing which idea is being tested
is a different job, done elsewhere.
For an equity root using rule-strategy.v3, the nullable numeric
"breakeven_r" may be activated by changing null to one finite in-bounds number
as a single coordinate change. Options remain on their executable schema and
may never use rule-strategy.v3 or "breakeven_r".

EXPERIMENT PHASE.  The request names a refinement_phase.  In "coordinate"
phase every returned variant must change exactly ONE field, so its effect is
attributable and a useful value can be retained.  In "interaction" phase it
must change exactly TWO fields whose one-field lessons were already measured.
Never bundle extra changes. Numeric moves must remain inside the deterministic
local neighborhood: at most 20% of the root value (with the factory's declared
small step for a zero-valued axis). Confirmatory replays are performed without
you.

WHAT YOU MUST LEARN FROM.  You are given the diagnosis of how the root failed
on fit data only, and the graded lessons from earlier attempts: each lesson's
id, what was tried, the reason given, and what the gates then said.  Every
variant must set "builds_on" to the id of the lesson it is reasoning from, and
its "reason" must say what that lesson showed and what you are changing as a
result.  Do not re-propose parameter values a lesson already recorded as
failed.  When no lessons are supplied, omit "builds_on".

"reason" is one plain sentence, at most 240 characters, naming the parameter
you changed and the diagnosed problem it should fix, so it can be graded
against the result later.
Never return markdown, Python/source code, executable instructions,
credentials, market rows, or fields outside schema, variants, rule_spec,
reason and builds_on.
"""

# The preflight prompt intentionally asks for no strategy content.  The
# request still travels through the exact structured-output provider path used
# by proposal/discovery/tuning calls, which is what catches endpoint,
# deployment, and API-capability mismatches before a research cycle starts.
PREFLIGHT_SYSTEM_PROMPT = """Respond with exactly {"status":"ok"}.  This is a
connectivity probe, not a strategy request; do not return a rule, market data,
credentials, or instructions.  The response is ignored and never authorizes
research.
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
_TUNED_VARIANT_OPTIONAL_KEYS = frozenset(("builds_on",))
MAX_THESIS_CHARS = 240
MAX_REASON_CHARS = 240
# A lesson reference is the short prefix of its ledger id, which is what the
# brief carries.  Anything else is a fabricated citation.
LESSON_REF_CHARS = 12
_LESSON_REF = re.compile(r"^[0-9a-f]{%d}$" % LESSON_REF_CHARS)


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


def _validate_vehicle_rule_spec(spec: Mapping[str, Any], vehicle: str) -> None:
    """Apply the vehicle executable-schema gate after grammar validation."""

    if vehicle == "option" and spec.get("schema") == RULE_SCHEMA_V3:
        raise ValueError("rule-strategy.v3 is not executable for options")


def _tuning_reason_check(reason: str, root: Mapping[str, Any],
                         normalized: Mapping[str, Any],
                         diagnosis: Mapping[str, Any],
                         lessons: Sequence[Mapping[str, Any]]) -> None:
    """Require a tuning rationale to name its actual change and evidence."""
    changed = [str(key) for key in root
               if root.get(key) != normalized.get(key)]
    if not changed:
        raise ValueError("tuning reason has no changed field to justify")
    phase = str(diagnosis.get("refinement_phase") or "coordinate")
    expected = 2 if phase == "interaction" else 1
    if len(changed) != expected:
        raise ValueError(
            f"{phase} tuning must change exactly {expected} field(s); "
            f"changed {len(changed)}: {', '.join(changed)}")
    for key in changed:
        before = root.get(key)
        after = normalized.get(key)
        nullable_numeric_activation = (
            key == "breakeven_r" and root.get("schema") == RULE_SCHEMA_V3 and
            before is None and isinstance(after, (int, float)) and
            not isinstance(after, bool))
        if (not nullable_numeric_activation and
                (isinstance(before, bool) or isinstance(after, bool) or
                 not isinstance(before, (int, float)) or
                 not isinstance(after, (int, float)))):
            raise ValueError(
                f"tuning may change numeric values only; {key} is categorical")
        if nullable_numeric_activation:
            # ``None`` has no meaningful relative origin.  Match the
            # deterministic factory's first breakeven coordinate (one
            # quarter of target R), while retaining a bounded minimum step.
            target = float(root.get("target_r") or 0.0)
            # Keep a useful first trigger (0.5R) available for roots whose
            # target is below 2R, while still scaling with wider targets.
            limit = max(.5, target * .25)
            moved = abs(float(after))
        else:
            origin = float(before)
            moved = abs(float(after) - origin)
            if origin == 0.0:
                limit = _TUNING_ZERO_AXIS_LIMITS.get(
                    key, 1.0 if isinstance(before, int) else .25)
            elif isinstance(before, int):
                limit = float(max(1, int(round(abs(origin) * .2))))
            else:
                limit = max(.25, abs(origin) * .2)
        if moved > limit + 1e-12:
            raise ValueError(
                f"tuning change for {key} exceeds the bounded local step "
                f"({moved:g} > {limit:g})")
    text = reason.lower()
    missing_cues: list[str] = []
    for key in changed:
        field_cues = {key.lower(), key.lower().replace("_", " ")}
        field_cues.update(part.lower() for part in key.split("_")
                          if len(part) >= 4 and part.lower() not in {
                              "before", "after"})
        if not any(cue in text for cue in field_cues):
            missing_cues.append(key)
    if missing_cues:
        raise ValueError(
            "tuning reason must name every changed field: " +
            ", ".join(missing_cues))
    if lessons:
        # A cited id proves which lesson was selected; the prose must still
        # acknowledge that evidence rather than being an ungrounded guess.
        cues = {"lesson", "prior", "earlier", "cited", "failed", "passed",
                "attempt", "overshot", "showed", "diagnosis"}
        for value in diagnosis.values():
            cues.update(token for token in re.findall(r"[a-z0-9_]+",
                                                       str(value).lower())
                        if len(token) >= 5)
        for lesson in lessons:
            for value in lesson.values():
                cues.update(token for token in re.findall(r"[a-z0-9_]+",
                                                           str(value).lower())
                            if len(token) >= 5)
        if not any(cue in text for cue in cues):
            raise ValueError(
                "tuning reason must cite a lesson or diagnosed problem")


def _safe_error(exc: BaseException, *, limit: int = 240) -> str:
    """Describe a provider failure without persisting response/secret content."""
    message = " ".join(str(exc).split())
    message = re.sub(
        r"(?i)[\"']?(?:authorization|proxy-authorization|auth)[\"']?\s*[=:]\s*"
        r"[\"']?(?:(?:bearer|basic)\s+)?[^\"',;}\]]+",
        "authorization=<redacted>", message)
    # Provider SDKs use several common spellings, including prose such as
    # ``Incorrect API key provided: sk-...`` and dict-shaped error payloads.
    message = re.sub(
        r"(?i)[\"']?(?:x-api-key|api[ _-]?key|apikey|access[ _-]?token|token|secret|"
        r"password|credential)[\"']?(?:\s+provided)?\s*[=:]\s*[\"']?"
        r"[^\"'\s,;}\]]+",
        "credential=<redacted>", message)
    # Redact OpenAI-style key values even when the SDK omits a field label.
    message = re.sub(r"(?i)\bsk-[a-z0-9_-]{4,}\b", "sk-<redacted>", message)
    # SDK errors occasionally echo an endpoint URL (including basic-auth
    # userinfo or bearer/query credentials). Keep only the host/path shape.
    message = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@",
                     r"\1<redacted>@", message)
    message = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", message)
    # Signed provider URLs use both generic names (``sig``/``token``) and
    # cloud-specific names. Redact the complete value, including URL-safe
    # base64 and percent-encoded values, while preserving route/key names.
    message = re.sub(
        r"(?i)([?&](?:api[_-]?key|apikey|key|access[_-]?token|token|sig|signature|"
        r"secret|password|credential|auth|jwt|code|policy|expires|expiry|"
        r"x-amz-(?:signature|credential|security-token|securitytoken|expires|date)|"
        r"x-goog-(?:signature|credential|security-token)|"
        r"(?:awsaccesskeyid|googleaccessid|se|sp|sv|sr))=)[^&#\s]+",
        r"\1<redacted>", message)
    # Also cover SDK exceptions that print a query pair without the leading
    # URL delimiter (for example ``X-Amz-Signature=...`` on its own line).
    message = re.sub(
        r"(?i)((?:^|[\s;&])(?:x-amz-(?:signature|credential|security-token|"
        r"securitytoken|expires|date)|x-goog-(?:signature|credential|"
        r"security-token)|(?:awsaccesskeyid|googleaccessid))=)[^\s;&]+",
        r"\1<redacted>", message)
    return f"{type(exc).__name__}: {message[:limit]}"


def _safe_tuned_variants(value: Any, *, limit: int,
                         known_lessons: frozenset[str] = frozenset()
                         ) -> list[dict[str, Any]]:
    """Validate the ``variants`` list of a tuning reply, before any grammar.

    When the request supplied graded lessons, every variant must cite the one
    it reasoned from.  An uncited proposal is indistinguishable from a guess,
    and a citation naming a lesson that was never supplied is a fabricated
    one, so both are refused rather than recorded as learning that did not
    happen.
    """

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
        unknown = set(entry) - _TUNED_VARIANT_KEYS - _TUNED_VARIANT_OPTIONAL_KEYS
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
        builds_on = entry.get("builds_on")
        if known_lessons:
            if not isinstance(builds_on, str) or not _LESSON_REF.match(builds_on):
                raise ValueError(
                    f"variants[{index}].builds_on must cite one of the "
                    f"{len(known_lessons)} lesson id(s) supplied")
            if builds_on not in known_lessons:
                raise ValueError(
                    f"variants[{index}].builds_on cites unknown lesson "
                    f"{builds_on!r}")
        elif builds_on is not None:
            raise ValueError(
                f"variants[{index}].builds_on cites a lesson, but none were "
                "supplied")
        parsed.append({"rule_spec": dict(entry["rule_spec"]),
                       "reason": _safe_reason(entry["reason"]),
                       "builds_on": builds_on if known_lessons else None})
    return parsed


def _bounded_append(pieces: list[str], value: str, used: int,
                    max_bytes: int | None) -> int:
    """Append one text block without crossing the materialization budget."""
    encoded = value.encode("utf-8")
    if max_bytes is not None and used + len(encoded) > max_bytes:
        raise ValueError(f"provider response exceeds {max_bytes}-byte cap")
    pieces.append(value)
    return used + len(encoded)


def _bounded_json(value: Mapping[str, Any], max_bytes: int | None) -> str:
    """Serialize a mapping incrementally, before joining the complete text."""
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False,
                               default=_json_default)
    pieces: list[str] = []
    used = 0
    for chunk in encoder.iterencode(value):
        used = _bounded_append(pieces, chunk, used, max_bytes)
    return "".join(pieces)


def _raw_text(value: Any, *, max_bytes: int | None = None) -> str:
    """Extract bounded text from common SDK response shapes without trusting it."""

    if isinstance(value, str):
        pieces: list[str] = []
        _bounded_append(pieces, value, 0, max_bytes)
        return value
    if isinstance(value, Mapping):
        return _bounded_json(value, max_bytes)
    output_text = getattr(value, "output_text", None)
    if isinstance(output_text, str):
        pieces = []
        _bounded_append(pieces, output_text, 0, max_bytes)
        return output_text
    pieces: list[str] = []
    used = 0
    # Responses API: output -> content -> text.
    output = getattr(value, "output", None)
    if output:
        for item in output:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, Mapping):
                content = item.get("content")
            for block in content or ():
                text = getattr(block, "text", None)
                if text is None and isinstance(block, Mapping):
                    text = block.get("text")
                if isinstance(text, str):
                    used = _bounded_append(pieces, text, used, max_bytes)
        if pieces:
            return "".join(pieces)
    # Chat Completions and Anthropic Messages response shapes.
    choices = getattr(value, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        text = getattr(message, "content", None)
        if isinstance(text, str):
            _bounded_append(pieces, text, 0, max_bytes)
            return text
    content = getattr(value, "content", None)
    if content:
        pieces = []
        used = 0
        for block in content:
            text = getattr(block, "text", None)
            if text is None and isinstance(block, Mapping):
                text = block.get("text")
            if isinstance(text, str):
                used = _bounded_append(pieces, text, used, max_bytes)
        if pieces:
            return "".join(pieces)
    raise ValueError("provider response did not contain text")


def _parse_response(value: Any, *, max_bytes: int,
                    schema: str = PROPOSAL_SCHEMA,
                    keys: frozenset[str] = _RESPONSE_KEYS,
                    spec_key: str | None = "rule_spec"
                    ) -> tuple[dict[str, Any], str]:
    raw = _raw_text(value, max_bytes=max_bytes)
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


@dataclass(frozen=True)
class PreflightResult:
    """Safe, non-authorizing result from :meth:`RuleProposalAdapter.preflight`.
    """

    status: str
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    schema: str = "research-llm-preflight.v1"

    @property
    def ok(self) -> bool:
        return self.status == "ready"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"schema": self.schema, "status": self.status,
                                  "evidence": dict(self.evidence)}
        if self.reason:
            result["reason"] = self.reason
        return result


class RuleProposalAdapter:
    """Bounded provider adapter for ``llm-rule-proposal.v1`` proposals."""

    def __init__(self, provider: str = "openai", *, model: str = "",
                 deployment: str | None = None,
                 caller: Callable[..., Any] | None = None,
                 system_prompt: str | None = None,
                 discovery_prompt: str | None = None,
                 tuning_prompt: str | None = None,
                 max_attempts: int = DEFAULT_ATTEMPTS,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                 max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
                 max_total_calls: int = DEFAULT_TOTAL_CALLS,
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
        if isinstance(max_total_calls, bool) or not 1 <= int(max_total_calls) <= 256:
            raise ValueError("max_total_calls must be between 1 and 256")
        self.provider = provider
        self.model = str(model)
        self.deployment = (str(deployment).strip()
                           if deployment not in (None, "") else None)
        self.caller = caller
        self.client = client
        self.system_prompt = str(system_prompt or SYSTEM_PROMPT)
        self.discovery_prompt = str(discovery_prompt or DISCOVERY_SYSTEM_PROMPT)
        self.tuning_prompt = str(tuning_prompt or TUNING_SYSTEM_PROMPT)
        self.max_attempts = int(max_attempts)
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self.max_total_calls = int(max_total_calls)
        self._calls_used = 0
        self._call_lock = Lock()
        self._auth_unavailable = False
        self._auth_error: str | None = None
        self._provider_unavailable = False
        self._provider_error: str | None = None

    def _config_hash(self) -> str:
        """Hash non-secret adapter configuration for reproducible evidence."""
        return content_hash({"provider": self.provider, "model": self.model,
                             "deployment": self.deployment,
                             "temperature": RESEARCH_SAMPLING_TEMPERATURE,
                             "max_attempts": self.max_attempts,
                             "timeout_seconds": self.timeout_seconds,
                             "max_response_bytes": self.max_response_bytes,
                             "max_total_calls": self.max_total_calls})

    @staticmethod
    def _is_auth_error(exc: BaseException) -> bool:
        status = getattr(exc, "status_code", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            pass
        if status in {401, 403}:
            return True
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        return ("auth" in name or "credential" in name or
                any(token in message for token in _AUTH_ERROR_TOKENS))

    @staticmethod
    def _is_provider_config_error(exc: BaseException) -> bool:
        """Identify a missing model/deployment that retries cannot repair."""
        status = getattr(exc, "status_code", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            pass
        message = str(exc).lower()
        return status == 404 or any(
            token in message for token in _PROVIDER_CONFIG_ERROR_TOKENS)

    def _mark_auth_unavailable(self, exc: BaseException) -> None:
        if self._is_auth_error(exc):
            self._auth_unavailable = True
            self._auth_error = _safe_error(exc)

    def _mark_provider_unavailable(self, exc: BaseException) -> None:
        if self._is_provider_config_error(exc):
            self._provider_unavailable = True
            self._provider_error = _safe_error(exc)

    def _reserve_call(self) -> None:
        with self._call_lock:
            if self._auth_unavailable:
                raise RuntimeError("LLM authentication circuit is open")
            if self._provider_unavailable:
                raise RuntimeError(
                    "LLM provider configuration circuit is open; "
                    "verify the model/deployment name and base URL")
            if self._calls_used >= self.max_total_calls:
                raise RuntimeError("LLM total call budget exhausted")
            self._calls_used += 1

    def _budget_evidence(self) -> dict[str, Any]:
        with self._call_lock:
            used = self._calls_used
        return {"calls_used": used, "max_total_calls": self.max_total_calls,
                "calls_remaining": max(0, self.max_total_calls - used),
                "auth_circuit_open": self._auth_unavailable,
                "provider_circuit_open": self._provider_unavailable,
                **({"provider_error": self._provider_error}
                   if self._provider_error else {})}

    def _base_evidence(self, *, kind: str, schema_name: str,
                       prompt_hash: str | None = None,
                       request_hash: str | None = None,
                       vehicle: str | None = None) -> dict[str, Any]:
        evidence = {
            "provider": self.provider,
            "model": self.model,
            **({"deployment": self.deployment} if self.deployment else {}),
            "kind": kind,
            "config_hash": self._config_hash(),
            "response_schema_hash": self._schema_hash(schema_name, vehicle),
            # This remains the complete audited grammar fingerprint; the
            # vehicle-specific provider shape is captured separately by
            # ``response_schema_hash``.
            "grammar_schema_hash": content_hash(rule_spec_json_schema()),
        }
        if prompt_hash is not None:
            evidence["system_prompt_hash"] = prompt_hash
        if request_hash is not None:
            evidence["request_hash"] = request_hash
        evidence.update(self._budget_evidence())
        if self._auth_error:
            evidence["auth_error"] = self._auth_error
        return evidence

    def _provider_model(self) -> str:
        """Return the inference identifier without discarding catalog metadata."""
        return self.deployment or self.model

    @staticmethod
    def _azure_endpoint_configured() -> bool:
        base_url = os.getenv("OPENAI_BASE_URL", "").strip()
        if not base_url:
            return False
        try:
            host = (urlparse(base_url).hostname or "").lower()
        except ValueError:
            return False
        return host.endswith(".openai.azure.com") or host.endswith(".azure.com")

    def _attempt_evidence(self, *, attempt: int,
                          schema_name: str,
                          prompt_hash: str,
                          request_hash: str,
                          vehicle: str | None = None) -> dict[str, Any]:
        return {
            "attempt": int(attempt),
            "request_hash": request_hash,
            "system_prompt_hash": prompt_hash,
            "config_hash": self._config_hash(),
            "response_schema_hash": self._schema_hash(schema_name, vehicle),
            "grammar_schema_hash": content_hash(rule_spec_json_schema()),
        }

    def _schema_hash(self, name: str, vehicle: str | None = None) -> str:
        return content_hash(self._schema(name, vehicle=vehicle))

    def _lazy_client(self) -> Any:
        if self._auth_unavailable:
            raise RuntimeError("LLM authentication circuit is open")
        if self.client is not None:
            return self.client
        env_name = "OPENAI_API_KEY" if self.provider == "openai" else "ANTHROPIC_API_KEY"
        api_key = os.getenv(env_name)
        if not api_key:
            exc = RuntimeError("LLM credentials are unavailable")
            self._mark_auth_unavailable(exc)
            raise exc
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
    def _grammar_schema(vehicle: str | None = None) -> dict[str, Any]:
        """Return the provider-facing rule grammar for one vehicle.

        The full audited grammar remains the default for backwards
        compatibility. Options are intentionally narrowed to v1/v2 because
        v3's exit amendment is not executable in the option lane. Internal
        ``validate_rule_spec`` remains authoritative for bounds and ordering.
        """

        if vehicle == "option":
            return {"oneOf": [
                rule_spec_json_schema(RULE_SCHEMA_V1),
                rule_spec_json_schema(RULE_SCHEMA_V2),
            ]}
        return rule_spec_json_schema()

    @staticmethod
    def _schema(name: str = PROPOSAL_SCHEMA,
                vehicle: str | None = None) -> dict[str, Any]:
        # OpenAI Responses API JSON schema; Anthropic accepts the same schema
        # under ``output_config.format`` on versions supporting structured
        # outputs.  additionalProperties is deliberately false.
        # Provider structured-output dialects implement a deliberately small
        # JSON Schema subset.  In particular, OpenAI strict schemas reject
        # validation-only keywords such as ``minimum`` and ``pattern`` with a
        # 400 rather than simply ignoring them.  The provider schema is only a
        # shape hint: ``validate_rule_spec``/``_safe_text``/``_parse_response``
        # remain the authoritative bounds and response gates below.
        provider_keys = frozenset((
            "type", "properties", "required", "items",
            "additionalProperties", "enum", "anyOf",
        ))

        def provider_schema(value: Any) -> Any:
            if isinstance(value, Mapping):
                result: dict[str, Any] = {}
                type_values: list[Any] | None = None
                for key, item in value.items():
                    # Keep only the portable strict-output vocabulary.  This
                    # deny-by-default handling also strips all currently
                    # known validation/annotation keywords (minimum/maximum,
                    # minItems/maxItems, pattern/format, contains,
                    # unevaluated*, and their relatives) if the grammar grows.
                    if key not in provider_keys and key not in {"oneOf", "const"}:
                        continue
                    if key == "const":
                        # Structured-output providers accept singleton enums
                        # more consistently than JSON Schema ``const``.
                        result["enum"] = [provider_schema(item)]
                        continue
                    if key == "oneOf":
                        result["anyOf"] = provider_schema(item)
                        continue
                    if key == "properties":
                        # Property names are data, not schema keywords, so
                        # they must not pass through the allowlist above.
                        result[key] = {
                            str(property_name): provider_schema(property_schema)
                            for property_name, property_schema in item.items()
                        }
                        continue
                    if key == "type" and isinstance(item, (list, tuple)):
                        # Nullable properties are represented as a union in
                        # the provider dialect.  Keeping the enclosing
                        # object's ``required`` list unchanged means a
                        # nullable field is still always present.
                        type_values = list(item)
                        continue
                    result[key] = provider_schema(item)
                if type_values is not None:
                    nullable = [{"type": provider_schema(item)}
                                for item in type_values]
                    # A type-array and oneOf are not emitted by the audited
                    # grammar together.  If a future schema does combine
                    # them, retain both alternatives rather than silently
                    # dropping one of the provider-safe branches.
                    existing = result.get("anyOf")
                    if existing is not None:
                        nullable.extend(existing)
                    result["anyOf"] = nullable
                return result
            if isinstance(value, list):
                return [provider_schema(item) for item in value]
            return value

        if name == PREFLIGHT_SCHEMA:
            return provider_schema({
                "type": "object", "additionalProperties": False,
                "required": ["status"],
                "properties": {
                    "status": {"type": "string", "enum": ["ok"]},
                },
            })

        rule_schema = provider_schema(RuleProposalAdapter._grammar_schema(vehicle))
        if name == TUNING_SCHEMA:
            return provider_schema({
                "type": "object", "additionalProperties": False,
                "required": ["schema", "variants"],
                "properties": {
                    "schema": {"type": "string", "const": name},
                    "variants": {
                        "type": "array", "minItems": 1,
                        "maxItems": MAX_TUNED_VARIANTS,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["rule_spec", "reason", "builds_on"],
                            "properties": {
                                "rule_spec": rule_schema,
                                "reason": {"type": "string",
                                           "maxLength": MAX_REASON_CHARS},
                                # Nullable: the first cycle has no lesson to
                                # cite.  Structured-output modes require every
                                # property to be listed, so it is required and
                                # nullable rather than optional.
                                "builds_on": {"type": ["string", "null"],
                                              "maxLength": LESSON_REF_CHARS},
                            }}}}})
        properties: dict[str, Any] = {
            "schema": {"type": "string", "const": name},
            "rule_spec": rule_schema,
        }
        required = ["schema", "rule_spec"]
        if name == DISCOVERY_SCHEMA:
            properties["thesis"] = {"type": "string", "maxLength": MAX_THESIS_CHARS}
            required.append("thesis")
        return provider_schema({"type": "object", "additionalProperties": False,
                                "required": required, "properties": properties})

    def _provider_call(self, system_prompt: str,
                       request: Mapping[str, Any], timeout: float,
                       schema_name: str = PROPOSAL_SCHEMA,
                       *, preflight: bool = False) -> Any:
        self._reserve_call()
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
            format_name = ("llm_provider_preflight" if preflight else
                           "llm_rule_proposal")
            response = client.responses.create(
                model=self._provider_model(), input=[
                    {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                    {"role": "user", "content": [{"type": "input_text", "text": request_text}]},
                ],
                text={"format": {"type": "json_schema", "name": format_name,
                                      "strict": True,
                                      "schema": self._schema(
                                          schema_name,
                                          vehicle=request.get("vehicle"))}},
                temperature=RESEARCH_SAMPLING_TEMPERATURE,
                **({"max_output_tokens": 32} if preflight else {}),
                timeout=timeout,
            )
            # Connectivity probes intentionally do not inspect/model-parse
            # the response body; a successful HTTP/API return is sufficient.
            return response if preflight else _raw_text(
                response, max_bytes=self.max_response_bytes)
        response = client.messages.create(
            model=self._provider_model(), max_tokens=(32 if preflight else 1200),
            temperature=RESEARCH_SAMPLING_TEMPERATURE,
            system=system_prompt,
            messages=[{"role": "user", "content": request_text}],
            output_config={"format": {"type": "json_schema",
                                      "schema": self._schema(
                                          schema_name,
                                          vehicle=request.get("vehicle"))}},
            timeout=timeout,
        )
        return response if preflight else _raw_text(
            response, max_bytes=self.max_response_bytes)

    @staticmethod
    def _preflight_status(exc: BaseException) -> str:
        """Classify one provider failure without leaking response contents."""
        if RuleProposalAdapter._is_auth_error(exc):
            return "fatal"
        status = getattr(exc, "status_code", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
        # Every non-auth 4xx is a configuration/capability problem.  A 408
        # request timeout and 429 rate limit are explicitly transient.
        if status is not None:
            if status == 404 or 400 <= status < 500 and status not in {408, 429}:
                return "fatal"
            if status == 408 or status == 429 or status >= 500:
                return "degraded"
        message = str(exc).lower()
        name = type(exc).__name__.lower()
        if (isinstance(exc, (TimeoutError, OSError, ConnectionError)) or
                any(token in message or token in name for token in (
                    "timeout", "timed out", "network", "connection", "dns",
                    "temporar", "unavailable", "retry"))):
            return "degraded"
        # Missing optional SDKs and malformed provider setup are fatal
        # configuration errors. Unknown exceptions are conservatively treated
        # as degraded so a transient SDK/network issue never authorizes a
        # misleading ready state.
        if isinstance(exc, (ImportError, ValueError)) or any(
                token in message for token in ("endpoint", "deployment", "capability")):
            return "fatal"
        return "degraded"

    def preflight(self) -> PreflightResult:
        """Probe the configured provider exactly once, without parsing output.

        The call uses the same ``_provider_call`` path as all model-assisted
        research.  Its tiny request/output budget and ignored response make
        the operation connectivity-only and non-authorizing.
        """
        request = {"vehicle": "equity", "purpose": "connectivity_preflight"}
        request_hash = content_hash(request)
        prompt_hash = content_hash(PREFLIGHT_SYSTEM_PROMPT)
        evidence = self._base_evidence(
            kind="preflight", schema_name=PREFLIGHT_SCHEMA,
            prompt_hash=prompt_hash, request_hash=request_hash, vehicle="equity")
        evidence.update({"attempts": 1, "request_kind": "minimal",
                         "response_parsed": False})
        if (self.provider == "openai" and self._azure_endpoint_configured()
                and not self.deployment):
            reason = ("fatal provider configuration failure: Azure OpenAI base URL "
                      "requires research.strategy_llm.deployment")
            evidence.update({"error_type": "ConfigurationError",
                             "azure_deployment_required": True})
            return PreflightResult(status="fatal", reason=reason,
                                   evidence=evidence)
        try:
            # ``_call_with_timeout`` supplies the same hard timeout boundary as
            # proposal calls.  No retry loop is intentionally present here.
            _call_with_timeout(
                lambda system_prompt, request, timeout: self._provider_call(
                    system_prompt, request, timeout, PREFLIGHT_SCHEMA,
                    preflight=True),
                self.timeout_seconds, PREFLIGHT_SYSTEM_PROMPT, request)
        except Exception as exc:  # noqa: BLE001 - classify safely below
            self._mark_auth_unavailable(exc)
            self._mark_provider_unavailable(exc)
            status = self._preflight_status(exc)
            safe = _safe_error(exc)
            evidence.update({"error": safe, "error_type": type(exc).__name__})
            status_code = getattr(exc, "status_code", None)
            if isinstance(status_code, int):
                evidence["status_code"] = status_code
            # Capture circuit evidence after marking the failure.
            evidence.update(self._budget_evidence())
            reason = ("fatal provider configuration/authentication failure: "
                      if status == "fatal" else "transient provider failure: ") + safe
            return PreflightResult(status=status, reason=reason, evidence=evidence)
        evidence.update(self._budget_evidence())
        evidence["response_observed"] = True
        return PreflightResult(status="ready", evidence=evidence)

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
            _validate_vehicle_rule_spec(prior, vehicle)
            safe_diagnosis = _safe_diagnosis(diagnosis)
            request = {"vehicle": vehicle, "generation": generation,
                       "prior_validated_rule_spec": prior,
                       "diagnosis": safe_diagnosis}
            request_hash = content_hash(request)
            system_hash = content_hash(self.system_prompt)
        except Exception as exc:
            return ProposalResult(
                False, error=str(exc),
                evidence=self._base_evidence(
                    kind="proposal", schema_name=PROPOSAL_SCHEMA,
                    prompt_hash=content_hash(self.system_prompt), vehicle=vehicle))

        errors: list[str] = []
        raw_hash: str | None = None
        attempt_evidence: list[dict[str, Any]] = []
        for attempt in range(1, self.max_attempts + 1):
            attempt_record = self._attempt_evidence(
                attempt=attempt, schema_name=PROPOSAL_SCHEMA,
                prompt_hash=system_hash, request_hash=request_hash,
                vehicle=vehicle)
            try:
                raw_value = _call_with_timeout(
                    self._provider_call, self.timeout_seconds,
                    self.system_prompt, request)
                # Hash the received representation even when strict parsing
                # subsequently rejects it.  Evidence never stores the raw
                # response itself.
                raw = _raw_text(raw_value, max_bytes=self.max_response_bytes)
                raw_hash = content_hash(raw)
                attempt_record["response_hash"] = raw_hash
                parsed, raw = _parse_response(raw,
                                              max_bytes=self.max_response_bytes)
                normalized = validate_rule_spec(parsed["rule_spec"])
                _validate_vehicle_rule_spec(normalized, vehicle)
                spec_hash = rule_spec_hash(normalized)
                variant = rule_variant_id(normalized)
                attempt_record["normalized_spec_hash"] = spec_hash
                attempt_record["variant_id"] = variant
                attempt_evidence.append(attempt_record)
                evidence = {
                    **self._base_evidence(
                        kind="proposal", schema_name=PROPOSAL_SCHEMA,
                        prompt_hash=system_hash, request_hash=request_hash,
                        vehicle=vehicle),
                    "raw_response_hash": raw_hash,
                    "normalized_spec_hash": spec_hash,
                    "spec_id": spec_hash,
                    "variant_id": variant,
                    "attempts": attempt,
                    "attempt_errors": errors[-3:],
                    "attempt_evidence": attempt_evidence,
                }
                return ProposalResult(True, schema=PROPOSAL_SCHEMA,
                                      rule_spec=normalized, variant_id=variant,
                                      spec_id=spec_hash, evidence=evidence)
            except Exception as exc:
                safe = _safe_error(exc)
                attempt_record["error"] = safe
                attempt_evidence.append(attempt_record)
                errors.append(f"attempt {attempt}: {safe}")
                self._mark_auth_unavailable(exc)
                self._mark_provider_unavailable(exc)
                if (self._auth_unavailable or self._provider_unavailable or
                        "call budget" in str(exc).lower()):
                    break
        evidence = self._base_evidence(
            kind="proposal", schema_name=PROPOSAL_SCHEMA,
            prompt_hash=system_hash, request_hash=request_hash, vehicle=vehicle)
        evidence["attempts"] = len(attempt_evidence)
        if raw_hash is not None:
            evidence["raw_response_hash"] = raw_hash
        evidence["attempt_errors"] = errors[-3:]
        evidence["attempt_evidence"] = attempt_evidence
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
            return ProposalResult(
                False, error=str(exc), schema=DISCOVERY_SCHEMA,
                evidence=self._base_evidence(
                    kind="discovery", schema_name=DISCOVERY_SCHEMA,
                    prompt_hash=content_hash(prompt), vehicle=vehicle))

        errors: list[str] = []
        raw_hash: str | None = None
        attempt_evidence: list[dict[str, Any]] = []
        for attempt in range(1, self.max_attempts + 1):
            attempt_record = self._attempt_evidence(
                attempt=attempt, schema_name=DISCOVERY_SCHEMA,
                prompt_hash=system_hash, request_hash=request_hash,
                vehicle=vehicle)
            try:
                raw_value = _call_with_timeout(
                    self._discovery_call, self.timeout_seconds, prompt, request)
                raw = _raw_text(raw_value, max_bytes=self.max_response_bytes)
                raw_hash = content_hash(raw)
                attempt_record["response_hash"] = raw_hash
                parsed, raw = _parse_response(
                    raw, max_bytes=self.max_response_bytes,
                    schema=DISCOVERY_SCHEMA, keys=_DISCOVERY_RESPONSE_KEYS)
                normalized = validate_rule_spec(parsed["rule_spec"])
                _validate_vehicle_rule_spec(normalized, vehicle)
                thesis = _safe_thesis(parsed["thesis"])
                spec_hash = rule_spec_hash(normalized)
                variant = rule_variant_id(normalized)
                attempt_record["normalized_spec_hash"] = spec_hash
                attempt_record["variant_id"] = variant
                attempt_evidence.append(attempt_record)
                evidence = {
                    **self._base_evidence(
                        kind="discovery", schema_name=DISCOVERY_SCHEMA,
                        prompt_hash=system_hash, request_hash=request_hash,
                        vehicle=vehicle),
                    "raw_response_hash": raw_hash,
                    "normalized_spec_hash": spec_hash,
                    "spec_id": spec_hash,
                    "variant_id": variant,
                    "rule_schema": normalized["schema"],
                    "attempts": attempt,
                    "attempt_errors": errors[-3:],
                    "attempt_evidence": attempt_evidence,
                }
                return ProposalResult(True, schema=DISCOVERY_SCHEMA,
                                      rule_spec=normalized, variant_id=variant,
                                      spec_id=spec_hash, evidence=evidence,
                                      thesis=thesis)
            except Exception as exc:
                safe = _safe_error(exc)
                attempt_record["error"] = safe
                attempt_evidence.append(attempt_record)
                errors.append(f"attempt {attempt}: {safe}")
                self._mark_auth_unavailable(exc)
                self._mark_provider_unavailable(exc)
                if (self._auth_unavailable or self._provider_unavailable or
                        "call budget" in str(exc).lower()):
                    break
        evidence = self._base_evidence(
            kind="discovery", schema_name=DISCOVERY_SCHEMA,
            prompt_hash=system_hash, request_hash=request_hash, vehicle=vehicle)
        evidence["attempts"] = len(attempt_evidence)
        if raw_hash is not None:
            evidence["raw_response_hash"] = raw_hash
        evidence["attempt_errors"] = errors[-3:]
        evidence["attempt_evidence"] = attempt_evidence
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
            _validate_vehicle_rule_spec(root, vehicle)
            safe_lessons = _safe_lessons(lessons)
            known_lessons = frozenset(
                str(item["id"]) for item in safe_lessons
                if isinstance(item, Mapping) and item.get("id"))
            request = {"vehicle": vehicle, "slot": slot,
                       "variants_requested": int(count),
                       "family": root["family"], "rule_schema": root["schema"],
                       "root_rule_spec": root,
                       "diagnosis": _safe_diagnosis(diagnosis),
                       "lessons": safe_lessons}
            request_hash = content_hash(request)
            system_hash = content_hash(prompt)
        except Exception as exc:
            return ProposalResult(
                False, error=str(exc), schema=TUNING_SCHEMA,
                evidence=self._base_evidence(
                    kind="tuning", schema_name=TUNING_SCHEMA,
                    prompt_hash=content_hash(prompt), vehicle=vehicle))

        errors: list[str] = []
        raw_hash: str | None = None
        attempt_evidence: list[dict[str, Any]] = []
        for attempt in range(1, self.max_attempts + 1):
            attempt_record = self._attempt_evidence(
                attempt=attempt, schema_name=TUNING_SCHEMA,
                prompt_hash=system_hash, request_hash=request_hash,
                vehicle=vehicle)
            try:
                raw_value = _call_with_timeout(
                    self._tuning_call, self.timeout_seconds, prompt, request)
                raw = _raw_text(raw_value, max_bytes=self.max_response_bytes)
                raw_hash = content_hash(raw)
                attempt_record["response_hash"] = raw_hash
                parsed, raw = _parse_response(
                    raw, max_bytes=self.max_response_bytes,
                    schema=TUNING_SCHEMA, keys=_TUNING_RESPONSE_KEYS,
                    spec_key=None)
                entries = _safe_tuned_variants(parsed["variants"], limit=count,
                                               known_lessons=known_lessons)
                variants: list[dict[str, Any]] = []
                seen: set[str] = set()
                for index, entry in enumerate(entries):
                    try:
                        normalized = validate_rule_spec(entry["rule_spec"])
                        _validate_vehicle_rule_spec(normalized, vehicle)
                    except Exception:
                        # Preserve the grammar validator's precise contract
                        # (unknown fields, unsupported family, or bad bounds)
                        # before applying the stricter tuning key-set policy.
                        raise
                    if normalized["family"] != root["family"]:
                        raise ValueError(
                            f"variants[{index}] changed family; tuning may not "
                            "replace the hypothesis's signal")
                    if normalized["schema"] != root["schema"]:
                        raise ValueError(
                            f"variants[{index}] changed schema from "
                            f"{root['schema']!r}; tuning may not widen the grammar")
                    # Normalization may fill omitted grammar defaults. Tuning
                    # is stricter: the provider must echo the complete root
                    # key set, with family and schema explicitly pinned.
                    raw_keys = set(entry["rule_spec"])
                    root_keys = set(root)
                    if raw_keys != root_keys:
                        missing = sorted(root_keys - raw_keys)
                        extra = sorted(raw_keys - root_keys)
                        detail = []
                        if missing:
                            detail.append("missing " + ", ".join(missing))
                        if extra:
                            detail.append("unknown " + ", ".join(extra))
                        raise ValueError(
                            f"variants[{index}].rule_spec must echo the full "
                            f"normalized root key set ({'; '.join(detail)})")
                    if "family" not in entry["rule_spec"] or "schema" not in entry["rule_spec"]:
                        raise ValueError(
                            f"variants[{index}].rule_spec must explicitly pin family and schema")
                    _tuning_reason_check(
                        entry["reason"], root, normalized, diagnosis,
                        safe_lessons)
                    # The signal primitives are fixed code.  Tuning changes the
                    # values inside one of them; it may not swap the family for
                    # another, and it may not raise the grammar version, which
                    # would unlock whole categories of predicate the root was
                    # never expressed in.  Either is a new signal, not a tune.
                    variant = rule_variant_id(normalized)
                    # A repeated spec is a duplicate, not a contract breach:
                    # keep the first and let the caller top up the remainder.
                    if variant in seen:
                        continue
                    seen.add(variant)
                    variants.append({"rule_spec": normalized,
                                     "variant_id": variant,
                                     "reason": entry["reason"],
                                     "builds_on": entry["builds_on"]})
                if not variants:
                    raise ValueError("tuning reply contained no usable variant")
                attempt_record["normalized_spec_hash"] = content_hash(
                    {item["variant_id"]: item["rule_spec"] for item in variants})
                attempt_evidence.append(attempt_record)
                evidence = {
                    **self._base_evidence(
                        kind="tuning", schema_name=TUNING_SCHEMA,
                        prompt_hash=system_hash, request_hash=request_hash,
                        vehicle=vehicle),
                    "raw_response_hash": raw_hash,
                    "root_variant_id": rule_variant_id(root),
                    "family": root["family"],
                    "rule_schema": root["schema"],
                    "requested": int(count),
                    "returned": len(variants),
                    "lessons_supplied": len(safe_lessons),
                    "lessons_cited": sorted(
                        {entry["builds_on"] for entry in variants
                         if entry["builds_on"]}),
                    "attempts": attempt,
                    "attempt_errors": errors[-3:],
                    "attempt_evidence": attempt_evidence,
                }
                return ProposalResult(True, schema=TUNING_SCHEMA,
                                      rule_spec=root, evidence=evidence,
                                      variants=tuple(variants))
            except Exception as exc:
                safe = _safe_error(exc)
                attempt_record["error"] = safe
                attempt_evidence.append(attempt_record)
                errors.append(f"attempt {attempt}: {safe}")
                self._mark_auth_unavailable(exc)
                self._mark_provider_unavailable(exc)
                if (self._auth_unavailable or self._provider_unavailable or
                        "call budget" in str(exc).lower()):
                    break
        evidence = self._base_evidence(
            kind="tuning", schema_name=TUNING_SCHEMA,
            prompt_hash=system_hash, request_hash=request_hash, vehicle=vehicle)
        evidence.update({
            "requested": int(count),
            "lessons_supplied": len(safe_lessons),
            "attempts": len(attempt_evidence),
        })
        if raw_hash is not None:
            evidence["raw_response_hash"] = raw_hash
        evidence["attempt_errors"] = errors[-3:]
        evidence["attempt_evidence"] = attempt_evidence
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
    "DEFAULT_TOTAL_CALLS", "DISCOVERY_SCHEMA", "DISCOVERY_SYSTEM_PROMPT", "LESSON_REF_CHARS",
    "MAX_REASON_CHARS", "MAX_THESIS_CHARS", "MAX_TUNED_VARIANTS",
    "PREFLIGHT_SCHEMA", "PROPOSAL_SCHEMA", "PREFLIGHT_SYSTEM_PROMPT",
    "RESEARCH_SAMPLING_TEMPERATURE", "SYSTEM_PROMPT", "TUNING_SCHEMA",
    "TUNING_SYSTEM_PROMPT",
    "ProposalResult", "PreflightResult", "RuleProposalResult",
    "RuleProposalAdapter", "LLMRuleProposalAdapter", "LLMStrategy", "canonical_json",
    "content_hash", "discover_rule", "propose_rule", "tune_rule",
]
