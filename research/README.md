# Alpaca intraday research

This directory contains offline replay and bounded research utilities for
US-listed equities/ETFs and listed OCC options; crypto is outside scope.
Options are single-leg long calls or puts only; multi-leg, naked, and short
structures are not supported. Data adapters
normalize their payloads through `research.market_data` before any analysis.
Every normalized event
records its provider, feed, schema, session date/timezone, observation time,
and the as-of timestamp used by the analysis.  Naive timestamps, future
as-of values, invalid quotes, and malformed OHLC values fail closed.

The shipped paper deployment defaults to SIP equity data and OPRA option data;
both entitlements are required for autonomous research and executable option
evidence. The trader's runtime execution profile remains `shares`, with live
trading disabled. Research still evaluates both vehicles by default
(`ALPACA_RESEARCH_VEHICLES=all`): option research is evidence generation, not
permission to execute options. Compose starts the recorder, scheduled research,
broker-free shadow, watchdog, trader, and dashboard with one plain
`docker compose up -d` command; systemd uses the same defaults.

## Normalized data

`UnderlyingBar` is a one-minute OHLCV record. `QuoteSnapshot` is a timestamped
bid/ask record. `OptionContract` and `OptionSnapshot` carry the contract
identity and multiplier separately from the quote. Use
`normalize_underlying_bar`, `normalize_quote`, and
`normalize_option_snapshot` at provider boundaries; replay code does not
accept raw provider dictionaries.

The deployment recorder writes bars, quotes, and option snapshots to one mixed
corpus, appended into one partition per New York session date under
`sessions/` with a sidecar index. `deploy/research-cycle.sh` concatenates those
partitions in session order (`ALPACA_RESEARCH_SESSION_WINDOW` limits it to the
most recent N), validates the result, and routes the vehicle-local discovery
lanes from it.
It also invokes `research.strategy_factory`, which evaluates eleven logical
strategy slots over the finite catalog of eleven rule families by default.
Seven is slot/worker capacity, not the number of families. Each generated
variant owns an isolated simulated account processed by one bounded worker; no
capital or P&L is shared between arms.
Paper runtime selection can then use `selection_mode: all_proved`, which keeps
one best proven variant per independent family under one global risk book.

The authorizing evidence floors are immutable: backtest/factory windows require
100 trades, 30 complete sessions, and 30 session clusters; qualification
requires 100 trades and 30 complete sessions/clusters; the parity-matched
live-shadow tail requires 150 trades and 30 complete sessions. These are
evidence floors, not tuning knobs. The shipped default universe is eight liquid
ETFs (`SPY`, `QQQ`, `IWM`, `DIA`, `XLF`, `XLK`, `XLE`, `XLV`), improving
opportunity capacity, but real signal rates still require sufficient history.
Replay allows at most one trade per symbol-session; floor feasibility fails
closed when a required floor cannot be supported. Widen history and/or
`universe.symbols`, never lower a floor.

Qualification is powered at a minimum of 100 trades, 30 complete sessions, and
30 session-level clusters. Replay epoch 3 additionally requires a 30 bps
minimum stop distance for both rule and IBR paths, a recomputable risk unit
that covers round-trip cost, and executable quote/snapshot fill-quality
evidence rather than bar-only fallback. Evidence from older replay epochs
remains auditable but is quarantined and cannot validate, champion, or authorize
the paper trader until replayed under epoch 3.

The default session timezone is `America/New_York`. Production replay requires
the exact Alpaca calendar session bounds for every session, including early
closes; it never promotes a missing calendar day to a fixed 16:00 close.
Session dates are derived after timezone conversion, so the daylight-saving
transitions in March and November do not shift a bar into a neighboring session.
A source `as_of` timestamp may not be later than its observation/ingestion
timestamp.

## IBR replay

`research.ibr.replay_ibr` implements the initial-balance-range path:

1. Require a complete, contiguous range of completed one-minute bars from the
   configured US session open.
2. Detect a breakout only after a range bar has closed.
3. Enter at the immediate next bar's open; a missing next bar yields no trade.
4. Apply gap-aware fills and stop-first ties when a candle touches both stop
   and target. A bar that opens beyond the stop or target fills at that open,
   on exit as well as on entry, not at a level the market never traded again.
