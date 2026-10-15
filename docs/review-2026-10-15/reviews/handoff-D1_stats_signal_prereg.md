# Review handoff: D1_stats_signal_prereg

Source: workflow structured result (reviewer could not write files; this file is generated from the journal).

## Files read

| file | lines | read to end |
|---|---|---|
| research/__init__.py | 24 | yes |
| research/stats.py | 1387 | yes |
| research/signal_quality.py | 1389 | yes |
| research/preregistered.py | 568 | yes |
| research/proof.py | 169 | yes |
| research/proof_payload.py | 630 | yes |
| research/maturity.py | 21 | yes |
| research/trial.py | 523 | yes |
| research/calibration.py | 587 | yes |
| research/protocol.md | 732 | yes |

## What this part does (as implemented)

Scope: the statistics library, the signal-quality instrument, the two preregistered hypotheses, the paper-trial verdict, execution calibration, proof artefacts and the research protocol. I also read docs/preregistered-hypotheses.md (118 lines, not assigned) in full for comparison.

stats.py:
- student_t_sf (306-364): regularised incomplete beta (Lentz continued fraction). Verified against closed forms for df 1 to 4 (worst relative error 4.2e-11) and against tail integration for df 5 to 59 (9.7e-15). sf(t_0.975,df) = 0.0250000 for df 1, 2, 5, 10, 17, 29 and 59.
- cluster_robust_mean_inference (367-412): CR1 with factor G/(G-1), df G-1. It equals an independent sandwich estimate to 10 decimals and reduces to stdev/sqrt(N) for singleton clusters.
- One-sided sign-flip: _sign_flip (225-254) enumerates exactly for 16 or fewer clusters and otherwise uses seeded Monte Carlo with p = (k+1)/(B+1). It matched brute-force enumeration (0.130859). paired_cluster_sign_flip (1277-1307) buckets by timestamp // cluster_seconds; gates pass 86,400. sign_flip_null_statistics (257-303) is Monte Carlo only.
- Bootstraps: an iid cluster bootstrap (415-490, no production caller) and a circular moving-block cluster bootstrap (493-614).
- Diagnostic-only reports: MDE/power (617-758), effective breadth (907-1021), cross-family dependence (1126-1266) and the frozen dependence map (49-196).
- BH and BY (1310-1364): BY multiplies BH-adjusted p by c(m). Rejections and adjusted p matched the step-up definition (max difference 1.1e-16).
- No binomial-interval code exists in research/ or agent/ (grep for wilson, clopper, binom found nothing).

signal_quality.py:
- One event per symbol-session: the first causal signal (_first_event, 473-606).
- The outcome runs from the signal-bar close to the close horizon bars later (576-586, 609-636, 1167-1173), with no costs unless cost_hurdle_bps is passed.
- The control (723-808) is the same symbol, same rounded session minute and same direction, averaged over every other session in the corpus, including later ones. It falls back to same-session tiers when no cross-session match exists (756-768). Using later sessions is not look-ahead on the candidate's own outcome, but the control cannot be computed in real time and is not a tradeable quantity.
- Per horizon it reports event-level t, session-clustered CR1 t/df and a Monte Carlo session sign-flip p (1231-1273). Session clustering correctly absorbs the 24 correlated ETFs firing in one session.
- Admissibility for session-anchored families needs a gap-free prefix from the first bar (feature_window_bars returns None, rule.py:376-379; see A-08).

