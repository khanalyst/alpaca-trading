"""Re-derive what any variant would have decided, on the data the agent saw.

The whole harness rests on one property of the codebase: ``setup_evidence``,
``build_setup_plan`` and ``vet_open`` are pure functions of
``(snapshot, cfg)``, and the snapshot they consumed is journalled verbatim on
every cycle. So for any candidate configuration, what the strategy contract
and risk engine would have produced is computable exactly, offline.

**This module calls the production functions. It never reimplements them.**
A reimplemented strategy tests the reimplementation, and the two would drift
within a month - at which point every number here would describe a system
nobody runs. Where behaviour is needed, it is imported.

Three proposer modes, and the distinction between them is the point:

``deterministic``
    The null model. The contract fired, so take it: direction by trend
    majority, fixed reward/risk, confidence 1.0. This is the floor the
    expensive component has to beat.

``recorded_llm``
    The decisions the live model actually made, replayed against a different
    configuration. Isolates the effect of *parameter* changes with model
    behaviour held fixed.

``deterministic_vetoed``
    The contract proposes and the recorded model decision is consulted only
    to *suppress*. This arm exists because the two-arm framing cannot detect
    the most plausible truth. A language model is poorly suited to selecting
    setups from numeric snapshots - deterministic code does that better and
    more cheaply - but well suited to rejecting setups that satisfy the
    contract and sit in an obviously bad context. If the model is a good
    vetoer and a poor selector, a pooled LLM arm looks mediocre and the
    conclusion drawn is "the LLM does not earn its keep", which is the wrong
    conclusion from the right data.

The third arm costs nothing extra: it is a different join over event streams
already recorded.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent import state, strategy
from agent.forward_models import require_complete_contract
from agent.risk import RiskEngine

from . import corpus
from .evidence_primitives import canonical_opportunity_key
from .outcomes import CostModel, SetupPlan, resolve_from_cache


MODES = ("deterministic", "recorded_llm", "deterministic_vetoed")


def fidelity_code_fingerprint() -> str:
    """Hash every source file that can change a G2 replay conclusion."""
    root = Path(__file__).resolve().parents[1]
    files = list((root / "agent").rglob("*.py"))
    files += [root / "main.py", root / "research.py",
              root / "research" / "corpus.py",
              root / "research" / "outcomes.py",
              root / "research" / "replay.py"]
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


@dataclass
class ReplayDecision:
    """One evaluated symbol-cycle, whatever became of it."""

    cycle_id: str | None
    ts: float
    symbol: str
    signal_ts: int | None
    stage: str                  # fired | proposed | vetoed | executed
    direction: str | None = None
    setup_type: str | None = None
    confidence: float | None = None
    reason: str | None = None
    stop_pct: float | None = None
    take_pct: float | None = None
    notional: float | None = None
    outcome: dict | None = None
    # Stable cross-variant identity used by real-time shadow portfolios. Replay
    # records created from older journals fall back to their signal identity.
    proposal_id: str | None = None
    # True once the strategy contract has accepted the setup, whether or not
    # the risk engine later refused it. This is the stage the live engine
    # journals `setup_proposed` at - before vet_open, not after - so gate G2
    # must compare against this rather than against executions.
    contract_passed: bool = False
    # Enrichment travels with the decision for conditioning, but is never
    # shown to the model.
    enrichment: dict = field(default_factory=dict)

    # The following are populated for current journals/replays.  They remain
    # optional so older ``ReplayDecision`` fixtures and protocol rows can be
    # read without inventing an identity they never recorded.
    strategy_id: str | None = None
    strategy_version: str | None = None
    setup_key: str | None = None

    def key(self) -> tuple:
        return (self.cycle_id, self.symbol, self.direction, self.setup_type)

    def proposal_key(self) -> tuple:
        """Identity shared across arms, independent of replay execution."""
        return canonical_opportunity_key(self)


@dataclass
class ReplayResult:
    variant_id: str
    mode: str
    decisions: list = field(default_factory=list)
    funnel: dict = field(default_factory=dict)
    cycles: int = 0
    corpus_from_ts: float = 0.0
    corpus_to_ts: float = 0.0
    state_diagnostics: dict = field(default_factory=dict)
    strategy_id: str | None = None
    strategy_version: str | None = None
    # G2 callers attach the authoritative journal fingerprint captured before
    # this replay started.  It is intentionally excluded from ``digest``.
    g2_metadata: dict | None = None

    def executed(self) -> list:
        return [d for d in self.decisions if d.stage == "executed"]

    def digest(self) -> str:
        """A stable hash of the run, for the determinism assertion."""
        import hashlib
        payload = json.dumps(
            [[d.cycle_id, d.symbol, d.stage, d.direction, d.setup_type,
              d.proposal_id, d.setup_key, d.strategy_id, d.strategy_version,
              d.reason, d.stop_pct, d.take_pct] for d in self.decisions],
            sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def trend_majority(row: dict) -> str | None:
    """Direction by majority vote of the three trend labels.

    The null model needs a direction rule that uses no judgement at all,
    because the point of the null is to hold everything except the LLM
    constant. Ties produce no proposal rather than a coin flip: a coin flip
    would add variance the LLM arm does not have.
    """
    votes = [row.get(f"trend_{tf}") for tf in ("15m", "1h", "4h")]
    ups = sum(1 for v in votes if v == "up")
    downs = sum(1 for v in votes if v == "down")
    if ups > downs and ups >= 2:
        return "long"
    if downs > ups and downs >= 2:
        return "short"
    return None


def _anchor_for(setup_type: str) -> str:
    # trend_continuation and range_breakout require a structure invalidation;
    # anything else may use the ATR anchor. Mirrors build_setup_plan rather
    # than duplicating its judgement.
    return ("structure"
            if setup_type in {"trend_continuation", "range_breakout"}
            else "atr")


def deterministic_proposals(row: dict, cfg: dict) -> list:
    """Every setup the contract declares valid, taken at face value."""
    # Recorded snapshots contain evidence computed with the configuration
    # that was live at the time. Reusing it for a candidate whose parameters
    # change the contract would silently replay the baseline.
    evidence = strategy.setup_evidence(row, cfg)
    direction = trend_majority(row)
    if direction is None:
        return []

    out = []
    for setup_type, contract in sorted(evidence.items()):
        if not isinstance(contract, dict):
            continue
        if contract.get(direction) is not True:
            continue
        if setup_type == "other":
            continue                      # never a deterministic proposal
        out.append({
            "symbol": row.get("_symbol"), "action": "open",
            "direction": direction, "setup_type": setup_type,
            "confidence": 1.0,
            "invalidation_anchor": _anchor_for(setup_type),
            "exit_policy": "fixed_rr",
        })
    return out


def _recorded_by_cycle(outputs: list) -> dict:
    out: dict = {}
    for record in outputs:
        if record.cycle_id:
            out.setdefault(record.cycle_id, []).extend(
                record.parsed_decisions or [])
    return out


class Replay:
    """Replay one variant over a corpus. No network, no LLM, no orders."""

    def __init__(self, cfg: dict, variant_id: str = "momentum.baseline",
                 mode: str = "recorded_llm", price_cache=None,
                 costs: CostModel | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        self.cfg = cfg
        self.variant_id = variant_id
        self.mode = mode
        self.price_cache = price_cache
        self.costs = costs or CostModel(
            expected_stop_slippage_pct=float(
                cfg["trading_costs"]["expected_stop_slippage_pct"]),
            max_order_book_slippage_pct=float(
                cfg["execution"]["max_order_book_slippage_pct"]))
        self.risk = RiskEngine(cfg)

    # Proposals

    def _proposals(self, cycle, row: dict, symbol: str,
                   recorded: dict) -> list:
        row = dict(row)
        row["_symbol"] = symbol
        if self.mode == "deterministic":
            return deterministic_proposals(row, self.cfg)

        model = [d for d in recorded.get(cycle.cycle_id or "", [])
                 if d.get("symbol") == symbol
                 and str(d.get("action")) == "open"]
        if self.mode == "recorded_llm":
            return model

        # deterministic_vetoed: the contract proposes, the model may only
        # suppress. A model proposal on a symbol the contract did not fire
        # on is ignored, and a contract firing the model declined is skipped.
        fired = deterministic_proposals(row, self.cfg)
        if not model:
            return []
        vetoed_directions = {str(d.get("direction")) for d in model}
        return [p for p in fired if p["direction"] in vetoed_directions]

    # Replay

    def run(self, cycles: list, outputs: list,
            max_hold_hours: float | None = None) -> ReplayResult:
        if self.price_cache is not None:
            require_complete_contract(str(self.cfg["strategy"]["id"]))
        recorded = _recorded_by_cycle(outputs)
        max_hold = (float(max_hold_hours) if max_hold_hours is not None
                    else float(self.cfg["risk"]["max_hold_hours"]))
        result = ReplayResult(
            variant_id=self.variant_id, mode=self.mode, cycles=len(cycles),
            corpus_from_ts=cycles[0].ts if cycles else 0.0,
            corpus_to_ts=cycles[-1].ts if cycles else 0.0,
            strategy_id=str(self.cfg["strategy"].get("id") or "") or None,
            strategy_version=str(self.cfg["strategy"].get("version") or "")
            or None)
        funnel = {"fired": 0, "proposed": 0, "vetoed": 0, "executed": 0,
                  "veto_reasons": {}}

        # Portfolio state simulated forward, so exposure caps and cooldowns
        # bind the way they do live rather than every cycle starting flat.
        positions: list = []
        active_trades: dict = {}
        # Setup memory, simulated forward exactly as the engine keeps it in
        # st["recent_setups"]. Without it the replay re-proposes setups the
        # live agent had already burned for the bar, blocked on a semantic
        # cooldown, or refused as a failed-thesis re-entry - so the funnel
        # overstates proposals and the veto distribution that gate G4 reads
        # is wrong in the direction that makes the contract look busier than
        # it is.
        records: dict = {}
        cooldowns: dict = {}
        gross_notional = 0.0
        try:
            equity = float((cycles[0].portfolio if cycles else {}).get(
                "equity_usdt") or 10_000.0)
        except (TypeError, ValueError):
            equity = 10_000.0
        high_water = equity
        day_start_equity = equity
        current_day = None
        circuit_state = "RUNNING"
        previous_universe = None
        diagnostics = {
            "modelled_fields": [
                "equity_feedback", "universe_membership", "cooldowns",
                "positions", "gross_exposure", "open_risk",
                "daily_loss_circuit", "max_drawdown_circuit",
                "circuit_breaker_flattening",
            ],
            "unmodelled_fields": [
                "exchange_fill_race", "exchange_protection_state",
                "liquidation_distance", "margin_usage", "transfer_identifiers",
            ],
            "transitions": {
                "positions_opened": 0, "positions_closed": 0,
                "loss_cooldowns_started": 0, "universe_changes": 0,
                "day_stops": 0, "drawdown_kills": 0,
                "circuit_positions_flattened": 0,
                "outcomes_unresolved": 0,
            },
            "recorded_mismatches": {
                "state": 0, "position_count": 0, "cooldown_symbols": 0,
            },
        }
        if self.price_cache is None:
            diagnostics["unmodelled_fields"].extend([
                "resolved_equity_feedback", "loss_cooldown_outcome",
            ])

        for cycle in cycles:
            now = cycle.ts
            positions, gross_notional, equity, closed, losses, unresolved = (
                self._settle_positions(
                    positions, now, max_hold, equity, cooldowns, records))
            diagnostics["transitions"]["positions_closed"] += closed
            diagnostics["transitions"]["loss_cooldowns_started"] += losses
            diagnostics["transitions"]["outcomes_unresolved"] += unresolved
            cooldowns = {
                symbol: until for symbol, until in cooldowns.items()
                if float(until) > now
            }
            active_trades = {p["symbol"]: active_trades.get(p["symbol"])
                             or {"risk_usd": float(p.get("risk_usd") or 0)}
                             for p in positions}

            day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
            if current_day != day:
                current_day = day
                day_start_equity = equity
                if circuit_state == "DAY_STOPPED":
                    circuit_state = "RUNNING"
            high_water = max(high_water, equity)
            drawdown = ((high_water - equity) / high_water * 100.0
                        if high_water > 0 else 100.0)
            day_pnl = ((equity - day_start_equity) / day_start_equity * 100.0
                       if day_start_equity > 0 else -100.0)
            kill_triggered = False
            day_stop_triggered = False
            if (circuit_state != "KILLED"
                    and drawdown >= float(
                        self.cfg["risk"]["max_drawdown_pct"])):
                circuit_state = "KILLED"
                kill_triggered = True
                diagnostics["transitions"]["drawdown_kills"] += 1
            elif (circuit_state == "RUNNING"
                  and day_pnl <= -float(
                      self.cfg["risk"]["daily_loss_limit_pct"])):
                circuit_state = "DAY_STOPPED"
                day_stop_triggered = True
                diagnostics["transitions"]["day_stops"] += 1

            flatten = kill_triggered or (
                day_stop_triggered
                and bool(self.cfg["execution"].get("flatten_on_daily_stop")))
            if flatten and positions:
                equity, flattened, unresolved = self._flatten_positions(
                    positions, cycle, equity, records, now)
                positions = []
                active_trades = {}
                gross_notional = 0.0
                diagnostics["transitions"]["positions_closed"] += flattened
                diagnostics["transitions"][
                    "circuit_positions_flattened"] += flattened
                diagnostics["transitions"]["outcomes_unresolved"] += unresolved

            universe = tuple(sorted(cycle.symbols()))
            if previous_universe is not None and universe != previous_universe:
                diagnostics["transitions"]["universe_changes"] += 1
            previous_universe = universe
            recorded_state = cycle.portfolio or {}
            if (recorded_state.get("state")
                    and recorded_state.get("state") != circuit_state):
                diagnostics["recorded_mismatches"]["state"] += 1
            recorded_positions = recorded_state.get("open_positions")
            if (isinstance(recorded_positions, list)
                    and len(recorded_positions) != len(positions)):
                diagnostics["recorded_mismatches"]["position_count"] += 1
            recorded_cooldowns = {
                str(item.get("symbol")) for item in
                (recorded_state.get("post_loss_cooldowns") or [])
                if isinstance(item, dict) and item.get("symbol")
            }
            if recorded_cooldowns != set(cooldowns):
                diagnostics["recorded_mismatches"]["cooldown_symbols"] += 1
            if circuit_state != "RUNNING":
                continue

            # The provider request intentionally excludes enrichment, while
            # deterministic live risk consumes this one recorded market-level
            # field. Join only that field into a private view: replayed model
            # input and the corpus snapshot must remain byte-identical.
            risk_snapshot = cycle.snapshot
            joined = (cycle.enrichment
                      if isinstance(cycle.enrichment, dict) else {})
            market_enrichment = joined.get("market")
            if (isinstance(market_enrichment, dict)
                    and "setup_breadth_by_strategy" in market_enrichment):
                risk_snapshot = dict(cycle.snapshot)
                raw_context = cycle.snapshot.get("_market_context")
                context = (dict(raw_context)
                           if isinstance(raw_context, dict) else {})
                raw_enrichment = context.get("_enrichment")
                risk_enrichment = (
                    dict(raw_enrichment)
                    if isinstance(raw_enrichment, dict) else {})
                risk_enrichment["setup_breadth_by_strategy"] = (
                    market_enrichment["setup_breadth_by_strategy"])
                context["_enrichment"] = risk_enrichment
                risk_snapshot["_market_context"] = context

            for symbol in cycle.symbols():
                row = dict(cycle.snapshot[symbol])
                row["setup_evidence"] = strategy.setup_evidence(row, self.cfg)
                signal_ts = row.get("signal_ts")
                proposals = self._proposals(cycle, row, symbol, recorded)
                if not proposals:
                    continue
                funnel["fired"] += len(proposals)

                for decision in proposals:
                    decision = dict(decision, symbol=symbol)

                    # Per-bar idempotency. A symbol that reached the contract
                    # and failed is burned for the whole signal bar, and the
                    # model cannot evade that by relabelling or flipping
                    # direction next cycle.
                    probe = strategy.signal_probe(decision, row, self.cfg)
                    if (probe is not None
                            and strategy.evaluated_signal(
                                records, probe) is not None):
                        result.decisions.append(ReplayDecision(
                            cycle.cycle_id, now, symbol, signal_ts,
                            "vetoed", decision.get("direction"),
                            decision.get("setup_type"),
                            _confidence(decision),
                            reason=("symbol already evaluated for this "
                                    "completed signal candle")))
                        funnel["vetoed"] += 1
                        _count(funnel["veto_reasons"],
                               "symbol already evaluated for this completed "
                               "signal candle")
                        continue

                    plan, why = strategy.build_setup_plan(
                        decision, row, self.cfg)
                    if plan is None:
                        # The live engine burns the bar here too, so a later
                        # cycle cannot retry the same symbol.
                        if probe is not None:
                            record = strategy.new_setup_record(
                                probe, self.cfg, now=now)
                            strategy.mark_setup(
                                record, "risk_rejected", self.cfg, now=now)
                            records[probe["setup_id"]] = record
                        result.decisions.append(ReplayDecision(
                            cycle.cycle_id, now, symbol, signal_ts,
                            "vetoed", decision.get("direction"),
                            decision.get("setup_type"),
                            _confidence(decision), reason=why))
                        funnel["vetoed"] += 1
                        _count(funnel["veto_reasons"], why)
                        continue

                    blocked = strategy.semantic_block(
                        records, plan["setup_key"], now=now)
                    if blocked is not None:
                        reason = ("semantically identical setup is cooling "
                                  "down")
                        result.decisions.append(ReplayDecision(
                            cycle.cycle_id, now, symbol, signal_ts,
                            "vetoed", decision.get("direction"),
                            decision.get("setup_type"),
                            _confidence(decision), reason=reason))
                        funnel["vetoed"] += 1
                        _count(funnel["veto_reasons"], reason)
                        continue

                    reentry = strategy.failed_thesis_reentry_reason(
                        records, plan, self.cfg, now=now)
                    if reentry is not None:
                        result.decisions.append(ReplayDecision(
                            cycle.cycle_id, now, symbol, signal_ts,
                            "vetoed", decision.get("direction"),
                            decision.get("setup_type"),
                            _confidence(decision), reason=reentry))
                        funnel["vetoed"] += 1
                        _count(funnel["veto_reasons"], reentry)
                        continue

                    records[plan["setup_id"]] = strategy.new_setup_record(
                        plan, self.cfg, now=now)

                    funnel["proposed"] += 1
                    merged = dict(decision)
                    merged.update({
                        "stop_loss_pct": plan.get("stop_loss_pct"),
                        "take_profit_pct": plan.get("take_profit_pct"),
                    })
                    sized, veto = self.risk.vet_open(
                        merged, equity, positions, risk_snapshot,
                        cooldowns, gross_notional,
                        active_trades=active_trades, now=now)
                    if sized is None:
                        result.decisions.append(ReplayDecision(
                            cycle.cycle_id, now, symbol, signal_ts,
                            "vetoed", decision.get("direction"),
                            decision.get("setup_type"),
                            _confidence(decision), reason=veto,
                            stop_pct=plan.get("stop_loss_pct"),
                            take_pct=plan.get("take_profit_pct"),
                            proposal_id=plan.get("setup_id"),
                            contract_passed=True,
                            strategy_id=plan.get("strategy_id"),
                            strategy_version=plan.get("strategy_version"),
                            setup_key=plan.get("setup_key")))
                        funnel["vetoed"] += 1
                        _count(funnel["veto_reasons"], veto)
                        continue

                    funnel["executed"] += 1
                    record = ReplayDecision(
                        cycle.cycle_id, now, symbol, signal_ts, "executed",
                        decision.get("direction"), decision.get("setup_type"),
                        _confidence(decision),
                        stop_pct=sized.get("sl_pct"),
                        take_pct=sized.get("tp_pct"),
                        notional=sized.get("notional"),
                        proposal_id=plan.get("setup_id"),
                        contract_passed=True,
                        enrichment=_enrichment_of(row, cycle, symbol),
                        strategy_id=plan.get("strategy_id"),
                        strategy_version=plan.get("strategy_version"),
                        setup_key=plan.get("setup_key"))
                    record.outcome = self._resolve(
                        symbol, row, sized, decision, max_hold,
                        decision_ts=now)
                    unresolved_outcome = bool(
                        isinstance(record.outcome, dict)
                        and record.outcome.get("result") == "no_data")
                    if unresolved_outcome:
                        diagnostics["transitions"][
                            "outcomes_unresolved"] += 1
                    result.decisions.append(record)

                    # "side", not "direction": vet_open validates held
                    # positions in the exchange's vocabulary, and a position
                    # it cannot read is one it refuses to size against.
                    # A position occupies a slot until it exits, and it exits
                    # when its stop or target is hit - not only at max hold.
                    # Expiring solely on the timer would leave every winner
                    # occupying a slot for a day, and "max concurrent
                    # positions" would dominate the funnel as an artefact of
                    # the simulation rather than a property of the strategy.
                    closes_at = now + max_hold * 3600
                    exit_ts = (record.outcome or {}).get("exit_ts")
                    if exit_ts:
                        closes_at = min(closes_at, float(exit_ts) / 1000.0)
                    positions.append({
                        "symbol": symbol,
                        "side": decision["direction"],
                        "entry_price": float(sized.get("price") or 0),
                        "notional": float(sized.get("notional") or 0),
                        "opened_ts": now,
                        "closes_at": closes_at,
                        "risk_usd": float(sized.get("risk_usd") or 0),
                        "round_trip_cost_pct": float(
                            sized.get("estimated_cost_pct") or 0),
                        "outcome": record.outcome,
                        "unresolved_counted": unresolved_outcome,
                        "setup_id": plan.get("setup_id"),
                    })
                    diagnostics["transitions"]["positions_opened"] += 1
                    active_trades[symbol] = {
                        "risk_usd": float(sized.get("risk_usd") or 0)}
                    gross_notional += abs(float(sized.get("notional") or 0))

        result.funnel = funnel
        diagnostics["final_state"] = {
            "equity_usdt": equity,
            "high_water_mark": high_water,
            "state": circuit_state,
            "positions": len(positions),
            "gross_notional": gross_notional,
            "open_risk_usdt": sum(
                float(position.get("risk_usd") or 0) for position in positions),
            "cooldowns": dict(sorted(cooldowns.items())),
            "universe": list(previous_universe or ()),
        }
        diagnostics["unmodelled_fields"] = sorted(set(
            diagnostics["unmodelled_fields"]))
        result.state_diagnostics = diagnostics
        return result

    def _flatten_positions(
            self, positions: list, cycle, equity: float, records: dict,
            now: float) -> tuple[float, int, int]:
        """Model the production circuit breaker's immediate market flatten."""
        unresolved = 0
        for position in positions:
            row = cycle.snapshot.get(position["symbol"]) or {}
            try:
                mark = float(row.get("price") or 0)
                entry = float(position.get("entry_price") or 0)
                notional = float(position.get("notional") or 0)
            except (TypeError, ValueError):
                mark = entry = notional = 0.0
            if mark > 0 and entry > 0 and notional > 0:
                sign = 1.0 if position.get("side") == "long" else -1.0
                gross_pct = sign * (mark - entry) / entry * 100.0
                net_pct = gross_pct - float(
                    position.get("round_trip_cost_pct") or 0)
                pnl = notional * net_pct / 100.0
                equity += pnl
            else:
                pnl = 0.0
                unresolved += 1
            setup = records.get(position.get("setup_id"))
            if isinstance(setup, dict):
                strategy.mark_setup(
                    setup, "closed", self.cfg, now=now,
                    apply_cooldown=False, realized_pnl_usd=pnl)
        return equity, len(positions), unresolved

    def _settle_positions(
            self, positions: list, now: float, max_hold_hours: float,
            equity: float, cooldowns: dict, records: dict) -> tuple:
        """Apply recorded forward outcomes to replay equity and cooldowns.

        ``closes_at`` is the resolved stop/target time when a price cache was
        available and the max-hold deadline otherwise, so a replay without
        outcomes degrades to timer-only expiry and explicitly diagnoses the
        missing equity/cooldown transition.
        """
        keep = []
        closed = losses = unresolved = 0
        for position in positions:
            closes_at = position.get("closes_at")
            if closes_at is None:
                closes_at = (float(position.get("opened_ts") or 0)
                             + max_hold_hours * 3600)
            if now < float(closes_at):
                keep.append(position)
                continue
            closed += 1
            outcome = position.get("outcome") or {}
            if outcome.get("r_multiple") is None:
                if not position.get("unresolved_counted"):
                    unresolved += 1
                continue
            pnl = (float(position.get("risk_usd") or 0)
                   * float(outcome["r_multiple"]))
            equity += pnl
            if pnl < 0:
                losses += 1
                cooldowns[position["symbol"]] = (
                    now + float(self.cfg["risk"]
                                ["cooldown_minutes_after_loss"]) * 60)
            setup = records.get(position.get("setup_id"))
            if isinstance(setup, dict):
                strategy.mark_setup(
                    setup, "closed", self.cfg, now=now,
                    apply_cooldown=pnl < 0, realized_pnl_usd=pnl)
        gross = sum(abs(float(p.get("notional") or 0)) for p in keep)
        return keep, gross, equity, closed, losses, unresolved

    def _resolve(self, symbol: str, row: dict, sized: dict,
                 decision: dict, max_hold_hours: float,
                 decision_ts: float) -> dict | None:
        if self.price_cache is None:
            return None
        signal_ts = row.get("signal_ts")
        if signal_ts is None:
            return None
        plan = SetupPlan(
            symbol=symbol, direction=decision["direction"],
            entry_price=float(row.get("price") or 0),
            stop_pct=float(sized.get("sl_pct") or 0),
            take_pct=float(sized.get("tp_pct") or 0),
            signal_ts=int(signal_ts),
            entry_ts=int(float(decision_ts) * 1000),
            signal_timeframe=str(self.cfg["strategy"]["signal_timeframe"]),
            spread_pct=float(row.get("spread_pct") or 0),
            funding_rate_pct=float(row.get("funding_rate_pct") or 0),
            funding_interval_hours=float(
                row.get("funding_interval_hours") or 0),
            taker_fee_pct_per_side=float(
                row.get("taker_fee_pct_per_side")
                or self.cfg["trading_costs"]["taker_fee_pct_per_side"]))
        if plan.entry_price <= 0 or plan.stop_pct <= 0 or plan.take_pct <= 0:
            return None
        outcome = resolve_from_cache(
            plan, self.price_cache, max_hold_hours=max_hold_hours,
            costs=self.costs)
        return {
            # ``no_data`` is unresolved, never a zero-R trade.  Keeping the
            # key present with None lets settlement release max-hold capacity
            # while incrementing the explicit unresolved diagnostic.
            "result": outcome.result,
            "r_multiple": (None if outcome.result == "no_data"
                            else outcome.r_multiple),
            "net_pct": outcome.net_pct, "mae_pct": outcome.mae_pct,
            "mfe_pct": outcome.mfe_pct, "bars_held": outcome.bars_held,
            "tie_broken": outcome.tie_broken, "exit_ts": outcome.exit_ts,
        }


