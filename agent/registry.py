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

**Confidence is evidenced, not asserted.** ``tier`` records how far a strategy
has actually progressed through qualification, from T0_REJECTED to T4_CONFIRMED.
It is backed by persisted research, changed through review, read by policy,
and never moved by a hunch or by an exploratory command editing code.

**The tier is enforced where it matters.** ``momentum`` is T0_REJECTED: across
115,929 signals it showed a 45.6-47.3% directional hit rate and -0.096 R at
ordinary costs, with 0 of 79 walk-forward variants positive out-of-sample.
Demo may run only a strategy whose deterministic contract and analyst prompt
are both ready. Contracts that are implemented only for cross-strategy shadow
research remain unavailable to the trading loop. Live additionally requires
T3_VALIDATED, so a strategy known to be negative cannot reach real capital by
editing one line of config.

**Where that evidence comes from, and what it is not.** Every tier claim in
this module currently rests on a *recomputed* backtest over downloaded OHLCV
- ``research/edge_lab.py`` and its dependents - which reconstructs indicators
after the fact rather than reading the snapshot the agent actually decided
from. ``research/AUTONOMOUS_RESEARCH.md`` is explicit that this is a different
system than the one you run: some snapshot fields come from the live 24h ticker
and cannot be reconstructed, so a recomputed backtest silently mixes revised
data with the original.

That does not make the evidence worthless, and the direction of the bias is
against the strategy rather than for it, so a T0_REJECTED verdict is the
conservative reading. It does mean the verdict is **exploratory and not yet
confirmed by the faithful path**: the replay harness in ``research/replay.py``
re-derives decisions from the recorded snapshot using the production contract
and risk engine, and proposal fidelity requires it to reproduce the live
agent's own decisions before any number downstream is trusted.

