"""Executable forward contracts for turning signals into paper outcomes.

A firing contract is not an expectancy observation until proposal identity,
entry, stop, target, costs, funding and maximum holding time are fixed before
the outcome is observed.  These models are implementation-validated only;
they do not raise a strategy's evidence tier or authorise exchange trading.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _field(row: dict, path: str):
    value = row
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


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
    required_fields: tuple[str, ...]
    signal_timestamp_field: str
    invalidation_anchor: str
    exit_policy: str
    stop_atr_multiple: float
    reward_risk: float
    entry_style: str = "observed_taker"
    maker_entry_fee_pct: float | None = None
    charge_spread: bool = True
    entry_slippage_pct: float | None = None
    stop_slippage_pct: float | None = None
    funding_treatment: str = "entry_rate_actual_schedule"
    validated: bool = False
    validation_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.model_id or not self.strategy_id:
            raise ValueError("forward outcome model identity is required")
        if self.horizon_hours <= 0:
            raise ValueError(f"{self.model_id}: horizon_hours must be positive")
        if self.stop_atr_multiple <= 0 or self.reward_risk <= 0:
            raise ValueError(
                f"{self.model_id}: stop and reward/risk must be positive")
        if self.entry_style not in {"observed_taker", "top_book_maker"}:
            raise ValueError(f"{self.model_id}: unknown entry_style")
        if self.invalidation_anchor not in {"structure", "atr"}:
            raise ValueError(f"{self.model_id}: unknown invalidation anchor")
        for name in (
                "signal_timeframe", "entry_assumption", "stop_assumption",
                "target_assumption", "cost_assumption", "holding_assumption",
                "signal_timestamp_field", "funding_treatment"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{self.model_id}: {name} is required")
        if not self.required_fields:
            raise ValueError(f"{self.model_id}: required_fields are required")
        if self.entry_style == "top_book_maker" and (
                self.maker_entry_fee_pct is None
                or self.maker_entry_fee_pct < 0):
            raise ValueError(
                f"{self.model_id}: maker entry fee assumption is required")
        if self.validated and not self.validation_evidence:
            raise ValueError(
                f"{self.model_id}: a validated model requires evidence")

    def as_dict(self) -> dict:
        return asdict(self)

    def horizon_for(self, cfg: dict) -> float:
        value = _finite(cfg["strategy"].get("forward_horizon_hours"))
        return value if value is not None and value > 0 else self.horizon_hours

    def missing_fields(self, row: dict) -> tuple[str, ...]:
        missing = []
        for path in self.required_fields:
            value = _field(row, path)
            if value is None:
                missing.append(path)
                continue
            if isinstance(value, (int, float)) and _finite(value) is None:
                missing.append(path)
        return tuple(missing)

    def deterministic_proposals(
            self, snapshot: dict, cfg: dict) -> list[dict]:
        """Emit comparable probes; each evaluator applies its own contract.

        Both directions are proposed deliberately.  A baseline and candidate
        therefore see the same proposal identity even when one threshold
        fires and the other vetoes, which is the comparable decision unit
        used by durable rotation.
        """
        from .registry import spec_for

        spec = spec_for(self.strategy_id)
        out = []
        for symbol in sorted(snapshot):
            if symbol.startswith("_") or not isinstance(snapshot[symbol], dict):
                continue
            row = snapshot[symbol]
            missing = self.missing_fields(row)
            signal_ts = _finite(_field(row, self.signal_timestamp_field))
            for setup_type in spec.setup_types:
                for direction in ("long", "short"):
                    proposal = {
                        "action": "open",
                        "symbol": symbol,
                        "direction": direction,
                        "setup_type": setup_type,
                        "confidence": 1.0,
                        "invalidation_anchor": self.invalidation_anchor,
                        "exit_policy": self.exit_policy,
                        "execution_choice": "normal",
                        "proposal_source": "deterministic_contract",
                        "strategy_id": self.strategy_id,
                        "model_id": self.model_id,
                        "signal_ts": signal_ts,
                    }
                    if missing:
                        proposal["research_refusal_reason"] = (
                            "data missing: " + ", ".join(missing))
                    out.append(proposal)
        return out

    def entry_price(self, row: dict, direction: str) -> float | None:
        if self.entry_style == "observed_taker":
            value = _finite(row.get("price"))
        else:
            key = "book_best_bid" if direction == "long" else "book_best_ask"
            value = _finite(_field(row, f"_enrichment.{key}"))
        return value if value is not None and value > 0 else None

    def risk_row(self, row: dict, direction: str) -> dict:
        """Return the local sizing view without mutating the common feed."""
        out = dict(row)
        entry = self.entry_price(row, direction)
        if entry is not None:
            out["price"] = entry
        if self.entry_style == "top_book_maker":
            # A passive entry joins the book, so it does not cross the spread
            # or consume the IOC slippage budget.  The exit remains taker.
            exit_fee = _finite(row.get("taker_fee_pct_per_side"))
            if exit_fee is not None:
                out["taker_fee_pct_per_side"] = (
                    float(self.maker_entry_fee_pct) + exit_fee) / 2.0
            out["spread_pct"] = 0.0
        return out

    def cost_components(self, row: dict, sized: dict, cfg: dict) -> dict:
        exit_fee = _finite(row.get("taker_fee_pct_per_side"))
        if exit_fee is None:
            exit_fee = float(cfg["trading_costs"]["taker_fee_pct_per_side"])
        if self.entry_style == "top_book_maker":
            entry_fee = float(self.maker_entry_fee_pct)
            spread = 0.0
            entry_slippage = 0.0
        else:
            entry_fee = exit_fee
            spread = (max(0.0, _finite(row.get("spread_pct")) or 0.0)
                      if self.charge_spread else 0.0)
            entry_slippage = (
                float(self.entry_slippage_pct)
                if self.entry_slippage_pct is not None
                else float(cfg["execution"]["max_order_book_slippage_pct"]))
        stop_slippage = (
            float(self.stop_slippage_pct)
            if self.stop_slippage_pct is not None
            else float(cfg["trading_costs"]["expected_stop_slippage_pct"]))
        return {
            "entry_fee_pct": entry_fee,
            "exit_fee_pct": exit_fee,
            "spread_pct": spread,
            "entry_slippage_pct": entry_slippage,
            "stop_slippage_pct": stop_slippage,
            "funding_rate_pct": _finite(row.get("funding_rate_pct")),
            "funding_interval_hours": _finite(
                row.get("funding_interval_hours")),
            "next_funding_minutes": _finite(row.get("next_funding_minutes")),
            "funding_treatment": self.funding_treatment,
            "sizing_estimated_cost_pct": float(
                sized.get("estimated_cost_pct") or 0.0),
        }


_COMMON_COST_FIELDS = (
    "price", "spread_pct", "funding_rate_pct", "funding_interval_hours",
    "next_funding_minutes", "taker_fee_pct_per_side", "atr_1h_pct",
    "ema20_1h_dist_pct",
)


MODELS: dict[str, ForwardOutcomeModel] = {
    model.model_id: model for model in (
        ForwardOutcomeModel(
            model_id="momentum.fixed_rr.15m.v1",
            strategy_id="momentum", signal_timeframe="15m",
            horizon_hours=48.0,
            entry_assumption="first observed snapshot price after decision",
            stop_assumption="structure stop with a one-ATR minimum",
            target_assumption="registered fixed reward/risk target",
            cost_assumption="observed taker fees and spread, bounded entry and "
                            "stop slippage, entry-rate funding settlements",
            holding_assumption="stop/target first, otherwise 48h timeout",
            required_fields=_COMMON_COST_FIELDS + (
                "signal_ts", "swing_low_pct", "swing_high_pct"),
            signal_timestamp_field="signal_ts", invalidation_anchor="structure",
            exit_policy="fixed_rr", stop_atr_multiple=1.0, reward_risk=3.0,
            validated=True,
            validation_evidence=(
                "research/results/edge-audit-2024-2026/REPORT.md",
                "tests/research/test_forward_models.py",
                "tests/research/test_cost_parity.py",
            ),
        ),
        ForwardOutcomeModel(
            model_id="flush_fade.reversion.15m.v1",
            strategy_id="flush-fade", signal_timeframe="15m",
            horizon_hours=24.0,
            entry_assumption="first observed post-signal snapshot price",
            stop_assumption="structure distance with a 1.5 ATR minimum",
            target_assumption="fixed 2R reversion target",
            cost_assumption="observed taker fees and spread, bounded entry and "
                            "stop slippage, entry-rate funding settlements",
            holding_assumption="stop/target first, otherwise 24h timeout",
            required_fields=_COMMON_COST_FIELDS + (
                "signal_ts", "oi_change_4h_pct", "relative_volume_1h",
                "mom_1h_pct", "swing_low_pct", "swing_high_pct"),
            signal_timestamp_field="signal_ts", invalidation_anchor="structure",
            exit_policy="fixed_rr", stop_atr_multiple=1.5, reward_risk=2.0,
            validated=True,
            validation_evidence=(
                "research/hypotheses/flush-fade.yaml",
                "tests/research/test_forward_models.py",
            ),
        ),
        ForwardOutcomeModel(
            model_id="funding_carry.interval.1h.v1",
            strategy_id="funding-carry", signal_timeframe="1h",
            horizon_hours=240.0,
            entry_assumption="first observed snapshot after the 1h signal",
            stop_assumption="structure distance with a 3 ATR minimum",
            target_assumption="fixed 2R price exit while funding is credited",
            cost_assumption="perpetual only: observed taker costs and actual "
                            "entry-rate funding schedule; no invented borrow",
            holding_assumption="stop/target first, otherwise ten-day timeout",
            required_fields=_COMMON_COST_FIELDS + (
                "signal_1h_ts", "funding_percentile_30",
                "funding_samples_30", "perp_index_basis_pct", "trend_1h",
                "trend_4h", "swing_low_pct", "swing_high_pct"),
            signal_timestamp_field="signal_1h_ts",
            invalidation_anchor="structure", exit_policy="fixed_rr",
            stop_atr_multiple=3.0, reward_risk=2.0, validated=True,
            validation_evidence=(
                "research/hypotheses/funding-carry.yaml",
                "tests/research/test_forward_models.py",
            ),
        ),
        ForwardOutcomeModel(
            model_id="funding_unwind.reversion.1h.v1",
            strategy_id="funding-unwind", signal_timeframe="1h",
            horizon_hours=240.0,
            entry_assumption="first observed snapshot after the 1h crowding signal",
            stop_assumption="structure distance with a 3 ATR minimum",
            target_assumption="fixed 2R positioning-unwind target",
            cost_assumption="observed taker costs plus actual entry-rate "
                            "funding settlements, including credits",
            holding_assumption="stop/target first, otherwise ten-day timeout",
            required_fields=_COMMON_COST_FIELDS + (
                "signal_1h_ts", "funding_percentile_30",
                "funding_samples_30", "trend_1h", "trend_4h",
                "swing_low_pct", "swing_high_pct"),
            signal_timestamp_field="signal_1h_ts",
            invalidation_anchor="structure", exit_policy="fixed_rr",
            stop_atr_multiple=3.0, reward_risk=2.0, validated=True,
            validation_evidence=(
                "research/hypotheses/funding-unwind.yaml",
                "tests/research/test_forward_models.py",
            ),
        ),
        ForwardOutcomeModel(
            model_id="trend_multiday.4h.v1",
            strategy_id="trend-multiday", signal_timeframe="4h",
            horizon_hours=336.0,
            entry_assumption="first observed snapshot after the completed 4h bar",
            stop_assumption="structure distance with a 2 ATR minimum",
            target_assumption="fixed 3R continuation target",
            cost_assumption="observed taker costs and all entry-rate funding "
                            "settlements over the multi-day hold",
            holding_assumption="stop/target first, otherwise 14-day timeout",
            required_fields=_COMMON_COST_FIELDS + (
                "signal_4h_ts", "trend_1h", "trend_4h", "mom_1h_pct",
                "range_pos_pct", "atr_1h_ratio", "swing_low_pct",
                "swing_high_pct"),
            signal_timestamp_field="signal_4h_ts",
            invalidation_anchor="structure", exit_policy="fixed_rr",
            stop_atr_multiple=2.0, reward_risk=3.0, validated=True,
            validation_evidence=(
                "research/hypotheses/trend-multiday.yaml",
                "tests/research/test_forward_models.py",
            ),
        ),
        ForwardOutcomeModel(
            model_id="ls_ratio_fade.1h.v1",
            strategy_id="ls-ratio-fade", signal_timeframe="1h",
            horizon_hours=48.0,
            entry_assumption="first observed snapshot after the 1h ratio signal",
            stop_assumption="structure distance with a 2 ATR minimum",
            target_assumption="fixed 2R positioning-reversion target",
            cost_assumption="observed taker costs plus actual entry-rate "
                            "funding settlements",
            holding_assumption="stop/target first, otherwise 48h timeout",
            required_fields=_COMMON_COST_FIELDS + (
                "signal_1h_ts", "long_short_ratio",
                "long_short_percentile_30", "trend_1h", "swing_low_pct",
                "swing_high_pct"),
            signal_timestamp_field="signal_1h_ts",
            invalidation_anchor="structure", exit_policy="fixed_rr",
            stop_atr_multiple=2.0, reward_risk=2.0, validated=True,
            validation_evidence=(
                "research/hypotheses/ls-ratio-fade.yaml",
                "tests/research/test_forward_models.py",
            ),
        ),
        ForwardOutcomeModel(
            model_id="scalp_maker.book.1m.v1",
            strategy_id="scalp-maker", signal_timeframe="1m",
            horizon_hours=4.0,
            entry_assumption="join best bid for long or best ask for short",
            stop_assumption="ATR stop with a 0.5 ATR minimum",
            target_assumption="fixed 1R inventory exit",
            cost_assumption="0.02% configured maker entry fee from the maker "
                            "study, observed taker exit fee, no crossed spread "
                            "or entry slippage, stop slippage and funding",
            holding_assumption="stop/target first, otherwise four-hour timeout",
            required_fields=_COMMON_COST_FIELDS + (
                "_enrichment.book_ts", "_enrichment.book_best_bid",
                "_enrichment.book_best_ask", "_enrichment.book_spread_pct",
                "_enrichment.book_bid_depth_usd",
                "_enrichment.book_ask_depth_usd",
                "_enrichment.book_top_bid_size",
                "_enrichment.book_top_ask_size"),
            signal_timestamp_field="_enrichment.book_ts",
            invalidation_anchor="atr", exit_policy="fixed_rr",
            stop_atr_multiple=0.5, reward_risk=1.0,
            entry_style="top_book_maker", maker_entry_fee_pct=0.02,
            charge_spread=False, entry_slippage_pct=0.0,
            stop_slippage_pct=0.15, validated=True,
            validation_evidence=(
                "research/maker_study.py",
                "research/hypotheses/scalp-maker.yaml",
                "tests/research/test_forward_models.py",
            ),
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
