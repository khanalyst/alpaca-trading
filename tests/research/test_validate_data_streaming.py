import io
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from argparse import Namespace
from unittest.mock import patch


_CLI_PATH = Path(__file__).parents[2] / "research.py"
_CLI_SPEC = importlib.util.spec_from_file_location("research_cli_streaming_test",
                                                    _CLI_PATH)
research_cli = importlib.util.module_from_spec(_CLI_SPEC)
assert _CLI_SPEC.loader is not None
_CLI_SPEC.loader.exec_module(research_cli)


def _bar():
    stamp = "2025-01-06T14:30:00+00:00"
    return {
        "kind": "bar", "provider": "alpaca", "feed": "iex",
        "source_mode": "forward_observed", "symbol": "SPY",
        "timestamp": stamp, "observed_at": stamp, "as_of": stamp,
        "open": 100.0, "high": 101.0, "low": 99.0,
        "close": 100.0, "volume": 10,
    }


class ValidateDataStreamingTests(unittest.TestCase):
    def test_file_validation_does_not_materialize_json_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "market.jsonl"
            source.write_text(json.dumps(_bar()) + "\n", encoding="utf-8")
            args = Namespace(input=source, provider="alpaca", feed="iex",
                             diagnostic_only=False)
            with patch.object(research_cli, "_json_rows",
                              side_effect=AssertionError("materialized rows")), \
                 patch.object(research_cli, "print") as emit:
                self.assertEqual(research_cli.cmd_validate_data(args), 0)
            report = json.loads(emit.call_args.args[0])
            self.assertTrue(report["valid"])
            self.assertEqual(report["counts"]["bars"], 1)

    def test_stdin_is_spooled_then_replayed_for_validation_and_normalization(self):
        payload = json.dumps(_bar()) + "\n"
        args = Namespace(input="-", provider="alpaca", feed="iex",
                         diagnostic_only=False)
        with patch.object(research_cli.sys, "stdin", io.StringIO(payload)), \
             patch.object(research_cli, "print") as emit:
            self.assertEqual(research_cli.cmd_validate_data(args), 0)
        report = json.loads(emit.call_args.args[0])
        self.assertTrue(report["valid"])
        self.assertEqual(report["counts"]["bars"], 1)

    def test_directory_validation_streams_each_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for name in ("2025-01-06.jsonl", "2025-01-07.jsonl"):
                (source / name).write_text(json.dumps(_bar()) + "\n",
                                            encoding="utf-8")
            args = Namespace(input=source, provider="alpaca", feed="iex",
                             diagnostic_only=False)
            with patch.object(research_cli, "_json_rows",
                              side_effect=AssertionError("materialized rows")), \
                 patch.object(research_cli, "print") as emit:
                self.assertEqual(research_cli.cmd_validate_data(args), 0)
            report = json.loads(emit.call_args.args[0])
            self.assertTrue(report["valid"])
            self.assertEqual(report["counts"]["bars"], 2)

    def test_source_change_between_validation_passes_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "market.jsonl"
            source.write_text(json.dumps(_bar()) + "\n", encoding="utf-8")
            args = Namespace(input=source, provider="alpaca", feed="iex",
                             diagnostic_only=False)
            original_iter = research_cli._iter_json_rows

            def mutate_after_rows(path):
                yield from original_iter(path)
                changed = _bar()
                changed["symbol"] = "QQQ"
                Path(path).write_text(json.dumps(changed) + "\n",
                                      encoding="utf-8")

            with patch.object(research_cli, "_iter_json_rows",
                              side_effect=mutate_after_rows), \
                 patch.object(research_cli, "print") as emit:
                self.assertEqual(research_cli.cmd_validate_data(args), 2)
            report = json.loads(emit.call_args.args[0])
            self.assertFalse(report["valid"])
            self.assertIn(
                "source: research source changed after provenance validation",
                report["errors"],
            )


if __name__ == "__main__":
    unittest.main()
