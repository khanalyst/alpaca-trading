# Closing the strategy and research gaps, 28 October 2026

This change closes the strategy, cost, statistics and gate findings of the 15 October review ([`../review-2026-10-15/README.md`](../review-2026-10-15/README.md); handoffs A, E1, D1 and D2 in `../review-2026-10-15/reviews/`). It follows the 26 October performance refresh ([`../research-2026-10-26/README.md`](../research-2026-10-26/README.md)), which found no edge in any of the 82 registered arms.

**Rule for every fix: version it, do not rewrite it.** Registered arms, the sealed v1 hypotheses, stored evidence and the runtime under `config.yaml` must reproduce bit for bit. New behaviour is opt-in through:

- a new field (the `rule-strategy.v7` corrections);
- a new model (`per_symbol_v2` costs);
- a new protocol (gate protocol 2);
- or a new registration (preregistered v2, cohort `intraday-mechanisms.v3`).

Golden fingerprints taken on the base commit `352fb70` pin the rule:

| fingerprint | what it pins |
| --- | --- |
| `tests/research/test_rule_golden_equivalence.py` | 75 rule arms: 13,456 signals and 4,980 exits |
| `tests/research/test_ibr_golden_equivalence.py` | the 7 IBR arms and `config.yaml` |
| `tests/research/test_costs_versions.py` | the `static_v1` cost model |
| `tests/research/test_gate_protocol.py` | protocol 1, byte for byte against a stored fixture |
| `tests/research/test_preregistered_reproducibility.py` | the v1 evaluation reports |

The work was done in two rounds:

1. Five parallel fixes, one per area.
2. An adversarial review of the merged result. It found 21 defects in the new code, including two regressions in existing behaviour. All 21 are fixed below, except where a finding is listed as open.

## 1. Headline

- **The gaps are closed, and there is still no edge.** The corrected cohort `intraday-mechanisms.v3` applies every signal fix at once. Replayed on the same 30 public sessions with realistic costs, none of its 12 arms is positive net of costs, and 3 are significantly negative (section 4).
- **The fixes changed what is measured, not whether money is made.** The reversion arms that the A-01 thesis deadline had switched off now trade, and they lose. VWAP reversion goes from 0 trades to 562, at -2.91 bps per trade (t -3.31).
- **The preregistered v2 tests are published and sealed from 29 October.** They were hardened after review:
  - the instrument identity now covers 46 whole modules;
  - the manifest hashes are pinned and checked at run time;
  - no statistic is disclosed before a look is recorded;
  - look records must match their own digests.

  Look 1 needs 30 complete IEX sessions, which means mid-December at the earliest.
- **Some fixes are research-only.** All V7 corrections are non-executable at runtime by design (`rule_vehicle_executable` is False for v6 and v7), and nothing in the runtime path changed. Making a correction tradable needs a runtime-executable schema and a new registration.

## 2. Findings and their status

Key: **closed** means fixed and tested. **New version** means the fix is opt-in and existing behaviour is unchanged. **Partly** and **open** list what remains in section 6.

### Signals and IBR (handoff A)

