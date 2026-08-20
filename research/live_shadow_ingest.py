"""Ingest complete, parity-matched live-shadow replays into EdgeLedger.

The recorder/shadow process is intentionally not a lifecycle authority.  This
module is the research-side write boundary: it opens the shadow WAL read-only,
requires a complete replay tail (including the paired control and null
sources), splits it into disjoint chronological selection and confirmatory
windows, rebuilds the existing verified-gate envelope, and only then appends
an immutable ``lane='shadow'`` run to EdgeLedger. Batch family/global BH
q-values select the candidate; the v4 LORD scope uses the raw-p v3 method and
receives only the newer confirmatory gate's raw p-value. A missing, mismatched,
or already-consumed session is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .edge_discovery_core import _discover_gate, _finalize_gate
from .edge_lab import _strengthen_gate
from .edge_ledger import EdgeLedger, VEHICLES, provenance_hash
from .edge_ledger_store import REPLAY_ENGINE_EPOCH, content_hash
from .costs import ReplayPolicy
from .gates import sample_counts, verify_gate_envelope, validate_protocol_floor
from .live_shadow import (
    REPLAY_QUARANTINE_OVERFLOW_KEY, ShadowError, ShadowStore,
    _opportunity_capacity,
)
from agent.contracts.rule import rule_variant_id, validate_rule_spec
from .factory_ledger import FDR_METHOD, FactoryLedger
from .stats import benjamini_hochberg


INGEST_SCHEMA = "shadow-ingest.v1"
DEFAULT_MIN_TRADES = 150
DEFAULT_MIN_SESSIONS = 30
DEFAULT_ALPHA = 0.05
DEFAULT_CONFIRMATORY_ITERATIONS = 20_000
MAX_CONFIRMATORY_ITERATIONS = 2_000_000
# Selection rows are retained so the adaptive p and BH inputs can be
# re-verified from durable evidence. Keep this audit payload bounded; a larger
# tail is a diagnostic/no-op and must be replayed in smaller chronological
# cycles rather than silently truncating the selection source.
MAX_SELECTION_SOURCE_ROWS = 100_000
# v4 starts a fresh durable LORD sequence.  The prior v2 sequence spent
# family/global BH q-values; those rows remain readable for audit but must not
# be reused for raw-p confirmatory spending.
CONFIRMATORY_SCOPE_VERSION = "shadow-confirmation-v4"
CONFIRMATORY_P_VALUE_SOURCE = "live_shadow_confirmatory_gate"


def _confirmatory_scope(vehicle: str) -> str:
    return f"{CONFIRMATORY_SCOPE_VERSION}:{vehicle}"


def _confirmatory_iterations(allocated_alpha: float, batch_size: int) -> int:
    """Resolve a BH-adjusted p-value below the next online allocation."""
    allocated = float(allocated_alpha)
    if not math.isfinite(allocated) or allocated <= 0:
        raise ValueError("confirmatory allocation must be positive and finite")
    required = int(math.ceil(max(1, int(batch_size)) / allocated))
    return max(DEFAULT_CONFIRMATORY_ITERATIONS, required)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _confirmatory_test_id(candidate_id: str, source: Mapping[str, Any]) -> str:
    """Build the crash-safe v4 FDR idempotency key.

    Resolution, alpha, p-values, and BH metadata may legitimately differ when
    a process retries the same uncommitted tail.  Only the candidate lineage
    and the immutable chronological/session evidence identify the hypothesis
    being spent, so keep those fields explicit and exclude all computation
    results from this key.
    """
    selection = source.get("selection")
    confirmatory = source.get("confirmatory")
    if not isinstance(selection, Mapping) or not isinstance(confirmatory, Mapping):
        raise ValueError("confirmatory source windows are required for FDR identity")
    immutable = {
        "candidate_id": str(candidate_id),
        "selection_session_digest": source.get("selection_session_digest"),
        "confirmatory_session_digest": source.get("confirmatory_session_digest"),
        "selection_rows_digest": selection.get("rows_digest"),
        "selection_baseline_rows_digest": selection.get("baseline_rows_digest"),
        "selection_null_rows_digest": selection.get("null_rows_digest"),
        "confirmatory_rows_digest": confirmatory.get("rows_digest"),
        "confirmatory_baseline_rows_digest": confirmatory.get("baseline_rows_digest"),
        "confirmatory_null_rows_digest": confirmatory.get("null_rows_digest"),
    }
    if any(not isinstance(value, str) or not value
           for key, value in immutable.items() if key != "candidate_id"):
        raise ValueError("confirmatory source digests are required for FDR identity")
    return f"{candidate_id}:{_digest(immutable)}"


def _config(candidate: Mapping[str, Any]) -> dict[str, Any]:
    value = candidate.get("config")
    if value is None and isinstance(candidate.get("config_json"), str):
        try:
            value = json.loads(str(candidate["config_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            value = None
    return dict(value) if isinstance(value, Mapping) else {}


def _session(value: Any) -> str:
    return str(value or "")


def _split_sessions(sessions: Sequence[str], min_sessions: int) -> tuple[list[str], list[str]]:
    """Split a complete chronological tail into disjoint windows.

    The split is deliberately deterministic and happens before any gate or
    p-value is computed.  The extra session in an odd-sized tail belongs to
    the confirmatory half, so the final boundary remains the newest consumed
    session.
    """
    ordered = sorted({_session(item) for item in sessions if _session(item)})
    required = max(1, int(min_sessions))
    if len(ordered) < 2 * required:
        return [], []
    cut = len(ordered) // 2
    selection, confirmatory = ordered[:cut], ordered[cut:]
    if len(selection) < required or len(confirmatory) < required:
        return [], []
    return selection, confirmatory


def _window_counts(rows: Sequence[Mapping[str, Any]], vehicle: str, *,
                   equity_feed: str = "iex") -> dict[str, Any]:
    counts = sample_counts(
        rows, vehicle=vehicle, equity_feed=equity_feed)
    capacity = _opportunity_capacity(rows, vehicle=vehicle)
    return {
        **{key: int(counts.get(key, 0)) for key in
           ("trades", "sessions", "clusters")},
        "observed_trades": int(capacity["observed_trades"]),
        "observed_sessions": int(counts.get("sessions", 0)),
        "opportunity_count": int(capacity["opportunity_count"]),
        "max_trade_opportunities": int(capacity["max_trade_opportunities"]),
        "opportunity_sessions": int(capacity["opportunity_sessions"]),
        "observed_trade_rate": float(capacity["observed_trade_rate"]),
        "refusal_reason_counts": dict(capacity["refusal_reason_counts"]),
    }


def _window_capacity(rows: Sequence[Mapping[str, Any]], *, vehicle: str,
                     min_trades: int, min_sessions: int) -> dict[str, Any]:
    """Return bounded non-authorizing capacity/readiness telemetry."""
    return _opportunity_capacity(
        rows, vehicle=vehicle, min_trades=int(min_trades),
        min_sessions=int(min_sessions))


def _window_ready(rows: Sequence[Mapping[str, Any]], *, vehicle: str,
                  min_trades: int, min_sessions: int,
                  equity_feed: str = "iex") -> tuple[bool, dict[str, Any]]:
    counts = _window_counts(rows, vehicle, equity_feed=equity_feed)
    return bool(counts["trades"] >= int(min_trades) and
                counts["sessions"] >= int(min_sessions)), counts


def _latest_boundary(ledger: EdgeLedger, candidate_id: str) -> str | None:
    """Return the greatest session consumed by a complete authorizing run.

    A crash can leave a deterministic shadow run (and its held-out bounds)
    durable before trades/evidence/lifecycle completion.  Such a run must not
    advance the source boundary or its retry would be mistaken for a no-op.
    Superseded replay epochs likewise remain readable but cannot consume the
    current epoch's tail.
    """
    values: list[str] = []
    evidence = ledger.evidence(candidate_id)
    # A durable validated transition binds lifecycle completion to the exact
    # run.  Looking only at current candidate status would lose boundaries
    # after a later safety demotion and could replay already-consumed sessions.
    validated_runs: set[str] = set()
    for event in ledger.history(candidate_id):
        if event.get("to_status") != "validated":
            continue
        try:
            payload = json.loads(str(event.get("payload_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping) and payload.get("run_id"):
            validated_runs.add(str(payload["run_id"]))
    for run in ledger.runs(candidate_id):
        if run.get("lane") == "shadow":
            metrics = run.get("metrics")
            if (not isinstance(metrics, Mapping) or
                    metrics.get("replay_engine_epoch") != int(REPLAY_ENGINE_EPOCH) or
                    str(run.get("run_id")) not in validated_runs):
                continue
            run_evidence = [item for item in evidence
                            if item.get("run_id") == run.get("run_id")]
            markers = [item for item in run_evidence
                        if item.get("kind") == "shadow_ingestion"]
            gates = [item for item in run_evidence
                     if item.get("kind") == "verified_gate"]
            if (len(markers) != 1 or len(gates) != 1 or
                    any(item.get("evidence_hash") != content_hash(item.get("payload"))
                        for item in (markers + gates))):
                continue
        for key in ("fit_end", "heldout_end"):
            if run.get(key):
                values.append(str(run[key]))
        gate = (run.get("metrics") or {}).get("gate")
        if isinstance(gate, Mapping):
            qualification = gate.get("qualification")
            if isinstance(qualification, Mapping):
                values.extend(str(item) for item in
                              (qualification.get("sessions") or ()) if item)
    return max(values) if values else None


def _latest_gate(ledger: EdgeLedger, candidate_id: str) -> tuple[dict, dict] | None:
    """Use the latest re-verifying proof as the fixed gate configuration."""
    for lane in ("shadow", "backtest"):
        result = ledger.latest_verified_run(candidate_id, lane=lane)
        if isinstance(result, Mapping):
            gate = result.get("verified_gate")
            if isinstance(gate, Mapping):
                return dict(result), dict(gate)
    return None


def _meta_by_session(store: ShadowStore, candidate_id: str,
                     sessions: Sequence[str], vehicle: str) -> tuple[dict[str, dict], str | None]:
    """Validate complete replay/account metadata for one candidate."""
    wanted = set(str(item) for item in sessions)
    rows = [row for row in store.replay_metadata(candidate_id)
            if str(row.get("session_date") or "") in wanted]
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("session_date") or ""), []).append(row)
    result: dict[str, dict] = {}
    for day in sorted(wanted):
        values = grouped.get(day, [])
        if len(values) != 1:
            return {}, f"session {day} has incomplete replay metadata"
        row = values[0]
        details = row.get("details")
        if (row.get("status") != "match" or row.get("replay_status") != "match" or
                not all(isinstance(row.get(key), str) and row.get(key)
                        for key in ("source_digest", "shadow_digest",
                                    "replay_digest")) or
                row.get("account_id") is None or
                row.get("vehicle") != vehicle or not isinstance(details, Mapping) or
                details.get("complete") is not True or
                details.get("signature_match") is not True):
            return {}, f"session {day} is not a complete parity match"
        result[day] = row
    return result, None


def _rows_for(store: ShadowStore, candidate_id: str, sessions: Sequence[str],
              vehicle: str) -> tuple[list[dict], str | None]:
    metadata, reason = _meta_by_session(store, candidate_id, sessions, vehicle)
    if reason:
        return [], reason
    rows: list[dict] = []
    for day in sorted(metadata):
        session_rows = store.gate_rows(candidate_id, day)
        if not session_rows:
            return [], f"session {day} has no parity-matched gate rows"
        for row in session_rows:
            if str(row.get("vehicle") or vehicle) != vehicle:
                return [], f"session {day} contains a cross-vehicle row"
            rows.append(dict(row))
    return rows, None


def _session_continuity(store: ShadowStore, boundary: str,
                        available: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return (missing, unknown) sessions from exact recorder provenance.

    The shadow WAL carries a monotonic catalog populated from the recorder's
    Alpaca calendar sidecar.  It is intentionally not synthesized from
    weekdays or timestamps.  Once a newer catalog-backed session is present,
    an available session absent from that catalog is also unknown and blocks
    authorization until the operator repairs/replays the source.
    """
    catalog = store.session_catalog()
    authoritative = sorted(
        str(session) for session, detail in catalog.items()
        if str(session) > str(boundary)
        and isinstance(detail, Mapping)
        and str(detail.get("source") or "") == "recorder_alpaca_calendar")
    if not authoritative:
        observed = {str(session) for session in available if str(session) > str(boundary)}
        return [], sorted(observed)
    observed = {str(session) for session in available if str(session) > str(boundary)}
    authoritative_set = set(authoritative)
    observed_catalog = observed & authoritative_set
    if not observed_catalog:
        return [], sorted(observed)
    newest = max(observed_catalog)
    missing = [session for session in authoritative
               if session <= newest and session not in observed]
    unknown = sorted(session for session in observed if session not in authoritative_set)
    return missing, unknown


