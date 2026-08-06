"""The pre-registrations in version control must be what gets measured.

A hand-written claim reaches the staging store through this path, and the
value of a pre-registration is entirely in it being written before the
result. So the properties that matter here are: the file's wording is what
lands in the store, running twice does not create a second identity, and a
claim that cannot be calibrated is visibly withheld rather than quietly
registered with a made-up threshold.
"""

import tempfile
import unittest
from pathlib import Path

import yaml

from agent.contract_dsl import compile_contract
from agent.staging import StagingStore
from research import staging_seed

CLAIM = ("A sentence long enough to state a cause and how it would be "
         "shown wrong, which the validator requires of every claim.")


def entry(contract_id="alpha", status="registered", **overrides):
    base = {
        "contract_id": contract_id,
        "status": status,
        "author": "human-preregistered",
        "direction": "both",
        "mechanism": CLAIM,
        "payer": CLAIM,
        "falsifier": CLAIM,
        "conditions": [{"field": "atr_1h_ratio", "op": ">=", "value": 1.3}],
    }
    base.update(overrides)
    return base


def write(entries) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8")
    yaml.safe_dump({"version": 1, "contracts": entries}, handle)
    handle.close()
    return Path(handle.name)


class TheSeedFileIsReadStrictlyTests(unittest.TestCase):
    def test_a_missing_file_is_refused_rather_than_treated_as_empty(self):
        with self.assertRaises(staging_seed.SeedError):
            staging_seed.load("/nonexistent/pre-registered.yaml")

    def test_an_unknown_status_is_refused(self):
        path = write([entry(status="draft")])
        with self.assertRaises(staging_seed.SeedError) as caught:
            staging_seed.load(path)
        self.assertIn("status", str(caught.exception))

    def test_a_deferred_entry_must_say_why(self):
        # Otherwise it is indistinguishable from one someone forgot to finish.
        path = write([entry(status="deferred")])
        with self.assertRaises(staging_seed.SeedError):
            staging_seed.load(path)


class SeedingIsIdempotentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = StagingStore(Path(self.dir.name) / "staging.db")

    def test_the_second_run_skips_rather_than_failing(self):
        entries = [entry("alpha")]
        first = staging_seed.seed(self.store, entries)
        second = staging_seed.seed(self.store, entries)
        self.assertEqual(first["registered_count"], 1)
        self.assertEqual(second["registered_count"], 0)
        self.assertEqual([s["contract_id"] for s in second["skipped"]],
                         ["alpha"])
        self.assertEqual(len(self.store.active()), 1)

    def test_a_retired_claim_is_not_resurrected_by_re_seeding(self):
        staging_seed.seed(self.store, [entry("alpha")])
        self.store.retire("alpha", "negative expectancy")
        result = staging_seed.seed(self.store, [entry("alpha")])
        self.assertEqual(result["registered_count"], 0)
        self.assertEqual(self.store.active(), [])

    def test_one_broken_entry_does_not_discard_the_rest(self):
        result = staging_seed.seed(self.store, [
            entry("alpha"),
            entry("beta", conditions=[
                {"field": "not_a_field", "op": ">=", "value": 1}]),
            entry("gamma"),
        ])
        self.assertEqual([r["contract_id"] for r in result["registered"]],
                         ["alpha", "gamma"])
        self.assertEqual([r["contract_id"] for r in result["rejected"]],
                         ["beta"])

    def test_a_deferred_entry_is_reported_and_never_registered(self):
        # A staged contract that cannot fire is worse than a missing one: in
        # the evidence it looks exactly like one that fired and lost.
        result = staging_seed.seed(self.store, [
            entry("alpha"),
            entry("beta", status="deferred",
                  deferred_reason="the field is never populated"),
        ])
        self.assertEqual(result["registered_count"], 1)
        self.assertEqual([d["contract_id"] for d in result["deferred"]],
                         ["beta"])
        self.assertIsNone(self.store.contract("beta"))

    def test_the_status_keys_do_not_leak_into_the_stored_claim(self):
        staging_seed.seed(self.store, [entry("alpha")])
        stored = self.store.contract("alpha")
        self.assertEqual(stored.author, "human-preregistered")
        self.assertEqual(stored.mechanism, " ".join(CLAIM.split()))


class TheShippedPreRegistrationsHoldTests(unittest.TestCase):
    """The file in version control must load, compile and be able to fire."""

    def setUp(self):
        self.entries = staging_seed.load()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = StagingStore(Path(self.dir.name) / "staging.db")

    def test_every_registered_entry_validates_and_compiles(self):
        result = staging_seed.seed(self.store, self.entries)
        self.assertEqual(result["rejected"], [])
        self.assertGreaterEqual(result["registered_count"], 3)
        # evaluators() compiles every active contract; a claim that cannot
        # execute must never have been allowed to occupy an identity.
        self.assertEqual(len(self.store.evaluators()),
                         result["registered_count"])

    def test_liq_absorption_direct_stays_deferred_until_the_field_exists(self):
        # liq_notional_1h_usd appears in none of the recorded observations,
        # so its threshold is uncalibrated and the lane would never fire.
        deferred = {str(e["contract_id"]) for e in self.entries
                    if e.get("status") == "deferred"}
        self.assertIn("liq-absorption-direct", deferred)

    def test_each_registered_contract_fires_on_some_reachable_snapshot(self):
        # A threshold outside a field's observed range measures nothing while
        # occupying a lane for weeks. These are the conditions that were
        # calibrated against the recorded corpus.
        cases = {
            "funding-crowd-unwind": ("short", {
                "funding_percentile_30": 95.0, "funding_rate_pct": 0.03,
                "funding_samples_30": 30}),
            "oi-buildup-fade": ("short", {
                "oi_change_4h_pct": 12.0, "range_pos_pct": 88.0}),
            "basis-crowd-fade": ("short", {"perp_index_basis_pct": 0.9}),
        }
        for contract_id, (direction, snapshot) in cases.items():
            with self.subTest(contract_id):
                proposal = next(e for e in self.entries
                                if e["contract_id"] == contract_id)
                self.assertEqual(proposal["status"], "registered")
                fired, reason = compile_contract(_validated(proposal))(
                    snapshot, direction, {}, {})
                self.assertTrue(fired, reason)

    def test_no_registered_contract_fires_on_an_empty_snapshot(self):
        # Missing data must decline, never fire. A contract that fires on an
        # empty snapshot is measuring the absence of data.
        for proposal in self.entries:
            if proposal.get("status") != "registered":
                continue
            with self.subTest(proposal["contract_id"]):
                evaluate = compile_contract(_validated(proposal))
                for direction in ("long", "short"):
                    fired, _ = evaluate({}, direction, {}, {})
                    self.assertFalse(fired)


def _validated(proposal: dict):
    from agent.contract_dsl import validate

    return validate({key: value for key, value in proposal.items()
                     if key not in ("status", "deferred_reason")})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
