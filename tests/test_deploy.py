"""Dependency-light checks for the paper deployment boundary."""

from __future__ import annotations

import json
import csv
import io
from contextlib import closing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import call, patch
from pathlib import Path
from types import SimpleNamespace

try:
    from deploy import dashboard, health, recorder, scheduler, scheduler_output
    from deploy import shadow as shadow_service
except ModuleNotFoundError as exc:  # dependency-independent local test runs
    if exc.name == "yaml":
        raise unittest.SkipTest("PyYAML is supplied by the deployment lockfile")
    raise
import report
from agent.alpaca_provider import AlpacaProvider, AlpacaSession

# Several tests run the real ``deploy/research-cycle.sh`` with the repository
# as its working directory, and a real cycle archives its narrative and edge
# proofs under ``research/results``.  Redirecting both for the whole module
# keeps generated artifacts out of the working copy no matter which invocation
# a later test adds; every call site builds its environment from ``os.environ``.
_ARTIFACTS: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:                                     # noqa: N802
    global _ARTIFACTS
    _ARTIFACTS = tempfile.TemporaryDirectory(prefix="alpaca-test-artifacts.")
    root = Path(_ARTIFACTS.name)
    os.environ["ALPACA_RESEARCH_REPORT_DIR"] = str(root / "reports")
    os.environ["ALPACA_RESEARCH_PROOF_DIR"] = str(root / "proofs")
    # Keep legacy recorder fakes on one request; chunking itself has dedicated
    # tests with a deliberately small window.
    os.environ["ALPACA_RECORDER_FETCH_WINDOW_MINUTES"] = "1000000"
    os.environ["ALPACA_RECORDER_BAR_GAP_MINUTES"] = "5"
    os.environ["ALPACA_RECORDER_STRICT_BAR_FEEDS"] = ""


def tearDownModule() -> None:                                  # noqa: N802
    for name in ("ALPACA_RESEARCH_REPORT_DIR", "ALPACA_RESEARCH_PROOF_DIR",
                 "ALPACA_RECORDER_FETCH_WINDOW_MINUTES",
                 "ALPACA_RECORDER_BAR_GAP_MINUTES",
                 "ALPACA_RECORDER_STRICT_BAR_FEEDS"):
        os.environ.pop(name, None)
    if _ARTIFACTS is not None:
        _ARTIFACTS.cleanup()


class _MarketFake:
    def __init__(self, feed=None):
        self.seen = []
        self.data_feed = "iex" if feed is None else feed

    def bars(self, symbols, *, start, end, feed, **kwargs):
        self.seen.append(("bars", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=datetime(2026, 8, 8, 13, 30, tzinfo=timezone.utc),
            open=100, high=101, low=99, close=100.5, volume=10)]}

    def quotes(self, symbols, *, start, end, feed):
        self.seen.append(("quotes", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=datetime(2026, 8, 8, 13, 30, 1, tzinfo=timezone.utc),
            bid=100, ask=101, last=100.5)]}


class _WindowFake(_MarketFake):
    def __init__(self):
        super().__init__()
        self.starts = []

    def bars(self, symbols, *, start, end, feed, **kwargs):
        self.starts.append(start)
        return super().bars(symbols, start=start, end=end, feed=feed, **kwargs)

    def quotes(self, symbols, *, start, end, feed):
        self.starts.append(start)
        return super().quotes(symbols, start=start, end=end, feed=feed)


class _QuoteChunkFake:
    data_feed = "sip"

    def __init__(self):
        self.windows = []

    def bars(self, symbols, *, start, end, feed, **kwargs):
        self.windows.append(("bars", start, end))
        return {}

    def quotes(self, symbols, *, start, end, feed):
        self.windows.append(("quotes", start, end))
        timestamp = end - timedelta(seconds=1)
        return {"SPY": [SimpleNamespace(
            timestamp=timestamp, bid=100, ask=101, last=100.5)]}


class _SparseFeedFake:
    def __init__(self, feed):
        self.data_feed = feed
        self.seen = []

    def bars(self, symbols, *, start, end, feed, **kwargs):
        self.seen.append(("bars", start, end, feed))
        return {}

    def quotes(self, symbols, *, start, end, feed):
        self.seen.append(("quotes", start, end, feed))
        timestamp = end - timedelta(seconds=1)
        return {symbol: [SimpleNamespace(
            timestamp=timestamp, bid=100, ask=101, last=100.5)]
            for symbol in symbols}


class _MissingQuoteTimestampFake(_MarketFake):
    def quotes(self, symbols, *, start, end, feed):
        self.seen.append(("quotes", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=None, bid=100, ask=101, last=100.5)]}


class _FutureBarFake(_MarketFake):
    def bars(self, symbols, *, start, end, feed, **kwargs):
        self.seen.append(("bars", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=end + timedelta(minutes=1),
            open=100, high=101, low=99, close=100.5, volume=10)]}


class _OptionFake(_MarketFake):
    def __init__(self):
        super().__init__(feed="sip")
        self.option_calls = []
        self.option_timestamp = datetime.now(timezone.utc) - timedelta(minutes=2)

    def bars(self, symbols, *, start, end, feed, **kwargs):
        self.seen.append(("bars", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=self.option_timestamp.replace(microsecond=0),
            open=100, high=101, low=99, close=100.5, volume=10)]}

    def quotes(self, symbols, *, start, end, feed):
        self.seen.append(("quotes", feed))
        return {"SPY": [SimpleNamespace(
            timestamp=self.option_timestamp, bid=100, ask=101, last=100.5)]}

    def option_candidates(self, symbol, *, now, underlying_price, feed,
                          min_dte, max_dte):
        self.option_calls.append((symbol, underlying_price, feed,
                                  min_dte, max_dte))
        timestamp = self.option_timestamp
        expiration = datetime.now(timezone.utc).date() + timedelta(days=30)
        rows = []
        for right in ("call", "put"):
            for index in range(6):
                strike = 100 + index if right == "call" else 100 - index
                rows.append({
                    "symbol": f"SPY{expiration:%y%m%d}{right[0].upper()}{int(strike * 1000):08d}",
                    "underlying": "SPY", "expiration": expiration.isoformat(),
                    "strike": strike, "right": right, "multiplier": 100,
                    "timestamp": timestamp, "bid": 1 + index / 10,
                    "ask": 1.1 + index / 10, "bid_size": 10 + index,
                    "ask_size": 11 + index, "volume": 100 - index,
                    "open_interest": 200 - index,
                    "underlying_price": 100, "feed": feed,
                })
        return rows


class _MismatchedOptionFake(_OptionFake):
    def option_candidates(self, *args, **kwargs):
        rows = super().option_candidates(*args, **kwargs)
        return [{**row, "underlying": "BTC"} for row in rows]


class _CalendarFake:
    """Two regular sessions, one 13:00 early close and one missing holiday."""

    def __init__(self):
        self.calls = 0

    def calendar(self, start=None, end=None):
        self.calls += 1
        return [{"date": "2026-08-04", "open": "09:30", "close": "16:00"},
                {"date": "2026-08-06", "open": "09:30", "close": "16:00"},
                {"date": "2026-08-07", "open": "09:30", "close": "13:00"}]


def _corpus_rows(*, sessions: int, per_session: int, minute: int = 0,
                 feed: str = "iex") -> list[dict]:
    """Synthesize a durable corpus ending the session before the fakes' rows."""
    from deploy.recorder_market import _event_key
    rows = []
    for index in range(sessions):
        day = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc) - timedelta(
            days=sessions - index)
        for step in range(per_session):
            stamp = (day + timedelta(minutes=minute + step)).isoformat()
            rows.append({"event_key": _event_key("bar_1m", "SPY", stamp),
                         "observed_at": stamp, "provider": "alpaca", "feed": feed,
                         "event_type": "bar_1m", "symbol": "SPY",
                         "timestamp": stamp, "as_of": stamp, "open": 100,
                         "high": 101, "low": 99, "close": 100.5, "volume": 10})
    return rows


def _write_flat_corpus(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=recorder.FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in recorder.FIELDS}
                         for row in rows)


