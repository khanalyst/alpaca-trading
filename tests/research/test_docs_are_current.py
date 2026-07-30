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
AZURE = (REPO / "AZURE_DEPLOYMENT.md").read_text(encoding="utf-8")
BOTH = README + SETUP
ALL_DOCS = README + SETUP + AZURE
PROTOCOL = (REPO / "research" / "protocol.md").read_text(encoding="utf-8")


def flowed(text: str) -> str:
    """Collapse whitespace so a phrase assertion survives a reflow.

    Asserting a phrase with the line break where it happens to fall today
    makes the test fail on rewrapping a paragraph, which teaches whoever hits
    it that these tests are noise. Match on the prose, not the column width.
    """
    return " ".join(text.split())


README_FLOWED = flowed(README)
PROTOCOL_FLOWED = flowed(PROTOCOL)


class ReferencedPathsExistTests(unittest.TestCase):
    def test_every_repo_path_named_in_backticks_exists(self):
        # Only paths that look like real files in this repo: a directory
        # component and a known extension, so prose like `long` is ignored.
        pattern = re.compile(
            r"`((?:agent|research|tests|deploy|findings)/[\w./-]+"
            r"\.(?:py|md|yaml|sh|timer|service))`")
        missing = sorted({
            path for path in pattern.findall(ALL_DOCS)
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
                    "shadow_variants", "shadow_budget_ms"):
            self.assertIn(key, BOTH, f"{key} is undocumented")

    def test_the_research_block_is_documented(self):
        self.assertIn("research:", README)

    def test_shipped_llm_defaults_are_documented(self):
        raw = yaml.safe_load((REPO / "config.yaml").read_text())
        for doc, name in ((README, "README"), (SETUP, "SETUP")):
            self.assertIn(raw["llm"]["provider"], doc,
                          f"{name} omits the shipped LLM provider")
            self.assertIn(raw["llm"]["model"], doc,
                          f"{name} omits the shipped LLM model")

    def test_cost_guidance_does_not_slow_safety_housekeeping(self):
        self.assertNotIn("cycle.interval_seconds: 600", BOTH)
        for doc, name in ((README, "README"), (SETUP, "SETUP")):
            self.assertIn("cycle.decision_interval_seconds", doc,
                          f"{name} omits the safe decision-cadence lever")

    def test_removed_shadow_llm_setting_is_not_documented(self):
        self.assertNotIn("shadow_llm_variants", BOTH)
        self.assertNotIn(
            "shadow_llm_variants",
            (REPO / "research" / "plan" / "batched-implementation.md").read_text())


class ResearchProtocolDocumentationTests(unittest.TestCase):
    def test_held_out_pair_minimums_are_documented(self):
        for phrase in ("100 full pairs", "70 fit", "30 confirmation",
                       "80% coverage"):
            self.assertIn(phrase, PROTOCOL)

    def test_the_episode_minimum_is_documented_with_its_reason(self):
        """A pair count is not a precision, and the doc has to say why."""
        from research.protocol import MIN_BOOTSTRAP_CLUSTERS

        self.assertIn(f"{MIN_BOOTSTRAP_CLUSTERS} distinct six-hour episodes",
                      PROTOCOL_FLOWED)
        self.assertIn("zero-width", PROTOCOL_FLOWED)

    def test_the_exploratory_tier_ceiling_is_documented(self):
        import sys

        sys.path.insert(0, str(REPO / "research"))
        from gates import EXPLORATORY_CEILING

        self.assertIn(f"awards no tier above `{EXPLORATORY_CEILING}`",
                      README_FLOWED)
        self.assertIn("unrevised rather than as demoted", README_FLOWED)

    def test_two_way_shadow_isolation_is_documented(self):
        for phrase in ("Isolation runs both ways",
                       "withheld from everything on the live path",
                       "breadth is recomputed"):
            self.assertIn(phrase, README_FLOWED)

    def test_executable_fingerprint_scope_is_documented(self):
        for phrase in ("LLM provider", "universe selection",
                       "decision cadence", "credentials are excluded"):
            self.assertIn(phrase, README)

    def test_store_default_and_axis_proof_are_documented(self):
        self.assertIn("never\nfalls back to a temporary database", README)
        self.assertIn("non-axis executable", README)

    def test_policy_vetoes_and_legacy_watermark_are_documented(self):
        for phrase in ("immutable decision ledger", "zero-return action",
                       "Schema migration 7"):
            self.assertIn(phrase, README)
        self.assertIn("explicit\n0R action", PROTOCOL)

    def test_common_forward_window_and_schema_7_migration_are_documented(self):
        for phrase in ("v6→v7 migration", "operational", "PAPER"):
            self.assertIn(phrase, README)
        self.assertIn("decision-ledger rows", PROTOCOL)


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

    def test_pending_work_is_documented_in_both(self):
        """An undocumented blocker becomes a surprise three weeks in."""
        for doc, name in ((README, "README"), (SETUP, "SETUP")):
            self.assertIn("readiness", doc, f"{name} omits the readiness cmd")
            self.assertIn("G2", doc, f"{name} omits gate G2")
            self.assertIn("B7.5", doc, f"{name} omits B7.5")

    def test_each_pending_item_says_how_it_completes(self):
        self.assertIn("How it completes", README)
        self.assertIn("Why it waits", README)

    def test_the_known_gaps_are_stated_rather_than_hidden(self):
        for gap in ("loss cooldown", "select_universe"):
            self.assertIn(gap, README, f"known gap {gap!r} is undocumented")

    def test_the_new_journal_events_are_documented(self):
        for kind in ("book_state", "snapshot_enrichment", "shadow_decision"):
            self.assertIn(kind, README, f"journal event {kind} undocumented")


class DeploymentDocTests(unittest.TestCase):
    """The Azure guide drifted unchecked while the research layer was built.

    It is the only document a VM deployment follows end to end, so a stale
    instruction there costs more than a stale one in README - the reader is
    at a terminal, not browsing.
    """

    def test_it_says_which_document_to_read_when(self):
        self.assertIn("SETUP.md", AZURE)
        self.assertIn("README.md", AZURE)

    def test_it_points_at_the_readiness_command(self):
        self.assertIn("research.py readiness", AZURE)

    def test_the_backup_list_covers_everything_unrecoverable(self):
        """Losing any of these cannot be undone by re-downloading."""
        for path in ("runtime/research/recorded",
                     "journal.db",
                     "findings.db"):
            self.assertIn(path, AZURE,
                          f"{path} is missing from the backup guidance")

    def test_it_warns_that_deleting_the_vm_destroys_the_corpus(self):
        """The doc itself recommends "Delete with VM: Checked"."""
        self.assertIn("Delete with VM", AZURE)
        self.assertIn("snapshot", AZURE.lower())

    def test_it_warns_against_becoming_the_service_user(self):
        """The first thing that bites: sudo prompting for a password."""
        self.assertIn("nologin", AZURE)
        self.assertIn("sudo -iu okx", AZURE)

    def test_every_service_unit_it_names_exists(self):
        import re
        for unit in set(re.findall(r"okx-[a-z]+\.(?:service|timer)", AZURE)):
            self.assertTrue(
                (REPO / "deploy" / unit).exists(),
                f"AZURE_DEPLOYMENT.md names {unit}, which is not in deploy/")

    def test_the_recorder_is_started_before_the_trader(self):
        """Every hour it is off is data OKX will never serve again."""
        self.assertLess(AZURE.index("enable --now okx-recorder"),
                        AZURE.index("enable --now okx-trader"))


if __name__ == "__main__":
    unittest.main()
