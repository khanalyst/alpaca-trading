"""Focused correctness checks for real-time shadow execution evidence."""

import json
import sqlite3
import unittest

from agent import brain, shadow, variants
from agent.forward_models import BY_STRATEGY
from agent.market import _funding_history_context
from research.findings import _assignment_trade_costs
from tests.helpers import valid_config
from tests.research.test_enrichment_isolation import (execution_enriched,
                                                      symbol_snapshot)
from tests.research.test_forward_models import market_row
from tests.research.test_shadow_isolation import proposal, variant


SYMBOL = "BTC/USDT:USDT"
NOW = 1_760_000_040.0
NOW_MS = int(NOW * 1000)
MINUTE_MS = 60_000


def execution_row(now=NOW, *, bars=None, funding_events=None):
    row = execution_enriched(symbol_snapshot())
    enrichment = dict(row[brain.ENRICHMENT_KEY])
    observed_ms = int(now * 1000)
    enrichment.update({
        "ticker_ts": observed_ms,
        "ticker_age_seconds": 0.0,
        "ticker_best_bid": 99.99,
        "ticker_best_ask": 100.01,
        "book_ts": observed_ms,
        "book_age_seconds": 0.0,
        "execution_bars": list(bars or []),
        "realized_funding_events": list(funding_events or []),
    })
    row[brain.ENRICHMENT_KEY] = enrichment
    return row


def one_minute_bar(timestamp_ms, *, open_=100.0, high=100.2, low=99.8,
                   close=100.0, complete=True):
    return {
        "timestamp_ms": int(timestamp_ms),
        "open": float(open_), "high": float(high), "low": float(low),
        "close": float(close), "volume": 1_000.0,
        "complete": bool(complete),
    }


def open_position(**overrides):
    base = {
        "direction": "long", "entry_price": 100.0,
        "stop_price": 99.0, "take_price": 101.0, "leverage": 2.0,
        "entry_ts": NOW, "fill_ts": NOW,
        "entry_bar_ts_ms": NOW_MS,
        "last_execution_bar_ts_ms": NOW_MS,
        "deadline_ts": NOW + 120.0,
        "data_status": "observed",
    }
    base.update(overrides)
    return base


class FundingSettlementTests(unittest.TestCase):
    def test_only_explicit_realized_rate_becomes_money(self):
        class Client:
            @staticmethod
            def fetch_funding_rate_history(symbol, since, limit):
                del symbol, since, limit
                return [
                    {"fundingRate": "0.001", "timestamp": NOW_MS - 2},
                    {"fundingRate": "0.002", "realizedRate": "0.0015",
                     "timestamp": NOW_MS - 1},
                ]

        class Exchange:
            x = Client()

            @staticmethod
            def retry(fn, *args, **kwargs):
                return fn(*args, **kwargs)

        context = _funding_history_context(
            Exchange(), "REALIZED-ONLY/USDT:USDT", 0.2)

        self.assertEqual(context["funding_samples_30"], 2)
        self.assertAlmostEqual(context["funding_mean_30_pct"], 0.15)
        self.assertEqual(len(context["_realized_funding_events"]), 1)
        event = context["_realized_funding_events"][0]
        self.assertAlmostEqual(event["rate_pct"], 0.15)
        self.assertIn("realizedRate", event["source"])


class CoverageAndFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.cfg = valid_config()

    def test_empty_bars_cannot_be_a_valid_timeout(self):
        now = NOW + 180.0
        resolved = shadow.ShadowEvaluator._execution_exit(
            open_position(deadline_ts=NOW + 60.0),
            execution_row(now), now, self.cfg)

        self.assertIsNotNone(resolved)
        result, _, evidence, valid, _ = resolved
        self.assertEqual(result, "execution_data_gap")
        self.assertFalse(valid)
        self.assertEqual(evidence["coverage_kind"],
                         "empty_or_trailing_gap")

    def test_trailing_missing_bar_cannot_be_a_valid_timeout(self):
        now = NOW + 180.0
        bars = [one_minute_bar(NOW_MS + MINUTE_MS)]
        resolved = shadow.ShadowEvaluator._execution_exit(
            open_position(deadline_ts=NOW + 60.0),
            execution_row(now, bars=bars), now, self.cfg)

        self.assertEqual(resolved[0], "execution_data_gap")
        self.assertEqual(resolved[2]["coverage_kind"],
                         "empty_or_trailing_gap")
        self.assertFalse(resolved[3])

    def test_gap_without_a_fresh_quote_stays_unresolved(self):
        now = NOW + 180.0
        row = execution_row(now, bars=[])
        row[brain.ENRICHMENT_KEY]["ticker_ts"] = int(
            (now - 30.0) * 1000)

        resolved = shadow.ShadowEvaluator._execution_exit(
            open_position(deadline_ts=NOW + 60.0), row, now, self.cfg)

        self.assertIsNone(resolved)

    def test_stale_ticker_and_book_are_rechecked_at_open(self):
        for stale_field, expected in (
                ("ticker_ts", "ticker is 11.0s old"),
                ("book_ts", "order book is 11.0s old")):
            with self.subTest(field=stale_field):
                evaluator = shadow.ShadowEvaluator(
                    [variant()], valid_config(), budget_ms=0)
                row = execution_row(NOW)
                row[brain.ENRICHMENT_KEY][stale_field] = int(
                    (NOW - 11.0) * 1000)

                records = evaluator.evaluate(
                    {SYMBOL: row}, now=NOW, proposals=[proposal()])

                self.assertEqual(records[0].outcome, "vetoed")
                self.assertIn(expected, records[0].reason)
                self.assertEqual(evaluator.store.paper_trades_for(
                    evaluator.scope_key, variant().variant_id), [])


