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
 │                              proposes open/close as JSON        │
 │                                                │                │
 │                                                ▼                │
 │                     deterministic RISK ENGINE (the boss)        │
 │       sizing from risk-per-trade, leverage clamp, exposure      │
 │       caps, confidence floor, cooldowns, circuit breakers       │
 │                                                │                │
 │                                                ▼                │
 │        executor ──► OKX orders with attached SL/TP (server-side)│
 └─────────────────────────────────────────────────────────────────┘
        control: runtime/state.json + CLI      journal: SQLite
```

| File | Role |
| --- | --- |
| `main.py` | CLI: run, pause, resume, kill, flatten, status, check |
| `agent/engine.py` | The loop: circuit breakers, transfer detection, execution |
| `agent/brain.py` | LLM providers, trader persona prompt, JSON decision parsing |
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
(explicitly on Anthropic with a 1-hour TTL, automatically on OpenAI), the
per-cycle market payload is serialized compactly, and cycles where the model
cannot act (daily loss stop with no open positions) skip the call entirely.
Every call logs `tokens: in=... out=... cache_write=... cache_read=...` to
`runtime/agent.log` — after the first call of a session, `cache_read` should
be a few thousand tokens; if it stays 0, caching isn't engaging (see the
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

## Controls

| Command | What it does |
| --- | --- |
| `python main.py status` | State, equity, day PnL, drawdown, open positions |
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
3. **Refresh the tradable universe (hourly).** The top `top_n` USDT
   perpetuals by 24h volume above `min_24h_quote_volume_usd` (default $50m).
   Thin and newly listed coins never qualify; the indicator-history
   requirement filters fresh listings a second time.
4. **Reconcile and housekeep open positions.** The exchange is authoritative:
   each cycle (including the first after a restart) matches live positions to
   durable trade IDs, verifies stop-loss/take-profit coverage, restores known
   protection where possible, and closes any position that cannot be verified
   to have a stop. It then force-closes anything past `max_hold_hours`, and if
   margin usage exceeds `max_margin_usage_pct`, closes the largest position(s)
   until it's healthy.
5. **Ask the brain.** It builds a compact snapshot per symbol — price, spread,
   correctly contract-normalized 24h quote volume, completed-hour relative
   volume, funding rate/interval/next settlement, RSI, current
   ATR%, ATR versus its recent history, trend on 15m/1h/4h, 1h momentum,
   30-hour BTC correlation, an explicit regime classification, position within
   the 24h range, distance to the recent swing high/low and to the 1h EMA20
   (structure anchors for stop placement) — plus a BTC-wide regime context and the live
   portfolio (equity, day PnL,
   drawdown, each open position's unrealised PnL and age). The LLM is
   prompted as an **aggressive but disciplined momentum day trader**:
   multi-timeframe trend alignment (a 15m impulse in the direction of the
   1h/4h trend is the A+ setup), long and short with equal comfort,
   funding-rate aware, ATR-sized stops, take-profits of at least 2x the stop
   distance, never averaging down, and "flat is a position" — returning zero
   trades is normal and often correct. It replies as strict JSON.
6. **The risk engine disposes.** The model only *proposes*. A deterministic
   engine ([agent/risk.py](agent/risk.py)) then vets every proposal:
   discards anything below the confidence floor, rejects symbols already held
   or in post-loss cooldown, clamps leverage, and **sizes each position from
   stop distance plus expected round-trip fees, live spread, adverse funding
   and stop slippage so the all-in expected stop loss stays within
   `risk_per_trade_pct` of equity** — then caps that by the per-position,
   whole-book, and net-direction exposure limits (the last one stops several
   same-direction positions in correlated coins from acting as one oversized
   macro bet). Closes execute first, then the surviving opens (highest
   confidence first).
7. **Execute with protection.** Orders use client IDs and are never blindly
   retried after an ambiguous network response. Actual terminal order status,
   filled quantity, average fill, fees, and partial fills are verified. Orders
   go to OKX with **exchange-side stop-loss and take-profit attached**, so
   positions stay protected even if this process dies. If a stop-loss can't
   be verified after an entry fills, the agent immediately closes that
   position rather than run it naked. Entries are marketable IOC limits:
   current spread and order-book depth must pass configured caps, and the
   exchange-side limit prevents a sudden move from producing unlimited entry
   slippage.

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
- **Total size is capped three ways regardless of count:** each position is
  at most `max_position_notional_pct` of equity (default 40%), the whole book
  is at most `max_gross_exposure_pct` of equity (default 150%), and the *net
  direction* (long notional minus short notional) is at most
  `max_net_direction_pct` of equity (default 100%) — because three correlated
  longs are really one big long.

So the maximum book is 3 positions, each on a different symbol, each ≤40% of
equity, ≤150% total notional, and ≤100% net in one direction.

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
| `denylist` | `[]` | Symbols to ban, e.g. `["DOGE/USDT:USDT"]` |
| `refresh_minutes` | 60 | How often the volume ranking is rebuilt |

**Change how often it thinks (`cycle:` block)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `interval_seconds` | 300 | Seconds between decisions. **Biggest cost lever** — raising it cuts the AI bill proportionally |
| `timeframes` | `[15m,1h,4h]` | Candle timeframes fed to the model |
| `candles` | 120 | Candles per timeframe; also excludes coins lacking history |

**Change how aggressive it is (`risk:` sizing)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `max_leverage` | 3 | Per-position leverage cap. **Hard ceiling of 10 in code** regardless of config |
| `risk_per_trade_pct` | 1.5 | Maximum expected equity loss at a stop, including configured costs |
| `max_position_notional_pct` | 40 | Per-position notional cap, % of equity |
| `max_gross_exposure_pct` | 150 | Whole-book notional cap, % of equity |
| `max_net_direction_pct` | 100 | Cap on net long-minus-short notional, % of equity (correlation guard) |
| `max_concurrent_positions` | 3 | Max simultaneous open positions |
| `min_confidence` | 0.65 | Proposals below this are discarded |
| `max_hold_hours` | 24 | Stale positions are force-closed |

**Change the safety brakes (`risk:` circuit breakers)**

| Parameter | Default | What it does |
| --- | --- | --- |
| `daily_loss_limit_pct` | 5 | Down this much on the day → no new entries until next UTC day |
| `flatten_on_daily_stop` | false | `true` also closes open positions when the daily stop trips |
| `max_drawdown_pct` | 15 | Down this much from the high-water mark → flatten everything and self-kill |
| `max_margin_usage_pct` | 60 | Above this, close the largest position(s) to reduce margin |
| `cooldown_minutes_after_loss` | 45 | Per-symbol timeout after a losing close |

**Change execution (`execution:` block)** — `slippage_guard_pct` (default 0.5)
rejects stale analyses, `max_spread_pct` (0.15) rejects a wide current market,
and `max_order_book_slippage_pct` (0.35) is the depth-test threshold, hard IOC
entry-price boundary, and a reserved component of all-in sizing.
`max_market_data_age_seconds` (10) rejects stale order books.
`fill_timeout_seconds` (12) bounds fill
verification before the agent cancels any unfilled remainder.

**Cost assumptions (`trading_costs:` block)** — expected taker fee per side,
stop slippage, minimum funding intervals and expected holding hours are sent to
the LLM alongside the live funding schedule. The deterministic risk engine
derives likely settlements from that schedule and includes their adverse cost
in position sizing; they do not replace technical stop placement.

**Human alerts (`alerts:` block)** — generic JSON, Slack or Discord webhooks
are optional in demo but mandatory in live. Live startup sends a preflight
message and refuses to trade unless it is acknowledged. Delivery is retried
three times; exhausted messages are stored in
`runtime/failed_alerts.jsonl`.

> **To turn aggression up**, raise `max_leverage`, `risk_per_trade_pct`, and
> `max_gross_exposure_pct` — but understand how they compound: 3 positions at
> 2% risk each is a 6% day if everything stops out at once. A few limits are
> hard-coded in [agent/risk.py](agent/risk.py) and cannot be raised via
> config: leverage is capped at 10, stop distances must be between 0.2% and
> 15%, and positions below ~$10 notional are skipped.
>
> **Know your *effective* risk per trade.** Sizing takes the *minimum* of
> three caps, so `risk_per_trade_pct` is a ceiling, not a promise: with a
> typical 1.5–2% stop, the 40% per-position notional cap binds first and the
> actual loss on a stop-out is ~0.6–0.8% of equity, not 1.5%. The risk
> target only fully binds for stops wider than ~3.75%
> (= risk 1.5 ÷ cap 0.40). The model can also voluntarily request less via
> `size_pct_equity`; it is prompted to do so only deliberately.

---

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
  a separate layer and inspect `runtime/failed_alerts.jsonl`.
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

Everything is recorded in `runtime/journal.db` (SQLite): every decision,
rejection, trade, transfer and an equity curve snapshot per cycle. The agent
checks that the journal is writable before starting and pauses immediately on
any later journal write failure. Plain-text logs rotate at 10 MB.

Each model cycle also records the exact provider request, every retry attempt,
the provider response ID, raw response text and parsed decisions. This makes
the nondeterministic LLM layer auditable after prompts or settings change;
protect and back up the journal as trading-sensitive data.

```bash
sqlite3 runtime/journal.db "SELECT datetime(ts,'unixepoch'), symbol, side, action, notional, reason FROM trades ORDER BY ts DESC LIMIT 20;"
sqlite3 runtime/journal.db "SELECT datetime(ts,'unixepoch'), equity FROM equity ORDER BY ts DESC LIMIT 10;"
```

Entries and exits share a durable trade ID and record actual fill price,
quantity, fees, slippage, planned risk, funding and net realized USDT. The
report excludes legacy or unmatched rows rather than pairing by symbol or
averaging unrelated percentage returns:

```bash
python3 report.py    # transfer-adjusted equity, net USDT, risk/notional returns,
                     # costs, confidence calibration and rejection reasons
```

The calibration table is the one to watch: if 0.9-confidence trades don't
outperform 0.7s after a few weeks, the confidence floor is not doing what
you think.

Plain-text logs are in `runtime/agent.log`.

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
- Model output parse failures: the agent simply holds for that cycle and
  logs the event; persistent failures usually mean the chosen model ignores
  JSON instructions, so switch models.
- Nothing is trading: check `status` (state must be RUNNING), then
  `runtime/agent.log` for rejection reasons; a quiet, choppy market plus a
  0.65 confidence floor legitimately produces long flat stretches.
