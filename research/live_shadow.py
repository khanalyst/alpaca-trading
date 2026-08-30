"""Broker-free, incremental real-time shadow evaluation.

The shadow lane deliberately has no authority over the trading runtime.  It
reads the recorder corpus and the EdgeLedger through read-only connections,
evaluates each immutable candidate in an isolated virtual book, and writes
only to its own WAL SQLite database.  A virtual open is an observation of what
the candidate would have requested; it is never treated as a fill and no P&L
is fabricated when a safe exit cannot be reconstructed.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
import hashlib
import io
import json
from dataclasses import replace
import math
from pathlib import Path
import sqlite3
import threading
import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.ibr import generate_ibr_signal
from agent.contracts.rule import (
    CROSS_SECTIONAL_BENCHMARK, evaluate_rule_signal_trace,
    generate_rule_signal, rule_variant_id, rule_vehicle_executable,
    validate_rule_spec,
)
from agent.risk import RiskEngine
from agent.strategy import build_setup_plan
from deploy.recorder import INDEX_NAME as RECORDER_INDEX_NAME, corpus_partitions
from research.costs import (ReplayPolicy, cost_model_for_vehicle,
                             replay_policy_for_session)
from research.edge_discovery_core import _effective_ibr_config, _opportunity_rows
from research.edge_discovery_core import _null_reference_rows, null_control_account
from research.edge_lab import _null_spec
from research.factory_core import simulate_account
from research.ibr import IBRConfig, replay_ibr
from research.market_data import (
    NormalizationError,
    normalize_option_snapshot,
    normalize_quote,
    normalize_underlying_bar,
)


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
SCHEMA = "live-shadow.v1"
DEFAULT_EQUITY = 100_000.0
DEFAULT_MAX_CANDIDATES = 32
DEFAULT_MAX_EVENTS = 20_000
DEFAULT_MAX_DECISIONS = 100_000
# Candidate evaluation is CPU-heavy but deliberately bounded.  SQLite/WAL
# mutation remains parent-owned; workers only inspect the frozen snapshot.
DEFAULT_MAX_WORKERS = 4
MAX_MAX_WORKERS = 32
# Shadow replay metadata must survive the longest supported confirmatory tail
# (and enough time for an operator to diagnose/replay a delayed session).
# Keep this as the single source of truth for the library and operations CLI.
DEFAULT_RETENTION_DAYS = 180
MAX_PENDING_CORPUS_BYTES = 64 * 1024 * 1024
MAX_QUARANTINE_EVENTS = 1024
QUARANTINE_OVERFLOW_KEY = "__quarantine_overflow__"
# Replay metadata is immutable evidence; these bounded meta projections make
# an incomplete/mismatched middle session visible to operators and require an
# explicit repaired replay before ingestion may advance its boundary.
REPLAY_QUARANTINE_META_KEY = "replay_quarantine"
SESSION_CATALOG_META_KEY = "session_catalog"
MAX_REPLAY_REPAIR_HISTORY = 32
MAX_ACTIVE_REPLAY_QUARANTINE = 1024
REPLAY_QUARANTINE_OVERFLOW_KEY = "__replay_quarantine_overflow__"
SHADOW_MANIFEST_META_PREFIX = "shadow-manifest.v1:"
SHADOW_MANIFEST_LATEST_KEY = "shadow-manifest.v1:latest"


class ShadowError(RuntimeError):
    """Base error for a shadow run."""


class InputConflict(ShadowError):
    """The same recorder event key was observed with different content."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _validated_shadow_manifest(
        value: Any, *, requested_digest: str | None = None) -> dict[str, Any]:
    """Validate one persisted manifest before exposing it to callers.

    Manifest rows are immutable evidence, so a syntactically valid JSON value
    is not sufficient: the body must reproduce its stored content digest.  A
    digest-addressed lookup also verifies that the requested key names that
    same digest.  The latest pointer has no digest-bearing key of its own, but
    still goes through the body/self-digest check.
    """
    if not isinstance(value, Mapping) or value.get("schema") != "shadow-manifest.v1":
        raise ShadowError("shadow manifest metadata is invalid")
    stored_digest = value.get("manifest_digest")
    body = dict(value)
    body.pop("manifest_digest", None)
    computed_digest = _digest(body)
    if not isinstance(stored_digest, str) or stored_digest != computed_digest:
        raise ShadowError("shadow manifest metadata digest mismatch")
    if requested_digest is not None and stored_digest != requested_digest:
        raise ShadowError("shadow manifest metadata key mismatch")
    return dict(value)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _availability_time(row: Mapping[str, Any]) -> datetime | None:
    """Return when a recorded event was actually usable by the strategy.

    Provider event time, provider as-of time, and local observation time are
    separate point-in-time constraints.  The event is not available until all
    three have occurred.
    """
    timestamp = _timestamp(row.get("timestamp"))
    if timestamp is None:
        return None
    as_of = _timestamp(row.get("as_of") or row.get("timestamp"))
    observed = _timestamp(row.get("observed_at") or row.get("as_of") or
                          row.get("timestamp"))
    if as_of is None or observed is None:
        return None
    return max(timestamp, as_of, observed)


def _row_visible(row: Mapping[str, Any], at: datetime) -> bool:
    available = _availability_time(row)
    return available is not None and available <= at


def _recorded_session_bounds(corpus_path: Path, session: str) -> tuple[datetime, datetime] | None:
    """Read the recorder's Alpaca-calendar close for one session.

    The sidecar is recorder-owned and rewritten atomically.  Missing legacy
    calendar metadata deliberately falls back to the regular close in replay;
    newly recorded early closes use the exact broker calendar boundary.
    """
    index_path = corpus_path.parent / RECORDER_INDEX_NAME
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    calendar = payload.get("session_calendar") if isinstance(payload, Mapping) else None
    value = calendar.get(session) if isinstance(calendar, Mapping) else None
    if not isinstance(value, Mapping):
        return None
    opened = _timestamp(value.get("open"))
    closed = _timestamp(value.get("close"))
    if (opened is None or closed is None or opened >= closed or
            opened.astimezone(NEW_YORK).date().isoformat() != session or
            closed.astimezone(NEW_YORK).date().isoformat() != session):
        return None
    return opened, closed


def _recorded_session_calendar(corpus_path: Path) -> dict[str, tuple[datetime, datetime]]:
    """Return validated, already-closed sessions from the recorder sidecar."""
    index_path = corpus_path.parent / RECORDER_INDEX_NAME
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    calendar = payload.get("session_calendar") if isinstance(payload, Mapping) else None
    if not isinstance(calendar, Mapping):
        return {}
    now = datetime.now(UTC)
    result: dict[str, tuple[datetime, datetime]] = {}
    for session in calendar:
        day = str(session)
        bounds = _recorded_session_bounds(corpus_path, day)
        if bounds is not None and bounds[1] <= now:
            result[day] = bounds
    return result


def _recorded_session_close(corpus_path: Path, session: str) -> datetime | None:
    bounds = _recorded_session_bounds(corpus_path, session)
    return bounds[1] if bounds is not None else None


def _session_close(corpus_path: Path, session: str, *,
                   require_exact_calendar: bool = False) -> tuple[datetime | None, str]:
    """Resolve the exact close, retaining an explicit legacy fallback label."""
    recorded = _recorded_session_close(corpus_path, session)
    if recorded is not None:
        return recorded, "recorder_alpaca_calendar"
    if require_exact_calendar:
        return None, "exact_calendar_metadata_missing"
    try:
        local_day = date.fromisoformat(session)
    except ValueError:
        return None, "invalid_session"
    return (datetime.combine(local_day, dt_time(16, 0), NEW_YORK).astimezone(UTC),
            "regular_close_fallback")


def _event_end(row: Mapping[str, Any]) -> datetime | None:
    as_of = _timestamp(row.get("as_of"))
    if as_of is not None:
        return as_of
    stamp = _timestamp(row.get("timestamp"))
    return stamp + timedelta(minutes=1) if stamp is not None else None


def _session_policy(config: Mapping[str, Any], close_at: datetime | None) -> ReplayPolicy:
    """Apply runtime close-relative entry and force-flat cutoffs to replay."""
    policy = _policy(config)
    if close_at is None:
        return policy
    local_close = close_at.astimezone(NEW_YORK)
    # The close-relative helper is shared with factory and IBR replay.  The
    # synthetic open is only used to validate the NY session date here.
    local_open = datetime.combine(local_close.date(), dt_time(9, 30),
                                  tzinfo=NEW_YORK).astimezone(UTC)
    return replay_policy_for_session(
        policy, session_open=local_open, session_close=close_at,
        session_date=local_close.date())


def _option_snapshot_index(values: Sequence[Any]) -> dict[datetime, Any]:
    """Preserve every option snapshot even when contracts share a timestamp."""
    origin = datetime(1970, 1, 1, tzinfo=UTC)
    return {origin + timedelta(microseconds=index): value
            for index, value in enumerate(values)}


def _plain(value: Any) -> Any:
    """Convert normalized dataclasses into finite JSON-safe mappings."""
    if dataclasses.is_dataclass(value):
        return _plain(dataclasses.asdict(value))
    if not isinstance(value, type) and hasattr(value, "__dict__"):
        return _plain(vars(value))
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _iso_time(value: Any) -> str | None:
    parsed = _timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def _number_or_none(value: Any) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, 10)


def _canonical_equity_feed(value: Any) -> str | None:
    feed = str(value or "").strip().lower().replace("-", "_")
    if feed == "delayed":
        feed = "delayed_sip"
    return feed if feed in {"iex", "sip", "delayed_sip"} else None


def _opportunity_capacity(rows: Sequence[Mapping[str, Any]], *,
                          vehicle: str | None = None,
                          min_trades: int | None = None,
                          min_sessions: int | None = None) -> dict[str, Any]:
    """Summarize the complete symbol/session opportunity denominator.

    Replay/account rows deliberately materialize ``no_trade`` opportunities.
    This diagnostic must therefore operate on the raw rows, before the gate's
    authorizing projection removes refusals.  One symbol/session is one
    opportunity even if a malformed retry supplied duplicate rows; an
    executed row wins over a duplicate refusal for the observed count.
    """
    selected: dict[str, dict[str, Any]] = {}
    refusal_reasons: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if vehicle is not None and str(row.get("vehicle") or vehicle) != str(vehicle):
            continue
        symbol = str(row.get("symbol") or "").upper()
        session = str(row.get("session_date") or "")
        opportunity = str(row.get("opportunity_id") or "")
        # Capacity is explicitly symbol×session.  Prefer that stable pair so
        # a malformed/reused opportunity id cannot collapse two sessions.
        key = (f"{symbol}:{session}" if symbol and session else opportunity)
        if not key:
            # Keep malformed rows visible in the refusal denominator without
            # allowing an unbounded arbitrary payload to become a key.
            key = f"__row_{len(selected)}"
        executed = row.get("no_trade") is not True
        item = selected.get(key)
        if item is None or (executed and not item["executed"]):
            selected[key] = {"executed": executed, "session": session}
        if not executed:
            reason = str(row.get("reject_reason") or "unspecified")[:120]
            refusal_reasons[reason] = refusal_reasons.get(reason, 0) + 1
    opportunity_count = len(selected)
    observed_trades = sum(1 for item in selected.values() if item["executed"])
    observed_sessions = len({item["session"] for item in selected.values()
                             if item["executed"] and item["session"]})
    opportunity_sessions = len({item["session"] for item in selected.values()
                                if item["session"]})
    floor_trades = (None if min_trades is None else max(0, int(min_trades)))
    floor_sessions = (None if min_sessions is None else max(0, int(min_sessions)))
    required_rate = (None if floor_trades is None or opportunity_count <= 0
                     else floor_trades / opportunity_count)
    observed_rate = (observed_trades / opportunity_count
                     if opportunity_count else 0.0)
    capacity_feasible = bool(
        (floor_trades is None or opportunity_count >= floor_trades) and
        (floor_sessions is None or opportunity_sessions >= floor_sessions))
    observed_feasible = bool(
        (floor_trades is None or observed_trades >= floor_trades) and
        (floor_sessions is None or observed_sessions >= floor_sessions))
    if not capacity_feasible:
        status = "structurally_impossible"
        reason = "opportunity capacity cannot satisfy configured floor"
    elif not observed_feasible:
        status = "underpowered_observed"
        reason = "observed executed trades are below configured floor"
    else:
        status = "feasible"
        reason = "opportunity capacity satisfies configured floor"
    return {
        "observed_trades": observed_trades,
        "observed_sessions": observed_sessions,
        "opportunity_count": opportunity_count,
        "max_trade_opportunities": opportunity_count,
        "opportunity_sessions": opportunity_sessions,
        "observed_trade_rate": observed_rate,
        "required_trade_rate": required_rate,
        "required_rate_for_floor": required_rate,
        # ``feasible`` is the end-to-end observed floor result.  Keep the
        # structural capacity result separate so selective low-rate lanes are
        # distinguishable from a corpus that cannot possibly supply enough
        # symbol/session opportunities.
        "feasible": bool(capacity_feasible and observed_feasible),
        "capacity_feasible": capacity_feasible,
        "observed_feasible": observed_feasible,
        "status": status,
        "reason": reason,
        "shortfalls": {
            "trades": max(0, (floor_trades or 0) - observed_trades),
            "opportunities": max(0, (floor_trades or 0) - opportunity_count),
            "sessions": max(0, (floor_sessions or 0) - opportunity_sessions),
        },
        "refusal_reason_counts": dict(sorted(refusal_reasons.items())[:64]),
    }


