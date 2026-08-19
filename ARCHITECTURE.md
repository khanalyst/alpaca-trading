# Platform architecture and decomposition record

This document describes the current Alpaca intraday research and trading
platform as implemented. It is an engineering map, not a performance claim.
The supported trading scope is US-listed equities/ETFs and listed OCC options.
Options are single-leg long calls or puts only. Crypto, overnight positions,
multi-leg options, naked options, and short-option exposure are rejected.

## System boundaries

The repository contains six core processes plus an optional broker-free shadow
process:

| Process | Authority | Durable output |
| --- | --- | --- |
| Recorder | Read-only Alpaca market-data collection | Mixed bars, quotes, and option snapshots, one partition per session date under `runtime/research/recorded/sessions` |
| Backfill | Read-only historical bar acquisition, run on demand | The same partitions and sidecar index the recorder writes |
| Research | Offline simulation, evidence gates, and candidate lifecycle | Edge/factory SQLite ledgers and content-addressed proof artifacts |
| Trader | Authenticated account reads and order/position mutation | Mode-scoped runtime state, operational journal, events, and heartbeat |
| Watchdog | Cancel and flatten only, never entries | Its own health status file |
| Dashboard | Read-only observation | No authoritative writes |
| Shadow (optional Compose profile) | Read-only recorder/EdgeLedger replay and semantic parity checks; no broker authority | Isolated WAL SQLite database |

Research cannot submit orders or mutate broker state. The trader cannot create
an edge: it may only select an already verified `validated` or `champion`
record whose latest shadow proof carries the parity-matched live-ingestion
marker. The dashboard is not an execution console.

Paper and live runtimes use separate directories and account fingerprints.
Paper is the documented default. Live mode requires an explicit live
configuration, environment guard, authenticated preflight, pattern-day-trader
status, one exact proved variant, and `llm.enabled: false`. The preferred route
is `selection_mode: pinned` with exactly one operator-named promotion; the
legacy `selection_mode: specific` route remains supported for one exact
validated/champion variant with that live marker. Neither route may auto-switch, and a runtime LLM
veto would deploy a different strategy from the deterministic one the gates
passed.

## Deployment and data flow

```text
Alpaca market-data APIs
        |
        v
deploy/recorder.py + deploy/recorder_market.py
        |
        v
normalized market corpus, append-only, partitioned by session date
        |
        +------------------------------+
        |                              |
        v                              v
research.edge_lab              research.strategy_factory
explicit IBR variants          bounded rule-family variants
        |                              |
        +--------------+---------------+
                       v
             statistical/evidence gates
                       |
                       v
        edge ledger + verified proof artifacts
                       |
                       v
              agent.edge edge resolver
                       |
Alpaca account/order APIs <-> trader Engine <-> runtime state/journal
```

The Compose deployment uses a read-only application filesystem, an
unprivileged user, dropped Linux capabilities, bounded CPU/memory/PIDs, Docker
secrets, and named volumes for runtime and research data. There is one trader
replica because the mode-scoped run lock is an additional safety boundary, not
a substitute for single-owner deployment.

The optional `shadow` Compose profile mounts the recorder corpus and EdgeLedger
read-only, has no broker credentials, and writes only its separate WAL database.
It evaluates eligible candidates in isolated virtual books from recorder events,
creates exact-session candidate/root-baseline/randomized-null replays, and
quarantines mismatch/incomplete rows. It uses the same deterministic
signal/setup/risk path and compares semantic signatures with factory/IBR replay;
it has no order, broker, or runtime-state mutation path.

The scheduled research cycle invokes `edge ingest-shadow` by default when
enabled. The consumer treats a missing shadow WAL as a no-op and never creates
or migrates that WAL; the live service remains the sole writer.

## Trading runtime

### Engine composition

`agent.engine.Engine` remains the stable public class. Its responsibilities
are composed in this order:

1. `ExecutionLifecycleMixin` — broker order/fill/position reconciliation and
   protection/close lifecycle.
2. `RuntimeControlMixin` — run loop ownership, pause/shutdown, flattening, and
   authenticated flat-only operator resume.
3. `StartupEdgePolicyMixin` — preflight, session policy, startup cleanup, and
   proved-edge readiness.
4. `MarketEntryRiskMixin` — market collection, freshness validation, daily
   risk state, fail-closed behavior, and order-plan construction.
5. `EngineCycleMixin` — one complete decision cycle.

The facade keeps `Engine` at `agent.engine.Engine`, preserves the established
MRO, and retains legacy patch/import seams. The extracted modules do not import
the facade eagerly, so reverse import order remains safe.

### One decision cycle

A normal cycle follows these gates:

1. Acquire the mode-scoped process lock and confirm state/journal readiness.
2. Authenticate the configured account and verify mode, endpoint, account
   fingerprint, account status, clock, and market calendar.
3. Reconcile durable orders/trades/protection with Alpaca positions and orders.
   Broker truth wins; malformed or unavailable broker state fails closed.
4. Apply regular-session and latest-entry cutoffs. Outside-session cleanup
   cancels working orders and flattens residual exposure.
5. Load the permitted universe and collect the latest valid bars, quotes, and
   (for the option profile) option-chain snapshots.
6. Resolve a vehicle-compatible proved edge from the research ledger and
   generate its deterministic rule/IBR signal.
