"""Append-only persistence for autonomous strategy-factory lineage."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
import uuid

from .edge_lab import DEFAULT_DB_PATH, EdgeLedger, canonical_json, content_hash
from .gates import verify_gate_envelope


FACTORY_SCHEMA = "strategy-factory.v1"
FACTORY_IDENTITY_SCHEMA = "strategy-experiment.v1"
ACTIVE_HYPOTHESIS_STATES = {
    "queued", "testing", "backtest_passed", "pending_generation_limit",
    "pending_llm_replacement",
}
FACTORY_STATUSES = ACTIVE_HYPOTHESIS_STATES | {"validated", "retired"}
# What a recorded reason was given for.  ``tuning`` changes the numbers of one
# hypothesis; the rest change which hypothesis a slot holds.
LESSON_KINDS = {"tuning", "discovery", "replacement", "rotation", "reseed",
                # What a live paper trial taught, which is the only lesson kind
                # produced by real fills rather than a replay.
                "trial"}
LESSON_SOURCES = {"llm", "deterministic", "live_paper"}
FDR_METHOD = "lord_balanced_v2"


class FactoryError(ValueError):
    """Raised when a factory operation cannot preserve research boundaries."""


def _fdr_gamma(index: int) -> float:
    """Balanced telescoping allocation weights whose infinite sum is one."""
    if index <= 0:
        return 0.0
    return 1.0 / (int(index) * (int(index) + 1))


def _fdr_allocation(decisions: list[Mapping[str, Any]], alpha: float) -> tuple[int, float]:
    """Return the next LORD-style allocation without mutating the ledger."""
    test_index = len(decisions) + 1
    allocated = float(alpha) * _fdr_gamma(test_index)
    for discovery_index, row in enumerate(decisions, start=1):
        if bool(row["decision"]):
            allocated += float(alpha) * _fdr_gamma(test_index - discovery_index)
    return test_index, min(float(alpha), allocated)


def deferred_fdr(scope: str, test_id: str) -> dict[str, Any]:
    """Describe an offline proof whose cumulative test is reserved for live shadow."""
    return {"scope": str(scope), "test_id": str(test_id), "required": False,
            "tested": False, "status": "deferred_to_live_shadow",
            "decision": False, "cumulative": True,
            "method": "deferred_confirmatory_v2"}


def _real(value: Any) -> float | None:
    """Coerce a metric to a finite float, or ``None`` rather than a guess."""
    if isinstance(value, (bool, str, bytes, bytearray)) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def experiment_identity(*, dataset_hash: str, vehicle: str,
                        code_hash: str | None = None,
                        config_hash: str | None = None,
                        cost: Mapping | None = None,
                        risk: Mapping | None = None,
                        gate: Mapping | None = None,
                        provenance: Mapping | None = None) -> dict[str, Any]:
    """Build the deterministic identity that distinguishes factory runs.

    The returned body is safe to persist and its ``identity_hash`` is stable
    across process/cycle UUIDs.  Legacy callers can omit optional fields; the
    dataset/vehicle identity remains backward-compatible while new callers
    should bind every supplied assumption.
    """
    body = {"schema": FACTORY_IDENTITY_SCHEMA, "dataset_hash": str(dataset_hash),
            "vehicle": str(vehicle), "code_hash": code_hash,
            "config_hash": config_hash, "cost": dict(cost or {}),
            "risk": dict(risk or {}), "gate": dict(gate or {}),
            "provenance": dict(provenance or {})}
    body["identity_hash"] = content_hash(body)
    return body


def experiment_provenance(*, dataset: Any = None, config: Any = None,
                          code: Any = None, cost: Mapping | None = None,
                          risk: Mapping | None = None,
                          gate: Mapping | None = None) -> dict[str, str]:
    """Hash the assumptions that materially affect a factory result."""
    return {
        "dataset_hash": content_hash(dataset if dataset is not None else {}),
        "config_hash": content_hash(config if config is not None else {}),
        "code_hash": content_hash(code if code is not None else {}),
        "cost_hash": content_hash(dict(cost or {})),
        "risk_hash": content_hash(dict(risk or {})),
        "gate_hash": content_hash(dict(gate or {})),
    }


def _migrate_cycle_identity(db: sqlite3.Connection) -> None:
    """Migrate the old dataset/vehicle uniqueness without losing rows."""
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='factory_cycles'").fetchone()
    if not exists:
        return
    columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(factory_cycles)")}
    if "identity_hash" in columns:
        if "identity_json" not in columns:
            db.execute("DROP TRIGGER IF EXISTS factory_cycles_no_update")
            db.execute("DROP TRIGGER IF EXISTS factory_cycles_no_delete")
            db.execute("ALTER TABLE factory_cycles ADD COLUMN identity_json TEXT NOT NULL DEFAULT '{}'")
            rows = db.execute("SELECT cycle_id,dataset_hash,vehicle,identity_hash FROM factory_cycles").fetchall()
            for row in rows:
                identity = experiment_identity(dataset_hash=row["dataset_hash"], vehicle=row["vehicle"])
                db.execute("UPDATE factory_cycles SET identity_json=? WHERE cycle_id=?",
                           (canonical_json(identity), row["cycle_id"]))
        return
    db.execute("DROP TRIGGER IF EXISTS factory_cycles_no_update")
    db.execute("DROP TRIGGER IF EXISTS factory_cycles_no_delete")
    db.execute("ALTER TABLE factory_cycles RENAME TO factory_cycles_legacy")
    db.execute("""CREATE TABLE factory_cycles (
        cycle_id TEXT PRIMARY KEY, dataset_hash TEXT NOT NULL,
        vehicle TEXT NOT NULL, identity_hash TEXT NOT NULL,
        identity_json TEXT NOT NULL, workers INTEGER NOT NULL,
        strategies INTEGER NOT NULL, variants INTEGER NOT NULL,
        result_json TEXT NOT NULL, created_at REAL NOT NULL)""")
    rows = db.execute("SELECT * FROM factory_cycles_legacy").fetchall()
    for row in rows:
        identity = experiment_identity(dataset_hash=row["dataset_hash"], vehicle=row["vehicle"])
        db.execute("""INSERT INTO factory_cycles
            (cycle_id,dataset_hash,vehicle,identity_hash,identity_json,workers,
             strategies,variants,result_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                row["cycle_id"], row["dataset_hash"], row["vehicle"],
                identity["identity_hash"], canonical_json(identity), row["workers"],
                row["strategies"], row["variants"], row["result_json"], row["created_at"]))
    db.execute("DROP TABLE factory_cycles_legacy")


