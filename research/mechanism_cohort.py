"""Frozen, small mechanism comparisons; never a declaration of positive edge.

Changing an arm requires a new cohort version. Shared sessions and overlapping
signals are correlated observations, not twelve independent portfolios.
"""
from agent.contracts.rule import RULE_SCHEMA_V5, rule_variant_id, validate_rule_spec
from .edge_ledger_store import content_hash

COHORT_ID = "intraday-mechanisms.v1"


def mechanism_cohort(name: str = COHORT_ID) -> dict:
    if name != COHORT_ID:
        raise ValueError(f"unknown mechanism cohort: {name}")
    common = {"schema": RULE_SCHEMA_V5, "side": "both", "confirmation": "none",
              "atr_period": 14, "stop_atr": 1.0, "target_r": 2.0,
              "max_hold_bars": 60, "entry_before_minutes": 300}
    trend = {"regime_mode": "trend", "regime_timeframe_minutes": 5,
             "regime_lookback_bars": 3, "regime_efficiency_threshold": .4}
    roots = [
        ("opening_continuation", {"family": "opening_range_breakout",
          "range_minutes": 15, "threshold_bps": 5.0},
         "Continuation after the completed opening range, conditional on aligned price-path context.",
         [({}, "Unfiltered opening-range parent."),
          (trend, "Add aligned completed 5-minute context."),
          ({**trend, "regime_timeframe_minutes": 15}, "Compare a slower completed 15-minute context."),
          ({**trend, "max_hold_bars": 30}, "Compare a shorter thesis horizon with the same entry predicate.")]),
        ("trend_pullback", {"family": "trend_pullback", "lookback": 10,
          "slow_lookback": 35, "threshold_bps": 15.0},
         "Trend continuation after a completed retracement and a close through its last bar.",
         [({}, "Legacy candle-based parent for an explicit behavioral comparison."),
          ({"entry_trigger": "reclaim"}, "Require the prior trend, retracement, and close reclaim."),
          ({"entry_trigger": "reclaim", **trend}, "Condition the structural entry on aligned context."),
          ({"entry_trigger": "reclaim", **trend, "trailing_stop_r": 1.5},
           "Compare a completed-close trailing exit on the same structural entry.")]),
        ("fair_value_reversion", {"family": "vwap_reversion", "lookback": 20,
          "threshold_bps": 20.0, "target_mode": "session_vwap", "max_hold_bars": 30},
         "Reversal toward frozen session value while the completed price path is non-directional.",
         [({}, "Frozen session-VWAP target parent."),
          ({"entry_trigger": "reclaim"}, "Require a confirmed reversal while distance to value remains."),
          ({"entry_trigger": "reclaim", **trend, "regime_mode": "range"},
           "Add a low-efficiency completed price-path condition."),
          ({"entry_trigger": "reclaim", **trend, "regime_mode": "range", "max_hold_bars": 15},
           "Compare a shorter reversion horizon with the same entry predicate.")]),
    ]
    families = []
    for name, root, thesis, comparisons in roots:
        arms = []
        for index, (delta, reason) in enumerate(comparisons):
            spec = validate_rule_spec({**common, **root, **delta})
            arms.append({"arm": index, "variant_id": rule_variant_id(spec),
                         "rule_spec": spec, "reason": reason})
        families.append({"name": name, "thesis": thesis, "arms": arms})
    manifest = {"cohort_id": COHORT_ID, "families": families,
                "registered_arms": 12,
                "selection_policy": "diagnostic_only_no_automatic_winner_selection",
                "risk_policy": "unchanged_runtime_limits_and_authored_stop_stress_veto"}
    return {**manifest, "manifest_hash": content_hash(manifest)}
