"""Frozen, non-authorizing stressed-cost policy counterfactual.

The experiment evaluates one preregistered set of rule specifications twice on
the same diagnostic corpus.  Only ``max_stressed_cost_to_risk_ratio`` changes.
It is an engineering reachability test, not a promotion gate and not a way to
choose a threshold from held-out performance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.config import load_config
from agent.contracts.rule import rule_variant_id, validate_rule_spec

from .costs import (CostModel, ReplayPolicy, SQLiteQuoteIndex,
                    diagnostic_backfill_policy)
from .edge_lab import _read_discovery_rows, content_hash
from .factory_core import simulate_account
from .gates import (
    PROTOCOL_BACKTEST_MIN_CLUSTERS, PROTOCOL_BACKTEST_MIN_SESSIONS,
    PROTOCOL_BACKTEST_MIN_TRADES, PROTOCOL_QUALIFICATION_MIN_CLUSTERS,
    PROTOCOL_QUALIFICATION_MIN_SESSIONS, PROTOCOL_QUALIFICATION_MIN_TRADES,
    PROTOCOL_SHADOW_MIN_CLUSTERS, PROTOCOL_SHADOW_MIN_SESSIONS,
    PROTOCOL_SHADOW_MIN_TRADES, RETIREMENT_MIN_USEFUL_R,
)
from .stats import (clustered_mde_power_report,
                    moving_block_cluster_bootstrap_lower_bound)


SCHEMA = "stressed-cost-ratio-counterfactual.v2"
LEGACY_SCHEMA = "stressed-cost-ratio-counterfactual.v1"
CHANGED_FIELD = "risk.max_stressed_cost_to_risk_ratio"
TERMINAL_DISPOSITIONS = frozenset({"executed", "refused", "no_signal"})
PAIRING_NUMERIC_FIELDS = (
    "quantity", "entry_price", "exit_price", "gross_pnl", "costs", "net_pnl",
    "risk_usd", "nominal_risk_usd", "realized_risk_usd", "r_multiple",
    "return_value",
)
DEFAULT_COUNTERFACTUAL_DRAWS = 4_000
DEFAULT_COUNTERFACTUAL_MIN_CLUSTERS = PROTOCOL_BACKTEST_MIN_CLUSTERS
DEFAULT_COUNTERFACTUAL_BLOCK_LENGTH = 5
_CODE_BUNDLE_ROOTS = ("agent", "research")
_CODE_BUNDLE_EXTRAS = ("requirements.lock.txt",)


def _finite(value: object) -> float | None:
    if isinstance(value, (bool, str, bytes, bytearray)) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    """Return deterministic empirical quantiles and sample dispersion."""
    clean = sorted(value for item in values
                   if (value := _finite(item)) is not None)
    if not clean:
        return {
            "count": 0, "min": None, "p25": None, "median": None,
            "p75": None, "max": None, "mean": None,
            "sample_standard_deviation": None,
            "sample_sigma": None,
        }

    def percentile(fraction: float) -> float:
        index = (len(clean) - 1) * fraction
        lower, upper = math.floor(index), math.ceil(index)
        if lower == upper:
            return clean[lower]
        return clean[lower] + (clean[upper] - clean[lower]) * (index - lower)

    mean = sum(clean) / len(clean)
    variance = (sum((value - mean) ** 2 for value in clean) /
                (len(clean) - 1) if len(clean) > 1 else None)
    return {
        "count": len(clean), "min": clean[0], "p25": percentile(.25),
        "median": percentile(.5), "p75": percentile(.75), "max": clean[-1],
        "mean": mean,
        "sample_standard_deviation": (math.sqrt(variance)
                                      if variance is not None else None),
        "sample_sigma": (math.sqrt(variance) if variance is not None else None),
    }


def _numeric_summary(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    values: list[float] = []
    missing = invalid = 0
    for row in rows:
        raw = row.get(key)
        if raw is None:
            missing += 1
            continue
        value = _finite(raw)
        if value is None:
            invalid += 1
            continue
        values.append(value)
    return {**_distribution(values), "missing": missing, "invalid": invalid}


def _entry_slippage_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    missing = invalid = 0
    for row in rows:
        telemetry = row.get("entry_slippage")
        if telemetry is None:
            missing += 1
            continue
        if not isinstance(telemetry, Mapping):
            invalid += 1
            continue
        raw = telemetry.get("slippage_bps", telemetry.get("adverse_bps"))
        if raw is None:
            missing += 1
            continue
        value = _finite(raw)
        if value is None:
            invalid += 1
            continue
        values.append(value)
    return {**_distribution(values), "missing": missing, "invalid": invalid,
            "unit": "basis_points"}


def _safe_json(value: Any) -> Any:
    """Return a bounded JSON-safe representation for persisted evidence.

    Counterfactual rows normally contain only primitives, but test fixtures and
    adapters can supply ``Decimal``, timestamps, mappings, or non-finite
    numbers.  The evidence projection must never make the complete report
    unserialisable.  Invalid/non-finite scalar numbers intentionally become
    ``None``; the cost decomposition carries the corresponding availability
    reason explicitly.
    """
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _safe_json(item) for key, item in sorted(
            value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    number = _finite(value)
    return number if number is not None else str(value)


def _number_field(row: Mapping[str, Any], *names: str) -> tuple[float | None, str]:
    """Read the first finite numeric alias and retain missing/invalid state."""
    present = False
    invalid = False
    for name in names:
        if name not in row or row.get(name) is None:
            continue
        present = True
        value = _finite(row.get(name))
        if value is not None:
            return value, "available"
        invalid = True
    if invalid or present:
        return None, "invalid"
    return None, "missing"


def _named_number_field(
        row: Mapping[str, Any], *names: str) -> tuple[float | None, str, str | None]:
    """Return a numeric alias together with the field that supplied it."""
    saw_invalid: str | None = None
    for name in names:
        if name not in row or row.get(name) is None:
            continue
        value = _finite(row.get(name))
        if value is not None:
            return value, "available", name
        if saw_invalid is None:
            saw_invalid = name
    if saw_invalid is not None:
        return None, "invalid", saw_invalid
    return None, "missing", None


def _measurement(value: float | None, *, unit: str,
                 reason: str | None = None) -> dict[str, Any]:
    finite = _finite(value)
    return {
        "value": finite,
        "unit": unit,
        "available": finite is not None,
        "reason": None if finite is not None else (reason or "unavailable"),
    }


def _cost_decomposition(row: Mapping[str, Any]) -> dict[str, Any]:
    """Derive descriptive model-cost components from one terminal row.

    This deliberately does not replay or infer a missing leg.  A component is
    available only when the row has finite references, quantity, direction,
    multiplier, and economics sufficient to calculate it.
    """
    entry_reference, entry_status = _number_field(row, "entry_reference")
    exit_reference, exit_status = _number_field(row, "exit_reference")
    quantity, quantity_status = _number_field(row, "quantity", "contracts")
    multiplier, multiplier_status = _number_field(
        row, "contract_multiplier", "multiplier")
    direction_raw = row.get("direction")
    direction = (str(direction_raw).strip().lower()
                 if direction_raw is not None else "")
    direction_status = ("available" if direction in {"long", "short"}
                        else "invalid" if direction else "missing")
    vehicle = str(row.get("vehicle") or "").strip().lower()
    execution_direction = "long" if vehicle == "option" else direction
    gross, gross_status = _number_field(row, "gross_pnl")
    fee_cost, fee_status = _number_field(row, "fee_cost", "fees", "costs")
    risk, risk_status = _number_field(
        row, "risk_usd", "realized_risk_usd", "nominal_risk_usd")
    if risk_status == "available" and (risk is None or risk <= 0):
        risk = None
        risk_status = "invalid"

    reference_reason = None
    reference_gross: float | None = None
    if entry_status != "available":
        reference_reason = f"{entry_status}_entry_reference"
    elif exit_status != "available":
        reference_reason = f"{exit_status}_exit_reference"
    elif entry_reference <= 0:
        reference_reason = "invalid_entry_reference"
    elif exit_reference <= 0:
        reference_reason = "invalid_exit_reference"
    elif quantity_status != "available":
        reference_reason = f"{quantity_status}_quantity"
    elif quantity <= 0:
        reference_reason = "invalid_quantity"
    elif multiplier_status != "available":
        reference_reason = f"{multiplier_status}_multiplier"
    elif multiplier <= 0:
        reference_reason = "invalid_multiplier"
    elif direction_status != "available":
        reference_reason = f"{direction_status}_direction"
    else:
        reference_gross = ((exit_reference - entry_reference)
                           if execution_direction == "long" else
                           (entry_reference - exit_reference)) * quantity * multiplier

    reference = _measurement(reference_gross, unit="currency",
                             reason=reference_reason)
    drag: float | None = None
    drag_reason = None
    actual_gross = gross
    actual_gross_status = gross_status
    if gross_status == "missing":
        actual_entry, actual_entry_status = _number_field(row, "entry_price")
        actual_exit, actual_exit_status = _number_field(row, "exit_price")
        if (actual_entry_status == "available" and
                actual_exit_status == "available" and
                quantity_status == "available" and
                multiplier_status == "available" and
                direction_status == "available"):
            actual_gross = ((actual_exit - actual_entry)
                            if execution_direction == "long" else
                            (actual_entry - actual_exit)) * quantity * multiplier
            actual_gross_status = "available"
    if reference_gross is None:
        drag_reason = "reference_gross_unavailable"
    elif actual_gross_status != "available":
        drag_reason = f"{actual_gross_status}_gross_pnl_or_fill_prices"
    else:
        drag = reference_gross - actual_gross
    execution_drag = {
        "currency": _measurement(drag, unit="currency", reason=drag_reason),
        "r": _measurement(
            drag / risk if drag is not None and risk is not None else None,
            unit="R",
            reason=(None if drag is not None and risk is not None else
                    "missing_or_invalid_risk_usd" if risk_status != "available"
                    else "execution_drag_unavailable")),
    }

    fee_valid = fee_status == "available" and fee_cost >= 0
    fee = _measurement(fee_cost if fee_valid else None, unit="currency",
                       reason=(None if fee_valid else
                               "invalid_fee_cost" if fee_status == "available"
                               else f"{fee_status}_fee_cost"))
    fee_r = _measurement(
        fee_cost / risk if fee_valid and risk is not None else None,
        unit="R",
        reason=(None if fee_valid and risk is not None else
                "missing_or_invalid_risk_usd" if risk_status != "available"
                else "fee_cost_unavailable"))
    fee_cost_component = {"currency": fee, "r": fee_r}

    total = (drag + fee_cost if drag is not None and fee_valid
             else None)
    total_reason = (None if total is not None else
                    "execution_drag_unavailable" if drag is None else
                    "fee_cost_unavailable")
    total_component = {
        "currency": _measurement(total, unit="currency", reason=total_reason),
        "r": _measurement(
            total / risk if total is not None and risk is not None else None,
            unit="R",
            reason=(None if total is not None and risk is not None else
                    "missing_or_invalid_risk_usd" if risk_status != "available"
                    else "total_modeled_drag_unavailable")),
    }

    stop_distance, stop_status = _number_field(row, "stop_distance")
    stop_price, stop_price_status = _number_field(row, "stop_price")
    if vehicle == "option":
        stop_basis, stop_basis_status, resolved_stop_basis = _named_number_field(
            row, "underlying_entry", "plan_entry")
        stop_basis_field = resolved_stop_basis or "underlying_entry_or_plan_entry"
    else:
        stop_basis, stop_basis_status = entry_reference, entry_status
        stop_basis_field = "entry_reference"
    stop_bps: float | None = None
    stop_reason = None
    if stop_basis_status != "available" or stop_basis <= 0:
        stop_reason = (f"{stop_basis_status}_{stop_basis_field}"
                       if stop_basis_status != "available" else
                       f"invalid_{stop_basis_field}")
    elif stop_status == "available":
        stop_bps = abs(stop_distance) / abs(stop_basis) * 10_000.0
    elif stop_status == "invalid":
        stop_reason = "invalid_stop_distance"
    elif stop_price_status == "available":
        stop_bps = abs(stop_price - stop_basis) / abs(stop_basis) * 10_000.0
    else:
        stop_reason = ("invalid_stop_price" if stop_price_status == "invalid"
                       else "missing_stop_distance")

    return {
        "reference_gross": reference,
        "execution_drag": execution_drag,
        "fee_cost": fee_cost_component,
        "total_modeled_drag": total_component,
        "stop_distance_basis": {
            "field": stop_basis_field,
            **_measurement(
                stop_basis, unit="price",
                reason=(None if stop_basis_status == "available" else
                        f"{stop_basis_status}_{stop_basis_field}")),
        },
        "stop_distance_bps": _measurement(
            stop_bps, unit="basis_points", reason=stop_reason),
    }


def _derived_distribution(rows: Sequence[Mapping[str, Any]], *, component: str,
                          unit: str, dimension: str = "value") -> dict[str, Any]:
    values: list[float] = []
    missing = invalid = 0
    for row in rows:
        decomposition = _cost_decomposition(row)
        current: Any = decomposition.get(component)
        if dimension != "value":
            current = current.get(dimension) if isinstance(current, Mapping) else None
        if not isinstance(current, Mapping):
            missing += 1
            continue
        value = _finite(current.get("value"))
        if value is not None:
            values.append(value)
        elif (str(current.get("reason") or "").startswith("invalid") or
              "invalid" in str(current.get("reason") or "")):
            invalid += 1
        else:
            missing += 1
    return {**_distribution(values), "missing": missing, "invalid": invalid,
            "unit": unit}


def _modeled_cost_distributions(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "reference_gross": _derived_distribution(
            rows, component="reference_gross", unit="currency"),
        "execution_drag": {
            "currency": _derived_distribution(
                rows, component="execution_drag", dimension="currency",
                unit="currency"),
            "r": _derived_distribution(
                rows, component="execution_drag", dimension="r", unit="R"),
        },
        "fee_cost": {
            "currency": _derived_distribution(
                rows, component="fee_cost", dimension="currency",
                unit="currency"),
            "r": _derived_distribution(
                rows, component="fee_cost", dimension="r", unit="R"),
        },
        "total_modeled_drag": {
            "currency": _derived_distribution(
                rows, component="total_modeled_drag", dimension="currency",
                unit="currency"),
            "r": _derived_distribution(
                rows, component="total_modeled_drag", dimension="r", unit="R"),
        },
    }


def _opportunity_evidence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a deterministic, tamper-evident projection of signal outcomes."""
    projected: list[dict[str, Any]] = []
    for row in rows:
        disposition = str(row.get("execution_disposition") or "").strip()
        if disposition not in {"executed", "refused"} or \
                row.get("signal_opportunity") is not True:
            continue

        def text(name: str) -> str | None:
            value = row.get(name)
            return None if value is None else str(value)

        def number(name: str, *aliases: str) -> float | None:
            value, _status = _number_field(row, name, *aliases)
            return value

        record: dict[str, Any] = {
            "identity": {
                "variant_id": text("_counterfactual_variant_id"),
                "opportunity_id": text("opportunity_id"),
                "session_date": text("session_date"),
                "symbol": text("symbol"),
                "vehicle": text("vehicle"),
                "direction": text("direction"),
                "contract": text("contract"),
            },
            "terminal": {
                "execution_disposition": disposition,
                "signal_opportunity": True,
                "no_trade": row.get("no_trade")
                if isinstance(row.get("no_trade"), bool) else None,
                "reject_stage": text("reject_stage"),
                "reject_reason": text("reject_reason"),
            },
            "levels": {
                "entry_reference": number("entry_reference"),
                "exit_reference": number("exit_reference"),
                "entry_price": number("entry_price"),
                "exit_price": number("exit_price"),
                "plan_entry": number("plan_entry"),
                "underlying_entry": number("underlying_entry"),
                "stop_price": number("stop_price"),
                "initial_stop_price": number("initial_stop_price"),
                "active_stop_price": number("active_stop_price"),
                "stop_distance": number("stop_distance"),
                "target_price": number("target_price"),
                "breakeven_r": number("breakeven_r"),
                "breakeven_armed_at": text("breakeven_armed_at"),
                "breakeven_armed_epoch": number("breakeven_armed_epoch"),
                "exit_reason": text("exit_reason"),
                "tie_broken": row.get("tie_broken")
                if isinstance(row.get("tie_broken"), bool) else None,
                "entry_gap_fill": row.get("entry_gap_fill")
                if isinstance(row.get("entry_gap_fill"), bool) else None,
                "exit_gap_fill": row.get("exit_gap_fill")
                if isinstance(row.get("exit_gap_fill"), bool) else None,
                "entry_timestamp": text("entry_timestamp"),
                "exit_timestamp": text("exit_timestamp"),
            },
            "fill_provenance": {
                "entry": {
                    "source": text("entry_fill_source"),
                    "feed": text("entry_feed"),
                    "provider": text("entry_provider"),
                    "quote_age_seconds": number("entry_quote_age_seconds"),
                },
                "exit": {
                    "source": text("exit_fill_source"),
                    "feed": text("exit_feed"),
                    "provider": text("exit_provider"),
                    "quote_age_seconds": number("exit_quote_age_seconds"),
                },
                "signal_bar": {
                    "feed": text("signal_bar_feed"),
                    "provider": text("signal_bar_provider"),
                },
                "entry_bar": {
                    "feed": text("entry_bar_feed"),
                    "provider": text("entry_bar_provider"),
                },
                "exit_bar": {
                    "feed": text("exit_bar_feed"),
                    "provider": text("exit_bar_provider"),
                },
                "evidence_mode": text("evidence_mode"),
            },
            "economics": {
                "quantity": number("quantity", "contracts"),
                "contract_multiplier": number("contract_multiplier", "multiplier"),
                "gross_pnl": number("gross_pnl"),
                "fee_cost": number("fee_cost", "fees", "costs"),
                "costs": number("costs"),
                "net_pnl": number("net_pnl"),
                "risk_usd": number("risk_usd"),
                "nominal_risk_usd": number("nominal_risk_usd"),
                "realized_risk_usd": number("realized_risk_usd"),
                "risk_per_unit": number("risk_per_unit"),
                "realized_risk_per_unit": number("realized_risk_per_unit"),
                "risk_budget": number("risk_budget"),
                "entry_notional": number("entry_notional"),
                "r_multiple": number("r_multiple"),
                "return_value": number("return_value"),
            },
            "cost_decomposition": _cost_decomposition(row),
            "terminal_validation": _terminal_error(row),
        }
        safe = _safe_json(record)
        projected.append(safe)

    projected.sort(key=lambda item: (
        str(item["identity"].get("variant_id") or ""),
        str(item["identity"].get("opportunity_id") or ""),
        str(item["identity"].get("session_date") or ""),
        content_hash(item),
    ))
    rows_with_hash: list[dict[str, Any]] = []
    for item in projected:
        row_hash = content_hash(item)
        rows_with_hash.append({**item, "row_hash": row_hash})
    rows_with_hash = _safe_json(rows_with_hash)
    manifest = {
        "schema": "counterfactual-opportunity-evidence.v1",
        "diagnostic_only": True,
        "authorizing": False,
        "terminal_dispositions": ["executed", "refused"],
        "excluded_dispositions": ["no_signal"],
        "count": len(rows_with_hash),
        "collection_hash": content_hash(rows_with_hash),
    }
    return {
        "schema": "counterfactual-opportunity-evidence.v1",
        "diagnostic_only": True,
        "authorizing": False,
        "terminal_dispositions": ["executed", "refused"],
        "excluded_dispositions": ["no_signal"],
        "rows": rows_with_hash,
        "count": len(rows_with_hash),
        "collection_hash": manifest["collection_hash"],
        "manifest_hash": content_hash(manifest),
    }


