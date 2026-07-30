"""B3: the replay must be deterministic, offline, and made of production code.

A replay harness has one job that outranks accuracy: being the same thing
twice. A run that varies between invocations cannot settle any question,
because the difference between two variants is then indistinguishable from
the difference between two runs of the same variant.

The isolation assertions matter for a related reason. A replay that reached
the network would be reading data revised since the decision was made, and a
replay that called an LLM would cost money per variant and stop being a
replay at all.
"""

import unittest
from unittest.mock import patch

from agent import strategy
from research import replay
from research.corpus import CycleRecord, ModelOutput
from tests.helpers import valid_config


SIGNAL_TS = 1_760_000_000_000


def row(**overrides) -> dict:
    base = {
        "price": 100.0, "signal_ts": SIGNAL_TS,
        "trend_15m": "up", "trend_1h": "up", "trend_4h": "up",
        "mom_1h_pct": 0.6, "mom_15m_pct": 0.3,
        "atr_1h_pct": 0.8, "atr_1h_ratio": 1.0, "ema20_1h_dist_pct": 0.2,
        "range_pos_pct": 60.0, "relative_volume_1h": 1.4,
        "funding_rate_pct": 0.005, "funding_interval_hours": 8,
        "funding_percentile_30": 50, "funding_samples_30": 30,
        "funding_mean_30_pct": 0.004, "next_funding_minutes": 240,
        "perp_index_basis_pct": 0.01, "open_interest_musd": 500.0,
        "swing_high_pct": 2.0, "swing_low_pct": 2.0,
        "spread_pct": 0.02, "taker_fee_pct_per_side": 0.05,
    }
    base.update(overrides)
    return base


def cycle(n: int = 0, symbols=("BTC/USDT:USDT",), **overrides) -> CycleRecord:
    cfg = valid_config()
    ts = 1_760_000_000.0 + n * 300
    # The signal bar advances with wall time, as it does live: a 300s cycle
    # against a 900s bar sees each bar about three times. A fixture that
    # reused one signal_ts forever would be burned by per-bar idempotency
    # after the first cycle and would test nothing beyond it.
    overrides.setdefault("signal_ts", int(ts // 900) * 900 * 1000)
    snapshot = {}
    for symbol in symbols:
        data = row(**overrides)
        strategy.enrich_snapshot(data, cfg)
        snapshot[symbol] = data
    snapshot["_market_context"] = {"benchmark": "BTC/USDT:USDT"}
    return CycleRecord(
        ts=ts, run_id="run-a", cycle_id=f"c{n}",
        snapshot=snapshot, portfolio={"equity_usdt": 10_000.0},
        max_new=3, provider="anthropic")


def model_output(n: int, decisions: list) -> ModelOutput:
    return ModelOutput(
        ts=1_760_000_000.0 + n * 300, cycle_id=f"c{n}", response_id=f"r{n}",
        raw_text="{}", parsed_decisions=decisions, request_attempts=1)


def open_decision(symbol="BTC/USDT:USDT", direction="long",
                  setup_type="trend_continuation", confidence=0.8) -> dict:
    return {
        "symbol": symbol, "action": "open", "direction": direction,
        "setup_type": setup_type, "confidence": confidence,
        "invalidation_anchor": "structure", "exit_policy": "fixed_rr",
    }


class DeterminismTests(unittest.TestCase):
    def test_same_corpus_and_variant_twice_is_byte_identical(self):
        cycles = [cycle(i) for i in range(5)]
        outputs = [model_output(i, [open_decision()]) for i in range(5)]

        first = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)
        second = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)

        self.assertEqual(first.digest(), second.digest())
        self.assertEqual(len(first.decisions), len(second.decisions))

    def test_variant_recomputes_evidence_instead_of_using_snapshot_cache(self):
        candidate_cfg = valid_config()
        candidate_cfg["strategy"]["breakout_discriminator"] = (
            "volatility_regime")
        observed = cycle(0)
        observed.snapshot["BTC/USDT:USDT"]["setup_evidence"] = {
            "trend_continuation": {"long": False}}

        with patch("agent.strategy.setup_evidence",
                   wraps=strategy.setup_evidence) as evidence:
            replay.Replay(candidate_cfg, mode="deterministic").run(
                [observed], [])

        self.assertTrue(evidence.called)
        self.assertIn(
            candidate_cfg,
            [call.args[1] for call in evidence.call_args_list])

    def test_the_deterministic_arm_is_also_stable(self):
        cycles = [cycle(i) for i in range(5)]

        first = replay.Replay(valid_config(), mode="deterministic").run(
            cycles, [])
        second = replay.Replay(valid_config(), mode="deterministic").run(
            cycles, [])

        self.assertEqual(first.digest(), second.digest())


