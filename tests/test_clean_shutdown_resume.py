"""Opt-in: a graceful, broker-flat shutdown does not leave an operator pause."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.alpaca_provider import AlpacaError
from agent.engine import Engine
from agent.runtime_control import RESUME_AFTER_CLEAN_SHUTDOWN_ENV
from tests.test_runtime_safety import FakeProvider, _cfg


class CleanShutdownResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpaca-clean-shutdown-")
        from agent import state
        self.original_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.tmp.name)
        state.configure_runtime("paper")
        state.ensure_ready()
        self.addCleanup(self._restore)

    def _restore(self):
        from agent import state
        state.RUNTIME_BASE = self.original_base
        state.configure_runtime("paper")
        self.tmp.cleanup()

    def _graceful_run(self, *, flatten_complete=True, cycle_error=None, before=None):
        """Start, receive SIGTERM during the first cycle, then shut down."""
        from agent import state
        engine = Engine(_cfg(), provider=FakeProvider())

        def cycle(*_args, **_kwargs):
            if before is not None:
                before(state)
            if cycle_error is not None:
                raise cycle_error
            engine.request_shutdown("SIGTERM")
            return {}

        with patch.object(engine, "_ensure_order_ready", return_value=True), \
             patch.object(engine, "run_once", side_effect=cycle), \
             patch.object(engine, "_flatten_all_impl", return_value=flatten_complete):
            try:
                engine.run()
            except AlpacaError:
                pass
        return state.load_state()

    def test_default_keeps_the_pause(self):
        with patch.dict(os.environ, {RESUME_AFTER_CLEAN_SHUTDOWN_ENV: ""}):
            runtime = self._graceful_run()
        self.assertTrue(runtime["operator_pause"])

    def test_opt_in_clears_the_pause_after_a_clean_flat_shutdown(self):
        from agent import state
        with patch.dict(os.environ, {RESUME_AFTER_CLEAN_SHUTDOWN_ENV: "1"}):
            runtime = self._graceful_run()
        self.assertEqual(runtime["state"], state.PAUSED)
        self.assertFalse(runtime["operator_pause"])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["reason"], "clean_shutdown_resume_authorized")
        events = state.JOURNAL_FILE.exists()
        self.assertTrue(events)

    def test_opt_in_keeps_the_pause_when_the_flatten_is_incomplete(self):
        with patch.dict(os.environ, {RESUME_AFTER_CLEAN_SHUTDOWN_ENV: "1"}):
            runtime = self._graceful_run(flatten_complete=False)
        self.assertTrue(runtime["operator_pause"])

    def test_opt_in_changes_nothing_after_a_cycle_fault(self):
        """A faulted run is not a clean shutdown: the switch must not act.

        The trader itself never paused on a cycle fault; the supervisor's
        recovery does after the non-zero exit.  So the end state must be the
        same with the switch on and off, and the resume path must not run.
        """
        from agent import state
        with patch.dict(os.environ, {RESUME_AFTER_CLEAN_SHUTDOWN_ENV: ""}):
            off = self._graceful_run(cycle_error=AlpacaError("broker down"))
        with patch.dict(os.environ, {RESUME_AFTER_CLEAN_SHUTDOWN_ENV: "1"}):
            on = self._graceful_run(cycle_error=AlpacaError("broker down"))
        self.assertEqual(off["operator_pause"], on["operator_pause"])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertNotEqual(heartbeat.get("reason"), "clean_shutdown_resume_authorized")

    def test_opt_in_keeps_the_pause_with_a_kill_reason(self):
        def kill(state):
            state.update_state(lambda current: {**current, "kill_reason": "operator kill"})
        with patch.dict(os.environ, {RESUME_AFTER_CLEAN_SHUTDOWN_ENV: "1"}):
            runtime = self._graceful_run(before=kill)
        self.assertTrue(runtime["operator_pause"])

    def test_opt_in_keeps_the_pause_with_a_daily_risk_stop(self):
        def stop(state):
            state.update_state(lambda current: {
                **current, "risk_day": {**dict(current.get("risk_day") or {}),
                                        "limit_hit": True}})
        with patch.dict(os.environ, {RESUME_AFTER_CLEAN_SHUTDOWN_ENV: "1"}):
            runtime = self._graceful_run(before=stop)
        self.assertTrue(runtime["operator_pause"])

    def test_opt_in_keeps_the_pause_with_local_trades_left(self):
        def leave_trade(state):
            state.update_state(lambda current: {
                **current, "active_trades": {"SPY": {"symbol": "SPY", "qty": 1}}})
        with patch.dict(os.environ, {RESUME_AFTER_CLEAN_SHUTDOWN_ENV: "1"}):
            runtime = self._graceful_run(before=leave_trade)
        self.assertTrue(runtime["operator_pause"])

    def test_a_crash_during_shutdown_still_leaves_the_pause_durable(self):
        """request_shutdown commits the pause before any flatten runs."""
        from agent import state
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        state.update_state(lambda current: {**current, "state": state.RUNNING,
                                            "operator_pause": False})
        with patch.dict(os.environ, {RESUME_AFTER_CLEAN_SHUTDOWN_ENV: "1"}):
            engine.request_shutdown("SIGTERM")
        self.assertTrue(state.load_state()["operator_pause"])


if __name__ == "__main__":
    unittest.main()
