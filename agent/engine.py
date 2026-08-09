"""Deterministic, mode-scoped orchestration for proved strategies."""

from __future__ import annotations

import json
import hashlib
import logging
import time
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from . import state
from .alpaca_domain import OrderRequest
from .alpaca_provider import AlpacaError, AlpacaProvider
from .alpaca_session import SessionPolicy, trading_env_guard
from .brain import DecisionBrain
from .contracts.ibr import generate_ibr_signal
from .contracts.rule import generate_rule_signal
from .market import MarketData
from .risk import RiskEngine
from .strategy import build_setup_plan
from .instruments import (validate_asset_class, validate_equity_symbol,
                          validate_instrument)
from .execution_lifecycle import (
    ExecutionLifecycleMixin,
    _FILLED_ORDER_STATUSES,
    _TERMINAL_ORDER_STATUSES,
    _plain,
    _value,
)
from .runtime_control import RuntimeControlMixin
from .startup_edge_policy import StartupEdgePolicyMixin
from .market_entry_risk import MarketEntryRiskMixin
from .engine_cycle import EngineCycleMixin

log = logging.getLogger("engine")


class Engine(ExecutionLifecycleMixin, RuntimeControlMixin, StartupEdgePolicyMixin,
             MarketEntryRiskMixin, EngineCycleMixin):
    def __init__(self, cfg: dict, light: bool = False, *, provider=None,
                 market_data: MarketData | None = None, brain=None):
        self.cfg = cfg
        broker = cfg.get("broker", {}) if isinstance(cfg, Mapping) else {}
        self.mode = str(cfg.get("mode", "paper")).lower()
        if self.mode == "demo":
            self.mode = "paper"
        if self.mode not in {"paper", "live"}:
            raise AlpacaError("mode must be paper or live")
        expected_paper = self.mode == "paper"
        paper = broker.get("paper", expected_paper)
        allow_live = broker.get("allow_live", False)
        if (self.mode == "paper" and (paper is not True or allow_live is not False)):
            raise AlpacaError("paper mode requires paper=true and allow_live=false")
        if (self.mode == "live" and (paper is not False or allow_live is not True)):
            raise AlpacaError("live mode requires paper=false and allow_live=true")
        try:
            trading_env_guard(paper=paper, allow_live=allow_live)
        except ValueError as exc:
            raise AlpacaError(str(exc)) from exc
        strategy_cfg = cfg.get("strategy", {}) if isinstance(cfg.get("strategy"), Mapping) else {}
        self._edge_selection_mode = str(
            strategy_cfg.get("selection_mode") or
            ("specific" if self.mode == "live" else "all_proved"))
        if self.mode == "live":
            requested = str(strategy_cfg.get("variant_id") or "").strip()
            if (not requested or requested.lower() == "auto" or
                    self._edge_selection_mode != "specific"):
                raise AlpacaError("live mode requires one named specific strategy variant")
        research_cfg = cfg.get("research", {}) if isinstance(cfg, Mapping) else {}
        if self.mode == "live" and (
                not isinstance(research_cfg, Mapping) or
                research_cfg.get("enabled") is not True or
                research_cfg.get("require_validated_variant") is not True):
            raise AlpacaError("live mode requires the validated research edge gate")
        self._edge_base_cfg = deepcopy(cfg)
        self._edge_requested_variant = str(
            cfg.get("strategy", {}).get("variant_id") or "")
        self._edge_db_path = research_cfg.get("db_path") if isinstance(research_cfg, Mapping) else None
        self._edge_required = bool(
            isinstance(research_cfg, Mapping) and research_cfg.get("enabled", False) and
            research_cfg.get("require_validated_variant", True))
        self._edge_record: dict | None = None
        self._edge_records: list[dict] = []
        self._edge_configs: list[tuple[dict | None, dict]] = []
        self._edge_pinned_candidate_id: str | None = None
        self._edge_pinned_config_hash: str | None = None
        self._edge_pinned_runtime_cfg: dict | None = None
        self._edge_error: str | None = None
        if isinstance(research_cfg, Mapping) and research_cfg.get("enabled", False):
            try:
                from .edge import (apply_variant, resolve_validated_variant,
                                   resolve_validated_variants)
                if self.mode == "live" or self._edge_selection_mode == "specific":
                    record = resolve_validated_variant(
                        cfg, db_path=self._edge_db_path or None)
                    records = [record] if record is not None else []
                else:
                    records = resolve_validated_variants(
                        cfg, db_path=self._edge_db_path or None)
                self._edge_records = records
                self._edge_record = records[0] if records else None
                self._edge_configs = [(record, apply_variant(cfg, record))
                                      for record in records]
                if len(self._edge_configs) == 1:
                    self.cfg = self._edge_configs[0][1]
                if self.mode == "live" and len(records) == 1:
                    self._edge_pinned_candidate_id = str(records[0].get("candidate_id") or "")
                    self._edge_pinned_config_hash = str(records[0].get("config_hash") or "")
                    self._edge_pinned_runtime_cfg = deepcopy(self._edge_configs[0][1])
                if not records and self._edge_required:
                    vehicle = str(cfg.get("strategy", {}).get("execution_mode", "shares"))
                    self._edge_error = f"no latest-passing validated edge for {vehicle}"
                elif not records:
                    self._edge_configs = [(None, cfg)]
            except Exception as exc:  # noqa: BLE001
                self._edge_error = f"edge resolution failed: {exc}"
        else:
            self._edge_configs = [(None, cfg)]
        self.provider = provider or AlpacaProvider(cfg)
        provider_paper = getattr(self.provider, "paper", None)
        if (not isinstance(provider_paper, bool) or
                provider_paper is not expected_paper):
            raise AlpacaError(f"provider is not {self.mode} scoped")
        session_cfg = cfg.get("session", {})
        self.market = market_data or MarketData(self.provider, SessionPolicy(**session_cfg))
        llm_cfg = cfg.get("llm", {})
        llm_enabled = bool(llm_cfg.get("enabled", False)) if isinstance(llm_cfg, Mapping) else False
        self.brain = brain or (DecisionBrain(llm_cfg) if llm_enabled and not light else None)
        self.risk = RiskEngine(self._edge_base_cfg)
        self.light = light
        self.running = False
        self.shutdown_reason: str | None = None
        self.run_id = f"run-{int(time.time() * 1000)}"
        self._runtime_state: dict = {}
        self._lock_handle = None
        self._persistent_lock = False
        self._state_ready = False
        self._preflight: dict[str, Any] | None = None
        self._preflight_error: str | None = None
        self._reconciled = False
        self._startup_cleanup_checked = False
        self._assets: dict[str, Any] = {}
        try:
            state.configure_runtime(self.mode)
            state.ensure_ready()
            self._runtime_state = state.load_state()
            state.write_heartbeat("starting", run_id=self.run_id)
            self._state_ready = True
        except Exception as exc:  # noqa: BLE001
            log.warning("state startup unavailable: %s", exc)
        if not light:
            if not self._acquire_lock():
                self._preflight_error = "runtime lock held during startup reconciliation"
                log.warning(self._preflight_error)
            else:
                try:
                    self.preflight()
                    self.reconcile()
                    self._enforce_intraday_cleanup(
                        self._preflight.get("clock") if self._preflight else None,
                        reason="startup_reconciliation", force=True)
                except Exception as exc:  # noqa: BLE001
                    # A REST reconciliation failure must prevent entries, but
                    # it need not make a status/check command unusable.
                    log.warning("startup reconciliation unavailable: %s", exc)
                    self._preflight_error = str(exc)
                finally:
                    self._release_lock()

    def check(self, authenticated: bool = False) -> dict[str, Any]:
        result = {
            "mode": self.mode, "paper": self.mode == "paper",
            "credentials_configured": bool(self.provider.session.api_key and self.provider.session.secret_key),
            "authenticated": False,
            "edge_required": self._edge_required,
            "edge_ready": self._edge_record is not None,
            "edge_error": self._edge_error,
            "variant_id": (self._edge_record or {}).get("variant_id"),
            "variant_ids": [record.get("variant_id") for record in self._edge_records],
            "edge_vehicle": (self._edge_record or {}).get("vehicle"),
        }
        if authenticated:
            preflight = self.preflight()
            result.update(authenticated=True, account=preflight["account"],
                          clock=preflight["clock"], endpoint=preflight["endpoint"],
                          data_feed=preflight["data_feed"],
                          options_feed=preflight["options_feed"])
        return result

    def status(self) -> dict[str, Any]:
        result = self.check(False)
        try:
            result["clock"] = self.provider.clock()
            result["positions"] = self.provider.positions()
        except Exception as exc:  # noqa: BLE001
            result["auth_error"] = str(exc)
            result["positions"] = []
        return result

    def _acquire_lock(self, *, persistent: bool = False) -> bool:
        if self._lock_handle is not None:
            self._persistent_lock = self._persistent_lock or persistent
            return True
        handle = state.acquire_run_lock()
        if handle is None:
            return False
        self._lock_handle = handle
        self._persistent_lock = persistent
        return True

    def _release_lock(self) -> None:
        if self._lock_handle is not None:
            state.release_run_lock(self._lock_handle)
        self._lock_handle = None
        self._persistent_lock = False