def _enrichment_of(row: dict, cycle, symbol: str) -> dict:
    """Merge the market and symbol enrichment blocks for this observation.

    The primary source is the cycle's joined ``snapshot_enrichment`` event,
    because B0.5 withholds these fields from the prompt and they are
    therefore absent from the recorded snapshot by design. The in-snapshot
    lookup is a fallback for corpora written by a build where the fields did
    briefly travel together.
    """
    from agent.brain import ENRICHMENT_KEY

    out = {}
    joined = cycle.enrichment or {}
    if isinstance(joined.get("market"), dict):
        out.update(joined["market"])
    symbols = joined.get("symbols") or {}
    if isinstance(symbols.get(symbol), dict):
        out.update(symbols[symbol])

    context = (cycle.snapshot.get("_market_context") or {})
    if isinstance(context.get(ENRICHMENT_KEY), dict):
        out.update(context[ENRICHMENT_KEY])
    if isinstance(row.get(ENRICHMENT_KEY), dict):
        out.update(row[ENRICHMENT_KEY])
    return out


def _confidence(decision: dict) -> float | None:
    try:
        return float(decision.get("confidence"))
    except (TypeError, ValueError):
        return None


def _count(bucket: dict, reason) -> None:
    key = str(reason or "unknown")
    bucket[key] = bucket.get(key, 0) + 1


