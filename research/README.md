# Research guide

The current research system has three related but separate paths. Executable
code and `config.yaml` are authoritative; this guide is the compact research
orientation. Installation belongs to [`../SETUP.md`](../SETUP.md), and
operation belongs to [`../OPERATIONS.md`](../OPERATIONS.md).
The canonical authority chain, contract/economics fidelity, bounded authoring,
handoff, scheduler, demo, and live-approval boundaries are in
[`AUTONOMOUS_RESEARCH.md`](AUTONOMOUS_RESEARCH.md).

## Realtime four-lane experiments

Every decision cycle sends the same market snapshot/timestamp to isolated
deterministic paper evaluators for `momentum`, `flush-fade`, `ls-ratio-fade`,
and `scalp-maker`. `funding-carry`, `funding-unwind`, and `trend-multiday` are
registered offline-only models. Only the configured main strategy can reach the
demo exchange.

Each realtime strategy continuously keeps one shared baseline and a bounded
batch of pre-registered candidate settings (four by shipped config, hard cap
eight per lane). Every assignment still tests only one candidate setting. The
four lanes use one snapshot/timestamp and deterministic contract proposals.
Packet computation is bounded and may run concurrently; durable SQLite writes
remain serialized. Candidate batches are isolated per strategy and survive
restart in the schema-16 findings store. The default assignment floor is both
ten elapsed days and 100 comparable paired observations.

At shipped configuration, these four lanes can produce up to twenty
deterministic comparison arms (one baseline plus four candidates per lane).
The hard cap permits up to thirty-six deterministic arms if every lane uses
eight candidates. The shipped deterministic runtime creates no `:llm` sibling.
Analyst mode may retain genuine analyst decisions in a distinct,
non-comparable population; deterministic proposals are never relabeled as
analyst evidence.

The LLM can submit one bounded research-only selection per decision context.
Invalid selections and their reasons persist. Accepted selections queue and
never preempt an active assignment. A nightly run processes a bounded queue of
terminal outcomes (default eight); each research-only review explains one
immutable outcome and may nominate one next registered setting. It cannot alter
the verdict or authorize execution. A review may also preserve one strictly
declarative, non-executable hypothesis draft against allowlisted numeric
snapshot keys from the immutable persisted `llm_input` corpus consumed through
`research.corpus`; those keys need not appear in terminal aggregates. The draft
creates no variant or selection, changes no configuration, tier, portfolio, or
order authority, and must be manually reviewed and registered in a later code
change before research can use it.

The active simulator scope is `forward_feed_version: 8`. Feed v8 retains the
four deterministic realtime lanes and repairs depth-ladder delivery across the
wider 25-instrument universe; the three long-horizon models remain offline-only.
Analyst mode may retain its actual choices in a sibling `:llm` scope for planner
history; the shipped deterministic mode does not create that lane. Such choices
are never pooled with lane comparisons. Feeds v1-v7 remain historical and are
not pooled into feed v8.

Staged mechanisms are a separate population. They arrive either from
`research.py author`, which proposes from what the evidence has killed, or
from `research.py stage-seed`, which registers the hand-written
pre-registrations kept in `research/staged/pre-registered.yaml`. They live in
`research/cache/staging.db`, run in a `:staged` scope with an isolated paper
account plus a paired neutral baseline per mechanism on a single fixed
measurement harness, and receive their own coded
verdicts from `research.py review-staged`: `NEGATIVE_EXPECTANCY`,
`DIED_OUT_OF_SAMPLE` and `NEVER_FIRED` retire a mechanism, while
`STARVED_OF_DATA` never does because a claim evaluated on snapshots it could
not read has not been tested. Retirement keeps the claim, the payer and the
reason together so the next generation is not told merely that something
failed. They enter at `T1_HYPOTHESIS` and are never pooled with the registered
strategies' comparison arms.

Terminal verdicts are `WORKED`, `FAILED`, and `INCONCLUSIVE`. Adequacy is
checked before performance, and every reason/limitation is stored. `WORKED`
creates only a `RESEARCH_ONLY` edge candidate with `promotion_allowed: false`;
current v8 forward qualification is still required and there is no automatic
live promotion.

### Authoring context and contract boundary

The authoring model receives a bounded projection of persisted findings. When
the store contains the relevant fields, the request includes firing rates,
conditional returns, feature distributions, missing-data rates, regime/asset/
time segments, null-model results, near-miss candidates, held-out degradation,
feature correlations, and mechanism families already tested. Missing raw
feature or regime observations are omitted rather than reconstructed or
invented.

The accepted contract language is deterministic and bounded. It supports
lagged values, rolling changes, percentile ranks, volatility and regime
filters, event sequences, bounded feature interactions, order-book imbalance,
and liquidity states. Cross-sectional rank fails closed because a staged
evaluation currently has one symbol row rather than a complete universe. The
authoring model cannot author exits, horizons, stops, targets, sizing, or
network/file operations.

