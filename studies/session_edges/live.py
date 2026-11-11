"""Forward shadow, and optional paper orders, for the registered session rules.

Usage, from the repository root::

    python -m studies.session_edges.live run                 # shadow only: no orders are ever sent
    python -m studies.session_edges.live run --paper-orders  # also trade the paper account
    python -m studies.session_edges.live report              # forward statistics so far

The runner polls the broker clock. At each registered decision time it reads
completed one-minute bars, calls the same decision function the study
simulator calls, and records what it would have paid at the live quote (the
ask to buy, the bid to sell). After the close it replays the day's bars
through the study simulator and stores both numbers, so implementation drift
and real spread costs are measured every session.

Safety: paper orders need ``--paper-orders``, a configuration pinned to paper
mode, a provider on the paper endpoint, and the runtime lock the trader also
takes, so the two can never share an account at the same time. Each traded
leg uses its own symbol. The runner never sends an order in live mode.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import time as time_module
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from studies.liquid_reversion.bars import NEW_YORK
from studies.session_edges import stats
from studies.session_edges.data import (MAX_SESSION_GAP_DAYS, MIN_OBSERVED, MINUTES,
                                        OPEN_MINUTE_OF_DAY, MinuteRow, Session, build_session)
from studies.session_edges.strategies import (CLOSE_REVERSION, ENTER, EXIT, FLAT, LONG, NOISE_BAND,
                                              OPENING_RANGE, SHORT, Fill, close_reversion_action,
                                              internal_bar_strength, noise_band_bounds, noise_band_sigma,
                                              noise_band_target, simulate_noise_band,
                                              simulate_opening_range)

SCHEMA = "session-edges-shadow.v1"
NOISE, REVERSION, OPENING = "noise_band_momentum", "close_weakness_reversion", "opening_range_breakout"
FLAT_MINUTE = 388                # 15:58 New York: intraday legs leave with a market order
GRACE_SECONDS = 4.0              # wait this long after a boundary for the completed bar
LATE_BAR_SECONDS = 45.0          # then accept the last traded price for a quiet minute
MAX_DELAY_SECONDS = 240.0        # a decision this late is skipped, never acted on stale
QUOTE_MAX_AGE_SECONDS = 30.0
HISTORY_CALENDAR_DAYS = 35
DAILY_LOSS_LIMIT = 0.02          # paper orders: no new entries after this fall in equity today
MAX_FLATTEN_ATTEMPTS = 5         # end-of-day closing orders for shares an earlier exit left behind
ORDER_PREFIX = "sedge"


@dataclass(frozen=True, slots=True)
class Leg:
    """One rule on one symbol. ``weight`` is the share of equity a paper order uses."""

    rule: str
    symbol: str
    weight: float = 0.0
    trade: bool = False

    @property
    def key(self) -> str:
        return f"{self.rule}:{self.symbol}"


# The paper candidates of the study, plus the shadow-only tier. The QQQ
# reversion leg stays shadow-only by default because the QQQ momentum leg
# already trades that symbol and one traded leg owns one symbol.
DEFAULT_LEGS = (
    Leg(NOISE, "QQQ", 0.50, True),
    Leg(REVERSION, "SPY", 0.25, True),
    Leg(REVERSION, "QQQ"),
    Leg(OPENING, "QQQ"),
)


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


class ShadowRunner:
    """One poll at a time; all state lives in ``directory`` so a restart resumes the session."""

    def __init__(self, provider: Any, *, directory: str | Path, legs: Sequence[Leg] = DEFAULT_LEGS,
                 paper_orders: bool = False, min_observed: int = MIN_OBSERVED) -> None:
        self.provider = provider
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.legs = tuple(legs)
        self.paper_orders = bool(paper_orders)
        self.min_observed = min_observed
        if len({leg.key for leg in self.legs}) != len(self.legs):
            raise ValueError("duplicate leg")
        traded = [leg.symbol for leg in self.legs if leg.trade]
        if len(set(traded)) != len(traded):
            raise ValueError("each traded leg needs its own symbol")
        if self.paper_orders:
            if getattr(provider, "mode", None) != "paper" or getattr(provider, "paper", None) is not True:
                raise RuntimeError("paper orders need a provider on the paper endpoint")
            if math.fsum(leg.weight for leg in self.legs if leg.trade) > 1.0 + 1e-9:
                raise ValueError("traded leg weights exceed the account equity")
        self.state_path = self.directory / "state.json"
        self.journal_path = self.directory / "journal.jsonl"
        self.state = self._load_state()
        self.history: dict[str, list[Session]] = {}
        self._history_day: date | None = None

    # ---- persistence -----------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("schema") == SCHEMA:
                return state
        return {"schema": SCHEMA, "day": None, "legs": {}, "carry": {}}

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
        try:
            rows = self.provider.quotes([symbol], start=now - timedelta(seconds=QUOTE_MAX_AGE_SECONDS),
                                        end=now).get(symbol, [])
        except Exception:  # noqa: BLE001 - a missing quote must not stop the session
            rows = []
        for quote in reversed(rows):
            price = quote.ask if side == LONG else quote.bid
            if price is not None and float(price) > 0:
                return float(price), "quote"
        return fallback, "bar_fallback"

    # ---- paper orders ----------------------------------------------------
    def _submit(self, leg: Leg, now: datetime, day: date, tag: str, delta_shares: int) -> str | None:
        if not (self.paper_orders and leg.trade) or delta_shares == 0:
            return None
        from agent.alpaca_domain import OrderRequest
        client_id = f"{ORDER_PREFIX}-{leg.rule[:5]}-{leg.symbol}-{day:%Y%m%d}-{tag}"
        request = OrderRequest(symbol=leg.symbol, qty=Decimal(abs(delta_shares)),
                               side="buy" if delta_shares > 0 else "sell", type="market",
                               time_in_force="day", client_order_id=client_id)
        try:
            self.provider.submit_order(request)
        except Exception as exc:  # noqa: BLE001 - record and keep the shadow running
            self._record("order_failed", now, leg=leg.key, client_order_id=client_id,
                         shares=delta_shares, error=str(exc)[:200])
            return None
        self._record("order_submitted", now, leg=leg.key, client_order_id=client_id, shares=delta_shares)
        return client_id

    def _shares(self, leg: Leg, price: float) -> int:
        if not (self.paper_orders and leg.trade and leg.weight > 0 and price > 0):
            return 0
        equity = float(self.provider.account().equity)
        return max(0, int(equity * leg.weight / price))

    def _entries_halted(self, now: datetime) -> bool:
        if not self.paper_orders:
            return False
        day_state = self.state
        if day_state.get("entries_halted"):
            return True
        equity = float(self.provider.account().equity)
        start = day_state.setdefault("equity_at_open", equity)
        if start > 0 and (start - equity) / start >= DAILY_LOSS_LIMIT:
            day_state["entries_halted"] = True
            self._record("entries_halted", now, equity=equity, equity_at_open=start)
            return True
        return False

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
                                           "done": [], "fills": [], "shares": 0}
        self._record("session_start", now, day=day.isoformat(), full_session=bool(full),
                     legs=[leg.key for leg in self.legs], paper_orders=self.paper_orders)
        self._save()

    def _fill(self, leg: Leg, now: datetime, day: date, minute: int, target: int, reference: float,
              reason: str) -> None:
        """Move a leg to ``target`` at the live quote and record the round trip it closes."""
        book = self.state["legs"][leg.key]
        position = int(book["position"])
        if target == position:
            return
        side = LONG if target > position else SHORT
        price, source = self._quote(leg.symbol, now, side, reference)
        if position != FLAT:
            book["fills"].append([position, book["entry_minute"], book["entry"], minute, price])
        wanted = 0
        if target != FLAT and not self._entries_halted(now):
            wanted = target * self._shares(leg, price)
        held = int(book.get("shares", 0))
        if held and wanted and (held > 0) != (wanted > 0):
            # A broker position cannot cross zero in one order: flatten now;
            # the new side is opened on the next poll (see _finish_flip).
            self._submit(leg, now, day, f"{minute}x", -held)
            book["shares"] = 0
            book["flip_pending"] = True
            self._record("flip_deferred", now, leg=leg.key, minute=minute)
        elif wanted != held:
            if self._submit(leg, now, day, str(minute), wanted - held) is not None:
                book["shares"] = wanted
        book.update({"position": target, "entry": price if target != FLAT else None,
                     "entry_minute": minute if target != FLAT else None})
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

    def _finish_flip(self, leg: Leg, now: datetime, day: date, partial: Partial) -> None:
        """Open the new side of a deferred flip on the first poll after the flattening order."""
        book = self.state["legs"][leg.key]
        if not book.pop("flip_pending", False):
            return
        position = int(book["position"])
        row = partial.rows.get(partial.latest)
        if position == FLAT or row is None or self._entries_halted(now):
            return
        wanted = position * self._shares(leg, row[4])
        if wanted and self._submit(leg, now, day, f"{book['entry_minute']}r", wanted) is not None:
            book["shares"] = wanted

    def _noise_band(self, leg: Leg, now: datetime, day: date, elapsed: float, partial: Partial) -> None:
        book = self.state["legs"][leg.key]
        history = self.history.get(leg.symbol, [])
        self._finish_flip(leg, now, day, partial)
        for minute in NOISE_BAND.decision_minutes():
            if minute in book["done"] or minute >= FLAT_MINUTE:
                continue
            ready = self._ready(minute, elapsed, partial.latest)
            if ready == "wait":
                break
            book["done"].append(minute)
            session = partial.session(day, minute)
            sigma = noise_band_sigma(history, minute, NOISE_BAND.lookback_sessions)
            if ready == "missed" or session is None or sigma is None:
                self._record("decision_skipped", now, leg=leg.key, minute=minute,
                             reason="late" if ready == "missed" else "history_or_bars_unavailable")
                continue
            prior = (history[-1].close[-1]
                     if history and (day - history[-1].day).days <= MAX_SESSION_GAP_DAYS else None)
            upper, lower = noise_band_bounds(session.open[0], prior, sigma, NOISE_BAND.band_multiple)
            price = session.close[minute - 1]
            target = noise_band_target(int(book["position"]), price, upper, lower, NOISE_BAND.sides)
            self._fill(leg, now, day, minute, target, price,
                       f"price {price:.4f} band [{lower:.4f}, {upper:.4f}]")

    def _flatten_intraday(self, leg: Leg, now: datetime, day: date, elapsed: float, partial: Partial) -> None:
        book = self.state["legs"][leg.key]
        if elapsed < FLAT_MINUTE * 60.0:
            return
        if FLAT_MINUTE not in book["done"]:
            book["done"].append(FLAT_MINUTE)
            session = partial.session(day, FLAT_MINUTE)
            if int(book["position"]) != FLAT and session is not None:
                self._fill(leg, now, day, FLAT_MINUTE, FLAT, session.close[FLAT_MINUTE - 1], "end of day")
        # Whatever the shadow book says, an intraday leg must not carry shares
        # past the close: an earlier exit order may have failed. Retry a few
        # times, each under its own client order id.
        held = int(book.get("shares", 0))
        attempts = int(book.get("flatten_attempts", 0))
        if held and attempts < MAX_FLATTEN_ATTEMPTS:
            book["flatten_attempts"] = attempts + 1
            if self._submit(leg, now, day, f"eod{attempts}", -held) is not None:
                book["shares"] = 0

    def _opening_range(self, leg: Leg, now: datetime, day: date, elapsed: float, partial: Partial) -> None:
        """Shadow-only: enter after the first candle, then track the stop on completed bars."""
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
        minute = CLOSE_REVERSION.decision_minute
        if minute in book["done"]:
            return
        ready = self._ready(minute, elapsed, partial.latest)
        if ready == "wait":
            return
        book["done"].append(minute)
        carry = self.state["carry"].setdefault(leg.key, {"in_position": False, "held": 0, "entry": None,
                                                         "entry_day": None, "shares": 0})
        session = partial.session(day, minute)
        if ready == "missed" or session is None:
            self._record("decision_skipped", now, leg=leg.key, minute=minute, reason="late_or_no_bars")
            return
        price = session.close[minute - 1]
        strength = internal_bar_strength(max(session.high[:minute]), min(session.low[:minute]), price)
        held = int(carry["held"]) + 1 if carry["in_position"] else 0
        action = close_reversion_action(bool(carry["in_position"]), held, strength)
        if carry["in_position"]:
            carry["held"] = held
        if action == ENTER:
            fill, source = self._quote(leg.symbol, now, LONG, price)
            shares = 0 if self._entries_halted(now) else self._shares(leg, fill)
            if shares and self._submit(leg, now, day, "in", shares) is None:
                shares = 0
            carry.update({"in_position": True, "held": 0, "entry": fill, "entry_day": day.isoformat(),
                          "shares": shares})
        elif action == EXIT:
            fill, source = self._quote(leg.symbol, now, SHORT, price)
            shares = int(carry.get("shares", 0))
            if shares:
                self._submit(leg, now, day, "out", -shares)
            self._record("round_trip", now, leg=leg.key, entry_day=carry["entry_day"], entry=carry["entry"],
                         exit=fill, gross=fill / carry["entry"] - 1.0 if carry["entry"] else None,
                         sessions_held=held)
            carry.update({"in_position": False, "held": 0, "entry": None, "entry_day": None, "shares": 0})
        else:
            fill, source = price, "none"
        self._record("decision", now, leg=leg.key, minute=minute, action=action, strength=strength,
                     price=fill, price_source=source, reference=price, sessions_held=held)

    # ---- end of day ------------------------------------------------------
    def _replay(self, leg: Leg, today: Session) -> tuple[float | None, int | None]:
        """What the study simulator makes of today's bars, for the fidelity comparison."""
        history = [session for session in self.history.get(leg.symbol, []) if session.day < today.day]
        if leg.rule == NOISE:
            if len(history) < NOISE_BAND.lookback_sessions:
                return None, None
            result = simulate_noise_band([*history, today], replace(NOISE_BAND, flat_minute=FLAT_MINUTE))[-1]
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
            row: dict[str, Any] = {"rule": leg.rule, "symbol": leg.symbol, "traded": leg.trade}
            if leg.rule == REVERSION:
                carry = self.state["carry"].get(leg.key, {})
                row["in_position"] = bool(carry.get("in_position"))
            else:
                fills = [Fill(int(f[0]), int(f[1]), float(f[2]), int(f[3]), float(f[4]))
                         for f in book.get("fills", [])]
                row["shadow_gross"] = math.fsum(fill.gross for fill in fills)
                row["shadow_round_trips"] = len(fills)
            today = todays.get(leg.symbol)
            if today is not None and self.state.get("full_session"):
                gross, trips = self._replay(leg, today)
                if gross is not None:
                    row["replay_gross"], row["replay_round_trips"] = gross, trips
            summary[leg.key] = row
        self._record("session_summary", now, day=self.state["day"],
                     full_session=bool(self.state.get("full_session")), legs=summary)
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
        if not self.state.get("full_session"):
            return "skipped"
        open_at = datetime.combine(day, datetime.min.time(), NEW_YORK) + timedelta(minutes=OPEN_MINUTE_OF_DAY)
        elapsed = (now - open_at).total_seconds()
        if elapsed < 0:
            return "open"
        if self._history_day != day:
            self._load_history(day, open_at)
        bars = self.provider.bars(self._symbols(), "1Min", start=open_at, end=now)
        try:
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
        finally:
            # Decisions already taken stay taken even if a later leg fails.
            self._save()
        return "open"


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

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
        out["legs"][key] = row
    return out


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


