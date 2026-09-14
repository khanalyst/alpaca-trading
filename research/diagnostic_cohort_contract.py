"""Shared, fail-closed contract for the diagnostic shadow cohort.

The live producer, health projection, and session acceptance evidence all see
the same small catalog.  Keeping this validator independent of the runner
prevents a consumer from inferring readiness from a reported arm count.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.contracts.rule import RULE_FAMILIES


DIAGNOSTIC_CANDIDATE_PREFIX = "shadow:diagnostic:"
IBR_FAMILY = "ibr"
MAX_IDENTITY_LENGTH = 256
MAX_ARM_ROWS = 31

SUPPORTED_COHORT_LAYOUTS = {
    24: {"family_count": 12, "baseline_count": 12, "variant_count": 12},
    31: {"family_count": 13, "baseline_count": 13, "variant_count": 18},
}
KNOWN_FAMILIES = frozenset((*RULE_FAMILIES, IBR_FAMILY))


def _identity(value: Any) -> str | None:
    """Accept only one finite, exact, bounded identity string."""
    if not isinstance(value, str):
        return None
    if not value or len(value) > MAX_IDENTITY_LENGTH or value != value.strip():
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return None
    return value


def _candidate_identity(value: Any) -> str | None:
    """Return one bounded candidate identity.

    Namespace ownership remains a producer/live-shadow concern; this shared
    layout contract checks exact identity equality and uniqueness without
    inventing a second candidate-id grammar for read-only evidence fixtures.
    """
    return _identity(value)


def validate_cohort_layout(value: Any) -> dict[str, Any] | None:
    """Return a normalized supported cohort layout, or ``None``.

    Counts and candidate identities are calculated from the complete arm
    catalog.  Declared fields are accepted only when they exactly agree with
    those derived values; callers must never use an arbitrary reported count
    as a readiness denominator.
    """
    if not isinstance(value, Mapping):
        return None
    raw_arms = value.get("arms")
    if not isinstance(raw_arms, list) or not raw_arms or len(raw_arms) > MAX_ARM_ROWS:
        return None
    raw_candidates = value.get("candidate_ids")
    raw_identities = value.get("candidate_identities")
    if raw_candidates is None:
        raw_candidates = raw_identities
    if not isinstance(raw_candidates, list):
        return None
    if raw_identities is not None and (
            not isinstance(raw_identities, list) or
            raw_identities != raw_candidates):
        return None

    code_identity = _identity(value.get("code_identity"))
    cohort_identity = _identity(value.get("cohort_identity"))
    if code_identity is None or cohort_identity is None:
        return None

    arms: list[dict[str, str]] = []
    candidate_ids: list[str] = []
    variant_ids: set[str] = set()
    family_roles: dict[str, dict[str, int]] = {}
    arm_codes: set[str] = set()
    arm_cohorts: set[str] = set()
    for raw_arm in raw_arms:
        if not isinstance(raw_arm, Mapping):
            return None
        candidate_id = _candidate_identity(raw_arm.get("candidate_id"))
        family = _identity(raw_arm.get("family"))
        role = _identity(raw_arm.get("role"))
        variant_id = _identity(raw_arm.get("variant_id"))
        arm_code = _identity(raw_arm.get("code_identity"))
        arm_cohort = _identity(raw_arm.get("cohort_identity"))
        if (candidate_id is None or family not in KNOWN_FAMILIES or
                role not in {"baseline", "variant"} or variant_id is None or
                arm_code is None or arm_cohort is None):
            return None
        if candidate_id in candidate_ids or variant_id in variant_ids:
            return None
        candidate_ids.append(candidate_id)
        variant_ids.add(variant_id)
        roles = family_roles.setdefault(family, {})
        roles[role] = roles.get(role, 0) + 1
        arm_codes.add(arm_code)
        arm_cohorts.add(arm_cohort)
        arms.append({
            "candidate_id": candidate_id,
            "family": family,
            "role": role,
            "variant_id": variant_id,
            "code_identity": arm_code,
            "cohort_identity": arm_cohort,
        })

    arm_count = len(arms)
    expected = SUPPORTED_COHORT_LAYOUTS.get(arm_count)
    if expected is None:
        return None
    expected_families = set(RULE_FAMILIES)
    if arm_count == 31:
        expected_families.add(IBR_FAMILY)
    if set(family_roles) != expected_families:
        return None
    for family in RULE_FAMILIES:
        if family_roles.get(family) != {"baseline": 1, "variant": 1}:
            return None
    if arm_count == 31:
        if family_roles.get(IBR_FAMILY) != {"baseline": 1, "variant": 6}:
            return None
    elif IBR_FAMILY in family_roles:
        return None
    if (len(arm_codes) != 1 or code_identity not in arm_codes or
            len(arm_cohorts) != 1 or cohort_identity not in arm_cohorts):
        return None

    baseline_count = sum(roles.get("baseline", 0)
                         for roles in family_roles.values())
    variant_count = sum(roles.get("variant", 0)
                        for roles in family_roles.values())
    derived_counts = {
        "arm_count": arm_count,
        "family_count": len(family_roles),
        "baseline_count": baseline_count,
        "variant_count": variant_count,
    }
    declared_counts = {
        "arm_count": value.get("arm_count", arm_count),
        "family_count": value.get("family_count", value.get("families_total",
                                                               len(family_roles))),
        "baseline_count": value.get("baseline_count", baseline_count),
        "variant_count": value.get("variant_count", variant_count),
    }
    if any(isinstance(declared_counts[key], bool) or
           not isinstance(declared_counts[key], int) or
           declared_counts[key] != derived_counts[key]
           for key in declared_counts):
        return None
    # Both spellings may appear at a producer/consumer seam. A preferred
    # spelling must not hide a contradictory declared alias.
    if "families_total" in value and (
            isinstance(value["families_total"], bool) or
            not isinstance(value["families_total"], int) or
            value["families_total"] != derived_counts["family_count"]):
        return None
    if (derived_counts["family_count"] != expected["family_count"] or
            baseline_count != expected["baseline_count"] or
            variant_count != expected["variant_count"]):
        return None
    if (len(raw_candidates) != arm_count or
            any(_candidate_identity(candidate) is None for candidate in raw_candidates) or
            len(set(raw_candidates)) != arm_count or
            set(raw_candidates) != set(candidate_ids)):
        return None

    arms.sort(key=lambda arm: (arm["candidate_id"], arm["variant_id"]))
    candidate_ids = sorted(candidate_ids)
    return {
        **derived_counts,
        "candidate_ids": candidate_ids,
        "candidate_identities": list(candidate_ids),
        "arms": arms,
        "code_identity": code_identity,
        "cohort_identity": cohort_identity,
    }


__all__ = [
    "DIAGNOSTIC_CANDIDATE_PREFIX", "IBR_FAMILY", "KNOWN_FAMILIES",
    "SUPPORTED_COHORT_LAYOUTS", "validate_cohort_layout",
]
