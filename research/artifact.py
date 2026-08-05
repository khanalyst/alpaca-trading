"""Content-addressed deployable-artifact identity.

This module has no dependency on the agent execution or research store.  It
uses only the pure provider-endpoint identity helper when a research packet
records the runtime that produced its evidence and a later executor verifies
that identity.

The manifest is a JSON object with an explicit schema.  Its hash is computed
from the canonical manifest *without* the hash field itself; callers persist
the returned hash alongside the manifest.  Keeping the hash outside the
manifest avoids a self-referential value while still making the pair
content-addressed and deterministic.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Mapping

from agent.provider import (hash_provider_endpoint, provider_endpoint_hash,
                             provider_endpoint_identity,
                             resolve_provider_endpoint, safe_provider_endpoint)

ARTIFACT_SCHEMA = "deployable_artifact.v1"
# This is a deliberately narrow, versioned source boundary.  ``research``
# contains a large amount of offline analysis code; only these two modules
# participate in constructing and validating the live authorization packet.
RUNTIME_SOURCE_SCOPE_VERSION = "v2"
RUNTIME_SOURCE_SCOPE = (
    "main.py", "agent/**/*.py", "research/artifact.py", "research/findings.py",
)
_HEX64 = set("0123456789abcdef")
_FULL_DIGEST_FIELDS = (
    "prompt_hash", "prompt_inputs_hash", "validated_config_hash",
    "runtime_source_hash", "variant_definition_hash",
    "forward_model_assumptions_hash", "deployment_config_hash",
    "llm_endpoint_hash",
)


class ArtifactValidationError(ValueError):
    """The artifact manifest is incomplete, malformed, or mismatched."""


def _json_safe(value: object):
    """Convert values to deterministic JSON-safe values.

    The findings store historically renders non-finite floats as strings.  We
    retain that behaviour here so packet construction cannot accidentally
    depend on the platform JSON encoder's handling of NaN/Infinity.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    raise TypeError(
        f"unsupported canonical JSON value type: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Return the one canonical JSON representation used by this boundary."""
    return json.dumps(
        _json_safe(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False)


def canonicalize(value: object):
    """Round-trip a value through canonical JSON for stable comparisons."""
    return json.loads(canonical_json(value))


def sha256_json(value: object) -> str:
    """SHA-256 of canonical JSON, always the full 64-character digest."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    """Full SHA-256 of the exact UTF-8 prompt bytes sent to the provider."""
    if not isinstance(value, str):
        raise TypeError("prompt text must be a string")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_full_digest(name: str, value: object) -> str:
    """Require one of the manifest's full, lowercase SHA-256 digests."""
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in _HEX64 for char in value)):
        raise ArtifactValidationError(
            f"artifact field {name} must be a full lowercase SHA-256 digest")
    return value


def _validate_strategy_config_version(value: object) -> str:
    """Validate a config version, accepting the runtime's 16-char prefix.

    Most callers use the 16-character lowercase prefix emitted by
    ``state.experiment_fingerprint``.  Human-readable version labels remain
    valid, but an all-hex value is treated as hash-shaped and must be either
    that accepted prefix or a full lowercase SHA-256 digest.
    """
    if not isinstance(value, str) or not value.strip():
        raise ArtifactValidationError(
            "artifact field strategy_config_version is required")
    if all(char in _HEX64 for char in value):
        if len(value) not in (16, 64):
            raise ArtifactValidationError(
                "artifact field strategy_config_version is not a valid "
                "SHA-256 prefix or digest")
    return value


def _validate_variant_definition_identity(
        definition: Mapping[str, object], *, variant_id: str,
        strategy_id: str, strategy_version: str) -> None:
    """Ensure the inner registry identity cannot be unrelated to its header."""
    expected = {
        "variant_id": str(variant_id),
        "strategy_id": str(strategy_id),
        "base_version": str(strategy_version),
    }
    for field, expected_value in expected.items():
        value = definition.get(field)
        if not isinstance(value, str) or value != expected_value:
            raise ArtifactValidationError(
                f"variant definition {field} does not match artifact")


