# Current findings — verified 10 November 2026

This is the operational snapshot as of the 19:44 UTC verification on 10
November. Evidence and remaining blockers are distinguished from modeled
strategy performance. See the [full audit](deep-audit-2026-11-10/README.md)
and [validation record](deep-audit-2026-11-10/validation.md).

## VM and storage

The tested source is published on GitHub main. The six production services
were updated at 19:28 UTC to commit
`41234e25c3a94575d52616f16d84c8a34e4b82dc`, image
`sha256:2db17c78fe78e3b435943f81c114bdc4bf04fe84cae9fcc8c76f4d5f784a5d8c`.
All six passed fresh health checks, and the recorder is recording without an
error. The paper account remains paused and flat.

Before activation, the 471,250,063-byte critical-state archive was copied to
the Mac and SHA-256 verified. An isolated restore verified every one of its
91 critical files, all five SQLite databases and the complete journal history.
The backup and the prior deployed image remain available.

The data disk had fallen below its unchanged 5 GiB reserve. First, 126
byte-identical inactive CSV exports were deduplicated, preserving every path
and file's contents, ownership, permissions and modification time. Then the
inactive historical archive tree was copied to the VM's existing persistent
backup disk. All 412 files were verified before removing the original
physical copy. The original archive path resolves to the retained copy;
the backup disk is persistently mounted read-only. Existing backups and trial
evidence remain intact. No Azure resource was added.

After verification, temporary restore/transfer files, the superseded build
checkout and its unactivated image were removed. Protected backups and trial
evidence were retained. The 19:44 UTC check measured 25,981,181,952 bytes free on
the recorder's disk (about 24.2 GiB). The archive disk retained
34,503,524,352 bytes free. Private maintenance/release receipts, manifests,
test logs and frozen configuration were copied to the Mac and verified;
they remain outside Git.

## Paper account and authority

The final read-only broker check confirmed a paused, flat paper account with
zero open orders. Account identity, configuration, persisted state, trial,
accepted-session and outcome counts, equity and complete order-history hash
matched the pre-maintenance check. No broker mutation or trial action occurred.

A follow-up check on 11 November (Dubai time) confirmed the direct runtime
entry blocker: `state=PAUSED`, `operator_pause=true`, with heartbeat reason
`operator_pause`. That flag predates this rollout and was preserved with the
frozen baseline. The stored trial record still says `running`; it does not
override the runtime pause. All six services are healthy, with zero broker
positions and zero open orders.

The frozen ORB trial has zero accepted sessions and zero outcomes. These are
trial-evaluation progress counts, rather than the switch that set the operator
pause. Its stored status alone reports entry eligibility, but comparing the
current descriptor finds different candidate, incumbent, policy, code and
cohort identities. The existing trial must not be silently rebound to those
new identities. A resume requires resolving that mismatch and passing the
account-scoped and flat-book preflight; research qualification remains subject
to its existing evidence gates. No strategy is qualified for activation by the
retrospective research in this report.

## Research and forward data

A fresh VM research run started at 19:33 UTC on the deployed source. It
covers all twelve mechanism families, their twelve direct-parent controls and
seven IBR comparisons: 31 execution reports and 24 signal reports. Its new
plan and registration are verified, and the job is running without an error
or OOM. Results are pending.

The run uses the previously examined 17-session sealed input. It recomputes
results with the new code and does not reuse the prior result as its baseline.
It is retrospective, non-authorizing research; it makes no untouched-holdout
claim and cannot change the frozen paper trial. The plan hash is
`9552d825ffae0f64b9f6cd3b75171179d38cc50ceb2ff415fe46ecb27f8d1f43`.

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

## Deployed repairs and remaining data work

The deployed release integrates the preserved runtime safeguards with the
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

The complete local suite passed 2,801 tests with one expected skip. The full
GitHub CI run on the executable fixes also passed; the subsequent lock-test
synchronization change passed its focused suite and the deployed-image checks.
The deployed image passed 1,124 runtime checks (one expected Docker Compose
availability skip inside the image) and 102 shadow checks. See the
[validation record](deep-audit-2026-11-10/validation.md) for exact source scopes.

New complete forward sessions must still pass the existing lag, cadence,
freshness, provenance, power, FDR and broker-equivalence requirements. The
frozen trial identity remains a separate activation blocker. Registrations,
statistical thresholds and protected trial evidence have not been rewritten.
