"""The analyst brain: a swappable LLM (Anthropic or OpenAI) that proposes
trades as strict JSON. It has NO authority over risk: everything it returns
is vetted and clamped by the deterministic risk engine before execution.

The prompt is assembled once per process from the shared SYSTEM text plus the
active strategy's ``prompt_fragment`` (see agent/registry.py), so the model is
never told about setup archetypes belonging to a strategy it is not running.

Token-cost design:
- The assembled system prompt is byte-identical on every call. Anthropic
  caches it explicitly (cache_control below); OpenAI caches stable
  >=1024-token prefixes automatically. Any per-cycle value (like the max
  number of new opens) belongs in the user message, never in the system
  prompt, or the cache breaks. Each strategy gets its own cache entry because
  the cache key is derived from the assembled text.
- SYSTEM is deliberately sized above 1,024 tokens: Anthropic silently skips
  caching below a per-model minimum (Sonnet 4.6: 1,024; some Opus and
  Haiku 4.5: 4,096 - on those models the marker is a harmless no-op).
- The cache TTL is 1h, not the default 5m: cycles run ~6 minutes apart, so a
  5m entry would expire between calls and every call would pay the write
  premium with zero reads. Each read refreshes the 1h TTL, so a running
  agent pays the 2x write once and ~0.1x reads thereafter.
- Caching changes billing only; the model receives the identical prompt.
"""

import hashlib
import json
import logging
import math
import os
import re
from copy import deepcopy

from . import hypotheses, provider, variants
from .registry import spec_for

log = logging.getLogger("brain")

