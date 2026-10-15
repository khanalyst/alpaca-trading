# Review handoff: A_signals

Source: workflow structured result (reviewer could not write files; this file is generated from the journal).

## Files read

| file | lines | read to end |
|---|---|---|
| agent/contracts/__init__.py | 48 | yes |
| agent/contracts/rule.py | 2979 | yes |
| agent/contracts/ibr.py | 806 | yes |
| agent/contracts/risk_geometry.py | 177 | yes |
| agent/contracts/market_context.py | 70 | yes |
| agent/strategy.py | 339 | yes |
| agent/variants.py | 106 | yes |
| agent/edge.py | 490 | yes |
| agent/brain.py | 170 | yes |
| agent/instruments.py | 129 | yes |
| agent/market.py | 163 | yes |
| agent/registry.py | 220 | yes |
| research/variants.yaml | 11 | yes |
| research/mechanism_cohort.py | 58 | yes |
| research/mechanism_cohort_v2.py | 133 | yes |
| research/mechanism_cost_cohort.py | 40 | yes |
| research/mechanism_deadline_cohort.py | 97 | yes |
| research/mechanism_refresh_cohort.py | 451 | yes |
| research/diagnostic_suite.py | 472 | yes |
| research/diagnostic_shadow.py | 386 | yes |
| research/diagnostic_cohort_contract.py | 189 | yes |
| research/ibr.py | 1476 | yes |

## What this part does (as implemented)

SCOPE. agent/contracts/rule.py is the single evaluator for 12 rule families under 7 schema versions (v1 to v7, rule.py:23-30); agent/contracts/ibr.py is the runtime IBR contract; research/ibr.py is the IBR replay; agent/strategy.py turns a signal into a bracket plan; diagnostic_shadow.py, mechanism_cohort*.py and diagnostic_suite.py freeze the arms that are researched. Hypothesis wording per family lives in research/factory_core.py:103-138 (_thesis, cross-group file).

COMMON MECHANICS (all rule families, as coded)
- Input: completed one-minute bars, timestamp = bar open; runtime rejects the whole evaluation if any row is still forming (rule.py:1686, engine_cycle.py:71-72). Entry is the NEXT bar (research factory_core.py:604-640 at quote/ask or next open; runtime agent/strategy.py:158-160 at snapshot price), but stop and target are fixed from the SIGNAL close (rule.py:2715-2717).
- Stop: distance = max(ATR(atr_period) x stop_atr, close x floor) (rule.py:2679-2681); floor 30 bps (rule.py:76-77) or v7 min_stop_distance_bps (rule.py:203-204). ATR = simple mean of the last 14 one-minute true ranges (rule.py:1921-1931), signal bar included for v1 to v5, excluded for v6/v7 (rule.py:2669). Per coordinator fact 2 the floor almost always binds.
- Target: fixed_r = close +/- distance x target_r (rule.py:2685-2687); v4 session_vwap or rolling_mean frozen from the signal prefix (rule.py:2210-2227, 2688-2696).
- Time: entry window = bar-open minutes since 09:30 in [entry_after, entry_before) for v2+ (rule.py:1947-1952); max hold = entry bar + max_hold_bars (rule.py:1164-1176); v4 thesis deadline = 16:00 minus exit_before_minutes (rule.py:1086-1107); session force-flat clamps (rule.py:1133-1161).
- Filters: side (rule.py:1934-1935); confirmations trend (fast SMA > slow SMA), volume (current >= mult x mean prior lookback), volatility (prior lookback high-low width <= compression_bps) (rule.py:1998-2036); ATR band in bps (rule.py:1976-1983); v5 regime from completed 5/15-minute buckets (market_context.py:66-118, rule.py:2632-2655); v5 reclaim trigger (rule.py:2258-2303).
- Warm-up: _causal_maturity_bars (rule.py:296-339): family prefix (rule.py:270-288), at least atr_period+1 (+2 for v6/v7), confirmation windows. Opening and VWAP families need a gap-free session prefix in research (feature_window_bars returns None, rule.py:376-379; factory_core.py:570-574).
- Research takes only the FIRST admissible signal per symbol-session (factory_core.py:1348); runtime blocks a second emitted signal per symbol-session (engine_cycle.py:743).

