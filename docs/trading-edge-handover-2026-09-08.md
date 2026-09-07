# Trading edge audit and implementation handover

Prepared September 8, 2026, for the user-authorized commit and push to local
and GitHub `main`. Implementation baseline: `958da0c`. Repository:
`khanalyst/alpaca-trading`. This is a code handover, not a deployment or an
instruction to resume trading.

Read the [original audit](trading-edge-audit-2026-09-07.md),
[historical variant appendix](trading-edge-historical-variants-2026-09-07.md),
and [implementation record](trading-edge-remediation-2026-09-07.md).
The original audit compares latest non-main `cdef4aa` first and main `958da0c`
second. Its findings describe those revisions before these fixes.

## Completed

| Area | Delivered behavior |
| --- | --- |
| Trade accounting | Partial/canceled/retried closes aggregate original entry quantity and weighted exit price; incremental close costs and a completed parent outcome prevent misleading P&L/R. |
| Account protection | Daily account-loss checks run before proof, entry-cutoff and position-monitor early returns. |
| Hung trader recovery | Deployment commands launch an owned-child supervisor. It confirms child exit before handing the exclusive lock to authenticated watchdog recovery; operator pause persists. |
| Recorder | Valid orphan calendar/source sidecars no longer prevent rebuilding; malformed metadata still fails. |
| Research status | Startup visibility, independent process lease versus actual progress, terminal outcomes preserving the last phase, and protection against older jobs overwriting newer status. |
| Cache safety | Automatic cache identity derivation from historical partition markers is disabled. The existing explicit immutable-source opt-in remains. The new historical fingerprint is diagnostic only. |
| Strategy grammar | Opt-in v5 completed 5/15-minute price-path context and structural pullback/value reclaim entries; old v1–v4 identities and behavior preserved. Active timeframe comparisons are structurally distinct. |
| Experiments | Fixed 12-arm, three-family mechanism cohort with proposal/mutation disabled and all negative/zero-trade results retained. Diagnostic only; cannot authorize a trade. |
| Economics | Separate missing data, execution refusal and no signal; net expectancy, payoff and break-even win rate replace arbitrary win-rate pass/fail assumptions. |
| Allocation | Current-opportunity filtering before multi-edge allocation, using the same collected snapshot and existing emission/held-position constraints. |
| Reports | Lifetime completed parent trades, net winners, explicit unknown costs, gross/fees/net, and correctly labeled R versus capital return. |
| Trial confidence | Positive point estimates also need a positive 95% session-cluster lower bound to become promotable. Positive but uncertain evidence stays inconclusive. |
| Dashboard | Read-only account equity/drawdown, net payoffs and uncertainty, recorded exposure, frozen entry context, direct-job status and corrected pinned-stop explanations. |
| Documentation | README, architecture, research and deployment documents updated to describe actual code. |

## Not completed — priority order

1. **Deploy and verify the new build.** No image was built or deployed, no broker
   orders were submitted, and no operator pause was cleared. The audited host
   ran an older image and had no running trader/watchdog. Deployment must verify
   image/checkout identity, recorder freshness, supervisor/watchdog ownership,
   authenticated account state, and startup behavior before any operator resume.
   Do not interpret the historical host inspection as proof that the account is
   currently flat.
2. **A clean complete CI run on the final commit.** The full discovery run
   exercised 1,677 tests but was not green. Its four failures/errors were traced
   to obsolete trial fixtures and a synthetic-provider fixture mismatch, and
   the affected tests subsequently passed. Later integration runs also exposed
   obsolete dashboard expectations, now corrected. Focused final changes are
   tested, but the entire final tree has not been rerun in one clean full-suite
   invocation. Run the repository CI, including container build, on this commit.
3. **Seal immutable market snapshots before automatic cache reuse.** Historical
   provenance markers do not prevent subsequent recorder writes. Hashing before
   and after processing is not a write barrier. Implement a frozen snapshot
   lifecycle with recorder-lock coordination, durable publication and verified
   content identity before enabling automatic preprocessing reuse. Do not supply
   `ALPACA_RESEARCH_IMMUTABLE_SOURCE_IDENTITY` for a growing recorder directory.
4. **Restore and measure forward evidence.** The audited corpus had 137 historical
   backfill partitions but no qualifying forward-observed sessions. The ledger
   contained seven candidates, zero research runs/outcomes, and 120 ungraded
   lessons. New code does not fill those gaps. Collect fresh recorder/shadow
   observations, then actual paper fills; keep each evidence/proof epoch distinct.
