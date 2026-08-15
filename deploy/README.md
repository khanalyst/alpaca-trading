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
| `research` (profile `research`) | Scheduled seven-slot offline factory/replay and shadow-WAL ingestion; no broker authority | `runtime-data`, research volumes |
| `watchdog` | Independent stale-trader flatten; cancel and close only, never entries | `runtime-data` |
| `dashboard` | Read-only localhost health and reports | Read-only mounts |
| `shadow` (profile `shadow`) | Broker-free incremental shadow evaluation and semantic replay parity; no broker authority | Read-only recorder/EdgeLedger mounts, isolated shadow WAL |

All workload services run as UID/GID 10001, drop Linux capabilities, use a
read-only root filesystem, and receive only the secret/config mounts they
need. The short-lived `shadow-init` setup service is the deliberate exception:
it has no credentials or network and only repairs the ownership of the shadow
WAL directory before ShadowRunner starts. The
dashboard receives no credentials. Health checks monitor recorder freshness,
trader state, research progress, and dashboard availability. Recorder health is
reported independently and does not gate trader startup.

The `watchdog` service exists because Alpaca provides no broker-resident stop
for options: it reads the trader heartbeat from the shared runtime volume and,
when that heartbeat is stale beyond `ALPACA_WATCHDOG_MAX_HEARTBEAT_AGE` (300s)
and the broker still reports positions, it cancels resting protective legs and
flattens through its own broker session. It first takes the mode-scoped run
lock, so a running trader keeps it inert; it can never submit an entry. It
keeps that lock through the final position snapshot and flatten action,
authenticates and binds the broker account fingerprint before any mutation,
and reports `acted` only after flattening is confirmed. An incomplete attempt
is `degraded` with residual risk and fails health checks. It cannot help when
the broker or the network is unreachable, or while a wedged trader still owns
the lock.

The recorder writes its mixed bars/quotes/options corpus partitioned by New
York session date under `runtime/research/recorded/sessions/market-<date>.csv`,
with a sidecar `.recorder-index.json` holding the watermark, the per-symbol last
bar and bounded coverage evidence, the corpus equity feed, a fifteen-minute
dedup window and the option contracts held open for continued sampling. A cycle
costs O(new rows); the index is verified against partition sizes on load and
rebuilt from the partitions when they disagree. The rebuild retains only the
bounded dedup window; use the explicit audit command when a full historical
duplicate check is required:

```sh
docker compose run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --audit
```

Catch-up requests are split into `ALPACA_RECORDER_FETCH_WINDOW_MINUTES`
windows (15 minutes by default), so a long outage cannot materialize the whole
quote backlog in one process. A legacy single-file `market.csv` is streamed,
audited, and partitioned in place on the first run after upgrade, then kept
beside the corpus as `market.csv.migrated`.

Option sampling takes `ALPACA_RECORDER_OPTION_LIMIT` contracts per side per
sample (default 10, capped at 25) and keeps every contract it has sampled in
the sample for `ALPACA_RECORDER_OPTION_HOLD_MINUTES` (default 180) so a trade
opened on a contract still has quotes at its exit.

Bar coverage is judged against the cached Alpaca calendar, so holidays and
early closes remain quiet. An intraday gap larger than
`ALPACA_RECORDER_BAR_GAP_MINUTES` (default 5) is recorded per symbol under
`bar_coverage` in the index and exposed by recorder health as
`coverage_status`, `bar_gap_symbols`, and `bar_gap_observations`. It does not
stop recording by default: missing trade bars can be legitimate feed coverage,
and research applies adjacency checks only where a replay depends on them.
Set `ALPACA_RECORDER_STRICT_BAR_FEEDS` to a comma-separated feed list (or `*`)
to make those observations fail closed for explicitly governed feeds. The
recorder refuses to change equity feeds inside an existing corpus because event
identity and per-symbol continuity must not cross feed provenance.

The research profile is disabled by default. `deploy/research-cycle.sh`
discovers and routes the corpus automatically, concatenating partitions in
session order; `ALPACA_RESEARCH_SESSION_WINDOW` limits that to the most recent
N sessions, and `ALPACA_RESEARCH_DATASET` can override the source with
normalized JSONL. Start it with
`docker compose --profile research up -d research`. The edge ledger is stored
at `runtime/research/edge_lab.sqlite3` (override with `ALPACA_EDGE_DB`) and is
read-only from the dashboard. Research cannot place orders or mutate broker
state. Paper `selection_mode: all_proved` runs one best proven variant per
independent family under one global risk book. Defaults are seven logical
strategy slots over eleven bounded rule families and four isolated variant
accounts per strategy; each isolated book is processed by one bounded worker.
Capacity is configurable through the
`ALPACA_FACTORY_*` environment variables.

The shipped/default universe is eight liquid ETFs (`SPY`, `QQQ`, `IWM`, `DIA`,
`XLF`, `XLK`, `XLE`, `XLV`), improving opportunity capacity. Real signal rates
still require sufficient history, and floor feasibility fails closed when the
100-trade held-out floor cannot be supported; widen history and/or the
configured universe, never lower the floor.

Start the broker-free shadow lane with `docker compose --profile shadow up -d
shadow`. Compose first runs the short-lived `shadow-init` service to repair the
ownership of the persistent WAL directory, then starts ShadowRunner as UID/GID
10001. It mounts the recorder corpus and EdgeLedger read-only, has no broker
credentials, and writes only `/app/shadow/shadow.sqlite3` (SQLite WAL). It
evaluates eligible candidates in isolated virtual books from recorder events,
creates exact-session candidate/root-baseline/randomized-null replays, and
quarantines mismatch/incomplete rows. Virtual opens use the deterministic
runtime signal/setup/risk path; completed sessions compare semantic shadow
signatures with factory/IBR replay. The lane cannot submit orders or mutate
broker/runtime state.