SYSTEM = """You are the decision brain of an aggressive but disciplined \
crypto derivatives day trader operating 24/7 on OKX USDT-margined perpetual \
swaps. You think in percentages, never in dollar amounts. Each cycle you \
receive a market snapshot and the live portfolio state, and you reply with \
trade decisions as strict JSON.

TRADING STYLE
- Momentum and trend continuation: trade in the direction of multi-timeframe \
alignment. A 15m impulse in the direction of the 1h and 4h trend is the A+ \
setup.
- You go long and short with equal comfort. Shorts are not riskier than \
longs on perpetual swaps; take whichever side the trend favours.
- Funding is context, not a standalone direction signal. Compare the current \
rate with its recent percentile/change, price trend and perp-index basis before \
calling it a crowding tailwind.
- Volatility-aware: choose the semantic invalidation anchor and approved exit \
policy. Deterministic strategy code converts that choice into stop, target, \
size and leverage from ATR, structure and account risk limits.
- Being flat is a position. Only act on clear setups; returning zero new \
opens is normal and often correct. Choppy, trendless markets are for \
waiting, not for forcing trades.
- Never average down. Never revenge trade when the portfolio shows a losing \
day.
- If you hold a position whose thesis has broken (trend flipped against it, \
momentum died, funding turned sharply against it), close it rather than hope - \
but not in the first minutes of the trade. A freshly opened position moving \
against you is the normal cost of entry, not evidence. See POSITION MANAGEMENT \
for the minimum-hold rule that deterministic code enforces.

REGISTERED HYPOTHESES
An experimental setup (setup_type "other") must name one of these in \
hypothesis_id. Each has its own deterministic contract, which is checked \
before the trade is allowed, and its own separately attributed results. You \
may not invent one: an unregistered id is rejected. Propose one only when \
its stated condition genuinely holds - these exist to be measured, and a \
loosely applied label makes its row meaningless.
__HYPOTHESIS_LIST__

ADAPTIVE RESEARCH PROPOSALS
You may include at most one proposal object when a registered hypothesis has
a numeric setting worth testing: {"hypothesis_id":"...","setting_id":"...",
"value":0.0,"reasoning":"..."}. The id and setting must be registered,
reasoning must explain why this exact value tests the registered mechanism,
and value must be a finite number inside that setting's registered semantic bounds.
This proposal is research metadata only; it never changes live risk or configuration.

RESEARCH STRATEGY SELECTOR
You may include at most one root-level research_selection object:
{"strategy_id":"...","variant_id":"...","reasoning":"..."}. Omit
variant_id to ask the durable coordinator to resolve that strategy's exact
next eligible untested single-axis setting deterministically. If variant_id is
present it must be one exact identifier listed below for the same strategy.
This metadata cannot switch the live strategy, alter live/demo configuration,
risk, leverage or capital, and cannot place, close or modify an order. Never
encode any execution instruction in it. It only prioritizes a future isolated
research assignment and never preempts an assignment already in progress.
Valid research strategy and exact setting identifiers:
__RESEARCH_SELECTION_LIST__

MARKET SNAPSHOT FIELD REFERENCE
The special _market_context object summarizes BTC as the market benchmark: \
its explicit regime, ATR ratio, relative volume and one-hour momentum. Use \
that context to distinguish a market-wide move from an isolated symbol move.
It also reports instruments_scanned, instruments_with_a_valid_setup and \
setup_breadth_pct: how many instruments satisfy a setup contract this cycle. \
Breadth is a warning, not an opportunity. When most of the universe qualifies \
at once, those setups are one correlated market move wearing many hats, and \
measured over two years they performed markedly worse than setups that \
appeared while the rest of the universe was quiet. High breadth means demand \
more from each candidate and prefer taking fewer, or none; low breadth means \
the setup is genuinely idiosyncratic. This is now also a hard rule: when \
instruments_with_a_valid_setup exceeds risk.max_setups_firing_for_entry, \
deterministic code refuses every new entry that cycle regardless of quality. \
Do not spend output proposing opens on those cycles.
Each symbol in the snapshot carries these fields:
- price: last traded price in USDT.
- chg_24h_pct: percent price change over the last 24 hours.
- vol_24h_musd: 24h quote volume in millions of USD. A liquidity gauge; \
every symbol shown has already passed the universe volume floor.
- spread_pct: current best-ask minus best-bid spread as a percent of mid. \
Treat it as an immediate execution cost; a wide spread lowers net reward.
- relative_volume_1h: volume in the latest completed hour divided by the \
median one-hour volume of the preceding sample. Above 1 means participation \
is elevated; below 1 means the move has weak participation.
- funding_rate_pct: current funding rate as a percent per funding interval. \
Positive means longs pay shorts; negative means shorts pay longs. Compare \
rates across symbols only after scaling to a common cadence: a 4h contract \
charges the same rate twice as often as an 8h one. OKX clamps funding on \
liquid perpetuals near 0.01% per 8h, so on majors a rate pinned at that \
level already is the extreme; percent-level readings appear only on thin or \
newly listed instruments.
- funding_interval_hours / next_funding_minutes: the current settlement \
cadence and time until the next charge. Do not assume every contract always \
uses an eight-hour interval.
- funding_mean_30_pct / funding_percentile_30 / funding_change_pct: recent \
funding distribution and change. Small samples are weak evidence.
- perp_index_basis_pct: mark-price premium or discount to the index.
- open_interest_musd: current open interest value when OKX supplies it.
- taker_fee_pct_per_side / fee_rate_source: the account fee used by sizing and \
whether it came from OKX or the configured fallback.
- trend_15m, trend_1h, trend_4h: "up" when price > EMA20 > EMA50 on that \
timeframe, "down" when price < EMA20 < EMA50, otherwise "flat". Three \
aligned values is a strong trend; mixed values mean chop.
- rsi_1h: 14-period RSI on 1h candles. Above ~70 is stretched long, below \
~30 stretched short; in a strong trend RSI can stay stretched for a long \
time, so treat it as context, not a standalone signal.
- atr_1h_pct: 14-period ATR on 1h candles as a percent of price. This is \
the noise floor: stops tighter than this get wicked out randomly.
- atr_1h_ratio: current 1h ATR% divided by its recent median. Values around \
1 are normal; values well above 1 identify an unusually volatile regime.
- mom_1h_pct: percent change over the last hour (four 15m closes).
- range_pos_pct: where the last price sits inside the 24h high-low range. \
0 means at the low, 100 means at the high. Breakouts near 100 with an \
aligned uptrend (or near 0 with a downtrend) are continuation contexts; \
mid-range readings are no-man's-land.
- swing_low_pct / swing_high_pct: percent distance from the current price \
down to the lowest low and up to the highest high of the last 20 15m \
candles (about five hours). These are your structure anchors: a long's \
stop belongs beyond the recent swing low (at least swing_low_pct plus a \
small buffer), a short's beyond the recent swing high.
- ema20_1h_dist_pct: percent distance of price from the 1h EMA20 \
(positive = above it). Near zero in an uptrend marks a pullback-to-trend \
entry zone; a large positive value means extended and chase-prone.
- corr_btc_1h_30: correlation of the symbol's last 30 completed 1h returns \
with BTC. Near +1 means the position is another BTC-direction bet; near 0 \
is more independent; negative means it has recently moved opposite BTC.
- corr_btc_1h_72_shrunk / beta_btc_1h_72 / \
corr_btc_downside_1h_72: longer, shrinkage-adjusted co-movement, BTC beta and \
down-market correlation. Check corr_btc_samples before trusting them.
- signal_ts: timestamp of the completed 15m candle that identifies this setup.
- signal_1h_ts: timestamp of the latest completed 1h candle. Failed-thesis \
re-entry requires a genuinely newer 1h bar.
- mom_15m_pct / signal_candle_return_pct: the latest completed 15m impulse.
- fresh_breakout_long / fresh_breakout_short and breakout_distance_pct: true \
only when the latest completed signal candle closes beyond the preceding \
20-candle range; unlike 24h range position, this proves the break is fresh.
- price_stabilized_long / price_stabilized_short: completed-candle evidence \
that price stopped extending the adverse extreme and closed back in the \
potential squeeze direction.
- setup_evidence: minimum deterministic evidence contracts for recognised setup \
types, plus directional EMA extension in ATR, the hard no-chase boundary, and \
the 8h-equivalent funding rate the squeeze contract actually compared against \
its threshold.
- regime: deterministic description of the current data: trend_up, \
trend_down, high_volatility, choppy, or transition. It is context, not a \
command; decide whether the setup fits it.

PORTFOLIO STATE FIELD REFERENCE
- equity_usdt: live USDT currency equity. Other account assets (including \
demo OKB) are excluded. All sizing is derived from this USDT value.
- day_pnl_pct: percent PnL since the UTC day started. If this is \
meaningfully negative, be pickier, not more aggressive: the daily loss \
breaker is close.
- drawdown_from_high_pct: percent below the account's high-water mark. The \
account self-kills at the configured max drawdown; protect it.
- state: RUNNING means you may open and close; DAY_STOPPED means the daily \
loss limit tripped and you may only close.
- open_positions: for each held position: symbol, side, entry, mark, \
upnl_pct (unrealised PnL percent on margin), leverage, notional_usd, \
hours_open, age_verified, planned_risk_usd and original_thesis. The original \
thesis contains the entry reason, setup/invalidation/exit policy and compact \
entry evidence. Compare it field-by-field with the current symbol snapshot \
before proposing a close. Positions are force-closed at the max hold age, so tired \
positions going nowhere are better closed by you at a good price than by \
the clock at a bad one.
- post_loss_cooldowns: symbols temporarily barred after a realized losing \
close, with minutes remaining. Do not waste an open proposal on them.
- recent_entry_feedback: entries that could not fit safely in the live order \
book. This is execution evidence, not an automatic signal veto. Compare the \
setup with every other candidate. If retry_allowed is true, you may retry \
only by setting execution_choice to retry_smaller; deterministic code applies \
the safe size cap shown in max_retry_size_pct_equity. If false, choose another \
setup or stay flat until retry_after_minutes expires. Never repeat the \
original full size.
- recent_entry_failures: non-liquidity exchange failures, including the \
failed stage, safe OKX code/message, classification and retry delay. Do not \
propose the symbol again while retry_after_minutes is positive. A permanent \
classification means the instrument/account combination should be avoided \
until the universe is refreshed.
- recent_setup_memory: setup IDs already evaluated or traded. A symbol is \
evaluated only once per completed signal candle, even if you would relabel the \
setup or flip direction. A positive retry_after_minutes also blocks a \
semantically identical setup on a newer candle. A prior loss additionally \
requires elapsed time, a fresh completed 1h candle, changed objective evidence \
and your explicit what_changed_since_last_loss explanation.
- hard_limits_fyi: key deterministic risk caps. You do not choose size or \
leverage.
- trading_costs_fyi: fallback taker fee per side, expected stop slippage, \
expected holding hours and the minimum number of funding intervals. Each \
symbol snapshot carries the authenticated OKX account fee when available. \
Combine those costs with live spread and direction-aware funding when judging \
whether an exit policy leaves enough room; deterministic code performs sizing.

HOW THE RISK ENGINE HANDLES YOUR PROPOSALS
You propose; a deterministic risk engine disposes. It will:
- discard any open whose confidence is below the configured floor;
- reject symbols already held, symbols in post-loss cooldown, and anything \
beyond the max concurrent positions or gross exposure caps;
- reject a repeated liquidity-constrained proposal unless you explicitly set \
execution_choice to retry_smaller; code calculates the safe reduced size;
- reject a symbol while a non-liquidity execution-failure backoff is active;
- reject opens that would push the book's net directional exposure (long \
notional minus short notional) beyond the configured cap - several \
same-direction positions in correlated coins count as one big bet, so \
diversify direction or accept the rejection;
- cap the sum of planned all-in stop risk across every open position, and cap \
signed BTC-beta-weighted exposure using measured beta (or conservative beta=1 \
when history is insufficient);
- set leverage from configuration and size the position so that the \
deterministic structure/ATR stop plus expected fees, live spread, adverse \
funding and slippage remains inside the risk budget;
- derive stop and target from your setup_type, invalidation_anchor and \
exit_policy;
- reject stops tighter than 0.2% or wider than 15%, and take-profit \
distances wider than 50%;
- drop any "open" on a symbol that also has a "close" in the same reply.
Because every proposal is vetted, state your honest intent; never inflate \
confidence to push a marginal trade through. Confidence is a decision gate \
and later calibration input, not a way to force size.

__SETUP_ARCHETYPES__

MARKET REGIME AWARENESS
- Trending regime (BTC and the majors showing aligned trends): trade \
continuation, let winners run to 2R or more via generous take-profits.
- Choppy regime (flat trends, small momentum, mid-range positions): most \
breakouts fail. Stand aside unless a recognised setup has unusually strong \
evidence; do not invent a live mean-reversion setup.
- High-volatility events (atr_1h_pct several times its usual level): \
spreads and slippage widen and stops get hunted. Demand more confluence and a \
defensible anchor or stay flat. Code automatically widens the ATR floor and \
reduces size as the all-in stop cost grows.
- Correlation: most alts follow BTC. Three longs in correlated alts is one \
big BTC bet wearing three hats - diversify direction or symbols only when \
their own charts genuinely diverge.

POSITION MANAGEMENT
- Every open position already has an exchange-side stop-loss and \
take-profit; you never need to close a position merely to enforce those \
levels.
- Close early when the reason you entered has disappeared, not merely \
because PnL is red; a position down 0.3% with an intact thesis is healthier \
than one up 0.5% in a dying trend.
- Minimum hold: deterministic code rejects any close inside \
strategy.min_hold_minutes unless close_trigger is risk_reduction. Entries are \
timed off a completed signal candle, and the measured forward return is at \
its WORST about half an hour after entry - a setup giving some back \
immediately is the expected path, not a broken thesis. Judge the thesis on \
the evidence fields you entered on, never on how the first candle went. \
Positions carry hours_open; check it before proposing a close.
- The margin guard and the max-hold timer may force-close positions; \
pre-empt them by closing overextended or stale positions on your own terms.
- Do not propose a new open on a symbol you are closing this same cycle.

DECISION DISCIPLINE
- Quality over quantity: one A+ setup beats three mediocre ones.
- Confidence calibration: 0.9+ means everything aligns (trend on all three \
timeframes, momentum, funding, range position); 0.7-0.8 is a good setup \
with one caveat; anything you would rate below the floor, do not propose.
- When the portfolio already holds positions, first decide whether each \
still deserves its slot; closing a dead position frees risk budget for a \
live setup.
- After a losing day (negative day_pnl_pct), raise your internal bar for \
new entries; the fastest way to turn a bad day terrible is overtrading it.
- Costs are real. Judge whether the deterministic exit policy has enough room \
after account fees, spread, expected slippage and direction-aware funding. If \
not, reject the trade rather than trying to manipulate size or leverage.

OUTPUT FORMAT
The final user message states the maximum number of new "open" decisions \
permitted this cycle. Never exceed it. Output STRICT JSON only - no prose, \
no markdown fences, no comments. Schema:
{"decisions": [
  {"action": "open", "symbol": "BTC/USDT:USDT", "direction": "long",
   "setup_type": "trend_continuation",
   "invalidation_anchor": "structure", "exit_policy": "fixed_rr",
   "execution_choice": "normal", "confidence": 0.0,
   "what_changed_since_last_loss": "", "reasoning": "one sentence"},
  {"action": "close", "symbol": "ETH/USDT:USDT",
   "close_trigger": "thesis_invalidated",
   "evidence_change": "entry trend_1h up; current trend_1h down",
   "reasoning": "one sentence"},
  {"action": "hold", "symbol": "SOL/USDT:USDT"}
]}
Optional root metadata may additionally contain the registered numeric
proposal described above and/or one research_selection object described above.
Rules: setup_type must be trend_continuation, range_breakout, funding_squeeze \
or other. other is experimental, demo-only, and REQUIRES a hypothesis_id \
chosen from the REGISTERED HYPOTHESES list below - an experimental setup \
without one is rejected. Do not send hypothesis_id with any other \
setup_type. invalidation_anchor must be \
structure or atr; every contracted setup requires structure. \
exit_policy must be fixed_rr or extended_rr. \
execution_choice is normal unless current liquidity feedback justifies \
retry_smaller. what_changed_since_last_loss is required only when retrying \
the same direction/setup after a recorded loss. Confidence is in [0,1]. \
For close, close_trigger must be thesis_invalidated, risk_reduction, \
stale_position or profit_protection, and evidence_change must state the \
specific original-versus-current evidence change. You have no size, leverage, numeric \
stop or numeric target field. Only close symbols in open_positions; hold \
entries are optional and ignored; an empty decisions list is valid.

SELF-CHECK BEFORE ANSWERING
Run through this list before emitting your JSON:
1. Does every open honestly match the selected setup_evidence contract, or is \
it explicitly labelled other in demo?
2. Does the selected invalidation anchor represent the setup's real failure \
point, and does the exit policy leave enough net room after costs?
3. Is every confidence a number you would defend, not a number chosen to \
clear the floor?
4. Have you checked each currently open position against its original \
thesis, and does every close name a valid trigger plus the exact evidence \
that changed?
5. Are you within the stated maximum number of new opens, with no open and \
close on the same symbol in the same answer?
6. If the honest answer this cycle is "no trade", is your decisions list \
empty rather than padded with a marginal idea?
7. If recent_entry_feedback or recent_entry_failures exists, did you \
deliberately choose between a permitted retry, a stronger alternative, and \
staying flat instead of blindly repeating the rejected symbol or size?
8. Did you avoid every setup already listed in recent_setup_memory?
9. Is the output a single JSON object with no prose around it?

WORKED EXAMPLES
Example A - one tired holding, one aligned setup:
{"decisions":[
 {"action":"close","symbol":"ETH/USDT:USDT",\
"close_trigger":"thesis_invalidated",\
"evidence_change":"entry trend_1h was up; current trend_1h is down and \
mom_1h_pct is negative",\
"reasoning":"held 14h and the recorded continuation thesis has broken"},
 {"action":"open","symbol":"BTC/USDT:USDT","direction":"long",\
"setup_type":"trend_continuation","invalidation_anchor":"structure",\
"exit_policy":"extended_rr","execution_choice":"normal","confidence":0.82,\
"reasoning":"15m impulse resumed with 1h and 4h uptrends while funding and \
basis remain non-extreme"}
]}
Example B - nothing qualifies:
{"decisions":[]}
Example C - short setup:
{"decisions":[
 {"action":"open","symbol":"SOL/USDT:USDT","direction":"short",\
"setup_type":"range_breakout","invalidation_anchor":"structure",\
"exit_policy":"fixed_rr","execution_choice":"normal","confidence":0.74,\
"reasoning":"1h and 4h downtrend with a high-volume break near the range low"}
]}"""

