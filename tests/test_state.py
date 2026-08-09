"""Durable paper-runtime identity and single-process ownership checks."""

from pathlib import Path
import tempfile
import unittest

from agent import state


class StateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpaca-state-")
        state.configure_runtime("paper", Path(self.tmp.name))
        state.ensure_ready()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        state.configure_runtime("paper")
        self.tmp.cleanup()

    def test_runtime_lock_allows_exactly_one_owner(self):
        first = state.acquire_run_lock()
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(state.acquire_run_lock())
        finally:
            state.release_run_lock(first)
        second = state.acquire_run_lock()
        self.assertIsNotNone(second)
        state.release_run_lock(second)

    def test_paper_account_identity_cannot_change_silently(self):
        first = state.account_fingerprint("paper", "first-account")
        second = state.account_fingerprint("paper", "second-account")
        state.bind_account_identity(first)
        with self.assertRaises(state.RuntimeIdentityError):
            state.bind_account_identity(second)

    def test_corrupt_state_fails_closed_and_is_not_replaced(self):
        corrupt = "{not-json"
        state.STATE_FILE.write_text(corrupt, encoding="utf-8")
        with self.assertRaises(state.StateCorruptionError):
            state.load_state()
        with self.assertRaises(state.StateCorruptionError):
            state.bind_account_identity(
                state.account_fingerprint("paper", "paper-account"))
        self.assertEqual(state.STATE_FILE.read_text(encoding="utf-8"), corrupt)

    def test_paper_and_live_state_are_mode_isolated(self):
        root = Path(self.tmp.name) / "scoped"
        state.configure_runtime("paper", root)
        state.ensure_ready()
        state.bind_account_identity(
            state.account_fingerprint("paper", "shared-key"))
        paper_path = state.STATE_FILE
        state.configure_runtime("live", root)
        state.ensure_ready()
        live = state.load_state()
        self.assertEqual(live["runtime_mode"], "live")
        self.assertIsNone(live["account_fingerprint"])
        state.bind_account_identity(
            state.account_fingerprint("live", "shared-key"))
        self.assertNotEqual(
            state.load_state()["account_fingerprint"],
            __import__("json").loads(paper_path.read_text())["account_fingerprint"])

    def test_transactional_update_preserves_unrelated_fields(self):
        state.commit({"operator_pause": True})
        updated = state.update_state(
            lambda current: {**current, "state": state.RUNNING})
        self.assertTrue(updated["operator_pause"])
        self.assertEqual(updated["state"], state.RUNNING)


if __name__ == "__main__":
    unittest.main()