preregistered.py, per hypothesis:
- vwap-reversion-control-adjusted.v1: registered 2026-09-22, sealed window after 2026-09-21. Subject rule.vwap-reversion.ab97bfe87ed566de: vwap_reversion, side both, threshold 25 bps, lookback 20, stop_atr 1.0 (30 bps floor), target 1.5R, max hold 90, entry window 0 to 390 minutes, no confirmation (45-55). Mirror: vwap_trend, threshold 8, lookback 15, volume confirmation, 2R (57-67).
- opening-range-fade-control-adjusted.v1: registered 2026-09-23, sealed window after 2026-09-23. Subject rule.opening-range-fade.d5785d9e70b56def: range 20 minutes, threshold 8 bps, lookback 15, 1.5R, hold 90, side both (71-81). Mirror: opening_range_breakout, range 15, threshold 5, volume confirmation, 2R (83-93).
- Shared endpoint: 60-minute candidate-minus-control return.
- Looks: exactly the first 30 and first 60 sealed session dates (PROTOCOL_QUALIFICATION_MIN_CLUSTERS = 30, gates.py:122), one-sided alpha 0.025 each (98, 179-184, 456-471).
- Pass: CR1 p < alpha AND sign-flip p < alpha AND point-estimate mean >= 3.0 bps (97, 410). The hurdle applies to the point estimate, not to a bound.
- Futility: P(mean < 3) has p < alpha (406, 412-414).
- Inconclusive: fewer than 30 matched controls, or coverage below 80% (99-100, 388-391), plus a registered control-tier check (105, 392-398).
- Decision eligibility comes from the strict source preflight (497-526). Both manifest hashes still equal the documented values.

trial.py:
- Defaults are 20 sessions, 20 trades and a 0R floor (43-68).
- The verdict (71-173) uses a 1,000-draw moving-block bootstrap with blocks of up to 5 sessions (235-239). Pass: total and mean R > 0 and the 5th percentile > 0. Fail: total and mean <= 0 and the 95th percentile < 0.
- A fail demotes the candidate and records a lesson (368-437). A pass appears in promotable_report with a config snippet (440-490).

calibration.py:
- Loads trades plus the latest order snapshots (97-190) and scores fill against the journal reference in bps (223-261), aggregated per order (264-302).
- Verdict: compared with model.entry_cost_bps (8 bps by default) after 20 fills (442-523). Partial-fill veto (320-362).

proof.py and proof_payload.py: a deterministic, secret-filtered payload with strict run authorisation when verified-gate evidence exists (209-241, 436-600). The artefact is created with O_EXCL (proof.py:124-153), with an optional HTTPS webhook (50-113).

maturity.py: a one-line pass-through to agent.contracts.rule.causal_maturity_bars.

protocol.md: narrative. Its LORD++ formula (543-560) matches Ramdas et al. 2017 and factory_ledger.py:319-354.

## Findings

### D1-01 Preregistration freezes manifest text only: decision rule and measurement instrument changed after sealed data were seen
- Severity: HIGH  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/preregistered.py:15-17, 112-119, 203-205, 392-398; commits c674f2f and 09e43d9; research/signal_quality.py; agent/contracts/rule.py`
- Evidence: research/preregistered.py:15-17: Changing anything in a manifest changes its hash; ``tests/research/
test_preregistered.py`` pins the hash, so an edit fails the suite and has to
be made as a new hypothesis version rather than silently rewriting this one.
research/preregistered.py:395-396: # Conformance with the sealed manifest, added after sealing: this can
        # only turn a pass into inconclusive, never the reverse.
- Why it matters: Sequence of events:
- The VWAP hypothesis was committed in c674f2f at 2026-09-22 21:27:48 UTC.
- 53 minutes later, commit 09e43d9 (22:20:02 UTC) added the control-tier decision rule to look_outcome.
- That same commit recorded docs/audit-2026-09-21/prereg-interim-2026-09-22.json: the interim on sealed session 2026-09-22, with primary_60m delta +6.95 bps and control_matching_counts {"same_session_minute_band": 19}.

The hash cannot see any of this. The VWAP manifest's underpowered_control text (203-205) does not contain the tier rule, so its hash did not change (verified: both hashes still equal the values in docs/preregistered-hypotheses.md). _frozen (112-119) checks only the variant id.

Since registration, git diff --stat c674f2f HEAD shows:
- 328 changed lines in research/signal_quality.py, the decision instrument, including its decision-time and outcome-availability logic;
- 672 changed lines in agent/contracts/rule.py, the evaluator of the frozen spec.

A look-1 result will therefore be a test of whatever code is checked out on the day, partly chosen after unblinded data, not of the hypothesis as sealed.
- Suggested fix: Register v2 hypotheses with a fresh sealed window that starts after the last examined session. Put a content digest of the instrument into the manifest: research/signal_quality.py, research/stats.py, look_outcome and the family evaluator in agent/contracts/rule.py, or the registration commit. Make evaluate() refuse decision-bearing output when the running code digest differs. Do not commit unblinded interim results before look 1.
- How verified by reviewer: Read preregistered.py in full. Ran git log and git log -S REGISTERED_CONTROL_TIER on research/preregistered.py. Inspected the committed interim JSON. Ran preregistration() and compared both manifest hashes with the documentation (both True). Ran git diff --stat c674f2f HEAD over the instrument files.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### D1-02 Control-tier rule voids a whole look on a single fallback control and pre-empts the futility rule
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/preregistered.py:392-398, 412-414; research/signal_quality.py:756-768`
- Evidence: research/preregistered.py:392-394: tiers = {tier for tier, count in summary["control_matching_counts"].items()
             if count}
    if tiers and tiers != {REGISTERED_CONTROL_TIER}:
