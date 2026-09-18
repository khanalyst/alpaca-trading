from datetime import datetime, timedelta, timezone
import unittest

from agent.contracts.rule import (CROSS_SECTIONAL_BENCHMARK,
                                  evaluate_rule_signal, generate_rule_signal,
                                  validate_rule_spec)


UTC = timezone.utc
START = datetime(2026, 9, 14, 13, 30, tzinfo=UTC)
BASE_SPEC = validate_rule_spec({
    "family": "momentum_continuation",
    "lookback": 3,
    "slow_lookback": 5,
    "atr_period": 3,
    "threshold_bps": 0.0,
})


def _bars(*, symbol="SPY", volume=1000.0, metadata=True):
    rows = []
    previous = 100.0
    for index in range(8):
        close = previous + 0.2
        stamp = START + timedelta(minutes=index)
        row = {
            "symbol": symbol,
            "timestamp": stamp.isoformat(),
            "open": previous,
            "high": close + 0.05,
            "low": previous - 0.05,
            "close": close,
            "volume": volume,
        }
        if metadata:
            row.update({"as_of": (stamp + timedelta(minutes=1)).isoformat(),
                        "observed_at": (stamp + timedelta(minutes=1)).isoformat()})
        rows.append(row)
        previous = close
    return rows


def _config(spec=BASE_SPEC):
    return {"strategy": {"rule_spec": spec, "execution_mode": "shares"}}


