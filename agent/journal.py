"""SQLite-backed operational journal primitives.

This module deliberately owns no runtime paths or process state.  Callers
provide the journal path, runtime directory, scope, schema aliases, and
SQLite connection factory for each operation.  Keeping those values at the
call boundary lets :mod:`agent.state` remain a backwards-compatible facade
whose runtime globals can be rebound by tests and long-running processes.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Mapping


class JournalNotReady(RuntimeError):
    """The durable SQLite state/journal is unavailable."""


class _ClosingConnection(sqlite3.Connection):
    """Close SQLite handles when used as context managers on Python 3.14+."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


_JOURNAL_TABLES = {
    "events": """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        kind TEXT NOT NULL, payload TEXT NOT NULL,
        run_id TEXT, cycle_id TEXT, strategy_id TEXT, strategy_version TEXT,
        setup_id TEXT, prompt_version TEXT, config_version TEXT,
        code_version TEXT, equity_basis_id TEXT, runtime_mode TEXT,
        account_fingerprint TEXT, variant_id TEXT,
        strategy_config_version TEXT
    )""",
    "orders": """CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        order_id TEXT, client_order_id TEXT, symbol TEXT, side TEXT,
        action TEXT, qty REAL, type TEXT, time_in_force TEXT, status TEXT,
        filled_qty REAL, filled_avg_price REAL, reason TEXT,
        run_id TEXT, cycle_id TEXT, runtime_mode TEXT,
        account_fingerprint TEXT, setup_id TEXT,
        execution_profile TEXT, vehicle TEXT, variant_id TEXT,
        reference_price REAL, entry_reference REAL, exit_reference REAL,
        market_price REAL, mid_price REAL, requested_qty REAL,
        planned_qty REAL, cumulative_filled_qty REAL, fill_fraction REAL,
        filled_fraction REAL, risk_usd REAL, intended_risk_usd REAL,
        delivered_risk_usd REAL, risk_delivery_ratio REAL,
        risk_shortfall_usd REAL, configured_risk_budget_usd REAL,
        planned_risk_usd REAL,
        planned_to_configured_risk_ratio REAL,
        delivered_to_configured_risk_ratio REAL
    )""",
    "trades": """CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        symbol TEXT, side TEXT, action TEXT, qty REAL, price REAL,
        notional REAL, leverage REAL, reason TEXT, confidence REAL,
        pnl_pct REAL, trade_id TEXT, order_id TEXT, fee_usd REAL,
        funding_usd REAL, realized_pnl_usd REAL, risk_usd REAL,
        fill_status TEXT, slippage_usd REAL, adverse_slippage_usd REAL,
        funding_status TEXT, pnl_semantics TEXT, strategy_id TEXT,
        gross_pnl REAL, fees REAL, slippage REAL, net_pnl REAL,
        pnl_provenance TEXT, cost_provenance TEXT,
        entry_fill_source TEXT, exit_fill_source TEXT,
        entry_feed TEXT, exit_feed TEXT,
        entry_provider TEXT, exit_provider TEXT,
        entry_quote_age_seconds REAL, exit_quote_age_seconds REAL,
        signal_bar_feed TEXT, signal_bar_provider TEXT,
        signal_bar_age_seconds REAL, entry_bar_feed TEXT,
        entry_bar_provider TEXT, exit_bar_feed TEXT,
        exit_bar_provider TEXT, entry_bar_age_seconds REAL,
        exit_bar_age_seconds REAL,
        strategy_version TEXT, setup_id TEXT, setup_key TEXT, setup_type TEXT,
        signal_ts REAL, exit_policy TEXT, invalidation_anchor TEXT,
        run_id TEXT, cycle_id TEXT, runtime_mode TEXT,
        account_fingerprint TEXT, variant_id TEXT,
        strategy_config_version TEXT, close_trigger TEXT, close_evidence TEXT,
        entry_equity_usd REAL,
        execution_profile TEXT, vehicle TEXT, reference_price REAL,
        entry_reference REAL, exit_reference REAL, market_price REAL,
        mid_price REAL, requested_qty REAL, planned_qty REAL,
        cumulative_filled_qty REAL, fill_fraction REAL, filled_fraction REAL,
        intended_risk_usd REAL, delivered_risk_usd REAL,
        risk_delivery_ratio REAL, risk_shortfall_usd REAL,
        configured_risk_budget_usd REAL,
        planned_risk_usd REAL, planned_to_configured_risk_ratio REAL,
        delivered_to_configured_risk_ratio REAL, nominal_risk_usd REAL
    )""",
    "equity": """CREATE TABLE IF NOT EXISTS equity (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
        equity REAL, state TEXT, basis_id TEXT, run_id TEXT, cycle_id TEXT,
        runtime_mode TEXT, account_fingerprint TEXT
    )""",
    "runs": """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, started_ts REAL NOT NULL, mode TEXT,
        strategy_id TEXT, strategy_version TEXT, model TEXT,
        prompt_version TEXT, config_version TEXT, code_version TEXT,
        account_fingerprint TEXT
    )""",
    "schema_meta": """CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""",
}