PER-FAMILY TABLE (predicate path:line; defaults = diagnostic-shadow baseline from factory_core.py:159-197 promoted to v4 neutral; common to all 24 arms: side both, atr 14, stop_atr 1.0, max_hold 90, window 0-390, fixed_r, stop effectively 30 bps)
| family | hypothesis (factory_core) | entry predicate and direction (rule.py) | warm-up | baseline params | target |
|---|---|---|---|---|---|
| opening_range_breakout | auction imbalance persists after escaping range | after full 09:30 window (2374-2380, fails closed on any missing minute 2253-2254): long close > ORH x(1+t), short close < ORL x(1-t) (2384-2392); v6 opening_hold_bars N closes beyond (2306-2333). Fires on EVERY bar while true | range+1 | range 15, t 5 bps, volume conf | 2R = 60 bps |
| opening_range_fade | opening overshoot reverses | short if bar high > ORH x(1+t) and close < ORH; long mirror (2393-2401). Stop from signal close, not the wick high | range+1 | range 20, t 8, none | 1.5R = 45 bps |
| momentum_continuation | slow execution continues | close/close[-lb-1]-1 > t and close > prev close (2402-2408) | lb+1 | lb 12, t 18, volume conf | 2R |
| mean_reversion | liquidity shock reverts | z of close vs mean/pstdev of last lb closes incl. current; long z <= -zscore (2409-2417) | lb | lb 20, z 1.5, volatility conf width <= 12 bps | 1.5R = 45 bps |
| trend_pullback | flow resumes after pullback | fast>slow, abs(close-fast)/fast <= t, close>open (2418-2434) | slow_lb | 10/35, t 3, trend conf (duplicates predicate) | 2R |
| volatility_breakout | compression releases | prior lb width bps <= compression (v6: <= ATR_prior x mult), close > prior high x(1+t) (2435-2452) | lb+1 | lb 12, comp 8, t 5, volume conf | 2R |
| volume_breakout | volume-backed displacement | vol >= mult x mean prior lb (v6 same-clock RVOL 2135-2207), close > prior high x(1+t) (2453-2481) | lb+1 | lb 15, mult 1.5, t 5, trend conf | 2R |
| vwap_reversion | impact away from VWAP decays | session VWAP = sum(typical x IEX vol)/sum(vol) incl. current (2073-2096); long if close/VWAP-1 <= -t (2486-2494) | lb+1, gap-free session | lb 20, t 25 | 1.5R |
| vwap_trend | rising VWAP = accumulation | close > VWAP and VWAP > VWAP(session[:-lb]) x(1+t) (2508-2518); v6 rolling two windows (2495-2507) | lb+1, gap-free session | lb 15, t 8, volume conf | 2R |
| range_expansion | range expansion persists | bar range >= volume_multiplier x mean prior lb ranges, close > open x(1+t) (2519-2535); v6 body/close-location (2336-2349) | lb+1 | lb 20, mult 2.0, t 5 | 2R |
| opening_drive | one-sided open continues | drive = last OR close/first OR open-1 > t and current close > open (2536-2561) | range+1 | range 30, t 30, volume conf | 2R |
| cross_sectional_residual | relative momentum vs SPY | raw (subject lb return - SPY lb return) > t long, < -t short on synchronized bars (1817-1914); eligible list rule.py:66-70 | lb+1 | lb 15, t 12 | 2R |
| IBR runtime (contracts/ibr.py) | auction imbalance continues (registry.py:86-91) | complete 09:30-09:45 range (373-441); first post-range bar opened <= latest_entry_time (536-540; config 15:00): close > high + close x 5 bps (194-214); relvol = bar vol / opening-range mean vol >= 1.0 (628-633); width/(Wilder ATR x sqrt(range)) in band (611-627; config 0.25-3.0); extension <= 1.0R (647-650); spread <= 25 bps (553-562); bar age <= 30 s (546-547); one per symbol-session (528-534) | 15 bars + ATR 14 | variants.yaml: range 15/30/45, target 2R/1.5R/3R, buffer 5/0/10 | stop = opposite range boundary or 30 bps floor (641-646), 2R (651), force flat 16:00 minus N (705-709) |
| IBR replay (research/ibr.py) | same | defaults range 30, WICK break, fixed 30/60 bps (50-66); via _effective_ibr_config close-confirmed, range stop, mapped filters; first breaking-and-admitted bar (835-836), stop/target anchored to signal close (1027-1039), entry next bar quote/open (916-950), hold contiguous bars only (882-886) | | | |

24 DIAGNOSTIC ARMS (research/diagnostic_shadow.py:213-236, one-factor deltas :34-47; all v4, verified by running _logical_arms): ORB 0eb200d3136d80ee / 4b10682772f81284 (t 5 to 8); ORF d5785d9e70b56def / be61630d346f0923 (t 8 to 12); momentum fe74f73cfc7d082a / 984cbcedd4a2846a (t 18 to 24); mean_reversion b4e231565af56f03 / 75a110f082d304d1 (z 1.5 to 1.75); trend_pullback eaf3e32977d88904 / 2ffd1a88404dd020 (t 3 to 5); volatility_breakout cc1b851a285825c5 / ba43d3a496ff37d8 (compression 8 to 12); volume_breakout 30fece6e3f9b9fd6 / a68b65c3c8e96ff4 (mult 1.5 to 1.75); vwap_reversion ab97bfe87ed566de / cf8b38b12c9bf952 (t 25 to 35); vwap_trend 349de232c3d7a18c / 588bc47ed64d051e (t 8 to 12); range_expansion a9871756f22463a5 / f7fd1b25f2f95ba6 (mult 2.0 to 2.5); opening_drive 393e7229056fc663 / 560520e6c8c85b1e (t 30 to 40); cross_sectional_residual 374375e771a64864 / 2969f7f47855f157 (t 12 to 16). Optional 7 IBR arms from variants.yaml (layout 31, diagnostic_cohort_contract.py:21-24). diagnostic_suite inventory = 24 + 12 (mechanism v1) + 7 IBR = 43 (diagnostic_suite.py:40).

MECHANISM COHORTS (dumped by running each builder)
- v1 intraday-mechanisms.v1 (mechanism_cohort.py:15-45), v5, common side both, ATR14, stop_atr 1, 2R, hold 60, entry_before 300: ORB range15 t5 x {none, +trend 5m x3 eff .4, +15m buckets, +hold 30}; trend_pullback 10/35 t15 x {legacy, reclaim, reclaim+trend, +trailing 1.5R}; vwap_reversion lb20 t20 session_vwap target hold 30 x {none, reclaim, reclaim+range regime, +hold 15}.
- v2 intraday-mechanisms.v2 (mechanism_cohort_v2.py:21-109), v6, one arm per family, every arm trailing_stop_r 1.5 (line 24): ORB 15-180 hold 30 opening_hold 2; ORF 20-180 session_vwap target exit_before 360; momentum 15-180 volume conf; MR 15-180 reclaim(2) rolling_mean target exit_before 360; trend_pullback 15-240 hold 60 reclaim + trend regime; volatility ATR compression x3; volume same_clock RVOL 5 sessions + volume conf; VWAP rev 30-300 hold 15 reclaim + range regime session_vwap target exit_before 360; vwap_trend rolling 5; range_expansion body .5 close-loc .75; opening_drive 30-180 hold 3 bars; cross-sectional 15-180.
- cost-compatible v1 (mechanism_cost_cohort.py:13-27): v2 arms with schema v7 and min_stop_distance_bps 100 (only change; so 100 bps stop, 200 bps fixed_r targets).
- deadline-compatible v1 (mechanism_deadline_cohort.py:22-23, 53-79): cost ORF, MR, VWAP-rev with exit_before 360 changed to 30 (15:30 deadline).
- refresh v1 (mechanism_refresh_cohort.py:66-79): one-coordinate children of cost (9) or deadline (3) parents: ORB hold bars 2 to 3, ORF t 8 to 12, momentum lb 12 to 20, MR z 1.5 to 2.0, trend_pullback efficiency .4 to .6, volatility ATR multiple 3 to 2, volume mult 1.5 to 2.0, VWAP-rev t 25 to 35, vwap_trend window 5 to 10, range_expansion body .5 to .65, opening_drive hold 3 to 4, cross-sectional lb 15 to 30. docs/current-findings.md says the current plan runs these 12 children, 12 parents and 7 IBR arms.

