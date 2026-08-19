"""Durable proof and eligibility behavior for EdgeLedger."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import json
import sqlite3
from typing import Mapping
import uuid


def run_engine_epoch(run: Mapping) -> int:
    """The replay generation a persisted run was measured under.

    A run written before the stamp existed reports epoch 1, which is exactly
    what it is: evidence from the engine that preceded point-in-time option
    entry pricing, bounded quote ages, and post-selection qualification.
    """
    if not isinstance(run, Mapping):
        return 1
    metrics = run.get("metrics")
    value = metrics.get("replay_engine_epoch") if isinstance(metrics, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 1
    return int(value)


def run_engine_epoch_current(run: Mapping) -> bool:
    """Whether a run may authorize deployment under the current engine.

    Older evidence is quarantined, never rewritten.  The rows stay auditable
    and the lifecycle history is intact; the run simply cannot promote a
    candidate, so a re-derivation under the current engine is the only route
    back to ``validated``.
    """
    from .edge_ledger_store import REPLAY_ENGINE_EPOCH
    # Evidence from a future engine is just as unverifiable as evidence from
    # an old engine: its semantics may have changed in ways this verifier does
    # not understand yet.  Keep it readable for audit, but require an exact
    # epoch match before authorization.
    return run_engine_epoch(run) == int(REPLAY_ENGINE_EPOCH)


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


def recompute_gate_statistics(envelope):
    from .gates import recompute_gate_statistics as _recompute
    return _recompute(envelope)


def _close(left, right, *, tolerance: float = 1e-9) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return abs(float(left) - float(right)) <= tolerance * max(
        1.0, abs(float(left)), abs(float(right)))


def _trade_rows_match(source, durable) -> bool:
    """Compare raw source rows with durable payloads as an unordered multiset.

    ``append_trade`` stores the caller's payload verbatim, so the source rows
    in a verified envelope must be the same rows that were persisted.  The
    only tolerated asymmetry is the optional ``r_multiple`` field: the edge
    discovery drift reference may attach that auxiliary metric while writing
    a trade after the gate source was built.  If the source carries that key,
    it remains fully bound and must match exactly.

    Grouping on canonical JSON with that one auxiliary key removed makes the
    comparison deterministic without relying on insertion order or random
    SQLite trade IDs, while counters preserve duplicate rows.
    """
    try:
        source_rows = [dict(row) for row in source]
        durable_rows = [dict(row) for row in durable]
    except (TypeError, ValueError):
        return False
    if len(source_rows) != len(durable_rows):
        return False

    source_groups = defaultdict(list)
    durable_groups = defaultdict(list)
    try:
        for row in source_rows:
            group = {key: value for key, value in row.items()
                     if key != "r_multiple"}
            source_groups[_json(group)].append((_json(row), "r_multiple" in row))
        for row in durable_rows:
            group = {key: value for key, value in row.items()
                     if key != "r_multiple"}
            durable_groups[_json(group)].append((_json(row), "r_multiple" in row))
    except (TypeError, ValueError, OverflowError):
        return False
    if set(source_groups) != set(durable_groups):
        return False

    for group, source_items in source_groups.items():
        durable_items = durable_groups[group]
        # Source rows that explicitly carry r_multiple are bound to its exact
        # value.  Rows without it may match the same durable row with the
        # optional auxiliary metric attached during persistence.
        source_exact = Counter(token for token, has_r in source_items if has_r)
        durable_exact = Counter(token for token, _ in durable_items)
        for token, count in source_exact.items():
            if durable_exact[token] < count:
                return False
            durable_exact[token] -= count
        source_without_aux = len(source_items) - sum(source_exact.values())
        durable_remaining = sum(durable_exact.values())
        if source_without_aux != durable_remaining:
            return False
    return True


def _durable_trade_columns_match(item, payload: Mapping, run: Mapping) -> bool:
    """Ensure indexed trade columns still describe their JSON payload."""
    if item["candidate_id"] != run.get("candidate_id"):
        return False
    payload_candidate = payload.get("candidate_id")
    if payload_candidate is not None and str(payload_candidate) != str(
            item["candidate_id"]):
        return False
    if str(item["vehicle"]) != str(run.get("vehicle")):
        return False

    payload_vehicle = payload.get("vehicle", run.get("vehicle"))
    if str(payload_vehicle) != str(item["vehicle"]):
        return False
    payload_session = payload.get("session_date")
    if payload_session is None and payload.get("entry_timestamp") is not None:
        payload_session = str(payload.get("entry_timestamp"))[:10]
    if payload_session is not None and str(payload_session) != str(item["session_date"]):
        return False
    payload_opportunity = payload.get("opportunity_id")
    if payload_opportunity is None:
        payload_opportunity = payload.get("entry_timestamp")
    if payload_opportunity is not None and str(payload_opportunity) != str(
            item["opportunity_id"]):
        return False
    for name in ("entry_timestamp", "exit_timestamp"):
        if payload.get(name) != item[name]:
            return False

    payload_net = _finite_number(payload.get("net_pnl", 0.0))
    durable_net = _finite_number(item["net_pnl"])
    if payload_net is None or durable_net is None or not _close(payload_net, durable_net):
        return False
    payload_return = payload.get("return_value")
    durable_return = item["return_value"]
    if payload_return is None or durable_return is None:
        return payload_return is None and durable_return is None
    payload_return = _finite_number(payload_return)
    durable_return = _finite_number(durable_return)
    return (payload_return is not None and durable_return is not None and
            _close(payload_return, durable_return))


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
        # V2 source-backed proofs must identify the immutable run candidate.
        # During the historical transition producers used the candidate's
        # stable variant id before the UUID-backed ledger record existed, so
        # accept exactly that alias as well; arbitrary/missing identities are
        # never allowed to cross candidate boundaries.
        source_backed_v2 = (
            envelope.get("schema") == "verified-research-gate.v2" and
            isinstance(envelope.get("fit_source"), list) and
            isinstance(envelope.get("heldout_source"), list))
        allowed_candidate_ids: set[str] = set()
        if source_backed_v2:
            candidate = self.candidate(run.get("candidate_id"))
            if not isinstance(candidate, Mapping):
                return "verified gate candidate record is missing"
            allowed_candidate_ids = {
                str(run.get("candidate_id")), str(candidate.get("variant_id"))}
            envelope_candidate_id = envelope.get("candidate_id")
            if (not isinstance(envelope_candidate_id, str) or
                    envelope_candidate_id not in allowed_candidate_ids):
                return "verified gate candidate identity does not match persisted run"
            qualification = envelope.get("qualification")
            if isinstance(qualification, Mapping) and "post_selection" in qualification:
                post = qualification.get("post_selection")
                if not isinstance(post, Mapping):
                    return "verified gate qualification candidate identity is invalid"
                post_candidate_id = post.get("candidate_id")
                if (post.get("preselected") is True or
                        post_candidate_id is not None):
                    if (not isinstance(post_candidate_id, str) or
                            post_candidate_id not in allowed_candidate_ids or
                            post_candidate_id != envelope_candidate_id):
                        return "verified gate qualification candidate identity does not match"
            # Factory and live-shadow runs persist the complete gate under
            # metrics.  When that immutable reference is present, it is the
            # candidate-bound proof origin and a re-signed envelope cannot be
            # transplanted by merely changing candidate_id and its hash.
            metrics = run.get("metrics")
            recorded_gate = metrics.get("gate") if isinstance(metrics, Mapping) else None
            recorded_envelope = None
            if isinstance(recorded_gate, Mapping):
                recorded_envelope = recorded_gate.get("verified_gate")
                if (not isinstance(recorded_envelope, Mapping) and
                        recorded_gate.get("schema") == "verified-research-gate.v2"):
                    recorded_envelope = recorded_gate
            if isinstance(recorded_envelope, Mapping):
                if recorded_envelope.get("content_hash") != envelope.get("content_hash"):
                    return "verified gate envelope does not match immutable run proof"
            if source_backed_v2 and run_engine_epoch_current(run):
                if (not isinstance(recorded_envelope, Mapping) or
                        recorded_envelope.get("content_hash") != envelope.get(
                            "content_hash")):
                    return "verified gate immutable run proof is missing or inconsistent"
        with closing(_connect(self.path)) as db:
            durable = db.execute(
                "SELECT candidate_id,vehicle,session_date,opportunity_id,"
                "entry_timestamp,exit_timestamp,net_pnl,return_value,payload_json "
                "FROM trades WHERE run_id=? ORDER BY session_date,trade_id",
                (run["run_id"],)).fetchall()
        rows = []
        for item in durable:
            try:
                payload = json.loads(item["payload_json"])
            except (TypeError, json.JSONDecodeError):
                return "persisted trade payload is invalid"
            if not isinstance(payload, Mapping):
                return "persisted trade payload is invalid"
            if not _durable_trade_columns_match(item, payload, run):
                return "persisted trade scalar columns do not match payload"
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
        # Source-bind every critical conclusion to the exact durable run.
        source_fit = envelope.get("fit_source")
        source_held = envelope.get("heldout_source")
        if (not isinstance(source_fit, list) or not isinstance(source_held, list) or
                sample_counts(source_fit, vehicle=run["vehicle"]) != actual["fit"] or
                sample_counts(source_held, vehicle=run["vehicle"]) != actual["heldout"]):
            return "verified gate source evidence is missing or does not match persisted trades"
        if (not _trade_rows_match(source_fit, fit_rows) or
                not _trade_rows_match(source_held, heldout_rows)):
            return "verified gate source rows do not match persisted trades"
        if run.get("lane") == "shadow":
            metrics = run.get("metrics")
            shadow_source = (metrics.get("shadow_source")
                             if isinstance(metrics, Mapping) else None)
            if shadow_source is not None:
                if (not isinstance(shadow_source, Mapping) or
                        shadow_source.get("candidate_id") != run.get("candidate_id")):
                    return "verified gate shadow source candidate identity does not match persisted run"
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
            feasibility = report.get("feasibility")
            if not isinstance(feasibility, Mapping):
                return "verified gate floor feasibility evidence is missing"
            from .gates import floor_feasibility
            recomputed_feasibility = floor_feasibility(
                fit_rows if name == "fit" else heldout_rows,
                vehicle=run["vehicle"],
                min_trades=normalized_minimums["trades"],
                min_sessions=normalized_minimums["sessions"],
                min_clusters=normalized_minimums["clusters"])
            if (feasibility.get("status") != recomputed_feasibility.get("status") or
                    bool(feasibility.get("adequate")) != bool(recomputed_feasibility.get("adequate"))):
                return "verified gate floor feasibility evidence is inconsistent"
        checks = envelope.get("checks")
        if not isinstance(checks, Mapping) or not checks:
            return "verified gate decision checks are missing"
        if not all(isinstance(value, bool) for value in checks.values()):
            return "verified gate decision checks are invalid"
        if envelope.get("passes") != all(checks.values()):
            return "verified gate pass decision is inconsistent"
        if envelope.get("passes"):
            from .gates import GATE_REQUIRED_CHECKS
            if (not GATE_REQUIRED_CHECKS.issubset(set(checks)) or
                    not all(bool(checks.get(key)) for key in GATE_REQUIRED_CHECKS)):
                return "passing verified gate is missing a required decision check"
            provenance = envelope.get("provenance")
            if (not isinstance(provenance, Mapping) or
                    any(not provenance.get(key) for key in
                        ("dataset_hash", "config_hash", "code_hash", "provenance_hash"))):
                return "passing verified gate provenance is missing"
            for key in ("dataset_hash", "config_hash", "code_hash", "provenance_hash"):
                if run.get(key) and str(provenance.get(key)) != str(run.get(key)):
                    return "passing verified gate provenance does not match persisted run"
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
        family_q_value = _finite_number(statistics.get("family_q_value"))
        if family_q_value is None or not 0.0 <= family_q_value <= 1.0:
            return "verified gate family FDR evidence is invalid"
        # Family correction and cycle-global correction authorize different
        # decisions.  Never compare the family flag with the global q (or the
        # global flag with the family q): a candidate may pass its local family
        # while correctly failing the cross-family gate.
        if "family_fdr_significant" in checks and \
                bool(checks["family_fdr_significant"]) != (family_q_value <= alpha):
            return "verified gate family FDR decision is inconsistent"
        if "global_fdr_significant" in checks and \
                bool(checks["global_fdr_significant"]) != (q_value <= alpha):
            return "verified gate global FDR decision is inconsistent"
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
        from .gates import max_drawdown_of
        if (not _close(max_drawdown, max_drawdown_of(heldout_rows)) and
                not _close(max_drawdown, max_drawdown_of(rows))):
            return "verified gate max drawdown does not match persisted trades"
        # Re-verification repeats the analysis from the persisted source rows
        # and matched deltas.  A recorded decision the evidence no longer
        # reproduces is not a proof, however well formed its hashes are.
        from .gates import performance_floor
        absolute = performance_floor(heldout_rows, vehicle=run["vehicle"])
        if "heldout_net_pnl" in performance and not _close(
                _finite_number(performance.get("heldout_net_pnl")), absolute["net_pnl"]):
            return "verified gate held-out net P&L does not match persisted trades"
        if "heldout_expectancy" in performance and not _close(
                _finite_number(performance.get("heldout_expectancy")), absolute["expectancy"]):
            return "verified gate held-out expectancy does not match persisted trades"
        for key, expected in (("heldout_net_pnl_positive", absolute["net_pnl_positive"]),
                              ("heldout_expectancy_positive", absolute["expectancy_positive"])):
            if key in checks and bool(checks[key]) != bool(expected):
                return "verified gate absolute performance decision is inconsistent"
        recomputed = recompute_gate_statistics(envelope)
        if envelope.get("passes") and not recomputed.get("available"):
            return "passing verified gate cannot be recomputed from its own observations"
        if recomputed.get("available"):
            if not _close(recomputed["mean_delta"], delta):
                return "verified gate control delta does not survive recomputation"
            if not _close(recomputed["p_value"], p_value):
                return "verified gate p-value does not survive recomputation"
            if "heldout_delta_lcb" in performance and not _close(
                    recomputed["mean_delta_lcb"],
                    _finite_number(performance.get("heldout_delta_lcb"))):
                return "verified gate lower confidence bound does not survive recomputation"
            bound = _finite_number(performance.get("heldout_delta_lcb"))
            if "heldout_delta_lcb_positive" in checks and bool(
                    checks["heldout_delta_lcb_positive"]) != bool(
                        bound is not None and bound > 0):
                return "verified gate lower-bound decision is inconsistent"
            # The null draws are reproducible from the matched deltas alone:
            # an absent seed is re-derived from the same content hash the
            # original draw used, so only the draw count must be recorded.
            seeded = (isinstance(falsification.get("draws"), int) and
                      not isinstance(falsification.get("draws"), bool) and
                      int(falsification["draws"]) > 0)
            if envelope.get("passes") and not seeded:
                return "passing verified gate lacks a reproducible falsification draw"
            if seeded and bool(falsification.get("passes")) != bool(
                    recomputed["falsification_passes"]):
                return "verified gate falsification does not survive recomputation"
            if seeded and "p_value" in falsification and not _close(
                    _finite_number(falsification.get("p_value")),
                    recomputed["falsification_p_value"]):
                return "verified gate falsification p-value does not survive recomputation"
        return None

    def _latest_verified_gate(self, candidate_id: str, *, lane: str | None = None) -> tuple[dict, dict]:
        runs = self._runs(candidate_id, lane=lane)
        if not runs:
            raise ValueError("lifecycle transition requires persisted verified gate evidence")
        return self._verified_gate_for_run(str(runs[-1]["run_id"]))

    def _verified_gate_for_run(self, run_id: str) -> tuple[dict, dict]:
        run = self.run(run_id)
        if run is None:
            raise ValueError("persisted run is missing")
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT * FROM evidence
                WHERE candidate_id=? AND run_id=? AND kind='verified_gate'
                ORDER BY created_at DESC,evidence_id DESC LIMIT 1""",
                (run["candidate_id"], run["run_id"])).fetchone()
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

    def verified_run(self, run_id: str) -> tuple[dict, dict] | None:
        """Return one run and its gate only when the durable proof re-verifies."""
        try:
            return self._verified_gate_for_run(str(run_id))
        except ValueError:
            return None

    def latest_verified_run(self, candidate_id: str, *,
                            lane: str | None = None) -> dict | None:
        """Return the latest run only when its durable gate proof re-verifies."""
        try:
            run, gate = self._latest_verified_gate(candidate_id, lane=lane)
        except ValueError:
            return None
        if lane is not None and run.get("lane") != lane:
            return None
        result = dict(run)
        result["verified_gate"] = gate
        result["gate_hash"] = gate["content_hash"]
        return result

    def eligibility(self, candidate_id: str, *, lane: str = "shadow") -> dict:
        """Explain latest-proof eligibility without trusting caller run metrics."""
        candidate = self.candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        proof = self.latest_verified_run(candidate_id, lane=lane)
        authorized = (lane != "shadow" or
                      (proof is not None and self._live_shadow_authorized(proof)))
        eligible = bool(
            candidate.get("status") in {"validated", "champion"} and
            proof is not None and proof["verified_gate"].get("passes") is True and
            authorized)
        # Name the quarantine explicitly.  "Not eligible" and "proved by an
        # engine that has since been corrected" are different operator
        # situations, and only the second one is fixed by re-deriving.
        epoch = run_engine_epoch(proof) if proof is not None else None
        return {"candidate_id": candidate_id, "status": candidate["status"],
                "lane": lane, "eligible": eligible, "latest_verified_run": proof,
                "replay_engine_epoch": epoch,
                "engine_epoch_current": (proof is not None and
                                         run_engine_epoch_current(proof)),
                "quarantined": bool(proof is not None and
                                    not run_engine_epoch_current(proof))}

    def _live_shadow_authorized(self, run: Mapping) -> bool:
        """Require the research-side parity-ingestion marker for deployment."""
        if not isinstance(run, Mapping) or run.get("lane") != "shadow":
            return False
        if not run_engine_epoch_current(run):
            return False
        metrics = run.get("metrics")
        source = metrics.get("shadow_source") if isinstance(metrics, Mapping) else None
        if (not isinstance(source, Mapping) or
                source.get("schema") != "shadow-ingest.v1" or
                not source.get("candidate_id") or not source.get("sessions")):
            return False
        # Epoch-4 live-shadow authorization has an explicit adaptive-selection
        # boundary.  Legacy v3/same-tail records remain readable, but without
        # these independently verifiable windows they cannot authorize.
        if source.get("independent_confirmatory") is not True or \
                source.get("disjoint_sessions") is not True or \
                source.get("session_disjoint") is not True:
            return False
        selection = source.get("selection")
        confirmatory = source.get("confirmatory")
        selection_sessions = source.get("selection_sessions")
        confirmatory_sessions = source.get("confirmatory_sessions")
        if (not isinstance(selection, Mapping) or
                not isinstance(confirmatory, Mapping) or
                not isinstance(selection_sessions, list) or
                not isinstance(confirmatory_sessions, list) or
                not selection_sessions or not confirmatory_sessions or
                any(not isinstance(item, str) or not item for item in
                    [*selection_sessions, *confirmatory_sessions]) or
                len(set(selection_sessions)) != len(selection_sessions) or
                len(set(confirmatory_sessions)) != len(confirmatory_sessions) or
                set(selection_sessions).intersection(confirmatory_sessions) or
                max(selection_sessions) >= min(confirmatory_sessions)):
            return False
        if (selection.get("sessions") != selection_sessions or
                confirmatory.get("sessions") != confirmatory_sessions or
                source.get("selection_session_digest") != content_hash(selection_sessions) or
                source.get("confirmatory_session_digest") != content_hash(confirmatory_sessions) or
                source.get("p_value_source") != "live_shadow_confirmatory_gate" or
                selection.get("session_digest") != content_hash(selection_sessions) or
                confirmatory.get("session_digest") != content_hash(confirmatory_sessions) or
                selection.get("p_value_source") != "selection_window_gate" or
                confirmatory.get("p_value_source") != "live_shadow_confirmatory_gate"):
            return False
        for item in (selection, confirmatory):
            if any(not isinstance(item.get(key), str) or not item.get(key)
                   for key in ("rows_digest", "baseline_rows_digest", "null_rows_digest")):
                return False
        all_sessions = [item.get("session_date") for item in source.get("sessions", ())
                        if isinstance(item, Mapping)]
        if (set(all_sessions) != set([*selection_sessions, *confirmatory_sessions]) or
                len(all_sessions) != len([*selection_sessions, *confirmatory_sessions]) or
                set(selection_sessions).intersection(confirmatory_sessions)):
            return False
        replay_digests = metrics.get("replay_digests") if isinstance(metrics, Mapping) else None
        expected_replays = [item.get("replay_digest") for item in source.get("sessions", ())
                            if isinstance(item, Mapping)]
        if not isinstance(replay_digests, list) or replay_digests != expected_replays:
            return False
        with closing(_connect(self.path)) as db:
            marker_rows = db.execute(
                """SELECT payload_json,evidence_hash FROM evidence
                   WHERE candidate_id=? AND run_id=? AND kind='shadow_ingestion'
                   ORDER BY created_at DESC,evidence_id DESC""",
                (run.get("candidate_id"), run.get("run_id"))).fetchall()
            gate_row = db.execute(
                """SELECT payload_json,evidence_hash FROM evidence
                   WHERE candidate_id=? AND run_id=? AND kind='verified_gate'
                   ORDER BY created_at DESC,evidence_id DESC LIMIT 1""",
                (run.get("candidate_id"), run.get("run_id"))).fetchone()
        # The current writer prevents duplicates atomically.  Legacy duplicate
        # markers are ambiguous authorization claims and remain audit-readable
        # but cannot authorize.
        if len(marker_rows) != 1 or gate_row is None:
            return False
        row = marker_rows[0]
        try:
            payload = json.loads(row["payload_json"])
            gate_payload = json.loads(gate_row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if (not isinstance(payload, Mapping) or
                row["evidence_hash"] != content_hash(payload) or
                not isinstance(gate_payload, Mapping) or
                gate_row["evidence_hash"] != content_hash(gate_payload)):
            return False
        candidate = self.candidate(str(run.get("candidate_id") or ""))
        candidate_proof = payload.get("candidate_proof")
        expected_candidate_proof = ({key: candidate.get(key) for key in (
            "candidate_id", "dataset_hash", "config_hash", "code_hash",
            "provenance_hash")} if isinstance(candidate, Mapping) else None)
        if (not isinstance(candidate_proof, Mapping) or
                dict(candidate_proof) != expected_candidate_proof or
                payload.get("candidate_id") != run.get("candidate_id") or
                payload.get("vehicle") != run.get("vehicle")):
            return False
        evidence_source = payload.get("source")
        gate_hash = gate_payload.get("gate_hash")
        gate = gate_payload.get("gate")
        if not isinstance(gate, Mapping) or gate_hash != gate.get("content_hash"):
            return False
        online = gate.get("online_fdr")
        if (not isinstance(online, Mapping) or
                online.get("required", True) is False or
                online.get("tested", True) is not True or
                online.get("decision") is not True or
                online.get("scope") !=
                    f"shadow-confirmation-v4:{run.get('vehicle')}"):
            return False
        # The envelope's online-FDR fields are claims, not authority.  The
        # ingester must have spent the exact immutable allocation recorded in
        # the co-located factory ledger before this run can authorize.  This
        # closes the former convention-only path where a caller could rewrite
        # ``online_fdr`` and append a marker without consuming LORD state.
        scope = f"shadow-confirmation-v4:{run.get('vehicle')}"
        test_id = online.get("test_id")
        confirmatory_test_id = confirmatory.get("test_id") if isinstance(
            confirmatory, Mapping) else None
        if (not isinstance(test_id, str) or not test_id or
                confirmatory_test_id != test_id):
            return False
        try:
            with closing(_connect(self.path)) as db:
                fdr_rows = db.execute(
                    "SELECT * FROM factory_fdr WHERE scope=? "
                    "ORDER BY created_at,decision_id", (scope,)).fetchall()
        except sqlite3.Error:
            # A ledger without a factory-FDR table cannot authorize a live
            # shadow proof; fail closed rather than leaking a schema error.
            return False
        matching = [
            (index, row) for index, row in enumerate(fdr_rows, start=1)
            if str(row["test_id"]) == test_id
        ]
        if len(matching) != 1:
            return False
        test_index, durable_fdr = matching[0]
        if (online.get("tests") != test_index or
                str(durable_fdr["scope"]) != scope or
                str(durable_fdr["test_id"]) != test_id or
                not _close(online.get("p_value"), durable_fdr["p_value"]) or
                not _close(online.get("alpha"), durable_fdr["alpha"]) or
                not _close(online.get("allocated_alpha"),
                           durable_fdr["allocated_alpha"]) or
                bool(online.get("decision")) is not bool(durable_fdr["decision"])):
            return False
        # Rebuild the adaptive selection statistic from persisted selection
        # rows.  Session/row digests are checked first, then the exact gate
        # computation and the complete batch BH inputs/results are replayed;
        # a consistently rewritten digest cannot authorize by itself.
        selection_candidate = selection.get("candidate_source")
        selection_baseline = selection.get("baseline_source")
        selection_null = selection.get("null_source")
        if (not isinstance(selection_candidate, list) or
                not isinstance(selection_baseline, list) or
                not isinstance(selection_null, list) or
                selection.get("rows_digest") != content_hash(selection_candidate) or
                selection.get("baseline_rows_digest") != content_hash(selection_baseline) or
                selection.get("null_rows_digest") != content_hash(selection_null)):
            return False
        selection_sessions_from_rows = {
            str(item.get("session_date") or "") for item in selection_candidate
            if isinstance(item, Mapping) and item.get("session_date")
        }
        if selection_sessions_from_rows != set(selection_sessions):
            return False
        for arm_rows in (selection_baseline, selection_null):
            arm_sessions = {
                str(item.get("session_date") or "") for item in arm_rows
                if isinstance(item, Mapping) and item.get("session_date")
            }
            if arm_sessions != set(selection_sessions):
                return False
        minimums = selection.get("minimums")
        if (not isinstance(minimums, Mapping) or
                any(not isinstance(minimums.get(key), int) or
                    isinstance(minimums.get(key), bool) or minimums.get(key) < 1
                    for key in ("trades", "sessions"))):
            return False
        try:
            from .edge_discovery_core import _discover_gate
            from .edge_lab import _strengthen_gate
            selection_gate = _discover_gate(
                selection_candidate, selection_baseline,
                vehicle=str(run.get("vehicle") or ""),
                min_trades=int(minimums["trades"]),
                min_sessions=int(minimums["sessions"]),
                alpha=float(selection.get("alpha")), shadow=True,
                null_rows=selection_null,
                qualification=gate.get("qualification"),
                test_iterations=int(selection.get("test_iterations", 20_000)))
            selection_gate = _strengthen_gate(
                selection_gate, selection_baseline,
                vehicle=str(run.get("vehicle") or ""))
        except (TypeError, ValueError, OverflowError, KeyError):
            return False
        if selection_gate.get("candidate_p_raw") != selection.get("raw_p_value"):
            return False
        batch = selection.get("bh")
        if not isinstance(batch, Mapping):
            return False
        try:
            from .stats import benjamini_hochberg
            family_values = batch.get("family_values")
            family_results = batch.get("family_results")
            global_values = batch.get("global_values")
            global_results = batch.get("global_results")
            if (not isinstance(family_values, Mapping) or
                    not isinstance(family_results, Mapping) or
                    not isinstance(global_values, Mapping) or
                    not isinstance(global_results, Mapping)):
                return False
            expected_family = {}
            for family_name, values in family_values.items():
                if not isinstance(values, Mapping):
                    return False
                expected_family.update(benjamini_hochberg(
                    {str(key): float(value) for key, value in values.items()},
                    alpha=float(selection.get("alpha"))))
            expected_global = benjamini_hochberg(
                {str(key): float(value) for key, value in global_values.items()},
                alpha=float(selection.get("alpha")))
            if dict(expected_family) != dict(family_results) or \
                    dict(expected_global) != dict(global_results):
                return False
        except (TypeError, ValueError, OverflowError):
            return False
        provenance = gate.get("provenance")
        if (not isinstance(provenance, Mapping) or
                provenance.get("independent_confirmatory") is not True or
                provenance.get("disjoint_sessions") is not True or
                provenance.get("session_disjoint") is not True or
                provenance.get("selection_sessions") != selection_sessions or
                provenance.get("confirmatory_sessions") != confirmatory_sessions or
                provenance.get("selection_session_digest") !=
                    content_hash(selection_sessions) or
                provenance.get("confirmatory_session_digest") !=
                    content_hash(confirmatory_sessions) or
                provenance.get("p_value_source") !=
                    "live_shadow_confirmatory_gate" or
                "selection_raw_p_value" not in provenance or
                "confirmatory_raw_p_value" not in provenance or
                provenance.get("selection_raw_p_value") !=
                    source.get("selection", {}).get("raw_p_value") or
                provenance.get("confirmatory_raw_p_value") !=
                    source.get("confirmatory", {}).get("raw_p_value")):
            return False
        if (online.get("independent_confirmatory") is not True or
                online.get("disjoint_sessions") is not True or
                online.get("session_disjoint") is not True or
                online.get("selection_sessions") != selection_sessions or
                online.get("confirmatory_sessions") != confirmatory_sessions or
                online.get("selection_session_digest") !=
                    content_hash(selection_sessions) or
                online.get("confirmatory_session_digest") !=
                    content_hash(confirmatory_sessions) or
                online.get("p_value_source") !=
                    "live_shadow_confirmatory_gate" or
                "selection_raw_p_value" not in online or
                online.get("selection_raw_p_value") !=
                    source.get("selection", {}).get("raw_p_value") or
                online.get("confirmatory_raw_p_value") !=
                    source.get("confirmatory", {}).get("raw_p_value") or
                online.get("confirmatory_raw_p_value") != online.get("p_value") or
                online.get("raw_p_value") != online.get("confirmatory_raw_p_value")):
            return False
        # The persisted confirmatory gate source is the authorizing p-value
        # source.  A selection digest alone is never sufficient to authorize.
        confirm_source = gate.get("heldout_source")
        if (not isinstance(confirm_source, list) or
                confirmatory.get("rows_digest") != content_hash(confirm_source) or
                confirmatory.get("baseline_rows_digest") != content_hash(
                    gate.get("heldout_baseline_source") or []) or
                confirmatory.get("null_rows_digest") != content_hash(
                    gate.get("null_source") or [])):
            return False
        statistics = gate.get("statistics")
        candidate_id = str(source.get("candidate_id") or "")
        family_result = family_results.get(candidate_id)
        global_result = global_results.get(candidate_id)
        if (not isinstance(statistics, Mapping) or
                statistics.get("p_value") != online.get("confirmatory_raw_p_value") or
                not isinstance(family_result, Mapping) or
                not isinstance(global_result, Mapping) or
                family_result.get("p") != online.get("selection_raw_p_value") or
                family_result.get("p_adjusted") != online.get("family_q_value") or
                global_result.get("p_adjusted") != online.get("global_q_value")):
            return False
        run_provenance = payload.get("run_provenance")
        if (not isinstance(run_provenance, Mapping) or
                run_provenance.get("candidate_proof") != candidate_proof or
                run_provenance.get("candidate_id") != run.get("candidate_id") or
                run_provenance.get("vehicle") != run.get("vehicle") or
                run_provenance.get("independent_confirmatory") is not True or
                run_provenance.get("disjoint_sessions") is not True or
                run_provenance.get("session_disjoint") is not True or
                run_provenance.get("selection_sessions") != selection_sessions or
                run_provenance.get("confirmatory_sessions") != confirmatory_sessions or
                run_provenance.get("selection_session_digest") !=
                    content_hash(selection_sessions) or
                run_provenance.get("confirmatory_session_digest") !=
                    content_hash(confirmatory_sessions) or
                run_provenance.get("p_value_source") !=
                    "live_shadow_confirmatory_gate" or
                "selection_raw_p_value" not in run_provenance or
                "confirmatory_raw_p_value" not in run_provenance or
                run_provenance.get("selection_raw_p_value") !=
                    online.get("selection_raw_p_value") or
                run_provenance.get("confirmatory_raw_p_value") !=
                    online.get("confirmatory_raw_p_value")):
            return False
        return (
            isinstance(evidence_source, Mapping)
            and dict(evidence_source) == dict(source)
            and payload.get("replay_digests") == replay_digests
            and payload.get("gate_hash") == gate_hash
            and gate_hash == gate.get("content_hash")
        )


__all__ = ["EdgeLedgerProofMixin"]
