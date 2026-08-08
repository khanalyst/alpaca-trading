"""Append-only provenance for nondeterministic research authoring calls.

The staging registry stores only contracts that passed validation.  That is
the right execution boundary, but it used to erase provider failures, malformed
responses, and rejected proposals.  This small companion migration lives in
the same SQLite file so an operator can reconstruct every authoring attempt
without introducing another service or a second path to configure.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


SCHEMA = 1


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def migrate(path: str | Path) -> None:
    """Add the audit ledger without changing the staging registry schema."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(target)) as connection, connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS authoring_attempts (
                attempt_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                requested_ts REAL NOT NULL,
                completed_ts REAL NOT NULL,
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                request_json TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                context_hash TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                raw_response TEXT,
                parser_status TEXT NOT NULL
                    CHECK (parser_status IN ('NOT_RUN','SUCCEEDED','FAILED')),
                validation_status TEXT NOT NULL
                    CHECK (validation_status IN
                           ('NOT_RUN','ACCEPTED','PARTIAL','REJECTED')),
                error TEXT,
                returned_contract_ids_json TEXT NOT NULL,
                accepted_contract_ids_json TEXT NOT NULL,
                rejections_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('SUCCEEDED','FAILED'))
            );
            CREATE INDEX IF NOT EXISTS authoring_attempts_completed
                ON authoring_attempts(completed_ts, attempt_id);
            CREATE TRIGGER IF NOT EXISTS authoring_attempts_no_update
                BEFORE UPDATE ON authoring_attempts BEGIN
                    SELECT RAISE(ABORT, 'authoring attempts are immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS authoring_attempts_no_delete
                BEFORE DELETE ON authoring_attempts BEGIN
                    SELECT RAISE(ABORT, 'authoring attempts are immutable');
                END;
            CREATE TABLE IF NOT EXISTS authoring_provenance_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        connection.execute(
            "INSERT OR IGNORE INTO authoring_provenance_meta VALUES "
            "('schema', ?)", (str(SCHEMA),))


def record_authoring_attempt(
        path: str | Path, *, generation: int, requested_ts: float,
        completed_ts: float, provider: str, model_id: str,
        prompt_version: str, request: dict, context: object,
        evidence: object, raw_response: str | None, parser_status: str,
        validation_status: str, error: str | None,
        returned_contract_ids: list[str], accepted_contract_ids: list[str],
        rejections: list[dict], result: dict, status: str) -> dict:
    """Persist one complete attempt and return its stable audit identifiers."""
    migrate(path)
    attempt_id = uuid.uuid4().hex
    request_json = canonical_json(request)
    row = {
        "attempt_id": attempt_id,
        "generation": int(generation),
        "requested_ts": float(requested_ts),
        "completed_ts": float(completed_ts),
        "provider": str(provider or "unknown"),
        "model_id": str(model_id or "unknown"),
        "prompt_version": str(prompt_version),
        "request_json": request_json,
        "request_hash": hashlib.sha256(
            request_json.encode("utf-8")).hexdigest(),
        "context_hash": content_hash(context),
        "evidence_hash": content_hash(evidence),
        # Deliberately untruncated. This table is the audit source; bounded
        # process output belongs to the scheduler, not to evidence retention.
        "raw_response": raw_response,
        "parser_status": str(parser_status),
        "validation_status": str(validation_status),
        "error": str(error) if error is not None else None,
        "returned_contract_ids_json": canonical_json(returned_contract_ids),
        "accepted_contract_ids_json": canonical_json(accepted_contract_ids),
        "rejections_json": canonical_json(rejections),
        "result_json": canonical_json(result),
        "status": str(status),
    }
    with closing(_connect(path)) as connection, connection:
        connection.execute("""
            INSERT INTO authoring_attempts (
                attempt_id, generation, requested_ts, completed_ts, provider,
                model_id, prompt_version, request_json, request_hash,
                context_hash, evidence_hash, raw_response, parser_status,
                validation_status, error, returned_contract_ids_json,
                accepted_contract_ids_json, rejections_json, result_json,
                status)
            VALUES (:attempt_id, :generation, :requested_ts, :completed_ts,
                :provider, :model_id, :prompt_version, :request_json,
                :request_hash, :context_hash, :evidence_hash, :raw_response,
                :parser_status, :validation_status, :error,
                :returned_contract_ids_json, :accepted_contract_ids_json,
                :rejections_json, :result_json, :status)
        """, row)
    return {
        "attempt_id": attempt_id,
        "request_hash": row["request_hash"],
        "context_hash": row["context_hash"],
        "evidence_hash": row["evidence_hash"],
    }


def authoring_attempts(path: str | Path, *, limit: int = 50) -> list[dict]:
    """Read attempts for tests and operational views, newest first."""
    migrate(path)
    with closing(_connect(path)) as connection:
        rows = connection.execute(
            "SELECT * FROM authoring_attempts "
            "ORDER BY completed_ts DESC, attempt_id DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for source, target in (
                ("request_json", "request"),
                ("returned_contract_ids_json", "returned_contract_ids"),
                ("accepted_contract_ids_json", "accepted_contract_ids"),
                ("rejections_json", "rejections"),
                ("result_json", "result")):
            item[target] = json.loads(item.pop(source))
        result.append(item)
    return result
