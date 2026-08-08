# OKX AI Trading Agent

This repository runs a demo-mode OKX perpetuals runtime and a separate
research system for seven registered mechanism models. The shipped
`strategy.execution_mode` is `shadow_only`: no order, LLM, or deterministic
entry path is active; only isolated shadow research runs. Four realtime lanes
use the shared market feed; `funding-carry`, `funding-unwind`, and
`trend-multiday` are offline-only. No strategy is currently eligible for live
capital.

Use [SETUP.md](SETUP.md) for installation and VM deployment, and
[OPERATIONS.md](OPERATIONS.md) for daily operation, research, backups, and
recovery. The one canonical lifecycle—from market snapshot through human live
approval—is [research/AUTONOMOUS_RESEARCH.md](research/AUTONOMOUS_RESEARCH.md).

Documentation authority is deliberately short: executable code and
`config.yaml` define behaviour; `SETUP.md` is the installation/deployment
authority; `OPERATIONS.md` is the runbook; the research documents define
evidence rules and strategy identities. Files under `research/plan/` are
compatibility or safety records, not a second implementation plan. When prose
and executable behaviour differ, report the executable behaviour narrowly.

## Canonical strategy identity

`StrategyContract` is the authoritative composite of the registry strategy
specification, forward-model economics/exit, evidence builder, immutable
variant identity, and semantic hash. Startup validation and evidence ingest
reject a missing or mismatched contract/hash; legacy or mismatched evidence is
retained for audit but is quarantined from inference.

Funding is an evidence gate, not an optional annotation. Only
`verified_realized` or `verified_no_settlement_due` funding status is eligible
for inference, qualification, or authoring metrics. Missing, partial, and
legacy funding rows remain audit-only and are excluded.

## Shipped configuration

These values come from the current `config.yaml` and registry:

```yaml
research:
  findings_store: research/cache/findings.db
  experiment_min_duration_days: 10
  experiment_min_observations: 100
  experiment_candidate_batch_size: 4
  shadow_workers: 4
  collector: {top_n: 50, workers: 4}
```

| Area | Current value |
| --- | --- |
| Account mode | `demo` (OKX Demo Trading) |
| Configured strategy contract | `ls-ratio-fade/v1`, explicit tuned variant `ls_ratio_fade.tuned_70_30_ext_1_5_stop_1_target_3`; base registry contract remains 80/20, 3 ATR extension, 2 ATR stop, 2R target |
| Execution mode | `shadow_only` (shipped): no order/LLM/deterministic entry path; shadow research only |
| Runtime tier | `T1_HYPOTHESIS`; unproven research identity, not supported |
| LLM route | provider `openai`, model/deployment identifier `gpt-5.6-sol-coding` |
| Housekeeping cadence | `cycle.interval_seconds: 60` |
| Decision throttle | `cycle.decision_interval_seconds: 300` elapsed seconds (with a 95% jitter tolerance); safety/mark cycles stay faster |
| Journal | `runtime/demo/journal.db` in the shipped mode |
| Findings store | `research/cache/findings.db`, SQLite schema 17 |
| Research feed | `forward_feed_version: 8`; feed v8 repairs the depth-ladder delivery that silently starved six of seven strategies and widens the universe to 25; feeds v1-v7 remain historical (v4 is the market-data plumbing repair feed, v5 the immutable-provenance fork, v6 the deterministic four-lane realtime fork, and v7 added the liquidation flow and conditioning axes) |
| Realtime comparison arms | Each deterministic lane keeps one shared baseline and a bounded batch of up to 4 pre-registered candidates (hard cap 8 per lane). The configured tuned LS identity is a pinned isolated paper arm with its own account; it is never offered as an adaptive one-axis selector candidate. The shipped `shadow_only` runtime creates no order path or `:llm` lane; analyst mode may create a separate non-comparable one |
| Shadow workers | `4` bounded workers per evaluator; durable FindingsStore commits remain serialized |
| Research collector | Separate recorder scans up to 50 instruments with 4 public-read workers; it does not alter the active universe or risk limits or block trader/research startup |
| Experiment floor | both 10 elapsed days and 100 comparable paired observations |
| Local paper balance | 10,000 USDT per isolated strategy/variant account |

The repository contains no credentials and does not claim that credentials are
installed. Secrets belong in `.env`. Never enable Withdraw on an API key.

## End-to-end behavior

The shipped `shadow_only` mode has no order path and no exchange object for
entry. It cannot place, amend, or cancel an order; the demo account setting is
an environment/account mode, not an authorization to trade.

On each eligible decision cycle:

1. The engine records one market snapshot and timestamp. `book_state` and
   `snapshot_enrichment` preserve the market inputs needed by research.
2. The shipped mode makes no LLM call and does not invoke a deterministic
   entry path. In explicitly selected `analyst` or `deterministic` modes,
   those paths remain separately contract-bound and their decisions are
   recorded for the selected mode.