The tier gate stays as it is. Keeping momentum away from live capital on
exploratory evidence is the right way to be wrong. But a tier may only be
*raised* on journal-replay evidence, never on a recomputed backtest. See
``research/AUTONOMOUS_RESEARCH.md``.
"""

from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .forward_models import ForwardOutcomeModel, model_for


# Evidence tiers are ordered from weakest to strongest.
TIERS = (
    "T0_REJECTED",     # failed a gate, or its placebo scored close to it
    "T1_HYPOTHESIS",   # mechanism and falsification stated, nothing tested
    "T2_CANDIDATE",    # beat the nulls and stayed positive out-of-sample
    "T3_VALIDATED",    # survived the placebo and realistic costs
    "T4_CONFIRMED",    # forward evidence agrees, at the required sample size
)

# Live capital requires validated placebo and cost evidence; demo does not.
LIVE_MIN_TIER = "T3_VALIDATED"

# Live-authorizing tiers must cite their content-addressed evidence packet.
PACKET_REFERENCE = re.compile(r"^t3-packet:[0-9a-f]{64}$")


def require_t3_packet_reference(spec: "StrategySpec") -> str | None:
    """Return the sole live-authorizing packet citation or fail closed.

    Evidence may include human-readable supporting documents, but a strategy
    at or above ``LIVE_MIN_TIER`` must carry exactly one content-addressed T3
    packet citation.  Multiple citations are ambiguous: the runtime cannot
    know which reviewed packet authorized the deployed artifact.
    """
    if spec.tier_rank() < TIERS.index(LIVE_MIN_TIER):
        return None
    references = [
        str(reference) for reference in spec.evidence
        if PACKET_REFERENCE.fullmatch(str(reference))
    ]
    if len(references) != 1:
        raise ValueError(
            f"strategy {spec.id!r} claims {spec.tier}, which requires "
            "exactly one t3-packet:<64hex> evidence citation")
    return references[0]

class UnknownStrategy(KeyError):
    """Raised when configuration names a strategy that is not registered."""


@dataclass(frozen=True)
class StrategySpec:
    """One registered strategy and everything policy needs to know about it."""

    id: str
    version: str
    mechanism: str
    falsification: str
    tier: str
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
    # True only when the analyst schema/prompt can produce this strategy's
    # setup types. An implemented evidence builder is sufficient for shadow
    # research but not sufficient to make the active LLM trading path valid.
    analyst_ready: bool = False
    # False when the holding horizon cannot reach the real-time sample floor.
    realtime_eligible: bool = True
    # True only when research/export_live.py has a validated stop/target,
    # cost and holding model for this mechanism. Raw shadow signals may be
    # collected without it, but they must not be converted into expectancy.
    forward_model_ready: bool = False
    forward_model_id: str = ""
    # Free-form note explaining a tier or an implementation gap.
    notes: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)
    # Parameters this strategy's contract reads out of cfg["strategy"] when
    # it is evaluated in SHADOW, i.e. while a different strategy is the one
    # actually trading. The active strategy always uses config.yaml; these
    # are what a non-active strategy is evaluated with, so that shadow
    # results are attributable to a stated parameter set rather than to
    # whatever the live config happened to hold.
    contract_params: dict = field(default_factory=dict)

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
        # Ad-hoc specs used by tooling and tests may deliberately omit a
        # forward model. Every entry in the authoritative REGISTRY binds one,
        # and claiming readiness without that binding is always invalid.
        if self.forward_model_id:
            model = model_for(self.id)
            if self.forward_model_id != model.model_id:
                raise ValueError(
                    f"strategy {self.id!r} must bind forward model "
                    f"{model.model_id!r}")
            if self.forward_model_ready != model.contract_complete:
                raise ValueError(
                    f"strategy {self.id!r} forward_model_ready must reflect "
                    f"whether {model.model_id}'s contract is complete")
        elif self.forward_model_ready:
            raise ValueError(
                f"strategy {self.id!r} cannot be forward-model ready without "
                "a forward_model_id")
        require_t3_packet_reference(self)

    @property
    def forward_model(self) -> ForwardOutcomeModel:
        if not self.forward_model_id:
            raise ValueError(
                f"strategy {self.id!r} has no forward outcome model")
        return model_for(self.id)

    def tier_rank(self) -> int:
        return TIERS.index(self.tier)

    def meets(self, minimum_tier: str) -> bool:
        return self.tier_rank() >= TIERS.index(minimum_tier)


@dataclass(frozen=True)
class StrategyContract:
    """The one executable identity for a registered strategy.

    ``StrategySpec`` describes the mechanism and policy while
    ``ForwardOutcomeModel`` describes how a firing becomes an observation.
    Keeping those two records is useful for their existing callers, but using
    either one by itself is unsafe: a setup can be labelled with one strategy
    and scored with another model.  This immutable composite is the boundary
    all runtime and evidence code can use to prove that the pieces agree.

    Builder functions are intentionally kept out of the serialized payload;
    their stable module/qualname is included instead.  A callable is exposed
    for the runtime through ``builder`` and is not part of equality or hash
    semantics, which keeps manifests deterministic across Python processes.
    """

    spec: StrategySpec
    outcome_model: ForwardOutcomeModel
    builder_id: str
    builder: Callable[..., dict] = field(repr=False, compare=False,
                                         hash=False)
    variant_id: str = ""
    variant_parameters: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.outcome_model.strategy_id != self.spec.id:
            raise ValueError(
                f"strategy {self.spec.id!r} and forward model "
                f"{self.outcome_model.strategy_id!r} disagree")
        if not self.builder_id or not callable(self.builder):
            raise ValueError(f"strategy {self.spec.id!r} has no executable builder")
        if not self.variant_id:
            object.__setattr__(self, "variant_id", baseline_variant_id(self.spec.id))
        if not self.variant_parameters:
            params = dict(self.spec.contract_params)
            params.update({
                "min_stop_atr_multiple": self.outcome_model.stop_atr_multiple,
                "fixed_reward_risk": self.outcome_model.reward_risk,
                "forward_horizon_hours": self.outcome_model.horizon_hours,
            })
            object.__setattr__(self, "variant_parameters", tuple(
                sorted(params.items(), key=lambda item: item[0])))

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def version(self) -> str:
        return self.spec.version

    @property
    def model(self) -> ForwardOutcomeModel:
        """Compatibility spelling for callers that call it the model."""
        return self.outcome_model

    @property
    def forward_model(self) -> ForwardOutcomeModel:
        return self.outcome_model

    @property
    def builder_identity(self) -> str:
        return self.builder_id

    @property
    def mechanism(self) -> str:
        return self.spec.mechanism

    @property
    def tier(self) -> str:
        return self.spec.tier

    @property
    def evidence(self) -> tuple[str, ...]:
        return self.spec.evidence

    @property
    def thresholds(self) -> dict:
        return self.parameters

    @property
    def setup_policy(self) -> dict:
        return {
            "setup_types": tuple(self.spec.setup_types),
            "signal_timeframe": self.spec.signal_timeframe,
            "required_timeframes": tuple(self.spec.required_timeframes),
            "max_hold_hours_ceiling": self.spec.max_hold_hours_ceiling,
        }

    @property
    def identity(self) -> dict:
        return {"strategy_id": self.id, "strategy_version": self.version,
                "variant_id": self.variant_id}

    @property
    def parameters(self) -> dict:
        return dict(self.variant_parameters)

    def manifest(self) -> dict:
        """Return a JSON/YAML-safe canonical semantic manifest."""
        return {
            "strategy_id": self.id,
            "strategy_version": self.version,
            "variant_id": self.variant_id,
            "identity": {"id": self.id, "version": self.version},
            "mechanism": self.spec.mechanism,
            "falsification": self.spec.falsification,
            "tier": self.spec.tier,
            "evidence": list(self.spec.evidence),
            "implementation": {
                "implemented": self.spec.implemented,
                "analyst_ready": self.spec.analyst_ready,
                "forward_model_ready": self.spec.forward_model_ready,
                "realtime_eligible": self.spec.realtime_eligible,
            },
            "executable": {
                "builder_id": self.builder_id,
                "setup_types": list(self.spec.setup_types),
                "signal_timeframe": self.spec.signal_timeframe,
                "required_timeframes": list(self.spec.required_timeframes),
                "max_hold_hours_ceiling": self.spec.max_hold_hours_ceiling,
                "execution_style": self.spec.execution_style,
            },
            "parameters": self.parameters,
            "outcome_model": self.outcome_model.as_dict(),
        }

    @property
    def semantic_hash(self) -> str:
        encoded = json.dumps(self.manifest(), sort_keys=True,
                             separators=(",", ":"), allow_nan=False,
                             default=_json_default).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def hash(self) -> str:
        """Short compatibility alias; manifests always use full SHA-256."""
        return self.semantic_hash

    @property
    def manifest_hash(self) -> str:
        return self.semantic_hash

    @property
    def canonical_hash(self) -> str:
        return self.semantic_hash

    @property
    def contract_hash(self) -> str:
        return self.semantic_hash

    def as_dict(self) -> dict:
        payload = self.manifest()
        payload["semantic_hash"] = self.semantic_hash
        return payload

    def canonical_manifest(self) -> dict:
        return self.manifest()


def _json_default(value: Any):
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"value {value!r} is not JSON serializable")


def baseline_variant_id(strategy_id: str) -> str:
    """Stable readable id for the registered (unmodified) semantics."""
    return str(strategy_id).replace("-", "_") + ".baseline"


# Strategy-local prompt text prevents cross-strategy label leakage.
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
            version="phase1-v3",
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
            analyst_ready=True,
            forward_model_ready=True,
            forward_model_id="momentum.fixed_rr.15m.v2",
            notes=(
                "Runnable on demo as an operations rehearsal. Blocked from "
                "live by LIVE_MIN_TIER, which is the intended behaviour. "
                "The T0 verdict rests on a recomputed OHLCV backtest, which "
                "is exploratory evidence: it is enough to withhold capital "
                "and not enough to raise a tier. Confirmation requires the "
                "proposal-fidelity journal replay and three-arm analyst-effect "
                "analysis - see research/AUTONOMOUS_RESEARCH.md."),
            evidence=("research/results/edge-audit-2024-2026/REPORT.md",),
            contract_params={
                "breakout_range_threshold_pct": 85.0,
                "breakout_min_relative_volume": 1.0,
                "funding_extreme_pct_per_8h": 0.01,
                "hard_max_entry_extension_atr": 1.2,
            },
        ),
        StrategySpec(
            id="flush-fade",
            version="v1",
            mechanism=(
                "Liquidation engines sell at market regardless of price. "
                "That flow is price-insensitive, mechanically finite and "
                "overshoots, and whoever absorbs it is compensated. The "
                "payer is the over-leveraged trader whose margin ran out "
                "and who has no discretion about exiting. Filled "
                "liquidation notional observes that flow directly "
                "rather than inferring it, and distinguishes forced "
                "deleveraging from new positioning, which should not "
                "revert."),
            falsification=(
                "Among the largest adverse moves, bars in the top decile of "
                "filled liquidation notional show no more 4-24h reversion "
                "than bars where no liquidation printed at all. If forced "
                "flow is what reverts, reversion must scale with how much of "
                "it was actually filled; if the subsets behave alike, the "
                "cascade is not the mechanism and the move is noise."),
            tier="T0_REJECTED",
            signal_timeframe="15m",
            required_timeframes=("15m", "1h", "4h"),
            max_hold_hours_ceiling=24.0,
            execution_style="taker",
            setup_types=("flush_reversion",),
            forward_model_id="flush_fade.reversion.15m.v2",
            forward_model_ready=True,
            implemented=True,
            contract_params={
                "flush_min_move_atr": 1.5,
                "flush_min_oi_drop_pct": 1.0,
                "flush_min_relative_volume": 1.2,
                "hard_max_entry_extension_atr": 5.0,
            },
            notes=(
                "Shadowed deliberately as a NEGATIVE CONTROL: a strategy "
                "measured negative should keep losing, and if it starts "
                "winning that is evidence about the pipeline rather than "
                "the market. The tier gate keeps it away from capital. "
                "Tested and rejected on first contact with data. 337 "
                "non-overlapping trades, 8 instruments, 60 days of "
                "open-interest coverage: -0.288% per trade at base costs and "
                "-0.043% frictionless, so it is negative before costs rather "
                "than merely cost-killed. Random entry timing (-0.231%) beat "
                "it, out-of-sample (-0.610%) was worse than in-sample "
                "(-0.195%), and inverting it was also negative (-0.368%), "
                "which rules out a direction error. The mechanism remains "
                "plausible; this parameterisation of it does not survive. "
                "Caveat, stated at pre-registration and not after: OKX serves "
                "only ~60 days of open interest, so this is an underpowered "
                "test - but an underpowered test that points the wrong way "
                "is not evidence of a hidden edge."),
            evidence=("research/results/tournament/REPORT.md",
                      "research/hypotheses/flush-fade.yaml"),
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
            tier="T0_REJECTED",
            signal_timeframe="1h",
            required_timeframes=("1h", "4h"),
            max_hold_hours_ceiling=240.0,
            execution_style="taker",
            setup_types=("carry",),
            realtime_eligible=False,   # real-time sample floor is unreachable
            forward_model_id="funding_carry.interval.1h.v2",
            forward_model_ready=True,
            implemented=True,
            contract_params={
                "funding_extreme_pct_per_8h": 0.01,
                "carry_percentile": 80.0,
                "carry_min_samples": 20,
                "hard_max_entry_extension_atr": 3.0,
            },
            notes=(
                "MECHANISM FALSIFIED, and instructively so: it posted "
                "+2.008% per trade over 116 trades, beat every null, "
                "survived the placebo (-4%), was better out-of-sample than "
                "in, and did NOT disappear on liquid majors. Decomposed, "
                "funding contributed +0.039% and price movement +1.969% - "
                "so it is a directional strategy wearing a carry label. "
                "Rejected because a result whose source is not understood "
                "cannot be told apart from overfitting and gives no warning "
                "when it stops. The residual directional result is a "
                "separate hypothesis needing its own pre-registration; "
                "folding it in here would be the retro-fitting this "
                "framework exists to prevent."),
            evidence=("research/results/tournament/REPORT.md",
                      "research/hypotheses/funding-carry.yaml"),
        ),
        StrategySpec(
            id="funding-unwind",
            version="v1",
            mechanism=(
                "Extreme funding is a POSITIONING indicator, not a yield "
                "opportunity. When funding sits at the top of its own recent "
                "range, leveraged longs are crowded and paying to stay in. "
                "Crowded leveraged positions are unstable: they are held by "
                "traders who need the move to continue in order to keep "
                "affording the position, and who are forced out when it does "
                "not. The payer is the crowded leveraged trader squeezed out "
                "of a position they could not finance. This is the opposite "
                "economics to funding-carry, whose carry claim was falsified "
                "at 2% attribution; here the carry is incidental and the "
                "return is the unwind."),
            falsification=(
                "Funding contributes 50% or more of the result, which would "
                "make it the carry trade already rejected; or the effect "
                "fails outside 2026-05..2026-07, the window that generated "
                "it; or the placebo reaches 25% of the candidate; or it "
                "fails to beat the random-timing and random-direction "
                "nulls."),
            tier="T1_HYPOTHESIS",
            signal_timeframe="1h",
            required_timeframes=("1h", "4h"),
            max_hold_hours_ceiling=240.0,
            execution_style="taker",
            setup_types=("positioning_unwind",),
            realtime_eligible=False,   # real-time sample floor is unreachable
            forward_model_id="funding_unwind.reversion.1h.v2",
            forward_model_ready=True,
            implemented=True,
            contract_params={
                "funding_extreme_pct_per_8h": 0.01,
                "unwind_percentile": 80.0,
                "unwind_min_samples": 20,
                "hard_max_entry_extension_atr": 3.0,
            },
            notes=(
                "GENERATED from funding-carry's decomposition on this same "
                "window, so any result here is in-sample by construction and "
                "cannot confirm it. Stays T1 until out-of-sample funding "
                "history or enough forward shadow evidence exists; the "
                "tournament's sample-size cap enforces that independently of "
                "how large the in-sample number looks."),
            evidence=("research/hypotheses/funding-unwind.yaml",),
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
            realtime_eligible=False,   # real-time sample floor is unreachable
            forward_model_id="trend_multiday.4h.v2",
            forward_model_ready=True,
            implemented=True,
            contract_params={
                "trend_min_range_pos_pct": 55.0,
                "trend_max_atr_ratio": 2.0,
                "hard_max_entry_extension_atr": 2.0,
            },
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
            forward_model_id="ls_ratio_fade.1h.v2",
            forward_model_ready=True,
            implemented=True,
            contract_params={
                "ls_high_percentile": 80.0,
                "ls_low_percentile": 20.0,
                "hard_max_entry_extension_atr": 3.0,
            },
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
            forward_model_id="scalp_maker.book.1m.v2",
            forward_model_ready=True,
            implemented=True,
            contract_params={
                "scalp_max_spread_pct": 0.02,
                "scalp_min_abs_imbalance": 0.15,
                "scalp_min_depth_usd": 10_000.0,
                "hard_max_entry_extension_atr": 10.0,
            },
            notes=(
                "Historical validation remains blocked because OKX never "
                "serves historical order books. The real-time forward model "
                "uses only an observed two-sided book and records a data-"
                "missing veto when it is absent. Spread varies ~90x across "
                "the universe, so this remains a tight-spread design."),
        ),
    )
}


# This is deliberately not a normal one-axis research setting.  The tuning
# run changed the two positioning thresholds, the no-chase cap, and both
# payoff numbers together.  Giving it a first-class immutable identity keeps
# those semantics from being mistaken for the registered 80/20, 3 ATR,
# 2 ATR/2R contract or scheduled as a one-axis experiment.
TUNED_LS_VARIANT_ID = "ls_ratio_fade.tuned_70_30_ext_1_5_stop_1_target_3"
_IMMUTABLE_VARIANT_OVERRIDES: dict[str, dict[str, Any]] = {
    TUNED_LS_VARIANT_ID: {
        "ls_high_percentile": 70.0,
        "ls_low_percentile": 30.0,
        "hard_max_entry_extension_atr": 1.5,
        "min_stop_atr_multiple": 1.0,
        "fixed_reward_risk": 3.0,
    },
}


def _builder_for(strategy_id: str) -> tuple[str, Callable[..., dict]]:
    # Import lazily: contracts import registry to obtain the registered
    # strategy policies, so importing the package at module import time would
    # create a cycle.
    from .contracts import EVIDENCE_BUILDERS

    try:
        builder = EVIDENCE_BUILDERS[strategy_id]
    except KeyError:
        raise ValueError(
            f"strategy {strategy_id!r} has no executable evidence builder") from None
    return f"{builder.__module__}.{builder.__qualname__}", builder


def _base_contract(strategy_id: str) -> StrategyContract:
    spec = spec_for(strategy_id)
    model = model_for(strategy_id)
    builder_id, builder = _builder_for(strategy_id)
    return StrategyContract(
        spec=spec, outcome_model=model, builder_id=builder_id,
        builder=builder, variant_id=baseline_variant_id(strategy_id))


def contract_for(strategy_id: str) -> StrategyContract:
    """Return the canonical registered contract for ``strategy_id``.

    The returned object is immutable and contains both the mechanism policy
    and the exact forward outcome model.  It is the preferred runtime/evidence
    lookup; ``spec_for`` and ``model_for`` remain available as narrow legacy
    helpers for callers that need only one half of the contract.
    """
    return _base_contract(str(strategy_id))


def contract_for_variant(strategy_id: str, variant_id: str | None = None
                         ) -> StrategyContract:
    """Resolve a canonical contract plus an explicitly named immutable variant.

    Only the tuned ls-ratio-fade semantics are encoded here.  Ordinary
    research variants remain parameter overlays and are intentionally not
    promoted to executable contracts until they have their own reviewed
    identity.
    """
    base = contract_for(strategy_id)
    if variant_id in (None, "", "live", base.variant_id):
        return base
    if variant_id != TUNED_LS_VARIANT_ID:
        raise ValueError(
            f"variant {variant_id!r} has no immutable strategy contract; "
            "register a reviewed contract before using it for execution")
    if strategy_id != "ls-ratio-fade":
        raise ValueError(
            f"variant {variant_id!r} belongs to ls-ratio-fade, not {strategy_id!r}")
    overrides = _IMMUTABLE_VARIANT_OVERRIDES[variant_id]
    model = replace(
        base.outcome_model,
        stop_atr_multiple=float(overrides["min_stop_atr_multiple"]),
        reward_risk=float(overrides["fixed_reward_risk"]),
        stop_assumption="structure distance with a 1 ATR minimum",
        target_assumption="fixed 3R positioning-reversion target",
        contract_evidence=base.outcome_model.contract_evidence,
    )
    params = dict(base.parameters)
    params.update(overrides)
    return StrategyContract(
        spec=base.spec, outcome_model=model,
        builder_id=base.builder_id, builder=base.builder,
        variant_id=variant_id,
        variant_parameters=tuple(sorted(params.items())))


def contract_catalog() -> dict[str, StrategyContract]:
    """Return every registered strategy's canonical composite contract."""
    return {strategy_id: contract_for(strategy_id)
            for strategy_id in sorted(REGISTRY)}