def _shadow_signature(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a runtime shadow open into the replay comparison contract."""
    if row.get("kind") != "open_incomplete":
        return None
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    signal = payload.get("signal") if isinstance(payload.get("signal"), Mapping) else {}
    plan = payload.get("setup_plan") if isinstance(payload.get("setup_plan"), Mapping) else {}
    risk_plan = payload.get("risk_plan") if isinstance(payload.get("risk_plan"), Mapping) else {}
    execution_profile = str(plan.get("execution_profile") or
                            risk_plan.get("execution_profile") or "shares").lower()
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), Mapping) else {}
    equity_feed = _canonical_equity_feed(
        payload.get("equity_feed") or plan.get("equity_feed") or
        snapshot.get("equity_feed"))
    signal_ts = _finite(plan.get("signal_ts", signal.get("signal_ts")))
    decision_ts = plan.get("decision_timestamp", signal.get("decision_timestamp"))
    entry_ts = plan.get("entry_timestamp", signal.get("entry_timestamp"))
    if entry_ts is None and decision_ts is not None:
        entry_ts = decision_ts
    if entry_ts is None and signal_ts is not None:
        # Legacy shadow payloads predate explicit causal timestamps.
        entry_ts = (datetime.fromtimestamp(signal_ts, UTC) +
                    timedelta(seconds=60)).isoformat()
    return {
        "symbol": str(row.get("symbol") or plan.get("symbol") or signal.get("symbol") or ""),
        "session_date": str(row.get("session_date") or plan.get("session") or signal.get("session") or ""),
        "direction": str(plan.get("direction") or signal.get("direction") or ""),
        "setup_type": str(plan.get("setup_type") or signal.get("setup_type") or ""),
        "signal_ts": _iso_time(datetime.fromtimestamp(signal_ts, UTC).isoformat()) if signal_ts is not None else None,
        "decision_ts": _iso_time(decision_ts),
        "entry_ts": _iso_time(entry_ts),
        "stop_price": _number_or_none(plan.get("stop_price", signal.get("stop_price"))),
        "target_price": _number_or_none(plan.get("target_price", signal.get("target_price"))),
        "stop_distance": _number_or_none(plan.get("stop_distance", signal.get("stop_distance"))),
        "range_high": _number_or_none(plan.get("range_high", signal.get("range_high"))),
        "range_low": _number_or_none(plan.get("range_low", signal.get("range_low"))),
        "target_r": _number_or_none(plan.get("target_r", signal.get("target_r"))),
        "vehicle": ("option" if execution_profile
                     in {"option", "options"} else "equity"),
        "profile": execution_profile,
        "equity_feed": equity_feed,
    }


def _replay_signature(row: Mapping[str, Any], *, vehicle: str,
                      strategy_id: str, target_r: float | None = None,
                      setup_type: str | None = None,
                      equity_feed: str | None = None) -> dict[str, Any] | None:
    """Project a factory/IBR replay trade into the same semantic contract."""
    if row.get("no_trade") is True:
        return None
    signal_ts = row.get("signal_timestamp")
    # Legacy rows have no causal decision field; preserve that absence so
    # their stored signatures remain comparable.  New factory/IBR rows carry
    # the explicit decision timestamp.
    decision_ts = row.get("decision_timestamp")
    entry_ts = row.get("entry_timestamp")
    direction = str(row.get("direction") or "")
    stop = _number_or_none(row.get("stop_price"))
    target = _number_or_none(row.get("target_price"))
    distance = _number_or_none(row.get("stop_distance"))
    if distance is None and stop is not None and row.get("entry_reference") is not None:
        reference = _finite(row.get("entry_reference"))
        distance = _number_or_none(abs(reference - stop)) if reference is not None else None
    resolved_target_r = target_r
    if resolved_target_r is None and stop is not None and target is not None and distance:
        resolved_target_r = abs(target - stop) / distance - 1.0
    setup_type = ("ibr_breakout" if strategy_id == "ibr" else
                  str(setup_type or row.get("setup_type") or "rule_signal"))
    return {
        "symbol": str(row.get("symbol") or ""),
        "session_date": str(row.get("session_date") or ""),
        "direction": direction,
        "setup_type": setup_type,
        "signal_ts": _iso_time(signal_ts),
        "decision_ts": _iso_time(decision_ts),
        "entry_ts": _iso_time(entry_ts),
        "stop_price": stop,
        "target_price": target,
        "stop_distance": distance,
        "range_high": _number_or_none(row.get("range_high")),
        "range_low": _number_or_none(row.get("range_low")),
        "target_r": _number_or_none(resolved_target_r),
        "vehicle": "option" if vehicle in {"option", "options"} else "equity",
        "profile": "options" if vehicle in {"option", "options"} else "shares",
        "equity_feed": _canonical_equity_feed(equity_feed),
    }


def _signature_diffs(expected: Sequence[Mapping[str, Any]],
                    observed: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic, field-level differences between semantic rows."""
    key_fields = ("symbol", "session_date", "direction", "setup_type")
    compare_fields = ("signal_ts", "decision_ts", "entry_ts", "stop_price", "target_price",
                      "stop_distance", "range_high", "range_low", "target_r",
                      "vehicle", "profile", "equity_feed")
    left = sorted((dict(item) for item in expected),
                  key=lambda item: tuple(str(item.get(key) or "") for key in key_fields))
    right = sorted((dict(item) for item in observed),
                   key=lambda item: tuple(str(item.get(key) or "") for key in key_fields))
    differences: list[dict[str, Any]] = []
    for index in range(max(len(left), len(right))):
        before = left[index] if index < len(left) else None
        after = right[index] if index < len(right) else None
        if before is None or after is None:
            differences.append({"index": index, "kind": "missing" if after is None else "extra",
                                "expected": before, "observed": after})
            continue
        for field in key_fields + compare_fields:
            if before.get(field) != after.get(field):
                differences.append({"index": index, "field": field,
                                    "expected": before.get(field),
                                    "observed": after.get(field)})
    return differences


def _row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    # Empty CSV cells are absent from normalized payloads.  Keeping the raw
    # key set out of the contract also makes equivalent CSV exports hash alike.
    return {str(key): value for key, value in row.items()
            if value is not None and str(value) != ""}


def _normalize_row(row: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
    raw = _row_payload(row)
    event_type = str(raw.get("event_type") or "").strip().lower()
    provider = str(raw.get("provider") or "recorder")
    feed = str(raw.get("feed") or "recorded")
    if event_type in {"bar", "bar_1m"}:
        event = normalize_underlying_bar(raw, provider=provider, feed=feed)
    elif event_type == "quote":
        event = normalize_quote(raw, provider=provider, feed=feed)
    elif event_type in {"option", "option_snapshot"}:
        event = normalize_option_snapshot(raw, provider=provider, feed=feed)
    else:
        raise NormalizationError(f"unsupported recorder event_type {event_type!r}")
    return raw, event


def _corpus_sources(path: Path) -> list[Path]:
    sources = [path] if path.is_file() and path.stat().st_size else []
    sources.extend(corpus_partitions(path))
    return sources


def _read_corpus_append(source: Path, offset: int) -> tuple[list[dict], int]:
    """Read only complete CSV rows appended after a durable byte offset."""
    size = source.stat().st_size
    if size < offset:
        raise ShadowError(f"shadow corpus source shrank: {source}")
    if size == offset:
        return [], offset
    with source.open("rb") as handle:
        header = handle.readline().decode("utf-8")
        try:
            fieldnames = next(csv.reader([header]))
        except (csv.Error, StopIteration) as exc:
            raise ShadowError(f"shadow corpus source has invalid header: {source}") from exc
        handle.seek(offset)
        payload = handle.read(size - offset)
    if payload and not payload.endswith(b"\n"):
        boundary = payload.rfind(b"\n")
        if boundary < 0:
            return [], offset
        payload = payload[:boundary + 1]
    consumed = offset + len(payload)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShadowError(f"shadow corpus source is not UTF-8: {source}") from exc
    if offset == 0:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fields = set(reader.fieldnames or ())
        required = {"event_key", "event_type", "symbol", "timestamp"}
        if not required.issubset(fields):
            raise ShadowError(f"shadow corpus source has invalid header: {source}")
    else:
        reader = csv.DictReader(io.StringIO(text, newline=""), fieldnames=fieldnames)
    rows = []
    for row in reader:
        if None in row:
            raise ShadowError(f"shadow corpus source has malformed CSV: {source}")
        rows.append(row)
    return rows, consumed


def _compact_shadow_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Keep all bars/options and every quote needed at a decision boundary.

    First/last minute quotes retain the open/close path while the exact latest
    visible quote at each recorded bar availability time preserves delayed
    after-close decisions.  This remains bounded without replacing an entry
    boundary quote with a later quote from the same minute.
    """
    retained: list[dict] = []
    quote_rows: dict[str, list[tuple[datetime, datetime, dict]]] = {}
    cutoffs: dict[str, list[datetime]] = {}
    for raw in rows:
        row = dict(raw)
        event_type = str(row.get("event_type") or "").strip().lower()
        if event_type != "quote":
            retained.append(row)
            if event_type in {"bar", "bar_1m"}:
                available = _availability_time(row)
                if available is not None:
                    cutoffs.setdefault(str(row.get("symbol") or ""), []).append(available)
            continue
        stamp = _timestamp(row.get("timestamp"))
        available = _availability_time(row)
        if stamp is None or available is None:
            retained.append(row)  # normal validation emits the hard failure
            continue
        quote_rows.setdefault(str(row.get("symbol") or ""), []).append(
            (available, stamp, row))

    selected: dict[str, dict] = {}
    for symbol, values in quote_rows.items():
        values.sort(key=lambda item: (item[1], str(item[2].get("event_key") or "")))
        by_minute: dict[str, list[tuple[datetime, datetime, dict]]] = {}
        for item in values:
            by_minute.setdefault(item[1].replace(second=0, microsecond=0).isoformat(), []).append(item)
        for minute_values in by_minute.values():
            for item in (minute_values[0], minute_values[-1]):
                row = item[2]
                selected[str(row.get("event_key") or _digest(row))] = row

        available_values = sorted(
            values, key=lambda item: (item[0], item[1],
                                      str(item[2].get("event_key") or "")))
        cursor = 0
        latest: dict | None = None
        for cutoff in sorted(set(cutoffs.get(symbol, ()))):
            while cursor < len(available_values) and available_values[cursor][0] <= cutoff:
                latest = available_values[cursor][2]
                cursor += 1
            if latest is not None:
                selected[str(latest.get("event_key") or _digest(latest))] = latest

    retained.extend(selected.values())
    retained.sort(key=lambda row: (str(row.get("timestamp") or ""),
                                   str(row.get("event_key") or "")))
    return retained


@dataclass(frozen=True)
class ShadowConfig:
    """Bounded paths and resource limits for one shadow process."""

    corpus_path: Path
    edge_db: Path
    shadow_db: Path
    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_events: int = DEFAULT_MAX_EVENTS
    max_decisions: int = DEFAULT_MAX_DECISIONS
    max_workers: int = DEFAULT_MAX_WORKERS
    retention_days: int = DEFAULT_RETENTION_DAYS
    equity: float = DEFAULT_EQUITY
    poll_seconds: float = 60.0

    def __post_init__(self) -> None:
        for name in ("max_candidates", "max_events", "max_decisions", "max_workers",
                     "retention_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) != value or int(value) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if int(self.max_workers) > MAX_MAX_WORKERS:
            raise ValueError(f"max_workers must be <= {MAX_MAX_WORKERS}")
        if _finite(self.equity) is None or float(self.equity) <= 0:
            raise ValueError("equity must be positive and finite")


class ShadowStore:
    """Own the isolated append-only shadow database."""

    def __init__(self, path: str | Path, *, retention_days: int = DEFAULT_RETENTION_DAYS,
                 readonly: bool = False):
        self.path = Path(path)
        self.readonly = bool(readonly)
        if not self.readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = int(retention_days)
        if not self.readonly:
            self._init()

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            if not self.path.is_file():
                raise ShadowError(f"shadow database is unavailable: {self.path}")
            uri = f"file:{self.path.resolve()}?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        if not self.readonly:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            yield db
            if not self.readonly:
                db.commit()
        finally:
            db.close()

    def _init(self) -> None:
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cursor (
                    scope TEXT PRIMARY KEY, last_event_key TEXT NOT NULL,
                    last_timestamp TEXT NOT NULL, last_digest TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_key TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    event_json TEXT NOT NULL, event_type TEXT NOT NULL,
                    symbol TEXT NOT NULL, timestamp TEXT NOT NULL,
                    as_of TEXT, inserted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY, variant_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL, vehicle TEXT NOT NULL,
                    status TEXT NOT NULL, config_json TEXT NOT NULL,
                    proof_json TEXT NOT NULL, observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    event_key TEXT NOT NULL, session_date TEXT NOT NULL,
                    symbol TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT,
                    payload_json TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(candidate_id, event_key)
                );
                CREATE TABLE IF NOT EXISTS virtual_books (
                    book_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL UNIQUE, symbol TEXT NOT NULL,
                    status TEXT NOT NULL, quantity REAL, entry_price REAL,
                    plan_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS replay_diffs (
                    diff_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    session_date TEXT NOT NULL, source_digest TEXT NOT NULL,
                    shadow_digest TEXT NOT NULL, replay_digest TEXT,
                    status TEXT NOT NULL, details_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(candidate_id, session_date)
                );
                CREATE TABLE IF NOT EXISTS shadow_accounts (
                    account_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    session_date TEXT NOT NULL, replay_digest TEXT NOT NULL,
                    vehicle TEXT NOT NULL, starting_cash REAL NOT NULL,
                    ending_cash REAL NOT NULL, realized_pnl REAL NOT NULL,
                    trade_count INTEGER NOT NULL, replay_status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(candidate_id, session_date, replay_digest)
                );
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    trade_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,
                    session_date TEXT NOT NULL, replay_digest TEXT NOT NULL,
                    replay_status TEXT NOT NULL, trade_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                  BEGIN SELECT RAISE(ABORT, 'shadow events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                  BEGIN SELECT RAISE(ABORT, 'shadow events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS candidates_no_update BEFORE UPDATE ON candidates
                  BEGIN SELECT RAISE(ABORT, 'shadow candidates are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS candidates_no_delete BEFORE DELETE ON candidates
                  BEGIN SELECT RAISE(ABORT, 'shadow candidates are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
                  BEGIN SELECT RAISE(ABORT, 'shadow decisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
                  BEGIN SELECT RAISE(ABORT, 'shadow decisions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_accounts_no_update BEFORE UPDATE ON shadow_accounts
                  BEGIN SELECT RAISE(ABORT, 'shadow accounts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_accounts_no_delete BEFORE DELETE ON shadow_accounts
                  BEGIN SELECT RAISE(ABORT, 'shadow accounts are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_trades_no_update BEFORE UPDATE ON shadow_trades
                  BEGIN SELECT RAISE(ABORT, 'shadow trades are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS shadow_trades_no_delete BEFORE DELETE ON shadow_trades
                  BEGIN SELECT RAISE(ABORT, 'shadow trades are immutable'); END;
            """)

    def ingest_event(self, row: Mapping[str, Any], *, max_events: int) -> tuple[str, bool]:
        event_key = str(row.get("event_key") or "").strip()
        if not event_key:
            raise ShadowError("recorder row has no event_key")
        # The corpus remains a streamed source, but known rows do not need to
        # be normalized again.  We still hash every row before skipping it so
        # a changed payload for an old key is always a hard conflict.
        payload = _row_payload(row)
        digest = _digest(payload)
        with self._connection() as db:
            existing = db.execute("SELECT digest FROM events WHERE event_key=?",
                                  (event_key,)).fetchone()
            if existing is not None:
                if existing["digest"] != digest:
                    raise InputConflict(f"event_key {event_key} changed content")
                return event_key, False
        payload, event = _normalize_row(row)
        timestamp = _timestamp(payload.get("timestamp"))
        as_of = _timestamp(payload.get("as_of") or payload.get("timestamp"))
        if timestamp is None:
            raise ShadowError(f"event {event_key} has invalid timestamp")
        symbol = str(payload.get("symbol") or getattr(event, "symbol", ""))
        event_type = str(payload.get("event_type") or "").lower()
        with self._connection() as db:
            db.execute("""INSERT INTO events
                (event_key,digest,event_json,event_type,symbol,timestamp,as_of,inserted_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (event_key, digest, _json(payload), event_type, symbol,
                 timestamp.isoformat(), as_of.isoformat() if as_of else None,
                 time.time()))
            cursor = db.execute("SELECT last_timestamp,last_event_key FROM cursor WHERE scope='corpus'").fetchone()
            if cursor is None or (timestamp.isoformat(), event_key) >= (cursor[0], cursor[1]):
                db.execute("""INSERT INTO cursor(scope,last_event_key,last_timestamp,last_digest,updated_at)
                    VALUES('corpus',?,?,?,?)
                    ON CONFLICT(scope) DO UPDATE SET last_event_key=excluded.last_event_key,
                        last_timestamp=excluded.last_timestamp,last_digest=excluded.last_digest,
                        updated_at=excluded.updated_at""",
                    (event_key, timestamp.isoformat(), digest, time.time()))
        return event_key, True

    def event_count(self) -> int:
        with self._connection() as db:
            return int(db.execute("SELECT count(*) FROM events").fetchone()[0])

    def source_offsets(self) -> dict[str, int] | None:
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='corpus_source_offsets'").fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow corpus offsets are invalid") from exc
        if not isinstance(value, dict) or any(
                not isinstance(key, str) or isinstance(offset, bool) or
                not isinstance(offset, int) or offset < 0
                for key, offset in value.items()):
            raise ShadowError("shadow corpus offsets are invalid")
        return value

    def save_source_offsets(self, offsets: Mapping[str, int]) -> None:
        payload = _json({str(key): int(value) for key, value in offsets.items()})
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES('corpus_source_offsets',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (payload,))

    def save_manifest(self, manifest: Mapping[str, Any]) -> str:
        """Persist one immutable content-addressed poll manifest.

        The manifest is written by the parent after ingestion and before any
        worker starts.  Its digest is derived from the manifest body (not
        wall-clock metadata), so retries over the same candidate/event
        snapshot address the same evidence.  A mutable latest pointer is only
        an operational convenience; the digest-keyed copy is the audit source.
        """
        if self.readonly:
            raise ShadowError("cannot update manifest on a read-only WAL")
        body = dict(manifest)
        body.pop("manifest_digest", None)
        body.setdefault("schema", "shadow-manifest.v1")
        digest = _digest(body)
        encoded = _json({**body, "manifest_digest": digest})
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO NOTHING""",
                       (f"{SHADOW_MANIFEST_META_PREFIX}{digest}", encoded))
            db.execute("""INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (SHADOW_MANIFEST_LATEST_KEY, encoded))
        return digest

    def manifest(self, digest: str | None = None) -> dict[str, Any] | None:
        """Read a digest-addressed manifest, or the latest poll manifest."""
        requested_digest = None if digest is None else str(digest)
        key = SHADOW_MANIFEST_LATEST_KEY if requested_digest is None else (
            f"{SHADOW_MANIFEST_META_PREFIX}{requested_digest}")
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow manifest metadata is invalid") from exc
        return _validated_shadow_manifest(value, requested_digest=requested_digest)

    def forward_event_floor(self) -> float | None:
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='forward_event_floor'").fetchone()
        if row is None:
            return None
        try:
            value = float(row["value"])
        except (TypeError, ValueError) as exc:
            raise ShadowError("shadow forward event floor is invalid") from exc
        if not math.isfinite(value) or value < 0:
            raise ShadowError("shadow forward event floor is invalid")
        return value

    def save_forward_event_floor(self, value: float) -> None:
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES('forward_event_floor',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (str(float(value)),))

    def quarantine_through_session(self) -> str | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='quarantine_through_session'").fetchone()
        return None if row is None else str(row["value"])

    def save_quarantine_through_session(self, value: str) -> None:
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value)
                VALUES('quarantine_through_session',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (value,))

    def quarantine_events(self) -> dict[str, dict[str, Any]]:
        """Return durable malformed-event diagnostics awaiting corrected replay."""
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='quarantine_events'").fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow quarantine metadata is invalid") from exc
        if not isinstance(value, Mapping):
            raise ShadowError("shadow quarantine metadata is invalid")
        result: dict[str, dict[str, Any]] = {}
        for key, detail in value.items():
            if not isinstance(key, str) or not isinstance(detail, Mapping):
                raise ShadowError("shadow quarantine metadata is invalid")
            result[key] = dict(detail)
        return result

    def save_quarantine_events(self, value: Mapping[str, Mapping[str, Any]], *,
                               replace: bool = False) -> None:
        """Persist bounded malformed-event diagnostics as mutable metadata.

        This metadata is not authorizing evidence.  It records why a source
        offset remains uncommitted and is removed only when the exact event
        key successfully normalizes on a later corrected replay.
        """
        prior = {} if replace else self.quarantine_events()
        encoded = {**prior, **{str(key): dict(detail) for key, detail in value.items()}}
        if len(encoded) > MAX_QUARANTINE_EVENTS:
            # Do not silently discard malformed evidence when the diagnostic
            # bound is reached. Retain a deterministic prefix plus an
            # unknown-tail sentinel; its missing session identity forces all
            # replay/gate consumers closed until an operator repairs the
            # source and explicitly clears the quarantine metadata.
            existing_overflow = encoded.get(QUARANTINE_OVERFLOW_KEY, {})
            dropped = int(existing_overflow.get("dropped_events", 0) or 0)
            keys = sorted(key for key in encoded
                          if key != QUARANTINE_OVERFLOW_KEY)
            dropped += max(0, len(keys) - (MAX_QUARANTINE_EVENTS - 1))
            encoded = {key: encoded[key]
                       for key in keys[:MAX_QUARANTINE_EVENTS - 1]}
            encoded[QUARANTINE_OVERFLOW_KEY] = {
                "event_key": QUARANTINE_OVERFLOW_KEY,
                "session_date": None,
                "reason": "quarantine_overflow",
                "dropped_events": dropped,
                "unknown_tail": True,
            }
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES('quarantine_events',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (_json(encoded),))

    def session_catalog(self) -> dict[str, dict[str, Any]]:
        """Return recorder-calendar session provenance seen by ShadowRunner."""
        with self._connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?",
                             (SESSION_CATALOG_META_KEY,)).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow session catalog metadata is invalid") from exc
        if not isinstance(value, Mapping):
            raise ShadowError("shadow session catalog metadata is invalid")
        result: dict[str, dict[str, Any]] = {}
        for session, detail in value.items():
            if not isinstance(session, str) or not isinstance(detail, Mapping):
                raise ShadowError("shadow session catalog metadata is invalid")
            result[session] = dict(detail)
        return result

    def save_session_catalog(self, value: Mapping[str, Mapping[str, Any]]) -> None:
        """Persist bounded exact-calendar session provenance.

        Only entries sourced from the recorder's Alpaca calendar may be used
        to detect an all-arm gap.  Timestamp-derived weekdays are deliberately
        not promoted to this catalog because holidays and early closes must
        remain an external authority.
        """
        if self.readonly:
            raise ShadowError("cannot update session catalog on a read-only WAL")
        incoming = {str(key): dict(detail) for key, detail in value.items()}
        existing = self.session_catalog()
        for key, detail in incoming.items():
            prior = existing.get(key)
            if prior is not None and _digest(prior) != _digest(detail):
                raise ShadowError(f"session catalog provenance conflicts for {key}")
        # The catalog is monotonic: a correction can add a session, but cannot
        # erase or replace exchange-calendar authority already observed.
        encoded = {**existing, **incoming}
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (SESSION_CATALOG_META_KEY, _json(encoded)))

    def record_session_calendar(self, session_date: str, *, opened: str,
                                closed: str, source: str) -> None:
        if self.readonly:
            raise ShadowError("cannot update session catalog on a read-only WAL")
        catalog = self.session_catalog()
        existing = catalog.get(str(session_date))
        if existing is not None:
            expected = {"session_date": str(session_date), "open": str(opened),
                        "close": str(closed), "source": str(source)}
            if any(str(existing.get(key)) != value for key, value in expected.items()):
                raise ShadowError(f"session catalog provenance conflicts for {session_date}")
            return
        catalog[str(session_date)] = {
            "session_date": str(session_date), "open": str(opened),
            "close": str(closed), "source": str(source),
            "recorded_ts": time.time(),
        }
        self.save_session_catalog(catalog)

    def replay_quarantine(self) -> dict[str, dict[str, Any]]:
        """Return durable replay repair/quarantine state.

        A replay that is incomplete or semantically mismatched is never
        silently treated as a missing row.  The shadow worker records a
        bounded diagnostic entry keyed by candidate/session; a later complete
        parity replay changes that same entry to ``repaired`` and retains the
        prior digests for audit.  This projection is also readable from the
        ingest-only process (which opens the WAL read-only).
        """
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key=?",
                (REPLAY_QUARANTINE_META_KEY,)).fetchone()
        if row is None:
            return {}
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow replay quarantine metadata is invalid") from exc
        if not isinstance(value, Mapping):
            raise ShadowError("shadow replay quarantine metadata is invalid")
        result: dict[str, dict[str, Any]] = {}
        for key, detail in value.items():
            if not isinstance(key, str) or not isinstance(detail, Mapping):
                raise ShadowError("shadow replay quarantine metadata is invalid")
            result[key] = dict(detail)
        return result

    def _save_replay_quarantine(self, value: Mapping[str, Mapping[str, Any]]) -> None:
        # Keep every unresolved entry (they are the repair boundary), while
        # bounding retained repaired history.  If the active set exceeds the
        # explicit safety limit, persist a visible overflow sentinel with a
        # deterministic digest/count; ingestion treats it as a global block.
        active = {
            str(key): dict(detail) for key, detail in value.items()
            if str(key) != REPLAY_QUARANTINE_OVERFLOW_KEY
            and str((detail or {}).get("status") or "") != "repaired"
        }
        repaired = [
            (str(key), dict(detail)) for key, detail in value.items()
            if str(key) != REPLAY_QUARANTINE_OVERFLOW_KEY
            and str((detail or {}).get("status") or "") == "repaired"
        ]
        repaired.sort(key=lambda item: (
            float(item[1].get("repaired_ts", 0.0) or 0.0), item[0]))
        encoded: dict[str, dict[str, Any]] = dict(active)
        encoded.update(dict(repaired[-MAX_REPLAY_REPAIR_HISTORY:]))
        if len(active) > MAX_ACTIVE_REPLAY_QUARANTINE:
            active_keys = sorted(active)
            encoded[REPLAY_QUARANTINE_OVERFLOW_KEY] = {
                "schema": "shadow-replay-repair.v1",
                "candidate_id": None,
                "session_date": None,
                "status": "overflow",
                "reason": "active replay quarantine exceeds safety bound",
                "max_active": MAX_ACTIVE_REPLAY_QUARANTINE,
                "active_count": len(active),
                "active_digest": _digest(active_keys),
                "unknown_tail": True,
                "updated_ts": time.time(),
            }
        with self._connection() as db:
            db.execute("""INSERT INTO meta(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                       (REPLAY_QUARANTINE_META_KEY, _json(encoded)))

    @staticmethod
    def _replay_quarantine_key(candidate_id: str, session_date: str) -> str:
        return f"{candidate_id}:{session_date}"

    def quarantine_replay_session(self, *, candidate_id: str,
                                  session_date: str, reason: str,
                                  status: str, source_digest: str | None = None,
                                  shadow_digest: str | None = None,
                                  replay_digest: str | None = None) -> dict[str, Any]:
        """Record a blocked replay session without deleting any evidence."""
        if self.readonly:
            raise ShadowError("cannot update replay quarantine on a read-only WAL")
        if status not in {"incomplete", "mismatch"}:
            raise ValueError("replay quarantine status must be incomplete or mismatch")
        key = self._replay_quarantine_key(str(candidate_id), str(session_date))
        quarantine = self.replay_quarantine()
        now = time.time()
        previous = quarantine.get(key, {})
        history = list(previous.get("history") or []) if isinstance(previous, Mapping) else []
        history.append({
            "status": status, "reason": str(reason),
            "source_digest": source_digest, "shadow_digest": shadow_digest,
            "replay_digest": replay_digest, "observed_ts": now,
        })
        history = history[-MAX_REPLAY_REPAIR_HISTORY:]
        entry = {
            "schema": "shadow-replay-repair.v1",
            "candidate_id": str(candidate_id),
            "session_date": str(session_date),
            "status": "quarantined",
            "reason": str(reason),
            "source_digest": source_digest,
            "shadow_digest": shadow_digest,
            "replay_digest": replay_digest,
            "first_seen_ts": previous.get("first_seen_ts", now),
            "last_seen_ts": now,
            "repair_count": int(previous.get("repair_count", 0) or 0),
            "history": history,
        }
        quarantine[key] = entry
        self._save_replay_quarantine(quarantine)
        return entry

    def repair_replay_session(self, *, candidate_id: str,
                              session_date: str, source_digest: str,
                              shadow_digest: str, replay_digest: str,
                              reason: str = "complete parity replay") -> dict[str, Any] | None:
        """Persist an explicit repaired/replayed transition for a session."""
        if self.readonly:
            raise ShadowError("cannot update replay quarantine on a read-only WAL")
        key = self._replay_quarantine_key(str(candidate_id), str(session_date))
        quarantine = self.replay_quarantine()
        previous = quarantine.get(key)
        if not isinstance(previous, Mapping):
            return None
        now = time.time()
        history = list(previous.get("history") or [])
        history.append({
            "status": "repaired", "reason": str(reason),
            "source_digest": source_digest, "shadow_digest": shadow_digest,
            "replay_digest": replay_digest, "repaired_ts": now,
        })
        entry = dict(previous)
        entry.update({
            "status": "repaired", "reason": str(reason),
            "source_digest": source_digest, "shadow_digest": shadow_digest,
            "replay_digest": replay_digest,
            "repaired_ts": now,
            "repair_count": int(previous.get("repair_count", 0) or 0) + 1,
            "history": history[-MAX_REPLAY_REPAIR_HISTORY:],
        })
        quarantine[key] = entry
        self._save_replay_quarantine(quarantine)
        return entry

    def upsert_candidate(self, candidate: Mapping[str, Any]) -> None:
        config = candidate.get("config")
        if config is None:
            encoded = candidate.get("config_json")
            if isinstance(encoded, str):
                try:
                    decoded = json.loads(encoded)
                except (TypeError, ValueError, json.JSONDecodeError):
                    decoded = None
                if isinstance(decoded, Mapping):
                    config = decoded
        if not isinstance(config, Mapping):
            config = {}
        proof = {key: candidate.get(key) for key in (
            "dataset_hash", "config_hash", "code_hash", "provenance_hash", "status")}
        with self._connection() as db:
            db.execute("""INSERT OR IGNORE INTO candidates
                (candidate_id,variant_id,strategy_id,vehicle,status,config_json,proof_json,observed_at)
                VALUES(?,?,?,?,?,?,?,?)""", (
                    str(candidate["candidate_id"]), str(candidate.get("variant_id") or ""),
                    str(candidate.get("strategy_id") or ""), str(candidate.get("vehicle") or ""),
                    str(candidate.get("status") or ""), _json(config), _json(proof), time.time()))

    def decision(self, *, candidate_id: str, event_key: str, session_date: str,
                 symbol: str, kind: str, reason: str | None, payload: Mapping,
                 max_decisions: int) -> bool:
        decision_id = _digest({"candidate_id": candidate_id, "event_key": event_key})
        with self._connection() as db:
            existing = db.execute("SELECT 1 FROM decisions WHERE candidate_id=? AND event_key=?",
                                  (candidate_id, event_key)).fetchone()
            if existing is not None:
                return False
            count = int(db.execute("SELECT count(*) FROM decisions").fetchone()[0])
            if count >= int(max_decisions):
                raise ShadowError(f"shadow decision bound {max_decisions} exceeded")
            db.execute("""INSERT INTO decisions
                (decision_id,candidate_id,event_key,session_date,symbol,kind,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (decision_id, candidate_id, event_key,
                    session_date, symbol, kind, reason, _json(payload), time.time()))
        return True

    def virtual_open(self, *, candidate_id: str, decision_id: str, symbol: str,
                     plan: Mapping) -> None:
        quantity = _finite(plan.get("contracts", plan.get("shares")))
        entry = _finite(plan.get("entry_price"))
        with self._connection() as db:
            db.execute("""INSERT OR IGNORE INTO virtual_books
                (book_id,candidate_id,decision_id,symbol,status,quantity,entry_price,plan_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (_digest({"candidate_id": candidate_id,
                "decision_id": decision_id}), candidate_id, decision_id, symbol,
                "open_incomplete", quantity, entry, _json(plan), time.time()))

    def has_open(self, candidate_id: str, symbol: str) -> bool:
        with self._connection() as db:
            return db.execute("""SELECT 1 FROM virtual_books WHERE candidate_id=?
                AND symbol=? AND status='open_incomplete' LIMIT 1""", (candidate_id, symbol)).fetchone() is not None

    def open_books(self, candidate_id: str) -> list[dict]:
        """Return this candidate's still-open virtual books only.

        The shadow lane never shares portfolio state across candidates.  This
        read is intentionally scoped by candidate and leaves the immutable
        book rows untouched; callers decode ``plan_json`` for admission.
        """
        with self._connection() as db:
            rows = db.execute("""SELECT * FROM virtual_books
                WHERE candidate_id=? AND status='open_incomplete'
                ORDER BY created_at, book_id""", (candidate_id,)).fetchall()
            return [dict(row) for row in rows]

    def close_session_books(self, candidate_id: str, session_date: str) -> int:
        """Close incomplete virtual observations once that session is replayed."""
        with self._connection() as db:
            cursor = db.execute("""UPDATE virtual_books SET status='closed_replay'
                WHERE candidate_id=? AND status='open_incomplete' AND decision_id IN
                    (SELECT decision_id FROM decisions WHERE candidate_id=? AND session_date=?)""",
                               (candidate_id, candidate_id, session_date))
            return int(cursor.rowcount)

    def candidates(self) -> list[dict]:
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM candidates ORDER BY candidate_id")]

    def events(self, *, inserted_after: float = 0.0) -> list[dict]:
        with self._connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE inserted_at>=? ORDER BY timestamp,event_key",
                (float(inserted_after),))]

    def decisions(self, candidate_id: str | None = None) -> list[dict]:
        with self._connection() as db:
            if candidate_id:
                rows = db.execute("SELECT * FROM decisions WHERE candidate_id=? ORDER BY created_at,decision_id", (candidate_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM decisions ORDER BY created_at,decision_id").fetchall()
            return [dict(row) for row in rows]

    def replay_diff(self, *, candidate_id: str, session_date: str, source_digest: str,
                    shadow_digest: str, replay_digest: str | None, status: str,
                    details: Mapping) -> None:
        with self._connection() as db:
            db.execute("""INSERT INTO replay_diffs
                (diff_id,candidate_id,session_date,source_digest,shadow_digest,replay_digest,status,details_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(candidate_id,session_date) DO UPDATE SET
                    source_digest=excluded.source_digest,
                    shadow_digest=excluded.shadow_digest,
                    replay_digest=excluded.replay_digest,
                    status=excluded.status,
                    details_json=excluded.details_json,
                    created_at=excluded.created_at""", (_digest({"candidate_id": candidate_id, "session_date": session_date}),
                    candidate_id, session_date, source_digest, shadow_digest, replay_digest,
                    status, _json(details), time.time()))

    def record_replay_evidence(self, *, candidate_id: str, session_date: str,
                               replay_digest: str, vehicle: str,
                               starting_cash: float, ending_cash: float,
                               realized_pnl: float, trades: Sequence[Mapping],
                               replay_status: str) -> None:
        """Persist immutable replay outcomes in the isolated shadow WAL.

        Rows are deliberately not written to EdgeLedger.  ``gate_rows`` below
        joins them to the current replay diff and exposes them only when the
        completed same-session replay has semantic parity.
        """
        values = (float(starting_cash), float(ending_cash), float(realized_pnl))
        if not all(math.isfinite(value) for value in values):
            raise ShadowError("shadow account cash/P&L must be finite")
        ordered: list[dict[str, Any]] = []
        for trade in trades:
            if not isinstance(trade, Mapping):
                continue
            ordered.append(dict(trade))
        ordered.sort(key=_json)
        trade_count = len([row for row in ordered if row.get("no_trade") is not True])
        account_id = _digest({"candidate_id": candidate_id,
                              "session_date": session_date,
                              "replay_digest": replay_digest})
        with self._connection() as db:
            db.execute("""INSERT OR IGNORE INTO shadow_accounts
                (account_id,candidate_id,session_date,replay_digest,vehicle,
                 starting_cash,ending_cash,realized_pnl,trade_count,replay_status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    account_id, candidate_id, session_date, replay_digest,
                    str(vehicle), values[0], values[1], values[2], trade_count,
                    str(replay_status), time.time()))
            occurrence: dict[str, int] = {}
            for trade in ordered:
                trade_digest = _digest(trade)
                index = occurrence.get(trade_digest, 0)
                occurrence[trade_digest] = index + 1
                trade_id = _digest({"candidate_id": candidate_id,
                                    "session_date": session_date,
                                    "replay_digest": replay_digest,
                                    "trade_digest": trade_digest,
                                    "occurrence": index})
                db.execute("""INSERT OR IGNORE INTO shadow_trades
                    (trade_id,candidate_id,session_date,replay_digest,replay_status,
                     trade_json,created_at)
                    VALUES(?,?,?,?,?,?,?)""", (
                        trade_id, candidate_id, session_date, replay_digest,
                        str(replay_status), _json(trade), time.time()))

    def replay_accounts(self, candidate_id: str | None = None) -> list[dict]:
        with self._connection() as db:
            if candidate_id is None:
                rows = db.execute("""SELECT * FROM shadow_accounts
                    ORDER BY session_date,candidate_id,replay_digest""").fetchall()
            else:
                rows = db.execute("""SELECT * FROM shadow_accounts
                    WHERE candidate_id=? ORDER BY session_date,replay_digest""",
                                  (candidate_id,)).fetchall()
            return [dict(row) for row in rows]

    def replay_metadata(self, candidate_id: str | None = None) -> list[dict]:
        """Return replay diffs joined to their immutable account summaries.

        Ingestion uses this read-only projection as the completion/parity
        boundary.  Keeping the join here prevents callers from accidentally
        treating a diagnostic trade row as authorizing evidence without its
        same-session source and replay digests.
        """
        query = """SELECT d.candidate_id, d.session_date, d.source_digest,
                   d.shadow_digest, d.replay_digest, d.status,
                   d.details_json, a.account_id, a.vehicle, a.starting_cash,
                   a.ending_cash, a.realized_pnl, a.trade_count,
                   a.replay_status, a.created_at AS account_created_at
                   FROM replay_diffs d LEFT JOIN shadow_accounts a
                   ON a.candidate_id=d.candidate_id
                   AND a.session_date=d.session_date
                   AND a.replay_digest=d.replay_digest"""
        params: tuple[Any, ...] = ()
        if candidate_id is not None:
            query += " WHERE d.candidate_id=?"
            params = (str(candidate_id),)
        query += " ORDER BY d.session_date,d.candidate_id"
        with self._connection() as db:
            rows = db.execute(query, params).fetchall()
        result: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json"))
            except (TypeError, ValueError, json.JSONDecodeError):
                item["details"] = {}
            result.append(item)
        return result

    def gate_rows(self, candidate_id: str, session_date: str | None = None) -> list[dict]:
        """Return rows eligible for existing gates after replay parity only."""
        params: list[Any] = [candidate_id]
        clause = ""
        if session_date is not None:
            clause = " AND t.session_date=?"
            params.append(session_date)
        with self._connection() as db:
            rows = db.execute(f"""SELECT t.trade_json FROM shadow_trades t
                JOIN replay_diffs d ON d.candidate_id=t.candidate_id
                    AND d.session_date=t.session_date
                    AND d.replay_digest=t.replay_digest
                WHERE t.candidate_id=? AND d.status='match'
                    AND t.replay_status='match'{clause}
                ORDER BY t.session_date,t.trade_id""", tuple(params)).fetchall()
            result: list[dict] = []
            for row in rows:
                try:
                    item = json.loads(row["trade_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(item, Mapping):
                    result.append(dict(item))
            return result

    def prune(self) -> dict[str, Any]:
        """Prune only derived replay metadata and report what was removed.

        Source events, decisions, accounts, and trades are immutable evidence
        and are intentionally never part of retention.  Returning bounded
        counters makes retention visible to the polling heartbeat without
        changing the evidence contract.
        """
        floor = time.time() - max(1, self.retention_days) * 86400
        # Retention is the only intentional deletion.  The append-only rows
        # themselves cannot be updated or deleted by ordinary writes.
        with self._connection() as db:
            before = int(db.execute(
                "SELECT count(*) FROM replay_diffs WHERE created_at < ?",
                (floor,)).fetchone()[0])
            previous = db.execute(
                "SELECT value FROM meta WHERE key='replay_diff_prune_watermark'").fetchone()
            if before:
                latest_session = db.execute(
                    "SELECT MAX(session_date) FROM replay_diffs WHERE created_at < ?",
                    (floor,)).fetchone()[0]
                watermark = {
                    "floor_ts": float(floor),
                    "pruned_replay_diffs": before,
                    "latest_pruned_session": (str(latest_session)
                                               if latest_session else None),
                    "updated_ts": time.time(),
                }
                # The watermark is diagnostic metadata, not an authorizing
                # row. Commit it in the same transaction before deleting
                # derived diffs so a crash cannot erase the gap indication.
                db.execute("""INSERT INTO meta(key,value) VALUES('replay_diff_prune_watermark',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                           (_json(watermark),))
            else:
                try:
                    watermark = (json.loads(previous["value"])
                                 if previous is not None else None)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ShadowError("shadow retention watermark is invalid") from exc
                if watermark is not None and not isinstance(watermark, Mapping):
                    raise ShadowError("shadow retention watermark is invalid")
            db.execute("DELETE FROM replay_diffs WHERE created_at < ?", (floor,))
        return {"retention_days": int(self.retention_days),
                "retention_floor_ts": float(floor),
                "pruned_replay_diffs": before,
                "retention_gap_watermark": watermark}

    def prune_watermark(self) -> dict[str, Any] | None:
        """Return the last non-authorizing replay-diff retention watermark."""
        with self._connection() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='replay_diff_prune_watermark'").fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ShadowError("shadow retention watermark is invalid") from exc
        if not isinstance(value, Mapping):
            raise ShadowError("shadow retention watermark is invalid")
        return dict(value)


def _read_candidates(path: Path, *, max_candidates: int) -> list[dict]:
    """Resolve candidates without constructing EdgeLedger (which migrates DBs)."""
    if not path.exists():
        return []
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        # ``sqlite3.Connection`` implements the transaction context manager,
        # but that manager does not close the connection on exit.  These
        # read-only helpers are called on every shadow poll; explicitly close
        # each handle so repeated polls cannot leak file descriptors.
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                ON c.candidate_id=s.candidate_id
                WHERE (s.status IN ('backtest_passed','shadow','demoted','validated','champion')
                       AND c.strategy_id IN ('ibr','rule'))
                   OR (c.strategy_id='ibr' AND c.variant_id='ibr.baseline')
                ORDER BY CASE WHEN c.strategy_id='ibr' AND c.variant_id='ibr.baseline'
                              THEN 0 ELSE 1 END, c.created_at,c.candidate_id
                LIMIT ?""", (int(max_candidates),)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["config"] = json.loads(item.pop("config_json"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["config"] = {}
                try:
                    item["axes"] = json.loads(item.pop("axes_json"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["axes"] = {}
                result.append(item)
            return result
    except sqlite3.OperationalError as exc:
        # A deployment may start before the research cycle has created its
        # ledger.  Treat that as an empty read-only source, never initialize
        # or mutate it from this process.
        if "no such table" in str(exc).lower():
            return []
        raise ShadowError(f"cannot read EdgeLedger read-only: {exc}") from exc
    except sqlite3.Error as exc:
        raise ShadowError(f"cannot read EdgeLedger read-only: {exc}") from exc


def _read_factory_rule_roots(path: Path) -> dict[str, dict[str, Any]]:
    """Read immutable factory root specs without opening a writable ledger.

    The live shadow worker may read the research ledger, but it must never
    initialize or migrate it.  ``factory_hypotheses`` is therefore queried
    directly through SQLite's read-only URI and malformed rows are ignored.
    """
    if not path.is_file():
        return {}
    uri = f"file:{path.resolve()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT hypothesis_id,vehicle,spec_json FROM factory_hypotheses"
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    roots: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            raw = json.loads(row["spec_json"])
            spec = validate_rule_spec(raw)
            roots[str(row["hypothesis_id"])] = {
                "vehicle": str(row["vehicle"]),
                "rule_spec": spec,
                "variant_id": rule_variant_id(spec),
            }
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return roots


def _policy(config: Mapping[str, Any]) -> ReplayPolicy:
    try:
        return replace(ReplayPolicy.from_config(config), strict_market_data=True)
    except Exception:
        session = config.get("session") if isinstance(config, Mapping) else {}
        required = bool(session.get("require_exact_calendar", False)) \
            if isinstance(session, Mapping) else False
        return ReplayPolicy(strict_market_data=True,
                            require_exact_calendar=required)


def _safe_config(candidate: Mapping[str, Any]) -> dict:
    config = candidate.get("config")
    # EdgeLedger's public candidate row stores the immutable configuration as
    # ``config_json``.  The read-only resolver decodes that field before
    # handing rows to the runner, but direct callers (and older integrations)
    # may provide the raw ledger row.  Preserve the same replay path for both
    # shapes without ever mutating the candidate mapping.
    if config is None:
        encoded = candidate.get("config_json")
        if isinstance(encoded, str):
            try:
                decoded = json.loads(encoded)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, Mapping):
                config = decoded
    out = dict(config) if isinstance(config, Mapping) else {}
    strategy = dict(out.get("strategy") or {})
    strategy.setdefault("id", candidate.get("strategy_id") or "ibr")
    strategy.setdefault("version", candidate.get("base_version") or "v1")
    strategy.setdefault("variant_id", candidate.get("variant_id"))
    out["strategy"] = strategy
    return out


class ShadowRunner:
    """Incrementally ingest, evaluate, and replay one broker-free corpus."""

    def __init__(self, config: ShadowConfig):
        self.config = config
        self.store = ShadowStore(config.shadow_db, retention_days=config.retention_days)
        self._factory_roots = _read_factory_rule_roots(config.edge_db)
        # Workers install an in-memory portfolio projection for their arm.
        # Thread-local state keeps the existing ``_evaluate`` call contract
        # (and test seams) while ensuring no worker mutates or observes a
        # sibling candidate's virtual book.
        self._worker_state = threading.local()

    def _rule_root_control(self, candidate: Mapping[str, Any]) -> dict[str, Any] | None:
        """Build the exact-window root control for a tuned rule candidate.

        Factory hypotheses are the authority for a slot's root rule.  A
        descendant is therefore compared with a synthetic control namespace
        tied to its own candidate id; this keeps the control replay isolated
        from any EdgeLedger lifecycle state and prevents a candidate from
        accidentally selecting its own mutated spec as its baseline.
        """
        if str(candidate.get("strategy_id")) != "rule":
            return None
        axes = candidate.get("axes")
        if axes is None and isinstance(candidate.get("axes_json"), str):
            try:
                axes = json.loads(str(candidate["axes_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                axes = None
        hypothesis_id = axes.get("hypothesis_id") if isinstance(axes, Mapping) else None
        root = self._factory_roots.get(str(hypothesis_id)) if hypothesis_id else None
        if not isinstance(root, Mapping) or root.get("vehicle") != str(candidate.get("vehicle")):
            return None
        root_variant = str(root.get("variant_id") or "")
        if not root_variant or str(candidate.get("variant_id")) == root_variant:
            # The root hypothesis is itself the control target.  Its paired
            # baseline is the randomized-entry null generated by _replay.
            return None
        config = _safe_config(candidate)
        strategy = dict(config.get("strategy") or {})
        strategy.update({"id": "rule", "variant_id": root_variant,
                         "rule_spec": dict(root["rule_spec"])})
        config["strategy"] = strategy
        return {**dict(candidate),
                "candidate_id": f"shadow:baseline:{candidate['candidate_id']}",
                "variant_id": root_variant,
                "config": config,
                "axes": {"hypothesis_id": hypothesis_id, "role": "paired_root_control"}}

    def _load_events(self) -> tuple[list[dict], dict[str, list[dict]], dict[str, list[dict]], dict[str, list[dict]]]:
        bars: dict[str, list[dict]] = {}
        quotes: dict[str, list[dict]] = {}
        options: dict[str, list[dict]] = {}
        floor = self.store.forward_event_floor() or 0.0
        event_rows = self.store.events(inserted_after=floor)
        for row in event_rows:
            try:
                payload = json.loads(row["event_json"])
                _, event = _normalize_row(payload)
            except (TypeError, ValueError, json.JSONDecodeError, NormalizationError):
                continue
            plain = payload
            symbol = str(row["symbol"])
            if row["event_type"] in {"bar", "bar_1m"}:
                bars.setdefault(symbol, []).append(plain)
            elif row["event_type"] == "quote":
                quotes.setdefault(symbol, []).append(plain)
            elif row["event_type"] in {"option", "option_snapshot"}:
                underlying = str(plain.get("underlying") or "")
                options.setdefault(underlying, []).append(plain)
        for values in (bars, quotes, options):
            for key in values:
                values[key].sort(key=lambda row: str(row.get("timestamp") or ""))
        return event_rows, bars, quotes, options

    @staticmethod
    def _latest_quote(rows: Sequence[Mapping], at: datetime, *,
                      expected_feed: str | None = None) -> Mapping | None:
        valid = []
        for row in rows:
            if (expected_feed is not None and
                    _canonical_equity_feed(row.get("feed")) != expected_feed):
                continue
            stamp = _timestamp(row.get("timestamp"))
            if stamp is not None and stamp <= at and _row_visible(row, at):
                bid, ask = _finite(row.get("bid")), _finite(row.get("ask"))
                if bid is not None and ask is not None and bid > 0 and ask >= bid:
                    valid.append((stamp, row))
        return max(valid, key=lambda pair: pair[0])[1] if valid else None

    def _evaluate(self, candidate: Mapping[str, Any], event: Mapping[str, Any],
                  bars: Mapping[str, list], quotes: Mapping[str, list],
                  options: Mapping[str, list]) -> tuple[str, str | None, dict, dict | None]:
        symbol = str(event.get("symbol") or "")
        cfg = _safe_config(candidate)
        strategy = cfg.get("strategy", {})
        event_at = _availability_time(event)
        if event_at is None:
            return "no_data", "event timestamp unavailable", {}, None
        market_at = _timestamp(event.get("timestamp") or event.get("as_of"))
        if market_at is None:
            return "no_data", "event market timestamp unavailable", {}, None
        session = market_at.astimezone(NEW_YORK).date().isoformat()
        session_cfg = cfg.get("session") if isinstance(cfg.get("session"), Mapping) else {}
        require_exact_calendar = bool(session_cfg.get("require_exact_calendar", False))
        close_at, calendar_source = _session_close(
            self.config.corpus_path, session,
            require_exact_calendar=require_exact_calendar)
        if require_exact_calendar and close_at is None:
            return ("no_data", "exact broker calendar metadata unavailable",
                    {"session_date": session,
                     "calendar_source": calendar_source}, None)
        policy = _session_policy(cfg, close_at)
        expected_equity_feed = policy.equity_feed
        observed_equity_feed = _canonical_equity_feed(event.get("feed"))
        if observed_equity_feed != expected_equity_feed:
            return ("no_data", "equity feed mismatch",
                    {"session_date": session,
                     "equity_feed": expected_equity_feed,
                     "observed_equity_feed": observed_equity_feed}, None)
        calendar_bounds = _recorded_session_bounds(
            self.config.corpus_path, session)
        if close_at is not None:
            local_day = market_at.astimezone(NEW_YORK).date()
            latest_at = (datetime.combine(local_day, policy.latest_entry_time,
                                          tzinfo=NEW_YORK).astimezone(UTC)
                         if policy.latest_entry_time is not None else close_at)
            if event_at >= close_at or event_at >= latest_at:
                return ("no_trade", "session entry cutoff reached",
                        {"session_date": session,
                         "session_close": close_at.isoformat(),
                         "calendar_source": calendar_source}, None)
        stream = [row for row in bars.get(symbol, [])
                  if _canonical_equity_feed(row.get("feed")) == expected_equity_feed
                  and _row_visible(row, event_at)
                  and (close_at is None or (
                      (calendar_bounds is None or
                       (_timestamp(row.get("timestamp")) or close_at) >= calendar_bounds[0])
                      and (_timestamp(row.get("timestamp")) or close_at) < close_at
                      and (_event_end(row) or close_at) <= close_at))]
        if len(stream) < 2:
            return "no_data", "insufficient bars", {"session_date": session}, None
        strategy_id = str(candidate.get("strategy_id") or strategy.get("id") or "ibr")
        rule_context = None
        rule_spec = None
        if strategy_id == "rule":
            raw_spec = strategy.get("rule_spec") if isinstance(strategy, Mapping) else None
            try:
                rule_spec = validate_rule_spec(raw_spec or {})
            except (TypeError, ValueError) as exc:
                return "reject", "invalid rule specification", {
                    "session_date": session, "error": str(exc)[:240]}, None
            if (rule_spec["family"] == "cross_sectional_residual" and
                    not rule_vehicle_executable(
                        rule_spec, str(candidate.get("vehicle") or "equity"))):
                return ("reject", "cross_sectional_requires_equity_shares",
                        {"session_date": session,
                         "benchmark_symbol": CROSS_SECTIONAL_BENCHMARK}, None)
            if rule_spec["family"] == "cross_sectional_residual":
                # Relative signals may consume only completed SPY bars that
                # were observable at the same decision instant as the subject.
                stream = [row for row in stream
                          if (_event_end(row) or event_at) <= event_at]
                benchmark = tuple(
                    row for row in bars.get(CROSS_SECTIONAL_BENCHMARK, ())
                    if (_canonical_equity_feed(row.get("feed")) ==
                        expected_equity_feed)
                    and _row_visible(row, event_at)
                    and (_event_end(row) or event_at) <= event_at
                    and ((_timestamp(row.get("timestamp")) or market_at)
                         .astimezone(NEW_YORK).date().isoformat() == session)
                )
                rule_context = MappingProxyType({
                    CROSS_SECTIONAL_BENCHMARK: benchmark,
                })
        try:
            if strategy_id == "rule":
                signal = (generate_rule_signal(
                              symbol, stream, config=cfg, now=event_at)
                          if rule_context is None else
                          generate_rule_signal(
                              symbol, stream, config=cfg, now=event_at,
                              bars_by_symbol=rule_context))
            else:
                signal = generate_ibr_signal(
                    symbol, stream, config=cfg, now=event_at)
        except Exception as exc:
            return "reject", f"signal exception: {type(exc).__name__}", {"error": str(exc)[:240]}, None
        base = {"session_date": session, "strategy_id": strategy_id,
                "equity_feed": expected_equity_feed,
                "variant_id": candidate.get("variant_id"), "signal": signal}
        if signal is None:
            if (rule_spec is not None and
                    rule_spec["family"] == "cross_sectional_residual"):
                trace = evaluate_rule_signal_trace(
                    stream, rule_spec, bars_by_symbol=rule_context,
                    symbol=symbol)
                stages = trace.get("stages") or []
                trace_reason = (str(stages[-1].get("reason") or "")
                                if stages else "")
                base["signal_trace"] = trace
                if trace_reason.startswith(("benchmark_context_",
                                            "subject_context_")):
                    return "no_data", trace_reason, base, None
            return "no_trade", "no signal", base, None
        # Runtime decisions are made when the completed feature prefix is
        # actually observed.  Persist that causal instant and use it as the
        # entry boundary; replay compares these fields rather than deriving a
        # synthetic next-bar timestamp from the market event alone.
        signal = dict(signal)
        signal["decision_timestamp"] = event_at.isoformat()
        signal["entry_timestamp"] = event_at.isoformat()
        base["signal"] = signal
        quote = self._latest_quote(
            quotes.get(symbol, ()), event_at,
            expected_feed=expected_equity_feed)
        snap: dict[str, Any] = {"price": _finite(event.get("close")) or _finite(event.get("open")),
                                "close": _finite(event.get("close")),
                                "spread_bps": None, "stale": True, "quote_stale": True,
                                "session": session, "signal_ts": signal.get("signal_ts"),
                                "equity_feed": expected_equity_feed}
        if quote is not None:
            quote_at = _timestamp(quote.get("timestamp"))
            bid, ask = _finite(quote.get("bid")), _finite(quote.get("ask"))
            age = None if quote_at is None else max(0.0, (event_at - quote_at).total_seconds())
            if bid and ask and bid > 0 and ask >= bid:
                snap.update(price=(bid + ask) / 2, spread_bps=(ask - bid) / ((bid + ask) / 2) * 10_000,
                            quote_ts=quote_at.isoformat() if quote_at else None,
                            quote_age_seconds=age,
                            stale=bool(age is None or age > policy.max_market_data_age_seconds),
                            quote_stale=bool(age is None or age > policy.max_market_data_age_seconds))
        # The setup primitive consumes the exact signal geometry while the
        # quote fields above carry strict point-in-time freshness metadata.
        snap.update({key: value for key, value in signal.items()
                     if key not in {"symbol", "action"} and value is not None})
        snap["price"] = snap.get("price") or _finite(signal.get("entry_price"))
        snap["entry_price"] = _finite(signal.get("entry_price")) or snap.get("price")
        if signal.get("range_high") is not None and signal.get("range_low") is not None:
            snap["ibr_range"] = {"high": signal.get("range_high"),
                                  "low": signal.get("range_low"),
                                  "width": signal.get("range_width"),
                                  "complete": True}
        if str(candidate.get("vehicle") or "equity") == "option":
            option_rows = []
            for row in options.get(symbol, ()):
                stamp = _timestamp(row.get("timestamp"))
                if (stamp is not None and stamp <= event_at and
                        _row_visible(row, event_at) and
                        str(row.get("feed") or "").strip().lower() == "opra"):
                    item = dict(row)
                    item.setdefault("quote_ts", stamp.isoformat())
                    age = max(0.0, (event_at - stamp).total_seconds())
                    item.setdefault("quote_age_seconds", age)
                    option_rows.append(item)
            if not option_rows:
                return ("unpriced", "executable OPRA option chain unavailable",
                        base | {"snapshot": snap}, None)
            snap["option_chain"] = option_rows
        if snap["stale"] or snap["quote_stale"]:
            return "unpriced", "stale or unavailable quote", base | {"snapshot": snap}, None
        try:
            plan, why = build_setup_plan(signal, snap, cfg)
        except Exception as exc:
            return "reject", f"setup exception: {type(exc).__name__}", base, None
        if plan is None:
            return "reject", why or "setup rejected", base | {"snapshot": snap}, None
        plan = dict(plan)
        plan["decision_timestamp"] = event_at.isoformat()
        plan["entry_timestamp"] = event_at.isoformat()
        plan["equity_feed"] = expected_equity_feed
        # A candidate's virtual book is isolated and feeds only that
        # candidate's portfolio admission.  Plans are immutable observations;
        # no fills, mark-to-market, or fabricated P&L is introduced here.
        try:
            positions, active_trades, gross_notional = self._portfolio_state(
                str(candidate["candidate_id"]))
        except ShadowError as exc:
            return "reject", f"portfolio state unavailable: {exc}", base, None
        risk = RiskEngine(cfg)
        try:
            risk_plan, why = risk.vet_open(
                plan, float(self.config.equity), positions, {symbol: snap}, {},
                gross_notional, active_trades=active_trades,
                now=event_at.timestamp())
        except Exception as exc:
            return "reject", f"risk exception: {type(exc).__name__}", base, None
        if risk_plan is None:
            return "reject", why or "risk rejected", base | {"snapshot": snap}, None
        return "open_incomplete", "virtual open; fills and P&L incomplete", base | {
            "snapshot": snap, "setup_plan": plan, "risk_plan": risk_plan,
        }, risk_plan

    def _portfolio_state(self, candidate_id: str) -> tuple[list[dict], dict[str, dict], float]:
        """Build risk admission state from one candidate's open books."""
        overrides = getattr(self._worker_state, "portfolios", None)
        if isinstance(overrides, Mapping) and candidate_id in overrides:
            positions, active_trades, gross_notional = overrides[candidate_id]
            return (list(positions), dict(active_trades), float(gross_notional))
        positions: list[dict] = []
        active_trades: dict[str, dict] = {}
        gross_notional = 0.0
        for row in self.store.open_books(candidate_id):
            try:
                plan = json.loads(row.get("plan_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ShadowError("open book plan is invalid") from exc
            if not isinstance(plan, Mapping):
                raise ShadowError("open book plan is invalid")
            symbol = str(row.get("symbol") or plan.get("symbol") or "")
            if not symbol:
                raise ShadowError("open book symbol is unavailable")
            risk_usd = _finite(plan.get("risk_usd"))
            notional = _finite(plan.get("notional"))
            if risk_usd is None or risk_usd < 0:
                raise ShadowError(f"open book risk is unavailable for {symbol}")
            if notional is None or notional < 0:
                raise ShadowError(f"open book notional is unavailable for {symbol}")
            position = dict(plan)
            position["symbol"] = symbol
            if row.get("quantity") is not None:
                position.setdefault("quantity", row["quantity"])
            if row.get("entry_price") is not None:
                position.setdefault("entry_price", row["entry_price"])
            positions.append(position)
            active_trade = dict(position)
            active_trade["risk_usd"] = risk_usd
            active_trades[symbol] = active_trade
            gross_notional += notional
        return positions, active_trades, gross_notional

    def _replay(self, candidate: Mapping[str, Any], session: str,
                session_bars: Sequence[Mapping], session_quotes: Sequence[Mapping],
                decisions: Sequence[Mapping],
                session_options: Sequence[Mapping] | None = None) -> bool:
        candidate_id = str(candidate["candidate_id"])
        # Option replay is also point-in-time input.  Include it in the source
        # digest so a corrected/stale option snapshot cannot be mistaken for
        # the same replay window merely because the underlying bars/quotes
        # were unchanged.
        cfg = _safe_config(candidate)
        session_cfg = cfg.get("session") if isinstance(cfg.get("session"), Mapping) else {}
        require_exact_calendar = bool(session_cfg.get("require_exact_calendar", False))
        calendar_close, calendar_source = _session_close(
            self.config.corpus_path, session,
            require_exact_calendar=require_exact_calendar)
        if require_exact_calendar and calendar_close is None:
            session_bars = ()
            session_quotes = ()
            session_options = ()
        in_session_bars = [row for row in session_bars
                           if calendar_close is None or (
                               (_timestamp(row.get("timestamp")) or calendar_close) < calendar_close
                               and (_event_end(row) or calendar_close) <= calendar_close)]
        in_session_quotes = [row for row in session_quotes
                             if calendar_close is None or
                             (_timestamp(row.get("timestamp")) or calendar_close) <= calendar_close]
        in_session_options = [row for row in (session_options or ())
                              if calendar_close is None or
                              (_timestamp(row.get("timestamp")) or calendar_close) <= calendar_close]
        replay_policy = _session_policy(cfg, calendar_close)
        expected_equity_feed = replay_policy.equity_feed
        candidate_vehicle = str(candidate.get("vehicle") or "equity")
        feed_mismatches = [
            {"kind": kind,
             "symbol": str(row.get("symbol") or ""),
             "timestamp": str(row.get("timestamp") or ""),
             "observed_feed": _canonical_equity_feed(row.get("feed"))}
            for kind, rows in (("bar", in_session_bars),
                               ("quote", in_session_quotes))
            for row in rows
            if _canonical_equity_feed(row.get("feed")) != expected_equity_feed
        ]
        source_digest = _digest({"bars": in_session_bars,
                                 "quotes": in_session_quotes,
                                 "options": in_session_options,
                                 "equity_feed": expected_equity_feed,
                                 "session_close": (calendar_close.isoformat()
                                                   if calendar_close else None),
                                 "calendar_source": calendar_source})
        shadow_signatures = []
        for row in decisions:
            if row.get("session_date") != session:
                continue
            signature = _shadow_signature(row)
            if signature is not None:
                shadow_signatures.append(signature)
        shadow_signatures.sort(key=lambda row: _json(row))
        shadow_digest = _digest(shadow_signatures)
        replay_digest = None
        replay_ok = False
        replay_signatures: list[dict[str, Any]] = []
        evidence_rows: list[dict[str, Any]] = []
        null_rows: list[dict[str, Any]] = []
        null_account: dict[str, Any] = {}
        starting_cash = float(self.config.equity)
        ending_cash = starting_cash
        realized_pnl = 0.0
        details: dict[str, Any] = {"complete": False, "trade_count": 0,
                                   "equity_feed": expected_equity_feed,
                                   "feed_mismatches": feed_mismatches,
                                   "session_close": (calendar_close.isoformat()
                                                     if calendar_close else None),
                                   "calendar_source": calendar_source,
                                   "shadow_signatures": shadow_signatures,
                                   "replay_signatures": replay_signatures}
        complete = bool(calendar_close is not None and
                        any((_event_end(row) or datetime.min.replace(tzinfo=UTC)) >=
                            calendar_close for row in in_session_bars))
        try:
            if feed_mismatches:
                raise ShadowError(
                    f"equity feed mismatch: expected {expected_equity_feed}")
            calendar_bounds = _recorded_session_bounds(
                self.config.corpus_path, session)
            if require_exact_calendar and calendar_bounds is None:
                raise ShadowError("exact broker calendar metadata unavailable")
            normalized_bar_rows = []
            for row in in_session_bars:
                enriched = dict(row)
                if calendar_bounds is not None:
                    enriched["session_open"] = calendar_bounds[0].isoformat()
                    enriched["session_close"] = calendar_bounds[1].isoformat()
                normalized_bar_rows.append(enriched)
            normalized_bars = [_normalize_row(row)[1]
                              for row in normalized_bar_rows]
            normalized_quotes = [_normalize_row(row)[1] for row in in_session_quotes]
            normalized_options = [_normalize_row(row)[1]
                                 for row in in_session_options]
            if str(candidate.get("strategy_id")) == "ibr":
                replay_cfg, _ = _effective_ibr_config(
                    cfg, {}, vehicle=candidate_vehicle,
                    close_confirmed=True, policy=replay_policy)
                option_index = _option_snapshot_index(normalized_options)
                result = replay_ibr(normalized_bars, config=replay_cfg,
                                    vehicle=candidate_vehicle,
                                    option_snapshots=option_index,
                                    quotes=normalized_quotes)
                trades = [_plain(trade) for trade in result.trades]
                evidence_rows = _opportunity_rows(
                    result, normalized_bars, candidate_vehicle)
                realized_pnl = sum(_finite(trade.get("net_pnl")) or 0.0
                                   for trade in trades)
                ending_cash = starting_cash + realized_pnl
                replay_signatures = [signature for trade in trades
                                     if (signature := _replay_signature(
                                         trade, vehicle=candidate_vehicle,
                                         strategy_id="ibr", target_r=replay_cfg.target_r,
                                         equity_feed=expected_equity_feed)) is not None]
                details.update(complete=complete, trade_count=len(trades),
                               opportunity_count=len(evidence_rows), trades=trades,
                               replay_signatures=replay_signatures)
                details["opportunity_capacity"] = _opportunity_capacity(
                    evidence_rows, vehicle=candidate_vehicle)
                # Persist the exact-window randomized-entry null alongside the
                # candidate replay.  It is a diagnostic research source, not a
                # runtime shadow decision, and therefore receives its own
                # synthetic WAL candidate id below.
                try:
                    null_account = null_control_account(
                        normalized_bars, normalized_options, _null_spec(replay_cfg),
                        vehicle=candidate_vehicle,
                        reference_rows=_null_reference_rows(
                            result, normalized_bars,
                            str(candidate.get("vehicle") or "equity"),
                            policy=replay_cfg.policy),
                        account_id=f"shadow:null:{candidate_id}:{session}",
                        starting_cash=float(self.config.equity), costs=replay_cfg.costs,
                        quotes=normalized_quotes, fixed_quantity=replay_cfg.quantity,
                        policy=_policy(cfg))
                    null_rows = list(null_account.get("rows") or [])
                    details["null_control"] = True
                    details["null_trade_count"] = len([
                        row for row in null_rows if row.get("no_trade") is not True])
                except Exception as exc:
                    details["null_control"] = False
                    details["null_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                replay_ok = True
            else:
                strategy = cfg.get("strategy") if isinstance(cfg.get("strategy"), Mapping) else {}
                spec = strategy.get("rule_spec") if isinstance(strategy, Mapping) else None
                if not isinstance(spec, Mapping):
                    raise ValueError("rule candidate has no validated rule_spec")
                policy = replay_policy
                account = simulate_account(
                    normalized_bars, normalized_options,
                    spec, vehicle=candidate_vehicle,
                    account_id=f"shadow:{candidate_id}:{session}",
                    starting_cash=float(self.config.equity),
                    risk_pct=float((cfg.get("risk") or {}).get("risk_per_trade_pct", .5)),
                    costs=cost_model_for_vehicle(cfg, candidate_vehicle),
                    quotes=normalized_quotes, policy=policy)
                rows = list(account.get("rows") or [])
                evidence_rows = rows
                starting_cash = _finite(account.get("starting_cash")) or starting_cash
                ending_cash = _finite(account.get("ending_equity")) or starting_cash
                realized_pnl = _finite(account.get("realized_pnl"))
                if realized_pnl is None:
                    realized_pnl = ending_cash - starting_cash
                replay_signatures = [signature for trade in rows
                                     if (signature := _replay_signature(
                                         trade, vehicle=candidate_vehicle,
                                         strategy_id="rule", target_r=float(spec.get("target_r", 2.0)),
                                         setup_type=f"rule_{spec.get('family', 'signal')}",
                                         equity_feed=expected_equity_feed)) is not None]
                details.update(complete=complete, trade_count=len(replay_signatures),
                               replay_signatures=replay_signatures, account=account)
                details["opportunity_capacity"] = _opportunity_capacity(
                    rows, vehicle=candidate_vehicle)
                try:
                    null_account = null_control_account(
                        normalized_bars, normalized_options, spec,
                        vehicle=candidate_vehicle,
                        reference_rows=rows,
                        account_id=f"shadow:null:{candidate_id}:{session}",
                        starting_cash=float(self.config.equity),
                        risk_pct=float((cfg.get("risk") or {}).get("risk_per_trade_pct", .5)),
                        costs=cost_model_for_vehicle(cfg, candidate_vehicle),
                        quotes=normalized_quotes,
                        policy=policy)
                    null_rows = list(null_account.get("rows") or [])
                    details["null_control"] = True
                    details["null_trade_count"] = len([
                        row for row in null_rows if row.get("no_trade") is not True])
                except Exception as exc:
                    details["null_control"] = False
                    details["null_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                replay_ok = True
        except Exception as exc:
            details.update(complete=complete, error=f"{type(exc).__name__}: {str(exc)[:240]}")
        differences = _signature_diffs(shadow_signatures, replay_signatures)
        replay_digest = _digest(replay_signatures) if details.get("complete") and replay_ok else None
        details.update(signature_match=not differences,
                       signature_mismatches=differences)
        status = ("incomplete" if not details.get("complete") or not replay_ok or replay_digest is None
                  else ("match" if not differences else "mismatch"))
        if complete and replay_ok:
            details["account_summary"] = {
                "starting_cash": starting_cash,
                "ending_cash": ending_cash,
                "realized_pnl": realized_pnl,
                "trade_count": len([row for row in evidence_rows
                                     if row.get("no_trade") is not True]),
                "replay_status": status,
            }
        self.store.replay_diff(candidate_id=candidate_id, session_date=session,
                               source_digest=source_digest, shadow_digest=shadow_digest,
                               replay_digest=replay_digest, status=status, details=details)
        # Keep a durable repair trail for any session that was incomplete or
        # semantically mismatched.  Ingestion treats a quarantined session as
        # blocked even when later sessions are healthy; only a subsequent
        # complete, parity-matched replay records the explicit ``repaired``
        # transition that permits the chronological tail to advance.
        # A normal forward poll may observe a session before its closing bar;
        # that expected open tail is diagnostic but is not itself a repair
        # incident.  Quarantine only an attempted closed-session replay (or a
        # replay exception), while ingestion still refuses incomplete metadata.
        if status == "mismatch" or (
                status == "incomplete" and (complete or not replay_ok)):
            self.store.quarantine_replay_session(
                candidate_id=candidate_id, session_date=session,
                reason=("replay incomplete" if status == "incomplete"
                        else "shadow/replay semantic mismatch"),
                status=status, source_digest=source_digest,
                shadow_digest=shadow_digest, replay_digest=replay_digest)
        else:
            self.store.repair_replay_session(
                candidate_id=candidate_id, session_date=session,
                source_digest=source_digest, shadow_digest=shadow_digest,
                replay_digest=str(replay_digest),
                reason="complete parity replay after quarantine")
        if complete and replay_ok and replay_digest is not None:
            # Persist fills/exits/P&L in the isolated shadow database.  The
            # rows are diagnostic while parity is mismatched; ``gate_rows``
            # exposes them to existing gates only for a current ``match``.
            self.store.record_replay_evidence(
                candidate_id=candidate_id, session_date=session,
                replay_digest=replay_digest,
                vehicle=str(candidate.get("vehicle") or "equity"),
                starting_cash=starting_cash, ending_cash=ending_cash,
                realized_pnl=realized_pnl, trades=evidence_rows,
                replay_status=status)
            # Randomized-entry nulls are generated from this exact normalized
            # session, so their source digest is identical.  They have no
            # runtime semantic signature to compare; ``match`` here means the
            # deterministic null replay completed, not that a broker decision
            # matched it.
            null_digest = _digest(null_rows)
            null_candidate_id = f"shadow:null:{candidate_id}"
            self.store.replay_diff(
                candidate_id=null_candidate_id, session_date=session,
                source_digest=source_digest, shadow_digest=_digest([]),
                replay_digest=null_digest, status="match",
                details={"complete": True, "signature_match": True,
                         "equity_feed": expected_equity_feed,
                         "null_control": True, "replay_signatures": [],
                         "null_rows_digest": null_digest})
            self.store.record_replay_evidence(
                candidate_id=null_candidate_id, session_date=session,
                replay_digest=null_digest,
                vehicle=str(candidate.get("vehicle") or "equity"),
                starting_cash=_finite(null_account.get("starting_cash")) or starting_cash,
                ending_cash=_finite(null_account.get("ending_equity")) or starting_cash,
                realized_pnl=_finite(null_account.get("realized_pnl")) or 0.0,
                trades=null_rows, replay_status="match")
        # A closing timestamp alone is not enough: malformed input or a
        # replay exception must remain diagnostic and keep the virtual open
        # blocked rather than silently declaring it settled.
        if complete and replay_ok:
            self.store.close_session_books(candidate_id, session)
        return complete

    @staticmethod
    def _append_worker_open(state: tuple[list[dict], dict[str, dict], float],
                            plan: Mapping[str, Any], symbol: str
                            ) -> tuple[list[dict], dict[str, dict], float]:
        """Advance one worker's private portfolio projection after an open."""
        positions, active_trades, gross_notional = state
        position = dict(plan)
        position["symbol"] = str(position.get("symbol") or symbol)
        risk_usd = _finite(position.get("risk_usd")) or 0.0
        notional = _finite(position.get("notional")) or 0.0
        active = dict(position)
        active["risk_usd"] = risk_usd
        positions.append(position)
        active_trades[str(symbol)] = active
        return positions, active_trades, gross_notional + notional

    def _evaluate_arm_snapshot(self, arm: Mapping[str, Any],
                               session_events: Mapping[str, Sequence[Mapping]],
                               session_inputs: Mapping[str, tuple[Sequence[Mapping],
                                                                   Sequence[Mapping],
                                                                   Sequence[Mapping]]],
                               bars: Mapping[str, Sequence[Mapping]],
                               quotes: Mapping[str, Sequence[Mapping]],
                               options: Mapping[str, Sequence[Mapping]],
                               initial_state: tuple[list[dict], dict[str, dict], float]
                               ) -> dict[str, Any]:
        """Evaluate one immutable arm without touching the shadow WAL.

        Every worker receives the same tuple-backed event snapshot.  The
        private portfolio projection reproduces within-arm admission for
        multiple events while keeping SQLite reads/writes out of the worker.
        """
        candidate_id = str(arm["candidate_id"])
        state = (list(initial_state[0]), dict(initial_state[1]),
                 float(initial_state[2]))
        self._worker_state.portfolios = {candidate_id: state}
        decisions: list[dict[str, Any]] = []
        try:
            for session in sorted(session_events):
                session_bars, session_quotes, session_options = session_inputs[session]
                for event in session_events[session]:
                    symbol = str(event.get("symbol") or "")
                    event_key = str(event.get("event_key") or "")
                    if any(str(row.get("symbol") or "") == symbol
                           for row in state[0]):
                        kind, reason, payload, plan = (
                            "no_trade", "virtual book has an incomplete open",
                            {"session_date": session,
                             "strategy_id": arm.get("strategy_id"),
                             "variant_id": arm.get("variant_id")}, None)
                    else:
                        kind, reason, payload, plan = self._evaluate(
                            arm, event, bars, quotes, options)
                    decisions.append({
                        "candidate_id": candidate_id,
                        "event_key": event_key,
                        "session_date": session,
                        "symbol": symbol,
                        "kind": kind,
                        "reason": reason,
                        "payload": payload,
                        "plan": plan,
                    })
                    if plan is not None:
                        state = self._append_worker_open(state, plan, symbol)
                        self._worker_state.portfolios[candidate_id] = state
                # Session replay is parent-owned and normally closes complete
                # virtual books before the next session.  Reset the private
                # projection at this boundary; an incomplete replay remains
                # durable in SQLite and blocks the next poll conservatively.
                state = ([], {}, 0.0)
                self._worker_state.portfolios[candidate_id] = state
        except Exception as exc:
            # The parent records the bounded diagnostic in its poll result and
            # continues sibling arms.  No partial worker output is committed,
            # so a retry can deterministically recompute this candidate.
            return {"candidate_id": candidate_id, "decisions": [],
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
        finally:
            try:
                del self._worker_state.portfolios
            except AttributeError:
                pass
        return {"candidate_id": candidate_id, "decisions": decisions,
                "error": None}

    def run_once(self) -> dict[str, Any]:
        # Factory hypotheses can be registered by the research cycle between
        # shadow polls; refresh the read-only root catalog for every pass.
        self._factory_roots = _read_factory_rule_roots(self.config.edge_db)
        candidates = _read_candidates(self.config.edge_db, max_candidates=self.config.max_candidates)
        for candidate in candidates:
            self.store.upsert_candidate(candidate)
        ingested = 0
        conflicts = 0
        invalid_events = 0
        sources = _corpus_sources(self.config.corpus_path)
        offsets = self.store.source_offsets()
        forward_floor = self.store.forward_event_floor()
        if forward_floor is None:
            # feaca71 established source offsets before filtering the legacy
            # event cache. Mark the migration boundary once and keep all later
            # cycles scoped to genuinely forward observations.
            forward_floor = (time.time() if self.store.event_count() >=
                             self.config.max_events else 0.0)
            self.store.save_forward_event_floor(forward_floor)
        if offsets is None:
            # A pre-upgrade WAL already at the old total-event ceiling has
            # consumed historical evidence. Baseline at the current committed
            # ends instead of replaying a multi-million-row recorder catch-up.
            offsets = ({str(source.resolve()): source.stat().st_size
                        for source in sources}
                       if self.store.event_count() >= self.config.max_events
                       else {})
            self.store.save_source_offsets(offsets)
        pending: list[dict] = []
        next_offsets = dict(offsets)
        source_consumed: dict[str, int] = {}
        source_for_event: dict[str, str] = {}
        pending_bytes = sum(max(0, source.stat().st_size - offsets.get(
            str(source.resolve()), 0)) for source in sources)
        skipped_recovery_bytes = 0
        if pending_bytes > MAX_PENDING_CORPUS_BYTES:
            # A failed pre-offset consumer may leave hundreds of megabytes of
            # raw quotes pending. Do not materialize that range merely to
            # compact it. Baseline it explicitly and quarantine the current
            # NY session so a partial forward window can never qualify.
            skipped_recovery_bytes = pending_bytes
            next_offsets = {str(source.resolve()): source.stat().st_size
                            for source in sources}
            forward_floor = time.time()
            self.store.save_forward_event_floor(forward_floor)
            self.store.save_quarantine_through_session(
                datetime.now(UTC).astimezone(NEW_YORK).date().isoformat())
        else:
            for source in sources:
                key = str(source.resolve())
                rows, consumed = _read_corpus_append(source, offsets.get(key, 0))
                pending.extend(rows)
                source_consumed[key] = consumed
                for row in rows:
                    event_key = str(row.get("event_key") or "")
                    if event_key:
                        source_for_event[event_key] = key
        selected = _compact_shadow_rows(pending)
        if len(selected) > self.config.max_events:
            raise ShadowError(
                f"shadow event batch bound {self.config.max_events} exceeded")
        quarantine = self.store.quarantine_events()
        invalid_sources: set[str] = set()
        resolved_quarantine: set[str] = set()
        invalid_sessions: set[str] = {
            str(detail.get("session_date"))
            for detail in quarantine.values()
            if isinstance(detail, Mapping) and detail.get("session_date")
        }
        unknown_quarantine = any(
            not isinstance(detail, Mapping) or not detail.get("session_date")
            for detail in quarantine.values())
        quarantine_overflow = QUARANTINE_OVERFLOW_KEY in quarantine
        for raw in selected:
            event_key = str(raw.get("event_key") or "")
            source_key = source_for_event.get(event_key)
            try:
                _, added = self.store.ingest_event(raw, max_events=self.config.max_events)
            except InputConflict:
                conflicts += 1
                raise
            except NormalizationError:
                # Keep the source offset before this malformed event.  The
                # event is explicitly quarantined and its local session is
                # blocked, so an operator can correct the recorder row and the
                # next poll will retry the exact same bytes.  Advancing over
                # it would permanently skip an unknown/incomplete session.
                invalid_events += 1
                if source_key:
                    invalid_sources.add(source_key)
                stamp = _timestamp(raw.get("as_of") or raw.get("timestamp"))
                session = (stamp.astimezone(NEW_YORK).date().isoformat()
                           if stamp is not None else None)
                if session:
                    invalid_sessions.add(session)
                else:
                    unknown_quarantine = True
                quarantine[event_key or _digest(raw)] = {
                    "event_key": event_key,
                    "source": source_key,
                    "session_date": session,
                    "reason": "normalization_error",
                }
                continue
            if event_key in quarantine:
                resolved_quarantine.add(event_key)
            if added:
                ingested += 1
        # Only commit offsets for sources whose complete forward batch was
        # normalized.  Other sources remain at their previous boundary and
        # are retried after correction; already-ingested rows are idempotent.
        for key, consumed in source_consumed.items():
            if key not in invalid_sources:
                next_offsets[key] = consumed
        for event_key in resolved_quarantine:
            quarantine.pop(event_key, None)
        # ``replace=True`` lets this poll remove an event after the exact
        # corrected bytes normalize; direct operator writes default to a
        # monotonic merge so unrelated quarantine evidence is preserved.
        self.store.save_quarantine_events(quarantine, replace=True)
        self.store.save_source_offsets(next_offsets)
        # Recompute the durable block after resolving corrected rows.  A
        # corrected replay can therefore become eligible in this same poll;
        # it does not require an operator to run an extra no-op cycle.
        invalid_sessions = {
            str(detail.get("session_date"))
            for detail in quarantine.values()
            if isinstance(detail, Mapping) and detail.get("session_date")
        }
        unknown_quarantine = any(
            not isinstance(detail, Mapping) or not detail.get("session_date")
            for detail in quarantine.values())
        quarantine_overflow = QUARANTINE_OVERFLOW_KEY in quarantine
        events, bars, quotes, options = self._load_events()
        # Persist only exact recorder/Alpaca calendar sessions.  This catalog
        # is the continuity authority used by ingestion; event timestamps or
        # weekday heuristics are intentionally insufficient (holidays and
        # early closes must remain represented by the recorder provenance).
        catalog = self.store.session_catalog()
        # Import completed calendar sessions even when a particular session
        # has no normalized events.  This is what makes an all-arm missing
        # middle session visible instead of letting the union of replay rows
        # silently skip it.
        for session, bounds in _recorded_session_calendar(self.config.corpus_path).items():
            catalog.setdefault(session, {
                "session_date": session,
                "open": bounds[0].isoformat(),
                "close": bounds[1].isoformat(),
                "source": "recorder_alpaca_calendar",
                "recorded_ts": time.time(),
            })
        for event in events:
            if event.get("event_type") not in {"bar", "bar_1m"}:
                continue
            stamp = _timestamp(event.get("as_of") or event.get("timestamp"))
            if stamp is None:
                continue
            session = stamp.astimezone(NEW_YORK).date().isoformat()
            bounds = _recorded_session_bounds(self.config.corpus_path, session)
            event_end = _event_end(event)
            if bounds is None or event_end is None or event_end < bounds[1]:
                continue
            catalog.setdefault(session, {
                "session_date": session,
                "open": bounds[0].isoformat(),
                "close": bounds[1].isoformat(),
                "source": "recorder_alpaca_calendar",
                "recorded_ts": time.time(),
            })
        if catalog != self.store.session_catalog():
            self.store.save_session_catalog(catalog)
        # Process one local session at a time.  A completed replay closes that
        # session's virtual books before the next session is evaluated, which
        # is essential when the recorder is catching up multiple sessions in
        # a single invocation.  Replay receives the complete symbol set for a
        # session; its diff row is unique by candidate/session and must not be
        # overwritten once per symbol.
        def row_session(row: Mapping[str, Any]) -> str | None:
            stamp = _timestamp(row.get("as_of") or row.get("timestamp"))
            return (stamp.astimezone(NEW_YORK).date().isoformat()
                    if stamp is not None else None)

        session_events: dict[str, list[dict]] = {}
        quarantine_through = self.store.quarantine_through_session()
        for event in events:
            if event.get("event_type") not in {"bar", "bar_1m"}:
                continue
            session = row_session(event)
            if session is not None and (
                    not unknown_quarantine and session not in invalid_sessions and
                    (quarantine_through is None or session > quarantine_through)):
                session_events.setdefault(session, []).append(event)

        session_inputs: dict[str, tuple[list[dict], list[dict], list[dict]]] = {}
        for session in sorted(session_events):
            session_inputs[session] = (
                [row for values in bars.values() for row in values
                 if row_session(row) == session],
                [row for values in quotes.values() for row in values
                 if row_session(row) == session],
                [row for values in options.values() for row in values
                 if row_session(row) == session],
            )

        # Freeze both arm definitions and event inputs before dispatch.  A
        # research poll that registers a candidate or appends a recorder row
        # while workers run therefore affects only the next poll.
        arms: list[dict[str, Any]] = []
        for candidate in candidates:
            if str(candidate.get("vehicle") or "equity") not in {"equity", "option"}:
                continue
            arms.append(dict(candidate))
            root_control = self._rule_root_control(candidate)
            if root_control is not None:
                arms.append(dict(root_control))
        arms.sort(key=lambda item: str(item.get("candidate_id") or ""))
        for arm in arms:
            self.store.upsert_candidate(arm)

        # Convert all worker inputs to detached JSON values and tuples.  The
        # tuples are never handed to a mutating path, making the poll snapshot
        # explicit even if a provider returns mutable row objects.
        frozen_events = tuple(json.loads(_json(dict(row))) for row in events)
        frozen_bars = {
            str(symbol): tuple(json.loads(_json(dict(row))) for row in values)
            for symbol, values in bars.items()}
        frozen_quotes = {
            str(symbol): tuple(json.loads(_json(dict(row))) for row in values)
            for symbol, values in quotes.items()}
        frozen_options = {
            str(symbol): tuple(json.loads(_json(dict(row))) for row in values)
            for symbol, values in options.items()}
        frozen_session_events: dict[str, tuple[dict, ...]] = {}
        for session, values in session_events.items():
            frozen_session_events[session] = tuple(
                json.loads(_json(dict(row))) for row in values)
        frozen_session_inputs: dict[str, tuple[tuple[dict, ...], tuple[dict, ...], tuple[dict, ...]]] = {}
        for session, (session_bars, session_quotes, session_options) in session_inputs.items():
            frozen_session_inputs[session] = (
                tuple(json.loads(_json(dict(row))) for row in session_bars),
                tuple(json.loads(_json(dict(row))) for row in session_quotes),
                tuple(json.loads(_json(dict(row))) for row in session_options),
            )
        event_watermark = {
            "count": len(frozen_events),
            "events_digest": _digest([
                {key: row.get(key) for key in (
                    "event_key", "digest", "event_type", "symbol",
                    "timestamp", "as_of")}
                for row in frozen_events]),
            "last_event_key": (str(frozen_events[-1].get("event_key") or "")
                                if frozen_events else None),
            "last_timestamp": (str(frozen_events[-1].get("timestamp") or "")
                                if frozen_events else None),
        }
        candidate_watermark = [{
            "candidate_id": str(arm.get("candidate_id") or ""),
            "variant_id": str(arm.get("variant_id") or ""),
            "strategy_id": str(arm.get("strategy_id") or ""),
            "vehicle": str(arm.get("vehicle") or ""),
            "status": str(arm.get("status") or ""),
            "config_digest": _digest(_safe_config(arm)),
        } for arm in arms]
        manifest = {
            "schema": "shadow-manifest.v1",
            "candidate_set": candidate_watermark,
            "candidate_set_digest": _digest(candidate_watermark),
            "source_watermark": {
                "offsets": {str(key): int(value) for key, value in next_offsets.items()},
                "forward_event_floor": float(forward_floor or 0.0),
            },
            "event_watermark": event_watermark,
            "session_watermark": sorted(str(session) for session in frozen_session_events),
            "max_workers": int(self.config.max_workers),
        }
        manifest_digest = self.store.save_manifest(manifest)

        # Dispatch one immutable session at a time.  The parent commits
        # decisions and performs replay before the next session is submitted,
        # preserving virtual-book blocking when a replay remains incomplete.
        # Workers still run candidate arms concurrently within each barrier.
        arm_by_id = {str(arm["candidate_id"]): arm for arm in arms}
        candidate_errors: dict[str, str] = {}
        failed_arms: set[str] = set()
        for session in sorted(frozen_session_events):
            session_events_one = {session: frozen_session_events[session]}
            session_inputs_one = {session: frozen_session_inputs[session]}
            active_arms = [arm for arm in arms
                           if str(arm["candidate_id"]) not in failed_arms]
            initial_states = {
                str(arm["candidate_id"]): self._portfolio_state(
                    str(arm["candidate_id"])) for arm in active_arms}
            worker_results: list[dict[str, Any]] = []
            if active_arms:
                with ThreadPoolExecutor(max_workers=self.config.max_workers,
                                        thread_name_prefix="shadow-eval") as pool:
                    futures = {
                        pool.submit(self._evaluate_arm_snapshot, arm,
                                    session_events_one, session_inputs_one,
                                    frozen_bars, frozen_quotes, frozen_options,
                                    initial_states[str(arm["candidate_id"])]): arm
                        for arm in active_arms}
                    for future in as_completed(futures):
                        arm = futures[future]
                        try:
                            worker_results.append(future.result())
                        except Exception as exc:  # pragma: no cover - defensive
                            worker_results.append({
                                "candidate_id": str(arm["candidate_id"]),
                                "decisions": [],
                                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                            })
            worker_results.sort(key=lambda item: str(item.get("candidate_id") or ""))

            # Stable candidate/event/session order is the sole write order.
            for result in worker_results:
                candidate_id = str(result.get("candidate_id") or "")
                if result.get("error"):
                    candidate_errors[candidate_id] = str(result["error"])
                    failed_arms.add(candidate_id)
                    continue
                decisions = sorted(result.get("decisions") or [], key=lambda item: (
                    str(item.get("event_key") or ""),
                    str(item.get("session_date") or ""),
                    str(item.get("symbol") or "")))
                for decision in decisions:
                    inserted = self.store.decision(
                        candidate_id=candidate_id,
                        event_key=str(decision.get("event_key") or ""),
                        session_date=str(decision.get("session_date") or ""),
                        symbol=str(decision.get("symbol") or ""),
                        kind=str(decision.get("kind") or "no_data"),
                        reason=decision.get("reason"),
                        payload=decision.get("payload") or {},
                        max_decisions=self.config.max_decisions)
                    if inserted and decision.get("plan") is not None:
                        self.store.virtual_open(
                            candidate_id=candidate_id,
                            decision_id=_digest({
                                "candidate_id": candidate_id,
                                "event_key": str(decision.get("event_key") or "")}),
                            symbol=str(decision.get("symbol") or ""),
                            plan=decision["plan"])

            # Replay is parent-only and runs before the next session barrier.
            session_bars, session_quotes, session_options = frozen_session_inputs[session]
            for result in worker_results:
                candidate_id = str(result.get("candidate_id") or "")
                if result.get("error"):
                    continue
                try:
                    rows = self.store.decisions(candidate_id)
                    self._replay(arm_by_id[candidate_id], session,
                                 session_bars, session_quotes, rows,
                                 session_options)
                except Exception as exc:
                    candidate_errors[candidate_id] = (
                        f"{type(exc).__name__}: {str(exc)[:240]}")
                    failed_arms.add(candidate_id)
        prune = self.store.prune()
        replay_quarantine = self.store.replay_quarantine()
        pending_repairs = [
            dict(detail) for detail in replay_quarantine.values()
            if isinstance(detail, Mapping) and detail.get("status") in {
                "quarantined", "overflow"}
        ]
        pending_repairs.sort(key=lambda item: (
            str(item.get("session_date") or ""),
            str(item.get("candidate_id") or "")))
        catalog = self.store.session_catalog()
        stale_tail = {
            "status": "blocked" if (
                invalid_sessions or unknown_quarantine or pending_repairs) else "clear",
            "sessions": sorted(invalid_sessions),
            "unknown_events": bool(unknown_quarantine),
            "quarantine_overflow": bool(quarantine_overflow),
            "invalid_events": int(invalid_events),
            "replay_repairs_required": len(pending_repairs),
            "replay_quarantine_sessions": sorted({
                str(item.get("session_date")) for item in pending_repairs
                if item.get("session_date")}),
            "replay_quarantine": pending_repairs[-64:],
            "authoritative_catalog_sessions": sorted(
                str(session) for session, detail in catalog.items()
                if isinstance(detail, Mapping)
                and str(detail.get("source") or "") == "recorder_alpaca_calendar")[-64:],
        }
        # Surface the latest per-candidate capacity summaries without copying
        # raw account rows into the heartbeat.  Replay details are already
        # bounded at write time; retain only a deterministic candidate/session
        # tail for the operational result.
        capacity: list[dict[str, Any]] = []
        for metadata in self.store.replay_metadata():
            details = metadata.get("details")
            summary = details.get("opportunity_capacity") if isinstance(details, Mapping) else None
            if not isinstance(summary, Mapping):
                continue
            capacity.append({
                "candidate_id": str(metadata.get("candidate_id") or ""),
                "session_date": str(metadata.get("session_date") or ""),
                "vehicle": str(metadata.get("vehicle") or ""),
                **dict(summary),
            })
        capacity = sorted(capacity, key=lambda item: (
            item["candidate_id"], item["session_date"]))[-64:]
        return {"candidates": len(candidates), "ingested_events": ingested,
                "events": len(events), "decisions": len(self.store.decisions()),
                "conflicts": conflicts, "invalid_events": invalid_events,
                "manifest_digest": manifest_digest,
                "candidate_errors": dict(sorted(candidate_errors.items())),
                "skipped_recovery_bytes": skipped_recovery_bytes,
                "quarantine_through_session": quarantine_through,
                "stale_tail": stale_tail,
                "replay_quarantine": pending_repairs[-64:],
                "opportunity_capacity": capacity,
                **prune}


def run_shadow_once(config: ShadowConfig) -> dict[str, Any]:
    """Convenience entrypoint used by operations and tests."""
    return ShadowRunner(config).run_once()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("runtime/research/recorded/data.csv"))
    parser.add_argument("--edge-db", type=Path, default=Path("runtime/research/edge_lab.sqlite3"))
    parser.add_argument("--shadow-db", type=Path, default=Path("runtime/research/shadow.sqlite3"))
    parser.add_argument("--once", action="store_true", help="ingest and evaluate one cycle")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = ShadowConfig(corpus_path=args.corpus, edge_db=args.edge_db,
                       shadow_db=args.shadow_db, poll_seconds=args.interval,
                       max_workers=args.max_workers)
    while True:
        try:
            print(json.dumps(run_shadow_once(cfg), sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval)))


__all__ = [
    "DEFAULT_EQUITY", "DEFAULT_MAX_WORKERS", "DEFAULT_RETENTION_DAYS", "InputConflict",
    "_opportunity_capacity", "REPLAY_QUARANTINE_META_KEY",
    "SESSION_CATALOG_META_KEY", "REPLAY_QUARANTINE_OVERFLOW_KEY",
    "ShadowConfig", "ShadowError",
    "ShadowRunner", "ShadowStore", "run_shadow_once", "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
