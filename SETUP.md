# Setup and deployment

This is the installation authority for the Alpaca intraday paper-trading
runtime. The default account is Alpaca paper, the data universe is US stocks
and ETFs, and options are available only as single-leg long intraday calls or
puts. A trader process runs one execution profile (`shares` or `options`) at a
time; multi-leg, naked, and short option structures are unsupported. Live
trading is unsupported and explicitly guarded in the provider.

## Local setup

Requirements: Python 3.12+, Git, Docker Compose v2 (for the container path),
and an Alpaca paper account. The repository does not contain credentials.

```bash
git clone <repository> alpaca-agent-trading
cd alpaca-agent-trading
python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

```dotenv
ALPACA_API_KEY=your-paper-key
ALPACA_SECRET_KEY=your-paper-secret
ALPACA_PAPER=true
ALPACA_DATA_FEED=iex
ALPACA_STOCK_FEED=iex
ALPACA_OPTIONS_FEED=indicative
```

Use paper credentials only. Keep withdrawal permissions disabled and do not
put the file in Git. `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` are optional. LLM use
is disabled by default and those keys are needed only when `llm.enabled: true`
is explicitly set for an analyst feature. Deterministic, content-addressed
rule execution remains the default.

Run the local checks before starting a loop:

```bash
./.venv/bin/python main.py check --offline
./.venv/bin/python main.py check
./.venv/bin/python main.py status
./.venv/bin/python main.py run
```

`check` is the authenticated preflight by default and must confirm the Alpaca
paper endpoint, credentials, market clock, and feed settings; it fails closed
if a live endpoint is requested. `check --offline` validates local
configuration only and is not a trading preflight. On a fresh edge ledger the
trader will remain safely idle until research has passed an initial backtest
and a strictly later unseen shadow tail. Confirm that expected hold in
`main.py status`, then run the trader once in the foreground before switching
to Compose or systemd.

## Behavioural defaults

- Strategy: `rule/auto`; the runtime accepts only a validated/champion rule
  selected from the autonomous research ledger. IBR remains an explicit
  baseline and replay contract.
- Instruments: liquid US equities/ETFs, or single-leg long options selected
  from a validated chain, according to the configured execution profile.
- Entries: regular NYSE session only, with a close-time cutoff.
- Exits: stop/target and risk controls, followed by a mandatory pre-close
  flatten. No position or option contract may cross the session close.
- Account: paper endpoint and paper credentials; there is no live-ready mode.
- Data: `iex` stock feed by default; set an entitled feed explicitly if the
  paper account supports one. Record the feed in run metadata.

Every generated hypothesis is unproven. The research cycle runs seven strategy
families in parallel by default, with four isolated simulated accounts per
family. Fit-only diagnostics create bounded variants; untouched held-out data
decides whether they pass. An adequately tested family with no positive edge
is retired and a replacement hypothesis is queued automatically. The edge
lifecycle remains forward-only for proof: an initial corpus backtest must pass before a
later unseen shadow tail can validate and select a champion; paper outcomes
are appended for forward monitoring and may demote a champion. Runtime entries
remain blocked without a validated/champion SQLite record.

For an operator-initiated close, run
`./.venv/bin/python main.py flatten --reason operator` (or the equivalent
Compose command) and treat a non-zero exit as an incomplete flatten requiring
broker reconciliation.

## Docker Compose on a VM

The supported container path uses an Ubuntu VM with Docker Engine and Compose
v2. Keep the checkout and volumes on a durable disk. A typical layout is
`/opt/alpaca-agent-trading` for source and `/etc/alpaca-agent-trading/agent.env`
for the secret file (mode 0600, owned by root). Set the file path when running
Compose:

```bash
cd /opt/alpaca-agent-trading
export ALPACA_AGENT_SECRET_FILE=/etc/alpaca-agent-trading/agent.env
docker compose config
docker compose build
docker compose up -d recorder trader dashboard
docker compose ps
docker compose logs --tail=100 trader
```

The trader may start before research, but it cannot open an entry on a fresh
ledger. Start the research profile after the recorder has an initial corpus;
keep recording so a later unseen tail can qualify a champion. Do not weaken
the entry gate to make a new deployment trade immediately.

The Compose services are `recorder`, `trader`, optional `research`, and
`dashboard`;
their container names are `alpaca-recorder`, `alpaca-trader`,
`alpaca-research`, and `alpaca-dashboard`. The dashboard binds to localhost
only. Use an SSH tunnel or a private network rather than exposing port 8080.
Do not run a second trader against the same paper account and journal.

Named volumes survive an ordinary `docker compose down`. Do not use
`down -v` or prune volumes unless the journal and research artifacts have been
backed up and the reset is intentional.

Research is an offline profile and does not run on a default startup. When the
recorder has written the mixed bars/quotes/options dataset at
`runtime/research/recorded/market.csv`, the research cycle discovers and routes
it automatically. An explicit normalized JSONL dataset can override it when
needed:

```bash
export ALPACA_RESEARCH_DATASET=/app/runtime/research/input/market.jsonl
docker compose --profile research up -d research
```

The edge-lab ledger defaults to `runtime/research/edge_lab.sqlite3` (override
with `ALPACA_EDGE_DB`). Inspect its append-only lifecycle with
`python research.py edge status`. The scheduled cycle performs normal
backtest-to-unseen-shadow validation and champion selection automatically;
manual `edge promote`/rollback commands are available only as audited controls
subject to lifecycle/evidence rules; demote, retire, and rollback are operator
safety actions.

## Legacy systemd lane

For hosts that do not use Compose, install the trader/recorder units and the
research units in `deploy/` as the `alpaca` service user. The research process
has no broker credentials, but it must produce a champion before entries are
enabled on a fresh deployment:

```bash
sudo useradd --system --home /opt/alpaca-agent-trading --shell /usr/sbin/nologin alpaca
sudo install -d -o alpaca -g alpaca /opt/alpaca-agent-trading
sudo cp deploy/alpaca-*.service deploy/alpaca-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alpaca-recorder.service
sudo systemctl enable --now alpaca-trader.service
```

Enable `alpaca-research.timer` after the recorder has produced its default
dataset, or set `ALPACA_RESEARCH_DATASET` to a normalized JSONL input in the
credential-free `/etc/alpaca-agent-trading/research.env` file. Research runs
offline and is optional as a service,
but the trader still requires a validated/champion edge record in SQLite
before opening entries.

Compose and the systemd application lane are alternatives. Do not enable both
on one host. The trader remains one replica/process. Put credentials in an
EnvironmentFile outside the checkout and set `ALPACA_AGENT_SECRETS_FILE` only
where the launcher needs it.

## Azure notes

Create or select the VM, network rules, managed disk, and backup destination
according to your organization's policy. This repository does not provision
Azure resources. Before deleting a VM, verify that a backup of
`runtime/`, `research/cache/`, `research/results/`, and `findings/` exists on a
different device or off-host service. A second directory on the VM is not an
off-host backup.

See [OPERATIONS.md](OPERATIONS.md) for update, backup, session-close,
reconciliation, and incident procedures. See [deploy/README.md](deploy/README.md)
for service ownership and health checks.