5. Price a fill landing on a bar boundary from a recorded quote at that
   instant when one exists, and record on the trade whether the quote or the
   bar was used. A level triggered inside a bar has no such instant and keeps
   the level.
6. Apply spread, adverse slippage, and per-side fees to both executions
   through the one shared model in `research.costs`.
7. Force-flat at the configured pre-close boundary; no position crosses a
   session boundary.

Equity and single-leg long-option vehicles are independent result books. Call
`replay_ibr_vehicles` for a mapping of separate results; it intentionally has
no pooled P&L field. Option replay requires timestamped option snapshots and
uses their contract multiplier.

Market-data strictness is lane-specific. Direct replay APIs default to strict
`ReplayPolicy` behavior and require a fresh executable equity quote at each
boundary that needs one. Historical backtest lanes use the validated,
research-only `research.backtest_bar_fallback` setting (default `true`): when a
boundary has no equity quote, the bar may supply the reference, the shared
conservative spread/slippage/fee model still prices the fill, and the result
records whether `quote` or `bar` supplied it. Forward-shadow, broker-free
live-shadow, and paper remain strict and require fresh executable quotes.
Backtest evidence never authorizes paper.

Authorizing fill-quality evidence is stricter than a diagnostic backtest:
equity entry and exit legs must carry Alpaca SIP quote provenance, and option
entry and exit legs must carry OPRA quote provenance. Every leg records provider,
feed, quote timestamp/age, and fill source; both legs must be executable and no
older than 30 seconds. Bar-only, missing, partial-feed, or stale legs remain
diagnostic and cannot authorize a proof.

The command-line surface is intentionally small:

```bash
python research.py validate-data bars.jsonl --provider alpaca --feed sip
python research.py backtest-ibr bars.jsonl --vehicle equity
python research.py backtest-ibr bars.jsonl --vehicle equity --strict-market-data
```

`backtest-ibr` defaults to historical bar fallback; `--strict-market-data`
opts into strict diagnostics. Both commands are offline and read JSONL only.
They never download data, call an exchange, place orders, or modify trading
state.

## Costs and fill calibration

`research.costs.CostModel` is the single expected-cost model: every lane --
IBR replay, edge discovery, and the strategy factory -- prices its fills
through it, and none carries its own spread/slippage/fee numbers or its own
arithmetic. The shipped `costs` block is 4 bps spread, 6 bps adverse
slippage, 0.5 bps per-side notional fee, and a 0.65 currency-unit option fee
per contract per side. These are expected costs, not rejection caps.

Every proof also persists the preregistered all-in stress scenarios of 9, 15,
25, and 50 bps. The 25 bps scenario is the authorization requirement; the
other scenarios remain diagnostics. A stress result that is missing, negative,
or not positive at 25 bps cannot authorize a candidate.

The runtime's `execution.max_slippage_bps` and `max_spread_bps` are rejection
caps, not expectations. They are read into the same model and bound it: a
model expecting a cost the runtime would refuse to submit fails closed rather
than simulating fills that could never happen. Sourcing an expected slippage
from the cap is as wrong as ignoring the cap.

Quote-driven fills need the quote rows to reach a strict lane. `research.edge_lab`
and `research.strategy_factory` are handed the complete mixed corpus by
`deploy/research-cycle.sh`, so a scheduled cycle prices a boundary fill from a
recorded quote where one exists and records the source on the trade. Historical
backtests may use the research-only bar fallback described above; the
bar-only, quote-only and option-only views the script derives alongside it are
used to decide which vehicle lanes to run and to feed the standalone
`backtest-ibr` invocation, which receives the quotes explicitly.

```bash
python research.py calibrate runtime/paper/journal.db
```

`research.calibration` reads the runtime journal read-only and reports adverse
cost in basis points, model bias, runtime-cap overruns, and a verdict of
`conservative`, `optimistic`, or `insufficient_data`. Results are stratified by
runtime mode, vehicle, execution profile, and by both entry and exit when
references are present; partial fills use the plan/reference fields rather than
their realized notional, and missing references remain unreferenced. Equity and
options are never pooled. Calibration is an authorization gate: missing, stale,
or insufficient evidence, an optimistic cost verdict, a terminal material
underfill (<80% of requested quantity), or a partial-cancel rate above 20%
returns a veto and non-zero status. The scheduled cycle still records offline
discovery/factory diagnostics, but it does not ingest shadow authorization until
calibration is fresh and authorized. In-flight orders are excluded; calibration
never adjusts the model automatically.

