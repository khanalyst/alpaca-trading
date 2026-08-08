"""Content-addressed, non-authorizing handoff for supported staged mechanisms."""

from __future__ import annotations

import json
from collections.abc import Mapping

from .artifact import sha256_json

HANDOFF_SCHEMA = "staged_registration_handoff.v1"


class HandoffError(ValueError):
    """A staged result cannot produce or verify a registration handoff."""


def build_registration_handoff(
        staging_store, staged_review: Mapping, mechanism_id: str) -> dict:
    """Build a human-reviewable proposal; never write code or the registry."""
    mechanism_id = str(mechanism_id)
    mechanisms = {
        str(item.get("mechanism_id")): item
        for item in staged_review.get("mechanisms", [])
        if isinstance(item, Mapping)
    }
    mechanism = mechanisms.get(mechanism_id)
    if mechanism is None:
        raise HandoffError(f"mechanism {mechanism_id!r} was not reviewed")
    if mechanism.get("code") != "SUPPORTED":
        raise HandoffError("only a SUPPORTED mechanism can produce a handoff")

    supported_ids = {
        str(value) for value in mechanism.get(
            "supported_configuration_ids", [])
    }
    records = [
        row for row in staging_store.mechanism_records(mechanism_id)
        if str(row["configuration_id"]) in supported_ids
    ]
    if not records:
        raise HandoffError("supported mechanism has no attributable configuration")
    evidence = [
        {
            key: entry.get(key)
            for key in (
                "contract_id", "configuration_id", "variant_id", "scope_key",
                "stage", "code", "trades", "mean_r", "firing_rate",
                "coverage_pct", "pair_coverage_pct", "explanation")
        }
        for entry in staged_review.get("verdicts", [])
        if (isinstance(entry, Mapping)
            and str(entry.get("mechanism_id")) == mechanism_id
            and str(entry.get("configuration_id")) in supported_ids
            and entry.get("code") == "SUPPORTED")
    ]
    if not evidence:
        raise HandoffError("supported mechanism has no supporting verdict rows")

    unsigned = {
        "schema": HANDOFF_SCHEMA,
        "mechanism_id": mechanism_id,
        "reviewed_ts": staged_review.get("reviewed_ts"),
        "mechanism": mechanism.get("mechanism"),
        "payer": mechanism.get("payer"),
        "supported_configurations": [
            {
                "contract_id": row["contract_id"],
                "configuration_id": row["configuration_id"],
                "generation": row["generation"],
                "proposal": row["proposal"],
            }
            for row in sorted(records, key=lambda item: (
                item["configuration_id"], item["contract_id"]))
        ],
        "evidence": sorted(evidence, key=lambda item: (
            str(item.get("configuration_id")), str(item.get("scope_key")),
            str(item.get("variant_id")))),
        "requested_action": {
            "kind": "human_registration_review",
            "implementation_required": True,
            "registry_mutation_performed": False,
            "code_mutation_performed": False,
        },
        "authority": {
            "current_tier": "T1_HYPOTHESIS",
            "live_eligible": False,
            "human_review_required": True,
            "reviewed_content_addressed_packet_required": True,
            "approval_status": "PENDING_HUMAN_REVIEW",
        },
    }
    digest = sha256_json(unsigned)
    return {
        **unsigned,
        "content_sha256": digest,
        "handoff_id": f"sha256:{digest}",
    }


def validate_registration_handoff(value: object) -> dict:
    """Verify content identity and the non-authorizing safety declaration."""
    if not isinstance(value, Mapping):
        raise HandoffError("handoff must be an object")
    result = dict(value)
    if result.get("schema") != HANDOFF_SCHEMA:
        raise HandoffError("unsupported staged handoff schema")
    digest = result.pop("content_sha256", None)
    handoff_id = result.pop("handoff_id", None)
    expected = sha256_json(result)
    if digest != expected or handoff_id != f"sha256:{expected}":
        raise HandoffError("staged handoff content address does not match")
    authority = result.get("authority")
    requested = result.get("requested_action")
    if (not isinstance(authority, Mapping)
            or authority.get("live_eligible") is not False
            or authority.get("human_review_required") is not True
            or authority.get("approval_status") != "PENDING_HUMAN_REVIEW"):
        raise HandoffError("handoff weakens the human live-approval boundary")
    if (not isinstance(requested, Mapping)
            or requested.get("registry_mutation_performed") is not False
            or requested.get("code_mutation_performed") is not False):
        raise HandoffError("handoff claims an unauthorized mutation")
    return dict(value)


def render_registration_handoff(value: object) -> str:
    """Render the verified artifact as stable, review-friendly JSON."""
    valid = validate_registration_handoff(value)
    return json.dumps(valid, sort_keys=True, indent=2, ensure_ascii=True) + "\n"

