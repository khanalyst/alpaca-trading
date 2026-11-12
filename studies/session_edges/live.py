"""Forward shadow, paper orders and live orders for the session-edge book.

Usage, from the repository root::

    python -m studies.session_edges.live run                        # shadow only: no order is ever sent
    python -m studies.session_edges.live run --book BOOK.json       # shadow the legs of a discovery run
    python -m studies.session_edges.live preflight --paper-orders   # read-only go / no-go checks
    python -m studies.session_edges.live run --paper-orders         # also trade the paper account
    python -m studies.session_edges.live run --mode live --live-orders --confirm LIVE --max-notional 2000
    python -m studies.session_edges.live halt | resume | flatten    # operator controls
    python -m studies.session_edges.live report                     # forward statistics so far

The runner polls the broker clock. At each decision time it reads completed
one-minute bars, calls the same decision function the study simulator calls,
and records what a marketable order would pay at the live quote (the ask to
buy, the bid to sell). After the close it replays the day's bars through the
simulator and stores both numbers, so implementation drift and real costs are
measured every session.

Orders, when enabled, follow the shadow book through a small state machine
that never assumes anything about the broker:

* one order at a time per leg, written to disk before it is sent and looked
  up by its client order id until the broker reports a final state;
* the broker's positions are read every poll; a leg that holds fewer shares
  than the runner believes adopts the smaller number, and a leg whose symbol
  holds shares the runner did not buy stops opening positions;
* a position never crosses zero in one order, never exceeds the leg's share
  of equity or ``--max-notional``, and is never opened after the daily loss
  limit, a ``HALT`` file or a ``FLATTEN`` request;
* intraday legs are closed at 15:58 and again on the next open if anything
  was left behind.

Paper orders need ``--paper-orders`` and the paper endpoint. Live orders need
``--mode live --live-orders --confirm LIVE``, a ``--max-notional`` cap, the
``ALPACA_LIVE_ENABLE=true`` environment guard and a passing preflight. Both
take the runtime lock the trader takes, so the two never share an account.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import sys
import threading
import time as time_module
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from studies.liquid_reversion.bars import NEW_YORK
from studies.session_edges import stats
from studies.session_edges.data import (MAX_SESSION_GAP_DAYS, MIN_OBSERVED, MINUTES,
                                        OPEN_MINUTE_OF_DAY, MinuteRow, Session, build_session)
from studies.session_edges.strategies import (CLOSE_REVERSION, ENTER, EXIT, FLAT, LONG, NOISE_BAND,
                                              OPENING_RANGE, SHORT, CloseReversionRules, Fill,
                                              NoiseBandRules, close_reversion_action,
                                              internal_bar_strength, noise_band_bounds, noise_band_sigma,
                                              noise_band_target, simulate_noise_band,
                                              simulate_opening_range)

SCHEMA = "session-edges-shadow.v2"
BOOK_SCHEMA = "session-edges-book.v1"
NOISE, REVERSION, OPENING = "noise_band_momentum", "close_weakness_reversion", "opening_range_breakout"
ORDERS_NONE, ORDERS_PAPER, ORDERS_LIVE = "none", "paper", "live"
FLAT_MINUTE = 388                # 15:58 New York: intraday legs leave with a market order
GRACE_SECONDS = 4.0              # wait this long after a boundary for the completed bar
LATE_BAR_SECONDS = 45.0          # then accept the last traded price for a quiet minute
MAX_DELAY_SECONDS = 240.0        # a decision this late is skipped, never acted on stale
QUOTE_FIRST_WINDOW_SECONDS = 5.0
QUOTE_MAX_AGE_SECONDS = 30.0
HISTORY_CALENDAR_DAYS = 35
DAILY_LOSS_LIMIT = 0.02          # orders: no new entries after this fall in equity today
ORDER_TIMEOUT_SECONDS = 60.0     # a market order still open after this is cancelled
ORDER_MISSING_SECONDS = 60.0     # an order the broker never saw is forgotten after this
MAX_ENTRY_ATTEMPTS = 3           # orders that open or add to a position, per decision
MAX_FLATTEN_ATTEMPTS = 5         # orders that reduce a position, per decision
ORDER_PREFIX = "sedge"
HALT_FILE, FLATTEN_FILE = "HALT", "FLATTEN"
# States after which an order can no longer fill. "stopped" and "suspended" are
# not final at the broker, so they run into the timeout and are cancelled.
_FINAL_UNFILLED = frozenset({"canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced"})


@dataclass(frozen=True, slots=True)
class Leg:
    """One rule on one symbol. ``weight`` is the share of equity an order may use."""

    rule: str
    symbol: str
    weight: float = 0.0
    trade: bool = False
    params: Mapping[str, Any] = field(default_factory=dict)
    tier: str = "registered"

    @property
    def key(self) -> str:
        return f"{self.rule}:{self.symbol}"

    def noise_rules(self, *, allow_short: bool = True) -> NoiseBandRules:
        given = self.params
        sides = tuple(given.get("sides", NOISE_BAND.sides))
        if not allow_short:
            sides = tuple(side for side in sides if side == "long")
        return NoiseBandRules(
            lookback_sessions=int(given.get("lookback_sessions", NOISE_BAND.lookback_sessions)),
            decision_step_minutes=int(given.get("decision_step_minutes", NOISE_BAND.decision_step_minutes)),
            first_decision_minute=int(given.get("first_decision_minute", NOISE_BAND.first_decision_minute)),
            last_decision_minute=int(given.get("last_decision_minute", NOISE_BAND.last_decision_minute)),
            band_multiple=float(given.get("band_multiple", NOISE_BAND.band_multiple)),
            sides=sides, flat_minute=FLAT_MINUTE)

    def reversion_rules(self) -> CloseReversionRules:
        given = self.params
        return CloseReversionRules(
            decision_minute=int(given.get("decision_minute", CLOSE_REVERSION.decision_minute)),
            enter_below=float(given.get("enter_below", CLOSE_REVERSION.enter_below)),
            exit_above=float(given.get("exit_above", CLOSE_REVERSION.exit_above)),
            max_sessions=int(given.get("max_sessions", CLOSE_REVERSION.max_sessions)))


# The paper candidates of the registered-rule study, plus the shadow-only
# tier. The QQQ reversion leg is recorded only because the QQQ momentum leg
# already trades that symbol and one traded leg owns one symbol.
DEFAULT_LEGS = (
    Leg(NOISE, "QQQ", 0.25, True),
    Leg(REVERSION, "SPY", 0.25, True),
    Leg(REVERSION, "QQQ"),
    Leg(OPENING, "QQQ"),
)


def load_book(path: str | Path) -> tuple[Leg, ...]:
    """The legs of an edge book written by ``discover.py``."""
    book = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(book, Mapping) or book.get("schema") != BOOK_SCHEMA:
        raise ValueError(f"{path}: not a {BOOK_SCHEMA} edge book")
    legs: list[Leg] = []
    for row in book.get("legs") or []:
        rule = str(row.get("rule"))
        if rule not in (NOISE, REVERSION):
            raise ValueError(f"{path}: the runner cannot trade rule {rule!r}")
        weight = float(row.get("weight") or 0.0)
        if not 0.0 <= weight <= 1.0:
            raise ValueError(f"{path}: weight out of range for {rule} {row.get('symbol')}")
        legs.append(Leg(rule, str(row["symbol"]).upper(), weight, bool(row.get("trade")) and weight > 0,
                        dict(row.get("params") or {}), str(row.get("tier") or "")))
    if not legs:
        raise ValueError(f"{path}: the edge book has no legs")
    return tuple(legs)


def _number(value: Any) -> float:
    return float(value)


def _minute_rows(bars: Sequence[Any]) -> dict[date, list[MinuteRow]]:
    """Group provider bars into ``(minute, o, h, l, c, v)`` rows per New York date."""
    out: dict[date, list[MinuteRow]] = {}
    for bar in bars:
        stamp = bar.timestamp
        if stamp.tzinfo is None:
            raise ValueError("bars must carry aware timestamps")
        local = stamp.astimezone(NEW_YORK)
        minute = local.hour * 60 + local.minute - OPEN_MINUTE_OF_DAY
        if 0 <= minute < MINUTES:
            out.setdefault(local.date(), []).append(
                (minute, _number(bar.open), _number(bar.high), _number(bar.low), _number(bar.close),
                 _number(getattr(bar, "volume", 0) or 0)))
    return out


class Partial:
    """Today's completed bars so far, forward-filled like a study session."""

    def __init__(self, rows: Sequence[MinuteRow]) -> None:
        self.rows = {row[0]: row for row in sorted(rows, key=lambda item: item[0])}
        self.latest = max(self.rows) if self.rows else -1

    def session(self, day: date, upto: int) -> Session | None:
        """A session whose first ``upto`` minutes are real; later minutes repeat the last price."""
        rows = [row for minute, row in self.rows.items() if minute < upto]
        return build_session(day, rows, min_observed=1) if rows else None


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str), encoding="utf-8")
    os.replace(temporary, path)


