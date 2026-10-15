# Review handoff: F1b_factory_core

Source: workflow structured result (reviewer could not write files; this file is generated from the journal).

## Files read

| file | lines | read to end |
|---|---|---|
| research/factory_core.py | 2895 | yes |

## What this part does (as implemented)

research/factory_core.py (2,895 lines) holds the strategy-factory primitives: the 12-family hypothesis catalogue and theses (93-257), replay visibility helpers (260-492), the per symbol-session trade simulator _simulate_trade (495-1380), the event-ordered account book simulate_account (1415-1969), the gate-facing diagnose (1972-2100), the deterministic mutation pools (2103-2658) and the discovery/reseeding ladders (2661-2895).

FAMILY TEMPLATES (159-199). Equity roots are promoted to rule-strategy.v4 with neutral v3/v4 extensions (236-243); option roots stay on v2 (220-221). Every root inherits side both, atr_period 14, stop_atr 1.0, target_r 2.0, max_hold_bars 90 and confirmation none (rule.py:82-94), an entry window of 0 to 390 minutes and an ATR band of 0 to 5,000 bps (rule.py:117-126), breakeven None, target_mode fixed_r, trailing None and exit_before None (rule.py:137-151). The 14-bar one-minute ATR is below the 30 bps floor (fact 2), so every root's effective bracket is a 30 bps stop and a 60 bps target (45 bps where target_r is 1.5). The hold is the entry bar plus up to 90 bars, under the shipped policy's 15:00 latest entry and 15:50 force flat.
| slot | family | template (line) | thesis mechanism (line) | effective target |
| 0 | opening_range_breakout | range 15, threshold 5 bps, volume (160-161) | opening inventory imbalance persists after escaping the range (103-105) | 60 bps |
| 1 | opening_range_fade | range 20, threshold 8, target_r 1.5, none (162-163) | opening overshoot reverses (106-108) | 45 bps |
| 2 | momentum_continuation | lookback 12, threshold 18, volume (164-165) | slow institutional execution (109-111) | 60 bps |
| 3 | mean_reversion | lookback 20, compression 12, zscore 1.5, target_r 1.5, volatility (170-171) | liquidity shock mean-reverts (112-114) | 45 bps |
| 4 | trend_pullback | lookback 10, slow 35, threshold 3, trend (178-179) | flow resumes after a pullback to the fast trend (115-117) | 60 bps |
| 5 | volatility_breakout | lookback 12, compression 8, threshold 5, volume (185-186) | compression releases latent orders (118-120) | 60 bps |
| 6 | volume_breakout | lookback 15, volume x1.5, threshold 5, trend (187-188) | abnormal participation is informed (121-123) | 60 bps |
| 7 | vwap_reversion | lookback 20, threshold 25, target_r 1.5, none (189-190) | impact away from VWAP decays (124-126) | 45 bps |
| 8 | vwap_trend | lookback 15, threshold 8, volume (191-192) | advancing VWAP reveals accumulation (127-129) | 60 bps |
| 9 | range_expansion | lookback 20, volume x2.0, threshold 5, none (193-194) | range expansion marks a regime transition (130-132) | 60 bps |
| 10 | opening_drive | range 30, threshold 30, volume (195-196) | one-sided opening auction continues (133-135) | 60 bps |
| 11 | cross_sectional_residual | lookback 15, threshold 12, none (197-198) | single-leg return relative to SPY, explicitly not hedged (136-139) | 60 bps |
The falsification text is generic (147-153). The reversion set is {opening_range_fade, mean_reversion, vwap_reversion} (205-209).

