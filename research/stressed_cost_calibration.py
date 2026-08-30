"""Deterministic, diagnostic calibration of the entry-cost stress scenario.

The expected-cost schedule in :mod:`research.quote_costs` is continuous (a
spread percentile and a depth impact).  Runtime risk, however, deliberately
accepts only the preregistered stress ladder ``9/15/25/50`` bps.  This module
bridges those two representations without changing runtime configuration:
fit cells select a ladder rung, validation cells may keep or widen it, and
missing or untrusted evidence falls back to the configured 25 bps rung.

The returned object is JSON-like and content-addressed.  It is suitable for a
rerun report or a persisted diagnostic artifact, but it is not self-authorizing:
runtime uses it only after an operator explicitly enables its path.
"""

from __future__ import annotations

import math
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .costs import COST_STRESS_SCENARIOS_BPS, DEFAULT_FEE_BPS
from .edge_ledger import content_hash


STRESS_CALIBRATION_SCHEMA = "stressed-cost-calibration.v1"
DEFAULT_FALLBACK_SCENARIO_BPS = 25.0
DEFAULT_MIN_SESSIONS_PER_CELL = 5
DEFAULT_VALIDATION_MATERIALITY_RATIO = 1.25


class StressCalibrationError(ValueError):
    """Raised for malformed calibration arguments or a foreign schedule."""


def _session_values(value: Any) -> list[str]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set)):
        try:
            value = tuple(value)
        except TypeError:
            return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _session_key(value: str) -> tuple[int, Any] | None:
    """Parse canonical ISO session IDs; unknown IDs cannot authorize."""
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return 0, parsed.timestamp()
        return 0, float(parsed.toordinal() * 86400 +
                         parsed.hour * 3600 + parsed.minute * 60 + parsed.second)
    except (TypeError, ValueError):
        try:
            return 1, float(date.fromisoformat(raw).toordinal() * 86400)
        except (TypeError, ValueError):
            return None


def _session_kind(value: str) -> str | None:
    """Classify canonical date-only versus datetime session identifiers."""
    raw = str(value).strip()
    try:
        date.fromisoformat(raw)
    except (TypeError, ValueError):
        pass
    else:
        return "date"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return "datetime" if parsed.tzinfo is not None else "datetime_naive"


def _latest_session(values: Iterable[str]) -> str | None:
    sessions = _session_values(values)
    keyed = [(key, value) for value in sessions
             if (key := _session_key(value)) is not None]
    return max(keyed, key=lambda item: item[0])[1] if keyed else None


