"""The strategy register: what may run, and on what evidence.

This module is the single place that answers "which strategies exist, what
does each one claim, and how much do we believe it". It is deliberately pure
data with no imports from the rest of ``agent`` so that configuration
validation, the engine, the prompt builder and offline research can all read
it without an import cycle.

Three ideas are encoded here, and they are the point of the module:

**A strategy is a hypothesis, not a set of parameters.** Every entry carries a
``mechanism`` (who loses the money and why they cannot stop) and a
``falsification`` (what observation would kill it). A strategy that cannot
state both is not ready to be registered, let alone traded.

**Confidence is computed, not asserted.** ``tier`` records how far a strategy
has actually got through the evidence gates, from T0_REJECTED to T4_CONFIRMED.
It is set by research, read by policy, and never by a hunch.

**The tier is enforced where it matters.** ``momentum`` is T0_REJECTED: across
115,929 signals it showed a 45.6-47.3% directional hit rate and -0.096 R at
ordinary costs, with 0 of 79 walk-forward variants positive out-of-sample.
Demo may run anything implemented, because demo is an operations rehearsal.
Live requires T3_VALIDATED, so a strategy known to be negative cannot reach
real capital by editing one line of config.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Ordered worst to best. A tier is a claim about evidence, so the ladder is
# the same ladder research/tournament.py scores against.
TIERS = (
    "T0_REJECTED",     # failed a gate, or its placebo scored close to it
    "T1_HYPOTHESIS",   # mechanism and falsification stated, nothing tested
    "T2_CANDIDATE",    # beat the nulls and stayed positive out-of-sample
    "T3_VALIDATED",    # survived the placebo and realistic costs
    "T4_CONFIRMED",    # forward evidence agrees, at the required sample size
)

# Live capital requires a strategy that has cleared the placebo and cost
# gates. Demo deliberately does not: running a known-negative strategy on
# paper to rehearse the machinery is a legitimate and useful thing to do.
LIVE_MIN_TIER = "T3_VALIDATED"


class UnknownStrategy(KeyError):
    """Raised when configuration names a strategy that is not registered."""


@dataclass(frozen=True)
class StrategySpec:
    """One registered strategy and everything policy needs to know about it."""

    id: str
    version: str
    # --- what the strategy claims -------------------------------------
    mechanism: str
    falsification: str
    tier: str
    # --- what the machinery must provide ------------------------------
    signal_timeframe: str
    required_timeframes: tuple[str, ...]
    # Per-strategy ceiling on holding time. config.yaml may tighten this but
    # never loosen it, so a day-trading contract cannot be turned into a
    # multi-day one by nudging a number.
    max_hold_hours_ceiling: float
    execution_style: str            # "taker" | "maker"
    setup_types: tuple[str, ...]
    # Appended to the shared system prompt. Empty for strategies whose
    # decisions are fully deterministic and need no analyst layer.
    prompt_fragment: str = ""
    # False while a strategy is registered for research but has no live
    # contract implementation. Configuration refuses to start on one.
    implemented: bool = False
    # Free-form note explaining a tier or an implementation gap.
    notes: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(
                f"strategy {self.id!r} has unknown tier {self.tier!r}")
        if self.execution_style not in {"taker", "maker"}:
            raise ValueError(
                f"strategy {self.id!r} has unknown execution style "
                f"{self.execution_style!r}")
        if self.signal_timeframe not in self.required_timeframes:
            raise ValueError(
                f"strategy {self.id!r} signal timeframe "
                f"{self.signal_timeframe!r} is not in its required timeframes")
        for label, text in (("mechanism", self.mechanism),
                            ("falsification", self.falsification)):
            if not text.strip():
                raise ValueError(
                    f"strategy {self.id!r} must state a {label}")

    def tier_rank(self) -> int:
        return TIERS.index(self.tier)

    def meets(self, minimum_tier: str) -> bool:
        return self.tier_rank() >= TIERS.index(minimum_tier)


# The archetype descriptions the analyst needs in order to label a momentum
# setup. Kept beside the spec rather than inside the shared prompt so that
# registering a second strategy does not mean teaching the model about a
# strategy it is not running.
_MOMENTUM_PROMPT = """
SETUP ARCHETYPES (long side described; mirror them for shorts)
- Trend continuation pullback: trend_4h and trend_1h up, then the latest \
completed 15m impulse resumes upward after a pullback (trend_15m flat or up, \
mom_15m_pct and mom_1h_pct positive, ema20_1h_dist_pct near zero), and \
range_pos_pct is recovering. Stop beyond the \
recent swing low: at least swing_low_pct plus a buffer, and never tighter \
than 1x atr_1h_pct.
- Range breakout: the latest completed 15m candle must freshly close beyond \
the preceding 20-candle range, with range_pos_pct near 100, trend_1h not \
opposed and volume/momentum expanding; enter in the breakout direction with the stop \
just inside the prior range (roughly swing_low_pct back for a long), \
never tighter than 1x atr_1h_pct.
- Funding squeeze: funding deeply negative and extreme versus its own history \
while price stops making new lows, closes back up, the perp trades at a \
discount and open interest is measurable - crowded shorts are paying to hold \
a losing position and fuel the reversal. Higher risk; demand a clear structure/ATR \
invalidation, enough net room for the chosen exit policy, and assign lower \
confidence.
- Avoid: mid-range entries with mixed trends; chasing a move already \
several ATRs extended; fading a strong aligned trend just because RSI is \
stretched.
""".strip()


REGISTRY: dict[str, StrategySpec] = {
    spec.id: spec for spec in (
        StrategySpec(
            id="momentum",
            version="phase1-v2",
            mechanism=(
                "None established. Retained as the benchmark null that any "
                "new strategy must beat, and as the only strategy here whose "
                "true expectancy has actually been measured."),
            falsification=(
                "Already falsified: directional hit rate 45.6-47.3% at every "
                "horizon from 15m to 24h, -0.096 R at ordinary costs "
                "(t=-4.60), 0 of 79 walk-forward variants positive "
                "out-of-sample, and at zero cost both random entry timing "
                "and the inverted signal beat it."),
            tier="T0_REJECTED",
            signal_timeframe="15m",
            required_timeframes=("15m", "1h", "4h"),
            max_hold_hours_ceiling=48.0,
            execution_style="taker",
            setup_types=("trend_continuation", "range_breakout",
                         "funding_squeeze", "other"),
            prompt_fragment=_MOMENTUM_PROMPT,
            implemented=True,
            notes=(
                "Runnable on demo as an operations rehearsal. Blocked from "
                "live by LIVE_MIN_TIER, which is the intended behaviour."),
            evidence=("research/results/edge-audit-2024-2026/REPORT.md",),
        ),
        StrategySpec(
            id="flush-fade",
            version="v1",
            mechanism=(
                "Liquidation engines sell at market regardless of price. "
                "That flow is price-insensitive, mechanically finite and "
                "overshoots, and whoever absorbs it is compensated. The "
                "payer is the over-leveraged trader whose margin ran out "
                "and who has no discretion about exiting. Open interest "
                "falling during an adverse move distinguishes forced "
                "deleveraging from new positioning, which should not "
                "revert."),
            falsification=(
                "Among the largest adverse moves, bars with open interest "
                "falling show no more 4-24h reversion than bars with open "
                "interest rising. If both subsets behave alike, OI adds "
                "nothing and the move is noise."),
            tier="T1_HYPOTHESIS",
            signal_timeframe="15m",
            required_timeframes=("15m", "1h", "4h"),
            max_hold_hours_ceiling=24.0,
            execution_style="taker",
            setup_types=("flush_reversion",),
            notes=(
                "Testable now but data-limited: OKX serves only ~60 days of "
                "open-interest history, which is a first look rather than a "
                "test."),
        ),
        StrategySpec(
            id="funding-carry",
            version="v1",
            mechanism=(
                "Funding is the price of leverage. When positioning is "
                "crowded the crowd pays continuously to hold, and the payer "
                "is the leveraged long in a persistently positive-funding "
                "regime. The return source is the carry itself rather than "
                "a directional forecast."),
            falsification=(
                "Holding the funding-receiving side through settlements does "
                "not produce positive net expectancy once price risk over "
                "the same window is charged against it."),
            tier="T1_HYPOTHESIS",
            signal_timeframe="1h",
            required_timeframes=("1h", "4h"),
            max_hold_hours_ceiling=240.0,
            execution_style="taker",
            setup_types=("carry",),
            notes=(
                "Needs multi-day holds, which the momentum-era 48h ceiling "
                "forbade. Measured rates are ~0.002%/8h on majors, so the "
                "interesting cells are where funding is pinned at its clamp."),
        ),
        StrategySpec(
            id="trend-multiday",
            version="v1",
            mechanism=(
                "Slow adoption flows and reflexive positioning make crypto "
                "trend persist at multi-week horizons. Cost falls from "
                "roughly 15% of a typical intraday move to about 1% of a "
                "multi-day one. The payer is the mean-reversion seller who "
                "is early."),
            falsification=(
                "Extending the existing features to 4-14 day horizons leaves "
                "expectancy negative net of costs, or positive only in the "
                "in-sample half."),
            tier="T1_HYPOTHESIS",
            signal_timeframe="4h",
            required_timeframes=("1h", "4h"),
            max_hold_hours_ceiling=336.0,
            execution_style="taker",
            setup_types=("trend_follow",),
            notes=(
                "Cheapest test available: no new features, one horizon "
                "constant changed in research/signal_lab.py."),
        ),
        StrategySpec(
            id="ls-ratio-fade",
            version="v1",
            mechanism=(
                "Within an instrument, retail long/short ratio rising "
                "relative to its own mean precedes outperformance over "
                "16-48h. A cross-sectional fixed effect was tested and "
                "rejected as the explanation: average long/short ratio "
                "correlates -0.432 with 30-day return, a headwind, and "
                "per-instrument demeaning makes the signal stronger."),
            falsification=(
                "On more than 30 days of data the effect fails to survive a "
                "placebo that shuffles the signal within each timestamp."),
            tier="T1_HYPOTHESIS",
            signal_timeframe="1h",
            required_timeframes=("1h", "4h"),
            max_hold_hours_ceiling=72.0,
            execution_style="taker",
            setup_types=("positioning_fade",),
            notes=(
                "+1.114% at 48h (t=2.72) on ~210 observations. On this data "
                "a placebo reached t=2.60 from pure noise, so this is a "
                "reason to collect data, not to trade."),
            evidence=("research/results/edge-discovery-method/REPORT.md",),
        ),
        StrategySpec(
            id="scalp-maker",
            version="v1",
            mechanism=(
                "A maker is paid the spread for supplying liquidity; the "
                "payer is the impatient taker. This does not require "
                "predicting direction, which is why it is the one scalping "
                "design that is not arithmetically dead: round-trip taker "
                "cost alone exceeds a typical 1m move."),
            falsification=(
                "Recorded book state shows resting orders require more than "
                "~10-15 bps of penetration to fill, at which point adverse "
                "selection exceeds the fee saving and the strategy is "
                "negative by construction."),
            tier="T1_HYPOTHESIS",
            signal_timeframe="1m",
            required_timeframes=("1m", "15m"),
            max_hold_hours_ceiling=4.0,
            execution_style="maker",
            setup_types=("spread_capture",),
            notes=(
                "Hard-blocked on data, not on development. OKX never serves "
                "historical order books, so fill quality cannot be simulated "
                "until research/record_flow.py has collected it. First "
                "honest result is roughly three months after recording "
                "starts. Spread varies ~90x across the universe (BTC 0.015 "
                "bps, DOGE 1.39 bps), so this is a tight-spread majors-only "
                "design."),
        ),
    )
}


def spec_for(strategy_id: str) -> StrategySpec:
    """Return the registered spec or raise ``UnknownStrategy``."""
    try:
        return REGISTRY[strategy_id]
    except KeyError:
        raise UnknownStrategy(
            f"strategy.id {strategy_id!r} is not registered. Known "
            f"strategies: {', '.join(sorted(REGISTRY))}") from None


def implemented_ids() -> tuple[str, ...]:
    return tuple(sorted(s.id for s in REGISTRY.values() if s.implemented))


def live_eligible_ids() -> tuple[str, ...]:
    return tuple(sorted(
        s.id for s in REGISTRY.values()
        if s.implemented and s.meets(LIVE_MIN_TIER)))
