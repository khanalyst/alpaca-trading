# OKX AI Trading Agent

A 24/7 autonomous trading agent for OKX USDT-margined perpetual swaps. An LLM
of your choice (Anthropic or OpenAI) acts as the analyst brain; a
deterministic risk engine owns sizing, leverage and circuit breakers; an
execution layer places orders with exchange-side stop-losses so positions
stay protected even if the process dies.

Everything is measured and sized in percentages of live **USDT currency
equity**, so the agent compounds automatically as the trading balance grows
and scales down as it shrinks. Account-wide USD values such as demo OKB are
never treated as USDT capital.

---

> **New to all of this?** [SETUP.md](SETUP.md) is a step-by-step beginner
> guide: installing, getting keys, running 24/7 on any machine or VPS, and
> what it costs per month.
>
> **Deploying to Azure?** [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) walks
> through it from an empty subscription: creating the VM, binding OKX keys to
> a static IP, installing the three services, and verifying shadow evaluation
> is recording.

## Read this first

- Leveraged perpetual futures can lose money faster than they make it, and
  liquidation is a real outcome. No strategy, human or model-driven,
  guarantees profits or a 10x. What this system does guarantee is discipline:
  hard caps the model cannot override, and circuit breakers that stop the
  bleeding on bad days.
- Run `mode: demo` (OKX Demo Trading, paper money) for at least two weeks
  before going live, and start live with money you can afford to lose.
- API key hygiene: create keys with Read + Trade permissions only, never
  Withdraw. Bind keys to your server's IP. Demo and live keys are separate.
- This is software, not investment advice. You own every parameter in
  `config.yaml` and every trade the account takes.

---

## Architecture

```
              every N minutes (config: cycle.interval_seconds)
 ┌─────────────────────────────────────────────────────────────────┐
 │  OKX market data ──► universe filter ──► snapshot (indicators)  │
 │       (top-volume USDT perps only)             │                │
 │                                                ▼                │
 │                            LLM analyst (Claude or OpenAI)       │
 │                       labels setup/anchor/exit as JSON          │
 │                                                │                │
 │                                                ▼                │
 │              versioned STRATEGY + RISK ENGINE (the boss)        │
 │     evidence contract, stops/targets, leverage, risk sizing,    │
 │        exposure caps, cooldowns and circuit breakers            │
 │                                                │                │
 │                                                ▼                │
 │        executor ──► OKX orders with attached SL/TP (server-side)│
 └─────────────────────────────────────────────────────────────────┘
 control: runtime/{demo|live}/state.json + CLI   journal: scoped SQLite
```

| File | Role |
| --- | --- |
| `main.py` | CLI: run, pause, resume, kill, flatten, status, check |
| `agent/engine.py` | The loop: circuit breakers, transfer detection, execution |
| `agent/brain.py` | LLM providers, trader persona prompt, JSON decision parsing |
| `agent/registry.py` | The strategy register: every strategy's mechanism, falsification test and confidence tier. Policy reads it; tier changes are deliberate reviewed code changes backed by persisted research evidence |
| `agent/contracts/` | One evidence contract per strategy. `strategy.id` selects which one runs |
| `agent/strategy.py` | Contract dispatch, deterministic stop/target derivation, setup memory |
| `agent/risk.py` | Deterministic sizing and hard caps |
| `agent/market.py` | Universe builder, indicators, market snapshot |
| `agent/exchange.py` | All OKX calls (via ccxt), orders, kill-switch cancellation |
| `agent/state.py` | Locked state machine, single-process lock, SQLite journal |
| `agent/config.py` | Fail-closed configuration schema and safe ranges |
| `agent/alerts.py` | Retried webhooks with a local failed-delivery queue |

---

## Setup

Requirements: Python 3.12+ on macOS or Linux and an always-on machine (a Linux
VPS is recommended for true 24/7 operation).

```bash
cd okx-agent-crypto
pip install -r requirements.lock.txt
cp .env.example .env      # then fill in your keys
```

### OKX account prerequisites

1. In OKX, make sure derivatives trading is enabled and your account mode is
   Single-currency or Multi-currency margin (Settings -> Account mode).
2. Keep your trading capital as USDT in the Trading account. The agent sizes
   strictly from the USDT currency equity there; disabled BTC/ETH/OKB assets
   are ignored, and the Funding account is invisible to it.
3. Set derivatives position mode to **one-way / net mode** in OKX while the
   account is flat. The agent never changes account settings implicitly and
   refuses to start if OKX reports hedge mode.

### API keys

- Demo: switch OKX to Demo Trading, create API keys there, set `mode: demo`.
- Live: Profile -> API -> create key with Read + Trade only, IP-bound, set
  `mode: live`.

### LLM

Set `llm.provider` and `llm.model` in `config.yaml` and the matching key in
`.env`. The agent makes one model call per cycle (288 calls/day at the
default 5-minute cycle), so pick a model whose per-call cost you are happy
with and check current pricing on the provider's site.

Token costs are kept down three ways: the static system prompt is cached
(explicitly on Anthropic with a 1-hour TTL; automatically on OpenAI with a
stable prompt-cache routing key), the
per-cycle market payload is serialized compactly, and cycles where the model
cannot act (daily loss stop with no open positions) skip the call entirely.
Every call logs total input, fresh (uncached) input, output, cache reads and
cache-hit percentage to `runtime/<mode>/agent.log`. Total input includes
cached tokens, so a 5,000-token total does not mean 5,000 freshly billed
tokens. After the first call, `cache_read` should be a few thousand tokens;
if it stays 0, caching isn't engaging (see the
note in `config.yaml` about per-model cache minimums). Indicative monthly
costs per model are in [SETUP.md](SETUP.md).

---

## Running it

```bash
python main.py check     # validates config, keys and OKX connectivity
python main.py run       # starts the loop in the foreground
```

For 24/7 operation, run it under tmux:

```bash
tmux new -s trader
python main.py run
# detach with Ctrl+B then D; reattach later with: tmux attach -t trader
```

Or as a systemd service (`/etc/systemd/system/okx-trader.service`):

