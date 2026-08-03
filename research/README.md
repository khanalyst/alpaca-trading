# Research guide

The current research system has three related but separate paths.

## Real-time seven-strategy experiments

Every decision cycle sends the same market snapshot/timestamp to isolated
paper evaluators for `momentum`, `flush-fade`, `funding-carry`,
`funding-unwind`, `trend-multiday`, `ls-ratio-fade`, and `scalp-maker`.
Only the configured main strategy can reach the demo exchange.

Each strategy continuously keeps its baseline and at most one candidate
setting. All seven lanes use one snapshot/timestamp and are logically isolated,
but the coordinator intentionally evaluates them in a bounded sequence and
serializes durable writes rather than creating seven simultaneous SQLite
writers. Candidate rotation is serial per strategy. The default assignment
floor is both three elapsed days and 100 comparable paired observations. State
survives restart in the schema-16 findings store.

The LLM can submit one bounded research-only selection. Invalid selections and
their reasons persist. Accepted selections queue and never preempt an active
assignment. A separate nightly research prompt explains one immutable terminal
outcome and may nominate one next registered setting; it cannot alter the
verdict or authorize execution.

Terminal verdicts are `WORKED`, `FAILED`, and `INCONCLUSIVE`. Adequacy is
checked before performance, and every reason/limitation is stored. `WORKED`
creates only a `RESEARCH_ONLY` edge candidate with `promotion_allowed: false`;
current v4 forward qualification is still required and there is no automatic
live promotion.

## Authoritative journal path

The journal contains the snapshots and decision ledger the agent actually
used. Replay/G2 must reproduce the recorded decisions before downstream
authoritative evidence is trusted. The current v4 `forward-qualify` path uses
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
