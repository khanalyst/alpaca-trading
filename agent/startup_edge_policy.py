"""Startup authentication, session policy, and validated-edge controls."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlparse

from . import state
from .alpaca_provider import AlpacaError
from .alpaca_session import as_new_york
from .execution_lifecycle import _value
from .instruments import validate_asset_class, validate_instrument


class StartupEdgePolicyMixin:
    def preflight(self) -> dict[str, Any]:
        """Authenticate and validate the configured endpoint before orders."""
        if self._preflight is not None:
            return self._preflight
        if not self._state_ready:
            state.ensure_ready()
            self._state_ready = True
        if bool(getattr(self.provider, "paper", False)) is not (self.mode == "paper"):
            raise AlpacaError(f"provider is not {self.mode} scoped")
        session = getattr(self.provider, "session", None)
        api_key = getattr(session, "api_key", None)
        secret_key = getattr(session, "secret_key", None)
        if not api_key or not secret_key:
            raise AlpacaError(f"authenticated {self.mode} credentials are required before trading")
        endpoint = getattr(self.provider, "endpoint", None) or getattr(session, "endpoint", None)
        endpoint_text = str(endpoint or "")
        expected_host = ("paper-api.alpaca.markets" if self.mode == "paper"
                         else "api.alpaca.markets")
        if endpoint is not None:
            parsed_endpoint = urlparse(endpoint_text)
            if (parsed_endpoint.scheme.lower() != "https" or
                    parsed_endpoint.hostname != expected_host or
                    parsed_endpoint.port is not None or
                    parsed_endpoint.username is not None or
                    parsed_endpoint.password is not None):
                raise AlpacaError(f"{self.mode} endpoint validation failed")
        data_feed = str(getattr(self.provider, "data_feed", "iex")).lower()
        options_feed = str(getattr(self.provider, "options_feed", "indicative")).lower()
        if data_feed not in {"iex", "sip", "delayed_sip"}:
            raise AlpacaError(f"unsupported equity feed {data_feed!r}")
        if options_feed not in {"indicative", "opra"}:
            raise AlpacaError(f"unsupported options feed {options_feed!r}")
        assets_method = getattr(self.provider, "assets", None)
        assets_supported = callable(assets_method)
        try:
            account = self.provider.account()
            clock = self.provider.clock()
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
                       not bool(_value(self._assets[symbol], "tradable", False)) or
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
                           "endpoint": endpoint or expected_host,
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
        if runtime.get("state") == state.KILLED or runtime.get("operator_pause") is True:
            self._preflight_error = runtime.get("kill_reason") or "operator_pause"
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
        now = _value(clock, "timestamp")
        session = self.market.session(now)
        return bool(_value(clock, "is_open", False) and session is not None and
                    session.open <= now < session.close)

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
        research = self._edge_base_cfg.get("research", {})
        if not isinstance(research, Mapping) or not research.get("enabled", False):
            return True
        try:
            from .edge import (apply_variant, resolve_validated_variant,
                               resolve_validated_variants)
            lookup = deepcopy(self._edge_base_cfg)
            if self._edge_requested_variant:
                lookup.setdefault("strategy", {})["variant_id"] = self._edge_requested_variant
            else:
                lookup.setdefault("strategy", {}).pop("variant_id", None)
            if self.mode == "live":
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
                        state.write_heartbeat(
                            "paused", run_id=self.run_id,
                            reason="validated_edge_required",
                            edge_vehicle=lookup.get("strategy", {}).get(
                                "execution_mode", "shares"))
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
                            runtime.get("operator_pause") is not True):
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
