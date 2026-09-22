# Remediation and re-measurement: 22 September 2026

Applies steps 1, 3 and 4 of `FINDINGS.md`, re-measures the result, extends the
sample from 5 to 19 sessions, and collapses the two IBR admission paths into
one contract. Step 2 is blocked on data this session does not have; see below.

Measurement corpus: 177,504 one-minute bars, 24 configured ETFs, 19 sessions
(2026-08-25 to 2026-09-21).

---

## Step 1: stressed-cost scenario 25.0 → 9.0 bps

`config.yaml`, one value. `9.0` is already a member of the preregistered
`COST_STRESS_SCENARIOS_BPS = (9.0, 15.0, 25.0, 50.0)`.

This changes only admission geometry. The proof-time robustness requirement is
a **separate hardcoded constant**, `research/gates.py:154
COST_STRESS_REQUIRED_BPS = 25.0`, and is untouched: a candidate must still show
positive net P&L when charged a 25 bps cost shock. The conservatism that
matters is fully preserved; only the arithmetic that demanded an 83.33 bps stop
on a 3 bps instrument is corrected.

| | before | after |
| --- | --- | --- |
| required stop | 83.33 bps | 30.00 bps |
| signals admitted (43,193 real bars) | **0.000%** | **100.000%** |

Admission is a step function: the cliff sits where `scenario / ratio` falls to
the 30 bps grammar floor. Raising the ratio to 0.60, which
`research/cost_counterfactual.py` defaults to testing, admits 0.009% and would
have reported no material change.

## Step 3: IBR width band made horizon-invariant

`agent/contracts/ibr.py`. The band compared a range spanning `range_minutes`
against a **one-minute** ATR, so the quotient grew with the window and carried
a unit the authored `[0.25, 3.0]` band was never written in.

Scaling the one-minute volatility to the range horizon as a diffusion does
(`atr * sqrt(range_minutes)`) makes the quotient horizon-invariant and the
authored band meaningful without changing it:

| range_minutes | raw p5 / p50 / p95 | raw in band | normalised p5 / p50 / p95 | normalised in band |
| --- | --- | --- | --- | --- |
| 15 | 3.60 / 7.21 / 14.00 | 0.00% | 0.93 / 1.86 / 3.62 | **89.74%** |
| 30 | 5.19 / 8.57 / 15.94 | 0.00% | 0.95 / 1.57 / 2.91 | 97.41% |
| 45 | 6.57 / 12.04 / 23.01 | 0.00% | 0.98 / 1.79 / 3.43 | 92.17% |
| 60 | 7.51 / 13.28 / 24.61 | 0.00% | 0.97 / 1.71 / 3.18 | 91.30% |

The band now selects the intended tail: unusually wide or unusually narrow
openings relative to the session's own volatility.

## Step 4: the dead filters

**4a. Volatility confirmation** (`agent/contracts/rule.py::_confirmation`).
`compression_bps` is a *range width* bound, which is how the
`volatility_breakout` family gate reads it. The confirmation compared it with a
one-minute ATR instead, putting the field's two uses an order of magnitude
apart (measured prior-window width median 11.5 bps against ATR median
3.2 bps). It now measures the same quantity, so one field keeps one meaning.

**4b. Proximity floor** (`trend_pullback`). `max(threshold, .0005)` silently
overrode the authored threshold below 5 bps, which is exactly the range where
the parameter becomes meaningful: measured `|close - fastSMA| / fastSMA` has a
median of 2.4 bps. Replaced with `max(threshold, 1e-9)`, matching the idiom the
reversion families already use. Behaviour is unchanged for every existing spec,
since all authored values were at or above 5 bps.

**4c. Recalibrated roots** (`research/factory_core.py::FAMILY_TEMPLATES`,
`research/diagnostic_shadow.py::_ONE_FACTOR_VARIANTS`), from the measured
distributions:

| family | field | was | now | admitted before → after |
| --- | --- | --- | --- | --- |
| volatility_breakout | compression_bps | 55 → 65 | 8 → 12 | 96.8%/98.9% → 26.4%/52.9% |
| trend_pullback | threshold_bps | 15 → 20 | 3 → 5 | 97.1%/98.6% → ~55%/77% |
| mean_reversion | compression_bps | (45 default) | 12 explicit | 96.8% → 34.6% |

The frozen `intraday-mechanisms.v1` cohort carries its own hardcoded
specifications and is unaffected; its reclaim arms, which read
`threshold_bps` as an impulse magnitude rather than a proximity band, keep
their authored 15 bps.

### Identity churn

These are behaviour changes, so the affected arms take new content-addressed
ids. `DEFAULT_RULE_SPEC` was deliberately **not** touched, because
`rule_spec_hash` hashes the full normalised specification and a default change
would have churned all 24 ids including families that ignore the field. Only
`mean_reversion`, `trend_pullback` and `volatility_breakout` ids move.

No evidence is lost. Every affected arm had produced exactly zero trades, so
there is no measurement to carry forward.

---

## Result: the system trades

19 sessions, 24 ETFs, one trade per symbol-session, stressed-cost gate applied.

