# Setup Guide — from zero to a running agent

This guide assumes you have never done any of this before. Every step says
what to type and what you should see. It is tool-agnostic: any computer that
can stay on works (a Mac, a Windows PC, a Linux box, a Raspberry Pi, or a
rented cloud server from any provider), and either AI provider (Anthropic or
OpenAI) works.

> **The two rules that matter more than everything else below:**
> 1. Start in **demo mode** (fake money, real markets). Run it for at least
>    two weeks before you even think about live mode.
> 2. When you eventually create live OKX keys: **Read + Trade permissions
>    only. Never enable Withdraw.** That way even a stolen key cannot move
>    your money off the exchange.

---

## What you need (3 things)

1. **A computer that stays on.** The agent trades in cycles around the clock;
   if the computer sleeps, the agent sleeps. (Open positions stay protected
   either way — their stop-losses live on OKX's servers, not on your machine.)
2. **An OKX account** — okx.com. For demo mode you don't need to deposit
   anything.
3. **An AI key** from Anthropic (console.anthropic.com) or OpenAI
   (platform.openai.com). This is the "brain" the agent calls every cycle.
   It costs real money even in demo mode — see the costs section at the end.

## Words you'll see

- **Terminal**: the app where you type commands. Mac: "Terminal". Windows:
  install "Ubuntu" from the Microsoft Store (WSL) and use that. Linux: you
  already know.
- **API key**: a long secret password that lets a program act on your behalf.
  Treat every key like cash.
- **Demo mode**: OKX gives you a pretend account with pretend money that
  trades against real prices. Perfect for testing.
- **VPS**: a small computer you rent in a data center that never sleeps.

---

## Part A — Install the agent (any computer)

Open a terminal and type these lines one at a time, pressing Enter after each.

1. Check Python is installed (need 3.10 or newer):

   ```bash
   python3 --version
   ```

   If that fails: Mac → `xcode-select --install` or install from python.org;
   Ubuntu/WSL → `sudo apt update && sudo apt install -y python3 python3-pip git`.

2. Get the code onto the machine (pick one):

   ```bash
   git clone https://github.com/khanalyst/okx-agent-crypto.git
   cd okx-agent-crypto
   ```

   (Or copy the folder over any way you like — USB stick works too.)

3. Install the libraries the agent needs:

   ```bash
   pip3 install -r requirements.txt
   ```

4. Create your secrets file:

   ```bash
   cp .env.example .env
   ```

   The `.env` file is where your keys go in Parts B and C. It stays on your
   machine — it is never uploaded anywhere (it's in `.gitignore`).

## Part B — OKX demo keys

1. Log in to okx.com. Find **Demo Trading** (usually under your profile menu
   or the trade menu) and switch to it. You'll see a pretend balance.
2. While still in Demo Trading, go to the API management page and create a
   new API key. Give it **Read** and **Trade** permissions. It will show you
   three things — copy each one:
   - API Key
   - Secret Key (shown only once!)
   - Passphrase (you choose this)
3. Open `.env` in any text editor (`nano .env` works in a terminal) and fill
   in:

   ```
   OKX_API_KEY=paste-the-api-key
   OKX_API_SECRET=paste-the-secret
   OKX_API_PASSPHRASE=paste-the-passphrase
   ```

4. Make sure derivatives/futures trading is enabled on the demo account and
   the account mode is Single-currency or Multi-currency margin
   (OKX Settings → Account mode).

Demo keys only work with `mode: demo` in `config.yaml` (already the default).
Live keys are a completely separate set you'd create later, outside Demo
Trading — and again: Read + Trade only, never Withdraw, and bind them to your
server's IP address.

## Part C — AI key

Pick ONE provider (Anthropic is the default in `config.yaml`):

- **Anthropic**: console.anthropic.com → API Keys → Create Key. Put it in
  `.env` as `ANTHROPIC_API_KEY=...`. You'll need to add a few dollars of
  credit to the account.
- **OpenAI**: platform.openai.com → API Keys → Create. Put it in `.env` as
  `OPENAI_API_KEY=...` and change `config.yaml` → `llm.provider: openai` and
  `llm.model` to e.g. `gpt-4.1`.

## Part D — First run

1. Check everything is wired up:

   ```bash
   python3 main.py check
   ```

   You should see your mode (DEMO), your equity, and a list of coins.
   If something is missing it tells you exactly which key to fix.

2. Start the agent:

   ```bash
   python3 main.py run
   ```

   You'll see a log line every cycle (every 5 minutes by default). Leave it
   running. Press Ctrl+C to stop it — that counts as "pause", and any open
   positions keep their stop-losses on OKX's servers.

3. Useful commands (from the same folder, in a second terminal):

   | Command | What it does |
   | --- | --- |
   | `python3 main.py status` | Balance, day PnL, open positions |
   | `python3 main.py pause` | Stop opening new trades (sticks until you resume) |
   | `python3 main.py resume` | Back to full trading |
   | `python3 main.py flatten` | Close everything, then pause |
   | `python3 main.py kill` | Emergency stop: close everything and halt |

   If the account ever loses 15% from its peak, the agent closes everything
   and **refuses to restart** until you run
   `python3 main.py run --acknowledge-kill`. That's deliberate — it forces a
   human to look at what happened before more money is risked.

---

## Part E — Keeping it on 24/7

The agent is only as always-on as the computer it runs on. Three options,
cheapest-effort first:

### Option 1 — a home computer that never sleeps

Any spare laptop, mini PC, or Raspberry Pi. Turn off sleep/hibernate in the
OS power settings. Then run the agent inside **tmux** so it survives you
closing the terminal window (tmux is like leaving a TV playing in another
room — you can walk away and come back):

```bash
tmux new -s trader        # opens a room
python3 main.py run       # start the agent inside it
# leave the room: press Ctrl+B, release, then press D
# come back later:
tmux attach -t trader
```

Downside: home power cuts and Wi-Fi drops stop the agent (positions stay
protected by their exchange-side stops, but no new decisions happen).

### Option 2 — rent a small cloud server (VPS)

Any provider works — Hetzner, DigitalOcean, Vultr, Lightsail, whatever is
cheap in your region. The agent is tiny: the smallest plan (1 CPU, 1 GB RAM,
~$5–12/month) is plenty.

1. Create the smallest Ubuntu server the provider offers.
2. Connect to it: the provider gives you a command like `ssh root@1.2.3.4`.
3. Do Part A–D on the server exactly as above.
4. Bonus: bind your OKX API keys to the server's IP address (an option when
   creating the key) so they're useless anywhere else.

### Option 3 — make it restart itself (systemd, Linux/VPS)

tmux keeps it running, but if the machine reboots you'd have to start it by
hand. systemd starts it at boot and restarts it if it crashes. Create
`/etc/systemd/system/okx-trader.service`:

```ini
[Unit]
Description=OKX AI Trading Agent
After=network-online.target

[Service]
WorkingDirectory=/root/okx-agent-crypto
ExecStart=/usr/bin/python3 /root/okx-agent-crypto/main.py run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now okx-trader
journalctl -u okx-trader -f     # watch the logs live
```

Two behaviors worth knowing: an explicit `python3 main.py pause` **survives**
crashes and reboots (the agent comes back paused until you `resume`), and a
drawdown self-kill is **not** restarted by systemd — it stays down until a
human runs `run --acknowledge-kill`. Both are on purpose.

### Checking on it (any option)

- `python3 main.py status` — the one-line health check.
- `tail -f runtime/agent.log` — the full diary, including why trades were
  rejected and the token/cache usage of every AI call.
- `sqlite3 runtime/journal.db "SELECT datetime(ts,'unixepoch'), symbol, side, action, reason FROM trades ORDER BY ts DESC LIMIT 10;"`
  — the last 10 trades.

The agent has no alerting built in: if it self-kills at 3am it stays down
silently. Check `status` daily, or wire `runtime/agent.log` into whatever
notification tool you already use.

---

## Part F — What it costs (indicative, mid-2026 prices)

Three separate bills. **Demo mode does not remove the AI bill** — the brain
makes real API calls even when the money is pretend.

### 1. The AI brain (the main cost)

One call per 5-minute cycle = 288 calls/day. The static part of the prompt is
cached on Anthropic's servers (and automatically on OpenAI's), so each call
bills roughly: ~2.6k cached tokens at ~10% price + ~1.8k fresh tokens + ~250
output tokens.