- Why it matters: One candidate whose minute has no admissible cross-session match falls back to a same-session tier (signal_quality.py:756-768). That makes the entire look inconclusive, when the candidate should simply be dropped and counted against the 80% coverage rule.

For vwap_reversion and opening_range_fade, admissibility needs a gap-free IEX prefix from the open (A-08). A single thin symbol can therefore void every look, and the hypothesis retires as not_established by default.

The tier check also runs before the futility branch, so it converts futility into inconclusive, contrary to the code comment. Demonstration, with 120 matched controls, mean -20 bps, SE 4 bps and df 29:
- all controls in the registered tier: futility (mean_credibly_below_hurdle);
- 1 of 120 in same_session_minute_band: inconclusive (control_tier_not_registered).
- Suggested fix: In a v2 registration, exclude non-registered-tier candidates from the look statistics and count them as unmatched for the coverage rule. Evaluate futility independently of control conformance. Correct the comment.
- How verified by reviewer: Ran research.preregistered.look_outcome on the two metric dicts above with alpha 0.025 and hurdle 3.0. Read the fallback path in signal_quality._control_return.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### D1-03 The 3.0 bps 'economic hurdle' is a kill threshold applied to a pre-cost, control-adjusted point estimate; it never binds and is far below the repository's own round-trip cost
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `research/preregistered.py:97, 189-196, 410, 440-441; docs/preregistered-hypotheses.md:41-43; research/costs.py:34-42, 677-679`
- Evidence: research/preregistered.py:410: if p_zero < alpha and float(flip_p) < alpha and mean >= hurdle_bps:
docs/preregistered-hypotheses.md:42-43: Significance alone never passes: a real but
  sub-hurdle effect is not worth trading.
- Why it matters: Where the 3 bps comes from: dc156ca:docs/audit-2026-09-21/FINDINGS.md:360-361 reads "Predeclared kill criterion: if no arm shows a control-adjusted mean above +3 bps". That is a floor below which the grammar has no edge at all, not a tradeable threshold.

The repository's own costs are far higher:
- static model: 4 bps spread, 6 bps slippage and 0.5 bps fee per side (costs.py:34-42), i.e. 2 x (8.0 + 0.5) = 17 bps per round trip;
- protocol.md:145-146 sets 25 bps as the authorisation stress.

The endpoint is not P&L either. It is measured from the signal-bar close with no cost (evaluate() passes no cost_hurdle_bps, 440-441), and it is candidate minus a clock control, not the result of the 30 bps stop and 1.5R/2R bracket.

The hurdle never binds. Using the manifest's own in-sample figures (19.5 bps delta, clustered t 2.95, df 17, so SE about 6.6 bps over 18 sessions; 240-241), scaled by 1/sqrt(G):
- a pass needs mean > 10.47 bps at look 1 and > 7.24 bps at look 2;
- P(pass) is 0.036 at a true delta of 0, 0.150 at 3 bps, 0.508 at 7 bps and 0.997 at 17 bps;
- at delta 0, P(not established) is 0.813 and P(futility) 0.151.

