#!/usr/bin/env python3
"""Create and verify sealed, bounded views of the recorder corpus.

The recorder corpus is append-only, but it remains writable while research is
running.  A snapshot is a small independent dataset containing a bounded set
of session partitions, their provenance/calendar markers, and the aggregate
recorder index.  Creation holds the same recorder lock as forward recording
and backfill for the entire streamed copy and manifest operation.  Readers
must verify the sealed directory before passing it to the research parser.

This module never contacts a broker.  Copying is deliberately streamed so a
large partition is not materialized in memory, and the output is published by
an atomic rename within the destination parent filesystem.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy.recorder import corpus_write_lock  # noqa: E402


SNAPSHOT_SCHEMA = "research-snapshot.v1"
MANIFEST_NAME = ".research-snapshot.json"
RECORDER_INDEX_NAME = ".recorder-index.json"
SESSIONS_NAME = "sessions"
SOURCE_SUFFIX = ".source.json"
CALENDAR_SUFFIX = ".calendar.json"
PARTITION_RE = re.compile(r"market-(\d{4}-\d{2}-\d{2})\.csv\Z")
IDENTITY_DOMAIN = b"alpaca-research-sealed-snapshot.v1\0"
COPY_CHUNK_SIZE = 1024 * 1024
MANIFEST_MAX_BYTES = 1024 * 1024


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be created or trusted."""


