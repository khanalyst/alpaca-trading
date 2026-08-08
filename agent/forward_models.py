"""Forward outcome metadata for the single IBR alpha family.

The model is a contract description, not an independent source of signals.
Shares and options wrappers use the same underlying stop, target, and
force-flat metadata produced by :mod:`agent.contracts.ibr`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math


def _finite(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _field(row: dict, path: str):
    value = row
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


@dataclass(frozen=True)
class ForwardOutcomeModel:
    model_id: str = "ibr.v1"
    strategy_id: str = "ibr"
    signal_timeframe: str = "1m"
    horizon_hours: float = 6.5
    entry_assumption: str = "next completed one-minute close"
    stop_assumption: str = "opposite IBR range boundary"
    target_assumption: str = "fixed target_R from entry to stop"
    cost_assumption: str = "observed execution costs"
    holding_assumption: str = "force flat before regular-session close"
    required_fields: tuple[str, ...] = ("signal_ts", "price")
    signal_timestamp_field: str = "signal_ts"
    invalidation_anchor: str = "structure"
    exit_policy: str = "fixed_rr"
    stop_atr_multiple: float = 1.0
    reward_risk: float = 2.0
    entry_style: str = "observed_taker"
    maker_entry_fee_pct: float | None = None
    charge_spread: bool = True
    entry_slippage_pct: float | None = None
    stop_slippage_pct: float | None = None
    contract_complete: bool = True
    contract_evidence: tuple[str, ...] = ("agent/contracts/ibr.py",)

    def __post_init__(self):
        if self.horizon_hours <= 0 or self.stop_atr_multiple <= 0 or self.reward_risk <= 0:
            raise ValueError("forward model horizon, stop, and target must be positive")
        if not self.required_fields:
            raise ValueError("forward model required_fields are required")

    def as_dict(self) -> dict:
        return asdict(self)

    def horizon_for(self, cfg: dict) -> float:
        value = _finite((cfg.get("strategy") or {}).get("forward_horizon_hours"))
        return value if value is not None and value > 0 else self.horizon_hours

    def missing_fields(self, row: dict) -> tuple[str, ...]:
        missing = []
        for path in self.required_fields:
            value = _field(row, path)
            if value is None or (isinstance(value, (int, float)) and _finite(value) is None):
                missing.append(path)
        return tuple(missing)

    def deterministic_proposals(self, snapshot: dict, cfg: dict) -> list[dict]:
        """Emit both direction probes for replay comparability."""
        out = []
        for symbol in sorted(snapshot or {}):
            if str(symbol).startswith("_") or not isinstance(snapshot[symbol], dict):
                continue
            row = snapshot[symbol]
            if self.missing_fields(row):
                continue
            for direction in ("long", "short"):
                out.append({
                    "action": "open", "symbol": symbol,
                    "direction": direction, "setup_type": "ibr_breakout",
                    "signal_ts": _finite(row.get(self.signal_timestamp_field)),
                    "confidence": 1.0,
                })
        return out


IBR_MODEL = ForwardOutcomeModel()
MODELS: dict[str, ForwardOutcomeModel] = {"ibr": IBR_MODEL}
BY_STRATEGY = MODELS

def model_for(strategy_id: str) -> ForwardOutcomeModel:
    try:
        return MODELS[str(strategy_id)]
    except KeyError:
        raise KeyError(f"unknown forward model strategy {strategy_id!r}") from None


def require_complete_contract(strategy_id: str) -> ForwardOutcomeModel:
    model = model_for(strategy_id)
    if not model.contract_complete:
        raise ValueError(f"forward model {model.model_id!r} is incomplete")
    return model


def completed_signal_timestamp(row: dict, field: str = "signal_ts") -> float | None:
    """Return a finite completed timestamp, or ``None`` for lookahead safety."""
    value = _finite(_field(row, field))
    return value if value is not None and value >= 0 else None
