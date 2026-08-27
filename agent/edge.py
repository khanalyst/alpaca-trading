"""Runtime boundary for validated edge variants.

Research may create and score many candidates, but paper execution receives a
variant only after an auditable ``validated`` or ``champion`` ledger state.
Unknown, candidate, demoted, and retired ids all fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .allocation import evidence_rank
from .registry import baseline_variant_id, known_variant_ids
from .variants import from_record, apply as apply_variant_config
from research.edge_lab import DEFAULT_DB_PATH, EdgeLedger
from research.edge_identity import candidate_assumptions
from research.edge_ledger_store import content_hash


VEHICLES = ("equity", "option")


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


def runtime_vehicle(config: Mapping) -> str | None:
    """The one vehicle this deployment's execution profile can trade.

    A trader process runs a single execution profile, so proving an edge in the
    other vehicle produces evidence it can never act on.  Research uses this to
    scope what it studies to what the deployment could actually deploy.
    """
    return _vehicle(config, None)


def research_vehicles(config: Mapping, override: str | None = None) -> list[str]:
    """Vehicles worth researching, defaulting to what the trader can trade.

    ``override`` accepts ``all`` or a comma-separated subset, so a deployment
    that intends to switch profiles — or one recording options for later — can
    keep both lanes running deliberately rather than by accident.
    """
    raw = str(override or "").strip().lower()
    if raw in {"all", "both"}:
        return list(VEHICLES)
    if raw:
        selected = [item.strip() for item in raw.split(",") if item.strip()]
        unknown = sorted({item for item in selected if item not in VEHICLES})
        if unknown:
            raise ValueError(
                f"unknown research vehicle(s): {', '.join(unknown)}")
        return [item for item in VEHICLES if item in set(selected)]
    vehicle = runtime_vehicle(config)
    return [vehicle] if vehicle in VEHICLES else []


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


def _runtime_identity_matches(record: Mapping, runtime_config: Mapping) -> bool:
    """Bind the loaded runtime assumptions to the immutable candidate hash.

    Applying a record is deliberately local and side-effect free; this helper
    never resolves another edge.  A malformed or legacy candidate config is a
    hard stop, even when its stored proof itself still verifies.
    """
    try:
        # Applying a candidate invokes the strict runtime validator.  For
        # identity purposes we neutralize the process's paper/live guard so a
        # refresh after startup does not depend on an environment variable;
        # candidate_assumptions itself intentionally excludes deployment mode.
        identity_config = dict(runtime_config)
        identity_config["mode"] = "paper"
        identity_broker = dict(identity_config.get("broker") or {})
        identity_broker.update({"paper": True, "allow_live": False})
        identity_config["broker"] = identity_broker
        applied = apply_variant(identity_config, record)
        record_config = record.get("config")
        if not isinstance(record_config, Mapping):
            record_config = json.loads(record.get("config_json") or "{}")
        strategy = record_config.get("strategy") if isinstance(record_config, Mapping) else {}
        rule_spec = strategy.get("rule_spec") if isinstance(strategy, Mapping) else None
        assumptions = candidate_assumptions(
            applied, vehicle=str(record.get("vehicle") or ""),
            strategy_id=str(record.get("strategy_id") or "ibr"),
            variant_id=str(record.get("variant_id") or ""),
            rule_spec=rule_spec if isinstance(rule_spec, Mapping) else None)
        return content_hash(assumptions) == str(record.get("config_hash") or "")
    except Exception:  # noqa: BLE001 - identity mismatch fails closed
        return False


def _eligible(ledger: EdgeLedger, record: Mapping | None, *, strategy_id: str,
              vehicle: str, min_confidence: float = 0.0,
              runtime_config: Mapping | None = None) -> dict | None:
    if (record is None or record.get("strategy_id") != strategy_id or
            record.get("vehicle") != vehicle or
            record.get("status") not in {"validated", "champion"}):
        return None
    # ``eligibility`` is the ledger's single deployment boundary.  It
    # re-verifies the latest immutable shadow gate, rejects a proof produced
    # by an older replay engine epoch, and requires the complete
    # parity-matched live-shadow ingestion marker.  Resolver modes must not
    # duplicate a weaker subset of those checks: a candidate that is safe for
    # ``select_champion`` is the same candidate that is safe when explicitly
    # requested, selected by ``all_proved``, or pinned by an operator.
    candidate_id = str(record.get("candidate_id") or "")
    if not candidate_id:
        return None
    try:
        boundary = ledger.eligibility(candidate_id, lane="shadow")
    except Exception:  # noqa: BLE001 - unreadable authorization fails closed
        return None
    if (not isinstance(boundary, Mapping) or
            boundary.get("eligible") is not True):
        return None
    proof = boundary.get("latest_verified_run")
    if not isinstance(proof, Mapping):
        return None
    decoded = _decoded(record)
    if (decoded is None or
            _proof_confidence(proof) < float(min_confidence)):
        return None
    # The eligibility boundary authenticates the run and gate; this identity
    # check binds that proof to the immutable candidate configuration so a
    # stale/mis-attributed run can never authorize a different record.
    candidate_config_hash = record.get("config_hash")
    proof_config_hash = proof.get("config_hash")
    if (not isinstance(candidate_config_hash, str) or
            not isinstance(proof_config_hash, str) or
            not candidate_config_hash or proof_config_hash != candidate_config_hash):
        return None
    if runtime_config is not None and not _runtime_identity_matches(
            record, runtime_config):
        return None
    decoded["latest_proof"] = proof
    return decoded


def _family(record: Mapping) -> str:
    axes = record.get("axes") if isinstance(record.get("axes"), Mapping) else {}
    config = record.get("config") if isinstance(record.get("config"), Mapping) else {}
    strategy = config.get("strategy") if isinstance(config.get("strategy"), Mapping) else {}
    spec = strategy.get("rule_spec") if isinstance(strategy.get("rule_spec"), Mapping) else {}
    # Hypothesis ids identify parameter lineages, not executable families.
    # all_proved must keep one strongest edge per actual rule family so a
    # mutation cannot multiply runtime risk merely by changing its lineage.
    return str(spec.get("family") or axes.get("family") or
               axes.get("hypothesis_id") or record.get("strategy_id") or "unknown")


# One notion of "better evidence" for the whole runtime: the ledger's own
# conservative champion ordering.  ``_eligible`` has already applied the
# confidence floor, so ranking is purely on the strength of the held-out proof.
_proof_score = evidence_rank


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
        min_confidence=min_confidence, runtime_config=config)
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
    preventing every mutation of one idea from becoming concurrent risk.  The
    families are returned strongest-evidence first, not by family name: order
    is what decides which candidate meets the shared risk caps first, so it
    must be decided by evidence.
    """
    strategy = config.get("strategy", {}) if isinstance(config, Mapping) else {}
    mode = str(strategy.get("selection_mode") or "specific")
    if mode == "specific":
        record = resolve_validated_variant(config, vehicle=vehicle, db_path=db_path)
        return [record] if record is not None else []
    if mode == "pinned":
        return resolve_pinned_variants(config, vehicle=vehicle, db_path=db_path)
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
            min_confidence=min_confidence, runtime_config=config)
        if record is None:
            continue
        family = _family(record)
        current = grouped.get(family)
        if current is None or _proof_score(record) > _proof_score(current):
            grouped[family] = record
    return sorted(grouped.values(), key=evidence_rank, reverse=True)


