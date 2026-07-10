"""The analyst brain: a swappable LLM (Anthropic or OpenAI) that proposes
trades as strict JSON. It has NO authority over risk: everything it returns
is vetted and clamped by the deterministic risk engine before execution.
"""

import json
import logging
import os

log = logging.getLogger("brain")

SYSTEM = """You are the decision brain of an aggressive but disciplined crypto \
derivatives day trader operating on OKX USDT-margined perpetual swaps. You \
think in percentages, never in dollar amounts.

Trading style:
- Momentum and trend continuation: trade in the direction of multi-timeframe \
alignment (a 15m impulse in the direction of the 1h and 4h trend is the A+ setup).
- You go long and short with equal comfort.
- Funding matters: strongly positive funding is a tailwind for shorts, strongly \
negative funding is a tailwind for longs.
- Volatility-aware: your stop distance should normally be at least 1x the 1h \
ATR percentage so stops sit outside the noise; wider stops mean smaller intent size.
- Cut losers fast; target at least 2R on take-profits (take_profit_pct roughly \
2x stop_loss_pct or more).
- Being flat is a position. Only act on clear setups; returning zero new opens \
is normal and often correct.
- Never average down. Never revenge trade when the portfolio shows a losing day.
- If you hold a position whose thesis has broken (trend flipped against it, \
momentum died), close it rather than hoping.

You propose; a deterministic risk engine disposes. It will clamp your leverage, \
size and exposure to hard caps, so state your honest intent.

Output STRICT JSON only. No prose, no markdown fences. Schema:
{"decisions": [
  {"action": "open", "symbol": "BTC/USDT:USDT", "direction": "long",
   "confidence": 0.0, "size_pct_equity": 0.0, "leverage": 0.0,
   "stop_loss_pct": 0.0, "take_profit_pct": 0.0, "reasoning": "one sentence"},
  {"action": "close", "symbol": "ETH/USDT:USDT", "reasoning": "one sentence"},
  {"action": "hold", "symbol": "SOL/USDT:USDT"}
]}
Rules: at most {max_new} "open" decisions; stop_loss_pct and take_profit_pct \
are positive percentage distances from entry; confidence is in [0,1]; only \
"close" symbols that appear in the portfolio's open positions."""


class LLM:
    def __init__(self, cfg: dict):
        self.cfg = cfg["llm"]
        provider = self.cfg["provider"]
        if provider == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY missing from .env")
            from anthropic import Anthropic
            self.client = Anthropic()
            self._call = self._anthropic
        elif provider == "openai":
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY missing from .env")
            from openai import OpenAI
            self.client = OpenAI()
            self._call = self._openai
        else:
            raise ValueError(f"Unknown llm.provider '{provider}' "
                             "(use anthropic or openai)")

    def _anthropic(self, system: str, user: str) -> str:
        resp = self.client.messages.create(
            model=self.cfg["model"],
            max_tokens=int(self.cfg.get("max_tokens", 2000)),
            temperature=float(self.cfg.get("temperature", 0.2)),
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        )

    def _openai(self, system: str, user: str) -> str:
        kwargs = dict(
            model=self.cfg["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        try:
            resp = self.client.chat.completions.create(
                temperature=float(self.cfg.get("temperature", 0.2)), **kwargs
            )
        except Exception:
            # Some reasoning models reject a temperature parameter.
            resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    # ----------------------------------------------------------- public

    def decide(self, snapshot: dict, portfolio: dict, max_new: int) -> list[dict]:
        system = SYSTEM.replace("{max_new}", str(max(0, max_new)))
        user = (
            "MARKET SNAPSHOT (liquid USDT perpetual swaps on OKX):\n"
            + json.dumps(snapshot)
            + "\n\nPORTFOLIO STATE:\n"
            + json.dumps(portfolio)
            + "\n\nReturn your decisions JSON now."
        )
        text = self._call(system, user)
        return parse_decisions(text)


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_decisions(text: str) -> list[dict]:
    if not text:
        return []
    t = text.strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        log.warning("No JSON object found in model output")
        return []
    try:
        obj = json.loads(t[start:end + 1])
    except Exception as e:
        log.warning("Model output was not valid JSON: %s", e)
        return []

    out = []
    for d in obj.get("decisions", []) or []:
        if not isinstance(d, dict):
            continue
        action = str(d.get("action", "")).lower()
        symbol = d.get("symbol")
        if action not in ("open", "close", "hold") or not symbol:
            continue
        if action == "hold":
            continue
        item = {
            "action": action,
            "symbol": symbol,
            "reasoning": str(d.get("reasoning", ""))[:300],
        }
        if action == "open":
            direction = str(d.get("direction", "")).lower()
            if direction not in ("long", "short"):
                continue
            item.update({
                "direction": direction,
                "confidence": _num(d.get("confidence")),
                "size_pct_equity": _num(d.get("size_pct_equity")),
                "leverage": _num(d.get("leverage"), 1.0),
                "stop_loss_pct": _num(d.get("stop_loss_pct")),
                "take_profit_pct": _num(d.get("take_profit_pct")),
            })
        out.append(item)
    return out