def _normal_variant(value, strategy_id: str | None = None) -> str | None:
    """Normalize live/baseline aliases to one comparable identity.

    Live journals historically use ``live`` while the authoritative replay is
    named ``<strategy>.baseline``.  Those names describe the same unmodified
    contract for G2.  Candidate variants remain distinct and therefore cannot
    accidentally clear the baseline gate.
    """
    if value is None or not str(value).strip():
        return "baseline"
    text = str(value).strip().lower()
    strategy = str(strategy_id or "").strip().lower()
    strategy_prefix = strategy.replace("-", "_")
    aliases = {"live", "baseline"}
    if strategy:
        aliases.add(f"{strategy}.baseline")
        # The registry's variant naming convention uses an underscore-safe
        # strategy prefix, while the strategy identity itself retains its
        # original hyphenated value.
        aliases.add(f"{strategy_prefix}.baseline")
    else:
        # Legacy rows have no strategy context; retain the historical
        # momentum alias only for that deliberately ambiguous case.
        aliases.add("momentum.baseline")
    if text in aliases:
        return "baseline"
    return text


def canonical_proposal_identity(row, *, strategy_id: str | None = None,
                                strategy_version: str | None = None,
                                variant_id: str | None = None,
                                legacy: bool = False,
                                include_strategy: bool = True) -> tuple:
    """Return the strict G2 identity for one recorded/replayed proposal.

    ``legacy`` is selected only when the corpus has no modern identity fields;
    it preserves replayability of old `(cycle, symbol, direction)` journals.
    Current rows include strategy/version, normalized variant, setup identity
    and type, signal timestamp, symbol, direction, and cycle identity.
    """
    def get(name, fallback=None):
        if isinstance(row, dict):
            value = row.get(name)
        else:
            value = getattr(row, name, None)
        return fallback if value is None else value

    cycle = get("cycle_id")
    symbol = get("symbol")
    direction = get("direction")
    if not isinstance(cycle, str) or not cycle.strip():
        raise ValueError("missing cycle_id")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("missing symbol")
    if direction not in {"long", "short"}:
        raise ValueError("invalid direction")
    if legacy:
        return ("g2-legacy", cycle.strip(), symbol.strip(), direction)

    sid = (get("strategy_id", strategy_id) if include_strategy
           else strategy_id)
    sversion = (get("strategy_version", strategy_version)
                if include_strategy else strategy_version)
    # Modern rows should carry both fields.  A missing pair is an intentional
    # legacy fallback; a half-populated pair is malformed and cannot be
    # compared safely.
    if (sid is None) != (sversion is None):
        raise ValueError("incomplete strategy identity")
    if sid is not None and (not isinstance(sid, str)
                            or not isinstance(sversion, str)):
        raise ValueError("invalid strategy identity")
    sid = str(sid).strip() if sid is not None else None
    sversion = str(sversion).strip() if sversion is not None else None
    if sid == "" or sversion == "":
        raise ValueError("empty strategy identity")
    signal = get("signal_ts")
    if signal is not None:
        try:
            if isinstance(signal, bool):
                raise ValueError
            numeric_signal = float(signal)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("invalid signal_ts") from None
        if not numeric_signal.is_integer():
            raise ValueError("invalid signal_ts")
        signal = int(numeric_signal)
    if signal is None:
        raise ValueError("missing signal_ts")
    if signal < 0:
        raise ValueError("invalid signal_ts")
    setup_type = get("setup_type")
    if setup_type is not None:
        if not isinstance(setup_type, str) or not setup_type.strip():
            raise ValueError("invalid setup_type")
        setup_type = setup_type.strip()
    raw_variant = get("variant_id", variant_id)
    if raw_variant is not None and not isinstance(raw_variant, str):
        raise ValueError("invalid variant identity")
    setup_id = get("setup_id", get("proposal_id", get("_g2_setup_id")))
    setup_key = get("setup_key")
    if setup_id is not None:
        if not isinstance(setup_id, str) or not setup_id.strip():
            raise ValueError("invalid setup_id")
        setup_id = setup_id.strip()
    if setup_key is not None:
        if not isinstance(setup_key, str) or not setup_key.strip():
            raise ValueError("invalid setup_key")
        setup_key = setup_key.strip()
    # setup identity is represented by the stable setup_id where present,
    # otherwise the semantic setup_key/type.  At least one must be present in
    # a modern row; this prevents materially different proposals collapsing.
    if setup_type is None:
        raise ValueError("missing setup_type")
    setup_identity = setup_id or setup_key
    if setup_identity is None:
        raise ValueError("missing setup identity")
    return (
        "g2-v2", sid, sversion,
        _normal_variant(raw_variant, sid),
        setup_identity, setup_key, setup_type, signal,
        symbol.strip(), direction, cycle.strip(),
    )


