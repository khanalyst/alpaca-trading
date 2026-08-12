"""Optional decision-model boundary for US stocks, ETFs and listed options."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Mapping
from typing import Any

SYSTEM = """You are a disciplined US equity and listed-options paper trader.
Trade only symbols in the supplied universe. Entries are regular NYSE-session
only; exits and reconciliation may happen outside that session. Respect the
account's deterministic risk limits and stay flat when evidence is weak.
Return strict JSON with a `decisions` array. Each decision may rank or veto a
configured signal and includes an action (`buy`, `sell`, `close`, or `hold`),
symbol, and concise reason. The execution layer owns quantity and order terms.
Options must identify a listed contract and never be invented.
"""


def prompt_version(system: str | None = None) -> str:
    return hashlib.sha256((system or SYSTEM).encode()).hexdigest()[:16]


def build_system(cfg: Mapping[str, Any] | None = None) -> str:
    cfg = cfg or {}
    strategy = cfg.get("strategy") if isinstance(cfg.get("strategy"), Mapping) else {}
    risk = cfg.get("risk") if isinstance(cfg.get("risk"), Mapping) else {}
    return SYSTEM + "\nCONFIGURED POLICY:\n" + json.dumps({
        "strategy": {k: strategy[k] for k in ("id", "range_minutes", "target_r", "latest_entry_time") if k in strategy},
        "risk": {k: risk[k] for k in ("risk_per_trade_pct", "max_concurrent_positions", "max_position_notional_pct") if k in risk},
    }, sort_keys=True)


def _extract_json(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    text = str(value).strip()
    # A provider may successfully return no content (for example when its
    # safety layer declines to make a recommendation).  Runtime LLM policy
    # is subtractive: no recommendation is not a veto.  Malformed non-empty
    # content remains an error and therefore fails closed in the caller.
    if not text:
        return {}
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    parsed = json.loads(text)
    if not isinstance(parsed, Mapping):
        raise ValueError("model response must be a JSON object")
    return dict(parsed)


class DecisionBrain:
    """Lazy LLM client; fake ``complete`` callables are accepted in tests."""

    def __init__(self, cfg: Mapping[str, Any], *, client=None, system: str | None = None):
        self.cfg = dict(cfg)
        self.enabled = bool(self.cfg.get("enabled", False))
        self.system = system or build_system(cfg)
        self.prompt_version = prompt_version(self.system)
        self.client = client
        try:
            self.timeout_seconds = float(self.cfg.get("timeout_seconds", 10.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("llm.timeout_seconds must be a positive number") from exc
        if not 0 < self.timeout_seconds < float("inf"):
            raise ValueError("llm.timeout_seconds must be a positive number")
        self._timed_out = False

    def _call_with_timeout(self, call, *, operation: str = "LLM request"):
        """Run one provider call behind a strict, process-safe time boundary.

        Provider SDK calls are synchronous and a broken transport can ignore
        its own socket timeout.  A daemon worker lets the engine return a
        deterministic veto when the deadline expires; the worker cannot keep
        the process alive or delay shutdown.  There is intentionally no
        attempt to reuse a timed-out client/thread.
        """
        if self._timed_out:
            raise TimeoutError("LLM provider quarantined after a previous timeout")
        result: list[Any] = []
        failure: list[BaseException] = []
        done = threading.Event()

        def invoke() -> None:
            try:
                result.append(call())
            except BaseException as exc:  # noqa: BLE001
                failure.append(exc)
            finally:
                done.set()

        worker = threading.Thread(
            target=invoke,
            name="alpaca-llm-call",
            daemon=True,
        )
        worker.start()
        if not done.wait(self.timeout_seconds):
            # Do not reuse a client while an uninterruptible provider call is
            # still running in its daemon worker.
            self._timed_out = True
            raise TimeoutError(
                f"{operation} exceeded timeout_seconds={self.timeout_seconds:g}")
        if failure:
            raise failure[0]
        return result[0] if result else None

    def _client(self):
        if self.client is not None:
            return self.client
        provider = str(self.cfg.get("provider", "openai")).lower()
        api_key = os.getenv("OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("LLM credentials are unavailable; authenticated decisions cannot run")
        base_url = self.cfg.get("base_url") or (
            os.getenv("OPENAI_BASE_URL") if provider == "openai" else None)
        if provider == "openai":
            from openai import OpenAI
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self.client = OpenAI(**kwargs)
        elif provider == "anthropic":
            from anthropic import Anthropic
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self.client = Anthropic(**kwargs)
        else:
            raise ValueError(f"unsupported LLM provider {provider!r}")
        return self.client

    def decide(self, snapshot: Mapping[str, Any], portfolio: Mapping[str, Any]) -> dict:
        if not self.enabled:
            raise RuntimeError("LLM decisions are disabled; deterministic strategy is active")
        prompt = json.dumps({"snapshot": snapshot, "portfolio": portfolio}, default=str, sort_keys=True)
        provider = str(self.cfg.get("provider", "openai")).lower()
        client = self._client()
        if hasattr(client, "complete"):
            result = self._call_with_timeout(
                lambda: client.complete(system=self.system, prompt=prompt))
        elif provider == "openai":
            response = self._call_with_timeout(lambda: client.chat.completions.create(
                model=self.cfg["model"], temperature=self.cfg.get("temperature", .2),
                max_tokens=self.cfg.get("max_tokens", 2000), messages=[
                    {"role": "system", "content": self.system},
                    {"role": "user", "content": prompt},
                ]))
            choices = getattr(response, "choices", None) or []
            result = (getattr(getattr(choices[0], "message", None), "content", None)
                      if choices else "")
        else:
            response = self._call_with_timeout(lambda: client.messages.create(
                model=self.cfg["model"], max_tokens=self.cfg.get("max_tokens", 2000),
                temperature=self.cfg.get("temperature", .2), system=self.system,
                messages=[{"role": "user", "content": prompt}]))
            content = getattr(response, "content", None) or []
            result = getattr(content[0], "text", None) if content else ""
        parsed = _extract_json(result)
        decisions = parsed.get("decisions", [])
        if not isinstance(decisions, list):
            raise ValueError("model decisions must be a list")
        return {"decisions": decisions}