3. Shadow evaluators apply deterministic contracts, risk rules, and paper
   execution controls without exchange access.
4. The `StrategyShadowCoordinator` gives the same snapshot/timestamp to four
   isolated deterministic realtime evaluators: `momentum`, `flush-fade`,
   `ls-ratio-fade`, and `scalp-maker`. The three long-horizon models are
   registered for offline research and do not occupy realtime lanes.
5. Each evaluator owns its own paper cash, positions, exposure, cooldowns,
   circuit breakers, decisions, and trades. `shadow_decision` rows include
   accepted actions and policy vetoes as explicit zero-return actions.
6. Every realtime strategy keeps one stable baseline running while a bounded
   batch of pre-registered candidates tests one axis family in parallel. Each
   candidate has its own durable assignment/account, all candidates share the
   same baseline input, and the batch is capped at eight candidates per lane.
   Each individual assignment still tests at most one candidate setting.
   Workers compute isolated packets concurrently; durable SQLite writes remain
   serialized. Assignments drain independently and survive process restarts.

The configured `ls_ratio_fade.tuned_70_30_ext_1_5_stop_1_target_3` arm is
explicitly pinned and isolated from that adaptive selector: its coupled 70/30,
1.5-ATR, 1-ATR, 3R contract is one paper account and is never an adaptive
one-axis assignment candidate.

The deterministic comparison set is therefore batched per lane rather than
serially evaluating one candidate at a time. The shipped `shadow_only` runtime
creates no order path or `:llm` scope. If analyst or deterministic mode is
selected explicitly, its decisions remain in a separate scope and are not
qualification evidence for another mode.

Isolation runs both ways: research state and decisions are withheld from
everything on the live path, while live account positions do not leak into a
shadow account. Universe breadth is recomputed for each strategy contract from
the shared snapshot rather than copied from the active strategy's result.

## Hypotheses, variants, and selection

The seven strategy hypotheses, their declared settings, and the different
variant registries are indexed in
[research/HYPOTHESES_AND_VARIANTS.md](research/HYPOTHESES_AND_VARIANTS.md).
The important distinction is:

- strategies are mechanism/falsification claims in `agent/registry.py`;
- static runtime variants come from `research/variants.yaml` and generated
  registered settings;
- exact adaptive numeric proposals become new immutable variant identities in
  the findings store;
- tournament settings are pre-registered offline parameterisations and do not
  alter runtime configuration.

The LLM may select only a registered strategy or an exact eligible single-axis
variant through a bounded research-only contract. Invalid requests are stored
as `REJECTED`, including their reason. Accepted requests are stored as
`ACCEPTED`, then become `ASSIGNED` and `TESTED` as the durable rotation reaches
them. An accepted request never preempts an active assignment.

The live-order prompt and the research-review prompt are separate. The nightly
research reviewer receives an already immutable deterministic outcome. It may
explain the result and nominate one bounded next selection; it cannot alter the
verdict, change account settings, or authorize an order.

## Contract-bound execution modes

`strategy.execution_mode` selects what decides on the order path:

- `analyst`: the momentum analyst makes one LLM call per decision cycle and
  its parsed decisions go through contracts, risk and execution;
- `deterministic`: the strategy's own forward contract proposes, and no LLM
  client, preflight, order-decision call, or `:llm` evidence lane is created;
- `shadow_only`: nothing proposes and nothing opens; research lanes run on.

A strategy earns promotion only on evidence its deterministic contract produced
in a shadow lane. Running it under an analyst would trade something other than
the thing that earned the promotion. The shipped `shadow_only` mode keeps both
entry sources disabled while that evidence is collected.

It also unblocks a practical constraint: `momentum` is the only strategy with
`analyst_ready=True`, so under `analyst` no other registered strategy can
occupy the order path whatever its evidence says. Under `deterministic` any
strategy with a complete forward contract can, and configuration refuses to
start when that contract is missing. Tier gating is unchanged - live still
requires `T3_VALIDATED` and a reviewed packet.

Only the source of the decisions differs. Research recording, risk vetting,
execution controls and the close path are one code path in every mode.

`ls-ratio-fade` is the configured research identity; it does not currently
occupy an order path. `momentum/phase1-v3` is
`T0_REJECTED`, returned -8.97% over 2026-07-29..08-05 across 35 closes, and is
the only strategy the recorded corpus says anything significant about
(-0.428R over 43 independent 48h episodes, t=-2.45). Left running at that rate
it reaches `risk.max_drawdown_pct`, which flattens the book and self-kills the
process, ending the research collection every other lane depends on.

