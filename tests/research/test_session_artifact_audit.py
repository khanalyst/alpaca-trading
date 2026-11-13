"""Saved claims are reassessed honestly; they never become raw-data proof."""

import json
from pathlib import Path
import tempfile
import unittest

from research.session_artifact_audit import audit


class ArtifactAuditTests(unittest.TestCase):
    def test_committed_claims_have_no_corrected_alpha_survivors(self):
        source = Path(__file__).resolve().parents[2] / (
            "studies/session_edges/results/discovery-public-2026-11-12/discovery.json")
        result = audit(source)
        self.assertEqual(result["promoted_count"], 124)
        self.assertEqual(len(result["total_return_holm"]), 9)
        self.assertEqual(result["alpha_holm"], [])
        self.assertEqual(result["alpha_by"], [])
        self.assertFalse(result["raw_market_data_replayed"])
        self.assertFalse(result["authorizing"])

    def test_missing_statistics_cannot_shrink_the_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            rows = {str(i): {"holdout": {"sessions": 600}} for i in range(99)}
            rows["signal"] = {"holdout": {"sessions": 600, "t": 3.0, "alpha_t": 3.0}}
            path.write_text(json.dumps({"schema": "session-edges-discovery.v1", "promoted": rows}))
            result = audit(path)
            self.assertEqual(result["promoted_count"], 100)
            self.assertEqual(result["alpha_holm"], [])
            self.assertEqual(result["holm_first_threshold"], 0.0005)


if __name__ == "__main__":
    unittest.main()
