import json
from pathlib import Path
import unittest

from agent.config import DEFAULT_CONFIG, validate_config


class DefaultResearchCapacityTests(unittest.TestCase):
    def test_shipped_and_code_defaults_use_the_24_symbol_universe(self):
        expected = [
            "SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV",
            "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "VTI", "VO",
            "VB", "EFA", "EEM", "TLT", "HYG", "GLD", "SLV", "SMH",
        ]
        self.assertEqual(DEFAULT_CONFIG["universe"]["symbols"], expected)
        self.assertEqual(validate_config({})["universe"]["symbols"], expected)

        root = Path(__file__).resolve().parents[1]
        shipped = json.loads((root / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(shipped["universe"]["symbols"], expected)


if __name__ == "__main__":
    unittest.main()