TRADER LENS. Configured static round trip is 17 bps (CostModel entry_cost_bps 8.0 + fee 0.5, x2, run output). With a 30 bps floor the bracket is a 90-minute time exit for HYG and SPY and a genuine bracket only for SLV-like names; v7 100/200 bps brackets are never touched in 30-60 bars (Gaussian estimate in finding A-02). Most plausible after costs: ORF and VWAP reversion with value targets on higher-volatility names, ORB/opening drive on gap days, cross-sectional residual only once beta-adjusted. Implausible as coded: trend_pullback (near-always-true predicate), cumulative vwap_trend, mean_reversion with a 45 bps target inside a 12 bps range. Near-duplicates: volatility_breakout, volume_breakout, momentum_continuation and range_expansion all key on a close beyond the recent range with a volume filter. Opposed pairs (ORB/ORF, momentum/mean_reversion, vwap_trend/vwap_reversion) are separate families; no arm registers long and short of the same signal separately (all side both).

## Findings

### A-01 v2 and cost-cohort reversion arms set exit_before_minutes 360, i.e. a 10:00 New York thesis deadline; VWAP-reversion can never trade and ORF/MR get at most ~9 minutes
- Severity: HIGH  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/mechanism_cohort_v2.py:59, 69, 92; agent/contracts/rule.py:1106-1107; research/factory_core.py:696-702; research/mechanism_cost_cohort.py:17-19`
- Evidence: mechanism_cohort_v2.py:58-59: "after": 20, "before": 180, "hold": 30,
            "target_mode": "session_vwap", "exit_before": 360,
rule.py:1106-1107: close = datetime.combine(local.date(), time(16, 0), tzinfo=zone)
    return close.timestamp() - minutes * 60.0
factory_core.py:696-700: if deadline <= entry_at.timestamp(): ... "entry_after_exit_deadline", stage="entry_policy",
- Why it matters: exit_before_minutes is minutes BEFORE the 16:00 close, so 360 means 10:00. Computed with thesis_exit_deadline: ORF window 09:50-12:30 deadline 10:00; MR 09:45-12:30 deadline 10:00; VWAP reversion 10:00-14:30 deadline 10:00. Research refuses every entry at or after the deadline, so the VWAP-reversion arm (entry_after 30) is refused on every signal and ORF/MR can only enter 09:50/09:52 to 09:58 and are cut at 10:00. The cost-compatible v7 cohort inherits this unchanged (only min_stop_distance_bps changes) and its manifest hash f3f7cb10... is pinned by the refresh cohort. Any v2 or cost-cohort outcome for these three arms (execution_blocked, no edge) is an artefact; 3 of 12 registered arms in each cohort do not test their stated thesis. The deadline cohort silently fixes it (360 to 30) without documenting the defect.
- Suggested fix: Mark the v2 and cost-cohort ORF, MR and VWAP-reversion arms as invalid in docs/trader-research-v6.md and any stored results; add validate_rule_spec checks that the thesis deadline is after entry_before (16:00 - exit_before > 09:30 + entry_before) and an explicit 'minutes before close' docstring; consider renaming the field.
- How verified by reviewer: Ran: python -c using research.mechanism_cohort_v2.mechanism_cohort and agent.contracts.rule.thesis_exit_deadline. Output: 'opening_range_fade entry window 20 180 exit_before 360 deadline 10:00', 'mean_reversion entry window 15 180 exit_before 360 deadline 10:00', 'vwap_reversion entry window 30 300 exit_before 360 deadline 10:00'. Read the refusal path factory_core.py:680-710 and the cost cohort derivation.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-02 ATR stop multiples are inert: a uniform bps floor sets every stop, so brackets mean completely different things across the 24 ETFs and most trades are time exits paying 17 bps
- Severity: HIGH  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `agent/contracts/rule.py:76-77, 1921-1931, 2679-2687; research/mechanism_cost_cohort.py:9`
- Evidence: rule.py:76: MIN_STOP_DISTANCE_BPS = 30.0
rule.py:2681: distance = max(atr * spec["stop_atr"], close * floor_fraction)
mechanism_cost_cohort.py:9: AUTHORED_STOP_FLOOR_BPS = 100.0
- Why it matters: ATR is a 14-bar one-minute ATR (single-digit bps), so with stop_atr 1.0 in every frozen arm the 30 bps (or 100 bps) floor always binds (coordinator fact 2); stop_atr, atr_period and target_r-in-ATR are dead parameters for all registered arms. The same 30/60 bps bracket is a 6.7 sigma target for HYG, 3.8 sigma for SPY and 1.4 sigma for SLV over 90 minutes. Simulation (driftless Gaussian, sigma from fact 2 medians/0.674, 40k paths): HYG 30/60/90 bars P(target)=0.000 P(stop)=0.001 P(time)=0.999; SPY P(target)=0.000 P(stop)=0.048 P(time)=0.952; SLV P(target)=0.128 P(stop)=0.443 P(time)=0.429; v7 100/200 over 30 or 60 bars P(time) >= 0.997 for all three. Configured static round trip is 17 bps (0.57R at 30 bps) against a 90-minute sigma of 9 to 16 bps for HYG/SPY, so expectancy needs a drift above one sigma per trade. Pooled results are dominated by high-volatility names and the 'R' framing in reports is misleading.
- Suggested fix: Express stops in a horizon-scaled volatility unit (e.g. k x sigma_1m x sqrt(hold)) or per-symbol bps, report target/stop touch rates per symbol, and state in every report that the v7 cohorts measure fixed-horizon drift. Remove stop_atr/atr_period from the search space or make the floor per-symbol.
- How verified by reviewer: Read stop/target code end to end; ran research.costs.CostModel on config.yaml (output 'entry_cost_bps 8.0 fee_bps 0.5 round_trip_bps 17.0'); ran verification/geom.py Monte Carlo (outputs quoted). Magnitudes are Gaussian estimates (fat tails would raise touch rates somewhat).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-03 Reversion arms use fixed-R targets that overshoot the value anchor (MR target 45 bps away inside a <=12 bps range)
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `research/factory_core.py:168-169, 186-187, 160-161; agent/contracts/rule.py:2034-2036, 2685-2687`
- Evidence: factory_core.py:168-169: {"family": "mean_reversion", "lookback": 20, "compression_bps": 12.0,
     "zscore": 1.5, "target_r": 1.5, "confirmation": "volatility"},
rule.py:2686: target = (close + distance * spec["target_r"] if direction == "long" else
- Why it matters: The diagnostic mean_reversion baseline only fires when the prior 20-bar width is <= 12 bps, so the mean is at most ~12 bps away, yet the take-profit is 1.5 x 30 bps = 45 bps from entry, i.e. >= 33 bps beyond the mean: the trade cannot realise its thesis and becomes a 90-minute time exit. The ORF baseline (preregistered subject d5785d9e70b56def) and VWAP-reversion baseline (ab97bfe87ed566de) also use fixed 45 bps targets rather than session VWAP. The mechanism cohorts use session_vwap/rolling_mean targets, so the 24-arm cohort and the mechanism cohorts test different exits for the same thesis.
- Suggested fix: Use target_mode session_vwap / rolling_mean for the three reversion families in the diagnostic baseline (new cohort version), or drop the bracket target and evaluate them on a fixed horizon explicitly.
- How verified by reviewer: Read predicate (rule.py:2409-2417), volatility confirmation (2028-2036) and target code; dumped diagnostic arm specs with _logical_arms (target_mode fixed_r, target_r 1.5 for ORF, MR, VWAP rev).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-04 Breakout/continuation predicates fire on every bar after the level is crossed; research takes the first, runtime takes the first bar it happens to see
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: LIKELY
- Location: `agent/contracts/rule.py:2384-2392, 2402-2408, 2554-2561; research/factory_core.py:1348; agent/engine_cycle.py:743, 775-783`
- Evidence: rule.py:2384-2385: if family == "opening_range_breakout":
            if close > high * (1 + threshold):
factory_core.py:1348: return trade_row
engine_cycle.py:743: if signal_sessions.get(signal_state_key) == current_session:
- Why it matters: ORB, opening_drive, momentum, volatility/volume breakout and ORF are state predicates, not crossing events. Research returns the first admissible signal per symbol-session, but some refusals (entry_price_unavailable, entry_bar_not_adjacent, entry_after_exit_deadline) 'continue' to a later still-true bar. Runtime acts only on the newest completed bar within 30 s and burns the session on first emission, so a missed first bar (slow poll, restart, stale data) becomes a later, worse-priced entry that research never modelled. opening_drive with window 0-390 will fire on any up bar all day after a 30 bps drive.
- Suggested fix: Add an explicit 'first cross' condition (previous close not beyond the level) or a max_signal_age_bars for state predicates, and make research and runtime apply the same rule after a refusal.
- How verified by reviewer: Ran verification/orf_check.py with PYTHONPATH set: 'ORB fires on consecutive bars: [('09:45', 'long'), ('09:46', 'long'), ('09:47', 'long'), ('09:48', 'long'), ('09:49', 'long'), ('09:50', 'long')]'. Read factory_core.py:564-640 and engine_cycle.py:743-797; runtime poll cadence not traced (cross-group).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-05 ORF stop is anchored to the signal close, not above the failed-breakout high
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/contracts/rule.py:2394-2401, 2681-2682`
- Evidence: rule.py:2394: if _number(current, "high") > high * (1 + threshold) and close < high:
rule.py:2682: stop = close - distance if direction == "long" else close + distance
- Why it matters: The structural invalidation of a fade is a new high beyond the rejection wick. With the stop at close + 30 bps, any rejection bar whose wick is > 30 bps above its close leaves the stop inside the wick, so a retest of the failed high stops the trade without invalidating the thesis; conversely a small wick gives a wider stop than needed. IBR does anchor to the structure (contracts/ibr.py:641-642), so families are inconsistent. ORF is the preregistered subject and was a paper-trial incumbent.
- Suggested fix: For ORF use stop = max(bar high, OR high x (1+t)) + buffer (mirror for longs), floored by the cost floor; record the structural level in the signal.
- How verified by reviewer: Synthetic session (verification/orf_check.py): 'ORF signal: short entry 99.99 stop 100.29 failed-breakout high 100.4 stop below wick high: True'.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-06 research/ibr.py IBRConfig defaults describe a different strategy from the runtime IBR contract; spread cap also diverges via the mapper
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/ibr.py:50-66, 75-82, 1333; agent/contracts/ibr.py:275-287; research/edge_discovery_core.py:631-632 (cross-group); config.yaml:26-41`
- Evidence: research/ibr.py:50: range_minutes: int = 30
research/ibr.py:61-66: range_stop: bool = False / breakout_buffer_bps: float = 0.0 / close_confirmed: bool = False
agent/contracts/ibr.py:287: max_spread_bps: float = 25.0
edge_discovery_core.py:632: max_spread_bps=float(strategy.get("max_spread_bps", float("inf"))),
- Why it matters: replay_ibr(config=None) and the research.py CLI (research.py:166-178, cross-group) replay a 30-minute WICK breakout with a fixed 30/60 bps bracket and no relative-volume filter, whereas runtime trades a 15-minute close-confirmed +5 bps break with a range stop and relvol >= 1.0. _effective_ibr_config fixes most fields, but config.yaml's strategy block has no max_spread_bps, so replay admits any spread (inf) while the runtime contract and agent/strategy.py:169 refuse > 25 bps. IEX-only quotes on thin ETFs can be wide, so replay can count trades runtime would refuse.
- Suggested fix: Make research IBRConfig defaults equal the runtime contract (or require an explicit config), and have _effective_ibr_config fall back to the contract defaults (IBRConfig.from_mapping) rather than permissive inf/0.0.
- How verified by reviewer: Read both dataclasses, replay_ibr, the mapper and config.yaml (no strategy.max_spread_bps; execution.max_spread_bps 100 at config.yaml:63).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-07 IBR replay truncates the hold at the first missing minute and labels the exit force_flat
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/ibr.py:882-886, 1268-1296`
- Evidence: research/ibr.py:883-886: for bar in post[entry_index + 1:]:
        if bar.timestamp != hold[-1].end:
            break
        hold.append(bar)
