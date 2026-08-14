# Complete setup: Alpaca paper trading on an Ubuntu VM

This is the primary installation and startup guide, written to be followed in
order by someone who has never deployed this before. Every step says what to
run, what you should see, and what to do when you see something else.

---

## Read this first: what you are actually deploying

This is **not** a bot you switch on and leave to trade. It is two cooperating
systems:

1. **A research factory** that invents candidate trading rules, tests them
   against recorded market data, and tries very hard to prove them wrong.
2. **A trader** that is *forbidden from placing any order* until the research
   side has produced a rule that survived every test.

On a brand-new install the trader will start, connect, report healthy — and
place no trades at all, possibly for weeks. **That is the system working
correctly, not a fault.** Most of this guide is about getting the research side
fed and readable so that silence is informative rather than worrying.

### Words you will meet

| Term | What it means here |
| --- | --- |
| **Edge** | A trading rule with evidence that it makes money after costs. |
| **Rule spec** | The edge itself, stored as *data* in a fixed grammar — never code. The LLM can only emit these. |
| **Family** | The kind of signal (opening-range breakout, mean reversion, VWAP reversion, …). There are 11. |
| **Variant** | One family with specific numbers — a 15-minute range instead of 20, a 2.0R target instead of 1.5R. |
| **Slot** | One of 7 logical research slots; each isolated book is processed by one bounded worker. |
| **Generation** | How many times a slot's hypothesis has been replaced after failing. |
| **Backtest lane** | First test, on the corpus split into fit and held-out parts. |
| **Offline shadow lane** | Forward replay on sessions recorded *strictly after* the backtest; it may leave a candidate at `shadow` but never authorizes runtime. |
| **Live-shadow marker** | Research-side proof from `edge ingest-shadow` after complete recorder parity; required for runtime eligibility. |
| **Validated / champion** | An edge with a passing live-shadow proof and marker. Only these may trade. |
| **R** | Profit measured in units of the risk taken. +1R means it made exactly what it risked. |
| **Corpus** | The recorded market data research runs on. |

### Realistic timeline

| When | What happens |
| --- | --- |
| Day 1 | Install, backfill history, start recording. Trader idle. |
| Day 1 + first research cycle | Hypotheses seeded, variants tested, most fail. Trader still idle. |
| Weeks 1–8 | Families are tried, retired, replaced. Reports get interesting. Trader still idle. |
| First live-shadow-validated edge | Trader begins placing paper trades under risk limits. |
| Ongoing | Proved edges are frozen; slots keep searching for new ones forever. |

**Without a historical backfill this takes months** — the recorder only samples
forward in real time. Step 10 fixes that and is the single biggest thing you
can do to shorten the wait.

### What you need before starting

- An Alpaca account (paper access is free).
- An Ubuntu LTS x86-64 VM: 4 vCPUs, 8 GB RAM, 40 GB+ disk.
- Comfort with SSH and a terminal. You do not need to read Python.
- Optionally, an OpenAI or Anthropic API key for LLM-driven discovery. The
  system works without one; discovery falls back to a deterministic search.

Do not start with live credentials. Live mode is an optional, separately
guarded step at the very end.

---

## 1. Prepare the Alpaca paper account

1. Create or sign in to an Alpaca account.
2. Switch to the **paper trading** dashboard. Check the URL and the on-screen
   label. Paper and live are different accounts with different keys, and this
   is the most common place to go wrong.
3. Generate a paper API key and secret.
4. Save both immediately in a password manager. **The secret is shown once.**
5. Note which data feed your account is entitled to. This project defaults to
   `iex` for stocks and `indicative` for options — both available on the free
   tier. Use `sip` or `opra` only if your account actually has them; requesting
   an unentitled feed fails at the first authenticated call.
6. Do not configure crypto. This repository accepts US equity/ETF underlyings
   and listed OCC option contracts only, and rejects everything else.

Official references:

