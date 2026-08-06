"""Register hand-written pre-registrations into the staging store.

The authoring loop proposes mechanisms from what the evidence has killed.
This is the other entry point to the same store: a claim that came out of
analysis instead of out of the proposer, written down before it is measured
and kept in version control so the wording cannot drift after results exist.

Both paths land in the same table under the same immutability triggers and
the same ``T1_HYPOTHESIS`` tier, so nothing here grants authority that
authoring does not. The only difference is the ``author`` column, which is
what keeps a reviewed claim distinguishable from a proposed one.

Seeding is idempotent by design. A contract id that is already registered is
reported as such and skipped rather than treated as an error, because the
natural way to run this is on every deploy, and a second run must not need a
human to decide whether the failure mattered.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agent.contract_dsl import ContractProposalError
from agent.staging import StagingError, StagingStore

DEFAULT_SEED = (Path(__file__).resolve().parent / "staged" /
                "pre-registered.yaml")
# A seed entry states which of the two it is. Anything else is a typo, and a
# typo that silently means "do not register" would lose a claim quietly.
STATUSES = ("registered", "deferred")


class SeedError(ValueError):
    """The seed file is not usable as written."""


def load(path: str | Path | None = None) -> list[dict]:
    """Read the seed file into raw entries without validating the claims."""
    source = Path(path) if path is not None else DEFAULT_SEED
    if not source.is_file():
        raise SeedError(f"no seed file at {source}")
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SeedError(f"{source}: the seed file must be a mapping")
    entries = raw.get("contracts")
    if not isinstance(entries, list) or not entries:
        raise SeedError(f"{source}: contracts must be a non-empty list")
    out = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SeedError(f"{source}: contract {index} must be a mapping")
        status = entry.get("status")
        if status not in STATUSES:
            raise SeedError(
                f"{source}: contract {index} has status {status!r}; "
                f"expected one of {', '.join(STATUSES)}")
        if status == "deferred" and not str(
                entry.get("deferred_reason") or "").strip():
            # A deferred claim without a reason is indistinguishable from one
            # someone forgot to finish, and the next reader has to guess.
            raise SeedError(
                f"{source}: contract {index} is deferred with no "
                "deferred_reason")
        out.append(entry)
    return out


def seed(store: StagingStore, entries: list[dict], *,
         generation: int = 0, now: float | None = None) -> dict:
    """Register every ``registered`` entry, reporting what each one did.

    One malformed entry does not discard the rest, for the same reason the
    authoring path does not: the useful outcome of a partly broken batch is
    the part that works plus an exact statement of what did not.
    """
    registered, skipped, deferred, rejected = [], [], [], []
    for index, entry in enumerate(entries):
        contract_id = str(entry.get("contract_id") or f"<entry {index}>")
        if entry.get("status") == "deferred":
            deferred.append({
                "contract_id": contract_id,
                "reason": " ".join(str(entry.get("deferred_reason")).split()),
            })
            continue
        if store.contract(contract_id) is not None:
            # Already staged, including already retired. Re-registering would
            # need a new id anyway; the claim is immutable once written.
            skipped.append({"contract_id": contract_id,
                            "reason": "already registered"})
            continue
        proposal = {key: value for key, value in entry.items()
                    if key not in ("status", "deferred_reason")}
        try:
            contract = store.register(proposal, generation=generation, now=now)
        except (ContractProposalError, StagingError) as exc:
            rejected.append({"contract_id": contract_id, "reason": str(exc)})
            continue
        registered.append({
            "contract_id": contract.contract_id,
            "direction": contract.direction,
            "conditions": contract.describe(),
        })
    return {
        "generation": generation,
        "registered": registered,
        "skipped": skipped,
        "deferred": deferred,
        "rejected": rejected,
        "registered_count": len(registered),
        "rejected_count": len(rejected),
    }


def seed_from_file(store: StagingStore, path: str | Path | None = None,
                   *, generation: int = 0, now: float | None = None) -> dict:
    """Load and register in one step."""
    return seed(store, load(path), generation=generation, now=now)
