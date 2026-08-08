"""Content-addressed, non-authorizing handoffs for discovery evidence.

Discovery can identify a typed observable and a complete, point-in-time
result, but it cannot register a strategy.  This module is the explicit
boundary between that persisted evidence and a future human decision.  It
reads an immutable ``analysis_runs`` row, verifies the result JSON and typed
artifact it names, and writes only a content-addressed review packet.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .artifact import sha256_json
from .discovery import (SCHEMA, DiscoveryProgram, canonical_json,
                        content_hash, program_identity_hash, verify_artifact)


HANDOFF_SCHEMA = "discovery_registration_handoff.v1"
DEFAULT_RESULT_DIR = "research/results/discovery"
DEFAULT_ARTIFACT_DIR = "research/results/discovery-artifacts"
DEFAULT_HANDOFF_DIR = "research/results/discovery-handoffs"


class HandoffError(ValueError):
    """Persisted discovery evidence cannot produce a safe handoff."""


DiscoveryHandoffError = HandoffError


def _canonical(value: Any) -> str:
    return canonical_json(value)


def _same(left: object, right: object) -> bool:
    return _canonical(left) == _canonical(right)


def _full_digest(name: str, value: object) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise HandoffError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _analysis_payload_hash(kind: str, subject_id: str, payload: Mapping) -> str:
    # FindingsStore hashes the kind, subject and canonical payload together.
    return content_hash({
        "kind": str(kind),
        "subject_id": str(subject_id),
        "payload": json.loads(_canonical(payload)),
    })


def _read_analysis_rows(store_path: str | Path) -> list[dict]:
    path = Path(store_path)
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT analysis_id, kind, subject_id, ts, payload_json, "
                "payload_hash FROM analysis_runs WHERE kind='discovery' "
                "ORDER BY ts DESC, analysis_id DESC").fetchall()
    except sqlite3.Error as exc:
        raise HandoffError(f"could not read persisted discovery analyses: {exc}") from exc
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HandoffError(
                f"discovery analysis {row['analysis_id']} has invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise HandoffError(
                f"discovery analysis {row['analysis_id']} payload is not an object")
        expected = _analysis_payload_hash(
            str(row["kind"]), str(row["subject_id"]), payload)
        if str(row["payload_hash"] or "") != expected:
            raise HandoffError(
                f"discovery analysis {row['analysis_id']} payload hash mismatch")
        if str(row["analysis_id"] or "") != expected[:32]:
            raise HandoffError(
                f"discovery analysis {row['analysis_id']} identity mismatch")
        result.append({
            "analysis_id": str(row["analysis_id"]),
            "kind": str(row["kind"]),
            "subject_id": str(row["subject_id"]),
            "ts": row["ts"],
            "payload_hash": expected,
            "payload": dict(payload),
        })
    return result


def _load_result_json(result_root: str | Path, result: Mapping) -> tuple[str, Path]:
    result_hash = content_hash(result)
    root = Path(result_root)
    path = root / result_hash / "result.json"
    if not path.is_file():
        raise HandoffError(f"persisted discovery result is missing: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        recorded = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HandoffError(f"persisted discovery result is invalid: {path}") from exc
    if not isinstance(recorded, Mapping) or not _same(recorded, result):
        raise HandoffError("persisted discovery result hash/content mismatch")
    if path.parent.name != result_hash:
        raise HandoffError("persisted discovery result directory hash mismatch")
    return result_hash, path


def _validate_result(
        result: Mapping, *, result_root: str | Path,
        artifact_root: str | Path,
        expected_contract_identity: Mapping | None = None) -> dict:
    if result.get("schema") != SCHEMA:
        raise HandoffError("discovery result schema mismatch")
    if str(result.get("status") or "").upper() != "COMPLETE":
        raise HandoffError("discovery handoff requires a persisted COMPLETE result")
    if str(result.get("verdict") or "").upper() == "FALSIFIED":
        raise HandoffError("FALSIFIED discovery results cannot produce a handoff")
    if result.get("authorizes") is not False:
        raise HandoffError("discovery result authorizes an unsupported action")
    contract = result.get("contract_identity")
    if not isinstance(contract, Mapping):
        raise HandoffError("discovery result contract identity is missing")
    _full_digest("contract_identity.semantic_hash", contract.get("semantic_hash"))
    for key in ("strategy_id", "strategy_version", "variant_id"):
        if not isinstance(contract.get(key), str) or not contract[key].strip():
            raise HandoffError(f"discovery result contract identity lacks {key}")
    if expected_contract_identity is not None and not _same(
            contract, expected_contract_identity):
        raise HandoffError("discovery result source contract identity drift")
    program_payload = result.get("program")
    if not isinstance(program_payload, Mapping):
        raise HandoffError("discovery result typed program is missing")
    try:
        program = DiscoveryProgram.from_dict(program_payload)
    except (TypeError, ValueError, KeyError) as exc:
        raise HandoffError(f"discovery result typed program is invalid: {exc}") from exc
    if program.content_hash != program_payload.get("content_hash"):
        raise HandoffError("discovery result program identity mismatch")
    expected_program_identity = program_identity_hash(program, contract)
    if result.get("program_identity_hash") != expected_program_identity:
        raise HandoffError("discovery result program/contract identity mismatch")
    evidence = result.get("event_evidence")
    if not isinstance(evidence, Mapping):
        raise HandoffError("discovery result event evidence is missing")
    if result.get("event_join_digest") != evidence.get("event_join_digest"):
        raise HandoffError("discovery result event evidence digest mismatch")
    artifact_hash = _full_digest("artifact_hash", result.get("artifact_hash"))
    artifact_path = Path(artifact_root) / artifact_hash
    if not artifact_path.is_dir():
        raise HandoffError(f"typed discovery artifact is missing: {artifact_path}")
    try:
        manifest = verify_artifact(
            artifact_path, contract_identity=contract,
            evidence_context=evidence)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HandoffError(f"typed discovery artifact failed validation: {exc}") from exc
    if manifest.get("program", {}).get("content_hash") != program.content_hash:
        raise HandoffError("typed discovery artifact program differs from result")
    result_hash, result_path = _load_result_json(result_root, result)
    return {
        "result": dict(result),
        "result_hash": result_hash,
        "result_path": result_path,
        "artifact_hash": artifact_hash,
        "artifact_path": artifact_path,
        "artifact_manifest": manifest,
        "contract_identity": dict(contract),
        "event_evidence": dict(evidence),
        "program": program,
    }


def load_persisted_discovery(
        findings_store: Any, *, analysis_id: str | None = None,
        result_root: str | Path = DEFAULT_RESULT_DIR,
        artifact_root: str | Path = DEFAULT_ARTIFACT_DIR,
        expected_contract_identity: Mapping | None = None) -> dict:
    """Load and validate one persisted discovery analysis.

    With no explicit id, the newest persisted discovery row is selected.  A
    newer incomplete or falsified row is not skipped in favour of old evidence
    because that would silently hand off stale research.
    """
    store_path = getattr(findings_store, "path", findings_store)
    rows = _read_analysis_rows(store_path)
    if analysis_id:
        rows = [row for row in rows if row["analysis_id"] == str(analysis_id)]
    if not rows:
        raise HandoffError("no persisted discovery analysis is available")
    row = rows[0]
    return {
        "analysis_id": row["analysis_id"],
        "analysis_payload_hash": row["payload_hash"],
        "analysis_ts": row["ts"],
        **_validate_result(
            row["payload"], result_root=result_root,
            artifact_root=artifact_root,
            expected_contract_identity=expected_contract_identity),
    }


def build_discovery_handoff(
        evidence: Mapping, *, analysis_id: str,
        analysis_payload_hash: str) -> dict:
    """Build a non-authorizing human-review packet from verified evidence."""
    if not isinstance(evidence, Mapping):
        raise HandoffError("verified discovery evidence must be a mapping")
    result = evidence.get("result")
    if not isinstance(result, Mapping):
        raise HandoffError("verified discovery result is missing")
    result_hash = _full_digest("result_hash", evidence.get("result_hash"))
    payload_hash = _full_digest("analysis_payload_hash", analysis_payload_hash)
    contract = evidence.get("contract_identity")
    program = evidence.get("program")
    manifest = evidence.get("artifact_manifest")
    if not isinstance(contract, Mapping) or not isinstance(program, DiscoveryProgram):
        raise HandoffError("verified discovery contract/program is missing")
    if not isinstance(manifest, Mapping):
        raise HandoffError("verified discovery artifact manifest is missing")
    unsigned = {
        "schema": HANDOFF_SCHEMA,
        "authorizes": False,
        "promotion_allowed": False,
        "live_eligible": False,
        "source": {
            "analysis_id": str(analysis_id),
            "analysis_payload_hash": payload_hash,
            "result_hash": result_hash,
            "artifact_hash": str(evidence["artifact_hash"]),
            "program_identity_hash": str(result["program_identity_hash"]),
            "contract_identity": dict(contract),
            "source_contract_identity": dict(contract),
            "event_evidence": dict(evidence["event_evidence"]),
        },
        "discovery": {
            "program": program.as_dict(),
            "status": str(result["status"]),
            "verdict": str(result["verdict"]),
            "coverage": result.get("coverage"),
            "rows": result.get("rows"),
            "counterfactual": result.get("counterfactual"),
            "world_model": result.get("world_model"),
            "as_of": result.get("as_of", {}),
        },
        "requested_action": {
            "kind": "human_discovery_registration_review",
            "registration_identity": "HUMAN_DECISION_REQUIRED",
            "registry_mutation_performed": False,
            "config_mutation_performed": False,
            "code_mutation_performed": False,
            "demo_mutation_performed": False,
            "live_mutation_performed": False,
            "promotion_allowed": False,
        },
        "authority": {
            "current_tier": "T1_HYPOTHESIS",
            "approval_status": "PENDING_HUMAN_REVIEW",
            "human_review_required": True,
            "future_registration_identity": "HUMAN_DECISION_REQUIRED",
            "registry_authority": False,
            "config_authority": False,
            "code_authority": False,
            "demo_authority": False,
            "live_authority": False,
            "live_eligible": False,
            "demo_eligible": False,
            "registry_mutation_performed": False,
            "config_mutation_performed": False,
            "code_mutation_performed": False,
            "demo_mutation_performed": False,
            "live_mutation_performed": False,
        },
    }
    digest = sha256_json(unsigned)
    return {**unsigned, "content_sha256": digest,
            "handoff_id": f"sha256:{digest}"}


def validate_discovery_handoff(value: object) -> dict:
    """Verify content identity and every non-authorizing declaration."""
    if not isinstance(value, Mapping):
        raise HandoffError("discovery handoff must be an object")
    result = dict(value)
    digest = result.pop("content_sha256", None)
    handoff_id = result.pop("handoff_id", None)
    expected = sha256_json(result)
    if digest != expected or handoff_id != f"sha256:{expected}":
        raise HandoffError("discovery handoff content address does not match")
    if result.get("schema") != HANDOFF_SCHEMA:
        raise HandoffError("unsupported discovery handoff schema")
    if (result.get("authorizes") is not False
            or result.get("promotion_allowed") is not False
            or result.get("live_eligible") is not False):
        raise HandoffError("discovery handoff claims authority")
    requested = result.get("requested_action")
    authority = result.get("authority")
    if not isinstance(requested, Mapping) or not isinstance(authority, Mapping):
        raise HandoffError("discovery handoff authority envelope is missing")
    if requested.get("registration_identity") != "HUMAN_DECISION_REQUIRED":
        raise HandoffError("future registration identity must require a human")
    source = result.get("source")
    discovery = result.get("discovery")
    if not isinstance(source, Mapping) or not isinstance(discovery, Mapping):
        raise HandoffError("discovery handoff evidence envelope is missing")
    for field in ("analysis_payload_hash", "result_hash", "artifact_hash",
                  "program_identity_hash"):
        _full_digest(f"source.{field}", source.get(field))
    contract = source.get("contract_identity")
    if (not isinstance(contract, Mapping)
            or not _same(contract, source.get("source_contract_identity"))):
        raise HandoffError("discovery handoff source contract identity is invalid")
    _full_digest("source.contract_identity.semantic_hash",
                 contract.get("semantic_hash"))
    try:
        program = DiscoveryProgram.from_dict(discovery["program"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HandoffError(f"discovery handoff typed program is invalid: {exc}") from exc
    if program_identity_hash(program, contract) != source["program_identity_hash"]:
        raise HandoffError("discovery handoff program identity mismatch")
    if discovery.get("status") != "COMPLETE":
        raise HandoffError("discovery handoff does not contain COMPLETE evidence")
    if str(discovery.get("verdict") or "").upper() == "FALSIFIED":
        raise HandoffError("FALSIFIED discovery results cannot be handed off")
    for key in (
            "registry_mutation_performed", "config_mutation_performed",
            "code_mutation_performed", "demo_mutation_performed",
            "live_mutation_performed", "promotion_allowed"):
        if requested.get(key) is not False:
            raise HandoffError(f"discovery handoff claims {key}")
    if (authority.get("approval_status") != "PENDING_HUMAN_REVIEW"
            or authority.get("human_review_required") is not True
            or authority.get("future_registration_identity")
            != "HUMAN_DECISION_REQUIRED"):
        raise HandoffError("discovery handoff weakens human approval boundary")
    for key in ("registry_authority", "config_authority", "code_authority",
                "demo_authority", "live_authority", "live_eligible",
                "demo_eligible", "registry_mutation_performed",
                "config_mutation_performed", "code_mutation_performed",
                "demo_mutation_performed", "live_mutation_performed"):
        if authority.get(key) is not False:
            raise HandoffError(f"discovery handoff grants {key}")
    return dict(value)


def render_discovery_handoff(value: object) -> str:
    valid = validate_discovery_handoff(value)
    return json.dumps(valid, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def write_discovery_handoff(
        handoff: Mapping, output_dir: str | Path = DEFAULT_HANDOFF_DIR) -> dict:
    """Atomically write one idempotent content-addressed handoff."""
    valid = validate_discovery_handoff(handoff)
    digest = str(valid["content_sha256"])
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"sha256-{digest}.json"
    content = render_discovery_handoff(valid)
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            validate_discovery_handoff(existing)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise HandoffError(f"existing handoff is invalid: {target}: {exc}") from exc
        if not _same(existing, valid):
            raise HandoffError(f"content-address collision at {target}")
        return {"status": "EXISTING", "path": str(target),
                "handoff_id": valid["handoff_id"], "live_eligible": False}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=out,
                prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"status": "CREATED", "path": str(target),
            "handoff_id": valid["handoff_id"], "live_eligible": False}


# Short aliases keep the boundary convenient for offline callers while the
# descriptive names remain the documented API.
build_handoff = build_discovery_handoff
validate_handoff = validate_discovery_handoff
write_handoff = write_discovery_handoff


__all__ = [
    "HANDOFF_SCHEMA", "DEFAULT_RESULT_DIR", "DEFAULT_ARTIFACT_DIR",
    "DEFAULT_HANDOFF_DIR", "HandoffError", "load_persisted_discovery",
    "build_discovery_handoff", "validate_discovery_handoff",
    "render_discovery_handoff", "write_discovery_handoff",
    "DiscoveryHandoffError", "build_handoff", "validate_handoff",
    "write_handoff",
]