7. Build a setup plan, then let `RiskEngine` validate prices, stops, daily P&L,
   gross/open risk, option identity, liquidity, freshness, debit, multiplier,
   and contract count.
8. Reject duplicates by underlying and durable pending exposure; create a
   deterministic client order ID with reserved retry suffix space.
9. Submit through `AlpacaProvider`, persist the order/risk plan, and reconcile
   acknowledgement/fills. A post-submit durability failure pauses the runtime
   because broker state may already have changed.
10. Monitor positions and protective exits, journal fills/trades/equity, and
    force-flat before the close.

The runtime never silently substitutes stale data, malformed numeric values,
an unrelated idempotency lookup, an incompatible OCC contract, or an unknown
broker status.

### The bounded-hold exit contract

The bounded rule grammar carries `max_hold_bars`, so a validated strategy has a
time exit as well as a stop and a target. `agent.contracts.rule.hold_deadline`
is the single definition of that exit: given the causal entry timestamp (the
signal's decision/observation time, or the immediate next-bar boundary when it
was already actionable) it returns the close of the last permitted bar, clamped
to the session force-flat time. Both the research simulator
(`research.edge_discovery_core`, `research.factory_core`) and the live runtime
(`agent.strategy` when building the setup plan) call it, so a strategy
validated with an N-bar time exit runs with that same time exit rather than an
approximation of it.

The executable exit grammar is fixed to the ATR-derived bracket (including the
30 bps minimum stop floor), configured R target, and bar-cap time exit. The
factory's fit-only diagnostics expose signal eligibility, 30-bps floor binding,
planned exits, configured/stressed economics, clustered power, behavioral alias
fingerprints, intended-versus-delivered risk, provider/feed provenance,
entry-pricing source, configured limits, and pass/fail/unknown row counts for
operator review. Planned signal/exit geometry may be counted as quote-required
measurement when delayed pricing is unavailable; it remains non-authorizing.
Diagnostics never expand the exit grammar or authorize a candidate.

The runtime persists the resulting `hold_deadline_ts` on the trade, so the exit
survives a restart. `agent.execution_lifecycle` fires the `max_hold` close from
durable state and the clock alone, without needing a market price: a data
outage must not silently extend a hold past the validated horizon. A trade
persisted before the field existed, or an IBR trade, has no deadline and keeps
its historical stop/target/close-only behaviour; a present but unusable
deadline is treated as expired. `tests/test_exit_contract.py` is the
differential test that pins the two engines to one another.

### Concurrent proved edges

`all_proved` paper selection can resolve several independently proved
candidates in one cycle, and the order in which they are offered decides which
one meets the shared risk caps first. `agent.allocation` makes that order
evidence, not alphabet. Candidates are ranked by `evidence_rank`, the identical
ordering `EdgeLedger.select_champion` uses — held-out delta lower confidence
bound, then smaller max drawdown, then held-out trade count, then the point
estimate, with ids only to break ties — and a missing or non-finite metric
collapses to its worst admissible value.

Ranked candidates are then admitted greedily under a correlation cap: a
candidate is refused while a free concurrent position slot is unavailable, or
when it is already represented by an admitted candidate it correlates with at
or above the threshold, so one directional bet expressed several ways does not
become several risks. Correlation is Pearson on held-out per-session R, matched
by session date, taken from the persisted trades of the candidate's latest
re-verified shadow run — the same evidence that authorized deployment. Fewer
than ten shared sessions, a zero-variance series, or an unreadable run all
yield correlation 1.0: absence of evidence is maximal correlation, never
independence. A negative estimate is floored at zero, so no extra slot is
granted on the strength of a claimed hedge. Every input appears exactly once in
the admitted or rejected list and every rejection carries a durable reason,
journalled as an `allocation_reject` event; an allocation failure falls back to
the single best-ranked edge.

Allocation only narrows and reorders. Every per-order risk check in the cycle
still runs unchanged, so it reallocates within the existing risk caps and
cannot admit a set the previous sequential path would have refused.
Live `selection_mode: pinned` (preferred) and `selection_mode: specific`
resolve exactly one record and bypass the allocator entirely. A pin is resolved
again at startup refresh and must retain the exact candidate id and config hash
captured at construction; otherwise the runtime pauses without substituting a
different champion.

### Option protection and its residual

A `buy_to_open` entry cannot ship a protective leg with it, so after the fill
is durable — outside every state transaction, so no retried callback can
replay a broker mutation — the runtime rests one `sell_to_close` limit order
sized to the filled quantity and amended whenever more of the entry fills. Its
limit price is the entry debit grown by the plan's reward-to-risk ratio: the
plan's stop and target are underlying prices, a premium is not a linear
function of them, and the long option's real risk is its whole debit. The leg
is stored in the same `protective_legs` structure as an equity bracket child,
so cancellation, the poller backstop, and the filled-leg close path are shared
code. A lone resting take-profit is not a half-dead bracket and never triggers
the lost-protection close.

The stop stays software. `deploy/watchdog.py` bounds it: a separate process
with its own broker session that flattens when the trader heartbeat is stale
and the broker reports exposure. It takes the mode-scoped run lock first, so a
living trader — even a hung one — keeps it inert rather than racing it into a
double close, and it has no entry path at all. While holding that lock it takes
the final broker snapshots, authenticates and binds the account fingerprint,
and transfers the same lock into the flatten engine. `acted` means flattening
was confirmed; an attempted but incomplete flatten is `degraded` with residual
risk and is unhealthy. What no local process can cover is the broker or the
network being unreachable; in that window an option position has no stop of
any kind. That is the residual, and it is why live mode is equities-only.

