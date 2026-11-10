# Code, strategy and evidence audit — 10 November 2026

This audit starts from GitHub main `21d97293455b42ac2ef7298bf814b09b68680200`.
Changes are prepared in an isolated checkout on `codex/full-audit-20261110`;
the older working copy and its uncommitted safety work are preserved.
The VM remains on `20da96612b21962091837a2efcba9180193b1cc0`.

The earlier readiness answer was incomplete: it did not reconcile the local
research archives, inspect all arm-level results, or independently test the
implementation gaps. In particular, its October 11 result was reported as
unlocated. The launch archive identified the exact backup directory, where the
completed result and receipt were found. The result hash was recomputed and
matched both stored identities. This document supersedes that uncertainty.

## Coverage and evidence

- The baseline inventory covers all **505 tracked files**: 188 non-test source
  files, 184 test files, 55 Markdown documents, 54 evidence artifacts and 24
  configuration files. SHA-256, size, line count and review responsibility are
  recorded in [baseline-file-inventory.csv](baseline-file-inventory.csv).
- The strategy review traces all **12 rule families and seven IBR variants**
  through signal, admission, entry, exit and runtime/schema reachability.
- [all-94-arms.csv](all-94-arms.csv) contains every distinct published arm:
  the 82-arm October 26 inventory plus 12 corrected v3 arms. It includes exact
  variant identities, trade/session counts, gross/net bps, clustered t,
  win rate, fresh-window results, exit mix and separate simulated portfolio
  returns. Parent rows from the v2/v3 comparison are not counted twice.
  Every row also includes its frozen spec/IBR overrides, schema-level equity
  reachability, economic classification, remaining gap IDs and relevant
  implemented fix IDs. Of the 94 arms, 43 have an executable equity schema and
  51 are research-only. Schema reachability is not qualification or permission
  to trade. Fix IDs describe the new code; historical returns are unchanged.
  Copied portfolio drawdowns are explicitly marked unvalidated: the legacy
  analysis helper omits initial equity when tracking peaks and can understate
  first-day losses. Correcting those numbers needs the raw replay; the
  per-trade return columns are unaffected by that reporting defect.
- [corrected-family-summary.csv](corrected-family-summary.csv) is the compact
  12-family view. Source JSON remains under the dated research directories.
- All **31 October VM arms** were inspected as aggregate results in place.
  The private aggregate, receipt hash and per-arm CSV are under
  `outputs/deep-audit/vm/`. Raw trade rows remain on the VM.
- Implementation probes and component reviews are under
  `outputs/deep-audit/{strategy,research,runtime,verification}/`.

Inventory coverage is not a claim that every line has been independently
proved correct. The audit combines complete file/result inventories,
end-to-end implementation tracing, adversarial probes and regression testing.
It does not repeat the multi-hour public-market replay or acquire a new sealed
forward corpus. Existing public reports are diagnostic retrospective evidence.

## What the October VM run actually found

The October 11 job completed at **2026-10-12 04:52:45 UTC**, after about
15.6 hours. It evaluated 31 arms over 17 previously examined sessions and
produced 24 signal-quality reports. Its result hash is
`1febfcf19804b14cfc2c010364f76a317c384da68bc56b4ca852a530c122c85c`.
There were 26 negative point estimates, three positive point estimates and two
underpowered arms. It produced no eligible edge, proof, or selected winner.

All 12 rule parents were negative. Ten refreshed rule children were negative;
opening-range fade and volatility breakout each had only three completed
trades and were underpowered. The three positive IBR arms were:

| IBR arm | modeled trades | isolated-book net P&L | profit factor |
| --- | ---: | ---: | ---: |
| `ibr.range.30` | 12 | +7.07 | 1.013 |
| `ibr.buffer.0bps` | 13 | +67.26 | 1.059 |
| `ibr.buffer.10bps` | 13 | +264.06 | 1.299 |

These are small modeled samples with substantial data rejections, not actual
broker fills. The dollar P&Ls belong to separate diagnostic books and must
not be added into a portfolio. All seven IBR arms are negative in the separate
30-session public-data refresh. Neither result can be substituted for the
other or described as stable profitability.

