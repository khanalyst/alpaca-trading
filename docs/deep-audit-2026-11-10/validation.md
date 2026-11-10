# Audit validation — 10 November 2026

Base: `21d97293455b42ac2ef7298bf814b09b68680200`. The repair checkout is
`codex/full-audit-20261110`; the older dirty working copy is preserved.
Tests use the existing project virtual environment. These checks establish
bounded correctness and compatibility, not strategy profitability or release
readiness. The remaining safety and data blockers are in [the audit](README.md).

## Fresh baseline execution

The existing 2,675-test inventory was run in four unittest shards before
production repairs. These are newly executed results, distinct from the
historical October 28 test claim.

| shard | tests | outcome | local log |
| --- | ---: | --- | --- |
| Runtime | 1,036 | Two recorder test failures: the host had less than the production 5 GiB reserve, so storage refusal occurred before their unrelated mocked request failures. | `outputs/deep-audit/runtime-baseline-tests.log` |
| Research | 1,570 | Passed. | `outputs/deep-audit/research-baseline-tests.log` |
| Edge discovery | 62 | Passed. | `outputs/deep-audit/edge-baseline-tests.log` |
| Factory integration | 7 | Passed. | `outputs/deep-audit/factory-baseline-tests.log` |

The two recorder fixtures now mock sufficient disk capacity only in the
unrelated failure scenarios. The real reserve and explicit storage-failure
tests are unchanged. All 16 recorder-measurement tests passed afterward.

## Repairs and targeted checks

The full runtime shard subsequently passed **1,048 tests** in 408.322 seconds
(`outputs/deep-audit/runtime-final-tests.log`). This run preceded the final
independent-review session-close boundary hardening; the post-hardening
strategy/exit checks are recorded separately below.

The complete runtime shard was then rerun after that hardening and passed
**1,053 tests** in 430.622 seconds with no failures
(`outputs/deep-audit/runtime-post-review-tests.log`). It uses unittest
discovery, flattened to test cases and excluding `tests.research.*`.

After hardening, the executor's strategy/zero-parameter/lifecycle run passed
65 tests and the exit-contract run passed 57. The independent reviewer
repeated strategy (26), lifecycle (30) and exit contract (57) successfully,
and reproduced all three formerly failing close boundaries as refusals.
The exit parity fixture was corrected to supply a real 15:50 force-flat and
16:00 close; its 09:35 thesis deadline still follows its 09:34 entry, with all
parity assertions retained.

Additional passing runs include:

- Research repair regressions: 3 tests, including diagnostic v3 dispatch,
  measured bar-row strict economics with non-authorizing provenance, and raw
  no-trade capacity reporting.
- Related mechanism/gates/cost/lifecycle: 142; strategy-factory: 39;
  cost/counterfactual/version: 103; diagnostic/factory-ladder: 23.
- Observation provenance, heartbeat and acceptance: 63; account/provider and
  health: 41; factory report: 26.
- Initial strategy/exit/lifecycle suite: 117; related IBR/rule suites: 73.
- Independent runtime audit: 297 existing tests on latest main, plus 45
  existing safeguard tests on the older dirty working copy. Those 45 passing
  tests do not establish integration of that patch into this branch.
- Registered rule and IBR golden equivalence, static-v1 cost equivalence,
  protocol-1 proof compatibility and v1 preregistration reproducibility passed
  in the post-repair contract run.

These runs overlap; their counts must not be added into a unique test total.
The entire research/edge/factory inventory was not rerun after every repair.

The final post-review research contract/report run completed **132 tests** in
194.373 seconds, with one expected skip and no failures
(`outputs/deep-audit/research-post-review-tests.log`):

```text
python -m unittest -v tests.research.test_rule_golden_equivalence \
  tests.research.test_ibr_golden_equivalence \
  tests.research.test_preregistered_reproducibility \
  tests.research.test_preregistered_v2 tests.research.test_costs_versions \
  tests.research.test_gate_protocol tests.research.test_research_repairs \
  tests.research.test_factory_report
```

## Independent review and boundaries

The independent reviewer checked all 94 public arm identities and mapped
numeric fields, the VM aggregate's 31-arm/26-negative/3-positive/2-underpowered
shape, archive hashes and documentation links. Adversarial cost-identity/risk
tampering and malformed observation writes were rejected as expected.

The first review found that explicit session-close metadata could precede
entry or disagree with the force-flat offset. The repair was tightened to
validate both explicit and derived closes, require a matching force-flat
boundary when supplied, limit the close to US regular-session bounds, and
reject expired V4/V5 setup deadlines. This does not rewrite older rule
definitions or recorded results.