### Provider boundary

`agent.alpaca_provider.AlpacaProvider` owns authenticated account and trading
operations. `AlpacaMarketDataMixin` owns the read-only discovery methods for
bars, quotes, option chains, snapshots, contracts, and risk-ready candidates.

`agent.alpaca_domain` contains broker-neutral account/order/position/contract
models and strict request validation. `agent.alpaca_sdk` contains SDK-shape
normalizers and lazy SDK compatibility helpers. Provider results are scoped to
the requested symbols, finite, timezone-aware, and normalized before entering
the engine. Order lookup is locally filtered even when a broker filter is
ignored; an exact client-ID mismatch or a full bounded history makes absence
unprovable and therefore blocks submission.

### Risk boundary

`agent.risk.RiskEngine` is the public risk decision engine. Pure input
normalization lives in `agent.risk_inputs` and is re-exported by the facade.
The risk engine:

- sizes shares from an explicit stop distance and risk budget;
- permits only one long option leg with OCC-consistent underlying, right,
  expiry, strike, and multiplier metadata;
- rejects stale/future/naive timestamps and malformed freshness flags;
- enforces DTE, spread, volume/open-interest, displayed-size, moneyness,
  per-contract loss, contract-count, notional, gross-risk, and daily-loss caps;
- applies the runtime `ReplayPolicy` in research: strict 30-second market/quote
  freshness, latest-entry and force-flat cutoffs, and portfolio limits for
  concurrent positions, position notional, gross exposure, and open risk;
- applies the configured stressed-cost scenario before submission and abstains
  when its cost-to-risk ratio exceeds the limit, persisting scenario/cost/ratio
  telemetry; execution lifecycle also records intended, delivered, and
  shortfall risk for each fill; and
- treats explicit malformed values as errors rather than falling back to a
  default; and
- emits a durable plan whose entry/stop/target/risk/notional fields are the
  same fields consumed by execution and reconciliation.

## Runtime state and recovery

`agent.state` is the compatibility facade for dynamic, mode-scoped paths.
`agent.state_store` owns validated atomic JSON reads/writes. `agent.journal`
owns path-parameterized SQLite schema, migration, readiness, and inserts.

The authoritative JSON state contains the runtime state machine, account
fingerprint, active trades, protective orders, opened timestamps, durable
orders, daily-risk baseline, reconciliation timestamp, preflight record, kill
reason, operator pause, and the durable edge outbox. Writes are lock-protected
and atomically replaced.

The edge outbox makes closed-trade learning durable. A close's paper outcome is
written in the same atomic replacement that removes the trade from
`active_trades`, then drained into the research ledger on that or a later
cycle. A crash before the replacement leaves the trade active and its close is
re-derived; a crash after it leaves the outcome queued. The operational trade
journal also carries a stable `trade_id` protected by a unique index, so a
re-derived close is idempotent. Paper ingestion is keyed by opportunity and
the entry-time shadow `proof_run_id`, so a replayed drain is a no-op while a
later re-proved trial remains a distinct evidence epoch.
A present but malformed state file is corruption; it is never treated as a
fresh default.

Flattening is fail-closed around that state. If `active_trades` cannot be read
and validated, the runtime refuses to bulk-cancel broker orders because doing
so could remove bracket protection without proving that every residual
position can be closed. Likewise, a protective cancel request is not treated
as completion: the runtime polls broker order state for a terminal cancel and
refuses the parent close when a child fill races the cancellation.

The SQLite journal uses WAL plus `synchronous=FULL`, validates required base
columns before migration, and records events, orders, trades, equity, runs,
and schema metadata. Order/trade/event mirroring is SQLite-first; optional JSONL
history failures do not invalidate a successful durable journal write.

`main.py resume` is deliberately flat-only. It acquires the run lock, requires
an exact paused/operator-paused state, re-runs authenticated preflight and
reconciliation, rejects terminal states or any durable/broker position,
working order, active trade, or protection row, performs a final read-only
broker confirmation, and only then clears `operator_pause`. It never cancels,
flattens, submits, or changes the state to `RUNNING`; the operator starts the
trader separately.

## Research and evidence pipeline

### Normalized observations

`research.market_data` defines point-in-time `UnderlyingBar`, `QuoteSnapshot`,
`OptionContract`, and `OptionSnapshot` records. Availability is bounded by the
latest event timestamp, `as_of`, and `observed_at`; normalization rejects naive
or future timestamps, `as_of > observed_at`, malformed OHLC/quotes, invalid
provider/feed identity, and out-of-scope instruments. Session grouping uses
`America/New_York` after timezone conversion. The shipped
`execution.strict_market_data` default is `true`. Required records become
actionable at the maximum of event timestamp, `as_of`, and `observed_at`;
delayed bars can signal when observed, and execution enters at that
decision/observation time using fresh SIP/OPRA evidence. Delayed full OHLC
never backfills an earlier entry, partial pre-entry ranges are excluded, and
historical bar fallback remains diagnostic and is excluded from authorizing
statistics.

