"""Deterministic, public-safe exports for one paper-research epoch.

The paper epoch store is an operational control-plane ledger.  This module
provides a deliberately small read path for sharing that ledger's current
contents as canonical JSONL.  It does not manufacture observer/signal fields
or market-data provenance: fields such as session dates, signal times,
symbols, sides, brackets, quotes, exits, and R values are absent unless the
store itself has one of those fields (the shipped store does not).

An export is content addressed by the exact UTF-8 bytes written.  Creation is
atomic and immutable (``O_EXCL``); a repeat succeeds only when the existing
file has byte-identical content.  The source store is opened read-only when a
path is supplied, and no source path or account/runtime fingerprint is placed
in the public artifact.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Callable, Mapping

from .paper_epoch import PaperEpochStore
from .proof import send_webhook


PAPER_EPOCH_EXPORT_SCHEMA = "paper-epoch-operational-export.v1"
DEFAULT_OUTPUT_ROOT = Path("research/results/paper-epochs")
_SAFE_EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_FILENAME_EPOCH_PREFIX_LENGTH = 96


class PaperEpochExportError(RuntimeError):
    """The export could not be built or its immutable artifact was altered."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PaperEpochExportError("export contains non-finite or non-JSON data") from exc


def export_digest(data: bytes) -> str:
    """Return the SHA-256 digest of canonical export bytes."""
    return hashlib.sha256(data).hexdigest()