The review also found two remaining reporting limitations: legacy drawdowns
can omit the initial equity peak, and a legacy no-fill diagnostic row can
inflate descriptive observed counts. The arm CSV flags the former as
unvalidated; strict proof authorization remains separate from the latter.

## Frozen instrument identity

The repaired tree changes source files covered by preregistered-v2's sealed
instrument digest. That old instrument must refuse decisions on this tree.
The first 106-test contract run had seven failures, two errors and one skip
because synthetic decision fixtures still carried the old instrument identity;
the passing refusal checks were retained. Registration literals and historical
manifest hashes are not repinned to make the repaired tree eligible.

The unit fixture now binds its copied synthetic manifest to the running
instrument. Its one-symbol universe was already a test-only override. The
v2 module then passed 32 tests with one expected skip, while the actual frozen
registration and explicit mismatch tests remain unchanged. Verification of
the 46 original `HEAD` source modules still reproduced the frozen digest.

An independent real-manifest probe on 31 synthetic complete sessions refused
with `instrument_digest_mismatch`: measurement was never called, no sealed
statistics were disclosed, and no look record was emitted. The independent
reviewer's final verdict on the bounded repair package is **pass**, with the
documented release/research limitations retained.

Frozen v2 decisions belong to the original registration checkout. New source
requires an explicitly registered instrument and an appropriate untouched
window. Synthetic unit fixtures are not registration evidence.

## Verification limits

No multi-hour public-market replay, new sealed forward research, real-order
test, deployment, resume or VM mutation was performed. Raw VM trade rows
remained on the VM; the receipt content hash and bounded per-arm aggregates
were checked in place. Broker state was inspected separately from local fake
broker tests.

Baseline file coverage is an inventory and responsibility map, not a proof
that every source line is correct. Historical performance numbers remain
as-source except for explicit classification/limitation fields in the new
inventory. Compilation and whitespace checks cover the repaired source tree.
All 59 Markdown files passed the relative-link check: 485 links, zero broken
targets. The 94 distinct arm rows, 505 baseline file rows and 56 documentation
classification rows were rechecked after final CSV normalization.


## Continuation integration and VM recovery

The preserved safety patch was integrated into the newer repair checkout;
the older dirty root checkout remains untouched. A further independent review
found missing-ID/unknown-status close responses, ambiguous legacy stop
restoration, and an authorizing source-provenance gap. The continuation
repairs these boundaries without changing frozen trial registrations.

Fresh checks after the runtime integration:

- Complete runtime shard: **1,124 tests passed in 430.885 seconds**.
- Close/monitor/protection checks: **83 tests passed in 9.330 seconds**.
- Authorizing provenance, recorder declarations, cache invalidation,
  incremental/replay/session projection: **40 tests passed in 2.074 seconds**.
- Explicit forward fixture and cross-sectional/control/risk checks: **24
  tests passed in 0.331 seconds**.

The complete local inventory then passed on source commit
`78b93cd663412888330cb823096447d4052e6ff9`: **2,801 tests**, no failures or
errors, with one expected research skip. Its disjoint shards were runtime
1,124 (430.885 seconds), research 1,608 (1,141.789 seconds), edge discovery
62 (1,350.038 seconds), and factory integration 7 (1,238.885 seconds).
Private logs and a source/test manifest bind that run to its exact inputs.
The focused counts above overlap these complete shards and must not be added.

Some older synthetic forward fixtures lacked explicit source labels; those
fixtures now declare their intended provenance. Negative tests retain missing,
historical, conflicting and invalid source declarations. The production 5 GiB
recorder reserve remains unchanged; test disk fixtures do not disable it.

The VM archive migration verified all 412 regular files, including hashes,
owner, permissions and modification times, before reclaiming the original
physical copy. Original paths resolve to the preserved archive on the existing
persistent read-only backup disk. The private manifest and receipts were
copied to the Mac and independently hash-verified. Final read-only verification
confirmed all six services healthy, approximately 27 GB free on the recorder
volume, and unchanged paused/flat paper state, configuration, trial and order
history. No broker mutation or trial transition was performed. This establishes
storage recovery, not accepted forward market data or deployment of the new
source. The detailed private receipts remain outside Git.


The isolated production-image checks subsequently exposed a race in the
snapshot-lock test: a fixed 150 ms delay did not establish that its helper
had acquired the lock on the VM. The fixture now waits for the helper's
explicit lock-acquired message and acknowledges it before measuring the
snapshot operation. The original blocking-duration assertion is retained.
This changes test synchronization only; production source is unchanged.