class IsolationTests(unittest.TestCase):
    """Zero network calls and zero LLM calls, asserted."""

    def test_replay_makes_no_network_call(self):
        cycles = [cycle(i) for i in range(3)]

        with patch("ccxt.okx") as exchange:
            replay.Replay(valid_config(), mode="deterministic").run(
                cycles, [])

        exchange.assert_not_called()

    def test_replay_makes_no_llm_call(self):
        cycles = [cycle(i) for i in range(3)]

        with patch("agent.brain.LLM") as llm:
            replay.Replay(valid_config(), mode="deterministic").run(
                cycles, [])

        llm.assert_not_called()

    def test_replay_places_no_order(self):
        cycles = [cycle(i) for i in range(3)]

        with patch("agent.exchange.Exchange") as ex:
            replay.Replay(valid_config(), mode="deterministic").run(
                cycles, [])

        ex.assert_not_called()


class ProductionCodeTests(unittest.TestCase):
    """The replay calls the real functions; it does not reimplement them."""

    def test_build_setup_plan_is_the_production_one(self):
        cycles = [cycle(0)]
        outputs = [model_output(0, [open_decision()])]

        with patch("agent.strategy.build_setup_plan",
                   wraps=strategy.build_setup_plan) as spy:
            replay.Replay(valid_config(), mode="recorded_llm").run(
                cycles, outputs)

        self.assertTrue(spy.called, "replay must call the real contract")

    def test_vet_open_is_the_production_one(self):
        from agent.risk import RiskEngine
        cycles = [cycle(0)]
        outputs = [model_output(0, [open_decision()])]

        with patch.object(RiskEngine, "vet_open",
                          autospec=True,
                          side_effect=RiskEngine.vet_open) as spy:
            replay.Replay(valid_config(), mode="recorded_llm").run(
                cycles, outputs)

        self.assertTrue(spy.called, "replay must call the real risk engine")

    def test_vet_open_receives_the_cycle_clock_not_wall_clock(self):
        """A wall-clock replay expires every cooldown in the corpus."""
        from agent.risk import RiskEngine
        cycles = [cycle(0)]
        outputs = [model_output(0, [open_decision()])]
        seen = []

        original = RiskEngine.vet_open

        def capture(self, *args, **kwargs):
            seen.append(kwargs.get("now"))
            return original(self, *args, **kwargs)

        with patch.object(RiskEngine, "vet_open", autospec=True,
                          side_effect=capture):
            replay.Replay(valid_config(), mode="recorded_llm").run(
                cycles, outputs)

        self.assertTrue(seen)
        self.assertEqual(seen[0], cycles[0].ts)


class TrendMajorityTests(unittest.TestCase):
    def test_two_of_three_up_is_long(self):
        self.assertEqual(
            replay.trend_majority(
                {"trend_15m": "up", "trend_1h": "up", "trend_4h": "flat"}),
            "long")

    def test_two_of_three_down_is_short(self):
        self.assertEqual(
            replay.trend_majority(
                {"trend_15m": "down", "trend_1h": "down", "trend_4h": "up"}),
            "short")

    def test_a_tie_produces_no_direction(self):
        """A coin flip would add variance the LLM arm does not have."""
        self.assertIsNone(replay.trend_majority(
            {"trend_15m": "up", "trend_1h": "down", "trend_4h": "flat"}))

    def test_all_flat_produces_no_direction(self):
        self.assertIsNone(replay.trend_majority(
            {"trend_15m": "flat", "trend_1h": "flat", "trend_4h": "flat"}))


