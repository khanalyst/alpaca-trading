# Setup

This guide installs the paper deployment defined by compose.yaml, config.yaml,
and deploy/*.service. The default runtime is paper-only, uses the IEX equity
feed, and stays entry-blocked until the research ledger has an eligible proof.
Idle time on a new installation is expected.

The shipped execution profile is US-listed equities/ETFs. Single-leg long
options require a separate OPRA profile and matching evidence; that profile is
not enabled here. Crypto, short/multi-leg options, and overnight positions are
unsupported. Read [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) before provisioning
Azure.

## Prerequisites

- Alpaca paper account, API key, and secret.
- Ubuntu x86-64 (or equivalent) with Docker Engine and Compose v2.
- Durable storage for runtime and research cache/results.
- SSH access; the dashboard must remain loopback-only.
- A separate OpenAI or Anthropic key. The checked research.strategy_llm lane is
  enabled and Compose requires its secret file.

Use paper credentials only. Live mode is a separately guarded deployment.

## Install and checkout

Docker Engine, the Docker Compose v2 plugin, and Git must already be installed
through the host’s approved administration process. This guide intentionally
does not select a vendor repository or provide package-install commands.
Verify the host prerequisites before continuing:

    docker version
    docker compose version
    git --version

The invoking user must be able to run Docker commands. If Docker or Compose is
missing, stop here and have the host administrator install and configure it.

    sudo install -d -o "$USER" -g "$USER" /opt/alpaca-agent-trading
    git clone <repository-url> /opt/alpaca-agent-trading
    cd /opt/alpaca-agent-trading

If another path is used, adjust systemd unit paths and working directory.

The Compose lane supplies Python and locked dependencies inside the image. An
optional Python 3.13 local environment is useful for read-only development
tools and the verification commands in the root README:

    python3 --version  # use Python 3.13
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.lock.txt

This local environment is not required for Compose. The systemd units are a
separate host lane and require the restricted `alpaca` user, their own
installed Python interpreter and dependencies at
`/opt/alpaca-agent-trading/.venv/bin/python`, writable runtime/cache/results
directories, and credentials readable by that user before enabling the units;
the Compose image and its host-file permissions do not satisfy those
requirements.

## Secrets

Keep credentials outside Git. The following is an initial-install procedure;
do not rerun the file-creation commands over an existing populated secret.
Compose uses file-backed secrets, so the host files must be readable by the
container’s `10001:10001` user/group. The `uid`, `gid`, and `mode` fields in
Compose do not rewrite permissions for a file source; see the [Compose secrets
reference](https://docs.docker.com/reference/compose-file/services/#secrets).

    sudo install -d -o "$USER" -g 10001 -m 0750 /etc/alpaca-agent-trading
    sudo install -o "$USER" -g 10001 -m 0640 deploy/agent.env.example /etc/alpaca-agent-trading/agent.env

Populate `/etc/alpaca-agent-trading/agent.env` with your editor as its owner,
then remove owner-write permission:

    chmod 0440 /etc/alpaca-agent-trading/agent.env

The paper file contains ALPACA_API_KEY, ALPACA_SECRET_KEY,
ALPACA_PAPER=true, ALPACA_LIVE_ENABLE=false, ALPACA_DATA_FEED=iex,
ALPACA_STOCK_FEED=iex, and ALPACA_OPTIONS_FEED=indicative.

Create a separate research-provider file:

    sudo install -o "$USER" -g 10001 -m 0640 deploy/research-llm.env.example /etc/alpaca-agent-trading/research-llm.env

Populate `/etc/alpaca-agent-trading/research-llm.env` with your editor as its
owner, then protect it:

    chmod 0440 /etc/alpaca-agent-trading/research-llm.env

Use OPENAI_API_KEY for the checked provider, or ANTHROPIC_API_KEY after a
reviewed provider change. Never put provider keys in agent.env, .env, or the
image. If OPENAI_BASE_URL is used, deployment must match the configured
research.strategy_llm.deployment exactly.

The systemd research unit also reads deploy/research.env.example; copy it to
/etc/alpaca-agent-trading/research.env only for systemd. Provision systemd
credential readability separately for the restricted `alpaca` user; do not
assume the Compose group-`10001` permissions above are sufficient for systemd.

## Validate, bootstrap, and start Compose

Point Compose at the external files, validate the Compose model, build the
image, and bootstrap only the data/proof services first:

    export ALPACA_AGENT_SECRET_FILE=/etc/alpaca-agent-trading/agent.env
    export ALPACA_RESEARCH_LLM_SECRET_FILE=/etc/alpaca-agent-trading/research-llm.env
    test -z "$(git status --porcelain=v1 --untracked-files=all)"
    export ALPACA_DEPLOYMENT_COMMIT="$(git rev-parse --verify 'HEAD^{commit}')"
    docker compose config --quiet
    docker compose build
    docker compose run --rm --no-deps trader python main.py check --offline
    docker compose up -d recorder research shadow dashboard
    docker compose ps

`check --offline` validates local configuration without contacting Alpaca.
`ALPACA_DEPLOYMENT_COMMIT` must be the full reviewed `HEAD` commit and must be
preserved for every Compose command and restart. An omitted/unknown or
mismatched identity can block session acceptance and cross-service parity.
`shadow-init` starts automatically as the shadow service dependency.

The authenticated check is intentionally run against this data-only bootstrap:

    docker compose run --rm --no-deps trader python main.py check

The authenticated `check` queries the configured paper account, broker clock,
calendar, and feeds; it is the broker-facing preflight. On a fresh default
install it normally exits nonzero with `edge_required` and `edge_ready: false`
because no validated edge exists yet. This is an expected readiness refusal,
not a successful check. Keep `trader` and `watchdog` stopped while the recorder,
research, and shadow services collect data and produce proof evidence. Do not
bypass or relabel the missing-edge refusal.

The model should retain a read-only application filesystem, unprivileged
services, dropped capabilities, one trader replica, and loopback-only port
8080. Services are recorder, trader, watchdog, research, shadow, and dashboard.
shadow-init is an expected short-lived initializer with no credentials/network.

Verify:

    docker compose logs --tail=100 recorder research shadow dashboard

After the authenticated check succeeds with a resolved edge and the account,
state, and deployment-identity gates are satisfied, start the order-capable
services:

    docker compose up -d trader watchdog
    docker compose ps
    docker compose exec -T trader python main.py check
    docker compose run --rm --no-deps trader python main.py inspect-deployment

`inspect-deployment` is the explicit authenticated, non-initializing,
read-only paused-and-flat inspection. A fresh or running runtime may return
nonzero because it is not explicitly paused and flat; that describes
inspection eligibility, not a Docker/startup failure. Use `status` separately
when an operational projection is wanted.

The dashboard is local or via SSH tunnel:

    ssh -N -L 8080:127.0.0.1:8080 <host>

## Recorder and research

The recorder writes normalized append-only session partitions and sidecars.
Historical backfill is diagnostic and cannot become forward authorization
evidence:

    docker compose run --rm --no-deps recorder python deploy/backfill.py --out /app/runtime/research/recorded --config /app/config.yaml --days 30

Use --quotes only when the larger dataset is intentional; avoid --overwrite on
data that must be preserved. The scheduled cycle runs
deploy/research-cycle.sh; systemd uses alpaca-research.timer. It validates
data, runs enabled diagnostics, consumes shadow WAL input, and writes
ledger/proof artifacts. See [docs/edge-diagnostics.md](docs/edge-diagnostics.md)
and [docs/trader-research-v6.md](docs/trader-research-v6.md).

    docker compose run --rm --no-deps research python research.py edge status
    docker compose run --rm --no-deps research python research.py edge promotable --vehicle equity
    docker compose run --rm --no-deps research python research.py factory status

## Systemd alternative

Use Compose or systemd, not duplicate trader owners:

    sudo cp deploy/alpaca-recorder.service /etc/systemd/system/
    sudo cp deploy/alpaca-trader.service /etc/systemd/system/
    sudo cp deploy/alpaca-watchdog.service /etc/systemd/system/
    sudo cp deploy/alpaca-research.service /etc/systemd/system/
    sudo cp deploy/alpaca-research.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now alpaca-recorder alpaca-trader alpaca-watchdog
    sudo systemctl enable --now alpaca-research.timer

Keep runtime and research cache/results on durable storage.

## Safety boundaries

- Do not edit runtime state, trial identities, or account fingerprints to make
  a blocked runtime appear ready.
- An orderly signal shutdown can latch operator_pause=true; restart/deploy does
  not clear it. The supervisor waits for authenticated flat-only resume.
- paper_trial.state=running is independent, diagnostic, and non-authorizing.
  Identity changes are rejected; replacement requires a terminal trial and an
  account-scoped flat book. See [docs/paper-shadow-trials.md](docs/paper-shadow-trials.md).

See [OPERATIONS.md](OPERATIONS.md) for controls and
[ARCHITECTURE.md](ARCHITECTURE.md) for ownership.
