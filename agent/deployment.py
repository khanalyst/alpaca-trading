"""Fail-closed authorization of a reviewed T3 artifact before live startup."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from research import artifact as artifact_mod
from research.findings import (FindingsStore, T3PacketResolutionError,
                               resolve_store_path)

from . import brain, provider, registry, state, strategy, variants
from .forward_models import require_complete_contract


class DeploymentAuthorizationError(RuntimeError):
    """The live process cannot be proven to match a reviewed artifact."""


def _prompt_inputs_hash(cfg: Mapping[str, object], catalog: dict) -> str:
    return artifact_mod.sha256_json({
        "catalog": catalog,
        "strategy": cfg.get("strategy") or {},
    })


def _variant_definition_from_row(row: Mapping[str, object]) -> dict:
    try:
        overrides = json.loads(str(row.get("overrides_json") or "{}"))
        hypothesis_params = json.loads(
            str(row.get("hypothesis_params_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeploymentAuthorizationError(
            "reviewed packet variant identity is malformed") from exc
    if not isinstance(overrides, Mapping) or not isinstance(
            hypothesis_params, Mapping):
        raise DeploymentAuthorizationError(
            "reviewed packet variant identity is malformed")
    return {
        "variant_id": str(row.get("variant_id") or ""),
        "strategy_id": str(row.get("strategy_id") or ""),
        "base_version": str(row.get("base_version") or ""),
        "overrides": overrides,
        "hypothesis": str(row.get("hypothesis") or ""),
        "hypothesis_id": str(row.get("hypothesis_id") or ""),
        "hypothesis_params": hypothesis_params,
    }


def verify_live_artifact(
        cfg: dict, *, catalog: dict | None = None,
        system_prompt: str | None = None,
        store: FindingsStore | None = None) -> dict:
    """Verify the exact packet, source, prompt, and deployment config.

    ``catalog`` and ``system_prompt`` are optional for direct callers, but the
    live Engine and ``main check`` pass one captured pair so verification and
    the eventual LLM request cannot observe different registry state.
    """
    if not isinstance(cfg, Mapping):
        raise DeploymentAuthorizationError(
            "live artifact authorization requires a config mapping")
    if cfg.get("mode") != "live":
        raise DeploymentAuthorizationError(
            "live artifact authorization requires mode=live")
    if (not isinstance(cfg.get("strategy"), Mapping)
            or not isinstance(cfg.get("llm"), Mapping)):
        raise DeploymentAuthorizationError(
            "live artifact strategy and LLM config must be mappings")
    captured_catalog = (
        variants.research_selection_catalog()
        if catalog is None else catalog)
    if not isinstance(captured_catalog, Mapping):
        raise DeploymentAuthorizationError(
            "live research catalog is malformed")
    prompt = (brain.build_system(cfg, catalog=captured_catalog)
              if system_prompt is None else str(system_prompt))
    prompt_hash = artifact_mod.sha256_text(prompt)
    prompt_inputs_hash = _prompt_inputs_hash(cfg, captured_catalog)
    strategy_id, strategy_version = strategy.identity(cfg)
    spec = registry.spec_for(strategy_id)
    citation = registry.require_t3_packet_reference(spec)
    if not citation:
        raise DeploymentAuthorizationError(
            f"strategy {strategy_id!r} has no live T3 packet citation")

    research_cfg = cfg.get("research") or {}
    if not isinstance(research_cfg, Mapping):
        raise DeploymentAuthorizationError(
            "live artifact research config is malformed")
    store_path = resolve_store_path(research_cfg.get("findings_store"))
    if not Path(store_path).is_file():
        raise DeploymentAuthorizationError(
            f"authoritative findings DB is missing: {store_path}")
    packet_store = store or FindingsStore(store_path)
    try:
        packet = packet_store.resolve_t3_packet(citation)
    except T3PacketResolutionError as exc:
        raise DeploymentAuthorizationError(str(exc)) from exc
    if packet is None:
        raise DeploymentAuthorizationError(
            f"reviewed T3 packet {citation} is not present in findings DB")
    if (not packet.get("content_addressed")
            or packet.get("review_status") != "REVIEWED"
            or not str(packet.get("reviewed_by") or "").strip()
            or not str(packet.get("registry_change_ref") or "").strip()):
        raise DeploymentAuthorizationError(
            "T3 packet is not a reviewed, content-addressed authorization")
    if not packet.get("artifact_bound"):
        raise DeploymentAuthorizationError(
            "T3 packet has no deployable artifact binding")

    payload = packet.get("payload")
    if not isinstance(payload, Mapping):
        raise DeploymentAuthorizationError("T3 packet payload is malformed")
    row_variant_id = packet.get("variant_id")
    payload_variant_id = payload.get("variant_id")
    if (not isinstance(row_variant_id, str)
            or not isinstance(payload_variant_id, str)
            or row_variant_id != payload_variant_id):
        raise DeploymentAuthorizationError(
            "T3 packet row and payload variant identities do not match")
    manifest = payload.get("artifact_manifest")
    outer_artifact_hash = payload.get("artifact_hash")
    if (not isinstance(manifest, Mapping)
            or not isinstance(outer_artifact_hash, str)):
        raise DeploymentAuthorizationError("T3 packet artifact is incomplete")
    if str(packet.get("payload_hash") or "") != citation.removeprefix("t3-packet:"):
        raise DeploymentAuthorizationError("T3 packet citation hash mismatch")
    if manifest.get("strategy_id") != strategy_id:
        raise DeploymentAuthorizationError("artifact strategy id does not match runtime")
    if manifest.get("strategy_version") != strategy_version:
        raise DeploymentAuthorizationError(
            "artifact strategy version does not match runtime")

    variant_id = manifest.get("variant_id")
    if (not isinstance(variant_id, str) or not variant_id
            or variant_id != row_variant_id
            or variant_id != payload_variant_id):
        raise DeploymentAuthorizationError(
            "T3 packet row, payload, and manifest variant identities do not match")
    variant_row = packet_store.variant(variant_id)
    if variant_row is None:
        raise DeploymentAuthorizationError(
            f"artifact variant {variant_id!r} is not registered")
    variant_definition = _variant_definition_from_row(variant_row)
    if manifest.get("variant_definition") != variant_definition:
        raise DeploymentAuthorizationError(
            "artifact variant definition does not match registry")
    if (manifest.get("variant_definition_hash")
            != artifact_mod.sha256_json(variant_definition)):
        raise DeploymentAuthorizationError(
            "artifact variant definition hash does not match registry")

    analysis = payload.get("forward_analysis")
    if not isinstance(analysis, Mapping):
        raise DeploymentAuthorizationError(
            "T3 packet forward analysis is malformed")
    analysis_payload = analysis.get("payload")
    if not isinstance(analysis_payload, Mapping):
        raise DeploymentAuthorizationError(
            "T3 packet forward analysis payload is malformed")
    source_evidence = analysis_payload.get("source_evidence")
    if not isinstance(source_evidence, Mapping):
        raise DeploymentAuthorizationError(
            "T3 packet source evidence is malformed")
    eligibility = source_evidence.get("eligibility")
    if not isinstance(eligibility, Mapping):
        raise DeploymentAuthorizationError(
            "T3 packet eligibility evidence is malformed")
    setting_provenances = eligibility.get("setting_provenances")
    if not isinstance(setting_provenances, Mapping):
        raise DeploymentAuthorizationError(
            "T3 packet candidate provenance is malformed")
    candidate_provenance = setting_provenances.get(variant_id)
    if not isinstance(candidate_provenance, Mapping):
        raise DeploymentAuthorizationError(
            "T3 packet candidate provenance is missing")
    candidate_config = candidate_provenance.get("experiment_config")
    if not isinstance(candidate_config, Mapping):
        raise DeploymentAuthorizationError(
            "T3 packet is missing persisted candidate experiment config")

    candidate_strategy = candidate_config.get("strategy")
    candidate_llm = candidate_config.get("llm")
    if (not isinstance(candidate_strategy, Mapping)
            or not isinstance(candidate_llm, Mapping)):
        raise DeploymentAuthorizationError(
            "T3 packet candidate config is malformed")
    if (str(candidate_strategy.get("id") or "") != strategy_id
            or str(candidate_strategy.get("version") or "")
            != strategy_version):
        raise DeploymentAuthorizationError(
            "reviewed candidate config strategy identity does not match runtime")
    try:
        candidate_effective_endpoint = provider.resolve_provider_endpoint(
            candidate_llm.get("provider"), candidate_llm)
        candidate_endpoint = provider.safe_provider_endpoint(
            candidate_effective_endpoint)
        candidate_endpoint_hash = provider.hash_provider_endpoint(
            candidate_effective_endpoint)
        runtime_effective_endpoint = provider.resolve_provider_endpoint(
            cfg["llm"].get("provider"), cfg["llm"])
        runtime_endpoint = provider.safe_provider_endpoint(
            runtime_effective_endpoint)
        runtime_endpoint_hash = provider.hash_provider_endpoint(
            runtime_effective_endpoint)
    except (TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError(
            "LLM endpoint identity is malformed") from exc
    if (candidate_endpoint != runtime_endpoint
            or candidate_endpoint_hash != runtime_endpoint_hash):
        raise DeploymentAuthorizationError(
            "reviewed candidate LLM endpoint differs from live runtime")

    # The packet must bind every artifact identity field to the persisted
    # candidate provenance that generated its forward evidence.  Deriving the
    # config/deployment hashes here prevents a packet author from supplying a
    # self-consistent forged manifest detached from the persisted portfolio.
    try:
        candidate_config_hash = artifact_mod.sha256_json(candidate_config)
        candidate_deployment_hash = state.deployment_config_hash(candidate_config)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError(
            "T3 packet candidate config is malformed") from exc
    variant_definition_hash = artifact_mod.sha256_json(variant_definition)
    provenance_expected = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "strategy_config_version": candidate_provenance.get(
            "strategy_config_version"),
        "validated_config_hash": candidate_config_hash,
        "deployment_config_hash": candidate_deployment_hash,
        "llm_endpoint": candidate_endpoint,
        "llm_endpoint_hash": candidate_endpoint_hash,
        "forward_model_id": candidate_provenance.get("forward_model_id"),
        "forward_model_assumptions_hash": candidate_provenance.get(
            "forward_model_assumptions_hash"),
        "variant_definition_hash": candidate_provenance.get(
            "variant_definition_hash"),
    }
    for name, expected_value in provenance_expected.items():
        if name in {"strategy_id", "strategy_version"}:
            # These two fields are derived from the immutable registry/runtime
            # identity and need not be duplicated in older provenance rows.
            supplied = candidate_provenance.get(name)
            if supplied is not None and supplied != expected_value:
                raise DeploymentAuthorizationError(
                    f"candidate provenance {name} does not match runtime")
            continue
        if not isinstance(expected_value, str) or not expected_value:
            raise DeploymentAuthorizationError(
                f"candidate provenance {name} is missing")
        supplied = candidate_provenance.get(name)
        if supplied is not None and supplied != expected_value:
            raise DeploymentAuthorizationError(
                f"candidate provenance {name} does not match artifact")
    supplied_definition = candidate_provenance.get("variant_definition")
    if (supplied_definition is not None
            and supplied_definition != variant_definition):
        raise DeploymentAuthorizationError(
            "candidate provenance variant definition does not match registry")
    if (provenance_expected["variant_definition_hash"]
            != variant_definition_hash):
        raise DeploymentAuthorizationError(
            "candidate provenance variant definition does not match registry")

    model = require_complete_contract(strategy_id)
    try:
        deployment_hash = state.deployment_config_hash(cfg)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError(
            "live deployment config is malformed") from exc
    if manifest.get("deployment_config_hash") != deployment_hash:
        raise DeploymentAuthorizationError(
            "deployment config hash does not match current live config")
    if candidate_deployment_hash != deployment_hash:
        raise DeploymentAuthorizationError(
            "reviewed candidate config differs from current live config")
    if (manifest.get("validated_config_hash") != candidate_config_hash
            or manifest.get("deployment_config_hash")
            != candidate_deployment_hash
            or manifest.get("strategy_config_version")
            != provenance_expected["strategy_config_version"]
            or manifest.get("forward_model_id")
            != provenance_expected["forward_model_id"]
            or manifest.get("forward_model_assumptions_hash")
            != provenance_expected["forward_model_assumptions_hash"]
            or manifest.get("llm_endpoint")
            != provenance_expected["llm_endpoint"]
            or manifest.get("llm_endpoint_hash")
            != provenance_expected["llm_endpoint_hash"]
            or manifest.get("variant_definition_hash")
            != provenance_expected["variant_definition_hash"]):
        raise DeploymentAuthorizationError(
            "artifact identity is not bound to candidate provenance")
    expected = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "variant_id": variant_id,
        "variant_definition_hash": variant_definition_hash,
        "forward_model_id": model.model_id,
        "forward_model_assumptions_hash": artifact_mod.sha256_json(model.as_dict()),
        "deployment_config_hash": deployment_hash,
        "prompt_hash": prompt_hash,
        "prompt_inputs_hash": prompt_inputs_hash,
        "llm_provider": candidate_llm.get("provider"),
        "llm_model": candidate_llm.get("model"),
        "llm_endpoint": runtime_endpoint,
        "llm_endpoint_hash": runtime_endpoint_hash,
    }
    try:
        artifact_mod.validate_manifest(
            manifest, outer_artifact_hash, config=candidate_config,
            deployment_config_hash=deployment_hash, expected=expected,
            forward_model=model.as_dict())
    except (artifact_mod.ArtifactValidationError, TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError(
            f"reviewed artifact failed runtime verification: {exc}") from exc
    return {
        "packet_id": packet["packet_id"],
        "payload_hash": packet["payload_hash"],
        "artifact_hash": outer_artifact_hash,
        "artifact_manifest": manifest,
        "variant_id": variant_id,
        "variant_definition_hash": manifest["variant_definition_hash"],
        "artifact_strategy_config_version": manifest["strategy_config_version"],
        "deployment_config_hash": deployment_hash,
        "prompt_hash": prompt_hash,
        "prompt_inputs_hash": prompt_inputs_hash,
        "catalog": captured_catalog,
        "system_prompt": prompt,
    }


require_live_artifact = verify_live_artifact
