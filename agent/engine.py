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

import ccxt

from . import brain, market, state
from .alerts import AlertManager
from .exchange import CredentialError, Exchange
from .risk import RiskEngine

log = logging.getLogger("engine")

# Consecutive cycles that may fail on credentials before the loop gives up.
# One spurious rejection should not stop a live agent; a revoked key should.
MAX_CREDENTIAL_FAILURES = 3


class Engine:
    def __init__(self, cfg: dict, light: bool = False):
        self.cfg = cfg
        self.alerts = AlertManager(cfg)
        if not light:
            state.check_journal()
            if cfg["mode"] == "live":
                self.alerts.require_live_ready(probe=True)
        self.ex = Exchange(
            cfg, self.alerts, validate_account=not light)
        if not light:
            self.ex.verify_account_safety(require_trade=True, refresh=True)
        if not light:
            self.llm = brain.LLM(cfg)
            self.llm.preflight()
            self.risk = RiskEngine(cfg)
        self.universe: list[str] = []
        self.universe_ts = 0.0
        self._startup_reconciled = False
        self._credential_failures = 0

    # ------------------------------------------------------------ lifecycle

    def run(self, run_lock=None) -> None:
        owns_lock = run_lock is None
        if run_lock is None:
            run_lock = state.acquire_run_lock()
        if run_lock is None:
            pid = state.read_pid()
            raise RuntimeError(
                "another agent loop already holds the run lock"
                + (f" (pid {pid})" if pid else ""))
        st = state.load_state()
        if "state file was corrupt" in str(st.get("kill_reason") or ""):
            self.alerts.send(
                "critical", "corrupt_state_kill",
                "State corruption forced the agent into KILLED mode",
                {"reason": st.get("kill_reason")})
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
                    if st.get("flatten_on_kill", True):
                        log.warning(
                            "Kill flag detected; flattening and exiting.")
                        self.flatten_all(st.get("kill_reason") or "kill flag")
                    else:
                        log.warning(
                            "Kill flag detected with keep-positions; exiting "
                            "without touching positions or protective orders.")
                    break
                try:
                    self.cycle(st)
                    self._credential_failures = 0
                except SystemExit:
                    raise
                except state.JournalError:
                    raise
                except (CredentialError, ccxt.AuthenticationError) as e:
                    if self._on_credential_failure(e):
                        break
                except Exception as e:
                    log.exception("Cycle error (agent continues): %s", e)
                    state.log_event("error", str(e))
                    self.alerts.send(
                        "error", "cycle_error", "Trading cycle failed",
                        {"error": str(e)})
                self._wait_for_next_cycle()
        except state.JournalError as exc:
            reason = f"durable journal unavailable: {exc}"
            log.critical("Agent stopped: %s", reason)
            try:
                state.set_state(
                    state.PAUSED, reason, operator_pause=True)
            except Exception as state_exc:
                log.critical("Could not persist journal-failure pause: %s",
                             state_exc)
            self.alerts.send(
                "critical", "journal_failure_stop",
                "Agent stopped because its durable audit journal failed",
                {"error": str(exc)})
        except KeyboardInterrupt:
            if state.load_state()["state"] == state.KILLED:
                log.warning("Interrupted during a kill; state stays KILLED.")
            else:
                log.warning("Interrupted. State set to PAUSED. Open positions "
                            "keep their exchange-side stop-loss/take-profit "
                            "orders on OKX.")
                state.set_state(state.PAUSED)
        finally:
            if owns_lock:
                state.release_run_lock(run_lock)

    def _wait_for_next_cycle(self) -> None:
        """Sleep responsively so a kill is observed within about one second."""
        deadline = time.monotonic() + int(
            self.cfg["cycle"]["interval_seconds"])
        while time.monotonic() < deadline:
            if state.load_state()["state"] == state.KILLED:
                return
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    def _on_credential_failure(self, exc: Exception) -> bool:
        """Handle an auth/clock rejection. True means stop the loop.

        Deliberately PAUSED, not KILLED: a kill flattens everything, and
        flattening needs the very API access we just lost, so a kill here
        would fail loudly and change nothing. Pausing stops the agent from
        pretending to trade while leaving open positions under their
        exchange-side stop-loss/take-profit, which survive this process.
        """
        self._credential_failures += 1
        n = self._credential_failures
        log.error("Credential/clock failure %d of %d: %s",
                  n, MAX_CREDENTIAL_FAILURES, exc)
        state.log_event("error", f"credentials: {exc}")
        if n < MAX_CREDENTIAL_FAILURES:
            self.alerts.send(
                "error", "credential_failure",
                f"OKX rejected our credentials ({n}/{MAX_CREDENTIAL_FAILURES}); "
                "stopping the agent if this keeps up",
                {"error": str(exc)})
            return False
        reason = f"OKX credentials rejected {n} cycles running: {exc}"
        try:
            open_symbols = [p["symbol"] for p in self.ex.positions()]
        except Exception:
            open_symbols = ["unknown - could not query OKX"]
        state.set_state(state.PAUSED, reason, operator_pause=True)
        log.critical(
            "Agent stopped: %s. Open positions keep their exchange-side "
            "SL/TP on OKX and are NOT being managed. Fix the credentials, "
            "run 'python main.py check', then 'python main.py resume'. "
            "Positions at stop time: %s", reason, open_symbols or "none")
        self.alerts.send(
            "critical", "credential_failure_stop",
            "Agent STOPPED: OKX credentials rejected. Open positions are "
            "unmanaged (exchange-side SL/TP still active).",
            {"error": str(exc), "open_positions": open_symbols})
        return True

    # ------------------------------------------------------------ the cycle

    @staticmethod
    def _ensure_equity_basis(st: dict, equity: float, now: float) -> bool:
        """Rebase legacy account-wide benchmarks to USDT equity exactly once.

        Before ``usdt_currency_equity_v1``, OKX ``totalEq`` made demo OKB and
        other currencies look like USDT trading capital. Comparing the new
        USDT-only value with those old benchmarks would create a false loss,
        so the first cycle resets both references before any breaker runs.
        """
        if st.get("equity_basis") == state.EQUITY_BASIS:
            return False
        previous = {
            "high_water_mark": st.get("high_water_mark"),
            "day_start_equity": st.get("day_start_equity"),
        }
        st["equity_basis"] = state.EQUITY_BASIS
        st["high_water_mark"] = equity
        st["day_start_equity"] = equity
        st["day"] = datetime.fromtimestamp(
            now, timezone.utc).strftime("%Y-%m-%d")
        # Current USDT equity already contains every earlier transfer. Moving
        # the cursor prevents one of those transfers being added a second time
        # to the freshly rebased references.
        st["last_ledger_ts"] = int(now * 1000)
        state.log_event("equity_basis_migration", json.dumps({
            "basis": state.EQUITY_BASIS,
            "previous": previous,
            "rebased_usdt_equity": equity,
        }))
        log.warning(
            "Equity basis migrated to USDT-only; benchmarks rebased to %.2f "
            "USDT (non-USDT assets are excluded)", equity)
        return True

    def cycle(self, st: dict) -> None:
        now = time.time()
        # A host that booted with a good clock can still drift into OKX's
        # 30s signing window later; warn before it starts rejecting orders.
        self.ex.recheck_clock_if_due()
        self.ex.recheck_account_safety_if_due()
        equity = self.ex.equity_usdt()
        if equity <= 0:
            log.warning("Equity reads as 0; skipping cycle")
            return
        if self._ensure_equity_basis(st, equity, now):
            state.commit(st)

        # Exchange state is authoritative. Reconcile fills and protection
        # before strategy decisions or account-level risk calculations.
        positions = self.ex.positions()
        try:
            positions = self._reconcile_positions(
                positions, st, startup=not self._startup_reconciled)
            self._startup_reconciled = True
            reconciled = True
        except Exception as exc:
            # A reconciliation problem must not disable the account-level
            # circuit breakers below. Trade conservatively instead: keep
            # housekeeping, closes and breakers, but open nothing new.
            log.error("Reconciliation failed; no new entries this cycle: %s",
                      exc)
            state.log_event("error", f"reconciliation: {exc}")
            self.alerts.send(
                "error", "reconciliation_failed",
                "Reconciliation failed; new entries disabled this cycle",
                {"error": str(exc)})
            reconciled = False
        state.commit(st)

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
            self.alerts.send(
                "critical", "max_drawdown_kill",
                f"Maximum drawdown reached: {drawdown_pct:.2f}%",
                {"equity": equity, "high_water_mark": st["high_water_mark"]})
            # Persist the terminal state before touching the exchange. If the
            # flatten is interrupted or fails, a restart must not resume
            # trading on the next process invocation.
            state.commit(st, kill=f"max drawdown {drawdown_pct:.1f}%")
            try:
                self.flatten_all("max drawdown breached")
            finally:
                # flatten_all commits its own execution state. Do not merge
                # this cycle's pre-flatten active-trade map over those closes.
                fresh = state.load_state()
                st.clear()
                st.update(fresh)
            raise SystemExit(1)

        day_pnl_pct = (equity - st["day_start_equity"]) / st["day_start_equity"] * 100
        if (day_pnl_pct <= -float(r["daily_loss_limit_pct"])
                and st["state"] == state.RUNNING):
            log.warning("Daily loss limit hit (%.1f%%). No new entries until "
                        "the next UTC day.", day_pnl_pct)
            state.commit(st, transition=(state.RUNNING, state.DAY_STOPPED))
            state.log_event("daily_stop", f"{day_pnl_pct:.2f}%")
            self.alerts.send(
                "warning", "daily_loss_stop",
                f"Daily loss stop reached: {day_pnl_pct:.2f}%",
                {"equity": equity})
            if r.get("flatten_on_daily_stop"):
                self.flatten_all("daily loss limit")
                fresh = state.load_state()
                st.clear()
                st.update(fresh)
        state.commit(st)

        # --- universe refresh
        refresh_s = float(self.cfg["universe"]["refresh_minutes"]) * 60
        if not self.universe or now - self.universe_ts > refresh_s:
            self.universe = market.build_universe(self.ex, self.cfg)
            self.universe_ts = now
            log.info("Universe (%d): %s", len(self.universe),
                     ", ".join(self.universe))

        # --- position housekeeping (runs in every state except KILLED)
        positions = self._manage_positions(positions, st, equity)

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
        # While DAY_STOPPED the engine drops every open, so tell the model
        # zero — otherwise it wastes output proposing entries that can't run.
        # The same applies when reconciliation failed: closes may still make
        # sense, but nothing new opens on top of unverified state.
        max_new = (int(r["max_concurrent_positions"]) - len(positions)
                   if st["state"] == state.RUNNING and reconciled else 0)
        try:
            decisions = self.llm.decide(snapshot, portfolio, max_new)
        except Exception as e:
            log.error("LLM call failed; holding this cycle: %s", e)
            return
        if decisions:
            state.log_event("decisions", json.dumps(decisions))
        # Pick up any pause/kill the CLI issued while the LLM call was running.
        state.commit(st)
        if st["state"] not in (state.RUNNING, state.DAY_STOPPED):
            return

        # --- closes first
        for d in [d for d in decisions if d["action"] == "close"]:
            if state.load_state()["state"] not in (
                    state.RUNNING, state.DAY_STOPPED):
                return
            pos = next((p for p in positions
                        if p.get("symbol") == d.get("symbol")), None)
            if pos and self._close(pos,
                                   "model close: " + d.get("reasoning", ""),
                                   st):
                positions.remove(pos)

        # --- opens (blocked while DAY_STOPPED or after a failed reconcile,
        # even if the model proposed one despite being told max_new = 0)
        if st["state"] != state.RUNNING or not reconciled:
            return
        gross = sum(self._notional(p) for p in positions)
        opens = sorted([d for d in decisions if d["action"] == "open"],
                       key=lambda d: d.get("confidence", 0), reverse=True)
        for d in opens:
            latest = state.load_state()
            if latest["state"] != state.RUNNING:
                return
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
                                  "notional": plan["notional"],
                                  "side": plan["direction"]})
        state.commit(st)

    # ------------------------------------------------------ reconciliation

    @staticmethod
    def _direction(pos: dict) -> str:
        side = str(pos.get("side") or "").lower()
        if side in {"long", "short"}:
            return side
        raw = float((pos.get("info") or {}).get("pos") or 0)
        return "long" if raw >= 0 else "short"

    @staticmethod
    def _position_id(pos: dict):
        return pos.get("id") or (pos.get("info") or {}).get("posId")

    def _reconcile_positions(self, positions: list[dict], st: dict,
                             startup: bool = False) -> list[dict]:
        """Match local trades to exchange positions and verify SL/TP coverage."""
        actual = {p["symbol"]: p for p in positions}
        if len(actual) != len(positions):
            raise RuntimeError(
                "OKX returned multiple positions for one symbol; net_mode "
                "cannot be proven")
        active = st.setdefault("active_trades", {})
        protection = st.setdefault("protection", {})
        opened_at = st.setdefault("opened_at", {})
        cooldowns = st.setdefault("cooldowns", {})

        # A tracked trade that disappeared was closed by exchange-side SL/TP
        # or another operator. Recover actual fills before allowing a re-entry.
        for symbol, trade in list(active.items()):
            live = actual.get(symbol)
            live_id = self._position_id(live) if live else None
            same_direction = (live is not None and self._direction(live)
                              == trade.get("direction"))
            same_id = (not live_id or not trade.get("position_id")
                       or str(live_id) == str(trade.get("position_id")))
            if live is not None and same_direction and same_id:
                continue
            replaced = live is not None
            try:
                summary = self.ex.closed_position_summary(
                    symbol, int(float(trade.get("opened_at") or 0) * 1000),
                    trade["direction"], float(trade.get("entry_price") or 0),
                    float(trade.get("qty") or 0))
                realized = float(summary.get("realized_pnl_usd") or 0)
                if summary.get("status") != "position_history":
                    realized -= float(trade.get("entry_fee_usd") or 0)
                # Partial closes were already journaled with their own
                # realized share (which covered their exit fee and entry-fee
                # portion); the final close row carries only the remainder so
                # summing a trade's rows never double-counts.
                realized -= float(trade.get("partial_realized_pnl_usd") or 0)
                if summary.get("status") == "fill_history_funding_unavailable":
                    self.alerts.send(
                        "warning", "funding_reconciliation_incomplete",
                        f"Funding could not be recovered for {symbol}",
                        {"trade_id": trade.get("trade_id")})
                entry_notional = float(trade.get("entry_notional") or 0)
                pnl_pct = realized / entry_notional * 100 if entry_notional else None
                state.log_trade(
                    symbol,
                    "sell" if trade["direction"] == "long" else "buy",
                    "close", summary.get("qty") or trade.get("qty"),
                    summary.get("price") or 0, entry_notional,
                    trade.get("leverage") or 0,
                    "exchange-side exit reconciled", pnl_pct=pnl_pct,
                    trade_id=trade.get("trade_id"),
                    fee_usd=summary.get("fee_usd") or 0,
                    funding_usd=summary.get("funding_usd") or 0,
                    realized_pnl_usd=realized,
                    risk_usd=trade.get("risk_usd"),
                    fill_status=summary.get("status"),
                )
                if realized < 0:
                    cooldown = float(
                        self.cfg["risk"]["cooldown_minutes_after_loss"])
                    cooldowns[symbol] = time.time() + cooldown * 60
                state.log_event("reconciled_close", json.dumps({
                    "symbol": symbol, "trade_id": trade.get("trade_id"),
                    "realized_pnl_usd": realized,
                }))
                active.pop(symbol, None)
                protection.pop(symbol, None)
                opened_at.pop(symbol, None)
                if not replaced:
                    # The attached-OCO entry path cleans itself up, but
                    # restored or fallback protection uses separate SL/TP
                    # orders: cancel any survivor so a later re-entry cannot
                    # inherit a stale trigger from this finished trade. Never
                    # cancel when a replacement position occupies the symbol —
                    # that would strip the replacement's own protection.
                    try:
                        self.ex.cancel_symbol(symbol)
                    except Exception as cleanup_exc:
                        log.warning("stale protective-order cleanup failed "
                                    "for %s: %s", symbol, cleanup_exc)
            except Exception as exc:
                log.error("Could not reconcile disappeared position %s: %s",
                          symbol, exc)
                cooldowns[symbol] = max(
                    float(cooldowns.get(symbol) or 0),
                    time.time() + int(self.cfg["cycle"]["interval_seconds"]) * 2)
                self.alerts.send(
                    "error", "close_reconciliation_failed",
                    f"Could not reconcile the closed {symbol} trade",
                    {"error": str(exc)})
                if replaced:
                    raise RuntimeError(
                        f"{symbol} live position no longer matches durable state"
                    ) from exc

        for symbol, pos in list(actual.items()):
            direction = self._direction(pos)
            contracts = abs(float(pos.get("contracts") or 0))
            mark = float(pos.get("markPrice") or pos.get("last")
                         or pos.get("entryPrice") or 0)
            status = self.ex.protection_status(
                symbol, contracts, direction, mark)

            if symbol not in active:
                # Adopt a pre-existing position so its eventual exit remains
                # measurable. Unknown positions are never assumed protected.
                trade_id = state.new_trade_id()
                entry = float(pos.get("entryPrice") or mark)
                notional = self._notional(pos)
                active[symbol] = {
                    "trade_id": trade_id,
                    "direction": direction,
                    "opened_at": float(opened_at.get(symbol) or time.time()),
                    "entry_price": entry,
                    "entry_notional": notional,
                    "qty": contracts,
                    "initial_qty": contracts,
                    "position_id": self._position_id(pos),
                    "leverage": float(pos.get("leverage") or 0),
                    "entry_fee_usd": 0.0,
                    "entry_fee_remaining_usd": 0.0,
                    "partial_realized_pnl_usd": 0.0,
                    "risk_usd": None,
                    "adopted": True,
                }
                opened_at[symbol] = active[symbol]["opened_at"]
                state.log_trade(
                    symbol, "buy" if direction == "long" else "sell", "open",
                    contracts, entry, notional, pos.get("leverage") or 0,
                    "position adopted during startup reconciliation",
                    trade_id=trade_id, fill_status="adopted")
                protection[symbol] = {
                    "side": direction, "contracts": contracts,
                    "sl_price": status.get("stop_price"),
                    "tp_price": status.get("take_price"),
                }
                self.alerts.send(
                    "warning", "position_adopted",
                    f"Adopted existing {direction} position in {symbol}",
                    {"protected": bool(status.get("stop_loss"))})

            target = protection.get(symbol) or {}
            if not status.get("stop_loss") or not status.get("take_profit"):
                sl_price = target.get("sl_price")
                tp_price = target.get("tp_price")
                if sl_price and tp_price:
                    try:
                        status = self.ex.ensure_protection(
                            symbol, direction, contracts, float(sl_price),
                            float(tp_price), mark)
                    except Exception as exc:
                        log.error("Protection restore failed for %s: %s",
                                  symbol, exc)
                if not status.get("stop_loss"):
                    self.alerts.send(
                        "critical", "startup_position_unprotected",
                        f"{symbol} has no verified stop-loss; closing it",
                        {"startup": startup})
                    if self._close(
                            pos, "reconciliation: stop-loss missing", st):
                        actual.pop(symbol, None)
                    else:
                        self.alerts.send(
                            "critical", "unprotected_close_failed",
                            f"{symbol} remains open without a verified stop-loss",
                            {"startup": startup})
                        raise RuntimeError(
                            f"{symbol} remains open without verified protection")
                    continue
                if not status.get("take_profit"):
                    self.alerts.send(
                        "error", "startup_take_profit_missing",
                        f"{symbol} has a stop-loss but no verified take-profit",
                        {"startup": startup})
            target.update({
                "side": direction, "contracts": contracts,
                "sl_price": target.get("sl_price") or status.get("stop_price"),
                "tp_price": target.get("tp_price") or status.get("take_price"),
            })
            protection[symbol] = target

        # Remove old max-hold timestamps that no longer identify any trade.
        for symbol in list(opened_at):
            if symbol not in actual and symbol not in active:
                opened_at.pop(symbol, None)
        if startup:
            log.info("Startup reconciliation complete: %d open position(s)",
                     len(actual))
        return list(actual.values())

    # ------------------------------------------------------------ execution

    def _execute_open(self, plan: dict, st: dict) -> bool:
        symbol = plan["symbol"]
        if state.load_state()["state"] != state.RUNNING:
            log.info("Control state changed; skipping entry for %s", symbol)
            return False
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
        else:
            side = "sell"

        try:
            entry_guard = self.ex.guarded_entry_limit(
                symbol, side, contracts,
                float(self.cfg["execution"]["max_spread_pct"]),
                float(self.cfg["execution"][
                    "max_order_book_slippage_pct"]),
                float(self.cfg["execution"]["max_market_data_age_seconds"]),
            )
        except Exception as exc:
            log.warning("Entry liquidity guard rejected %s: %s", symbol, exc)
            return False

        entry_reference = float(entry_guard["mid"])
        book_sized_contracts = self.ex.contracts_for_notional(
            symbol, plan["notional"], entry_reference)
        contracts = min(contracts, book_sized_contracts)
        if contracts <= 0:
            log.info("Order-book-priced size below minimum for %s", symbol)
            return False
        if plan["direction"] == "long":
            sl_price = entry_reference * (1 - plan["sl_pct"] / 100)
            tp_price = entry_reference * (1 + plan["tp_pct"] / 100)
        else:
            sl_price = entry_reference * (1 + plan["sl_pct"] / 100)
            tp_price = entry_reference * (1 - plan["tp_pct"] / 100)

        # If the spread widened after analysis, reduce contracts again before
        # submitting the IOC order so the all-in stop budget remains hard.
        estimated_loss_pct = float(
            plan.get("estimated_loss_pct") or plan["sl_pct"])
        live_loss_pct = estimated_loss_pct + max(
            0.0, float(entry_guard["spread_pct"])
            - float(plan.get("spread_pct") or 0))
        risk_budget = float(plan.get("risk_budget_usd") or 0)
        if risk_budget > 0 and live_loss_pct > 0:
            live_notional_cap = risk_budget / (live_loss_pct / 100)
            if live_notional_cap < float(plan["notional"]):
                cost_adjusted_contracts = self.ex.contracts_for_notional(
                    symbol, live_notional_cap, entry_reference)
                contracts = min(contracts, cost_adjusted_contracts)
                if contracts <= 0:
                    log.info("Live cost-adjusted size below minimum for %s",
                             symbol)
                    return False
                plan["notional"] = live_notional_cap
        plan["estimated_loss_pct"] = live_loss_pct

        if state.load_state()["state"] != state.RUNNING:
            log.info("Control state changed before order; skipping %s", symbol)
            return False

        try:
            execution = self.ex.open_position(
                symbol, side, contracts, plan["leverage"], sl_price, tp_price,
                expected_price=entry_reference,
                entry_limit_price=entry_guard["limit_price"])
        except Exception as e:
            log.error("Entry failed for %s: %s", symbol, e)
            return False

        filled = float(execution["filled"])
        fill_price = float(execution["average"])
        contract_size = float(
            self.ex.x.market(symbol).get("contractSize") or 1)
        actual_notional = filled * contract_size * fill_price
        trade_id = state.new_trade_id()
        opened = time.time()
        estimated_loss_pct = float(plan["estimated_loss_pct"])
        risk_usd = actual_notional * estimated_loss_pct / 100.0
        st.setdefault("opened_at", {})[symbol] = opened
        st.setdefault("active_trades", {})[symbol] = {
            "trade_id": trade_id,
            "direction": plan["direction"],
            "opened_at": opened,
            "entry_price": fill_price,
            "entry_notional": actual_notional,
            "qty": filled,
            "initial_qty": filled,
            "position_id": execution.get("position_id"),
            "leverage": plan["leverage"],
            "entry_fee_usd": float(execution.get("fee_usd") or 0),
            "entry_fee_remaining_usd": float(execution.get("fee_usd") or 0),
            "partial_realized_pnl_usd": 0.0,
            "risk_usd": risk_usd,
        }
        st.setdefault("protection", {})[symbol] = {
            "side": plan["direction"],
            "contracts": float(execution.get("position_contracts") or filled),
            "sl_price": sl_price,
            "tp_price": tp_price,
        }
        plan["notional"] = actual_notional
        plan["risk_usd"] = risk_usd
        persistence_error = None
        try:
            # Make the verified exchange fill durable before any journal
            # failure can pause the loop. Startup reconciliation can recover
            # the position from state if the audit write fails afterward.
            state.commit(st)
            state.log_trade(
                symbol, side, "open", filled, fill_price, actual_notional,
                plan["leverage"], plan["reason"],
                confidence=plan["confidence"], trade_id=trade_id,
                order_id=execution.get("order_id"),
                fee_usd=execution.get("fee_usd") or 0,
                risk_usd=risk_usd,
                fill_status=("partial" if execution.get("partial") else
                             execution.get("status")),
                slippage_usd=execution.get("slippage_usd") or 0)
        except Exception as exc:
            persistence_error = (
                exc if isinstance(exc, state.JournalError) else
                state.JournalError(f"post-entry persistence failed: {exc}"))
            log.critical("Post-entry persistence failed for %s: %s",
                         symbol, exc)

        if not (execution.get("protection") or {}).get("stop_loss"):
            # Persist the verified fill before the emergency close so even a
            # process crash leaves a durable, reconcilable trade record. A
            # persistence failure must never prevent this exchange close.
            emergency_position = {
                "symbol": symbol,
                "contracts": float(
                    execution.get("position_contracts") or filled),
                "side": plan["direction"],
                "entryPrice": fill_price,
                "markPrice": fill_price,
                "leverage": plan["leverage"],
                "info": {},
            }
            close_error = None
            closed = False
            current_position = emergency_position
            verified_flat = False
            for attempt in range(3):
                try:
                    closed = self._close(
                        current_position,
                        "emergency close: stop-loss verification failed", st)
                except Exception as exc:
                    close_error = exc
                    if persistence_error is None:
                        persistence_error = (
                            exc if isinstance(exc, state.JournalError) else
                            state.JournalError(
                                f"emergency-close persistence failed: {exc}"))
                    log.critical(
                        "Emergency-close bookkeeping failed for %s: %s",
                        symbol, exc)
                if closed:
                    break
                try:
                    remaining = self.ex.position(
                        symbol, plan["direction"])
                except Exception as verify_exc:
                    close_error = close_error or verify_exc
                    log.critical(
                        "Could not verify emergency close for %s: %s",
                        symbol, verify_exc)
                    break
                if remaining is None:
                    closed = True
                    verified_flat = True
                    break
                current_position = remaining
                if attempt < 2:
                    time.sleep(0.25)
            if verified_flat:
                st.get("opened_at", {}).pop(symbol, None)
                st.get("active_trades", {}).pop(symbol, None)
                st.get("protection", {}).pop(symbol, None)
                try:
                    state.commit(st)
                except Exception as cleanup_exc:
                    if persistence_error is None:
                        persistence_error = state.JournalError(
                            f"emergency-close cleanup failed: {cleanup_exc}")
                    log.critical(
                        "Could not persist emergency-close cleanup for "
                        "%s: %s", symbol, cleanup_exc)
            if not closed:
                self.alerts.send(
                    "critical", "emergency_close_failed",
                    f"{symbol} remains open without a verified stop-loss",
                    {"contracts": emergency_position["contracts"]})
                raise RuntimeError(
                    f"{symbol} emergency close failed after unprotected fill"
                ) from (close_error or persistence_error)
            if persistence_error is not None:
                raise persistence_error
            return False
        if persistence_error is not None:
            raise persistence_error
        log.info("OPENED %s %s | notional %.0f USDT | %.1fx | SL %.2f%% "
                 "all-in risk %.2f%% | TP %.2f%% | conf %.2f | %s",
                 plan["direction"].upper(), symbol, actual_notional,
                 plan["leverage"], plan["sl_pct"], estimated_loss_pct,
                 plan["tp_pct"],
                 plan["confidence"], plan["reason"])
        return True

    def _close(self, pos: dict, reason: str, st: dict) -> bool:
        symbol = pos["symbol"]
        try:
            execution = self.ex.close_position(pos)
        except Exception as e:
            log.error("Close failed for %s: %s", symbol, e)
            return False
        trade = (st.get("active_trades") or {}).get(symbol) or {}
        direction = trade.get("direction") or self._direction(pos)
        if not execution.get("fully_closed"):
            remaining = float(execution.get("remaining_contracts") or 0)
            filled = float(execution.get("filled") or 0)
            fill_price = float(execution.get("average") or 0)
            entry_price = float(
                trade.get("entry_price") or pos.get("entryPrice") or 0)
            contract_size = float(
                self.ex.x.market(symbol).get("contractSize") or 1)
            gross = (fill_price - entry_price) * filled * contract_size * (
                1 if direction == "long" else -1)
            initial_qty = float(trade.get("initial_qty") or
                                trade.get("qty") or filled or 1)
            entry_fee_total = float(trade.get("entry_fee_usd") or 0)
            entry_fee_remaining = float(trade.get(
                "entry_fee_remaining_usd", entry_fee_total))
            entry_fee_share = min(
                entry_fee_remaining,
                entry_fee_total * min(1.0, filled / initial_qty),
            )
            exit_fee = float(execution.get("fee_usd") or 0)
            partial_realized = gross - entry_fee_share - exit_fee
            if trade:
                trade["qty"] = remaining
                trade["entry_fee_remaining_usd"] = max(
                    0.0, entry_fee_remaining - entry_fee_share)
                trade["partial_realized_pnl_usd"] = float(
                    trade.get("partial_realized_pnl_usd") or 0
                ) + partial_realized
            if symbol in (st.get("protection") or {}):
                st["protection"][symbol]["contracts"] = remaining
            state.log_trade(
                symbol, "sell" if direction == "long" else "buy",
                "partial_close", filled, fill_price, self._notional(pos),
                float(pos.get("leverage") or 0), reason,
                trade_id=trade.get("trade_id"),
                order_id=execution.get("order_id"),
                fee_usd=exit_fee, realized_pnl_usd=partial_realized,
                risk_usd=trade.get("risk_usd"),
                fill_status="partial",
                slippage_usd=execution.get("slippage_usd") or 0)
            state.commit(st)
            return False

        price = float(execution.get("average") or pos.get("markPrice") or 0)
        qty = float(execution.get("filled") or pos.get("contracts") or 0)
        entry_price = float(trade.get("entry_price") or pos.get("entryPrice") or 0)
        entry_notional = float(trade.get("entry_notional") or self._notional(pos))
        entry_fee = float(trade.get(
            "entry_fee_remaining_usd", trade.get("entry_fee_usd") or 0))
        exit_fee = float(execution.get("fee_usd") or 0)
        funding_raw = (pos.get("info") or {}).get("fundingFee")
        if funding_raw in (None, "") and trade.get("opened_at"):
            funding_raw = self.ex.funding_since(
                symbol, int(float(trade["opened_at"]) * 1000))
        funding = float(funding_raw or 0)
        contract_size = float(
            self.ex.x.market(symbol).get("contractSize") or 1)
        move = price - entry_price
        gross_pnl = move * qty * contract_size * (
            1 if direction == "long" else -1)
        realized = (float(trade.get("partial_realized_pnl_usd") or 0)
                    + gross_pnl - entry_fee - exit_fee + funding)
        pnl_pct = realized / entry_notional * 100 if entry_notional else None
        state.log_trade(
            symbol, "sell" if direction == "long" else "buy", "close", qty,
            price, entry_notional, float(pos.get("leverage") or 0), reason,
            pnl_pct=pnl_pct, trade_id=trade.get("trade_id"),
            order_id=execution.get("order_id"), fee_usd=exit_fee,
            funding_usd=funding, realized_pnl_usd=realized,
            risk_usd=trade.get("risk_usd"),
            fill_status=execution.get("status"),
            slippage_usd=execution.get("slippage_usd") or 0)
        if realized < 0:
            cooldown = float(self.cfg["risk"]["cooldown_minutes_after_loss"])
            st.setdefault("cooldowns", {})[symbol] = time.time() + cooldown * 60
        st.get("opened_at", {}).pop(symbol, None)
        st.get("active_trades", {}).pop(symbol, None)
        st.get("protection", {}).pop(symbol, None)
        state.commit(st)
        log.info("CLOSED %s (%s, %+.2f USDT realized): %s",
                 symbol, direction, realized, reason)
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

        try:
            usage = self.ex.margin_usage_pct()
        except Exception as exc:
            self.alerts.send(
                "critical", "margin_risk_unavailable",
                "New entries blocked because OKX margin risk is unavailable",
                {"error": str(exc)})
            raise RuntimeError(
                f"cannot enforce margin-usage guard: {exc}") from exc
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
                "min_confidence": r["min_confidence"],
                "max_net_direction_pct": r.get("max_net_direction_pct", 100),
            },
            "trading_costs_fyi": self.cfg["trading_costs"],
        }

    # ------------------------------------------------------------- flatten

    def flatten_all(self, reason: str) -> bool:
        log.warning("FLATTEN ALL (close positions, then cancel orders): %s",
                    reason)
        try:
            st = state.load_state()
        except Exception as exc:
            log.critical(
                "local state unavailable during flatten; exchange remains "
                "authoritative: %s", exc)
            st = {"opened_at": {}, "active_trades": {}, "protection": {},
                  "cooldowns": {}}
        failed = []
        bookkeeping_failures = []
        for p in self.ex.positions():
            try:
                closed = self._close(p, f"flatten: {reason}", st)
            except Exception as exc:
                # The exchange close happens before journaling. Verify the
                # exchange directly and keep flattening other symbols even if
                # local persistence fails during an emergency.
                log.critical("close bookkeeping failed during flatten for %s: %s",
                             p.get("symbol"), exc)
                bookkeeping_failures.append(str(p.get("symbol")))
                try:
                    remaining = self.ex.position(
                        p["symbol"], self._direction(p))
                except Exception:
                    remaining = p
                closed = remaining is None
                if closed:
                    st.get("opened_at", {}).pop(p["symbol"], None)
                    st.get("active_trades", {}).pop(p["symbol"], None)
                    st.get("protection", {}).pop(p["symbol"], None)
                    try:
                        state.commit(st)
                    except Exception as state_exc:
                        log.critical(
                            "could not persist post-flatten state for %s: %s",
                            p.get("symbol"), state_exc)
            if not closed:
                failed.append(str(p.get("symbol")))
        if bookkeeping_failures:
            self.alerts.send(
                "critical", "flatten_bookkeeping_failed",
                "One or more emergency closes lost local bookkeeping",
                {"symbols": bookkeeping_failures, "reason": reason})
        orders_cleared = True
        if failed:
            log.error("FLATTEN INCOMPLETE; still open: %s. Close them "
                      "manually on OKX.", ", ".join(failed))
            self.alerts.send(
                "critical", "flatten_incomplete",
                "One or more positions remained open after flatten",
                {"symbols": failed, "reason": reason})
        else:
            try:
                self.ex.cancel_everything()
            except Exception as e:
                orders_cleared = False
                log.error("cancel_everything after flatten: %s", e)
                self.alerts.send(
                    "error", "order_cancel_incomplete",
                    "Positions are flat but some orders may remain",
                    {"error": str(e), "reason": reason})
        try:
            state.log_event("flatten", reason)
        except state.JournalError as exc:
            # Closing risk takes precedence during an emergency. Report the
            # lost audit write, but do not describe a successful flatten as a
            # failed close merely because SQLite became unavailable afterward.
            log.critical("flatten completed but journal write failed: %s", exc)
            self.alerts.send(
                "critical", "flatten_journal_failed",
                "Flatten completed but could not be written to the journal",
                {"error": str(exc), "reason": reason})
        return not failed and orders_cleared
