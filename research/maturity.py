"""Causal history requirements shared by fit diagnostics and signal quality.

The executable rule evaluator has a small set of mandatory dependencies that
must be present before a predicate can be tested.  Keeping that calculation in
one module prevents direct scans and fit-prefix scans from silently using
different maturity boundaries.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.contracts.rule import causal_maturity_bars as _rule_causal_maturity_bars


def causal_maturity_bars(spec: Mapping[str, Any]) -> int:
    """Return the executable rule's exact causal maturity boundary."""
    return _rule_causal_maturity_bars(spec)


__all__ = ["causal_maturity_bars"]
