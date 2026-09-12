"""The deployed IBR opt-in cannot enable an authorizing or broker lane."""
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.config import load_config
from deploy import shadow


ROOT = Path(__file__).resolve().parents[1]


class IBRShadowOperationsTests(unittest.TestCase):
    def test_default_and_explicit_cli_opt_in(self):
        with patch.dict(os.environ, {"ALPACA_SHADOW_INCLUDE_IBR": "0"}):
            self.assertFalse(shadow.parser().parse_args([]).diagnostic_include_ibr)
            self.assertTrue(shadow.parser().parse_args(
                ["--diagnostic-include-ibr"]).diagnostic_include_ibr)
        with patch.dict(os.environ, {"ALPACA_SHADOW_INCLUDE_IBR": "1"}):
            self.assertTrue(shadow.parser().parse_args([]).diagnostic_include_ibr)
            self.assertFalse(shadow.parser().parse_args(
                ["--no-diagnostic-include-ibr"]).diagnostic_include_ibr)

    def test_invalid_env_and_authorizing_combination_fail_before_runner(self):
        with patch.object(shadow, "ShadowRunner") as runner:
            for value in ("", "true", "2", "-1"):
                with self.subTest(value=value), patch.dict(
                        os.environ, {"ALPACA_SHADOW_INCLUDE_IBR": value}), \
                        redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    shadow.main(["--once"])
                self.assertEqual(caught.exception.code, 2)
            with patch.dict(os.environ, {"ALPACA_SHADOW_INCLUDE_IBR": "1"}), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                shadow.main(["--no-diagnostic", "--once"])
            self.assertEqual(caught.exception.code, 2)
            runner.assert_not_called()

    def test_operations_entrypoint_passes_opt_in_with_unchanged_policy(self):
        runtime = load_config(ROOT / "config.yaml")
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"ALPACA_SHADOW_INCLUDE_IBR": "1"}), \
                patch.object(shadow, "load_runtime_config", return_value=runtime), \
                patch.object(shadow, "ShadowRunner") as runner, \
                patch.object(shadow, "_record_acceptance", return_value={"accepted": False}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            runner.return_value.run_once.return_value = {"candidate_errors": {}}
            result = shadow.main([
                "--once", "--shadow-db", str(Path(directory) / "shadow.sqlite3")])
        config = runner.call_args.args[0]
        self.assertEqual(result, 0)
        self.assertTrue(config.diagnostic)
        self.assertTrue(config.diagnostic_include_ibr)
        self.assertEqual(config.runtime_config, runtime)
        self.assertFalse(config.runtime_config["broker"]["allow_live"])

    def test_compose_opt_in_has_no_broker_secret_mount(self):
        compose = (ROOT / "compose.yaml").read_text()
        service = compose.split("  shadow:\n", 1)[1].split("  dashboard:\n", 1)[0]
        self.assertIn("ALPACA_SHADOW_INCLUDE_IBR: ${ALPACA_SHADOW_INCLUDE_IBR:-1}", service)
        self.assertIn('ALPACA_LIVE_ENABLE: "false"', service)
        self.assertNotIn("agent_credentials", service)
        self.assertNotIn("/run/secrets", service)


if __name__ == "__main__":
    unittest.main()