The tuned identity is a research choice among unproven mechanisms, has no
positive edge claim, and is not a promotion. The registered base contract
remains 80/20 tails, 3 ATR extension cap, 2 ATR minimum stop, and 2R target.
The explicit tuned
variant `ls_ratio_fade.tuned_70_30_ext_1_5_stop_1_target_3` records 70/30 tails,
1.5 ATR extension, 1 ATR stop, and 3R target; it is research-only and its
evidence has not established a positive edge.
[research/plan/order-path-succession.md](research/plan/order-path-succession.md)
holds the full comparison and states, pre-committed, what would earn the seat
on evidence rather than on elimination.

## Experiment outcomes and edge evidence

Assignment completion requires both configured rotation floors: by default,
ten elapsed calendar days and 100 comparable paired observations. Completion
only closes the collection assignment; it does not guarantee adequate evidence.

The deterministic outcome evaluator checks adequacy before performance. It
requires resolved baseline/candidate evidence, 100 full pairs, 70 fit pairs,
30 confirmation pairs, 80% coverage, at least eight distinct six-hour
episodes, consistent model/provenance, and no unresolved or operationally
invalid evidence. It then checks positive after-cost expectancy, improvement
over baseline, drawdown no worse than baseline, fit/confirmation consistency,
confidence intervals, and existing disqualifying statuses.

Every terminal assignment receives exactly one immutable outcome:

- `WORKED`: all conservative gates passed;
- `FAILED`: adequate evidence showed failure or a disqualifying condition;
- `INCONCLUSIVE`: evidence was inadequate or did not establish the delta.

Success and failure reasons, limitations, analyses, review attempts, and LLM
explanations persist. `WORKED` creates an `EDGE_CANDIDATE` whose authority is
explicitly `RESEARCH_ONLY` and whose `promotion_allowed` flag is false. There
is no automatic live deployment, strategy switch, tier change, or edge
promotion. It is an immutable research lead, and current v8 forward
qualification is still required.

The current `forward-qualify` path reconstructs current feed-v8 evidence from eligible
completed assignments and each setting's contemporaneous baseline. It proves
one declared axis from the immutable decision ledger, including the non-axis executable
configuration, held-out confirmation, and family correction. Its
paired cluster sign-flip test is valid only under the persisted
cluster-delta sign-exchangeability/symmetric-null assumption; it is not an
assumption-free p-value.

Feed v8 is the current deterministic four-lane identity; the three long-horizon
models remain offline-only. In analyst mode only, genuine analyst decisions
use a separate `:llm` scope and are never pooled with lane evidence. Feeds
v1-v7 remain historical and are never migrated or pooled. `research.py
prepare-review-artifacts` runs only after v8 qualification. It
fails closed unless the saved edge evidence and every non-manual T3 check
validate, then idempotently creates an immutable, content-addressed draft
review artifact. It cannot complete manual review, edit the registry or
configuration, switch strategies, or enable live trading. A reviewed T3 packet
and any registry/configuration change remain explicit human actions.

The protocol and shortlist use the same policy-neutral primitives in
`research/evidence_primitives.py`: canonical opportunity identity, duplicate
exclusion, chronological split, and pair/union coverage. Their lane policies
remain separate (qualification gates versus shortlist labels), but neither may
silently invent a different opportunity or resurrect a duplicate.

Price-cache requests use an end-exclusive range: a bar exactly at `end_ms` is
outside the requested window. Direct outcome timeouts likewise require every
bar through the full requested horizon; a partial cache is not a timeout.

## Evidence paths

- The recorded journal path is authoritative only after the proposal-fidelity
  replay passes. It compares the full canonical pre-risk proposal identity (including
  cycle, symbol, direction, setup identity/type, signal timestamp, strategy
  version, and baseline variant) symmetrically with replay keys. It requires a
  non-vacuous exact match; malformed, duplicate, missing, or extra identities
  fail closed. Outcome-resolution gaps remain explicit diagnostics and are not
  scored as proposal mismatches. A failed, stale, or vacuous result blocks
  downstream evidence.
  `INSUFFICIENT_SAMPLE` means collection is open, not that an edge failed.
- The OHLCV tournament is exploratory. It awards no tier above
  `T2_CANDIDATE`; an existing higher registered tier is reported as unrevised
  rather than as demoted. Tournament results cannot promote live capital.

The v6→v7 migration introduced the complete immutable decision ledger and its
legacy watermark. Schema migration 7 therefore remains relevant to evidence
validity even though the current Findings store is schema 17.

Executable fingerprints cover the LLM provider/model, prompt, strategy and
configuration, code, universe selection, and decision cadence; credentials are
excluded. Known replay limits include exchange-only fill races, `loss cooldown`
state, and `select_universe` feedback that cannot be reconstructed exactly.
Operational failures invalidate a forward window instead of disappearing as
missing data. PAPER is local simulation only.

