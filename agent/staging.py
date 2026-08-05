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
from dataclasses import asdict
from pathlib import Path

from .contract_dsl import (ContractProposalError, ProposedContract,
                           compile_contract, validate)

# Staged mechanisms enter here and can rise no further without the reviewed
# packet path. The constant is duplicated deliberately rather than imported:
# a future edit to the registry's ladder must not silently raise the tier a
# machine-authored contract is born at.
STAGING_TIER = "T1_HYPOTHESIS"
SCHEMA = 1
_ID = re.compile(r"\A[a-z0-9][a-z0-9._-]{2,63}\Z")


class StagingError(RuntimeError):
    """The staging store refused a write."""


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
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
            retired_reason  TEXT
        )""")
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS staging_meta (key TEXT PRIMARY KEY, "
        "value TEXT NOT NULL)")
    conn.execute(
        "INSERT OR IGNORE INTO staging_meta VALUES ('schema', ?)", (str(SCHEMA),))
    conn.commit()


class StagingStore:
    """Append-only registry of machine-authored candidate mechanisms."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.path) as conn:
            _migrate(conn)

    def register(self, proposal: object, *, generation: int,
                 now: float | None = None) -> ProposedContract:
        """Validate, compile and persist one authored mechanism.

        Compilation happens before the write on purpose: a contract that
        cannot execute must never occupy an identity, because a registered id
        that never fires is indistinguishable in the evidence from one that
        fired and lost.
        """
        contract = validate(proposal)
        if not _ID.match(contract.contract_id):
            raise ContractProposalError(
                f"contract_id {contract.contract_id!r} must be 3-64 lowercase "
                "alphanumerics, '.', '-' or '_'")
        if not isinstance(generation, int) or generation < 0:
            raise StagingError("generation must be a non-negative integer")
        compile_contract(contract)
        timestamp = time.time() if now is None else float(now)
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
        }
        try:
            with _connect(self.path) as conn:
                conn.execute(
                    "INSERT INTO staged_contracts (contract_id, generation, "
                    "author, tier, mechanism, payer, falsifier, direction, "
                    "conditions_json, notes, registered_ts) VALUES "
                    "(:contract_id,:generation,:author,:tier,:mechanism,"
                    ":payer,:falsifier,:direction,:conditions_json,:notes,"
                    ":registered_ts)", row)
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise StagingError(
                f"contract_id {contract.contract_id!r} is already "
                "registered; a changed claim needs a new id") from exc
        return contract

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
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM staged_contracts WHERE retired_ts IS NULL "
                "ORDER BY generation ASC, contract_id ASC").fetchall()
        return [self._row_to_contract(row) for row in rows]

    def contract(self, contract_id: str) -> ProposedContract | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM staged_contracts WHERE contract_id=?",
                (str(contract_id),)).fetchone()
        return self._row_to_contract(row) if row is not None else None

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
        with _connect(self.path) as conn:
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
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT MAX(generation) AS latest FROM staged_contracts"
            ).fetchone()
        return -1 if row["latest"] is None else int(row["latest"])

    def summary(self) -> dict:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT tier, retired_ts IS NULL AS live, COUNT(*) n "
                "FROM staged_contracts GROUP BY tier, live").fetchall()
        return {
            "active": sum(r["n"] for r in rows if r["live"]),
            "retired": sum(r["n"] for r in rows if not r["live"]),
            "tiers": sorted({r["tier"] for r in rows}),
        }
