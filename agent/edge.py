"""Runtime boundary for validated edge variants.

Research may create and score many candidates, but paper execution receives a
variant only after an auditable ``validated`` or ``champion`` ledger state.
Unknown, candidate, demoted, and retired ids all fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .registry import baseline_variant_id, known_variant_ids
from .variants import from_record, apply as apply_variant_config
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


def _decoded(record: Mapping) -> dict | None:
    result = dict(record)
    try:
        result["config"] = json.loads(result.get("config_json") or "{}")
        result["axes"] = json.loads(result.get("axes_json") or "{}")
    except (TypeError, ValueError):
        return None
    return result


def _latest_passing_proof(ledger: EdgeLedger, record: Mapping) -> dict | None:
    candidate_id = str(record.get("candidate_id") or "")
    latest = ledger.latest_verified_run(candidate_id, lane="shadow")
    if not isinstance(latest, Mapping):
        return None
    # The proof must attest the immutable candidate configuration itself.
    # Dataset and code hashes describe the evidence corpus/build and may
    # legitimately differ between candidate and shadow run; config identity
    # cannot drift without invalidating the edge.
    candidate_config_hash = record.get("config_hash")
    proof_config_hash = latest.get("config_hash")
    if (not isinstance(candidate_config_hash, str) or
            not isinstance(proof_config_hash, str) or
            not candidate_config_hash or not proof_config_hash or
            proof_config_hash != candidate_config_hash):
        return None
    gate = latest.get("verified_gate")
    if not isinstance(gate, Mapping) or gate.get("passes") is not True:
        return None
    return dict(latest)


def _proof_confidence(proof: Mapping) -> float:
    gate = proof.get("verified_gate") if isinstance(
        proof.get("verified_gate"), Mapping) else {}
    statistics = gate.get("statistics") if isinstance(
        gate.get("statistics"), Mapping) else {}
    q_value = statistics.get("q_value")
    if q_value is not None:
        try:
            return max(0.0, min(1.0, 1.0 - float(q_value)))
        except (TypeError, ValueError):
            return 0.0
    metrics = proof.get("metrics") if isinstance(proof.get("metrics"), Mapping) else {}
    try:
        return max(0.0, min(1.0, float(metrics.get("confidence", 0.0) or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _eligible(ledger: EdgeLedger, record: Mapping | None, *, strategy_id: str,
              vehicle: str, min_confidence: float = 0.0) -> dict | None:
    if (record is None or record.get("strategy_id") != strategy_id or
            record.get("vehicle") != vehicle or
            record.get("status") not in {"validated", "champion"}):
        return None
    proof = _latest_passing_proof(ledger, record)
    decoded = _decoded(record)
    if (proof is None or decoded is None or
            _proof_confidence(proof) < float(min_confidence)):
        return None
    decoded["latest_proof"] = proof
    return decoded


def _family(record: Mapping) -> str:
    axes = record.get("axes") if isinstance(record.get("axes"), Mapping) else {}
    config = record.get("config") if isinstance(record.get("config"), Mapping) else {}
    strategy = config.get("strategy") if isinstance(config.get("strategy"), Mapping) else {}
    spec = strategy.get("rule_spec") if isinstance(strategy.get("rule_spec"), Mapping) else {}
    return str(axes.get("hypothesis_id") or axes.get("family") or
               spec.get("family") or record.get("strategy_id") or "unknown")


def _proof_score(record: Mapping) -> tuple:
    proof = record.get("latest_proof") if isinstance(record.get("latest_proof"), Mapping) else {}
    gate = proof.get("verified_gate") if isinstance(proof.get("verified_gate"), Mapping) else {}
    performance = gate.get("performance") if isinstance(
        gate.get("performance"), Mapping) else {}
    counts = gate.get("counts") if isinstance(gate.get("counts"), Mapping) else {}
    heldout = counts.get("heldout") if isinstance(counts.get("heldout"), Mapping) else {}
    confidence = _proof_confidence(proof)
    heldout_delta = float(performance.get("heldout_delta", -float("inf")))
    drawdown = float(performance.get("max_drawdown", float("inf")))
    trades = int(heldout.get("trades", 0) or 0)
    return (confidence, heldout_delta, -drawdown, trades,
            str(record.get("variant_id") or ""),
            str(record.get("candidate_id") or ""))


def resolve_validated_variant(config: Mapping, vehicle: str | None = None,
                              db_path: str | Path | None = None,
                              candidate_id: str | None = None) -> dict | None:
    """Resolve a config's variant only when the ledger proves it validated."""
    strategy = config.get("strategy", {}) if isinstance(config, Mapping) else {}
    strategy_id = str(strategy.get("id") or "ibr")
    selected_vehicle = _vehicle(config, vehicle)
    if selected_vehicle not in {"equity", "option"}:
        return None
    ledger = EdgeLedger(db_path or DEFAULT_DB_PATH)
    research = config.get("research", {}) if isinstance(config, Mapping) else {}
    min_confidence = float(research.get("champion_min_confidence", .95) or .95) \
        if isinstance(research, Mapping) else .95
    requested = str(strategy.get("variant_id") or "").strip()
    if requested and requested.lower() != "auto":
        if strategy_id != "rule" and requested not in set(known_variant_ids(strategy_id)):
            return None
        record = ledger.candidate_by_variant(requested, selected_vehicle)
    else:
        record = ledger.select_champion(
            vehicle=selected_vehicle, min_confidence=min_confidence,
            strategy_id=strategy_id)
    record = _eligible(
        ledger, record, strategy_id=strategy_id, vehicle=selected_vehicle,
        min_confidence=min_confidence)
    if record is None:
        return None
    if candidate_id is not None and record.get("candidate_id") != candidate_id:
        return None
    return record