def _counterfactual_scope() -> dict[str, Any]:
    return {
        "schema": "counterfactual-scope.v1",
        "diagnostic_only": True,
        "authorizing": False,
        "stateful": True,
        "stateful_replay": True,
        "path_dependent": True,
        "same_corpus_two_arm_replay": True,
        "per_opportunity_isolated_replay_available": False,
        "randomized_null_available": False,
        "per_opportunity_isolated_replay": {
            "available": False,
            "reason": "deferred; replay state is shared across the opportunity path",
        },
        "randomized_null": {
            "available": False,
            "reason": "deferred; this report does not randomize opportunity assignment",
        },
        "limitation": (
            "arm outcomes are stateful and path-dependent; evidence is descriptive "
            "and cannot establish an isolated per-opportunity causal effect"),
    }


def _terminal_error(row: Mapping[str, Any]) -> str | None:
    disposition = str(row.get("execution_disposition") or "").strip()
    if disposition not in TERMINAL_DISPOSITIONS:
        return "invalid_terminal_disposition"
    no_trade = row.get("no_trade")
    if disposition == "executed" and no_trade is not False:
        return "executed_no_trade_mismatch"
    if disposition == "executed" and row.get("signal_opportunity") is not True:
        return "executed_signal_opportunity_mismatch"
    if disposition != "executed" and no_trade is not True:
        return "refusal_no_signal_trade_mismatch"
    if disposition == "refused" and not str(row.get("reject_reason") or "").strip():
        return "refusal_missing_reason"
    reason = str(row.get("reject_reason") or "").strip()
    stage = str(row.get("reject_stage") or "").strip()
    if (reason == "stressed_cost_risk_limit" or stage == "cost_stress") and \
            disposition != "refused":
        return "cost_gate_disposition_mismatch"
    if (reason == "stressed_cost_risk_limit" or stage == "cost_stress") and \
            row.get("signal_opportunity") is not True:
        return "cost_gate_signal_opportunity_mismatch"
    if reason == "stressed_cost_risk_limit" and stage != "cost_stress":
        return "cost_gate_stage_mismatch"
    if stage == "cost_stress" and reason != "stressed_cost_risk_limit":
        return "cost_stage_reason_mismatch"
    if (disposition == "no_signal" and
            row.get("signal_opportunity") is not False):
        return "no_signal_opportunity_mismatch"
    return None


