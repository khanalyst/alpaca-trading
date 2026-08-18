"""Option exits must be priced honestly or rejected visibly, never fabricated."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest

from agent.contracts.rule import validate_rule_spec
from research.costs import ReplayPolicy
from research.factory_core import (
    FRESH_OPTION_QUOTE_SECONDS, MAX_OPTION_QUOTE_STALENESS_SECONDS, _option_at,
    _simulate_trade, diagnose, simulate_account,
)
from research.gates import AcceptanceFloor, performance_floor
from research.market_data import (
    EventIdentity, OptionContract, OptionSnapshot, UnderlyingBar,
)

SESSION = date(2024, 1, 2)
OPEN = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
CONTRACT = OptionContract(
    symbol="SPY240119C00500000", underlying="SPY", expiration=date(2024, 1, 19),
    strike=500.0, right="call", multiplier=100, currency="USD",
    provider="alpaca", feed="opra")
SPEC = {"family": "momentum_continuation", "lookback": 3, "threshold_bps": 5.0,
        "confirmation": "none", "max_hold_bars": 30, "side": "long"}


def _identity(ts: datetime) -> EventIdentity:
    return EventIdentity(provider="alpaca", feed="opra", as_of=ts, observed_at=ts,
                         session_date=SESSION, timezone="America/New_York")


def _bar(minute: int, close: float) -> UnderlyingBar:
    ts = OPEN + timedelta(minutes=minute)
    return UnderlyingBar(symbol="SPY", timestamp=ts, open=close, high=close + .2,
                         low=close - .2, close=close, volume=1_000,
                         identity=_identity(ts), interval_seconds=60)


def _snap(minute: int, *, bid: float = 2.0, ask: float | None = None,
          contract: OptionContract = CONTRACT,
          seconds: int = 0, bid_size: float | None = 5,
          ask_size: float | None = 5, volume: float | None = 20,
          open_interest: float | None = 100) -> OptionSnapshot:
    ts = OPEN + timedelta(minutes=minute, seconds=seconds)
    return OptionSnapshot(contract=contract, timestamp=ts, bid=bid,
                          ask=bid + .1 if ask is None else ask,
                          last=bid, underlying_price=500.0, identity=_identity(ts),
                          bid_size=bid_size, ask_size=ask_size, volume=volume,
                          open_interest=open_interest)


def _rising_session(minutes: int = 40) -> list[UnderlyingBar]:
    # A clean monotone ramp so the momentum rule signals on the first eligible
    # bar and holds to the deadline.
    return [_bar(index, 500.0 + index * .5) for index in range(minutes)]


class OptionExitToleranceTests(unittest.TestCase):
    def test_indicative_option_quote_is_not_executable_research_evidence(self):
        bars = _rising_session()
        indicative = OptionContract(
            symbol=CONTRACT.symbol, underlying="SPY", expiration=CONTRACT.expiration,
            strike=CONTRACT.strike, right="call", multiplier=100,
            currency="USD", provider="alpaca", feed="indicative")
        self.assertIsNone(_option_at(
            [_snap(6, contract=indicative)], symbol="SPY", day=SESSION,
            direction="long", cutoff=bars[6].timestamp,
            contract_symbol=indicative.symbol))

    def test_missing_or_empty_liquidity_is_not_an_eligible_option_quote(self):
        bars = _rising_session()
        for kwargs in (
                {"bid_size": None, "ask_size": None, "volume": None,
                 "open_interest": None},
                {"bid_size": 0, "ask_size": 0, "volume": 0,
                 "open_interest": 0}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(_option_at(
                    [_snap(6, **kwargs)], symbol="SPY", day=SESSION,
                    direction="long", cutoff=bars[6].timestamp,
                    contract_symbol=CONTRACT.symbol))

    def test_runtime_liquidity_rule_requires_volume_oi_or_both_sizes(self):
        bars = _rising_session()
        for kwargs in (
                {"bid_size": None, "ask_size": None, "volume": 1,
                 "open_interest": None},
                {"bid_size": 1, "ask_size": 1, "volume": None,
                 "open_interest": None}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNotNone(_option_at(
                    [_snap(6, **kwargs)], symbol="SPY", day=SESSION,
                    direction="long", cutoff=bars[6].timestamp,
                    contract_symbol=CONTRACT.symbol))

        # A one-sided displayed size is not executable depth.  Runtime risk
        # treats it as unavailable unless volume or open interest supplies an
        # independent liquidity measurement.
        self.assertIsNone(_option_at(
            [_snap(6, bid_size=1, ask_size=None, volume=None,
                   open_interest=None)], symbol="SPY", day=SESSION,
            direction="long", cutoff=bars[6].timestamp,
            contract_symbol=CONTRACT.symbol))

    def test_pinned_lookup_requires_a_quote_within_the_runtime_age_limit(self):
        bars = _rising_session()
        snaps = [_snap(6, seconds=30, bid=6.0)]
        found = _option_at(snaps, symbol="SPY", day=SESSION, direction="long",
                           cutoff=bars[6].end, contract_symbol=CONTRACT.symbol)
        self.assertIsNotNone(found)
        self.assertEqual(found.bid, 6.0)

    def test_pinned_lookup_refuses_a_quote_beyond_the_staleness_bound(self):
        bars = _rising_session()
        stale = _snap(1)
        cutoff = stale.timestamp + timedelta(
            seconds=MAX_OPTION_QUOTE_STALENESS_SECONDS + 1)
        self.assertIsNone(_option_at([stale], symbol="SPY", day=SESSION,
                                     direction="long", cutoff=cutoff,
                                     contract_symbol=CONTRACT.symbol))
        self.assertIsNotNone(
            _option_at([stale], symbol="SPY", day=SESSION, direction="long",
                       cutoff=stale.timestamp + timedelta(
                           seconds=MAX_OPTION_QUOTE_STALENESS_SECONDS),
                       contract_symbol=CONTRACT.symbol))

    def test_factory_option_bounds_use_runtime_spread_formula_and_avoid_zero_dte(self):
        bars = _rising_session()
        policy = ReplayPolicy(options_min_dte=0, options_max_dte=60,
                              options_max_spread_pct=5.0)
        # (ask-bid)/mid * 100 = 4.878%, just inside the runtime 5% cap.
        self.assertIsNotNone(_option_at(
            [_snap(6, bid=2.0, ask=2.1)], symbol="SPY", day=SESSION,
            direction="long", cutoff=bars[6].timestamp,
            contract_symbol=CONTRACT.symbol, policy=policy))
        # A 5.485% midpoint spread is rejected at the same boundary.
        self.assertIsNone(_option_at(
            [_snap(6, bid=1.95, ask=2.06)], symbol="SPY", day=SESSION,
            direction="long", cutoff=bars[6].timestamp,
            contract_symbol=CONTRACT.symbol, policy=policy))
        zero_dte = OptionContract(
            symbol="SPY240102C00500000", underlying="SPY", expiration=SESSION,
            strike=500.0, right="call", multiplier=100, currency="USD",
            provider="alpaca", feed="opra")
        self.assertIsNone(_option_at(
            [_snap(6, contract=zero_dte)], symbol="SPY", day=SESSION,
            direction="long", cutoff=bars[6].timestamp,
            contract_symbol=zero_dte.symbol, policy=policy))

    def test_contract_that_stops_being_quoted_yields_a_reasoned_no_trade(self):
        bars = _rising_session()
        # The recorder quotes the contract at the entry and then loses it.
        snaps = [_snap(15)]
        raw = _simulate_trade(bars, validate_rule_spec(SPEC), snaps, "option")
        self.assertIsNotNone(raw)
        self.assertEqual(raw.get("unpriced_reason"),
                         "entry contract stopped being quoted before exit")
        book = simulate_account(bars, snaps, SPEC, vehicle="option",
                                account_id="acct")
        (row,) = book["rows"]
        self.assertTrue(row["no_trade"])
        self.assertIn("stopped being quoted", row["reject_reason"])
        self.assertEqual(book["trades"], 0)
        # The loss is visible but is not a flat trade: it must not dilute
        # expectancy or win rate, and must not satisfy a trade floor.
        self.assertEqual(diagnose(book["rows"])["trades"], 0)
        self.assertEqual(diagnose(book["rows"])["expectancy"], 0.0)
        self.assertEqual(diagnose(book["rows"])["sessions"], 1)
        floor = AcceptanceFloor(min_trades=1, min_sessions=1).check(
            book["rows"], vehicle="option")
        self.assertFalse(floor["structural_passes"])
        self.assertEqual(performance_floor(book["rows"], vehicle="option")["trades"], 0)

    def test_a_signal_that_never_had_a_quote_is_also_reasoned(self):
        bars = _rising_session()
        book = simulate_account(bars, [], SPEC, vehicle="option", account_id="acct")
        (row,) = book["rows"]
        self.assertEqual(row["reject_reason"],
                         "no option quote within staleness bound at entry")
        self.assertEqual(row["net_pnl"], 0.0)

    def test_stale_exit_is_rejected_explicitly(self):
        bars = _rising_session()
        quoted = [_snap(minute) for minute in range(1, 40)]
        gapped = [snap for snap in quoted
                  if not OPEN + timedelta(minutes=20) <= snap.timestamp
                  <= OPEN + timedelta(minutes=22)]
        fresh_book = simulate_account(bars, quoted, SPEC, vehicle="option",
                                      account_id="fresh")
        stale_book = simulate_account(bars, gapped, SPEC, vehicle="option",
                                      account_id="stale")
        (fresh_row,) = fresh_book["rows"]
        (stale_row,) = stale_book["rows"]
        self.assertFalse(fresh_row["no_trade"])
        self.assertTrue(stale_row["no_trade"])
        self.assertLessEqual(fresh_row["exit_quote_age_seconds"],
                             FRESH_OPTION_QUOTE_SECONDS)
        self.assertIn("stopped being quoted", stale_row["reject_reason"])

    def test_realistic_gapped_corpus_rejects_stale_quotes_explicitly(self):
        # Ten sessions of one-minute bars; on five of them the recorder drops
        # the pinned contract for three minutes around the exit, on three it
        # loses the contract entirely after the entry.
        bars: list[UnderlyingBar] = []
        snaps: list[OptionSnapshot] = []
        for day_index in range(10):
            offset = day_index * 1_440
            for minute in range(40):
                ts = OPEN + timedelta(minutes=offset + minute)
                bars.append(UnderlyingBar(
                    symbol="SPY", timestamp=ts, open=500.0 + minute * .5,
                    high=500.2 + minute * .5, low=499.8 + minute * .5,
                    close=500.0 + minute * .5, volume=1_000,
                    identity=EventIdentity(
                        provider="alpaca", feed="opra", as_of=ts, observed_at=ts,
                        session_date=ts.date(), timezone="America/New_York"),
                    interval_seconds=60))
            for minute in range(1, 40):
                if day_index < 3 and minute > 12:
                    continue                      # contract lost for the session
                if 3 <= day_index < 8 and 20 <= minute <= 22:
                    continue                      # three skipped recorder cycles
                ts = OPEN + timedelta(minutes=offset + minute)
                snaps.append(OptionSnapshot(
                    contract=CONTRACT, timestamp=ts, bid=2.0, ask=2.1, last=2.0,
                    underlying_price=500.0, bid_size=5, ask_size=5, volume=20,
                    open_interest=100,
                    identity=EventIdentity(
                        provider="alpaca", feed="opra", as_of=ts, observed_at=ts,
                        session_date=ts.date(), timezone="America/New_York")))
        book = simulate_account(bars, snaps, SPEC, vehicle="option",
                                account_id="corpus")
        rows = book["rows"]
        self.assertEqual(len(rows), 10)
        priced = [row for row in rows if row.get("no_trade") is False]
        rejected = [row for row in rows if row.get("reject_reason")]
        self.assertEqual(len(priced), 2)
        self.assertEqual(len(rejected), 8)
        self.assertTrue(all(row.get("reject_reason") for row in rejected))
        # Quotes beyond the runtime freshness boundary are not reused.
        # Every session stays in the sample; none silently vanished.
        self.assertEqual(diagnose(rows)["sessions"], 10)
        self.assertEqual(diagnose(rows)["trades"], 2)


if __name__ == "__main__":
    unittest.main()
