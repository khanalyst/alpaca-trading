"""Deterministic payload and rendering helpers for research proof artifacts.

This module contains the pure, deterministic proof boundary.  It consumes a
ledger-like object but does not depend on a particular ledger implementation.
No files, clocks, or network calls are used here.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import html
import json
import math
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import validate_rule_spec


PROOF_SCHEMA = "research-proof.v1"


_DIAGNOSTIC_HISTORICAL_BACKFILL = "diagnostic_historical_backfill"
_DIAGNOSTIC_BAR_FALLBACK = "diagnostic_bar_fallback"
_DIAGNOSTIC_EVIDENCE_MODES = frozenset({
    _DIAGNOSTIC_HISTORICAL_BACKFILL, _DIAGNOSTIC_BAR_FALLBACK,
})


def _diagnostic_evidence_mode(value: Any) -> str | None:
    """Return the first diagnostic evidence label in persisted proof input."""
    if isinstance(value, Mapping):
        mode = str(value.get("evidence_mode") or "").strip().lower()
        if mode in _DIAGNOSTIC_EVIDENCE_MODES:
            return mode
        diagnostic_reason = str(
            value.get("diagnostic_reason") or "").strip().lower()
        if diagnostic_reason and (
                value.get("diagnostic_only") is True or
                value.get("authorizing") is False):
            return diagnostic_reason
        reasons = value.get("reasons")
        if isinstance(reasons, Mapping):
            for candidate in _DIAGNOSTIC_EVIDENCE_MODES:
                count = reasons.get(candidate)
                if (isinstance(count, (int, float)) and
                        not isinstance(count, bool) and count > 0):
                    return candidate
        for item in value.values():
            found = _diagnostic_evidence_mode(item)
            if found is not None:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _diagnostic_evidence_mode(item)
            if found is not None:
                return found
    return None


def _contains_diagnostic_backfill(value: Any) -> bool:
    """Compatibility predicate for callers that only need a boolean."""
    return _diagnostic_evidence_mode(value) is not None
SESSION_ZONE = "America/New_York"
_HASH_FIELDS = ("dataset_hash", "config_hash", "code_hash", "provenance_hash")
_FORBIDDEN = {"source", "code", "raw_response", "response", "html", "api_key",
              "secret", "token", "password", "credential"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _finite(value: Any, *, path: str = "value") -> Any:
    """Make a safe JSON value while rejecting non-finite numbers and secrets."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.lower() in _FORBIDDEN:
                continue
            result[key] = _finite(child, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_finite(item, path=f"{path}[{i}]") for i, item in enumerate(value)]
    # Datetimes and enums are represented as stable text, never object reprs.
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _call(ledger: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    method = getattr(ledger, name, None)
    if not callable(method):
        return None
    try:
        return method(*args, **kwargs)
    except (KeyError, TypeError):
        # Fake ledgers often expose a narrower signature; a missing optional
        # collection should not make proof creation dependent on one class.
        try:
            return method(*args)
        except Exception:
            return None


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _json_field(value: Any, default: Any = None) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
    return value if value is not None else default


def _candidate_record(ledger: Any, candidate_id: str, context: Mapping[str, Any]) -> dict[str, Any]:
    direct = context.get("candidate")
    if direct is None and isinstance(ledger, Mapping):
        direct = ledger.get(candidate_id) or ledger.get("candidate")
        if direct is None and isinstance(ledger.get("candidates"), Mapping):
            direct = ledger["candidates"].get(candidate_id)
        if direct is None and ledger.get("candidate_id"):
            direct = ledger
    if direct is None and context.get("candidate_id"):
        # A context mapping can itself be the candidate record in tiny fake
        # ledgers used by offline callers.
        direct = context
    if direct is None:
        direct = _call(ledger, "candidate", candidate_id)
    return _as_mapping(direct)


def _records(ledger: Any, candidate_id: str, context: Mapping[str, Any],
             name: str, singular: str) -> list[dict[str, Any]]:
    direct = None
    # A real ledger method outranks caller-supplied collections; the latter
    # are only a compatibility seam for tiny mapping/fake ledgers.
    if not isinstance(ledger, Mapping) and callable(getattr(ledger, name, None)):
        direct = _call(ledger, name, candidate_id)
    if direct is None:
        direct = context.get(name)
    if direct is None and isinstance(ledger, Mapping):
        direct = ledger.get(name)
    if direct is None and not (not isinstance(ledger, Mapping) and callable(getattr(ledger, name, None))):
        direct = _call(ledger, name, candidate_id)
    if direct is None:
        one = context.get(singular)
        direct = [one] if one else []
    if isinstance(direct, Mapping):
        direct = direct.get(name, [direct])
    if not isinstance(direct, Sequence) or isinstance(direct, (str, bytes)):
        direct = [direct] if direct else []
    return [_as_mapping(item) for item in direct if item is not None]


def _latest(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records:
        return {}
    # Ledger APIs already order by immutable creation keys.  Sorting by stable
    # identifiers makes fake ledgers deterministic as well.
    return dict(sorted(records, key=lambda item: (
        str(item.get("created_at") or item.get("timestamp") or ""),
        str(item.get("run_id") or item.get("evidence_id") or item.get("event_id") or ""),
    ))[-1])


def _run_bound_records(records: Sequence[Mapping[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """Keep only records explicitly bound to one authorized run."""
    output = []
    for item in records:
        payload = _json_field(item.get("payload") or item.get("payload_json"), {})
        bound = item.get("run_id") or (payload.get("run_id") if isinstance(payload, Mapping) else None)
        if str(bound or "") == str(run_id):
            output.append(dict(item))
    return output


def _select_authorized_shadow_run(runs: Sequence[Mapping[str, Any]],
                                  evidence: Sequence[Mapping[str, Any]],
                                  context: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    requested = context.get("authorized_run_id", context.get("run_id"))
    verified_ids = set()
    for item in evidence:
        if str(item.get("kind") or "") != "verified_gate":
            continue
        payload = _json_field(item.get("payload") or item.get("payload_json"), {})
        bound = item.get("run_id") or (payload.get("run_id") if isinstance(payload, Mapping) else None)
        if bound:
            verified_ids.add(str(bound))
    strict = bool(requested or verified_ids)
    if requested:
        matches = [item for item in runs if str(item.get("run_id")) == str(requested)]
        if len(matches) != 1:
            raise ValueError("authorized proof run is missing or ambiguous")
        run = dict(matches[0])
        if run.get("lane") != "shadow":
            raise ValueError("proof authorization requires a shadow run")
        if verified_ids and str(requested) not in verified_ids:
            raise ValueError("authorized proof run lacks verified gate evidence")
        return run, True
    if verified_ids:
        matches = [item for item in runs if str(item.get("run_id")) in verified_ids
                   and item.get("lane") == "shadow"]
        if not matches:
            raise ValueError("no verified shadow run is available for proof")
        return _latest(matches), True
    # Legacy/fake ledgers may not expose evidence run bindings.  Keep their
    # deterministic behavior while requiring explicit authorization whenever
    # a real ledger supplies run-scoped verified evidence.
    return _latest(runs), strict


def _extract_spec(candidate: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any] | None:
    value = context.get("normalized_rule_spec", context.get("rule_spec"))
    if value is None:
        value = candidate.get("normalized_rule_spec", candidate.get("rule_spec"))
    if value is None:
        config = _json_field(candidate.get("config_json"), {})
        if isinstance(config, Mapping):
            strategy = config.get("strategy")
            if isinstance(strategy, Mapping):
                value = strategy.get("rule_spec")
    if not isinstance(value, Mapping):
        return None
    try:
        return validate_rule_spec(value)
    except Exception as exc:
        raise ValueError(f"normalized rule_spec is invalid: {exc}") from exc


def _session_info(context: Mapping[str, Any], candidate: Mapping[str, Any],
                  run: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    raw = (context.get("session_timestamp") or context.get("session_date") or
           context.get("as_of") or run.get("heldout_end") or run.get("fit_end"))
    if raw is None:
        for item in evidence:
            payload = _json_field(item.get("payload") or item.get("payload_json"), {})
            if isinstance(payload, Mapping) and (payload.get("session_date") or payload.get("as_of")):
                raw = payload.get("session_date") or payload.get("as_of")
                break
    local = None
    if isinstance(raw, datetime):
        local = raw if raw.tzinfo else raw.replace(tzinfo=ZoneInfo("UTC"))
        local = local.astimezone(ZoneInfo(SESSION_ZONE))
    elif raw:
        text = str(raw)
        try:
            if len(text) == 10:
                # A bare session date has no UTC offset; interpret it in the
                # exchange zone so DST labels do not shift to the prior day.
                local = datetime.fromisoformat(text).replace(
                    hour=12, tzinfo=ZoneInfo(SESSION_ZONE))
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                local = (parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("UTC"))).astimezone(
                    ZoneInfo(SESSION_ZONE))
        except ValueError:
            pass
    explicit_date = context.get("session_date")
    if explicit_date is not None:
        try:
            date_value = (explicit_date.isoformat()
                          if isinstance(explicit_date, date) and not isinstance(explicit_date, datetime)
                          else date.fromisoformat(str(explicit_date)).isoformat())
        except ValueError as exc:
            raise ValueError(f"invalid proof session date: {explicit_date!r}") from exc
    else:
        date_value = str(local.date().isoformat() if local else raw or "")
    if not date_value:
        raise ValueError("proof requires a New York session date")
    try:
        datetime.fromisoformat(date_value)
    except ValueError as exc:
        raise ValueError(f"invalid proof session date: {date_value!r}") from exc
    dst = (local.tzname() if local else str(context.get("dst_label") or ""))
    if dst not in {"EST", "EDT"} and date_value:
        # Session dates without time still have a deterministic DST label.
        try:
            noon = datetime.fromisoformat(date_value).replace(hour=12, tzinfo=ZoneInfo(SESSION_ZONE))
            dst = noon.tzname() or ""
        except ValueError:
            dst = ""
    return {"timezone": SESSION_ZONE, "date": date_value,
            "dst_label": dst, "label": f"{date_value} ({dst})".strip()}


def _hashes(candidate: Mapping[str, Any], run: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    explicit = context.get("hashes") if isinstance(context.get("hashes"), Mapping) else {}
    for key in _HASH_FIELDS:
        aliases = (key, key.replace("_hash", "_sha256"))
        value = next((source.get(alias) for source in (explicit, context, run, candidate)
                      for alias in aliases if source.get(alias) is not None), None)
        result[key] = str(value) if value is not None else None
    return result


def _provider_info(candidate: Mapping[str, Any], run: Mapping[str, Any],
                   evidence: Sequence[Mapping[str, Any]], context: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("provider", "feed", "schema", "as_of"):
        aliases = {"provider": ("provider", "provider_id"),
                   "feed": ("feed", "feed_id"),
                   "schema": ("schema", "schema_id", "schema_version"),
                   "as_of": ("as_of", "as_of_timestamp")}[key]
        value = next((source.get(alias) for source in (context, run, candidate)
                      for alias in aliases if source.get(alias) is not None), None)
        if value is not None:
            result[key] = str(value)
    for item in evidence:
        payload = _json_field(item.get("payload") or item.get("payload_json"), {})
        if not isinstance(payload, Mapping):
            continue
        for key in ("provider", "feed", "schema", "as_of"):
            aliases = {"provider": ("provider", "provider_id"),
                       "feed": ("feed", "feed_id"),
                       "schema": ("schema", "schema_id", "schema_version"),
                       "as_of": ("as_of", "as_of_timestamp")}[key]
            if key not in result:
                value = next((payload.get(alias) for alias in aliases
                              if payload.get(alias) is not None), None)
                if value is not None:
                    result[key] = str(value)
    return {key: result.get(key) for key in ("provider", "feed", "schema", "as_of")}


def _metrics(run: Mapping[str, Any], context: Mapping[str, Any],
             evidence: Sequence[Mapping[str, Any]] = (),
             trades: Sequence[Mapping[str, Any]] = ()) -> tuple[dict[str, Any], dict[str, int]]:
    raw = _json_field(run.get("metrics"), {})
    metrics = dict(raw) if isinstance(raw, Mapping) else {}
    supplied = context.get("metrics")
    if isinstance(supplied, Mapping):
        metrics.update(supplied)
    gate = metrics.get("gate") if isinstance(metrics.get("gate"), Mapping) else {}
    if isinstance(gate.get("verified_gate"), Mapping):
        gate = dict(gate["verified_gate"])
    verified = None
    for item in evidence:
        if str(item.get("kind") or "") != "verified_gate":
            continue
        payload = _json_field(item.get("payload") or item.get("payload_json"), {})
        if isinstance(payload, Mapping) and isinstance(payload.get("gate"), Mapping):
            verified = dict(payload["gate"])
    if verified is not None:
        gate = verified
    samples = context.get("samples") if isinstance(context.get("samples"), Mapping) else context.get("sample_counts", {})
    if not isinstance(samples, Mapping):
        samples = {}
    counts = gate.get("counts") if isinstance(gate.get("counts"), Mapping) else {}
    fit_counts = counts.get("fit") if isinstance(counts.get("fit"), Mapping) else {}
    held_counts = counts.get("heldout") if isinstance(counts.get("heldout"), Mapping) else {}
    context_fit = (len(context.get("fit", ()))
                   if isinstance(context.get("fit"), Sequence) else 0)
    context_held = (len(context.get("heldout", ()))
                    if isinstance(context.get("heldout"), Sequence) else 0)
    fit_default = metrics.get(
        "fit_samples", metrics.get(
            "fit_trades", fit_counts.get("trades", context_fit)))
    held_default = metrics.get(
        "heldout_samples", metrics.get(
            "heldout_trades", held_counts.get("trades", context_held)))
    fit = samples.get("fit", fit_default)
    held = samples.get("heldout", held_default)
    shadow_default = held if str(run.get("lane") or "") == "shadow" else 0
    shadow = samples.get("shadow", metrics.get("shadow_samples", metrics.get("shadow_trades",
                                     len(context.get("shadow", ())) if isinstance(context.get("shadow"), Sequence)
                                     else shadow_default)))
    result_samples = {}
    for key, value in (("fit", fit), ("heldout", held), ("shadow", shadow)):
        try:
            result_samples[key] = max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            result_samples[key] = 0
    if not gate:
        gate = {"passes": metrics.get("passes")}
    statistics = gate.get("statistics") if isinstance(gate.get("statistics"), Mapping) else {}
    performance = gate.get("performance") if isinstance(gate.get("performance"), Mapping) else {}
    q_value = statistics.get("q_value")
    confidence = metrics.get("confidence", metrics.get("ci_low"))
    if confidence is None and q_value is not None:
        confidence = 1.0 - float(q_value)
    drawdown = metrics.get("max_drawdown", metrics.get("drawdown",
                              performance.get("max_drawdown")))
    cost = metrics.get("cost", metrics.get("total_cost", metrics.get("costs")))
    if cost is None:
        finite_costs = []
        for trade in trades:
            try:
                value = float(trade.get("costs"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                finite_costs.append(value)
        if finite_costs:
            cost = {"total": sum(finite_costs), "trades_with_costs": len(finite_costs)}
    selected = {"gate": _finite(gate), "confidence": confidence,
                "drawdown": drawdown, "cost": cost}
    for key in ("gate", "confidence", "drawdown", "cost"):
        if selected[key] is None:
            selected[key] = None
    return selected, result_samples


def build_proof_payload(ledger: Any, candidate_id: str,
                        context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a deterministic proof payload without writing an artifact."""

    context = dict(context or {})
    candidate_id = str(candidate_id)
    candidate = _candidate_record(
        ledger, candidate_id,
        {} if (context.get("authorized_run_id") or context.get("run_id")) else context)
    if not candidate:
        raise KeyError(f"unknown candidate {candidate_id!r}")
    # Once a run is explicitly authorized, context collections are hints only;
    # source-bound records must come from the ledger API and cannot be swapped
    # in from another lane.
    record_context = dict(context)
    if context.get("authorized_run_id") or context.get("run_id"):
        for key in ("runs", "run", "evidence", "evidence_item", "events", "event",
                    "history", "trades", "trade"):
            record_context.pop(key, None)
    runs = _records(ledger, candidate_id, record_context, "runs", "run")
    evidence = _records(ledger, candidate_id, record_context, "evidence", "evidence_item")
    events = _records(ledger, candidate_id, record_context, "events", "event")
    if not events and "events" not in context:
        events = _records(ledger, candidate_id, context, "history", "event")
    # Reporting events are intentionally excluded from their own payload so
    # an idempotent rerun does not create a new report merely because the last
    # report was announced.
    events = [item for item in events
              if not str(item.get("event_type") or "").startswith("proof_")]
    trades = _records(ledger, candidate_id, record_context, "trades", "trade")
    run, strict_authorization = _select_authorized_shadow_run(runs, evidence, context)
    run_id = str(run.get("run_id") or "")
    if strict_authorization:
        persisted_candidate = _candidate_record(ledger, candidate_id, {})
        if persisted_candidate:
            supplied_candidate = context.get("candidate")
            if isinstance(supplied_candidate, Mapping):
                for key in ("vehicle", "variant_id", "strategy_id"):
                    if supplied_candidate.get(key) is not None and str(
                            supplied_candidate.get(key)) != str(persisted_candidate.get(key) or ""):
                        raise ValueError(f"proof context candidate {key} does not match persisted record")
            candidate = persisted_candidate
        evidence = _run_bound_records(evidence, run_id)
        events = _run_bound_records(events, run_id) if events else []
        trades = _run_bound_records(trades, run_id)
        if not evidence or not any(str(item.get("kind") or "") == "verified_gate"
                                   for item in evidence):
            raise ValueError("proof requires evidence from the authorized verified shadow run")
        diagnostic_mode = (
            _diagnostic_evidence_mode(run.get("metrics")) or
            _diagnostic_evidence_mode(evidence) or
            _diagnostic_evidence_mode(trades))
        if diagnostic_mode is not None:
            label = ("diagnostic historical backfill"
                     if diagnostic_mode == _DIAGNOSTIC_HISTORICAL_BACKFILL
                     else "diagnostic bar fallback"
                     if diagnostic_mode == _DIAGNOSTIC_BAR_FALLBACK
                     else diagnostic_mode.replace("_", " "))
            raise ValueError(
                f"{label} cannot emit an authorized proof")
    evidence_latest = _latest(evidence)
    vehicle = str(context.get("vehicle") or candidate.get("vehicle") or run.get("vehicle") or "")
    if vehicle not in {"equity", "option"}:
        raise ValueError("vehicle must be equity or option")
    if strict_authorization:
        for key, expected in (("vehicle", run.get("vehicle")),
                              ("lane", run.get("lane"))):
            supplied = context.get(key)
            if supplied is not None and str(supplied) != str(expected):
                raise ValueError(f"proof context {key} does not match authorized run")
        vehicle = str(run.get("vehicle"))
    status = str(context.get("status") or candidate.get("status") or "unknown")
    if strict_authorization and context.get("status") is not None:
        persisted_status = str(candidate.get("status") or "unknown")
        if str(context.get("status")) != persisted_status:
            raise ValueError("proof context status does not match persisted candidate")
    trusted_context = {} if strict_authorization else context
    spec = _extract_spec(candidate, trusted_context)
    if strict_authorization:
        for key in ("variant_id", "strategy_id"):
            if context.get(key) is not None and str(context[key]) != str(candidate.get(key) or ""):
                raise ValueError(f"proof context {key} does not match persisted candidate")
    metric_values, samples = _metrics(run, trusted_context, evidence, trades)
    hashes = _hashes(candidate, run, trusted_context)
    if strict_authorization and isinstance(metric_values.get("gate"), Mapping) and \
            metric_values["gate"].get("passes") is True:
        missing = [key for key in _HASH_FIELDS if not hashes.get(key)]
        if missing:
            raise ValueError("qualifying proof is missing provenance hashes: " +
                             ", ".join(missing))
    provider = _provider_info(candidate, run, evidence, trusted_context)
    session = _session_info(trusted_context, candidate, run, evidence)

    run_ids = sorted({str(item.get("run_id")) for item in runs if item.get("run_id") is not None})
    evidence_ids = sorted({str(item.get("evidence_id")) for item in evidence if item.get("evidence_id") is not None})
    event_ids = sorted({str(item.get("event_id")) for item in events if item.get("event_id") is not None})
    statuses = {"candidate": status,
                "runs": sorted(str(item.get("lane") or item.get("status") or "")
                                for item in runs)}
    if not strict_authorization and isinstance(context.get("statuses"), Mapping):
        statuses.update(_finite(context["statuses"]))

    llm_hashes = trusted_context.get("llm_evidence_hashes")
    if llm_hashes is None:
        llm_context = trusted_context.get("llm_evidence")
        if isinstance(llm_context, Mapping):
            llm_hashes = [str(value) for key, value in llm_context.items()
                          if "hash" in str(key).lower() and value is not None]
    if llm_hashes is None:
        llm_hashes = []
        for item in evidence:
            kind = str(item.get("kind") or "").lower()
            if "llm" not in kind:
                continue
            value = item.get("evidence_hash") or item.get("hash")
            if value:
                llm_hashes.append(str(value))
    if isinstance(llm_hashes, Mapping):
        llm_hashes = sorted(str(value) for value in llm_hashes.values())
    elif isinstance(llm_hashes, (str, bytes)):
        llm_hashes = [str(llm_hashes)]
    else:
        llm_hashes = sorted(str(value) for value in (llm_hashes or ()))
    evidence_hashes = sorted(str(item.get("evidence_hash")) for item in evidence
                             if item.get("evidence_hash") is not None)

    payload: dict[str, Any] = {
        "schema": PROOF_SCHEMA,
        "candidate_id": candidate_id,
        "run_id": run.get("run_id"),
        "authorized_run_id": run.get("run_id") if strict_authorization else None,
        "run_ids": run_ids,
        "evidence_id": evidence_latest.get("evidence_id"),
        "evidence_ids": evidence_ids,
        "event_ids": event_ids,
        "status": status,
        "statuses": statuses,
        "variant_id": str(trusted_context.get("variant_id") or candidate.get("variant_id") or ""),
        "strategy_id": str(trusted_context.get("strategy_id") or candidate.get("strategy_id") or ""),
        "strategy": str(trusted_context.get("strategy_id") or candidate.get("strategy_id") or ""),
        "vehicle": vehicle,
        "hashes": hashes,
        "dataset_hash": hashes["dataset_hash"],
        "config_hash": hashes["config_hash"],
        "code_hash": hashes["code_hash"],
        "provenance_hash": hashes["provenance_hash"],
        "provider": provider["provider"], "feed": provider["feed"],
        "schema_identity": provider["schema"], "provider_schema": provider["schema"],
        "data_schema": provider["schema"], "source_schema": provider["schema"],
        "schema_version": provider["schema"],
        "as_of": provider["as_of"],
        "session": session, "session_date": session["date"],
        "timezone": SESSION_ZONE, "dst_label": session["dst_label"],
        "samples": samples, "fit_samples": samples["fit"],
        "heldout_samples": samples["heldout"], "shadow_samples": samples["shadow"],
        "gate": metric_values["gate"], "confidence": metric_values["confidence"],
        "drawdown": metric_values["drawdown"], "cost": metric_values["cost"],
        "metrics": metric_values,
        "normalized_rule_spec": spec,
        "evidence_hashes": evidence_hashes,
        "llm_evidence_hashes": llm_hashes,
    }
    # The final finite pass removes object-specific values and rejects NaN;
    # no generation timestamp is ever introduced here.
    return _finite(payload)


def _md_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return html.escape(text.replace("`", "'"), quote=True).replace("\n", " ")


def render_markdown(payload: Mapping[str, Any], digest: str | None = None) -> str:
    """Render a stable, escaped Markdown proof artifact."""

    digest = digest or payload_hash(payload)
    fields = (
        ("Candidate", payload.get("candidate_id")), ("Status", payload.get("status")),
        ("Variant", payload.get("variant_id")), ("Strategy", payload.get("strategy_id")),
        ("Vehicle", payload.get("vehicle")), ("Provider", payload.get("provider")),
        ("Feed", payload.get("feed")), ("Schema", payload.get("schema_identity")),
        ("As of", payload.get("as_of")), ("Session", payload.get("session", {}).get("label")),
    )
    lines = ["# Research proof", "", f"Payload hash: `{_md_escape(digest)}`", "",
             "| Field | Value |", "|---|---|"]
    lines.extend(f"| {_md_escape(label)} | {_md_escape(value)} |" for label, value in fields)
    lines.extend(["", "## Evidence", "", f"- Run IDs: {_md_escape(', '.join(payload.get('run_ids', [])))}",
                  f"- Evidence IDs: {_md_escape(', '.join(payload.get('evidence_ids', [])))}",
                  f"- Event IDs: {_md_escape(', '.join(payload.get('event_ids', [])))}",
                  f"- Fit / held-out / shadow samples: {_md_escape(payload.get('fit_samples'))} / "
                  f"{_md_escape(payload.get('heldout_samples'))} / {_md_escape(payload.get('shadow_samples'))}",
                  "", "## Canonical payload", "", "```json",
                  html.escape(canonical_json(payload), quote=False).replace("`", "&#96;"),
                  "```", ""])
    return "\n".join(lines)
