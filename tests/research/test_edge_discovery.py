from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from research import edge_lab, edge_ledger, edge_ledger_store
from research.edge_lab import DiscoveryError, EdgeLedger, discover
from research.edge_ledger import (
    SCHEMA_VERSION, canonical_json, content_hash, hash_config, hash_dataset,
    hash_provenance, init_db, init_ledger,
)
from research.gates import (
    heldout_separation, structural_floor, verified_gate_envelope,
)


def _sessions(start: datetime, count: int) -> list[dict]:
    rows: list[dict] = []
    for offset in range(count):
        session = start + timedelta(days=offset)
        values = (
            (100, 101, 99, 100),    # one-minute opening range
            (100, 102, 99, 102),    # confirmed breakout
            (102, 103, 101, 102),   # next-bar entry
            (102, 107, 101, 106),   # 1.5R target, but not 2R
        )
        for minute, (open_, high, low, close) in enumerate(values):
            rows.append({
                "symbol": "SPY",
                "timestamp": (session + timedelta(minutes=minute)).isoformat(),
                "open": open_, "high": high, "low": low, "close": close,
                "volume": 1, "provider": "alpaca", "feed": "sip",
            })
    return rows


def _persist_gate(ledger: EdgeLedger, candidate_id: str, lane: str, *,
                  passes: bool = True, record: bool = True,
                  score: float = 1.0) -> tuple[dict, dict]:
    prefix = f"{lane}-{'pass' if passes else 'fail'}"
    fit = [] if lane == "shadow" else [
        {"vehicle": "equity", "session_date": "2024-01-02",
         "opportunity_id": f"{prefix}-fit", "net_pnl": 1.0},
    ]
    heldout = [
        {"vehicle": "equity", "session_date": f"2024-01-0{day}",
         "opportunity_id": f"{prefix}-held-{day}",
         "net_pnl": score if passes else -1.0}
        for day in (3, 4)
    ]
    fit_floor = structural_floor(
        fit, vehicle="equity", min_trades=1, min_sessions=1,
        required=lane != "shadow")
    held_floor = structural_floor(
        heldout, vehicle="equity", min_trades=1, min_sessions=1)
    separation = (heldout_separation(fit, heldout) if lane == "backtest" else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": True, "mode": "new_data"})
    checks = {"edge_positive": passes, "family_fdr_significant": True}
    envelope = verified_gate_envelope(
        lane=lane, vehicle="equity", fit=fit, heldout=heldout,
        fit_floor=fit_floor, heldout_floor=held_floor,
        control={"kind": "matched_actual_baseline", "actual_control": True,
                 "available": True, "matched": len(heldout),
                 "mean_delta": score if passes else -1.0},
        p_value=.01, q_value=.02, alpha=.05,
        falsification={"passes": passes, "method": "test_placebo"},
        separation=separation, checks=checks, passes=passes,
        performance={"heldout_delta": score if passes else -1.0,
                     "max_drawdown": 0.0 if passes else 2.0})
    run = ledger.append_run(
        candidate_id, lane=lane, vehicle="equity", fit=fit, heldout=heldout,
        metrics={"gate": {"passes": not passes}, "confidence": 0.0})
    for row in [*fit, *heldout]:
        ledger.append_trade(run["run_id"], row)
    if record:
        ledger.record_verified_gate(run["run_id"], envelope)
    return run, envelope


class EdgeLedgerStoreExtractionTests(unittest.TestCase):
    def test_store_symbols_are_identical_through_edge_facades(self):
        names = (
            "VEHICLES", "LANES", "LIFECYCLE", "CANDIDATE", "BACKTEST_PASSED",
            "SHADOW", "VALIDATED", "CHAMPION", "RETIRED", "DEMOTED",
            "SCHEMA_VERSION", "DEFAULT_DB_PATH", "PAPER_DEMOTION_MIN_OUTCOMES",
            "PAPER_DEMOTION_R_FLOOR",
            "canonical_json", "content_hash", "hash_dataset", "hash_config",
            "hash_provenance", "hash_file", "provenance_hash", "init_ledger", "init_db",
        )
        for name in names:
            self.assertIs(getattr(edge_ledger, name), getattr(edge_ledger_store, name))
            self.assertIs(getattr(edge_lab, name), getattr(edge_ledger_store, name))
        self.assertIs(edge_ledger.EdgeLedger, edge_lab.EdgeLedger)

    def test_hash_aliases_are_deterministic_and_reject_nonfinite_json(self):
        value = {"z": "café", "a": [1, True, None]}
        self.assertEqual(canonical_json(value), '{"a":[1,true,null],"z":"café"}')
        self.assertIs(hash_dataset, content_hash)
        self.assertIs(hash_config, content_hash)
        self.assertIs(hash_provenance, content_hash)
        expected = content_hash(value)
        self.assertEqual(hash_dataset(value), expected)
        self.assertEqual(hash_config(value), expected)
        self.assertEqual(hash_provenance(value), expected)
        with self.assertRaises(ValueError):
            canonical_json(float("nan"))
        with self.assertRaises(ValueError):
            canonical_json(float("inf"))

    def test_fresh_schema_initialization_is_idempotent_and_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.sqlite3"
            self.assertEqual(init_ledger(path), {"db_path": str(path), "schema": SCHEMA_VERSION})
            self.assertEqual(init_db(path), {"db_path": str(path), "schema": SCHEMA_VERSION})
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute("SELECT value FROM ledger_meta WHERE key='schema'").fetchone()[0],
                    str(SCHEMA_VERSION),
                )
            candidate = EdgeLedger(path).register_candidate(
                "ibr.target.1_5r", hypothesis="immutable", config={})
            with closing(sqlite3.connect(path)) as db:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    db.execute(
                        "UPDATE candidates SET hypothesis='changed' WHERE candidate_id=?",
                        (candidate["candidate_id"],),
                    )


