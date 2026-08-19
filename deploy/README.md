# Deployment topology

Installation is in [`../SETUP.md`](../SETUP.md); operation, backup, and
recovery are in [`../OPERATIONS.md`](../OPERATIONS.md). This file records
process ownership and the two supported paper launch lanes. The runtime scope
is US-listed equities/ETFs and listed OCC options only; crypto is rejected.
Every shipped deployment is paper-only (`ALPACA_PAPER=true`, live disabled),
uses SIP for equities and OPRA for options, and keeps the trader's runtime
execution profile at `shares`. Options research is enabled as an evidence lane;
it does not enable options order execution.

## Docker Compose (recommended)

[`../compose.yaml`](../compose.yaml) runs one least-privilege image:

| Service | Responsibility | Durable state |
| --- | --- | --- |
| `recorder` | Alpaca bars, quotes, and session observations (paper by default) | `runtime-data` |
| `trader` | Exactly one paper intraday loop in the shipped `shares` execution profile and broker reconciliation | `runtime-data`, `research-cache` |
| `research` | Scheduled eleven-slot offline factory/replay and shadow-WAL ingestion; no broker authority | `runtime-data`, research volumes |
| `watchdog` | Independent stale-trader flatten; cancel and close only, never entries | `runtime-data` |
| `dashboard` | Read-only localhost health and reports | Read-only mounts |
| `shadow` | Broker-free incremental shadow evaluation and semantic replay parity; no broker authority | Read-only recorder/EdgeLedger mounts, isolated shadow WAL |

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

Bar coverage and replay cutoffs use the exact Alpaca calendar, so holidays and
early closes remain quiet; missing calendar metadata is a refusal, never a
fixed 16:00 promotion fallback. An intraday gap larger than
`ALPACA_RECORDER_BAR_GAP_MINUTES` (default 5) is recorded per symbol under
`bar_coverage` in the index and exposed by recorder health as
`coverage_status`, `bar_gap_symbols`, and `bar_gap_observations`. It does not
stop recording by default: missing trade bars can be legitimate feed coverage,
and research applies adjacency checks only where a replay depends on them.
Set `ALPACA_RECORDER_STRICT_BAR_FEEDS` to a comma-separated feed list (or `*`)
to make those observations fail closed for explicitly governed feeds. The
recorder refuses to change equity feeds inside an existing corpus because event
identity and per-symbol continuity must not cross feed provenance.

On-demand historical backfill uses that exact calendar, including early-close
open/close bounds, labels written partitions `source_mode: historical_backfill`,
and retains the truthful fetch-time `observed_at`. It is diagnostic historical
evidence only: an explicit diagnostic replay policy may inspect provider-
`as_of` visibility, but `diagnostic_historical_backfill` rows are excluded from
authorizing statistics and cannot authorize a proof or live deployment.

The plain supported startup includes research: `docker compose up -d` starts
the scheduled research service in the default startup. `deploy/research-cycle.sh`
discovers and routes the corpus automatically, concatenating partitions in
session order; `ALPACA_RESEARCH_SESSION_WINDOW` limits that to the most recent
N sessions, and `ALPACA_RESEARCH_DATASET` can override the source with
normalized JSONL. The edge ledger is stored at
`runtime/research/edge_lab.sqlite3` (override with `ALPACA_EDGE_DB`) and is
read-only from the dashboard. Research cannot place orders or mutate broker
state. Paper `selection_mode: all_proved` runs one best proven variant per
independent family under one global risk book. Defaults are eleven logical
strategy slots over all eleven bounded rule families and four isolated variant
accounts per strategy; each isolated book is processed by one bounded worker.
Scheduled research evaluates the equity vehicle only by default because runtime
execution remains the single `shares` profile. Set
`ALPACA_RESEARCH_VEHICLES=all` explicitly to evaluate both equity and option
vehicles independently; their calibration and authorization evidence stays
per vehicle. Capacity is configurable through the
`ALPACA_FACTORY_*` environment variables.

The shipped/default universe is eight liquid ETFs (`SPY`, `QQQ`, `IWM`, `DIA`,
`XLF`, `XLK`, `XLE`, `XLV`), improving opportunity capacity. Authorizing floors
are immutable: backtest/factory requires 100 trades and 30 complete
sessions/clusters; sealed qualification requires 100 trades and 30 complete
sessions/clusters; parity-matched live shadow requires 150 trades and 30
complete sessions. Real signal rates still require sufficient history, and
floor feasibility fails closed; widen history and/or the configured universe,
never lower a floor. Effective breadth is a persisted/re-verified matched
symbol/session diagnostic and never counts as extra independent N. Serial
inference uses a deterministic seeded moving-block day/session-cluster
bootstrap.

