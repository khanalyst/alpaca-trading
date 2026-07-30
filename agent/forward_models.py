"""Explicit strategy-specific contracts for turning signals into outcomes.

A raw firing signal is not an expectancy observation until its entry timing,
stop, target, costs and maximum holding period are fixed in advance. Keeping
those assumptions in a named model prevents a new mechanism from silently
inheriting momentum's 15-minute/48-hour fixed-R interpretation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ForwardOutcomeModel:
    model_id: str
    strategy_id: str
    signal_timeframe: str
    horizon_hours: float
    entry_assumption: str
    stop_assumption: str
    target_assumption: str
    cost_assumption: str
    holding_assumption: str
    validated: bool = False
    validation_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.model_id or not self.strategy_id:
            raise ValueError("forward outcome model identity is required")
        if self.horizon_hours <= 0:
            raise ValueError(f"{self.model_id}: horizon_hours must be positive")
        for name in (
                "signal_timeframe", "entry_assumption", "stop_assumption",
                "target_assumption", "cost_assumption", "holding_assumption"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{self.model_id}: {name} is required")
        if self.validated and not self.validation_evidence:
            raise ValueError(
                f"{self.model_id}: a validated model requires evidence")

    def as_dict(self) -> dict:
        return asdict(self)


MODELS: dict[str, ForwardOutcomeModel] = {
    model.model_id: model for model in (
        ForwardOutcomeModel(
            model_id="momentum.fixed_rr.15m.v1",
            strategy_id="momentum",
            signal_timeframe="15m",
            horizon_hours=48.0,
            entry_assumption="first observed snapshot price after decision",
            stop_assumption="deterministic structure/ATR setup-plan stop",
            target_assumption="registered fixed or extended reward/risk target",
            cost_assumption="two taker fees, observed spread, bounded entry and "
                            "stop slippage, adverse funding",
            holding_assumption="first observed stop/target, otherwise 48h timeout",
            validated=True,
            validation_evidence=(
                "research/results/edge-audit-2024-2026/REPORT.md",
                "tests/research/test_no_lookahead.py",
                "tests/research/test_cost_parity.py",
            ),
        ),
        ForwardOutcomeModel(
            model_id="flush_fade.reversion.15m.v1",
            strategy_id="flush-fade", signal_timeframe="15m",
            horizon_hours=24.0,
            entry_assumption="next observable post-signal price",
            stop_assumption="flush structure plus ATR buffer",
            target_assumption="pre-registered reversion destination",
            cost_assumption="taker round trip, spread, slippage and funding",
            holding_assumption="stop/target first, otherwise 24h timeout",
        ),
        ForwardOutcomeModel(
            model_id="funding_carry.interval.1h.v1",
            strategy_id="funding-carry", signal_timeframe="1h",
            horizon_hours=168.0,
            entry_assumption="next observable price before funding eligibility",
            stop_assumption="price-risk and basis-widening invalidation",
            target_assumption="funding capture plus basis-normalisation target",
            cost_assumption="maker/taker execution, funding receipts and borrow",
            holding_assumption="funding schedule aware, capped at seven days",
        ),
        ForwardOutcomeModel(
            model_id="funding_unwind.reversion.1h.v1",
            strategy_id="funding-unwind", signal_timeframe="1h",
            horizon_hours=72.0,
            entry_assumption="next observable post-crowding snapshot",
            stop_assumption="crowding/price thesis invalidation",
            target_assumption="funding and basis normalisation",
            cost_assumption="taker round trip, spread, slippage and funding",
            holding_assumption="stop/target first, otherwise 72h timeout",
        ),
        ForwardOutcomeModel(
            model_id="trend_multiday.4h.v1",
            strategy_id="trend-multiday", signal_timeframe="4h",
            horizon_hours=336.0,
            entry_assumption="next observable completed-4h-bar price",
            stop_assumption="multi-day structure/ATR invalidation",
            target_assumption="pre-registered trailing or fixed-R exit",
            cost_assumption="taker round trip, spread, slippage and all funding",
            holding_assumption="bar-close stop/target/trail, capped at 14 days",
        ),
        ForwardOutcomeModel(
            model_id="ls_ratio_fade.1h.v1",
            strategy_id="ls-ratio-fade", signal_timeframe="1h",
            horizon_hours=72.0,
            entry_assumption="next observable post-ratio snapshot",
            stop_assumption="positioning thesis and ATR invalidation",
            target_assumption="pre-registered 16-48h positioning reversion",
            cost_assumption="taker round trip, spread, slippage and funding",
            holding_assumption="fixed horizon selected before evaluation",
        ),
        ForwardOutcomeModel(
            model_id="scalp_maker.book.1m.v1",
            strategy_id="scalp-maker", signal_timeframe="1m",
            horizon_hours=4.0,
            entry_assumption="recorded queue-aware post-only fill only",
            stop_assumption="recorded adverse-selection/book invalidation",
            target_assumption="recorded spread capture and inventory exit",
            cost_assumption="maker fees/rebates plus measured queue slippage",
            holding_assumption="recorded fill-to-exit lifecycle, capped at 4h",
        ),
    )
}


BY_STRATEGY = {model.strategy_id: model for model in MODELS.values()}


def model_for(strategy_id: str) -> ForwardOutcomeModel:
    try:
        return BY_STRATEGY[strategy_id]
    except KeyError:
        raise KeyError(
            f"no forward outcome model is registered for {strategy_id!r}") from None


def require_validated(strategy_id: str) -> ForwardOutcomeModel:
    model = model_for(strategy_id)
    if not model.validated:
        raise ValueError(
            f"{strategy_id}: forward outcome model {model.model_id} is not "
            "validated; expectancy and promotion are prohibited")
    return model
