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
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deploy import health, load_config
from research.gates import verify_gate_envelope


SAFE_STATE_FIELDS = (
    "state", "operator_pause", "runtime_mode",
    "account_fingerprint", "day", "day_start_equity", "high_water_mark",
    "equity_basis", "transfer_reconciliation_required",
)
SAFE_TRADE_FIELDS = (
    "symbol", "direction", "qty", "entry_price", "opened_at", "setup_type",
    "strategy_id", "strategy_version", "stop_loss_pct", "take_profit_pct",
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
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


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


# Mirrors research.edge_ledger.PAPER_DEMOTION_* .  The dashboard deliberately
# does not import the research package, so the guard thresholds it displays are
# restated here and pinned to the ledger's constants by test_deploy.
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
        "research_cycle",
    }
    return {key: value for key, value in raw.items() if key in allowed}


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
        "recorder": health.recorder(recorder_path, 900),
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
        "edge": edge,
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
 c=card('Recorder & scheduler');row(c,'recorder',d.recorder.status,good(d.recorder.ok));row(c,'latest market write',when(d.recorder.latest_write_ts));row(c,'research scheduler',d.research_service.health.status,good(d.research_service.health.ok));row(c,'cycle outcome',d.research_service.heartbeat.cycle_status);row(c,'job id',d.research_service.health.job_id);row(c,'job started',when(d.research_service.health.started_ts));row(c,'job completed',when(d.research_service.health.completed_ts));row(c,'hung',d.research_service.health.hung,good(!d.research_service.health.hung));row(c,'next UTC run',when(d.research_service.health.next_run_ts));row(c,'last exit',d.research_service.health.last_exit_code);row(c,'structured failures',(d.research_service.health.structured_failures||[]).length,good(!(d.research_service.health.structured_failures||[]).length));
 c=card('Execution journal');row(c,'available',d.performance.available,good(d.performance.available));row(c,'events',d.performance.events);row(c,'closed trades',d.performance.closed_trades);row(c,'realized P&L USD',d.performance.realized_pnl_usd);row(c,'win rate',d.performance.win_rate);
 c=card('Research');row(c,'service mode',d.research.service_optional?'on demand':'continuous');row(c,'ledger available',d.research.available,good(d.research.available));row(c,'edge ledger',d.edge.status,good(d.edge.available));row(c,'candidates',d.edge.candidates);row(c,'proved edges',(d.edge.proved_edges||[]).length);row(c,'vehicles',JSON.stringify(d.edge.by_vehicle||{}));row(c,'lifecycle',JSON.stringify(d.edge.by_status||{}));row(c,'factory hypotheses',(d.edge.factory||{}).hypotheses);row(c,'isolated simulations',(d.edge.factory||{}).accounts);row(c,'factory cycles',(d.edge.factory||{}).cycles);row(c,'tradeable vehicle',d.research.tradeable_vehicle);row(c,'proved but untradeable',d.research.untradeable_proved_edges,d.research.untradeable_proved_edges?'warn':'ok');c.append(el('p',d.research.note||'No research status.','muted'));
 c=card('Proved edges — evidence at promotion',true);table(c,d.edge.proved_edges||[],['status','vehicle','strategy_id','variant_id','confidence','candidate_id','gate_hash']);
 c=card('Live paper results by edge',true);const lp=d.edge.live_paper||[];if(!lp.length){c.append(el('p','No paper outcomes recorded yet. Results appear once a deployed edge closes its first trade.','muted'))}else{table(c,lp,['status','vehicle','variant_id','outcomes','sessions','last_session','total_r','mean_r','win_rate','net_pnl','rolling_r','guard'])};
 c=card('Active positions',true);table(c,d.trader.state.active_trades||[],['symbol','direction','qty','entry_price','opened_at','setup_type']);
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
