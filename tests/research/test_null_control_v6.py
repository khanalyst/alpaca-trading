"""A v6/v7 arm's randomized null must not crash (it read an undefined name)."""

from datetime import timedelta
import unittest

from research.mechanism_cohort_v2 import mechanism_cohort
from tests.research import test_null_entry_clock as clock


class NullControlV6Tests(unittest.TestCase):
    def test_a_filled_v6_null_records_no_relative_volume(self):
        bars = clock._bars(120)
        quotes = [clock._quote(clock.OPEN + timedelta(minutes=k + 1), 100.0, 100.02)
                  for k in range(119)]
        filled = 0
        for family in mechanism_cohort()["families"]:
            spec = family["arms"][0]["rule_spec"]
            with self.subTest(family=spec["family"]):
                row = clock._account(bars, quotes=quotes, spec=spec)["rows"][0]
                if not row.get("no_trade"):
                    filled += 1
                    self.assertTrue(row["diagnostic_only"])
                    self.assertIsNone(row["relative_volume"])
        self.assertGreater(filled, 0, "the scenario must fill at least one v6 null")


if __name__ == "__main__":
    unittest.main()
