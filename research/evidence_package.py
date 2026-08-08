"""Content-addressed, research-only evidence packages and replay fixtures.

An evidence package is deliberately less capable than a deployment artifact.
It records the exact inputs and outputs of a research run, and nothing in this
module can promote a strategy, change configuration, or place an order.

The package directory is named by the SHA-256 of its canonical manifest.  All
file inputs are copied into a deduplicated ``blobs/`` directory, so verification
does not depend on the original files continuing to exist.  Callers may also
ask verification to compare the package with a current code tree and config;
that comparison fails closed on drift.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence


SCHEMA = "research-evidence-package.v1"
REPLAY_FIXTURE_SCHEMA = "research-replay-fixture.v1"
GOLDEN_OUTPUT_SCHEMA = "research-golden-replay-output.v1"
PROVENANCE_CLASSES = frozenset({"synthetic", "real_market"})
_DIGEST_LENGTH = 64
_DEFAULT_MAX_PARENT_DEPTH = 32
GOLDEN_REPLAY_NORMALIZATION = "python-ast-no-docstrings.v1"
_GOLDEN_REPLAY_CORE_SCOPE = (
    "agent/contracts/__init__.py",
    "agent/forward_models.py",
    "agent/registry.py",
    "agent/risk.py",
    "agent/strategy.py",
    "research/corpus.py",
    "research/replay.py",
)
_MANIFEST_FIELDS = frozenset({
    "schema", "purpose", "capabilities", "dataset", "code_tree",
    "strategy_contract", "forward_contract", "config", "economics",
    "prompt", "runtime", "command", "outputs", "timestamps",
    "parent_evidence_ids", "evidence_id",
})


class EvidenceValidationError(ValueError):
    """The evidence is incomplete, inconsistent, or no longer intact."""


@dataclass(frozen=True)
class DatasetEvidence:
    path: str | Path
    format: str
    provenance: Mapping[str, object]
    expected_records: int
    observed_records: int
    gaps: Sequence[Mapping[str, object]] = field(default_factory=tuple)


@dataclass(frozen=True)
class ContractEvidence:
    identity: str
    paths: Sequence[str | Path]


@dataclass(frozen=True)
class PromptEvidence:
    applicable: bool
    provider: str | None = None
    model: str | None = None
    prompt_path: str | Path | None = None
    inputs_path: str | Path | None = None
    reason: str | None = None


@dataclass(frozen=True)
class EvidenceSpec:
    dataset: DatasetEvidence
    code_root: str | Path
    code_files: Sequence[str | Path]
    strategy_contract: ContractEvidence
    forward_contract: ContractEvidence
    config_path: str | Path
    config_identity: Mapping[str, object]
    economics: Mapping[str, object]
    prompt: PromptEvidence
    runtime: Mapping[str, object]
    command: Sequence[str]
    outputs: Sequence[str | Path]
    timestamps: Mapping[str, str]
    parent_evidence_ids: Sequence[str] = field(default_factory=tuple)


def canonical_json(value: object) -> str:
    """Strict canonical JSON used for every identity in this module."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvidenceValidationError(
            f"evidence is not strict JSON: {exc}") from exc


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_runtime(distributions: Sequence[str] = ()) -> dict:
    """Return an explicit, secret-free runtime identity for a package.

    Dependency versions are opt-in because recording every installed package
    makes evidence depend on unrelated developer tooling.  Integrators should
    name the load-bearing distributions for their run.
    """
    versions = {}
    for name in sorted(set(str(item) for item in distributions)):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise EvidenceValidationError(
                f"runtime distribution is not installed: {name}") from exc
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "dependencies": versions,
    }


def _require_nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _require_digest(value: object, field_name: str) -> str:
    text = _require_nonempty(value, field_name).lower()
    if len(text) != _DIGEST_LENGTH or any(
            char not in "0123456789abcdef" for char in text):
        raise EvidenceValidationError(
            f"{field_name} must be a full lowercase SHA-256 digest")
    return text