# These durable columns make each writer's row meaningful.  Migration may add
# nullable, deployment-era columns, but never silently treats a partial base
# table as ready.
_JOURNAL_REQUIRED_COLUMNS = {
    "events": {"ts", "kind", "payload"},
    "orders": {"ts", "action"},
    "trades": {"ts", "symbol", "action", "qty"},
    "equity": {"ts", "equity", "state"},
    "schema_meta": {"key", "value"},
}


def _journal_columns(db, table: str) -> set[str]:
    """Return the columns currently present in one SQLite table."""
    cursor = db.execute(f"PRAGMA table_info({table})")
    try:
        return {row[1] for row in cursor.fetchall()}
    finally:
        cursor.close()


def _validate_existing_journal_schema(
    db,
    runtime_scope: str = "_unconfigured",
    required_columns: Mapping[str, set[str]] | None = None,
) -> None:
    """Reject malformed existing base tables before migration writes."""
    required_columns = (required_columns if required_columns is not None
                        else _JOURNAL_REQUIRED_COLUMNS)
    cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    try:
        existing = {row[0] for row in cursor.fetchall()}
    finally:
        cursor.close()
    for table, required in required_columns.items():
        if table not in existing:
            continue
        present = _journal_columns(db, table)
        missing = set(required) - present
        if missing:
            names = ", ".join(sorted(missing))
            raise JournalNotReady(
                f"{runtime_scope} journal table {table!r} is missing columns: {names}"
            )


def _resolve_connect(connect: Callable | None) -> Callable:
    return sqlite3.connect if connect is None else connect


def _execute_with_lock_retry(db, statement: str, *, attempts: int = 20):
    """Run a schema statement through brief SQLite lock contention."""
    for attempt in range(max(1, int(attempts))):
        try:
            return db.execute(statement)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt + 1 >= attempts:
                raise
            time.sleep(0.05)


