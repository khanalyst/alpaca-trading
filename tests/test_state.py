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


if __name__ == "__main__":
    unittest.main()