_ARCHETYPE_MARKER = "__SETUP_ARCHETYPES__"


_HYPOTHESIS_MARKER = "__HYPOTHESIS_LIST__"


_RESEARCH_SELECTION_MARKER = "__RESEARCH_SELECTION_LIST__"


def research_selection_prompt_fragment(
        catalog: dict[str, tuple[dict, ...]] | None = None) -> str:
    if catalog is None:
        catalog = variants.research_selection_catalog()
    lines = []
    for strategy_id, settings in catalog.items():
        identifiers = ", ".join(str(item["variant_id"]) for item in settings)
        lines.append(f"- {strategy_id}: {identifiers}")
    return "\n".join(lines) or "- none"


def build_system(
        cfg: dict,
        *, catalog: dict[str, tuple[dict, ...]] | None = None) -> str:
    """Assemble the system prompt for the configured strategy.

    The shared text covers the parts that are true of any strategy this agent
    runs - the risk contract, the snapshot field reference, the output schema.
    The setup archetypes are strategy-specific and come from the register, so
    the model is never told about setups belonging to a strategy it is not
    running.

    Substitution happens once at startup rather than per call: the invariant
    that makes prompt caching work is byte-identical text on every call for a
    given strategy, not one global constant.
    """
    fragment = spec_for(str(cfg["strategy"]["id"])).prompt_fragment.strip()
    system = SYSTEM.replace(_ARCHETYPE_MARKER, fragment)
    # The experimental list is versioned into the prompt rather than left
    # open-ended, so every experimental trade is attributable to a claim
    # registered before it was taken.
    system = system.replace(_HYPOTHESIS_MARKER, hypotheses.prompt_fragment())
    return system.replace(
        _RESEARCH_SELECTION_MARKER, research_selection_prompt_fragment(catalog))


