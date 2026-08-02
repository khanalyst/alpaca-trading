# Main repository review — implementation reconciliation

The original July 30, 2026 review plan is a historical snapshot. Git retains
its earlier wording; this file now records the reconciled state after the seven
implementation stages completed on July 31, 2026. Current operation belongs in
`README.md`, `SETUP.md`, and `OPERATIONS.md`.

## Seven-topic closure

| Topic | Current state |
| --- | --- |
| Adaptive proposal correctness | Complete: bounded exact values, immutable proposal events, atomic variant/proposal/finding persistence, duplicate/lock protection |
| Durable experiment rotation | Complete: per-strategy baseline plus one candidate, restart-safe assignments, both 3-day and 100-observation default floors |
| Seven-strategy real-time simulation | Complete: all seven consume one cycle snapshot/timestamp with isolated paper state and validated forward models |
| LLM research selection | Complete: bounded research-only contract; accepted/rejected history; no active-assignment preemption |
| Closed learning loop | Complete: immutable `WORKED`/`FAILED`/`INCONCLUSIVE` outcomes, reasons, limitations, separate research review, research-only edge candidates |
| Durable tournament and backup evidence | Complete in-repo: append-only run history, immutable artifacts, verified versioned backups, fail-closed external classification |
| Documentation/configuration reconciliation | Complete when this update passes its semantic checks |

## Safety conclusions

- One configured strategy can place orders on the demo account. Research paths
  cannot reach the exchange.
- All strategy experiments continue together; only each strategy's settings
  rotate serially.
- No LLM response, tournament result, terminal outcome, edge candidate,
  qualification event, or packet automatically changes live configuration or
  deploys capital.
- Both successes and failures remain persistent research evidence.
- Schema 16 is the current findings-store schema.

## Environment-only action

The repository cannot provision an off-host filesystem. Before the VM can be
considered deletion-safe, mount a pre-existing destination whose device differs
from all repository/source devices, configure `BACKUP_TARGET`, set
`REQUIRE_EXTERNAL_BACKUP=1`, create a backup, verify it, and confirm
`research.py readiness` reports the external-backup gate as PASS.

This is deployment configuration, not missing pipeline code. A configured path
or same-device directory is deliberately classified as `configured_local`.

## Historical evidence

The detailed findings that motivated the original R1–R4 plan remain in
`findings/main-repo-review-2026-07-30.md`. They are preserved as a dated review,
not as current implementation status.