def _absolute(path: os.PathLike[str] | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise SnapshotError(f"{label} is unavailable: {path}: {exc}") from exc
    if stat.S_ISLNK(value.st_mode):
        raise SnapshotError(f"{label} must not be a symlink: {path}")
    return value


def _require_directory(path: Path, *, label: str) -> None:
    value = _lstat(path, label=label)
    if not stat.S_ISDIR(value.st_mode):
        raise SnapshotError(f"{label} must be a directory: {path}")


def _require_regular(path: Path, *, label: str) -> os.stat_result:
    value = _lstat(path, label=label)
    if not stat.S_ISREG(value.st_mode):
        raise SnapshotError(f"{label} must be a regular file: {path}")
    return value


def _ensure_tree_no_symlinks(root: Path, path: Path, *, label: str) -> None:
    """Reject symlinks in the part of ``path`` below ``root``."""
    root = _absolute(root)
    path = _absolute(path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SnapshotError(f"{label} escapes its root: {path}") from exc
    current = root
    _lstat(current, label=label)
    for component in relative.parts:
        current /= component
        _lstat(current, label=label)


def _canonical_partition_name(name: str) -> bool:
    match = PARTITION_RE.fullmatch(name)
    if match is None:
        return False
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date().isoformat() == match.group(1)
    except ValueError:
        return False


def _selected_partitions(recorded_root: Path, session_window: int,
                         end_session: str | None = None) -> list[Path]:
    if not isinstance(session_window, int) or isinstance(session_window, bool):
        raise SnapshotError("session_window must be a positive integer")
    if session_window <= 0:
        raise SnapshotError(
            "session_window must be a positive integer; refusing an unbounded snapshot")
    _require_directory(recorded_root, label="recorder root")
    sessions = recorded_root / SESSIONS_NAME
    _require_directory(sessions, label="recorder sessions directory")
    selected: list[Path] = []
    try:
        entries = sorted(sessions.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise SnapshotError(f"cannot list recorder sessions: {exc}") from exc
    for path in entries:
        if not path.name.startswith("market-"):
            continue
        # A matching symlink is corruption, not an irrelevant file that can be
        # silently skipped.
        if PARTITION_RE.fullmatch(path.name):
            _require_regular(path, label="recorder partition")
            selected.append(path)
    if not selected:
        raise SnapshotError("no recorder session partitions are available")
    if end_session is not None:
        try:
            if datetime.strptime(end_session, "%Y-%m-%d").date().isoformat() != end_session:
                raise ValueError
        except ValueError as exc:
            raise SnapshotError("end_session must be an ISO session date") from exc
        selected = [p for p in selected if p.name[7:17] <= end_session]
    if not selected:
        raise SnapshotError("no partitions are available in the requested window")
    return selected[-session_window:]


def _relative_files(recorded_root: Path, partitions: Iterable[Path]) -> list[tuple[Path, str]]:
    """Return required files, in manifest order, without following symlinks."""
    files: list[tuple[Path, str]] = []
    index = recorded_root / RECORDER_INDEX_NAME
    _require_regular(index, label="recorder aggregate index")
    files.append((index, RECORDER_INDEX_NAME))
    for partition in partitions:
        relative = f"{SESSIONS_NAME}/{partition.name}"
        files.append((partition, relative))
        for suffix in (SOURCE_SUFFIX, CALENDAR_SUFFIX):
            sidecar = partition.with_name(partition.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _require_regular(sidecar, label="recorder partition sidecar")
                files.append((sidecar, f"{SESSIONS_NAME}/{sidecar.name}"))
    return files


def _copy_and_hash(source: Path, target: Path) -> dict[str, Any]:
    source_stat = _require_regular(source, label="snapshot source")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        with source.open("rb") as source_handle, target.open("xb") as target_handle:
            while True:
                chunk = source_handle.read(COPY_CHUNK_SIZE)
                if not chunk:
                    break
                target_handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    except OSError as exc:
        raise SnapshotError(f"cannot copy snapshot source {source}: {exc}") from exc
    after = _require_regular(source, label="snapshot source")
    if (getattr(source_stat, "st_dev", None), getattr(source_stat, "st_ino", None),
            source_stat.st_size, source_stat.st_mtime_ns) != (
                getattr(after, "st_dev", None), getattr(after, "st_ino", None),
                after.st_size, after.st_mtime_ns):
        raise SnapshotError(f"recorder source changed while snapshotting: {source}")
    return {"path": target.name, "size": total, "sha256": digest.hexdigest()}


def _hash_file(path: Path, *, label: str) -> tuple[int, str]:
    _require_regular(path, label=label)
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(COPY_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except OSError as exc:
        raise SnapshotError(f"cannot hash {label}: {path}: {exc}") from exc
    return total, digest.hexdigest()


def _identity_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": manifest["schema"],
        "session_window": manifest["session_window"],
        "partitions": manifest["partitions"],
        "files": manifest["files"],
    }


def _identity(manifest: dict[str, Any]) -> str:
    encoded = json.dumps(_identity_payload(manifest), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(IDENTITY_DOMAIN + encoded).hexdigest()


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_manifest_bytes(manifest))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SnapshotError(f"cannot publish snapshot manifest: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SnapshotError(f"cannot fsync snapshot directory {path}: {exc}") from exc


def _validate_sidecar(path: Path, *, partition_name: str, suffix: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"invalid recorder sidecar {path}") from exc
    if not isinstance(payload, dict):
        raise SnapshotError(f"invalid recorder sidecar {path}")
    expected_schema = ("recorder-partition-source.v1" if suffix == SOURCE_SUFFIX
                      else "recorder-partition-calendar.v1")
    if (payload.get("schema") != expected_schema or
            payload.get("partition") != partition_name):
        raise SnapshotError(f"recorder sidecar does not bind {partition_name}: {path}")
    if suffix == SOURCE_SUFFIX and payload.get("source_mode") != "historical_backfill":
        raise SnapshotError(f"unsupported recorder source marker: {path}")
    if suffix == CALENDAR_SUFFIX and payload.get("source") != "alpaca_calendar":
        raise SnapshotError(f"unsupported recorder calendar marker: {path}")


def _expected_paths(manifest: dict[str, Any]) -> set[str]:
    return {str(item.get("path")) for item in manifest["files"]}


def _validate_manifest_shape(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema") != SNAPSHOT_SCHEMA:
        raise SnapshotError("snapshot manifest has an unsupported schema")
    allowed = {"schema", "created_at", "session_window", "partitions",
               "files", "bytes", "identity", "immutable"}
    if set(manifest) != allowed:
        raise SnapshotError("snapshot manifest has unexpected or missing fields")
    window = manifest.get("session_window")
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise SnapshotError("snapshot manifest has invalid session_window")
    partitions = manifest.get("partitions")
    if (not isinstance(partitions, list) or not partitions or
            len(partitions) > window or
            any(not isinstance(item, str) or not _canonical_partition_name(item)
                for item in partitions) or
            len(set(partitions)) != len(partitions) or partitions != sorted(partitions)):
        raise SnapshotError("snapshot manifest has invalid partitions")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SnapshotError("snapshot manifest has no files")
    previous: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise SnapshotError("snapshot manifest has an invalid file entry")
        path = item.get("path")
        if (not isinstance(path, str) or not path or path.startswith("/") or
                "\\" in path or any(part in {"", ".", ".."}
                                   for part in path.split("/")) or path in previous):
            raise SnapshotError("snapshot manifest has an unsafe file path")
        previous.add(path)
        if (isinstance(item.get("size"), bool) or
                not isinstance(item.get("size"), int) or item["size"] < 0 or
                not isinstance(item.get("sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise SnapshotError("snapshot manifest has invalid file identity")
    if (isinstance(manifest.get("bytes"), bool) or
            not isinstance(manifest.get("bytes"), int) or manifest["bytes"] < 0 or
            manifest["bytes"] != sum(item["size"] for item in files)):
        raise SnapshotError("snapshot manifest has invalid byte count")
    if manifest.get("immutable") is not True or not isinstance(manifest.get("identity"), str):
        raise SnapshotError("snapshot manifest is not sealed")
    if manifest["identity"] != _identity(manifest):
        raise SnapshotError("snapshot manifest identity does not match its contents")
    expected_partitions = {f"{SESSIONS_NAME}/{name}" for name in partitions}
    allowed_paths = {RECORDER_INDEX_NAME, *expected_partitions,
                     *(p + suffix for p in expected_partitions
                       for suffix in (SOURCE_SUFFIX, CALENDAR_SUFFIX))}
    if not previous <= allowed_paths:
        raise SnapshotError("snapshot manifest contains an unexpected file")
    if RECORDER_INDEX_NAME not in previous or not expected_partitions.issubset(previous):
        raise SnapshotError("snapshot manifest omits required recorder files")
    return manifest


def _list_relative_files(root: Path) -> set[str]:
    result: set[str] = set()
    _require_directory(root, label="snapshot root")
    for path in root.iterdir():
        if path.name == MANIFEST_NAME:
            _require_regular(path, label="snapshot manifest")
            result.add(path.name)
        elif path.name == RECORDER_INDEX_NAME:
            _require_regular(path, label="snapshot aggregate index")
            result.add(path.name)
        elif path.name == SESSIONS_NAME:
            _require_directory(path, label="snapshot sessions directory")
            for child in path.iterdir():
                _require_regular(child, label="snapshot session entry")
                result.add(f"{SESSIONS_NAME}/{child.name}")
        else:
            raise SnapshotError(f"unexpected entry in sealed snapshot: {path}")
    return result


def verify_snapshot(snapshot_root: os.PathLike[str] | str) -> dict[str, Any]:
    """Verify every selected byte and metadata binding before consumption."""
    root = _absolute(snapshot_root)
    _require_directory(root, label="snapshot root")
    manifest_path = root / MANIFEST_NAME
    _require_regular(manifest_path, label="snapshot manifest")
    try:
        if manifest_path.stat().st_size > MANIFEST_MAX_BYTES:
            raise SnapshotError("snapshot manifest is too large")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except SnapshotError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("snapshot manifest is unreadable") from exc
    manifest = _validate_manifest_shape(manifest)
    expected = _expected_paths(manifest)
    actual = _list_relative_files(root)
    expected_with_manifest = expected | {MANIFEST_NAME}
    if actual != expected_with_manifest:
        missing = sorted(expected_with_manifest - actual)
        extra = sorted(actual - expected_with_manifest)
        raise SnapshotError(
            f"snapshot file set is incomplete or unexpected; missing={missing}, extra={extra}")
    for item in manifest["files"]:
        relative = item["path"]
        path = root / relative
        _ensure_tree_no_symlinks(root, path, label="snapshot file")
        size, digest = _hash_file(path, label="snapshot file")
        if size != item["size"] or digest != item["sha256"]:
            raise SnapshotError(f"snapshot file identity mismatch: {relative}")
    for partition in manifest["partitions"]:
        partition_path = root / SESSIONS_NAME / partition
        for suffix in (SOURCE_SUFFIX, CALENDAR_SUFFIX):
            sidecar = partition_path.with_name(partition + suffix)
            if sidecar.exists():
                _validate_sidecar(sidecar, partition_name=partition, suffix=suffix)
    # The snapshot has no mutable recorder writer; readers use this exact
    # directory and its sessions child.  The identity is suitable for the
    # preprocessing cache only after all checks above pass.
    return {
        "schema": SNAPSHOT_SCHEMA,
        "status": "verified",
        "verified": True,
        "immutable": True,
        "identity": manifest["identity"],
        "snapshot_root": str(root),
        "partition_root": str(root / SESSIONS_NAME),
        "recorded_root": str(root),
        "session_window": manifest["session_window"],
        "partitions": list(manifest["partitions"]),
        "bytes": manifest["bytes"],
    }


def create_snapshot(
        recorded_root: os.PathLike[str] | str,
        snapshot_root: os.PathLike[str] | str,
        *,
        session_window: int,
        end_session: str | None = None,
        max_bytes: int | None = None,
        min_free_bytes: int = 0,
        progress=None,
) -> dict[str, Any]:
    """Create one atomically published bounded snapshot.

    ``progress`` receives short strings and is intended for CLI stderr output;
    it is never used to retain rows or file contents.
    """
    source_root = _absolute(recorded_root)
    destination = _absolute(snapshot_root)
    if (max_bytes is not None and
            (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or
             max_bytes <= 0)):
        raise SnapshotError("max_bytes must be a positive integer when supplied")
    if (isinstance(min_free_bytes, bool) or not isinstance(min_free_bytes, int) or
            min_free_bytes < 0):
        raise SnapshotError("min_free_bytes must be a nonnegative integer")
    _require_directory(source_root, label="recorder root")
    parent = destination.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotError(
            f"cannot create snapshot destination parent {parent}: {exc}") from exc
    _require_directory(parent, label="snapshot destination parent")
    try:
        if destination.resolve().is_relative_to(source_root.resolve()):
            raise SnapshotError("snapshot destination must be outside recorder root")
    except AttributeError:  # pragma: no cover - Python 3.8 compatibility
        if str(destination).startswith(str(source_root) + os.sep):
            raise SnapshotError("snapshot destination must be outside recorder root")
    if destination.exists() or destination.is_symlink():
        raise SnapshotError(f"snapshot destination already exists: {destination}")
    if progress:
        progress("waiting for recorder corpus lock")
    # The lock is derived from the same nominal output parent used by recorder
    # and backfill.  It is held through copy, hashing, manifest fsync, and the
    # final rename, so no append can race the selected bytes or metadata.
    lock_output = source_root / "market.csv"
    staging = parent / f".{destination.name}.staging-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with corpus_write_lock(lock_output):
            partitions = _selected_partitions(source_root, session_window, end_session)
            files = _relative_files(source_root, partitions)
            expected_bytes = sum(
                _require_regular(source, label="snapshot source").st_size
                for source, _relative in files)
            if max_bytes is not None and expected_bytes > max_bytes:
                raise SnapshotError(
                    f"selected snapshot is {expected_bytes} bytes, above max_bytes {max_bytes}")
            try:
                free_bytes = shutil.disk_usage(parent).free
            except OSError as exc:
                raise SnapshotError(
                    f"cannot inspect free space for snapshot destination: {exc}") from exc
            if free_bytes < expected_bytes + min_free_bytes:
                raise SnapshotError(
                    f"snapshot needs {expected_bytes} bytes plus {min_free_bytes} reserved "
                    f"bytes, but destination has {free_bytes} free bytes")
            if progress:
                progress(f"copying {len(files)} sealed files ({expected_bytes} bytes)")
            staging.mkdir(mode=0o755)
            (staging / SESSIONS_NAME).mkdir(mode=0o755)
            manifest_files: list[dict[str, Any]] = []
            total = 0
            for source, relative in files:
                target = staging / relative
                entry = _copy_and_hash(source, target)
                entry["path"] = relative
                manifest_files.append(entry)
                total += entry["size"]
                _fsync_directory(target.parent)
            manifest = {
                "schema": SNAPSHOT_SCHEMA,
                "created_at": datetime.now().astimezone().isoformat(),
                "session_window": int(session_window),
                "partitions": [path.name for path in partitions],
                "files": manifest_files,
                "bytes": total,
                "immutable": True,
            }
            manifest["identity"] = _identity(manifest)
            _write_manifest(staging / MANIFEST_NAME, manifest)
            # Validate metadata and the complete file set before publication.
            # A bad sidecar must never leave a directory that looks sealed.
            verify_snapshot(staging)
            for path in staging.rglob("*"):
                if path.is_file():
                    path.chmod(0o444)
            _fsync_directory(staging / SESSIONS_NAME)
            _fsync_directory(staging)
            # Both paths are siblings under ``parent``; os.replace is atomic
            # and the parent fsync makes the publication durable.
            os.replace(staging, destination)
            _fsync_directory(parent)
    except SnapshotError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except (OSError, ValueError, TypeError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise SnapshotError(f"snapshot creation failed: {exc}") from exc
    if progress:
        progress("published sealed snapshot")
    return verify_snapshot(destination)


def _json_print(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="copy and seal recorder sessions")
    create.add_argument("--recorded-root", type=Path, required=True)
    create.add_argument("--snapshot-root", type=Path, required=True)
    create.add_argument("--session-window", type=int, required=True)
    create.add_argument("--end-session", help="last included ISO session date")
    create.add_argument("--max-bytes", type=int, default=None)
    create.add_argument("--min-free-bytes", type=int, default=0)
    verify = subparsers.add_parser("verify", help="verify a sealed snapshot")
    verify.add_argument("--snapshot-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            value = create_snapshot(
                args.recorded_root, args.snapshot_root,
                session_window=args.session_window,
                end_session=args.end_session,
                max_bytes=args.max_bytes,
                min_free_bytes=args.min_free_bytes,
                progress=lambda message: print(f"snapshot: {message}", file=sys.stderr,
                                               flush=True))
        else:
            print("snapshot: verifying sealed files", file=sys.stderr, flush=True)
            value = verify_snapshot(args.snapshot_root)
    except SnapshotError as exc:
        print(f"research_snapshot: {exc}", file=sys.stderr)
        return 2
    _json_print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