class EdgeDiscoveryLifecycleTests(unittest.TestCase):
    def test_trade_metrics_reject_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="finite evidence", config={})
            run = ledger.append_run(candidate["candidate_id"], lane="backtest")
            base = {
                "session_date": "2024-01-02",
                "opportunity_id": "finite-check",
            }
            for value in ("inf", "-inf", "nan"):
                with self.subTest(net_pnl=value), self.assertRaisesRegex(
                        ValueError, "net_pnl must be finite"):
                    ledger.append_trade(run["run_id"], {**base, "net_pnl": value})
                with self.subTest(return_value=value), self.assertRaisesRegex(
                        ValueError, "return_value must be finite"):
                    ledger.append_trade(run["run_id"], {
                        **base, "opportunity_id": f"return-{value}",
                        "net_pnl": 1.0, "return_value": value,
                    })
            for field in ("net_pnl", "return_value"):
                for invalid in (True, b"1.0", 10 ** 10000):
                    with self.subTest(field=field, value=type(invalid).__name__), \
                            self.assertRaisesRegex(ValueError, f"{field} must be numeric"):
                        ledger.append_trade(run["run_id"], {
                            **base, "opportunity_id": f"invalid-{field}",
                            "net_pnl": 1.0, field: invalid,
                        })

    def test_paper_outcomes_demote_a_champion_that_breaks_the_rolling_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="earlier target", config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            for lane in ("backtest", "shadow"):
                _persist_gate(ledger, candidate_id, lane)
                if lane == "backtest":
                    ledger.transition(candidate_id, "backtest_passed",
                                      reason="backtest gates passed")
                else:
                    ledger.transition(candidate_id, "shadow",
                                      reason="shadow evidence started")
                    ledger.transition(candidate_id, "validated",
                                      reason="shadow gates passed")
            ledger.transition(candidate_id, "champion",
                              reason="best validated evidence")
            for index in range(20):
                outcome = ledger.ingest_paper_outcome(candidate_id, {
                    "vehicle": "equity", "opportunity_id": f"paper-{index}",
                    "session_date": f"2024-02-{index + 1:02d}",
                    "net_pnl": -1, "risk_usd": 10, "r_multiple": 999,
                })
            self.assertEqual(outcome["status"], "demoted")

    def test_paper_outcomes_recompute_r_and_reject_manual_demotion(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="paper guard", config={"strategy": {"target_r": 1.5}})
            with self.assertRaisesRegex(ValueError, "positive risk_usd"):
                ledger.ingest_paper_outcome(candidate["candidate_id"], {
                    "vehicle": "equity", "net_pnl": -1, "r_multiple": -100,
                    "demote": True,
                })
            result = ledger.ingest_paper_outcome(candidate["candidate_id"], {
                "vehicle": "equity", "opportunity_id": "paper-safe",
                "net_pnl": -1, "risk_usd": 10, "r_multiple": -100,
                "demote": True,
            })
            self.assertEqual(result["rolling_r"], -0.1)
            self.assertEqual(result["status"], "candidate")

    def test_discovery_operational_error_is_distinct_from_insufficient_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options_only = root / "options.jsonl"
            options_only.write_text(
                json.dumps({"kind": "quote", "symbol": "SPY"}) + "\n",
                encoding="utf-8")
            result = subprocess.run([
                sys.executable, "research.py", "edge", "discover",
                "--data", str(options_only), "--db", str(root / "edge.sqlite3")],
                cwd=Path(__file__).resolve().parents[2], check=False,
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_forward_lifecycle_cannot_be_manually_advanced_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="earlier target", config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            with self.assertRaisesRegex(ValueError, "verified gate evidence"):
                ledger.transition(candidate_id, "backtest_passed", reason="operator request")
            ledger.append_run(
                candidate_id, lane="backtest", vehicle="equity",
                fit=[{"session_date": "2024-01-02"}],
                heldout=[{"session_date": "2024-01-03"}],
                metrics={"gate": {"passes": True}})
            with self.assertRaisesRegex(ValueError, "lacks verified gate evidence"):
                ledger.transition(candidate_id, "backtest_passed", reason="forged metrics")
            _persist_gate(ledger, candidate_id, "backtest")
            self.assertEqual(ledger.transition(
                candidate_id, "backtest_passed", reason="gates passed")["status"],
                "backtest_passed")
            with self.assertRaisesRegex(ValueError, "passing shadow verified evidence"):
                ledger.transition(candidate_id, "shadow", reason="operator request")

    def test_retirement_rollback_and_forged_gate_cannot_bypass_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="earlier target",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            with self.assertRaisesRegex(ValueError, "verified gate evidence"):
                ledger.transition(candidate_id, "retired", reason="manual retirement")
            with self.assertRaisesRegex(ValueError, "rollback cannot bypass evidence"):
                ledger.transition(candidate_id, "candidate", reason="rollback", rollback=True)
            run, envelope = _persist_gate(
                ledger, candidate_id, "backtest", passes=True, record=False)
            envelope["passes"] = False
            with self.assertRaisesRegex(ValueError, "envelope/hash"):
                ledger.record_verified_gate(run["run_id"], envelope)

    def test_latest_failing_proof_makes_a_champion_ineligible(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="earlier target",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            _persist_gate(ledger, candidate_id, "backtest")
            ledger.transition(candidate_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, candidate_id, "shadow")
            ledger.transition(candidate_id, "shadow", reason="shadow proof")
            ledger.transition(candidate_id, "validated", reason="validated proof")
            self.assertIsNotNone(ledger.select_champion(
                vehicle="equity", min_confidence=.9))
            self.assertTrue(ledger.eligibility(candidate_id)["eligible"])
            self.assertEqual(
                ledger.latest_verified_run(candidate_id, lane="shadow")["lane"],
                "shadow")
            _persist_gate(ledger, candidate_id, "shadow", passes=False)
            self.assertIsNone(ledger.select_champion(
                vehicle="equity", min_confidence=.9))
            self.assertFalse(ledger.eligibility(candidate_id)["eligible"])

    def test_new_champion_keeps_previous_proved_edge_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.baseline", vehicle="equity", hypothesis="proved edge 1",
                config={"strategy": {"target_r": 2.0}})
            first_id = candidate["candidate_id"]
            _persist_gate(ledger, first_id, "backtest")
            ledger.transition(first_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, first_id, "shadow")
            ledger.transition(first_id, "shadow", reason="shadow proof")
            ledger.transition(first_id, "validated", reason="validated proof")
            first = ledger.select_champion(vehicle="equity", min_confidence=.9)
            self.assertIsNotNone(first)

            candidate = ledger.register_candidate(
                "ibr.target.3r", vehicle="equity", hypothesis="proved edge 2",
                config={"strategy": {"target_r": 3.0}})
            second_id = candidate["candidate_id"]
            _persist_gate(ledger, second_id, "backtest", score=2.0)
            ledger.transition(second_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, second_id, "shadow", score=2.0)
            ledger.transition(second_id, "shadow", reason="shadow proof")
            ledger.transition(second_id, "validated", reason="validated proof")

            # Explicit evidence-authorized champion selection is also used by
            # the conservative ranker when a stronger candidate appears.
            ledger.transition(second_id, "champion", reason="stronger conservative evidence")
            selected = ledger.select_champion(vehicle="equity", min_confidence=.9)
            self.assertIsNotNone(selected)
            self.assertEqual((ledger.candidate(first_id) or {})["status"], "validated")
            self.assertEqual((ledger.candidate(second_id) or {})["status"], "champion")

    def test_auto_requires_a_later_forward_tail_for_shadow(self):
        registry = {"variants": [
            {"variant_id": "ibr.baseline", "strategy_id": "ibr",
             "base_version": "v1", "overrides": {}, "vehicles": ["equity"],
             "hypothesis": "registered baseline"},
            {"variant_id": "ibr.target.1_5r", "strategy_id": "ibr",
             "base_version": "v1", "overrides": {"strategy.target_r": 1.5},
             "vehicles": ["equity"], "hypothesis": "earlier target"},
        ]}
        config = {"strategy": {"range_minutes": 1, "range_stop": True,
                                "target_r": 2}}
        first = _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20)
        later = _sessions(datetime(2024, 2, 1, 14, 30, tzinfo=timezone.utc), 20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_one = root / "first.jsonl"
            data_all = root / "all.jsonl"
            variants = root / "variants.yaml"
            db = root / "edge.sqlite3"
            data_one.write_text("\n".join(json.dumps(row) for row in first), encoding="utf-8")
            data_all.write_text("\n".join(json.dumps(row) for row in first + later), encoding="utf-8")
            variants.write_text(json.dumps(registry), encoding="utf-8")

            initial = discover(data_one, db_path=db, variants_path=variants,
                               config=config, min_trades=5, min_sessions=5,
                               lane="auto")
            candidate = initial["variants"][0]
            self.assertEqual(candidate["status"], "backtest_passed")
            self.assertIsNone(candidate["shadow_run_id"])

            with self.assertRaises(DiscoveryError):
                discover(data_one, db_path=db, variants_path=variants,
                         config=config, min_trades=5, min_sessions=5,
                         lane="shadow")

            forward = discover(data_all, db_path=db, variants_path=variants,
                               config=config, min_trades=5, min_sessions=5,
                               lane="auto")
            candidate = forward["variants"][0]
            self.assertIn(candidate["status"], {"validated", "champion"})
            self.assertEqual(candidate["mode"], "shadow")
            self.assertEqual(candidate["unseen_sessions"], 20)
            trades = EdgeLedger(db).trades(candidate["candidate_id"], lane="shadow")
            self.assertTrue(trades)
            self.assertTrue(all(row["session_date"] >= "2024-02-01" for row in trades))


if __name__ == "__main__":
    unittest.main()