def runtime_source_files(root: str | Path | None = None) -> list[Path]:
    """Return the deterministic source set covered by the runtime hash.

    The scope is intentionally explicit: ``main.py`` and every runtime agent
    module, plus the artifact/findings persistence boundary used by live
    authorization.  Other research modules are offline-only and must not
    influence a deployable artifact.
    """
    repo = (Path(root) if root is not None
            else Path(__file__).resolve().parents[1]).resolve()

    def inside_repo(path: Path) -> bool:
        try:
            path.resolve().relative_to(repo)
            return True
        except ValueError:
            return False

    paths: set[Path] = set()
    main = repo / "main.py"
    if main.is_file() and inside_repo(main):
        paths.add(main)
    agent = repo / "agent"
    if agent.is_dir():
        paths.update(
            path for path in agent.rglob("*.py")
            if path.is_file() and inside_repo(path))
    for relative in ("research/artifact.py", "research/findings.py"):
        path = repo / relative
        if path.is_file() and inside_repo(path):
            paths.add(path)
    return sorted(paths, key=lambda path: path.relative_to(repo).as_posix())


def runtime_source_hash(root: str | Path | None = None) -> str:
    """Hash every load-bearing runtime source in the explicit scope.

    Relative paths are included before bytes so renaming a source file changes
    identity even when its contents do not.  The scope intentionally excludes
    research code other than the artifact/findings boundary, data, bytecode,
    and repository metadata.
    """
    repo = (Path(root) if root is not None
            else Path(__file__).resolve().parents[1]).resolve()
    digest = hashlib.sha256()
    for path in runtime_source_files(repo):
        digest.update(path.relative_to(repo).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def artifact_sha256(manifest: Mapping[str, object]) -> str:
    """Return the full hash of a manifest, ignoring an embedded hash field.

    Accepting either spelling makes verification tolerant of an executor that
    stores the digest inside the manifest while the packet stores it beside
    the manifest.  The canonical packet representation uses the outer field.
    """
    unsigned = dict(manifest)
    unsigned.pop("artifact_hash", None)
    unsigned.pop("artifact_sha256", None)
    return sha256_json(unsigned)


def _validate_config_identity(
        config: Mapping[str, object], strategy_id: str,
        strategy_version: str) -> None:
    strategy = config.get("strategy")
    if not isinstance(strategy, Mapping):
        raise ArtifactValidationError(
            "validated config must contain a strategy mapping")
    if (str(strategy.get("id") or "") != str(strategy_id)
            or str(strategy.get("version") or "") != str(strategy_version)):
        raise ArtifactValidationError(
            "validated config strategy identity does not match artifact")


def build_manifest(
        *, strategy_id: str, strategy_version: str, variant_id: str,
        variant_definition_hash: str, config: Mapping[str, object],
        strategy_config_version: str, forward_model_id: str,
        forward_model_assumptions_hash: str,
        prompt_hash: str, llm_provider: str, llm_model: str,
        prompt_inputs_hash: str, deployment_config_hash: str,
        variant_definition: Mapping[str, object] | None = None,
        diagnostic_prompt_version: str | None = None,
        diagnostic_model_id: str | None = None,
        root: str | Path | None = None) -> tuple[dict, str]:
    """Build and hash one ``deployable_artifact.v1`` manifest.

    ``config`` is expected to be the fully validated executable config.  Only
    its hash is persisted, so credentials or other config values are never
    copied into the packet.  Optional diagnostic fields are included only when
    already available to the caller.
    """
    required = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "variant_id": variant_id,
        "variant_definition_hash": variant_definition_hash,
        "strategy_config_version": strategy_config_version,
        "forward_model_id": forward_model_id,
        "forward_model_assumptions_hash": forward_model_assumptions_hash,
        "prompt_hash": prompt_hash,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "prompt_inputs_hash": prompt_inputs_hash,
        "deployment_config_hash": deployment_config_hash,
    }
    if any(not isinstance(value, str) or not value.strip()
           for value in required.values()):
        raise ArtifactValidationError("artifact identity fields are required")
    if not isinstance(config, Mapping):
        raise ArtifactValidationError("validated config must be a mapping")
    _validate_config_identity(config, str(strategy_id), str(strategy_version))
    llm = config.get("llm")
    if not isinstance(llm, Mapping):
        raise ArtifactValidationError("validated config must contain llm identity")
    if (str(llm.get("provider") or "") != str(llm_provider)
            or str(llm.get("model") or "") != str(llm_model)):
        raise ArtifactValidationError(
            "validated config LLM identity does not match artifact")
    for field, value in (
            ("variant_definition_hash", variant_definition_hash),
            ("forward_model_assumptions_hash",
             forward_model_assumptions_hash),
            ("prompt_hash", prompt_hash),
            ("prompt_inputs_hash", prompt_inputs_hash),
            ("deployment_config_hash", deployment_config_hash)):
        _require_full_digest(field, value)
    _validate_strategy_config_version(strategy_config_version)
    try:
        effective_endpoint = resolve_provider_endpoint(llm_provider, llm)
        llm_endpoint = safe_provider_endpoint(effective_endpoint)
        llm_endpoint_hash = hash_provider_endpoint(effective_endpoint)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(
            "validated config LLM endpoint is invalid") from exc

    validated_config_hash = sha256_json(config)
    source_hash = runtime_source_hash(root)
    _require_full_digest("validated_config_hash", validated_config_hash)
    _require_full_digest("runtime_source_hash", source_hash)

    manifest: dict[str, object] = {
        "schema": ARTIFACT_SCHEMA,
        "strategy_id": str(strategy_id),
        "strategy_version": str(strategy_version),
        "variant_id": str(variant_id),
        "variant_definition_hash": str(variant_definition_hash),
        "validated_config_hash": validated_config_hash,
        "strategy_config_version": str(strategy_config_version),
        "runtime_source_hash": source_hash,
        "runtime_source_scope": list(RUNTIME_SOURCE_SCOPE),
        "runtime_source_scope_version": RUNTIME_SOURCE_SCOPE_VERSION,
        "forward_model_id": str(forward_model_id),
        "forward_model_assumptions_hash": str(
            forward_model_assumptions_hash),
        "prompt_hash": str(prompt_hash),
        "llm_provider": str(llm_provider),
        "llm_model": str(llm_model),
        "llm_endpoint": llm_endpoint,
        "llm_endpoint_hash": llm_endpoint_hash,
        "prompt_inputs_hash": str(prompt_inputs_hash),
        "deployment_config_hash": str(deployment_config_hash),
    }
    if not isinstance(variant_definition, Mapping):
        raise ArtifactValidationError("variant definition must be a mapping")
    _validate_variant_definition_identity(
        variant_definition, variant_id=str(variant_id),
        strategy_id=str(strategy_id), strategy_version=str(strategy_version))
    if sha256_json(variant_definition) != str(variant_definition_hash):
        raise ArtifactValidationError(
            "variant definition hash does not match supplied definition")
    manifest["variant_definition"] = canonicalize(variant_definition)
    if diagnostic_prompt_version:
        manifest["diagnostic_prompt_version"] = str(diagnostic_prompt_version)
    if diagnostic_model_id:
        manifest["diagnostic_model_id"] = str(diagnostic_model_id)
    manifest = canonicalize(manifest)
    digest = artifact_sha256(manifest)
    # Keep the digest in the manifest as an audit convenience as well as in
    # the packet's dedicated field.  ``artifact_sha256`` explicitly excludes
    # this field, so the value remains non-self-referential and deterministic.
    manifest["artifact_hash"] = digest
    return canonicalize(manifest), digest


