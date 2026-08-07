"""Deterministic identity for staged mechanisms and their configurations.

The contract id is an operator-facing label.  It is deliberately not used as
novelty: changing a label or a scalar threshold must not make an old idea look
new.  Mechanism identity removes scalar configuration values and normalises
the explanatory claim; configuration identity retains the complete executable
claim.  Both are content addresses, not authority or registry identifiers.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from .contract_dsl import Condition, ProposedContract, validate

IDENTITY_SCHEMA = "staged_identity.v1"
_WORD = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "because", "by", "for",
    "from", "has", "have", "in", "into", "is", "it", "of", "on", "or",
    "that", "the", "their", "then", "they", "this", "to", "when", "who",
    "will", "with",
})


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False)


def _digest(prefix: str, value: object) -> str:
    raw = _canonical(value).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:24]}"


def _stem(word: str) -> str:
    # This is intentionally modest.  It catches cosmetic inflection without
    # pretending a deterministic store can solve unrestricted paraphrase.
    for suffix in ("ingly", "edly", "ation", "ments", "ment", "ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def semantic_terms(value: object) -> tuple[str, ...]:
    """Return order-independent content terms for deterministic comparison."""
    words = (_stem(word) for word in _WORD.findall(str(value or "").lower()))
    return tuple(sorted({word for word in words
                         if word not in _STOP_WORDS and not word.isdigit()}))


def _thaw(value):
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(item, (list, tuple)) and len(item) == 2
                         and isinstance(item[0], str) for item in value):
            return {str(item[0]): _thaw(item[1]) for item in value}
        return [_thaw(item) for item in value]
    return value


def condition_mapping(condition: Condition) -> dict:
    """Return the public proposal representation of one validated condition."""
    if condition.kind == "comparison":
        result = {
            "field": condition.field, "op": condition.op,
            "value": condition.value,
        }
    else:
        result = dict(_thaw(condition.args))
        result["primitive"] = condition.kind
    if condition.when_direction:
        result["when_direction"] = condition.when_direction
    return result


def proposal_mapping(contract: ProposedContract, *, contract_id: str | None = None) -> dict:
    """Render a validated contract back into an executable proposal mapping."""
    return {
        "contract_id": contract_id or contract.contract_id,
        "mechanism": contract.mechanism,
        "payer": contract.payer,
        "falsifier": contract.falsifier,
        "direction": contract.direction,
        "conditions": [condition_mapping(item) for item in contract.conditions],
        "author": contract.author,
        "notes": contract.notes,
    }


def _without_numbers(value):
    if isinstance(value, Mapping):
        return {str(key): _without_numbers(item)
                for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_without_numbers(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return "<number>"
    return str(value)


def mechanism_descriptor(proposal: ProposedContract | dict) -> dict:
    """Canonical mechanism semantics, excluding executable scalar settings."""
    contract = proposal if isinstance(proposal, ProposedContract) else validate(proposal)
    shapes = [_without_numbers(condition_mapping(item))
              for item in contract.conditions]
    return {
        "schema": IDENTITY_SCHEMA,
        "mechanism_terms": semantic_terms(contract.mechanism),
        "payer_terms": semantic_terms(contract.payer),
        "direction": contract.direction,
        # Conditions are conjunctions, so their source ordering is cosmetic.
        "condition_shapes": sorted(shapes, key=_canonical),
    }


def configuration_descriptor(proposal: ProposedContract | dict) -> dict:
    """Canonical executable claim, retaining all parameter values."""
    contract = proposal if isinstance(proposal, ProposedContract) else validate(proposal)
    return {
        "schema": IDENTITY_SCHEMA,
        "mechanism": " ".join(contract.mechanism.lower().split()),
        "payer": " ".join(contract.payer.lower().split()),
        "falsifier": " ".join(contract.falsifier.lower().split()),
        "direction": contract.direction,
        "conditions": sorted(
            [condition_mapping(item) for item in contract.conditions], key=_canonical),
    }


def mechanism_identity(proposal: ProposedContract | dict) -> str:
    return _digest("mech", mechanism_descriptor(proposal))


def configuration_identity(proposal: ProposedContract | dict) -> str:
    return _digest("cfg", configuration_descriptor(proposal))


def mechanism_similarity(
        left: ProposedContract | dict, right: ProposedContract | dict) -> float:
    """Bounded lexical/structural similarity used only as a novelty guard."""
    a, b = mechanism_descriptor(left), mechanism_descriptor(right)
    left_terms = set(a["mechanism_terms"]) | set(a["payer_terms"])
    right_terms = set(b["mechanism_terms"]) | set(b["payer_terms"])
    union = left_terms | right_terms
    lexical = len(left_terms & right_terms) / len(union) if union else 1.0
    structure = 1.0 if (a["direction"] == b["direction"]
                        and a["condition_shapes"] == b["condition_shapes"]) else 0.0
    return 0.8 * lexical + 0.2 * structure
