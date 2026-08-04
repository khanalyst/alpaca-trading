"""Append-only, verified research backups.

SQLite sources are always captured with SQLite's online backup API. Regular
recorder, immutable market-snapshot, manifest, and durable result files are
copied into a new versioned directory and covered by a SHA-256 manifest.
Nothing here prunes or replaces a prior backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .findings import (
    FindingsStore,
    _content_hash,
    _positive_external_target_evidence,
)


REPO = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = REPO / "runtime" / "backups" / "research"
DEFAULT_RUNTIME_RESEARCH = REPO / "runtime" / "research"
DEFAULT_RESULTS = REPO / "research" / "results"
MANIFEST_NAME = "MANIFEST.json"
MANIFEST_CHECKSUM_NAME = "MANIFEST.sha256"


class BackupError(RuntimeError):
    """A backup was not created and verified successfully."""

    def __init__(self, message: str, result: dict | None = None) -> None:
        super().__init__(message)
        self.result = result or {"ok": False, "errors": [message]}


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
        allow_nan=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ")


def _manifest_content_id(manifest: dict) -> str:
    """Address the captured bytes independently of invocation metadata."""
    files = [{
        "archive_path": item.get("archive_path"),
        "kind": item.get("kind"),
        "size_bytes": item.get("size_bytes"),
        "sha256": item.get("sha256"),
        "verification": item.get("verification") or {},
    } for item in manifest.get("files") or []]
    return hashlib.sha256(_canonical({
        "schema": manifest.get("schema"),
        "files": files,
    }).encode("utf-8")).hexdigest()


def _sqlite_verification(path: Path) -> dict:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        integrity_rows = [str(row[0]) for row in conn.execute(
            "PRAGMA integrity_check").fetchall()]
        foreign_keys = [list(row) for row in conn.execute(
            "PRAGMA foreign_key_check").fetchall()]
    ok = integrity_rows == ["ok"] and not foreign_keys
    return {
        "ok": ok,
        "integrity_check": integrity_rows,
        "foreign_key_check": foreign_keys,
    }


def _online_sqlite_backup(source: Path, destination: Path) -> None:
    """Capture a WAL-consistent snapshot without copying live DB bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(
            f"file:{source}?mode=ro", uri=True, timeout=30) as source_conn:
        with sqlite3.connect(destination, timeout=30) as destination_conn:
            source_conn.backup(destination_conn)


