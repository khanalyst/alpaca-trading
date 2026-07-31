# Research guide

Research has two deliberately separate evidence paths.

## Authoritative journal path

The agent records the snapshots and decision ledger it actually used. Replay
and G2 test whether the deterministic contract reproduces those decisions.
Only after a current G2 PASS may the authoritative funnel, cadence, sweep,
forward qualification, or T3 packet be trusted for promotion review.

## Exploratory tournament path

The tournament recomputes strategy contracts against an explicitly supplied
OHLCV corpus. It runs the pre-registered `settings:` for each backtestable
strategy, records the setting identity and multiplicity, and awards no tier
above `T2_CANDIDATE`. It is useful for rejecting or prioritising research, not
for silently changing the live registry.

The VM's live corpus is not stored in Git. The export under
`vm-import/2026-07-30/` is a project test fixture only. Preserve the corpus
manifest, time window, code/config fingerprints, and findings DB backup when
copying data from the VM.

## Current commands

```bash
./.venv/bin/python research.py corpus stats
./.venv/bin/python research.py readiness
./.venv/bin/python research.py replay --check-fidelity
./.venv/bin/python research.py funnel
./.venv/bin/python research.py cadence
./.venv/bin/python research.py three-arm
./.venv/bin/python research.py sweep research/sweeps/regime_conditioning.yaml
./.venv/bin/python research.py forward-qualify
./.venv/bin/python research.py t3-packet --variant <qualified-variant-id>
./.venv/bin/python research.py report
```

For the exact VM and fixture tournament commands, read
[`../OPERATIONS.md`](../OPERATIONS.md). For the identity and values of current
hypotheses and variants, read
[`HYPOTHESES_AND_VARIANTS.md`](HYPOTHESES_AND_VARIANTS.md). The protocol and
thresholds are in [`protocol.md`](protocol.md); committed result reports are
historical until rerun against an approved corpus.

The detailed pre-registration rationale for the H-G through H-L hypotheses is
preserved in [`plan/edge-hypotheses.md`](plan/edge-hypotheses.md). It is a
historical rationale record; the current status is the current hypothesis
index and the root implementation plan.

## Current evidence status

The runtime is `momentum/phase1-v3`, retained as a benchmark/null and not live
eligible. Runtime hypotheses are collected through isolated shadow variants;
the LLM proposes only bounded registered numeric settings, and every proposal
and failed attempt remains in the findings history. A qualification event
stores the exact variant and evidence window, starts local PAPER only when
flat, and does not edit `agent/registry.py` or `config.yaml`.

The current implementation plan and remaining R1–R4 work are in
[`../MAIN_REPO_REVIEW_PLAN.md`](../MAIN_REPO_REVIEW_PLAN.md). Reconciliation
policy is kept in [`plan/RECONCILIATION.md`](plan/RECONCILIATION.md) as a short
current policy, not as a historical implementation plan.