## Evidence and provenance

Research artifacts require row-level provider/feed identity; command-line flags
cannot manufacture missing SIP/OPRA provenance. They retain the normalized input
digest, configuration, code version, cost model, risk/`ReplayPolicy`, and
gate assumptions alongside results. The experiment identity binds dataset,
configuration, code, cost, risk, gates, and provenance hashes. Any feature or
label must be computed from events at or before its as-of timestamp.  A
completed-bar fixture and a no-look-ahead test are required for every new
replay path.  Fixed-rule rolling-origin forward-stability, paired-baseline, placebo/falsification, and
acceptance-floor checks are mandatory gates, with fit/held-out structural
floors, family-local and cycle-global false-discovery correction, sealed
qualification source binding, and a durable verified gate. A family-local pass
with a cycle-global failure is a normal marginal result; only the global result
can authorize cross-family selection. The gates are applied per vehicle and per
session rather than to a pooled equity/options series. Rolling-origin uses the
same fixed rule in every fold. The cumulative
online-FDR allocation is durable per vehicle scope and persists across cycles;
the sealed qualification window is released once by one preselected candidate
alone, while other variants remain diagnostic.

Serial inference is deterministic: paired deltas are grouped by chronological
session/day clusters and the persisted lower bound uses a seeded moving-block
cluster bootstrap (with its draw count, seed, and block length). The persisted
effective-breadth report is a correlation/eigenvalue participation-ratio
diagnostic over matched symbol-by-session deltas. Breadth is re-derived and
verified with the proof, but it never counts as additional independent sample
size; independence still comes from session clusters.

## Edge laboratory

`research.edge_lab` stores pre-registered candidates, immutable
backtest/shadow runs, trades, evidence, paper outcomes, and lifecycle events
in SQLite. Every run carries dataset, configuration, code, and provenance
SHA-256 hashes. The default database is `runtime/research/edge_lab.sqlite3`
(override with `ALPACA_EDGE_DB`).

The lifecycle is forward-only: an initial corpus backtest moves a candidate to
`backtest_passed`; a later corpus must contain sessions strictly after the
persisted boundary, and passing offline forward-shadow gates may move it to
`shadow` only. Offline historical/forward replay never authorizes runtime.
Only broker-free ShadowRunner output consumed by research-side
`edge ingest-shadow` can append the parity-matched live marker and move a
candidate to `validated`/`champion`. A shadow boundary advances only from a
durable re-verifying passing proof; an underpowered worker persists no shadow
result, so the same tail can be reconsidered later. Paper outcomes
are append-only evidence scoped to the exact shadow proof that authorized the
entry and may demote a deployed edge. A demoted candidate may re-prove on a
newer shadow run; re-proving begins a new trial epoch instead of aggregating
lifetime outcomes. Candidates are scored
separately for `equity` and `option` vehicles. Gates require chronological
held-out data, fit/held-out trade and session structural floors, matched
controls, cluster-aware randomisation, family-local and cycle-global FDR,
sealed qualification observations/digests, and placebo/falsification.
Underpowered or inconclusive data is not failure. Retirement is allowed only
after all bounded coordinate/interaction points and the final confirmation
carry a powered upper-bound rejection across multiple negative windows; a
valid bounded LLM replacement must be registered first when that lane is enabled.
Drawdown is
measured and used in conservative champion ranking. Normal operation needs no
manual promotion; the `edge promote` CLI is only for explicit, audited controls
and cannot bypass the live marker. Legacy validated/champion rows without that
marker may be evaluated/migrated but remain ineligible until a new authorized
live proof. Backward rollback is rejected; explicit demotion is the operator
safety action.

## Broker-free live shadow

For a supported paper deployment, create two files outside Git: an Alpaca
paper broker secret (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
`ALPACA_PAPER=true`) and a separate readable research-provider dotenv file
containing `OPENAI_API_KEY` for the checked provider (or the matching
configured provider key). Set the host path in
`ALPACA_RESEARCH_LLM_SECRET_FILE`, validate, and start every lane together:

```bash
docker compose config --quiet
docker compose up -d
docker compose ps
```