### Explicit IBR path

`research.ibr` implements the initial-balance-range reference strategy. It
requires contiguous completed opening-range bars, detects a breakout only
after bar close, enters at the causal decision/observation time (the next-bar
boundary when immediately actionable), applies gap-aware fills,
resolves same-bar stop/target ties against the strategy, fills exit gaps at
the gap open, prices boundary fills from recorded quotes where they exist,
charges spread, slippage and fees through the shared cost model, and closes
before the session boundary. Equity and option
vehicles have separate books. `cost_model_for_vehicle` applies any explicit
vehicle schedule with recorded provenance; the shipped fallback is 4 bps
spread, 6 bps slippage, and the configured per-side fees. Preregistered stress
charges scenario bps against entry notional, plus listed-option round-trip fees
for both per-contract sides; it is not a per-side bps charge. The shipped
25-bps scenario and `max_stressed_cost_to_risk_ratio: 0.30` veto a 30-bps-floor
trade at about `0.833` cost-to-risk before option fees.

`research.edge_discovery_core` owns deterministic corpus loading, effective
IBR configuration, opportunity materialization, the randomized-entry null
control both lanes spend, gate construction, and gate finalization.
`research.edge_lab` owns orchestration: variant registry lookup, replay,
forward-only tail selection, ledger writes, lifecycle transitions, and
champion selection.

Corpus loading serves two shapes. `_read_discovery_rows` materializes one
corpus for an orchestrator; `corpus_slice` re-reads a session window of a
recorded corpus for a worker that was handed a descriptor instead of a copy.
The predicates are the orchestrator's own, so the two produce the same records
in the same order and every hash computed from them is unchanged.

### Autonomous bounded strategy factory

`research.strategy_factory` orchestrates multiple independent strategy
families and isolated simulated accounts. `research.factory_core` owns pure
hypothesis construction, simulation, diagnosis, and bounded mutation.
`research.factory_ledger` owns factory lineage and events.

Generated strategies are data in the finite grammar defined by
`agent.contracts.rule`; research never generates or executes Python source.
Diagnosis uses chronological fit data. Variants are judged on untouched
held-out data, and a backtest winner still needs a strictly later forward
shadow sample before runtime eligibility.

The grammar is versioned. `rule-strategy.v1` is unchanged and keeps every
existing `variant_id` byte-identical, so ledgers written before v2 stay
resolvable. `rule-strategy.v2` is a strict superset, reached only by naming it,
that adds four entry-side predicates — a multi-filter `confirmations` list, a
session-time entry window, and an ATR volatility band. Each is a pure function
of the same completed-bar prefix, so `evaluate_rule_signal` remains the single
evaluator shared by research and runtime and no extension can reach sizing,
exits, or order placement. A v2 spec that admits a signal emits exactly the v1
plan.

### Corpus acquisition

`deploy/recorder.py` samples forward in real time. `deploy/backfill.py` fills
the same corpus from Alpaca's historical bars so a new deployment is not months
away from its first proof. It writes the recorder's exact normalized fields,
`event_key`, session-partition layout, and sidecar index, and rebuilds that
index with the recorder's own scan — which is also the validator, so a repeated
key or malformed row fails at write time rather than downstream. Three
boundaries keep the result trustworthy: only completed sessions are written, so
the recorder's continuity check never meets a mid-session hole; `as_of` is the
bar's completed one-minute boundary, while delayed observations are handled at
their actionable availability time without backfilling an earlier entry; and options are never
fabricated, because their quote-age semantics cannot be
reconstructed from a historical endpoint. Backfill is resumable — a session
with an existing partition is skipped — so re-running is a no-op.

Corpus length is not the only cold-start constraint. `_simulate_trade` takes at
most one trade per symbol-session, so the held-out trade floor is a function of
universe width as well as history depth.

### Research scope

A trader process runs one execution profile, so an edge proved in the other
vehicle is evidence it can never deploy. `agent.edge.research_vehicles` keeps
the scheduled cycle on the equity lane by default, independently of the
trader's execution profile. `ALPACA_RESEARCH_VEHICLES=all` is an explicit
override that runs the equity and option lanes independently, with calibration
and authorization kept per vehicle. The dashboard counts proved edges outside
the tradeable vehicle so evidence stranded by a profile change is reported
rather than silently unusable.

### Slot lifecycle

A slot is a unit of logical research capacity. Its hypothesis leaves
`ACTIVE_HYPOTHESIS_STATES` permanently on a shadow pass, because the proved
variant is deployed and must never be re-tuned; the slot is therefore reseeded
with a new hypothesis in the same cycle. Reseeding prefers an untried family at
that family's template and then continues into a deterministic conditional-v2
ladder. Each reseed grants one further `max_generations` budget and is counted
separately from the failure-recovery rotation budget, which it never consumes.
`run_factory` additionally revives any slot holding no active hypothesis before
scheduling, so an older ledger or a raised `strategies` count recovers without
touching a deployed edge. Without this, capacity fell by one slot per success
and the factory eventually returned `exhausted` permanently.

### The LLM's authority