def contract_catalog_manifest(*, include_variants: bool = True) -> dict:
    """Return deterministic catalog data suitable for YAML/reports/checks."""
    contracts = {
        strategy_id: contract.as_dict()
        for strategy_id, contract in contract_catalog().items()
    }
    if include_variants:
        tuned = contract_for_variant("ls-ratio-fade", TUNED_LS_VARIANT_ID)
        contracts["ls-ratio-fade"]["variants"] = {
            TUNED_LS_VARIANT_ID: tuned.as_dict(),
        }
    payload = {"schema": "strategy-contract-catalog.v1", "contracts": contracts}
    payload["catalog_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   allow_nan=False, default=_json_default).encode("utf-8")
    ).hexdigest()
    return payload


def validate_contract_config(cfg: Mapping[str, Any], *, require_variant: bool = False
                             ) -> StrategyContract:
    """Validate effective strategy values against a named composite contract.

    Legacy callers may omit ``strategy.variant_id`` while migrating.  Once a
    variant is declared, every semantic value that variant owns is checked;
    this is the startup/evidence boundary that prevents a tuned config from
    being scored under the base outcome model.
    """
    strategy = cfg.get("strategy") if isinstance(cfg, Mapping) else None
    if not isinstance(strategy, Mapping):
        raise ValueError("config.strategy must be a mapping")
    strategy_id = str(strategy.get("id") or "")
    variant_id = str(strategy.get("variant_id") or "")
    if require_variant and not variant_id:
        raise ValueError(
            f"strategy {strategy_id!r} must declare strategy.variant_id")
    contract = contract_for_variant(strategy_id, variant_id or None)
    if not variant_id:
        return contract
    expected = contract.parameters
    paths = {
        "ls_high_percentile": "ls_high_percentile",
        "ls_low_percentile": "ls_low_percentile",
        "hard_max_entry_extension_atr": "hard_max_entry_extension_atr",
        "min_stop_atr_multiple": "min_stop_atr_multiple",
        "fixed_reward_risk": "fixed_reward_risk",
        "forward_horizon_hours": "forward_horizon_hours",
    }
    for key, field_name in paths.items():
        if key not in expected or key not in strategy:
            continue
        try:
            actual = float(strategy[key])
            target = float(expected[key])
        except (TypeError, ValueError):
            raise ValueError(
                f"strategy.{key} is not numeric for contract {variant_id!r}") from None
        if actual != target:
            raise ValueError(
                f"strategy.{key}={actual:g} does not match contract "
                f"{variant_id!r} value {target:g}")
    return contract


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