def _replay_corpus_csv(*, quotes: bool = True, sessions: int = 1) -> str:
    """One tradable IBR session per day, with quotes priced away from the bar."""
    from deploy.recorder_market import _event_key
    from zoneinfo import ZoneInfo
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=recorder.FIELDS)
    writer.writeheader()
    open_bell = datetime(2026, 8, 3, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    for session in range(sessions):
        day = open_bell + timedelta(days=session)
        for minute in range(60):
            stamp = (day + timedelta(minutes=minute)).astimezone(timezone.utc).isoformat()
            if minute < 15:
                values = (100.0, 100.5, 99.8, 100.2)
            elif minute == 15:
                values = (100.2, 101.5, 100.2, 101.4)
            else:
                base = 101.4 + minute * 0.02
                values = (base, base + 0.3, base - 0.1, base + 0.2)
            writer.writerow({
                "event_key": _event_key("bar_1m", "SPY", stamp), "observed_at": stamp,
                "provider": "alpaca", "feed": "iex", "event_type": "bar_1m",
                "symbol": "SPY", "timestamp": stamp, "as_of": stamp,
                "open": values[0], "high": values[1], "low": values[2],
                "close": values[3], "volume": 1000})
            if quotes:
                writer.writerow({
                    "event_key": _event_key("quote", "SPY", stamp), "observed_at": stamp,
                    "provider": "alpaca", "feed": "iex", "event_type": "quote",
                    "symbol": "SPY", "timestamp": stamp, "as_of": stamp,
                    "bid": values[0] - 0.5, "ask": values[0] + 0.5,
                    "bid_size": 10, "ask_size": 10})
    return buffer.getvalue()


def _run_research_cycle(dataset: Path | str, root: Path, **env):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    config = root / "research-test-config.json"
    if not config.exists():
        config.write_text(json.dumps({
            "mode": "paper",
            "broker": {"paper": True, "allow_live": False,
                       "data_feed": "iex", "options_feed": "opra"},
            "universe": {"asset_classes": ["us_equity", "us_option"]},
            # These are explicit synthetic/external fixtures without a
            # recorder sidecar. Exercise the diagnostic fallback while the
            # shipped production config remains exact-calendar strict.
            "session": {"require_exact_calendar": False},
            "strategy": {"selection_mode": "all_proved",
                         "execution_mode": "shares"},
            "research": {"enabled": True,
                         "require_validated_variant": True,
                         "strategy_llm": {"enabled": False}},
        }), encoding="utf-8")
    test_env = dict(os.environ, PYTHON=sys.executable,
                    ALPACA_RESEARCH_DATASET=str(dataset),
                    ALPACA_FACTORY_ENABLED="0",
                    ALPACA_EDGE_DB=str(root / "edge.sqlite3"),
                    ALPACA_AGENT_CONFIG=str(config),
                    ALPACA_DATA_FEED="iex", ALPACA_STOCK_FEED="iex",
                    ALPACA_OPTIONS_FEED="opra",
                    ALPACA_RESEARCH_LLM_SECRETS_FILE="/dev/null")
    test_env.update(env)
    return subprocess.run(
        ["deploy/research-cycle.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env=test_env,
        capture_output=True, text=True, check=False)


def _cycle_payloads(text: str, key: str) -> list[dict]:
    payloads = []
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and key in payload:
            payloads.append(payload)
    return payloads


_corpus_iterator = recorder.iter_corpus_rows


class _StockDataFake:
    def __init__(self):
        self.bar_request = None
        self.quote_request = None

    @staticmethod
    def _response():
        return SimpleNamespace(data={"SPY": [SimpleNamespace(
            timestamp=datetime(2026, 8, 8, 13, 30, tzinfo=timezone.utc),
            open=100, high=101, low=99, close=100.5, volume=10,
            bid_price=100, ask_price=101, last_price=100.5)]})

    def get_stock_bars(self, request):
        self.bar_request = request
        return self._response()

    def get_stock_quotes(self, request):
        self.quote_request = request
        return self._response()


class DeployTests(unittest.TestCase):
    def _calibration_normalizer(self, report, *, bootstrap=False,
                                journal_present=True):
        """Execute the research-cycle calibration normalizer in isolation."""
        script = Path("deploy/research-cycle.sh").read_text(encoding="utf-8")
        marker = 'normalized_report="$('
        start = script.index("<<'PY'\n", script.index(marker)) + len("<<'PY'\n")
        end = script.index("\nPY\n", start)
        body = script[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "calibration.json"
            journal = root / "journal.db"
            output.write_text(json.dumps(report), encoding="utf-8")
            if journal_present:
                journal.touch()
            result = subprocess.run(
                [sys.executable, "-", str(output), str(journal), "86400", "2",
                 "option", "1" if bootstrap else "0"],
                input=body, text=True, capture_output=True, check=False)
        return result

    def _run_update_compose(self, *, commit, dirty="", declared=None):
        """Run the VM helper against tiny fake git/docker/systemd commands."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            fake_bin = root / "bin"
            app.mkdir()
            fake_bin.mkdir()
            (app / "compose.yaml").write_text("services: {}\n",
                                               encoding="utf-8")
            secret = root / "agent.env"
            secret.write_text("PAPER=true\n", encoding="utf-8")
            docker_log = root / "docker.log"
            (fake_bin / "uname").write_text(
                "#!/bin/sh\nprintf '%s\\n' Linux\n", encoding="utf-8")
            (fake_bin / "systemctl").write_text(
                "#!/bin/sh\nexit 1\n", encoding="utf-8")
            (fake_bin / "git").write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = rev-parse ]; then\n"
                "  printf '%s\\n' \"$FAKE_GIT_COMMIT\"\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = status ]; then\n"
                "  if [ -n \"$FAKE_GIT_DIRTY\" ]; then\n"
                "    printf '%s\\n' \"$FAKE_GIT_DIRTY\"\n"
                "  fi\n"
                "  exit 0\n"
                "fi\n"
                "exit 1\n", encoding="utf-8")
            (fake_bin / "docker").write_text(
                "#!/bin/sh\n"
                "printf 'commit=%s args=%s\\n' \"$ALPACA_DEPLOYMENT_COMMIT\" \"$*\" >> \"$DOCKER_LOG\"\n"
                "exit 0\n", encoding="utf-8")
            for command in fake_bin.iterdir():
                command.chmod(0o755)
            environment = dict(os.environ)
            environment.update({
                "APP_DIR": str(app),
                "ALPACA_AGENT_SECRET_FILE": str(secret),
                "DOCKER_LOG": str(docker_log),
                "FAKE_GIT_COMMIT": commit,
                "FAKE_GIT_DIRTY": dirty,
                "PATH": str(fake_bin) + os.pathsep + environment.get("PATH", ""),
            })
            environment.pop("ALPACA_EXTERNAL_BACKUP_PATH", None)
            if declared is None:
                environment.pop("ALPACA_DEPLOYMENT_COMMIT", None)
            else:
                environment["ALPACA_DEPLOYMENT_COMMIT"] = declared
            script = (Path(__file__).resolve().parents[1] /
                      "deploy/update-compose.sh")
            result = subprocess.run(
                ["bash", str(script)], env=environment,
                capture_output=True, text=True, check=False)
            return result, docker_log.read_text(encoding="utf-8") \
                if docker_log.exists() else ""

    def test_compose_update_exports_clean_full_head_before_mutations(self):
        commit = "a" * 40
        result, log = self._run_update_compose(commit=commit)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = log.splitlines()
        self.assertGreaterEqual(len(lines), 7)
        self.assertIn("args=compose version", lines[0])
        for operation in ("config --quiet", "build trader", "run --rm",
                          "up -d", "exec -T", "ps"):
            with self.subTest(operation=operation):
                line = next(item for item in lines if operation in item)
                self.assertIn(f"commit={commit}", line)

    def test_compose_update_rejects_declared_commit_mismatch_before_compose(self):
        result, log = self._run_update_compose(
            commit="a" * 40, declared="b" * 40)
        self.assertEqual(result.returncode, 2)
        self.assertIn("disagrees with checked-out Git HEAD", result.stderr)
        self.assertEqual(log, "")
        self.assertNotIn("config --quiet", log)
        self.assertNotIn("build trader", log)

    def test_compose_update_rejects_dirty_checkout_before_compose(self):
        result, log = self._run_update_compose(
            commit="a" * 40, dirty=" M deploy/update-compose.sh")
        self.assertEqual(result.returncode, 2)
        self.assertIn("checkout", result.stderr)
        self.assertIn("dirty", result.stderr)
        self.assertEqual(log, "")
        self.assertNotIn("config --quiet", log)
        self.assertNotIn("build trader", log)

    def test_calibration_bootstrap_unknown_is_opt_in_and_fail_closed(self):
        empty = {"authorization_verdict": "insufficient_data",
                 "authorization_exit_code": 2, "journal_fills": 0,
                 "available_vehicles": []}
        denied = self._calibration_normalizer(empty)
        self.assertEqual(denied.returncode, 2)
        self.assertEqual(json.loads(denied.stdout)["calibration_status"], "blocked")

        bootstrap = self._calibration_normalizer(empty, bootstrap=True)
        self.assertEqual(bootstrap.returncode, 2)
        payload = json.loads(bootstrap.stdout)
        self.assertEqual(payload["calibration_status"], "bootstrap_unknown")
        self.assertEqual(payload["calibration_state"], "bootstrap_unknown")
        self.assertEqual(payload["authorization_exit_code"], 2)
        self.assertTrue(payload["bootstrap_unknown"])

        missing = self._calibration_normalizer(
            empty, bootstrap=True, journal_present=False)
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(json.loads(missing.stdout)["calibration_status"],
                         "bootstrap_unknown")

        for history in (
                {"journal_fills": 1, "available_vehicles": ["option"]},
                {"journal_fills": 0, "available_vehicles": ["equity"]},
                {"journal_fills": 3, "available_vehicles": ["equity", "option"]}):
            with self.subTest(history=history):
                report = {**empty, **history}
                result = self._calibration_normalizer(report, bootstrap=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stdout)["calibration_status"],
                                 "blocked")

    def test_research_cycle_missing_journal_bootstrap_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "data.csv"
            dataset.write_text(_replay_corpus_csv(quotes=True), encoding="utf-8")
            journal = root / "missing" / "journal.db"
            report_path = root / "calibration-%s.json"
            denied = _run_research_cycle(
                dataset, root / "off",
                ALPACA_RESEARCH_JOURNAL=str(journal),
                ALPACA_RESEARCH_CALIBRATION_REPORT=str(report_path),
                ALPACA_RESEARCH_CALIBRATION_BOOTSTRAP_UNKNOWN="0")
            self.assertIn("journal_unavailable", denied.stderr)

            allowed = _run_research_cycle(
                dataset, root / "on",
                ALPACA_RESEARCH_JOURNAL=str(journal),
                ALPACA_RESEARCH_CALIBRATION_REPORT=str(root / "on" /
                                                       "calibration-%s.json"),
                ALPACA_RESEARCH_CALIBRATION_BOOTSTRAP_UNKNOWN="1")
            report = root / "on" / "calibration-equity.json"
            self.assertTrue(report.is_file(), allowed.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["calibration_state"], "bootstrap_unknown")
            self.assertEqual(payload["authorization_exit_code"], 2)
            self.assertIn("bootstrap_unknown", allowed.stderr)

    def test_direct_health_probe_bootstraps_repo_import(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "deploy/health.py", "dashboard",
             "--url", "http://127.0.0.1:1"],
            cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["component"], "dashboard")

    def test_recorder_compose_service_receives_credentials_secret(self):
        text = Path("compose.yaml").read_text(encoding="utf-8")
        recorder = text.split("  trader:", 1)[0]
        self.assertIn("ALPACA_AGENT_SECRETS_FILE: /run/secrets/agent_credentials",
                      recorder)
        self.assertIn("source: agent_credentials", recorder)
        self.assertIn(
            "ALPACA_RECORDER_FETCH_WINDOW_MINUTES: ${ALPACA_RECORDER_FETCH_WINDOW_MINUTES:-1}",
            recorder)
        self.assertIn(
            "ALPACA_RECORDER_FORWARD_OBSERVATION_MAX_LAG_MINUTES: ${ALPACA_RECORDER_FORWARD_OBSERVATION_MAX_LAG_MINUTES:-15}",
            recorder)

    def test_recorder_fetch_window_default_and_override(self):
        with patch.dict(os.environ):
            os.environ.pop("ALPACA_RECORDER_FETCH_WINDOW_MINUTES", None)
            self.assertEqual(recorder._fetch_window_minutes(), 1)
        with patch.dict(os.environ, {
                "ALPACA_RECORDER_FETCH_WINDOW_MINUTES": "30"}):
            self.assertEqual(recorder._fetch_window_minutes(), 30)

    def test_recorder_forward_observation_lag_is_bounded_and_validated(self):
        name = "ALPACA_RECORDER_FORWARD_OBSERVATION_MAX_LAG_MINUTES"
        with patch.dict(os.environ):
            os.environ.pop(name, None)
            self.assertEqual(
                recorder._forward_observation_max_lag(), timedelta(minutes=15))
        with patch.dict(os.environ, {name: "30"}):
            self.assertEqual(
                recorder._forward_observation_max_lag(), timedelta(minutes=30))
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value), patch.dict(os.environ, {name: value}):
                with self.assertRaisesRegex(RuntimeError, "positive integer"):
                    recorder._forward_observation_max_lag()

    def test_research_service_has_no_broker_credentials(self):
        text = Path("compose.yaml").read_text(encoding="utf-8")
        research = text.split("  research:", 1)[1].split("  dashboard:", 1)[0]
        self.assertNotIn("ALPACA_AGENT_SECRETS_FILE", research)
        self.assertNotIn("agent_credentials", research)
        self.assertIn(
            "ALPACA_RESEARCH_LLM_SECRETS_FILE: /run/secrets/research_llm_credentials",
            research)
        self.assertIn("source: research_llm_credentials", research)
        self.assertNotIn("OPENAI_API_KEY:", research)
        self.assertNotIn("ANTHROPIC_API_KEY:", research)
        self.assertIn(
            "ALPACA_SHADOW_DB: ${ALPACA_SHADOW_DB:-/app/shadow/shadow.sqlite3}",
            research)
        self.assertIn(
            "ALPACA_FACTORY_WORKERS: ${ALPACA_FACTORY_WORKERS:-2}",
            research)
        self.assertIn(
            "ALPACA_FACTORY_STRATEGIES: ${ALPACA_FACTORY_STRATEGIES:-12}",
            research)
        self.assertIn(
            "ALPACA_RESEARCH_IMMUTABLE_SOURCE_IDENTITY: ${ALPACA_RESEARCH_IMMUTABLE_SOURCE_IDENTITY:-}",
            research)
        self.assertIn(
            "ALPACA_FACTORY_DIAGNOSTIC_ONLY: ${ALPACA_FACTORY_DIAGNOSTIC_ONLY:-0}",
            research)
        self.assertIn(
            "mem_limit: ${ALPACA_RESEARCH_MEMORY_LIMIT:-10g}",
            research)
        self.assertIn("- shadow-data:/app/shadow:ro", research)
        shadow = text.split("  shadow:", 1)[1].split("  dashboard:", 1)[0]
        self.assertIn("- shadow-data:/app/shadow", shadow)
        self.assertIn("- /app/runtime/research/recorded/market.csv", shadow)
        self.assertNotIn("/app/runtime/research/recorded/data.csv", shadow)
        self.assertIn("- /app/shadow/shadow.sqlite3", shadow)
        self.assertIn("depends_on:", shadow)
        self.assertIn("shadow-init:", shadow)
        self.assertIn("--health-file", shadow)
        self.assertIn("deploy/health.py", shadow)
        self.assertIn('"shadow"', shadow)
        shadow_init = text.split("  shadow-init:", 1)[1].split("  shadow:", 1)[0]
        self.assertIn('user: "0:0"', shadow_init)
        self.assertIn("chown 10001:10001 /app/shadow", shadow_init)
        unit = Path("deploy/alpaca-research.service").read_text(encoding="utf-8")
        self.assertNotIn("ALPACA_AGENT_SECRETS_FILE", unit)
        self.assertNotIn("agent.env", unit)
        self.assertIn("research.env", unit)

    def test_scheduler_default_points_to_deploy_cycle(self):
        self.assertEqual(scheduler.build_parser().parse_args([]).script,
                         "deploy/research-cycle.sh")

    def test_scheduler_output_facade_reexports_moved_symbols(self):
        names = (
            "_BoundedCapture", "structured_failure",
            "structured_research_cycle", "_drain", "_start_capture",
            "_capture_detail",
        )
        for name in names:
            self.assertIs(getattr(scheduler, name),
                          getattr(scheduler_output, name))

    def test_scheduler_output_bounded_capture_and_structured_results(self):
        capture = scheduler_output._BoundedCapture(8)
        capture.feed("prefix\n")
        capture.feed(json.dumps({
            "generation": 2, "accepted_count": 0, "status": "FAILED",
            "attempt_id": "attempt-1", "error": "author failed",
        }) + "\n")
        cycle_line = json.dumps({
            "schema": "research-cycle.v1", "status": "completed_no_edge",
            "reason": "no eligible edge", "exit_code": 0,
            "outcomes": ["equity", "option"], "proofs": True,
        }) + "\n"
        capture.feed(cycle_line)
        self.assertEqual(capture.tail, cycle_line[-8:])
        self.assertTrue(capture.truncated)
        self.assertEqual(capture.structured_failures, [{
            "component": "authoring", "status": "FAILED",
            "attempt_id": "attempt-1", "error": "author failed",
        }])
        self.assertEqual(capture.research_cycles, [{
            "schema": "research-cycle.v1", "status": "completed_no_edge",
            "reason": "no eligible edge", "exit_code": 0,
            "outcomes": ["equity", "option"], "proofs": True,
            "no_edge": False,
        }])

    def test_scheduler_output_preserves_factory_observability_blocks(self):
        payload = scheduler_output.structured_research_cycle({
            "schema": "research-cycle.v1", "status": "completed_no_edge",
            "reason": "diagnostic", "exit_code": 0,
            "research_funnel": {
                "schema": "research-funnel.v1", "diagnostic_only": True,
                "authorizing": False,
                "counts": {"opportunities": 4, "admitted": 2, "executed": 2,
                           "authorizing_eligible": 2, "gated": 2,
                           "selected": 0},
                "no_signal": 1, "refused": 2,
                "dominant_refusal_reason": "no_quote_at_entry",
            },
            "research_verdict": {
                "schema": "research-verdict.v1", "diagnostic_only": True,
                "authorizing": False, "status": "evidence_available",
                "effect_estimate": .1, "confidence_interval": {"lower": .01},
            },
            "cost_diagnostic": {
                "schema": "cost-rerun-diagnostic.v1", "diagnostic_only": True,
                "authorizing": False, "status": "completed",
                "report_path": "/tmp/cost.json",
                "delta": {"round_trip_bps_delta": 1.5},
            },
        })
        self.assertEqual(payload["research_funnel"]["counts"]["opportunities"], 4)
        self.assertEqual(payload["research_funnel"]["dominant_refusal_reason"],
                         "no_quote_at_entry")
        self.assertEqual(payload["research_verdict"]["effect_estimate"], .1)
        self.assertEqual(payload["cost_diagnostic"]["delta"]["round_trip_bps_delta"], 1.5)

    def test_scheduler_output_capture_detail_combines_streams(self):
        stdout = scheduler_output._BoundedCapture(4)
        stderr = scheduler_output._BoundedCapture(200)
        stdout.feed("abcdef")
        stderr.feed(json.dumps({
            "max_reviews": 2, "reviewed": 2, "retry_pending": 1,
            "failed": 0, "status": "RETRY_PENDING",
        }) + "\n")
        detail = scheduler_output._capture_detail(stdout, stderr)
        self.assertEqual(detail["stdout_tail"], "cdef")
        self.assertEqual(detail["stdout_chars"], 6)
        self.assertTrue(detail["stdout_truncated"])
        self.assertEqual(detail["stderr_chars"], len(stderr.tail))
        self.assertFalse(detail["stderr_truncated"])
        self.assertEqual(detail["structured_failures"], [{
            "component": "review", "status": "RETRY_PENDING",
            "reviewed": 2, "retry_pending": 1, "failed": 0,
        }])

    def test_scheduler_persists_bounded_provider_preflight_in_cycle(self):
        preflight = {
            "schema": "research-llm-preflight.v1", "status": "degraded",
            "reason": "transient provider failure",
            "evidence": {"error": "token=<redacted>",
                         "calls_used": 1,
                         "raw": {"secret": "must not cross"}},
        }
        payload = scheduler_output.structured_research_cycle({
            "schema": "research-cycle.v1", "status": "completed_no_edge",
            "reason": "fallback", "exit_code": 0, "preflight": preflight,
        })
        self.assertEqual(payload["preflight"]["status"], "degraded")
        self.assertNotIn("raw", payload["preflight"]["evidence"])
        capture = scheduler_output._BoundedCapture(4096)
        capture.feed(json.dumps({
            "schema": "research-llm-preflight.v1", "status": "degraded",
            "reason": "transient", "evidence": {"error": "safe"},
        }) + "\n")
        self.assertEqual(
            scheduler_output._capture_detail(capture, None)["research_preflight"]["status"],
            "degraded")

    def test_scheduler_preflight_drops_unknown_and_redacts_adversarial_evidence(self):
        payload = scheduler_output.structured_research_preflight({
            "schema": "research-llm-preflight.v1", "status": "fatal",
            "reason": "X-Amz-Signature=reason-secret",
            "evidence": {
                "api_key": "api-secret", "authorization": "Basic secret",
                "X-Amz-Signature": "signed-secret",
                "error": "https://provider.test/?token=query-secret",
                "raw_response": "should be dropped",
            },
        })
        serialized = json.dumps(payload)
        for secret in ("reason-secret", "api-secret", "signed-secret",
                       "query-secret", "should be dropped"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(set(payload["evidence"]), {"error"})

    def test_research_cycle_factory_status_contract_is_explicit(self):
        script = Path("deploy/research-cycle.sh").read_text(encoding="utf-8")
        self.assertIn(
            '--max-confirmatory-attempts "${ALPACA_FACTORY_MAX_CONFIRMATORY_ATTEMPTS:-3}"',
            script)
        self.assertIn('cycle_outcomes+=("$vehicle:factory:search_exhausted")',
                      script)
        self.assertIn('cycle_outcomes+=("$vehicle:factory:llm_provider_failure")',
                      script)
        self.assertIn('finish "search_exhausted"', script)
        self.assertIn('finish "llm_provider_failure"', script)
        self.assertIn("execution-blocked", script)
        self.assertIn("qualification-unavailable", script)
        self.assertIn('finish "completed_no_edge"', script)
        self.assertIn(
            '(cd "$repo_root" && "$python_bin" -m research.cost_rerun '
            '--calibration-only', script)
        self.assertNotIn(
            '"$python_bin" "$repo_root/research/cost_rerun.py" '
            '--calibration-only', script)
        self.assertIn('ALPACA_RESEARCH_STRESS_CALIBRATION_REPORT', script)

        for status in ("search_exhausted", "llm_provider_failure"):
            with self.subTest(status=status):
                payload = scheduler_output.structured_research_cycle({
                    "schema": "research-cycle.v1", "status": status,
                    "reason": "factory diagnostic", "exit_code": 0,
                    "outcomes": [f"equity:factory:{status}"],
                    "proofs": False, "no_edge": False,
                })
                self.assertIsNotNone(payload)
                self.assertEqual(payload["status"], status)

    def test_research_cycle_llm_preflight_fails_before_dataset_work(self):
        script = Path("deploy/research-cycle.sh").read_text(encoding="utf-8")
        preflight = script.index('research.py" llm-preflight')
        vehicles = script.index('research.py" vehicles')
        dataset = script.index('dataset="${ALPACA_RESEARCH_DATASET:-}"')
        self.assertLess(preflight, vehicles)
        self.assertLess(preflight, dataset)
        fatal = script.index('finish "failed" "strategy LLM preflight fatal')
        self.assertLess(fatal, vehicles)
        self.assertIn('research-llm-preflight-warning.v1', script)

    def test_systemd_research_cache_topology_is_explicit_and_preflighted(self):
        unit = Path("deploy/alpaca-research.service").read_text(
            encoding="utf-8")
        env = Path("deploy/research.env.example").read_text(encoding="utf-8")
        self.assertIn(
            "Environment=TMPDIR=/opt/alpaca-agent-trading/research/cache/tmp",
            unit)
        self.assertIn(
            "ALPACA_RESEARCH_PREPROCESSING_CACHE_ROOT=/opt/alpaca-agent-trading/research/cache/preprocessing",
            env)
        self.assertIn(
            "TMPDIR=/opt/alpaca-agent-trading/research/cache/tmp", env)
        self.assertNotIn("/app/", env)
        self.assertIn(
            "ReadWritePaths=/opt/alpaca-agent-trading/runtime /opt/alpaca-agent-trading/research/cache /opt/alpaca-agent-trading/research/results",
            unit)

        script = Path("deploy/research-cycle.sh").read_text(encoding="utf-8")
        temp_parent = script.index('mkdir -p "$tmp_root"')
        temp_create = script.index('mktemp -d "$tmp_root/')
        self.assertLess(temp_parent, temp_create)
        guard = script.index("cache_topology_preflight()")
        preprocessing = script.index('research_dataset.py"')
        self.assertLess(guard, preprocessing)
        self.assertIn('research_cache.py" topology', script)

    def test_scheduler_persists_explicit_factory_terminal_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text("mode: paper\nbroker:\n  paper: true\n",
                              encoding="utf-8")
            script = root / "cycle.sh"
            script.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$CYCLE_RESULT\"\n"
                "exit \"${CYCLE_EXIT:-0}\"\n", encoding="utf-8")
            script.chmod(0o755)
            status_file = root / "health.json"
            for status in ("search_exhausted", "llm_provider_failure"):
                scheduler._running = True
                env = dict(os.environ,
                           CYCLE_RESULT=json.dumps({
                               "schema": "research-cycle.v1",
                               "status": status,
                               "reason": "factory diagnostic",
                               "exit_code": 0,
                               "outcomes": [f"equity:factory:{status}"],
                               "proofs": False, "no_edge": False,
                           }), CYCLE_EXIT="0")
                with patch.dict(os.environ, env, clear=True):
                    args = SimpleNamespace(
                        status_file=str(status_file), config=str(config),
                        script=str(script), root=str(root), hour=3, minute=0,
                        once=True, timeout_seconds=10,
                        output_limit_chars=4096)
                    self.assertEqual(scheduler.run_scheduler(args), 0)
                payload = json.loads(status_file.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], status)
                self.assertEqual(payload["cycle_status"], status)
                self.assertEqual(payload["research_cycle"]["status"], status)

    def test_scheduler_output_start_capture_drains_text_stream(self):
        stream = io.StringIO("line one\nline two\n")
        result = scheduler_output._start_capture(stream, 100)
        self.assertIsNotNone(result)
        capture, thread = result
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(capture.total_chars, len("line one\nline two\n"))
        self.assertEqual(capture.tail, "line one\nline two\n")

    def test_recorder_facade_reexports_market_symbols(self):
        from deploy import recorder_market
        names = (
            "FIELDS", "_value", "_timeframe", "_feed", "_options_feed",
            "_call_market_data", "_call_quotes", "_call_options", "_event_key",
            "_number", "_iso", "_point_in_time", "_timestamp",
            "_underlying_price", "_option_right", "_option_rank", "_option_rows",
            "_rows",
        )
        for name in names:
            self.assertIs(getattr(recorder, name), getattr(recorder_market, name))

    def test_recorder_migrates_legacy_header_before_deduplicating(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            path.write_text(
                "event_type,symbol,timestamp\n"
                "bar_1m,SPY,2026-08-08T13:30:00+00:00\n",
                encoding="utf-8")
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 1)
            partitions = recorder.corpus_partitions(path)
            self.assertEqual([item.name for item in partitions],
                             ["market-2026-08-08.csv"])
            with partitions[0].open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(list(rows[0]), list(recorder.FIELDS))
            self.assertFalse(path.exists())
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 0)

    def test_research_cycle_uses_recorded_csv_and_initializes_edge_ledger(self):
        csv_text = (
            "event_key,observed_at,provider,feed,event_type,symbol,timestamp,"
            "open,high,low,close,volume,bid,ask,last\n"
            "k,2026-08-08T13:31:00+00:00,alpaca,iex,bar_1m,SPY,"
            "2026-08-08T13:30:00+00:00,100,101,99,100.5,10,,,,\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            dataset.write_text(csv_text, encoding="utf-8")
            edge_db = root / "edge.sqlite3"
            result = _run_research_cycle(dataset, root)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('"valid": true', result.stdout)
            self.assertTrue(edge_db.is_file())
            with closing(sqlite3.connect(edge_db)) as db:
                self.assertGreater(db.execute(
                    "SELECT COUNT(*) FROM candidates").fetchone()[0], 0)

    def test_research_cycle_reuses_only_explicitly_identified_preprocessing(self):
        csv_text = (
            "event_key,observed_at,provider,feed,event_type,symbol,timestamp,as_of,"
            "open,high,low,close,volume\n"
            "k,2026-08-08T13:31:00+00:00,alpaca,iex,bar_1m,SPY,"
            "2026-08-08T13:30:00+00:00,2026-08-08T13:31:00+00:00,"
            "100,101,99,100.5,10\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            cache = root / "preprocessing-cache"
            dataset.write_text(csv_text, encoding="utf-8")
            cache_env = {
                "ALPACA_RESEARCH_IMMUTABLE_SOURCE_IDENTITY":
                    "sha256:" + "a" * 64,
                "ALPACA_RESEARCH_PREPROCESSING_CACHE_ROOT": str(cache),
                "ALPACA_RESEARCH_BACKTEST": "0",
            }

            first = _run_research_cycle(dataset, root / "first", **cache_env)
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            first_cache = [
                item for item in _cycle_payloads(first.stderr, "schema")
                if item.get("schema") ==
                "research-preprocessing-cache-result.v1"]
            self.assertEqual([item["status"] for item in first_cache],
                             ["miss", "published"])

            second = _run_research_cycle(dataset, root / "first", **cache_env)
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            second_cache = [
                item for item in _cycle_payloads(second.stderr, "schema")
                if item.get("schema") ==
                "research-preprocessing-cache-result.v1"]
            self.assertEqual([item["status"] for item in second_cache], ["hit"])
            self.assertTrue(second_cache[0]["hit"])
            views = [item for item in _cycle_payloads(second.stderr, "schema")
                     if item.get("schema") == "research-cycle-views.v1"]
            self.assertEqual(views[0]["bars"], 1)

    def test_research_cycle_quarantines_legacy_observation_inversions(self):
        csv_text = (
            "event_key,observed_at,provider,feed,event_type,symbol,timestamp,as_of,"
            "open,high,low,close,volume,bid,ask,bid_size,ask_size\n"
            "bar,2026-08-08T13:31:00+00:00,alpaca,iex,bar_1m,SPY,"
            "2026-08-08T13:30:00+00:00,2026-08-08T13:31:00+00:00,"
            "100,101,99,100.5,10,,,,\n"
            "quote,2026-08-08T13:31:00+00:00,alpaca,iex,quote,SPY,"
            "2026-08-08T13:31:00+00:00,2026-08-08T13:32:00+00:00,"
            ",,,,,100,101,10,10\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            dataset.write_text(csv_text, encoding="utf-8")
            result = _run_research_cycle(dataset, root)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            reports = [item for item in _cycle_payloads(result.stderr, "schema")
                       if item["schema"] == "research-cycle-quarantine.v1"]
            self.assertEqual(reports, [{
                "by_kind": {"quote": 1}, "first_source_row": 3,
                "kept_rows": 1, "last_source_row": 3,
                "reason": "as_of_after_observed_at", "rows": 1,
                "schema": "research-cycle-quarantine.v1",
                "status": "quarantined",
            }])
            views = [item for item in _cycle_payloads(result.stderr, "schema")
                     if item["schema"] == "research-cycle-views.v1"][0]
            self.assertEqual((views["bars"], views["quotes"], views["options"]),
                             (1, 0, 0))
            self.assertEqual(views["replay"], 1)
            validation = [item for item in _cycle_payloads(result.stdout, "valid")][0]
            self.assertTrue(validation["valid"])
            # The append-only source remains evidence, including the quarantined
            # row; only the temporary research view is filtered.
            self.assertEqual(dataset.read_text(encoding="utf-8"), csv_text)

    def test_research_cycle_keeps_other_integrity_errors_fail_closed(self):
        csv_text = (
            "event_key,observed_at,provider,feed,event_type,symbol,timestamp,as_of,"
            "open,high,low,close,volume\n"
            "bad,2026-08-08T13:31:00+00:00,alpaca,iex,bar_1m,SPY/QQQ,"
            "2026-08-08T13:30:00+00:00,2026-08-08T13:31:00+00:00,"
            "100,101,99,100.5,10\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            dataset.write_text(csv_text, encoding="utf-8")
            result = _run_research_cycle(dataset, root)
            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            terminal = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(terminal["status"], "failed")
            self.assertEqual(terminal["reason"],
                             "research dataset validation failed")
            validation = [item for item in _cycle_payloads(result.stdout, "valid")][0]
            self.assertFalse(validation["valid"])
            self.assertIn("slash pair", validation["errors"][0])

    def test_research_cycle_rejects_external_sip_equity_provenance(self):
        """A SIP row cannot be promoted under the shipped IEX configuration."""
        csv_text = (
            "event_key,observed_at,provider,feed,event_type,symbol,timestamp,as_of,"
            "open,high,low,close,volume\n"
            "sip,2026-08-08T13:31:00+00:00,alpaca,sip,bar_1m,SPY,"
            "2026-08-08T13:30:00+00:00,2026-08-08T13:31:00+00:00,"
            "100,101,99,100.5,10\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            dataset.write_text(csv_text, encoding="utf-8")
            result = _run_research_cycle(dataset, root)
            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            validation = [item for item in _cycle_payloads(result.stdout, "valid")][0]
            self.assertFalse(validation["valid"])
            self.assertTrue(any(("configured executable feed 'iex'" in error or
                                "expected 'iex'" in error)
                                for error in validation["errors"]))

    def test_research_cycle_accepts_sip_when_configured_end_to_end(self):
        """Configured SIP must survive feed guard and corpus validation."""
        csv_text = (
            "event_key,observed_at,provider,feed,event_type,symbol,timestamp,as_of,"
            "open,high,low,close,volume\n"
            "sip,2026-08-08T13:31:00+00:00,alpaca,sip,bar_1m,SPY,"
            "2026-08-08T13:30:00+00:00,2026-08-08T13:31:00+00:00,"
            "100,101,99,100.5,10\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            config = root / "sip-config.json"
            dataset.write_text(csv_text, encoding="utf-8")
            config.write_text(json.dumps({
                "mode": "paper",
                "broker": {"paper": True, "allow_live": False,
                           "data_feed": "sip", "options_feed": "opra"},
                "universe": {"asset_classes": ["us_equity"]},
                "session": {"require_exact_calendar": False},
                "strategy": {"selection_mode": "all_proved",
                             "execution_mode": "shares"},
                "research": {"enabled": True,
                             "require_validated_variant": True,
                             "strategy_llm": {"enabled": False}},
            }), encoding="utf-8")
            result = _run_research_cycle(
                dataset, root, ALPACA_AGENT_CONFIG=str(config),
                ALPACA_DATA_FEED="sip", ALPACA_STOCK_FEED="sip",
                ALPACA_OPTIONS_FEED="opra")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            validation = [item for item in _cycle_payloads(result.stdout, "valid")][0]
            self.assertTrue(validation["valid"])

    def test_research_cycle_routes_recorded_quotes_into_the_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summaries = {}
            for label in ("quotes", "bars_only"):
                dataset = root / f"{label}.csv"
                dataset.write_text(_replay_corpus_csv(quotes=label == "quotes"),
                                   encoding="utf-8")
                result = _run_research_cycle(dataset, root / label)
                self.assertEqual(result.returncode, 0, result.stderr)
                views = _cycle_payloads(result.stderr, "schema")
                views = [item for item in views
                         if item["schema"] == "research-cycle-views.v1"][0]
                self.assertEqual(views["quotes"], 60 if label == "quotes" else 0)
                self.assertEqual(views["bars"], 60)
                self.assertEqual(views["replay"], 60)
                summaries[label] = _cycle_payloads(result.stdout, "vehicle")[0]
            # The routed quotes are the executable price at the fill instant,
            # so a corpus carrying them must not replay like the bars alone.
            # Historical research may explicitly use the conservative bar
            # fallback when quotes are absent, but its fill economics must
            # remain distinguishable from recorded point-in-time quotes.
            self.assertEqual(summaries["quotes"]["trades"], 1)
            self.assertEqual(summaries["bars_only"]["trades"], 1)
            self.assertNotEqual(summaries["quotes"]["net_pnl"],
                                summaries["bars_only"]["net_pnl"])

    def test_research_cycle_reads_a_partitioned_corpus_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "recorded"
            path = corpus / "market.csv"
            _write_flat_corpus(path, _corpus_rows(sessions=4, per_session=3))
            recorder.migrate_corpus(path)
            result = _run_research_cycle(
                "", root, ALPACA_RECORDED_DATASET_ROOT=str(corpus),
                ALPACA_RESEARCH_SESSION_WINDOW="2")
            self.assertEqual(result.returncode, 0, result.stderr)
            views = [item for item in _cycle_payloads(result.stderr, "schema")
                     if item["schema"] == "research-cycle-views.v1"][0]
            self.assertEqual(views["bars"], 6)  # two sessions, not twelve
            self.assertEqual(views["replay"], 6)

    def test_research_cycle_reports_no_data_as_structured_nonzero_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "empty.jsonl"
            dataset.write_text("\n", encoding="utf-8")
            result = _run_research_cycle(dataset, root)
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["schema"], "research-cycle.v1")
            self.assertEqual(payload["status"], "no_data")
            self.assertEqual(payload["exit_code"], 2)
            self.assertIn('"schema":"research-llm.v1"', result.stderr)
            self.assertIn('"status":"disabled"', result.stderr)

    def test_research_cycle_emits_forward_readiness_after_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            dataset.write_text(_replay_corpus_csv(quotes=True, sessions=2),
                               encoding="utf-8")
            result = _run_research_cycle(dataset, root,
                                         ALPACA_RESEARCH_BACKTEST="0")
        readiness = [item for item in _cycle_payloads(result.stderr, "schema")
                     if item.get("schema") == "research-readiness.v1"]
        self.assertTrue(readiness, result.stderr)
        event = readiness[0]
        self.assertEqual(event["state"], "pending")
        self.assertEqual(event["recorded_sessions"], 2)
        self.assertEqual(event["shadow_min_sessions"], 60)
        self.assertAlmostEqual(event["heldout_fraction"], .24)
        self.assertEqual(event["offline_required_sessions"], 150)
        self.assertEqual(event["required_sessions"], 210)
        self.assertGreater(event["sessions_remaining"], 0)
        self.assertIn("candidate-specific shadow proof", event["reason"])

    def test_research_cycle_fails_fast_when_enabled_llm_has_no_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({
                "mode": "paper",
                "broker": {"paper": True, "allow_live": False},
                "research": {"strategy_llm": {
                    "enabled": True, "provider": "openai", "model": "gpt-5",
                }},
            }), encoding="utf-8")
            dataset = root / "empty.jsonl"
            dataset.write_text("\n", encoding="utf-8")
            env = dict(
                os.environ, PYTHON=sys.executable,
                ALPACA_AGENT_CONFIG=str(config),
                ALPACA_RESEARCH_DATASET=str(dataset),
                ALPACA_RESEARCH_LLM_SECRETS_FILE="/dev/null")
            env.pop("OPENAI_API_KEY", None)
            result = subprocess.run(
                ["deploy/research-cycle.sh"],
                cwd=Path(__file__).resolve().parents[1], env=env,
                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 3, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "failed")
        self.assertIn("OPENAI_API_KEY is unavailable", payload["reason"])

    def test_scheduler_preserves_research_cycle_statuses_in_health_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text("mode: paper\nbroker:\n  paper: true\n",
                              encoding="utf-8")
            script = root / "cycle.sh"
            script.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$CYCLE_RESULT\"\n"
                "exit \"${CYCLE_EXIT:-0}\"\n", encoding="utf-8")
            script.chmod(0o755)
            status = root / "health.json"
            for expected, exit_code in (("completed_no_edge", 0),
                                        ("completed_no_edge", 2),
                                        ("no_data", 2)):
                scheduler._running = True
                env = dict(os.environ, CYCLE_RESULT=json.dumps({
                    "schema": "research-cycle.v1", "status": expected,
                    "reason": "test", "exit_code": exit_code,
                }), CYCLE_EXIT=str(exit_code))
                with patch.dict(os.environ, env, clear=True):
                    args = SimpleNamespace(
                        status_file=str(status), config=str(config),
                        script=str(script), root=str(root), hour=3, minute=0,
                        once=True, timeout_seconds=10,
                        output_limit_chars=4096)
                    expected_exit = (0 if expected == "completed_no_edge"
                                     else exit_code)
                    self.assertEqual(scheduler.run_scheduler(args), expected_exit)
                payload = json.loads(status.read_text(encoding="utf-8"))
                self.assertEqual(payload["status"], expected)
                self.assertEqual(payload["cycle_status"], expected)
                self.assertEqual(payload["research_cycle"]["status"], expected)

    def test_research_health_distinguishes_no_edge_from_no_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            now = datetime.now(timezone.utc).timestamp()
            path.write_text(json.dumps({
                "status": "completed_no_edge", "updated_ts": now,
                "last_exit_code": 0,
                "research_readiness": {
                    "schema": "research-readiness.v1", "state": "pending",
                    "recorded_sessions": 1, "required_sessions": 2,
                    "sessions_remaining": 1,
                },
            }), encoding="utf-8")
            self.assertTrue(health.research(path, 60, now=now)["ok"])
            path.write_text(json.dumps({
                "status": "no_data", "updated_ts": now,
                "last_exit_code": 2}), encoding="utf-8")
            degraded = health.research(path, 60, now=now)
            self.assertTrue(degraded["ok"])
            self.assertEqual(degraded["research_status"], "degraded")
            self.assertFalse(degraded["evidence_available"])

    def test_research_health_exposes_transient_provider_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            now = datetime.now(timezone.utc).timestamp()
            path.write_text(json.dumps({
                "status": "completed_no_edge", "updated_ts": now,
                "last_exit_code": 0,
                "research_preflight": {
                    "schema": "research-llm-preflight.v1",
                    "status": "degraded", "reason": "transient",
                    "evidence": {"error": "safe"},
                },
            }), encoding="utf-8")
            result = health.research(path, 60, now=now)
        self.assertEqual(result["research_preflight"]["status"], "degraded")
        self.assertTrue(result["provider_preflight_degraded"])

    def test_running_research_exposes_scheduler_liveness_but_fails_prior_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": 100,
                "deadline_ts": 200, "last_exit_code": 2,
            }), encoding="utf-8")
            result = health.research(path, 60, now=100)
        self.assertTrue(result["ok"])
        self.assertTrue(result["scheduler_liveness"]["ok"])
        self.assertTrue(result["previous_cycle_degraded"])
        self.assertEqual(result["research_status"], "degraded")
        self.assertEqual(result["last_exit_code"], 2)

    def test_waiting_scheduler_exposes_liveness_but_fails_prior_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            path.write_text(json.dumps({
                "status": "waiting", "updated_ts": 100,
                "last_exit_code": 2, "cycle_status": "no_data",
                "next_run_ts": 200,
            }), encoding="utf-8")
            result = health.research(path, 60, now=100)
            self.assertTrue(result["ok"])
            self.assertTrue(result["scheduler_liveness"]["ok"])
            self.assertTrue(result["waiting_after_no_data"])
            self.assertTrue(result["previous_cycle_degraded"])
            self.assertEqual(result["research_status"], "degraded")

            path.write_text(json.dumps({
                "status": "waiting", "updated_ts": 100,
                "last_exit_code": 1, "cycle_status": "failed",
                "next_run_ts": 200,
            }), encoding="utf-8")
            result = health.research(path, 60, now=100)
        self.assertTrue(result["ok"])
        self.assertTrue(result["scheduler_liveness"]["ok"])
        self.assertFalse(result["waiting_after_no_data"])
        self.assertTrue(result["previous_cycle_degraded"])
        self.assertEqual(result["research_status"], "degraded")

    def test_waiting_scheduler_before_first_cycle_is_process_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.json"
            path.write_text(json.dumps({
                "status": "waiting", "updated_ts": 100,
                "next_run_ts": 200,
            }), encoding="utf-8")
            result = health.research(path, 60, now=100)
        self.assertTrue(result["ok"])
        self.assertTrue(result["scheduler_liveness"]["ok"])
        self.assertEqual(result["research_status"], "degraded")
        self.assertEqual(result["reason"],
                         "research scheduler waiting for first cycle")

    def test_shadow_health_requires_a_fresh_successful_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": 100,
                "candidates": 2, "events": 10, "decisions": 3,
            }), encoding="utf-8")
            self.assertTrue(health.shadow(path, 60, now=100)["ok"])
            path.write_text(json.dumps({
                "status": "degraded", "updated_ts": 100,
                "last_error": "ShadowError: test",
            }), encoding="utf-8")
            degraded = health.shadow(path, 60, now=100)
        self.assertFalse(degraded["ok"])
        self.assertEqual(degraded["last_error"], "ShadowError: test")

    def test_shadow_once_publishes_an_atomic_health_record(self):
        class Runner:
            def __init__(self, _config):
                pass

            def run_once(self):
                return {"candidates": 1, "events": 4, "decisions": 2,
                        "ingested_events": 1,
                        "quarantine_through_session": "2026-08-14"}

        with tempfile.TemporaryDirectory() as directory, patch.object(
                shadow_service, "ShadowRunner", Runner):
            health_path = Path(directory) / "health.json"
            code = shadow_service.main([
                "--once", "--shadow-db", str(Path(directory) / "shadow.db"),
                "--health-file", str(health_path)])
            payload = json.loads(health_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], "shadow-health.v1")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["ingested_events"], 1)

    def test_research_cycle_sends_recorded_options_to_autonomous_discovery(self):
        expiry = (datetime.now(timezone.utc).date() + timedelta(days=30)).isoformat()
        contract_symbol = f"SPY{datetime.fromisoformat(expiry):%y%m%d}C00100000"
        fields = list(recorder.FIELDS)
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "event_key": "bar", "observed_at": "2026-08-08T13:31:00+00:00",
            "provider": "alpaca", "feed": "iex", "event_type": "bar_1m",
            "symbol": "SPY", "timestamp": "2026-08-08T13:30:00+00:00",
            "as_of": "2026-08-08T13:30:00+00:00", "open": 100, "high": 101,
            "low": 99, "close": 100.5, "volume": 10,
        })
        writer.writerow({
            "event_key": "option", "observed_at": "2026-08-08T13:31:00+00:00",
            "provider": "alpaca", "feed": "opra",
            "event_type": "option_snapshot", "symbol": contract_symbol,
            "contract": contract_symbol,
            "timestamp": "2026-08-08T13:30:02+00:00",
            "as_of": "2026-08-08T13:30:02+00:00", "volume": 100, "bid": 1,
            "ask": 1.1, "underlying": "SPY", "expiration": expiry,
            "strike": 100, "right": "call", "multiplier": 100,
            "bid_size": 10, "ask_size": 11, "open_interest": 200,
            "underlying_price": 100,
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            edge_db = root / "edge.sqlite3"
            dataset.write_text(csv_buffer.getvalue(), encoding="utf-8")
            # The checked config trades shares, so the option lane runs only
            # when it is asked for; a trader cannot deploy option evidence.
            result = _run_research_cycle(
                dataset, root, ALPACA_RESEARCH_VEHICLES="all",
                ALPACA_FACTORY_ENABLED="1")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('"vehicle": "equity"', result.stdout)
            self.assertIn('"vehicle": "option"', result.stdout)
            self.assertTrue(edge_db.is_file())
            with closing(sqlite3.connect(edge_db)) as db:
                vehicles = dict(db.execute(
                    "SELECT vehicle, COUNT(*) FROM factory_hypotheses GROUP BY vehicle"
                ).fetchall())
            self.assertEqual(vehicles, {"equity": 12, "option": 12})

    def test_research_cycle_studies_equity_only_by_default(self):
        """The scheduled profile stays on the runtime's equity lane by default."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.jsonl"
            edge_db = root / "edge.sqlite3"
            dataset.write_text("\n".join(json.dumps(row) for row in (
                {"kind": "bar", "provider": "alpaca", "feed": "iex",
                 "symbol": "SPY", "timestamp": "2026-08-08T13:30:00+00:00",
                 "as_of": "2026-08-08T13:30:00+00:00",
                 "observed_at": "2026-08-08T13:31:00+00:00",
                 "open": 100, "high": 101, "low": 99, "close": 100.5,
                 "volume": 10},
                {"kind": "option_snapshot", "provider": "alpaca",
                 "feed": "opra", "symbol": "SPY260918C00100000",
                 "contract": "SPY260918C00100000",
                 "timestamp": "2026-08-08T13:30:02+00:00",
                 "as_of": "2026-08-08T13:30:02+00:00",
                 "observed_at": "2026-08-08T13:31:00+00:00",
                 "underlying": "SPY", "expiration": "2026-09-18", "strike": 100,
                 "right": "call", "multiplier": 100, "bid": 1, "ask": 1.1,
                 "bid_size": 10, "ask_size": 11, "volume": 100,
                 "open_interest": 200, "underlying_price": 100},
            )) + "\n", encoding="utf-8")
            result = _run_research_cycle(dataset, root,
                                         ALPACA_FACTORY_ENABLED="1")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            with closing(sqlite3.connect(edge_db)) as db:
                vehicles = dict(db.execute(
                    "SELECT vehicle, COUNT(*) FROM factory_hypotheses GROUP BY vehicle"
                ).fetchall())
            self.assertEqual(vehicles, {"equity": 12})

    def test_research_cycle_equity_only_filters_indicative_options(self):
        """A mixed indicative corpus remains usable for the equity lane."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "research-test-config.json"
            config.write_text(json.dumps({
                "mode": "paper",
                "broker": {"paper": True, "allow_live": False,
                           "data_feed": "iex", "options_feed": "indicative"},
                "universe": {"asset_classes": ["us_equity"]},
                "session": {"require_exact_calendar": False},
                "strategy": {"selection_mode": "all_proved",
                             "execution_mode": "shares"},
                "research": {"enabled": True,
                             "require_validated_variant": True,
                             "strategy_llm": {"enabled": False}},
            }), encoding="utf-8")
            dataset = root / "market.jsonl"
            rows = [
                {"kind": "bar", "provider": "alpaca", "feed": "iex",
                 "symbol": "SPY", "timestamp": "2026-08-08T13:30:00+00:00",
                 "as_of": "2026-08-08T13:30:00+00:00",
                 "observed_at": "2026-08-08T13:31:00+00:00",
                 "open": 100, "high": 101, "low": 99, "close": 100.5,
                 "volume": 10},
                {"kind": "option_snapshot", "provider": "alpaca",
                 "feed": "indicative", "symbol": "SPY260918C00100000",
                 "contract": "SPY260918C00100000",
                 "timestamp": "2026-08-08T13:30:02+00:00",
                 "as_of": "2026-08-08T13:30:02+00:00",
                 "observed_at": "2026-08-08T13:31:00+00:00",
                 "underlying": "SPY", "expiration": "2026-09-18",
                 "strike": 100, "right": "call", "multiplier": 100,
                 "bid": 1, "ask": 1.1, "bid_size": 10, "ask_size": 11,
                 "volume": 100, "open_interest": 200,
                 "underlying_price": 100},
            ]
            dataset.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8")
            source_bytes = dataset.read_bytes()
            result = _run_research_cycle(
                dataset, root, ALPACA_AGENT_CONFIG=str(config),
                ALPACA_OPTIONS_FEED="indicative",
                ALPACA_RESEARCH_VEHICLES="equity",
                ALPACA_FACTORY_ENABLED="1")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            diagnostics = [item for item in _cycle_payloads(
                result.stderr, "schema")
                           if item["schema"] ==
                           "research-cycle-vehicle-filter.v1"]
            self.assertEqual(diagnostics, [{
                "excluded_option_rows": 1,
                "schema": "research-cycle-vehicle-filter.v1",
                "selected_vehicles": ["equity"],
                "source_unchanged": True,
                "status": "filtered",
            }])
            views = [item for item in _cycle_payloads(result.stderr, "schema")
                     if item["schema"] == "research-cycle-views.v1"][0]
            self.assertEqual((views["bars"], views["options"], views["replay"]),
                             (1, 0, 1))
            validation = [item for item in _cycle_payloads(result.stdout, "valid")][0]
            self.assertTrue(validation["valid"])
            self.assertEqual(dataset.read_bytes(), source_bytes)
            with closing(sqlite3.connect(root / "edge.sqlite3")) as db:
                vehicles = dict(db.execute(
                    "SELECT vehicle, COUNT(*) FROM factory_hypotheses GROUP BY vehicle"
                ).fetchall())
            self.assertEqual(vehicles, {"equity": 12})

    def test_research_cycle_selected_all_with_indicative_options_fails(self):
        """Selecting the option lane requires configured OPRA provenance."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "research-test-config.json"
            config.write_text(json.dumps({
                "mode": "paper",
                "broker": {"paper": True, "allow_live": False,
                           "data_feed": "iex", "options_feed": "indicative"},
                "universe": {"asset_classes": ["us_equity"]},
                "session": {"require_exact_calendar": False},
                "strategy": {"selection_mode": "all_proved",
                             "execution_mode": "shares"},
                "research": {"enabled": True,
                             "require_validated_variant": True,
                             "strategy_llm": {"enabled": False}},
            }), encoding="utf-8")
            dataset = root / "market.jsonl"
            dataset.write_text(json.dumps({
                "kind": "bar", "provider": "alpaca", "feed": "iex",
                "symbol": "SPY", "timestamp": "2026-08-08T13:30:00+00:00",
                "as_of": "2026-08-08T13:30:00+00:00",
                "observed_at": "2026-08-08T13:31:00+00:00",
                "open": 100, "high": 101, "low": 99, "close": 100.5,
                "volume": 10,
            }) + "\n", encoding="utf-8")
            result = _run_research_cycle(
                dataset, root, ALPACA_AGENT_CONFIG=str(config),
                ALPACA_OPTIONS_FEED="indicative",
                ALPACA_RESEARCH_VEHICLES="all")
            self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
            self.assertIn("OPRA", result.stdout)

    def test_dashboard_tradeable_vehicle_matches_the_runtime_resolver(self):
        from agent.edge import runtime_vehicle
        for mode, expected in (("shares", "equity"), ("options", "option"),
                               ("option", "option"), ("", "equity")):
            config = {"strategy": {"execution_mode": mode},
                      "universe": {"asset_classes": ["us_equity", "us_option"]}}
            with self.subTest(mode=mode):
                self.assertEqual(dashboard._tradeable_vehicle(config), expected)
                self.assertEqual(runtime_vehicle(config), expected)

    def test_dashboard_counts_proved_edges_this_profile_cannot_trade(self):
        from research.edge_lab import EdgeLedger, init_ledger
        from tests.research.test_edge_discovery import _persist_gate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime" / "research").mkdir(parents=True)
            # An options profile holding a proved *equity* edge: real evidence
            # this trader can never act on, so it is reported, not counted as
            # deployable.
            (root / "config.yaml").write_text(
                "mode: paper\nstrategy:\n  id: ibr\n  version: v1\n"
                "  execution_mode: options\n", encoding="utf-8")
            ledger_path = root / "runtime" / "research" / "edge_lab.sqlite3"
            init_ledger(ledger_path)
            ledger = EdgeLedger(ledger_path)
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="an equity edge an options trader cannot deploy",
                config={"strategy": {"target_r": 1.5}})
            _persist_gate(ledger, candidate["candidate_id"], "shadow")
            with closing(sqlite3.connect(ledger_path)) as db, db:
                db.execute("UPDATE candidate_state SET status='validated' WHERE candidate_id=?",
                           (candidate["candidate_id"],))
            snapshot = dashboard.snapshot(root)
            self.assertEqual(snapshot["research"]["tradeable_vehicle"], "option")
            self.assertEqual(snapshot["edge"]["proved_edges"][0]["vehicle"], "equity")
            self.assertEqual(snapshot["research"]["untradeable_proved_edges"], 1)

    def test_recorder_passes_feed_and_deduplicates_overlapping_windows(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="sip"), 2)
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="sip"), 0)
            rows = list(recorder.iter_corpus_rows(path))
            self.assertEqual(len(rows), 2)  # one bar and one quote
            self.assertEqual(fake.seen, [("bars", "sip"), ("quotes", "sip"),
                                         ("bars", "sip"), ("quotes", "sip")])
            self.assertEqual(len({row["event_key"] for row in rows}), 2)
            self.assertTrue(all(row["feed"] == "sip" for row in rows))

    def test_duplicate_only_stale_quote_is_live_but_not_authorization_ready(self):
        fixed_now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

        class StaleQuoteFake:
            data_feed = "iex"

            def bars(self, symbols, *, start, end, feed, **kwargs):
                return {}

            def quotes(self, symbols, *, start, end, feed):
                return {"SPY": [SimpleNamespace(
                    timestamp=fixed_now - timedelta(seconds=45),
                    bid=100, ask=101, last=100.5)]}

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(recorder, "datetime", FrozenDatetime):
            root = Path(directory)
            path = root / "market.csv"
            fake = StaleQuoteFake()
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 1)
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 0)
            for item in (next((root / "sessions").glob("*.csv")),
                         root / recorder.INDEX_NAME):
                os.utime(item, (fixed_now.timestamp(), fixed_now.timestamp()))

            result = health.recorder(
                root, max_age=300, now=fixed_now.timestamp(),
                configured_symbols=["SPY"], configured_data_feed="iex")

        self.assertTrue(result["service_liveness"]["ok"])
        self.assertFalse(result["market_data_ready"])
        self.assertEqual(result["market_data_freshness_status"], "stale")
        self.assertGreater(result["observation_ages"]["SPY"]
                           ["quote_age_seconds"], 30)

    def test_recorder_health_marks_fresh_exact_feed_quotes_ready(self):
        now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "sessions" / "market-2026-08-28.csv"
            corpus.parent.mkdir()
            corpus.write_text("event_key\n", encoding="utf-8")
            index = root / recorder.INDEX_NAME
            index.write_text(json.dumps({
                "data_feed": "iex", "configured_symbols": ["SPY"],
                "observation_watermarks": {
                    "SPY": {
                        "quote": datetime.fromtimestamp(
                            now - 10, timezone.utc).isoformat(),
                        "bar": datetime.fromtimestamp(
                            now - 20, timezone.utc).isoformat(),
                    },
                },
            }), encoding="utf-8")
            os.utime(corpus, (now, now))
            os.utime(index, (now, now))

            result = health.recorder(
                root, max_age=300, now=now, configured_symbols=["SPY"],
                configured_data_feed="iex")

        self.assertTrue(result["ok"])
        self.assertTrue(result["market_data_ready"])
        self.assertEqual(result["market_data_freshness_status"], "ready")
        self.assertEqual(result["aggregate_observation_ages"]
                         ["quote_age_seconds"], 10)

    def test_recorder_health_missing_watermarks_is_unknown_and_fail_closed(self):
        now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "sessions" / "market-2026-08-28.csv"
            corpus.parent.mkdir()
            corpus.write_text("event_key\n", encoding="utf-8")
            index = root / recorder.INDEX_NAME
            index.write_text(json.dumps({
                "data_feed": "iex", "configured_symbols": ["SPY"],
            }), encoding="utf-8")
            os.utime(corpus, (now, now))
            os.utime(index, (now, now))

            result = health.recorder(
                root, max_age=300, now=now, configured_symbols=["SPY"],
                configured_data_feed="iex")

        self.assertTrue(result["service_liveness"]["ok"])
        self.assertFalse(result["market_data_ready"])
        self.assertEqual(result["market_data_freshness_status"], "unknown")
        self.assertEqual(result["market_data_reason"],
                         "observation_watermarks_missing")

    def test_recorder_health_missing_bar_is_unknown_and_fail_closed(self):
        now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "sessions" / "market-2026-08-28.csv"
            corpus.parent.mkdir()
            corpus.write_text("event_key\n", encoding="utf-8")
            index = root / recorder.INDEX_NAME
            index.write_text(json.dumps({
                "data_feed": "iex", "configured_symbols": ["SPY"],
                "observation_watermarks": {
                    "SPY": {"quote": datetime.fromtimestamp(
                        now - 10, timezone.utc).isoformat()},
                },
            }), encoding="utf-8")
            os.utime(corpus, (now, now))
            os.utime(index, (now, now))

            result = health.recorder(
                root, max_age=300, now=now, configured_symbols=["SPY"],
                configured_data_feed="iex")

        self.assertFalse(result["market_data_ready"])
        self.assertEqual(result["market_data_freshness_status"], "unknown")
        self.assertEqual(result["market_data_reason"],
                         "bar_watermarks_missing:SPY")

    def test_recorder_health_stale_bar_is_not_authorization_ready(self):
        now = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "sessions" / "market-2026-08-28.csv"
            corpus.parent.mkdir()
            corpus.write_text("event_key\n", encoding="utf-8")
            index = root / recorder.INDEX_NAME
            index.write_text(json.dumps({
                "data_feed": "iex", "configured_symbols": ["SPY"],
                "observation_watermarks": {
                    "SPY": {
                        "quote": datetime.fromtimestamp(
                            now - 10, timezone.utc).isoformat(),
                        "bar": datetime.fromtimestamp(
                            now - 31, timezone.utc).isoformat(),
                    },
                },
            }), encoding="utf-8")
            os.utime(corpus, (now, now))
            os.utime(index, (now, now))

            result = health.recorder(
                root, max_age=300, now=now, configured_symbols=["SPY"],
                configured_data_feed="iex")

        self.assertFalse(result["market_data_ready"])
        self.assertEqual(result["market_data_freshness_status"], "stale")
        self.assertEqual(result["market_data_reason"],
                         "bar_observations_stale:SPY")

    def test_closed_market_no_data_keeps_recorder_live_but_not_ready(self):
        now = datetime(2026, 8, 28, 22, 0, tzinfo=timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / recorder.INDEX_NAME
            index.write_text(json.dumps({
                "data_feed": "iex", "configured_symbols": ["SPY"],
                "session_calendar": {
                    "2026-08-28": {
                        "open": "2026-08-28T13:30:00+00:00",
                        "close": "2026-08-28T20:00:00+00:00",
                    },
                },
                "observation_watermarks": {
                    "SPY": {"quote": "2026-08-28T19:59:50+00:00"},
                },
            }), encoding="utf-8")
            status = root / recorder.STATUS_NAME
            status.write_text(json.dumps({
                "status": "failed", "updated_ts": now,
                "failure_kind": "market_data_request_failed",
                "error": "Alpaca returned no point-in-time bars or quotes",
            }), encoding="utf-8")
            os.utime(index, (now - 3600, now - 3600))

            result = health.recorder(
                root, max_age=300, now=now, configured_symbols=["SPY"],
                configured_data_feed="iex")

        self.assertTrue(result["ok"])
        self.assertTrue(result["service_liveness"]["ok"])
        self.assertEqual(result["series_files"], 0)
        self.assertEqual(result["status"], "recording_market_closed")
        self.assertFalse(result["market_data_ready"])
        self.assertEqual(result["market_data_freshness_status"],
                         "market_closed")

    def test_recorder_never_persists_an_active_partial_bar(self):
        fake = _MarketFake()
        active = datetime(2026, 8, 8, 13, 30, 30, tzinfo=timezone.utc)
        rows = list(recorder._rows(fake, ["SPY"], active, feed="iex"))
        self.assertEqual([row["event_type"] for row in rows], ["quote"])

        complete = datetime(2026, 8, 8, 13, 31, tzinfo=timezone.utc)
        rows = list(recorder._rows(fake, ["SPY"], complete, feed="iex"))
        bar = next(row for row in rows if row["event_type"] == "bar_1m")
        self.assertEqual(bar["as_of"], complete.isoformat())

    def test_recorder_resumes_from_durable_watermark_after_long_outage(self):
        fake = _WindowFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="iex"), 2)
            self.assertEqual(recorder.record_once(fake, ["SPY"], path,
                                                  feed="iex"), 0)
            expected = datetime(2026, 8, 8, 13, 29, 1,
                                tzinfo=timezone.utc)
            self.assertEqual(fake.starts[-2:], [expected, expected])

    def test_recorder_chunks_a_stale_quote_catch_up_window(self):
        fake = _QuoteChunkFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            stamp = datetime.now(timezone.utc) - timedelta(hours=3)
            row = {field: "" for field in recorder.FIELDS}
            row.update({
                "event_key": recorder._event_key("quote", "SPY", stamp.isoformat()),
                "observed_at": stamp.isoformat(), "provider": "alpaca",
                "feed": "sip", "event_type": "quote", "symbol": "SPY",
                "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
                "bid": "100", "ask": "101", "last": "100.5",
            })
            recorder._append_partitions(path, [row])
            recorder._save_index(path, recorder._scan_corpus(path))
            with patch.dict(os.environ, {"ALPACA_RECORDER_FETCH_WINDOW_MINUTES": "30"}):
                count = recorder.record_once(fake, ["SPY"], path)
            self.assertGreater(count, 1)
            windows = [item for item in fake.windows if item[0] == "quotes"]
            self.assertGreater(len(windows), 1)
            self.assertTrue(all(end - start <= timedelta(minutes=30)
                                for _kind, start, end in windows))
            index = recorder._prepare_index(path)
            stale_day = recorder._session_date(stamp)
            self.assertEqual(index["partition_sources"][
                recorder._partition_path(path, stale_day).name], {
                    "source_mode": "historical_backfill",
                })

    def test_recorder_classifies_only_late_first_observations_as_historical(self):
        observed = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)

        def row(stamp, lag):
            return {
                "timestamp": stamp.isoformat(),
                "as_of": (stamp + timedelta(minutes=1)).isoformat(),
                "observed_at": (stamp + timedelta(minutes=1, seconds=lag)).isoformat(),
            }

        live = datetime(2026, 8, 13, 13, 58, tzinfo=timezone.utc)
        stale = observed - timedelta(days=2)
        days = recorder._historical_partition_days(
            [row(live, 10), row(stale, 16 * 60)], timedelta(minutes=15))
        self.assertEqual(days, {recorder._session_date(stale)})

    def test_sparse_iex_gap_advances_and_is_exposed_as_coverage(self):
        fixed_now = datetime(2026, 8, 13, 16, 0, tzinfo=timezone.utc)
        bar_stamp = fixed_now - timedelta(hours=1, minutes=13)
        quote_stamp = fixed_now - timedelta(hours=1, minutes=5)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

        def row(event_type, stamp):
            value = {field: "" for field in recorder.FIELDS}
            value.update({
                "event_key": recorder._event_key(
                    event_type, "DIA", stamp.isoformat()),
                "observed_at": stamp.isoformat(), "provider": "alpaca",
                "feed": "iex", "event_type": event_type, "symbol": "DIA",
                "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
            })
            if event_type == "bar_1m":
                value.update({"open": "100", "high": "101", "low": "99",
                              "close": "100.5", "volume": "10"})
            else:
                value.update({"bid": "100", "ask": "101", "last": "100.5"})
            return value

        fake = _SparseFeedFake("iex")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            recorder._append_partitions(
                path, [row("bar_1m", bar_stamp), row("quote", quote_stamp)])
            recorder._save_index(path, recorder._scan_corpus(path))

            with patch.object(recorder, "datetime", FrozenDatetime):
                self.assertEqual(recorder.record_once(fake, ["DIA"], path), 1)

            index = recorder._load_index(path)
            coverage = index["bar_coverage"]["DIA"]
            self.assertEqual(index["data_feed"], "iex")
            self.assertEqual(coverage["feed"], "iex")
            self.assertEqual(coverage["policy"], "observe")
            self.assertEqual(coverage["status"], "gap_observed")
            self.assertEqual(coverage["last_bar"], bar_stamp.isoformat())
            self.assertEqual(coverage["gap_observations"], 1)
            self.assertGreater(coverage["max_gap_seconds"], 5 * 60)

            index_path = root / recorder.INDEX_NAME
            result = health.recorder(
                root, max_age=1, now=index_path.stat().st_mtime)
            self.assertTrue(result["ok"])
            self.assertEqual(result["coverage_status"], "gap_observed")
            self.assertEqual(result["bar_gap_symbols"], ["DIA"])
            self.assertEqual(result["bar_coverage"]["DIA"]["policy"], "observe")

    def test_strict_recorder_health_fails_gap_and_unobserved_configured_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            sessions.mkdir()
            corpus = sessions / "market-2026-08-13.csv"
            corpus.write_text("event_key\n", encoding="utf-8")
            index = root / recorder.INDEX_NAME
            index.write_text(json.dumps({
                "data_feed": "iex",
                "configured_symbols": ["DIA", "SPY"],
                "bar_coverage": {
                    "DIA": {"status": "gap_observed", "gap_observations": 1,
                             "last_bar": "2026-08-13T19:00:00+00:00"},
                },
            }), encoding="utf-8")
            os.utime(corpus, (1000, 1000))
            os.utime(index, (1000, 1000))

            result = health.recorder(
                root, max_age=60, now=1000,
                configured_symbols=["DIA", "SPY"], strict_bar_feeds="iex")

        self.assertFalse(result["ok"])
        self.assertTrue(result["strict_bar_policy"])
        self.assertEqual(result["bar_coverage_failures"], ["DIA", "SPY"])
        self.assertIn("strict_bar_coverage_failed", result["reason"])

    def test_deployment_provenance_parity_is_deterministic(self):
        from deploy.provenance import deployment_parity, deployment_provenance

        current = deployment_provenance({
            "ALPACA_DEPLOYMENT_COMMIT": "A" * 40,
            "ALPACA_DEPLOYMENT_IMAGE": "agent:test",
        })
        self.assertEqual(current["identity"], "a" * 40)
        self.assertIsNone(deployment_provenance({
            "ALPACA_DEPLOYMENT_IMAGE": "agent:local",
        })["identity"])
        invalid = deployment_provenance({
            "ALPACA_DEPLOYMENT_IMAGE_DIGEST": "latest",
        })
        self.assertIsNone(invalid["identity"])
        self.assertTrue(invalid["image_digest_invalid"])
        self.assertEqual(invalid["image_digest_reason"],
                         "invalid_oci_sha256_digest")
        self.assertFalse(deployment_parity([
            {"component": "recorder", "provenance": invalid},
        ])["ok"])
        valid = deployment_provenance({
            "ALPACA_DEPLOYMENT_IMAGE_DIGEST": "sha256:" + "AB" * 32,
        })
        self.assertEqual(valid["identity"], "sha256:" + "ab" * 32)
        embedded = deployment_provenance({
            "ALPACA_DEPLOYMENT_IMAGE": "agent@sha256:" + "CD" * 32,
        })
        self.assertEqual(embedded["identity"], "sha256:" + "cd" * 32)
        commit_fallback = deployment_provenance({
            "ALPACA_BUILD_COMMIT": "a" * 40,
            "ALPACA_DEPLOYMENT_IMAGE_DIGEST": "latest",
        })
        self.assertEqual(commit_fallback["identity"], "a" * 40)
        invalid_commit = deployment_provenance({
            "ALPACA_DEPLOYMENT_COMMIT": "main",
        })
        self.assertIsNone(invalid_commit["identity"])
        self.assertTrue(invalid_commit["commit_invalid"])
        self.assertEqual(invalid_commit["commit_invalid_reason"],
                         "invalid_git_object_id")
        self.assertFalse(deployment_parity([
            {"component": "invalid", "provenance": invalid_commit},
        ])["ok"])
        mixed_commit = deployment_provenance({
            "ALPACA_DEPLOYMENT_COMMIT": "main",
            "ALPACA_BUILD_COMMIT": "a" * 40,
        })
        self.assertEqual(mixed_commit["identity"], "a" * 40)
        self.assertTrue(deployment_parity([
            {"component": "mixed", "provenance": mixed_commit},
        ])["ok"])
        self.assertTrue(deployment_parity([
            {"component": "recorder", "provenance": current},
            {"component": "research", "provenance": current},
        ])["ok"])
        self.assertFalse(deployment_parity([
            {"component": "recorder", "provenance": current},
            {"component": "research", "provenance": {"identity": "other"}},
        ])["ok"])

    def test_baked_commit_outranks_placeholder_and_conflicts_fail_closed(self):
        from deploy.provenance import deployment_provenance

        baked = deployment_provenance({
            "ALPACA_DEPLOYMENT_COMMIT": "unknown",
            "ALPACA_BUILD_COMMIT": "a" * 40,
            "ALPACA_DEPLOYMENT_IMAGE": "agent:local",
        })
        self.assertEqual(baked["identity"], "a" * 40)
        self.assertFalse(baked["commit_mismatch"])
        conflict = deployment_provenance({
            "ALPACA_DEPLOYMENT_COMMIT": "b" * 40,
            "ALPACA_BUILD_COMMIT": "a" * 40,
        })
        self.assertIsNone(conflict["identity"])
        self.assertTrue(conflict["commit_mismatch"])
        from deploy.provenance import deployment_parity
        self.assertFalse(deployment_parity([{"component": "conflict",
                                             "provenance": conflict}])["ok"])
        compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
        self.assertIn("ALPACA_DEPLOYMENT_IMAGE_DIGEST", compose)
        self.assertIn("ALPACA_BUILD_COMMIT", dockerfile)

    def test_recorder_cadence_skips_missed_ticks_without_bursting(self):
        self.assertEqual(recorder._next_cadence_deadline(None, 100.0, 30), 130.0)
        # A five-second request that began on the 100-second tick still sleeps
        # only to 130, so work time is not added to the polling interval.
        self.assertEqual(recorder._next_cadence_deadline(100.0, 105.0, 30), 130.0)
        self.assertEqual(recorder._next_cadence_deadline(130.0, 120.0, 30), 130.0)
        # A 95-second request overran three 30-second ticks; the next request
        # is scheduled for 130 rather than replaying 40/70/100 immediately.
        self.assertEqual(recorder._next_cadence_deadline(40.0, 125.0, 30), 130.0)
        unit = (Path(__file__).resolve().parents[1] /
                "deploy/alpaca-recorder.service").read_text()
        self.assertIn("deploy/recorder.py --out", unit)
        self.assertIn("--interval 30", unit)

    def test_recorder_rejects_intervals_above_quote_freshness_window(self):
        with self.assertRaisesRegex(SystemExit, "at most 30 seconds"):
            recorder.main(["--interval", "30.001"])

    def test_strict_feed_gap_fails_before_mutating_the_corpus(self):
        fixed_now = datetime(2026, 8, 13, 16, 0, tzinfo=timezone.utc)
        bar_stamp = fixed_now - timedelta(hours=1, minutes=13)
        quote_stamp = fixed_now - timedelta(hours=1, minutes=5)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

        rows = []
        for event_type, stamp in (("bar_1m", bar_stamp), ("quote", quote_stamp)):
            row = {field: "" for field in recorder.FIELDS}
            row.update({
                "event_key": recorder._event_key(
                    event_type, "DIA", stamp.isoformat()),
                "observed_at": stamp.isoformat(), "provider": "alpaca",
                "feed": "sip", "event_type": event_type, "symbol": "DIA",
                "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
                "open": "100", "high": "101", "low": "99",
                "close": "100.5", "volume": "10", "bid": "100",
                "ask": "101", "last": "100.5",
            })
            rows.append(row)

        fake = _SparseFeedFake("sip")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            recorder._append_partitions(path, rows)
            recorder._save_index(path, recorder._scan_corpus(path))
            before_rows = list(recorder.iter_corpus_rows(path))
            index_path = root / recorder.INDEX_NAME
            before_index = index_path.read_text(encoding="utf-8")

            with patch.dict(os.environ, {
                    "ALPACA_RECORDER_STRICT_BAR_FEEDS": "sip"}), \
                    patch.object(recorder, "datetime", FrozenDatetime):
                with self.assertRaisesRegex(RuntimeError, "continuity gap for DIA"):
                    recorder.record_once(fake, ["DIA"], path)

            self.assertEqual(list(recorder.iter_corpus_rows(path)), before_rows)
            self.assertEqual(index_path.read_text(encoding="utf-8"), before_index)

    def test_recorder_scan_retains_only_recent_keys_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            rows = _corpus_rows(sessions=30, per_session=200)
            recorder._append_partitions(path, rows)
            index = recorder._scan_corpus(path)
            metadata = index["recent_key_index"]
            self.assertLess(metadata["count"], len(rows))
            self.assertLessEqual(metadata["count"], 16)
            self.assertNotIn("recent_keys", index)
            with recorder.RecentKeyIndex(
                    Path(directory) / recorder.RECENT_KEY_INDEX_NAME,
                    read_only=True) as recent:
                self.assertEqual(recent.count(), metadata["count"])

    def test_recorder_rebuild_removes_stale_target_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            target = root / recorder.RECENT_KEY_INDEX_NAME
            stamp = "2026-08-08T13:30:00+00:00"
            recorder._build_recent_key_index(
                path, [("old", stamp)], watermark=stamp,
                partitions={}, fingerprints={})
            sidecars = [Path(str(target) + suffix)
                        for suffix in ("-journal", "-wal", "-shm")]
            for sidecar in sidecars:
                sidecar.write_bytes(b"stale rollback state")

            with patch.object(recorder, "_fsync_directory") as fsync:
                metadata = recorder._build_recent_key_index(
                    path, [("new", stamp)], watermark=stamp,
                    partitions={}, fingerprints={})

            self.assertEqual(fsync.call_args_list, [call(root), call(root)])
            self.assertTrue(all(not sidecar.exists() for sidecar in sidecars))
            with recorder.RecentKeyIndex(target, read_only=True) as recent:
                self.assertEqual(recent.count(), metadata["count"])
                self.assertTrue(recent.contains("new"))
                self.assertFalse(recent.contains("old"))
                self.assertEqual(
                    recent.db.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok")

    def test_recorder_high_rate_window_keeps_json_index_small(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            start = datetime(2026, 8, 8, 13, 30, tzinfo=timezone.utc)
            rows = []
            for number in range(20_000):
                stamp = start + timedelta(microseconds=number)
                row = {field: "" for field in recorder.FIELDS}
                row.update({
                    "event_key": recorder._event_key(
                        "quote", "SPY", stamp.isoformat()),
                    "observed_at": stamp.isoformat(), "provider": "alpaca",
                    "feed": "iex", "event_type": "quote", "symbol": "SPY",
                    "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
                    "bid": "100", "ask": "101", "last": "100.5",
                })
                rows.append(row)
            recorder._append_partitions(path, rows)
            index = recorder._scan_corpus(path)
            recorder._save_index(path, index)

            index_path = root / recorder.INDEX_NAME
            self.assertLess(index_path.stat().st_size, 16_384)
            loaded = recorder._load_index(path)
            self.assertIsNotNone(loaded)
            self.assertNotIn("recent_keys", loaded)
            self.assertEqual(loaded["recent_key_index"]["count"], len(rows))
            with recorder.RecentKeyIndex(
                    root / recorder.RECENT_KEY_INDEX_NAME,
                    read_only=True) as recent:
                self.assertEqual(recent.count(), len(rows))
                self.assertTrue(recent.contains(rows[-1]["event_key"]))

    def test_recorder_does_not_decode_oversized_legacy_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=2)
            recorder._append_partitions(path, rows)
            index = recorder._scan_corpus(path)
            legacy = {
                **index,
                "recent_keys": {
                    row["event_key"]: row["timestamp"] for row in rows},
            }
            legacy.pop("recent_key_index")
            (root / recorder.INDEX_NAME).write_text(
                json.dumps(legacy), encoding="utf-8")
            with patch.object(recorder, "MAX_INLINE_INDEX_BYTES", 64):
                self.assertIsNone(recorder._load_index(path))

    def test_recorder_oversized_migration_preserves_calendar_and_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=3)
            recorder._append_partitions(path, rows)
            legacy = recorder._scan_corpus(path)
            legacy.pop("recent_key_index")
            legacy.pop("partition_fingerprints")
            legacy["recent_keys"] = {
                f"{number:064x}": "2026-01-05T15:00:00+00:00"
                for number in range(200)
            }
            legacy["bar_coverage"] = {
                "SPY": {"status": "covered", "observations": 7},
            }
            legacy["session_calendar"] = {
                "2026-01-05": {
                    "open": "2026-01-05T14:30:00+00:00",
                    "close": "2026-01-05T21:00:00+00:00",
                    "source": "alpaca_calendar",
                },
            }
            legacy["option_pins"] = {
                "SPY260116C00600000": "2026-01-05T18:00:00+00:00",
            }
            index_path = root / recorder.INDEX_NAME
            index_path.write_text(json.dumps(legacy, sort_keys=True),
                                  encoding="utf-8")
            self.assertGreater(index_path.stat().st_size, 8_192)

            with patch.object(recorder, "MAX_INLINE_INDEX_BYTES", 8_192):
                migrated = recorder._prepare_index(path)

            self.assertEqual(migrated["bar_coverage"], legacy["bar_coverage"])
            self.assertEqual(migrated["session_calendar"],
                             legacy["session_calendar"])
            self.assertEqual(migrated["option_pins"], legacy["option_pins"])
            self.assertNotIn("recent_keys", migrated)
            self.assertLess(index_path.stat().st_size, 8_192)
            with recorder.RecentKeyIndex(
                    root / recorder.RECENT_KEY_INDEX_NAME,
                    read_only=True) as recent:
                self.assertEqual(
                    recent.count(), migrated["recent_key_index"]["count"])

    def test_recorder_rebuild_preserves_observation_watermarks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=2)
            quote = dict(rows[-1])
            quote.update({
                "event_key": recorder._event_key(
                    "quote", "SPY", quote["timestamp"]),
                "event_type": "quote", "bid": "100", "ask": "101",
                "last": "100.5", "open": "", "high": "", "low": "",
                "close": "", "volume": "",
            })
            recorder._append_partitions(path, [*rows, quote])
            legacy = recorder._scan_corpus(path)
            legacy["partitions"] = {}
            (root / recorder.INDEX_NAME).write_text(
                json.dumps(legacy, sort_keys=True), encoding="utf-8")

            rebuilt = recorder._prepare_index(path)
            self.assertEqual(
                rebuilt["observation_watermarks"]["SPY"]["quote"],
                quote["as_of"])
            self.assertEqual(
                rebuilt["observation_watermarks"]["SPY"]["bar"],
                rows[-1]["as_of"])

            pre_watermark = dict(rebuilt)
            pre_watermark.pop("observation_watermarks")
            (root / recorder.INDEX_NAME).write_text(
                json.dumps(pre_watermark, sort_keys=True), encoding="utf-8")
            with patch.object(
                    recorder, "_scan_corpus",
                    side_effect=AssertionError(
                        "additive observation watermark migration must not scan")):
                migrated = recorder._prepare_index(path)
            self.assertEqual(migrated["observation_watermarks"], {})

            class FrozenDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    fixed = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)
                    return fixed if tz is not None else fixed.replace(tzinfo=None)

            with patch.object(recorder, "datetime", FrozenDatetime):
                self.assertEqual(recorder.record_once(
                    _MarketFake(), ["SPY"], path), 2)
            reloaded = recorder._load_index(path)

        self.assertEqual(reloaded["observation_watermarks"]["SPY"]["quote"],
                         "2026-08-08T13:30:01+00:00")
        self.assertEqual(reloaded["observation_watermarks"]["SPY"]["bar"],
                         "2026-08-08T13:31:00+00:00")

    def test_recorder_same_size_partition_rewrite_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=3)
            recorder._append_partitions(path, rows)
            index = recorder._scan_corpus(path)
            index["session_calendar"] = {
                "2026-01-05": {
                    "open": "2026-01-05T14:30:00+00:00",
                    "close": "2026-01-05T21:00:00+00:00",
                    "source": "alpaca_calendar",
                },
            }
            recorder._save_index(path, index)
            partition = recorder.corpus_partitions(path)[0]
            before = partition.stat()
            content = partition.read_bytes()
            old_key = rows[-1]["event_key"]
            new_key = "f" * len(old_key)
            if new_key == old_key:
                new_key = "e" * len(old_key)
            partition.write_bytes(content.replace(
                old_key.encode("utf-8"), new_key.encode("utf-8"), 1))
            os.utime(partition, ns=(before.st_atime_ns, before.st_mtime_ns + 1))

            self.assertEqual(partition.stat().st_size, before.st_size)
            self.assertIsNone(recorder._load_index(path))
            recovered = recorder._prepare_index(path)
            self.assertEqual(recovered["session_calendar"],
                             index["session_calendar"])

    def test_recorder_append_crash_recovery_preserves_compact_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=2)
            recorder._append_partitions(path, rows)
            index = recorder._scan_corpus(path)
            index["bar_coverage"] = {
                "SPY": {"status": "covered", "observations": 3},
            }
            index["session_calendar"] = {
                "2026-01-05": {
                    "open": "2026-01-05T14:30:00+00:00",
                    "close": "2026-01-05T21:00:00+00:00",
                    "source": "alpaca_calendar",
                },
            }
            recorder._save_index(path, index)
            before_size = (root / recorder.INDEX_NAME).stat().st_size

            # Simulate a crash after the authoritative CSV fsync but before
            # SQLite/JSON sidecar commits. The marker is newer than the stale
            # aggregate index, as it is during interrupted backfill.
            extra = _corpus_rows(sessions=2, per_session=1)[0]
            extra_day = recorder._session_date(
                datetime.fromisoformat(extra["timestamp"]))
            recorder._save_partition_source(
                path, extra_day, "historical_backfill")
            recorder._append_partitions(path, [extra])
            self.assertIsNone(recorder._load_index(path))
            recovered = recorder._prepare_index(path)

            self.assertEqual(recovered["bar_coverage"], index["bar_coverage"])
            self.assertEqual(recovered["session_calendar"],
                             index["session_calendar"])
            self.assertEqual(recovered["partition_sources"], {
                recorder._partition_path(path, extra_day).name: {
                    "source_mode": "historical_backfill",
                },
            })
            self.assertLess((root / recorder.INDEX_NAME).stat().st_size,
                            max(16_384, before_size * 2))

    def test_recorder_repairs_missing_historical_partition_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            rows = _corpus_rows(sessions=2, per_session=1)
            stale_stamp = datetime.fromisoformat(rows[0]["timestamp"])
            rows[0]["observed_at"] = (
                stale_stamp + timedelta(days=2)).isoformat()
            recorder._append_partitions(path, rows)
            recorder._save_index(path, recorder._scan_corpus(path))

            result = recorder.repair_partition_provenance(
                path, maximum_lag=timedelta(minutes=15))

            stale_day = recorder._session_date(stale_stamp)
            live_day = recorder._session_date(
                datetime.fromisoformat(rows[1]["timestamp"]))
            self.assertEqual(result["rows_scanned"], 2)
            self.assertEqual(result["new_historical_partitions"], 1)
            sources = recorder._prepare_index(path)["partition_sources"]
            self.assertEqual(sources, {
                recorder._partition_path(path, stale_day).name: {
                    "source_mode": "historical_backfill",
                },
            })
            self.assertNotIn(recorder._partition_path(path, live_day).name,
                             sources)

            repeated = recorder.repair_partition_provenance(
                path, maximum_lag=timedelta(minutes=15))
            self.assertEqual(repeated["new_historical_partitions"], 0)

    def test_recorder_fsyncs_new_partition_and_provenance_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=1)
            day = recorder._session_date(
                datetime.fromisoformat(rows[0]["timestamp"]))
            sessions = Path(directory) / recorder.PARTITION_DIR
            with patch.object(recorder, "_fsync_directory") as fsync:
                recorder._save_partition_source(
                    path, day, "historical_backfill")
                recorder._append_partitions(path, rows)
            self.assertEqual(fsync.call_args_list, [
                call(sessions),
                call(sessions),
            ])

    def test_recorder_tolerates_marker_only_backfill_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            day = datetime(2026, 8, 7, tzinfo=timezone.utc).date()
            recorder._save_partition_source(
                path, day, "historical_backfill")

            self.assertEqual(
                recorder._partition_sources_from_markers(path), {})
            rebuilt = recorder._scan_corpus(path)
            self.assertEqual(rebuilt["partition_sources"], {})

    def test_recorder_corpus_lock_serializes_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            command = [
                sys.executable, "-c",
                "from pathlib import Path; import sys; "
                "from deploy.recorder import corpus_write_lock; "
                "\nwith corpus_write_lock(Path(sys.argv[1])): "
                "print('acquired', flush=True)",
                str(path),
            ]
            with recorder.corpus_write_lock(path):
                child = subprocess.Popen(
                    command, cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.2)
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "acquired")

    def test_recorder_rebuilds_when_recent_key_count_does_not_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=3)
            recorder._append_partitions(path, rows)
            recorder._save_index(path, recorder._scan_corpus(path))

            database = root / recorder.RECENT_KEY_INDEX_NAME
            with closing(sqlite3.connect(database)) as db:
                with db:
                    db.execute(
                        "DELETE FROM recent_keys WHERE event_key=?",
                        (rows[-1]["event_key"],))

            self.assertIsNone(recorder._load_index(path))

    def test_recorder_recent_key_timestamps_are_normalized_to_utc(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / recorder.RECENT_KEY_INDEX_NAME
            with recorder.RecentKeyIndex(database, create=True) as recent:
                recent.add_many([
                    ("first", "2026-08-08T08:59:00-04:00"),
                    ("second", "2026-08-08T13:01:00+00:00"),
                ])
                recent.prune(datetime(
                    2026, 8, 8, 13, 0, tzinfo=timezone.utc))
                self.assertFalse(recent.contains("first"))
                self.assertTrue(recent.contains("second"))

    def test_recorder_service_retries_errors_without_exiting(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(recorder, "AlpacaProvider", return_value=object()), \
                    patch.object(recorder, "record_once",
                                 side_effect=[RuntimeError("continuity gap"),
                                              KeyboardInterrupt()]) as record, \
                    patch.object(recorder.time, "sleep") as sleep:
                with self.assertRaises(KeyboardInterrupt):
                    recorder.main([
                        "--config", "config.yaml", "--out", directory,
                        "--interval", "2",
                    ])
            self.assertEqual(record.call_count, 2)
            sleep.assert_called_once_with(2.0)

    def test_recorder_skips_quotes_without_point_in_time_timestamp(self):
        fake = _MissingQuoteTimestampFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 1)
            rows = list(recorder.iter_corpus_rows(path))
            self.assertEqual([row["event_type"] for row in rows], ["bar_1m"])

    def test_recorder_fails_closed_on_corrupt_existing_csv(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            path.write_text(
                "event_key,event_type,symbol,timestamp\n"
                "x,bar_1m,SPY,2026-08-08T13:30:00+00:00,unexpected\n",
                encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "malformed CSV"):
                recorder.record_once(fake, ["SPY"], path)

    def test_recorder_rejects_crypto_symbols_before_provider_calls(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            with self.assertRaisesRegex(ValueError, "slash pair"):
                recorder.record_once(fake, ["BTC/USD"], path)
            self.assertEqual(fake.seen, [])

    def test_recorder_rejects_future_provider_events(self):
        fake = _FutureBarFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            with self.assertRaisesRegex(RuntimeError, "future"):
                recorder.record_once(fake, ["SPY"], path)
            self.assertFalse(path.exists())

    def test_recorder_does_not_mutate_invalid_legacy_csv(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            original = ("event_type,symbol,timestamp\n"
                        "bar_1m,SPY,not-a-timestamp\n")
            path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid timestamp"):
                recorder.record_once(fake, ["SPY"], path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_recorder_skips_option_rows_with_mismatched_underlying(self):
        fake = _MismatchedOptionFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(
                fake, ["SPY"], path, include_options=True), 2)
            rows = list(recorder.iter_corpus_rows(path))
            self.assertEqual({row["event_type"] for row in rows},
                             {"bar_1m", "quote"})

    def test_recorder_rejects_invalid_existing_row_semantics(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            writer_buffer = io.StringIO()
            writer = csv.DictWriter(writer_buffer, fieldnames=recorder.FIELDS)
            writer.writeheader()
            writer.writerow({
                "event_key": "bad", "event_type": "evil", "symbol": "BTC/USD",
                "timestamp": "2026-08-08T13:30:00+00:00"})
            path.write_text(writer_buffer.getvalue(), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid recorder dataset row"):
                recorder.record_once(fake, ["SPY"], path)
            self.assertEqual(fake.seen, [])

    def test_recorder_observes_or_rejects_intraday_gap_by_policy(self):
        previous = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
        current = previous + timedelta(hours=2)
        rows = [{"event_type": "bar_1m", "symbol": "SPY",
                 "timestamp": current.isoformat()}]
        evidence = recorder._verify_bar_continuity(
            rows, {"SPY": previous}, current, ["SPY"],
            feed="iex", policy="observe")
        self.assertEqual(evidence["SPY"]["status"], "gap_observed")
        self.assertEqual(evidence["SPY"]["window_gap_count"], 1)
        with self.assertRaisesRegex(RuntimeError, "continuity gap"):
            recorder._verify_bar_continuity(
                rows, {"SPY": previous}, current, ["SPY"],
                feed="sip", policy="strict")
        adjacent = recorder._verify_bar_continuity(
            [{"event_type": "bar_1m", "symbol": "SPY",
              "timestamp": (previous + timedelta(minutes=1)).isoformat()}],
            {"SPY": previous}, previous + timedelta(minutes=1), ["SPY"],
            feed="sip", policy="strict")
        self.assertEqual(adjacent["SPY"]["status"], "covered")

    def test_recorder_loads_precoverage_v1_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=2, feed="sip")
            recorder._append_partitions(path, rows)
            index = recorder._scan_corpus(path)
            index.pop("bar_coverage")
            index.pop("data_feed")
            (root / recorder.INDEX_NAME).write_text(
                json.dumps(index), encoding="utf-8")

            loaded = recorder._load_index(path)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["bar_coverage"], {})
            self.assertIsNone(loaded["data_feed"])

            fake = _SparseFeedFake("iex")
            with self.assertRaisesRegex(RuntimeError,
                                        "data feed changed from sip to iex"):
                recorder.record_once(fake, ["SPY"], path)
            self.assertEqual(fake.seen, [])

    def test_precoverage_index_rejects_a_mixed_feed_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=2)
            mixed = dict(rows[-1])
            mixed_stamp = datetime.fromisoformat(mixed["timestamp"]) + timedelta(
                minutes=10)
            mixed.update({
                "event_key": recorder._event_key(
                    "bar_1m", "SPY", mixed_stamp.isoformat()),
                "feed": "sip", "timestamp": mixed_stamp.isoformat(),
                "as_of": mixed_stamp.isoformat(),
            })
            recorder._append_partitions(path, [*rows, mixed])
            index = {
                "schema": recorder.INDEX_SCHEMA,
                "watermark": mixed_stamp.isoformat(),
                "latest_bars": {"SPY": mixed_stamp.isoformat()},
                "recent_keys": {
                    row["event_key"]: row["timestamp"] for row in [*rows, mixed]
                },
                "option_pins": {},
                "partitions": recorder._partition_sizes(path),
            }
            (root / recorder.INDEX_NAME).write_text(
                json.dumps(index), encoding="utf-8")

            fake = _SparseFeedFake("sip")
            with self.assertRaisesRegex(RuntimeError,
                                        "mixes equity data feeds"):
                recorder.record_once(fake, ["SPY"], path)
            self.assertEqual(fake.seen, [])

    def test_recorder_partitions_by_session_and_migrates_a_legacy_corpus(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            legacy = _corpus_rows(sessions=3, per_session=4)
            _write_flat_corpus(path, legacy)
            self.assertEqual(recorder.migrate_corpus(path), len(legacy))
            self.assertEqual(
                [item.name for item in recorder.corpus_partitions(path)],
                ["market-2026-08-05.csv", "market-2026-08-06.csv",
                 "market-2026-08-07.csv"])
            self.assertFalse(path.exists())
            self.assertTrue(path.with_name("market.csv.migrated").is_file())
            migrated = list(recorder.iter_corpus_rows(path))
            self.assertEqual([row["event_key"] for row in migrated],
                             [row["event_key"] for row in legacy])
            # The first cycle after the upgrade resumes from the migrated
            # watermark instead of rewriting anything.
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 2)
            self.assertEqual(len(recorder.corpus_partitions(path)), 4)

    def test_recorder_rejects_duplicate_keys_and_rebuilds_a_stale_index(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            rows = _corpus_rows(sessions=1, per_session=4)
            recorder._append_partitions(path, rows)
            recorder._save_index(path, recorder._scan_corpus(path))
            index = Path(directory) / recorder.INDEX_NAME
            self.assertTrue(index.is_file())
            # A cycle that appended rows and died before rewriting the index
            # leaves partition sizes disagreeing with it; that must rebuild.
            recorder._append_partitions(path, _corpus_rows(
                sessions=1, per_session=2, minute=90))
            self.assertIsNone(recorder._load_index(path))
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 2)
            keys = [row["event_key"] for row in recorder.iter_corpus_rows(path)]
            self.assertEqual(len(keys), len(set(keys)))
            recorder._append_partitions(path, rows[:1])
            with self.assertRaisesRegex(RuntimeError, "repeats event_key"):
                recorder.audit_corpus(path)

    def test_recorder_refuses_rows_older_than_the_dedup_window(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 2)
            state = recorder._load_index(path)
            state["watermark"] = (
                datetime(2026, 8, 8, 13, 30, tzinfo=timezone.utc) +
                recorder.DEDUP_HORIZON + timedelta(minutes=5)).isoformat()
            recorder._save_index(path, state)
            with self.assertRaisesRegex(RuntimeError, "older than the dedup window"):
                recorder.record_once(fake, ["SPY"], path)

    def test_recorder_cycle_cost_stays_flat_as_the_corpus_grows(self):
        counts = []

        def counting(output):
            rows = 0
            for row in _corpus_iterator(output):
                rows += 1
                yield row
            counts.append(rows)

        with tempfile.TemporaryDirectory() as directory:
            measured = {}
            for label, sessions in (("small", 3), ("large", 30)):
                root = Path(directory) / label
                path = root / "market.csv"
                rows = _corpus_rows(sessions=sessions, per_session=200)
                recorder._append_partitions(path, rows)
                recorder._save_index(path, recorder._scan_corpus(path))
                self.assertEqual(len(recorder.corpus_partitions(path)), sessions)
                with patch.object(recorder, "iter_corpus_rows", counting):
                    counts.clear()
                    recorder.record_once(_MarketFake(), ["SPY"], path)
                    steady = sum(counts)
                    counts.clear()
                    recorder._scan_corpus(path)
                    measured[label] = (steady, sum(counts), len(rows))
            # A steady cycle reads no durable rows at either size, while the
            # recovery scan it replaced still grows with the corpus.
            self.assertEqual(measured["small"][0], 0)
            self.assertEqual(measured["large"][0], 0)
            self.assertGreaterEqual(
                measured["large"][1] / measured["small"][1],
                0.9 * measured["large"][2] / measured["small"][2])

    def test_recorder_keeps_calendar_holidays_and_early_closes_quiet(self):
        calendar = recorder.CalendarCache(_CalendarFake())
        zone = recorder.NEW_YORK
        gap = timedelta(hours=2)
        trading = datetime(2026, 8, 6, 10, 0, tzinfo=zone)
        self.assertTrue(recorder._regular_session_gap(
            trading, trading + gap, calendar))
        holiday = datetime(2026, 8, 5, 10, 0, tzinfo=zone)  # weekday, shut
        self.assertFalse(recorder._regular_session_gap(
            holiday, holiday + gap, calendar))
        self.assertTrue(recorder._regular_session_gap(
            holiday, holiday + gap))  # no calendar: heuristic still fails closed
        early = datetime(2026, 8, 7, 12, 55, tzinfo=zone)  # closes 13:00
        self.assertFalse(recorder._regular_session_gap(
            early, early + gap, calendar))
        self.assertEqual(calendar.provider.calls, 1)

    def test_recorder_persists_exact_early_close_for_shadow_replay(self):
        calendar = recorder.CalendarCache(_CalendarFake())
        index = {"session_calendar": {}}
        start = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 7, 22, 0, tzinfo=timezone.utc)
        recorder._record_session_calendar(index, calendar, start, end)
        session = index["session_calendar"]["2026-08-07"]
        self.assertEqual(session["open"], "2026-08-07T13:30:00+00:00")
        self.assertEqual(session["close"], "2026-08-07T17:00:00+00:00")
        self.assertEqual(session["source"], "alpaca_calendar")

    def test_recorder_persists_closed_weekday_calendar_marker(self):
        calendar = recorder.CalendarCache(_CalendarFake())
        index = {"session_calendar": {}}
        zone = recorder.NEW_YORK
        start = datetime(2026, 8, 5, 12, 0, tzinfo=zone)
        end = datetime(2026, 8, 5, 18, 0, tzinfo=zone)
        recorder._record_session_calendar(index, calendar, start, end)
        self.assertEqual(index["session_calendar"]["2026-08-05"], {
            "status": "closed", "source": "alpaca_calendar"})
        self.assertEqual(health._market_session_status(index, start.timestamp()),
                         "closed")
        row = {"event_type": "bar_1m",
               "timestamp": "2026-08-05T14:00:00+00:00",
               "as_of": "2026-08-05T14:01:00+00:00"}
        self.assertFalse(recorder._inside_recorded_session(
            index, row, require_exact_calendar=True))
        self.assertEqual(recorder._recorded_session_rows(
            index, [row], require_exact_calendar=True), [])

    def test_recorder_health_holiday_no_data_is_live_but_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            now = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc).timestamp()
            (path / recorder.INDEX_NAME).write_text(json.dumps({
                "session_calendar": {
                    "2026-08-05": {
                        "status": "closed", "source": "alpaca_calendar",
                    }},
                "data_feed": "iex", "observation_watermarks": {},
            }), encoding="utf-8")
            (path / ".recorder-status.json").write_text(json.dumps({
                "status": "failed", "updated_ts": now,
                "failure_kind": "market_data_request_failed",
                "error": "Alpaca returned no point-in-time bars or quotes",
                "data_feed": "iex",
            }), encoding="utf-8")
            os.utime(path / recorder.INDEX_NAME, (now, now))
            os.utime(path / ".recorder-status.json", (now, now))
            result = health.recorder(path, 60, now=now,
                                     configured_data_feed="iex",
                                     configured_symbols=["SPY"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["service_liveness"]["ok"])
        self.assertEqual(result["market_session_status"], "closed")
        self.assertFalse(result["market_data_ready"])

    def test_recorder_discards_extended_rows_after_an_early_close(self):
        index = {"session_calendar": {"2026-08-07": {
            "open": "2026-08-07T13:30:00+00:00",
            "close": "2026-08-07T17:00:00+00:00",
            "source": "alpaca_calendar",
        }}}
        regular = {
            "event_type": "bar_1m", "timestamp": "2026-08-07T16:59:00+00:00",
            "as_of": "2026-08-07T17:00:00+00:00",
        }
        extended = {
            "event_type": "bar_1m", "timestamp": "2026-08-07T17:01:00+00:00",
            "as_of": "2026-08-07T17:02:00+00:00",
        }
        self.assertTrue(recorder._inside_recorded_session(index, regular))
        self.assertFalse(recorder._inside_recorded_session(index, extended))

    def test_recorder_exact_calendar_discards_known_extended_hours(self):
        index = {"session_calendar": {"2026-08-07": {
            "open": "2026-08-07T13:30:00+00:00",
            "close": "2026-08-07T17:00:00+00:00",
            "source": "alpaca_calendar",
        }}}
        extended = {
            "event_type": "quote", "timestamp": "2026-08-07T17:00:01+00:00",
        }
        self.assertTrue(recorder._has_exact_recorded_session(index, extended))
        self.assertEqual(recorder._recorded_session_rows(
            index, [extended], require_exact_calendar=True), [])

    def test_recorder_exact_calendar_still_fails_when_metadata_is_missing(self):
        row = {
            "event_type": "quote", "timestamp": "2026-08-07T16:00:00+00:00",
        }
        with self.assertRaisesRegex(
                RuntimeError, "exact broker calendar metadata missing or invalid"):
            recorder._recorded_session_rows(
                {"session_calendar": {}}, [row], require_exact_calendar=True)

    def test_recorder_exact_calendar_rejects_invalid_in_session_bar(self):
        index = {"session_calendar": {"2026-08-07": {
            "open": "2026-08-07T13:30:00+00:00",
            "close": "2026-08-07T17:00:00+00:00",
            "source": "alpaca_calendar",
        }}}
        row = {
            "event_type": "bar_1m", "timestamp": "2026-08-07T16:59:00+00:00",
            "as_of": "2026-08-07T17:01:00+00:00",
        }
        with self.assertRaisesRegex(
                RuntimeError, "invalid within exact broker session"):
            recorder._recorded_session_rows(
                index, [row], require_exact_calendar=True)

    def test_recorder_exact_catchup_skips_closed_intervals(self):
        calendar = recorder.CalendarCache(_CalendarFake())
        cursor = datetime(2026, 8, 4, 20, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 6, 14, 0, tzinfo=timezone.utc)
        request = recorder._next_exact_session_window(
            cursor, end, timedelta(minutes=1), calendar)
        self.assertEqual(request, (
            datetime(2026, 8, 6, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 6, 13, 31, tzinfo=timezone.utc)))

    def test_recorder_keeps_pinned_option_contracts_in_the_sample(self):
        from deploy import recorder_market
        fake = _OptionFake()
        quotes = fake.quotes(["SPY"], start=None, end=None, feed="iex")
        now = fake.option_timestamp + timedelta(minutes=1)
        expiry = datetime.now(timezone.utc).date() + timedelta(days=30)
        drifted = f"SPY{expiry:%y%m%d}C00105000"

        def sample(pinned=frozenset()):
            return [row["contract"] for row in recorder_market._option_rows(
                fake, ["SPY"], quotes, now, feed="iex", config=None, limit=2,
                pinned=pinned)]

        self.assertNotIn(drifted, sample())
        self.assertIn(drifted, sample(frozenset({drifted})))
        self.assertEqual(len(sample(frozenset({drifted}))), len(sample()) + 1)

    def test_recorder_discards_option_quotes_older_than_the_fetch_window(self):
        from deploy import recorder_market
        fake = _OptionFake()
        quotes = fake.quotes(["SPY"], start=None, end=None, feed="iex")
        now = datetime.now(timezone.utc)
        rows = list(recorder_market._option_rows(
            fake, ["SPY"], quotes, now, feed="iex", config=None, limit=2,
            minimum_timestamp=fake.option_timestamp + timedelta(seconds=1)))
        self.assertEqual(rows, [])

    def test_recorder_pins_sampled_contracts_until_the_hold_expires(self):
        fake = _OptionFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            recorder.record_once(fake, ["SPY"], path, include_options=True,
                                 option_limit=1)
            pins = recorder._load_index(path)["option_pins"]
            self.assertEqual(len(pins), 2)
            self.assertTrue(all(value > datetime.now(timezone.utc).isoformat()
                                for value in pins.values()))
            recorder.record_once(fake, ["SPY"], path, include_options=True,
                                 option_limit=1,
                                 option_hold=timedelta(seconds=-1))
            expired = recorder._load_index(path)["option_pins"]
            self.assertEqual(set(expired), set(pins))
            self.assertTrue(all(value < datetime.now(timezone.utc).isoformat()
                                for value in expired.values()))
            self.assertEqual(recorder.record_once(
                fake, ["SPY"], path, include_options=True, option_limit=1), 0)

    def test_recorder_records_bounded_lossless_option_snapshots(self):
        from research.market_data import normalize_option_snapshot
        fake = _OptionFake()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            config = {
                "universe": {"asset_classes": ["us_equity", "us_option"]},
                "risk": {"options_min_dte": 7, "options_max_dte": 60},
            }
            self.assertEqual(recorder.record_once(
                fake, ["SPY"], path, config=config, include_options=True), 12)
            self.assertEqual(recorder.record_once(
                fake, ["SPY"], path, config=config, include_options=True), 0)
            self.assertEqual(fake.option_calls,
                             [("SPY", 100.5, "opra", 7, 60),
                              ("SPY", 100.5, "opra", 7, 60)])
            rows = list(recorder.iter_corpus_rows(path))
            options = [row for row in rows if row["event_type"] == "option_snapshot"]
            self.assertEqual(len(options), 10)
            self.assertEqual({row["right"] for row in options}, {"call", "put"})
            expiry = datetime.now(timezone.utc).date() + timedelta(days=30)
            self.assertNotIn(
                f"SPY{expiry:%y%m%d}C00105000",
                {row["symbol"] for row in options})
            required = {"contract", "underlying", "expiration", "strike", "right",
                        "multiplier", "bid", "ask", "bid_size", "ask_size",
                        "volume", "open_interest", "observed_at", "timestamp"}
            self.assertTrue(required.issubset(options[0]))
            for row in options:
                payload = {key: (None if value == "" else value)
                           for key, value in row.items()}
                normalized = normalize_option_snapshot(
                    payload, provider=row["provider"], feed=row["feed"])
                self.assertEqual(normalized.contract.underlying, "SPY")
                self.assertEqual(normalized.contract.right, row["right"])

    def test_recorder_uses_provider_feed_over_config_and_records_it(self):
        fake = _MarketFake(feed="sip")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                "os.environ", {"ALPACA_DATA_FEED": "iex"}, clear=False):
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(fake, ["SPY"], path), 2)
            self.assertEqual(fake.seen, [("bars", "sip"), ("quotes", "sip")])
            self.assertTrue(all(row["feed"] == "sip"
                                for row in recorder.iter_corpus_rows(path)))

    def test_provider_env_feed_wins_over_iex_config_and_is_recorded(self):
        sdk = _StockDataFake()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                "os.environ", {"ALPACA_DATA_FEED": "iex"}, clear=False):
            provider = AlpacaProvider(
                {"mode": "paper", "broker": {"paper": True,
                                               "data_feed": "sip"}},
                session=AlpacaSession(api_key="key", secret_key="secret",
                                      paper=True, stock_data_client=sdk))
            path = Path(directory) / "market.csv"
            self.assertEqual(recorder.record_once(provider, ["SPY"], path), 2)
            self.assertEqual(provider.data_feed, "iex")
            for request in (sdk.bar_request, sdk.quote_request):
                value = getattr(request, "feed", None)
                if value is None and isinstance(request, dict):
                    value = request.get("feed")
                self.assertIn("iex", str(value).lower())
            self.assertTrue(all(row["feed"] == "iex"
                                for row in recorder.iter_corpus_rows(path)))

    def test_health_uses_running_paper_heartbeat_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": 100,
                "research_expected": False,
            }), encoding="utf-8")
            result = health.trader(path, max_age=30, now=100)
            self.assertTrue(result["ok"])

    def test_health_distinguishes_edge_gate_from_operator_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            path.write_text(json.dumps({
                "status": "paused", "updated_ts": 100,
                "reason": "validated_edge_required",
            }), encoding="utf-8")
            result = health.trader(path, max_age=30, now=100)
            self.assertTrue(result["ok"])
            self.assertTrue(result["edge_gate_pause"])
            self.assertFalse(result["operator_pause"])
            self.assertEqual(result["classification"],
                             "validated_edge_required")

            path.write_text(json.dumps({
                "status": "paused", "updated_ts": 100,
                "reason": "operator_resume_ready",
            }), encoding="utf-8")
            result = health.trader(path, max_age=30, now=100)
        self.assertTrue(result["ok"])
        self.assertFalse(result["edge_gate_pause"])
        self.assertTrue(result["operator_pause"])

    def test_recorder_health_uses_fresh_index_when_corpus_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "sessions" / "market-2026-08-07.csv"
            csv_path.parent.mkdir()
            csv_path.write_text("event_key\n", encoding="utf-8")
            index_path = root / ".recorder-index.json"
            index_path.write_text("{}", encoding="utf-8")
            os.utime(csv_path, (0, 0))
            os.utime(index_path, (900, 900))

            result = health.recorder(root, max_age=300, now=1000)

            self.assertTrue(result["ok"])
            self.assertTrue(result["fresh"])
            self.assertEqual(result["latest_csv_write_ts"], 0)
            self.assertEqual(result["index_write_ts"], 900)
            self.assertEqual(result["latest_write_ts"], 900)

    def test_recorder_health_exposes_readiness_counts_watermark_and_cadence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = 1000.0
            csv_path = root / "sessions" / "market-2026-08-07.csv"
            csv_path.parent.mkdir()
            csv_path.write_text("event_key\n", encoding="utf-8")
            (root / recorder.INDEX_NAME).write_text(json.dumps({
                "data_feed": "iex", "configured_symbols": ["SPY", "QQQ"],
                "watermark": "1970-01-01T00:16:30+00:00",
                "observation_watermarks": {
                    "SPY": {"quote": "1970-01-01T00:16:25+00:00",
                             "bar": "1970-01-01T00:16:25+00:00"},
                    "QQQ": {"quote": "1970-01-01T00:15:00+00:00",
                             "bar": "1970-01-01T00:15:00+00:00"},
                },
                "partition_sources": {
                    "market-2026-08-07.csv": {"source_mode": "forward"},
                },
            }), encoding="utf-8")
            (root / recorder.STATUS_NAME).write_text(json.dumps({
                "status": "recording", "updated_ts": now,
                "cadence": {"configured_interval_seconds": 30,
                             "realized_intervals_seconds": [30, 45],
                             "gap_seconds": 15, "gap_detected": True},
            }), encoding="utf-8")
            os.utime(csv_path, (now, now))
            os.utime(root / recorder.INDEX_NAME, (now, now))
            result = health.recorder(root, max_age=60, now=now,
                                     configured_symbols=["SPY", "QQQ"],
                                     configured_data_feed="iex")
        self.assertEqual(result["missing_quote_symbol_count"], 0)
        self.assertEqual(result["stale_quote_symbol_count"], 1)
        self.assertEqual(result["recorded_watermark"],
                         "1970-01-01T00:16:30+00:00")
        self.assertEqual(result["data_readiness"]["provenance"][
            "source_mode_counts"], {"forward": 1})
        self.assertTrue(result["cadence"]["gap_detected"])
        self.assertEqual(result["cadence"]["realized_interval_p95_seconds"], 45)

    def test_recorder_health_does_not_decode_oversized_legacy_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "sessions" / "market-2026-08-07.csv"
            csv_path.parent.mkdir()
            csv_path.write_text("event_key\n", encoding="utf-8")
            index_path = root / ".recorder-index.json"
            index_path.write_text(json.dumps({
                "data_feed": "iex", "recent_keys": {"x": "y"},
            }), encoding="utf-8")
            with patch.object(health, "MAX_RECORDER_INDEX_BYTES", 16):
                result = health.recorder(root, max_age=300)

            self.assertTrue(result["index_migration_pending"])
            self.assertIsNone(result["data_feed"])

    def test_recorder_health_is_stale_when_index_and_corpus_are_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "market.csv"
            csv_path.write_text("event_key\n", encoding="utf-8")
            index_path = root / ".recorder-index.json"
            index_path.write_text("{}", encoding="utf-8")
            os.utime(csv_path, (0, 0))
            os.utime(index_path, (0, 0))

            result = health.recorder(root, max_age=300, now=1000)

            self.assertFalse(result["ok"])
            self.assertFalse(result["fresh"])
            self.assertEqual(result["status"], "stale_or_empty")

    def test_recorder_health_accepts_progress_after_a_retry_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "market.csv"
            csv_path.write_text("event_key\n", encoding="utf-8")
            index_path = root / ".recorder-index.json"
            index_path.write_text("{}", encoding="utf-8")
            status_path = root / ".recorder-status.json"
            status_path.write_text(json.dumps({
                "schema": "recorder-status.v1",
                "status": "failed",
                "updated_ts": 800,
                "failure_kind": "market_data_request_failed",
                "retryable": True,
                "error": "older retry failed",
            }), encoding="utf-8")
            os.utime(csv_path, (700, 700))
            os.utime(index_path, (900, 900))

            result = health.recorder(root, max_age=300, now=1000)

            self.assertTrue(result["ok"])
            self.assertTrue(result["fresh"])
            self.assertEqual(result["status"], "recording")

    def test_recorder_health_requires_a_csv_corpus_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index_path = root / ".recorder-index.json"
            index_path.write_text("{}", encoding="utf-8")
            os.utime(index_path, (900, 900))

            result = health.recorder(root, max_age=300, now=1000)

            self.assertFalse(result["ok"])
            self.assertTrue(result["fresh"])
            self.assertEqual(result["series_files"], 0)

    def test_recorder_health_surfaces_a_permanent_iex_entitlement_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "market.csv"
            csv_path.write_text("event_key\n", encoding="utf-8")
            index_path = root / ".recorder-index.json"
            index_path.write_text(json.dumps({"data_feed": "iex"}),
                                  encoding="utf-8")
            status_path = root / ".recorder-status.json"
            status_path.write_text(json.dumps({
                "schema": "recorder-status.v1",
                "status": "failed",
                "updated_ts": 995,
                "data_feed": "iex",
                "failure_kind": "iex_entitlement_required",
                "retryable": False,
                "error": "subscription does not permit querying recent IEX data",
            }), encoding="utf-8")
            os.utime(csv_path, (995, 995))
            os.utime(index_path, (995, 995))

            result = health.recorder(root, max_age=300, now=1000)

            self.assertFalse(result["ok"])
            self.assertTrue(result["fresh"])
            self.assertEqual(result["status"], "iex_entitlement_required")
            self.assertEqual(result["failure_kind"],
                             "iex_entitlement_required")
            self.assertFalse(result["retryable"])
            self.assertIn("recent IEX", result["last_error"])

    def test_market_data_probe_does_not_mutate_the_corpus(self):
        fake = _MarketFake()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "market.csv"

            result = recorder.probe_market_data(
                fake, ["SPY"], config={"universe": {"asset_classes": []}})

            self.assertEqual(result["status"], "probe_ok")
            self.assertEqual(result["data_feed"], "iex")
            self.assertEqual(result["event_counts"],
                             {"bar_1m": 1, "quote": 1})
            self.assertFalse(output.exists())
            self.assertFalse((root / "sessions").exists())

    def test_subscription_error_is_classified_as_non_retryable(self):
        kind, retryable = recorder._market_data_failure(
            RuntimeError(
                "subscription does not permit querying recent IEX data"),
            data_feed="iex", options_feed="opra")
        self.assertEqual(kind, "iex_entitlement_required")
        self.assertFalse(retryable)

    def test_shipped_defaults_use_free_basic_iex_equity_lane(self):
        config = json.loads((Path(__file__).resolve().parents[1] /
                             "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["broker"]["data_feed"], "iex")
        self.assertEqual(config["broker"]["options_feed"], "indicative")
        self.assertEqual(config["universe"]["asset_classes"], ["us_equity"])
        self.assertEqual(config["strategy"]["execution_mode"], "shares")
        self.assertEqual(
            Path(__file__).resolve().parents[1].joinpath(
                ".env.example").read_text(encoding="utf-8").split(
                    "ALPACA_RESEARCH_VEHICLES=", 1)[1].splitlines()[0],
            "equity")

    def test_research_cycle_option_lane_still_requires_opra(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "market.csv"
            dataset.write_text(_replay_corpus_csv(), encoding="utf-8")
            result = _run_research_cycle(
                dataset, root, ALPACA_OPTIONS_FEED="indicative")
            self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
            self.assertIn("OPRA", result.stdout)

    def test_scheduler_accepts_paper_config_without_secret_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("mode: paper\nbroker:\n  paper: true\n",
                              encoding="utf-8")
            self.assertEqual(scheduler.configured_mode(config), "paper")
        self.assertNotIn("load_secrets", Path(scheduler.__file__).read_text())

    def test_dashboard_does_not_claim_promotion_when_research_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime").mkdir()
            (root / "config.yaml").write_text(
                "mode: paper\nstrategy:\n  id: ibr\n  version: v1\n",
                encoding="utf-8")
            snapshot = dashboard.snapshot(root)
            self.assertTrue(snapshot["research"]["service_optional"])
            self.assertTrue(snapshot["research"]["entry_gate_required"])
            self.assertEqual(snapshot["recorder"]["configured_data_feed"], "iex")
            self.assertEqual(snapshot["recorder"]["configured_options_feed"],
                             "indicative")
            self.assertNotIn("gates", snapshot)

    def test_dashboard_html_renders_the_current_snapshot_contract(self):
        self.assertIn("d.strategy.execution_mode", dashboard.HTML)
        self.assertIn("d.recorder.configured_data_feed", dashboard.HTML)
        self.assertIn("d.research.entry_gate_required", dashboard.HTML)
        self.assertIn("d.research.service_optional", dashboard.HTML)
        self.assertIn("d.edge.proved_edges", dashboard.HTML)
        self.assertIn("d.edge.live_paper", dashboard.HTML)
        self.assertIn("Live paper results by edge", dashboard.HTML)
        self.assertIn("d.research.tradeable_vehicle", dashboard.HTML)
        self.assertIn("d.research.untradeable_proved_edges", dashboard.HTML)
        self.assertIn("cycle outcome", dashboard.HTML)
        self.assertIn("Execution journal", dashboard.HTML)
        self.assertIn("configured_risk_budget_usd", dashboard.HTML)
        self.assertIn("planned_to_configured_risk_ratio", dashboard.HTML)
        self.assertIn("delivered_to_configured_risk_ratio", dashboard.HTML)
        self.assertIn("Paper-account trials", dashboard.HTML)
        self.assertNotIn("d.research_feed_version", dashboard.HTML)
        self.assertNotIn("d.research.optional", dashboard.HTML)
        self.assertNotIn("d.trader.heartbeat.research_available", dashboard.HTML)

    def test_dashboard_renders_the_detailed_reporting_panels(self):
        """Every question the operator asked has a surface that answers it."""
        for marker in (
                # Which edge is earning a promotion, and what to paste.
                "d.trial", "Paper-account trials", "Promotable", "config_snippet",
                # What was pinned, and whether it can actually trade.
                "d.promotions", "Pinned promotions", "Pinned but NOT trading",
                "notify only",
                # Which strategy and variant each real fill came from.
                "d.journal", "Trades by edge", "Recent trades, attributed",
                "variant_id",
                # Why research tried what it tried.
                "d.learning", "What research learned", "built_on",
                # Every configuration change, by id.
                "d.config_audit", "Configuration audit trail",
                "config_version_id"):
            with self.subTest(marker=marker):
                self.assertIn(marker, dashboard.HTML)

    def test_dashboard_attributes_each_trade_to_its_edge(self):
        import sqlite3 as sqlite
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            with closing(sqlite.connect(journal)) as db, db:
                db.execute("""CREATE TABLE trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT,
                    side TEXT, action TEXT, qty REAL, price REAL, notional REAL,
                    realized_pnl_usd REAL, risk_usd REAL, pnl_pct REAL,
                    fill_status TEXT, setup_type TEXT, strategy_id TEXT,
                    strategy_version TEXT, variant_id TEXT, runtime_mode TEXT,
                    exit_policy TEXT, close_trigger TEXT)""")
                for index, (variant, pnl) in enumerate(
                        [("rule.a.1", 200.0), ("rule.a.1", -100.0),
                         ("rule.b.2", 50.0)]):
                    db.execute(
                        "INSERT INTO trades (ts,symbol,side,action,qty,price,"
                        "realized_pnl_usd,risk_usd,strategy_id,variant_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (1000.0 + index, "SPY", "buy", "close", 10, 100.0,
                         pnl, 100.0, "rule", variant))
            view = dashboard._journal_view(journal)
        self.assertTrue(view["available"])
        self.assertEqual(len(view["trades"]), 3)
        by_variant = {row["variant_id"]: row for row in view["by_variant"]}
        self.assertEqual(by_variant["rule.a.1"]["trades"], 2)
        self.assertEqual(by_variant["rule.a.1"]["total_r"], 1.0)
        self.assertEqual(by_variant["rule.a.1"]["win_rate"], 0.5)
        self.assertEqual(by_variant["rule.b.2"]["realized_pnl_usd"], 50.0)

    def test_dashboard_journal_view_degrades_without_a_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            view = dashboard._journal_view(Path(directory) / "absent.db")
        self.assertFalse(view["available"])
        self.assertEqual(view["trades"], [])

    def test_dashboard_read_only_wal_falls_back_only_without_pending_wal(self):
        class Connection:
            def __init__(self, broken=False):
                self.broken = broken
                self.closed = False
                self.row_factory = None

            def execute(self, statement):
                if self.broken and statement.startswith("SELECT 1"):
                    raise sqlite3.OperationalError("unable to open database file")
                return self

            def fetchone(self):
                return None

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            journal.touch()
            broken, fallback = Connection(True), Connection()
            calls = []

            def connect(database, **_kwargs):
                calls.append(database)
                return broken if len(calls) == 1 else fallback

            with patch.object(dashboard.sqlite3, "connect", side_effect=connect):
                opened = dashboard._ro_connect(journal)
            self.assertIs(opened, fallback)
            self.assertTrue(broken.closed)
            self.assertIn("immutable=1", calls[1])
            opened.close()

            journal.with_name("journal.db-wal").write_bytes(b"pending")
            calls.clear()
            broken = Connection(True)
            with patch.object(dashboard.sqlite3, "connect", return_value=broken):
                with self.assertRaises(sqlite3.OperationalError):
                    dashboard._ro_connect(journal)
            self.assertTrue(broken.closed)

    def test_dashboard_surfaces_the_config_audit_trail(self):
        import copy as copier
        from agent.config import DEFAULT_CONFIG
        from agent.governance import record_config_version

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            base = copier.deepcopy(DEFAULT_CONFIG)
            record_config_version(journal, base)
            base["risk"]["daily_loss_limit_pct"] = 3.0
            record_config_version(journal, base)
            audit = dashboard._config_audit(journal)
        self.assertTrue(audit["available"])
        self.assertEqual(len(audit["versions"]), 2)
        self.assertEqual(audit["current"], audit["versions"][0]["config_version_id"])
        self.assertIn("risk.daily_loss_limit_pct",
                      audit["versions"][0]["changed_paths"])

    def test_dashboard_reports_a_pin_that_cannot_trade(self):
        from research.edge_lab import init_ledger

        with tempfile.TemporaryDirectory() as directory:
            edge_db = Path(directory) / "edge.sqlite3"
            init_ledger(edge_db)
            from agent.config import DEFAULT_CONFIG as _DEFAULTS

            config = {**_DEFAULTS}
            config["strategy"] = {**config["strategy"],
                                  "selection_mode": "pinned",
                                  "pinned": [{"id": "pin-ghost",
                                              "variant_id": "rule.ghost.1",
                                              "vehicle": "equity",
                                              "strategy_id": "rule",
                                              "note": "", "promoted_at": ""}]}
            view = dashboard._promotions(config, edge_db)
        self.assertEqual(view["selection_mode"], "pinned")
        self.assertTrue(view["frozen"])
        self.assertEqual(len(view["pinned"]), 1)
        self.assertEqual(view["unresolved"][0]["id"], "pin-ghost")

    def test_dashboard_exposes_edge_ledger_status(self):
        from research.edge_lab import EdgeLedger, init_ledger
        from tests.research.test_edge_discovery import _persist_gate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime" / "research").mkdir(parents=True)
            (root / "config.yaml").write_text(
                "mode: paper\nstrategy:\n  id: ibr\n  version: v1\n",
                encoding="utf-8")
            ledger_path = root / "runtime" / "research" / "edge_lab.sqlite3"
            init_ledger(ledger_path)
            ledger = EdgeLedger(ledger_path)
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="dashboard proof visibility",
                config={"strategy": {"target_r": 1.5}})
            _persist_gate(ledger, candidate["candidate_id"], "shadow")
            with closing(sqlite3.connect(ledger_path)) as db, db:
                db.execute("UPDATE candidate_state SET status='validated' WHERE candidate_id=?",
                           (candidate["candidate_id"],))
            result = dashboard._edge_status(ledger_path)
            self.assertTrue(result["available"])
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["proved_edges"][0]["variant_id"],
                             "ibr.target.1_5r")
            _persist_gate(ledger, candidate["candidate_id"], "shadow",
                          passes=False)
            self.assertEqual(dashboard._edge_status(ledger_path)["proved_edges"], [])

    def test_dashboard_reports_live_paper_results_for_each_edge(self):
        """Proof confidence is what the evidence was; this is how it is doing."""
        from research.edge_lab import EdgeLedger, init_ledger
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "edge_lab.sqlite3"
            init_ledger(ledger_path)
            ledger = EdgeLedger(ledger_path)
            # No outcomes yet: the key exists so the card can render empty.
            self.assertEqual(dashboard._edge_status(ledger_path)["live_paper"], [])
            for variant, values in (("rule.mean-reversion.win", [1.0, 1.0, .5]),
                                    ("rule.trend-pullback.lose", [-1.0, -.5])):
                record = ledger.register_candidate(
                    variant, strategy_id="rule", vehicle="equity",
                    hypothesis="live paper visibility",
                    config={"strategy": {"rule_spec": {"family": "mean_reversion"}}})
                for index, value in enumerate(values):
                    ledger.ingest_paper_outcome(record["candidate_id"], {
                        "opportunity_id": f"{variant}:{index}",
                        "session_date": f"2026-06-{index + 1:02d}",
                        "net_pnl": value * 100.0, "risk_usd": 100.0})
            live = dashboard._edge_status(ledger_path)["live_paper"]
            self.assertEqual([row["variant_id"] for row in live],
                             ["rule.mean-reversion.win", "rule.trend-pullback.lose"])
            winner = live[0]
            self.assertEqual(winner["outcomes"], 3)
            self.assertEqual(winner["sessions"], 3)
            self.assertEqual(winner["last_session"], "2026-06-03")
            self.assertAlmostEqual(winner["total_r"], 2.5)
            self.assertAlmostEqual(winner["net_pnl"], 250.0)
            self.assertEqual(winner["guard"], "3/20")
            self.assertFalse(winner["rolling_authoritative"])
            self.assertEqual(winner["rolling_action"], "warning_only")

    def test_dashboard_paper_guard_thresholds_track_the_ledger(self):
        """The dashboard restates the advisory monitor without importing it."""
        from research.edge_ledger import (PAPER_DEMOTION_MIN_OUTCOMES,
                                          PAPER_DEMOTION_R_FLOOR)
        self.assertEqual(dashboard.PAPER_ROLLING_WINDOW,
                         PAPER_DEMOTION_MIN_OUTCOMES)
        self.assertEqual(dashboard.PAPER_ROLLING_FLOOR, PAPER_DEMOTION_R_FLOOR)

    def test_report_is_a_small_usd_paper_summary(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript("""
                CREATE TABLE trades (ts REAL, action TEXT, symbol TEXT,
                    realized_pnl_usd REAL, pnl_pct REAL);
                CREATE TABLE equity (ts REAL, equity REAL, state TEXT);
                INSERT INTO trades VALUES (1, 'open', 'SPY', NULL, NULL);
                INSERT INTO trades VALUES (2, 'close', 'SPY', 12.5, 0.25);
                INSERT INTO equity VALUES (1, 1000, 'running');
                INSERT INTO equity VALUES (2, 1012.5, 'running');
            """)
            summary = report.json_report(db)
            self.assertEqual(summary["scope"], "alpaca-paper")
            self.assertEqual(summary["closed_trades"], 1)
            self.assertEqual(summary["realized_pnl_usd"], 12.5)
            self.assertNotIn("promotion", json.dumps(summary))

    def test_compose_is_paper_only_and_research_shadow_start_by_default(self):
        text = Path("compose.yaml").read_text(encoding="utf-8")
        self.assertIn("ALPACA_PAPER: \"true\"", text)
        self.assertIn("ALPACA_LIVE_ENABLE: \"false\"", text)
        self.assertIn("  research:\n", text)
        self.assertIn("  shadow:\n", text)
        self.assertNotIn("profiles:", text)
        self.assertIn("ALPACA_RESEARCH_DATASET", text)

    def test_shadow_retention_defaults_are_shared_and_explicit(self):
        self.assertEqual(shadow_service.parser().parse_args([]).retention_days, 180)
        text = Path("compose.yaml").read_text(encoding="utf-8")
        self.assertIn("--retention-days", text)
        self.assertIn("${ALPACA_SHADOW_RETENTION_DAYS:-180}", text)

    def test_shadow_health_exposes_retention_and_stale_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.json"
            path.write_text(json.dumps({
                "status": "running", "updated_ts": 100,
                "retention_days": 180, "retention_floor_ts": 1.0,
                "pruned_replay_diffs": 2,
                "stale_tail": {"status": "blocked", "sessions": ["2026-01-02"]},
            }), encoding="utf-8")
            result = health.shadow(path, 60, now=100)
        self.assertEqual(result["retention_days"], 180)
        self.assertEqual(result["pruned_replay_diffs"], 2)
        self.assertEqual(result["stale_tail"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