def _changed_paths(left: Any, right: Any, prefix: str = "") -> list[str]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        changed: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                changed.append(path)
            else:
                changed.extend(_changed_paths(left[key], right[key], path))
        return changed
    return [] if left == right else [prefix or "$root"]


def _code_bundle_files() -> tuple[str, ...]:
    root = Path(__file__).resolve().parent.parent
    files = {
        path.relative_to(root).as_posix()
        for name in _CODE_BUNDLE_ROOTS
        for path in (root / name).rglob("*.py")
        if path.is_file()
    }
    files.update(name for name in _CODE_BUNDLE_EXTRAS
                 if (root / name).is_file())
    return tuple(sorted(files))


def _code_bundle_hash(files: Sequence[str]) -> str:
    root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for name in files:
        digest.update(name.encode("utf-8") + b"\0")
        path = root / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(f"missing:{name}".encode("utf-8"))
    return digest.hexdigest()


def _funnel_manifest() -> dict[str, Any]:
    unavailable = "whole_corpus_counterfactual_has_no_sealed_or_live_window_assignments"
    windows = (
        ("fit", PROTOCOL_BACKTEST_MIN_TRADES,
         PROTOCOL_BACKTEST_MIN_SESSIONS, PROTOCOL_BACKTEST_MIN_CLUSTERS),
        ("heldout", PROTOCOL_BACKTEST_MIN_TRADES,
         PROTOCOL_BACKTEST_MIN_SESSIONS, PROTOCOL_BACKTEST_MIN_CLUSTERS),
        ("qualification", PROTOCOL_QUALIFICATION_MIN_TRADES,
         PROTOCOL_QUALIFICATION_MIN_SESSIONS,
         PROTOCOL_QUALIFICATION_MIN_CLUSTERS),
        ("shadow_selection", PROTOCOL_SHADOW_MIN_TRADES,
         PROTOCOL_SHADOW_MIN_SESSIONS, PROTOCOL_SHADOW_MIN_CLUSTERS),
        ("shadow_confirmation", PROTOCOL_SHADOW_MIN_TRADES,
         PROTOCOL_SHADOW_MIN_SESSIONS, PROTOCOL_SHADOW_MIN_CLUSTERS),
    )
    records = [{
        "window": name,
        "minimums": {"trades": trades, "sessions": sessions,
                     "clusters": clusters},
        "measurement_available": False,
        "observed": None,
        "reason": unavailable,
    } for name, trades, sessions, clusters in windows]
    qualification_fraction = .20
    heldout_fraction = .80 * .30
    offline_sessions = max(
        math.ceil(PROTOCOL_BACKTEST_MIN_SESSIONS / heldout_fraction),
        math.ceil(PROTOCOL_QUALIFICATION_MIN_SESSIONS /
                  qualification_fraction),
    )
    shadow_sessions = PROTOCOL_SHADOW_MIN_SESSIONS * 2
    return {
        "schema": "research-evidence-funnel.v1",
        "diagnostic_only": True,
        "authorizing": False,
        "requested_windows": len(records),
        "actual_window_measurements_available": False,
        "reason": unavailable,
        "windows": records,
        "nominal_trade_floor_sum": sum(item[1] for item in windows),
        "readiness_context": {
            "heldout_fraction": heldout_fraction,
            "qualification_fraction": qualification_fraction,
            "offline_required_sessions": offline_sessions,
            "shadow_required_sessions": shadow_sessions,
            "total_required_sessions": offline_sessions + shadow_sessions,
            "diagnostic_only": True,
            "authorizing": False,
        },
    }


