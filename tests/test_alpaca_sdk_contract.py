"""Exercise the installed SDK parser at the provider boundary without HTTP."""

import unittest
from unittest.mock import patch

from agent.alpaca_provider import AlpacaError, AlpacaProvider, AlpacaSession


class AlpacaSdkAssetContractTests(unittest.TestCase):
    @staticmethod
    def _row(exchange="TXSE"):
        return {
            "id": "00000000-0000-0000-0000-000000000001",
            "class": "us_equity", "exchange": exchange, "symbol": "SPY",
            "name": "SDK contract fixture", "status": "active",
            "tradable": True, "marginable": True, "shortable": True,
            "easy_to_borrow": True, "fractionable": True,
        }

    @staticmethod
    def _provider():
        from alpaca.trading.client import TradingClient

        client = TradingClient("contract-test-key", "contract-test-secret", paper=True)
        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=client))
        return provider, client

    def test_current_txse_asset_passes_real_sdk_and_domain_validation(self):
        provider, client = self._provider()
        with patch.object(client, "get", return_value=[self._row()]) as get:
            rows = provider.assets()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].symbol, rows[0].exchange), ("SPY", "txse"))
        self.assertTrue(rows[0].tradable)
        self.assertEqual(get.call_args.args[0], "/assets")

    def test_unrecognized_exchange_still_fails_closed(self):
        provider, client = self._provider()
        with patch.object(client, "get", return_value=[self._row("UNKNOWN_EXCHANGE")]):
            with self.assertRaisesRegex(AlpacaError, "asset request failed"):
                provider.assets()


if __name__ == "__main__":
    unittest.main()