Research qualification requires at least 100 trades, 30 complete sessions, and
30 session-level clusters. Epoch 5 retains epoch-4 point-in-time,
executable-row, vehicle-cost, raw-confirmatory-p, and stressed-cost boundaries,
and additionally seals paired synthetic root-control shadow decisions/replays,
diagnostic historical-backfill provenance with exact calendar metadata, durable
live-shadow FDR binding, chronological paired inference, finite BH input
validation, and conservative broker-tick equity rounding. Epoch-4 proofs remain
readable for audit but cannot validate, champion, or authorize the paper trader;
they must be re-derived under epoch 5. Authorization requires exact equality
with current epoch 5; future generations are audit-only too. A current-epoch run
seals one immutable verified gate proof, and re-derivation appends a new proof
instead of rewriting history.

The plain supported startup also includes the broker-free shadow lane:
`docker compose up -d` runs the short-lived `shadow-init` service to repair the
ownership of the persistent WAL directory, then starts ShadowRunner as UID/GID
10001. It mounts the recorder corpus and EdgeLedger read-only, has no broker
credentials, and writes only `/app/shadow/shadow.sqlite3` (SQLite WAL). It
evaluates eligible candidates in isolated virtual books from recorder events,
creates exact-session candidate, paired synthetic root-control, and
randomized-null replays, and
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

