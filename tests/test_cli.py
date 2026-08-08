"""Truthful command exit semantics for operator-facing safety checks."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main


class _Engine:
    def __init__(self, *, check=None, status=None, flatten=True):
        self._check = check or {}
        self._status = status or {}
        self._flatten = flatten
        self.closed = 0

    def check(self, authenticated=False):
        return {**self._check, "authenticated": authenticated}

    def status(self):
        return dict(self._status)

    def flatten_all(self, reason):
        return self._flatten

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


if __name__ == "__main__":
    unittest.main()
