"""Paper no-edge standby heartbeat regression; no broker access."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agent import state
from agent.config import DEFAULT_CONFIG
from agent.engine import Engine
from deploy import health


class FakePaperProvider:
    paper = True

    class Session:
        api_key = "paper-key"
        secret_key = "paper-secret"

    session = Session()

    def __init__(self):
        self.orders_sent = []

    def account(self):
        return {}

    def submit_order(self, request):
        self.orders_sent.append(request)
        raise AssertionError("no-proof standby submitted an order")


class PaperSupervisorStatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="paper-supervisor-status-")
        self.original_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.directory.name) / "runtime"
        state.configure_runtime("paper")
        state.ensure_ready()
        runtime = deepcopy(state.DEFAULT)
        runtime.update({
            "runtime_mode": "paper",
            "state": state.RUNNING,
            "operator_pause": False,
        })
        state.save_state(runtime)
        self.addCleanup(self._cleanup_runtime)

    def _cleanup_runtime(self):
        state.RUNTIME_BASE = self.original_runtime_base
        state.configure_runtime("paper")
        self.directory.cleanup()

    def test_open_market_no_proof_heartbeat_retains_selection_status(self):
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["research"]["db_path"] = str(
            Path(self.directory.name) / "empty-edge.sqlite3")
        provider = FakePaperProvider()
        clock = SimpleNamespace(
            timestamp=datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc),
            is_open=True,
        )

        with patch("agent.edge.resolve_validated_variant",
                   side_effect=[None, None]):
            engine = Engine(cfg, light=True, provider=provider)
            self.addCleanup(engine.close)
            with patch.object(engine, "_ensure_order_ready", return_value=True), \
                    patch.object(engine.market, "refresh_calendar"), \
                    patch.object(engine.market, "clock", return_value=clock), \
                    patch.object(engine, "_validated_clock_timestamp",
                                 return_value=clock.timestamp), \
                    patch.object(engine, "_inside_regular_session",
                                 return_value=True), \
                    patch.object(engine.market, "should_force_flat",
                                 return_value=False), \
                    patch.object(engine, "reconcile",
                                 return_value={"positions": []}), \
                    patch.object(engine, "_universe", return_value=[]), \
                    patch.object(engine, "_monitor_positions", return_value={}), \
                    patch.object(engine, "_update_daily_risk",
                                 return_value=(0.0, False)):
                result = engine.run_once({})

        self.assertEqual(result["action"], "hold")
        self.assertIn("no latest-passing validated edge", result["reason"])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "paused")
        self.assertEqual(heartbeat["reason"], "validated_edge_required")
        self.assertEqual(heartbeat["paper_selection"], {
            "configured_strategy": "rule",
            "selection_mode": "specific",
            "requested_variant": "auto",
            "resolved": None,
            "blocker_code": "validated_edge_required",
            "armed": True,
            "state": "waiting_for_proof",
        })

        projected = health.trader(
            state.HEARTBEAT_FILE, max_age=30,
            now=float(heartbeat["updated_ts"]))
        self.assertTrue(projected["ok"])
        self.assertEqual(projected["classification"],
                         "validated_edge_required")
        self.assertFalse(projected["operator_pause"])
        self.assertEqual(projected["paper_selection"],
                         heartbeat["paper_selection"])

        runtime = state.load_state()
        self.assertEqual(runtime["state"], state.PAUSED)
        self.assertFalse(runtime["operator_pause"])
        self.assertEqual(runtime["orders"], {})
        self.assertEqual(runtime["active_trades"], {})
        self.assertEqual(runtime["protection"], {})
        self.assertEqual(provider.orders_sent, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
