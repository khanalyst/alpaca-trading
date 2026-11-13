# Session mechanisms integrated with main research — 13 November 2026

This release imports `session-edges-discovery` through `ee3f592` while retaining
main's existing validation, trader, risk, source-provenance, frozen trial and
proof infrastructure. No existing `agent/` implementation or active research
grammar was changed. Session mechanisms enter through an exact-specification
research review; unsupported runtime/swing lifecycles remain blocked.

## Implemented behavior

1. Historical support requires corrected positive net return **and** alpha.
   Holm/BY receives the larger of their one-sided p-values. The alpha statistic
   includes HAC intercept uncertainty from estimating beta. Missing evidence
   remains p=1 in the denominator. Historical splits carry no adaptive or
   source-independent holdout claim.
2. Daily close, RSI, streak and expiration signals execute at the following
   observed open. Daily and 15:50 minute identities remain distinct. Noise-band
   simulation exits at 15:58, matching shadow timing. Executed entry and exit
   sides are charged separately, including terminal open positions.
3. Minute bars retain observed masks; leading gaps stay unknown. Statistical
   screens refuse incomplete sessions rather than assigning zero returns from
   unavailable future exits. Missing sessions cannot erase an owned position.
   Month-end signals require an explicit independent exchange calendar.
4. An explicit shared v2 ledger binds data, context, code, costs and protocol to
   each named experiment. Changed input requires an explicit new experiment,
   retaining cumulative trial history. Locks, integrity checks and atomic
   writes prevent lost or silently reset evidence. Legacy migration preserves
   the original artifact as diagnostic history and never upgrades it.
5. Main candidate review retains exact spec/kernel/source identities and
   reuses main's structural, matched-control, rolling and sealed-window checks
   only on genuinely supplied trade-level observations. Session return counts
   never become fabricated trades. It does not mutate EdgeLedger, online FDR,
   the frozen trial or authorization state. Every dossier lists the remaining
   main runtime, complete gate and strictly newer parity-shadow requirements.
6. Study books and registered-rule results are diagnostic only. They cannot
   enable paper/live entries. Owned-position recovery retains existing pending
   IDs across ambiguous submissions/restarts, quarantines incompatible broker
   exposure and reduces saved holdings without requiring signal data. It does
   not create a new trading path. Stale, crossed, fallback or untimestamped
   quotes cannot count as measured forward evidence.
7. Model receipts retain exact provider/model, prompt/request/response,
   configuration identities, thesis and failed calls. Equal-budget mechanical
   and model lanes retain failed/rejected/duplicate proposals, freeze all
   selections before paired outcomes and report diagnostic comparisons. No
   provider was called for this release; no model superiority is claimed.

## Research results and evidence boundary

[The saved-artifact reassessment](saved-artifact-audit.json) recalculates the
correction from the original 124 promoted historical rows: nine total-return
Holm survivors, zero alpha Holm and zero alpha BY survivors. All rows were
labelled grid-generated. This does not establish the absence of an effect and
does not replay new timing, fills, prices or costs.

The complete original public/SIP market datasets are not checked into the
repository. A fresh raw-data research run needs those inputs. The current
cloud workspace has neither the VM SSH connection nor its data/credentials;
the Mac's SSH file is outside its filesystem. VM rollout and research on the
VM are therefore unverified until that execution connection is available.
Public data retrieval was also denied by this workspace's network policy.

The last recorded VM/account state remains the dated
[current findings](../current-findings.md). This release makes no new claim
about current broker holdings or service health. Deployment must preserve the
existing operator pause, trial identity, journals, evidence and rollback image;
it must not resume or rebind the trial or enable session-runner entries.

## Validation and rerun

The validation receipt records completed checks and the exact source hashes.
The initial runtime shard passed 1,124 tests. The corrected session tests and
main-candidate checks passed in the focused run; the remaining full-suite
shards and container validation are recorded in the receipt when complete.

```bash
python -m compileall -q agent research deploy studies main.py report.py research.py
python deploy/test_suite.py --shard runtime
python deploy/test_suite.py --shard edge
python deploy/test_suite.py --shard factory
python deploy/test_suite.py --shard research
```

Use [the study workflow](../../studies/session_edges/README.md) for the exact
SIP rerun commands, explicit ledger/experiment flags and main-candidate review.
Use a new persistent output directory and retain the same shared ledger across
experiments. The existing `deploy/update-compose.sh` remains the ordinary
six-service update helper; the optional session shadow service is not enabled
by that helper. Review current VM state and retain verified backup/rollback
evidence before running it.

To inspect an imported candidate against real, bound main-format executions:

```bash
python -m research.session_candidate_review --discovery OUT/discovery.json --executions executions.json --out OUT/main_candidate_review.json
```

Execution records must identify the exact candidate request and execution
kernel, real fills and matched controls. The review refuses different rules,
changed requests and reused/overlapping final sessions. It still returns
`qualified: false`: only main's existing complete proof pipeline can supply
trading authority for a supported runtime contract.
