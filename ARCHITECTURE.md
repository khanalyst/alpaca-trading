# Architecture

This map is based on agent/, deploy/, research/, main.py, compose.yaml, and
config.yaml. It describes authority and data flow, not performance or
historical test results.

## Boundaries

| Component | Broker authority | Durable output |
| --- | --- | --- |
| deploy/recorder.py | Read-only market-data reads | Session corpus and sidecars |
| research.py / scheduler | None | EdgeLedger SQLite, proofs, diagnostics |
| deploy/shadow.py | No credentials/order path | Isolated shadow WAL and reports |
| main.py + agent.engine.Engine | Account reads and order mutation | Mode-scoped state, journal, heartbeat |
| deploy/trader_supervisor.py | One child and post-exit recovery | Supervisor health |
| deploy/watchdog.py | Cancel/flatten only; never entries | Recovery verdict/health |
| deploy/dashboard.py | Read-only projection | No authoritative writes |

Research and shadow cannot place broker orders. The main research/proof
pipeline can produce authorizing evidence in the ledger; the trader cannot
manufacture proof and startup policy resolves a compatible record. The
diagnostic shadow cohort and the session-edge study do not grant proof
authority by themselves. Paper/live isolation uses
mode, account fingerprint, runtime directory, lock, journal, credentials, and
endpoint.

## Topology

    Alpaca market data
           |
           v
    recorder --> runtime/research/recorded/sessions --> shadow (isolated WAL)
           |                         |
           +--> research cycle ------+
                        |
                        v
                 EdgeLedger + proofs
                        |
                        v
                 startup edge policy
                        |
                        v
    Alpaca account <--> Engine <--> runtime state/journal
                             ^
                             |
                          watchdog

Compose uses a read-only application filesystem, unprivileged services,
dropped capabilities, resource bounds, secrets/configs, named volumes, and a
loopback-only dashboard. shadow-init has no credentials/network and only
prepares shadow-volume ownership. The supplied systemd units cover recorder,
trader, watchdog, and a research timer invoking a bounded oneshot cycle. They
do not install shadow or dashboard services. A systemd deployment needs a
separately managed, single shadow writer to produce fresh shadow evidence;
the research cycle only consumes its WAL.

recorder writes the corpus. research reads corpus/shadow inputs and mounts the
LLM provider secret separately. shadow has no broker secret and writes only its
WAL. trader owns order mutation. watchdog shares runtime/credentials for
cancel/flatten recovery. dashboard mounts reporting inputs read-only.

## Data and research flow

The recorder validates point-in-time timestamps and provider/feed provenance,
normalizes bars/quotes/option snapshots, partitions by New York session date,
and writes append-only data plus bounded status/index sidecars. Catch-up and
forward-only capture remain distinct; historical backfill is diagnostic.

The recorder targets a 30-second poll interval. In `forward_only` capture,
the next poll can recover recently completed bars from the same session.
It records when it actually received each response. The recorder fetches
full quote ticks on their own schedule and de-duplicates repeated ticks by
exact event key in SQLite with `synchronous=FULL`.

The trader requests completed bars from the session open and quotes within
its configured maximum quote age. Paper trial events identify the frozen
variant and distinguish feature warmup, missing or stale inputs, and a valid
evaluation that produced no signal.

research.py provides source validation, IBR replay, edge commands, factory
discovery, shadow ingestion, calibration, and reports. The scheduled cycle
checks source boundaries, runs enabled diagnostics, consumes shadow WAL, and
persists gate/proof results. Runtime selection remains the trader's job.

The LLM adapter proposes bounded rule data only. It cannot write executable
code, bypass gates, or submit orders. Feed/account/configuration/code identity
changes require a new compatible evidence chain.

## Trader composition and cycle

agent.engine.Engine is the public facade and composes:

1. ExecutionLifecycleMixin for reconciliation, protection, monitoring, closes;
2. RuntimeControlMixin for run lock, loop, pause, shutdown, flatten, resume;
3. StartupEdgePolicyMixin for preflight, cleanup, session policy, and edge/trial
   resolution;
4. MarketEntryRiskMixin for market freshness, sizing, daily risk, and plans;
5. EngineCycleMixin for one complete decision cycle.

AlpacaProvider/AlpacaMarketData own broker operations and reads. alpaca_sdk,
alpaca_domain, and alpaca_session normalize SDK/domain values. RiskEngine and
risk_inputs separate risk decisions from submission.

