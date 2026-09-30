"""Read-only inspection of frozen paper runtimes."""

from __future__ import annotations

from copy import deepcopy
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import main
from agent import state
from agent.alpaca_domain import Account, MarketClock
from agent.config import DEFAULT_CONFIG, validate_config
from agent.paper_trial import build_descriptor, new_state
from agent.runtime_inspection import inspect_runtime
from research.diagnostic_shadow import _logical_arms


OLD_CODE = "1" * 64
CURRENT_CODE = "2" * 64


def _tree_snapshot(root: Path) -> dict[str, tuple[bool, int, bytes | None]]:
    rows = {
        ".": (True, root.stat().st_mtime_ns, None),
    }
    for path in sorted(root.rglob("*")):
        rows[str(path.relative_to(root))] = (
            path.is_dir(),
            path.stat().st_mtime_ns,
            None if path.is_dir() else path.read_bytes(),
        )
    return rows


def _trial_config(report_root: Path) -> dict:
    cfg = deepcopy(DEFAULT_CONFIG)
    cfg["mode"] = "paper"
    cfg["broker"].update({
        "paper": True,
        "allow_live": False,
        "provider": "alpaca",
        "data_feed": "iex",
        "options_feed": "indicative",
    })
    cfg["strategy"].update({
        "id": "rule",
        "execution_mode": "shares",
    })
    cfg["universe"]["asset_classes"] = ["us_equity"]
    cfg["llm"]["enabled"] = False
    cfg["research"]["enabled"] = True
    cfg["research"]["require_validated_variant"] = True
    cfg["research"]["trial"]["enabled"] = True
    cfg["research"]["paper_trial"].update({
        "enabled": True,
        "trial_id": "paper-frozen-test",
        "variant_id": _logical_arms()[0]["variant_id"],
        "accepted_session_report_root": str(report_root),
        "max_review_sessions": 60,
    })
    return validate_config(cfg)


class _ReadOnlyProvider:
    paper = True
    endpoint = "https://paper-api.alpaca.markets"

    class Session:
        api_key = "paper-key"
        secret_key = "paper-secret"

    session = Session()

    def __init__(self, *, auth_error: bool = False):
        self.auth_error = auth_error
        self.calls: list[tuple[str, object]] = []

    def account(self):
        self.calls.append(("account", None))
        if self.auth_error:
            raise RuntimeError(
                "authorization=paper-secret api_key=paper-key denied")
        return Account(
            "paper-account", "active", Decimal("100000"),
            Decimal("100000"), Decimal("200000"))

    def positions(self):
        self.calls.append(("positions", None))
        return []

    def orders(self, *, status=None):
        self.calls.append(("orders", status))
        return []

    def clock(self):
        self.calls.append(("clock", None))
        return MarketClock(datetime.now(timezone.utc), True)

    def submit_order(self, *_args, **_kwargs):  # pragma: no cover - safety tripwire
        raise AssertionError("read-only inspection submitted an order")

    def cancel_all_orders(self):  # pragma: no cover - safety tripwire
        raise AssertionError("read-only inspection cancelled orders")

    def close_position(self, *_args, **_kwargs):  # pragma: no cover - safety tripwire
        raise AssertionError("read-only inspection closed a position")


class RuntimeInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="runtime-inspection-")
        self.root = Path(self.tmp.name)
        self.runtime_base = self.root / "runtime"
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.config = _trial_config(self.reports)
        self.original_runtime_base = state.RUNTIME_BASE
        self.include_ibr = mock.patch.dict(
            os.environ, {"ALPACA_SHADOW_INCLUDE_IBR": "0"})
        self.include_ibr.start()
        state.RUNTIME_BASE = self.runtime_base
        state.configure_runtime("paper")
        state.ensure_ready()
        self.fingerprint = state.account_fingerprint(
            "paper", "paper-key\0paper-account")
        with mock.patch(
                "agent.paper_trial.runtime_code_identity",
                return_value=OLD_CODE):
            descriptor = build_descriptor(self.config)
        trial = new_state(descriptor, self.config)
        trial.update({
            "activation_confirmed": True,
            "activation_account_fingerprint": self.fingerprint,
            "started_on": "2026-09-14",
        })
        state.save_state({
            **state.load_state(),
            "state": state.PAUSED,
            "runtime_mode": "paper",
            "account_fingerprint": self.fingerprint,
            "operator_pause": True,
            "active_trades": {},
            "protection": {},
            "orders": {},
            "risk_day": {"limit_hit": False},
            "paper_trial": trial,
        })
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.include_ibr.stop()
        state.RUNTIME_BASE = self.original_runtime_base
        state.configure_runtime("paper")
        self.tmp.cleanup()

    def _inspect(self, provider, *, deployment=True):
        with mock.patch(
                "agent.paper_trial.runtime_code_identity",
                return_value=CURRENT_CODE):
            return inspect_runtime(
                self.config, provider=provider, deployment=deployment,
                runtime_base=self.runtime_base)

    def test_drifted_paused_flat_inspection_is_get_only_and_byte_immutable(self):
        provider = _ReadOnlyProvider()
        before = _tree_snapshot(self.runtime_base)

        result = self._inspect(provider)

        after = _tree_snapshot(self.runtime_base)
        self.assertEqual(before, after)
        self.assertEqual(provider.calls, [
            ("account", None),
            ("positions", None),
            ("orders", "open"),
            ("clock", None),
        ])
        self.assertTrue(result["paused_flat_verified"])
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["broker_flat"])
        self.assertFalse(result["entry_eligible"])
        self.assertFalse(result["identity_match"])
        self.assertIn(
            "paper_trial_identity_mismatch", result["readiness_blockers"])
        self.assertEqual(
            result["frozen_trial_identity"]["code_identity"], OLD_CODE)
        self.assertEqual(
            result["expected_trial_identity"]["code_identity"], CURRENT_CODE)
        self.assertEqual(
            result["frozen_trial_identity"]["accepted_sessions"], 0)
        self.assertEqual(
            result["frozen_trial_identity"]["closed_outcomes"], 0)

    def test_auth_failure_preserves_local_pause_and_redacts_credentials(self):
        provider = _ReadOnlyProvider(auth_error=True)

        result = self._inspect(provider)

        self.assertTrue(result["operator_paused"])
        self.assertTrue(result["local_flat"])
        self.assertIsNotNone(result["frozen_trial_identity"])
        self.assertFalse(result["broker_flat"])
        self.assertFalse(result["paused_flat_verified"])
        self.assertIn("broker_authentication_failed", result["readiness_blockers"])
        self.assertIn("<redacted>", result["auth_error"])
        self.assertNotIn("paper-secret", result["auth_error"])
        self.assertNotIn("paper-key", result["auth_error"])
        self.assertEqual(provider.calls, [("account", None)])

    def test_cli_drift_paths_bypass_engine_and_keep_runtime_immutable(self):
        provider = _ReadOnlyProvider()
        before = _tree_snapshot(self.runtime_base)
        output = StringIO()
        with mock.patch(
                "agent.paper_trial.runtime_code_identity",
                return_value=CURRENT_CODE), \
                mock.patch.object(main, "_provider", return_value=provider), \
                mock.patch.object(main, "_engine") as engine_factory, \
                redirect_stdout(output), redirect_stderr(StringIO()):
            self.assertEqual(main.cmd_status(SimpleNamespace(), self.config), 1)
            self.assertEqual(main.cmd_check(
                SimpleNamespace(authenticated=True), self.config), 1)
            self.assertEqual(main.cmd_inspect_deployment(
                SimpleNamespace(), self.config), 0)

        engine_factory.assert_not_called()
        after = _tree_snapshot(self.runtime_base)
        self.assertEqual(before, after)
        self.assertIn("paper_trial_identity_mismatch", output.getvalue())
        self.assertTrue(
            "paused_flat_verified: true" in output.getvalue() or
            '"paused_flat_verified": true' in output.getvalue())

    def test_missing_and_corrupt_journal_are_explicit_blockers(self):
        state.JOURNAL_FILE.unlink()
        missing = self._inspect(_ReadOnlyProvider(), deployment=False)
        self.assertFalse(missing["journal_ready"])
        self.assertIn("runtime_journal_missing", missing["readiness_blockers"])
        self.assertFalse(missing["status_ok"])

        state.JOURNAL_FILE.write_bytes(b"not a sqlite database")
        corrupt = self._inspect(_ReadOnlyProvider(), deployment=False)
        self.assertFalse(corrupt["journal_ready"])
        self.assertIn("runtime_journal_invalid", corrupt["readiness_blockers"])
        self.assertFalse(corrupt["status_ok"])

    def test_closed_wal_journal_inspection_creates_no_source_sidecars(self):
        wal = Path(f"{state.JOURNAL_FILE}-wal")
        shm = Path(f"{state.JOURNAL_FILE}-shm")
        wal.unlink(missing_ok=True)
        shm.unlink(missing_ok=True)
        before = _tree_snapshot(self.runtime_base)

        result = self._inspect(_ReadOnlyProvider(), deployment=False)

        self.assertTrue(result["journal_ready"])
        self.assertEqual(before, _tree_snapshot(self.runtime_base))
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())

    def test_rogue_journal_table_is_rejected(self):
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            db.execute(
                "CREATE TABLE rogue_table "
                "(id INTEGER PRIMARY KEY, payload TEXT)")
            db.commit()
        before = _tree_snapshot(self.runtime_base)

        result = self._inspect(_ReadOnlyProvider())

        self.assertEqual(before, _tree_snapshot(self.runtime_base))
        self.assertFalse(result["journal_ready"])
        self.assertIn("runtime_journal_invalid", result["readiness_blockers"])
        self.assertFalse(result["paused_flat_verified"])

    def test_active_wal_schema_is_read_from_snapshot_without_source_writes(self):
        wal = Path(f"{state.JOURNAL_FILE}-wal")
        shm = Path(f"{state.JOURNAL_FILE}-shm")
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "CREATE TABLE rogue_wal_table "
                "(id INTEGER PRIMARY KEY, payload TEXT)")
            writer.commit()
            self.assertTrue(wal.is_file())
            self.assertTrue(shm.is_file())
            before = _tree_snapshot(self.runtime_base)

            result = self._inspect(_ReadOnlyProvider())

            self.assertEqual(before, _tree_snapshot(self.runtime_base))
            self.assertFalse(result["journal_ready"])
            self.assertIn(
                "runtime_journal_invalid", result["readiness_blockers"])
            self.assertFalse(result["paused_flat_verified"])

    def test_omitted_safety_fields_cannot_normalize_to_flat(self):
        payload = json.loads(state.STATE_FILE.read_text(encoding="utf-8"))
        for key in ("active_trades", "protection", "orders", "risk_day"):
            payload.pop(key)
        state.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")

        result = self._inspect(_ReadOnlyProvider())

        self.assertFalse(result["runtime_state_ready"])
        self.assertFalse(result["local_book_valid"])
        self.assertFalse(result["local_flat"])
        self.assertFalse(result["paused_flat_verified"])
        self.assertIn("runtime_state_unreadable", result["readiness_blockers"])
        for key in ("active_trades", "protection", "orders", "risk_day"):
            self.assertIn(key, result["state_error"])

    def test_selection_is_not_armed_without_journal_and_authentication(self):
        state.update_state(lambda current: {
            **current,
            "state": state.RUNNING,
            "operator_pause": False,
        })
        state.JOURNAL_FILE.unlink()
        with mock.patch(
                "agent.paper_trial.runtime_code_identity",
                return_value=OLD_CODE):
            result = inspect_runtime(
                self.config,
                provider=_ReadOnlyProvider(auth_error=True),
                deployment=False,
                runtime_base=self.runtime_base,
            )

        self.assertFalse(result["journal_ready"])
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["status_ok"])
        self.assertFalse(result["paper_selection"]["armed"])
        self.assertIn("runtime_journal_missing", result["readiness_blockers"])
        self.assertIn(
            "broker_authentication_failed", result["readiness_blockers"])

    def test_stale_broker_clock_cannot_verify_paused_flat(self):
        class StaleProvider(_ReadOnlyProvider):
            def clock(self):
                self.calls.append(("clock", None))
                return MarketClock(
                    datetime(2000, 1, 1, tzinfo=timezone.utc), False)

        result = self._inspect(StaleProvider())

        self.assertIn("broker_clock_stale", result["readiness_blockers"])
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["paused_flat_verified"])
        self.assertFalse(result["status_ok"])

    def test_check_rejects_missing_or_corrupt_trial_state_without_engine(self):
        for corruption in ("missing", "corrupt"):
            with self.subTest(corruption=corruption):
                if corruption == "missing":
                    state.STATE_FILE.unlink()
                else:
                    state.STATE_FILE.write_text("{broken", encoding="utf-8")
                with mock.patch.object(main, "_engine") as engine_factory, \
                        mock.patch.object(main, "_provider") as provider_factory, \
                        redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    code = main.cmd_check(
                        SimpleNamespace(authenticated=True), self.config)
                self.assertEqual(code, 1)
                engine_factory.assert_not_called()
                provider_factory.assert_not_called()
                if corruption == "missing":
                    state.STATE_FILE.write_text("{broken", encoding="utf-8")

    def test_corrupt_state_is_reported_without_replacement(self):
        payload = json.loads(state.STATE_FILE.read_text(encoding="utf-8"))
        payload.pop("operator_pause")
        state.STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        before = state.STATE_FILE.read_bytes()

        result = self._inspect(_ReadOnlyProvider(), deployment=False)

        self.assertEqual(state.STATE_FILE.read_bytes(), before)
        self.assertFalse(result["runtime_state_ready"])
        self.assertIn("runtime_state_unreadable", result["readiness_blockers"])
        self.assertIn("operator_pause", result["state_error"])
        self.assertFalse(result["status_ok"])

    def test_daily_stop_is_visible_and_cannot_pass_deployment_inspection(self):
        state.update_state(lambda current: {
            **current,
            "state": state.DAY_STOPPED,
            "risk_day": {"limit_hit": True},
        })

        result = self._inspect(_ReadOnlyProvider())

        self.assertIn("runtime_state_day_stopped", result["readiness_blockers"])
        self.assertIn("daily_risk_limit_hit", result["readiness_blockers"])
        self.assertFalse(result["paused_flat_verified"])
        self.assertFalse(result["status_ok"])


if __name__ == "__main__":
    unittest.main()
