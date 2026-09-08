"""Compose contract for explicit research calibration opt-in."""

from pathlib import Path
import unittest


class ResearchCalibrationComposeTests(unittest.TestCase):
    def test_research_calibration_is_forwarded_default_closed(self):
        lines = Path("compose.yaml").read_text(encoding="utf-8").splitlines()
        research_start = lines.index("  research:")
        research_end = next(
            index for index in range(research_start + 1, len(lines))
            if lines[index].startswith("  ") and
            not lines[index].startswith("    ") and
            lines[index].strip().endswith(":"))
        environment_start = lines.index(
            "    environment:", research_start, research_end)
        environment = {}
        for line in lines[environment_start + 1:research_end]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indentation = len(line) - len(line.lstrip())
            if indentation <= 4:
                break
            if indentation == 6:
                key, separator, value = line.strip().partition(":")
                if separator:
                    environment[key] = value.strip()

        self.assertIsInstance(environment, dict)
        self.assertEqual(
            environment["ALPACA_RESEARCH_CALIBRATION_ENABLED"],
            "${ALPACA_RESEARCH_CALIBRATION_ENABLED:-0}")


if __name__ == "__main__":
    unittest.main()
