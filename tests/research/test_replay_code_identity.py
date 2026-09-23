"""A run's code fingerprint covers the evaluator, not only the calling module."""

from pathlib import Path
import unittest
from unittest.mock import patch

from research import edge_ledger_store as store
import research.strategy_factory as factory


class ReplayCodeIdentityTests(unittest.TestCase):
    def test_identity_names_the_caller_and_every_evaluator_module(self):
        identity = store.replay_code_identity(Path(factory.__file__))
        self.assertEqual(identity["schema"], store.REPLAY_CODE_IDENTITY_SCHEMA)
        self.assertIn("research/strategy_factory.py", identity["files"])
        for module in store.REPLAY_EVALUATOR_MODULES:
            self.assertIn(module, identity["files"])
            self.assertEqual(len(identity["files"][module]), 64)

    def test_an_evaluator_change_moves_the_run_code_hash(self):
        before = store.provenance_hash(
            code=store.replay_code_identity(Path(factory.__file__)))["code_hash"]
        real = store.hash_file

        def edited(path):
            if Path(path).as_posix().endswith("agent/contracts/rule.py"):
                return "0" * 64
            return real(path)
        with patch.object(store, "hash_file", side_effect=edited):
            after = store.provenance_hash(
                code=store.replay_code_identity(Path(factory.__file__)))["code_hash"]
        self.assertNotEqual(before, after)

    def test_the_factory_and_live_ingest_use_the_wide_identity(self):
        root = Path(factory.__file__).resolve().parent
        for name in ("strategy_factory.py", "live_shadow_ingest.py"):
            source = (root / name).read_text()
            self.assertNotIn("code=Path(__file__)", source, name)
            self.assertIn("code=replay_code_identity(Path(__file__))", source, name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
