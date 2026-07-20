import json
import tempfile
import unittest
from pathlib import Path

from agent import state


class StateSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.originals = (
            state.RUNTIME, state.STATE_FILE, state.PID_FILE, state.DB_FILE)
        runtime = Path(self.temp.name)
        state.RUNTIME = runtime
        state.STATE_FILE = runtime / "state.json"
        state.PID_FILE = runtime / "agent.pid"
        state.DB_FILE = runtime / "journal.db"

    def tearDown(self):
        (state.RUNTIME, state.STATE_FILE, state.PID_FILE,
         state.DB_FILE) = self.originals
        self.temp.cleanup()

    def test_corrupt_state_is_preserved_and_forces_killed(self):
        state.RUNTIME.mkdir(parents=True, exist_ok=True)
        state.STATE_FILE.write_text("{not-json")

        loaded = state.load_state()

        self.assertEqual(loaded["state"], state.KILLED)
        self.assertTrue(loaded["operator_pause"])
        self.assertIn("state file was corrupt", loaded["kill_reason"])
        backups = list(state.RUNTIME.glob("state.corrupt.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{not-json")
        persisted = json.loads(state.STATE_FILE.read_text())
        self.assertEqual(persisted["state"], state.KILLED)

    def test_invalid_state_cannot_be_saved(self):
        invalid = dict(state.DEFAULT)
        invalid["state"] = "RUNNIG"
        with self.assertRaisesRegex(ValueError, "invalid state"):
            state.save_state(invalid)

    def test_valid_json_with_malformed_trade_metadata_fails_closed(self):
        state.RUNTIME.mkdir(parents=True, exist_ok=True)
        malformed = dict(state.DEFAULT)
        malformed["state"] = state.RUNNING
        malformed["active_trades"] = {
            "BTC/USDT:USDT": {"trade_id": "x", "direction": "sideways"}
        }
        state.STATE_FILE.write_text(json.dumps(malformed))
        loaded = state.load_state()
        self.assertEqual(loaded["state"], state.KILLED)


if __name__ == "__main__":
    unittest.main()