```ini
[Unit]
Description=OKX AI Trading Agent
After=network-online.target

[Service]
WorkingDirectory=/opt/okx-ai-trader
ExecStart=/usr/bin/python3 /opt/okx-ai-trader/main.py run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

A drawdown self-kill sets the state to KILLED; restarts (manual or systemd)
are refused until you pass `--acknowledge-kill`, which forces a human review
between a blow-up and the next trade.

---

## Running the tests

The suite is plain `unittest`, so it needs nothing beyond the production
dependencies:

```bash
python -m unittest discover -s tests -t . -q     # full suite
```

Two alarming-looking lines in the output — `event journal write failed: disk
full` and `Corrupt state detected ...` — are tests proving those guards fire,
not failures. The verdict is the final `OK`.

`pip install -r requirements-dev.txt` adds pytest if you prefer its output;
it is deliberately absent from `requirements.lock.txt`, because a trading
host should not carry a test runner.

## Controls

| Command | What it does |
| --- | --- |
| `python main.py status` | State, equity, day PnL, drawdown, open positions |
| `python main.py strategies` | List the strategy register: every registered strategy, its confidence tier, whether it is runnable, and whether it may run live. Add `--verbose` for each one's mechanism and falsification test. Works without `.env`. |
| `python main.py pause` | No new positions. Existing ones keep their exchange-side SL/TP; max-hold and margin guards still run. No LLM calls (saves cost). Survives crashes and restarts until `resume`. |
| `python main.py pause --flatten` | Pause and close everything immediately |
| `python main.py resume` | Back to full trading |
| `python main.py flatten` | Close all positions, cancel all orders, then pause |
| `python main.py kill` | Kill switch: cancel everything, close everything, stop. Restart requires `run --acknowledge-kill`. |
| `python main.py kill --keep-positions` | Stop the loop but leave positions open (their SL/TP orders remain live on OKX) |

Ctrl+C on a foreground loop is treated as `pause`: the process exits, state
becomes PAUSED, and open positions remain protected by their server-side
stops. Unlike an explicit `pause` command, a Ctrl+C pause does not stick:
the next `run` resumes full trading.

The agent also stops itself:

- Daily loss limit (`daily_loss_limit_pct`, default 5%): no new entries until
  the next UTC day; the model may still close positions.
- Max drawdown (`max_drawdown_pct`, default 15% from the high-water mark):
  flattens everything and self-kills.

---

## Deposits and withdrawals

You can add or remove funds at any time; the agent is designed for it.

- Sizing is recomputed from live USDT equity every cycle, so a USDT deposit
  compounds into larger positions on the next cycle and a withdrawal shrinks
  them. Price changes in disabled non-USDT assets do not affect the agent.
- The agent reads the OKX ledger each cycle and rebases its benchmarks by the
  net transfer, so a deposit is not mistaken for profit and a withdrawal is
  not mistaken for a crash (neither will falsely trip the drawdown or daily
  stop, and profits are still tracked honestly).
- If a withdrawal squeezes margin, the margin guard
  (`max_margin_usage_pct`, default 60%) closes the largest position(s) until
  usage is healthy again.
- Best practice for large withdrawals: `python main.py pause --flatten`,
  withdraw, then `python main.py resume`.

Note: transfers move between your OKX Funding and Trading accounts; the agent
only sees (and trades with) the Trading account balance.

---

## How the agent thinks

Every cycle (default: every 5 minutes) the agent runs the same loop:

1. **Sync USDT equity and money movements.** It reads the Trading account's
   USDT currency equity and checks the OKX ledger for USDT
   deposits/withdrawals, rebasing its benchmarks so a transfer is never
   mistaken for profit or a crash. Other currencies are excluded.
2. **Check the circuit breakers first.** If the account is down past the
   daily loss limit, no new positions open until the next UTC day. If it's
   down past the max drawdown from its high-water mark, it flattens
   everything and self-kills (see Caveats).
3. **Refresh the tradable universe (hourly).** Candidates are ranked by
   contract-normalized 24h quote volume above
   `min_24h_quote_volume_usd` (default $50m), but a slot is filled only after
   the instrument is confirmed by OKX's private account catalogue as
   account-available, active, linear, USDT-settled, and category `1`
   (**crypto**), with at least `min_history_candles` completed bars on every
   configured timeframe. An ineligible high-volume symbol is skipped and the
   next ranked eligible symbol fills the slot. The full inclusion/exclusion
   audit is journaled as `universe_selection`. Even an empty result is cached
   until the next refresh, preventing a bad/new listing from being fetched
   again every five minutes.
4. **Reconcile and housekeep open positions.** The exchange is authoritative:
   each cycle (including the first after a restart) matches live positions to
   durable trade IDs, verifies stop-loss/take-profit coverage, restores known
   protection where possible, and closes any position that cannot be verified
   to have a stop. A valid OKX liquidation estimate must also remain beyond
   the stop by `min_stop_liquidation_buffer_pct`; otherwise the position is
   closed. For an adopted/restarted position, opening time is recovered from
   OKX's position creation time or fill history. If age cannot be proven, the
   agent pauses for operator review instead of pretending it opened "now" and
   silently resetting the max-hold clock. It then force-closes anything past
   `max_hold_hours`, and closes the
   largest position(s) until both initial-margin use is below
   `max_margin_usage_pct` and adjusted-equity/maintenance-margin is above
   `min_maintenance_margin_ratio`.
5. **Ask the brain.** It builds a compact snapshot per symbol — price, spread,
   correctly contract-normalized 24h quote volume, completed-hour relative
   volume, current and historical funding, perp/index basis, authenticated
   account taker fee, open interest, RSI, current ATR%, ATR versus recent
   history, trend on 15m/1h/4h, completed 15m/1h signal timestamps, 15m/1h
   momentum, a fresh break of the prior completed 20-candle range,
   stabilization evidence, short and shrinkage-adjusted BTC
   correlation/beta/downside correlation, an explicit regime classification,
   range position, recent swing distances and 1h EMA20 distance — plus the
   BTC-wide regime and live portfolio. The LLM remains the discretionary
   analyst: it can return no trade or label an idea as `trend_continuation`,
   `range_breakout`, `funding_squeeze`, or (in demo only) `other`; it chooses
   direction, confidence, invalidation anchor, exit policy and whether a prior
   liquidity failure merits one smaller retry. Open positions include their
   original entry thesis/evidence; a model close must name a structured close
   trigger and the specific original-versus-current evidence change. It does
   **not** choose numeric size, leverage, stop or target.
6. **The strategy and risk layers dispose.** The versioned momentum contract
   ([agent/strategy.py](agent/strategy.py)) checks that a recognised label has
   minimum supporting evidence: continuations require aligned 1h/4h direction
   plus a completed 15m resumption; breakouts require a fresh completed-candle
   range break plus volume/momentum; funding squeezes require extreme
   historical funding, basis/open-interest context and price stabilization.
   It enforces an extreme no-chase boundary, and
   converts the chosen anchor/exit policy into reproducible stop and target
   distances. Structure stops sit beyond the recent swing plus an ATR buffer
   and can never be tighter than the configured ATR floor. Leverage comes from
   `risk.entry_leverage`. The risk engine then discards low-confidence,
   duplicate, held or cooling-down ideas and **sizes from the derived stop plus
   actual/fallback fees, live spread, adverse funding and expected stop
   slippage so the all-in stop loss stays within `risk_per_trade_pct` of
   equity**. Per-position, whole-book, total planned stop-risk, net-direction
   and BTC-beta-weighted caps still apply. Demo-only `other` ideas retain an
   agentic experimental lane but receive a separate strategy identity and a
   smaller risk budget.
   Every symbol is evaluated at most once for each completed 15-minute signal
   candle; completed setups also receive semantic cooldown. After a losing
   thesis, re-entry additionally needs the configured delay, a fresh completed
   1h bar, changed objective evidence and an explicit model explanation.
   Closes execute
   first, then surviving opens are validated sequentially in confidence order
   against the exposure created by earlier fills.
7. **Execute with protection.** Orders use client IDs and are never blindly
   retried after an ambiguous network response. Actual terminal order status,
   filled quantity, average fill, fees, and partial fills are verified. Orders
   go to OKX with **exchange-side stop-loss and take-profit attached**, so
   positions stay protected even if this process dies. If a stop-loss can't
   be verified after an entry fills, the agent immediately closes that
   position rather than run it naked. Entries are marketable IOC limits:
   current spread and order-book depth must pass configured caps, and the
   exchange-side limit prevents a sudden move from producing unlimited entry
   slippage. A depth rejection is fed into the next portfolio prompt. The
   model may choose another setup, stay flat, or make one explicit smaller
   retry; a repeated failure creates a persisted temporary liquidity backoff.
   Other OKX entry failures preserve the safe exchange code/message and create
   a separate persisted exponential backoff, so the same rejected symbol is
   not submitted every cycle. Instrument/account incompatibilities remain
   blocked for at least one universe refresh. No path falls back to an entry
   without attached protection.

The LLM never touches the exchange, never sizes a position, and never
overrides a cap. It is an idea generator inside hard, code-enforced rails.

### How many positions can it hold at once?

- **At most `max_concurrent_positions` open positions simultaneously**
  (default **3**).
- **One position per symbol.** It never adds to or averages into a symbol it
  already holds — a second proposal on the same symbol is rejected.
- **Each new cycle it may open at most `max_concurrent_positions` minus what
  it already holds.** If 3 are open, it can only close or hold until a slot
  frees up.
- **Total risk/size is capped five ways regardless of count:** each position is
  at most `max_position_notional_pct` of equity (default 30%), the whole book
  is at most `max_gross_exposure_pct` of equity (default 150%), and the *net
  direction* (long notional minus short notional) is at most
  `max_net_direction_pct` of equity (default 100%), planned all-in stop risk
  is at most `max_total_open_risk_pct` (default 3%), and signed BTC-beta
  exposure is capped at `max_btc_beta_exposure_pct` (default 100%).

So the maximum book is 3 positions, each on a different symbol, each ≤30% of
equity, ≤150% total notional, ≤3% planned stop risk, ≤100% net direction and
≤100% BTC-beta-weighted exposure.

That 30% is not arbitrary. Per-position initial margin is
`max_position_notional_pct / entry_leverage`, so a full book uses
`max_concurrent_positions ×` that much. At 3 × 30% ÷ 2 = 45% against a 60%
`max_margin_usage_pct`, the book can lose about a quarter of its value before
the margin guard starts force-closing positions. Config validation rejects any
combination that leaves less than 20% headroom, because without it any
unrealized loss on a full book would trip the guard and close the largest
position for arithmetic reasons rather than strategy ones.

---

## Configuration reference — where to change what

Everything lives in [`config.yaml`](config.yaml). Edit it, then restart the
agent. Grouped by what you're trying to change:

**Switch demo ↔ live** — `mode` (`demo` | `live`). Demo and live use separate
API keys; the mode must match the keys in `.env`.

**Change the brain (`llm:` block)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `provider` | `anthropic` | `anthropic` or `openai` |
| `model` | `claude-sonnet-4-6` | Any model from that provider (e.g. `claude-opus-4-8`, `gpt-4.1`) |
| `temperature` | 0.2 | Creativity; auto-ignored on models that reject it |
| `max_tokens` | 2000 | Cap on the model's reply length |

**Change what it watches (`universe:` block)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `top_n` | 10 | Trade only the N highest-volume USDT perps |
| `min_24h_quote_volume_usd` | 50000000 | Liquidity floor; filters thin/new coins |
| `min_history_candles` | 60 | Completed bars required on every timeframe before a symbol can occupy a universe slot |
| `denylist` | `[]` | Symbols to ban, e.g. `["DOGE/USDT:USDT"]` |
| `refresh_minutes` | 60 | How often the volume ranking is rebuilt |

**Change how often it thinks (`cycle:` block)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `interval_seconds` | 300 | Seconds between **housekeeping** cycles: reconciliation, circuit breakers, the max-hold force close |
| `decision_interval_seconds` | *(unset)* | Seconds between **decisions** (the snapshot build and LLM call). Unset means every cycle decides, which is the pre-B9.2 behaviour. Set to `900` to align with the 15m signal bar |
| `timeframes` | `[15m,1h,4h]` | Candle timeframes fed to the model |
| `candles` | 120 | Candles per timeframe; also excludes coins lacking history |

**The cost lever is `decision_interval_seconds`, not `interval_seconds`.**
Measured over the corpus, 66.5% of cycles observe only signal bars that were
already evaluated, and an LLM call on one of those cannot produce a fresh
evaluation for any symbol — `strategy.evaluated_signal` blocks it. Aligning
the decision cadence to the bar is roughly a **3× reduction in LLM spend**.

Raising `interval_seconds` instead would buy the same saving and take the
drawdown breaker, the daily-loss stop, reconciliation and the max-hold force
close from a 5-minute reaction time to a 15-minute one. That is not an
acceptable price for a cost reduction, which is why the two are separate.
Verify the share on your own journal with `python research.py cadence`.

**Choosing a strategy (the register)**

`strategy.id` selects which registered strategy runs. `agent/registry.py`
holds one entry per strategy, and each entry must state a **mechanism** (who
loses the money and why they cannot stop) and a **falsification** (what
observation would kill it), plus a **confidence tier** backed by persisted
research and changed through review:

| Tier | Meaning |
| --- | --- |
| `T0_REJECTED` | Failed a gate, or its placebo scored close to it |
| `T1_HYPOTHESIS` | Mechanism stated, nothing tested yet |
| `T2_CANDIDATE` | Cleared exploratory gates, or has promising authoritative evidence that is not yet sufficient for capital |
| `T3_VALIDATED` | Cleared an authoritative, G2-valid recorded-replay confirmation with persisted evidence |
| `T4_CONFIRMED` | Forward evidence agrees, at the required sample size |

Demo runs only a strategy whose deterministic evidence contract and analyst
prompt/schema are both implemented. Other implemented contracts remain
**shadow-only** until the analyst can emit their setup types correctly.
Paper trading a known-negative but fully runnable strategy is still a useful
operations rehearsal. **Live requires `T3_VALIDATED` or better**, enforced in
`agent/config.py`. The shipped `momentum`/`phase1-v3` is the only analyst-ready
strategy and is `T0_REJECTED` on the evidence in
`research/results/edge-audit-2024-2026/`, so switching `mode: live` with it
active fails validation and prints the reason. That is intended behaviour,
not a bug to work around.

**Exit policies**

The model chooses `exit_policy` and deterministic code converts it into a
target. Two are accepted: `fixed_rr` (target at `fixed_reward_risk` x the
stop) and `extended_rr` (at `extended_reward_risk`).

A third, `structure_target`, was **removed in batch 6.1**. It computed
`max(stop x fixed_rr, distance to the recent extreme)`, and in both setups it
was designed for the first term always won — a `range_breakout` fires with
price already at the highs, so the remaining distance is near zero. It was
therefore identical to `fixed_rr` precisely where it was meant to differ. An
inert choice is worse than no choice: it makes the decision space look richer
than it is and attributes outcomes to a policy that never applied.

**Experimental setups (`hypothesis_id`)**

`setup_type: "other"` no longer accepts anything the model invents. An
experimental setup must name a hypothesis registered in
`agent/hypotheses.py`, each of which states a mechanism, a falsifier and its
own contract, and each of which gets **its own attribution** — so ten
experiments produce ten rows rather than one average describing none of them.
The list is versioned into the prompt; an unregistered id is rejected.

Gated by `strategy.allow_experimental_setups_in_demo` (default `false`) and
demo-only regardless.

**Shadow evaluation of parameter variants (`research:` block)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `shadow_enabled` | `false` | Master switch. Absent or false is a complete no-op |
| `shadow_variants` | `[]` | Variant ids from `research/variants.yaml` to evaluate each cycle |
| `shadow_budget_ms` | 500 | Wall-clock budget; overrun abandons the rest and journals it |
| `shadow_llm_variants` | `[]` | **Capped at 2.** Each costs a full extra model call per cycle (~288/day) |

The evaluator is constructed from configuration alone — it receives no
`Exchange`, no state handle, no journal writer — and a test walks its
attribute graph to assert no exchange is reachable from it. It runs after
trading decisions are committed, inside `try/except`, and writes only
`variant_shadow_decision` events.

**Passive entry (`execution.maker_first_*`)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `maker_first_enabled` | `false` | H-K(ii). **The exchange primitive exists; the entry-path integration deliberately does not** — see [`research/plan/B7.5-record.md`](research/plan/B7.5-record.md) |
| `maker_first_wait_seconds` | 20 | Bounded to 120, well inside a 15m bar |

```bash
python main.py strategies --verbose   # the register, tiers and mechanisms
```

**Shadow evaluation.** Every cycle, the agent evaluates every implemented
deterministic contract against the same snapshot and journals what each one
would have done — not only the strategy holding capital. Strategy shadows use
`strategy_shadow_decision` / `strategy_shadow_summary`; parameter variants use
the separate `variant_shadow_decision` schema, so the two populations cannot
be pooled accidentally. Every implemented contract can collect raw firing
evidence, but forward expectancy is emitted only when its registry entry has
a validated outcome model; currently that is momentum only. This prevents a
carry or multi-day mechanism from inheriting momentum's exit horizon and cost
model. Shadowing costs no extra LLM call and places no orders.

The active strategy is shadowed too, which is the part that is easy to miss:
comparing what the contract fired on against what the account actually opened
is the only direct measurement of what the LLM layer contributes. Offline
research can bound that contribution; it cannot observe it.

```bash
# resolve shadow decisions against real subsequent bars
python research/export_live.py --mode demo --data runtime/research/data
# score every strategy through the six gates, with forward evidence attached
python research/tournament.py --data runtime/research/data
```

Shadow outcomes are resolved with the same simulator the backtests use, so
the forward and backtest numbers are directly comparable. A **sign
disagreement between them is the earliest available warning that a backtest
was fitted**, and the report flags it long before the sample is large enough
to prove anything. `T4_CONFIRMED` is the one tier offline data cannot grant:
it requires forward evidence agreeing in sign, over at least as many trades
as the detectability gate computed.

**Change the momentum contract (`strategy:` block)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `id` / `version` | `momentum` / `phase1-v3` | Must name an entry in the strategy register (`agent/registry.py`). Stored with every run, setup and trade. Run `python main.py strategies` to list them |
| `signal_timeframe` | `15m` | Must equal the registered spec's timeframe; one symbol evaluation per completed bar |
| `setup_cooldown_minutes` | 45 | Blocks a completed semantic setup before it can be reused |
| `setup_memory_hours` | 72 | Persists bounded setup/idempotency history across restarts |
| `loss_reentry_min_minutes` | 60 | After a loss, also require a fresh completed 1h bar, changed objective evidence and an explicit model explanation |
| `min_stop_atr_multiple` | 1.0 | Minimum deterministic stop width |
| `min_hold_minutes` | 90 | Floor on discretionary model closes. Exchange-side SL/TP and the max-hold timer are unaffected, and `risk_reduction` closes are exempt. Measured forward return is most negative ~30 minutes after entry, so sub-hour exits removed the payoff tail while still paying a full taker round trip |
| `structure_buffer_atr_multiple` | 0.15 | Buffer added beyond the selected swing invalidation |
| `hard_max_entry_extension_atr` | 1.2 | Absolute no-chase boundary from the 1h EMA20. Lowered from 2.5: the model was treating the ceiling as a target and entering at maximum extension, which is where mean reversion is most likely |
| `breakout_range_threshold_pct` / `breakout_min_relative_volume` | 85 / 1.0 | Minimum 24h range position and participation for a `range_breakout` |
| `funding_extreme_pct_per_8h` | 0.01 | Absolute funding floor for `funding_squeeze`, as an **8h-equivalent** rate: a 4h contract's rate is doubled before comparison. Whether funding is extreme *for that instrument* is decided by `funding_percentile_30`; this only screens out near-zero funding |
| `fixed_reward_risk` / `extended_reward_risk` | 3.0 / 4.0 | Code-derived target multiples selected through the model's exit policy. 3R beat 2R in all four matched comparisons at the 48h hold; beyond 3R the gain reverses out-of-sample |

**Change how aggressive it is (`risk:` sizing)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `max_leverage` | 3 | Validation ceiling. **Hard ceiling of 10 in code** regardless of config |
| `entry_leverage` | 2 | Actual deterministic entry leverage; the model cannot change it |
| `risk_per_trade_pct` | 1.5 | Maximum expected equity loss at a stop, including configured costs |
| `experimental_risk_per_trade_pct` | 0.5 | Smaller budget for demo-only `other` setups, attributed as `momentum-experimental` |
| `max_total_open_risk_pct` | 3.0 | Cap on the sum of planned all-in stop losses across held positions |
| `max_position_notional_pct` | 30 | Per-position notional cap, % of equity. With `entry_leverage` and `max_concurrent_positions` it also sets full-book margin usage, which validation keeps ≤80% of `max_margin_usage_pct` |
| `max_gross_exposure_pct` | 150 | Whole-book notional cap, % of equity |
| `max_net_direction_pct` | 100 | Cap on net long-minus-short notional, % of equity |
| `max_btc_beta_exposure_pct` | 100 | Cap on signed BTC-beta-weighted notional; insufficient histories conservatively use beta 1 |
| `min_btc_beta_samples` | 24 | Minimum observations before measured BTC beta is trusted |
| `max_concurrent_positions` | 3 | Max simultaneous open positions |
| `max_same_direction_positions` | 2 | Max positions on the same side at once. The notional caps do not bind here: 3 x 30% = 90% net passes a 100% net-direction cap, so a wholly one-sided book was previously legal |
| `max_setups_firing_for_entry` | 4 | Refuse all new entries when more instruments than this satisfy a contract in one cycle. Crowded bars measured -0.3475%/trade against -0.1627% on quiet ones |
| `min_confidence` | 0.65 | Proposals below this are discarded |
| `max_hold_hours` | 48 | Stale positions are force-closed. **Capped by the registered strategy's own ceiling** (48h for `momentum`), so a day-trading contract still cannot become a multi-day one, while a genuinely multi-day strategy declares its own limit. 48 beat 24 in 8/8 matched walk-forward cells |

**Change the safety brakes (`risk:` circuit breakers)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `daily_loss_limit_pct` | 5 | Down this much on the day → no new entries until next UTC day |
| `flatten_on_daily_stop` | false | `true` also closes open positions when the daily stop trips |
| `max_drawdown_pct` | 15 | Down this much from the high-water mark → flatten everything and self-kill |
| `max_margin_usage_pct` | 60 | Above this, close the largest position(s) to reduce margin |
| `min_maintenance_margin_ratio` | 3.0 | Minimum conservative adjusted-equity/MMR ratio; OKX's liquidation boundary is 1.0 |
| `min_stop_liquidation_buffer_pct` | 1.0 | Required distance between the stop and a valid OKX liquidation estimate, as % of mark |
| `cooldown_minutes_after_loss` | 45 | Per-symbol timeout after a losing close |

**Change execution (`execution:` block)** — `slippage_guard_pct` (default 0.5)
rejects stale analyses, `max_spread_pct` (0.15) rejects a wide current market,
and `max_order_book_slippage_pct` (0.35) is the depth-test threshold, hard IOC
entry-price boundary, and a reserved component of all-in sizing.
`max_market_data_age_seconds` (10) rejects stale order books.
`fill_timeout_seconds` (12) bounds fill
verification before the agent cancels any unfilled remainder. Liquidity
feedback remains visible to the model for
`liquidity_feedback_ttl_minutes` (30). It may make
`liquidity_retries_before_backoff` (1) smaller retry, capped to
`liquidity_depth_buffer_pct` (70) percent of the observed safe depth; another
failure blocks that direction in the symbol for
`liquidity_backoff_minutes` (15). Every retry still uses a fresh order book.
Non-liquidity execution failures start at
`entry_failure_backoff_minutes` (15), double on consecutive failures up to
`entry_failure_backoff_max_minutes` (60), and remain visible for
`entry_failure_ttl_minutes` (240). These records survive restarts.

**Cost assumptions (`trading_costs:` block)** — the configured taker fee is a
fallback when the authenticated OKX account fee cannot be read. Expected stop
slippage, minimum funding intervals and expected holding hours are sent to the
LLM alongside live funding history and basis. Deterministic sizing uses the
actual/fallback fee and derives likely adverse funding settlements; costs do
not replace technical stop placement.

**Human alerts (`alerts:` block)** — generic JSON, Slack or Discord webhooks
are optional in demo but mandatory in live. Live startup sends a preflight
message and refuses to trade unless it is acknowledged. Delivery is retried
three times; exhausted messages are stored in
`runtime/<mode>/failed_alerts.jsonl`.

> **To turn aggression up**, raise `entry_leverage` (without exceeding
> `max_leverage`), `risk_per_trade_pct`, and `max_gross_exposure_pct` — but
> understand how they compound: 3 positions at
> 2% risk each is a 6% day if everything stops out at once. A few limits are
> hard-coded in [agent/risk.py](agent/risk.py) and cannot be raised via
> config: leverage is capped at 10, stop distances must be between 0.2% and
> 15%, and positions below ~$10 notional are skipped.
>
> **Know your *effective* risk per trade.** Sizing takes the *minimum* of
> several caps, so `risk_per_trade_pct` is a ceiling, not a promise: with a
> typical 1–2% stop, the 30% per-position notional cap binds first and the
> actual loss on a stop-out is ~0.5–0.8% of equity, not 1.5%. The risk
> target only fully binds for stops wider than ~5%
> (= risk 1.5 ÷ cap 0.30). You do not have to work this out yourself — every
> entry records `sizing_constraint` (which cap actually decided the size) and
> `effective_risk_pct_equity` (what the trade really risks), and both appear
> in the `OPENED` log line and the journal. If `sizing_constraint` is not
> `risk_per_trade_budget`, raising `risk_per_trade_pct` alone will change
> nothing. After an order-book rejection, the model may choose
> `retry_smaller`; code—not the model—calculates the permitted reduced size.

---

## The research layer

Two evidence paths live in this repository and **they are not equals**. The
distinction decides what is allowed to put capital at risk, so it is worth
stating before the commands.

**Journal replay is the authoritative path, after G2 passes.** Every cycle the
agent journals the per-symbol snapshot and portfolio state the model received.
Replay recomputes setup evidence under each candidate configuration instead
of reusing cached baseline evidence, and outcome resolution excludes bars that
precede the recorded decision/entry time. It still has explicit state-model
limits (including loss-cooldown feedback and some universe-selection effects),
which is why fidelity is measured rather than assumed. Until G2 passes on the
exact current corpus, replay output is diagnostic, not promotion evidence.

**The OHLCV backtest is exploratory.** It recomputes indicators from
downloaded candles, and some snapshot fields (`range_pos_pct`,
`chg_24h_pct`, `vol_24h_musd`) come from the live 24h ticker and cannot be
reconstructed afterwards. Its evidence is enough to withhold capital from a
strategy and **not** enough to raise a tier. See
[`research/plan/RECONCILIATION.md`](research/plan/RECONCILIATION.md).

> A tier may be **lowered** on exploratory evidence and **raised** only on
> journal-replay evidence. Withholding capital on suspicion is the right way
> to be wrong; granting it on suspicion is not.

### The research CLI

```bash
python research.py readiness               # which gates are open or blocked
python research.py corpus stats            # how much data is there?
python research.py replay --check-fidelity # gate G2 — exits non-zero on failure
python research.py funnel                  # gate G4 — the veto distribution
python research.py cadence                 # B9.2 evidence
python research.py three-arm               # H-E: does the LLM earn its keep?
python research.py sweep research/sweeps/regime_conditioning.yaml
python research.py report                  # regenerate findings/ scorecards
```

Nothing here touches the trading path or places an order. Commands read the
journal and a local price cache; `replay --check-fidelity` appends a durable
`research_gate_result` audit row, while sweeps append atomic run/metric/finding
records to `findings.db`. Successful evaluations and `research.py report`
also maintain `findings.db.backup`. No command calls an LLM or mutates trading
state.

`research/nightly.sh` runs the whole sequence (authoritative path first) and
is wired to `deploy/okx-research.timer`.

### Gate G2 is a full stop

`replay --check-fidelity` asserts that replaying the baseline reproduces at
least 99% of the agent's own recorded decisions. If it does not, the replay
is wrong and **every number downstream of it is worthless** — and the failure
is silent, because a broken replay still emits a clean, plausible,
internally consistent table describing a system nobody runs.

| Exit | Meaning |
| --- | --- |
| 0 | Reproduced ≥ 99%. Proceed. |
| 2 | **Failed.** Stop and explain every mismatch before trusting anything. |
| 4 | **Collecting/vacuous** — fewer than 100 recorded proposals, including an empty corpus. Not enough evidence for the ratio. |
| 5 | Fidelity passed but its durable audit record could not be stored. Downstream work stays blocked. |

A pass is stored with the proposal count, latest proposal timestamp, strategy
configuration fingerprint and fidelity-code fingerprint. New proposals or a
decision/replay code or strategy-config change makes that pass stale and
returns G2 to `READY` until it is rerun.
`three-arm`, `funnel`, and `sweep` enforce G2 themselves; a caller cannot skip
the gate by invoking those commands directly.

### The three arms

`three-arm` answers whether the LLM's value is in *selection*, in
*rejection*, or absent. A two-arm test cannot tell those apart: if the model
is a poor selector but a good vetoer, the pooled LLM arm looks mediocre and
the conclusion drawn is "it does not earn its keep" — the wrong conclusion
from the right data, and an expensive one.

| Arm | Proposals from |
| --- | --- |
| A `deterministic` | The contract fired, so take it. The floor. |
| B `recorded_llm` | The model's own recorded decisions. |
| C `deterministic_vetoed` | Contract proposes; the model may only *suppress*. |

### Variants, sweeps and the promotion protocol

A **variant** is a named parameter hypothesis in
[`research/variants.yaml`](research/variants.yaml) — `momentum.rr.fixed_2_5`,
not a sixteen-character hash. It must state what it claims, and an override
naming a path that does not exist is refused at registration rather than
after a week of replay reporting no difference.

Two kinds of axis, and only one divides the sample:

- A **parameter axis** generates variants. Three settings at 100 round trips
  each is 300 before it can conclude anything.
- A **conditioning axis** partitions results. One replay produces every
  bucket and reuses trades that already exist, so it is affordable at samples
  where a parameter sweep is not. Buckets must be pre-registered.

[`research/protocol.md`](research/protocol.md) is the comparison rule, applied
by code. Selection occurs only on the 70% fit window; the 30% confirmation
window cannot choose its own winner. Promotion needs all five criteria (≥100
round trips **for every setting**, ≥3 settings, expectancy CI clearing the
baseline, drawdown no worse, and a positive out-of-sample confirmation delta).
Rejection also requires every setting to meet the sample floor — a hypothesis
is never killed by one badly chosen or under-observed value. Exploratory OHLCV
gates are capped at `T2_CANDIDATE`; no research command silently rewrites the
static strategy tier in `agent/registry.py`.

**`INSUFFICIENT_SAMPLE` means the question is open, not answered in the
negative.** It will be returned repeatedly and it is the most important
verdict the harness produces.

### What is recorded and never shown to the model

Batch B0.5 journals fields months before anything reads them, because a
snapshot taken without book depth in it can never be made to have book depth
in it. `book_state` (per symbol, per cycle) and `snapshot_enrichment`
(realised-vol ratio, UTC hour, BTC reference returns) are written every
cycle and cost **1.88 MB/day** plus 3,168 OKX calls/day.

They are withheld from the prompt by construction: enrichment lives under
`_enrichment` keys and `brain.withhold_enrichment()` strips them on the way
to the provider. Changing the prompt changes model behaviour, which forks the
comparability of every observation either side of the change — so showing the
model a new field is a deliberate, versioned act with its own batch, not a
side effect of starting to record.

### What is still pending, and how you will know

Two things are finished as code and unfinished as evidence. Both wait on the
agent running, and both fail the same quiet way if nobody checks — someone
eventually decides they have waited long enough.

`python research.py readiness` answers it mechanically instead:

```
  GATE   STATUS      WHAT IT MEANS
  G1     PASS        B0.5 enrichment changes no decision
  G2     ....        Replay reproduces the agent's own decisions
                     12 recorded proposals; ~100 needed before a 99% ratio
                     is meaningful (at this n one mismatch reads as 92%)
                     -> keep the agent running
  B7.5   BLKD        Passive entry validated on a live account
                     gate G2 must pass first
