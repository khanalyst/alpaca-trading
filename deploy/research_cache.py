#!/usr/bin/env python3
"""Immutable, content-addressed cache for deterministic research artifacts.

The cache deliberately has no path/mtime fallback for source identity.  A
caller must supply an immutable source identity (normally a content digest or
an audited recorder snapshot identity), plus identities for the relevant
configuration, preprocessing code, and execution context.  Those four values
form the cache key.

Entries are published as complete directories under a per-key filesystem lock.
Every lookup re-hashes every artifact before reporting a hit.  Invalid or
partial entries are misses and, by default, are moved aside for inspection.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from typing import Any, Iterator
import uuid


IDENTITY_SCHEMA = "research-preprocessing-cache-identity.v1"
ENTRY_SCHEMA = "research-preprocessing-cache-entry.v1"
RESULT_SCHEMA = "research-preprocessing-cache-result.v1"
KEY_ALGORITHM = "sha256"
_KEY_DOMAIN = b"alpaca-research-preprocessing-cache-key.v1\0"
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_NAME = "manifest.json"
_MANIFEST_LIMIT = 1024 * 1024
_CHUNK_SIZE = 1024 * 1024


class ResearchCacheError(Exception):
    """Base class for expected, structured cache errors."""

    code = "cache_error"


class CacheIdentityError(ResearchCacheError):
    code = "invalid_identity"


class CacheArtifactError(ResearchCacheError):
    code = "invalid_artifact"


class CacheCorruptionError(ResearchCacheError):
    code = "cache_corruption"


class _CliUsageError(ResearchCacheError):
    code = "usage"


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliUsageError(message)


def _validate_json_value(value: Any, label: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CacheIdentityError(f"{label} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CacheIdentityError(f"{label} contains a non-string key")
            _validate_json_value(item, f"{label}.{key}")
        return
    raise CacheIdentityError(
        f"{label} must be a JSON value, not {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False)


def _identity_value(value: Any, label: str) -> Any:
    if value is None:
        raise CacheIdentityError(f"{label} is required")
    _validate_json_value(value, label)
    if isinstance(value, str) and not value.strip():
        raise CacheIdentityError(f"{label} must not be blank")
    if isinstance(value, (list, dict)) and not value:
        raise CacheIdentityError(f"{label} must not be empty")
    # Round-tripping produces a detached, JSON-only value for manifests.
    return json.loads(_canonical_json(value))


def identity_document(
    *,
    source_identity: Any = None,
    config_identity: Any = None,
    code_identity: Any = None,
    context_identity: Any = None,
) -> dict[str, Any]:
    """Return the canonical key document, rejecting every implicit identity.

    In particular, ``source_identity`` is never inferred from a source path,
    size, or mtime.  Callers must bind an immutable identity explicitly.
    """

    return {
        "schema": IDENTITY_SCHEMA,
        "source": _identity_value(source_identity, "source_identity"),
        "config": _identity_value(config_identity, "config_identity"),
        "code": _identity_value(code_identity, "code_identity"),
        "context": _identity_value(context_identity, "context_identity"),
    }


def make_cache_key(
    *,
    source_identity: Any = None,
    config_identity: Any = None,
    code_identity: Any = None,
    context_identity: Any = None,
) -> str:
    """Build a deterministic SHA-256 key over all cache identities."""

    identity = identity_document(
        source_identity=source_identity,
        config_identity=config_identity,
        code_identity=code_identity,
        context_identity=context_identity)
    encoded = _canonical_json(identity).encode("utf-8")
    return hashlib.sha256(_KEY_DOMAIN + encoded).hexdigest()


# A short alias is convenient for API callers and shell helper snippets.
cache_key = make_cache_key


def _absolute(path: os.PathLike[str] | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
                raise
    finally:
        os.close(descriptor)


def _sha256_regular_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CacheArtifactError(f"artifact is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev, before.st_ino, before.st_size,
            getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9)))
        after_identity = (
            after.st_dev, after.st_ino, after.st_size,
            getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9)))
        if before_identity != after_identity or total != after.st_size:
            raise CacheArtifactError(f"artifact changed while being read: {path}")
        return digest.hexdigest(), total
    finally:
        os.close(descriptor)


def _copy_and_hash(source: Path, target: Path) -> tuple[str, int]:
    source_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        source_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    source_descriptor = os.open(source, source_flags)
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CacheArtifactError(
                f"artifact source is not a regular file: {source}")
        target_descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(source_descriptor, "rb", closefd=False) as source_file, \
                    os.fdopen(target_descriptor, "wb", closefd=False) as target_file:
                while True:
                    chunk = source_file.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    target_file.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                target_file.flush()
                os.fsync(target_descriptor)
        finally:
            os.close(target_descriptor)
        after = os.fstat(source_descriptor)
        before_identity = (
            before.st_dev, before.st_ino, before.st_size,
            getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9)))
        after_identity = (
            after.st_dev, after.st_ino, after.st_size,
            getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9)))
        if before_identity != after_identity or total != after.st_size:
            raise CacheArtifactError(
                f"artifact source changed while being copied: {source}")
        return digest.hexdigest(), total
    finally:
        os.close(source_descriptor)


def _remove_staging_tree(path: Path) -> None:
    if not _lexists(path):
        return
    for directory, child_directories, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.chmod(Path(directory) / name, 0o600, follow_symlinks=False)
            except OSError:
                pass
        for name in child_directories:
            try:
                os.chmod(Path(directory) / name, 0o700, follow_symlinks=False)
            except OSError:
                pass
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except OSError:
        pass
    shutil.rmtree(path)


class ResearchArtifactCache:
    """Filesystem-backed immutable preprocessing artifact cache."""

    def __init__(self, root: os.PathLike[str] | str) -> None:
        if not os.fspath(root):
            raise ResearchCacheError("cache root is required")
        self.root = _absolute(root)
        self.entries = self.root / "entries"
        self.locks = self.root / "locks"
        self.staging = self.root / "staging"
        self.quarantine = self.root / "quarantine"

    def _ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (
                self.entries, self.locks, self.staging, self.quarantine):
            directory.mkdir(mode=0o700, exist_ok=True)
            if not directory.is_dir():
                raise ResearchCacheError(
                    f"cache layout path is not a directory: {directory}")

    @contextmanager
    def _lock(self, key: str) -> Iterator[None]:
        if not _KEY_RE.fullmatch(key):
            raise ResearchCacheError("cache key is malformed")
        self._ensure_layout()
        lock_path = self.locks / f"{key}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @staticmethod
    def _identity_and_key(
        *, source_identity: Any, config_identity: Any,
        code_identity: Any, context_identity: Any,
    ) -> tuple[dict[str, Any], str]:
        identity = identity_document(
            source_identity=source_identity,
            config_identity=config_identity,
            code_identity=code_identity,
            context_identity=context_identity)
        key = hashlib.sha256(
            _KEY_DOMAIN + _canonical_json(identity).encode("utf-8")).hexdigest()
        return identity, key

    @staticmethod
    def _normalize_artifacts(
            artifacts: Mapping[str, os.PathLike[str] | str]) -> dict[str, Path]:
        if not isinstance(artifacts, Mapping) or not artifacts:
            raise CacheArtifactError("at least one artifact is required")
        if len(artifacts) > 128:
            raise CacheArtifactError("too many artifacts")
        normalized: dict[str, Path] = {}
        for name, path in artifacts.items():
            if not isinstance(name, str) or not _ARTIFACT_RE.fullmatch(name):
                raise CacheArtifactError(
                    f"invalid artifact name: {name!r}")
            if path is None or not os.fspath(path):
                raise CacheArtifactError(f"artifact path is required for {name}")
            normalized[name] = _absolute(path)
        return normalized

    def _validate_entry(
        self, entry: Path, key: str, identity: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            entry_stat = entry.lstat()
            if not stat.S_ISDIR(entry_stat.st_mode):
                return None, "entry_not_directory"
            root_names = {item.name for item in entry.iterdir()}
            if root_names != {_MANIFEST_NAME, "artifacts"}:
                return None, "entry_layout_mismatch"
            manifest_path = entry / _MANIFEST_NAME
            manifest_stat = manifest_path.lstat()
            if (not stat.S_ISREG(manifest_stat.st_mode)
                    or manifest_stat.st_size > _MANIFEST_LIMIT):
                return None, "manifest_not_regular_or_too_large"
            raw = manifest_path.read_bytes()
            if len(raw) != manifest_stat.st_size:
                return None, "manifest_changed_during_read"
            manifest = json.loads(raw.decode("utf-8"))
            if not isinstance(manifest, dict):
                return None, "manifest_not_object"
            if set(manifest) != {
                    "schema", "key", "key_algorithm", "identity", "artifacts"}:
                return None, "manifest_fields_mismatch"
            if (manifest.get("schema") != ENTRY_SCHEMA
                    or manifest.get("key") != key
                    or manifest.get("key_algorithm") != KEY_ALGORITHM):
                return None, "manifest_identity_mismatch"
            try:
                manifest_identity = _canonical_json(manifest.get("identity"))
            except (TypeError, ValueError):
                return None, "manifest_identity_invalid"
            if manifest_identity != _canonical_json(identity):
                return None, "manifest_identity_mismatch"
            records = manifest.get("artifacts")
            if not isinstance(records, dict) or not records or len(records) > 128:
                return None, "manifest_artifacts_invalid"
            artifacts_directory = entry / "artifacts"
            artifact_directory_stat = artifacts_directory.lstat()
            if not stat.S_ISDIR(artifact_directory_stat.st_mode):
                return None, "artifact_directory_invalid"
            if set(os.listdir(artifacts_directory)) != set(records):
                return None, "artifact_set_mismatch"
            for name, record in records.items():
                if not isinstance(name, str) or not _ARTIFACT_RE.fullmatch(name):
                    return None, "artifact_name_invalid"
                if not isinstance(record, dict) or set(record) != {
                        "path", "sha256", "size"}:
                    return None, f"artifact_record_invalid:{name}"
                expected_path = f"artifacts/{name}"
                expected_digest = record.get("sha256")
                expected_size = record.get("size")
                if record.get("path") != expected_path:
                    return None, f"artifact_path_invalid:{name}"
                if (not isinstance(expected_digest, str)
                        or not _DIGEST_RE.fullmatch(expected_digest)):
                    return None, f"artifact_digest_invalid:{name}"
                if (not isinstance(expected_size, int)
                        or isinstance(expected_size, bool) or expected_size < 0):
                    return None, f"artifact_size_invalid:{name}"
                try:
                    actual_digest, actual_size = _sha256_regular_file(
                        artifacts_directory / name)
                except (OSError, ResearchCacheError):
                    return None, f"artifact_unreadable:{name}"
                if actual_size != expected_size:
                    return None, f"artifact_size_mismatch:{name}"
                if actual_digest != expected_digest:
                    return None, f"artifact_sha256_mismatch:{name}"
            return manifest, None
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None, "entry_unreadable_or_partial"

    def _quarantine_entry(self, entry: Path, key: str) -> Path:
        suffix = f"{time.time_ns()}.{os.getpid()}.{uuid.uuid4().hex}"
        destination = self.quarantine / f"{key}.{suffix}"
        # Published entry directories are read-only.  macOS requires owner
        # write permission on a directory being moved, so relax only the
        # already-invalid entry while the exclusive key lock is held.
        try:
            if stat.S_ISDIR(entry.lstat().st_mode):
                os.chmod(entry, 0o700, follow_symlinks=False)
        except OSError:
            pass
        os.replace(entry, destination)
        _fsync_directory(self.entries)
        _fsync_directory(self.quarantine)
        return destination

    @staticmethod
    def _artifact_result(
        entry: Path, manifest: dict[str, Any],
        materialized: Mapping[str, Path] | None = None,
    ) -> dict[str, dict[str, Any]]:
        materialized = materialized or {}
        result: dict[str, dict[str, Any]] = {}
        for name, record in sorted(manifest["artifacts"].items()):
            path = materialized.get(name, entry / record["path"])
            result[name] = {
                "path": str(path),
                "sha256": record["sha256"],
                "size": record["size"],
                "materialized": name in materialized,
            }
        return result

    def _materialize(
        self, entry: Path, manifest: dict[str, Any],
        destinations: Mapping[str, os.PathLike[str] | str],
    ) -> dict[str, Path]:
        if not isinstance(destinations, Mapping):
            raise CacheArtifactError("materialize must be an artifact mapping")
        records = manifest["artifacts"]
        normalized: dict[str, Path] = {}
        seen_paths: set[Path] = set()
        cache_root = self.root.resolve(strict=False)
        for name, raw_path in destinations.items():
            if name not in records:
                raise CacheArtifactError(
                    f"cache entry has no artifact named {name!r}")
            if raw_path is None or not os.fspath(raw_path):
                raise CacheArtifactError(
                    f"materialization path is required for {name}")
            destination = _absolute(raw_path)
            resolved = destination.resolve(strict=False)
            try:
                within_cache = os.path.commonpath(
                    (os.fspath(cache_root), os.fspath(resolved))) == os.fspath(cache_root)
            except ValueError:
                within_cache = False
            if within_cache:
                raise CacheArtifactError(
                    "materialization destination must be outside the cache root")
            if resolved in seen_paths:
                raise CacheArtifactError(
                    "materialization destinations must be distinct")
            seen_paths.add(resolved)
            normalized[name] = destination

        for name, destination in normalized.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.research-cache-",
                dir=destination.parent)
            os.close(descriptor)
            temporary = Path(temporary_name)
            temporary.unlink()
            try:
                digest, size = _copy_and_hash(
                    entry / records[name]["path"], temporary)
                if (digest != records[name]["sha256"]
                        or size != records[name]["size"]):
                    raise CacheCorruptionError(
                        f"cached artifact changed during materialization: {name}")
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
            finally:
                if _lexists(temporary):
                    temporary.unlink()
        return normalized

    def lookup(
        self,
        *,
        source_identity: Any = None,
        config_identity: Any = None,
        code_identity: Any = None,
        context_identity: Any = None,
        materialize: Mapping[str, os.PathLike[str] | str] | None = None,
        quarantine_corrupt: bool = True,
    ) -> dict[str, Any]:
        """Validate and return a hit, or a safe miss for any partial entry."""

        identity, key = self._identity_and_key(
            source_identity=source_identity,
            config_identity=config_identity,
            code_identity=code_identity,
            context_identity=context_identity)
        entry = self.entries / key
        with self._lock(key):
            if not _lexists(entry):
                return {
                    "schema": RESULT_SCHEMA, "operation": "lookup",
                    "status": "miss", "hit": False, "key": key,
                    "reason": "not_found", "artifacts": {},
                    "quarantined": None,
                }
            manifest, validation_error = self._validate_entry(entry, key, identity)
            if manifest is None:
                quarantined = None
                quarantine_error = None
                if quarantine_corrupt:
                    try:
                        quarantined = str(self._quarantine_entry(entry, key))
                    except OSError as exc:
                        quarantine_error = f"{type(exc).__name__}: {exc}"
                return {
                    "schema": RESULT_SCHEMA, "operation": "lookup",
                    "status": "miss", "hit": False, "key": key,
                    "reason": "corrupt_or_partial",
                    "validation_error": validation_error,
                    "artifacts": {}, "quarantined": quarantined,
                    "quarantine_error": quarantine_error,
                }
            materialized = (
                self._materialize(entry, manifest, materialize)
                if materialize else {})
            return {
                "schema": RESULT_SCHEMA, "operation": "lookup",
                "status": "hit", "hit": True, "key": key,
                "reason": None, "entry": str(entry),
                "artifacts": self._artifact_result(
                    entry, manifest, materialized),
                "quarantined": None,
            }

    def publish(
        self,
        artifacts: Mapping[str, os.PathLike[str] | str],
        *,
        source_identity: Any = None,
        config_identity: Any = None,
        code_identity: Any = None,
        context_identity: Any = None,
    ) -> dict[str, Any]:
        """Atomically publish a complete entry; never overwrite a valid one."""

        identity, key = self._identity_and_key(
            source_identity=source_identity,
            config_identity=config_identity,
            code_identity=code_identity,
            context_identity=context_identity)
        sources = self._normalize_artifacts(artifacts)
        entry = self.entries / key
        with self._lock(key):
            quarantined = None
            if _lexists(entry):
                manifest, validation_error = self._validate_entry(entry, key, identity)
                if manifest is not None:
                    return {
                        "schema": RESULT_SCHEMA, "operation": "publish",
                        "status": "existing", "hit": True,
                        "published": False, "key": key,
                        "entry": str(entry), "reason": None,
                        "artifacts": self._artifact_result(entry, manifest),
                        "quarantined": None,
                    }
                try:
                    quarantined = str(self._quarantine_entry(entry, key))
                except OSError as exc:
                    raise CacheCorruptionError(
                        "cannot quarantine invalid cache entry before rebuild: "
                        f"{validation_error}: {exc}") from exc

            staging_path = Path(tempfile.mkdtemp(
                prefix=f"{key}.{os.getpid()}.", dir=self.staging))
            try:
                artifacts_path = staging_path / "artifacts"
                artifacts_path.mkdir(mode=0o700)
                records: dict[str, dict[str, Any]] = {}
                for name, source in sorted(sources.items()):
                    destination = artifacts_path / name
                    try:
                        digest, size = _copy_and_hash(source, destination)
                    except OSError as exc:
                        raise CacheArtifactError(
                            f"cannot copy artifact {name!r}: {exc}") from exc
                    os.chmod(destination, 0o444)
                    records[name] = {
                        "path": f"artifacts/{name}",
                        "sha256": digest,
                        "size": size,
                    }
                manifest = {
                    "schema": ENTRY_SCHEMA,
                    "key": key,
                    "key_algorithm": KEY_ALGORITHM,
                    "identity": identity,
                    "artifacts": records,
                }
                manifest_path = staging_path / _MANIFEST_NAME
                with manifest_path.open("x", encoding="utf-8") as handle:
                    handle.write(_canonical_json(manifest) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(manifest_path, 0o444)
                _fsync_directory(artifacts_path)
                _fsync_directory(staging_path)
                os.chmod(artifacts_path, 0o555)
                # Keep the staging directory owner-writable through rename.
                # macOS requires that permission when moving a directory;
                # readers cannot observe it because publication and lookup
                # share the per-key lock.
                os.replace(staging_path, entry)
                os.chmod(entry, 0o555)
                _fsync_directory(self.entries)
            except Exception:
                _remove_staging_tree(staging_path)
                raise
            return {
                "schema": RESULT_SCHEMA, "operation": "publish",
                "status": "published", "hit": True, "published": True,
                "key": key, "entry": str(entry), "reason": None,
                "artifacts": self._artifact_result(entry, manifest),
                "quarantined": quarantined,
            }


def lookup(
    cache_root: os.PathLike[str] | str,
    *,
    source_identity: Any = None,
    config_identity: Any = None,
    code_identity: Any = None,
    context_identity: Any = None,
    materialize: Mapping[str, os.PathLike[str] | str] | None = None,
    quarantine_corrupt: bool = True,
) -> dict[str, Any]:
    """Functional wrapper for :class:`ResearchArtifactCache.lookup`."""

    return ResearchArtifactCache(cache_root).lookup(
        source_identity=source_identity,
        config_identity=config_identity,
        code_identity=code_identity,
        context_identity=context_identity,
        materialize=materialize,
        quarantine_corrupt=quarantine_corrupt)


def publish(
    cache_root: os.PathLike[str] | str,
    artifacts: Mapping[str, os.PathLike[str] | str],
    *,
    source_identity: Any = None,
    config_identity: Any = None,
    code_identity: Any = None,
    context_identity: Any = None,
) -> dict[str, Any]:
    """Functional wrapper for :class:`ResearchArtifactCache.publish`."""

    return ResearchArtifactCache(cache_root).publish(
        artifacts,
        source_identity=source_identity,
        config_identity=config_identity,
        code_identity=code_identity,
        context_identity=context_identity)


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-identity", "--source-id", dest="source_identity")
    parser.add_argument("--config-identity", "--config-id", dest="config_identity")
    parser.add_argument("--code-identity", "--code-id", dest="code_identity")
    parser.add_argument("--context-identity", "--context-id", dest="context_identity")


def _build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(
        dest="operation", parser_class=_JsonArgumentParser)

    key_parser = subparsers.add_parser("key")
    _add_identity_arguments(key_parser)

    lookup_parser = subparsers.add_parser("lookup")
    lookup_parser.add_argument("--cache-root", "--root", dest="cache_root")
    _add_identity_arguments(lookup_parser)
    lookup_parser.add_argument(
        "--materialize", action="append", default=[], metavar="NAME=PATH")
    lookup_parser.add_argument("--no-quarantine", action="store_true")

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--cache-root", "--root", dest="cache_root")
    _add_identity_arguments(publish_parser)
    publish_parser.add_argument(
        "--artifact", action="append", default=[], metavar="NAME=PATH")
    return parser


def _assignments(values: list[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise _CliUsageError(f"{label} must use NAME=PATH")
        name, path = value.split("=", 1)
        if not _ARTIFACT_RE.fullmatch(name):
            raise _CliUsageError(f"invalid artifact name: {name!r}")
        if not path:
            raise _CliUsageError(f"path is required for artifact {name!r}")
        if name in result:
            raise _CliUsageError(f"duplicate artifact assignment: {name!r}")
        result[name] = path
    return result


def _error_result(operation: str | None, error: Exception) -> dict[str, Any]:
    code = error.code if isinstance(error, ResearchCacheError) else "io_error"
    return {
        "schema": RESULT_SCHEMA,
        "operation": operation,
        "status": "error",
        "error": code,
        "message": str(error),
    }


def main(argv: list[str] | None = None) -> int:
    operation = None
    try:
        args = _build_parser().parse_args(argv)
        operation = args.operation
        if operation not in {"key", "lookup", "publish"}:
            raise _CliUsageError("an operation is required: key, lookup, or publish")
        identity_arguments = {
            "source_identity": args.source_identity,
            "config_identity": args.config_identity,
            "code_identity": args.code_identity,
            "context_identity": args.context_identity,
        }
        if operation == "key":
            identity = identity_document(**identity_arguments)
            result = {
                "schema": RESULT_SCHEMA, "operation": "key",
                "status": "ok",
                "key": hashlib.sha256(
                    _KEY_DOMAIN
                    + _canonical_json(identity).encode("utf-8")).hexdigest(),
            }
        else:
            if not args.cache_root:
                raise _CliUsageError("--cache-root is required")
            cache = ResearchArtifactCache(args.cache_root)
            if operation == "lookup":
                materialize = _assignments(args.materialize, "--materialize")
                result = cache.lookup(
                    **identity_arguments,
                    materialize=materialize or None,
                    quarantine_corrupt=not args.no_quarantine)
            else:
                artifacts = _assignments(args.artifact, "--artifact")
                result = cache.publish(artifacts, **identity_arguments)
        print(_canonical_json(result), flush=True)
        return 1 if result.get("status") == "miss" else 0
    except (ResearchCacheError, OSError, ValueError, TypeError) as exc:
        print(_canonical_json(_error_result(operation, exc)), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
