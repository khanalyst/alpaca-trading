"""Process ownership and emergency handoff regressions; no broker access."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy import trader_supervisor as supervisor


class SupervisorTests(unittest.TestCase):
    def test_hung_child_is_reaped_and_releases_its_lock(self):
        # A real child owns a real lock: killing the owner, then waiting, must
        # make the same inode lockable without ever deleting the lock file.
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "owner.lock"
            ready = Path(directory) / "ready"
            code = ("import fcntl,sys,time,signal; from pathlib import Path; "
                    "h=open(sys.argv[1],'w'); fcntl.flock(h,fcntl.LOCK_EX); "
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    "Path(sys.argv[2]).write_text('ready'); time.sleep(30)")
            child = subprocess.Popen([sys.executable, "-c", code, str(lock), str(ready)],
                                     start_new_session=True)
            try:
                import time
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                with lock.open("a") as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertTrue(supervisor.terminate_child(child, grace=.05))
                    self.assertIsNotNone(child.poll())
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if child.poll() is None:
                    supervisor.terminate_child(child, grace=.05)

    def test_only_owned_child_fresh_progress_extends_deadline(self):
        child = Mock(pid=27)
        child.poll.return_value = None
        for payload in ({"pid": 99, "updated_ts": 100.0},
                        {"pid": 27, "updated_ts": 10000.0},
                        {"pid": 27, "updated_ts": 1.0},
                        {"pid": 27, "updated_ts": 100.0}):
            clock = [0.0]
            def sleep(seconds):
                clock[0] += seconds
            with patch.object(supervisor, "_read_json", return_value=payload):
                result = supervisor.monitor(
                    child, Path("unused"), max_age=3, interval=1,
                    started_wall=100, stop_requested=lambda: False,
                    monotonic=lambda: clock[0], wall_time=lambda: 100 + clock[0],
                    sleep=sleep)
            self.assertEqual(result, "unresponsive")
            self.assertEqual(clock[0], 3)

    def test_fresh_heartbeat_progress_is_not_a_timeout(self):
        clock = [0.0]
        child = Mock(pid=27)
        child.poll.side_effect = lambda: None if clock[0] < 8 else 0
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(supervisor, "_read_json", side_effect=lambda _: {
                "pid": 27, "updated_ts": 100 + clock[0]}):
            result = supervisor.monitor(
                child, Path("unused"), max_age=3, interval=1,
                started_wall=100, stop_requested=lambda: False,
                monotonic=lambda: clock[0], wall_time=lambda: 100 + clock[0], sleep=sleep)
        self.assertEqual(result, "exit")

    def test_recovery_only_after_confirmed_child_exit(self):
        for reaped in (False, True):
            order = []
            child = Mock(pid=27, returncode=-9)
            child.poll.return_value = -9
            def terminate(*args, **kwargs):
                order.append("waited")
                return reaped
            def recover(*args):
                order.append("recover")
                return {"flattened": True}
            with patch("main.load_cfg", return_value={"mode": "paper"}), \
                 patch.object(supervisor.state, "configure_runtime"), \
                 patch.object(supervisor.subprocess, "Popen", return_value=child), \
                 patch.object(supervisor, "write_status"), \
                 patch.object(supervisor, "monitor", return_value="unresponsive"), \
                 patch.object(supervisor, "terminate_child", side_effect=terminate), \
                 patch.object(supervisor, "recover", side_effect=recover):
                self.assertEqual(supervisor.supervise("test.json"), 1)
            if reaped:
                self.assertEqual(order, ["waited", "recover"])
            else:
                self.assertNotIn("recover", order)

    def test_authenticated_recovery_retains_pause_on_account_failure(self):
        from agent import state
        from deploy import watchdog
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            old = state.RUNTIME, state.RUNTIME_SCOPE
            state._set_paths(runtime, "paper")
            state.update_state({"runtime_mode": "paper", "state": "RUNNING"})
            provider = Mock()
            provider.account.side_effect = RuntimeError("offline")
            try:
                with patch.object(state, "configure_runtime"), \
                     self.assertRaises(watchdog.WatchdogError):
                    watchdog.run_once({"mode": "paper"}, provider, max_age=300,
                                      terminated_child=True)
                self.assertTrue(state.load_state()["operator_pause"])
                self.assertEqual(state.load_state()["state"], "PAUSED")
                provider.close_position.assert_not_called()
            finally:
                state._set_paths(*old)

    def test_status_failure_still_stops_child_before_recovery(self):
        order = []
        child = Mock(pid=27, returncode=-9)
        with patch("main.load_cfg", return_value={"mode": "paper"}), \
             patch.object(supervisor.state, "configure_runtime"), \
             patch.object(supervisor.subprocess, "Popen", return_value=child), \
             patch.object(supervisor, "write_status", side_effect=OSError("disk full")), \
             patch.object(supervisor, "terminate_child", side_effect=lambda *a, **k: order.append("reaped") or True), \
             patch.object(supervisor, "recover", side_effect=lambda *a: order.append("recovery") or {}):
            self.assertEqual(supervisor.supervise("test.json"), 1)
        self.assertEqual(order, ["reaped", "recovery"])


if __name__ == "__main__":
    unittest.main()
