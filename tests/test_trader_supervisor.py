"""Process ownership and emergency handoff regressions; no broker access."""
from contextlib import closing
import json
from pathlib import Path
import signal
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy import trader_supervisor as supervisor


class SupervisorTests(unittest.TestCase):
    def test_paused_journal_anchor_preserves_wal_and_fresh_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            writer = sqlite3.connect(path)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE probe (value INTEGER)")
            writer.commit()
            writer.close()
            anchor = supervisor._open_journal_wal_anchor(path)
            try:
                self.assertFalse(anchor.in_transaction)
                with self.assertRaises(sqlite3.OperationalError):
                    anchor.execute("INSERT INTO probe VALUES (0)")
                for value in (1, 2):
                    writer = sqlite3.connect(path)
                    writer.execute("INSERT INTO probe VALUES (?)", (value,))
                    writer.commit()
                    writer.close()
                    self.assertTrue(Path(str(path) + "-wal").exists())
                    self.assertTrue(Path(str(path) + "-shm").exists())
                    reader = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
                    try:
                        self.assertEqual(reader.execute(
                            "SELECT max(value) FROM probe").fetchone()[0], value)
                    finally:
                        reader.close()
            finally:
                anchor.close()

    def test_pause_waits_for_persisted_resume_without_broker_recovery(self):
        with patch.object(supervisor.state, "load_state", side_effect=[
                {"operator_pause": True}, {"operator_pause": True}, {"operator_pause": False}]), \
             patch.object(supervisor.state, "write_heartbeat") as heartbeat, \
             patch.object(supervisor, "write_status"), \
             patch.object(supervisor, "recover") as recovery:
            sleep = Mock()
            self.assertTrue(supervisor.wait_until_resumed(
                Path("unused"), interval=5, stop_requested=lambda: False, sleep=sleep))
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(heartbeat.call_count, 2)
            recovery.assert_not_called()

    def test_shutdown_while_paused_never_launches_a_child(self):
        with patch("main.load_cfg", return_value={"mode": "paper"}), \
             patch.object(supervisor.state, "configure_runtime"), \
             patch.object(supervisor, "wait_until_resumed", return_value=False), \
             patch.object(supervisor, "write_status"), \
             patch.object(supervisor.subprocess, "Popen") as launch:
            self.assertEqual(supervisor.supervise("test.json"), 0)
            launch.assert_not_called()

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
                 patch.object(supervisor, "wait_until_resumed", return_value=True), \
                 patch.object(supervisor.subprocess, "Popen", return_value=child), \
                 patch.object(supervisor, "write_status"), \
                 patch.object(supervisor, "monitor", return_value="unresponsive"), \
                 patch.object(supervisor, "terminate_child", side_effect=terminate), \
                 patch.object(supervisor, "recover", side_effect=recover), \
                 patch.object(supervisor.state, "log_event"):
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
             patch.object(supervisor, "wait_until_resumed", return_value=True), \
             patch.object(supervisor.subprocess, "Popen", return_value=child), \
             patch.object(supervisor, "write_status", side_effect=OSError("disk full")), \
             patch.object(supervisor, "terminate_child", side_effect=lambda *a, **k: order.append("reaped") or True), \
             patch.object(supervisor, "recover", side_effect=lambda *a: order.append("recovery") or {}):
            self.assertEqual(supervisor.supervise("test.json"), 1)
        self.assertEqual(order, ["reaped", "recovery"])

    def test_unresponsive_reap_receipt_survives_status_rewrite(self):
        from agent import state
        child = Mock(pid=27, returncode=-9)
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            status_path = Path(directory) / "supervisor.json"
            old_paths = state.RUNTIME, state.RUNTIME_SCOPE
            state._set_paths(runtime, "paper")
            state.ensure_ready()
            try:
                with patch("main.load_cfg", return_value={"mode": "paper"}), \
                     patch.object(supervisor.state, "configure_runtime"), \
                     patch.object(supervisor, "wait_until_resumed", return_value=True), \
                     patch.object(supervisor.subprocess, "Popen", return_value=child), \
                     patch.object(supervisor, "monitor", return_value="unresponsive"), \
                     patch.object(supervisor, "terminate_child", return_value=True), \
                     patch.object(supervisor, "recover",
                                  return_value={"flattened": True}), \
                     patch.object(supervisor.signal, "signal", return_value=object()):
                    self.assertEqual(supervisor.supervise(
                        "test.json", status_path=status_path), 1)
                supervisor.write_status(status_path, "watching", child_pid=27)
                with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
                    row = db.execute(
                        "SELECT kind, payload FROM events "
                        "WHERE kind = 'supervisor_child_stopped' "
                        "ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "supervisor_child_stopped")
                payload = json.loads(row[1])
                self.assertEqual(payload["trigger"], "unresponsive")
                self.assertEqual(payload["monitor_result"], "unresponsive")
                self.assertEqual(payload["child_pid"], 27)
                self.assertEqual(payload["child_exit_code"], -9)
                self.assertEqual(payload["recovery_ok"], True)
                self.assertEqual(payload["runtime_mode"], "paper")
            finally:
                state._set_paths(*old_paths)

    def test_reap_audit_failure_still_recovers_without_relaunch(self):
        order = []
        child = Mock(pid=27, returncode=-9)

        def terminate(*args, **kwargs):
            order.append("reaped")
            return True

        def recover(*args):
            order.append("recovery")
            return {"flattened": True}

        with patch("main.load_cfg", return_value={"mode": "paper"}), \
             patch.object(supervisor.state, "configure_runtime"), \
             patch.object(supervisor, "wait_until_resumed", return_value=True), \
             patch.object(supervisor.subprocess, "Popen", return_value=child) as launch, \
             patch.object(supervisor, "write_status"), \
             patch.object(supervisor, "monitor", return_value="unresponsive"), \
             patch.object(supervisor, "terminate_child", side_effect=terminate), \
             patch.object(supervisor, "recover", side_effect=recover), \
             patch.object(supervisor.state, "log_event",
                          side_effect=OSError("journal unavailable")):
            self.assertEqual(supervisor.supervise("test.json"), 1)
        self.assertEqual(order, ["reaped", "recovery"])
        self.assertEqual(launch.call_count, 1)

    def test_received_signal_metadata_is_retained_in_final_status(self):
        child = Mock(pid=27, returncode=0)
        child.wait.return_value = 0
        callbacks = {}
        statuses = []

        def install(signum, handler):
            callbacks[signum] = handler
            return signal_handler_previous

        signal_handler_previous = object()

        def record_status(_path, status, **detail):
            statuses.append((status, detail))

        with patch("main.load_cfg", return_value={"mode": "paper"}), \
             patch.object(supervisor.state, "configure_runtime"), \
             patch.object(supervisor, "wait_until_resumed", return_value=True), \
             patch.object(supervisor.subprocess, "Popen", return_value=child), \
             patch.object(supervisor, "write_status", side_effect=record_status), \
             patch.object(supervisor.signal, "signal", side_effect=install), \
             patch.object(supervisor, "monitor", side_effect=lambda *args, **kwargs: (
                 callbacks[signal.SIGTERM](signal.SIGTERM, None) or "exit")):
            self.assertEqual(supervisor.supervise("test.json"), 0)

        self.assertEqual(statuses[-1][0], "stopped")
        received = statuses[-1][1]["received_signal"]
        self.assertEqual(received["source"],
                         "trader_supervisor_signal_handler")
        self.assertEqual(received["signal_name"], "SIGTERM")
        self.assertEqual(received["signal_number"], signal.SIGTERM)
        self.assertEqual(received["pid"], supervisor.os.getpid())
        self.assertEqual(received["ppid"], supervisor.os.getppid())
        self.assertEqual(statuses[-1][1]["signal_source"], received["source"])


if __name__ == "__main__":
    unittest.main()
