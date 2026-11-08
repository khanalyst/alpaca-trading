"""Tests for the edgefinder package: data, causality, engine, statistics, trader."""

from __future__ import annotations

import datetime as dt
import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from edgefinder import backtest, catalogue, costs, data, discover, report, stats, trader
from edgefinder.broker import BrokerError, FakeBroker
from edgefinder.catalogue import Candidate
from edgefinder.data import NY, Panel

SYMBOLS = ("SPY", "QQQ", "IWM", "EFA", "TLT")


def business_days(start: dt.date, count: int) -> list[dt.date]:
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


def synthetic_panel(days: int = 700, seed: int = 7, start: dt.date = dt.date(2010, 1, 4),
                    drift_on: float = 0.0, symbols=SYMBOLS) -> Panel:
    rng = np.random.default_rng(seed)
    dates = business_days(start, days)
    s = len(symbols)
    on = rng.normal(drift_on, 0.004, (days, s))
    idr = rng.normal(0.0, 0.008, (days, s))
    close = np.empty((days, s))
    open_ = np.empty((days, s))
    price = np.full(s, 100.0)
    for t in range(days):
        open_[t] = price * (1 + on[t])
        close[t] = open_[t] * (1 + idr[t])
        price = close[t]
    high = np.maximum(open_, close) * 1.002
    low = np.minimum(open_, close) * 0.998
    volume = rng.uniform(1e5, 1e6, (days, s))
    dividend = np.zeros((days, s))
    dividend[::63, :] = 0.3
    rf = np.full(days, 0.02)
    return Panel(dates, list(symbols), open_, high, low, close, volume, dividend, rf,
                 {k: "synthetic" for k in symbols})


def perturbed(panel: Panel, k: int, *, keep_open_k: bool = False, seed: int = 99) -> Panel:
    rng = np.random.default_rng(seed)
    p = {name: getattr(panel, name).copy() for name in
         ("open", "high", "low", "close", "volume", "dividend")}
    factor = rng.uniform(0.5, 1.5, p["close"][k:].shape)
    for name in ("high", "low", "close", "volume"):
        p[name][k:] *= factor
    open_factor = rng.uniform(0.5, 1.5, p["open"][k:].shape)
    if keep_open_k:
        open_factor[0] = 1.0
    p["open"][k:] *= open_factor
    p["dividend"][k:] = rng.uniform(0, 2, p["dividend"][k:].shape)
    # Delete some future bars too, including on day k, so a validity mask that
    # looks at today's bar is caught as well as a price that does.
    gone = rng.uniform(size=p["close"][k:].shape) < 0.15
    gone[0, 0] = True
    for name in ("high", "low", "close", "volume"):
        p[name][k:][gone] = np.nan
    gone_open = gone.copy()
    if keep_open_k:
        gone_open[0] = False
    p["open"][k:][gone_open] = np.nan
    rf = panel.rf_rate.copy()
    rf[k + 1:] = rng.uniform(0, 0.1, len(rf) - k - 1)      # rf[t] is known at the close of t-1
    return Panel(list(panel.dates), list(panel.symbols), p["open"], p["high"], p["low"],
                 p["close"], p["volume"], p["dividend"], rf, dict(panel.sources))


