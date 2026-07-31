# Historical findings pointer

The original broad codebase analysis is retained in Git history. Current
implementation status is maintained in
[`../../MAIN_REPO_REVIEW_PLAN.md`](../../MAIN_REPO_REVIEW_PLAN.md), not in this
historical planning file.

The current system now has:

- isolated hypothesis variants and bounded adaptive proposals;
- persisted proposal/lock/history and exact values in FindingsStore schema 8;
- immutable decision-ledger evidence, forward qualification, and family
  correction;
- content-addressed T3 packets that require review and do not edit the
  strategy registry.

The remaining operational work is the approved-corpus tournament re-score,
targeted exploratory fixtures, packet-to-strategy hash resolution, and an
explicit reviewed approval command. See [`../../OPERATIONS.md`](../../OPERATIONS.md).
