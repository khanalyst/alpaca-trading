# Setup and deployment

This is the installation authority for the Alpaca intraday paper-trading
runtime. The default account is Alpaca paper, the data universe is US stocks
and ETFs, and options are available only through defined-risk intraday
profiles. Live trading is unsupported and explicitly guarded in the provider.

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
put the file in Git. `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` are optional and are
needed only by an explicitly enabled research or analyst feature.

Run the local checks before starting a loop:

```bash
./.venv/bin/python main.py check
./.venv/bin/python main.py status
./.venv/bin/python main.py run
```

`check` must confirm the Alpaca paper endpoint, credentials, market clock, and
feed settings. It must fail closed if a live endpoint is requested. Run the
trader once in the foreground first; only then use Compose or systemd.

## Behavioural defaults

- Strategy: initial balance range (IBR), normally 09:30–09:45 America/New_York.
- Instruments: liquid US equities/ETFs; options are selected from a validated
  chain and traded only as defined-risk structures.
- Entries: regular NYSE session only, with a close-time cutoff.
- Exits: stop/target and risk controls, followed by a mandatory pre-close
  flatten. No position may cross the session close.
- Account: paper endpoint and paper credentials; there is no live-ready mode.
- Data: `iex` stock feed by default; set an entitled feed explicitly if the
  paper account supports one. Record the feed in run metadata.

The IBR hypothesis is unproven. Research output is evidence for review, never
an automatic promotion or configuration mutation.

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

The Compose services are `recorder`, `trader`, optional `research`, and
`dashboard`;
their container names are `alpaca-recorder`, `alpaca-trader`,
`alpaca-research`, and `alpaca-dashboard`. The dashboard binds to localhost
only. Use an SSH tunnel or a private network rather than exposing port 8080.
Do not run a second trader against the same paper account and journal.

Named volumes survive an ordinary `docker compose down`. Do not use
`down -v` or prune volumes unless the journal and research artifacts have been
backed up and the reset is intentional.

Research is an offline profile and does not run on a default startup. Supply a
normalized JSONL dataset explicitly when needed:

```bash
export ALPACA_RESEARCH_DATASET=/app/runtime/research/input/bars.jsonl
docker compose --profile research up -d research
```

## Legacy systemd lane

For hosts that do not use Compose, install the trader/recorder units and the
optional research units in `deploy/` as the `alpaca` service user:

```bash
sudo useradd --system --home /opt/alpaca-agent-trading --shell /usr/sbin/nologin alpaca
sudo install -d -o alpaca -g alpaca /opt/alpaca-agent-trading
sudo cp deploy/alpaca-*.service deploy/alpaca-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alpaca-recorder.service
sudo systemctl enable --now alpaca-trader.service
```

Enable `alpaca-research.timer` only after setting `ALPACA_RESEARCH_DATASET` to
a normalized JSONL input in the service EnvironmentFile. Research is offline
and never a prerequisite for paper trading.

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