class DataTests(unittest.TestCase):
    def test_legs_compose_to_close_to_close(self):
        panel = synthetic_panel(200)
        both = np.isfinite(panel.ret_on) & np.isfinite(panel.ret_id)
        composed = (1 + panel.ret_on) * (1 + panel.ret_id) - 1
        # equal up to the dividend's reinvestment price
        self.assertLess(np.max(np.abs(composed[both] - panel.ret_cc[both])), 1e-4)

    def test_dividend_belongs_to_the_overnight_leg(self):
        panel = synthetic_panel(100)
        t, j = 63, 0
        expected = (panel.open[t, j] + 0.3) / panel.close[t - 1, j] - 1
        self.assertAlmostEqual(panel.ret_on[t, j], expected, places=12)
        self.assertAlmostEqual(panel.ret_id[t, j], panel.close[t, j] / panel.open[t, j] - 1)

    def test_bill_rate_is_split_by_calendar_time(self):
        panel = synthetic_panel(30)
        monday = next(i for i, d in enumerate(panel.dates) if d.weekday() == 0 and i > 0)
        self.assertAlmostEqual(panel.rf_cc[monday], 0.02 * 3 / 365)
        self.assertAlmostEqual(panel.rf_id[monday] + panel.rf_on[monday], panel.rf_cc[monday])
        self.assertAlmostEqual(panel.rf_id[monday], 0.02 * 6.5 / 24 / 365)

    def test_parse_and_build_from_chart_payload(self):
        def stamp(day):
            return int(dt.datetime.combine(day, dt.time(9, 30), NY).timestamp())
        days = business_days(dt.date(2020, 1, 6), 4)
        payload = {"chart": {"result": [{
            "timestamp": [stamp(d) for d in days],
            "indicators": {"quote": [{"open": [10, 11, None, 12], "high": [11, 12, 1, 13],
                                      "low": [9, 10, 1, 11], "close": [10.5, 11.5, 1, 12.5],
                                      "volume": [1, 2, 3, 4]}],
                           "adjclose": [{"adjclose": [10.5, 11.5, 1, 12.5]}]},
            "events": {"dividends": {"x": {"amount": 0.25, "date": stamp(days[3])}}}}]}}
        parsed = data.parse_yahoo_daily(payload, "SPY")
        self.assertEqual([r[0] for r in parsed["rows"]], [d.isoformat() for d in
                                                          (days[0], days[1], days[3])])
        self.assertEqual(parsed["dividends"], {days[3].isoformat(): 0.25})
        panel = data.build_panel({"SPY": parsed}, ["SPY"])
        self.assertAlmostEqual(panel.ret_on[2, 0], (12 + 0.25) / 11.5 - 1)

    def test_incomplete_today_is_dropped_until_after_the_close(self):
        rows = [["2026-11-06", 1, 1, 1, 1, 1, 1], ["2026-11-09", 1, 1, 1, 1, 1, 1]]
        during = dt.datetime(2026, 11, 9, 15, 0, tzinfo=NY)
        after = dt.datetime(2026, 11, 9, 16, 30, tzinfo=NY)
        self.assertEqual(len(data.drop_incomplete_today(rows, during)), 1)
        self.assertEqual(len(data.drop_incomplete_today(rows, after)), 2)

    def test_non_https_and_redirects_are_refused(self):
        with self.assertRaises(data.DataError):
            data._http_json("http://example.com/x", attempts=1)


class CostTests(unittest.TestCase):
    def test_tables_match_the_research_cost_model(self):
        from research import costs as research_costs
        self.assertEqual(dict(costs.REFERENCE_SPREADS_BPS), research_costs.REFERENCE_SPREADS_BPS)
        self.assertEqual(dict(costs.REFERENCE_PRICES), research_costs.REFERENCE_PRICES)
        model = research_costs.PerSymbolCostModel()
        for symbol, price in research_costs.REFERENCE_PRICES.items():
            self.assertAlmostEqual(costs.round_trip_bps(symbol), model.round_trip_bps(symbol, price),
                                   places=9)

    def test_unknown_symbol_is_charged_conservatively(self):
        self.assertGreater(costs.round_trip_bps("ZZZZ"), costs.round_trip_bps("SPY"))


class CausalityTests(unittest.TestCase):
    """No decision on day k may change when anything at or after day k changes."""

    @classmethod
    def setUpClass(cls):
        cls.panel = synthetic_panel(560)
        cls.candidates = catalogue.daily_candidates(SYMBOLS) + catalogue.benchmarks(SYMBOLS)

    def test_every_family_is_covered(self):
        self.assertEqual({c.family for c in self.candidates}, set(catalogue.FAMILIES))

    def test_future_data_cannot_change_past_decisions(self):
        for k in (260, 401, 530):
            future = perturbed(self.panel, k)
            same_open_k = perturbed(self.panel, k, keep_open_k=True)
            for c in self.candidates:
                with self.subTest(candidate=c.id, k=k):
                    o1, c1, r1 = catalogue.decide(self.panel, c)
                    o2, c2, r2 = catalogue.decide(future, c)
                    np.testing.assert_array_equal(o1[:k + 1], o2[:k + 1])
                    np.testing.assert_array_equal(c1[:k], c2[:k])
                    np.testing.assert_array_equal(r1[:k + 1], r2[:k + 1])
                    _, c3, _ = catalogue.decide(same_open_k, c)
                    np.testing.assert_array_equal(c1[:k + 1], c3[:k + 1])

    def test_candidate_ids_are_unique_and_round_trip(self):
        ids = [c.id for c in self.candidates]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.candidates:
            self.assertEqual(Candidate.from_json(c.to_json()), c)

    def test_turn_of_month_and_holiday_calendar(self):
        days = [dt.date(2026, 11, d) for d in (23, 24, 25, 27, 30)] + [dt.date(2026, 12, 1)]
        eve = catalogue.holiday_eve(days)
        self.assertEqual(eve.tolist(), [False, False, True, False, False, False])
        start, end = catalogue.month_position(days)
        self.assertEqual(end.tolist()[:5], [5, 4, 3, 2, 1])
        self.assertEqual(start.tolist()[-1], 1)
        self.assertEqual(end.tolist()[-1], 0)          # December is incomplete

    def test_rsi_extremes(self):
        up = np.cumprod(np.full((10, 1), 1.01), axis=0)
        self.assertEqual(catalogue.rsi(up, 2)[-1, 0], 100.0)
        down = np.cumprod(np.full((10, 1), 0.99), axis=0)
        self.assertEqual(catalogue.rsi(down, 2)[-1, 0], 0.0)