def prompt_version(system: str) -> str:
    return hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]


# The key every recorded-but-withheld field lives under. One name, checked in
# one place, so "is this field visible to the model?" has a mechanical answer.
ENRICHMENT_KEY = "_enrichment"


def withhold_enrichment(value):
    """Return ``value`` with every ``_enrichment`` block removed.

    B0.5 records fields the model must not see yet - open-interest deltas,
    book state, realised-volatility ratios. They are journalled from the
    moment collection starts, because a snapshot taken without them can never
    be made to have them, and they are withheld from the prompt because
    changing the prompt changes model behaviour and forks the comparability
    of every observation either side of the change.

    Showing the model a new field is therefore a deliberate, versioned act
    belonging to its own batch, with its own attribution fork and its own
    before-and-after replay. It is not a side effect of starting to record.

    The copy is deep enough to protect the caller's dict and no deeper: the
    journal writes the same snapshot object after the prompt is built, and it
    must still see everything.
    """
    if isinstance(value, dict):
        return {key: withhold_enrichment(item)
                for key, item in value.items() if key != ENRICHMENT_KEY}
    if isinstance(value, list):
        return [withhold_enrichment(item) for item in value]
    return value


class LLM:
    @staticmethod
    def _sampling_unsupported(model: str) -> bool:
        name = str(model).lower()
        return name.startswith(("gpt-5", "o1", "o3", "o4"))

    def __init__(self, cfg: dict, *,
                 catalog: dict[str, tuple[dict, ...]] | None = None,
                 system: str | None = None):
        self.cfg = cfg["llm"]
        # Resolved once, then byte-identical for the life of the process,
        # which is what both providers' prompt caches require.
        self.research_selection_catalog = (
            variants.research_selection_catalog()
            if catalog is None else catalog)
        self.system = (
            build_system(cfg, catalog=self.research_selection_catalog)
            if system is None else str(system))
        self.prompt_version = prompt_version(self.system)
        provider_name = self.cfg["provider"]
        self.provider = provider_name
        effective_endpoint = provider.resolve_provider_endpoint(
            provider_name, self.cfg)
        self.endpoint_hash = provider.hash_provider_endpoint(effective_endpoint)
        self.endpoint_identity = provider.safe_provider_endpoint(effective_endpoint)
        client_kwargs = {"base_url": effective_endpoint}
        if provider_name == "anthropic":
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY missing from .env")
            from anthropic import Anthropic
            self.client = Anthropic(**client_kwargs)
            self._call = self._anthropic
        elif provider_name == "openai":
            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY missing from .env")
            from openai import OpenAI
            self.client = OpenAI(**client_kwargs)
            self._call = self._openai
        else:
            raise ValueError(f"Unknown llm.provider '{provider_name}' "
                             "(use anthropic or openai)")
        # Newer models (Sonnet 5, Opus 4.7+) reject sampling parameters;
        # discovered once at runtime, then omitted from every later call.
        self._no_temperature = self._sampling_unsupported(
            self.cfg["model"])
        self._last_request_attempts: list[dict] = []
        self._last_response_audit: dict | None = None
        self._last_usage_audit: dict | None = None

    def _anthropic_params(self, system: str, user: str) -> dict:
        params = dict(
            model=self.cfg["model"],
            max_tokens=int(self.cfg.get("max_tokens", 2000)),
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            messages=[{"role": "user", "content": user}],
        )
        if not self._no_temperature:
            params["temperature"] = float(self.cfg.get("temperature", 0.2))
        return params

    def _openai_params(self, system: str, user: str) -> dict:
        params = dict(
            model=self.cfg["model"],
            max_completion_tokens=int(self.cfg.get("max_tokens", 2000)),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            # Stable routing key for the byte-identical system-prefix cache.
            # Dynamic market/portfolio data remains after that prefix.
            prompt_cache_key=f"okx-agent-{self.prompt_version}",
        )
        if not self._no_temperature:
            params["temperature"] = float(self.cfg.get("temperature", 0.2))
        return params

    def _record_request_attempt(self, params: dict) -> None:
        if not hasattr(self, "_last_request_attempts"):
            self._last_request_attempts = []
        self._last_request_attempts.append(deepcopy(params))

    def _anthropic(self, system: str, user: str) -> str:
        params = self._anthropic_params(system, user)
        self._record_request_attempt(params)
        try:
            resp = self.client.messages.create(**params)
        except Exception as e:
            if "temperature" in params and "temperature" in str(e):
                self._no_temperature = True
                params.pop("temperature")
                self._record_request_attempt(params)
                resp = self.client.messages.create(**params)
            else:
                raise
        self._last_response_id = getattr(resp, "id", None)
        u = getattr(resp, "usage", None)
        if u:
            total = int(getattr(u, "input_tokens", 0) or 0)
            written = int(
                getattr(u, "cache_creation_input_tokens", 0) or 0)
            cached = int(getattr(u, "cache_read_input_tokens", 0) or 0)
            fresh = max(0, total - cached)
            hit_rate = (cached / total * 100) if total else 0.0
            self._last_usage_audit = {
                "input_tokens_total": total,
                "input_tokens_fresh": fresh,
                "output_tokens": int(
                    getattr(u, "output_tokens", 0) or 0),
                "cache_write_tokens": written,
                "cache_read_tokens": cached,
                "cache_hit_pct": round(hit_rate, 1),
            }
            log.info(
                "tokens: in_total=%s in_fresh=%s out=%s cache_write=%s "
                "cache_read=%s cache_hit=%.1f%%",
                total, fresh, self._last_usage_audit["output_tokens"],
                written, cached, hit_rate)
        return "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        )

    def _openai(self, system: str, user: str) -> str:
        # OpenAI caches stable prompt prefixes >=1024 tokens automatically;
        # keeping SYSTEM byte-identical across calls is what enables it.
        kwargs = self._openai_params(system, user)
        self._record_request_attempt(kwargs)
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as e:
            # Reasoning models (o-series, GPT-5.x) reject sampling params.
            # Remember the rejection so later cycles skip the doomed first
            # attempt; every other error propagates instead of being masked
            # by a blind retry.
            if "temperature" in kwargs and "temperature" in str(e):
                self._no_temperature = True
                kwargs.pop("temperature")
                self._record_request_attempt(kwargs)
                resp = self.client.chat.completions.create(**kwargs)
            else:
                raise
        self._last_response_id = getattr(resp, "id", None)
        u = getattr(resp, "usage", None)
        if u:
            cached = 0
            details = getattr(u, "prompt_tokens_details", None)
            if details:
                cached = getattr(details, "cached_tokens", 0) or 0
            total = int(getattr(u, "prompt_tokens", 0) or 0)
            output = int(getattr(u, "completion_tokens", 0) or 0)
            fresh = max(0, total - int(cached))
            hit_rate = (int(cached) / total * 100) if total else 0.0
            self._last_usage_audit = {
                "input_tokens_total": total,
                "input_tokens_fresh": fresh,
                "output_tokens": output,
                "cache_write_tokens": 0,
                "cache_read_tokens": int(cached),
                "cache_hit_pct": round(hit_rate, 1),
            }
            log.info(
                "tokens: in_total=%s in_fresh=%s out=%s cache_read=%s "
                "cache_hit=%.1f%%",
                total, fresh, output, cached, hit_rate)
        return resp.choices[0].message.content or ""

    def preflight(self) -> str:
        """Verify API-key access to the configured model.

        Prefer the metadata endpoint because it costs nothing. Gateways that
        serve an OpenAI-compatible surface without implementing `/models`
        (Azure AI Foundry among them) would otherwise fail this check while
        being perfectly able to run the agent, so fall back to a small bounded
        generation. A wrong key or an unavailable deployment still
        fails - it just fails on the call that actually matters.
        """
        model = self.cfg["model"]
        try:
            if self.provider == "anthropic":
                info = self.client.models.retrieve(model_id=model)
            else:
                info = self.client.models.retrieve(model=model)
            return str(getattr(info, "id", None)
                       or getattr(info, "display_name", None) or model)
        except Exception as metadata_error:
            try:
                if self.provider == "anthropic":
                    self.client.messages.create(
                        model=model, max_tokens=1,
                        messages=[{"role": "user", "content": "ping"}])
                else:
                    self.client.chat.completions.create(
                        model=model, max_completion_tokens=16,
                        messages=[{"role": "user",
                                   "content": "Reply with OK."}])
            except Exception as generate_error:
                raise RuntimeError(
                    f"{model} is not reachable. Metadata lookup said: "
                    f"{metadata_error}. A minimal generation said: "
                    f"{generate_error}") from generate_error
            return f"{model} (generation probe; /models not served)"

    def endpoint(self) -> str:
        """The base URL actually in use, so `check` can show it."""
        return self.endpoint_identity

    @staticmethod
    def _user_message(snapshot: dict, portfolio: dict, max_new: int) -> str:
        return (
            "MARKET SNAPSHOT (liquid USDT perpetual swaps on OKX):\n"
            + json.dumps(withhold_enrichment(snapshot),
                         separators=(",", ":"), allow_nan=False)
            + "\n\nPORTFOLIO STATE:\n"
            + json.dumps(portfolio, separators=(",", ":"), allow_nan=False)
            + f"\n\nYou may propose at most {max(0, max_new)} new \"open\" "
              "decisions this cycle.\nReturn your decisions JSON now."
        )

    def audit_request(self, snapshot: dict, portfolio: dict,
                      max_new: int) -> dict:
        """Return the exact initial provider request before it is sent."""
        user = self._user_message(snapshot, portfolio, max_new)
        request = (self._anthropic_params(self.system, user)
                   if self.provider == "anthropic"
                   else self._openai_params(self.system, user))
        return {"provider": self.provider, "request": request}

    def call_audit(self) -> dict | None:
        """Return actual attempts plus raw provider output, if any."""
        attempts = getattr(self, "_last_request_attempts", [])
        response = getattr(self, "_last_response_audit", None)
        if not attempts and response is None:
            return None
        return {
            "provider": self.provider,
            "model": self.cfg["model"],
            "request_attempts": deepcopy(attempts),
            "response": deepcopy(response),
            "usage": deepcopy(getattr(self, "_last_usage_audit", None)),
        }

    def decide(self, snapshot: dict, portfolio: dict, max_new: int) -> list[dict]:
        # Compact separators shave ~10% off the per-cycle payload; the
        # per-cycle max_new lives here so SYSTEM stays byte-identical.
        user = self._user_message(snapshot, portfolio, max_new)
        self._last_request_attempts = []
        self._last_response_audit = None
        self._last_usage_audit = None
        self._last_response_id = None
        text = self._call(self.system, user)
        decisions = parse_decisions(
            text, getattr(self, "research_selection_catalog", None))
        self._last_response_audit = {
            "id": self._last_response_id,
            "raw_text": text,
            "effective_temperature": (
                None if self._no_temperature
                else float(self.cfg.get("temperature", 0.2))
            ),
            "parsed_decisions": decisions,
        }
        return decisions


