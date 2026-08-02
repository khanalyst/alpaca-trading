# OKX AI Trading Agent

This repository runs one demo-first OKX perpetuals strategy and a separate
research system that evaluates every registered strategy from the same live
market feed. The shipped account mode is `demo`; no strategy is currently
eligible for live capital.

Use [SETUP.md](SETUP.md) for installation and VM deployment, and
[OPERATIONS.md](OPERATIONS.md) for daily operation, research, backups, and
recovery.

## Shipped configuration

These values come from the current `config.yaml` and registry:

```yaml
research:
  findings_store: research/cache/findings.db
  experiment_min_duration_days: 3
  experiment_min_observations: 100
```

| Area | Current value |
| --- | --- |
| Account mode | `demo` (OKX Demo Trading) |
| Order-executing strategy | `momentum/phase1-v3` |
| Runtime tier | `T0_REJECTED`; demo rehearsal and comparison baseline only |
| LLM route | provider `openai`, model/deployment identifier `gpt-5.6-sol-coding` |
| Housekeeping cadence | `cycle.interval_seconds: 60` |
| Decision cadence | `cycle.decision_interval_seconds: 300`; safety/mark cycles stay faster |
| Journal | `runtime/demo/journal.db` in the shipped mode |
| Findings store | `research/cache/findings.db`, SQLite schema 16 |
| Research feed | `forward_feed_version: 2`; prior simulator evidence remains isolated |
| Active research arms | 14: one baseline and at most one candidate for each of 7 strategies |
| Shadow workers | `2`; all seven strategies still advance on the same cycle snapshot |
| Experiment floor | both 3 elapsed days and 100 comparable paired observations |
| Local paper balance | 10,000 USDT per isolated strategy/variant account |

The repository contains no credentials and does not claim that credentials are
installed. Secrets belong in `.env`. Never enable Withdraw on an API key.

## End-to-end behavior

One strategy is allowed to reach the configured demo account. The shadow and
research paths have no exchange object and cannot place, amend, or cancel an
order.

On each eligible decision cycle:

1. The engine records one market snapshot and timestamp. `book_state` and
   `snapshot_enrichment` preserve the market inputs needed by research.
2. The active momentum analyst makes at most one live-path LLM call. The parsed
   decisions, optional bounded hypothesis proposal, and optional
   `research_selection` are recorded.
3. The order path applies deterministic contracts, risk rules, and execution
   controls to the configured main strategy only.
4. The `StrategyShadowCoordinator` gives the same snapshot/timestamp to seven
   isolated evaluators: `momentum`, `flush-fade`, `funding-carry`,
   `funding-unwind`, `trend-multiday`, `ls-ratio-fade`, and `scalp-maker`.
5. Each evaluator owns its own paper cash, positions, exposure, cooldowns,
   circuit breakers, decisions, and trades. `shadow_decision` rows include
   accepted actions and policy vetoes as explicit zero-return actions.
6. Every strategy keeps its stable baseline running and tests at most one
   candidate setting at a time. The seven lanes are logically isolated over
   the same frozen input, but the coordinator intentionally evaluates them in
   a bounded sequence and serializes durable writes rather than creating seven
   simultaneous SQLite writers. Physical wall-clock concurrency is not a
   correctness requirement. Settings rotate serially within each strategy and
   survive process restarts.

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

## Experiment outcomes and edge evidence

Assignment completion requires both configured rotation floors: by default,
three elapsed calendar days and 100 comparable paired observations. Completion
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
promotion. It is an immutable research lead, and current v3 forward
qualification is still required.

The current `forward-qualify` path reconstructs v3 evidence from eligible
completed assignments and each setting's contemporaneous baseline. It proves
one declared axis from the immutable decision ledger, including the non-axis executable
configuration, held-out confirmation, and family correction. Its
paired cluster sign-flip test is valid only under the persisted
cluster-delta sign-exchangeability/symmetric-null assumption; it is not an
assumption-free p-value.

`research.py prepare-review-artifacts` runs only after v3 qualification. It
fails closed unless the saved edge evidence and every non-manual T3 check
validate, then idempotently creates an immutable, content-addressed draft
review artifact. It cannot complete manual review, edit the registry or
configuration, switch strategies, or enable live trading. A reviewed T3 packet
and any registry/configuration change remain explicit human actions.

## Evidence paths

- The recorded journal path is authoritative only after a current G2 replay
  PASS. G2 is a stop when replay cannot reproduce the recorded decisions.
  `INSUFFICIENT_SAMPLE` means collection is open, not that an edge failed.
- The OHLCV tournament is exploratory. It awards no tier above
  `T2_CANDIDATE`; an existing higher registered tier is reported as unrevised
  rather than as demoted. Tournament results cannot promote live capital.

The v6→v7 migration introduced the complete immutable decision ledger and its
legacy watermark. Schema migration 7 therefore remains relevant to evidence
validity even though the current findings store schema is 16.

