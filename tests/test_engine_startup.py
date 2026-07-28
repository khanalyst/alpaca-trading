"""Engine.__init__ must actually run.

Every other engine test constructs the object with ``Engine.__new__(Engine)``
to skip the exchange and the journal. That is reasonable for unit-testing a
method, and it left the real startup path - the one `main.py run` takes -
completely uncovered.

It cost a broken release: ``__init__`` still referenced
``brain.PROMPT_VERSION`` after that constant was replaced with a
per-strategy ``prompt_version()``, so the full suite passed while
``main.py run`` died immediately with an AttributeError.

This file exercises the constructor with the exchange and journal stubbed,
so anything ``__init__`` touches has to exist.
"""

import unittest
from unittest.mock import MagicMock, patch

from tests.helpers import valid_config


class EngineStartupTests(unittest.TestCase):
    def _build(self, cfg=None):
        cfg = cfg or valid_config()
        with patch("agent.engine.Exchange") as exchange, \
             patch("agent.engine.AlertManager"), \
             patch("agent.engine.state.configure_runtime"), \
             patch("agent.engine.state.bind_runtime_identity"), \
             patch("agent.engine.state.check_journal"), \
             patch("agent.engine.state.set_journal_context"), \
             patch("agent.engine.state.new_run_id", return_value="run-test"), \
             patch("agent.engine.state.stable_fingerprint",
                   return_value="cfg-test"), \
             patch("agent.engine.state.code_fingerprint",
                   return_value="code-test"):
            exchange.return_value = MagicMock()
            from agent.engine import Engine
            return Engine(cfg, light=True)

    def test_the_constructor_runs_at_all(self):
        # The regression this file exists for: every attribute __init__
        # reads has to exist on the modules it reads them from.
        engine = self._build()
        self.assertEqual(engine.run_id, "run-test")

    def test_prompt_version_is_derived_from_the_active_strategy(self):
        engine = self._build()
        self.assertTrue(engine.prompt_version)
        self.assertNotEqual(engine.prompt_version, "")
        # 16 hex characters, matching brain.prompt_version's digest slice.
        self.assertEqual(len(engine.prompt_version), 16)

    def test_strategy_identity_is_captured_for_the_journal(self):
        engine = self._build()
        self.assertEqual(engine.strategy_id, "momentum")
        self.assertEqual(engine.strategy_version, "phase1-v2")

    def test_two_strategies_produce_different_prompt_versions(self):
        # Prompt caching keys off this, so two strategies sharing a version
        # would share a cache entry and one would get the other's prompt.
        first = self._build().prompt_version
        cfg = valid_config()
        cfg["strategy"]["id"] = "trend-multiday"
        cfg["strategy"]["version"] = "v1"
        cfg["strategy"]["signal_timeframe"] = "4h"
        second = self._build(cfg).prompt_version
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