The VM's holding-path quality is materially weak. In refreshed children,
missing-data exits ended 14/33 ORB trades, 34/69 momentum trades, 38/64
trend-pullback trades, 20/34 volume-breakout trades, 41/70 VWAP-trend trades,
49/90 range-expansion trades and 27/49 residual trades. Thus many measured
trades did not complete their intended holding thesis. The 17-session VM
results mix strategy behavior with data discontinuity and modeled execution
drag; they are not clean confirmatory negatives. Fixing coverage could change
returns in either direction. The independent public-data comparison remains
negative after modeled costs.

The aggregate fee analysis also distinguishes P&L at modeled fill prices
from a pure cost-free signal return. Most rule arms were already negative at
their modeled fills before the separately charged fee. That does not isolate
spread/slippage drag, which is already inside those fill prices. The private
per-arm exit/cost breakdown is
`outputs/deep-audit/vm/oct11-cost-exit-breakdown.json`.

The completed receipt is at
`/datadrive/alpaca-backups/research-mechanism-refresh-20261011-20da96612b21/work/run-receipt.json`.
The stale narrative which said the job was pending has been retired.

## Each family: implementation gap versus economics

The corrected v3 figures below are net bps per trade over 30 public sessions.
These v7-schema arms remain research-only. An implementation repair does not
make them executable or authorize an older arm under a new identity.

| family | v3 net bps | code/data gap | economic conclusion / next test |
| --- | ---: | --- | --- |
| Opening-range breakout | -4.51 | Opening-prefix completeness, quote availability and stop-floor admission affect coverage. Most corrected exits are time exits. | No positive aggregate or fresh result. Extra confirmation did not create a profitable VM child. Stop parameter search until a separate, prospectively specified effect beats costs. |
| Opening-range fade | -0.33 | Deadline/structural-stop/anchor-target corrections are research-only; the VM child has only three trades. | Closest corrected mean to zero, but fresh window -1.28. Original baseline's positive public mean also reversed fresh. Existing paper wiring is operational convenience, not an alpha ranking. |
| Momentum continuation | -2.91 | Shared causal quote and session coverage matter; no independently reproduced family-predicate defect. | Full and fresh windows lose. A new regime/event hypothesis would be new research, not a repair. |
| Mean reversion | -1.43 | Corrected deadline and anchor target restore tradability of the research hypothesis; missing context/coverage remain measurable. | Corrected 720 trades still lose, with -4.23 fresh. More signals are not stronger evidence. |
| Trend pullback | -2.08 | Same-clock volume needs point-in-time prior sessions; the factory does not supply all required context. | Fresh +1.52 is weak and accompanies negative full and other slices. Fix context before interpreting absent signals; do not select on the fresh slice. |
| Volatility breakout | -1.88 | Legacy bps compression and corrected ATR compression are different schemas. A VM child reduced the sample to three trades. | Negative corrected full/fresh results. Schema reachability and sample size are separate from the negative measured economics. |
| Volume breakout | -1.87 | Same-clock variants need prior-session baselines; current-bar volume must remain excluded from its own comparison baseline. | Fresh +0.51 is too small/unstable. Verify executable costs and power before another threshold change. |
| VWAP reversion | -2.91 | The old deadline could switch an arm off; v3 repairs that and produces 562 trades. | Restoring trades exposes a loss, not a hidden profit. Fresh -2.92 and the separate liquid-reversion smoke runs are also negative. |
| VWAP trend | -0.95 | Session-prefix VWAP and opt-in rolling slope have different semantics; strict prefix gaps suppress coverage. | Near-flat fresh -0.07 does not establish an effect. Any small gross drift needs credible costs and sufficient independent days. |
| Range expansion | -2.64 | Legacy `volume_multiplier` reuse is versioned; v7 has an explicit range multiplier. | Full and fresh windows lose; the VM child is also materially negative. Renaming or tightening the rule cannot establish alpha. |
| Opening drive | -2.33 | Exact opening-window and hold checks must remain causal. No independent predicate defect was reproduced. | Fresh +3.25 is a small subset against negative full and VM results. Require a new untouched test. |
| Cross-sectional residual | -4.96 | Factory context discards prior subject history needed by beta mode; subject/SPY alignment and executable schema reach are mandatory. | The correctly contextualized public v3 driver still loses. Context plumbing repairs measurement, not the observed residual effect. |

