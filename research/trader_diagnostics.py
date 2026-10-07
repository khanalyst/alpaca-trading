"""Pure, non-authorizing diagnostics for replayed strategy arms.

The factory replay produces rows for both executed trades and observations that
could not be executed.  This module keeps those two populations visible while
making descriptive economics and cross-arm dependence easy to inspect.  It
does not select a variant, authorize a trade, or write any state.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import itertools
import json
import math
from statistics import mean, median
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from agent.contracts.rule import canonical_exit_reason


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
SCHEMA = "trader-diagnostics.v1"
DEFAULT_MIN_SHARED_SESSIONS = 30
_CANONICAL_REASONS = frozenset({
    "stop", "target", "max_hold", "thesis_deadline",
    "session_force_flat", "data_discontinuity", "unknown",
})
_DISCONTINUITY_TOKENS = frozenset({
    "gap", "data_gap", "missing_data", "missing_bar", "discontinuity",
    "data_discontinuity", "internal_gap", "entry_bar_missing",
    "right_censored",
})


def _finite(value: Any) -> float | None:
    """Return a finite float, rejecting booleans and JSON non-finite values."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            text = str(value or "").strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _json_safe(value: Any) -> Any:
    """Recursively make arbitrary metadata finite and JSON-compatible."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value)


def _session(row: Mapping[str, Any]) -> str | None:
    value = row.get("session_date", row.get("session"))
    if value is not None:
        if isinstance(value, datetime):
            stamp = _timestamp(value)
            return stamp.astimezone(NEW_YORK).date().isoformat() if stamp else None
        text = _text(value)
        if text:
            return text[:10]
    stamp = _timestamp(row.get("entry_timestamp") or row.get("timestamp"))
    return stamp.astimezone(NEW_YORK).date().isoformat() if stamp else None


def _is_discontinuity_token(value: Any) -> bool:
    token = _text(value).lower().replace("-", "_").replace(" ", "_")
    return token in _DISCONTINUITY_TOKENS or "discontinu" in token


def _explicit_discontinuity(row: Mapping[str, Any]) -> bool:
    for name in (
            "hold_discontinuity", "hold_discontinuity_exit", "discontinuity",
            "data_discontinuity", "gap_detected"):
        if row.get(name) is True:
            return True
    for name in ("censor_reason", "discontinuity_kind", "hold_discontinuity_kind",
                 "exit_reason_detail", "unpriced_reason", "coverage_issue"):
        value = row.get(name)
        if isinstance(value, str) and _is_discontinuity_token(value):
            return True
        if isinstance(value, (list, tuple, set, frozenset)) and any(
                _is_discontinuity_token(item) for item in value):
            return True
    return False


def _is_discontinuity(row: Mapping[str, Any], reason: str) -> bool:
    return reason == "data_discontinuity" or _explicit_discontinuity(row)


def _right_censored(row: Mapping[str, Any], reason: str) -> bool:
    if _is_discontinuity(row, reason):
        return True
    return bool(row.get("right_censored") is True or row.get("censored") is True)


def _canonical_reason(row: Mapping[str, Any]) -> str:
    """Resolve one exit cause while treating an explicit canonical field as authoritative."""
    if "canonical_exit_reason" in row and row.get("canonical_exit_reason") not in (None, ""):
        value = _text(row.get("canonical_exit_reason")).lower().replace("-", "_")
        if value in _CANONICAL_REASONS:
            return value
        # A few older persisted rows used a discontinuity alias in the field
        # that later became canonical.  Preserve that explicit cause while
        # never translating an explicit ``unknown`` into legacy ``time``.
        return "data_discontinuity" if _is_discontinuity_token(value) else "unknown"
    detail = row.get("exit_reason_detail")
    discontinuity = _explicit_discontinuity(row) or _is_discontinuity_token(detail)
    raw = row.get("exit_reason", detail)
    if _is_discontinuity_token(raw):
        discontinuity = True
    try:
        result = canonical_exit_reason(raw, discontinuity=discontinuity)
    except (TypeError, ValueError):
        result = "unknown"
    return result if result in _CANONICAL_REASONS else "unknown"


def _discontinuity_detail(row: Mapping[str, Any], reason: str) -> str:
    for name in (
            "hold_discontinuity_kind", "discontinuity_kind", "censor_reason",
            "exit_reason_detail", "unpriced_reason"):
        value = row.get(name)
        if value not in (None, ""):
            if isinstance(value, (str, int, float, bool)):
                return _text(value)
            return _text(value, "unknown")
    return reason if reason == "data_discontinuity" else "unknown"


def _executed(row: Mapping[str, Any]) -> bool:
    if row.get("no_trade") is True:
        return False
    disposition = _text(row.get("execution_disposition")).lower()
    return disposition not in {"no_signal", "refused", "unpriced"}


def _symbol(row: Mapping[str, Any]) -> str:
    return _text(row.get("symbol"), "unknown").upper() or "unknown"


def _direction(row: Mapping[str, Any]) -> str:
    return _text(row.get("direction"), "unknown").lower() or "unknown"


def _time_bucket(row: Mapping[str, Any]) -> str:
    for name in ("time_bucket", "entry_time_bucket", "bucket"):
        value = row.get(name)
        if value not in (None, ""):
            return _text(value, "unknown")
    stamp = _timestamp(row.get("entry_timestamp"))
    if stamp is None:
        return "unknown"
    local = stamp.astimezone(NEW_YORK)
    minutes = local.hour * 60 + local.minute
    start = 9 * 60 + 30
    if minutes < start or minutes >= 16 * 60:
        return "outside_session"
    offset = minutes - start
    bucket_start = start + (offset // 30) * 30
    bucket_end = min(bucket_start + 30, 16 * 60)
    def label(total: int) -> str:
        return f"{total // 60:02d}:{total % 60:02d}"
    return f"{label(bucket_start)}-{label(bucket_end)}"


def _opportunity_key(variant_id: str, row: Mapping[str, Any], index: int) -> str:
    for name in ("opportunity_id", "opportunity", "signal_id", "trade_id"):
        value = row.get(name)
        if value not in (None, ""):
            return _text(value)
    pieces = (_session(row), _symbol(row), _direction(row),
              _text(row.get("entry_timestamp")))
    if any(pieces):
        return "fallback:" + "|".join(_text(item, "") for item in pieces)
    return f"row:{variant_id}:{index}"


def _row_fees(row: Mapping[str, Any]) -> tuple[float | None, str | None]:
    for name in ("fees", "costs", "total_fees", "fee_cost"):
        if name in row:
            value = _finite(row.get(name))
            return value, name if value is not None else None
    gross = _finite(row.get("gross_pnl"))
    net = _finite(row.get("net_pnl"))
    if gross is not None and net is not None:
        return gross - net, "derived_gross_minus_net"
    return None, None


def _add(mapping: dict[str, float], key: str, value: float | None) -> None:
    if value is not None and math.isfinite(value):
        mapping[key] = mapping.get(key, 0.0) + value


def _counts(mapping: Mapping[str, int]) -> dict[str, int]:
    return dict(sorted((str(key), int(value)) for key, value in mapping.items()))


def _pnl_map(mapping: Mapping[str, float]) -> dict[str, float]:
    return {str(key): float(value) for key, value in sorted(mapping.items())
            if math.isfinite(float(value))}


def _holding_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    invalid = 0
    for row in rows:
        entry = _timestamp(row.get("entry_timestamp"))
        exit_ = _timestamp(row.get("exit_timestamp"))
        if entry is None or exit_ is None or exit_ < entry:
            invalid += 1
            continue
        values.append((exit_ - entry).total_seconds())
    if not values:
        return {"available": False, "count": 0, "mean_seconds": None,
                "median_seconds": None, "min_seconds": None,
                "max_seconds": None, "invalid_timestamp_rows": invalid}
    return {"available": True, "count": len(values),
            "mean_seconds": mean(values), "median_seconds": median(values),
            "min_seconds": min(values), "max_seconds": max(values),
            "invalid_timestamp_rows": invalid}


def _session_membership(value: Any, session: str) -> bool | None:
    """Return membership for an explicitly supplied session collection."""
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        values = (value,)
    else:
        try:
            values = tuple(value)
        except TypeError:
            return None
    return session in {str(item)[:10] for item in values}


def _metadata_sessions(metadata: Mapping[str, Any] | None,
                       session: str) -> tuple[bool | None, bool | None]:
    """Return explicit metadata flags, preserving unknown coverage as ``None``."""
    if not isinstance(metadata, Mapping):
        return None, None
    statuses = metadata.get("session_status")
    status = statuses.get(session) if isinstance(statuses, Mapping) else None
    if isinstance(status, Mapping):
        complete = status.get("complete")
        eligible = status.get("eligible")
        return (None if complete is None else complete is True,
                None if eligible is None else eligible is True)
    return (_session_membership(metadata.get("complete_sessions"), session),
            _session_membership(metadata.get("eligible_sessions"), session))


def _row_session_flag(row: Mapping[str, Any], names: tuple[str, ...]) -> bool | None:
    """Read one explicit row-level coverage flag without treating omission as true."""
    present = [row[name] for name in names if name in row]
    if not present:
        return None
    return all(value is True for value in present)


def _session_pnl(rows: Iterable[Mapping[str, Any]],
                 metadata: Mapping[str, Any] | None) -> dict[str, float]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if _executed(row):
            session = _session(row)
            if session:
                grouped[session].append(row)
    result: dict[str, float] = {}
    for session, items in grouped.items():
        meta_complete, meta_eligible = _metadata_sessions(metadata, session)
        row_complete = all(
            _row_session_flag(row, ("session_complete", "complete")) is True
            for row in items)
        row_eligible = all(
            _row_session_flag(row, ("session_eligible", "eligible")) is True
            for row in items)
        metadata_disqualifies = (meta_complete is False or
                                 meta_eligible is False)
        complete = ((meta_complete is True and meta_eligible is True) or
                    (not metadata_disqualifies and row_complete and row_eligible))
        for row in items:
            if row.get("session_complete") is False or row.get("complete") is False:
                complete = False
            if row.get("session_eligible") is False or row.get("eligible") is False:
                complete = False
            if _right_censored(row, _canonical_reason(row)):
                complete = False
            pnl = _finite(row.get("net_pnl"))
            if pnl is None:
                complete = False
        if not complete:
            continue
        values = [_finite(row.get("net_pnl")) for row in items]
        if all(value is not None for value in values):
            result[session] = float(sum(value for value in values if value is not None))
    return result


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean, right_mean = mean(left), mean(right)
    left_dev = [item - left_mean for item in left]
    right_dev = [item - right_mean for item in right]
    left_ss = sum(item * item for item in left_dev)
    right_ss = sum(item * item for item in right_dev)
    if left_ss <= 0.0 or right_ss <= 0.0:
        return None
    value = sum(a * b for a, b in zip(left_dev, right_dev)) / math.sqrt(left_ss * right_ss)
    return value if math.isfinite(value) else None


def _arm_report(variant_id: str, rows: list[Mapping[str, Any]],
                metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    executed = [row for row in rows if _executed(row)]
    reasons: Counter[str] = Counter()
    details: Counter[str] = Counter()
    net_by_exit: dict[str, float] = {}
    net_by_symbol: dict[str, float] = {}
    net_by_direction: dict[str, float] = {}
    net_by_bucket: dict[str, float] = {}
    net_by_session: dict[str, float] = {}
    coverage_counts: Counter[str] = Counter()
    coverage_rows: list[dict[str, Any]] = []
    invalid_metrics: Counter[str] = Counter()
    gross_total = 0.0
    fees_total = 0.0
    net_total = 0.0
    gross_count = fees_count = net_count = 0
    opportunity_keys: list[str] = []
    opportunity_rows = 0
    censored = 0
    for index, row in enumerate(rows):
        key = _opportunity_key(variant_id, row, index)
        if (_executed(row) or row.get("signal_opportunity") is True):
            opportunity_keys.append(key)
            opportunity_rows += 1
        if not _executed(row):
            reason = _text(row.get("reject_reason") or row.get("unpriced_reason") or
                           row.get("no_signal_reason") or row.get("execution_disposition"),
                           "no_trade")
            coverage_counts[reason] += 1
            coverage_rows.append({
                "row_index": index, "opportunity_id": key,
                "session": _session(row), "reason": reason,
            })
            continue
        reason = _canonical_reason(row)
        reasons[reason] += 1
        if reason == "data_discontinuity":
            details[_discontinuity_detail(row, reason)] += 1
        is_censored = _right_censored(row, reason)
        censored += int(is_censored)
        if is_censored:
            issue = _discontinuity_detail(row, reason)
            coverage_counts[issue] += 1
            issue_row: dict[str, Any] = {
                "row_index": index, "opportunity_id": key,
                "session": _session(row), "reason": issue,
                "canonical_exit_reason": reason,
            }
            pnl = _finite(row.get("net_pnl"))
            if pnl is not None:
                issue_row["net_pnl"] = pnl
            coverage_rows.append(issue_row)
        net = _finite(row.get("net_pnl"))
        if net is None:
            invalid_metrics["net_pnl"] += 1
        else:
            net_count += 1
            net_total += net
            _add(net_by_exit, reason, net)
            _add(net_by_symbol, _symbol(row), net)
            _add(net_by_direction, _direction(row), net)
            _add(net_by_bucket, _time_bucket(row), net)
            session = _session(row) or "unknown"
            _add(net_by_session, session, net)
        gross = _finite(row.get("gross_pnl"))
        if gross is None:
            if "gross_pnl" in row:
                invalid_metrics["gross_pnl"] += 1
        else:
            gross_count += 1
            gross_total += gross
        fees, _fee_source = _row_fees(row)
        if fees is None:
            if any(name in row for name in ("fees", "costs", "total_fees", "fee_cost")):
                invalid_metrics["fees"] += 1
        else:
            fees_count += 1
            fees_total += fees
    opportunity_count = len(set(opportunity_keys))
    unique_sessions = sorted({session for session in (_session(row) for row in rows)
                              if session})
    discontinuity_count = sum(
        1 for row in executed
        if _is_discontinuity(row, _canonical_reason(row)))
    pnl_basis = "gross_at_modeled_fill_prices_minus_fees"
    return {
        "variant_id": variant_id,
        "metadata": _json_safe(metadata) if metadata is not None else {},
        "total_rows": len(rows),
        "executed_trades": len(executed),
        "valid_net_pnl_trades": net_count,
        "unique_opportunities": opportunity_count,
        "duplicate_opportunity_rows": opportunity_rows - opportunity_count,
        "sessions": len(unique_sessions),
        "session_ids": unique_sessions,
        "canonical_exit_counts": _counts(reasons),
        "data_discontinuity_details": _counts(details),
        "discontinuity_count": discontinuity_count,
        "right_censored_count": censored,
        "discontinuity_fraction": (
            discontinuity_count / len(executed) if executed else None),
        "right_censored_fraction": (censored / len(executed) if executed else None),
        "net_pnl": net_total if net_count else None,
        "gross_pnl_at_modeled_fill_prices": gross_total if gross_count else None,
        "fees": fees_total if fees_count else None,
        "pnl_basis": pnl_basis,
        "net_pnl_by_exit": _pnl_map(net_by_exit),
        "net_pnl_by_symbol": _pnl_map(net_by_symbol),
        "net_pnl_by_direction": _pnl_map(net_by_direction),
        "net_pnl_by_time_bucket": _pnl_map(net_by_bucket),
        "net_pnl_by_session": _pnl_map(net_by_session),
        "holding_time": _holding_summary(executed),
        "coverage_issues": {
            "counts": _counts(coverage_counts),
            "rows": _json_safe(coverage_rows),
            "retained": True,
        },
        "invalid_metric_counts": _counts(invalid_metrics),
        "authorizing": False,
        "diagnostic_only": True,
        "_session_pnl": _session_pnl(rows, metadata),
    }


def _cross_arm(arms: Mapping[str, Mapping[str, Any]],
               *, min_shared_sessions: int) -> dict[str, Any]:
    pair_reports: list[dict[str, Any]] = []
    for left_id, right_id in itertools.combinations(sorted(arms), 2):
        left = arms[left_id].get("_session_pnl", {})
        right = arms[right_id].get("_session_pnl", {})
        shared = sorted(set(left) & set(right))
        left_values = [float(left[session]) for session in shared]
        right_values = [float(right[session]) for session in shared]
        report: dict[str, Any] = {
            "variant_a": left_id, "variant_b": right_id,
            "method": "pearson_session_pnl",
            "min_shared_sessions": min_shared_sessions,
            "shared_complete_eligible_sessions": len(shared),
            "shared_sessions": shared,
            "measured": False, "measurement": "unmeasured",
            "status": "insufficient_shared_sessions",
            "unmeasured_reason": "insufficient_shared_sessions",
            "correlation": None,
        }
        if len(shared) >= min_shared_sessions:
            correlation = _pearson(left_values, right_values)
            report["measured"] = correlation is not None
            report["measurement"] = "measured" if correlation is not None else "unmeasured"
            report["status"] = "measured" if correlation is not None else "zero_variance"
            report["unmeasured_reason"] = (
                None if correlation is not None else "zero_variance")
            report["correlation"] = correlation
        pair_reports.append(report)
    return {
        "method": "pearson_session_pnl",
        "min_shared_sessions": min_shared_sessions,
        "pairs": pair_reports,
        "insufficient_pairs": sum(item["status"] == "insufficient_shared_sessions"
                                   for item in pair_reports),
    }


def build_trader_diagnostics(
        rows_by_variant: Mapping[str, Iterable[Mapping[str, Any]]],
        variant_metadata: Mapping[str, Mapping[str, Any]] | None = None,
        *, min_shared_sessions: int = DEFAULT_MIN_SHARED_SESSIONS) -> dict[str, Any]:
    """Build a strict, descriptive report from one-shot per-arm row iterables.

    ``min_shared_sessions`` is exposed so tests can use a small corpus.  The
    production default remains thirty shared complete and eligible sessions.
    """
    if not isinstance(rows_by_variant, Mapping):
        raise TypeError("rows_by_variant must be a mapping")
    if isinstance(min_shared_sessions, bool) or not isinstance(min_shared_sessions, int):
        raise TypeError("min_shared_sessions must be an integer")
    if min_shared_sessions < 1:
        raise ValueError("min_shared_sessions must be positive")
    metadata = variant_metadata if isinstance(variant_metadata, Mapping) else {}
    arm_reports: dict[str, dict[str, Any]] = {}
    all_rows: list[tuple[str, Mapping[str, Any], int]] = []
    opportunity_variants: dict[str, set[str]] = defaultdict(set)
    aggregate_net = aggregate_gross = aggregate_fees = 0.0
    net_seen = gross_seen = fees_seen = 0
    for raw_variant, raw_rows in rows_by_variant.items():
        variant_id = _text(raw_variant)
        if not variant_id:
            variant_id = "unknown"
        rows: list[Mapping[str, Any]] = []
        for index, raw_row in enumerate(raw_rows or ()):
            if isinstance(raw_row, Mapping):
                row = dict(raw_row)
            else:
                row = {"coverage_issue": "invalid_row", "no_trade": True}
            rows.append(row)
            all_rows.append((variant_id, row, index))
            if (_executed(row) or row.get("signal_opportunity") is True):
                opportunity_variants[_opportunity_key(
                    variant_id, row, index)].add(variant_id)
        arm = _arm_report(variant_id, rows, metadata.get(variant_id))
        arm_reports[variant_id] = arm
        if arm["net_pnl"] is not None:
            aggregate_net += float(arm["net_pnl"])
            net_seen += 1
        if arm["gross_pnl_at_modeled_fill_prices"] is not None:
            aggregate_gross += float(arm["gross_pnl_at_modeled_fill_prices"])
            gross_seen += 1
        if arm["fees"] is not None:
            aggregate_fees += float(arm["fees"])
            fees_seen += 1
    overlap_keys = {key for key, variants in opportunity_variants.items()
                    if len(variants) > 1}
    overlapping_rows = sum(
        1 for variant, row, index in all_rows
        if (_executed(row) or row.get("signal_opportunity") is True)
        and _opportunity_key(variant, row, index) in overlap_keys)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "diagnostic_only": True,
        "authorizing": False,
        "selection": "none",
        "arms": dict(sorted(arm_reports.items())),
        "total_rows": sum(int(arm["total_rows"]) for arm in arm_reports.values()),
        "executed_trades": sum(int(arm["executed_trades"])
                               for arm in arm_reports.values()),
        "cross_arm_dependence": _cross_arm(
            arm_reports, min_shared_sessions=min_shared_sessions),
        "aggregate": {
            "net_pnl_overlapping_not_portfolio": aggregate_net if net_seen else None,
            "gross_pnl_at_modeled_fill_prices_overlapping_not_portfolio": (
                aggregate_gross if gross_seen else None),
            "fees_overlapping_not_portfolio": aggregate_fees if fees_seen else None,
            "pnl_basis": "gross_at_modeled_fill_prices_minus_fees",
            "interpretation": "overlapping_not_portfolio",
            "overlapping_not_portfolio": {
                "net_pnl": aggregate_net if net_seen else None,
                "gross_pnl_at_modeled_fill_prices": (
                    aggregate_gross if gross_seen else None),
                "fees": aggregate_fees if fees_seen else None,
            },
        },
        "opportunity_overlap": {
            "unique_opportunities": len(opportunity_variants),
            "overlapping_opportunities": len(overlap_keys),
            "overlapping_variant_rows": overlapping_rows,
            "overlap_is_not_portfolio": True,
        },
        "portfolio_pnl": None,
        "portfolio_interpretation": "not_computed_for_overlapping_arms",
    }
    # Remove private session vectors after dependence has been computed.
    for arm in report["arms"].values():
        arm.pop("_session_pnl", None)
    return _json_safe(report)


def summarize_trader_diagnostics(
        rows_by_variant: Mapping[str, Iterable[Mapping[str, Any]]],
        variant_metadata: Mapping[str, Mapping[str, Any]] | None = None,
        *, min_shared_sessions: int = DEFAULT_MIN_SHARED_SESSIONS) -> dict[str, Any]:
    """Compatibility name for callers that use ``summarize`` semantics."""
    return build_trader_diagnostics(
        rows_by_variant, variant_metadata,
        min_shared_sessions=min_shared_sessions)


def validate_trader_diagnostics(report: Mapping[str, Any]) -> bool:
    """Check the output contract and strict finite JSON serialization."""
    if not isinstance(report, Mapping):
        return False
    if report.get("diagnostic_only") is not True or report.get("authorizing") is not False:
        return False
    try:
        json.dumps(report, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        return False
    return True


__all__ = [
    "DEFAULT_MIN_SHARED_SESSIONS", "SCHEMA", "build_trader_diagnostics",
    "summarize_trader_diagnostics", "validate_trader_diagnostics",
]
