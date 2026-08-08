"""Import and command-line contracts for previously uncovered research tools."""

import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]

# These executable modules previously had no direct test import.  The options
# below are their operational input contract, not a count-driven list of every
# helper in research/: each script either acquires durable input or transforms
# a named dataset into an auditable analysis artifact.  The one-shot programs
# live under research.legacy; maker_study remains at its load-bearing path.
ENTRYPOINTS = {
    "research.legacy.analyse_flow": ("--data", "--flow"),
    "research.legacy.deep_edge": ("--data", "--out"),
    "research.legacy.edge_report": ("--data", "--stage", "--out"),
    "research.legacy.fetch_flow_data": ("--out", "--days", "--period"),
    "research.legacy.find_edge": ("--data", "--out"),
    "research.legacy.make_legacy_dataset": (
        "--source", "--dest", "--min-bars"),
    "research.maker_study": ("--data", "--out"),
    "research.legacy.selection_study": ("--data", "--out"),
    "research.legacy.unbiased_recheck": ("--data", "--out"),
    "research.legacy.validate_candidate": ("--data", "--out"),
}


class PreviouslyUncoveredResearchCliTests(unittest.TestCase):
    def test_executable_modules_import_and_publish_their_input_contract(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        for module, required_options in ENTRYPOINTS.items():
            with self.subTest(module=module):
                completed = subprocess.run(
                    [sys.executable, "-m", module, "--help"],
                    cwd=REPO, env=env, text=True, capture_output=True,
                    timeout=10, check=False)
                self.assertEqual(
                    completed.returncode, 0,
                    f"{module} --help failed:\n{completed.stderr}")
                self.assertIn("usage:", completed.stdout.lower())
                for option in required_options:
                    self.assertIn(option, completed.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
