# Archived maker-first design record

> **ARCHIVED / FROZEN — not a current checklist.** This record preserves the
> design boundary for the maker-first entry experiment. Current operator
> guidance uses the semantic name “maker-first entry.”

The design introduced a demo-only passive entry attempt followed by bounded IOC
fallback. It is distinct from the `scalp-maker` research family. Ambiguous
cancel/order state must fail closed, and configuration validation still rejects
the maker-first path in live mode.

Current behavior and completion conditions are in
[`../../OPERATIONS.md`](../../OPERATIONS.md#maker-first-entry-boundary) and the
authority chain is in
[`../AUTONOMOUS_RESEARCH.md`](../AUTONOMOUS_RESEARCH.md). Git history retains
the original experiment label and evidence checklist.
