"""Bounded read-only trader workbench over recorded evidence."""
from collections import Counter
from contextlib import closing
import csv
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

from deploy.market_observations import epoch, read_bars
from agent.order_timing import TIMING_FIELDS
from deploy.research_dataset import (_apply_calendar, _partition_calendar_sidecars,
                                     _partition_source_sidecars)
from report import _parent_key, closed_parent_trades

NY = ZoneInfo("America/New_York")
SCHEMA = "dashboard-workbench.v1"
LIMIT = 10000


def number(value):
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def stamp(value):
    try:
        return epoch(value)
    except (TypeError, ValueError, OverflowError):
        return None


def decoded(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None


def filters(query):
    result = {}
    for key in ("source", "symbol", "candidate_id", "variant_id", "proof_epoch",
                "feed", "start_date", "end_date", "as_of"):
        value = query.get(key, "")
        result[key] = str(value[0] if isinstance(value, list) and value else value).strip()
    result["source"] = result["source"] or "paper"
    if result["source"] not in {"paper", "live", "sim", "research"}:
        raise ValueError("source must be paper, live, sim or research")
    result["symbol"] = result["symbol"].upper()
    for key in ("start_date", "end_date"):
        if result[key]:
            result[key] = date.fromisoformat(result[key]).isoformat()
    if result["start_date"] and result["end_date"] < result["start_date"]:
        result["end_date"] = result["start_date"] if not result["end_date"] else result["end_date"]
        if result["end_date"] < result["start_date"]:
            raise ValueError("end date precedes start date")
    if result["as_of"] and stamp(result["as_of"]) is None:
        raise ValueError("as_of must be an aware timestamp")
    return result


def matches(row, selected):
    for field in ("symbol", "candidate_id", "variant_id", "proof_epoch", "feed"):
        if selected[field] and str(row.get(field) or "") != selected[field]:
            return False
    when = stamp(row.get("ts"))
    if selected["as_of"] and (when is None or when > stamp(selected["as_of"])):
        return False
    day = row.get("session_date") or (
        datetime.fromtimestamp(when, NY).date().isoformat() if when is not None else "")
    return (not selected["start_date"] or day >= selected["start_date"]) and (
        not selected["end_date"] or bool(day) and day <= selected["end_date"])


def sql_rows(path, table):
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in reversed(db.execute(
                f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (LIMIT,)).fetchall())]
    except sqlite3.Error:
        return []


def identity(row):
    result = dict(row)
    payload = decoded(row.get("payload") or row.get("payload_json"))
    if not isinstance(payload, dict):
        payload = {}
    for key in ("symbol", "variant_id", "candidate_id", "proof_epoch", "reason"):
        result[key] = row.get(key) or payload.get(key)
    result["feed"] = (row.get("feed") or row.get("entry_feed") or
                      row.get("signal_bar_feed") or payload.get("feed"))
    result["proof_epoch"] = row.get("proof_run_id") or result.get("proof_epoch")
    return result


