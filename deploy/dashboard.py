#!/usr/bin/env python3
"""Read-only local dashboard over durable runtime and research evidence."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import mimetypes
import os
import sqlite3
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlencode, urlparse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deploy import health, load_config
from deploy.provenance import deployment_provenance
from deploy.scheduler_output import (derive_research_readiness,
                                     structured_research_preflight,
                                     structured_research_progress,
                                     structured_research_readiness)
from research.gates import verify_gate_envelope


SAFE_STATE_FIELDS = (
    "state", "operator_pause", "runtime_mode",
    "account_fingerprint", "day", "day_start_equity", "high_water_mark",
    "equity_basis", "transfer_reconciliation_required",
)
SAFE_TRADE_FIELDS = (
    "symbol", "direction", "position_side", "qty", "entry_price", "current_price",
    "opened_at", "setup_type", "variant_id", "underlying_symbol", "vehicle",
    "execution_profile", "stop_price", "target_price", "active_stop_price",
    "hold_deadline_ts", "intraday_context",
    "strategy_id", "strategy_version", "stop_loss_pct", "take_profit_pct",
    "intended_risk_usd", "delivered_risk_usd", "risk_delivery_ratio",
    "risk_shortfall_usd", "configured_risk_budget_usd", "planned_risk_usd",
    "planned_to_configured_risk_ratio",
    "delivered_to_configured_risk_ratio",
)
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()

def _content_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cached(key: str, ttl_seconds: float, loader):
    now = time.monotonic()
    with _CACHE_LOCK:
        existing = _CACHE.get(key)
        if existing and existing[0] > now:
            return existing[1]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = (now + ttl_seconds, value)
    return value


def _json_file(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _recorder_corpus_path(root: Path) -> Path:
    """Resolve the current recorder epoch inside the mounted runtime tree."""
    raw = str(os.getenv("ALPACA_RECORDER_CORPUS_ROOT") or
              "runtime/research/recorded").strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    runtime_root = (root / "runtime").resolve()
    if not candidate.is_relative_to(runtime_root):
        raise ValueError(
            "ALPACA_RECORDER_CORPUS_ROOT must remain inside runtime")
    return candidate


def _safe_state(path: Path) -> dict:
    raw = _json_file(path)
    result = {key: raw.get(key) for key in SAFE_STATE_FIELDS if key in raw}
    trades = raw.get("active_trades")
    if isinstance(trades, dict):
        result["active_trades"] = [
            {key: ({"symbol": symbol, **trade}).get(key)
             for key in SAFE_TRADE_FIELDS
             if key in {"symbol", *trade.keys()}}
            for symbol, trade in sorted(trades.items()) if isinstance(trade, dict)
        ]
    else:
        result["active_trades"] = []
    return result


def _portfolio_exposure(trades: Sequence[dict]) -> dict:
    """Describe recorded position dollars without inventing independent bets.

    These coarse symbol buckets are labels, not measured betas or risk gates.
    Options cannot be converted to equity exposure without a delta snapshot.
    """
    equity_etfs = {"SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV",
                   "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "VTI", "VO", "VB", "SMH"}
    groups = {}
    unknown = 0
    for trade in trades:
        symbol = str(trade.get("underlying_symbol") or trade.get("symbol") or "").upper()
        if str(trade.get("vehicle") or "").lower() == "option" or str(trade.get("execution_profile") or "").lower() in {"option", "options"}:
            unknown += 1
            continue
        group = ("US equity ETFs" if symbol in equity_etfs else
                 "Treasury ETFs" if symbol in {"TLT", "IEF", "SHY"} else
                 "Precious metals ETFs" if symbol in {"GLD", "SLV", "IAU"} else
                 "Other / unclassified")
        try:
            price = float(trade.get("current_price") or trade.get("entry_price"))
            qty = abs(float(trade.get("qty")))
        except (TypeError, ValueError, OverflowError):
            unknown += 1
            continue
        if not math.isfinite(price * qty) or price <= 0 or qty <= 0:
            unknown += 1
            continue
        direction = str(trade.get("position_side") or trade.get("direction") or "").lower()
        if direction not in {"long", "short"}:
            unknown += 1
            continue
        dollars = price * qty
        item = groups.setdefault(group, {"group": group, "gross_usd": 0.0,
                                        "net_usd": 0.0, "positions": 0})
        item["gross_usd"] += dollars
        item["net_usd"] += dollars if direction == "long" else -dollars
        item["positions"] += 1
    return {"groups": list(groups.values()), "unpriced_or_unmapped_positions": unknown,
            "basis": "recorded_price_notional", "pending_orders_included": False,
            "beta_adjusted": False, "independent_bets_estimated": False}


def _ro_connect(path: Path) -> sqlite3.Connection:
    """Open a journal without requiring writes beside a WAL-mode database.

    SQLite normally creates ``-shm`` state when the database header says WAL,
    even for a ``mode=ro`` connection.  The dashboard deliberately receives a
    read-only volume, so a fully checkpointed journal with no remaining WAL
    sidecar otherwise fails with ``unable to open database file``.  In that
    exact case an immutable connection is safe: there is no uncheckpointed WAL
    to ignore.  If a non-empty WAL exists, fail closed instead of presenting a
    stale main-database snapshot.
    """

    def opened(uri: str) -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=2000")
            # Connection creation is lazy. Force the first schema read here so
            # a read-only WAL/SHM failure is handled before returning the handle.
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except Exception:
            connection.close()
            raise
        return connection

    try:
        return opened(f"file:{path}?mode=ro")
    except sqlite3.OperationalError:
        wal = path.with_name(f"{path.name}-wal")
        try:
            wal_pending = wal.is_file() and wal.stat().st_size > 0
        except OSError:
            wal_pending = True
        if wal_pending:
            raise
        return opened(f"file:{path}?mode=ro&immutable=1")


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }


def _performance(path: Path) -> dict:
    if not path.is_file():
        return {"available": False, "reason": "journal not created"}
    try:
        import report
        with closing(_ro_connect(path)) as connection:
            return {"available": True, **report.json_report(connection)}
    except Exception as exc:                               # noqa: BLE001
        return {"available": False, "reason": type(exc).__name__}


def _charts(path: Path, mode: str = "paper") -> dict:
    """Return small chart-ready series from one runtime journal.

    The dashboard never reconstructs equity or costs. Account equity comes from
    the account equity samples, payoff values come from completed parent closes
    with known net P&L, and uncertainty uses the existing session-cluster
    moving-block bootstrap only when positive risk denominators are present.
    """
    source = str(mode or "paper").strip().lower()
    empty = {
        "available": False, "source": source,
        "net_equity": {"available": False, "points": []},
        "drawdown": {"available": False, "points": []},
        "payoff": {"available": False, "values": [], "sample_count": 0},
        "uncertainty": {"available": False, "sample_count": 0,
                        "session_count": 0, "mean_net_r": None,
                        "lower_net_r": None, "upper_net_r": None},
    }
    if not path.is_file():
        empty["reason"] = "journal not created"
        return empty
    try:
        from datetime import datetime, timezone
        import report
        with closing(_ro_connect(path)) as connection:
            tables = _tables(connection)
            if "equity" not in tables:
                equity_rows = []
            else:
                equity_columns = {str(row[1]) for row in
                                  connection.execute("PRAGMA table_info(equity)")}
                equity_mode = ("runtime_mode" if "runtime_mode" in equity_columns
                               else None)
                selected_mode = f", {equity_mode}" if equity_mode else ""
                equity_rows = connection.execute(
                    f"SELECT ts,equity{selected_mode} FROM equity ORDER BY ts,rowid"
                ).fetchall()
            events = _trades(connection, limit=None) if "trades" in tables else []
        points = []
        for row in equity_rows:
            if equity_mode and row["runtime_mode"] not in (None, "", source):
                continue
            try:
                value = float(row["equity"])
                timestamp = float(row["ts"])
            except (TypeError, ValueError, OverflowError):
                continue
            if not (value == value and abs(value) != float("inf") and
                    timestamp == timestamp and abs(timestamp) != float("inf")):
                continue
            points.append({"ts": timestamp, "value": value})
        drawdown = []
        high_water = None
        for point in points:
            high_water = point["value"] if high_water is None else max(
                high_water, point["value"])
            drawdown.append({"ts": point["ts"],
                             "value": point["value"] - high_water})
        events = [event for event in events if str(event.get("runtime_mode") or source) == source]
        parents = sorted(report.closed_parent_trades(events),
                         key=lambda parent: float(parent.get("last_ts") or 0))
        net_values = [float(parent["net"]) for parent in parents
                      if parent.get("net") is not None]
        r_values, clusters = [], []
        from zoneinfo import ZoneInfo
        for parent in parents:
            if parent.get("r_multiple") is None or parent.get("last_ts") is None:
                continue
            try:
                session = datetime.fromtimestamp(float(parent["last_ts"]), ZoneInfo("America/New_York")).date().isoformat()
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            r_values.append(float(parent["r_multiple"]))
            clusters.append(session)
        payoff = {
            "available": bool(net_values), "values": net_values,
            "sample_count": len(net_values),
            "wins": sum(value > 0 for value in net_values),
            "losses": sum(value < 0 for value in net_values),
            "zeroes": sum(value == 0 for value in net_values),
            "basis": "net_pnl" if net_values else None,
        }
        uncertainty = {**empty["uncertainty"],
                       "sample_count": len(r_values),
                       "session_count": len(set(clusters)),
                       "mean_net_r": (sum(r_values) / len(r_values)
                                      if r_values else None)}
        if len(r_values) >= 2 and len(set(clusters)) >= 2:
            try:
                from research.stats import moving_block_cluster_bootstrap_lower_bound
                cluster_count = len(set(clusters))
                bound = moving_block_cluster_bootstrap_lower_bound(
                    r_values, clusters, confidence=.95, draws=1000,
                    block_length=min(5, cluster_count - 1), min_clusters=2)
                uncertainty.update({
                    "available": bool(bound.get("available")),
                    "lower_net_r": bound.get("lower_bound"),
                    "upper_net_r": bound.get("upper_bound"),
                    "method": bound.get("method"),
                    "confidence": bound.get("confidence"),
                    "clusters": bound.get("clusters"),
                })
            except (TypeError, ValueError, OverflowError):
                pass
        return {
            "available": bool(points or net_values), "source": source,
            "net_equity": {"available": bool(points), "points": points,
                           "basis": "observed_account_equity", "cash_flows_adjusted": False},
            "drawdown": {"available": bool(drawdown), "points": drawdown},
            "payoff": payoff, "uncertainty": uncertainty,
        }
    except (OSError, sqlite3.Error, ValueError, KeyError):
        return {**empty, "reason": "journal unreadable"}


from deploy.dashboard_workbench import workbench


# Mirrors research.edge_ledger.PAPER_DEMOTION_* for compatibility. The dashboard
# deliberately does not import the research package, so the advisory rolling
# thresholds it displays are restated here and pinned to the ledger constants by
# test_deploy. A breach is an alert; it is not a lifecycle transition.
PAPER_ROLLING_WINDOW = 20
PAPER_ROLLING_FLOOR = -2.0


def _live_paper(connection: sqlite3.Connection) -> list[dict]:
    """Per-edge live paper results, strongest realized R first.

    Proof confidence says how strong the evidence *was*; this says how the
    deployed edge is *doing*.  Both are needed to answer "which of my edges is
    working", and only the first was visible before.
    """
    rows = connection.execute(
        """SELECT p.candidate_id, c.variant_id, c.strategy_id, c.vehicle,
                  s.status, p.session_date, p.net_pnl, p.outcome_json
           FROM paper_outcomes p
             JOIN candidates c ON c.candidate_id=p.candidate_id
             JOIN candidate_state s ON s.candidate_id=p.candidate_id
           ORDER BY p.candidate_id, p.created_at, p.outcome_id""").fetchall()
    grouped: dict[str, dict] = {}
    for row in rows:
        item = grouped.setdefault(str(row["candidate_id"]), {
            "candidate_id": str(row["candidate_id"]),
            "variant_id": row["variant_id"], "strategy_id": row["strategy_id"],
            "vehicle": row["vehicle"], "status": row["status"],
            "outcomes": 0, "net_pnl": 0.0, "_net_known": True,
            "_r": [], "_sessions": set()})
        item["outcomes"] += 1
        try:
            value = float(row["net_pnl"])
            if not (value == value and abs(value) != float("inf")):
                raise ValueError
            item["net_pnl"] += value
        except (TypeError, ValueError, OverflowError):
            item["_net_known"] = False
        if row["session_date"]:
            item["_sessions"].add(str(row["session_date"]))
        try:
            payload = json.loads(row["outcome_json"])
            value = float(payload["r_multiple"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if value == value and value not in (float("inf"), float("-inf")):
            item["_r"].append(value)
    report = []
    for item in grouped.values():
        r_values = item.pop("_r")
        sessions = item.pop("_sessions")
        net_known = bool(item.pop("_net_known"))
        recent = r_values[-PAPER_ROLLING_WINDOW:]
        wins = [value for value in r_values if value > 0]
        report.append({
            **item,
            "sessions": len(sessions),
            "last_session": max(sessions) if sessions else None,
            "net_pnl": round(item["net_pnl"], 2) if net_known else None,
            "net_pnl_available": net_known,
            "total_r": round(sum(r_values), 4) if r_values else None,
            "mean_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
            "win_rate": round(len(wins) / len(r_values), 4) if r_values else None,
            "rolling_r": round(sum(recent), 4) if recent else None,
            "rolling_floor": PAPER_ROLLING_FLOOR,
            "rolling_authoritative": False,
            "rolling_action": "warning_only",
            "guard": ("breached" if len(recent) >= PAPER_ROLLING_WINDOW and
                      sum(recent) <= PAPER_ROLLING_FLOOR else
                      "armed" if len(recent) >= PAPER_ROLLING_WINDOW else
                      f"{len(recent)}/{PAPER_ROLLING_WINDOW}"),
        })
    return sorted(report, key=lambda item: (
        item["total_r"] is not None,
        item["total_r"] if item["total_r"] is not None else 0.0), reverse=True)


def _tradeable_vehicle(config: dict) -> str:
    """The vehicle this deployment's execution profile can trade.

    Mirrors ``agent.edge.runtime_vehicle`` without importing the runtime edge
    resolver into a read-only view; ``test_deploy`` pins the two together.
    """
    strategy = config.get("strategy") if isinstance(config, dict) else {}
    mode = str((strategy or {}).get("execution_mode", "")).strip().lower()
    return "option" if mode in {"options", "option"} else "equity"


def _edge_status(path: Path) -> dict:
    """Expose the append-only edge-lab lifecycle without promoting anything.

    The dashboard is intentionally read-only and does not import the edge
    runner.  Reading the small SQLite ledger directly also keeps the view
    usable in a recovery image where optional research dependencies are not
    installed.
    """
    if not path.is_file():
        return {"available": False, "status": "not_initialized",
                "candidates": 0, "by_status": {}, "by_vehicle": {},
                "proved_edges": [], "live_paper": []}
    try:
        factory = {"hypotheses": 0, "accounts": 0, "cycles": 0}
        live_paper: list[dict] = []
        with closing(_ro_connect(path)) as connection:
            tables = _tables(connection)
            if not {"candidates", "candidate_state"}.issubset(tables):
                return {"available": False, "status": "invalid_ledger",
                        "candidates": 0, "by_status": {}, "by_vehicle": {},
                        "proved_edges": [], "live_paper": []}
            rows = connection.execute(
                """SELECT c.vehicle, s.status, COUNT(*) AS count
                   FROM candidates c JOIN candidate_state s
                     ON s.candidate_id=c.candidate_id
                   GROUP BY c.vehicle, s.status
                   ORDER BY c.vehicle, s.status""").fetchall()
            proved_candidates = connection.execute(
                """SELECT c.candidate_id, c.variant_id, c.strategy_id,
                          c.vehicle, s.status
                   FROM candidates c JOIN candidate_state s
                     ON s.candidate_id=c.candidate_id
                   WHERE s.status IN ('validated','champion')
                   ORDER BY CASE s.status WHEN 'champion' THEN 0 ELSE 1 END,
                            c.vehicle, c.strategy_id, c.variant_id
                   LIMIT 100""").fetchall()
            proved = []
            if {"runs", "evidence"}.issubset(tables):
                for candidate in proved_candidates:
                    run = connection.execute(
                        """SELECT run_id, lane FROM runs
                           WHERE candidate_id=?
                           ORDER BY created_at DESC, run_id DESC LIMIT 1""",
                        (candidate["candidate_id"],)).fetchone()
                    if run is None or run["lane"] != "shadow":
                        continue
                    evidence = connection.execute(
                        """SELECT payload_json, evidence_hash FROM evidence
                           WHERE candidate_id=? AND run_id=?
                             AND kind='verified_gate'
                           ORDER BY created_at DESC, evidence_id DESC LIMIT 1""",
                        (candidate["candidate_id"], run["run_id"])).fetchone()
                    if evidence is None:
                        continue
                    try:
                        payload = json.loads(evidence["payload_json"])
                        gate = payload["gate"]
                        valid = bool(
                            evidence["evidence_hash"] == _content_hash(payload) and
                            payload.get("gate_hash") == gate.get("content_hash") and
                            gate.get("passes") is True and
                            verify_gate_envelope(gate))
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        valid = False
                    if not valid:
                        continue
                    statistics = gate.get("statistics") or {}
                    try:
                        confidence = 1.0 - float(statistics.get("q_value", 1.0))
                    except (TypeError, ValueError):
                        confidence = 0.0
                    proved.append({**dict(candidate), "run_id": run["run_id"],
                                   "gate_hash": gate["content_hash"],
                                   "confidence": round(confidence, 6)})
            if "paper_outcomes" in tables:
                live_paper = _live_paper(connection)
            if {"factory_hypotheses", "factory_accounts", "factory_cycles"}.issubset(tables):
                factory = {
                    "hypotheses": int(connection.execute(
                        "SELECT COUNT(*) FROM factory_hypotheses").fetchone()[0]),
                    "accounts": int(connection.execute(
                        "SELECT COUNT(*) FROM factory_accounts").fetchone()[0]),
                    "cycles": int(connection.execute(
                        "SELECT COUNT(*) FROM factory_cycles").fetchone()[0]),
                }
        by_status: dict[str, int] = {}
        by_vehicle: dict[str, int] = {}
        for row in rows:
            status = str(row["status"])
            vehicle = str(row["vehicle"])
            count = int(row["count"])
            by_status[status] = by_status.get(status, 0) + count
            by_vehicle[vehicle] = by_vehicle.get(vehicle, 0) + count
        return {"available": True, "status": "ready",
                "candidates": sum(by_status.values()),
                "by_status": by_status, "by_vehicle": by_vehicle,
                "proved_edges": [dict(row) for row in proved],
                "live_paper": live_paper,
                "factory": factory}
    except (OSError, sqlite3.Error, ValueError):
        return {"available": False, "status": "unreadable",
                "candidates": 0, "by_status": {}, "by_vehicle": {},
                "proved_edges": [], "live_paper": []}


def _trades(connection: sqlite3.Connection, limit: int | None = 200,
            offset: int = 0) -> list[dict]:
    """Every recorded fill, and which edge decided to place it.

    The journal already stamped ``strategy_id``/``variant_id`` on every row;
    nothing read them back.  Without that join a trade list answers "what
    happened" but not "which of my edges did this", which is the question that
    decides whether an edge is worth promoting.
    """
    columns = {str(row[1]) for row in
               connection.execute("PRAGMA table_info(trades)").fetchall()}
    # The dashboard can be mounted against a read-only legacy runtime before
    # the next trader startup performs SQLite migrations.  Preserve the
    # stable response shape by selecting NULL for telemetry columns absent in
    # that deployment-era schema.
    def field(name: str) -> str:
        return name if name in columns else f"NULL AS {name}"

    order_id = "id" if "id" in columns else "rowid"
    limit_clause = "" if limit is None else " LIMIT ? OFFSET ?"
    params = () if limit is None else (max(1, int(limit)), max(0, int(offset)))
    rows = connection.execute(
        f"""SELECT {field('ts')}, {field('symbol')}, {field('side')},
                  {field('action')}, {field('qty')}, {field('price')},
                  {field('notional')},
                  {field('id')}, {field('order_id')}, {field('parent_trade_id')},
                  {field('setup_id')}, {field('setup_key')}, {field('trade_id')},
                  {field('requested_qty')}, {field('planned_qty')},
                  {field('cumulative_filled_qty')}, {field('fill_fraction')},
                  {field('filled_fraction')}, {field('position_closed')},
                  {field('realized_pnl_usd')}, {field('gross_pnl')},
                  {field('fees')}, {field('fee_usd')}, {field('funding_usd')},
                  {field('slippage')}, {field('slippage_usd')},
                  {field('net_pnl')}, {field('pnl_semantics')}, {field('pnl_provenance')},
                  {field('cost_provenance')}, {field('risk_usd')},
                  {field('intended_risk_usd')}, {field('delivered_risk_usd')},
                  {field('risk_delivery_ratio')}, {field('risk_shortfall_usd')},
                  {field('configured_risk_budget_usd')},
                  {field('planned_risk_usd')},
                  {field('planned_to_configured_risk_ratio')},
                  {field('delivered_to_configured_risk_ratio')},
                  {field('pnl_pct')}, {field('fill_status')}, {field('setup_type')},
                  {field('strategy_id')}, {field('strategy_version')},
                  {field('variant_id')}, {field('runtime_mode')},
                  {field('exit_policy')}, {field('close_trigger')}
           FROM trades ORDER BY ts DESC, {order_id} DESC{limit_clause}""",
        params).fetchall()
    trades = []
    for row in rows:
        item = dict(row)
        risk = item.get("risk_usd")
        realized = item.get("realized_pnl_usd")
        net = item.get("net_pnl")
        if net is None:
            # Keep this row visibly gross/unknown rather than presenting a
            # legacy fill-only value as net R.
            item["r_multiple"] = None
        try:
            item["net_r_multiple"] = (round(float(net) / float(risk), 4)
                                      if risk and net is not None else None)
        except (TypeError, ValueError, ZeroDivisionError):
            item["net_r_multiple"] = None
        try:
            item["gross_r_multiple"] = (round(float(realized) / float(risk), 4)
                                        if risk and realized is not None else None)
        except (TypeError, ValueError, ZeroDivisionError):
            item["gross_r_multiple"] = None
        if item.get("net_pnl") is not None:
            item["r_multiple"] = item["net_r_multiple"]
        else:
            item["r_multiple"] = None
        item["when"] = item.pop("ts", None)
        trades.append(item)
    return trades


def _by_variant(trades: Sequence[dict]) -> list[dict]:
    """Roll lifetime closed parent trades up per deployed variant.

    This is the runtime's view of an edge, independent of the research
    ledger's: it counts what the broker actually did.  When the two disagree,
    that disagreement is the finding.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    close_actions = {"close", "partial_close", "exit", "sell_to_close"}
    import report as reporting
    all_rows = [dict(trade) for trade in trades]
    for index, trade in enumerate(all_rows):
        action = str(trade.get("action") or "").lower()
        if action not in close_actions and action not in {"open", "buy_to_open", "sell_to_open"}:
            continue
        key = (str(trade.get("strategy_id") or "unknown"),
               str(trade.get("variant_id") or "unattributed"),
               reporting._parent_key(trade, index))
        grouped.setdefault(key, []).append(trade)
    by_variant: dict[tuple[str, str], dict] = {}
    for (strategy_id, variant_id, _parent), rows in grouped.items():
        parents = reporting.closed_parent_trades(rows)
        if not parents:
            continue
        parent = parents[0]
        key = (strategy_id, variant_id)
        item = by_variant.setdefault(key, {
            "strategy_id": strategy_id, "variant_id": variant_id,
            "trades": 0, "close_fills": 0, "symbols": set(),
            "gross_pnl_usd": 0.0, "fees_usd": 0.0, "net_pnl_usd": 0.0,
            "realized_pnl_usd": 0.0, "_gross_known": True, "_fees_known": True,
            "_net_known": True, "_r": [], "_r_bases": set(), "_gross_r": [],
            "_net_outcomes": [], "_gross_outcomes": [],
            "last_trade_ts": None, "_cost_provenance": set()})
        item["trades"] += 1
        item["close_fills"] += int(parent.get("close_fills") or 0)
        for row in rows:
            if row.get("symbol"):
                item["symbols"].add(str(row["symbol"]))
        for field, flag in (("gross", "_gross_known"), ("fees", "_fees_known"),
                            ("net", "_net_known")):
            value = parent.get(field)
            if value is None:
                item[flag] = False
            else:
                destination = {"gross": "gross_pnl_usd", "fees": "fees_usd",
                               "net": "net_pnl_usd"}[field]
                item[destination] += value
        if parent.get("net") is not None:
            item["_net_outcomes"].append(float(parent["net"]))
        if parent.get("gross") is not None:
            item["_gross_outcomes"].append(float(parent["gross"]))
        if parent.get("r_multiple") is not None:
            item["_r"].append(float(parent["r_multiple"]))
            item["_r_bases"].add("net_pnl")
        elif parent.get("gross_r_multiple") is not None:
            # Compatibility telemetry for legacy rows.  The explicit basis
            # below keeps this gross R from being mistaken for net R.
            item["_r"].append(float(parent["gross_r_multiple"]))
            item["_r_bases"].add("gross_legacy_unknown_cost")
        if parent.get("gross") is not None and parent.get("risk"):
            item["_gross_r"].append(float(parent["gross_r_multiple"]))
        item["_cost_provenance"].update(parent.get("cost_provenance") or ())
        when = parent.get("last_ts")
        if when is not None and (item["last_trade_ts"] is None or
                                 when > item["last_trade_ts"]):
            item["last_trade_ts"] = when
    output = []
    for item in by_variant.values():
        values = item.pop("_r")
        r_bases = item.pop("_r_bases")
        gross_values = item.pop("_gross_r")
        net_known = bool(item.pop("_net_known"))
        gross_known = bool(item.pop("_gross_known"))
        fees_known = bool(item.pop("_fees_known"))
        net = item["net_pnl_usd"] if net_known else None
        gross = item["gross_pnl_usd"] if gross_known else None
        fees = item["fees_usd"] if fees_known else None
        if not r_bases:
            r_basis = "unavailable"
        elif len(r_bases) == 1:
            r_basis = next(iter(r_bases))
        else:
            r_basis = "mixed_net_and_gross_unknown_cost"
        net_outcomes = item.pop("_net_outcomes")
        gross_outcomes = item.pop("_gross_outcomes")
        wins = [value for value in net_outcomes if value > 0]
        gross_wins = [value for value in gross_outcomes if value > 0]
        output.append({
            **item,
            "symbols": ", ".join(sorted(item["symbols"])),
            "gross_pnl_usd": round(gross, 2) if gross is not None else None,
            "fees_usd": round(fees, 2) if fees is not None else None,
            "net_pnl_usd": round(net, 2) if net is not None else None,
            "realized_pnl_usd": (round(net, 2) if net is not None else
                                 round(gross, 2) if gross is not None else None),
            "realized_pnl_basis": ("net_pnl" if net is not None else
                                    "gross_legacy_unknown_cost"),
            "r_basis": r_basis,
            "cost_provenance": sorted(item.pop("_cost_provenance")),
            "total_r": round(sum(values), 4) if len(values) == item["trades"] and len(r_bases) == 1 else None,
            "mean_r": round(sum(values) / len(values), 4) if values and len(values) == item["trades"] and len(r_bases) == 1 else None,
            "win_rate": (round(len(wins) / len(net_outcomes), 4)
                         if net_known and net_outcomes else None),
            "win_rate_basis": "net_pnl" if net_known else "unavailable",
            "gross_win_rate": (round(len(gross_wins) / len(gross_outcomes), 4)
                               if gross_known and gross_outcomes else None),
        })
    return sorted(output, key=lambda item: item["trades"], reverse=True)


