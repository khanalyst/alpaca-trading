"""Non-authorizing, paper-only incumbent trial runtime helpers.

This lane deliberately reuses the normal broker, risk, execution, exit, and
journal paths.  It selects one predeclared diagnostic rule arm without
creating or consulting an authorizing EdgeLedger record, and keeps its
forward-session evidence in the existing mode/account-scoped atomic state.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from research.diagnostic_shadow import (
    _logical_arms,
    build_diagnostic_cohort,
    content_identity,
)


SCHEMA = "paper-incumbent-trial.v1"
OUTCOME_SCHEMA = "paper-incumbent-outcome.v1"
REPORT_SCHEMA = "session-acceptance-report.v1"
TERMINAL_AUDIT_SCHEMA = "paper-incumbent-terminal-audit.v1"
REPLACEMENT_LOCAL_BOOK_ERROR = (
    "paper trial replacement requires restart with the frozen incumbent "
    "until the old local book is flat")
TERMINAL_STATES = {"failed", "review_required"}
ACTIVE_STATES = {"running", "passed"}
MAX_ACCEPTED_SESSIONS = 500
MAX_OUTCOMES = 10_000
MAX_REPORT_FILES = 5_000
_REPORT_NAME = re.compile(r"session-(\d{4}-\d{2}-\d{2})\.report\.json\Z")
_TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "replaced",
    "stopped", "suspended", "failed", "not_found",
}


class PaperTrialError(ValueError):
    """The requested incumbent trial cannot be safely continued."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    return json.loads(_json(value))