TRADE SIMULATOR (one call per symbol-session).
- Validation. Bars must be 60 s, strictly increasing and unique (420-440). The spec is validated; v6/v7 are replayable but flagged non-executable (509-520, 1330-1345). The policy is re-derived from bar session metadata, so under the shipped config force flat is close minus 10 and latest entry is min(close minus 5, 15:00) (522-530; costs.py:535-543; config.yaml:15-16, 39). Bars outside the exact session are refused (531-538), and short sessions are refused as insufficient_history (546-555).
- Signal scan. Every prefix 1..n-2 with full causal maturity (564-566) and a contiguous feature window (571-575) is evaluated by the shared evaluator (599-601). A window of None means the whole session for anchored families, as at runtime (B-12). The residual family receives SPY context through replay_bar_prefix, cut at the signal bar and the decision time (474-479, 578-597).
- Decision time. signal_ready = max(signal-bar end, availability of every feature-window bar) (482-485), where availability = max(timestamp, as_of, observed_at) (market_data.py:237-242). entry_at is the boundary if the signal was ready by then, otherwise signal_ready (619). The entry bar is the first bar starting at or after entry_at (632-642).
- Entry policy. Refuse after latest_entry_time (648-656), at or after force flat (657-664; strict < at 492), after the bounded deadline, or when the entry bar ends past it (671-711).
- Deadlines. Computed once at entry as min(entry_at + (max_hold_bars + 1) bars, 16:00 minus exit_before, force flat), with force flat winning ties (680-686; rule.py:1133-1161, 1106-1107).
- Entry price. For equity, the executable side (ask for long, bid for short) of the latest recorded quote at or before entry_at whose market timestamp is at most 30 s old (738-752; costs.py:2361-2380). In strict mode with no such quote, the session ends (753-759). The bar open is used only in non-strict mode, and only if the bar's open was visible at its own timestamp (719-722, 760-766). The entry is therefore neither the signal close nor the next-bar open. Options use an OPRA snapshot ask (767-797).
- Geometry. Stop, target and distance come from the signal, anchored at the signal close (805-815; rule.py:2681), and are tick-rounded against the quote (820-840; risk_geometry.py:135-170). When a stress scenario or ratio is configured, a quote-anchored distance below the stress floor is vetoed (841-962). The sizing distance is quote-to-stop (976-977, 1284-1285).
- Hold. Bars after the entry bar are held while their end is at or before the deadline and they start before force flat (989-1030). A missing minute ends the hold at the last contiguous bar (994-1023); data that ends early is flagged observed_data_end (1041-1047).
- Exits. The shared completed_bar_exit_transition runs from the entry bar (1061-1088), in this order: an entry at or through a leg, then open gaps, then intrabar hits with the stop winning ties (rule.py:1409-1439), then breakeven and trailing ratchets from the completed close, applied to the next bar (rule.py:1441-1471). With no level hit, the exit is at the last hold bar with reason thesis deadline or time (1048-1057).
- Exit price. Time, thesis, force-flat and gap exits use the recorded quote at or before the exit bar end (bar open for gaps); in strict mode without one, the whole trade is refused (1101-1121). A non-gap stop or target fills at the active leg level as a RESTING_BRACKET claim (1126-1140).
- First admissible signal. The first prefix that fires and survives the pre-pricing checks is priced, and is the session's only trade (1348). Pre-pricing refusals (entry bar not adjacent, past latest entry, after force flat or deadline, bar not visible, price unavailable) continue to later prefixes (613-664, 696-711, 760-766, 798-804). Pricing, geometry, stress and exit-quote refusals return and end the session (754, 773, 786, 829, 851, 866, 949, 958, 968, 1117, 1152).

ACCOUNT BOOK (1415-1969). Sessions are simulated independently first; candidates are then processed in entry-time order (1619-1620).
- Exits at or before the entry instant are realised (1523-1536). Open positions are marked from quotes or visible bars, and the candidate is refused if any mark is missing (1540-1641).
- Quantity = min(floor(equity x risk_pct / quote-to-stop distance), floor(equity x 25% / entry quote)) (1643-1659), which reproduces B-06.
- Then come the concurrency, gross exposure, open risk and daily loss limits (1731-1753), the stressed-cost plan (1755-1801), and the 50 bps entry slippage cap against the signal close (1819-1837).
- Quote fills move 6 bps adversely and resting-leg fills 8 bps (half the 4 bps spread plus 6 bps slippage) (1844-1853; costs.py:681-693; config.yaml:67-69). Fees are 0.5 bps per side (1856-1860).
- Rows from non-strict bar fallback, historical backfill or a hold discontinuity are flagged non-authorizing (1907-1937).
- An account refusal also ends the symbol-session, because no later signal is considered.

DIAGNOSE (1972-2100). Authorizing trades exclude flagged rows (1977-1981). The failure ladder is execution_blocked, insufficient_signals (fewer than max(3, sessions/3) trades), negative_expectancy, then excess_drawdown (above 5% of starting cash) (2019-2031). The variant is authorizing only if every executed row is (2074-2075).

MUTATION AND DISCOVERY (2103-2895).
- A one-factor coordinate pool over 23 fields, ordered by the diagnosed failure (2159-2191, 2365-2409), with family-specific exit ladders (2202-2217) and a family exit hypothesis moved to the front (2220-2245, 2402-2408).
- A two-factor interaction pool built from measured lessons (2518-2646), with target/hold and ATR/stop selectors (2448-2515, 2707-2791).
- A Cartesian discovery ladder of 5 windows x 5 confirmations x 4 ATR bands x 13 shapes x 4 breakeven fractions, 5,200 equity combinations (2666-2704, 2794-2836).
- Reseeding prefers untried families (2839-2876).

