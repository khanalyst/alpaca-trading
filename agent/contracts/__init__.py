"""Deterministic trading contracts used by research and paper execution."""

from __future__ import annotations

import math


def finite(value, default=None):
    """Return a finite float or *default* (used by protocol boundaries)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


EVIDENCE_BUILDERS: dict[str, object] = {}


def register(strategy_id: str, builder) -> None:
    if strategy_id in EVIDENCE_BUILDERS:
        raise ValueError(f"contract for {strategy_id!r} is already registered")
    EVIDENCE_BUILDERS[strategy_id] = builder


from .ibr import (  # noqa: E402,F401
    IBRConfig,
    build_ibr_range,
    evaluate_exit,
    evaluate_ibr_breakout,
    generate_ibr_signal,
    setup_evidence,
)
from .rule import (  # noqa: E402,F401
    RULE_FAMILIES,
    evaluate_rule_signal,
    generate_rule_signal,
    rule_variant_id,
    validate_rule_spec,
)


__all__ = [
    "EVIDENCE_BUILDERS", "IBRConfig", "build_ibr_range",
    "evaluate_ibr_breakout", "evaluate_exit", "generate_ibr_signal",
    "setup_evidence", "RULE_FAMILIES", "evaluate_rule_signal",
    "generate_rule_signal", "rule_variant_id", "validate_rule_spec",
]
