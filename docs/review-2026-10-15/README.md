# Independent review of the Alpaca intraday ETF trading system

Date: 15 October 2026. Reviewed commit: `eefb965` on branch `claude/laughing-fermat-83p4gd`. Scope: all 343 tracked files (about 202,000 lines), plus fresh market research on every registered strategy arm.

This document is the hand-over. Every finding links to a handoff file in [`reviews/`](reviews/) that holds the evidence (file and line, a verbatim quote, and how it was verified). The fresh research is reproducible from [`research/`](research/README.md).

## Contents

1. [Executive verdict](#1-executive-verdict)
2. [Scope, method, and how hallucination was controlled](#2-scope-method-and-how-hallucination-was-controlled)
3. [First principles used as the yardstick](#3-first-principles-used-as-the-yardstick)
4. [Scorecard: every hypothesis, as coded and as it performs](#4-scorecard-every-hypothesis-as-coded-and-as-it-performs)
5. [The strategy as it is actually implemented](#5-the-strategy-as-it-is-actually-implemented)
6. [Fresh research](#6-fresh-research)
7. [Findings by severity](#7-findings-by-severity)
8. [Wrong assumptions](#8-wrong-assumptions)
9. [What is missing](#9-what-is-missing)
10. [Over-engineering, quantified](#10-over-engineering-quantified)
11. [What to do: three paths](#11-what-to-do-three-paths)
12. [Sources](#12-sources)
13. [Appendix: evidence index](#13-appendix-evidence-index)

## 1. Executive verdict

1. **There is no tradeable edge in the catalogue.**
   - I replayed all 82 registered arms (12 rule families plus IBR, across every cohort) with the repository's own signal and exit code. The data was 22 fresh sessions, 14 of which no committed analysis had seen.
   - After realistic costs:
     - none is significantly profitable;
     - 47 are significantly negative;
     - the average arm loses 4.4 bps per trade.
   - The best arm, an opening-range fade with a t-statistic of +1.59, is what chance alone produces from 82 tries. 41% of its profit came from a single day.
   - Classic intraday hypotheses tested over 2 to 16 years on the same ETFs agree. The only consistent structure is mild 30 to 60 minute mean reversion worth 2 to 3 bps gross, which is about the size of a realistic round-trip cost.
2. **The runtime has defects that can lose money or leave positions open overnight before any edge could show:**
   - close order ids are reused across days (B-01, critical);
   - flatten-driven closes are never booked, so trades become phantoms that block resumption and poison the next trade on the same symbol (C1-01, critical);
   - a failed flatten leaves the position without protective orders (C1-02);
   - a paused trader skips the 15:50 flat while its heartbeat stays healthy (C2-01);
   - one transient error or one rejected order liquidates the whole book (B-03, B-04);
   - bracket legs are anchored to a different price from the fill (B-02).
3. **The research machinery cannot find or confirm an edge, by construction:**
   - The cost model charges 17 bps per round trip against a realistic 1.5 to 4 bps (E1-01).
   - The 30 bps and 100 bps stop floors were derived from that stress arithmetic, so the ATR settings are dead and most trades are time exits (A-02, B-05).
   - A proof needs about 25 bps of gross edge per trade (D2-01).
   - The fit and held-out split needs at least 100 self-recorded IEX sessions, so the first possible proof is around mid-January 2027 (F1a-03). Session acceptance has so far accepted no sessions at all (C2-02).
   - The online false-discovery budget starves after a few tests (D1-08).
   - The live-shadow lane is the only route to authorisation, and it fails twice over:
     - Its virtual book holds capital for the whole session, while the replay it is checked against releases capital as trades exit. Any arm with three or more entries in a session is therefore quarantined, and 94% of arms average that many (the median is 18 entries per session) (G1a-03).
     - It stops for good after 100,000 stored decisions, about 11 sessions for a single arm (G1a-02).
   - The factory's own power diagnostic implies about 3,500 trades for the effect size it targets (F2b-02).
4. **The system is heavily over-engineered.** About 202,000 lines support a system that has not yet traded a single validated edge: 63,000 lines of research code, 62,000 of tests and 30,000 of documentation and audit evidence. Examples:
   - one 1,983-line function;
   - a 2,527-line subsystem that nothing calls;
   - an 8,349-line shadow runner;
   - seven rule-schema versions;
   - a 1,940-line bash orchestrator;
   - more than 7,000 lines of operating documentation.
5. **On win rate.** The arms with the highest win rates in the replay (60 to 61% net of costs, 71 to 73% gross: mean reversion with near targets) earn **minus 0.1 bps per trade**. A 60 to 70% win rate is a property of where the stop and target sit, not evidence of edge.

**Recommendation (Path A, section 11):**

- Fix the runtime correctness defects first.
- Make the configuration honest: risk per trade, per-symbol costs and volatility-scaled stops.
- Move research onto Alpaca's free historical consolidated (SIP) bars, which go back to 2016.
- Cut the system to a 10,000 to 15,000 line core.
- Research one hypothesis at a time, with a power calculation up front.
- Do not resume paper or live trading on the current runtime.

## 2. Scope, method, and how hallucination was controlled

### 2.1 What was reviewed

Every tracked file was assigned to exactly one review group (`live_shadow.py` was split into two halves). The file-by-file ledger is [`reviews/COVERAGE.md`](reviews/COVERAGE.md): 343 files, each with its group, reviewer, depth and handoff.

| area | files | lines | how it was reviewed |
| --- | --- | --- | --- |
| Trading-critical code: runtime, signals, execution, state, statistics, gates, costs, factory core, live shadow | 73 | about 53,700 | every line, by review agents working to a shared evidence brief |
| Research tooling, deployment, infrastructure | 65 | about 50,700 | by the coordinator, with targeted reading: full function inventories, every decision path that affects correctness, pattern searches across the tree, and code execution where useful |
| Documentation and audit evidence | 41 | about 35,700 | every concrete parameter checked against the code; key documents read in full; audit JSON reloaded and checked number by number |
| Tests | 164 | about 62,200 | complete AST inventory of every test; full suite run (2,359 tests: 2,358 pass, 1 fails); targeted reading of the trading-critical modules |

### 2.2 Who reviewed what

| group | content | reviewer | handoff |
| --- | --- | --- | --- |
| A | signal grammar and contracts | agent | [A](reviews/handoff-A_signals.md) |
| B | execution, broker, risk | agent | [B](reviews/handoff-B_execution_broker.md) |
| C1 | state, journal, paper trial | agent | [C1](reviews/handoff-C1_state_trial.md) |
| C2 | startup, session acceptance, watchdog | agent | [C2](reviews/handoff-C2_startup_acceptance_watchdog.md) |
| D1 | statistics, signal quality, preregistration | agent | [D1](reviews/handoff-D1_stats_signal_prereg.md) |
| D2 | proof gates | agent | [D2](reviews/handoff-D2_gates.md) |
| E1 | cost model | agent | [E1](reviews/handoff-E1_costs.md) |
| F1b | factory simulator core | agent | [F1b](reviews/handoff-F1b_factory_core.md) |
| G1a, G1b | live shadow runner | agent | [G1a](reviews/handoff-G1a_live_shadow_part1.md); G1b in progress |
| E2 | market-data normalisation, cost tools | coordinator | [E2](reviews/handoff-E2_coordinator.md) |
| F1a | strategy factory | coordinator | [F1a](reviews/handoff-F1a_coordinator.md) |
| F2a | factory ledger and report | coordinator | [F2a](reviews/handoff-F2a_coordinator.md) |
| F2b | LLM proposer, fit diagnostics | coordinator | [F2b](reviews/handoff-F2b_coordinator.md) |
| F3a | edge discovery and research CLI | coordinator | [F3a](reviews/handoff-F3a_coordinator.md) |
| F3b | edge ledger | coordinator | [F3b](reviews/handoff-F3b_coordinator.md) |
| G1c | shadow ingestion | coordinator | [G1c](reviews/handoff-G1c_coordinator.md) |
| G2a | diagnostic accounts, trader research | coordinator | [G2a](reviews/handoff-G2a_coordinator.md) |
| G2b | paper epochs, projection | coordinator | [G2b](reviews/handoff-G2b_coordinator.md) |
| H1a | recorder | coordinator | [H1a](reviews/handoff-H1a_coordinator.md) |
| H1b | backfill, dataset, cache | coordinator | [H1b](reviews/handoff-H1b_coordinator.md) |
| H2a | research orchestration | coordinator | [H2a](reviews/handoff-H2a_coordinator.md) |
| H2b, I1 | infrastructure, top-level documentation | coordinator | [I1 and H2b](reviews/handoff-I1_H2b_coordinator.md) |
| I2 | September audit evidence | coordinator | [I2](reviews/handoff-I2_audit_coordinator.md) |
| J | all tests | coordinator | [J](reviews/handoff-J_tests_coordinator.md) |

### 2.3 Handoff protocol

Every review agent worked from one shared brief, [`reviews/BRIEF.md`](reviews/BRIEF.md). It set these rules:

- **Read to the end.** Read every assigned file to its last line, and list each file with its line count and a "read to end" confirmation.
- **Cite and quote.** Cite `path:line` and quote 1 to 4 lines verbatim for every finding. No quote, no finding.
- **Label confidence.** Every finding carries VERIFIED (code path traced end to end, or code executed), LIKELY or SPECULATIVE.
- **Mark cross-group claims.** Anything depending on a file outside the reviewer's group is marked cross-group, or "UNVERIFIED: needs check in ...".
- **Use the coordinator facts.** A numbered list of facts the coordinator had already verified was shared, so agents did not re-derive or contradict them. One example: FINRA retired the pattern day trader rule on 4 June 2026.

Agents returned schema-enforced structured results. Agents could not write files, so the coordinator converted each result into a Markdown handoff with [`reviews/tools/journal_to_md.py`](reviews/tools/journal_to_md.py). Scripts that reviewers ran to verify claims are in [`reviews/verification/`](reviews/verification/).

### 2.4 Verification of the most serious findings

The planned second stage, two independent verifiers per finding, could not run within the account's usage limits. In its place, the coordinator personally re-verified **every CRITICAL and HIGH finding** by re-reading the cited lines. Code was executed where possible, for example:

- the cost arithmetic (17.0, 13.0 and 15.0 bps round trips; a 100.60 take-profit booked at 100.51952);
- the failing test;
- the stop geometry on fresh data.

Corrections made along the way are part of the record:

- **PDT rule.** One reviewer flagged missing pattern day trader handling (B-14). It is moot, because the rule was retired in June 2026.
- **`exit_before_minutes`.** My first replay read it as minutes after the open. The signals reviewer showed that it means minutes before 16:00 (`agent/contracts/rule.py:1103-1107`). The replay was corrected and re-run before any number in this report was produced.
- **IBR as the default strategy.** An early draft called IBR the runtime's default strategy. The default is `strategy.id: rule` (`agent/config.py:85`, `config.yaml:26`), and IBR is now a diagnostic comparison family.
- **Rolling-R demotion rule.** Before writing up the "last 20 trades lose 2R" rule, I checked the code: it is advisory only (`research/edge_ledger.py:705-722`). The finding was reframed accordingly (F3b-03).
- **Repeated hypotheses.** Twice I suspected a broken CLI branch or a P&L sign error. Each time I read the full function and found the code correct, and no finding was written.

For the live-shadow findings, the coordinator re-read the replay key, the gate join and the restart path (G1a-01). It also re-read the global decision bound and the shipped default (G1a-02), and the session-long virtual book against the replay's release of capital (G1a-03). The frequency of G1a-03 was then measured from the fresh replay.

MEDIUM and LOW findings carry the reviewer's own confidence label. Several were spot-checked: F1b-01, F1b-02, E1-02 and E1-11.

### 2.5 Limits

- **No VM data.** The VM's recorder corpus (Alpaca IEX, forward-observed) could not be reached: this environment has no Alpaca credentials and no network path to the VM. The fresh research therefore uses public consolidated-tape bars for the same 24 ETFs (section 6.1). It is a faithful, independent measurement of signal value. It is not decision-bearing under the repository's own protocol.
- **Short one-minute sample.** One-minute data covers 22 sessions, which is one regime. Longer samples (42 five-minute sessions, two years of hourly bars and 16 years of daily bars) were used for the hypotheses that can be tested at those resolutions.
- **Targeted coordinator reviews.** The groups reviewed by the coordinator received targeted reading, not a line-by-line pass. The handoffs say exactly what was read.

## 3. First principles used as the yardstick

1. **Net expectancy per trade, set against round-trip cost, is the whole game.** A strategy earns `win% x average win - loss% x average loss - costs`. A 60 to 70% win rate means nothing on its own: mean-reversion systems win often and small, and breakout systems win rarely and large. What matters is net basis points per trade, and whether that holds up across independent days.
2. **Know your real costs to a tenth of a basis point.** On these 24 ETFs, a marketable round trip costs about one quoted spread (one cent is 0.13 bps on SPY at $778, and 2.4 bps on XLU or XLRE at about $42), plus slippage. There is also the SEC Section 31 fee, charged on sells only: $20.60 per million from 4 April 2026, about 0.2 bps. Alpaca charges no commission on API equity trades. A realistic all-in round trip is therefore about 1.5 to 4 bps, not 17.
3. **The edge must exceed noise by enough to be found with the data you have.** In this period a 60-minute return had a standard deviation of about 16 bps on SPY, 8.5 bps on HYG and 43 bps on SLV. At a typical 20 bps, detecting a 3 bps edge at t = 2 needs about `(2 x 20 / 3)^2`, or 180, *independent* trades. Twenty-four ETFs on one day are not 24 independent observations: the day is the unit.
4. **Scale stops and targets to each instrument's volatility over the holding period.** A fixed 30 bps stop is a coin flip on SLV and a near-certain time exit on HYG.
5. **Execution quality is part of the edge.**
   - Act on a completed bar within seconds.
   - Use marketable limits with a short time to live, and never let an unfilled entry rest for hours.
   - Model non-fills and adverse selection in research the way the runtime experiences them.
6. **Data must match the venue you trade.** Alpaca's free real-time feed is IEX only: one exchange with a few per cent of volume. Bars, VWAP and relative volume built from it are sparse and noisy. The paid SIP feed provides consolidated bars and NBBO quotes, and the free plan already includes historical SIP bars.
7. **Regime matters more than parameter tuning.** Intraday mean reversion and momentum alternate with volatility regimes, and 22 sessions cover only one regime.
8. **Simplicity is a risk control.** Every line that does not change a trade decision is a place for a bug to hide. A system that cannot place a trade without passing dozens of gates is not safer: it is untested where it matters.
9. **Risk is set by position size and stop distance, and must match what the configuration says.**
10. **Flat by the close means flat by the close, every day,** including restarts, rejections and broker errors. Any path that can leave a position open overnight is a bigger risk than any signal.

## 4. Scorecard: every hypothesis, as coded and as it performs

The figures are fresh runtime-faithful replays: the first trade per symbol-session (the runtime's rule), realistic costs, 22 sessions from 14 September to 13 October 2026, the session as the inference unit, and the baseline arm of each family. "Unseen" means the 14 sessions from 24 September onwards. Full tables are in section 6.

| hypothesis | mechanism claimed | how it is coded, and the main flaw | trades | win rate | net bps per trade (t) | unseen net | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Opening-range breakout | opening imbalance persists after leaving the range | close beyond the 15-minute range by 5 bps; fires on every bar while true (A-04) | 491 | 40.7% | -5.2 (-2.59) | -5.6 | reject on ETFs: the documented edge needs stocks in play |
| Opening-range fade | opening overshoot reverses | poke above the range, close back inside; stop measured from the close, not the failed high (A-05) | 124 | 54.0% | **+5.8 (+1.39)** | +4.4 | fragile: 41% of profit from one day, and a structurally correct version loses. Keep as a sealed forward test only |
| Momentum continuation | slow institutional execution continues | lookback return beyond a threshold | 472 | 37.1% | -5.4 (-2.70) | -4.2 | reject |
| Mean reversion (z-score) | liquidity shocks revert | z-score against 20 closes; a 45 bps target inside a 12 bps range (A-03) | 457 | 42.0% | -4.2 (-4.29) | -4.1 | the mechanism is real but the target geometry kills it. With near targets: 60% win rate, -0.1 bps |
| Trend pullback | trend resumes after a pullback | fast SMA above slow SMA, close near the fast SMA; the confirmation repeats the predicate (A-14) | 528 | 42.2% | -4.3 (-2.84) | -3.7 | reject |
| Volatility breakout | compression releases | narrow prior range, then a close beyond it | 89 | 41.6% | -3.9 (-1.24) | -2.7 | reject |
| Volume breakout | volume-backed displacement | volume multiple not adjusted for time of day (A-10) | 327 | 38.8% | -3.3 (-1.71) | -1.7 | reject |
| VWAP reversion | impact away from VWAP decays | close at least 25 bps from the IEX-volume VWAP, then fade | 477 | 43.6% | -1.6 (-0.69) | -0.2 | weak real drift (+3.2 bps control-adjusted at 60 minutes, t 1.12), too small for costs |
| VWAP trend | accumulation | the mirror of VWAP reversion | 383 | 35.8% | -8.6 (-3.76) | -9.5 | reject: it takes the losing side of the one real structure |
| Range expansion | expansion persists | bar range at least 2x average; reuses `volume_multiplier` as the range multiple (A-15) | 509 | 32.4% | -8.8 (-5.26) | -9.3 | reject |
| Opening drive | one-sided open continues | 30-minute drive beyond 30 bps | 189 | 39.7% | -4.7 (-1.73) | -3.5 | reject |
| Cross-sectional residual | relative momentum against SPY | raw return minus SPY, not beta-adjusted (A-09) | 396 | 39.9% | -3.4 (-1.23) | -4.9 | reject as coded; mostly a market-direction bet |
| IBR (initial balance range) | the opening-range break continues | 15-minute range, relative volume against the opening-range mean | 369 | 33.3% | **-13.1 (-3.33)** | -17.4 | reject: the worst family |

The two preregistered hypotheses, evaluated on their sealed windows (diagnostic only):

| hypothesis | in-sample claim | sealed window now | decay |
| --- | --- | --- | --- |
| VWAP reversion, control-adjusted, 60 minutes | +9.44 bps, t 4.34 | +3.50 bps, t 0.92 (16 sessions) | 63% |
| Opening-range fade, control-adjusted, 60 minutes | +19.5 bps, t 2.95 | +2.04 bps, t 0.36 (14 sessions) | 90% |

## 5. The strategy as it is actually implemented

### 5.1 What the system is

| area | lines |
| --- | --- |
| runtime (`agent/`) | 20,498 |
| research (`research/`) | 62,913 |
| deployment and operations (`deploy/`) | 19,558 |
| tests | 62,159 |
| documentation and audit evidence (`docs/`) | 30,162 |
| top-level files (README, architecture, configuration and similar) | 6,867 |

Around one bounded grammar of intraday rules, the repository builds:

- a market-data recorder;
- a research "strategy factory" with an LLM proposer;
- statistical gates;
- a broker-free shadow runner;
- an edge ledger;
- preregistered hypotheses;
- a paper-trial lane;
- a deterministic Alpaca trader that uses broker-resident bracket orders.

At the last verified deployment (`docs/current-findings.md:3-5`), on 11 October 2026:

- the paper account was **paused and flat**;
- the frozen ORB trial had **zero accepted sessions**;
- **no strategy was eligible to trade**.

### 5.2 The geometry every arm really trades

- **Stops** are `max(14-bar one-minute ATR x stop_atr, 30 bps)` (`agent/contracts/rule.py:76-77`, `:2681`).
  - The median absolute one-minute move on these ETFs runs from 0.64 bps (HYG) to 2.97 bps (SLV), so the 30 bps floor sets essentially every stop. The ATR parameters are dead (A-02).
  - Every arm trades a fixed **30 bps stop with a 45 to 60 bps target**. The v7 "cost-compatible" cohorts use a 100 bps stop with a 200 bps target, and in the replay 95 to 100% of their trades end on time.
  - The same "1R" means completely different things across the 24 instruments: it is a coin flip on SLV and almost never touched on HYG.
- **Sizing** is `min(0.5% of equity / stop, 25% of equity / price)` (`agent/risk.py:123-149`).
  - With a 30 bps stop the uncapped size would be 167% of equity, so the notional cap always binds, and **real risk per trade is 0.075% of equity, not 0.5%** (B-06; pinned by `tests/test_risk.py:132-147`).
  - The 50% gross cap allows at most two positions (`agent/risk.py:784-786`). So `max_concurrent_positions: 3` can never bind, and neither can the 2% open-risk cap or, in practice, the 2% daily loss limit.
- **Entries.** The runtime converts entries to DAY limit orders at the current IEX quote, with broker-resident bracket legs. The legs stay anchored to the *signal-bar close* while the entry is re-priced to the live quote (B-02). The entry limit never expires before the force flat (B-08).
- **Data.** The runtime uses REST polling of the free IEX feed (the trader once a minute, the recorder every 30 seconds). Opening-range and VWAP families need a gap-free IEX session, so one missing minute disables that symbol for the rest of the day (A-08, B-12).
- **Time.**
  - No entries after 15:00, and flat at 15:50.
  - The v4 "thesis deadline" is 16:00 minus `exit_before_minutes`, so a value of 360 means 10:00 (A-01).
  - On early-close days, entries are allowed until five minutes after the force-flat time, because the 15:00 cap is a fixed wall-clock time (C2-05, E1-11).
- **Costs in research.** The model uses a 4 bps spread, 6 bps slippage per side and a 0.5 bps fee per leg. That is 17 bps per round trip on bar prices, and 13 bps on top of quotes (E1-01).
- **Promotion path.** A rule must pass the factory's fit and held-out gates and a sealed qualification window. It then needs a live-shadow run of at least 60 consecutive complete sessions, split into disjoint selection and confirmation halves (G1c-01), followed by an online FDR test and the edge ledger's checks. Only after all of that can it be paper-traded.

## 6. Fresh research

### 6.1 Data, and what it can and cannot prove

| dataset | sessions | span | use |
| --- | --- | --- | --- |
| one-minute bars | 22 | 14 Sep to 13 Oct 2026 | every registered arm, through the repository's own evaluators |
| five-minute bars | 42 | 17 Aug to 14 Oct 2026 | professional variants over a longer window |
| hourly bars | 487 complete | Oct 2024 to Oct 2026 | classic intraday hypotheses over two years |
| daily bars | about 4,220 | Jan 2010 to Oct 2026 | gap and overnight-drift hypotheses over 16 years |

Data quality was checked before use:

- Each symbol-session has 362 to 390 one-minute bars, and no session is missing.
- The 14 sessions from 24 September onwards are unseen: no analysis committed to the repository has examined them.
- These are public consolidated-tape bars, not the IEX bars the runtime trades, so none of this is decision-bearing under the repository's protocol.

### 6.2 Method

- **The repository's own code.** Every registered arm was replayed with `agent.contracts.rule.evaluate_rule_signal`, `initialize_exit_state` and `completed_bar_exit_transition`, and with `agent.contracts.ibr.generate_ibr_signal`. That covers 82 arms: the 24-arm diagnostic cohort, 7 IBR variants, and the v1, v2, cost-compatible, deadline-compatible and refresh mechanism cohorts.
- **The runtime's rules, mirrored:**
  - one trade per symbol per session (`agent/engine_cycle.py:743`);
  - entry at the next bar's open, plus a one-bar-delay sensitivity check;
  - no entry at or after 15:00, and flat at 15:50;
  - the 30 bps stressed-cost veto;
  - the thesis deadline at 16:00 minus `exit_before_minutes`;
  - the maximum hold as the entry bar plus `max_hold_bars`;
  - when a bar touches both the stop and the target, the stop is assumed to have hit first.
- **Three cost levels:**
  1. gross;
  2. realistic: the larger of one tick and the Corwin-Schultz spread estimate, plus 1.0 bps slippage and 0.28 bps fees;
  3. the shipped model, at 17 bps.
- **Inference.** The session is the unit: a CR1 clustered t, and a session sign-flip test.
- **Independent replication.** My own clock-matched signal-quality measure was checked against the repository's instrument (`research.signal_quality.measure_signal_quality`) on identical arms and windows. Across 9 arms and 3 horizons, the deltas agree to within about 0.6 bps and the clustered t to within about 0.25 (`research/results/crosscheck_sq.txt`).

### 6.3 Headline result

| measure (runtime-faithful: first trade per symbol-session) | value |
| --- | --- |
| arms replayed | 82 (80 produced trades) |
| arms with a positive mean net of realistic costs | 3, all opening-range fade; none significant (best clustered t +1.59) |
| arms significantly negative after realistic costs (t below -2) | 47 |
| arms significantly positive after realistic costs (t above +2) | 0 |
| average arm: gross / realistic net / shipped cost model | -1.5 / -4.4 / -18.5 bps per trade |
| arms producing zero trades because of the 10:00 thesis-deadline defect | 2 |
| share of exits that are time exits, 100 bps-stop cohorts | 95 to 100% |
| effect of entering one bar later | about 0.2 bps on average (latency is not the problem) |
| median win rate across arms, net | 41.6% |
| highest win rate, net | 60.6% (expectancy +0.1 bps) and 60.4% (expectancy -0.1 bps) |

With 82 highly correlated arms tested, the largest t-statistic would be expected to reach roughly 2.0 to 2.4 by chance alone. A best value of +1.59 is therefore fully consistent with **no edge anywhere in the catalogue**.

### 6.4 The 24-arm diagnostic cohort and IBR

| family | arm | first trades | gross bps | net bps | clustered t | net, 14 unseen sessions | 60-min control-adjusted delta (t) | exits: stop / target / time and flat |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cross_sectional_residual | baseline | 396 | -0.6 | -3.4 | -1.23 | -4.9 | -3.4 (-0.84) | 49% / 16% / 34% |
| cross_sectional_residual | variant | 391 | -0.8 | -3.6 | -1.27 | -5.6 | -2.0 (-0.49) | 49% / 17% / 34% |
| mean_reversion | baseline | 457 | -1.5 | -4.2 | -4.29 | -4.1 | +0.1 (+0.12) | 18% / 4% / 78% |
| mean_reversion | variant | 452 | -1.7 | -4.4 | -4.61 | -4.5 | +0.6 (+0.51) | 20% / 4% / 76% |
| momentum_continuation | baseline | 472 | -2.5 | -5.4 | -2.70 | -4.2 | -3.1 (-1.27) | 45% / 11% / 44% |
| momentum_continuation | variant | 407 | -4.2 | -7.1 | -3.26 | -6.7 | -3.6 (-1.27) | 48% / 11% / 41% |
| opening_drive | baseline | 189 | -1.7 | -4.7 | -1.73 | -3.5 | -3.6 (-1.20) | 45% / 11% / 44% |
| opening_drive | variant | 122 | -3.5 | -6.5 | -2.36 | -6.1 | -5.8 (-1.91) | 47% / 9% / 44% |
| opening_range_breakout | baseline | 491 | -2.4 | -5.2 | -2.59 | -5.6 | -3.5 (-1.86) | 39% / 8% / 53% |
| opening_range_breakout | variant | 475 | -1.9 | -4.7 | -2.57 | -5.4 | -3.7 (-2.06) | 39% / 9% / 52% |
| opening_range_fade | baseline | 124 | +8.8 | +5.8 | +1.39 | +4.4 | +9.8 (+1.42) | 35% / 40% / 25% |
| opening_range_fade | variant | 59 | +12.1 | +9.0 | +1.59 | +5.1 | +16.4 (+1.33) | 34% / 46% / 20% |
| range_expansion | baseline | 509 | -6.0 | -8.8 | -5.26 | -9.3 | -4.2 (-2.24) | 42% / 6% / 52% |
| range_expansion | variant | 479 | -0.9 | -3.7 | -1.60 | -4.3 | -0.1 (-0.03) | 31% / 8% / 62% |
| trend_pullback | baseline | 528 | -1.5 | -4.3 | -2.84 | -3.7 | -2.7 (-1.52) | 40% / 9% / 51% |
| trend_pullback | variant | 528 | -2.2 | -5.0 | -3.08 | -5.3 | -3.0 (-1.75) | 42% / 9% / 50% |
| volatility_breakout | baseline | 89 | -1.4 | -3.9 | -1.24 | -2.7 | -0.4 (-0.11) | 19% / 2% / 79% |
| volatility_breakout | variant | 213 | +1.6 | -1.1 | -0.42 | -1.4 | +1.7 (+0.68) | 21% / 7% / 73% |
| volume_breakout | baseline | 327 | -0.4 | -3.3 | -1.71 | -1.7 | -2.3 (-0.82) | 34% / 10% / 56% |
| volume_breakout | variant | 310 | -1.0 | -3.9 | -2.04 | -3.0 | -2.1 (-0.72) | 35% / 9% / 56% |
| vwap_reversion | baseline | 477 | +1.2 | -1.6 | -0.69 | -0.2 | +3.2 (+1.12) | 38% / 25% / 37% |
| vwap_reversion | variant | 401 | +1.5 | -1.4 | -0.73 | -0.9 | +0.4 (+0.17) | 37% / 24% / 39% |
| vwap_trend | baseline | 383 | -5.7 | -8.6 | -3.76 | -9.5 | -5.6 (-1.90) | 47% / 8% / 45% |
| vwap_trend | variant | 289 | -7.1 | -10.1 | -4.20 | -12.7 | -6.3 (-1.93) | 51% / 10% / 39% |
| ibr | ibr.baseline | 369 | -10.4 | -13.1 | -3.33 | -17.4 | -3.0 (-1.45) | 33% / 3% / 64% |
| ibr | ibr.buffer.0bps | 403 | -9.7 | -12.5 | -3.38 | -16.9 | -2.5 (-1.32) | 35% / 4% / 62% |
| ibr | ibr.buffer.10bps | 321 | -11.5 | -14.3 | -3.80 | -19.6 | -4.5 (-1.94) | 30% / 2% / 67% |
| ibr | ibr.range.30 | 382 | -12.5 | -15.2 | -3.57 | -19.7 | -4.3 (-1.67) | 30% / 2% / 68% |
| ibr | ibr.range.45 | 328 | -7.1 | -9.8 | -2.55 | -11.2 | -0.1 (-0.04) | 22% / 2% / 76% |
| ibr | ibr.target.1_5r | 369 | -9.3 | -12.0 | -3.20 | -16.0 | -3.0 (-1.45) | 32% / 8% / 59% |
| ibr | ibr.target.3r | 369 | -10.6 | -13.4 | -3.30 | -18.1 | -3.0 (-1.45) | 34% / 1% / 66% |

### 6.5 Mechanism cohorts (v1, v2, cost-compatible, deadline-compatible, refresh)

| cohort | family | arm | first trades | gross bps | net bps | clustered t | net, unseen | exits: stop / target / time and deadline |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cost_v1 | cross_sectional_residual | #0 | 396 | -2.0 | -4.8 | -1.45 | -9.2 | 2% / 0% / 98% |
| cost_v1 | mean_reversion | #0 | 161 | +0.8 | -2.0 | -2.44 | -0.9 | 0% / 30% / 70% |
| cost_v1 | momentum_continuation | #0 | 463 | -0.7 | -3.6 | -2.20 | -2.7 | 1% / 0% / 99% |
| cost_v1 | opening_drive | #0 | 160 | -1.7 | -4.6 | -2.10 | -6.1 | 0% / 0% / 100% |
| cost_v1 | opening_range_breakout | #0 | 483 | -2.0 | -4.8 | -2.86 | -6.4 | 1% / 0% / 99% |
| cost_v1 | opening_range_fade | #0 | 11 | -1.3 | -4.7 | -1.85 | -7.7 | 0% / 9% / 91% |
| cost_v1 | range_expansion | #0 | 454 | -0.9 | -3.7 | -2.43 | -4.1 | 0% / 0% / 100% |
| cost_v1 | trend_pullback | #0 | 512 | -0.5 | -3.3 | -2.31 | -2.8 | 0% / 0% / 100% |
| cost_v1 | volatility_breakout | #0 | 287 | -0.0 | -3.0 | -1.62 | -3.3 | 2% / 0% / 98% |
| cost_v1 | volume_breakout | #0 | 223 | -2.3 | -5.2 | -2.10 | -4.9 | 1% / 0% / 99% |
| cost_v1 | vwap_reversion | #0 | 0 | n/a | n/a | n/a | n/a | no trades (A-01) |
| cost_v1 | vwap_trend | #0 | 504 | +1.1 | -1.7 | -0.70 | -2.3 | 2% / 0% / 98% |
| deadline_v1 | mean_reversion | #0 | 528 | +2.7 | -0.1 | -0.09 | +1.2 | 0% / 68% / 32% |
| deadline_v1 | opening_range_fade | #0 | 71 | +3.3 | +0.1 | +0.05 | +2.5 | 0% / 59% / 41% |
| deadline_v1 | vwap_reversion | #0 | 397 | +0.4 | -2.4 | -2.47 | -1.7 | 0% / 10% / 90% |
| mech_v1 | opening_range_breakout | opening_continuation#0 | 495 | -2.8 | -5.6 | -2.33 | -7.7 | 36% / 7% / 58% |
| mech_v1 | opening_range_breakout | opening_continuation#1 | 489 | -3.3 | -6.1 | -2.65 | -7.3 | 35% / 6% / 59% |
| mech_v1 | opening_range_breakout | opening_continuation#2 | 468 | -2.3 | -5.1 | -3.23 | -4.4 | 30% / 3% / 67% |
| mech_v1 | opening_range_breakout | opening_continuation#3 | 489 | -1.9 | -4.7 | -3.09 | -6.0 | 23% / 3% / 74% |
| mech_v1 | trend_pullback | #0 | 528 | -3.1 | -5.9 | -3.31 | -6.3 | 37% / 5% / 58% |
| mech_v1 | trend_pullback | #1 | 450 | -3.3 | -6.1 | -4.31 | -5.4 | 31% / 3% / 66% |
| mech_v1 | trend_pullback | #2 | 356 | -3.4 | -6.3 | -4.26 | -5.5 | 26% / 3% / 71% |
| mech_v1 | trend_pullback | #3 | 356 | -2.8 | -5.6 | -3.99 | -4.9 | 28% / 3% / 69% |
| mech_v1 | vwap_reversion | fair_value_reversion#0 | 494 | +0.0 | -2.8 | -1.79 | -1.7 | 22% / 32% / 46% |
| mech_v1 | vwap_reversion | fair_value_reversion#1 | 491 | +0.8 | -2.1 | -1.22 | -0.1 | 18% / 36% / 46% |
| mech_v1 | vwap_reversion | fair_value_reversion#2 | 449 | +0.3 | -2.5 | -1.77 | -1.8 | 16% / 30% / 54% |
| mech_v1 | vwap_reversion | fair_value_reversion#3 | 449 | -0.1 | -2.9 | -2.88 | -1.5 | 9% / 22% / 69% |
| mech_v2 | cross_sectional_residual | #0 | 396 | -0.1 | -2.9 | -1.24 | -5.4 | 35% / 8% / 58% |
| mech_v2 | mean_reversion | #0 | 161 | +0.7 | -2.0 | -2.44 | -0.9 | 1% / 30% / 70% |
| mech_v2 | momentum_continuation | #0 | 463 | -0.2 | -3.1 | -2.10 | -2.3 | 28% / 4% / 67% |
| mech_v2 | opening_drive | #0 | 160 | -1.8 | -4.7 | -1.92 | -6.5 | 29% / 4% / 67% |
| mech_v2 | opening_range_breakout | #0 | 483 | -1.3 | -4.1 | -2.42 | -5.3 | 24% / 4% / 72% |
| mech_v2 | opening_range_fade | #0 | 11 | -1.3 | -4.7 | -1.85 | -7.7 | 0% / 9% / 91% |
| mech_v2 | range_expansion | #0 | 454 | -0.9 | -3.7 | -2.39 | -3.4 | 19% / 4% / 77% |
| mech_v2 | trend_pullback | #0 | 512 | -0.5 | -3.3 | -2.59 | -2.4 | 27% / 3% / 70% |
| mech_v2 | volatility_breakout | #0 | 287 | +1.4 | -1.5 | -0.90 | -3.0 | 25% / 4% / 70% |
| mech_v2 | volume_breakout | #0 | 223 | +0.1 | -2.8 | -1.29 | -2.7 | 30% / 5% / 66% |
| mech_v2 | vwap_reversion | #0 | 0 | n/a | n/a | n/a | n/a | no trades (A-01) |
| mech_v2 | vwap_trend | #0 | 504 | +1.8 | -0.9 | -0.45 | -0.9 | 28% / 6% / 66% |
| refresh_v1 | cross_sectional_residual | #0 | 395 | +1.3 | -1.5 | -0.64 | -3.2 | 1% / 0% / 99% |
| refresh_v1 | mean_reversion | #0 | 526 | +2.0 | -0.7 | -0.63 | +1.2 | 0% / 65% / 35% |
| refresh_v1 | momentum_continuation | #0 | 477 | -1.8 | -4.6 | -3.03 | -4.4 | 1% / 0% / 99% |
| refresh_v1 | opening_drive | #0 | 157 | -2.4 | -5.3 | -2.37 | -6.0 | 0% / 0% / 100% |
| refresh_v1 | opening_range_breakout | #0 | 479 | -1.2 | -4.0 | -2.85 | -5.0 | 1% / 0% / 99% |
| refresh_v1 | opening_range_fade | #0 | 22 | +3.2 | -0.1 | -0.03 | +4.0 | 0% / 68% / 32% |
| refresh_v1 | range_expansion | #0 | 453 | -1.2 | -4.0 | -2.69 | -4.1 | 0% / 0% / 100% |
| refresh_v1 | trend_pullback | #0 | 484 | +1.3 | -1.5 | -1.18 | +0.4 | 0% / 0% / 100% |
| refresh_v1 | volatility_breakout | #0 | 24 | -3.9 | -7.0 | -1.26 | -7.9 | 0% / 0% / 100% |
| refresh_v1 | volume_breakout | #0 | 185 | +1.1 | -1.8 | -0.63 | -1.5 | 1% / 0% / 99% |
| refresh_v1 | vwap_reversion | #0 | 262 | +0.8 | -2.2 | -1.72 | -1.1 | 0% / 6% / 94% |
| refresh_v1 | vwap_trend | #0 | 511 | -0.8 | -3.6 | -2.13 | -5.4 | 1% / 0% / 99% |

### 6.6 The preregistered hypotheses on their sealed windows

`research.preregistered.evaluate` was run with `decision_eligible=False`. The decision field reads `diagnostic_only`, and look 1 (30 sessions) is not yet reached.

| hypothesis | sealed sessions | 60-minute delta (t, sign-flip p) | 120-minute delta (t) | mirror at 60 minutes (t) | in-sample claim |
| --- | --- | --- | --- | --- | --- |
| `vwap-reversion-control-adjusted.v1` | 16 | +3.50 bps (0.92, p 0.19) | +9.14 bps (1.70) | `vwap_trend` -4.43 bps (-1.23) | +9.44 bps, t 4.34 |
| `opening-range-fade-control-adjusted.v1` | 14 | +2.04 bps (0.36, p 0.38) | +0.84 bps (0.12) | ORB -4.16 bps (-1.57) | +19.5 bps, t 2.95 |

Both effects keep their sign out of sample, but at a third (VWAP) and a tenth (ORF) of their in-sample size: the classic decay of a selected result (I2-02). Neither is near significance.

The decision rule is also the wrong endpoint for trading. A 3 bps *control-adjusted* drift over 60 minutes is not P&L: as traded, with a bracket and realistic costs, the same VWAP-reversion subject loses 1.6 bps per trade (D1-03).

### 6.7 The one live lead, opening-range fade, examined closely

- **Baseline (`d5785d9e70b56def`).** 124 first trades: +8.8 bps gross, +5.8 bps net (t 1.39). The 14 unseen sessions give +4.4 bps net.
- **Portfolio view.** Under the shipped caps, the baseline made +1.16% over 22 sessions, a naive annualised Sharpe ratio of 4.5. This is what a short, lucky sample looks like.
- **Concentration.** 41% of net profit came from one day (16 September). Without it, the mean falls to +3.8 bps, and only 12 of 21 days were positive. The variant takes 55% of its profit from the same day.
- **Shape.** Shorts carried it (+10.5 bps, n 52), while longs barely paid (+2.4 bps, n 72). Results by symbol show no coherent liquidity or volatility pattern.
- **Structural version.** I built a professionally structured fade: 15-minute range, stop just beyond the failed-break extreme, target at the range midpoint, 60-minute time stop. It **loses** 2.6 bps net per trade, both on 22 one-minute sessions (t -1.9) and on 42 five-minute sessions (t -2.3). The repository arm's result therefore depends on its particular geometry (30 bps stop, 45 bps target, 20-minute range, 8 bps threshold). That is the signature of fitting, not of a mechanism.

Verdict: keep it as a sealed forward test, not as a use of capital.

### 6.8 Classic intraday hypotheses, tested independently over longer samples

| hypothesis | sample | result |
| --- | --- | --- |
| Market intraday momentum: the first half hour predicts the last half hour (Gao, Han, Li and Zhou 2018) | 487 sessions, Oct 2024 to Oct 2026 | SPY slope -0.003 (t -0.14). A sign strategy on the last 30 minutes earns -0.56 bps (t -0.58); pooled over 24 ETFs, +0.14 bps (t 0.62). Dead in this period, and the 15:50 flat rule would truncate it anyway. GLD alone shows +1.6 bps (t 2.17), 1 of 7 series tested |
| Fade large opening gaps (more than 2 standard deviations) | 16 years, daily | No robust effect: 6 of 11 years positive for SPY, and t between -2.1 and +1.7 across ETFs. GLD and SLV gaps *continue*. Recent fade gains come from a handful of crisis days shared by every ETF |
| Cross-sectional first-hour reversal: long the 4 losers, short the 4 winners, 10:30 to 15:30 | 487 sessions | +2.25 bps per day gross (t 0.72), before the costs of 8 legs. Nothing |
| Overnight versus intraday drift | 16 years | Overnight returns are positive and significant for most equity ETFs (SPY +3.6 bps per night, t 3.4). Open-to-close returns are smaller and mostly insignificant (SPY +2.2, t 1.8; IWM -0.3). A flat-overnight book gives up the one reliable premium in the data |
| Intraday mean reversion: variance ratio, excluding the first 15 minutes | 22 one-minute, 42 five-minute and 487 hourly sessions | VR(60 min) = 0.885 [0.796, 0.980]; on five-minute bars 0.830 [0.755, 0.897]; hourly over two years, VR(2 h) = 1.076 [0.954, 1.221]. Mild reversion exists **in the current regime only** |
| Professional VWAP reversion against its mirror: volatility-scaled threshold, VWAP target, 1.5x stop, 60-minute time stop | 22 and 42 sessions | Reversion +2.5 / +3.3 bps gross and -0.3 / +0.5 bps net (t about 0). The mirror (trend) loses 6.3 / 5.3 bps net (t -3.0 / -3.2) |
| Opening-range breakout on the first 5-minute bar, in the style of Zarattini, Barbon and Aziz (2024), without the stocks-in-play filter | 42 sessions | -5.1 bps net (t -2.45). The published edge depends on trading stocks in play (news and unusual volume), which broad ETFs are not |

### 6.9 What the research says

On this universe and in this regime, intraday ETF prices mean-revert mildly at 30 to 60 minutes once the opening quarter hour has passed. That is the only consistent structure. It is worth about 2 to 3 bps gross per trade, roughly the realistic round-trip cost, and it has not been stable over two years of hourly data.

- **Continuation families** (breakouts, momentum, VWAP trend, opening drive, IBR) sit on the wrong side of that structure. They lose money net of costs, often significantly.
- **Reversion families** sit on the right side, but the effect is too small to pay for execution.
- **The single positive arm**, opening-range fade, rests on one day and on its particular bracket geometry.

**Nothing in the catalogue, in its current form, has an edge that justifies risking capital.** The machinery around it, however careful, is measuring the absence of an edge with great precision.

## 7. Findings by severity

Each ID links to its evidence in the handoff named by its prefix:

- A, B, C1, C2, D1, D2, E1 and F1b: the agent handoffs;
- G1a and G1b: the live-shadow handoffs;
- all other prefixes: the coordinator handoffs.

### 7.1 Critical

**B-01: close and flatten order ids are reused across days.**

- **The defect.** Close ids have the form `edge-close-<symbol>-<reason>` and flatten ids `edge-flatten-<symbol>-<reason>`, with no trade or date component (`agent/execution_lifecycle.py:1051-1052`, `agent/runtime_control.py:197-199`, `agent/market_entry_risk.py:856-880`). The provider looks up an existing order with that id and returns it instead of submitting a new one (`agent/alpaca_provider.py:546-550`).
- **Same quantity as last time.** The second close of a symbol through the same exit path returns yesterday's filled order, and nothing is sent to the broker.
- **Different quantity.** `IdempotencyConflict` is raised on every cycle. On the force-flat path this escapes `flatten_all`, and the fallback ids collide in turn. Once the DAY bracket legs expire, the position stays open overnight.
- **Verification.** The reviewer reproduced the lookup with a fake client; the coordinator re-read the id construction and the provider lookup.
- **Fix.** Include the entry order id, the session date and the attempt counter in every close and flatten id.

**C1-01: flatten-driven closes are never booked.**

- **The defect.** The flatten path records its order as `action: "flatten"` and never sets `closing_order_id` (`agent/runtime_control.py:214-238`). Reconcile accepts only `action == "close"` or the trade's `closing_order_id` (`agent/execution_lifecycle.py:2288-2320`), so a flattened trade stays in `active_trades` for ever.
- **Paths affected.** The daily 15:50 force flat, fail-closed exits, run exit, startup cleanup and watchdog flattens.
- **Consequences:**
  - no journal close and no outcome is recorded, which censors the trial's evidence by exit path;
  - `resume()` deadlocks;
  - the next trade on the same symbol inherits the phantom's cancelled legs and is closed at once as `protection_missing`.
- **Verification.** The reviewer reproduced this with the repository's own `ProtectionHarness`; the coordinator re-read both code paths.
- **Fix.** Mark the trade as closing before a flatten is submitted, and add the missing test.

**G1a-01: a re-replay can add a second set of gate-eligible trade rows under the same key.** This is latent today, because no authorising candidate is in the shadow lane, but it would corrupt the first real confirmation.

- **The defect.** The replay key `replay_digest` hashes only the signal signatures (`research/live_shadow.py:6532`). The account row is written with `INSERT OR IGNORE` under that key, so the first P&L is kept. Each trade row, however, is keyed by its full content (`:3471-3498`), and `gate_rows` returns every row that matches the key (`:3683-3699`).
- **The trigger.** The replay memo lives only in process memory (`:3941-3945`), so a restart re-replays history. If anything that changes trade rows but not signals has also changed (cost settings, exit code, a new telemetry field), a second set of rows is added. Ingestion then counts those opportunities twice in the confirmatory gates, with P&L from two different models.
- **Verification.** The reviewer reproduced the double rows against a scratch store (`gate_rows count: 2 [60.0, 55.0]`, while the account still shows the first P&L). The coordinator re-read the key construction, the join and the restart path. Confidence: LIKELY, because whether the gate statistics de-duplicate by opportunity was not traced. D2-06 suggests they do not de-duplicate, and instead drop such symbol-sessions from pairing while still counting them.
- **Fix.** Key evidence by a digest of its full content, and treat a re-replay whose evidence differs as a mismatch.

### 7.2 High

| ID | area | finding | consequence |
| --- | --- | --- | --- |
| C1-02 | runtime | Flatten cancels a symbol's bracket legs before its close is accepted, and never re-arms them if the close fails (`agent/execution_lifecycle.py:1110`; order pinned by `tests/test_broker_protection.py:821-856`) | a naked position after any failed flatten |
| C2-01 | runtime | A paused or not-ready trader returns "hold" before the force-flat step, while its heartbeat stays fresh (`agent/engine_cycle.py:391-398` against `:446-449`; `agent/startup_edge_policy.py:429-442`) | neither the supervisor nor the watchdog intervenes, so positions can be carried past the close |
| B-03 | runtime | Any transient REST read failure, or a millisecond of negative clock skew, sends the cycle to `_fail_closed`, which flattens the book (`agent/engine_cycle.py:399-417`, `agent/market_entry_risk.py:502-531`) | infrastructure noise turns into realised losses and pauses |
| B-04 | runtime | One broker rejection or ambiguous submit on one entry re-raises and flattens every other position (`agent/engine_cycle.py:945-980`) | one bad order closes good trades |
| B-02 | runtime | Bracket stop and target stay anchored to the signal-bar close while the entry is re-priced to the live quote (`agent/market_entry_risk.py:597-605`) | the live reward-to-risk differs from research; asymmetric vetoes |
| B-05 | runtime and research | The runtime stress veto (9 bps at a 0.30 ratio) is set to exactly the 30 bps stop floor, and sits below the model's own expected cost (`config.yaml:52-53`; commit `2249cc5`) | the stop floor is an artefact of stress arithmetic, not of volatility |
| C1-03 | runtime | A running paper trial hashes every `.py` file under `agent/` and `research/` into its identity, so any deploy breaks Engine construction (trader, watchdog flatten, supervisor recovery) | a code deploy can disable the safety paths |
| C2-02 | data | Session acceptance has zero tolerance: one IEX minute without a sample rejects the whole session | consistent with zero accepted sessions (`docs/current-findings.md:3`); the trial cannot accrue evidence |
| A-01 | signals | `exit_before_minutes: 360` in the v2 and cost-cohort reversion arms means a 10:00 thesis deadline (`agent/contracts/rule.py:1103-1107`) | VWAP reversion can never trade (confirmed: 0 trades in the replay); ORF and mean reversion get about 9 minutes |
| A-02 | signals | ATR stop multiples are inert, because the 30 bps or 100 bps floor sets every stop (`rule.py:76-77`, `:2681`, `:1921-1931`) | brackets mean different things on each ETF; most trades are time exits |
| E1-01 | costs | The static cost model charges 17 bps per round trip (13 on top of quotes), against a realistic 1.5 to 4 bps (`research/costs.py:34-42`, `:677-704`) | research verdicts are dominated by the cost constants; the stop floors derive from them |
| D1-01 | statistics | The preregistration freezes the manifest text only. A control-tier decision rule was added in `09e43d9` (22:20:02Z), 53 minutes after the sealing commit `c674f2f` (21:27:48Z) and after the first sealed interim, and the measurement code has since changed by about 1,000 lines | the sealed tests are not the tests that were sealed |
| D1-08 | statistics | LORD++ with `gamma_k = 1/(k(k+1))` front-loads its wealth, so allocations collapse after a few tests (`research/factory_ledger.py:301-305`) | the live authorisation lane starves |
| D2-01 | gates | The 25 bps proof-time cost stress needs about 25 bps of gross edge per trade (`research/gates.py:153-154`, `:1540-1551`) | every realistic intraday edge fails |
| D2-02 | gates | Factory forward envelopes declare 100-trade floors, but the gates require 150 for the shadow lane (`research/gates.py:3050-3056`, `:799-811`; `research/strategy_factory.py:4040`, `:5173`) | the factory shadow lane can never pass |
| D2-03 | gates | Re-verification ignores the persisted iteration count, so envelopes tested with more than 20,000 draws fail | combined with G1c-03 (iterations rise as allocations shrink), the live-shadow lane stops |
| D2-04 | gates | The full gate chain needs at least 150 quote-priced sessions per lane and has near-zero power for a realistic edge | no edge can be authorised at plausible effect sizes |
| I2-02 | evidence | The in-sample effects that motivated the preregistrations decayed by 60 to 90% on fresh data | the paper incumbent was chosen on selection noise |
| G1a-02 | shadow | Every bar event for every authorising arm is stored as a decision, including "no signal" (`research/live_shadow.py:6660-6690`, `:7965-7975`). A global, never-pruned bound of 100,000 decisions applies (`:92`, `:2993-3026`), and the deployment never overrides it (`deploy/shadow.py:72`) | one arm hits the bound after about 11 sessions (24 symbols x 390 bars); after that, every poll raises and forward evidence stops |
| G1a-03 | shadow | The live virtual book keeps every open position, and its notional, for the whole session (`research/live_shadow.py:6665-6693`), while the replay releases capital at each exit (`research/factory_core.py:1523-1536`) | with a 25% notional cap and a 50% gross cap, the shadow refuses the third entry of a session but the replay admits it. The result is an "extra" signature difference and a quarantined session (`research/live_shadow.py:1102-1121`, `:6566-6577`). 94% of arms average three or more entries per session (median 18), so most sessions would be quarantined |

### 7.3 Medium

| ID | finding |
| --- | --- |
| A-03 | Reversion arms use fixed-R targets that overshoot the value anchor: the mean-reversion target sits 45 bps away inside a range of 12 bps or less |
| A-04 | Breakout predicates fire on every bar after the level is crossed. Research takes the first bar; the runtime takes the first bar it happens to see |
| A-05 | The ORF stop is anchored to the signal close, not above the failed-breakout high |
| A-06 | The `research/ibr.py` defaults describe a different strategy from the runtime IBR contract |
| A-07 | The IBR replay truncates the hold at the first missing minute and labels the exit `force_flat` |
| A-08, B-12 | Opening and VWAP families need a gap-free IEX session, so one missing minute disables the rest of the day. VWAP is built from IEX volume only |
| A-09 | `cross_sectional_residual` is a raw return difference, not a beta-adjusted residual |
| A-10 | Relative-volume baselines are not adjusted for time of day |
| A-11, I2-01 | The ORF rationale comes from an audit population that skipped the first 60 bars of each session; a third of the runtime's ORF population, and two thirds of its ORB population, fire before 10:30 |
| A-13, F2b-01 | The equity LLM grammar offers v6 and v7 schemas that can never execute. The only failing test in the suite is the guard that caught this |
| B-06 | The notional cap makes realised risk 0.075% of equity, not 0.5%. The gross cap makes three concurrent positions unreachable |
| B-07 | Every process start, including a restart in the middle of a position, flattens the whole book |
| B-08 | Unfilled entry limits never expire before the force flat, so stale or adversely selected fills can arrive hours later |
| B-09 | A single non-standard order in the last 500 permanently breaks reconciliation |
| B-10 | No HTTP timeout is set on any broker or data call, so a hung socket freezes the loop, including the force flat |
| B-11 | The runtime refuses `rule-strategy.v6` and v7, so the 100 bps cost-compatible cohorts can never be paper-traded |
| C1-04 | Trial evidence is not joined to what it qualifies: sessions come from shadow reports regardless of trader activity |
| C1-05, D1-07 | The trial verdict is anti-conservative: about 21% false passes across the sequential looks, and about 18% of zero-edge candidates become "promotable" |
| C1-06 | The run loop sleeps through SIGTERM for up to 60 seconds, while the supervisor kills after 15, so the shutdown flatten usually never runs |
| C2-03 | Acceptance for a single-strategy trial requires the whole 24- or 31-arm research cohort and its telemetry to be flawless |
| C2-04 | Watchdog and supervisor recovery hold the run lock across broker calls that have no timeout |
| D1-02 | The control-tier rule voids a whole look on a single fallback control and pre-empts the futility rule |
| D1-03 | The 3 bps "economic hurdle" is applied to a pre-cost, control-adjusted point estimate. It never binds, and it is not P&L |
| D1-04 | Looks count any date with one sealed bar as a session, and nothing records that a look was spent |
| D1-05 | Signal quality is anchored at the signal-bar IEX close, not at an executable price |
| D1-06 | Calibration cannot veto equity entry costs, because the journal reference is the limit price itself |
| D2-05 | The 20,000-draw Monte Carlo p-value floor makes a lone BY discovery impossible once the global batch reaches 175 candidates |
| D2-06 | The pairing key `vehicle:symbol:session` drops every trade in a symbol-session that has more than one entry |
| E1-02 | The runtime "stressed" cost (9 bps) is below the model's own expected round trip (13 to 17 bps); two of the four stress rungs are inert for equity |
| E1-03 | The same slippage is charged on limit legs that cannot slip, while non-fill risk is not modelled |
| E1-04 | Stressed-cost calibration cannot narrow below 9 bps |
| E1-05 | The measured-cost mode prices stop exits at half the IEX spread, with no trigger slippage |
| F1b-01 | A missing exit quote in strict mode turns an already-entered trade into a refusal. The sample is selected on post-entry data (verified by the coordinator) |
| F1b-02 | A missing minute during the hold ends the replayed trade early and removes it from authorising evidence, which also conditions on the post-entry path (verified by the coordinator) |
| F1b-03 | The replay assumes every entry fills instantly at the quote plus 6 bps; the runtime's DAY limit may not fill, or may fill hours later |
| F1b-04 | The stop floor overrides each family's scale, so low-volatility ETFs resolve almost entirely by 90-minute time exits |
| F1b-05 | Many mutation and discovery steps are behaviourally inert on one-minute data, and the semantic de-duplication keeps them |
| F1a-01 | `_run_factory` is a single 1,983-line function |
| F1a-03 | The earliest date any factory proof could pass is about mid-January 2027 (it needs 100 accepted IEX sessions) |
| F2a-03 | The research report's "confidence" is 1 minus the smallest p-value across all variants, with no multiplicity adjustment |
| F3a-01 | The randomised-entry null draws one entry per opportunity, which roughly doubles the variance of the comparison |
| F3b-02 | The drift SPRT never resets. A dead edge takes a median of 129 trades to retire with no history, and 429 after 300 good trades |
| G1c-01 | Shadow authorisation needs at least 60 consecutive complete sessions, and a single missing session halts it |
| G2a-05 | `trader_research.py` hard-codes the arm counts of one historical experiment (55, 67, 70, 31) |
| G2b-01 | The paper-epoch subsystem (2,527 lines plus tests) is not wired into anything |
| H1a-02 | The recorder tolerates IEX gaps but session acceptance does not: two components disagree about what a usable session is |
| H1a-04 | REST polling forces about 3,000 lines of bespoke state machinery that a WebSocket stream would make unnecessary |
| H1b-05 | There is no historical SIP research lane, although the free plan serves consolidated history back to 2016 |
| H2a-01 | A scheduler restart during the session starts a full four-hour research run next to the live trader |
| H2a-02 | 1,940 lines of bash orchestrate Python, with 24 embedded heredocs and 21 `set +e` toggles |
| H2b-03 | CI is red on one test and takes about 38 minutes |
| I1-01 | The documentation states three different stress numbers (9, 25 and 25 bps), sometimes within one document |
| I1-03 | More than 7,000 lines of operating documentation |
| G1a-04 | Shadow "parity" certifies signal geometry only. All gate P&L comes from the backtest simulator's fill model, so the shadow lane cannot detect B-02 or B-08 effects |
| G1a-05 | The replay diff, the evidence and the book closure are committed separately, with the "match" diff written first. A crash between them leaves a matched session with no evidence, which is never replayed again and blocks the candidate's tail |
| G1a-06 | Candidate intake silently keeps only the 32 oldest eligible candidates, including demoted ones. Newer candidates can never be shadowed, and nothing reports the truncation |

### 7.4 Low

These are recorded in the handoffs and not repeated in full here:

- **Signals:** A-12, A-14 to A-19.
- **Execution:** B-13 and B-15 to B-17 (B-14 is moot: the PDT rule was retired).
- **State and runtime:** C1-07 to C1-09 and C2-05 to C2-07.
- **Statistics and gates:** D1-09 to D1-13 and D2-07 to D2-09.
- **Costs:** E1-06 to E1-11.
- **Factory core:** F1b-06 to F1b-10.
- **Live shadow:** G1a-07 to G1a-09. G1a-08 independently confirms E2-01: availability falls back to the bar's start when stamps are missing.
- **Coordinator handoffs:** E2-01 (a latent 60-second look-ahead default for bars without timestamps), E2-05, F2a-02, F3a-03 to F3a-05, F3b-03 (the rolling warning fires for 95% of healthy edges), F3b-05, G1c-03, G2a-04, H1a-03, H2b-02, I1-02, I1-04, I1-05, I2-03 and I2-04.

### 7.5 What is done well

A fair review also records what should survive any rewrite:

- **Exit contract.** The exit contract and its 57 tests cover:
  - same-bar ties, resolved stop-first;
  - gaps;
  - entry-bar ranges;
  - deadline precedence;
  - breakeven and trailing rules that act from the next bar;
  - replay and runtime agreement on share counts.

  This is the strongest part of the code.
- **Market data timing is causal and honest.**
  - In-progress bars are never stored.
  - Each bar's `as_of` is its completed minute boundary.
  - `observed_at` is the real receipt time of each source.
  - Late rows are labelled as backfill.
  - Feeds are never mixed (H1a-01, H1b-01).
- **Selection is separated from confirmation.**
  - Variants are selected by BY-adjusted q-values on held-out data.
  - A sealed qualification window is opened only for the selected variant.
  - The online alpha is spent only on new live-shadow sessions (F1a-02, G1c-02).
- **LORD++ is implemented exactly as published** (Ramdas et al. 2017) and atomically (F2a-01). The drift SPRT is mathematically correct (F3b-01).
- **Diagnostic accounting is correct.**
  - Proper quote sides, adverse slippage and per-leg fees.
  - Three-way P&L reconciliation (G2a-01, G2a-02).
- **Containers are hardened.**
  - Non-root.
  - Read-only filesystem.
  - Capabilities dropped.
  - The dashboard is bound to localhost only and confines report paths (H2b-01).
- **Research outcomes are labelled honestly:** `completed_no_edge`, `search_exhausted` and `no_data` are kept distinct (H2a-03).
- **The September audit documents report their own numbers faithfully.** Every claim I checked matches the committed JSON (I2).

## 8. Wrong assumptions

| assumption in the code or documentation | reality | consequence | ref |
| --- | --- | --- | --- |
| A round trip costs 17 bps | about 0.35 bps (SPY) to 2.6 bps (XLRE) for one spread plus the SEC fee, before slippage; about 1.5 to 4 bps all-in | research verdicts and stop floors are driven by the cost constants | E1-01, E1-07 |
| Risk per trade is 0.5% of equity | 0.075% at a 30 bps stop; 0.25% at a 100 bps stop | the configured risk budget is fiction; the P&L scale is 7 times smaller than intended | B-06 |
| Stops are scaled by ATR | the 30 bps floor binds on essentially every trade | a stop means a different thing on every ETF | A-02 |
| `exit_before_minutes: 360` means six hours of thesis | it means 10:00 | two cohorts cannot trade | A-01 |
| Entries are market orders | they are DAY limits at the IEX quote; legs are anchored to the signal close; no expiry | fills differ from research and can arrive hours late | B-02, B-08, F1b-03 |
| IEX bars are a usable proxy for the market | IEX carries a few per cent of volume; missing minutes disable sessions; VWAP and relative volume use IEX volume only | sessions rejected; noisy features | A-08, B-12, C2-02 |
| The free plan offers no useful history | it serves consolidated (SIP) bars back to 2016; only the latest 15 minutes are restricted | the system waits months for its own recorder instead of using ten years of history | H1b-05 |
| A 60 to 70% win rate indicates an edge | the 60% win-rate arms earn -0.1 bps | optimising win rate optimises geometry, not edge | 6.3 |
| An in-sample t of 4.34 is strong evidence | after selection across 24 arms, 5 horizons and 18 sessions, the effect fell by 60 to 90% | the preregistered tests were chosen on noise | I2-02 |
| Preregistration seals the protocol | a decision rule was added 53 minutes after sealing; the instrument has changed by about 1,000 lines | the sealed test is not the one that was sealed | D1-01 |
| A 9 bps "stress" is conservative | it is below the model's own expected round trip | the cost-to-risk veto does not hold | E1-02 |
| Opening-range breakout is a documented edge | the published result applies to stocks in play, not broad ETFs | every breakout family loses on this universe | 6.8 |
| The PDT rule constrains intraday trading | FINRA retired it on 4 June 2026; Alpaca removed the fields | no action needed (B-14 moot) | coordinator fact 8 |

## 9. What is missing

**Runtime safety:**

- unique per-exposure close ids (B-01);
- booking of flatten closes (C1-01);
- re-arming protection after a failed close (C1-02);
- a force flat that runs whatever the pause state (C2-01);
- HTTP connect and read timeouts, and handling of 429 rate limits (B-10);
- a time to live on entry limits (B-08);
- re-adoption of live bracketed positions on restart (B-07);
- `easy_to_borrow` and shortability checks before short entries (B-14 remainder, F1b-07);
- an early-close-aware entry cut-off (C2-05);
- handling of halts and limit-up/limit-down pauses.

**Tests for the dangerous paths** (J):

- flatten booking;
- id reuse across days;
- a rejection that should not flatten the book;
- 429 responses and timeouts;
- easy-to-borrow checks;
- DST changes;
- halts;
- the paused-trader flat;
- an IEX session with one empty minute.

**Research realism:**

- a per-symbol, order-type-aware cost model (E1-01, E1-03);
- non-fill and queue modelling for limit legs (F1b-03);
- volatility-scaled stops (A-02);
- relative volume adjusted for time of day (A-10);
- a beta-adjusted residual (A-09);
- a day-type or regime filter, and an event calendar (FOMC, CPI, payrolls);
- one shared session-quality rule (H1a-02).

**Shadow evidence integrity:**

- replay evidence keyed by a digest of its full content (G1a-01);
- one atomic commit for the replay diff, the evidence and the book closure (G1a-05);
- a virtual book that releases capital at simulated exits, exactly as the replay does (G1a-03);
- persistence of signal-bearing decisions only, with a bound scoped to each session (G1a-02).

**Research design:**

- a power calculation before any hypothesis is opened (F2b-02);
- a historical SIP lane (H1b-05);
- a tradeable endpoint in the preregistrations (D1-03);
- change-point (CUSUM) drift monitoring (F3b-02);
- several random draws per null opportunity (F3a-01).

**Data:** a WebSocket stream instead of REST polling (H1a-04), and a paid SIP feed for live trading if the strategy depends on volume or VWAP.

## 10. Over-engineering, quantified

| item | size | evidence |
| --- | --- | --- |
| Research, deployment and tests compared with the runtime | 144,600 lines against 20,500 | section 5.1 |
| `_run_factory` | one function of 1,983 lines | F1a-01 |
| Paper-epoch subsystem | 2,527 lines, plus tests; nothing calls it | G2b-01 |
| Live shadow runner and ingestion | 8,349 + 1,473 lines to approximate runtime parity | G1, G1c |
| `deploy/research-cycle.sh` | 1,940 lines of bash, 24 Python heredocs, 21 `set +e` toggles | H2a-02 |
| Options execution stack | about 1,450 runtime lines that cannot run under the shipped configuration | B handoff |
| Rule grammar | seven schema versions (v1 to v7) and six cohort modules (`research/mechanism_*cohort*.py`, `diagnostic_cohort_contract.py`) | A, G2a-05 |
| Validation functions | `_gate_envelope_error` 519 lines; `null_control_account` 664; `_discover_gate` 333 | F3b-05, F3a-03 |
| Recorder | 3,028 + 531 lines, mostly to make overlapping REST polls idempotent | H1a-04 |
| Research cache and snapshot | 934 + 550 lines | H1b |
| Proof gates | about 12 of 31 required checks are implied by others or vacuous | D2-08 |
| FDR methods | three generations (legacy, v5, v6) live, with migration code | F2a |
| Documentation | more than 7,000 lines; one paragraph copied into six documents | I1-02, I1-03 |
| Tests | 62,000 lines; about 40% of assertions check receipts and provenance; the factory and edge shards take 32 and 38 minutes | J |

The pattern is consistent. Each time an experiment raised a question, the answer was encoded as a new code path (a cohort, a schema version, a gate, a ledger) instead of as data or configuration. The repository therefore records its own research history in code, and every past decision becomes a permanent maintenance cost.

## 11. What to do: three paths

The evidence points one way: the problem is not a missing gate, a missing variant or a missing cohort.

- The catalogue has no edge net of realistic costs.
- The runtime has defects that would lose money, or leave positions open, before any edge could show.
- The research process is built to be slow.

### Path A (recommended): make the trading core correct and small, then research properly

**Step 1. Make the runtime safe.** This is required before any further paper or live trading.

1. Make every close and flatten `client_order_id` unique per exposure (B-01). Book flatten closes (C1-01), and re-arm or keep protection until a close is accepted (C1-02). Add the missing tests.
2. Force flat at 15:50 whatever the pause or readiness state (C2-01).
3. Stop flattening the book on transient read errors and single rejections. Block new entries, keep the broker-resident brackets, and escalate only after N consecutive failures or near the close (B-03, B-04).
4. On restart during the session, re-adopt positions that still have live bracket legs instead of liquidating them (B-07).
5. Give entry limits a time to live of one or two bars, and set HTTP timeouts on every Alpaca call (B-08, B-10).
6. Re-anchor stop and target to the actual fill (B-02).
7. Bind the paper trial's identity to the strategy code it runs, not to the whole tree (C1-03).

**Step 2. Make the configuration honest.**

1. Decide the real risk per trade. Either raise `max_position_notional_pct` and the gross cap so that 0.5% is reachable, or set risk to what the caps allow (B-06).
2. Replace the 17 bps static cost model with a per-symbol schedule:
   - one tick or the measured spread;
   - slippage measured from your own paper fills;
   - the SEC fee on sells only.

   On these ETFs that is 1.5 to 4 bps per round trip.
3. Then delete the 30 bps and 100 bps stop floors, and express stops in units of each symbol's volatility over the holding period (A-02, B-05).
4. Validate thesis deadlines against the entry window, rename `exit_before_minutes`, and mark the v2 and cost-cohort reversion arms invalid (A-01).

**Step 3. Fix the data.**

1. Research on Alpaca's historical consolidated one-minute bars, available on the free plan back to 2016. That gives ten years of history, instead of waiting 30 to 100 live IEX sessions per question.
2. If a strategy depends on volume, VWAP or quotes, trade it on SIP in real time (the paid Algo Trader Plus plan) over the WebSocket stream.
3. Define one session-quality rule and use it everywhere (H1a-02).

**Step 4. Cut the system to its decision-making core.** The target is 10,000 to 15,000 lines:

- one event-driven engine shared by backtest, paper and live, so that parity holds by construction rather than through a shadow runner;
- one strategy interface and one risk module.

Archive the following (Git keeps the history):

- the LLM proposer;
- the options stack;
- six of the seven schema versions;
- the cohort lineage;
- the paper-epoch store;
- the parallel ledger, proof and epoch machinery.

**Step 5. Research the way a professional desk does.** Work on one hypothesis at a time:

1. Write down the mechanism and the expected effect size, and run a power calculation.
2. Test on ten years of SIP history, with walk-forward splits and session-clustered inference.
3. Use per-symbol costs and an honest fill model.
4. Only then run a short, sealed forward paper test with real fills.

The research found exactly one consistent structure: mild 30 to 60 minute mean reversion after the first quarter hour. Candidates worth that process:

- **Cost-aware reversion on the most liquid names only** (SPY, QQQ, IWM, DIA and VTI, with round trips of about 1 to 2 bps). Use volatility-scaled entry thresholds and a VWAP or mean target, and make the strategy conditional on day type, because reversion fails on trend days.
- **Day-type classification first.** Decide from the first 30 minutes whether a day is a drive or gap-and-go day or a range day, and trade reversion only on range days.
- **Event days.** FOMC announcements (14:00), CPI releases (08:30) and payrolls produce distinct intraday paths. Trade them, or switch off on them.
- **The close.** Market-on-close imbalances and leveraged-ETF rebalancing flows concentrate in the last 30 minutes. This needs the 15:50 flat rule to become a parameter.
- **Execution alpha.** Passive limit entries at the near touch on liquid ETFs earn part of the spread instead of paying it. Model fills honestly and measure them with real paper fills.

### Path B: keep the current framework, but stop it measuring noise

1. Fix the runtime defects in Step 1 above.
2. Retire the continuation families: they are consistently and significantly negative in sample, in the unseen window and in two years of related evidence.
3. Freeze the factory and the LLM proposer until the cost model and the data are fixed. Searching a grammar in which every member loses after costs only spends the multiple-testing budget.
4. Let the two preregistered tests run, but add a tradeable endpoint (net bracket P&L with per-symbol costs) next to the control-adjusted drift.
5. Repair the gate defects that make passing impossible (D2-01 to D2-04, D1-08, G1c-01).

### Path C: change the arena

The best-documented intraday edge available to retail traders is opening-range breakout on single stocks that are in play (news plus unusual relative volume), not on broad ETFs. Zarattini, Barbon and Aziz (2024) report it, and QuantConnect's replication confirms it with a lower Sharpe ratio. Broad ETFs are the least favourable universe for intraday breakouts, and the replay agrees: every breakout family loses on these 24 ETFs.

A pivot would need:

- SIP data;
- a news and relative-volume screener;
- borrow checks for shorts;
- a liquidity floor.

It is a new hypothesis, not a variant.

### What I would not do

- Do not go live, and do not resume paper trading, on the current runtime: B-01 and C1-01 alone can leave positions open or unprotected.
- Do not add more arms, cohorts, schema versions or gates.
- Do not chase win rate. The only number that matters is net expectancy per trade, stable across independent days, after real costs.

## 12. Sources

Market structure and costs:

- Alpaca, About Market Data API: plans, IEX against SIP, historical depth since 2016, the 15-minute restriction and WebSocket limits. https://docs.alpaca.markets/docs/about-market-data-api
- Alpaca, FINRA retires the PDT rule. https://alpaca.markets/blog/finra-retires-the-pdt-rule-introducing-alpacas-new-intraday-margin-framework/
- U.S. SEC, Fee Rate Advisory 2026-2 (Section 31 rate of $20.60 per million). https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2
- Corwin, S. and Schultz, P. (2012). A simple way to estimate bid-ask spreads from daily high and low prices. Journal of Finance 67(2). https://doi.org/10.1111/j.1540-6261.2012.01729.x

Intraday return predictability:

- Gao, L., Han, Y., Li, S. and Zhou, G. (2018). Market intraday momentum. Journal of Financial Economics 129(2). https://profiles.wustl.edu/en/publications/market-intraday-momentum/
- Zarattini, C., Barbon, A. and Aziz, A. (2024). A profitable day trading strategy for the U.S. equity market. https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/
- QuantConnect, Opening range breakout for stocks in play (replication). https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/
- Lo, A. and MacKinlay, A. C. (1988). Stock market prices do not follow random walks. Review of Financial Studies 1(1). https://doi.org/10.1093/rfs/1.1.41

Statistics and multiple testing:

- Benjamini, Y. and Yekutieli, D. (2001). The control of the false discovery rate in multiple testing under dependency. Annals of Statistics 29(4). https://doi.org/10.1214/aos/1013699998
- Ramdas, A., Yang, F., Wainwright, M. and Jordan, M. (2017). Online control of the false discovery rate with decaying memory (LORD++). https://arxiv.org/abs/1710.00499
- Harvey, C., Liu, Y. and Zhu, H. (2016). ... and the cross-section of expected returns. Review of Financial Studies 29(1). https://doi.org/10.1093/rfs/hhv059
- Bailey, D. and López de Prado, M. (2014). The deflated Sharpe ratio. Journal of Portfolio Management 40(5). https://doi.org/10.3905/jpm.2014.40.5.094
- Cameron, A. C. and Miller, D. (2015). A practitioner's guide to cluster-robust inference. Journal of Human Resources 50(2). https://doi.org/10.3368/jhr.50.2.317
- Wald, A. (1945). Sequential tests of statistical hypotheses. Annals of Mathematical Statistics 16(2). https://doi.org/10.1214/aoms/1177731118
- Page, E. S. (1954). Continuous inspection schemes. Biometrika 41(1/2). https://doi.org/10.1093/biomet/41.1-2.100
- Lorden, G. (1971). Procedures for reacting to a change in distribution. Annals of Mathematical Statistics 42(6). https://doi.org/10.1214/aoms/1177693055

## 13. Appendix: evidence index

| what | where |
| --- | --- |
| Shared review brief and coordinator facts | [`reviews/BRIEF.md`](reviews/BRIEF.md) |
| File-by-file coverage | [`reviews/COVERAGE.md`](reviews/COVERAGE.md) |
| Review handoffs (one per group) | [`reviews/`](reviews/) |
| Reviewers' verification scripts | [`reviews/verification/`](reviews/verification/) |
| Test inventory and handoff conversion tools | [`reviews/tools/`](reviews/tools/) |
| Fresh research: scripts, results and how to reproduce | [`research/README.md`](research/README.md) |
| Drift-monitor simulation (F3b) | [`research/scripts/sprt_sim.py`](research/scripts/sprt_sim.py) |
