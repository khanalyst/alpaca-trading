# Research guide

The current research system has three related but separate paths. Executable
code and `config.yaml` are authoritative; this guide is the compact research
orientation. Installation belongs to [`../SETUP.md`](../SETUP.md), and
operation belongs to [`../OPERATIONS.md`](../OPERATIONS.md).

## Realtime four-lane experiments

Every decision cycle sends the same market snapshot/timestamp to isolated
deterministic paper evaluators for `momentum`, `flush-fade`, `ls-ratio-fade`,
and `scalp-maker`. `funding-carry`, `funding-unwind`, and `trend-multiday` are
registered offline-only models. Only the configured main strategy can reach the
demo exchange.

Each realtime strategy continuously keeps its baseline and at most one candidate
setting. The four lanes use one snapshot/timestamp and deterministic contract
proposals. They are logically isolated, but the coordinator intentionally
evaluates them in a bounded sequence and serializes durable writes rather than
creating four simultaneous SQLite writers. Candidate rotation is serial per
strategy. The default assignment floor is both ten elapsed days and 100
comparable paired observations. State survives restart in the schema-16
findings store.

These four lanes produce eight deterministic comparison arms. The separate
`:llm` sibling may hold its own baseline and candidate, adding two
non-comparable arms for a runtime maximum of ten when present.

The LLM can submit one bounded research-only selection. Invalid selections and
their reasons persist. Accepted selections queue and never preempt an active
assignment. A separate nightly research prompt explains one immutable terminal
outcome and may nominate one next registered setting; it cannot alter the
verdict or authorize execution. That same single review may preserve one
strictly declarative, non-executable hypothesis draft against allowlisted
numeric snapshot keys from the immutable persisted `llm_input` corpus consumed
through `research.corpus`; those keys need not appear in terminal aggregates.
The draft creates no variant or selection, changes no configuration, tier,
portfolio, or order authority, and must be manually reviewed and registered in
a later code change before research can use it.

The active simulator scope is `forward_feed_version: 8`. Feed v8 makes the
four realtime lanes deterministic; the three long-horizon models remain
offline-only. The active analyst's actual choices remain in a sibling `:llm`
scope for planner history and are never pooled with lane comparisons. Feeds
v1-v7 remain historical (v4 is the market-data plumbing repair feed and v5 the
immutable-provenance fork).

Staged mechanisms are a separate population. They arrive either from
`research.py author`, which proposes from what the evidence has killed, or
from `research.py stage-seed`, which registers the hand-written
pre-registrations kept in `research/staged/pre-registered.yaml`. They live in
`research/cache/staging.db`, run in a `:staged` scope with one paper account
each on a single fixed measurement harness, and receive their own coded
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

## Authoritative journal path

The journal contains the snapshots and decision ledger the agent actually
used. Replay/G2 compares the full canonical pre-risk proposal identity (cycle,
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
./.venv/bin/python research.py research-loop
./.venv/bin/python research.py research-loop --no-review
./.venv/bin/python research.py prepare-review-artifacts
./.venv/bin/python research.py t3-packet --variant <qualified-variant-id>
./.venv/bin/python research.py report
./.venv/bin/python research.py backup
./.venv/bin/python research.py backup --target <mounted-directory> --require-external
./.venv/bin/python research.py verify-backup <backup-directory>
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