The available exit policies are `fixed_rr`, `extended_rr` and
`carry_until_normalised`. The last one belongs to `funding-carry`, whose return
source is the funding it collects rather than a directional forecast: it holds
while funding pays and closes when the 30-day funding percentile falls back to
its median, instead of on a price target it never claimed to be forecasting.
Its stop is unchanged, because price risk over the holding window still has to
be charged against the carry.

## Recorded market events and bounded discovery

The recorder annotates each row with receipt/availability, source, feed,
schema, revision, and quality metadata. `research.py ingest-recorded` writes
the `event-plane.v1` SQLite view at
`runtime/research/market_events.db`, archives immutable raw CSV snapshots,
strictly joins events and availability as-of a decision, and quarantines
malformed or legacy rows. Nightly research ingests before discovery;
missing data is a nonfatal `NO_DATA` state. Recorder health is observable but
no longer blocks trader or research Compose startup.
The event-plane schema is independent of the experiment identity;
`forward_feed_version` remains 8.

`research.py discover` is research-only. It evaluates a typed bounded IR,
including the forced-flow-pressure observable from persisted open-interest,
taker, and book fields; records a causal claim and falsifier; applies bounded
deterministic transforms and a mechanism-aligned fixed `ExitProfile`; checks
verified funding/cost/episode counterfactuals; fits only a small empirical
world model; emits AST-verified content-addressed artifacts; and stores exact
event evidence digests. Candidate progression and Findings analysis are
append-only. `IDLE`, `NO_DATA`, and `NO_STATE_DATA` are non-authorizing, and
discovery cannot edit the registry/configuration/tier or place orders. A
`COMPLETE` discovery result is still research-only; scalar or mixed
scalar/non-episode rows cannot complete a counterfactual. Completion requires
paired contiguous `execution_bar_1m` episodes, the observable's normalization
path, and verified funding (including an explicit
`verified_no_settlement_due` status when no settlement was due).

The recorder's `execution_bar_1m` series is joined after the signal feature
cutoff at a later bounded outcome cutoff. Long/short direction is taken from
the evidence-derived episode identity, not inferred from a scalar result.
Direct timeout is valid only when the full requested bar horizon is present;
missing or partial bars remain `no_data`.

`research.py prepare-discovery-handoff` verifies the complete result and its
typed artifact, then writes an idempotent content-addressed packet under
`research/results/discovery-handoffs/`. Its registration identity is
`HUMAN_DECISION_REQUIRED`; registry, configuration, code, demo, and live
authority are all false, and no mutation occurs. A missing or no-eligible
nightly discovery result is a nonfatal research state.

## Tournament and persistence

Every tournament invocation creates an immutable run directory under
`research/results/tournament/runs/<timestamp>-<run-id>/`, records the run and
its settings/results in the findings store, and writes completion or failure
evidence. The top-level `REPORT.md` and `leaderboard.json` are only a latest
view copied from the newest run; they are not the historical record.

Research evidence tables in schema 17 use append-only rows and immutability
triggers where implemented. That protects against accidental SQL updates or
deletes; it does not make the filesystem undeletable.

`research.py backup` creates a new versioned directory, uses SQLite's online
backup API for live databases, hashes every captured file, verifies SQLite
integrity and foreign keys, and records the attempt. It never prunes an older
backup. Target classifications are:

- `local_default`: repository-local default under `runtime/backups/research`;
- `configured_local`: explicit target without different-device proof;
- `external_mounted`: a pre-existing target whose `st_dev` differs from every
  repository/source device before and after capture.

A configured path is not proof of off-host safety. `--require-external` and
`research.py readiness` require a verified `external_mounted` backup. Real
VM-loss protection therefore requires an operator-provisioned mounted
destination outside this repository and confirmation that the storage is
outside the VM's deletion/failure domain. Different `st_dev` proves a separate
mounted device, not remote retention by itself.

Legacy backup records without positive device evidence migrate conservatively
to `configured_local` in schema 14.

Complete manifest-bearing immutable trees under
`runtime/research/snapshots/` are included in backups. In-progress directories
and directories without a final manifest are excluded; every captured raw
snapshot file is size- and SHA-256-verified.

Heartbeat/status files are append-only bounded redacted JSONL histories.
Verified backups include runtime/account identity, heartbeat and failed-alert
histories, research health/history, the event-plane database and raw recorder
archives/snapshots, findings/journal/results, and discovery artifacts. The
verifier checks checksums, JSON/JSONL, SQLite integrity, and secret
exclusions.

## Commands

