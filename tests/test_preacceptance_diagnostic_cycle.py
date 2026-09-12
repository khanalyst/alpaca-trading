"""Bounded routing tests for the one-shot pre-acceptance diagnostic cycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WAIT_REASON = "1 accepted full session; 30 are required"


def _config(path: Path) -> None:
    path.write_text(json.dumps({
        "mode": "paper",
        "broker": {"paper": True, "allow_live": False,
                   "data_feed": "iex", "options_feed": "opra"},
        "universe": {"symbols": ["SPY"], "asset_classes": ["us_equity"]},
        "session": {"require_exact_calendar": True},
        "strategy": {"selection_mode": "all_proved",
                     "execution_mode": "shares"},
        "research": {"enabled": True, "require_validated_variant": True,
                     "strategy_llm": {"enabled": True,
                                      "provider": "openai", "model": "gpt-5"}},
    }), encoding="utf-8")


def _snapshot(path: Path) -> None:
    sessions = path / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "market-2026-09-10.csv").write_text(
        "event_key,timestamp\nrow,2026-09-10T13:30:00+00:00\n",
        encoding="utf-8")


def _fake_python(path: Path) -> None:
    path.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json
        import os
        from pathlib import Path
        import sys

        args = sys.argv[1:]
        log = Path(os.environ["PREACCEPTANCE_CALL_LOG"])

        def record():
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(args) + "\\n")

        def value(flag):
            return args[args.index(flag) + 1]

        target = args[0] if args else ""
        if target.endswith("/deploy/research_snapshot.py"):
            record()
            if len(args) > 1 and args[1] == "verify":
                print(json.dumps({{
                    "schema": "research-snapshot-verification.v1",
                    "verified": True, "immutable": True,
                    "identity": "sha256:" + "a" * 64,
                }}))
                raise SystemExit(0)
            raise SystemExit(91)
        if target.endswith("/deploy/research_census.py"):
            record()
            underpowered = os.environ.get(
                "PREACCEPTANCE_UNDERPOWERED", "1") == "1"
            reason = ({WAIT_REASON!r} if underpowered else
                      "30 accepted full sessions are available")
            state = ("waiting_for_forward_sessions" if underpowered else
                     "input_ready")
            print(json.dumps({{
                "structurally_underpowered": underpowered,
                "reason": reason,
                "readiness": {{
                    "schema": "research-readiness.v1", "state": state,
                    "reason": reason,
                    "complete_forward_partition_count": 1,
                    "accepted_forward_partition_count": 1 if underpowered else 30,
                    "required_forward_sessions": 30,
                    "readiness_session_count": 1 if underpowered else 30,
                    "recorded_sessions": 1 if underpowered else 30,
                    "required_sessions": 30,
                    "sessions_remaining": 29 if underpowered else 0,
                    "readiness_basis": "accepted_full_sessions",
                    "acceptance_required": True, "authorizing": False,
                }},
            }}))
            raise SystemExit(0)
        if target.endswith("/deploy/research_budget.py"):
            record()
            raise SystemExit(0)
        if target.endswith("/deploy/research_cache.py"):
            record()
            operation = args[1]
            if operation == "lookup":
                print(json.dumps({{"operation": "lookup", "hit": False}}))
                raise SystemExit(1)
            if operation == "publish":
                artifacts = {{}}
                for index, item in enumerate(args):
                    if item == "--artifact":
                        name, artifact = args[index + 1].split("=", 1)
                        artifacts[name] = {{"path": artifact}}
                print(json.dumps({{"operation": "publish",
                                  "artifacts": artifacts}}))
                raise SystemExit(0)
            print(json.dumps({{"operation": operation, "status": "ok"}}))
            raise SystemExit(0)
        if target.endswith("/deploy/research_dataset.py"):
            record()
            row = json.dumps({{
                "event_key": "bar-1", "event_type": "bar_1m",
                "symbol": "SPY", "timestamp": "2026-09-10T13:30:00+00:00",
                "as_of": "2026-09-10T13:31:00+00:00",
                "observed_at": "2026-09-10T13:31:00+00:00",
                "provider": "alpaca", "feed": "iex",
                "source_mode": "forward_observed", "open": 100,
                "high": 101, "low": 99, "close": 100, "volume": 1000,
            }}) + "\\n"
            for flag in ("--normalized", "--bars", "--replay"):
                Path(value(flag)).write_text(row, encoding="utf-8")
            Path(value("--options")).write_text("", encoding="utf-8")
            print(json.dumps({{
                "schema": "research-dataset.v1",
                "view_counts": {{"bars": 1, "quotes": 0,
                                "options": 0, "replay": 1}},
                "vehicle_filter": {{"schema": "vehicle-filter.v1",
                                    "status": "ok"}},
            }}))
            raise SystemExit(0)
        if target.endswith("/research.py"):
            record()
            command = args[1] if len(args) > 1 else ""
            if command == "validate-data" and "--diagnostic-only" in args:
                print(json.dumps({{"status": "valid", "diagnostic_only": True}}))
                raise SystemExit(0)
            raise SystemExit(92)
        if args[:2] == ["-m", "research.diagnostic_suite"]:
            record()
            required = {{"--data", "--agent-config", "--out",
                        "--diagnostic-only", "--workers"}}
            if not required.issubset(args) or value("--workers") != "2":
                raise SystemExit(93)
            output = Path(value("--out"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({{
                "schema": "complete-diagnostic-suite.v1",
                "status": "diagnostic_complete", "authorizing": False,
            }}) + "\\n", encoding="utf-8")
            Path(str(output) + ".manifest.json").write_text(
                json.dumps({{"immutable": True}}) + "\\n", encoding="utf-8")
            print(json.dumps({{"status": "diagnostic_complete",
                              "authorizing": False}}))
            raise SystemExit(2)
        os.execv(os.environ["PREACCEPTANCE_REAL_PYTHON"],
                 [os.environ["PREACCEPTANCE_REAL_PYTHON"], *args])
        """), encoding="utf-8")
    path.chmod(0o755)


