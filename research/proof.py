"""Proof artifact persistence and webhook delivery.

Deterministic payload construction and Markdown rendering live in
:mod:`research.proof_payload`; this module keeps the result type and the
side-effecting artifact/webhook operations.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import tempfile
from threading import Thread
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .proof_payload import (
    PROOF_SCHEMA,
    PROOF_SCHEMA_V1,
    PROOF_SCHEMA_V2,
    SESSION_ZONE,
    build_proof_payload,
    canonical_json,
    payload_hash,
    proof_statistics,
    render_markdown,
)


@dataclass(frozen=True)
class ProofResult:
    payload: dict[str, Any]
    payload_hash: str
    path: Path
    created: bool = True
    webhook: dict[str, Any] | None = None

    @property
    def artifact_path(self) -> Path:
        return self.path

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __str__(self) -> str:
        return str(self.path)


def send_webhook(url: str, metadata: Mapping[str, Any], *,
                 sender: Callable[..., Any] | None = None,
                 timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Best-effort HTTPS notification containing minimal report metadata."""

    parsed = urlparse(str(url))
    if parsed.scheme != "https" or not parsed.netloc:
        return {"ok": False, "error": "webhook URL must use HTTPS"}
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "error": "webhook timeout must be in (0, 30] seconds"}
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        return {"ok": False, "error": "webhook timeout must be in (0, 30] seconds"}
    safe = {key: str(metadata[key]) for key in ("candidate_id", "vehicle", "status", "payload_hash", "artifact")
            if metadata.get(key) is not None}
    try:
        if sender is None:
            # Keep networking optional and dependency-free.  urllib is imported
            # only when a caller explicitly requests a webhook.
            from urllib.request import Request, urlopen
            request = Request(str(url), data=canonical_json(safe).encode("utf-8"),
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=float(timeout_seconds)) as response:  # nosec B310 - HTTPS checked above
                return {"ok": True, "status": getattr(response, "status", 200)}
        results: Queue[tuple[bool, Any]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                try:
                    signature = inspect.signature(sender)
                except (TypeError, ValueError):
                    signature = None
                if signature is None:
                    value = sender(str(url), safe)
                else:
                    positional = [
                        parameter for parameter in signature.parameters.values()
                        if parameter.kind in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD)]
                    variadic = any(
                        parameter.kind == inspect.Parameter.VAR_POSITIONAL
                        for parameter in signature.parameters.values())
                    value = (sender(str(url), safe)
                             if variadic or len(positional) >= 2 else sender(safe))
                results.put_nowait((True, value))
            except Exception as exc:  # noqa: BLE001 - returned as best-effort result
                results.put_nowait((False, exc))

        worker = Thread(target=invoke, daemon=True, name="proof-webhook")
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            return {"ok": False, "error": "TimeoutError: webhook sender timed out"}
        try:
            ok, value = results.get_nowait()
        except Empty:
            return {"ok": False, "error": "webhook sender returned no result"}
        if ok:
            return {"ok": True, "result": value}
        raise value
    except Exception as exc:  # best effort by contract
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _publish_once(target: Path, data: bytes) -> bool:
    """Publish ``data`` at ``target`` atomically; never replace an existing file.

    The bytes are written and fsynced under a temporary name in the same
    directory, then hard-linked to the final name, which fails if the name
    exists.  The final name therefore never exists partially written: a
    crash or a full disk leaves at most a stray temporary file, and a retry
    with the same payload succeeds (review finding D1-10).  Returns whether
    this call created the artefact.
    """
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".partial", dir=str(target.parent))
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, str(target))
        except FileExistsError:
            existing = target.read_bytes()
            if existing == data:
                return False
            if len(existing) < len(data) and data.startswith(existing):
                # A strict prefix is the remains of a write the code before
                # this fix interrupted; it was never a complete artefact.
                os.replace(temporary, str(target))
                _fsync_directory(target.parent)
                return True
            raise RuntimeError(
                f"existing proof artifact does not match payload: {target}")
        except OSError:
            # No hard links on this filesystem: fall back to an exclusive
            # create.  The name can then hold a partial file after a crash,
            # which the strict-prefix repair above recovers on the next run.
            try:
                handle = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if target.read_bytes() != data:
                    raise RuntimeError(
                        f"existing proof artifact does not match payload: {target}")
                return False
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        _fsync_directory(target.parent)
        return True
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _artifact_path(output_root: str | Path, vehicle: str, candidate_id: str,
                   digest: str, hash_prefix_length: int) -> Path:
    safe_candidate = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(candidate_id)).strip("._") or "candidate"
    return Path(output_root) / vehicle / f"{safe_candidate}-{digest[:hash_prefix_length]}.md"


def write_proof(ledger: Any, candidate_id: str, context: Mapping[str, Any] | None = None,
                output_root: str | Path = "research/results/edges",
                webhook_url: str | None = None,
                webhook_sender: Callable[..., Any] | None = None,
                webhook_timeout_seconds: float = 5.0,
                hash_prefix_length: int = 16,
                schema: str | None = None) -> ProofResult:
    """Create one deterministic artifact atomically and never overwrite it.

    Without ``schema`` an artefact already written under the v1 payload is
    reproduced exactly (no new file, no webhook), and anything new is written
    under the current payload schema.
    """

    if hash_prefix_length < 8 or hash_prefix_length > 64:
        raise ValueError("hash_prefix_length must be between 8 and 64")
    payload = None
    if schema is None:
        legacy = build_proof_payload(ledger, candidate_id, context,
                                     schema=PROOF_SCHEMA_V1)
        legacy_digest = payload_hash(legacy)
        legacy_target = _artifact_path(output_root, str(legacy["vehicle"]),
                                       candidate_id, legacy_digest,
                                       hash_prefix_length)
        if legacy_target.exists():
            payload, digest = legacy, legacy_digest
    if payload is None:
        payload = build_proof_payload(ledger, candidate_id, context,
                                      schema=schema or PROOF_SCHEMA)
        digest = payload_hash(payload)
    vehicle = str(payload["vehicle"])
    target = _artifact_path(output_root, vehicle, candidate_id, digest,
                            hash_prefix_length)
    target.parent.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(payload, digest)
    created = _publish_once(target, markdown.encode("utf-8"))

    webhook = None
    if webhook_url and created:
        webhook = send_webhook(webhook_url, {
            "candidate_id": candidate_id, "vehicle": vehicle,
            "status": payload.get("status"), "payload_hash": digest,
            "artifact": str(target.relative_to(Path(output_root))),
        }, sender=webhook_sender, timeout_seconds=webhook_timeout_seconds)
    return ProofResult(payload=payload, payload_hash=digest, path=target,
                       created=created, webhook=webhook)


__all__ = [
    "PROOF_SCHEMA", "PROOF_SCHEMA_V1", "PROOF_SCHEMA_V2", "ProofResult",
    "canonical_json", "payload_hash", "build_proof_payload",
    "proof_statistics", "render_markdown", "send_webhook", "write_proof",
]