Executable fingerprints cover the LLM provider/model, prompt, strategy and
configuration, code, universe selection, and decision cadence; credentials are
excluded. Known replay limits include exchange-only fill races, `loss cooldown`
state, and `select_universe` feedback that cannot be reconstructed exactly.
Operational failures invalidate a forward window instead of disappearing as
missing data. PAPER is local simulation only.

The available exit policies are `fixed_rr` and `extended_rr`.

## Tournament and persistence

Every tournament invocation creates an immutable run directory under
`research/results/tournament/runs/<timestamp>-<run-id>/`, records the run and
its settings/results in the findings store, and writes completion or failure
evidence. The top-level `REPORT.md` and `leaderboard.json` are only a latest
view copied from the newest run; they are not the historical record.

Research evidence tables in schema 16 use append-only rows and immutability
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

## Commands

```bash
./.venv/bin/python main.py check
./.venv/bin/python main.py strategies --verbose
./.venv/bin/python main.py run
./.venv/bin/python main.py status

./.venv/bin/python research.py corpus stats
./.venv/bin/python research.py readiness
./.venv/bin/python research.py replay --check-fidelity
./.venv/bin/python research.py funnel
./.venv/bin/python research.py cadence
./.venv/bin/python research.py three-arm
./.venv/bin/python research.py sweep research/sweeps/regime_conditioning.yaml
./.venv/bin/python research.py forward-qualify
./.venv/bin/python research.py research-loop
./.venv/bin/python research.py research-loop --no-review
./.venv/bin/python research.py prepare-review-artifacts
./.venv/bin/python research.py t3-packet --variant <qualified-variant-id>
./.venv/bin/python research.py report
./.venv/bin/python research.py backup
./.venv/bin/python research.py backup --target <mounted-directory> --require-external
./.venv/bin/python research.py verify-backup <backup-directory>
```

`research.findings_store` never falls back to a temporary database. If the
configured path cannot be used, the operation fails.

## One-time VM fixture

`vm-import/2026-07-30/` is a read-only, one-time evidence export supplied for
development and tests. Copy its databases or extract its archive into a
temporary directory before using them. Never configure the trader, findings
store, recorder, tournament, or backup system to use this fixture as a default
runtime location. Its 3,520 legacy shadow decisions have no current stored
outcomes or current provenance manifest, so they remain audit history and are
rejected for promotion. Do not infer or manufacture an edge from them.

## B7.5 and current boundaries

The optional maker-first exchange primitive (B7.5) exists but the shipped
configuration omits `execution.maker_first_enabled`, so validation supplies
the safe default `false`; `maker_first_wait_seconds` defaults to 20 seconds.
It is not the `scalp-maker` shadow strategy and is not active on the demo order
path.

How it completes: enable it deliberately in demo, collect and review its
execution evidence, and include its assumptions in the relevant forward model.
Why it waits: passive-order fill/cancel races require account evidence and are
not established by the current corpus.

No repository change can by itself provision an off-host mount, install
credentials, or approve capital. Those are explicit environment/operator
actions, not hidden automatic steps.

## Documentation index

This index covers every version-controlled Markdown document in the
repository. The one-time `vm-import/` fixture is excluded because it is
external evidence, not a maintained documentation source.

### Primary and deployment documentation

| Document | What it contains |
| --- | --- |
| [README.md](README.md) | Current architecture, shipped configuration, evidence boundaries, persistence model, commands, and this documentation index. Start here for the system-wide view. |
| [SETUP.md](SETUP.md) | Local and Azure installation, credentials, configuration, services, external backup provisioning, deployment updates, and safe VM-import handling. |
| [OPERATIONS.md](OPERATIONS.md) | Daily operation, runtime paths, nightly sequencing and exit codes, experiments, tournaments, backups, recovery, and troubleshooting. |
| [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) | Short compatibility pointer for Azure deployment. It directs operators to setup/operations and lists data that must survive VM loss. |
| [MAIN_REPO_REVIEW_PLAN.md](MAIN_REPO_REVIEW_PLAN.md) | Reconciled closure record for the seven implementation topics and remaining environment-only actions. It is a status summary, not a second runbook. |
| [V2_HARDENING_PLAN.md](V2_HARDENING_PLAN.md) | Concise record of the hardened foundations now present and the safety boundaries that remain intentional. |
| [WIP_HANDOFF.md](WIP_HANDOFF.md) | Final implementation handoff, assessment reconciliation, validation record, and remaining environment/operator prerequisites. |
| [deploy/README.md](deploy/README.md) | Purpose and ordering of the systemd trader, recorder, research service, and timer units. It also points to external-backup configuration. |

### Strategy and research documentation