def _journal_view(path: Path, *, page: int = 1, page_size: int = 200) -> dict:
    """Per-trade attribution and its per-variant roll-up, read-only."""
    if not path.is_file():
        return {"available": False, "trades": [], "by_variant": []}
    try:
        with closing(_ro_connect(path)) as connection:
            if "trades" not in _tables(connection):
                return {"available": False, "trades": [], "by_variant": []}
            page = max(1, int(page))
            page_size = max(1, min(1000, int(page_size)))
            offset = (page - 1) * page_size
            trades = _trades(connection, limit=page_size, offset=offset)
            all_fills = _trades(connection, limit=None)
            all_closes = all_fills
            close_count = sum(
                str(row.get("action") or "").lower() in
                {"close", "partial_close", "exit", "sell_to_close"}
                for row in all_closes)
    except (OSError, sqlite3.Error, ValueError):
        return {"available": False, "trades": [], "by_variant": []}
    by_variant = _by_variant(all_fills)
    return {"available": True, "trades": trades,
            "recent_trades": trades, "by_variant": by_variant,
            "lifetime": {"close_fills": close_count,
                         "closed_trades": sum(item.get("trades", 0)
                                               for item in by_variant)},
            "page": page, "page_size": page_size,
            "total_fills": len(all_fills),
            "has_more": offset + len(trades) < len(all_fills)}