def runnable_ids() -> tuple[str, ...]:
    return tuple(sorted(
        s.id for s in REGISTRY.values()
        if s.implemented and s.analyst_ready))


def live_eligible_ids() -> tuple[str, ...]:
    return tuple(sorted(
        s.id for s in REGISTRY.values()
        if s.implemented and s.analyst_ready and s.meets(LIVE_MIN_TIER)))


def validate_contract_catalog() -> dict[str, StrategyContract]:
    """Build every composite contract, raising on registry/runtime drift."""
    catalog = contract_catalog()
    for strategy_id, contract in catalog.items():
        if contract.id != strategy_id:
            raise ValueError(
                f"catalog key {strategy_id!r} does not match contract id "
                f"{contract.id!r}")
        if contract.outcome_model.model_id != contract.spec.forward_model_id:
            raise ValueError(
                f"strategy {strategy_id!r} forward model binding drift: "
                f"{contract.outcome_model.model_id!r} != "
                f"{contract.spec.forward_model_id!r}")
    return catalog


def validate_hypothesis_catalog(root: str | Path | None = None) -> tuple[str, ...]:
    """Validate hypothesis YAML identities and declared base semantics.

    Hypothesis files are pre-registration inputs, not executable code.  This
    small check still prevents a renamed strategy, timeframe, or payoff from
    silently describing a different runtime contract.  Settings under a
    hypothesis's ``settings`` list remain experiment values and are not
    compared with the registered base.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover - dependency is shipped in runtime
        return ()
    base = Path(root) if root is not None else (
        Path(__file__).resolve().parents[1] / "research" / "hypotheses")
    errors: list[str] = []
    if not base.exists():
        return ()
    for path in sorted(base.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        strategy_id = str(raw.get("strategy_id") or "")
        if not strategy_id:
            continue
        try:
            contract = contract_for(strategy_id)
        except (KeyError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        if raw.get("version") not in (None, contract.version):
            errors.append(
                f"{path}: version {raw.get('version')!r} does not match "
                f"{contract.version!r}")
        if raw.get("signal_timeframe") not in (
                None, contract.spec.signal_timeframe):
            errors.append(
                f"{path}: signal_timeframe {raw.get('signal_timeframe')!r} "
                f"does not match {contract.spec.signal_timeframe!r}")
        expected = contract.parameters
        for key in ("ls_high_percentile", "ls_low_percentile",
                    "hard_max_entry_extension_atr", "stop_atr_multiple",
                    "reward_risk"):
            if key not in raw:
                continue
            expected_key = {
                "stop_atr_multiple": "min_stop_atr_multiple",
                "reward_risk": "fixed_reward_risk",
            }.get(key, key)
            if expected_key not in expected:
                continue
            try:
                if float(raw[key]) != float(expected[expected_key]):
                    errors.append(
                        f"{path}: {key}={raw[key]!r} does not match "
                        f"contract value {expected[expected_key]!r}")
            except (TypeError, ValueError):
                errors.append(f"{path}: {key} is not numeric")
        tuned_id = raw.get("tuned_variant_id")
        tuned_semantics = raw.get("tuned_semantics")
        if tuned_id or tuned_semantics:
            if tuned_id != TUNED_LS_VARIANT_ID:
                errors.append(f"{path}: unknown tuned_variant_id {tuned_id!r}")
            elif not isinstance(tuned_semantics, Mapping):
                errors.append(f"{path}: tuned_semantics must be a mapping")
            else:
                tuned = contract_for_variant(strategy_id, str(tuned_id))
                for key, value in tuned_semantics.items():
                    if key not in tuned.parameters or float(value) != float(
                            tuned.parameters[key]):
                        errors.append(
                            f"{path}: tuned {key}={value!r} does not match "
                            f"{tuned.parameters.get(key)!r}")
    if errors:
        raise ValueError("hypothesis contract catalog drift: " + "; ".join(errors))
    return tuple(str(path) for path in sorted(base.glob("*.yaml")))


if __name__ == "__main__":  # pragma: no cover - tiny catalog CLI
    import argparse

    parser = argparse.ArgumentParser(description="strategy contract catalog")
    parser.add_argument("--check", action="store_true",
                        help="validate all composite contract bindings")
    parser.add_argument("--json", action="store_true",
                        help="print the canonical manifest")
    args = parser.parse_args()
    validate_contract_catalog()
    validate_hypothesis_catalog()
    if args.json or not args.check:
        print(json.dumps(contract_catalog_manifest(), sort_keys=True,
                         indent=2, default=_json_default))
