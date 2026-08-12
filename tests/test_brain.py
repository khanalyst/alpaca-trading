"""Direct tests for the runtime decision-model safety boundary."""

import time
import unittest

from agent.brain import DecisionBrain
from agent.config import ConfigError, validate_config


class _Client:
    def __init__(self, result=None, error=None, delay=0):
        self.result = result
        self.error = error
        self.delay = delay

    def complete(self, *, system, prompt):
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


class DecisionBrainTests(unittest.TestCase):
    CFG = {"enabled": True, "provider": "openai", "model": "test",
           "timeout_seconds": .05}

    def test_empty_success_is_a_no_veto_decision(self):
        result = DecisionBrain(self.CFG, client=_Client(result="")).decide({}, {})
        self.assertEqual(result, {"decisions": []})

    def test_irrelevant_success_is_a_no_veto_decision(self):
        result = DecisionBrain(
            self.CFG,
            client=_Client(result='{"decisions":[{"symbol":"QQQ","action":"hold"}]}'),
        ).decide({"symbol": "SPY"}, {})
        self.assertEqual(result["decisions"][0]["symbol"], "QQQ")

    def test_provider_timeout_is_strict_and_quarantines_client(self):
        brain = DecisionBrain(self.CFG, client=_Client(delay=.5, result=""))
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            brain.decide({}, {})
        self.assertLess(time.monotonic() - started, .3)
        with self.assertRaises(TimeoutError):
            brain.decide({}, {})

    def test_provider_error_remains_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "provider down"):
            DecisionBrain(self.CFG, client=_Client(error=RuntimeError("provider down"))).decide({}, {})

    def test_config_validates_runtime_timeout(self):
        cfg = validate_config({"llm": {"enabled": True, "model": "test",
                                         "timeout_seconds": .5}})
        self.assertEqual(cfg["llm"]["timeout_seconds"], .5)
        with self.assertRaises(ConfigError):
            validate_config({"llm": {"enabled": True, "model": "test",
                                       "timeout_seconds": 0}})


if __name__ == "__main__":
    unittest.main()
