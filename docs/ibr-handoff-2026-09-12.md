# IBR runtime handoff — September 12, 2026

The remaining code handoff is implemented. This is a diagnostic measurement and
shadow-operation repair, not a profitability claim or permission to trade.
No new strategy families, parameter search, risk relaxation or broker orders
were introduced.

## Completed implementation

- The seven existing IBR variants are resolved through `agent.variants.apply`.
  The shared live signal, setup and risk code now runs against causally visible
  forward events in the diagnostic shadow worker.
- Each arm retains its own cash, positions and one-signal-per-symbol/session
  state. Signal state is recorded before setup/risk refusal, matching the live
  sequencing. Authored brackets remain based on the signal close; executable
  bid/ask, tick geometry, stress limits and sizing are evaluated afterward.
- IBR account exits use the existing stop/target, gap/tie and exact force-flat
  logic without inventing a rule holding period. Broker-only shortability,
  buying power, pending orders and actual fills remain unobserved.
- The new offline adapter reuses that worker without opening a shadow store,
  EdgeLedger or broker connection. It rejects altered cohort identities,
  validates actual source hashes, and bounds the input without truncating it.
- Missing forward provenance, quotes or exact calendars produce unavailable
  runtime diagnostics. Incomplete/unpriced opportunities remain missing data.
  An independent review reproduced and closed a bug where a sparse closing bar,
  including another symbol's bar, falsely established a no-signal session.
  Completion now requires contiguous coverage for that symbol and session.
- The inventory still contains 43 unique existing arms. Runtime IBR measurements
  and the unchanged legacy fixed-share comparisons are reported separately.
- Compose explicitly opts its broker-free shadow service into 31 modeled
  accounts: 24 rule arms plus seven IBR arms. The library/standalone CLI retains
  the 24-arm default unless opted in. Changing code/cohort starts a new evidence
  epoch; old observations are not relabeled as belonging to the new epoch.
- Earlier pending repairs are included: bounded pre-acceptance diagnostics,
  quote-only calibration with immutable nonactivating publication, batched
  idempotent shadow processing, recorder phase/cadence measurement and refreshed
  forward quote-request bounds, and corrected cost-refusal accounting.

Operational commands and safeguards are in [edge-diagnostics.md](edge-diagnostics.md).

## Replayed evidence, not new market observations

The frozen source is the previously examined June 30–August 11, 2026 historical
corpus: 86,998 bars, eight ETFs, 30 sessions, and zero contemporaneous quotes.
Both replays were completed on the frozen September 12 code. The independent
audit recomputed source/config/code/report hashes and confirmed the old rule
and legacy IBR outcomes are unchanged.

| Inventory lane | Arms | Result |
| --- | ---: | --- |
| Rule diagnostic and mechanism arms | 36 | Execution-blocked; no measured trade expectancy |
| Shared-runtime IBR | 7 | Unavailable on this historical, quote-free source |
| Legacy IBR comparisons, kept separately | 7 existing IBR IDs, not additional arms | Six negative and one small positive point estimate; none eligible |

All 43 arm IDs and measurements are included in
[edge-results-2026-09-12.json](edge-results-2026-09-12.json).
The only positive legacy result remains `ibr.range.45`: **+$1.055386 total over
42 fixed one-share trades**, or +$0.025128 per trade. It is not a runtime-parity
result, untouched holdout, passing proof or selected candidate.

The repeated, nonauthorizing twelve-arm cost/risk counterfactual also matches
the prior completed replay. The unchanged 0.30 ratio admits zero trades; the
predeclared 1.00 diagnostic alternative admits 791 overlapping variant-trades.
Eight arms have positive **reference gross before modeled costs**, but all twelve
are negative after modeled execution drag and fees. Reference gross is not the
report's already fill-adjusted gross field. For those eight arms, the mean
reference gain is $5.45–$23.72 versus $41.63–$41.94 modeled drag per trade.
These are isolated, path-dependent accounts, not a combined portfolio or an
isolated causal experiment. No production ratio was changed.

The economic gap therefore remains unresolved: the observed modeled drag
exceeds the signal return, and actual quote/fill cost evidence is insufficient.
More variants or repeatedly examining the same source cannot establish a new
validated edge. The report has zero eligible candidates, zero proofs and null
portfolio P&L.