def run_loop(runner: ShadowRunner, *, interval: float, once: bool = False,
             sleep: Callable[[float], None] = time_module.sleep) -> int:
    while True:
        try:
            status = runner.step()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001 - a transient data error must not end the session
            detail = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]
            print(f"step failed: {detail}", file=sys.stderr, flush=True)
            status = "error"
        if once:
            print(status)
            return 0 if status != "error" else 1
        sleep(interval if status == "open" else max(interval, 60.0))


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="poll the session and record shadow decisions")
    run.add_argument("--config", default=str(root / "config.yaml"))
    run.add_argument("--env-file", default=None)
    run.add_argument("--dir", default=str(root / "runtime" / "session_edges"))
    run.add_argument("--interval", type=float, default=10.0, help="seconds between polls while open")
    run.add_argument("--paper-orders", action="store_true",
                     help="also send market orders to the paper account for the traded legs")
    run.add_argument("--once", action="store_true", help="run a single poll and exit")
    report = sub.add_parser("report", help="forward statistics from the journal")
    report.add_argument("--dir", default=str(root / "runtime" / "session_edges"))
    report.add_argument("--cost-bps", type=float, default=0.0)
    args = parser.parse_args(argv)
    if args.command == "report":
        print(json.dumps(forward_report(read_journal(Path(args.dir) / "journal.jsonl"),
                                        cost_bps=args.cost_bps), indent=1, sort_keys=True))
        return 0
    _load_environment(args.env_file, root)
    from agent import state
    from agent.alpaca_provider import AlpacaProvider
    from agent.config import ConfigError, load_config
    try:
        config = load_config(args.config)
    except (OSError, ConfigError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    broker = config.get("broker") if isinstance(config.get("broker"), dict) else {}
    paper_pinned = (config.get("mode", "paper") == "paper" and broker.get("paper") is True
                    and broker.get("allow_live", False) is False)
    if args.paper_orders and not paper_pinned:
        print("refused: --paper-orders needs a configuration pinned to paper mode", file=sys.stderr)
        return 2
    lock = None
    try:
        if args.paper_orders:
            state.configure_runtime("paper")
            lock = state.acquire_run_lock()
            if lock is None:
                print("refused: the paper runtime lock is held; stop the trader first, "
                      "or run without --paper-orders", file=sys.stderr)
                return 2
        runner = ShadowRunner(AlpacaProvider(config), directory=args.dir, paper_orders=args.paper_orders)
        return run_loop(runner, interval=max(2.0, args.interval), once=args.once)
    finally:
        if lock is not None:
            state.release_run_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
