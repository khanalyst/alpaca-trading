"""Startup authentication, session policy, and validated-edge controls."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from typing import Any, Mapping
from urllib.parse import urlparse

from . import state
from .alpaca_provider import AlpacaError
from .alpaca_session import as_new_york
from .execution_lifecycle import _value
from .instruments import validate_asset_class, validate_instrument
from .paper_trial import (REPLACEMENT_LOCAL_BOOK_ERROR,
                          replacement_local_book_is_flat)


class StartupEdgePolicyMixin:
    def _paper_selection_status(self) -> dict[str, Any]:
        """Describe paper selection without implying execution or broker state.

        ``armed`` is the operator/runtime authorization posture.  A process may
        therefore be armed while it waits for the first passing proof; durable
        pause, kill, daily-risk, shutdown, and resolution faults all fail it
        closed.  Ordinarily ``resolved`` names only a durable proved edge; the
        explicit paper-trial lane instead publishes its frozen identity with
        ``proof: null`` and non-authorizing flags.
        """
        base_cfg = (self._edge_base_cfg
                    if isinstance(getattr(self, "_edge_base_cfg", None), Mapping)
                    else self.cfg if isinstance(self.cfg, Mapping) else {})
        strategy = (base_cfg.get("strategy", {})
                    if isinstance(base_cfg.get("strategy"), Mapping) else {})
        configured_strategy = str(strategy.get("id") or "").strip() or None
        selection_mode = str(
            getattr(self, "_edge_selection_mode", None) or
            strategy.get("selection_mode") or "specific")
        requested_variant = str(
            getattr(self, "_edge_requested_variant", None) or
            strategy.get("variant_id") or "").strip() or None
        trial_runtime = getattr(self, "_paper_trial_runtime", None)
        if trial_runtime is not None:
            configured_strategy = "rule"
            requested_variant = str(
                trial_runtime.descriptor.get("variant_id") or "").strip() or None

        edge_error = str(getattr(self, "_edge_error", None) or "").strip()
        records = [record for record in
                   (getattr(self, "_edge_records", None) or ())
                   if isinstance(record, Mapping)]
        record = (getattr(self, "_edge_record", None)
                  if not edge_error and len(records) == 1 else None)
        resolved = None
        if (isinstance(record, Mapping) and
                record.get("candidate_id") == records[0].get("candidate_id")):
            proof = record.get("latest_proof")
            gate = (proof.get("verified_gate")
                    if isinstance(proof, Mapping) and
                    isinstance(proof.get("verified_gate"), Mapping) else {})
            candidate_id = str(record.get("candidate_id") or "").strip()
            variant_id = str(record.get("variant_id") or "").strip()
            record_strategy = str(record.get("strategy_id") or "").strip()
            record_hash = str(record.get("config_hash") or "").strip()
            proof_hash = (str(proof.get("config_hash") or "").strip()
                          if isinstance(proof, Mapping) else "")
            run_id = (str(proof.get("run_id") or "").strip()
                      if isinstance(proof, Mapping) else "")
            gate_hash = (str(proof.get("gate_hash") or "").strip()
                         if isinstance(proof, Mapping) else "")
            lane = (str(proof.get("lane") or "").strip().lower()
                    if isinstance(proof, Mapping) else "")
            verified = bool(
                candidate_id and variant_id and run_id and gate_hash and
                record_hash and proof_hash == record_hash and lane == "shadow" and
                gate.get("passes") is True and
                (configured_strategy is None or
                 record_strategy == configured_strategy))
            if verified:
                config = (record.get("config")
                          if isinstance(record.get("config"), Mapping) else {})
                record_strategy_cfg = (
                    config.get("strategy", {})
                    if isinstance(config.get("strategy"), Mapping) else {})
                rule_spec = (
                    record_strategy_cfg.get("rule_spec", {})
                    if isinstance(record_strategy_cfg.get("rule_spec"), Mapping)
                    else {})
                axes = (record.get("axes")
                        if isinstance(record.get("axes"), Mapping) else {})
                family = str(
                    rule_spec.get("family") or axes.get("family") or
                    record_strategy or "").strip() or None
                resolved = {
                    "candidate_id": candidate_id,
                    "variant_id": variant_id,
                    "family": family,
                    "proof": {
                        "run_id": run_id,
                        "gate_hash": gate_hash,
                        "config_hash": proof_hash,
                        "lane": lane,
                    },
                }

        runtime_error = False
        try:
            runtime = state.load_state()
        except Exception:  # noqa: BLE001 - observability must fail closed
            runtime = {}
            runtime_error = True
        risk_day = runtime.get("risk_day", {}) if isinstance(runtime, Mapping) else {}
        if not isinstance(runtime, Mapping) or not isinstance(risk_day, Mapping):
            runtime_error = True
        risk_limit = risk_day.get("limit_hit") if isinstance(risk_day, Mapping) else None
        if risk_limit is not None and not isinstance(risk_limit, bool):
            runtime_error = True

        runtime_state = runtime.get("state") if isinstance(runtime, Mapping) else None
        kill_reason = runtime.get("kill_reason") if isinstance(runtime, Mapping) else None
        operator_pause = (runtime.get("operator_pause")
                          if isinstance(runtime, Mapping) else None)
        shutdown_reason = str(
            getattr(self, "shutdown_reason", None) or "").strip()
        edge_required = bool(getattr(self, "_edge_required", False))
        selection_ambiguous = len(records) > 1
        waiting_for_proof = bool(
            edge_required and resolved is None and not selection_ambiguous and
            (not edge_error or
             edge_error.startswith("no latest-passing validated edge")))
        edge_fault = bool(
            edge_required and resolved is None and not waiting_for_proof)
        trial_status = (trial_runtime.status(runtime.get("paper_trial"))
                        if trial_runtime is not None and isinstance(runtime, Mapping)
                        else None)
        if isinstance(trial_status, Mapping) and all(
                trial_status.get(key) for key in (
                    "candidate_id", "variant_id", "incumbent_identity")):
            resolved = {
                "candidate_id": trial_status["candidate_id"],
                "variant_id": trial_status["variant_id"],
                "family": trial_status.get("family"),
                "incumbent_identity": trial_status["incumbent_identity"],
                "proof": None,
                "authorizing": False,
                "proof_authority": False,
            }
        trial_blocker = None
        if (isinstance(trial_status, Mapping) and
                trial_status.get("entry_eligible") is not True):
            trial_blockers = trial_status.get("blockers")
            if isinstance(trial_blockers, list) and trial_blockers:
                trial_blocker = str(trial_blockers[0])
            else:
                trial_blocker = "paper_trial_blocked"

        if runtime_error:
            blocker = "runtime_state_unavailable"
        elif (runtime_state == state.KILLED or
              kill_reason not in (None, False, "")):
            blocker = "runtime_killed"
        elif shutdown_reason:
            blocker = "shutdown_requested"
        elif runtime_state == state.DAY_STOPPED or risk_limit is True:
            blocker = "daily_risk_limit"
        elif operator_pause is not False:
            blocker = "operator_paused"
        elif selection_ambiguous:
            blocker = "selection_not_singular"
        elif edge_fault:
            blocker = "edge_resolution_failed"
        elif waiting_for_proof:
            blocker = "validated_edge_required"
        elif trial_blocker:
            blocker = trial_blocker
        else:
            blocker = None

        armed = (blocker in {None, "validated_edge_required"}
                 if trial_runtime is None else blocker is None)
        if trial_runtime is not None:
            selection_state = "paper_trial" if armed else "blocked"
        elif armed and blocker == "validated_edge_required":
            selection_state = "waiting_for_proof"
        elif armed:
            selection_state = "ready"
        else:
            selection_state = "blocked"
        result = {
            "configured_strategy": configured_strategy,
            "selection_mode": selection_mode,
            "requested_variant": requested_variant,
            "resolved": resolved,
            "blocker_code": blocker,
            "armed": armed,
            "state": selection_state,
        }
        if trial_status is not None:
            result["paper_trial"] = trial_status
        return result

    @staticmethod
    def _feed_name(value: Any, *, default: str) -> str:
        """Return a provider/config feed's canonical comparison name.

        Alpaca's SDK may expose enum values while injected providers usually
        expose strings.  Startup authorization compares the two at the
        semantic boundary, so normalize both representations here.
        """
        value = getattr(value, "value", value)
        return str(default if value is None else value).strip().lower().replace("-", "_")

    def _feed_policy(self) -> tuple[str | None, str | None, bool, bool]:
        """Resolve configured feeds and the lanes that require entitlement.

        A validated config normally carries feeds under ``broker``.  The
        legacy ``data`` aliases remain accepted for direct Engine callers.
        ``None`` means no feed was explicitly configured; in that case only
        an enabled research/option lane supplies a fail-closed requirement.
        """
        cfg = self.cfg if isinstance(self.cfg, Mapping) else {}
        broker = cfg.get("broker") if isinstance(cfg.get("broker"), Mapping) else {}
        data = cfg.get("data") if isinstance(cfg.get("data"), Mapping) else {}
        configured_data = (broker.get("data_feed") if "data_feed" in broker
                           else data.get("feed") if "feed" in data else None)
        configured_options = (broker.get("options_feed") if "options_feed" in broker
                              else data.get("options_feed")
                              if "options_feed" in data else None)
        configured_data = (self._feed_name(configured_data, default="")
                           if configured_data is not None else None)
        configured_options = (self._feed_name(configured_options, default="")
                              if configured_options is not None else None)

        research = cfg.get("research") if isinstance(cfg.get("research"), Mapping) else {}
        research_enabled = research.get("enabled") is True
        validated_equity = any(
            str(record.get("vehicle", "")).lower() in {"equity", "shares"}
            for record in (getattr(self, "_edge_records", None) or ())
            if isinstance(record, Mapping)) or str(
                (getattr(self, "_edge_record", None) or {}).get(
                    "vehicle", "")).lower() in {"equity", "shares"}
        strategy = cfg.get("strategy") if isinstance(cfg.get("strategy"), Mapping) else {}
        option_execution = str(strategy.get("execution_mode", "shares")).lower() \
            in {"option", "options"}
        universe = cfg.get("universe") if isinstance(cfg.get("universe"), Mapping) else {}
        classes = universe.get("asset_classes", [])
        option_research = research_enabled and isinstance(classes, (list, tuple)) and any(
            str(item).lower() in {"us_option", "option", "options"}
            for item in classes)
        # A validated edge can promote an option vehicle even when a direct
        # caller omitted the corresponding universe class.
        option_research = option_research or (
            research_enabled and
            str((getattr(self, "_edge_record", None) or {}).get("vehicle", "")).lower() in
            {"option", "options"})
        return configured_data, configured_options, (
            research_enabled or validated_equity), (
            option_execution or option_research)

    def _authorize_feeds(self, *, data_feed: str, options_feed: str,
                         data_feed_missing: bool,
                         options_feed_missing: bool) -> None:
        """Enforce provider/config feed identity before trading can start."""
        configured_data, configured_options, equity_lane, option_lane = \
            self._feed_policy()

        # Explicit config is authoritative even for diagnostics.  This closes
        # the injected-provider path where a provider silently reports a
        # different stream than the operator configured.
        if configured_data is not None and configured_data not in {
                "iex", "sip", "delayed_sip"}:
            raise AlpacaError(f"unsupported configured equity feed {configured_data!r}")
        if configured_options is not None and configured_options not in {
                "indicative", "opra"}:
            raise AlpacaError(f"unsupported configured options feed {configured_options!r}")
        if configured_data is not None and data_feed != configured_data:
            raise AlpacaError(
                "provider equity feed does not match configured feed "
                f"{configured_data!r} (provider={data_feed!r})")
        if configured_options is not None and options_feed != configured_options:
            raise AlpacaError(
                "provider options feed does not match configured feed "
                f"{configured_options!r} (provider={options_feed!r})")

        # Research and validated-equity lanes use the exact configured
        # real-time equity view.  The shipped/default identity is IEX, while
        # SIP is valid only when it is configured and reported consistently.
        # Delayed SIP remains diagnostic-only.  Missing provider metadata is
        # not evidence of the configured identity.
        if equity_lane:
            expected_data = configured_data or "iex"
            if expected_data not in {"iex", "sip"}:
                raise AlpacaError(
                    "research/validated-equity runtime requires a configured "
                    "real-time equity feed (iex or sip); delayed_sip is "
                    "diagnostic-only")
            if data_feed_missing:
                raise AlpacaError(
                    "provider equity feed metadata is unavailable; "
                    f"research/validated-equity runtime requires configured "
                    f"feed {expected_data!r}")
            if data_feed != expected_data:
                raise AlpacaError(
                    "research/validated-equity runtime requires the exact "
                    f"configured equity feed {expected_data!r} "
                    f"(provider={data_feed!r})")
        if option_lane:
            if options_feed_missing:
                raise AlpacaError(
                    "provider options feed metadata is unavailable; "
                    "option execution/research requires the OPRA feed entitlement")
            if options_feed != "opra":
                raise AlpacaError(
                    "option execution/research requires the OPRA feed "
                    f"entitlement (provider={options_feed!r})")

    def preflight(self) -> dict[str, Any]:
        """Authenticate and validate the configured endpoint before orders."""
        if self._preflight is not None:
            return self._preflight
        if not self._state_ready:
            state.ensure_ready()
            self._state_ready = True
        provider_paper = getattr(self.provider, "paper", None)
        if (not isinstance(provider_paper, bool) or
                provider_paper is not (self.mode == "paper")):
            raise AlpacaError(f"provider is not {self.mode} scoped")
        session = getattr(self.provider, "session", None)
        api_key = getattr(session, "api_key", None)
        secret_key = getattr(session, "secret_key", None)
        if not api_key or not secret_key:
            raise AlpacaError(f"authenticated {self.mode} credentials are required before trading")
        endpoint = getattr(self.provider, "endpoint", None)
        if endpoint is None:
            endpoint = getattr(session, "endpoint", None)
        endpoint_text = str(endpoint or "").strip()
        expected_host = ("paper-api.alpaca.markets" if self.mode == "paper"
                         else "api.alpaca.markets")
        if not endpoint_text:
            raise AlpacaError(f"{self.mode} endpoint validation failed")
        parsed_endpoint = urlparse(endpoint_text)
        if (parsed_endpoint.scheme.lower() != "https" or
                parsed_endpoint.hostname != expected_host or
                parsed_endpoint.port is not None or
                parsed_endpoint.username is not None or
                parsed_endpoint.password is not None):
            raise AlpacaError(f"{self.mode} endpoint validation failed")
        provider_data_value = getattr(self.provider, "data_feed", None)
        provider_options_value = getattr(self.provider, "options_feed", None)
        data_feed_missing = provider_data_value is None or not str(
            getattr(provider_data_value, "value", provider_data_value)).strip()
        options_feed_missing = provider_options_value is None or not str(
            getattr(provider_options_value, "value", provider_options_value)).strip()
        data_feed = self._feed_name(provider_data_value, default="iex")
        options_feed = self._feed_name(provider_options_value, default="indicative")
        if data_feed not in {"iex", "sip", "delayed_sip"}:
            raise AlpacaError(f"unsupported equity feed {data_feed!r}")
        if options_feed not in {"indicative", "opra"}:
            raise AlpacaError(f"unsupported options feed {options_feed!r}")
        assets_method = getattr(self.provider, "assets", None)
        assets_supported = callable(assets_method)
        try:
            account = self.provider.account()
            clock = self.provider.clock()
            if not isinstance(_value(clock, "is_open", None), bool):
                raise ValueError("broker clock is_open must be true or false")
            if self._timestamp(_value(clock, "timestamp", None)) is None:
                raise ValueError("broker clock timestamp is invalid")
            self.market.refresh_calendar()
            assets = assets_method() if assets_supported else []
        except Exception as exc:  # noqa: BLE001
            raise AlpacaError(f"authenticated {self.mode} preflight failed: {exc}") from exc
        account_id = _value(account, "id")
        if not account_id:
            raise AlpacaError(f"{self.mode} account identity is unavailable")
        if str(_value(account, "status", "")).lower() not in {"active", "account_status.active"}:
            raise AlpacaError(f"{self.mode} account is not active")
        if (self._number(_value(account, "equity", None)) or 0) <= 0:
            raise AlpacaError(f"{self.mode} account equity is unavailable")
        if self.mode == "live" and _value(account, "pattern_day_trader", None) is not True:
            raise AlpacaError(
                "live account must explicitly report pattern_day_trader=true")
        self._authorize_feeds(
            data_feed=data_feed, options_feed=options_feed,
            data_feed_missing=data_feed_missing,
            options_feed_missing=options_feed_missing)
        if assets_supported:
            self._assets = {}
            for asset in assets:
                symbol = validate_instrument(
                    _value(asset, "symbol", ""),
                    validate_asset_class(_value(asset, "asset_class",
                                                _value(asset, "class", ""))))
                self._assets[symbol] = asset
            invalid = [symbol for symbol in self._universe()
                       if symbol not in self._assets or
                       _value(self._assets[symbol], "tradable", False) is not True or
                       str(_value(self._assets[symbol], "status", "")).lower()
                       not in {"active", "asset_status.active"}]
            if invalid:
                raise AlpacaError(
                    "configured symbols are inactive or not tradable: " +
                    ", ".join(sorted(invalid)))
        fingerprint = state.account_fingerprint(
            self.mode, f"{api_key}\0{account_id}")
        state.bind_account_identity(fingerprint)
        self._runtime_state = state.load_state()
        self._preflight = {"account": account, "clock": clock,
                           "endpoint": endpoint_text,
                           "data_feed": data_feed, "options_feed": options_feed,
                           "account_fingerprint": fingerprint}
        self._preflight_error = None
        try:
            state.commit({"runtime_mode": self.mode, "account_fingerprint": fingerprint,
                          "preflight": {"endpoint": self._preflight["endpoint"],
                                         "data_feed": data_feed,
                                         "options_feed": options_feed,
                                         "authenticated": True}})
            state.log_equity(_value(account, "equity", None), "starting",
                             runtime_mode=self.mode, account_fingerprint=fingerprint,
                             run_id=self.run_id)
        except Exception as exc:  # noqa: BLE001
            self._state_ready = False
            raise AlpacaError(f"{self.mode} journal preflight write failed: {exc}") from exc
        return self._preflight

    def _ensure_order_ready(self) -> bool:
        if not self._state_ready:
            return False
        try:
            state.check_journal()
        except Exception as exc:  # noqa: BLE001
            self._state_ready = False
            self._preflight_error = f"{self.mode} journal unavailable: {exc}"
            return False
        runtime = state.load_state()
        if (runtime.get("state") == state.KILLED or
                runtime.get("operator_pause") is not False):
            self._preflight_error = runtime.get("kill_reason") or "operator_pause"
            return False
        trial_runtime = getattr(self, "_paper_trial_runtime", None)
        if (trial_runtime is not None and
                trial_runtime.pending_replacement and
                not replacement_local_book_is_flat(runtime)):
            self._preflight_error = REPLACEMENT_LOCAL_BOOK_ERROR
            self._edge_error = REPLACEMENT_LOCAL_BOOK_ERROR
            return False
        try:
            self.preflight()
        except Exception as exc:  # noqa: BLE001
            self._preflight_error = str(exc)
            self._event("cycle_blocked", {"reason": "preflight_failed", "error": str(exc)})
            return False
        if not self._reconciled:
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001
                self._preflight_error = f"startup reconciliation failed: {exc}"
                self._event("cycle_blocked", {"reason": "reconciliation_failed", "error": str(exc)})
                return False
        if (trial_runtime is not None and
                trial_runtime.pending_replacement and
                not self._refresh_edge()):
            self._preflight_error = (
                "paper trial replacement cannot manage old exposure; restart "
                "with the frozen incumbent until broker and local books are flat")
            # Force a fresh broker snapshot on the next attempt so a newly
            # flattened old book can complete the replacement safely.
            self._reconciled = False
            return False
        if not self._startup_cleanup_checked:
            try:
                self._enforce_intraday_cleanup(
                    self.market.clock(), reason="startup_reconciliation",
                    force=True)
            except Exception as exc:  # noqa: BLE001
                self._preflight_error = str(exc)
                return False
        return True

    def _inside_regular_session(self, clock: Any) -> bool:
        if _value(clock, "is_open", None) is not True:
            return False
        now = self._timestamp(_value(clock, "timestamp", None))
        if now is None:
            return False
        session = self.market.session(now)
        return bool(session is not None and session.open <= now < session.close)

    def _enforce_intraday_cleanup(self, clock: Any, *, reason: str,
                                  force: bool = False) -> bool:
        """Cancel working orders and flatten startup/out-of-session exposure."""
        if self._inside_regular_session(clock) and not force:
            self._startup_cleanup_checked = True
            return True
        cleanup_error = None
        try:
            complete = self.flatten_all(reason)
        except Exception as exc:  # noqa: BLE001
            complete = False
            cleanup_error = exc
        self._startup_cleanup_checked = complete
        if complete:
            return True
        try:
            state.commit({"operator_pause": True},
                         transition=(state.RUNNING, state.PAUSED))
            state.write_heartbeat("degraded", run_id=self.run_id,
                                  reason="intraday_cleanup_incomplete")
        except Exception:  # noqa: BLE001
            pass
        detail = f": {cleanup_error}" if cleanup_error is not None else ""
        raise AlpacaError(
            f"outside-session cleanup incomplete; entries remain blocked{detail}")

    def _latest_entry_allowed(self, now: datetime, cfg: Mapping | None = None) -> bool:
        strategy = (cfg or self.cfg).get("strategy", {})
        raw = str(strategy.get("latest_entry_time", "15:00"))
        try:
            if len(raw) != 5 or raw[2] != ":":
                return False
            cutoff = datetime.strptime(raw, "%H:%M").time()
        except (TypeError, ValueError):
            return False
        local = as_new_york(now)
        return local.time().replace(second=0, microsecond=0) <= cutoff

    def _refresh_edge(self) -> bool:
        """Refresh paper proofs or re-verify the one pinned live candidate."""
        trial_runtime = getattr(self, "_paper_trial_runtime", None)
        if trial_runtime is not None:
            try:
                snapshot = getattr(self, "_last_reconcile_snapshot", None)
                runtime_before = state.load_state()
                replacement_audit = trial_runtime.replacement_audit(
                    runtime_before, snapshot)
                if replacement_audit is not None:
                    # Preserve the completed incumbent and stopping rationale
                    # in the existing account-scoped journal before replacing
                    # its single mutable runtime slot.  A failed audit write
                    # leaves the old terminal state intact and blocks rollover.
                    state.log_event(
                        "paper_trial_terminal_snapshot",
                        json.dumps(replacement_audit, sort_keys=True,
                                   allow_nan=False, default=str),
                        run_id=self.run_id, runtime_mode=self.mode)
                self._runtime_state = state.update_state(
                    lambda runtime: trial_runtime.update_runtime(
                        runtime, broker_snapshot=snapshot))
                status = trial_runtime.status(
                    self._runtime_state.get("paper_trial"))
                if status.get("entry_eligible") is True:
                    self.cfg = trial_runtime.config
                    self._edge_record = None
                    self._edge_records = []
                    self._edge_configs = [(None, self.cfg)]
                    self._edge_error = None
                    try:
                        def resume(runtime: dict) -> dict:
                            risk_day = runtime.get("risk_day")
                            hard_stop = bool(
                                runtime.get("state") in {
                                    state.KILLED, state.DAY_STOPPED} or
                                runtime.get("kill_reason") not in (
                                    None, False, "") or
                                (isinstance(risk_day, Mapping) and
                                 risk_day.get("limit_hit") is True))
                            if (self.running and not hard_stop and
                                    runtime.get("state") == state.PAUSED and
                                    runtime.get("operator_pause") is False):
                                runtime["state"] = state.RUNNING
                            return runtime
                        self._runtime_state = state.update_state(resume)
                    except Exception:  # noqa: BLE001
                        pass
                    return True
                self._edge_record = None
                self._edge_records = []
                self._edge_configs = []
                blockers = status.get("blockers")
                self._edge_error = (str(blockers[0]) if isinstance(blockers, list)
                                    and blockers else "paper trial is paused")
                try:
                    def pause_trial(runtime: dict) -> dict:
                        risk_day = runtime.get("risk_day")
                        hard_stop = bool(
                            runtime.get("state") in {state.KILLED, state.DAY_STOPPED} or
                            runtime.get("kill_reason") not in (None, False, "") or
                            (isinstance(risk_day, Mapping) and
                             risk_day.get("limit_hit") is True))
                        if not hard_stop:
                            runtime["state"] = state.PAUSED
                        return runtime
                    self._runtime_state = state.update_state(pause_trial)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    state.write_heartbeat(
                        "paused", run_id=self.run_id,
                        reason=self._edge_error,
                        paper_selection=self._paper_selection_status())
                except Exception:  # noqa: BLE001
                    pass
                return False
            except Exception as exc:  # noqa: BLE001
                trial_runtime.error = f"paper_trial_refresh_failed:{exc}"
                self._edge_record = None
                self._edge_records = []
                self._edge_configs = []
                self._edge_error = str(exc)
                self._event("paper_trial_refresh_failed", {"error": str(exc)})
                return False
        research = self._edge_base_cfg.get("research", {})
        if not isinstance(research, Mapping) or not research.get("enabled", False):
            return True
        try:
            from .edge import (apply_variant, resolve_pinned_variants,
                               resolve_validated_variant,
                               resolve_validated_variants)
            lookup = deepcopy(self._edge_base_cfg)
            if self._edge_requested_variant:
                lookup.setdefault("strategy", {})["variant_id"] = self._edge_requested_variant
            else:
                lookup.setdefault("strategy", {}).pop("variant_id", None)
            if self.mode == "live" and self._edge_selection_mode == "pinned":
                # Re-resolve the operator's promotion, then insist that both
                # immutable identities are unchanged.  A pin that goes stale
                # must stop trading; it must never fall through to a
                # competing champion or a newly selected record.
                pinned_records = resolve_pinned_variants(
                    lookup, db_path=self._edge_db_path or None)
                record = (pinned_records[0]
                          if len(pinned_records) == 1 and
                          str(pinned_records[0].get("candidate_id") or "") ==
                          self._edge_pinned_candidate_id and
                          str(pinned_records[0].get("config_hash") or "") ==
                          self._edge_pinned_config_hash else None)
                records = [record] if record is not None else []
            elif self.mode == "live":
                record = resolve_validated_variant(
                    lookup, db_path=self._edge_db_path or None,
                    candidate_id=self._edge_pinned_candidate_id)
                if (record is not None and
                        str(record.get("config_hash") or "") !=
                        self._edge_pinned_config_hash):
                    record = None
                records = [record] if record is not None else []
            elif self._edge_selection_mode == "specific":
                record = resolve_validated_variant(
                    lookup, db_path=self._edge_db_path or None)
                records = [record] if record is not None else []
            else:
                records = resolve_validated_variants(
                    lookup, db_path=self._edge_db_path or None)
            if not records:
                self._edge_record = None
                self._edge_records = []
                self._edge_configs = ([] if self._edge_required else
                                      [(None, self._edge_base_cfg)])
                self._edge_error = "no latest-passing validated edge for " + str(
                    lookup.get("strategy", {}).get("execution_mode", "shares"))
                if self._edge_required:
                    try:
                        state.update_state(lambda runtime: {
                            **runtime, "state": state.PAUSED})
                        detail = {
                            "run_id": self.run_id,
                            "reason": "validated_edge_required",
                            "edge_vehicle": lookup.get("strategy", {}).get(
                                "execution_mode", "shares"),
                        }
                        if self.mode == "paper":
                            detail["paper_selection"] = \
                                self._paper_selection_status()
                        state.write_heartbeat(
                            "paused", **detail)
                    except Exception:  # noqa: BLE001
                        pass
                return not self._edge_required
            prior_ids = [record.get("candidate_id") for record in self._edge_records]
            next_ids = [record.get("candidate_id") for record in records]
            if self.mode == "live" and prior_ids and prior_ids != next_ids:
                self._edge_error = "pinned live edge changed identity"
                return False
            if prior_ids != next_ids:
                self._event("edge_selected", {
                    "candidate_ids": next_ids,
                    "variant_ids": [record.get("variant_id") for record in records],
                    "vehicle": records[0].get("vehicle"),
                })
            self._edge_records = records
            self._edge_record = records[0]
            if self.mode == "live":
                if self._edge_pinned_runtime_cfg is None:
                    self._edge_error = "pinned live strategy config is unavailable"
                    return False
                self._edge_configs = [
                    (records[0], deepcopy(self._edge_pinned_runtime_cfg))]
            else:
                self._edge_configs = [
                    (record, apply_variant(self._edge_base_cfg, record))
                    for record in records]
            if len(self._edge_configs) == 1:
                self.cfg = self._edge_configs[0][1]
            self._edge_error = None
            try:
                def resume(runtime: dict) -> dict:
                    if (self.running and runtime.get("state") == state.PAUSED and
                            runtime.get("operator_pause") is False):
                        runtime["state"] = state.RUNNING
                    return runtime
                state.update_state(resume)
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception as exc:  # noqa: BLE001
            self._edge_record = None
            self._edge_records = []
            self._edge_configs = []
            self._edge_error = f"edge resolution failed: {exc}"
            self._event("edge_resolution_failed", {"error": str(exc)})
            return not self._edge_required