def load_frozen_specs(source: str | Path | Mapping | Sequence) -> list[dict]:
    """Load and content-deduplicate specs from a diagnostic report or list."""
    if isinstance(source, (str, Path)):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        payload = source
    candidates: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        reports = payload.get("reports")
        if isinstance(reports, Sequence) and not isinstance(reports, (str, bytes)):
            for report in reports:
                if not isinstance(report, Mapping):
                    continue
                variants = report.get("variants")
                if isinstance(variants, Sequence) and not isinstance(
                        variants, (str, bytes)):
                    candidates.extend(
                        item["rule_spec"] for item in variants
                        if isinstance(item, Mapping) and
                        isinstance(item.get("rule_spec"), Mapping))
                elif isinstance(report.get("rule_spec"), Mapping):
                    candidates.append(report["rule_spec"])
        elif isinstance(payload.get("rule_specs"), Sequence):
            candidates.extend(item for item in payload["rule_specs"]
                              if isinstance(item, Mapping))
        elif isinstance(payload.get("rule_spec"), Mapping):
            candidates.append(payload["rule_spec"])
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            if isinstance(item, Mapping) and isinstance(item.get("rule_spec"), Mapping):
                candidates.append(item["rule_spec"])
            elif isinstance(item, Mapping):
                candidates.append(item)
    if not candidates:
        raise ValueError("counterfactual requires at least one frozen rule spec")
    unique = {}
    for candidate in candidates:
        spec = validate_rule_spec(candidate)
        unique[rule_variant_id(spec)] = spec
    return [unique[key] for key in sorted(unique)]


def _source_report_binding(
        source: str | Path, frozen_specs: Sequence[Mapping[str, Any]],
        ) -> tuple[str, dict[str, Any]]:
    """Recompute a source report binding from disk and verify its cohort."""
    payload = json.loads(Path(source).read_text(encoding="utf-8"))
    bound_specs = load_frozen_specs(payload)
    if content_hash(bound_specs) != content_hash(frozen_specs):
        raise ValueError("source report frozen cohort does not match specs")
    identity: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        for key in ("schema", "dataset_hash", "diagnostic_only", "authorizing"):
            if key in payload:
                identity[key] = payload[key]
        proofs = payload.get("proofs")
        if isinstance(proofs, Sequence) and not isinstance(proofs, (str, bytes)):
            identity["proof_count"] = len(proofs)
    return content_hash(payload), identity