`research.llm_strategy` answers three bounded requests. `llm-rule-proposal.v1`
repairs a family whose intended variants all failed. `llm-edge-discovery.v1`
seeds a free slot with a new hypothesis, given an aggregate brief of what the
slot has tried, what is already proved, *what the other slots took earlier in
this same cycle*, and how earlier reasons were graded; it must return a short
plain-text thesis alongside the spec. `llm-variant-tuning.v1` proposes the
parameter variants of one hypothesis, each with a one-sentence reason naming
the parameter it changed and the diagnosed problem it should fix. All three
replies are strict, size-capped, fence- and unsafe-key-rejecting JSON validated
against the rule grammar before storage. A proposal is only ever a *seed*: the
resulting hypothesis is registered `queued` and must earn `backtest_passed` and
a strictly later offline forward-shadow pass through the same gates as a
deterministic one. That pass may leave the candidate at `shadow`; live parity
ingestion is still required. All three requests use strict full-schema
structured output (`additionalProperties:
false`, including the complete rule specification). The adapter enforces a
per-run total-call budget, bounded attempts/time/response bytes, and an
authentication circuit that stops further calls after an auth failure; evidence
records schema/grammar hashes, call counts, circuit state, and per-attempt
hashes/errors. The LLM decides what to try next; it cannot shorten the evidence
path, retire a family on invalid output, or authorize trading. Every seeding and
tuning path has a deterministic fallback, so the factory keeps discovering with
no provider configured.

`agent.brain.DecisionBrain` is a separate optional paper-runtime adapter. It is
consulted only after a deterministic strategy has produced a setup; it can
subtract that opportunity with a matching veto but cannot author a setup,
quantity, side, price, order type, or broker call. A strict timeout quarantines
the client for the rest of the process after a hung request. Provider errors or
malformed non-empty responses veto the cycle; an empty or irrelevant response
means no veto and returns to the deterministic path. Live mode rejects both
`llm.enabled: true` and a directly injected brain at the Engine boundary.

No lane can extend the signal vocabulary. `RULE_FAMILIES`, `CONFIRMATIONS`,
`SIDES`, the permitted field set and the numeric bounds in
`agent.contracts.rule` are closed, and `evaluate_rule_signal` — how each signal
is actually computed from bars — is code the research process never generates.
An unknown family, filter, indicator field or data source is rejected at the
boundary, so the model selects and parameterizes signals rather than inventing
them.

Tuning is narrower again: a tuned spec keeps its root's `family` *and* its
root's `schema`. The family pin keeps "which idea is under test" a discovery
decision; the schema pin stops a v1 root reaching v2's extra predicate
categories, which would be adding structure rather than tuning values. What is
left is the values of fields the root already carries. The unmutated root is
always variant zero and is never proposed away. It is compared with an
independent randomized-entry null that preserves the candidate's
session/symbol/direction distribution and exit rules; it is never compared with
itself. The root remains a real candidate in the family and cycle-global
Benjamini–Hochberg denominators, so it consumes multiplicity like any mutation.
Anything the model does not supply, supplies as a duplicate, or supplies
invalidly is topped up from the same deterministic mutation table, so the
variant count is unchanged whether a provider answered or not. Discovery and
tuning de-duplicate by the family-specific executable semantic signature
(including v1/v2 no-op aliases); only exact variant ids with an adequate
recorded failure are suppressed, while underpowered failures remain eligible.

The cycle therefore runs in two scheduled phases. `_diagnose_worker` replays
each hypothesis's root on its fit partition only; the orchestrator then chooses
that hypothesis's variants; `_worker` replays them. Splitting the passes is
what keeps every provider call in the parent process — no adapter is ever
pickled into a worker. Isolated books are logically concurrent but each is
processed by one bounded worker; this is a capacity description, not a claim of
unbounded OS-level parallelism.

### Reasons, and grading them

`factory_lessons` records why something was tried; `factory_lesson_outcomes`
records what the gates then said. They are two append-only tables rather than
one updated row because the two facts are learned at different times: the
reason exists when a variant is proposed, the grade only after its gate is
computed. Fixing the reason first is what makes it a prediction rather than a
story told afterwards. Deterministic mutations record reasons in the same
shape, including an explicit "no diagnosis behind it" for the arithmetic sweep
that fills variants past the diagnosed changes, so the feedback loop can
compare a tuned reason against the fixed table instead of only against itself.

`_lesson_brief` reads the graded pairs back into the next tuning and discovery
request, trimming oldest-first to stay inside the adapter's aggregate bound. A
ledger written before lessons existed degrades to "no history", never to a
failed cycle. Hypothesis-level reasons — why a slot was given an idea at all —
are graded by the best variant that idea produced, never by the root.

Supplying history does not make a model use it, so three rules make the loop
structural. Each brief entry carries a short id, and a tuned variant proposed
against a non-empty brief must cite one in `builds_on`; an uncited proposal, or
one citing an id never supplied, is refused. A variant whose parameters a
graded lesson already recorded as an adequate failure is dropped and the slot
topped up deterministically — `FactoryLedger.failed_variant_ids` deliberately
excludes underpowered results, so a thin sample never closes a door. The
citation is resolved through `resolve_lesson_ref` and stored as
`parent_lesson_id`, making "B was tried because A failed this way" a durable
edge the report renders and counts.

### Statistical gates and lifecycle

