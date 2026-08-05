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
        self.assertEqual(engine.strategy_version, "phase1-v3")

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

    def test_live_artifact_gate_runs_before_external_clients(self):
        cfg = valid_config()
        cfg["mode"] = "live"
        events = []
        with patch("agent.engine.deployment.verify_live_artifact",
                   side_effect=lambda *args, **kwargs: (
                       events.append("gate")
                       or (_ for _ in ()).throw(
                           RuntimeError("missing reviewed packet")))), \
             patch("agent.engine.AlertManager",
                   side_effect=lambda *_args, **_kwargs: events.append("alert")), \
             patch("agent.engine.Exchange",
                   side_effect=lambda *_args, **_kwargs: events.append("exchange")), \
             patch("agent.engine.brain.LLM",
                   side_effect=lambda *_args, **_kwargs: events.append("llm")), \
             patch("agent.engine.state.configure_runtime"), \
             patch("agent.engine.state.bind_runtime_identity"), \
             patch("agent.engine.state.new_run_id", return_value="run-test"), \
             patch("agent.engine.state.stable_fingerprint",
                   return_value="cfg-test"), \
             patch("agent.engine.state.code_fingerprint",
                   return_value="code-test"):
            from agent.engine import Engine
            with self.assertRaisesRegex(RuntimeError, "missing reviewed packet"):
                Engine(cfg)
        self.assertEqual(events, ["gate"])

    def test_live_llm_receives_the_verified_catalog_and_system(self):
        cfg = valid_config()
        cfg["mode"] = "live"
        catalog = {"momentum": ()}
        artifact = {
            "packet_id": "packet-id", "artifact_hash": "a" * 64,
            "payload_hash": "e" * 64,
            "variant_id": "momentum.baseline",
            "variant_definition_hash": "b" * 64,
            "artifact_strategy_config_version": "c" * 16,
            "deployment_config_hash": "d" * 64,
        }
        llm = MagicMock()
        with patch("agent.engine.deployment.verify_live_artifact",
                   return_value=artifact) as verify, \
             patch("agent.engine.variants.research_selection_catalog",
                   return_value=catalog), \
             patch("agent.engine.brain.build_system",
                   return_value="captured-system"), \
             patch("agent.engine.brain.prompt_version",
                   return_value="prompt-version"), \
             patch("agent.engine.brain.LLM", return_value=llm) as llm_cls, \
             patch("agent.engine.AlertManager",
                   return_value=MagicMock()), \
             patch("agent.engine.Exchange",
                   return_value=MagicMock()), \
             patch("agent.engine.RiskEngine"), \
             patch("agent.engine.Engine._build_shadow",
                   return_value=None), \
             patch("agent.engine.state.configure_runtime"), \
             patch("agent.engine.state.bind_runtime_identity"), \
             patch("agent.engine.state.check_journal"), \
             patch("agent.engine.state.set_journal_context"), \
             patch("agent.engine.state.new_run_id", return_value="run-test"), \
             patch("agent.engine.state.stable_fingerprint",
                   return_value="cfg-test"), \
             patch("agent.engine.state.code_fingerprint",
                   return_value="code-test"):
            from agent.engine import Engine
            Engine(cfg)
        verify.assert_called_once_with(
            cfg, catalog=catalog, system_prompt="captured-system")
        llm_cls.assert_called_once_with(
            cfg, catalog=catalog, system="captured-system")


if __name__ == "__main__":
    unittest.main()