### Every IBR variant

| variant | 30-session public net bps/trade | fresh eight-session net bps |
| --- | ---: | ---: |
| `ibr.baseline` | -8.01 | +5.73 |
| `ibr.range.30` | -8.37 | +10.46 |
| `ibr.range.45` | -5.93 | +4.27 |
| `ibr.target.1_5r` | -6.82 | +7.11 |
| `ibr.target.3r` | -8.03 | +6.35 |
| `ibr.buffer.0bps` | -7.58 | +5.63 |
| `ibr.buffer.10bps` | -7.97 | +7.87 |

The shared IBR code has a duplicate list-state mutation, a raw-versus-rounded
stress-floor comparison, and an unwired same-clock prior-session input in
shadow. The latter two can change admission. They need a new evidence epoch;
old replay results must retain their actual semantics.

### Other research tracks

The arm table does not replace studies with different endpoints or holding
periods. Their latest published outcomes are listed separately below; these
are reviewed source results, not newly rerun market studies.

| track | result | gap / decision |
| --- | --- | --- |
| VWAP-reversion control-adjusted v1 | 24 sessions; 60-minute delta +1.94 bps, t 0.55, sign-flip p 0.29. | Below its first 30-session look; instrument/decision changes after sealing weaken confirmatory status. Inconclusive. |
| Opening-range-fade control-adjusted v1 | 22 sessions; delta +2.74 bps, t 0.45, p 0.34. | Below first look and shares the v1 instrument problem. Inconclusive. |
| Preregistered v2 | Registered 28 October for complete sessions from 29 October onward. The inspected VM has no post-19-October forward observations. | No usable VM accrual in that window. Evaluate only with the frozen registered instrument; repaired source requires a separate identity/window. |
| Liquid-ETF reversion smoke | One-minute: 23 trades, -10.26 net bps/trade. Five-minute: 38 trades, -2.46. | Negative, small, diagnostic samples. The proposed long SIP walk-forward is not a completed result. |
| Overnight drift | Sixteen-year gross nightly means: SPY +3.57 bps, QQQ +5.04, IWM +5.19; reported t 3.39–4.12. | A separate overnight exposure/risk-premium hypothesis. Requires executable costs, gap/drawdown and exposure analysis; cannot be inserted into a flat-overnight intraday pilot. |
| First-half-hour to last-half-hour momentum | 486 hourly sessions; pooled signed strategy +0.20 gross bps, t 0.92. | No convincing pooled effect. The isolated GLD result is a selected subset, not a qualified winner. |
| Large-opening-gap fade | Sixteen years; no robust effect, with some metal ETFs continuing instead. | No basis for adding this filter as a profit repair. |
| Cross-sectional first-hour reversal | 486 sessions; +2.6 gross bps/day, t 0.84, before eight-leg costs. | Weak gross effect with substantial execution drag. |
| Professional VWAP reversion | One-/five-minute net -2.6 / -1.0 bps/trade. | Negative despite volatility-scaled entry and anchor target. |
| Professional opening-range fade | One-/five-minute net -2.2 / -2.3 bps/trade. | Negative; a more elaborate stop/target did not solve economics. |
| First-five-minute ORB | 49 five-minute sessions; net -3.7 bps/trade, t -1.78. | Negative diagnostic result. |