def _g2_legacy_mode(rows: list[dict]) -> bool:
    """Detect journals predating strict proposal identity fields."""
    return corpus.g2_evidence_window(rows).legacy_mode


def _strict_replay_rows(result: ReplayResult, legacy: bool,
                        include_strategy: bool,
                        allowed_cycle_ids: set[str] | None = None
                        ) -> tuple[list[tuple], dict]:
    keys = []
    diagnostics = {"malformed": 0, "duplicate": 0, "reasons": {}}
    seen = set()
    def reject(reason):
        diagnostics["malformed"] += 1
        diagnostics["reasons"][reason] = diagnostics["reasons"].get(reason, 0) + 1
    for decision in result.decisions:
        if not getattr(decision, "contract_passed", False):
            continue
        if (allowed_cycle_ids is not None
                and getattr(decision, "cycle_id", None)
                not in allowed_cycle_ids):
            continue
        try:
            key = canonical_proposal_identity(
                decision,
                strategy_id=(result.strategy_id if include_strategy else None),
                strategy_version=(result.strategy_version
                                  if include_strategy else None),
                variant_id=result.variant_id, legacy=legacy,
                include_strategy=include_strategy)
        except ValueError as exc:
            reject(str(exc))
            continue
        if key in seen:
            diagnostics["duplicate"] += 1
        seen.add(key)
        keys.append(key)
    return keys, diagnostics


