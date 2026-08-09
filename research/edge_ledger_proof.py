"""Durable proof and eligibility behavior for EdgeLedger."""

from __future__ import annotations

from contextlib import closing
import json
from typing import Mapping
import uuid


def _facade_helper(name: str):
    """Resolve an EdgeLedger helper at call time for patch-compatible facades."""
    from . import edge_ledger
    return getattr(edge_ledger, name)


def _connect(*args, **kwargs):
    return _facade_helper("_connect")(*args, **kwargs)


def _json(*args, **kwargs):
    return _facade_helper("_json")(*args, **kwargs)


def _row(*args, **kwargs):
    return _facade_helper("_row")(*args, **kwargs)


def _utc(*args, **kwargs):
    return _facade_helper("_utc")(*args, **kwargs)


def content_hash(*args, **kwargs):
    return _facade_helper("content_hash")(*args, **kwargs)


def sample_counts(*args, **kwargs):
    return _facade_helper("sample_counts")(*args, **kwargs)


def verify_gate_envelope(*args, **kwargs):
    return _facade_helper("verify_gate_envelope")(*args, **kwargs)


def _finite_number(*args, **kwargs):
    return _facade_helper("_finite_number")(*args, **kwargs)


def _nonnegative_integer(*args, **kwargs):
    return _facade_helper("_nonnegative_integer")(*args, **kwargs)