`research.gates` provides chronological splits, structural floors, paired and
cluster-aware controls, placebo/falsification tests, held-out separation,
drawdown, sample counts, fixed-rule rolling-origin forward-stability checks,
family-local and cycle-global false-discovery correction inputs, and the
verified-gate envelope. The family and global decisions are separate: global q
is always at least as conservative as the family q, so a marginal candidate
commonly passes its family test while failing the cycle-global test. The
rolling-origin rule is fixed across folds. Proof verification compares each
flag with its own q-value; only the global decision
can authorize cross-family selection.

Offline post-selection does not consume cumulative alpha. It records an
explicit, non-authorizing deferral after family and global BH, then uses the
sealed window only for qualification. The strictly newer parity-matched
live-shadow tail receives the one durable cumulative online-FDR test per
vehicle scope. Its balanced LORD-style state is persisted in the factory
ledger across cycles, so a new cycle cannot reset allocated alpha or discovery
history. The base allocation is `alpha / (n(n+1))`; each prior discovery starts
the same telescoping reward stream. This preserves an infinite testing horizon
without allowing offline candidate churn to exhaust it.

Live-shadow ingestion splits each complete tail into older chronological
selection sessions and a newer disjoint confirmatory window. BH selection uses
only the former; the selected candidate is recomputed on the latter and only
that raw confirmatory p-value is sent to LORD. Same-tail v3 evidence remains
auditable but is quarantined under replay epoch 4. The current
`REPLAY_ENGINE_EPOCH` is 4: older runs remain readable for audit, cannot validate
or authorize runtime, and must be re-derived under epoch 4 to become eligible;
future epochs are quarantined by the same exact-equality check. A current-epoch
run seals one immutable verified gate proof; re-derivation appends a new proof
instead of rewriting the old run.

`research.edge_ledger_store` owns the SQLite schema and hashing primitives.
`research.edge_ledger_proof` owns verified-gate persistence and re-verification.
`research.edge_ledger` owns candidate/run/trade/evidence/event lifecycle,
champion selection, and paper-outcome monitoring.

The lifecycle is forward-only:

```text
candidate -> backtest_passed -> shadow (offline evidence)
                                  --ingest-shadow + live marker-->
                                  shadow (authorized) -> validated -> champion
                                                        \-> retired/demoted when rules permit
```

Every experiment identity binds the dataset, runtime configuration, code, cost
model, risk/`ReplayPolicy`, gate assumptions, and provenance hashes. Every
accepted proof retains those content hashes. Offline historical or forward
replay may persist a passing `lane=shadow` proof, but it is prerequisite
stability evidence only and never authorizes runtime entries. Gate envelopes are
re-verified before use. Malformed legacy proof
rows are skipped rather than crashing champion selection. The final sealed
qualification decision retains bounded candidate/baseline source observations,
their declared session set, and content digests in the envelope; verification
recomputes the qualification report and rejects missing, extra, or tampered
sessions. Gate envelopes also retain per-arm candidate, baseline, and
randomized-null counts, fill sources, quote-age summaries, gross/cost/net
economics, matched and dropped keys, and directional/pair coverage. Quote
density can legitimately change null/control evidence even when the candidate
count is unchanged. Paper outcomes are append-only and can demote an edge; they
cannot manufacture a proof. Retirement requires adequate terminal negative evidence
for every intended variant (and a valid bounded replacement when that lane is
enabled), not an underpowered or transient result. A demoted candidate remains
eligible to re-prove on a newer shadow run, which starts a new evidence epoch.
Only the research-side `edge ingest-shadow` consumer may append the
parity-matched live-shadow marker required for `validated`/`champion`
eligibility; manual/offline promotion cannot bypass it. Legacy
validated/champion rows without that marker can be evaluated or migrated but
remain ineligible until a new authorized live proof.

Qualification is post-selection: development evidence is ranked and corrected
first, then one preselected candidate alone releases and consumes the sealed
window. Other variants may retain diagnostic qualification records, but their
sealed rows cannot authorize a proof.

Offline forward-shadow boundaries advance only from a durable, re-verifying
passing replay proof. Diagnostic account rows do not move the boundary, and a
worker whose intended variants are not all adequately powered persists no
shadow proof or lifecycle result. Its tail is therefore reconsidered instead of
being lost to a prematurely advanced boundary. The live ShadowRunner consumes
strictly newer recorder sessions in candidate-isolated books; `edge
ingest-shadow` opens its WAL read-only, requires complete parity matches and
prior qualification plus source/config/code/provenance/replay/gate hashes,
applies family and global BH with durable online FDR, and only then appends an
immutable `lane=shadow` proof that may transition a candidate to `validated`.
The ingester previews the next allocation without writing it and increases
Monte Carlo resolution enough for the batch-adjusted p-value grid to cross that
allocation. A two-million-draw cap turns an unresolvable test into an explicit
no-op rather than a false failure or spent decision.

Retirement guards every deployed candidate, not only the champion, because
`all_proved` selection trades one validated candidate per family. Two
independent signals demote: the registered rolling-R floor, and a one-sided
sequential likelihood test of live paper R against the held-out R distribution
the candidate's re-verified shadow proof was computed over. The sequential
boundary bounds the probability of retiring an undegraded candidate at
`exp(-4)`, about 1.8% per deployment.

