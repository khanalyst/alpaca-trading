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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent import strategy
from agent.forward_models import require_complete_contract
from agent.risk import RiskEngine

from . import corpus
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
    # The B0.5 enrichment for this symbol-cycle, carried alongside the
    # decision so a conditioning axis can partition on it without a second
    # pass over the corpus. Recorded, never shown to the model.
    enrichment: dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.cycle_id, self.symbol, self.direction, self.setup_type)

    def proposal_key(self) -> tuple:
        """Identity shared across arms, independent of replay execution."""
        if self.proposal_id:
            return ("proposal_id", self.proposal_id)
        signal = self.signal_ts if self.signal_ts is not None else self.cycle_id
        return (self.symbol, signal, self.direction, self.setup_type)


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

    def executed(self) -> list:
        return [d for d in self.decisions if d.stage == "executed"]

    def digest(self) -> str:
        """A stable hash of the run, for the determinism assertion."""
        import hashlib
        payload = json.dumps(
            [[d.cycle_id, d.symbol, d.stage, d.direction, d.setup_type,
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

    # ------------------------------------------------------------- proposal

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

    # --------------------------------------------------------------- replay

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
            corpus_to_ts=cycles[-1].ts if cycles else 0.0)
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
                            contract_passed=True))
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
                        contract_passed=True,
                        enrichment=_enrichment_of(row, cycle, symbol))
                    record.outcome = self._resolve(
                        symbol, row, sized, decision, max_hold,
                        decision_ts=now)
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
            "result": outcome.result, "r_multiple": outcome.r_multiple,
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


# ------------------------------------------------------------ self-validation

def fidelity(result: ReplayResult, db) -> dict:
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
    recorded = corpus.load_events(db, "setup_proposed")
    recorded_keys = {
        (r.get("cycle_id"), r.get("symbol"), r.get("direction"))
        for r in recorded
    }
    # Everything the contract accepted, not everything that executed. The
    # live engine journals setup_proposed in _prepare_setup_decision, which
    # runs BEFORE RiskEngine.vet_open - so comparing against executions
    # would count every risk-vetoed setup as a reproduction failure. On the
    # historical corpus that is roughly four fifths of them, and G2 would
    # fail at ~20% while the replay was in fact correct.
    replayed_keys = {
        (d.cycle_id, d.symbol, d.direction)
        for d in result.decisions if d.contract_passed
    }
    matched = recorded_keys & replayed_keys
    missing = recorded_keys - replayed_keys
    extra = replayed_keys - recorded_keys
    total = len(recorded_keys)
    return {
        "recorded": total,
        "replayed": len(replayed_keys),
        "matched": len(matched),
        "missing": sorted(missing)[:50],
        "extra": sorted(extra)[:50],
        "missing_count": len(missing),
        "extra_count": len(extra),
        "reproduction_rate": (len(matched) / total) if total else 1.0,
        "passes_g2": ((len(matched) / total) >= 0.99) if total else True,
        # A corpus with nothing recorded reproduces 100% of nothing. That is
        # not evidence the replay is faithful, and treating it as a pass
        # would let an empty or broken journal clear the keystone gate while
        # every downstream number ran on air. Callers gating on G2 must check
        # this and refuse to proceed, rather than reading the rate alone.
        "vacuous": total == 0,
        "state_diagnostics": result.state_diagnostics,
    }