Shadow consumes recorder partitions from durable byte offsets rather than
rescanning the historical corpus on every poll. High-frequency equity quotes
are compacted to the final quote per symbol/minute before entering the shadow
WAL; bars and option snapshots remain lossless. When upgrading a legacy WAL
that already reached the old 20,000-event ceiling, ShadowRunner preserves its
existing evidence, baselines the current recorder file ends, and resumes only
from subsequently committed rows. This is intentionally forward-only: a large
recorder recovery is research input, not a synthetic live-shadow window.

The scheduled research cycle invokes `edge ingest-shadow` by default when
`ALPACA_SHADOW_INGEST_ENABLED=1`; a missing shadow WAL is a no-op. The consumer
mounts the same `shadow-data` volume read-only at `/app/shadow` and is the only
process that writes the live-ingestion authorization marker to EdgeLedger.

The default Compose lane passes the Alpaca paper endpoint and feed settings
explicitly through `ALPACA_PAPER`, `ALPACA_DATA_FEED`, and
`ALPACA_OPTIONS_FEED`. Credentials are mounted from
`ALPACA_AGENT_SECRET_FILE`, never copied into the image. Named volumes survive
ordinary `docker compose down`; a second trader or `down -v` can
corrupt/delete operational state.

Before market-data or order calls, run `python main.py check`; this is the
authenticated preflight by default (`--offline` is local configuration only,
not a trading preflight). A guarded paper-endpoint round trip is available as
`python main.py paper-smoke --symbol SPY --confirm PAPER`; it requires the
paper runtime lock, an open market, and an initially flat account, then buys
and closes one share and proves the account is flat. Orders are day-only;
startup cleanup cancels working
orders and flattens residuals, and the session policy force-flats before the
regular NY close. The checked research config uses deterministic strategy
discovery. Bounded model-assisted replacement can be enabled with credentials
only from `ALPACA_RESEARCH_LLM_SECRETS_FILE`; enabling it without the matching
provider key fails the research cycle before discovery. LLM
discovery/replacement/tuning use full-schema structured
contracts, a per-run call budget and authentication circuit, and record
per-attempt evidence. Runtime decision LLM use remains disabled. The trader opens
entries only when the SQLite ledger has a vehicle-local `validated` or
`champion` edge for its configured execution profile whose latest shadow proof
carries the parity-matched live-ingestion marker.

Research never rewrites the append-only recorder corpus. A temporary cycle view
explicitly quarantines legacy rows whose `as_of` is later than `observed_at`,
emits `research-cycle-quarantine.v1` counts, and lets replay coverage/refusal
gates account for the missing evidence. All other normalization and integrity
errors still fail the cycle before discovery. The same source pass builds a
compact bar/option worker view; factory workers share the parent's finalized
read-only quote index, so each hypothesis no longer rescans and reindexes the
full quote corpus. The parent verifies that view against the full normalized
bar/option projection before scheduling workers, and workers verify its digest
when they read it. `research-progress.v1` records expose the current bounded
phase through scheduler health and the dashboard without changing gate or
promotion semantics.

Model-assisted research is disabled in the shipped paper configuration so an
empty Compose secret can never masquerade as an authenticated provider. To
enable it, set `research.strategy_llm.enabled=true` and set
`ALPACA_RESEARCH_LLM_SECRET_FILE` to the host path of the
separate research-provider dotenv file. It is mounted read-only as
`/run/secrets/research_llm_credentials`; broker credentials are not mounted
into the research service.

The shipped Compose and systemd lanes are paper-scoped. A live deployment is a
separate reviewed config/runtime scope: `mode: live`, `broker.paper: false`,
`broker.allow_live: true`, `ALPACA_LIVE_ENABLE=true`,
and either `strategy.selection_mode: pinned` with exactly one operator-named
entry (preferred), or legacy `selection_mode: specific` with one exact named
validated/champion `strategy.variant_id` carrying the parity-matched
live-ingestion marker. The runtime resolves only that edge,
re-verifies its proof/configuration at startup refresh, and does not
auto-switch. Runtime LLM decisions are rejected independently by configuration
validation and the Engine constructor. The authenticated live preflight also
requires `pattern_day_trader=true`.
Research lifecycle gates include fit/held-out structural floors, matched
controls, placebo/falsification, fixed-rule rolling-origin stability,
family-local/cycle-global FDR and cumulative online-FDR state, sealed
qualification source binding (one preselected candidate alone consumes the
window), and durable verification. Offline historical/forward replay may leave
a candidate at `shadow` only and never authorizes runtime. Research-side `edge
ingest-shadow` opens the shadow WAL read-only, requires strictly newer complete
parity-matched rows, prior qualification, source/config/code/provenance/replay/
gate hashes, family/global BH plus durable online FDR, then appends the
immutable `lane=shadow` proof and live marker. Underpowered, mismatched, or
incomplete shadow data advances no boundary and is reconsidered. Legacy
validated/champion rows without the marker can be evaluated/migrated but remain
ineligible until a new authorized live proof. Retirement requires adequate terminal negative
evidence for every intended variant; a valid bounded LLM replacement is
registered before retirement when enabled, and demoted candidates can re-prove
on a newer shadow run. Scheduler
terminal statuses are `completed`, `completed_no_edge`, `no_data`, and
`failed`.

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
