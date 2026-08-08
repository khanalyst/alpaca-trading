# Findings index

No historical findings are currently registered. Previous exchange-specific
and digital-asset findings were removed when the research scope moved to
Alpaca US equities, ETFs, and listed options; they must not be mixed with the
new session-level evidence.

New findings belong in a content-addressed artifact that records:

- normalized input and code digests;
- provider/feed/schema and as-of cutoff;
- New York session/DST policy;
- vehicle (`equity` or single-leg long `option`) and its independent sample;
- costs, walk-forward/paired/placebo gates, and acceptance-floor result.

An option result and its underlying result are separate findings. There is no
pooled P&L scorecard.

Autonomous edge candidates are vehicle-local and must include the edge-lab
candidate/run/evidence identifiers. A champion is selected only from
conservative later-held-out evidence. The autonomous lane requires an initial
corpus backtest before later unseen shadow evidence; paper outcomes are
append-only forward evidence and normal promotion is automatic, while demotion
and any explicit rollback history can be audited.
