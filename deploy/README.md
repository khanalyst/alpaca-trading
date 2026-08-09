# Deployment topology

Installation is in [`../SETUP.md`](../SETUP.md); operation, backup, and
recovery are in [`../OPERATIONS.md`](../OPERATIONS.md). This file records
process ownership and the two supported paper launch lanes. The runtime scope
is US-listed equities/ETFs and listed OCC options only; crypto is rejected.

## Docker Compose (recommended)

[`../compose.yaml`](../compose.yaml) runs one least-privilege image:

| Service | Responsibility | Durable state |
| --- | --- | --- |
| `recorder` | Alpaca bars, quotes, and session observations (paper by default) | `runtime-data` |
| `trader` | Exactly one intraday loop in one configured execution profile and broker reconciliation | `runtime-data`, `research-cache` |
| `research` (profile `research`) | Scheduled seven-strategy parallel factory, validation, and replay; no broker authority | `runtime-data`, research volumes |
| `dashboard` | Read-only localhost health and reports | Read-only mounts |

All services run as UID/GID 10001, drop Linux capabilities, use a read-only
root filesystem, and receive only the secret/config mounts they need. The
dashboard receives no credentials. Health checks monitor recorder freshness,
trader state, research progress, and dashboard availability. Recorder health is
reported independently and does not gate trader startup.

The research profile is disabled by default. When the recorder has produced
the mixed bars/quotes/options dataset at
`runtime/research/recorded/market.csv`, `deploy/research-cycle.sh` discovers
and routes it automatically; `ALPACA_RESEARCH_DATASET` can override that
source with normalized JSONL. Start it with
`docker compose --profile research up -d research`. The edge ledger is stored
at `runtime/research/edge_lab.sqlite3` (override with `ALPACA_EDGE_DB`) and is
read-only from the dashboard. Research cannot place orders or mutate broker
state. Paper `selection_mode: all_proved` runs one best proven variant per
independent family under one global risk book. Defaults are seven concurrent
strategy workers and four isolated variant accounts per strategy; capacity is
configurable through the `ALPACA_FACTORY_*` environment variables.

The default Compose lane passes the Alpaca paper endpoint and feed settings
explicitly through `ALPACA_PAPER`, `ALPACA_DATA_FEED`, and
`ALPACA_OPTIONS_FEED`. Credentials are mounted from
`ALPACA_AGENT_SECRET_FILE`, never copied into the image. Named volumes survive
ordinary `docker compose down`; a second trader or `down -v` can
corrupt/delete operational state.

Before market-data or order calls, run `python main.py check`; this is the
authenticated preflight by default (`--offline` is local configuration only,
not a trading preflight). Orders are day-only; startup cleanup cancels working
orders and flattens residuals, and the session policy force-flats before the
regular NY close. The checked research config enables bounded `gpt-5` strategy
replacement, using credentials only from optional
`ALPACA_RESEARCH_LLM_SECRETS_FILE`; invalid or missing output leaves a pending
replacement. Runtime decision LLM use remains disabled. The trader opens
entries only when the SQLite ledger has a vehicle-local `validated` or
`champion` edge for its configured execution profile.

For Compose, set `ALPACA_RESEARCH_LLM_SECRET_FILE` to the host path of the
separate research-provider dotenv file. It is mounted read-only as
`/run/secrets/research_llm_credentials`; broker credentials are not mounted
into the research service.

The shipped Compose and systemd lanes are paper-scoped. A live deployment is a
separate reviewed config/runtime scope: `mode: live`, `broker.paper: false`,
`broker.allow_live: true`, `ALPACA_LIVE_ENABLE=true`,
`strategy.selection_mode: specific`, and one exact named validated/champion
`strategy.variant_id`. The runtime pins that edge and does not auto-switch.
The authenticated live preflight also requires `pattern_day_trader=true`.
Research lifecycle gates include fit/held-out structural floors, matched
controls, placebo/falsification, FDR, and durable verification; underpowered
data is not failure. A valid bounded LLM replacement is registered before an
adequately tested failed family can retire. Scheduler terminal statuses are
`completed`, `completed_no_edge`, `no_data`, and `failed`.

## Legacy systemd lane

For an existing non-Compose host, install these units as the restricted
`alpaca` user:

- `alpaca-recorder.service` — recorder;
- `alpaca-trader.service` — one paper trader process and one execution profile;
- `alpaca-research.service` and `alpaca-research.timer` — scheduled research.

The units are alternatives to Compose. Do not enable both lanes on one host or
run a second trader against the same paper account. Keep the EnvironmentFile
outside Git and set `ALPACA_AGENT_SECRETS_FILE` only for the processes that
need it. Disable the lane before migrating to Compose.

## Backup override

`compose.external-backup.yaml` is optional and only valid when
`ALPACA_EXTERNAL_BACKUP_PATH` is a verified different-device or off-host mount.
A normal directory on the VM does not prove recovery from VM loss. See the
backup verification steps in [`../OPERATIONS.md`](../OPERATIONS.md).
