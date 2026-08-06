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
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone

import ccxt

from .forward_models import require_complete_contract
from . import (brain, contracts, deployment, hypotheses, market, registry,
               shadow, state, strategy, variants)
from .alerts import AlertManager
from .exchange import (CredentialError, EntryLiquidityRejected,
                       EntryOrderRejected, Exchange,
                       MakerFirstAmbiguousError,
                       MakerFirstPreSubmitError,
                       OrderSubmissionAmbiguousError)
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
    def __init__(self, cfg: dict, light: bool = False,
                 candidate_demo: Mapping[str, object] | None = None,
                 *, demo_variant_id: str | None = None,
                 demo_scope_key: str | None = None,
                 demo_packet_ref: str | None = None,
                 expected_demo_account_fingerprint: str | None = None):
        self.cfg = cfg
        # Protect library/direct callers as well as main.py: state and journal
        # access must already be scoped to, and bound to, this exact key/mode.
        state.configure_runtime(cfg["mode"])
        state.bind_runtime_identity(
            cfg["mode"], os.environ.get("OKX_API_KEY", ""))
        requested_demo = dict(candidate_demo or {})
        if demo_variant_id is not None:
            requested_demo["variant_id"] = demo_variant_id
        if demo_scope_key is not None:
            requested_demo["scope_key"] = demo_scope_key
        if demo_packet_ref is not None:
            requested_demo["packet_ref"] = demo_packet_ref
        if expected_demo_account_fingerprint is not None:
            requested_demo["expected_account_fingerprint"] = (
                expected_demo_account_fingerprint)
        self.demo_authorization = None
        if requested_demo:
            if cfg.get("mode") != "demo":
                raise deployment.DeploymentAuthorizationError(
                    "candidate demo startup cannot authorize live mode")
            if light:
                raise deployment.DeploymentAuthorizationError(
                    "candidate demo startup requires the full engine")
            self.demo_authorization = deployment.verify_demo_artifact(
                cfg,
                variant_id=requested_demo.get("variant_id"),
                scope_key=requested_demo.get("scope_key"),
                packet_ref=requested_demo.get("packet_ref"),
                expected_account_fingerprint=requested_demo.get(
                    "expected_account_fingerprint"),
                runtime_account_fingerprint=(
                    state.journal_context().get("account_fingerprint")),
            )
            # Applying a reviewed variant in memory is intentional.  The
            # registry and on-disk config remain untouched, while every
            # client below receives the exact reviewed candidate config.
            self.cfg = deepcopy(self.demo_authorization["runtime_config"])
            cfg = self.cfg
        self.run_id = state.new_run_id()
        self.config_version = state.stable_fingerprint(cfg)
        # Fingerprint only configuration that can change a decision.
        self.strategy_config_version = state.strategy_fingerprint(cfg)
        self.code_version = state.code_fingerprint()
        self.strategy_id, self.strategy_version = strategy.identity(cfg)
        self.deployment_artifact = None
        self.research_selection_catalog = None
        self.system_prompt = None
        if cfg["mode"] == "live" and not light:
            # Capture registry state once. The verifier, prompt version, LLM,
            # and parser must all use this exact catalog snapshot.
            self.research_selection_catalog = (
                variants.research_selection_catalog())
            self.system_prompt = brain.build_system(
                cfg, catalog=self.research_selection_catalog)
            self.deployment_artifact = deployment.verify_live_artifact(
                cfg, catalog=self.research_selection_catalog,
                system_prompt=self.system_prompt)
            self.prompt_version = brain.prompt_version(self.system_prompt)
        elif self.demo_authorization:
            self.research_selection_catalog = self.demo_authorization["catalog"]
            self.system_prompt = self.demo_authorization["system_prompt"]
            self.prompt_version = brain.prompt_version(self.system_prompt)
        else:
            # Demo and light control paths retain their existing startup
            # semantics; only non-light live startup is artifact-gated.
            self.prompt_version = brain.prompt_version(brain.build_system(cfg))
        artifact_context = {}
        if self.deployment_artifact:
            artifact_context = {
                "packet_id": self.deployment_artifact["packet_id"],
                "packet_hash": self.deployment_artifact["payload_hash"],
                "artifact_hash": self.deployment_artifact["artifact_hash"],
                "artifact_variant_id": self.deployment_artifact["variant_id"],
                "artifact_variant_definition_hash": (
                    self.deployment_artifact["variant_definition_hash"]),
                "artifact_strategy_config_version": (
                    self.deployment_artifact[
                        "artifact_strategy_config_version"]),
                "deployment_config_hash": (
                    self.deployment_artifact["deployment_config_hash"]),
            }
        journal_variant_id = (
            self.demo_authorization["variant_id"]
            if self.demo_authorization else variants.LIVE_VARIANT_ID)
        if self.demo_authorization:
            artifact_context.update({
                "packet_id": self.demo_authorization.get("packet_id"),
                "packet_hash": self.demo_authorization.get("packet_hash"),
                "artifact_hash": self.demo_authorization.get("artifact_hash"),
                "artifact_variant_id": self.demo_authorization.get(
                    "variant_id"),
                "artifact_variant_definition_hash": (
                    self.demo_authorization.get("variant_definition_hash")),
                "artifact_strategy_config_version": (
                    self.demo_authorization.get(
                        "artifact_strategy_config_version")),
                "deployment_config_hash": self.demo_authorization.get(
                    "deployment_config_hash"),
            })
        state.set_journal_context(
            run_id=self.run_id,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            prompt_version=self.prompt_version,
            config_version=self.config_version,
            code_version=self.code_version,
            strategy_config_version=self.strategy_config_version,
            # Everything the live agent writes is attributed to "live".
            # Replayed and shadow variants carry their own readable ids, so
            # the two populations can never be pooled by accident.
            variant_id=journal_variant_id,
            **artifact_context,
        )
        if cfg["mode"] == "live" and not light:
            # A missing or stale findings DB must stop before constructing any
            # exchange, alert, or model client.
            state.check_journal()
        self.alerts = AlertManager(cfg)
        if not light:
            if cfg["mode"] != "live":
                state.check_journal()
            if cfg["mode"] == "live":
                self.alerts.require_live_ready(probe=True)
        self.ex = Exchange(
            cfg, self.alerts, validate_account=not light)
        if not light:
            self.ex.verify_account_safety(require_trade=True, refresh=True)
        if self.demo_authorization:
            self.demo_preflight = deployment.preflight_demo_account(
                self.ex,
                expected_account_fingerprint=self.demo_authorization[
                    "expected_account_fingerprint"],
                runtime_account_fingerprint=(
                    state.journal_context().get("account_fingerprint")),
                runtime_state_snapshot=state.load_state(),
            )
            self.demo_receipt = deployment.record_demo_authorization_receipt(
                self.demo_authorization,
                account_fingerprint=self.demo_preflight[
                    "account_fingerprint"],
                preflight=self.demo_preflight,
            )
        if not light:
            llm_kwargs = {}
            if self.research_selection_catalog is not None:
                llm_kwargs = {
                    "catalog": self.research_selection_catalog,
                    "system": self.system_prompt,
                }
            self.llm = brain.LLM(cfg, **llm_kwargs)
            self.llm.preflight()
            self.risk = RiskEngine(cfg)
        # Disabled shadow evaluation remains a complete no-op.
        self._research_failure_count = 0
        self._research_consecutive_failures = 0
        self._research_last_failure: dict | None = None
        self._research_last_success_ts: float | None = None
        self.shadow = self._build_shadow(cfg)
        self.universe: list[str] = []
        self.universe_ts = 0.0
        self._startup_reconciled = False
        self._credential_failures = 0
        self._shutdown_reason: str | None = None

    def _record_research_proposals(
            self, decisions: list[dict]) -> list[dict]:
        trading_decisions = [
            decision for decision in decisions
            if decision.get("action") not in {
                "research_proposal", "research_selection"}]
        proposal = next((d for d in decisions
                         if d.get("action") == "research_proposal"), None)
        selection = next((d for d in decisions
                          if d.get("action") == "research_selection"), None)
        accepted = list(trading_decisions)
        if proposal is not None and self.shadow is not None:
            store = self.shadow.store
            try:
                metadata = hypotheses.numeric_setting_metadata(
                    proposal["hypothesis_id"], proposal["setting_id"])
                if metadata is None:
                    raise ValueError("adaptive proposal is not registered")
                adaptive = variants.adaptive_hypothesis_variant(
                    self.strategy_id, self.strategy_version,
                    proposal["hypothesis_id"], proposal["setting_id"],
                    float(proposal["value"]))
                stored = store.propose_numeric_setting(
                    self.strategy_id, proposal["hypothesis_id"],
                    proposal["setting_id"], proposal["value"], self.run_id,
                    minimum=metadata["minimum"], maximum=metadata["maximum"],
                    target_parameter=metadata["target_parameter"],
                    variant=adaptive, reasoning=proposal["reasoning"],
                    observation_lock_seconds=metadata["observation_seconds"])
                accepted.append({
                    "action": "research_proposal", **proposal, **stored,
                })
            except Exception as exc:                       # noqa: BLE001
                log.warning("research proposal rejected: %s", exc)
        if selection is not None:
            context = state.journal_context()
            llm_cfg = (getattr(self, "cfg", {}).get("llm") or {})
            attribution = {
                "run_id": getattr(self, "run_id", None)
                or context.get("run_id") or "unknown-run",
                "cycle_id": context.get("cycle_id"),
                "model_id": llm_cfg.get("model") or "unknown-model",
                "prompt_version": getattr(self, "prompt_version", None)
                or context.get("prompt_version") or "unknown-prompt",
            }
            if self.shadow is None:
                try:
                    from research.findings import (FindingsStore,
                                                   resolve_store_path)
                    research_cfg = getattr(self, "cfg", {}).get("research") or {}
                    scope_key = (
                        f"{context.get('runtime_mode') or self.cfg.get('mode', 'unknown')}:"
                        f"{context.get('account_fingerprint') or 'unscoped'}:"
                        "selector-unavailable")
                    persisted = FindingsStore(resolve_store_path(
                        research_cfg.get("findings_store"))).record_research_selection(
                            selection, [], scope_key=scope_key,
                            run_id=attribution["run_id"],
                            cycle_id=attribution["cycle_id"],
                            model_id=attribution["model_id"],
                            prompt_version=attribution["prompt_version"],
                            validation_error=(
                                selection.get("rejection_reason")
                                or "research coordinator is unavailable"))
                    log.warning(
                        "research selection %s: research coordinator is "
                        "unavailable", persisted["current_status"])
                except Exception as exc:                   # noqa: BLE001
                    log.warning(
                        "research selection could not be persisted while the "
                        "coordinator was unavailable: %s", exc)
            else:
                try:
                    persisted = self.shadow.record_research_selection(
                        selection, attribution)
                    log.info(
                        "research selection %s: strategy=%s requested=%s "
                        "resolved=%s",
                        persisted["current_status"],
                        persisted["requested_strategy_id"],
                        persisted.get("requested_variant_id"),
                        persisted.get("resolved_variant_id"))
                except Exception as exc:                   # noqa: BLE001
                    log.warning("research selection persistence failed: %s", exc)
        return accepted

    @staticmethod
    def _build_shadow(cfg: dict):
        """Never fatal. A bad research block disables shadow, not trading."""
        try:
            from pathlib import Path
            registry_path = (Path(__file__).resolve().parent.parent
                             / "research" / "variants.yaml")
            context = state.journal_context()
            scope_key = (
                f"{context.get('runtime_mode') or cfg.get('mode', 'unknown')}:"
                f"{context.get('account_fingerprint') or 'unscoped'}")
            return shadow.build(
                cfg, variants.load_registry(registry_path),
                scope_key=scope_key)
        except Exception as exc:                           # noqa: BLE001
            log.warning("Shadow evaluation disabled: %s", exc)
            return None

    def request_shutdown(self, reason: str = "process shutdown") -> None:
        """Ask the loop to pause safely after its current bounded operation."""
        self._shutdown_reason = str(reason)[:80] or "process shutdown"

    def _heartbeat(self, status: str, **detail) -> None:
        """Publish health without ever turning observability into an order path."""
        research_cfg = self.cfg.get("research") or {}
        expected = bool(research_cfg.get("shadow_enabled"))
        available = getattr(self, "shadow", None) is not None
        failures = int(getattr(self, "_research_consecutive_failures", 0))
        try:
            state.write_heartbeat(
                status,
                run_id=getattr(self, "run_id", None),
                strategy_id=getattr(self, "strategy_id", None),
                strategy_version=getattr(self, "strategy_version", None),
                research_expected=expected,
                research_available=available,
                research_status=(
                    "disabled" if not expected else
                    "unavailable" if not available else
                    "degraded" if failures else "healthy"),
                research_failure_count=int(getattr(
                    self, "_research_failure_count", 0)),
                research_consecutive_failures=failures,
                research_last_failure=getattr(
                    self, "_research_last_failure", None),
                research_last_success_ts=getattr(
                    self, "_research_last_success_ts", None),
                **detail,
            )
        except Exception as exc:                           # noqa: BLE001
            # A stale/missing heartbeat is itself an unhealthy signal. Do not
            # let an auxiliary JSON file interrupt position protection.
            log.error("Could not publish process heartbeat: %s", type(exc).__name__)

    def _pause_for_shutdown(self) -> None:
        reason = self._shutdown_reason or "process shutdown"
        current = state.load_state()
        if current["state"] == state.KILLED:
            self._heartbeat("killed", stop_reason=reason)
            return
        log.warning(
            "%s requested. State set to PAUSED; open positions retain their "
            "exchange-side protection.", reason)
        self._heartbeat("pausing", stop_reason=reason)
        state.set_state(state.PAUSED, operator_pause=True)
        self._heartbeat("paused", stop_reason=reason)

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
            # Derived from the assembled per-strategy prompt, so the journal
            # records which prompt actually ran rather than a global constant
            # that no longer distinguishes strategies.
            self.prompt_version = brain.prompt_version(
                brain.build_system(self.cfg))
        if not hasattr(self, "config_version"):
            self.config_version = state.stable_fingerprint(self.cfg)
        if not hasattr(self, "strategy_config_version"):
            self.strategy_config_version = state.strategy_fingerprint(self.cfg)
        if not hasattr(self, "code_version"):
            self.code_version = state.code_fingerprint()
        run_context = {
            "variant_id": (
                self.demo_authorization["variant_id"]
                if getattr(self, "demo_authorization", None)
                else variants.LIVE_VARIANT_ID),
        }
        if getattr(self, "demo_authorization", None):
            run_context.update({
                "packet_id": self.demo_authorization.get("packet_id"),
                "packet_hash": self.demo_authorization.get("packet_hash"),
                "artifact_hash": self.demo_authorization.get("artifact_hash"),
                "artifact_variant_id": self.demo_authorization.get(
                    "variant_id"),
                "artifact_variant_definition_hash": (
                    self.demo_authorization.get("variant_definition_hash")),
                "artifact_strategy_config_version": (
                    self.demo_authorization.get(
                        "artifact_strategy_config_version")),
                "deployment_config_hash": self.demo_authorization.get(
                    "deployment_config_hash"),
            })
        state.set_journal_context(
            run_id=self.run_id,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            prompt_version=self.prompt_version,
            config_version=self.config_version,
            code_version=self.code_version,
            strategy_config_version=self.strategy_config_version,
            **run_context,
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
            packet_id=(getattr(self, "deployment_artifact", None) or {}
                       ).get("packet_id"),
            packet_hash=(getattr(self, "deployment_artifact", None) or {}
                         ).get("payload_hash"),
            artifact_hash=(getattr(self, "deployment_artifact", None) or {}
                           ).get("artifact_hash"),
            artifact_variant_id=(getattr(self, "deployment_artifact", None)
                                 or {}).get("variant_id"),
            artifact_variant_definition_hash=(
                (getattr(self, "deployment_artifact", None) or {}).get(
                    "variant_definition_hash")),
            artifact_strategy_config_version=(
                (getattr(self, "deployment_artifact", None) or {}).get(
                    "artifact_strategy_config_version")),
            deployment_config_hash=(
                (getattr(self, "deployment_artifact", None) or {}).get(
                    "deployment_config_hash")),
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
        initial_health = (
            "degraded" if (self.cfg.get("research") or {}).get(
                "shadow_enabled") and (
                    getattr(self, "shadow", None) is None
                    or self._research_consecutive_failures)
            else "starting")
        self._heartbeat(initial_health, trading_state=st["state"])
        final_health = "stopped"
        try:
            while True:
                if getattr(self, "_shutdown_reason", None):
                    self._pause_for_shutdown()
                    final_health = "stopped"
                    break
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
                    final_health = "killed"
                    break
                try:
                    self.cycle(st)
                    self._credential_failures = 0
                    health = (
                        "degraded" if (self.cfg.get("research") or {}).get(
                            "shadow_enabled") and (
                                getattr(self, "shadow", None) is None
                                or self._research_consecutive_failures)
                        else "running")
                    self._heartbeat(
                        health, trading_state=state.load_state()["state"],
                        last_cycle_ts=time.time())
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
                    self._heartbeat(
                        "degraded", trading_state=st["state"],
                        last_cycle_error=type(e).__name__)
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
            final_health = "stopped"
        except KeyboardInterrupt:
            if state.load_state()["state"] == state.KILLED:
                log.warning("Interrupted during a kill; state stays KILLED.")
            else:
                log.warning("Interrupted. State set to PAUSED. Open positions "
                            "keep their exchange-side stop-loss/take-profit "
                            "orders on OKX.")
                state.set_state(state.PAUSED, operator_pause=True)
            final_health = "stopped"
        finally:
            self._heartbeat(final_health)
            if owns_lock:
                state.release_run_lock(run_lock)

    def _decision_due(self, now: float) -> bool:
        """True when the decision cadence has elapsed. Records the attempt.

        Absent configuration means every cycle is a decision cycle, which is
        the pre-B9.2 behaviour, so this cannot change an existing deployment
        until it is deliberately configured.
        """
        interval = self.cfg["cycle"].get("decision_interval_seconds")
        if not interval:
            # Unconfigured means no gate at all, rather than a gate set to
            # the housekeeping cadence. Those are not the same thing: the run
            # loop already sleeps interval_seconds between cycles, so a gate
            # at that value would do nothing in the ordinary case and would
            # silently drop a decision whenever a cycle finished marginally
            # early. A feature that is off must be off.
            return True

        # A tolerance below the configured interval, for the same reason:
        # scheduling jitter must not be able to defer a decision by a whole
        # extra period.
        last = getattr(self, "_last_decision_ts", None)
        if last is not None and (now - last) < float(interval) * 0.95:
            state.log_event("decision_skipped", self._audit_json({
                "reason": "decision cadence not elapsed",
                "seconds_since_last": round(now - last, 1),
                "decision_interval_seconds": float(interval),
            }))
            return False
        self._last_decision_ts = now
        return True

    def _wait_for_next_cycle(self) -> None:
        """Sleep responsively so a kill is observed within about one second."""
        deadline = time.monotonic() + int(
            self.cfg["cycle"]["interval_seconds"])
        while time.monotonic() < deadline:
            if (getattr(self, "_shutdown_reason", None)
                    or state.load_state()["state"] == state.KILLED):
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

    def _sync_transfers(self, st: dict, now: float) -> None:
        """Apply each external cash flow once, with honest identity status."""
        since = int(st.get("last_ledger_ts") or (now - 3600) * 1000)
        processed = dict(st.get("processed_transfer_ids") or {})
        pending = dict(st.get("transfer_reconciliation_required") or {})
        batch = self.ex.transfers_since(since, set(processed))
        net_transfer, next_since = batch
        records = getattr(batch, "records", None)

        if records is None:
            # Compatibility for exchange fakes and journals produced before
            # ledger identity was exposed. The amount is applied, but the
            # aggregate is explicitly not represented as exactly deduplicated.
            if abs(float(net_transfer)) > 0.01:
                key = f"legacy-aggregate:{since}:{next_since}"
                payload = {
                    "net_usdt": float(net_transfer),
                    "identity_status": "legacy_aggregate",
                    "reconciliation_required": True,
                    "reconciliation_key": key,
                }
                pending[key] = payload
                state.log_event("transfer", json.dumps(payload))
        else:
            for record in records:
                payload = record.as_event()
                transfer_id = payload.get("transfer_id")
                if transfer_id:
                    processed[str(transfer_id)] = float(
                        payload.get("ledger_ts_ms") or 0)
                else:
                    pending[str(payload["reconciliation_key"])] = payload
                    state.log_event(
                        "warning", json.dumps({
                            "kind": "transfer_reconciliation_required",
                            **payload,
                        }))
                state.log_event("transfer", json.dumps(payload))

        # Bound operational state without weakening database history.
        st["processed_transfer_ids"] = dict(sorted(
            processed.items(), key=lambda item: item[1], reverse=True)[:2000])
        st["transfer_reconciliation_required"] = dict(
            list(pending.items())[-200:])
        st["last_ledger_ts"] = int(next_since)

        if abs(float(net_transfer)) <= 0.01:
            return
        log.info("Net transfer detected: %+.2f USDT; rebasing benchmarks",
                 net_transfer)
        if st.get("high_water_mark"):
            st["high_water_mark"] = max(
                1e-9, float(st["high_water_mark"]) + float(net_transfer))
        if st.get("day_start_equity"):
            st["day_start_equity"] = max(
                1e-9, float(st["day_start_equity"]) + float(net_transfer))

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
        except (CredentialError, ccxt.AuthenticationError,
                state.JournalError, OrderSubmissionAmbiguousError):
            raise
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

        # Transfers rebase benchmarks and never affect trading decisions.
        self._sync_transfers(st, now)

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

        refresh_s = float(self.cfg["universe"]["refresh_minutes"]) * 60
        if self.universe_ts <= 0 or now - self.universe_ts > refresh_s:
            self.universe, universe_audit = market.select_universe(
                self.ex, self.cfg)
            self.universe_ts = now
            state.log_event(
                "universe_selection", self._audit_json(universe_audit))
            log.info("Universe (%d): %s", len(self.universe),
                     ", ".join(self.universe))

        # Position protection runs in every state except KILLED.
        positions = self._manage_positions(positions, st, equity)

        can_decide = st["state"] in (state.RUNNING, state.DAY_STOPPED)
        decision_due = bool(
            can_decide
            and not (st["state"] == state.DAY_STOPPED and not positions)
            and self._decision_due(now))

        # Local SHADOW/PAPER accounts are independent of the exchange account.
        # Refresh their held symbols even after the live universe reranks, and
        # advance them before every pause/day-stop/cadence/LLM early return.
        evaluator = getattr(self, "shadow", None)
        if evaluator is not None:
            # The nightly authoring process may add or retire staged research
            # mechanisms while this long-running trader stays up. Refresh
            # only the research coordinator at the cycle boundary; the
            # method reuses existing paper accounts and never touches the
            # registered strategy evaluators or live order state.
            refresh = getattr(evaluator, "refresh_staged_lanes", None)
            if callable(refresh):
                try:
                    refresh_report = refresh(now=now)
                    if refresh_report.get("status") in {
                            "changed", "partial", "error"}:
                        state.log_event(
                            "shadow_staged_refresh",
                            self._audit_json(refresh_report))
                except Exception as exc:                  # noqa: BLE001
                    # Research refresh is auxiliary. Keep trading and make
                    # the failure visible in the durable audit trail.
                    log.warning("Staged shadow refresh failed: %s", exc)
                    try:
                        state.log_event(
                            "shadow_staged_refresh",
                            self._audit_json({
                                "status": "error",
                                "error": f"{type(exc).__name__}: {exc}",
                            }))
                    except Exception:                       # noqa: BLE001
                        pass
        shadow_symbols = []
        if evaluator is not None:
            try:
                shadow_symbols = evaluator.held_symbols()
            except Exception as exc:                       # noqa: BLE001
                log.warning("Could not inspect shadow held symbols: %s", exc)
        if not decision_due and evaluator is None:
            return

        # Two symbol sets, deliberately. The live path may only see what live
        # universe selection chose plus what the exchange account actually
        # holds. Symbols present solely because a local variant account holds
        # them are fetched so that account can mark its own positions, and are
        # withheld from everything else: they would otherwise reach the model
        # as candidates and inflate the breadth count that vetoes opens, which
        # would let a simulated position change a real one.
        live_symbols = list(dict.fromkeys(
            self.universe + [p["symbol"] for p in positions]))
        symbols = list(dict.fromkeys(live_symbols + shadow_symbols))
        snapshot = market.market_snapshot(self.ex, symbols, self.cfg)
        if snapshot and evaluator is not None:
            self._advance_shadow_variants(snapshot, time.time())
        # Everything below this line runs on the restricted view, so the live
        # decision is byte-identical to the one taken with shadow disabled.
        live_snapshot = market.restrict_snapshot(snapshot, live_symbols)

        if not can_decide:
            return  # PAUSED: shadow marks advance, but there is no LLM call
        if st["state"] == state.DAY_STOPPED and not positions:
            return  # opens blocked and nothing held: an LLM call cannot act

        # Decision cadence never delays reconciliation or safety housekeeping.
        if not decision_due:
            return

        if not live_snapshot:
            log.warning("Empty market snapshot; holding")
            return

        # One observed book read is shared by every deterministic research
        # strategy and withheld from the active LLM prompt.  Missing book
        # data remains explicit nulls, allowing scalp-maker to persist a
        # data-missing veto without changing the demo strategy's input.
        self._attach_research_book_state(live_snapshot)

        # Every registered contract is evaluated on this snapshot, not just
        # the one that is trading. Costs no LLM call and places no order.
        # Restricted like the live path: a cross-strategy population whose
        # symbol set moved with another variant's holdings would not be
        # comparable from one cycle to the next.
        setup_breadth_by_strategy = self._record_shadow_decisions(live_snapshot)
        context = live_snapshot.get("_market_context")
        if not isinstance(context, dict):
            context = {}
            live_snapshot["_market_context"] = context
        enrichment = context.get(brain.ENRICHMENT_KEY)
        if not isinstance(enrichment, dict):
            enrichment = {}
            context[brain.ENRICHMENT_KEY] = enrichment
        enrichment["setup_breadth_by_strategy"] = setup_breadth_by_strategy

        # Persist withheld observations now; missing historical inputs cannot
        # be reconstructed later.
        self._record_observations(live_snapshot)

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
        # Only the SOURCE of the decisions differs between the two modes.
        # Everything after this - research recording, risk, execution, the
        # close path - stays one code path, because a second copy of the
        # order logic is a second place for it to diverge.
        if self._shadow_only_order_path():
            # No source of entries at all. Observations were already recorded
            # above and the research lanes below still run, so the platform
            # keeps measuring every contract while the account opens nothing.
            decisions = []
        elif self._deterministic_order_path():
            decisions = self._deterministic_decisions(live_snapshot, max_new)
        else:
            self._journal_llm_input(live_snapshot, portfolio, max_new)
            try:
                decisions = self.llm.decide(live_snapshot, portfolio, max_new)
            except Exception as e:
                self._journal_llm_output()
                log.error("LLM call failed; holding this cycle: %s", e)
                state.log_event("error", f"llm: {e}")
                self._run_shadow_variants(
                    live_snapshot, equity, positions, st, 0.0, decisions=[],
                    advance_accounts=False)
                self.alerts.send(
                    "error", "llm_call_failed",
                    "LLM call failed; holding this cycle", {"error": str(e)})
                return
            self._journal_llm_output()
        # An empty list is a real decision ("no trade"); journal it too so
        # the audit trail distinguishes a deliberate hold from a failed call.
        state.log_event("decisions", json.dumps(decisions))
        decisions = self._record_research_proposals(decisions)
        # Reuse the exact parsed proposals and confidences. No second LLM call,
        # and no deterministic confidence substitution.
        self._run_shadow_variants(
            live_snapshot, equity, positions, st, 0.0, decisions=decisions,
            advance_accounts=False)
        # Pick up any pause/kill the CLI issued while the LLM call was running.
        state.commit(st)
        if st["state"] not in (state.RUNNING, state.DAY_STOPPED):
            return

        for d in [d for d in decisions if d["action"] == "close"]:
            if state.load_state()["state"] not in (
                    state.RUNNING, state.DAY_STOPPED):
                return
            pos = next((p for p in positions
                        if p.get("symbol") == d.get("symbol")), None)
            if pos and self._too_young_to_close(pos, d, st):
                continue
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

        # Opens remain blocked after a daily stop or failed reconciliation.
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
                d, live_snapshot, st)
            if not prepared:
                log.info("Rejected %s %s: %s", d.get("direction"),
                         d.get("symbol"), why)
                state.log_event("rejected", json.dumps(
                    {"symbol": d.get("symbol"), "why": why}))
                continue
            symbol_data = live_snapshot.get(prepared.get("symbol")) or {}
            if symbol_data.get("fee_rate_source") == "unavailable":
                why = "account taker fee unavailable"
                self._mark_setup_status(
                    st, prepared["setup_id"], "risk_rejected")
                log.info("Rejected %s %s: %s", prepared.get("direction"),
                         prepared.get("symbol"), why)
                state.log_event("rejected", json.dumps(
                    {"symbol": prepared.get("symbol"), "why": why,
                     "setup_id": prepared["setup_id"]}),
                    setup_id=prepared["setup_id"])
                continue
            plan, why = self.risk.vet_open(
                prepared, equity, positions, live_snapshot,
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

    def _run_shadow_variants(self, snapshot: dict, equity: float,
                             positions: list, st: dict,
                             gross_notional: float, *,
                             decisions: list[dict] | None = None,
                             advance_accounts: bool = True) -> None:
        """Evaluate registered parameter variants against this snapshot.

        Three properties, each individually tested. It consumes only the
        already-journalled model response and has no exchange, so it cannot
        change the live decision. It is wrapped in try/except, so a failure is
        journalled and swallowed. And it writes only
        ``variant_shadow_decision`` events, never a key in
        ``state.LOOP_KEYS``, so it cannot corrupt trading state even if it
        is wrong. The distinct name prevents parameter-variant records from
        being pooled with cross-strategy shadow records, whose payload has a
        different schema and research meaning.

        A research feature that could interrupt a trading cycle would be a
        safety regression however good its output.
        """
        evaluator = getattr(self, "shadow", None)
        if evaluator is None:
            return
        del equity, positions, st, gross_notional
        try:
            records = evaluator.evaluate(
                snapshot, now=time.time(),
                cycle_id=state.journal_context().get("cycle_id"),
                proposals=decisions,
                advance_accounts=advance_accounts)
            for record in records:
                state.log_event(
                    "variant_shadow_decision",
                    self._audit_json(record.as_event()),
                    variant_id=record.variant_id)
                if record.paper_action:
                    state.log_event(
                        ("variant_paper_trade"
                         if record.portfolio_status == "PAPER"
                         else "variant_shadow_trade"),
                        self._audit_json(record.as_event()),
                        variant_id=record.variant_id)
            coverage = getattr(evaluator, "last_coverage", None)
            if coverage:
                state.log_event(
                    "shadow_coverage", self._audit_json(coverage))
            budget = getattr(evaluator, "last_budget", None)
            if budget is not None and budget.overran:
                state.log_event("shadow_budget_overrun", self._audit_json({
                    "limit_ms": budget.limit_ms,
                    "spent_ms": round(budget.spent_ms(), 2),
                    "records": len(records),
                    "coverage": getattr(evaluator, "last_coverage", {}),
                }))
            self._research_consecutive_failures = 0
            self._research_last_success_ts = time.time()
        except Exception as exc:                           # noqa: BLE001
            self._research_failure_count = int(getattr(
                self, "_research_failure_count", 0)) + 1
            self._research_consecutive_failures = int(getattr(
                self, "_research_consecutive_failures", 0)) + 1
            self._research_last_failure = {
                "phase": "evaluate", "type": type(exc).__name__,
                "ts": time.time(),
            }
            log.warning("Shadow variant evaluation failed: %s", exc)
            try:
                state.log_event("shadow_failed", self._audit_json(
                    {"error": f"{type(exc).__name__}: {exc}"}))
            except Exception:                              # noqa: BLE001
                pass

    def _advance_shadow_variants(self, snapshot: dict, now: float) -> None:
        """Advance local accounts without scheduling any new proposal."""
        evaluator = getattr(self, "shadow", None)
        if evaluator is None:
            return
        try:
            records = evaluator.advance(snapshot, now=now)
            for record in records:
                state.log_event(
                    "variant_shadow_decision",
                    self._audit_json(record.as_event()),
                    variant_id=record.variant_id)
                if record.paper_action:
                    state.log_event(
                        ("variant_paper_trade"
                         if record.portfolio_status == "PAPER"
                         else "variant_shadow_trade"),
                        self._audit_json(record.as_event()),
                        variant_id=record.variant_id)
            self._research_consecutive_failures = 0
            self._research_last_success_ts = time.time()
        except Exception as exc:                           # noqa: BLE001
            self._research_failure_count = int(getattr(
                self, "_research_failure_count", 0)) + 1
            self._research_consecutive_failures = int(getattr(
                self, "_research_consecutive_failures", 0)) + 1
            self._research_last_failure = {
                "phase": "advance", "type": type(exc).__name__,
                "ts": time.time(),
            }
            log.warning("Shadow portfolio advance failed: %s", exc)
            try:
                state.log_event("shadow_failed", self._audit_json(
                    {"phase": "advance",
                     "error": f"{type(exc).__name__}: {exc}"}))
            except Exception:                              # noqa: BLE001
                pass

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
        if not isinstance(pos, dict):
            return None
        info = pos.get("info")
        if info not in (None, "") and not isinstance(info, dict):
            return None
        value = pos.get("id") or (info or {}).get("posId")
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            identity = str(value).strip()
        except Exception:
            return None
        return identity or None

    @staticmethod
    def _quantity_matches(left: float, right: float) -> bool:
        tolerance = max(1e-12, max(abs(left), abs(right)) * 1e-6)
        return abs(left - right) <= tolerance

    def _pause_for_reconciliation_ambiguity(
            self, st: dict, symbol: str, reason: object) -> None:
        audit = getattr(reason, "_order_audit", None) or {}
        self._pause_for_operator_review(
            st, symbol, reason,
            operation="position reconciliation ambiguity",
            order_id=audit.get("order_id"),
            persistence_label="position reconciliation")

    def _reconciliation_ambiguous(
            self, st: dict, symbol: str, message: str,
            *, cause: Exception | None = None):
        error = OrderSubmissionAmbiguousError(
            message, {"symbol": symbol,
                      "outcome": "position_reconciliation_ambiguous"})
        self._pause_for_reconciliation_ambiguity(st, symbol, error)
        if cause is None:
            raise error
        raise error from cause

    @staticmethod
    def _validated_close_summary(summary: object, symbol: str) -> dict:
        if not isinstance(summary, dict) or summary.get("symbol") != symbol:
            raise OrderSubmissionAmbiguousError(
                f"close history for {symbol} has no exact symbol identity",
                {"symbol": symbol, "outcome": "close_history_unresolved"})
        for key in ("price", "qty"):
            raw = summary.get(key)
            if raw in (None, "") or isinstance(raw, bool):
                raise OrderSubmissionAmbiguousError(
                    f"close history for {symbol} has no valid {key}",
                    {"symbol": symbol,
                     "outcome": "close_history_unresolved"})
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise OrderSubmissionAmbiguousError(
                    f"close history for {symbol} has malformed {key}",
                    {"symbol": symbol,
                     "outcome": "close_history_unresolved"}) from exc
            if not math.isfinite(value) or value <= 0:
                raise OrderSubmissionAmbiguousError(
                    f"close history for {symbol} has invalid {key}",
                    {"symbol": symbol,
                     "outcome": "close_history_unresolved"})
            summary[key] = value
        status = summary.get("status")
        if status not in {
                "position_history", "fill_history",
                "fill_history_funding_unavailable"}:
            raise OrderSubmissionAmbiguousError(
                f"close history for {symbol} has an invalid status",
                {"symbol": symbol, "outcome": "close_history_unresolved"})
        for key in ("fee_usd", "funding_usd", "realized_pnl_usd"):
            raw = summary.get(key)
            if raw in (None, "") or isinstance(raw, bool):
                raise OrderSubmissionAmbiguousError(
                    f"close history for {symbol} has no valid {key}",
                    {"symbol": symbol,
                     "outcome": "close_history_unresolved"})
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise OrderSubmissionAmbiguousError(
                    f"close history for {symbol} has malformed {key}",
                    {"symbol": symbol,
                     "outcome": "close_history_unresolved"}) from exc
            if not math.isfinite(value):
                raise OrderSubmissionAmbiguousError(
                    f"close history for {symbol} has non-finite {key}",
                    {"symbol": symbol,
                     "outcome": "close_history_unresolved"})
            summary[key] = value
        return summary

    def _reconcile_positions(self, positions: list[dict], st: dict,
                             startup: bool = False) -> list[dict]:
        """Match local trades to exchange positions and verify SL/TP coverage."""
        active = st.setdefault("active_trades", {})
        protection = st.setdefault("protection", {})
        opened_at = st.setdefault("opened_at", {})
        cooldowns = st.setdefault("cooldowns", {})
        unknown_age: list[str] = []
        actual = {}
        if not isinstance(positions, list):
            self._reconciliation_ambiguous(
                st, "unknown", "OKX positions response is not structured")
        for position in positions:
            if not isinstance(position, dict):
                self._reconciliation_ambiguous(
                    st, "unknown", "OKX returned a malformed live position")
            symbol = position.get("symbol")
            if not isinstance(symbol, str) or not symbol.strip():
                self._reconciliation_ambiguous(
                    st, "unknown", "OKX returned a position without a symbol")
            symbol = symbol.strip()
            if symbol in actual:
                self._reconciliation_ambiguous(
                    st, symbol,
                    "OKX returned multiple positions for one symbol; "
                    "net_mode cannot be proven")
            actual[symbol] = position

        # A tracked trade that disappeared was closed by exchange-side SL/TP
        # or another operator. Recover actual fills before allowing a re-entry.
        for symbol, trade in list(active.items()):
            live = actual.get(symbol)
            tracked_id = self._position_id({
                "id": trade.get("position_id"), "info": {}})
            if not tracked_id:
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"{symbol} durable trade has no exact position identity")
            live_id = None
            live_contracts = None
            live_direction = None
            if live is not None:
                info = live.get("info")
                if info not in (None, "") and not isinstance(info, dict):
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} live position metadata is malformed")
                info = info or {}
                live_id = self._position_id(live)
                if not live_id:
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} live position has no exact position identity")
                raw_side = live.get("side")
                if isinstance(raw_side, str) and raw_side.strip().lower() in {
                        "long", "short"}:
                    live_direction = raw_side.strip().lower()
                else:
                    raw_position = info.get("pos")
                    if isinstance(raw_position, bool):
                        self._reconciliation_ambiguous(
                            st, symbol,
                            f"{symbol} live position direction is malformed")
                    try:
                        signed_position = float(raw_position)
                    except (TypeError, ValueError, OverflowError) as exc:
                        self._reconciliation_ambiguous(
                            st, symbol,
                            f"{symbol} live position direction is malformed",
                            cause=exc)
                    if not math.isfinite(signed_position) \
                            or signed_position == 0:
                        self._reconciliation_ambiguous(
                            st, symbol,
                            f"{symbol} live position direction is invalid")
                    live_direction = (
                        "long" if signed_position > 0 else "short")
                raw_contracts = live.get("contracts")
                if isinstance(raw_contracts, bool):
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} live position contracts are malformed")
                try:
                    live_contracts = abs(float(raw_contracts))
                except (TypeError, ValueError, OverflowError) as exc:
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} live position contracts are malformed",
                        cause=exc)
                if not math.isfinite(live_contracts) or live_contracts <= 0:
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} live position contracts are invalid")
            same_direction = (
                live is not None and live_direction == trade.get("direction"))
            same_id = live is not None and live_id == tracked_id
            if live is not None and same_direction and same_id:
                raw_tracked_qty = trade.get("qty")
                if isinstance(raw_tracked_qty, bool):
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} durable trade quantity is malformed")
                try:
                    tracked_qty = float(raw_tracked_qty)
                except (TypeError, ValueError, OverflowError) as exc:
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} durable trade quantity is malformed",
                        cause=exc)
                if (not math.isfinite(tracked_qty) or tracked_qty <= 0
                        or not self._quantity_matches(
                            live_contracts, tracked_qty)):
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} live contracts differ from the durable "
                        "trade; manual add/reduction cannot be auto-adopted")
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
                summary = self._validated_close_summary(summary, symbol)
                if not self._quantity_matches(
                        float(summary["qty"]), float(trade.get("qty"))):
                    raise OrderSubmissionAmbiguousError(
                        f"close history quantity for {symbol} does not match "
                        "the durable trade remainder",
                        {"symbol": symbol,
                         "outcome": "close_history_unresolved"})
                realized = float(summary["realized_pnl_usd"])
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
                    "close", summary["qty"],
                    summary["price"], entry_notional,
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
                    except (CredentialError, ccxt.AuthenticationError,
                            state.JournalError,
                            OrderSubmissionAmbiguousError):
                        raise
                    except Exception as cleanup_exc:
                        log.warning("stale protective-order cleanup failed "
                                    "for %s: %s", symbol, cleanup_exc)
            except (CredentialError, ccxt.AuthenticationError,
                    state.JournalError):
                raise
            except OrderSubmissionAmbiguousError as exc:
                self._pause_for_reconciliation_ambiguity(st, symbol, exc)
                raise
            except Exception as exc:
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"could not safely reconcile the prior {symbol} trade",
                    cause=exc)

        for symbol, pos in list(actual.items()):
            info = pos.get("info")
            if info not in (None, "") and not isinstance(info, dict):
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"{symbol} live position metadata is malformed")
            info = info or {}
            position_id = self._position_id(pos)
            if not position_id:
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"{symbol} live position has no exact position identity")
            raw_side = pos.get("side")
            if isinstance(raw_side, str) and raw_side.strip().lower() in {
                    "long", "short"}:
                direction = raw_side.strip().lower()
            else:
                raw_position = info.get("pos")
                if isinstance(raw_position, bool):
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} live position direction is malformed")
                try:
                    signed_position = float(raw_position)
                except (TypeError, ValueError, OverflowError) as exc:
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} live position direction is malformed",
                        cause=exc)
                if not math.isfinite(signed_position) or signed_position == 0:
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} live position direction is invalid")
                direction = "long" if signed_position > 0 else "short"
            raw_contracts = pos.get("contracts")
            if isinstance(raw_contracts, bool):
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"{symbol} live position contracts are malformed")
            try:
                contracts = abs(float(raw_contracts))
            except (TypeError, ValueError, OverflowError) as exc:
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"{symbol} live position contracts are malformed",
                    cause=exc)
            if not math.isfinite(contracts) or contracts <= 0:
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"{symbol} live position contracts are invalid")
            mark_raw = pos.get("markPrice")
            if mark_raw in (None, ""):
                mark_raw = pos.get("last")
            if mark_raw in (None, ""):
                mark_raw = pos.get("entryPrice")
            if isinstance(mark_raw, bool):
                self._reconciliation_ambiguous(
                    st, symbol, f"{symbol} live mark price is malformed")
            try:
                mark = float(mark_raw)
            except (TypeError, ValueError, OverflowError) as exc:
                self._reconciliation_ambiguous(
                    st, symbol, f"{symbol} live mark price is malformed",
                    cause=exc)
            if not math.isfinite(mark) or mark <= 0:
                self._reconciliation_ambiguous(
                    st, symbol, f"{symbol} live mark price is invalid")
            try:
                status = self.ex.protection_status(
                    symbol, contracts, direction, mark)
            except (CredentialError, ccxt.AuthenticationError,
                    state.JournalError):
                raise
            except OrderSubmissionAmbiguousError as exc:
                self._pause_for_reconciliation_ambiguity(st, symbol, exc)
                raise
            except Exception as exc:
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"could not verify protection for {symbol}", cause=exc)
            if not isinstance(status, dict):
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"{symbol} protection status is not structured")
            if (not isinstance(status.get("stop_loss"), bool)
                    or not isinstance(status.get("take_profit"), bool)):
                self._reconciliation_ambiguous(
                    st, symbol,
                    f"{symbol} protection flags are not exact booleans")

            if symbol not in active:
                # Adopt a pre-existing position so its eventual exit remains
                # measurable. Unknown positions are never assumed protected.
                trade_id = state.new_trade_id()
                entry_raw = pos.get("entryPrice")
                if entry_raw in (None, ""):
                    entry_raw = mark
                leverage_raw = pos.get("leverage")
                try:
                    entry = float(entry_raw)
                    leverage = float(leverage_raw)
                    notional = float(self._notional(pos))
                except (TypeError, ValueError, OverflowError) as exc:
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} adoption metadata is malformed", cause=exc)
                if (not math.isfinite(entry) or entry <= 0
                        or not math.isfinite(notional) or notional <= 0
                        or not math.isfinite(leverage) or leverage < 0):
                    self._reconciliation_ambiguous(
                        st, symbol,
                        f"{symbol} adoption metadata is invalid")
                recovered_opened_at = opened_at.get(symbol)
                if not recovered_opened_at:
                    try:
                        recovered_opened_at = self.ex.position_opened_at(pos)
                    except (CredentialError, ccxt.AuthenticationError,
                            state.JournalError):
                        raise
                    except OrderSubmissionAmbiguousError as exc:
                        self._pause_for_reconciliation_ambiguity(
                            st, symbol, exc)
                        raise
                    except Exception as exc:
                        self._reconciliation_ambiguous(
                            st, symbol,
                            f"could not recover opening time for {symbol}",
                            cause=exc)
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
                    "position_id": position_id,
                    "leverage": leverage,
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
                    contracts, entry, notional, leverage,
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
                    except (CredentialError, ccxt.AuthenticationError,
                            state.JournalError):
                        raise
                    except OrderSubmissionAmbiguousError as exc:
                        self._pause_for_reconciliation_ambiguity(
                            st, symbol, exc)
                        raise
                    except Exception as exc:
                        self._reconciliation_ambiguous(
                            st, symbol,
                            f"protection restore failed for {symbol}",
                            cause=exc)
                    if (not isinstance(status, dict)
                            or not isinstance(status.get("stop_loss"), bool)
                            or not isinstance(
                                status.get("take_profit"), bool)):
                        self._reconciliation_ambiguous(
                            st, symbol,
                            f"{symbol} restored protection status is malformed")
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

    @staticmethod
    def _exception_in_chain(exc: BaseException, error_type):
        """Find a classified cause without relying on exception text."""
        current = exc
        seen = set()
        while current is not None and id(current) not in seen:
            if isinstance(current, error_type):
                return current
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        return None

    def _discard_pending_entry(self) -> None:
        clear = getattr(self.ex, "_clear_pending_entry", None)
        if callable(clear):
            clear()

    def _pause_for_operator_review(
            self, st: dict, symbol: str, reason: object, *, operation: str,
            order_id: object = None, discard_pending_entry: bool = False,
            persistence_label: str = "execution ambiguity") -> dict:
        """Persist an operator pause for an uncertain exchange-side result."""
        if discard_pending_entry:
            self._discard_pending_entry()
        current = state.load_state()
        if current.get("state") == state.KILLED:
            # State.set_state enforces this atomically too. Keep this fast path
            # so the caller's in-memory view immediately reflects the kill.
            st.clear()
            st.update(current)
            return current
        safe_reason = Exchange._safe_exchange_error_text(reason)[:240]
        safe_symbol = str(symbol)[:80]
        safe_order = (
            str(order_id).strip()[:80]
            if order_id not in (None, "") else "unknown")
        context = (
            f"{operation} for {safe_symbol} (order {safe_order}); "
            f"inspect OKX before explicit operator resume: {safe_reason}"
        )[:480]
        try:
            paused = state.set_state(
                state.PAUSED, reason=context, operator_pause=True)
        except state.JournalError:
            raise
        except Exception as exc:                           # noqa: BLE001
            raise state.JournalError(
                f"{persistence_label} pause persistence failed: {exc}") from exc
        st.clear()
        st.update(paused)
        if paused.get("state") == state.KILLED:
            log.warning(
                "Operator-review pause lost a race to KILLED after %s for %s; "
                "KILLED remains authoritative", operation, symbol)
        else:
            log.critical(
                "Agent PAUSED for operator review after %s for %s: %s",
                operation, symbol, safe_reason)
        return paused

    def _pause_for_entry_ambiguity(
            self, st: dict, symbol: str, reason: object,
            *, order_id: object = None) -> dict:
        return self._pause_for_operator_review(
            st, symbol, reason, operation="entry order ambiguity",
            order_id=order_id, discard_pending_entry=True,
            persistence_label="entry ambiguity")

    def _pause_for_close_ambiguity(
            self, st: dict, symbol: str, reason: object,
            *, order_id: object = None) -> dict:
        return self._pause_for_operator_review(
            st, symbol, reason, operation="close order ambiguity",
            order_id=order_id, persistence_label="close ambiguity")

    def _pause_for_unsettled_fill(
            self, st: dict, symbol: str, reason: object,
            *, order_id: object = None) -> dict:
        return self._pause_for_operator_review(
            st, symbol, reason,
            operation="filled entry could not be safely settled",
            order_id=order_id, discard_pending_entry=True,
            persistence_label="post-fill safety")

    def _maker_first_attempt(self, plan: dict, st: dict, symbol: str,
                             side: str, contracts: float, sl_price: float,
                             tp_price: float, reference: float):
        """Try passively first. Returns a settled execution, or None to cross.

        Maker-first entry study. Every IOC entry crosses the spread and accepts adverse
        selection, and at a 2% stop round-trip friction is roughly 10% of the
        risk unit - so converting the filled fraction from taker to maker
        moves expectancy by more than most of the parameter axes queued for
        sweeping, without requiring any signal to have edge.

        Only an explicit zero-fill plus terminal cancellation may cross. Any
        exception or malformed maker result is fail-closed, because the
        exchange may have accepted an order whose state is not known here.
        """
        execution_cfg = self.cfg["execution"]
        if not execution_cfg.get("maker_first_enabled"):
            return None

        terminal_cancel = {"canceled", "cancelled", "expired", "rejected"}

        def journal(payload: dict) -> None:
            try:
                state.log_event(
                    "maker_attempt", self._audit_json(payload))
            except state.JournalError:
                self._discard_pending_entry()
                raise
            except Exception as exc:                       # noqa: BLE001
                self._discard_pending_entry()
                raise state.JournalError(
                    f"maker_attempt journal failed: {exc}") from exc

        def pause_and_block(reason: object, *, audit: dict | None = None,
                            order_id: object = None) -> bool:
            details = dict(audit or {}) if isinstance(audit, dict) else {}
            self._pause_for_entry_ambiguity(
                st, symbol, reason,
                order_id=order_id or details.get("order_id"))
            journal({
                "symbol": symbol,
                "outcome": "ambiguous",
                "error": Exchange._safe_exchange_error_text(reason),
                "submission_audit": details or None,
                "crossing_fallback_allowed": False,
            })
            try:
                self.alerts.send(
                    "critical", "maker_order_ambiguous",
                    f"Maker-first order state for {symbol} is ambiguous; "
                    "the agent was paused and crossing was blocked",
                    {"symbol": symbol,
                     "order_id": order_id or details.get("order_id"),
                     "client_order_id": details.get("client_order_id")})
            except state.JournalError:
                raise
            except Exception:                               # noqa: BLE001
                log.exception("failed to alert maker-first block for %s",
                              symbol)
            return False

        try:
            attempt = self.ex.maker_first_entry(
                symbol, side, contracts, plan["leverage"], sl_price,
                tp_price,
                float(execution_cfg["maker_first_wait_seconds"]),
                reference)
        except state.JournalError:
            self._discard_pending_entry()
            raise
        except (CredentialError, ccxt.AuthenticationError) as exc:
            audit = getattr(exc, "_order_audit", None) or {}
            if getattr(exc, "_post_fill_unsettled", False):
                self._pause_for_unsettled_fill(
                    st, symbol, exc, order_id=audit.get("order_id"))
            elif str(audit.get("outcome") or "").startswith("ambiguous_"):
                self._pause_for_entry_ambiguity(
                    st, symbol, exc, order_id=audit.get("order_id"))
            else:
                self._discard_pending_entry()
            raise
        except MakerFirstPreSubmitError as exc:
            nested_journal = self._exception_in_chain(exc, state.JournalError)
            if nested_journal is not None:
                self._discard_pending_entry()
                raise nested_journal
            nested_credential = self._exception_in_chain(
                exc, (CredentialError, ccxt.AuthenticationError))
            if nested_credential is not None:
                self._discard_pending_entry()
                raise nested_credential
            self._discard_pending_entry()
            self._remember_entry_failure(
                plan, st, exc, "maker_pre_submit")
            log.warning("Maker-first pre-submit attempt failed for %s: %s",
                        symbol, exc)
            return False
        except MakerFirstAmbiguousError as exc:
            audit = getattr(exc, "_order_audit", None) or {}
            nested_journal = self._exception_in_chain(exc, state.JournalError)
            if nested_journal is not None:
                self._pause_for_entry_ambiguity(
                    st, symbol, exc, order_id=audit.get("order_id"))
                raise nested_journal
            nested_credential = self._exception_in_chain(
                exc, (CredentialError, ccxt.AuthenticationError))
            if nested_credential is not None:
                self._pause_for_entry_ambiguity(
                    st, symbol, exc, order_id=audit.get("order_id"))
                raise nested_credential
            return pause_and_block(
                exc, audit=audit, order_id=audit.get("order_id"))
        except Exception as exc:                           # noqa: BLE001
            # Exchange.maker_first_entry classifies every known pre-submit
            # failure. An unclassified exception therefore cannot prove that
            # no order exists and is durable ambiguity.
            audit = getattr(exc, "_order_audit", None) or {}
            nested_journal = self._exception_in_chain(exc, state.JournalError)
            if nested_journal is not None:
                self._pause_for_entry_ambiguity(
                    st, symbol, exc, order_id=audit.get("order_id"))
                raise nested_journal
            nested_credential = self._exception_in_chain(
                exc, (CredentialError, ccxt.AuthenticationError))
            if nested_credential is not None:
                self._pause_for_entry_ambiguity(
                    st, symbol, exc, order_id=audit.get("order_id"))
                raise nested_credential
            return pause_and_block(
                exc, audit=audit, order_id=audit.get("order_id"))

        if not isinstance(attempt, dict):
            return pause_and_block(
                "maker-first result was not structured")
        try:
            if isinstance(attempt.get("filled_contracts"), bool):
                raise ValueError("filled_contracts is boolean")
            if isinstance(attempt.get("fill_rate"), bool):
                raise ValueError("fill_rate is boolean")
            if isinstance(attempt.get("requested_contracts"), bool):
                raise ValueError("requested_contracts is boolean")
            filled = float(attempt.get("filled_contracts"))
            fill_rate = float(attempt.get("fill_rate"))
            requested = float(attempt.get("requested_contracts"))
            requested_here = float(contracts)
        except (TypeError, ValueError, OverflowError) as exc:
            return pause_and_block(
                f"maker-first result quantities were malformed: {exc}",
                audit=attempt.get("submission_audit"),
                order_id=attempt.get("order_id"))
        if (not all(math.isfinite(value)
                    for value in (filled, fill_rate, requested,
                                  requested_here))
                or requested <= 0 or filled < 0 or filled > requested
                or fill_rate < 0 or fill_rate > 1):
            return pause_and_block(
                "maker-first result quantities were invalid",
                audit=attempt.get("submission_audit"),
                order_id=attempt.get("order_id"))
        if requested != requested_here:
            return pause_and_block(
                "maker-first result requested quantity did not match request",
                audit=attempt.get("submission_audit"),
                order_id=attempt.get("order_id"))
        if attempt.get("symbol") != symbol:
            return pause_and_block(
                "maker-first result symbol did not match request",
                audit=attempt.get("submission_audit"),
                order_id=attempt.get("order_id"))
        raw_order_id = attempt.get("order_id")
        try:
            order_id = str(raw_order_id).strip()
        except Exception as exc:                           # noqa: BLE001
            return pause_and_block(
                f"maker-first result order id was malformed: {exc}",
                audit=attempt.get("submission_audit"))
        if not order_id:
            return pause_and_block(
                "maker-first result had no order id",
                audit=attempt.get("submission_audit"))
        quantity_evidence = attempt.get("quantity_evidence")
        if quantity_evidence not in {
                "filled", "info.accFillSz", "info.fillSz"}:
            return pause_and_block(
                "maker-first result lacked explicit quantity evidence",
                audit=attempt.get("submission_audit"), order_id=order_id)
        expected_rate = filled / requested
        if abs(fill_rate - expected_rate) > 1e-9:
            return pause_and_block(
                "maker-first result fill rate was inconsistent",
                audit=attempt.get("submission_audit"), order_id=order_id)

        if attempt.get("resting") is True:
            # The cancel failed and the order may still be live. Crossing now
            # could double the position, so this cycle ends here.
            return pause_and_block(
                "maker-first order may still be resting",
                audit=attempt.get("submission_audit"), order_id=order_id)
        if attempt.get("resting") is not False:
            return pause_and_block(
                "maker-first result lacked an explicit resting state",
                audit=attempt.get("submission_audit"), order_id=order_id)

        # A partial fill is NOT topped up by crossing the remainder. Splitting
        # one setup across two prices would make the recorded entry an average
        # of two decisions, and entry price is exactly what the maker-first
        # counterfactual measures.
        # The position is smaller than planned, which is journalled and is the
        # conservative direction.
        execution = attempt.get("execution")
        if filled == 0:
            status = str(attempt.get("cancellation_status") or "").lower()
            cancellation_order_id = attempt.get("cancellation_order_id")
            # This is the sole crossing hand-off. Every field is exact and
            # explicit so a partial response cannot accidentally open.
            if (fill_rate == 0 and attempt.get("cancelled") is True
                    and attempt.get("cancellation_confirmed") is True
                    and status in terminal_cancel
                    and attempt.get("resting") is False
                    and execution is None
                    and cancellation_order_id == order_id
                    and quantity_evidence in {
                        "filled", "info.accFillSz", "info.fillSz"}):
                journalled = {k: v for k, v in attempt.items()
                              if k != "execution"}
                journal({"symbol": symbol, "outcome": "unfilled",
                         **journalled})
                return None
            return pause_and_block(
                "maker-first zero-fill result lacked exact terminal "
                "cancellation evidence",
                audit=attempt.get("submission_audit"), order_id=order_id)
        if filled < requested:
            status = str(attempt.get("cancellation_status") or "").lower()
            if (attempt.get("cancelled") is not True
                    or attempt.get("cancellation_confirmed") is not True
                    or status not in terminal_cancel
                    or attempt.get("cancellation_order_id") != order_id):
                return pause_and_block(
                    "maker-first partial fill lacked terminal cancellation",
                    audit=attempt.get("submission_audit"), order_id=order_id)
        if not isinstance(execution, dict):
            return pause_and_block(
                "maker-first filled result lacked settlement",
                audit=attempt.get("submission_audit"), order_id=order_id)
        journalled = {k: v for k, v in attempt.items()
                      if k != "execution"}
        journal({
            "symbol": symbol,
            "outcome": "filled" if filled >= requested else "partial",
            **journalled,
        })
        # Partial and full fills are already entries. They never cross the
        # remainder; settled execution continues through normal bookkeeping.
        return execution

    def _execute_open(self, plan: dict, st: dict) -> bool:
        symbol = plan["symbol"]
        if state.load_state()["state"] != state.RUNNING:
            log.info("Control state changed; skipping entry for %s", symbol)
            return False
        try:
            live = self.ex.price(symbol)
        except state.JournalError:
            raise
        except (CredentialError, ccxt.AuthenticationError):
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
        except state.JournalError:
            raise
        except (CredentialError, ccxt.AuthenticationError):
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

        # Passive and crossing fills share the same settlement and risk checks.
        maker = self._maker_first_attempt(
            plan, st, symbol, side, contracts, sl_price, tp_price,
            entry_reference)
        if maker is False:
            return False                     # order may be resting; stop
        if maker is not None:
            return self._settle_entry(
                plan, st, maker, symbol, side, sl_price, tp_price)

        # A pause/kill can arrive while the passive order is waiting or being
        # cancelled. Consume the one-shot hand-off and refuse the IOC unless
        # the durable control state is still RUNNING immediately beforehand.
        try:
            control = state.load_state()
        except Exception:
            self._discard_pending_entry()
            raise
        if control.get("state") != state.RUNNING:
            self._discard_pending_entry()
            st.clear()
            st.update(control)
            log.info("Control state changed during maker wait; skipping "
                     "crossing fallback for %s", symbol)
            return False

        try:
            execution = self.ex.open_position(
                symbol, side, contracts, plan["leverage"], sl_price, tp_price,
                expected_price=entry_reference,
                entry_limit_price=entry_guard["limit_price"])
        except state.JournalError:
            raise
        except (CredentialError, ccxt.AuthenticationError) as exc:
            if getattr(exc, "_post_fill_unsettled", False):
                audit = getattr(exc, "_order_audit", None) or {}
                self._pause_for_unsettled_fill(
                    st, symbol, exc, order_id=audit.get("order_id"))
            raise
        except OrderSubmissionAmbiguousError as exc:
            audit = getattr(exc, "_order_audit", None) or {}
            self._pause_for_entry_ambiguity(
                st, symbol, exc, order_id=audit.get("order_id"))
            try:
                state.log_event(
                    "entry_order_ambiguous",
                    self._audit_json({
                        "symbol": symbol,
                        "submission_audit": audit or None,
                        "crossing_fallback": bool(
                            audit.get("params", {}).get("timeInForce")
                            == "IOC") if isinstance(audit, dict) else False,
                    }),
                    setup_id=plan.get("setup_id"),
                )
            except state.JournalError:
                raise
            except Exception as journal_exc:                # noqa: BLE001
                raise state.JournalError(
                    f"entry ambiguity journal failed: {journal_exc}"
                ) from journal_exc
            try:
                self.alerts.send(
                    "critical", "entry_order_ambiguous",
                    f"Entry order state for {symbol} is ambiguous; the "
                    "agent was paused for operator review",
                    {"symbol": symbol,
                     "client_order_id": audit.get("client_order_id")})
            except state.JournalError:
                raise
            except Exception:                               # noqa: BLE001
                log.exception("failed to alert direct entry ambiguity for %s",
                              symbol)
            return False
        except Exception as e:
            self._remember_entry_failure(
                plan, st, e, "attached_entry")
            log.error("Entry failed for %s: %s", symbol, e)
            return False

        return self._settle_entry(
            plan, st, execution, symbol, side, sl_price, tp_price)

    @staticmethod
    def _trimmed_execution_id(value: object) -> str | None:
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            identity = str(value).strip()
        except Exception:
            return None
        return identity or None

    def _validated_entry_execution(self, plan: dict, execution: object,
                                   symbol: str) -> tuple[dict, float]:
        def fail(message: str, cause: Exception | None = None):
            audit = {}
            if isinstance(execution, dict):
                raw_audit = execution.get("submission_audit")
                if isinstance(raw_audit, dict):
                    audit.update(raw_audit)
                audit.update({
                    "order_id": execution.get("order_id"),
                    "last_fill_status": execution.get("status"),
                    "symbol": symbol,
                    "outcome": "post_fill_unsettled",
                })
            error = OrderSubmissionAmbiguousError(
                f"filled entry for {symbol} has invalid execution evidence: "
                f"{message}", audit)
            setattr(error, "_post_fill_unsettled", True)
            if cause is None:
                raise error
            raise error from cause

        if not isinstance(execution, dict):
            fail("execution is not structured")
        execution_symbol = execution.get("symbol")
        if (not isinstance(execution_symbol, str)
                or execution_symbol.strip() != symbol):
            fail("symbol identity is missing or mismatched")
        for key in ("order_id", "client_order_id", "position_id"):
            identity = self._trimmed_execution_id(execution.get(key))
            if not identity:
                fail(f"{key} is missing or malformed")
            execution[key] = identity
        status = execution.get("status")
        if not isinstance(status, str) or not status.strip():
            fail("status is missing or malformed")
        execution["status"] = status.strip().lower()
        if not isinstance(execution.get("partial"), bool):
            fail("partial flag is not an exact boolean")
        protection = execution.get("protection")
        if not isinstance(protection, dict):
            fail("protection status is not structured")
        if (not isinstance(protection.get("stop_loss"), bool)
                or not isinstance(protection.get("take_profit"), bool)):
            fail("protection flags are not exact booleans")
        for key in ("filled", "average", "position_contracts"):
            raw = execution.get(key)
            if raw in (None, "") or isinstance(raw, bool):
                fail(f"{key} is missing or malformed")
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                fail(f"{key} is malformed", exc)
            if not math.isfinite(value) or value <= 0:
                fail(f"{key} is not positive and finite")
            execution[key] = value
        if not self._quantity_matches(
                execution["filled"], execution["position_contracts"]):
            fail("position contracts do not match the verified fill")
        for key in ("fee_usd", "slippage_usd", "adverse_slippage_usd"):
            raw = execution.get(key)
            if raw in (None, "") or isinstance(raw, bool):
                fail(f"{key} is missing or malformed")
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                fail(f"{key} is malformed", exc)
            if not math.isfinite(value):
                fail(f"{key} is not finite")
            execution[key] = value
        estimated_loss_raw = plan.get("estimated_loss_pct")
        if isinstance(estimated_loss_raw, bool):
            fail("estimated loss is malformed")
        try:
            estimated_loss = float(estimated_loss_raw)
        except (TypeError, ValueError, OverflowError) as exc:
            fail("estimated loss is malformed", exc)
        if not math.isfinite(estimated_loss) or estimated_loss <= 0:
            fail("estimated loss is not positive and finite")
        plan["estimated_loss_pct"] = estimated_loss
        try:
            market = self.ex.x.market(symbol)
        except (CredentialError, ccxt.AuthenticationError, state.JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            fail("contract metadata could not be read", exc)
        if not isinstance(market, dict):
            fail("contract metadata is not structured")
        contract_size_raw = market.get("contractSize")
        if contract_size_raw in (None, "") or isinstance(
                contract_size_raw, bool):
            fail("contract size is missing or malformed")
        try:
            contract_size = float(contract_size_raw)
        except (TypeError, ValueError, OverflowError) as exc:
            fail("contract size is malformed", exc)
        if not math.isfinite(contract_size) or contract_size <= 0:
            fail("contract size is not positive and finite")
        return execution, contract_size

    def _settle_entry(self, plan: dict, st: dict, execution: dict,
                      symbol: str, side: str, sl_price: float,
                      tp_price: float) -> bool:
        try:
            return self._settle_entry_impl(
                plan, st, execution, symbol, side, sl_price, tp_price)
        except (CredentialError, ccxt.AuthenticationError,
                state.JournalError, OrderSubmissionAmbiguousError) as exc:
            if (isinstance(exc, OrderSubmissionAmbiguousError)
                    and getattr(exc, "_post_fill_unsettled", False)):
                audit = getattr(exc, "_order_audit", None) or {}
                self._pause_for_unsettled_fill(
                    st, symbol, exc, order_id=audit.get("order_id"))
            raise
        except Exception as exc:
            error = OrderSubmissionAmbiguousError(
                f"filled entry for {symbol} could not be safely settled: {exc}",
                {"symbol": symbol,
                 "order_id": execution.get("order_id")
                 if isinstance(execution, dict) else None,
                 "outcome": "post_fill_unsettled"})
            setattr(error, "_post_fill_unsettled", True)
            self._pause_for_unsettled_fill(
                st, symbol, error,
                order_id=(execution.get("order_id")
                          if isinstance(execution, dict) else None))
            raise error from exc

    def _settle_entry_impl(self, plan: dict, st: dict, execution: dict,
                           symbol: str, side: str, sl_price: float,
                           tp_price: float) -> bool:
        """Everything that must happen once an entry has actually filled.

        Extracted so that any path which can create a position runs the same
        code: the trade journal row, the liquidation-distance check, the
        protection audit that confirms the exchange-side stop really exists,
        and the state bookkeeping that lets reconciliation recognise the
        position later.

        This exists because B7.5 needs a second entry path. A passive
        maker-first fill creates a position exactly as an IOC fill does, and
        a position that skipped any of the below is one the engine cannot
        manage - unjournalled, unverified, or invisible to reconciliation.
        Sharing the code is the only way both paths stay correct as it
        changes.
        """
        execution, contract_size = self._validated_entry_execution(
            plan, execution, symbol)
        filled = execution["filled"]
        fill_price = execution["average"]
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
        stop_verified = execution["protection"]["stop_loss"]
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
            "position_id": execution["position_id"],
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
            "contracts": execution["position_contracts"],
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
                "contracts": execution["position_contracts"],
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
                except (CredentialError, ccxt.AuthenticationError) as exc:
                    audit = getattr(exc, "_order_audit", None) or {}
                    self._pause_for_unsettled_fill(
                        st, symbol, exc, order_id=audit.get("order_id"))
                    raise
                except (state.JournalError,
                        OrderSubmissionAmbiguousError):
                    raise
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
                except (CredentialError, ccxt.AuthenticationError) as exc:
                    audit = getattr(exc, "_order_audit", None) or {}
                    self._pause_for_unsettled_fill(
                        st, symbol, exc, order_id=audit.get("order_id"))
                    raise
                except (state.JournalError,
                        OrderSubmissionAmbiguousError):
                    raise
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
                except state.JournalError:
                    raise
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
        except OrderSubmissionAmbiguousError as exc:
            audit = getattr(exc, "_order_audit", None) or {}
            self._pause_for_close_ambiguity(
                st, symbol, exc, order_id=audit.get("order_id"))
            raise
        except (CredentialError, ccxt.AuthenticationError,
                state.JournalError):
            raise
        except Exception as e:
            log.error("Close failed for %s: %s", symbol, e)
            return False
        try:
            execution = Exchange._validated_close_execution(
                execution, pos.get("contracts"), symbol)
        except OrderSubmissionAmbiguousError as exc:
            audit = getattr(exc, "_order_audit", None) or {}
            self._pause_for_close_ambiguity(
                st, symbol, exc, order_id=audit.get("order_id"))
            raise
        trade = (st.get("active_trades") or {}).get(symbol) or {}
        direction = trade.get("direction") or self._direction(pos)
        if close_trigger is None:
            close_trigger = "engine_safety"
        if close_evidence is None:
            close_evidence = reason
        if not execution["fully_closed"]:
            remaining = execution["remaining_contracts"]
            filled = execution["filled"]
            fill_price = execution["average"]
            entry_price = float(
                trade.get("entry_price") or pos.get("entryPrice") or 0)
            try:
                market = self.ex.x.market(symbol)
                contract_size_raw = (
                    market.get("contractSize")
                    if isinstance(market, dict) else None)
                if (contract_size_raw in (None, "")
                        or isinstance(contract_size_raw, bool)):
                    raise ValueError("contract size is missing")
                contract_size = float(contract_size_raw)
                if not math.isfinite(contract_size) or contract_size <= 0:
                    raise ValueError("contract size is invalid")
            except (CredentialError, ccxt.AuthenticationError,
                    state.JournalError, OrderSubmissionAmbiguousError):
                raise
            except Exception as exc:
                error = OrderSubmissionAmbiguousError(
                    f"partial close metadata for {symbol} is ambiguous: {exc}",
                    {"symbol": symbol, "order_id": execution["order_id"],
                     "outcome": "close_result_ambiguous"})
                self._pause_for_close_ambiguity(
                    st, symbol, error, order_id=execution["order_id"])
                raise error from exc
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
            exit_fee = execution["fee_usd"]
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

        price = execution["average"]
        qty = execution["filled"]
        entry_price = float(trade.get("entry_price") or pos.get("entryPrice") or 0)
        entry_notional = float(trade.get("entry_notional") or self._notional(pos))
        entry_fee = float(trade.get(
            "entry_fee_remaining_usd", trade.get("entry_fee_usd") or 0))
        exit_fee = execution["fee_usd"]
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
        try:
            market = self.ex.x.market(symbol)
            contract_size_raw = (
                market.get("contractSize")
                if isinstance(market, dict) else None)
            if (contract_size_raw in (None, "")
                    or isinstance(contract_size_raw, bool)):
                raise ValueError("contract size is missing")
            contract_size = float(contract_size_raw)
            if not math.isfinite(contract_size) or contract_size <= 0:
                raise ValueError("contract size is invalid")
        except (CredentialError, ccxt.AuthenticationError,
                state.JournalError, OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            error = OrderSubmissionAmbiguousError(
                f"close metadata for {symbol} is ambiguous: {exc}",
                {"symbol": symbol, "order_id": execution["order_id"],
                 "outcome": "close_result_ambiguous"})
            self._pause_for_close_ambiguity(
                st, symbol, error, order_id=execution["order_id"])
            raise error from exc
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

    @staticmethod
    def _plain(value):
        """JSON-safe number, or None.

        The journal serializer runs with allow_nan=False, so a single NaN
        field would raise and cost the whole cycle's shadow records for that
        strategy. A missing measurement recorded as null is recoverable;
        a lost cycle is not.
        """
        if value is None or isinstance(value, (bool, str)):
            return value if not isinstance(value, bool) else value
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _plain_levels(value):
        """Return a JSON-safe executable depth ladder and why it was cut.

        ``_plain`` is intentionally scalar-only. Order-book depth is the one
        research field that must retain its two-dimensional shape so the
        forward simulator can walk observed prices and contract amounts.

        Truncating at the first malformed level replaces an earlier rule that
        discarded the whole ladder. A ladder is ordered outward from the
        touch, so a valid prefix is a true book that is merely shallower than
        the one observed: it understates available depth, which is the
        conservative direction for a simulator deciding whether size could
        have filled. Discarding it instead produced a data-missing veto, and
        on the 2026-07-29..08-05 corpus that silently rejected 6,184 of 8,727
        ladders while the fetch itself never once failed, starving six of
        seven strategies of 63-100% of their decisions with no error recorded.

        Returns ``(levels, reason)``. ``reason`` is None only when the whole
        observed ladder survived, so a caller can never mistake a truncated
        book for a complete one.
        """
        if not isinstance(value, (list, tuple)):
            return None, f"depth ladder is {type(value).__name__}, not a list"
        levels: list[list[float]] = []
        for index, raw in enumerate(value):
            reason = None
            if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                reason = f"level {index} is not a [price, amount] pair"
            else:
                try:
                    price = float(raw[0])
                    amount = float(raw[1])
                except (TypeError, ValueError):
                    reason = f"level {index} has non-numeric price or amount"
                else:
                    if not math.isfinite(price) or price <= 0:
                        reason = f"level {index} has invalid price {raw[0]!r}"
                    elif not math.isfinite(amount) or amount < 0:
                        reason = f"level {index} has invalid amount {raw[1]!r}"
            if reason is not None:
                note = f"{reason}; kept {len(levels)} of {len(value)} levels"
                # Zero surviving levels is not a shallow book, it is no book.
                # ``missing_fields`` treats only None as absent, so returning
                # an empty list here would present unusable depth to a
                # contract as though it were observed.
                return (levels or None), note
            levels.append([price, amount])
        return levels, None

    def _shadow_cfg(self, spec) -> dict:
        """Config a shadow strategy's contract is evaluated against.

        The active strategy uses config.yaml unchanged, so its shadow record
        is exactly what the live contract saw. Every other strategy uses the
        parameters declared on its registry entry, so a shadow result is
        attributable to a stated parameter set rather than to whichever
        strategy happened to be trading that day.
        """
        if spec.id == str(self.cfg["strategy"]["id"]):
            return self.cfg
        block = dict(self.cfg["strategy"])
        block.update(spec.contract_params)
        block["id"] = spec.id
        block["version"] = spec.version
        return {**self.cfg, "strategy": block}

    def _execution_mode(self) -> str:
        # Configuration validation already guarantees an exact value.
        return (self.cfg.get("strategy") or {}).get(
            "execution_mode", "analyst")

    def _deterministic_order_path(self) -> bool:
        """True when the contract decides and no analyst call is made."""
        return self._execution_mode() == "deterministic"

    def _shadow_only_order_path(self) -> bool:
        """True when nothing may open a position this cycle.

        The research lanes are the measuring instrument; the order path is
        not. Holding the order path empty while no mechanism has earned it
        keeps the instrument running instead of spending the account - and
        the drawdown breaker - on a claim the evidence has already rejected.

        Open positions are unaffected: exchange stops and targets, the
        max_hold_hours force-close and every risk reduction path run exactly
        as before. Only discretionary opens and closes have no source.
        """
        return self._execution_mode() == "shadow_only"

    def _deterministic_decisions(self, snapshot: dict,
                                 max_new: int) -> list[dict]:
        """Trade the contract that was measured, with no analyst layer.

        A strategy earns promotion on evidence produced by its deterministic
        contract in a shadow lane. Running it live under an analyst would
        trade something other than the thing that earned the promotion, and
        the evidence would no longer describe what the account is doing. This
        path therefore uses the same proposals the lane used - the contract's
        own output on the same snapshot - and lets risk and execution apply
        unchanged.

        No LLM call happens at all, so there is nothing to journal as model
        input or output. The proposals are journalled as ``decisions`` like
        any other cycle, tagged with their source.
        """
        budget = max(0, int(max_new))
        if not budget:
            return []
        model = require_complete_contract(self.strategy_id)
        proposals = model.deterministic_proposals(snapshot, self.cfg)
        accepted = []
        for proposal in proposals:
            if len(accepted) >= budget:
                break
            if proposal.get("research_refusal_reason"):
                continue
            # The contract decides BEFORE the budget does. ``deterministic_
            # proposals`` emits a probe for every symbol and both directions -
            # 50 of them on a 25-symbol universe - and applies no contract at
            # all; the contract runs downstream in _prepare_setup_decision.
            # Capping the probes first would therefore spend the whole
            # new-position budget on the alphabetically first symbols
            # whatever their signal, and a strategy whose setups are anywhere
            # else in the universe would essentially never open.
            # build_setup_plan is pure, so asking it here costs a recomputation
            # and nothing else; the open path asks it again for real.
            if strategy.build_setup_plan(
                    proposal, snapshot.get(proposal.get("symbol")) or {},
                    self.cfg)[0] is None:
                continue
            entry = dict(proposal)
            entry["proposal_source"] = "deterministic_contract"
            # Confidence is not a model opinion here. The contract either
            # fired or it did not, so a fabricated score would let the
            # min_confidence gate look like it was doing work it is not.
            entry["confidence"] = 1.0
            accepted.append(entry)
        return accepted

    def _record_shadow_decisions(self, snapshot: dict) -> dict:
        """Journal what every registered contract would have done.

        This is the cheapest way to forward-test strategies that hold no
        capital: without it, evaluating N strategies takes N times as long as
        evaluating one, because each has to wait its turn at the account.
        With it, every registered contract accumulates genuine out-of-sample
        evidence from the same market data, at the same moments, starting the
        day it is registered.

        Recording the ACTIVE strategy too is deliberate and is the point that
        is easy to miss: comparing what the contract fired on against what
        the model actually took is the only direct measurement of what the
        LLM layer contributes. Offline research can bound that; it cannot
        observe it.

        Deterministic only. No orders, no LLM call, no position state. Never
        allowed to raise: shadow bookkeeping must not be able to interrupt
        trading.
        """
        symbols = [s for s in snapshot
                   if not s.startswith("_") and isinstance(snapshot[s], dict)]
        breadth_by_strategy = {}
        for strategy_id, builder in sorted(contracts.EVIDENCE_BUILDERS.items()):
            try:
                spec = registry.spec_for(strategy_id)
                shadow_cfg = self._shadow_cfg(spec)
                fired = []
                for symbol in symbols:
                    data = snapshot[symbol]
                    evidence = builder(data, shadow_cfg)
                    for setup in spec.setup_types:
                        contract = evidence.get(setup)
                        if not isinstance(contract, dict):
                            continue
                        for direction in ("long", "short"):
                            if contract.get(direction) is not True:
                                continue
                            extension = (evidence.get("extension_atr")
                                         or {}).get(direction)
                            fired.append({
                                "symbol": symbol,
                                "setup_type": setup,
                                "direction": direction,
                                "price": self._plain(data.get("price")),
                                "signal_ts": self._plain(data.get("signal_ts")),
                                "atr_1h_pct": self._plain(data.get("atr_1h_pct")),
                                "swing_low_pct": self._plain(
                                    data.get("swing_low_pct")),
                                "swing_high_pct": self._plain(
                                    data.get("swing_high_pct")),
                                "extension_atr": self._plain(extension),
                            })
                fired_symbols = {signal["symbol"] for signal in fired}
                breadth = {
                    "instruments_scanned": len(symbols),
                    "instruments_with_a_valid_setup": len(fired_symbols),
                    "setup_breadth_pct": (
                        round(len(fired_symbols) / len(symbols) * 100, 1)
                        if symbols else None),
                }
                breadth_by_strategy[spec.id] = breadth
                # One summary per strategy per cycle. Without the denominator
                # a count of firings cannot be turned into a rate.
                state.log_event(
                    "strategy_shadow_summary",
                    self._audit_json({
                        "instruments_scanned": breadth["instruments_scanned"],
                        "instruments_fired": breadth[
                            "instruments_with_a_valid_setup"],
                        "signals": len(fired),
                        "is_active": spec.id == str(
                            self.cfg["strategy"]["id"]),
                    }),
                    strategy_id=spec.id,
                    strategy_version=spec.version,
                )
                for signal in fired:
                    state.log_event(
                        "strategy_shadow_decision", self._audit_json(signal),
                        strategy_id=spec.id, strategy_version=spec.version)
            except Exception as exc:                       # noqa: BLE001
                log.warning("Shadow evaluation failed for %s: %s",
                            strategy_id, exc)
        return breadth_by_strategy

    def _record_observations(self, snapshot: dict) -> None:
        """Journal the enrichment fields and the per-cycle book state.

        Two events, both write-only for now, and deliberately so. Nothing in
        this repository reads them today; positioning/book-state conditioning
        cannot be tested for
        roughly three months because the sample does not exist yet. Waiting
        until the analysis is ready to write the collection would mean
        starting the three-month clock three months late.

        ``book_state`` is the one that matters most and costs the least. The
        depth and spread reading already happens at entry, but it is only
        journalled when it *rejects*, so every ordinary observation is thrown
        away - and the ordinary observations are the baseline against which a
        cascade's depth collapse and refill are measured.

        Never raises. Observation must not be able to interrupt trading.
        """
        symbols = [s for s in snapshot
                   if not s.startswith("_") and isinstance(snapshot[s], dict)]
        try:
            enrichment = {
                symbol: snapshot[symbol].get(brain.ENRICHMENT_KEY)
                for symbol in symbols
            }
            context = snapshot.get("_market_context") or {}
            state.log_event(
                "snapshot_enrichment",
                self._audit_json({
                    "market": context.get(brain.ENRICHMENT_KEY),
                    "symbols": enrichment,
                }),
            )
        except Exception as exc:                           # noqa: BLE001
            log.warning("Snapshot enrichment journalling failed: %s", exc)

        for symbol in symbols:
            try:
                enrichment = snapshot[symbol].get(brain.ENRICHMENT_KEY)
                enrichment = enrichment if isinstance(enrichment, dict) else {}
                if "book_observation_error" in enrichment:
                    observed = {
                        "symbol": symbol,
                        "mid": enrichment.get("book_mid"),
                        "best_bid": enrichment.get("book_best_bid"),
                        "best_ask": enrichment.get("book_best_ask"),
                        "spread_pct": enrichment.get("book_spread_pct"),
                        "bid_depth_usd": enrichment.get(
                            "book_bid_depth_usd"),
                        "ask_depth_usd": enrichment.get(
                            "book_ask_depth_usd"),
                        "top_bid_size": enrichment.get("book_top_bid_size"),
                        "top_ask_size": enrichment.get("book_top_ask_size"),
                        "band_pct": enrichment.get("book_band_pct"),
                        "book_ts": enrichment.get("book_ts"),
                        "error": enrichment.get("book_observation_error"),
                    }
                else:
                    observed = self.ex.book_state(symbol)
                state.log_event(
                    "book_state",
                    self._audit_json({
                        "signal_ts": self._plain(
                            snapshot[symbol].get("signal_ts")),
                        **observed,
                    }),
                )
            except Exception as exc:                       # noqa: BLE001
                log.warning("Book state journalling failed for %s: %s",
                            symbol, exc)

    def _attach_research_book_state(self, snapshot: dict) -> None:
        """Attach one hidden real-time book reading per visible symbol."""
        for symbol, row in snapshot.items():
            if symbol.startswith("_") or not isinstance(row, dict):
                continue
            try:
                observed = self.ex.book_state(
                    symbol,
                    max_age_seconds=float(
                        self.cfg["execution"]["max_market_data_age_seconds"]),
                )
            except Exception as exc:                       # noqa: BLE001
                observed = {
                    "mid": None, "best_bid": None, "best_ask": None,
                    "spread_pct": None, "bid_depth_usd": None,
                    "ask_depth_usd": None, "top_bid_size": None,
                    "top_ask_size": None, "bid_levels": [],
                    "ask_levels": [], "contract_size": None,
                    "band_pct": None, "book_ts": None,
                    "age_seconds": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            # A truncated ladder must say so. The observation error carries
            # both the exchange-side problem and any depth we could not keep,
            # so a silently shortened book cannot reach a contract as if it
            # were the full observed one.
            bid_levels, bid_cut = self._plain_levels(
                observed.get("bid_levels"))
            ask_levels, ask_cut = self._plain_levels(
                observed.get("ask_levels"))

            def _observed_len(raw) -> int | None:
                return len(raw) if isinstance(raw, (list, tuple)) else None

            # Coerced, not trusted: ``error`` reaches here from an exchange
            # response and must never turn an observation into an exception.
            notes = [
                str(observed.get("error") or ""),
                f"bid depth: {bid_cut}" if bid_cut else "",
                f"ask depth: {ask_cut}" if ask_cut else "",
            ]
            observation_error = " | ".join(n for n in notes if n) or None

            enrichment = dict(row.get(brain.ENRICHMENT_KEY) or {})
            enrichment.update({
                "book_mid": self._plain(observed.get("mid")),
                "book_best_bid": self._plain(observed.get("best_bid")),
                "book_best_ask": self._plain(observed.get("best_ask")),
                "book_spread_pct": self._plain(observed.get("spread_pct")),
                "book_bid_depth_usd": self._plain(
                    observed.get("bid_depth_usd")),
                "book_ask_depth_usd": self._plain(
                    observed.get("ask_depth_usd")),
                "book_top_bid_size": self._plain(
                    observed.get("top_bid_size")),
                "book_top_ask_size": self._plain(
                    observed.get("top_ask_size")),
                "book_bid_levels": bid_levels,
                "book_ask_levels": ask_levels,
                "book_bid_levels_observed": _observed_len(
                    observed.get("bid_levels")),
                "book_ask_levels_observed": _observed_len(
                    observed.get("ask_levels")),
                "book_contract_size": self._plain(
                    observed.get("contract_size")),
                "book_band_pct": self._plain(observed.get("band_pct")),
                "book_ts": self._plain(observed.get("book_ts")),
                "book_age_seconds": self._plain(
                    observed.get("age_seconds")),
                "book_observation_error": observation_error,
            })
            row[brain.ENRICHMENT_KEY] = enrichment

    def _adverse_r(self, pos: dict, st: dict) -> float | None:
        """How far the position has moved against entry, in units of its stop.

        The stop lives in ``st["protection"][symbol]`` - that is the price
        actually attached at the exchange, written by ``_record_open`` and
        kept current by the protection audit. ``active_trades`` carries the
        planned distance as ``stop_loss_pct`` but never an ``sl_price``, so
        it is the fallback for a position whose protection row has not been
        reconciled yet.

        Returns ``None`` when entry, stop or mark cannot be read, which the
        caller must treat as "unknown" rather than "not adverse".
        """
        symbol = pos.get("symbol")
        trade = (st.get("active_trades") or {}).get(symbol) or {}
        protection = (st.get("protection") or {}).get(symbol) or {}
        try:
            entry = float(
                trade.get("entry_price") or pos.get("entryPrice") or 0)
            mark = float(pos.get("markPrice") or 0)
            stop = float(protection.get("sl_price") or 0)
            stop_pct = float(trade.get("stop_loss_pct") or 0)
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(value) and value > 0
                   for value in (entry, mark)):
            return None
        if math.isfinite(stop) and stop > 0:
            risk_distance = abs(entry - stop)
        elif math.isfinite(stop_pct) and stop_pct > 0:
            risk_distance = entry * stop_pct / 100.0
        else:
            return None
        if risk_distance <= 0:
            return None
        adverse = (entry - mark) if self._direction(pos) == "long" else (
            mark - entry)
        return adverse / risk_distance

    def _too_young_to_close(self, pos: dict, decision: dict,
                            st: dict) -> bool:
        """Block a discretionary close only while the position is not losing.

        Exchange-side stops and targets are untouched, the max-hold timer
        still fires, and the engine's own safety closes do not pass through
        here - this only constrains the model's judgement calls.

        A ``risk_reduction`` close is always allowed: that trigger means the
        model is de-risking rather than second-guessing the entry, and a
        floor that blocks de-risking would be a safety regression.

        WHY THIS IS NO LONGER A PURE CLOCK. The 90-minute floor was set to
        protect a right tail - "the payoff depends on the ~20% of trades that
        reach a 3R target at a ~21h hold". Over 24 demo round trips that tail
        appeared 0 times, while the floor's cost showed up on every losing
        trade. One position was refused eleven consecutive times over 55
        minutes and closed for -250 USDT the moment the gate lifted.

        The result was an inverted exit policy: the model closed winners on
        momentum fade (average win +28.55) while losers were held past the
        floor into their exchange stop (average loss -204.75, and five stop
        closes carried 40% of the total loss). A 12.5% hit rate needs a
        win/loss ratio above ~7 to break even; the measured ratio was 0.14.

        So the floor keeps the job it can actually do - stop the model
        scratching a position that has not gone against it - and gives up the
        job it was doing badly. Once the position is beyond
        ``min_hold_adverse_r`` of its own planned stop distance, the thesis is
        measurably failing and the model may act immediately.

        When age or adverse excursion is unknown the close is ALLOWED.
        Refusing to close a position we cannot measure would trap it until
        the max-hold timer, which is the more dangerous failure.
        """
        floor_minutes = float(self.cfg["strategy"].get("min_hold_minutes", 0))
        if floor_minutes <= 0:
            return False
        if str(decision.get("close_trigger") or "") == "risk_reduction":
            return False
        symbol = pos.get("symbol")
        opened = (st.get("opened_at") or {}).get(symbol)
        try:
            opened = float(opened or 0)
        except (TypeError, ValueError):
            opened = 0.0
        if opened <= 0:
            return False
        held_minutes = (time.time() - opened) / 60.0
        if held_minutes >= floor_minutes:
            return False
        adverse_r = self._adverse_r(pos, st)
        release_r = float(
            self.cfg["strategy"].get("min_hold_adverse_r", 0.5))
        if adverse_r is None or adverse_r >= release_r:
            log.info(
                "Allowed early model close %s: held %.0f min but adverse "
                "excursion is %s (release at %.2fR)",
                symbol, held_minutes,
                "unknown" if adverse_r is None else f"{adverse_r:.2f}R",
                release_r)
            return False
        log.info(
            "Rejected model close %s: held %.0f min, minimum is %.0f min, "
            "adverse %.2fR below the %.2fR release (trigger=%s)",
            symbol, held_minutes, floor_minutes, adverse_r, release_r,
            decision.get("close_trigger"))
        state.log_event("rejected", json.dumps({
            "symbol": symbol,
            "action": "close",
            "why": "inside strategy.min_hold_minutes",
            "held_minutes": round(held_minutes, 1),
            "min_hold_minutes": floor_minutes,
            "adverse_r": round(adverse_r, 3),
            "min_hold_adverse_r": release_r,
            "close_trigger": decision.get("close_trigger"),
        }))
        return True

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

    def flatten_all(self, reason: str) -> bool:
        log.warning("FLATTEN ALL (close positions, then cancel orders): %s",
                    reason)
        try:
            st = state.load_state()
        except state.JournalError:
            raise
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
            except (CredentialError, ccxt.AuthenticationError,
                    state.JournalError, OrderSubmissionAmbiguousError):
                raise
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
                except (CredentialError, ccxt.AuthenticationError,
                        state.JournalError, OrderSubmissionAmbiguousError):
                    raise
                except Exception:
                    remaining = p
                closed = remaining is None
                if closed:
                    st.get("opened_at", {}).pop(p["symbol"], None)
                    st.get("active_trades", {}).pop(p["symbol"], None)
                    st.get("protection", {}).pop(p["symbol"], None)
                    try:
                        state.commit(st)
                    except state.JournalError:
                        raise
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
            except (CredentialError, ccxt.AuthenticationError,
                    state.JournalError, OrderSubmissionAmbiguousError):
                raise
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
            raise
        return not failed and orders_cleared
