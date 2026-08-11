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
| `watchdog` | Independent stale-trader flatten; cancel and close only, never entries | `runtime-data` |
| `dashboard` | Read-only localhost health and reports | Read-only mounts |

All services run as UID/GID 10001, drop Linux capabilities, use a read-only
root filesystem, and receive only the secret/config mounts they need. The
dashboard receives no credentials. Health checks monitor recorder freshness,
trader state, research progress, and dashboard availability. Recorder health is
reported independently and does not gate trader startup.

The `watchdog` service exists because Alpaca provides no broker-resident stop
for options: it reads the trader heartbeat from the shared runtime volume and,
when that heartbeat is stale beyond `ALPACA_WATCHDOG_MAX_HEARTBEAT_AGE` (300s)
and the broker still reports positions, it cancels resting protective legs and
flattens through its own broker session. It first takes the mode-scoped run
lock, so a running trader keeps it inert; it can never submit an entry. It
cannot help when the broker or the network is unreachable.

The recorder writes its mixed bars/quotes/options corpus partitioned by New
York session date under `runtime/research/recorded/sessions/market-<date>.csv`,
with a sidecar `.recorder-index.json` holding the watermark, the per-symbol last
bar, a fifteen-minute dedup window and the option contracts held open for
continued sampling. A cycle costs O(new rows); the index is verified against
partition sizes on load and rebuilt from the partitions when they disagree. A
legacy single-file `market.csv` is partitioned in place on the first run after
upgrade and kept beside the corpus as `market.csv.migrated`.

Option sampling takes `ALPACA_RECORDER_OPTION_LIMIT` contracts per side per
sample (default 10, capped at 25) and keeps every contract it has sampled in
the sample for `ALPACA_RECORDER_OPTION_HOLD_MINUTES` (default 180) so a trade
opened on a contract still has quotes at its exit. Continuity gaps are judged
against the Alpaca calendar, cached per fetch window, so holidays and early
closes are quiet and a genuine intraday hole still fails closed.

The research profile is disabled by default. `deploy/research-cycle.sh`
discovers and routes the corpus automatically, concatenating partitions in
session order; `ALPACA_RESEARCH_SESSION_WINDOW` limits that to the most recent
N sessions, and `ALPACA_RESEARCH_DATASET` can override the source with
normalized JSONL. Start it with
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
- `alpaca-watchdog.service` — flatten-only watchdog; enable it with the
  trader, since it is the only bound on the option profile's software stop;
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