def _learning(path: Path, limit: int = 60) -> dict:
    """The graded reason history, and the chain each proposal built on."""
    empty = {"available": False, "lessons": [], "summary": {}}
    if not path.is_file():
        return empty
    try:
        with closing(_ro_connect(path)) as connection:
            tables = _tables(connection)
            if not {"factory_lessons", "factory_lesson_outcomes"}.issubset(tables):
                return empty
            columns = {str(row["name"]) for row in
                       connection.execute("PRAGMA table_info(factory_lessons)")}
            outcome_columns = {str(row["name"]) for row in
                               connection.execute(
                                   "PRAGMA table_info(factory_lesson_outcomes)")}
            parent = ("l.parent_lesson_id" if "parent_lesson_id" in columns
                      else "NULL AS parent_lesson_id")
            classification = ("o.classification" if
                              "classification" in outcome_columns else
                              "CASE WHEN o.passed=1 THEN 'proved' "
                              "WHEN o.underpowered=1 THEN 'underpowered' "
                              "ELSE 'legacy_unclassified' END AS classification")
            rows = connection.execute(
                f"""SELECT l.lesson_id, {parent}, l.vehicle, l.family, l.kind,
                           l.source, l.reason, l.variant_id, l.changed_json,
                           l.created_at, o.passed, o.underpowered, {classification},
                           o.heldout_delta, o.q_value, o.outcome_id
                    FROM factory_lessons l
                    LEFT JOIN factory_lesson_outcomes o
                      ON o.lesson_id=l.lesson_id
                    ORDER BY l.created_at DESC, l.lesson_id DESC LIMIT ?""",
                (max(1, int(limit)),)).fetchall()
            reasons = {str(row[0]): str(row[1]) for row in connection.execute(
                "SELECT lesson_id, reason FROM factory_lessons")}
    except (OSError, sqlite3.Error, ValueError):
        return empty
    lessons = []
    for row in rows:
        item = dict(row)
        graded = item.pop("outcome_id") is not None
        changed = json.loads(item.pop("changed_json") or "{}")
        lessons.append({
            "lesson_id": item["lesson_id"], "vehicle": item["vehicle"],
            "family": item["family"], "kind": item["kind"],
            "proposed_by": item["source"], "reason": item["reason"],
            "variant_id": item["variant_id"],
            "changed": "; ".join(
                f"{key} {value.get('from')}→{value.get('to')}"
                if isinstance(value, dict) and "from" in value else f"{key}={value}"
                for key, value in sorted(changed.items())),
            "verdict": None if not graded else item["classification"],
            "heldout_delta": item["heldout_delta"],
            "built_on": reasons.get(str(item["parent_lesson_id"] or "")),
            "when": item["created_at"],
        })
    graded_rows = [item for item in lessons if item["verdict"]]
    return {
        "available": True, "lessons": lessons,
        "summary": {
            "recorded": len(lessons),
            "graded": len(graded_rows),
            "built_on_a_prior_lesson": sum(
                1 for item in lessons if item["built_on"]),
            "from_live_trials": sum(
                1 for item in lessons if item["kind"] == "trial"),
            "llm_authored": sum(
                1 for item in lessons if item["proposed_by"] == "llm"),
        },
    }