5. **Run the new mechanism comparison on real, frozen input.** The 12 arms are
   implemented and tested with synthetic fixtures, not established as profitable
   alternatives. Freeze the dataset and cost assumptions, retain every arm,
   report opportunity counts/refusals/net payoff by family and session, then
   preregister an unseen confirmation experiment before choosing a treatment.
6. **Market-data fidelity beyond intraday OHLCV.** No new authoritative
   receipt-time/revision event store, paired IEX/SIP coverage study, prior-session
   levels, overnight gaps, catalyst/news calendar, market breadth or calibrated
   volume seasonality was implemented. V5 context is a bounded price-path
   feature; it does not provide these missing inputs.
7. **Calibrate execution with observed fills.** Measure decision-to-order-to-fill
   latency, crossing/spread, adverse selection, partial fills and cost by
   session/symbol/size. The code supports measured schedules, but this work did
   not produce a new calibrated schedule. Paper simulation omits execution
   effects; never infer zero impact merely from available touch depth.
8. **Portfolio and policy experiments.** The existing global book and correlation
   cap remain. Coarse dashboard ETF buckets are not a beta/sector/duration risk
   budget. A factor-aware allocator and the opportunity cost of consuming the
   once-per-session emission allowance before later refusals remain untested.
   A relative-strength/residual strategy still needs its own synchronized data
   and, if hedged, a separate portfolio/execution contract.
9. **Complete the trader's chart workbench.** The delivered charts cover account
   evidence and frozen entry context. They do not provide linked 1/5/15-minute
   and daily candles with prior-session levels, quote/fill timing, markouts
   versus matched controls, MFE/MAE, exit distributions, complete signal-to-proof
   funnels, or candidate/proof/feed/date filtering across every view. Equity
   charts are observed account equity, not cash-flow-adjusted strategy returns.
10. **Finish independent execution review and operational fault tests.** An
    independent final execution reviewer hit a provider usage limit before
    returning a verdict. Parent review and fake-broker/process regressions pass;
    that does not replace deployment-level outage/restart testing. Broker or
    network unavailability can still prevent local recovery.

## Verification evidence

- Full discovery: 1,677 tests in 2,263.466 seconds, with two failures and two
  errors. Preserve this result; do not describe it as a green full run.
- Current execution/reporting/trial/V5 package: 176 tests passed in 8.914 seconds.
- Market-data lane rerun after provider-fixture corrections: 14 tests passed
  in 149.964 seconds, including strict shadow versus diagnostic bar fallback.
- Reporting and status follow-up: 15 tests passed in 1.278 seconds.
- Independent strategy review: 10 strategy/cohort tests and 100 runtime-safety
  tests passed; its structural-timeframe defect was subsequently fixed.
- Independent pipeline review: 48 dataset/backfill/evidence tests, 17 cache
  tests and three direct-status/orphan tests passed. Its cache/status findings
  drove the final conservative changes described above.
- Final review-fix package: 47 tests passed in 35.782 seconds, including
  source fingerprint semantics, explicit cache reuse, startup/abort/older-owner
  status handling, structural context comparisons and the fixed cohort.
- Python compilation, Bash syntax, dashboard JavaScript syntax, whitespace,
  and default/research Compose configuration validation passed. The local Docker
  daemon was unavailable, so no image-build result is claimed.
- Browser verification used only synthetic data on localhost: three closed
  parent outcomes, one net winner/two net losers, gross $1.05, fees $0.30,
  net $0.75, separate job/scheduler status, equity/drawdown/payoff charts,
  frozen entry context and corrected pinned-stop labels.

Final review-fix regression results are also recorded in the local status file
listed below. Test counts overlap; they must not be added as unique tests or
interpreted as market-edge evidence.

## Economic conclusion

No deployable positive edge has been established. In the historical sample,
20 of 44 variants traded and all 20 lost; 24 had no trades. Those isolated
simulations totaled 650 trades and net −$29,748.81. They are not a combined
investable portfolio. Gross was already measured at modeled fills. The purpose
of these fixes is to make subsequent economic evidence reliable and the next
hypotheses interpretable. More variants or looser proof/risk limits would not
resolve the demonstrated absence of edge.

## Retained local evidence

Raw host snapshots, detailed simulation artifacts, test logs and reproducible
probe scripts remain in these local repository directories and are not part of
the GitHub handover:

- `outputs/trading-edge-audit-2026-09-07/`
- `outputs/trading-edge-remediation-2026-09-07/`

Their Markdown audit findings and variant appendix are included in `docs/`.
Operational snapshots and generated logs are retained locally to avoid mixing
runtime material with the source change. Unrelated `.codex-work/`, other
`outputs/` content and the existing Instagram document are outside this commit.
