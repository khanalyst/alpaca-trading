#!/usr/bin/env python3
"""Read-only local dashboard over durable runtime and research evidence."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import mimetypes
import os
import sqlite3
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deploy import health, load_config
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
    "symbol", "direction", "qty", "entry_price", "opened_at", "setup_type",
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
            "outcomes": 0, "net_pnl": 0.0, "_r": [], "_sessions": set()})
        item["outcomes"] += 1
        try:
            item["net_pnl"] += float(row["net_pnl"])
        except (TypeError, ValueError):
            pass
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
        recent = r_values[-PAPER_ROLLING_WINDOW:]
        wins = [value for value in r_values if value > 0]
        report.append({
            **item,
            "sessions": len(sessions),
            "last_session": max(sessions) if sessions else None,
            "net_pnl": round(item["net_pnl"], 2),
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


def _trades(connection: sqlite3.Connection, limit: int = 200) -> list[dict]:
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

    rows = connection.execute(
        f"""SELECT ts, symbol, side, action, qty, price, notional,
                  {field('realized_pnl_usd')}, {field('risk_usd')},
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
           FROM trades ORDER BY ts DESC, id DESC LIMIT ?""",
        (max(1, int(limit)),)).fetchall()
    trades = []
    for row in rows:
        item = dict(row)
        risk = item.get("risk_usd")
        realized = item.get("realized_pnl_usd")
        try:
            item["r_multiple"] = (round(float(realized) / float(risk), 4)
                                  if risk and realized is not None else None)
        except (TypeError, ValueError, ZeroDivisionError):
            item["r_multiple"] = None
        item["when"] = item.pop("ts", None)
        trades.append(item)
    return trades


def _by_variant(trades: Sequence[dict]) -> list[dict]:
    """Roll the journal's own fills up per deployed variant.

    This is the runtime's view of an edge, independent of the research
    ledger's: it counts what the broker actually did.  When the two disagree,
    that disagreement is the finding.
    """
    grouped: dict[tuple[str, str], dict] = {}
    for trade in trades:
        key = (str(trade.get("strategy_id") or "unknown"),
               str(trade.get("variant_id") or "unattributed"))
        item = grouped.setdefault(key, {
            "strategy_id": key[0], "variant_id": key[1], "trades": 0,
            "symbols": set(), "realized_pnl_usd": 0.0, "_r": [],
            "last_trade_ts": None})
        item["trades"] += 1
        if trade.get("symbol"):
            item["symbols"].add(str(trade["symbol"]))
        try:
            item["realized_pnl_usd"] += float(trade.get("realized_pnl_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        if trade.get("r_multiple") is not None:
            item["_r"].append(float(trade["r_multiple"]))
        when = trade.get("when")
        if when is not None and (item["last_trade_ts"] is None or
                                 when > item["last_trade_ts"]):
            item["last_trade_ts"] = when
    report = []
    for item in grouped.values():
        values = item.pop("_r")
        wins = [value for value in values if value > 0]
        report.append({
            **item,
            "symbols": ", ".join(sorted(item["symbols"])),
            "realized_pnl_usd": round(item["realized_pnl_usd"], 2),
            "total_r": round(sum(values), 4) if values else None,
            "mean_r": round(sum(values) / len(values), 4) if values else None,
            "win_rate": round(len(wins) / len(values), 4) if values else None,
        })
    return sorted(report, key=lambda item: item["trades"], reverse=True)


def _journal_view(path: Path) -> dict:
    """Per-trade attribution and its per-variant roll-up, read-only."""
    if not path.is_file():
        return {"available": False, "trades": [], "by_variant": []}
    try:
        with closing(_ro_connect(path)) as connection:
            if "trades" not in _tables(connection):
                return {"available": False, "trades": [], "by_variant": []}
            trades = _trades(connection)
    except (OSError, sqlite3.Error, ValueError):
        return {"available": False, "trades": [], "by_variant": []}
    return {"available": True, "trades": trades,
            "by_variant": _by_variant(trades)}


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
            } for item in review.get("reviews") or []],
            "promotable": promotable}


def _promotions(config: dict, edge_path: Path) -> dict:
    """What the operator pinned, and whether each pin can actually trade."""
    strategy = config.get("strategy") if isinstance(config, dict) else {}
    entries = (strategy or {}).get("pinned") or []
    mode = str((strategy or {}).get("selection_mode") or "all_proved")
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
            "note": ("pinned edges are never changed automatically; guard "
                     "breaches raise an alert and leave them in place")}


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
    return result


def snapshot(root: Path) -> dict:
    config_path = root / "config.yaml"
    config = load_config(config_path)
    mode = str(config.get("mode") or "paper").lower()
    runtime = root / "runtime"
    journal = runtime / mode / "journal.db"
    recorder_path = runtime / "research" / "recorded"
    trader_heartbeat = runtime / mode / "heartbeat.json"
    research_heartbeat = runtime / "health" / "research.json"
    edge_configured = Path(os.getenv("ALPACA_EDGE_DB", "runtime/research/edge_lab.sqlite3"))
    edge_path = edge_configured if edge_configured.is_absolute() else root / edge_configured
    cycle_seconds = float(config.get("cycle", {}).get("interval_seconds") or 60)
    trader_max_age = max(90.0, cycle_seconds * 4)
    edge = _cached(f"edge:{edge_path}", 30, lambda: _edge_status(edge_path))
    trial = _cached(f"trial:{edge_path}", 60,
                    lambda: _trial_view(config, edge_path))
    tradeable = _tradeable_vehicle(config)
    untradeable = sum(1 for row in edge.get("proved_edges") or ()
                      if str(row.get("vehicle")) != tradeable)
    return {
        "schema": 1,
        "generated_ts": time.time(),
        "mode": mode,
        "strategy": {
            key: config.get("strategy", {}).get(key)
            for key in ("id", "version", "execution_mode", "variant_id")
        },
        "cycle": {
            key: config.get("cycle", {}).get(key)
            for key in ("interval_seconds",)
        },
        "trader": {
            "health": health.trader(trader_heartbeat, trader_max_age),
            "heartbeat": _safe_heartbeat(trader_heartbeat),
            "state": _safe_state(runtime / mode / "state.json"),
        },
        "recorder": health.recorder(
            recorder_path, 900,
            configured_data_feed=((config.get("broker") or {}).get("data_feed")
                                  or "iex"),
            configured_options_feed=((config.get("broker") or {}).get(
                "options_feed") or "indicative")),
        "research_service": {
            "health": (
                health.research(research_heartbeat, 180)
                if research_heartbeat.exists() else {
                    "ok": True, "component": "research", "status": "disabled",
                    "optional": True, "fresh": False, "hung": False,
                    "structured_failures": [],
                }),
            "heartbeat": _safe_heartbeat(research_heartbeat),
        },
        "performance": _cached(
            f"performance:{journal}", 30, lambda: _performance(journal)),
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
            "tradeable_vehicle": tradeable,
            # Proved edges in the vehicle this profile cannot trade. They are
            # real evidence, but this trader will never act on them, so they
            # are reported rather than counted among the deployable edges.
            "untradeable_proved_edges": untradeable,
            "note": "the service is optional to run continuously; entries require a validated edge record",
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
<script>
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const card=(title,wide=false)=>{const n=el('section');n.className='card'+(wide?' wide':'');n.append(el('h2',title));cards.append(n);return n};
const row=(parent,k,v,cls)=>{const n=el('div',undefined,'row');n.append(el('span',k,'muted'),el('span',String(v??'—'),cls));parent.append(n)};
const good=x=>x?'ok':'bad'; const when=x=>x?new Date(x*1000).toISOString():'—';
function table(parent,rows,cols){const t=el('table'),h=el('tr');cols.forEach(c=>h.append(el('th',c)));t.append(h);rows.forEach(r=>{const tr=el('tr');cols.forEach(c=>tr.append(el('td',String(r[c]??'—'))));t.append(tr)});parent.append(t)}
async function showReport(path){const r=await fetch('/api/report?path='+encodeURIComponent(path));const j=await r.json();const p=card(path,true);p.append(el('pre',j.text||j.error||'unavailable'));p.scrollIntoView({behavior:'smooth'})}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'}),d=await r.json();cards.replaceChildren();
 let c=card('Trader');row(c,'mode',d.mode);row(c,'strategy',d.strategy.id+' / '+d.strategy.version);row(c,'execution profile',d.strategy.execution_mode);row(c,'configured variant',d.strategy.variant_id);row(c,'health',d.trader.health.status,good(d.trader.health.ok));row(c,'state',d.trader.state.state);row(c,'last heartbeat',when(d.trader.heartbeat.updated_ts));row(c,'edge entry gate',d.research.entry_gate_required?'required':'disabled',d.research.entry_gate_required?'warn':'ok');
 c=card('Recorder & scheduler');row(c,'recorder',d.recorder.status,good(d.recorder.ok));row(c,'equity feed',d.recorder.configured_data_feed||d.recorder.data_feed||'—');row(c,'options feed',d.recorder.configured_options_feed||'disabled');row(c,'latest market write',when(d.recorder.latest_write_ts));row(c,'bar coverage',d.recorder.coverage_status,d.recorder.coverage_status==='covered'?'ok':'warn');row(c,'bar gap symbols',(d.recorder.bar_gap_symbols||[]).join(', ')||'none',(d.recorder.bar_gap_symbols||[]).length?'warn':'ok');row(c,'research scheduler',d.research_service.health.status,good(d.research_service.health.ok));row(c,'cycle outcome',d.research_service.heartbeat.cycle_status);const pf=d.research_service.health.research_preflight||d.research_service.heartbeat.research_preflight||{};row(c,'provider preflight',pf.status||'not_run',pf.status==='ready'||pf.status==='disabled'?'ok':pf.status==='degraded'?'warn':'bad');const rp=d.research_service.heartbeat.research_progress||{};const rpLine=rp.phase?rp.phase+' · '+rp.vehicle+' · '+rp.done+'/'+rp.total+' '+rp.unit:'—';row(c,'research progress',rpLine);const rr=d.research_service.health.research_readiness||{};row(c,'research readiness',rr.state||'unknown',rr.state==='ready'?'ok':'warn');row(c,'sessions remaining',rr.sessions_remaining??'—');row(c,'readiness ETA',when(rr.eta_ts));row(c,'job id',d.research_service.health.job_id);row(c,'job started',when(d.research_service.health.started_ts));row(c,'job completed',when(d.research_service.health.completed_ts));row(c,'hung',d.research_service.health.hung,good(!d.research_service.health.hung));row(c,'next UTC run',when(d.research_service.health.next_run_ts));row(c,'last exit',d.research_service.health.last_exit_code);row(c,'structured failures',(d.research_service.health.structured_failures||[]).length,good(!(d.research_service.health.structured_failures||[]).length));
 c=card('Execution journal');row(c,'available',d.performance.available,good(d.performance.available));row(c,'events',d.performance.events);row(c,'closed trades',d.performance.closed_trades);row(c,'realized P&L USD',d.performance.realized_pnl_usd);row(c,'win rate',d.performance.win_rate);
 c=card('Research');row(c,'service mode',d.research.service_optional?'on demand':'continuous');row(c,'ledger available',d.research.available,good(d.research.available));row(c,'edge ledger',d.edge.status,good(d.edge.available));row(c,'candidates',d.edge.candidates);row(c,'proved edges',(d.edge.proved_edges||[]).length);row(c,'vehicles',JSON.stringify(d.edge.by_vehicle||{}));row(c,'lifecycle',JSON.stringify(d.edge.by_status||{}));row(c,'factory hypotheses',(d.edge.factory||{}).hypotheses);row(c,'isolated simulations',(d.edge.factory||{}).accounts);row(c,'factory cycles',(d.edge.factory||{}).cycles);row(c,'tradeable vehicle',d.research.tradeable_vehicle);row(c,'proved but untradeable',d.research.untradeable_proved_edges,d.research.untradeable_proved_edges?'warn':'ok');c.append(el('p',d.research.note||'No research status.','muted'));
 c=card('Proved edges — evidence at promotion',true);table(c,d.edge.proved_edges||[],['status','vehicle','strategy_id','variant_id','confidence','candidate_id','gate_hash']);
 c=card('Live paper results by edge',true);const lp=d.edge.live_paper||[];if(!lp.length){c.append(el('p','No paper outcomes recorded yet. Results appear once a deployed edge closes its first trade.','muted'))}else{table(c,lp,['status','vehicle','variant_id','outcomes','sessions','last_session','total_r','mean_r','win_rate','net_pnl','rolling_r','guard','rolling_action'])};
 c=card('Active positions',true);table(c,d.trader.state.active_trades||[],['symbol','direction','qty','entry_price','configured_risk_budget_usd','planned_risk_usd','delivered_risk_usd','planned_to_configured_risk_ratio','delivered_to_configured_risk_ratio','opened_at','setup_type']);

 const tr=d.trial||{};
 c=card('Paper-account trials — which edge is earning a promotion',true);
 if(!tr.available){c.append(el('p','No trial data yet. Trials use the same Alpaca paper account once an edge is proved.','muted'))}
 else{const p=tr.policy||{};c.append(el('p','Trial window: '+p.min_sessions+' sessions and '+p.min_trades+' trades, then judged against total R > '+p.min_total_r+' and mean R > '+p.min_mean_r+'.','muted'));
  table(c,tr.reviews||[],['state','action','family','vehicle','variant_id','sessions','trades','total_r','mean_r','pinned'])}
 c=card('Promotable — positive on the paper account',true);
 const pr=(tr.promotable)||[];
 if(!pr.length){c.append(el('p','Nothing has cleared its trial floor yet. Promotion is never automatic.','muted'))}
 else{table(c,pr,['variant_id','family','vehicle','sessions','trades','total_r','mean_r','win_rate','net_pnl','return_pct','already_pinned']);
  pr.filter(x=>x.config_snippet).forEach(x=>{const b=el('details');b.append(el('summary','Config to promote '+x.variant_id));b.append(el('pre',x.config_snippet));c.append(b)})}

 const pm=d.promotions||{};
 c=card('Pinned promotions (operator-declared)',true);
 row(c,'selection mode',pm.selection_mode,pm.selection_mode==='pinned'?'ok':'muted');
 row(c,'automatic changes',pm.frozen?'disabled — notify only':'enabled (auto lane)',pm.frozen?'ok':'warn');
 table(c,pm.pinned||[],['id','variant_id','vehicle','strategy_id','promoted_at','note']);
 if((pm.unresolved||[]).length){const w=el('h3','Pinned but NOT trading');w.className='bad';c.append(w);table(c,pm.unresolved,['id','variant_id','vehicle','reason'])}
 c.append(el('p',pm.note||'','muted'));

 const jr=d.journal||{};
 c=card('Trades by edge — what the broker actually did',true);
 if(!(jr.by_variant||[]).length){c.append(el('p','No fills recorded yet.','muted'))}
 else{table(c,jr.by_variant,['strategy_id','variant_id','trades','symbols','total_r','mean_r','win_rate','realized_pnl_usd'])}
 c=card('Recent trades, attributed',true);
 table(c,(jr.trades||[]).slice(0,60),['when','symbol','side','action','qty','price','configured_risk_budget_usd','planned_risk_usd','delivered_risk_usd','planned_to_configured_risk_ratio','delivered_to_configured_risk_ratio','realized_pnl_usd','r_multiple','strategy_id','variant_id','setup_type','close_trigger']);

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
            self._json(HTTPStatus.OK, {"ok": True, "component": "dashboard"})
            return
        if parsed.path == "/readyz":
            ok = all((
                (self.root / "config.yaml").is_file(),
                (self.root / "runtime").is_dir(),
                (self.root / "research" / "cache").is_dir(),
            ))
            self._json(HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE,
                       {"ok": ok, "component": "dashboard"})
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