A pass is therefore compatible with an effect that loses money after the repository's own costs.
- Suggested fix: Label 3 bps as a necessary condition only. Alternatively, in v2, replace it with an after-cost hurdle on a lower confidence bound of an executable return: anchored at the next-bar open or the decision-time quote, and net of the cost model.
- How verified by reviewer: Read the code and costs.py, and ran git show of the FINDINGS kill criterion. Pass and futility thresholds were computed with t quantiles 2.0452 and 2.0010. Power comes from 1,000,000 simulated two-look outcomes; the power figures assume the in-sample SE scales as 1/sqrt(G). The sign-flip condition was ignored, which only makes passing harder.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### D1-04 Looks count any date with one sealed bar as a session, and nothing records that a look was spent
- Severity: MEDIUM  |  Category: incomplete  |  Confidence: LIKELY
- Location: `research/preregistered.py:185-188, 434-436, 456-463, 539-550`
- Evidence: research/preregistered.py:436: sessions = sorted({_session_of(bar) for bar in sealed})
research/preregistered.py:462: window = set(sessions[:needed])
- Why it matters: There is no completeness test: no calendar, no minimum bar count, no per-symbol coverage. Each of these counts as one of the 30 or 60 sessions:
- a recorder-outage day;
- an early close;
- the session in progress, if the command is run during market hours. The docs run it against the live recorder directory (docs/preregistered-hypotheses.md:95-97).

Running on the day the 30th session trades and again after the close gives two different look-1 results. Every run writes a new file (539-550) and nothing records that look 1 was evaluated, so the manifest claim that "a result cannot be improved by choosing when to look" (186-188) does not hold. The protocol's own floors are stated in complete sessions (protocol.md:33-36).
- Suggested fix: Define a session as a calendar-complete regular session (exact Alpaca calendar metadata plus a minimum coverage per symbol) and exclude the current date. Persist an append-only look record (look number, session-list digest, code digest, result) that later runs must reproduce rather than re-decide.
- How verified by reviewer: Read evaluate() end to end. Found no session-completeness filter in research/edge_discovery_core.py::_read_discovery_rows (lines 371-460) or, by grep, in research/source_validation.py. UNVERIFIED: whether the VM recorder directory exposes the in-progress partition.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### D1-05 Signal-quality outcome is anchored at the signal-bar IEX close, not at an executable price
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: LIKELY
- Location: `research/signal_quality.py:576-586, 1167-1173, 1328; research/protocol.md:89`
- Evidence: research/signal_quality.py:577-579: # fill.  Anchor the return to the completed signal close, which is
        # observable at ``signal_end`` even when the next bar's opening print
        # was delivered only with its completed OHLC record.
research/signal_quality.py:1328: "return_basis": "signal_bar_close_to_contiguous_horizon_close",
- Why it matters: Both registered subjects are fade or reversion rules. They fire because the signal bar closed at an extreme relative to VWAP or the opening range.

That close is the last IEX print of the minute (IEX is about 2% of volume). A print at the bid or ask, or a stale print, mechanically reverts toward the mid in the next minute. The clock-matched control is an unconditional bar at the same minute and carries no such conditioning, so the bounce is credited to the predicate.

The runtime cannot capture that bounce: it acts on the next poll and enters at the IEX ask or bid (B-02 context). The protocol's own replay invariant says "entries occur on the next bar, never on the signal bar" (protocol.md:89). Against a 3 bps hurdle, or the 7 to 10 bps effective threshold, a half-spread of 1 to 3 bps on HYG- or SLV-priced ETFs is material.
- Suggested fix: Decide on a return anchored at the next bar's open or at the decision-time quote side, with a one-bar-skip variant. Keep the close-anchored figure as a secondary diagnostic.
- How verified by reviewer: The anchoring was verified by reading _first_event, _forward_return and the per-horizon loop. The size of the bounce on this corpus is unverified, because this container has no corpus access.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### D1-06 Calibration authorisation cannot veto equity entry costs, because the journal reference is the limit price itself
- Severity: MEDIUM  |  Category: broken  |  Confidence: VERIFIED
- Location: `research/calibration.py:131-190, 236, 344, 443, 494-506; agent/market_entry_risk.py:84-86, 598-605, 826-831; agent/execution_lifecycle.py:401-404, 595-598`
- Evidence: research/calibration.py:236: adverse = (fill - reference) if side == "buy" else (reference - fill)
research/calibration.py:494-495: verdict = ("insufficient_data" if mixed or len(measured) < MIN_FILLS
               else "optimistic" if mean_bps > expected else "conservative")