def runtime_evidence(root, selected):
    path = root / "runtime" / selected["source"] / "journal.db"
    raw = sql_rows(path, "trades")
    fills = [identity(r) for r in raw if r.get("runtime_mode") == selected["source"]]
    groups = {}
    for i, row in enumerate(fills):
        groups.setdefault(_parent_key(row, i), []).append(row)
    parents = []
    for parent in closed_parent_trades(fills):
        group = groups[parent["parent_trade_id"]]
        opens = [r for r in group if r.get("action") == "open"]
        # A bounded tail may lack an entry. Never label an orphan close from
        # that tail as a completed parent.
        if len(raw) == LIMIT and not opens:
            continue
        latest = group[-1]
        item = {**identity(latest), **parent, "ts": parent["last_ts"]}
        opened = min((value for r in opens
                      if (value := stamp(r.get("entry_filled_at_ts") or r.get("ts")))
                      is not None), default=None)
        closed = stamp(latest.get("exit_filled_at_ts") or latest.get("ts"))
        item["hold_minutes"] = ((closed - opened) / 60
                                if opened is not None and closed is not None else None)
        item["hold_basis"] = "recorded fill or journal interval"
        item["exit_reason"] = latest.get("close_trigger") or latest.get("reason")
        parents.append(item)
    events = [identity(r) for r in sql_rows(path, "events")
              if r.get("runtime_mode") == selected["source"]]
    orders = {}
    for row in sql_rows(path, "orders"):
        if row.get("runtime_mode") != selected["source"]:
            continue
        if selected["as_of"] and (stamp(row.get("ts")) is None or
                                   stamp(row["ts"]) > stamp(selected["as_of"])):
            continue
        key = row.get("order_id") or row.get("client_order_id")
        if not key:
            continue
        previous = orders.setdefault(key, {})
        # Reconciliation appends may omit original request/decision timing.
        # Keep recorded values across those null fields, as calibration does.
        previous.update({k: v for k, v in identity(row).items() if v is not None})
    parents = [r for r in parents if matches(r, selected)]
    events = [r for r in events if matches(r, selected)]
    fields = ("parent_trade_id", "symbol", "variant_id", "candidate_id", "proof_epoch",
              "feed", "ts", "gross", "fees", "net", "r_multiple", "hold_minutes",
              "hold_basis", "exit_reason", "close_fills")
    timing_fields = ("ts", "symbol", "variant_id", "candidate_id", "proof_epoch",
                     "action", "status", "entry_quote_age_seconds", *TIMING_FIELDS)
    return {"parents": [{k: r.get(k) for k in fields} for r in parents],
            "order_timing": [{k: row.get(k) for k in timing_fields}
                             for row in orders.values() if matches(row, selected)][-100:],
            "events": [{k: r.get(k) for k in ("ts", "kind", "symbol", "variant_id", "reason")}
                       for r in events[-200:]],
            "signal_funnel": dict(Counter(r.get("kind", "unknown") for r in events)),
            "exit_distribution": dict(Counter(r.get("exit_reason") or "unknown" for r in parents)),
            "truncated": len(raw) == LIMIT,
            "path_metrics": {"available": False,
                             "reason": "runtime journal has no recorded MFE/MAE or matched-control paths"}}


def research_evidence(root, selected):
    rows = []
    edge = root / "runtime" / "research" / "edge_lab.sqlite3"
    candidates = {r["candidate_id"]: r for r in sql_rows(edge, "candidates")}
    if selected["source"] == "research":
        for row in sql_rows(edge, "evidence"):
            payload = decoded(row.get("payload_json")) or {}
            item = {**row, "variant_id": candidates.get(row.get("candidate_id"), {}).get("variant_id"),
                    "ts": row.get("created_at"), "proof_epoch": payload.get("proof_epoch"),
                    "feed": payload.get("feed"), "evidence": payload}
            if matches(item, selected):
                rows.append(item)
    else:
        for row in sql_rows(edge, "factory_accounts"):
            item = {**row, "evidence": decoded(row.get("result_json")) or {},
                    "ts": row.get("created_at"), "proof_epoch": row.get("learning_epoch")}
            if matches(item, selected):
                rows.append(item)
        # Only explicit diagnostic report locations, never recorder or secret
        # directories. Bounded files and bytes prevent a request corpus scan.
        total = 0
        for base in (root / "runtime/research/diagnostics", root / "research/results"):
            for path in sorted(base.glob("*.json"))[:64]:
                if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
                    continue
                size = path.stat().st_size
                if size > 8 * 1024**2 or total + size > 32 * 1024**2:
                    continue
                total += size
                payload = decoded(path.read_text())
                if not isinstance(payload, dict) or payload.get("diagnostic_only") is not True:
                    continue
                for family in payload.get("reports", []):
                    for variant in family.get("variants", []):
                        item = {"variant_id": variant.get("variant_id"), "family": family.get("family"),
                                "feed": payload.get("feed"), "ts": payload.get("created_at"),
                                "evidence": variant.get("diagnostic"), "artifact": str(path.relative_to(root))}
                        if matches(item, selected):
                            rows.append(item)
    return {"records": rows[-200:], "authorizing": False,
            "reason": "Recorded diagnostics and proof artifacts; this view does not authorize trading"}


