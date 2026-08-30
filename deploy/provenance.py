"""Deterministic deployment identity and cross-service parity checks.

The process environment is the narrowest reliable boundary shared by Compose,
systemd, and recovery shells.  A deployment may provide a source commit, an
immutable image digest, or both.  Health/status payloads expose the same
bounded projection so operators and tests can verify that every service came
from one build.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping


SCHEMA = "deployment-provenance.v1"
_MAX = 256
_OCI_DIGEST = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
_GIT_OBJECT = re.compile(r"^[0-9a-fA-F]+$")


def _text(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())[:_MAX]
    if text.lower() in {"unknown", "unset", "none", "null"}:
        return None
    return text or None


def _first_text(*values: object) -> str | None:
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return None


def _oci_digest(value: object) -> str | None:
    """Normalize exactly one OCI sha256 digest, rejecting arbitrary tags."""
    text = _text(value)
    if text is None:
        return None
    match = _OCI_DIGEST.fullmatch(text)
    return f"sha256:{match.group(1).lower()}" if match else None


def _git_object(value: object) -> str | None:
    """Normalize a full SHA-1/SHA-256 Git object id only."""
    text = _text(value)
    if text is None or len(text) not in {40, 64} or not _GIT_OBJECT.fullmatch(text):
        return None
    return text.lower()


def deployment_provenance(environ: Mapping[str, object] | None = None) -> dict:
    """Return a bounded, deterministic identity for the current process.

    ``ALPACA_DEPLOYMENT_COMMIT`` and ``ALPACA_DEPLOYMENT_IMAGE_DIGEST`` are
    preferred explicit inputs.  The aliases make the helper usable from CI,
    Docker labels, and systemd without requiring a particular launcher.
    """
    env = os.environ if environ is None else environ
    declared_raw = _text(env.get("ALPACA_DEPLOYMENT_COMMIT"))
    build_raw = _text(env.get("ALPACA_BUILD_COMMIT"))
    fallback_raw = _text(env.get("GIT_COMMIT"))
    declared_commit = _git_object(declared_raw)
    build_commit = _git_object(build_raw)
    fallback_commit = _git_object(fallback_raw)
    commit_invalid = any(raw is not None and parsed is None
                         for raw, parsed in ((declared_raw, declared_commit),
                                             (build_raw, build_commit),
                                             (fallback_raw, fallback_commit)))
    commit_mismatch = bool(
        declared_commit and build_commit and declared_commit != build_commit)
    # The build-time value is baked into the image and therefore outranks a
    # mutable runtime declaration.  Placeholder values such as ``unknown``
    # must not mask that immutable identity.
    commit = build_commit or declared_commit or fallback_commit
    image = _first_text(
        env.get("ALPACA_DEPLOYMENT_IMAGE"), env.get("ALPACA_AGENT_IMAGE"),
        env.get("IMAGE_NAME"))
    explicit_digest = _first_text(
        env.get("ALPACA_DEPLOYMENT_IMAGE_DIGEST"),
        env.get("ALPACA_IMAGE_DIGEST"), env.get("IMAGE_DIGEST"))
    embedded_digest = None
    if image and "@" in image:
        embedded_digest = image.rsplit("@", 1)[1]
    elif image and image.startswith("sha256:"):
        embedded_digest = image
    digest_raw = explicit_digest if explicit_digest is not None else embedded_digest
    digest = _oci_digest(digest_raw)
    digest_invalid = digest_raw is not None and digest is None
    tag = _text(env.get("ALPACA_AGENT_IMAGE_TAG"))
    # A digest is the strongest identity.  A commit is deterministic when an
    # image digest is unavailable (for example in a local Compose checkout).
    # Bare tags are deliberately *not* identities: ``:local`` can point at a
    # different build while every container still reports the same tag.
    identity = None if commit_mismatch else (digest or commit)
    return {
        "schema": SCHEMA,
        "identity": identity,
        "commit": commit,
        "build_commit": build_commit,
        "declared_commit": declared_commit,
        "declared_commit_raw": declared_raw,
        "build_commit_raw": build_raw,
        "commit_invalid": commit_invalid,
        "commit_invalid_reason": ("invalid_git_object_id" if commit_invalid
                                   else None),
        "commit_mismatch": commit_mismatch,
        "image": image,
        "image_digest": digest or digest_raw,
        "image_digest_valid": (None if digest_raw is None else not digest_invalid),
        "image_digest_invalid": digest_invalid,
        "image_digest_reason": ("invalid_oci_sha256_digest" if digest_invalid
                                 else None),
        "invalid_digest": digest_invalid,
        "invalid_digest_reason": ("invalid_oci_sha256_digest"
                                   if digest_invalid else None),
        "image_tag": tag,
    }


def deployment_parity(records: Iterable[Mapping[str, object]]) -> dict:
    """Compare service provenance records and fail closed when identity is absent."""
    values = []
    names = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        names.append(_text(record.get("component")) or f"service-{len(names) + 1}")
        value = record.get("provenance")
        if not isinstance(value, Mapping):
            value = record.get("deployment_provenance")
        if not isinstance(value, Mapping):
            value = record
        identity = _text(value.get("identity"))
        # An invalid explicit digest cannot be accepted as an opaque identity.
        # A verified baked/declared commit remains a valid fallback.
        if value.get("commit_mismatch") is True:
            identity = None
        elif value.get("image_digest_invalid") is True:
            identity = _git_object(value.get("commit"))
        elif value.get("commit_invalid") is True:
            identity = (_git_object(value.get("commit")) or
                        _oci_digest(value.get("image_digest")))
        values.append(identity)
    known = [value for value in values if value]
    ok = bool(values) and len(known) == len(values) and len(set(known)) == 1
    return {
        "schema": "deployment-parity.v1",
        "ok": ok,
        "reason": ("all services share one deployment identity" if ok else
                   "deployment identity is missing or mismatched"),
        "services": names,
        "identities": values,
        "identity": known[0] if ok else None,
    }


# Short alias for callers that prefer a verb.
check_deployment_parity = deployment_parity
