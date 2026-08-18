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
from typing import Any, Mapping, Sequence

from .edge_discovery_core import _discover_gate, _finalize_gate
from .edge_lab import _strengthen_gate
from .edge_ledger import EdgeLedger, VEHICLES, provenance_hash
from .gates import sample_counts, verify_gate_envelope, validate_protocol_floor
from .live_shadow import ShadowError, ShadowStore
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


def _window_counts(rows: Sequence[Mapping[str, Any]], vehicle: str) -> dict[str, int]:
    counts = sample_counts(rows, vehicle=vehicle)
    return {key: int(counts.get(key, 0)) for key in
            ("trades", "sessions", "clusters")}


def _window_ready(rows: Sequence[Mapping[str, Any]], *, vehicle: str,
                  min_trades: int, min_sessions: int) -> tuple[bool, dict[str, int]]:
    counts = _window_counts(rows, vehicle)
    return bool(counts["trades"] >= int(min_trades) and
                counts["sessions"] >= int(min_sessions)), counts


def _latest_boundary(ledger: EdgeLedger, candidate_id: str) -> str | None:
    """Return the greatest consumed session in any persisted run."""
    values: list[str] = []
    for run in ledger.runs(candidate_id):
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
        prior = _latest_gate(self.ledger, candidate_id)
        boundary = _latest_boundary(self.ledger, candidate_id)
        if prior is None or boundary is None:
            return {"candidate_id": candidate_id, "status": "no_prior_proof",
                    "ingested": False, "boundary": boundary}
        metadata = self.store.replay_metadata(candidate_id)
        available = sorted({_session(row.get("session_date")) for row in metadata
                            if _session(row.get("session_date")) > boundary})
        if not available:
            return {"candidate_id": candidate_id, "status": "no_new_session",
                    "ingested": False, "boundary": boundary}
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

        baseline_id = self._paired_id(candidate, self.config.baseline_candidate_id)
        null_id = self._paired_id(candidate, self.config.null_candidate_id, null=True)
        if not baseline_id or not null_id:
            return {"candidate_id": candidate_id, "status": "control_unavailable",
                    "ingested": False, "boundary": boundary,
                    "baseline_candidate_id": baseline_id,
                    "null_candidate_id": null_id}
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
                    min_sessions=self.config.min_sessions)
                arm_checks[arm_name] = {"ready": ready, "counts": counts}
            arm_checks["ready"] = all(
                bool(value.get("ready")) for key, value in arm_checks.items()
                if key != "ready")
            window_checks[name] = arm_checks
        if not all(bool(item.get("ready")) for item in window_checks.values()):
            return {
                "candidate_id": candidate_id,
                "status": "underpowered_confirmatory_window",
                "reason": "selection and confirmatory windows must each meet structural minimums",
                "ingested": False,
                "boundary": boundary,
                "sessions": available,
                "selection_sessions": selection_sessions,
                "confirmatory_sessions": confirmatory_sessions,
                "window_checks": window_checks,
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
                test_iterations=test_iterations)
            selection_gate = _strengthen_gate(
                selection_gate, selection_baseline_rows, vehicle=vehicle)
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
                test_iterations=test_iterations)
            gate = _strengthen_gate(gate, confirmatory_baseline_rows, vehicle=vehicle)
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
                provenance=hashes, candidate_id=candidate_id)
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
        run_id = "shadow-" + _digest({"candidate_id": candidate_id,
                                       "vehicle": vehicle,
                                       "replay_digests": replay_digests})
        existing = self.ledger.run(run_id)
        if existing is not None:
            return {"candidate_id": candidate_id, "status": "already_ingested",
                    "ingested": False, "run_id": run_id,
                    "sessions": available, "boundary": boundary}
        run = self.ledger.append_run(
            candidate_id, lane="shadow", vehicle=vehicle,
            dataset=source, config=_config(candidate), code=Path(__file__),
            # The authorizing run contains only the disjoint confirmatory
            # window, matching the verified gate envelope.  Its heldout_end is
            # still the newest session in the complete consumed tail.
            provenance=run_provenance, fit=[], heldout=confirmatory_rows,
            metrics={"gate": gate, "shadow_source": source,
                     "replay_digests": list(replay_digests),
                     "selection_sessions": selection_sessions,
                     "confirmatory_sessions": confirmatory_sessions,
                     "independent_confirmatory": True,
                     "disjoint_sessions": True,
                     "session_disjoint": True,
                     "prior_run_id": previous_run.get("run_id")}, run_id=run_id)
        for index, row in enumerate(confirmatory_rows):
            self.ledger.append_trade(
                run["run_id"], row,
                trade_id=_digest({"run_id": run_id, "index": index,
                                  "opportunity_id": row.get("opportunity_id")}))
        self.ledger.record_verified_gate(run_id, gate)
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
        self.ledger.append_evidence(candidate_id, "shadow_ingestion", evidence,
                                    run_id=run_id)
        status = str(candidate.get("status"))
        transitions: list[str] = []
        if status in {"backtest_passed", "demoted"}:
            self.ledger.transition(candidate_id, "shadow",
                                   reason="complete parity-matched live shadow ingestion",
                                   actor="shadow_ingest",
                                   payload={"run_id": run_id, "source": source})
            transitions.append("shadow")
            status = "shadow"
        if status == "shadow":
            self.ledger.transition(candidate_id, "validated",
                                   reason="live shadow verified gate passed",
                                   actor="shadow_ingest",
                                   payload={"run_id": run_id, "source": source})
            transitions.append("validated")
            status = "validated"
        # Rule-factory slots are reseeded only after this real-time proof, not
        # after an offline forward replay.  The hypothesis id is immutable
        # candidate lineage (axes_json), so this cannot mark another slot.
        if "validated" in transitions and str(candidate.get("strategy_id")) == "rule":
            axes = candidate.get("axes")
            if axes is None and isinstance(candidate.get("axes_json"), str):
                try:
                    axes = json.loads(str(candidate["axes_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    axes = None
            hypothesis_id = axes.get("hypothesis_id") if isinstance(axes, Mapping) else None
            if hypothesis_id:
                FactoryLedger(self.config.edge_db).event(
                    str(hypothesis_id), "validated",
                    "parity-matched live shadow ingestion validated candidate",
                    {"candidate_id": candidate_id, "run_id": run_id,
                     "source": source})
        return {"candidate_id": candidate_id, "status": "ingested", "ingested": True,
                "run_id": run_id, "sessions": available, "boundary": boundary,
                "selection_sessions": selection_sessions,
                "confirmatory_sessions": confirmatory_sessions,
                "preflight_ready": preflight_ready,
                "preflight_checks": preflight_checks,
                "transitions": transitions, "gate_hash": envelope["content_hash"],
                "source": source}

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
