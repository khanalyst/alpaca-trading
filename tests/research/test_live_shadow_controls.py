"""Production-path control resolution checks for the live shadow worker."""

import csv
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.contracts.rule import rule_variant_id, validate_rule_spec
from deploy.recorder_market import _event_key
from research.edge_ledger import EdgeLedger
from research.factory_core import template_hypothesis
from research.factory_ledger import FactoryLedger
from research.live_shadow import (ShadowConfig, ShadowRunner, ShadowStore,
                                   _read_candidates)


class LiveShadowControlTests(unittest.TestCase):
    def test_rule_descendant_uses_factory_root_synthetic_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge_path = root / "edge.sqlite3"
            shadow_path = root / "shadow.sqlite3"
            hypothesis = template_hypothesis(0)
            FactoryLedger(edge_path).register(hypothesis)
            root_spec = validate_rule_spec(hypothesis.rule_spec)
            descendant = validate_rule_spec({**root_spec, "target_r": 2.5})
            ledger = EdgeLedger(edge_path)
            candidate = ledger.register_candidate(
                rule_variant_id(descendant), strategy_id="rule", vehicle="equity",
                hypothesis="descendant", config={
                    "strategy": {"id": "rule", "rule_spec": descendant}},
                axes={"hypothesis_id": hypothesis.hypothesis_id})
            control = ShadowRunner(ShadowConfig(root / "empty.csv", edge_path,
                                                shadow_path))._rule_root_control(candidate)
            self.assertIsNotNone(control)
            self.assertEqual(control["candidate_id"],
                             f"shadow:baseline:{candidate['candidate_id']}")
            self.assertEqual(control["config"]["strategy"]["rule_spec"], root_spec)
            self.assertIsNone(ShadowRunner(ShadowConfig(
                root / "empty.csv", edge_path, root / "shadow2.sqlite"
            ))._rule_root_control(ledger.register_candidate(
                rule_variant_id(root_spec), strategy_id="rule", vehicle="equity",
                hypothesis="root", config={
                    "strategy": {"id": "rule", "rule_spec": root_spec}},
                axes={"hypothesis_id": hypothesis.hypothesis_id})))

    def test_ibr_baseline_is_read_even_while_candidate_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge_path = root / "edge.sqlite3"
            ledger = EdgeLedger(edge_path)
            baseline = ledger.register_candidate(
                "ibr.baseline", strategy_id="ibr", vehicle="equity",
                hypothesis="baseline", config={"strategy": {"id": "ibr"}})
            candidate = ledger.register_candidate(
                "ibr.range.30", strategy_id="ibr", vehicle="equity",
                hypothesis="candidate", config={"strategy": {"id": "ibr"}})
            rows = _read_candidates(edge_path, max_candidates=10)
            ids = {row["candidate_id"] for row in rows}
            self.assertIn(baseline["candidate_id"], ids)
            self.assertNotIn(candidate["candidate_id"], ids)

    def test_paired_root_control_gets_own_replay_and_mismatch_fails_closed(self):
        """The synthetic root is a real shadow arm, not an empty replay stub."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge_path = root / "edge.sqlite3"
            shadow_path = root / "shadow.sqlite3"
            corpus_path = root / "recorded.csv"

            hypothesis = template_hypothesis(0)
            FactoryLedger(edge_path).register(hypothesis)
            root_spec = validate_rule_spec(hypothesis.rule_spec)
            descendant = validate_rule_spec({**root_spec, "target_r": 2.5})
            candidate = EdgeLedger(edge_path).register_candidate(
                rule_variant_id(descendant), strategy_id="rule", vehicle="equity",
                hypothesis="descendant", config={
                    "strategy": {"id": "rule", "version": "v1",
                                  "variant_id": rule_variant_id(descendant),
                                  "rule_spec": descendant},
                    "risk": {"risk_per_trade_pct": 1},
                    "execution": {}, "session": {}},
                axes={"hypothesis_id": hypothesis.hypothesis_id})
            with closing(sqlite3.connect(edge_path)) as db, db:
                db.execute("UPDATE candidate_state SET status='backtest_passed' "
                           "WHERE candidate_id=?", (candidate["candidate_id"],))

            stamp = "2026-01-02T20:59:00+00:00"
            fields = ["event_key", "event_type", "symbol", "timestamp", "as_of",
                      "observed_at", "provider", "feed", "open", "high", "low",
                      "close", "volume"]
            with corpus_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "event_key": _event_key("bar_1m", "SPY", stamp),
                    "event_type": "bar_1m", "symbol": "SPY", "timestamp": stamp,
                    "as_of": "2026-01-02T21:00:00+00:00",
                    "observed_at": "2026-01-02T21:01:00+00:00",
                    "provider": "recorded", "feed": "iex", "open": "100",
                    "high": "103", "low": "99", "close": "100", "volume": "1000",
                })

            runner = ShadowRunner(ShadowConfig(corpus_path, edge_path, shadow_path))
            control_id = f"shadow:baseline:{candidate['candidate_id']}"
            event_at = "2026-01-02T21:01:00+00:00"

            def evaluate(paired, event, _bars, _quotes, _options):
                spec = paired["config"]["strategy"]["rule_spec"]
                target_r = float(spec["target_r"])
                setup_type = f"rule_{spec['family']}"
                signal = {
                    "symbol": "SPY", "direction": "long", "setup_type": setup_type,
                    "signal_ts": datetime.fromisoformat(stamp).timestamp(),
                    "decision_timestamp": event_at, "entry_timestamp": event_at,
                }
                plan = {
                    "symbol": "SPY", "direction": "long", "setup_type": setup_type,
                    "signal_ts": signal["signal_ts"], "stop_price": 99.0,
                    "target_price": 99.0 + (1.0 + target_r), "stop_distance": 1.0,
                    "target_r": target_r, "execution_profile": "shares",
                    "decision_timestamp": event_at, "entry_timestamp": event_at,
                }
                return ("open_incomplete", "paired test open", {
                    "session_date": "2026-01-02", "strategy_id": "rule",
                    "variant_id": paired["variant_id"], "signal": signal,
                    "setup_plan": plan, "risk_plan": plan,
                }, {**plan, "entry_price": 100.0, "shares": 1.0,
                    "risk_usd": 100.0, "notional": 100.0})

            def account(_bars, _options, spec, **kwargs):
                target_r = float(spec["target_r"])
                target_price = 99.0 + (1.0 + target_r)
                trade = {
                    "symbol": "SPY", "session_date": "2026-01-02",
                    "direction": "long", "setup_type": f"rule_{spec['family']}",
                    "signal_timestamp": stamp, "decision_timestamp": event_at,
                    "entry_timestamp": event_at, "stop_price": 99.0,
                    "target_price": target_price, "stop_distance": 1.0,
                    "net_pnl": 0.0, "return_value": 0.0,
                }
                return {"starting_cash": 100_000.0, "ending_equity": 100_000.0,
                        "realized_pnl": 0.0, "rows": [trade],
                        "account_id": kwargs.get("account_id")}

            edge_before = edge_path.read_bytes()
            with patch.object(runner, "_evaluate", side_effect=evaluate), \
                    patch("research.live_shadow.simulate_account",
                          side_effect=account):
                runner.run_once()

            store = ShadowStore(shadow_path)
            self.assertEqual(len(store.decisions(candidate["candidate_id"])), 1)
            self.assertEqual(len(store.decisions(control_id)), 1)
            self.assertTrue(store.gate_rows(candidate["candidate_id"]))
            self.assertTrue(store.gate_rows(control_id))
            control_replay = store.replay_metadata(control_id)
            self.assertEqual(len(control_replay), 1)
            self.assertTrue(control_replay[0]["details"]["shadow_signatures"])
            self.assertTrue(control_replay[0]["details"]["replay_signatures"])
            with closing(sqlite3.connect(shadow_path)) as db:
                books = db.execute(
                    "SELECT candidate_id,status FROM virtual_books "
                    "ORDER BY candidate_id").fetchall()
            self.assertEqual(
                books,
                [(candidate["candidate_id"], "closed_replay"),
                 (control_id, "closed_replay")],
            )
            self.assertEqual(edge_before, edge_path.read_bytes())

            # A later replay correction that changes only the candidate's
            # semantic target must revoke its gate rows while the paired root
            # remains independently matched.
            def mismatching_account(_bars, _options, spec, **kwargs):
                result = account(_bars, _options, spec, **kwargs)
                if str(kwargs.get("account_id", "")).startswith(
                        f"shadow:{candidate['candidate_id']}:"):
                    result["rows"][0]["target_price"] += 0.25
                return result

            with patch.object(runner, "_evaluate", side_effect=evaluate), \
                    patch("research.live_shadow.simulate_account",
                          side_effect=mismatching_account):
                runner.run_once()
            self.assertEqual(store.gate_rows(candidate["candidate_id"]), [])
            self.assertTrue(store.gate_rows(control_id))
            self.assertEqual(len(store.decisions(control_id)), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
