"""Per-strategy evidence contracts.

A contract answers one question for one strategy: given a symbol snapshot,
which setups are valid right now, in which direction, and how extended is the
entry. It is the deterministic half of the decision - the LLM may only choose
among setups a contract has already declared valid, and may never invent one.

Each strategy owns a module here and registers its builder in
``EVIDENCE_BUILDERS``. ``agent.strategy.setup_evidence`` dispatches on the
active ``strategy.id``, so adding a strategy means adding a module and a
registry entry rather than editing shared code.

The helpers below are shared because every contract needs them and because a
second copy of a numeric guard is a second chance to get it wrong.
"""

from __future__ import annotations

import math


def finite(value, default: float | None = None) -> float | None:
    """Return ``value`` as a float, or ``default`` if it is not finite.

    Snapshot fields arrive from an exchange and may be ``None``, a string, or
    NaN. Comparisons against NaN are silently false, which would turn a
    missing measurement into a passed check, so nothing numeric enters a
    contract without going through here first.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


# Populated by the contract modules at import time. Keyed by strategy id.
EVIDENCE_BUILDERS: dict = {}


def register(strategy_id: str, builder) -> None:
    if strategy_id in EVIDENCE_BUILDERS:
        raise ValueError(f"contract for {strategy_id!r} is already registered")
    EVIDENCE_BUILDERS[strategy_id] = builder


from . import (flush_fade, funding_carry,  # noqa: E402,F401
               funding_unwind, ls_ratio_fade,
               momentum_phase1v2, scalp_maker,
               trend_multiday)                            # (register on import)
