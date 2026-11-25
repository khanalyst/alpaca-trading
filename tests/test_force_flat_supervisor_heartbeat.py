"""Regression coverage for force-flat heartbeat progress and pause safety."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import state
from agent.engine import Engine
from deploy import trader_supervisor as supervisor
from tests.test_runtime_safety import FakeProvider, _cfg


class _FakeClock:
    def __init__(self, wall: float = 1_000.0):
        self.wall = wall
        self.monotonic_value = 0.0

    def wall_time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value


class _FakeChild:
    """A child that exits after enough monitor sleeps to exceed max_age."""

    pid = os.getpid()

    def __init__(self, *, polls_before_exit: int):
        self.polls_before_exit = polls_before_exit
        self.polls = 0
        self.returncode = None

    def poll(self):
        self.polls += 1
        if self.polls > self.polls_before_exit:
            self.returncode = 0
            return self.returncode
        return None


class ForceFlatSupervisorHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.runtime_tmp = tempfile.TemporaryDirectory(
            prefix="alpaca-force-flat-heartbeat-")
        self.original_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.runtime_tmp.name)
        state.configure_runtime("paper")
        state.ensure_ready()
        self.addCleanup(self._cleanup_runtime)

    def _cleanup_runtime(self):
        state.RUNTIME_BASE = self.original_runtime_base
        state.configure_runtime("paper")
        self.runtime_tmp.cleanup()

    @staticmethod
    def _near_close_provider() -> FakeProvider:
        provider = FakeProvider()
        # Keep the market inside the regular session for the entire simulated
        # >300-second monitor window while every cycle remains before_close.
        provider.day = replace(
            provider.day,
            close=datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc),
        )
        provider.now = datetime(2026, 8, 7, 17, 50, tzinfo=timezone.utc)
        return provider

    def test_FH01_before_close_cycles_keep_supervisor_heartbeat_fresh(self):
        """A live force-flat loop must refresh its own child heartbeat."""
        provider = self._near_close_provider()
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        engine._wall_clock = lambda: provider.now
        fake_time = _FakeClock(provider.now.timestamp())
        child = _FakeChild(polls_before_exit=7)
        cycle_results = []
        heartbeat_stamps = []

        def sleep(interval: float):
            fake_time.wall += interval
            provider.now += timedelta(seconds=interval)
            cycle_results.append(engine.run_once({}))
            heartbeat_stamps.append(json.loads(
                state.HEARTBEAT_FILE.read_text())['updated_ts'])
            fake_time.monotonic_value += interval

        with patch.object(state.time, "time", side_effect=fake_time.wall_time):
            first = engine.run_once({})
            initial_stamp = json.loads(
                state.HEARTBEAT_FILE.read_text())['updated_ts']
            result = supervisor.monitor(
                child, state.HEARTBEAT_FILE, max_age=300.0, interval=60.0,
                started_wall=fake_time.wall, stop_requested=lambda: False,
                monotonic=fake_time.monotonic, wall_time=fake_time.wall_time,
                sleep=sleep)
            final_heartbeat = json.loads(
                state.HEARTBEAT_FILE.read_text())

        self.assertEqual(first["action"], "force_flat")
        self.assertTrue(first["closed"])
        self.assertEqual(result, "exit")
        self.assertGreater(fake_time.monotonic_value, 300.0)
        self.assertEqual(len(cycle_results), 7)
        self.assertTrue(all(cycle["action"] == "force_flat" and
                            cycle["closed"] for cycle in cycle_results))
        self.assertEqual(heartbeat_stamps,
                         sorted(set(heartbeat_stamps)))
        self.assertGreater(final_heartbeat["updated_ts"], initial_stamp)
        self.assertEqual(final_heartbeat["status"], "running")
        self.assertEqual(final_heartbeat["reason"], "before_close")
        self.assertEqual(provider.orders_sent, [])
        self.assertFalse(state.load_state()["operator_pause"])

    def test_FH02_operator_pause_refreshes_paused_heartbeat_without_resume(self):
        """Protection-only force-flat cycles remain paused and observable."""
        provider = self._near_close_provider()
        state.update_state(lambda current: {
            **current, "state": state.PAUSED, "operator_pause": True,
        })
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        engine._wall_clock = lambda: provider.now
        fake_time = _FakeClock(provider.now.timestamp())

        with patch.object(state.time, "time", side_effect=fake_time.wall_time):
            before = state.write_heartbeat("paused", reason="operator_pause")
            fake_time.wall += 1.0
            result = engine.run_once({})
            heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())

        self.assertEqual(result["action"], "force_flat")
        self.assertTrue(result["closed"])
        self.assertEqual(heartbeat["status"], "paused")
        self.assertEqual(heartbeat["reason"], "operator_pause")
        self.assertGreater(heartbeat["updated_ts"], before["updated_ts"])
        runtime = state.load_state()
        self.assertEqual(runtime["state"], state.PAUSED)
        self.assertTrue(runtime["operator_pause"])
        self.assertEqual(provider.orders_sent, [])

    def test_flat_book_data_outage_reports_progress_to_supervisor(self):
        provider = FakeProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        engine._wall_clock = lambda: provider.now
        clock = _FakeClock(provider.now.timestamp())
        child = _FakeChild(polls_before_exit=7)

        def run_cycle(interval):
            provider.now += timedelta(seconds=interval)
            clock.wall += interval
            clock.monotonic_value += interval
            self.assertEqual(engine.run_once()["reason"], "market_data_unavailable")

        with patch.object(engine, "_collect", side_effect=RuntimeError("feed offline")), \
                patch.object(state.time, "time", side_effect=clock.wall_time):
            verdict = supervisor.monitor(
                child, state.HEARTBEAT_FILE, max_age=300, interval=60,
                started_wall=clock.wall, stop_requested=lambda: False,
                monotonic=clock.monotonic, wall_time=clock.wall_time, sleep=run_cycle)
        self.assertEqual(verdict, "exit")
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertEqual(heartbeat["reason"], "market_data_unavailable")
        self.assertFalse(state.load_state()["operator_pause"])
        self.assertEqual(provider.orders_sent, [])


if __name__ == "__main__":
    unittest.main()
