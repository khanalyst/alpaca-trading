"""Runtime boundary for validated edge variants.

Research may create and score many candidates, but paper execution receives a
variant only after an auditable ``validated`` or ``champion`` ledger state.
Unknown, candidate, demoted, and retired ids all fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .registry import known_variant_ids
from .variants import from_record, load_registry, apply as apply_variant_config
from research.edge_lab import DEFAULT_DB_PATH, EdgeLedger


def _vehicle(config: Mapping, vehicle: str | None) -> str | None:
    if vehicle is not None:
        return str(vehicle)
    strategy = config.get("strategy", {}) if isinstance(config, Mapping) else {}
    execution_mode = str(strategy.get("execution_mode", "")).lower()
    if execution_mode in {"options", "option"}:
        return "option"
    if execution_mode in {"shares", "stock", "equity"}:
        return "equity"
    classes = config.get("universe", {}).get("asset_classes", []) if isinstance(config, Mapping) else []
    if any(str(item).lower() in {"us_option", "option", "options"} for item in classes) and \
            not any(str(item).lower() in {"us_equity", "equity", "stock"} for item in classes):
        return "option"
    return "equity"


def resolve_validated_variant(config: Mapping, vehicle: str | None = None,
                              db_path: str | Path | None = None) -> dict | None:
    """Resolve a config's variant only when the ledger proves it validated."""
    strategy = config.get("strategy", {}) if isinstance(config, Mapping) else {}
    strategy_id = str(strategy.get("id") or "ibr")
    selected_vehicle = _vehicle(config, vehicle)
    if selected_vehicle not in {"equity", "option"}:
        return None
    ledger = EdgeLedger(db_path or DEFAULT_DB_PATH)
    requested = str(strategy.get("variant_id") or "").strip()
    if requested and requested.lower() != "auto":
        if requested not in set(known_variant_ids(strategy_id)):
            return None
        record = ledger.candidate_by_variant(requested, selected_vehicle)
    else:
        research = config.get("research", {}) if isinstance(config, Mapping) else {}
        confidence = float(research.get("champion_min_confidence", .95) or .95) \
            if isinstance(research, Mapping) else .95
        record = ledger.select_champion(
            vehicle=selected_vehicle, min_confidence=confidence)
    if record is None or record.get("status") not in {"validated", "champion"}:
        return None
    try:
        record["config"] = json.loads(record.get("config_json") or "{}")
        record["axes"] = json.loads(record.get("axes_json") or "{}")
    except (TypeError, ValueError):
        return None
    return record


def apply_variant(config: Mapping, record: Mapping) -> dict:
    """Apply an already-resolved record to a validated runtime config."""
    if str(record.get("status")) not in {"validated", "champion"}:
        raise ValueError("only validated or champion variants may be applied")
    variant_id = str(record.get("variant_id") or "")
    if not variant_id:
        raise ValueError("validated record has no variant_id")
    strategy_id = str(record.get("strategy_id") or "ibr")
    if variant_id not in set(known_variant_ids(strategy_id)):
        raise ValueError(f"variant {variant_id!r} is not pre-registered")
    overrides = record.get("overrides") or {}
    if not overrides and isinstance(record.get("axes"), Mapping):
        overrides = record["axes"].get("overrides") or {}
    if not overrides:
        registry_path = Path(__file__).resolve().parents[1] / "research" / "variants.yaml"
        registered = load_registry(registry_path).get(variant_id)
        if registered is not None:
            overrides = dict(registered.overrides)
    if isinstance(overrides, str):
        try:
            overrides = json.loads(overrides)
        except json.JSONDecodeError as exc:
            raise ValueError("validated record overrides are invalid JSON") from exc
    base = dict(record)
    base.update({"variant_id": variant_id, "strategy_id": strategy_id,
                 "base_version": record.get("base_version", "v1"), "overrides": overrides,
                 "hypothesis": record.get("hypothesis", "")})
    variant = from_record(base)
    applied = apply_variant_config(variant, dict(config))
    vehicle = str(record.get("vehicle") or "")
    if vehicle in {"equity", "option"}:
        applied.setdefault("strategy", {})["execution_mode"] = (
            "options" if vehicle == "option" else "shares")
    return applied


def record_paper_outcome(outcome: Mapping, *, candidate_id: str | None = None,
                         db_path: str | Path | None = None) -> dict:
    """Append an observed paper outcome and return the resulting status."""
    ledger = EdgeLedger(db_path or DEFAULT_DB_PATH)
    cid = candidate_id or outcome.get("candidate_id")
    if not cid:
        variant_id = outcome.get("variant_id")
        vehicle = outcome.get("vehicle")
        if not variant_id or not vehicle:
            raise ValueError("candidate_id or variant_id+vehicle is required")
        record = ledger.candidate_by_variant(str(variant_id), str(vehicle))
        if record is None:
            raise KeyError(f"unknown candidate {variant_id!r}/{vehicle!r}")
        cid = record["candidate_id"]
    return ledger.ingest_paper_outcome(str(cid), outcome)


__all__ = ["apply_variant", "record_paper_outcome", "resolve_validated_variant"]