def _public_cohort(epoch: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Retain cohort labels while excluding account/runtime fingerprints."""
    cohort = epoch.get("cohort")
    if not isinstance(cohort, list):
        raise PaperEpochExportError("paper epoch cohort is not a list")
    result: list[dict[str, Any]] = []
    for member in cohort:
        if not isinstance(member, Mapping):
            raise PaperEpochExportError("paper epoch cohort member is not an object")
        # Member labels, role, mode, and frozen manifest binding are useful
        # operational context.  The account/runtime identity values are
        # intentionally not copied into a public export.
        allowed = ("member_id", "role", "runtime_mode", "manifest_digest")
        result.append({key: member[key] for key in allowed if key in member})
    result.sort(key=lambda item: str(item.get("member_id", "")))
    return result


def _public_epoch(epoch: Mapping[str, Any], integrity: Mapping[str, Any],
                  outcome_count: int) -> dict[str, Any]:
    required = ("epoch_id", "ordinal", "status", "manifest", "manifest_digest",
                "cohort_digest", "policy")
    missing = [key for key in required if key not in epoch]
    if missing:
        raise PaperEpochExportError(
            "paper epoch is missing required fields: " + ", ".join(missing))
    manifest = epoch["manifest"]
    if not isinstance(manifest, Mapping):
        raise PaperEpochExportError("paper epoch manifest is not an object")
    policy = epoch["policy"]
    if not isinstance(policy, Mapping):
        raise PaperEpochExportError("paper epoch policy is not an object")
    # Keep an allowlist rather than copying an open-ended policy mapping.  If
    # the store schema grows a local path or identity field later, it must not
    # silently become public metadata.
    safe_policy = {
        key: policy[key] for key in (
            "paper_success_can_promote", "paper_counts_as_alpha",
            "alpha_evidence_count", "promotion_authority")
        if key in policy
    }
    safe_integrity = {
        key: integrity[key] for key in (
            "schema", "ok", "schema_version", "audit_events", "audit_head")
        if key in integrity
    }
    if safe_integrity.get("ok") is not True:
        raise PaperEpochExportError("paper epoch integrity verification did not pass")
    safe_manifest = {
        key: manifest[key] for key in (
            "realtime_stream_digest", "data_window_digest", "config_digest",
            "code_digest", "cost_digest", "risk_digest",
            "runtime_llm_adaptation", "halt_on_operational_failure")
        if key in manifest
    }
    return {
        "schema": PAPER_EPOCH_EXPORT_SCHEMA,
        "record_type": "epoch_manifest",
        "kind": "manifest",
        "epoch_id": epoch["epoch_id"],
        "ordinal": epoch["ordinal"],
        "predecessor_epoch_id": epoch.get("predecessor_epoch_id"),
        "status": epoch["status"],
        "manifest": safe_manifest,
        "manifest_digest": epoch["manifest_digest"],
        "cohort_digest": epoch["cohort_digest"],
        "cohort": _public_cohort(epoch),
        "policy": safe_policy,
        # Export semantics are intentionally separate from the frozen epoch
        # policy, so no generated claim is mistaken for a stored observation.
        "export": {
            "kind": "operational",
            "observer_signal_feed": False,
            "alpha_authority": False,
            "promotion_authority": False,
        },
        "integrity": safe_integrity,
        "audit_head": safe_integrity.get("audit_head"),
        "record_count": int(outcome_count),
    }


def _outcome_record(epoch_id: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap the exact stored outcome without adding market-observer fields."""
    if str(outcome.get("epoch_id")) != epoch_id:
        raise PaperEpochExportError("outcome belongs to a different epoch")
    # ``outcome`` is sourced from PaperEpochStore.outcomes().  Copying it as a
    # mapping preserves all currently stored evidence (including row digests)
    # and intentionally does not add absent signal/market fields.
    record = dict(outcome)
    record["schema"] = PAPER_EPOCH_EXPORT_SCHEMA
    record["record_type"] = "outcome"
    record["kind"] = "outcome"
    return record


def _build_export_records(store: PaperEpochStore,
                          epoch_id: str) -> list[dict[str, Any]]:
    """Build canonical records from an already isolated read snapshot."""
    integrity = store.verify_integrity()
    epoch = store.epoch(epoch_id)
    outcomes = store.outcomes(epoch_id)
    summary = store.operational_summary(epoch_id)
    records = [_public_epoch(epoch, integrity, len(outcomes))]
    records.append({
        "schema": PAPER_EPOCH_EXPORT_SCHEMA,
        "record_type": "operational_summary",
        "kind": "summary",
        "epoch_id": epoch_id,
        "summary": dict(summary),
    })
    records.extend(_outcome_record(epoch_id, outcome) for outcome in outcomes)
    return records


def canonical_jsonl(records: list[Mapping[str, Any]]) -> bytes:
    """Encode records as deterministic UTF-8 JSONL bytes with one final LF."""
    if not records:
        raise PaperEpochExportError("an export must contain at least a manifest")
    return ("\n".join(_canonical_json(dict(record)) for record in records) + "\n").encode(
        "utf-8")


@dataclass(frozen=True)
class PaperEpochExportResult:
    """Structured result returned by :func:`write_paper_epoch_export`."""

    path: Path
    digest: str
    created: bool
    record_count: int
    webhook: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PAPER_EPOCH_EXPORT_SCHEMA,
            "path": str(self.path),
            "digest": self.digest,
            "created": self.created,
            "record_count": self.record_count,
            "webhook": self.webhook,
        }

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return str(self.path)


@contextmanager
def _snapshot_store(store: PaperEpochStore | str | Path):
    """Yield a read-only SQLite backup representing one coherent snapshot.

    The store facade opens a fresh connection for each read method.  A writer
    can therefore append between ``verify_integrity()``, ``epoch()``, and
    ``outcomes()`` unless those reads share one SQLite snapshot.  SQLite's
    online backup API captures a transactionally consistent view (including a
    WAL database) without mutating the source; all export reads then target
    that temporary immutable copy.
    """
    source = store if isinstance(store, PaperEpochStore) else PaperEpochStore(
        store, readonly=True)
    source_path = Path(source.path).resolve()
    if not source_path.is_file():
        raise PaperEpochExportError(f"paper epoch store is unavailable: {source_path}")
    uri = source_path.as_uri() + "?mode=ro"
    with tempfile.TemporaryDirectory(prefix="paper-epoch-export-") as directory:
        snapshot_path = Path(directory) / "snapshot.sqlite3"
        try:
            with closing(sqlite3.connect(
                    uri, uri=True, timeout=30)) as source_db, \
                    closing(sqlite3.connect(
                        str(snapshot_path), timeout=30)) as snapshot_db:
                source_db.backup(snapshot_db)
        except sqlite3.Error as exc:
            raise PaperEpochExportError("could not snapshot paper epoch store") from exc
        yield PaperEpochStore(snapshot_path, readonly=True)


