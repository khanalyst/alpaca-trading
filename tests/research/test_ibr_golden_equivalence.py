"""Golden equivalence for the registered IBR arms after the review fixes.

``fixtures/ibr_golden_signals.json`` was computed by
``tests/research/ibr_golden_support.py`` on the revision before the
2026-10-15 review fixes (commit 352fb70), with the repository
``config.yaml``.  It pins, for the seven registered equity IBR arms and for
the runtime contract with the shipped config, every runtime signal, every
replayed trade and refusal, the effective mapped configuration and the arm
identities.  The fixes must leave all of it unchanged, with two declared
exceptions:

* A-16 adds ``signal_bar_open_ts`` and ``signal_bar_close_ts`` to each IBR
  signal; every original key keeps its value;
* A-07 corrects one label: a ``force_flat`` exit whose hold was cut short by
  missing data now has the canonical cause ``data_discontinuity`` instead of
  ``session_force_flat``.  The trade itself (prices, times, P&L, fill
  provenance) is unchanged.

The opt-in fields (A-07 ``hold_across_gaps`` and A-10 same-clock relative
volume) must keep their registered defaults on every resolved config.
"""

from __future__ import annotations

import unittest

from tests.research import ibr_golden_support as golden


REGISTERED_VARIANTS = [
    "ibr.baseline", "ibr.range.30", "ibr.range.45", "ibr.target.1_5r",
    "ibr.target.3r", "ibr.buffer.0bps", "ibr.buffer.10bps",
]


class IBRGoldenEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = golden.load_fixture()
        cls.full = golden.compute_fingerprint()
        cls.current = golden.compact_fingerprint(
            cls.full, cls.fixture["record_keys"])
        cls.scenarios = {
            (row["symbol"], row["session"]): row["scenario"]
            for row in cls.fixture["sessions"]}

    def test_fixture_covers_every_registered_arm_and_the_shipped_config(self):
        self.assertEqual(self.fixture["schema"], "ibr-golden-signals.v1")
        self.assertEqual(
            sorted(arm["variant_id"] for arm in self.fixture["arms"]),
            sorted(REGISTERED_VARIANTS))
        self.assertEqual(self.current["sessions"], self.fixture["sessions"])
        signals = sum(
            1 for arm in self.fixture["arms"] for row in arm["runtime"]
            if row["full_scan"] is not None)
        trades = sum(len(arm["replay_strict"]["trades"])
                     for arm in self.fixture["arms"])
        self.assertGreater(signals, 0)
        self.assertGreater(trades, 0)

    def test_registered_arm_identities_and_mapped_configs_are_unchanged(self):
        for original, current in zip(self.fixture["arms"],
                                     self.current["arms"], strict=True):
            with self.subTest(variant=original["variant_id"]):
                for key in ("variant_id", "spec_identity", "config_identity",
                            "effective_config", "strict_config",
                            "fallback_config"):
                    self.assertEqual(current[key], original[key], key)

    def test_runtime_contract_signals_are_unchanged(self):
        pairs = [(arm["variant_id"], original["runtime"], arm["runtime"])
                 for original, arm in zip(self.fixture["arms"],
                                          self.current["arms"], strict=True)]
        for name in ("validated", "raw"):
            pairs.append((f"config.yaml:{name}",
                          self.fixture["shipped_runtime"][name],
                          self.current["shipped_runtime"][name]))
        for label, original, current in pairs:
            with self.subTest(runtime=label):
                self.assertEqual(current, original)

    def _assert_replay_equivalent(self, label, original, current):
        self.assertEqual(current["refusals"], original["refusals"], label)
        self.assertEqual(current["authorizing"], original["authorizing"])
        self.assertEqual(current["diagnostic_only"],
                         original["diagnostic_only"])
        self.assertEqual(len(current["trades"]), len(original["trades"]))
        relabelled = 0
        for before, after in zip(original["trades"], current["trades"],
                                 strict=True):
            for key in ("sha256", "symbol", "session_date", "exit_reason"):
                self.assertEqual(after[key], before[key], f"{label} {key}")
            scenario = self.scenarios[(before["symbol"],
                                       before["session_date"][2:])]
            expected = before["canonical_exit_reason"]
            if (expected == "session_force_flat" and
                    scenario in golden.TRUNCATED_HOLD_SCENARIOS):
                self.assertEqual(before["exit_reason"], "force_flat")
                expected = "data_discontinuity"
                relabelled += 1
            self.assertEqual(after["canonical_exit_reason"], expected,
                             f"{label} {before['symbol']} "
                             f"{before['session_date']}")
        return relabelled

    def test_replay_trades_are_unchanged_except_the_a07_label(self):
        relabelled = 0
        for original, current in zip(self.fixture["arms"],
                                     self.current["arms"], strict=True):
            for lane in ("replay_strict", "replay_bar_fallback"):
                relabelled += self._assert_replay_equivalent(
                    f"{original['variant_id']} {lane}",
                    original[lane], current[lane])
        legacy = self.fixture["legacy_research_defaults"]
        now = self.current["legacy_research_defaults"]
        self.assertEqual(now["config"], legacy["config"])
        for lane in ("replay_default", "replay_bar_fallback"):
            relabelled += self._assert_replay_equivalent(
                f"legacy {lane}", legacy[lane], now[lane])
        # The fixtures include truncated holds, so the correction is
        # exercised rather than vacuously satisfied.
        self.assertGreater(relabelled, 0)

    def test_only_declared_fields_are_added(self):
        original = self.fixture["record_keys"]
        now = golden.record_keys(self.full)
        self.assertEqual(sorted(set(now["signal"]) - set(original["signal"])),
                         ["signal_bar_close_ts", "signal_bar_open_ts"])
        self.assertEqual(now["trade"], original["trade"])
        self.assertEqual(now["effective_config"], original["effective_config"])
        self.assertEqual(
            sorted(set(now["replay_config"]) - set(original["replay_config"])),
            ["hold_across_gaps", "relative_volume_mode",
             "relative_volume_sessions"])
        for key in ("signal", "replay_config"):
            self.assertFalse(set(original[key]) - set(now[key]))

    def test_added_signal_timestamps_name_the_signal_bar(self):
        for signal in golden._iter_signals(self.full):
            close = signal["signal_bar_close_ts"]
            self.assertEqual(close, signal["signal_ts"])
            opened = float(signal["signal_bar_open_ts"][2:])
            self.assertEqual(opened + 60.0, float(close[2:]))

    def test_opt_in_fields_keep_their_registered_defaults(self):
        configs = [arm[name] for arm in self.full["arms"]
                   for name in ("strict_config", "fallback_config")]
        configs.append(self.full["legacy_research_defaults"]["config"])
        for config in configs:
            self.assertEqual(config["relative_volume_mode"],
                             "opening_range_mean")
            self.assertEqual(config["relative_volume_sessions"], 5)
            self.assertIs(config["hold_across_gaps"], False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