def _trial_view(config: dict, edge_path: Path) -> dict:
    """Paper-account trials: what is running and what has earned a pin.

    The promotable list is the hand-off: it names the variant and its edge,
    shows what it actually returned on the book, and carries the exact config
    block to paste. Nothing here promotes anything.
    """
    empty = {"available": False, "policy": {}, "reviews": [], "promotable": []}
    if not edge_path.is_file():
        return empty
    try:
        from agent.governance import pinned_variant_ids
        from research.trial import promotable_report, review_trials

        pinned = sorted(pinned_variant_ids(config))
        review = review_trials(edge_path, config=config, pinned=pinned,
                               apply=False)
        promotable = promotable_report(edge_path, config=config, pinned=pinned)
    except Exception:                                      # noqa: BLE001
        # A recovery image without the research package still gets a
        # dashboard; it simply does not get this panel.
        return empty
    return {"available": True, "policy": review.get("policy") or {},
            "reviews": [{
                "variant_id": item["variant_id"], "vehicle": item["vehicle"],
                "family": item["family"], "status": item["status"],
                "pinned": item["pinned"], "action": item.get("action"),
                "state": item["verdict"]["state"],
                "sessions": item["verdict"].get("sessions"),
                "trades": item["verdict"].get("trades"),
                "total_r": item["verdict"].get("total_r"),
                "mean_r": item["verdict"].get("mean_r"),
                "mean_r_lcb": (item["verdict"].get("session_cluster_confidence") or {}).get("lower_bound"),
                "session_cluster_confidence": item["verdict"].get(
                    "session_cluster_confidence"),
            } for item in review.get("reviews") or []],
            "promotable": promotable}


def _promotions(config: dict, edge_path: Path) -> dict:
    """What the operator pinned, and whether each pin can actually trade."""
    strategy = config.get("strategy") if isinstance(config, dict) else {}
    entries = (strategy or {}).get("pinned") or []
    mode = str((strategy or {}).get("selection_mode") or "specific")
    unresolved: list[dict] = []
    if entries:
        try:
            from agent.edge import unresolved_promotions

            unresolved = unresolved_promotions(config, db_path=edge_path)
        except Exception:                                  # noqa: BLE001
            # The dashboard is a view. A research package it cannot import
            # must cost it this panel, never the whole page.
            unresolved = []
    return {"selection_mode": mode, "pinned": [dict(item) for item in entries],
            "unresolved": unresolved,
            "frozen": bool(entries),
            "note": ("pins prevent automatic substitution; authoritative drift "
                     "or trial failures can still pause or demote a pinned edge. "
                     "Rolling-R warnings are advisory.")}


