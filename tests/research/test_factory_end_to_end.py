"""End-to-end proof that a gate pass can actually be earned, and paid for.

Every other ``run_factory`` test either feeds a fixture built to fail or
patches ``_worker`` with synthetic evidence, so nothing exercised

    real bars -> real replay -> real costs -> gate evaluation -> PASS

and "no previously-passing fixture now fails" was therefore vacuous.  These
tests drive the real orchestrator over the real worker with an economically
real synthetic edge, and then require that the same edge *fails* under
materially worse costs.  The second half is what makes the cost model
load-bearing rather than merely asserted.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.rule import rule_variant_id, validate_rule_spec
from research.costs import CostModel
from research.edge_lab import _read_discovery_rows
from research.factory_core import simulate_account
from research.strategy_factory import run_factory

NEW_YORK = ZoneInfo("America/New_York")
SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")
# `_simulate_trade` takes at most one trade per symbol-session, so the 100-trade
# held-out floor is a symbol count as much as a session count.  56 sessions seal
# 11, leaving 45 development sessions that split 31/14; 14 held-out sessions
# across eight symbols is 112 trades, clearing both floors without slack to
# spare.
SESSIONS = 56
# Slot 0 of the shipped catalog.  The factory diagnoses this on fit data and,
# finding no failure, proposes `target_r * 1.25` as its first variant.
ROOT_SPEC = validate_rule_spec({
    "family": "opening_range_breakout", "range_minutes": 15,
    "threshold_bps": 5.0, "confirmation": "volume"})
WINNING_SPEC = validate_rule_spec({**ROOT_SPEC, "target_r": 2.5})
ROOT_VARIANT = rule_variant_id(ROOT_SPEC)
WINNING_VARIANT = rule_variant_id(WINNING_SPEC)
# Six bps of spread and fifteen of slippage: still inside the runtime's own
# rejection caps, so this is a fill the trader would accept, not an impossible
# one chosen to guarantee the failure.
WORSE_COSTS = CostModel(spread_bps=12.0, slippage_bps=15.0, fee_bps=1.0)


def _wobble(symbol: str, day: int) -> float:
    """Deterministic per-symbol-session jitter; never large enough to change
    which side of a level a bar closes on."""
    return ((sum(ord(char) for char in symbol) * 31 + day * 17) % 7 - 3) * .01


def _session_closes(symbol: str, day: int) -> tuple[list[float], list[int]]:
    """One session with a genuine, mechanical opening-range-breakout edge.

    A steady decline builds the opening range, one high-volume bar breaks it
    upward, the move runs far enough to pay a 2.5R target, and the whole of it
    is given back afterwards.  The give-back is deliberate: it is what makes a
    randomly timed entry lose, so beating the randomized-entry null measures
    timing rather than a session-long drift any entry would have caught.
    """
    drift = _wobble(symbol, day)
    closes: list[float] = []
    volumes: list[int] = []
    price = 100.60
    for _ in range(15):
        price -= .05
        closes.append(price)
        volumes.append(1000)
    closes.append(100.85 + drift)
    volumes.append(6000)
    for step in (.20, .40, .60):
        closes.append(100.85 + drift + step)
        volumes.append(3000)
    price = 101.45 + drift
    for _ in range(15):
        price -= .10
        closes.append(price)
        volumes.append(1200)
    return closes, volumes


def _session_rows(symbol: str, day: int, opening: datetime) -> list[dict]:
    closes, volumes = _session_closes(symbol, day)
    rows = []
    opened = 100.60
    for index, (close, volume) in enumerate(zip(closes, volumes)):
        stamp = opening + timedelta(minutes=index)
        end = stamp + timedelta(minutes=1)
        rows.append({
            "kind": "bar", "provider": "test", "feed": "sip", "symbol": symbol,
            "timestamp": stamp.isoformat(), "as_of": end.isoformat(),
            "observed_at": end.isoformat(), "open": round(opened, 4),
            "high": round(max(opened, close) + .02, 4),
            "low": round(min(opened, close) - .02, 4),
            "close": round(close, 4), "volume": volume,
        })
        opened = close
    return rows


def edge_corpus(sessions: int = SESSIONS, *, skip: int = 0) -> list[dict]:
    """Consecutive weekday sessions of the corpus above, ``skip`` days in."""
    rows: list[dict] = []
    day = 0
    stamp = datetime(2026, 1, 5, 9, 30, tzinfo=NEW_YORK)
    while day < skip + sessions:
        if stamp.weekday() < 5:
            if day >= skip:
                for symbol in SYMBOLS:
                    rows.extend(_session_rows(symbol, day,
                                              stamp.astimezone(timezone.utc)))
            day += 1
        stamp += timedelta(days=1)
    return rows


class EarnedGatePassTests(unittest.TestCase):
    """The real worker, the real replay, the real costs, and a real pass."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = edge_corpus()
        cls.directory = tempfile.TemporaryDirectory(prefix="alpaca-e2e-")
        cls.db = Path(cls.directory.name) / "edge.sqlite3"
        # No patching of `_worker`, no synthetic rows: everything below is
        # replayed from these bars through the shared cost model.
        cls.result = run_factory(cls.corpus, db_path=cls.db, strategies=1,
                                 variants_per_strategy=2, workers=1)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def _by_variant(self, result):
        return {row["variant_id"]: row for row in result["results"]}

    def test_a_replayed_edge_earns_a_passing_backtest_gate(self):
        self.assertEqual(self.result["status"], "complete")
        rows = self._by_variant(self.result)
        self.assertEqual(sorted(rows), sorted([ROOT_VARIANT, WINNING_VARIANT]))
        winner = rows[WINNING_VARIANT]
        gate = winner["gate"]
        self.assertTrue(gate["passes"])
        # Every mandatory check, not merely the aggregate, and the structural
        # floors that the shipped defaults require.
        self.assertEqual([name for name, ok in gate["checks_without_family"].items()
                          if not ok], [])
        self.assertTrue(gate["multiple_tests"]["significant"])
        self.assertTrue(gate["global_multiple_tests"]["significant"])
        self.assertGreaterEqual(gate["heldout_floor"]["trades"], 100)
        self.assertGreaterEqual(gate["heldout_floor"]["sessions"], 10)
        self.assertGreater(gate["heldout_net_pnl"], 0.0)
        self.assertGreater(gate["heldout_expectancy"], 0.0)
        self.assertEqual(winner["status"], "backtest_passed")
        self.assertEqual(winner["mode"], "backtest")

    def test_the_unmutated_root_does_not_pass_its_own_control(self):
        # The root is its own matched baseline, so every delta is exactly zero.
        # A pass here would mean the control was not actually being subtracted.
        gate = self._by_variant(self.result)[ROOT_VARIANT]["gate"]
        self.assertFalse(gate["passes"])
        self.assertEqual(gate["test"]["mean_delta"], 0.0)
        self.assertGreater(gate["heldout_net_pnl"], 0.0)
        self.assertEqual(self._by_variant(self.result)[ROOT_VARIANT]["status"],
                         "retired")

    def test_the_lifecycle_reaches_validated_and_a_champion(self):
        # A backtest pass is not runtime eligibility.  A second cycle over
        # strictly later, never-simulated sessions must carry the same variant
        # through shadow to validated before a champion exists at all.
        with tempfile.TemporaryDirectory(prefix="alpaca-e2e-fwd-") as forward:
            shutil.copytree(self.directory.name, forward, dirs_exist_ok=True)
            result = run_factory(edge_corpus(30, skip=SESSIONS),
                                 db_path=Path(forward) / "edge.sqlite3",
                                 strategies=1, variants_per_strategy=2,
                                 workers=1)
        rows = self._by_variant(result)
        self.assertEqual(list(rows), [WINNING_VARIANT])
        self.assertEqual(rows[WINNING_VARIANT]["mode"], "shadow")
        self.assertTrue(rows[WINNING_VARIANT]["gate"]["passes"])
        self.assertEqual(rows[WINNING_VARIANT]["status"], "validated")
        self.assertEqual(result["champion"]["variant_id"], WINNING_VARIANT)

    def test_the_pass_is_bought_and_worse_costs_take_it_away(self):
        # The identical corpus under a materially worse — but still runtime
        # acceptable — cost model must not produce a passing candidate.  This
        # is the assertion Phase 4 could not make: without it, "no previously
        # passing fixture now fails" is true of a suite in which nothing ever
        # passed.
        with tempfile.TemporaryDirectory(prefix="alpaca-e2e-cost-") as directory:
            result = run_factory(self.corpus,
                                 db_path=Path(directory) / "edge.sqlite3",
                                 strategies=1, variants_per_strategy=2,
                                 workers=1, costs=WORSE_COSTS)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([row["variant_id"] for row in result["results"]
                          if row["gate"]["passes"]], [])
        self.assertIsNone(result["champion"])
        self.assertNotIn("validated", {row["status"] for row in result["results"]})
        # The root's own after-cost held-out result, positive above, is now a
        # loss: the costs are what moved, nothing else.
        root = self._by_variant(result)[ROOT_VARIANT]["gate"]
        self.assertLess(root["heldout_net_pnl"], 0.0)

    def test_the_winning_spec_itself_flips_sign_on_costs_alone(self):
        # Worse costs also change which variant the diagnosis proposes, so pin
        # the claim to one spec over one corpus with only the model swapped.
        _, bars, _, quotes = _read_discovery_rows(self.corpus)
        books = {}
        for name, model in (("real", CostModel()), ("worse", WORSE_COSTS)):
            books[name] = simulate_account(
                bars, [], WINNING_SPEC, vehicle="equity",
                account_id=f"cost:{name}", costs=model, quotes=list(quotes))
        self.assertEqual(books["real"]["trades"], books["worse"]["trades"])
        self.assertGreater(books["real"]["realized_pnl"], 0.0)
        self.assertLess(books["worse"]["realized_pnl"], 0.0)


if __name__ == "__main__":
    unittest.main()
