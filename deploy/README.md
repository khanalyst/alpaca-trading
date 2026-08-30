# Deployment topology

Installation is in [`../SETUP.md`](../SETUP.md); operation, backup, and
recovery are in [`../OPERATIONS.md`](../OPERATIONS.md). For an existing VM
migrating from a legacy SIP corpus, follow the short [VM feed migration
handoff](../VM_MIGRATION.md) before starting services. This file records
process ownership and the two supported paper launch lanes. The runtime scope
is US-listed equities/ETFs and listed OCC options only; crypto is rejected.
Every shipped deployment is paper-only (`ALPACA_PAPER=true`, live disabled),
uses the free Basic IEX feed for its equity-only universe, and keeps the
trader's runtime execution profile at `shares`. Options acquisition/research
is disabled by default; `indicative` is a non-executable placeholder. An
explicit option lane requires OPRA and separate review. Authorizing equity
evidence must use the exact IEX or SIP feed; `delayed_sip` is diagnostic only.

## Docker Compose (recommended)

[`../compose.yaml`](../compose.yaml) runs one least-privilege image:

| Service | Responsibility | Durable state |
| --- | --- | --- |
| `recorder` | Alpaca bars, quotes, and session observations (paper by default) | `runtime-data` |
| `trader` | Exactly one paper intraday loop in the shipped `shares` execution profile and broker reconciliation | `runtime-data`, `research-cache` |
| `research` | Scheduled twelve-slot offline factory/replay and shadow-WAL ingestion; no broker authority | `runtime-data`, research volumes |
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

The recorder runs on a fixed 30-second cadence and writes its mixed
bars/quotes/options corpus partitioned by New
York session date under `runtime/research/recorded/sessions/market-<date>.csv`,
with a compact `.recorder-index.json` sidecar holding the watermark, per-symbol
last bar, durable per-symbol quote and completed-bar watermarks, bounded
coverage evidence, corpus equity feed, and option contracts
held open for continued sampling. Exact keys for the fifteen-minute dedup
window live in `.recorder-recent-keys.sqlite3`, so high-rate quote bursts do not
expand a large JSON object in recorder memory. Both caches are bound to the
partition size/mtime fingerprints and watermark and are rebuilt by streaming
the partitions when they disagree. Recorder and backfill mutations share one
corpus lock. The rebuild retains only the bounded dedup window; use the
explicit audit command when a full historical duplicate check is required:

```sh
docker compose run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --probe

docker compose run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --audit
```

The probe makes real recent IEX bar/quote requests (and OPRA requests only when
the configured universe explicitly records options) without appending corpus
rows. A configured feed name is not an entitlement check. Deployment now stops
before service replacement when the probe reports the generic configured-feed
entitlement failure (`iex_entitlement_required` or
`opra_entitlement_required`). The recorder also persists its latest attempt in
`.recorder-status.json`, so health and the dashboard distinguish a permanent
subscription failure from an empty or temporarily stale corpus. IEX is a
limited venue view rather than consolidated SIP; sparse coverage is expected
and is never repaired by relabeling another feed. New exact-feed evidence needs
a fresh research/shadow proof epoch. Readiness requires both quote and completed
bar watermarks to be no older than 30 seconds for every required symbol; an
alive recorder or scheduler is not evidence of readiness or research quality.

Catch-up requests are split into `ALPACA_RECORDER_FETCH_WINDOW_MINUTES`
windows (1 minute by default), so a long outage cannot materialize the whole
quote backlog in one process. The small default bounds peak memory, at the cost
of more API requests and a slower stale-corpus catch-up. A legacy single-file
`market.csv` is streamed,
audited, and partitioned in place on the first run after upgrade, then kept
beside the corpus as `market.csv.migrated`.

Rows first seen more than
`ALPACA_RECORDER_FORWARD_OBSERVATION_MAX_LAG_MINUTES` after their point-in-time
availability (15 minutes by default) are durably marked
`historical_backfill` at the partition boundary before append. They remain
usable by the explicit diagnostic replay policy, but cannot count as
forward-shadow or paper authorization evidence.

Existing corpora can be classified with the same rule using an explicit,
streaming maintenance pass:

```sh
docker compose run --rm --no-deps recorder \
  python deploy/recorder.py --out runtime/research/recorded --repair-provenance
```

The operation takes the corpus lock, writes a partition marker before changing
the index, is idempotent, and never turns a historical partition back into a
forward-observed one. Review its machine-readable report before using the
result in a diagnostic cycle.

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
state. Paper `selection_mode: all_proved` runs one strongest proven variant per
verified frozen prior-cycle dependence cluster under one global risk book;
families without a verified assignment use the held-out correlation-safe
fallback. Defaults are twelve logical
strategy slots over all twelve bounded rule families and four isolated variant
accounts per strategy; each isolated book is processed by one bounded worker.
The twelfth family, `cross_sectional_residual`, is shares-only, uses SPY as its
benchmark, and requires synchronized one-minute context. It does not replace
the 24-ETF universe; future family or universe changes require evidence from
the screen and cross-sectional report.
Scheduled research evaluates the equity vehicle only by default because runtime
execution remains the single `shares` profile. Set
`ALPACA_RESEARCH_VEHICLES=all` explicitly to evaluate both equity and option
vehicles independently; their calibration and authorization evidence stays
per vehicle. Capacity is configurable through the
`ALPACA_FACTORY_*` environment variables.
Scheduler liveness, recorder service liveness, and research evidence/readiness
are separate health dimensions. A live scheduler can report a pending or
blocked corpus, and a market holiday is an explicit closed state rather than a
missing-data failure.

Deterministic preprocessing can be reused across repeated experiments by
setting `ALPACA_RESEARCH_IMMUTABLE_SOURCE_IDENTITY` to an audited digest that
covers every selected partition byte plus the recorder calendar and partition-
source sidecars. Path, size, and modification time are intentionally
insufficient. `ALPACA_RESEARCH_PREPROCESSING_CACHE_ROOT` selects the immutable
content-addressed store. Recorder session partitions are streamed directly in
filename order; the cycle does not build a merged CSV. Quarantine, vehicle
selection, exact-calendar/provenance checks, option availability correction,
and final view routing happen in that same pass. The canonical normalized file
also supplies quote rows to backtest, so no corpus-sized quote-only copy is
stored. Every cache hit re-hashes each normalized, bar, option, replay, and
report artifact; corruption becomes a quarantined miss and is rebuilt
atomically. On a miss, disposable outputs are moved into cache staging on the
shared research-cache rename domain instead of being copied. Leaving the source
identity unset disables the cache, which is the safe mode for the actively
changing recorder corpus. If either cache path is overridden, keep `TMPDIR`
and `ALPACA_RESEARCH_PREPROCESSING_CACHE_ROOT` on the same rename-capable
mount (the rename domain); the low-space publisher fails closed rather than
falling back to a full copy. The systemd unit sets `TMPDIR` to
`/opt/alpaca-agent-trading/research/cache/tmp`, and the systemd example sets
the same `TMPDIR` plus the cache root
`/opt/alpaca-agent-trading/research/cache/preprocessing`, so both paths remain
under the durable research-cache mount. When immutable cache reuse is enabled,
the cycle performs a real `os.replace` probe from its
temporary working directory into cache staging before preprocessing. A
cross-domain `EXDEV` result is reported as a topology-preflight failure and
stops the cycle before it can spend time preprocessing; probe files are cleaned
up on both success and failure. Compose keeps its `/app/research/cache` paths
from `compose.yaml`.

Historical catch-up may run Terra without contaminating authorization by
setting `ALPACA_FACTORY_DIAGNOSTIC_ONLY=1`. The cycle then writes a separate
`ALPACA_FACTORY_DIAGNOSTIC_REPORT`, runs model discovery and bounded tuning on
the diagnostic view, and skips EdgeLedger proof emission, trial review, and
shadow ingestion. It exits non-authorizing even when a hypothesis looks good.
Use `python -m research.cost_counterfactual` against the resulting frozen
specifications to compare `0.30` with a preregistered alternative such as
`0.60`; the report is reachability evidence only and cannot alter the shipped
threshold or promote an edge. It compares cost-veto refusals, admitted trades,
and ordinary diagnostic replay P&L; it does not relabel that replay P&L as a
separate stressed-expectancy estimator.