Compose starts scheduled research and the broker-free shadow lane in the default
startup. It refuses to render the research service when the separate provider
secret path is missing or unreadable. The plain Compose `shadow` service reads recorder rows and EdgeLedger
candidates through read-only connections, evaluates eligible candidates in
isolated virtual books, and writes only its own WAL SQLite database. For each
complete session it creates candidate, exact root-baseline, and
randomized-entry-null replays; mismatch or incomplete rows are quarantined.
It uses the deterministic runtime signal/setup/risk path and compares semantic
shadow signatures with factory/IBR replay for parity. It has no broker
credentials, order path, or broker/runtime state authority. The scheduled
research cycle runs `edge ingest-shadow` by default when enabled; absent shadow
DB is a no-op. Ingestion opens the WAL read-only and requires strictly newer,
complete parity-matched rows, prior qualification, source/config/code/
provenance/replay/gate hashes, family/global BH, and durable online FDR before
appending the immutable `lane=shadow` proof and live marker.

## Autonomous strategy factory

`research.strategy_factory` owns safe hypothesis generation. Its proposal
language is the finite grammar in `agent.contracts.rule`: eleven signal
primitives — opening-range breakout/fade, momentum continuation, mean
reversion, trend pullback, volatility breakout, volume breakout, VWAP
reversion, VWAP trend, range expansion, and opening drive — with bounded
confirmations and exit parameters. It never generates or imports source code.

The last four are session-anchored: they re-derive the current session from the
bars' own New York dates, so a longer history can never contaminate a session
statistic. Research replays one session at a time and the runtime fetches from
the session open, so the two see the same window either way.

The grammar has two versions. `rule-strategy.v1` is the original field set and
is unchanged, so every candidate already in a ledger keeps its exact
`variant_id`. `rule-strategy.v2` is a strict superset reached only by naming it
explicitly, and adds four *entry-side* predicates: `confirmations` (a list of
additional trend/volume/volatility filters, all of which must hold),
`entry_after_minutes`/`entry_before_minutes` (the minutes-from-09:30-New-York
window a signal may fire in), and `min_atr_bps`/`max_atr_bps` (the volatility
regime the rule may trade). Together they let a hypothesis express a
*conditional* edge rather than only retuned numbers. Every extension is a pure
function of the same completed-bar prefix the v1 grammar already sees, so
research and runtime remain one evaluator and no extension can reach sizing,
exits, or order placement; a v2 spec that admits a signal produces exactly the
v1 plan.

### Slots are capacity, not licences

The factory runs a fixed number of logical slots. A slot's hypothesis leaves
the active set permanently once it proves an edge — the deployed variant is
frozen and must never be re-tuned — so the slot is immediately reseeded with a
*new* hypothesis in the same cycle. Without that reseed the factory would lose
one worker per success and eventually have nothing left to search. Reseeding
prefers a family the slot has not tried, at that family's own template, and
then continues into the conditional v2 ladder: a slot that has run out of
families has not run out of hypotheses. Each reseed grants one further
`max_generations` mutation budget and never consumes the separate
failure-recovery rotation budget.

`run_factory` gives every configured slot an active hypothesis at the start of
every cycle, which is one code path for three situations: genesis on a fresh
ledger, a slot added by raising `--strategies`, and a slot that lost its
hypothesis (a ledger written before reseeding existed). The cycle result
reports `seeded`, `revived`, `reseeds`, and `active_slots` separately —
`revived` is the one that says something had gone wrong — so idle capacity is
visible rather than silent.

Research defaults to both vehicles. `research.py vehicles` reports the selected
set, and `ALPACA_RESEARCH_VEHICLES` may narrow it to a comma-separated subset;
the shipped Compose and systemd paths set it to `all`. The trader still runs one
runtime execution profile, `shares`, so proving an option edge is useful
research evidence but never switches the trader to options or authorizes an
options order. Select the `options` execution profile separately, on paper,
only after its OPRA evidence and controls have been reviewed.

On a fresh corpus, each worker diagnoses its baseline only from the
chronological fit partition, creates bounded variants based on the observed
failure mode, and evaluates those variants on untouched held-out sessions.
Every variant has a separate simulated cash/equity account. Coordinate
refinement changes exactly one executable field at a time. After all such
points are measured, at most two measured fields are combined in a bounded
interaction phase, followed by one unchanged confirmation. A family is retired
only when every point explicitly rejects a useful edge with adequate powered
evidence; if LLM replacement is enabled, a valid bounded proposal is registered first. A
missing or invalid LLM proposal leaves the family pending replacement, not
retired. Insufficient data is not treated as failure. Backtest winners must
still pass strictly later forward data before runtime can select them.