| Model (config.yaml) | Per call | Per day | Per month |
| --- | --- | --- | --- |
| `claude-sonnet-4-6` (default) | ~$0.010 | ~$2.90 | **~$85–95** |
| `claude-haiku-4-5` (cheaper, less capable) | ~$0.006 | ~$1.65 | ~$50 |
| `claude-opus-4-8` (stronger, pricier; prompt below its cache minimum) | ~$0.027 | ~$7.80 | ~$230 |

**The biggest cost lever is the cycle time.** `cycle.interval_seconds: 600`
(10 minutes) halves the bill; 15 minutes cuts it to a third. Slower cycles
also mean slower reactions — a trade-off you choose. The agent also skips
calls it can't act on (e.g. after the daily loss stop with nothing held).

Watch your actual spend on the provider's usage dashboard for the first few
days rather than trusting estimates.

### 2. The computer

- Home machine / Raspberry Pi: ≈ free (a Pi uses ~2–4 W, pennies per month).
- VPS: **$5–12/month** on any provider's smallest plan.

### 3. The exchange (live mode only)

- Demo mode: free.
- Live mode: OKX charges a fee per trade (taker fees around 0.05% per side
  for regular users — check OKX's current fee page), plus funding payments
  that perpetual positions pay or receive every few hours. These come out of
  the trading account automatically; the agent's PnL already reflects them.

**Total for a demo trial: roughly $50–100/month of AI usage and nothing
else** (if you use a computer you already own).