def _parse_timestamp(value: object, field_name: str) -> datetime:
    text = _require_nonempty(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceValidationError(
            f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceValidationError(f"{field_name} must include a UTC offset")
    return parsed


def validate_provenance(provenance: Mapping[str, object]) -> dict:
    """Validate a truthful, explicit dataset classification.

    A real-market label is an attestation, not a conclusion inferred from the
    shape of prices.  It therefore requires a hash-bound source identity and
    collection window.  Synthetic evidence is required to name its generator
    and carry an unambiguous disclaimer.
    """
    if not isinstance(provenance, Mapping):
        raise EvidenceValidationError("dataset provenance must be a mapping")
    result = dict(provenance)
    classification = result.get("classification")
    if classification not in PROVENANCE_CLASSES:
        raise EvidenceValidationError(
            "dataset provenance.classification must be 'synthetic' or "
            "'real_market'")
    if classification == "synthetic":
        _require_nonempty(result.get("generator"), "provenance.generator")
        disclaimer = _require_nonempty(
            result.get("disclaimer"), "provenance.disclaimer")
        if "synthetic" not in disclaimer.lower():
            raise EvidenceValidationError(
                "synthetic provenance disclaimer must say synthetic")
    else:
        for key in ("venue", "source", "collection_started_at",
                    "collection_completed_at", "source_sha256"):
            _require_nonempty(result.get(key), f"provenance.{key}")
        started = _parse_timestamp(
            result["collection_started_at"],
            "provenance.collection_started_at")
        completed = _parse_timestamp(
            result["collection_completed_at"],
            "provenance.collection_completed_at")
        if completed < started:
            raise EvidenceValidationError(
                "provenance collection window ends before it starts")
        _require_digest(result["source_sha256"], "provenance.source_sha256")
    canonical_json(result)
    return result


def _validate_coverage(coverage: Mapping[str, object]) -> dict:
    if not isinstance(coverage, Mapping):
        raise EvidenceValidationError("dataset coverage must be a mapping")
    expected = coverage.get("expected_records")
    observed = coverage.get("observed_records")
    gaps = coverage.get("gaps")
    if (isinstance(expected, bool) or not isinstance(expected, int)
            or expected <= 0):
        raise EvidenceValidationError(
            "coverage.expected_records must be a positive integer")
    if (isinstance(observed, bool) or not isinstance(observed, int)
            or observed < 0):
        raise EvidenceValidationError(
            "coverage.observed_records must be a non-negative integer")
    if not isinstance(gaps, list):
        raise EvidenceValidationError("coverage.gaps must be a list")
    if observed != expected:
        raise EvidenceValidationError(
            "dataset coverage is incomplete: observed_records does not match "
            "expected_records")
    if gaps:
        raise EvidenceValidationError(
            "dataset coverage contains gaps and cannot be authoritative")
    return {
        "expected_records": expected,
        "observed_records": observed,
        "gaps": [],
    }


def _regular_file(path: str | Path, field_name: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise EvidenceValidationError(
            f"{field_name} is missing or is not a regular file: {candidate}")
    return candidate


def _artifact(path: Path, *, name: str | None = None) -> dict:
    return {
        "name": name or path.name,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


def _copy_blob(path: Path, blobs: Path, descriptor: Mapping[str, object]) -> None:
    digest = str(descriptor["sha256"])
    size = int(descriptor["size"])
    destination = blobs / digest
    if destination.exists():
        if (destination.stat().st_size != size
                or sha256_file(destination) != digest):
            raise EvidenceValidationError(
                f"temporary blob collision for {digest}")
        return
    with path.open("rb") as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
        target.flush()
        os.fsync(target.fileno())
    # Re-hash the destination, rather than trusting the source descriptor or
    # copy operation.  A partially written/corrupt blob must never become part
    # of a package that is subsequently published under its manifest ID.
    if destination.stat().st_size != size or sha256_file(destination) != digest:
        raise EvidenceValidationError(
            f"freshly copied evidence blob failed hash verification: {digest}")


def _relative_code_files(
        root: Path,
        paths: Sequence[str | Path]) -> list[tuple[Path, str]]:
    if not paths:
        raise EvidenceValidationError("code_files must not be empty")
    result = []
    resolved_root = root.resolve()
    for item in paths:
        candidate = Path(item)
        candidate = candidate if candidate.is_absolute() else root / candidate
        candidate = _regular_file(candidate, "code file").resolve()
        try:
            relative = candidate.relative_to(resolved_root).as_posix()
        except ValueError as exc:
            raise EvidenceValidationError(
                f"code file is outside code_root: {candidate}") from exc
        result.append((candidate, relative))
    unique = {relative: path for path, relative in result}
    if len(unique) != len(result):
        raise EvidenceValidationError("code_files contains duplicate paths")
    return [(unique[name], name) for name in sorted(unique)]


def _contract_manifest(
        contract: ContractEvidence, *, field_name: str,
        code_root: Path, code_index: Mapping[str, Mapping[str, object]]) -> dict:
    identity = _require_nonempty(contract.identity, f"{field_name}.identity")
    if not contract.paths:
        raise EvidenceValidationError(f"{field_name}.paths must not be empty")
    artifacts = []
    for item in contract.paths:
        candidate = Path(item)
        candidate = candidate if candidate.is_absolute() else code_root / candidate
        candidate = _regular_file(candidate, field_name).resolve()
        try:
            relative = candidate.relative_to(code_root.resolve()).as_posix()
        except ValueError as exc:
            raise EvidenceValidationError(
                f"{field_name} file is outside code_root: {candidate}") from exc
        descriptor = code_index.get(relative)
        if descriptor is None:
            raise EvidenceValidationError(
                f"{field_name} file must also appear in code_files: {relative}")
        artifacts.append({"path": relative, **dict(descriptor)})
    artifacts.sort(key=lambda item: str(item["path"]))
    return {
        "identity": identity,
        "artifacts": artifacts,
        "contract_sha256": sha256_json(artifacts),
    }


def _prompt_manifest(prompt: PromptEvidence, blobs: Path) -> dict:
    if not isinstance(prompt.applicable, bool):
        raise EvidenceValidationError("prompt.applicable must be boolean")
    if not prompt.applicable:
        return {
            "applicable": False,
            "reason": _require_nonempty(prompt.reason, "prompt.reason"),
        }
    provider = _require_nonempty(prompt.provider, "prompt.provider")
    model = _require_nonempty(prompt.model, "prompt.model")
    prompt_path = _regular_file(prompt.prompt_path or "", "prompt.prompt_path")
    prompt_artifact = _artifact(prompt_path)
    _copy_blob(prompt_path, blobs, prompt_artifact)
    result = {
        "applicable": True,
        "provider": provider,
        "model": model,
        "prompt": prompt_artifact,
    }
    if prompt.inputs_path is not None:
        inputs_path = _regular_file(prompt.inputs_path, "prompt.inputs_path")
        inputs_artifact = _artifact(inputs_path)
        _copy_blob(inputs_path, blobs, inputs_artifact)
        result["inputs"] = inputs_artifact
    return result


def _validate_timestamps(timestamps: Mapping[str, str]) -> dict:
    if not isinstance(timestamps, Mapping):
        raise EvidenceValidationError("timestamps must be a mapping")
    if set(timestamps) != {"started_at", "completed_at"}:
        raise EvidenceValidationError(
            "timestamps must contain exactly started_at and completed_at")
    started = _parse_timestamp(timestamps["started_at"], "timestamps.started_at")
    completed = _parse_timestamp(
        timestamps["completed_at"], "timestamps.completed_at")
    if completed < started:
        raise EvidenceValidationError("evidence completed before it started")
    return dict(timestamps)


def _manifest_for(spec: EvidenceSpec, blobs: Path) -> dict:
    dataset_path = _regular_file(spec.dataset.path, "dataset.path")
    provenance = validate_provenance(spec.dataset.provenance)
    coverage = _validate_coverage({
        "expected_records": spec.dataset.expected_records,
        "observed_records": spec.dataset.observed_records,
        "gaps": list(spec.dataset.gaps),
    })
    dataset_artifact = _artifact(dataset_path)
    _copy_blob(dataset_path, blobs, dataset_artifact)

    code_root = Path(spec.code_root)
    if not code_root.is_dir():
        raise EvidenceValidationError(f"code_root is not a directory: {code_root}")
    code_files = _relative_code_files(code_root, spec.code_files)
    code_manifest = []
    code_index = {}
    for path, relative in code_files:
        descriptor = _artifact(path, name=relative)
        _copy_blob(path, blobs, descriptor)
        without_name = {key: descriptor[key] for key in ("sha256", "size")}
        code_index[relative] = without_name
        code_manifest.append({"path": relative, **without_name})

    strategy = _contract_manifest(
        spec.strategy_contract, field_name="strategy_contract",
        code_root=code_root, code_index=code_index)
    forward = _contract_manifest(
        spec.forward_contract, field_name="forward_contract",
        code_root=code_root, code_index=code_index)

    config_path = _regular_file(spec.config_path, "config_path")
    config_identity = dict(spec.config_identity)
    for key in ("strategy_id", "strategy_version", "config_id"):
        _require_nonempty(config_identity.get(key), f"config_identity.{key}")
    canonical_json(config_identity)
    config_artifact = _artifact(config_path)
    _copy_blob(config_path, blobs, config_artifact)

    if not isinstance(spec.economics, Mapping):
        raise EvidenceValidationError("economics must be a mapping")
    economics = dict(spec.economics)
    for key in ("fees", "slippage", "funding"):
        if not isinstance(economics.get(key), Mapping) or not economics[key]:
            raise EvidenceValidationError(
                f"economics.{key} must be a non-empty mapping")
    economics_sha256 = sha256_json(economics)

    if not isinstance(spec.runtime, Mapping) or not spec.runtime:
        raise EvidenceValidationError("runtime must be a non-empty mapping")
    runtime = dict(spec.runtime)
    runtime_sha256 = sha256_json(runtime)

    command = list(spec.command)
    if not command or not all(isinstance(item, str) and item for item in command):
        raise EvidenceValidationError("command must be a non-empty argv list")

    if not spec.outputs:
        raise EvidenceValidationError("outputs must not be empty")
    outputs = []
    for item in spec.outputs:
        output_path = _regular_file(item, "output")
        descriptor = _artifact(output_path)
        _copy_blob(output_path, blobs, descriptor)
        outputs.append(descriptor)
    outputs.sort(key=lambda item: (str(item["name"]), str(item["sha256"])))

    parents = sorted(set(str(item) for item in spec.parent_evidence_ids))
    for index, parent in enumerate(parents):
        _require_digest(parent, f"parent_evidence_ids[{index}]")

    base = {
        "schema": SCHEMA,
        "purpose": "research_evidence_only",
        "capabilities": {"promotion": False, "live_trading": False},
        "dataset": {
            "format": _require_nonempty(spec.dataset.format, "dataset.format"),
            "artifact": dataset_artifact,
            "provenance": provenance,
            "coverage": coverage,
        },
        "code_tree": {
            "files": code_manifest,
            "tree_sha256": sha256_json(code_manifest),
        },
        "strategy_contract": strategy,
        "forward_contract": forward,
        "config": {
            "identity": config_identity,
            "artifact": config_artifact,
        },
        "economics": {
            "assumptions": economics,
            "sha256": economics_sha256,
        },
        "prompt": _prompt_manifest(spec.prompt, blobs),
        "runtime": {"identity": runtime, "sha256": runtime_sha256},
        "command": {"argv": command, "sha256": sha256_json(command)},
        "outputs": outputs,
        "timestamps": _validate_timestamps(spec.timestamps),
        "parent_evidence_ids": parents,
    }
    base["evidence_id"] = sha256_json(base)
    return base


def _write_manifest(path: Path, manifest: Mapping[str, object]) -> None:
    payload = (canonical_json(manifest) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_evidence_package(
        store: str | Path, spec: EvidenceSpec, *,
        parent_package_root: str | Path | None = None,
        parent_lookup: Mapping[str, str | Path]
        | Callable[[str], str | Path | None] | None = None,
        max_parent_depth: int = _DEFAULT_MAX_PARENT_DEPTH) -> Path:
    """Create one package atomically and return its content-addressed path.

    Existing packages are never overwritten.  A concurrent writer producing
    the same identity is accepted only after the existing package verifies.
    """
    root = Path(store)
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise EvidenceValidationError(f"evidence store is not a directory: {root}")
    if (isinstance(max_parent_depth, bool)
            or not isinstance(max_parent_depth, int)
            or max_parent_depth < 0):
        raise EvidenceValidationError("max_parent_depth must be non-negative")
    if parent_package_root is not None and parent_lookup is not None:
        raise EvidenceValidationError(
            "provide parent_package_root or parent_lookup, not both")
    # Packages conventionally share one content-addressed store.  A caller
    # with a separately managed ancestry can pass an explicit root or lookup.
    ancestry_root = (Path(parent_package_root)
                     if parent_package_root is not None else root)
    temporary = Path(tempfile.mkdtemp(prefix=".evidence-", dir=root))
    try:
        blobs = temporary / "blobs"
        blobs.mkdir()
        manifest = _manifest_for(spec, blobs)
        evidence_id = str(manifest["evidence_id"])
        _write_manifest(temporary / "manifest.json", manifest)
        # Verify the bytes while still in the private directory.  Publishing a
        # directory and discovering a copy/hash defect afterwards would leave
        # an addressable but invalid package in the store.
        _verify_evidence_package(
            temporary, require_package_name=False)
        if manifest["parent_evidence_ids"]:
            _verify_parent_tree(
                manifest, package_root=ancestry_root,
                parent_lookup=parent_lookup,
                max_parent_depth=max_parent_depth,
                allow_unresolved_parents=False)
        _fsync_directory(blobs)
        _fsync_directory(temporary)
        destination = root / evidence_id
        try:
            os.rename(temporary, destination)
        except OSError:
            # POSIX platforms disagree on whether renaming onto a non-empty
            # directory reports EEXIST or ENOTEMPTY.  Treat it as the benign
            # same-content race only when the destination really exists and
            # independently verifies; every other error remains fatal.
            if not destination.is_dir():
                raise
            verify_evidence_package(
                destination, package_root=ancestry_root,
                parent_lookup=parent_lookup,
                max_parent_depth=max_parent_depth)
            shutil.rmtree(temporary)
        _fsync_directory(root)
        return destination
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _load_manifest(package: Path) -> dict:
    path = package / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise EvidenceValidationError("package manifest is missing")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError("package manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise EvidenceValidationError("package manifest must be an object")
    if manifest.get("schema") != SCHEMA:
        raise EvidenceValidationError("unsupported evidence package schema")
    if set(manifest) != _MANIFEST_FIELDS:
        raise EvidenceValidationError(
            "package manifest fields do not match its schema")
    return manifest


def verify_evidence_package(
        package: str | Path, *, code_root: str | Path | None = None,
        config_path: str | Path | None = None,
        package_root: str | Path | None = None,
        parent_lookup: Mapping[str, str | Path]
        | Callable[[str], str | Path | None] | None = None,
        max_parent_depth: int = _DEFAULT_MAX_PARENT_DEPTH,
        allow_unresolved_parents: bool = False) -> dict:
    """Verify package identity, all blobs, coverage, and optional inputs.

    A package that declares parents must be verified with an explicit package
    root or lookup.  This prevents a valid child from silently referring to a
    missing, tampered, cyclic, or over-deep ancestry.  The unresolved escape
    hatch is reserved for fixtures that intentionally model an external store.
    """
    manifest = _verify_evidence_package(
        package, code_root=code_root, config_path=config_path)
    if manifest["parent_evidence_ids"] or package_root is not None \
            or parent_lookup is not None:
        return verify_evidence_ancestry(
            package, package_root=package_root,
            parent_lookup=parent_lookup, code_root=code_root,
            config_path=config_path, max_parent_depth=max_parent_depth,
            allow_unresolved_parents=allow_unresolved_parents)
    return manifest


def _parent_path(
        parent_id: str, *, package_root: str | Path | None,
        parent_lookup: Mapping[str, str | Path] | Callable[[str], str | Path | None]
        | None) -> Path | None:
    if parent_lookup is not None:
        value = (parent_lookup(parent_id) if callable(parent_lookup)
                 else parent_lookup.get(parent_id))
        return None if value is None else Path(value)
    if package_root is None:
        return None
    return Path(package_root) / parent_id


def _verify_parent_tree(
        manifest: Mapping[str, object], *, package_root: str | Path | None,
        parent_lookup: Mapping[str, str | Path]
        | Callable[[str], str | Path | None] | None,
        max_parent_depth: int,
        allow_unresolved_parents: bool) -> None:
    """Verify declared parents recursively from an explicit source."""
    def visit(parent_id: str, depth: int,
              ancestors: frozenset[str]) -> None:
        if depth > max_parent_depth:
            raise EvidenceValidationError(
                "parent evidence ancestry exceeds maximum depth")
        candidate = _parent_path(
            parent_id, package_root=package_root, parent_lookup=parent_lookup)
        if candidate is None:
            if allow_unresolved_parents:
                return
            raise EvidenceValidationError(
                f"parent evidence package is not resolvable: {parent_id}")
        candidate_path = Path(candidate)
        if not candidate_path.exists():
            if allow_unresolved_parents:
                return
            raise EvidenceValidationError(
                f"parent evidence package is missing: {parent_id}")
        try:
            parent = _verify_evidence_package(candidate_path)
        except EvidenceValidationError:
            # A present parent is never tolerated as an unresolved fixture.
            raise
        evidence_id = str(parent["evidence_id"])
        if evidence_id != parent_id:
            raise EvidenceValidationError(
                "parent evidence package identity does not match declared ID: "
                f"{parent_id} != {evidence_id}")
        if evidence_id in ancestors:
            raise EvidenceValidationError(
                "parent evidence ancestry contains a cycle")
        current = ancestors | {evidence_id}
        for nested in parent["parent_evidence_ids"]:
            visit(str(nested), depth + 1, current)

    root_id = str(manifest["evidence_id"])
    ancestors = frozenset({root_id})
    for parent in manifest["parent_evidence_ids"]:
        visit(str(parent), 1, ancestors)


def verify_evidence_ancestry(
        package: str | Path, *,
        package_root: str | Path | None = None,
        parent_lookup: Mapping[str, str | Path]
        | Callable[[str], str | Path | None] | None = None,
        code_root: str | Path | None = None,
        config_path: str | Path | None = None,
        max_parent_depth: int = _DEFAULT_MAX_PARENT_DEPTH,
        allow_unresolved_parents: bool = False) -> dict:
    """Verify a package and every declared parent from an explicit source.

    A root is interpreted as ``root/<evidence_id>``; a lookup may instead
    return each package path directly.  Missing parents, cycles, and excessive
    depth fail closed.  ``allow_unresolved_parents`` is an explicit fixture
    escape hatch only: it permits missing parents but still verifies every
    parent that can be resolved and never suppresses cycle/depth failures.
    """
    if (isinstance(max_parent_depth, bool)
            or not isinstance(max_parent_depth, int)
            or max_parent_depth < 0):
        raise EvidenceValidationError("max_parent_depth must be non-negative")
    if package_root is None and parent_lookup is None:
        if not allow_unresolved_parents:
            raise EvidenceValidationError(
                "parent package root or lookup is required for ancestry verification")
        return _verify_evidence_package(
            package, code_root=code_root, config_path=config_path)

    manifest = _verify_evidence_package(package)
    _verify_parent_tree(
        manifest, package_root=package_root, parent_lookup=parent_lookup,
        max_parent_depth=max_parent_depth,
        allow_unresolved_parents=allow_unresolved_parents)
    # Verify the requested package against optional current inputs after the
    # ancestry walk; parent packages intentionally use their own snapshots.
    return _verify_evidence_package(
        package, code_root=code_root, config_path=config_path)


def _artifact_descriptors(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    descriptors = []
    try:
        descriptors.append(manifest["dataset"]["artifact"])
        descriptors.append(manifest["config"]["artifact"])
        descriptors.extend(manifest["code_tree"]["files"])
        descriptors.extend(manifest["strategy_contract"]["artifacts"])
        descriptors.extend(manifest["forward_contract"]["artifacts"])
        descriptors.extend(manifest["outputs"])
        prompt = manifest["prompt"]
        if prompt["applicable"]:
            descriptors.append(prompt["prompt"])
            if "inputs" in prompt:
                descriptors.append(prompt["inputs"])
    except (KeyError, TypeError) as exc:
        raise EvidenceValidationError(
            "manifest is missing required evidence inputs") from exc
    return descriptors


def _verify_blob(package: Path, descriptor: Mapping[str, object]) -> None:
    if not isinstance(descriptor, Mapping):
        raise EvidenceValidationError("artifact descriptor must be a mapping")
    digest = _require_digest(descriptor.get("sha256"), "artifact.sha256")
    size = descriptor.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise EvidenceValidationError("artifact.size must be non-negative")
    blob = package / "blobs" / digest
    if blob.is_symlink() or not blob.is_file():
        raise EvidenceValidationError(f"evidence input blob is missing: {digest}")
    if blob.stat().st_size != size or sha256_file(blob) != digest:
        raise EvidenceValidationError(f"evidence input blob was tampered: {digest}")


def _verify_current_code(
        manifest: Mapping[str, object], code_root: str | Path) -> None:
    root = Path(code_root)
    if not root.is_dir():
        raise EvidenceValidationError(f"current code root is missing: {root}")
    current = []
    for descriptor in manifest["code_tree"]["files"]:
        relative = _require_nonempty(descriptor.get("path"), "code path")
        candidate = _regular_file(root / relative, "current code file")
        current_descriptor = {
            "path": relative,
            "sha256": sha256_file(candidate),
            "size": candidate.stat().st_size,
        }
        current.append(current_descriptor)
    if sha256_json(current) != manifest["code_tree"]["tree_sha256"]:
        raise EvidenceValidationError("current code tree does not match evidence")


def _verify_evidence_package(
        package: str | Path, *, code_root: str | Path | None = None,
        config_path: str | Path | None = None,
        require_package_name: bool = True) -> dict:
    """Verify package identity, all blobs, coverage, and optional current inputs."""
    package_path = Path(package)
    if package_path.is_symlink() or not package_path.is_dir():
        raise EvidenceValidationError("evidence package directory is missing")
    manifest = _load_manifest(package_path)
    evidence_id = _require_digest(manifest.get("evidence_id"), "evidence_id")
    unsigned = dict(manifest)
    unsigned.pop("evidence_id", None)
    if (sha256_json(unsigned) != evidence_id
            or (require_package_name and package_path.name != evidence_id)):
        raise EvidenceValidationError("evidence manifest identity mismatch")
    if (manifest.get("purpose") != "research_evidence_only"
            or manifest.get("capabilities") != {
                "promotion": False, "live_trading": False}):
        raise EvidenceValidationError(
            "evidence package must remain research-only")

    try:
        validate_provenance(manifest["dataset"]["provenance"])
        _validate_coverage(manifest["dataset"]["coverage"])
        _validate_timestamps(manifest["timestamps"])
    except (KeyError, TypeError) as exc:
        raise EvidenceValidationError(
            "manifest is missing provenance, coverage, or timestamps") from exc
    for descriptor in _artifact_descriptors(manifest):
        _verify_blob(package_path, descriptor)

    code_files = manifest["code_tree"]["files"]
    if not isinstance(code_files, list) or not code_files:
        raise EvidenceValidationError("code tree has no source inputs")
    code_paths = [item.get("path") for item in code_files]
    if code_paths != sorted(set(code_paths)):
        raise EvidenceValidationError("code tree paths are not canonical")
    if sha256_json(code_files) != manifest["code_tree"]["tree_sha256"]:
        raise EvidenceValidationError("code tree identity mismatch")
    code_artifacts = {
        (item.get("path"), item.get("sha256"), item.get("size"))
        for item in code_files
    }
    for contract_name in ("strategy_contract", "forward_contract"):
        contract = manifest[contract_name]
        _require_nonempty(contract.get("identity"), f"{contract_name}.identity")
        if not contract.get("artifacts"):
            raise EvidenceValidationError(
                f"{contract_name} has no exact contract artifacts")
        if sha256_json(contract["artifacts"]) != contract["contract_sha256"]:
            raise EvidenceValidationError(f"{contract_name} identity mismatch")
        for item in contract["artifacts"]:
            identity = (item.get("path"), item.get("sha256"), item.get("size"))
            if identity not in code_artifacts:
                raise EvidenceValidationError(
                    f"{contract_name} is not part of the recorded code tree")
    config_identity = manifest["config"].get("identity")
    if not isinstance(config_identity, Mapping):
        raise EvidenceValidationError("config identity is missing")
    for key in ("strategy_id", "strategy_version", "config_id"):
        _require_nonempty(config_identity.get(key), f"config.identity.{key}")
    economics = manifest["economics"]
    for key in ("fees", "slippage", "funding"):
        if (not isinstance(economics["assumptions"].get(key), Mapping)
                or not economics["assumptions"][key]):
            raise EvidenceValidationError(f"economics.{key} is missing")
    if sha256_json(economics["assumptions"]) != economics["sha256"]:
        raise EvidenceValidationError("economics identity mismatch")
    if (not isinstance(manifest["runtime"].get("identity"), Mapping)
            or not manifest["runtime"]["identity"]):
        raise EvidenceValidationError("runtime identity is missing")
    if sha256_json(manifest["runtime"]["identity"]) != manifest["runtime"]["sha256"]:
        raise EvidenceValidationError("runtime identity mismatch")
    argv = manifest["command"].get("argv")
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(item, str) and item for item in argv)):
        raise EvidenceValidationError("command argv is missing")
    if sha256_json(manifest["command"]["argv"]) != manifest["command"]["sha256"]:
        raise EvidenceValidationError("command identity mismatch")
    if not isinstance(manifest.get("outputs"), list) or not manifest["outputs"]:
        raise EvidenceValidationError("evidence outputs are missing")

    prompt = manifest["prompt"]
    if prompt.get("applicable") is False:
        _require_nonempty(prompt.get("reason"), "prompt.reason")
    elif prompt.get("applicable") is True:
        _require_nonempty(prompt.get("provider"), "prompt.provider")
        _require_nonempty(prompt.get("model"), "prompt.model")
    else:
        raise EvidenceValidationError("prompt applicability is missing")

    parents = manifest.get("parent_evidence_ids")
    if not isinstance(parents, list) or parents != sorted(set(parents)):
        raise EvidenceValidationError("parent evidence IDs are not canonical")
    for index, parent in enumerate(parents):
        _require_digest(parent, f"parent_evidence_ids[{index}]")

    if code_root is not None:
        _verify_current_code(manifest, code_root)
    if config_path is not None:
        current_config = _regular_file(config_path, "current config")
        descriptor = manifest["config"]["artifact"]
        if (sha256_file(current_config) != descriptor["sha256"]
                or current_config.stat().st_size != descriptor["size"]):
            raise EvidenceValidationError(
                "current config does not match evidence")
    return manifest


# Backwards-compatible names retained for integrations that used the original
# concise package API.  They inherit the explicit ancestry checks above.
create_package = create_evidence_package
verify_package = verify_evidence_package


_SECRET_FRAGMENTS = (
    "api_key", "apikey", "secret", "password", "passphrase",
    "authorization", "webhook", "access_token", "refresh_token",
)


def _sanitized(value: object) -> object:
    if isinstance(value, Mapping):
        clean = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(fragment in normalized for fragment in _SECRET_FRAGMENTS):
                continue
            clean[str(key)] = _sanitized(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitized(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _atomic_write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (canonical_json(value) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise EvidenceValidationError(
                f"refusing to overwrite existing fixture: {path}") from exc
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fixture_coverage(value: Mapping[str, object]) -> dict:
    cycles = value.get("cycles")
    if not isinstance(cycles, list):
        raise EvidenceValidationError("replay fixture cycles must be a list")
    coverage = _validate_coverage(value.get("coverage"))
    if coverage["observed_records"] != len(cycles):
        raise EvidenceValidationError(
            "replay fixture observed_records does not match its cycles")
    return coverage


def load_replay_fixture(
        path: str | Path, *, require_classification: str | None = None) -> dict:
    """Load a sanitized replay fixture without upgrading its provenance."""
    source = _regular_file(path, "replay fixture")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError("replay fixture is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != REPLAY_FIXTURE_SCHEMA:
        raise EvidenceValidationError("unsupported replay fixture schema")
    provenance = validate_provenance(value.get("provenance"))
    _fixture_coverage(value)
    if require_classification is not None:
        if require_classification not in PROVENANCE_CLASSES:
            raise EvidenceValidationError("unknown required provenance class")
        if provenance["classification"] != require_classification:
            raise EvidenceValidationError(
                f"fixture is {provenance['classification']}, not "
                f"{require_classification}")
    if not isinstance(value.get("config"), Mapping):
        raise EvidenceValidationError("replay fixture config must be a mapping")
    if not isinstance(value.get("model_outputs"), list):
        raise EvidenceValidationError(
            "replay fixture model_outputs must be a list")
    replay = value.get("replay")
    if not isinstance(replay, Mapping):
        raise EvidenceValidationError("replay fixture replay block is missing")
    _require_nonempty(replay.get("variant_id"), "replay.variant_id")
    _require_nonempty(replay.get("mode"), "replay.mode")
    return value


def golden_replay_code_scope(config: Mapping[str, object]) -> tuple[str, ...]:
    """Return only source files executable by this fixture's replay path.

    The G2 fidelity identity is intentionally much broader: it invalidates on
    any agent source change because it protects a production evidence gate.
    A checked golden vector has a narrower job.  Its input/output comparison
    already proves behavior on that vector, so unrelated engine, deployment,
    staging, and CLI changes must not churn its expected output.
    """
    try:
        strategy_id = str(config["strategy"]["id"])
    except (KeyError, TypeError) as exc:
        raise EvidenceValidationError(
            "golden replay config has no strategy identity") from exc
    from agent.contracts import EVIDENCE_BUILDERS

    try:
        builder = EVIDENCE_BUILDERS[strategy_id]
    except KeyError as exc:
        raise EvidenceValidationError(
            f"golden replay strategy has no contract: {strategy_id}") from exc
    module = str(builder.__module__)
    if not module.startswith("agent.contracts."):
        raise EvidenceValidationError(
            "golden replay contract module is outside agent.contracts")
    contract_path = module.replace(".", "/") + ".py"
    return tuple(sorted(set((*_GOLDEN_REPLAY_CORE_SCOPE, contract_path))))


class _ExecutableAst(ast.NodeTransformer):
    """Remove only genuine leading docstrings from executable source ASTs."""

    @staticmethod
    def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            return body[1:]
        return body

    def _visit_body(self, node):
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        return node

    def visit_Module(self, node: ast.Module):  # noqa: N802 - AST API name
        return self._visit_body(node)

    def visit_ClassDef(self, node: ast.ClassDef):  # noqa: N802
        return self._visit_body(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
        return self._visit_body(node)

    def visit_AsyncFunctionDef(  # noqa: N802
            self, node: ast.AsyncFunctionDef):
        return self._visit_body(node)


def _normalized_python_ast(path: Path) -> bytes:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise EvidenceValidationError(
            f"golden replay source is not valid Python: {path}") from exc
    normalized = _ExecutableAst().visit(tree)
    # Locations are deliberately excluded: comments and blank lines may move
    # executable nodes without changing the program represented by the AST.
    return ast.dump(
        normalized, annotate_fields=True, include_attributes=False,
    ).encode("utf-8")


def golden_replay_code_fingerprint(
        config: Mapping[str, object], *, root: str | Path | None = None) -> dict:
    """Hash executable ASTs, excluding comments, docstrings, and unrelated files."""
    repository = (Path(root) if root is not None
                  else Path(__file__).resolve().parents[1]).resolve()
    scope = golden_replay_code_scope(config)
    digest = hashlib.sha256()
    digest.update(GOLDEN_REPLAY_NORMALIZATION.encode("utf-8"))
    digest.update(b"\0")
    for relative in scope:
        path = _regular_file(repository / relative, "golden replay source")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_normalized_python_ast(path))
        digest.update(b"\0")
    return {
        "normalization": GOLDEN_REPLAY_NORMALIZATION,
        "scope": list(scope),
        "sha256": digest.hexdigest(),
    }


def import_replay_fixture(
        source: str | Path, destination: str | Path, *,
        classification: str = "synthetic",
        provenance: Mapping[str, object] | None = None,
        config: Mapping[str, object] | None = None,
        variant_id: str = "UNSET", mode: str = "recorded_llm") -> Path:
    """Sanitize a journal or fixture into the canonical replay format.

    Existing synthetic fixtures can only remain synthetic.  A SQLite journal
    may be labelled real-market only when a separate hash-bound provenance
    attestation is supplied.  Raw model text is never exported; parsed
    decisions are sufficient for replay and avoid copying prompts or secrets.
    """
    source_path = _regular_file(source, "replay import source")
    if classification not in PROVENANCE_CLASSES:
        raise EvidenceValidationError("unknown replay provenance classification")

    parsed_json = None
    try:
        parsed_json = json.loads(source_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if (isinstance(parsed_json, dict)
            and parsed_json.get("schema") == REPLAY_FIXTURE_SCHEMA):
        existing = validate_provenance(parsed_json.get("provenance"))
        if (existing["classification"] == "synthetic"
                and classification == "real_market"):
            raise EvidenceValidationError(
                "a synthetic replay fixture cannot be relabelled real_market")
        sanitized = _sanitized(parsed_json)
        sanitized["provenance"] = existing
        _fixture_coverage(sanitized)
    else:
        from . import corpus

        if classification == "real_market":
            attestation = validate_provenance(provenance or {})
            if attestation["classification"] != "real_market":
                raise EvidenceValidationError(
                    "real_market import needs a real_market attestation")
            if attestation["source_sha256"] != sha256_file(source_path):
                raise EvidenceValidationError(
                    "real_market attestation does not match the source journal")
        else:
            attestation = validate_provenance(provenance or {
                "classification": "synthetic",
                "generator": "research.evidence_package.import_replay_fixture",
                "disclaimer": (
                    "Synthetic classification: no real-market provenance is "
                    "asserted for this imported fixture."),
            })
        try:
            cycles, cycle_report = corpus.load_cycles(source_path)
            model_outputs, output_report = corpus.load_model_outputs(source_path)
        except Exception as exc:  # noqa: BLE001 - converted to one safe boundary
            raise EvidenceValidationError(
                "replay import source is neither a fixture nor a readable "
                "journal") from exc
        gaps = corpus.uptime_gaps(cycles)
        serialized_cycles = []
        for cycle in cycles:
            serialized_cycles.append(_sanitized({
                "ts": cycle.ts,
                "run_id": cycle.run_id,
                "cycle_id": cycle.cycle_id,
                "snapshot": cycle.snapshot,
                "portfolio": cycle.portfolio,
                "max_new": cycle.max_new,
                "provider": cycle.provider,
                "variant_id": cycle.variant_id,
                "strategy_config_version": cycle.strategy_config_version,
                "enrichment": cycle.enrichment,
            }))
        serialized_outputs = []
        for output in model_outputs:
            serialized_outputs.append(_sanitized({
                "ts": output.ts,
                "cycle_id": output.cycle_id,
                "response_id": output.response_id,
                "raw_text_sha256": hashlib.sha256(
                    output.raw_text.encode("utf-8")).hexdigest(),
                "parsed_decisions": output.parsed_decisions,
                "request_attempts": output.request_attempts,
            }))
        sanitized = {
            "schema": REPLAY_FIXTURE_SCHEMA,
            "provenance": attestation,
            "coverage": {
                "expected_records": cycle_report.rows,
                "observed_records": cycle_report.parsed,
                "gaps": gaps,
                "cycle_skip_reasons": dict(cycle_report.reasons),
                "output_rows": output_report.rows,
                "output_parsed": output_report.parsed,
                "output_skip_reasons": dict(output_report.reasons),
            },
            "config": _sanitized(dict(config or {})),
            "replay": {
                "variant_id": _require_nonempty(
                    variant_id, "replay.variant_id"),
                "mode": _require_nonempty(mode, "replay.mode"),
            },
            "cycles": serialized_cycles,
            "model_outputs": serialized_outputs,
        }
    _fixture_coverage(sanitized)
    destination_path = Path(destination)
    _atomic_write_new(destination_path, sanitized)
    return destination_path


def replay_fixture(path: str | Path) -> dict:
    """Run one sanitized fixture through the production replay implementation."""
    from .corpus import CycleRecord, ModelOutput
    from .replay import Replay

    fixture = load_replay_fixture(path)
    cycles = [CycleRecord(**dict(item)) for item in fixture["cycles"]]
    outputs = []
    for item in fixture["model_outputs"]:
        output = dict(item)
        output.pop("raw_text_sha256", None)
        output["raw_text"] = ""
        outputs.append(ModelOutput(**output))
    replay_settings = fixture["replay"]
    result = Replay(
        dict(fixture["config"]),
        variant_id=str(replay_settings["variant_id"]),
        mode=str(replay_settings["mode"]),
    ).run(cycles, outputs)
    decisions = [{
        "cycle_id": decision.cycle_id,
        "symbol": decision.symbol,
        "stage": decision.stage,
        "direction": decision.direction,
        "setup_type": decision.setup_type,
        "reason": decision.reason,
        "stop_pct": decision.stop_pct,
        "take_pct": decision.take_pct,
    } for decision in result.decisions]
    canonical_fixture = json.loads(canonical_json(fixture))
    return {
        "schema": GOLDEN_OUTPUT_SCHEMA,
        "provenance_classification": fixture["provenance"]["classification"],
        "fixture_sha256": sha256_json(canonical_fixture),
        "config_sha256": sha256_json(fixture["config"]),
        "replay_code": golden_replay_code_fingerprint(fixture["config"]),
        "variant_id": result.variant_id,
        "mode": result.mode,
        "cycles": result.cycles,
        "digest": result.digest(),
        "funnel": result.funnel,
        "decisions": decisions,
    }


def verify_golden_replay(
        fixture_path: str | Path, expected_path: str | Path) -> dict:
    """Fail when replay output, code, config, or provenance no longer matches."""
    expected_file = _regular_file(expected_path, "golden replay expected output")
    try:
        expected = json.loads(expected_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError(
            "golden replay expected output is not valid JSON") from exc
    actual = replay_fixture(fixture_path)
    if expected != actual:
        raise EvidenceValidationError("golden replay output does not match")
    return actual
