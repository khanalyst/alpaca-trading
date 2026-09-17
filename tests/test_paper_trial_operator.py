"""Audited operator cancellation never creates or mutates broker orders."""

from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent import state
import agent.paper_trial_operator as operator
from agent.state_store import StateCorruptionError
from agent.paper_trial import PaperTrialRuntime, public_status, refresh_state
from agent.paper_trial_operator import (
    AUDIT_KIND,
    MAX_BROKER_SNAPSHOT_SECONDS,
    PaperTrialOperatorError,
    cancel_paper_trial,
)


def _config() -> dict:
    return {
        "mode": "paper",
        "broker": {
            "paper": True, "allow_live": False, "api_key": "paper-key",
        },
    }


class _Provider:
    paper = True
    endpoint = "https://paper-api.alpaca.markets"

    class Session:
        api_key = "paper-key"

    session = Session()

    def __init__(self, *, positions=None, orders=None):
        self.positions_rows = [] if positions is None else positions
        self.order_rows = [] if orders is None else orders
        self.calls: list[tuple] = []

    def account(self):
        self.calls.append(("account",))
        return {"id": "paper-account", "status": "active"}

    def positions(self):
        self.calls.append(("positions",))
        return self.positions_rows

    def orders(self, *, status=None):
        self.calls.append(("orders", status))
        return self.order_rows


class PaperTrialOperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paper-trial-operator-")
        self.root = Path(self.tmp.name)
        self.original_base = state.RUNTIME_BASE
        self.original_scope = state.RUNTIME_SCOPE
        state.RUNTIME_BASE = self.root / "runtime"
        state.configure_runtime("paper")
        state.ensure_ready()
        self.fingerprint = state.account_fingerprint(
            "paper", "paper-key\0paper-account")
        self.trial = {
            "schema": "paper-incumbent-trial.v1",
            "state": "running", "trial_id": "trial-old",
            "candidate_id": "candidate-old", "variant_id": "variant-old",
            "family": "rule", "role": "control",
            "incumbent_identity": "identity-old", "policy_identity": "policy-old",
            "spec_identity": "spec-old", "code_identity": "code-old",
            "cohort_identity": "cohort-old", "include_ibr": False,
            "report_identities": {"deployment": None, "code": "code-old",
                                   "cohort": "cohort-old", "activation": None},
            "activation_confirmed": True,
            "activation_account_fingerprint": self.fingerprint,
            "started_on": "2026-09-01",
            "accepted_session_report_root": str(self.root / "reports"),
            "max_review_sessions": 60, "required_sessions": 20,
            "required_trades": 20, "accepted_sessions": [], "outcomes": [],
            "verdict": {"state": "running", "sessions": 0, "trades": 0,
                        "sessions_required": 20, "trades_required": 20},
            "blockers": [], "authorizing": False, "proof_authority": False,
        }
        state.save_state({
            **state.load_state(), "state": state.DAY_STOPPED,
            "runtime_mode": "paper", "account_fingerprint": self.fingerprint,
            "operator_pause": True, "kill_reason": "daily_stop",
            "active_trades": {}, "protection": {}, "orders": {},
            "paper_trial": self.trial,
        })
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        state.RUNTIME_BASE = self.original_base
        state.configure_runtime(self.original_scope)
        self.tmp.cleanup()

    def _cancel(self, provider=None, **overrides):
        provider = provider or _Provider()
        args = {
            "confirm_trial_id": "trial-old",
            "confirm_incumbent_identity": "identity-old",
            "reason": "requested config correction",
        }
        args.update(overrides)
        return cancel_paper_trial(
            _config(), provider_factory=lambda _config: provider, **args)

    def test_cancel_is_audited_and_preserves_runtime_and_old_verdict(self):
        before = state.load_state()
        provider = _Provider()
        result = self._cancel(provider)
        after = state.load_state()

        self.assertEqual(result["status"], "operator_cancelled")
        self.assertEqual(after["state"], state.DAY_STOPPED)
        self.assertEqual(after["kill_reason"], "daily_stop")
        self.assertTrue(after["operator_pause"])
        self.assertEqual(after["paper_trial"]["state"], "operator_cancelled")
        self.assertEqual(after["paper_trial"]["verdict"], before["paper_trial"]["verdict"])
        self.assertEqual(after["paper_trial"]["accepted_sessions"], [])
        self.assertEqual(after["paper_trial"]["outcomes"], [])
        self.assertIn("paper_trial_operator_cancelled",
                      public_status(after["paper_trial"])["blockers"])
        self.assertEqual(provider.calls,
                         [("account",), ("positions",), ("orders", "open")])
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            rows = db.execute(
                "SELECT kind, run_id, payload FROM events WHERE kind=?",
                (AUDIT_KIND,)).fetchall()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0][2])
        self.assertEqual(payload["audit_id"], rows[0][1])
        self.assertEqual(payload["trial"]["trial_id"], "trial-old")
        self.assertEqual(payload["account_binding"]["account_fingerprint"],
                         self.fingerprint)
        self.assertNotIn("paper-key", json.dumps(payload))
        self.assertNotIn("paper-account", json.dumps(payload))

    def test_retry_reuses_audit_without_duplicate_or_order_calls(self):
        first_provider = _Provider()
        first = self._cancel(first_provider)
        second_provider = _Provider()
        second = self._cancel(second_provider)
        self.assertEqual(second["audit_id"], first["audit_id"])
        self.assertEqual(second_provider.calls,
                         [("account",), ("positions",), ("orders", "open")])
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM events WHERE kind=?", (AUDIT_KIND,)
            ).fetchone()[0], 1)

    def test_state_write_failure_leaves_old_state_then_retry_reuses_audit(self):
        original_write = state._atomic_write
        failed = []

        def fail_once(path, value):
            if not failed:
                failed.append(True)
                raise OSError("state write unavailable")
            return original_write(path, value)

        with mock.patch.object(state, "_atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                self._cancel()
        self.assertEqual(state.load_state()["paper_trial"]["state"], "running")
        result = self._cancel()
        self.assertEqual(result["status"], "operator_cancelled")
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM events WHERE kind=?", (AUDIT_KIND,)
            ).fetchone()[0], 1)

    def test_journal_failure_leaves_state_unchanged(self):
        before = state.load_state()
        with mock.patch.object(state, "log_event",
                               side_effect=OSError("journal unavailable")):
            with self.assertRaises(PaperTrialOperatorError):
                self._cancel()
        self.assertEqual(state.load_state(), before)

    def test_guards_reject_unpaused_identity_evidence_and_exposure(self):
        state.update_state({"operator_pause": False})
        with self.assertRaisesRegex(PaperTrialOperatorError, "operator_pause"):
            self._cancel()
        state.update_state({"operator_pause": True})
        with self.assertRaisesRegex(PaperTrialOperatorError, "identity"):
            self._cancel(confirm_incumbent_identity="wrong")
        state.update_state(lambda current: {
            **current,
            "paper_trial": {**current["paper_trial"],
                             "accepted_sessions": [{"date": "2026-09-01"}]},
        })
        with self.assertRaisesRegex(PaperTrialOperatorError, "evidence-empty"):
            self._cancel()

    def test_broker_snapshot_and_paper_guards_reject_without_order_actions(self):
        with self.assertRaisesRegex(PaperTrialOperatorError, "broker-flat"):
            self._cancel(_Provider(positions=[{"symbol": "SPY"}]))
        with self.assertRaisesRegex(PaperTrialOperatorError, "broker-flat"):
            self._cancel(_Provider(orders=[{"id": "open"}]))
        provider = _Provider()
        provider.paper = False
        with self.assertRaisesRegex(PaperTrialOperatorError, "paper endpoint"):
            self._cancel(provider)

    def test_endpoint_guard_rejects_hostname_confusion_and_url_metadata(self):
        deceptive = (
            "https://paper-api.alpaca.markets.evil.example",
            "https://paper-api.alpaca.markets@evil.example",
            "http://paper-api.alpaca.markets",
            "https://paper-api.alpaca.markets:443",
            "https://paper-api.alpaca.markets/?query=secret",
            "https://paper-api.alpaca.markets/#fragment",
        )
        for endpoint in deceptive:
            with self.subTest(endpoint=endpoint):
                provider = _Provider()
                provider.endpoint = endpoint
                with self.assertRaisesRegex(PaperTrialOperatorError,
                                             "paper endpoint"):
                    self._cancel(provider)
                self.assertEqual(provider.calls, [])

    def test_endpoint_guard_accepts_canonical_paper_url(self):
        provider = _Provider()
        provider.endpoint = "https://paper-api.alpaca.markets"
        result = self._cancel(provider)
        self.assertEqual(result["status"], "operator_cancelled")

    def test_endpoint_guard_rejects_provider_with_no_endpoint_metadata(self):
        provider = _Provider()
        provider.endpoint = None
        with self.assertRaisesRegex(PaperTrialOperatorError,
                                     "endpoint metadata"):
            self._cancel(provider)
        self.assertEqual(provider.calls, [])

    def test_lock_is_acquired_before_provider_creation(self):
        handle = state.acquire_run_lock()
        self.assertIsNotNone(handle)
        self.addCleanup(state.release_run_lock, handle)
        created = []

        def factory(_config):
            created.append(True)
            return _Provider()

        with self.assertRaisesRegex(PaperTrialOperatorError, "lock"):
            cancel_paper_trial(
                _config(), confirm_trial_id="trial-old",
                confirm_incumbent_identity="identity-old", reason="retry",
                provider_factory=factory)
        self.assertEqual(created, [])

    def test_snapshot_freshness_is_bounded_after_all_gets(self):
        provider = _Provider()
        with mock.patch(
                "agent.paper_trial_operator.time.monotonic",
                side_effect=[100.0, 100.0 + MAX_BROKER_SNAPSHOT_SECONDS + 1]):
            with self.assertRaisesRegex(PaperTrialOperatorError, "too old"):
                self._cancel(provider)
        self.assertEqual(state.load_state()["paper_trial"]["state"], "running")

    def test_malformed_broker_snapshot_is_rejected(self):
        provider = _Provider()
        provider.positions_rows = None
        with self.assertRaisesRegex(PaperTrialOperatorError, "malformed"):
            self._cancel(provider)

    def test_replacement_audit_retains_operator_cancellation_record(self):
        runtime = object.__new__(PaperTrialRuntime)
        runtime.descriptor = {
            "trial_id": "trial-new", "candidate_id": "candidate-new",
            "variant_id": "variant-new", "incumbent_identity": "identity-new",
            "policy_identity": "policy-new", "code_identity": "code-new",
            "cohort_identity": "cohort-new",
        }
        self._cancel()
        current = state.load_state()
        audit = runtime.replacement_audit(
            current, {"positions": [], "orders": []})
        self.assertIsNotNone(audit)
        self.assertEqual(
            audit["terminal"]["operator_cancellation"]["audit_id"],
            current["paper_trial"]["operator_cancellation"]["audit_id"])
        self.assertEqual(audit["terminal"]["operator_cancellation"]["reason"],
                         "requested config correction")

    def test_refresh_preserves_cancelled_trial_without_recomputing_verdict(self):
        self._cancel()
        before = deepcopy(state.load_state()["paper_trial"])
        descriptor = {key: before[key] for key in (
            "trial_id", "candidate_id", "variant_id", "incumbent_identity",
            "policy_identity", "spec_identity", "code_identity",
            "cohort_identity", "accepted_session_report_root",
            "max_review_sessions")}
        descriptor["include_ibr"] = before["include_ibr"]
        refreshed = refresh_state(before, descriptor, _config())
        self.assertEqual(refreshed, before)

    def test_missing_cancellation_record_fails_closed(self):
        self._cancel()
        malformed = state.load_state()
        malformed["paper_trial"].pop("operator_cancellation")
        state._atomic_write(state.STATE_FILE, malformed)
        with self.assertRaises(StateCorruptionError):
            state.load_state()

    def test_tampered_audit_payload_fails_closed(self):
        self._cancel()
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            row = db.execute(
                "SELECT run_id, payload FROM events WHERE kind=?",
                (AUDIT_KIND,),
            ).fetchone()
            payload = json.loads(row[1])
            payload["reason"] = "tampered"
            db.execute("UPDATE events SET payload=? WHERE run_id=?",
                       (json.dumps(payload), row[0]))
            db.commit()
        with self.assertRaises(StateCorruptionError):
            state.load_state()

    def test_duplicate_audit_rows_fail_closed(self):
        self._cancel()
        with closing(sqlite3.connect(state.JOURNAL_FILE)) as db:
            row = db.execute(
                "SELECT ts, kind, payload, run_id, runtime_mode, "
                "account_fingerprint FROM events WHERE kind=?",
                (AUDIT_KIND,),
            ).fetchone()
            db.execute(
                "INSERT INTO events(ts, kind, payload, run_id, runtime_mode, "
                "account_fingerprint) VALUES (?, ?, ?, ?, ?, ?)", row)
            db.commit()
        with self.assertRaises(StateCorruptionError):
            state.load_state()

    def test_cancelled_state_audit_verifier_uses_read_only_journal_mode(self):
        self._cancel()
        original_connect = sqlite3.connect
        with mock.patch.object(operator.sqlite3, "connect",
                               wraps=original_connect) as connect:
            state.load_state()
        uris = [call.args[0] for call in connect.call_args_list
                if call.args and isinstance(call.args[0], str)]
        self.assertTrue(any(uri.endswith("?mode=ro") for uri in uris))
        self.assertFalse(any(uri.endswith("?mode=rw") for uri in uris))

    def test_missing_journal_is_not_created_during_cancelled_state_validation(self):
        self._cancel()
        journal_path = Path(state.JOURNAL_FILE)
        saved_path = journal_path.with_name("journal.db.saved")
        journal_path.rename(saved_path)
        try:
            with self.assertRaises(StateCorruptionError):
                state.load_state()
            self.assertFalse(journal_path.exists())
        finally:
            saved_path.rename(journal_path)

    def test_missing_wal_sidecar_fails_closed_for_audit_validation(self):
        self._cancel()
        journal_path = Path(state.JOURNAL_FILE)
        writer = sqlite3.connect(journal_path, timeout=5)
        try:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO events(ts, kind, payload, run_id) "
                "VALUES (?, ?, ?, ?)",
                (0.0, "unrelated", "{}", "unrelated"),
            )
            writer.commit()
            wal_path = Path(str(journal_path) + "-wal")
            self.assertTrue(wal_path.exists())
            wal_path.unlink()
            with self.assertRaises(StateCorruptionError):
                state.load_state()
        finally:
            writer.close()


if __name__ == "__main__":
    unittest.main()
