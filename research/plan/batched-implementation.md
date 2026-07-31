# Historical batch implementation map

This file is a compatibility pointer and historical snapshot. The former batch
plan is superseded by the current code, [`../../README.md`](../../README.md),
and [`../../OPERATIONS.md`](../../OPERATIONS.md).

The implemented path is:

```text
journal snapshots
  → one main demo strategy plus seven isolated research evaluators
  → baseline + one candidate per strategy
  → deterministic terminal outcome and reason
  → research-only LLM explanation / bounded next selection
  → RESEARCH_ONLY edge evidence when all gates pass
```

The older replay/G2 → forward qualification → reviewed T3 packet path remains
available as stricter evidence tooling. Neither path silently switches the
configured strategy or authorizes live capital. Durable findings are stored in
schema 14.

Use [`../../OPERATIONS.md`](../../OPERATIONS.md) for commands and
[`../HYPOTHESES_AND_VARIANTS.md`](../HYPOTHESES_AND_VARIANTS.md) for current
identities and values.
