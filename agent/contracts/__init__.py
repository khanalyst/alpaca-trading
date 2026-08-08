"""Deterministic trading contracts.

The order path has one alpha family: the initial-breakout-range (IBR)
contract.  Shares and single-leg long options are execution profiles of the
same underlying signal; they are deliberately not registered as separate
strategies.
"""

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


__all__ = [
    "EVIDENCE_BUILDERS", "IBRConfig", "build_ibr_range",
    "evaluate_ibr_breakout", "evaluate_exit", "generate_ibr_signal",
    "setup_evidence",
]