def resolve_validated_variants(config: Mapping, vehicle: str | None = None,
                               db_path: str | Path | None = None) -> list[dict]:
    """Resolve deterministic vehicle-local proved edges for paper execution.

    ``specific`` retains the single-record behavior.  ``all_proved`` selects
    the strongest latest passing proof in each independent hypothesis/family,
    preventing every mutation of one idea from becoming concurrent risk.
    """
    strategy = config.get("strategy", {}) if isinstance(config, Mapping) else {}
    if str(strategy.get("selection_mode") or "specific") == "specific":
        record = resolve_validated_variant(config, vehicle=vehicle, db_path=db_path)
        return [record] if record is not None else []
    selected_vehicle = _vehicle(config, vehicle)
    if selected_vehicle not in {"equity", "option"}:
        return []
    strategy_id = str(strategy.get("id") or "ibr")
    research = config.get("research", {}) if isinstance(config, Mapping) else {}
    min_confidence = float(research.get("champion_min_confidence", .95) or .95) \
        if isinstance(research, Mapping) else .95
    ledger = EdgeLedger(db_path or DEFAULT_DB_PATH)
    grouped: dict[str, dict] = {}
    for raw in ledger.status(vehicle=selected_vehicle):
        record = _eligible(
            ledger, raw, strategy_id=strategy_id, vehicle=selected_vehicle,
            min_confidence=min_confidence)
        if record is None:
            continue
        family = _family(record)
        current = grouped.get(family)
        if current is None or _proof_score(record) > _proof_score(current):
            grouped[family] = record
    return [grouped[key] for key in sorted(grouped)]


def apply_variant(config: Mapping, record: Mapping) -> dict:
    """Apply an already-resolved record to a validated runtime config."""
    if str(record.get("status")) not in {"validated", "champion"}:
        raise ValueError("only validated or champion variants may be applied")
    variant_id = str(record.get("variant_id") or "")
    if not variant_id:
        raise ValueError("validated record has no variant_id")
    strategy_id = str(record.get("strategy_id") or "ibr")
    if strategy_id == "rule":
        from .config import validate_config
        from .contracts.rule import rule_variant_id, validate_rule_spec
        record_config = record.get("config")
        if not isinstance(record_config, Mapping):
            try:
                record_config = json.loads(record.get("config_json") or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("validated rule record has invalid config") from exc
        raw_spec = (record_config.get("strategy") or {}).get("rule_spec") \
            if isinstance(record_config, Mapping) else None
        spec = validate_rule_spec(raw_spec or {})
        if rule_variant_id(spec) != variant_id:
            raise ValueError("validated rule record does not match its content hash")
        applied = dict(config)
        applied["strategy"] = dict(applied.get("strategy") or {})
        applied["strategy"].update({"id": "rule", "version": "v1",
                                    "variant_id": variant_id, "rule_spec": spec})
        vehicle = str(record.get("vehicle") or "")
        if vehicle in {"equity", "option"}:
            applied["strategy"]["execution_mode"] = (
                "options" if vehicle == "option" else "shares")
        return validate_config(applied)
    if variant_id not in set(known_variant_ids(strategy_id)):
        raise ValueError(f"variant {variant_id!r} is not pre-registered")
    overrides = record.get("overrides") or {}
    if not overrides and isinstance(record.get("axes"), Mapping):
        overrides = record["axes"].get("overrides") or {}
    if not overrides and variant_id != baseline_variant_id(strategy_id):
        raise ValueError("validated variant record lacks immutable overrides")
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


__all__ = ["apply_variant", "record_paper_outcome", "resolve_validated_variant",
           "resolve_validated_variants"]