Generation exhaustion in the strategy factory is recoverable but still
bounded. A slot whose family has spent its mutation budget may be reseeded
with an untried family at template defaults, at most `MAX_ROTATIONS` times per
slot and `ROTATION_BUDGET` times per cycle, each rotation granting one further
`max_generations` budget. Neither bound can be raised by configuration. A
reseed after a *proved* edge is a separate, unbounded path: it follows success
rather than failure, so bounding it would only shrink the search.

### Discovery observability

`research.factory_report` reads the factory and edge ledgers and renders what
research did as text, Markdown, or JSON. Both ledgers already recorded every
hypothesis, every variant's full gate, the diagnosis behind each mutation, the
retirement reason and variant count, and the provider/prompt hashes behind an
LLM proposal; none of it was readable, because `factory status` returned rows
and three counts. The report is strictly derived — it opens both ledgers
read-only, computes on read, drops the stored trade rows, and reports the gate
hash beside anything a gate produced, so a claim in the report traces back to
the immutable row it came from. Genesis slots have no ancestor to carry their
provenance, so the seeding decision is recorded on the hypothesis itself; every
other origin is recorded on the ancestor it replaced. Every variant carries the
reason it was tried and who proposed it, and each vehicle carries the graded
reason history: what was tried, why, and what the gates said.

`write_report` archives the Markdown narrative under `research/results/`, which
is the tree the read-only dashboard already lists, and `research.py factory
run` calls it on every cycle — including a cycle that proved nothing, which is
exactly the one an operator needs to read. Previously the report existed only
as a command whose output went to stdout, and the scheduled cycle never invoked
it, so on the documented headless topology nobody ever saw it. The path is
stable per vehicle: the ledgers are the durable record, this file is a view.

### Deployed-edge observability

`EdgeLedger.paper_performance` and `paper_report` read back the append-only
`paper_outcomes` the demotion guards already consume: per-edge trade and
session counts, total and mean R, win rate, net P&L, the rolling-R guard with
its floor and armed/breached state, and the sequential drift statistic against
the validated held-out distribution. Each outcome is attributed to the exact
passing shadow `proof_run_id` captured when the trade entered. Reads and trial
verdicts use only the latest passing proof epoch; a demoted, re-proved candidate
does not inherit losses or wins from the evidence epoch that preceded the new
proof. A failed or unverifiable latest shadow proof quarantines the history
instead of falling back to lifetime outcomes. `research.py edge paper` and the
dashboard's "Live paper results by edge" card are the two read surfaces. They
are derived on every read, never stored, so they cannot drift from the
outcomes they summarize or from the guard that acts on them. Neither surface
can change a lifecycle state.

### Trial, promotion, and the freeze

`research.trial` is the lane between "proved" and "given money". A proved,
unpinned edge trades the paper account; after a configured window of sessions
and trades its live record is judged against an explicit floor. Clearing it
makes the edge *promotable* — reported, never promoted. Missing it parks the
edge and writes the reason into `factory_lessons` as a `trial` lesson sourced
from `live_paper`, graded on the spot, which the next tuning request reads.
That feedback path is the lane's purpose: without it the paper book drove the
demotion guards and taught the search nothing.

`agent.governance` owns the other half. `strategy.pinned` is a list of
operator-declared promotions, each with an id the operator assigns, validated
at config time so a malformed promotion fails the preflight rather than
silently trading nothing. `agent.edge.resolve_pinned_variants` resolves them
through the same `_eligible` gate every other path uses — pinning selects, it
never authorizes — and `unresolved_promotions` reports any pin that cannot
trade, because a promotion that quietly resolves to nothing is the failure
worth surfacing. A pinned candidate is frozen: `ingest_paper_outcome(frozen=
True)` evaluates the guards exactly as before and appends a `guard_alert`
event instead of transitioning, and `review_trials` skips it entirely.

The scheduled cycle reviews trials *before* discovery and tuning, so a trial
that just closed below its floor is already a lesson by the time this cycle
proposes anything.

### Configuration provenance

`record_config_version` writes each distinct configuration the runtime loads
into `config_versions` in the mode-scoped journal: a content-addressed
`config_version_id`, the version it replaced, and a diff naming every field
that moved. Content addressing is what makes the id answer "which settings were
in force" rather than "how many times did the process start" — restarting under
unchanged settings records nothing. Secret-bearing fields are redacted before
hashing, so the trail records that a key changed and never its value; the
honest consequence, which a test pins, is that a change confined to redacted
values does not produce a distinguishable version. Rows are immutable. The
runtime records its version on start and journals the changed paths. Failing to
write the trail never stops a runtime that may already own exposure; instead it
sets a sticky `degraded` heartbeat with reason `config_audit_unavailable` until
a later successful audit clears the condition.

### The reporting surface

`deploy/dashboard.py` is the detailed read-only UI. Beyond trader health it
answers, in order: which edge is earning a promotion (`_trial_view`), what is
pinned and whether each pin can trade (`_promotions`), which strategy and
variant placed every recorded fill and what each variant actually did
(`_journal_view`, joining the `strategy_id`/`variant_id` the journal already
stamped and nothing previously read back), why research tried what it tried
(`_learning`, with the chain each proposal built on), and every configuration
change by id (`_config_audit`). Each panel degrades to an empty state rather
than failing the page, so a recovery image without the research package still
gets a dashboard.

## Safety invariants

- Paper/live mode, endpoint, credentials, runtime directory, and account
  fingerprint must agree.