def candles(root, selected):
    if not selected["symbol"] or not selected["feed"] or not selected["start_date"]:
        return {"available": False, "reason": "Choose symbol, feed and dates for bounded chart reads"}
    first = date.fromisoformat(selected["start_date"])
    last = date.fromisoformat(selected["end_date"] or selected["start_date"])
    if (last - first).days > 9:
        raise ValueError("chart range is limited to ten calendar days")
    begin = datetime.combine(first - timedelta(days=7), datetime.min.time(), NY).timestamp()
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), NY).timestamp()
    cutoff = stamp(selected["as_of"]) if selected["as_of"] else time.time()
    recorded = root / "runtime/research/recorded"
    paths = [recorded / "sessions" / f"market-{(first - timedelta(days=7) + timedelta(days=i)).isoformat()}.csv"
             for i in range((last - first).days + 8)]
    paths = [p for p in paths if p.is_file() and not p.is_symlink()]
    calendar = _partition_calendar_sidecars(paths, recorded)
    source_modes = _partition_source_sidecars(paths, recorded)
    store = recorded / "bar-observations.sqlite3"
    rows = read_bars(store, symbol=selected["symbol"], feed=selected["feed"],
                     start=begin, end=end, as_of=cutoff) if store.is_file() else []
    using_store = bool(rows)
    source = "receipt-time revision store" if rows else "first-observation CSV"
    incomplete = len(rows) == LIMIT
    if not rows:
        total = 0
        for path in paths:
            size = path.stat().st_size
            if size > 64 * 1024**2 or total + size > 128 * 1024**2:
                incomplete = True
                continue
            total += size
            with path.open(newline="") as handle:
                for row in csv.DictReader(handle):
                    if len(rows) >= LIMIT:
                        incomplete = True
                        break
                    if row.get("event_type") not in {"bar", "bar_1m"} or row.get("symbol") != selected["symbol"] or row.get("feed") != selected["feed"]:
                        continue
                    if stamp(row.get("observed_at")) is None or stamp(row["observed_at"]) > cutoff:
                        continue
                    rows.append(dict(row))
    usable = []
    for row in rows:
        item = dict(row)
        for field in ("timestamp", "as_of"):
            value = stamp(item.get(field))
            if value is not None:
                item[field] = datetime.fromtimestamp(value, timezone.utc).isoformat()
        if not _apply_calendar(item, row_number=0, calendar=calendar,
                               partition_sources=None if using_store else source_modes,
                               required=True, trusted_recorder=not using_store):
            continue
        ts = stamp(item["timestamp"])
        if ts is None or ts + 60 > cutoff or stamp(item.get("as_of")) is None or stamp(item["as_of"]) > cutoff:
            continue
        if selected["source"] in {"paper", "live"} and item.get("source_mode") != "forward_observed":
            continue
        item.update({k: number(item.get(k)) for k in ("open", "high", "low", "close", "volume")})
        if any(item[k] is None or item[k] <= 0 for k in ("open", "high", "low", "close")):
            continue
        item.update(ts=ts, timestamp=ts, session_date=datetime.fromtimestamp(ts, NY).date().isoformat())
        usable.append(item)
    usable.sort(key=lambda r: r["ts"])
    def aggregate(minutes):
        buckets = {}
        for row in usable:
            opened, closed = stamp(row["session_open"]), stamp(row["session_close"])
            width = (closed - opened) if minutes == "1d" else minutes * 60
            key = (row["provider"], row["feed"], row["source_mode"], opened,
                   int((row["ts"] - opened) // width), width)
            buckets.setdefault(key, []).append(row)
        result = []
        for key, group in sorted(buckets.items(), key=lambda x: x[1][0]["ts"]):
            opened, index, width = key[-3:]
            start = opened + index * width
            expected = [start + n * 60 for n in range(int(width / 60))]
            if [r["ts"] for r in group] != expected:
                continue
            result.append({"ts": start, "open": group[0]["open"], "high": max(r["high"] for r in group),
                           "low": min(r["low"] for r in group), "close": group[-1]["close"],
                           "volume": sum(r["volume"] or 0 for r in group), "session_date": group[0]["session_date"]})
        return result
    daily = aggregate("1d")
    prior_day = max((d for d, c in (calendar or {}).items()
                     if d < first.isoformat() and c.get("status") != "closed"), default=None)
    prior = next((r for r in daily if r["session_date"] == prior_day), None)
    series = {name: [r for r in aggregate(minutes) if first.isoformat() <= r["session_date"] <= last.isoformat()]
              for name, minutes in (("1m", 1), ("5m", 5), ("15m", 15), ("1d", "1d"))}
    return {"available": any(series.values()), "source": source, "series": series,
            "prior_session": prior, "as_of": cutoff, "incomplete_source": incomplete,
            "reason": "Only complete contiguous bars and exact recorded session boundaries are displayed"}


def workbench(root: Path, query=None):
    selected = filters(query or {})
    evidence = (runtime_evidence(root, selected) if selected["source"] in {"paper", "live"}
                else research_evidence(root, selected))
    try:
        ohlc = candles(root, selected)
    except (ValueError, OSError, sqlite3.Error) as exc:
        ohlc = {"available": False, "reason": str(exc)}
    return {"schema": SCHEMA, "filters": selected, "evidence": evidence, "candles": ohlc,
            "read_only": True, "authorizing": False,
            "bounds": {"journal_rows": LIMIT, "chart_days": 10}}