class EngineTests(unittest.TestCase):
    def test_overnight_excess_is_the_leg_minus_bills_minus_cost(self):
        panel = synthetic_panel(40)
        c = Candidate("overnight", ("SPY",), (("filter", "none"),))
        r = backtest.run(panel, c)
        one_way = costs.round_trip_bps("SPY") / 2 / 1e4
        for t in (5, 17, 30):
            expected = (panel.ret_on[t, 0] - panel.rf_on[t]) - 2 * one_way
            self.assertAlmostEqual(r.excess[t], expected, places=12)
        self.assertEqual(r.opened[10], 1.0)

    def test_short_pays_no_bills_and_mirrors_the_leg(self):
        panel = synthetic_panel(40)
        r = backtest.run(panel, Candidate("intraday", ("QQQ",), (("direction", -1),)))
        one_way = costs.round_trip_bps("QQQ") / 2 / 1e4
        t = 12
        self.assertAlmostEqual(r.excess[t], -panel.ret_id[t, 1] - 2 * one_way, places=12)

    def test_held_position_trades_only_on_changes(self):
        panel = synthetic_panel(400)
        r = backtest.run(panel, Candidate("buy_and_hold", ("SPY",)))
        self.assertEqual(int((r.turnover > 0).sum()), 1)
        ready = np.where(r.ready)[0]
        t = ready[50]
        self.assertAlmostEqual(r.excess[t], panel.ret_cc[t, 0] - panel.rf_cc[t], places=4)

    def test_cost_stress_scales_costs(self):
        panel = synthetic_panel(60)
        c = Candidate("overnight", ("SPY",), (("filter", "none"),))
        a, b = backtest.run(panel, c), backtest.run(panel, c, stress=2.0)
        np.testing.assert_allclose(b.cost, 2 * a.cost)


class StatsTests(unittest.TestCase):
    def test_hac_mean_matches_the_classic_error_without_autocorrelation(self):
        rng = np.random.default_rng(1)
        x = rng.normal(0.001, 0.01, 20000)
        _, se, _ = stats.hac_mean(x)
        self.assertAlmostEqual(se / (x.std() / math.sqrt(len(x))), 1.0, delta=0.05)

    def test_hac_alpha_recovers_known_coefficients(self):
        rng = np.random.default_rng(2)
        b = rng.normal(0, 0.01, 5000)
        y = 0.0005 + 0.5 * b + rng.normal(0, 0.002, 5000)
        fit = stats.hac_alpha(y, b)
        self.assertAlmostEqual(fit["beta"], 0.5, delta=0.02)
        self.assertAlmostEqual(fit["alpha"], 0.0005, delta=0.0001)
        self.assertGreater(fit["alpha_t"], 10)

    def test_adjustments(self):
        p = [0.01, 0.04, 0.03, 0.005]
        self.assertEqual([round(v, 6) for v in stats.holm(p)], [0.03, 0.06, 0.06, 0.02])
        by = stats.benjamini_yekutieli(p)
        c = sum(1 / i for i in range(1, 5))
        self.assertAlmostEqual(by[3], min(1, 0.005 * 4 * c / 1), places=9)
        self.assertTrue(all(b >= q for b, q in zip(by, p)))

    def test_deflated_sharpe_falls_with_more_trials(self):
        rng = np.random.default_rng(3)
        x = rng.normal(0.0008, 0.01, 3000)
        few = stats.deflated_sharpe(x, trials=2, sharpe_variance=0.0004)
        many = stats.deflated_sharpe(x, trials=1000, sharpe_variance=0.0004)
        self.assertGreater(few, many)

    def test_pbo_is_high_for_noise_and_low_for_a_real_edge(self):
        rng = np.random.default_rng(4)
        noise = rng.normal(0, 0.01, (3200, 30))
        self.assertGreater(stats.pbo_cscv(noise).pbo, 0.3)
        edge = noise.copy()
        edge[:, 0] += 0.004
        self.assertLess(stats.pbo_cscv(edge).pbo, 0.05)

    def test_spa_rejects_only_with_a_real_edge(self):
        rng = np.random.default_rng(5)
        noise = rng.normal(0, 0.01, (2000, 20))
        self.assertGreater(stats.spa_test(noise, draws=300).p_consistent, 0.05)
        edge = noise.copy()
        edge[:, 3] += 0.003
        result = stats.spa_test(edge, draws=300)
        self.assertLess(result.p_consistent, 0.01)
        self.assertEqual(result.best, 3)

    def test_stationary_bootstrap_preserves_the_mean(self):
        rng = np.random.default_rng(6)
        d = rng.normal(0.5, 1.0, (500, 3))
        means = stats.stationary_bootstrap_means(d, 400, 10.0, seed=1)
        np.testing.assert_allclose(means.mean(axis=0), d.mean(axis=0), atol=0.02)