def _artifact_body(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Return the content-addressed body without its self hash."""
    body = dict(artifact)
    body.pop("content_hash", None)
    return body


def verify_stress_calibration_artifact(
        artifact: Mapping[str, Any] | None, *,
        expected_provider: str | None = None,
        expected_feed: str | None = None) -> tuple[bool, str | None]:
    """Check whether an artifact is activation-ready.

    Calibration remains diagnostic by default.  Activation is an explicit
    operator decision and is accepted only for a held-out validated artifact:
    every emitted cell must be selected and usable, both provenance values
    must match the runtime identity, and the artifact's self hash must verify.
    """
    if not isinstance(artifact, Mapping):
        return False, "artifact_missing"
    if str(artifact.get("schema") or "") != STRESS_CALIBRATION_SCHEMA:
        return False, "artifact_schema_mismatch"
    digest = str(artifact.get("content_hash") or "")
    if not digest or digest != content_hash(_artifact_body(artifact)):
        return False, "artifact_content_hash_invalid"
    if artifact.get("diagnostic_only") is not True or artifact.get("authorizing") is not False:
        return False, "artifact_authority_invalid"
    validation_hash = str(artifact.get("validation_schedule_hash") or "")
    fit_hash = str(artifact.get("fit_schedule_hash") or "")
    if not fit_hash or not validation_hash or fit_hash == validation_hash:
        return False, "validation_evidence_missing"
    if artifact.get("validation_failure_reason") not in (None, ""):
        return False, "validation_failure"
    provider = _text(artifact.get("provider"))
    feed = _text(artifact.get("feed"))
    expected_provider_text = _text(expected_provider)
    expected_feed_text = _text(expected_feed)
    if not provider:
        return False, "provider_provenance_invalid"
    if expected_provider is not None and provider != expected_provider_text:
        return False, "provider_mismatch"
    if feed not in {"iex", "sip"}:
        return False, "feed_non_authorizing"
    if expected_feed is not None and feed != expected_feed_text:
        return False, "feed_mismatch"
    cells = artifact.get("cells")
    if not isinstance(cells, list) or not cells:
        return False, "cells_missing"
    latest_cell_sessions: list[str] = []
    for cell in cells:
        if not isinstance(cell, Mapping):
            return False, "cell_malformed"
        if cell.get("status") != "selected" or cell.get("usable") is not True:
            return False, "cell_not_usable"
        if cell.get("validation_used") is not True:
            return False, "cell_validation_missing"
        if cell.get("fallback_reason") not in (None, ""):
            return False, "cell_fallback"
        if str(cell.get("validation_schedule_hash") or "") != validation_hash:
            return False, "cell_validation_hash_mismatch"
        if str(cell.get("fit_schedule_hash") or "") != fit_hash:
            return False, "cell_fit_hash_mismatch"
        if _text(cell.get("provider")) != provider:
            return False, "cell_provider_mismatch"
        if _text(cell.get("feed")) != feed:
            return False, "cell_feed_mismatch"
        selected = _number(cell.get("selected_scenario_bps"))
        if selected not in COST_STRESS_SCENARIOS_BPS:
            return False, "cell_scenario_invalid"
        min_quotes = _number(artifact.get("min_quotes_per_cell"))
        min_sessions = _number(artifact.get("min_sessions_per_cell"))
        fit_quotes = _number(cell.get("quote_count"))
        fit_session_count = _number(cell.get("session_count"))
        validation_quotes = _number(cell.get("validation_quote_count"))
        validation_session_count = _number(cell.get("validation_session_count"))
        if (min_quotes is None or min_sessions is None or
                fit_quotes is None or fit_quotes < min_quotes or
                fit_session_count is None or fit_session_count < min_sessions or
                validation_quotes is None or validation_quotes < min_quotes or
                validation_session_count is None or
                validation_session_count < min_sessions):
            return False, "cell_coverage_insufficient"
        fit_sessions = _session_values(cell.get("fit_sessions"))
        validation_sessions = _session_values(cell.get("validation_sessions"))
        if (len(fit_sessions) < int(min_sessions) or
                len(validation_sessions) < int(min_sessions)):
            return False, "cell_sessions_missing"
        if set(fit_sessions) & set(validation_sessions):
            return False, "cell_sessions_overlap"
        fit_keys = [_session_key(item) for item in fit_sessions]
        validation_keys = [_session_key(item) for item in validation_sessions]
        if any(item is None for item in fit_keys + validation_keys):
            return False, "cell_sessions_unordered"
        kinds = {_session_kind(item) for item in (*fit_sessions,
                                                   *validation_sessions)}
        if None in kinds or len(kinds) != 1:
            return False, "cell_sessions_mixed_kinds"
        if max(item for item in fit_keys if item is not None) >= min(
                item for item in validation_keys if item is not None):
            return False, "cell_sessions_not_chronological"
        effective_after = str(cell.get("effective_after_session") or "")
        latest_validation = _latest_session(validation_sessions)
        if not effective_after or effective_after != latest_validation:
            return False, "effective_after_session_invalid"
        if _session_key(effective_after) is None:
            return False, "effective_after_session_invalid"
        latest_cell_sessions.append(effective_after)
    if (str(artifact.get("effective_after_session") or "") !=
            _latest_session(latest_cell_sessions)):
        return False, "effective_after_session_invalid"
    return True, None


def load_stress_calibration_artifact(path: str | Path | None) -> tuple[Mapping[str, Any] | None, str | None]:
    """Read a JSON artifact without allowing I/O or JSON errors to authorize."""
    if path in (None, ""):
        return None, "artifact_path_missing"
    try:
        parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, UnicodeError):
        return None, "artifact_unavailable"
    if not isinstance(parsed, Mapping):
        return None, "artifact_malformed"
    return parsed, None


def resolve_stress_scenario(
        artifact: Mapping[str, Any] | None, *, symbol: str | None = None,
        bucket: str | None = None, fallback_scenario_bps: float = DEFAULT_FALLBACK_SCENARIO_BPS,
        operator_enabled: bool = False, expected_provider: str | None = None,
        expected_feed: str | None = None,
        observation_session: str | None = None) -> tuple[float, str | None]:
    """Resolve one symbol/time cell, failing closed to the configured rung."""
    fallback = _number(fallback_scenario_bps)
    if fallback not in COST_STRESS_SCENARIOS_BPS:
        fallback = DEFAULT_FALLBACK_SCENARIO_BPS
    if not operator_enabled:
        return float(fallback), "activation_disabled"
    valid, reason = verify_stress_calibration_artifact(
        artifact, expected_provider=expected_provider, expected_feed=expected_feed)
    if not valid:
        return float(fallback), reason
    if observation_session in (None, ""):
        return float(fallback), "observation_session_missing"
    observation_key = _session_key(str(observation_session))
    if observation_key is None:
        return float(fallback), "observation_session_invalid"
    wanted_symbol = str(symbol or "").strip().upper()
    wanted_bucket = None if bucket in (None, "") else str(bucket)
    cells = [cell for cell in artifact.get("cells", ())
             if isinstance(cell, Mapping) and
             str(cell.get("symbol") or "").strip().upper() == wanted_symbol]
    selected = None
    if wanted_bucket is not None:
        selected = next((cell for cell in cells
                         if str(cell.get("bucket") or "") == wanted_bucket), None)
        # An aggregate cell is valid only when no bucket-level evidence exists
        # for this symbol; missing bucket evidence otherwise falls back.
        if selected is None and not any(cell.get("bucket") not in (None, "") for cell in cells):
            selected = next((cell for cell in cells if cell.get("bucket") in (None, "")), None)
    else:
        selected = next((cell for cell in cells if cell.get("bucket") in (None, "")), None)
        if selected is None and len(cells) == 1:
            selected = cells[0]
    if selected is None:
        return float(fallback), "cell_missing"
    # The artifact boundary is global: a multi-cell artifact must not let one
    # symbol become active while another symbol's later validation remains in
    # the future relative to the replay observation.
    boundary = _session_key(str(artifact.get("effective_after_session") or ""))
    if boundary is None or observation_key <= boundary:
        return float(fallback), "observation_before_effective_after_session"
    value = _number(selected.get("selected_scenario_bps"))
    if value not in COST_STRESS_SCENARIOS_BPS:
        return float(fallback), "cell_scenario_invalid"
    return float(value), None


def activation_overlay(artifact: Mapping[str, Any], *, expected_provider: str | None = None,
                       expected_feed: str | None = None) -> dict[str, Any]:
    """Build a compact operator-facing recommendation with explicit reasons."""
    valid, reason = verify_stress_calibration_artifact(
        artifact, expected_provider=expected_provider, expected_feed=expected_feed)
    return {
        "schema": "stressed-cost-activation.v1",
        "ready": bool(valid),
        "reasons": [] if valid else [reason or "activation_not_ready"],
        "diagnostic_only": True,
        "authorizing": False,
        "config_overlay": {
            "risk": {"stressed_cost_calibration_enabled": bool(valid)},
        },
        "artifact_content_hash": artifact.get("content_hash") if isinstance(artifact, Mapping) else None,
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip().lower()


def _section_value(section: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = section
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _coverage(section: Mapping[str, Any]) -> tuple[int, int]:
    count = _number(section.get("quote_count"))
    sessions = _number(section.get("session_count"))
    return (int(count) if count is not None and count >= 0 else 0,
            int(sessions) if sessions is not None and sessions >= 0 else 0)


def _sessions(section: Mapping[str, Any]) -> list[str]:
    """Return stable session identifiers when the schedule persisted them."""
    raw = section.get("sessions") if isinstance(section, Mapping) else None
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple, set)):
        return []
    return sorted({str(item) for item in raw if item not in (None, "")})


def _provenance(schedule: Mapping[str, Any] | None, *, expected_provider: str | None,
                expected_feed: str | None) -> tuple[bool, str | None, str | None, str | None]:
    """Return ``(valid, provider, feed, reason)`` for a measured schedule."""
    if schedule is None:
        # Validation is optional; absence means fit-only evidence, not an
        # invalid provider claim.  The fit schedule is still mandatory.
        return True, None, None, None
    measured = schedule.get("measured")
    if not isinstance(measured, Mapping):
        return False, None, None, "missing_measurement_metadata"
    raw_providers = measured.get("providers") or ()
    raw_feeds = measured.get("feeds") or ()
    if isinstance(raw_providers, str):
        raw_providers = (raw_providers,)
    if isinstance(raw_feeds, str):
        raw_feeds = (raw_feeds,)
    providers = {_text(item) for item in raw_providers
                 if _text(item)}
    feeds = {_text(item) for item in raw_feeds
             if _text(item)}
    provider = next(iter(providers)) if len(providers) == 1 else None
    feed = next(iter(feeds)) if len(feeds) == 1 else None
    if len(providers) != 1:
        return False, provider, feed, "provider_provenance_invalid"
    if len(feeds) != 1:
        return False, provider, feed, "feed_provenance_invalid"
    if expected_provider is not None and provider != _text(expected_provider):
        return False, provider, feed, "provider_mismatch"
    if expected_feed is not None and feed != _text(expected_feed):
        return False, provider, feed, "feed_mismatch"
    return True, provider, feed, None


def _validate_schedule(schedule: Mapping[str, Any] | None, *, schema: str) -> None:
    if schedule is None:
        return
    if str(schedule.get("schema")) != schema:
        raise StressCalibrationError(
            f"expected quote-cost schedule {schema}, got {schedule.get('schema')!r}")


def _schedule_hash(schedule: Mapping[str, Any] | None) -> str | None:
    if not isinstance(schedule, Mapping):
        return None
    value = schedule.get("schedule_hash")
    return str(value) if value not in (None, "") else None


def _cell_candidates(schedule: Mapping[str, Any], requested: Iterable[tuple[str, str]] | None
                     ) -> list[tuple[str, str | None, Mapping[str, Any], str]]:
    """Return deterministic symbol/bucket cells without inventing coverage."""
    symbols = schedule.get("symbols")
    if not isinstance(symbols, Mapping):
        return []
    if requested is not None:
        output = []
        for symbol, bucket in sorted({(str(s).strip().upper(), str(b))
                                      for s, b in requested}):
            entry = symbols.get(symbol)
            section = ((entry or {}).get("buckets") or {}).get(bucket) \
                if isinstance(entry, Mapping) else None
            origin = f"symbol_bucket:{symbol}:{bucket}"
            output.append((symbol, bucket, section if isinstance(section, Mapping) else {}, origin))
        return output
    output = []
    for symbol, entry in sorted(symbols.items(), key=lambda item: str(item[0])):
        symbol_name = str(symbol).strip().upper()
        if not isinstance(entry, Mapping):
            continue
        buckets = entry.get("buckets")
        if isinstance(buckets, Mapping) and buckets:
            for bucket, section in sorted(buckets.items(), key=lambda item: str(item[0])):
                output.append((symbol_name, str(bucket),
                               section if isinstance(section, Mapping) else {},
                               f"symbol_bucket:{symbol_name}:{bucket}"))
        else:
            # A symbol aggregate is still useful when no bucket survived the
            # coverage floor; the result is marked sparse below rather than
            # silently treating it as a valid bucket fit.
            output.append((symbol_name, None, entry, f"symbol:{symbol_name}"))
    return output


def _find_cell(schedule: Mapping[str, Any] | None, symbol: str,
               bucket: str | None) -> Mapping[str, Any] | None:
    if not isinstance(schedule, Mapping):
        return None
    entry = (schedule.get("symbols") or {}).get(symbol)
    if not isinstance(entry, Mapping):
        return None
    if bucket is not None:
        section = (entry.get("buckets") or {}).get(bucket)
        return section if isinstance(section, Mapping) else None
    return entry


def _one_way_cost(section: Mapping[str, Any], *, percentile: str,
                  depth_percentile: str, order_shares: float | None,
                  fee_bps: float, max_impact_half_spreads: float) -> tuple[float | None, dict[str, Any]]:
    spread = _number(_section_value(section, ("spread_bps", percentile)))
    depth = _number(_section_value(section, ("touch_shares", depth_percentile)))
    if spread is None or spread < 0:
        return None, {"spread_bps": None, "depth_shares": depth,
                      "impact_bps": None, "fee_bps": fee_bps}
    if depth is not None and depth <= 0:
        depth = None
    shares = _number(order_shares)
    impact = 0.0
    if shares is not None and shares > 0 and depth is not None:
        impact = (spread / 2.0) * min(
            max(0.0, shares / depth - 1.0), max_impact_half_spreads)
    total = spread / 2.0 + impact + fee_bps
    return total, {"spread_bps": spread, "depth_shares": depth,
                   "impact_bps": impact, "fee_bps": fee_bps,
                   "one_way_entry_cost_bps": total}


def _ladder(value: float | None) -> tuple[float | None, str | None]:
    if value is None or not math.isfinite(value) or value < 0:
        return None, "measured_cost_invalid"
    for scenario in COST_STRESS_SCENARIOS_BPS:
        if value <= scenario:
            return float(scenario), None
    return None, "measured_cost_exceeds_ladder_max"


def _fallback(*, symbol: str, bucket: str | None, origin: str,
              fit: Mapping[str, Any], fit_hash: str | None,
              validation_hash: str | None, percentile: str,
              reason: str, provider: str | None, feed: str | None,
              fallback_scenario_bps: float, max_cost_to_risk_ratio: float) -> dict[str, Any]:
    quote_count, session_count = _coverage(fit)
    return {
        "symbol": symbol, "bucket": bucket, "cell_origin": origin,
        "status": "fallback", "usable": False,
        "quote_count": quote_count, "session_count": session_count,
        "fit_sessions": _sessions(fit),
        "percentile": percentile, "provider": provider, "feed": feed,
        "fit_schedule_hash": fit_hash, "validation_schedule_hash": validation_hash,
        "fit_cost_bps": None, "validation_cost_bps": None,
        "selected_scenario_bps": float(fallback_scenario_bps),
        "fallback_reason": reason,
        "feasible_minimum_stop_bps": (
            float(fallback_scenario_bps) / max_cost_to_risk_ratio
            if max_cost_to_risk_ratio > 0 else None),
    }


def calibrate_stressed_cost(
        fit_schedule: Mapping[str, Any], *,
        validation_schedule: Mapping[str, Any] | None = None,
        percentile: str = "p95", depth_percentile: str = "p25",
        order_shares: float | None = None,
        fee_bps: float = DEFAULT_FEE_BPS,
        max_impact_half_spreads: float = 4.0,
        min_quotes_per_cell: int | None = None,
        min_sessions_per_cell: int = DEFAULT_MIN_SESSIONS_PER_CELL,
        expected_provider: str | None = None,
        expected_feed: str | None = None,
        validation_failure_reason: str | None = None,
        fallback_scenario_bps: float = DEFAULT_FALLBACK_SCENARIO_BPS,
        max_cost_to_risk_ratio: float = 0.30,
        validation_materiality_ratio: float = DEFAULT_VALIDATION_MATERIALITY_RATIO,
        cells: Iterable[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Calibrate one-way entry stress from fit and optional validation schedules.

    Fit cells are the only source of a narrower scenario.  If a validation
    cell is valid, its rung is ``max(fit_rung, validation_rung)``; it can never
    lower the fit result.  Any malformed provenance, sparse fit cell, or cost
    above 50 bps falls back to the configured 25 bps rung.
    """
    if not isinstance(fit_schedule, Mapping):
        raise StressCalibrationError("fit_schedule must be a mapping")
    _validate_schedule(fit_schedule, schema="quote-cost-schedule.v1")
    _validate_schedule(validation_schedule, schema="quote-cost-schedule.v1")
    if percentile not in {"p25", "median", "p75", "p90", "p95"}:
        raise StressCalibrationError(f"unsupported percentile {percentile!r}")
    if depth_percentile not in {"p25", "median", "p75", "p90", "p95"}:
        raise StressCalibrationError(f"unsupported depth percentile {depth_percentile!r}")
    min_quotes = _number(min_quotes_per_cell)
    if min_quotes is None:
        min_quotes = _number(_section_value(fit_schedule, ("measured", "min_quotes_per_cell"))) or 1
    if min_quotes < 1 or int(min_quotes) != min_quotes:
        raise StressCalibrationError("min_quotes_per_cell must be a positive integer")
    if isinstance(min_sessions_per_cell, bool) or int(min_sessions_per_cell) != min_sessions_per_cell or min_sessions_per_cell < 1:
        raise StressCalibrationError("min_sessions_per_cell must be a positive integer")
    fee = _number(fee_bps)
    cap = _number(max_impact_half_spreads)
    ratio = _number(max_cost_to_risk_ratio)
    fallback = _number(fallback_scenario_bps)
    materiality = _number(validation_materiality_ratio)
    if fee is None or fee < 0 or cap is None or cap < 0 or ratio is None or ratio <= 0:
        raise StressCalibrationError("fee, impact cap, and cost/risk ratio must be finite and non-negative (ratio > 0)")
    if fallback not in COST_STRESS_SCENARIOS_BPS:
        raise StressCalibrationError("fallback_scenario_bps must be on the preregistered ladder")
    if materiality is None or materiality < 1:
        raise StressCalibrationError("validation_materiality_ratio must be >= 1")

    fit_ok, fit_provider, fit_feed, fit_provenance_reason = _provenance(
        fit_schedule, expected_provider=expected_provider, expected_feed=expected_feed)
    validation_present = (validation_schedule is not None or
                          validation_failure_reason not in (None, ""))
    if validation_failure_reason not in (None, ""):
        validation_ok, validation_provider, validation_feed = False, None, None
        validation_provenance_reason = str(validation_failure_reason)
    else:
        validation_ok, validation_provider, validation_feed, validation_provenance_reason = _provenance(
            validation_schedule, expected_provider=expected_provider, expected_feed=expected_feed)
    fit_hash = _schedule_hash(fit_schedule)
    validation_hash = _schedule_hash(validation_schedule)
    cells_out: list[dict[str, Any]] = []
    for symbol, bucket, section, origin in _cell_candidates(fit_schedule, cells):
        fit_section = section
        # A requested bucket missing from the schedule is sparse by definition.
        quote_count, session_count = _coverage(fit_section)
        common = {
            "symbol": symbol, "bucket": bucket, "cell_origin": origin,
            "quote_count": quote_count, "session_count": session_count,
            "fit_sessions": _sessions(fit_section),
            "validation_quote_count": 0, "validation_session_count": 0,
            "validation_sessions": [],
            "effective_after_session": None,
            "percentile": percentile, "provider": fit_provider,
            "feed": fit_feed, "fit_schedule_hash": fit_hash,
            "validation_schedule_hash": validation_hash,
        }
        reason = None
        if not fit_ok:
            reason = fit_provenance_reason
        elif bucket is not None and not fit_section:
            reason = "sparse_cell_missing"
        elif bucket is None and _number(fit_section.get("sparse_buckets")):
            # ``measure_quote_costs`` intentionally omits sparse bucket
            # sections.  Do not replace those omitted cells with the symbol
            # aggregate: that would turn missing time-of-day evidence into a
            # deceptively precise stress selection.
            reason = "sparse_cell_coverage"
        elif quote_count < int(min_quotes) or session_count < int(min_sessions_per_cell):
            reason = "sparse_cell_coverage"
        elif validation_present and not validation_ok:
            reason = validation_provenance_reason
        fit_cost, fit_parts = _one_way_cost(
            fit_section, percentile=percentile, depth_percentile=depth_percentile,
            order_shares=order_shares, fee_bps=fee, max_impact_half_spreads=cap)
        if reason is None and fit_cost is None:
            reason = "fit_measurement_missing"
        fit_scenario, fit_ladder_reason = _ladder(fit_cost)
        if reason is None and fit_ladder_reason:
            reason = fit_ladder_reason
        if reason is not None:
            result = {**common, "status": "fallback", "usable": False,
                      "fit_cost_bps": fit_cost, "validation_cost_bps": None,
                      "selected_scenario_bps": float(fallback),
                      "fallback_reason": reason,
                      "feasible_minimum_stop_bps": float(fallback) / ratio,
                      **fit_parts}
            cells_out.append(result)
            continue

        validation_section = _find_cell(validation_schedule, symbol, bucket)
        validation_cost = None
        validation_scenario = None
        validation_parts: dict[str, Any] = {}
        validation_used = False
        validation_reason = None
        validation_quote_count = 0
        validation_session_count = 0
        validation_session_ids: list[str] = []
        if validation_present and validation_ok and validation_section is None:
            validation_reason = "validation_cell_missing"
        elif validation_present and validation_ok and validation_section is not None:
            v_count, v_sessions = _coverage(validation_section)
            validation_quote_count = v_count
            validation_session_count = v_sessions
            validation_session_ids = _sessions(validation_section)
            if v_count >= int(min_quotes) and v_sessions >= int(min_sessions_per_cell):
                validation_cost, validation_parts = _one_way_cost(
                    validation_section, percentile=percentile,
                    depth_percentile=depth_percentile, order_shares=order_shares,
                    fee_bps=fee, max_impact_half_spreads=cap)
                validation_scenario, _ = _ladder(validation_cost)
                if validation_scenario is None:
                    # A validation cost above the ladder is evidence for the
                    # fail-closed rung, not permission to narrow the fit.
                    validation_reason = "validation_cost_exceeds_ladder_max"
                    validation_scenario = float(fallback)
                validation_used = validation_cost is not None
            else:
                validation_reason = "validation_cell_coverage"
        selected = max(float(fit_scenario), float(validation_scenario or fit_scenario))
        materially_wider = bool(
            validation_cost is not None and fit_cost is not None and
            validation_cost > fit_cost * float(materiality))
        if materially_wider:
            # A validation shock this large is evidence that the fit cell is
            # not stable enough to select a narrower rung.  Retain the
            # measured values for diagnosis but fail closed to 25 bps (or the
            # already-wider fit rung) and mark the cell unusable.
            validation_reason = "validation_materially_exceeds_fit"
        if validation_reason is not None:
            cells_out.append({
                **common, "status": "fallback", "usable": False,
                "fit_cost_bps": fit_cost,
                "validation_cost_bps": validation_cost,
                "validation_used": validation_used,
                "validation_quote_count": validation_quote_count,
                "validation_session_count": validation_session_count,
                "validation_sessions": validation_session_ids,
                "effective_after_session": _latest_session(validation_session_ids),
                "validation_materially_exceeds_fit": materially_wider,
                "selected_scenario_bps": max(float(fit_scenario), float(fallback)),
                "fallback_reason": validation_reason,
                "feasible_minimum_stop_bps": (
                    max(float(fit_scenario), float(fallback)) / ratio),
                **fit_parts, **({f"validation_{key}": value
                                 for key, value in validation_parts.items()} if validation_parts else {}),
            })
            continue
        cells_out.append({
            **common, "status": "selected", "usable": True,
            "fit_cost_bps": fit_cost,
            "validation_cost_bps": validation_cost,
            "validation_used": validation_used,
            "validation_quote_count": validation_quote_count,
            "validation_session_count": validation_session_count,
            "validation_sessions": validation_session_ids,
            "effective_after_session": _latest_session(validation_session_ids),
            "validation_materially_exceeds_fit": materially_wider,
            "selected_scenario_bps": selected,
            "fallback_reason": None,
            "feasible_minimum_stop_bps": selected / ratio,
            **fit_parts, **({f"validation_{key}": value
                             for key, value in validation_parts.items()} if validation_parts else {}),
        })

    if not cells_out:
        cells_out.append(_fallback(
            symbol="*", bucket=None, origin="universe", fit=fit_schedule.get("universe") or {},
            fit_hash=fit_hash, validation_hash=validation_hash, percentile=percentile,
            reason=(fit_provenance_reason or "no_symbol_cells"), provider=fit_provider,
            feed=fit_feed, fallback_scenario_bps=float(fallback),
            max_cost_to_risk_ratio=ratio))
    aggregate = max(float(item.get("selected_scenario_bps") or fallback) for item in cells_out)
    fit_quote_count, fit_session_count = _coverage(
        fit_schedule.get("universe") or {})
    validation_quote_count, validation_session_count = _coverage(
        (validation_schedule or {}).get("universe") or {})
    effective_after_session = _latest_session(
        session for cell in cells_out
        for session in _session_values(cell.get("validation_sessions")))
    body = {
        "schema": STRESS_CALIBRATION_SCHEMA,
        "diagnostic_only": True, "authorizing": False,
        "fit_schedule_hash": fit_hash, "validation_schedule_hash": validation_hash,
        "validation_failure_reason": validation_failure_reason,
        "fit_quote_count": fit_quote_count, "fit_session_count": fit_session_count,
        "validation_quote_count": validation_quote_count,
        "validation_session_count": validation_session_count,
        "effective_after_session": effective_after_session,
        "provider": fit_provider, "feed": fit_feed,
        "percentile": percentile, "depth_percentile": depth_percentile,
        "min_quotes_per_cell": int(min_quotes),
        "min_sessions_per_cell": int(min_sessions_per_cell),
        "max_cost_to_risk_ratio": ratio,
        "fallback_scenario_bps": float(fallback),
        "aggregate_conservative_scenario_bps": aggregate,
        "aggregate_feasible_minimum_stop_bps": aggregate / ratio,
        "cells": cells_out,
    }
    body["content_hash"] = content_hash(body)
    return body


# Descriptive aliases keep the artifact easy to discover for callers that use
# either "stress" or "calibration" in their vocabulary.
calibrate_stress_schedule = calibrate_stressed_cost
empirical_stress_calibration = calibrate_stressed_cost
resolve_stressed_cost = resolve_stress_scenario
verify_stress_calibration = verify_stress_calibration_artifact


__all__ = [
    "DEFAULT_FALLBACK_SCENARIO_BPS", "DEFAULT_MIN_SESSIONS_PER_CELL",
    "STRESS_CALIBRATION_SCHEMA", "StressCalibrationError",
    "activation_overlay", "load_stress_calibration_artifact",
    "resolve_stress_scenario", "verify_stress_calibration_artifact",
    "resolve_stressed_cost", "verify_stress_calibration",
    "calibrate_stress_schedule", "calibrate_stressed_cost",
    "empirical_stress_calibration",
]