def build_export_records(
        store: PaperEpochStore | str | Path,
        epoch_id: str) -> list[dict[str, Any]]:
    """Build canonical records from one coherent, integrity-checked snapshot."""
    with _snapshot_store(store) as snapshot:
        try:
            return _build_export_records(snapshot, epoch_id)
        except KeyError as exc:
            raise PaperEpochExportError(
                f"unknown paper epoch: {epoch_id}") from exc


def write_paper_epoch_export(
    store: PaperEpochStore | str | Path,
    epoch_id: str,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    *,
    webhook_url: str | None = None,
    webhook_sender: Callable[..., Any] | None = None,
    webhook_timeout_seconds: float = 5.0,
) -> PaperEpochExportResult:
    """Write one immutable content-addressed operational paper-epoch export.

    Supplying a SQLite path opens it read-only.  A store object is accepted for
    library callers, but no mutating method is called.  Webhook delivery is
    best effort and occurs only when a new artifact is created, matching the
    existing :func:`research.proof.send_webhook` contract.
    """
    if not isinstance(epoch_id, str) or not _SAFE_EPOCH_RE.fullmatch(epoch_id):
        raise PaperEpochExportError("epoch_id must be a safe non-empty token")
    records = build_export_records(store, epoch_id)
    data = canonical_jsonl(records)
    digest = export_digest(data)
    root = Path(output_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PaperEpochExportError(
            f"cannot create paper epoch export directory: {root}") from exc
    safe_epoch = (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", epoch_id).strip("._") or "epoch"
    )[:_FILENAME_EPOCH_PREFIX_LENGTH]
    target = root / f"paper-epoch-{safe_epoch}-{digest}.jsonl"
    created = True
    descriptor = -1
    try:
        descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        created = False
        if target.is_symlink():
            raise PaperEpochExportError(
                f"existing export path is a symlink, refusing to trust it: {target}")
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise PaperEpochExportError(f"existing export is unreadable: {target}") from exc
        if existing != data:
            raise PaperEpochExportError(
                f"existing export does not match canonical bytes: {target}")
    except OSError as exc:
        raise PaperEpochExportError(
            f"cannot create paper epoch export: {target}") from exc
    else:
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor != -1:
                os.close(descriptor)

    webhook = None
    if webhook_url and created:
        manifest = records[0]
        # ``send_webhook`` has a compatibility allowlist.  The generic
        # candidate_id/vehicle mapping is explicit here and the artifact itself
        # carries the unambiguous operational-export schema.
        webhook = send_webhook(
            webhook_url,
            {
                "candidate_id": epoch_id,
                "vehicle": "paper_epoch_operational",
                "status": manifest.get("status"),
                "payload_hash": digest,
                "artifact": target.name,
            },
            sender=webhook_sender,
            timeout_seconds=webhook_timeout_seconds,
        )
    return PaperEpochExportResult(
        path=target,
        digest=digest,
        created=created,
        record_count=max(0, len(records) - 2),
        webhook=webhook,
    )


# A concise alias for callers that use ``export_*`` naming.
export_paper_epoch = write_paper_epoch_export


__all__ = [
    "PAPER_EPOCH_EXPORT_SCHEMA", "DEFAULT_OUTPUT_ROOT", "PaperEpochExportError",
    "PaperEpochExportResult", "export_digest", "canonical_jsonl",
    "build_export_records", "write_paper_epoch_export", "export_paper_epoch",
]
