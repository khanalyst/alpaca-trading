"""Immutable schema, hashing, and SQLite setup for the research edge ledger."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


VEHICLES = ("equity", "option")
LANES = ("backtest", "shadow")
LIFECYCLE = ("candidate", "backtest_passed", "shadow", "validated", "champion",
             "retired", "demoted")
CANDIDATE, BACKTEST_PASSED, SHADOW, VALIDATED, CHAMPION, RETIRED, DEMOTED = LIFECYCLE
DEFAULT_DB_PATH = Path(os.getenv(
    "ALPACA_EDGE_DB",
    str(Path(__file__).resolve().parents[1] / "runtime" / "research" / "edge_lab.sqlite3")))
SCHEMA_VERSION = 3
# The replay-correctness generation that produced a run.  This is not the
# storage schema: rows stay readable across epochs, but evidence recorded by an
# older replay engine cannot authorize deployment, because the fills, quote-age
# bounds, partition arithmetic and multiplicity accounting it was measured
# under are not the ones the current gates assume.
#
# Epoch 2 is the first to carry point-in-time option entry pricing, bounded and
# session-scoped equity quote ages, per-signal (not per-session) bar adjacency,
# runtime portfolio limits inside the simulated book, contiguous multi-session
# walk-forward folds, post-selection sealed-window qualification, and a
# cumulative cross-cycle false-discovery budget.  Epoch 1 evidence predates all
# of it and is quarantined rather than deleted: the rows remain auditable, they
# simply cannot promote anything.
#
# Bump this whenever a replay or gate change invalidates previously recorded
# runs.  Runs are stamped at ``append_run``; a run with no stamp is epoch 1.
REPLAY_ENGINE_EPOCH = 2
PAPER_DEMOTION_MIN_OUTCOMES = 20
PAPER_DEMOTION_R_FLOOR = -2.0


def canonical_json(value: Any) -> str:
    """Encode a finite JSON value deterministically for hashing and storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def content_hash(value: Any) -> str:
    streamed = getattr(value, "content_hash", None)
    if callable(streamed):
        return str(streamed())
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
                proof_run_id TEXT REFERENCES runs(run_id),
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
        # ``proof_run_id`` scopes live paper observations to the shadow proof
        # epoch that authorized the deployment.  Existing ledgers predate the
        # column; nullable migration preserves their historical rows while a
        # newly proved epoch can no longer inherit them.
        paper_columns = {row[1] for row in db.execute(
            "PRAGMA table_info(paper_outcomes)").fetchall()}
        if "proof_run_id" not in paper_columns:
            db.execute("ALTER TABLE paper_outcomes ADD COLUMN proof_run_id TEXT")
        db.execute("INSERT OR REPLACE INTO ledger_meta(key,value) VALUES('schema',?)",
                   (str(SCHEMA_VERSION),))
    return {"db_path": str(target), "schema": SCHEMA_VERSION}


init_db = init_ledger


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


__all__ = [
    "BACKTEST_PASSED", "CANDIDATE", "CHAMPION", "DEFAULT_DB_PATH",
    "DEMOTED", "LANES", "LIFECYCLE", "PAPER_DEMOTION_MIN_OUTCOMES",
    "PAPER_DEMOTION_R_FLOOR", "RETIRED", "SCHEMA_VERSION", "SHADOW",
    "VALIDATED", "VEHICLES", "canonical_json", "content_hash", "hash_config",
    "hash_dataset", "hash_file", "hash_provenance", "init_db", "init_ledger",
    "provenance_hash",
]
