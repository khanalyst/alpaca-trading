# Current findings — verified 10 November 2026

This is the operational snapshot as of the 18:24 UTC verification on 10
November. Evidence and remaining blockers are distinguished from modeled
strategy performance. See the [full audit](deep-audit-2026-11-10/README.md)
and [validation record](deep-audit-2026-11-10/validation.md).

## VM and storage

The six production services still run source commit
`20da96612b21962091837a2efcba9180193b1cc0`. All six are healthy. Storage
maintenance recovered the recorder without restarting it. This maintenance
has not deployed the newer source or resumed trading.

The data disk had fallen below its unchanged 5 GiB reserve. First, 126
byte-identical inactive CSV exports were deduplicated, preserving every path
and file's contents, ownership, permissions and modification time. Then the
inactive historical archive tree was copied to the VM's existing persistent
backup disk. All 412 files were verified before removing the original
physical copy. The original archive path resolves to the retained copy;
the backup disk is persistently mounted read-only. Existing backups and trial
evidence remain intact. No Azure resource was added.

The final check measured 26,985,652,224 bytes free on the recorder's disk
(about 25.1 GiB). The archive disk retained 34,503,524,352 bytes free.
Private maintenance receipts, the complete file manifest and their hashes
were also copied to the Mac and verified. They remain outside Git.

## Paper account and authority

The final read-only broker check confirmed a paused, flat paper account with
zero open orders. Account identity, configuration, persisted state, trial,
accepted-session and outcome counts, equity and complete order-history hash
matched the pre-maintenance check. No broker mutation or trial action occurred.

The frozen ORB trial still has zero accepted sessions and zero outcomes.
Its frozen identity differs from the inspected current configuration. Do not
resume or replace it without its account-scoped identity and flat-book
preflight. No strategy is eligible for activation from this evidence.

## Research and forward data

The recorder resumed collecting observations after storage recovery. This
does not recover the missed forward observations after its October 19 outage,
or establish a newly accepted complete session. The latest acceptance census
remains the morning measurement: zero accepted sessions from 23 complete
partitions, with all 24 acceptance reports rejected. Earlier lag and cadence
failures remain separate from the repaired storage failure.

The October 11 mechanism-refresh job completed successfully on October 12.
Its receipt, sealed inputs, all 31 arm components and 24 signal reports were
reverified on November 10. Across 17 sessions, 26 arm point estimates were
negative, three IBR modeled accounts were positive, and two children were
underpowered. All 12 rule parents were negative; there were no eligible edges,
proofs or selections. The result content hash is
`1febfcf19804b14cfc2c010364f76a317c384da68bc56b4ca852a530c122c85c`.
Positive IBR rows are separate modeled accounts and cannot be added together
or treated as demonstrated profitability.

The corrected public `intraday-mechanisms.v3` comparison was negative for all
12 families over 30 sessions. All seven public IBR comparisons were also
negative. These are diagnostic results; they do not override rejected forward
data or authorize the frozen paper trial. See the
[October 28 report](research-2026-10-28/README.md).

## Source repairs and remaining release work

The continuation branch integrates the preserved runtime safeguards with the
newer main branch: monitoring while paused, durable close intent before
canceling protection, recovery after ambiguous broker responses, conservative
stop restoration and degraded-calendar force-flat handling. It also binds
shadow position lifetimes and quote visibility to causal observations.

Authorizing shadow inputs now require explicit forward provenance. Historical
partition markers exclude their data even when a row claims otherwise;
source-marker changes invalidate cached authorizing views and replay results.
The recorder declares newly created forward partitions before appending data,
without rewriting old CSV files or relabeling existing unmarked partitions.
Legacy diagnostic data remains available for non-authorizing analysis.

Local tests establish software behavior, not deployed behavior, market-data
acceptance or a profitable edge. A reviewed VM release and a complete forward
session that passes the existing lag, cadence, freshness and provenance checks
remain necessary. Frozen registrations, statistical thresholds and protected
trial evidence have not been rewritten.
