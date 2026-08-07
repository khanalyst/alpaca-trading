# Archived edge-platform design record

> **ARCHIVED / FROZEN — not a current checklist.** The original document
> described the seven-day demo corpus, depth-ladder repair, and staged-platform
> implementation while that work was in progress. Its numbered batches and
> open items are intentionally retired.

The durable historical conclusions are limited: momentum was negative, several
lanes were starved by missing depth data, and the order path moved to unproven
`ls-ratio-fade` so rejected momentum would not end the collection process. None
of those facts proves a positive edge.

The current runtime uses feed 8, four deterministic realtime families, three
offline-only long-horizon families, a shared baseline plus bounded candidates,
and a separate staged mechanism population. See
[`../AUTONOMOUS_RESEARCH.md`](../AUTONOMOUS_RESEARCH.md) for current semantics
and [`../HYPOTHESES_AND_VARIANTS.md`](../HYPOTHESES_AND_VARIANTS.md) for
strategy status. Git history retains the full design narrative.
