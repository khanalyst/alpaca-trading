# Setup and deployment

This is the installation authority for the Alpaca intraday runtime. Paper is
the default account mode. The supported universe is US-listed equities/ETFs
and listed OCC options; options are single-leg long intraday calls or puts.
Crypto, multi-leg, naked, and short option structures are unsupported. A
trader process runs one execution profile (`shares` or `options`) at a time.

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

Use paper credentials only for the default lane. Keep withdrawal permissions
disabled and do not put the file in Git. The checked research config enables a
bounded replacement adapter (`gpt-5`); if used, load provider keys only from a
separate optional `ALPACA_RESEARCH_LLM_SECRETS_FILE`. Invalid or missing LLM
output leaves a pending replacement and does not retire a family. Runtime
decision LLM use remains disabled (`llm.enabled: false`); deterministic,
content-addressed rule execution remains the runtime default.

For Compose, set `ALPACA_RESEARCH_LLM_SECRET_FILE` to the host path of that
separate file; Compose mounts it read-only and the research container exposes
it as `ALPACA_RESEARCH_LLM_SECRETS_FILE`. Leaving it unset mounts an empty
secret and keeps failed replacements pending.

The separate file is dotenv-style and should contain only the selected model
provider credentials, for example `OPENAI_API_KEY=...` (or
`ANTHROPIC_API_KEY=...`), plus an optional provider base URL.

Run the local checks before starting a loop:

```bash
./.venv/bin/python main.py check --offline
./.venv/bin/python main.py check
./.venv/bin/python main.py status
./.venv/bin/python main.py run
```

`check` is the authenticated preflight by default and must confirm the Alpaca
paper endpoint, credentials, market clock, and feed settings. `check --offline`
validates local configuration only; it never authenticates or serves as a
trading preflight. On a fresh edge ledger the trader will remain safely idle
until research has passed an initial backtest and a strictly later unseen
shadow tail. Confirm that expected hold in `main.py status`, then run the
trader once in the foreground before switching to Compose or systemd.

## Explicit live mode

Live mode is not a paper-lane toggle. Use a separately reviewed config,
credentials, and runtime root (`ALPACA_AGENT_RUNTIME_ROOT`) and stop the paper
process before starting it. All of the following are required:

```yaml
mode: live
broker:
  paper: false
  allow_live: true
strategy:
  selection_mode: specific
  variant_id: <exact-validated-or-champion-variant>
```

Set `ALPACA_LIVE_ENABLE=true` in that live process and do not set
`ALPACA_PAPER=true`. The named edge must already be `validated` or `champion`
in the vehicle-local ledger. Live startup pins its candidate/configuration and
does not auto-switch to a new champion. The authenticated live preflight also
requires the account to report `pattern_day_trader=true`; a missing or false
value blocks startup. The shipped Compose and systemd launch lanes set
`ALPACA_PAPER=true` and remain paper defaults.

## Behavioural defaults

- Strategy: paper `selection_mode: all_proved` selects one best proven variant
  per independent family under one global risk book; `specific` pins one
  validated/champion variant. IBR remains an explicit baseline and replay
  contract.
- Instruments: US-listed equities/ETFs and listed OCC options, with options
  limited to single-leg long contracts selected from a validated chain.
- Entries: regular NYSE session only, with a close-time cutoff.
- Orders/exits: `time_in_force: day`; startup cancels working orders and
  flattens residuals, and a mandatory pre-close flatten leaves no overnight
  position or option contract.
- Account: Alpaca paper endpoint and paper credentials by default; live mode
  requires the separate guard and scope above.
- Data: `iex` stock feed by default; set an entitled feed explicitly if the
  paper account supports one. Record the feed in run metadata.

Every generated hypothesis is unproven. The research cycle runs seven
independent strategy families in parallel by default, with four isolated
simulated accounts per family. Fit-only diagnostics create bounded variants;
untouched held-out data decides whether they pass. Lifecycle proof requires fit
and held-out structural floors, matched controls, placebo/falsification,
family-level FDR, and a durable verified gate before validation/champion
selection. Underpowered data is not failure. Retirement is allowed only after
all intended variants are adequately tested and fail; when LLM replacement is
enabled, a valid bounded replacement must be registered first. The lifecycle is
forward-only: an initial corpus backtest precedes a strictly later unseen
shadow tail. Paper outcomes are append-only forward evidence and may demote a
champion. Runtime entries remain blocked without a validated/champion SQLite
record.

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

Research is a broker-independent profile and does not run on a default
startup. When the recorder has written the mixed bars/quotes/options dataset at
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
manual `edge promote` remains an audited control subject to lifecycle/evidence
rules. Backward rollback is rejected; explicit demotion is the operator safety
action.

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
`/etc/alpaca-agent-trading/research.env` file. Research has no broker
credentials and is optional as a service; if the bounded LLM replacement is
used, point `ALPACA_RESEARCH_LLM_SECRETS_FILE` at its separate provider-secret
file. The trader still requires a validated/champion edge record in SQLite
before opening entries.

Compose and the systemd application lane are alternatives. Do not enable both
on one host. The trader remains one replica/process. Put credentials in an
EnvironmentFile outside the checkout and set `ALPACA_AGENT_SECRETS_FILE` only
where the launcher needs it. These shipped units are paper-scoped; use a
separate reviewed live unit/config/runtime if live mode is approved.

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
