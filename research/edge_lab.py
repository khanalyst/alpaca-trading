"""Lean, auditable edge discovery ledger.

The edge lab is deliberately boring infrastructure: JSON is canonicalised,
every durable row is append-only, and lifecycle decisions are events rather
than edits to an experiment.  It is suitable for both deterministic replay
(``backtest``) and a point-in-time paper lane (``shadow``), without bringing
in pandas, numpy, or a service dependency.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
import uuid
from zoneinfo import ZoneInfo

from .gates import (chronological_split, deterministic_placebo_deltas,
                    falsification_gate, heldout_separation,
                    matched_cluster_test, max_drawdown_of, paired_delta,
                    sample_counts, structural_floor, verified_gate_envelope,
                    verify_gate_envelope)
from .stats import benjamini_hochberg
from .ibr import IBRConfig, replay_ibr
from .market_data import (OptionSnapshot, UnderlyingBar,
                          normalize_option_snapshot, normalize_underlying_bar)

VEHICLES = ("equity", "option")
LANES = ("backtest", "shadow")
LIFECYCLE = ("candidate", "backtest_passed", "shadow", "validated", "champion",
             "retired", "demoted")
CANDIDATE, BACKTEST_PASSED, SHADOW, VALIDATED, CHAMPION, RETIRED, DEMOTED = LIFECYCLE
DEFAULT_DB_PATH = Path(os.getenv(
    "ALPACA_EDGE_DB",
    str(Path(__file__).resolve().parents[1] / "runtime" / "research" / "edge_lab.sqlite3")))
SCHEMA_VERSION = 2
PAPER_DEMOTION_MIN_OUTCOMES = 20
PAPER_DEMOTION_R_FLOOR = -2.0


def canonical_json(value: Any) -> str:
    """Encode a finite JSON value deterministically for hashing and storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


hash_dataset = content_hash
hash_config = content_hash
hash_provenance = content_hash


def hash_file(path: str | Path) -> str:
    source = Path(path)
    try:
        return hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        return content_hash({"missing": str(source)})


def provenance_hash(*, dataset: Any = None, config: Any = None,
                    code: Any = None, provenance: Any = None) -> dict[str, str]:
    """Return the four stable fingerprints attached to each run."""
    return {"dataset_hash": content_hash(dataset if dataset is not None else {}),
            "config_hash": content_hash(config if config is not None else {}),
            "code_hash": (hash_file(code) if isinstance(code, (str, Path))
                          else content_hash(code if code is not None else {})),
            "provenance_hash": content_hash(
                provenance if provenance is not None else {})}


def _connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _utc() -> float:
    return datetime.now(timezone.utc).timestamp()


def _json(value: Any) -> str:
    return canonical_json(value if value is not None else {})


def init_ledger(path: str | Path = DEFAULT_DB_PATH) -> dict:
    """Create (or migrate) the append-only edge ledger."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(target)) as db, db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS ledger_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_id TEXT PRIMARY KEY,
                variant_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                vehicle TEXT NOT NULL CHECK (vehicle IN ('equity','option')),
                base_version TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                axes_json TEXT NOT NULL,
                config_json TEXT NOT NULL,
                dataset_hash TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                provenance_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(variant_id, vehicle)
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                lane TEXT NOT NULL CHECK (lane IN ('backtest','shadow')),
                vehicle TEXT NOT NULL CHECK (vehicle IN ('equity','option')),
                dataset_hash TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                provenance_hash TEXT NOT NULL,
                fit_start TEXT, fit_end TEXT, heldout_start TEXT, heldout_end TEXT,
                metrics_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                vehicle TEXT NOT NULL CHECK (vehicle IN ('equity','option')),
                session_date TEXT NOT NULL,
                opportunity_id TEXT NOT NULL,
                entry_timestamp TEXT,
                exit_timestamp TEXT,
                net_pnl REAL NOT NULL,
                return_value REAL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(run_id, opportunity_id)
            );
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                run_id TEXT REFERENCES runs(run_id),
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                candidate_id TEXT REFERENCES candidates(candidate_id),
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_outcomes (
                outcome_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                vehicle TEXT NOT NULL CHECK (vehicle IN ('equity','option')),
                opportunity_id TEXT NOT NULL,
                session_date TEXT,
                net_pnl REAL NOT NULL,
                outcome_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(candidate_id, opportunity_id)
            );
            CREATE TABLE IF NOT EXISTS candidate_state (
                candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id),
                status TEXT NOT NULL CHECK (status IN ('candidate','backtest_passed',
                    'shadow','validated','champion','retired','demoted')),
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runs_candidate ON runs(candidate_id, created_at);
            CREATE INDEX IF NOT EXISTS trades_candidate ON trades(candidate_id, vehicle, session_date);
            CREATE INDEX IF NOT EXISTS events_candidate ON events(candidate_id, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS evidence_verified_gate_run
                ON evidence(run_id) WHERE kind='verified_gate';
            CREATE TRIGGER IF NOT EXISTS candidates_no_update BEFORE UPDATE ON candidates BEGIN
                SELECT RAISE(ABORT, 'candidates are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS candidates_no_delete BEFORE DELETE ON candidates BEGIN
                SELECT RAISE(ABORT, 'candidates are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS runs_no_update BEFORE UPDATE ON runs BEGIN
                SELECT RAISE(ABORT, 'runs are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS runs_no_delete BEFORE DELETE ON runs BEGIN
                SELECT RAISE(ABORT, 'runs are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS trades_no_update BEFORE UPDATE ON trades BEGIN
                SELECT RAISE(ABORT, 'trades are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS trades_no_delete BEFORE DELETE ON trades BEGIN
                SELECT RAISE(ABORT, 'trades are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence BEGIN
                SELECT RAISE(ABORT, 'evidence is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence BEGIN
                SELECT RAISE(ABORT, 'evidence is immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN
                SELECT RAISE(ABORT, 'events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN
                SELECT RAISE(ABORT, 'events are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS paper_outcomes_no_update BEFORE UPDATE ON paper_outcomes BEGIN
                SELECT RAISE(ABORT, 'paper outcomes are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS paper_outcomes_no_delete BEFORE DELETE ON paper_outcomes BEGIN
                SELECT RAISE(ABORT, 'paper outcomes are immutable');
            END;
        """)
        db.execute("INSERT OR REPLACE INTO ledger_meta(key,value) VALUES('schema',?)",
                   (str(SCHEMA_VERSION),))
    return {"db_path": str(target), "schema": SCHEMA_VERSION}