def initialize_journal(
    journal_file: str | Path,
    runtime: str | Path,
    runtime_scope: str,
    *,
    tables: Mapping[str, str] | None = None,
    required_columns: Mapping[str, set[str]] | None = None,
    connect: Callable | None = None,
    connection_factory: type[sqlite3.Connection] | None = None,
) -> Path:
    """Create or migrate an append-only operational journal.

    All path/context and compatibility seams are explicit arguments.  In
    particular, ``connect`` is resolved at call time when omitted rather than
    captured as a module-level default.
    """
    tables = _JOURNAL_TABLES if tables is None else tables
    required_columns = (_JOURNAL_REQUIRED_COLUMNS
                        if required_columns is None else required_columns)
    connect = _resolve_connect(connect)
    connection_factory = (_ClosingConnection if connection_factory is None
                          else connection_factory)
    journal_path = Path(journal_file)
    runtime_path = Path(runtime)
    try:
        runtime_path.mkdir(parents=True, exist_ok=True)
        with connect(journal_path, timeout=5, factory=connection_factory) as db:
            db.execute("PRAGMA busy_timeout=5000")
            _validate_existing_journal_schema(
                db, runtime_scope, required_columns)
            _execute_with_lock_retry(db, "PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            for ddl in tables.values():
                db.execute(ddl)
            # Older deployed databases predate the orders table and may omit
            # a few optional columns. Add only columns required by writers.
            migration_columns = {
                "events": {
                    "run_id": "TEXT", "cycle_id": "TEXT",
                    "runtime_mode": "TEXT", "account_fingerprint": "TEXT",
                },
                "orders": {
                    "reason": "TEXT", "run_id": "TEXT", "cycle_id": "TEXT",
                    "runtime_mode": "TEXT", "account_fingerprint": "TEXT",
                    "setup_id": "TEXT", "execution_profile": "TEXT",
                    "vehicle": "TEXT", "variant_id": "TEXT",
                    "reference_price": "REAL", "entry_reference": "REAL",
                    "exit_reference": "REAL", "market_price": "REAL",
                    "mid_price": "REAL", "requested_qty": "REAL",
                    "planned_qty": "REAL", "cumulative_filled_qty": "REAL",
                    "fill_fraction": "REAL", "filled_fraction": "REAL",
                    "risk_usd": "REAL", "intended_risk_usd": "REAL",
                    "delivered_risk_usd": "REAL",
                    "risk_delivery_ratio": "REAL",
                    "risk_shortfall_usd": "REAL",
                    "configured_risk_budget_usd": "REAL",
                    "planned_risk_usd": "REAL",
                    "planned_to_configured_risk_ratio": "REAL",
                    "delivered_to_configured_risk_ratio": "REAL",
                    "gross_pnl": "REAL", "fees": "REAL", "slippage": "REAL",
                    "net_pnl": "REAL", "pnl_provenance": "TEXT",
                    "cost_provenance": "TEXT",
                    "entry_fill_source": "TEXT", "exit_fill_source": "TEXT",
                    "entry_feed": "TEXT", "exit_feed": "TEXT",
                    "entry_provider": "TEXT", "exit_provider": "TEXT",
                    "entry_quote_age_seconds": "REAL",
                    "exit_quote_age_seconds": "REAL",
                    "signal_bar_feed": "TEXT", "signal_bar_provider": "TEXT",
                    "signal_bar_age_seconds": "REAL",
                    "entry_bar_feed": "TEXT", "entry_bar_provider": "TEXT",
                    "exit_bar_feed": "TEXT", "exit_bar_provider": "TEXT",
                    "entry_bar_age_seconds": "REAL",
                    "exit_bar_age_seconds": "REAL",
                    "nominal_risk_usd": "REAL",
                },
                "trades": {
                    "run_id": "TEXT", "cycle_id": "TEXT",
                    "runtime_mode": "TEXT", "account_fingerprint": "TEXT",
                    "risk_usd": "REAL",
                    "execution_profile": "TEXT", "vehicle": "TEXT",
                    "reference_price": "REAL", "entry_reference": "REAL",
                    "exit_reference": "REAL", "market_price": "REAL",
                    "mid_price": "REAL", "requested_qty": "REAL",
                    "planned_qty": "REAL", "cumulative_filled_qty": "REAL",
                    "fill_fraction": "REAL", "filled_fraction": "REAL",
                    "intended_risk_usd": "REAL", "delivered_risk_usd": "REAL",
                    "risk_delivery_ratio": "REAL",
                    "risk_shortfall_usd": "REAL",
                    "configured_risk_budget_usd": "REAL",
                    "planned_risk_usd": "REAL",
                    "planned_to_configured_risk_ratio": "REAL",
                    "delivered_to_configured_risk_ratio": "REAL",
                    "gross_pnl": "REAL", "fees": "REAL", "slippage": "REAL",
                    "net_pnl": "REAL", "pnl_provenance": "TEXT",
                    "cost_provenance": "TEXT",
                    "entry_fill_source": "TEXT", "exit_fill_source": "TEXT",
                    "entry_feed": "TEXT", "exit_feed": "TEXT",
                    "entry_provider": "TEXT", "exit_provider": "TEXT",
                    "entry_quote_age_seconds": "REAL",
                    "exit_quote_age_seconds": "REAL",
                    "signal_bar_feed": "TEXT", "signal_bar_provider": "TEXT",
                    "signal_bar_age_seconds": "REAL",
                    "entry_bar_feed": "TEXT", "entry_bar_provider": "TEXT",
                    "exit_bar_feed": "TEXT", "exit_bar_provider": "TEXT",
                    "entry_bar_age_seconds": "REAL",
                    "exit_bar_age_seconds": "REAL",
                    "nominal_risk_usd": "REAL",
                },
                "equity": {
                    "run_id": "TEXT", "cycle_id": "TEXT",
                    "runtime_mode": "TEXT", "account_fingerprint": "TEXT",
                },
            }
            for table, columns in migration_columns.items():
                # A caller may intentionally provide a reduced schema map for
                # seam tests. Only migrate tables that were requested.
                if table not in tables:
                    continue
                present = _journal_columns(db, table)
                for name, typ in columns.items():
                    if name not in present:
                        try:
                            db.execute(
                                f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                        except sqlite3.OperationalError as exc:
                            # Startup can race with another process migrating
                            # the same mode-scoped database.  Re-probe after
                            # SQLite's duplicate-column error and continue
                            # only when that process completed this exact
                            # migration; all other failures stay fail-closed.
                            if "duplicate column name" not in str(exc).lower() or \
                                    name not in _journal_columns(db, table):
                                raise
                        present.add(name)
            if "schema_meta" in tables:
                db.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                    ("runtime_schema", "2"),
                )
            db.commit()
    except JournalNotReady:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise JournalNotReady(
            f"{runtime_scope} journal unavailable: {exc}"
        ) from exc
    return journal_path


def journal_ready(
    journal_file: str | Path,
    runtime_scope: str = "_unconfigured",
    *,
    runtime: str | Path | None = None,
    tables: Mapping[str, str] | None = None,
    required_columns: Mapping[str, set[str]] | None = None,
    connect: Callable | None = None,
    connection_factory: type[sqlite3.Connection] | None = None,
) -> bool:
    """Check required journal tables and columns without initializing it."""
    # ``runtime`` and ``tables`` are accepted for facade seam symmetry.  A
    # readiness probe only needs the journal path and required columns, but
    # callers pass their current aliases on every operation so rebinding is
    # never accidentally hidden by this adapter.
    del runtime, tables
    required_columns = (_JOURNAL_REQUIRED_COLUMNS
                        if required_columns is None else required_columns)
    connect = _resolve_connect(connect)
    connection_factory = (_ClosingConnection if connection_factory is None
                          else connection_factory)
    try:
        with connect(Path(journal_file), timeout=1,
                     factory=connection_factory) as db:
            cursor = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            try:
                tables = {row[0] for row in cursor.fetchall()}
            finally:
                cursor.close()
            required_tables = tuple(required_columns)
            if not set(required_tables).issubset(tables):
                return False
            return all(
                set(required_columns[table]).issubset(
                    _journal_columns(db, table)
                )
                for table in required_tables
            )
    except (OSError, sqlite3.Error, RuntimeError):
        return False


def insert(
    journal_file: str | Path,
    table: str,
    payload: Mapping[str, Any],
    runtime_scope: str = "_unconfigured",
    *,
    runtime: str | Path | None = None,
    tables: Mapping[str, str] | None = None,
    required_columns: Mapping[str, set[str]] | None = None,
    connect: Callable | None = None,
    connection_factory: type[sqlite3.Connection] | None = None,
) -> None:
    """Insert a payload after filtering it to columns in ``table``."""
    # These aliases are intentionally accepted even though generic insertion
    # only needs the path and table metadata.  They preserve state facade seam
    # rebinding and make the adapter's context explicit at each call.
    del runtime, tables, required_columns
    connect = _resolve_connect(connect)
    connection_factory = (_ClosingConnection if connection_factory is None
                          else connection_factory)
    try:
        with connect(Path(journal_file), timeout=5,
                     factory=connection_factory) as db:
            db.execute("PRAGMA busy_timeout=5000")
            columns = _journal_columns(db, table)
            row = {
                str(key): value for key, value in payload.items()
                if str(key) in columns
            }
            if not row:
                raise JournalNotReady(
                    f"journal table {table!r} has no writable columns"
                )
            names = list(row)
            placeholders = ",".join("?" for _ in names)
            db.execute(
                f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders})",
                [row[name] for name in names],
            )
            db.commit()
    except JournalNotReady:
        raise
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise JournalNotReady(f"journal write failed: {exc}") from exc


# Descriptive alias for callers that prefer the operation's role in the
# journal facade.  ``insert`` remains the primary generic primitive.
journal_insert = insert