A worker is given a corpus descriptor — the session window it needs — rather
than a copy of the corpus, and re-reads that window itself. The predicates are
the orchestrator's own, so the trades, statistics and content hashes are the
same either way; an in-memory corpus has nothing to re-read and still travels
with the task.

### What the LLM does, and what it cannot do

`research.llm_strategy` serves three distinct requests, all bounded by the same
output contract:

- `llm-rule-proposal.v1` — **repair.** Asked for a replacement `rule_spec` only
  after every intended variant of a family has failed with an adequate sample.
- `llm-edge-discovery.v1` — **discovery.** Asked for a genuinely new hypothesis
  whenever a slot needs one: at genesis on a fresh ledger, when a slot proves
  an edge and is reseeded, when a generation budget runs out, and when an idle
  slot is revived. The request carries a small aggregate brief — the families
  this slot has tried, the families already carrying a deployed edge, the last
  diagnosis, the slots already seeded earlier *in this same cycle*, and the
  graded history of earlier reasons — and the reply must include a
  one-sentence `thesis` of at most 240 characters, which is recorded as
  evidence and displayed but never interpreted as an instruction.
- `llm-variant-tuning.v1` — **tuning.** Asked for the parameter variants of one
  hypothesis, given its root spec, the fit-partition diagnosis, and the graded
  lessons. Each returned variant must carry a `reason` of at most 240
  characters naming the parameter it changed and the diagnosed problem it
  should fix, and a `builds_on` citing the lesson it reasoned from.

### The model tunes values; it cannot invent a signal

The signal primitives are fixed code. `RULE_FAMILIES` names eleven of them,
`CONFIRMATIONS` and `SIDES` are closed enums, the permitted field set is fixed
per grammar version, and every numeric field has hard bounds — so
`validate_rule_spec` rejects an unknown family, an unknown filter, an invented
indicator field, or a new data source before anything reaches an evaluator.
How each signal is computed from the bars lives in
`agent/contracts/rule.py::evaluate_rule_signal`, which is code the research
process never generates, imports, or rewrites. That has always been true of
every lane; tuning does not weaken it.

Tuning is narrower still. A tuned spec must keep its root's `family` *and* its
root's `schema`:

- **family** — changing which idea is under test is discovery's job, not
  tuning's.
- **schema** — `rule-strategy.v2` unlocks whole categories of predicate
  (`confirmations`, an entry-time window, an ATR regime band) that the root was
  never expressed in. Reaching them is adding structure, not tuning values, so
  a v1 root is tuned in v1 and a v2 root is tuned in v2.

What is left is exactly what "tuning the parameters" means: the values of the
fields the root already has, inside the bounds the grammar already enforces.

### Why something was tried, and how that turned out

Parameter search used to be a fixed table: three hand-written responses per
diagnosed failure mode, with an arithmetic sweep filling anything past that.
It worked, but nothing it learned in one cycle reached the next one.

All three replies use strict full-schema structured JSON (`additionalProperties:
false`, including the complete rule specification), are size-capped,
fence-rejecting, and validated against the rule grammar before anything is
stored; keys that look like source, credentials, or market rows are refused
outright. The adapter enforces a per-run total-call budget, bounded attempts,
timeouts, and response bytes, and opens an authentication circuit after an auth
failure. Result evidence records schema/grammar hashes, call counts, circuit
state, and each attempt's hashes/errors. A discovered hypothesis is
registered `queued` with no run, no gate, and no candidate — it must earn
`backtest_passed` and then a strictly later offline forward-shadow pass through
exactly the same gates as a deterministic one. That pass may leave the
candidate at `shadow`; only live parity ingestion can authorize runtime. A
tuned variant gets its own isolated
simulated account and faces every gate a mutated variant faces. **The LLM
chooses what to try next; it can never shorten the evidence path or authorize
trading.** The shipped lane requires its separate readable provider secret;
missing or unreadable credentials fail closed before discovery rather than
downgrading the run to an unauthenticated or silently different mode.