# Self-validation

def fidelity(result: ReplayResult, db, captured_metadata: dict | None = None) -> dict:
    """Gate G2: does the baseline replay reproduce what the agent recorded?

    This is the keystone. If the replay does not reproduce the live agent's
    own decisions then every number downstream of it is worthless, and the
    failure is silent: the replay still produces a clean table, and the table
    is wrong. Treat a G2 failure as a full stop rather than a debugging task
    to work around.

    **Known mismatch sources, to explain rather than to work around.**

    In the *missing* direction (live proposed, replay did not) these are
    expected and should be individually accounted for:

    - ``time.time()`` boundary effects on cooldown expiry.
    - Cycles where reconciliation set ``max_new = 0``, so the agent could not
      act on a setup the contract genuinely fired.

    In the *extra* direction (replay proposed, live did not) the three
    checks ``_prepare_setup_decision`` applies beyond the contract are now
    simulated - per-bar idempotency, semantic cooldown, and failed-thesis
    re-entry - so the replayed funnel is a measurement rather than an upper
    bound. On the demonstration corpus, adding them moved the proposal count
    from 954 to 707 and revealed that a quarter of all vetoes were per-bar
    idempotency, which had been invisible.

    Outcome feedback is modelled when a price cache supplies a resolved exit:
    equity changes, positions close, and losing symbols enter cooldown. Without
    a price cache those transitions remain explicitly listed as unmodelled in
    ``state_diagnostics`` rather than being silently assumed.
    """
    # ``captured_metadata`` is supplied by the CLI immediately before the
    # Replay.run that produced ``result``.  Recomputing the fingerprint only
    # after execution would certify a journal that the replay never saw when
    # another process appends an event during the run.
    captured_metadata = (captured_metadata
                         or getattr(result, "g2_metadata", None)
                         or g2_evidence_metadata(db))
    captured_metadata = dict(captured_metadata)
    all_recorded, load_report = corpus.load_g2_proposals(db)
    evidence = corpus.g2_evidence_window(all_recorded)
    recorded = evidence.rows
    legacy = evidence.legacy_mode
    include_strategy = any(
        row.get("strategy_id") is not None
        or row.get("strategy_version") is not None for row in recorded)
    recorded_keys: list[tuple] = []
    recorded_duplicates = 0
    seen_recorded = set()
    for row in recorded:
        try:
            key = canonical_proposal_identity(
                row, strategy_id=row.get("strategy_id"),
                strategy_version=row.get("strategy_version"),
                variant_id=row.get("variant_id"), legacy=legacy,
                include_strategy=include_strategy)
        except ValueError as exc:
            load_report.reject(str(exc))
            continue
        if key in seen_recorded:
            recorded_duplicates += 1
        seen_recorded.add(key)
        recorded_keys.append(key)
    recorded_reasons = dict(load_report.reasons)
    allowed_cycle_ids = corpus.g2_post_boundary_cycle_ids(db, evidence)
    replayed_keys, replay_diag = _strict_replay_rows(
        result, legacy, include_strategy, allowed_cycle_ids)
    replay_corpus = corpus.g2_replay_corpus_metadata(db, evidence)
    current_metadata = g2_evidence_metadata(db)
    changed_fields = g2_metadata_changes(captured_metadata, current_metadata)
    capture_stale = bool(changed_fields)
    capture_stale_reason = (
        "authoritative G2 replay corpus changed after replay capture: "
        + ", ".join(changed_fields)
        if capture_stale else "")
    # Everything the contract accepted, not everything that executed. The
    # live engine journals setup_proposed before RiskEngine.vet_open.
    recorded_set = set(recorded_keys)
    replayed_set = set(replayed_keys)
    matched = recorded_set & replayed_set
    missing = recorded_set - replayed_set
    extra = replayed_set - recorded_set
    total = len(recorded_set)
    transitions = result.state_diagnostics.get("transitions") or {}
    unresolved = int(transitions.get("outcomes_unresolved") or 0)
    malformed = (int(load_report.malformed)
                 + int(replay_diag["malformed"])
                 + int(evidence.legacy_after_boundary_count))
    duplicate = recorded_duplicates + int(replay_diag["duplicate"])
    # G2 is a proposal-fidelity check.  Outcome resolution is deliberately
    # diagnostic here: the nightly replay has no authoritative price cache,
    # and a ``no_data`` outcome must not turn an otherwise exact proposal
    # comparison into a false failure.  If a future authoritative outcome
    # lane changes later proposals, the exact missing/extra comparison above
    # still fails closed.
    passes = bool(total and not missing and not extra and not malformed
                 and not duplicate and not capture_stale)
    reasons = dict(recorded_reasons)
    if evidence.legacy_after_boundary_count:
        reasons["legacy_after_modern_boundary"] = (
            evidence.legacy_after_boundary_count)
    for key, value in replay_diag["reasons"].items():
        reasons[f"replay:{key}"] = value
    if capture_stale:
        reasons["authoritative_corpus_changed_after_replay"] = 1
    missing_display = (sorted(missing, key=repr)[:50]
                       if not legacy else
                       sorted((key[1], key[2], key[3]) for key in missing)[:50])
    extra_display = (sorted(extra, key=repr)[:50]
                     if not legacy else
                     sorted((key[1], key[2], key[3]) for key in extra)[:50])
    return {
        "recorded": total,
        "replayed": len(replayed_set),
        "matched": len(matched),
        "missing": missing_display,
        "extra": extra_display,
        "missing_count": len(missing),
        "extra_count": len(extra),
        "malformed_count": malformed,
        "duplicate_count": duplicate,
        "unresolved_count": unresolved,
        "malformed_reasons": reasons,
        "reproduction_rate": (len(matched) / total) if total else 0.0,
        "passes_g2": passes,
        "vacuous": total == 0,
        "legacy_identity": legacy,
        "modern_evidence_count": captured_metadata.get(
            "modern_evidence_count", len(recorded)),
        "legacy_excluded_count": captured_metadata.get(
            "legacy_excluded_count", evidence.legacy_excluded_count),
        "legacy_after_boundary_count": captured_metadata.get(
            "legacy_after_boundary_count", evidence.legacy_after_boundary_count),
        "modern_boundary_ts": captured_metadata.get(
            "modern_boundary_ts", evidence.modern_boundary_ts),
        "modern_boundary_rowid": captured_metadata.get(
            "modern_boundary_rowid", evidence.modern_boundary_rowid),
        "modern_boundary_cycle_id": captured_metadata.get(
            "modern_boundary_cycle_id", evidence.modern_boundary_cycle_id),
        "corpus_digest": captured_metadata.get(
            "corpus_digest", corpus.g2_proposal_digest(recorded)),
        "replay_corpus_digest": captured_metadata.get(
            "replay_corpus_digest", replay_corpus["replay_corpus_digest"]),
        "replay_corpus_count": captured_metadata.get(
            "replay_corpus_count", replay_corpus["replay_corpus_count"]),
        "replay_corpus_max_rowid": captured_metadata.get(
            "replay_corpus_max_rowid",
            replay_corpus["replay_corpus_max_rowid"]),
        "capture_stale": capture_stale,
        "capture_stale_reason": capture_stale_reason,
        "state_diagnostics": result.state_diagnostics,
    }