PARITY GAPS (replay versus runtime, fact 10, B-02, B-handoff)
- P1 Decision time. Replay uses the recorder observation time of the feature bars, with no staleness bound (482-485, 619). Runtime acts only on a bar closed at most 30 s ago (B-12). See F1b-06.
- P2 Entry anchor. Both use a live/recorded quote. Replay always fills at it; runtime sends a DAY limit at the IEX quote that may not fill, or may fill late (B-08). See F1b-03.
- P3 Bracket anchor. Both keep the legs at the signal close and tick-round against the quote (805-840), so replay reproduces B-02. The floor veto, however, runs only when stress is configured (841-845); runtime vet_open always applies it (B-05). See F1b-07.
- P4 Consumption. One trade per symbol-session; post-pricing and account refusals end the session, matching consume-before-sizing (B-02). Exceptions are the pre-pricing refusals that continue, and the adjacency refusal that uses the next bar's existence (624-631).
- P5 Level detection. Replay detects levels on recorded IEX bars, with the stop winning ties and touch counting as fill (1071-1088). Runtime legs are broker-resident and trigger on the broker's market, not on IEX bars (LIKELY divergence, unmeasured).
- P6 Level exit cost. Replay charges 8 bps on a limit target that cannot fill worse than its price (1126-1140, 1849-1853), which is conservative.
- P7 Time, thesis and force-flat exits. Replay needs a recorded quote at or before the exit bar end, or the trade is deleted (1101-1121). Runtime sends a market close at the next cycle. See F1b-01.
- P8 Missing minutes. Replay ends the hold and excludes the trade from authorizing evidence (989-1047, 1915-1928); runtime legs stay live. See F1b-02.
- P9 Exit scan start. Replay starts at the first bar starting at or after entry_at (632-642, 1071); runtime brackets are live from the fill. See F1b-06.
- P10 Sizing. Parity: the 25% notional cap binds, reproducing B-06.
- P11 Gross exposure. Replay uses entry notional (1735-1739); runtime uses market value (B-06). See F1b-07.
- P12 Shortability. Replay has no check; runtime checks shortable.
- P13 Fees. Replay charges 0.5 bps on both legs; the actual fee is about 0.21 bps on sells only (fact 9), so replay is conservative by about 0.8 bps per round trip.
- P14 Pending orders. Replay has none, because fills are instant.
- P15 Force flat (15:50), latest entry (15:00), max hold ((held+1) bars, rule.py:1150) and thesis deadline (16:00 minus exit_before, fact 7b) share the same definitions as runtime.

## Findings

### F1b-01 In strict mode a missing exit quote turns an already-entered trade into a refusal, deleting it from P&L on post-entry data
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/factory_core.py:1101-1121 (gap cutoff at 1087)`
- Evidence:             if reason in {"time", "exit_before"} or gapped or exit_gapped:
...
                elif resolved_policy.strict_market_data:
                    return _unpriced(signal_bar, entry_bar, day, direction,
                                     "no fresh equity quote at exit",
- Why it matters: The shipped config is strict (config.yaml:64). Every time, thesis-deadline, force-flat and gap exit needs a recorded quote whose market timestamp is at most 30 s before the exit bar end, or before the bar open for gaps (costs.py:2376-2379). If there is none, _unpriced returns a refused row with signal_opportunity True. The trade, whose outcome is already determined, then vanishes from expectancy, win rate and drawdown. The session's signal is already consumed, so no other trade replaces it, and diagnose counts the row as a signal/execution refusal that can produce execution_blocked (2017-2019). The runtime closes such positions with a market order at the next cycle, with no quote dependency (B handoff step 8), so live keeps exactly these trades. The recorder samples at most every 30 s (recorder.py:2862, 2882) and age is measured from the quote's own market timestamp, so IEX quotes on thinner ETFs that have not updated for 30 s will be dropped. That correlates with quiet markets and with the time-exit branch, which is the dominant exit for low-volatility ETFs (F1b-04). The sample is therefore selected on exit-time data.
- Suggested fix: Price a time or gap exit at the first visible quote after the boundary, within one runtime cycle (the runtime's market close happens after the deadline). If there is still none, fall back to the next visible bar open and flag the row, rather than deleting the trade. Report the count of exit_pricing refusals per variant next to expectancy.
- How verified by reviewer: Read the code path end to end: 1048-1057 set reason time/exit_before, 1102-1121 the strict refusal, 355-387 _unpriced defaulting signal_opportunity to True, 1503-1511 the refusal row, 2017-2019 its diagnose classification; quote age rule at costs.py:2361-2380; recorder interval at deploy/recorder.py:2862, 2882. How often it happens is UNVERIFIED (no corpus, fact 6).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### F1b-02 A missing minute during the hold ends the replayed trade early and removes it from authorizing evidence; one such trade makes the whole variant diagnosis non-authorizing
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/factory_core.py:989-1047, 1089, 1915-1928, 1976-1987, 2074-2075`
- Evidence:             if (session_bars[probe].timestamp -
                    session_bars[probe - 1].timestamp != timedelta(minutes=1)):
...
    has_non_authorizing = len(authorizing_trades) != len(all_trades)
...
        "authorizing": not diagnostic_only and not has_non_authorizing,