def _preflight_ready(gate: Mapping[str, Any]) -> tuple[bool, dict[str, bool]]:
    """Report non-multiplicity requirements before selecting an online test.

    A raw p-value is still useful for the batch BH correction when a candidate
    is underpowered or fails a control check, but such a candidate must not
    consume the single online allocation.  The candidate-level p-significance
    check is intentionally excluded here; BH supplies that multiplicity gate
    across the complete tested family.
    """
    checks = gate.get("checks_without_family")
    if not isinstance(checks, Mapping):
        return False, {"checks_available": False}
    ready_checks = {
        str(key): bool(value) for key, value in checks.items()
        if str(key) != "heldout_p_significant"
    }
    qualification = gate.get("qualification")
    ready_checks["qualification_available"] = bool(
        isinstance(qualification, Mapping) and qualification.get("available"))
    return bool(ready_checks) and all(ready_checks.values()), ready_checks


@dataclass(frozen=True)
class ShadowIngestConfig:
    edge_db: Path
    shadow_db: Path
    vehicle: str | None = None
    candidate_id: str | None = None
    baseline_candidate_id: str | None = None
    null_candidate_id: str | None = None
    min_trades: int = DEFAULT_MIN_TRADES
    min_sessions: int = DEFAULT_MIN_SESSIONS
    alpha: float = DEFAULT_ALPHA

    def __post_init__(self) -> None:
        try:
            validate_protocol_floor(lane="shadow", min_trades=self.min_trades,
                                    min_sessions=self.min_sessions)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if isinstance(self.alpha, bool) or not isinstance(self.alpha, (int, float)) \
                or not 0.0 < float(self.alpha) <= 1.0:
            raise ValueError("alpha must be finite and in (0,1]")
        if self.vehicle is not None and self.vehicle not in VEHICLES:
            raise ValueError("vehicle must be equity or option")