init_db = init_ledger


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class EdgeLedger:
    """SQLite-backed ledger; methods never mutate an experiment row."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        init_ledger(self.path)

    def register_candidate(self, variant_id: str, *, strategy_id: str = "ibr",
                           vehicle: str = "equity", base_version: str = "v1",
                           hypothesis: str, config: Mapping | None = None,
                           axes: Mapping | None = None, overrides: Mapping | None = None,
                           dataset: Any = None,
                           code: Any = None, provenance: Any = None,
                           candidate_id: str | None = None) -> dict:
        if vehicle not in VEHICLES:
            raise ValueError("vehicle must be equity or option")
        if not str(variant_id).strip() or not str(hypothesis).strip():
            raise ValueError("variant_id and hypothesis are required")
        if str(strategy_id) == "ibr":
            # Keep the laboratory bounded at its write boundary too; a
            # runtime-only arbitrary id must not become research evidence by
            # calling the ledger API directly.
            from agent.registry import validate_variant_id
            validate_variant_id("ibr", str(variant_id))
        cfg = dict(config or {})
        axis_payload = dict(axes or {})
        if overrides is not None:
            axis_payload.setdefault("overrides", dict(overrides))
        hashes = provenance_hash(dataset=dataset, config=cfg, code=code,
                                 provenance=provenance)
        candidate_id = candidate_id or uuid.uuid4().hex
        now = _utc()
        with closing(_connect(self.path)) as db, db:
            existing = db.execute("""SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                                  ON s.candidate_id=c.candidate_id
                                  WHERE c.variant_id=? AND c.vehicle=?""",
                                  (variant_id, vehicle)).fetchone()
            if existing:
                same = (existing["config_hash"] == hashes["config_hash"] and
                        existing["hypothesis"] == str(hypothesis))
                if not same:
                    raise ValueError("candidate identity is immutable; use a new variant_id")
                return dict(existing)
            db.execute("""INSERT INTO candidates
                (candidate_id,variant_id,strategy_id,vehicle,base_version,hypothesis,
                 axes_json,config_json,dataset_hash,config_hash,code_hash,provenance_hash,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, str(variant_id), str(strategy_id), vehicle,
                 str(base_version), str(hypothesis), _json(axis_payload), _json(cfg),
                 hashes["dataset_hash"], hashes["config_hash"], hashes["code_hash"],
                 hashes["provenance_hash"], now))
            db.execute("INSERT INTO candidate_state VALUES(?,?,?)",
                       (candidate_id, "candidate", now))
            db.execute("""INSERT INTO events
                (event_id,candidate_id,event_type,from_status,to_status,actor,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, candidate_id, "candidate_registered", None,
                 "candidate", "edge_lab", "pre-registered candidate", _json({}), now))
            return dict(db.execute("""SELECT c.*, s.status FROM candidates c
                JOIN candidate_state s ON s.candidate_id=c.candidate_id
                WHERE c.candidate_id=?""", (candidate_id,)).fetchone())

    def candidate(self, candidate_id: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                ON s.candidate_id=c.candidate_id WHERE c.candidate_id=?""",
                             (candidate_id,)).fetchone()
        return _row(row)

    def candidate_by_variant(self, variant_id: str, vehicle: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                ON s.candidate_id=c.candidate_id WHERE c.variant_id=? AND c.vehicle=?""",
                             (variant_id, vehicle)).fetchone()
        return _row(row)

    def append_run(self, candidate_id: str, *, lane: str, vehicle: str | None = None,
                   dataset: Any = None, config: Any = None, code: Any = None,
                   provenance: Any = None, fit: Sequence | None = None,
                   heldout: Sequence | None = None, metrics: Mapping | None = None,
                   run_id: str | None = None) -> dict:
        if lane not in LANES:
            raise ValueError("lane must be backtest or shadow")
        candidate = self.candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        vehicle = vehicle or candidate["vehicle"]
        if vehicle != candidate["vehicle"]:
            raise ValueError("run vehicle must match candidate vehicle; vehicles are never pooled")
        hashes = provenance_hash(dataset=dataset, config=config,
                                 code=code, provenance=provenance)
        fit_rows, held_rows = list(fit or ()), list(heldout or ())
        payload = dict(metrics or {})
        payload.setdefault("fit_trades", len(fit_rows))
        payload.setdefault("heldout_trades", len(held_rows))
        run_id = run_id or uuid.uuid4().hex
        def bounds(rows):
            values = [str(r.get("session_date") or r.get("entry_timestamp") or "")
                      for r in rows if isinstance(r, Mapping)]
            values = [v for v in values if v]
            return (min(values) if values else None, max(values) if values else None)
        fit_start, fit_end = bounds(fit_rows)
        held_start, held_end = bounds(held_rows)
        now = _utc()
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO runs
                (run_id,candidate_id,lane,vehicle,dataset_hash,config_hash,code_hash,
                 provenance_hash,fit_start,fit_end,heldout_start,heldout_end,metrics_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, candidate_id, lane, vehicle, hashes["dataset_hash"],
                 hashes["config_hash"], hashes["code_hash"], hashes["provenance_hash"],
                 fit_start, fit_end, held_start, held_end, _json(payload), now))
        return self.run(run_id) or {}

    def run(self, run_id: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        return result

    def append_trade(self, run_id: str, trade: Mapping, *, trade_id: str | None = None) -> dict:
        run = self.run(run_id)
        if run is None:
            raise KeyError(run_id)
        vehicle = str(trade.get("vehicle") or run["vehicle"])
        if vehicle != run["vehicle"]:
            raise ValueError("trade vehicle differs from run")
        opportunity = str(trade.get("opportunity_id") or trade.get("entry_timestamp") or uuid.uuid4().hex)
        session = str(trade.get("session_date") or "")
        if not session and trade.get("entry_timestamp"):
            session = str(trade.get("entry_timestamp"))[:10]
        if not session:
            raise ValueError("trade session_date is required")
        try:
            net = float(trade.get("net_pnl", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("trade net_pnl must be numeric") from exc
        tid = trade_id or uuid.uuid4().hex
        payload = dict(trade)
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO trades
                (trade_id,run_id,candidate_id,vehicle,session_date,opportunity_id,
                 entry_timestamp,exit_timestamp,net_pnl,return_value,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, run_id, run["candidate_id"], vehicle, session, opportunity,
                 trade.get("entry_timestamp"), trade.get("exit_timestamp"), net,
                 float(trade["return_value"]) if trade.get("return_value") is not None else None,
                 _json(payload), _utc()))
            row = db.execute("SELECT * FROM trades WHERE trade_id=?", (tid,)).fetchone()
        return _row(row) or {}

    def append_evidence(self, candidate_id: str, kind: str, payload: Any,
                        *, run_id: str | None = None) -> dict:
        if str(kind) == "verified_gate":
            raise ValueError("verified_gate evidence must be recorded through record_verified_gate")
        if self.candidate(candidate_id) is None:
            raise KeyError(candidate_id)
        if run_id is not None:
            run = self.run(run_id)
            if run is None:
                raise KeyError(run_id)
            if run["candidate_id"] != candidate_id:
                raise ValueError("evidence run belongs to another candidate")
        eid = uuid.uuid4().hex
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO evidence
                (evidence_id,candidate_id,run_id,kind,payload_json,evidence_hash,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (eid, candidate_id, run_id, str(kind), _json(payload), content_hash(payload), _utc()))
            row = db.execute("SELECT * FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
        return _row(row) or {}

    def record_verified_gate(self, run_id: str, gate: Mapping) -> dict:
        """Persist a gate only after its envelope agrees with durable run trades."""
        run = self.run(run_id)
        if run is None:
            raise KeyError(run_id)
        envelope = gate.get("verified_gate") if isinstance(gate.get("verified_gate"), Mapping) else gate
        if not isinstance(envelope, Mapping) or not verify_gate_envelope(envelope):
            raise ValueError("verified gate envelope/hash is invalid")
        error = self._gate_envelope_error(run, envelope)
        if error:
            raise ValueError(error)
        payload = {"run_id": run_id, "candidate_id": run["candidate_id"],
                   "lane": run["lane"], "vehicle": run["vehicle"],
                   "gate_hash": envelope["content_hash"],
                   "gate": dict(envelope)}
        eid = uuid.uuid4().hex
        with closing(_connect(self.path)) as db, db:
            existing = db.execute(
                "SELECT 1 FROM evidence WHERE run_id=? AND kind='verified_gate' LIMIT 1",
                (run_id,)).fetchone()
            if existing is not None:
                raise ValueError("run already has immutable verified gate evidence")
            db.execute("""INSERT INTO evidence
                (evidence_id,candidate_id,run_id,kind,payload_json,evidence_hash,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (eid, run["candidate_id"], run_id, "verified_gate", _json(payload),
                 content_hash(payload), _utc()))
            row = db.execute("SELECT * FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
        return _row(row) or {}

    def _gate_envelope_error(self, run: Mapping, envelope: Mapping) -> str | None:
        if envelope.get("lane") != run.get("lane") or envelope.get("vehicle") != run.get("vehicle"):
            return "verified gate lane/vehicle does not match the persisted run"
        with closing(_connect(self.path)) as db:
            durable = db.execute(
                "SELECT payload_json FROM trades WHERE run_id=? ORDER BY session_date,trade_id",
                (run["run_id"],)).fetchall()
        rows = []
        for item in durable:
            try:
                payload = json.loads(item["payload_json"])
            except (TypeError, json.JSONDecodeError):
                return "persisted trade payload is invalid"
            if not isinstance(payload, Mapping):
                return "persisted trade payload is invalid"
            rows.append(payload)
        if run["lane"] == "shadow":
            fit_rows, heldout_rows = [], rows
        else:
            fit_end = run.get("fit_end")
            held_start = run.get("heldout_start")
            if not fit_end or not held_start or str(fit_end) >= str(held_start):
                return "persisted run does not have a separated fit/heldout boundary"
            fit_rows = [row for row in rows
                        if str(row.get("session_date") or row.get("entry_timestamp") or "") <= str(fit_end)]
            heldout_rows = [row for row in rows
                            if str(row.get("session_date") or row.get("entry_timestamp") or "") >= str(held_start)]
            if len(fit_rows) + len(heldout_rows) != len(rows):
                return "persisted trades do not fit the recorded chronological boundary"
        expected = envelope.get("counts") or {}
        actual = {
            "fit": sample_counts(fit_rows, vehicle=run["vehicle"]),
            "heldout": sample_counts(heldout_rows, vehicle=run["vehicle"]),
            "total": sample_counts(rows, vehicle=run["vehicle"]),
        }
        if expected != actual:
            return "verified gate counts do not match persisted trades"
        floors = envelope.get("floors") or {}
        for name in ("fit", "heldout"):
            report = floors.get(name)
            if not isinstance(report, Mapping):
                return "verified gate floor report is missing"
            counts = actual[name]
            if any(int(report.get(key, -1)) != int(counts[key])
                   for key in ("trades", "sessions", "clusters")):
                return "verified gate floor report does not match persisted trades"
            structural = report.get("structural_checks")
            minimums = report.get("minimums")
            if (not isinstance(structural, Mapping) or
                    set(structural) != {"trades", "sessions", "clusters"} or
                    not isinstance(minimums, Mapping) or
                    set(minimums) != {"trades", "sessions", "clusters"}):
                return "verified gate structural floor checks are missing"
            try:
                expected_checks = {key: counts[key] >= int(minimums[key])
                                   for key in ("trades", "sessions", "clusters")}
            except (TypeError, ValueError):
                return "verified gate structural floor checks are invalid"
            if dict(structural) != expected_checks:
                return "verified gate structural floor checks are inconsistent"
            structural_passes = all(bool(value) for value in structural.values())
            required = bool(report.get("required", True))
            if bool(report.get("structural_passes")) != structural_passes:
                return "verified gate structural floor result is inconsistent"
            if bool(report.get("adequate")) != (structural_passes if required else True):
                return "verified gate adequacy result is inconsistent"
        checks = envelope.get("checks")
        if not isinstance(checks, Mapping) or not checks:
            return "verified gate decision checks are missing"
        if bool(envelope.get("passes")) != all(bool(value) for value in checks.values()):
            return "verified gate pass decision is inconsistent"
        statistics = envelope.get("statistics") or {}
        try:
            p_value = float(statistics["p_value"])
            q_value = float(statistics["q_value"])
            alpha = float(statistics["alpha"])
        except (KeyError, TypeError, ValueError):
            return "verified gate p/q evidence is invalid"
        if not (0.0 <= p_value <= 1.0 and 0.0 <= q_value <= 1.0 and 0.0 < alpha <= 1.0):
            return "verified gate p/q evidence is invalid"
        if "family_fdr_significant" in checks and \
                bool(checks["family_fdr_significant"]) != (q_value <= alpha):
            return "verified gate FDR decision is inconsistent"
        control = envelope.get("control") or {}
        if "actual_control_available" in checks and bool(checks["actual_control_available"]) != bool(
                control.get("actual_control") is True and control.get("available") is True):
            return "verified gate control decision is inconsistent"
        if "heldout_delta_positive" in checks:
            delta = control.get("mean_delta")
            positive = delta is not None and float(delta) > 0
            if bool(checks["heldout_delta_positive"]) != positive:
                return "verified gate control delta decision is inconsistent"
        if "heldout_p_significant" in checks and \
                bool(checks["heldout_p_significant"]) != (p_value <= alpha):
            return "verified gate raw p decision is inconsistent"
        if "falsification" in checks and bool(checks["falsification"]) != bool(
                (envelope.get("falsification") or {}).get("passes")):
            return "verified gate falsification decision is inconsistent"
        if "separated" in checks and bool(checks["separated"]) != bool(
                (envelope.get("separation") or {}).get("passes")):
            return "verified gate separation decision is inconsistent"
        if envelope.get("passes"):
            control = envelope.get("control") or {}
            if not (control.get("actual_control") is True and control.get("available") is True and
                    int(control.get("matched", 0)) > 0):
                return "passing verified gate lacks an actual matched control"
            if not bool((envelope.get("falsification") or {}).get("passes")):
                return "passing verified gate lacks a passing falsification"
            if not bool((envelope.get("separation") or {}).get("passes")):
                return "passing verified gate lacks fit/heldout separation"
        return None

    def _latest_verified_gate(self, candidate_id: str) -> tuple[dict, dict]:
        runs = self._runs(candidate_id)
        if not runs:
            raise ValueError("lifecycle transition requires persisted verified gate evidence")
        run = dict(runs[-1])
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT * FROM evidence
                WHERE candidate_id=? AND run_id=? AND kind='verified_gate'
                ORDER BY created_at DESC,evidence_id DESC LIMIT 1""",
                (candidate_id, run["run_id"])).fetchone()
        if row is None:
            raise ValueError("latest persisted run lacks verified gate evidence")
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("latest persisted verified gate evidence is invalid") from exc
        if row["evidence_hash"] != content_hash(payload):
            raise ValueError("latest persisted verified gate evidence hash is invalid")
        envelope = payload.get("gate") if isinstance(payload, Mapping) else None
        if not isinstance(envelope, Mapping) or payload.get("gate_hash") != envelope.get("content_hash"):
            raise ValueError("latest persisted verified gate envelope/hash is invalid")
        if not verify_gate_envelope(envelope) or self._gate_envelope_error(run, envelope):
            raise ValueError("latest persisted verified gate envelope is invalid")
        return run, dict(envelope)

    def append_event(self, *, candidate_id: str | None, event_type: str,
                     reason: str, actor: str = "edge_lab",
                     from_status: str | None = None, to_status: str | None = None,
                     payload: Mapping | None = None) -> dict:
        """Append an auditable non-lifecycle event (for operators/integrations)."""
        if not str(reason).strip():
            raise ValueError("event reason is required")
        if candidate_id is not None and self.candidate(candidate_id) is None:
            raise KeyError(candidate_id)
        event_id = uuid.uuid4().hex
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO events
                (event_id,candidate_id,event_type,from_status,to_status,actor,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (event_id, candidate_id, str(event_type), from_status, to_status,
                 str(actor), str(reason), _json(payload), _utc()))
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return _row(row) or {}

    def transition(self, candidate_id: str, to_status: str, *, reason: str,
                   actor: str = "edge_lab", rollback: bool = False,
                   payload: Mapping | None = None) -> dict:
        if not str(reason).strip():
            raise ValueError("transition reason is required")
        if to_status not in LIFECYCLE:
            raise ValueError(f"unknown lifecycle status: {to_status}")
        current = self.candidate(candidate_id)
        if current is None:
            raise KeyError(candidate_id)
        from_status = current["status"]
        if rollback:
            raise ValueError("rollback cannot bypass evidence; use an auditable demotion")
        allowed = {
            "candidate": {"backtest_passed", "retired"},
            "backtest_passed": {"shadow", "retired"},
            "shadow": {"validated", "demoted", "retired"},
            "validated": {"champion", "demoted", "retired"},
            # A stronger champion does not invalidate this candidate's proof.
            # It returns to the validated pool and remains available to paper
            # ``all_proved`` selection.  ``demoted`` is reserved for an actual
            # safety/performance failure.
            "champion": {"validated", "demoted", "retired"},
            "demoted": {"shadow", "retired"},
            "retired": set(),
        }
        if to_status not in allowed.get(from_status, set()):
            raise ValueError(f"invalid lifecycle transition {from_status}->{to_status}")
        required_lane = {
            "backtest_passed": "backtest",
            "shadow": "shadow",
            "validated": "shadow",
            "champion": "shadow",
        }.get(to_status)
        if required_lane:
            run, gate = self._latest_verified_gate(candidate_id)
            if run["lane"] != required_lane or gate.get("passes") is not True:
                raise ValueError(
                    f"{to_status} requires latest persisted passing {required_lane} verified evidence")
            if from_status == "demoted" and to_status == "shadow":
                with closing(_connect(self.path)) as db:
                    demotion = db.execute("""SELECT created_at FROM events
                        WHERE candidate_id=? AND to_status='demoted'
                        ORDER BY created_at DESC,event_id DESC LIMIT 1""",
                        (candidate_id,)).fetchone()
                if demotion is not None and float(run["created_at"]) <= float(demotion["created_at"]):
                    raise ValueError("shadow re-entry after demotion requires a newer verified shadow run")
        if to_status == "retired":
            _run, gate = self._latest_verified_gate(candidate_id)
            floors = gate.get("floors") or {}
            structurally_adequate = all(
                isinstance(floors.get(name), Mapping) and floors[name].get("adequate") is True
                for name in ("fit", "heldout"))
            if gate.get("passes") is not False or not structurally_adequate:
                raise ValueError("retirement requires latest adequate failed verified gate evidence")
        now = _utc()
        event_type = "safety_demotion" if to_status == "demoted" else "lifecycle_transition"
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO candidate_state VALUES(?,?,?)
                       ON CONFLICT(candidate_id)
                       DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
                       (candidate_id, to_status, now))
            db.execute("""INSERT INTO events
                (event_id,candidate_id,event_type,from_status,to_status,actor,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, candidate_id, event_type, from_status, to_status,
                 str(actor), str(reason), _json(payload), now))
        return self.candidate(candidate_id) or {}

    def history(self, candidate_id: str) -> list[dict]:
        with closing(_connect(self.path)) as db:
            rows = db.execute("SELECT * FROM events WHERE candidate_id=? ORDER BY created_at,event_id",
                              (candidate_id,)).fetchall()
        return [dict(row) for row in rows]

    def status(self, *, vehicle: str | None = None) -> list[dict]:
        query = """SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                   ON c.candidate_id=s.candidate_id"""
        params: tuple = ()
        if vehicle:
            if vehicle not in VEHICLES:
                raise ValueError("vehicle must be equity or option")
            query += " WHERE c.vehicle=?"; params = (vehicle,)
        query += " ORDER BY c.created_at,c.candidate_id"
        with closing(_connect(self.path)) as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def trades(self, candidate_id: str, *, lane: str | None = None) -> list[dict]:
        query = """SELECT t.*, r.lane FROM trades t JOIN runs r ON r.run_id=t.run_id
                   WHERE t.candidate_id=?"""
        params: list = [candidate_id]
        if lane:
            query += " AND r.lane=?"; params.append(lane)
        query += " ORDER BY t.session_date,t.entry_timestamp,t.trade_id"
        with closing(_connect(self.path)) as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def runs(self, candidate_id: str, *, lane: str | None = None) -> list[dict]:
        rows = self._runs(candidate_id, lane=lane)
        result = []
        for row in rows:
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json"))
            result.append(item)
        return result

    def evidence(self, candidate_id: str) -> list[dict]:
        with closing(_connect(self.path)) as db:
            rows = db.execute("SELECT * FROM evidence WHERE candidate_id=? ORDER BY created_at,evidence_id",
                              (candidate_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def latest_verified_run(self, candidate_id: str, *,
                            lane: str | None = None) -> dict | None:
        """Return the latest run only when its durable gate proof re-verifies."""
        try:
            run, gate = self._latest_verified_gate(candidate_id)
        except ValueError:
            return None
        if lane is not None and run.get("lane") != lane:
            return None
        result = dict(run)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        result["verified_gate"] = gate
        result["gate_hash"] = gate["content_hash"]
        return result

    def eligibility(self, candidate_id: str, *, lane: str = "shadow") -> dict:
        """Explain latest-proof eligibility without trusting caller run metrics."""
        candidate = self.candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        proof = self.latest_verified_run(candidate_id, lane=lane)
        eligible = bool(
            candidate.get("status") in {"validated", "champion"} and
            proof is not None and proof["verified_gate"].get("passes") is True)
        return {"candidate_id": candidate_id, "status": candidate["status"],
                "lane": lane, "eligible": eligible, "latest_verified_run": proof}

    def select_champion(self, *, vehicle: str, min_confidence: float = .95,
                        strategy_id: str | None = None) -> dict | None:
        """Select one conservative validated candidate for a vehicle only."""
        if vehicle not in VEHICLES:
            raise ValueError("vehicle must be equity or option")
        rows = self.status(vehicle=vehicle)
        eligible = [r for r in rows if r["status"] in {"validated", "champion"}
                    and (strategy_id is None or r["strategy_id"] == strategy_id)]
        scored = []
        for candidate in eligible:
            try:
                run, gate = self._latest_verified_gate(candidate["candidate_id"])
            except ValueError:
                continue
            if run["lane"] != "shadow" or gate.get("passes") is not True:
                continue
            # Conservative ranking: held-out lower confidence bound first,
            # then drawdown and sample size.  No metric can cross vehicles.
            statistics = gate.get("statistics") or {}
            performance = gate.get("performance") or {}
            held_counts = ((gate.get("counts") or {}).get("heldout") or {})
            confidence = 1.0 - float(statistics.get("q_value", 1.0) or 1.0)
            if confidence < min_confidence:
                continue
            scored.append((float(performance.get("heldout_delta", -float("inf"))),
                           -float(performance.get("max_drawdown", float("inf"))),
                           int(held_counts.get("trades", 0)), candidate))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        selected = scored[0][3]
        if selected["status"] != "champion":
            self.transition(selected["candidate_id"], "champion", reason="conservative evidence selection")
            selected = self.candidate(selected["candidate_id"]) or selected
        # Retain every still-proved edge.  Selection names one conservative
        # champion, but paper mode may continue evaluating all validated
        # candidates under the shared risk limits.
        for other in rows:
            if other["candidate_id"] != selected["candidate_id"] and other["status"] == "champion":
                self.transition(other["candidate_id"], "validated",
                                reason="superseded as champion; proof remains valid")
        return selected

    def ingest_paper_outcome(self, candidate_id: str, outcome: Mapping) -> dict:
        candidate = self.candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        vehicle = str(outcome.get("vehicle") or candidate["vehicle"])
        if vehicle != candidate["vehicle"]:
            raise ValueError("paper outcome vehicle differs from candidate")
        opportunity = str(outcome.get("opportunity_id") or outcome.get("entry_timestamp") or uuid.uuid4().hex)
        try:
            net = float(outcome["net_pnl"])
            risk_usd = float(outcome["risk_usd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("paper outcome requires finite net_pnl and positive risk_usd") from exc
        if not (math.isfinite(net) and math.isfinite(risk_usd) and risk_usd > 0):
            raise ValueError("paper outcome requires finite net_pnl and positive risk_usd")
        normalized = {**dict(outcome), "r_multiple": net / risk_usd,
                      "net_pnl": net, "risk_usd": risk_usd}
        oid = uuid.uuid4().hex
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO paper_outcomes
                (outcome_id,candidate_id,vehicle,opportunity_id,session_date,net_pnl,outcome_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (oid, candidate_id, vehicle, opportunity, outcome.get("session_date"),
                 net, _json(normalized), _utc()))
            db.execute("""INSERT INTO events
                (event_id,candidate_id,event_type,from_status,to_status,actor,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, candidate_id, "paper_outcome", candidate["status"],
                 candidate["status"], "paper", "paper outcome ingested", _json(normalized), _utc()))
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT outcome_json FROM paper_outcomes WHERE candidate_id=? "
                "ORDER BY created_at,outcome_id", (candidate_id,)).fetchall()
        r_values = []
        for row in rows:
            try:
                value = json.loads(row["outcome_json"]).get("r_multiple")
                number = float(value)
                if number == number and abs(number) != float("inf"):
                    r_values.append(number)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        recent_r = r_values[-PAPER_DEMOTION_MIN_OUTCOMES:]
        automatic_guard = (
            len(recent_r) >= PAPER_DEMOTION_MIN_OUTCOMES and
            sum(recent_r) <= PAPER_DEMOTION_R_FLOOR)
        if candidate["status"] == "champion" and automatic_guard:
            self.transition(
                candidate_id, "demoted",
                reason="paper outcomes failed the registered rolling R guard",
                payload={"outcomes": len(recent_r), "rolling_r": sum(recent_r)})
        return {"outcome_id": oid, "candidate_id": candidate_id,
                "status": (self.candidate(candidate_id) or {}).get("status"),
                "rolling_outcomes": len(recent_r),
                "rolling_r": sum(recent_r) if recent_r else None}

    def _runs(self, candidate_id: str, *, lane: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM runs WHERE candidate_id=?"; params: list = [candidate_id]
        if lane: query += " AND lane=?"; params.append(lane)
        query += " ORDER BY created_at,run_id"
        with closing(_connect(self.path)) as db:
            return db.execute(query, params).fetchall()


class DiscoveryError(ValueError):
    """Raised when a discovery corpus cannot be evaluated safely."""


def _read_discovery_rows(data: str | Path | Sequence[Mapping]) -> tuple[list[dict], list[UnderlyingBar], dict[str, OptionSnapshot]]:
    """Load one normalized JSONL corpus, retaining bars and option quotes."""
    if isinstance(data, (str, Path)):
        source = Path(data)
        try:
            raw_rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise DiscoveryError(f"invalid discovery JSONL {source}: {exc}") from exc
    else:
        raw_rows = [dict(row) for row in data]
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise DiscoveryError("discovery rows must be JSON objects")
    bars: list[UnderlyingBar] = []
    snapshots: dict[str, OptionSnapshot] = {}
    for number, source_row in enumerate(raw_rows, 1):
        row = dict(source_row)
        kind = str(row.get("kind", "bar")).lower()
        provider = str(row.get("provider") or "alpaca")
        feed = str(row.get("feed") or ("opra" if "option" in kind else "sip"))
        try:
            if kind in {"bar", "underlying", "underlying_bar"}:
                bars.append(normalize_underlying_bar(row, provider=provider, feed=feed))
            elif kind in {"option", "option_snapshot", "option_quote"}:
                contract = row.get("contract")
                if isinstance(contract, Mapping):
                    flattened = dict(contract)
                    flattened.update({key: value for key, value in row.items()
                                      if key != "contract"})
                    row = flattened
                snap = normalize_option_snapshot(row, provider=provider, feed=feed)
                snapshots[f"{snap.timestamp.isoformat()}|{snap.contract.symbol}"] = snap
            # Quotes and metadata are retained in the dataset hash but are not
            # silently fed into an OHLC replay.
        except (TypeError, ValueError) as exc:
            raise DiscoveryError(f"row {number}: {exc}") from exc
    if not bars:
        raise DiscoveryError("discovery corpus contains no underlying bars")
    return raw_rows, bars, snapshots


def _effective_ibr_config(base: Mapping | None, overrides: Mapping,
                          *, close_confirmed: bool = True) -> tuple[IBRConfig, dict]:
    """Build the replay config used by every variant from one immutable base."""
    source = dict(base or {})
    strategy = dict(source.get("strategy") or {})
    # The runtime contract's defaults are explicit here rather than inherited
    # from a notebook's replay defaults.
    strategy.setdefault("range_minutes", 15)
    strategy.setdefault("breakout_buffer_bps", 5.0)
    strategy.setdefault("target_r", 2.0)
    strategy.setdefault("range_stop", True)
    strategy.setdefault("stop_pct", .003)
    strategy.setdefault("target_pct", .006)
    for path, value in overrides.items():
        parts = str(path).split(".")
        if parts and parts[0] == "strategy":
            parts = parts[1:]
        node = strategy
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if parts:
            node[parts[-1]] = value
    research = dict(source.get("research") or {})
    execution = dict(source.get("execution") or {})
    cfg = IBRConfig(
        range_minutes=int(strategy.get("range_minutes", 15)),
        stop_pct=float(strategy.get("stop_pct", .003)),
        target_pct=float(strategy.get("target_pct", .006)),
        target_r=float(strategy["target_r"]) if strategy.get("target_r") is not None else None,
        range_stop=bool(strategy.get("range_stop", True)),
        breakout_buffer_bps=float(strategy.get("breakout_buffer_bps", 5.0)),
        spread_bps=float(research.get("spread_bps", strategy.get("spread_bps", 1.0))),
        slippage_bps=float(research.get("slippage_bps", execution.get("max_slippage_bps", 1.0))),
        fee_bps=float(research.get("fee_bps", .5)),
        close_confirmed=bool(close_confirmed),
        timezone=str((source.get("session") or {}).get("timezone", "America/New_York")),
    )
    effective = dict(source)
    effective["strategy"] = strategy
    effective["replay"] = {"close_confirmed": cfg.close_confirmed,
                            "range_stop": cfg.range_stop}
    return cfg, effective


def _opportunity_rows(result, bars: Sequence[UnderlyingBar], vehicle: str) -> list[dict]:
    """Materialize one row per symbol/session, including no-trade zeros."""
    zone = ZoneInfo("America/New_York")
    sessions = sorted({(bar.symbol, bar.timestamp.astimezone(zone).date()) for bar in bars})
    by_session = {(trade.symbol, trade.session_date): trade for trade in result.trades}
    rows: list[dict] = []
    for symbol, day in sessions:
        opportunity_id = f"ibr:{vehicle}:{symbol}:{day.isoformat()}"
        trade = by_session.get((symbol, day))
        if trade is None:
            rows.append({"vehicle": vehicle, "symbol": symbol,
                         "session_date": day.isoformat(),
                         "opportunity_id": opportunity_id, "net_pnl": 0.0,
                         "return_value": 0.0, "no_trade": True})
            continue
        row = {key: value for key, value in vars(trade).items()}
        row.update({"session_date": trade.session_date.isoformat(),
                    "opportunity_id": opportunity_id, "no_trade": False,
                    "return_value": float(trade.net_pnl)})
        for key, value in list(row.items()):
            if isinstance(value, (datetime, date)):
                row[key] = value.isoformat()
        rows.append(row)
    return rows


def _discover_gate(candidate: Sequence[Mapping], baseline: Sequence[Mapping], *,
                   vehicle: str, min_trades: int, min_sessions: int,
                   alpha: float, shadow: bool = False,
                   actual_control: bool = True,
                   control_kind: str = "matched_actual_baseline") -> dict:
    """Evaluate one chronological backtest or a genuinely new shadow sample.

    A backtest is split into fit/held-out partitions.  A shadow evaluation is
    already supplied as a later corpus, so every row is held out and there is
    deliberately no in-sample fit partition to accidentally reuse for a
    lifecycle transition.
    """
    ordered = sorted(candidate, key=lambda row: (str(row.get("session_date", "")),
                                                  str(row.get("entry_timestamp", ""))))
    base_ordered = sorted(baseline, key=lambda row: (str(row.get("session_date", "")),
                                                      str(row.get("entry_timestamp", ""))))
    if shadow:
        fit, heldout = [], ordered
        base_fit, base_heldout = [], base_ordered
    else:
        fit, heldout = chronological_split(ordered, fit_fraction=.7)
        fit_sessions = {str(row.get("session_date") or "") for row in fit}
        held_sessions = {str(row.get("session_date") or "") for row in heldout}
        base_fit = [row for row in base_ordered
                    if str(row.get("session_date") or "") in fit_sessions]
        base_heldout = [row for row in base_ordered
                       if str(row.get("session_date") or "") in held_sessions]
    fit_floor = structural_floor(
        fit, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions,
        required=not shadow)
    held_floor = structural_floor(
        heldout, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    overall_floor = structural_floor(
        ordered, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    separation = (heldout_separation(fit, heldout) if not shadow else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": bool(heldout), "mode": "new_data"})
    delta_all = paired_delta(ordered, baseline, vehicle=vehicle)
    delta_fit = (matched_cluster_test(fit, base_fit, vehicle=vehicle) if not shadow else
                 {"available": True, "actual_control": True, "matched": 0,
                  "mean_delta": None, "p_value": 1.0, "mode": "prior_backtest"})
    delta_held = matched_cluster_test(heldout, base_heldout, vehicle=vehicle)
    delta_fit["actual_control"] = bool(actual_control)
    delta_held["actual_control"] = bool(actual_control)
    placebo = deterministic_placebo_deltas(
        heldout, base_heldout, vehicle=vehicle)
    falsification = {
        **falsification_gate(placebo["observed"], placebo["placebo"]),
        "method": placebo["method"],
        "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
    }
    candidate_p = float(delta_held.get("p_value", 1.0))
    checks = {
        "fit_structurally_adequate": bool(fit_floor["adequate"]),
        "heldout_structurally_adequate": bool(held_floor["adequate"]),
        "separated": bool(separation["passes"]),
        "actual_control_available": bool(delta_held.get("available") and
                                         delta_held.get("actual_control")),
        "fit_delta_positive": bool(shadow or (
            delta_fit.get("mean_delta") is not None and float(delta_fit["mean_delta"]) > 0)),
        "heldout_delta_positive": bool(delta_held.get("mean_delta") is not None and
                                        float(delta_held["mean_delta"]) > 0),
        "heldout_p_significant": candidate_p <= float(alpha),
        "falsification": bool(falsification["passes"]),
    }
    passes_without_family = bool(
        all(checks.values()) and delta_all.get("mean_delta") is not None and
        float(delta_all["mean_delta"]) > 0)
    return {"vehicle": vehicle, "shadow": shadow,
            "alpha": float(alpha),
            "passes_without_family": passes_without_family,
            "candidate_p_raw": candidate_p,
            "floor": overall_floor, "fit_floor": fit_floor, "heldout_floor": held_floor,
            "heldout_separation": separation, "paired_baseline": delta_all,
            "fit_paired_baseline": delta_fit,
            "heldout_paired_baseline": delta_held,
            "control": {**delta_held, "kind": control_kind},
            "falsification": falsification,
            "checks_without_family": checks,
            "max_drawdown": max_drawdown_of(ordered),
            "fit_trades": sample_counts(fit, vehicle=vehicle)["trades"],
            "heldout_trades": sample_counts(heldout, vehicle=vehicle)["trades"],
            "fit_sessions": len({row.get("session_date") for row in fit}),
            "heldout_sessions": len({row.get("session_date") for row in heldout}),
            "_fit_rows": fit, "_heldout_rows": heldout}


def _finalize_gate(gate: dict, *, lane: str, family: Mapping) -> dict:
    checks = {**gate["checks_without_family"],
              "family_fdr_significant": bool(family.get("significant", False))}
    passes = bool(gate["passes_without_family"] and checks["family_fdr_significant"])
    gate["multiple_tests"] = {"candidate": dict(family),
                              "method": "benjamini_hochberg"}
    gate["passes"] = passes
    fit = gate.pop("_fit_rows")
    heldout = gate.pop("_heldout_rows")
    envelope = verified_gate_envelope(
        lane=lane, vehicle=gate["vehicle"], fit=fit, heldout=heldout,
        fit_floor=gate["fit_floor"], heldout_floor=gate["heldout_floor"],
        control=gate["control"], p_value=gate["candidate_p_raw"],
        q_value=float(family.get("p_adjusted", 1.0)), alpha=gate.get("alpha", 0.05),
        falsification=gate["falsification"], separation=gate["heldout_separation"],
        checks=checks, passes=passes,
        performance={"heldout_delta": gate["heldout_paired_baseline"].get("mean_delta"),
                     "max_drawdown": gate["max_drawdown"]})
    gate["verified_gate"] = envelope
    gate["gate_hash"] = envelope["content_hash"]
    return gate


def discover(data: str | Path | Sequence[Mapping], *, db_path: str | Path = DEFAULT_DB_PATH,
             vehicle: str = "equity", lane: str = "auto", config: Mapping | None = None,
             variants_path: str | Path | None = None, min_trades: int = 100,
             min_sessions: int = 10, alpha: float = .05) -> dict:
    """Run every bounded IBR variant on one normalized corpus.

    ``backtest`` writes only the fit/held-out replay and can advance a fresh
    candidate to ``backtest_passed``.  ``shadow`` requires an existing
    backtest-passed candidate and consumes only sessions strictly later than
    the latest persisted boundary.  ``auto`` chooses backtest for fresh arms
    and the same forward-only tail for already-qualified arms.  No result is
    invented for a missing trade or quote.
    """
    if vehicle not in VEHICLES:
        raise DiscoveryError("vehicle must be equity or option")
    if lane not in {"auto", "backtest", "shadow"}:
        raise DiscoveryError("lane must be auto, backtest, or shadow")
    raw_rows, bars, snapshots = _read_discovery_rows(data)
    from agent.variants import load_registry
    registry_path = variants_path or Path(__file__).with_name("variants.yaml")
    variants = load_registry(registry_path)
    selected = [variant for variant in variants.values()
                if variant.strategy_id == "ibr" and vehicle in variant.vehicles]
    baseline_variant = variants.get("ibr.baseline")
    if baseline_variant is None:
        raise DiscoveryError("research/variants.yaml must register ibr.baseline")
    if baseline_variant not in selected:
        selected.insert(0, baseline_variant)
    candidates = [variant for variant in selected
                  if variant.variant_id != baseline_variant.variant_id]
    ledger = EdgeLedger(db_path)
    code_path = Path(__file__)
    data_hash = content_hash(raw_rows)

    base_results: dict[str, list[dict]] = {}
    effective_configs: dict[str, dict] = {}
    for variant in selected:
        cfg, effective = _effective_ibr_config(config, variant.overrides)
        result = replay_ibr(bars, config=cfg, vehicle=vehicle,
                            option_snapshots=snapshots if vehicle == "option" else None)
        base_results[variant.variant_id] = _opportunity_rows(result, bars, vehicle)
        effective_configs[variant.variant_id] = effective

    def latest_boundary(record: Mapping | None) -> str | None:
        if not record:
            return None
        values = []
        for run in ledger.runs(record["candidate_id"]):
            value = run.get("heldout_end") or run.get("fit_end")
            if value:
                values.append(str(value))
        return max(values) if values else None

    existing = {variant.variant_id: ledger.candidate_by_variant(variant.variant_id, vehicle)
                for variant in selected}
    active = {"backtest_passed", "shadow", "validated", "champion"}
    if lane == "shadow":
        if existing.get(baseline_variant.variant_id) is None:
            raise DiscoveryError(
                "shadow requires a prior backtest baseline; run edge discover --lane backtest first")
        for variant in candidates:
            record = existing.get(variant.variant_id)
            if record is None:
                raise DiscoveryError(
                    f"shadow requires prior backtest candidate {variant.variant_id!r}")
            if record.get("status") not in active:
                raise DiscoveryError(
                    f"candidate {variant.variant_id!r} is {record.get('status')!r}; "
                    "shadow requires backtest_passed or later")

    baseline_rows = base_results[baseline_variant.variant_id]
    baseline_boundary = latest_boundary(existing.get(baseline_variant.variant_id))
    baseline_mode = "shadow" if (
        lane == "shadow" or (lane == "auto" and baseline_boundary is not None)
    ) else "backtest"

    def tail(rows: Sequence[Mapping], boundary: str | None) -> list[dict]:
        if boundary is None:
            return [dict(row) for row in rows]
        return [dict(row) for row in rows
                if str(row.get("session_date") or "") > str(boundary)]

    baseline_eval = tail(baseline_rows, baseline_boundary) if baseline_mode == "shadow" else baseline_rows
    if lane == "shadow" and not baseline_eval:
        raise DiscoveryError("shadow corpus contains no unseen sessions after the persisted boundary")

    modes: dict[str, str] = {}
    eval_rows: dict[str, list[dict]] = {}
    for variant in candidates:
        record = existing.get(variant.variant_id)
        if record is not None and record.get("status") in {"retired", "demoted"}:
            # A failed, adequately-powered evaluation closes this hypothesis
            # for every lane, including an explicitly requested backtest.
            mode = "skip"
        elif lane == "backtest":
            mode = "backtest"
        elif lane == "shadow":
            mode = "shadow"
        elif record is not None and record.get("status") in active:
            mode = "shadow"
        else:
            mode = "backtest"
        modes[variant.variant_id] = mode
        boundary = latest_boundary(record)
        rows = (tail(base_results[variant.variant_id], boundary)
                if mode == "shadow" else
                ([] if mode == "skip" else base_results[variant.variant_id]))
        eval_rows[variant.variant_id] = rows
        if lane == "shadow" and not rows:
            raise DiscoveryError(
                f"shadow corpus contains no unseen sessions for {variant.variant_id!r}")

    baseline_zero = [{**row, "net_pnl": 0.0, "return_value": 0.0}
                     for row in baseline_eval]
    gates = {}
    for variant in candidates:
        mode = modes[variant.variant_id]
        if mode == "skip":
            continue
        gates[variant.variant_id] = _discover_gate(
            eval_rows[variant.variant_id], baseline_eval,
            vehicle=vehicle, min_trades=min_trades,
            min_sessions=min_sessions, alpha=alpha, shadow=(mode == "shadow"))
    corrected = benjamini_hochberg(
        {variant_id: gate["candidate_p_raw"] for variant_id, gate in gates.items()}, alpha=alpha)

    # Attach the family decision before writing any shadow rows.  A failed or
    # under-powered forward check is not "consumed": the same unseen tail may
    # be reconsidered when the append-only recorder supplies more sessions.
    for variant in candidates:
        if modes[variant.variant_id] == "skip":
            continue
        gate = gates[variant.variant_id]
        family = corrected.get(variant.variant_id, {"p": gate["candidate_p_raw"],
                                                     "p_adjusted": 1.0,
                                                     "significant": False})
        _finalize_gate(gate, lane=modes[variant.variant_id], family=family)
    forward_success = any(
        modes[variant.variant_id] == "shadow" and
        gates.get(variant.variant_id, {}).get("passes", False)
        for variant in candidates)

    results = []
    baseline_record = ledger.register_candidate(
        baseline_variant.variant_id, vehicle=vehicle, strategy_id="ibr",
        base_version=baseline_variant.base_version, hypothesis=baseline_variant.hypothesis,
        config=effective_configs[baseline_variant.variant_id], dataset=raw_rows,
        code=code_path, provenance={"lane": lane, "vehicle": vehicle, "role": "baseline"},
        overrides=baseline_variant.overrides)
    baseline_gate = _discover_gate(
        baseline_eval, baseline_zero, vehicle=vehicle,
        min_trades=min_trades, min_sessions=min_sessions, alpha=alpha,
        shadow=(baseline_mode == "shadow"), actual_control=False,
        control_kind="synthetic_zero_reference")
    _finalize_gate(
        baseline_gate, lane=baseline_mode,
        family={"p": baseline_gate["candidate_p_raw"],
                "p_adjusted": baseline_gate["candidate_p_raw"],
                "significant": baseline_gate["candidate_p_raw"] <= alpha,
                "family_size": 1})
    baseline_run = None
    baseline_adequate = bool(
        baseline_gate.get("fit_floor", {}).get("adequate") and
        baseline_gate.get("heldout_floor", {}).get("adequate"))
    if baseline_eval and not baseline_adequate:
        ledger.append_event(
            candidate_id=baseline_record["candidate_id"],
            event_type="insufficient_data", actor="edge_lab",
            reason="baseline sample floor not met; no observations consumed",
            payload={"dataset_hash": data_hash, "mode": baseline_mode,
                     "rows": len(baseline_eval),
                     "trades": baseline_gate.get("floor", {}).get("trades", 0),
                     "sessions": baseline_gate.get("floor", {}).get("sessions", 0),
                     "boundary": baseline_boundary})
    if baseline_eval and baseline_adequate and (baseline_mode != "shadow" or forward_success):
        if baseline_mode == "shadow":
            baseline_fit, baseline_held = [], baseline_eval
        else:
            baseline_fit, baseline_held = chronological_split(baseline_eval, fit_fraction=.7)
        baseline_run = ledger.append_run(
            baseline_record["candidate_id"], lane=baseline_mode, vehicle=vehicle,
            dataset=raw_rows, config=effective_configs[baseline_variant.variant_id], code=code_path,
            provenance={"lane": lane, "vehicle": vehicle, "role": "baseline"},
            fit=baseline_fit, heldout=baseline_held,
            metrics={"gate": baseline_gate, "role": "baseline"})
        for row in baseline_eval:
            ledger.append_trade(baseline_run["run_id"], row)
        ledger.record_verified_gate(baseline_run["run_id"], baseline_gate)
        ledger.append_evidence(baseline_record["candidate_id"], "baseline_control",
                               baseline_gate, run_id=baseline_run["run_id"])

    for variant in candidates:
        mode = modes[variant.variant_id]
        rows = eval_rows[variant.variant_id]
        record = ledger.register_candidate(
            variant.variant_id, vehicle=vehicle, strategy_id="ibr",
            base_version=variant.base_version, hypothesis=variant.hypothesis,
            config=effective_configs[variant.variant_id], dataset=raw_rows,
            code=code_path, provenance={"lane": lane, "vehicle": vehicle},
            overrides=variant.overrides)
        if mode == "skip":
            results.append({"variant_id": variant.variant_id, "vehicle": vehicle,
                            "candidate_id": record["candidate_id"],
                            "status": record.get("status", "retired"),
                            "gate": None, "shadow_gate": None,
                            "run_id": None, "shadow_run_id": None,
                            "mode": mode, "unseen_sessions": None})
            continue
        gate = gates[variant.variant_id]
        family = gate["multiple_tests"]["candidate"]
        adequate = bool(gate.get("fit_floor", {}).get("adequate") and
                        gate.get("heldout_floor", {}).get("adequate"))
        run = None
        shadow_run = None
        status = record.get("status", "candidate")
        if not rows and mode == "shadow":
            ledger.append_event(
                candidate_id=record["candidate_id"], event_type="insufficient_data",
                actor="edge_lab", reason="no unseen sessions; no observations consumed",
                payload={"dataset_hash": data_hash, "mode": mode,
                         "rows": 0, "trades": 0, "sessions": 0,
                         "boundary": latest_boundary(record)})
        elif rows and not adequate:
            ledger.append_event(
                candidate_id=record["candidate_id"], event_type="insufficient_data",
                actor="edge_lab", reason="sample floor not met; no observations consumed",
                payload={"dataset_hash": data_hash, "mode": mode,
                         "rows": len(rows),
                         "trades": gate.get("floor", {}).get("trades", 0),
                         "sessions": gate.get("floor", {}).get("sessions", 0),
                         "heldout_trades": gate.get("heldout_floor", {}).get("trades", 0),
                         "heldout_sessions": gate.get("heldout_floor", {}).get("sessions", 0),
                         "boundary": latest_boundary(record)})
        elif rows:
            if mode == "shadow":
                fit_rows, heldout_rows = [], rows
            else:
                fit_rows, heldout_rows = chronological_split(rows, fit_fraction=.7)
            run = ledger.append_run(
                record["candidate_id"], lane=mode, vehicle=vehicle, dataset=raw_rows,
                config=effective_configs[variant.variant_id], code=code_path,
                provenance={"lane": lane, "vehicle": vehicle},
                fit=fit_rows, heldout=heldout_rows,
                metrics={"gate": gate, "confidence": 1.0 - family.get("p_adjusted", 1.0),
                         "heldout_delta": gate["heldout_paired_baseline"].get("mean_delta") or 0.0,
                         "max_drawdown": gate["max_drawdown"],
                         "heldout_trades": gate["heldout_trades"],
                         "role": "forward_shadow" if mode == "shadow" else "candidate"})
            for row in rows:
                ledger.append_trade(run["run_id"], row)
            ledger.record_verified_gate(run["run_id"], gate)
            ledger.append_evidence(record["candidate_id"],
                                   "shadow_gate" if mode == "shadow" else "gate_report",
                                   gate, run_id=run["run_id"])
        if adequate and gate["passes"] and mode == "backtest" and lane in {"backtest", "auto"} \
                and status == "candidate":
            ledger.transition(record["candidate_id"], "backtest_passed",
                              reason="pre-registered backtest gates passed")
            status = "backtest_passed"
        if adequate and gate["passes"] and mode == "shadow" and status in {"backtest_passed", "shadow"}:
            if status == "backtest_passed":
                ledger.transition(record["candidate_id"], "shadow",
                                  reason="later unseen shadow lane started")
                status = "shadow"
            shadow_run = run
            if status == "shadow":
                ledger.transition(record["candidate_id"], "validated",
                                  reason="later unseen shadow gates passed")
                status = "validated"
        elif adequate and not gate["passes"] and status not in {"retired", "demoted"}:
            target = "demoted" if status in {"shadow", "validated", "champion"} else "retired"
            ledger.transition(record["candidate_id"], target,
                              reason=f"{mode} evidence failed mandatory gates")
            status = target
        results.append({"variant_id": variant.variant_id, "vehicle": vehicle,
                        "candidate_id": record["candidate_id"], "status": status,
                        "gate": gate,
                        "shadow_gate": gate if mode == "shadow" else None,
                        "run_id": run["run_id"] if run else None,
                        "shadow_run_id": shadow_run["run_id"] if shadow_run else None,
                        "mode": mode, "unseen_sessions": len(rows) if mode == "shadow" else None})
    champion = ledger.select_champion(vehicle=vehicle) if lane in {"auto", "shadow"} else None
    return {"vehicle": vehicle, "lane": lane, "dataset_hash": data_hash,
            "variants": results, "family_correction": corrected,
            "baseline": {"candidate_id": baseline_record["candidate_id"],
                         "run_id": baseline_run["run_id"] if baseline_run else None,
                         "gate": baseline_gate, "mode": baseline_mode,
                         "unseen_sessions": len(baseline_eval) if baseline_mode == "shadow" else None},
            "champion": champion}


__all__ = ["BACKTEST_PASSED", "CANDIDATE", "CHAMPION", "DEFAULT_DB_PATH",
           "DEMOTED", "DiscoveryError", "EdgeLedger", "LANES", "LIFECYCLE",
           "RETIRED", "SHADOW", "VALIDATED", "VEHICLES", "canonical_json",
           "content_hash", "discover", "hash_config", "hash_dataset",
           "hash_file", "hash_provenance", "init_db", "init_ledger",
           "provenance_hash"]