```

| Status | Meaning |
| --- | --- |
| `PASS` | Satisfied, on evidence |
| `READY` | Enough data exists, but no persisted pass covers the exact current corpus |
| `....` | Collecting. The shortfall is counted, never estimated |
| `BLKD` | Something upstream must happen first |
| `FAIL` | It ran and did not pass. **A stop, not a delay** |

The command exits non-zero on any failure, so the nightly run surfaces it
rather than printing a report that looks fine.

#### G2 — replay fidelity

**Why it waits:** the 99% threshold is a ratio, so it is meaningless on a
handful of events. At ten recorded proposals a single mismatch reads as 90%
and fails a replay that is perfectly correct.

**How it completes:** run the agent. At roughly fifteen proposals a day the
gate becomes meaningful in about a week. `readiness` counts them and says how
many are missing.

**What "ready" does not mean:** that it passed. `READY` means the sample is
sufficient but no persisted pass covers this exact corpus. `PASS` is reserved
for a successful fidelity run whose audit row still matches the corpus.

#### B7.5 — passive entry

**Why it waits:** fill rate cannot be inferred from history, because the
passive order was never there. Every test runs against a mock, which proves
the logic and says nothing about OKX's real post-only semantics or cancel
races under load.

**How it completes:** G2 first, then `execution.maker_first_enabled: true` on
demo, then twenty clean attempts. `readiness` tracks the count.

**The one thing that fails it:** an order that could not be cancelled and may
still be resting. That is the single failure mode the design forbids, and it
fails the gate immediately regardless of anything else.

#### Gates still collecting

| Gate | Waiting for | Roughly |
| --- | --- | --- |
| **G4** | Any corpus at all, to publish the funnel | Days |
| **G5** | 300 matched round trips to rank the breakout discriminator | Weeks. The contract default is `none`, so nothing is baked in meanwhile |
| **G6** | 150 observations per conditioning cell for H-G / H-H | ~3 months. Cascades are infrequent by construction |

#### Known gaps, stated rather than hidden

- **The replay does not simulate the loss cooldown.** It replays setup memory
  — per-bar idempotency, semantic cooldown, failed-thesis re-entry — but the
  live engine also cools a setup down after a losing exit, and the replay has
  no PnL to mark with. Expect a small residual excess of replayed proposals
  after a losing streak.
- **`select_universe` needs an authenticated account endpoint,** so it cannot
  be exercised without credentials.
- **`research.py` and the `research/` package share a name.** Python resolves
  the package first, so the CLI is a script and cannot be imported by name.

## Caveats and limitations

Read these before running anything with real money.

- **Leverage can lose money faster than it makes it, and liquidation is
  real.** No strategy — human or model-driven — guarantees profit or a "10x".
  The caps and breakers here enforce discipline; they do not promise gains.
- **The brain is a probabilistic model, not an oracle.** It can propose bad
  trades, misread a regime, or produce malformed output. The risk engine
  bounds the damage (sizing, caps, cooldowns) but cannot make a losing
  strategy win.
- **A webhook is mandatory in live, but it is not uptime monitoring.** A dead
  machine cannot send its own alert. Keep external process/host monitoring as
  a separate layer and inspect `runtime/<mode>/failed_alerts.jsonl`.
- **The drawdown self-kill halts the agent until a human intervenes.** After a
  15% drawdown it flattens and refuses to trade again until you run
  `--acknowledge-kill`. This is intentional (a human should review a blow-up),
  but it means the agent can sit dead through a recovery.
- **It needs an always-on machine and network.** If the process dies, no new
  decisions happen. Open positions remain protected by their exchange-side
  stop-losses on OKX, but nothing new is managed until it's back up.
- **Demo results ≠ live results.** Demo fills are idealized. The live executor
  records actual fills, fees and partial fills, but real slippage and funding
  still make live outcomes different from demo.
- **It costs money even in demo.** The LLM calls are real (~$50–95/month at
  the default cycle). Only the trading is simulated in demo mode.
- **One OKX account per running instance,** and it only sees the **Trading**
  account balance (not Funding). For multiple strategies, use OKX
  sub-accounts (see below).
- **The daily reset is UTC,** not your local midnight. The daily loss limit
  and PnL are measured against the start of the UTC day.
- **This is software, not investment advice.** You own every parameter in
  `config.yaml` and every trade the account takes.

---

## The journal

Everything is recorded in `runtime/<mode>/journal.db` (SQLite): every decision,
rejection, setup lifecycle, order submission/recovery result, trade, transfer
and equity snapshot. Runs and records carry run/cycle IDs, strategy and prompt
versions, config/code fingerprints and the equity-basis segment ID. The agent
checks that the journal is writable before starting and pauses immediately on
any later journal write failure. Existing journals migrate in place;
plain-text logs rotate at 10 MB.

Each model cycle also records the exact provider request, every retry attempt,
the provider response ID, raw response text and parsed decisions. This makes
the nondeterministic LLM layer auditable after prompts or settings change;
protect and back up the journal as trading-sensitive data.
Structured liquidity rejections and their temporary backoffs are journaled as
`entry_liquidity_rejected` and `entry_liquidity_backoff` events. Their active
state lives in `runtime/<mode>/state.json`, not in the provider's prompt cache.
Universe eligibility is journaled as `universe_selection`; non-liquidity OKX
entry failures are journaled as `entry_execution_failed` with a bounded,
redacted exchange code/message and their active backoff also lives in
`runtime/<mode>/state.json`.

Demo and live have separate state, PID, log, failed-alert and journal files.
Each state is also bound to a one-way hash of the configured OKX API key; a
different key cannot silently inherit positions, cooldowns or performance.
Legacy runtime files are copied only when the journal proves their recorded
mode matches the selected mode. Tests use a temporary runtime and cannot
append synthetic records to either operational journal.

```bash
sqlite3 runtime/demo/journal.db "SELECT datetime(ts,'unixepoch'), symbol, side, action, notional, reason FROM trades ORDER BY ts DESC LIMIT 20;"
sqlite3 runtime/demo/journal.db "SELECT datetime(ts,'unixepoch'), equity FROM equity ORDER BY ts DESC LIMIT 10;"
```

Entries and exits share a durable trade ID and record actual fill price,
quantity, fees, signed implementation shortfall, adverse slippage, planned
risk, funding status and incremental net realized USDT. The report excludes
unmatched/open rows, never chains equity across valuation-basis migrations,
and reports independently by runtime account, strategy/version,
prompt/config/code version, parameter variant, strategy-config version and
setup. `--json` uses the same full-provenance groups rather than pooling them
under a strategy name:

```bash
python3 report.py    # basis-segmented equity; matched net USDT, expectancy,
                     # profit factor, R, costs/drawdown by strategy/setup;
                     # universe exclusions and underlying OKX rejection codes