| | before | after |
| --- | --- | --- |
| signals admitted | 0.000% | 100.000% |
| **trades produced** | **0** | **5,833** |
| mean time-exit rate | 65.5% | 65.0% |
| mean target-hit rate | 10.9% | 8.0% |
| mean stop-hit rate | 23.6% | 27.0% |

**The exit geometry is still wrong.** Steps 1, 3 and 4 fix admission; they do
not touch the 30 bps stop or the 1.5-2R target, so the bracket is still far
wider than the path the trades take and two thirds still exit on time. Step 5
of `FINDINGS.md` (exit-side variant axes) is the outstanding work, and it is
now testable because there are trades to grade.

Economics, gross of costs:

- **1 of 22** arms beats the modelled 17 bps round trip.
- **6 of 22** beat a realistic 1.2 bps round trip.
- Nothing survives a Bonferroni correction over 22 arms (|t| > ~3.6 needed).

Strongest arms by session-clustered t (df=18; 2.10 nominal, 3.6 corrected):

| arm | clustered t | gross | trades | verdict |
| --- | --- | --- | --- | --- |
| `rule.vwap-reversion.cf8b38b12c9bf952` | +2.79 | +4.11 bps | 300 | nominal only |
| `rule.opening-range-fade.d5785d9e70b56def` | +2.77 | +13.55 bps | 65 | nominal only |
| `rule.opening-range-breakout.0eb200d3136d80ee` | -2.28 | -3.38 bps | 401 | nominal only |
| `rule.opening-range-fade.be61630d346f0923` | +2.16 | +21.07 bps | 31 | nominal only |
| `rule.vwap-reversion.ab97bfe87ed566de` | +2.15 | +3.92 bps | 380 | nominal only |

`opening_range_fade` is the only family whose target-hit rate rises sharply
(38.5% and 58.1% against ~5-8% elsewhere), which is what a working bracket
looks like. It is also the thinnest: 65 and 31 trades.

The harness does not supply `bars_by_symbol`, so the two
`cross_sectional_residual` arms produce no signals here. That is a limitation
of the measurement script, not of the code.

---

## Q2: the momentum/reversion split was a regime artefact

The five-session sample suggested momentum and breakout families carried edge
and VWAP reversion lost badly. Extending to 19 sessions reverses it.

| arm | 5-session mean | 19-session mean | clustered t (19) | sessions positive |
| --- | --- | --- | --- | --- |
| momentum_continuation variant | +9.84 | **+3.31** | **-0.50** | 7/19 |
| momentum_continuation baseline | +6.54 | +1.52 | -0.78 | 7/19 |
| volatility_breakout variant | +8.38 | +2.54 | +0.37 | 9/19 |
| volume_breakout baseline | +8.54 | +3.73 | +0.36 | 8/19 |
| **vwap_reversion baseline** | **-2.75** | **+0.24** | +1.08 | **14/19** |
| range_expansion | -2.03 | -1.31 | **-2.44** | **3/19** |

Every apparent winner shrank by 60-70% and none is significant. The whole
five-session picture was one trending day, 16 September, on which continuation
families gained 12-15 bps and reversion families lost the same amount.

Two results survive the larger sample:

- **`range_expansion` reliably loses.** 3 of 19 sessions positive, clustered
  t = -2.44, one-sided p = 0.0022, which survives a Bonferroni correction over
  12 arms. This is the only statistically defensible finding in the corpus,
  and it is negative.
- **`vwap_reversion` is the most consistent positive lead.** 14 of 19 sessions
  positive (one-sided p = 0.032, does *not* survive correction), small mean,
  and it is independently the strongest arm in the post-fix bracket replay
  above. Two different measurements agreeing on the same family is the most
  interesting signal in this work. It is not an edge yet.

19 sessions is the maximum one-minute depth available from the free source
used here, short of the 30-session cluster floor the repository's own gates
require. Nothing here authorizes anything.

---

## Q3: one IBR admission contract

`research.ibr.IBRConfig` and `agent.contracts.ibr.IBRConfig` shared 4 of 25
fields, only 3 of them substantive. `research/diagnostic_suite.py` already
named the gap as `IBR_UNMAPPED_FILTERS`, listing eight runtime filters the
replay never applied.

All eight are now mapped and applied:

- `research/ibr.py`: `IBRConfig` carries `min_relative_volume`,
  `min_ibr_width_atr`, `max_ibr_width_atr`, `max_ibr_width_pct`, `atr_period`,
  `max_entry_extension_r`, `stale_minutes` and `max_spread_bps`, with the
  *contract's own* permissive defaults so existing callers are unaffected.
- The breakout scan selects the first bar that both breaks the range and
  clears admission, matching how `evaluate_ibr_breakout` refuses a candidate
  and moves to the next one. Stopping at the first raw break would discard a
  later admissible breakout the runtime would have taken.
- Filter polarity mirrors the contract exactly. Relative volume and the width
  band reject on absence; staleness and spread reject only a *known* breach,
  because the contract stays silent when the observation is missing. Rejecting
  on absence would make replay stricter than the runtime and reopen the
  divergence from the other side.
