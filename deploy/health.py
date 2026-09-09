#!/usr/bin/env python3
"""Container health probes with machine-readable failure reasons."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

# ``python deploy/health.py ...`` sets ``sys.path[0]`` to ``deploy/`` rather
# than the repository root.  Add the root explicitly so recovery shells and
# Compose health checks do not depend on PYTHONPATH being preconfigured.
import sys

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from deploy import load_config
from deploy.provenance import deployment_parity, deployment_provenance
from deploy.scheduler_output import (derive_research_readiness,
                                     structured_research_preflight,
                                     structured_research_progress)

MAX_RECORDER_INDEX_BYTES = 16 * 1024 * 1024
AUTHORIZING_MARKET_DATA_MAX_AGE_SECONDS = 30.0
NEW_YORK = ZoneInfo("America/New_York")


def _with_provenance(result: dict, payload: dict | None = None) -> dict:
    """Attach one bounded deployment identity to every health projection."""
    value = payload.get("provenance") if isinstance(payload, dict) else None
    if not isinstance(value, dict):
        value = deployment_provenance()
    result["provenance"] = value
    # Keep the explicit name available to API consumers that do not know the
    # shorter historical key yet.
    result["deployment_provenance"] = value
    return result


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _status_text(value: object, *, limit: int = 160) -> str | None:
    """Return one bounded scalar string for externally visible status data."""
    if value in (None, "") or isinstance(value, (dict, list, tuple)):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _status_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return min(int(number), 1_000_000_000)


def _status_nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return min(number, 1_000_000_000_000.0)


def _status_signed_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return max(-1_000_000_000_000.0, min(number, 1_000_000_000_000.0))


def _status_string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:limit]:
        text = _status_text(item)
        if text is not None and text not in result:
            result.append(text)
    return result


def _paper_trial_summary(value: object) -> dict | None:
    """Expose a paper experiment without projecting it as verified proof."""
    if (not isinstance(value, dict) or
            value.get("schema") != "paper-incumbent-trial.v1" or
            value.get("enabled") is not True or
            value.get("authorizing") is not False or
            value.get("proof_authority") is not False):
        return None
    state = value.get("state")
    if state not in {"running", "passed", "failed", "review_required", "blocked"}:
        return None
    eligible = value.get("entry_eligible")
    if not isinstance(eligible, bool) or (eligible and state not in {"running", "passed"}):
        return None
    activated = value.get("activation_confirmed") is True
    if eligible and not activated:
        return None
    result = {
        "schema": value["schema"], "enabled": True,
        "state": state, "entry_eligible": eligible,
        "activation_confirmed": activated,
        "authorizing": False, "proof_authority": False,
        "blockers": _status_string_list(value.get("blockers"), limit=8),
    }
    for key in ("trial_id", "candidate_id", "variant_id", "incumbent_identity",
                "verdict", "started_on"):
        result[key] = _status_text(value.get(key))
    for key in ("valid_sessions", "required_sessions", "closed_outcomes",
                "required_trades", "max_review_sessions"):
        result[key] = _status_nonnegative_int(value.get(key))
    if eligible and not all(result.get(key) for key in (
            "trial_id", "candidate_id", "variant_id", "incumbent_identity")):
        return None
    detail = value.get("verdict_detail")
    if isinstance(detail, dict):
        result["verdict_detail"] = {
            "state": _status_text(detail.get("state"), limit=40),
            "reason": _status_text(detail.get("reason"), limit=240),
            **{key: _status_signed_float(detail.get(key)) for key in (
                "total_r", "mean_r", "min_total_r", "min_mean_r", "net_pnl", "win_rate")},
        }
        confidence = detail.get("session_cluster_confidence")
        if isinstance(confidence, dict):
            result["verdict_detail"]["session_cluster_confidence"] = {
                "available": confidence.get("available") is True,
                **{key: _status_signed_float(confidence.get(key)) for key in (
                    "confidence", "lower_bound", "upper_bound")},
                **{key: _status_nonnegative_int(confidence.get(key)) for key in (
                    "observations", "session_clusters", "clusters")},
            }
    return result


def paper_selection_summary(value: object) -> dict | None:
    """Whitelist the one requested/resolved paper identity from a heartbeat."""
    if not isinstance(value, dict):
        return None
    state = _status_text(value.get("state"), limit=40)
    armed = value.get("armed")
    if state not in {"waiting_for_proof", "ready", "blocked", "paper_trial"} or not isinstance(
            armed, bool):
        return None
    trial = _paper_trial_summary(value.get("paper_trial"))
    if "paper_trial" in value and trial is None:
        return None
    if state == "paper_trial" and (trial is None or not trial["entry_eligible"] or not armed):
        return None
    if trial is not None and state == "ready":
        return None

    resolved = None
    raw_resolved = value.get("resolved")
    if isinstance(raw_resolved, dict):
        raw_proof = raw_resolved.get("proof")
        if trial is not None and raw_proof is not None:
            return None
        proof = None
        if isinstance(raw_proof, dict):
            proof = {
                key: _status_text(raw_proof.get(key))
                for key in ("run_id", "gate_hash", "config_hash", "lane")
            }
            if any(item is None for item in proof.values()):
                proof = None
        identity = {
            key: _status_text(raw_resolved.get(key))
            for key in ("candidate_id", "variant_id", "family")
        }
        if proof is not None and all(identity.values()):
            resolved = {**identity, "proof": proof}
        elif (trial is not None and raw_proof is None and all(identity.values()) and
              identity["candidate_id"] == trial["candidate_id"] and
              identity["variant_id"] == trial["variant_id"]):
            resolved = {**identity, "proof": None, "proof_authority": False}
    # A ready label without one verified identity is less honest than no
    # projection at all. Waiting/blocked states may legitimately be unresolved.
    if state in {"ready", "paper_trial"} and resolved is None:
        return None
    result = {
        "configured_strategy": _status_text(value.get("configured_strategy")),
        "selection_mode": _status_text(value.get("selection_mode")),
        "requested_variant": _status_text(value.get("requested_variant")),
        "resolved": resolved,
        "blocker_code": _status_text(value.get("blocker_code"), limit=80),
        "armed": armed,
        "state": state,
    }
    if trial is not None:
        result["paper_trial"] = trial
    return result


def _shadow_accounts_summary(value: object, arms: list[dict]) -> dict | None:
    if (not isinstance(value, dict) or
            value.get("schema") != "diagnostic-forward-accounts-summary.v1" or
            _status_nonnegative_int(value.get("actual_fills")) != 0):
        return None
    identity_by_candidate = {
        _status_text(arm.get("candidate_id")): {
            key: _status_text(arm.get(key)) for key in ("family", "role", "variant_id")}
        for arm in arms}
    result = {
        "schema": value["schema"], "actual_fills": 0,
        "authorizing": False, "proof_authority": False,
    }
    for key in ("account_count", "priced_account_count", "unpriced_account_count",
                "open_positions", "closed_positions", "orders", "modeled_fills",
                "entry_fills", "exit_fills", "late_data_gap_positions"):
        result[key] = _status_nonnegative_int(value.get(key))
    for key in ("cash", "equity", "realized_pnl", "unrealized_pnl"):
        result[key] = _status_signed_float(value.get(key))
    projected = []
    seen = set()
    candidates = value.get("by_candidate")
    for raw in candidates[:24] if isinstance(candidates, list) else []:
        if not isinstance(raw, dict):
            continue
        candidate = _status_text(raw.get("candidate_id"))
        if not candidate or candidate in seen or candidate not in identity_by_candidate:
            continue
        seen.add(candidate)
        item = {"candidate_id": candidate, **identity_by_candidate[candidate],
                "mark_status": _status_text(raw.get("mark_status"), limit=40),
                "last_event_at": _status_text(raw.get("last_event_at"), limit=80)}
        item.update({key: _status_signed_float(raw.get(key)) for key in (
            "cash", "equity", "realized_pnl", "unrealized_pnl")})
        item.update({key: _status_nonnegative_int(raw.get(key)) for key in (
            "open_positions", "closed_positions", "fills", "late_data_gaps")})
        projected.append(item)
    result["by_candidate"] = projected
    return result


def _shadow_diagnostic_summary(value: object, *, max_age: float) -> dict | None:
    """Validate and bound non-authorizing fixed-cohort shadow telemetry."""
    if not isinstance(value, dict):
        return None

    candidate_ids = _status_string_list(
        value.get("candidate_identities"), limit=25)
    raw_arms = value.get("arms")
    arms = ([item for item in raw_arms[:25] if isinstance(item, dict)]
            if isinstance(raw_arms, list) else [])
    family_roles: dict[str, set[str]] = {}
    arm_candidate_ids: set[str] = set()
    for arm in arms:
        family = _status_text(arm.get("family"))
        role = _status_text(arm.get("role"), limit=20)
        candidate_id = _status_text(arm.get("candidate_id"))
        if family and role in {"baseline", "variant"}:
            family_roles.setdefault(family, set()).add(role)
        if candidate_id:
            arm_candidate_ids.add(candidate_id)

    decision_raw = value.get("decision_counts")
    decision_raw = decision_raw if isinstance(decision_raw, dict) else {}
    by_kind_raw = decision_raw.get("by_kind")
    by_kind: dict[str, int] = {}
    if isinstance(by_kind_raw, dict):
        for key, count in list(by_kind_raw.items())[:16]:
            name = _status_text(key, limit=60)
            number = _status_nonnegative_int(count)
            if name is not None and number is not None:
                by_kind[name] = number
    rejection_raw = value.get("rejection_counts")
    rejection_raw = rejection_raw if isinstance(rejection_raw, dict) else {}

    source_lag = _status_nonnegative_float(value.get("source_lag_seconds"))
    poll_duration = _status_nonnegative_float(
        value.get("poll_duration_seconds"))
    flags = {
        key: value.get(key) if isinstance(value.get(key), bool) else None
        for key in (
            "enabled", "diagnostic", "authorizing", "gate_eligible",
            "promotion_eligible", "online_fdr", "actual_fill_claims",
            "realized_pnl_authorizing",
        )
    }
    diagnostic_only = (
        flags["enabled"] is True and
        flags["diagnostic"] is True and
        flags["authorizing"] is False and
        flags["gate_eligible"] is False and
        flags["promotion_eligible"] is False and
        flags["actual_fill_claims"] is False and
        flags["realized_pnl_authorizing"] is False
    )
    families_missing_raw = value.get("families_missing")
    families_missing = _status_string_list(families_missing_raw, limit=12)
    families_without_decisions = _status_string_list(
        value.get("families_without_decisions"), limit=12)
    families_total = _status_nonnegative_int(value.get("families_total"))
    families_covered = _status_nonnegative_int(
        value.get("families_covered"))
    baseline_count = _status_nonnegative_int(value.get("baseline_count"))
    variant_count = _status_nonnegative_int(value.get("variant_count"))
    actual_fills = _status_nonnegative_int(value.get("actual_fills"))
    cohort_identity = _status_text(value.get("cohort_identity"))
    activation_identity = _status_text(value.get("activation_identity"))
    activation_status = _status_text(value.get("activation_status"), limit=40)
    cohort_active = bool(
        cohort_identity and activation_identity and activation_status == "active")
    coverage_complete = bool(
        families_total == 12 and families_covered == 12 and
        isinstance(families_missing_raw, list) and not families_missing and
        baseline_count == 12 and variant_count == 12 and
        len(candidate_ids) == 24 and len(set(candidate_ids)) == 24 and
        isinstance(raw_arms, list) and len(raw_arms) == 24 and len(arms) == 24 and
        len(arm_candidate_ids) == 24 and
        set(candidate_ids) == arm_candidate_ids and
        len(family_roles) == 12 and
        all(roles == {"baseline", "variant"}
            for roles in family_roles.values()))
    metadata_valid = bool(
        value.get("schema") == "diagnostic-shadow-coverage.v1" and
        diagnostic_only and flags["online_fdr"] is False and
        actual_fills == 0 and poll_duration is not None)

    result = {
        "schema": _status_text(value.get("schema")),
        **flags,
        "diagnostic_only": diagnostic_only,
        "proof_authority": False if diagnostic_only else None,
        "metadata_valid": metadata_valid,
        "cohort_active": cohort_active,
        "coverage_complete": coverage_complete,
        "families_total": families_total,
        "families_covered": families_covered,
        "families_missing": families_missing,
        "families_observed": _status_nonnegative_int(
            value.get("families_observed")),
        "families_without_decisions": families_without_decisions,
        "baseline_count": baseline_count,
        "variant_count": variant_count,
        "candidate_count": len(candidate_ids),
        "arms_total": len(arms),
        "cohort_identity": cohort_identity,
        "activation_identity": activation_identity,
        "activation_status": activation_status,
        "warmup_session": _status_text(value.get("warmup_session"), limit=40),
        "observation_status": _status_text(
            value.get("observation_status"), limit=80),
        "decision_counts": {
            "total": _status_nonnegative_int(decision_raw.get("total")),
            "this_poll": _status_nonnegative_int(
                decision_raw.get("this_poll")),
            "warmup": _status_nonnegative_int(decision_raw.get("warmup")),
            "by_kind": by_kind,
            **{key: _status_nonnegative_int(decision_raw.get(key)) for key in (
                "evaluated_total", "compacted_no_trade", "compacted_no_data")},
        },
        "rejection_counts": {
            key: _status_nonnegative_int(rejection_raw.get(key))
            for key in (
                "reject", "unpriced", "preactivation",
                "preactivation_this_poll",
            )
        },
        "quoteable_virtual_opens": _status_nonnegative_int(
            value.get("quoteable_virtual_opens")),
        "unpriced_virtual_opens": _status_nonnegative_int(
            value.get("unpriced_virtual_opens")),
        "replay_modeled_fills": _status_nonnegative_int(
            value.get("replay_modeled_fills")),
        "warmup_replay_modeled_fills": _status_nonnegative_int(
            value.get("warmup_replay_modeled_fills")),
        "actual_fills": actual_fills,
        "poll_duration_seconds": poll_duration,
        "source_lag_seconds": source_lag,
        "source_data_fresh": (
            source_lag <= float(max_age) if source_lag is not None else None),
    }
    forward_accounts = (_shadow_accounts_summary(value.get("forward_accounts"), arms)
                        if diagnostic_only else None)
    if forward_accounts is not None:
        result["forward_accounts"] = forward_accounts
    for key in ("by_reason", "unpriced_by_reason"):
        raw = rejection_raw.get(key)
        if isinstance(raw, dict):
            result["rejection_counts"][key] = {
                name: count for reason, number in list(raw.items())[:24]
                if (name := _status_text(reason, limit=120)) is not None
                and (count := _status_nonnegative_int(number)) is not None}
    return result


def _fresh(timestamp: object, max_age: float, now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else float(now)) - float(timestamp)
    except (TypeError, ValueError):
        return False
    return -5 <= age <= float(max_age)


def _timestamp_epoch(value: object) -> float | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if value in (None, ""):
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _quantile(values: list[float], fraction: float) -> float | None:
    """Return a bounded nearest-rank quantile for telemetry only."""
    if not values:
        return None
    ordered = sorted(float(item) for item in values)
    index = min(len(ordered) - 1,
                max(0, int(math.ceil(len(ordered) * float(fraction))) - 1))
    return ordered[index]


def _market_session_status(index: dict, now: float) -> str:
    current = datetime.fromtimestamp(float(now), timezone.utc)
    local = current.astimezone(NEW_YORK)
    calendar = index.get("session_calendar")
    record = (calendar.get(local.date().isoformat())
              if isinstance(calendar, dict) else None)
    if isinstance(record, dict):
        if (record.get("status") == "closed" and
                record.get("source") == "alpaca_calendar"):
            return "closed"
        opened = _timestamp_epoch(record.get("open"))
        closed = _timestamp_epoch(record.get("close"))
        if opened is not None and closed is not None and opened < closed:
            return "open" if opened <= now < closed else "closed"
    if local.weekday() >= 5:
        return "closed"
    return "unknown"


def _market_data_readiness(
        index: dict, *, symbols: list[str], selected_feed: str,
        configured_feed: str | None, now: float,
        index_migration_pending: bool) -> dict:
    raw = index.get("observation_watermarks")
    watermarks = raw if isinstance(raw, dict) else {}
    required = symbols or sorted(str(symbol).strip().upper()
                                 for symbol in watermarks if str(symbol).strip())
    observations: dict[str, dict] = {}
    missing_quotes: list[str] = []
    missing_bars: list[str] = []
    quote_ages: list[float] = []
    bar_ages: list[float] = []
    for symbol in required:
        record = watermarks.get(symbol)
        record = record if isinstance(record, dict) else {}
        quote_epoch = _timestamp_epoch(record.get("quote"))
        bar_epoch = _timestamp_epoch(record.get("bar"))
        quote_age = None if quote_epoch is None else float(now) - quote_epoch
        bar_age = None if bar_epoch is None else float(now) - bar_epoch
        if quote_age is None:
            missing_quotes.append(symbol)
        else:
            quote_ages.append(quote_age)
        if bar_age is None:
            missing_bars.append(symbol)
        else:
            bar_ages.append(bar_age)
        observations[symbol] = {
            "quote_watermark": record.get("quote"),
            "bar_watermark": record.get("bar"),
            "quote_age_seconds": quote_age,
            "bar_age_seconds": bar_age,
            "quote_fresh": (quote_age is not None and
                            -5.0 <= quote_age <=
                            AUTHORIZING_MARKET_DATA_MAX_AGE_SECONDS),
            "bar_fresh": (bar_age is not None and
                          -5.0 <= bar_age <=
                          AUTHORIZING_MARKET_DATA_MAX_AGE_SECONDS),
        }

    configured = str(configured_feed or "").strip().lower().replace("-", "_")
    selected = str(selected_feed or "").strip().lower().replace("-", "_")
    session_status = _market_session_status(index, now)
    stale_quotes = sorted(
        symbol for symbol, value in observations.items()
        if value["quote_age_seconds"] is not None and
        not value["quote_fresh"])
    stale_bars = sorted(
        symbol for symbol, value in observations.items()
        if value["bar_age_seconds"] is not None and
        not value["bar_fresh"])
    if session_status == "closed":
        status, reason = "market_closed", "market_closed"
    elif index_migration_pending:
        status, reason = "unknown", "recorder_index_migration_pending"
    elif not required or not watermarks:
        status, reason = "unknown", "observation_watermarks_missing"
    elif not selected:
        status, reason = "unknown", "recorded_feed_unknown"
    elif configured and selected != configured:
        status, reason = "feed_mismatch", "configured_feed_mismatch"
    elif missing_quotes:
        status = "unknown"
        reason = "quote_watermarks_missing:" + ",".join(sorted(missing_quotes))
    elif stale_quotes:
        status = "stale"
        reason = "quote_observations_stale:" + ",".join(stale_quotes)
    elif missing_bars:
        status = "unknown"
        reason = "bar_watermarks_missing:" + ",".join(sorted(missing_bars))
    elif stale_bars:
        status = "stale"
        reason = "bar_observations_stale:" + ",".join(stale_bars)
    else:
        status, reason = "ready", "fresh_exact_feed_quotes_and_bars"
    return {
        "market_data_ready": status == "ready",
        "market_data_fresh": status == "ready",
        "market_data_freshness_status": status,
        "market_data_reason": reason,
        "market_session_status": session_status,
        "authorization_max_age_seconds":
            AUTHORIZING_MARKET_DATA_MAX_AGE_SECONDS,
        "observation_ages": observations,
        "aggregate_observation_ages": {
            "quote_age_seconds": max(quote_ages) if quote_ages else None,
            "bar_age_seconds": max(bar_ages) if bar_ages else None,
        },
        "required_symbol_count": len(required),
        "missing_quote_symbol_count": len(missing_quotes),
        "missing_bar_symbol_count": len(missing_bars),
        "stale_quote_symbol_count": len(stale_quotes),
        "stale_bar_symbol_count": len(stale_bars),
        "missing_quote_symbols": missing_quotes[:64],
        "missing_bar_symbols": missing_bars[:64],
        "stale_quote_symbols": stale_quotes[:64],
        "stale_bar_symbols": stale_bars[:64],
    }


def trader(path: Path, max_age: float, *, now: float | None = None) -> dict:
    heartbeat = _read_json(path)
    status = str(heartbeat.get("status") or "missing")
    fresh = _fresh(heartbeat.get("updated_ts"), max_age, now)
    research_ok = not (
        heartbeat.get("research_expected") is True
        and (heartbeat.get("research_available") is not True
             or heartbeat.get("research_status") not in {"healthy", "disabled"}))
    reason = str(heartbeat.get("reason") or "").strip()
    # ``paused`` is a safe operator gate unless the payload explicitly
    # describes residual exposure or a failed/degraded operation.  Degraded
    # is never healthy: it is the runtime's assertion that safety could not
    # be proven (most importantly, an incomplete flatten).
    residual_risk = status == "degraded" or status == "failed" or any(
        marker in reason.lower()
        for marker in ("flatten", "residual", "incomplete", "failed", "error", "unavailable")
    )
    edge_gate_pause = (
        status == "paused"
        and reason == "validated_edge_required"
        and not residual_risk
    )
    operator_pause = (
        status in {"paused", "pausing"}
        and not residual_risk
        and not edge_gate_pause
    )
    classification = (
        "degraded_residual_risk" if residual_risk else
        "validated_edge_required" if edge_gate_pause else
        "operator_pause" if operator_pause else
        "healthy"
    )
    alert_kind = (
        "residual_risk" if residual_risk else
        "operator_pause" if operator_pause else
        None
    )
    ok = fresh and status in {"starting", "running", "paused", "pausing"} and research_ok and not residual_risk
    return _with_provenance({
        "ok": ok,
        "component": "trader",
        "status": status,
        "fresh": fresh,
        "research_available": heartbeat.get("research_available"),
        "research_status": heartbeat.get("research_status"),
        "reason": reason or None,
        "classification": classification,
        "pause_class": classification,
        "operator_pause": operator_pause,
        "edge_gate_pause": edge_gate_pause,
        "residual_risk": residual_risk,
        "alert": residual_risk,
        "alert_kind": alert_kind,
        "paper_selection": paper_selection_summary(
            heartbeat.get("paper_selection")),
    }, heartbeat)


def _recorder_cadence(attempt: dict) -> dict:
    """Project recorder timing evidence without making it a liveness gate."""
    raw = attempt.get("cadence")
    cadence = dict(raw) if isinstance(raw, dict) else {}
    interval = cadence.get("configured_interval_seconds",
                           attempt.get("configured_interval_seconds"))
    try:
        interval = float(interval) if interval is not None else None
    except (TypeError, ValueError):
        interval = None
    realized_values: list[float] = []
    raw_values = cadence.get("realized_intervals_seconds",
                            attempt.get("realized_intervals_seconds"))
    if isinstance(raw_values, (list, tuple)):
        for value in raw_values[-128:]:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number >= 0:
                realized_values.append(number)
    current = cadence.get("realized_interval_seconds")
    try:
        if current is not None and float(current) >= 0:
            realized_values.append(float(current))
    except (TypeError, ValueError):
        pass
    gap = cadence.get("gap_seconds")
    try:
        gap = float(gap) if gap is not None else None
    except (TypeError, ValueError):
        gap = None
    p50 = cadence.get("realized_interval_p50_seconds")
    p95 = cadence.get("realized_interval_p95_seconds")
    if p50 is None:
        p50 = _quantile(realized_values, .50)
    if p95 is None:
        p95 = _quantile(realized_values, .95)
    return {
        "configured_interval_seconds": interval,
        "realized_interval_seconds": (realized_values[-1]
                                       if realized_values else None),
        "realized_interval_p50_seconds": p50,
        "realized_interval_p95_seconds": p95,
        "gap_seconds": gap,
        "gap_detected": bool(cadence.get("gap_detected", gap is not None and
                                           interval is not None and gap > 0)),
        "samples": len(realized_values),
    }


def recorder(path: Path, max_age: float, *, now: float | None = None,
             configured_data_feed: str | None = None,
             configured_options_feed: str | None = None,
             configured_symbols: list[str] | tuple[str, ...] | None = None,
             strict_bar_feeds: str | None = None) -> dict:
    files = [item for item in path.rglob("*.csv") if item.is_file()]
    latest_csv = max((item.stat().st_mtime for item in files), default=None)
    # A deduplicated recorder cycle may append no corpus rows while still
    # advancing the durable sidecar.  Only the sidecar at the recorder root
    # is authoritative; nested files must not mask a stale recorder.
    index_path = path / ".recorder-index.json"
    index_write = index_path.stat().st_mtime if index_path.is_file() else None
    # A legacy recorder index can contain more than a million recent quote keys.
    # Health only needs compact metadata and must not compete with migration for
    # the recorder's cgroup, so defer decoding until the recorder rewrites it.
    index_oversized = bool(
        index_path.is_file() and
        index_path.stat().st_size > MAX_RECORDER_INDEX_BYTES)
    index = {} if index_oversized else _read_json(index_path)
    attempt = _read_json(path / ".recorder-status.json")
    try:
        attempt_ts = float(attempt.get("updated_ts"))
    except (TypeError, ValueError):
        attempt_ts = None
    # A stale retry failure must not mask later durable catch-up progress.  The
    # recorder writes its compact index after each successful chunk; a failure
    # in the current attempt is written afterwards and therefore remains newer.
    failure_superseded = bool(
        attempt.get("status") == "failed" and
        attempt_ts is not None and index_write is not None and
        index_write > attempt_ts)
    attempt_failed = (
        attempt.get("status") == "failed" and not failure_superseded)
    raw_coverage = index.get("bar_coverage")
    coverage = ({str(symbol): dict(value)
                 for symbol, value in raw_coverage.items()
                 if isinstance(value, dict)}
                if isinstance(raw_coverage, dict) else {})
    gap_symbols = sorted(
        symbol for symbol, value in coverage.items()
        if value.get("status") == "gap_observed")
    unobserved = sorted(
        symbol for symbol, value in coverage.items()
        if value.get("status") == "unobserved")
    gap_observations = 0
    for value in coverage.values():
        try:
            gap_observations += max(0, int(value.get("gap_observations") or 0))
        except (TypeError, ValueError):
            continue
    coverage_status = (
        "gap_observed" if gap_symbols else
        "unobserved" if unobserved else
        "covered" if coverage else
        "unknown"
    )
    writes = [timestamp for timestamp in (latest_csv, index_write)
              if timestamp is not None]
    latest = max(writes, default=None)
    current = time.time() if now is None else float(now)
    activity = max([timestamp for timestamp in (latest, attempt_ts)
                    if timestamp is not None], default=None)
    fresh = _fresh(activity, max_age, current)
    configured = [str(symbol).strip().upper() for symbol in
                  (configured_symbols or index.get("configured_symbols") or ())
                  if str(symbol).strip()]
    configured = sorted(set(configured))
    required_unobserved = [symbol for symbol in configured
                           if symbol not in coverage or
                           coverage[symbol].get("status") == "unobserved" or
                           not coverage[symbol].get("last_bar")]
    strict_raw = (strict_bar_feeds if strict_bar_feeds is not None else
                  os.getenv("ALPACA_RECORDER_STRICT_BAR_FEEDS", ""))
    strict = {item.strip().lower().replace("-", "_")
              for item in str(strict_raw).split(",") if item.strip()}
    selected_feed = str(index.get("data_feed") or
                        attempt.get("data_feed") or "").strip().lower()
    market_readiness = _market_data_readiness(
        index, symbols=configured, selected_feed=selected_feed,
        configured_feed=configured_data_feed, now=current,
        index_migration_pending=index_oversized)
    recorded_watermark = index.get("watermark")
    watermark_epoch = _timestamp_epoch(recorded_watermark)
    partition_sources = index.get("partition_sources")
    partition_sources = (partition_sources
                         if isinstance(partition_sources, dict) else {})
    provenance_counts: dict[str, int] = {}
    for source in partition_sources.values():
        mode = (str(source.get("source_mode") or "unknown")
                if isinstance(source, dict) else "unknown")
        provenance_counts[mode] = provenance_counts.get(mode, 0) + 1
    cadence = _recorder_cadence(attempt)
    closed_no_data_failure = bool(
        attempt_failed and
        market_readiness["market_session_status"] == "closed" and
        attempt.get("failure_kind") == "market_data_request_failed" and
        "no point-in-time bars or quotes" in str(attempt.get("error") or ""))
    blocking_attempt_failure = attempt_failed and not closed_no_data_failure
    strict_coverage = ("*" in strict or selected_feed in strict or
                       any(value.get("policy") == "strict"
                           for value in coverage.values()))
    coverage_failures = sorted(set(gap_symbols + required_unobserved)) \
        if strict_coverage else []
    coverage_reason = None
    if coverage_failures:
        details = []
        if gap_symbols:
            details.append("gap_observed=" + ",".join(gap_symbols))
        if required_unobserved:
            details.append("unobserved=" + ",".join(required_unobserved))
        coverage_reason = "strict_bar_coverage_failed: " + "; ".join(details)
    service_liveness_ok = fresh and (bool(files) or closed_no_data_failure)
    data_readiness = {
        "ok": bool(market_readiness["market_data_ready"] and
                   not coverage_failures),
        "status": market_readiness["market_data_freshness_status"],
        "market_data_ready": bool(market_readiness["market_data_ready"]),
        "watermark": recorded_watermark,
        "watermark_age_seconds": (None if watermark_epoch is None else
                                   current - watermark_epoch),
        "provenance": {
            "partition_count": len(partition_sources),
            "source_mode_counts": provenance_counts,
            "partition_sources": dict(sorted(partition_sources.items())[-64:]),
        },
        "cadence": cadence,
    }
    result = {
        "ok": (service_liveness_ok and not blocking_attempt_failure and
               not coverage_failures),
        "component": "recorder",
        "corpus_root": str(path),
        "status": ("recording_market_closed"
                   if closed_no_data_failure else
                   str(attempt.get("failure_kind") or "failed")
                   if blocking_attempt_failure else
                   "recording" if files and fresh else "stale_or_empty"),
        "fresh": fresh,
        "service_liveness": {
            "ok": service_liveness_ok,
            "status": "alive" if service_liveness_ok else "stale_or_empty",
            "latest_activity_ts": activity,
        },
        "series_files": len(files),
        "latest_write_ts": latest,
        "latest_csv_write_ts": latest_csv,
        "index_write_ts": index_write,
        "index_migration_pending": index_oversized,
        "data_feed": index.get("data_feed"),
        "capture_policy": attempt.get("capture_policy") or index.get("capture_policy"),
        "deferred_catchup": (index.get("deferred_catchup")
                             if isinstance(index.get("deferred_catchup"), dict) else None),
        "configured_data_feed": (attempt.get("data_feed") or
                                  configured_data_feed),
        "configured_options_feed": (attempt.get("options_feed") or
                                     configured_options_feed),
        "last_attempt_ts": attempt.get("updated_ts"),
        "last_error": attempt.get("error"),
        "failure_kind": attempt.get("failure_kind"),
        "retryable": attempt.get("retryable"),
        "probe": attempt.get("probe"),
        "coverage_status": coverage_status,
        "bar_gap_symbols": gap_symbols,
        "bar_unobserved_symbols": unobserved,
        "bar_gap_observations": gap_observations,
        "bar_coverage": coverage,
        "configured_symbols": configured,
        "strict_bar_policy": strict_coverage,
        "bar_coverage_failures": coverage_failures,
        "coverage_reason": coverage_reason,
        # Data readiness is deliberately separate from service liveness: a
        # fresh recorder process may still be missing/stale an authorizing
        # quote or bar and must not be presented as trade-ready.
        "data_readiness": data_readiness,
        "readiness": data_readiness,
        "recorded_watermark": recorded_watermark,
        "watermark_age_seconds": (None if watermark_epoch is None else
                                   current - watermark_epoch),
        "partition_provenance": dict(sorted(partition_sources.items())[-64:]),
        "cadence": cadence,
        **market_readiness,
    }
    if coverage_reason:
        result["reason"] = coverage_reason
        result["status"] = "degraded_bar_coverage"
    return _with_provenance(result, attempt)


def research(path: Path, max_age: float, *, now: float | None = None) -> dict:
    heartbeat = _read_json(path)
    status = str(heartbeat.get("status") or "missing")
    fresh = _fresh(heartbeat.get("updated_ts"), max_age, now)
    last_exit = heartbeat.get("last_exit_code")
    current = time.time() if now is None else float(now)
    deadline = heartbeat.get("deadline_ts")
    try:
        hung = status == "running" and deadline is not None and current > float(deadline)
    except (TypeError, ValueError):
        hung = status == "running"
    scheduler_operational = status in {
        "waiting", "running", "waiting_for_forward_sessions"}
    previous_cycle_degraded = last_exit not in {None, 0}
    waiting_after_no_data = (
        status == "waiting"
        and str(heartbeat.get("cycle_status") or "").lower() == "no_data"
        and last_exit == 2
    )
    cycle = heartbeat.get("research_cycle")
    preflight = structured_research_preflight(
        heartbeat.get("research_preflight"))
    if preflight is None and isinstance(cycle, dict):
        preflight = structured_research_preflight(cycle.get("preflight"))
    scheduler_liveness_ok = fresh and not hung and status in {
        "waiting", "running", "completed", "completed_no_edge", "failed",
        "no_data", "unevaluable", "search_exhausted",
        "llm_provider_failure", "waiting_for_forward_sessions"}
    terminal_status = str(cycle.get("status") if isinstance(cycle, dict)
                          else heartbeat.get("cycle_status") or status).lower()
    explicit_evidence = (cycle.get("evidence_available")
                         if isinstance(cycle, dict) else
                         heartbeat.get("evidence_available"))
    if terminal_status == "waiting_for_forward_sessions":
        evidence_available = False
    elif isinstance(explicit_evidence, bool):
        evidence_available = explicit_evidence
    elif terminal_status in {"failed", "no_data", "unevaluable",
                             "search_exhausted", "llm_provider_failure"}:
        evidence_available = False
    elif isinstance(cycle, dict):
        # Completed/no-edge is a valid negative observation; a completed cycle
        # with no proof, no-edge, and no outcomes is not evidence at all.
        evidence_available = bool(
            cycle.get("proofs") or cycle.get("no_edge") or
            cycle.get("outcomes") or terminal_status == "completed_no_edge")
    else:
        evidence_available = terminal_status in {"completed", "completed_no_edge"}
    readiness = derive_research_readiness(
        structured_research_progress(heartbeat.get("research_progress")),
        heartbeat.get("research_readiness"), now=current,
        deadline_ts=heartbeat.get("deadline_ts"))
    readiness_state = str(readiness.get("state") or "unknown")
    readiness_ok = readiness_state in {"pending", "ready"}
    cycle_ok = not previous_cycle_degraded and evidence_available
    # The Compose/container probe answers whether the scheduler service is
    # alive.  Research evidence and readiness are separate non-authorizing
    # diagnostics: a fresh waiting scheduler before its first cycle is healthy
    # as a process, while failed/no-data/unevaluable cycles remain degraded in
    # their own fields without declaring the scheduler dead.
    ok = scheduler_liveness_ok
    research_evidence_status = "available" if evidence_available else "unavailable"
    research_readiness_status = "ready" if readiness_ok else readiness_state
    research_status = ("healthy" if cycle_ok and readiness_ok else "degraded")
    result = {
        "ok": ok,
        "component": "research",
        "status": status,
        "health_status": "healthy" if ok else "degraded",
        "research_status": research_status,
        "fresh": fresh,
        "hung": hung,
        "job_id": heartbeat.get("job_id"),
        "started_ts": heartbeat.get("started_ts"),
        "completed_ts": heartbeat.get("completed_ts"),
        "last_exit_code": last_exit,
        "previous_cycle_degraded": previous_cycle_degraded,
        "previous_cycle_failed": previous_cycle_degraded,
        "scheduler_operational": scheduler_operational,
        "scheduler_liveness": {
            "ok": scheduler_liveness_ok,
            "status": status,
            "fresh": fresh,
            "hung": hung,
        },
        "cycle_status": terminal_status,
        "cycle_ok": cycle_ok,
        "evidence_available": evidence_available,
        "research_evidence_status": research_evidence_status,
        "readiness_ok": readiness_ok,
        "research_readiness_status": research_readiness_status,
        "waiting_after_no_data": waiting_after_no_data,
        "next_run_ts": heartbeat.get("next_run_ts"),
        "structured_failures": heartbeat.get("structured_failures") or [],
        # A transient provider outage is explicitly non-authorizing evidence:
        # deterministic research may continue, but operators must see that
        # the model lane was degraded in the terminal cycle and history.
        "research_preflight": preflight,
        "provider_preflight_status": (preflight.get("status")
                                       if preflight else None),
        "provider_preflight_degraded": bool(
            preflight and preflight.get("status") == "degraded"),
        # Keep the health response bounded even if an operator hand-edits a
        # status file; scheduler-produced values already satisfy this schema.
        "research_progress": structured_research_progress(
            heartbeat.get("research_progress")),
        "research_readiness": readiness,
    }
    if not scheduler_liveness_ok:
        result["reason"] = "research scheduler is stale or hung"
    elif status == "waiting" and cycle is None and last_exit is None:
        result["reason"] = "research scheduler waiting for first cycle"
    elif previous_cycle_degraded:
        result["reason"] = "previous research cycle failed"
    elif terminal_status == "waiting_for_forward_sessions":
        result["reason"] = readiness.get("reason") or "waiting for accepted forward sessions"
    elif not evidence_available:
        result["reason"] = "latest research cycle produced no usable evidence"
    elif not readiness_ok:
        result["reason"] = f"research readiness is {readiness_state}"
    return _with_provenance(result, heartbeat)


def shadow(path: Path, max_age: float, *, now: float | None = None) -> dict:
    """Health of the broker-free forward shadow polling loop."""
    heartbeat = _read_json(path)
    status = str(heartbeat.get("status") or "missing")
    fresh = _fresh(heartbeat.get("updated_ts"), max_age, now)
    raw_error = heartbeat.get("last_error")
    last_error = (str(raw_error)[:500] if raw_error not in {None, ""} else None)
    stale_tail = heartbeat.get("stale_tail")
    stale_tail = stale_tail if isinstance(stale_tail, dict) else {}
    signal_dispositions = heartbeat.get("signal_dispositions")
    if not isinstance(signal_dispositions, dict):
        signal_dispositions = stale_tail.get("signal_dispositions")
    stress_calibration = heartbeat.get("stress_calibration")
    if not isinstance(stress_calibration, dict):
        stress_calibration = stale_tail.get("stress_calibration")
    diagnostic = _shadow_diagnostic_summary(
        heartbeat.get("diagnostic_shadow"), max_age=max_age)
    liveness_ok = fresh and status == "running"
    if not fresh:
        coverage_status = "stale_heartbeat"
    elif status != "running":
        coverage_status = "service_not_running"
    elif diagnostic is None:
        coverage_status = "diagnostic_metadata_missing"
    elif not diagnostic["metadata_valid"]:
        coverage_status = "diagnostic_metadata_invalid"
    elif not diagnostic["cohort_active"]:
        coverage_status = "active_cohort_missing"
    elif not diagnostic["coverage_complete"]:
        coverage_status = "family_coverage_incomplete"
    elif diagnostic["source_lag_seconds"] is None:
        coverage_status = "fresh_data_unavailable"
    elif not diagnostic["source_data_fresh"]:
        coverage_status = "source_data_stale"
    else:
        coverage_status = "ready"
    coverage_ready = coverage_status == "ready"
    current = time.time() if now is None else float(now)
    try:
        heartbeat_age = current - float(heartbeat.get("updated_ts"))
        heartbeat_age = (round(max(0.0, heartbeat_age), 6)
                         if math.isfinite(heartbeat_age) else None)
    except (TypeError, ValueError):
        heartbeat_age = None
    return _with_provenance({
        "ok": liveness_ok,
        "component": "shadow",
        "status": status,
        "fresh": fresh,
        "heartbeat_age_seconds": heartbeat_age,
        "coverage_ready": coverage_ready,
        "coverage_status": coverage_status,
        "diagnostic_shadow": diagnostic,
        "last_error": last_error,
        "candidates": heartbeat.get("candidates"),
        "events": heartbeat.get("events"),
        "decisions": heartbeat.get("decisions"),
        "ingested_events": heartbeat.get("ingested_events"),
        "pruned_replay_diffs": heartbeat.get("pruned_replay_diffs"),
        "retention_days": heartbeat.get("retention_days"),
        "retention_floor_ts": heartbeat.get("retention_floor_ts"),
        "retention_gap_watermark": heartbeat.get("retention_gap_watermark"),
        "stale_tail": heartbeat.get("stale_tail"),
        "signal_dispositions": signal_dispositions,
        "stress_calibration": stress_calibration,
        "quarantine_through_session": heartbeat.get(
            "quarantine_through_session"),
        # Shadow capacity is diagnostic only.  Keep it bounded by forwarding
        # the already-capped summary emitted by the worker/ingester.
        "opportunity_capacity": heartbeat.get("opportunity_capacity") or
        heartbeat.get("capacity"),
    }, heartbeat)


def watchdog(path: Path, max_age: float, *, now: float | None = None) -> dict:
    status_payload = _read_json(path)
    status = str(status_payload.get("status") or "missing")
    fresh = _fresh(status_payload.get("updated_ts"), max_age, now)
    flattened = status_payload.get("flattened")
    # An ``acted`` record is healthy only when flatten completion was
    # explicitly confirmed.  Older/malformed records and degraded/failed
    # records remain visible but are residual-risk alerts.
    incomplete = status == "acted" and flattened is not True
    residual_risk = status in {"degraded", "failed"} or incomplete
    alert_kind = "residual_risk" if residual_risk else None
    return _with_provenance({
        "ok": fresh and status in {"watching", "acted"} and not incomplete,
        "component": "watchdog",
        "status": status,
        "fresh": fresh,
        "reason": status_payload.get("reason"),
        "flattened": flattened,
        "classification": "degraded_residual_risk" if residual_risk else "healthy",
        "pause_class": "degraded_residual_risk" if residual_risk else "healthy",
        "residual_risk": residual_risk,
        "alert": residual_risk,
        "alert_kind": alert_kind,
    }, status_payload)


def dashboard(url: str, timeout: float = 3.0) -> dict:
    payload = {}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ok = response.status == 200 and payload.get("ok") is True
    except Exception:                                      # noqa: BLE001
        ok = False
    return _with_provenance({"ok": ok, "component": "dashboard",
                             "status": "up" if ok else "unreachable"},
                            payload if isinstance(payload, dict) else None)


def _trader_path(args) -> Path:
    if args.path:
        return Path(args.path)
    raw = load_config(args.config)
    broker = raw.get("broker") if isinstance(raw.get("broker"), dict) else {}
    mode = str(raw.get("mode") or broker.get("mode") or "paper").lower()
    if mode not in {"paper", "live"}:
        raise ValueError("config mode must be paper or live")
    return Path(args.runtime_root) / mode / "heartbeat.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="component", required=True)

    item = sub.add_parser("trader")
    item.add_argument("--path")
    item.add_argument("--config", default="config.yaml")
    item.add_argument("--runtime-root", default="runtime")
    item.add_argument("--max-age", type=float, default=900)

    item = sub.add_parser("recorder")
    item.add_argument("--path", default="runtime/research/recorded")
    item.add_argument("--max-age", type=float, default=900)
    item.add_argument("--config", default=None)

    item = sub.add_parser("research")
    item.add_argument("--path", default="runtime/health/research.json")
    item.add_argument("--max-age", type=float, default=180)

    item = sub.add_parser("shadow")
    item.add_argument("--path", default="runtime/research/shadow-health.json")
    item.add_argument("--max-age", type=float, default=180)

    item = sub.add_parser("watchdog")
    item.add_argument("--path", default="runtime/health/watchdog.json")
    item.add_argument("--max-age", type=float, default=180)

    item = sub.add_parser("dashboard")
    item.add_argument("--url", default="http://127.0.0.1:8080/healthz")
    item.add_argument("--timeout", type=float, default=3)

    item = sub.add_parser("parity")
    item.add_argument("paths", nargs="+",
                      help="health/status JSON files from services to compare")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.component == "trader":
            result = trader(_trader_path(args), args.max_age)
        elif args.component == "recorder":
            config_symbols = None
            config_feed = config_options = None
            if args.config:
                raw = load_config(args.config)
                universe = raw.get("universe") if isinstance(raw, dict) else {}
                broker = raw.get("broker") if isinstance(raw, dict) else {}
                config_symbols = (universe or {}).get("symbols") or []
                config_feed = (broker or {}).get("data_feed")
                config_options = (broker or {}).get("options_feed")
            result = recorder(
                Path(args.path), args.max_age,
                configured_symbols=config_symbols,
                configured_data_feed=config_feed,
                configured_options_feed=config_options)
        elif args.component == "research":
            result = research(Path(args.path), args.max_age)
        elif args.component == "shadow":
            result = shadow(Path(args.path), args.max_age)
        elif args.component == "watchdog":
            result = watchdog(Path(args.path), args.max_age)
        elif args.component == "dashboard":
            result = dashboard(args.url, args.timeout)
        else:
            records = []
            for raw_path in args.paths:
                payload = _read_json(Path(raw_path))
                payload.setdefault("component", Path(raw_path).stem)
                records.append(payload)
            result = deployment_parity(records)
    except Exception as exc:                               # noqa: BLE001
        result = {"ok": False, "component": args.component,
                  "status": type(exc).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