Every proposal records a **reason** before the gate that will judge it exists,
and that reason is **graded** against the gate afterwards. The pair lives in two
append-only tables — `factory_lessons` for the reason,
`factory_lesson_outcomes` for the verdict — because the two facts are known at
different times, and writing the reason first is what makes it a prediction
rather than a summary. Deterministic mutations record reasons in the same
shape, including an explicit "no diagnosis behind it" marker on the sweep fill,
so a tuned reason can be compared against the fixed table rather than only
against other tuned reasons.

The graded pairs are read back into the next tuning and discovery request,
oldest-first-trimmed to stay inside the prompt's aggregate bound.

### A proposal has to build on a learning, not arrive from nowhere

Handing the model its history is necessary but not sufficient — nothing in a
prompt makes a model use what it was given. Three rules make the loop a
property of the system instead:

1. **Cite the lesson.** Each brief entry carries a short id. When any lesson is
   supplied, every returned variant must set `builds_on` to one of those ids,
   and its `reason` must say what that lesson showed. An uncited proposal is
   refused; so is one citing an id that was never supplied, which would be a
   fabricated citation.
2. **Do not re-run a settled experiment.** A variant whose parameters a graded
   lesson already recorded as a powered negative rejection is dropped, and the
   deterministic table tops the slot back up. *Underpowered* is not a failure,
   so a thin sample never closes a door.
3. **Keep the chain.** The citation is resolved to a real lesson id and stored
   as `parent_lesson_id`, so "B was tried because A failed this way" is a
   durable edge in the ledger. `factory report` renders it as `built on:` and
   counts how many proposals stood on an earlier result.

That is the whole loop: propose with a reason and a citation, evaluate under
unchanged gates, grade the reason, and hand the grade forward.

Across all discovery lanes, executable variants are de-duplicated by a
family-specific semantic signature, including v1/v2 no-op aliases. Only a
graded `adequate_negative_rejection` suppresses its exact variant id;
underpowered and adequate-but-inconclusive results remain eligible.

### Learning shared across every strategy

A per-family brief can only say what happened to one idea. Some of what
research learns is not about one idea at all — that lowering a target tends to
help across families, that live trials keep disagreeing with replays, that one
direction of change fails everywhere it is tried. `shared_learning` aggregates
exactly that and attaches it to every tuning and discovery request, so a fresh
slot with no history of its own still has evidence to reason from.

It is an aggregate of *outcomes*, never of market data: each entry is a
parameter name, a direction, how many graded attempts changed it that way, and
how many passed. Underpowered attempts are excluded — they say something about
the sample, not the parameter — and a direction needs at least three graded
attempts before it is reported, because a "trend" drawn from one is worse than
no trend. Nothing in the digest is derived from a held-out, sealed, or
later-forward observation, so sharing it adds no information about unseen data.

## The paper-account trial lane

`research.trial` closes the loop between the book and the search.

A proved edge that is not pinned trades the same Alpaca paper account, and its real fills
accumulate as `paper_outcomes`, each carrying the exact passing shadow
`proof_run_id` that authorized entry. After the trial window —
`research.trial` config, default 30 sessions and 100 trades — that proof epoch
is judged against an explicit floor (total R and mean R both positive by
default):

- **Clears it** → the edge keeps trading and becomes *promotable*. Nothing is
  promoted; `edge promotable` hands the operator the config block.
- **Misses it** → the edge is parked, and the reason is written into the lesson
  ledger as a `trial` lesson from `live_paper`, graded immediately. The next
  tuning request reads it, so the parameters proposed next are answering the
  book rather than only the replay. This is the point of the lane.
- **Window still open, or outcomes carry no usable R** → nothing happens.
  Underpowered is not failure here either.

A pinned edge is never judged: it is reported with its verdict and left exactly
where the operator put it.

```bash
python research.py edge trials --dry-run   # verdicts, changing nothing
python research.py edge trials             # park the failures (exit 3)
python research.py edge promotable         # what has earned a promotion
```

The scheduled cycle runs the review *before* discovery and tuning, so a trial
that just finished below its floor is already a recorded lesson by the time
this cycle proposes anything. `ALPACA_TRIAL_REVIEW_ENABLED=0` turns it off.

If the same immutable candidate is later re-proved, the newer passing shadow
run starts a fresh trial epoch. Earlier outcomes remain in the append-only
ledger but are excluded from `paper_performance`, drift, and the new verdict.
A failed or unverifiable latest shadow proof quarantines history rather than
falling back to lifetime aggregation.

