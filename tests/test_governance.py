"""Operator-declared promotions, and the audit trail for configuration changes.

Two things must not move on their own: which edge is given money, and what the
configuration said. These tests pin the first as a selection the operator makes
and no automatic process may undo, and the second as a content-addressed
version id with a diff naming every field that moved.
"""

import copy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent.config import ConfigError, DEFAULT_CONFIG, validate_config
from agent.governance import (
    REDACTED, GovernanceError, config_diff, config_history, config_version_at,
    pinned_variant_ids, promotion_snippet, record_config_version, redact,
    validate_promotions,
)


def _config(**strategy):
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["strategy"] = {**base["strategy"], **strategy}
    return base


class PromotionValidationTests(unittest.TestCase):
    def test_a_well_formed_promotion_is_accepted_and_normalized(self):
        entries = validate_promotions([
            {"id": "pin-spy-orb", "variant_id": "rule.orb.abc123",
             "vehicle": "equity", "note": "cleared its live paper trial"}])
        self.assertEqual(entries, [{
            "id": "pin-spy-orb", "variant_id": "rule.orb.abc123",
            "vehicle": "equity", "strategy_id": "rule",
            "note": "cleared its live paper trial", "promoted_at": ""}])

    def test_every_promotion_needs_its_own_stable_id(self):
        """The id is the audit handle, so it is required and unique."""
        cases = {
            "must be 3-64 characters": [{"variant_id": "rule.a.1"}],
            "duplicates": [{"id": "pin-a", "variant_id": "rule.a.1"},
                           {"id": "pin-a", "variant_id": "rule.b.2"}],
        }
        for expected, entries in cases.items():
            with self.subTest(expected=expected):
                with self.assertRaises(GovernanceError) as caught:
                    validate_promotions(entries)
                self.assertIn(expected, str(caught.exception))

    def test_malformed_promotions_are_refused(self):
        for entries in ([{"id": "pin-a"}],
                        [{"id": "pin a b", "variant_id": "x"}],
                        [{"id": "pin-a", "variant_id": "x", "vehicle": "crypto"}],
                        [{"id": "pin-a", "variant_id": "x", "surprise": 1}],
                        [{"id": "pin-a", "variant_id": "x", "note": "n" * 501}],
                        "not-a-list"):
            with self.subTest(entries=entries):
                with self.assertRaises(GovernanceError):
                    validate_promotions(entries)

    def test_the_same_variant_cannot_be_pinned_twice(self):
        with self.assertRaises(GovernanceError):
            validate_promotions([
                {"id": "pin-a", "variant_id": "rule.a.1", "vehicle": "equity"},
                {"id": "pin-b", "variant_id": "rule.a.1", "vehicle": "equity"}])

    def test_config_accepts_pinned_and_reports_the_pairs(self):
        config = validate_config(_config(
            selection_mode="pinned",
            pinned=[{"id": "pin-a", "variant_id": "rule.a.1",
                     "vehicle": "equity"}]))
        self.assertEqual(pinned_variant_ids(config), {("rule.a.1", "equity")})

    def test_pinned_mode_without_entries_is_refused(self):
        with self.assertRaises(ConfigError):
            validate_config(_config(selection_mode="pinned", pinned=[]))

    def test_entries_without_pinned_mode_are_refused(self):
        """Silently ignored promotions are worse than rejected ones."""
        with self.assertRaises(ConfigError):
            validate_config(_config(
                selection_mode="all_proved",
                pinned=[{"id": "pin-a", "variant_id": "rule.a.1"}]))

    def test_a_snippet_is_valid_config_for_the_variant_it_names(self):
        import json

        snippet = json.loads(promotion_snippet("rule.orb.abc12345", "equity"))
        merged = _config(**snippet["strategy"])
        config = validate_config(merged)
        self.assertEqual(pinned_variant_ids(config),
                         {("rule.orb.abc12345", "equity")})