| id | status | what changed |
| --- | --- | --- |
| A-01 | closed | Frozen v2 and cost arms with a 10:00 thesis deadline are marked invalid in an `errata` block outside the manifest hash. Corrected specs are rejected if the deadline falls at or before the first possible entry (the review fixed an off-by-one here). v3 uses 15:30. |
| A-02 | closed, new version | `stop_mode: horizon_atr`: the stop is the symbol's own ATR times sqrt(max hold), floored. |
| A-03 | closed, new version | v3 reversion arms target their value anchor (session VWAP or rolling mean). |
| A-04 | partly | `signal_event: first_cross` fires only when the predicate turns true. Research and runtime parity on refusals is unchanged. |
| A-05 | closed, new version | `stop_mode: structure` for opening-range fade: one ATR beyond the failed-breakout extreme. |
| A-06 | closed | The research harness's legacy defaults are named. The replay mapper takes omitted admission keys from the runtime contract (25 bps spread cap, relative volume 1.0, 30 s staleness), and the review added those values to the candidate identity. |
| A-07 | closed | A hold cut short by missing data is labelled `data_discontinuity` (a declared label change). `hold_across_gaps` is opt-in; the review fixed its label when the gap falls just before force-flat. |
| A-08 | closed, new version | `session_gap_mode: zero_volume`: a missing IEX minute counts as zero volume instead of voiding the rest of the session. The session must still start at 09:30, and replay checks adjacency only over the trailing causal window. After review, the factory, signal quality, null controls and fit diagnostics all honour it. `research/session_coverage.py` reports the loss per symbol. |
| A-09 | partly | `residual_mode: beta`: residual net of a causal prior-session OLS beta. Factory replay has no prior-session context, so the arm now reports `residual_beta_unavailable` instead of "rule not triggered". |
| A-10 | closed, opt-in | Same-clock relative volume, for the rule confirmation and for IBR. |
| A-11 | closed | `docs/preregistered-hypotheses.md` states the population mismatch (backfill rather than IEX, first 60 bars skipped). |
| A-12 | closed | Docs and errata state the 9 bps stress scenario (a 30 bps floor), the 1.5R trailing stop and the 10:00 deadlines. |
| A-13 | closed | The equity LLM grammar offers only executable schemas (commit `1708ff4`). |
| A-14 | closed, new version | Corrected trend-pullback specs reject the redundant trend confirmation; v3 uses a same-clock volume filter instead. |
| A-15 | closed, new version | `range_multiplier` for range expansion. |
| A-16 | partly | IBR signals carry both bar-open and bar-close timestamps; rule signals do not yet. |
| A-17 | closed | The three ATR definitions are named and pinned in tests, not merged (the rule family's simple mean gives different numbers). |
| A-18 | closed | Dead helper and contradictory branch removed. |
| A-19 | partly | `thesis_exit_deadline` takes the calendar close; callers do not pass it yet. |

### Costs (handoff E1)

| id | status | what changed |
| --- | --- | --- |
| E1-01 | closed, new version | `PerSymbolCostModel` (`per_symbol_v2`). Each round trip is one full spread (at least one tick), slippage by order type, and the SEC fee ($20.60 per million, from 4 April 2026) plus the FINRA trading activity fee, both on the sell leg only. Typical round trips are SPY 2.18, QQQ 2.50 and IWM 2.49 bps, against 17 under `static_v1`. It is the default for `cost_rerun`; the factory and gates still use `static_v1`. |
| E1-02 | partly | Stress rungs are stated as round trips and their coherence with the expected cost is reported. The runtime veto is unchanged. |
| E1-03 | closed for v2 | Limit legs do not slip; a non-fill probability is a parameter. |
| E1-04 | open by design | The calibration ladder cannot narrow below the 9 bps rung, and the artefact says so. |
| E1-05 | closed, opt-in | A stop-trigger charge in ticks on measured stop legs. The review made it opt-in (default 0), so `measured_quote_v1` is unchanged, and the gate now binds an opted-in leg. Before the fix, every measured envelope with a stop exit failed the binding check. |
| E1-06 | closed | Calibration ladders the round trip and fails closed on missing depth; `one_way_v1` reproduces old artefacts. |
| E1-07 | closed for v2 | Regulatory fees on the sell leg only. |
| E1-08 | closed | The stale 25 bps text is corrected. Two hash-bound labels stay and are documented. |
| E1-09 | partly | An under-covered measured *entry* cell refuses one opportunity. The review restored the stop for an under-covered *exit* cell: the exit cell depends on the trade's outcome, so refusing that one trade would bias the sample. IBR repricing still aborts. |
| E1-10 | partly | Aggregates use regular hours only, and a per-cell session floor is opt-in. `deploy/trader_projection.py` passes its legacy settings explicitly, so its artefacts reproduce. |
| E1-11 | closed | The IBR replay refuses an entry after force-flat on an early close, as the factory and live shadow already did. The review removed the first version, which clamped the policy instead, because the clamp relabelled refusals and broke `test_shadow_deadlines`. |

### Statistics and preregistration (handoff D1)

| id | status | what changed |
| --- | --- | --- |
| D1-01 | closed | v2 hypotheses on a fresh window, with a frozen instrument. After review, the identity covers all 46 modules the evaluator can import (imports inside function bodies and package `__init__`s included). The manifest hashes are pinned and checked at run time, and nothing computed on sealed sessions is disclosed before a look is recorded. v2 is evaluated from a checkout of the registration commit. |
| D1-02 | closed in v2 | An unmatched candidate counts against coverage. The futility text now matches the coverage gate. |
| D1-03 | closed | A pass needs four things: the delta's 97.5% lower bound above 17 bps, the after-cost sign flip, the candidate's own after-cost CR1 t above the critical value (added by the review), and coverage. |
| D1-04 | closed in v2 | Strict session definition; append-only, locked look records. After review, each record is checked against its own digest, and duplicates, a future `as_of`, symbols outside the universe and an interrupted append are all handled. |
| D1-05 | closed in v2 | The outcome is anchored at the next bar's open. |
| D1-06 | partly | Entry calibration is scored against the decision-time mid, but the runtime does not journal that mid yet. |
| D1-07 | partly | An opt-in v2 trial verdict that is decided once. Its policy is not yet frozen per epoch. |
| D1-08 | partly | A LORD++ v7 scope with the Javanmard and Montanari sequence. It is correct but stricter than v6 for the first 22 tests, so it is not active. |
| D1-09 | closed | The sign-flip test clusters by New York session by default. |
| D1-10 | closed | Proofs are written atomically. The review added a fallback for filesystems without hard links and repair of truncated artefacts. |
| D1-11 | closed | Proofs publish the q-value and lower bound by name. |
| D1-12 | closed, opt-in | A maximum calibration age. |
| D1-13 | closed in v2 | Wall-clock horizon. |

### Proof gates (handoff D2)

| id | status | what changed |
| --- | --- | --- |
| D2-01 | closed, new version | Protocol 2 stresses at twice the expected per-symbol round trip, on a bootstrap lower bound. After review, the expected round trip is floored at the code-owned `per_symbol_v2` figure, so neither a row nor a caller can declare it away. No producer supplies it yet, so the 25 bps fallback still applies. |
| D2-02 | closed | The forward lane takes its 150-trade protocol floor. |
| D2-03 | closed | Re-verification replays the persisted draw count and seed. |
| D2-04 | closed | Minimum detectable effects are published: 8.1 bps per trade at the shipped floors (40 bps SD, 5 trades per session). |
| D2-05 | closed, new version | A Hoeffding-bounded p-value when no Monte Carlo draw reaches the statistic. |
| D2-06 | partly | Ambiguous pairing keys fail a named check; producers do not stamp `comparison_id` yet. |
| D2-07 | closed, new version | Held-out drawdown capped at 10R. |
| D2-08 | closed | Every check is classified as independent, implied or diagnostic. |
| D2-09 | closed, new version | Passing envelopes must declare their protocol parameters. After review, the ledger stamps each new current-epoch run with a minimum gate protocol, so a protocol-1 envelope built today cannot prove it. |

## 3. The adversarial review

Three independent reviewers tried to refute the merged changes: signals and IBR, costs and gates, and statistics and preregistration. They confirmed what holds:

- no existing arm, sealed hypothesis or runtime path changed;
- the golden tests are not vacuous;
- `PerSymbolCostModel` matches `studies/liquid_reversion` exactly;
- the D2-05 p-value is valid;
- the CR1 and sign-flip statistics in v2 are correct;
- the new anchors and corrections are causal.

They found 21 defects in the new code. All are fixed in commits `c3e84ef` to `166b094`, and each fix has a test that fails on the code before it. The ones that mattered most:

| finding | why it mattered |
| --- | --- |
| the E1-05 stop-trigger charge broke the gate's cost binding | no measured-mode envelope with a stop exit could ever pass |
| the E1-11 clamp relabelled refusals | an existing test failed |
| the v2 instrument identity missed module-level statements, `__init__` files and lazily imported readers | an edited rule could still decide a look, and refused runs disclosed sealed results |
| a tampered look record was trusted | its outcome field was never checked against its digest |
| the D2-01 stress accepted a declared round trip of 1e-9 bps | the protocol-2 veto could be weaker than protocol 1's |

## 4. Did the corrections change performance? The v3 cohort

`intraday-mechanisms.v3` has one corrected arm per family; each is its v2 parent with the declared corrections. Both were replayed with the review's driver:

- 30 public one-minute sessions, 14 September to 23 October;
- runtime-faithful: first trade per symbol-session, next-bar entry;
- realistic costs: the larger of one tick and the Corwin-Schultz spread, plus 1.0 bps slippage and 0.28 bps fees.

v3 needs prior-session context for its beta and same-clock arms, which `tools/replay_v3.py` supplies point in time. The v2 rows are from the 26 October replay; the code is unchanged for those arms. All of this is diagnostic: the data are public bars that had already been examined.

| family | v2 trades | v2 net bps (t) | v3 trades | v3 net bps (t) | v3 fresh 8 sessions | main v3 exit |
| --- | --- | --- | --- | --- | --- | --- |
| opening-range breakout | 660 | -3.97 (-2.42) | 660 | -4.51 (-2.26) | -5.01 | time 91% |
| opening-range fade | 21 | -4.97 (-1.90) | 109 | -0.33 (-0.13) | -1.28 | target 58% |
| momentum continuation | 640 | -2.52 (-1.96) | 518 | -2.91 (-1.62) | -3.78 | time 89% |
| mean reversion | 220 | -2.03 (-2.51) | 720 | -1.43 (-1.05) | -4.23 | target 66% |
| trend pullback | 701 | -3.06 (-2.22) | 391 | -2.08 (-0.99) | +1.52 | time 86% |
| volatility breakout | 409 | -0.85 (-0.53) | 409 | -1.88 (-1.09) | -1.39 | time 89% |
| volume breakout | 338 | -1.52 (-0.81) | 415 | -1.87 (-0.95) | +0.51 | time 85% |
| VWAP reversion | 0 | (never traded) | 562 | -2.91 (-3.31) | -2.92 | time 82% |
| VWAP trend | 689 | -0.88 (-0.48) | 689 | -0.95 (-0.46) | -0.07 | time 93% |
| range expansion | 627 | -2.99 (-2.43) | 627 | -2.64 (-2.03) | -0.56 | time 87% |
| opening drive | 238 | -3.42 (-1.07) | 238 | -2.33 (-0.57) | +3.25 | time 90% |
| cross-sectional residual | 540 | -2.29 (-1.15) | 450 | -4.96 (-1.76) | -1.60 | time 92% |

What it shows:

- **No v3 arm is positive over 30 sessions.** Three are significantly negative, and the average arm loses 2.40 bps per trade (about 2.6 for the v2 parents). In the fresh window, 3 of 12 are positive and none is significant.
- **The A-01 fix matters for measurement, not for profit.** The reversion arms now trade, and they lose. Opening-range fade improves the most (-4.97 to -0.33 bps, now mostly target exits), but at t -0.13 that is no edge.
- **Horizon-scaled stops rarely bind.** Most trades end at their time limit, so these arms measure a 30 to 60 minute drift that is not there after costs.
- **`first_cross` changes no first trades.** The runtime-faithful view already takes the first trade per symbol-session, and `first_cross` only removes later re-signals.

Files: `results/compare_v3.json`, `results/runtime_faithful_v2_v3.json` and `.txt`, `results/replay_v3.log`, `results/v3_parents.json`.

## 5. How much data the old gap rule threw away (A-08)

On the 30 public sessions (consolidated tape, which has fewer gaps than IEX), 116 of 720 symbol-sessions have at least one missing minute. A strict gap-free-prefix rule therefore loses 7.6% of all observed minutes. The loss is concentrated:

| symbol | sessions with a gap | minutes lost |
| --- | --- | --- |
| VB | 30 of 30 (median first gap at minute 17) | 90% |
| XLRE | 18 of 30 | 24% |
| HYG | 7 of 30 | 12% |
| XLB | 8 of 30 | 11% |

The thinner IEX feed the deployment trades on will lose more. `research/session_coverage.prefix_gap_report` measures this on any corpus (`results/session_coverage.json`). The strict rule therefore biases replay evidence towards liquid, busy sessions. `session_gap_mode: zero_volume` is the opt-in remedy.

## 6. What remains open

Each item needs a new version or an owner decision, because fixing it in place would change existing evidence or runtime behaviour.

- **Runtime reach.** The V7 corrections are research-only. Making one tradable needs a runtime-executable schema, the runtime handling the new stop modes and the prior-session context it needs, and a new registration.
- **Wiring:**
  - `run_factory` cannot dispatch the v3 cohort;
  - factory replay passes no prior-session context, so the beta arm (A-09) and same-clock IBR in live shadow cannot fire;
  - producers do not yet supply `expected_round_trip_bps` (D2-01) or `comparison_id` (D2-06);
  - callers do not pass `session_close` (A-19);
  - rule signals do not carry both timestamps (A-16).
- **Costs.**
  - The factory and gates still default to `static_v1` (17 bps). Switching them changes every candidate identity, so it is a new epoch.
  - The runtime stress rung (9 bps) is below the `static_v1` expected round trip (E1-02).
  - IBR repricing still aborts on an under-covered cell (E1-09).
- **Statistics.**
  - LORD++ v7 does not unblock the first confirmatory tests; a bounded Bonferroni split would (D1-08).
  - Trial v2 reads its policy live and leaves `not_established` trials holding a paper slot (D1-07).
  - The runtime does not journal the decision-time mid (D1-06).
- **New findings, not fixed:**
  - `research/ibr.py` compares a raw stop distance with a cent-rounded stress floor, so an IBR replay session can be refused although its stop is far above the floor. Fixing it changes registered arm results.
  - A bar-priced diagnostic entry under a per-leg cost model fails the row-economics check in `factory_core._simulate_trade`.
  - `tests/test_research_snapshot.py::test_create_waits_for_the_recorder_lock` is timing-sensitive under load. It is pre-existing; a separate task is suggested.

## 7. Tests

Full disjoint shards (`deploy/test_suite.py`) on the final code, `4d82354`. Earlier shard runs, on `eb19721` before the review fixes:

- runtime: 1,036 tests, all pass except the timing-sensitive lock test above (it passes in isolation);
- edge: 61 tests, all pass;
- factory: 7 tests, all pass;
- research: 1,550 tests, 3 failures. All three are fixed in `c3e84ef` and `166b094`: the E1-11 shadow-deadline regression and the legacy-envelope fixture.

The run on `4d82354`, the code as pushed (the later commits change docs only), finished at 19:04 UTC on 28 October:

| shard | tests | result |
| --- | --- | --- |
| runtime | 1,036 | OK (1 skipped) |
| research | 1,570 | OK |
| edge | 62 | OK |
| factory | 7 | OK |

That is 2,675 tests with no failures.

## 8. Reproducing section 4

```sh
# MERGED is the merged public one-minute corpus from the 26 October refresh
git worktree add /tmp/v3tree <this commit>
export REVIEW_BARS_1M=MERGED/bars_1m.json REVIEW_FRESH_FROM=2026-10-14
python -I docs/research-2026-10-28/tools/replay_v3.py /tmp/v3tree run/replay_v3 2
# score directory: scripts -> /tmp/v3tree/docs/review-2026-10-15/research/scripts,
# data/bars_1m.json -> MERGED, results/replay -> run/replay_v3/*.json plus the
# 26 October replay files of the v2 parents (results/v3_parents.json)
python -I /tmp/v3tree/docs/review-2026-10-15/research/scripts/runtime_faithful.py
python -I docs/research-2026-10-28/tools/compare_v3.py results/runtime_faithful.json results/v3_parents.json results/compare_v3.json
```
