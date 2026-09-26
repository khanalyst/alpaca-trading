# Strategy and deployment audit — 2026-09-26

The review inspected the implemented signal families, setup construction,
shared risk/cost calculation, shadow evaluation and persistence, VM databases,
broker GET responses, and saved numeric experiment outputs. Historical reports
below were parsed as data, not accepted as current-code proof. No strategy is
shown to have a deployable positive edge.

## What is actually negative

The September 24 broker inspection returned paper equity/cash of $99,999.92,
zero positions and zero working orders. The six returned filled orders were
three one-share SPY round trips on August 14. The current strategy journal had
zero trade rows. This eight-cent account deficit is not an ongoing strategy
drawdown. The exact cash reconciliation includes charges beyond the returned
fill prices; fees were not independently reconciled in this review.

The current research epoch is primarily **blocked**, not a measured losing
portfolio. On September 24 the shadow WAL contained 288,560 events (40,894 bars
and 247,666 quotes, all carrying provider `alpaca`). Both retained diagnostic
cohorts had 31 accounts, zero closed positions, and zero open positions. The
current cohort had stopped at 115,167 processed events per arm. The research
scheduler had accepted zero of six complete recorded partitions. By September
26 the shadow service remained unhealthy and the September 25 session was
also rejected.

Five completed close-only IBR replay trades existed on September 21–22:
zero-buffer +0.81747, ten-bps buffer +0.33852, and 45-minute range −0.53360
in total. These are replay quantities and do not represent broker trades or
validated forward account P&L. The 24 current rule arms had no completed
replay trades. Neither zero observations nor blocked execution means zero risk
or positive expectancy.

## Confirmed implementation defects repaired

1. `research/live_shadow.py::events_after_cursor` rejected the whole diagnostic
   backlog whenever it exceeded 20,000 rows. Every retry encountered the same
   backlog, so no arm could advance. It now returns a bounded cursor page.
   Session replay is deferred until that arm has consumed all recorded events
   for the session, including quotes and rejected provenance rows. Source
   ingestion and complete-session context bounds remain enforced.
2. `ShadowRunner._evaluate` received a SQLite envelope from legacy workers,
   but read provider/feed/observation time as if it were the embedded payload.
   The database contained 30,951 false `equity provider mismatch` decisions;
   a joined event showed `provider=alpaca` in `event_json` but null in the
   decision. Evaluation now decodes and validates the stored payload. Missing
   or mismatched payload identity still fails closed. This defect affected
   the legacy candidate path; diagnostic workers already passed payloads.
3. A paused trader had no persistent journal connection. SQLite removed WAL
   sidecars after short-lived readers/writers closed, making the dashboard's
   read-only mount report `OperationalError`. The supervisor now holds a
   query-only autocommit connection for its lifetime, including while paused.
   The dashboard retains normal WAL consistency; it does not use immutable
   reads or a writable mount.

## Shared economics and constraints

All rule families share the configured 25-bps stress scenario and maximum
stress-cost/risk ratio of 0.30. For shares this requires stop risk of at least
25/0.30 = **83.33 bps of notional**. A setup at the rule contract's 30-bps stop
floor has a ratio of 0.833 and is rejected. Smaller position sizing does not
solve this ratio because cost and stop risk scale together. The one-minute
ATR templates frequently produce stops below the required distance.

The historical 43-arm inventory from September 12 contains 36 rule variants
across 12 families and seven IBR arms. All 36 rule variants were execution
blocked. The table below counts stress rejections across each family's
variants, so counts are overlapping hypotheses, not independent trades.

