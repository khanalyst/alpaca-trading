import unittest

from agent.engine import Engine


def open_decision(symbol, confidence):
    return {
        "action": "open", "symbol": symbol, "direction": "long",
        "confidence": confidence, "size_pct_equity": 0, "leverage": 2,
        "stop_loss_pct": 1.0, "take_profit_pct": 2.0, "reasoning": "",
    }


class SortedOpensTests(unittest.TestCase):
    def test_opens_are_ordered_by_descending_confidence(self):
        opens, conflicted = Engine._sorted_opens([
            open_decision("ETH/USDT:USDT", 0.7),
            open_decision("BTC/USDT:USDT", 0.9),
        ])
        self.assertEqual([d["symbol"] for d in opens],
                         ["BTC/USDT:USDT", "ETH/USDT:USDT"])
        self.assertEqual(conflicted, [])

    def test_open_and_close_on_one_symbol_drops_the_open(self):
        close = {"action": "close", "symbol": "ETH/USDT:USDT",
                 "reasoning": "thesis broken"}
        keep = open_decision("BTC/USDT:USDT", 0.7)
        conflict = open_decision("ETH/USDT:USDT", 0.95)
        opens, conflicted = Engine._sorted_opens([close, conflict, keep])
        # The higher-confidence open loses: SYSTEM forbids closing a symbol
        # and re-entering (or reversing) it in the same reply, and the
        # engine enforces that instead of trusting the prompt.
        self.assertEqual(opens, [keep])
        self.assertEqual(conflicted, [conflict])


if __name__ == "__main__":
    unittest.main()
