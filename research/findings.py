"""The findings store: append-only, so a rejection stays legible afterwards.

Intention #5 - every learning and recommendation persisted, per strategy and
per variant, reviewable later. ``report.py`` printed to stdout and forgot,
which meant the reasoning behind a decision survived only as long as the
terminal scrollback.

Two design commitments, both about what happens to negative results.

**Findings are append-only.** A rejection is a row, not a deletion. Six
months from now the question that matters is not "which variants are alive"
but "why was this rejected, and on what sample" - and if the answer was
deleted the same idea comes back, gets tested again, and consumes the same
calendar time a second time.

**Null results are recorded with the same weight as positive ones.** A
programme that only writes down what worked is a programme that only writes
down noise: at these sample sizes, filtering for positives is filtering for
the largest random numbers. ``INSUFFICIENT_SAMPLE`` is a finding, and often
the most useful one, because it says the question is still open rather than
answered in the negative.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path


DEFAULT_STORE = Path(__file__).resolve().parent / "cache" / "findings.db"
SCHEMA_VERSION = 7

KINDS = ("observation", "recommendation", "decision")


def resolve_store_path(configured: str | Path | None = None) -> Path:
    """Resolve configured storage deterministically against the repository."""
    if configured is None or not str(configured).strip():
        return DEFAULT_STORE
    path = Path(configured)
    return path if path.is_absolute() else DEFAULT_STORE.parents[2] / path


def _json_safe(value: object):
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_safe(value), sort_keys=True, separators=(",", ":"), default=str,
        allow_nan=False)


def _content_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_axis(axis: object) -> list[str]:
    if (not isinstance(axis, list) or not axis
            or not all(isinstance(path, str) and path.strip() for path in axis)):
        raise ValueError("forward axis must be a non-empty list of dotted paths")
    normalized = sorted(path.strip() for path in axis)
    if len(set(normalized)) != len(normalized):
        raise ValueError("forward axis contains duplicate paths")
    return normalized


def _dotted_value(mapping: dict, path: str):
    node = mapping
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(
                f"forward axis path {path!r} is not executable configuration")
        node = node[part]
    return node


def _without_dotted_paths(mapping: dict, paths: list[str]) -> dict:
    result = json.loads(_canonical_json(mapping))
    for path in paths:
        parts = path.split(".")
        node = result
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                raise ValueError(
                    f"forward axis path {path!r} is not executable configuration")
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            raise ValueError(
                f"forward axis path {path!r} is not executable configuration")
        del node[parts[-1]]
    return result


def _analysis_canonical(
        kind: str, subject_id: str, payload: dict) -> tuple[str, str]:
    payload_json = _canonical_json(payload)
    digest = _content_hash({
        "kind": str(kind), "subject_id": str(subject_id),
        "payload": json.loads(payload_json),
    })
    return payload_json, digest


def _rewrite_analysis_references(value: object, remap: dict[str, str]):
    """Rewrite structured analysis references through a final-id mapping."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if (key in {"analysis_id", "source_analysis_id"}
                    and isinstance(item, str)):
                out[key] = remap.get(item, item)
            else:
                out[key] = _rewrite_analysis_references(item, remap)
        return out
    if isinstance(value, list):
        return [_rewrite_analysis_references(item, remap) for item in value]
    return value


def _variant_identity(row_or_variant) -> dict:
    if isinstance(row_or_variant, sqlite3.Row):
        overrides = json.loads(row_or_variant["overrides_json"])
        return {
            "variant_id": str(row_or_variant["variant_id"]),
            "strategy_id": str(row_or_variant["strategy_id"]),
            "base_version": str(row_or_variant["base_version"]),
            "overrides": overrides,
            "hypothesis": str(row_or_variant["hypothesis"]),
        }
    return {
        "variant_id": str(row_or_variant.variant_id),
        "strategy_id": str(row_or_variant.strategy_id),
        "base_version": str(row_or_variant.base_version),
        "overrides": dict(row_or_variant.overrides),
        "hypothesis": str(row_or_variant.hypothesis),
    }


def variant_identity_hash(row_or_variant) -> str:
    return _content_hash(_variant_identity(row_or_variant))


FORWARD_TRADE_FIELDS = (
    "trade_id", "proposal_id", "scope_key", "variant_id", "cycle_id",
    "symbol", "direction", "setup_type", "signal_ts", "model_id",
    "entry_ts", "entry_price", "notional", "risk_usd", "stop_price",
    "take_price", "exit_ts", "exit_price", "result", "net_pnl_usd",
    "r_multiple", "status", "failure",
)

FORWARD_DECISION_FIELDS = (
    "decision_id", "proposal_id", "scope_key", "variant_id", "cycle_id",
    "symbol", "direction", "setup_type", "signal_ts", "confidence",
    "decision_outcome", "reason", "paper_trade_id", "model_id",
    "decision_ts", "trade_proposal_id", "trade_scope_key",
    "trade_variant_id", "trade_model_id", "trade_entry_ts",
    "trade_exit_ts", "trade_result", "trade_r_multiple", "trade_status",
)