```bash
./.venv/bin/python main.py check
./.venv/bin/python main.py strategies --verbose
./.venv/bin/python main.py run
./.venv/bin/python main.py run --candidate-demo \
  --variant-id <qualified-variant-id> --scope-key <scope> \
  --packet-ref t3-packet:<reviewed-packet-hash> \
  --expected-demo-account-fingerprint <okx-demo-fingerprint>
./.venv/bin/python main.py status

./.venv/bin/python research.py corpus stats
./.venv/bin/python research.py readiness
./.venv/bin/python research.py replay --check-fidelity
./.venv/bin/python research.py funnel
./.venv/bin/python research.py cadence
./.venv/bin/python research.py three-arm
./.venv/bin/python research.py sweep research/sweeps/regime_conditioning.yaml
./.venv/bin/python research.py forward-qualify
./.venv/bin/python research.py stage-seed
./.venv/bin/python research.py author
./.venv/bin/python research.py author --dry-run
./.venv/bin/python research.py staged
./.venv/bin/python research.py review-staged --dry-run
./.venv/bin/python research.py qualify-staged
./.venv/bin/python research.py prepare-handoff
./.venv/bin/python research.py shortlist
./.venv/bin/python research.py research-loop --max-reviews 8
./.venv/bin/python research.py research-loop --no-review
./.venv/bin/python research.py prepare-review-artifacts
./.venv/bin/python research.py ingest-recorded
./.venv/bin/python research.py discover
./.venv/bin/python research.py t3-packet --variant <qualified-variant-id>
./.venv/bin/python research.py report
./.venv/bin/python research.py backup
./.venv/bin/python research.py backup --target <mounted-directory> --require-external
./.venv/bin/python research.py verify-backup <backup-directory>
```

### Screening many candidates at once

Test fifty candidates at 5% and two or three will look significant with no
edge at all. `SUPPORTED` therefore requires surviving Benjamini-Hochberg
false-discovery control across every candidate screened alongside it, and the
report states the family size and the adjusted p-value. Holm is used elsewhere
for a handful of pre-registered comparisons; it is far too conservative for a
screening population, where the quantity that matters is the expected
proportion of false discoveries among those declared rather than the chance of
any false positive at all.

Arms too thin to judge do not count toward the family. Including tests that
were never run would make the correction look stricter than the search
actually was.

Candidates move through three stages rather than facing the strictest gate
from the first bar, which is why nothing used to finish:

| Stage | Trades | What can happen |
| --- | --- | --- |
| `SCREEN` | under 30 | Retire a clearly adverse mechanism early; never promote one |
| `MEASURE` | 30-99 | Accumulate with full costs |
| `CONFIRM` | 100+ | Held-out window and family correction decide |

The screen is decisive in one direction only. Passing it means "not yet
excluded", not evidence of an edge, and a borderline loser is given the longer
look because that is exactly where a small sample misleads.

### Reading the shortlist

`research.py shortlist` ranks every measured arm and states how far the
evidence goes. It reports arms with nothing to show as well as the ones with
results, because a mechanism starved of market data is untested rather than
unsupported, and a report that omitted it would read as though it had been
tried and failed.

| Label | What it means |
| --- | --- |
| `SUPPORTED` | Adequate sample, 95% interval excludes zero, held-out window agrees in sign, and survives false-discovery control across everything screened alongside it |
| `PRELIMINARY` | Positive but under the 100-trade floor |
| `INCONCLUSIVE` | Adequate sample whose interval still includes zero, or a result that does not persist out of sample |
| `INSUFFICIENT` | Under the 30-trade floor; no reading means much |
| `NO_EVIDENCE` | No closed trades, with the reason: starved of data, or the contract never fired |
| `NEGATIVE` | The whole interval is at or below zero |

`SUPPORTED` is the strongest statement the system makes and it is not a
recommendation to trade. The confirmation window is the last 30% of a
candidate's trades in time, not a random split, because an edge that only
works where it was found is exactly what that split exists to catch. The
nightly run writes the report as `shortlist.md` under `research/results`,
which is generated output rather than a committed file.

### Proposing new mechanisms

`research.py author` asks the configured model for new candidate mechanisms
and stages the ones that validate. A proposal is data, not code: a mechanism,
the payer, a falsifier, and comparisons over fields the validated forward
models already declare or one bounded deterministic primitive over persisted
market data. Unknown fields, unsupported context (such as cross-sectional
rank without a universe), unsafe operators or ranges, exit/sizing authority,
and claims too thin to name a cause are refused; one bad proposal never
discards the rest of a generation. The prompt also receives bounded summaries
from the persisted findings store, including opportunity rates, conditional
returns, null/near-miss reasons, held-out results, and tested families.

It is deliberately not gated on a terminal outcome. The nightly reviewer needs
a finished assignment to have something to explain, so on a corpus where none
has finished it never runs at all - which is exactly the state in which new
ideas matter most.

`research.py review-staged` gives each staged mechanism a coded verdict and
retires the ones that are finished, so the next generation is told what has
already been tried and why:

