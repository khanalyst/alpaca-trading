# Review handoff: E1_costs

Source: workflow structured result (reviewer could not write files; this file is generated from the journal).

## Files read

| file | lines | read to end |
|---|---|---|
| research/costs.py | 2407 | yes |
| research/quote_costs.py | 1237 | yes |
| research/stressed_cost_calibration.py | 732 | yes |

## What this part does (as implemented)

research/costs.py owns the one static CostModel used by every replay lane. Defaults are spread 4.0, slippage 6.0 and fee 0.5 bps, with runtime caps of 100 bps on spread and 50 bps on slippage (costs.py:34-49). config.yaml:67-69 ships the same values with provenance "shipped_conservative_v1_plus_25bps_stress" (config.yaml:71).

Cost formula (costs.py:677-742), in basis points of notional, not R:
- Per side on a bar or resting-bracket reference: entry_cost_bps = spread/2 + slippage = 2 + 6 = 8 bps.
- Per side when the fill price is already an executable IEX quote: slippage only, 6 bps (per_side_bps, costs.py:681-683).
- execution_price moves the reference adversely by that rate. fees() charges 0.5 bps on the notional of BOTH legs (costs.py:695-703).
- Round trip = (8 + 8) + (0.5 + 0.5) = 17 bps on bar references; (6 + 6) + 1 = 13 bps on top of the IEX spread actually crossed; 6 + 8 + 1 = 15 bps for a quote entry with a resting-bracket exit. Verified by running CostModel().round_trip_cost(1,1,1,1,...): outputs 17.0, 13.0 and 15.0.
- The factory account lane applies this per leg (factory_core.py:1844-1860): equity legs are "executable" only when fill_source == quote, so resting stop and target exits pay the full 8 bps.
- risk_unit_report compares dollar round-trip cost with dollar risk: risk >= cost x min_cost_coverage, where gates use 1.0 (costs.py:1916-1918; gates.py:3631).

Stress (two different uses of the same ladder):
- Ladder COST_STRESS_SCENARIOS_BPS = (9, 15, 25, 50), described as "all-in" (costs.py:50-53). It is duplicated in gates.py:153.
- Runtime veto: stressed_cost_usd = scenario x entry notional, charged once (costs.py:1660). check_stressed_cost_plan rejects when that divided by planned risk_usd exceeds max_stressed_cost_to_risk_ratio (costs.py:1732-1742). Config uses 9 bps and 0.30 (config.yaml:52-53), so stop_bps >= 9/0.30 = 30. Commit 2249cc5 lowered the scenario from 25 to 9 explicitly because "9/0.30 is exactly MIN_STOP_DISTANCE_BPS" (see B-05).
- Research gate: requires positive net P&L at 25 bps (gates.py:154), applied as net - max(0, scenario x notional - recorded round trip) (gates.py:1541-1551).
- The 100 bps floor of the v7 cost-compatible cohort (mechanism_cost_cohort.py:9) carries risk_policy "unchanged_25bps_stress_and_0.30_cost_to_risk_veto" (line 31). 25/0.30 = 83.3 bps, so 100 bps is the round number above it (LIKELY derivation).
- Both stop floors are therefore functions of the stress ladder and the 0.30 ratio, not of measured volatility or cost.

Reality on Alpaca for these 24 ETFs (coordinator facts 2 and 9): zero commission, SEC fee about 0.21 bps on the sell leg only, one-cent spreads of 0.13 bps (SPY) to about 2.3 bps (XLU/XLRE). A realistic round trip is therefore about 0.35 bps (SPY) to about 2.5 bps (XLRE) before stop slippage, so the model is about 7x to 50x reality. The repository's own commit 2249cc5 says "six beat a realistic 1.2 bps; nothing survives multiplicity correction".
- At the 30 bps floor with target_r 2 (60 bps target), 17 bps is 0.57R. The break-even win rate (30+c)/90 rises from 33.3% to 52.2%, and to 61.1% under the 25 bps gate.
- 17 bps equals about 6 (SLV) to 27 (HYG) median one-minute absolute moves.