class MakerChronologyTests(unittest.TestCase):
    def test_bar_straddling_expiry_never_fills(self):
        position = {
            "direction": "long", "entry_ts": NOW,
            "order_expires_ts": NOW + 90.0,
            "entry_execution": {
                "book_ts": NOW_MS, "limit_price": 100.0,
                "penetration_bps": 1.0,
            },
        }
        straddling = one_minute_bar(
            NOW_MS + MINUTE_MS, low=99.0)

        fill = shadow.ShadowEvaluator._maker_fill_bar(
            position, execution_row(NOW + 120.0, bars=[straddling]))

        self.assertIsNone(fill)

    def test_fully_contained_bar_can_fill_before_expiry(self):
        position = {
            "direction": "long", "entry_ts": NOW,
            "order_expires_ts": NOW + 2 * 60.0,
            "entry_execution": {
                "book_ts": NOW_MS, "limit_price": 100.0,
                "penetration_bps": 1.0,
            },
        }
        contained = one_minute_bar(
            NOW_MS + MINUTE_MS, low=99.0)

        fill = shadow.ShadowEvaluator._maker_fill_bar(
            position, execution_row(NOW + 120.0, bars=[contained]))

        self.assertEqual(fill, contained)

    def test_bar_after_expiry_never_fills(self):
        position = {
            "direction": "long", "entry_ts": NOW,
            "order_expires_ts": NOW + 60.0,
            "entry_execution": {
                "book_ts": NOW_MS, "limit_price": 100.0,
                "penetration_bps": 1.0,
            },
        }
        late = one_minute_bar(
            NOW_MS + 2 * MINUTE_MS, low=99.0)

        fill = shadow.ShadowEvaluator._maker_fill_bar(
            position, execution_row(NOW + 180.0, bars=[late]))

        self.assertIsNone(fill)

    def test_fill_bar_stop_wins_even_when_target_also_touches(self):
        fill_ts_ms = NOW_MS + MINUTE_MS
        position = open_position(
            entry_bar_ts_ms=fill_ts_ms,
            last_execution_bar_ts_ms=fill_ts_ms - MINUTE_MS,
            maker_fill_bar_pending=True,
            deadline_ts=NOW + 4 * 3600)
        fill_bar = one_minute_bar(
            fill_ts_ms, high=102.0, low=98.0)

        resolved = shadow.ShadowEvaluator._execution_exit(
            position, execution_row(NOW + 120.0, bars=[fill_bar]),
            NOW + 120.0, valid_config())

        self.assertEqual(resolved[0], "stop")
        self.assertEqual(resolved[2]["exit_bar_timestamp_ms"], fill_ts_ms)

    def test_target_only_fill_bar_is_not_credited_and_uses_bar_clock(self):
        cfg = shadow._research_cfg(valid_config(), "scalp-maker")
        baseline = variants.baseline(
            "scalp-maker", cfg["strategy"]["version"])
        evaluator = shadow.ShadowEvaluator([baseline], cfg, budget_ms=0)
        row = market_row()
        enrichment = dict(row[brain.ENRICHMENT_KEY])
        enrichment.update({
            "ticker_ts": NOW_MS, "book_ts": NOW_MS,
            "ticker_best_bid": 99.99, "ticker_best_ask": 100.01,
            "execution_bars": [], "realized_funding_events": [],
        })
        row[brain.ENRICHMENT_KEY] = enrichment
        proposals = BY_STRATEGY["scalp-maker"].deterministic_proposals(
            {SYMBOL: row}, cfg)

        evaluator.evaluate({SYMBOL: row}, now=NOW, proposals=proposals)
        state = evaluator.store.paper_portfolio_state(
            evaluator.scope_key, baseline.variant_id)
        position = state["positions"][0]
        fill_bar_ts = NOW_MS + MINUTE_MS
        fill_bar = one_minute_bar(
            fill_bar_ts,
            high=float(position["take_price"]) * 1.01,
            low=max(float(position["stop_price"]) * 1.001,
                    float(position["entry_price"]) * 0.9998))
        advance_now = NOW + 180.0
        advanced = execution_row(advance_now, bars=[fill_bar])

        evaluator.advance({SYMBOL: advanced}, now=advance_now)
        state = evaluator.store.paper_portfolio_state(
            evaluator.scope_key, baseline.variant_id)

        self.assertEqual(len(state["positions"]), 1)
        filled = state["positions"][0]
        self.assertEqual(filled["order_status"], "filled")
        self.assertEqual(filled["fill_ts"], fill_bar_ts / 1000.0)
        self.assertEqual(
            filled["deadline_ts"],
            filled["fill_ts"] + float(filled["horizon_hours"]) * 3600.0)
        self.assertFalse(filled["maker_fill_bar_pending"])