| Document | What it contains |
| --- | --- |
| [research/HYPOTHESES_AND_VARIANTS.md](research/HYPOTHESES_AND_VARIANTS.md) | Canonical definitions of all seven strategies: exact triggers, mechanisms, falsifiers, forward assumptions, evidence status, and settings. It also defines momentum hypotheses and every hand-authored variant. |
| [research/README.md](research/README.md) | Compact guide to real-time experiments, the authoritative journal path, exploratory tournament path, commands, and backup classification. |
| [research/protocol.md](research/protocol.md) | Statistical and operational evidence rules for `WORKED`, `FAILED`, `INCONCLUSIVE`, qualification, rejection, pairing, held-out confirmation, and multiple testing. |
| [research/plan/RECONCILIATION.md](research/plan/RECONCILIATION.md) | Current authority policy separating journal evidence, exploratory tournament evidence, configuration exceptions, and VM handoff requirements. |
| [research/plan/B7.5-record.md](research/plan/B7.5-record.md) | Current status and completion conditions for the optional maker-first order primitive. It distinguishes that execution experiment from `scalp-maker`. |
| [research/plan/edge-hypotheses.md](research/plan/edge-hypotheses.md) | Compatibility pointer to the canonical strategy definitions. It maps non-strategy research concepts to their descriptive implementation artifacts. |
| [research/plan/batched-implementation.md](research/plan/batched-implementation.md) | Reconciled implementation-flow summary showing how snapshots become isolated experiments, outcomes, edge evidence, tournaments, and backups. |
| [research/plan/findings.md](research/plan/findings.md) | Pointer from the earlier findings plan to current implementation and evidence documents. It preserves context without acting as current instructions. |

### Findings indexes, audits, and scorecards

| Document | What it contains |
| --- | --- |
| [findings/README.md](findings/README.md) | Index of repository audits and committed momentum variant scorecards. Use it to navigate identity-specific findings. |
| [findings/main-repo-review-2026-07-30.md](findings/main-repo-review-2026-07-30.md) | Detailed review snapshot explaining the original gaps, repairs, and reasoning behind the current reconciliation plan. Current operation remains in the primary guides. |
| [findings/orchestrated-audit-2026-07-29.md](findings/orchestrated-audit-2026-07-29.md) | Earlier multi-pass audit of research validity, reporting, persistence, strategy coherence, and documentation. It is retained as an evidence trail. |
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
| [findings/momentum/momentum.conf.floor_0_50.md](findings/momentum/momentum.conf.floor_0_50.md) | Scorecard for a 0.50 confidence floor. It asks whether the shipped floor excludes profitable lower-confidence proposals. |
| [findings/momentum/momentum.conf.floor_0_55.md](findings/momentum/momentum.conf.floor_0_55.md) | Scorecard for a 0.55 confidence floor. It tests a middle point between sample recovery and proposal quality. |
| [findings/momentum/momentum.conf.floor_0_60.md](findings/momentum/momentum.conf.floor_0_60.md) | Scorecard for a 0.60 confidence floor. It tests a small relaxation from the shipped threshold. |
| [findings/momentum/momentum.discriminator.trend_alignment.md](findings/momentum/momentum.discriminator.trend_alignment.md) | Scorecard for separating breakouts from continuations using prior trend alignment. |
| [findings/momentum/momentum.discriminator.volatility_regime.md](findings/momentum/momentum.discriminator.volatility_regime.md) | Scorecard for separating breakouts using compression versus expansion rather than trend alignment. |

### Research result snapshots

| Document | What it contains |
| --- | --- |
| [research/results/edge-audit-2024-2026/REPORT.md](research/results/edge-audit-2024-2026/REPORT.md) | Independent audit of the earlier momentum strategy over 24 months. It establishes the rejected benchmark and the evidence standard new strategies must beat. |
| [research/results/edge-audit-2024-2026/crosscheck-legacy-backtest-REPORT.md](research/results/edge-audit-2024-2026/crosscheck-legacy-backtest-REPORT.md) | Deterministic legacy cross-check of momentum entries and portfolio behavior. It is supporting evidence with explicitly documented limitations. |
| [research/results/edge-discovery-method/REPORT.md](research/results/edge-discovery-method/REPORT.md) | Methodology for recognizing an edge, ranking research directions, and measuring noise, costs, placebos, and mechanism attribution. |
| [research/results/edge-search-2024-2026/REPORT.md](research/results/edge-search-2024-2026/REPORT.md) | Historical multi-pass edge search and adversarial validation results. It records what was tested rather than granting current execution authority. |
| [research/results/phase1-v2-backtest-2025-2026/REPORT.md](research/results/phase1-v2-backtest-2025-2026/REPORT.md) | Earlier phase1-v2 backtest snapshot used for comparison and audit continuity. It does not define the current runtime strategy. |
| [research/results/selection-and-management/REPORT.md](research/results/selection-and-management/REPORT.md) | Historical analysis of universe selection and position management choices, including measured trade-offs and limitations. |
| [research/results/tournament/REPORT.md](research/results/tournament/REPORT.md) | Committed historical tournament latest-view report. New runs write immutable per-run evidence below `research/results/tournament/runs/`. |

## Tests

```bash
./.venv/bin/python -m pytest -q
``
