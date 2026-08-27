"""A research slot is parallel capacity, not a one-shot licence.

Before this behaviour existed, a slot that proved an edge left
``ACTIVE_HYPOTHESIS_STATES`` and nothing ever replaced it, so the factory lost
one worker per success and eventually returned ``exhausted`` forever: the more
edges it found, the less it searched.  These tests pin the opposite invariant —
a proved slot is immediately reseeded, capacity stays constant, and the proved
hypothesis and its deployed candidate are never touched again.
"""

from pathlib import Path
import tempfile
import unittest

from agent.config import validate_config
from agent.contracts.rule import (MIN_STOP_DISTANCE_BPS, RULE_FAMILIES,
                                  RULE_SCHEMA_V2, RULE_SCHEMA_V3,
                                  rule_variant_id)
from research.edge_lab import EdgeLedger
from research.costs import CostModel, ReplayPolicy
from research.factory_core import (
    MAX_DISCOVERY_ATTEMPTS, discovery_hypothesis, discovery_spec,
    initial_hypotheses, template_hypothesis,
)
from research.factory_ledger import ACTIVE_HYPOTHESIS_STATES, FactoryLedger
from research.llm_strategy import DISCOVERY_SCHEMA, ProposalResult
from research.strategy_factory import (
    _discovery_context, _ensure_slots, _execution_geometry_context,
    _proved_families, _seed_slot, _slot_reseeds,
)

STRATEGIES = 7


def _ledgers(directory):
    path = Path(directory) / "edge_lab.sqlite3"
    return FactoryLedger(path), EdgeLedger(path)


def _seed(factory, count=STRATEGIES, vehicle="equity"):
    for hypothesis in initial_hypotheses(count, vehicle=vehicle):
        factory.register(hypothesis)


def _variant_ids(factory, vehicle="equity"):
    return {rule_variant_id(item["rule_spec"])
            for item in factory.hypotheses(vehicle=vehicle)}


def _prove_every_active_slot(factory, vehicle="equity"):
    """Mark each active slot validated, exactly as a shadow pass does."""
    proved = list(factory.active(vehicle))
    for hypothesis in proved:
        factory.event(hypothesis["hypothesis_id"], "validated",
                      "1 autonomous variant(s) passed shadow",
                      {"passing": ["rule.test.0000000000000000"]})
    return proved