The current v2 contract verifies the exact changed config path
`risk.max_stressed_cost_to_risk_ratio` and records baseline/alternative/runtime
config hashes. It pairs opportunities exactly by `variant_id + opportunity_id`,
excluding malformed terminal/numeric/identity rows and duplicate keys from
valid pairing. Identity/duplicate exclusions also apply to empirical
summaries; incomplete, ambiguous, or path-dependent pairs fail the controlled
change invariant and are not causally interpretable.
Each arm records empirical R (`r_multiple`, including mean and sample sigma),
trades per session, target hits, costs, stressed-cost-to-risk values, entry
slippage, and fill sources. The diagnostic Section 0.5 report provides a 95%
moving-block session-cluster interval and clustered MDE/power at alpha .05 for
`.05R`; it derives uncertainty from the data and assumes no fixed `0.38R`
width.

Source-report, measurement-code, dataset, frozen-cohort, run-settings, and
final-result content digests are retained. A bound source report must include
its dataset hash, strict diagnostic/non-authorizing flags, and an explicit
empty proof list or the controlled-change invariant fails. The funnel lists fit, heldout,
qualification, `shadow_selection`, and `shadow_confirmation`, with nominal
floors of 100/100/100/150/150 trades (30 sessions/clusters each; 600 total);
every window is `measurement_available: false` without sealed/live
assignments. Readiness context is 150 offline + 60 shadow = 210 sessions. All
output remains diagnostic-only with descriptive P&L and no threshold-selection
or promotion authority.

The shipped/default universe is 24 liquid ETFs spanning broad-market, size,
sector, international, rates/credit, metals, and semiconductor exposures (the
exact list is in `config.yaml`), improving opportunity capacity. Authorizing floors
are immutable: backtest/factory requires 100 trades and 30 complete
sessions/clusters; sealed qualification requires 100 trades and 30 complete
sessions/clusters; parity-matched live shadow requires 150 trades and 30
complete sessions. Real signal rates still require sufficient history, and
floor feasibility fails closed; widen history and/or the configured universe,
never lower a floor. An expansion requires an operator-approved exact symbol
list, recorder coverage for that list, and a new candidate identity/proof; it is
not a parameter-only tuning arm. Effective breadth is a persisted/re-verified matched
symbol/session diagnostic and never counts as extra independent N. Before each
factory cycle, completed prior-cycle family deltas are frozen into a
hash-verified map; strong clusters receive a cluster-level BH veto and runtime
allocation admits one strongest edge per verified frozen cluster. Serial
inference uses a deterministic seeded moving-block day/session-cluster
bootstrap.

Event conditioning requires a point-in-time event source with provider,
`as_of`, and observation provenance. Prior-session, true multi-timeframe, and
cross-sectional features require explicit replay context and fail closed when
that context is missing or ambiguous.

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
Concurrent shadow workers bind each poll to a content-addressed manifest;
readers re-verify the manifest digest before consuming it. A manifest mismatch
or unreadable artifact is quarantined and cannot advance the shadow boundary.

Replay-diff metadata is retained for 180 days by default so a complete
confirmatory tail remains inspectable; set `ALPACA_SHADOW_RETENTION_DAYS` only
when a shorter operational window is deliberately accepted. Pruning reports
its floor and count in the shadow heartbeat; a non-authorizing watermark turns
any candidate boundary predating deleted replay metadata into a blocked
`retention_gap`. Source events, decisions, accounts, and trades remain
immutable evidence.

The scheduled research cycle invokes `edge ingest-shadow` by default when
`ALPACA_SHADOW_INGEST_ENABLED=1`; a missing shadow WAL is a no-op. The consumer
mounts the same `shadow-data` volume read-only at `/app/shadow` and is the only
process that writes the live-ingestion authorization marker to EdgeLedger.