def _config_audit(journal: Path) -> dict:
    """The configuration versions this runtime has operated under."""
    if not journal.is_file():
        return {"available": False, "versions": []}
    try:
        with closing(_ro_connect(journal)) as connection:
            if "config_versions" not in _tables(connection):
                return {"available": False, "versions": []}
            rows = connection.execute(
                """SELECT config_version_id, previous_version_id, mode, source,
                          actor, diff_json, created_at
                   FROM config_versions
                   ORDER BY created_at DESC, config_version_id DESC LIMIT 25"""
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            diff = json.loads(item.pop("diff_json")) or []
            if not isinstance(diff, list):
                raise ValueError("config audit diff must be a list")
            item["diff"] = diff
            item["changed_paths"] = [
                str(entry["path"]) for entry in diff
                if isinstance(entry, dict) and "path" in entry
            ]
            history.append(item)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return {"available": False, "versions": []}
    return {"available": bool(history),
            "current": history[0]["config_version_id"] if history else None,
            "versions": [{
                "config_version_id": item["config_version_id"],
                "previous_version_id": item["previous_version_id"],
                "mode": item["mode"], "source": item["source"],
                "actor": item["actor"], "when": item["created_at"],
                "changes": len(item["diff"]),
                "changed_paths": ", ".join(item["changed_paths"][:8]),
            } for item in history]}


def _reports(root: Path) -> list[dict]:
    candidates = set((root / "research" / "results").glob("**/*.md"))
    rows = []
    for path in candidates:
        try:
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
        except OSError:
            continue
        rows.append({"path": relative, "updated_ts": stat.st_mtime,
                     "size_bytes": stat.st_size})
    return sorted(rows, key=lambda row: row["updated_ts"], reverse=True)[:100]


def _safe_heartbeat(path: Path) -> dict:
    raw = _json_file(path)
    allowed = {
        "schema", "status", "updated_ts", "pid", "runtime_mode", "run_id",
        "strategy_id", "strategy_version", "research_expected",
        "research_available", "research_status", "research_failure_count",
        "research_consecutive_failures", "research_last_failure",
        "research_last_success_ts",
        "trading_state", "last_cycle_ts",
        "last_cycle_error", "stop_reason", "next_run_ts", "last_run_date",
        "last_exit_code", "started_ts", "completed_ts", "job_id",
        "run_date", "timeout_seconds", "deadline_ts",
        "structured_failures", "stdout_chars", "stderr_chars",
        "stdout_truncated", "stderr_truncated", "cycle_status",
        "research_cycle", "research_preflight",
        "provenance", "evidence_available",
    }
    result = {key: value for key, value in raw.items() if key in allowed}
    # Progress is the one nested heartbeat object intentionally exposed.  It
    # still goes through the same closed-schema parser as scheduler output so
    # arbitrary child JSON cannot become dashboard data.
    progress = structured_research_progress(raw.get("research_progress"))
    if progress is not None:
        result["research_progress"] = progress
    readiness = structured_research_readiness(raw.get("research_readiness"))
    if readiness is not None:
        result["research_readiness"] = derive_research_readiness(
            progress, readiness, now=time.time(),
            deadline_ts=raw.get("deadline_ts"))
    preflight = structured_research_preflight(result.get("research_preflight"))
    if preflight is None and isinstance(result.get("research_cycle"), dict):
        preflight = structured_research_preflight(
            result["research_cycle"].get("preflight"))
    if preflight is not None:
        result["research_preflight"] = preflight
    paper_selection = health.paper_selection_summary(raw.get("paper_selection"))
    if paper_selection is not None:
        result["paper_selection"] = paper_selection
    return result


def _direct_research_status(path: Path, *, now: float | None = None,
                            max_age: float = 180.0) -> dict:
    """Project the direct research lease using its versioned status contract."""
    empty = {
        "available": False, "schema": "research-direct-status.v1",
        "status": "missing", "execution_mode": "direct",
        "scheduler_managed": None, "status_scope": "cycle_process", "fresh": False, "running": False,
        "job_id": None, "pid": None, "started_ts": None,
        "updated_ts": None, "lease_ts": None, "progress": None,
        "terminal": None, "dataset": None,
    }
    raw = _json_file(path)
    if raw.get("schema") != "research-direct-status.v1":
        return {**empty, "reason": "invalid_schema" if raw else "missing"}
    current = time.time() if now is None else float(now)
    updated = raw.get("updated_ts")
    lease = raw.get("lease_ts", updated)
    try:
        age = current - float(lease)
        fresh = -5.0 <= age <= float(max_age)
    except (TypeError, ValueError, OverflowError):
        fresh = False
    progress = raw.get("progress")
    bounded_progress = None
    if isinstance(progress, dict):
        bounded_progress = {
            key: progress.get(key) for key in (
                "schema", "phase", "unit", "vehicle", "done", "total",
                "updated_ts") if key in progress
        }
    terminal = raw.get("terminal")
    bounded_terminal = (dict(terminal) if isinstance(terminal, dict) else None)
    dataset = raw.get("dataset")
    bounded_dataset = (dict(dataset) if isinstance(dataset, dict) else None)
    status = str(raw.get("status") or "unknown")
    return {
        "available": True,
        "schema": "research-direct-status.v1",
        "status": status,
        "execution_mode": "direct",
        "scheduler_managed": raw.get("scheduler_managed") if isinstance(raw.get("scheduler_managed"), bool) else None,
        "status_scope": raw.get("status_scope", "cycle_process"),
        "process_start_ts": raw.get("process_start_ts"),
        "build_identity": raw.get("build_identity"),
        "fresh": fresh,
        "running": status == "running" and fresh,
        "job_id": raw.get("job_id"), "pid": raw.get("pid"),
        "started_ts": raw.get("started_ts"),
        "updated_ts": updated, "lease_ts": lease,
        "progress": bounded_progress, "terminal": bounded_terminal,
        "dataset": bounded_dataset,
        "provenance": raw.get("provenance") if isinstance(
            raw.get("provenance"), dict) else None,
    }


def snapshot(root: Path) -> dict:
    config_path = root / "config.yaml"
    config = load_config(config_path)
    mode = str(config.get("mode") or "paper").lower()
    runtime = root / "runtime"
    journal = runtime / mode / "journal.db"
    recorder_path = _recorder_corpus_path(root)
    trader_heartbeat = runtime / mode / "heartbeat.json"
    shadow_heartbeat = root / "shadow" / "health.json"
    research_heartbeat = runtime / "health" / "research.json"
    direct_research_status = runtime / "health" / "research-direct.json"
    edge_configured = Path(os.getenv("ALPACA_EDGE_DB", "runtime/research/edge_lab.sqlite3"))
    edge_path = edge_configured if edge_configured.is_absolute() else root / edge_configured
    cycle_seconds = float(config.get("cycle", {}).get("interval_seconds") or 60)
    trader_max_age = max(90.0, cycle_seconds * 4)
    edge = _cached(f"edge:{edge_path}", 30, lambda: _edge_status(edge_path))
    trial = _cached(f"trial:{edge_path}", 60,
                    lambda: _trial_view(config, edge_path))
    direct = _cached(
        f"research-direct:{direct_research_status}", 15,
        lambda: _direct_research_status(direct_research_status))
    tradeable = _tradeable_vehicle(config)
    trader_state = _safe_state(runtime / mode / "state.json")
    untradeable = sum(1 for row in edge.get("proved_edges") or ()
                      if str(row.get("vehicle")) != tradeable)
    return {
        "schema": 1,
        "provenance": deployment_provenance(),
        "generated_ts": time.time(),
        "mode": mode,
        "strategy": {
            key: config.get("strategy", {}).get(key)
            for key in (
                "id", "version", "execution_mode", "variant_id",
                "selection_mode",
            )
        },
        "cycle": {
            key: config.get("cycle", {}).get(key)
            for key in ("interval_seconds",)
        },
        "trader": {
            "health": health.trader(trader_heartbeat, trader_max_age),
            "heartbeat": _safe_heartbeat(trader_heartbeat),
            "state": trader_state,
            "exposure": _portfolio_exposure(trader_state.get("active_trades", [])),
        },
        "recorder": health.recorder(
            recorder_path, 900,
            configured_symbols=((config.get("universe") or {}).get("symbols")
                                or []),
            configured_data_feed=((config.get("broker") or {}).get("data_feed")
                                  or "iex"),
            configured_options_feed=((config.get("broker") or {}).get(
                "options_feed") or "indicative")),
        # Broker-free diagnostic liveness is separate from complete/fresh
        # cohort coverage. This is a bounded projection, never raw shadow data.
        "shadow": health.shadow(shadow_heartbeat, 180),
        "research_service": {
            "health": (
                health.research(research_heartbeat, 180)
                if research_heartbeat.exists() else {
                    "ok": True, "component": "research", "status": "disabled",
                    "optional": True, "fresh": False, "hung": False,
                    "structured_failures": [],
                }),
            "heartbeat": _safe_heartbeat(research_heartbeat),
            # Direct invocations do not write the scheduler heartbeat. Keep
            # their versioned lease visible beside it, with its own freshness
            # and terminal state so an old file cannot look active.
            "direct": direct,
            "direct_status": direct,
        },
        "performance": _cached(
            f"performance:{journal}", 30, lambda: _performance(journal)),
        "charts": _cached(
            f"charts:{journal}:{mode}", 30, lambda: _charts(journal, mode)),
        # What the broker actually did, attributed to the edge that decided it.
        "journal": _cached(
            f"journal:{journal}", 30, lambda: _journal_view(journal)),
        # Why research tried what it tried, and what the gates said about it.
        "learning": _cached(
            f"learning:{edge_path}", 30, lambda: _learning(edge_path)),
        # Operator-declared promotions: what is pinned, and what is pinned but
        # cannot currently trade, which is the failure worth surfacing.
        "promotions": _cached(
            f"promotions:{config_path}:{edge_path}", 30,
            lambda: _promotions(config, edge_path)),
        # Configuration changes, each with the version id that identifies it.
        "config_audit": _cached(
            f"config_audit:{journal}", 30, lambda: _config_audit(journal)),
        "edge": edge,
        "trial": trial,
        "research": {
            "available": edge_path.is_file(),
            "service_optional": True,
            "entry_gate_required": bool(
                config.get("research", {}).get("enabled", True) and
                config.get("research", {}).get("require_validated_variant", True)),
            "paper_trial_enabled": config.get("research", {}).get(
                "paper_trial", {}).get("enabled") is True,
            "tradeable_vehicle": tradeable,
            # Proved edges in the vehicle this profile cannot trade. They are
            # real evidence, but this trader will never act on them, so they
            # are reported rather than counted among the deployable edges.
            "untradeable_proved_edges": untradeable,
            "note": (
                "one explicitly configured, unvalidated paper experiment is permitted; "
                "qualified and live lanes still require verified edge proof"
                if config.get("research", {}).get("paper_trial", {}).get("enabled") is True
                else "the service is optional to run continuously; entries require a validated edge record"),
            "direct_job": direct,
        },
        "reports": _cached(
            f"reports:{root}", 30, lambda: _reports(root)),
    }


def report_file(root: Path, relative: str) -> tuple[str, str]:
    allowed_roots = [
        (root / "research" / "results").resolve(),
    ]
    candidate = (root / relative).resolve()
    if candidate.suffix.lower() != ".md" or not any(
            candidate.is_relative_to(base) for base in allowed_roots):
        raise FileNotFoundError("report is outside the read-only report roots")
    text = candidate.read_text(encoding="utf-8")
    return text[:200_000], mimetypes.guess_type(candidate.name)[0] or "text/plain"


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alpaca Agent — read-only state</title>
<style>
:root{color-scheme:dark;font:14px system-ui,sans-serif;background:#0b1020;color:#e7ecf7}
body{margin:0 auto;max-width:1440px;padding:24px}h1{margin:0 0 4px}.muted{color:#9aa7bd}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;margin-top:18px}
.card{background:#131b2e;border:1px solid #28334b;border-radius:10px;padding:14px;overflow:auto}
.wide{grid-column:1/-1}.row{display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px solid #202b40}
.ok{color:#65d98a}.bad{color:#ff7b86}.warn{color:#f4c95d}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px;border-bottom:1px solid #28334b}button{background:#263652;color:#e7ecf7;border:0;border-radius:6px;padding:6px 9px;cursor:pointer}
pre{white-space:pre-wrap;max-height:70vh;overflow:auto;background:#090d18;padding:12px;border-radius:8px}
h2{font-size:15px;margin:0 0 8px}h3{font-size:13px;margin:12px 0 4px}
details{margin:8px 0}summary{cursor:pointer;color:#9aa7bd;padding:4px 0}
td,th{white-space:nowrap;font-variant-numeric:tabular-nums}
.card>table{display:block;overflow-x:auto}
</style></head><body>
<h1>Alpaca agent</h1><div class="muted">Read-only operational view. Auto-refreshes every 30 seconds.</div>
<div id="error" class="bad"></div><main class="grid" id="cards"></main>
<section class="card" style="margin-top:18px"><h2>Trader workbench</h2>
<p class="muted">Filter recorded evidence. Market candles show complete observations available by the chosen cutoff. Research and account results have separate sources.</p>
<form id="workbench-form" style="display:flex;flex-wrap:wrap;gap:10px">
<label>Source <select name="source"><option>paper</option><option>live</option><option>sim</option><option>research</option></select></label>
<label>Symbol <input name="symbol" value="SPY" size="6"></label>
<label>Feed <select name="feed"><option>iex</option><option>sip</option></select></label>
<label>From <input type="date" name="start_date"></label><label>To <input type="date" name="end_date"></label>
<label>Variant <input name="variant_id" size="18"></label><label>Candidate <input name="candidate_id" size="18"></label>
<label>Proof epoch <input name="proof_epoch" size="14"></label>
<label>Information cutoff <input name="as_of" placeholder="ISO time with timezone" size="24"></label>
<button type="submit">Load evidence</button></form>
<div id="workbench-result"></div></section>
<script>
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const card=(title,wide=false)=>{const n=el('section');n.className='card'+(wide?' wide':'');n.append(el('h2',title));cards.append(n);return n};
function displayValue(value,key=''){if(value===null||value===undefined)return '—';if(typeof value==='number'){if(!Number.isFinite(value))return '—';if(/win[_ ]rate$/.test(key))return (value*100).toFixed(2)+'%';return value.toLocaleString('en-US',{maximumFractionDigits:/usd|p&l|fees/i.test(key)?2:4})}return String(value)}
const row=(parent,k,v,cls)=>{const n=el('div',undefined,'row');n.append(el('span',k,'muted'),el('span',displayValue(v,k),cls));parent.append(n)};
const good=x=>x?'ok':'bad'; const when=x=>{if(!x)return '—';const d=new Date(typeof x==='number'?x*1000:x);return Number.isNaN(d.getTime())?'—':d.toISOString()};
const columnLabels={mean_r_pct:'Mean R × 100',capital_return_pct:'Capital return %',mean_r_lcb:'Mean R: 95% lower bound',win_rate:'Net win rate'};
function table(parent,rows,cols){const t=el('table'),h=el('tr');cols.forEach(c=>h.append(el('th',columnLabels[c]||c.replaceAll('_',' '))));t.append(h);rows.forEach(r=>{const tr=el('tr');cols.forEach(c=>tr.append(el('td',c==='when'?when(r[c]):displayValue(r[c],c))));t.append(tr)});parent.append(t)}
function chart(parent,title,series,color){
 const wrap=el('div');wrap.append(el('h3',title));
 const pts=((series||{}).points||[]).filter(x=>Number.isFinite(Number(x.value)));
 if(!series||!series.available||!pts.length){wrap.append(el('p','Unavailable: no recorded samples.','muted'));parent.append(wrap);return}
 const w=720,h=150,p=18,vals=pts.map(x=>Number(x.value)),lo=vals.reduce((a,b)=>Math.min(a,b),Infinity),hi=vals.reduce((a,b)=>Math.max(a,b),-Infinity),span=hi-lo||1;
 const first=Number(pts[0].ts),last=Number(pts[pts.length-1].ts),timed=Number.isFinite(first)&&Number.isFinite(last)&&last>first;
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 '+w+' '+h);svg.setAttribute('width','100%');svg.setAttribute('height','150');svg.setAttribute('role','img');svg.setAttribute('aria-label',title);
 const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('fill','none');path.setAttribute('stroke',color);path.setAttribute('stroke-width','2');
 path.setAttribute('d',pts.map((x,i)=>((i?'L':'M')+(p+(timed?(Number(x.ts)-first)/(last-first):i/Math.max(1,pts.length-1))*(w-2*p))+' '+(h-p-(Number(x.value)-lo)*(h-2*p)/span))).join(' '));svg.append(path);
 row(wrap,'Value range',lo.toFixed(2)+' to '+hi.toFixed(2));wrap.append(svg);row(wrap,'Observed interval',when(pts[0].ts)+' to '+when(pts[pts.length-1].ts));parent.append(wrap)
}
function contextCharts(trades){
 const c=card('Entry context from completed bars',true);let count=0;
 for(const t of trades||[]){const x=t.intraday_context;if(!x||!Array.isArray(x.bars)||!x.bars.length)continue;count++;
  chart(c,t.symbol+' · '+x.timeframe_minutes+'-minute closes',{available:true,points:x.bars.map(b=>({ts:Number(b.timestamp)+Number(x.timeframe_minutes)*60,value:b.close}))},'#8fb9ff');
  row(c,'Entry / stop / target',[t.entry_price,t.active_stop_price??t.stop_price,t.target_price].map(v=>v??'—').join(' / '));
  row(c,'Context direction / efficiency',x.direction+' / '+Number(x.efficiency).toFixed(3));
  row(c,'Information cutoff',when(x.asof_ts));
 }
 c.append(el('p',count?'Frozen context used at entry; this chart does not update with current market prices.':'No active position has recorded entry context. New context-enabled strategies record it when a signal passes.','muted'))
}
function evidenceCharts(d){const ch=d.charts||{};const c=card('Actual account evidence — '+(ch.source||'unknown'),true);row(c,'source',ch.source||'unknown');const u=ch.uncertainty||{};row(c,'net-R samples',u.sample_count);row(c,'sessions',u.session_count);row(c,'net-R mean',u.mean_net_r);row(c,'net-R 95% lower bound',u.lower_net_r);row(c,'net-R 95% upper bound',u.upper_net_r);row(c,'equity basis','Observed account equity; cash transfers are not adjusted');chart(c,'Account equity',ch.net_equity,'#65d98a');chart(c,'Drawdown',ch.drawdown,'#ff7b86');const p=ch.payoff||{};row(c,'net payoff samples',p.sample_count);row(c,'net wins / losses',(p.wins??'—')+' / '+(p.losses??'—'));if(!p.available)c.append(el('p','Payoff unavailable until closed trades have known net P&L.','muted'));else{const vals=p.values||[],max=vals.reduce((a,b)=>Math.max(a,Math.abs(b)),1);const bar=el('div');vals.slice(-40).forEach(v=>{const b=el('span');b.style.display='inline-block';b.style.width='6px';b.style.height=Math.max(2,Math.round(Math.abs(v)/max*70))+'px';b.style.margin='1px';b.style.background=v>=0?'#65d98a':'#ff7b86';b.title=String(v);bar.append(b)});c.append(el('h3','Recent net payoff'));c.append(bar)}}
async function showReport(path){const r=await fetch('/api/report?path='+encodeURIComponent(path));const j=await r.json();const p=card(path,true);p.append(el('pre',j.text||j.error||'unavailable'));p.scrollIntoView({behavior:'smooth'})}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'}),d=await r.json();cards.replaceChildren();
 let c=card('Trader');row(c,'mode',d.mode);row(c,'strategy',d.strategy.id+' / '+d.strategy.version);row(c,'execution profile',d.strategy.execution_mode);row(c,'configured variant',d.strategy.variant_id);row(c,'health',d.trader.health.status,good(d.trader.health.ok));row(c,'state',d.trader.state.state);row(c,'last heartbeat',when(d.trader.heartbeat.updated_ts));row(c,'edge entry gate',d.research.entry_gate_required?'required':'disabled',d.research.entry_gate_required?'warn':'ok');const ps=d.trader.health.paper_selection||d.trader.heartbeat.paper_selection||{};const resolved=ps.resolved||{},proof=resolved.proof||{};row(c,'selection mode',ps.selection_mode||d.strategy.selection_mode);row(c,'requested paper pair',(ps.configured_strategy||d.strategy.id)+' / '+(ps.requested_variant||d.strategy.variant_id));row(c,'selection state',ps.state||'unavailable',ps.state==='ready'?'ok':['waiting_for_proof','paper_trial'].includes(ps.state)?'warn':'bad');row(c,'armed',ps.armed===true?'yes':ps.armed===false?'no':'unavailable',ps.armed===true?'ok':'warn');row(c,'selection blocker',ps.blocker_code||'none');row(c,'resolved identity',resolved.candidate_id?[resolved.candidate_id,resolved.variant_id,resolved.family].join(' / '):'not resolved');row(c,'verified proof',proof.run_id?[proof.run_id,proof.gate_hash,proof.lane].join(' / '):ps.paper_trial?'none — unvalidated paper experiment':'not resolved');
 const pt=ps.paper_trial;
 if(pt){c=card('Frozen Alpaca paper experiment');row(c,'trial',pt.trial_id);row(c,'exact incumbent',pt.variant_id);row(c,'trial state',pt.state,pt.state==='failed'?'bad':'warn');row(c,'flat-book activation',pt.activation_confirmed===true?'confirmed':'pending');row(c,'started on',pt.started_on);row(c,'valid market sessions',(pt.valid_sessions??'—')+' / '+(pt.required_sessions??'—'));row(c,'closed parent outcomes',(pt.closed_outcomes??'—')+' / '+(pt.required_trades??'—'));row(c,'review horizon',pt.max_review_sessions);row(c,'evidence verdict',pt.verdict);row(c,'entry eligible',pt.entry_eligible===true?'yes — subject to all risk checks':'no');row(c,'evidence blockers',(pt.blockers||[]).join(', ')||'none');const v=pt.verdict_detail||{},ci=v.session_cluster_confidence||{};row(c,'net P&L USD',v.net_pnl);row(c,'mean net R',v.mean_r);row(c,'session-cluster interval',ci.available?[ci.lower_bound,ci.upper_bound].join(' to '):'insufficient evidence');row(c,'proof authority','none — paper-only experiment','warn');c.append(el('p','The incumbent is frozen across restarts. Uncertain losses do not force a variant switch. Safety stops remain active; reaching the review horizon pauses for review, not a fabricated loss verdict. This lane never grants live-trading authority.','muted'));}
 c=card('Recorder & scheduler');row(c,'recorder',d.recorder.status,good(d.recorder.ok));row(c,'current corpus root',d.recorder.corpus_root);row(c,'equity feed',d.recorder.configured_data_feed||d.recorder.data_feed||'—');row(c,'options feed',d.recorder.configured_options_feed||'disabled');row(c,'capture policy',d.recorder.capture_policy||'unknown');if(d.recorder.deferred_catchup){const gap=d.recorder.deferred_catchup;row(c,'latest deferred history',(gap.from||'?')+' → '+(gap.through||'?')+' (unfilled)','warn');}row(c,'latest market write',when(d.recorder.latest_write_ts));row(c,'bar coverage',d.recorder.coverage_status,d.recorder.coverage_status==='covered'?'ok':'warn');row(c,'bar gap symbols',(d.recorder.bar_gap_symbols||[]).join(', ')||'none',(d.recorder.bar_gap_symbols||[]).length?'warn':'ok');row(c,'research scheduler',d.research_service.health.status,good(d.research_service.health.ok));row(c,'cycle outcome',d.research_service.heartbeat.cycle_status);const pf=d.research_service.health.research_preflight||d.research_service.heartbeat.research_preflight||{};row(c,'provider preflight',pf.status||'not_run',pf.status==='ready'||pf.status==='disabled'?'ok':pf.status==='degraded'?'warn':'bad');const rp=d.research_service.heartbeat.research_progress||{};const rpLine=rp.phase?rp.phase+' · '+rp.vehicle+' · '+rp.done+'/'+rp.total+' '+rp.unit:'—';row(c,'research progress',rpLine);const rr=d.research_service.health.research_readiness||{};row(c,'research readiness',rr.state||'unknown',rr.state==='ready'?'ok':'warn');row(c,'sessions remaining',rr.sessions_remaining??'—');row(c,'readiness ETA',when(rr.eta_ts));row(c,'job id',d.research_service.health.job_id);row(c,'job started',when(d.research_service.health.started_ts));row(c,'job completed',when(d.research_service.health.completed_ts));row(c,'hung',d.research_service.health.hung,good(!d.research_service.health.hung));row(c,'next UTC run',when(d.research_service.health.next_run_ts));row(c,'last exit',d.research_service.health.last_exit_code);row(c,'structured failures',(d.research_service.health.structured_failures||[]).length,good(!(d.research_service.health.structured_failures||[]).length));c.append(el('p','This card follows only the configured current recorder corpus. Historical research reports retain their original source identities.','muted'));
 const sh=d.shadow||{},ds=sh.diagnostic_shadow||{};c=card('Diagnostic shadow');row(c,'service alive',sh.ok===true?'yes':'no',good(sh.ok));row(c,'heartbeat age seconds',sh.heartbeat_age_seconds);row(c,'coverage ready',sh.coverage_ready===true?'yes':'no',sh.coverage_ready?'ok':'warn');row(c,'coverage status',sh.coverage_status);row(c,'active cohort',ds.cohort_active===true?'yes':'no',ds.cohort_active?'ok':'warn');row(c,'activation',ds.activation_status);row(c,'family coverage',(ds.families_covered??'—')+' / '+(ds.families_total??'—'));row(c,'baseline / variant / arms',(ds.baseline_count??'—')+' / '+(ds.variant_count??'—')+' / '+(ds.arms_total??'—'));row(c,'arm cursors projected',(ds.cursor_count??'—')+' / 24');row(c,'arm cursor status',ds.cursor_status);row(c,'candidate errors',ds.candidate_errors_present===true?(ds.candidate_error_count??'invalid'):'unknown',ds.candidate_errors_clear===true?'ok':'warn');row(c,'families observed',ds.families_observed);row(c,'source lag seconds',ds.source_lag_seconds);row(c,'poll duration seconds',ds.poll_duration_seconds);row(c,'observation',ds.observation_status);row(c,'proof authority',ds.proof_authority===false?'none — diagnostic only':'unavailable',ds.proof_authority===false?'ok':'warn');c.append(el('p','Coverage readiness describes only the current bounded poll, not an accepted full market session. This lane cannot authorize proof, promotion, or broker orders. Zero actual fills is not a profit claim.','muted'));
 const fa=ds.forward_accounts||{},dc=ds.decision_counts||{},rc=ds.rejection_counts||{};row(c,'persistent shadow accounts',fa.account_count);row(c,'modeled orders / fills',(fa.orders??'—')+' / '+(fa.modeled_fills??'—'));row(c,'open / closed positions',(fa.open_positions??'—')+' / '+(fa.closed_positions??'—'));row(c,'unpriced accounts',fa.unpriced_account_count);row(c,'late-data-gap positions',fa.late_data_gap_positions);row(c,'evaluations / no-trade decisions',(dc.evaluated_total??'—')+' / '+(dc.compacted_no_trade??'—'));row(c,'risk refusals / unpriced signals',(rc.reject??'—')+' / '+(rc.unpriced??'—'));
 c=card('Persistent shadow books — modeled, not broker returns',true);if((fa.by_candidate||[]).length){table(c,fa.by_candidate,['family','role','variant_id','cash','equity','realized_pnl','unrealized_pnl','open_positions','closed_positions','fills','mark_status','last_event_at','late_data_gaps']);}else{c.append(el('p','No persistent account snapshot yet. Zero signal events do not mean fills or profits.','muted'));}c.append(el('p','Each row is an independent virtual account. Cash carries across sessions; entry and exit costs are charged once. Unknown marks stay unknown. Valuations are as of last_event_at; check source freshness above. These balances are not pooled into an Alpaca account return.','muted'));
 const refusals=[...Object.entries(rc.by_reason||{}).map(([reason,count])=>({stage:'risk refusal',reason,count})),...Object.entries(rc.unpriced_by_reason||{}).map(([reason,count])=>({stage:'unpriced',reason,count}))];if(refusals.length){c=card('Why shadow signals did not trade',true);table(c,refusals,['stage','reason','count']);}
 c=card('Execution journal');row(c,'available',d.performance.available,good(d.performance.available));row(c,'events',d.performance.events);row(c,'closed trades',d.performance.closed_trades);row(c,'gross P&L USD',d.performance.gross_pnl_usd);row(c,'fees USD',d.performance.fees_usd);row(c,'net P&L USD',d.performance.net_pnl_usd);row(c,'net win rate',d.performance.win_rate);row(c,'completed parent trades',d.performance.closed_trades);row(c,'cost basis',(d.performance.cost_provenance||[]).join(', ')||'unavailable');
 const direct=d.research_service.direct||{};
 c=card('Research process — direct research status');row(c,'status',direct.status,direct.running?'ok':'warn');row(c,'job',direct.job_id);row(c,'process',direct.pid);row(c,'scheduler ownership',direct.scheduler_managed===null?'unknown':direct.scheduler_managed?'managed':'direct');row(c,'build',direct.build_identity);row(c,'started',when(direct.started_ts));row(c,'process lease',when(direct.lease_ts));const dp=direct.progress||{};row(c,'phase',dp.phase);row(c,'completed work',dp.done===undefined?'—':dp.done+'/'+dp.total+' '+(dp.unit||''));row(c,'last progress',when(dp.updated_ts));row(c,'dataset',(direct.dataset||{}).source);row(c,'source identity',(direct.dataset||{}).source_identity);row(c,'outcome',(direct.terminal||{}).reason);if(!direct.running)c.append(el('p','No fresh running process lease. A saved result is not an active job.','muted'));
 evidenceCharts(d);
 c=card('Research');row(c,'service mode',d.research.service_optional?'on demand':'continuous');row(c,'ledger available',d.research.available,good(d.research.available));row(c,'edge ledger',d.edge.status,good(d.edge.available));row(c,'candidates',d.edge.candidates);row(c,'proved edges',(d.edge.proved_edges||[]).length);row(c,'vehicles',JSON.stringify(d.edge.by_vehicle||{}));row(c,'lifecycle',JSON.stringify(d.edge.by_status||{}));row(c,'factory hypotheses',(d.edge.factory||{}).hypotheses);row(c,'isolated simulations',(d.edge.factory||{}).accounts);row(c,'factory cycles',(d.edge.factory||{}).cycles);row(c,'tradeable vehicle',d.research.tradeable_vehicle);row(c,'proved but untradeable',d.research.untradeable_proved_edges,d.research.untradeable_proved_edges?'warn':'ok');c.append(el('p',d.research.note||'No research status.','muted'));
 c=card('Proved edges — evidence at promotion',true);table(c,d.edge.proved_edges||[],['status','vehicle','strategy_id','variant_id','confidence','candidate_id','gate_hash']);
 c=card('Live paper results by edge',true);const lp=d.edge.live_paper||[];if(!lp.length){c.append(el('p','No paper outcomes recorded yet. Results appear once a deployed edge closes its first trade.','muted'))}else{table(c,lp,['status','vehicle','variant_id','outcomes','sessions','last_session','total_r','mean_r','win_rate','net_pnl','rolling_r','guard','rolling_action'])};
 c=card('Recorded portfolio exposure',true);const exposure=d.trader.exposure||{};table(c,exposure.groups||[],['group','positions','gross_usd','net_usd']);row(c,'positions without a comparable exposure',exposure.unpriced_or_unmapped_positions);c.append(el('p','Recorded price notional, excluding pending orders. Shared ETF exposure is not an independent bet count; no beta hedge is estimated.','muted'));
 c=card('Active positions',true);table(c,d.trader.state.active_trades||[],['symbol','variant_id','direction','qty','entry_price','stop_price','target_price','active_stop_price','configured_risk_budget_usd','planned_risk_usd','delivered_risk_usd','planned_to_configured_risk_ratio','delivered_to_configured_risk_ratio','opened_at','setup_type']);

 contextCharts(d.trader.state.active_trades);
 const tr=d.trial||{};
 c=card('Qualified-edge paper reviews — separate from the experiment',true);
 if(!tr.available){c.append(el('p','No trial data yet. Trials use the same Alpaca paper account once an edge is proved.','muted'))}
 else{const p=tr.policy||{};c.append(el('p','Trial window: '+p.min_sessions+' sessions and '+p.min_trades+' trades, then judged against total R > '+p.min_total_r+' and a 95% session-cluster lower bound for mean R > '+p.min_mean_r+'.','muted'));
  table(c,tr.reviews||[],['state','action','family','vehicle','variant_id','sessions','trades','total_r','mean_r','mean_r_lcb','pinned'])}
 c=card('Promotable — positive on the paper account',true);
 const pr=(tr.promotable)||[];
 if(!pr.length){c.append(el('p','Nothing has cleared its trial floor yet. Promotion is never automatic.','muted'))}
 else{table(c,pr,['variant_id','family','vehicle','sessions','trades','total_r','mean_r','win_rate','net_pnl','mean_r_pct','capital_return_pct','already_pinned']);
  pr.filter(x=>x.config_snippet).forEach(x=>{const b=el('details');b.append(el('summary','Config to promote '+x.variant_id));b.append(el('pre',x.config_snippet));c.append(b)})}

 const pm=d.promotions||{};
 c=card('Pinned promotions (operator-declared)',true);
 row(c,'selection mode',pm.selection_mode,pm.selection_mode==='pinned'?'ok':'muted');
 row(c,'automatic substitution',pm.frozen?'disabled for pinned edges':'enabled (auto lane)',pm.frozen?'ok':'warn');
 table(c,pm.pinned||[],['id','variant_id','vehicle','strategy_id','promoted_at','note']);
 if((pm.unresolved||[]).length){const w=el('h3','Pinned but NOT trading');w.className='bad';c.append(w);table(c,pm.unresolved,['id','variant_id','vehicle','reason'])}
 c.append(el('p',pm.note||'','muted'));

 const jr=d.journal||{};
 c=card('Trades by edge — what the broker actually did',true);
 if(!(jr.by_variant||[]).length){c.append(el('p','No fills recorded yet.','muted'))}
 else{row(c,'lifetime closed parent trades',jr.lifetime&&jr.lifetime.closed_trades);row(c,'lifetime close fills',jr.lifetime&&jr.lifetime.close_fills);table(c,jr.by_variant,['strategy_id','variant_id','trades','close_fills','symbols','total_r','r_basis','mean_r','win_rate','win_rate_basis','gross_pnl_usd','fees_usd','net_pnl_usd','realized_pnl_basis','cost_provenance'])}
 c=card('Recent fills, attributed (page '+(jr.page||1)+' of recent journal)',true);
 table(c,jr.trades||[],['when','symbol','side','action','qty','price','configured_risk_budget_usd','planned_risk_usd','delivered_risk_usd','planned_to_configured_risk_ratio','delivered_to_configured_risk_ratio','gross_pnl','fees','net_pnl','realized_pnl_usd','realized_pnl_basis','net_r_multiple','gross_r_multiple','strategy_id','variant_id','setup_type','close_trigger']);
 row(c,'fills on this page',(jr.trades||[]).length);row(c,'total fills',jr.total_fills);row(c,'next page',jr.has_more?'available':'none');

 const lr=d.learning||{};
 c=card('What research learned',true);
 if(!lr.available){c.append(el('p','No recorded reasons yet.','muted'))}
 else{const s=lr.summary||{};row(c,'reasons recorded',s.recorded);row(c,'graded against a gate',s.graded);row(c,'built on an earlier lesson',s.built_on_a_prior_lesson);row(c,'from live paper trials',s.from_live_trials);row(c,'authored by the model',s.llm_authored);
  table(c,(lr.lessons||[]).slice(0,40),['verdict','kind','proposed_by','family','reason','built_on','changed','heldout_delta'])}

 const ca=d.config_audit||{};
 c=card('Configuration audit trail',true);
 row(c,'current version',ca.current);
 if(!(ca.versions||[]).length){c.append(el('p','No configuration versions recorded yet. One is written the first time the trader starts.','muted'))}
 else{table(c,ca.versions,['config_version_id','when','mode','actor','source','changes','changed_paths','previous_version_id'])}

 c=card('Latest reports',true);(d.reports||[]).forEach(x=>{const n=el('div',undefined,'row');n.append(el('span',x.path),el('button','view'));n.lastChild.onclick=()=>showReport(x.path);c.append(n)});
 error.textContent='';}catch(e){error.textContent='Dashboard refresh failed: '+e.name}}
function candleChart(parent,name,points){
 const box=el('div');box.append(el('h3',name+' candles'));parent.append(box);
 if(!points.length){box.append(el('p','Unavailable: no complete recorded bars.','muted'));return}
 const data=points.slice(-600),w=960,h=200,p=18,lo=Math.min(...data.map(r=>r.low)),hi=Math.max(...data.map(r=>r.high)),span=hi-lo||1;
 const start=data[0].ts,end=data[data.length-1].ts,range=end-start||1,x=t=>p+(t-start)/range*(w-2*p),y=v=>h-p-(v-lo)/span*(h-2*p);
 const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 '+w+' '+h);svg.setAttribute('width','100%');svg.setAttribute('height',h);svg.setAttribute('role','img');svg.setAttribute('aria-label',name+' OHLC candles');
 for(const r of data){const line=document.createElementNS(svg.namespaceURI,'path');const px=x(r.ts),bw=Math.max(1,Math.min(8,(w-2*p)/data.length*.6));line.setAttribute('d',`M${px} ${y(r.high)}V${y(r.low)} M${px-bw/2} ${y(r.open)}H${px}V${y(r.close)}H${px+bw/2}`);line.setAttribute('stroke',r.close>=r.open?'#65d98a':'#ff7b86');line.setAttribute('fill','none');const title=document.createElementNS(svg.namespaceURI,'title');title.textContent=when(r.ts)+' · O '+r.open+' H '+r.high+' L '+r.low+' C '+r.close;line.append(title);svg.append(line)}
 box.append(svg);row(box,'Observed interval',when(start)+' to '+when(end));row(box,'Price range',lo.toFixed(2)+' to '+hi.toFixed(2));if(points.length>data.length)box.append(el('p','Showing the latest 600 complete candles.','muted'))
}
document.getElementById('workbench-form').addEventListener('submit',async event=>{
 event.preventDefault();const out=document.getElementById('workbench-result');out.replaceChildren(el('p','Loading recorded evidence…'));
 try{const query=new URLSearchParams(new FormData(event.target));const response=await fetch('/api/workbench?'+query);const d=await response.json();if(!response.ok)throw Error(d.error||'Evidence unavailable');out.replaceChildren();
 const c=d.candles||{},e=d.evidence||{};row(out,'Evidence source',d.filters.source);row(out,'Chart source',c.source||c.reason);if(c.incomplete_source)out.append(el('p','Some source files exceeded the read bound. This is an incomplete chart window.','warn'));
 for(const [name,points] of Object.entries(c.series||{}))candleChart(out,name,points);
 if(c.prior_session){row(out,'Prior complete session',c.prior_session.session_date);row(out,'Prior high / low / close',[c.prior_session.high,c.prior_session.low,c.prior_session.close].join(' / '))}
 if(e.parents){out.append(el('h3','Completed parent trades'));table(out,e.parents.map(r=>({...r,when:r.ts})),['symbol','variant_id','when','gross','fees','net','r_multiple','hold_minutes','exit_reason']);out.append(el('p','Holding time uses recorded fill timestamps where available, otherwise the journal interval.','muted'));out.append(el('h3','Recorded signal and risk events'));table(out,Object.entries(e.signal_funnel||{}).map(([event,count])=>({event,count})),['event','count']);out.append(el('h3','Exit distribution'));table(out,Object.entries(e.exit_distribution||{}).map(([reason,count])=>({reason,count})),['reason','count']);if(e.path_metrics)out.append(el('p',e.path_metrics.reason,'muted'))}
 if(e.order_timing){out.append(el('h3','Recorded order timing'));const timing=e.order_timing.map(r=>Object.fromEntries(Object.entries(r).map(([k,v])=>[k,k.endsWith('_ts')?when(v):v])));table(out,timing,['symbol','variant_id','status','decision_ts','request_sent_ts','response_received_ts','submit_roundtrip_ms','broker_submitted_ts','broker_filled_ts','entry_quote_age_seconds']);out.append(el('p','Missing timing stays unknown. Local request timing and broker clocks have different measurement boundaries.','muted'))}
 for(const r of e.records||[]){const detail=el('details'),summary=el('summary',r.variant_id||r.candidate_id||r.kind||'Recorded evidence');detail.append(summary,el('pre',JSON.stringify(r.evidence||r,null,2)));out.append(detail)}
 if(e.reason)out.append(el('p',e.reason,'muted'));if(e.truncated)out.append(el('p','Journal read is limited to the latest 10,000 rows; lifetime totals remain in the account view.','warn'));
 }catch(error){out.replaceChildren(el('p',error.message,'bad'))}
});
refresh();setInterval(refresh,30000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    root = Path(".").resolve()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        self._send(status, json.dumps(value, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:                              # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(HTTPStatus.OK, HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if parsed.path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True, "component": "dashboard",
                                       "provenance": deployment_provenance()})
            return
        if parsed.path == "/readyz":
            ok = all((
                (self.root / "config.yaml").is_file(),
                (self.root / "runtime").is_dir(),
                (self.root / "research" / "cache").is_dir(),
            ))
            self._json(HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE,
                       {"ok": ok, "component": "dashboard",
                        "provenance": deployment_provenance()})
            return
        if parsed.path == "/api/workbench":
            try:
                self._json(HTTPStatus.OK, workbench(self.root, parse_qs(parsed.query)))
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": type(exc).__name__})
            return
        if parsed.path == "/api/status":
            try:
                self._json(HTTPStatus.OK, snapshot(self.root))
            except Exception as exc:                       # noqa: BLE001
                self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                           {"error": type(exc).__name__})
            return
        if parsed.path == "/api/report":
            relative = (parse_qs(parsed.query).get("path") or [""])[0]
            try:
                text, _ = report_file(self.root, relative)
                self._json(HTTPStatus.OK, {"path": relative, "text": text})
            except (OSError, ValueError):
                self._json(HTTPStatus.NOT_FOUND, {"error": "report not found"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:                             # noqa: N802
        self._json(HTTPStatus.METHOD_NOT_ALLOWED,
                   {"error": "dashboard is read-only"})

    def log_message(self, fmt: str, *args) -> None:
        print("dashboard:", fmt % args)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    Handler.root = args.root.resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"read-only dashboard listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
