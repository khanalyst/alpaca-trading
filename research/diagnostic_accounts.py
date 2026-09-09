"""Persistent, broker-free forward account mechanics for diagnostic shadows.

This module is deliberately small and pure.  It advances one isolated
candidate book from completed bars and recorded quotes, but it owns no SQLite
connection and has no authority over the runtime or the authorizing replay.
The caller commits the returned state and immutable modeled order/fill records
alongside the diagnostic decision cursor.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    canonical_exit_reason,
    completed_bar_exit_transition,
    exit_deadline,
    initialize_exit_state,
)
from research.costs import CostModel, ReplayPolicy, index_quotes, quote_fill_record
from research.market_data import normalize_quote, record_available_at
from research.quote_costs import CostResolverSetup, cost_resolver_setup


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
ACCOUNT_SCHEMA = "diagnostic-forward-account.v1"
POSITION_SCHEMA = "diagnostic-forward-position.v1"
ORDER_SCHEMA = "diagnostic-forward-order.v1"
FILL_SCHEMA = "diagnostic-forward-fill.v1"


class DiagnosticAccountError(ValueError):
    """Raised when a persistent diagnostic account cannot advance safely."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def content_digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            text = str(value or "").strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def event_available_at(event: Mapping[str, Any]) -> datetime:
    values = [
        _timestamp(event.get("timestamp")),
        _timestamp(event.get("as_of") or event.get("timestamp")),
        _timestamp(event.get("observed_at") or event.get("as_of") or
                   event.get("timestamp")),
    ]
    if any(value is None for value in values):
        raise DiagnosticAccountError("diagnostic event availability is invalid")
    return max(value for value in values if value is not None)


def _event_end(event: Mapping[str, Any]) -> datetime:
    ended = _timestamp(event.get("as_of"))
    opened = _timestamp(event.get("timestamp"))
    if ended is None or opened is None or ended <= opened:
        raise DiagnosticAccountError("completed diagnostic bar boundary is invalid")
    return ended


