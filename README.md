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

## Strategy and the risk engine

The model is prompted as an aggressive, disciplined momentum day trader:
multi-timeframe trend alignment (15m impulse with the 1h/4h trend), long and
short, funding-rate aware, ATR-aware stops, at least 2R take-profits, no
averaging down, and "flat is a position". It sees a compact snapshot per
symbol (price, 24h change, volume, funding, RSI, ATR%, trend per timeframe,
momentum, range position) plus the live portfolio in percentage terms.

The universe is rebuilt hourly: top `top_n` USDT perpetuals by 24h quote
volume above `min_24h_quote_volume_usd` (default $50m). Thin and newly listed
coins never qualify; the indicator history requirement filters fresh listings
a second time.

The risk engine then clamps every proposal. All values live in `config.yaml`:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `max_leverage` | 3 | Per position. Hard ceiling of 10 in code regardless of config. |
| `risk_per_trade_pct` | 1.5 | % of equity lost if a stop is hit; position size is derived from this and the stop distance |
| `max_position_notional_pct` | 40 | Per-position notional cap, % of equity |
| `max_gross_exposure_pct` | 150 | Whole-book notional cap, % of equity |
| `max_concurrent_positions` | 3 | |
| `min_confidence` | 0.65 | Model decisions below this are discarded |
| `max_hold_hours` | 24 | Stale positions are force-closed |
| `daily_loss_limit_pct` | 5 | Daily circuit breaker |
| `max_drawdown_pct` | 15 | Account circuit breaker: flatten and self-kill |
| `max_margin_usage_pct` | 60 | Margin guard threshold |
| `cooldown_minutes_after_loss` | 45 | Per-symbol timeout after a losing close |

Turning aggression up means raising `max_leverage`, `risk_per_trade_pct` and
`max_gross_exposure_pct`. Understand the compounding of those three before
touching them: 3 positions at 2% risk each is a 6% day if everything stops
out at once.

If a stop-loss order cannot be placed right after an entry fills, the agent
closes that position immediately rather than let it run unprotected.

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