class ScanTests(unittest.TestCase):
    def test_end_to_end_scan_on_synthetic_data(self):
        panel = synthetic_panel(4300, start=dt.date(2000, 1, 3), drift_on=0.0004)
        result = discover.scan(panel, draws=60)
        self.assertEqual(result["catalogue"]["candidates"],
                         len(catalogue.daily_candidates(SYMBOLS)))
        self.assertEqual(len(result["confirmations"]), discover.CONFIRM_TOP)
        top = result["confirmations"][0]["id"]
        self.assertTrue(top.startswith("overnight:"), top)   # the planted edge is found
        rounded = discover._round(result)
        json.dumps(rounded)                                   # serialisable
        text = report.render(rounded)
        self.assertIn("## Verdict", text)
        spec = report.strategy_spec(rounded)
        self.assertIn(spec["verdict"], ("confirmed", "weak", None))


class SelectionRuleTests(unittest.TestCase):
    def _scan(self, verdicts, passed):
        confs = []
        rows = []
        for i, (v, n) in enumerate(zip(verdicts, passed), start=1):
            tests = {f"t{k}": k < n for k in range(5)}
            confs.append({"discovery_rank": i, "id": f"overnight:SPY:filter=none#{i}",
                          "verdict": v, "tests": tests, "holdout_alpha_p_holm": 0.01})
            metrics = {k: 0.1 for k in ("years", "ann_excess", "sharpe", "alpha")}
            rows.append({"id": confs[-1]["id"], "family": "overnight", "symbols": ["SPY"],
                         "params": {"filter": "none"}, "discovery": metrics, "holdout": metrics,
                         "full": metrics})
        return {"confirmations": confs, "candidates": rows}

    def test_confirmed_beats_weak(self):
        pick, verdict = report.pick_strategy(self._scan(["weak", "confirmed"], [4, 5]))
        self.assertEqual((pick["discovery_rank"], verdict), (2, "confirmed"))
        self.assertTrue(report.strategy_spec(self._scan(["weak", "confirmed"], [4, 5]))
                        ["live_allowed"])

    def test_weak_by_tests_then_rank(self):
        pick, verdict = report.pick_strategy(self._scan(["weak", "weak", "weak"], [3, 4, 4]))
        self.assertEqual((pick["discovery_rank"], verdict), (2, "weak"))
        self.assertFalse(report.strategy_spec(self._scan(["weak"], [4]))["live_allowed"])

    def test_nothing_to_trade(self):
        spec = report.strategy_spec(self._scan(["rejected"], [1]))
        self.assertIsNone(spec["candidate"])


class TraderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.panel = synthetic_panel(400, start=dt.date(2025, 1, 1))
        self.today = business_days(self.panel.dates[-1] + dt.timedelta(days=1), 1)[0]
        future = business_days(self.today, 60)
        self.broker = FakeBroker(trading_days=list(self.panel.dates[-30:]) + future,
                                 prices={s: 100.0 for s in SYMBOLS})
        self.close_time = dt.datetime.combine(self.today, dt.time(15, 40), NY)
        self.broker.now = self.close_time.astimezone(dt.timezone.utc)
        self.spec_path = root / "strategy.json"
        self.write_spec(Candidate("overnight", ("SPY",), (("filter", "none"),)), "weak")
        self.cfg = trader.Config(spec_path=self.spec_path, state_dir=root / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def write_spec(self, candidate, verdict):
        spec = {"schema": report.STRATEGY_SCHEMA, "candidate": candidate.to_json(),
                "verdict": verdict, "live_allowed": verdict == "confirmed"}
        self.spec_path.write_text(json.dumps(spec))

    def run_phase(self, phase="close", **kw):
        return trader.run_phase(self.broker, self.cfg, phase, now=self.broker.now,
                                panel_loader=lambda c, cfg: self.panel, **kw)

    def test_live_weights_equal_the_backtest_decision(self):
        full = synthetic_panel(400, start=dt.date(2025, 1, 1))
        for c in catalogue.daily_candidates(SYMBOLS)[::3]:
            for k in (300, 371):
                with self.subTest(candidate=c.id, k=k):
                    past = full.slice_dates(None, full.dates[k - 1])
                    open_live, close_live, _ = trader.todays_weights(past, c, full.dates[k],
                                                                     full.dates[k + 1:])
                    o, cl, _ = catalogue.decide(full, c)
                    cols = range(len(c.symbols))
                    self.assertEqual([open_live[s] for s in c.symbols], [o[k, i] for i in cols])
                    self.assertEqual([close_live[s] for s in c.symbols], [cl[k, i] for i in cols])

    def test_close_phase_buys_whole_shares_at_the_close_auction(self):
        result = self.run_phase(submit=True)
        self.assertEqual(result["status"], "submitted")
        order = self.broker.submitted[0]
        self.assertEqual((order["symbol"], order["side"], order["time_in_force"]),
                         ("SPY", "buy", "cls"))
        self.assertEqual(int(order["qty"]), int(100_000 * 0.95 / 100.0))

    def test_retry_never_doubles_an_order(self):
        self.run_phase(submit=True)
        again = self.run_phase(submit=True)
        self.assertEqual(again["status"], "skipped")
        self.cfg.journal.unlink()                      # even with the journal lost
        third = self.run_phase(submit=True)
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(third["results"][0]["status"], "exists")

    def test_open_phase_sells_at_the_open_auction(self):
        self.broker.held = {"SPY": 950.0}
        self.broker.now = dt.datetime.combine(self.today, dt.time(9, 10), NY).astimezone(
            dt.timezone.utc)
        result = self.run_phase("open", submit=True)
        self.assertEqual(result["status"], "submitted")
        order = self.broker.submitted[0]
        self.assertEqual((order["side"], order["qty"], order["time_in_force"]),
                         ("sell", "950", "opg"))

    def test_outside_the_window_nothing_happens(self):
        self.broker.now = dt.datetime.combine(self.today, dt.time(15, 55), NY).astimezone(
            dt.timezone.utc)
        self.assertEqual(self.run_phase(submit=True)["status"], "skipped")
        self.assertEqual(self.broker.submitted, [])

    def test_early_close_moves_the_close_window(self):
        self.broker.early_closes = {self.today: "13:00"}
        self.assertEqual(self.run_phase(submit=True)["status"], "skipped")
        self.broker.now = dt.datetime.combine(self.today, dt.time(12, 40), NY).astimezone(
            dt.timezone.utc)
        self.assertEqual(self.run_phase(submit=True)["status"], "submitted")

    def test_stale_data_is_refused(self):
        stale = self.panel.slice_dates(None, self.panel.dates[-2])
        with self.assertRaisesRegex(trader.TraderError, "stale data"):
            trader.run_phase(self.broker, self.cfg, "close", now=self.broker.now,
                             panel_loader=lambda c, cfg: stale, submit=True)

    def test_foreign_positions_are_refused(self):
        self.broker.held = {"AAPL": 10.0}
        with self.assertRaisesRegex(trader.TraderError, "dedicated account"):
            self.run_phase(submit=True)

    def test_halt_file_stops_everything(self):
        self.cfg.halt_file.parent.mkdir(parents=True)
        self.cfg.halt_file.write_text("operator")
        with self.assertRaisesRegex(trader.TraderError, "halted"):
            self.run_phase(submit=True)

    def test_drawdown_halts_and_writes_the_halt_file(self):
        trader.journal(self.cfg, "equity", equity=200_000.0)
        with self.assertRaisesRegex(trader.TraderError, "drawdown"):
            self.run_phase(submit=True)
        self.assertTrue(self.cfg.halt_file.exists())

    def test_live_needs_a_confirmed_spec(self):
        with self.assertRaisesRegex(trader.TraderError, "paper only"):
            self.run_phase(submit=True, live=True)
        self.write_spec(Candidate("overnight", ("SPY",), (("filter", "none"),)), "confirmed")
        with self.assertRaisesRegex(trader.TraderError, "mode does not match"):
            self.run_phase(submit=True, live=True)     # a paper broker cannot trade live

    def test_spec_without_candidate_is_refused(self):
        self.spec_path.write_text(json.dumps({"schema": report.STRATEGY_SCHEMA,
                                              "candidate": None, "reason": "none"}))
        with self.assertRaisesRegex(trader.TraderError, "no candidate"):
            self.run_phase()

    def test_submit_error_is_journalled_and_phase_stays_open(self):
        self.broker.fail_submit = True
        result = self.run_phase(submit=True)
        self.assertEqual(result["status"], "error")
        self.assertFalse(trader.phase_done(self.cfg, self.today, "close"))

    def test_crossing_zero_uses_two_distinct_orders(self):
        c = Candidate("overnight", ("SPY",), (("filter", "none"),))
        plan = trader.build_plan(phase="close", session=self.today, candidate=c,
                                 weights={"SPY": -0.5}, equity=10_000, current={"SPY": 10},
                                 prices={"SPY": 100.0}, cfg=self.cfg)
        self.assertEqual([(o.side, o.qty) for o in plan.orders], [("sell", 10), ("sell", 47)])
        self.assertEqual(len({o.client_order_id for o in plan.orders}), 2)
        self.assertTrue(all(len(o.client_order_id) <= 48 for o in plan.orders))

    def test_loop_runs_each_phase_once_and_survives_errors(self):
        times = iter([self.close_time + dt.timedelta(minutes=m) for m in (0, 1, 2)])
        calls = []
        original = trader.run_phase

        def counting(*args, **kwargs):
            calls.append(args[2])
            kwargs["panel_loader"] = lambda c, cfg: self.panel
            return original(*args, **kwargs)

        trader.run_phase = counting
        try:
            trader.loop(self.broker, self.cfg, submit=True, live=False, iterations=3,
                        clock=lambda: next(times).astimezone(dt.timezone.utc),
                        sleep=lambda s: None)
        finally:
            trader.run_phase = original
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertTrue(trader.phase_done(self.cfg, self.today, "close"))

    def test_status_measures_auction_shortfall_and_round_trips(self):
        self.broker.prices = {"SPY": float(self.panel.close[-1, 0])}
        self.broker.submit(symbol="SPY", qty=10, side="buy", time_in_force="cls",
                           client_order_id="efx-x-1")
        self.broker.fill_all({"SPY": self.panel.close[-1, 0] * 1.0001})
        for o in self.broker.submitted:
            o["filled_at"] = dt.datetime.combine(self.panel.dates[-1], dt.time(16, 0),
                                                 NY).isoformat()
            o["submitted_at"] = o["filled_at"]
        out = trader.status(self.broker, self.cfg, days=100000,
                            panel_loader=lambda c, cfg: self.panel)
        self.assertEqual(out["filled"], 1)
        self.assertAlmostEqual(out["auction_shortfall_bps"]["mean"], 1.0, places=6)


class TraderCommandTests(unittest.TestCase):
    def test_secrets_file_and_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s.env").write_text("# account\nALPACA_API_KEY='k1'\nALPACA_SECRET_KEY=s1\n")
            self.assertEqual(trader.read_secrets_file(root / "s.env"),
                             {"ALPACA_API_KEY": "k1", "ALPACA_SECRET_KEY": "s1"})
            cfg = trader.Config(state_dir=root / "state")
            self.assertEqual(trader.health(cfg, 60), 1)
            (root / "state").mkdir()
            (root / "state" / "heartbeat").write_text(
                dt.datetime.now(dt.timezone.utc).isoformat())
            self.assertEqual(trader.health(cfg, 60), 0)
            cfg.halt_file.write_text("x")
            self.assertEqual(trader.health(cfg, 60), 1)


class BrokerTests(unittest.TestCase):
    def test_alpaca_broker_needs_keys_and_only_alpaca_hosts(self):
        import os
        from edgefinder.broker import AlpacaBroker
        saved = {k: os.environ.pop(k, None) for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")}
        try:
            with self.assertRaises(BrokerError):
                AlpacaBroker()
            broker = AlpacaBroker(key="k", secret="s")
            with self.assertRaises(BrokerError):
                broker._request("GET", "example.com", "/x")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