class EdgeLedgerProofMixin:
    """Mixin containing durable gate-proof and eligibility operations."""

    def record_verified_gate(self, run_id: str, gate: Mapping) -> dict:
        """Persist a gate only after its envelope agrees with durable run trades."""
        if not isinstance(gate, Mapping):
            raise ValueError("verified gate envelope/hash is invalid")
        nested = gate.get("verified_gate")
        if "verified_gate" in gate and not isinstance(nested, Mapping):
            raise ValueError("verified gate envelope/hash is invalid")
        envelope = nested if isinstance(nested, Mapping) else gate
        if not isinstance(envelope, Mapping) or not verify_gate_envelope(envelope):
            raise ValueError("verified gate envelope/hash is invalid")
        run = self.run(run_id)
        if run is None:
            raise KeyError(run_id)
        error = self._gate_envelope_error(run, envelope)
        if error:
            raise ValueError(error)
        payload = {"run_id": run_id, "candidate_id": run["candidate_id"],
                   "lane": run["lane"], "vehicle": run["vehicle"],
                   "gate_hash": envelope["content_hash"],
                   "gate": dict(envelope)}
        eid = uuid.uuid4().hex
        with closing(_connect(self.path)) as db, db:
            existing = db.execute(
                "SELECT 1 FROM evidence WHERE run_id=? AND kind='verified_gate' LIMIT 1",
                (run_id,)).fetchone()
            if existing is not None:
                raise ValueError("run already has immutable verified gate evidence")
            db.execute("""INSERT INTO evidence
                (evidence_id,candidate_id,run_id,kind,payload_json,evidence_hash,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (eid, run["candidate_id"], run_id, "verified_gate", _json(payload),
                 content_hash(payload), _utc()))
            row = db.execute("SELECT * FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
        return _row(row) or {}

    def _gate_envelope_error(self, run: Mapping, envelope: Mapping) -> str | None:
        if envelope.get("lane") != run.get("lane") or envelope.get("vehicle") != run.get("vehicle"):
            return "verified gate lane/vehicle does not match the persisted run"
        with closing(_connect(self.path)) as db:
            durable = db.execute(
                "SELECT payload_json FROM trades WHERE run_id=? ORDER BY session_date,trade_id",
                (run["run_id"],)).fetchall()
        rows = []
        for item in durable:
            try:
                payload = json.loads(item["payload_json"])
            except (TypeError, json.JSONDecodeError):
                return "persisted trade payload is invalid"
            if not isinstance(payload, Mapping):
                return "persisted trade payload is invalid"
            rows.append(payload)
        if run["lane"] == "shadow":
            fit_rows, heldout_rows = [], rows
        else:
            fit_end = run.get("fit_end")
            held_start = run.get("heldout_start")
            if not fit_end or not held_start or str(fit_end) >= str(held_start):
                return "persisted run does not have a separated fit/heldout boundary"
            fit_rows = [row for row in rows
                        if str(row.get("session_date") or row.get("entry_timestamp") or "") <= str(fit_end)]
            heldout_rows = [row for row in rows
                            if str(row.get("session_date") or row.get("entry_timestamp") or "") >= str(held_start)]
            if len(fit_rows) + len(heldout_rows) != len(rows):
                return "persisted trades do not fit the recorded chronological boundary"
        expected = envelope.get("counts") or {}
        actual = {
            "fit": sample_counts(fit_rows, vehicle=run["vehicle"]),
            "heldout": sample_counts(heldout_rows, vehicle=run["vehicle"]),
            "total": sample_counts(rows, vehicle=run["vehicle"]),
        }
        if expected != actual:
            return "verified gate counts do not match persisted trades"
        floors = envelope.get("floors")
        if not isinstance(floors, Mapping):
            return "verified gate floor report is missing"
        for name in ("fit", "heldout"):
            report = floors.get(name)
            if not isinstance(report, Mapping):
                return "verified gate floor report is missing"
            counts = actual[name]
            reported_counts = {
                key: _nonnegative_integer(report.get(key))
                for key in ("trades", "sessions", "clusters")
            }
            if (any(value is None for value in reported_counts.values()) or
                    any(reported_counts[key] != counts[key]
                        for key in ("trades", "sessions", "clusters"))):
                return "verified gate floor report does not match persisted trades"
            structural = report.get("structural_checks")
            minimums = report.get("minimums")
            if (not isinstance(structural, Mapping) or
                    set(structural) != {"trades", "sessions", "clusters"} or
                    not all(isinstance(value, bool) for value in structural.values()) or
                    not isinstance(minimums, Mapping) or
                    set(minimums) != {"trades", "sessions", "clusters"}):
                return "verified gate structural floor checks are missing"
            normalized_minimums = {
                key: _nonnegative_integer(minimums.get(key))
                for key in ("trades", "sessions", "clusters")
            }
            if any(value is None for value in normalized_minimums.values()):
                return "verified gate structural floor checks are invalid"
            expected_checks = {
                key: counts[key] >= normalized_minimums[key]
                for key in ("trades", "sessions", "clusters")
            }
            if dict(structural) != expected_checks:
                return "verified gate structural floor checks are inconsistent"
            structural_passes = all(bool(value) for value in structural.values())
            required = report.get("required")
            if not isinstance(required, bool):
                return "verified gate structural floor result is inconsistent"
            reported_structural_passes = report.get("structural_passes")
            reported_adequate = report.get("adequate")
            if (not isinstance(reported_structural_passes, bool) or
                    not isinstance(reported_adequate, bool)):
                return "verified gate structural floor result is inconsistent"
            if reported_structural_passes != structural_passes:
                return "verified gate structural floor result is inconsistent"
            if reported_adequate != (structural_passes if required else True):
                return "verified gate adequacy result is inconsistent"
        checks = envelope.get("checks")
        if not isinstance(checks, Mapping) or not checks:
            return "verified gate decision checks are missing"
        if not all(isinstance(value, bool) for value in checks.values()):
            return "verified gate decision checks are invalid"
        if envelope.get("passes") != all(checks.values()):
            return "verified gate pass decision is inconsistent"
        statistics = envelope.get("statistics")
        if not isinstance(statistics, Mapping):
            return "verified gate p/q evidence is invalid"
        p_value = _finite_number(statistics.get("p_value"))
        q_value = _finite_number(statistics.get("q_value"))
        alpha = _finite_number(statistics.get("alpha"))
        if p_value is None or q_value is None or alpha is None:
            return "verified gate p/q evidence is invalid"
        if not (0.0 <= p_value <= 1.0 and 0.0 <= q_value <= 1.0 and 0.0 < alpha <= 1.0):
            return "verified gate p/q evidence is invalid"
        if "family_fdr_significant" in checks and \
                bool(checks["family_fdr_significant"]) != (q_value <= alpha):
            return "verified gate FDR decision is inconsistent"
        control = envelope.get("control")
        if not isinstance(control, Mapping):
            return "verified gate control decision is inconsistent"
        if (not all(key in control for key in ("actual_control", "available", "matched")) or
                not isinstance(control["actual_control"], bool) or
                not isinstance(control["available"], bool)):
            return "verified gate control decision is inconsistent"
        matched = _nonnegative_integer(control.get("matched"))
        if matched is None:
            return "verified gate control decision is inconsistent"
        for key in ("actual_control", "available"):
            if key in control and not isinstance(control[key], bool):
                return "verified gate control decision is inconsistent"
        if "actual_control_available" in checks and bool(checks["actual_control_available"]) != bool(
                control.get("actual_control") is True and control.get("available") is True):
            return "verified gate control decision is inconsistent"
        no_control_failure = (
            matched == 0 and control["available"] is False and
            envelope.get("passes") is False and
            checks.get("heldout_delta_positive") is False)
        if "mean_delta" not in control:
            return "verified gate control delta decision is inconsistent"
        delta = _finite_number(control.get("mean_delta"))
        if control.get("mean_delta") is None and not no_control_failure:
            return "verified gate control delta decision is inconsistent"
        if control.get("mean_delta") is not None and delta is None:
            return "verified gate control delta decision is inconsistent"
        if "heldout_delta_positive" in checks:
            if delta is None and not no_control_failure:
                return "verified gate control delta decision is inconsistent"
            positive = delta is not None and delta > 0
            if bool(checks["heldout_delta_positive"]) != positive:
                return "verified gate control delta decision is inconsistent"
        if "heldout_p_significant" in checks and \
                bool(checks["heldout_p_significant"]) != (p_value <= alpha):
            return "verified gate raw p decision is inconsistent"
        falsification = envelope.get("falsification")
        if (not isinstance(falsification, Mapping) or
                not isinstance(falsification.get("passes"), bool)):
            return "verified gate falsification decision is inconsistent"
        if "falsification" in checks:
            if not isinstance(falsification, Mapping):
                return "verified gate falsification decision is inconsistent"
            if bool(checks["falsification"]) != bool(falsification.get("passes")):
                return "verified gate falsification decision is inconsistent"
        separation = envelope.get("separation")
        if (not isinstance(separation, Mapping) or
                not isinstance(separation.get("passes"), bool)):
            return "verified gate separation decision is inconsistent"
        if "separated" in checks:
            if not isinstance(separation, Mapping):
                return "verified gate separation decision is inconsistent"
            if bool(checks["separated"]) != bool(separation.get("passes")):
                return "verified gate separation decision is inconsistent"
        if envelope.get("passes"):
            if not (control.get("actual_control") is True and control.get("available") is True and
                    matched is not None and matched > 0):
                return "passing verified gate lacks an actual matched control"
            if not isinstance(falsification, Mapping) or not bool(falsification.get("passes")):
                return "passing verified gate lacks a passing falsification"
            if not isinstance(separation, Mapping) or not bool(separation.get("passes")):
                return "passing verified gate lacks fit/heldout separation"
        performance = envelope.get("performance")
        if not isinstance(performance, Mapping):
            return "verified gate performance evidence is invalid"
        if not all(key in performance for key in ("heldout_delta", "max_drawdown")):
            return "verified gate performance evidence is invalid"
        heldout_delta = _finite_number(performance.get("heldout_delta"))
        max_drawdown = _finite_number(performance.get("max_drawdown"))
        if (performance.get("heldout_delta") is None and no_control_failure):
            heldout_delta = None
        elif heldout_delta is None:
            return "verified gate performance evidence is invalid"
        if (max_drawdown is None or
                max_drawdown < 0):
            return "verified gate performance evidence is invalid"
        return None

    def _latest_verified_gate(self, candidate_id: str) -> tuple[dict, dict]:
        runs = self._runs(candidate_id)
        if not runs:
            raise ValueError("lifecycle transition requires persisted verified gate evidence")
        run = dict(runs[-1])
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT * FROM evidence
                WHERE candidate_id=? AND run_id=? AND kind='verified_gate'
                ORDER BY created_at DESC,evidence_id DESC LIMIT 1""",
                (candidate_id, run["run_id"])).fetchone()
        if row is None:
            raise ValueError("latest persisted run lacks verified gate evidence")
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("latest persisted verified gate evidence is invalid") from exc
        if row["evidence_hash"] != content_hash(payload):
            raise ValueError("latest persisted verified gate evidence hash is invalid")
        envelope = payload.get("gate") if isinstance(payload, Mapping) else None
        if not isinstance(envelope, Mapping) or payload.get("gate_hash") != envelope.get("content_hash"):
            raise ValueError("latest persisted verified gate envelope/hash is invalid")
        if not verify_gate_envelope(envelope) or self._gate_envelope_error(run, envelope):
            raise ValueError("latest persisted verified gate envelope is invalid")
        return run, dict(envelope)

    def latest_verified_run(self, candidate_id: str, *,
                            lane: str | None = None) -> dict | None:
        """Return the latest run only when its durable gate proof re-verifies."""
        try:
            run, gate = self._latest_verified_gate(candidate_id)
        except ValueError:
            return None
        if lane is not None and run.get("lane") != lane:
            return None
        result = dict(run)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        result["verified_gate"] = gate
        result["gate_hash"] = gate["content_hash"]
        return result

    def eligibility(self, candidate_id: str, *, lane: str = "shadow") -> dict:
        """Explain latest-proof eligibility without trusting caller run metrics."""
        candidate = self.candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        proof = self.latest_verified_run(candidate_id, lane=lane)
        eligible = bool(
            candidate.get("status") in {"validated", "champion"} and
            proof is not None and proof["verified_gate"].get("passes") is True)
        return {"candidate_id": candidate_id, "status": candidate["status"],
                "lane": lane, "eligible": eligible, "latest_verified_run": proof}


__all__ = ["EdgeLedgerProofMixin"]