class FactoryLedger:
    """Store immutable hypotheses, accounts, events, and completed cycles."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        EdgeLedger(self.path)
        with closing(_connect(self.path)) as db, db:
            _migrate_cycle_identity(db)
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
                    identity_hash TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    workers INTEGER NOT NULL,
                    strategies INTEGER NOT NULL,
                    variants INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                -- Why something was tried, and what happened when it was.
                -- Split in two because the two facts are learned at different
                -- times: the reason exists when the variant is proposed, the
                -- grade only after its gate is computed.  Keeping them as two
                -- append-only rows preserves the ledger's no-update rule
                -- instead of carving an exception into it.
                CREATE TABLE IF NOT EXISTS factory_lessons (
                    lesson_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                    vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
                    family TEXT NOT NULL,
                    variant_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    changed_json TEXT NOT NULL,
                    diagnosis_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    -- The graded lesson this proposal reasoned from, so the
                    -- chain of learning is a durable edge in the ledger rather
                    -- than an assertion in a prompt.
                    parent_lesson_id TEXT REFERENCES factory_lessons(lesson_id),
                    UNIQUE(hypothesis_id,variant_id,kind)
                );
                CREATE TABLE IF NOT EXISTS factory_lesson_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    lesson_id TEXT NOT NULL REFERENCES factory_lessons(lesson_id),
                    passed INTEGER NOT NULL,
                    underpowered INTEGER NOT NULL,
                    heldout_delta REAL,
                    heldout_net_pnl REAL,
                    q_value REAL,
                    failed_checks_json TEXT NOT NULL,
                    gate_hash TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(lesson_id)
                );
                CREATE INDEX IF NOT EXISTS factory_lessons_family
                    ON factory_lessons(vehicle,family,created_at);
                CREATE TRIGGER IF NOT EXISTS factory_lessons_no_update
                    BEFORE UPDATE ON factory_lessons BEGIN
                    SELECT RAISE(ABORT, 'factory lessons are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_lessons_no_delete
                    BEFORE DELETE ON factory_lessons BEGIN
                    SELECT RAISE(ABORT, 'factory lessons are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_lesson_outcomes_no_update
                    BEFORE UPDATE ON factory_lesson_outcomes BEGIN
                    SELECT RAISE(ABORT, 'factory lesson outcomes are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_lesson_outcomes_no_delete
                    BEFORE DELETE ON factory_lesson_outcomes BEGIN
                    SELECT RAISE(ABORT, 'factory lesson outcomes are immutable');
                END;
                CREATE INDEX IF NOT EXISTS factory_lessons_parent
                    ON factory_lessons(parent_lesson_id);
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
            # ``CREATE TABLE IF NOT EXISTS`` leaves an already-created table
            # alone, so a ledger written before the learning chain existed
            # needs the column added rather than assumed.
            columns = {str(row["name"]) for row in
                       db.execute("PRAGMA table_info(factory_lessons)")}
            if columns and "parent_lesson_id" not in columns:
                db.execute("ALTER TABLE factory_lessons "
                           "ADD COLUMN parent_lesson_id TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS factory_fdr (
                decision_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                test_id TEXT NOT NULL, p_value REAL NOT NULL,
                alpha REAL NOT NULL, allocated_alpha REAL NOT NULL,
                decision INTEGER NOT NULL, created_at REAL NOT NULL,
                UNIQUE(scope,test_id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS factory_fdr_scope ON factory_fdr(scope,created_at)")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_fdr_no_update
                BEFORE UPDATE ON factory_fdr BEGIN
                SELECT RAISE(ABORT, 'factory FDR decisions are immutable'); END;""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_fdr_no_delete
                BEFORE DELETE ON factory_fdr BEGIN
                SELECT RAISE(ABORT, 'factory FDR decisions are immutable'); END;""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_cycles_no_update
                BEFORE UPDATE ON factory_cycles BEGIN
                SELECT RAISE(ABORT, 'factory cycles are immutable'); END;""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_cycles_no_delete
                BEFORE DELETE ON factory_cycles BEGIN
                SELECT RAISE(ABORT, 'factory cycles are immutable'); END;""")

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
            performance = envelope.get("performance")
            try:
                net = float(performance.get("heldout_net_pnl"))
                expectancy = float(performance.get("heldout_expectancy"))
            except (AttributeError, TypeError, ValueError):
                raise FactoryError(
                    "hypothesis retirement requires terminal negative performance evidence")
            if not (net <= 0.0 and expectancy <= 0.0):
                raise FactoryError(
                    "hypothesis retirement requires terminal negative performance evidence")
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

    def record_lesson(self, hypothesis_id: str, *, vehicle: str, family: str,
                      variant_id: str, kind: str, source: str, reason: str,
                      changed: Mapping | None = None,
                      diagnosis: Mapping | None = None,
                      evidence: Mapping | None = None,
                      parent_lesson_id: str | None = None) -> str:
        """Record why something was tried, before anyone knows if it worked.

        Writing the reason at proposal time is what makes it evidence rather
        than a story told afterwards: it is fixed before the gate that will
        judge it has been computed.
        """
        if kind not in LESSON_KINDS:
            raise FactoryError(f"unknown lesson kind: {kind}")
        if source not in LESSON_SOURCES:
            raise FactoryError(f"unknown lesson source: {source}")
        if not str(reason).strip():
            raise FactoryError("a lesson requires a stated reason")
        if vehicle not in {"equity", "option"}:
            raise FactoryError("vehicle must be equity or option")
        if self.hypothesis(hypothesis_id) is None:
            raise KeyError(hypothesis_id)
        with closing(_connect(self.path)) as db, db:
            existing = db.execute(
                """SELECT lesson_id FROM factory_lessons
                   WHERE hypothesis_id=? AND variant_id=? AND kind=?""",
                (hypothesis_id, str(variant_id), kind)).fetchone()
            if existing is not None:
                return str(existing["lesson_id"])
            lesson_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO factory_lessons
                   (lesson_id,hypothesis_id,vehicle,family,variant_id,kind,
                    source,reason,changed_json,diagnosis_json,evidence_json,
                    created_at,parent_lesson_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    lesson_id, hypothesis_id, vehicle, str(family),
                    str(variant_id), kind, source,
                    " ".join(str(reason).split()),
                    canonical_json(dict(changed or {})),
                    canonical_json(dict(diagnosis or {})),
                    canonical_json(dict(evidence or {})),
                    datetime.now().timestamp(),
                    str(parent_lesson_id) if parent_lesson_id else None,
                ))
            return lesson_id

    def resolve_lesson_ref(self, ref: str) -> str | None:
        """Map a short citation back to the lesson it names, or ``None``.

        Proposals cite the truncated id the brief carried, so the citation has
        to be resolved before it can be stored as a foreign key.  An
        unresolvable reference is dropped rather than stored, which keeps a
        fabricated citation out of the chain.
        """
        text = str(ref or "").strip().lower()
        if not text:
            return None
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT lesson_id FROM factory_lessons WHERE lesson_id LIKE ? || '%'"
                " LIMIT 2", (text,)).fetchall()
        return str(rows[0]["lesson_id"]) if len(rows) == 1 else None

    def failed_variant_ids(self, *, vehicle: str,
                           family: str | None = None) -> set[str]:
        """Variants a graded lesson already recorded as an adequate failure.

        Re-proposing one is not a new experiment: it is the same experiment,
        and its answer is already in the ledger.
        """
        parameters: list[Any] = [vehicle]
        clause = ""
        if family is not None:
            clause = " AND l.family=?"
            parameters.append(family)
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                """SELECT DISTINCT l.variant_id FROM factory_lessons l
                   JOIN factory_lesson_outcomes o ON o.lesson_id=l.lesson_id
                   WHERE l.vehicle=? AND o.passed=0 AND o.underpowered=0"""
                + clause, parameters).fetchall()
        return {str(row["variant_id"]) for row in rows}

    def grade_lesson(self, hypothesis_id: str, variant_id: str, *, kind: str,
                     outcome: Mapping) -> str | None:
        """Attach the gate's verdict to the reason that predicted it.

        Returns ``None`` when nothing proposed this variant with a reason, so
        grading a deterministic path that predates lessons is a no-op rather
        than an error.
        """
        with closing(_connect(self.path)) as db, db:
            lesson = db.execute(
                """SELECT lesson_id FROM factory_lessons
                   WHERE hypothesis_id=? AND variant_id=? AND kind=?""",
                (hypothesis_id, str(variant_id), kind)).fetchone()
            if lesson is None:
                return None
            lesson_id = str(lesson["lesson_id"])
            if db.execute("SELECT 1 FROM factory_lesson_outcomes WHERE lesson_id=?",
                          (lesson_id,)).fetchone() is not None:
                return lesson_id
            db.execute("INSERT INTO factory_lesson_outcomes VALUES(?,?,?,?,?,?,?,?,?,?)", (
                uuid.uuid4().hex, lesson_id,
                1 if outcome.get("passed") else 0,
                1 if outcome.get("underpowered") else 0,
                _real(outcome.get("heldout_delta")),
                _real(outcome.get("heldout_net_pnl")),
                _real(outcome.get("q_value")),
                canonical_json(list(outcome.get("failed_checks") or [])),
                str(outcome["gate_hash"]) if outcome.get("gate_hash") else None,
                datetime.now().timestamp(),
            ))
            return lesson_id

    def lessons(self, *, vehicle: str | None = None, family: str | None = None,
                hypothesis_id: str | None = None, graded_only: bool = False,
                limit: int = 50) -> list[dict]:
        """Return graded proposal reasons, most recent first."""
        where = []
        parameters: list[Any] = []
        for column, value in (("l.vehicle", vehicle), ("l.family", family),
                              ("l.hypothesis_id", hypothesis_id)):
            if value is not None:
                where.append(f"{column}=?")
                parameters.append(value)
        if graded_only:
            where.append("o.outcome_id IS NOT NULL")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        parameters.append(max(1, int(limit)))
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                """SELECT l.*, o.passed, o.underpowered, o.heldout_delta,
                          o.heldout_net_pnl, o.q_value, o.failed_checks_json,
                          o.gate_hash, o.outcome_id
                   FROM factory_lessons l
                   LEFT JOIN factory_lesson_outcomes o ON o.lesson_id=l.lesson_id"""
                + clause + " ORDER BY l.created_at DESC, l.lesson_id DESC LIMIT ?",
                parameters).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["changed"] = json.loads(item.pop("changed_json"))
            item["diagnosis"] = json.loads(item.pop("diagnosis_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            failed = item.pop("failed_checks_json")
            graded = item.pop("outcome_id") is not None
            item["outcome"] = ({
                "passed": bool(item["passed"]),
                "underpowered": bool(item["underpowered"]),
                "heldout_delta": item["heldout_delta"],
                "heldout_net_pnl": item["heldout_net_pnl"],
                "q_value": item["q_value"],
                "failed_checks": json.loads(failed) if failed else [],
                "gate_hash": item["gate_hash"],
            } if graded else None)
            for key in ("passed", "underpowered", "heldout_delta",
                        "heldout_net_pnl", "q_value", "gate_hash"):
                item.pop(key, None)
            output.append(item)
        return output

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
        # A factory account is diagnostic evidence, not a consumed forward
        # boundary: it is written before the gate knows whether all intended
        # shadow variants were adequately powered.  Read boundaries only from
        # durable EdgeLedger proof runs, so an underpowered sibling cannot make
        # the next cycle skip unseen observations.
        with closing(_connect(self.path)) as db:
            try:
                rows = db.execute("""SELECT r.run_id, r.candidate_id,
                        r.heldout_end, r.fit_end, r.metrics_json, c.axes_json,
                        e.payload_json,
                        e.evidence_hash
                    FROM runs r JOIN candidates c ON c.candidate_id=r.candidate_id
                    JOIN evidence e ON e.run_id=r.run_id AND e.kind='verified_gate'
                    WHERE r.vehicle=? AND r.lane='shadow'""",
                    (vehicle,)).fetchall()
            except sqlite3.Error:
                return None
        proof_ledger = EdgeLedger(self.path)
        values = []
        for row in rows:
            try:
                axes = json.loads(row["axes_json"] or "{}")
                if axes.get("hypothesis_id") != hypothesis_id:
                    continue
                verified = proof_ledger.verified_run(str(row["run_id"]))
                if (verified is None or verified[0].get("candidate_id") !=
                        row["candidate_id"] or verified[0].get("lane") != "shadow" or
                        verified[0].get("vehicle") != vehicle or
                        verified[1].get("passes") is not True):
                    continue
                payload = json.loads(row["payload_json"] or "{}")
                if (row["evidence_hash"] != content_hash(payload) or
                        not isinstance(payload, Mapping)):
                    continue
                gate = payload.get("gate")
                if (not isinstance(gate, Mapping) or
                        payload.get("gate_hash") != gate.get("content_hash") or
                        not verify_gate_envelope(gate)):
                    continue
                for value in (row["heldout_end"], row["fit_end"]):
                    if value:
                        values.append(str(value))
                metrics = json.loads(row["metrics_json"] or "{}")
                run_gate = metrics.get("gate") if isinstance(metrics, Mapping) else None
                qualification = (run_gate.get("qualification")
                                 if isinstance(run_gate, Mapping) else None)
                if isinstance(qualification, Mapping):
                    values.extend(str(item) for item in
                                   (qualification.get("sessions") or ()) if item)
            except json.JSONDecodeError:
                continue
        return max(values) if values else None

    def existing_cycle(self, dataset_hash: str, vehicle: str,
                       identity: Mapping | str | None = None) -> dict | None:
        identity_hash = (str(identity.get("identity_hash")) if isinstance(identity, Mapping)
                         and identity.get("identity_hash") else
                         str(identity) if isinstance(identity, str) else None)
        with closing(_connect(self.path)) as db:
            if identity_hash:
                row = db.execute(
                    "SELECT result_json FROM factory_cycles WHERE identity_hash=?",
                    (identity_hash,)).fetchone()
            else:
                row = db.execute(
                    "SELECT result_json FROM factory_cycles WHERE dataset_hash=? AND vehicle=? "
                    "ORDER BY created_at DESC LIMIT 1", (dataset_hash, vehicle)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def add_cycle(self, cycle_id: str, dataset_hash: str, vehicle: str,
                  workers: int, strategies: int, variants: int,
                  result: Mapping, *, identity: Mapping | str | None = None,
                  provenance: Mapping | None = None) -> None:
        if identity is None:
            identity = experiment_identity(
                dataset_hash=dataset_hash, vehicle=vehicle,
                provenance=provenance)
        elif isinstance(identity, str):
            identity = {"schema": FACTORY_IDENTITY_SCHEMA,
                        "dataset_hash": str(dataset_hash), "vehicle": str(vehicle),
                        "identity_hash": identity}
        else:
            identity = dict(identity)
            identity.setdefault("identity_hash", content_hash(identity))
        recorded = dict(result)
        recorded.setdefault("experiment_identity", identity)
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO factory_cycles
                (cycle_id,dataset_hash,vehicle,identity_hash,identity_json,workers,
                 strategies,variants,result_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                cycle_id, dataset_hash, vehicle, identity["identity_hash"],
                canonical_json(identity), workers, strategies, variants,
                canonical_json(recorded), datetime.now().timestamp(),
            ))

    def next_fdr_allocation(self, scope: str, *, alpha: float = .05) -> dict[str, Any]:
        """Preview the next durable allocation without spending it."""
        nominal = float(alpha)
        if not 0 < nominal <= 1:
            raise FactoryError("alpha must be in (0,1]")
        resolved_scope = str(scope)
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT decision FROM factory_fdr WHERE scope=? "
                "ORDER BY created_at,decision_id", (resolved_scope,)).fetchall()
        test_index, allocated = _fdr_allocation(list(rows), nominal)
        return {"scope": resolved_scope, "alpha": nominal,
                "allocated_alpha": allocated, "tests": test_index,
                "cumulative": True, "method": FDR_METHOD, "preview": True}

    def record_fdr_decision(self, scope: str, test_id: str, p_value: float,
                            *, alpha: float = .05) -> dict[str, Any]:
        """Persist one balanced cumulative LORD-style online-FDR decision.

        Only independent confirmatory callers should spend this allocation.
        Development and offline-forward screens deliberately defer cumulative
        testing to the strictly newer live-shadow boundary.
        """
        p = float(p_value); nominal = float(alpha)
        if not 0 <= p <= 1 or not 0 < nominal <= 1:
            raise FactoryError("p_value must be in [0,1] and alpha in (0,1]")
        scope, test_id = str(scope), str(test_id)
        with closing(_connect(self.path)) as db, db:
            rows = db.execute(
                "SELECT * FROM factory_fdr WHERE scope=? "
                "ORDER BY created_at,decision_id", (scope,)).fetchall()
            for existing_index, existing in enumerate(rows, start=1):
                if str(existing["test_id"]) == test_id:
                    return dict(existing) | {
                        "decision": bool(existing["decision"]),
                        "tests": existing_index, "cumulative": True,
                        "method": FDR_METHOD,
                    }
            test_index, allocated = _fdr_allocation(list(rows), nominal)
            decision = bool(p <= allocated)
            db.execute("INSERT INTO factory_fdr VALUES(?,?,?,?,?,?,?,?)", (
                uuid.uuid4().hex, scope, test_id, p, nominal, allocated,
                int(decision), datetime.now().timestamp()))
        return {"scope": scope, "test_id": test_id, "p_value": p,
                "alpha": nominal, "allocated_alpha": allocated,
                "decision": decision, "tests": test_index,
                "cumulative": True, "method": FDR_METHOD}

    # Explicit aliases are the integration seam for strategy_factory callers.
    online_fdr = record_fdr_decision
    cumulative_fdr = record_fdr_decision

    def fdr_state(self, scope: str = "global") -> dict[str, Any]:
        with closing(_connect(self.path)) as db:
            rows = db.execute("SELECT * FROM factory_fdr WHERE scope=? ORDER BY created_at,decision_id",
                              (str(scope),)).fetchall()
        return {"scope": str(scope), "cumulative": True, "tests": len(rows),
                "alpha_spent": sum(float(row["allocated_alpha"]) for row in rows),
                "discoveries": sum(int(row["decision"]) for row in rows),
                "decisions": [dict(row) for row in rows]}

    online_fdr_state = fdr_state

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
    "ACTIVE_HYPOTHESIS_STATES", "FACTORY_SCHEMA", "FACTORY_IDENTITY_SCHEMA", "FACTORY_STATUSES",
    "FDR_METHOD", "LESSON_KINDS", "LESSON_SOURCES", "FactoryError", "FactoryLedger",
    "deferred_fdr",
    "experiment_identity",
    "experiment_provenance",
]