| Code | Meaning |
| --- | --- |
| `NEGATIVE_EXPECTANCY` | Fired enough, lost; a nearby threshold on the same fields is the same claim |
| `DIED_OUT_OF_SAMPLE` | Positive while fitting, negative held out |
| `NEVER_FIRED` | Evaluated repeatedly and never triggered; unreachable rather than wrong |
| `STARVED_OF_DATA` | Mostly never evaluated; a pipeline result, so the claim is kept staged |
| `COLLECTING` / `SUPPORTED` | Kept running |

Only the first three retire a mechanism. `STARVED_OF_DATA` never does, because
a claim evaluated on snapshots it could not read has not been tested - the
distinction that made six strategies look falsified for a week. Retirement
frees the lane and keeps the claim, the payer and the reason recorded together,
because a proposer told only that something failed will restate it with a
different number.

`research.py stage-seed` is the other entry point to the same store, for
claims that came out of analysis rather than out of the proposer.
`research/staged/pre-registered.yaml` holds them in version control so the
wording cannot drift after results exist, and the command is idempotent: a
claim already registered is reported and skipped, so it can run on every
deploy. Both paths land in the same table under the same immutability
triggers at the same tier; only the `author` column differs, which is what
keeps a reviewed claim distinguishable from a proposed one. An entry marked
`deferred` is parsed and reported but never registered - a staged contract
that cannot fire is worse than a missing one, because in the evidence it
looks exactly like one that fired and lost.

Staged mechanisms enter at `T1_HYPOTHESIS` and are append-only: a registered
claim cannot be reworded once results exist, which is the difference between a
pre-registered hypothesis and a retro-fitted one. Live still requires
`T3_VALIDATED` and a reviewed content-addressed packet, so nothing here
shortens the path to capital. `research.py staged` lists what is registered
and what each mechanism claims.

`research.findings_store` never falls back to a temporary database. If the
configured path cannot be used, the operation fails.

### Reviewed candidate on OKX demo

`run --candidate-demo` is an explicit operator action for one reviewed variant;
it is not part of the normal demo startup and is never selected automatically
by research or the LLM. The command requires `mode: demo`, a current
non-revoked qualification, a content-addressed `REVIEWED` T3 packet, and a
successful local PAPER stage that is flat, has no open paper trades, meets the
configured closed-trade floor, and has positive finite expectancy. Packet,
artifact, variant, configuration, prompt, provider endpoint, forward-model,
source, and deployment identities must still match the current runtime.

After local authorization, startup verifies the expected demo account, trade
permission, flat positions, every supported regular/algo open-order query, and
flat local runtime state. Unknown or malformed account/order state fails
closed. The reviewed variant is applied only in memory: the command does not
edit `config.yaml`, the strategy registry, or live authorization. A successful
preflight writes a `demo_candidate_authorization` receipt to
`runtime/demo/journal.db` before the trading loop continues. A real OKX demo
run still requires operator-provided demo credentials and external account
availability.

## Optional local historical data

An ignored local `vm-import/` directory may exist on some development
machines. If present, it is optional read-only historical data, is not part of
the clone, and is never required or current. Never configure the trader,
findings store, recorder, tournament, or backup system to use it as a runtime
location or infer an edge from it.

## Maker-first entry boundary

The optional maker-first exchange primitive is enabled in the shipped
demo configuration with a 20-second wait before IOC fallback. It is not the
`scalp-maker` shadow strategy. Configuration validation refuses this path in
live mode; demo execution is explicitly measurement, not promotion.

How it completes: collect and review its demo execution evidence and include
its assumptions in the relevant forward model. Why it waits: the shipped demo
measurement must establish passive-order behaviour before any live-mode
consideration. Why it remains demo-only:
passive-order fill/cancel races require account evidence and are not established
for live capital by the current corpus.

No repository change can by itself provision an off-host mount, install
credentials, or approve capital. Those are explicit environment/operator
actions, not hidden automatic steps.

## Documentation index

This index covers every version-controlled Markdown document in the
repository. Ignored local historical data is not a maintained documentation
source.

### Primary and deployment documentation

| Document | What it contains |
| --- | --- |
| [README.md](README.md) | Current architecture, shipped configuration, evidence boundaries, persistence model, commands, and this documentation index. Start here for the system-wide view. |
| [SETUP.md](SETUP.md) | Local and Azure installation, credentials, configuration, services, external backup provisioning, deployment updates, and optional local-history boundaries. |
| [OPERATIONS.md](OPERATIONS.md) | Daily operation, runtime paths, nightly sequencing and exit codes, experiments, tournaments, backups, recovery, and troubleshooting. |
| [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) | Short compatibility pointer for Azure deployment. It directs operators to setup/operations and lists data that must survive VM loss. |
| [deploy/README.md](deploy/README.md) | Deployment topology: Compose service ownership, durable volumes, safety boundaries, and the legacy systemd lane. Procedures remain in SETUP/OPERATIONS. |

### Strategy and research documentation

