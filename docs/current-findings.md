# Current findings — verified 10 November 2026

This is the authoritative operational snapshot for the repository as of 10
November 2026. It records evidence and blockers; it is not a performance or
profitability claim. The [documentation catalogue](README.md) explains which
other files are current procedures, sealed protocols, historical reports, and
source artifacts.

The [full 10 November audit](deep-audit-2026-11-10/README.md) contains the
implementation, arm-level, and validation detail; this page is the concise
operational snapshot.

## Deployment and authority

- The VM deployment still reports source commit
  `20da96612b21962091837a2efcba9180193b1cc0` from 11 October. It is 60
  commits behind current main, `21d97293455b42ac2ef7298bf814b09b68680200`
  (28 October).
- A live infrastructure recheck at `2026-11-10T07:27:25Z` found all six
  containers on the `20da966` image. The recorder was unhealthy; the other
  five containers were healthy.
- The local root contains safety work that is not all upstream. This review has
  not mutated the VM or resumed trading.
- The 11 October mechanism-refresh receipt is now located at
  `/datadrive/alpaca-backups/research-mechanism-refresh-20261011-20da96612b21/work/run-receipt.json`;
  the recorded job completed with exit 0 at `2026-10-12T04:52:45Z`. It covers
  17 sessions, 31 arms, and 24 signal reports: 26 negative-point arms, 3
  positive IBR modeled rows, and 2 underpowered arms. All 12 rule parents were
  negative, with no eligible edge, proof, or selection. These are small
  retrospective modeled-account outcomes, not paper/live validation; the
  receipt content hash is
  `1febfcf19804b14cfc2c010364f76a317c384da68bc56b4ca852a530c122c85c`, and
  the local aggregate is `outputs/deep-audit/vm/oct11-31-arm-summary.json`.
  The earlier note that this receipt was unlocated is superseded.

## Data readiness blockers

- Recorder storage is below the reserve: 5,038,686,208 bytes free versus
  5,368,709,120 bytes required. The data drive is approximately 97% full, and
  there are no observations after 19 October.
- The scheduler is waiting: 0 accepted sessions out of 23 complete partitions,
  and all 24 acceptance reports are rejected. Lag and cadence failures were
  already present during 12–16 October.
- The 31 forward shadow books have 0 fills and 285,290 unpriced stale-quote
  rows. The active ledger has 7 candidates but 0 runs and 0 qualifying
  evidence.

These are data and capacity failures, not negative strategy returns. Do not
convert missing, stale, rejected, or unpriced rows into outcomes. See the
[diagnostic protocol](edge-diagnostics.md), [research protocol](../research/protocol.md),
and [operations runbook](../OPERATIONS.md).

## Broker and paper state

- The following broker and paper observations were made at approximately
  01:55–02:01 UTC; the 07:27 infrastructure recheck did not recheck broker
  state. At that earlier check, the broker was paused and flat, with no orders.
  The exact account balance is private aggregate evidence and is intentionally
  not reproduced here.
- The frozen paper ORB trial has 0 accepted sessions and 0 outcomes. Its frozen
  identity does not match the currently inspected configuration. Do not resume
  or replace it until the account-scoped preflight, identity check, and flat
  book/no-working-orders checks pass.
- No strategy is eligible for activation from this evidence. Paper shadows and
  diagnostic accounts remain non-authorizing; they do not grant live permission.

## Research status

- The latest-main report records 2,675 tests with no failures and one skipped
  test. This is a historical report from the 28 October release record, not a
  newly run test claim in this snapshot; see
  [the 28 October report](research-2026-10-28/README.md#7-tests). Fresh audit
  validation is linked from the [full 10 November audit](deep-audit-2026-11-10/README.md).
- The corrected `intraday-mechanisms.v3` cohort is net negative for all 12 arms
  over its 30 public diagnostic sessions. The older 82-arm comparison found no
  arm convincingly positive. These are public-bar research results, not live or
  broker evidence; see the [corrected cohort report](research-2026-10-28/README.md#4-did-the-corrections-change-performance-the-v3-cohort)
  and the [26 October refresh](research-2026-10-26/README.md).
- The October public IBR refresh was negative across its tested arms. It does
  not override the missing forward observations or authorize a paper trial.
- The preregistered hypotheses and their manifests remain sealed source
  artifacts. They must not be rewritten to make current results look better;
  use [preregistered-hypotheses.md](preregistered-hypotheses.md) and the
  [historical audit evidence](audit-2026-09-21/README.md).

## Decision and next action

Treat the project as data-readiness blocked. The next work remains versioned
fixes for storage/session readiness and a reviewed release, followed—only if
separately reviewed—by an audited paper-trial replacement exception. The
07:27 infrastructure check did not change or authorize broker state. No
document here authorizes live trading or claims a profitable edge.
