# OKX AI Trading Agent

A 24/7 autonomous trading agent for OKX USDT-margined perpetual swaps. An LLM
of your choice (Anthropic or OpenAI) acts as the analyst brain; a
deterministic risk engine owns sizing, leverage and circuit breakers; an
execution layer places orders with exchange-side stop-losses so positions
stay protected even if the process dies.

Everything is measured and sized in percentages of live equity, so the agent
compounds automatically as the account grows and scales down automatically if
it shrinks.

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
| `agent/state.py` | State machine, PID file, SQLite journal |

---

## Setup

Requirements: Python 3.10+ and an always-on machine (VPS recommended for
true 24/7 operation).

```bash
cd okx-agent-crypto
pip install -r requirements.txt
cp .env.example .env      # then fill in your keys
```

### OKX account prerequisites

1. In OKX, make sure derivatives trading is enabled and your account mode is
   Single-currency or Multi-currency margin (Settings -> Account mode).
2. Keep your trading capital as USDT in the Trading account (the agent reads
   equity from there; the Funding account is invisible to it).
3. The agent sets one-way (net) position mode on startup. If you have open
   positions in hedge mode, close them first.

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

- Sizing is recomputed from live equity every cycle, so a deposit compounds
  into larger positions on the very next cycle and a withdrawal shrinks them.
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

1. **Sync equity and money movements.** It reads live Trading-account equity
   and checks the OKX ledger for deposits/withdrawals, rebasing its
   benchmarks so a transfer is never mistaken for profit or a crash.
2. **Check the circuit breakers first.** If the account is down past the
   daily loss limit, no new positions open until the next UTC day. If it's
   down past the max drawdown from its high-water mark, it flattens
   everything and self-kills (see Caveats).
3. **Refresh the tradable universe (hourly).** The top `top_n` USDT
   perpetuals by 24h volume above `min_24h_quote_volume_usd` (default $50m).
   Thin and newly listed coins never qualify; the indicator-history
   requirement filters fresh listings a second time.
4. **Housekeep open positions.** Force-close anything past `max_hold_hours`,
   and if margin usage exceeds `max_margin_usage_pct`, close the largest
   position(s) until it's healthy.
5. **Ask the brain.** It builds a compact snapshot per symbol — price, 24h
   change, volume, funding rate, RSI, ATR%, trend on 15m/1h/4h, 1h momentum,
   position within the 24h range — plus the live portfolio (equity, day PnL,
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
   your stop distance so that being stopped out loses exactly
   `risk_per_trade_pct` of equity** — then caps that by the per-position and
   whole-book exposure limits. Closes execute first, then the surviving opens
   (highest confidence first).
7. **Execute with protection.** Orders go to OKX with **exchange-side
   stop-loss and take-profit attached**, so positions stay protected even if
   this process dies. If a stop-loss can't be placed after an entry fills,
   the agent immediately closes that position rather than run it naked.

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
- **Total size is capped two ways regardless of count:** each position is at
  most `max_position_notional_pct` of equity (default 40%), and the whole
  book is at most `max_gross_exposure_pct` of equity (default 150%).

So the maximum book is 3 positions, each on a different symbol, each ≤40% of
equity, summing to ≤150% of equity in notional.

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
| `risk_per_trade_pct` | 1.5 | % of equity lost if a stop is hit; position size is derived from this |
| `max_position_notional_pct` | 40 | Per-position notional cap, % of equity |
| `max_gross_exposure_pct` | 150 | Whole-book notional cap, % of equity |
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

**Change execution (`execution:` block)** — `slippage_guard_pct` (default 0.5):
abort an entry if price moved more than this between analysis and execution.

> **To turn aggression up**, raise `max_leverage`, `risk_per_trade_pct`, and
> `max_gross_exposure_pct` — but understand how they compound: 3 positions at
> 2% risk each is a 6% day if everything stops out at once. A few limits are
> hard-coded in [agent/risk.py](agent/risk.py) and cannot be raised via
> config: leverage is capped at 10, stop distances must be between 0.2% and
> 15%, and positions below ~$10 notional are skipped.

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
- **No alerting exists.** If the agent self-kills or an error stops it at 3am,
  it goes silent — nothing texts or emails you. Check `status` daily, or wire
  the log into your own notifier.
- **The drawdown self-kill halts the agent until a human intervenes.** After a
  15% drawdown it flattens and refuses to trade again until you run
  `--acknowledge-kill`. This is intentional (a human should review a blow-up),
  but it means the agent can sit dead through a recovery.
- **It needs an always-on machine and network.** If the process dies, no new
  decisions happen. Open positions remain protected by their exchange-side
  stop-losses on OKX, but nothing new is managed until it's back up.
- **Demo results ≠ live results.** Demo fills are idealized. Live trading adds
  real slippage, trading fees, funding payments, and occasional partial
  fills. Expect live to underperform demo.
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
rejection, trade, transfer and an equity curve snapshot per cycle.

```bash
sqlite3 runtime/journal.db "SELECT datetime(ts,'unixepoch'), symbol, side, action, notional, reason FROM trades ORDER BY ts DESC LIMIT 20;"
sqlite3 runtime/journal.db "SELECT datetime(ts,'unixepoch'), equity FROM equity ORDER BY ts DESC LIMIT 10;"
```

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