def _calls(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line]


def _run(root: Path, *, opt_in: bool, underpowered: bool = True,
         overrides: dict[str, str | None] | None = None
         ) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    config = root / "config.json"
    snapshot = root / "snapshot"
    acceptance = root / "acceptance"
    recorded = root / "recorded"
    output = root / "diagnostic.json"
    call_log = root / "calls.jsonl"
    fake_python = root / "python"
    _config(config)
    _snapshot(snapshot)
    acceptance.mkdir()
    (recorded / "sessions").mkdir(parents=True)
    _fake_python(fake_python)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("ALPACA_")}
    env.update({
        "PYTHON": str(fake_python),
        "PREACCEPTANCE_REAL_PYTHON": sys.executable,
        "PREACCEPTANCE_CALL_LOG": str(call_log),
        "PREACCEPTANCE_UNDERPOWERED": "1" if underpowered else "0",
        "ALPACA_AGENT_CONFIG": str(config),
        "ALPACA_RECORDED_DATASET_ROOT": str(recorded),
        "ALPACA_RESEARCH_ACCEPTANCE_ROOT": str(acceptance),
        "ALPACA_RESEARCH_DIRECT_STATUS_FILE": str(root / "direct.json"),
        "ALPACA_RESEARCH_DIRECT_HEARTBEAT_SECONDS": "1",
        "ALPACA_RESEARCH_LLM_SECRETS_FILE": (
            "/missing/llm-secrets" if opt_in else "/dev/null"),
        "ALPACA_RESEARCH_SESSION_WINDOW": "1",
        "ALPACA_RESEARCH_MAX_SOURCE_BYTES": "1000000",
        "ALPACA_RESEARCH_PREPROCESSING_CACHE_ROOT": str(root / "cache"),
        "ALPACA_RESEARCH_BACKTEST": "1",
        "ALPACA_RESEARCH_STRESS_CALIBRATION_ENABLED": "1",
        "ALPACA_FACTORY_ENABLED": "1",
    })
    if opt_in:
        env.update({
            "ALPACA_RESEARCH_PREACCEPTANCE_DIAGNOSTIC_ONCE": "1",
            "ALPACA_RESEARCH_SNAPSHOT_ROOT": str(snapshot),
            "ALPACA_RESEARCH_DIAGNOSTIC_OUTPUT": str(output),
        })
    if overrides:
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    result = subprocess.run(
        ["deploy/research-cycle.sh"], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False)
    return result, _calls(call_log)


def _terminal(result: subprocess.CompletedProcess) -> dict:
    return json.loads(result.stdout.strip().splitlines()[-1])