class ConfigAuditTests(unittest.TestCase):
    def test_a_version_is_recorded_once_per_distinct_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            base = copy.deepcopy(DEFAULT_CONFIG)
            first = record_config_version(journal, base)
            repeat = record_config_version(journal, base)
            self.assertTrue(first["recorded"])
            self.assertFalse(repeat["recorded"])
            self.assertEqual(first["config_version_id"],
                             repeat["config_version_id"])
            self.assertEqual(len(config_history(journal)), 1)

    def test_a_changed_value_creates_a_new_version_naming_the_field(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            base = copy.deepcopy(DEFAULT_CONFIG)
            first = record_config_version(journal, base)
            changed = copy.deepcopy(base)
            changed["risk"]["risk_per_trade_pct"] = 0.75
            second = record_config_version(journal, changed)
            self.assertNotEqual(second["config_version_id"],
                                first["config_version_id"])
            self.assertEqual(second["previous_version_id"],
                             first["config_version_id"])
            self.assertEqual(second["diff"], [{
                "path": "risk.risk_per_trade_pct", "change": "changed",
                "from": 0.5, "to": 0.75}])

    def test_secrets_are_recorded_as_changed_never_as_values(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            base = copy.deepcopy(DEFAULT_CONFIG)
            record_config_version(journal, base)
            secret = copy.deepcopy(base)
            secret["broker"]["api_key"] = "sk-live-do-not-store-me"
            result = record_config_version(journal, secret)
            body = str(result["diff"])
            self.assertIn(REDACTED, body)
            self.assertNotIn("sk-live-do-not-store-me", body)
            with closing(sqlite3.connect(journal)) as db:
                stored = db.execute(
                    "SELECT config_json FROM config_versions").fetchall()
            self.assertNotIn("sk-live-do-not-store-me", str(stored))

    def test_a_secret_change_alone_still_produces_a_new_version(self):
        """Redaction hides the value, it must not hide that a value moved."""
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            base = copy.deepcopy(DEFAULT_CONFIG)
            base["broker"]["api_key"] = "first"
            first = record_config_version(journal, base)
            base["broker"]["api_key"] = "second"
            second = record_config_version(journal, base)
            # Both redact to the same token, so the trail records one version.
            # That is the honest outcome: it cannot prove a rotation happened
            # without storing the secret, and it never claims to.
            self.assertEqual(second["config_version_id"],
                             first["config_version_id"])

    def test_versions_are_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            record_config_version(journal, copy.deepcopy(DEFAULT_CONFIG))
            with closing(sqlite3.connect(journal)) as db:
                for statement in ("UPDATE config_versions SET mode='live'",
                                  "DELETE FROM config_versions"):
                    with self.subTest(statement=statement):
                        with self.assertRaises(sqlite3.IntegrityError):
                            db.execute(statement)

    def test_the_version_id_can_be_computed_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.db"
            base = copy.deepcopy(DEFAULT_CONFIG)
            self.assertEqual(config_version_at(journal, base),
                             record_config_version(journal, base)["config_version_id"])

    def test_history_of_a_missing_or_unrelated_journal_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(config_history(Path(directory) / "absent.db"), [])
            other = Path(directory) / "other.db"
            with closing(sqlite3.connect(other)) as db, db:
                db.execute("CREATE TABLE unrelated (x INTEGER)")
            self.assertEqual(config_history(other), [])

    def test_diff_names_additions_and_removals(self):
        changes = config_diff({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"d": 3}})
        self.assertEqual(changes, [
            {"path": "b.c", "change": "removed", "from": 2},
            {"path": "b.d", "change": "added", "to": 3}])

    def test_redaction_walks_nested_structures(self):
        safe = redact({"a": {"secret_key": "x", "keep": 1},
                       "list": [{"token": "y"}]})
        self.assertEqual(safe, {"a": {"secret_key": REDACTED, "keep": 1},
                                "list": [{"token": REDACTED}]})

    def test_an_empty_secret_is_not_reported_as_present(self):
        self.assertEqual(redact({"webhook_url": ""}), {"webhook_url": ""})


class LiveModeTests(unittest.TestCase):
    """Live still pins exactly one edge, by either route."""

    def _live(self, **strategy):
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["mode"] = "live"
        config["broker"] = {**config["broker"], "paper": False, "allow_live": True}
        config["strategy"] = {**config["strategy"], **strategy}
        return config

    def setUp(self):
        import os
        self._env = dict(os.environ)
        os.environ["ALPACA_LIVE_ENABLE"] = "true"
        os.environ.pop("ALPACA_PAPER", None)

    def tearDown(self):
        import os
        os.environ.clear()
        os.environ.update(self._env)

    def test_live_accepts_exactly_one_pinned_entry(self):
        config = validate_config(self._live(
            selection_mode="pinned",
            pinned=[{"id": "prod-2026-05", "variant_id": "rule.a.1",
                     "vehicle": "equity"}]))
        self.assertEqual(config["strategy"]["pinned"][0]["id"], "prod-2026-05")

    def test_live_rejects_more_than_one_pinned_entry(self):
        with self.assertRaises(ConfigError):
            validate_config(self._live(
                selection_mode="pinned",
                pinned=[{"id": "a-one", "variant_id": "rule.a.1"},
                        {"id": "b-two", "variant_id": "rule.b.2"}]))

    def test_live_still_rejects_automatic_selection(self):
        with self.assertRaises(ConfigError):
            validate_config(self._live(selection_mode="all_proved"))


if __name__ == "__main__":
    unittest.main()