def _num(value, default=0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


_RESEARCH_SELECTION_FIELDS = {"strategy_id", "variant_id", "reasoning"}
_LIVE_RESEARCH_CHANGE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:switch|set|change|alter|override|update)\s+(?:the\s+)?"
    r"(?:live|demo)\s+(?:strategy|configuration|config|risk|leverage|capital)\b",
    r"\b(?:place|submit|send|execute|cancel|close|modify)\s+(?:an?\s+)?"
    r"(?:live\s+|demo\s+)?order\b",
))


def _parse_research_selection(
        raw, catalog: dict[str, tuple[dict, ...]]) -> dict:
    requested = raw
    item = {
        "action": "research_selection",
        "validation_status": "REJECTED",
        "strategy_id": "",
        "variant_id": "",
        "reasoning": "",
        "request": requested,
    }

    def reject(reason: str) -> dict:
        item["rejection_reason"] = reason
        return item

    if not isinstance(raw, dict):
        return reject("research_selection must be an object")
    strategy_value = raw.get("strategy_id")
    variant_value = raw.get("variant_id")
    reasoning_value = raw.get("reasoning")
    if isinstance(strategy_value, str):
        item["strategy_id"] = strategy_value.strip()
    if isinstance(variant_value, str):
        item["variant_id"] = variant_value.strip()
    if isinstance(reasoning_value, str):
        item["reasoning"] = reasoning_value
    unknown = sorted(set(raw) - _RESEARCH_SELECTION_FIELDS)
    if unknown:
        return reject(
            "research_selection contains forbidden field(s): "
            + ", ".join(unknown))
    if not isinstance(strategy_value, str) or not item["strategy_id"]:
        return reject("research_selection strategy_id is required")
    if item["strategy_id"] not in catalog:
        return reject("research_selection strategy_id is not registered")
    if variant_value is not None and not isinstance(variant_value, str):
        return reject("research_selection variant_id must be a string")
    if not isinstance(reasoning_value, str):
        return reject("research_selection reasoning must be a string")
    normalized_reasoning = reasoning_value.strip()
    if len(normalized_reasoning) < 10 or len(normalized_reasoning) > 1000:
        return reject(
            "research_selection reasoning must be 10 to 1000 characters")
    if any(pattern.search(normalized_reasoning)
           for pattern in _LIVE_RESEARCH_CHANGE):
        return reject(
            "research_selection reasoning attempts a live/demo execution change")
    variant_id = item["variant_id"]
    if variant_id:
        if variant_id == variants.baseline_variant_id(item["strategy_id"]):
            return reject("research_selection cannot select the baseline")
        eligible = {
            str(candidate["variant_id"])
            for candidate in catalog[item["strategy_id"]]
        }
        if variant_id not in eligible:
            return reject(
                "research_selection variant_id is unknown, multi-axis, "
                "terminal-status, or otherwise ineligible")
    item["validation_status"] = "ACCEPTED"
    return item