- Why it matters: On IEX, a minute without an IEX print has no bar (B-12). The replay then does three things. First, it exits at the last bar before the gap (close or quote), although at runtime the broker legs stay live through the gap. Second, when no level was hit before the gap (1089), it flags the trade diagnostic_only and non-authorizing (1915-1928). Third, diagnose drops it from expectancy, win rate and drawdown (1977-1981, 1987). Exclusion depends on data after entry: trades still open when a later minute is missing are removed, while trades that hit a leg before any gap (mostly the nearer 30 bps stop) are kept. The authorizing sample is therefore conditioned on the post-entry path, and for gap-prone symbols it over-weights early stop-outs. With 90-bar default holds (rule.py:93), at least one missing IEX minute is plausible for the thinner ETFs. In addition, a single discontinuity sets the returned authorizing and directional_authorizing to False for the whole variant (2074-2075), and strategy_factory spreads that into the fit diagnostic (strategy_factory.py:2827).
- Suggested fix: Keep the position open across gaps, as the broker does: evaluate legs on the next observed bar with gap-at-open semantics and keep the trade in the sample, flagged. Report the discontinuity rate separately. Do not let a per-row flag veto the variant-level diagnosis.
- How verified by reviewer: Read 989-1047 (gap break and observed_data_end), 1089-1091, 1217-1248 (flags), 1915-1928 (non-authorizing), 1976-1987 and 2074-2075 (diagnose). Gap frequency on IEX and whether a consumer gates on the variant-level flag are UNVERIFIED (cross-group).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### F1b-03 Replay assumes every entry fills instantly at the recorded quote plus 6 bps, while the runtime submits a DAY limit at the IEX quote that may not fill or may fill hours later
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: LIKELY
- Location: `research/factory_core.py:738-752, 1844-1848`
- Evidence:             quoted = quote_fill_record(
                quotes, symbol=signal_bar.symbol, at=entry_at,
                side="buy" if direction == "long" else "sell",
...
        entry = model.execution_price(
            raw["entry_reference"], execution_direction, entry=True,
- Why it matters: Fact 10 and the B handoff establish that runtime market entries become DAY limit orders at the IEX quote with broker-resident brackets, and B-08 that unfilled entries never expire before force flat. The replay has no fill probability, no time to fill and no late fill: every admitted signal fills at entry_at. For continuation families (8 of the 12 templates), non-fills concentrate where price runs away from the limit (would-be winners), and late fills where it comes back (losers). This adverse selection is absent from the replay, so replay P&L is optimistic for those families. In the other direction, the replay adds 6 bps of slippage on top of a limit price that a filled order cannot exceed (costs.py:681-693; config.yaml:68). It also charges 8 bps on limit target fills (1126-1140, 1849-1853). A buy limit at the IEX ask normally fills at an NBBO ask at or below it, which is conservative. The net sign is family-dependent and unmeasured. This answers the B reviewer's open question (handoff line 362): factory_core does not model non-fills.
- Suggested fix: Model the runtime order: a limit at the entry quote, filled only if a later quote or trade crosses it within a time-to-live, with non-fills recorded as consumed refusals. Drop the extra slippage on limit fills, or calibrate both the non-fill rate and slippage from paper fills.
- How verified by reviewer: Code path VERIFIED by reading (738-759 entry quote, 1819-1853 slippage cap and fill prices, costs.py:681-693 per-side cost); runtime order type from coordinator fact 10 and handoff-B lines 52 and 207-215. Magnitude not measurable here (fact 6).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### F1b-04 Family templates: the 30 bps stop floor overrides family scale, so exit geometry is driven by symbol volatility and low-volatility ETFs resolve almost entirely by 90-minute time exits
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: LIKELY
- Location: `research/factory_core.py:159-199 (with rule.py:91-93 defaults)`
- Evidence:     {"family": "opening_range_breakout", "range_minutes": 15,
     "threshold_bps": 5.0, "confirmation": "volume"},
...
    {"family": "mean_reversion", "lookback": 20, "compression_bps": 12.0,
     "zscore": 1.5, "target_r": 1.5, "confirmation": "volatility"},
- Why it matters: Roots inherit stop_atr 1.0, target_r 2.0 and max_hold_bars 90 (rule.py:91-93). The 14-bar one-minute ATR is below the 30 bps floor (fact 2), so every root trades a 30 bps stop with a 60 bps target, or 45 bps for the three reversion templates. A driftless Gaussian estimate over 91 bars from the fact 2 medians gives the following probability of touching -30 bps: HYG 0.001, SPY 0.056, SLV 0.475; for +60 bps: 0.0000, 0.0001, 0.153. For HYG- and SPY-like names, therefore, at least 94% of trades are time exits, and the bracket is a decoration; for SLV the bracket dominates. The same template is two different strategies across the universe, so family-level conclusions are confounded by symbol volatility. The modelled round trip is about 13 to 15 bps plus the quoted spread (6 bps entry slippage, 6 bps on a quoted exit or 8 bps on a leg, 1 bps fees; config.yaml:67-69). That is about one 90-minute standard deviation for SPY (15.7 bps), so a template needs a per-trade 90-minute information ratio near one. 8 of the 11 explicit thresholds are 3 to 12 bps. The mean-reversion template's 45 bps fixed target is roughly ten times the typical distance of a 1.5 z-score close from its 20-bar mean, so the exit does not test the reversion thesis; the fair-value target exists only as one coordinate (2233-2236). The theses (103-140) are reasonable narratives, but the parameterisation makes them weak baselines.
- Suggested fix: Scale stops and targets to measured per-symbol volatility at the holding horizon (for example 30 to 60-minute realised volatility, not the one-minute ATR), or replace the floor with a cost-based admission rule. Give reversion roots their fair-value target at the root. Report results per volatility bucket, and set max_hold per family.
- How verified by reviewer: Read templates 159-199 and rule.py:82-94, 97-109; costs from config.yaml:67-69 and costs.py:681-693. Ran: cd /tmp && python (locked requirements) -I -c "from statistics import NormalDist; ... for sym,med in (('HYG',0.64),('SPY',1.11),('SLV',2.97)): s1=med/0.6745; atr=math.sqrt(8/math.pi)*s1; s91=s1*math.sqrt(91); p_stop=min(1,2*N.cdf(-30/s91)); p_t60=min(1,2*N.cdf(-60/s91)) ..." Output: 'HYG: sigma1=0.95bps ATR~1.51bps stop_atr_needed>19.8 sigma_91bars=9.1bps P(touch -30)=0.001 P(touch +60)=0.0000'; 'SPY: sigma1=1.65bps ATR~2.63bps stop_atr_needed>11.4 sigma_91bars=15.7bps P(touch -30)=0.056 P(touch +60)=0.0001'; 'SLV: sigma1=4.40bps ATR~7.03bps stop_atr_needed>4.3 sigma_91bars=42.0bps P(touch -30)=0.475 P(touch +60)=0.1532'. The Gaussian approximation ignores fat tails, drift and the U-shaped intraday volatility.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### F1b-05 Many mutation and discovery rungs are behaviourally inert or infeasible on one-minute data, and semantic de-duplication keeps them
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/factory_core.py:2202, 2213, 2317-2318, 2352-2362, 2380-2399, 2670-2671`
- Evidence: _STOP_ATR_LADDER = (0.2, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0)
...
_EXIT_BEFORE_MINUTES_LADDER = (None, 30, 60, 120, 180, 240, 300, 360, 389)
...
    (0.0, 5_000.0), (0.0, 60.0), (25.0, 120.0), (60.0, 5_000.0))
- Why it matters: (a) exit_before_minutes 389 sets the thesis deadline to 16:00 minus 389 minutes, i.e. 09:31 (rule.py:1106-1107). No entry can precede the first bar's end, so every entry is refused as entry_after_exit_deadline (696-703): every v4 root's pool contains a guaranteed zero-trade variant. 360 (10:00) is infeasible for opening_drive, whose range is 30 minutes. (b) max_atr_bps 5,000 receives the generic plus or minus 20% step (2361-2362), giving 4,000 bps, which no one-minute ATR approaches, so that variant is inert. (c) To beat the 30 bps floor, stop_atr must exceed 19.8 (HYG) or 11.4 (SPY) at median conditions, above the grammar maximum of 10 (rule.py:106). All ten alternative rungs are therefore inert for those symbols, and the rungs up to 3.0 are inert for every symbol in fact 2. The comment at 2197-2201 calls this ladder the lever for a 25 bps stress gate, which needs stops of at least 83 bps, so it cannot work for low-volatility names. (d) The discovery bands (25,120) and (60,5000) on a one-minute ATR (rule.py:1982) admit almost no bars, and (0,60) behaves like (0,5000). (e) target_lookback variants on fixed_r roots are not consumed (rule.py:315-319), yet they carry distinct semantic signatures (rule.py:436-440), so the check at 2393-2394 keeps them. These variants consume worker batches and ledger slots and, if counted as tests, the FDR or LORD++ budget, while giving the impression that stop geometry was explored.
- Suggested fix: Prune exit_before rungs that end before the family's causal maturity. Remove max_atr 4,000. Replace the ATR rungs with per-symbol percentiles. Normalise irrelevant fields (target_lookback when the mode is not rolling_mean) in rule_semantic_signature. Before counting a variant as a test, detect behavioural identity on the fit data (identical trade lists).
- How verified by reviewer: (a) and (b) VERIFIED by reading 680-703, 2317-2318, 2352-2362 with rule.py:1106-1107, 97-109 and 127-132. (c) uses the F1b-04 command output (stop_atr_needed>19.8, >11.4, >4.3), and (d) and (e) are LIKELY (Gaussian ATR estimate; rule.py:315-319 comment and 436-440 signature code read, the family predicates were not traced). Overall: VERIFIED for (a) and (b), LIKELY for (c) to (e).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### F1b-06 Decision time is the recorder observation time: the entry bar usually shifts one bar later, so the first partial minute after the fill is never scanned for stop or target, and delayed signals have no staleness bound
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/factory_core.py:482-485, 618-642, 1071`
- Evidence:         entry_at = boundary if signal_ready <= boundary else signal_ready
...
        entry_index = next((probe for probe in range(index + 1, len(session_bars))
                            if session_bars[probe].timestamp >= entry_at), None)
- Why it matters: Availability is max(timestamp, as_of, observed_at) (market_data.py:237-242), and the recorder stamps observed_at with the fetch time (recorder.py:2843). For forward-recorded rows, therefore, signal_ready is later than the boundary and entry_at becomes the observation time. The next bar starts at the boundary, before entry_at, so it is skipped: the exit scan starts one bar later (1071), and a leg touched between the fill and the next minute is ignored (missed stop-outs are optimistic, gap-at-next-open exits pessimistic). Runtime brackets are live from the fill. Nothing bounds signal_ready minus the boundary, whereas the runtime acts only on a bar closed at most 30 s ago (B-12, engine_cycle.py:96-98). A late-observed signal is therefore traded at a late quote and consumes the session. Conversely, when signal_ready is at or before the boundary (legacy rows whose observed_at defaults to the event time), a missing next bar refuses the signal as entry_bar_not_adjacent (624-631), using information the runtime would not yet have. The impact is small with 30 bps stops.
- Suggested fix: Scan from the bar containing entry_at, treating that bar conservatively. Refuse signals whose decision lag exceeds the runtime bar-age limit. Remove the adjacency refusal.
- How verified by reviewer: Read 482-485, 606-642 and 1061-1088; availability semantics at market_data.py:219-244 and recorder observed_at at deploy/recorder.py:2843; runtime bar-age rule from handoff-B B-12. The impact is LIKELY small, not measured.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### F1b-07 Account-level parity gaps: gross exposure uses entry notional, no shortability check, and the quote-anchored floor veto runs only when stress is configured
- Severity: LOW  |  Category: incomplete  |  Confidence: VERIFIED
- Location: `research/factory_core.py:1735-1739, 841-845`
- Evidence:         elif (resolved_policy.max_gross_exposure_pct is not None and
              sum(float(item.get("entry_notional", 0.0)) for item in active) + entry_notional >
...
        if vehicle == "equity" and stress_geometry_requested:
- Why it matters: Runtime vet_open measures gross exposure at market value, and a second full-size position is rejected once the first drifts above its slack (handoff-B B-06). The replay uses static entry notional, so it admits second positions that the runtime rejects. The replay never checks shortable or easy-to-borrow status, while the runtime checks shortable (handoff-B step 5). The B-02/B-05 veto on a quote-anchored stop below the floor applies only when a stress scenario, ratio or calibration is configured (841-845). Direct callers using ReplayPolicy() therefore trade sub-floor brackets that the runtime always vetoes. The shipped config enables stress (config.yaml:52-53), so the production lanes are at parity on the last point.
- Suggested fix: Mark active positions to market for the gross check, add a shortability input, and apply the floor veto regardless of stress configuration, as vet_open does.
- How verified by reviewer: Replay side read at 841-962 and 1731-1753; runtime side taken from handoff-B (cross-group, not re-derived).
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### F1b-08 About 70 lines of dead, no-op or duplicated simulator logic
- Severity: LOW  |  Category: dead-code  |  Confidence: VERIFIED
- Location: `research/factory_core.py:612-617, 963-972, 1383-1386, 1846-1852, 1408-1412, 342-344, 1689, 1890-1906, 2211-2212`
- Evidence:     return max([bars[-1].end, *(stamp for stamp in available if stamp is not None)])
...
        if signal_ready is None:
            last_refusal = _unpriced(
- Why it matters: - _signal_ready_timestamp can never return None, so the signal_not_observable branch is unreachable. It also treats a record of unknown availability as visible at its bar end, contrary to market_data.py:228-229.
- The second quantize_equity_bracket (963-972) re-rounds legs already rounded against the same quote; stop and target are not reassigned between 826 and 965, so it can neither change nor fail.
- Option rows always carry entry and exit source QUOTE (793, 1167), so the _fresh clauses are no-ops.
- _visible_bar_mark and this module's _option_liquid have no callers (research/ibr.py defines its own).
- risk_usd switches between nominal and realised risk (1689), but the two are always equal (1284-1289 with 748-749 and 721-723).
- check_stressed_cost_plan is recomputed on the pass path (1890-1906).
- _TARGET_LOOKBACK_LADDER and _TRAILING_STOP_R_LADDER are unused anywhere in the repository.
- _at_or_before_force_flat implements a strict less-than (492).
The cost is audit noise in the parity-critical path.
- Suggested fix: Delete the dead branches and constants, and rename _at_or_before_force_flat to _before_force_flat.
- How verified by reviewer: Read the full file. Repo-wide grep for _visible_bar_mark, _option_liquid, _TARGET_LOOKBACK_LADDER and _TRAILING_STOP_R_LADDER found no callers outside definitions (research/ibr.py:361 is a separate function).
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### F1b-09 Stale and self-contradictory comments on recorder cadence, quote staleness and template calibration
- Severity: LOW  |  Category: doc-drift  |  Confidence: VERIFIED
- Location: `research/factory_core.py:59-69, 166-184, 2798-2799`
- Evidence: # The recorder samples on a 60s cycle (`deploy/recorder.py --interval`), so a
...
    # 2.4 bps, so it was not selecting a pullback.  3 bps sits between the
    # measured p25 and p50.
- Why it matters: - The recorder --interval defaults to 30 and is capped at 30 s (recorder.py:2862, 2882).
- The described tiers (half-spread for aged quotes, rejection beyond five cycles) do not exist: quote_fill_record rejects anything older than 30 s (costs.py:2376-2379).
- If the median deviation is 2.4 bps, then 3 bps is above p50, not between p25 and p50.
- 55 bps 'above the 98th percentile' cannot have admitted only 96.8% of bars, and 45 bps (166-167) is quoted at the same 96.8%.
- The mean-reversion compression (lookback 20) reuses a width distribution apparently measured for 12-bar windows.
- The discovery_spec docstring gives the dimensions as (5, 5, 4, 7), but _DISCOVERY_SHAPES has 13 entries (2675-2685).
- No dataset, date or script is cited for any percentile.
Calibration claims that steer template choice cannot be audited.
- Suggested fix: Correct the comments and cite the measurement artefact (corpus window and script) for each calibrated threshold.
- How verified by reviewer: Read the comments and compared them with deploy/recorder.py:2862, 2882, costs.py:2376-2379 and the tuple at 2675-2685.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### F1b-10 Option lane: strict mode prices a stop or target exit from the snapshot at or before the start of the trigger bar, i.e. before the level was hit
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/factory_core.py:1087, 1144-1150, 1169-1171`
- Evidence:             pricing_cutoff = bar.timestamp
...
                                   cutoff=(pricing_cutoff if
                                           resolved_policy.strict_market_data else
                                           exit_bar.end),
- Why it matters: For a long call stopped out intrabar, the replay uses the option bid from before the adverse underlying move, which understates losses; target exits are understated the other way. This contradicts the comment at 1169-1171. The lane is off by default: the recorder writes OPRA only for an explicitly enabled option lane (recorder.py:2867, 2930), the shipped config names the indicative feed (config.yaml:8), and _option_at accepts only OPRA (309-310). _option_at also re-sorts every snapshot on every call (306) and performs a linear membership test (333-334), which is quadratic in the snapshot count.
- Suggested fix: Price option level exits from the first snapshot after the trigger bar ends, and index snapshots once per run.
- How verified by reviewer: Read 1061-1088 and 1141-1172 and _option_at 292-339. Whether any lane currently runs options is UNVERIFIED.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

## Over-engineering

1) _simulate_trade is a single 886-line function (495-1380). It mixes validation, the signal scan, entry policy, quote pricing, bracket geometry, the stress veto, the hold scan, exit pricing and about 175 lines of row telemetry (1173-1347); the pre_admission_plan for a refused row alone is 45 lines (891-935). Splitting it into scan, admit, hold and price stages would make the replay-runtime contract auditable.
2) About 70 lines are dead or no-op (F1b-08): the unreachable branch at 612-617, the second quantize at 963-972, the _fresh clauses, _visible_bar_mark, _option_liquid, the risk_usd switch whose operands are always equal (1689), the recomputed stress plan (1890-1906), two unused ladders (2211-2212), and MAX_DISCOVERY_ATTEMPTS duplicating discovery_attempt_limit (2689-2704).
3) Several row fields are duplicated:
- hold_discontinuity and hold_discontinuity_exit (1217-1218);
- identical six-line expressions for hold_exit_reason and exit_reason_detail (1235-1248);
- plan_entry and authored_entry_reference (1281-1282);
- active_stop_price, written up to three times (1197-1198, 1317, 1324);
- vehicle/stressed_cost_vehicle, entry_notional/stressed_cost_entry_notional and risk_usd/stressed_cost_risk_usd (1773-1785).
4) The option lane (about 150 lines: 292-344, 767-797, 1141-1172, 1561-1570, 1660-1668) is not reachable with the shipped indicative option feed unless an OPRA lane is explicitly enabled.
5) _stressed_cost_diagnostic (2248-2286) recursively substring-matches every key and string in an arbitrary diagnostic payload. Any string containing 'cost_stress', 'stressed_cost' or 'execution_blocked', or any key containing 'stressed_cost_risk' with a positive value, opens the 17-rung min_atr ladder. An explicit boolean from diagnose would be simpler and safer.
6) About 320 lines of interaction, geometry and blocked-pair selectors (2412-2646, 2707-2791) exist to choose at most a dozen two-factor variants. Many of their inputs are the inert axes of F1b-05 (stop_atr and min_atr under the 30 bps floor).

## Cross-group questions

- F1a (research/strategy_factory.py): does any promotion or selection path read 'authorizing' or 'directional_authorizing' from the diagnose() output (factory_core.py:2074-2075) that is spread into the fit 'diagnostic' at strategy_factory.py:2827? If so, a single hold-discontinuity trade vetoes the whole variant (F1b-02).
- H1b (dataset loader): the recorder re-fetches the last 3 minutes of bars every cycle with observed_at=current (deploy/recorder.py:2843). Which observation's observed_at survives de-duplication? If the latest survives, signal_ready (factory_core.py:482-485) and every replay entry shift by up to about 3 minutes.
- H1a (deploy/recorder.py): per symbol, what fraction of minute boundaries have a recorded quote whose market timestamp is at most 30 s old? That is the survival rate of time and gap exits under factory_core.py:1101-1121 and of entries under 753-759.
- D (stats/gates) and F1a: are behaviourally identical variants counted as separate hypotheses in FDR/LORD++ budgets or the variant ledger? Examples: stop_atr rungs under the 30 bps floor, max_atr_bps 4000, target_lookback on fixed_r roots, and exit_before 389, which always yields zero trades.
- A_signals (agent/contracts/rule.py evaluate_rule_signal): for session-anchored families (feature_window_bars None), is session_bars[0] required to be the 09:30 bar? factory_core.py:571-575 only checks contiguity from the first recorded bar.
- Runtime (agent/execution_lifecycle.py / engine_cycle.py): which entry_ts is passed to exit_deadline (signal-bar end, submission time or fill time)? Replay passes entry_at, the decision or observation time (factory_core.py:680-681), and rule.py:1150 adds (max_hold_bars + 1) bars, so a different anchor changes the hold by up to one bar.
- G1 (live shadow): does shadow replay reuse _simulate_trade, and therefore also drop trades with missing exit quotes (F1b-01) and gap-interrupted holds (F1b-02)? If shadow evidence gates paper promotion, both biases carry through.
- E1 (research/costs.py): is charging execution_price(executable_quote=False), i.e. half-spread plus slippage (8 bps), on resting limit target fills intended (factory_core.py:1126-1140, 1849-1853), given that a limit leg cannot fill worse than its price?
- F1a: which keys does measure_fit_diagnostics put under fit_diagnostics? Can ordinary passing telemetry containing 'stressed_cost' strings open the min_atr ladder through _stressed_cost_diagnostic (factory_core.py:2248-2286)?

## Summary

- The replay does not enter at the signal close or the next-bar open. It fills at the executable side of the latest recorded quote (at most 30 s old) at the recorder observation time. The legs stay at the signal close and are tick-rounded against that quote, so it reproduces B-02 and B-06 faithfully. There is one trade per symbol-session, and pricing-stage and account refusals end the session, matching consume-before-sizing.
- F1b-01 MEDIUM: in strict mode, a missing exit quote deletes an already-entered trade (factory_core.py:1101-1121). The runtime exits by market order regardless, so the replay sample is selected on exit-time data.
- F1b-02 MEDIUM: a missing IEX minute during the hold ends the replayed trade at the gap and excludes it from authorizing evidence. A single such trade makes the whole variant non-authorizing (2074-2075).
- F1b-03 MEDIUM: the replay assumes guaranteed instant fills at quote plus 6 bps, while the runtime sends a DAY limit at the IEX quote that may not fill or may fill late (B-08). Non-fills and adverse selection are not modelled.
- F1b-04 MEDIUM: the 30 bps floor overrides the family templates. Over 91 bars, the estimated probability of touching -30 bps is 0.1% (HYG), 5.6% (SPY) and 47.5% (SLV), so family results are confounded by symbol volatility. The 13 to 15 bps round trip is about one 90-minute sigma for SPY.
- F1b-05 MEDIUM: the search grammar contains dead rungs. exit_before 389 (a 09:31 deadline) always yields zero trades, max_atr 4000 is inert, the stop_atr ladder cannot clear the floor for HYG or SPY, and target_lookback mutations on fixed_r roots survive de-duplication.
- The exit mechanics are otherwise sound: a shared completed-bar transition with gaps first, stop-wins ties and next-bar breakeven and trailing; deadlines computed once at entry ((held+1) bars, 16:00 minus exit_before, force flat 15:50); latest entry 15:00 from session metadata.
- LOW findings: the entry bar shifts one bar later when bars are observed after their end, and delayed signals have no staleness bound (F1b-06); account parity gaps on gross exposure, shortability and the floor veto (F1b-07); about 70 lines of dead or duplicated code (F1b-08); stale calibration and cadence comments (F1b-09); the option lane prices strict-mode level exits before the trigger and is off by default (F1b-10).
- Severity counts: 0 CRITICAL, 0 HIGH, 5 MEDIUM, 5 LOW. Report path: none (structured result only, file writes blocked).