def _text(value: Any) -> str | None:
    if value in (None, "") or isinstance(value, (Mapping, list, tuple)):
        return None
    result = str(value).strip()
    return result or None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _fresh_stats(value: Any, *, count: int, limit: float) -> bool:
    if not isinstance(value, Mapping):
        return False
    observed = value.get("count")
    p50 = _finite(value.get("p50"))
    p95 = _finite(value.get("p95"))
    maximum = _finite(value.get("max"))
    return bool(
        not isinstance(observed, bool) and isinstance(observed, int) and
        observed == count and
        all(item is not None for item in (p50, p95, maximum)) and
        0 <= float(p50) <= float(p95) <= float(maximum) <= limit)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            raw = str(value or "").strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _today(config: Mapping[str, Any], now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise PaperTrialError("paper trial clock must be timezone-aware")
    session = config.get("session") if isinstance(config.get("session"), Mapping) else {}
    zone = str(session.get("timezone") or "America/New_York")
    return current.astimezone(ZoneInfo(zone)).date().isoformat()


def paper_trial_block(config: Mapping[str, Any] | None) -> dict[str, Any]:
    research = config.get("research") if isinstance(config, Mapping) else {}
    value = research.get("paper_trial") if isinstance(research, Mapping) else {}
    return dict(value) if isinstance(value, Mapping) else {}


def paper_trial_enabled(config: Mapping[str, Any] | None) -> bool:
    return paper_trial_block(config).get("enabled") is True


def resolve_catalog_arm(variant_id: str) -> dict[str, Any]:
    """Resolve one exact arm only from the fixed diagnostic catalog."""
    requested = str(variant_id or "").strip()
    matches = [dict(arm) for arm in _logical_arms()
               if str(arm.get("variant_id") or "") == requested]
    if len(matches) != 1:
        raise PaperTrialError(
            "research.paper_trial.variant_id must name exactly one fixed "
            "diagnostic catalog arm")
    return _plain(matches[0])


def runtime_code_identity(root: str | Path | None = None) -> str:
    """Match the bounded code identity used by diagnostic live shadow."""
    repo = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    files = {
        path.relative_to(repo).as_posix()
        for name in ("agent", "research")
        for path in (repo / name).rglob("*.py")
        if path.is_file()
    }
    for name in ("deploy/recorder.py", "requirements.lock.txt"):
        if (repo / name).is_file():
            files.add(name)
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8") + b"\0")
        try:
            digest.update((repo / name).read_bytes())
        except OSError:
            digest.update(f"missing:{name}".encode("utf-8"))
    return digest.hexdigest()


def _frozen_policy(config: Mapping[str, Any], arm: Mapping[str, Any],
                   code_identity: str) -> dict[str, Any]:
    broker = config.get("broker") if isinstance(config.get("broker"), Mapping) else {}
    data = config.get("data") if isinstance(config.get("data"), Mapping) else {}
    return _plain({
        "rule_spec": arm.get("rule_spec"),
        "universe": config.get("universe", {}),
        "risk": config.get("risk", {}),
        "execution": config.get("execution", {}),
        "costs": config.get("costs", {}),
        "session": config.get("session", {}),
        "trial_review": _policy(config),
        "feed": {
            "provider": broker.get("provider") or data.get("provider"),
            "equity": broker.get("data_feed") or data.get("feed"),
            "options": broker.get("options_feed") or data.get("options_feed"),
        },
        "code_identity": code_identity,
    })


def build_descriptor(config: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the exact incumbent and mounted policy identity."""
    block = paper_trial_block(config)
    if block.get("enabled") is not True:
        raise PaperTrialError("paper trial is not enabled")
    arm = resolve_catalog_arm(str(block.get("variant_id") or ""))
    code_identity = runtime_code_identity()
    cohort = build_diagnostic_cohort(config, code_identity=code_identity)
    candidates = [candidate for candidate in cohort.get("arms", ())
                  if isinstance(candidate, Mapping) and
                  candidate.get("variant_id") == arm.get("variant_id")]
    if len(candidates) != 1:
        raise PaperTrialError("fixed paper trial arm has no unique cohort identity")
    candidate = candidates[0]
    policy = _frozen_policy(config, arm, code_identity)
    report_root = str(Path(str(block.get("accepted_session_report_root") or "")).resolve())
    body = {
        "trial_id": str(block.get("trial_id") or "").strip(),
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "variant_id": str(arm.get("variant_id") or ""),
        "family": str(arm.get("family") or ""),
        "role": str(arm.get("role") or ""),
        "spec_identity": str(arm.get("spec_identity") or ""),
        "cohort_identity": str(cohort.get("cohort_identity") or ""),
        "code_identity": code_identity,
        "policy": policy,
        "policy_identity": content_identity(policy),
        "diagnostic_candidate_ids": sorted(
            str(item.get("candidate_id") or "")
            for item in cohort.get("arms", ())
            if isinstance(item, Mapping) and item.get("candidate_id")),
        "accepted_session_report_root": report_root,
        "max_review_sessions": int(block.get("max_review_sessions") or 60),
    }
    body["incumbent_identity"] = content_identity({
        "schema": SCHEMA,
        **body,
    })
    return body


def effective_config(config: Mapping[str, Any], descriptor: Mapping[str, Any]) -> dict:
    """Apply the frozen catalog rule without changing shared safety policy."""
    result = deepcopy(dict(config))
    strategy = dict(result.get("strategy") or {})
    strategy.update({
        "id": "rule",
        "version": "v1",
        "variant_id": descriptor["variant_id"],
        "selection_mode": "specific",
        "pinned": [],
        "execution_mode": "shares",
        "rule_spec": deepcopy(descriptor["policy"]["rule_spec"]),
    })
    result["strategy"] = strategy
    return result


def _policy(config: Mapping[str, Any]) -> dict[str, Any]:
    research = config.get("research") if isinstance(config.get("research"), Mapping) else {}
    trial = research.get("trial") if isinstance(research.get("trial"), Mapping) else {}
    return {
        "min_sessions": int(trial.get("min_sessions", 20)),
        "min_trades": int(trial.get("min_trades", 20)),
        "min_mean_r": float(trial.get("min_mean_r", 0.0)),
        "min_total_r": float(trial.get("min_total_r", 0.0)),
    }


def new_state(descriptor: Mapping[str, Any], config: Mapping[str, Any], *,
              now: datetime | None = None) -> dict[str, Any]:
    policy = _policy(config)
    return {
        "schema": SCHEMA,
        "state": "running",
        "trial_id": descriptor["trial_id"],
        "candidate_id": descriptor["candidate_id"],
        "variant_id": descriptor["variant_id"],
        "family": descriptor["family"],
        "role": descriptor["role"],
        "incumbent_identity": descriptor["incumbent_identity"],
        "policy_identity": descriptor["policy_identity"],
        "spec_identity": descriptor["spec_identity"],
        "code_identity": descriptor["code_identity"],
        "cohort_identity": descriptor["cohort_identity"],
        "report_identities": {
            "deployment": None,
            "code": descriptor["code_identity"],
            "cohort": descriptor["cohort_identity"],
            "activation": None,
        },
        "activation_confirmed": False,
        "activation_account_fingerprint": None,
        "started_on": None,
        "accepted_session_report_root": descriptor["accepted_session_report_root"],
        "max_review_sessions": descriptor["max_review_sessions"],
        "required_sessions": policy["min_sessions"],
        "required_trades": policy["min_trades"],
        "accepted_sessions": [],
        "outcomes": [],
        "verdict": {
            "state": "running", "sessions": 0, "trades": 0,
            "sessions_required": policy["min_sessions"],
            "trades_required": policy["min_trades"],
        },
        "blockers": [],
        "authorizing": False,
        "proof_authority": False,
    }


def validate_state(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise PaperTrialError("paper_trial runtime state has an invalid schema")
    result = deepcopy(dict(value))
    if result.get("state") not in ACTIVE_STATES | TERMINAL_STATES:
        raise PaperTrialError("paper_trial runtime state is invalid")
    for key in ("trial_id", "candidate_id", "variant_id", "incumbent_identity",
                "policy_identity", "spec_identity", "code_identity",
                "cohort_identity", "accepted_session_report_root"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise PaperTrialError(f"paper_trial runtime {key} is invalid")
    activated = result.get("activation_confirmed")
    activation_fingerprint = result.get("activation_account_fingerprint")
    if not isinstance(activated, bool):
        raise PaperTrialError(
            "paper_trial runtime activation_confirmed is invalid")
    if activated:
        if (not isinstance(activation_fingerprint, str) or
                not activation_fingerprint.strip()):
            raise PaperTrialError(
                "paper_trial runtime activation account is invalid")
        try:
            date.fromisoformat(str(result.get("started_on") or ""))
        except ValueError as exc:
            raise PaperTrialError(
                "paper_trial runtime started_on is invalid") from exc
    elif (activation_fingerprint is not None or
          result.get("started_on") is not None):
        raise PaperTrialError(
            "paper_trial runtime cannot start before flat-book activation")
    if result.get("authorizing") is not False or result.get("proof_authority") is not False:
        raise PaperTrialError("paper_trial runtime cannot carry proof authority")
    accepted = result.get("accepted_sessions")
    outcomes = result.get("outcomes")
    identities = result.get("report_identities")
    verdict = result.get("verdict")
    blockers = result.get("blockers")
    if not isinstance(accepted, list) or len(accepted) > MAX_ACCEPTED_SESSIONS:
        raise PaperTrialError("paper_trial accepted sessions are invalid")
    if not isinstance(outcomes, list) or len(outcomes) > MAX_OUTCOMES:
        raise PaperTrialError("paper_trial outcomes are invalid")
    if (any(not isinstance(item, Mapping) for item in accepted) or
            len({str(item.get("date")) for item in accepted}) != len(accepted)):
        raise PaperTrialError("paper_trial accepted sessions are invalid")
    if (any(not isinstance(item, Mapping) for item in outcomes) or
            len({str(item.get("outcome_id")) for item in outcomes}) != len(outcomes)):
        raise PaperTrialError("paper_trial outcomes are invalid")
    if not isinstance(identities, Mapping) or not isinstance(verdict, Mapping):
        raise PaperTrialError("paper_trial evidence state is invalid")
    if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
        raise PaperTrialError("paper_trial blockers are invalid")
    for key in ("max_review_sessions", "required_sessions", "required_trades"):
        number = result.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise PaperTrialError(f"paper_trial runtime {key} is invalid")
    return result


def _same_incumbent(state_value: Mapping[str, Any],
                    descriptor: Mapping[str, Any]) -> bool:
    return all(state_value.get(key) == descriptor.get(key) for key in (
        "trial_id", "candidate_id", "variant_id", "incumbent_identity",
        "policy_identity", "spec_identity", "code_identity", "cohort_identity",
        "accepted_session_report_root", "max_review_sessions",
    ))


def _order_status(value: Any) -> str:
    raw = value.get("status") if isinstance(value, Mapping) else getattr(value, "status", "")
    raw = getattr(raw, "value", raw)
    return str(raw or "").split(".")[-1].strip().lower()


def replacement_local_book_is_flat(runtime: Mapping[str, Any]) -> bool:
    """Return whether local state has no exposure requiring its old policy."""
    if not isinstance(runtime, Mapping):
        return False
    for key in ("active_trades", "protection"):
        value = runtime.get(key)
        if not isinstance(value, Mapping) or value:
            return False
    local_orders = runtime.get("orders")
    if not isinstance(local_orders, Mapping) or any(
            not _order_status(order) or
            _order_status(order) not in _TERMINAL_ORDER_STATUSES
            for order in local_orders.values()):
        return False
    return True


def replacement_book_is_flat(runtime: Mapping[str, Any],
                             broker_snapshot: Mapping[str, Any] | None) -> bool:
    """Require both reconciled local state and an explicit broker snapshot."""
    if (not replacement_local_book_is_flat(runtime) or
            not isinstance(broker_snapshot, Mapping)):
        return False
    if not str(runtime.get("account_fingerprint") or "").strip():
        return False
    positions = broker_snapshot.get("positions")
    orders = broker_snapshot.get("orders")
    if not isinstance(positions, (list, tuple)) or positions:
        return False
    if not isinstance(orders, (list, tuple)) or any(
            not _order_status(order) or
            _order_status(order) not in _TERMINAL_ORDER_STATUSES
            for order in orders):
        return False
    return True


def _read_report(path: Path, *, descriptor: Mapping[str, Any],
                 state_value: Mapping[str, Any], now: datetime) -> tuple[dict | None, str | None]:
    match = _REPORT_NAME.fullmatch(path.name)
    if match is None:
        return None, None
    try:
        parsed_date = date.fromisoformat(match.group(1))
        started = date.fromisoformat(str(state_value.get("started_on") or ""))
    except ValueError:
        return None, f"report_session_invalid:{path.name}"
    market_today = now.astimezone(ZoneInfo("America/New_York")).date()
    if parsed_date <= started or parsed_date > market_today:
        return None, None
    if path.is_symlink():
        return None, f"report_symlink:{path.name}"
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4 * 1024 * 1024:
            return None, f"report_file_invalid:{path.name}"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, f"report_unreadable:{path.name}"
    if not isinstance(payload, Mapping):
        return None, f"report_invalid:{path.name}"
    if payload.get("accepted") is not True:
        return None, None
    if (payload.get("schema") != REPORT_SCHEMA or
            payload.get("authorizing") is not False):
        return None, f"report_contract_invalid:{path.name}"
    source_mode_value = payload.get("source_mode")
    payload_session = payload.get("session")
    if source_mode_value in (None, "") and isinstance(
            payload_session, Mapping):
        source_mode_value = payload_session.get("source_mode")
    source_mode = str(source_mode_value or "").strip().lower()
    if source_mode and source_mode != "forward_observed":
        return None, None
    if (payload.get("status") != "accepted" or
            payload.get("operational_only") is not True or
            payload.get("promotion_eligible") is not False or
            payload.get("reasons") != [] or
            not isinstance(payload.get("reason_counts"), Mapping) or
            payload.get("reason_counts") or
            not isinstance(payload.get("symbol_failure_counts"), Mapping) or
            payload.get("symbol_failure_counts")):
        return None, f"report_contract_invalid:{path.name}"
    session = payload.get("session")
    if not isinstance(session, Mapping):
        return None, f"report_session_invalid:{path.name}"
    session_date = str(session.get("date") or "")
    if session_date != match.group(1) or session.get("source") != "alpaca_calendar":
        return None, f"report_session_invalid:{path.name}"
    opened, closed = _timestamp(session.get("open")), _timestamp(session.get("close"))
    if (opened is None or closed is None or not opened < closed or
            opened.astimezone(ZoneInfo("America/New_York")).date() != parsed_date or
            closed.astimezone(ZoneInfo("America/New_York")).date() != parsed_date):
        return None, f"report_session_invalid:{path.name}"
    if closed > now.astimezone(timezone.utc):
        return None, None
    opened_ts = opened.timestamp()
    closed_ts = closed.timestamp()
    finalized_ts = _finite(payload.get("finalized_ts"))
    if (finalized_ts is None or finalized_ts < closed_ts or
            finalized_ts > now.astimezone(timezone.utc).timestamp() + 1.0):
        return None, f"report_finalization_invalid:{path.name}"
    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping):
        return None, f"report_coverage_invalid:{path.name}"
    first_sample = _finite(coverage.get("first_sample_ts"))
    last_sample = _finite(coverage.get("last_sample_ts"))
    coverage_open = _finite(coverage.get("open_ts"))
    coverage_close = _finite(coverage.get("close_ts"))
    start_tolerance = _finite(coverage.get("start_tolerance_seconds"))
    end_tolerance = _finite(coverage.get("end_tolerance_seconds"))
    max_gap = _finite(coverage.get("max_sample_gap_seconds"))
    observed_gap = _finite(coverage.get("max_observed_gap_seconds"))
    if (coverage.get("closed_at_report") is not True or
            any(value is None for value in (
                first_sample, last_sample, coverage_open, coverage_close,
                start_tolerance, end_tolerance, max_gap, observed_gap)) or
            abs(float(coverage_open) - opened_ts) > 1e-6 or
            abs(float(coverage_close) - closed_ts) > 1e-6 or
            float(start_tolerance) < 0 or float(end_tolerance) < 0 or
            float(max_gap) <= 0 or float(observed_gap) < 0 or
            not opened_ts - float(start_tolerance) <= float(first_sample) <=
                    opened_ts + float(start_tolerance) or
            not closed_ts - float(end_tolerance) <= float(last_sample) <=
                    closed_ts + float(end_tolerance) or
            float(first_sample) > float(last_sample) or
            float(last_sample) > finalized_ts or
            float(observed_gap) > float(max_gap)):
        return None, f"report_coverage_invalid:{path.name}"
    counts = payload.get("sample_counts")
    count_values: dict[str, int] = {}
    if isinstance(counts, Mapping):
        for key in ("total", "healthy", "failed", "valid_timestamps"):
            value = counts.get(key)
            if (isinstance(value, bool) or not isinstance(value, int) or
                    value < 0):
                break
            count_values[key] = value
    if (len(count_values) != 4 or count_values["total"] <= 0 or
            count_values["healthy"] != count_values["total"] or
            count_values["failed"] != 0 or
            count_values["valid_timestamps"] != count_values["total"]):
        return None, f"report_samples_invalid:{path.name}"
    expected_symbols = payload.get("expected_symbols")
    policy = descriptor.get("policy")
    universe = policy.get("universe") if isinstance(policy, Mapping) else {}
    configured_symbols = (universe.get("symbols")
                          if isinstance(universe, Mapping) else None)
    normalized_symbols = (sorted({str(item).strip().upper()
                                  for item in expected_symbols
                                  if isinstance(item, str) and item.strip()})
                          if isinstance(expected_symbols, list) else [])
    expected_configured = (sorted({str(item).strip().upper()
                                   for item in configured_symbols
                                   if isinstance(item, str) and item.strip()})
                           if isinstance(configured_symbols, list) else [])
    if (not normalized_symbols or normalized_symbols != expected_configured or
            len(normalized_symbols) != len(expected_symbols)):
        return None, f"report_universe_mismatch:{path.name}"
    freshness = payload.get("freshness")
    strict_threshold = (_finite(freshness.get(
        "strict_threshold_cap_seconds"))
        if isinstance(freshness, Mapping) else None)
    # Completed-bar publication deadlines are the producer's immutable
    # freshness authority.  A fresh read/ingestion timestamp for an old bar
    # must never substitute for this evidence.
    observation_count = count_values["total"] * len(normalized_symbols)
    if (strict_threshold != 30.0 or
            not _fresh_stats(freshness.get("quote_event_age_seconds"),
                             count=observation_count,
                             limit=strict_threshold) or
            not _fresh_stats(freshness.get(
                "bar_publication_deadline_lag_seconds"),
                count=observation_count, limit=strict_threshold) or
            not _fresh_stats(freshness.get("shadow_source_lag_seconds"),
                             count=count_values["total"],
                             limit=strict_threshold)):
        return None, f"report_freshness_invalid:{path.name}"
    warmups = payload.get("warmup_sessions")
    try:
        warmup_dates = [date.fromisoformat(str(item)) for item in warmups]
    except (TypeError, ValueError):
        warmup_dates = []
    if len(warmup_dates) != 1 or warmup_dates[0] >= parsed_date:
        return None, f"report_warmup_invalid:{path.name}"
    progress = payload.get("arm_progress")
    cursors = progress.get("cursors") if isinstance(progress, Mapping) else None
    expected_candidates = descriptor.get("diagnostic_candidate_ids")
    if (not isinstance(cursors, Mapping) or
            not isinstance(expected_candidates, list) or
            progress.get("arms") != len(expected_candidates) or
            set(map(str, cursors)) != set(map(str, expected_candidates))):
        return None, f"report_progress_invalid:{path.name}"
    progress_values: list[tuple[float, str, int]] = []
    for cursor in cursors.values():
        if not isinstance(cursor, Mapping):
            return None, f"report_progress_invalid:{path.name}"
        inserted_at = _finite(cursor.get("last_inserted_at"))
        event_key = _text(cursor.get("last_event_key"))
        processed = cursor.get("processed_events")
        if (inserted_at is None or inserted_at < opened_ts or
                inserted_at > finalized_ts or not event_key or
                isinstance(processed, bool) or not isinstance(processed, int) or
                processed <= 0):
            return None, f"report_progress_invalid:{path.name}"
        progress_values.append((inserted_at, event_key, processed))
    if len(set(progress_values)) != 1:
        return None, f"report_progress_invalid:{path.name}"
    progress_summary = payload.get("post_activation_progress")
    activation = (progress_summary.get("activation_watermark")
                  if isinstance(progress_summary, Mapping) else None)
    summary_snapshots = (progress_summary.get("snapshots")
                         if isinstance(progress_summary, Mapping) else None)
    summary_minimum = (progress_summary.get("minimum_processed_events")
                       if isinstance(progress_summary, Mapping) else None)
    summary_delta = (progress_summary.get("minimum_session_delta")
                     if isinstance(progress_summary, Mapping) else None)
    activation_inserted = (_finite(activation.get("last_inserted_at"))
                           if isinstance(activation, Mapping) else None)
    activation_count = (activation.get("count")
                        if isinstance(activation, Mapping) else None)
    activation_decisions = (activation.get("decision_event_count")
                            if isinstance(activation, Mapping) else None)
    activation_event = (_text(activation.get("last_event_key"))
                        if isinstance(activation, Mapping) else None)
    if (not isinstance(progress_summary, Mapping) or
            progress_summary.get("arms") != len(expected_candidates) or
            progress_summary.get("all_arms_progressed") is not True or
            isinstance(summary_snapshots, bool) or
            not isinstance(summary_snapshots, int) or summary_snapshots < 2 or
            isinstance(summary_minimum, bool) or
            not isinstance(summary_minimum, int) or summary_minimum <= 0 or
            summary_minimum != min(value[2] for value in progress_values) or
            isinstance(summary_delta, bool) or
            not isinstance(summary_delta, int) or summary_delta <= 0 or
            summary_delta > summary_minimum or activation_inserted is None or
            not 0 <= activation_inserted < opened_ts or
            isinstance(activation_count, bool) or
            not isinstance(activation_count, int) or activation_count < 0 or
            isinstance(activation_decisions, bool) or
            not isinstance(activation_decisions, int) or
            activation_decisions < 0 or not activation_event):
        return None, f"report_progress_invalid:{path.name}"
    identities = payload.get("identities")
    if not isinstance(identities, Mapping):
        return None, f"report_identity_missing:{path.name}"
    normalized = {key: _text(identities.get(key))
                  for key in ("deployment", "code", "cohort", "activation")}
    if any(value is None for value in normalized.values()):
        return None, f"report_identity_missing:{path.name}"
    if (normalized["code"] != descriptor.get("code_identity") or
            normalized["cohort"] != descriptor.get("cohort_identity")):
        return None, f"report_identity_mismatch:{path.name}"
    return {
        "date": session_date,
        "report_digest": _digest(payload),
        "identities": normalized,
    }, None


def _merge_reports(state_value: dict[str, Any], descriptor: Mapping[str, Any], *,
                   now: datetime) -> dict[str, Any]:
    root = Path(str(descriptor["accepted_session_report_root"]))
    errors: list[str] = []
    if not root.is_dir():
        state_value["accepted_sessions"] = []
        state_value["blockers"] = ["accepted_session_report_root_unavailable"]
        return state_value
    try:
        paths = sorted(path for path in root.iterdir()
                       if _REPORT_NAME.fullmatch(path.name))
    except OSError:
        state_value["accepted_sessions"] = []
        state_value["blockers"] = ["accepted_session_report_root_unavailable"]
        return state_value
    if len(paths) > MAX_REPORT_FILES:
        paths = paths[-MAX_REPORT_FILES:]
        errors.append("accepted_session_report_bound_exceeded")
    prior = {str(item.get("date")): dict(item)
             for item in state_value.get("accepted_sessions", ())
             if isinstance(item, Mapping)}
    accepted: dict[str, dict] = {}
    pinned = dict(state_value.get("report_identities") or {})
    limit = int(state_value["max_review_sessions"])
    for path in paths:
        item, error = _read_report(
            path, descriptor=descriptor, state_value=state_value, now=now)
        if error:
            errors.append(error)
            continue
        if item is None:
            continue
        identities = item["identities"]
        if pinned.get("deployment") not in (None, identities["deployment"]):
            errors.append(f"report_deployment_drift:{path.name}")
            continue
        if pinned.get("activation") not in (None, identities["activation"]):
            errors.append(f"report_activation_drift:{path.name}")
            continue
        prior_item = prior.get(item["date"])
        if prior_item is not None:
            if prior_item.get("report_digest") != item.get("report_digest"):
                errors.append(f"counted_report_changed:{path.name}")
                continue
            accepted[item["date"]] = item
            continue
        if len(accepted) >= limit:
            continue
        pinned["deployment"] = identities["deployment"]
        pinned["activation"] = identities["activation"]
        accepted[item["date"]] = item
    for missing in sorted(set(prior) - set(accepted)):
        errors.append(f"counted_report_missing:session-{missing}.report.json")
    state_value["accepted_sessions"] = [accepted[key] for key in sorted(accepted)]
    state_value["report_identities"] = pinned
    state_value["blockers"] = sorted(set(errors))[:64]
    return state_value


def _confidence(outcomes: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]) -> dict:
    values: list[float] = []
    clusters: list[str] = []
    for outcome in outcomes:
        value = _finite(outcome.get("r_multiple"))
        session = _text(outcome.get("session_date"))
        if value is not None and session:
            values.append(value)
            clusters.append(session)
    if not values:
        return {
            "schema": "session-cluster-confidence.v1", "available": False,
            "confidence": .95, "lower_bound": None, "upper_bound": None,
            "mean": None, "clusters": 0, "session_clusters": 0,
            "observations": 0, "method": "moving_block_cluster_bootstrap",
            "reason": "no_usable_r_observations",
        }
    from research.stats import moving_block_cluster_bootstrap_lower_bound
    cluster_count = len(set(clusters))
    result = moving_block_cluster_bootstrap_lower_bound(
        values, clusters, confidence=.95, draws=1000,
        block_length=max(1, min(5, cluster_count - 1)), min_clusters=2)
    return {
        "schema": "session-cluster-confidence.v1",
        **{key: result.get(key) for key in (
            "available", "confidence", "lower_bound", "upper_bound", "mean",
            "clusters", "observations", "method", "reason")},
        "session_clusters": cluster_count,
    }


def _performance(state_value: Mapping[str, Any], policy: Mapping[str, Any]) -> dict:
    outcomes = [item for item in state_value.get("outcomes", ())
                if isinstance(item, Mapping)]
    r_values = [_finite(item.get("r_multiple")) for item in outcomes]
    finite_r = [value for value in r_values if value is not None]
    pnl_values = [_finite(item.get("net_pnl")) for item in outcomes]
    finite_pnl = [value for value in pnl_values if value is not None]
    return {
        "sessions": len(state_value.get("accepted_sessions", ())),
        "outcomes": len(outcomes),
        "total_r": sum(finite_r) if len(finite_r) == len(outcomes) and outcomes else None,
        "mean_r": (sum(finite_r) / len(finite_r)
                   if len(finite_r) == len(outcomes) and outcomes else None),
        "net_pnl": sum(finite_pnl) if len(finite_pnl) == len(outcomes) and outcomes else None,
        "win_rate": (sum(1 for value in finite_pnl if value > 0) / len(finite_pnl)
                     if len(finite_pnl) == len(outcomes) and outcomes else None),
        "session_cluster_confidence": _confidence(outcomes, policy),
    }


def refresh_state(state_value: Mapping[str, Any], descriptor: Mapping[str, Any],
                  config: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    current = validate_state(state_value)
    if not _same_incumbent(current, descriptor):
        raise PaperTrialError("active paper trial identity/config changed")
    if current.get("activation_confirmed") is not True:
        raise PaperTrialError(
            "paper trial cannot accrue evidence before flat-book activation")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise PaperTrialError("paper trial clock must be timezone-aware")
    current = _merge_reports(current, descriptor, now=current_time)
    policy = _policy(config)
    performance = _performance(current, policy)
    from research.trial import _verdict
    verdict = _verdict(performance, policy)
    prior_state = str(current.get("state") or "running")
    if prior_state in TERMINAL_STATES:
        lifecycle = prior_state
    elif verdict.get("state") == "failed":
        lifecycle = "failed"
    elif len(current.get("accepted_sessions", ())) >= int(
            current["max_review_sessions"]):
        lifecycle = "review_required"
    elif verdict.get("state") == "passed":
        lifecycle = "passed"
    else:
        lifecycle = "running"
    current["state"] = lifecycle
    current["verdict"] = _plain(verdict)
    current["required_sessions"] = policy["min_sessions"]
    current["required_trades"] = policy["min_trades"]
    current["authorizing"] = False
    current["proof_authority"] = False
    return validate_state(current)


def outcome_entry(outcome: Mapping[str, Any]) -> dict[str, Any]:
    trial_id = _text(outcome.get("paper_trial_id"))
    candidate_id = _text(outcome.get("paper_trial_candidate_id") or
                         outcome.get("candidate_id"))
    variant_id = _text(outcome.get("paper_trial_variant_id") or
                       outcome.get("variant_id"))
    incumbent_identity = _text(outcome.get("paper_trial_incumbent_identity"))
    if not trial_id or not candidate_id or not variant_id or not incumbent_identity:
        raise PaperTrialError("paper trial outcome identity is incomplete")
    opened = _finite(outcome.get("opened_at"))
    parent = (_text(outcome.get("order_id")) or
              _text(outcome.get("opportunity_id")) or
              f"{outcome.get('symbol')}:{opened}")
    body = {
        **_plain(dict(outcome)),
        "schema": OUTCOME_SCHEMA,
        "paper_trial_id": trial_id,
        "paper_trial_candidate_id": candidate_id,
        "paper_trial_variant_id": variant_id,
        "paper_trial_incumbent_identity": incumbent_identity,
        "candidate_id": candidate_id,
        "variant_id": variant_id,
        "proof_run_id": None,
        "authorizing": False,
        "proof_authority": False,
    }
    if opened is not None:
        body["session_date"] = datetime.fromtimestamp(
            opened, timezone.utc).astimezone(
                ZoneInfo("America/New_York")).date().isoformat()
    outcome_id = _digest({
        "schema": OUTCOME_SCHEMA,
        "trial_id": trial_id,
        "candidate_id": candidate_id,
        "parent": parent,
    })
    body["outcome_id"] = outcome_id
    return {"outcome_id": outcome_id, **body}


def merge_outcomes(state_value: Mapping[str, Any],
                   pending: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    current = validate_state(state_value)
    rows = [dict(item) for item in current.get("outcomes", ())]
    known = {str(item.get("outcome_id")) for item in rows}
    for item in pending:
        row = outcome_entry(item)
        if (row["paper_trial_id"] != current["trial_id"] or
                row["paper_trial_candidate_id"] != current["candidate_id"] or
                row["paper_trial_variant_id"] != current["variant_id"] or
                row["paper_trial_incumbent_identity"] !=
                current["incumbent_identity"]):
            raise PaperTrialError("paper trial outcome differs from active incumbent")
        if row["outcome_id"] not in known:
            rows.append(row)
            known.add(row["outcome_id"])
    if len(rows) > MAX_OUTCOMES:
        raise PaperTrialError("paper trial outcome bound exceeded")
    current["outcomes"] = rows
    return validate_state(current)


def public_status(state_value: Mapping[str, Any] | None, *,
                  pending_replacement: bool = False,
                  error: str | None = None) -> dict[str, Any]:
    try:
        current = validate_state(state_value)
    except PaperTrialError as exc:
        return {
            "schema": SCHEMA, "enabled": True, "state": "blocked",
            "entry_eligible": False, "blockers": [str(exc)],
            "authorizing": False, "proof_authority": False,
        }
    if not current:
        blockers = [error or "paper_trial_state_unavailable"]
        return {
            "schema": SCHEMA, "enabled": True, "state": "blocked",
            "entry_eligible": False, "blockers": blockers,
            "authorizing": False, "proof_authority": False,
        }
    blockers = list(current.get("blockers") or [])
    if current.get("activation_confirmed") is not True:
        blockers.insert(0, "initial_activation_requires_account_bound_flat_book")
    if pending_replacement:
        blockers.insert(0, "replacement_requires_terminal_flat_incumbent")
    if current["state"] == "failed":
        blockers.insert(0, "paper_trial_failed")
    elif current["state"] == "review_required":
        blockers.insert(0, "paper_trial_review_required")
    if error:
        blockers.insert(0, str(error))
    blockers = list(dict.fromkeys(blockers))[:8]
    eligible = (current.get("activation_confirmed") is True and
                current["state"] in ACTIVE_STATES and not blockers and
                not pending_replacement and not error)
    verdict = current.get("verdict") if isinstance(current.get("verdict"), Mapping) else {}
    return {
        "schema": SCHEMA,
        "enabled": True,
        "trial_id": current["trial_id"],
        "candidate_id": current["candidate_id"],
        "variant_id": current["variant_id"],
        "family": current.get("family"),
        "role": current.get("role"),
        "incumbent_identity": current["incumbent_identity"],
        "state": current["state"],
        "activation_confirmed": current.get("activation_confirmed") is True,
        "entry_eligible": eligible,
        "valid_sessions": len(current.get("accepted_sessions", ())),
        "required_sessions": current["required_sessions"],
        "closed_outcomes": len(current.get("outcomes", ())),
        "required_trades": current["required_trades"],
        "verdict": verdict.get("state", "running"),
        "verdict_detail": _plain(verdict),
        "max_review_sessions": current["max_review_sessions"],
        "started_on": current.get("started_on"),
        "report_identities": _plain(current.get("report_identities", {})),
        "blockers": blockers,
        "authorizing": False,
        "proof_authority": False,
    }


class PaperTrialRuntime:
    """Small engine adapter around one frozen durable incumbent."""

    def __init__(self, config: Mapping[str, Any]):
        self.base_config = deepcopy(dict(config))
        self.descriptor = build_descriptor(self.base_config)
        self.config = effective_config(self.base_config, self.descriptor)
        self.pending_replacement = False
        self.error: str | None = None
        self.current: dict[str, Any] = {}

    def replacement_audit(
            self, runtime: Mapping[str, Any],
            broker_snapshot: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """Return the bounded terminal snapshot that must precede replacement."""
        existing = validate_state(runtime.get("paper_trial"))
        if (not existing or _same_incumbent(existing, self.descriptor) or
                existing.get("state") not in TERMINAL_STATES or
                not replacement_book_is_flat(runtime, broker_snapshot)):
            return None
        terminal = {
            key: _plain(existing.get(key)) for key in (
                "trial_id", "candidate_id", "variant_id", "family", "role",
                "incumbent_identity", "policy_identity", "spec_identity",
                "code_identity", "cohort_identity", "state", "started_on",
                "accepted_sessions", "outcomes", "verdict", "blockers",
                "report_identities", "max_review_sessions",
                "required_sessions", "required_trades", "authorizing",
                "proof_authority",
            )
        }
        audit = {
            "schema": TERMINAL_AUDIT_SCHEMA,
            "reason": "replacement_requested_after_terminal_flat_book",
            "terminal": terminal,
            "replacement": {
                key: self.descriptor[key] for key in (
                    "trial_id", "candidate_id", "variant_id",
                    "incumbent_identity", "policy_identity", "code_identity",
                    "cohort_identity",
                )
            },
            "valid_sessions": len(existing.get("accepted_sessions", ())),
            "closed_outcomes": len(existing.get("outcomes", ())),
            "authorizing": False,
            "proof_authority": False,
        }
        audit["audit_id"] = _digest(audit)
        return audit

    def update_runtime(self, runtime: dict[str, Any], *,
                       broker_snapshot: Mapping[str, Any] | None = None,
                       now: datetime | None = None) -> dict[str, Any]:
        existing = validate_state(runtime.get("paper_trial"))
        if not existing:
            current = new_state(self.descriptor, self.base_config, now=now)
            self.pending_replacement = False
        elif _same_incumbent(existing, self.descriptor):
            current = existing
            self.pending_replacement = False
        elif existing.get("state") not in TERMINAL_STATES:
            raise PaperTrialError(
                "active paper trial identity/config changed; keep the frozen incumbent")
        elif replacement_book_is_flat(runtime, broker_snapshot):
            current = new_state(self.descriptor, self.base_config, now=now)
            self.pending_replacement = False
        else:
            current = existing
            self.pending_replacement = True
        if not self.pending_replacement:
            if current.get("activation_confirmed") is True:
                fingerprint = str(runtime.get("account_fingerprint") or "")
                if fingerprint != current.get("activation_account_fingerprint"):
                    raise PaperTrialError(
                        "active paper trial account identity changed")
            elif replacement_book_is_flat(runtime, broker_snapshot):
                current["activation_confirmed"] = True
                current["activation_account_fingerprint"] = str(
                    runtime["account_fingerprint"])
                current["started_on"] = _today(self.base_config, now)
            if current.get("activation_confirmed") is True:
                current = refresh_state(
                    current, self.descriptor, self.base_config, now=now)
        runtime["paper_trial"] = current
        self.current = current
        self.error = None
        return runtime

    def status(self, state_value: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return public_status(
            state_value if state_value is not None else self.current,
            pending_replacement=self.pending_replacement,
            error=self.error)

    def entry_identity(self) -> dict[str, Any]:
        current = validate_state(self.current)
        if not current:
            raise PaperTrialError("paper trial state is unavailable")
        if current.get("activation_confirmed") is not True:
            raise PaperTrialError(
                "paper trial cannot tag entries before flat-book activation")
        return {
            "paper_trial_id": current["trial_id"],
            "paper_trial_candidate_id": current["candidate_id"],
            "paper_trial_variant_id": current["variant_id"],
            "paper_trial_incumbent_identity": current["incumbent_identity"],
            "paper_trial_authorizing": False,
            "candidate_id": current["candidate_id"],
            "variant_id": current["variant_id"],
            "proof_run_id": None,
        }

    def annotate(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(plan)
        result.update(self.entry_identity())
        return result

    def merge_pending(self, state_value: Mapping[str, Any],
                      pending: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        current = merge_outcomes(state_value, pending)
        self.current = current
        return current


__all__ = [
    "ACTIVE_STATES", "OUTCOME_SCHEMA", "PaperTrialError",
    "PaperTrialRuntime", "REPORT_SCHEMA", "REPLACEMENT_LOCAL_BOOK_ERROR",
    "SCHEMA", "TERMINAL_AUDIT_SCHEMA",
    "build_descriptor", "effective_config", "merge_outcomes", "new_state",
    "outcome_entry", "paper_trial_block", "paper_trial_enabled",
    "public_status", "refresh_state", "replacement_book_is_flat",
    "replacement_local_book_is_flat", "resolve_catalog_arm",
    "runtime_code_identity", "validate_state",
]
