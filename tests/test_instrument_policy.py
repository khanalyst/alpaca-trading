"""Focused parity and safety checks for the fail-closed instrument policy."""

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from itertools import product
import unittest

from agent import instruments
from agent.instruments import (reject_crypto, validate_asset_class,
                               validate_equity_symbol, validate_instrument,
                               validate_option_symbol)


def _reference_reject_crypto(value, field="instrument"):
    """Snapshot of the pre-optimization rejection behavior."""
    if isinstance(value, Mapping):
        for name in ("asset_class", "class", "kind", "asset_type", "symbol"):
            if name in value:
                _reference_reject_crypto(value[name], f"{field}.{name}")
        return
    raw_value = getattr(value, "value", value)
    raw = str(raw_value or "").strip()
    lowered = raw.lower().replace("-", "_").replace(" ", "_")
    if (lowered in instruments._CRYPTO_ASSET_CLASSES or
            "crypto" in lowered):
        raise ValueError(f"{field} must not be crypto")
    upper = raw.upper().replace(" ", "")
    if "/" in upper:
        raise ValueError(f"{field} must not be a slash pair")
    compact = upper.replace("-", "").replace("_", "")
    if any(compact == base + quote for base in instruments._CRYPTO_BASES
           for quote in instruments._CRYPTO_QUOTES):
        raise ValueError(f"{field} must not be a crypto pair")


def _outcome(function, value, field="instrument"):
    try:
        function(value, field)
    except Exception as exc:  # Exact exception type and text are the contract.
        return type(exc), str(exc)
    return None


