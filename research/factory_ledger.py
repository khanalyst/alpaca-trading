"""Append-only persistence for autonomous strategy-factory lineage."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
import uuid

from .edge_lab import DEFAULT_DB_PATH, EdgeLedger, canonical_json
from .gates import verify_gate_envelope


FACTORY_SCHEMA = "strategy-factory.v1"
ACTIVE_HYPOTHESIS_STATES = {
    "queued", "testing", "backtest_passed", "pending_generation_limit",
    "pending_llm_replacement",
}
FACTORY_STATUSES = ACTIVE_HYPOTHESIS_STATES | {"validated", "retired"}


class FactoryError(ValueError):
    """Raised when a factory operation cannot preserve research boundaries."""


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


class FactoryLedger:
    """Store immutable hypotheses, accounts, events, and completed cycles."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        EdgeLedger(self.path)
        with closing(_connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS factory_hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    slot INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
                    parent_hypothesis_id TEXT,
                    family TEXT NOT NULL,
                    thesis TEXT NOT NULL,
                    falsification TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    not_before TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(vehicle,slot,generation)
                );
                CREATE TABLE IF NOT EXISTS factory_events (
                    event_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS factory_accounts (
                    account_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                    variant_id TEXT NOT NULL,
                    vehicle TEXT NOT NULL,
                    starting_cash REAL NOT NULL,
                    ending_equity REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    trades INTEGER NOT NULL,
                    worker_pid INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(cycle_id,variant_id,vehicle)
                );
                CREATE TABLE IF NOT EXISTS factory_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    dataset_hash TEXT NOT NULL,
                    vehicle TEXT NOT NULL,
                    workers INTEGER NOT NULL,
                    strategies INTEGER NOT NULL,
                    variants INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(dataset_hash,vehicle)
                );
                CREATE TRIGGER IF NOT EXISTS factory_hypotheses_no_update
                    BEFORE UPDATE ON factory_hypotheses BEGIN
                    SELECT RAISE(ABORT, 'factory hypotheses are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_hypotheses_no_delete
                    BEFORE DELETE ON factory_hypotheses BEGIN
                    SELECT RAISE(ABORT, 'factory hypotheses are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_accounts_no_update
                    BEFORE UPDATE ON factory_accounts BEGIN
                    SELECT RAISE(ABORT, 'factory accounts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_accounts_no_delete
                    BEFORE DELETE ON factory_accounts BEGIN
                    SELECT RAISE(ABORT, 'factory accounts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_events_no_update
                    BEFORE UPDATE ON factory_events BEGIN
                    SELECT RAISE(ABORT, 'factory events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_events_no_delete
                    BEFORE DELETE ON factory_events BEGIN
                    SELECT RAISE(ABORT, 'factory events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_cycles_no_update
                    BEFORE UPDATE ON factory_cycles BEGIN
                    SELECT RAISE(ABORT, 'factory cycles are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_cycles_no_delete
                    BEFORE DELETE ON factory_cycles BEGIN
                    SELECT RAISE(ABORT, 'factory cycles are immutable');
                END;
            """)

    def register(self, hypothesis: Any) -> dict:
        now = datetime.now().timestamp()
        with closing(_connect(self.path)) as db, db:
            row = db.execute(
                "SELECT * FROM factory_hypotheses WHERE hypothesis_id=?",
                (hypothesis.hypothesis_id,),
            ).fetchone()
            if row is None:
                db.execute("INSERT INTO factory_hypotheses VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    hypothesis.hypothesis_id, hypothesis.slot, hypothesis.generation,
                    hypothesis.vehicle, hypothesis.parent_hypothesis_id,
                    hypothesis.family, hypothesis.thesis,
                    hypothesis.falsification, canonical_json(hypothesis.rule_spec),
                    hypothesis.not_before, now,
                ))
                db.execute("INSERT INTO factory_events VALUES(?,?,?,?,?,?)", (
                    uuid.uuid4().hex, hypothesis.hypothesis_id, "queued",
                    "autonomous hypothesis registered", canonical_json({}), now,
                ))
            return self.hypothesis(hypothesis.hypothesis_id) or {}

    def event(self, hypothesis_id: str, status: str, reason: str,
              payload: Mapping | None = None) -> None:
        if not reason.strip():
            raise FactoryError("factory event reason is required")
        if status not in FACTORY_STATUSES:
            raise FactoryError(f"unknown factory status: {status}")
        if status == "retired":
            raise FactoryError("retirement requires retire_hypothesis evidence verification")
        if self.hypothesis(hypothesis_id) is None:
            raise KeyError(hypothesis_id)
        self._append_event(hypothesis_id, status, reason, payload)

    def _append_event(self, hypothesis_id: str, status: str, reason: str,
                      payload: Mapping | None = None) -> None:
        with closing(_connect(self.path)) as db, db:
            db.execute("INSERT INTO factory_events VALUES(?,?,?,?,?,?)", (
                uuid.uuid4().hex, hypothesis_id, status, reason,
                canonical_json(dict(payload or {})), datetime.now().timestamp(),
            ))

    def events(self, hypothesis_id: str) -> list[dict]:
        if self.hypothesis(hypothesis_id) is None:
            raise KeyError(hypothesis_id)
        with closing(_connect(self.path)) as db:
            rows = db.execute("""SELECT * FROM factory_events
                WHERE hypothesis_id=? ORDER BY created_at,event_id""",
                (hypothesis_id,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            output.append(item)
        return output

    def retire_hypothesis(self, hypothesis_id: str, *, cycle_id: str,
                          expected_variants: int, reason: str,
                          payload: Mapping | None = None) -> None:
        """Retire only after a replacement exists and every intended gate failed."""
        if not reason.strip():
            raise FactoryError("factory retirement reason is required")
        with closing(_connect(self.path)) as db:
            child = db.execute("""SELECT 1 FROM factory_hypotheses
                WHERE parent_hypothesis_id=? LIMIT 1""", (hypothesis_id,)).fetchone()
            rows = db.execute("""SELECT result_json FROM factory_accounts
                WHERE cycle_id=? AND hypothesis_id=? ORDER BY variant_id""",
                (cycle_id, hypothesis_id)).fetchall()
        if child is None:
            raise FactoryError("hypothesis cannot retire before its replacement is registered")
        if len(rows) != int(expected_variants) or int(expected_variants) < 1:
            raise FactoryError("hypothesis retirement requires every intended variant account")
        gate_hashes = []
        for row in rows:
            try:
                result = json.loads(row["result_json"])
                gate = result["gate"]
                envelope = gate["verified_gate"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise FactoryError("hypothesis retirement evidence is incomplete") from exc
            if (gate.get("sample_adequate") is not True or
                    gate.get("heldout_sample_adequate") is not True or
                    gate.get("passes") is not False or
                    not verify_gate_envelope(envelope) or
                    envelope.get("passes") is not False):
                raise FactoryError("hypothesis retirement requires adequate failed verified gates")
            gate_hashes.append(str(envelope["content_hash"]))
        detail = {**dict(payload or {}), "cycle_id": cycle_id,
                  "expected_variants": int(expected_variants),
                  "verified_gate_hashes": sorted(gate_hashes)}
        self._append_event(hypothesis_id, "retired", reason, detail)

    def hypothesis(self, hypothesis_id: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT h.*, (
                SELECT status FROM factory_events e WHERE e.hypothesis_id=h.hypothesis_id
                ORDER BY e.created_at DESC,e.event_id DESC LIMIT 1) AS status
                FROM factory_hypotheses h WHERE h.hypothesis_id=?""",
                (hypothesis_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["rule_spec"] = json.loads(item.pop("spec_json"))
        return item

    def hypotheses(self, *, vehicle: str | None = None) -> list[dict]:
        where = " WHERE h.vehicle=?" if vehicle else ""
        parameters = (vehicle,) if vehicle else ()
        with closing(_connect(self.path)) as db:
            rows = db.execute("""SELECT h.*, (
                SELECT status FROM factory_events e WHERE e.hypothesis_id=h.hypothesis_id
                ORDER BY e.created_at DESC,e.event_id DESC LIMIT 1) AS status
                FROM factory_hypotheses h""" + where +
                " ORDER BY h.vehicle,h.slot,h.generation", parameters).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["rule_spec"] = json.loads(item.pop("spec_json"))
            output.append(item)
        return output

    def active(self, vehicle: str) -> list[dict]:
        latest: dict[int, dict] = {}
        for item in self.hypotheses(vehicle=vehicle):
            if item.get("status") in ACTIVE_HYPOTHESIS_STATES:
                latest[int(item["slot"])] = item
        return [latest[key] for key in sorted(latest)]

    def slot_latest(self, vehicle: str) -> dict[int, dict]:
        """Return the highest-generation hypothesis in each occupied slot."""
        latest: dict[int, dict] = {}
        for item in self.hypotheses(vehicle=vehicle):
            slot = int(item["slot"])
            current = latest.get(slot)
            if current is None or int(item["generation"]) >= int(current["generation"]):
                latest[slot] = item
        return latest

    def next_generation(self, vehicle: str, slot: int) -> int:
        """Return the next free generation number in a slot.

        ``factory_hypotheses`` is unique on ``(vehicle, slot, generation)``, so
        a successor registered into an occupied slot has to continue that
        slot's numbering rather than restart it.
        """
        generations = [int(item["generation"])
                       for item in self.hypotheses(vehicle=vehicle)
                       if int(item["slot"]) == int(slot)]
        return max(generations) + 1 if generations else 0

    def slot_families(self, vehicle: str, slot: int) -> set[str]:
        return {str(item["family"]) for item in self.hypotheses(vehicle=vehicle)
                if int(item["slot"]) == int(slot)}

    def slot_event_count(self, vehicle: str, slot: int, *, status: str,
                         flag: str) -> int:
        """Count a slot's events carrying ``payload[flag] is True``."""
        total = 0
        for item in self.hypotheses(vehicle=vehicle):
            if int(item["slot"]) != int(slot):
                continue
            for event in self.events(str(item["hypothesis_id"])):
                payload = event.get("payload")
                if (event.get("status") == status and
                        isinstance(payload, Mapping) and payload.get(flag) is True):
                    total += 1
        return total

    def add_account(self, cycle_id: str, hypothesis_id: str, result: Mapping) -> None:
        account = result["account"]
        with closing(_connect(self.path)) as db, db:
            db.execute("INSERT INTO factory_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                account["account_id"], cycle_id, hypothesis_id, result["variant_id"],
                result["vehicle"], account["starting_cash"], account["ending_equity"],
                account["realized_pnl"], account["max_drawdown"], account["trades"],
                result["worker_pid"], canonical_json(dict(result)), datetime.now().timestamp(),
            ))

    def last_boundary(self, hypothesis_id: str, vehicle: str) -> str | None:
        with closing(_connect(self.path)) as db:
            rows = db.execute("""SELECT result_json FROM factory_accounts
                WHERE hypothesis_id=? AND vehicle=? ORDER BY created_at DESC""",
                (hypothesis_id, vehicle)).fetchall()
        values = []
        for row in rows:
            try:
                value = json.loads(row["result_json"]).get("evaluation_end")
                if value:
                    values.append(str(value))
            except json.JSONDecodeError:
                continue
        return max(values) if values else None

    def existing_cycle(self, dataset_hash: str, vehicle: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute(
                "SELECT result_json FROM factory_cycles WHERE dataset_hash=? AND vehicle=?",
                (dataset_hash, vehicle),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def add_cycle(self, cycle_id: str, dataset_hash: str, vehicle: str,
                  workers: int, strategies: int, variants: int,
                  result: Mapping) -> None:
        with closing(_connect(self.path)) as db, db:
            db.execute("INSERT INTO factory_cycles VALUES(?,?,?,?,?,?,?,?)", (
                cycle_id, dataset_hash, vehicle, workers, strategies, variants,
                canonical_json(dict(result)), datetime.now().timestamp(),
            ))

    def status(self) -> dict:
        hypotheses = self.hypotheses()
        with closing(_connect(self.path)) as db:
            accounts = db.execute(
                "SELECT COUNT(*) AS n FROM factory_accounts").fetchone()["n"]
            cycles = db.execute(
                "SELECT COUNT(*) AS n FROM factory_cycles").fetchone()["n"]
        return {"schema": FACTORY_SCHEMA, "hypotheses": hypotheses,
                "accounts": int(accounts), "cycles": int(cycles)}


__all__ = [
    "ACTIVE_HYPOTHESIS_STATES", "FACTORY_SCHEMA", "FACTORY_STATUSES",
    "FactoryError", "FactoryLedger",
]
