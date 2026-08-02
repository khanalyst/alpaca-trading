#!/usr/bin/env python3
"""Read-only local dashboard over durable runtime and research evidence."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deploy import health


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
        with _ro_connect(path) as connection:
            return {"available": True, **report.json_report(connection)}
    except Exception as exc:                               # noqa: BLE001
        return {"available": False, "reason": type(exc).__name__}


def _readiness(path: Path, config: dict) -> dict:
    if not path.is_file():
        return {"available": False, "gates": [], "stats": {}}
    try:
        from research import readiness
        gates, stats = readiness.report(path, config)
        return {
            "available": True,
            "gates": [
                {"name": gate.name, "title": gate.title,
                 "status": gate.status, "detail": gate.detail,
                 "next_step": gate.next_step, "blocks": gate.blocks}
                for gate in gates
            ],
            "stats": stats,
        }
    except Exception as exc:                               # noqa: BLE001
        return {"available": False, "gates": [], "stats": {},
                "reason": type(exc).__name__}


def _findings(path: Path) -> dict:
    result = {
        "available": False, "assignments": [], "outcomes": [],
        "backup": None,
    }
    if not path.is_file():
        return result
    try:
        with _ro_connect(path) as connection:
            tables = _tables(connection)
            if {"strategy_experiment_assignments",
                    "strategy_experiment_assignment_events"} <= tables:
                rows = connection.execute("""
                    SELECT assignment.assignment_id, assignment.strategy_id,
                           assignment.baseline_variant_id,
                           assignment.candidate_variant_id,
                           assignment.started_ts,
                           assignment.minimum_duration_seconds,
                           assignment.minimum_observations,
                           event.status, event.event_ts,
                           (SELECT COUNT(*) FROM
                                strategy_experiment_observations AS observation
                            WHERE observation.assignment_id=
                                assignment.assignment_id) AS observed_count
                    FROM strategy_experiment_assignments AS assignment
                    JOIN strategy_experiment_assignment_events AS event
                      ON event.rowid=(
                        SELECT latest.rowid
                        FROM strategy_experiment_assignment_events AS latest
                        WHERE latest.assignment_id=assignment.assignment_id
                        ORDER BY latest.event_ts DESC, latest.rowid DESC LIMIT 1)
                    ORDER BY assignment.started_ts DESC LIMIT 50
                """).fetchall()
                result["assignments"] = [dict(row) for row in rows]
            if "experiment_outcomes" in tables:
                rows = connection.execute("""
                    SELECT outcome_id, assignment_id, strategy_id,
                           baseline_variant_id, candidate_variant_id,
                           terminal_status, verdict, created_ts, payload_hash
                    FROM experiment_outcomes
                    ORDER BY created_ts DESC LIMIT 50
                """).fetchall()
                result["outcomes"] = [dict(row) for row in rows]
            if {"backup_runs", "backup_run_events"} <= tables:
                row = connection.execute("""
                    SELECT run.backup_id, run.started_ts, run.target_kind,
                           run.require_external, event.status,
                           event.event_ts AS status_ts
                    FROM backup_runs AS run
                    JOIN backup_run_events AS event ON event.rowid=(
                        SELECT latest.rowid FROM backup_run_events AS latest
                        WHERE latest.backup_id=run.backup_id
                        ORDER BY latest.event_ts DESC, latest.rowid DESC LIMIT 1)
                    ORDER BY run.started_ts DESC LIMIT 1
                """).fetchone()
                if row:
                    result["backup"] = {
                        **dict(row),
                        "different_device_claimed": (
                            row["target_kind"] == "external_mounted"),
                        # The application has no independent remote-storage
                        # attestation. A container mount boundary is not proof
                        # that the copy survives deletion of the VM.
                        "off_host_verified": False,
                        "off_host_note": (
                            "A different-device/container mount is not proof "
                            "that this backup survives VM deletion."),
                    }
        result["available"] = True
    except Exception as exc:                               # noqa: BLE001
        result["reason"] = type(exc).__name__
    return result


def _reports(root: Path) -> list[dict]:
    candidates = set((root / "findings").glob("**/*.md"))
    candidates.update((root / "research" / "results").glob("**/REPORT.md"))
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
        "research_available", "trading_state", "last_cycle_ts",
        "last_cycle_error", "stop_reason", "next_run_ts", "last_run_date",
        "last_exit_code", "started_ts", "completed_ts",
    }
    return {key: value for key, value in raw.items() if key in allowed}


def snapshot(root: Path) -> dict:
    config_path = root / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    mode = str(config.get("mode") or "_unconfigured")
    runtime = root / "runtime"
    journal = runtime / mode / "journal.db"
    recorder_path = runtime / "research" / "recorded"
    findings_path = root / "research" / "cache" / "findings.db"
    trader_heartbeat = runtime / mode / "heartbeat.json"
    research_heartbeat = runtime / "health" / "research.json"
    cycle_seconds = float(config.get("cycle", {}).get("interval_seconds") or 60)
    trader_max_age = max(90.0, cycle_seconds * 4)
    return {
        "schema": 1,
        "generated_ts": time.time(),
        "mode": mode,
        "strategy": {
            key: config.get("strategy", {}).get(key)
            for key in ("id", "version", "signal_timeframe")
        },
        "cycle": {
            key: config.get("cycle", {}).get(key)
            for key in ("interval_seconds", "decision_interval_seconds")
        },
        "research_feed_version": config.get("research", {}).get(
            "forward_feed_version"),
        "trader": {
            "health": health.trader(trader_heartbeat, trader_max_age),
            "heartbeat": _safe_heartbeat(trader_heartbeat),
            "state": _safe_state(runtime / mode / "state.json"),
        },
        "recorder": health.recorder(recorder_path, 900),
        "research_service": {
            "health": health.research(research_heartbeat, 180),
            "heartbeat": _safe_heartbeat(research_heartbeat),
        },
        # Readiness reconstructs the corpus and is intentionally not executed
        # on every browser refresh. Operational state remains uncached.
        "readiness": _cached(
            f"readiness:{journal}", 300, lambda: _readiness(journal, config)),
        "performance": _cached(
            f"performance:{journal}", 30, lambda: _performance(journal)),
        "research": _cached(
            f"findings:{findings_path}", 30, lambda: _findings(findings_path)),
        "reports": _cached(
            f"reports:{root}", 30, lambda: _reports(root)),
    }


def report_file(root: Path, relative: str) -> tuple[str, str]:
    allowed_roots = [
        (root / "findings").resolve(),
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
<title>OKX Agent — read-only state</title>
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
<h1>OKX agent</h1><div class="muted">Read-only operational view. Auto-refreshes every 30 seconds.</div>
<div id="error" class="bad"></div><main class="grid" id="cards"></main>
<script>
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
const card=(title,wide=false)=>{const n=el('section');n.className='card'+(wide?' wide':'');n.append(el('h2',title));cards.append(n);return n};
const row=(parent,k,v,cls)=>{const n=el('div',undefined,'row');n.append(el('span',k,'muted'),el('span',String(v??'—'),cls));parent.append(n)};
const good=x=>x?'ok':'bad'; const when=x=>x?new Date(x*1000).toISOString():'—';
function table(parent,rows,cols){const t=el('table'),h=el('tr');cols.forEach(c=>h.append(el('th',c)));t.append(h);rows.forEach(r=>{const tr=el('tr');cols.forEach(c=>tr.append(el('td',String(r[c]??'—'))));t.append(tr)});parent.append(t)}
async function showReport(path){const r=await fetch('/api/report?path='+encodeURIComponent(path));const j=await r.json();const p=card(path,true);p.append(el('pre',j.text||j.error||'unavailable'));p.scrollIntoView({behavior:'smooth'})}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'}),d=await r.json();cards.replaceChildren();
 let c=card('Trader');row(c,'mode',d.mode);row(c,'strategy',d.strategy.id+' / '+d.strategy.version);row(c,'research feed','v'+d.research_feed_version);row(c,'health',d.trader.health.status,good(d.trader.health.ok));row(c,'state',d.trader.state.state);row(c,'last heartbeat',when(d.trader.heartbeat.updated_ts));row(c,'research attached',d.trader.heartbeat.research_available,good(d.trader.heartbeat.research_available));
 c=card('Recorder & scheduler');row(c,'recorder',d.recorder.status,good(d.recorder.ok));row(c,'latest market write',when(d.recorder.latest_write_ts));row(c,'research scheduler',d.research_service.health.status,good(d.research_service.health.ok));row(c,'next UTC run',when(d.research_service.health.next_run_ts));row(c,'last exit',d.research_service.health.last_exit_code);
 c=card('Performance');row(c,'available',d.performance.available,good(d.performance.available));row(c,'round trips',d.performance.round_trips);row(c,'expectancy R',d.performance.overall?.expectancy_r);row(c,'total R',d.performance.overall?.total_r);row(c,'max drawdown R',d.performance.overall?.max_drawdown_r);
 c=card('Backup');const b=d.research.backup||{};row(c,'latest status',b.status||'none',good(b.status==='VERIFIED'));row(c,'target class',b.target_kind);row(c,'different device claimed',b.different_device_claimed);row(c,'off-host verified',b.off_host_verified,'warn');c.append(el('p',b.off_host_note||'No backup evidence yet.','muted'));
 c=card('Readiness',true);table(c,d.readiness.gates||[],['name','status','title','detail']);
 c=card('Active positions',true);table(c,d.trader.state.active_trades||[],['symbol','direction','qty','entry_price','opened_at','setup_type']);
 c=card('Experiment assignments',true);table(c,d.research.assignments||[],['strategy_id','candidate_variant_id','status','observed_count','minimum_observations','started_ts']);
 c=card('Outcomes',true);table(c,d.research.outcomes||[],['strategy_id','candidate_variant_id','verdict','terminal_status','created_ts']);
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