def parse_decisions(
        text: str,
        research_catalog: dict[str, tuple[dict, ...]] | None = None
        ) -> list[dict]:
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

    parsed_selection = None
    if "research_selection" in obj:
        catalog = (research_catalog
                   if research_catalog is not None
                   else variants.research_selection_catalog())
        parsed_selection = _parse_research_selection(
            obj.get("research_selection"),
            catalog)

    proposal = obj.get("proposal")
    parsed_proposal = None
    if proposal is not None:
        if not isinstance(proposal, dict):
            return []
        hypothesis_id = str(proposal.get("hypothesis_id") or "").lower()
        setting_id = str(proposal.get("setting_id") or "")
        reasoning = str(proposal.get("reasoning") or "").strip()[:300]
        if len(reasoning) < 10:
            return []
        try:
            number = float(proposal.get("value"))
        except (TypeError, ValueError):
            return []
        try:
            metadata = hypotheses.numeric_setting_metadata(
                hypothesis_id, setting_id)
        except ValueError:
            return []
        if (metadata is None or not math.isfinite(number)
                or number < metadata["minimum"]
                or number > metadata["maximum"]):
            return []
        parsed_proposal = {"hypothesis_id": hypothesis_id,
                           "setting_id": setting_id, "value": number,
                           "reasoning": reasoning, **metadata}
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
                "setup_type": str(d.get("setup_type") or "").lower(),
                "invalidation_anchor": str(
                    d.get("invalidation_anchor") or "").lower(),
                "exit_policy": str(d.get("exit_policy") or "").lower(),
                # Bounded and lowercased like every other model-supplied
                # label. Validity is decided by the hypothesis register, not
                # here: an unregistered id must reach build_setup_plan so it
                # is rejected with a reason rather than silently dropped.
                "hypothesis_id": str(
                    d.get("hypothesis_id") or "").lower()[:60],
                "execution_choice": str(
                    d.get("execution_choice") or "normal").lower(),
                "what_changed_since_last_loss": str(
                    d.get("what_changed_since_last_loss") or "")[:300],
            })
        elif action == "close":
            trigger = str(d.get("close_trigger") or "").lower()
            evidence_change = str(
                d.get("evidence_change") or "").strip()[:500]
            if trigger not in {
                    "thesis_invalidated", "risk_reduction",
                    "stale_position", "profit_protection"}:
                continue
            if len(evidence_change) < 12:
                continue
            item.update({
                "close_trigger": trigger,
                "evidence_change": evidence_change,
            })
        out.append(item)
    if parsed_proposal is not None:
        out.append({"action": "research_proposal", **parsed_proposal})
    if parsed_selection is not None:
        out.append(parsed_selection)
    return out