These values and study definitions are preserved in the
[26 October report](../research-2026-10-26/README.md#4-the-sealed-preregistered-hypotheses).
The September sweeps remain exploratory history rather than independent
evidence to add to these later samples.

## Repairs and remaining engineering work

CSV gap IDs: `FORWARD_DATA_TIMING` is the recorder/shadow quote ordering and
freshness issue; `EXPECTED_COST_PRODUCER` is the unwired expected-cost gate
input; `RESEARCH_ONLY_SCHEMA` denotes deliberately non-executable V6/V7;
`PRIOR_SESSION_BETA_CONTEXT` and `PRIOR_SESSION_SAME_CLOCK_CONTEXT` denote
missing causal replay context; `IBR_RAW_VS_ROUNDED_FLOOR` is the false
admission refusal described below. The `implemented_relevant_fix_ids` column
identifies dispatcher, early-close and list-state repairs without claiming a
new market replay was performed.

The current repair branch addresses bounded correctness issues; final status
and executed validation are recorded in [validation.md](validation.md).

| issue | disposition |
| --- | --- |
| v3 diagnostic cohort dispatch falls through to v1 loader | Repair diagnostic dispatch and arm-description compatibility; preserve the manifest and non-authorizing lane. |
| Measured bar-priced rows fail strict per-leg cost binding | Repair cost/economics identity while retaining explicit bar-fill provenance and exclusion from authorization. |
| Capacity reporting discards explicit no-trade opportunities | Count raw capacity separately from observed executable trades; retain all proof floors. |
| IBR wrapper records list-backed session state twice | Make wrapper recording idempotent; retain direct evaluator behavior. |
| V4/V5 setup/fill thesis deadlines assume 16:00 on early closes | Carry the resolved exact close through setup, persistence and fill recomputation. |
| Paused heartbeat hides an explicit residual-risk marker | Classify the marker as degraded; malformed explicit values fail closed. |
| Revision store promotes unknown source modes or NaN lag limits to forward data | Reject invalid provenance/limits before creating or mutating storage. |
| Broker account equity/cash admit non-finite decimals | Validate them with the same finite-decimal boundary already used for buying power; preserve finite negative cash and zero buying power. |
| Factory narrative calls `1 - min(p)` confidence | Leave confidence unavailable, label the minimum p-value as descriptive/unadjusted, and reject invalid p-values. Gate statistics and old artifacts remain unchanged. |
| Factory verdict averages effects across records/endpoints and envelopes their intervals | This remains a descriptive roll-up, not a pooled net-return estimate, pooled confidence interval or portfolio power calculation. Gate deltas and signal bps must be interpreted in their original units. A unit-aware reporting redesign remains separate work. |
| Two recorder tests require the host to have 5 GiB free | Isolate their unrelated disk assumption in test fixtures; retain the production reserve and explicit storage-failure tests. |
| Legacy no-fill rows in descriptive floor reporting | The compatibility projection can count a malformed legacy row as observed. Strict authorization excludes it; do not treat this descriptive count as verified execution. Tightening legacy reporting remains separate work. |
| Bar published before its fresh quote can be priced by shadow | Reproduced ordering mechanism. Coordinated decision-time publication/deferral remains required; do not use future-observed quotes or relax freshness as a shortcut. |
| Raw IBR stop compared with tick-rounded floor | Reproduced false refusal. Version the admission correction; changing the existing registered arm in place would rewrite results. |
| Factory beta and shadow same-clock context are incomplete | Wire causal prior-session context with explicit data sufficiency; do not fabricate a baseline. |
| Expected round-trip cost and comparison IDs lack producers | Wire and bind producers in a new cost/evidence epoch. Protocol-2's 25-bps fallback remains active when expected costs are absent. |
| Preserved runtime safeguards | Integrated into the continuation branch with fault-injection and restart checks. Deployment status remains separate from local verification. |

### Runtime safety integration and operational recovery

The continuation integrates the preserved protection-only lane while paused,
requires both broker-flat and local-flat evidence, and persists close intent
before canceling protective legs. Missing orders, missing broker IDs and
unknown statuses retain the same durable client request for recovery. Legacy
stop restoration accepts only a confirmed live broker stop; ambiguous close
responses do not create a competing stop. Current-date and degraded-calendar
force-flat boundaries have dedicated regressions. Options remain outside the
shipped equity pilot; an equity recovery stop is not option protection.

Authorizing shadow entry, context, portfolio marks and replay require explicit
forward source provenance. Durable historical markers override row claims,
and declaration changes invalidate event caches and replay memos. The recorder
writes forward declarations only for new partitions, before their first append;
old unmarked files are not relabeled. Diagnostic compatibility remains separate.

The complete runtime shard passed 1,124 tests after integration. See
[validation](validation.md) for the final research checks and their scope.
These are synthetic broker tests, not reports of incidents in the real account.

VM storage recovery completed at 18:21 UTC. Deduplication preserved 126
identical inactive export paths, and relocation preserved all 412 archived
files on the existing persistent read-only backup disk. The final check
measured 26,985,652,224 free data-disk bytes, all six services healthy, and
unchanged paused/flat paper state and order history. The receipts and complete
manifest were copied off the VM and hash-verified. No protected backup or
trial evidence was discarded and no Azure resource was added.

This repairs capacity, not the historical data gap. The morning census had
zero accepted sessions and 24 rejected acceptance reports; lag and cadence
failures preceded the October 19 storage stop. New complete forward-session
acceptance and a reviewed VM source release remain outstanding. The frozen
ORB trial identity mismatch remains a separate activation blocker.

## Profitability work that is justified

There is no code change which can honestly guarantee a profitable strategy.
The project can repair distorted measurements and test a better-specified
hypothesis. The next research decision should be small and falsifiable:

From a trading perspective, the main mismatch is between small, unstable gross
intraday effects and the spread/slippage required to enter and exit. Public
consolidated bars also do not establish tradability on the deployed IEX
forward stream. Adding more filters, strategies, LLM tuning or proof layers
cannot repair that mismatch. The research surface has grown ahead of usable
forward observations. Freeze expansion and simplify to one experiment; retain
broker protection, reconciliation and durable close recovery as essential
execution controls.

1. Freeze one executable strategy, universe, feed, holding period, entry/exit
   policy and cost model. Use a single non-authorizing paper experiment to
   measure execution; stop expanding the LLM/family/options search meanwhile.
2. First establish a gross signal effect on causal, executable opportunities,
   against a clock-matched/factor-aware control. Report missing, refused and
   censored opportunities separately. Avoid selecting a good recent slice.
3. Charge the same frozen order-type/per-symbol cost assumptions in candidate,
   controls, replay and paper attribution. Journal decision-time mid, quote
   age, latency, requested size, fill and exit costs. Never double-charge a
   quote's already-paid spread or declare paper fills to be live impact data.
4. Require a net effect which survives cost stress, chronological validation,
   independent-day uncertainty and a sample sized for the proposed effect.
   The published 8.1-bps minimum-detectable-effect example is much larger than
   the few-bps gross effects being discussed; nominal trade floors alone do
   not solve power.
5. Retire a hypothesis when a clean test fails. Parameter changes, a new
   regime filter, passive-entry policy or overnight holding are new hypotheses
   with their own untouched tests. The reported overnight gross premium is
   a separate risk-premium/holding-period question, not an intraday patch.

What cannot be recovered by a repair: the lost forward observation interval,
an untouched status for already examined data, a live execution guarantee from
paper fills, or a positive edge from the current negative/underpowered results.

## Paper decision

The explicit fixed-incumbent paper exception permits an experiment before
validated alpha. Its 20 accepted sessions and 20 closed outcomes are minimum
review floors, not prerequisites to the first experimental trade. They also
do not themselves prove profitability.

Before the first paper session: repair storage/cadence and validate a complete
session; deploy the integrated safety work as a reviewed release; retire
the stale trial through the audited operator path; freeze the replacement;
and pass authenticated identity, freshness and flat-book preflight. A pilot
with the existing ORF profile is an execution experiment only. None of these
results supports live capital.

Current operational state belongs in [current-findings.md](../current-findings.md).
The [documentation catalogue](../README.md) records retired narratives and
historical evidence retained for reproducibility.
