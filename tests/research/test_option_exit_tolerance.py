"""Option exits must be priced honestly or rejected visibly, never fabricated."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest

from agent.contracts.rule import validate_rule_spec
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


def _snap(minute: int, *, bid: float = 2.0, contract: OptionContract = CONTRACT,
          seconds: int = 0) -> OptionSnapshot:
    ts = OPEN + timedelta(minutes=minute, seconds=seconds)
    return OptionSnapshot(contract=contract, timestamp=ts, bid=bid, ask=bid + .1,
                          last=bid, underlying_price=500.0, identity=_identity(ts))


def _rising_session(minutes: int = 40) -> list[UnderlyingBar]:
    # A clean monotone ramp so the momentum rule signals on the first eligible
    # bar and holds to the deadline.
    return [_bar(index, 500.0 + index * .5) for index in range(minutes)]


class OptionExitToleranceTests(unittest.TestCase):
    def test_pinned_lookup_tolerates_a_skipped_recorder_cycle(self):
        bars = _rising_session()
        snaps = [_snap(1), _snap(3, bid=6.0)]
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

    def test_contract_that_stops_being_quoted_yields_a_reasoned_no_trade(self):
        bars = _rising_session()
        # The recorder quotes the contract around the entry and then loses it.
        # Quoted up to 14:42 and never again: the entry at 14:45 is still
        # inside the bound, the 14:48 exit is not.
        snaps = [_snap(minute) for minute in range(1, 13)]
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

    def test_stale_exit_is_priced_recorded_and_charged_a_spread(self):
        bars = _rising_session()
        quoted = [_snap(minute) for minute in range(1, 40)]
        gapped = [snap for snap in quoted
                  if not OPEN + timedelta(minutes=16) <= snap.timestamp
                  <= OPEN + timedelta(minutes=18)]
        fresh_book = simulate_account(bars, quoted, SPEC, vehicle="option",
                                      account_id="fresh")
        stale_book = simulate_account(bars, gapped, SPEC, vehicle="option",
                                      account_id="stale")
        (fresh_row,) = fresh_book["rows"]
        (stale_row,) = stale_book["rows"]
        self.assertFalse(fresh_row["no_trade"])
        self.assertFalse(stale_row["no_trade"])
        self.assertLessEqual(fresh_row["exit_quote_age_seconds"],
                             FRESH_OPTION_QUOTE_SECONDS)
        self.assertGreater(stale_row["exit_quote_age_seconds"],
                           FRESH_OPTION_QUOTE_SECONDS)
        # Same bid, same quantity: the only difference is that the stale exit is
        # not treated as an executable quote, so it is filled worse.
        self.assertEqual(stale_row["quantity"], fresh_row["quantity"])
        self.assertLess(stale_row["exit_price"], fresh_row["exit_price"])

    def test_realistic_gapped_corpus_recovers_rather_than_discards(self):
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
                if 3 <= day_index < 8 and 16 <= minute <= 18:
                    continue                      # three skipped recorder cycles
                ts = OPEN + timedelta(minutes=offset + minute)
                snaps.append(OptionSnapshot(
                    contract=CONTRACT, timestamp=ts, bid=2.0, ask=2.1, last=2.0,
                    underlying_price=500.0,
                    identity=EventIdentity(
                        provider="alpaca", feed="opra", as_of=ts, observed_at=ts,
                        session_date=ts.date(), timezone="America/New_York")))
        book = simulate_account(bars, snaps, SPEC, vehicle="option",
                                account_id="corpus")
        rows = book["rows"]
        self.assertEqual(len(rows), 10)
        priced = [row for row in rows if row.get("no_trade") is False]
        rejected = [row for row in rows if row.get("reject_reason")]
        self.assertEqual(len(priced), 7)
        self.assertEqual(len(rejected), 3)
        self.assertTrue(all("stopped being quoted" in row["reject_reason"]
                            for row in rejected))
        # Five of the seven are recovered across a real recorder gap and carry
        # the staleness that recovery cost them.
        recovered = [row for row in priced
                     if row["exit_quote_age_seconds"] > FRESH_OPTION_QUOTE_SECONDS]
        self.assertEqual(len(recovered), 5)
        self.assertTrue(all(row["exit_quote_age_seconds"] <=
                            MAX_OPTION_QUOTE_STALENESS_SECONDS for row in recovered))
        # Every session stays in the sample; none silently vanished.
        self.assertEqual(diagnose(rows)["sessions"], 10)
        self.assertEqual(diagnose(rows)["trades"], 7)


if __name__ == "__main__":
    unittest.main()
