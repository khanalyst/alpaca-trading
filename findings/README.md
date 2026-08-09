# Findings index

No historical findings are currently registered. Findings are scoped to
Alpaca US-listed equities/ETFs and listed OCC options, with one independent
result book per vehicle.

New findings belong in a content-addressed artifact that records:

- normalized input and code digests;
- provider/feed/schema and as-of cutoff;
- New York session/DST policy;
- vehicle (`equity` or single-leg long `option`) and its independent sample;
- costs, walk-forward/paired/placebo-falsification gates, structural floors,
  FDR result, and durable verified-gate result.

An option result and its underlying result are separate findings. There is no
pooled P&L scorecard.

Autonomous edge candidates are vehicle-local and must include the edge-lab
candidate/run/evidence identifiers. A champion is selected only from
conservative later-held-out evidence. The autonomous lane requires an initial
corpus backtest before later unseen shadow evidence; paper outcomes are
append-only forward evidence and normal promotion is automatic, while demotion
and any explicit rollback history can be audited.

Good edges are written as deterministic, content-addressed artifacts. An
optional `research.proof.webhook_url` may notify an HTTPS endpoint after the
artifact is durable; notification failure does not change the artifact. The
scheduled cycle reports `completed`, `completed_no_edge`, `no_data`, or
`failed`; only a durable verified gate can make a finding runtime-eligible.

Underpowered data is not failure. Retirement requires every intended variant to
be adequately tested and failed, and an enabled strategy LLM must register a
valid bounded replacement first. The checked config uses model `gpt-5` and
loads optional credentials only from `ALPACA_RESEARCH_LLM_SECRETS_FILE`; missing
or invalid output leaves a pending replacement.
