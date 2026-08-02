# Historical V2 hardening plan — reconciled

This filename is retained for old links. The former batch plan is superseded by
the current implementation and is not an operator runbook.

Implemented foundations now include:

- G2 replay fidelity and immutable decision-ledger evidence;
- all seven strategies on one real-time snapshot with isolated accounts;
- per-strategy baseline-plus-one durable rotation;
- bounded adaptive proposals and research-only LLM selections;
- deterministic `WORKED`/`FAILED`/`INCONCLUSIVE` outcomes and separate review;
- `RESEARCH_ONLY` edge candidates with no automatic promotion;
- append-only tournament runs and immutable per-run artifacts;
- FindingsStore schema 16;
- versioned SQLite-online backups with checksums, integrity verification, and
  fail-closed `external_mounted` classification.

The remaining environment action is to provision a genuine different-device
mounted backup destination on the VM. Current setup and operation are described
in `SETUP.md` and `OPERATIONS.md`.