## Verification

The independent final IBR/source/suite package passed **36/36** checks. It covers
all previously unmapped IBR filters, causal delayed observations, quote/session
cutoffs, durable rejection state, long/short geometry and sizing, exits,
incomplete-source reporting, deterministic P&L reconciliation and forbidden I/O.

The complete disjoint regression run passed **2,057/2,057 tests**, with zero
failures, errors or skips. Every shard exited 0 on the frozen source.

| Shard | Tests | Final result | Local seconds |
| --- | ---: | --- | ---: |
| Edge discovery | 61 | Passed | 952.686 |
| Factory end-to-end | 6 | Passed | 612.113 |
| Research | 1,129 | Passed | 525.160 |
| Runtime | 861 | Passed | 383.444 |

Commands: `.venv/bin/python deploy/test_suite.py --shard NAME`, with `NAME` set
to `edge`, `factory`, `research`, and `runtime`. These ran concurrently; times
are local verification timings, not production-cadence measurements. The final
code hash still matches the frozen experiment and both completed replays.

Locked dependencies were installed, including the previously unavailable
`alpaca-py==0.43.5`; its focused runtime module passed 36 tests with no skips.
Python compilation, shell syntax, whitespace checks, Compose configuration
validation (normal and research profile), and the CI stale-provider-term check
passed. Compose validation used `/dev/null` as an inert research-secret path;
it did not start containers or contact a provider.

## Production boundary and next operating handoff

This code handoff does not include a production deployment. The successful
September 12 read-only preflight found six healthy services on the older
`c9368e2` build. At 07:44:21 UTC, research health reported four complete sessions,
zero accepted and four rejected. At 07:44:20 UTC, shadow health reported 24 arms,
38,433 events per arm, a 20.425263-second poll and stale source data. Those are
process-health observations while the market was closed, not accepted market
coverage or measurements of the new 31-arm release. Later detailed evidence
projections were denied by the approval boundary; no per-session rejection
breakdown or new calibrated execution-cost claim was established.

The operating next steps remain explicit:

1. Deploy the verified commit using the existing approved backup/image rollout
   procedure, preserving operator pause, live-money disabling and all risk gates.
2. Freeze the 31-arm code/config/feed epoch before a complete session; verify
   contemporaneous bars/quotes, every arm's progress, no replay errors, and the
   existing full-session acceptance requirements. Do not count old rejected
   sessions or the prior 24-arm epoch as new accepted evidence.
3. Measure fit/held-out quote-cost coverage without activating the artifact.
   Actual fills and broker-only conditions require their own observed evidence.
4. Evaluate independently specified economic hypotheses on untouched validation
   data. Do not widen stops, reduce assumed costs or select extra variants merely
   to turn historical results green.

## Frozen identities and local artifacts

- Research/agent bundle (76 files):
  `0651cd17890e6650210b7986a9a87c75111413904cf7b22ebe34991b07d44a1b`
- Unchanged `config.yaml` file SHA-256:
  `805cd15c9451ef7e39716c652a8b7e18f60e268afec4548a67ab3e8d069c700e`
- Source file SHA-256:
  `000ae5d970dafd46f87574c6e2f5407450912b3e0142b5729326c6da6c7b920a`
- Normalized source:
  `0d6e1a796d451d87c59c8ef2c2ce7e42b5eaf2928c4f0596c3b712f52a41de12`
- Inventory manifest:
  `eab62f5a3d5f776d6d5b6e13bd67f57af6903e2260acbf9bb5e4e512d8d1e864`
- Inventory report:
  `cbe63873446c4c8621e42cd44f9ffd69bf28a236ff534e9bc69b689a159b555c`
- Counterfactual report:
  `1dcfb103ccaa3d6cd473d2da30ca3303b32d4aec69961367d7d14aeeae526922`

The experiment, full reports and test logs are retained locally under
`outputs/ibr-handoff-2026-09-12/`. Large source/output files and unrelated local
work are not included in the Git change. The compact results above are included.
Earlier dated artifacts remain historical records and have not been overwritten.
