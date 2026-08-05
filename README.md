# OKX AI Trading Agent

This repository runs one demo-first OKX perpetuals strategy and a separate
research system for seven registered mechanism models. Four realtime lanes use
the shared live market feed; `funding-carry`, `funding-unwind`, and
`trend-multiday` are offline-only. The shipped account mode is `demo`; no
strategy is currently eligible for live capital.

Use [SETUP.md](SETUP.md) for installation and VM deployment, and
[OPERATIONS.md](OPERATIONS.md) for daily operation, research, backups, and
recovery.

Documentation authority is deliberately short: executable code and
`config.yaml` define behaviour; `SETUP.md` is the installation/deployment
authority; `OPERATIONS.md` is the runbook; the research documents define
evidence rules and strategy identities. Files under `research/plan/` are
compatibility or safety records, not a second implementation plan. When prose
and executable behaviour differ, report the executable behaviour narrowly.

## Shipped configuration

These values come from the current `config.yaml` and registry:

```yaml
research:
  findings_store: research/cache/findings.db
  experiment_min_duration_days: 10
  experiment_min_observations: 100
```

| Area | Current value |
| --- | --- |
| Account mode | `demo` (OKX Demo Trading) |
| Order-executing strategy | `momentum/phase1-v3` |
| Runtime tier | `T0_REJECTED`; demo rehearsal and comparison baseline only |
| LLM route | provider `openai`, model/deployment identifier `gpt-5.6-sol-coding` |
| Housekeeping cadence | `cycle.interval_seconds: 60` |
| Decision throttle | `cycle.decision_interval_seconds: 300` elapsed seconds (with a 95% jitter tolerance); safety/mark cycles stay faster |
| Journal | `runtime/demo/journal.db` in the shipped mode |
| Findings store | `research/cache/findings.db`, SQLite schema 16 |
| Research feed | `forward_feed_version: 7`; feed v7 adds the real liquidation flow and the pre-registered conditioning axes; feeds v1-v6 remain historical (v4 is the market-data plumbing repair feed, v5 the immutable-provenance fork, and v6 the deterministic four-lane realtime fork) |
| Realtime comparison arms | 8 deterministic arms: one baseline and at most one candidate for each of 4 realtime lanes; the separate `:llm` sibling adds 2 non-comparable arms, for a runtime maximum of 10 when present |
| Shadow workers | `2`; the four realtime lanes advance on the same cycle snapshot |
| Experiment floor | both 10 elapsed days and 100 comparable paired observations |
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
4. The `StrategyShadowCoordinator` gives the same snapshot/timestamp to four
   isolated deterministic realtime evaluators: `momentum`, `flush-fade`,
   `ls-ratio-fade`, and `scalp-maker`. The three long-horizon models are
   registered for offline research and do not occupy realtime lanes.
5. Each evaluator owns its own paper cash, positions, exposure, cooldowns,
   circuit breakers, decisions, and trades. `shadow_decision` rows include
   accepted actions and policy vetoes as explicit zero-return actions.
6. Every realtime strategy keeps its stable baseline running and tests at most
   one candidate setting at a time. The four deterministic realtime lanes are
   logically isolated over the same frozen input, but the coordinator
   intentionally evaluates them in a bounded sequence and serializes durable
   writes rather than creating four simultaneous SQLite writers. Physical
   wall-clock concurrency is not a
   correctness requirement. Settings rotate serially within each strategy and
   survive process restarts.

The deterministic comparison set is therefore eight arms. The active analyst's
separate `:llm` scope may also hold its own baseline and candidate, adding two
non-comparable arms; when that sibling is present, the runtime maximum is ten.

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
promotion. It is an immutable research lead, and current v7 forward
qualification is still required.

The current `forward-qualify` path reconstructs v6 evidence from eligible
completed assignments and each setting's contemporaneous baseline. It proves
one declared axis from the immutable decision ledger, including the non-axis executable
configuration, held-out confirmation, and family correction. Its
paired cluster sign-flip test is valid only under the persisted
cluster-delta sign-exchangeability/symmetric-null assumption; it is not an
assumption-free p-value.

Feed v6 is a clean fork in which the four realtime lanes use deterministic
contract proposals; the three long-horizon models remain offline-only. The
analyst's own decisions remain in a separate `:llm` scope and are never pooled
with lane evidence. Feeds v1-v5 remain historical and are never migrated or
pooled; feed v4 remains the market-data plumbing repair feed and v5 the
immutable-provenance fork. `research.py
prepare-review-artifacts` runs only after v7 qualification. It
fails closed unless the saved edge evidence and every non-manual T3 check
validate, then idempotently creates an immutable, content-addressed draft
review artifact. It cannot complete manual review, edit the registry or
configuration, switch strategies, or enable live trading. A reviewed T3 packet
and any registry/configuration change remain explicit human actions.

