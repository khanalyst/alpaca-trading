"""Deterministic proof artifacts for research candidates.

The proof boundary consumes a ledger-like object but does not depend on a
particular ledger implementation.  It copies only stable identifiers,
validated specifications, metrics, and provenance hashes into a canonical
payload.  Wall-clock timestamps, raw model responses, HTML, and source text
are intentionally excluded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import html
import inspect
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
from threading import Thread
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from agent.contracts.rule import validate_rule_spec


PROOF_SCHEMA = "research-proof.v1"
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
    direct = context.get(name)
    if direct is None and isinstance(ledger, Mapping):
        direct = ledger.get(name)
    if direct is None:
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
    candidate = _candidate_record(ledger, candidate_id, context)
    if not candidate:
        raise KeyError(f"unknown candidate {candidate_id!r}")
    runs = _records(ledger, candidate_id, context, "runs", "run")
    evidence = _records(ledger, candidate_id, context, "evidence", "evidence_item")
    events = _records(ledger, candidate_id, context, "events", "event")
    if not events and "events" not in context:
        events = _records(ledger, candidate_id, context, "history", "event")
    # Reporting events are intentionally excluded from their own payload so
    # an idempotent rerun does not create a new report merely because the last
    # report was announced.
    events = [item for item in events
              if not str(item.get("event_type") or "").startswith("proof_")]
    trades = _records(ledger, candidate_id, context, "trades", "trade")
    run = _latest(runs)
    evidence_latest = _latest(evidence)
    vehicle = str(context.get("vehicle") or candidate.get("vehicle") or run.get("vehicle") or "")
    if vehicle not in {"equity", "option"}:
        raise ValueError("vehicle must be equity or option")
    status = str(context.get("status") or candidate.get("status") or "unknown")
    spec = _extract_spec(candidate, context)
    metric_values, samples = _metrics(run, context, evidence, trades)
    hashes = _hashes(candidate, run, context)
    provider = _provider_info(candidate, run, evidence, context)
    session = _session_info(context, candidate, run, evidence)

    run_ids = sorted({str(item.get("run_id")) for item in runs if item.get("run_id") is not None})
    evidence_ids = sorted({str(item.get("evidence_id")) for item in evidence if item.get("evidence_id") is not None})
    event_ids = sorted({str(item.get("event_id")) for item in events if item.get("event_id") is not None})
    statuses = {"candidate": status,
                "runs": sorted(str(item.get("lane") or item.get("status") or "")
                                for item in runs)}
    if isinstance(context.get("statuses"), Mapping):
        statuses.update(_finite(context["statuses"]))

    llm_hashes = context.get("llm_evidence_hashes")
    if llm_hashes is None:
        llm_context = context.get("llm_evidence")
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
        "run_ids": run_ids,
        "evidence_id": evidence_latest.get("evidence_id"),
        "evidence_ids": evidence_ids,
        "event_ids": event_ids,
        "status": status,
        "statuses": statuses,
        "variant_id": str(context.get("variant_id") or candidate.get("variant_id") or ""),
        "strategy_id": str(context.get("strategy_id") or candidate.get("strategy_id") or ""),
        "strategy": str(context.get("strategy_id") or candidate.get("strategy_id") or ""),
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
    if not math.isfinite(float(timeout_seconds)) or not 0 < float(timeout_seconds) <= 30:
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
        worker.join(float(timeout_seconds))
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


def write_proof(ledger: Any, candidate_id: str, context: Mapping[str, Any] | None = None,
                output_root: str | Path = "findings", webhook_url: str | None = None,
                webhook_sender: Callable[..., Any] | None = None,
                webhook_timeout_seconds: float = 5.0,
                hash_prefix_length: int = 16) -> ProofResult:
    """Create one deterministic artifact atomically and never overwrite it."""

    payload = build_proof_payload(ledger, candidate_id, context)
    digest = payload_hash(payload)
    if hash_prefix_length < 8 or hash_prefix_length > 64:
        raise ValueError("hash_prefix_length must be between 8 and 64")
    vehicle = str(payload["vehicle"])
    safe_candidate = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(candidate_id)).strip("._") or "candidate"
    target_dir = Path(output_root) / vehicle
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{safe_candidate}-{digest[:hash_prefix_length]}.md"
    markdown = render_markdown(payload, digest)
    data = markdown.encode("utf-8")
    # O_EXCL gives atomic create/no overwrite semantics even for concurrent
    # proof writers targeting the same candidate and payload.
    created = True
    try:
        descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        created = False
        if target.read_bytes() != data:
            raise RuntimeError(f"existing proof artifact does not match payload: {target}")
    else:
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor != -1:
                os.close(descriptor)

    webhook = None
    if webhook_url and created:
        webhook = send_webhook(webhook_url, {
            "candidate_id": candidate_id, "vehicle": vehicle,
            "status": payload.get("status"), "payload_hash": digest,
            "artifact": str(target.relative_to(Path(output_root))),
        }, sender=webhook_sender, timeout_seconds=webhook_timeout_seconds)
    return ProofResult(payload=payload, payload_hash=digest, path=target,
                       created=created, webhook=webhook)


# Compatibility aliases for callers using imperative or artifact terminology.
create_proof = write_proof
emit_proof = write_proof
build_proof = write_proof
write_proof_artifact = write_proof
generate_proof = write_proof


__all__ = [
    "PROOF_SCHEMA", "ProofResult", "canonical_json", "payload_hash",
    "build_proof_payload", "render_markdown", "send_webhook", "write_proof",
    "create_proof", "emit_proof", "build_proof", "write_proof_artifact",
    "generate_proof",
]
