"""Fail-closed authorization of a reviewed T3 artifact before live startup."""

from __future__ import annotations

import json
import math
import time
import uuid
from copy import deepcopy
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


def _require_text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DeploymentAuthorizationError(f"demo authorization requires {name}")
    return text


def _paper_is_flat(store: FindingsStore, scope_key: str,
                   variant_id: str, cfg: Mapping[str, object]) -> dict:
    """Read local evidence without creating or adopting a paper account."""
    try:
        portfolio = store.paper_portfolio_state(scope_key, variant_id)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError(
            "demo candidate PAPER portfolio cannot be read") from exc
    if not isinstance(portfolio, Mapping):
        raise DeploymentAuthorizationError(
            "demo candidate has no persisted PAPER portfolio")
    if portfolio.get("status") != "PAPER":
        raise DeploymentAuthorizationError(
            "demo candidate PAPER portfolio is not in PAPER state")
    if portfolio.get("revoked_reason") or portfolio.get("revoke_reason"):
        raise DeploymentAuthorizationError(
            "demo candidate PAPER portfolio is revoked")
    if portfolio.get("paper_started_ts") is None:
        raise DeploymentAuthorizationError(
            "demo candidate PAPER portfolio has no paper start")
    for field in ("positions", "active_trades"):
        value = portfolio.get(field)
        if value:
            raise DeploymentAuthorizationError(
                "demo candidate PAPER portfolio is not flat")
    try:
        trades = store.paper_trades_for(scope_key, variant_id)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError(
            "demo candidate PAPER trade ledger cannot be read") from exc
    if not isinstance(trades, list):
        raise DeploymentAuthorizationError(
            "demo candidate PAPER trade ledger is malformed")
    open_trades = [row for row in trades
                   if isinstance(row, Mapping)
                   and str(row.get("status") or "").upper() == "OPEN"]
    if open_trades:
        raise DeploymentAuthorizationError(
            "demo candidate PAPER ledger has OPEN trades")
    try:
        summary = store.paper_summary(scope_key, variant_id)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError(
            "demo candidate PAPER summary cannot be read") from exc
    if not isinstance(summary, Mapping):
        raise DeploymentAuthorizationError(
            "demo candidate PAPER summary is malformed")
    research_cfg = cfg.get("research")
    minimum_closed = (
        int(research_cfg.get("paper_min_closed_trades") or 100)
        if isinstance(research_cfg, Mapping) else 100)
    try:
        closed_trades = int(summary.get("closed_trades") or 0)
        expectancy = summary.get("expectancy_r")
        expectancy_value = float(expectancy)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DeploymentAuthorizationError(
            "demo candidate PAPER success metrics are malformed") from exc
    if (summary.get("status") != "PAPER"
            or summary.get("revoked_reason")
            or summary.get("open_positions")
            or closed_trades < minimum_closed
            or expectancy is None
            or not math.isfinite(expectancy_value)
            or expectancy_value <= 0):
        raise DeploymentAuthorizationError(
            "demo candidate PAPER evidence is stale, insufficient, or not positive")
    return {
        "status": portfolio.get("status"),
        "paper_started_ts": portfolio.get("paper_started_ts"),
        "closed_trades": closed_trades,
        "expectancy_r": expectancy_value,
    }


