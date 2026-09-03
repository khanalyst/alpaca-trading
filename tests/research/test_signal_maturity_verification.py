"""Adversarial verification for signal-screen maturity evidence."""

import unittest

from research.strategy_factory import (
    _screen_record_can_skip,
    _seal_signal_quality_screen_record,
)


class TerminalScreenEvidenceTests(unittest.TestCase):
    def test_terminal_zero_screen_without_mature_evaluator_provenance_fails_open(self):
        """A sealed terminal record must carry maturity/evaluator evidence."""
        record = {
            "schema": "signal-quality-screen.v2",
            "scope": "fit_only",
            "authorizing": False,
            "diagnostic_only": True,
            "variant_id": "variant",
            "status": "complete_zero_actionable_signal",
            "reason": "no_actionable_signal",
            "event_count": 0,
            "fit_cells": 1,
            "event_rejection_counts": {"no_actionable_signal": 1},
        }
        # This simulates an untrusted compact hand-off that was sealed without
        # the fit eligibility payload.  Terminal suppression must remain
        # fail-open because no mature/evaluator-tested prefix is proven.
        sealed = _seal_signal_quality_screen_record(
            record,
            quality={
                "schema": "signal-quality.v2",
                "scope": "fit_only",
                "authorizing": False,
                "diagnostic_only": True,
                "variant_id": "variant",
            },
        )
        self.assertFalse(_screen_record_can_skip(sealed, variant_id="variant"))


if __name__ == "__main__":
    unittest.main()
