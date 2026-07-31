# Historical findings pointer

The original broad codebase analysis is retained in Git history. Current
implementation status is maintained in
[`../../MAIN_REPO_REVIEW_PLAN.md`](../../MAIN_REPO_REVIEW_PLAN.md), not in this
historical planning file.

The current system now has:

- all seven strategies evaluated from one live snapshot with isolated state;
- persisted baseline-plus-one assignments, bounded proposals/selections, and
  exact values in FindingsStore schema 14;
- immutable deterministic outcomes, reasons, reviews, and research-only edge
  candidates;
- append-only tournament and verified-backup history.

No result automatically edits the strategy registry. The environment-only
deployment action is provisioning a different-device external mount. See
[`../../OPERATIONS.md`](../../OPERATIONS.md).
