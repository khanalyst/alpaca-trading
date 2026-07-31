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
OPERATIONS = (REPO / "OPERATIONS.md").read_text(encoding="utf-8")
HYPOTHESES = (REPO / "research" / "HYPOTHESES_AND_VARIANTS.md").read_text(
    encoding="utf-8")
BOTH = README + SETUP
ALL_DOCS = README + SETUP + AZURE + OPERATIONS + HYPOTHESES
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

    def test_readme_indexes_every_maintained_markdown_document(self):
        documents = []
        for path in REPO.rglob("*.md"):
            relative = path.relative_to(REPO)
            if any(part in {".git", ".venv", "vm-import"}
                   for part in relative.parts):
                continue
            documents.append(relative.as_posix())
        missing = sorted(
            path for path in documents if f"({path})" not in README)
        self.assertEqual(
            missing, [],
            f"README documentation index omits: {missing}")

    def test_retired_letter_taxonomy_is_not_used(self):
        pattern = re.compile(r"\bH-[G-M](?:\([iv]+\))?\b")
        offenders = []
        for root in (REPO / "agent", REPO / "research", REPO / "tests"):
            for path in root.rglob("*"):
                if path.suffix not in {".py", ".md", ".yaml"}:
                    continue
                if pattern.search(path.read_text(encoding="utf-8")):
                    offenders.append(path.relative_to(REPO).as_posix())
        self.assertEqual(
            offenders, [],
            f"retired letter taxonomy remains in: {offenders}")


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
            self.assertIn(phrase, README_FLOWED)

    def test_store_default_and_axis_proof_are_documented(self):
        self.assertIn("never falls back to a temporary database",
                      README_FLOWED)
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


class CurrentPipelineClaimsTests(unittest.TestCase):
    def test_shipped_account_strategy_cadence_and_paths_are_exact(self):
        raw = yaml.safe_load((REPO / "config.yaml").read_text())
        claims = (
            raw["mode"], raw["strategy"]["id"],
            raw["strategy"]["version"], str(raw["cycle"]["interval_seconds"]),
            raw["research"]["findings_store"],
        )
        for claim in claims:
            self.assertIn(claim, README)

    def test_strategy_and_variant_inventory_matches_code(self):
        from agent import registry, variants

        raw = yaml.safe_load((REPO / "config.yaml").read_text())
        named = variants.load_registry(REPO / "research" / "variants.yaml")
        materialized = dict(named)
        for strategy_id, spec in registry.REGISTRY.items():
            for variant in variants.preregistered_variants(
                    strategy_id, spec.version):
                materialized.setdefault(variant.variant_id, variant)
            if strategy_id == "momentum":
                for variant in variants.hypothesis_variants(
                        strategy_id, spec.version):
                    materialized.setdefault(variant.variant_id, variant)
            materialized.setdefault(
                variants.baseline_variant_id(strategy_id),
                variants.baseline(strategy_id, spec.version))
        selector_count = sum(
            len(items) for items in variants.research_selection_catalog().values())
        tournament_settings = 0
        for path in (REPO / "research" / "hypotheses").glob("*.yaml"):
            tournament_settings += len(
                (yaml.safe_load(path.read_text()) or {}).get("settings") or [])

        for value in (len(registry.REGISTRY), len(named), len(materialized),
                      selector_count, tournament_settings):
            self.assertIn(str(value), HYPOTHESES)
        for strategy_id in registry.REGISTRY:
            self.assertIn(f"`{strategy_id}`", HYPOTHESES)
        self.assertEqual(raw["research"]["experiment_min_duration_days"], 3)
        self.assertEqual(raw["research"]["experiment_min_observations"], 100)
        self.assertIn("14 maximum", HYPOTHESES)
        self.assertIn("5 strategies and 16 applicable setting results",
                      HYPOTHESES)
        self.assertIn("`NOT SCORED`", HYPOTHESES)

    def test_current_findings_schema_is_documented(self):
        from research.findings import SCHEMA_VERSION

        self.assertEqual(SCHEMA_VERSION, 14)
        for doc in (README, OPERATIONS, HYPOTHESES):
            self.assertIn(f"schema {SCHEMA_VERSION}", doc.lower())

    def test_nightly_order_and_failure_semantics_are_documented(self):
        for phrase in ("research-loop", "G2", "exit 3", "exit 4", "exit 5",
                       "tournament", "verified backup"):
            self.assertIn(phrase, OPERATIONS)

    def test_external_classification_does_not_claim_path_is_proof(self):
        flowed_ops = flowed(OPERATIONS)
        self.assertIn("Configuration/path alone is not off-host proof",
                      flowed_ops)
        self.assertIn("different-device", flowed_ops)


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

    def test_current_boundaries_are_documented_in_both(self):
        """Data gates and dormant execution paths must remain visible."""
        for doc, name in ((README, "README"), (SETUP, "SETUP")):
            self.assertIn("readiness", doc, f"{name} omits the readiness cmd")
            self.assertIn("G2", doc, f"{name} omits gate G2")
            self.assertIn("B7.5", doc, f"{name} omits B7.5")

    def test_environment_boundary_says_how_it_completes(self):
        self.assertIn("How it completes", README)
        self.assertIn("Why it waits", README)

    def test_current_pipeline_and_schema_are_not_hidden(self):
        for phrase in ("all seven", "baseline", "at most one candidate",
                       "WORKED", "FAILED", "INCONCLUSIVE", "schema 14"):
            self.assertIn(phrase.lower(), README_FLOWED.lower())

    def test_research_results_never_claim_live_authority(self):
        for phrase in ("RESEARCH_ONLY", "promotion_allowed", "no automatic"):
            self.assertIn(phrase.lower(), README.lower())

    def test_backup_classification_is_fail_closed(self):
        for phrase in ("local_default", "configured_local",
                       "external_mounted", "--require-external", "st_dev"):
            self.assertIn(phrase, BOTH)

    def test_vm_fixture_is_documented_as_read_only_and_not_default(self):
        for doc, name in ((README, "README"), (SETUP, "SETUP")):
            self.assertIn("vm-import/2026-07-30", doc,
                          f"{name} omits the supplied VM fixture")
            self.assertIn("read-only", doc,
                          f"{name} does not mark the fixture read-only")

    def test_tournament_latest_view_is_not_described_as_history(self):
        for phrase in ("runs/<timestamp>-<run-id>", "latest view"):
            self.assertIn(phrase, README_FLOWED)

    def test_the_known_gaps_are_stated_rather_than_hidden(self):
        for gap in ("loss cooldown", "select_universe"):
            self.assertIn(gap, README, f"known gap {gap!r} is undocumented")

    def test_the_new_journal_events_are_documented(self):
        for kind in ("book_state", "snapshot_enrichment", "shadow_decision"):
            self.assertIn(kind, README, f"journal event {kind} undocumented")