Staged qualification is deliberately two-layered only in its first step: the
signal is screened under the fixed neutral harness (one-ATR minimum structure
stop, fixed 2R target, observed costs, 24-hour timeout). A later
strategy-specific horizon/exit optimization stage is not yet automatically
connected. Support also requires minimum firing and opportunity coverage,
matched baseline evidence, zero-return treatment for eligible declines,
held-out confirmation, and family-level multiple-testing correction.

## Authoritative journal path

The journal contains the snapshots and decision ledger the agent actually
used. The proposal-fidelity replay compares the full canonical pre-risk proposal identity (cycle,
symbol, direction, setup identity/type, signal timestamp, strategy version, and
baseline variant) symmetrically with replay keys and requires a non-vacuous
exact match before downstream authoritative evidence is trusted. Malformed,
duplicate, missing, or extra identities fail closed; outcome-resolution gaps
remain diagnostics rather than proposal mismatches. It does not reproduce full
contract or execution semantics. The current v8
`forward-qualify` path uses
eligible completed assignments and each setting's contemporaneous baseline.
Its paired cluster sign-flip result is valid only under the documented
cluster-delta sign-exchangeability/symmetric-null assumption.

`research.py prepare-review-artifacts` runs after qualification and fails
closed unless persisted edge evidence and every non-manual T3 check validate.
It creates only an immutable/content-addressed draft: it cannot complete manual
review, edit registry/configuration, or enable live trading.

`research.py prepare-handoff` is the earlier boundary for supported staged
mechanisms: it writes a content-addressed, explicitly non-authorizing proposal
for human implementation/registry review. It does not create a reviewed packet,
mutate code, or authorize demo/live orders.

## Exploratory tournament path

The tournament recomputes contracts against an explicitly supplied historical
corpus. It scores every declared setting for strategies with an implemented
historical tournament contract and required inputs; other registered
strategies are retained as `NOT SCORED` rather than silently omitted. It
records every run and failure in schema 16 and writes immutable artifacts beneath
`research/results/tournament/runs/`. Top-level tournament files are latest-view
copies only. The tournament awards no tier above `T2_CANDIDATE` and cannot
promote capital.

Declared settings are loaded from the current strategy YAML rather than a
fixed strategy or row count. A strategy is scored only when its historical
contract and required inputs are available; otherwise every declared setting
is retained and reported `NOT SCORED`. `ls-ratio-fade` and `scalp-maker` use
real-time forward evidence because their required historical inputs are not
reconstructable from candles.

An ignored local `vm-import/` directory, if present, is optional read-only
historical data only. It is not part of the clone, is not required by the
research workflow, and is never a current runtime or findings-store input.

## Commands

```bash
./.venv/bin/python research.py corpus stats
./.venv/bin/python research.py readiness
./.venv/bin/python research.py replay --check-fidelity
./.venv/bin/python research.py funnel
./.venv/bin/python research.py cadence
./.venv/bin/python research.py three-arm
./.venv/bin/python research.py sweep research/sweeps/regime_conditioning.yaml
./.venv/bin/python research.py forward-qualify
./.venv/bin/python research.py author --dry-run
./.venv/bin/python research.py author
./.venv/bin/python research.py stage-seed
./.venv/bin/python research.py staged
./.venv/bin/python research.py review-staged --dry-run
./.venv/bin/python research.py qualify-staged
./.venv/bin/python research.py prepare-handoff
./.venv/bin/python research.py shortlist
./.venv/bin/python research.py research-loop
./.venv/bin/python research.py research-loop --no-review
./.venv/bin/python research.py prepare-review-artifacts
./.venv/bin/python research.py t3-packet --variant <qualified-variant-id>
./.venv/bin/python research.py report
./.venv/bin/python research.py backup
./.venv/bin/python research.py backup --target <mounted-directory> --require-external
./.venv/bin/python research.py verify-backup <backup-directory>

./.venv/bin/python -m research.evidence_cli verify-package \
  <evidence-package> --code-root . --config config.yaml
./.venv/bin/python -m research.evidence_cli verify-golden \
  tests/fixtures/evidence/golden_replay_synthetic.json \
  tests/fixtures/evidence/golden_replay_expected.json
```

Backups are versioned, checksummed, immediately verified, and never pruned by
the application. `local_default` and `configured_local` are not VM-loss
protection. `external_mounted` requires positive different-`st_dev` evidence;
the mount must be provisioned outside this repository. Complete
manifest-bearing immutable snapshot trees are included; incomplete or
in-progress snapshot directories are excluded.

See [../OPERATIONS.md](../OPERATIONS.md) for the exact VM workflow and exit
codes, [HYPOTHESES_AND_VARIANTS.md](HYPOTHESES_AND_VARIANTS.md) for identity
and current counts, and [protocol.md](protocol.md) for evidence rules.

Committed reports under `research/results/` are historical evidence snapshots
unless their own run metadata says otherwise. Historical conclusions remain
useful, but they do not describe the current executable pipeline.
