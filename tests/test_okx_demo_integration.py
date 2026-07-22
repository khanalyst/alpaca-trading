"""Opt-in, read-only OKX demo integration test.

Run explicitly after configuring demo-only keys:
    OKX_RUN_DEMO_INTEGRATION=1 python -m unittest \
        tests.test_okx_demo_integration -v

It never places, changes or cancels an order and refuses to run unless the
checked configuration is exactly demo mode.
"""

import os
import unittest
from pathlib import Path

import yaml
from dotenv import load_dotenv

from agent import market
from agent.config import validate_config
from agent.exchange import Exchange

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(
    os.getenv("OKX_RUN_DEMO_INTEGRATION") == "1",
    "set OKX_RUN_DEMO_INTEGRATION=1 to run against OKX demo",
)
class OKXDemoReadOnlyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = validate_config(yaml.safe_load(
            (ROOT / "config.yaml").read_text()))
        if cfg["mode"] != "demo":
            raise RuntimeError("integration test refuses non-demo mode")
        env_file = ROOT / ".env"
        if not env_file.exists():
            raise RuntimeError("demo integration test requires .env")
        load_dotenv(env_file, override=True)
        cls.cfg = cfg
        cls.exchange = Exchange(cfg)

    def test_read_only_account_and_market_preflight(self):
        account = self.exchange.verify_account_safety(
            require_trade=True, refresh=True)
        self.assertEqual(account["position_mode"], "net_mode")
        self.assertIn("trade", account["permissions"])
        self.assertGreater(self.exchange.equity_usdt(), 0)
        self.assertGreaterEqual(self.exchange.margin_usage_pct(), 0)
        universe = market.build_universe(self.exchange, self.cfg)
        self.assertTrue(universe)
        snapshot = market.market_snapshot(
            self.exchange, universe[:1], self.cfg)
        self.assertGreater(len(snapshot), 1)


if __name__ == "__main__":
    unittest.main()