Parking an edge never promotes anything in its place. The replacement still has
to earn `backtest_passed`, a strictly later offline shadow pass, and every gate,
then obtain a new parity-matched live proof before runtime eligibility.

```bash
python research.py factory report --format markdown   # includes the graded reasons
```

The unmutated root is always variant zero and is never proposed away. It is
compared with an independent randomized-entry null that preserves the
candidate's session/symbol/direction distribution and exit rules; it is never
compared with itself. The root remains a real candidate in the family and
cycle-global Benjamini–Hochberg denominators, so it consumes multiplicity like
any mutation.

The checked config enables model-assisted discovery, replacement, and tuning
with OpenAI `gpt-5`. Compose uses the host override
`ALPACA_RESEARCH_LLM_SECRET_FILE`; the scheduler reads the mounted path through
`ALPACA_RESEARCH_LLM_SECRETS_FILE`. Provider keys are read only from that
separate readable file, never from the broker secret. An enabled adapter
without its provider key fails before discovery; invalid or rejected model
output records a pending replacement and cannot retire a family prematurely.
Successful proof produces a deterministic,
content-addressed finding. `research.proof.webhook_url` may send that finding
to an HTTPS webhook without changing the durable artifact.

`OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` are trust boundaries: a configured
endpoint receives the provider key and the bounded aggregate prompt. Use only a
trusted HTTPS service; there is no application host allowlist. Prompt, request,
and raw-response hashes prove what the cycle consumed, but they do not make a
mutable model/provider response reproducible. The adapter's call budget is a
per-run call-count guard rather than spend accounting; provider-side quotas
remain an operational control.

The scheduled cycle reports `completed`, `completed_no_edge`, `no_data`, or
`failed`. `completed_no_edge` means the input was valid but no candidate was
proved; the report separates `adequate_negative_rejection`,
`adequate_negative_inconclusive`, `adequate_inconclusive`, `underpowered`, and
families not yet tested. `no_data` means the input was unavailable or empty. Neither status
permits bypassing the runtime edge gate.

Before validation, the scheduled cycle builds temporary normalized views and
emits `research-cycle-quarantine.v1`. Recorder rows from the legacy observation
timestamp bug (`as_of > observed_at`) are excluded from those views without
modifying the append-only source. Missing evidence remains visible to replay
coverage/refusal gates; every other malformed row remains a hard validation
failure.

```bash
python research.py factory run --data market.jsonl --strategies 11 --variants 4 --workers 2
python research.py factory status
python research.py factory report [--slot N] [--format text|markdown|json] [--write]
```

`factory run` archives the Markdown narrative under `research/results/factory/`
on every cycle, including a cycle that proved nothing, so the read-only
dashboard lists it without anyone running a command. `--write` does the same
on demand; `ALPACA_RESEARCH_REPORT_DIR` overrides the destination.

`research.factory_report` is the reader for everything the two ledgers already
record but nothing previously opened: per slot, the lineage of hypotheses it
has held; per hypothesis, its origin (template, deterministic mutation,
rotation, LLM discovery, LLM replacement) with the model and the prompt,
request and response content hashes where one applies, plus its thesis and its
falsification condition; per variant, trade counts split fit/held-out, net P&L,
drawdown, held-out delta and lower bound, q-value, and the named gates it
missed; and per outcome, why a hypothesis was retired, after how many of its
intended variants, the dominant failure mode, and what replaced it. It opens
both ledgers read-only, derives every number on read, and reports the gate hash
beside anything a gate produced.

```bash
python research.py edge init
python research.py edge discover --data market.jsonl --vehicle equity --lane auto
python research.py edge status --vehicle equity
python research.py edge ingest CANDIDATE_ID paper-outcome.json
python research.py edge paper --vehicle equity --deployed
```

`edge status` reports lifecycle state; `edge paper` reports how each edge is
actually doing on live paper outcomes — trade and session counts, total and
mean R, win rate, net P&L, the registered rolling-R guard with its floor, and
the sequential drift statistic against the held-out distribution the edge was
validated on. The live view is limited to the latest passing shadow proof epoch,
so “what it has done since” means since the proof currently authorizing it, not
the candidate's lifetime. Both matter: the first is the evidence an edge was
promoted with, the second is what it has done since. Neither can change a
lifecycle state; they are read-only views of append-only data.
