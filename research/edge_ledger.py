"""Immutable storage and lifecycle authority for research edges."""

from __future__ import annotations

from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
import uuid

from .edge_ledger_store import (
    BACKTEST_PASSED, CANDIDATE, CHAMPION, DEFAULT_DB_PATH, DEMOTED, LANES,
    LIFECYCLE, PAPER_DEMOTION_MIN_OUTCOMES, PAPER_DEMOTION_R_FLOOR, RETIRED,
    SCHEMA_VERSION, SHADOW, VALIDATED, VEHICLES, _connect, _json, _row, _utc,
    canonical_json, content_hash, hash_config, hash_dataset, hash_file,
    hash_provenance, init_db, init_ledger, provenance_hash,
)
from .gates import sample_counts, verify_gate_envelope


class EdgeLedger:
    """SQLite-backed ledger; methods never mutate an experiment row."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        init_ledger(self.path)

    def register_candidate(self, variant_id: str, *, strategy_id: str = "ibr",
                           vehicle: str = "equity", base_version: str = "v1",
                           hypothesis: str, config: Mapping | None = None,
                           axes: Mapping | None = None, overrides: Mapping | None = None,
                           dataset: Any = None,
                           code: Any = None, provenance: Any = None,
                           candidate_id: str | None = None) -> dict:
        if vehicle not in VEHICLES:
            raise ValueError("vehicle must be equity or option")
        if not str(variant_id).strip() or not str(hypothesis).strip():
            raise ValueError("variant_id and hypothesis are required")
        if str(strategy_id) == "ibr":
            # Keep the laboratory bounded at its write boundary too; a
            # runtime-only arbitrary id must not become research evidence by
            # calling the ledger API directly.
            from agent.registry import validate_variant_id
            validate_variant_id("ibr", str(variant_id))
        cfg = dict(config or {})
        axis_payload = dict(axes or {})
        if overrides is not None:
            axis_payload.setdefault("overrides", dict(overrides))
        hashes = provenance_hash(dataset=dataset, config=cfg, code=code,
                                 provenance=provenance)
        candidate_id = candidate_id or uuid.uuid4().hex
        now = _utc()
        with closing(_connect(self.path)) as db, db:
            existing = db.execute("""SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                                  ON s.candidate_id=c.candidate_id
                                  WHERE c.variant_id=? AND c.vehicle=?""",
                                  (variant_id, vehicle)).fetchone()
            if existing:
                same = (existing["config_hash"] == hashes["config_hash"] and
                        existing["hypothesis"] == str(hypothesis))
                if not same:
                    raise ValueError("candidate identity is immutable; use a new variant_id")
                return dict(existing)
            db.execute("""INSERT INTO candidates
                (candidate_id,variant_id,strategy_id,vehicle,base_version,hypothesis,
                 axes_json,config_json,dataset_hash,config_hash,code_hash,provenance_hash,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, str(variant_id), str(strategy_id), vehicle,
                 str(base_version), str(hypothesis), _json(axis_payload), _json(cfg),
                 hashes["dataset_hash"], hashes["config_hash"], hashes["code_hash"],
                 hashes["provenance_hash"], now))
            db.execute("INSERT INTO candidate_state VALUES(?,?,?)",
                       (candidate_id, "candidate", now))
            db.execute("""INSERT INTO events
                (event_id,candidate_id,event_type,from_status,to_status,actor,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, candidate_id, "candidate_registered", None,
                 "candidate", "edge_lab", "pre-registered candidate", _json({}), now))
            return dict(db.execute("""SELECT c.*, s.status FROM candidates c
                JOIN candidate_state s ON s.candidate_id=c.candidate_id
                WHERE c.candidate_id=?""", (candidate_id,)).fetchone())

    def candidate(self, candidate_id: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                ON s.candidate_id=c.candidate_id WHERE c.candidate_id=?""",
                             (candidate_id,)).fetchone()
        return _row(row)

    def candidate_by_variant(self, variant_id: str, vehicle: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                ON s.candidate_id=c.candidate_id WHERE c.variant_id=? AND c.vehicle=?""",
                             (variant_id, vehicle)).fetchone()
        return _row(row)

    def append_run(self, candidate_id: str, *, lane: str, vehicle: str | None = None,
                   dataset: Any = None, config: Any = None, code: Any = None,
                   provenance: Any = None, fit: Sequence | None = None,
                   heldout: Sequence | None = None, metrics: Mapping | None = None,
                   run_id: str | None = None) -> dict:
        if lane not in LANES:
            raise ValueError("lane must be backtest or shadow")
        candidate = self.candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        vehicle = vehicle or candidate["vehicle"]
        if vehicle != candidate["vehicle"]:
            raise ValueError("run vehicle must match candidate vehicle; vehicles are never pooled")
        hashes = provenance_hash(dataset=dataset, config=config,
                                 code=code, provenance=provenance)
        fit_rows, held_rows = list(fit or ()), list(heldout or ())
        payload = dict(metrics or {})
        payload.setdefault("fit_trades", len(fit_rows))
        payload.setdefault("heldout_trades", len(held_rows))
        run_id = run_id or uuid.uuid4().hex
        def bounds(rows):
            values = [str(r.get("session_date") or r.get("entry_timestamp") or "")
                      for r in rows if isinstance(r, Mapping)]
            values = [v for v in values if v]
            return (min(values) if values else None, max(values) if values else None)
        fit_start, fit_end = bounds(fit_rows)
        held_start, held_end = bounds(held_rows)
        now = _utc()
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO runs
                (run_id,candidate_id,lane,vehicle,dataset_hash,config_hash,code_hash,
                 provenance_hash,fit_start,fit_end,heldout_start,heldout_end,metrics_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, candidate_id, lane, vehicle, hashes["dataset_hash"],
                 hashes["config_hash"], hashes["code_hash"], hashes["provenance_hash"],
                 fit_start, fit_end, held_start, held_end, _json(payload), now))
        return self.run(run_id) or {}

    def run(self, run_id: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        return result

    def append_trade(self, run_id: str, trade: Mapping, *, trade_id: str | None = None) -> dict:
        run = self.run(run_id)
        if run is None:
            raise KeyError(run_id)
        vehicle = str(trade.get("vehicle") or run["vehicle"])
        if vehicle != run["vehicle"]:
            raise ValueError("trade vehicle differs from run")
        opportunity = str(trade.get("opportunity_id") or trade.get("entry_timestamp") or uuid.uuid4().hex)
        session = str(trade.get("session_date") or "")
        if not session and trade.get("entry_timestamp"):
            session = str(trade.get("entry_timestamp"))[:10]
        if not session:
            raise ValueError("trade session_date is required")
        try:
            net = float(trade.get("net_pnl", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("trade net_pnl must be numeric") from exc
        tid = trade_id or uuid.uuid4().hex
        payload = dict(trade)
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO trades
                (trade_id,run_id,candidate_id,vehicle,session_date,opportunity_id,
                 entry_timestamp,exit_timestamp,net_pnl,return_value,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, run_id, run["candidate_id"], vehicle, session, opportunity,
                 trade.get("entry_timestamp"), trade.get("exit_timestamp"), net,
                 float(trade["return_value"]) if trade.get("return_value") is not None else None,
                 _json(payload), _utc()))
            row = db.execute("SELECT * FROM trades WHERE trade_id=?", (tid,)).fetchone()
        return _row(row) or {}

    def append_evidence(self, candidate_id: str, kind: str, payload: Any,
                        *, run_id: str | None = None) -> dict:
        if str(kind) == "verified_gate":
            raise ValueError("verified_gate evidence must be recorded through record_verified_gate")
        if self.candidate(candidate_id) is None:
            raise KeyError(candidate_id)
        if run_id is not None:
            run = self.run(run_id)
            if run is None:
                raise KeyError(run_id)
            if run["candidate_id"] != candidate_id:
                raise ValueError("evidence run belongs to another candidate")
        eid = uuid.uuid4().hex
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO evidence
                (evidence_id,candidate_id,run_id,kind,payload_json,evidence_hash,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (eid, candidate_id, run_id, str(kind), _json(payload), content_hash(payload), _utc()))
            row = db.execute("SELECT * FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
        return _row(row) or {}

    def record_verified_gate(self, run_id: str, gate: Mapping) -> dict:
        """Persist a gate only after its envelope agrees with durable run trades."""
        run = self.run(run_id)
        if run is None:
            raise KeyError(run_id)
        envelope = gate.get("verified_gate") if isinstance(gate.get("verified_gate"), Mapping) else gate
        if not isinstance(envelope, Mapping) or not verify_gate_envelope(envelope):
            raise ValueError("verified gate envelope/hash is invalid")
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
        floors = envelope.get("floors") or {}
        for name in ("fit", "heldout"):
            report = floors.get(name)
            if not isinstance(report, Mapping):
                return "verified gate floor report is missing"
            counts = actual[name]
            if any(int(report.get(key, -1)) != int(counts[key])
                   for key in ("trades", "sessions", "clusters")):
                return "verified gate floor report does not match persisted trades"
            structural = report.get("structural_checks")
            minimums = report.get("minimums")
            if (not isinstance(structural, Mapping) or
                    set(structural) != {"trades", "sessions", "clusters"} or
                    not isinstance(minimums, Mapping) or
                    set(minimums) != {"trades", "sessions", "clusters"}):
                return "verified gate structural floor checks are missing"
            try:
                expected_checks = {key: counts[key] >= int(minimums[key])
                                   for key in ("trades", "sessions", "clusters")}
            except (TypeError, ValueError):
                return "verified gate structural floor checks are invalid"
            if dict(structural) != expected_checks:
                return "verified gate structural floor checks are inconsistent"
            structural_passes = all(bool(value) for value in structural.values())
            required = bool(report.get("required", True))
            if bool(report.get("structural_passes")) != structural_passes:
                return "verified gate structural floor result is inconsistent"
            if bool(report.get("adequate")) != (structural_passes if required else True):
                return "verified gate adequacy result is inconsistent"
        checks = envelope.get("checks")
        if not isinstance(checks, Mapping) or not checks:
            return "verified gate decision checks are missing"
        if bool(envelope.get("passes")) != all(bool(value) for value in checks.values()):
            return "verified gate pass decision is inconsistent"
        statistics = envelope.get("statistics") or {}
        try:
            p_value = float(statistics["p_value"])
            q_value = float(statistics["q_value"])
            alpha = float(statistics["alpha"])
        except (KeyError, TypeError, ValueError):
            return "verified gate p/q evidence is invalid"
        if not (0.0 <= p_value <= 1.0 and 0.0 <= q_value <= 1.0 and 0.0 < alpha <= 1.0):
            return "verified gate p/q evidence is invalid"
        if "family_fdr_significant" in checks and \
                bool(checks["family_fdr_significant"]) != (q_value <= alpha):
            return "verified gate FDR decision is inconsistent"
        control = envelope.get("control") or {}
        if "actual_control_available" in checks and bool(checks["actual_control_available"]) != bool(
                control.get("actual_control") is True and control.get("available") is True):
            return "verified gate control decision is inconsistent"
        if "heldout_delta_positive" in checks:
            delta = control.get("mean_delta")
            positive = delta is not None and float(delta) > 0
            if bool(checks["heldout_delta_positive"]) != positive:
                return "verified gate control delta decision is inconsistent"
        if "heldout_p_significant" in checks and \
                bool(checks["heldout_p_significant"]) != (p_value <= alpha):
            return "verified gate raw p decision is inconsistent"
        if "falsification" in checks and bool(checks["falsification"]) != bool(
                (envelope.get("falsification") or {}).get("passes")):
            return "verified gate falsification decision is inconsistent"
        if "separated" in checks and bool(checks["separated"]) != bool(
                (envelope.get("separation") or {}).get("passes")):
            return "verified gate separation decision is inconsistent"
        if envelope.get("passes"):
            control = envelope.get("control") or {}
            if not (control.get("actual_control") is True and control.get("available") is True and
                    int(control.get("matched", 0)) > 0):
                return "passing verified gate lacks an actual matched control"
            if not bool((envelope.get("falsification") or {}).get("passes")):
                return "passing verified gate lacks a passing falsification"
            if not bool((envelope.get("separation") or {}).get("passes")):
                return "passing verified gate lacks fit/heldout separation"
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

    def append_event(self, *, candidate_id: str | None, event_type: str,
                     reason: str, actor: str = "edge_lab",
                     from_status: str | None = None, to_status: str | None = None,
                     payload: Mapping | None = None) -> dict:
        """Append an auditable non-lifecycle event (for operators/integrations)."""
        if not str(reason).strip():
            raise ValueError("event reason is required")
        if candidate_id is not None and self.candidate(candidate_id) is None:
            raise KeyError(candidate_id)
        event_id = uuid.uuid4().hex
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO events
                (event_id,candidate_id,event_type,from_status,to_status,actor,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (event_id, candidate_id, str(event_type), from_status, to_status,
                 str(actor), str(reason), _json(payload), _utc()))
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return _row(row) or {}

    def transition(self, candidate_id: str, to_status: str, *, reason: str,
                   actor: str = "edge_lab", rollback: bool = False,
                   payload: Mapping | None = None) -> dict:
        if not str(reason).strip():
            raise ValueError("transition reason is required")
        if to_status not in LIFECYCLE:
            raise ValueError(f"unknown lifecycle status: {to_status}")
        current = self.candidate(candidate_id)
        if current is None:
            raise KeyError(candidate_id)
        from_status = current["status"]
        if rollback:
            raise ValueError("rollback cannot bypass evidence; use an auditable demotion")
        allowed = {
            "candidate": {"backtest_passed", "retired"},
            "backtest_passed": {"shadow", "retired"},
            "shadow": {"validated", "demoted", "retired"},
            "validated": {"champion", "demoted", "retired"},
            # A stronger champion does not invalidate this candidate's proof.
            # It returns to the validated pool and remains available to paper
            # ``all_proved`` selection.  ``demoted`` is reserved for an actual
            # safety/performance failure.
            "champion": {"validated", "demoted", "retired"},
            "demoted": {"shadow", "retired"},
            "retired": set(),
        }
        if to_status not in allowed.get(from_status, set()):
            raise ValueError(f"invalid lifecycle transition {from_status}->{to_status}")
        required_lane = {
            "backtest_passed": "backtest",
            "shadow": "shadow",
            "validated": "shadow",
            "champion": "shadow",
        }.get(to_status)
        if required_lane:
            run, gate = self._latest_verified_gate(candidate_id)
            if run["lane"] != required_lane or gate.get("passes") is not True:
                raise ValueError(
                    f"{to_status} requires latest persisted passing {required_lane} verified evidence")
            if from_status == "demoted" and to_status == "shadow":
                with closing(_connect(self.path)) as db:
                    demotion = db.execute("""SELECT created_at FROM events
                        WHERE candidate_id=? AND to_status='demoted'
                        ORDER BY created_at DESC,event_id DESC LIMIT 1""",
                        (candidate_id,)).fetchone()
                if demotion is not None and float(run["created_at"]) <= float(demotion["created_at"]):
                    raise ValueError("shadow re-entry after demotion requires a newer verified shadow run")
        if to_status == "retired":
            _run, gate = self._latest_verified_gate(candidate_id)
            floors = gate.get("floors") or {}
            structurally_adequate = all(
                isinstance(floors.get(name), Mapping) and floors[name].get("adequate") is True
                for name in ("fit", "heldout"))
            if gate.get("passes") is not False or not structurally_adequate:
                raise ValueError("retirement requires latest adequate failed verified gate evidence")
        now = _utc()
        event_type = "safety_demotion" if to_status == "demoted" else "lifecycle_transition"
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO candidate_state VALUES(?,?,?)
                       ON CONFLICT(candidate_id)
                       DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
                       (candidate_id, to_status, now))
            db.execute("""INSERT INTO events
                (event_id,candidate_id,event_type,from_status,to_status,actor,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, candidate_id, event_type, from_status, to_status,
                 str(actor), str(reason), _json(payload), now))
        return self.candidate(candidate_id) or {}

    def history(self, candidate_id: str) -> list[dict]:
        with closing(_connect(self.path)) as db:
            rows = db.execute("SELECT * FROM events WHERE candidate_id=? ORDER BY created_at,event_id",
                              (candidate_id,)).fetchall()
        return [dict(row) for row in rows]

    def status(self, *, vehicle: str | None = None) -> list[dict]:
        query = """SELECT c.*, s.status FROM candidates c JOIN candidate_state s
                   ON c.candidate_id=s.candidate_id"""
        params: tuple = ()
        if vehicle:
            if vehicle not in VEHICLES:
                raise ValueError("vehicle must be equity or option")
            query += " WHERE c.vehicle=?"; params = (vehicle,)
        query += " ORDER BY c.created_at,c.candidate_id"
        with closing(_connect(self.path)) as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def trades(self, candidate_id: str, *, lane: str | None = None) -> list[dict]:
        query = """SELECT t.*, r.lane FROM trades t JOIN runs r ON r.run_id=t.run_id
                   WHERE t.candidate_id=?"""
        params: list = [candidate_id]
        if lane:
            query += " AND r.lane=?"; params.append(lane)
        query += " ORDER BY t.session_date,t.entry_timestamp,t.trade_id"
        with closing(_connect(self.path)) as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def runs(self, candidate_id: str, *, lane: str | None = None) -> list[dict]:
        rows = self._runs(candidate_id, lane=lane)
        result = []
        for row in rows:
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json"))
            result.append(item)
        return result

    def evidence(self, candidate_id: str) -> list[dict]:
        with closing(_connect(self.path)) as db:
            rows = db.execute("SELECT * FROM evidence WHERE candidate_id=? ORDER BY created_at,evidence_id",
                              (candidate_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

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

    def select_champion(self, *, vehicle: str, min_confidence: float = .95,
                        strategy_id: str | None = None) -> dict | None:
        """Select one conservative validated candidate for a vehicle only."""
        if vehicle not in VEHICLES:
            raise ValueError("vehicle must be equity or option")
        rows = self.status(vehicle=vehicle)
        eligible = [r for r in rows if r["status"] in {"validated", "champion"}
                    and (strategy_id is None or r["strategy_id"] == strategy_id)]
        scored = []
        for candidate in eligible:
            try:
                run, gate = self._latest_verified_gate(candidate["candidate_id"])
            except ValueError:
                continue
            if run["lane"] != "shadow" or gate.get("passes") is not True:
                continue
            # Conservative ranking: held-out lower confidence bound first,
            # then drawdown and sample size.  No metric can cross vehicles.
            statistics = gate.get("statistics") or {}
            performance = gate.get("performance") or {}
            held_counts = ((gate.get("counts") or {}).get("heldout") or {})
            confidence = 1.0 - float(statistics.get("q_value", 1.0) or 1.0)
            if confidence < min_confidence:
                continue
            scored.append((float(performance.get("heldout_delta", -float("inf"))),
                           -float(performance.get("max_drawdown", float("inf"))),
                           int(held_counts.get("trades", 0)), candidate))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        selected = scored[0][3]
        if selected["status"] != "champion":
            self.transition(selected["candidate_id"], "champion", reason="conservative evidence selection")
            selected = self.candidate(selected["candidate_id"]) or selected
        # Retain every still-proved edge.  Selection names one conservative
        # champion, but paper mode may continue evaluating all validated
        # candidates under the shared risk limits.
        for other in rows:
            if other["candidate_id"] != selected["candidate_id"] and other["status"] == "champion":
                self.transition(other["candidate_id"], "validated",
                                reason="superseded as champion; proof remains valid")
        return selected

    def ingest_paper_outcome(self, candidate_id: str, outcome: Mapping) -> dict:
        candidate = self.candidate(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        vehicle = str(outcome.get("vehicle") or candidate["vehicle"])
        if vehicle != candidate["vehicle"]:
            raise ValueError("paper outcome vehicle differs from candidate")
        opportunity = str(outcome.get("opportunity_id") or outcome.get("entry_timestamp") or uuid.uuid4().hex)
        try:
            net = float(outcome["net_pnl"])
            risk_usd = float(outcome["risk_usd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("paper outcome requires finite net_pnl and positive risk_usd") from exc
        if not (math.isfinite(net) and math.isfinite(risk_usd) and risk_usd > 0):
            raise ValueError("paper outcome requires finite net_pnl and positive risk_usd")
        normalized = {**dict(outcome), "r_multiple": net / risk_usd,
                      "net_pnl": net, "risk_usd": risk_usd}
        oid = uuid.uuid4().hex
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO paper_outcomes
                (outcome_id,candidate_id,vehicle,opportunity_id,session_date,net_pnl,outcome_json,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (oid, candidate_id, vehicle, opportunity, outcome.get("session_date"),
                 net, _json(normalized), _utc()))
            db.execute("""INSERT INTO events
                (event_id,candidate_id,event_type,from_status,to_status,actor,reason,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, candidate_id, "paper_outcome", candidate["status"],
                 candidate["status"], "paper", "paper outcome ingested", _json(normalized), _utc()))
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT outcome_json FROM paper_outcomes WHERE candidate_id=? "
                "ORDER BY created_at,outcome_id", (candidate_id,)).fetchall()
        r_values = []
        for row in rows:
            try:
                value = json.loads(row["outcome_json"]).get("r_multiple")
                number = float(value)
                if number == number and abs(number) != float("inf"):
                    r_values.append(number)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        recent_r = r_values[-PAPER_DEMOTION_MIN_OUTCOMES:]
        automatic_guard = (
            len(recent_r) >= PAPER_DEMOTION_MIN_OUTCOMES and
            sum(recent_r) <= PAPER_DEMOTION_R_FLOOR)
        if candidate["status"] == "champion" and automatic_guard:
            self.transition(
                candidate_id, "demoted",
                reason="paper outcomes failed the registered rolling R guard",
                payload={"outcomes": len(recent_r), "rolling_r": sum(recent_r)})
        return {"outcome_id": oid, "candidate_id": candidate_id,
                "status": (self.candidate(candidate_id) or {}).get("status"),
                "rolling_outcomes": len(recent_r),
                "rolling_r": sum(recent_r) if recent_r else None}

    def _runs(self, candidate_id: str, *, lane: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM runs WHERE candidate_id=?"; params: list = [candidate_id]
        if lane: query += " AND lane=?"; params.append(lane)
        query += " ORDER BY created_at,run_id"
        with closing(_connect(self.path)) as db:
            return db.execute(query, params).fetchall()


__all__ = [
    "BACKTEST_PASSED", "CANDIDATE", "CHAMPION", "DEFAULT_DB_PATH",
    "DEMOTED", "EdgeLedger", "LANES", "LIFECYCLE",
    "PAPER_DEMOTION_MIN_OUTCOMES", "PAPER_DEMOTION_R_FLOOR", "RETIRED",
    "SCHEMA_VERSION", "SHADOW", "VALIDATED", "VEHICLES", "canonical_json",
    "content_hash", "hash_config", "hash_dataset", "hash_file",
    "hash_provenance", "init_db", "init_ledger", "provenance_hash",
]
