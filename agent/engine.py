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
import math
import os
import time
from datetime import datetime, timezone

import ccxt

from . import brain, market, state, strategy
from .alerts import AlertManager
from .exchange import (CredentialError, EntryLiquidityRejected,
                       EntryOrderRejected, Exchange)
from .risk import RiskEngine

log = logging.getLogger("engine")

# Consecutive cycles that may fail on credentials before the loop gives up.
# One spurious rejection should not stop a live agent; a revoked key should.
MAX_CREDENTIAL_FAILURES = 3


class PositionAgeUnknown(RuntimeError):
    """A held position cannot be proven to be inside max-hold policy."""

    def __init__(self, symbols: list[str]):
        self.symbols = sorted(set(symbols))
        super().__init__(
            "cannot recover opening time for held position(s): "
            + ", ".join(self.symbols))


class Engine:
    def __init__(self, cfg: dict, light: bool = False):
        self.cfg = cfg
        # Protect library/direct callers as well as main.py: state and journal
        # access must already be scoped to, and bound to, this exact key/mode.
        state.configure_runtime(cfg["mode"])
        state.bind_runtime_identity(
            cfg["mode"], os.environ.get("OKX_API_KEY", ""))
        self.run_id = state.new_run_id()
        self.config_version = state.stable_fingerprint(cfg)
        self.code_version = state.code_fingerprint()
        self.prompt_version = brain.PROMPT_VERSION
        self.strategy_id, self.strategy_version = strategy.identity(cfg)
        state.set_journal_context(
            run_id=self.run_id,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            prompt_version=self.prompt_version,
            config_version=self.config_version,
            code_version=self.code_version,
        )
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
        if not hasattr(self, "run_id"):
            self.run_id = state.new_run_id()
        if not hasattr(self, "strategy_id"):
            self.strategy_id, self.strategy_version = strategy.identity(
                self.cfg)
        if not hasattr(self, "prompt_version"):
            self.prompt_version = brain.PROMPT_VERSION
        if not hasattr(self, "config_version"):
            self.config_version = state.stable_fingerprint(self.cfg)
        if not hasattr(self, "code_version"):
            self.code_version = state.code_fingerprint()
        state.set_journal_context(
            run_id=self.run_id,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            prompt_version=self.prompt_version,
            config_version=self.config_version,
            code_version=self.code_version,
        )
        st = state.load_state()
        state.log_run(
            self.run_id,
            mode=self.cfg["mode"],
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            model=self.cfg["llm"]["model"],
            prompt_version=self.prompt_version,
            config_version=self.config_version,
            code_version=self.code_version,
        )
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
            if st.get("equity_basis_id"):
                state.set_journal_context(
                    equity_basis_id=st["equity_basis_id"])
                return False
            st["equity_basis_id"] = state.new_equity_basis_id()
            state.set_journal_context(
                equity_basis_id=st["equity_basis_id"])
            state.log_event("equity_basis_segment_started", json.dumps({
                "basis": state.EQUITY_BASIS,
                "basis_id": st["equity_basis_id"],
                "reason": "existing USDT basis assigned a segment ID",
            }))
            return True
        previous = {
            "high_water_mark": st.get("high_water_mark"),
            "day_start_equity": st.get("day_start_equity"),
        }
        st["equity_basis"] = state.EQUITY_BASIS
        st["equity_basis_id"] = state.new_equity_basis_id()
        state.set_journal_context(
            equity_basis_id=st["equity_basis_id"])
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
            "basis_id": st["equity_basis_id"],
            "previous": previous,
            "rebased_usdt_equity": equity,
        }))
        log.warning(
            "Equity basis migrated to USDT-only; benchmarks rebased to %.2f "
            "USDT (non-USDT assets are excluded)", equity)
        return True

    def cycle(self, st: dict) -> None:
        now = time.time()
        state.set_journal_context(
            cycle_id=state.new_cycle_id(),
            equity_basis_id=st.get("equity_basis_id"),
        )
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
        invalid_position_metrics = []
        for p in positions:
            # Normalize once so the net-direction guard and portfolio view
            # never meet a position whose side/notional ccxt left unset.
            p["side"] = self._direction(p)
            p["notional"] = self._notional(p)
            if p["side"] not in {"long", "short"} or p["notional"] <= 0:
                p["_risk_notional_invalid"] = True
                invalid_position_metrics.append(str(p.get("symbol") or "?"))
        if invalid_position_metrics:
            detail = {
                "symbols": invalid_position_metrics,
                "why": "non-finite or unavailable position risk metrics",
            }
            log.error("Position risk metrics invalid for %s; new entries "
                      "disabled this cycle", ", ".join(invalid_position_metrics))
            state.log_event("error", json.dumps(detail, separators=(",", ":")))
            self.alerts.send(
                "error", "position_metrics_invalid",
                "New entries disabled because held-position risk metrics are "
                "invalid", detail)
        try:
            positions = self._reconcile_positions(
                positions, st, startup=not self._startup_reconciled)
            self._startup_reconciled = True
            reconciled = not invalid_position_metrics
        except PositionAgeUnknown as exc:
            # Keep verified exchange-side protection in place, preserve the
            # adopted trade metadata, and require an operator decision. A
            # guessed "opened now" timestamp would silently reset max-hold.
            state.commit(st)
            paused = state.set_state(
                state.PAUSED,
                "position age is unknown; inspect OKX and flatten or resume "
                "only after resolving: " + ", ".join(exc.symbols),
                operator_pause=True,
            )
            st.clear()
            st.update(paused)
            detail = {"symbols": exc.symbols}
            log.critical(
                "Position age unavailable for %s; agent PAUSED",
                ", ".join(exc.symbols))
            state.log_event(
                "position_age_unknown", self._audit_json(detail))
            self.alerts.send(
                "critical", "position_age_unknown",
                "Agent paused because max-hold age cannot be verified",
                detail)
            reconciled = False
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
        st["entry_feedback"] = {
            symbol: feedback
            for symbol, feedback in (st.get("entry_feedback") or {}).items()
            if float(feedback.get("expires_at") or 0) > now
        }
        st["entry_failures"] = {
            symbol: failure
            for symbol, failure in (st.get("entry_failures") or {}).items()
            if float(failure.get("expires_at") or 0) > now
        }
        st["recent_setups"] = strategy.prune_records(
            st.get("recent_setups") or {}, now)

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
        state.log_equity(
            equity, st["state"], st.get("equity_basis_id"))

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
        if self.universe_ts <= 0 or now - self.universe_ts > refresh_s:
            self.universe, universe_audit = market.select_universe(
                self.ex, self.cfg)
            self.universe_ts = now
            state.log_event(
                "universe_selection", self._audit_json(universe_audit))
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
        # The model is the one nondeterministic component in the pipeline.
        # Journal the exact provider request and the raw provider result so
        # every parsed decision - and every silent hold - can be reconstructed.
        self._journal_llm_input(snapshot, portfolio, max_new)
        try:
            decisions = self.llm.decide(snapshot, portfolio, max_new)
        except Exception as e:
            self._journal_llm_output()
            log.error("LLM call failed; holding this cycle: %s", e)
            state.log_event("error", f"llm: {e}")
            self.alerts.send(
                "error", "llm_call_failed",
                "LLM call failed; holding this cycle", {"error": str(e)})
            return
        self._journal_llm_output()
        # An empty list is a real decision ("no trade"); journal it too so
        # the audit trail distinguishes a deliberate hold from a failed call.
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
            if pos:
                trade = (st.get("active_trades") or {}).get(
                    d.get("symbol")) or {}
                state.log_event(
                    "model_close_audit",
                    self._audit_json({
                        "symbol": d.get("symbol"),
                        "trade_id": trade.get("trade_id"),
                        "setup_id": trade.get("setup_id"),
                        "close_trigger": d.get("close_trigger"),
                        "evidence_change": d.get("evidence_change"),
                        "reasoning": d.get("reasoning"),
                        "original_thesis": {
                            "reason": trade.get("entry_reason"),
                            "entry_evidence": trade.get("entry_evidence"),
                            "invalidation_anchor": trade.get(
                                "invalidation_anchor"),
                            "exit_policy": trade.get("exit_policy"),
                        },
                    }),
                    setup_id=trade.get("setup_id"),
                )
            if pos and self._close(
                    pos,
                    "model close: " + d.get("reasoning", ""),
                    st,
                    close_trigger=d.get("close_trigger"),
                    close_evidence=d.get("evidence_change")):
                positions.remove(pos)

        # --- opens (blocked while DAY_STOPPED or after a failed reconcile,
        # even if the model proposed one despite being told max_new = 0)
        if st["state"] != state.RUNNING or not reconciled:
            return
        gross = sum(self._notional(p) for p in positions)
        opens, conflicted = self._sorted_opens(decisions)
        for d in conflicted:
            log.info("Rejected %s %s: open and close on the same symbol in "
                     "one reply", d.get("direction"), d.get("symbol"))
            state.log_event("rejected", json.dumps(
                {"symbol": d.get("symbol"),
                 "why": "open and close on the same symbol in one reply"}))
        for d in opens:
            latest = state.load_state()
            if latest["state"] != state.RUNNING:
                return
            prepared, why = self._prepare_setup_decision(
                d, snapshot, st)
            if not prepared:
                log.info("Rejected %s %s: %s", d.get("direction"),
                         d.get("symbol"), why)
                state.log_event("rejected", json.dumps(
                    {"symbol": d.get("symbol"), "why": why}))
                continue
            plan, why = self.risk.vet_open(
                prepared, equity, positions, snapshot,
                                           st.get("cooldowns", {}), gross,
                                           st.get("entry_feedback", {}),
                                           st.get("entry_failures", {}),
                                           st.get("active_trades", {}))
            if not plan:
                self._mark_setup_status(
                    st, prepared["setup_id"], "risk_rejected")
                log.info("Rejected %s %s: %s", prepared.get("direction"),
                         prepared.get("symbol"), why)
                state.log_event("rejected", json.dumps(
                    {"symbol": prepared.get("symbol"), "why": why,
                     "setup_id": prepared["setup_id"]}),
                    setup_id=prepared["setup_id"])
                if str(why).startswith("liquidity retry"):
                    self._backoff_ignored_liquidity_feedback(
                        st, prepared.get("symbol"), str(why))
                continue
            plan["entry_equity_usd"] = equity
            self._mark_setup_status(
                st, plan["setup_id"], "attempted")
            if self._execute_open(plan, st):
                self._mark_setup_status(
                    st, plan["setup_id"], "opened")
                gross += plan["notional"]
                positions.append({"symbol": plan["symbol"],
                                  "notional": plan["notional"],
                                  "side": plan["direction"]})
            else:
                setup_record = (st.get("recent_setups") or {}).get(
                    plan["setup_id"]) or {}
                if setup_record.get("status") != "closed":
                    self._mark_setup_status(
                        st, plan["setup_id"], "execution_rejected")
        state.commit(st)

    # ------------------------------------------------------ reconciliation

    @staticmethod
    def _direction(pos: dict) -> str:
        side = str(pos.get("side") or "").lower()
        if side in {"long", "short"}:
            return side
        raw = float((pos.get("info") or {}).get("pos") or 0)
        return "long" if raw >= 0 else "short"

    def _liquidation_stop_check(
            self, direction: str, mark_price: object, stop_price: object,
            liquidation_price: object) -> dict:
        """Verify a valid OKX liquidation estimate remains beyond the stop."""
        if liquidation_price in (None, "", "0", 0):
            return {
                "available": False,
                "safe": True,
                "buffer_pct": None,
                "reason": "liquidation price unavailable",
            }
        try:
            mark = float(mark_price)
            stop = float(stop_price)
            liquidation = float(liquidation_price)
        except (TypeError, ValueError):
            return {
                "available": True,
                "safe": False,
                "buffer_pct": None,
                "reason": "liquidation measurement is invalid",
            }
        if (direction not in {"long", "short"}
                or not all(math.isfinite(value) and value > 0
                           for value in (mark, stop, liquidation))):
            return {
                "available": True,
                "safe": False,
                "buffer_pct": None,
                "reason": "liquidation measurement is invalid",
            }
        if direction == "long":
            geometrically_valid = liquidation < stop < mark
            buffer_pct = (stop - liquidation) / mark * 100
        else:
            geometrically_valid = liquidation > stop > mark
            buffer_pct = (liquidation - stop) / mark * 100
        minimum = float(
            self.cfg["risk"]["min_stop_liquidation_buffer_pct"])
        safe = geometrically_valid and buffer_pct >= minimum
        return {
            "available": True,
            "safe": safe,
            "buffer_pct": buffer_pct,
            "minimum_buffer_pct": minimum,
            "reason": (
                "stop precedes liquidation"
                if safe else
                "stop is not safely between mark and liquidation "
                f"(buffer {buffer_pct:.2f}%, minimum {minimum:.2f}%)"
            ),
        }

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
        unknown_age: list[str] = []

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
                opened = float(
                    trade.get("opened_at")
                    or opened_at.get(symbol) or 0)
                if opened > 0:
                    trade["opened_at"] = opened
                    trade["age_known"] = True
                    opened_at[symbol] = opened
                if not trade.get("age_known", opened > 0) or opened <= 0:
                    recovered = self.ex.position_opened_at(live)
                    if recovered is None:
                        trade["opened_at"] = 0.0
                        trade["age_known"] = False
                        opened_at.pop(symbol, None)
                        unknown_age.append(symbol)
                    else:
                        trade["opened_at"] = float(recovered)
                        trade["age_known"] = True
                        opened_at[symbol] = float(recovered)
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
                partial_realized = float(
                    trade.get("partial_realized_pnl_usd") or 0)
                realized -= partial_realized
                total_realized = partial_realized + realized
                if summary.get("status") == "fill_history_funding_unavailable":
                    self.alerts.send(
                        "warning", "funding_reconciliation_incomplete",
                        f"Funding could not be recovered for {symbol}",
                        {"trade_id": trade.get("trade_id")})
                entry_notional = float(trade.get("entry_notional") or 0)
                pnl_pct = (
                    total_realized / entry_notional * 100
                    if entry_notional else None)
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
                    funding_status=(
                        "unavailable"
                        if summary.get("status")
                        == "fill_history_funding_unavailable"
                        else "available"),
                    strategy_id=trade.get("strategy_id"),
                    strategy_version=trade.get("strategy_version"),
                    setup_id=trade.get("setup_id"),
                    setup_key=trade.get("setup_key"),
                    setup_type=trade.get("setup_type"),
                    signal_ts=trade.get("signal_ts"),
                    exit_policy=trade.get("exit_policy"),
                    invalidation_anchor=trade.get("invalidation_anchor"),
                    close_trigger="exchange_protection",
                    close_evidence=(
                        "position disappeared from OKX and actual exit "
                        "fills were reconciled"),
                )
                if total_realized < 0:
                    cooldown = float(
                        self.cfg["risk"]["cooldown_minutes_after_loss"])
                    cooldowns[symbol] = time.time() + cooldown * 60
                state.log_event("reconciled_close", json.dumps({
                    "symbol": symbol, "trade_id": trade.get("trade_id"),
                    "incremental_realized_pnl_usd": realized,
                    "total_realized_pnl_usd": total_realized,
                    "setup_id": trade.get("setup_id"),
                }), setup_id=trade.get("setup_id"))
                self._mark_setup_status(
                    st, trade.get("setup_id"), "closed", cooldown=True,
                    realized_pnl_usd=total_realized)
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
                recovered_opened_at = opened_at.get(symbol)
                if not recovered_opened_at:
                    recovered_opened_at = self.ex.position_opened_at(pos)
                age_known = bool(recovered_opened_at)
                active[symbol] = {
                    "trade_id": trade_id,
                    "direction": direction,
                    "opened_at": float(recovered_opened_at or 0),
                    "age_known": age_known,
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
                    "strategy_id": "external",
                    "strategy_version": "adopted-v1",
                    "setup_type": "adopted",
                    "run_id": getattr(
                        self, "run_id", state.journal_context().get("run_id")
                        or "unknown-run"),
                    "cycle_id": state.journal_context().get("cycle_id"),
                }
                if age_known:
                    opened_at[symbol] = active[symbol]["opened_at"]
                else:
                    opened_at.pop(symbol, None)
                    unknown_age.append(symbol)
                state.log_trade(
                    symbol, "buy" if direction == "long" else "sell", "open",
                    contracts, entry, notional, pos.get("leverage") or 0,
                    "position adopted during startup reconciliation",
                    trade_id=trade_id, fill_status="adopted",
                    funding_status="unknown",
                    strategy_id="external",
                    strategy_version="adopted-v1",
                    setup_type="adopted",
                    entry_equity_usd=None)
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
            liquidation = (
                pos.get("liquidationPrice")
                or (pos.get("info") or {}).get("liqPx")
            )
            liquidation_check = self._liquidation_stop_check(
                direction,
                mark,
                status.get("stop_price") or target.get("sl_price"),
                liquidation,
            )
            if (liquidation_check["available"]
                    and not liquidation_check["safe"]):
                detail = {
                    "symbol": symbol,
                    "liquidation_price": liquidation,
                    "mark_price": mark,
                    **liquidation_check,
                }
                self.alerts.send(
                    "critical", "liquidation_buffer_unsafe",
                    f"{symbol} stop is too close to liquidation; closing it",
                    detail)
                if self._close(
                        pos, "reconciliation: unsafe liquidation buffer", st):
                    actual.pop(symbol, None)
                    state.log_event(
                        "liquidation_buffer_unsafe",
                        self._audit_json(detail),
                    )
                    continue
                raise RuntimeError(
                    f"{symbol} remains open with unsafe liquidation buffer")
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
        if unknown_age:
            raise PositionAgeUnknown(unknown_age)
        return list(actual.values())

    # ------------------------------------------------------------ execution

    def _remember_liquidity_rejection(
            self, plan: dict, st: dict,
            rejection: EntryLiquidityRejected) -> dict:
        """Persist depth feedback for one model-directed smaller retry."""
        now = time.time()
        cfg = self.cfg["execution"]
        details = rejection.details
        symbol = plan["symbol"]
        requested = float(details["requested_contracts"])
        available = float(details["available_contracts"])
        requested_notional = float(details["requested_notional_usdt"])
        available_notional = float(details["available_notional_usdt"])
        max_slippage_pct = float(details["max_slippage_pct"])
        margin_pct = float(plan.get("margin_pct_equity") or 0)
        values = (
            requested, available, requested_notional, available_notional,
            max_slippage_pct, margin_pct,
        )
        if (not all(math.isfinite(value) for value in values)
                or requested <= 0 or available < 0 or available >= requested
                or requested_notional <= 0 or available_notional < 0
                or margin_pct <= 0):
            raise RuntimeError(
                f"{symbol} returned invalid structured liquidity feedback")

        records = st.setdefault("entry_feedback", {})
        previous = records.get(symbol) or {}
        related = (
            previous.get("direction") == plan["direction"]
            and float(previous.get("expires_at") or 0) > now
        )
        count = (int(previous.get("consecutive_rejections") or 0) + 1
                 if related else 1)
        ratio = available / requested
        buffer_fraction = float(
            cfg["liquidity_depth_buffer_pct"]) / 100.0
        max_retry_pct = min(100.0, margin_pct * ratio * buffer_fraction)
        retries = int(cfg["liquidity_retries_before_backoff"])
        blocked_until = (
            now + float(cfg["liquidity_backoff_minutes"]) * 60
            if count > retries else 0.0
        )
        expires_at = max(
            now + float(cfg["liquidity_feedback_ttl_minutes"]) * 60,
            blocked_until,
        )
        record = {
            "reason": "insufficient_depth",
            "direction": plan["direction"],
            "last_rejected_at": now,
            "expires_at": expires_at,
            "blocked_until": blocked_until,
            "consecutive_rejections": count,
            "requested_contracts": requested,
            "available_contracts": available,
            "requested_notional_usdt": requested_notional,
            "available_notional_usdt": available_notional,
            "available_ratio": ratio,
            "max_retry_size_pct_equity": max_retry_pct,
            "max_slippage_pct": max_slippage_pct,
        }
        records[symbol] = record
        event = {"symbol": symbol, **record}
        state.log_event("entry_liquidity_rejected", self._audit_json(event))
        state.commit(st)
        if blocked_until:
            log.info(
                "%s entered a %.0f-minute liquidity backoff after %d "
                "depth rejections", symbol,
                float(cfg["liquidity_backoff_minutes"]), count)
        return record

    def _backoff_ignored_liquidity_feedback(
            self, st: dict, symbol: str | None, reason: str) -> None:
        """Rate-limit a model that repeats a rejected pair at full size."""
        if not symbol:
            return
        record = (st.get("entry_feedback") or {}).get(symbol)
        if not record:
            return
        now = time.time()
        blocked_until = now + float(
            self.cfg["execution"]["liquidity_backoff_minutes"]) * 60
        record["blocked_until"] = max(
            float(record.get("blocked_until") or 0), blocked_until)
        record["expires_at"] = max(
            float(record.get("expires_at") or 0), record["blocked_until"])
        state.log_event("entry_liquidity_backoff", self._audit_json({
            "symbol": symbol,
            "reason": reason,
            "blocked_until": record["blocked_until"],
        }))
        state.commit(st)

    def _remember_entry_failure(
            self, plan: dict, st: dict, exc: Exception,
            stage: str) -> dict:
        """Persist non-liquidity entry failure with bounded backoff."""
        now = time.time()
        symbol = plan["symbol"]
        details = (
            dict(exc.details)
            if isinstance(exc, EntryOrderRejected)
            else {
                "stage": stage,
                "classification": "transient",
                "error_code": None,
                "error_message": Exchange._safe_exchange_error_text(exc),
                "http_status": None,
                "result_rows": [],
                "order_audit": getattr(exc, "_order_audit", None),
            }
        )
        classification = str(
            details.get("classification") or "transient")
        if classification not in {"transient", "permanent"}:
            classification = "transient"
        records = st.setdefault("entry_failures", {})
        previous = records.get(symbol) or {}
        related = (
            previous.get("direction") == plan["direction"]
            and previous.get("stage") == details.get("stage", stage)
            and float(previous.get("expires_at") or 0) > now
        )
        count = (int(previous.get("consecutive_failures") or 0) + 1
                 if related else 1)
        execution = self.cfg["execution"]
        base = float(execution["entry_failure_backoff_minutes"])
        maximum = float(execution["entry_failure_backoff_max_minutes"])
        delay = min(maximum, base * (2 ** min(count - 1, 10)))
        if classification == "permanent":
            delay = max(
                delay, float(self.cfg["universe"]["refresh_minutes"]))
        blocked_until = now + delay * 60
        expires_at = max(
            blocked_until,
            now + float(execution["entry_failure_ttl_minutes"]) * 60,
        )
        record = {
            "reason": "exchange_rejected",
            "direction": plan["direction"],
            "stage": str(details.get("stage") or stage),
            "classification": classification,
            "error_code": (
                str(details["error_code"])
                if details.get("error_code") not in (None, "") else None
            ),
            "error_message": Exchange._safe_exchange_error_text(
                details.get("error_message")),
            "last_failed_at": now,
            "blocked_until": blocked_until,
            "expires_at": expires_at,
            "consecutive_failures": count,
        }
        records[symbol] = record
        state.log_event(
            "entry_execution_failed",
            self._audit_json({
                "symbol": symbol,
                **record,
                "diagnostics": {
                    "http_status": details.get("http_status"),
                    "result_rows": details.get("result_rows") or [],
                    "order_audit": details.get("order_audit"),
                },
                "setup_id": plan.get("setup_id"),
            }),
            setup_id=plan.get("setup_id"),
        )
        state.commit(st)
        log.warning(
            "%s entered a %.0f-minute %s entry backoff after %d failure(s)",
            symbol, delay, classification, count)
        return record

    def _execute_open(self, plan: dict, st: dict) -> bool:
        symbol = plan["symbol"]
        if state.load_state()["state"] != state.RUNNING:
            log.info("Control state changed; skipping entry for %s", symbol)
            return False
        try:
            live = self.ex.price(symbol)
        except CredentialError:
            raise
        except Exception as e:
            self._remember_entry_failure(plan, st, e, "price_check")
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
        except EntryLiquidityRejected as exc:
            self._remember_liquidity_rejection(plan, st, exc)
            log.warning("Entry liquidity guard rejected %s: %s", symbol, exc)
            return False
        except CredentialError:
            raise
        except Exception as exc:
            self._remember_entry_failure(
                plan, st, exc, "order_book_guard")
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
        except CredentialError:
            raise
        except Exception as e:
            self._remember_entry_failure(
                plan, st, e, "attached_entry")
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
        liquidation_check = self._liquidation_stop_check(
            plan["direction"],
            execution.get("mark_price") or fill_price,
            sl_price,
            execution.get("liquidation_price"),
        )
        stop_verified = bool(
            (execution.get("protection") or {}).get("stop_loss"))
        liquidation_unsafe = (
            liquidation_check["available"]
            and not liquidation_check["safe"]
        )
        if not liquidation_check["available"]:
            log.warning(
                "OKX returned no liquidation price for %s; relying on "
                "attached stop and account-level IMR/MMR guards", symbol)
        st.setdefault("opened_at", {})[symbol] = opened
        st.setdefault("active_trades", {})[symbol] = {
            "trade_id": trade_id,
            "direction": plan["direction"],
            "opened_at": opened,
            "age_known": True,
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
            "strategy_id": plan.get("strategy_id"),
            "strategy_version": plan.get("strategy_version"),
            "setup_id": plan.get("setup_id"),
            "setup_key": plan.get("setup_key"),
            "setup_type": plan.get("setup_type"),
            "signal_ts": plan.get("signal_ts"),
            "exit_policy": plan.get("exit_policy"),
            "invalidation_anchor": plan.get("invalidation_anchor"),
            "entry_reason": (
                plan.get("reason") or "model supplied no entry thesis"),
            "entry_evidence": plan.get("entry_evidence") or {},
            "stop_loss_pct": plan.get("sl_pct"),
            "take_profit_pct": plan.get("tp_pct"),
            "run_id": getattr(
                self, "run_id", state.journal_context().get("run_id")
                or "unknown-run"),
            "cycle_id": state.journal_context().get("cycle_id"),
        }
        st.setdefault("protection", {})[symbol] = {
            "side": plan["direction"],
            "contracts": float(execution.get("position_contracts") or filled),
            "sl_price": sl_price,
            "tp_price": tp_price,
        }
        st.setdefault("entry_feedback", {}).pop(symbol, None)
        st.setdefault("entry_failures", {}).pop(symbol, None)
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
                slippage_usd=execution.get("slippage_usd") or 0,
                adverse_slippage_usd=execution.get(
                    "adverse_slippage_usd") or 0,
                funding_status="not_applicable",
                strategy_id=plan.get("strategy_id"),
                strategy_version=plan.get("strategy_version"),
                setup_id=plan.get("setup_id"),
                setup_key=plan.get("setup_key"),
                setup_type=plan.get("setup_type"),
                signal_ts=plan.get("signal_ts"),
                exit_policy=plan.get("exit_policy"),
                invalidation_anchor=plan.get("invalidation_anchor"),
                entry_equity_usd=plan.get("entry_equity_usd"))
            state.log_event(
                "order_execution",
                self._audit_json({
                    "symbol": symbol,
                    "stage": "entry",
                    "trade_id": trade_id,
                    "setup_id": plan.get("setup_id"),
                    "order_id": execution.get("order_id"),
                    "client_order_id": execution.get("client_order_id"),
                    "status": execution.get("status"),
                    "requested": execution.get("requested"),
                    "filled": execution.get("filled"),
                    "average": execution.get("average"),
                    "fee_usd": execution.get("fee_usd"),
                    "implementation_shortfall_usd": execution.get(
                        "slippage_usd"),
                    "adverse_slippage_usd": execution.get(
                        "adverse_slippage_usd"),
                    "submission_audit": execution.get("submission_audit"),
                }),
                setup_id=plan.get("setup_id"),
            )
        except Exception as exc:
            persistence_error = (
                exc if isinstance(exc, state.JournalError) else
                state.JournalError(f"post-entry persistence failed: {exc}"))
            log.critical("Post-entry persistence failed for %s: %s",
                         symbol, exc)

        if not stop_verified or liquidation_unsafe:
            # Persist the verified fill before the emergency close so even a
            # process crash leaves a durable, reconcilable trade record. A
            # persistence failure must never prevent this exchange close.
            if not stop_verified:
                emergency_reason = "stop-loss verification failed"
            else:
                emergency_reason = "liquidation buffer is unsafe"
                self.alerts.send(
                    "critical", "liquidation_buffer_unsafe",
                    f"{symbol} entry filled with an unsafe liquidation "
                    "buffer; closing it",
                    {
                        "liquidation_price": execution.get(
                            "liquidation_price"),
                        "mark_price": execution.get("mark_price") or fill_price,
                        **liquidation_check,
                    },
                )
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
                        f"emergency close: {emergency_reason}", st)
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
                    f"{symbol} remains open without verified liquidation "
                    "safety",
                    {"contracts": emergency_position["contracts"],
                     "reason": emergency_reason})
                raise RuntimeError(
                    f"{symbol} emergency close failed after unsafe fill"
                ) from (close_error or persistence_error)
            if persistence_error is not None:
                raise persistence_error
            return False
        if persistence_error is not None:
            raise persistence_error
        log.info("OPENED %s %s | notional %.0f USDT | %.1fx | SL %.2f%% "
                 "all-in risk %.2f%% | equity at risk %.2f%% (sized by %s) | "
                 "TP %.2f%% | conf %.2f | %s",
                 plan["direction"].upper(), symbol, actual_notional,
                 plan["leverage"], plan["sl_pct"], estimated_loss_pct,
                 risk_usd / plan["entry_equity_usd"] * 100.0
                 if plan.get("entry_equity_usd") else float("nan"),
                 plan.get("sizing_constraint", "unknown"),
                 plan["tp_pct"],
                 plan["confidence"], plan["reason"])
        return True

    def _close(self, pos: dict, reason: str, st: dict,
               *, close_trigger: str | None = None,
               close_evidence: str | None = None) -> bool:
        symbol = pos["symbol"]
        try:
            execution = self.ex.close_position(pos)
        except Exception as e:
            log.error("Close failed for %s: %s", symbol, e)
            return False
        trade = (st.get("active_trades") or {}).get(symbol) or {}
        direction = trade.get("direction") or self._direction(pos)
        if close_trigger is None:
            close_trigger = "engine_safety"
        if close_evidence is None:
            close_evidence = reason
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
                slippage_usd=execution.get("slippage_usd") or 0,
                adverse_slippage_usd=execution.get(
                    "adverse_slippage_usd") or 0,
                funding_status="deferred",
                strategy_id=trade.get("strategy_id"),
                strategy_version=trade.get("strategy_version"),
                setup_id=trade.get("setup_id"),
                setup_key=trade.get("setup_key"),
                setup_type=trade.get("setup_type"),
                signal_ts=trade.get("signal_ts"),
                exit_policy=trade.get("exit_policy"),
                invalidation_anchor=trade.get("invalidation_anchor"),
                close_trigger=close_trigger,
                close_evidence=close_evidence)
            self._log_order_execution(
                symbol, "partial_close", execution, trade)
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
        funding_status = "available"
        if funding_raw in (None, "") and trade.get("opened_at"):
            funding_raw = self.ex.funding_since(
                symbol, int(float(trade["opened_at"]) * 1000))
        if funding_raw is None:
            funding_status = "unavailable"
            funding = 0.0
            state.log_event(
                "funding_reconciliation_incomplete",
                self._audit_json({
                    "symbol": symbol,
                    "trade_id": trade.get("trade_id"),
                    "setup_id": trade.get("setup_id"),
                }),
                setup_id=trade.get("setup_id"),
            )
            self.alerts.send(
                "warning", "funding_reconciliation_incomplete",
                f"Funding could not be recovered for {symbol}",
                {"trade_id": trade.get("trade_id")})
        else:
            funding = float(funding_raw)
        contract_size = float(
            self.ex.x.market(symbol).get("contractSize") or 1)
        move = price - entry_price
        gross_pnl = move * qty * contract_size * (
            1 if direction == "long" else -1)
        final_realized = gross_pnl - entry_fee - exit_fee + funding
        total_realized = (
            float(trade.get("partial_realized_pnl_usd") or 0)
            + final_realized
        )
        pnl_pct = (
            total_realized / entry_notional * 100
            if entry_notional else None)
        state.log_trade(
            symbol, "sell" if direction == "long" else "buy", "close", qty,
            price, entry_notional, float(pos.get("leverage") or 0), reason,
            pnl_pct=pnl_pct, trade_id=trade.get("trade_id"),
            order_id=execution.get("order_id"), fee_usd=exit_fee,
            funding_usd=funding, realized_pnl_usd=final_realized,
            risk_usd=trade.get("risk_usd"),
            fill_status=execution.get("status"),
            slippage_usd=execution.get("slippage_usd") or 0,
            adverse_slippage_usd=execution.get(
                "adverse_slippage_usd") or 0,
            funding_status=funding_status,
            strategy_id=trade.get("strategy_id"),
            strategy_version=trade.get("strategy_version"),
            setup_id=trade.get("setup_id"),
            setup_key=trade.get("setup_key"),
            setup_type=trade.get("setup_type"),
            signal_ts=trade.get("signal_ts"),
            exit_policy=trade.get("exit_policy"),
            invalidation_anchor=trade.get("invalidation_anchor"),
            close_trigger=close_trigger,
            close_evidence=close_evidence)
        self._log_order_execution(symbol, "close", execution, trade)
        if total_realized < 0:
            cooldown = float(self.cfg["risk"]["cooldown_minutes_after_loss"])
            st.setdefault("cooldowns", {})[symbol] = time.time() + cooldown * 60
        self._mark_setup_status(
            st, trade.get("setup_id"), "closed", cooldown=True,
            realized_pnl_usd=total_realized)
        st.get("opened_at", {}).pop(symbol, None)
        st.get("active_trades", {}).pop(symbol, None)
        st.get("protection", {}).pop(symbol, None)
        state.commit(st)
        log.info("CLOSED %s (%s, %+.2f USDT realized): %s",
                 symbol, direction, total_realized, reason)
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

        def read_account_risk() -> dict:
            try:
                return self.ex.account_risk_metrics()
            except CredentialError:
                raise
            except Exception as exc:
                self.alerts.send(
                    "critical", "margin_risk_unavailable",
                    "New entries blocked because OKX account risk is "
                    "unavailable",
                    {"error": str(exc)})
                raise RuntimeError(
                    f"cannot enforce IMR/MMR guards: {exc}") from exc

        def breaches(metrics: dict) -> list[str]:
            reasons = []
            usage = float(metrics["initial_margin_usage_pct"])
            if usage > float(r["max_margin_usage_pct"]):
                reasons.append(
                    f"initial-margin usage {usage:.1f}% exceeds "
                    f"{float(r['max_margin_usage_pct']):.1f}%")
            ratio = metrics.get("maintenance_margin_ratio")
            if (ratio is not None
                    and float(ratio)
                    < float(r["min_maintenance_margin_ratio"])):
                reasons.append(
                    f"maintenance-margin ratio {float(ratio):.2f} is below "
                    f"{float(r['min_maintenance_margin_ratio']):.2f}")
            return reasons

        metrics = read_account_risk()
        reasons = breaches(metrics)
        while reasons and kept:
            kept.sort(
                key=lambda p: (float("inf")
                               if p.get("_risk_notional_invalid")
                               else self._notional(p)),
                reverse=True)
            biggest = kept.pop(0)
            log.warning(
                "Account risk guard (%s); closing largest position %s",
                "; ".join(reasons), biggest["symbol"])
            if not self._close(biggest, "account IMR/MMR guard", st):
                kept.insert(0, biggest)
                break
            time.sleep(2)  # let the close settle before re-reading margin
            metrics = read_account_risk()
            reasons = breaches(metrics)
        if reasons:
            detail = {
                "reasons": reasons,
                "metrics": metrics,
                "open_positions": [p.get("symbol") for p in kept],
            }
            self.alerts.send(
                "critical", "margin_risk_unsafe",
                "New entries blocked because account margin risk remains "
                "outside configured limits",
                detail)
            raise RuntimeError(
                "account margin risk remains unsafe: " + "; ".join(reasons))
        return kept

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _audit_json(payload: object) -> str:
        return json.dumps(
            payload, separators=(",", ":"), allow_nan=False)

    def _journal_llm_input(self, snapshot: dict, portfolio: dict,
                           max_new: int) -> None:
        state.log_event(
            "llm_input",
            self._audit_json(
                self.llm.audit_request(snapshot, portfolio, max_new)),
        )

    def _journal_llm_output(self) -> None:
        payload = self.llm.call_audit()
        if payload is not None:
            state.log_event("llm_output", self._audit_json(payload))

    def _log_order_execution(
            self, symbol: str, stage: str,
            execution: dict, trade: dict) -> None:
        state.log_event(
            "order_execution",
            self._audit_json({
                "symbol": symbol,
                "stage": stage,
                "trade_id": trade.get("trade_id"),
                "setup_id": trade.get("setup_id"),
                "order_id": execution.get("order_id"),
                "client_order_id": execution.get("client_order_id"),
                "status": execution.get("status"),
                "requested": execution.get("requested"),
                "filled": execution.get("filled"),
                "average": execution.get("average"),
                "fee_usd": execution.get("fee_usd"),
                "implementation_shortfall_usd": execution.get(
                    "slippage_usd"),
                "adverse_slippage_usd": execution.get(
                    "adverse_slippage_usd"),
                "submission_audit": execution.get("submission_audit"),
            }),
            setup_id=trade.get("setup_id"),
        )

    def _prepare_setup_decision(
            self, decision: dict, snapshot: dict,
            st: dict) -> tuple[dict | None, str | None]:
        symbol = decision.get("symbol")
        symbol_snapshot = snapshot.get(symbol)
        if not isinstance(symbol_snapshot, dict):
            return None, "symbol not in current snapshot"
        records = st.setdefault("recent_setups", {})
        probe = strategy.signal_probe(
            decision, symbol_snapshot, self.cfg)
        if (probe is not None
                and strategy.evaluated_signal(records, probe) is not None):
            return None, (
                "symbol already evaluated for this completed signal candle")
        prepared, why = strategy.build_setup_plan(
            decision, symbol_snapshot, self.cfg)
        if prepared is None:
            if probe is not None:
                record = strategy.new_setup_record(
                    probe, self.cfg)
                strategy.mark_setup(
                    record, "risk_rejected", self.cfg)
                records[probe["setup_id"]] = record
                state.log_event(
                    "setup_status",
                    self._audit_json({
                        "setup_id": probe["setup_id"],
                        "status": "risk_rejected",
                        "symbol": symbol,
                        "signal_ts": probe["signal_ts"],
                        "why": why,
                    }),
                    setup_id=probe["setup_id"],
                )
                state.commit(st)
            return None, why
        prepared["entry_evidence"] = strategy.compact_entry_evidence(
            symbol_snapshot, snapshot.get("_market_context"))
        setup_id = prepared["setup_id"]
        blocked = strategy.semantic_block(
            records, prepared["setup_key"])
        if blocked is not None:
            remaining = max(
                0.0, float(blocked["blocked_until"]) - time.time()) / 60
            return None, (
                "semantically identical setup is cooling down for "
                f"{remaining:.1f} more minute(s)")
        failed_reentry = strategy.failed_thesis_reentry_reason(
            records, prepared, self.cfg)
        if failed_reentry is not None:
            return None, failed_reentry
        records[setup_id] = strategy.new_setup_record(
            prepared, self.cfg)
        state.log_event(
            "setup_proposed",
            self._audit_json({
                "setup_id": setup_id,
                "setup_key": prepared["setup_key"],
                "symbol": symbol,
                "direction": prepared["direction"],
                "setup_type": prepared["setup_type"],
                "signal_ts": prepared["signal_ts"],
                "invalidation_anchor": prepared["invalidation_anchor"],
                "exit_policy": prepared["exit_policy"],
                "execution_choice": prepared["execution_choice"],
                "entry_evidence_fingerprint": prepared.get(
                    "entry_evidence_fingerprint"),
                "what_changed_since_last_loss": prepared.get(
                    "what_changed_since_last_loss") or None,
            }),
            setup_id=setup_id,
        )
        state.commit(st)
        return prepared, None

    def _mark_setup_status(
            self, st: dict, setup_id: str | None, status: str,
            *, cooldown: bool = False,
            realized_pnl_usd: float | None = None) -> None:
        if not setup_id:
            return
        record = (st.setdefault("recent_setups", {}).get(setup_id))
        if not isinstance(record, dict):
            return
        strategy.mark_setup(
            record, status, self.cfg, apply_cooldown=cooldown,
            realized_pnl_usd=realized_pnl_usd)
        state.log_event(
            "setup_status",
            self._audit_json({
                "setup_id": setup_id,
                "status": status,
                "blocked_until": record.get("blocked_until"),
                "outcome": record.get("outcome"),
                "realized_pnl_usd": record.get("realized_pnl_usd"),
            }),
            setup_id=setup_id,
        )
        state.commit(st)

    @staticmethod
    def _sorted_opens(decisions: list[dict]) -> tuple[list[dict], list[dict]]:
        """Opens by descending confidence, minus same-reply close conflicts.

        SYSTEM forbids proposing an open and a close on one symbol in the
        same answer. Enforce that deterministically: a violating reply must
        not close a position and instantly re-enter (or reverse) it within
        the same cycle. Returns (executable opens, conflicted opens).
        """
        closing = {d["symbol"] for d in decisions if d["action"] == "close"}
        conflicted = [d for d in decisions
                      if d["action"] == "open" and d["symbol"] in closing]
        opens = sorted(
            [d for d in decisions
             if d["action"] == "open" and d["symbol"] not in closing],
            key=lambda d: d.get("confidence", 0), reverse=True)
        return opens, conflicted

    def _notional(self, pos: dict) -> float:
        n = pos.get("notional")
        if n not in (None, ""):
            try:
                value = abs(float(n))
                if math.isfinite(value) and value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        try:
            m = self.ex.x.market(pos["symbol"])
            value = (abs(float(pos.get("contracts") or 0))
                     * float(m.get("contractSize") or 1)
                     * float(pos.get("markPrice") or 0))
            return value if math.isfinite(value) and value > 0 else 0.0
        except Exception:
            return 0.0

    def _portfolio_view(self, equity: float, positions: list[dict], st: dict,
                        day_pnl_pct: float, drawdown_pct: float) -> dict:
        now = time.time()
        views = []
        for p in positions:
            opened = st.get("opened_at", {}).get(p["symbol"])
            trade = (st.get("active_trades") or {}).get(p["symbol"]) or {}
            views.append({
                "symbol": p["symbol"],
                "side": p.get("side"),
                "entry": p.get("entryPrice"),
                "mark": p.get("markPrice"),
                "upnl_pct": round(float(p.get("percentage") or 0), 2),
                "leverage": p.get("leverage"),
                "notional_usd": round(self._notional(p), 1),
                "hours_open": round((now - opened) / 3600, 1)
                if opened else None,
                "age_verified": bool(
                    trade.get("age_known", bool(opened))),
                "planned_risk_usd": trade.get("risk_usd"),
                "original_thesis": {
                    "reason": trade.get("entry_reason"),
                    "setup_type": trade.get("setup_type"),
                    "invalidation_anchor": trade.get(
                        "invalidation_anchor"),
                    "exit_policy": trade.get("exit_policy"),
                    "stop_loss_pct": trade.get("stop_loss_pct"),
                    "take_profit_pct": trade.get("take_profit_pct"),
                    "signal_ts": trade.get("signal_ts"),
                    "entry_evidence": trade.get("entry_evidence"),
                },
            })
        post_loss_cooldowns = [
            {
                "symbol": symbol,
                "minutes_remaining": round((float(until) - now) / 60, 1),
            }
            for symbol, until in sorted((st.get("cooldowns") or {}).items())
            if float(until) > now
        ]
        entry_feedback = []
        for symbol, feedback in sorted(
                (st.get("entry_feedback") or {}).items()):
            if float(feedback.get("expires_at") or 0) <= now:
                continue
            blocked_seconds = max(
                0.0, float(feedback.get("blocked_until") or 0) - now)
            max_retry = float(
                feedback.get("max_retry_size_pct_equity") or 0)
            entry_feedback.append({
                "symbol": symbol,
                "direction": feedback.get("direction"),
                "reason": feedback.get("reason"),
                "minutes_since_rejection": round(
                    max(0.0, now - float(
                        feedback.get("last_rejected_at") or now)) / 60, 1),
                "consecutive_rejections": int(
                    feedback.get("consecutive_rejections") or 0),
                "available_pct_of_requested": round(
                    float(feedback.get("available_ratio") or 0) * 100, 1),
                "max_retry_size_pct_equity": round(max_retry, 2),
                "retry_allowed": blocked_seconds <= 0 and max_retry > 0,
                "retry_after_minutes": round(blocked_seconds / 60, 1),
            })
        entry_failures = []
        for symbol, failure in sorted(
                (st.get("entry_failures") or {}).items()):
            if float(failure.get("expires_at") or 0) <= now:
                continue
            blocked_seconds = max(
                0.0, float(failure.get("blocked_until") or 0) - now)
            entry_failures.append({
                "symbol": symbol,
                "direction": failure.get("direction"),
                "stage": failure.get("stage"),
                "classification": failure.get("classification"),
                "error_code": failure.get("error_code"),
                "error_message": failure.get("error_message"),
                "consecutive_failures": int(
                    failure.get("consecutive_failures") or 0),
                "retry_after_minutes": round(blocked_seconds / 60, 1),
            })
        r = self.cfg["risk"]
        return {
            "equity_usdt": round(equity, 2),
            "day_pnl_pct": round(day_pnl_pct, 2),
            "drawdown_from_high_pct": round(drawdown_pct, 2),
            "state": st["state"],
            "open_positions": views,
            "post_loss_cooldowns": post_loss_cooldowns,
            "recent_entry_feedback": entry_feedback,
            "recent_entry_failures": entry_failures,
            "recent_setup_memory": strategy.recent_setup_view(
                st.get("recent_setups") or {}, now),
            "strategy": {
                "id": getattr(
                    self, "strategy_id", strategy.identity(self.cfg)[0]),
                "version": getattr(
                    self, "strategy_version", strategy.identity(self.cfg)[1]),
            },
            "hard_limits_fyi": {
                "max_leverage": r["max_leverage"],
                "entry_leverage": r["entry_leverage"],
                "risk_per_trade_pct": r["risk_per_trade_pct"],
                "experimental_risk_per_trade_pct": r[
                    "experimental_risk_per_trade_pct"],
                "max_total_open_risk_pct": r[
                    "max_total_open_risk_pct"],
                "max_concurrent_positions": r["max_concurrent_positions"],
                "min_confidence": r["min_confidence"],
                "max_net_direction_pct": r.get("max_net_direction_pct", 100),
                "max_btc_beta_exposure_pct": r[
                    "max_btc_beta_exposure_pct"],
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
