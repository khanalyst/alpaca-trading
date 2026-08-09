"""Durable paper-runtime identity and single-process ownership checks."""

from pathlib import Path
import tempfile
import unittest

from agent import state
from agent import state_store


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

    def test_store_primitives_are_facade_exports(self):
        for name in ("RUNNING", "PAUSED", "DAY_STOPPED", "KILLED", "DEFAULT",
                     "StateCorruptionError", "_validated", "_atomic_write", "_read"):
            self.assertIs(getattr(state, name), getattr(state_store, name))

    def test_validation_drops_unknown_fields_and_is_deepcopy_isolated(self):
        source = {"unknown": "ignored"}
        result = state_store._validated(source)
        self.assertNotIn("unknown", result)
        result["active_trades"]["AAPL"] = {"qty": 2}
        self.assertNotIn("AAPL", state_store.DEFAULT["active_trades"])
        fallback = {"active_trades": {}}
        loaded = state_store._read(Path(self.tmp.name) / "missing.json", fallback)
        loaded["active_trades"]["AAPL"] = {"qty": 1}
        self.assertNotIn("AAPL", fallback["active_trades"])

    def test_atomic_write_is_private_and_rejects_non_finite_json_without_replace(self):
        path = Path(self.tmp.name) / "atomic.json"
        state_store._atomic_write(path, {"state": state.PAUSED})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        original = path.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            state_store._atomic_write(path, {"value": float("nan")})
        self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
