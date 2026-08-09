"""Truthful command exit semantics for operator-facing safety checks."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main


class _Engine:
    def __init__(self, *, check=None, status=None, flatten=True,
                 resume=None, resume_error=None):
        self._check = check or {}
        self._status = status or {}
        self._flatten = flatten
        self._resume = resume if resume is not None else {"resumed": True}
        self._resume_error = resume_error
        self.closed = 0
        self.resume_calls = 0

    def check(self, authenticated=False):
        return {**self._check, "authenticated": authenticated}

    def status(self):
        return dict(self._status)

    def flatten_all(self, reason):
        return self._flatten

    def resume(self):
        self.resume_calls += 1
        if self._resume_error is not None:
            raise self._resume_error
        return self._resume

    def close(self):
        self.closed += 1


class CliSafetyTests(unittest.TestCase):
    def test_checked_in_config_loads_without_yaml_only_syntax(self):
        cfg = main.load_cfg(Path(main.__file__).resolve().with_name("config.yaml"))
        self.assertEqual(cfg["mode"], "paper")
        self.assertTrue(cfg["research"]["require_validated_variant"])

    def test_check_and_status_fail_when_required_edge_is_missing(self):
        engine = _Engine(
            check={"edge_required": True, "edge_ready": False},
            status={"edge_required": True, "edge_ready": False})
        with patch.object(main, "_engine", return_value=engine), \
                redirect_stdout(StringIO()):
            self.assertEqual(main.cmd_check(
                SimpleNamespace(authenticated=True), {}), 1)
            self.assertEqual(main.cmd_status(SimpleNamespace(), {}), 1)
        self.assertEqual(engine.closed, 2)

    def test_flatten_never_reports_success_with_residual_positions(self):
        with patch.object(main, "_engine", return_value=_Engine(flatten=False)), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()) as error:
            code = main.cmd_flatten(SimpleNamespace(reason="test"), {})
        self.assertEqual(code, 1)
        self.assertIn("residual", error.getvalue())

    def test_authenticated_check_is_default_and_offline_is_explicit(self):
        parsed = main.parser().parse_args(["check"])
        self.assertTrue(parsed.authenticated)
        parsed = main.parser().parse_args(["check", "--offline"])
        self.assertFalse(parsed.authenticated)

    def test_offline_check_never_constructs_engine_or_requires_edge(self):
        output = StringIO()
        cfg = {"mode": "paper", "research": {
            "enabled": True, "require_validated_variant": True}}
        with patch.object(main, "_engine") as factory, redirect_stdout(output):
            code = main.cmd_check(SimpleNamespace(authenticated=False), cfg)
        self.assertEqual(code, 0)
        factory.assert_not_called()
        self.assertIn("local_config_valid", output.getvalue())
        self.assertIn("edge_checked", output.getvalue())
        self.assertIn("true", output.getvalue())

    def test_resume_parser_is_authenticated_flat_only_without_force_or_offline(self):
        parsed = main.parser().parse_args(["resume"])
        self.assertEqual(parsed.command, "resume")
        self.assertNotIn("offline", vars(parsed))
        self.assertNotIn("force", vars(parsed))
        parser = main.parser()
        subparsers = next(action for action in parser._actions
                          if getattr(action, "choices", None))
        self.assertIn("authenticate", subparsers.choices["resume"].description.lower())
        self.assertIn("flat", subparsers.choices["resume"].description)

    def test_resume_success_prints_result_without_closing_control_engine(self):
        output = StringIO()
        engine = _Engine(resume={"action": "resume", "state": "PAUSED"})
        with patch.object(main, "_engine", return_value=engine), \
                redirect_stdout(output), redirect_stderr(StringIO()):
            code = main.cmd_resume(SimpleNamespace(), {})
        self.assertEqual(code, 0)
        self.assertEqual(engine.resume_calls, 1)
        self.assertEqual(engine.closed, 0)
        self.assertTrue("action: resume" in output.getvalue() or
                        '"action": "resume"' in output.getvalue())

    def test_resume_failure_is_structured_on_stderr_without_closing(self):
        error = StringIO()
        engine = _Engine(resume_error=RuntimeError("not flat"))
        with patch.object(main, "_engine", return_value=engine), \
                redirect_stdout(StringIO()), redirect_stderr(error):
            code = main.cmd_resume(SimpleNamespace(), {})
        self.assertEqual(code, 1)
        self.assertIn("resume failed: not flat", error.getvalue())
        self.assertEqual(engine.closed, 0)

    def test_resume_constructor_failure_is_structured_on_stderr(self):
        error = StringIO()
        with patch.object(main, "_engine",
                          side_effect=RuntimeError("provider unavailable")), \
                redirect_stdout(StringIO()), redirect_stderr(error):
            code = main.cmd_resume(SimpleNamespace(), {})
        self.assertEqual(code, 1)
        self.assertIn("resume failed: provider unavailable", error.getvalue())


if __name__ == "__main__":
    unittest.main()