research/ibr.py:1296: reason="force_flat", tie=False, gap=False, cfg=cfg,
- Why it matters: IBR has no max hold, so a trade entered at ~10:00 needs about 350 consecutive IEX minutes to reach the real 15:55 flatten. IEX emits no bar for a minute without IEX trades, so on thinner ETFs the hold ends at the first gap, the trade exits at that bar's close and canonical_exit_reason becomes session_force_flat. P&L is truncated and the exit-reason mix is wrong; factory_core labels the same situation data_discontinuity.
- Suggested fix: Label gap exits data_discontinuity (canonical_exit_reason(..., discontinuity=True)), report their frequency, and continue the hold across gaps using the next observed bar's open for stop/target gap fills.
- How verified by reviewer: Traced _replay_session hold construction, the stop/target loop and the post-loop exit path (lines 1163-1319). Gap frequency on the corpus is unverified (no corpus access).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-08 Opening and VWAP families require a gap-free IEX session prefix, so one missing minute disables the rest of the session (selection bias); VWAP is IEX-volume-only
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: LIKELY
- Location: `agent/contracts/rule.py:256-263, 376-379, 2073-2096, 2253-2254; research/factory_core.py:570-574`
- Evidence: factory_core.py:570-574: feature_start = (0 if window is None
                         else max(0, index + 1 - int(window)))
        if not _contiguous(session_bars, feature_start, index + 1):