class ExitAccountingTests(unittest.TestCase):
    def test_bar_exit_time_caps_funding_and_starts_cooldown(self):
        evaluator = shadow.ShadowEvaluator(
            [variant()], valid_config(), budget_ms=0)
        evaluator.evaluate(
            {SYMBOL: execution_row(NOW)}, now=NOW, proposals=[proposal()])
        state = evaluator.store.paper_portfolio_state(
            evaluator.scope_key, variant().variant_id)
        position = state["positions"][0]
        exit_bar_ts = NOW_MS + MINUTE_MS
        expected_exit_ts = exit_bar_ts / 1000.0
        stop_bar = one_minute_bar(
            exit_bar_ts, high=float(position["entry_price"]) * 1.001,
            low=float(position["stop_price"]) * 0.999,
            close=float(position["stop_price"]))
        funding_events = [
            {"timestamp_ms": int((expected_exit_ts - 30.0) * 1000),
             "rate_pct": 0.01, "status": "realized", "source": "test"},
            {"timestamp_ms": int((expected_exit_ts + 30.0) * 1000),
             "rate_pct": 0.02, "status": "realized", "source": "test"},
        ]
        observed_at = NOW + 300.0

        evaluator.advance(
            {SYMBOL: execution_row(
                observed_at, bars=[stop_bar], funding_events=funding_events)},
            now=observed_at)
        trade = evaluator.store.paper_trades_for(
            evaluator.scope_key, variant().variant_id)[0]
        state = evaluator.store.paper_portfolio_state(
            evaluator.scope_key, variant().variant_id)

        self.assertEqual(trade["exit_ts"], expected_exit_ts)
        self.assertEqual(trade["execution"]["exit"]["exit_ts"],
                         expected_exit_ts)
        self.assertEqual(
            [event["rate_pct"]
             for event in trade["execution"]["funding_events"]], [0.01])
        self.assertEqual(
            state["cooldowns"][SYMBOL],
            expected_exit_ts
            + valid_config()["risk"]["cooldown_minutes_after_loss"] * 60)

    def test_directional_timeout_quote_charges_spread_once(self):
        position = {
            "entry_price": 100.0, "direction": "long",
            "notional": 1_000.0, "risk_usd": 10.0,
            "cost_components": {
                "entry_fee_pct": 0.0, "exit_fee_pct": 0.0,
                "spread_pct": 0.1, "entry_slippage_pct": 0.0,
                "stop_slippage_pct": 0.0,
            },
            "funding_events": [],
        }

        pnl, _ = shadow.ShadowEvaluator._paper_pnl(
            position, 99.9, "timeout", NOW,
            spread_in_exit_price=True)

        self.assertAlmostEqual(pnl, -1.0, places=8)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 'timeout-trade' AS trade_id, 1000.0 AS notional, "
            "100.0 AS entry_price, 99.9 AS exit_price, 'long' AS direction, "
            "-1.0 AS net_pnl_usd, 'timeout' AS result, ? AS assumptions_json, "
            "? AS execution_json",
            (json.dumps({"cost_components": position["cost_components"]}),
             json.dumps({"exit": {"spread_in_exit_price": True},
                         "funding_events": []}))).fetchone()
        costs, limitations = _assignment_trade_costs(row)
        conn.close()
        self.assertAlmostEqual(costs["execution_cost_usdt"], 0.0)
        self.assertFalse(any(
            item["code"] == "COST_COMPONENT_RECONCILIATION_GAP"
            for item in limitations))

    def test_depth_vwap_rebases_notional_margin_and_risk_under_cap(self):
        evaluator = shadow.ShadowEvaluator(
            [variant()], valid_config(), budget_ms=0)
        row = execution_row(NOW)
        enrichment = row[brain.ENRICHMENT_KEY]
        enrichment.update({
            "book_mid": 100.0,
            "book_best_bid": 99.99,
            "book_best_ask": 100.01,
            "book_ask_levels": [[100.01, 1.0], [100.30, 1_000.0]],
            "book_top_ask_size": 1.0,
        })

        evaluator.evaluate(
            {SYMBOL: row}, now=NOW, proposals=[proposal()])
        trade = evaluator.store.paper_trades_for(
            evaluator.scope_key, variant().variant_id)[0]
        state = evaluator.store.paper_portfolio_state(
            evaluator.scope_key, variant().variant_id)
        entry = trade["execution"]["entry"]
        position = state["positions"][0]

        self.assertEqual(len(entry["consumed_levels"]), 2)
        self.assertAlmostEqual(trade["entry_price"], entry["fill_price"])
        self.assertAlmostEqual(trade["notional"], entry["filled_notional"])
        self.assertAlmostEqual(trade["risk_usd"], entry["risk_usd"])
        self.assertAlmostEqual(
            trade["risk_usd"],
            trade["notional"] * entry["risk_loss_pct"] / 100.0)
        self.assertLessEqual(
            trade["risk_usd"], entry["original_risk_cap_usd"] + 1e-9)
        self.assertAlmostEqual(
            position["margin_usd"], trade["notional"] / position["leverage"])
        self.assertLessEqual(
            sum(level[1] for level in entry["consumed_levels"]), 1_001.0)


if __name__ == "__main__":
    unittest.main()