class SlotStateTests(unittest.TestCase):
    def test_discovery_context_exposes_configuration_only_execution_geometry(self):
        config = validate_config({})
        geometry = _execution_geometry_context(
            vehicle="equity",
            costs=CostModel.from_config(config, vehicle="equity"),
            policy=ReplayPolicy.from_config(config),
        )
        context = _discovery_context(
            slot=0, reason="fresh_slot", previous={}, tried_families=set(),
            proved_families=(), execution_geometry=geometry)

        self.assertEqual(context["execution_geometry"]["scenario_bps"], 25.0)
        self.assertEqual(
            context["execution_geometry"]["max_stressed_cost_to_risk_ratio"],
            0.30)
        self.assertAlmostEqual(
            context["execution_geometry"]["required_stop_distance_bps"],
            25.0 / 0.30)
        self.assertEqual(
            context["execution_geometry"]["grammar_stop_floor_bps"],
            MIN_STOP_DISTANCE_BPS)
        self.assertFalse(
            context["execution_geometry"]["grammar_stop_floor_admissible"])
        self.assertEqual(
            context["execution_geometry"]["expected_symmetric_bar_round_trip_bps"],
            17.0)
        self.assertEqual(
            context["execution_geometry"]["expected_executable_quote_round_trip_bps"],
            13.0)
        self.assertTrue(context["execution_geometry"]["diagnostic_only"])
        self.assertFalse(context["execution_geometry"]["authorizing"])
        self.assertTrue(
            context["execution_geometry"][
                "expected_cost_calibration_independent_of_stress"])
        self.assertNotIn("outcomes", context["execution_geometry"])
        self.assertNotIn("heldout", context["execution_geometry"])

    def test_option_geometry_marks_stop_formula_unavailable(self):
        config = validate_config({})
        geometry = _execution_geometry_context(
            vehicle="option",
            costs=CostModel.from_config(config, vehicle="option"),
            policy=ReplayPolicy.from_config(config),
        )
        context = _discovery_context(
            slot=0, reason="fresh_slot", previous={}, tried_families=set(),
            proved_families=(), execution_geometry=geometry)
        option_geometry = context["execution_geometry"]

        self.assertFalse(option_geometry["stop_distance_formula_available"])
        self.assertIsNone(option_geometry["required_stop_distance_bps"])
        self.assertIsNone(option_geometry["grammar_stop_floor_admissible"])
        self.assertIn("plan-dependent", option_geometry[
            "stop_distance_formula_unavailable_reason"])
        self.assertIsNone(option_geometry[
            "expected_symmetric_bar_round_trip_bps"])
        self.assertIsNone(option_geometry[
            "expected_executable_quote_round_trip_bps"])

    def test_validated_is_not_an_active_state(self):
        """The regression's root cause, stated directly."""
        self.assertNotIn("validated", ACTIVE_HYPOTHESIS_STATES)

    def test_proving_every_slot_empties_the_active_set_without_a_reseed(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            _seed(factory)
            self.assertEqual(len(factory.active("equity")), STRATEGIES)
            _prove_every_active_slot(factory)
            self.assertEqual(factory.active("equity"), [])

    def test_next_generation_continues_a_slots_numbering(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            _seed(factory)
            self.assertEqual(factory.next_generation("equity", 0), 1)
            self.assertEqual(factory.next_generation("equity", 99), 0)


class ReseedTests(unittest.TestCase):
    def test_ensure_slots_revives_a_ledger_whose_slots_all_proved(self):
        """The migration case: an existing deployment that already went quiet."""
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            _seed(factory)
            proved = _prove_every_active_slot(factory)
            seeded, revived = _ensure_slots(
                factory, edge, vehicle="equity", strategies=STRATEGIES,
                existing_variant_ids=_variant_ids(factory), llm_enabled=False,
                llm_config={}, adapter=None)
            self.assertEqual(seeded, [])
            self.assertEqual(len(revived), STRATEGIES)
            self.assertEqual(len(factory.active("equity")), STRATEGIES)
            # The proved hypotheses keep their state and their spec.
            for hypothesis in proved:
                current = factory.hypothesis(hypothesis["hypothesis_id"])
                self.assertEqual(current["status"], "validated")
                self.assertEqual(current["rule_spec"], hypothesis["rule_spec"])

    def test_ensure_slots_fills_slots_added_by_raising_strategies(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            _seed(factory, count=3)
            self.assertEqual(len(factory.active("equity")), 3)
            seeded, revived = _ensure_slots(
                factory, edge, vehicle="equity", strategies=STRATEGIES,
                existing_variant_ids=_variant_ids(factory), llm_enabled=False,
                llm_config={}, adapter=None)
            # New slots were never occupied, so they are seeded, not revived.
            self.assertEqual(len(seeded), STRATEGIES - 3)
            self.assertEqual(revived, [])
            self.assertEqual(len(factory.active("equity")), STRATEGIES)

    def test_ensure_slots_seeds_a_completely_empty_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            seeded, revived = _ensure_slots(
                factory, edge, vehicle="equity", strategies=STRATEGIES,
                existing_variant_ids=set(), llm_enabled=False,
                llm_config={}, adapter=None)
            self.assertEqual(len(seeded), STRATEGIES)
            self.assertEqual(revived, [])
            self.assertEqual({item["source"] for item in seeded}, {"template"})
            self.assertEqual([item["rule_spec"] for item in seeded],
                             [h.rule_spec for h in initial_hypotheses(STRATEGIES)])

    def test_ensure_slots_is_idempotent_while_slots_are_active(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            _seed(factory)
            before = len(factory.hypotheses(vehicle="equity"))
            for _ in range(3):
                self.assertEqual(_ensure_slots(
                    factory, edge, vehicle="equity", strategies=STRATEGIES,
                    existing_variant_ids=_variant_ids(factory),
                    llm_enabled=False, llm_config={}, adapter=None), ([], []))
            self.assertEqual(len(factory.hypotheses(vehicle="equity")), before)

    def test_capacity_survives_many_consecutive_all_success_cycles(self):
        """Sixty rounds where every slot proves an edge every time."""
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            _seed(factory)
            for cycle in range(60):
                _prove_every_active_slot(factory)
                _ensure_slots(
                    factory, edge, vehicle="equity", strategies=STRATEGIES,
                    existing_variant_ids=_variant_ids(factory),
                    llm_enabled=False, llm_config={}, adapter=None)
                self.assertEqual(len(factory.active("equity")), STRATEGIES,
                                 f"capacity lost at cycle {cycle}")
            hypotheses = factory.hypotheses(vehicle="equity")
            self.assertEqual(len(hypotheses), STRATEGIES * 61)
            # Every registered hypothesis is a distinct rule.
            self.assertEqual(len({rule_variant_id(item["rule_spec"])
                                  for item in hypotheses}), len(hypotheses))

    def test_reseeds_are_counted_apart_from_failure_rotations(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            _seed(factory, count=1)
            hypothesis = factory.active("equity")[0]
            factory.event(hypothesis["hypothesis_id"], "validated", "passed shadow")
            factory.event(hypothesis["hypothesis_id"], "validated",
                          "slot proved an edge and was reseeded",
                          {"reseed": True, "source": "deterministic_discovery"})
            self.assertEqual(_slot_reseeds(factory, "equity", 0), 1)
            # A reseed is not a rotation and must not spend the rotation budget.
            from research.strategy_factory import _slot_rotations
            self.assertEqual(_slot_rotations(factory, "equity", 0), 0)


class DeterministicDiscoveryTests(unittest.TestCase):
    def test_an_untried_family_at_its_template_comes_first(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            _seed(factory, count=1)
            previous = factory.active("equity")[0]
            seeded = discovery_hypothesis(
                previous, generation=1, not_before=None,
                existing_variant_ids=_variant_ids(factory),
                tried_families={previous["family"]})
            self.assertIsNotNone(seeded)
            self.assertNotEqual(seeded.family, previous["family"])
            self.assertEqual(seeded.rule_spec,
                             template_hypothesis(1).rule_spec)

    def test_equity_discovery_continues_into_v3_once_every_family_is_tried(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            _seed(factory)
            previous = factory.active("equity")[0]
            seeded = discovery_hypothesis(
                previous, generation=1, not_before=None,
                existing_variant_ids=_variant_ids(factory),
                tried_families=set(RULE_FAMILIES))
            self.assertIsNotNone(seeded)
            self.assertEqual(seeded.rule_spec["schema"], RULE_SCHEMA_V3)
            self.assertIsNotNone(seeded.rule_spec["breakeven_r"])

    def test_option_discovery_stays_on_executable_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            _seed(factory, vehicle="option")
            previous = factory.active("option")[0]
            seeded = discovery_hypothesis(
                previous, generation=1, not_before=None,
                existing_variant_ids=_variant_ids(factory, "option"),
                tried_families=set(RULE_FAMILIES))
            self.assertIsNotNone(seeded)
            self.assertEqual(seeded.rule_spec["schema"], RULE_SCHEMA_V2)
            self.assertNotIn("breakeven_r", seeded.rule_spec)

    def test_the_deterministic_ladder_is_wide_and_collision_free(self):
        """A slot that runs out of families has not run out of hypotheses."""
        reachable = {rule_variant_id(discovery_spec(index, family="mean_reversion"))
                     for index in range(1, MAX_DISCOVERY_ATTEMPTS + 1)}
        self.assertEqual(len(reachable), MAX_DISCOVERY_ATTEMPTS)

    def test_discovery_returns_none_only_when_truly_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            _seed(factory, count=1)
            previous = factory.active("equity")[0]
            everything = {rule_variant_id(discovery_spec(index, family=family))
                          for family in RULE_FAMILIES
                          for index in range(0, MAX_DISCOVERY_ATTEMPTS + 1)}
            self.assertIsNone(discovery_hypothesis(
                previous, generation=1, not_before=None,
                existing_variant_ids=everything,
                tried_families=set(RULE_FAMILIES)))


class LLMDiscoveryTests(unittest.TestCase):
    """The LLM seeds slots; it never shortens the evidence path."""

    @staticmethod
    def _previous(factory):
        return factory.active("equity")[0]

    def _proposal(self, **overrides):
        spec = {"schema": RULE_SCHEMA_V2, "family": "volatility_breakout",
                "entry_after_minutes": 90, "entry_before_minutes": 300,
                "confirmations": ["volume"], "max_atr_bps": 75.0}
        spec.update(overrides.pop("rule_spec", {}))
        from agent.contracts.rule import validate_rule_spec
        normalized = validate_rule_spec(spec)
        return ProposalResult(
            True, schema=DISCOVERY_SCHEMA, rule_spec=normalized,
            variant_id=rule_variant_id(normalized),
            spec_id="abc", thesis="Midday compression resolves directionally.",
            evidence={"provider": "openai", "kind": "discovery"}, **overrides)

    def test_a_successful_proposal_becomes_the_seed_and_keeps_its_thesis(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            _seed(factory)
            previous = self._previous(factory)

            class Adapter:
                def discover(inner, *, vehicle, slot, context):
                    inner.context = context
                    return self._proposal()

            adapter = Adapter()
            seeded, proposal, source = _seed_slot(
                previous, generation=1, not_before="2026-05-01",
                existing_variant_ids=_variant_ids(factory),
                tried_families={previous["family"]},
                context=_discovery_context(
                    slot=0, reason="slot_proved_an_edge", previous=previous,
                    tried_families={previous["family"]},
                    proved_families=["opening_range_breakout"]),
                llm_enabled=True, config={"model": "gpt-5"}, adapter=adapter)
            self.assertEqual(source, "llm_discovery")
            self.assertTrue(proposal.success)
            self.assertEqual(seeded.family, "volatility_breakout")
            self.assertEqual(seeded.thesis,
                             "Midday compression resolves directionally.")
            # The brief tells the model what to avoid.
            self.assertEqual(adapter.context["reason"], "slot_proved_an_edge")
            self.assertIn("opening_range_breakout", adapter.context["proved_families"])

    def test_a_seeded_hypothesis_still_starts_queued_with_no_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            _seed(factory)
            previous = self._previous(factory)

            class Adapter:
                def discover(inner, **_):
                    return self._proposal()

            seeded, _proposal, _source = _seed_slot(
                previous, generation=1, not_before=None,
                existing_variant_ids=_variant_ids(factory),
                tried_families=set(), context={}, llm_enabled=True,
                config={"model": "gpt-5"}, adapter=Adapter())
            factory.register(seeded)
            registered = factory.hypothesis(seeded.hypothesis_id)
            # An LLM-seeded hypothesis enters the ledger exactly where a
            # deterministic one does: queued, with no run, no gate, and no
            # candidate.  It has to earn backtest_passed and then a strictly
            # later shadow pass through the same gates as everything else.
            self.assertEqual(registered["status"], "queued")
            self.assertEqual(
                [event["status"] for event in factory.events(seeded.hypothesis_id)],
                ["queued"])

    def test_a_failed_or_duplicate_proposal_falls_back_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            _seed(factory)
            previous = self._previous(factory)
            existing = _variant_ids(factory)

            class Failing:
                def discover(inner, **_):
                    return ProposalResult(False, error="provider unavailable",
                                          schema=DISCOVERY_SCHEMA)

            duplicate = self._proposal()

            class Duplicate:
                def discover(inner, **_):
                    return duplicate

            for adapter in (Failing(), Duplicate()):
                with self.subTest(adapter=type(adapter).__name__):
                    known = set(existing)
                    if isinstance(adapter, Duplicate):
                        known.add(duplicate.variant_id)
                    seeded, _proposal, source = _seed_slot(
                        previous, generation=1, not_before=None,
                        existing_variant_ids=known, tried_families=set(),
                        context={}, llm_enabled=True,
                        config={"model": "gpt-5"}, adapter=adapter)
                    self.assertEqual(source, "deterministic_discovery")
                    self.assertIsNotNone(seeded)

    def test_genesis_slots_are_discovered_not_only_templated(self):
        """A fresh ledger is where discovery matters most."""
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            briefs = []
            config = validate_config({})
            execution_geometry = _execution_geometry_context(
                vehicle="equity",
                costs=CostModel.from_config(config, vehicle="equity"),
                policy=ReplayPolicy.from_config(config),
            )

            class Adapter:
                def discover(inner, *, vehicle, slot, context):
                    briefs.append(context)
                    spec = {"schema": RULE_SCHEMA_V2, "family": "vwap_reversion",
                            # Keep each slot's deliberately distinct proposal
                            # outside the configured near-duplicate radius;
                            # the test is about genesis discovery, not alias
                            # suppression between siblings.
                            "entry_after_minutes": 30 + slot * 20,
                            "entry_before_minutes": 300}
                    from agent.contracts.rule import validate_rule_spec
                    normalized = validate_rule_spec(spec)
                    return ProposalResult(
                        True, schema=DISCOVERY_SCHEMA, rule_spec=normalized,
                        variant_id=rule_variant_id(normalized),
                        thesis=f"Slot {slot} fades the session average.",
                        evidence={"kind": "discovery"})

            seeded, revived = _ensure_slots(
                factory, edge, vehicle="equity", strategies=3,
                existing_variant_ids=set(), llm_enabled=True,
                llm_config={"model": "gpt-5"}, adapter=Adapter(),
                execution_geometry=execution_geometry)
            self.assertEqual(revived, [])
            self.assertEqual(len(seeded), 3)
            self.assertEqual({item["source"] for item in seeded},
                             {"llm_discovery"})
            self.assertEqual({item["family"] for item in seeded},
                             {"vwap_reversion"})
            self.assertEqual([brief["reason"] for brief in briefs],
                             ["fresh_slot"] * 3)
            self.assertEqual(
                [brief["execution_geometry"] for brief in briefs],
                [execution_geometry] * 3)
            self.assertEqual(len(factory.active("equity")), 3)

    def test_each_genesis_slot_is_told_what_its_siblings_just_took(self):
        """Slots are seeded in sequence inside one cycle.

        Without this the brief is identical for every slot of a fresh ledger —
        empty tried/proved families — so a model that answers consistently
        returns the same hypothesis N times, N-1 are dropped as duplicates, and
        the deployment pays for N proposals to fill one slot.
        """
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            briefs = []

            class Steady:
                """A realistic model: same brief in, same answer out."""

                def discover(inner, *, vehicle, slot, context):
                    briefs.append(context)
                    from agent.contracts.rule import validate_rule_spec
                    taken = {item["family"] for item in
                             context.get("already_seeded_this_cycle") or ()}
                    family = next(name for name in RULE_FAMILIES
                                  if name not in taken)
                    spec = validate_rule_spec({"family": family})
                    return ProposalResult(
                        True, schema=DISCOVERY_SCHEMA, rule_spec=spec,
                        variant_id=rule_variant_id(spec),
                        thesis=f"{family} should carry an edge.",
                        evidence={"kind": "discovery"})

            seeded, _revived = _ensure_slots(
                factory, edge, vehicle="equity", strategies=4,
                existing_variant_ids=set(), llm_enabled=True,
                llm_config={"model": "gpt-5"}, adapter=Steady())

            self.assertEqual([item["source"] for item in seeded],
                             ["llm_discovery"] * 4)
            self.assertEqual(len({item["family"] for item in seeded}), 4)
            # The first slot has no siblings yet; each later one is told about
            # every slot already seeded in this same cycle.
            self.assertEqual(briefs[0].get("already_seeded_this_cycle"), None)
            for index, brief in enumerate(briefs[1:], start=1):
                self.assertEqual(len(brief["already_seeded_this_cycle"]), index)
                self.assertEqual(
                    [row["slot"] for row in brief["already_seeded_this_cycle"]],
                    list(range(index)))

    def test_a_revived_slot_is_told_about_the_slots_still_running(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)
            _seed(factory, count=3)
            briefs = []

            class Recorder:
                def discover(inner, *, vehicle, slot, context):
                    briefs.append(context)
                    return ProposalResult(False, error="deterministic please")

            _prove_every_active_slot(factory)
            _ensure_slots(factory, edge, vehicle="equity", strategies=3,
                          existing_variant_ids=_variant_ids(factory),
                          llm_enabled=True, llm_config={"model": "gpt-5"},
                          adapter=Recorder())
            self.assertEqual(len(briefs), 3)
            for index, brief in enumerate(briefs):
                seen = brief.get("already_seeded_this_cycle") or []
                self.assertEqual(len(seen), index)

    def test_genesis_falls_back_to_templates_when_discovery_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)

            class Failing:
                def discover(inner, **_):
                    return ProposalResult(False, schema=DISCOVERY_SCHEMA,
                                          error="provider unavailable")

            seeded, _revived = _ensure_slots(
                factory, edge, vehicle="equity", strategies=STRATEGIES,
                existing_variant_ids=set(), llm_enabled=True,
                llm_config={"model": "gpt-5"}, adapter=Failing())
            self.assertEqual({item["source"] for item in seeded}, {"template"})
            self.assertEqual([item["rule_spec"] for item in seeded],
                             [h.rule_spec for h in initial_hypotheses(STRATEGIES)])

    def test_a_broken_adapter_degrades_instead_of_failing_the_cycle(self):
        """A nightly job must not die because a provider seam misbehaves."""
        with tempfile.TemporaryDirectory() as directory:
            factory, edge = _ledgers(directory)

            class Exploding:
                def discover(inner, **_):
                    raise RuntimeError("provider exploded")

            class NoDiscovery:
                def propose(inner, **_):
                    raise AssertionError("propose is not the discovery seam")

            for adapter in (Exploding(), NoDiscovery()):
                with self.subTest(adapter=type(adapter).__name__):
                    seeded, _revived = _ensure_slots(
                        factory, edge, vehicle="equity", strategies=STRATEGIES,
                        existing_variant_ids=_variant_ids(factory),
                        llm_enabled=True, llm_config={"model": "gpt-5"},
                        adapter=adapter)
                    self.assertEqual(len(factory.active("equity")), STRATEGIES)

    def test_discovery_is_never_called_when_the_llm_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            _seed(factory)

            class Exploding:
                def discover(inner, **_):
                    raise AssertionError("the adapter must not be consulted")

            seeded, proposal, source = _seed_slot(
                self._previous(factory), generation=1, not_before=None,
                existing_variant_ids=_variant_ids(factory),
                tried_families=set(), context={}, llm_enabled=False,
                config={}, adapter=Exploding())
            self.assertEqual(source, "deterministic_discovery")
            self.assertIsNone(proposal)
            self.assertIsNotNone(seeded)


class RunFactoryRecoveryTests(unittest.TestCase):
    """The orchestrator itself must never report a permanently idle factory."""

    def test_a_fully_proved_ledger_is_revived_instead_of_reported_exhausted(self):
        from tests.research.test_strategy_factory import losing_breakouts
        from research.strategy_factory import run_factory
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge_lab.sqlite3"
            factory = FactoryLedger(path)
            _seed(factory)
            _prove_every_active_slot(factory)
            self.assertEqual(factory.active("equity"), [])

            result = run_factory(losing_breakouts(), db_path=path,
                                 vehicle="equity", strategies=STRATEGIES,
                                 variants_per_strategy=2, workers=2)
            self.assertNotEqual(result["status"], "exhausted")
            self.assertEqual(len(result["revived"]), STRATEGIES)
            self.assertEqual(result["active_slots"], STRATEGIES)

    def test_a_healthy_cycle_revives_nothing_and_keeps_full_capacity(self):
        from tests.research.test_strategy_factory import losing_breakouts
        from research.strategy_factory import run_factory
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge_lab.sqlite3"
            result = run_factory(losing_breakouts(), db_path=path,
                                 vehicle="equity", strategies=STRATEGIES,
                                 variants_per_strategy=2, workers=2)
            self.assertEqual(result["revived"], [])
            self.assertEqual(result["reseeds"], [])
            self.assertEqual(result["active_slots"], STRATEGIES)


class ProvedFamilyTests(unittest.TestCase):
    def test_proved_families_reads_deployed_candidates_only(self):
        with tempfile.TemporaryDirectory() as directory:
            _factory, edge = _ledgers(directory)
            for variant, family, status in (
                    ("rule.mean-reversion.a", "mean_reversion", "validated"),
                    ("rule.trend-pullback.b", "trend_pullback", "candidate")):
                record = edge.register_candidate(
                    variant, strategy_id="rule", vehicle="equity",
                    hypothesis="h", config={"strategy": {
                        "rule_spec": {"family": family}}})
                if status == "validated":
                    import sqlite3
                    from contextlib import closing
                    with closing(sqlite3.connect(edge.path)) as db, db:
                        db.execute(
                            "UPDATE candidate_state SET status=? WHERE candidate_id=?",
                            (status, record["candidate_id"]))
            self.assertEqual(_proved_families(edge, "equity"), ["mean_reversion"])


if __name__ == "__main__":
    unittest.main()