Quote fills (costs.py:2226-2380): the latest visible quote at the fill instant, ask for buys and bid for sells, at most 30 s old, with visibility = max(timestamp, as_of, observed_at), so there is no look-ahead. A SQLite index serves large corpora (costs.py:1979-2310). The feed is IEX only (config.yaml:7).
- IEX's touch is normally at or outside the NBBO, and Alpaca routes with NBBO protection, so IEX-touch prices plus 6 bps are doubly pessimistic on price.
- The replay assumes fill certainty, whereas the runtime posts a DAY limit at the IEX touch that cannot slip but can fail to fill (coordinator fact 10, B-08).

research/quote_costs.py, the measured mode:
- measure_quote_costs (702-899) streams quotes. It rejects crossed, invalid or provenance-less rows. It accumulates spread (bps of mid, 0.05 bps bins) and log10(min(bid_size, ask_size)) depth histograms for the universe, each symbol, and each (symbol, half-hour bucket) cell. Quantiles are upper bin edges (584-599). Only bucket cells with at least min_quotes_per_cell (500) quotes are emitted (858-862). Pre-open and post-close quotes still enter the symbol and universe aggregates (806-810).
- cost_model_from_schedule (1013-1115) applies a strict policy: no fallback from an under-covered bucket (957-1002). Spread is the chosen percentile (p75). slippage_bps is replaced by depth impact = half_spread x min(order/depth_p25 - 1, max_impact_half_spreads 4.0), which is 0 when the order fits displayed size.
- validate_measured_quote_config and cost_resolver_setup (152-327, 490-542) wire it from config. reprice_ibr_result (339-487) reprices IBR legs.
- Why it is disabled: config.yaml:74 has enabled false. README.md:342-344 says no universal corpus-specific schedule exists. Enabling needs an embedded or hashed schedule (quote_costs.py:261-275), and this container has no corpus (coordinator fact 6).

research/stressed_cost_calibration.py:
- calibrate_stressed_cost (484-713) computes a ONE-WAY cost per fit cell as p95 spread/2 + impact + 0.5 bps fee (432-450), mapped to the smallest ladder rung at or above it (453-459). It requires at least min_quotes and at least 5 sessions (573-574). Validation can only widen. A validation cost above 1.25 x the fit cost makes the cell a fallback (625-634). The output is a content-hashed, diagnostic-only artifact.
- verify_stress_calibration_artifact (94-197) requires EVERY cell to be selected and usable, with disjoint, chronologically later validation sessions.
- resolve_stress_scenario (213-262) uses a cell's rung only after effective_after_session, and otherwise falls back to the configured rung.
- Disabled in config.yaml:54. ReplayPolicy plumbing is at costs.py:232-249, 369-378 and 460-501, and check_stressed_cost_plan activation at 1692-1703.

## Findings

### E1-01 Static cost model charges 17 bps round trip (13 bps on top of quotes), roughly 7x to 50x the real Alpaca cost, and dominates every research verdict and the stop geometry
- Severity: HIGH  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `research/costs.py:34-42, 677-683, 695-700, 740-742; config.yaml:67-69; research/quote_costs.py:5-8`
- Evidence: costs.py:34 `DEFAULT_SPREAD_BPS = 4.0` | costs.py:40 `DEFAULT_SLIPPAGE_BPS = 6.0` | costs.py:679 `return self.spread_bps / 2.0 + self.slippage_bps` | costs.py:683 `return self.slippage_bps if executable_quote else self.entry_cost_bps` | quote_costs.py:6-7 "universe they are the dominant term in every replayed result: 17 bps round / trip on bar references and 13 bps when executable quote prices already carry"
- Why it matters: Arithmetic: per side 4/2 + 6 = 8 bps (or 6 bps on an IEX quote price) plus 0.5 bps fee per leg gives 17, 13, or 15 bps (quote entry plus resting exit) of notional per round trip.

Reality on Alpaca for this universe (coordinator facts 2 and 9): zero commission, SEC fee about 0.21 bps on sells only, one-cent spreads of 0.13 to about 2.3 bps. That makes a real round trip about 0.35 bps (SPY) to 2.5 bps (XLRE). The repository's own commit 2249cc5 records "six beat a realistic 1.2 bps".