- Why it matters: How the reference is set for shares:
- the plan's entry_price is overwritten with the IEX ask (long) or bid (short) (market_entry_risk.py:84-86, 598-605);
- the entry limit is that same quote side, rounded the same way (826-831);
- the journal reference falls back to plan entry_price, because no entry_reference or reference_price is ever set (execution_lifecycle.py:401-404, 595-598).

A DAY limit fills only at or better than its limit, so every entry's adverse_bps is <= 0. The comparator is entry_cost_bps = spread/2 + slippage = 8 bps (costs.py:677-679). That includes the half-spread, which the measured value excludes.

Orders that never fill are invisible: only trades rows are loaded (131-190), and underfill is counted only when 0 < fraction (344).

Demonstration: 24 entries filled at or better than the limit gave verdict conservative, authorization authorized, observed mean -0.333 bps against 8.0 bps. The veto described in protocol.md:167-174 therefore always passes once 20 fills exist, and the real costs of limit-at-touch entry (missed fills, adverse selection) are never measured.
- Suggested fix: Journal the decision-time mid and the signal reference. Score fills against the mid, spread included, and against the signal close. Count unfilled or expired entry orders from the orders table as missed fills with an opportunity-cost measure. Compare like with like.
- How verified by reviewer: Read calibration.py in full and traced the runtime writers in market_entry_risk.py and execution_lifecycle.py; grep found no other setter of reference_price or entry_reference in agent/. Ran calibrate() on synthetic fills at or better than the limit.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### D1-07 Trial verdict re-tests a small-sample bootstrap bound at every review; about 18% of zero-edge candidates become 'promotable'
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/trial.py:43-46, 142-168, 235-239, 440-490; deploy/dashboard.py:800-802; research.py:756`
- Evidence: research/trial.py:237-239: bound = moving_block_cluster_bootstrap_lower_bound(
            values, clusters, confidence=.95, draws=1000,
            block_length=max(1, min(5, cluster_count - 1)), min_clusters=2)
research/trial.py:163-164: if positive_economics and lower_bound > min_mean_r:
        return {"state": "passed", **common}
- Why it matters: At the default 20 sessions, a circular bootstrap with 5-session blocks under-estimates the variance of the mean, and the percentile bound is applied to skewed bracket R. The verdict is recomputed on every CLI review and every dashboard render, with no sequential adjustment.

Simulated under zero edge (R = +2 with probability 1/3, -1 with probability 2/3; 1 to 3 trades per session), calling trial._verdict and the repository bootstrap exactly as _session_cluster_confidence does:
- single look: P(passed) 0.100 at G=20 (400 reps) and 0.060 at G=40, against a nominal 0.05;
- daily reviews from 20 to 60 sessions: 18.5% are 'passed' at least once before being parked, and 25.6% are parked (1,000 reps with an equivalent numpy bootstrap; its single-look rate was 0.077).

'Passed' feeds promotable_report with a ready-to-paste config snippet (484-488). Separately, the [5th, 95th] percentile pair is described as a "95% session-cluster interval" (158-159); it is a 90% two-sided interval.
- Suggested fix: Decide once, at a preregistered session count, or use an alpha-spending or always-valid boundary. At small G, use a CR1 session t or a sign-flip test in place of the 5-block percentile bootstrap. Persist the first terminal verdict.
- How verified by reviewer: Monte Carlo simulation as described, using research.trial._verdict, trial_policy(None) and research.stats.moving_block_cluster_bootstrap_lower_bound. The assumptions (null R distribution, trades per session, daily reviews) are stated above.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### D1-08 LORD++ v6 with gamma_k = 1/(k(k+1)) starves the live authorisation lane after a few tests
- Severity: HIGH  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `research/protocol.md:543-560; cross-group: research/factory_ledger.py:62-67, 301-354; research/live_shadow_ingest.py:64-70, 1131-1134`
- Evidence: research/protocol.md:546-547: an explicit initial wealth `W0` and the balanced sequence
`gamma_k = 1/(k*(k+1))` for `k >= 1`. If `tau_j` is the test index of the
research/factory_ledger.py:305: return 1.0 / (int(index) * (int(index) + 1))
- Why it matters: The formula matches Ramdas et al. 2017 and the code: _fdr_allocation_v6 returns exactly 0.025/(t(t+1)) before any discovery. The problem is the decay. It falls as 1/t^2, and one scope is shared by all candidates and cycles and persists.

Required one-sided z before any discovery, and the per-session Sharpe that implies over a 30-session confirmatory window:
- test 1: z 2.24, Sharpe about 0.41;
- test 5: z 3.14, about 0.57;
- test 10: z 3.51, about 0.64;
- test 20: z 3.85, about 0.70;
- test 50: z 4.27, about 0.78.
That is roughly 6.5 to 12 annualised.

The Monte Carlo iteration count is scaled to resolve the allocation (live_shadow_ingest.py:64-70), so the budget itself is the blocker, not resolution. Realistic intraday ETF edges cannot pass after the first few tests, so a 'no edge' outcome from this lane is uninformative. It fails closed: no money is lost, but the research-to-deployment path is effectively shut.
- Suggested fix: Use a slowly decaying gamma, for example the Javanmard-Montanari sequence proportional to log(max(j,2))/(j*exp(sqrt(log j))). Alternatively, use alpha-investing or a Bonferroni split over a preregistered, bounded number of confirmatory tests per epoch. Document the required z for each test index in the protocol.
- How verified by reviewer: Computed the allocations with research.factory_ledger._fdr_allocation_v6 for t = 1..100, compared them with 0.025/(t(t+1)) and converted them with NormalDist.inv_cdf. Read _confirmatory_iterations and the record_fdr_decision call site.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### D1-09 paired_cluster_sign_flip default of 21,600 seconds splits every New York session into two clusters
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/stats.py:1277-1293`
- Evidence: research/stats.py:1279: cluster_seconds: int = 21_600, exact_max_clusters: int = 16,
research/stats.py:1292: clusters.setdefault(int(timestamp // cluster_seconds), []).append(delta)
- Why it matters: The 6-hour UTC buckets [12:00, 18:00) and [18:00, 24:00) both cover part of a 09:30 to 16:00 ET session. The default therefore doubles the apparent number of independent clusters and overstates significance. All production callers pass CLUSTER_SECONDS = 86_400 (gates.py:104, 1259, 4315), so this is a latent hazard only.
- Suggested fix: Remove the default and have callers pass explicit session identities, or cluster by New York session date.
- How verified by reviewer: Four timestamps from 2026-09-24, 09:45 to 15:30 ET, give 2 clusters with the default and 1 with 86,400. Callers checked by grep.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### D1-10 write_proof creates the final path before writing, so a crash leaves a truncated artefact that blocks every retry
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/proof.py:137-153`
- Evidence: research/proof.py:139: descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
research/proof.py:142-143: if target.read_bytes() != data:
            raise RuntimeError(f"existing proof artifact does not match payload: {target}")
- Why it matters: A crash or full disk between create and fsync leaves a partial file. Every later attempt to write the same payload then raises RuntimeError, blocking that candidate's proof and webhook until someone removes the file by hand.
- Suggested fix: Write to a temporary file in the same directory, fsync it, then os.link it to the final name (link fails if the target exists).
- How verified by reviewer: Read write_proof end to end.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### D1-11 Proof payload reports 1 - q_value as 'confidence'
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/proof_payload.py:408-413, 591`
- Evidence: research/proof_payload.py:411-413: confidence = metrics.get("confidence", metrics.get("ci_low"))
    if confidence is None and q_value is not None:
        confidence = 1.0 - float(q_value)
- Why it matters: The same key holds either a lower bound (ci_low) or one minus an FDR-adjusted p. A reader of the artefact can take, for example, 0.97 as the probability that the edge is real. Presentation only.
- Suggested fix: Publish q_value and the lower bound under their own names and drop the derived 'confidence'.
- How verified by reviewer: Read _metrics and build_proof_payload.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### D1-12 Protocol says stale calibration vetoes, but calibration.py has no notion of age
- Severity: LOW  |  Category: doc-drift  |  Confidence: VERIFIED
- Location: `research/protocol.md:167-174; research/calibration.py:401-524`
- Evidence: research/protocol.md:170-173: Missing,
stale, or insufficient calibration, an optimistic cost verdict, a terminal fill
below 80% of requested quantity, or a partial-cancel rate above 20% returns a
veto and non-zero status.
- Why it matters: calibrate() takes no clock or as-of and compares no timestamps against now. A journal whose fills are months old authorises exactly as a fresh one does. Together with D1-06, an 'authorized' status says nothing about current execution.
- Suggested fix: Add as_of and maximum-age parameters that fail closed on old evidence, or correct the protocol.
- How verified by reviewer: Read calibration.py in full; it contains no age comparison. The live_shadow 'stress_calibration' staleness check is a different artefact.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### D1-13 Candidate horizon can stretch past 60 minutes after a missing minute, while the control requires 61 contiguous bars
- Severity: LOW  |  Category: incorrect  |  Confidence: LIKELY
- Location: `research/signal_quality.py:537-541, 622-626, 692-694, 1167-1173`
- Evidence: research/signal_quality.py:538-541: if _timestamp(next_row) != signal_end and decision <= signal_end:
            status_counts["entry_not_adjacent"] += 1
            continue
        entry_at = signal_end if decision <= signal_end else decision
- Why it matters: For forward-recorded rows, availability includes the wall-clock observation time (research/market_data.py:258-267). Decision is therefore later than signal_end, and the adjacency test is skipped.

The candidate's outcome then only needs bars signal_index+1 to signal_index+60 to be contiguous with each other (622-626). A missing IEX minute right after the signal stretches the clock span, while the control needs 61 contiguous bars starting at the signal bar (692-694). The result is a small, systematic mismatch between candidate and control on thin symbols.
- Suggested fix: Require rows[signal_index+1] to start at signal_end for the outcome window regardless of decision latency, or define the horizon by clock time.
- How verified by reviewer: Read _first_event, _forward_return and _control_indices. Recorder observed_at falling after the bar end is taken from the module comments (signal_quality.py:184-191) and the protocol; it is not checked against data.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

## Over-engineering

About 650 of research/stats.py's 1,387 lines are diagnostics their own docstrings call non-authorising:
- clustered_mde_power_report and its aliases (617-766, about 150 lines);
- effective_breadth_report with _coerce_breadth_observations (four input shapes) and a hand-written Jacobi eigen-solver (769-1021, about 250 lines);
- cross_family_dependence_report with _family_session_observations (1024-1274, about 250 lines).

At current volumes (about 17 sealed sessions, 24 symbols) none of these can change a decision. A 24 x 24 correlation spectrum needs far more complete sessions to be stable, and deterministic_dependence_map (49-196) needs at least 2 prior cycles, 20 sessions and |r| >= 0.8 before it does anything.

Dead or duplicate surface:
- stats.py carries 10 function aliases and 3 constant aliases (36-38, 201-203, 764-766, 1271-1274). The three dependence-map aliases have no production caller.
- The iid cluster_bootstrap_lower_bound path (432-490) has no production caller.
- Results repeat the same number under several keys: breadth / effective_breadth / participation_ratio; missing_data / missing; mean_absolute_correlation / mean_abs_correlation. proof_payload.py:583-585 emits five keys for one schema value, and 'strategy' duplicates 'strategy_id'.
- maturity.py (21 lines) and signal_quality._mature_prefix (251-254) are two pass-through layers over agent.contracts.rule.causal_maturity_bars.

signal_quality.py spends about 135 lines on eligibility-provenance bookkeeping (300-433) and about 110 lines validating a precomputed hand-off (850-959), all for a diagnostic that cannot authorise. proof.send_webhook introspects sender signatures and runs a thread (77-111) mainly to accommodate test fakes.

The three components that should carry decision value behave as follows:
- trial verdict: too permissive under repeated looks (D1-07);
- calibration authorisation: vacuous (D1-06);
- online FDR: too strict to ever pass a realistic edge (D1-08).

Simplify to:
- one session-level t / sign-flip test at fixed horizons;
- a calibration that scores fills against the mid and counts missed fills;
- a bounded-budget alpha split.

## Cross-group questions

- D2/F3 (research/live_shadow_ingest.py:1131-1134, research/factory_ledger.py::record_fdr_decision): how many tests have already been recorded in the shadow-confirmation-v6 scope on the VM? From test 10 without a discovery, any candidate needs one-sided z >= 3.51.
- B_execution (agent/execution_lifecycle.py:401-404, 595-598, 1933-1934): confirm that this is the only path writing entry_reference/reference_price into the trades journal for shares. What is exit_reference for bracket stop and target fills (trigger price or decision quote)?
- H1 recorder (research/edge_discovery_core.py::_read_discovery_rows, research/source_validation.py::validate_source): does the forward corpus directory expose the in-progress session's partition during market hours? If so, research.preregistered counts it as a session.
- A_signals (agent/contracts/rule.py:376-379, feature_window_bars returns None): on the IEX corpus, what share of symbol-sessions has a gap-free prefix to the typical first-signal minute for vwap_reversion and opening_range_fade? This sizes the risk that one fallback control voids a preregistered look (D1-02).
- H2b dashboard (deploy/dashboard.py:797-802): promotable_report is recomputed on every render with no persisted 'first passed' state. Can an operator pin a candidate from a transient 'passed' verdict?
- J2 tests (tests/research/test_preregistered.py): is anything pinned besides the two manifest hashes, for example a digest of signal_quality.py, look_outcome or the vwap_reversion/opening_range_fade evaluators?
- D2 gates (research/gates.py:1597, 4322, 4367): sign_flip_null_statistics is Monte Carlo only. Confirm the draws passed are at least 10,000, as protocol.md:603-605 states, and that the authorising p is paired_cluster_sign_flip with CLUSTER_SECONDS = 86,400.

## Summary

- HIGH D1-01: the preregistrations are not frozen. The manifest hash covers text only. A control-tier decision rule was added 53 minutes after the VWAP registration, in the commit that recorded the first sealed-session interim. Since registration, 328 lines of the signal_quality instrument and 672 lines of the rule contract have changed with the hash unchanged.
- HIGH D1-08: LORD++ v6 is mathematically correct (formula matches Ramdas et al. 2017 and the code), but with gamma_k = 1/(k(k+1)) and W0 = 0.025, alpha_t = 0.025/(t(t+1)). With about 30 sessions the 10th confirmatory test needs z 3.51 (per-session Sharpe about 0.64), so the live authorisation lane is effectively closed to realistic edges.
- MEDIUM D1-03: the 3.0 bps hurdle is the audit's kill threshold, applied to the point estimate of a pre-cost, close-anchored, control-adjusted return. The repository's static round trip is 17 bps. Pass thresholds implied by the looks are 10.5 and 7.2 bps, so the hurdle never binds. P(pass) is 0.15 at a true 3 bps and 0.997 at 17 bps.
- MEDIUM D1-02: one fallback control voids a whole look and turns futility into inconclusive; demonstrated with look_outcome. The VWAP manifest does not contain this rule.
- MEDIUM D1-06: calibration authorisation is structurally always 'conservative' or 'authorized' for equity entries. The journal reference is the limit price (the IEX ask or bid), unfilled orders are invisible, and the observed metric excludes the spread that the 8 bps comparator includes.
- MEDIUM D1-07: under a zero-edge null the paper-trial verdict says 'passed' in 7.7 to 10% of single looks at 20 sessions. With daily re-review to 60 sessions, 18.5% of zero-edge candidates show as promotable at some point.
- MEDIUM D1-05 and D1-04: signal quality anchors returns at the signal-bar IEX close, which favours fade and reversion rules through bid-ask bounce. Preregistered looks count any date with one bar as a session, and no record is kept that a look was spent.
- The core statistics verify numerically. Student-t sf: error at most 4e-11 against closed forms and tail integration, with exact 0.025 at the t quantiles for df 1 to 59. CR1 matches an independent sandwich. Exact sign-flip matches brute force. BH and BY match the step-up definition. No binomial-interval code exists in research/ or agent/.
- LOW D1-09 to D1-13: the 21,600 s sign-flip default splits a session (no production caller uses it); the proof artefact write is non-atomic; 'confidence' is reported as 1 - q; the protocol claims a stale-calibration veto that does not exist; the candidate and control horizons differ slightly after a missing minute.
- Over-engineering: about 650 of stats.py's 1,387 lines are non-authorising diagnostics that cannot change a decision at current volumes (about 17 sealed sessions, 24 symbols).