```

The calibration table is the one to watch: if 0.9-confidence trades don't
outperform 0.7s after a few weeks, the confidence floor is not doing what
you think.

Plain-text logs are in `runtime/demo/agent.log` or `runtime/live/agent.log`.

---

### Event kinds added by the research layer

| Kind | Written | Read by |
| --- | --- | --- |
| `book_state` | Per symbol, per cycle — **including when the book is fine** | H-H. The entry guard already read depth but journalled it only on rejection, so every ordinary observation was discarded — and the ordinary readings are the baseline a cascade's collapse is measured against |
| `snapshot_enrichment` | Per cycle | H-I, H-J, H-L. Realised-vol ratio, UTC hour, BTC reference returns. Deliberately **not** in `llm_input`, so the corpus loader joins it by `cycle_id` |
| `variant_shadow_decision` | Per parameter variant, per symbol | Forward evidence for parameter variants that hold no capital |
| `strategy_shadow_decision` | Per strategy contract, per symbol | Cross-strategy forward signals; exported separately from parameter variants |
| `strategy_shadow_summary` | Per strategy contract, per cycle | The scanned-instrument denominator needed to turn signals into rates |
| `research_gate_result` | After every G2 fidelity check | Durable pass/fail/sample record tied to the exact proposal corpus |
| `maker_attempt` | Per passive entry attempt | H-K(ii), once B7.5 is wired |
| `decision_skipped` | When `decision_interval_seconds` defers a decision | Cost accounting |

**Attribution columns** (`journal_schema_version` 4). `events` and `trades`
gained `variant_id` and `strategy_config_version`. Live trading writes
`variant_id = "live"`; replayed and shadow records carry their own readable
ids, so the two populations can never be pooled by accident.

`strategy_config_version` fingerprints only the blocks that can change a
decision — `strategy`, `risk`, `execution`, `trading_costs` and
`cycle.timeframes`. The older whole-config `config_version` is still written
beside it, but it changes when an alert timeout changes, which forks the
attribution bucket and splits an already-small sample for no reason.

Rows written before schema 4 keep `NULL` rather than being back-filled with
`"live"` — back-filling would assert an attribution nobody recorded.

## Historical backtest

`research/phase1_v2_backtest.py` performs an offline, look-ahead-safe replay
of the deterministic phase1-v2 setup contracts and `RiskEngine`. It enters at
the next completed-bar open, handles ambiguous same-bar SL/TP touches
conservatively, includes fees/funding/slippage scenarios, and never calls the
LLM or exchange order endpoints.

The committed January 2025–June 2026 six-instrument result found no reliable
deterministic edge: the combined fixed-RR event study was approximately flat
before costs and negative after ordinary costs. See
`research/results/phase1-v2-backtest-2025-2026/REPORT.md` and
`research/README.md` for methodology, limitations and reproduction.

This result does not measure the LLM selector's incremental contribution.
That requires a frozen forward demo test; replaying a current model over old
history can leak knowledge from model training.

---

## Running multiple agents

One process with specialised modules beats several chatty agents fighting
over the same margin. If you genuinely want multiple independent agents (for
example a conservative one and an aggressive one), use OKX sub-accounts: one
copy of this folder per sub-account, each with its own API keys, config and
runtime directory. Capital is isolated, so one agent's drawdown can never
touch another's.

---

## Troubleshooting

- `check` fails with authentication errors: demo keys with `mode: live` (or
  the reverse) is the most common cause; keys and mode must match.
- "Insufficient balance" on entries: margin is tied up; lower
  `max_gross_exposure_pct` or add USDT to the Trading account.
- A symbol is missing from the universe: run `python main.py check`; it prints
  the first account/category/history exclusion reasons. The complete audit is
  in the latest `universe_selection` journal event.
- `entry_execution_failed`: the order was not opened. The symbol is in a
  persisted execution backoff; inspect its recorded OKX code/message instead
  of repeatedly retrying it.
- Model output parse failures: the agent simply holds for that cycle and
  logs the event; persistent failures usually mean the chosen model ignores
  JSON instructions, so switch models.
- Nothing is trading: check `status` (state must be RUNNING), then
  `runtime/<mode>/agent.log` for rejection reasons; a quiet, choppy market plus a
  0.65 confidence floor legitimately produces long flat stretches.