def g2_corpus_digest(db) -> str:
    """Return the digest used by persisted G2 readiness evidence."""
    rows, _ = corpus.load_g2_proposals(db)
    return corpus.g2_proposal_digest(corpus.g2_evidence_window(rows).rows)


def g2_evidence_metadata(db) -> dict:
    """Return the deterministic modern-boundary metadata for persistence."""
    metadata = g2_capture_metadata(db)
    metadata.pop("_allowed_cycle_ids", None)
    return metadata


def g2_capture_metadata(db) -> dict:
    """Capture G2 metadata and its replay window before loading corpus rows."""
    rows, _ = corpus.load_g2_proposals(db)
    evidence = corpus.g2_evidence_window(rows)
    metadata = {
        "corpus_digest": corpus.g2_proposal_digest(evidence.rows),
        "legacy_identity": evidence.legacy_mode,
        "modern_evidence_count": len(evidence.rows),
        "legacy_excluded_count": evidence.legacy_excluded_count,
        "legacy_after_boundary_count": evidence.legacy_after_boundary_count,
        "modern_boundary_ts": evidence.modern_boundary_ts,
        "modern_boundary_rowid": evidence.modern_boundary_rowid,
        "modern_boundary_cycle_id": evidence.modern_boundary_cycle_id,
    }
    metadata.update(corpus.g2_replay_corpus_metadata(db, evidence))
    metadata["_allowed_cycle_ids"] = corpus.g2_post_boundary_cycle_ids(
        db, evidence)
    return metadata