- The Alpaca clock/calendar controls session eligibility; no local weekday
  approximation can authorize an entry.
- Only day orders are supported. Extended-hours orders and locally submitted
  stop orders are rejected; the equity profile's protection is a broker-side
  bracket whose stop/target child legs the broker itself creates and owns.
- An equity entry is submitted as a bracket or not at all. The local poller is
  a backstop for exits the legs cannot express (session force-flat, bounded
  hold, and lost protection) and must cancel the legs before any close.
- Alpaca accepts market and limit day orders on options only: no bracket, no
  OCO/OTO, and no stop or stop-limit at all. The option profile therefore gets
  a broker-resident take-profit and no broker-resident stop. Its stop is the
  local poller, bounded from outside by `deploy/watchdog.py`, and `mode: live`
  rejects `strategy.execution_mode: options` for exactly that reason.
- `mode: live` rejects `llm.enabled: true`; the deployed strategy must be the
  deterministic rule the gates passed.
- A bounded-hold deadline is computed by one shared helper for research and
  runtime, is clamped to the session force-flat time, and closes from durable
  state and the clock without requiring a market price.
- Startup and shutdown are reconciled; exposure or ambiguous broker state
  blocks entries and pauses safely.
- A durable operator pause survives restart until authenticated flat-only
  resume succeeds.
- No position is intentionally held overnight.
- Research has no broker mutation path, and runtime has no proof-generation
  path.
- Runtime decisions require a vehicle-compatible re-verified proved edge.
- All risk, price, quantity, time, multiplier, P&L, and exposure inputs must be
  finite and type-correct; booleans are not accepted as numbers.
- SQLite/state failures at order-bearing boundaries are fatal to new entries.
- Client order IDs and reconciliation make retries idempotent and observable.

## Decomposition record

The decomposition used stable facades, composition/mixins, path-parameterized
adapters, and call-time dependency forwarding. This kept public class/module
identities, signatures, import order, monkeypatch seams, and serialized class
identity stable while moving cohesive responsibilities.

| Original authority | Extracted responsibility |
| --- | --- |
| `agent.engine` | `execution_lifecycle`, `runtime_control`, `startup_edge_policy`, `market_entry_risk`, `engine_cycle` |
| `agent.state` | Atomic JSON primitives in `state_store`; SQLite adapter in `journal` |
| `agent.alpaca_provider` | Read-only market/options discovery in `alpaca_market_data` |
| `agent.risk` | Pure normalization and identity helpers in `risk_inputs` |
| `research.edge_ledger` | SQLite/hash primitives in `edge_ledger_store`; proof operations in `edge_ledger_proof` |
| `research.edge_lab` | Deterministic discovery helpers in `edge_discovery_core` |
| replay cost/fill arithmetic | One shared model in `research.costs` |
| `research.strategy_factory` | Pure simulation/diagnosis/mutation in `factory_core`; lineage storage in `factory_ledger`; the randomized-entry null control in `edge_discovery_core` and the sealed-window scorer in `gates`, both now spent by either lane |

Compatibility tests assert facade identities, MRO, method ownership, reverse
import order, lazy imports, pickle identity, path rebinding, and legacy helper
patch interception. During extraction, moved method/helper ASTs and differential
runtime scenarios were compared with their pre-extraction versions. The
canonical suite runs under warnings-as-errors; its size is the suite itself,
not a number recorded here.

## Why the remaining larger modules are stop points

Line count alone is not treated as monolithic design. A further split is made
only when it separates an independently testable responsibility without
fragmenting one atomic side-effect boundary. After the extractions above, the
remaining larger modules are cohesive:

| Module | Cohesive authority retained |
| --- | --- |
| `agent.execution_lifecycle` | One broker order/fill/position/protection lifecycle and its durable transitions |
| `research.strategy_factory` | Process/thread orchestration and cross-ledger lifecycle; pure simulation and storage are already extracted |
| `agent.risk` | One public risk-plan decision engine; normalization is already extracted |
| `agent.runtime_control` | One runtime state machine, lock owner, recovery, shutdown, and heartbeat lifecycle |
| `agent.alpaca_provider` | Authenticated account/trading boundary; read-only market discovery is already extracted |
| `research.edge_ledger` | Candidate/run/trade/event lifecycle authority; storage primitives and proof logic are already extracted |
| `agent.market_entry_risk` | One entry-data/risk/fail-closed orchestration boundary |
| `agent.alpaca_domain` | Broker-neutral models and validation |
| `research.market_data` | Provider-neutral market models and normalization |

Splitting these merely to reduce file length would distribute atomic
invariants across modules, add import/patch seams, and increase semantic-drift
risk without creating a new responsibility boundary.

## Verification authority

The release gate is:

```bash
PYTHONPATH=. ./.venv/bin/python -W error -m unittest \
  discover -s tests -t . -p 'test_*.py' -q
./.venv/bin/python -m compileall -q agent deploy research tests
git diff --check
```

The `-t .` argument is intentional: running discovery with `tests/` as the
top-level import directory lets `tests/research` shadow the production
`research` package. That runner-path problem is not a product test failure.

The test suite is the behavioral authority. Setup and runtime operations are
documented in `SETUP.md` and `OPERATIONS.md`; research evidence requirements
are documented in `research/README.md` and `research/protocol.md`.