def resolve_pinned_variants(config: Mapping, vehicle: str | None = None,
                            db_path: str | Path | None = None) -> list[dict]:
    """Resolve exactly the edges the operator pinned, and nothing else.

    Pinning is a selection, not an authorization.  Each entry still has to
    resolve to a ``validated``/``champion`` record whose latest shadow proof
    re-verifies and passes, so an id written into a file can never put an
    unproved variant on the book.  What it does guarantee is the converse: no
    automatic process chose this, and none will change it.

    An entry that does not resolve is skipped rather than substituted.  Quietly
    trading a different edge than the one named would be the worst possible
    reading of a promotion.
    """
    strategy = config.get("strategy", {}) if isinstance(config, Mapping) else {}
    entries = strategy.get("pinned") or []
    if not entries:
        return []
    selected_vehicle = _vehicle(config, vehicle)
    research = config.get("research", {}) if isinstance(config, Mapping) else {}
    min_confidence = float(research.get("champion_min_confidence", .95) or .95) \
        if isinstance(research, Mapping) else .95
    ledger = EdgeLedger(db_path or DEFAULT_DB_PATH)
    resolved: list[dict] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        entry_vehicle = str(entry.get("vehicle") or selected_vehicle)
        # A trader runs one execution profile; a promotion for the other
        # vehicle is a real record this process simply cannot act on.
        if entry_vehicle != selected_vehicle:
            continue
        record = _eligible(
            ledger,
            ledger.candidate_by_variant(str(entry.get("variant_id")), entry_vehicle),
            strategy_id=str(entry.get("strategy_id") or "rule"),
            vehicle=entry_vehicle, min_confidence=min_confidence,
            runtime_config=config)
        if record is None:
            continue
        # Carry the promotion through so every downstream record — journal
        # rows, notifications, the dashboard — can name the decision that put
        # this edge on the book.
        record["promotion"] = {"id": str(entry.get("id") or ""),
                               "note": str(entry.get("note") or ""),
                               "promoted_at": str(entry.get("promoted_at") or "")}
        record["pinned"] = True
        resolved.append(record)
    return sorted(resolved, key=evidence_rank, reverse=True)


