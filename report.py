#!/usr/bin/env python3
"""Small, dependency-free summaries for a mode-scoped Alpaca journal.

The runtime journal is deliberately an append-only execution ledger.  This
module only reads it and emits a compact JSON or CSV summary; it does not
attempt to promote strategies, score experiments, or contact a broker.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import csv
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "runtime" / "paper" / "journal.db"

def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def load_trade_events(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Load journal trade rows through a stable, best-effort shape."""
    columns = _columns(db, "trades")
    if not columns:
        return []
    fields = (
        "ts", "symbol", "side", "action", "qty", "price", "notional",
        "trade_id", "order_id", "realized_pnl_usd", "pnl_pct", "risk_usd",
        "fill_status", "strategy_id", "strategy_version", "setup_type",
        "runtime_mode",
    )
    selected = [name if name in columns else f"NULL AS {name}" for name in fields]
    db.row_factory = sqlite3.Row
    rows = db.execute(
        f"SELECT {', '.join(selected)} FROM trades ORDER BY ts, rowid"
    ).fetchall()
    return [dict(row) for row in rows]


def _equity_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _columns(db, "equity"):
        return []
    db.row_factory = sqlite3.Row
    return [dict(row) for row in db.execute(
        "SELECT ts, equity, state FROM equity ORDER BY ts, rowid").fetchall()]


def _finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _safe(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def _summary(events: list[dict[str, Any]], equity: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [row for row in events if str(row.get("action") or "").lower()
              in {"close", "partial_close", "exit", "sell_to_close"}]
    realized = [_finite(row.get("realized_pnl_usd")) for row in closes]
    realized = [value for value in realized if value is not None]
    pnl_pct = [_finite(row.get("pnl_pct")) for row in closes]
    pnl_pct = [value for value in pnl_pct if value is not None]
    wins = sum(value > 0 for value in realized)
    latest_equity = _finite(equity[-1].get("equity")) if equity else None
    first_equity = _finite(equity[0].get("equity")) if equity else None
    modes = {str(row.get("runtime_mode") or "").lower() for row in events
             if str(row.get("runtime_mode") or "").lower() in {"paper", "live"}}
    scope = (f"alpaca-{next(iter(modes))}" if len(modes) == 1 else
             "alpaca-mixed" if len(modes) > 1 else "alpaca-paper")
    return {
        "schema": 1,
        "scope": scope,
        "events": len(events),
        "closed_trades": len(closes),
        "realized_pnl_usd": sum(realized) if realized else 0.0,
        "average_pnl_usd": (sum(realized) / len(realized) if realized else None),
        "win_rate": (wins / len(realized) if realized else None),
        "average_pnl_pct": (sum(pnl_pct) / len(pnl_pct) if pnl_pct else None),
        "latest_equity_usd": latest_equity,
        "equity_change_usd": (
            latest_equity - first_equity
            if latest_equity is not None and first_equity is not None else None),
        "latest_event_ts": events[-1].get("ts") if events else None,
    }


def json_report(db: sqlite3.Connection) -> dict[str, Any]:
    """Return a compact machine-readable journal summary."""
    events = load_trade_events(db)
    return _safe(_summary(events, _equity_rows(db)))  # type: ignore[return-value]


def csv_report(db: sqlite3.Connection) -> str:
    """Return the summary as one CSV record, suitable for cron artifacts."""
    summary = json_report(db)
    fields = list(summary)
    from io import StringIO
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerow(summary)
    return output.getvalue()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", nargs="?", type=Path, default=DEFAULT_DB)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit JSON (default)")
    output.add_argument("--csv", action="store_true", help="emit one CSV summary row")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.journal.is_file():
        print(f"journal not found: {args.journal}", file=sys.stderr)
        return 1
    try:
        with closing(sqlite3.connect(args.journal)) as db:
            if args.csv:
                print(csv_report(db), end="")
            else:
                print(json.dumps(json_report(db), sort_keys=True,
                                  allow_nan=False))
    except (OSError, sqlite3.Error) as exc:
        print(f"journal read failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