class FunnelTests(unittest.TestCase):
    def test_funnel_counts_reconcile(self):
        cycles = [cycle(i) for i in range(4)]
        outputs = [model_output(i, [open_decision()]) for i in range(4)]

        result = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)
        funnel = result.funnel

        self.assertEqual(funnel["fired"],
                         funnel["vetoed"] + funnel["executed"])

    def test_veto_reasons_are_attributed(self):
        # A snapshot the contract does not fire on: no trend alignment.
        cycles = [cycle(0, trend_1h="down", trend_4h="down",
                        mom_1h_pct=-0.6, mom_15m_pct=-0.3)]
        outputs = [model_output(0, [open_decision(direction="long")])]

        result = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)

        self.assertGreater(result.funnel["vetoed"], 0)
        self.assertTrue(result.funnel["veto_reasons"])


class PortfolioStateTests(unittest.TestCase):
    def test_state_model_reports_modelled_and_unmodelled_transitions(self):
        result = replay.Replay(valid_config(), mode="recorded_llm").run(
            [cycle(0)], [model_output(0, [open_decision()])])

        diagnostics = result.state_diagnostics
        self.assertIn("equity_feedback", diagnostics["modelled_fields"])
        self.assertIn("positions", diagnostics["modelled_fields"])
        self.assertIn("exchange_fill_race", diagnostics["unmodelled_fields"])
        self.assertIn("final_state", diagnostics)
        self.assertGreaterEqual(
            diagnostics["transitions"]["positions_opened"], 0)

    def test_daily_stop_flattens_remaining_positions_when_configured(self):
        class MixedExit:
            @staticmethod
            def bars(symbol, start_ms, end_ms):
                from research.prices import Bar
                if symbol.startswith("BTC/"):
                    return [Bar(int(start_ms), 100.0, 100.5, 90.0, 95.0, 1.0)]
                return [Bar(
                    int(end_ms) - 60_000, 100.0, 100.2, 99.8, 100.0, 1.0)]

        cfg = valid_config()
        cfg["risk"]["daily_loss_limit_pct"] = 0.1
        cfg["risk"]["max_hold_hours"] = 1
        cfg["execution"]["flatten_on_daily_stop"] = True
        symbols = ("BTC/USDT:USDT", "ETH/USDT:USDT")
        cycles = [cycle(i, symbols=symbols) for i in range(3)]
        outputs = [
            model_output(i, [open_decision(symbol=symbol) for symbol in symbols])
            for i in range(3)
        ]

        result = replay.Replay(
            cfg, mode="recorded_llm", price_cache=MixedExit()).run(
                cycles, outputs)

        self.assertEqual(result.state_diagnostics["final_state"]["state"],
                         "DAY_STOPPED")
        self.assertEqual(result.state_diagnostics["final_state"]["positions"],
                         0)
        self.assertGreaterEqual(
            result.state_diagnostics["transitions"]
            ["circuit_positions_flattened"], 1)

    def test_positions_carry_forward_across_cycles(self):
        """Exposure caps must bind the way they do live."""
        cycles = [cycle(i, symbols=(f"S{j}/USDT:USDT" for j in range(6)))
                  for i in range(3)]
        outputs = [
            model_output(i, [open_decision(symbol=f"S{j}/USDT:USDT")
                             for j in range(6)])
            for i in range(3)
        ]

        result = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)

        # Something must be refused: six symbols per cycle across three
        # cycles cannot all clear the gross and net-direction caps.
        self.assertGreater(result.funnel["vetoed"], 0)

    def test_a_flat_start_would_execute_more_than_a_stateful_one(self):
        cycles = [cycle(i, symbols=(f"S{j}/USDT:USDT" for j in range(6)))
                  for i in range(3)]
        outputs = [
            model_output(i, [open_decision(symbol=f"S{j}/USDT:USDT")
                             for j in range(6)])
            for i in range(3)
        ]

        result = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)

        self.assertLess(result.funnel["executed"], funnel_upper_bound(cycles))


    def test_a_position_releases_its_slot_at_max_hold(self):
        """Otherwise the concurrency cap dominates as a simulation artefact."""
        cfg = valid_config()
        cfg["risk"]["max_hold_hours"] = 1

        # 40 cycles at 300s spans 3.3 hours, so early positions must expire.
        cycles = [cycle(i) for i in range(40)]
        outputs = [model_output(i, [open_decision()]) for i in range(40)]

        result = replay.Replay(cfg, mode="recorded_llm").run(cycles, outputs)

        self.assertGreater(
            result.funnel["executed"], 1,
            "a slot freed by max hold must be reusable by a later cycle")

    def test_a_resolved_exit_frees_the_slot_earlier_than_max_hold(self):
        class InstantExit:
            """A cache whose bars stop the trade on the first minute."""

            @staticmethod
            def bars(symbol, start_ms, end_ms):
                from research.prices import Bar
                return [Bar(int(start_ms), 100.0, 100.5, 90.0, 95.0, 1.0)]

        cfg = valid_config()
        cycles = [cycle(i) for i in range(6)]
        outputs = [model_output(i, [open_decision()]) for i in range(6)]

        engine = replay.Replay(cfg, mode="recorded_llm",
                               price_cache=InstantExit())
        result = engine.run(cycles, outputs)

        executed = result.executed()
        self.assertTrue(executed)
        self.assertTrue(all(d.outcome for d in executed))
        self.assertTrue(all(d.outcome["exit_ts"] for d in executed))


