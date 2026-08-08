"""Optional decision-model boundary for US stocks, ETFs and listed options."""

from __future__ import annotations

import hashlib
import json
import os
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


def build_system(cfg: Mapping[str, Any] | None = None, catalog=None) -> str:
    del catalog
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
    text = str(value).strip()
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
        self.system = system or build_system(cfg)
        self.prompt_version = prompt_version(self.system)
        self.client = client

    def _client(self):
        if self.client is not None:
            return self.client
        provider = str(self.cfg.get("provider", "openai")).lower()
        api_key = os.getenv("OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("LLM credentials are unavailable; authenticated decisions cannot run")
        if provider == "openai":
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key)
        elif provider == "anthropic":
            from anthropic import Anthropic
            self.client = Anthropic(api_key=api_key)
        else:
            raise ValueError(f"unsupported LLM provider {provider!r}")
        return self.client

    def decide(self, snapshot: Mapping[str, Any], portfolio: Mapping[str, Any]) -> dict:
        prompt = json.dumps({"snapshot": snapshot, "portfolio": portfolio}, default=str, sort_keys=True)
        provider = str(self.cfg.get("provider", "openai")).lower()
        client = self._client()
        if hasattr(client, "complete"):
            result = client.complete(system=self.system, prompt=prompt)
        elif provider == "openai":
            response = client.chat.completions.create(model=self.cfg.get("model", "gpt-4o-mini"), temperature=self.cfg.get("temperature", .2), max_tokens=self.cfg.get("max_tokens", 2000), messages=[{"role": "system", "content": self.system}, {"role": "user", "content": prompt}])
            result = response.choices[0].message.content
        else:
            response = client.messages.create(model=self.cfg.get("model", "claude-3-5-sonnet"), max_tokens=self.cfg.get("max_tokens", 2000), temperature=self.cfg.get("temperature", .2), system=self.system, messages=[{"role": "user", "content": prompt}])
            result = response.content[0].text
        parsed = _extract_json(result)
        decisions = parsed.get("decisions", [])
        if not isinstance(decisions, list):
            raise ValueError("model decisions must be a list")
        return {"decisions": decisions}


Brain = DecisionBrain