| Document | What it contains |
| --- | --- |
| [research/AUTONOMOUS_RESEARCH.md](research/AUTONOMOUS_RESEARCH.md) | Canonical semantic lifecycle, evidence/contract fidelity, bounded authoring/refinement, handoff, scheduler/health behavior, demo review, and human live approval. |
| [research/HYPOTHESES_AND_VARIANTS.md](research/HYPOTHESES_AND_VARIANTS.md) | Canonical definitions of all seven strategies: exact triggers, mechanisms, falsifiers, forward assumptions, evidence status, and settings. It also defines momentum hypotheses and every hand-authored variant. |
| [research/README.md](research/README.md) | Compact guide to real-time experiments, the authoritative journal path, exploratory tournament path, commands, and backup classification. |
| [research/archive/README.md](research/archive/README.md) | Inventory of dated audit exports and retained historical studies, with active-versus-audit boundaries. |
| [research/archive/2026-08-07/audit-data/README.md](research/archive/2026-08-07/audit-data/README.md) | Provenance and limitations for the archived aggregate audit CSVs. |
| [research/legacy/README.md](research/legacy/README.md) | Retained exploratory CLIs, their migrated invocation paths, and deliberate load-bearing deferrals. |
| [research/protocol.md](research/protocol.md) | Statistical and operational evidence rules for `WORKED`, `FAILED`, `INCONCLUSIVE`, qualification, rejection, pairing, held-out confirmation, and multiple testing. |
| [research/plan/RECONCILIATION.md](research/plan/RECONCILIATION.md) | Frozen evidence-policy record retained for historical context; current authority is the canonical lifecycle document. |
| [Frozen maker-first design record](research/plan/maker-first-entry-boundary.md) | Historical design context. Current behavior and completion conditions are in OPERATIONS. |
| [research/plan/edge-platform.md](research/plan/edge-platform.md) | Frozen design record for the earlier edge-platform implementation. |
| [research/plan/autonomous-loop-integration.md](research/plan/autonomous-loop-integration.md) | Frozen integration record for the earlier autonomous-loop merge. |
| [research/plan/order-path-succession.md](research/plan/order-path-succession.md) | Frozen record of an earlier order-path succession decision; the current shipped mode is `shadow_only`. |
| [research/plan/batched-implementation.md](research/plan/batched-implementation.md) | Historical implementation pointer; current flow and commands are in the primary guides. |
| [research/plan/findings.md](research/plan/findings.md) | Historical findings pointer; current evidence boundaries are in RECONCILIATION and protocol. |

### Findings indexes, audits, and scorecards

