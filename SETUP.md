# Setup Guide — from zero to a running agent

This is the complete, assume-nothing walkthrough. It covers **every** piece
involved, not just the agent: the OKX exchange account, the AI provider
account, the computer or server it runs on, installing the code, filling in
your keys, tuning the config, the first run, and keeping it alive 24/7.

It is tool-agnostic. Any always-on computer works (a spare laptop, a mini PC,
a Raspberry Pi, or a rented cloud server from any provider), and either AI
provider works (Anthropic or OpenAI).

> **The two rules that matter more than everything else below:**
> 1. Start in **demo mode** (fake money, real market prices). Run it for at
>    least two weeks before you even think about live mode.
> 2. When you eventually create *live* OKX keys: **Read + Trade permissions
>    only. Never enable Withdraw.** That way even a stolen key cannot move
>    your money off the exchange.

---

## Table of contents

- [How long this takes](#how-long-this-takes)
- [Words you'll see](#words-youll-see)
- [Step 1 — OKX account and demo keys](#step-1--okx-account-and-demo-keys)
- [Step 2 — An AI provider key](#step-2--an-ai-provider-key)
- [Step 3 — Choose the machine it runs on](#step-3--choose-the-machine-it-runs-on)
- [Step 4 — Install the agent](#step-4--install-the-agent)
- [Step 5 — Fill in your secrets (.env)](#step-5--fill-in-your-secrets-env)
- [Step 6 — Configure the agent (config.yaml)](#step-6--configure-the-agent-configyaml)
- [Step 7 — First run](#step-7--first-run)
- [Step 8 — Keep it running 24/7](#step-8--keep-it-running-247)
- [Step 9 — Monitor it](#step-9--monitor-it)
- [Going live (much later)](#going-live-much-later)
- [What it costs](#what-it-costs)
- [Troubleshooting](#troubleshooting)

---

## How long this takes

- OKX demo account + keys: ~15 minutes
- AI provider account + key + a few dollars of credit: ~10 minutes
- Installing and first run: ~10 minutes on a machine you already own; ~30
  minutes if you're also renting and setting up a fresh VPS

You do **not** need to deposit real money anywhere to run the demo.

## Words you'll see

- **Terminal**: the app where you type commands. Mac: "Terminal" (in
  Applications → Utilities). Windows: install "Ubuntu" from the Microsoft
  Store (that's WSL) and use that window. Linux: you already know.
- **API key**: a long secret string that lets a program act on your behalf.
  Treat every key like cash. Anyone who has it can act as you.
- **Demo mode / Demo Trading**: OKX gives you a pretend account with pretend
  balance that trades against *real* live prices. Perfect and free for
  testing.
- **Perpetual swap ("perp")**: a crypto futures contract with no expiry. This
  agent trades USDT-margined perps (e.g. `BTC/USDT:USDT`).
- **Leverage**: borrowing to control a bigger position than your cash.
  Amplifies gains **and** losses. This agent caps it (default 3x).
- **VPS**: a small computer you rent in a data center that never sleeps.

---

## Step 1 — OKX account and demo keys

You'll do everything in OKX's **Demo Trading** environment. Demo keys are
completely separate from live keys and only work in demo mode.

### 1.1 Create an OKX account

1. Go to okx.com and sign up (email or phone). Complete whatever
   verification OKX requires in your region. For demo trading you generally
   do **not** need to deposit or fully KYC, but requirements vary by country.

### 1.2 Enable derivatives/futures trading

The agent trades perpetual swaps, which live in the derivatives section.

1. In the OKX web app, open **Trade → Demo Trading** (top navigation), or
   your profile menu → **Demo Trading**. The interface switches to a
   simulated account with a pretend balance.
2. If prompted to enable Futures/Perpetual trading, accept. In demo this is
   just a toggle; no risk.

### 1.3 Set the account mode

1. Go to **Settings → Account mode** (the account-mode selector is usually
   top-right in the trading view).
2. Choose **Single-currency margin** or **Multi-currency margin**. Either
   works. (Do not use "Spot mode" — the agent needs derivatives.)
3. Keep your (pretend) balance as **USDT in the Trading account**. The agent
   reads equity from the Trading account only; the Funding account is
   invisible to it.

### 1.4 Create DEMO API keys

**You must be inside Demo Trading when you create these**, or you'll get live
keys by mistake.

1. While in Demo Trading, open the API management page: profile menu →
   **API** (or **Demo Trading → API**).
2. Click **Create API Key** (sometimes "Create Demo API Key").
3. Set permissions to **Read** and **Trade**. **Do not** enable Withdraw.
4. Set (and remember) a **passphrase** — you choose this string yourself.
5. OKX shows you three values. Copy all three now; the secret is shown only
   once:
   - **API Key**
   - **Secret Key**
   - **Passphrase** (the one you just chose)

Keep these somewhere safe for Step 5. If you lose the secret, just delete the
key and make a new one.

**Two things OKX does that will bite you later:**

- **Keys without an IP binding expire after 14 days of inactivity** if they
  have Trade permission. A running agent counts as activity, so this only
  matters if you set up keys and then leave them unused for a fortnight.
  If the agent suddenly can't authenticate after a long pause, this is why —
  make a new key.
- **Pick a passphrase without `#`, quotes, or trailing spaces.** The `.env`
  file treats `#` as the start of a comment, so a passphrase like `my#pass`
  silently arrives at OKX as `my` and you get a confusing "invalid
  passphrase" error. Letters, digits, `-` and `_` are the safe set. (If you
  already have one with symbols, wrap the whole value in single quotes in
  `.env`: `OKX_API_PASSPHRASE='my#pass'`.)

> Live keys (for real money, much later) are created the same way but
> **outside** Demo Trading, and you should additionally bind them to your
> server's IP address and, again, never grant Withdraw. Note that an IP
> binding breaks the moment your server's IP changes — if you move hosts or
> your VPS provider reassigns you, update the binding in OKX first.

---

## Step 2 — An AI provider key

The agent calls a large language model once per cycle to make decisions. Pick
**one** provider. Anthropic is the default in `config.yaml`.

This costs real money **even in demo mode** — the AI calls are real. See
[What it costs](#what-it-costs). Budget roughly $50–95/month at the default
5-minute cycle, or far less if you slow the cycle down.

### 2.1 Option A — Anthropic (default)

1. Go to console.anthropic.com and sign up.
2. Add a payment method and a small amount of credit (**Billing** → add
   credit; $10–20 is plenty to start).
3. Go to **API Keys → Create Key**. Copy the key (starts with `sk-ant-`).
   You'll paste it in Step 5 as `ANTHROPIC_API_KEY`.
4. Leave `config.yaml` as-is (`provider: anthropic`,
   `model: claude-sonnet-4-6`).

### 2.2 Option B — OpenAI

1. Go to platform.openai.com and sign up.
2. Add a payment method and credit under **Billing**.
3. Go to **API Keys → Create new secret key**. Copy it (starts with `sk-`).
   You'll paste it in Step 5 as `OPENAI_API_KEY`.
4. In `config.yaml`, set `llm.provider: openai` and `llm.model` to a model
   you have access to (e.g. `gpt-4.1`).

---

## Step 3 — Choose the machine it runs on

The agent is only as always-on as the computer it runs on. If that computer
sleeps or loses power, the agent stops making new decisions. (Open positions
stay protected either way — their stop-losses live on OKX's servers, not on
your machine.) Pick one:

### 3.1 Option A — a computer you already own

Any spare laptop, desktop, mini PC, or Raspberry Pi that can stay powered on.
Turn off sleep/hibernate in the OS power settings. Good for testing and demo.
Downsides: home power cuts and Wi-Fi drops pause the agent, and you probably
don't want a 24/7 trading bot on your daily-driver machine long-term.

Then continue to Step 4 on that machine.

### 3.2 Option B — a rented cloud server (VPS) — recommended for real 24/7

Any provider works — Hetzner, DigitalOcean, Vultr, Linode, AWS Lightsail,
Oracle Cloud free tier, etc. The agent is tiny; the smallest plan is plenty
(**1 CPU, 1 GB RAM, ~$5–12/month**, or free on some tiers).

1. Create the smallest **Ubuntu** (22.04 or 24.04) server the provider offers.
2. The provider gives you an IP address and a way to connect. From your own
   computer's terminal:

   ```bash
   ssh root@YOUR_SERVER_IP
   ```

   (Some providers give you a different username or a key file. Follow their
   "how to connect" page.)

3. Once connected, install the basics:

   ```bash
   sudo apt update && sudo apt install -y python3 python3-pip git tmux sqlite3
   ```

4. Now do Steps 4–8 **on the server** (in that SSH session).
5. Bonus for live mode later: bind your OKX live API keys to this server's IP
   address (an option when creating the key) so they're useless anywhere else.

---

## Step 4 — Install the agent

On whichever machine you chose, open a terminal and run these one at a time.

1. Check Python is 3.11 or newer:

   ```bash
   python3 --version
   ```

   If it's missing: Mac → install from python.org or `xcode-select --install`;
   Ubuntu/VPS → `sudo apt install -y python3 python3-pip`.

2. Download the code:

   ```bash
   git clone https://github.com/khanalyst/okx-agent-crypto.git
   cd okx-agent-crypto
   ```

   (No git? Download the ZIP from the GitHub page and unzip it, then `cd`
   into the folder.)

3. Install the Python libraries it needs:

   ```bash
   pip3 install -r requirements.lock.txt
   ```

4. Create your secrets file from the template:

   ```bash
   cp .env.example .env
   ```

---

## Step 5 — Fill in your secrets (.env)

The `.env` file holds your keys. It stays on this machine and is never
uploaded (it's listed in `.gitignore`).

Open it in a text editor (`nano .env` in a terminal, save with Ctrl+O then
Enter, exit with Ctrl+X) and fill in:

```
# From Step 1 (OKX Demo Trading)
OKX_API_KEY=your-demo-api-key
OKX_API_SECRET=your-demo-secret
OKX_API_PASSPHRASE=your-demo-passphrase

# From Step 2 — fill in ONLY the provider you chose
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...

# Optional in demo; mandatory before mode: live
# ALERT_WEBHOOK_URL=https://hooks.example/...
```

You only need the AI key for the provider set in `config.yaml`. Leave the
other one blank.

**`.env` is the only place the agent reads credentials from.** Nothing is
hardcoded, and no key is read from anywhere else. If the same variable is
also exported in your shell, `.env` wins — but `python main.py check` will
warn you, because having two copies is how people end up pointing a live run
at the wrong account. Lock the file down so other users on the machine can't
read it:

```bash
chmod 600 .env
```

### Your computer's clock matters

OKX signs every request with a timestamp and **rejects anything more than 30
seconds off its own clock**. If your machine's clock drifts, every single
request fails. Make sure automatic time sync is on:

- **macOS**: System Settings → General → Date & Time → *Set time and date
  automatically*
- **Linux**: `sudo timedatectl set-ntp true`

The agent checks this for you at startup, refuses to start if the clock is
more than 15 seconds out, and re-checks every 15 minutes while running.
`python main.py check` prints your current drift.

---

## Step 6 — Configure the agent (config.yaml)

Open `config.yaml`. For a first demo run you can leave everything at its
defaults. Here's what each block controls, so you know where to change what:

- **`mode`** — keep `demo` for now. `live` uses real money.
- **`llm`** — which AI provider and model, and how creative it is
  (`temperature`). Default is Anthropic Sonnet 4.6, which is a good balance
  of cost and quality.
- **`universe`** — which coins it's allowed to trade: the top `top_n` by
  volume above a dollar floor. Add symbols to `denylist` to ban them.
- **`cycle`** — how often it thinks (`interval_seconds`, default 300 = every
  5 minutes) and which candle timeframes it looks at. **Raising
  `interval_seconds` is the biggest cost saver** (600 = every 10 min = half
  the AI bill).
- **`risk`** — how aggressive it is (leverage, risk per trade, how many
  positions, exposure caps) and its safety brakes (daily loss limit, max
  drawdown, margin guard). See the "Configuration reference" and "How the
  agent thinks" sections in the [README](README.md) for exactly what each
  parameter does and how they interact.
- **`execution`** — stale-price, spread and order-book-depth caps plus the
  timeout used to verify actual and partial IOC fills. The IOC slippage cap is
  reserved in deterministic risk sizing.
- **`trading_costs`** — expected taker fees, stop slippage and funding holding
  time used by both the LLM and deterministic all-in risk sizing.
- **`alerts`** — generic, Slack or Discord webhooks. They are optional in demo
  and mandatory in live; put the URL in the named `.env` variable.

After any edit here, restart the agent for it to take effect.

---

## Step 7 — First run

1. Validate everything is wired up correctly:

   ```bash
   python3 main.py check
   ```

   You should see your mode (DEMO), your equity, and a short list of coins.
   If a key is missing or wrong, it tells you exactly which one to fix.

2. Start the agent in the foreground (a log line prints every cycle):

   ```bash
   python3 main.py run
   ```

   Leave it running. Press **Ctrl+C** to stop — that counts as a pause, and
   any open positions keep their stop-losses on OKX's servers. (A Ctrl+C
   pause does not stick across restarts; an explicit `pause` command does.)

---

## Step 8 — Keep it running 24/7

Running in the foreground stops the moment you close the terminal. Two ways
to keep it alive:

### 8.1 tmux (simple, works everywhere)

tmux is like leaving a program running in another room — you can walk away
and reconnect later.

```bash
tmux new -s trader        # opens the "room"
python3 main.py run       # start the agent inside it
# leave the room: press Ctrl+B, release, then press D
# come back later:
tmux attach -t trader
```

If the machine reboots, you have to start it again by hand. For that, use
systemd instead.

### 8.2 systemd (auto-start at boot, auto-restart on crash — Linux/VPS)

Do not run a live trading process as root. Put the checkout under a dedicated
service account (the example uses `/opt/okx-agent`), make `.env` mode 600 and
ensure that account owns only this directory. Then create
`/etc/systemd/system/okx-trader.service`:

```ini
[Unit]
Description=OKX AI Trading Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=okx-agent
Group=okx-agent
WorkingDirectory=/opt/okx-agent
ExecStart=/opt/okx-agent/.venv/bin/python /opt/okx-agent/main.py run
Restart=on-failure
RestartSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=/opt/okx-agent/runtime

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now okx-trader
journalctl -u okx-trader -f     # watch the logs live (Ctrl+C to stop watching)
```

Two behaviors worth knowing:

- An explicit `python3 main.py pause` **survives** crashes and reboots (the
  agent comes back paused until you `resume`). Run control commands as the
  same service user; from `/opt/okx-agent`, use
  `sudo -u okx-agent .venv/bin/python main.py pause`.
- A drawdown self-kill is **not** auto-restarted into trading: the service
  will keep trying and failing to start until a human runs
  `python3 main.py run --acknowledge-kill`. That's deliberate — a human
  should look at a blow-up before more money is risked.

---

## Step 9 — Monitor it

There's no dashboard or GUI — monitoring is the terminal, a `status` command,
and a SQLite journal. All commands run from inside the repo folder.

- One-line health check:

  ```bash
  python3 main.py status
  ```

  Shows state, equity, day PnL, drawdown, and open positions.

- The full diary (every decision, rejection, trade, and the token/cache usage
  of each AI call):

  ```bash
  tail -f runtime/agent.log
  ```

- The last trades from the journal:

  ```bash
  sqlite3 runtime/journal.db "SELECT datetime(ts,'unixepoch'), symbol, side, action, reason FROM trades ORDER BY ts DESC LIMIT 10;"
  ```

- The performance report (after it has traded for a while):

  ```bash
  python3 report.py
  ```

  Shows transfer-adjusted equity, trade-ID-matched net USDT, notional- and
  risk-weighted results, recorded costs, and confidence calibration.

For built-in alerts, set `alerts.enabled: true`, choose `generic`, `slack`, or
`discord`, and set `ALERT_WEBHOOK_URL` in `.env`. Alerts cover circuit breakers,
unprotected/reconciled positions, incomplete flattening, journal failures and
cycle failures. Failed deliveries are retried and saved to
`runtime/failed_alerts.jsonl`. Keep external uptime monitoring too: a dead
machine cannot send its webhook.

Confirm prompt caching is working (keeps your AI bill down): after a couple of
cycles, `grep cache_read runtime/agent.log` — from the second call onward it
should show a few thousand tokens. If it stays 0, see the note in
`config.yaml` about per-model cache minimums.

---

## Going live (much later)

Only after a demo run of at least two weeks that you're happy with, and with
money you can afford to lose entirely:

Before changing modes, run the full local suite and the opt-in read-only OKX
demo integration preflight:

```bash
python3 -m unittest discover -v
OKX_RUN_DEMO_INTEGRATION=1 python3 -m unittest \
  tests.test_okx_demo_integration -v
```

1. In OKX (not in Demo Trading this time), create **live** API keys with
   **Read + Trade only, never Withdraw**, and **bind them to your server's
   IP address**.
2. Enable derivatives, select **one-way / net position mode** while flat, and
   move real USDT into the **Trading** account. The agent checks this setting
   but never changes it.
3. Put the live keys in `.env` (they replace the demo keys).
4. Enable alerts, configure `ALERT_WEBHOOK_URL`, and verify external host
   monitoring. Live startup is blocked if the webhook preflight fails.
5. Ensure `.env` is mode 600, then set `mode: live` in `config.yaml`.
6. Run `python3 main.py check` — it should say LIVE, confirm net mode, Read +
   Trade only, IP binding, and successful alert delivery. This preflight is
   read-only and does not alter leverage or position mode.
7. Start small. Consider lowering `risk_per_trade_pct` and
   `max_gross_exposure_pct` below the demo defaults for your first live days.

Demo fills are idealized; live trading has real slippage, fees, funding
payments, and partial fills. Expect live results to differ from demo.

---

## What it costs

Three separate bills. **Demo mode does not remove the AI bill** — the brain
makes real API calls even when the money is pretend.

### 1. The AI brain (the main cost)

One call per cycle. At the default 5-minute cycle that's 288 calls/day. The
static part of the prompt is cached (10% price on repeats), so each call is
cheap, but they add up:

| Model (`config.yaml`) | Per call | Per day | Per month |
| --- | --- | --- | --- |
| `claude-sonnet-4-6` (default) | ~$0.010 | ~$2.90 | **~$85–95** |
| `claude-haiku-4-5` (cheaper, less capable) | ~$0.006 | ~$1.65 | ~$50 |
| `claude-opus-4-8` (stronger, pricier) | ~$0.027 | ~$7.80 | ~$230 |

**The biggest cost lever is the cycle time.** `cycle.interval_seconds: 600`
(10 min) halves the bill; 900 (15 min) cuts it to a third. Slower cycles also
mean slower reactions — a trade-off you choose. Watch your actual spend on the
provider's usage dashboard for the first few days rather than trusting
estimates.

### 2. The computer

- A machine you already own / Raspberry Pi: ≈ free (a Pi draws a few watts).
- VPS: **$5–12/month** on any provider's smallest plan (some free tiers work).

### 3. The exchange (live mode only)

- Demo mode: free.
- Live mode: OKX charges a fee per trade (taker fees ~0.05% per side for
  regular users — check OKX's current fee page), plus funding payments that
  perpetual positions pay or receive periodically. These come out of the
  Trading account automatically; the agent's reported PnL already reflects
  them.

**Total for a demo trial: roughly $50–100/month of AI usage and nothing
else**, if you run it on a computer you already own.

---

## Troubleshooting

- **`check` fails with an authentication error.** The most common cause is a
  mode/key mismatch: demo keys with `mode: live`, or live keys with
  `mode: demo`. They must match. Double-check you created the keys *inside*
  Demo Trading for demo mode. After that, in order of likelihood: a
  passphrase containing `#` or trailing spaces (see Step 1.4), an IP binding
  that no longer matches this machine, or a key that expired after 14 days
  unused.
- **`TRADE PERMISSION FAILED` in `check`.** The key can read your account but
  not place orders — OKX defaults new keys to Read-only. Edit the key in OKX
  API management and tick **Trade**. Never tick Withdraw. This check exists
  so you find out now instead of when the agent tries its first real entry.
- **"timestamp expired" / error 50102, or `check` reports a large clock
  drift.** Your machine's clock is more than 30 seconds off OKX's. Turn on
  automatic time sync (see Step 5) and restart. Laptops that sleep and
  cheap VPS instances are the usual offenders.
- **"Agent STOPPED: OKX credentials rejected".** After 3 consecutive cycles
  of auth failures the agent pauses itself rather than pretending to trade.
  Any open positions are still protected by their stop-loss/take-profit
  orders on OKX, but nothing is managing them — so fix this promptly. Repair
  the credentials, run `check`, then `python3 main.py resume`. If you run
  under systemd, note that `Restart=always` will keep restarting into the
  same failure; fix the key rather than watching it loop.
- **"Insufficient balance" when opening.** Margin is tied up, or the (demo)
  Trading account has too little USDT. Lower `max_gross_exposure_pct`, or add
  USDT to the Trading account. Confirm your balance is in the **Trading**
  account, not Funding.
- **`ModuleNotFoundError` on start.** Dependencies aren't installed in the
  Python you're running. Re-run `pip3 install -r requirements.lock.txt` from
  inside the repo folder.
- **Nothing is trading.** Run `status` (state must be RUNNING), then read
  `runtime/agent.log` for rejection reasons. A quiet, choppy market plus the
  0.65 confidence floor legitimately produces long flat stretches — that's
  the agent being disciplined, not broken.
- **Model output parse failures in the log.** The agent just holds for that
  cycle. Persistent failures usually mean the chosen model ignores the JSON
  instruction — switch to a more capable model.
- **`cache_read` stays 0 in the log.** Prompt caching isn't engaging. On Opus
  and Haiku the system prompt is below their cache minimum (expected — it
  bills normally). On Sonnet it should cache; if not, you may have edited the
  system prompt below the minimum length.
- **It self-killed and won't restart.** That's the drawdown circuit breaker.
  Review `runtime/agent.log` and the journal, then restart deliberately with
  `python3 main.py run --acknowledge-kill`.