Impact on the strategies:
- At the 30 bps stop floor with a 2R target, 17 bps is 0.57R. The break-even win rate rises from 33.3% to 52.2%, and to 61.1% under the gate's 25 bps.
- It equals about 6 (SLV) to 27 (HYG) median one-minute moves, while 65% of trades exit on time (commit 2249cc5).
- Negative research verdicts therefore say more about the cost constants than about tradeable edge. The commit notes nothing survived multiplicity even at 1.2 bps, so cost is decisive at the margin but not the only cause.
- The same stress and ratio arithmetic sets the 30 bps (9/0.30) and 100 bps (25/0.30 = 83.3) stop floors, which are 10x to 100x one-minute volatility (B-05).
- IEX-only quote fills: pricing at the IEX touch is already pessimistic versus NBBO-protected routing, and the extra 6 bps doubles that pessimism, while fill certainty is optimistic (see E1-03).
- Suggested fix: Replace the constants with a per-symbol model:
- half of max(one tick, measured median spread) per side;
- SEC fee about 0.21 bps (plus FINRA TAF per share) on sell legs only;
- zero price slippage on limit legs, with non-fill modelled instead;
- a tick-based plus gap-through term on stop-market legs.
Keep stress as explicit multiples of that baseline, and set stop floors from volatility (ATR), not from stress/ratio.
- How verified by reviewer: Read costs.py:633-742 end to end.

Ran python (locked requirements) with CostModel().round_trip_cost(1,1,1,1[, executable_quotes=True | entry_executable_quote=True, exit_executable_quote=False]) x 1e4. Output: "RT bps bar refs : 17.0", "RT bps both quotes : 13.0", "RT bps quote entry + resting exit: 15.0".