G2_METADATA_FIELDS = (
    "corpus_digest", "legacy_identity", "modern_evidence_count",
    "legacy_excluded_count", "legacy_after_boundary_count",
    "modern_boundary_ts", "modern_boundary_rowid",
    "modern_boundary_cycle_id", "replay_corpus_digest",
    "replay_corpus_count", "replay_corpus_max_rowid")


def g2_metadata_changes(captured: dict, current: dict) -> list[str]:
    """List authoritative G2 fields changed since a pre-replay capture."""
    return [
        field for field in G2_METADATA_FIELDS
        if field in captured and captured.get(field) != current.get(field)]


def validate_persisted_g2(payload: dict | None, db, cfg: dict) -> tuple[bool, str]:
    """Validate a persisted G2 result against current corpus/code/config.

    Kept in replay.py so the CLI's T3 path and readiness report cannot drift.
    Missing fields deliberately return ``False`` (READY/re-run), while a
    malformed payload never raises into a readiness command.
    """
    if not isinstance(payload, dict) or payload.get("gate") != "G2":
        return False, "no persisted G2 result"
    try:
        proposals, max_ts = _proposal_snapshot_for_validation(db)
    except Exception:  # noqa: BLE001
        return False, "journal unreadable"
    required = ("status", "proposal_count", "max_proposal_ts",
                "strategy_config_version", "fidelity_code_version",
                "corpus_digest", "matched", "recorded",
                "reproduction_rate", "missing_count", "extra_count",
                "malformed_count", "duplicate_count", "unresolved_count",
                "malformed_reasons", "vacuous", "legacy_identity",
                "modern_evidence_count",
                "legacy_excluded_count", "legacy_after_boundary_count",
                "modern_boundary_ts", "modern_boundary_rowid",
                "modern_boundary_cycle_id",
                "replay_corpus_digest", "replay_corpus_count",
                "replay_corpus_max_rowid",
                "replay_digest", "variant_id", "replay_mode")
    if any(field not in payload for field in required):
        return False, "persisted G2 result lacks current diagnostics"
    try:
        status = payload.get("status")
        if not isinstance(status, str):
            return False, "persisted G2 result has an invalid status"
        if status not in {"PASS", "FAILED", "VACUOUS", "INSUFFICIENT_SAMPLE"}:
            return False, "persisted G2 result has an unknown status"
        if status == "PASS" and payload.get("capture_stale"):
            return False, "persisted G2 replay corpus changed during replay"
        if not isinstance(payload.get("strategy_config_version"), str) \
                or not payload["strategy_config_version"].strip():
            return False, "persisted G2 result has an invalid config fingerprint"
        if not isinstance(payload.get("fidelity_code_version"), str) \
                or not payload["fidelity_code_version"].strip():
            return False, "persisted G2 result has an invalid code fingerprint"
        if not isinstance(payload.get("corpus_digest"), str) \
                or not payload["corpus_digest"].strip():
            return False, "persisted G2 result has an invalid corpus digest"
        if not isinstance(payload.get("replay_digest"), str) \
                or not payload["replay_digest"].strip():
            return False, "persisted G2 result has an invalid replay digest"
        if not isinstance(payload.get("replay_corpus_digest"), str) \
                or not payload["replay_corpus_digest"].strip():
            return False, "persisted G2 result has an invalid replay corpus digest"
        if not isinstance(payload.get("variant_id"), str) \
                or not payload["variant_id"].strip():
            return False, "persisted G2 result has an invalid variant id"
        if payload.get("replay_mode") != "recorded_llm":
            return False, "persisted G2 result has an invalid replay mode"
        if not isinstance(payload.get("malformed_reasons"), dict):
            return False, "persisted G2 result has invalid diagnostics"
        if "capture_stale" in payload and not isinstance(
                payload.get("capture_stale"), bool):
            return False, "persisted G2 result has invalid capture status"
        if "capture_stale_reason" in payload and not isinstance(
                payload.get("capture_stale_reason"), str):
            return False, "persisted G2 result has invalid capture reason"
        if not isinstance(payload.get("vacuous"), bool) \
                or not isinstance(payload.get("legacy_identity"), bool):
            return False, "persisted G2 result has invalid flags"

        def nonnegative_int(name: str) -> int:
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 0:
                raise ValueError(f"invalid {name}")
            return value

        proposal_count = nonnegative_int("proposal_count")
        matched_count = nonnegative_int("matched")
        recorded_count = nonnegative_int("recorded")
        diagnostic_counts = {
            name: nonnegative_int(name)
            for name in ("missing_count", "extra_count", "malformed_count",
                         "duplicate_count", "unresolved_count",
                         "modern_evidence_count",
                         "legacy_excluded_count",
                         "legacy_after_boundary_count",
                         "replay_corpus_count",
                         "replay_corpus_max_rowid")}
        max_ts_value = payload.get("max_proposal_ts")
        if isinstance(max_ts_value, bool) or not isinstance(
                max_ts_value, (int, float)):
            return False, "persisted G2 result has an invalid timestamp"
        max_ts_value = float(max_ts_value)
        if not math.isfinite(max_ts_value):
            return False, "persisted G2 result has an invalid timestamp"
        reproduction_rate = payload.get("reproduction_rate")
        if isinstance(reproduction_rate, bool) or not isinstance(
                reproduction_rate, (int, float)):
            return False, "persisted G2 result has an invalid reproduction rate"
        reproduction_rate = float(reproduction_rate)
        if (not math.isfinite(reproduction_rate)
                or not 0.0 <= reproduction_rate <= 1.0):
            return False, "persisted G2 result has an invalid reproduction rate"
        if matched_count > recorded_count:
            return False, "persisted G2 result has inconsistent counts"
        expected_rate = (matched_count / recorded_count
                         if recorded_count else 0.0)
        if reproduction_rate != expected_rate:
            return False, "persisted G2 result has inconsistent counts"
        if bool(payload["vacuous"]) != (recorded_count == 0):
            return False, "persisted G2 result has inconsistent vacuous flag"
        current_digest = g2_corpus_digest(db)
        current_rows, current_load = corpus.load_g2_proposals(db)
        evidence = corpus.g2_evidence_window(current_rows)
        if evidence.modern_boundary_ts is None:
            if payload.get("modern_boundary_ts") is not None \
                    or payload.get("modern_boundary_rowid") is not None \
                    or payload.get("modern_boundary_cycle_id") is not None:
                return False, "persisted G2 modern boundary is stale"
        else:
            boundary_ts = payload.get("modern_boundary_ts")
            boundary_rowid = payload.get("modern_boundary_rowid")
            boundary_cycle_id = payload.get("modern_boundary_cycle_id")
            if (isinstance(boundary_ts, bool)
                    or not isinstance(boundary_ts, (int, float))
                    or not math.isfinite(float(boundary_ts))
                    or float(boundary_ts) != evidence.modern_boundary_ts
                    or isinstance(boundary_rowid, bool)
                    or not isinstance(boundary_rowid, int)
                    or boundary_rowid != evidence.modern_boundary_rowid
                    or boundary_cycle_id != evidence.modern_boundary_cycle_id):
                return False, "persisted G2 modern boundary is stale"
        if (diagnostic_counts["legacy_excluded_count"]
                != evidence.legacy_excluded_count
                or diagnostic_counts["legacy_after_boundary_count"]
                != evidence.legacy_after_boundary_count
                or diagnostic_counts["modern_evidence_count"]
                != len(evidence.rows)):
            return False, "persisted G2 legacy boundary diagnostics are stale"
        current_replay_corpus = corpus.g2_replay_corpus_metadata(
            db, evidence)
        if (payload["replay_corpus_digest"]
                != current_replay_corpus["replay_corpus_digest"]):
            return False, "persisted G2 replay corpus digest is stale"
        if (diagnostic_counts["replay_corpus_count"]
                != current_replay_corpus["replay_corpus_count"]):
            return False, "persisted G2 replay corpus count is stale"
        if (diagnostic_counts["replay_corpus_max_rowid"]
                != current_replay_corpus["replay_corpus_max_rowid"]):
            return False, "persisted G2 replay corpus watermark is stale"
        if current_load.malformed and status != "FAILED":
            return False, "current proposal corpus contains malformed rows"
        current_rows = evidence.rows
        legacy = evidence.legacy_mode
        include_strategy = any(
            row.get("strategy_id") is not None
            or row.get("strategy_version") is not None
            for row in current_rows)
        current_keys = []
        for row in current_rows:
            try:
                current_keys.append(canonical_proposal_identity(
                    row, strategy_id=row.get("strategy_id"),
                    strategy_version=row.get("strategy_version"),
                    variant_id=row.get("variant_id"), legacy=legacy,
                    include_strategy=include_strategy))
            except ValueError:
                if status != "FAILED":
                    return False, "current proposal corpus contains malformed rows"
        current_unique_count = len(set(current_keys))
        if (len(current_keys) != current_unique_count
                and status != "FAILED"):
            return False, "current proposal corpus contains duplicate identities"
        # ``recorded`` is the number of unique, valid canonical identities,
        # not the raw setup_proposed row count.  A FAILED result may have
        # malformed or duplicate rows, but its denominator still reflects the
        # unique identities that could be compared.
        if recorded_count != current_unique_count:
            return False, "persisted G2 denominator is stale"
        if payload.get("legacy_identity") != evidence.legacy_mode:
            return False, "persisted G2 identity mode is stale"
        strategy_cfg = cfg.get("strategy") if isinstance(cfg, dict) else {}
        strategy_id = (strategy_cfg or {}).get("id")
        if _normal_variant(payload["variant_id"], strategy_id) != "baseline":
            return False, "persisted G2 variant is not the baseline"
        if status == "PASS":
            if any(diagnostic_counts[name] != 0 for name in (
                    "missing_count", "extra_count", "malformed_count",
                    "duplicate_count", "legacy_after_boundary_count")):
                return False, "persisted G2 result is not a clean PASS"
            if recorded_count <= 0 or matched_count != recorded_count:
                return False, "persisted G2 PASS is not exact/non-vacuous"
        current = (
            proposal_count == proposals
            and max_ts_value == max_ts
            and payload["strategy_config_version"] ==
                state.strategy_fingerprint(cfg)
            and payload["fidelity_code_version"] == fidelity_code_fingerprint()
            and payload["corpus_digest"] == current_digest)
    except (TypeError, ValueError, OSError, sqlite3.Error):
        return False, "persisted G2 result is malformed"
    return current, ("" if current else "persisted G2 result is stale")


def _proposal_snapshot_for_validation(db) -> tuple[int, float]:
    with closing(sqlite3.connect(
            f"file:{Path(db)}?mode=ro", uri=True)) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(ts), 0) FROM events "
            "WHERE kind='setup_proposed'").fetchone()
    return int(row[0]), float(row[1])
