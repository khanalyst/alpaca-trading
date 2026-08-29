"""Focused regressions for shadow replay repair and deployment bootstrap."""

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from research import edge_discovery_core, gates
from research.edge_ledger import EdgeLedger
from research.factory_ledger import FactoryLedger
import research.live_shadow as live_shadow
from research.live_shadow import REPLAY_QUARANTINE_OVERFLOW_KEY, ShadowStore
from research.live_shadow import _recorded_session_calendar
from research.live_shadow_ingest import ShadowIngestConfig, _session_continuity, ingest_shadow
from tests.research.test_edge_discovery import _persist_gate


class LiveShadowRepairTests(unittest.TestCase):
    def test_quarantine_requires_explicit_repaired_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ShadowStore(Path(directory) / "shadow.sqlite3")
            blocked = store.quarantine_replay_session(
                candidate_id="candidate", session_date="2026-01-05",
                reason="missing middle session", status="incomplete",
                source_digest="source-old", shadow_digest="shadow-old")
            self.assertEqual(blocked["status"], "quarantined")
            self.assertEqual(store.replay_quarantine()[
                "candidate:2026-01-05"]["status"], "quarantined")

            repaired = store.repair_replay_session(
                candidate_id="candidate", session_date="2026-01-05",
                source_digest="source-new", shadow_digest="shadow-new",
                replay_digest="replay-new")
            self.assertEqual(repaired["status"], "repaired")
            self.assertEqual(repaired["repair_count"], 1)
            self.assertEqual(repaired["history"][-1]["status"], "repaired")

    def test_quarantine_overflow_retains_active_entries_and_exposes_sentinel(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ShadowStore(Path(directory) / "shadow.sqlite3")
            with patch.object(live_shadow, "MAX_ACTIVE_REPLAY_QUARANTINE", 2):
                for index in range(3):
                    store.quarantine_replay_session(
                        candidate_id=f"candidate-{index}",
                        session_date="2026-01-05", reason="incomplete",
                        status="incomplete", source_digest=f"source-{index}")
            quarantine = store.replay_quarantine()
            sentinel = quarantine[REPLAY_QUARANTINE_OVERFLOW_KEY]
            self.assertEqual(sentinel["status"], "overflow")
            self.assertEqual(sentinel["active_count"], 3)
            self.assertEqual(sentinel["max_active"], 2)
            self.assertEqual(len(quarantine), 4)  # three active + sentinel
            self.assertIn("candidate-0:2026-01-05", quarantine)
            self.assertIn("candidate-2:2026-01-05", quarantine)

    def test_calendar_catalog_does_not_contaminate_event_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ShadowStore(Path(directory) / "shadow.sqlite3")
            store.save_session_catalog({
                "2026-01-05": {"session_date": "2026-01-05",
                               "open": "2026-01-05T14:30:00+00:00",
                               "close": "2026-01-05T21:00:00+00:00",
                               "source": "recorder_alpaca_calendar"},
            })
            store.save_quarantine_events({
                "bad-row": {"event_key": "bad-row", "session_date": "2026-01-05",
                             "reason": "normalization_error"},
            })
            store.save_quarantine_events({
                "other-row": {"event_key": "other-row", "session_date": "2026-01-06",
                               "reason": "normalization_error"},
            })
            self.assertEqual(set(store.quarantine_events()), {"bad-row", "other-row"})
            self.assertEqual(set(store.session_catalog()), {"2026-01-05"})

    def test_calendar_catalog_detects_all_arm_middle_gap_without_weekdays(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ShadowStore(Path(directory) / "shadow.sqlite3")
            store.save_session_catalog({
                day: {"session_date": day, "open": f"{day}T14:30:00+00:00",
                      "close": f"{day}T21:00:00+00:00",
                      "source": "recorder_alpaca_calendar"}
                for day in ("2026-01-02", "2026-01-05", "2026-01-06")
            })
            missing, unknown = _session_continuity(
                store, "2025-12-31", ["2026-01-02", "2026-01-06"])
            self.assertEqual(missing, ["2026-01-05"])
            self.assertEqual(unknown, [])

            # A session with no calendar provenance is unknown, not silently
            # accepted as a weekday or holiday assumption.
            missing, unknown = _session_continuity(
                store, "2025-12-31", ["2026-01-02", "2026-01-06", "2026-01-07"])
            self.assertEqual(missing, ["2026-01-05"])
            self.assertEqual(unknown, ["2026-01-07"])

            # A stale catalog cannot authorize a newer tail by omission.
            store.save_session_catalog({
                "2025-12-30": {"session_date": "2025-12-30",
                               "open": "2025-12-30T14:30:00+00:00",
                               "close": "2025-12-30T21:00:00+00:00",
                               "source": "recorder_alpaca_calendar"},
            })
            missing, unknown = _session_continuity(
                store, "2026-01-01", ["2026-01-07"])
            self.assertEqual(missing, [])
            self.assertEqual(unknown, ["2026-01-07"])

    def test_recorder_sidecar_catalog_includes_completed_zero_event_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "market.csv"
            index = root / ".recorder-index.json"
            def bounds(day):
                return {
                    "open": f"{day}T14:30:00+00:00",
                    "close": f"{day}T21:00:00+00:00",
                }
            index.write_text(json.dumps({
                "session_calendar": {
                    day: bounds(day) for day in
                    ("2026-01-02", "2026-01-05", "2026-01-06")
                }
            }), encoding="utf-8")
            catalog = _recorded_session_calendar(corpus)
            self.assertEqual(set(catalog), {"2026-01-02", "2026-01-05", "2026-01-06"})

    def test_compose_bootstrap_is_explicit_and_operator_can_disable(self):
        compose = Path("compose.yaml").read_text(encoding="utf-8")
        env = Path("deploy/research.env.example").read_text(encoding="utf-8")
        self.assertIn(
            "ALPACA_RESEARCH_CALIBRATION_BOOTSTRAP_UNKNOWN: ${ALPACA_RESEARCH_CALIBRATION_BOOTSTRAP_UNKNOWN:-1}",
            compose)
        self.assertIn("ALPACA_RESEARCH_CALIBRATION_BOOTSTRAP_UNKNOWN=1", env)
        self.assertIn("Set to 0", env)

    def test_baseline_quarantine_blocks_tail_before_fdr_or_boundary_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge_path, shadow_path = root / "edge.sqlite3", root / "shadow.sqlite3"
            with patch.object(edge_discovery_core, "MIN_PROMOTION_CLUSTERS", 1), \
                    patch.multiple(
                        gates, PROTOCOL_BACKTEST_MIN_TRADES=1,
                        PROTOCOL_BACKTEST_MIN_SESSIONS=1,
                        PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
                        PROTOCOL_SHADOW_MIN_TRADES=1,
                        PROTOCOL_SHADOW_MIN_SESSIONS=1,
                        PROTOCOL_SHADOW_MIN_CLUSTERS=1,
                        PROTOCOL_QUALIFICATION_MIN_TRADES=1,
                        PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
                        PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1):
                ledger = EdgeLedger(edge_path)
                baseline = ledger.register_candidate(
                    "ibr.baseline", strategy_id="ibr", vehicle="equity",
                    hypothesis="baseline", config={"strategy": {"id": "ibr"}})
                candidate = ledger.register_candidate(
                    "ibr.range.30", strategy_id="ibr", vehicle="equity",
                    hypothesis="candidate", config={"strategy": {"id": "ibr"}})
                _persist_gate(ledger, candidate["candidate_id"], "backtest")
                ledger.transition(candidate["candidate_id"], "backtest_passed",
                                  reason="backtest proof")
                store = ShadowStore(shadow_path)
                store.save_session_catalog({
                    session: {
                        "session_date": session,
                        "open": f"{session}T14:30:00+00:00",
                        "close": f"{session}T21:00:00+00:00",
                        "source": "recorder_alpaca_calendar",
                    }
                    for session in (f"2024-04-{day:02d}" for day in range(1, 9))
                })
                null_id = f"shadow:null:{candidate['candidate_id']}"
                for arm, values in ((candidate["candidate_id"], 2.0),
                                    (baseline["candidate_id"], 0.0),
                                    (null_id, -1.0)):
                    for day in range(1, 9):
                        session = f"2024-04-{day:02d}"
                        row = {"vehicle": "equity", "symbol": "SPY",
                               "session_date": session,
                               "opportunity_id": f"equity:SPY:{session}",
                               "net_pnl": values, "return_value": values,
                               "no_trade": False}
                        replay = f"replay:{arm}:{session}"
                        store.replay_diff(
                            candidate_id=arm, session_date=session,
                            source_digest=f"source:{arm}:{session}",
                            shadow_digest=f"shadow:{arm}:{session}",
                            replay_digest=replay, status="match",
                            details={"complete": True, "signature_match": True})
                        store.record_replay_evidence(
                            candidate_id=arm, session_date=session,
                            replay_digest=replay, vehicle="equity",
                            starting_cash=100_000, ending_cash=100_000 + values,
                            realized_pnl=values, trades=[row], replay_status="match")
                store.quarantine_replay_session(
                    candidate_id=baseline["candidate_id"], session_date="2024-04-05",
                    reason="middle control replay incomplete", status="incomplete",
                    source_digest="source-old", shadow_digest="shadow-old")
                result = ingest_shadow(ShadowIngestConfig(
                    edge_path, shadow_path, min_trades=1, min_sessions=1))
                row = result["candidates"][0]
                self.assertEqual(row["status"], "repair_required")
                self.assertEqual({item["arm"] for item in row["repair_required"]}, {"baseline"})
                self.assertEqual(ledger.runs(candidate["candidate_id"], lane="shadow"), [])
                self.assertEqual(FactoryLedger(edge_path).fdr_state(
                    "shadow-confirmation-v5:equity")["tests"], 0)

    def test_absent_calendar_catalog_blocks_before_fdr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge_path, shadow_path = root / "edge.sqlite3", root / "shadow.sqlite3"
            with patch.object(edge_discovery_core, "MIN_PROMOTION_CLUSTERS", 1), \
                    patch.multiple(
                        gates, PROTOCOL_BACKTEST_MIN_TRADES=1,
                        PROTOCOL_BACKTEST_MIN_SESSIONS=1,
                        PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
                        PROTOCOL_SHADOW_MIN_TRADES=1,
                        PROTOCOL_SHADOW_MIN_SESSIONS=1,
                        PROTOCOL_SHADOW_MIN_CLUSTERS=1,
                        PROTOCOL_QUALIFICATION_MIN_TRADES=1,
                        PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
                        PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1):
                ledger = EdgeLedger(edge_path)
                baseline = ledger.register_candidate(
                    "ibr.baseline", strategy_id="ibr", vehicle="equity",
                    hypothesis="baseline", config={"strategy": {"id": "ibr"}})
                candidate = ledger.register_candidate(
                    "ibr.range.30", strategy_id="ibr", vehicle="equity",
                    hypothesis="candidate", config={"strategy": {"id": "ibr"}})
                _persist_gate(ledger, candidate["candidate_id"], "backtest")
                ledger.transition(candidate["candidate_id"], "backtest_passed",
                                  reason="backtest proof")
                store = ShadowStore(shadow_path)
                null_id = f"shadow:null:{candidate['candidate_id']}"
                for arm, value in ((candidate["candidate_id"], 2.0),
                                   (baseline["candidate_id"], 0.0),
                                   (null_id, -1.0)):
                    for day in (1, 2):
                        session = f"2024-04-{day:02d}"
                        row = {"vehicle": "equity", "symbol": "SPY",
                               "session_date": session,
                               "opportunity_id": f"equity:SPY:{session}",
                               "net_pnl": value, "return_value": value,
                               "no_trade": False}
                        replay = f"replay:{arm}:{session}"
                        store.replay_diff(
                            candidate_id=arm, session_date=session,
                            source_digest=f"source:{arm}:{session}",
                            shadow_digest=f"shadow:{arm}:{session}",
                            replay_digest=replay, status="match",
                            details={"complete": True, "signature_match": True})
                        store.record_replay_evidence(
                            candidate_id=arm, session_date=session,
                            replay_digest=replay, vehicle="equity",
                            starting_cash=100_000, ending_cash=100_000 + value,
                            realized_pnl=value, trades=[row], replay_status="match")
                result = ingest_shadow(ShadowIngestConfig(
                    edge_path, shadow_path, min_trades=1, min_sessions=1))
                row = result["candidates"][0]
                self.assertTrue(row["catalog_unavailable"])
                self.assertEqual(row["status"], "incomplete")
                self.assertEqual(ledger.runs(candidate["candidate_id"], lane="shadow"), [])
                self.assertEqual(FactoryLedger(edge_path).fdr_state(
                    "shadow-confirmation-v5:equity")["tests"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