The default Compose lane passes the Alpaca paper endpoint and SIP/OPRA feed
settings explicitly through `ALPACA_PAPER`, `ALPACA_DATA_FEED`, and
`ALPACA_OPTIONS_FEED`; a partial-feed override is not a supported
autonomous-research deployment. Credentials are mounted from
`ALPACA_AGENT_SECRET_FILE`, never copied into the image. The research service
also requires `ALPACA_RESEARCH_LLM_SECRET_FILE` to name a separate readable
dotenv file containing `OPENAI_API_KEY` (or the configured provider's key).
There is no deterministic downgrade when that file is absent or unreadable:
the Compose interpolation or cycle preflight fails closed. Named volumes
survive ordinary `docker compose down`; a second trader or `down -v` can
corrupt/delete operational state.

Before market-data or order calls, run `python main.py check`; this is the
authenticated preflight by default (`--offline` is local configuration only,
not a trading preflight). A guarded paper-endpoint round trip is available as
`python main.py paper-smoke --symbol SPY --confirm PAPER`; it requires the
paper runtime lock, an open market, and an initially flat account, then buys
and closes one share and proves the account is flat. Orders are day-only;
startup cleanup cancels working
orders and flattens residuals, and the session policy force-flats before the
exact broker calendar close. The checked research config enables bounded model-assisted
discovery, replacement, and tuning. Credentials come only from
`ALPACA_RESEARCH_LLM_SECRET_FILE`; an absent, unreadable, or keyless file fails
the research cycle before discovery. LLM requests use full-schema structured
contracts, a per-run call budget and authentication circuit, and record
per-attempt evidence. The runtime decision LLM is hard-off in the paper trader.
The trader opens
entries only when the SQLite ledger has a vehicle-local `validated` or
`champion` edge for its configured execution profile whose latest shadow proof
carries the parity-matched live-ingestion marker.

Authorizing fill quality retains provider/feed/source and quote age for both
legs: required records become actionable at the maximum of event timestamp,
`as_of`, and `observed_at`. A delayed recorder bar may signal when observed;
execution enters at that decision/observation time using fresh SIP (equity) or
OPRA (option) evidence. Delayed full OHLC never backfills an earlier entry, and
partial pre-entry bar ranges are excluded. SIP is required for equity entry and
exit, OPRA for option entry and exit, each no older than 30 seconds.
The shipped `execution.strict_market_data` default is `true`; historical bar
fallback is an explicit diagnostic lane only.
Bar-only, partial-feed, missing, or stale legs remain diagnostic and cannot
authorize a proof. Optional `costs.vehicles.equity`/`.option` schedules are
selected independently with provenance; absent overrides, the shipped model is
4 bps spread, 6 bps slippage, 0.5 bps per-side fee, plus a 0.65 option fee per
contract side. Preregistered stress scenarios are 9/15/25/50 bps; 25 bps is
the authorization requirement. Runtime stressed-cost risk abstains when its
cost-to-risk limit is exceeded and persists scenario/cost/ratio plus
intended/delivered risk-delivery telemetry. Missing, stale, or insufficient
calibration, an optimistic cost result, terminal material underfill (<80%), or
partial-cancel
rate above 20% blocks shadow authorization. Offline discovery/factory
diagnostics remain available while that boundary is closed. Fit diagnostics may
count planned signal/exit geometry as quote-required, non-authorizing
measurement. Stress applies scenario bps to entry notional and adds listed-option
round-trip fees for both per-contract sides; it is not a per-side bps charge.
The shipped `max_stressed_cost_to_risk_ratio` is `0.30`, so a 30-bps-floor trade
is about `0.833` cost-to-risk at the 25-bps stress and is vetoed before option
fees.

The executable exit grammar remains fixed to the 30-bps-floor ATR bracket,
configured R target, and bar-cap time exit. Fit-only factory diagnostics expose
signal-prefix/floor binding, planned exits, cost/risk, power, behavior aliases,
provider/feed provenance, pricing source, configured limits, and
pass/fail/unknown row counts for operator review; they are non-authorizing and
do not expand exits.

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

Model-assisted research is enabled in the shipped paper configuration. Set
`ALPACA_RESEARCH_LLM_SECRET_FILE` to the host path of the separate
research-provider dotenv file before startup; it is mounted read-only as
`/run/secrets/research_llm_credentials`. The file must be readable by the
container's restricted UID and contain `OPENAI_API_KEY` for the checked
`openai`/`gpt-5` provider (or the matching `ANTHROPIC_API_KEY` when configured).
Broker credentials are not mounted into the research service.

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
family-local/cycle-global FDR, sealed qualification source binding (one
preselected candidate alone consumes the window), and durable verification.
Offline historical/forward replay defers cumulative online FDR, may leave a
candidate at `shadow` only, and never authorizes runtime. Research-side `edge
ingest-shadow` opens the shadow WAL read-only, requires strictly newer complete
parity-matched rows, prior qualification, source/config/code/provenance/replay/
gate hashes, family/global BH plus durable online FDR, then appends the
immutable `lane=shadow` proof and live marker. Underpowered, mismatched, or
incomplete shadow data advances no boundary and is reconsidered. The unchanged
`shadow-confirmation-v4` ingestion scope splits each tail into older
chronological selection sessions and a newer disjoint confirmatory window; BH
uses selection raw p-values, while only the selected candidate's raw
confirmatory p-value reaches LORD. Same-tail v3
rows remain auditable but quarantined. Simulation resolution scales to the next
online allocation and stops without spending at its bounded cap. Legacy
validated/champion rows without the marker can be evaluated/migrated but remain
ineligible until a new authorized live proof. Retirement requires a powered
upper-bound rejection across multiple negative windows for every bounded
variant; a valid bounded LLM replacement is
registered before retirement when enabled, and demoted candidates can re-prove
on a newer shadow run. Scheduler
terminal statuses are `completed`, `completed_no_edge`, `no_data`, and
`failed`.
Gate envelopes retain per-arm candidate, baseline, and randomized-null counts,
fill sources, quote ages, gross/cost/net economics, matched and dropped keys,
and directional/pair coverage. Quote density can change null/control evidence
even when the candidate count is unchanged.

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
need it. Copy `agent.env.example`, `research.env.example`, and
`research-llm.env.example` to `/etc/alpaca-agent-trading/`, fill the Alpaca
paper key/secret and the separate provider key, then set mode `0400` and owner
`alpaca`. The research unit defaults to the equity lane, SIP, OPRA, and the
provider path above. Set `ALPACA_RESEARCH_VEHICLES=all` explicitly when both
research vehicles are needed; their calibration remains independent. The unit
fails closed if the provider file is not readable. Enable the full paper lane
with:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now alpaca-recorder.service alpaca-trader.service \
  alpaca-watchdog.service alpaca-research.timer
```

Disable the lane before migrating to Compose.

## Backup override

`compose.external-backup.yaml` is optional and only valid when
`ALPACA_EXTERNAL_BACKUP_PATH` is a verified different-device or off-host mount.
A normal directory on the VM does not prove recovery from VM loss. See the
backup verification steps in [`../OPERATIONS.md`](../OPERATIONS.md).