rule.py:2253-2254: if any(point not in by_minute for point in expected):
        return None
- Why it matters: For ORB, ORF, opening_drive, vwap_reversion and vwap_trend, any missing IEX minute from the session start up to the signal bar removes every later signal that session. Sessions and symbols with complete IEX coverage (busy, news-driven days; liquid names) are over-represented, which biases both the edge estimate and its transfer to quiet days. If the first minutes are missing but the rest is contiguous, session VWAP silently starts late. VWAP uses (H+L+C)/3 weighted by IEX volume (~2-3% of consolidated), a noisy proxy for the VWAP institutions trade against.
- Suggested fix: Report per-symbol coverage and the share of sessions disabled by gaps; for VWAP allow zero-volume gaps (missing IEX minute = no IEX volume) rather than discarding; check that session_bars[0] is 09:30 for session-anchored families.
- How verified by reviewer: Read feature_window_bars, _complete_opening_window, _vwap and the research contiguity loop. Gap rate on the corpus not measured (coordinator fact 6).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-09 cross_sectional_residual is a raw return difference, not a beta-adjusted residual, so it is mostly a market-direction bet
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: LIKELY
- Location: `agent/contracts/rule.py:1896-1898, 1909-1913`
- Evidence: rule.py:1896-1898: symbol_return = subject_end / subject_start - 1.0
    benchmark_return = benchmark_end / benchmark_start - 1.0
    residual = symbol_return - benchmark_return
- Why it matters: With beta != 1 the 'residual' is (beta-1) x SPY move + idiosyncratic. Example: SPY +20 bps over 15 minutes gives SMH (beta ~1.6) about +12 bps residual (long) and XLU (beta ~0.4) about -12 bps (short) with no relative information. The arm then trades market momentum through beta and is correlated with the momentum and ORB arms; the label 'relative strength' overstates what is tested.
- Suggested fix: Estimate a rolling beta from prior sessions (causal) and use r_sym - beta x r_SPY, or restrict to near-unit-beta ETFs; report correlation with SPY's own lookback return.
- How verified by reviewer: Read _cross_sectional_direction end to end; beta magnitudes are typical values, not measured on the corpus.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-10 Relative-volume baselines are not time-of-day adjusted (IBR, volume_breakout current_feed, volume confirmation)
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: LIKELY
- Location: `agent/contracts/ibr.py:437, 628-633; agent/contracts/rule.py:2010-2019, 2461-2464`
- Evidence: contracts/ibr.py:437: "volume_mean": sum(item[4] for item in rows) / len(rows),
contracts/ibr.py:630-631: if relvol is None and mean_volume and mean_volume > 0 and volume is not None:
        relvol = volume / mean_volume