def unresolved_promotions(config: Mapping, vehicle: str | None = None,
                          db_path: str | Path | None = None) -> list[dict]:
    """Pinned entries that cannot currently trade, and why.

    A promotion that silently resolves to nothing is the failure mode worth
    surfacing: the operator believes an edge is deployed and it is not.
    """
    strategy = config.get("strategy", {}) if isinstance(config, Mapping) else {}
    entries = strategy.get("pinned") or []
    if not entries:
        return []
    selected_vehicle = _vehicle(config, vehicle)
    ledger = EdgeLedger(db_path or DEFAULT_DB_PATH)
    research = config.get("research", {}) if isinstance(config, Mapping) else {}
    min_confidence = float(research.get("champion_min_confidence", .95) or .95) \
        if isinstance(research, Mapping) else .95
    problems = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        variant_id = str(entry.get("variant_id") or "")
        entry_vehicle = str(entry.get("vehicle") or selected_vehicle)
        record = ledger.candidate_by_variant(variant_id, entry_vehicle)
        if entry_vehicle != selected_vehicle:
            reason = (f"pinned for {entry_vehicle}, but this trader runs "
                      f"{selected_vehicle}")
        elif record is None:
            reason = "no candidate with this variant_id exists in the ledger"
        elif record.get("status") not in {"validated", "champion"}:
            reason = f"candidate status is {record.get('status')!r}"
        elif _eligible(ledger, record, strategy_id=str(entry.get("strategy_id") or "rule"),
                       vehicle=entry_vehicle, min_confidence=min_confidence,
                       runtime_config=config) is None:
            reason = "no re-verified passing shadow proof at the required confidence"
        else:
            continue
        problems.append({"id": str(entry.get("id") or ""),
                         "variant_id": variant_id, "vehicle": entry_vehicle,
                         "reason": reason})
    return problems


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
                         db_path: str | Path | None = None,
                         config: Mapping | None = None) -> dict:
    """Append an observed paper outcome and return the resulting status.

    When ``config`` pins this variant, the outcome carries the promotion
    context into the authoritative lifecycle guard. Pinning selects an identity
    but does not exempt it from sequential-drift or trial demotion; a demoted
    pinned edge is removed from runtime selection and the operator's promotion
    remains auditable. Rolling-R telemetry is advisory.
    """
    ledger = EdgeLedger(db_path or DEFAULT_DB_PATH)
    cid = candidate_id or outcome.get("candidate_id")
    variant_id = outcome.get("variant_id")
    vehicle = outcome.get("vehicle")
    if not cid:
        if not variant_id or not vehicle:
            raise ValueError("candidate_id or variant_id+vehicle is required")
        record = ledger.candidate_by_variant(str(variant_id), str(vehicle))
        if record is None:
            raise KeyError(f"unknown candidate {variant_id!r}/{vehicle!r}")
        cid = record["candidate_id"]
    frozen = False
    pin_context: dict[str, object] = {}
    if config is not None:
        if not variant_id or not vehicle:
            record = ledger.candidate(str(cid)) or {}
            variant_id = variant_id or record.get("variant_id")
            vehicle = vehicle or record.get("vehicle")
        strategy = config.get("strategy") if isinstance(config, Mapping) else {}
        entries = strategy.get("pinned") if isinstance(strategy, Mapping) else ()
        for entry in entries or ():
            if (isinstance(entry, Mapping) and
                    str(entry.get("variant_id")) == str(variant_id) and
                    str(entry.get("vehicle") or "equity") == str(vehicle)):
                frozen = True
                pin_context = {
                    key: entry.get(key) for key in
                    ("id", "variant_id", "vehicle", "strategy_id",
                     "promoted_at", "note") if key in entry
                }
                break
    return ledger.ingest_paper_outcome(
        str(cid), outcome, frozen=frozen, pin_context=pin_context)


__all__ = ["VEHICLES", "apply_variant", "record_paper_outcome",
           "research_vehicles", "resolve_pinned_variants",
           "resolve_validated_variant", "resolve_validated_variants",
           "runtime_vehicle", "unresolved_promotions"]