def _canonical_identity(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return "delayed_sip" if normalized == "delayed" else normalized


def _state_digest(state: Mapping[str, Any]) -> str:
    body = dict(state)
    body.pop("state_digest", None)
    return content_digest(body)


def new_account_state(*, cohort_identity: str, candidate_id: str,
                      starting_cash: float) -> dict[str, Any]:
    cash = _finite(starting_cash)
    if cash is None or cash <= 0:
        raise DiagnosticAccountError("diagnostic starting cash must be positive")
    state = {
        "schema": ACCOUNT_SCHEMA,
        "cohort_identity": str(cohort_identity),
        "candidate_id": str(candidate_id),
        "starting_cash": cash,
        "cash": cash,
        "equity": cash,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "open_position_count": 0,
        "closed_position_count": 0,
        "order_count": 0,
        "fill_count": 0,
        "late_data_gap_count": 0,
        "mark_status": "priced",
        "last_event_key": "",
        "last_event_at": None,
    }
    state["state_digest"] = _state_digest(state)
    return state


def validate_account_state(state: Mapping[str, Any], *,
                           cohort_identity: str, candidate_id: str) -> dict[str, Any]:
    if not isinstance(state, Mapping) or state.get("schema") != ACCOUNT_SCHEMA:
        raise DiagnosticAccountError("diagnostic account state is invalid")
    result = deepcopy(dict(state))
    if (str(result.get("cohort_identity") or "") != str(cohort_identity) or
            str(result.get("candidate_id") or "") != str(candidate_id)):
        raise DiagnosticAccountError("diagnostic account identity conflicts")
    stored = str(result.get("state_digest") or "")
    if not stored or stored != _state_digest(result):
        raise DiagnosticAccountError("diagnostic account state digest mismatch")
    for name in ("starting_cash", "cash", "realized_pnl"):
        value = _finite(result.get(name))
        if value is None:
            raise DiagnosticAccountError(f"diagnostic account {name} is invalid")
        result[name] = value
    for name in ("equity", "unrealized_pnl"):
        value = result.get(name)
        if value is not None:
            value = _finite(value)
            if value is None:
                raise DiagnosticAccountError(
                    f"diagnostic account {name} is invalid")
        result[name] = value
    for name in ("open_position_count", "closed_position_count", "order_count",
                 "fill_count", "late_data_gap_count"):
        value = result.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DiagnosticAccountError(f"diagnostic account {name} is invalid")
    return result


def validate_position_state(state: Mapping[str, Any], *,
                            cohort_identity: str, candidate_id: str) -> dict[str, Any]:
    if not isinstance(state, Mapping) or state.get("schema") != POSITION_SCHEMA:
        raise DiagnosticAccountError("diagnostic position state is invalid")
    result = deepcopy(dict(state))
    if (str(result.get("cohort_identity") or "") != str(cohort_identity) or
            str(result.get("candidate_id") or "") != str(candidate_id)):
        raise DiagnosticAccountError("diagnostic position identity conflicts")
    if str(result.get("status") or "") not in {"open", "closed"}:
        raise DiagnosticAccountError("diagnostic position status is invalid")
    if str(result.get("direction") or "") not in {"long", "short"}:
        raise DiagnosticAccountError("diagnostic position direction is invalid")
    stored = str(result.get("state_digest") or "")
    if not stored or stored != _state_digest(result):
        raise DiagnosticAccountError("diagnostic position state digest mismatch")
    return result


class DiagnosticAccountBook:
    """In-memory transition book for one cohort candidate and one WAL batch."""

    def __init__(self, *, account: Mapping[str, Any],
                 positions: Sequence[Mapping[str, Any]], config: Mapping[str, Any],
                 policy: ReplayPolicy, rule_spec: Mapping[str, Any]):
        cohort = str(account.get("cohort_identity") or "")
        candidate = str(account.get("candidate_id") or "")
        self.account = validate_account_state(
            account, cohort_identity=cohort, candidate_id=candidate)
        self.positions: dict[str, dict[str, Any]] = {}
        for raw in positions:
            position = validate_position_state(
                raw, cohort_identity=cohort, candidate_id=candidate)
            position_id = str(position.get("position_id") or "")
            if not position_id or position_id in self.positions:
                raise DiagnosticAccountError("diagnostic position identity is invalid")
            self.positions[position_id] = position
        self._base_account_digest = str(self.account["state_digest"])
        self._base_position_digests = {
            position_id: str(position["state_digest"])
            for position_id, position in self.positions.items()
        }
        self._touched_positions: set[str] = set()
        self.orders: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.config = deepcopy(dict(config))
        self.policy = policy
        self.rule_spec = deepcopy(dict(rule_spec))
        self.costs: CostResolverSetup = cost_resolver_setup(
            self.config, vehicle="equity")

    @property
    def cohort_identity(self) -> str:
        return str(self.account["cohort_identity"])

    @property
    def candidate_id(self) -> str:
        return str(self.account["candidate_id"])

    @property
    def equity(self) -> float | None:
        return _finite(self.account.get("equity"))

    def has_open(self, symbol: str) -> bool:
        wanted = str(symbol).upper()
        return any(position.get("status") == "open" and
                   str(position.get("symbol") or "").upper() == wanted
                   for position in self.positions.values())

    def risk_state(self) -> tuple[list[dict], dict[str, dict], float]:
        positions: list[dict] = []
        active: dict[str, dict] = {}
        gross = 0.0
        for position in sorted(self.positions.values(), key=lambda row: str(
                row.get("position_id") or "")):
            if position.get("status") != "open":
                continue
            plan = position.get("plan")
            if not isinstance(plan, Mapping):
                raise DiagnosticAccountError("diagnostic position plan is invalid")
            item = deepcopy(dict(plan))
            symbol = str(position.get("symbol") or "")
            item.update({
                "symbol": symbol,
                "direction": position.get("direction"),
                "quantity": position.get("quantity"),
                "shares": position.get("quantity"),
                "entry_price": position.get("entry_price"),
                "risk_usd": position.get("risk_usd"),
                "notional": position.get("notional"),
            })
            positions.append(item)
            active[symbol] = dict(item)
            gross += float(_finite(position.get("notional")) or 0.0)
        return positions, active, gross

    def _cost_model(self, *, leg: str, timestamp: datetime,
                    position: Mapping[str, Any], quantity: float) -> CostModel:
        context = {
            "vehicle": "equity",
            "symbol": str(position.get("symbol") or ""),
            "session_date": timestamp.astimezone(NEW_YORK).date().isoformat(),
            "cost_leg": str(leg),
            "cost_timestamp": timestamp.isoformat(),
            "entry_timestamp": position.get("entry_timestamp"),
            "exit_timestamp": timestamp.isoformat() if leg == "exit" else None,
            "quantity": quantity,
            "shares": quantity,
            "contracts": None,
            "entry_notional": float(position.get("notional") or 0.0),
        }
        resolver = self.costs.resolver
        model = resolver(context) if resolver is not None else self.costs.model
        if not isinstance(model, CostModel):
            raise DiagnosticAccountError("diagnostic cost resolver is invalid")
        return model

    def _normalized_quotes(self, rows: Sequence[Mapping[str, Any]], *,
                           symbol: str) -> list[Any]:
        expected_feed = _canonical_identity(self.policy.equity_feed)
        expected_provider = _canonical_identity(self.policy.equity_provider)
        result = []
        for row in rows:
            if str(row.get("symbol") or "").upper() != str(symbol).upper():
                continue
            if str(row.get("source_mode") or "forward_observed").strip().lower() \
                    != "forward_observed":
                continue
            if (_canonical_identity(row.get("feed")) != expected_feed or
                    _canonical_identity(row.get("provider")) != expected_provider):
                continue
            try:
                result.append(normalize_quote(row))
            except Exception:
                continue
        result.sort(key=lambda quote: (quote.timestamp,
                                      record_available_at(quote) or quote.timestamp))
        return result

    @staticmethod
    def _quote_evidence(quote: Any, fill: Any, *, side: str,
                        boundary: datetime) -> dict[str, Any]:
        available = record_available_at(quote)
        return {
            "side": side,
            "price": float(fill.price),
            "bid": float(quote.bid),
            "ask": float(quote.ask),
            "timestamp": quote.timestamp.isoformat(),
            "as_of": quote.identity.as_of.isoformat(),
            "observed_at": quote.identity.observed_at.isoformat(),
            "available_at": available.isoformat() if available else None,
            "boundary": boundary.isoformat(),
            "age_seconds": max(0.0, (boundary - quote.timestamp).total_seconds()),
            "feed": str(fill.feed),
            "provider": str(fill.provider),
            "source_mode": str(fill.source_mode),
        }

    @staticmethod
    def _fill_matches_quote(quote: Any, fill: Any, *, at: datetime,
                            side: str) -> bool:
        """Match resolver output to the exact causal quote used as evidence."""
        available = record_available_at(quote)
        price = quote.ask if side == "buy" else quote.bid
        return bool(
            available is not None and available <= at and
            quote.timestamp == fill.timestamp and
            quote.identity.as_of == fill.as_of and
            str(quote.identity.feed) == str(fill.feed) and
            str(quote.identity.provider) == str(fill.provider) and
            str(quote.identity.source_mode) == str(fill.source_mode) and
            math.isclose(float(price), float(fill.price), rel_tol=0.0,
                         abs_tol=1e-12))

    def executable_quote(self, rows: Sequence[Mapping[str, Any]], *,
                         symbol: str, at: datetime, side: str) -> dict[str, Any] | None:
        normalized = self._normalized_quotes(rows, symbol=symbol)
        if not normalized:
            return None
        fill = quote_fill_record(
            index_quotes(normalized), symbol=symbol, at=at, side=side,
            max_age_seconds=self.policy.max_market_data_age_seconds,
            session_date=at.astimezone(NEW_YORK).date(),
            allow_historical_backfill_diagnostics=False)
        if fill is None:
            return None
        matching = [quote for quote in normalized
                    if self._fill_matches_quote(
                        quote, fill, at=at, side=side)]
        if not matching:
            return None
        return self._quote_evidence(matching[-1], fill, side=side, boundary=at)

    def first_executable_quote_after(
            self, rows: Sequence[Mapping[str, Any]], *, symbol: str,
            after: datetime, visible_by: datetime, side: str) -> dict[str, Any] | None:
        normalized = self._normalized_quotes(rows, symbol=symbol)
        candidates = []
        for quote in normalized:
            available = record_available_at(quote)
            if (available is None or quote.timestamp < after or
                    available < after or available > visible_by):
                continue
            candidates.append((available, quote.timestamp, quote))
        for available, _timestamp_value, quote in sorted(candidates,
                                                          key=lambda item: item[:2]):
            fill = quote_fill_record(
                index_quotes(normalized), symbol=symbol, at=available, side=side,
                max_age_seconds=self.policy.max_market_data_age_seconds,
                session_date=quote.session_date,
                allow_historical_backfill_diagnostics=False)
            if (fill is not None and self._fill_matches_quote(
                    quote, fill, at=available, side=side)):
                return self._quote_evidence(
                    quote, fill, side=side, boundary=available)
        return None

    def _action_id(self, *, position_id: str, event_key: str, action: str) -> str:
        return content_digest({
            "schema": "diagnostic-forward-action.v1",
            "cohort_identity": self.cohort_identity,
            "candidate_id": self.candidate_id,
            "position_id": str(position_id),
            "event_key": str(event_key),
            "action": str(action),
        })

    def _record_order(self, *, position_id: str, event_key: str, action: str,
                      status: str, side: str, quantity: float,
                      reference_price: float | None,
                      modeled_price: float | None,
                      evidence: Mapping[str, Any]) -> dict[str, Any]:
        action_id = self._action_id(
            position_id=position_id, event_key=event_key, action=action)
        order = {
            "schema": ORDER_SCHEMA,
            "order_id": action_id,
            "action_id": action_id,
            "cohort_identity": self.cohort_identity,
            "candidate_id": self.candidate_id,
            "position_id": position_id,
            "event_key": str(event_key),
            "action": str(action),
            "status": str(status),
            "side": str(side),
            "quantity": float(quantity),
            "reference_price": reference_price,
            "modeled_price": modeled_price,
            "modeled_only": True,
            "actual_fill": False,
            "evidence": deepcopy(dict(evidence)),
        }
        order["digest"] = content_digest(order)
        self.orders.append(order)
        self.account["order_count"] = int(self.account["order_count"]) + 1
        return order

    def _record_fill(self, *, order: Mapping[str, Any], event_key: str,
                     action: str, side: str, quantity: float, price: float,
                     fee: float, evidence: Mapping[str, Any],
                     gross_pnl: float | None = None,
                     net_pnl: float | None = None) -> dict[str, Any]:
        fill_id = content_digest({
            "schema": FILL_SCHEMA,
            "cohort_identity": self.cohort_identity,
            "candidate_id": self.candidate_id,
            "position_id": order["position_id"],
            "event_key": str(event_key),
            "action": str(action),
        })
        fill = {
            "schema": FILL_SCHEMA,
            "fill_id": fill_id,
            "order_id": order["order_id"],
            "cohort_identity": self.cohort_identity,
            "candidate_id": self.candidate_id,
            "position_id": order["position_id"],
            "event_key": str(event_key),
            "action": str(action),
            "side": str(side),
            "quantity": float(quantity),
            "price": float(price),
            "fee": float(fee),
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "modeled_only": True,
            "actual_fill": False,
            "evidence": deepcopy(dict(evidence)),
        }
        fill["digest"] = content_digest(fill)
        self.fills.append(fill)
        self.account["fill_count"] = int(self.account["fill_count"]) + 1
        return fill

    def _touch(self, position: dict[str, Any]) -> None:
        position["state_digest"] = _state_digest(position)
        self._touched_positions.add(str(position["position_id"]))

    def _revalue(self, *, at: datetime,
                 quote_rows: Sequence[Mapping[str, Any]]) -> None:
        total = 0.0
        unpriced = False
        for position in self.positions.values():
            if position.get("status") != "open":
                continue
            if isinstance(position.get("pending_exit"), Mapping):
                position["mark_status"] = "unpriced"
                position["mark_price"] = None
                position["mark_evidence"] = None
                position["unrealized_pnl"] = None
                unpriced = True
                self._touch(position)
                continue
            side = "sell" if position["direction"] == "long" else "buy"
            evidence = self.executable_quote(
                quote_rows, symbol=str(position["symbol"]), at=at, side=side)
            if evidence is None:
                position["mark_status"] = "unpriced"
                position["mark_price"] = None
                position["mark_evidence"] = None
                position["unrealized_pnl"] = None
                unpriced = True
                self._touch(position)
                continue
            mark = float(evidence["price"])
            quantity = float(position["quantity"])
            entry = float(position["entry_price"])
            gross = ((mark - entry) if position["direction"] == "long" else
                     (entry - mark)) * quantity
            unrealized = gross - float(position["entry_fee"])
            position.update({
                "mark_status": "priced",
                "mark_price": mark,
                "mark_evidence": evidence,
                "unrealized_pnl": unrealized,
                "last_mark_at": at.isoformat(),
            })
            total += unrealized
            self._touch(position)
        self.account["unrealized_pnl"] = None if unpriced else total
        self.account["equity"] = (None if unpriced else
                                  float(self.account["cash"]) + total)
        self.account["mark_status"] = "unpriced" if unpriced else "priced"
        self.account["open_position_count"] = sum(
            1 for position in self.positions.values()
            if position.get("status") == "open")
        self.account["late_data_gap_count"] = sum(
            1 for position in self.positions.values()
            if position.get("status") == "open" and
            position.get("late_data_gap") is True)

    def _close(self, position: dict[str, Any], *, event_key: str,
               action: str, reason: str, at: datetime,
               reference_price: float, executable_quote: bool,
               evidence: Mapping[str, Any], tie_broken: bool = False,
               gap_fill: bool = False) -> None:
        quantity = float(position["quantity"])
        direction = str(position["direction"])
        model = self._cost_model(
            leg="exit", timestamp=at, position=position, quantity=quantity)
        modeled = model.execution_price(
            float(reference_price), direction, entry=False,
            executable_quote=bool(executable_quote))
        exit_fee = model.fees(
            modeled, modeled, quantity, vehicle="equity") / 2.0
        entry = float(position["entry_price"])
        gross = ((modeled - entry) if direction == "long" else
                 (entry - modeled)) * quantity
        net = gross - float(position["entry_fee"]) - exit_fee
        side = "sell" if direction == "long" else "buy"
        order = self._record_order(
            position_id=str(position["position_id"]), event_key=event_key,
            action=action, status="modeled_filled", side=side,
            quantity=quantity, reference_price=float(reference_price),
            modeled_price=modeled, evidence=evidence)
        self._record_fill(
            order=order, event_key=event_key, action=action, side=side,
            quantity=quantity, price=modeled, fee=exit_fee,
            gross_pnl=gross, net_pnl=net, evidence=evidence)
        position.update({
            "status": "closed",
            "exit_event_key": str(event_key),
            "exit_timestamp": at.isoformat(),
            "exit_reference": float(reference_price),
            "exit_price": modeled,
            "exit_fee": exit_fee,
            "gross_pnl": gross,
            "realized_pnl": net,
            "unrealized_pnl": 0.0,
            "mark_price": modeled,
            "mark_status": "closed",
            "exit_reason": str(reason),
            "canonical_exit_reason": canonical_exit_reason(reason),
            "tie_broken": bool(tie_broken),
            "gap_fill": bool(gap_fill),
            "late_data_gap": bool(position.get("late_data_gap")),
            "pending_exit": None,
            "last_event_key": str(event_key),
            "last_event_at": at.isoformat(),
            "exit_cost_model": model.as_dict(),
        })
        self.account["cash"] = float(self.account["cash"]) + net
        self.account["realized_pnl"] = (
            float(self.account["realized_pnl"]) + net)
        self.account["closed_position_count"] = (
            int(self.account["closed_position_count"]) + 1)
        self._touch(position)

    def _defer_market_exit(self, position: dict[str, Any], *, event_key: str,
                           reason: str, at: datetime, evidence: Mapping[str, Any],
                           tie_broken: bool, gap_fill: bool) -> None:
        if isinstance(position.get("pending_exit"), Mapping):
            return
        action = f"exit_{canonical_exit_reason(reason)}_unpriced"
        side = "sell" if position["direction"] == "long" else "buy"
        self._record_order(
            position_id=str(position["position_id"]), event_key=event_key,
            action=action, status="unpriced_data_gap", side=side,
            quantity=float(position["quantity"]), reference_price=None,
            modeled_price=None, evidence=evidence)
        position.update({
            "pending_exit": {
                "reason": str(reason),
                "canonical_reason": canonical_exit_reason(reason),
                "requested_at": at.isoformat(),
                "event_key": str(event_key),
                "tie_broken": bool(tie_broken),
                "gap_fill": bool(gap_fill),
            },
            "late_data_gap": True,
            "late_since": at.isoformat(),
            "mark_status": "unpriced",
            "mark_price": None,
            "mark_evidence": None,
            "unrealized_pnl": None,
            "last_event_key": str(event_key),
            "last_event_at": at.isoformat(),
        })
        self._touch(position)

    def _complete_pending_exit(self, position: dict[str, Any], *,
                               event_key: str, visible_by: datetime,
                               quote_rows: Sequence[Mapping[str, Any]]) -> bool:
        pending = position.get("pending_exit")
        if not isinstance(pending, Mapping):
            return False
        requested = _timestamp(pending.get("requested_at"))
        if requested is None:
            raise DiagnosticAccountError("diagnostic pending exit is invalid")
        side = "sell" if position["direction"] == "long" else "buy"
        evidence = self.first_executable_quote_after(
            quote_rows, symbol=str(position["symbol"]), after=requested,
            visible_by=visible_by, side=side)
        if evidence is None:
            return False
        self._close(
            position, event_key=event_key, action="exit_after_data_gap",
            reason=str(pending.get("reason") or "data_discontinuity"),
            at=_timestamp(evidence.get("available_at")) or visible_by,
            reference_price=float(evidence["price"]), executable_quote=True,
            evidence={**dict(evidence), "late_data_gap": True,
                      "requested_at": requested.isoformat()},
            tie_broken=bool(pending.get("tie_broken")),
            gap_fill=bool(pending.get("gap_fill")))
        return True

    def _apply_due_deadline(self, position: dict[str, Any], *, event_key: str,
                            visible_by: datetime,
                            quote_rows: Sequence[Mapping[str, Any]]) -> bool:
        deadline = position.get("deadline")
        deadline_at = (_timestamp(deadline.get("timestamp"))
                       if isinstance(deadline, Mapping) else None)
        if deadline_at is None or visible_by < deadline_at:
            return False
        side = "sell" if position["direction"] == "long" else "buy"
        evidence = self.executable_quote(
            quote_rows, symbol=str(position["symbol"]), at=deadline_at,
            side=side)
        reason = str(deadline.get("reason") or "max_hold")
        if evidence is not None:
            self._close(
                position, event_key=event_key, action=f"exit_{reason}",
                reason=reason, at=deadline_at,
                reference_price=float(evidence["price"]),
                executable_quote=True, evidence=evidence,
                tie_broken=False, gap_fill=False)
            return True
        self._defer_market_exit(
            position, event_key=event_key, reason=reason, at=deadline_at,
            evidence={
                "source": "deadline_exit",
                "deadline": deadline_at.isoformat(),
                "quote_status": "missing_or_stale",
                "required_provider": _canonical_identity(
                    self.policy.equity_provider),
                "required_feed": _canonical_identity(self.policy.equity_feed),
                "required_source_mode": "forward_observed",
            }, tie_broken=False, gap_fill=False)
        return self._complete_pending_exit(
            position, event_key=event_key, visible_by=visible_by,
            quote_rows=quote_rows)

    def advance_completed_bar(self, event: Mapping[str, Any], *,
                              quote_rows: Sequence[Mapping[str, Any]]) -> None:
        symbol = str(event.get("symbol") or "").upper()
        event_key = str(event.get("event_key") or "")
        if not symbol or not event_key:
            raise DiagnosticAccountError("diagnostic bar identity is unavailable")
        expected_feed = _canonical_identity(self.policy.equity_feed)
        expected_provider = _canonical_identity(self.policy.equity_provider)
        if (str(event.get("source_mode") or "forward_observed").strip().lower()
                != "forward_observed" or
                _canonical_identity(event.get("feed")) != expected_feed or
                _canonical_identity(event.get("provider")) != expected_provider):
            return
        available_at = event_available_at(event)
        bar_start = _timestamp(event.get("timestamp"))
        bar_end = _event_end(event)
        if bar_start is None:
            raise DiagnosticAccountError("diagnostic bar timestamp is invalid")
        for position in sorted(self.positions.values(), key=lambda row: str(
                row.get("position_id") or "")):
            if (position.get("status") != "open" or
                    str(position.get("symbol") or "").upper() != symbol):
                continue
            entry_at = _timestamp(position.get("entry_timestamp"))
            if entry_at is None:
                raise DiagnosticAccountError(
                    "diagnostic position entry timestamp is invalid")
            if bar_start < entry_at:
                continue
            if isinstance(position.get("pending_exit"), Mapping):
                self._complete_pending_exit(
                    position, event_key=event_key, visible_by=available_at,
                    quote_rows=quote_rows)
                # Once a market exit is due, later OHLC cannot replace its
                # reason or resting level. It remains open/unpriced until the
                # first causal executable quote is observed.
                continue
            deadline = position.get("deadline")
            deadline_at = (_timestamp(deadline.get("timestamp"))
                           if isinstance(deadline, Mapping) else None)
            if (deadline_at is not None and bar_start >= deadline_at and
                    self._apply_due_deadline(
                        position, event_key=event_key,
                        visible_by=available_at, quote_rows=quote_rows)):
                # Once the deadline precedes the entire bar, its market-exit
                # obligation wins. Later OHLC cannot earn a stop/target exit.
                continue
            transition = completed_bar_exit_transition(
                position.get("exit_state") or {}, event)
            position["exit_state"] = deepcopy(dict(transition["state"]))
            position["last_event_key"] = event_key
            position["last_event_at"] = available_at.isoformat()
            resolved = transition.get("exit")
            if isinstance(resolved, Mapping):
                reason = str(resolved.get("reason") or "unknown")
                if (not bool(resolved.get("gapped")) and
                        reason in {"stop", "target"}):
                    self._close(
                        position, event_key=event_key,
                        action=f"exit_resting_{reason}", reason=reason,
                        at=bar_end, reference_price=float(resolved["price"]),
                        executable_quote=False,
                        evidence={
                            "source": "resting_bracket",
                            "bar_timestamp": bar_start.isoformat(),
                            "bar_end": bar_end.isoformat(),
                            "bar_feed": str(event.get("feed") or ""),
                            "bar_provider": str(event.get("provider") or ""),
                            "bar_source_mode": str(
                                event.get("source_mode") or
                                "forward_observed").strip().lower(),
                            "stop_price": position["exit_state"].get(
                                "active_stop_price"),
                            "target_price": position["exit_state"].get(
                                "target_price"),
                            "modeled_only": True,
                            "liquidity_claim": False,
                        },
                        tie_broken=bool(resolved.get("tie_broken")),
                        gap_fill=False)
                    continue
                side = "sell" if position["direction"] == "long" else "buy"
                evidence = self.executable_quote(
                    quote_rows, symbol=symbol, at=bar_start, side=side)
                if evidence is None:
                    self._defer_market_exit(
                        position, event_key=event_key, reason=reason,
                        at=bar_start,
                        evidence={"source": "gap_exit",
                                  "bar_timestamp": bar_start.isoformat(),
                                  "quote_status": "missing_or_stale",
                                  "required_provider": expected_provider,
                                  "required_feed": expected_feed,
                                  "required_source_mode": "forward_observed"},
                        tie_broken=bool(resolved.get("tie_broken")),
                        gap_fill=True)
                    continue
                self._close(
                    position, event_key=event_key, action=f"exit_gap_{reason}",
                    reason=reason, at=bar_start,
                    reference_price=float(evidence["price"]),
                    executable_quote=True, evidence=evidence,
                    tie_broken=bool(resolved.get("tie_broken")), gap_fill=True)
                continue
            if deadline_at is not None and bar_end >= deadline_at:
                self._apply_due_deadline(
                    position, event_key=event_key, visible_by=available_at,
                    quote_rows=quote_rows)
                continue
            self._touch(position)
        self._revalue(at=available_at, quote_rows=quote_rows)
        self.account["last_event_key"] = event_key
        self.account["last_event_at"] = available_at.isoformat()

    def advance_quote_event(self, event: Mapping[str, Any], *,
                            quote_rows: Sequence[Mapping[str, Any]]) -> None:
        """Apply a newly observed quote to pending exits and liquidation marks."""
        symbol = str(event.get("symbol") or "").upper()
        event_key = str(event.get("event_key") or "")
        if not symbol or not event_key:
            raise DiagnosticAccountError("diagnostic quote identity is unavailable")
        if str(event.get("source_mode") or "forward_observed").strip().lower() \
                != "forward_observed":
            return
        if (_canonical_identity(event.get("feed")) !=
                _canonical_identity(self.policy.equity_feed) or
                _canonical_identity(event.get("provider")) !=
                _canonical_identity(self.policy.equity_provider)):
            return
        available_at = event_available_at(event)
        for position in sorted(self.positions.values(), key=lambda row: str(
                row.get("position_id") or "")):
            if (position.get("status") != "open" or
                    str(position.get("symbol") or "").upper() != symbol):
                continue
            if self._complete_pending_exit(
                    position, event_key=event_key, visible_by=available_at,
                    quote_rows=quote_rows):
                continue
            self._apply_due_deadline(
                position, event_key=event_key, visible_by=available_at,
                quote_rows=quote_rows)
        self._revalue(at=available_at, quote_rows=quote_rows)
        self.account["last_event_key"] = event_key
        self.account["last_event_at"] = available_at.isoformat()

    def open_requested_position(self, *, event: Mapping[str, Any],
                                plan: Mapping[str, Any],
                                quote_rows: Sequence[Mapping[str, Any]]) -> bool:
        symbol = str(event.get("symbol") or plan.get("symbol") or "").upper()
        event_key = str(event.get("event_key") or "")
        direction = str(plan.get("direction") or "").lower()
        quantity = _finite(plan.get("shares", plan.get("contracts")))
        if (not symbol or not event_key or direction not in {"long", "short"} or
                quantity is None or quantity <= 0):
            raise DiagnosticAccountError("diagnostic entry plan is invalid")
        if self.has_open(symbol):
            return False
        entry_at = event_available_at(event)
        side = "buy" if direction == "long" else "sell"
        evidence = self.executable_quote(
            quote_rows, symbol=symbol, at=entry_at, side=side)
        position_id = content_digest({
            "schema": POSITION_SCHEMA,
            "cohort_identity": self.cohort_identity,
            "candidate_id": self.candidate_id,
            "symbol": symbol,
            "entry_event_key": event_key,
        })
        if evidence is None:
            self._record_order(
                position_id=position_id, event_key=event_key,
                action="entry_unpriced", status="unpriced_data_gap", side=side,
                quantity=quantity, reference_price=None, modeled_price=None,
                evidence={"source": "market_entry",
                          "quote_status": "missing_or_stale",
                          "required_provider": _canonical_identity(
                              self.policy.equity_provider),
                          "required_feed": _canonical_identity(
                              self.policy.equity_feed),
                          "required_source_mode": "forward_observed"})
            self.account["last_event_key"] = event_key
            self.account["last_event_at"] = entry_at.isoformat()
            return False
        reference = float(evidence["price"])
        provisional = {
            "symbol": symbol,
            "entry_timestamp": entry_at.isoformat(),
            "notional": float(plan.get("notional") or reference * quantity),
        }
        model = self._cost_model(
            leg="entry", timestamp=entry_at, position=provisional,
            quantity=quantity)
        entry_price = model.execution_price(
            reference, direction, entry=True, executable_quote=True)
        entry_fee = model.fees(
            entry_price, entry_price, quantity, vehicle="equity") / 2.0
        stop = _finite(plan.get("stop_price"))
        target = _finite(plan.get("target_price"))
        if stop is None or target is None:
            raise DiagnosticAccountError("diagnostic entry bracket is invalid")
        try:
            exit_state = initialize_exit_state(
                direction, entry_price, stop, target,
                breakeven_r=plan.get("breakeven_r"),
                trailing_stop_r=plan.get("trailing_stop_r"),
                target_mode=str(plan.get("target_mode") or "fixed_r"),
                target_lookback=plan.get("target_lookback"),
                exit_before_ts=plan.get("exit_before_ts"))
            deadline_contract = exit_deadline(
                entry_at, self.rule_spec,
                force_flat_ts=plan.get("force_flat_ts"))
        except Exception as exc:
            raise DiagnosticAccountError(
                f"diagnostic exit state is invalid: {exc}") from exc
        deadline = (None if deadline_contract is None else {
            "timestamp": datetime.fromtimestamp(
                float(deadline_contract["timestamp"]), UTC).isoformat(),
            "reason": str(deadline_contract["reason"]),
        })
        notional = float(plan.get("notional") or reference * quantity)
        risk_usd = float(plan.get("risk_usd") or
                         abs(entry_price - stop) * quantity)
        position = {
            "schema": POSITION_SCHEMA,
            "position_id": position_id,
            "cohort_identity": self.cohort_identity,
            "candidate_id": self.candidate_id,
            "symbol": symbol,
            "status": "open",
            "direction": direction,
            "quantity": quantity,
            "entry_event_key": event_key,
            "entry_timestamp": entry_at.isoformat(),
            "entry_reference": reference,
            "entry_price": entry_price,
            "entry_fee": entry_fee,
            "entry_evidence": evidence,
            "entry_cost_model": model.as_dict(),
            "mark_status": "unpriced",
            "mark_price": None,
            "mark_evidence": None,
            "last_mark_at": None,
            "unrealized_pnl": None,
            "realized_pnl": 0.0,
            "risk_usd": risk_usd,
            "notional": notional,
            "stop_price": stop,
            "target_price": target,
            "exit_state": exit_state,
            "deadline": deadline,
            "pending_exit": None,
            "late_data_gap": False,
            "late_since": None,
            "plan": deepcopy(dict(plan)),
            "last_event_key": event_key,
            "last_event_at": entry_at.isoformat(),
            "exit_event_key": None,
            "exit_timestamp": None,
            "exit_reference": None,
            "exit_price": None,
            "exit_fee": None,
            "gross_pnl": None,
            "exit_reason": None,
            "canonical_exit_reason": None,
            "tie_broken": False,
            "gap_fill": False,
        }
        order = self._record_order(
            position_id=position_id, event_key=event_key,
            action="entry_market", status="modeled_filled", side=side,
            quantity=quantity, reference_price=reference,
            modeled_price=entry_price, evidence=evidence)
        self._record_fill(
            order=order, event_key=event_key, action="entry_market", side=side,
            quantity=quantity, price=entry_price, fee=entry_fee,
            evidence=evidence)
        self.positions[position_id] = position
        self._touch(position)
        self._revalue(at=entry_at, quote_rows=quote_rows)
        self.account["last_event_key"] = event_key
        self.account["last_event_at"] = entry_at.isoformat()
        return True

    def batch(self) -> dict[str, Any]:
        self.account["open_position_count"] = sum(
            1 for position in self.positions.values()
            if position.get("status") == "open")
        self.account["late_data_gap_count"] = sum(
            1 for position in self.positions.values()
            if position.get("status") == "open" and
            position.get("late_data_gap") is True)
        self.account["state_digest"] = _state_digest(self.account)
        updates = []
        for position_id in sorted(self._touched_positions):
            position = self.positions[position_id]
            position["state_digest"] = _state_digest(position)
            updates.append({
                "previous_state_digest": self._base_position_digests.get(
                    position_id),
                "state": deepcopy(position),
            })
        return {
            "schema": "diagnostic-forward-account-batch.v1",
            "account": {
                "previous_state_digest": self._base_account_digest,
                "state": deepcopy(self.account),
            },
            "positions": updates,
            "orders": deepcopy(self.orders),
            "fills": deepcopy(self.fills),
        }


__all__ = [
    "ACCOUNT_SCHEMA", "POSITION_SCHEMA", "ORDER_SCHEMA", "FILL_SCHEMA",
    "DiagnosticAccountBook", "DiagnosticAccountError", "content_digest",
    "event_available_at", "new_account_state", "validate_account_state",
    "validate_position_state",
]