- `IBR_UNMAPPED_FILTERS` is now empty and the diagnostic parity field reads
  `full` instead of `partial`.
- `tests/research/test_ibr_lane_parity.py` is the differential test that pins
  the two admission decisions to one another.

**Evidence to discard: effectively none.** The affected IBR evidence is one
preserved ledger candidate (`ibr.baseline`, `fd51e0b3031142c1a91cecc019bb52bf`,
status `candidate`) with its root control, and the `legacy_comparison` block
carrying the +$1.055386 / 42-trade result. Neither ever reached `validated` or
`champion`, and the runtime lane it was supposed to describe produced zero
signals. The correct disposition is to mark the legacy comparison
non-comparable and re-derive under the merged contract.

What is **not** merged: fill pricing, cost application and exit simulation stay
in `research.ibr`. That division is correct. The contract owns whether a signal
is admissible; research owns what happens to it afterwards.

---

## Step 2: not done, and why

Step 2 was to replace the cost assumption with a measurement. It is blocked on
data this session does not have, and the blocker is worth stating plainly
rather than working around.

`costs.measured_quote` needs a per-symbol, per-half-hour schedule fitted from
a recorded **quote** corpus with at least 500 quotes per cell, carrying exact
provider and feed provenance. This session has bars, not quotes. Fabricating a
schedule from bar data would put an unmeasured number into the one place the
repository is most careful about, which is the disease rather than the cure.

The measured evidence for how wrong the current constant is, is in
`FINDINGS.md` section 3: two independent estimators put the median quoted
spread near 1 bps against a 4.0 bps assumption, and the single global constant
is 30x too high for SPY while only 1.7x too high for XLU.

The operator command, against the recorder's own quote corpus:

```sh
.venv/bin/python -m research.cost_rerun \
  --calibration-only --corpus /absolute/path/to/frozen/quotes.jsonl \
  --config config.yaml --min-quotes-per-cell 500 \
  --publish-latest /absolute/path/to/calibration-latest.json
```

Note the ordering: step 1 alone unblocks the system. Step 2 changes how much an
edge earns, not whether it can trade.

---

## A consequence worth stating: the veto is now exactly non-binding

At `9.0 / 0.30` the stress-implied minimum stop is `30.0` bps, which is
exactly `MIN_STOP_DISTANCE_BPS`. The stressed-cost veto therefore never binds
at the shipped scalars: every authored stop already sits at or above the floor.

That is not an accident of this change. `9 / 0.30 = 30` is the grammar floor,
so the 9 bps member of the preregistered scenario ladder is the one value
consistent with the floor the grammar already enforces. The neighbouring
values are not: 15 bps implies a 50 bps stop and 25 bps implies 83.33, and both
admit 0.000% of real bars.

The veto still binds wherever it should, and this is why
`admission_preflight` keeps its incompatibility flag: a higher symbol-specific
scenario from an enabled calibration artifact, or an explicitly chosen 15/25/50
bps scenario, raises the requirement above the floor again and the veto
resumes. What has been removed is a global contradiction, not the control.

## Verification

`python -m compileall` clean across `agent`, `research`, `deploy` and the
entry-point modules. Full `deploy/test_suite.py` run recorded in the commit
message.

Ten tests encoded the previous behaviour and were updated, with their intent
preserved and the unit change documented in each. Six of them only surfaced in
the complete suite:

- `tests/research/test_cost_rerun.py`: the gate assertion now derives the
  scenario from the mounted config instead of restating the shipped scalar,
  and the tight-stop veto test names an explicitly binding scenario so it
  exercises the veto whatever the deployment selects.
- `tests/research/test_diagnostic_suite.py`: preflight arithmetic derived
  from config; the incompatibility flag is now asserted **False** at the
  shipped values and **True** under an explicit 25 bps scenario, which is the
  clearest statement of what this change did. The legacy-comparison parity
  field is asserted `full`.
- `tests/research/test_offline_shadow_worker.py`: its opening range was
  fifteen identical full-span bars, which makes ATR equal the range width and
  pins the normalised quotient at `1/sqrt(range_minutes)` at every price
  scale. The range minutes now walk across the band as real ones do.
- `deploy/paper-orb.config.json`: the shipped paper profile is `config.yaml`
  plus a `paper_trial` override, and a test enforces that. The scenario value
  is synced. This was a genuine find: the two files must not drift.

And four from the targeted run:

- `tests/test_strategy.py`: width band fixture, permissive floor for a test
  about causal `now` handling rather than width selection.
- `tests/research/test_rule_grammar_v2.py`: compression bound restated in the
  window-width unit; the calm/wild polarity assertion still discriminates.
- `tests/research/test_ibr_runtime_parity.py`: two band values retuned. These
  fixtures carry a unit range against a unit one-minute ATR, implying a 100 bps
  one-minute ATR; real ETF one-minute ATR is ~3 bps. They were calibrated
  against the defect.

No gate threshold, evidence floor, FDR method, risk limit or cost constant was
relaxed. `COST_STRESS_REQUIRED_BPS` and every trade/session/cluster floor are
unchanged.