The default Compose lane passes the Alpaca paper endpoint and IEX/indicative
feed settings explicitly through `ALPACA_PAPER`, `ALPACA_DATA_FEED`, and
`ALPACA_OPTIONS_FEED`; a partial or mixed-feed override is not a supported
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
Before resolving lanes or touching the dataset, the cycle runs one bounded,
non-authorizing `python3 research.py llm-preflight --agent-config config.yaml`
probe. Its bounded, redacted result is retained in the terminal cycle JSON,
status heartbeat, and operational history, including transient `degraded`
outages that permit deterministic fallback. See
[research/README.md](../research/README.md#provider-preflight) for the
model/deployment contract and fatal/degraded classification; operators should
rerun it after endpoint/model changes and before an expensive cycle.
The trader opens
entries only when the SQLite ledger has a vehicle-local `validated` or
`champion` edge for its configured execution profile whose latest shadow proof
carries the parity-matched live-ingestion marker.

Authorizing fill quality retains provider/feed/source and quote age for both
legs: required records become actionable at the maximum of event timestamp,
`as_of`, and `observed_at`. A delayed recorder bar may signal when observed;
execution enters at that decision/observation time using fresh exact IEX or SIP
(equity) or OPRA (option) evidence. Delayed full OHLC never backfills an earlier
entry, and partial pre-entry bar ranges are excluded. Equity entry and exit
require exact IEX or SIP, while OPRA is required for an option lane, each no
older than 30 seconds. `delayed_sip` is diagnostic only. Feed provenance is
request-bound: an explicit provider-row feed
label is retained when present, otherwise the configured/requested feed label is
used; it is not an independent venue attestation.
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
The pure entry-slippage cap is shared by runtime, factory, explicit IBR replay,
and randomized-null quote entries; malformed inputs use the stable reason
`entry_slippage_invalid`, while over-cap quotes use
`entry_slippage_exceeds_limit` as a no-trade/refusal.

Compose ships `ALPACA_RESEARCH_CALIBRATION_BOOTSTRAP_UNKNOWN=1` so a fresh
installation with no paper journal can collect broker-free shadow evidence.
This is a narrow empty-journal bootstrap only: the persisted
`calibration_state=bootstrap_unknown` and `authorization_exit_code=2` remain
non-authorizing and cannot claim measured calibration or promote a candidate.
Set the variable to `0` in the deployment environment when measured
calibration must exist before shadow ingestion. Thin, existing, mixed-vehicle,
stale, or optimistic history remains blocked until normal measured
calibration is authorized.

The scheduled calibration-only pass measures per-symbol/session stress on the
9/15/25/50-bps ladder. It is disabled by default and can be activated only by
an operator-controlled path. The artifact must bind the exact provider/feed,
content hash, sufficient disjoint chronological held-out sessions, and one
artifact-wide effective-after boundary; unusable cells use the configured
scalar fallback. Calibration remains diagnostic unless that explicit path is
enabled and never self-authorizes.

ShadowRunner records a durable `replay_quarantine` entry for every incomplete
or mismatched candidate/session replay. Its health result reports
`stale_tail.status=blocked`, the session, and the exact source/shadow/replay
digests. Correct the recorder source, then run one bounded replay cycle (for
example `docker compose run --rm --no-deps shadow --once`); only when all
required arms produce a complete parity match is the entry changed to
`status=repaired`. The read-only `edge ingest-shadow` consumer refuses to
advance its boundary or spend FDR while a quarantine entry or an
authoritative-calendar gap remains, and its retry records the repaired
session explicitly. Semantic or mid-tail incompleteness therefore fails closed
without permanently losing the session; correction plus the bounded replay is
the repair path. There is no unsafe auto-skip of quarantine, and no watermark/FDR
advancement occurs until the repair is complete. A missing or stale recorder calendar is reported as
`catalog_unavailable`/unknown rather than inferred from weekdays.

The executable exit grammar remains fixed to the 30-bps-floor ATR bracket,
configured R target, and bar-cap time exit. Fit-only factory diagnostics expose
signal-prefix/floor binding, planned exits, gross/net/fees/slippage cost/risk,
planned versus fill-delivered risk, per-leg provider/feed provenance, power,
behavior aliases, pricing source, configured limits,
pass/fail/unknown row counts, and aggregate fit-partition execution-rejection
counts/reasons for operator review; they are non-authorizing and do not expand
exits. A fit whose opportunities are all explicitly execution-rejected is
`execution_blocked`, distinct from sparse/underpowered data; bounded budget
closure only progresses search exhaustion and is not a powered negative edge.
The pre-replay signal screen records forward-return/control rows and uses
explicit `p=1` placeholders when a comparison is unavailable. Its terminal
current-hypothesis no-edge result reseeds on a changed corpus. Target/hold path
telemetry is reachability-only; bounded lower-target/hold proposals cannot
authorize a candidate.

Research never rewrites the append-only recorder corpus. A temporary cycle view
explicitly quarantines legacy rows whose `as_of` is later than `observed_at`
and excludes rows proven outside an authoritative Alpaca session, emitting
`research-cycle-quarantine.v1` and `research-cycle-calendar-filter.v1` counts.
Missing, malformed, or conflicting exact-calendar metadata still fails the
cycle before discovery. The same source pass builds a
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
`openai`/`gpt-5.6-terra` provider (or the matching `ANTHROPIC_API_KEY` when configured).
When `OPENAI_BASE_URL` is Azure, set the exact resource-local alias in
`research.strategy_llm.deployment`; the catalog model name is retained only as
evidence and is not guessed as the deployment.
Broker credentials are not mounted into the research service. The OpenAI path
uses the Responses API structured-output request; prompt, request, schema, and
configuration (including the effective sampling setting), plus response hashes,
reproduce the evidence for the exact invocation but do not guarantee bit-for-bit
identical later model output. OpenAI requests send `temperature: 0` when
supported; when the configured model or deployment is exactly
`gpt-5.6-terra`, the adapter omits that unsupported parameter and records
`temperature: null` in configuration evidence. Anthropic requests keep
`temperature` at `0`; the hashes preserve the actual request and response.

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
family-local/frozen-dependence-cluster/cycle-global FDR, sealed qualification
source binding (one
preselected candidate alone consumes the window), and durable verification.
Offline historical/forward replay defers cumulative online FDR, may leave a
candidate at `shadow` only, and never authorizes runtime. Research-side `edge
ingest-shadow` opens the shadow WAL read-only, requires strictly newer complete
parity-matched rows, prior qualification, source/config/code/provenance/replay/
gate hashes, family/global BH plus the frozen-dependence-cluster veto and
durable online FDR, then appends the
immutable `lane=shadow` proof and live marker. Underpowered, mismatched, or
incomplete shadow data advances no boundary and is reconsidered. The
`shadow-confirmation-v5` ingestion scope splits each tail into older
chronological selection sessions and a newer disjoint confirmatory window; BH
uses selection raw p-values, while only the selected candidate's raw
confirmatory p-value reaches LORD++. With `W0=alpha`, its first-discovery reward
is zero and later discoveries receive the standard `alpha` stream. Legacy
v2/v3/v4 rows remain auditable but quarantined; there is no unsafe auto-skip, so an
unresolved quarantine blocks watermark/FDR advancement until a bounded parity
replay repairs it. Simulation resolution scales to the next
online allocation and stops without spending at its bounded cap. Legacy
validated/champion rows without the marker can be evaluated/migrated but remain
ineligible until a new authorized live proof. Retirement requires a powered
upper-bound rejection across multiple negative windows for every bounded
variant; a valid bounded LLM replacement is
registered before retirement when enabled, and demoted candidates can re-prove
on a newer shadow run. Scheduler
terminal statuses are `completed`, `completed_no_edge`, `no_data`,
`search_exhausted`, `llm_provider_failure`, and `failed`. The factory wrapper
passes `ALPACA_FACTORY_MAX_CONFIRMATORY_ATTEMPTS` (default `3`) to each run.
Factory exits 5 (`bounded_space_exhausted`) and 6 (`llm_all_calls_failed`) are
normalized into the two explicit cycle statuses and the wrapper exits 0 so
the structured reason is retained in scheduler history; they are not proof
and do not bypass the runtime edge gate.
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
`alpaca`. The research unit defaults to the equity lane, IEX, and the
non-executable indicative options placeholder. Set
`ALPACA_RESEARCH_VEHICLES=all` explicitly when both research vehicles are
needed after configuring OPRA; their calibration remains independent. The unit
fails closed if the provider file is not readable. Equity-only cycles keep the
mixed recorder corpus append-only but use a temporary view that excludes
option rows (and records the excluded count); selecting `option` or `all`
retains those rows and applies strict OPRA validation. Enable the full paper lane
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