| Document | What it contains |
| --- | --- |
| [findings/main-repo-review-2026-07-30.md](findings/main-repo-review-2026-07-30.md) | Review snapshot explaining the original gaps and repairs. Retained as an evidence trail; current operation lives in the primary guides. |
| [findings/orchestrated-audit-2026-07-29.md](findings/orchestrated-audit-2026-07-29.md) | Earlier multi-pass audit of research validity, persistence and strategy coherence. Retained as an evidence trail. |
| [findings/README.md](findings/README.md) | Index of repository audits and committed variant scorecards. Use it to navigate identity-specific findings. |
| [findings/ls-ratio-fade/ls_ratio_fade.tuned_70_30_ext_1_5_stop_1_target_3.md](findings/ls-ratio-fade/ls_ratio_fade.tuned_70_30_ext_1_5_stop_1_target_3.md) | Explicit tuned 70/30, 1.5 ATR extension, 1 ATR stop, 3R target identity; unproven, research-only, pinned as an isolated paper arm, and never an adaptive one-axis selector candidate. |
| [findings/momentum/momentum.baseline.md](findings/momentum/momentum.baseline.md) | Identity card for the shipped momentum comparison floor. It records status, immutable claim, sample state, and findings log. |
| [findings/momentum/momentum.rr.fixed_1_5.md](findings/momentum/momentum.rr.fixed_1_5.md) | Scorecard for the 1.5R target candidate. It asks whether improved hit rate outweighs a smaller average win. |
| [findings/momentum/momentum.rr.fixed_2_0.md](findings/momentum/momentum.rr.fixed_2_0.md) | Scorecard for the 2R target candidate. It tests whether more excursions become wins than under the shipped 3R target. |
| [findings/momentum/momentum.rr.fixed_2_5.md](findings/momentum/momentum.rr.fixed_2_5.md) | Scorecard for the immutable 2.5R target identity. Its original registered claim is preserved even though the shipped baseline later changed. |
| [findings/momentum/momentum.rr.fixed_3_0.md](findings/momentum/momentum.rr.fixed_3_0.md) | Superseded 3R scorecard retained for identity history. It now duplicates the shipped baseline and is not an independent arm. |
| [findings/momentum/momentum.stop.atr_1_25.md](findings/momentum/momentum.stop.atr_1_25.md) | Scorecard for a 1.25 ATR stop floor. It tests whether a modest widening filters ordinary market noise. |
| [findings/momentum/momentum.stop.atr_1_5.md](findings/momentum/momentum.stop.atr_1_5.md) | Scorecard for a 1.5 ATR stop floor. It tests whether materially wider invalidation rescues early-but-correct trades. |
| [findings/momentum/momentum.stop.atr_2_0.md](findings/momentum/momentum.stop.atr_2_0.md) | Scorecard for a 2 ATR stop floor. It supplies the wide endpoint needed to test monotonicity. |
| [findings/momentum/momentum.net_direction.60.md](findings/momentum/momentum.net_direction.60.md) | Scorecard for a 60% net-direction cap. It tests whether same-side opportunities are mostly correlated duplicates. |
| [findings/momentum/momentum.net_direction.80.md](findings/momentum/momentum.net_direction.80.md) | Scorecard for an 80% net-direction cap. It tests an intermediate concentration versus opportunity trade-off. |
| [findings/momentum/momentum.net_direction.120.md](findings/momentum/momentum.net_direction.120.md) | Scorecard for a 120% net-direction cap. It tests whether the shipped ceiling suppresses genuinely independent signals. |
| [findings/momentum/momentum.conf.floor_0_50.md](findings/momentum/momentum.conf.floor_0_50.md) | Superseded 0.50 confidence-floor scorecard; proposals below the shipped 0.65 prompt floor are unobservable. |
| [findings/momentum/momentum.conf.floor_0_55.md](findings/momentum/momentum.conf.floor_0_55.md) | Superseded 0.55 confidence-floor scorecard; proposals below the shipped 0.65 prompt floor are unobservable. |
| [findings/momentum/momentum.conf.floor_0_60.md](findings/momentum/momentum.conf.floor_0_60.md) | Superseded 0.60 confidence-floor scorecard; proposals below the shipped 0.65 prompt floor are unobservable. |
| [findings/momentum/momentum.conf.floor_0_70.md](findings/momentum/momentum.conf.floor_0_70.md) | Active scorecard for whether a modestly stricter observable confidence floor adds value over 0.65. |
| [findings/momentum/momentum.conf.floor_0_75.md](findings/momentum/momentum.conf.floor_0_75.md) | Active scorecard for whether a materially stricter observable confidence floor adds value over 0.65. |
| [findings/momentum/momentum.conf.floor_0_80.md](findings/momentum/momentum.conf.floor_0_80.md) | Active scorecard for the strictest registered observable confidence floor. |
| [findings/momentum/momentum.discriminator.trend_alignment.md](findings/momentum/momentum.discriminator.trend_alignment.md) | Scorecard for separating breakouts from continuations using prior trend alignment. |
| [findings/momentum/momentum.discriminator.volatility_regime.md](findings/momentum/momentum.discriminator.volatility_regime.md) | Scorecard for separating breakouts using compression versus expansion rather than trend alignment. |
| [findings/momentum/momentum.cond.vol_regime.md](findings/momentum/momentum.cond.vol_regime.md) | Conditioning-axis scorecard. It partitions existing trades by volatility regime instead of changing a setting, so it consumes no rotation arm. |
| [findings/momentum/momentum.cond.session.md](findings/momentum/momentum.cond.session.md) | Conditioning-axis scorecard. It partitions existing trades by UTC session window and may only be quoted after the out-of-sample split. |
| [findings/momentum/momentum.universe.top_5.md](findings/momentum/momentum.universe.top_5.md) | Scorecard for a five-instrument universe. It asks whether the edge lives in the liquid majors. |
| [findings/momentum/momentum.universe.top_25.md](findings/momentum/momentum.universe.top_25.md) | Scorecard for a twenty-five-instrument universe. It asks the opposite question: whether the edge lives in the tail. |
| [findings/momentum/momentum.universe.top_10.md](findings/momentum/momentum.universe.top_10.md) | Scorecard for the former ten-instrument universe. It asks whether the narrower liquid set beats the shipped 25. |

### Research result snapshots

| Document | What it contains |
| --- | --- |
| [research/results/edge-audit-2024-2026/REPORT.md](research/results/edge-audit-2024-2026/REPORT.md) | Frozen independent audit of the earlier momentum strategy over 24 months. Its [MANIFEST.json](research/results/edge-audit-2024-2026/MANIFEST.json) binds the archived inputs. |
| [research/results/edge-discovery-method/REPORT.md](research/results/edge-discovery-method/REPORT.md) | Methodology for recognizing an edge, ranking research directions, and measuring noise, costs, placebos, and mechanism attribution. |
| [research/results/tournament/REPORT.md](research/results/tournament/REPORT.md) | Committed historical tournament latest-view report. New runs write immutable per-run evidence below `research/results/tournament/runs/`. |

## Tests

```bash
./.venv/bin/python -m pytest -q
```