- Why it matters: Intraday volume is U-shaped. IBR's 'relative volume >= 1.0' compares a post-range bar with the mean of the 09:30-09:45 minutes, the heaviest of the morning, so it acts mainly as a time filter (breakouts in the first minutes after the range, or news spikes). ORB's volume confirmation at 09:45 and volume_breakout early in the session compare with opening bars for the same reason. Only the v6 same_clock mode is a genuine RVOL.
- Suggested fix: Use same-clock RVOL from prior sessions (already implemented in rule.py:2135-2207) for IBR and the diagnostic volume filters, or normalise by a time-of-day volume profile.
- How verified by reviewer: Read both baselines; the U-shape is a market regularity, not measured here on IEX data.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-11 Preregistered ORF rationale comes from an audit population that differs from the sealed test (first 60 bars skipped, SIP backfill vs IEX)
- Severity: MEDIUM  |  Category: doc-drift  |  Confidence: VERIFIED
- Location: `docs/preregistered-hypotheses.md:69-72, 77; docs/audit-2026-09-21/forward_costs.py:41; docs/audit-2026-09-21/FORWARD-2026-09-23.md:3-5`
- Evidence: preregistered-hypotheses.md:69-71: It was the only catalogue arm still positive after
realistic costs in the audit's bracket replay (+10.2 bps per trade over 59
trades, session-clustered t +2.57)
forward_costs.py:41: for i in range(60, len(sr) - 1):
- Why it matters: The subject arm d5785d9e70b56def has entry_after_minutes 0 and range 20, so it can fire from 09:50, but the audit that motivated it only evaluated bars from index 60 (about 10:30) on consolidated SIP backfill (FORWARD-2026-09-23.md:3-5), while the sealed test uses IEX from 09:50. The 09:50-10:30 window, where an opening fade is most likely to fire, was never in the motivating sample, so the prior evidence does not describe the registered rule. The same applies to the VWAP-reversion rationale (coordinator fact 4).
- Suggested fix: State the population mismatch in the doc, or register the arm with entry_after_minutes 60 to match the evidence, as a new version with its own sealed window.
- How verified by reviewer: Read the doc, the audit loop (forward_costs.py:41, q1trades.py:31) and the subject spec from _logical_arms (entry_after_minutes 0, range_minutes 20).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### A-12 Docs and cohort labels cite a 25 bps stress scenario; every shipped config uses 9 bps; v2 doc omits trailing 1.5R and the 10:00 deadlines
- Severity: LOW  |  Category: doc-drift  |  Confidence: VERIFIED
- Location: `docs/trader-research-v6.md:9-27; research/mechanism_cost_cohort.py:31; research/mechanism_deadline_cohort.py:83; config.yaml:52; research/mechanism_cohort_v2.py:24`
- Evidence: trader-research-v6.md:26: 30 bps stop floor, preserved 25 bps stressed admission scenario, and 30%
mechanism_cost_cohort.py:31: "risk_policy": "unchanged_25bps_stress_and_0.30_cost_to_risk_veto",
config.yaml:52: "stressed_cost_scenario_bps": 9.0,
- Why it matters: 25/0.30 = 83 bps is the rationale for the 100 bps 'cost-compatible' floor; with the shipped 9/0.30 = 30 bps the original v6 arms already pass admission, so the v7 cohorts' premise and labels are stale (deploy/paper-orb and paper-orf configs also use 9). The v2 table also omits that every arm carries trailing_stop_r 1.5 and that three arms have the 10:00 deadline (A-01).
- Suggested fix: Update the doc and manifest labels to the configured scenario, or record which config the 25 bps value came from (docs/paper-activation-2026-09-15.json:119).
- How verified by reviewer: grep of stressed_cost_scenario_bps across configs and docs; dumped v2 specs.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### A-13 Equity LLM grammar offers v6/v7 schemas that can never execute
- Severity: LOW  |  Category: incorrect  |  Confidence: LIKELY
- Location: `research/llm_strategy.py:1308 (cross-group); agent/contracts/rule.py:1482-1483, 2853-2854`
- Evidence: llm_strategy.py:1308: return rule_spec_json_schema()
rule.py:1482-1483: if normalized["schema"] in {RULE_SCHEMA_V6, RULE_SCHEMA_V7}:
        return False
- Why it matters: Equity proposals can use the diagnostic-only v6/v7 grammar (43,261-byte union schema), producing candidates that research can replay but runtime will never trade; this is the regression behind the one failing test (coordinator fact 1).
- Suggested fix: Restrict the equity provider grammar to executable schemas (v1 to v5) or have the factory reject non-executable schemas before replay.
- How verified by reviewer: Read _grammar_schema and rule_vehicle_executable; measured schema size by running rule_spec_json_schema(); did not trace whether strategy_factory filters them.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### A-14 trend_pullback baseline's 'trend' confirmation repeats the family predicate
- Severity: LOW  |  Category: dead-code  |  Confidence: VERIFIED
- Location: `agent/contracts/rule.py:2421-2433, 2004-2009; research/factory_core.py:177-178`
- Evidence: rule.py:2431: if fast > slow and near_fast and close > opened:
rule.py:2009: return fast > slow if direction == "long" else fast < slow
- Why it matters: Both use SMA(lookback) vs SMA(slow_lookback) on the same closes, so the confirmation in arms eaf3e32977d88904 and 2ffd1a88404dd020 filters nothing.
- Suggested fix: Use confirmation none or a different filter for trend_pullback in a new cohort version.
- How verified by reviewer: Read both functions; same inputs and comparisons.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### A-15 range_expansion reuses volume_multiplier as a range multiplier
- Severity: LOW  |  Category: over-engineered  |  Confidence: VERIFIED
- Location: `agent/contracts/rule.py:2524-2525, 247`
- Evidence: rule.py:2524-2525: if (average_range > 0 and
                current_range >= average_range * spec["volume_multiplier"]):