def _forward_trade_evidence(row: dict | sqlite3.Row) -> dict:
    item = {field: row[field] for field in FORWARD_TRADE_FIELDS}
    try:
        assumptions = json.loads(row["assumptions_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"paper trade {row['trade_id']} has invalid assumptions") from exc
    if not isinstance(assumptions, dict):
        raise ValueError(
            f"paper trade {row['trade_id']} assumptions are not a mapping")
    item["assumptions"] = assumptions
    return json.loads(_canonical_json(item))


def _forward_decision_evidence(row: dict | sqlite3.Row) -> dict:
    """Canonical evidence for every evaluated proposal, including vetoes."""
    item = {field: row[field] for field in FORWARD_DECISION_FIELDS}
    try:
        assumptions = json.loads(row["assumptions_json"])
        proposal = json.loads(row["proposal_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"paper decision {row['decision_id']} has invalid JSON") from exc
    if not isinstance(assumptions, dict) or not isinstance(proposal, dict):
        raise ValueError(
            f"paper decision {row['decision_id']} evidence is not a mapping")
    item["assumptions"] = assumptions
    item["proposal"] = proposal
    return json.loads(_canonical_json(item))


class MigrationError(RuntimeError):
    """The findings database cannot be opened without risking its history."""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, factory=_ClosingConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        _migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a SQL script without sqlite3's implicit pre-script commit."""
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise MigrationError("incomplete SQL statement in findings migration")


def _t3_canonical(
        variant_id: str, payload: dict, review_status: str,
        reviewed_by: str | None, registry_change_ref: str | None) -> tuple[str, str]:
    payload_json = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
        allow_nan=False)
    envelope = json.dumps({
        "variant_id": variant_id,
        "review_status": review_status,
        "reviewed_by": reviewed_by or None,
        "registry_change_ref": registry_change_ref or None,
        "payload": json.loads(payload_json),
    }, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return payload_json, hashlib.sha256(envelope.encode("utf-8")).hexdigest()


def _stored_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_meta"):
        # The pre-migration findings store already had the four core tables.
        return 1 if _table_exists(conn, "variants") else 0
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        return 1 if _table_exists(conn, "variants") else 0
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise MigrationError("findings schema version is not an integer") from exc


def _migration_1(conn: sqlite3.Connection) -> None:
    _execute_script(conn, """
        CREATE TABLE IF NOT EXISTS variants (
            variant_id TEXT PRIMARY KEY, strategy_id TEXT,
            base_version TEXT, overrides_json TEXT, hypothesis TEXT,
            status TEXT, created_ts REAL, updated_ts REAL);
        CREATE TABLE IF NOT EXISTS variant_runs (
            run_id TEXT PRIMARY KEY, variant_id TEXT, corpus_from_ts REAL,
            corpus_to_ts REAL, corpus_cycles INTEGER, mode TEXT,
            code_version TEXT, scorer_version TEXT, ts REAL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE IF NOT EXISTS variant_results (
            run_id TEXT, metric TEXT, value REAL, ci_low REAL,
            ci_high REAL, n INTEGER,
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id),
            UNIQUE (run_id, metric));
        CREATE TABLE IF NOT EXISTS findings (
            finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id TEXT, ts REAL, author TEXT, kind TEXT, text TEXT,
            run_id TEXT,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id),
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id));
        CREATE INDEX IF NOT EXISTS findings_variant
            ON findings (variant_id, ts);
        CREATE UNIQUE INDEX IF NOT EXISTS variant_result_metric
            ON variant_results (run_id, metric);
    """)


def _drop_history_triggers(conn: sqlite3.Connection) -> None:
    _execute_script(conn, """
        DROP TRIGGER IF EXISTS findings_no_update;
        DROP TRIGGER IF EXISTS findings_no_delete;
        DROP TRIGGER IF EXISTS variant_runs_no_update;
        DROP TRIGGER IF EXISTS variant_runs_no_delete;
        DROP TRIGGER IF EXISTS variant_results_no_update;
        DROP TRIGGER IF EXISTS variant_results_no_delete;
    """)


def _install_history_triggers(conn: sqlite3.Connection) -> None:
    _execute_script(conn, """
        CREATE TRIGGER IF NOT EXISTS findings_no_update
            BEFORE UPDATE ON findings BEGIN
                SELECT RAISE(ABORT, 'findings are append-only');
            END;
        CREATE TRIGGER IF NOT EXISTS findings_no_delete
            BEFORE DELETE ON findings BEGIN
                SELECT RAISE(ABORT, 'findings are append-only');
            END;
        CREATE TRIGGER IF NOT EXISTS variant_runs_no_update
            BEFORE UPDATE ON variant_runs BEGIN
                SELECT RAISE(ABORT, 'variant runs are immutable');
            END;
        CREATE TRIGGER IF NOT EXISTS variant_runs_no_delete
            BEFORE DELETE ON variant_runs BEGIN
                SELECT RAISE(ABORT, 'variant runs are immutable');
            END;
        CREATE TRIGGER IF NOT EXISTS variant_results_no_update
            BEFORE UPDATE ON variant_results BEGIN
                SELECT RAISE(ABORT, 'variant results are immutable');
            END;
        CREATE TRIGGER IF NOT EXISTS variant_results_no_delete
            BEFORE DELETE ON variant_results BEGIN
                SELECT RAISE(ABORT, 'variant results are immutable');
            END;
    """)


def _migration_2(conn: sqlite3.Connection) -> None:
    """Rebuild legacy tables so constraints are real, without losing rows."""
    _drop_history_triggers(conn)
    conn.execute("DROP INDEX IF EXISTS variant_result_metric")
    conn.execute("DROP INDEX IF EXISTS findings_variant")
    _execute_script(conn, """
        ALTER TABLE variants RENAME TO variants_legacy;
        ALTER TABLE variant_runs RENAME TO variant_runs_legacy;
        ALTER TABLE variant_results RENAME TO variant_results_legacy;
        ALTER TABLE findings RENAME TO findings_legacy;

        CREATE TABLE variants (
            variant_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            base_version TEXT NOT NULL,
            overrides_json TEXT NOT NULL,
            hypothesis TEXT NOT NULL,
            status TEXT NOT NULL,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL);
        CREATE TABLE variant_runs (
            run_id TEXT PRIMARY KEY,
            variant_id TEXT NOT NULL,
            corpus_from_ts REAL,
            corpus_to_ts REAL,
            corpus_cycles INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL,
            code_version TEXT NOT NULL DEFAULT '',
            scorer_version TEXT NOT NULL DEFAULT '',
            ts REAL NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE variant_results (
            run_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL,
            ci_low REAL,
            ci_high REAL,
            n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, metric),
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id));
        CREATE TABLE findings (
            finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id TEXT NOT NULL,
            ts REAL NOT NULL,
            author TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            run_id TEXT,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id),
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id));
    """)
    conn.execute("""
        INSERT INTO variants
        SELECT variant_id, COALESCE(strategy_id, 'legacy'),
               COALESCE(base_version, 'legacy'), COALESCE(overrides_json, '{}'),
               COALESCE(hypothesis, 'Legacy record; hypothesis was not stored.'),
               COALESCE(status, 'testing'), COALESCE(created_ts, 0),
               COALESCE(updated_ts, created_ts, 0)
        FROM variants_legacy
    """)
    conn.execute("""
        INSERT INTO variant_runs
        SELECT run_id, variant_id, corpus_from_ts, corpus_to_ts,
               COALESCE(corpus_cycles, 0), COALESCE(mode, 'unknown'),
               COALESCE(code_version, ''), COALESCE(scorer_version, ''),
               COALESCE(ts, 0)
        FROM variant_runs_legacy
    """)
    conn.execute("""
        INSERT INTO variant_results
        SELECT run_id, metric, value, ci_low, ci_high, COALESCE(n, 0)
        FROM variant_results_legacy
    """)
    conn.execute("""
        INSERT INTO findings
        SELECT finding_id, variant_id, COALESCE(ts, 0),
               COALESCE(author, 'legacy'), COALESCE(kind, 'observation'),
               COALESCE(text, ''), NULLIF(run_id, '')
        FROM findings_legacy
    """)
    _execute_script(conn, """
        DROP TABLE findings_legacy;
        DROP TABLE variant_results_legacy;
        DROP TABLE variant_runs_legacy;
        DROP TABLE variants_legacy;
        CREATE INDEX findings_variant ON findings (variant_id, ts);
    """)
    _install_history_triggers(conn)


def _migration_3(conn: sqlite3.Connection) -> None:
    _execute_script(conn, """
        CREATE TABLE variant_scheduler (
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            evaluations INTEGER NOT NULL DEFAULT 0,
            skips INTEGER NOT NULL DEFAULT 0,
            last_evaluated_ts REAL,
            last_skipped_ts REAL,
            PRIMARY KEY (scope_key, variant_id),
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE paper_portfolios (
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            state_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_ts REAL NOT NULL,
            revoked_ts REAL,
            revoke_reason TEXT,
            PRIMARY KEY (scope_key, variant_id),
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL UNIQUE,
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            cycle_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            setup_type TEXT,
            signal_ts REAL,
            model_id TEXT NOT NULL,
            assumptions_json TEXT NOT NULL,
            entry_ts REAL NOT NULL,
            entry_price REAL NOT NULL,
            notional REAL NOT NULL,
            risk_usd REAL NOT NULL,
            stop_price REAL NOT NULL,
            take_price REAL NOT NULL,
            exit_ts REAL,
            exit_price REAL,
            result TEXT,
            net_pnl_usd REAL,
            r_multiple REAL,
            status TEXT NOT NULL,
            failure TEXT,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE INDEX paper_trades_variant_ts
            ON paper_trades (scope_key, variant_id, entry_ts);
        CREATE TABLE paper_failures (
            failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE run_evidence (
            run_id TEXT PRIMARY KEY,
            evidence_json TEXT NOT NULL,
            created_ts REAL NOT NULL,
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id));
        CREATE TABLE analysis_runs (
            analysis_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            ts REAL NOT NULL,
            payload_json TEXT NOT NULL);
        CREATE TABLE t3_evidence_packets (
            packet_id TEXT PRIMARY KEY,
            variant_id TEXT NOT NULL,
            created_ts REAL NOT NULL,
            review_status TEXT NOT NULL,
            reviewed_by TEXT,
            registry_change_ref TEXT,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TRIGGER t3_packets_no_update
            BEFORE UPDATE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
        CREATE TRIGGER t3_packets_no_delete
            BEFORE DELETE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
    """)


def _migration_4(conn: sqlite3.Connection) -> None:
    """Scope paper proposal identity and append qualification history."""
    _execute_script(conn, """
        DROP INDEX IF EXISTS paper_trades_variant_ts;
        ALTER TABLE paper_trades RENAME TO paper_trades_legacy;
        CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            cycle_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            setup_type TEXT,
            signal_ts REAL,
            model_id TEXT NOT NULL,
            assumptions_json TEXT NOT NULL,
            entry_ts REAL NOT NULL,
            entry_price REAL NOT NULL,
            notional REAL NOT NULL,
            risk_usd REAL NOT NULL,
            stop_price REAL NOT NULL,
            take_price REAL NOT NULL,
            exit_ts REAL,
            exit_price REAL,
            result TEXT,
            net_pnl_usd REAL,
            r_multiple REAL,
            status TEXT NOT NULL,
            failure TEXT,
            UNIQUE (scope_key, variant_id, proposal_id),
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        INSERT INTO paper_trades
            SELECT * FROM paper_trades_legacy;
        DROP TABLE paper_trades_legacy;
        CREATE INDEX paper_trades_variant_ts
            ON paper_trades (scope_key, variant_id, entry_ts);
        CREATE TABLE edge_qualification_events (
            event_id TEXT PRIMARY KEY,
            variant_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('QUALIFIED','REVOKED')),
            ts REAL NOT NULL,
            source_analysis_id TEXT,
            detail_json TEXT NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE INDEX edge_qualification_variant_ts
            ON edge_qualification_events (variant_id, ts, event_id);
        CREATE TRIGGER edge_qualification_no_update
            BEFORE UPDATE ON edge_qualification_events BEGIN
                SELECT RAISE(ABORT, 'qualification history is immutable');
            END;
        CREATE TRIGGER edge_qualification_no_delete
            BEFORE DELETE ON edge_qualification_events BEGIN
                SELECT RAISE(ABORT, 'qualification history is immutable');
            END;
    """)


def _migration_5(conn: sqlite3.Connection) -> None:
    """Make every T3 packet content-addressed and independently verifiable."""
    conn.execute("DROP TRIGGER IF EXISTS t3_packets_no_update")
    conn.execute(
        "ALTER TABLE t3_evidence_packets ADD COLUMN payload_hash TEXT")
    seen: dict[str, str] = {}
    for row in conn.execute(
            "SELECT packet_id, variant_id, review_status, reviewed_by, "
            "registry_change_ref, payload_json FROM t3_evidence_packets"):
        try:
            payload = json.loads(row["payload_json"])
            canonical, payload_hash = _t3_canonical(
                str(row["variant_id"]), payload, str(row["review_status"]),
                row["reviewed_by"], row["registry_change_ref"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"T3 packet {row['packet_id']} has invalid JSON") from exc
        if payload_hash in seen:
            raise MigrationError(
                "duplicate historical T3 packet payloads prevent content "
                f"addressing: {seen[payload_hash]} and {row['packet_id']}")
        seen[payload_hash] = str(row["packet_id"])
        conn.execute(
            "UPDATE t3_evidence_packets SET payload_json=?, payload_hash=? "
            "WHERE packet_id=?", (canonical, payload_hash, row["packet_id"]))
    conn.execute(
        "CREATE UNIQUE INDEX t3_packet_payload_hash "
        "ON t3_evidence_packets (payload_hash)")
    _execute_script(conn, """
        CREATE TRIGGER t3_packets_no_update
            BEFORE UPDATE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
    """)


def _migration_6(conn: sqlite3.Connection) -> None:
    """Content-address analyses, merging valid historical duplicates safely."""
    conn.execute("ALTER TABLE analysis_runs ADD COLUMN payload_hash TEXT")
    rows = conn.execute(
        "SELECT analysis_id, kind, subject_id, payload_json "
        "FROM analysis_runs ORDER BY analysis_id").fetchall()
    records = {}
    for row in rows:
        analysis_id = str(row["analysis_id"])
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"analysis {analysis_id} has invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MigrationError(
                f"analysis {analysis_id} payload is not a mapping")
        records[analysis_id] = {
            "kind": str(row["kind"]),
            "subject_id": str(row["subject_id"]),
            "payload": payload,
        }

    parent = {analysis_id: analysis_id for analysis_id in records}

    def find(analysis_id: str) -> str:
        while parent[analysis_id] != analysis_id:
            parent[analysis_id] = parent[parent[analysis_id]]
            analysis_id = parent[analysis_id]
        return analysis_id

    def union(left: str, right: str) -> bool:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return False
        keeper, duplicate = sorted((left_root, right_root))
        parent[duplicate] = keeper
        return True

    payloads = {analysis_id: record["payload"]
                for analysis_id, record in records.items()}
    for _ in range(len(records) + 2):
        remap = {analysis_id: find(analysis_id) for analysis_id in records}
        payloads = {
            analysis_id: _rewrite_analysis_references(payload, remap)
            for analysis_id, payload in payloads.items()
        }
        groups: dict[str, list[str]] = {}
        for analysis_id, record in records.items():
            _, payload_hash = _analysis_canonical(
                record["kind"], record["subject_id"], payloads[analysis_id])
            groups.setdefault(payload_hash, []).append(analysis_id)
        changed = False
        for members in groups.values():
            for duplicate in members[1:]:
                changed = union(members[0], duplicate) or changed
        if not changed:
            break
    else:
        raise MigrationError(
            "analysis reference deduplication did not converge")

    remap = {analysis_id: find(analysis_id) for analysis_id in records}
    payloads = {
        analysis_id: _rewrite_analysis_references(payload, remap)
        for analysis_id, payload in payloads.items()
    }

    _execute_script(conn, """
        DROP TRIGGER IF EXISTS edge_qualification_no_update;
        DROP TRIGGER IF EXISTS edge_qualification_no_delete;
        DROP TRIGGER IF EXISTS t3_packets_no_update;
        DROP TRIGGER IF EXISTS t3_packets_no_delete;
        DROP INDEX IF EXISTS t3_packet_payload_hash;
    """)

    for row in conn.execute(
            "SELECT event_id, source_analysis_id, detail_json "
            "FROM edge_qualification_events"):
        source_id = row["source_analysis_id"]
        if source_id is not None and str(source_id) not in records:
            raise MigrationError(
                f"qualification {row['event_id']} references missing analysis "
                f"{source_id}")
        try:
            detail = json.loads(row["detail_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"qualification {row['event_id']} has invalid detail JSON") \
                from exc
        conn.execute(
            "UPDATE edge_qualification_events SET source_analysis_id=?, "
            "detail_json=? WHERE event_id=?",
            (remap.get(str(source_id), str(source_id))
             if source_id is not None else None,
             _canonical_json(_rewrite_analysis_references(detail, remap)),
             row["event_id"]))

    packet_rows = conn.execute(
        "SELECT * FROM t3_evidence_packets ORDER BY packet_id").fetchall()
    packets = []
    for row in packet_rows:
        try:
            payload = json.loads(row["payload_json"])
            payload = _rewrite_analysis_references(payload, remap)
            canonical, payload_hash = _t3_canonical(
                str(row["variant_id"]), payload, str(row["review_status"]),
                row["reviewed_by"], row["registry_change_ref"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"T3 packet {row['packet_id']} has invalid JSON") from exc
        packets.append((str(row["packet_id"]), canonical, payload_hash))
    packet_keepers: dict[str, str] = {}
    for packet_id, canonical, payload_hash in packets:
        keeper = packet_keepers.setdefault(payload_hash, packet_id)
        if keeper != packet_id:
            conn.execute(
                "DELETE FROM t3_evidence_packets WHERE packet_id=?",
                (packet_id,))
            continue
        conn.execute(
            "UPDATE t3_evidence_packets SET payload_json=?, payload_hash=? "
            "WHERE packet_id=?", (canonical, payload_hash, packet_id))

    for analysis_id, keeper in remap.items():
        if analysis_id != keeper:
            conn.execute(
                "DELETE FROM analysis_runs WHERE analysis_id=?", (analysis_id,))
    for analysis_id, record in records.items():
        if remap[analysis_id] != analysis_id:
            continue
        canonical, payload_hash = _analysis_canonical(
            record["kind"], record["subject_id"], payloads[analysis_id])
        conn.execute(
            "UPDATE analysis_runs SET payload_json=?, payload_hash=? "
            "WHERE analysis_id=?", (canonical, payload_hash, analysis_id))

    conn.execute(
        "CREATE UNIQUE INDEX analysis_payload_hash "
        "ON analysis_runs (payload_hash)")
    conn.execute(
        "CREATE UNIQUE INDEX t3_packet_payload_hash "
        "ON t3_evidence_packets (payload_hash)")
    _execute_script(conn, """
        CREATE TRIGGER edge_qualification_no_update
            BEFORE UPDATE ON edge_qualification_events BEGIN
                SELECT RAISE(ABORT, 'qualification history is immutable');
            END;
        CREATE TRIGGER edge_qualification_no_delete
            BEFORE DELETE ON edge_qualification_events BEGIN
                SELECT RAISE(ABORT, 'qualification history is immutable');
            END;
        CREATE TRIGGER t3_packets_no_update
            BEFORE UPDATE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
        CREATE TRIGGER t3_packets_no_delete
            BEFORE DELETE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
        CREATE TRIGGER analysis_runs_no_update
            BEFORE UPDATE ON analysis_runs BEGIN
                SELECT RAISE(ABORT, 'analyses are immutable');
            END;
        CREATE TRIGGER analysis_runs_no_delete
            BEFORE DELETE ON analysis_runs BEGIN
                SELECT RAISE(ABORT, 'analyses are immutable');
            END;
    """)


def _migration_7(conn: sqlite3.Connection) -> None:
    """Persist every arm decision so policy vetoes remain attributable."""
    _execute_script(conn, """
        CREATE TABLE paper_decisions (
            decision_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            cycle_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            setup_type TEXT,
            signal_ts REAL,
            confidence REAL,
            decision_outcome TEXT NOT NULL
                CHECK (decision_outcome IN ('PROPOSED','VETOED')),
            reason TEXT,
            paper_trade_id TEXT,
            model_id TEXT NOT NULL,
            assumptions_json TEXT NOT NULL,
            proposal_json TEXT NOT NULL,
            decision_ts REAL NOT NULL,
            UNIQUE (scope_key, variant_id, proposal_id),
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id),
            FOREIGN KEY (paper_trade_id) REFERENCES paper_trades(trade_id));
        CREATE INDEX paper_decisions_variant_ts
            ON paper_decisions (scope_key, variant_id, decision_ts);
    """)

    # Executed trades are the only historical decisions that can be recovered.
    # They are retained for audit, but the ledger watermark below deliberately
    # excludes them from new forward qualification because historical vetoes
    # were not stored and therefore cannot be reconstructed honestly.
    for row in conn.execute(
            "SELECT * FROM paper_trades ORDER BY entry_ts, trade_id"):
        raw_id = "\0".join((
            "legacy", str(row["scope_key"]), str(row["variant_id"]),
            str(row["proposal_id"])))
        decision_id = hashlib.sha256(raw_id.encode()).hexdigest()[:32]
        proposal = {
            "symbol": row["symbol"], "direction": row["direction"],
            "setup_type": row["setup_type"],
            "signal_ts": row["signal_ts"],
        }
        conn.execute(
            "INSERT INTO paper_decisions (decision_id, proposal_id, "
            "scope_key, variant_id, cycle_id, symbol, direction, setup_type, "
            "signal_ts, confidence, decision_outcome, reason, "
            "paper_trade_id, model_id, assumptions_json, proposal_json, "
            "decision_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, row["proposal_id"], row["scope_key"],
             row["variant_id"], row["cycle_id"], row["symbol"],
             row["direction"], row["setup_type"], row["signal_ts"], None,
             "PROPOSED", "legacy executed trade; veto ledger unavailable",
             row["trade_id"], row["model_id"], row["assumptions_json"],
             _canonical_json(proposal), row["entry_ts"]))

    ledger_started = time.time()
    for row in conn.execute(
            "SELECT scope_key, variant_id, state_json FROM paper_portfolios"):
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"paper portfolio {row['scope_key']}:{row['variant_id']} "
                "has invalid state JSON") from exc
        if not isinstance(state, dict):
            raise MigrationError(
                f"paper portfolio {row['scope_key']}:{row['variant_id']} "
                "state is not a mapping")
        state["decision_ledger_started_ts"] = ledger_started
        conn.execute(
            "UPDATE paper_portfolios SET state_json=? WHERE scope_key=? "
            "AND variant_id=?",
            (_canonical_json(state), row["scope_key"], row["variant_id"]))

    # Analyses created before complete veto persistence cannot support an edge.
    # Qualification history stays immutable: append a revocation rather than
    # mutating or deleting the original event.
    latest = {}
    for row in conn.execute(
            "SELECT * FROM edge_qualification_events ORDER BY ts, event_id"):
        latest[(str(row["variant_id"]), str(row["scope_key"]))] = row
    for (variant_id, scope_key), event in latest.items():
        if event["status"] != "QUALIFIED":
            continue
        analysis_id = event["source_analysis_id"]
        analysis = (conn.execute(
            "SELECT payload_json FROM analysis_runs WHERE analysis_id=?",
            (analysis_id,)).fetchone() if analysis_id else None)
        schema = None
        if analysis is not None:
            try:
                payload = json.loads(analysis["payload_json"])
                schema = (payload.get("source_evidence") or {}).get("schema")
            except (TypeError, json.JSONDecodeError, AttributeError):
                schema = None
        if schema == "paper_decision_evidence.v2":
            continue
        conn.execute(
            "INSERT INTO edge_qualification_events (event_id, variant_id, "
            "scope_key, status, ts, source_analysis_id, detail_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, variant_id, scope_key, "REVOKED",
             ledger_started, analysis_id, _canonical_json({
                 "source": "schema_migration_7",
                 "reason": (
                     "qualification predates complete persisted veto/accept "
                     "decision evidence; collect a new forward sample"),
             })))

    _execute_script(conn, """
        CREATE TRIGGER paper_decisions_no_update
            BEFORE UPDATE ON paper_decisions BEGIN
                SELECT RAISE(ABORT, 'paper decisions are immutable');
            END;
        CREATE TRIGGER paper_decisions_no_delete
            BEFORE DELETE ON paper_decisions BEGIN
                SELECT RAISE(ABORT, 'paper decisions are immutable');
            END;
    """)


MIGRATIONS = {
    1: ("create_initial_store", _migration_1),
    2: ("rebuild_legacy_constraints", _migration_2),
    3: ("paper_research_and_t3_evidence", _migration_3),
    4: ("scoped_paper_identity_and_qualification", _migration_4),
    5: ("content_addressed_t3_packets", _migration_5),
    6: ("content_addressed_analyses", _migration_6),
    7: ("complete_paper_decision_ledger", _migration_7),
}


def _validate_schema(conn: sqlite3.Connection) -> None:
    required = {
        "schema_meta", "schema_migrations", "variants", "variant_runs",
        "variant_results", "findings", "variant_scheduler",
        "paper_portfolios", "paper_trades", "paper_failures",
        "run_evidence", "analysis_runs", "t3_evidence_packets",
        "edge_qualification_events",
    }
    if _stored_version(conn) >= 7:
        required.add("paper_decisions")
    missing = sorted(table for table in required
                     if not _table_exists(conn, table))
    if missing:
        raise MigrationError(
            "findings schema claims to be current but is missing: "
            + ", ".join(missing))


def _migrate(conn: sqlite3.Connection) -> None:
    current = _stored_version(conn)
    if current > SCHEMA_VERSION:
        raise MigrationError(
            f"findings.db schema {current} is newer than supported "
            f"schema {SCHEMA_VERSION}; downgrade refused")
    if current == SCHEMA_VERSION:
        _validate_schema(conn)
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_ts REAL NOT NULL)"
        )
        if current:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(version, name, applied_ts) VALUES (?,?,?)",
                (current, MIGRATIONS[current][0], 0.0))
        for version in range(current + 1, SCHEMA_VERSION + 1):
            name, migrate = MIGRATIONS[version]
            migrate(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) "
                "VALUES ('schema_version', ?)", (str(version),))
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_ts) "
                "VALUES (?,?,?)", (version, name, time.time()))
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MigrationError(
                f"findings migration would leave {len(violations)} foreign-key "
                "violation(s); history was not modified")
        _validate_schema(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


class FindingsStore:
    def __init__(self, path: str | Path = DEFAULT_STORE) -> None:
        self.path = Path(path)

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.backup")

    def backup(self, destination: str | Path | None = None) -> Path:
        """Write a transactionally consistent SQLite backup."""
        target = Path(destination) if destination else self.backup_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.path) as source, sqlite3.connect(
                target, factory=_ClosingConnection) as copy:
            source.backup(copy)
        return target

    def schema_version(self) -> int:
        with _connect(self.path) as conn:
            return _stored_version(conn)

    def migration_history(self) -> list:
        with _connect(self.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version")]

    # ------------------------------------------------------- paper research

    @staticmethod
    def _paper_default(initial_balance: float, now: float) -> dict:
        balance = float(initial_balance)
        return {
            "state_version": 1,
            "status": "SHADOW",
            "paper_started_ts": None,
            "decision_ledger_started_ts": now,
            "resume_status": "SHADOW",
            "cash_usdt": balance,
            "equity_usdt": balance,
            "realized_pnl_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "high_water_mark": balance,
            "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
            "day_start_equity": balance,
            "positions": [],
            "gross_notional": 0.0,
            "net_notional": 0.0,
            "open_risk_usdt": 0.0,
            "cooldowns": {},
            "active_trades": {},
            "seen_proposals": {},
            "unpriced_positions": 0,
            "loss_count": 0,
            "consecutive_losses": 0,
            "win_count": 0,
            "max_drawdown_pct": 0.0,
            "last_mark_ts": now,
            "failure_count": 0,
            "revoked_reason": None,
            "experiment_provenance": None,
        }

    def load_paper_portfolio(
            self, scope_key: str, variant_id: str,
            initial_balance: float = 10_000.0,
            now: float | None = None) -> tuple[dict, int]:
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            row = conn.execute(
                "SELECT state_json, version FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, variant_id)).fetchone()
            if row is None:
                state = self._paper_default(initial_balance, timestamp)
                conn.execute(
                    "INSERT INTO paper_portfolios (scope_key, variant_id, "
                    "state_json, version, updated_ts) VALUES (?,?,?,?,?)",
                    (scope_key, variant_id,
                     json.dumps(state, sort_keys=True), 1, timestamp))
                return state, 1
        return json.loads(row["state_json"]), int(row["version"])

    @staticmethod
    def _validate_experiment_provenance(
            variant: sqlite3.Row, provenance: dict) -> dict:
        from agent import state as runtime_state

        if not isinstance(provenance, dict):
            raise ValueError("experiment provenance must be a mapping")
        required = {
            "variant_definition_hash", "strategy_config_version",
            "experiment_config",
            "code_version", "forward_model_id",
            "forward_model_assumptions_hash",
        }
        missing = sorted(required - set(provenance))
        if missing:
            raise ValueError(
                "experiment provenance is incomplete: " + ", ".join(missing))
        canonical = json.loads(_canonical_json(provenance))
        expected = variant_identity_hash(variant)
        if canonical["variant_definition_hash"] != expected:
            raise ValueError(
                f"{variant['variant_id']}: variant definition provenance "
                "does not match the immutable registry identity")
        experiment_config = canonical.get("experiment_config")
        if not isinstance(experiment_config, dict):
            raise ValueError("experiment provenance config is not a mapping")
        if runtime_state.experiment_fingerprint_material(
                experiment_config) != experiment_config:
            raise ValueError(
                "experiment provenance config contains secrets or "
                "non-executable settings")
        if canonical["strategy_config_version"] != _content_hash(
                experiment_config)[:16]:
            raise ValueError(
                "experiment provenance config does not match its fingerprint")
        for key in required - {"experiment_config"}:
            if not str(canonical.get(key) or "").strip():
                raise ValueError(f"experiment provenance {key} is empty")
        return canonical

    def bind_paper_experiment(
            self, scope_key: str, variant_id: str, provenance: dict,
            initial_balance: float = 10_000.0,
            now: float | None = None) -> tuple[dict, int]:
        """Bind one portfolio to one immutable executable experiment.

        Existing evidence without provenance is deliberately not adopted. A
        code/config/model change must use a new variant id (or an explicitly
        migrated empty account), otherwise old and new outcomes would share a
        bucket while claiming to describe one experiment.
        """
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            variant = self._require_variant(conn, variant_id)
            canonical = self._validate_experiment_provenance(
                variant, provenance)
            row = conn.execute(
                "SELECT state_json, version FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, variant_id)).fetchone()
            if row is None:
                state = self._paper_default(initial_balance, timestamp)
                state["experiment_provenance"] = canonical
                conn.execute(
                    "INSERT INTO paper_portfolios (scope_key, variant_id, "
                    "state_json, version, updated_ts) VALUES (?,?,?,?,?)",
                    (scope_key, variant_id, _canonical_json(state), 1,
                     timestamp))
                return state, 1

            state = json.loads(row["state_json"])
            version = int(row["version"])
            existing = state.get("experiment_provenance")
            if existing == canonical:
                return state, version
            trades = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE scope_key=? "
                "AND variant_id=?", (scope_key, variant_id)).fetchone()[0]
            decisions = conn.execute(
                "SELECT COUNT(*) FROM paper_decisions WHERE scope_key=? "
                "AND variant_id=?", (scope_key, variant_id)).fetchone()[0]
            has_evidence = bool(
                trades or decisions or state.get("positions")
                or state.get("active_trades")
                or state.get("seen_proposals") or state.get("paper_started_ts")
                or state.get("win_count") or state.get("loss_count"))
            if existing is None and not has_evidence:
                state["experiment_provenance"] = canonical
                version = self._update_paper_portfolio(
                    conn, scope_key, variant_id, state, version, timestamp)
                return state, version
            raise ValueError(
                f"{variant_id}: persisted paper evidence belongs to a "
                "different or unprovenanced experiment; use a new variant id")

    def save_paper_portfolio(
            self, scope_key: str, variant_id: str, state: dict,
            expected_version: int, now: float | None = None) -> int:
        return self.commit_paper_portfolio(
            scope_key, variant_id, state, expected_version, now=now)

    @staticmethod
    def _update_paper_portfolio(
            conn: sqlite3.Connection, scope_key: str, variant_id: str,
            state: dict, expected_version: int, timestamp: float) -> int:
        payload = json.dumps(state, sort_keys=True, allow_nan=False)
        cursor = conn.execute(
            "UPDATE paper_portfolios SET state_json=?, version=version+1, "
            "updated_ts=?, revoked_ts=?, revoke_reason=? "
            "WHERE scope_key=? AND variant_id=? AND version=?",
            (payload, timestamp,
             timestamp if state.get("status") == "REVOKED" else None,
             state.get("revoked_reason"), scope_key, variant_id,
             int(expected_version)))
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"paper portfolio version conflict for {variant_id}")
        return int(expected_version) + 1

    @staticmethod
    def _insert_paper_trade(conn: sqlite3.Connection, trade: dict) -> str:
        trade_id = str(trade.get("trade_id") or uuid.uuid4().hex)
        conn.execute(
            "INSERT INTO paper_trades (trade_id, proposal_id, scope_key, "
            "variant_id, cycle_id, symbol, direction, setup_type, signal_ts, "
            "model_id, assumptions_json, entry_ts, entry_price, notional, "
            "risk_usd, stop_price, take_price, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')",
            (trade_id, trade["proposal_id"], trade["scope_key"],
             trade["variant_id"], trade.get("cycle_id"), trade["symbol"],
             trade["direction"], trade.get("setup_type"),
             trade.get("signal_ts"), trade["model_id"],
             json.dumps(trade.get("assumptions") or {}, sort_keys=True),
             trade["entry_ts"], trade["entry_price"], trade["notional"],
             trade["risk_usd"], trade["stop_price"], trade["take_price"]))
        return trade_id

    @staticmethod
    def _insert_paper_decision(
            conn: sqlite3.Connection, decision: dict) -> str:
        decision_id = str(decision.get("decision_id") or uuid.uuid4().hex)
        outcome = str(decision.get("decision_outcome") or "").upper()
        if outcome not in {"PROPOSED", "VETOED"}:
            raise ValueError("paper decision outcome must be PROPOSED or VETOED")
        conn.execute(
            "INSERT INTO paper_decisions (decision_id, proposal_id, "
            "scope_key, variant_id, cycle_id, symbol, direction, setup_type, "
            "signal_ts, confidence, decision_outcome, reason, paper_trade_id, "
            "model_id, assumptions_json, proposal_json, decision_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, decision["proposal_id"], decision["scope_key"],
             decision["variant_id"], decision.get("cycle_id"),
             decision["symbol"], decision.get("direction"),
             decision.get("setup_type"), decision.get("signal_ts"),
             decision.get("confidence"), outcome, decision.get("reason"),
             decision.get("paper_trade_id"), decision["model_id"],
             _canonical_json(decision.get("assumptions") or {}),
             _canonical_json(decision.get("proposal") or {}),
             decision["decision_ts"]))
        return decision_id

    @staticmethod
    def _close_paper_trade(conn: sqlite3.Connection, close: dict) -> None:
        cursor = conn.execute(
            "UPDATE paper_trades SET exit_ts=?, exit_price=?, result=?, "
            "net_pnl_usd=?, r_multiple=?, status='CLOSED' "
            "WHERE trade_id=? AND status='OPEN'",
            (close["exit_ts"], close["exit_price"], close["result"],
             close["net_pnl_usd"], close["r_multiple"], close["trade_id"]))
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"paper trade {close['trade_id']} was not open")

    def commit_paper_portfolio(
            self, scope_key: str, variant_id: str, state: dict,
            expected_version: int, *, opened_trades: list | None = None,
            decisions: list | None = None,
            closed_trades: list | None = None,
            now: float | None = None) -> int:
        """Atomically persist trade mutations and their resulting account."""
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            for trade in opened_trades or []:
                if (trade.get("scope_key") != scope_key
                        or trade.get("variant_id") != variant_id):
                    raise ValueError("paper trade attribution mismatch")
                self._insert_paper_trade(conn, trade)
            for decision in decisions or []:
                if (decision.get("scope_key") != scope_key
                        or decision.get("variant_id") != variant_id):
                    raise ValueError("paper decision attribution mismatch")
                self._insert_paper_decision(conn, decision)
            for close in closed_trades or []:
                self._close_paper_trade(conn, close)
            return self._update_paper_portfolio(
                conn, scope_key, variant_id, state, expected_version, timestamp)

    def scheduler_order(self, scope_key: str, variant_ids: list[str]) -> list[str]:
        """Least-observed-first ordering: durable fair round-robin."""
        with _connect(self.path) as conn:
            for ordinal, variant_id in enumerate(variant_ids):
                self._require_variant(conn, variant_id)
                conn.execute(
                    "INSERT INTO variant_scheduler (scope_key, variant_id, "
                    "ordinal) VALUES (?,?,?) ON CONFLICT(scope_key, variant_id) "
                    "DO UPDATE SET ordinal=excluded.ordinal",
                    (scope_key, variant_id, ordinal))
            placeholders = ",".join("?" for _ in variant_ids)
            if not placeholders:
                return []
            rows = conn.execute(
                "SELECT variant_id FROM variant_scheduler WHERE scope_key=? "
                f"AND variant_id IN ({placeholders}) ORDER BY evaluations, "
                "COALESCE(last_evaluated_ts, 0), ordinal, variant_id",
                (scope_key, *variant_ids)).fetchall()
        return [str(row[0]) for row in rows]

    def record_scheduler_cycle(
            self, scope_key: str, evaluated: list[str], skipped: list[str],
            now: float | None = None) -> None:
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            conn.executemany(
                "UPDATE variant_scheduler SET evaluations=evaluations+1, "
                "last_evaluated_ts=? WHERE scope_key=? AND variant_id=?",
                [(timestamp, scope_key, variant_id)
                 for variant_id in evaluated])
            conn.executemany(
                "UPDATE variant_scheduler SET skips=skips+1, "
                "last_skipped_ts=? WHERE scope_key=? AND variant_id=?",
                [(timestamp, scope_key, variant_id) for variant_id in skipped])

    def scheduler_coverage(self, scope_key: str) -> list:
        with _connect(self.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM variant_scheduler WHERE scope_key=? "
                "ORDER BY ordinal, variant_id", (scope_key,))]

    def paper_scopes(self) -> list[str]:
        with _connect(self.path) as conn:
            return [str(row[0]) for row in conn.execute(
                "SELECT DISTINCT scope_key FROM paper_portfolios "
                "ORDER BY scope_key")]

    def paper_portfolio_state(
            self, scope_key: str, variant_id: str) -> dict | None:
        """Read an enrolled portfolio without creating one as a side effect."""
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT state_json, version, updated_ts FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, variant_id)).fetchone()
        if row is None:
            return None
        state = json.loads(row["state_json"])
        state["portfolio_version"] = int(row["version"])
        state["updated_ts"] = float(row["updated_ts"])
        return state

    def record_paper_trade_open(self, trade: dict) -> str:
        with _connect(self.path) as conn:
            return self._insert_paper_trade(conn, trade)

    def close_paper_trade(
            self, trade_id: str, *, exit_ts: float, exit_price: float,
            result: str, net_pnl_usd: float, r_multiple: float) -> None:
        with _connect(self.path) as conn:
            self._close_paper_trade(conn, {
                "trade_id": trade_id, "exit_ts": exit_ts,
                "exit_price": exit_price, "result": result,
                "net_pnl_usd": net_pnl_usd, "r_multiple": r_multiple})

    def record_paper_failure(
            self, scope_key: str, variant_id: str, kind: str, detail: dict,
            now: float | None = None) -> int:
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            cursor = conn.execute(
                "INSERT INTO paper_failures (scope_key, variant_id, ts, kind, "
                "detail_json) VALUES (?,?,?,?,?)",
                (scope_key, variant_id, timestamp, kind,
                 json.dumps(detail, sort_keys=True, default=str)))
            return int(cursor.lastrowid)

    def paper_trades_for(self, scope_key: str, variant_id: str) -> list:
        with _connect(self.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM paper_trades WHERE scope_key=? AND variant_id=? "
                "ORDER BY entry_ts, trade_id", (scope_key, variant_id))]

    def paper_decisions_for(self, scope_key: str, variant_id: str) -> list:
        with _connect(self.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM paper_decisions WHERE scope_key=? AND "
                "variant_id=? ORDER BY decision_ts, decision_id",
                (scope_key, variant_id))]

    def paper_summary(
            self, scope_key: str, variant_id: str,
            paper_only: bool = True) -> dict:
        state, _ = self.load_paper_portfolio(scope_key, variant_id)
        trades = self.paper_trades_for(scope_key, variant_id)
        decisions = self.paper_decisions_for(scope_key, variant_id)
        paper_started = state.get("paper_started_ts")
        if paper_only:
            trades = [row for row in trades if paper_started is not None
                      and float(row["entry_ts"]) >= float(paper_started)]
            decisions = [
                row for row in decisions if paper_started is not None
                and float(row["decision_ts"]) >= float(paper_started)]
        closed = [row for row in trades if row["status"] == "CLOSED"]
        accepted = [row for row in decisions
                    if row["decision_outcome"] == "PROPOSED"]
        vetoed = [row for row in decisions
                  if row["decision_outcome"] == "VETOED"]
        return {
            "scope_key": scope_key,
            "variant_id": variant_id,
            "status": state.get("status"),
            "paper_started_ts": paper_started,
            "decision_ledger_started_ts": state.get(
                "decision_ledger_started_ts"),
            "decisions": len(decisions),
            "accepted_decisions": len(accepted),
            "vetoed_decisions": len(vetoed),
            "equity_usdt": state.get("equity_usdt"),
            "cash_usdt": state.get("cash_usdt"),
            "max_drawdown_pct": state.get("max_drawdown_pct"),
            "open_positions": len(state.get("positions") or []),
            "closed_trades": len(closed),
            "net_pnl_usdt": sum(float(row["net_pnl_usd"] or 0) for row in closed),
            "expectancy_r": (sum(float(row["r_multiple"] or 0) for row in closed)
                             / len(closed) if closed else None),
            "failures": int(state.get("failure_count") or 0),
            "revoked_reason": state.get("revoked_reason"),
        }

    # ------------------------------------------------ edge qualification

    def qualification_events(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM edge_qualification_events "
                "WHERE variant_id=? ORDER BY ts, event_id", (variant_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            out.append(item)
        return out

    def qualification_status(
            self, variant_id: str, scope_key: str = "*") -> dict | None:
        eligible = [event for event in self.qualification_events(variant_id)
                    if event["scope_key"] in {"*", scope_key}]
        return eligible[-1] if eligible else None

    def qualified_variant_ids(self, scope_key: str = "*") -> list[str]:
        with _connect(self.path) as conn:
            ids = [str(row[0]) for row in conn.execute(
                "SELECT variant_id FROM variants ORDER BY variant_id")]
        return [variant_id for variant_id in ids
                if (self.qualification_status(variant_id, scope_key) or {})
                .get("status") == "QUALIFIED"]

    @staticmethod
    def _analysis_payload(row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload_json"])
        canonical, payload_hash = _analysis_canonical(
            str(row["kind"]), str(row["subject_id"]), payload)
        if canonical != row["payload_json"] or payload_hash != row["payload_hash"]:
            raise ValueError(
                f"analysis {row['analysis_id']} failed its content hash")
        return payload

    @classmethod
    def _validate_forward_axis(
            cls, conn: sqlite3.Connection, strategy_id: str, axis: object,
            baseline_id: str, setting_ids: list[str], arms: dict) -> list[str]:
        """Prove arms are one version of one strategy differing only on axis."""
        canonical_axis = _canonical_axis(axis)
        expected_baseline = f"{strategy_id}.baseline"
        if baseline_id != expected_baseline:
            raise ValueError(
                f"forward baseline must be {expected_baseline!r}, not "
                f"{baseline_id!r}")
        if len(set(setting_ids)) != len(setting_ids):
            raise ValueError("forward candidates must be unique")

        baseline = cls._require_variant(conn, baseline_id)
        try:
            baseline_overrides = json.loads(baseline["overrides_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("forward baseline has invalid overrides") from exc
        if (not isinstance(baseline_overrides, dict)
                or baseline["strategy_id"] != strategy_id
                or baseline_overrides):
            raise ValueError(
                "forward baseline must be the unmodified registered strategy")
        base_version = str(baseline["base_version"])
        baseline_provenance = cls._validate_experiment_provenance(
            baseline, arms[baseline_id].get("experiment_provenance") or {})
        baseline_config = baseline_provenance["experiment_config"]
        strategy_config = baseline_config.get("strategy") or {}
        if (strategy_config.get("id") != strategy_id
                or strategy_config.get("version") != base_version):
            raise ValueError(
                "forward baseline executable config does not match its "
                "registered strategy/version")

        baseline_without_axis = _without_dotted_paths(
            baseline_config, canonical_axis)
        seen_values = {
            _canonical_json([
                _dotted_value(baseline_config, path)
                for path in canonical_axis
            ])
        }
        for variant_id in setting_ids:
            variant = cls._require_variant(conn, variant_id)
            if (variant["strategy_id"] != strategy_id
                    or str(variant["base_version"]) != base_version):
                raise ValueError(
                    "forward axis mixes strategy versions or strategies")
            try:
                overrides = json.loads(variant["overrides_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"{variant_id}: invalid registered overrides") from exc
            if not isinstance(overrides, dict):
                raise ValueError(
                    f"{variant_id}: registered overrides are not a mapping")
            if set(overrides) != set(canonical_axis):
                raise ValueError(
                    f"{variant_id}: overrides {sorted(overrides)} do not "
                    f"exactly match declared axis {canonical_axis}")
            provenance = cls._validate_experiment_provenance(
                variant, arms[variant_id].get("experiment_provenance") or {})
            config = provenance["experiment_config"]
            candidate_strategy = config.get("strategy") or {}
            if (candidate_strategy.get("id") != strategy_id
                    or candidate_strategy.get("version") != base_version):
                raise ValueError(
                    f"{variant_id}: executable config is not the registered "
                    "strategy/version")
            for path in canonical_axis:
                if _canonical_json(_dotted_value(config, path)) != _canonical_json(
                        overrides[path]):
                    raise ValueError(
                        f"{variant_id}: executable value for {path} does not "
                        "match its registered override")
            if _canonical_json(_without_dotted_paths(
                    config, canonical_axis)) != _canonical_json(
                        baseline_without_axis):
                raise ValueError(
                    f"{variant_id}: non-axis executable configuration differs "
                    "from the baseline")
            values = _canonical_json([
                _dotted_value(config, path) for path in canonical_axis
            ])
            if values in seen_values:
                raise ValueError(
                    f"{variant_id}: axis setting duplicates the baseline or "
                    "another candidate")
            seen_values.add(values)
        return canonical_axis

    @classmethod
    def _forward_verdict_from_payload(
            cls, conn: sqlite3.Connection, payload: dict):
        from agent.forward_models import require_validated
        from research import protocol

        source = payload.get("source_evidence") or {}
        arms = source.get("arms")
        if (source.get("schema") != "paper_decision_evidence.v2"
                or not isinstance(arms, dict)
                or source.get("sha256") != _content_hash(arms)):
            raise ValueError("forward source evidence failed its content hash")
        strategy_id = str(payload.get("strategy_id") or "")
        model = require_validated(strategy_id)
        baseline_id = str(source.get("baseline_id") or "")
        setting_ids = [str(value) for value in source.get("setting_ids") or []]
        if (not baseline_id or len(setting_ids) < 1
                or len(set(setting_ids)) != len(setting_ids)
                or set(arms) != {baseline_id, *setting_ids}
                or not all(isinstance(arm, dict) for arm in arms.values())):
            raise ValueError("forward source evidence has inconsistent arms")
        cls._validate_forward_axis(
            conn, strategy_id, payload.get("axis"), baseline_id,
            setting_ids, arms)

        common_code_versions = set()
        for variant_id, arm in arms.items():
            variant = cls._require_variant(conn, variant_id)
            if variant["strategy_id"] != strategy_id:
                raise ValueError("forward evidence mixes strategies")
            provenance = cls._validate_experiment_provenance(
                variant, arm.get("experiment_provenance") or {})
            common_code_versions.add(provenance["code_version"])
            if provenance["forward_model_id"] != model.model_id:
                raise ValueError("forward evidence mixes outcome models")
            if provenance["forward_model_assumptions_hash"] != _content_hash(
                    model.as_dict()):
                raise ValueError("forward model assumptions changed")
            decisions = arm.get("decisions")
            if not isinstance(decisions, list):
                raise ValueError("forward decision corpus is invalid")
            for decision in decisions:
                assumptions = decision.get("assumptions") or {}
                outcome = str(decision.get("decision_outcome") or "")
                proposed = outcome == "PROPOSED"
                vetoed = outcome == "VETOED"
                if (decision.get("scope_key") != payload.get("scope_key")
                        or decision.get("variant_id") != variant_id
                        or decision.get("model_id") != model.model_id
                        or assumptions.get("forward_model") != json.loads(
                            _canonical_json(model.as_dict()))
                        or assumptions.get("experiment_provenance")
                        != provenance
                        or not (proposed or vetoed)
                        or (proposed and not decision.get("paper_trade_id"))
                        or (proposed and (
                            decision.get("trade_proposal_id")
                            != decision.get("proposal_id")
                            or decision.get("trade_scope_key")
                            != decision.get("scope_key")
                            or decision.get("trade_variant_id") != variant_id
                            or decision.get("trade_model_id") != model.model_id
                            or decision.get("trade_status")
                            not in {"OPEN", "CLOSED"}))
                        or (vetoed and (
                            decision.get("paper_trade_id")
                            or decision.get("trade_status") is not None))):
                    raise ValueError(
                        "forward evidence contains mixed model, assumptions, "
                        "scope, experiment provenance, or decision linkage")
        if len(common_code_versions) != 1:
            raise ValueError("forward evidence mixes code versions")

        baseline = protocol.paper_trade_decisions(
            arms[baseline_id]["decisions"])
        settings = [
            (variant_id,
             protocol.paper_trade_decisions(arms[variant_id]["decisions"]))
            for variant_id in setting_ids
        ]
        return protocol.evaluate_axis(
            settings, baseline, strategy_id=strategy_id)

    def record_forward_analysis(
            self, scope_key: str, strategy_id: str, axis: list[str],
            baseline_id: str, setting_ids: list[str]) -> tuple[str, object]:
        """Create forward evidence from the complete persisted decision ledger."""
        from agent import state as runtime_state
        from agent.forward_models import require_validated
        from research import protocol

        model = require_validated(strategy_id)
        axis = _canonical_axis(axis)
        current_code = runtime_state.code_fingerprint()
        ordered_ids = [str(baseline_id)] + [str(value) for value in setting_ids]
        if len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError("forward analysis arms must be unique")
        arms = {}
        statuses = {}
        with _connect(self.path) as conn:
            enrolled = {}
            evidence_ends = []
            for variant_id in ordered_ids:
                variant = self._require_variant(conn, variant_id)
                if variant["strategy_id"] != strategy_id:
                    raise ValueError("forward analysis cannot mix strategies")
                portfolio = conn.execute(
                    "SELECT state_json FROM paper_portfolios WHERE "
                    "scope_key=? AND variant_id=?",
                    (scope_key, variant_id)).fetchone()
                if portfolio is None:
                    raise ValueError(
                        f"{variant_id}: no enrolled paper portfolio")
                state = json.loads(portfolio["state_json"])
                provenance = self._validate_experiment_provenance(
                    variant, state.get("experiment_provenance") or {})
                if provenance["code_version"] != current_code:
                    raise ValueError(
                        f"{variant_id}: evidence code version is not current")
                if provenance["forward_model_id"] != model.model_id:
                    raise ValueError(
                        f"{variant_id}: evidence outcome model is not current")
                ledger_started = state.get("decision_ledger_started_ts")
                if ledger_started is None:
                    raise ValueError(
                        f"{variant_id}: complete paper decision ledger has "
                        "not started")
                if state.get("status") == "REVOKED":
                    raise ValueError(
                        f"{variant_id}: revoked portfolio cannot contribute "
                        "forward evidence")
                qualification = conn.execute(
                    "SELECT MIN(ts) FROM edge_qualification_events WHERE "
                    "variant_id=? AND scope_key IN ('*', ?) "
                    "AND status='QUALIFIED'",
                    (variant_id, scope_key)).fetchone()[0]
                for cutoff in (qualification, state.get("paper_started_ts")):
                    if cutoff is not None:
                        evidence_ends.append(float(cutoff))
                enrolled[variant_id] = (state, provenance)
                statuses[variant_id] = state.get("status")

            common_ledger_start = max(
                float(state["decision_ledger_started_ts"])
                for state, _ in enrolled.values())
            common_evidence_end = min(evidence_ends) if evidence_ends else None
            if (common_evidence_end is not None
                    and common_evidence_end <= common_ledger_start):
                raise ValueError(
                    "forward evidence window ends before the common decision "
                    "ledger starts")
            for variant_id in ordered_ids:
                state, provenance = enrolled[variant_id]
                params = [scope_key, variant_id, common_ledger_start]
                before_end = ""
                if common_evidence_end is not None:
                    before_end = " AND d.decision_ts<?"
                    params.append(common_evidence_end)
                failure_params = [scope_key, variant_id, common_ledger_start]
                failure_end = ""
                if common_evidence_end is not None:
                    failure_end = " AND ts<?"
                    failure_params.append(common_evidence_end)
                failure = conn.execute(
                    "SELECT kind, ts FROM paper_failures WHERE scope_key=? "
                    "AND variant_id=? AND ts>=?" + failure_end
                    + " ORDER BY ts LIMIT 1", failure_params).fetchone()
                if failure is not None:
                    raise ValueError(
                        f"{variant_id}: operational failure "
                        f"{failure['kind']} occurred inside the forward "
                        "evidence window")
                rows = conn.execute(
                    "SELECT d.*, t.proposal_id AS trade_proposal_id, "
                    "t.scope_key AS trade_scope_key, "
                    "t.variant_id AS trade_variant_id, "
                    "t.model_id AS trade_model_id, "
                    "t.entry_ts AS trade_entry_ts, "
                    "t.exit_ts AS trade_exit_ts, t.result AS trade_result, "
                    "t.r_multiple AS trade_r_multiple, "
                    "t.status AS trade_status FROM paper_decisions d "
                    "LEFT JOIN paper_trades t ON t.trade_id=d.paper_trade_id "
                    "WHERE d.scope_key=? AND d.variant_id=? "
                    "AND d.decision_ts>=?" + before_end
                    + " ORDER BY d.decision_ts, d.decision_id", params
                ).fetchall()
                arms[variant_id] = {
                    "experiment_provenance": provenance,
                    "decisions": [
                        _forward_decision_evidence(row) for row in rows],
                }

            source = {
                "schema": "paper_decision_evidence.v2",
                "baseline_id": baseline_id,
                "setting_ids": list(setting_ids),
                "decision_ledger_from_ts": common_ledger_start,
                "decision_ledger_to_ts": common_evidence_end,
                "arms": arms,
                "sha256": _content_hash(arms),
            }
            provisional = {
                "source": "real_time_shadow_portfolios",
                "source_evidence": source,
                "scope_key": scope_key,
                "strategy_id": strategy_id,
                "axis": list(axis),
            }
            verdict = self._forward_verdict_from_payload(conn, provisional)
            corpus = [decision for arm in arms.values()
                      for decision in arm["decisions"]]
            resolved = [row for row in corpus
                        if row.get("decision_outcome") == "VETOED"
                        or (row.get("trade_status") == "CLOSED"
                            and row.get("trade_r_multiple") is not None)]
            payload = {
                **provisional,
                "settings": list(setting_ids),
                "portfolio_statuses": statuses,
                "verdict": verdict.verdict,
                "governing_criterion": verdict.governing_criterion,
                "detail": verdict.detail,
                "evidence": verdict.evidence,
                "corpus_from_ts": min(
                    (row["decision_ts"] for row in corpus), default=None),
                "corpus_to_ts": max(
                    (row["decision_ts"] for row in corpus), default=None),
                "resolved_outcomes": len(resolved),
                "code_version": current_code,
                "forward_model_id": model.model_id,
                "experiment_provenance": {
                    variant_id: arm["experiment_provenance"]
                    for variant_id, arm in arms.items()},
            }
            subject = (
                f"{scope_key}:{strategy_id}:{'+'.join(map(str, axis))}")
            analysis_id = self._insert_analysis(
                conn, "forward_parameter_axis", subject, payload)
        self.backup()
        return analysis_id, verdict

    @staticmethod
    def _latest_qualification(
            conn: sqlite3.Connection, variant_id: str,
            scope_key: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM edge_qualification_events WHERE variant_id=? "
            "AND scope_key IN ('*', ?) ORDER BY ts DESC, event_id DESC LIMIT 1",
            (variant_id, scope_key)).fetchone()

    @classmethod
    def _validated_forward_analysis(
            cls, conn: sqlite3.Connection, variant_id: str,
            source_analysis_id: str | None, scope_key: str) -> dict:
        from research import protocol

        if not source_analysis_id:
            raise ValueError(
                "edge qualification requires a persisted forward analysis")
        variant = cls._require_variant(conn, variant_id)
        row = conn.execute(
            "SELECT * FROM analysis_runs WHERE analysis_id=?",
            (source_analysis_id,)).fetchone()
        if row is None:
            raise ValueError(
                f"unknown forward analysis {source_analysis_id!r}")
        payload = cls._analysis_payload(row)
        recomputed = cls._forward_verdict_from_payload(conn, payload)
        stored_claim = {
            "verdict": payload.get("verdict"),
            "governing_criterion": payload.get("governing_criterion"),
            "detail": payload.get("detail"),
            "evidence": payload.get("evidence") or {},
        }
        recomputed_claim = {
            "verdict": recomputed.verdict,
            "governing_criterion": recomputed.governing_criterion,
            "detail": recomputed.detail,
            "evidence": recomputed.evidence,
        }
        if _canonical_json(stored_claim) != _canonical_json(recomputed_claim):
            raise ValueError(
                "forward analysis claims do not recompute from source evidence")
        evidence = payload.get("evidence") or {}
        paired = evidence.get("paired") or {}
        fit_paired = evidence.get("fit_paired") or {}
        confirmation_paired = evidence.get("confirmation_paired") or {}
        criteria = evidence.get("criteria") or {}
        required_criteria = {
            "min_round_trips", "min_axis_settings", "paired_sample",
            "paired_coverage", "no_duplicate_proposals",
            "paired_dependence_aware", "fit_paired_sample",
            "fit_paired_coverage", "fit_no_duplicate_proposals",
            "fit_dependence_aware", "confirmation_paired_sample",
            "confirmation_paired_coverage",
            "confirmation_no_duplicate_proposals",
            "confirmation_dependence_aware",
            "fit_interval_positive", "drawdown_no_worse",
            "out_of_sample_survives", "confirmation_interval_positive",
        }

        def evidence_pair_ok(pair: dict, minimum: int) -> bool:
            bootstrap = pair.get("bootstrap") or {}
            return bool(
                int(pair.get("paired_n") or 0) >= minimum
                and float(pair.get("pair_coverage_pct") or 0)
                >= protocol.MIN_PAIR_COVERAGE_PCT
                and not int(pair.get("left_duplicates") or 0)
                and not int(pair.get("right_duplicates") or 0)
                and bootstrap.get("kind") == protocol.PAIR_BOOTSTRAP_KIND
                and int(bootstrap.get("cluster_seconds") or 0)
                == protocol.PAIR_CLUSTER_SECONDS)

        checks = {
            "analysis kind": row["kind"] == "forward_parameter_axis",
            "real-time source": (
                payload.get("source") == "real_time_shadow_portfolios"),
            "scope": payload.get("scope_key") == scope_key,
            "strategy": payload.get("strategy_id") == variant["strategy_id"],
            "promotion verdict": payload.get("verdict") == protocol.PROMOTE,
            "selected variant": evidence.get("best") == variant_id,
            "fit-only selection": evidence.get("selection_window") == "fit",
            "minimum sample": int(evidence.get("n") or 0)
            >= protocol.MIN_ROUND_TRIPS,
            "axis settings": int(evidence.get("axis_settings") or 0)
            >= protocol.MIN_AXIS_SETTINGS,
            "paired sample": int(paired.get("paired_n") or 0)
            >= protocol.MIN_ROUND_TRIPS,
            "paired coverage": float(paired.get("pair_coverage_pct") or 0)
            >= protocol.MIN_PAIR_COVERAGE_PCT,
            "no duplicates": not (
                int(paired.get("left_duplicates") or 0)
                or int(paired.get("right_duplicates") or 0)),
            "dependence-aware bootstrap": (
                evidence_pair_ok(paired, protocol.MIN_ROUND_TRIPS)),
            "fit paired evidence": evidence_pair_ok(
                fit_paired, protocol.MIN_PAIRED_FIT_OBSERVATIONS),
            "confirmation paired evidence": evidence_pair_ok(
                confirmation_paired,
                protocol.MIN_PAIRED_CONFIRM_OBSERVATIONS),
            "out-of-sample survival": (
                (evidence.get("split") or {}).get("survives") is True),
            "fit interval": float(
                (evidence.get("fit_interval") or {}).get("low") or 0) > 0,
            "confirmation interval": float(
                (evidence.get("confirmation_interval") or {}).get("low") or 0)
            > 0,
            "criteria": required_criteria.issubset(criteria)
            and all(criteria.get(name) is True for name in required_criteria),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise ValueError(
                "forward analysis does not satisfy qualification protocol: "
                + ", ".join(failed))
        return payload

    def qualify_variant(
            self, variant_id: str, detail: dict, *,
            source_analysis_id: str | None = None,
            scope_key: str = "*") -> str:
        from agent.forward_models import require_validated

        with _connect(self.path) as conn:
            variant = self._require_variant(conn, variant_id)
            require_validated(str(variant["strategy_id"]))
            self._validated_forward_analysis(
                conn, variant_id, source_analysis_id, scope_key)
            current = self._latest_qualification(
                conn, variant_id, scope_key)
            if current and current["status"] == "QUALIFIED":
                return str(current["event_id"])
            event_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO edge_qualification_events (event_id, variant_id, "
                "scope_key, status, ts, source_analysis_id, detail_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (event_id, variant_id, scope_key, "QUALIFIED", time.time(),
                 source_analysis_id,
                json.dumps(detail, sort_keys=True, default=str)))
        return event_id

    def revoke_variant(
            self, variant_id: str, detail: dict, *,
            scope_key: str = "*") -> str:
        current = self.qualification_status(variant_id, scope_key)
        if current and current["status"] == "REVOKED":
            return str(current["event_id"])
        event_id = uuid.uuid4().hex
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            conn.execute(
                "INSERT INTO edge_qualification_events (event_id, variant_id, "
                "scope_key, status, ts, detail_json) VALUES (?,?,?,?,?,?)",
                (event_id, variant_id, scope_key, "REVOKED", time.time(),
                 json.dumps(detail, sort_keys=True, default=str)))
        return event_id

    # --------------------------------------------------------- evidence links

    def record_run_evidence(self, run_id: str, evidence: dict) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                "INSERT INTO run_evidence (run_id, evidence_json, created_ts) "
                "VALUES (?,?,?)",
                (run_id, json.dumps(evidence, sort_keys=True, default=str),
                 time.time()))

    def run_evidence(self, run_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT evidence_json FROM run_evidence WHERE run_id=?",
                (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _insert_analysis(
            conn: sqlite3.Connection, kind: str, subject_id: str,
            payload: dict) -> str:
        canonical, payload_hash = _analysis_canonical(
            kind, subject_id, payload)
        existing = conn.execute(
            "SELECT analysis_id FROM analysis_runs WHERE payload_hash=?",
            (payload_hash,)).fetchone()
        if existing is not None:
            return str(existing["analysis_id"])
        analysis_id = payload_hash[:32]
        conn.execute(
            "INSERT INTO analysis_runs (analysis_id, kind, subject_id, ts, "
            "payload_json, payload_hash) VALUES (?,?,?,?,?,?)",
            (analysis_id, kind, subject_id, time.time(), canonical,
             payload_hash))
        return analysis_id

    def record_analysis(self, kind: str, subject_id: str, payload: dict) -> str:
        if kind == "forward_parameter_axis":
            raise ValueError(
                "forward_parameter_axis analyses must be created from "
                "persisted trades by record_forward_analysis")
        with _connect(self.path) as conn:
            analysis_id = self._insert_analysis(
                conn, kind, subject_id, payload)
        self.backup()
        return analysis_id

    def analysis(self, analysis_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM analysis_runs WHERE analysis_id=?",
                (analysis_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = self._analysis_payload(row)
        item.pop("payload_json")
        return item

    @classmethod
    def _t3_evidence_is_backed_by_store(
            cls, conn: sqlite3.Connection, variant_id: str,
            payload: dict) -> bool:
        """Refuse reviewed status for a caller-authored checklist alone."""
        from research import protocol

        try:
            if payload.get("variant_id") != variant_id:
                return False
            scope_key = str(payload.get("scope_key") or "")
            if not scope_key:
                return False
            qualification = cls._latest_qualification(
                conn, variant_id, scope_key)
            if qualification is None or qualification["status"] != "QUALIFIED":
                return False
            supplied_qualification = payload.get("qualification") or {}
            if supplied_qualification.get("event_id") != qualification["event_id"]:
                return False
            analysis_id = str(qualification["source_analysis_id"] or "")
            cls._validated_forward_analysis(
                conn, variant_id, analysis_id, scope_key)
            supplied_analysis = payload.get("forward_analysis") or {}
            if supplied_analysis.get("analysis_id") != analysis_id:
                return False

            portfolio = conn.execute(
                "SELECT state_json FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, variant_id)).fetchone()
            if portfolio is None:
                return False
            state = json.loads(portfolio["state_json"])
            paper_started = state.get("paper_started_ts")
            if (state.get("status") != "PAPER" or paper_started is None
                    or state.get("revoked_reason")):
                return False
            paper = conn.execute(
                "SELECT COUNT(*) AS n, AVG(r_multiple) AS expectancy "
                "FROM paper_trades WHERE scope_key=? AND variant_id=? "
                "AND status='CLOSED' AND entry_ts>=?",
                (scope_key, variant_id, float(paper_started))).fetchone()
            if (int(paper["n"] or 0) < protocol.MIN_ROUND_TRIPS
                    or paper["expectancy"] is None
                    or float(paper["expectancy"]) <= 0):
                return False

            g2 = payload.get("g2") or {}
            provenance = payload.get("current_provenance") or {}
            if g2.get("status") != "PASS":
                return False
            if (g2.get("strategy_config_version")
                    != provenance.get("strategy_config_version")):
                return False
            if (g2.get("fidelity_code_version")
                    != provenance.get("fidelity_code_version")):
                return False
            return True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def create_t3_packet(
            self, variant_id: str, payload: dict, *, reviewed_by: str = "",
            registry_change_ref: str = "") -> str:
        required = {
            "current_g2_pass", "held_out_confirmation", "corpus_provenance",
            "config_provenance", "code_provenance", "forward_sample",
            "paper_sample", "paper_positive", "manual_registry_review",
        }
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            checklist = payload.get("checklist") or {}
            complete = (
                required.issubset(checklist)
                and all(bool(checklist[key]) for key in required)
                and self._t3_evidence_is_backed_by_store(
                    conn, variant_id, payload))
            status = (
                "REVIEWED"
                if reviewed_by and registry_change_ref and complete
                else "DRAFT_REVIEW_REQUIRED")
            canonical, payload_hash = _t3_canonical(
                variant_id, payload, status, reviewed_by or None,
                registry_change_ref or None)
            packet_id = payload_hash[:32]
            existing = conn.execute(
                "SELECT packet_id FROM t3_evidence_packets "
                "WHERE payload_hash=?", (payload_hash,)).fetchone()
            if existing:
                return str(existing[0])
            conn.execute(
                "INSERT INTO t3_evidence_packets (packet_id, variant_id, "
                "created_ts, review_status, reviewed_by, registry_change_ref, "
                "payload_json, payload_hash) VALUES (?,?,?,?,?,?,?,?)",
                (packet_id, variant_id, time.time(), status,
                 reviewed_by or None, registry_change_ref or None,
                 canonical, payload_hash))
        self.backup()
        return packet_id

    def t3_packets_for(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM t3_evidence_packets WHERE variant_id=? "
                "ORDER BY created_ts", (variant_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            out.append(item)
        return out

    # ------------------------------------------------------------- variants

    def register(self, variant) -> None:
        """Register immutable experiment identity; status alone may change.

        ``updated_ts`` reaches the committed index, so bumping it on every
        run would make ``research.py report`` produce a diff every time it
        was invoked - and a diff that always appears is a diff nobody reads.
        """
        now = time.time()
        overrides = json.dumps(variant.overrides, sort_keys=True)
        with _connect(self.path) as conn:
            existing = conn.execute(
                "SELECT * FROM variants WHERE variant_id=?",
                (variant.variant_id,)).fetchone()
            if existing is not None:
                if _variant_identity(existing) != _variant_identity(variant):
                    raise ValueError(
                        f"{variant.variant_id}: registered experiment identity "
                        "is immutable; use a new variant_id for changes to "
                        "strategy, version, overrides, or hypothesis")
                if existing["status"] == variant.status:
                    return
                conn.execute(
                    "UPDATE variants SET status=?, updated_ts=? "
                    "WHERE variant_id=?",
                    (variant.status, now, variant.variant_id))
                return
            conn.execute(
                "INSERT INTO variants (variant_id, strategy_id, "
                "base_version, overrides_json, hypothesis, status, "
                "created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (variant.variant_id, variant.strategy_id,
                 variant.base_version, overrides, variant.hypothesis,
                 variant.status, now, now))

    def set_status(self, variant_id: str, status: str) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                "UPDATE variants SET status=?, updated_ts=? "
                "WHERE variant_id=?", (status, time.time(), variant_id))

    def variants(self) -> list:
        with _connect(self.path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM variants ORDER BY variant_id")]

    def variant(self, variant_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM variants WHERE variant_id=?",
                (variant_id,)).fetchone()
        return dict(row) if row else None

    # ----------------------------------------------------------------- runs

    def record_run(self, run_id: str, variant_id: str, result,
                   scorer_version: str = "1", code_version: str = "") -> None:
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            conn.execute(
                "INSERT INTO variant_runs (run_id, variant_id, "
                "corpus_from_ts, corpus_to_ts, corpus_cycles, mode, "
                "code_version, scorer_version, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, variant_id, result.corpus_from_ts,
                 result.corpus_to_ts, result.cycles, result.mode,
                 code_version, scorer_version, time.time()))

    def record_metrics(self, run_id: str, scored: dict) -> None:
        rows = self._metric_rows(run_id, scored)
        with _connect(self.path) as conn:
            if conn.execute(
                    "SELECT 1 FROM variant_runs WHERE run_id=?",
                    (run_id,)).fetchone() is None:
                raise ValueError(f"unknown run_id {run_id!r}")
            conn.executemany(
                "INSERT INTO variant_results (run_id, metric, value, "
                "ci_low, ci_high, n) VALUES (?,?,?,?,?,?)", rows)

    def record_evaluation(
            self, run_id: str, variant_id: str, result, scored: dict,
            finding_text: str, kind: str = "decision",
            author: str = "research", scorer_version: str = "1",
            code_version: str = "") -> int:
        """Atomically append one run, its metrics, and its conclusion."""
        if kind not in KINDS:
            raise ValueError(
                f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
        rows = self._metric_rows(run_id, scored)
        now = time.time()
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            conn.execute(
                "INSERT INTO variant_runs (run_id, variant_id, "
                "corpus_from_ts, corpus_to_ts, corpus_cycles, mode, "
                "code_version, scorer_version, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, variant_id, result.corpus_from_ts,
                 result.corpus_to_ts, result.cycles, result.mode,
                 code_version, scorer_version, now))
            conn.executemany(
                "INSERT INTO variant_results (run_id, metric, value, "
                "ci_low, ci_high, n) VALUES (?,?,?,?,?,?)", rows)
            cursor = conn.execute(
                "INSERT INTO findings (variant_id, ts, author, kind, text, "
                "run_id) VALUES (?,?,?,?,?,?)",
                (variant_id, now, author, kind, finding_text, run_id))
            finding_id = int(cursor.lastrowid)
        self.backup()
        return finding_id

    @staticmethod
    def _metric_rows(run_id: str, scored: dict) -> list:
        rows = []
        for metric, value in scored.items():
            if metric in ("label", "verdict"):
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            rows.append((run_id, metric, numeric,
                         float(scored.get("ci_low") or 0.0),
                         float(scored.get("ci_high") or 0.0),
                         int(scored.get("n") or 0)))
        return rows

    @staticmethod
    def _require_variant(
            conn: sqlite3.Connection, variant_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM variants WHERE variant_id=?", (variant_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown variant_id {variant_id!r}")
        return row

    def runs_for(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM variant_runs WHERE variant_id=? "
                "ORDER BY ts DESC", (variant_id,))]

    def metrics_for(self, run_id: str) -> dict:
        with _connect(self.path) as conn:
            return {r["metric"]: dict(r) for r in conn.execute(
                "SELECT * FROM variant_results WHERE run_id=?", (run_id,))}

    # ------------------------------------------------------------- findings

    def add_finding(self, variant_id: str, kind: str, text: str,
                    author: str = "research", run_id: str = "",
                    ts: float | None = None) -> int:
        """Append a finding. Nothing in this class ever deletes one."""
        if kind not in KINDS:
            raise ValueError(
                f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            if run_id and conn.execute(
                    "SELECT 1 FROM variant_runs WHERE run_id=?",
                    (run_id,)).fetchone() is None:
                raise ValueError(f"unknown run_id {run_id!r}")
            cursor = conn.execute(
                "INSERT INTO findings (variant_id, ts, author, kind, text, "
                "run_id) VALUES (?,?,?,?,?,?)",
                (variant_id, ts if ts is not None else time.time(),
                 author, kind, text, run_id or None))
            finding_id = int(cursor.lastrowid)
        self.backup()
        return finding_id

    def findings_for(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM findings WHERE variant_id=? "
                "ORDER BY ts ASC, finding_id ASC", (variant_id,))]


# --------------------------------------------------------------- scorecards

def _fmt(value, spec: str = "+.4f") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number == float("inf"):
        return "inf"
    return format(number, spec)


def scorecard(store: FindingsStore, variant_id: str,
              baseline_id: str = "momentum.baseline") -> str:
    """One markdown file per variant, regenerated deterministically.

    Deterministic matters because these are committed: a generator whose
    output moved on every run would produce a diff on every run, and a diff
    that always appears is a diff nobody reads.

    Nothing here is derived from ``time.time()`` for that reason - the
    timestamps come from the stored rows.
    """
    variant = store.variant(variant_id)
    if variant is None:
        return f"# {variant_id}\n\nNot registered.\n"

    runs = store.runs_for(variant_id)
    latest = runs[0] if runs else None
    metrics = store.metrics_for(latest["run_id"]) if latest else {}
    overrides = json.loads(variant["overrides_json"] or "{}")

    lines = [f"# {variant_id}", ""]
    lines.append(f"Status: {variant['status']}")
    lines.append(f"Hypothesis: {variant['hypothesis']}")
    if overrides:
        rendered = ", ".join(f"{k} = {v}" for k, v in sorted(overrides.items()))
        lines.append(f"Overrides: {rendered}")
    else:
        lines.append("Overrides: none (this is the comparison floor)")
    lines.append("")

    lines.append("## Sample")
    if latest is None:
        lines += ["", "Registered but never run. No sample, and therefore no "
                      "result to report.", ""]
    else:
        n = int((metrics.get("n") or {}).get("n")
                or (metrics.get("expectancy_r") or {}).get("n") or 0)
        mde = (metrics.get("mde_r") or {}).get("value")
        lines += [
            "",
            f"corpus {int(latest['corpus_cycles'] or 0):,} cycles | "
            f"mode {latest['mode']} | {n} round trips",
            f"MDE at n={n}: {_fmt(mde, '.4f')}R "
            f"-- effects below this are undetectable",
            "",
        ]
        lines.append("## Results")
        lines.append("")
        lines.append("| metric | value | 95% interval |")
        lines.append("| --- | --- | --- |")
        for name in ("expectancy_r", "win_rate", "profit_factor", "total_r",
                     "max_drawdown_r"):
            row = metrics.get(name)
            if not row:
                continue
            interval = (f"[{_fmt(row['ci_low'])}, {_fmt(row['ci_high'])}]"
                        if name == "expectancy_r" else "")
            lines.append(f"| {name} | {_fmt(row['value'])} | {interval} |")
        lines.append("")

    runtime_rows = []
    for scope in store.paper_scopes():
        state = store.paper_portfolio_state(scope, variant_id)
        if state is None:
            continue
        all_summary = store.paper_summary(
            scope, variant_id, paper_only=False)
        paper_summary = store.paper_summary(
            scope, variant_id, paper_only=True)
        coverage = next((row for row in store.scheduler_coverage(scope)
                         if row["variant_id"] == variant_id), {})
        qualification = store.qualification_status(variant_id, scope) or {}
        runtime_rows.append((scope, state, all_summary, paper_summary,
                             coverage, qualification))
    if runtime_rows:
        lines += [
            "## Real-time learning and paper",
            "",
            "| scope | stage | evaluations / skips | accepts / vetoes | "
            "shadow closed | "
            "paper closed | paper expectancy | qualification |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for (scope, state, all_summary, paper_summary, coverage,
             qualification) in runtime_rows:
            paper_closed = int(paper_summary["closed_trades"] or 0)
            shadow_closed = max(
                0, int(all_summary["closed_trades"] or 0) - paper_closed)
            expectancy = _fmt(paper_summary.get("expectancy_r"))
            lines.append(
                f"| {scope} | {state.get('status', '-')} | "
                f"{int(coverage.get('evaluations') or 0)} / "
                f"{int(coverage.get('skips') or 0)} | "
                f"{int(all_summary.get('accepted_decisions') or 0)} / "
                f"{int(all_summary.get('vetoed_decisions') or 0)} | "
                f"{shadow_closed} | "
                f"{paper_closed} | {expectancy} | "
                f"{qualification.get('status', '-')} |")
        lines.append("")

    packets = store.t3_packets_for(variant_id)
    if packets:
        lines += ["## T3 evidence packets", ""]
        for packet in packets:
            lines.append(
                f"- `{packet['packet_id']}` — {packet['review_status']} — "
                f"SHA-256 `{packet.get('payload_hash') or 'legacy-unhashed'}`")
        lines.append("")

    log = store.findings_for(variant_id)
    lines.append("## Findings log")
    lines.append("")
    if not log:
        lines.append("No findings recorded yet.")
    else:
        for entry in log:
            stamp = time.strftime("%Y-%m-%d",
                                  time.gmtime(float(entry["ts"] or 0)))
            lines.append(f"- **{stamp}  {entry['kind']}** — {entry['text']}")
    lines.append("")
    return "\n".join(lines)


def index(store: FindingsStore) -> str:
    """``findings/README.md``: every variant, status, sample, last updated."""
    lines = [
        "# Findings index",
        "",
        "Every registered variant, including the rejected ones. A rejection "
        "is a row here, never a deletion: the question six months from now "
        "is not which variants are alive but why this one was rejected and "
        "on what sample.",
        "",
        "| variant | status | round trips | expectancy | last updated |",
        "| --- | --- | --- | --- | --- |",
    ]
    for variant in store.variants():
        runs = store.runs_for(variant["variant_id"])
        metrics = store.metrics_for(runs[0]["run_id"]) if runs else {}
        expectancy = metrics.get("expectancy_r")
        n = int((expectancy or {}).get("n") or 0)
        updated = time.strftime(
            "%Y-%m-%d", time.gmtime(float(variant["updated_ts"] or 0)))
        lines.append(
            f"| [{variant['variant_id']}]"
            f"({variant['strategy_id']}/{variant['variant_id']}.md) "
            f"| {variant['status']} | {n} "
            f"| {_fmt((expectancy or {}).get('value'))} | {updated} |")
    lines.append("")
    return "\n".join(lines)


def write_scorecards(store: FindingsStore, root: str | Path) -> list:
    """Regenerate every scorecard. Running twice with no new data is a no-op."""
    root = Path(root)
    written = []
    for variant in store.variants():
        directory = root / variant["strategy_id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{variant['variant_id']}.md"
        path.write_text(scorecard(store, variant["variant_id"]),
                        encoding="utf-8")
        written.append(path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(index(store), encoding="utf-8")
    written.append(root / "README.md")
    return written