class InstrumentPolicyTests(unittest.TestCase):
    def test_precomputed_pairs_are_immutable_and_complete(self):
        expected = {
            base + quote
            for base, quote in product(instruments._CRYPTO_BASES,
                                       instruments._CRYPTO_QUOTES)
        }
        self.assertIsInstance(instruments._CRYPTO_PAIRS, frozenset)
        self.assertEqual(instruments._CRYPTO_PAIRS, expected)

    def test_every_crypto_pair_matches_reference_across_case_and_separators(self):
        for base, quote in product(instruments._CRYPTO_BASES,
                                   instruments._CRYPTO_QUOTES):
            pair = base + quote
            variants = {
                "compact": pair,
                "lower": pair.lower(),
                "mixed-hyphen": f"{base.lower()}-{quote.title()}",
                "mixed-underscore": f"{base.title()}_{quote.lower()}",
                "spaced": f"  {base.lower()} {quote.title()}  ",
                "slash": f"{base.lower()}/{quote.title()}",
            }
            for variant_name, value in variants.items():
                with self.subTest(base=base, quote=quote,
                                  variant=variant_name):
                    reference = _outcome(_reference_reject_crypto, value)
                    actual = _outcome(reject_crypto, value)
                    self.assertEqual(actual, reference)
                    self.assertEqual(actual[0], ValueError)
                    expected_reason = ("slash pair" if variant_name == "slash"
                                       else "crypto pair")
                    self.assertIn(expected_reason, actual[1])

    def test_reference_parity_for_classes_mappings_and_near_misses(self):
        class AssetClass(Enum):
            CRYPTO = "crypto"
            EQUITY = "us_equity"

        cases = (
            None,
            "",
            0,
            "SPY",
            "ETH",
            "BTC",
            "BTCUSDX",
            "XBTCUSD",
            "SPY260821C00600000",
            "crypto",
            "Cryptocurrency",
            "spot crypto",
            "not-a-cryptocurrency",
            AssetClass.CRYPTO,
            AssetClass.EQUITY,
            {"asset_class": "us_equity", "symbol": "SPY"},
            {"asset_class": "crypto", "symbol": "SPY"},
            {"class": "us_equity", "symbol": "btc-usd"},
            {"kind": {"asset_type": "digital asset"}},
            {"ignored": "BTCUSD"},
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(_outcome(reject_crypto, value, "position"),
                                 _outcome(_reference_reject_crypto, value,
                                          "position"))

    def test_mapping_paths_slash_precedence_and_mutable_values_are_preserved(self):
        with self.assertRaisesRegex(
                ValueError, r"^position\.symbol must not be a crypto pair$"):
            reject_crypto({"asset_class": "us_equity", "symbol": "Eth-Usd"},
                          "position")
        with self.assertRaisesRegex(
                ValueError, r"^order\.kind\.symbol must not be a slash pair$"):
            reject_crypto({"kind": {"symbol": "BTC/USD"}}, "order")
        with self.assertRaisesRegex(ValueError, r"must not be crypto$"):
            reject_crypto("crypto/USD")
        with self.assertRaisesRegex(ValueError, r"must not be a slash pair$"):
            reject_crypto("SPY/USD")

        class MutableValue:
            value = "SPY"

        value = MutableValue()
        reject_crypto(value)
        value.value = "BTCUSD"
        with self.assertRaisesRegex(ValueError, r"must not be a crypto pair$"):
            reject_crypto(value)

    def test_supported_equities_asset_classes_and_options_remain_accepted(self):
        equities = {
            " spy ": "SPY",
            "brk.b": "BRK.B",
            "bf-b": "BF-B",
            "eth": "ETH",
            "btc": "BTC",
            "btcusdx": "BTCUSDX",
        }
        for value, expected in equities.items():
            with self.subTest(equity=value):
                self.assertEqual(validate_equity_symbol(value), expected)
                self.assertEqual(validate_instrument(value), expected)

        self.assertEqual(validate_asset_class(" US_EQUITY "), "us_equity")
        self.assertEqual(validate_asset_class("Us_Option"), "us_option")

        call = "SPY260821C00600000"
        put = "BRK.B260821P00450000"
        self.assertEqual(validate_option_symbol(
            f"  {call[:3]} {call[3:]}  ", underlying="spy",
            expiration=datetime(2026, 8, 21, 15, 30),
            strike=Decimal("600.000")), call)
        self.assertEqual(validate_option_symbol(
            put, underlying="BRK.B", expiration=date(2026, 8, 21),
            strike="450"), put)
        self.assertEqual(validate_instrument(call), call)
        self.assertEqual(validate_instrument(call.lower(), " us_option "), call)
        self.assertEqual(validate_instrument("spy", "US_EQUITY"), "SPY")

    def test_invalid_and_crypto_inputs_remain_fail_closed(self):
        cases = (
            (validate_equity_symbol, ("BTC/USD",), "slash pair"),
            (validate_equity_symbol, ("btc_usd",), "crypto pair"),
            (validate_equity_symbol, ("",), "invalid US equity symbol"),
            (validate_equity_symbol, (None,), "invalid US equity symbol"),
            (validate_equity_symbol, ("1SPY",), "invalid US equity symbol"),
            (validate_equity_symbol, ("SPY260821C00600000",),
             "must not be an OCC option symbol"),
            (validate_option_symbol, ("SPY",), "invalid OCC option symbol"),
            (validate_option_symbol, ("SPY269931C00600000",),
             "invalid expiration"),
            (validate_option_symbol, ("SPY260821C00000000",),
             "strike must be positive"),
            (validate_option_symbol,
             ("BTCUSD260821C00600000",), "crypto pair"),
            (validate_asset_class, ("crypto",), "must not be crypto"),
            (validate_asset_class, ("equity",), "unsupported asset class"),
            (validate_instrument, ("ETH_USD",), "crypto pair"),
        )
        for function, args, message in cases:
            with self.subTest(function=function.__name__, value=args[0]):
                with self.assertRaisesRegex(ValueError, message):
                    function(*args)

        with self.assertRaisesRegex(ValueError,
                                    "does not match its underlying"):
            validate_option_symbol("SPY260821C00600000", underlying="QQQ")
        with self.assertRaisesRegex(ValueError,
                                    "does not match its expiration"):
            validate_option_symbol("SPY260821C00600000",
                                   expiration="2026-08-22")
        with self.assertRaisesRegex(ValueError,
                                    "does not match its strike"):
            validate_option_symbol("SPY260821C00600000", strike="601")


if __name__ == "__main__":
    unittest.main()