## Evidence paths

- The recorded journal path is authoritative only after a current G2 replay
  PASS. G2 compares the full canonical pre-risk proposal identity (including
  cycle, symbol, direction, setup identity/type, signal timestamp, strategy
  version, and baseline variant) symmetrically with replay keys. It requires a
  non-vacuous exact match; malformed, duplicate, missing, or extra identities
  fail closed. Outcome-resolution gaps remain explicit diagnostics and are not
  scored as proposal mismatches. A failed, stale, or vacuous G2 blocks
  downstream evidence.
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

The available exit policies are `fixed_rr`, `extended_rr` and
`carry_until_normalised`. The last one belongs to `funding-carry`, whose return
source is the funding it collects rather than a directional forecast: it holds
while funding pays and closes when the 30-day funding percentile falls back to
its median, instead of on a price target it never claimed to be forecasting.
Its stop is unchanged, because price risk over the holding window still has to
be charged against the carry.

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

## Optional local historical data

An ignored local `vm-import/` directory may exist on some development
machines. If present, it is optional read-only historical data, is not part of
the clone, and is never required or current. Never configure the trader,
findings store, recorder, tournament, or backup system to use it as a runtime
location or infer an edge from it.

## B7.5 and current boundaries

The optional maker-first exchange primitive (B7.5) is enabled in the shipped
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
| [research/HYPOTHESES_AND_VARIANTS.md](research/HYPOTHESES_AND_VARIANTS.md) | Canonical definitions of all seven strategies: exact triggers, mechanisms, falsifiers, forward assumptions, evidence status, and settings. It also defines momentum hypotheses and every hand-authored variant. |
| [research/README.md](research/README.md) | Compact guide to real-time experiments, the authoritative journal path, exploratory tournament path, commands, and backup classification. |
| [research/protocol.md](research/protocol.md) | Statistical and operational evidence rules for `WORKED`, `FAILED`, `INCONCLUSIVE`, qualification, rejection, pairing, held-out confirmation, and multiple testing. |
| [research/plan/RECONCILIATION.md](research/plan/RECONCILIATION.md) | Current authority policy separating journal evidence, exploratory tournament evidence, configuration exceptions, and VM handoff requirements. |
| [research/plan/B7.5-record.md](research/plan/B7.5-record.md) | Current status and completion conditions for the optional maker-first order primitive. It distinguishes that execution experiment from `scalp-maker`. |
| [research/plan/autonomous-loop-integration.md](research/plan/autonomous-loop-integration.md) | Batched plan and checklist for merging the G2/promotion and audit-remediation work into one unattended loop. Records why runtime code settles before collection opens. |
| [research/plan/batched-implementation.md](research/plan/batched-implementation.md) | Historical implementation pointer; current flow and commands are in the primary guides. |
| [research/plan/findings.md](research/plan/findings.md) | Historical findings pointer; current evidence boundaries are in RECONCILIATION and protocol. |

### Findings indexes, audits, and scorecards

| Document | What it contains |
| --- | --- |
| [findings/main-repo-review-2026-07-30.md](findings/main-repo-review-2026-07-30.md) | Review snapshot explaining the original gaps and repairs. Retained as an evidence trail; current operation lives in the primary guides. |
| [findings/orchestrated-audit-2026-07-29.md](findings/orchestrated-audit-2026-07-29.md) | Earlier multi-pass audit of research validity, persistence and strategy coherence. Retained as an evidence trail. |
| [findings/README.md](findings/README.md) | Index of repository audits and committed momentum variant scorecards. Use it to navigate identity-specific findings. |
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

### Research result snapshots

| Document | What it contains |
| --- | --- |
| [research/results/edge-audit-2024-2026/REPORT.md](research/results/edge-audit-2024-2026/REPORT.md) | Independent audit of the earlier momentum strategy over 24 months. It establishes the rejected benchmark and the evidence standard new strategies must beat. |
| [research/results/edge-discovery-method/REPORT.md](research/results/edge-discovery-method/REPORT.md) | Methodology for recognizing an edge, ranking research directions, and measuring noise, costs, placebos, and mechanism attribution. |
| [research/results/tournament/REPORT.md](research/results/tournament/REPORT.md) | Committed historical tournament latest-view report. New runs write immutable per-run evidence below `research/results/tournament/runs/`. |

## Tests

```bash
./.venv/bin/python -m pytest -q
``