def orphaned_shares(state: Mapping[str, Any], legs: Sequence[Leg]) -> dict[str, int]:
    """Shares the runner bought for legs that the current book no longer trades."""
    traded = {leg.key for leg in legs if leg.trade}
    return {key: int(book.get("shares", 0)) for key, book in (state.get("orders") or {}).items()
            if int(book.get("shares", 0)) != 0 and key not in traded}


class ShadowRunner:
    """One poll at a time; all state lives in ``directory`` so a restart resumes the session."""

    def __init__(self, provider: Any, *, directory: str | Path, legs: Sequence[Leg] = DEFAULT_LEGS,
                 orders: str = ORDERS_NONE, max_notional: float | None = None, allow_short: bool = True,
                 daily_loss_limit: float = DAILY_LOSS_LIMIT, min_observed: int = MIN_OBSERVED) -> None:
        if orders not in (ORDERS_NONE, ORDERS_PAPER, ORDERS_LIVE):
            raise ValueError("orders must be none, paper or live")
        self.provider = provider
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.legs = tuple(legs)
        self.orders = orders
        self.max_notional = None if max_notional is None else float(max_notional)
        self.allow_short = bool(allow_short)
        self.daily_loss_limit = float(daily_loss_limit)
        self.min_observed = min_observed
        if len({leg.key for leg in self.legs}) != len(self.legs):
            raise ValueError("duplicate leg")
        traded = [leg.symbol for leg in self.legs if leg.trade]
        if len(set(traded)) != len(traded):
            raise ValueError("each traded leg needs its own symbol")
        if any(leg.trade and leg.rule == OPENING for leg in self.legs):
            raise ValueError("the opening range leg is recorded only")
        if orders != ORDERS_NONE:
            mode, paper = getattr(provider, "mode", None), getattr(provider, "paper", None)
            if orders == ORDERS_PAPER and not (mode == "paper" and paper is True):
                raise RuntimeError("paper orders need a provider on the paper endpoint")
            if orders == ORDERS_LIVE and not (mode == "live" and paper is False):
                raise RuntimeError("live orders need a provider on the live endpoint")
            if orders == ORDERS_LIVE and not (self.max_notional and self.max_notional > 0):
                raise ValueError("live orders need a positive max_notional cap")
            if math.fsum(leg.weight for leg in self.legs if leg.trade) > 1.0 + 1e-9:
                raise ValueError("traded leg weights exceed the account equity")
            if not 0 < self.daily_loss_limit < 1:
                raise ValueError("daily_loss_limit must be a fraction between 0 and 1")
        self.state_path = self.directory / "state.json"
        self.journal_path = self.directory / "journal.jsonl"
        self.state = self._load_state()
        orphans = orphaned_shares(self.state, self.legs) if orders != ORDERS_NONE else {}
        if orphans:
            raise RuntimeError(
                f"the state holds shares for legs this book does not trade: {orphans}; "
                "run with the previous book and flatten them, or keep those legs traded")
        self.history: dict[str, list[Session]] = {}
        self._history_day: date | None = None

    # ---- persistence -----------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("schema") == SCHEMA:
                state.setdefault("orders", {})
                return state
        return {"schema": SCHEMA, "day": None, "legs": {}, "carry": {}, "orders": {}, "order_seq": 0}

    def _save(self) -> None:
        _atomic_write(self.state_path, self.state)

    def _record(self, kind: str, now: datetime, **detail: Any) -> dict[str, Any]:
        event = {"schema": SCHEMA, "kind": kind, "ts": now.astimezone(timezone.utc).isoformat(), **detail}
        with self.journal_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    # ---- market data -----------------------------------------------------
    def _symbols(self) -> list[str]:
        return sorted({leg.symbol for leg in self.legs})

    def _noise_rules(self, leg: Leg) -> NoiseBandRules:
        """A traded leg may only short when shorting is allowed; a recorded leg keeps its own sides."""
        return leg.noise_rules(allow_short=self.allow_short or not self._trading(leg))

    def _load_history(self, day: date, open_at: datetime) -> None:
        start = open_at - timedelta(days=HISTORY_CALENDAR_DAYS)
        bars = self.provider.bars(self._symbols(), "1Min", start=start, end=open_at)
        self.history = {}
        for symbol in self._symbols():
            sessions = []
            for session_day, rows in sorted(_minute_rows(bars.get(symbol, [])).items()):
                if session_day >= day:
                    continue
                session = build_session(session_day, rows, min_observed=self.min_observed)
                if session is not None:
                    sessions.append(session)
            self.history[symbol] = sessions
        self._history_day = day

    def _quote(self, symbol: str, now: datetime, side: int, fallback: float) -> tuple[float, str]:
        """The price a marketable order would pay now: the ask to buy, the bid to sell."""
        # A liquid ETF prints many quotes a second, so ask for a short window
        # first and widen it only when that window is empty.
        for window in (QUOTE_FIRST_WINDOW_SECONDS, QUOTE_MAX_AGE_SECONDS):
            try:
                rows = self.provider.quotes([symbol], start=now - timedelta(seconds=window),
                                            end=now).get(symbol, [])
            except Exception:  # noqa: BLE001 - a missing quote must not stop the session
                rows = []
            for quote in reversed(rows):
                price = quote.ask if side == LONG else quote.bid
                if price is not None and float(price) > 0:
                    return float(price), "quote"
        return fallback, "bar_fallback"

    # ---- orders ----------------------------------------------------------
    def _trading(self, leg: Leg) -> bool:
        return self.orders != ORDERS_NONE and leg.trade

    def _order_book(self, leg: Leg) -> dict[str, Any]:
        return self.state["orders"].setdefault(leg.key, {
            "shares": 0, "desired": 0, "pending": None, "attempts": 0, "disabled": None, "quote": None})

    def _size(self, leg: Leg, price: float) -> int:
        """Whole shares for one leg: its share of equity, under the absolute cap."""
        if not (self._trading(leg) and leg.weight > 0 and price > 0):
            return 0
        notional = float(self.provider.account().equity) * leg.weight
        if self.max_notional is not None:
            notional = min(notional, self.max_notional)
        return max(0, int(notional / price))

    def _sized(self, leg: Leg, now: datetime, price: float) -> int:
        shares = self._size(leg, price)
        if self._trading(leg) and shares == 0:
            self._record("size_zero", now, leg=leg.key, price=price, weight=leg.weight,
                         max_notional=self.max_notional)
        return shares

    def _want(self, leg: Leg, now: datetime, desired: int, quote: float | None) -> None:
        """Set the shares the order side should hold; ``_align`` works towards it."""
        if not self._trading(leg):
            return
        book = self._order_book(leg)
        if int(book["desired"]) != int(desired):
            book.update({"desired": int(desired), "attempts": 0, "quote": quote})
            book.pop("gave_up", None)
            self._record("target_shares", now, leg=leg.key, desired=int(desired))

    def _halted(self, now: datetime) -> bool:
        """Whether new or larger positions are forbidden right now."""
        if self.orders == ORDERS_NONE:
            return False
        if (self.directory / HALT_FILE).exists() or (self.directory / FLATTEN_FILE).exists():
            if not self.state.get("halt_noted"):
                self.state["halt_noted"] = True
                self._record("entries_halted", now, reason="operator halt file")
            return True
        self.state.pop("halt_noted", None)
        if self.state.get("entries_halted"):
            return True
        try:
            equity = float(self.provider.account().equity)
        except Exception as exc:  # noqa: BLE001 - an unknown equity must not open a position
            self._record("entries_halted", now, reason=f"equity unavailable: {str(exc)[:120]}")
            return True
        start = self.state.setdefault("equity_at_open", equity)
        if start > 0 and (start - equity) / start >= self.daily_loss_limit:
            self.state["entries_halted"] = True
            self._record("entries_halted", now, reason="daily loss limit", equity=equity,
                         equity_at_open=start)
            return True
        return False

    def _submit(self, leg: Leg, now: datetime, day: date, step: int) -> None:
        from agent.alpaca_domain import OrderRequest
        book = self._order_book(leg)
        self.state["order_seq"] = int(self.state.get("order_seq", 0)) + 1
        client_id = f"{ORDER_PREFIX}-{leg.rule[:5]}-{leg.symbol}-{day:%Y%m%d}-{self.state['order_seq']:04d}"
        book["pending"] = {"client_order_id": client_id, "step": int(step), "ts": now.timestamp(),
                           "quote": book.get("quote"), "submitted": False}
        self._save()                     # the intent is on disk before the broker sees the order
        try:
            self.provider.submit_order(OrderRequest(
                symbol=leg.symbol, qty=Decimal(abs(step)), side="buy" if step > 0 else "sell",
                type="market", time_in_force="day", client_order_id=client_id))
        except Exception as exc:  # noqa: BLE001 - record it and let the retry limit decide
            self._record("order_failed", now, leg=leg.key, client_order_id=client_id, shares=int(step),
                         error=str(exc)[:200])
            book["pending"] = None
            return
        book["pending"]["submitted"] = True
        self._record("order_submitted", now, leg=leg.key, client_order_id=client_id, shares=int(step),
                     mode=self.orders)

    def _sync_pending(self, leg: Leg, now: datetime) -> None:
        """Follow the outstanding order to a final state; only then do the shares change."""
        book = self._order_book(leg)
        pending = book["pending"]
        if not pending:
            return
        age = now.timestamp() - float(pending["ts"])
        try:
            rows = self.provider.orders(client_order_id=pending["client_order_id"])
        except Exception as exc:  # noqa: BLE001 - keep waiting; never guess a fill
            if not pending.get("lookup_error"):
                pending["lookup_error"] = True
                self._record("order_lookup_failed", now, leg=leg.key,
                             client_order_id=pending["client_order_id"], error=str(exc)[:200])
            return
        if not rows:
            if age > ORDER_MISSING_SECONDS:
                self._record("order_missing", now, leg=leg.key, client_order_id=pending["client_order_id"])
                book["pending"] = None
            return
        order = rows[0]
        status = str(order.status).lower()
        sign = 1 if int(pending["step"]) > 0 else -1
        filled = int(Decimal(str(order.filled_qty or 0)))
        if status == "filled" or status in _FINAL_UNFILLED:
            if filled:
                book["shares"] = int(book["shares"]) + sign * filled
                price = float(order.filled_avg_price) if order.filled_avg_price is not None else None
                quote = pending.get("quote")
                slippage = (sign * (price / quote - 1.0) * 1e4) if price and quote else None
                self._record("fill", now, leg=leg.key, client_order_id=pending["client_order_id"],
                             shares=sign * filled, price=price, quote=quote, slippage_bps=slippage,
                             holding=int(book["shares"]))
            if status != "filled":
                self._record("order_dead", now, leg=leg.key, client_order_id=pending["client_order_id"],
                             status=status, filled=sign * filled)
            book["pending"] = None
        elif age > ORDER_TIMEOUT_SECONDS and not pending.get("cancel_requested"):
            pending["cancel_requested"] = True
            try:
                self.provider.cancel_order(order.id)
            except Exception as exc:  # noqa: BLE001
                self._record("order_cancel_failed", now, leg=leg.key, error=str(exc)[:200])
            self._record("order_timeout", now, leg=leg.key, client_order_id=pending["client_order_id"],
                         status=status)

    def _reconcile(self, now: datetime) -> None:
        """Compare what the runner believes it holds with the broker, every poll."""
        held: dict[str, int] = {}
        for position in self.provider.positions():
            quantity = int(Decimal(str(position.qty)))
            held[str(position.symbol).upper()] = quantity if position.side == "long" else -quantity
        for leg in self.legs:
            if not self._trading(leg):
                continue
            book = self._order_book(leg)
            if book["pending"]:
                continue
            own, broker = int(book["shares"]), held.get(leg.symbol, 0)
            book["broker"] = broker
            if own == broker:
                if book.get("disabled") == "foreign_position":
                    book["disabled"] = None
                    self._record("position_matched", now, leg=leg.key, shares=own)
                book.pop("mismatch", None)
                continue
            if book.get("mismatch") != [own, broker]:
                book["mismatch"] = [own, broker]
                self._record("position_mismatch", now, leg=leg.key, expected=own, broker=broker)
            if own != 0 and own * broker >= 0 and abs(broker) < abs(own):
                book["shares"] = broker          # some of the runner's shares are gone: adopt what is left
                book["desired"] = broker if abs(int(book["desired"])) > abs(broker) else book["desired"]
            else:
                book["disabled"] = "foreign_position"   # shares the runner did not buy: never touch them

    def _align(self, leg: Leg, now: datetime, day: date) -> None:
        """Send at most one order that moves the leg towards its desired shares."""
        book = self._order_book(leg)
        if book["pending"]:
            return
        shares, desired = int(book["shares"]), int(book["desired"])
        if shares == desired:
            return
        crossing = shares != 0 and desired != 0 and (shares > 0) != (desired > 0)
        step = -shares if crossing else desired - shares
        opening = abs(shares + step) > abs(shares)
        if opening:
            if book.get("disabled") or self._halted(now):
                self._record("entry_blocked", now, leg=leg.key,
                             reason=book.get("disabled") or "entries halted", desired=desired)
                book["desired"] = shares
                return
            if not self.allow_short and shares + step < 0:
                self._record("entry_blocked", now, leg=leg.key, reason="short selling is off", desired=desired)
                book["desired"] = shares
                return
        limit = MAX_ENTRY_ATTEMPTS if opening else MAX_FLATTEN_ATTEMPTS
        if int(book["attempts"]) >= limit:
            if not book.get("gave_up"):
                book["gave_up"] = True
                self._record("order_gave_up", now, leg=leg.key, opening=opening, shares=shares, desired=desired)
                if opening:
                    book["desired"] = shares
                else:
                    book["disabled"] = "exit_failed"     # the operator must look at this leg
            return
        book["attempts"] = int(book["attempts"]) + 1
        self._submit(leg, now, day, step)

    def _sync_orders(self, now: datetime, day: date) -> None:
        if self.orders == ORDERS_NONE:
            return
        for leg in self.legs:
            if self._trading(leg):
                self._sync_pending(leg, now)
        self._reconcile(now)
        flatten = self.directory / FLATTEN_FILE
        if flatten.exists():
            for leg in self.legs:
                if self._trading(leg):
                    self._want(leg, now, 0, None)
            books = [self._order_book(leg) for leg in self.legs if self._trading(leg)]
            if all(int(book["shares"]) == 0 and not book["pending"] for book in books):
                (self.directory / HALT_FILE).write_text("set by a completed flatten request\n", encoding="utf-8")
                flatten.unlink()
                self._record("flatten_complete", now)

    # ---- session lifecycle ----------------------------------------------
    def _start_day(self, day: date, now: datetime, close_at: datetime | None) -> None:
        if self.state.get("day") not in (None, day.isoformat()):
            self._finish_day(now)
        full = close_at is not None and close_at.astimezone(NEW_YORK).time().hour == 16
        self.state.update({"day": day.isoformat(), "full_session": bool(full), "summarised": False,
                           "entries_halted": False, "legs": {}})
        self.state.pop("equity_at_open", None)
        for leg in self.legs:
            self.state["legs"][leg.key] = {"position": FLAT, "entry": None, "entry_minute": None,
                                           "done": [], "fills": []}
            if self._trading(leg):
                orders = self._order_book(leg)
                orders["attempts"] = 0               # a new session may try again
                orders.pop("gave_up", None)
                if orders.get("disabled") == "exit_failed":
                    orders["disabled"] = None
                if leg.rule != REVERSION:
                    self._want(leg, now, 0, None)    # nothing intraday is carried into a new session
        if self.orders != ORDERS_NONE:
            try:
                self.state["equity_at_open"] = float(self.provider.account().equity)
            except Exception:  # noqa: BLE001 - _halted fails closed when equity stays unknown
                pass
        self._record("session_start", now, day=day.isoformat(), full_session=bool(full),
                     legs=[leg.key for leg in self.legs], orders=self.orders)
        self._save()

    def _fill(self, leg: Leg, now: datetime, day: date, minute: int, target: int, reference: float,
              reason: str) -> None:
        """Move a leg's shadow book to ``target`` at the live quote and set the order side to match."""
        book = self.state["legs"][leg.key]
        position = int(book["position"])
        if target == position:
            return
        side = LONG if target > position else SHORT
        price, source = self._quote(leg.symbol, now, side, reference)
        if position != FLAT:
            book["fills"].append([position, book["entry_minute"], book["entry"], minute, price])
        book.update({"position": target, "entry": price if target != FLAT else None,
                     "entry_minute": minute if target != FLAT else None})
        self._want(leg, now, target * self._sized(leg, now, price) if target != FLAT else 0, price)
        self._record("decision", now, leg=leg.key, minute=minute, target=target, price=price,
                     price_source=source, reference=reference, reason=reason)

    def _ready(self, minute: int, elapsed: float, latest_bar: int) -> str:
        """Whether the decision for ``minute`` can be taken now: ``wait``, ``go`` or ``missed``."""
        due = minute * 60.0
        if elapsed < due + GRACE_SECONDS:
            return "wait"
        if elapsed > due + MAX_DELAY_SECONDS:
            return "missed"
        if latest_bar >= minute - 1 or elapsed >= due + LATE_BAR_SECONDS:
            return "go"
        return "wait"

    def _noise_band(self, leg: Leg, now: datetime, day: date, elapsed: float, partial: Partial) -> None:
        book = self.state["legs"][leg.key]
        history = self.history.get(leg.symbol, [])
        rules = self._noise_rules(leg)
        for minute in rules.decision_minutes():
            if minute in book["done"] or minute >= FLAT_MINUTE:
                continue
            ready = self._ready(minute, elapsed, partial.latest)
            if ready == "wait":
                break
            book["done"].append(minute)
            session = partial.session(day, minute)
            sigma = noise_band_sigma(history, minute, rules.lookback_sessions)
            if ready == "missed" or session is None or sigma is None:
                self._record("decision_skipped", now, leg=leg.key, minute=minute,
                             reason="late" if ready == "missed" else "history_or_bars_unavailable")
                continue
            prior = (history[-1].close[-1]
                     if history and (day - history[-1].day).days <= MAX_SESSION_GAP_DAYS else None)
            upper, lower = noise_band_bounds(session.open[0], prior, sigma, rules.band_multiple)
            price = session.close[minute - 1]
            target = noise_band_target(int(book["position"]), price, upper, lower, rules.sides)
            if target == int(book["position"]):     # nothing to do; keep what the decision saw
                self._record("decision_unchanged", now, leg=leg.key, minute=minute, position=target,
                             price=price, lower=lower, upper=upper)
            self._fill(leg, now, day, minute, target, price,
                       f"price {price:.4f} band [{lower:.4f}, {upper:.4f}]")

    def _flatten_intraday(self, leg: Leg, now: datetime, day: date, elapsed: float, partial: Partial) -> None:
        book = self.state["legs"][leg.key]
        if elapsed < FLAT_MINUTE * 60.0 or FLAT_MINUTE in book["done"]:
            return
        book["done"].append(FLAT_MINUTE)
        session = partial.session(day, FLAT_MINUTE)
        if int(book["position"]) != FLAT and session is not None:
            self._fill(leg, now, day, FLAT_MINUTE, FLAT, session.close[FLAT_MINUTE - 1], "end of day")
        # Whatever the shadow book says, an intraday leg holds no shares past
        # the close, and an exit that failed earlier gets a fresh set of attempts.
        self._want(leg, now, 0, None)
        if self._trading(leg):
            orders = self._order_book(leg)
            if int(orders["shares"]) != 0:
                orders["attempts"] = 0
                orders.pop("gave_up", None)
                if orders.get("disabled") == "exit_failed":
                    orders["disabled"] = None

    def _opening_range(self, leg: Leg, now: datetime, day: date, elapsed: float, partial: Partial) -> None:
        """Recorded only: enter after the first candle, then track the stop on completed bars."""
        book = self.state["legs"][leg.key]
        width = OPENING_RANGE.minutes
        if width not in book["done"]:
            ready = self._ready(width, elapsed, partial.latest)
            if ready == "wait":
                return
            book["done"].append(width)
            session = partial.session(day, width)
            if ready == "missed" or session is None or partial.latest < width - 1:
                self._record("decision_skipped", now, leg=leg.key, minute=width, reason="late_or_no_bars")
                return
            first_open, first_close = session.open[0], session.close[width - 1]
            if first_close == first_open:
                return
            side = LONG if first_close > first_open else SHORT
            self._fill(leg, now, day, width, side, first_close, "first candle direction")
            entry = book["entry"]
            stop = min(session.low[:width]) if side == LONG else max(session.high[:width])
            if entry is None or not side * (entry - stop) > 0:
                self._fill(leg, now, day, width, FLAT, first_close, "no room to the stop")
                return
            risk = side * (entry - stop)
            book["stop"] = stop
            book["target"] = entry + side * OPENING_RANGE.target_r * risk
            book["checked"] = width - 1
            return
        position = int(book["position"])
        if position == FLAT or "stop" not in book:
            return
        for minute in range(int(book["checked"]) + 1, min(partial.latest, FLAT_MINUTE - 1) + 1):
            row = partial.rows.get(minute)
            book["checked"] = minute
            if row is None:
                continue
            _, _, high, low, close, _ = row
            hit_stop = low <= book["stop"] if position == LONG else high >= book["stop"]
            hit_target = high >= book["target"] if position == LONG else low <= book["target"]
            if hit_stop or hit_target:
                self._fill(leg, now, day, minute + 1, FLAT, close, "stop" if hit_stop else "target")
                return

    def _close_reversion(self, leg: Leg, now: datetime, day: date, elapsed: float, partial: Partial) -> None:
        book = self.state["legs"][leg.key]
        rules = leg.reversion_rules()
        minute = rules.decision_minute
        if minute in book["done"]:
            return
        ready = self._ready(minute, elapsed, partial.latest)
        if ready == "wait":
            return
        book["done"].append(minute)
        carry = self.state["carry"].setdefault(leg.key, {"in_position": False, "held": 0, "entry": None,
                                                         "entry_day": None})
        session = partial.session(day, minute)
        if ready == "missed" or session is None:
            self._record("decision_skipped", now, leg=leg.key, minute=minute, reason="late_or_no_bars")
            return
        price = session.close[minute - 1]
        strength = internal_bar_strength(max(session.high[:minute]), min(session.low[:minute]), price)
        held = int(carry["held"]) + 1 if carry["in_position"] else 0
        action = close_reversion_action(bool(carry["in_position"]), held, strength, rules)
        if carry["in_position"]:
            carry["held"] = held
        if action == ENTER:
            fill, source = self._quote(leg.symbol, now, LONG, price)
            carry.update({"in_position": True, "held": 0, "entry": fill, "entry_day": day.isoformat()})
            self._want(leg, now, self._sized(leg, now, fill), fill)
        elif action == EXIT:
            fill, source = self._quote(leg.symbol, now, SHORT, price)
            self._record("round_trip", now, leg=leg.key, entry_day=carry["entry_day"], entry=carry["entry"],
                         exit=fill, gross=fill / carry["entry"] - 1.0 if carry["entry"] else None,
                         sessions_held=held)
            carry.update({"in_position": False, "held": 0, "entry": None, "entry_day": None})
            self._want(leg, now, 0, fill)
        else:
            fill, source = price, "none"
        self._record("decision", now, leg=leg.key, minute=minute, action=action, strength=strength,
                     price=fill, price_source=source, reference=price, sessions_held=held)

    # ---- end of day ------------------------------------------------------
    def _replay(self, leg: Leg, today: Session) -> tuple[float | None, int | None]:
        """What the study simulator makes of today's bars, for the fidelity comparison."""
        history = [session for session in self.history.get(leg.symbol, []) if session.day < today.day]
        if leg.rule == NOISE:
            rules = self._noise_rules(leg)
            if len(history) < rules.lookback_sessions:
                return None, None
            result = simulate_noise_band([*history, today], rules)[-1]
        elif leg.rule == OPENING:
            result = simulate_opening_range([today])[0]
        else:
            return None, None       # a multi-session position has no single-day replay
        return result.gross, result.round_trips

    def _finish_day(self, now: datetime) -> None:
        if self.state.get("summarised") or not self.state.get("day"):
            return
        day = date.fromisoformat(self.state["day"])
        summary: dict[str, Any] = {}
        todays: dict[str, Session | None] = {}
        try:
            open_at = datetime.combine(day, datetime.min.time(), NEW_YORK) + timedelta(minutes=OPEN_MINUTE_OF_DAY)
            bars = self.provider.bars(self._symbols(), "1Min", start=open_at,
                                      end=open_at + timedelta(minutes=MINUTES))
            for symbol in self._symbols():
                rows = _minute_rows(bars.get(symbol, [])).get(day, [])
                todays[symbol] = build_session(day, rows, min_observed=self.min_observed)
        except Exception as exc:  # noqa: BLE001 - the shadow numbers are still worth keeping
            self._record("replay_unavailable", now, error=str(exc)[:200])
        for leg in self.legs:
            book = self.state["legs"].get(leg.key, {})
            row: dict[str, Any] = {"rule": leg.rule, "symbol": leg.symbol, "traded": self._trading(leg)}
            if leg.rule == REVERSION:
                carry = self.state["carry"].get(leg.key, {})
                row["in_position"] = bool(carry.get("in_position"))
            else:
                fills = [Fill(int(f[0]), int(f[1]), float(f[2]), int(f[3]), float(f[4]))
                         for f in book.get("fills", [])]
                row["shadow_gross"] = math.fsum(fill.gross for fill in fills)
                row["shadow_round_trips"] = len(fills)
            if self._trading(leg):
                orders = self._order_book(leg)
                row["shares"] = int(orders["shares"])
                if orders.get("disabled"):
                    row["orders_disabled"] = orders["disabled"]
            today = todays.get(leg.symbol)
            if today is not None and self.state.get("full_session"):
                gross, trips = self._replay(leg, today)
                if gross is not None:
                    row["replay_gross"], row["replay_round_trips"] = gross, trips
            summary[leg.key] = row
        self._record("session_summary", now, day=self.state["day"],
                     full_session=bool(self.state.get("full_session")), orders=self.orders, legs=summary)
        self.state["summarised"] = True
        self._save()

    # ---- the poll --------------------------------------------------------
    def step(self) -> str:
        """Run one poll and return what happened: ``closed``, ``skipped`` or ``open``."""
        clock = self.provider.clock()
        now = clock.timestamp
        local = now.astimezone(NEW_YORK)
        day = local.date()
        if not clock.is_open:
            if self.state.get("day") and not self.state.get("summarised"):
                self._finish_day(now)
            return "closed"
        if self.state.get("day") != day.isoformat():
            self._start_day(day, now, clock.next_close)
        try:
            self._sync_orders(now, day)
            if self.state.get("full_session"):
                open_at = (datetime.combine(day, datetime.min.time(), NEW_YORK)
                           + timedelta(minutes=OPEN_MINUTE_OF_DAY))
                elapsed = (now - open_at).total_seconds()
                if elapsed >= 0:
                    if self._history_day != day:
                        self._load_history(day, open_at)
                    bars = self.provider.bars(self._symbols(), "1Min", start=open_at, end=now)
                    for leg in self.legs:
                        partial = Partial(_minute_rows(bars.get(leg.symbol, [])).get(day, []))
                        if leg.rule == NOISE:
                            self._noise_band(leg, now, day, elapsed, partial)
                            self._flatten_intraday(leg, now, day, elapsed, partial)
                        elif leg.rule == OPENING:
                            self._opening_range(leg, now, day, elapsed, partial)
                            self._flatten_intraday(leg, now, day, elapsed, partial)
                        elif leg.rule == REVERSION:
                            self._close_reversion(leg, now, day, elapsed, partial)
                        else:
                            raise ValueError(f"unknown rule {leg.rule!r}")
            # Orders are worked even on a session the rules skip, so a
            # position can always be reduced.
            for leg in self.legs:
                if self._trading(leg):
                    self._align(leg, now, day)
        finally:
            # Decisions already taken stay taken even if a later leg fails.
            self._save()
        return "open" if self.state.get("full_session") else "skipped"


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