| Rule family | Variants | Stress rejections | Code behavior and implication |
|---|---:|---:|---|
| Opening-range breakout | 6 | 1,158 | Completed close clears opening range. ATR/fixed-R exits need sufficient remaining range and follow-through. |
| Opening-range fade | 2 | 61 | Bar breaches the range then closes back inside; some reversion has occurred before entry. A fixed-R target may disagree with the fair-value thesis. |
| Momentum continuation | 2 | 397 | Lookback return plus latest close direction; repeated directional closes alone do not establish continuation after costs. |
| Mean reversion | 2 | 442 | Current close participates in mean/std calculation. The default target is fixed-R rather than an explicit return to the mean. |
| Trend pullback | 6 | 1,101 | Fast/slow averages, proximity and current candle direction; the legacy predicate does not require a separately observed prior retracement. |
| Volatility breakout | 2 | 402 | Prior range compression and range break; fixed-bps compression behaves differently across volatility regimes. |
| Volume breakout | 2 | 256 | Range break plus volume versus prior rolling mean; baseline does not normalize volume by time of day. |
| VWAP reversion | 6 | 1,194 | Deviation from session VWAP; fixed-R roots do not necessarily exit at VWAP. Later grammar supports VWAP targets. |
| VWAP trend | 2 | 305 | Current cumulative session VWAP versus earlier cumulative VWAP; the comparison's effective horizon changes through the day. |
| Range expansion | 2 | 408 | Current bar expands versus recent ranges. `volume_multiplier` supplies the range multiplier in this family; naming is misleading but changing it requires a versioned contract. |
| Opening drive | 2 | 221 | Opening-window displacement plus current candle direction; late confirmation can enter after much of the move. |
| Cross-sectional residual | 2 | 386 | Subject return minus synchronized SPY return, expressed as an outright position; it is not a beta-hedged spread. |

These behavioral observations come from `agent/contracts/rule.py`,
`agent/strategy.py`, and `research/factory_core.py`. They are hypotheses to
test, not proof that a parameter change will make a family profitable. The
release preserves existing strategy definitions and cost/risk gates.

## Historical loss evidence

The saved September 12 cost-gate counterfactual covers 30 historical sessions
and four variants each of ORB, trend pullback, and VWAP reversion. Raising the
ratio limit from 0.30 to 1.0 admitted 791 variant-trades:

- All 12 variants had negative net P&L.
- Pooled descriptive mean was −0.4580R/trade; session-block bootstrap interval
  was [−0.5754R, −0.3486R]. This is not one deployable account or independent
  evidence across variants.
- Gross P&L at modeled fill prices was −$26,336.12; net after fees was
  −$28,276.85. Modeled spread/slippage are already in fill prices, so the gross
  figure must not be described as cost-free signal performance.
- Exits: 381 time, 212 stop, 198 target. Frequent time exits show that the
  nominal target-R setting does not describe the realized payoff distribution.
- ORB variants averaged −0.235 to −0.398R; trend pullback −0.353 to −0.459R;
  VWAP reversion −0.560 to −0.746R.

This was a historical, path-dependent diagnostic, not a fresh run on this
release. It does not justify relaxing the production cost gate.

The separate legacy IBR comparison used normalized replay quantities:

| IBR variant | Trades | Net replay P&L |
|---|---:|---:|
| Baseline | 31 | −17.4323 |
| Zero buffer | 23 | −9.7184 |
| 10-bps buffer | 34 | −15.4924 |
| 30-minute range | 40 | −33.3015 |
| 45-minute range | 42 | +1.0554 |
| 1.5R target | 31 | −16.2313 |
| 3R target | 31 | −17.4323 |

The runtime-equivalent IBR analysis explicitly rejected this historical
source as non-forward evidence. The one small positive legacy arm is neither
a validated winner nor comparable to the risk-sized rule-account totals.

## Remaining blockers and validation

- The persisted paper trial has a frozen identity from an earlier release.
  `main.py status` raises `active paper trial identity/config changed`.
  Deployment must preserve operator pause. Resumption requires the existing
  audited trial transition and fresh flat-account checks; this audit does not
  rewrite the incumbent or enable entries.
- Freshness and coverage remain limiting. September 25 acceptance recorded
  median recorder intervals near 60 seconds despite a 30-second configuration,
  frequent stale quotes/bars across the 24-symbol IEX universe, and no accepted
  shadow coverage. Software recovery alone cannot manufacture complete past
  observations. Do not lower freshness/evidence requirements to mark them good.
- A code change creates a new diagnostic cohort identity. Retain old evidence
  and collect post-activation observations; recovered historical data cannot
  be relabeled as a new prospective sample.
- Options were not activated: the deployment is shares/equity with indicative
  options configured. The review establishes no executable options edge.

Verification before deployment: 2,173 baseline tests passed. The changed code
passed 97 targeted shadow, provenance, backlog, supervisor, WAL and status
tests, including a 20,001-event cursor case, partial-session replay deferral,
restart/idempotency, and direct-versus-stored-event evaluation parity. Python
compilation and `git diff --check` passed.

Local audit receipts are in `outputs/strategy-audit-2026-09-24/`, including
`status-before.json`, `shadow-before.json`, and
`historical-strategy-evidence.json`. These private operational outputs are
excluded from the image and from this source commit. Deployment verification
is recorded separately after rollout.