def validate_manifest(
        manifest: object, artifact_hash: object | None = None, *,
        config: Mapping[str, object] | None = None,
        deployment_config_hash: str | None = None,
        expected: Mapping[str, object] | None = None,
        forward_model: Mapping[str, object] | None = None,
        root: str | Path | None = None,
        check_runtime_source: bool = True) -> dict:
    """Validate a manifest and return its canonical form.

    ``expected`` is a field-to-value mapping supplied by packet/runtime
    callers.  Legacy packets without a manifest are not upgraded; callers can
    treat a missing value as an unbound artifact before invoking this helper.
    """
    if not isinstance(manifest, Mapping):
        raise ArtifactValidationError("artifact manifest is not a mapping")
    if not isinstance(config, Mapping):
        raise ArtifactValidationError(
            "validated config is required to verify an artifact")
    canonical = canonicalize(manifest)
    if canonical.get("schema") != ARTIFACT_SCHEMA:
        raise ArtifactValidationError("unsupported artifact manifest schema")
    required = (
        "strategy_id", "strategy_version", "variant_id",
        "variant_definition_hash", "validated_config_hash",
        "strategy_config_version", "runtime_source_hash",
        "forward_model_id", "forward_model_assumptions_hash",
        "prompt_hash", "llm_provider", "llm_model", "prompt_inputs_hash",
        "deployment_config_hash", "llm_endpoint", "llm_endpoint_hash",
    )
    for name in required:
        value = canonical.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ArtifactValidationError(f"artifact field {name} is required")
    for name in _FULL_DIGEST_FIELDS:
        _require_full_digest(name, canonical.get(name))
    _validate_strategy_config_version(canonical["strategy_config_version"])
    if (deployment_config_hash is not None
            and canonical["deployment_config_hash"]
            != deployment_config_hash):
        raise ArtifactValidationError(
            "deployment config hash does not match artifact")
    if (canonical.get("runtime_source_scope") != list(RUNTIME_SOURCE_SCOPE)
            or canonical.get("runtime_source_scope_version")
            != RUNTIME_SOURCE_SCOPE_VERSION):
        raise ArtifactValidationError("artifact runtime source scope changed")
    _validate_config_identity(
        config, canonical["strategy_id"], canonical["strategy_version"])
    llm = config.get("llm")
    if (not isinstance(llm, Mapping)
            or str(llm.get("provider") or "") != canonical["llm_provider"]
            or str(llm.get("model") or "") != canonical["llm_model"]):
        raise ArtifactValidationError("validated config LLM identity changed")
    try:
        effective_endpoint = resolve_provider_endpoint(
            canonical["llm_provider"], llm)
        expected_endpoint = safe_provider_endpoint(effective_endpoint)
        expected_endpoint_hash = hash_provider_endpoint(effective_endpoint)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(
            "validated config LLM endpoint is invalid") from exc
    if canonical["llm_endpoint"] != expected_endpoint:
        raise ArtifactValidationError("validated config LLM endpoint changed")
    if canonical["llm_endpoint_hash"] != expected_endpoint_hash:
        raise ArtifactValidationError(
            "validated config LLM endpoint hash changed")
    if sha256_json(config) != canonical["validated_config_hash"]:
        raise ArtifactValidationError(
            "validated config hash does not match artifact")
    definition = canonical.get("variant_definition")
    if not isinstance(definition, Mapping):
        raise ArtifactValidationError("variant definition identity is missing")
    _validate_variant_definition_identity(
        definition, variant_id=canonical["variant_id"],
        strategy_id=canonical["strategy_id"],
        strategy_version=canonical["strategy_version"])
    if sha256_json(definition) != canonical["variant_definition_hash"]:
        raise ArtifactValidationError(
            "variant definition identity does not match")
    computed = artifact_sha256(canonical)
    embedded = canonical.get("artifact_hash") or canonical.get("artifact_sha256")
    if embedded is not None and embedded != computed:
        raise ArtifactValidationError("embedded artifact hash does not match")
    if artifact_hash is not None and artifact_hash != computed:
        raise ArtifactValidationError("artifact hash does not match manifest")
    if forward_model is not None:
        if (forward_model.get("model_id") != canonical["forward_model_id"]
                or sha256_json(forward_model)
                != canonical["forward_model_assumptions_hash"]):
            raise ArtifactValidationError(
                "forward model identity does not match artifact")
    if check_runtime_source and canonical["runtime_source_hash"] != runtime_source_hash(root):
        raise ArtifactValidationError("runtime source hash does not match current sources")
    for name, expected_value in (expected or {}).items():
        if expected_value is not None and canonical.get(name) != expected_value:
            raise ArtifactValidationError(f"artifact field {name} does not match")
    return canonical


# Descriptive aliases keep the runtime-facing contract discoverable without
# creating a second implementation (all callers still share the functions
# above and therefore the exact same canonicalization rules).
hash_manifest = artifact_sha256
build_deployable_artifact = build_manifest
verify_manifest = validate_manifest
