import tempfile
import unittest

from agent import shadow, variants
from research.findings import FindingsStore
from tests.helpers import valid_config


def wildcard_config(feed_version: int) -> dict:
    cfg = valid_config()
    cfg["research"] = {
        "shadow_enabled": True,
        "shadow_variants": ["*"],
        "shadow_budget_ms": 0,
        "forward_feed_version": feed_version,
    }
    return cfg


class FeedIdentityForkTests(unittest.TestCase):
    def test_v5_wildcard_scope_is_clean_and_preserves_v4_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FindingsStore(f"{directory}/findings.db")
            root_scope = "demo:feed-identity"
            feed_v4 = shadow.build(
                wildcard_config(4), {}, scope_key=root_scope, store=store)
            self.assertIsNotNone(feed_v4)
            self.assertEqual(feed_v4.scope_key, f"{root_scope}:feed-v4")

            baseline_id = variants.baseline_variant_id("flush-fade")
            # Reproduce the legacy row that caused the in-place rebind error:
            # evidence exists, but its experiment provenance is absent.
            old_state, old_version = store.load_paper_portfolio(
                feed_v4.scope_key, baseline_id)
            old_state["experiment_provenance"] = None
            old_state["win_count"] = 1
            store.save_paper_portfolio(
                feed_v4.scope_key, baseline_id, old_state, old_version,
                now=1_700_000_000.0)
            before = store.paper_portfolio_state(feed_v4.scope_key, baseline_id)
            before_assignment = store.active_experiment_assignment(
                feed_v4.scope_key, "flush-fade")

            feed_v5 = shadow.build(
                wildcard_config(5), {}, scope_key=root_scope, store=store)
            self.assertIsNotNone(feed_v5)
            self.assertEqual(feed_v5.scope_key, f"{root_scope}:feed-v5")
            self.assertNotEqual(feed_v4.scope_key, feed_v5.scope_key)

            # The new fork initializes its own attributable account while the
            # old unprovenanced evidence remains byte-for-byte unchanged.
            fresh = store.paper_portfolio_state(feed_v5.scope_key, baseline_id)
            self.assertIsNotNone(fresh)
            self.assertIsNotNone(fresh["experiment_provenance"])
            self.assertEqual(
                before, store.paper_portfolio_state(feed_v4.scope_key, baseline_id))
            self.assertEqual(
                before_assignment["assignment_id"],
                store.active_experiment_assignment(
                    feed_v4.scope_key, "flush-fade")["assignment_id"])


if __name__ == "__main__":
    unittest.main()
