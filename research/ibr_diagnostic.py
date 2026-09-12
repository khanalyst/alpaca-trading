"""Bounded, broker-free forward replay for the registered IBR cohort.

The adapter deliberately delegates signal, setup, risk, cost, and account
transitions to :mod:`research.live_shadow`.  This module only validates and
projects a frozen JSONL source, invokes that shared evaluator, and summarizes
the resulting modeled account books into diagnostic rows.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .costs import ReplayPolicy
from .diagnostic_accounts import event_available_at, new_account_state
from .diagnostic_shadow import build_diagnostic_cohort, content_identity
from .factory_core import diagnose
from .live_shadow import (
    ShadowConfig,
    ShadowRunner,
    _RecordedSessionCalendarSnapshot,
)
from .market_data import NormalizationError, parse_timestamp
from .source_validation import (
    SourceValidationError,
    source_content_hash,
    source_paths,
    validate_source,
)


SCHEMA = "offline-forward-ibr-diagnostic.v1"
NEW_YORK = ZoneInfo("America/New_York")
EXPECTED_IBR_ARMS = 7

_BAR_KINDS = frozenset({"bar", "underlying", "underlying_bar"})
_QUOTE_KINDS = frozenset({
    "quote", "quote_snapshot", "equity_quote", "underlying_quote",
})
_OPTION_KINDS = frozenset({"option", "option_snapshot", "option_quote"})
_BROKER_ONLY_LIMITS = (
    "shortability", "buying_power", "pending_orders", "actual_fills",
)


class _Unavailable(ValueError):
    def __init__(self, *reason_codes: str, source: Mapping[str, Any] | None = None):
        super().__init__(", ".join(reason_codes))
        self.reason_codes = tuple(str(value) for value in reason_codes if value)
        self.source = dict(source or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _canonical_identity(value: Any) -> str:
    result = str(value or "").strip().lower().replace("-", "_")
    return "delayed_sip" if result == "delayed" else result


def _replay_scope() -> dict[str, Any]:
    return {
        "engine": "research.live_shadow.ShadowRunner",
        "mode": "offline_forward_observed_diagnostic",
        "shared_signal_setup_risk": True,
        "shared_cost_and_account_engine": True,
        "isolated_starting_cash_per_arm": True,
        "broker_equivalence": False,
        "unsupported_broker_observations": list(_BROKER_ONLY_LIMITS),
        "actual_fills": False,
        "authorizing": False,
    }


def _arm_index(
        arms: Sequence[Mapping[str, Any]], *,
        runtime_config: Mapping[str, Any],
        ) -> dict[str, dict[str, Any]]:
    if (isinstance(arms, (str, bytes, bytearray, Mapping)) or
            not isinstance(arms, Sequence)):
        raise TypeError("arms must be a sequence of diagnostic IBR mappings")
    if len(arms) != EXPECTED_IBR_ARMS:
        raise ValueError("offline IBR replay requires all seven registered arms")
    result: dict[str, dict[str, Any]] = {}
    cohort_ids: set[str] = set()
    candidate_ids: set[str] = set()
    code_ids: set[str] = set()
    runtime_config_identity = content_identity(runtime_config)
    for raw in arms:
        if not isinstance(raw, Mapping):
            raise TypeError("every offline IBR arm must be a mapping")
        # The evaluator receives a detached JSON projection so no runtime
        # helper can mutate the caller's frozen cohort/config objects.
        arm = json.loads(_canonical_json(dict(raw)))
        variant_id = str(arm.get("variant_id") or "")
        candidate_id = str(arm.get("candidate_id") or "")
        config = arm.get("config")
        marker = (config.get("diagnostic_shadow")
                  if isinstance(config, Mapping) else None)
        cohort_id = str(arm.get("cohort_identity") or "")
        if (not variant_id or variant_id in result or not candidate_id or
                candidate_id in candidate_ids or
                str(arm.get("strategy_id") or "") != "ibr" or
                arm.get("diagnostic_only") is not True or
                arm.get("authorizing") is not False or
                not isinstance(config, Mapping) or
                not isinstance(marker, Mapping) or
                marker.get("diagnostic_only") is not True or
                marker.get("authorizing") is not False or
                not cohort_id):
            raise ValueError("offline IBR arm contract is invalid")
        if (str(marker.get("cohort_identity") or "") != cohort_id or
                str(marker.get("runtime_config_identity") or "") !=
                runtime_config_identity or
                str(marker.get("config_identity") or "") !=
                str(arm.get("config_identity") or "") or
                str(marker.get("code_identity") or "") !=
                str(arm.get("code_identity") or "")):
            raise ValueError("offline IBR arm identity metadata conflicts")
        cohort_ids.add(cohort_id)
        candidate_ids.add(candidate_id)
        code_ids.add(str(arm.get("code_identity") or ""))
        result[variant_id] = arm
    if len(cohort_ids) != 1 or len(code_ids) != 1 or not next(iter(code_ids)):
        raise ValueError("offline IBR arms must share one frozen cohort identity")
    expected_cohort = build_diagnostic_cohort(
        runtime_config, code_identity=next(iter(code_ids)), include_ibr=True)
    expected = {
        str(arm["variant_id"]): arm
        for arm in expected_cohort["arms"]
        if arm.get("strategy_id") == "ibr"
    }
    if _canonical_json(result) != _canonical_json(expected):
        raise ValueError("offline IBR arms do not match the frozen cohort")
    return dict(sorted(result.items()))


def _arm_unavailable(arm: Mapping[str, Any], reasons: Sequence[str]) -> dict[str, Any]:
    return {
        "candidate_id": str(arm.get("candidate_id") or ""),
        "variant_id": str(arm.get("variant_id") or ""),
        "role": str(arm.get("role") or ""),
        "cohort_identity": str(arm.get("cohort_identity") or ""),
        "config_identity": str(arm.get("config_identity") or ""),
        "code_identity": str(arm.get("code_identity") or ""),
        "status": "unavailable",
        "outcome": "unavailable",
        "reason_codes": sorted(set(str(value) for value in reasons if value)),
        "rows": [],
        "diagnostic": None,
        "account": None,
        "decision_summary": None,
        "broker_equivalence": False,
        "actual_fills": False,
        "authorizing": False,
        "gate_eligible": False,
        "eligible": False,
        "promotion_eligible": False,
        "proofs": [],
    }


def _unavailable_report(
        arms: Mapping[str, Mapping[str, Any]], reasons: Sequence[str], *,
        source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    codes = sorted(set(str(value) for value in reasons if value))
    source_payload = dict(source or {})
    source_payload.setdefault("content_hash", None)
    source_payload.setdefault("validated_forward_only", False)
    source_payload.update({"diagnostic_only": True, "authorizing": False})
    return {
        "schema": SCHEMA,
        "status": "unavailable",
        "reason_codes": codes,
        "source": source_payload,
        "replay_scope": _replay_scope(),
        "arms": {
            variant_id: _arm_unavailable(arm, codes)
            for variant_id, arm in arms.items()
        },
        "diagnostic": None,
        "broker_equivalence": False,
        "actual_fills": False,
        "authorizing": False,
        "gate_eligible": False,
        "eligible": [],
        "promotion_eligible": False,
        "proofs": [],
    }


def _load_rows(data: Any, *, max_events: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def append(row: Any, *, label: str) -> None:
        if len(rows) >= max_events:
            raise _Unavailable(
                "event_budget_exhausted",
                source={"rows_at_least": max_events + 1, "content_hash": None})
        if not isinstance(row, Mapping):
            raise _Unavailable(
                "invalid_source_row",
                source={"row": label, "content_hash": None})
        rows.append(dict(row))

    if isinstance(data, (str, Path)):
        if str(data) == "-":
            raise _Unavailable("source_unavailable", source={
                "reason": "stdin must be materialized before offline replay",
                "content_hash": None,
            })
        try:
            paths = source_paths(data)
            for path in paths:
                with path.open(encoding="utf-8") as stream:
                    for number, line in enumerate(stream, 1):
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise _Unavailable("invalid_json", source={
                                "path": str(path), "row": number,
                                "reason": str(exc), "content_hash": None,
                            }) from exc
                        append(row, label=f"{path}:{number}")
        except _Unavailable:
            raise
        except (OSError, UnicodeError) as exc:
            raise _Unavailable("source_unavailable", source={
                "reason": str(exc), "content_hash": None,
            }) from exc
    else:
        if isinstance(data, Mapping) or not isinstance(data, Iterable):
            raise TypeError("data must be a JSONL path or iterable of mappings")
        for number, row in enumerate(data, 1):
            append(row, label=str(number))
    if not rows:
        raise _Unavailable("missing_events", source={
            "rows": 0, "content_hash": source_content_hash(()),
        })
    return rows


def _source_reason_codes(report: Mapping[str, Any]) -> list[str]:
    reasons = {"source_validation_failed"}
    counts = dict(report.get("source_mode_counts") or {})
    modes = {mode for mode, count in counts.items() if int(count)}
    if int(report.get("implicit_source_mode_rows") or 0):
        reasons.add("implicit_source_mode")
    if int(counts.get("historical_backfill") or 0):
        reasons.add("historical_source")
    if len(modes) > 1:
        reasons.add("mixed_source_modes")
    if int(report.get("future_observed_rows") or 0):
        reasons.add("future_observation")
    if int(report.get("late_forward_observation_rows") or 0):
        reasons.add("late_forward_observation")
    if report.get("errors"):
        reasons.add("invalid_source")
    return sorted(reasons)


def _source_summary(report: Mapping[str, Any], *, bars: int = 0,
                    quotes: int = 0, symbols: Sequence[str] = (),
                    sessions: Sequence[str] = (),
                    supplied_report_hash_verified: bool = False) -> dict[str, Any]:
    return {
        "rows": int(report.get("rows") or 0),
        "content_hash": report.get("content_hash"),
        "source_mode_counts": dict(report.get("source_mode_counts") or {}),
        "implicit_source_mode_rows": int(
            report.get("implicit_source_mode_rows") or 0),
        "future_observed_rows": int(report.get("future_observed_rows") or 0),
        "late_forward_observation_rows": int(
            report.get("late_forward_observation_rows") or 0),
        "latest_observed_at": report.get("latest_observed_at"),
        "providers": list(report.get("providers") or ()),
        "feeds": list(report.get("feeds") or ()),
        "kinds": list(report.get("kinds") or ()),
        "bars": int(bars),
        "quotes": int(quotes),
        "symbols": sorted(set(str(value) for value in symbols)),
        "sessions": sorted(set(str(value) for value in sessions)),
        "supplied_report_hash_verified": bool(supplied_report_hash_verified),
        "validated_forward_only": not bool(report.get("errors")),
        "diagnostic_only": True,
        "authorizing": False,
    }


def _event_type(row: Mapping[str, Any]) -> str:
    raw_kind = row.get("kind")
    if raw_kind in (None, ""):
        raw_kind = row.get("event_type") or "bar"
    kind = str(raw_kind).strip().lower()
    if kind in _BAR_KINDS or kind in {"bar_1m"}:
        return "bar_1m"
    if kind in _QUOTE_KINDS:
        return "quote"
    if kind in _OPTION_KINDS:
        return "option_snapshot"
    raise _Unavailable("unsupported_event_kind")


def _project_events(rows: Sequence[Mapping[str, Any]]) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]]]:
    projected: list[dict[str, Any]] = []
    wrappers: list[dict[str, Any]] = []
    event_keys: set[str] = set()
    for row in rows:
        payload = dict(row)
        event_type = _event_type(payload)
        supplied_type = payload.get("event_type")
        if supplied_type not in (None, ""):
            normalized = str(supplied_type).strip().lower()
            compatible = ((_BAR_KINDS | {"bar_1m"})
                          if event_type == "bar_1m" else
                          (_OPTION_KINDS | {"option_snapshot"})
                          if event_type == "option_snapshot" else
                          _QUOTE_KINDS)
            if normalized not in compatible:
                raise _Unavailable("event_type_conflict")
        payload["event_type"] = event_type
        event_key = payload.get("event_key")
        if event_key in (None, ""):
            body = dict(payload)
            body.pop("event_key", None)
            event_key = "offline:" + hashlib.sha256(
                _canonical_json(body).encode("utf-8")).hexdigest()
            payload["event_key"] = event_key
        if not isinstance(event_key, str) or not event_key.strip():
            raise _Unavailable("invalid_event_key")
        event_key = event_key.strip()
        if event_key in event_keys:
            raise _Unavailable("duplicate_event_key")
        event_keys.add(event_key)
        payload["event_key"] = event_key
        symbol = payload.get("symbol")
        if event_type == "option_snapshot":
            symbol = symbol or payload.get("underlying")
        if not isinstance(symbol, str) or not symbol.strip():
            raise _Unavailable("missing_symbol")
        payload["symbol"] = symbol.strip().upper()
        projected.append(payload)
        wrappers.append({
            "event_type": event_type,
            "symbol": payload["symbol"],
            "event_json": _canonical_json(payload),
        })
    return projected, wrappers


def _session_of(row: Mapping[str, Any]) -> str:
    timestamp = parse_timestamp(row.get("timestamp"), name="timestamp")
    return timestamp.astimezone(NEW_YORK).date().isoformat()


def _calendar_snapshot(
        rows: Sequence[Mapping[str, Any]], *, require_exact: bool,
        ) -> _RecordedSessionCalendarSnapshot:
    bounds: dict[str, tuple[datetime, datetime]] = {}
    missing = False
    for row in rows:
        if str(row.get("event_type") or "") not in {"bar_1m", "quote"}:
            continue
        session = _session_of(row)
        raw_open = row.get("session_open")
        raw_close = row.get("session_close")
        if (raw_open in (None, "")) != (raw_close in (None, "")):
            raise _Unavailable("calendar_conflict")
        if raw_open in (None, ""):
            missing = True
            continue
        opened = parse_timestamp(raw_open, name="session_open")
        closed = parse_timestamp(raw_close, name="session_close")
        if (opened >= closed or
                opened.astimezone(NEW_YORK).date().isoformat() != session or
                closed.astimezone(NEW_YORK).date().isoformat() != session):
            raise _Unavailable("calendar_conflict")
        prior = bounds.get(session)
        if prior is not None and prior != (opened, closed):
            raise _Unavailable("calendar_conflict")
        bounds[session] = (opened, closed)
        supplied_session = row.get("session_date")
        if supplied_session not in (None, "") and str(supplied_session) != session:
            raise _Unavailable("calendar_conflict")
    if require_exact and (missing or not bounds):
        raise _Unavailable("calendar_missing")
    entries = sorted(bounds.items())
    return _RecordedSessionCalendarSnapshot(
        tuple(session for session, _ in entries),
        tuple(value for _, value in entries))


def _validated_source(
        rows: Sequence[Mapping[str, Any]], *, runtime_config: Mapping[str, Any],
        source_report: Mapping[str, Any] | None) -> tuple[dict[str, Any],
                                                           list[dict[str, Any]],
                                                           list[dict[str, Any]],
                                                           _RecordedSessionCalendarSnapshot]:
    actual_hash = source_content_hash(rows)
    supplied_verified = False
    if source_report is not None:
        supplied_hash = source_report.get("content_hash")
        if not isinstance(supplied_hash, str) or supplied_hash != actual_hash:
            raise _Unavailable("source_report_hash_mismatch", source={
                "rows": len(rows), "content_hash": actual_hash,
            })
        supplied_verified = True
    if any(not isinstance(row.get("source_mode"), str) or
           str(row.get("source_mode")).strip().lower() != "forward_observed"
           for row in rows):
        # The shared live evaluator intentionally accepts only this literal;
        # reject aliases here instead of allowing them to disappear later as
        # apparently ordinary no-signal observations.
        try:
            report = validate_source(rows, diagnostic_only=True)
        except SourceValidationError as exc:
            report = dict(exc.report)
        counts = dict(report.get("source_mode_counts") or {})
        modes = {mode for mode, count in counts.items() if int(count)}
        reasons = {"non_forward_source"}
        if int(report.get("implicit_source_mode_rows") or 0):
            reasons.add("implicit_source_mode")
        if int(counts.get("historical_backfill") or 0):
            reasons.add("historical_source")
        if len(modes) > 1:
            reasons.add("mixed_source_modes")
        raise _Unavailable(*sorted(reasons), source=report)
    try:
        validated = validate_source(
            rows, diagnostic_only=False, expected_content_hash=actual_hash)
    except SourceValidationError as exc:
        report = dict(exc.report)
        report.update({"diagnostic_only": True, "authorizing": False})
        raise _Unavailable(
            *_source_reason_codes(report), source=report) from exc
    counts = dict(validated.get("source_mode_counts") or {})
    if (set(counts) != {"forward_observed"} or
            int(counts.get("forward_observed") or 0) != len(rows) or
            int(validated.get("implicit_source_mode_rows") or 0)):
        raise _Unavailable("non_forward_source", source=validated)

    try:
        projected, wrappers = _project_events(rows)
    except _Unavailable as exc:
        raise _Unavailable(*exc.reason_codes, source=validated) from exc
    policy = ReplayPolicy.from_config(runtime_config)
    expected_provider = _canonical_identity(policy.equity_provider)
    expected_feed = _canonical_identity(policy.equity_feed)
    bars = [row for row in projected if row["event_type"] == "bar_1m"]
    quotes = [row for row in projected if row["event_type"] == "quote"]
    symbols = sorted({str(row["symbol"]) for row in bars})
    provisional_source = _source_summary(
        validated, bars=len(bars), quotes=len(quotes), symbols=symbols,
        supplied_report_hash_verified=supplied_verified)
    if not bars:
        raise _Unavailable("missing_bars", source=provisional_source)
    if not quotes:
        raise _Unavailable("missing_quotes", source=provisional_source)
    for row in (*bars, *quotes):
        if _canonical_identity(row.get("provider")) != expected_provider:
            raise _Unavailable("provider_mismatch", source=provisional_source)
        if _canonical_identity(row.get("feed")) != expected_feed:
            raise _Unavailable("feed_mismatch", source=provisional_source)
    session_cfg = runtime_config.get("session")
    require_exact = bool(
        session_cfg.get("require_exact_calendar", False)
        if isinstance(session_cfg, Mapping) else False)
    try:
        calendar = _calendar_snapshot(projected, require_exact=require_exact)
    except _Unavailable as exc:
        raise _Unavailable(
            *exc.reason_codes, source=provisional_source) from exc
    except (NormalizationError, TypeError, ValueError) as exc:
        raise _Unavailable(
            "calendar_conflict", source=provisional_source) from exc
    sessions = sorted({_session_of(row) for row in bars})
    source = _source_summary(
        validated, bars=len(bars), quotes=len(quotes), symbols=symbols,
        sessions=sessions, supplied_report_hash_verified=supplied_verified)
    return source, projected, wrappers, calendar


def _session_inputs(
        projected: Sequence[Mapping[str, Any]],
        bars: Mapping[str, Sequence[Mapping[str, Any]]],
        quotes: Mapping[str, Sequence[Mapping[str, Any]]],
        options: Mapping[str, Sequence[Mapping[str, Any]]],
        ) -> tuple[dict[str, list[dict[str, Any]]],
                   dict[str, tuple[list[dict[str, Any]],
                                   list[dict[str, Any]],
                                   list[dict[str, Any]]]],
                   list[tuple[str, str]]]:
    session_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in projected:
        if str(row.get("event_type") or "") in {"bar_1m", "quote"}:
            session_events[_session_of(row)].append(dict(row))
    for rows in session_events.values():
        rows.sort(key=lambda row: (
            event_available_at(row), str(row.get("event_key") or "")))

    def rows_for_session(
            values: Mapping[str, Sequence[Mapping[str, Any]]], session: str,
            ) -> list[dict[str, Any]]:
        return [dict(row) for rows in values.values() for row in rows
                if _session_of(row) == session]

    inputs = {
        session: (
            rows_for_session(bars, session),
            rows_for_session(quotes, session),
            rows_for_session(options, session),
        )
        for session in sorted(session_events)
    }
    opportunities = sorted({
        (str(row.get("symbol") or "").upper(), _session_of(row))
        for rows in bars.values() for row in rows
    }, key=lambda value: (value[1], value[0]))
    return dict(session_events), inputs, opportunities


def _complete_opportunities(
        bars: Mapping[str, Sequence[Mapping[str, Any]]],
        calendar: _RecordedSessionCalendarSnapshot,
        ) -> dict[tuple[str, str], bool]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for raw_symbol, values in bars.items():
        symbol = str(raw_symbol).upper()
        for row in values:
            grouped[(symbol, _session_of(row))].append(row)
    result: dict[tuple[str, str], bool] = {}
    for key, rows in grouped.items():
        _symbol, session = key
        bounds = calendar.get(session)
        if bounds is None:
            # A non-exact policy intentionally defines the supplied source
            # boundary as terminal. Exact policies cannot reach this branch.
            result[key] = True
            continue
        opened, closed = bounds
        intervals = sorted((
            parse_timestamp(row.get("timestamp"), name="timestamp"),
            parse_timestamp(
                row.get("as_of") or row.get("timestamp"), name="as_of"),
        ) for row in rows)
        cursor = opened
        complete = bool(intervals)
        for started, ended in intervals:
            if ended <= opened or started >= closed:
                continue
            if (ended <= started or
                    (ended - started).total_seconds() != 60.0 or
                    started != cursor):
                complete = False
                break
            cursor = ended
            if cursor >= closed:
                break
        result[key] = bool(complete and cursor >= closed)
    return result


def _signal_decision(decision: Mapping[str, Any]) -> bool:
    payload = decision.get("payload")
    return isinstance(payload, Mapping) and isinstance(
        payload.get("signal"), Mapping)


def _no_trade_row(*, symbol: str, session: str, disposition: str,
                  reason: str, signal_opportunity: bool) -> dict[str, Any]:
    return {
        "opportunity_id": f"ibr:equity:{symbol}:{session}",
        "vehicle": "equity",
        "symbol": symbol,
        "session_date": session,
        "no_trade": True,
        "net_pnl": None,
        "gross_pnl": None,
        "return_value": None,
        "execution_disposition": disposition,
        "signal_opportunity": bool(signal_opportunity),
        "reject_reason": reason,
        "evidence_mode": "forward_observed",
        "diagnostic_only": True,
        "directional_authorizing": False,
        "authorizing": False,
        "broker_equivalence": False,
        "actual_fill": False,
    }


def _closed_row(position: Mapping[str, Any], *, session: str) -> dict[str, Any]:
    symbol = str(position.get("symbol") or "").upper()
    quantity = float(position["quantity"])
    gross = float(position["gross_pnl"])
    net = float(position["realized_pnl"])
    entry_fee = float(position.get("entry_fee") or 0.0)
    exit_fee = float(position.get("exit_fee") or 0.0)
    if not all(math.isfinite(value) for value in (
            quantity, gross, net, entry_fee, exit_fee)) or quantity <= 0:
        raise _Unavailable("invalid_account_result")
    if not math.isclose(gross - entry_fee - exit_fee, net,
                        rel_tol=1e-9, abs_tol=1e-8):
        raise _Unavailable("position_reconciliation_failed")
    return {
        "opportunity_id": f"ibr:equity:{symbol}:{session}",
        "vehicle": "equity",
        "symbol": symbol,
        "session_date": session,
        "no_trade": False,
        "direction": position.get("direction"),
        "quantity": quantity,
        "shares": quantity,
        "entry_timestamp": position.get("entry_timestamp"),
        "exit_timestamp": position.get("exit_timestamp"),
        "entry_event_key": position.get("entry_event_key"),
        "exit_event_key": position.get("exit_event_key"),
        "entry_reference": position.get("entry_reference"),
        "entry_price": position.get("entry_price"),
        "exit_reference": position.get("exit_reference"),
        "exit_price": position.get("exit_price"),
        "gross_pnl": gross,
        "net_pnl": net,
        "return_value": net,
        "fees_after_fill_prices": entry_fee + exit_fee,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "exit_reason": position.get("exit_reason"),
        "canonical_exit_reason": position.get("canonical_exit_reason"),
        "tie_broken": bool(position.get("tie_broken")),
        "gap_fill": bool(position.get("gap_fill")),
        "late_data_gap": bool(position.get("late_data_gap")),
        "deadline": position.get("deadline"),
        "evidence_mode": "forward_observed",
        "diagnostic_only": True,
        "directional_authorizing": False,
        "authorizing": False,
        "broker_equivalence": False,
        "actual_fill": False,
    }


def _outcome(diagnostic: Mapping[str, Any]) -> str:
    if not diagnostic.get("executed_trades"):
        if diagnostic.get("signal_execution_rejection_count"):
            return "execution_blocked"
        if diagnostic.get("data_rejection_count"):
            return "missing_data"
        if (diagnostic.get("no_signal_count") and
                not diagnostic.get("unclassified_no_trade_count")):
            return "no_signal"
        return "insufficient_observations"
    if diagnostic.get("evidence_status") == "insufficient_trade_sample":
        return "underpowered"
    expectancy = diagnostic.get("measured_net_expectancy")
    if (not isinstance(expectancy, (int, float)) or
            not math.isfinite(float(expectancy))):
        return "insufficient_observations"
    return ("positive_point_estimate" if float(expectancy) > 0 else
            "negative_point_estimate" if float(expectancy) < 0 else
            "flat_point_estimate")


def _arm_result(
        arm: Mapping[str, Any], evaluation: Mapping[str, Any], *,
        opportunities: Sequence[tuple[str, str]], starting_cash: float,
        complete_opportunities: Mapping[tuple[str, str], bool],
        ) -> dict[str, Any]:
    error = evaluation.get("error")
    if error:
        return _arm_unavailable(arm, ("arm_evaluation_failed",)) | {
            "error": str(error),
        }
    batch = evaluation.get("account_batch")
    if not isinstance(batch, Mapping):
        return _arm_unavailable(arm, ("arm_account_unavailable",))
    account_entry = batch.get("account")
    account = (account_entry.get("state")
               if isinstance(account_entry, Mapping) else None)
    if not isinstance(account, Mapping):
        return _arm_unavailable(arm, ("arm_account_unavailable",))
    decisions = [dict(value) for value in evaluation.get("decisions", ())
                 if isinstance(value, Mapping)]
    by_opportunity: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        key = (str(decision.get("symbol") or "").upper(),
               str(decision.get("session_date") or ""))
        by_opportunity[key].append(decision)

    closed: dict[tuple[str, str], Mapping[str, Any]] = {}
    opened: dict[tuple[str, str], Mapping[str, Any]] = {}
    all_positions: list[Mapping[str, Any]] = []
    for entry in batch.get("positions", ()):
        position = entry.get("state") if isinstance(entry, Mapping) else None
        if not isinstance(position, Mapping):
            continue
        all_positions.append(position)
        try:
            session = parse_timestamp(
                position.get("entry_timestamp"),
                name="entry_timestamp").astimezone(NEW_YORK).date().isoformat()
        except (NormalizationError, TypeError, ValueError):
            return _arm_unavailable(arm, ("invalid_account_result",))
        key = (str(position.get("symbol") or "").upper(), session)
        target = closed if position.get("status") == "closed" else opened
        if key in target:
            return _arm_unavailable(
                arm, ("multiple_positions_per_opportunity",))
        target[key] = position

    rows: list[dict[str, Any]] = []
    try:
        for symbol, session in opportunities:
            key = (symbol, session)
            if key in closed:
                rows.append(_closed_row(closed[key], session=session))
                continue
            related = by_opportunity.get(key, ())
            if key in opened or any(
                    str(decision.get("reason") or "") ==
                    "persistent diagnostic position is open"
                    for decision in related):
                rows.append(_no_trade_row(
                    symbol=symbol, session=session, disposition="refused",
                    reason="open_or_unpriced_position_at_replay_boundary",
                    signal_opportunity=False))
                continue
            signal_decisions = [decision for decision in related
                                if _signal_decision(decision)]
            missing = next((decision for decision in signal_decisions
                            if str(decision.get("kind") or "") in {
                                "unpriced", "open_incomplete", "no_data",
                            }), None)
            if missing is not None:
                rows.append(_no_trade_row(
                    symbol=symbol, session=session, disposition="refused",
                    reason=str(missing.get("reason") or
                               "unpriced_signal_at_replay_boundary"),
                    signal_opportunity=False))
                continue
            refused = next((decision for decision in signal_decisions
                            if str(decision.get("kind") or "") == "reject"), None)
            if refused is not None:
                rows.append(_no_trade_row(
                    symbol=symbol, session=session, disposition="refused",
                    reason=str(refused.get("reason") or "signal_refused"),
                    signal_opportunity=True))
                continue
            terminal = related[-1] if related else None
            if (isinstance(terminal, Mapping) and
                    str(terminal.get("kind") or "") in {
                        "unpriced", "no_data", "open_incomplete",
                    }):
                rows.append(_no_trade_row(
                    symbol=symbol, session=session, disposition="refused",
                    reason=str(terminal.get("reason") or
                               "unpriced_terminal_observation"),
                    signal_opportunity=False))
                continue
            unpriced = next((decision for decision in related
                             if str(decision.get("kind") or "") == "unpriced"),
                            None)
            blocking_no_data = next((
                decision for decision in related
                if str(decision.get("kind") or "") == "no_data" and
                str(decision.get("reason") or "") != "insufficient bars"
            ), None)
            if unpriced is not None or blocking_no_data is not None:
                missing_decision = unpriced or blocking_no_data or {}
                rows.append(_no_trade_row(
                    symbol=symbol, session=session, disposition="refused",
                    reason=str(missing_decision.get("reason") or
                               "incomplete_market_data_coverage"),
                    signal_opportunity=False))
                continue
            if (complete_opportunities.get(key, False) and
                    any(str(decision.get("kind") or "") == "no_trade"
                        for decision in related)):
                no_signal = next((
                    decision for decision in reversed(related)
                    if str(decision.get("kind") or "") == "no_trade"
                ), None)
                rows.append(_no_trade_row(
                    symbol=symbol, session=session, disposition="no_signal",
                    reason=str((no_signal or {}).get("reason") or "no_signal"),
                    signal_opportunity=False))
            else:
                rows.append(_no_trade_row(
                    symbol=symbol, session=session, disposition="refused",
                    reason="incomplete_session_observation",
                    signal_opportunity=False))
    except _Unavailable as exc:
        return _arm_unavailable(arm, exc.reason_codes)

    closed_net = sum(float(position["realized_pnl"])
                     for position in all_positions
                     if position.get("status") == "closed")
    row_net = sum(float(row["net_pnl"]) for row in rows
                  if row.get("no_trade") is not True)
    account_realized = account.get("realized_pnl")
    if (not isinstance(account_realized, (int, float)) or
            not math.isfinite(float(account_realized)) or
            not math.isclose(closed_net, float(account_realized),
                             rel_tol=1e-9, abs_tol=1e-8) or
            not math.isclose(row_net, float(account_realized),
                             rel_tol=1e-9, abs_tol=1e-8)):
        return _arm_unavailable(arm, ("account_reconciliation_failed",))
    if not rows:
        return _arm_unavailable(arm, ("empty_opportunity_set",))
    try:
        diagnostic = diagnose(
            rows, starting_cash=float(starting_cash), diagnostic_only=True)
    except (TypeError, ValueError, OverflowError) as exc:
        return _arm_unavailable(arm, ("diagnostic_failed",)) | {
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }
    kind_counts = Counter(str(value.get("kind") or "") for value in decisions)
    reason_counts = Counter(str(value.get("reason") or "") for value in decisions)
    return {
        "candidate_id": str(arm.get("candidate_id") or ""),
        "variant_id": str(arm.get("variant_id") or ""),
        "role": str(arm.get("role") or ""),
        "cohort_identity": str(arm.get("cohort_identity") or ""),
        "config_identity": str(arm.get("config_identity") or ""),
        "code_identity": str(arm.get("code_identity") or ""),
        "status": "measured",
        "outcome": _outcome(diagnostic),
        "reason_codes": [],
        "rows": rows,
        "diagnostic": diagnostic,
        "account": {
            "starting_cash": float(account.get("starting_cash")),
            "cash": float(account.get("cash")),
            "equity": (None if account.get("equity") is None else
                       float(account["equity"])),
            "realized_pnl": float(account_realized),
            "unrealized_pnl": (None if account.get("unrealized_pnl") is None else
                               float(account["unrealized_pnl"])),
            "open_positions": int(account.get("open_position_count") or 0),
            "closed_positions": int(account.get("closed_position_count") or 0),
            "orders": int(account.get("order_count") or 0),
            "modeled_fills": int(account.get("fill_count") or 0),
            "mark_status": account.get("mark_status"),
            "signal_sessions": dict(account.get("signal_sessions") or {}),
            "last_event_key": account.get("last_event_key"),
            "last_event_at": account.get("last_event_at"),
            "state_digest": account.get("state_digest"),
            "realized_pnl_reconciled": True,
        },
        "decision_summary": {
            "total": len(decisions),
            "signal_opportunities": sum(
                1 for decision in decisions if _signal_decision(decision)),
            "by_kind": dict(sorted(kind_counts.items())),
            "by_reason": dict(sorted(reason_counts.items())),
        },
        "broker_equivalence": False,
        "actual_fills": False,
        "authorizing": False,
        "gate_eligible": False,
        "eligible": False,
        "promotion_eligible": False,
        "proofs": [],
    }


def run_offline_forward_ibr(
        data: str | Path | Iterable[Mapping[str, Any]], *,
        runtime_config: Mapping[str, Any],
        arms: Sequence[Mapping[str, Any]],
        starting_cash: float = 100_000.0,
        source_report: Mapping[str, Any] | None = None,
        max_events: int = 100_000) -> dict[str, Any]:
    """Replay one bounded forward-observed source through all seven IBR arms.

    Source defects are evidence unavailability, not zero-return measurements.
    Programmer contract errors (invalid runtime config, arm set, or numeric
    bounds) raise immediately before source or shadow engine work.
    """
    if not isinstance(runtime_config, Mapping):
        raise TypeError("runtime_config must be a mapping")
    if (isinstance(starting_cash, bool) or
            not math.isfinite(float(starting_cash)) or float(starting_cash) <= 0):
        raise ValueError("starting_cash must be positive and finite")
    if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
        raise ValueError("max_events must be a positive integer")
    if source_report is not None and not isinstance(source_report, Mapping):
        raise TypeError("source_report must be a mapping when supplied")
    arm_map = _arm_index(arms, runtime_config=runtime_config)
    try:
        rows = _load_rows(data, max_events=max_events)
        source, projected, wrappers, calendar = _validated_source(
            rows, runtime_config=runtime_config, source_report=source_report)
        worker = ShadowRunner.for_offline_diagnostic(ShadowConfig(
            corpus_path=Path("offline-forward-ibr.jsonl"),
            edge_db=Path("offline-forward-ibr-edge.sqlite3"),
            shadow_db=Path("offline-forward-ibr-shadow.sqlite3"),
            diagnostic=True, diagnostic_include_ibr=True,
            runtime_config=runtime_config, max_events=max_events,
            stress_calibration_enabled=False))
        bars, quotes, options = worker._group_event_rows(wrappers)
        normalized = sum(len(values) for grouped in (bars, quotes, options)
                         for values in grouped.values())
        if normalized != len(projected):
            raise _Unavailable("normalization_rejected", source=source)
        session_events, inputs, opportunities = _session_inputs(
            projected, bars, quotes, options)
        complete_opportunities = _complete_opportunities(bars, calendar)
        if not opportunities:
            raise _Unavailable("empty_opportunity_set", source=source)
        identities = {
            worker._diagnostic_market_view_identity(arm)
            for arm in arm_map.values()
        }
        if len(identities) != 1:
            raise _Unavailable("arm_market_policy_conflict", source=source)
        first_arm = next(iter(arm_map.values()))
        market_views = worker._build_diagnostic_market_views(
            first_arm,
            [row for row in projected if row["event_type"] == "bar_1m"],
            bars, quotes, options, calendar)
    except _Unavailable as exc:
        return _unavailable_report(
            arm_map, exc.reason_codes, source=exc.source)
    except (NormalizationError, TypeError, ValueError) as exc:
        return _unavailable_report(
            arm_map, ("source_preparation_failed",), source={
                "reason": f"{type(exc).__name__}: {str(exc)[:240]}",
                "content_hash": None,
            })

    results: dict[str, dict[str, Any]] = {}
    for variant_id, arm in arm_map.items():
        account = new_account_state(
            cohort_identity=str(arm["cohort_identity"]),
            candidate_id=str(arm["candidate_id"]),
            starting_cash=float(starting_cash))
        evaluation = worker._evaluate_diagnostic_arm_snapshot(
            arm, session_events, inputs, bars, quotes, options,
            {"account": account, "positions": []},
            calendar_snapshot=calendar,
            diagnostic_market_views=market_views)
        results[variant_id] = _arm_result(
            arm, evaluation, opportunities=opportunities,
            starting_cash=float(starting_cash),
            complete_opportunities=complete_opportunities)

    failed = sorted(variant_id for variant_id, result in results.items()
                    if result.get("status") != "measured")
    if failed:
        reasons = sorted({
            str(reason)
            for variant_id in failed
            for reason in results[variant_id].get("reason_codes", ())
        } or {"arm_evaluation_failed"})
        return {
            "schema": SCHEMA,
            "status": "unavailable",
            "reason_codes": reasons,
            "source": source,
            "replay_scope": _replay_scope(),
            "arms": results,
            "diagnostic": None,
            "broker_equivalence": False,
            "actual_fills": False,
            "authorizing": False,
            "gate_eligible": False,
            "eligible": [],
            "promotion_eligible": False,
            "proofs": [],
        }
    outcomes = Counter(str(result["outcome"]) for result in results.values())
    return {
        "schema": SCHEMA,
        "status": "measured",
        "reason_codes": [],
        "source": source,
        "replay_scope": _replay_scope(),
        "arms": results,
        "diagnostic": {
            "arms_measured": len(results),
            "outcome_counts": dict(sorted(outcomes.items())),
            "opportunities_per_arm": len(opportunities),
        },
        "broker_equivalence": False,
        "actual_fills": False,
        "authorizing": False,
        "gate_eligible": False,
        "eligible": [],
        "promotion_eligible": False,
        "proofs": [],
    }


__all__ = ["SCHEMA", "run_offline_forward_ibr"]