def funnel_upper_bound(cycles) -> int:
    return sum(len(c.symbols()) for c in cycles)


if __name__ == "__main__":
    unittest.main()


class SetupMemoryTests(unittest.TestCase):
    """The replay keeps setup memory, so its funnel is not an upper bound.

    _prepare_setup_decision applies three checks beyond the contract: per-bar
    idempotency, semantic cooldown, and failed-thesis re-entry. A replay
    without them re-proposes setups the live agent had already refused, which
    inflates the proposal count and distorts the veto distribution that gate
    G4 reads - in the direction that makes the contract look busier than it
    is.
    """

    def test_a_symbol_is_evaluated_once_per_signal_bar(self):
        # Three cycles inside one 900s bar, all proposing the same symbol.
        cycles = [cycle(i, signal_ts=1_760_000_000_000) for i in range(3)]
        outputs = [model_output(i, [open_decision()]) for i in range(3)]

        result = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)

        reasons = result.funnel["veto_reasons"]
        self.assertTrue(
            any("already evaluated" in r for r in reasons),
            f"per-bar idempotency did not bind: {reasons}")

    def test_a_new_bar_is_a_fresh_opportunity(self):
        """Burning the bar must not burn the symbol forever."""
        cycles = [cycle(i) for i in range(10)]        # bars advance
        outputs = [model_output(i, [open_decision()]) for i in range(10)]

        result = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)

        self.assertGreater(result.funnel["executed"], 0)

    def test_the_funnel_still_reconciles_with_memory_in_play(self):
        cycles = [cycle(i) for i in range(12)]
        outputs = [model_output(i, [open_decision()]) for i in range(12)]

        result = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)
        funnel = result.funnel

        self.assertEqual(funnel["fired"],
                         funnel["vetoed"] + funnel["executed"])

    def test_memory_makes_the_replay_more_conservative_not_less(self):
        """Every memory check can only remove proposals, never add them."""
        cycles = [cycle(i, signal_ts=1_760_000_000_000) for i in range(6)]
        outputs = [model_output(i, [open_decision()]) for i in range(6)]

        result = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)

        self.assertLessEqual(result.funnel["executed"], 1,
                             "one bar can produce at most one entry")

    def test_memory_is_still_deterministic(self):
        cycles = [cycle(i) for i in range(8)]
        outputs = [model_output(i, [open_decision()]) for i in range(8)]

        first = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)
        second = replay.Replay(valid_config(), mode="recorded_llm").run(
            cycles, outputs)

        self.assertEqual(first.digest(), second.digest())

    def test_the_memory_checks_use_the_cycle_clock(self):
        """Wall-clock would expire every cooldown in the corpus at once."""
        import inspect
        source = inspect.getsource(replay.Replay.run)

        self.assertIn("semantic_block(", source)
        self.assertIn("now=now", source)
        self.assertNotIn("time.time()", source)