class PreacceptanceDiagnosticCycleTests(unittest.TestCase):
    def test_default_underpowered_cycle_still_exits_before_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = _run(Path(directory), opt_in=False)

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(_terminal(result)["status"],
                         "waiting_for_forward_sessions")
        self.assertFalse(any(call[:2] == ["-m", "research.diagnostic_suite"]
                             for call in calls))
        self.assertFalse(any("llm-preflight" in call for call in calls))

    def test_opt_in_runs_only_bounded_diagnostic_then_restores_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, calls = _run(root, opt_in=True)
            output = root / "diagnostic.json"
            terminal = _terminal(result)

            self.assertTrue(output.is_file())
            self.assertTrue(Path(str(output) + ".manifest.json").is_file())

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(terminal["status"], "waiting_for_forward_sessions")
        self.assertEqual(terminal["reason"], WAIT_REASON)
        self.assertEqual(terminal["preflight"]["status"], "not_run")
        self.assertIn("skips provider preflight", terminal["preflight"]["reason"])
        suite = [call for call in calls
                 if call[:2] == ["-m", "research.diagnostic_suite"]]
        self.assertEqual(len(suite), 1)
        self.assertEqual(suite[0][suite[0].index("--workers") + 1], "2")
        self.assertIn("--diagnostic-only", suite[0])
        positions = {
            name: next(index for index, call in enumerate(calls)
                       if any(item.endswith(name) for item in call))
            for name in ("research_snapshot.py", "research_census.py",
                         "research_budget.py", "research_dataset.py")}
        suite_index = calls.index(suite[0])
        self.assertLess(positions["research_snapshot.py"],
                        positions["research_census.py"])
        self.assertLess(positions["research_census.py"],
                        positions["research_budget.py"])
        self.assertLess(positions["research_budget.py"],
                        positions["research_dataset.py"])
        self.assertLess(positions["research_dataset.py"], suite_index)
        validations = [call for call in calls
                       if call and call[0].endswith("/research.py") and
                       len(call) > 1 and call[1] == "validate-data"]
        self.assertEqual(len(validations), 1)
        self.assertIn("--diagnostic-only", validations[0])
        research_commands = {
            call[1] for call in calls
            if call and call[0].endswith("/research.py") and len(call) > 1}
        self.assertTrue(research_commands.isdisjoint({
            "llm-preflight", "vehicles", "backtest-ibr", "factory"}))
        self.assertFalse(any(call[:2] == ["-m", "research.cost_rerun"]
                             for call in calls))
        self.assertFalse(any("live_shadow" in " ".join(call) or
                             "live-shadow" in " ".join(call)
                             for call in calls))

    def test_unsafe_opt_in_inputs_fail_before_replay(self):
        cases = {
            "missing_output": {
                "ALPACA_RESEARCH_DIAGNOSTIC_OUTPUT": None},
            "missing_snapshot": {
                "ALPACA_RESEARCH_SNAPSHOT_ROOT": None},
            "zero_window": {
                "ALPACA_RESEARCH_SESSION_WINDOW": "0"},
            "zero_budget": {
                "ALPACA_RESEARCH_MAX_SOURCE_BYTES": "0"},
            "zero_padded_window": {
                "ALPACA_RESEARCH_SESSION_WINDOW": "00"},
            "zero_padded_budget": {
                "ALPACA_RESEARCH_MAX_SOURCE_BYTES": "000"},
            "invalid_window": {
                "ALPACA_RESEARCH_SESSION_WINDOW": "1x"},
            "fractional_budget": {
                "ALPACA_RESEARCH_MAX_SOURCE_BYTES": "0.5"},
            "explicit_dataset": {
                "ALPACA_RESEARCH_DATASET": "/tmp/not-a-sealed-snapshot"},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                result, calls = _run(
                    Path(directory), opt_in=True, overrides=overrides)
                self.assertEqual(result.returncode, 3,
                                 result.stderr + result.stdout)
                self.assertEqual(_terminal(result)["status"], "failed")
                self.assertFalse(any(
                    call[:2] == ["-m", "research.diagnostic_suite"]
                    for call in calls))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "diagnostic.json"
            existing.write_text("reserved\n", encoding="utf-8")
            result, calls = _run(root, opt_in=True, overrides={
                "ALPACA_RESEARCH_DIAGNOSTIC_OUTPUT": str(existing)})
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertFalse(any(call[:2] == ["-m", "research.diagnostic_suite"]
                             for call in calls))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, calls = _run(root, opt_in=True, overrides={
                "ALPACA_RESEARCH_DIAGNOSTIC_OUTPUT": str(
                    root / "snapshot" / "diagnostic.json")})
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertIn("outside the sealed snapshot", _terminal(result)["reason"])
        self.assertFalse(any(call[:2] == ["-m", "research.diagnostic_suite"]
                             for call in calls))

    def test_positive_zero_padded_budgets_remain_bounded_and_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = _run(Path(directory), opt_in=True, overrides={
                "ALPACA_RESEARCH_SESSION_WINDOW": "01",
                "ALPACA_RESEARCH_MAX_SOURCE_BYTES": "01000000",
            })

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(_terminal(result)["status"], "waiting_for_forward_sessions")
        self.assertEqual(sum(call[:2] == ["-m", "research.diagnostic_suite"]
                             for call in calls), 1)
        budgets = [call for call in calls
                   if call and call[0].endswith("/deploy/research_budget.py")]
        self.assertEqual(len(budgets), 1)
        self.assertEqual(budgets[0][budgets[0].index("--session-window") + 1], "01")
        self.assertEqual(budgets[0][budgets[0].index("--max-bytes") + 1], "01000000")

    def test_opt_in_rejects_ready_snapshot_and_points_to_standalone_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = _run(
                Path(directory), opt_in=True, underpowered=False)

        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        terminal = _terminal(result)
        self.assertEqual(terminal["status"], "failed")
        self.assertIn("standalone diagnostic", terminal["reason"])
        self.assertFalse(any(call[:2] == ["-m", "research.diagnostic_suite"]
                             for call in calls))
        self.assertFalse(any(call and call[0].endswith("research_dataset.py")
                             for call in calls))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