- [Alpaca paper trading](https://docs.alpaca.markets/us/docs/paper-trading)
- [Alpaca authentication and API domains](https://docs.alpaca.markets/us/docs/authentication)
- [Alpaca option-chain feeds](https://docs.alpaca.markets/us/reference/optionchain)

**Checkpoint:** you have a paper key, a paper secret, and you know your feed.

## 2. Create the VM

Use a current Ubuntu LTS x86-64 VM. A practical starting size for all four
services is 4 vCPUs, 8 GB RAM, and at least 40 GB of durable storage. Research
and recorded market data grow over time; a year of 1-minute bars and quotes for
a handful of symbols is comfortably within 40 GB, but option snapshots are much
larger if you raise the sampling limits in step 11.

**On Azure, do the storage and network work first.**
[AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) covers attaching and mounting a
managed data disk, pointing Docker at it, and closing the network security
group — including the one mistake that quietly destroys all accumulated
research, which is putting durable data on Azure's temporary resource disk.
Come back here at step 3 afterwards.

Restrict inbound access to SSH from your own address. Nothing in this stack
should be exposed to the internet — the dashboard binds to localhost and you
will reach it through an SSH tunnel.

## 3. Install Git, Docker Engine, and Compose

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

**Log out and back in** for the group change to apply, then verify:

```bash
docker run --rm hello-world
docker compose version
```

**Expected:** a "Hello from Docker!" message and a Compose version of v2 or
later.

**If `docker` says "permission denied"**, the group change has not applied —
log out fully and back in, or run `newgrp docker`.

## 4. Clone the repository

```bash
sudo install -d -o "$USER" -g "$USER" /opt/alpaca-agent-trading
git clone <repository-url> /opt/alpaca-agent-trading
cd /opt/alpaca-agent-trading
```

The default branch is what you want. Every command from here on assumes you are
in `/opt/alpaca-agent-trading`.

## 5. Create the paper credential file

Credentials live outside the checkout so they can never be committed:

```bash
sudo install -d -m 0750 /etc/alpaca-agent-trading
sudo install -m 0600 /dev/null /etc/alpaca-agent-trading/agent.env
sudoedit /etc/alpaca-agent-trading/agent.env
```

Enter only the paper credentials and selected feeds:

```dotenv
ALPACA_API_KEY=<paper-api-key>
ALPACA_SECRET_KEY=<paper-api-secret>
ALPACA_PAPER=true
ALPACA_DATA_FEED=iex
ALPACA_STOCK_FEED=iex
ALPACA_OPTIONS_FEED=indicative
```

Never put live credentials in this file. Never commit it. In your Alpaca
account, use trading permissions only and keep withdrawals disabled.

## 6. Optionally configure the research LLM

The LLM's job here is narrow and worth understanding before you enable it:

- It **proposes** new rule specs — new hypotheses for free research slots, and
  replacements for families that have failed.
- It **cannot** write code, place an order, or shorten the evidence path. Its
  proposals enter the ledger as `queued` and must pass exactly the same gates a
  deterministic hypothesis faces.
- If you skip this step, discovery falls back to a deterministic search over
  the same grammar. The system works; it just explores in a fixed order rather
  than an informed one.

Create a **separate** provider-only secret — Alpaca credentials must never
appear in it:

```bash
sudo install -m 0600 /dev/null /etc/alpaca-agent-trading/research-llm.env
sudoedit /etc/alpaca-agent-trading/research-llm.env
```

For OpenAI:

```dotenv
OPENAI_API_KEY=<research-provider-key>
```

Or for Anthropic:

```dotenv
ANTHROPIC_API_KEY=<research-provider-key>
```

The model is set in `config.yaml` under `research.strategy_llm.model` and
defaults to `gpt-5`. Set `research.strategy_llm.provider` to `anthropic` if you
supplied an Anthropic key.

Treat a custom provider base URL as a secret-bearing outbound destination.
`OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL` receives the provider key and bounded
research prompt; use only a trusted HTTPS endpoint. The application does not
currently enforce an LLM host allowlist. Provider-side spend/rate quotas are
also required for unattended use because the application bounds attempts,
timeouts, and response bytes but does not impose a daily currency budget.

## 7. Review the trading configuration

Open `config.yaml`. For a first deployment, verify these and change nothing
else:

```json
{
  "mode": "paper",
  "broker": {"paper": true, "allow_live": false},
  "strategy": {
    "selection_mode": "all_proved",
    "execution_mode": "shares"
  },
  "execution": {"time_in_force": "day"},
  "research": {
    "enabled": true,
    "require_validated_variant": true
  }
}
```

Two settings are worth understanding now, because they shape how long step 12
takes:

**`universe.symbols`** defaults to `["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV"]`.
Research takes at most one trade per symbol per session, so the 100-trade
evidence floor is as much a *universe width* requirement as a history-length
one. The eight-symbol default improves opportunity capacity, but real signal
rates still require sufficient history. Floor feasibility fails closed when the
100-trade floor cannot be supported; widen history and/or `universe.symbols`,
never lower the floor. Keep them US equity/ETF symbols.

**`strategy.execution_mode`** picks one profile per trader process:

- `shares` trades proved equity variants;
- `options` selects single-leg long listed options for proved option variants.

Research now studies only the vehicle your profile can trade, so this also
decides what gets researched. `options` is paper-only — see step 11.

`validate_config` also accepts an optional top-level `costs` block
(`spread_bps`, `slippage_bps`, `fee_bps`) — the expected per-fill cost every
research lane prices its simulated fills through, built by
`research/costs.py::CostModel.from_config`. It defaults to 2.0/3.0/0.5 bps when
the block is absent. These are expectations, not rejection caps, and the
`execution` block bounds them: a model whose expected half-spread plus
slippage exceeds `execution.max_slippage_bps`, or whose expected spread exceeds
`execution.max_spread_bps`, fails validation rather than simulating fills the
runtime would refuse to submit. Sourcing an expected slippage from the cap is
as wrong as ignoring the cap. **Leave the block out** unless you have measured
values; `python research.py calibrate` (see OPERATIONS.md) is how they are
checked against real fills.

## 8. Export deployment paths and validate Compose

Run these exports in every administrative shell, or put them in a root-owned
deployment environment file:

```bash
cd /opt/alpaca-agent-trading
export ALPACA_AGENT_SECRET_FILE=/etc/alpaca-agent-trading/agent.env
export ALPACA_RESEARCH_LLM_SECRET_FILE=/etc/alpaca-agent-trading/research-llm.env
```

If you skipped step 6, omit the second export.

Validate and build:

```bash
docker compose config --quiet
docker compose --profile research config --quiet
docker compose build
```

**Expected:** the two `config` commands print nothing (that is success), and
the build completes.

**If `docker compose config` errors** about a missing file, one of the two
exports above is unset or points at a path that does not exist. Any validation
or build failure is a deployment blocker — do not continue.

## 9. Run local safety and authentication checks

First, without contacting Alpaca at all:

```bash
docker compose run --rm trader python main.py check --offline
```

**Expected:**

```
mode: paper
paper: true
authenticated: false
local_config_valid: true
edge_checked: false
```

`--offline` validates local configuration only. It is **not** a trading
preflight and does not prove your keys work.

Now authenticate against the paper endpoint:

```bash
docker compose run --rm trader python main.py check
```

**Expected:** paper mode, the paper endpoint, your account details, and the
selected feeds. The command **may still exit non-zero** on a fresh install
because no edge is proved yet — that is expected here.

**A live endpoint or live-account response is a hard stop.** Recheck step 5.

**If authentication fails**, the usual causes are: live keys pasted into the
paper file, a typo in the secret, or a feed your account is not entitled to.

To prove the paper broker can both open and close an order, run the guarded
one-share smoke test during regular market hours. The paper account must start
flat with no open orders. Stop both broker-control services so nothing can race
the test:

```bash
docker compose stop trader watchdog
docker compose run --rm trader python main.py paper-smoke --symbol SPY --confirm PAPER
docker compose up -d trader watchdog
```

Success reports `status: ok`, filled entry and exit orders, `flat: true`, and
`open_orders: 0`. This proves broker plumbing only; it does not bypass the
validated-edge gate or authorize a strategy. If the command reports a cleanup
with `flat: false`, reconcile the Alpaca paper dashboard immediately and run:

```bash
docker compose run --rm trader python main.py flatten --reason paper-smoke-recovery
```

## 10. Backfill historical market data

This is the step that turns "months before anything happens" into "days".

The recorder only samples forward in real time, so a fresh install has no
history. Backfill fetches completed sessions from Alpaca and writes them into
exactly the same corpus format the recorder uses — same fields, same
per-session partitions, same index — so research cannot tell the difference and
no evidence gate is weakened.

```bash
docker compose run --rm recorder python deploy/backfill.py --days 180
```

**Expected:** a line per session as it is written, then a JSON summary:

```
backfilled 1560 rows for 2025-08-12
...
{"calendar_sessions": 124, "rows": 193440, "schema": "recorder-backfill.v1", ...}
```

Useful facts:

- **It is resumable.** Sessions that already have a partition are skipped, so
  re-running is a no-op and an interrupted run continues where it stopped.
- **Only completed sessions are written**, so the recorder's continuity checks
  are never confused by a half-finished day.
- **Options are not backfilled.** Option research needs sampled chain snapshots
  with quote-age semantics that no historical endpoint can reconstruct, and
  inventing them would fabricate the one thing option research depends on. If
  you plan to run the `options` profile, that lane still needs recorded
  sessions.
- `--quotes` also backfills quotes. It is far larger and rarely needed —
  without them, replay prices boundary fills from bars and charges the modelled
  half-spread.
- `--overwrite` re-fetches sessions you already have.

Run this **before** starting the recorder, or outside market hours, so the
recorder's first live cycle resumes cleanly from a completed session boundary.

## 11. Start recording market data

```bash
docker compose up -d recorder dashboard
docker compose ps
docker compose logs --tail=100 recorder
```

**Expected:** `recorded N Alpaca rows to ...` roughly once a minute during
market hours, and the recorder's health turning healthy.

Outside market hours the recorder is quiet — this is normal. Coverage is judged
against the Alpaca calendar, so holidays and early closes are silent. Intraday
bar gaps are retained as per-symbol index and health evidence rather than being
treated as automatic corpus corruption; replay gates still refuse a gap where
the strategy actually needs adjacent bars.

The recorder writes one append-only partition per New York session date under
`runtime/research/recorded/sessions/`, with a sidecar `.recorder-index.json`
holding the watermark and dedup state.

Recorder controls have working defaults:

- `ALPACA_RECORDER_OPTION_LIMIT` — contracts sampled per side per cycle
  (default 10, hard-capped at 25);
- `ALPACA_RECORDER_OPTION_HOLD_MINUTES` — how long an already sampled contract
  keeps being sampled (default 180), so a trade opened on a contract still has
  quotes at its exit;
- `ALPACA_RECORDER_BAR_GAP_MINUTES` — the intraday gap threshold recorded in
  coverage evidence (default 5);
- `ALPACA_RECORDER_STRICT_BAR_FEEDS` — optional comma-separated feeds whose
  observed gaps must stop the recorder; blank keeps coverage observational.

Raising the option limit or hold window increases quote volume and disk use
substantially. Compose forwards all recorder controls from your shell (or from
a deployment environment file), so
`ALPACA_RECORDER_OPTION_LIMIT=20 docker compose up -d recorder` is enough to
change one. The research lane forwards its own tuning variables the same way —
`ALPACA_FACTORY_STRATEGIES`, `ALPACA_FACTORY_VARIANTS`,
`ALPACA_FACTORY_WORKERS`, `ALPACA_FACTORY_MIN_TRADES`,
`ALPACA_FACTORY_MIN_SESSIONS`, `ALPACA_FACTORY_ALPHA`,
`ALPACA_FACTORY_MAX_GENERATIONS`, and `ALPACA_RESEARCH_SESSION_WINDOW` (limit a
cycle to the most recent N session partitions). Leave them at their defaults
for a first deployment.

## 12. Start the trader and the watchdog

```bash
docker compose up -d trader watchdog
docker compose logs --tail=150 trader
docker compose exec -T watchdog python deploy/health.py watchdog
```

**Expected:** the trader starts, reports healthy, and places **no trades**. On
a fresh deployment it must remain idle because no live-shadow-marked validated
edge exists. Do not
disable `research.require_validated_variant` to force entries — that guard is
part of the boundary standing between you and trading an unproven or
live-shadow-unmarked rule.

The watchdog should report `watching`. That is the steady state; `acted` means
it authenticated the scoped account and confirmed flattening. `degraded` or
`residual_risk: true` means it attempted to act but could not prove the account
flat and requires immediate broker reconciliation.

**The watchdog is not optional for the `options` profile.** Alpaca offers no
broker-resident stop on options, so an option position's stop is the trader's
own 60-second poller, and the watchdog is the only thing bounding it if that
process dies or hangs. It is a separate container with its own broker session
that can cancel and flatten but never enter. It takes the mode-scoped run lock
first, so a living trader keeps it inert; when the trader heartbeat is stale
beyond `ALPACA_WATCHDOG_MAX_HEARTBEAT_AGE` (default 300s) and the broker still
reports exposure, it keeps the lock through the final snapshot and action,
binds the authenticated account identity, cancels resting protective legs, and
flattens.

Two gaps it does **not** cover: a trader that is alive but wedged (it holds the
lock, so the watchdog stays inert), and an unreachable broker or network — in
that window nothing local can close anything. This is why live mode rejects the
options profile.

The runtime always uses day orders, rejects entries outside the NYSE regular
session, cancels working orders at startup, flattens residual positions, and
forces a close before the session ends.

## 13. Run the first research cycle

Start the scheduled research service:

```bash
docker compose --profile research up -d research
```

It runs daily at 03:00 UTC. To run one cycle immediately:

```bash
docker compose --profile research run --rm research \
  /bin/bash deploy/research-cycle.sh
```

This takes minutes to tens of minutes depending on corpus size. The last line
is a status record:

| Status | Meaning | Action |
| --- | --- | --- |
| `completed` | A cycle ran and produced proof artifacts. | None. |
| `completed_no_edge` | Valid run, nothing passed the gates. | **Normal.** Keep going. |
| `no_data` | The corpus is missing or empty. | Check steps 10–11. |
| `failed` | Operational error. | Read the logs; this is a real fault. |

`completed_no_edge` is what you should expect for a long time. It means the
evidence gates did their job.

The research Compose service defaults to two factory workers and a 10 GB
container limit. Override `ALPACA_FACTORY_WORKERS` and
`ALPACA_RESEARCH_MEMORY_LIMIT` in the deployment environment when the VM is
smaller or larger; leave headroom for the recorder, trader, watchdog, and
Docker itself.

The scheduled cycle runs `edge ingest-shadow` by default when
`ALPACA_SHADOW_INGEST_ENABLED=1`; a missing shadow WAL is a harmless no-op. The
offline cycle may produce a passing `lane=shadow` candidate, but that status is
only forward-stability evidence. Start the optional broker-free live lane and
then ingest its complete parity-matched sessions before validation/champion
selection can authorize the trader:

```bash
docker compose --profile shadow up -d shadow
docker compose --profile research run --rm research \
  python research.py edge ingest-shadow
```

ShadowRunner has no broker credentials or mutation path. It evaluates eligible
candidates in isolated virtual books from recorder events, writes only its WAL,
and records candidate/root-baseline/randomized-null exact-session replays.
Mismatch or incomplete rows are quarantined. Ingestion opens that WAL
read-only, requires strictly newer sessions, prior qualification, complete
parity, and matching source/config/code/provenance/replay/gate hashes; family
and global BH plus durable online FDR must pass before an immutable live marker
is appended. Manual/offline promotion cannot bypass it.

### How much data a first proof actually needs

The bare structural floor is 100 executed trades and 10 sessions per required
window (`ALPACA_FACTORY_MIN_TRADES`, `ALPACA_FACTORY_MIN_SESSIONS`), but those
floors apply to windows that are themselves fractions of the corpus:

- the latest 20% of sessions are sealed into a final qualification window
  before any worker runs, leaving 80% as development corpus;
- the development corpus is cut chronologically into a 70% fit and 30%
  held-out partition, by whole sessions;
- **both** partitions must clear the floor, and the held-out one binds: 10
  held-out sessions out of a 70/30 split needs about 31 development sessions,
  which after the 20% seal needs about 38 recorded sessions **with trades**;
- the offline forward-shadow evaluation is a second, strictly later corpus,
  sealed the same way, needing about 12 further sessions of its own; live
  ShadowRunner ingestion then needs a still-newer complete parity-matched tail;
- walk-forward needs a fit block plus three test blocks;
- the falsification gate is an empirical p-value against cluster-level
  sign-flip draws where a cluster is one session, so with C held-out sessions
  no draw distribution can put the observed mean below about 2⁻ᶜ. Alpha 0.05 is
  unreachable under about five held-out sessions. More held-out *sessions*, not
  more trades per session, is what makes the null test decidable.

The trade floors bind separately — 100 executed trades in each partition — and
the arithmetic above assumes every session produces trades, which no strategy
does. **Treat "about fifty trading sessions before a first proof is even
possible" as a floor on patience, not a schedule.** This is why step 10 and a
wider `universe.symbols` matter so much.

## 14. Read the reports

This is where you watch the system think. Run these from
`/opt/alpaca-agent-trading` with the exports from step 8 set.

### The one command that answers most questions

```bash
docker compose --profile research run --rm research \
  python research.py factory report
```

This prints the full discovery narrative: every slot, every hypothesis it has
held, where each came from, every variant tested with its performance, and
exactly why anything was retired. A slice of real output:

```
  SLOT 0

    gen 0  vwap_reversion  [retired]  via llm discovery
      id      hyp.equity.00.00.4b63bb08503e
      grammar rule-strategy.v2
      thesis  Late-morning stretch from session VWAP reverts when volume confirms.
      refuted if: The vwap reversion rule has no positive held-out and forward
                  expectancy after costs and multiple-test correction.
      proposed by openai/gpt-5 in 1 attempt(s)
        prompt ssssssssssssssss…  request rrrrrrrrrrrrrrrr…  response xxxxxxxxxxxxxxxx…
      variants tested: 2
        - rule.vwap-reversion.4b63bb08503e875b  [fail]  lane=backtest
            trades 20 (fit 14 / held-out 6)  net -20  maxDD 20
            held-out delta -1  lcb -1  q 1  win 0.35
            diagnosis: negative_expectancy
            failed: beat the baseline held-out; made money held-out; positive
                    expectancy per trade; beat randomized entry timing
      outcome: retired — LLM replacement registered after every intended variant failed
        after 2 of 2 intended variants failed adequately powered gates
        dominant failure mode: negative_expectancy
        replaced by hyp.equity.00.01.3af7ad929224

    gen 1  volume_breakout  [queued]  via llm replacement
```

Reading it:

- **`via llm discovery` / `via llm replacement` / `via template`** — who
  proposed this hypothesis. The hashes are the audit trail: prompt, request and
  response are content-hashed so a proposal can be traced without storing the
  raw model output.
- **`thesis`** — for an LLM-proposed hypothesis this is the model's own
  one-sentence rationale. This is the "LLM learnings" view. It is displayed
  text, never an instruction.
- **`refuted if`** — the falsification condition, written when the hypothesis
  was registered, before any result existed.
- **Per variant** — trades split fit/held-out, net P&L, max drawdown, the
  held-out edge over baseline (`delta`) and its lower confidence bound (`lcb`),
  the multiple-testing-corrected `q`, and win rate.
- **`failed:`** — plain-English list of exactly which of the 16 gates it
  missed. This is usually the most informative line on the page.
- **`outcome:`** — what happened and *why*, including **after how many
  variants**, the dominant failure mode, and what replaced it.

Useful variations:

```bash
# just one slot
python research.py factory report --slot 3
# machine-readable, for piping into your own tooling
python research.py factory report --format json
# a shareable document
python research.py factory report --format markdown > research-report.md
```

`--format markdown` includes a per-variant table and is the right thing to
archive or send to someone.

### Which edges are deployed, and how are they doing?

Two different questions, two commands.

**The evidence an edge was promoted with:**

```bash
python research.py edge status --vehicle equity
```

**What it has actually done since** — this is the live-performance view:

```bash
python research.py edge paper --deployed
```

This reports, per edge: trades, sessions, total and mean R, win rate, net P&L,
the rolling-R demotion guard with its floor and whether it is armed, and the
sequential drift statistic measured against the held-out distribution the edge
was validated on. An edge that stops working shows up here before it is
retired.

### Quick status

```bash
python research.py factory status   # raw lineage rows and counts
python report.py runtime/paper/journal.db --json   # account-level P&L
```

### The dashboard

Reach it through an SSH tunnel from your workstation:

```bash
ssh -L 8080:127.0.0.1:8080 <vm-user>@<vm-address>
```

Then open `http://127.0.0.1:8080`. It shows trader and recorder health, the
research scheduler's last cycle, the proved-edges table with promotion
confidence, a **live paper results by edge** table, and — if your profile
changed — a count of proved edges the current profile cannot trade.

The dashboard is read-only and localhost-bound. It is an observation aid, not
an execution console.

## 15. What a proved edge looks like

When a candidate finally earns a live-shadow proof:

1. `edge ingest-shadow` appends an immutable `lane=shadow` run and
   parity-matched live-ingestion marker; its ledger candidate may then become
   `validated` or `champion`.
2. A deterministic, content-addressed edge proof report is written under
   `research/results/edges/<vehicle>/`.
3. The dashboard lists it — but only while its latest verified shadow gate and
   live-ingestion marker still pass.
4. The paper trader may select it on a later cycle, under the global risk
   limits and the correlation cap.
5. Its slot is immediately reseeded with a **new** hypothesis. The proved rule
   itself is frozen and never re-tuned; the slot goes back to searching.

A deployed edge can still be **retired** — not re-tuned — if live paper results
degrade past the rolling-R floor or the sequential drift boundary. `edge paper`
is where you see that coming.

## 16. Daily operation

```bash
docker compose ps
docker compose logs --tail=100 recorder
docker compose logs --tail=100 trader
docker compose logs --tail=100 watchdog
docker compose --profile research logs --tail=100 research
```

For an operator-requested exit:

```bash
docker compose run --rm trader python main.py flatten --reason operator
```

A non-zero flatten result requires immediate broker reconciliation.

For a later operator recovery, stop the trader, run the authenticated checks,
flatten only if needed, then authorize the paused runtime and start it again:

```bash
docker compose stop trader
docker compose run --rm trader python main.py check
docker compose run --rm trader python main.py status
# only when positions or working orders remain:
docker compose run --rm trader python main.py flatten --reason operator
docker compose run --rm trader python main.py resume
docker compose start trader
```

`resume` is a strict authenticated, flat-only control command. It performs one
reconciliation plus a final read-only broker confirmation and does not cancel,
submit, or flatten anything. Never edit the runtime state or journal by hand.

## 17. Back up before updates

Back up the Docker volumes containing:

- `runtime/` and the execution journal;
- `research/cache/` and the edge ledger;
- `research/results/`, including generated edge proof reports;
- the reviewed `config.yaml` and deployed Git revision.

The backup must be off-host or on a different durable device. Do not use
`docker compose down -v` and do not prune volumes unless deletion is explicit
and the backup has been tested. The edge ledger is the accumulated result of
every research cycle you have ever run; losing it means starting the search
over.

See [OPERATIONS.md](OPERATIONS.md) for reconciliation, backup, recovery, and
incident procedures.

## 18. Optional guarded live mode

Do this only after extended paper validation and a separate review. Do not
reuse the paper runtime directory, secrets file, or running process.

Required live configuration:

```yaml
mode: live
broker:
  paper: false
  allow_live: true
strategy:
  selection_mode: pinned
  pinned:
    - id: <operator-assigned-promotion-id>
      variant_id: <exact-live-shadow-marked-validated-or-champion-variant>
      vehicle: equity
      strategy_id: rule
llm:
  enabled: false
research:
  enabled: true
  require_validated_variant: true
```

The live process also requires:

```bash
export ALPACA_LIVE_ENABLE=true
export ALPACA_PAPER=false
export ALPACA_AGENT_RUNTIME_ROOT=/opt/alpaca-agent-live-runtime
```

Note what live mode does **not** do: there is no automatic promotion from paper
to live. You pin one exact validated variant by hand, and it never auto-switches
for the process lifetime. The legacy `selection_mode: specific` form with one
exact `variant_id` remains supported, but `pinned` is preferred because its
operator-assigned id makes the promotion auditable.

Configuration validation rejects `mode: live` with `llm.enabled: true`: the
pinned edge was proven with the deterministic rule and no LLM in the loop, so a
runtime LLM veto would deploy a strategy that never passed the gates. It also
rejects `strategy.execution_mode: options` in live, for the protection reasons
in step 12. The bounded research adapter of step 6 is a separate offline
setting and is unaffected.

Live startup fails unless the exact named vehicle-local edge has a latest
passing verified shadow proof with the research-side parity-matched
live-ingestion marker. A legacy validated/champion row without that marker is
evaluated or migrated but remains ineligible until a new authorized live proof.
The account must report
`pattern_day_trader=true`.

The shipped Compose services are paper-scoped. Build a separately reviewed live
service/configuration rather than modifying the running paper deployment in
place.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Trader healthy but never trades | No live-shadow-marked validated edge yet | Expected. Check `factory report`, ShadowRunner, and `edge ingest-shadow`. |
| `research-cycle` says `no_data` | Corpus empty or path wrong | Re-run step 10; check the recorder volume. |
| `research-cycle` says `completed_no_edge` every night | Normal, or too little data | Check `factory report` — if variants show `underpowered`, widen `universe.symbols` and backfill more. |
| Every variant fails only on "held-out sample big enough" | Universe too narrow | Add symbols. This is the most common first-deployment wall. |
| Authentication fails | Live keys in the paper file, or unentitled feed | Recheck step 5. |
| `docker compose config` errors | Missing export | Re-run step 8. |
| Watchdog says `acted` | It flattened a stale trader's positions | Reconcile per OPERATIONS.md before restarting. |
| Recorder health lists `bar_gap_symbols` | Sparse feed coverage or a real hole inside a session | Compare the exact index gap with the provider; let research adjacency gates decide usability. Do not delete the corpus. |
| Dashboard shows "proved but untradeable" > 0 | Edges proved under a different `execution_mode` | Expected after a profile change; harmless. |
| LLM proposals never appear in the report | No provider key, or proposals rejected | Check step 6; the report shows rejection reasons. |

---

## Local developer setup (without a VM)

For tests and offline development:

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
chmod 600 .env
./.venv/bin/python main.py check --offline
PYTHONPATH=. ./.venv/bin/python -W error -m unittest discover -s tests -t . -p 'test_*.py' -q
```

The test suite uses fakes and needs no credentials. Do not use the developer
path as an unattended production deployment.