- Why it matters: One field carries two meanings; with a volume confirmation the same value would gate both volume and range, and mutation/distance code treats it as one axis. Confusing for authors and the LLM.
- Suggested fix: Introduce a dedicated range_multiplier field in a new schema.
- How verified by reviewer: Read predicate and _FAMILY_EXECUTABLE_FIELDS.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### A-16 signal_ts means bar open for rule signals but bar close for IBR signals
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/contracts/rule.py:2713; agent/contracts/ibr.py:656; agent/strategy.py:186`
- Evidence: rule.py:2713: "signal_ts": stamp.timestamp(),
ibr.py:656: "signal_ts": close_ts.timestamp(), "signal_time": close_ts.isoformat(),
strategy.py:186: deadline_ts = hold_deadline(signal_ts + BAR_SECONDS, spec,
- Why it matters: Same field name, one-minute different semantics; any shared consumer (journal, latency metrics, dedupe) that compares them is off by a bar. Currently each path adds its own offset correctly.
- Suggested fix: Emit both signal_bar_open_ts and signal_bar_close_ts and deprecate the ambiguous field.
- How verified by reviewer: Read both builders and the rule plan deadline.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### A-17 Three different ATR definitions (SMA in rule.py, Wilder in contracts/ibr.py and agent/market.py)
- Severity: LOW  |  Category: over-engineered  |  Confidence: VERIFIED
- Location: `agent/contracts/rule.py:1921-1931; agent/contracts/ibr.py:217-247; agent/market.py:28-61`
- Evidence: rule.py:1930: result = mean(values) if values else 0.0
ibr.py:243-244: current_atr = (sum(window) / period if current_atr is None else
                           ((current_atr * (period - 1)) + tr) / period)
- Why it matters: The same word 'ATR 14' means a 14-minute SMA for rule families and a Wilder average for IBR; market.py attaches a third copy that generate_ibr_signal overwrites (ibr.py:763). If runtime passes multi-session bars, Wilder ATR seeds across the overnight gap.
- Suggested fix: One shared ATR function with explicit method and session reset.
- How verified by reviewer: Read all three implementations.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### A-18 Dead code: edge._latest_passing_proof unused; refresh cohort's 360-minute branch and comment contradict the check
- Severity: LOW  |  Category: dead-code  |  Confidence: VERIFIED
- Location: `agent/edge.py:82-101; research/mechanism_refresh_cohort.py:281-287`
- Evidence: edge.py:82: def _latest_passing_proof(ledger: EdgeLedger, record: Mapping) -> dict | None:
refresh:281-283: expected_deadline = 30 if family in _DEADLINE_FAMILIES else 360
    if family not in _DEADLINE_FAMILIES and normalized.get("exit_before_minutes") is not None:
        # A cost parent may only use the frozen 360-minute reversion deadline;
- Why it matters: 20 lines of unused authorization logic invite drift; the comment claims cost parents may use 360 while the code rejects any deadline.
- Suggested fix: Delete both.
- How verified by reviewer: grep found no callers of _latest_passing_proof outside its definition; read the refresh branch.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### A-19 Thesis deadline hard-codes a 16:00 close
- Severity: LOW  |  Category: incomplete  |  Confidence: VERIFIED
- Location: `agent/contracts/rule.py:1103-1107`
- Evidence: rule.py:1106: close = datetime.combine(local.date(), time(16, 0), tzinfo=zone)
- Why it matters: On 13:00 early-close days exit_before_minutes is measured from 16:00, so the thesis deadline can fall after the close; the session force-flat clamps it, so this is a semantic, not a safety, issue.
- Suggested fix: Anchor to the exact calendar close passed by the caller.
- How verified by reviewer: Read thesis_exit_deadline and exit_deadline.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

## Over-engineering

1) Seven schema versions for one evaluator: rule.py has 74 lines naming RULE_SCHEMA_V*, 49 set-literal membership checks such as {RULE_SCHEMA_V2, ..., RULE_SCHEMA_V7}, and parallel default/bounds/JSON blocks per version (rule.py:111-206, 514-710, 760-1083). Two of the seven (v6, v7) are research-only (rule.py:1482-1483, 2853-2854). The provider union schema is 43,261 bytes (v1 alone 3,013, v7 10,393; measured). An integer version with 'version >= N' gates would remove most of this. 2) Options paths in an equity-only deployment: config.yaml has execution_mode shares, asset_classes us_equity and options_feed indicative, while research/ibr.py:366-377 requires OPRA for an executable option, so the option lane cannot produce authorizing evidence. About 130 lines in research/ibr.py (951-1022, 1101-1133, option branches 1182-1184, 1228-1232, 1284-1289), OCC validation in agent/instruments.py:78-112, option vehicles on all 7 variants in research/variants.yaml, VEHICLES in agent/edge.py:22 and option chain helpers in agent/market.py serve no decision here. 3) mechanism_refresh_cohort.py (451 lines for 12 one-field deltas): about 13 alias pairs carrying the same value (declared_mechanism_delta/mechanism_delta, parent_rule_spec/parent_spec, selection_policy/selection, retrospective_only/retrospective_hypotheses, forward_confirmation_required/forward_confirmation, optimization_sweep/no_optimization_sweep, primary_research_priorities/primary_research_priority, ibr_primary_contrast/primary_ibr_contrast, three copies of each source manifest hash, cohort_manifest_content_hash/manifest_hash), plus _EXPECTED_PARENT_VALUES (lines 87-148) duplicating what pinned content hashes already guarantee. 4) diagnostic_cohort_contract.py: 189 lines validating two fixed layouts (24 and 31 arms) that are built deterministically by code in the same repository. 5) Three ATR implementations (A-17); rule_signal_metadata alias (rule.py:2841) and edge._latest_passing_proof (dead, 20 lines). 6) Cohort lineage: v1 (12 arms), v2 (12), cost (12, one field changed), deadline (3, one field changed), refresh (12, one field changed) are five manifests where a single parameterised manifest with explicit diffs would do; the chain also freezes A-01's defect into pinned hashes. 7) agent/brain.py (170 lines) is an LLM decision boundary that config.yaml disables (llm.enabled false) and whose prompt still describes listed-options trading.

## Cross-group questions

- F1 research/factory_core.py:_simulate_trade: refusals entry_price_unavailable, entry_bar_not_adjacent and entry_after_exit_deadline 'continue' to a later still-true bar, while runtime (agent/engine_cycle.py:785-797) burns the symbol-session on the first emitted signal. Is this parity gap measured anywhere?
- B agent/engine_cycle.py:_rule_runtime_bars (max_age 30 s) and contracts/ibr.py stale_minutes 0.5: what is the real poll cadence at the open, and how often is the first breakout bar missed so that a state predicate (A-04) enters a bar or more late?
- B/H agent/engine_cycle.py:71-72 returns None for the whole evaluation if any bar is still forming. Does the Alpaca bars request ever include the in-progress minute? If so, signals are skipped rather than the forming bar dropped.
- H1 recorder corpus: what share of sessions per symbol (VO, VB, XLB, XLRE, EFA, HYG) has at least one missing IEX minute between 09:30 and 12:00? That decides how much A-07 and A-08 bias results.
- B agent/engine_cycle.py:330 generate_ibr_signal: are previous-session bars passed in? contracts/ibr.py:763 computes Wilder ATR over all rows, so an overnight gap would inflate ATR and the width/ATR filter.
- E research/costs.py: the static round trip is 17 bps (entry_cost_bps 8.0 + fee 0.5, x2) while the audit used RT_REAL 1.2 bps (docs/audit-2026-09-21/q1trades.py:15). Which cost basis decides refresh-cohort outcomes, and is it per-symbol?
- D/G research/signal_quality and research/preregistered: for the ORF and VWAP-reversion endpoints, is every firing bar an observation (ORF and VWAP reversion fire on many bars per session) or the first per session? How are sessions with many firings weighted?
- F2 research/llm_strategy.py:1308 offers v6/v7 to equity providers. Does research/strategy_factory.py reject non-executable schemas before spending replay budget? (Related to the failing test in coordinator fact 1.)
- F1 research/ibr.py:1166-1170 skips post-entry bars whose availability is None during the stop/target loop. Under which policy can that happen, and can it hide a stop breach?
- I research.py:166-178 _config builds the research IBRConfig without range_stop or close_confirmed (wick breakouts, fixed 30/60 bps). Were any documented IBR results produced through that CLI?
- B agent/engine_cycle.py _rule_session: are pre-market bars excluded before VWAP/session families run in runtime? rule.py:_session_prefix (2039-2057) filters only by local date, not by 09:30-16:00.
- I/C docs/current-findings.md: do any stored v2 or cost-cohort results for ORF, mean_reversion or VWAP reversion (exit_before 360, A-01) need to be marked invalid?

## Summary

- Report file NOT written: the subagent harness blocked report files, so this structured object is the full handoff (intended path reviews/report-A_signals.md). All 22 assigned files (9,010 lines) were read to the end. Findings by severity: 0 CRITICAL, 2 HIGH, 9 MEDIUM, 8 LOW.
- HIGH A-01: v2 and cost-cohort ORF, mean-reversion and VWAP-reversion arms use exit_before_minutes 360, which is minutes before 16:00, so the thesis deadline is 10:00 New York. VWAP reversion can never enter; ORF and MR get at most about 9 minutes. 3 of 12 arms in each cohort are invalid. The deadline cohort quietly fixes this without documenting it.
- HIGH A-02: ATR multiples are inert because one-minute ATR is single-digit bps, so the 30 bps or 100 bps floor sets every stop. The same bracket is about a 1.4 sigma target for SLV but 6.7 sigma for HYG over 90 minutes. Simulated P(time exit) is 0.95 to 1.00 for SPY and HYG, and at least 0.997 for every v7 100/200 bps arm. The configured static round trip is 17 bps (0.57R).
- MEDIUM A-03: reversion arms in the 24-arm cohort (including preregistered ORF subject d5785d9e70b56def) use fixed 45 bps targets. For mean reversion this sits at least 33 bps past the mean inside a range of 12 bps or less.
- MEDIUM A-04 and A-05: breakout predicates fire on every bar after the level is crossed (ORB fired on 6 consecutive bars in a synthetic test). Research takes the first signal, runtime takes the first one it sees. Separately, the ORF stop is anchored to the signal close and in a test sat inside the rejection wick (100.29 vs 100.40).
- MEDIUM A-06 and A-07: research IBR defaults differ from the runtime contract (30-minute wick breakout, fixed 30/60 bps, no relative-volume filter; the spread cap is unlimited in replay but 25 bps live). The IBR replay also ends a trade at the first missing IEX minute and labels it force_flat.
- MEDIUM A-08 to A-10: opening and VWAP families need a gap-free IEX session, which biases the sample. The cross-sectional 'residual' is not beta-adjusted, so it is mostly a market-direction bet. Relative-volume baselines ignore the intraday U-shape.
- MEDIUM A-11: the preregistered ORF and VWAP-reversion rationale comes from audit replays that skipped the first 60 bars and used SIP backfill, while the sealed tests run on IEX from 09:50. LOW A-12: docs and cohort labels cite a 25 bps stress scenario, but every shipped config uses 9 bps.
- Trader view: plausible edges after costs are limited to ORF and VWAP reversion with value targets on higher-volatility names, plus ORB or opening drive on gap days. trend_pullback, cumulative vwap_trend and mean_reversion as coded are not credible. The four breakout-style families are near-duplicates. No arm registers the long and short of one signal separately; the opposing pairs are ORB/ORF, momentum/mean reversion and vwap_trend/vwap_reversion.
- Over-engineering: 7 schema versions with 49 set-membership branches and a 43 KB provider schema; about 130 lines of options code that cannot produce evidence under this config (indicative feed, OPRA required); about 13 duplicate alias keys in the refresh manifest; 3 ATR implementations; dead edge._latest_passing_proof.
