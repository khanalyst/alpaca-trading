"""Reject invalid broker money fields before they enter runtime telemetry."""

from decimal import Decimal
import unittest

from agent.alpaca_provider import AlpacaError, AlpacaProvider, AlpacaSession


class AccountNumericBoundaryTests(unittest.TestCase):
    @staticmethod
    def _provider(**changes):
        payload = {"id": "paper-fixture", "status": "ACTIVE", "equity": "100",
                   "cash": "100", "buying_power": "100", "currency": "USD",
                   **changes}

        class Trading:
            def get_account(self):
                return payload

        return AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=Trading()))

    def test_all_account_money_fields_require_finite_values(self):
        for field in ("equity", "cash", "buying_power"):
            for invalid in ("NaN", "Infinity", "-Infinity", "invalid", None, True):
                with self.subTest(field=field, invalid=invalid):
                    provider = self._provider(**{field: invalid})
                    with self.assertRaisesRegex(
                            AlpacaError, f"account {field} must be (numeric|finite)"):
                        provider.account()

    def test_finite_zero_and_negative_cash_are_preserved(self):
        account = self._provider(cash="-50.25", buying_power="0").account()
        self.assertEqual(account.equity, Decimal("100"))
        self.assertEqual(account.cash, Decimal("-50.25"))
        self.assertEqual(account.buying_power, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