The factory lane applies it per leg at research/factory_core.py:1844-1860 (cross-group). Real-cost figures are coordinator facts 2 and 9. Commit text from git show 2249cc5.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### E1-02 Runtime 'stressed' cost (9 bps all-in) is below the model's own expected round trip (13 to 17 bps): the 0.30 cost/risk veto does not hold under the expected model and the 9 and 15 bps gate rungs are inert for equity
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/costs.py:50-53, 1660, 1736-1742; config.yaml:52-53; research/gates.py:1551 (cross-group)`
- Evidence: costs.py:50 "# These are the preregistered all-in cost shocks used by the research gate." | costs.py:53 `COST_STRESS_SCENARIOS_BPS = (9.0, 15.0, 25.0, 50.0)` | costs.py:1660 `stressed = abs(notional) * scenario / 10_000.0` | config.yaml:52 `"stressed_cost_scenario_bps": 9.0,` | gates.py:1551 `stressed_values.append(net - max(0.0, stressed_cost - base_cost))`
- Why it matters: The veto is meant to keep the stressed cost at or below 30% of risk. Under the research model's own expected cost, a trade at the 30 bps floor has cost/risk of 17/30 = 0.57 (bars) or 13/30 = 0.43 (quotes), yet runtime telemetry records stressed_cost_to_risk_ratio = 0.30. The 'stress' is cheaper than the base case.

In the gate, max(0, scenario - base) makes the 9 bps rung a no-op for every equity row (base 13 to 17 bps). The 15 bps rung adds 0 for bar and quote-plus-resting rows and only 2 bps for all-quote rows. So two of four preregistered rungs are inert for equity.

Commit 2249cc5 chose 9 because "9/0.30 is exactly MIN_STOP_DISTANCE_BPS". The scenario was reverse-engineered from the floor (relates to B-05). No money is lost at real Alpaca costs; the defect is an incoherent risk contract and misleading telemetry.
- Suggested fix: Define stress relative to the expected model (expected x k or expected + x bps). State once whether a rung is per side or round trip. Either set the runtime rung at or above the expected round trip, or drop the veto and derive stop floors from volatility.
- How verified by reviewer: Read costs.py:1625-1756 and gates.py:1452-1580 (cross-group). Base costs of 13, 15 and 17 bps come from the run output in E1-01. Config values were read at config.yaml:52-53. The 25 -> 9 history and its rationale come from git show 2249cc5.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### E1-03 The same per-leg slippage is charged on limit legs that cannot slip (take-profit; runtime entry limit at the IEX quote), while their real risk (non-fill) is unmodelled
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `research/costs.py:681-693, 1456-1458; research/factory_core.py:1849-1853 (cross-group)`
- Evidence: costs.py:691-693 `sign = 1.0 if ((direction == "long") == entry) else -1.0` / `rate = self.per_side_bps(executable_quote=executable_quote) / 10_000.0` / `return float(reference) * (1.0 + sign * rate)` | costs.py:1457-1458 "callers must separately apply the cost model with ``executable_quotes`` / false for this source."
- Why it matters: A broker-resident take-profit is a limit order and cannot fill below its limit. The replay books a long target at 100.60 as 100.51952 (8 bps), which is 13% of a 60 bps target.

Quote-priced entries pay the IEX touch plus 6 bps. The runtime entry, however, is a DAY limit at the IEX quote (coordinator fact 10): it cannot slip, it can only fail to fill (B-08).

The model therefore charges price slippage where none is possible and ignores what is possible: entry non-fill, adverse selection on fills, and touch-without-fill at the target. Stop-market legs, the only legs with real adverse slippage, get the same 8 bps as the target. The cost structure is mis-allocated across legs, which biases any comparison of target-heavy and stop-heavy exit geometries.
- Suggested fix: Price legs by order type:
- limit legs (entry limit, take-profit) at the limit price, filled only on trade-through or by a queue model, with zero slippage and explicit non-fill;
- stop legs with tick-based slippage plus gap-through to the next print.
- How verified by reviewer: Ran CostModel().execution_price(100.60, 'long', entry=False). Output: "long target exit priced at 100.60 -> 100.51952".

Read factory_core.py:1849-1853: resting exits are executable only if fill_source == quote. Runtime order types come from coordinator fact 10 and were not re-traced.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### E1-04 Stressed-cost calibration cannot narrow below the configured 9 bps; for these ETFs it reproduces 9 bps everywhere or fails verification on a one-tick change in one symbol
- Severity: MEDIUM  |  Category: over-engineered  |  Confidence: VERIFIED
- Location: `research/stressed_cost_calibration.py:453-459, 625-634, 139-140; research/costs.py:53; config.yaml:52, 54`
- Evidence: stressed_cost_calibration.py:456-458 `for scenario in COST_STRESS_SCENARIOS_BPS:` / `if value <= scenario:` / `return float(scenario), None` | :626-628 `materially_wider = bool(` / `validation_cost is not None and fit_cost is not None and` / `validation_cost > fit_cost * float(materiality))` | :139-140 `if cell.get("status") != "selected" or cell.get("usable") is not True:` / `return False, "cell_not_usable"`
- Why it matters: The ladder minimum (9) equals the configured scenario (config.yaml:52), so an activated artifact can only keep 9 or widen. It can never relax the binding 30 bps floor (B-05).

With one-cent spreads, every RTH cell's one-way p95 cost (about 0.6 to 3 bps) maps to 9, identical to the config.

Materiality is tested on the continuous cost rather than the rung, and verification is all-or-nothing. A one-tick change in XLRE's p95 (one-way 1.7 -> 2.9 bps, both deep inside the 9 bps rung) therefore marks all its cells unusable, reports a 25 bps rung with an 83.3 bps feasible stop, and invalidates the whole artifact.

About 732 lines plus about 75 lines of ReplayPolicy plumbing change no decision at the shipped configuration.
- Suggested fix: Remove the module, or park it until the ladder has rungs below the runtime scenario. If kept, apply materiality to rungs, not continuous costs, and verify per cell instead of all-or-nothing.
- How verified by reviewer: Ran measure_quote_costs on synthetic one-cent-spread IEX quotes (30 s snapshots, 10 fit plus 10 validation sessions), then calibrate_stressed_cost and verify_stress_calibration_artifact.

Output:
- "SPY only: cells=13 ... ('SPY', 'selected', 9.0, None) ... fit_cost_bps= 0.575 val_cost_bps= 0.65 ... aggregate rung: 9.0 feasible min stop: 30.0 ... verify: (True, None)"
- "SPY+XLRE ... ('XLRE', 'fallback', 25.0, 'validation_materially_exceeds_fit') n_buckets= 13 fit_cost_bps= 1.7 val_cost_bps= 2.9 ... aggregate rung: 25.0 feasible min stop: 83.33 ... verify: (False, 'cell_not_usable')"
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### E1-05 Measured mode (the documented replacement) sets slippage to depth impact only, which is 0 for small orders, so stop exits would be priced at half the IEX spread with no trigger slippage; spread and depth are IEX-only
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `research/quote_costs.py:1074-1110; research/costs.py:35-37`
- Evidence: quote_costs.py:1077 `impact_bps = 0.0` | quote_costs.py:1107-1110 `multiple = max(0.0, shares / depth_shares - 1.0)` / `impact_bps = half_spread * min(multiple, impact_cap)` / `return CostModel(` / `spread_bps=spread_bps, slippage_bps=impact_bps, fee_bps=fee_bps,` | costs.py:36-37 "# book at whatever is resting when they arrive; a triggered stop in a moving / # market pays materially more than half the quoted spread."
- Why it matters: README.md:332-334 presents measured mode as the honest replacement for the constants. Once enabled, a resting stop exit (executable_quote False) pays only half the p75 IEX spread, about 0.07 bps on SPY. There is no trigger or gap slippage, contradicting the module's own rationale. The bias flips from about 50x pessimistic to optimistic on the leg where losses concentrate.

The schedule must be fitted on the configured feed, iex (quote_costs.py:242-256, 290-292). IEX's touch is often wider than the NBBO, which overstates spread. IEX displayed size is a small fraction of consolidated depth, so the impact term, capped at 4 half spreads, mostly measures IEX thinness rather than market impact. It is not a drop-in fix for E1-01.
- Suggested fix: Keep an explicit stop-trigger slippage term in measured mode (ticks, or a fraction of the trigger bar's range plus gap-through). Treat IEX spreads as an upper bound or fit on SIP. Base impact on consolidated depth or ADV.
- How verified by reviewer: Read quote_costs.py:1013-1115 end to end. factory_core.py:1849-1853 (cross-group) shows resting exits use executable_quote False, so the per-side cost is spread/2 + slippage (= impact).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### E1-06 Calibration maps a one-way cost onto the all-in ladder and fails open on missing depth
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/stressed_cost_calibration.py:432-450, 500; research/costs.py:50`
- Evidence: stressed_cost_calibration.py:500 `"""Calibrate one-way entry stress from fit and optional validation schedules.` | :444-447 `if shares is not None and shares > 0 and depth is not None:` / `impact = (spread / 2.0) * min(` / `max(0.0, shares / depth - 1.0), max_impact_half_spreads)` / `total = spread / 2.0 + impact + fee_bps` | costs.py:50 "# These are the preregistered all-in cost shocks used by the research gate."
- Why it matters: The ladder is all-in: the gate replaces the whole round trip (gates.py:1551), and README.md:350-352 says "it is not a per-side bps charge". The rung is chosen from a one-way cost, so a cell with a 16 bps p95 spread (a round trip of at least 17 bps) gets the 9 bps rung.

A missing depth percentile with a positive order size gives zero impact. cost_model_from_schedule fails closed in the same case (quote_costs.py:1086-1106).

There is no current impact, because calibration is disabled and RTH costs are far below 9 bps.
- Suggested fix: Ladder the round-trip cost (two sides, fee on the sell leg). Return fit_measurement_missing when depth is absent and order_shares > 0.
- How verified by reviewer: Ran _one_way_cost and _ladder. Output: "16 bps p95 spread -> one-way 8.5 -> rung (9.0, None)" and "missing depth, 10k shares -> impact 0.0 one-way 1.7".
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### E1-07 Fee model charges 0.5 bps on both legs; Alpaca API equity trades pay about 0.21 bps SEC fee on sells only
- Severity: LOW  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `research/costs.py:41-42, 695-700; config.yaml:69`
- Evidence: costs.py:41-42 "# Regulatory and exchange fees on notional, charged on both sides." / `DEFAULT_FEE_BPS = 0.5` | costs.py:698-700 `notional = (abs(float(entry_price)) + abs(float(exit_price))) * \` / `float(quantity) * float(multiplier)` / `total = notional * self.fee_bps / 10_000.0`
- Why it matters: The model charges 1.0 bps per round trip against about 0.21 bps in reality (coordinator fact 9; FINRA TAF is negligible, and no exchange fees or commission are passed through). The overstatement is about 5x and the fee is charged on buys. It is small next to the 16 bps of spread and slippage, but it is structurally wrong and inflates the stress-free baseline.
- Suggested fix: Charge about 0.21 bps (plus TAF per share) on the sell leg only, and zero on buys.
- How verified by reviewer: Read costs.py:695-703 and config.yaml:69. The real fee schedule is coordinator fact 9.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### E1-08 Stale '25 bps' runtime-stress references after commit 2249cc5 lowered the scenario to 9 bps; the stale label is persisted into evidence rows
- Severity: LOW  |  Category: doc-drift  |  Confidence: VERIFIED
- Location: `research/stressed_cost_calibration.py:7-8, 28; config.yaml:71; README.md:346-347 (cross-group); research/mechanism_cost_cohort.py:31 (cross-group)`
- Evidence: stressed_cost_calibration.py:8 "missing or untrusted evidence falls back to the configured 25 bps rung." | :28 `DEFAULT_FALLBACK_SCENARIO_BPS = 25.0` | config.yaml:71 `"provenance": "shipped_conservative_v1_plus_25bps_stress",` | README.md:346-347 "9, 15, 25, and 50 bps. The shipped runtime uses 25 bps as its default and / fail-closed fallback"
- Why it matters: The runtime scenario is 9 bps (config.yaml:52). The cost provenance string is copied into every row's cost_model_provenance (factory_core.py:1720), so evidence claims a 25 bps stress the runtime does not apply.

Calibration artifacts report feasible_minimum_stop_bps 83.3 for fallback cells while the runtime enforces 30. The README tells operators that stops must clear about 83 bps.
- Suggested fix: Update the provenance label, README and cohort risk_policy. Take the calibration fallback from risk.stressed_cost_scenario_bps rather than a module constant.
- How verified by reviewer: Read the cited lines. git show 2249cc5 shows "Stressed-cost scenario 25.0 -> 9.0 bps (config.yaml, deploy/paper-orb.config.json)". The 83.33 figure appears in the E1-04 run output.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### E1-09 Strict measured-cost coverage raises out of the replay instead of refusing one opportunity
- Severity: LOW  |  Category: incorrect  |  Confidence: LIKELY
- Location: `research/quote_costs.py:988-991, 495-498; research/factory_core.py:1435-1441, 1718 (cross-group)`
- Evidence: quote_costs.py:988-991 `raise QuoteCostError(` / `"requested measured cost bucket "` / `f"{normalized_symbol}/{normalized_bucket} is unavailable "` / `"or under-covered; refusing symbol-wide fallback")` | quote_costs.py:496 "Enabled measurement is equity-only and returns a fail-closed per-opportunity" | factory_core.py:1438 `resolved = cost_resolver(context)`
- Why it matters: The docstring promises per-opportunity fail-closed behaviour. In practice, QuoteCostError (a ValueError) propagates through resolve_cost_model with no handler in factory_core. One opportunity in an under-covered cell, such as the last half hour of a thin ETF or a post-close exit allowed by session.allow_exits_outside_session (config.yaml:13), would abort the whole measured replay. This is a further practical reason, beyond README.md:342-344, why measured_quote stays disabled.
- Suggested fix: Catch QuoteCostError per opportunity and emit a refusal row with reject_stage 'cost_coverage'.
- How verified by reviewer: Read quote_costs.py:957-1002 and factory_core.py:1433-1441 and 1704-1723. Grep found no QuoteCostError handler in factory_core, strategy_factory, edge_lab or live_shadow. Callers above factory_core were not traced (UNVERIFIED: needs check in research/strategy_factory.py).
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### E1-10 The 500-quote cell floor counts autocorrelated snapshots, not sessions, and pre/post-session quotes enter symbol and universe aggregates
- Severity: LOW  |  Category: incomplete  |  Confidence: VERIFIED
- Location: `research/quote_costs.py:976-983, 806-810, 686-693`
- Evidence: quote_costs.py:978-981 `if (isinstance(measured, Mapping) and measured and` / `required_quotes is not None and` / `observed_quotes is not None and` / `observed_quotes >= required_quotes):` | quote_costs.py:806-808 `for accumulator in (universe, by_symbol[symbol], by_cell[cell]):` / `accumulator.spread.add(spread_bps)` / `accumulator.sessions.add(session)`
- Why it matters: With a 30 s recorder cycle (costs.py:1884), a half-hour cell reaches 500 snapshots after about 9 sessions, or in one session with event-level quotes. The p75/p95 is a pooled quantile of within-session autocorrelated data, and measured mode applies no session floor (only the calibration module requires 5).

Quotes with bucket pre_open or post_close are added to the symbol and universe aggregates, which validate_measured_quote_config checks against caps (quote_costs.py:301-306). This matters only if the recorder stores extended-hours quotes (UNVERIFIED).
- Suggested fix: Add a minimum session count per cell to measure_quote_costs and _cell. Exclude non-RTH buckets from aggregates.
- How verified by reviewer: Read quote_costs.py:702-899 and 957-1002. The recorder cadence and extended-hours behaviour were not checked (cross-group).
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### E1-11 On early-close days the replay policy allows entries 5 minutes after the force-flat time
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/costs.py:535-543`
- Evidence: costs.py:539-542 `force = (closed - timedelta(minutes=flat_minutes)).time().replace(tzinfo=None)` / `latest = (closed - timedelta(minutes=reject_minutes)).time().replace(tzinfo=None)` / `if policy.latest_entry_time is not None:` / `latest = min(latest, policy.latest_entry_time)`
- Why it matters: Config has force-flat at 10 minutes and the entry reject at 5 minutes before close (config.yaml:15-16). On normal days the 15:00 strategy cap hides this. On a 13:00 close, latest entry is 12:55 but force-flat is 12:50, so any entry in that window is flattened immediately and pays a full round trip (17 bps under the model).
- Suggested fix: Clamp latest_entry_time to before force_flat_time (minus one bar), or express the strategy cap relative to the close.
- How verified by reviewer: Ran ReplayPolicy.from_config(config.yaml) with replay_policy_for_session. Output: "normal 2026-10-14 force_flat_time 15:50:00 latest_entry_time 15:00:00" and "early close 2026-11-27 force_flat_time 12:50:00 latest_entry_time 12:55:00". Whether replay entry logic also checks force_flat_time is UNVERIFIED: needs check in research/factory_core.py and the runtime session code.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

## Over-engineering

Of 4,376 assigned lines, about 2,560 (about 58%) serve no decision in the shipped configuration.

- The arithmetic that sets every replayed cost is about 67 lines: CostModel.entry_cost_bps, per_side_bps, execution_price, fees and round_trip_cost (costs.py:676-742), plus one line in stressed_cost_usd (costs.py:1660). That is about 3% of costs.py.
- Measured-mode stack: quote_costs.py (1,237 lines) plus the measured row-binding, regex provenance and tamper-check machinery in costs.py (64-92 and 849-1333, about 515 lines), about 1,750 lines in total. It is disabled (config.yaml:74), no schedule is committed, and the code defends against an adversary (costs.py:909-910: \"an attacker could choose ``measured:fake``\") in a single-operator repository. A 24-row per-symbol spread and fee table (about 30 lines) would do the job.
- Stress calibration: stressed_cost_calibration.py (732 lines) plus ReplayPolicy and veto plumbing (costs.py:243-249, 294-298, 331-337, 369-378, 460-501, 1692-1703; about 75 lines). It is disabled (config.yaml:54), and even if activated it can only output at least the configured 9 bps; it was verified to reproduce 9 bps exactly for SPY (E1-04).
- Hashing: multiset SHA-256 sum/xor digests over every quote (quote_costs.py:726-731, 776-789, 818-821), plus schedule, session and artifact content hashes, and per-cell session-ID lists (quote_costs.py:647) that are embedded into candidate identity (quote_costs.py:325-326).
- About 11 alias names for about 6 functions: costs.py:568, 1363-1365, 1761-1762, 1766; quote_costs.py:73; stressed_cost_calibration.py:718-721.
- Duplicate stress ladder: gates.py:153 versus costs.py:53.
- An unreachable failure string in risk_unit_report (costs.py:1970-1973).

What is not over-engineered: the quote-visibility logic (costs.py:2226-2380) and the SQLite quote index (costs.py:1979-2310), which the strict replay uses.

## Cross-group questions

- H1a recorder: what is the quote recording cadence (costs.py:1884 implies a 30 s cycle), and are pre-market or after-hours quotes stored? If they are, they enter the symbol and universe aggregates (quote_costs.py:806-810) and the pre_open/post_close calibration cells.
- E2/H1b research/market_data.py:706,775: are Alpaca IEX bid_size and ask_size in shares or round lots? quote_costs.py:1107 divides order shares by displayed size.
- F1b research/factory_core.py:1438 and 1718: does any caller up the stack catch QuoteCostError from the measured resolver, or does one under-covered cell abort the whole run (E1-09)?
- A_signals/F1b replay: which instant prices the entry quote fill? Does the replay assume fill certainty while the runtime posts a DAY limit at the IEX touch that may never fill (B-08)? Are take-profit fills booked on touch rather than trade-through (E1-03)?
- D2 research/gates.py:153-154 and 1551: COST_STRESS_SCENARIOS_BPS is redefined rather than imported from costs.py:53. The 9 and 15 bps scenarios are inert for equity rows. Is the split of COST_STRESS_REQUIRED_BPS 25 at the gate versus 9 at runtime intended and documented?
- I1 README.md:345-348 and research/mechanism_cost_cohort.py:31 still describe a 25 bps runtime stress (E1-08). Is the 100 bps AUTHORED_STOP_FLOOR_BPS derived from 25/0.30 = 83.3?
- Runtime session code and agent/risk.py: on early-close days, does the runtime also accept entries between close minus 10 (force-flat) and close minus 5 (entry reject) (E1-11)? Does RiskEngine telemetry report stressed_cost_to_risk_ratio 0.30 for trades whose expected-model ratio is 0.43 to 0.57 (E1-02)?

## Summary

- HIGH E1-01: the static model charges 17 bps per round trip on bar references (8+8 per side, plus 0.5+0.5 fee), 13 bps on top of IEX quotes and 15 bps for a quote entry with a resting exit, all as bps of notional. That is about 7x (XLRE) to 50x (SPY) the real Alpaca cost (zero commission, about 0.21 bps SEC fee on sells, one-cent spreads). The repository's own commit 2249cc5 says six arms beat a realistic 1.2 bps.
- In R terms: at the 30 bps floor with a 2R target, 17 bps is 0.57R and moves the break-even win rate from 33.3% to 52.2% (61.1% under the gate's 25 bps). It equals 6 to 27 median one-minute moves. The cost model is decisive at the margin, although nothing survived multiplicity even at 1.2 bps.
- Both stop floors come from the stress arithmetic, not the market: 30 bps = 9/0.30 (commit 2249cc5 chose 9 for exactly this reason; B-05), and the 100 bps cohort floor is above 25/0.30 = 83.3 (mechanism_cost_cohort.py:31, LIKELY).
- MEDIUM E1-02: the runtime 'stressed' cost (9 bps all-in) is below the expected round trip (13 to 17 bps). The 0.30 veto does not hold under the model's own expectation (0.43 to 0.57 at the floor), and the 9 and 15 bps gate rungs are inert for equity.
- MEDIUM E1-03: the same 8 bps is charged on a take-profit limit leg (a 100.60 target is booked at 100.5195) and IEX-quote entries pay the touch plus 6 bps, although the runtime entry is a limit that cannot slip but can fail to fill (B-08). IEX-touch fills are pessimistic on price and optimistic on fill certainty.
- MEDIUM E1-04: stress calibration can never go below the configured 9 bps. Verified on synthetic one-cent quotes: SPY gets 9 bps in every cell (no change), and a one-tick change in XLRE's p95 invalidates the whole artifact. Its 732 lines change no decision.
- MEDIUM E1-05: measured mode (disabled, because no corpus-specific schedule exists, README.md:342-344) replaces slippage with depth impact only, so stop exits would cost about 0.07 bps on SPY. It is not a drop-in fix and is fitted on IEX-only spread and depth.
- LOW: one-way cost mapped onto the all-in ladder and fail-open on missing depth (E1-06); fees charged on both legs (E1-07); stale 25 bps labels persisted into rows (E1-08); strict coverage aborts the replay (E1-09); the 500-quote floor counts snapshots, not sessions (E1-10); early-close latest entry falls after force-flat (E1-11).
- Over-engineering: about 58% of the 4,376 assigned lines (the measured-mode stack of about 1,750 lines and the calibration stack of about 805 lines) serve no shipped decision. The cost arithmetic itself is about 67 lines.