The continuous run holds the mode lock; a direct bounded cycle takes a
temporary lock. Preflight authenticates the account/clock/calendar/feed.
Each cycle reconciles durable state with broker positions/orders; applies
session cutoffs; collects fresh data; resolves a proved edge or explicit
non-authorizing trial; builds setup/risk geometry; rejects duplicate exposure;
submits with a client ID; persists/reconciles fills; monitors exits; and
force-flats before close. Unknown broker status, malformed/stale data,
incompatible instruments, and post-submit durability uncertainty fail closed.

## State, identity, and pause

agent.state configures runtime/<mode>/ (or ALPACA_AGENT_RUNTIME_ROOT/<mode>/):

- state.json: lifecycle, account fingerprint, operator pause, trades,
  protection, orders, risk-day, preflight, and paper trial;
- journal.db: operational/audit tables and migrations;
- heartbeat.json: process status/freshness;
- state.lock/run lock: one mode owner; and
- events.jsonl: bounded event projections.

State uses locked read-modify-write and atomic replacement. An existing state
bound to another authenticated account is rejected, never silently rebound.

The supervisor starts a child only when operator_pause is false. When true it
parks with a paused heartbeat and no child/broker I/O. This is independent of
paper_trial.state=running, which is a non-authorizing diagnostic lifecycle.

RuntimeControlMixin.resume requires PAUSED/operator_pause=true, no kill/daily
stop, empty local trades/protection, terminal local orders, authenticated
preflight, empty broker positions, terminal broker orders, a final flat
confirmation, and the unchanged account fingerprint. Preflight and entry
resolution reject a frozen paper-trial identity/config mismatch; resume cannot
clear that blocker. Trial activation/replacement additionally requires both
local and authenticated account-scoped broker books to be flat. Resume clears
only the pause; it never flattens or submits. An orderly SIGTERM/SIGINT can
set the pause and restart does not clear it.

## Paper trial

PaperTrialRuntime freezes catalog arm, rule/spec, cohort, code, and policy
identities. Public status is always authorizing=false and proof_authority=false.
When enabled, the paper-trial path sets Engine._edge_required false and uses
the frozen non-authorizing trial controls instead of requiring a validated-edge
proof. Initial activation still requires a reconciled local and account-scoped
broker-flat book and stores its fingerprint.

A changed active identity/config is rejected. Replacement is considered only
after the old trial is terminal and local plus broker books are flat with
terminal orders. Editing state flags or rebinding a frozen identity is not
supported. Shipped config sets research.paper_trial.enabled=false; see
[docs/paper-shadow-trials.md](docs/paper-shadow-trials.md).

## Monitoring and recovery

When a live child is not entry-ready, EngineCycle checks the clock and invokes
not-ready force-flat at cutoff (except killed state). ExecutionLifecycle
handles stop/target/max-hold/force-flat deadlines and close intents. Ambiguous
close/state durability pauses and reports residual risk.

trader_supervisor watches advancing child heartbeats with a monotonic deadline,
signals only its process group, waits for exit, and invokes watchdog recovery;
it never relaunches entries after recovery.

watchdog acquires the same lock, verifies account identity, and can
cancel/flatten stale exposure. It cannot act while a live owner holds the lock
or while the broker/network is unreachable. Health distinguishes watching,
acted, degraded, and failed. The current adapter uses software-polled stops
for long options rather than the equity bracket/OCO path.

## Configuration and controls

agent.config.load_config validates mode guards, feeds, sessions, universe,
risk, execution, costs, research, paper-trial, and LLM sections. Shipped
defaults include paper mode, paper broker/disabled live, IEX equities,
indicative options, specific/auto shares selection, a 15:00 New York strategy
entry cutoff plus the five-minute pre-close session safety gate,
ten-minute force-flat, strict freshness, runtime llm.enabled=false, and a
separate enabled research proposal adapter.

Compose mounts config read-only, uses separate Alpaca/provider secrets, and
limits resources. Systemd uses ProtectSystem=strict, NoNewPrivileges, private
temporary files, and explicit writable paths.

main.py controls check, status, inspect-deployment, run, flatten, resume, and
paper-smoke (which requires --confirm PAPER). research.py --help is
authoritative for research arguments.

For procedures see [OPERATIONS.md](OPERATIONS.md), for install see
[SETUP.md](SETUP.md), and for retained references see [docs/README.md](docs/README.md).
