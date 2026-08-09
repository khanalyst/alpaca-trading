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
from .edge_ledger_proof import EdgeLedgerProofMixin


def _finite_number(value: Any) -> float | None:
    """Return a finite numeric value, rejecting JSON scalar impostors."""
    if isinstance(value, (bool, bytes, bytearray, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_integer(value: Any) -> int | None:
    """Return a finite, integer-valued, non-negative number or ``None``."""
    number = _finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return None


class EdgeLedger(EdgeLedgerProofMixin):
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
        net_source = trade.get("net_pnl", 0.0)
        if isinstance(net_source, (bool, bytes, bytearray)):
            raise ValueError("trade net_pnl must be numeric")
        try:
            net = float(net_source)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("trade net_pnl must be numeric") from exc
        if not math.isfinite(net):
            raise ValueError("trade net_pnl must be finite")
        return_source = trade.get("return_value")
        if isinstance(return_source, (bool, bytes, bytearray)):
            raise ValueError("trade return_value must be numeric")
        try:
            return_value = (float(return_source)
                            if return_source is not None else None)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("trade return_value must be numeric") from exc
        if return_value is not None and not math.isfinite(return_value):
            raise ValueError("trade return_value must be finite")
        tid = trade_id or uuid.uuid4().hex
        payload = dict(trade)
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO trades
                (trade_id,run_id,candidate_id,vehicle,session_date,opportunity_id,
                 entry_timestamp,exit_timestamp,net_pnl,return_value,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, run_id, run["candidate_id"], vehicle, session, opportunity,
                 trade.get("entry_timestamp"), trade.get("exit_timestamp"), net,
                 return_value,
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
            except (TypeError, ValueError, OverflowError):
                continue
            if (not isinstance(run, Mapping) or not isinstance(gate, Mapping) or
                    run.get("lane") != "shadow" or gate.get("passes") is not True):
                continue
            # Conservative ranking: held-out lower confidence bound first,
            # then drawdown and sample size.  No metric can cross vehicles.
            statistics = gate.get("statistics")
            performance = gate.get("performance")
            counts = gate.get("counts")
            if (not isinstance(statistics, Mapping) or
                    not isinstance(performance, Mapping) or
                    not isinstance(counts, Mapping)):
                continue
            held_counts = counts.get("heldout")
            if not isinstance(held_counts, Mapping):
                continue
            q_value = _finite_number(statistics.get("q_value"))
            heldout_delta = _finite_number(performance.get("heldout_delta"))
            max_drawdown = _finite_number(performance.get("max_drawdown"))
            heldout_trades = _nonnegative_integer(held_counts.get("trades"))
            if (q_value is None or not 0.0 <= q_value <= 1.0 or
                    heldout_delta is None or max_drawdown is None or
                    max_drawdown < 0 or heldout_trades is None):
                continue
            confidence = 1.0 - q_value
            if confidence < min_confidence:
                continue
            scored.append((heldout_delta, -max_drawdown, heldout_trades, candidate))
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
        error = "paper outcome requires finite net_pnl and positive risk_usd"
        if not isinstance(outcome, Mapping):
            raise ValueError(error)
        vehicle = str(outcome.get("vehicle") or candidate["vehicle"])
        if vehicle != candidate["vehicle"]:
            raise ValueError("paper outcome vehicle differs from candidate")
        opportunity = str(outcome.get("opportunity_id") or outcome.get("entry_timestamp") or uuid.uuid4().hex)
        net_source = outcome.get("net_pnl")
        risk_source = outcome.get("risk_usd")
        if (isinstance(net_source, (bool, bytes, bytearray)) or
                isinstance(risk_source, (bool, bytes, bytearray))):
            raise ValueError(error)
        try:
            net = float(net_source)
            risk_usd = float(risk_source)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(error) from exc
        if not (math.isfinite(net) and math.isfinite(risk_usd) and risk_usd > 0):
            raise ValueError(error)
        try:
            r_multiple = net / risk_usd
        except (TypeError, ValueError, OverflowError, ZeroDivisionError) as exc:
            raise ValueError(error) from exc
        if not math.isfinite(r_multiple):
            raise ValueError(error)
        normalized = {**dict(outcome), "r_multiple": r_multiple,
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
                payload = json.loads(row["outcome_json"])
                if not isinstance(payload, Mapping):
                    continue
                value = payload.get("r_multiple")
                number = _finite_number(value)
                if number is not None:
                    r_values.append(number)
            except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
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