def verify_demo_artifact(
        cfg: dict, *, variant_id: str | None = None,
        scope_key: str | None = None, packet_ref: str | None = None,
        expected_account_fingerprint: str | None = None,
        catalog: dict | None = None, system_prompt: str | None = None,
        store: FindingsStore | None = None,
        runtime_account_fingerprint: str | None = None) -> dict:
    """Authorize one reviewed variant for an explicitly requested demo run.

    This gate is deliberately independent from :func:`verify_live_artifact`.
    It performs local-only packet, registry, qualification, paper-flatness and
    executable-identity checks.  Exchange/LLM clients must be constructed only
    after it returns successfully.
    """
    if not isinstance(cfg, Mapping) or cfg.get("mode") != "demo":
        raise DeploymentAuthorizationError(
            "demo candidate authorization requires mode=demo")
    variant_id = _require_text(variant_id, "variant_id")
    packet_ref = _require_text(packet_ref, "packet_ref")
    expected_account_fingerprint = _require_text(
        expected_account_fingerprint, "expected_demo_account_fingerprint")
    if runtime_account_fingerprint and (
            runtime_account_fingerprint != expected_account_fingerprint):
        raise DeploymentAuthorizationError(
            "expected demo account fingerprint does not match runtime")
    if not isinstance(cfg.get("strategy"), Mapping) or not isinstance(
            cfg.get("llm"), Mapping):
        raise DeploymentAuthorizationError(
            "demo candidate strategy and LLM config must be mappings")

    captured_catalog = (
        variants.research_selection_catalog() if catalog is None else catalog)
    if not isinstance(captured_catalog, Mapping):
        raise DeploymentAuthorizationError("demo research catalog is malformed")
    prompt = (brain.build_system(cfg, catalog=captured_catalog)
              if system_prompt is None else str(system_prompt))

    research_cfg = cfg.get("research") or {}
    if not isinstance(research_cfg, Mapping):
        raise DeploymentAuthorizationError("demo research config is malformed")
    store_path = resolve_store_path(research_cfg.get("findings_store"))
    if store is None and not Path(store_path).is_file():
        raise DeploymentAuthorizationError(
            f"authoritative findings DB is missing: {store_path}")
    packet_store = store or FindingsStore(store_path)
    try:
        packet = packet_store.resolve_t3_packet(packet_ref)
    except T3PacketResolutionError as exc:
        raise DeploymentAuthorizationError(str(exc)) from exc
    if packet is None:
        raise DeploymentAuthorizationError(
            f"T3 packet {packet_ref!r} is not present in findings DB")
    if packet_ref.startswith("t3-packet:"):
        if str(packet.get("payload_hash") or "") != packet_ref.removeprefix(
                "t3-packet:"):
            raise DeploymentAuthorizationError("T3 packet citation hash mismatch")
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
    if packet.get("variant_id") != variant_id or payload.get("variant_id") != variant_id:
        raise DeploymentAuthorizationError("T3 packet variant does not match requested variant")
    packet_scope = payload.get("scope_key")
    if packet_scope is None:
        # Some packet builders place the scope on source evidence instead.
        source = ((payload.get("forward_analysis") or {}).get("payload")
                  if isinstance(payload.get("forward_analysis"), Mapping)
                  else {})
        source_evidence = source.get("source_evidence") if isinstance(source, Mapping) else {}
        packet_scope = (
            source_evidence.get("scope_key")
            if isinstance(source_evidence, Mapping) else None)
    if packet_scope != scope_key:
        raise DeploymentAuthorizationError("T3 packet scope does not match requested scope")

    try:
        qualification = packet_store.qualification_status(variant_id, scope_key)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError("demo candidate qualification cannot be read") from exc
    if not isinstance(qualification, Mapping) or qualification.get("status") != "QUALIFIED":
        raise DeploymentAuthorizationError("demo candidate is not currently QUALIFIED")
    supplied_qualification = payload.get("qualification")
    if (not isinstance(supplied_qualification, Mapping)
            or supplied_qualification.get("event_id")
            != qualification.get("event_id")):
        raise DeploymentAuthorizationError(
            "T3 packet qualification is missing or stale")
    paper = _paper_is_flat(packet_store, scope_key, variant_id, cfg)

    variant_row = packet_store.variant(variant_id)
    if not isinstance(variant_row, Mapping):
        raise DeploymentAuthorizationError(
            f"artifact variant {variant_id!r} is not registered")
    variant_definition = _variant_definition_from_row(variant_row)
    variant_definition_hash = artifact_mod.sha256_json(variant_definition)
    if "mode" in (variant_definition.get("overrides") or {}):
        raise DeploymentAuthorizationError(
            "demo candidate variants cannot override runtime mode")
    strategy_id, strategy_version = strategy.identity(cfg)
    if (variant_definition["strategy_id"] != strategy_id
            or variant_definition["base_version"] != strategy_version):
        raise DeploymentAuthorizationError(
            "demo candidate variant strategy identity does not match runtime")
    try:
        candidate_variant = variants.from_record(dict(variant_row))
        try:
            candidate_cfg = variants.apply(
                candidate_variant, dict(cfg), allow_shadow_strategy=True)
        except ValueError:
            # Direct callers and focused checks often provide only a findings
            # store path under ``research``.  That bookkeeping block is not
            # executable experiment identity; validate the candidate against
            # the ordinary config and restore the caller's research settings.
            research_block = cfg.get("research")
            if not (isinstance(research_block, Mapping)
                    and set(research_block) <= {"findings_store"}):
                raise
            stripped = deepcopy(dict(cfg))
            stripped.pop("research", None)
            candidate_cfg = variants.apply(
                candidate_variant, stripped, allow_shadow_strategy=True)
            candidate_cfg["research"] = deepcopy(dict(research_block))
    except (TypeError, ValueError, KeyError) as exc:
        raise DeploymentAuthorizationError(
            "demo candidate variant cannot be applied to runtime config") from exc
    if candidate_cfg.get("mode") != "demo":
        raise DeploymentAuthorizationError(
            "demo candidate runtime config must retain mode=demo")
    manifest = payload.get("artifact_manifest")
    outer_artifact_hash = payload.get("artifact_hash")
    if not isinstance(manifest, Mapping) or not isinstance(outer_artifact_hash, str):
        raise DeploymentAuthorizationError("T3 packet artifact is incomplete")
    if manifest.get("variant_definition") != variant_definition or manifest.get(
            "variant_definition_hash") != variant_definition_hash:
        raise DeploymentAuthorizationError("demo artifact variant identity drifted")

    forward = payload.get("forward_analysis")
    analysis_payload = forward.get("payload") if isinstance(forward, Mapping) else None
    source_evidence = (
        analysis_payload.get("source_evidence")
        if isinstance(analysis_payload, Mapping) else None)
    eligibility = (
        source_evidence.get("eligibility")
        if isinstance(source_evidence, Mapping) else None)
    provenances = (
        eligibility.get("setting_provenances")
        if isinstance(eligibility, Mapping) else None)
    provenance = provenances.get(variant_id) if isinstance(provenances, Mapping) else None
    if not isinstance(provenance, Mapping):
        raise DeploymentAuthorizationError("T3 packet candidate provenance is missing")
    candidate_config = provenance.get("experiment_config")
    candidate_material = state.experiment_fingerprint_material(candidate_cfg)
    if candidate_config != candidate_material:
        raise DeploymentAuthorizationError("reviewed candidate config differs from demo runtime")
    # The packet stores the secret-free executable fingerprint material (the
    # same representation produced by ``experiment_fingerprint_material``),
    # while ``candidate_cfg`` is the validated runtime config used by clients.
    # Artifact manifests are hashed against the former, as are existing live
    # packets, so both identities must be checked explicitly.
    candidate_config_hash = artifact_mod.sha256_json(candidate_config)
    strategy_config_version = state.experiment_fingerprint(candidate_cfg)
    if provenance.get("strategy_config_version") != strategy_config_version:
        raise DeploymentAuthorizationError("candidate strategy config hash does not match runtime")
    current_code_version = state.code_fingerprint()
    if provenance.get("code_version") != current_code_version:
        raise DeploymentAuthorizationError("candidate source hash does not match runtime")
    model = require_complete_contract(strategy_id)
    model_hash = artifact_mod.sha256_json(model.as_dict())
    if (provenance.get("forward_model_id") != model.model_id
            or provenance.get("forward_model_assumptions_hash") != model_hash):
        raise DeploymentAuthorizationError("candidate model hash does not match runtime")
    candidate_prompt = brain.build_system(candidate_config, catalog=captured_catalog)
    prompt_hash = artifact_mod.sha256_text(candidate_prompt)
    prompt_inputs_hash = _prompt_inputs_hash(candidate_cfg, captured_catalog)
    if system_prompt is not None and str(system_prompt) != candidate_prompt:
        raise DeploymentAuthorizationError("demo prompt snapshot drifted")
    try:
        candidate_endpoint_effective = provider.resolve_provider_endpoint(
            candidate_cfg["llm"].get("provider"), candidate_cfg["llm"])
        candidate_endpoint = provider.safe_provider_endpoint(candidate_endpoint_effective)
        candidate_endpoint_hash = provider.hash_provider_endpoint(candidate_endpoint_effective)
        deployment_hash = state.deployment_config_hash(candidate_config)
    except (TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError(
            "demo candidate executable identity is malformed") from exc
    expected = {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "variant_id": variant_id,
        "variant_definition_hash": variant_definition_hash,
        "forward_model_id": model.model_id,
        "forward_model_assumptions_hash": model_hash,
        "deployment_config_hash": deployment_hash,
        "prompt_hash": prompt_hash,
        "prompt_inputs_hash": prompt_inputs_hash,
        "llm_provider": candidate_cfg["llm"].get("provider"),
        "llm_model": candidate_cfg["llm"].get("model"),
        "llm_endpoint": candidate_endpoint,
        "llm_endpoint_hash": candidate_endpoint_hash,
    }
    if manifest.get("validated_config_hash") != candidate_config_hash or manifest.get(
            "strategy_config_version") != strategy_config_version:
        raise DeploymentAuthorizationError("demo artifact config hash does not match runtime")
    try:
        artifact_mod.validate_manifest(
            manifest, outer_artifact_hash, config=candidate_config,
            deployment_config_hash=deployment_hash, expected=expected,
            forward_model=model.as_dict())
    except (artifact_mod.ArtifactValidationError, TypeError, ValueError) as exc:
        raise DeploymentAuthorizationError(
            f"reviewed demo artifact failed runtime verification: {exc}") from exc
    return {
        "packet_id": packet["packet_id"],
        "packet_hash": packet["payload_hash"],
        "payload_hash": packet["payload_hash"],
        "artifact_hash": outer_artifact_hash,
        "artifact_manifest": dict(manifest),
        "variant_id": variant_id,
        "scope_key": scope_key,
        "variant_definition_hash": variant_definition_hash,
        "artifact_strategy_config_version": strategy_config_version,
        "deployment_config_hash": deployment_hash,
        "candidate_config_hash": candidate_config_hash,
        "prompt_hash": prompt_hash,
        "prompt_inputs_hash": prompt_inputs_hash,
        "runtime_source_hash": manifest.get("runtime_source_hash"),
        "model_id": model.model_id,
        "model_assumptions_hash": model_hash,
        "reviewed_by": packet.get("reviewed_by"),
        "registry_change_ref": packet.get("registry_change_ref"),
        "expected_account_fingerprint": expected_account_fingerprint,
        "paper": paper,
        "catalog": captured_catalog,
        "system_prompt": candidate_prompt,
        "runtime_config": candidate_cfg,
    }


def preflight_demo_account(
        exchange, *, expected_account_fingerprint: str,
        runtime_account_fingerprint: str | None = None,
        runtime_state_snapshot: Mapping[str, object] | None = None) -> dict:
    """Run read-only account and flatness checks after local authorization."""
    expected = _require_text(expected_account_fingerprint,
                             "expected_demo_account_fingerprint")
    if not bool(getattr(exchange, "demo", False)):
        raise DeploymentAuthorizationError("candidate demo startup requires a demo exchange")
    if runtime_account_fingerprint and runtime_account_fingerprint != expected:
        raise DeploymentAuthorizationError(
            "expected demo account fingerprint does not match runtime")
    account = exchange.verify_trade_permission()
    if not isinstance(account, Mapping):
        raise DeploymentAuthorizationError("demo account safety response is malformed")
    for key in ("account_fingerprint", "fingerprint"):
        observed = account.get(key)
        if observed and str(observed) != expected:
            raise DeploymentAuthorizationError("demo account fingerprint mismatch")
    positions = exchange.positions()
    if positions:
        raise DeploymentAuthorizationError("demo account is not flat: open positions exist")
    orders = []
    client = getattr(exchange, "x", None)
    fetch = getattr(client, "fetch_open_orders", None)
    if not callable(fetch):
        raise DeploymentAuthorizationError(
            "demo account open-order API is unavailable")
    try:
        regular = fetch()
    except Exception as exc:  # noqa: BLE001
        raise DeploymentAuthorizationError(
            "demo account regular open-order state is ambiguous") from exc
    if not isinstance(regular, list):
        raise DeploymentAuthorizationError(
            "demo account regular open-order response is malformed")
    orders.extend(regular)
    # OKX/CCXT exposes conditional, OCO, trigger, and related algorithmic
    # orders through this same method with an ordType query.  Require every
    # supported query to answer with a concrete list before declaring flatness.
    from .exchange import ALL_ALGO_TYPES
    for order_type in ALL_ALGO_TYPES:
        try:
            rows = fetch(None, None, None, {"ordType": order_type})
        except Exception as exc:  # noqa: BLE001
            raise DeploymentAuthorizationError(
                f"demo account {order_type} open-order state is ambiguous") from exc
        if not isinstance(rows, list):
            raise DeploymentAuthorizationError(
                f"demo account {order_type} open-order response is malformed")
        orders.extend(rows)
    if orders:
        raise DeploymentAuthorizationError("demo account has open orders or algos")
    snapshot = runtime_state_snapshot or {}
    if any(snapshot.get(name) for name in ("active_trades", "opened_at", "protection")):
        raise DeploymentAuthorizationError("demo runtime state is not flat")
    return {
        "account": dict(account),
        "account_fingerprint": expected,
        "positions": [],
        "open_orders": [],
        "checked_ts": time.time(),
    }


def record_demo_authorization_receipt(
        authorization: Mapping[str, object], *,
        account_fingerprint: str, preflight: Mapping[str, object]) -> dict:
    """Append one attributable receipt to the mode-scoped runtime journal."""
    receipt = {
        "receipt_id": uuid.uuid4().hex,
        "kind": "demo_candidate_authorization",
        "recorded_ts": time.time(),
        "runtime_mode": "demo",
        "variant_id": authorization.get("variant_id"),
        "scope_key": authorization.get("scope_key"),
        "packet_id": authorization.get("packet_id"),
        "packet_hash": authorization.get("packet_hash"),
        "artifact_hash": authorization.get("artifact_hash"),
        "variant_definition_hash": authorization.get("variant_definition_hash"),
        "candidate_config_hash": authorization.get("candidate_config_hash"),
        "strategy_config_version": authorization.get("artifact_strategy_config_version"),
        "model_id": authorization.get("model_id"),
        "model_assumptions_hash": authorization.get("model_assumptions_hash"),
        "prompt_hash": authorization.get("prompt_hash"),
        "prompt_inputs_hash": authorization.get("prompt_inputs_hash"),
        "runtime_source_hash": authorization.get("runtime_source_hash"),
        "deployment_config_hash": authorization.get("deployment_config_hash"),
        "account_fingerprint": account_fingerprint,
        "reviewed_by": authorization.get("reviewed_by"),
        "registry_change_ref": authorization.get("registry_change_ref"),
        "preflight": dict(preflight),
    }
    # ``state.log_event`` intentionally accepts only event identity overrides;
    # the rest of the attribution lives in the process journal context.
    context = {
        "runtime_mode": "demo",
        "account_fingerprint": account_fingerprint,
        "variant_id": authorization.get("variant_id"),
        "packet_id": authorization.get("packet_id"),
        "packet_hash": authorization.get("packet_hash"),
        "artifact_hash": authorization.get("artifact_hash"),
        "artifact_variant_id": authorization.get("variant_id"),
        "artifact_variant_definition_hash": authorization.get(
            "variant_definition_hash"),
        "artifact_strategy_config_version": authorization.get(
            "artifact_strategy_config_version"),
        "deployment_config_hash": authorization.get(
            "deployment_config_hash"),
    }
    state.set_journal_context(
        **{key: value for key, value in context.items() if value})
    state.log_event(
        "demo_candidate_authorization",
        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        variant_id=str(authorization.get("variant_id") or ""))
    return receipt


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
