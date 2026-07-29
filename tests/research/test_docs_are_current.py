"""The documentation must describe the software that exists.

Docs rot silently and in one direction: a command gets renamed, a config key
gets added, and README keeps confidently describing the previous version.
Someone following it then hits an error that looks like their mistake.

These tests are cheap and they only assert things that are mechanically
checkable - that every command and file path the docs name actually exists,
and that every config key the agent accepts is documented somewhere. They
cannot tell whether the prose is *true*, only whether it still refers to real
things.
"""

import re
import unittest
from pathlib import Path

import yaml

from agent.config import validate_config


REPO = Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text(encoding="utf-8")
SETUP = (REPO / "SETUP.md").read_text(encoding="utf-8")
BOTH = README + SETUP


class ReferencedPathsExistTests(unittest.TestCase):
    def test_every_repo_path_named_in_backticks_exists(self):
        # Only paths that look like real files in this repo: a directory
        # component and a known extension, so prose like `long` is ignored.
        pattern = re.compile(
            r"`((?:agent|research|tests|deploy|findings)/[\w./-]+"
            r"\.(?:py|md|yaml|sh|timer|service))`")
        missing = sorted({
            path for path in pattern.findall(BOTH)
            if not (REPO / path).exists()
        })
        self.assertEqual(missing, [], f"documented paths do not exist: {missing}")

    def test_the_research_cli_exists(self):
        self.assertTrue((REPO / "research.py").exists())


class DocumentedCommandsExistTests(unittest.TestCase):
    def _subcommands(self):
        # Loaded by path, not by name. `research.py` and the `research/`
        # package share a name and Python resolves the package first, so
        # `import research` gives the package. The CLI is a script.
        import importlib.util
        import sys

        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        spec = importlib.util.spec_from_file_location(
            "_research_cli", REPO / "research.py")
        research_cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(research_cli)

        parser = research_cli.build_parser()
        found = set()
        for action in parser._actions:
            if hasattr(action, "choices") and action.choices:
                found.update(str(c) for c in action.choices)
        return found

    def test_every_documented_research_command_exists(self):
        available = self._subcommands()
        for command in ("corpus", "replay", "funnel", "cadence", "three-arm",
                        "sweep", "report"):
            if f"research.py {command}" in BOTH:
                self.assertIn(command, available,
                              f"docs reference `research.py {command}`")

    def test_every_available_command_is_documented(self):
        """A command nobody documents is a command nobody runs."""
        for command in self._subcommands():
            if command in ("stats",):        # nested under `corpus`
                continue
            self.assertIn(f"research.py {command}", BOTH,
                          f"`research.py {command}` is undocumented")


class ConfigKeysAreDocumentedTests(unittest.TestCase):
    """Every block the validator accepts should be findable in the docs."""

    def test_the_shipped_config_validates(self):
        raw = yaml.safe_load((REPO / "config.yaml").read_text())
        self.assertIsNotNone(validate_config(raw))

    def test_new_config_keys_are_documented(self):
        for key in ("decision_interval_seconds", "maker_first_enabled",
                    "maker_first_wait_seconds", "shadow_enabled",
                    "shadow_variants", "shadow_budget_ms",
                    "shadow_llm_variants"):
            self.assertIn(key, BOTH, f"{key} is undocumented")

    def test_the_research_block_is_documented(self):
        self.assertIn("research:", README)


class VersionClaimsTests(unittest.TestCase):
    def test_the_documented_strategy_version_matches_the_register(self):
        from agent import registry

        version = registry.spec_for("momentum").version

        self.assertIn(version, README,
                      f"README does not mention the live version {version}")

    def test_the_documented_version_matches_the_shipped_config(self):
        from agent import registry

        raw = yaml.safe_load((REPO / "config.yaml").read_text())

        self.assertEqual(raw["strategy"]["version"],
                         registry.spec_for("momentum").version)

    def test_a_removed_exit_policy_is_not_advertised_as_available(self):
        """structure_target was deleted in 6.1."""
        from agent import strategy

        self.assertNotIn("structure_target", strategy.EXIT_POLICIES)
        for policy in strategy.EXIT_POLICIES:
            self.assertIn(policy, BOTH, f"exit policy {policy} undocumented")


class ResearchLayerIsDocumentedTests(unittest.TestCase):
    def test_the_two_evidence_paths_are_distinguished(self):
        """The distinction decides what may put capital at risk."""
        self.assertIn("authoritative", README.lower())
        self.assertIn("exploratory", README.lower())

    def test_gate_g2_is_documented_as_a_stop(self):
        self.assertIn("G2", README)
        self.assertIn("check-fidelity", BOTH)

    def test_insufficient_sample_is_explained(self):
        self.assertIn("INSUFFICIENT_SAMPLE", BOTH)

    def test_the_new_journal_events_are_documented(self):
        for kind in ("book_state", "snapshot_enrichment", "shadow_decision"):
            self.assertIn(kind, README, f"journal event {kind} undocumented")


if __name__ == "__main__":
    unittest.main()