class RuleSignalInputTests(unittest.TestCase):
    def test_explicit_bar_interval_is_validated_without_runtime_now(self):
        for interval in (300, 0, -60, True, None, float("nan"), "bad"):
            with self.subTest(interval=interval):
                rows = [dict(row, interval_seconds=interval)
                        for row in _bars(metadata=False)]
                self.assertIsNone(evaluate_rule_signal(rows, BASE_SPEC))
        rows = [dict(row, interval_seconds=60) for row in _bars(metadata=False)]
        self.assertIsNotNone(evaluate_rule_signal(rows, BASE_SPEC))

    def test_malformed_historical_or_current_ohlcv_fails_closed(self):
        for field, value in (("open", None), ("high", float("nan")),
                             ("low", float("inf")), ("close", True)):
            with self.subTest(field=field):
                rows = _bars(metadata=False)
                rows[0 if field != "close" else -1][field] = value
                self.assertIsNone(evaluate_rule_signal(rows, BASE_SPEC))

        inconsistent = _bars(metadata=False)
        inconsistent[-1]["high"] = inconsistent[-1]["open"]
        self.assertIsNone(evaluate_rule_signal(inconsistent, BASE_SPEC))

    def test_non_volume_family_preserves_signal_when_volume_is_omitted(self):
        rows = _bars(metadata=False)
        for row in rows:
            row.pop("volume")
        self.assertIsNotNone(evaluate_rule_signal(rows, BASE_SPEC))

    def test_now_ignores_unconsumed_context_for_non_cross_sectional_rule(self):
        rows = _bars()
        unrelated = _bars(symbol="QQQ")
        unrelated[-1]["observed_at"] = (
            START + timedelta(minutes=9)).isoformat()
        now = START + timedelta(minutes=8)
        self.assertIsNotNone(generate_rule_signal(
            "SPY", rows, config=_config(), now=now,
            bars_by_symbol={"QQQ": unrelated}))

    def test_volume_confirmation_requires_explicit_positive_evidence(self):
        volume_spec = validate_rule_spec({**BASE_SPEC, "confirmation": "volume"})
        for supplied in (0.0, None):
            rows = _bars(metadata=False, volume=supplied)
            if supplied is None:
                for row in rows:
                    row.pop("volume")
            self.assertIsNone(evaluate_rule_signal(rows, volume_spec))

        rows = _bars(metadata=False, volume=1000.0)
        rows[-1]["volume"] = 0.0
        self.assertIsNone(evaluate_rule_signal(rows, volume_spec))

    def test_vwap_allows_zero_current_volume_with_positive_session_denominator(self):
        vwap_spec = validate_rule_spec({
            **BASE_SPEC, "family": "vwap_reversion", "threshold_bps": 5.0,
        })
        baseline = _bars(metadata=False, volume=1000.0)
        with_volume = evaluate_rule_signal(baseline, vwap_spec)
        zero_current = [*baseline]
        zero_current[-1] = {**zero_current[-1], "volume": 0.0}
        without_current_volume = evaluate_rule_signal(zero_current, vwap_spec)
        self.assertIsNotNone(with_volume)
        self.assertEqual(with_volume, without_current_volume)

        missing = [*zero_current]
        missing[-1] = {key: value for key, value in missing[-1].items()
                       if key != "volume"}
        self.assertIsNone(evaluate_rule_signal(missing, vwap_spec))
        invalid = [*zero_current]
        invalid[-1] = {**invalid[-1], "volume": float("nan")}
        self.assertIsNone(evaluate_rule_signal(invalid, vwap_spec))

    def test_cross_sectional_future_unused_benchmark_prices_do_not_contaminate_signal(self):
        cross_spec = validate_rule_spec({
            **BASE_SPEC, "family": "cross_sectional_residual",
            "threshold_bps": 5.0,
        })
        subject = _bars(symbol="QQQ", metadata=False)
        benchmark = _bars(symbol="SPY", metadata=False)
        for index, row in enumerate(benchmark):
            close = 100.0 + (index + 1) * 0.05
            row.update(close=close, high=max(row["open"], close) + 0.05,
                       low=min(row["open"], close) - 0.05)
        context = {CROSS_SECTIONAL_BENCHMARK: benchmark}
        baseline = evaluate_rule_signal(
            subject, cross_spec, bars_by_symbol=context, symbol="QQQ")
        self.assertIsNotNone(baseline)
        future = [*benchmark, {
            **benchmark[-1], "timestamp": (START + timedelta(minutes=8)).isoformat(),
            "close": float("nan"), "high": float("nan"),
        }]
        with_future = evaluate_rule_signal(
            subject, cross_spec,
            bars_by_symbol={CROSS_SECTIONAL_BENCHMARK: future}, symbol="QQQ")
        self.assertEqual(baseline, with_future)

        malformed_selected = [*benchmark]
        malformed_selected[4] = {**malformed_selected[4], "close": None}
        self.assertIsNone(evaluate_rule_signal(
            subject, cross_spec,
            bars_by_symbol={CROSS_SECTIONAL_BENCHMARK: malformed_selected},
            symbol="QQQ"))

    def test_now_requires_completed_one_minute_bars_and_available_metadata(self):
        rows = _bars()
        latest_start = START + timedelta(minutes=7)
        for offset in (59, 60):
            now = latest_start + timedelta(seconds=offset)
            signal = generate_rule_signal("SPY", rows, config=_config(), now=now)
            self.assertEqual(signal is not None, offset == 60)

        future = _bars()
        availability_now = latest_start + timedelta(minutes=1)
        future[0]["observed_at"] = (availability_now + timedelta(seconds=1)).isoformat()
        self.assertIsNone(generate_rule_signal(
            "SPY", future, config=_config(), now=availability_now))

        naive = _bars()
        naive[0]["as_of"] = "2026-09-14T13:31:00"
        self.assertIsNone(generate_rule_signal(
            "SPY", naive, config=_config(), now=latest_start + timedelta(minutes=1)))

    def test_now_rejects_wrong_or_mixed_symbols(self):
        now = START + timedelta(minutes=8)
        wrong = _bars(symbol="QQQ")
        self.assertIsNone(generate_rule_signal("SPY", wrong, config=_config(), now=now))
        mixed = _bars()
        mixed[-1]["symbol"] = "QQQ"
        self.assertIsNone(generate_rule_signal("SPY", mixed, config=_config(), now=now))


if __name__ == "__main__":
    unittest.main()
