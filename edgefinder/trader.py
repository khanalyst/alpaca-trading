"""Trade the frozen strategy spec at the opening and closing auctions.

Parity holds by construction: the trader calls the same
:func:`edgefinder.catalogue.decide` the research used, on the same kind of
panel, with one extra row for today whose prices are unknown.  The causality
tests guarantee that row cannot influence today's decision.

Two phases per trading day:

* ``open``  between 08:00 and 09:27 New York time: market-on-open orders
  (``opg``; Alpaca rejects them from 09:28) to reach the session weights;
* ``close`` between 25 and 12 minutes before the close: market-on-close
  orders (``cls``; rejected from 15:50, earlier on half days) to reach the
  overnight weights.

``catchup`` repairs a missed open with ordinary market orders while the
market is open.  Every check fails closed:

* paper unless ``--live``, and live also needs a ``confirmed`` spec and
  ``ALPACA_LIVE_ENABLE=true``;
* the account must be dedicated: a position in a fund the strategy does not
  trade stops it (the intraday runtime force-flats at 15:50 and its watchdog
  flattens anything it does not know, so the two must never share an
  account);
* the data must end at the previous session, the look-back must be full,
  and a ``HALT`` file or a drawdown beyond the limit stops all orders;
* client order ids are deterministic per strategy, session, phase, fund and
  side, so a retry can never double an order.

Every decision, order and error is appended to a JSONL journal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from edgefinder import catalogue, data
from edgefinder.broker import AlpacaBroker, Broker, BrokerError
from edgefinder.catalogue import Candidate
from edgefinder.data import NY, Panel
from edgefinder.report import STRATEGY_SCHEMA

CLIENT_PREFIX = "efx"
OPEN_WINDOW = (dt.time(8, 0), dt.time(9, 27))
CLOSE_LEAD_MINUTES = 25
CLOSE_CUTOFF_MINUTES = 12
CATCHUP_AFTER_OPEN_MINUTES = 5
DEFAULT_SPEC = Path("edgefinder/results/strategy.json")
DEFAULT_STATE = Path("runtime/edgefinder")
TIF = {"open": "opg", "close": "cls", "catchup": "day", "flatten": "day"}
# The paper trial's job is the execution check (the return itself needs about
# four years of nights to test).  Shortfall is measured per auction leg against
# the printed open or close the research used; a round trip has two legs.
# On a paper account the fills are simulated, so the verdict there tests the
# trader's mechanics and the simulator, not the auction; see status().
TRIAL_MIN_LEGS = 120                  # about 60 round trips, three months
TRIAL_PASS_ROUND_TRIP_BPS = 1.0       # mean shortfall per round trip
TRIAL_STOP_ROUND_TRIP_BPS = 2.0       # beyond this the print-location share is gone


class TraderError(RuntimeError):
    """A check failed; nothing was submitted."""


@dataclass
class Config:
    spec_path: Path = DEFAULT_SPEC
    state_dir: Path = DEFAULT_STATE
    capital_fraction: float = 0.95
    max_drawdown_halt: float = 0.20
    max_order_fraction: float = 1.05       # of capital, per order
    allow_foreign_positions: bool = False
    data_provider: str = "yahoo"
    cache_dir: Path = data.DEFAULT_CACHE

    @property
    def journal(self) -> Path:
        return self.state_dir / "journal.jsonl"

    @property
    def halt_file(self) -> Path:
        return self.state_dir / "HALT"


@dataclass
class Session:
    date: dt.date
    open: dt.datetime
    close: dt.datetime


@dataclass
class Order:
    symbol: str
    side: str
    qty: int
    time_in_force: str
    client_order_id: str
    current_qty: float
    target_qty: int
    price: float

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass
class Plan:
    phase: str
    session: str
    candidate: str
    weights: dict[str, float]
    equity: float
    capital: float
    current: dict[str, float]
    targets: dict[str, int]
    orders: list[Order] = field(default_factory=list)


# --------------------------------------------------------------------------
# spec, journal, calendar


def load_spec(path: Path) -> dict[str, Any]:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if spec.get("schema") != STRATEGY_SCHEMA:
        raise TraderError(f"unknown spec schema {spec.get('schema')!r}")
    if not spec.get("candidate"):
        raise TraderError(f"the spec has no candidate: {spec.get('reason', 'no edge')}")
    family = spec["candidate"]["family"]
    if family not in catalogue.FAMILIES or catalogue.FAMILIES[family].benchmark_only:
        raise TraderError(f"family {family!r} is not executable")
    return spec


def candidate_of(spec: Mapping[str, Any]) -> Candidate:
    return Candidate.from_json(spec["candidate"])


def journal(cfg: Config, event: str, **fields: Any) -> dict[str, Any]:
    record = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "event": event, **fields}
    cfg.journal.parent.mkdir(parents=True, exist_ok=True)
    with cfg.journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
    return record


def read_journal(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.journal.exists():
        return []
    out = []
    for line in cfg.journal.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def phase_done(cfg: Config, session: dt.date, phase: str) -> bool:
    return any(r.get("event") == "phase_complete" and r.get("session") == session.isoformat()
               and r.get("phase") == phase for r in read_journal(cfg))


def sessions(broker: Broker, around: dt.date, *, back: int = 14, ahead: int = 75) -> list[Session]:
    rows = broker.calendar(around - dt.timedelta(days=back), around + dt.timedelta(days=ahead))
    out = []
    for row in rows:
        day = dt.date.fromisoformat(row["date"])
        o = dt.datetime.combine(day, dt.time.fromisoformat(row["open"]), NY)
        c = dt.datetime.combine(day, dt.time.fromisoformat(row["close"]), NY)
        out.append(Session(day, o, c))
    return sorted(out, key=lambda s: s.date)


def window_open(phase: str, session: Session, now: dt.datetime) -> bool:
    local = now.astimezone(NY)
    if local.date() != session.date:
        return False
    if phase == "open":
        return OPEN_WINDOW[0] <= local.time() < OPEN_WINDOW[1]
    if phase == "close":
        return (session.close - dt.timedelta(minutes=CLOSE_LEAD_MINUTES) <= local
                < session.close - dt.timedelta(minutes=CLOSE_CUTOFF_MINUTES))
    if phase in ("catchup", "flatten"):
        return (session.open + dt.timedelta(minutes=CATCHUP_AFTER_OPEN_MINUTES) <= local
                < session.close - dt.timedelta(minutes=CLOSE_LEAD_MINUTES))
    raise TraderError(f"unknown phase {phase}")


def client_order_id(candidate: Candidate, session: dt.date, phase: str, symbol: str,
                    side: str, leg: str = "") -> str:
    """At most 48 characters, stable across retries of the same intent."""
    tag = hashlib.sha256(candidate.id.encode()).hexdigest()[:8]
    return f"{CLIENT_PREFIX}-{tag}-{session:%Y%m%d}-{phase[:2]}-{symbol}-{side[0]}{leg}"


# --------------------------------------------------------------------------
# decisions


def extend_with_today(panel: Panel, today: dt.date, future: Sequence[dt.date]) -> Panel:
    """Append a row for ``today`` with unknown prices; carry the known calendar."""
    if today <= panel.dates[-1]:
        raise TraderError(f"today {today} is not after the last bar {panel.dates[-1]}")
    s = len(panel.symbols)
    nan_row = np.full((1, s), np.nan)
    ext = Panel(panel.dates + [today], list(panel.symbols),
                np.vstack([panel.open, nan_row]), np.vstack([panel.high, nan_row]),
                np.vstack([panel.low, nan_row]), np.vstack([panel.close, nan_row]),
                np.vstack([panel.volume, nan_row]), np.vstack([panel.dividend, np.zeros((1, s))]),
                np.append(panel.rf_rate, panel.rf_rate[-1]), dict(panel.sources),
                [d for d in future if d > today])
    return ext


def todays_weights(panel: Panel, candidate: Candidate, today: dt.date,
                   future: Sequence[dt.date]) -> tuple[dict[str, float], dict[str, float], bool]:
    """Session weights (set at today's open) and overnight weights (set at today's close)."""
    ext = extend_with_today(panel, today, future)
    open_w, close_w, ready = catalogue.decide(ext, candidate)
    syms = candidate.symbols
    return ({s: float(open_w[-1, i]) for i, s in enumerate(syms)},
            {s: float(close_w[-1, i]) for i, s in enumerate(syms)}, bool(ready[-1]))


def load_live_panel(candidate: Candidate, cfg: Config) -> Panel:
    symbols = list(dict.fromkeys(list(candidate.symbols) + [data.CALENDAR_SYMBOL]))
    raw = data.fetch_daily(symbols, cfg.cache_dir, refresh=True, provider=cfg.data_provider)
    return data.build_panel(raw, list(candidate.symbols))


def build_plan(*, phase: str, session: dt.date, candidate: Candidate, weights: Mapping[str, float],
               equity: float, current: Mapping[str, float], prices: Mapping[str, float],
               cfg: Config) -> Plan:
    capital = equity * cfg.capital_fraction
    targets: dict[str, int] = {}
    orders: list[Order] = []
    for symbol in candidate.symbols:
        w = float(weights.get(symbol, 0.0))
        price = prices.get(symbol)
        if w != 0 and (price is None or not math.isfinite(price) or price <= 0):
            raise TraderError(f"no usable price for {symbol}")
        target = int(math.trunc(w * capital / price)) if w else 0
        targets[symbol] = target
        have = float(current.get(symbol, 0.0))
        if have != int(have):
            raise TraderError(f"{symbol}: fractional position {have}; auction orders need whole shares")
        legs = []
        if have * target < 0:                 # crossing zero: close first, then open
            legs = [(-int(have), "1"), (target, "2")]
        elif target != have:
            legs = [(int(target - have), "")]
        for delta, leg in legs:
            if delta == 0:
                continue
            side = "buy" if delta > 0 else "sell"
            cid = client_order_id(candidate, session, phase, symbol, side, leg)
            orders.append(Order(symbol, side, abs(delta), TIF[phase], cid, have, target,
                                float(price or 0.0)))
    for o in orders:
        if o.notional > cfg.max_order_fraction * capital:
            raise TraderError(f"{o.symbol}: order notional {o.notional:.0f} exceeds the cap")
    return Plan(phase, session.isoformat(), candidate.id, dict(weights), equity, capital,
                dict(current), targets, orders)


# --------------------------------------------------------------------------
# checks and execution


def check_account(broker: Broker, account: Mapping[str, Any], *, live: bool) -> float:
    if broker.paper == live:
        raise TraderError("broker mode does not match the requested mode")
    if account.get("trading_blocked") or account.get("account_blocked"):
        raise TraderError("the account is blocked from trading")
    if str(account.get("status", "ACTIVE")).upper() != "ACTIVE":
        raise TraderError(f"account status {account.get('status')}")
    equity = float(account["equity"])
    if equity <= 0:
        raise TraderError("non-positive equity")
    return equity


def check_drawdown(cfg: Config, equity: float) -> None:
    marks = [float(r["equity"]) for r in read_journal(cfg) if r.get("event") == "equity"]
    peak = max(marks + [equity])
    if equity < peak * (1.0 - cfg.max_drawdown_halt):
        cfg.halt_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.halt_file.write_text(f"drawdown halt: equity {equity:.2f} against peak {peak:.2f}\n")
        raise TraderError(f"drawdown {1 - equity / peak:.1%} beyond {cfg.max_drawdown_halt:.0%}; "
                          f"halted")


def check_positions(candidate: Candidate, positions: Mapping[str, float], cfg: Config) -> None:
    foreign = sorted(s for s, q in positions.items() if q and s not in candidate.symbols)
    if foreign and not cfg.allow_foreign_positions:
        raise TraderError(f"positions in funds this strategy does not trade: {foreign}. "
                          f"Use a dedicated account.")


def run_phase(broker: Broker, cfg: Config, phase: str, *, now: dt.datetime | None = None,
              submit: bool = False, live: bool = False, ignore_window: bool = False,
              panel_loader: Callable[[Candidate, Config], Panel] = load_live_panel) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    spec = load_spec(cfg.spec_path)
    if live and not spec.get("live_allowed"):
        raise TraderError(f"spec verdict {spec.get('verdict')!r} is paper only")
    candidate = candidate_of(spec)
    if cfg.halt_file.exists():
        raise TraderError(f"halted: {cfg.halt_file.read_text().strip()}")
    today = now.astimezone(NY).date()
    cal = sessions(broker, today)
    session = next((s for s in cal if s.date == today), None)
    if session is None:
        return {"status": "skipped", "reason": "not a trading day", "phase": phase}
    if not ignore_window and not window_open(phase, session, now):
        return {"status": "skipped", "reason": "outside the phase window", "phase": phase}
    if submit and phase_done(cfg, today, phase):
        return {"status": "skipped", "reason": "phase already completed", "phase": phase}
    previous = [s.date for s in cal if s.date < today]
    if not previous:
        raise TraderError("calendar has no previous session")
    panel = panel_loader(candidate, cfg)
    if panel.dates[-1] != previous[-1]:
        raise TraderError(f"stale data: last bar {panel.dates[-1]}, previous session {previous[-1]}")
    future = [s.date for s in cal if s.date > today]
    open_w, close_w, ready = todays_weights(panel, candidate, today, future)
    if not ready:
        raise TraderError("the look-back is not full for today")
    weights = {"open": open_w, "catchup": open_w, "close": close_w,
               "flatten": {s: 0.0 for s in candidate.symbols}}[phase]
    account = broker.account()
    equity = check_account(broker, account, live=live)
    check_drawdown(cfg, equity)
    positions = broker.positions()
    check_positions(candidate, positions, cfg)
    j = {s: i for i, s in enumerate(panel.symbols)}
    prices = {}
    for s in candidate.symbols:
        prices[s] = broker.latest_price(s) or float(panel.close[-1, j[s]])
    plan = build_plan(phase=phase, session=today, candidate=candidate, weights=weights,
                      equity=equity, current=positions, prices=prices, cfg=cfg)
    plan_record = {"phase": phase, "session": today.isoformat(), "candidate": candidate.id,
                   "verdict": spec.get("verdict"), "live": live, "submit": submit,
                   "weights": weights, "targets": plan.targets, "current": plan.current,
                   "equity": equity, "capital": plan.capital, "data_last_bar": str(panel.dates[-1]),
                   "orders": [asdict(o) for o in plan.orders]}
    if not submit:
        return {"status": "planned", **plan_record}
    journal(cfg, "equity", equity=equity, session=today.isoformat())
    journal(cfg, "decision", **plan_record)
    results, failed = [], False
    for order in plan.orders:
        existing = broker.order_by_client_id(order.client_order_id)
        if existing:
            results.append({"client_order_id": order.client_order_id, "status": "exists",
                            "broker_status": existing.get("status")})
            continue
        try:
            placed = broker.submit(symbol=order.symbol, qty=order.qty, side=order.side,
                                   time_in_force=order.time_in_force,
                                   client_order_id=order.client_order_id)
            results.append({"client_order_id": order.client_order_id, "status": "submitted",
                            "broker_id": placed.get("id"), "broker_status": placed.get("status")})
        except BrokerError as error:
            again = broker.order_by_client_id(order.client_order_id)
            if again:
                results.append({"client_order_id": order.client_order_id, "status": "exists",
                                "broker_status": again.get("status")})
                continue
            failed = True
            results.append({"client_order_id": order.client_order_id, "status": "error",
                            "error": str(error), "http": error.status, "body": error.body[:500]})
        journal(cfg, "order", session=today.isoformat(), phase=phase, **results[-1])
    if not failed:
        journal(cfg, "phase_complete", session=today.isoformat(), phase=phase,
                orders=len(plan.orders))
    return {"status": "error" if failed else "submitted", **plan_record, "results": results}


def catchup_needed(cfg: Config, today: dt.date) -> bool:
    return not phase_done(cfg, today, "open") and not phase_done(cfg, today, "catchup")


def loop(broker: Broker, cfg: Config, *, submit: bool, live: bool, interval: float = 30.0,
         iterations: int | None = None, clock: Callable[[], dt.datetime] | None = None,
         sleep: Callable[[float], None] = time.sleep) -> None:
    """Run each phase once per session inside its window; survive errors."""
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    count = 0
    last_problem = None
    while iterations is None or count < iterations:
        count += 1
        now = clock()
        heartbeat = cfg.state_dir / "heartbeat"
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.write_text(now.isoformat())
        local = now.astimezone(NY)
        try:
            due = []
            if OPEN_WINDOW[0] <= local.time() < OPEN_WINDOW[1]:
                due.append("open")
            elif dt.time(9, 35) <= local.time() < dt.time(15, 25) and catchup_needed(cfg, local.date()):
                due.append("catchup")
            if dt.time(12, 0) <= local.time() < dt.time(16, 0):
                due.append("close")
            for phase in due:
                if phase_done(cfg, local.date(), phase):
                    continue
                result = run_phase(broker, cfg, phase, now=now, submit=submit, live=live)
                if result["status"] != "skipped":
                    print(json.dumps({k: result[k] for k in ("status", "phase", "session")
                                      if k in result}), flush=True)
                if phase == "catchup" and result["status"] == "skipped":
                    continue
        except TraderError as error:
            if str(error) != last_problem:      # journal a repeated refusal once
                journal(cfg, "refused", reason=str(error), at=now.isoformat())
                print(f"refused: {error}", file=sys.stderr, flush=True)
            last_problem = str(error)
        except Exception as error:  # keep the loop alive; idempotent ids prevent doubles
            if repr(error) != last_problem:
                journal(cfg, "error", error=repr(error), at=now.isoformat())
                print(f"error: {error!r}", file=sys.stderr, flush=True)
            last_problem = repr(error)
        else:
            last_problem = None
        sleep(interval)


# --------------------------------------------------------------------------
# monitoring


def _default_auctions(symbols: Sequence[str], start: dt.date) -> Mapping[str, Any]:
    from edgefinder.auctions import fetch_auctions
    return fetch_auctions(symbols, start)


def status(broker: Broker, cfg: Config, *, days: int = 60,
           panel_loader: Callable[[Candidate, Config], Panel] = load_live_panel,
           auction_loader: Callable[[Sequence[str], dt.date], Mapping[str, Any]] | None
           = _default_auctions) -> dict[str, Any]:
    """Positions, recent strategy orders, fill shortfall and realised round trips.

    Shortfall is measured against the official auction prints when Alpaca
    serves them, else against the bars.  On a paper account the fills are
    Alpaca's simulation (an opening order fills like a market order at the
    opening quote), so the shortfall there describes the simulator; only live
    fills test the auction assumption.
    """
    spec = load_spec(cfg.spec_path)
    candidate = candidate_of(spec)
    after = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    ours = [o for o in broker.orders(after)
            if str(o.get("client_order_id", "")).startswith(CLIENT_PREFIX + "-")]
    filled = [o for o in ours if o.get("status") == "filled" and o.get("filled_avg_price")]
    panel = panel_loader(candidate, cfg)
    position = {d: i for i, d in enumerate(panel.dates)}
    j = {s: i for i, s in enumerate(panel.symbols)}
    reference = "bar open and close (first and last valid trade)"
    prints: Mapping[str, Any] = {}
    if auction_loader is _default_auctions and getattr(broker, "key", None):
        from edgefinder.auctions import fetch_auctions

        def auction_loader(symbols: Sequence[str], start: dt.date) -> Mapping[str, Any]:
            return fetch_auctions(symbols, start, key=broker.key, secret=broker.secret)
    if auction_loader is not None:
        try:
            prints = auction_loader(candidate.symbols, after.date())
            reference = "official auction prints (largest print per session)"
        except (data.DataError, BrokerError):
            prints = {}
    shortfalls, legs = [], []
    for o in sorted(filled, key=lambda o: o.get("filled_at") or o["submitted_at"]):
        when = dt.datetime.fromisoformat(str(o.get("filled_at") or o["submitted_at"])
                                         .replace("Z", "+00:00")).astimezone(NY).date()
        i, s = position.get(when), o["symbol"]
        price = float(o["filled_avg_price"])
        legs.append((s, o["side"], float(o["filled_qty"]), price, o.get("time_in_force")))
        if i is None or s not in j:
            continue
        kind = "open" if o.get("time_in_force") == "opg" else "close"
        auction = ((prints.get(s) or {}).get(when.isoformat()) or {}).get(kind)
        if prints and not auction:
            continue                  # compare like with like once auctions are in use
        ref = auction["price"] if auction else (panel.open[i, j[s]] if kind == "open"
                                                else panel.close[i, j[s]])
        if math.isfinite(ref):
            sign = 1.0 if o["side"] == "buy" else -1.0
            shortfalls.append(sign * (price / ref - 1.0) * 1e4)
    trips = _round_trips(legs)
    expected = (spec.get("evidence", {}).get("holdout", {}) or {}).get("net_bps_per_trade")
    check = execution_verdict(shortfalls)
    out = {"candidate": candidate.id, "verdict": spec.get("verdict"),
           "account": "paper (simulated auction fills)" if broker.paper else "live",
           "shortfall_reference": reference,
           "positions": broker.positions(), "equity": float(broker.account()["equity"]),
           "orders": len(ours), "filled": len(filled),
           "auction_shortfall_bps": _summary(shortfalls),
           "round_trip_net_bps_before_fees": _summary(trips),
           "backtest_holdout_net_bps_per_trade": expected,
           "execution_check": check,
           "halted": cfg.halt_file.exists()}
    return out


def execution_verdict(shortfall_per_leg: Sequence[float]) -> dict[str, Any]:
    """``pending`` until enough legs; then ``pass``, ``stop`` or ``inconclusive``.

    The round-trip figures are twice the per-leg mean and twice its upper 95%
    bound, so a pass needs the bound, not just the mean, inside the limit.
    """
    n = len(shortfall_per_leg)
    if n < TRIAL_MIN_LEGS:
        return {"verdict": "pending", "legs": n, "needed": TRIAL_MIN_LEGS}
    arr = np.asarray(shortfall_per_leg, float)
    mean = float(arr.mean())
    upper = mean + 1.96 * float(arr.std(ddof=1)) / math.sqrt(n)
    lower = mean - 1.96 * float(arr.std(ddof=1)) / math.sqrt(n)
    rt_mean, rt_upper, rt_lower = 2 * mean, 2 * upper, 2 * lower
    if rt_lower > TRIAL_STOP_ROUND_TRIP_BPS:
        verdict = "stop"
    elif rt_mean <= TRIAL_PASS_ROUND_TRIP_BPS and rt_upper <= TRIAL_STOP_ROUND_TRIP_BPS:
        verdict = "pass"
    else:
        verdict = "inconclusive"
    return {"verdict": verdict, "legs": n, "round_trip_mean_bps": rt_mean,
            "round_trip_upper95_bps": rt_upper, "round_trip_lower95_bps": rt_lower,
            "pass_if_mean_at_most": TRIAL_PASS_ROUND_TRIP_BPS,
            "stop_if_lower_bound_above": TRIAL_STOP_ROUND_TRIP_BPS}


def _round_trips(legs: Sequence[tuple[str, str, float, float, Any]]) -> list[float]:
    open_lots: dict[str, list[tuple[float, float]]] = {}
    out = []
    for symbol, side, qty, price, _ in legs:
        if side == "buy":
            open_lots.setdefault(symbol, []).append((qty, price))
        else:
            lots = open_lots.get(symbol, [])
            while qty > 0 and lots:
                lot_qty, lot_price = lots[0]
                used = min(qty, lot_qty)
                out.append((price / lot_price - 1.0) * 1e4)
                qty -= used
                lots[0] = (lot_qty - used, lot_price)
                if lots[0][0] <= 0:
                    lots.pop(0)
    return out


def _summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    arr = np.asarray(values, float)
    sd = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    return {"n": len(arr), "mean": float(arr.mean()), "median": float(np.median(arr)),
            "t": float(arr.mean() / (sd / math.sqrt(len(arr)))) if sd > 0 else None}


# --------------------------------------------------------------------------
# command line


def offline_broker(cfg: Config, equity: float):
    """A flat fake account dated the next weekday after the last bar (preview only)."""
    from edgefinder.broker import FakeBroker
    spec = load_spec(cfg.spec_path)
    panel = load_live_panel(candidate_of(spec), cfg)
    days = list(panel.dates[-20:])
    day = panel.dates[-1]
    while len(days) < 80:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    today = next(d for d in days if d > panel.dates[-1])
    now = dt.datetime.combine(today, dt.time(15, 30), NY).astimezone(dt.timezone.utc)
    return FakeBroker(equity=equity, now=now, trading_days=days)


def read_secrets_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines (a dotenv file); comments and blank lines are ignored."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip("'\"")
    return out


def make_broker(live: bool) -> AlpacaBroker:
    """Keys come from ``EDGEFINDER_SECRETS_FILE`` (a dedicated account) or the environment."""
    secrets = {}
    if os.getenv("EDGEFINDER_SECRETS_FILE"):
        secrets = read_secrets_file(Path(os.environ["EDGEFINDER_SECRETS_FILE"]))
    key, secret = secrets.get("ALPACA_API_KEY"), secrets.get("ALPACA_SECRET_KEY")
    if live:
        if os.getenv("ALPACA_LIVE_ENABLE", "").lower() != "true":
            raise TraderError("live mode needs ALPACA_LIVE_ENABLE=true")
        return AlpacaBroker(paper=False, key=key, secret=secret)
    return AlpacaBroker(paper=True, key=key, secret=secret)


def health(cfg: Config, max_age: float) -> int:
    beat = cfg.state_dir / "heartbeat"
    if not beat.exists():
        return 1
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(beat.read_text().strip()))
    return 0 if age.total_seconds() <= max_age and not cfg.halt_file.exists() else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trade the frozen edgefinder strategy.")
    parser.add_argument("command",
                        choices=("plan", "run", "loop", "status", "halt", "resume", "health"))
    parser.add_argument("--phase", choices=("open", "close", "catchup", "flatten"), default="close")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--submit", action="store_true", help="send orders (default: plan only)")
    parser.add_argument("--live", action="store_true", help="live account (needs a confirmed spec)")
    parser.add_argument("--ignore-window", action="store_true", help="plan outside the window")
    parser.add_argument("--capital-fraction", type=float, default=0.95)
    parser.add_argument("--data", choices=("yahoo", "alpaca"), default="yahoo")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--max-age", type=float, default=180.0, help="health: heartbeat age")
    parser.add_argument("--offline-equity", type=float, default=None,
                        help="plan without API keys: a flat account of this size and a "
                             "calendar of the data's sessions plus weekdays")
    args = parser.parse_args(argv)
    cfg = Config(spec_path=args.spec, state_dir=args.state, capital_fraction=args.capital_fraction,
                 data_provider=args.data)
    if args.command == "halt":
        cfg.halt_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.halt_file.write_text(f"operator halt at {dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        print(f"halted: {cfg.halt_file}")
        return 0
    if args.command == "resume":
        cfg.halt_file.unlink(missing_ok=True)
        print("resumed")
        return 0
    if args.command == "health":
        return health(cfg, args.max_age)
    try:
        if args.offline_equity is not None:
            if args.command != "plan":
                raise TraderError("--offline-equity only works with plan")
            broker = offline_broker(cfg, args.offline_equity)
            result = run_phase(broker, cfg, args.phase, now=broker.now, ignore_window=True)
            print(json.dumps(result, indent=1, default=str))
            return 0
        broker = make_broker(args.live)
        if args.command == "status":
            print(json.dumps(status(broker, cfg), indent=1, default=str))
            return 0
        if args.command == "loop":
            loop(broker, cfg, submit=args.submit, live=args.live, interval=args.interval)
            return 0
        if args.submit and args.ignore_window:
            raise TraderError("--ignore-window is for planning only")
        result = run_phase(broker, cfg, args.phase, submit=args.command == "run" and args.submit,
                           live=args.live, ignore_window=args.ignore_window)
        print(json.dumps(result, indent=1, default=str))
        return 0 if result["status"] in ("planned", "submitted", "skipped") else 1
    except (TraderError, BrokerError, data.DataError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
