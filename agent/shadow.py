"""Parameter variants evaluated on live snapshots, unable to trade by design.

Replay ranks candidates on data already collected; shadow filters them on
data arriving now, with live spread and depth observed rather than assumed.
Between them they answer intentions #2 and #3 without a second account, a
second process, or a second LLM bill: every variant reads the same snapshot
in the same cycle as the trading agent, so their evidence is comparable by
construction rather than by argument.

**The isolation is a type boundary, not a discipline.** ``ShadowEvaluator``
is constructed with configuration and nothing else. It receives no
``Exchange``, no state handle, no journal writer - it takes dicts and returns
records. A test walks its attribute graph and asserts no exchange is
reachable from it, because "we were careful" is not a guarantee that survives
the next person to edit the file.

**It runs deterministic variants only, by default.** An LLM-driven variant
costs a full extra call per cycle - 288 a day, $50-95 a month, each. Ten of
them is $500-950 a month. Every parameter axis in the sweep list costs
approximately nothing, and covers intention #4 completely.

**It cannot slow the loop.** The evaluator enforces a wall-clock budget and
abandons the remaining variants when it is spent, and the engine calls it
after trading decisions are committed, inside a try/except. A shadow failure
is journalled and swallowed. Research must never be able to stop trading.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import strategy
from .risk import RiskEngine
from .variants import Variant, apply


@dataclass
class ShadowRecord:
    """What one variant would have done with one symbol, this cycle."""

    variant_id: str
    symbol: str
    signal_ts: int | None
    outcome: str                  # proposed | vetoed | no_setup
    direction: str | None = None
    setup_type: str | None = None
    reason: str | None = None
    stop_pct: float | None = None
    take_pct: float | None = None
    notional: float | None = None

    def as_event(self) -> dict:
        return {
            "variant_id": self.variant_id, "symbol": self.symbol,
            "signal_ts": self.signal_ts, "outcome": self.outcome,
            "direction": self.direction, "setup_type": self.setup_type,
            "reason": self.reason, "stop_pct": self.stop_pct,
            "take_pct": self.take_pct, "notional": self.notional,
        }


@dataclass
class ShadowBudget:
    """Wall-clock guard. Overrun abandons the rest of the cycle's variants."""

    limit_ms: float
    started: float = field(default_factory=time.monotonic)
    overran: bool = False

    def spent_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000.0

    def exhausted(self) -> bool:
        if self.limit_ms <= 0:
            return False
        if self.spent_ms() >= self.limit_ms:
            self.overran = True
            return True
        return False