def _ratio(value: object, label: str) -> float:
    if isinstance(value, (bool, str, bytes, bytearray)):
        raise ValueError(f"{label} must be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return parsed


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _summarize(rows: Sequence[Mapping[str, Any]], *,
               observation_unit: str = "trade") -> dict[str, Any]:
    dispositions = Counter()
    reasons = Counter()
    malformed = Counter()
    signal_opportunities = 0
    gross_pnl = 0.0
    net_pnl = 0.0
    returns: list[float] = []
    gross_values: list[float] = []
    valid_rows: list[Mapping[str, Any]] = []
    keys = Counter(
        (str(row.get("_counterfactual_variant_id") or "").strip(),
         str(row.get("opportunity_id") or "").strip())
        for row in rows
        if str(row.get("_counterfactual_variant_id") or "").strip() and
        str(row.get("opportunity_id") or "").strip())
    duplicate_keys = {key for key, count in keys.items() if count > 1}
    for row in rows:
        disposition = str(row.get("execution_disposition") or "unclassified")
        dispositions[disposition] += 1
        variant_id = str(row.get("_counterfactual_variant_id") or "").strip()
        opportunity_id = str(row.get("opportunity_id") or "").strip()
        if not variant_id:
            malformed["missing_variant_id"] += 1
            continue
        if not opportunity_id:
            malformed["missing_opportunity_id"] += 1
            continue
        row_key = (variant_id, opportunity_id)
        if row_key in duplicate_keys:
            malformed["duplicate_opportunity_key"] += 1
            continue
        error = _terminal_error(row)
        if error is not None:
            malformed[error] += 1
            continue
        valid_rows.append(row)
        if row.get("signal_opportunity") is True:
            signal_opportunities += 1
        reason = str(row.get("reject_reason") or "").strip()
        if reason:
            reasons[reason] += 1
    executed = [row for row in valid_rows
                if row.get("execution_disposition") == "executed"]
    for row in executed:
        pnl = _finite(row.get("gross_pnl"))
        if pnl is not None:
            gross_pnl += pnl
            gross_values.append(pnl)
        pnl = _finite(row.get("net_pnl"))
        if pnl is not None:
            net_pnl += pnl
        value = _finite(row.get("return_value"))
        if value is not None:
            returns.append(value)

    sessions = sorted({str(row.get("session_date") or "").strip()
                       for row in valid_rows
                       if str(row.get("session_date") or "").strip()})
    trades_by_session = Counter({session: 0 for session in sessions})
    missing_trade_session = 0
    for row in executed:
        session = str(row.get("session_date") or "").strip()
        if session:
            trades_by_session[session] += 1
        else:
            missing_trade_session += 1

    target_defined = sum(_finite(row.get("target_price")) is not None
                         for row in executed)
    target_reached = sum(str(row.get("exit_reason") or "") == "target"
                         for row in executed)
    target_reached_with_definition = sum(
        str(row.get("exit_reason") or "") == "target" and
        _finite(row.get("target_price")) is not None for row in executed)
    entry_sources = Counter(str(row.get("entry_fill_source") or "").strip()
                            for row in executed
                            if str(row.get("entry_fill_source") or "").strip())
    exit_sources = Counter(str(row.get("exit_fill_source") or "").strip()
                           for row in executed
                           if str(row.get("exit_fill_source") or "").strip())
    exit_reasons = Counter(str(row.get("exit_reason") or "").strip()
                           for row in executed
                           if str(row.get("exit_reason") or "").strip())
    opportunity_rows = [row for row in valid_rows
                        if row.get("signal_opportunity") is True and
                        row.get("execution_disposition") in {"executed", "refused"}]
    stressed = int(reasons.get("stressed_cost_risk_limit", 0))
    denominator = signal_opportunities
    return {
        "rows": len(rows),
        "valid_terminal_rows": len(valid_rows),
        "malformed_rows": sum(malformed.values()),
        "malformed_reasons": dict(sorted(malformed.items())),
        "duplicate_opportunity_keys": len(duplicate_keys),
        "observation_unit": observation_unit,
        "signal_opportunities": signal_opportunities,
        "dispositions": dict(sorted(dispositions.items())),
        "reject_reasons": dict(sorted(reasons.items())),
        "stressed_cost_risk_limit": stressed,
        "stressed_cost_rejection_rate": (
            stressed / denominator if denominator else None),
        "stressed_cost_admissibility_rate": (
            1.0 - stressed / denominator if denominator else None),
        "trades": len(executed),
        "execution_admission_rate": (len(executed) / denominator
                                     if denominator else None),
        "gross_pnl": gross_pnl,
        "gross_pnl_measurement": {**_distribution(gross_values),
                                   "missing": sum(
                                       row.get("gross_pnl") is None
                                       for row in executed),
                                   "invalid": sum(
                                       row.get("gross_pnl") is not None and
                                       _finite(row.get("gross_pnl")) is None
                                       for row in executed)},
        "net_pnl": net_pnl,
        "mean_return_value": (sum(returns) / len(returns) if returns else None),
        "net_pnl_measurement": _numeric_summary(executed, "net_pnl"),
        "return_value": _numeric_summary(executed, "return_value"),
        "r_multiple": _numeric_summary(executed, "r_multiple"),
        "costs": {**_numeric_summary(executed, "costs"), "unit": "currency"},
        "stressed_cost_to_risk_ratio": _numeric_summary(
            valid_rows, "stressed_cost_to_risk_ratio"),
        "entry_slippage": _entry_slippage_summary(valid_rows),
        "fill_sources": {
            "entry": dict(sorted(entry_sources.items())),
            "exit": dict(sorted(exit_sources.items())),
        },
        "exit_reasons": dict(sorted(exit_reasons.items())),
        "stop_bps": _derived_distribution(
            opportunity_rows, component="stop_distance_bps", unit="basis_points"),
        "modeled_costs": _modeled_cost_distributions(opportunity_rows),
        "target_reach": {
            "executed_trades": len(executed),
            "target_defined_trades": target_defined,
            "target_reached_trades": target_reached,
            "target_reached_with_definition": target_reached_with_definition,
            "rate_of_executed": (target_reached / len(executed)
                                 if executed else None),
            "rate_of_target_defined": (
                target_reached_with_definition / target_defined
                if target_defined else None),
        },
        "trades_per_session": {
            **_distribution(list(trades_by_session.values())),
            "observed_sessions": len(sessions),
            "sessions_with_trades": sum(value > 0
                                         for value in trades_by_session.values()),
            "missing_trade_session": missing_trade_session,
            "counts": [{"session_date": session,
                        "trades": trades_by_session[session]}
                       for session in sessions],
            "cluster_unit": "session",
            "observation_unit": observation_unit,
        },
    }


def _pairing_index(rows: Sequence[Mapping[str, Any]]) -> tuple[
        dict[tuple[str, str], Mapping[str, Any]], Counter,
        set[tuple[str, str]]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    malformed = Counter()
    for row in rows:
        error = _terminal_error(row)
        if error is not None:
            malformed[error] += 1
            continue
        invalid_numeric = next((key for key in PAIRING_NUMERIC_FIELDS
                                if key in row and row.get(key) is not None and
                                _finite(row.get(key)) is None), None)
        if invalid_numeric is not None:
            malformed[f"invalid_numeric_{invalid_numeric}"] += 1
            continue
        variant_id = str(row.get("_counterfactual_variant_id") or "").strip()
        opportunity_id = str(row.get("opportunity_id") or "").strip()
        if not variant_id:
            malformed["missing_variant_id"] += 1
            continue
        if not opportunity_id:
            malformed["missing_opportunity_id"] += 1
            continue
        key = (variant_id, opportunity_id)
        grouped.setdefault(key, []).append(row)
    duplicates = {key for key, values in grouped.items() if len(values) != 1}
    unique = {key: values[0] for key, values in grouped.items()
              if key not in duplicates}
    return unique, malformed, duplicates


def _transition_state(row: Mapping[str, Any]) -> str:
    reason = str(row.get("reject_reason") or "").strip()
    return reason or str(row.get("execution_disposition") or "unclassified")


def _outcome_signature(row: Mapping[str, Any]) -> str:
    """Hash output and stop/fill evidence while excluding the changed limit."""
    keys = (
        "execution_disposition", "reject_stage", "reject_reason", "vehicle",
        "symbol", "direction", "contract", "quantity", "contract_multiplier",
        "entry_reference", "exit_reference", "entry_price", "exit_price",
        "plan_entry", "underlying_entry", "stop_price", "initial_stop_price",
        "active_stop_price", "stop_distance", "target_price", "breakeven_r",
        "breakeven_armed_at", "gross_pnl", "costs", "net_pnl", "risk_usd",
        "nominal_risk_usd", "realized_risk_usd", "risk_per_unit",
        "realized_risk_per_unit", "r_multiple", "return_value",
        "exit_reason", "entry_fill_source", "exit_fill_source", "entry_feed",
        "exit_feed", "entry_provider", "exit_provider", "entry_option_feed",
        "exit_option_feed", "entry_quote_age_seconds", "exit_quote_age_seconds",
        "entry_slippage_reference", "entry_slippage", "entry_gap_fill",
        "exit_gap_fill", "entry_timestamp", "exit_timestamp", "tie_broken",
        "evidence_mode",
    )
    def sanitized(value: Any) -> Any:
        if value is None or isinstance(value, (bool, str, int)):
            return value
        if isinstance(value, Mapping):
            return {str(key): sanitized(item) for key, item in sorted(
                value.items(), key=lambda pair: str(pair[0]))}
        if isinstance(value, (list, tuple)):
            return [sanitized(item) for item in value]
        number = _finite(value)
        return number if number is not None else {"invalid": type(value).__name__}

    return content_hash({key: sanitized(row.get(key))
                         for key in keys if key in row})


def _paired_measurement(
        baseline_rows: Sequence[Mapping[str, Any]],
        alternative_rows: Sequence[Mapping[str, Any]], *,
        baseline_ratio: float, alternative_ratio: float) -> dict[str, Any]:
    baseline, baseline_malformed, baseline_duplicates = _pairing_index(
        baseline_rows)
    alternative, alternative_malformed, alternative_duplicates = _pairing_index(
        alternative_rows)
    matched_keys = sorted(set(baseline) & set(alternative))
    baseline_only = sorted(set(baseline) - set(alternative))
    alternative_only = sorted(set(alternative) - set(baseline))
    disposition_transitions = Counter()
    reason_transitions = Counter()
    classifications = Counter()
    unexpected = 0
    relaxed = alternative_ratio > baseline_ratio
    for key in matched_keys:
        before, after = baseline[key], alternative[key]
        before_disposition = str(before.get("execution_disposition"))
        after_disposition = str(after.get("execution_disposition"))
        before_state, after_state = _transition_state(before), _transition_state(after)
        disposition_transitions[f"{before_disposition}->{after_disposition}"] += 1
        reason_transitions[f"{before_state}->{after_state}"] += 1
        if before_state == after_state:
            if _outcome_signature(before) == _outcome_signature(after):
                classifications["unchanged"] += 1
            else:
                classifications["path_dependent_output_change"] += 1
                unexpected += 1
        elif (relaxed and before_state == "stressed_cost_risk_limit" and
              after_disposition == "executed"):
            classifications["direct_cost_gate_admission"] += 1
        elif (not relaxed and before_disposition == "executed" and
              after_state == "stressed_cost_risk_limit"):
            classifications["direct_cost_gate_refusal"] += 1
        elif (before_state == "stressed_cost_risk_limit" or
              after_state == "stressed_cost_risk_limit"):
            classifications["downstream_transition_after_cost_gate_change"] += 1
            unexpected += 1
        else:
            classifications["unexpected_or_path_dependent"] += 1
            unexpected += 1
    union_size = len(set(baseline) | set(alternative))
    duplicate_union = baseline_duplicates | alternative_duplicates
    malformed_count = (sum(baseline_malformed.values()) +
                       sum(alternative_malformed.values()))
    complete = (bool(matched_keys) and not baseline_only and not alternative_only and
                not duplicate_union and malformed_count == 0)
    return {
        "schema": "counterfactual-opportunity-pairing.v1",
        "diagnostic_only": True,
        "authorizing": False,
        "key": ["variant_id", "opportunity_id"],
        "matched": len(matched_keys),
        "baseline_only": len(baseline_only),
        "alternative_only": len(alternative_only),
        "duplicate_keys": len(duplicate_union),
        "malformed_rows": malformed_count,
        "malformed_reasons": {
            "baseline": dict(sorted(baseline_malformed.items())),
            "alternative": dict(sorted(alternative_malformed.items())),
        },
        "pairing_coverage": (len(matched_keys) / union_size
                             if union_size else None),
        "reason": (None if complete else
                   "no_valid_pairs" if not matched_keys else
                   "incomplete_or_ambiguous_pairing"),
        "key_digests": {
            "matched": content_hash(matched_keys),
            "baseline_only": content_hash(baseline_only),
            "alternative_only": content_hash(alternative_only),
            "duplicates": content_hash(sorted(duplicate_union)),
        },
        "disposition_transitions": dict(sorted(disposition_transitions.items())),
        "reason_transitions": dict(sorted(reason_transitions.items())),
        "transition_classifications": dict(sorted(classifications.items())),
        "unexpected_or_path_dependent_transitions": unexpected,
        "complete_pairing": complete,
        "only_expected_cost_gate_transitions": bool(complete and unexpected == 0),
        "direct_causal_interpretation_isolated": bool(complete and unexpected == 0),
        "ratio_direction": "relaxation" if relaxed else "tightening",
    }


def _clustered_section_05(
        rows: Sequence[Mapping[str, Any]], *, draws: int,
        min_clusters: int, block_length: int,
        observation_unit: str) -> dict[str, Any]:
    observations: list[tuple[str, str, float]] = []
    missing_r = invalid_r = missing_session = 0
    malformed_identity = Counter()
    keys = Counter(
        (str(row.get("_counterfactual_variant_id") or "").strip(),
         str(row.get("opportunity_id") or "").strip())
        for row in rows
        if str(row.get("_counterfactual_variant_id") or "").strip() and
        str(row.get("opportunity_id") or "").strip())
    duplicate_keys = {key for key, count in keys.items() if count > 1}
    for row in rows:
        variant_id = str(row.get("_counterfactual_variant_id") or "").strip()
        opportunity_id = str(row.get("opportunity_id") or "").strip()
        if not variant_id:
            malformed_identity["missing_variant_id"] += 1
            continue
        if not opportunity_id:
            malformed_identity["missing_opportunity_id"] += 1
            continue
        if (variant_id, opportunity_id) in duplicate_keys:
            malformed_identity["duplicate_opportunity_key"] += 1
            continue
        if (row.get("execution_disposition") != "executed" or
                _terminal_error(row) is not None):
            continue
        session = str(row.get("session_date") or "").strip()
        if not session:
            missing_session += 1
            continue
        raw = row.get("r_multiple")
        if raw is None:
            missing_r += 1
            continue
        value = _finite(raw)
        if value is None:
            invalid_r += 1
            continue
        identity = f"{variant_id}:{opportunity_id}"
        observations.append((session, identity, value))
    observations.sort(key=lambda item: (item[0], item[1]))
    values = [item[2] for item in observations]
    clusters = [item[0] for item in observations]
    interval = moving_block_cluster_bootstrap_lower_bound(
        values, clusters, confidence=.95, draws=draws,
        block_length=block_length, min_clusters=min_clusters)
    interval = {**interval, "diagnostic_only": True, "authorizing": False,
                "effect_unit": "r_multiple_per_variant_trade",
                "cluster_unit": "session"}
    mde = clustered_mde_power_report(
        values, clusters, target_effect=RETIREMENT_MIN_USEFUL_R,
        alpha=.05, target_power=.80, draws=draws,
        block_length=block_length, min_clusters=min_clusters,
        effect_unit="r_multiple_per_variant_trade", cluster_unit="session")
    lower = _finite(interval.get("lower_bound"))
    upper = _finite(interval.get("upper_bound"))
    width = (upper - lower if lower is not None and upper is not None else None)
    observed_mean = _finite(mde.get("observed_mean"))
    mde_value = _finite(mde.get("mde"))
    available = bool(interval.get("available") and mde.get("available"))
    return {
        "schema": "counterfactual-arm-section-05-measurement.v1",
        "diagnostic_only": True,
        "authorizing": False,
        "measurement_kind": "per_arm_empirical_outcome_dispersion",
        "counterfactual_effect_estimate": False,
        "observation_unit": observation_unit,
        "cluster_unit": "session",
        "cohort_warning": (
            "pooled rows are variant-trades from isolated diagnostic accounts; "
            "they are not one deployable candidate's trade stream"),
        "finite_r_observations": len(values),
        "missing_r": missing_r,
        "invalid_r": invalid_r,
        "missing_session": missing_session,
        "excluded_identity_rows": sum(malformed_identity.values()),
        "excluded_identity_reasons": dict(sorted(malformed_identity.items())),
        "confidence_interval": interval,
        "mde_power": mde,
        "dead_band": {
            "schema": "empirical-dead-band-diagnostic.v1",
            "diagnostic_only": True,
            "authorizing": False,
            "available": available,
            "reason": (None if available else
                       interval.get("reason") or mde.get("reason") or
                       "clustered_measurement_unavailable"),
            "fixed_width_assumed": False,
            "economic_floor_r": RETIREMENT_MIN_USEFUL_R,
            "observed_mean_r": observed_mean,
            "confidence_lower_r": lower,
            "confidence_upper_r": upper,
            "confidence_interval_width_r": width,
            "minimum_detectable_effect_r": mde_value,
            "economic_floor_inside_confidence_interval": (
                lower <= RETIREMENT_MIN_USEFUL_R <= upper
                if lower is not None and upper is not None else None),
            "observed_effect_below_mde": (
                abs(observed_mean) < mde_value
                if observed_mean is not None and mde_value is not None else None),
            "interpretation": (
                "data-derived uncertainty; no fixed 0.38R width is assumed"),
        },
    }


def _variant_level_distribution(
        variants: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    sigmas: list[float] = []
    trades_per_session: list[float] = []
    trade_counts: list[float] = []
    for record in variants:
        summary = record.get("summary")
        if not isinstance(summary, Mapping):
            continue
        sigma = _finite((summary.get("r_multiple") or {}).get("sample_sigma"))
        cluster_mean = _finite(
            (summary.get("trades_per_session") or {}).get("mean"))
        trades = _finite(summary.get("trades"))
        if sigma is not None:
            sigmas.append(sigma)
        if cluster_mean is not None:
            trades_per_session.append(cluster_mean)
        if trades is not None:
            trade_counts.append(trades)
    return {
        "schema": "counterfactual-variant-distribution.v1",
        "diagnostic_only": True,
        "authorizing": False,
        "variant_count": len(variants),
        "per_trade_r_sample_sigma": _distribution(sigmas),
        "mean_trades_per_session": _distribution(trades_per_session),
        "trade_count": _distribution(trade_counts),
        "interpretation": (
            "distribution across isolated frozen variants; no pooled candidate "
            "independence is assumed"),
    }


def run_counterfactual(
        data: str | Path | Sequence[Mapping], *,
        specs: Sequence[Mapping[str, Any]], runtime_config: Mapping[str, Any],
        baseline_ratio: float = .30, alternative_ratio: float = .60,
        vehicle: str = "equity", starting_cash: float = 100_000.0,
        bootstrap_draws: int = DEFAULT_COUNTERFACTUAL_DRAWS,
        bootstrap_min_clusters: int = DEFAULT_COUNTERFACTUAL_MIN_CLUSTERS,
        bootstrap_block_length: int = DEFAULT_COUNTERFACTUAL_BLOCK_LENGTH,
        source_report_path: str | Path | None = None,
        source_report_hash: str | None = None,
        source_report_identity: Mapping[str, Any] | None = None) -> dict:
    """Run the same frozen cohort under exactly two stressed-cost ratios."""
    baseline = _ratio(baseline_ratio, "baseline_ratio")
    alternative = _ratio(alternative_ratio, "alternative_ratio")
    cash = _ratio(starting_cash, "starting_cash")
    if baseline == alternative:
        raise ValueError("counterfactual ratios must differ")
    draws = _positive_int(bootstrap_draws, "bootstrap_draws")
    minimum_clusters = _positive_int(
        bootstrap_min_clusters, "bootstrap_min_clusters")
    block_length = _positive_int(
        bootstrap_block_length, "bootstrap_block_length")
    if (source_report_path is not None and
            (source_report_hash is not None or source_report_identity is not None)):
        raise ValueError(
            "source_report_path cannot be combined with asserted source metadata")
    frozen = load_frozen_specs(list(specs))
    source_binding_verified = False
    source_binding_origin = "unavailable"
    if source_report_path is not None:
        source_report_hash, source_report_identity = _source_report_binding(
            source_report_path, frozen)
        source_binding_verified = True
        source_binding_origin = "source_path_recomputed"
    elif source_report_hash is not None or source_report_identity is not None:
        source_binding_origin = "caller_asserted"
    if source_report_hash is not None:
        source_report_hash = str(source_report_hash).strip().lower()
        if (len(source_report_hash) != 64 or
                any(character not in "0123456789abcdef"
                    for character in source_report_hash)):
            raise ValueError("source_report_hash must be a SHA-256 hex digest")
    raw_rows, bars, snapshot_map, quote_rows = _read_discovery_rows(
        data, require_provenance=False,
        expected_equity_feed=ReplayPolicy.from_config(runtime_config).equity_feed)
    quotes = (quote_rows if callable(getattr(quote_rows, "quote_fill", None))
              else list(quote_rows))
    snapshots = list(snapshot_map.values())
    arms: dict[str, dict[str, Any]] = {}
    arm_configs: dict[str, dict[str, Any]] = {}
    arm_rows: dict[str, list[dict[str, Any]]] = {}
    try:
        for name, ratio in (("baseline", baseline),
                            ("alternative", alternative)):
            config = deepcopy(dict(runtime_config))
            risk = dict(config.get("risk") or {})
            risk["max_stressed_cost_to_risk_ratio"] = ratio
            config["risk"] = risk
            arm_configs[name] = config
            policy = diagnostic_backfill_policy(ReplayPolicy.from_config(config))
            costs = CostModel.from_config(config, vehicle=vehicle)
            combined: list[dict] = []
            per_variant = []
            for spec in frozen:
                variant_id = rule_variant_id(spec)
                account = simulate_account(
                    bars, snapshots, spec, vehicle=vehicle,
                    account_id=f"counterfactual:{name}:{variant_id}",
                    starting_cash=cash, costs=costs,
                    quotes=quotes, policy=policy)
                rows = [{**dict(row), "_counterfactual_variant_id": variant_id}
                        for row in account.get("rows", ())
                        if isinstance(row, Mapping)]
                combined.extend(rows)
                per_variant.append({
                    "variant_id": variant_id,
                    "summary": _summarize(rows, observation_unit="trade"),
                })
            arm_rows[name] = combined
            arms[name] = {
                "max_stressed_cost_to_risk_ratio": ratio,
                "replay_policy": policy.as_dict(),
                "config_hash": content_hash(config),
                "diagnostic_only": True,
                "authorizing": False,
                "promotion_allowed": False,
                "production_mutation": False,
                "summary": _summarize(
                    combined, observation_unit="variant_trade"),
                "opportunity_evidence": _opportunity_evidence(combined),
                "section_05_measurement": _clustered_section_05(
                    combined, draws=draws, min_clusters=minimum_clusters,
                    block_length=block_length,
                    observation_unit="variant_trade"),
                "variant_level_empirical_distribution": (
                    _variant_level_distribution(per_variant)),
                "variants": per_variant,
            }
    finally:
        close = getattr(quote_rows, "close", None)
        if callable(close) and isinstance(quote_rows, SQLiteQuoteIndex):
            close()
    base = arms["baseline"]["summary"]
    alt = arms["alternative"]["summary"]
    changed_paths = _changed_paths(
        arm_configs["baseline"], arm_configs["alternative"])
    exact_change = changed_paths == [CHANGED_FIELD]
    pairing = _paired_measurement(
        arm_rows["baseline"], arm_rows["alternative"],
        baseline_ratio=baseline, alternative_ratio=alternative)
    dataset_hash = content_hash(raw_rows)
    source_identity = dict(source_report_identity or {})
    expected_dataset_hash = str(source_identity.get("dataset_hash") or "").strip()
    source_dataset_matches = (dataset_hash == expected_dataset_hash
                              if expected_dataset_hash else None)
    source_has_safety_contract = (
        "diagnostic_only" in source_identity or "authorizing" in source_identity)
    source_safety_matches = (
        source_identity.get("diagnostic_only") is True and
        source_identity.get("authorizing") is False
        if source_has_safety_contract else None)
    source_proof_count = source_identity.get("proof_count")
    source_proofs_empty = (int(source_proof_count) == 0
                           if isinstance(source_proof_count, int) else None)
    variant_ids = [rule_variant_id(spec) for spec in frozen]
    cohort_hash = hashlib.sha256(json.dumps(
        frozen, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    code_files = _code_bundle_files()
    code_hash = _code_bundle_hash(code_files)
    runtime_config_hash = content_hash(runtime_config)
    settings = {
        "schema": SCHEMA,
        "baseline_ratio": baseline,
        "alternative_ratio": alternative,
        "vehicle": vehicle,
        "starting_cash": cash,
        "bootstrap": {"draws": draws, "min_clusters": minimum_clusters,
                      "block_length": block_length},
        "dataset_hash": dataset_hash,
        "frozen_cohort_hash": cohort_hash,
        "runtime_config_hash": runtime_config_hash,
        "measurement_code_hash": code_hash,
        "source_report_hash": source_report_hash,
    }
    invariant_failures = ([] if exact_change else
                          ["unexpected_counterfactual_config_change"])
    if source_dataset_matches is False:
        invariant_failures.append("source_report_dataset_hash_mismatch")
    if source_binding_verified and source_dataset_matches is None:
        invariant_failures.append("source_report_dataset_hash_missing")
    if source_safety_matches is False:
        invariant_failures.append("source_report_not_strictly_diagnostic")
    if source_binding_verified and source_safety_matches is None:
        invariant_failures.append("source_report_safety_contract_missing")
    if source_proofs_empty is False:
        invariant_failures.append("source_report_contains_authorizing_proofs")
    if source_binding_verified and source_proofs_empty is None:
        invariant_failures.append("source_report_proofs_contract_missing")
    if source_binding_origin == "caller_asserted":
        invariant_failures.append("source_report_binding_unverified")
    configuration_change_verified = not invariant_failures
    if not pairing["complete_pairing"]:
        invariant_failures.append("incomplete_or_ambiguous_pairing")
    if not pairing["direct_causal_interpretation_isolated"]:
        invariant_failures.append("direct_causal_interpretation_not_isolated")
    result = {
        "schema": SCHEMA,
        "compatibility": {"legacy_schema": LEGACY_SCHEMA,
                          "v1_fields_preserved": True},
        "status": ("no_signal_reachability" if
                   not base["signal_opportunities"] and
                   not alt["signal_opportunities"] else "measured"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "diagnostic_only": True,
        "authorizing": False,
        "promotion_allowed": False,
        "production_mutation": False,
        "proofs": [],
        "scope": _counterfactual_scope(),
        "primary_endpoint": "stressed_cost_risk_limit refusal rate",
        "dataset_hash": dataset_hash,
        "frozen_variant_ids": variant_ids,
        "frozen_cohort_hash": cohort_hash,
        "only_changed_field": CHANGED_FIELD,
        "config_evidence": {
            "runtime_config_hash": runtime_config_hash,
            "baseline_config_hash": content_hash(arm_configs["baseline"]),
            "alternative_config_hash": content_hash(arm_configs["alternative"]),
            "changed_paths": changed_paths,
            "expected_changed_paths": [CHANGED_FIELD],
            "unexpected_changed_paths": [path for path in changed_paths
                                         if path != CHANGED_FIELD],
            "exact_only_changed_field": exact_change,
            "config_contents_persisted": False,
        },
        "provenance": {
            "dataset_hash": dataset_hash,
            "runtime_config_hash": runtime_config_hash,
            "frozen_cohort_hash": cohort_hash,
            "source_report_hash": source_report_hash,
            "source_report_hash_available": bool(source_report_hash),
            "source_report_hash_verified": bool(
                source_report_hash and source_binding_verified),
            "source_report_binding_origin": source_binding_origin,
            "source_report_schema": source_identity.get("schema"),
            "source_report_dataset_hash": expected_dataset_hash or None,
            "source_report_dataset_matches": source_dataset_matches,
            "source_report_safety_matches": source_safety_matches,
            "source_report_proof_count": source_proof_count,
            "source_report_proofs_empty": source_proofs_empty,
            "measurement_code_hash": code_hash,
            "measurement_code_files": list(code_files),
            "run_settings_hash": content_hash(settings),
        },
        "invariants": {
            "diagnostic_only": True,
            "authorizing": False,
            "promotion_allowed": False,
            "production_mutation": False,
            "exactly_two_arms": len(arms) == 2,
            "exact_only_changed_field": exact_change,
            "source_report_dataset_matches": source_dataset_matches,
            "source_report_safety_matches": source_safety_matches,
            "source_report_proofs_empty": source_proofs_empty,
            "source_report_binding_verified": source_binding_verified,
            "source_report_contract_complete": bool(
                source_dataset_matches is not None and
                source_safety_matches is not None and
                source_proofs_empty is not None),
            "configuration_change_verified": configuration_change_verified,
            "pairing_complete": pairing["complete_pairing"],
            "direct_causal_interpretation_isolated": pairing[
                "direct_causal_interpretation_isolated"],
            "controlled_change_verified": not invariant_failures,
            "invariant_failures": invariant_failures,
        },
        "arms": arms,
        "pairing": pairing,
        "funnel": _funnel_manifest(),
        "difference": {
            "stressed_cost_rejections": (
                alt["stressed_cost_risk_limit"] -
                base["stressed_cost_risk_limit"]),
            "admitted_trades": alt["trades"] - base["trades"],
            "signal_opportunities": (
                alt["signal_opportunities"] - base["signal_opportunities"]),
            "pnl_interpretation": "descriptive_only",
        },
        "decision_rule": (
            "This one-cycle counterfactual may justify a separately reviewed "
            "policy experiment; it cannot change production or promote an edge."),
    }
    result["content_hash"] = content_hash(result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--specs", required=True)
    parser.add_argument("--agent-config", default="config.yaml")
    parser.add_argument("--vehicle", choices=("equity", "option"), default="equity")
    parser.add_argument("--baseline", type=float, default=.30)
    parser.add_argument("--alternative", type=float, default=.60)
    parser.add_argument("--starting-cash", type=float, default=100_000.0)
    parser.add_argument("--bootstrap-draws", type=int,
                        default=DEFAULT_COUNTERFACTUAL_DRAWS)
    parser.add_argument("--bootstrap-min-clusters", type=int,
                        default=DEFAULT_COUNTERFACTUAL_MIN_CLUSTERS)
    parser.add_argument("--bootstrap-block-length", type=int,
                        default=DEFAULT_COUNTERFACTUAL_BLOCK_LENGTH)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    source_payload = json.loads(Path(args.specs).read_text(encoding="utf-8"))
    frozen_specs = load_frozen_specs(source_payload)
    del source_payload
    result = run_counterfactual(
        args.data, specs=frozen_specs,
        runtime_config=load_config(args.agent_config),
        baseline_ratio=args.baseline, alternative_ratio=args.alternative,
        vehicle=args.vehicle, starting_cash=args.starting_cash,
        bootstrap_draws=args.bootstrap_draws,
        bootstrap_min_clusters=args.bootstrap_min_clusters,
        bootstrap_block_length=args.bootstrap_block_length,
        source_report_path=args.specs)
    serialized = json.dumps(
        result, sort_keys=True, allow_nan=False, default=str) + "\n"
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, target)
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