class VariantScorecardsAreCurrentTests(unittest.TestCase):
    """The committed scorecards are the registry's identity baseline.

    ``findings.db`` lives on the host and is not in the repository, so the
    committed cards are the only copy of what each variant was registered as.
    That makes them the place to catch an in-place edit to a hypothesis or an
    override, which is otherwise invisible until a host that already holds the
    old row refuses to register the new one - and it refuses inside
    ``research.py report``, ``forward-qualify`` and ``t3-packet``, so the
    symptom is a research loop that stops rather than an error anyone reads.

    A variant's claim is immutable by design. Restating it means a new
    ``variant_id``; only ``status`` may move. These tests hold that line at
    review time, where fixing it is free.
    """

    @classmethod
    def setUpClass(cls):
        from agent import variants as variant_mod

        cls.registry = variant_mod.load_registry(
            REPO / "research" / "variants.yaml")
        cls.findings = REPO / "findings"

    def _card(self, variant):
        return (self.findings / variant.strategy_id
                / f"{variant.variant_id}.md")

    def test_every_registered_variant_has_a_committed_scorecard(self):
        missing = sorted(
            variant.variant_id for variant in self.registry.values()
            if not self._card(variant).exists())

        self.assertEqual(
            missing, [],
            "registered variants with no committed scorecard: "
            f"{missing}. Run `python research.py report`.")

    def test_committed_claims_match_the_registry(self):
        """An edited claim is an edited experiment identity."""
        for variant in self.registry.values():
            with self.subTest(variant=variant.variant_id):
                card = flowed(self._card(variant).read_text(encoding="utf-8"))
                self.assertIn(f"Hypothesis: {flowed(variant.hypothesis)}", card)
                self.assertIn(f"Status: {variant.status}", card)

    def test_committed_overrides_match_the_registry(self):
        for variant in self.registry.values():
            with self.subTest(variant=variant.variant_id):
                card = flowed(self._card(variant).read_text(encoding="utf-8"))
                rendered = ", ".join(
                    f"{key} = {value}"
                    for key, value in sorted(variant.overrides.items()))
                self.assertIn(
                    f"Overrides: {rendered}" if variant.overrides
                    else "Overrides: none", card)

    def test_the_index_links_every_scorecard(self):
        text = (self.findings / "README.md").read_text(encoding="utf-8")
        for variant in self.registry.values():
            with self.subTest(variant=variant.variant_id):
                self.assertIn(
                    f"({variant.strategy_id}/{variant.variant_id}.md)", text)

    def test_the_index_links_every_document_beside_it(self):
        """The generator rewrites this file; it must not orphan an audit."""
        from research import findings as findings_mod

        text = (self.findings / "README.md").read_text(encoding="utf-8")
        documents = findings_mod.audit_documents(self.findings)

        self.assertTrue(documents, "findings/ has no audit documents at all")
        for path in documents:
            with self.subTest(document=path.name):
                self.assertIn(f"({path.name})", text)


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
        self.assertIn("Delete with VM", flowed(AZURE))
        self.assertIn("snapshot", AZURE.lower())

    def test_it_warns_against_becoming_the_service_user(self):
        """The first thing that bites: sudo prompting for a password."""
        self.assertIn("nologin", AZURE)
        self.assertIn("do not use `sudo -iu okx`", AZURE)
        self.assertIn("sudo -u okx", AZURE)

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