class ShadowEvaluator:
    """Evaluates parameter variants against a live snapshot. Cannot trade.

    Constructed from configuration alone. There is deliberately no parameter
    through which an exchange client, a state handle or a journal connection
    could be passed, so the isolation cannot be undone by a caller.
    """

    def __init__(self, variants: list, base_cfg: dict,
                 budget_ms: float = 500.0) -> None:
        self.budget_ms = float(budget_ms)
        self._configs: dict = {}
        self._engines: dict = {}
        self.registration_errors: dict = {}

        for variant in variants:
            if not isinstance(variant, Variant):
                raise TypeError(
                    "shadow variants must be Variant instances; got "
                    f"{type(variant).__name__}")
            try:
                cfg = apply(variant, base_cfg)
            except Exception as exc:                       # noqa: BLE001
                # A variant that cannot be configured is recorded and
                # skipped rather than raising: one bad registry entry must
                # not disable shadow evaluation for every other variant.
                self.registration_errors[variant.variant_id] = str(exc)
                continue
            self._configs[variant.variant_id] = cfg
            self._engines[variant.variant_id] = RiskEngine(cfg)

    @property
    def variant_ids(self) -> list:
        return sorted(self._configs)

    def evaluate(self, snapshot: dict, portfolio: dict,
                 now: float) -> list:
        """Return records for every variant, or as many as the budget allows.

        Never raises. A variant that throws is recorded as an error and the
        rest continue, because a research feature that could interrupt a
        trading cycle is a safety regression however useful its output.
        """
        budget = ShadowBudget(self.budget_ms)
        records: list = []
        symbols = [s for s, v in (snapshot or {}).items()
                   if not s.startswith("_") and isinstance(v, dict)]

        for variant_id in self.variant_ids:
            if budget.exhausted():
                break
            cfg = self._configs[variant_id]
            engine = self._engines[variant_id]
            for symbol in symbols:
                if budget.exhausted():
                    break
                try:
                    records.append(self._evaluate_one(
                        variant_id, cfg, engine, snapshot, symbol,
                        portfolio, now))
                except Exception as exc:                   # noqa: BLE001
                    records.append(ShadowRecord(
                        variant_id=variant_id, symbol=symbol,
                        signal_ts=None, outcome="vetoed",
                        reason=f"shadow error: {type(exc).__name__}: {exc}"))

        self.last_budget = budget
        return records

    def _evaluate_one(self, variant_id: str, cfg: dict, engine: RiskEngine,
                      snapshot: dict, symbol: str, portfolio: dict,
                      now: float) -> ShadowRecord:
        row = snapshot[symbol]
        signal_ts = row.get("signal_ts")
        evidence = row.get("setup_evidence")
        if not isinstance(evidence, dict):
            evidence = strategy.setup_evidence(row, cfg)

        for setup_type, contract in sorted(evidence.items()):
            if not isinstance(contract, dict):
                continue
            for direction in ("long", "short"):
                if contract.get(direction) is not True:
                    continue
                decision = {
                    "symbol": symbol, "action": "open",
                    "direction": direction, "setup_type": setup_type,
                    "confidence": 1.0,
                    "invalidation_anchor": "structure",
                    "exit_policy": "fixed_rr",
                }
                plan, why = strategy.build_setup_plan(decision, row, cfg)
                if plan is None:
                    return ShadowRecord(
                        variant_id, symbol, signal_ts, "vetoed",
                        direction, setup_type, reason=why)

                merged = dict(decision, **{
                    "stop_loss_pct": plan.get("stop_loss_pct"),
                    "take_profit_pct": plan.get("take_profit_pct")})
                sized, veto = engine.vet_open(
                    merged,
                    float(portfolio.get("equity_usdt") or 0.0),
                    list(portfolio.get("positions") or []),
                    snapshot, dict(portfolio.get("cooldowns") or {}),
                    float(portfolio.get("gross_notional") or 0.0),
                    active_trades=dict(portfolio.get("active_trades") or {}),
                    now=now)
                if sized is None:
                    return ShadowRecord(
                        variant_id, symbol, signal_ts, "vetoed",
                        direction, setup_type, reason=veto,
                        stop_pct=plan.get("stop_loss_pct"),
                        take_pct=plan.get("take_profit_pct"))
                return ShadowRecord(
                    variant_id, symbol, signal_ts, "proposed",
                    direction, setup_type,
                    stop_pct=sized.get("sl_pct"),
                    take_pct=sized.get("tp_pct"),
                    notional=sized.get("notional"))

        return ShadowRecord(variant_id, symbol, signal_ts, "no_setup")


def build(cfg: dict, registry: dict) -> ShadowEvaluator | None:
    """Construct an evaluator from the ``research:`` block, or return None.

    An absent block, a disabled flag, or an empty variant list are all a
    complete no-op: no evaluator, no events, no cost.
    """
    block = (cfg.get("research") or {})
    if not block.get("shadow_enabled"):
        return None
    names = list(block.get("shadow_variants") or [])
    if not names:
        return None
    chosen = [registry[name] for name in names if name in registry]
    if not chosen:
        return None
    return ShadowEvaluator(
        chosen, cfg, budget_ms=float(block.get("shadow_budget_ms") or 500))