def _looks_like_sqlite(path: Path) -> bool:
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _copy_stable(source: Path, destination: Path) -> None:
    """Copy a regular file, refusing a snapshot that changed mid-read."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        before = source.stat()
        shutil.copyfile(source, destination)
        after = source.stat()
        if ((before.st_size, before.st_mtime_ns, before.st_ino)
                == (after.st_size, after.st_mtime_ns, after.st_ino)):
            return
    raise BackupError(f"source changed repeatedly while backing up: {source}")


def _capture_file(source: Path, destination: Path) -> tuple[str, dict]:
    if _looks_like_sqlite(source):
        _online_sqlite_backup(source, destination)
        return "sqlite", _sqlite_verification(destination)
    _copy_stable(source, destination)
    return "file", {}


def _regular_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
        and not path.name.endswith(("-wal", "-shm", "-journal")))


def _immutable_snapshot_files(root: Path) -> list[Path]:
    """Return files from completed downloader snapshots only.

    The downloader makes a timestamped directory immutable by removing its
    in-progress marker and writing ``manifest.json`` last. Partial downloads
    are intentionally excluded so a retry cannot turn an incomplete tree into
    apparently durable tournament input.
    """
    if not root.is_dir():
        return []
    files: list[Path] = []
    for snapshot in sorted(root.iterdir()):
        manifest = snapshot / "manifest.json"
        in_progress = snapshot / ".download-in-progress"
        if (not snapshot.is_dir() or snapshot.is_symlink()
                or not manifest.is_file() or manifest.is_symlink()
                or in_progress.exists()):
            continue
        files.extend(_regular_files(snapshot))
    return sorted(files)


def _source_entries(
        store_path: Path, journal_path: Path | None,
        runtime_research_root: Path, results_root: Path) -> list[tuple[Path, Path]]:
    entries: list[tuple[Path, Path]] = [
        (store_path, Path("sqlite") / "findings.db"),
    ]
    if journal_path is not None and journal_path.is_file():
        entries.append((journal_path, Path("sqlite") / "journal.db"))

    recorded = runtime_research_root / "recorded"
    for path in _regular_files(recorded):
        entries.append((
            path, Path("files/runtime/research/recorded")
            / path.relative_to(recorded)))

    snapshots = runtime_research_root / "snapshots"
    for path in _immutable_snapshot_files(snapshots):
        entries.append((
            path, Path("files/runtime/research/snapshots")
            / path.relative_to(snapshots)))

    if runtime_research_root.is_dir():
        for path in _regular_files(runtime_research_root):
            relative = path.relative_to(runtime_research_root)
            if relative.parts and relative.parts[0] == "snapshots":
                continue
            name = path.name.lower()
            if ("manifest" in name and path.suffix.lower() == ".json") \
                    or relative == Path("forward_evidence.json"):
                entries.append((
                    path, Path("files/runtime/research") / relative))

    for path in _regular_files(results_root):
        entries.append((
            path, Path("files/research/results")
            / path.relative_to(results_root)))

    by_archive: dict[str, tuple[Path, Path]] = {}
    for source, archive in entries:
        by_archive[archive.as_posix()] = (source, archive)
    return [by_archive[key] for key in sorted(by_archive)]


def _assert_target_outside_sources(target: Path, source_roots: list[Path]) -> None:
    resolved = target.resolve()
    for root in source_roots:
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        raise BackupError(
            f"backup target {target} is inside included source tree {root}")


def _device_id(path: Path) -> int:
    """Return the mounted filesystem/device identity used for classification."""
    return int(path.stat().st_dev)


def _existing_anchor(path: Path) -> Path:
    """Find an existing path whose device represents a not-yet-created source."""
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _classify_target(
        target: Path, *, configured: bool,
        source_paths: list[Path]) -> tuple[str, dict]:
    """Classify a target as external only with positive device evidence."""
    evidence: dict = {
        "configured": bool(configured),
        "external_proven": False,
        "method": "st_dev" if configured else "local_default_policy",
        "target_exists": target.exists(),
        "target_is_directory": target.is_dir(),
        "target_device": None,
        "source_devices": [],
        "source_device_paths": [],
    }
    if not configured:
        evidence["reason"] = "repository-local default is always local"
        return "local_default", evidence
    if not evidence["target_exists"]:
        evidence["reason"] = "configured target does not exist"
        return "configured_local", evidence
    if not evidence["target_is_directory"]:
        evidence["reason"] = "configured target is not a directory"
        return "configured_local", evidence

    try:
        target_device = _device_id(target)
        seen: set[tuple[str, int]] = set()
        source_device_paths = []
        for source in source_paths:
            anchor = _existing_anchor(source)
            device = _device_id(anchor)
            key = (str(anchor.resolve()), device)
            if key in seen:
                continue
            seen.add(key)
            source_device_paths.append({
                "path": str(source),
                "device_path": str(anchor),
                "st_dev": device,
            })
        source_devices = sorted({
            int(item["st_dev"]) for item in source_device_paths})
        evidence.update({
            "target_device": target_device,
            "source_devices": source_devices,
            "source_device_paths": source_device_paths,
        })
    except OSError as exc:
        evidence["reason"] = f"device classification failed: {exc}"
        evidence["classification_error"] = str(exc)
        return "configured_local", evidence

    if target_device not in source_devices:
        evidence["external_proven"] = True
        evidence["reason"] = (
            "target st_dev differs from every repository/source device")
        return "external_mounted", evidence
    evidence["reason"] = (
        "target shares st_dev with the repository or another source")
    return "configured_local", evidence


def verify_backup(path: str | Path) -> dict:
    """Verify manifest, payload checksums, and every SQLite snapshot."""
    root = Path(path)
    errors: list[str] = []
    manifest_path = root / MANIFEST_NAME
    checksum_path = root / MANIFEST_CHECKSUM_NAME
    manifest: dict = {}
    manifest_sha256 = None
    try:
        manifest_sha256 = _sha256(manifest_path)
        expected_line = checksum_path.read_text(encoding="utf-8").strip()
        expected = expected_line.split()[0] if expected_line else ""
        if expected != manifest_sha256:
            errors.append("manifest checksum mismatch")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError) as exc:
        errors.append(f"manifest unreadable: {exc}")

    content_id = manifest.get("content_id") if isinstance(manifest, dict) else None
    if manifest:
        calculated = _manifest_content_id(manifest)
        if content_id != calculated:
            errors.append("manifest content identity mismatch")
        if content_id and content_id[:16] not in root.name:
            errors.append("backup directory does not contain its content identity")
        kind = manifest.get("target_kind")
        evidence = manifest.get("target_evidence")
        if (kind == "external_mounted"
                and not _positive_external_target_evidence(evidence)):
            errors.append("external target classification evidence is invalid")
        if (kind != "external_mounted" and isinstance(evidence, dict)
                and evidence.get("external_proven") is True):
            errors.append("local target carries inconsistent external evidence")

    checked = 0
    sqlite_checks: dict[str, dict] = {}
    for item in manifest.get("files", []) if manifest else []:
        archive_path = str(item.get("archive_path") or "")
        relative = Path(archive_path)
        if (not archive_path or relative.is_absolute()
                or ".." in relative.parts):
            errors.append(f"unsafe archive path: {archive_path!r}")
            continue
        file_path = root / relative
        try:
            size = file_path.stat().st_size
            digest = _sha256(file_path)
        except OSError as exc:
            errors.append(f"missing backup file {archive_path}: {exc}")
            continue
        if size != int(item.get("size_bytes", -1)):
            errors.append(f"size mismatch: {archive_path}")
        if digest != item.get("sha256"):
            errors.append(f"checksum mismatch: {archive_path}")
        if item.get("kind") == "sqlite":
            try:
                check = _sqlite_verification(file_path)
            except sqlite3.Error as exc:
                check = {"ok": False, "error": str(exc)}
            sqlite_checks[archive_path] = check
            if not check.get("ok"):
                errors.append(f"SQLite verification failed: {archive_path}")
        checked += 1

    return {
        "ok": not errors,
        "backup_id": manifest.get("backup_id") if manifest else None,
        "content_id": content_id,
        "target_kind": manifest.get("target_kind") if manifest else None,
        "target_evidence": (
            manifest.get("target_evidence") if manifest else None),
        "manifest_sha256": manifest_sha256,
        "files_verified": checked,
        "sqlite_checks": sqlite_checks,
        "errors": errors,
    }


def create_backup(
        *, store_path: str | Path, target: str | Path | None = None,
        target_configured: bool = False,
        journal_path: str | Path | None = None,
        runtime_research_root: str | Path = DEFAULT_RUNTIME_RESEARCH,
        results_root: str | Path = DEFAULT_RESULTS,
        require_external: bool = False,
        command: list[str] | None = None,
        options: dict | None = None) -> dict:
    """Create one new backup directory, verify it, and append its history."""
    store_path = Path(store_path)
    journal = Path(journal_path) if journal_path is not None else None
    runtime_root = Path(runtime_research_root)
    results = Path(results_root)
    target_root = Path(target) if target is not None else DEFAULT_TARGET
    configured_target = bool(target_configured or target is not None)
    classification_sources = [
        REPO, store_path, runtime_root, results,
        *([journal] if journal is not None else []),
    ]
    target_kind, target_evidence = _classify_target(
        target_root, configured=configured_target,
        source_paths=classification_sources)
    started_ts = time.time()
    request = {
        "store_path": str(store_path),
        "journal_path": str(journal) if journal is not None else None,
        "runtime_research_root": str(runtime_root),
        "results_root": str(results),
        "target_root": str(target_root),
        "target_kind": target_kind,
        "target_evidence": target_evidence,
        "require_external": bool(require_external),
        "command": command or [],
        "options": options or {},
    }
    request_content_id = _content_hash(request)
    backup_id = _content_hash({
        "request_content_id": request_content_id,
        "started_ns": time.time_ns(),
        "nonce": uuid.uuid4().hex,
    })
    source_paths = [
        str(store_path), str(runtime_root), str(results),
        str(journal) if journal is not None else None,
    ]
    source_paths = [path for path in source_paths if path is not None]
    store = FindingsStore(store_path)
    store.start_backup_run({
        "backup_id": backup_id,
        "request_content_id": request_content_id,
        "started_ts": started_ts,
        "target_root": str(target_root),
        "target_kind": target_kind,
        "target_evidence": target_evidence,
        "require_external": require_external,
        "command": command or [],
        "options": options or {},
        "source_paths": source_paths,
    })

    backup_path = target_root
    try:
        if configured_target:
            if not target_root.exists():
                raise BackupError(
                    f"configured backup target does not exist: "
                    f"{target_root}. Mount/provision it first; no local "
                    "directory was substituted.")
            if not target_root.is_dir():
                raise BackupError(
                    f"configured backup target is not a directory: "
                    f"{target_root}")
            if target_evidence.get("classification_error"):
                raise BackupError(
                    "configured backup target could not be classified by "
                    f"device: {target_evidence['classification_error']}")
        if require_external and target_kind != "external_mounted":
            raise BackupError(
                "external backup required, but the target is not proven to "
                "be on a different mounted filesystem/device")
        if not configured_target:
            target_root.mkdir(parents=True, exist_ok=True)

        _assert_target_outside_sources(
            target_root, [runtime_root, results])
        if not store_path.is_file():
            raise BackupError(f"findings store does not exist: {store_path}")

        stamp = _stamp(started_ts)
        staging = target_root / f".incomplete-{stamp}-{backup_id[:12]}"
        staging.mkdir(parents=False, exist_ok=False)
        backup_path = staging
        files = []
        for source, archive in _source_entries(
                store_path, journal, runtime_root, results):
            destination = staging / archive
            kind, verification = _capture_file(source, destination)
            files.append({
                "archive_path": archive.as_posix(),
                "source_path": str(source),
                "kind": kind,
                "size_bytes": destination.stat().st_size,
                "sha256": _sha256(destination),
                "verification": verification,
            })

        manifest_without_identity = {
            "schema": "research-backup.v1",
            "backup_id": backup_id,
            "request_content_id": request_content_id,
            "created_ts": started_ts,
            "created_utc": datetime.fromtimestamp(
                started_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "target_kind": target_kind,
            "target_evidence": target_evidence,
            "source_paths": source_paths,
            "files": files,
        }
        content_id = _manifest_content_id(manifest_without_identity)
        manifest = {**manifest_without_identity, "content_id": content_id}
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        manifest_sha256 = _sha256(manifest_path)
        (staging / MANIFEST_CHECKSUM_NAME).write_text(
            f"{manifest_sha256}  {MANIFEST_NAME}\n", encoding="utf-8")

        final = target_root / (
            f"{stamp}-{content_id[:16]}-{backup_id[:12]}")
        staging.rename(final)
        backup_path = final
        if (target_kind == "external_mounted"
                and _device_id(target_root)
                != target_evidence["target_device"]):
            raise BackupError(
                "external target device changed while the backup was written")
        result = verify_backup(final)
        ended_ts = time.time()
        if not result["ok"]:
            store.finish_backup_run(
                backup_id, "FAILED", ended_ts=ended_ts,
                backup_path=final, files=files, detail=result)
            raise BackupError("backup verification failed", result)
        store.finish_backup_run(
            backup_id, "VERIFIED", ended_ts=ended_ts,
            backup_path=final, files=files, detail=result)
        return {
            **result,
            "backup_id": backup_id,
            "backup_path": str(final),
            "target_kind": target_kind,
            "target_evidence": target_evidence,
        }
    except Exception as exc:
        if isinstance(exc, BackupError) and exc.result.get("recorded"):
            raise
        result = (dict(exc.result) if isinstance(exc, BackupError)
                  else {"ok": False, "errors": [str(exc)]})
        result.update({
            "backup_id": backup_id,
            "backup_path": str(backup_path),
            "target_kind": target_kind,
            "target_evidence": target_evidence,
        })
        try:
            store.finish_backup_run(
                backup_id, "FAILED", ended_ts=time.time(),
                backup_path=backup_path, detail=result)
            result["recorded"] = True
        except ValueError:
            # Preserve any verification failure recorded by an earlier check.
            result["recorded"] = True
        raise BackupError(str(exc), result) from exc


def verify_and_record(
        path: str | Path, *, store_path: str | Path) -> dict:
    """Verify an existing backup and append the attempt to FindingsStore."""
    result = verify_backup(path)
    FindingsStore(store_path).record_backup_verification(
        result.get("backup_id"), Path(path),
        "VERIFIED" if result["ok"] else "FAILED", result)
    return result