class ShadowIngestor:
    """Research-side shadow WAL consumer."""

    def __init__(self, config: ShadowIngestConfig):
        self.config = config
        self.ledger = EdgeLedger(config.edge_db)
        # Never initialize, migrate, or write the broker-free shadow WAL from
        # this process.  A missing DB is simply an empty/no-op source.
        self.store = (ShadowStore(config.shadow_db, readonly=True)
                      if Path(config.shadow_db).is_file() else None)

    def _candidate_ids(self) -> list[str]:
        if self.config.candidate_id:
            return [str(self.config.candidate_id)]
        candidate_ids: list[str] = []
        for row in self.ledger.status():
            status = row.get("status")
            if status not in {"backtest_passed", "shadow", "demoted",
                              "validated", "champion"}:
                continue
            if self.config.vehicle is not None and row.get("vehicle") != self.config.vehicle:
                continue
            if status in {"validated", "champion"}:
                try:
                    if self.ledger.eligibility(str(row["candidate_id"]), lane="shadow").get("eligible"):
                        continue
                except (KeyError, ValueError, TypeError):
                    # A malformed legacy proof is exactly the migration case
                    # this consumer must be able to recover.
                    pass
            candidate_ids.append(str(row["candidate_id"]))
        return candidate_ids

    def _paired_id(self, candidate: Mapping[str, Any], explicit: str | None,
                   *, null: bool = False) -> str | None:
        if explicit:
            return str(explicit)
        if null:
            # ShadowRunner persists one deterministic randomized-entry null
            # WAL namespace per candidate/session.  An explicit id remains
            # available for externally supplied control workers.
            return f"shadow:null:{candidate['candidate_id']}"
        if str(candidate.get("strategy_id")) == "ibr":
            baseline = self.ledger.candidate_by_variant(
                "ibr.baseline", str(candidate.get("vehicle")))
            if isinstance(baseline, Mapping):
                return str(baseline["candidate_id"])
        if str(candidate.get("strategy_id")) == "rule":
            axes = candidate.get("axes")
            if axes is None and isinstance(candidate.get("axes_json"), str):
                try:
                    axes = json.loads(str(candidate["axes_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    axes = None
            hypothesis_id = axes.get("hypothesis_id") if isinstance(axes, Mapping) else None
            if hypothesis_id:
                try:
                    hypothesis = FactoryLedger(self.config.edge_db).hypothesis(str(hypothesis_id))
                    root_spec = hypothesis.get("rule_spec") if isinstance(hypothesis, Mapping) else None
                    if isinstance(root_spec, Mapping):
                        root_variant = rule_variant_id(validate_rule_spec(root_spec))
                        if str(candidate.get("variant_id")) == root_variant:
                            # A root hypothesis is compared to the exact-window
                            # randomized-entry null, never to its own replay.
                            return f"shadow:null:{candidate['candidate_id']}"
                        return f"shadow:baseline:{candidate['candidate_id']}"
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    pass
        return None

    @staticmethod
    def _family(candidate: Mapping[str, Any]) -> str:
        if str(candidate.get("strategy_id")) == "rule":
            spec = (_config(candidate).get("strategy") or {}).get("rule_spec")
            if isinstance(spec, Mapping) and spec.get("family"):
                return f"rule:{spec['family']}"
        variant = str(candidate.get("variant_id") or "")
        parts = variant.split(".")
        return f"{candidate.get('strategy_id', 'unknown')}:{parts[1] if len(parts) > 1 else variant}"

    @staticmethod
    def _trade_id(run_id: str, index: int, row: Mapping[str, Any]) -> str:
        """Return the immutable id used for one confirmatory source row."""
        return _digest({"run_id": run_id, "index": index,
                        "opportunity_id": row.get("opportunity_id")})

    def _existing_run_error(self, run: Mapping[str, Any], *, candidate_id: str,
                            vehicle: str, source: Mapping[str, Any],
                            replay_digests: Sequence[str], gate: Mapping[str, Any],
                            hashes: Mapping[str, Any]) -> str | None:
        """Validate the immutable identity before resuming a partial run."""
        if (run.get("run_id") is None or run.get("candidate_id") != candidate_id or
                run.get("lane") != "shadow" or run.get("vehicle") != vehicle):
            return "existing shadow run identity conflicts with candidate"
        metrics = run.get("metrics")
        if not isinstance(metrics, Mapping):
            return "existing shadow run metrics are invalid"
        if (metrics.get("shadow_source") != dict(source) or
                metrics.get("replay_digests") != list(replay_digests)):
            return "existing shadow run source identity conflicts with retry"
        if metrics.get("replay_engine_epoch") != int(REPLAY_ENGINE_EPOCH):
            return "existing shadow run replay engine epoch is stale"
        expected_envelope = gate.get("verified_gate") if isinstance(gate, Mapping) else None
        existing_gate = metrics.get("gate")
        existing_envelope = (existing_gate.get("verified_gate")
                             if isinstance(existing_gate, Mapping) else None)
        if (not isinstance(expected_envelope, Mapping) or
                not isinstance(existing_envelope, Mapping) or
                content_hash(existing_envelope) != content_hash(expected_envelope)):
            return "existing shadow run gate identity conflicts with retry"
        for key in ("dataset_hash", "config_hash", "code_hash", "provenance_hash"):
            if run.get(key) != hashes.get(key):
                return f"existing shadow run {key} conflicts with retry"
        return None

    def _run_evidence(self, candidate_id: str, run_id: str,
                      kind: str) -> list[dict[str, Any]]:
        return [item for item in self.ledger.evidence(candidate_id)
                if item.get("run_id") == run_id and item.get("kind") == kind]

    @staticmethod
    def _evidence_payload_valid(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
        payload = item.get("payload")
        if (not isinstance(payload, Mapping) or
                item.get("evidence_hash") != content_hash(payload)):
            return None
        return payload

    def _reconcile_run(self, *, candidate_id: str, vehicle: str,
                       run_id: str, run: Mapping[str, Any],
                       confirmatory_rows: Sequence[Mapping[str, Any]],
                       gate: Mapping[str, Any], marker: Mapping[str, Any]) -> tuple[bool, str | None]:
        """Complete a run left partially durable by a process crash.

        Every existing row is checked against the deterministic source before
        a missing row/evidence item is appended.  Any unexpected or conflicting
        durable state fails closed; concurrent retries may safely race because
        each insert is idempotently re-checked after a uniqueness error.
        """
        changed = False
        expected: dict[str, tuple[Mapping[str, Any], str]] = {
            self._trade_id(run_id, index, row): (
                row, str(row.get("opportunity_id") or row.get("entry_timestamp") or ""))
            for index, row in enumerate(confirmatory_rows)
        }
        durable = [item for item in self.ledger.trades(candidate_id, lane="shadow")
                   if item.get("run_id") == run_id]
        seen: set[str] = set()
        for item in durable:
            tid = str(item.get("trade_id") or "")
            if tid not in expected or tid in seen:
                return False, "existing shadow run contains unexpected or duplicate trade"
            seen.add(tid)
            row, opportunity = expected[tid]
            if str(item.get("opportunity_id") or "") != opportunity:
                return False, "existing shadow run trade identity conflicts with retry"
            try:
                payload = json.loads(str(item.get("payload_json") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                return False, "existing shadow run trade payload is invalid"
            if not isinstance(payload, Mapping) or dict(payload) != dict(row):
                return False, "existing shadow run trade payload conflicts with retry"
        for index, row in enumerate(confirmatory_rows):
            tid = self._trade_id(run_id, index, row)
            if tid in seen:
                continue
            try:
                self.ledger.append_trade(run_id, row, trade_id=tid)
            except sqlite3.IntegrityError:
                # Another consumer may have inserted the same deterministic
                # row.  Re-read and accept it only when it is byte-for-byte
                # equivalent to this retry's source.
                current = [item for item in self.ledger.trades(candidate_id, lane="shadow")
                           if item.get("run_id") == run_id and item.get("trade_id") == tid]
                if len(current) != 1:
                    return False, "concurrent shadow trade insert is conflicting"
                try:
                    payload = json.loads(str(current[0].get("payload_json") or ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    return False, "concurrent shadow trade payload is invalid"
                if dict(payload) != dict(row):
                    return False, "concurrent shadow trade payload conflicts with retry"
            changed = True
            seen.add(tid)

        expected_envelope = gate.get("verified_gate") if isinstance(gate, Mapping) else None
        gates = self._run_evidence(candidate_id, run_id, "verified_gate")
        if len(gates) > 1:
            return False, "existing shadow run has duplicate verified gate evidence"
        if gates:
            payload = self._evidence_payload_valid(gates[0])
            if (not isinstance(payload, Mapping) or
                    payload.get("gate_hash") != (expected_envelope or {}).get("content_hash") or
                    payload.get("gate") != expected_envelope):
                return False, "existing shadow run verified gate conflicts with retry"
        else:
            try:
                self.ledger.record_verified_gate(run_id, gate)
            except (sqlite3.IntegrityError, ValueError):
                gates = self._run_evidence(candidate_id, run_id, "verified_gate")
                if len(gates) != 1:
                    return False, "concurrent verified gate insert is conflicting"
                payload = self._evidence_payload_valid(gates[0])
                if (not isinstance(payload, Mapping) or
                        payload.get("gate_hash") != (expected_envelope or {}).get("content_hash") or
                        payload.get("gate") != expected_envelope):
                    return False, "concurrent verified gate conflicts with retry"
            changed = True

        markers = self._run_evidence(candidate_id, run_id, "shadow_ingestion")
        if len(markers) > 1:
            return False, "existing shadow run has duplicate shadow ingestion markers"
        if markers:
            payload = self._evidence_payload_valid(markers[0])
            if not isinstance(payload, Mapping) or dict(payload) != dict(marker):
                return False, "existing shadow ingestion marker conflicts with retry"
        else:
            try:
                self.ledger.record_shadow_ingestion(candidate_id, marker, run_id=run_id)
            except (sqlite3.IntegrityError, ValueError):
                markers = self._run_evidence(candidate_id, run_id, "shadow_ingestion")
                if len(markers) != 1:
                    return False, "concurrent shadow marker insert is conflicting"
                payload = self._evidence_payload_valid(markers[0])
                if not isinstance(payload, Mapping) or dict(payload) != dict(marker):
                    return False, "concurrent shadow marker conflicts with retry"
            changed = True
        return changed, None

    def _one(self, candidate_id: str, *, correction: Mapping[str, Any] | None = None,
             dry: bool = False,
             test_iterations: int = DEFAULT_CONFIRMATORY_ITERATIONS) -> dict[str, Any]:
        if self.store is None:
            return {"candidate_id": candidate_id, "status": "no_shadow_db",
                    "ingested": False}
        candidate = self.ledger.candidate(candidate_id)
        if not isinstance(candidate, Mapping):
            return {"candidate_id": candidate_id, "status": "unknown_candidate",
                    "ingested": False}
        vehicle = str(candidate.get("vehicle") or "")
        if vehicle not in VEHICLES:
            return {"candidate_id": candidate_id, "status": "invalid_vehicle",
                    "ingested": False}
        if self.config.vehicle is not None and vehicle != self.config.vehicle:
            return {"candidate_id": candidate_id, "status": "vehicle_filtered",
                    "ingested": False}
        equity_feed = ReplayPolicy.from_config(_config(candidate)).equity_feed
        prior = _latest_gate(self.ledger, candidate_id)
        boundary = _latest_boundary(self.ledger, candidate_id)
        if prior is None or boundary is None:
            return {"candidate_id": candidate_id, "status": "no_prior_proof",
                    "ingested": False, "boundary": boundary}

        # A short explicit retention window can remove replay metadata needed
        # to prove the chronological tail after this candidate's boundary. The
        # non-authorizing watermark makes that loss visible; never treat the
        # remaining subset as a complete tail or advance the boundary across
        # the gap.
        retention_watermark = self.store.prune_watermark()
        latest_pruned = (str(retention_watermark.get("latest_pruned_session"))
                         if isinstance(retention_watermark, Mapping) and
                         retention_watermark.get("latest_pruned_session") else None)
        if latest_pruned is not None and str(boundary) < latest_pruned:
            return {
                "candidate_id": candidate_id,
                "status": "retention_gap",
                "reason": "shadow replay metadata was pruned after the prior boundary",
                "ingested": False,
                "boundary": boundary,
                "retention_gap": True,
                "retention_watermark": retention_watermark,
                "stale_tail": {"status": "blocked",
                               "reason": "retention_gap",
                               "latest_pruned_session": latest_pruned},
            }
        baseline_id = self._paired_id(candidate, self.config.baseline_candidate_id)
        null_id = self._paired_id(candidate, self.config.null_candidate_id, null=True)
        if not baseline_id or not null_id:
            return {"candidate_id": candidate_id, "status": "control_unavailable",
                    "ingested": False, "boundary": boundary,
                    "baseline_candidate_id": baseline_id,
                    "null_candidate_id": null_id}

        # Build the forward tail from every required arm.  Looking only at the
        # candidate arm can silently skip a control-only or malformed session
        # and then advance the candidate boundary past evidence still being
        # repaired.  Unknown session labels are explicit incomplete tails.
        arm_metadata = {
            "candidate": self.store.replay_metadata(candidate_id),
            "baseline": self.store.replay_metadata(baseline_id),
            "null": self.store.replay_metadata(null_id),
        }
        unknown_sessions = sorted({
            _session(row.get("session_date"))
            for values in arm_metadata.values() for row in values
            if not _session(row.get("session_date"))
        })
        if unknown_sessions:
            return {"candidate_id": candidate_id, "status": "incomplete",
                    "reason": "shadow metadata contains an unknown session",
                    "unknown_sessions": unknown_sessions, "ingested": False,
                    "boundary": boundary,
                    "stale_tail": {"status": "blocked",
                                   "unknown_sessions": unknown_sessions}}
        session_sets = {
            _session(row.get("session_date"))
            for values in arm_metadata.values() for row in values
            if _session(row.get("session_date"))
        }
        stale_sessions = sorted(day for day in session_sets if day <= boundary)
        available = sorted(day for day in session_sets if day > boundary)
        if not available:
            return {"candidate_id": candidate_id, "status": "no_new_session",
                    "ingested": False, "boundary": boundary,
                    "stale_tail": {"status": "stale" if stale_sessions else "clear",
                                   "sessions": stale_sessions,
                                   "unknown_sessions": []}}
        catalog = self.store.session_catalog()
        replay_quarantine = self.store.replay_quarantine()
        overflow = replay_quarantine.get(REPLAY_QUARANTINE_OVERFLOW_KEY)
        if isinstance(overflow, Mapping) and str(overflow.get("status") or "") == "overflow":
            return {
                "candidate_id": candidate_id,
                "status": "repair_required",
                "reason": "replay quarantine overflow requires operator rebuild",
                "quarantine_overflow": True,
                "repair_required": [dict(overflow)],
                "ingested": False,
                "boundary": boundary,
                "sessions": available,
                "stale_tail": {
                    "status": "blocked",
                    "reason": "replay_quarantine_overflow",
                    "quarantine_overflow": True,
                    "active_count": overflow.get("active_count"),
                    "active_digest": overflow.get("active_digest"),
                },
            }
        if not catalog:
            return {
                "candidate_id": candidate_id,
                "status": "incomplete",
                "reason": "authoritative recorder session catalog unavailable",
                "catalog_unavailable": True,
                "ingested": False,
                "boundary": boundary,
                "sessions": available,
                "stale_tail": {
                    "status": "blocked",
                    "reason": "catalog_unavailable",
                    "catalog_unavailable": True,
                },
            }
        missing_sessions, unknown_catalog_sessions = _session_continuity(
            self.store, boundary, available)
        if missing_sessions or unknown_catalog_sessions:
            return {
                "candidate_id": candidate_id,
                "status": "incomplete",
                "reason": ("authoritative recorder calendar has an unobserved "
                           "mid-tail session" if missing_sessions else
                           "available replay session lacks authoritative calendar "
                           "provenance: " + ", ".join(
                               f"session {session}" for session in unknown_catalog_sessions)),
                "ingested": False,
                "boundary": boundary,
                "sessions": available,
                "missing_sessions": missing_sessions,
                "unknown_sessions": unknown_catalog_sessions,
                "stale_tail": {
                    "status": "blocked",
                    "missing_sessions": missing_sessions,
                    "unknown_sessions": unknown_catalog_sessions,
                    "reason": "authoritative_session_continuity_gap",
                },
            }
        # A prior incomplete/mismatched replay is a durable repair boundary,
        # not a reason to discard that session and continue with a newer tail.
        # ShadowRunner changes the entry to ``repaired`` only after a complete
        # parity replay; until then no gate/FDR/boundary mutation is allowed.
        required_arm_ids = {
            "candidate": candidate_id,
            "baseline": baseline_id,
            "null": null_id,
        }
        repair_required: list[dict[str, Any]] = []
        for day in available:
            for arm, arm_id in required_arm_ids.items():
                for detail in replay_quarantine.values():
                    if not isinstance(detail, Mapping):
                        continue
                    if (str(detail.get("candidate_id") or "") == str(arm_id) and
                            str(detail.get("session_date") or "") == day and
                            str(detail.get("status") or "") != "repaired"):
                        repair_required.append({**dict(detail), "arm": arm})
        if repair_required:
            repair_required.sort(key=lambda item: str(item.get("session_date") or ""))
            return {
                "candidate_id": candidate_id,
                "status": "repair_required",
                "reason": ("mid-tail replay session is quarantined; repair and "
                           "replay it before authorization can advance"),
                "ingested": False,
                "boundary": boundary,
                "sessions": available,
                "repair_required": repair_required,
                "stale_tail": {
                    "status": "blocked",
                    "sessions": [str(item.get("session_date") or "")
                                 for item in repair_required],
                    "reason": "replay_repair_required",
                },
            }
        selection_sessions, confirmatory_sessions = _split_sessions(
            available, self.config.min_sessions)
        if not selection_sessions or not confirmatory_sessions:
            return {
                "candidate_id": candidate_id,
                "status": "underpowered_confirmatory_split",
                "reason": ("complete live-shadow tail must contain at least "
                           "two independently adequate session windows"),
                "ingested": False,
                "boundary": boundary,
                "sessions": available,
                "selection_sessions": selection_sessions,
                "confirmatory_sessions": confirmatory_sessions,
                "online_allocation_spent": False,
            }
        # One incomplete/mismatched session blocks the complete tail.  This is
        # deliberately stricter than selecting only the currently matching
        # subset, which would advance a boundary past data still being replayed.
        rows, reason = _rows_for(self.store, candidate_id, available, vehicle)
        if reason:
            return {"candidate_id": candidate_id, "status": "incomplete",
                    "reason": reason, "ingested": False, "boundary": boundary}

        baseline_rows, reason = _rows_for(self.store, baseline_id, available, vehicle)
        if reason:
            return {"candidate_id": candidate_id, "status": "control_incomplete",
                    "reason": reason, "ingested": False, "boundary": boundary}
        null_rows, reason = _rows_for(self.store, null_id, available, vehicle)
        if reason:
            return {"candidate_id": candidate_id, "status": "null_incomplete",
                    "reason": reason, "ingested": False, "boundary": boundary}

        def partition(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict], list[dict]]:
            selected = set(selection_sessions)
            confirmed = set(confirmatory_sessions)
            return ([dict(row) for row in rows
                     if _session(row.get("session_date")) in selected],
                    [dict(row) for row in rows
                     if _session(row.get("session_date")) in confirmed])

        selection_rows, confirmatory_rows = partition(rows)
        selection_baseline_rows, confirmatory_baseline_rows = partition(baseline_rows)
        selection_null_rows, confirmatory_null_rows = partition(null_rows)
        if (len(selection_rows) + len(selection_baseline_rows) +
                len(selection_null_rows) > MAX_SELECTION_SOURCE_ROWS):
            return {
                "candidate_id": candidate_id,
                "status": "selection_evidence_too_large",
                "reason": ("durable selection evidence exceeds the bounded "
                           f"{MAX_SELECTION_SOURCE_ROWS}-row limit"),
                "ingested": False,
                "boundary": boundary,
                "sessions": available,
                "selection_sessions": selection_sessions,
                "confirmatory_sessions": confirmatory_sessions,
                "online_allocation_spent": False,
            }
        window_checks: dict[str, dict[str, Any]] = {}
        for name, arms in (
                ("selection", (selection_rows, selection_baseline_rows,
                               selection_null_rows)),
                ("confirmatory", (confirmatory_rows, confirmatory_baseline_rows,
                                   confirmatory_null_rows))):
            arm_checks: dict[str, Any] = {}
            for arm_name, arm_rows in zip(("candidate", "baseline", "null"), arms):
                ready, counts = _window_ready(
                    arm_rows, vehicle=vehicle,
                    min_trades=self.config.min_trades,
                    min_sessions=self.config.min_sessions,
                    equity_feed=equity_feed)
                capacity = _window_capacity(
                    arm_rows, vehicle=vehicle,
                    min_trades=self.config.min_trades,
                    min_sessions=self.config.min_sessions)
                arm_checks[arm_name] = {
                    "ready": ready,
                    "counts": counts,
                    "capacity": capacity,
                }
            arm_checks["ready"] = all(
                bool(value.get("ready")) for key, value in arm_checks.items()
                if key != "ready")
            arm_checks["capacity_feasible"] = all(
                bool(value.get("capacity", {}).get("capacity_feasible"))
                for key, value in arm_checks.items() if key not in {"ready", "capacity_feasible"})
            arm_checks["capacity_status"] = (
                "feasible" if arm_checks["capacity_feasible"]
                else "structurally_impossible")
            window_checks[name] = arm_checks
        if not all(bool(item.get("ready")) for item in window_checks.values()):
            structurally_impossible = any(
                not bool(item.get("capacity_feasible"))
                for item in window_checks.values())
            return {
                "candidate_id": candidate_id,
                "status": "underpowered_confirmatory_window",
                "reason": ("opportunity capacity is structurally impossible for the "
                           "configured floor" if structurally_impossible else
                           "selection and confirmatory windows must each meet structural minimums"),
                "ingested": False,
                "boundary": boundary,
                "sessions": available,
                "selection_sessions": selection_sessions,
                "confirmatory_sessions": confirmatory_sessions,
                "window_checks": window_checks,
                "capacity_status": ("structurally_impossible"
                                    if structurally_impossible else "underpowered_observed"),
                "online_allocation_spent": False,
            }

        candidate_meta, _ = _meta_by_session(self.store, candidate_id, available, vehicle)
        selection_session_digest = _digest(selection_sessions)
        confirmatory_session_digest = _digest(confirmatory_sessions)
        source = {
            "schema": INGEST_SCHEMA,
            "candidate_id": candidate_id,
            "vehicle": vehicle,
            "independent_confirmatory": True,
            "disjoint_sessions": True,
            "session_disjoint": True,
            "selection_sessions": selection_sessions,
            "confirmatory_sessions": confirmatory_sessions,
            "selection_session_digest": selection_session_digest,
            "confirmatory_session_digest": confirmatory_session_digest,
            "sessions": [
                {key: candidate_meta[day].get(key) for key in (
                    "session_date", "source_digest", "shadow_digest",
                    "replay_digest", "account_id", "trade_count")}
                for day in available
            ],
            "selection": {
                "sessions": selection_sessions,
                "session_digest": selection_session_digest,
                "rows_digest": _digest(selection_rows),
                "baseline_rows_digest": _digest(selection_baseline_rows),
                "null_rows_digest": _digest(selection_null_rows),
                "p_value_source": "selection_window_gate",
                "candidate_source": selection_rows,
                "baseline_source": selection_baseline_rows,
                "null_source": selection_null_rows,
                "minimums": {"trades": int(self.config.min_trades),
                              "sessions": int(self.config.min_sessions)},
            },
            "confirmatory": {
                "sessions": confirmatory_sessions,
                "session_digest": confirmatory_session_digest,
                "rows_digest": _digest(confirmatory_rows),
                "baseline_rows_digest": _digest(confirmatory_baseline_rows),
                "null_rows_digest": _digest(confirmatory_null_rows),
                "p_value_source": CONFIRMATORY_P_VALUE_SOURCE,
            },
            "baseline": {"candidate_id": baseline_id,
                         "rows_digest": _digest(baseline_rows),
                         "role": "paired_root_control"},
            "null": {"candidate_id": null_id,
                      "rows_digest": _digest(null_rows),
                      "role": "randomized_entry_null"},
            "capacity": {
                name: {
                    arm: dict(value.get("capacity") or {})
                    for arm, value in checks.items()
                    if arm in {"candidate", "baseline", "null"}
                }
                for name, checks in window_checks.items()
            },
        }
        previous_run, previous_gate = prior
        qualification = previous_gate.get("qualification")
        if not isinstance(qualification, Mapping) or not qualification.get("available"):
            return {"candidate_id": candidate_id, "status": "qualification_unavailable",
                    "ingested": False, "boundary": boundary}
        try:
            # Selection is computed only on the older half.  Its raw p-value
            # is the sole input to family/global BH; no confirmatory statistic
            # is available at this stage.
            selection_gate = _discover_gate(
                selection_rows, selection_baseline_rows, vehicle=vehicle,
                min_trades=self.config.min_trades,
                min_sessions=self.config.min_sessions,
                alpha=float(self.config.alpha), shadow=True,
                null_rows=selection_null_rows, qualification=qualification,
                test_iterations=test_iterations, equity_feed=equity_feed)
            selection_gate = _strengthen_gate(
                selection_gate, selection_baseline_rows, vehicle=vehicle,
                equity_feed=equity_feed)
            selection_p_value = float(selection_gate.get("candidate_p_raw", 1.0))
            source["selection"]["raw_p_value"] = selection_p_value
            preflight_ready, preflight_checks = _preflight_ready(selection_gate)
            if dry:
                return {"candidate_id": candidate_id, "status": "prepared",
                        "ingested": False, "raw_p": selection_p_value,
                        "preflight_ready": preflight_ready,
                        "preflight_checks": preflight_checks,
                        "family": self._family(candidate), "boundary": boundary,
                        "sessions": available,
                        "selection_sessions": selection_sessions,
                        "confirmatory_sessions": confirmatory_sessions,
                        "window_checks": window_checks,
                        "capacity_status": {
                            name: checks.get("capacity_status")
                            for name, checks in window_checks.items()},
                        "independent_confirmatory": True,
                        "test_iterations": int(test_iterations)}
            correction = dict(correction or {})
            family_data = correction.get("family") if isinstance(correction, Mapping) else None
            global_data = correction.get("global") if isinstance(correction, Mapping) else None
            family_data = family_data if isinstance(family_data, Mapping) else {
                "p_adjusted": selection_p_value,
                "significant": selection_p_value <= float(self.config.alpha),
                "family_size": 1}
            global_data = global_data if isinstance(global_data, Mapping) else family_data
            selected = bool(correction.get("selected"))
            q_value = float(global_data.get("p_adjusted", 1.0))
            family_q = float(family_data.get("p_adjusted", 1.0))
            source["selection"]["alpha"] = float(self.config.alpha)
            source["selection"]["test_iterations"] = int(test_iterations)
            batch_bh = correction.get("bh")
            if isinstance(batch_bh, Mapping):
                source["selection"]["bh"] = dict(batch_bh)
            # A non-selected candidate is diagnostic only.  In particular, do
            # not compute a confirmatory gate or consume an online allocation.
            if not selected:
                return {"candidate_id": candidate_id,
                        "status": "not_selected", "ingested": False,
                        "boundary": boundary, "sessions": available,
                        "selection_sessions": selection_sessions,
                        "confirmatory_sessions": confirmatory_sessions,
                        "selection_raw_p": selection_p_value,
                        "family": dict(family_data),
                        "global": dict(global_data),
                        "preflight_ready": preflight_ready,
                        "preflight_checks": preflight_checks,
                        "window_checks": window_checks,
                        "capacity_status": {
                            name: checks.get("capacity_status")
                            for name, checks in window_checks.items()},
                        "independent_confirmatory": True,
                        "online_allocation_spent": False}

            # The selected candidate gets one and only one new gate on the
            # disjoint newer half.  LORD receives this confirmatory raw p;
            # selection p and BH q-values never enter the online ledger.
            gate = _discover_gate(
                confirmatory_rows, confirmatory_baseline_rows, vehicle=vehicle,
                min_trades=self.config.min_trades,
                min_sessions=self.config.min_sessions,
                alpha=float(self.config.alpha), shadow=True,
                null_rows=confirmatory_null_rows, qualification=qualification,
                test_iterations=test_iterations, equity_feed=equity_feed)
            gate = _strengthen_gate(
                gate, confirmatory_baseline_rows, vehicle=vehicle,
                equity_feed=equity_feed)
            p_value = float(gate.get("candidate_p_raw", 1.0))
            source["p_value_source"] = CONFIRMATORY_P_VALUE_SOURCE
            source["confirmatory"]["raw_p_value"] = p_value
            confirmatory_ready, confirmatory_checks = _preflight_ready(gate)
            if not confirmatory_ready:
                return {"candidate_id": candidate_id,
                        "status": "underpowered_confirmatory_gate",
                        "reason": "confirmatory gate failed structural preflight",
                        "ingested": False, "boundary": boundary,
                        "sessions": available,
                        "selection_sessions": selection_sessions,
                        "confirmatory_sessions": confirmatory_sessions,
                        "selection_raw_p": selection_p_value,
                        "confirmatory_raw_p": p_value,
                        "preflight_ready": False,
                        "preflight_checks": confirmatory_checks,
                        "online_allocation_spent": False}
            test_id = _confirmatory_test_id(candidate_id, source)
            source["confirmatory"]["test_id"] = test_id
            online = FactoryLedger(self.config.edge_db).record_fdr_decision(
                _confirmatory_scope(vehicle),
                test_id, p_value,
                alpha=float(self.config.alpha))
            online = {**online, "required": True, "tested": True,
                      "raw_p_value": p_value,
                      "p_value_source": CONFIRMATORY_P_VALUE_SOURCE,
                      "family_q_value": family_q,
                      "global_q_value": q_value,
                      "selection_raw_p_value": selection_p_value,
                      "confirmatory_raw_p_value": p_value,
                      "selection_sessions": selection_sessions,
                      "confirmatory_sessions": confirmatory_sessions,
                      "selection_session_digest": selection_session_digest,
                      "confirmatory_session_digest": confirmatory_session_digest,
                      "independent_confirmatory": True,
                      "disjoint_sessions": True,
                      "session_disjoint": True,
                      "test_iterations": int(test_iterations),
                      "minimum_raw_p": 1.0 / (int(test_iterations) + 1)}
            # FactoryLedger returns storage-only identity/timestamp columns
            # when this test id already exists.  They are not part of the
            # immutable gate identity and would make a crash retry's envelope
            # differ from the run written by the first attempt.
            online.pop("decision_id", None)
            online.pop("created_at", None)
            family = {"p": selection_p_value,
                      "p_adjusted": family_q,
                      "significant": bool(family_data.get("significant")),
                      "family_size": int(family_data.get("family_size", 1))}
            run_provenance = {
                "schema": INGEST_SCHEMA, "candidate_id": candidate_id,
                "vehicle": vehicle, "boundary": boundary,
                "session_start": available[0], "session_end": available[-1],
                "session_window": available, "source": source,
                "selection_sessions": selection_sessions,
                "confirmatory_sessions": confirmatory_sessions,
                "selection_session_digest": selection_session_digest,
                "confirmatory_session_digest": confirmatory_session_digest,
                "independent_confirmatory": True,
                "disjoint_sessions": True,
                "session_disjoint": True,
                "p_value_source": CONFIRMATORY_P_VALUE_SOURCE,
                "selection_raw_p_value": selection_p_value,
                "confirmatory_raw_p_value": p_value,
                "replay_digests": [item["replay_digest"] for item in source["sessions"]],
                "candidate_proof": {key: candidate.get(key) for key in (
                    "candidate_id", "dataset_hash", "config_hash",
                    "code_hash", "provenance_hash")},
                "prior_run_id": previous_run.get("run_id"),
                "prior_gate_hash": previous_gate.get("content_hash"),
                "baseline_candidate_id": baseline_id,
                "null_candidate_id": null_id,
                "multiple_tests": {"family": dict(family_data),
                                    "global": dict(global_data),
                                    "selected": selected},
            }
            payload = {"source": source, "baseline_candidate_id": baseline_id,
                       "null_candidate_id": null_id,
                       "run_provenance": run_provenance}
            hashes = provenance_hash(
                dataset=source, config=_config(candidate), code=Path(__file__),
                provenance=run_provenance)
            hashes.update({
                "independent_confirmatory": True,
                "disjoint_sessions": True,
                "session_disjoint": True,
                "selection_sessions": selection_sessions,
                "confirmatory_sessions": confirmatory_sessions,
                "selection_session_digest": selection_session_digest,
                "confirmatory_session_digest": confirmatory_session_digest,
                "p_value_source": CONFIRMATORY_P_VALUE_SOURCE,
                "selection_raw_p_value": selection_p_value,
                "confirmatory_raw_p_value": p_value,
            })
            _finalize_gate(
                gate, lane="shadow", family=family, global_fdr=global_data,
                online_fdr=online,
                provenance=hashes, candidate_id=candidate_id,
                equity_feed=equity_feed)
            envelope = gate.get("verified_gate")
            if not isinstance(envelope, Mapping) or not envelope.get("passes") \
                    or not verify_gate_envelope(envelope):
                return {"candidate_id": candidate_id, "status": "gate_failed",
                        "ingested": False, "boundary": boundary,
                        "preflight_ready": preflight_ready,
                        "preflight_checks": preflight_checks,
                        "gate": envelope}
        except (TypeError, ValueError, OverflowError, ShadowError) as exc:
            return {"candidate_id": candidate_id, "status": "gate_error",
                    "reason": str(exc), "ingested": False, "boundary": boundary}

        replay_digests = tuple(str(item.get("replay_digest")) for item in source["sessions"])
        # Include the replay generation in the idempotency key.  A corrected
        # engine must be able to re-prove the same candidate/tail beside its
        # older evidence instead of colliding with the legacy run.
        run_id = "shadow-" + _digest({"candidate_id": candidate_id,
                                       "vehicle": vehicle,
                                       "replay_engine_epoch": int(REPLAY_ENGINE_EPOCH),
                                       "replay_digests": replay_digests})
        hashes = provenance_hash(
            dataset=source, config=_config(candidate), code=Path(__file__),
            provenance=run_provenance)
        metrics = {"gate": gate, "shadow_source": source,
                   "replay_digests": list(replay_digests),
                   "selection_sessions": selection_sessions,
                   "confirmatory_sessions": confirmatory_sessions,
                   "independent_confirmatory": True,
                   "disjoint_sessions": True,
                   "session_disjoint": True,
                   "prior_run_id": previous_run.get("run_id")}
        existing = self.ledger.run(run_id)
        created = False
        if existing is None:
            try:
                # The authorizing run contains only the disjoint confirmatory
                # window, matching the verified gate envelope.  Its heldout_end
                # is still the newest session in the complete consumed tail.
                existing = self.ledger.append_run(
                    candidate_id, lane="shadow", vehicle=vehicle,
                    dataset=source, config=_config(candidate), code=Path(__file__),
                    provenance=run_provenance, fit=[], heldout=confirmatory_rows,
                    metrics=metrics, run_id=run_id)
                created = True
            except sqlite3.IntegrityError:
                # A concurrent ingester may have won the deterministic insert;
                # reconcile its run below rather than reporting a false no-op.
                existing = self.ledger.run(run_id)
                if existing is None:
                    raise
        run = existing
        conflict = self._existing_run_error(
            run, candidate_id=candidate_id, vehicle=vehicle, source=source,
            replay_digests=replay_digests, gate=gate, hashes=hashes)
        if conflict:
            return {"candidate_id": candidate_id, "status": "conflicting_partial_state",
                    "reason": conflict, "ingested": False, "run_id": run_id,
                    "sessions": available, "boundary": boundary}
        evidence = {
            "schema": INGEST_SCHEMA, "candidate_id": candidate_id,
            "vehicle": vehicle, "source": source,
            "replay_digests": list(replay_digests),
            "session_window": available, "boundary_before": boundary,
            "selection_sessions": selection_sessions,
            "confirmatory_sessions": confirmatory_sessions,
            "selection_session_digest": selection_session_digest,
            "confirmatory_session_digest": confirmatory_session_digest,
            "independent_confirmatory": True,
            "disjoint_sessions": True,
            "session_disjoint": True,
            "p_value_source": CONFIRMATORY_P_VALUE_SOURCE,
            "run_provenance": run_provenance,
            "prior_run_id": previous_run.get("run_id"),
            "prior_gate_hash": previous_gate.get("content_hash"),
            "candidate_proof": payload["run_provenance"]["candidate_proof"],
            "gate_hash": envelope["content_hash"],
        }
        reconciled, conflict = self._reconcile_run(
            candidate_id=candidate_id, vehicle=vehicle, run_id=run_id,
            run=run, confirmatory_rows=confirmatory_rows, gate=gate,
            marker=evidence)
        if conflict:
            return {"candidate_id": candidate_id, "status": "conflicting_partial_state",
                    "reason": conflict, "ingested": False, "run_id": run_id,
                    "sessions": available, "boundary": boundary}
        # The candidate status may have advanced before a retry reached this
        # point.  Always re-read it and perform only the missing transitions.
        current = self.ledger.candidate(candidate_id) or {}
        status = str(current.get("status"))
        transitions: list[str] = []
        if status in {"backtest_passed", "demoted"}:
            try:
                self.ledger.transition(candidate_id, "shadow",
                                       reason="complete parity-matched live shadow ingestion",
                                       actor="shadow_ingest",
                                       payload={"run_id": run_id, "source": source})
                transitions.append("shadow")
                status = "shadow"
            except ValueError:
                status = str((self.ledger.candidate(candidate_id) or {}).get("status"))
                if status != "shadow":
                    return {"candidate_id": candidate_id, "status": "conflicting_partial_state",
                            "reason": "shadow lifecycle transition conflicts with retry",
                            "ingested": False, "run_id": run_id,
                            "sessions": available, "boundary": boundary}
        if status == "shadow":
            try:
                self.ledger.transition(candidate_id, "validated",
                                       reason="live shadow verified gate passed",
                                       actor="shadow_ingest",
                                       payload={"run_id": run_id, "source": source})
                transitions.append("validated")
                status = "validated"
            except ValueError:
                status = str((self.ledger.candidate(candidate_id) or {}).get("status"))
                if status != "validated":
                    return {"candidate_id": candidate_id, "status": "conflicting_partial_state",
                            "reason": "validated lifecycle transition conflicts with retry",
                            "ingested": False, "run_id": run_id,
                            "sessions": available, "boundary": boundary}
        if status not in {"validated", "champion"}:
            return {"candidate_id": candidate_id, "status": "conflicting_partial_state",
                    "reason": f"shadow run is complete but candidate status is {status!r}",
                    "ingested": False, "run_id": run_id,
                    "sessions": available, "boundary": boundary}
        # Rule-factory slots are reseeded only after this real-time proof, not
        # after an offline forward replay.  The hypothesis id is immutable
        # candidate lineage (axes_json), so this cannot mark another slot.
        if "validated" in transitions and str(current.get("strategy_id")) == "rule":
            axes = current.get("axes")
            if axes is None and isinstance(current.get("axes_json"), str):
                try:
                    axes = json.loads(str(current["axes_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    axes = None
            hypothesis_id = axes.get("hypothesis_id") if isinstance(axes, Mapping) else None
            if hypothesis_id:
                FactoryLedger(self.config.edge_db).event(
                    str(hypothesis_id), "validated",
                    "parity-matched live shadow ingestion validated candidate",
                    {"candidate_id": candidate_id, "run_id": run_id,
                     "source": source})
        complete_status = "ingested" if (created or reconciled or transitions) else "already_ingested"
        return {"candidate_id": candidate_id, "status": complete_status,
                "ingested": complete_status == "ingested",
                "run_id": run_id, "sessions": available, "boundary": boundary,
                "selection_sessions": selection_sessions,
                "confirmatory_sessions": confirmatory_sessions,
                "preflight_ready": preflight_ready,
                "preflight_checks": preflight_checks,
                "transitions": transitions, "gate_hash": envelope["content_hash"],
                "source": source,
                "capacity": source.get("capacity", {}),
                "capacity_status": {
                    name: checks.get("capacity_status")
                    for name, checks in window_checks.items()},
                }

    def ingest(self) -> dict[str, Any]:
        candidate_ids = self._candidate_ids()
        candidate_records = [self.ledger.candidate(candidate_id)
                             for candidate_id in candidate_ids]
        vehicles = sorted({str(row.get("vehicle")) for row in candidate_records
                           if isinstance(row, Mapping) and row.get("vehicle") in VEHICLES})
        allocation_previews = {
            vehicle: FactoryLedger(self.config.edge_db).next_fdr_allocation(
                _confirmatory_scope(vehicle), alpha=float(self.config.alpha))
            for vehicle in vehicles
        }
        smallest_allocation = min(
            (float(item["allocated_alpha"]) for item in allocation_previews.values()),
            default=float(self.config.alpha))
        test_iterations = _confirmatory_iterations(
            smallest_allocation, max(1, len(candidate_ids)))
        if test_iterations > MAX_CONFIRMATORY_ITERATIONS:
            rows = [{"candidate_id": candidate_id,
                     "status": "confirmatory_resolution_exhausted",
                     "ingested": False, "required_iterations": test_iterations,
                     "max_iterations": MAX_CONFIRMATORY_ITERATIONS}
                    for candidate_id in candidate_ids]
            return {"schema": INGEST_SCHEMA, "candidates": rows, "ingested": 0,
                    "no_op": len(rows), "confirmatory": {
                        "scope_version": CONFIRMATORY_SCOPE_VERSION,
                        "allocation_previews": allocation_previews,
                        "required_iterations": test_iterations,
                        "resolution_exhausted": True}}
        prepared = [self._one(candidate_id, dry=True,
                              test_iterations=test_iterations)
                    for candidate_id in candidate_ids]
        eligible = [row for row in prepared if row.get("status") == "prepared"]
        # Correct the current batch twice: first within each independent rule
        # family, then globally across every family/vehicle tested this cycle.
        by_family: dict[str, dict[str, float]] = {}
        for row in eligible:
            by_family.setdefault(str(row["family"]), {})[str(row["candidate_id"])] = float(row["raw_p"])
        family_results: dict[str, dict] = {}
        for family, values in by_family.items():
            corrected = benjamini_hochberg(values, alpha=float(self.config.alpha))
            family_results.update({cid: {**item, "family_size": len(values)}
                                   for cid, item in corrected.items()})
        global_values = {str(row["candidate_id"]): float(row["raw_p"]) for row in eligible}
        global_results = benjamini_hochberg(global_values, alpha=float(self.config.alpha))
        preflight_by_id = {
            str(item.get("candidate_id")): bool(item.get("preflight_ready"))
            for item in prepared
        }
        selectable = [
            cid for cid in global_results
            if preflight_by_id.get(str(cid), False)
            and bool(global_results[cid].get("significant"))
            and bool(family_results.get(cid, {}).get("significant"))
        ]
        selected_id = (min(selectable,
                           key=lambda cid: (float(global_results[cid].get("p_adjusted", 1.0)), cid))
                       if selectable else None)
        rows: list[dict[str, Any]] = []
        for preflight in prepared:
            cid = str(preflight["candidate_id"])
            if preflight.get("status") != "prepared":
                rows.append(preflight)
                continue
            rows.append(self._one(cid, correction={
                "family": family_results.get(cid, {}),
                "global": global_results.get(cid, {}),
                "selected": cid == selected_id,
                "bh": {
                    "family_values": by_family,
                    "family_results": family_results,
                    "global_values": global_values,
                    "global_results": global_results,
                },
            }, test_iterations=test_iterations))
        return {"schema": INGEST_SCHEMA, "candidates": rows,
                "ingested": sum(1 for row in rows if row.get("ingested")),
                "no_op": sum(1 for row in rows if not row.get("ingested")),
                "confirmatory": {
                    "scope_version": CONFIRMATORY_SCOPE_VERSION,
                    "allocation_previews": allocation_previews,
                    "test_iterations": test_iterations,
                    "resolution_exhausted": False}}


def ingest_shadow(config: ShadowIngestConfig) -> dict[str, Any]:
    """Idempotently ingest complete parity-matched shadow sessions."""
    return ShadowIngestor(config).ingest()


__all__ = ["INGEST_SCHEMA", "MAX_SELECTION_SOURCE_ROWS",
           "ShadowIngestConfig", "ShadowIngestor", "ingest_shadow"]
