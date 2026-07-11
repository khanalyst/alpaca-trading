"""The engine: one loop that ties everything together.

Each cycle:
 1. Sync equity; detect deposits/withdrawals via the OKX ledger and rebase
    the drawdown / daily-loss benchmarks accordingly.
 2. Enforce account-level circuit breakers (max drawdown -> flatten + kill,
    daily loss limit -> no new entries until the next UTC day).
 3. Refresh the top-volume universe when stale.
 4. Housekeeping on open positions (max hold time, margin-usage guard).
 5. Ask the LLM for decisions; execute closes, then risk-vetted opens.

State semantics:
  RUNNING     - full operation
  DAY_STOPPED - daily loss limit hit: model may close, cannot open
  PAUSED      - housekeeping only: no LLM calls, no new entries
  KILLED      - flatten everything and exit
"""

import json
import logging
import time
from datetime import datetime, timezone

from . import brain, market, state
from .exchange import Exchange
from .risk import RiskEngine

log = logging.getLogger("engine")


class Engine:
    def __init__(self, cfg: dict, light: bool = False):
        self.cfg = cfg
        self.ex = Exchange(cfg)
        if not light:
            self.llm = brain.LLM(cfg)
            self.risk = RiskEngine(cfg)
        self.universe: list[str] = []
        self.universe_ts = 0.0

    # ------------------------------------------------------------ lifecycle

    def run(self) -> None:
        state.write_pid()
        st = state.load_state()
        if st["state"] == state.PAUSED and not st.get("operator_pause"):
            st = state.set_state(state.RUNNING)
        elif st["state"] == state.PAUSED:
            log.info("Operator pause is in effect; housekeeping only until "
                     "'python main.py resume'.")
        log.info("Agent loop started | mode=%s | state=%s | llm=%s/%s",
                 self.cfg["mode"].upper(), st["state"],
                 self.cfg["llm"]["provider"], self.cfg["llm"]["model"])
        try:
            while True:
                st = state.load_state()
                if st["state"] == state.KILLED:
                    log.warning("Kill flag detected; flattening and exiting.")
                    self.flatten_all(st.get("kill_reason") or "kill flag")
                    break
                try:
                    self.cycle(st)
                except SystemExit:
                    raise
                except Exception as e:
                    log.exception("Cycle error (agent continues): %s", e)
                    state.log_event("error", str(e))
                time.sleep(int(self.cfg["cycle"]["interval_seconds"]))
        except KeyboardInterrupt:
            if state.load_state()["state"] == state.KILLED:
                log.warning("Interrupted during a kill; state stays KILLED.")
            else:
                log.warning("Interrupted. State set to PAUSED. Open positions "
                            "keep their exchange-side stop-loss/take-profit "
                            "orders on OKX.")
                state.set_state(state.PAUSED)
        finally:
            state.clear_pid()

    # ------------------------------------------------------------ the cycle

    def cycle(self, st: dict) -> None:
        now = time.time()
        equity = self.ex.equity_usdt()
        if equity <= 0:
            log.warning("Equity reads as 0; skipping cycle")
            return

        # --- drop expired per-symbol cooldowns
        st["cooldowns"] = {s: t for s, t in (st.get("cooldowns") or {}).items()
                           if float(t) > now}

        # --- deposits / withdrawals: rebase benchmarks, never trade on them
        since = int(st.get("last_ledger_ts") or (now - 3600) * 1000)
        net_transfer, next_since = self.ex.transfers_since(since)
        if abs(net_transfer) > 0.01:
            log.info("Net transfer detected: %+.2f USDT; rebasing benchmarks",
                     net_transfer)
            if st.get("high_water_mark"):
                st["high_water_mark"] = max(
                    1e-9, st["high_water_mark"] + net_transfer)
            if st.get("day_start_equity"):
                st["day_start_equity"] = max(
                    1e-9, st["day_start_equity"] + net_transfer)
            state.log_event("transfer", json.dumps({"net_usdt": net_transfer}))
        st["last_ledger_ts"] = next_since

        # --- UTC day rollover
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if st.get("day") != today:
            st["day"] = today
            st["day_start_equity"] = equity
            if st["state"] == state.DAY_STOPPED:
                state.commit(st, transition=(state.DAY_STOPPED, state.RUNNING))
                log.info("New UTC day; trading re-enabled")

        if not st.get("high_water_mark"):
            st["high_water_mark"] = equity
        st["high_water_mark"] = max(st["high_water_mark"], equity)
        state.log_equity(equity, st["state"])

        # --- circuit breakers
        r = self.cfg["risk"]
        drawdown_pct = (st["high_water_mark"] - equity) / st["high_water_mark"] * 100
        if drawdown_pct >= float(r["max_drawdown_pct"]):
            log.error("MAX DRAWDOWN breached (%.1f%% from high-water mark). "
                      "Flattening everything and self-killing.", drawdown_pct)
            self.flatten_all("max drawdown breached")
            state.commit(st, kill=f"max drawdown {drawdown_pct:.1f}%")
            raise SystemExit(1)

        day_pnl_pct = (equity - st["day_start_equity"]) / st["day_start_equity"] * 100
        if (day_pnl_pct <= -float(r["daily_loss_limit_pct"])
                and st["state"] == state.RUNNING):
            log.warning("Daily loss limit hit (%.1f%%). No new entries until "
                        "the next UTC day.", day_pnl_pct)
            state.commit(st, transition=(state.RUNNING, state.DAY_STOPPED))
            state.log_event("daily_stop", f"{day_pnl_pct:.2f}%")
            if r.get("flatten_on_daily_stop"):
                self.flatten_all("daily loss limit")
        state.commit(st)

        # --- universe refresh
        refresh_s = float(self.cfg["universe"]["refresh_minutes"]) * 60
        if not self.universe or now - self.universe_ts > refresh_s:
            self.universe = market.build_universe(self.ex, self.cfg)
            self.universe_ts = now
            log.info("Universe (%d): %s", len(self.universe),
                     ", ".join(self.universe))

        # --- position housekeeping (runs in every state except KILLED)
        positions = self._manage_positions(self.ex.positions(), st, equity)

        if st["state"] not in (state.RUNNING, state.DAY_STOPPED):
            return  # PAUSED: no LLM calls, no new trades
        if st["state"] == state.DAY_STOPPED and not positions:
            return  # opens blocked and nothing held: an LLM call cannot act

        # --- build snapshot and ask the brain
        symbols = list(dict.fromkeys(
            self.universe + [p["symbol"] for p in positions]))
        snapshot = market.market_snapshot(self.ex, symbols, self.cfg)
        if not snapshot:
            log.warning("Empty market snapshot; holding")
            return

        portfolio = self._portfolio_view(equity, positions, st,
                                         day_pnl_pct, drawdown_pct)
        max_new = int(r["max_concurrent_positions"]) - len(positions)
        try:
            decisions = self.llm.decide(snapshot, portfolio, max_new)
        except Exception as e:
            log.error("LLM call failed; holding this cycle: %s", e)
            return
        if decisions:
            state.log_event("decisions", json.dumps(decisions))
        # Pick up any pause/kill the CLI issued while the LLM call was running.
        state.commit(st)

        # --- closes first
        for d in [d for d in decisions if d["action"] == "close"]:
            pos = next((p for p in positions
                        if p.get("symbol") == d.get("symbol")), None)
            if pos and self._close(pos,
                                   "model close: " + d.get("reasoning", ""),
                                   st):
                positions.remove(pos)

        # --- opens (blocked while DAY_STOPPED)
        if st["state"] != state.RUNNING:
            return
        gross = sum(self._notional(p) for p in positions)
        opens = sorted([d for d in decisions if d["action"] == "open"],
                       key=lambda d: d.get("confidence", 0), reverse=True)
        for d in opens:
            plan, why = self.risk.vet_open(d, equity, positions, snapshot,
                                           st.get("cooldowns", {}), gross)
            if not plan:
                log.info("Rejected %s %s: %s", d.get("direction"),
                         d.get("symbol"), why)
                state.log_event("rejected", json.dumps(
                    {"symbol": d.get("symbol"), "why": why}))
                continue
            if self._execute_open(plan, st):
                gross += plan["notional"]
                positions.append({"symbol": plan["symbol"],
                                  "notional": plan["notional"]})
        state.commit(st)

    # ------------------------------------------------------------ execution

    def _execute_open(self, plan: dict, st: dict) -> bool:
        symbol = plan["symbol"]
        try:
            live = self.ex.price(symbol)
        except Exception as e:
            log.warning("Price check failed for %s: %s", symbol, e)
            return False
        guard = float(self.cfg["execution"]["slippage_guard_pct"])
        if abs(live - plan["price"]) / plan["price"] * 100 > guard:
            log.info("Slippage guard: %s moved more than %.2f%%; skipping",
                     symbol, guard)
            return False

        contracts = self.ex.contracts_for_notional(symbol, plan["notional"], live)
        if contracts <= 0:
            log.info("Size below exchange minimum for %s; skipping", symbol)
            return False

        if plan["direction"] == "long":
            side = "buy"
            sl_price = live * (1 - plan["sl_pct"] / 100)
            tp_price = live * (1 + plan["tp_pct"] / 100)
        else:
            side = "sell"
            sl_price = live * (1 + plan["sl_pct"] / 100)
            tp_price = live * (1 - plan["tp_pct"] / 100)

        try:
            self.ex.open_position(symbol, side, contracts, plan["leverage"],
                                  sl_price, tp_price)
        except Exception as e:
            log.error("Entry failed for %s: %s", symbol, e)
            return False

        st.setdefault("opened_at", {})[symbol] = time.time()
        state.log_trade(symbol, side, "open", contracts, live,
                        plan["notional"], plan["leverage"], plan["reason"])
        log.info("OPENED %s %s | notional %.0f USDT | %.1fx | SL %.2f%% "
                 "TP %.2f%% | conf %.2f | %s",
                 plan["direction"].upper(), symbol, plan["notional"],
                 plan["leverage"], plan["sl_pct"], plan["tp_pct"],
                 plan["confidence"], plan["reason"])
        return True

    def _close(self, pos: dict, reason: str, st: dict) -> bool:
        symbol = pos["symbol"]
        try:
            self.ex.close_position(pos)
        except Exception as e:
            log.error("Close failed for %s: %s", symbol, e)
            return False
        try:
            price = self.ex.price(symbol)
        except Exception:
            price = float(pos.get("markPrice") or 0)
        upnl_pct = float(pos.get("percentage") or 0)
        state.log_trade(symbol,
                        "sell" if pos.get("side") == "long" else "buy",
                        "close", abs(float(pos.get("contracts") or 0)), price,
                        self._notional(pos), float(pos.get("leverage") or 0),
                        reason)
        if upnl_pct < 0:
            cooldown = float(self.cfg["risk"]["cooldown_minutes_after_loss"])
            st.setdefault("cooldowns", {})[symbol] = time.time() + cooldown * 60
        st.get("opened_at", {}).pop(symbol, None)
        state.commit(st)
        log.info("CLOSED %s (%s, %.2f%% uPnL at close): %s",
                 symbol, pos.get("side"), upnl_pct, reason)
        return True

    # --------------------------------------------------------- housekeeping

    def _manage_positions(self, positions: list[dict], st: dict,
                          equity: float) -> list[dict]:
        r = self.cfg["risk"]
        now = time.time()
        kept = []
        for p in positions:
            opened = st.get("opened_at", {}).get(p["symbol"])
            if (opened and now - opened > float(r["max_hold_hours"]) * 3600
                    and self._close(p, "max hold time reached", st)):
                continue
            kept.append(p)

        usage = self.ex.margin_usage_pct()
        while (usage is not None and usage > float(r["max_margin_usage_pct"])
               and kept):
            kept.sort(key=self._notional, reverse=True)
            biggest = kept.pop(0)
            log.warning("Margin usage %.0f%% above %.0f%% cap; closing "
                        "largest position %s", usage,
                        float(r["max_margin_usage_pct"]), biggest["symbol"])
            if not self._close(biggest, "margin usage guard", st):
                kept.insert(0, biggest)
                break
            time.sleep(2)  # let the close settle before re-reading margin
            usage = self.ex.margin_usage_pct()
        return kept

    # ------------------------------------------------------------- helpers

    def _notional(self, pos: dict) -> float:
        n = pos.get("notional")
        if n:
            return abs(float(n))
        try:
            m = self.ex.x.market(pos["symbol"])
            return (abs(float(pos.get("contracts") or 0))
                    * float(m.get("contractSize") or 1)
                    * float(pos.get("markPrice") or 0))
        except Exception:
            return 0.0

    def _portfolio_view(self, equity: float, positions: list[dict], st: dict,
                        day_pnl_pct: float, drawdown_pct: float) -> dict:
        views = []
        for p in positions:
            opened = st.get("opened_at", {}).get(p["symbol"])
            views.append({
                "symbol": p["symbol"],
                "side": p.get("side"),
                "entry": p.get("entryPrice"),
                "mark": p.get("markPrice"),
                "upnl_pct": round(float(p.get("percentage") or 0), 2),
                "leverage": p.get("leverage"),
                "notional_usd": round(self._notional(p), 1),
                "hours_open": round((time.time() - opened) / 3600, 1)
                if opened else None,
            })
        r = self.cfg["risk"]
        return {
            "equity_usdt": round(equity, 2),
            "day_pnl_pct": round(day_pnl_pct, 2),
            "drawdown_from_high_pct": round(drawdown_pct, 2),
            "state": st["state"],
            "open_positions": views,
            "hard_limits_fyi": {
                "max_leverage": r["max_leverage"],
                "risk_per_trade_pct": r["risk_per_trade_pct"],
                "max_concurrent_positions": r["max_concurrent_positions"],
            },
        }

    # ------------------------------------------------------------- flatten

    def flatten_all(self, reason: str) -> bool:
        log.warning("FLATTEN ALL (cancel every order, close every position): %s",
                    reason)
        try:
            self.ex.cancel_everything()
        except Exception as e:
            log.error("cancel_everything: %s", e)
        st = state.load_state()
        failed = []
        for p in self.ex.positions():
            if not self._close(p, f"flatten: {reason}", st):
                failed.append(str(p.get("symbol")))
        if failed:
            log.error("FLATTEN INCOMPLETE; still open: %s. Close them "
                      "manually on OKX.", ", ".join(failed))
        state.log_event("flatten", reason)
        return not failed