_PROBLEM_EVENTS = frozenset({"order_failed", "order_dead", "order_timeout", "order_missing",
                             "order_gave_up", "order_lookup_failed", "position_mismatch", "entry_blocked",
                             "decision_skipped", "size_zero", "replay_unavailable"})


def read_journal(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        return []
    events = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def forward_report(events: Sequence[Mapping[str, Any]], *, cost_bps: float = 0.0) -> dict[str, Any]:
    """Forward statistics per leg from ``session_summary`` and ``round_trip`` events.

    Shadow fills are taken at the quote, so the spread is already paid;
    ``cost_bps`` adds fees and slippage on top. For the intraday legs the
    report also compares each day's shadow result with the bar replay.
    """
    charge = cost_bps / 1e4
    legs: dict[str, dict[str, list[Any]]] = {}
    for event in events:
        if event.get("kind") == "session_summary" and event.get("full_session"):
            day = date.fromisoformat(event["day"])
            for key, row in (event.get("legs") or {}).items():
                book = legs.setdefault(key, {"days": [], "shadow": [], "replay": [], "trips": []})
                if "shadow_gross" in row:
                    book["days"].append(day)
                    book["shadow"].append(row["shadow_gross"] - row["shadow_round_trips"] * charge)
                    book["trips"].append(row["shadow_round_trips"])
                    book["replay"].append(row.get("replay_gross"))
        elif event.get("kind") == "round_trip" and event.get("gross") is not None:
            book = legs.setdefault(event["leg"], {"days": [], "shadow": [], "replay": [], "trips": []})
            book.setdefault("round_trips", []).append(event["gross"] - charge)
        elif event.get("kind") == "fill" and event.get("slippage_bps") is not None:
            book = legs.setdefault(event["leg"], {"days": [], "shadow": [], "replay": [], "trips": []})
            book.setdefault("slippage", []).append(float(event["slippage_bps"]))
    out: dict[str, Any] = {"schema": SCHEMA, "cost_bps_added": cost_bps, "legs": {}}
    for key, book in sorted(legs.items()):
        row: dict[str, Any] = {"sessions": len(book["days"])}
        if book["shadow"]:
            row["net_bps_per_session"] = math.fsum(book["shadow"]) / len(book["shadow"]) * 1e4
            row["cumulative_pct"] = math.fsum(book["shadow"]) * 100
            row["round_trips"] = sum(book["trips"])
            row["t_newey_west"] = stats.newey_west_t(book["shadow"])
            pairs = [(s, r) for s, r in zip(book["shadow"], book["replay"]) if r is not None]
            if len(pairs) >= 3:
                gaps = [s - r for s, r in pairs]
                row["shadow_minus_replay_bps_per_session"] = math.fsum(gaps) / len(gaps) * 1e4
                row["shadow_replay_correlation"] = stats.correlation([s for s, _ in pairs],
                                                                     [r for _, r in pairs])
        trips = book.get("round_trips") or []
        if trips:
            row["closed_round_trips"] = len(trips)
            row["mean_bps_per_round_trip"] = math.fsum(trips) / len(trips) * 1e4
            row["cumulative_pct"] = math.fsum(trips) * 100
        slippage = book.get("slippage") or []
        if slippage:
            row["broker_fills"] = len(slippage)
            row["mean_fill_slippage_bps"] = math.fsum(slippage) / len(slippage)
            row["worst_fill_slippage_bps"] = max(slippage)
        out["legs"][key] = row
    problems = [event for event in events if event.get("kind") in _PROBLEM_EVENTS]
    out["problem_events"] = {kind: sum(1 for event in problems if event["kind"] == kind)
                             for kind in sorted({event["kind"] for event in problems})}
    return out


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def _raw(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception:  # noqa: BLE001 - an unavailable detail is reported as unknown, not as a pass
        return None


def preflight(provider: Any, legs: Sequence[Leg], *, orders: str, directory: str | Path,
              max_notional: float | None = None, allow_short: bool = True,
              min_observed: int = MIN_OBSERVED) -> dict[str, Any]:
    """Read-only checks that must all pass before orders are enabled.

    Nothing here sends, changes or cancels an order. Each check is
    ``{"name", "ok", "detail"}``; ``ok`` is ``False`` for anything that could
    not be proved.
    """
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    mode, paper = getattr(provider, "mode", None), getattr(provider, "paper", None)
    wanted = {ORDERS_PAPER: ("paper", True), ORDERS_LIVE: ("live", False)}.get(orders)
    check("endpoint_matches_order_mode", wanted is None or (mode, paper) == wanted,
          f"provider is {mode}, orders are {orders}")
    if orders == ORDERS_LIVE:
        check("live_notional_cap_set", bool(max_notional and max_notional > 0), max_notional)
    traded = [leg for leg in legs if leg.trade and orders != ORDERS_NONE]
    equity = 0.0
    try:
        account = provider.account()
        equity = float(account.equity)
        check("account_active", str(account.status).lower() == "active", account.status)
        check("account_has_equity", equity > 0, equity)
    except Exception as exc:  # noqa: BLE001
        check("account_readable", False, str(exc)[:200])
    session = getattr(provider, "session", None)
    raw_account = _raw(lambda: session.trading.get_account()) if session is not None else None
    if raw_account is not None:
        blocked = [name for name in ("trading_blocked", "account_blocked", "trade_suspended_by_user")
                   if getattr(raw_account, name, False)]
        check("account_not_blocked", not blocked, blocked or "no blocks reported")
    now = None
    is_open = False
    try:
        clock = provider.clock()
        now, is_open = clock.timestamp, bool(clock.is_open)
        check("clock_readable", True, {"is_open": is_open, "next_close": str(clock.next_close)})
    except Exception as exc:  # noqa: BLE001
        check("clock_readable", False, str(exc)[:200])
    state_path = Path(directory) / "state.json"
    own: dict[str, int] = {}
    if state_path.is_file():
        stored = json.loads(state_path.read_text(encoding="utf-8"))
        if stored.get("schema") == SCHEMA:
            own = {key: int(book.get("shares", 0)) for key, book in (stored.get("orders") or {}).items()}
            if orders != ORDERS_NONE:
                orphans = orphaned_shares(stored, legs)
                check("no_shares_in_untraded_legs", not orphans, orphans or "none")
    if traded:
        try:
            held = {str(position.symbol).upper():
                    int(Decimal(str(position.qty))) * (1 if position.side == "long" else -1)
                    for position in provider.positions()}
            for leg in traded:
                expected, broker = own.get(leg.key, 0), held.get(leg.symbol, 0)
                check(f"position_is_the_runners:{leg.symbol}", broker == expected,
                      f"broker holds {broker}, the runner's record is {expected}")
        except Exception as exc:  # noqa: BLE001
            check("positions_readable", False, str(exc)[:200])
        try:
            symbols = {leg.symbol for leg in traded}
            resting = [order.symbol for order in provider.orders(status="open") if order.symbol in symbols]
            check("no_open_orders_in_traded_symbols", not resting, sorted(set(resting)) or "none")
        except Exception as exc:  # noqa: BLE001
            check("open_orders_readable", False, str(exc)[:200])
    if now is not None:
        symbols = sorted({leg.symbol for leg in legs})
        try:
            bars = provider.bars(symbols, "1Min", start=now - timedelta(days=HISTORY_CALENDAR_DAYS), end=now)
        except Exception as exc:  # noqa: BLE001
            bars = {}
            check("bars_readable", False, str(exc)[:200])
        today = now.astimezone(NEW_YORK).date()
        for leg in legs:
            rows = _minute_rows(bars.get(leg.symbol, []))
            sessions = [build_session(day, day_rows, min_observed=min_observed)
                        for day, day_rows in sorted(rows.items()) if day < today]
            sessions = [session for session in sessions if session is not None]
            needed = leg.noise_rules().lookback_sessions if leg.rule == NOISE else 1
            check(f"history:{leg.key}", len(sessions) >= needed,
                  f"{len(sessions)} full sessions, {needed} needed")
            if leg not in traded or not sessions:
                continue
            price = sessions[-1].close[-1]
            notional = equity * leg.weight
            if max_notional is not None:
                notional = min(notional, max_notional)
            shares = int(notional / price) if price > 0 else 0
            check(f"size:{leg.symbol}", shares >= 1,
                  f"{shares} shares at about {price:.2f} ({notional:.0f} of notional)")
            if is_open:
                latest = max((bar.timestamp for bar in bars.get(leg.symbol, [])), default=None)
                age = None if latest is None else (now - latest).total_seconds()
                check(f"bars_are_current:{leg.symbol}", age is not None and age < 240,
                      "no bars" if age is None else f"latest bar started {age:.0f}s ago")
    for leg in traded:
        if leg.rule != NOISE or not allow_short or "short" not in leg.noise_rules().sides:
            continue
        asset = _raw(lambda symbol=leg.symbol: session.trading.get_asset(symbol)) if session is not None else None
        shortable = bool(getattr(asset, "shortable", False)) and bool(getattr(asset, "easy_to_borrow", False))
        enabled = bool(getattr(raw_account, "shorting_enabled", False))
        check(f"short_selling_available:{leg.symbol}", shortable and enabled,
              {"asset_shortable_and_easy_to_borrow": shortable, "account_shorting_enabled": enabled})
    if (Path(directory) / HALT_FILE).exists():
        check("no_halt_file", False, "remove it with the resume command")
    return {"schema": SCHEMA, "orders": orders, "ok": all(item["ok"] for item in checks), "checks": checks}


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _load_environment(env_file: str | None, root: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    chosen = env_file or os.getenv("ALPACA_AGENT_SECRETS_FILE") or os.getenv("ALPACA_AGENT_SECRET_FILE")
    if chosen:
        load_dotenv(chosen, override=False)
    elif (root / ".env").is_file():
        load_dotenv(root / ".env", override=False)


def build_provider(mode: str, feed: str) -> Any:
    """A provider on the endpoint ``mode`` names; the environment guards for live still apply."""
    from agent.alpaca_provider import AlpacaProvider
    return AlpacaProvider({"mode": mode, "research": {"enabled": False},
                           "broker": {"paper": mode == "paper", "allow_live": mode == "live",
                                      "data_feed": feed}})


def run_loop(runner: ShadowRunner, *, interval: float, once: bool = False,
             sleep: Callable[[float], None] = time_module.sleep) -> int:
    try:
        while True:
            try:
                status = runner.step()
            except Exception as exc:  # noqa: BLE001 - a transient data error must not end the session
                detail = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]
                print(f"step failed: {detail}", file=sys.stderr, flush=True)
                status = "error"
            if once:
                print(status)
                return 0 if status != "error" else 1
            if status in ("open", "skipped"):
                sleep(interval)
            else:
                sleep(max(interval, 30.0 if status == "error" else 60.0))
    except KeyboardInterrupt:           # Ctrl-C, or a stop signal: state is saved after every step
        print("stopped", file=sys.stderr, flush=True)
        return 0


def restrict_trading(legs: Sequence[Leg], only: str) -> tuple[Leg, ...]:
    """Keep orders for the named rules or ``rule:SYMBOL`` legs; record the rest."""
    names = {item.strip() for item in only.split(",") if item.strip()}
    if not names:
        return tuple(legs)
    known = {leg.rule for leg in legs} | {leg.key for leg in legs}
    if names - known:
        raise ValueError(f"--trade-only names nothing in the book: {sorted(names - known)}")
    return tuple(leg if (leg.rule in names or leg.key in names) else replace(leg, trade=False) for leg in legs)


def order_mode(args: argparse.Namespace) -> str:
    """The order mode the flags ask for; raises ``ValueError`` when they are inconsistent."""
    if args.paper_orders and args.live_orders:
        raise ValueError("choose --paper-orders or --live-orders, not both")
    if args.paper_orders:
        if args.mode != "paper":
            raise ValueError("--paper-orders needs --mode paper")
        return ORDERS_PAPER
    if args.live_orders:
        if args.mode != "live":
            raise ValueError("--live-orders needs --mode live")
        if args.confirm != "LIVE":
            raise ValueError("--live-orders needs --confirm LIVE")
        if not (args.max_notional and args.max_notional > 0):
            raise ValueError("--live-orders needs --max-notional, the most dollars one leg may hold")
        return ORDERS_LIVE
    return ORDERS_NONE


def _account_arguments(parser: argparse.ArgumentParser, root: Path) -> None:
    parser.add_argument("--mode", choices=("paper", "live"), default="paper",
                        help="which Alpaca endpoint the keys belong to")
    parser.add_argument("--feed", default=os.getenv("ALPACA_DATA_FEED", "iex"), choices=("iex", "sip"))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--dir", default=str(root / "runtime" / "session_edges"))
    parser.add_argument("--book", default=None, help="edge_book.json from discover.py; default legs otherwise")
    parser.add_argument("--trade-only", default="",
                        help="comma-separated rules or rule:SYMBOL keys that may send orders; "
                             "every other leg is recorded only")
    parser.add_argument("--paper-orders", action="store_true", help="send market orders to the paper account")
    parser.add_argument("--live-orders", action="store_true", help="send market orders to the live account")
    parser.add_argument("--confirm", default="", help="must be LIVE with --live-orders")
    parser.add_argument("--max-notional", type=float, default=None,
                        help="most dollars one leg may hold; required for live orders")
    parser.add_argument("--short", choices=("auto", "yes", "no"), default="auto",
                        help="short selling for the momentum leg; auto is yes on paper and no on live")
    parser.add_argument("--daily-loss-limit", type=float, default=DAILY_LOSS_LIMIT,
                        help="fall in equity today, as a fraction, after which nothing new is opened")


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="poll the session; record decisions; send orders if asked")
    _account_arguments(run, root)
    run.add_argument("--interval", type=float, default=10.0, help="seconds between polls while open")
    run.add_argument("--once", action="store_true", help="run a single poll and exit")
    check = sub.add_parser("preflight", help="read-only go / no-go checks for the chosen order mode")
    _account_arguments(check, root)
    for name, text in (("halt", "forbid new positions until resume (creates the HALT file)"),
                       ("resume", "allow new positions again (removes the HALT file)"),
                       ("flatten", "ask the running runner to close every position it holds, then halt")):
        control = sub.add_parser(name, help=text)
        control.add_argument("--dir", default=str(root / "runtime" / "session_edges"))
    report = sub.add_parser("report", help="forward statistics from the journal")
    report.add_argument("--dir", default=str(root / "runtime" / "session_edges"))
    report.add_argument("--cost-bps", type=float, default=0.0)
    args = parser.parse_args(argv)
    directory = Path(args.dir)
    if args.command == "report":
        print(json.dumps(forward_report(read_journal(directory / "journal.jsonl"), cost_bps=args.cost_bps),
                         indent=1, sort_keys=True))
        return 0
    if args.command in ("halt", "resume", "flatten"):
        directory.mkdir(parents=True, exist_ok=True)
        if args.command == "resume":
            for name in (HALT_FILE, FLATTEN_FILE):
                (directory / name).unlink(missing_ok=True)
            print("new positions are allowed again")
        else:
            name = HALT_FILE if args.command == "halt" else FLATTEN_FILE
            (directory / name).write_text(f"requested {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
            print("no new positions will be opened" if args.command == "halt" else
                  "the running runner will close its positions at its next poll while the market is open, "
                  "then halt; if no runner is running, start one with the same order flags")
        return 0
    try:
        orders = order_mode(args)
        legs = restrict_trading(load_book(args.book) if args.book else DEFAULT_LEGS, args.trade_only)
    except (OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    allow_short = args.short == "yes" or (args.short == "auto" and orders != ORDERS_LIVE)
    _load_environment(args.env_file, root)
    from agent import state
    try:
        provider = build_provider(args.mode, args.feed)
    except Exception as exc:  # noqa: BLE001 - the provider's own guards explain themselves
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.command == "preflight":
        result = preflight(provider, legs, orders=orders, directory=directory,
                           max_notional=args.max_notional, allow_short=allow_short)
        print(json.dumps(result, indent=1, sort_keys=True, default=str))
        return 0 if result["ok"] else 1
    lock = None
    try:
        if orders != ORDERS_NONE:
            state.configure_runtime(args.mode)
            lock = state.acquire_run_lock()
            if lock is None:
                print("refused: the runtime lock is held; stop the trader first, or run without orders",
                      file=sys.stderr)
                return 2
            result = preflight(provider, legs, orders=orders, directory=directory,
                               max_notional=args.max_notional, allow_short=allow_short)
            if not result["ok"]:
                failed = [item for item in result["checks"] if not item["ok"]]
                print("refused: preflight failed\n" + json.dumps(failed, indent=1, default=str), file=sys.stderr)
                return 2
        runner = ShadowRunner(provider, directory=directory, legs=legs, orders=orders,
                              max_notional=args.max_notional, allow_short=allow_short,
                              daily_loss_limit=args.daily_loss_limit)
        if threading.current_thread() is threading.main_thread():
            # ``docker stop`` sends SIGTERM: end the loop the way Ctrl-C does, releasing the lock.
            signal.signal(signal.SIGTERM, signal.default_int_handler)
        return run_loop(runner, interval=max(2.0, args.interval), once=args.once)
    except (RuntimeError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            state.release_run_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
