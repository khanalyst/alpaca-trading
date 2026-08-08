"""Where an authored mechanism lives until evidence promotes or kills it.

A proposed contract must be testable the day it is written and must never be
one edit away from capital. Those two requirements pull in opposite
directions, so they are separated here: staging owns registration and shadow
exposure, and the existing tier ladder owns authority.

Everything registered through this module enters at ``T1_HYPOTHESIS``. Live
eligibility requires ``T3_VALIDATED`` and a content-addressed reviewed packet,
neither of which staging can grant, so an authored mechanism reaching real
capital still needs the same human signature every other strategy needs. What
staging removes is the developer in the middle of *measuring* it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .contract_dsl import (ContractProposalError, ProposedContract,
                           compile_contract, validate)
from .staged_identity import (configuration_identity, mechanism_identity,
                              mechanism_similarity, proposal_mapping)

# Staged mechanisms enter here and can rise no further without the reviewed
# packet path. The constant is duplicated deliberately rather than imported:
# a future edit to the registry's ladder must not silently raise the tier a
# machine-authored contract is born at.
STAGING_TIER = "T1_HYPOTHESIS"
SCHEMA = 2
DEFAULT_MAX_ACTIVE = 32
_ID = re.compile(r"\A[a-z0-9][a-z0-9._-]{2,63}\Z")


class StagingError(RuntimeError):
    """The staging store refused a write."""


class StagingCapacityError(StagingError):
    """The configured active-lane budget has no room for another contract."""


class StagingNoveltyError(StagingError):
    """A strict registration repeats an existing claim or configuration."""


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        conn.close()
        raise
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS staged_contracts (
            contract_id     TEXT PRIMARY KEY,
            generation      INTEGER NOT NULL,
            author          TEXT NOT NULL,
            tier            TEXT NOT NULL,
            mechanism       TEXT NOT NULL,
            payer           TEXT NOT NULL,
            falsifier       TEXT NOT NULL,
            direction       TEXT NOT NULL,
            conditions_json TEXT NOT NULL,
            notes           TEXT NOT NULL DEFAULT '',
            registered_ts   REAL NOT NULL,
            retired_ts      REAL,
            retired_reason  TEXT,
            mechanism_id    TEXT,
            configuration_id TEXT,
            parent_contract_id TEXT,
            refinement_attempt INTEGER NOT NULL DEFAULT 0
        )""")
    columns = {str(row[1]) for row in conn.execute(
        "PRAGMA table_info(staged_contracts)").fetchall()}
    additions = {
        "mechanism_id": "TEXT",
        "configuration_id": "TEXT",
        "parent_contract_id": "TEXT",
        "refinement_attempt": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE staged_contracts ADD COLUMN {name} {declaration}")

    # Existing rows predate the identity split.  Backfill from their immutable
    # claims; no executable or retirement data is rewritten.
    for row in conn.execute(
            "SELECT * FROM staged_contracts WHERE mechanism_id IS NULL "
            "OR configuration_id IS NULL").fetchall():
        proposal = {
            "contract_id": row["contract_id"],
            "mechanism": row["mechanism"],
            "payer": row["payer"],
            "falsifier": row["falsifier"],
            "direction": row["direction"],
            "conditions": json.loads(row["conditions_json"]),
            "author": row["author"],
            "notes": row["notes"],
        }
        contract = validate(proposal)
        conn.execute(
            "UPDATE staged_contracts SET mechanism_id=?, configuration_id=? "
            "WHERE contract_id=?",
            (mechanism_identity(contract), configuration_identity(contract),
             row["contract_id"]))
    # A registered claim is identity, not a draft. Editing one after results
    # exist makes a retro-fitted hypothesis indistinguishable from a
    # pre-registered one, which is the failure this whole framework exists to
    # prevent, so the claim columns are immutable at the database level.
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS staged_contracts_claims_immutable
        BEFORE UPDATE OF mechanism, payer, falsifier, direction,
                         conditions_json, generation, author, registered_ts
        ON staged_contracts
        BEGIN
            SELECT RAISE(ABORT,
                'a staged contract claim is immutable; register a new id');
        END""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS staged_contracts_no_delete
        BEFORE DELETE ON staged_contracts
        BEGIN
            SELECT RAISE(ABORT,
                'staged contracts are append-only; retire instead of deleting');
        END""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS staged_contracts_identity_immutable
        BEFORE UPDATE OF mechanism_id, configuration_id, parent_contract_id,
                         refinement_attempt
        ON staged_contracts
        BEGIN
            SELECT RAISE(ABORT,
                'a staged contract identity is immutable; register a new id');
        END""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS staged_contracts_mechanism "
        "ON staged_contracts(mechanism_id, registered_ts, contract_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS staged_contracts_configuration "
        "ON staged_contracts(configuration_id)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS staging_meta (key TEXT PRIMARY KEY, "
        "value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO staging_meta VALUES ('schema', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA),))
    conn.commit()


class StagingStore:
    """Append-only registry of machine-authored candidate mechanisms."""

    def __init__(self, path: str | Path, *,
                 max_active: int = DEFAULT_MAX_ACTIVE):
        if (isinstance(max_active, bool) or not isinstance(max_active, int)
                or max_active < 1):
            raise StagingError("max_active must be a positive integer")
        self.path = Path(path)
        self.max_active = max_active
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(self.path)) as conn:
            _migrate(conn)

    def register(self, proposal: object, *, generation: int,
                 now: float | None = None) -> ProposedContract:
        """Validate, compile and persist one authored mechanism.

        Compilation happens before the write on purpose: a contract that
        cannot execute must never occupy an identity, because a registered id
        that never fires is indistinguishable in the evidence from one that
        fired and lost.
        """
        return self._register(
            proposal, generation=generation, now=now, strict_novelty=False,
            parent_contract_id=None, refinement_attempt=0,
            mechanism_id_override=None)

    def register_bounded(self, proposal: object, *, generation: int,
                         now: float | None = None,
                         novelty_threshold: float = 0.84) -> ProposedContract:
        """Register a genuinely novel root mechanism under the lane budget.

        Unlike the legacy-compatible :meth:`register`, this entry point
        rejects an already-seen executable configuration and a semantically
        equivalent mechanism even when the caller changes ``contract_id``.
        Explicit variants must use :meth:`register_variant`.
        """
        return self._register(
            proposal, generation=generation, now=now, strict_novelty=True,
            parent_contract_id=None, refinement_attempt=0,
            mechanism_id_override=None,
            novelty_threshold=novelty_threshold)

    def register_variant(
            self, proposal: object, *, mechanism_id: str,
            parent_contract_id: str, generation: int,
            refinement_attempt: int = 0,
            now: float | None = None) -> ProposedContract:
        """Register one preregistered configuration of an existing mechanism."""
        parent = self.record(parent_contract_id)
        if parent is None:
            raise StagingError(f"parent contract {parent_contract_id!r} is unknown")
        if str(parent["mechanism_id"]) != str(mechanism_id):
            raise StagingError("variant mechanism_id does not match its parent")
        candidate = validate(proposal)
        if mechanism_identity(candidate) != str(mechanism_id):
            raise StagingError(
                "variant changes mechanism semantics; register it as a novel "
                "root instead of attaching it to this family")
        if (isinstance(refinement_attempt, bool)
                or not isinstance(refinement_attempt, int)
                or refinement_attempt < 0):
            raise StagingError("refinement_attempt must be a non-negative integer")
        return self._register(
            candidate, generation=generation, now=now, strict_novelty=False,
            reject_configuration_duplicate=True,
            parent_contract_id=str(parent_contract_id),
            refinement_attempt=refinement_attempt,
            mechanism_id_override=str(mechanism_id))

    def register_batch(
            self, root_proposal: object, variants: list[object] | tuple[object, ...], *,
            generation: int, now: float | None = None,
            novelty_threshold: float = 0.84) -> list[ProposedContract]:
        """Register a root and its initial configuration family atomically.

        Initial neighborhoods are one logical registration: publishing the
        root without all of its children leaves a family that cannot be
        interpreted as pre-registered.  Validate and compile every proposal
        before opening the transaction, then perform the capacity, novelty,
        identity, and inserts on one connection so any failure rolls back the
        complete family.
        """
        if not isinstance(variants, (list, tuple)):
            raise StagingError("variants must be a list or tuple")
        root = (root_proposal if isinstance(root_proposal, ProposedContract)
                else validate(root_proposal))
        contracts = [
            (item if isinstance(item, ProposedContract) else validate(item))
            for item in variants
        ]
        all_contracts = [root, *contracts]
        if not isinstance(generation, int) or generation < 0:
            raise StagingError("generation must be a non-negative integer")
        mechanism_id_value = mechanism_identity(root)
        for contract in all_contracts:
            if not _ID.match(contract.contract_id):
                raise ContractProposalError(
                    f"contract_id {contract.contract_id!r} must be 3-64 lowercase "
                    "alphanumerics, '.', '-' or '_'")
            compile_contract(contract)
            if contract is not root and mechanism_identity(contract) != mechanism_id_value:
                raise StagingError(
                    "batch child changes mechanism semantics; register it as "
                    "a separate root")
        ids = [contract.contract_id for contract in all_contracts]
        if len(ids) != len(set(ids)):
            raise StagingError("batch contains duplicate contract_id values")
        timestamp = time.time() if now is None else float(now)
        rows = []
        for index, contract in enumerate(all_contracts):
            rows.append({
                "contract_id": contract.contract_id,
                "generation": generation,
                "author": contract.author,
                "tier": STAGING_TIER,
                "mechanism": contract.mechanism,
                "payer": contract.payer,
                "falsifier": contract.falsifier,
                "direction": contract.direction,
                "conditions_json": json.dumps(
                    [asdict(condition) for condition in contract.conditions],
                    sort_keys=True, separators=(",", ":")),
                "notes": contract.notes,
                "registered_ts": timestamp,
                "mechanism_id": mechanism_id_value,
                "configuration_id": configuration_identity(contract),
                "parent_contract_id": None if index == 0 else root.contract_id,
                "refinement_attempt": 0,
            })

        try:
            with closing(_connect(self.path)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                active = int(conn.execute(
                    "SELECT COUNT(*) FROM staged_contracts "
                    "WHERE retired_ts IS NULL").fetchone()[0])
                if active + len(rows) > self.max_active:
                    raise StagingCapacityError(
                        f"active staged-contract cap {self.max_active} reached; "
                        "review, retire, or raise the configured budget")

                root_configuration = rows[0]["configuration_id"]
                duplicate = conn.execute(
                    "SELECT contract_id FROM staged_contracts "
                    "WHERE configuration_id=? ORDER BY registered_ts LIMIT 1",
                    (root_configuration,)).fetchone()
                if duplicate is not None:
                    raise StagingNoveltyError(
                        f"configuration repeats {duplicate['contract_id']!r}; "
                        "changing contract_id is not a new test")
                conflicts = self._novelty_conflicts(
                    conn, root, novelty_threshold=novelty_threshold)
                if conflicts:
                    first = conflicts[0]
                    raise StagingNoveltyError(
                        f"mechanism is not novel relative to "
                        f"{first['contract_id']!r} "
                        f"(similarity {first['similarity']:.3f}); use an "
                        "explicit bounded variant/refinement instead")

                seen_configurations = set()
                for row in rows:
                    configuration_id = row["configuration_id"]
                    if configuration_id in seen_configurations:
                        raise StagingNoveltyError(
                            "batch contains duplicate executable configurations")
                    seen_configurations.add(configuration_id)
                    duplicate = conn.execute(
                        "SELECT contract_id FROM staged_contracts "
                        "WHERE configuration_id=? ORDER BY registered_ts LIMIT 1",
                        (configuration_id,)).fetchone()
                    if duplicate is not None:
                        raise StagingNoveltyError(
                            f"configuration repeats {duplicate['contract_id']!r}; "
                            "changing contract_id is not a new test")
                    conn.execute(
                        "INSERT INTO staged_contracts (contract_id, generation, "
                        "author, tier, mechanism, payer, falsifier, direction, "
                        "conditions_json, notes, registered_ts, mechanism_id, "
                        "configuration_id, parent_contract_id, refinement_attempt) "
                        "VALUES (:contract_id,:generation,:author,:tier,:mechanism,"
                        ":payer,:falsifier,:direction,:conditions_json,:notes,"
                        ":registered_ts,:mechanism_id,:configuration_id,"
                        ":parent_contract_id,:refinement_attempt)", row)
                conn.commit()
        except (StagingCapacityError, StagingNoveltyError):
            raise
        except sqlite3.IntegrityError as exc:
            raise StagingError("staged registration batch was rejected") from exc
        return all_contracts

    def _register(
            self, proposal: object, *, generation: int,
            now: float | None, strict_novelty: bool,
            parent_contract_id: str | None, refinement_attempt: int,
            mechanism_id_override: str | None,
            reject_configuration_duplicate: bool = False,
            novelty_threshold: float = 0.84) -> ProposedContract:
        contract = (proposal if isinstance(proposal, ProposedContract)
                    else validate(proposal))
        if not _ID.match(contract.contract_id):
            raise ContractProposalError(
                f"contract_id {contract.contract_id!r} must be 3-64 lowercase "
                "alphanumerics, '.', '-' or '_'")
        if not isinstance(generation, int) or generation < 0:
            raise StagingError("generation must be a non-negative integer")
        compile_contract(contract)
        timestamp = time.time() if now is None else float(now)
        inferred_mechanism_id = mechanism_identity(contract)
        mechanism_id_value = mechanism_id_override or inferred_mechanism_id
        configuration_id_value = configuration_identity(contract)
        row = {
            "contract_id": contract.contract_id,
            "generation": generation,
            "author": contract.author,
            "tier": STAGING_TIER,
            "mechanism": contract.mechanism,
            "payer": contract.payer,
            "falsifier": contract.falsifier,
            "direction": contract.direction,
            "conditions_json": json.dumps(
                [asdict(condition) for condition in contract.conditions],
                sort_keys=True, separators=(",", ":")),
            "notes": contract.notes,
            "registered_ts": timestamp,
            "mechanism_id": mechanism_id_value,
            "configuration_id": configuration_id_value,
            "parent_contract_id": parent_contract_id,
            "refinement_attempt": refinement_attempt,
        }
        try:
            with closing(_connect(self.path)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                active = int(conn.execute(
                    "SELECT COUNT(*) FROM staged_contracts "
                    "WHERE retired_ts IS NULL").fetchone()[0])
                if active >= self.max_active:
                    raise StagingCapacityError(
                        f"active staged-contract cap {self.max_active} reached; "
                        "review, retire, or raise the configured budget")
                if strict_novelty or reject_configuration_duplicate:
                    duplicate = conn.execute(
                        "SELECT contract_id FROM staged_contracts "
                        "WHERE configuration_id=? ORDER BY registered_ts LIMIT 1",
                        (configuration_id_value,)).fetchone()
                    if duplicate is not None:
                        raise StagingNoveltyError(
                            f"configuration repeats {duplicate['contract_id']!r}; "
                            "changing contract_id is not a new test")
                if strict_novelty:
                    conflicts = self._novelty_conflicts(
                        conn, contract, novelty_threshold=novelty_threshold)
                    if conflicts:
                        first = conflicts[0]
                        raise StagingNoveltyError(
                            f"mechanism is not novel relative to "
                            f"{first['contract_id']!r} "
                            f"(similarity {first['similarity']:.3f}); use an "
                            "explicit bounded variant/refinement instead")
                conn.execute(
                    "INSERT INTO staged_contracts (contract_id, generation, "
                    "author, tier, mechanism, payer, falsifier, direction, "
                    "conditions_json, notes, registered_ts, mechanism_id, "
                    "configuration_id, parent_contract_id, "
                    "refinement_attempt) VALUES "
                    "(:contract_id,:generation,:author,:tier,:mechanism,"
                    ":payer,:falsifier,:direction,:conditions_json,:notes,"
                    ":registered_ts,:mechanism_id,:configuration_id,"
                    ":parent_contract_id,:refinement_attempt)", row)
                conn.commit()
        except (StagingCapacityError, StagingNoveltyError):
            raise
        except sqlite3.IntegrityError as exc:
            raise StagingError(
                f"contract_id {contract.contract_id!r} is already "
                "registered; a changed claim needs a new id") from exc
        return contract

    def _novelty_conflicts(
            self, conn: sqlite3.Connection, contract: ProposedContract, *,
            novelty_threshold: float) -> list[dict]:
        if not 0.0 <= float(novelty_threshold) <= 1.0:
            raise StagingError("novelty_threshold must be between zero and one")
        conflicts = []
        for row in conn.execute(
                "SELECT * FROM staged_contracts ORDER BY registered_ts, "
                "contract_id").fetchall():
            existing = self._row_to_contract(row)
            similarity = mechanism_similarity(contract, existing)
            if (str(row["mechanism_id"]) == mechanism_identity(contract)
                    or similarity >= novelty_threshold):
                conflicts.append({
                    "contract_id": str(row["contract_id"]),
                    "mechanism_id": str(row["mechanism_id"]),
                    "similarity": similarity,
                    "retired": row["retired_ts"] is not None,
                })
        return sorted(conflicts, key=lambda item: (
            -item["similarity"], item["contract_id"]))

    def novelty_conflicts(self, proposal: object, *,
                          novelty_threshold: float = 0.84) -> list[dict]:
        """Report prior semantic claims without mutating the staging store."""
        contract = validate(proposal)
        with closing(_connect(self.path)) as conn:
            return self._novelty_conflicts(
                conn, contract, novelty_threshold=novelty_threshold)

    def _row_to_contract(self, row: sqlite3.Row) -> ProposedContract:
        return validate({
            "contract_id": row["contract_id"],
            "mechanism": row["mechanism"],
            "payer": row["payer"],
            "falsifier": row["falsifier"],
            "direction": row["direction"],
            "conditions": json.loads(row["conditions_json"]),
            "author": row["author"],
            "notes": row["notes"],
        })

    def active(self) -> list[ProposedContract]:
        """Every staged mechanism still eligible for a shadow lane."""
        with closing(_connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT * FROM staged_contracts WHERE retired_ts IS NULL "
                "ORDER BY generation ASC, contract_id ASC").fetchall()
        return [self._row_to_contract(row) for row in rows]

    def contract(self, contract_id: str) -> ProposedContract | None:
        with closing(_connect(self.path)) as conn:
            row = conn.execute(
                "SELECT * FROM staged_contracts WHERE contract_id=?",
                (str(contract_id),)).fetchone()
        return self._row_to_contract(row) if row is not None else None

    def _row_to_record(self, row: sqlite3.Row) -> dict:
        contract = self._row_to_contract(row)
        return {
            "contract_id": contract.contract_id,
            "mechanism_id": str(row["mechanism_id"]),
            "configuration_id": str(row["configuration_id"]),
            "parent_contract_id": row["parent_contract_id"],
            "refinement_attempt": int(row["refinement_attempt"] or 0),
            "generation": int(row["generation"]),
            "tier": str(row["tier"]),
            "registered_ts": float(row["registered_ts"]),
            "retired_ts": (float(row["retired_ts"])
                           if row["retired_ts"] is not None else None),
            "retired_reason": row["retired_reason"],
            "proposal": proposal_mapping(contract),
        }

    def record(self, contract_id: str) -> dict | None:
        with closing(_connect(self.path)) as conn:
            row = conn.execute(
                "SELECT * FROM staged_contracts WHERE contract_id=?",
                (str(contract_id),)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def records(self, *, active_only: bool | None = None) -> list[dict]:
        where = (" WHERE retired_ts IS NULL" if active_only is True
                 else " WHERE retired_ts IS NOT NULL" if active_only is False
                 else "")
        with closing(_connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT * FROM staged_contracts" + where
                + " ORDER BY registered_ts, contract_id").fetchall()
        return [self._row_to_record(row) for row in rows]

    def mechanism_records(self, mechanism_id: str) -> list[dict]:
        return [row for row in self.records()
                if row["mechanism_id"] == str(mechanism_id)]

    def capacity(self) -> dict:
        active = len(self.active())
        return {
            "max_active": self.max_active,
            "active": active,
            "available": max(0, self.max_active - active),
            "backpressured": active >= self.max_active,
        }

    def ensure_capacity(self, slots: int = 1) -> None:
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 1:
            raise StagingError("slots must be a positive integer")
        state = self.capacity()
        if state["available"] < slots:
            raise StagingCapacityError(
                f"staging requires {slots} free slots but only "
                f"{state['available']} of {state['max_active']} are available")

    def retired(self) -> list[dict]:
        """Retired mechanisms with the reason they were retired for.

        A proposer told only that something failed will restate it with a
        different threshold, which is the same claim wearing a different
        number, so the claim and the reason travel together.
        """
        with closing(_connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT contract_id, mechanism, payer, falsifier, generation, "
                "retired_ts, retired_reason, mechanism_id, configuration_id, "
                "parent_contract_id, refinement_attempt FROM staged_contracts "
                "WHERE retired_ts IS NOT NULL "
                "ORDER BY retired_ts ASC").fetchall()
        return [dict(row) for row in rows]

    def evaluators(self) -> dict[str, object]:
        """Compiled callables keyed by contract id, ready for a shadow lane."""
        return {contract.contract_id: compile_contract(contract)
                for contract in self.active()}

    def retire(self, contract_id: str, reason: str,
               now: float | None = None) -> None:
        """Stop scheduling a mechanism without erasing what it claimed."""
        reason = " ".join(str(reason or "").split())
        if not reason:
            raise StagingError("a retirement reason is required")
        timestamp = time.time() if now is None else float(now)
        with closing(_connect(self.path)) as conn:
            changed = conn.execute(
                "UPDATE staged_contracts SET retired_ts=?, retired_reason=? "
                "WHERE contract_id=? AND retired_ts IS NULL",
                (timestamp, reason, str(contract_id))).rowcount
            conn.commit()
        if not changed:
            raise StagingError(
                f"contract_id {contract_id!r} is unknown or already retired")

    def generation(self) -> int:
        """The highest generation registered so far, or -1 when empty."""
        with closing(_connect(self.path)) as conn:
            row = conn.execute(
                "SELECT MAX(generation) AS latest FROM staged_contracts"
            ).fetchone()
        return -1 if row["latest"] is None else int(row["latest"])

    def summary(self) -> dict:
        with closing(_connect(self.path)) as conn:
            rows = conn.execute(
                "SELECT tier, retired_ts IS NULL AS live, COUNT(*) n "
                "FROM staged_contracts GROUP BY tier, live").fetchall()
        return {
            "active": sum(r["n"] for r in rows if r["live"]),
            "retired": sum(r["n"] for r in rows if not r["live"]),
            "tiers": sorted({r["tier"] for r in rows}),
            "max_active": self.max_active,
            "available": max(
                0, self.max_active
                - sum(r["n"] for r in rows if r["live"])),
        }
