# Test Suite Audit — /home/user/alpaca-trading @ claude/trading-strategy-audit-zveg57
Domain: tests/ and tests/research/. Method: run things.
Status legend: CONFIRMED (I ran it / read it) vs SUSPECTED.

## 0. Inventory (CONFIRMED)
- 63 test files, 983 `def test_*`, 22,124 lines under `tests/` (vs 38,578 lines of `agent/ research/ deploy/`).
- Baseline core subset (test_risk, test_strategy, test_exit_contract, test_allocation, research/test_costs, research/test_gates, research/test_stats): **164 tests, 3.1s, OK**. CONFIRMED.

---

## 1. MUTATION TESTING (CONFIRMED — all mutants applied to source, run, then `git checkout --` restored)

Harness: `/tmp/.../scratchpad/mutate.py` + `mutants.json`. Each mutant patched into real source, the 164-test
core subset run with `-v`, failing test ids captured, source restored. Full run took 33s for 11 mutants.

| # | Mutant | Site | Tests killed / 164 |
|---|--------|------|--------------------|
| a | `evaluate_rule_signal` -> always `None` | agent/contracts/rule.py:604 | **41** |
| b | `evaluate_rule_signal` -> fixed long signal ignoring all inputs | agent/contracts/rule.py:604 | **32** |
| c | remove 30bps stop floor (`distance = atr*stop_atr`) | agent/contracts/rule.py:773-774 | **3** |
| c2 | remove stop floor in rule.py **and** strategy.py:220 **and** contracts/ibr.py:380 | 3 sites | **4** |
| d1 | invert spread limit (`spread > max_spread` -> `<`) | agent/risk.py:546 | **0** |
| d2 | max open-risk cap never binds (`>` -> `<`) | agent/risk.py:597 | **0** |
| d3 | daily-loss limit inverted (`<=` -> `>=`) | agent/risk.py:612 | **0** |
| d4 | remove the 25% notional cap from sizing (`min(raw, cap_shares, liq)` -> `min(raw, liq)`) | agent/risk.py:106 | **2** |
| d5 | `max_concurrent_positions` never binds | agent/risk.py:527 | **0** |
| e1 | `falsification_gate` -> `"passes": True` | research/gates.py:906-909 | **4** |
| e2 | `heldout_separation` -> `"passes": True` (fit/heldout leakage undetectable) | research/gates.py:881 | **0** |

### CRITICAL — five mutants survive the entire core subset untouched (0 kills)

**d1/d2/d3/d5 all live in `RiskProfile.vet_open` (agent/risk.py:509-630) — the single function that
decides whether an order is allowed.** I can break the spread limit, the aggregate open-risk cap, the
daily-loss circuit breaker and the concurrent-position cap simultaneously and `tests/test_risk.py`
(502 lines) still reports OK. These are the four limits that stand between the account and ruin.

**e2 is worse in kind:** `heldout_separation` is the fit/held-out leakage check — the thing that stops a
backtest from grading itself on its own training data. Hardcoding it to `passes: True` breaks nothing in
`tests/research/test_gates.py` (496 lines). A suite that cannot detect "in-sample and out-of-sample are the
same rows" is not testing a research protocol.

### HIGH — the stop-distance floor is barely covered (c/c2 = 3-4 kills)
Removing the 30bps floor everywhere kills exactly one test that is *about* the floor
(`tests/test_strategy.py` IBRContractTests.test_ibr_contract_and_runtime_plan_enforce_the_30bps_stop_floor)
plus 3 incidental exit-contract tests. Note the surviving-floor test at tests/test_strategy.py:72 and :91
computes its expectation as `signal["entry_price"] * MIN_STOP_DISTANCE_FRACTION` — importing the constant
**from the module under test** (tests/test_strategy.py:7). Changing `MIN_STOP_DISTANCE_BPS` from 30.0 to
any other number leaves that assertion green. See §4.

### MEDIUM — mutants a/b (41 and 32 kills) show signal-shape coverage is real
Interesting detail: nearly all the a/b kills are in `test_costs`/`test_exit_contract` — i.e. the tests that
detect a broken signal generator are *fill/exit accounting* tests that happened to consume a signal, not
tests asserting the signal is economically correct. Mutant b returns a hardcoded long at entry 100/stop 99
for every input and still passes 132/164.

### Why the risk mutants survive (CONFIRMED root cause)
`tests/test_risk.py:146-153` builds its `RiskEngine` from a **hand-written dict, not `validate_config`**:
```python
self.cfg = {"risk": {"risk_per_trade_pct": 1.0, "max_position_notional_pct": 50,
                     "max_concurrent_positions": 3, "options_min_dte": 7,
                     "options_max_dte": 45, "options_max_spread_pct": 10},
            "execution": {}}
```
It omits `max_open_risk_pct`, `max_gross_exposure_pct`, `daily_loss_limit_pct` and leaves `execution` empty.
In `vet_open` every one of those is `if X is not None and ...` — with the key absent the whole branch is
**dead code during the test**. Meanwhile production `validate_config({})` returns (CONFIRMED by running it):
`max_open_risk_pct=2.0, daily_loss_limit_pct=2.0, max_position_notional_pct=25.0, risk_per_trade_pct=0.5,
max_concurrent_positions=3, execution.max_spread_bps=100.0`.
**So the unit tests for the risk engine run under a config that no deployment ever uses, and the four caps
that actually ship are never entered.** `max_concurrent_positions` is set to 3 but every `vet_open` call in
the file passes `positions=[]` (tests/test_risk.py:145,151,190,339,422,430,436,452,472,477), so mutant d5
(cap = 1e9) is invisible too.

`tests/test_risk.py:29-71` — `test_risk_input_facade_preserves_identity_and_keeps_engine_methods_local` — is a
**pure refactoring-shape test**: it `ast.parse`s risk.py and asserts function names live in the right module
and that `risk_module.X is risk_inputs.X`. It asserts zero behaviour. It is the first test in the risk file.

---

## 2. NULL-HYPOTHESIS AND POWER TESTS — HEADLINE FINDING (CONFIRMED)

**There is no null-hypothesis test in this repository.** No test anywhere feeds zero-edge or random-walk
price data through the discovery/gate pipeline and asserts that nothing validates.

Evidence — I scanned all 63 test files for any stochastic price generation:
```
$ grep for (random.|Random(|gauss|normalvariate|np.random|choice(|uniform() across tests/
7 hits — all in tests/research/test_rule_grammar_v2.py
```
Those 7 hits are `tests/research/test_rule_grammar_v2.py:97-113`, a `random.Random(20260811)` sweep over
**rule-spec field values** (lookback, threshold_bps, target_r) asserting `validate_rule_spec` normalizes to
the v1 field set. It never touches a price. **Zero random or noisy market data exists in the suite.**

The closest thing to a negative control:
- `tests/research/test_gates.py:230-254 test_placebo_is_a_null_distribution_not_a_sign_reflection` — feeds a
  hand-authored mixed-sign `net_pnl` list to `falsification_gate` and asserts it fails. This is a unit test
  of one statistic over 10 literal numbers, not a null test of the pipeline.
- `tests/research/test_factory_end_to_end.py:226-243 test_the_pass_is_bought_and_worse_costs_take_it_away` —
  a negative control on **costs**, not on edge: same guaranteed-win corpus, worse `CostModel`, assert no pass.

**A power test (known edge DOES validate) exists**: `tests/research/test_factory_end_to_end.py` drives real
bars -> replay -> costs -> gates -> PASS. But see §3 — its "edge" carries no noise, so it is a power test
against an effect size of infinity, which no gate could fail.

The asymmetry is the whole problem. The suite proves the gates **accept** a deterministic edge. It never
once proves they **reject** a non-edge. That is exactly the failure mode a research-gate suite exists to
prevent, and this suite is blind to it. **CRITICAL.**

---

## 3. FIXTURE REALISM — the fixtures are engineered wins, not markets (CONFIRMED)

### 3a. `tests/research/test_factory_end_to_end.py` — 1200 copies of one trade
`_session_closes` (test_factory_end_to_end.py:57-88) builds a 33-bar session: 15 bars of `price -= .05`
steady decline, one +0.25 breakout bar on 6000 volume, a gap up to 101.70, then 15 bars of `price -= .10`
give-back. Per-session/per-symbol variation is `_wobble` (line 51-55), documented in its own docstring as
"never large enough to change which side of a level a bar closes on".

I ran it (CONFIRMED):
```
sessions x symbols: 1200
max deviation of ANY session's close path from the AAA/day-0 path: 0.04
distinct wobble values: [-0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03]
```
**1200 "independent observations" are 1200 copies of the same price path perturbed by at most 4 cents,
drawn from 7 possible values.** The volume series is literally identical across all 1200
(`[1000]*15 + [6000] + [3000,3000] + [1200]*15`).

Consequence: the sign-flip null (`falsification_gate`), the moving-block cluster bootstrap LCB, the
walk-forward fold-majority check, the effective-breadth and FDR checks are all computed over a sample with
essentially **zero variance and zero cross-sectional independence**. A per-cluster delta that is the same
positive constant 1200 times drives every one of those statistics to its extreme by construction. The gate
suite's flagship "PASS is earned" test therefore certifies nothing about the gates' discriminating power —
a gate implemented as `return mean(deltas) > 0` passes it identically. **CRITICAL.**

### 3b. `tests/research/test_edge_discovery.py:54-135 _sessions()` — the same, more explicitly
`values` is a hardcoded 24-bar OHLC list reused for **every** session; the only per-symbol variation is
`shift = index * .01` added uniformly to O/H/L/C, i.e. the four "symbols" (SPY, QQQ, IWM, DIA) are the
identical series translated by 0, 1, 2, 3 cents. Cross-symbol correlation is exactly 1.0. Quotes are
`bid = x - .01, ask = x + .01` — a **constant 2-cent spread on every quote in the fixture**, with
`quote_age_seconds` effectively zero. No spread widening, no crossed/locked books, no size, no partial
fills, no halts.

The docstring at line 55-62 is candid about the design: the give-back exists "so a randomly timed entry
would lose... which is the point of having the control at all." The fixture is reverse-engineered from the
gate it must satisfy. That is the textbook definition of a tautological test.

### 3c. `tests/research/test_gates.py:16-21 _rows()` — gate tests never see prices at all
```python
def _rows(count, *, net, start=2, symbol="SPY", prefix="candidate"):
    return [{"vehicle":"equity","symbol":symbol,"session_date":f"2024-01-{day:02d}",
             "opportunity_id":f"{prefix}-{day}","net_pnl": float(net(day) if callable(net) else net)}
            for day in range(start, start+count)]
```
Every statistical-gate test is driven by literal `net_pnl` scalars the test author chose (`net=10.0`,
`net=lambda day: float(day)`). One symbol, one trade per day, no bars, no fills, no costs. This is fine for
unit-testing a statistic, but it means **the gate module is never tested against data produced by the
replay engine it is supposed to judge** — the two halves of the research system are only ever joined in
test_factory_end_to_end.py, on the noiseless fixture of §3a.

---

## 4. TAUTOLOGIES AND OVER-MOCKING

### CRITICAL — the Alpaca SDK is never imported by any test (CONFIRMED)
`requirements.lock.txt:5` pins `alpaca-py==0.43.5`. **It is not installed in this environment and the whole
suite passes anyway** — `python3 -c "import importlib.util as u; print(u.find_spec('alpaca'))"` -> `None`,
164/164 core tests OK, and 21 other modules I ran all OK. `grep "from alpaca\.|^import alpaca" tests/` returns
nothing. `find tests -type f ! -name '*.py'` returns **nothing** — there is not one recorded Alpaca payload,
VCR cassette, or JSON fixture in the repository.

Every broker interaction is a hand-written fake that always behaves:
- `tests/test_alpaca_runtime.py:24-43 TradingFake` — `submit_order` returns `{"status": "accepted"}`
  unconditionally, for every request, forever. No rejection, no `pending_new`, no 429, no `APIError`,
  no partial fill, no wash-trade block, no PDT block, no `insufficient buying power`.
- `tests/test_runtime_safety.py:38 FakeProvider`, `tests/test_broker_protection.py:53 ProtectionProvider`,
  `tests/test_execution_lifecycle.py:31 LifecycleProvider`, `tests/test_backfill.py:31 FakeProvider`,
  `tests/test_cli.py:47 _SmokeProvider` — all return dicts/dataclasses the test authored.

Worse: the **actual SDK translation layer is untested**. `agent/alpaca_sdk.py:120 normalize_quote` and
`:136 normalize_bar` convert alpaca-py model objects into domain `Quote`/`Bar`. The only test reference to
them is `tests/test_alpaca_runtime.py:59-67`, which does
`self.assertIs(getattr(provider_module, name), getattr(sdk_module, name))` — an **alias-identity assertion**.
It proves the name is re-exported; it proves nothing about whether `bid_price`/`ask_price`/`timestamp`
field extraction matches what alpaca-py 0.43.5 actually returns. If Alpaca renamed a field tomorrow the
suite would stay green and the runtime would silently normalize every quote to `None`.

`tests/research/test_market_data.py`, `test_costs.py`, `test_ibr.py` etc. call `normalize_quote` — but that is
`research.market_data.normalize_quote`, the **research** normalizer fed hand-written dicts, not the SDK one.

### HIGH — assertions computed with the constant under test
- `tests/test_strategy.py:7` imports `MIN_STOP_DISTANCE_FRACTION` **from the module under test** and then
  asserts at `:72` and `:91` that the stop equals `signal["entry_price"] * MIN_STOP_DISTANCE_FRACTION`.
  The "30 bps floor" is never asserted as 30 bps. Change `MIN_STOP_DISTANCE_BPS` (agent/contracts/rule.py:55)
  to 1.0 or 300.0 and the assertion follows the change. Same pattern at `tests/test_exit_contract.py:21,52`.
- `tests/test_risk.py:29-71` — `ast.parse`s `agent/risk.py` and asserts which module each helper lives in and
  that `risk_module.X is risk_inputs.X`. Zero behaviour asserted; it is a lint rule wearing a test's clothes.
- `tests/test_alpaca_runtime.py:56-86` — same pattern for the provider/session/sdk split.
These "facade identity" tests inflate the test count and the line count while proving only that a refactor
did not move a symbol.

### MEDIUM — `_worker` patched out
`tests/research/test_factory_end_to_end.py:3-9` states it plainly in its own module docstring:
> "Every other `run_factory` test either feeds a fixture built to fail or patches `_worker` with synthetic
> evidence, so nothing exercised real bars -> real replay -> real costs -> gate evaluation -> PASS, and
> 'no previously-passing fixture now fails' was therefore vacuous."

That is the suite's own admission that its factory tests were tautological until one file was added. That
one file is the only real integration, and it runs on the noiseless fixture of §3a.

---

## 5. TESTS CERTIFYING UNREACHABLE BEHAVIOUR — the notional cap (CONFIRMED)

Shipped `config.yaml:38-48`: `risk_per_trade_pct: 0.5`, `max_position_notional_pct: 25.0`.
Shipped floor `agent/contracts/rule.py:55`: `MIN_STOP_DISTANCE_BPS = 30.0`.

I ran the shipped engine (`validate_config({})` + `RiskEngine.size_shares`, equity $100k):

| entry | stop distance | shares | delivered risk | % of equity | cap binds? |
|---|---|---|---|---|---|
| 100 | 30 bps (the floor) | 250 | $75 | **0.075%** | yes |
| 100 | 50 bps | 250 | $125 | 0.125% | yes |
| 100 | 100 bps | 250 | $250 | 0.250% | yes |
| 100 | 200 bps | 250 | $500 | 0.500% | yes (exactly at parity) |
| 100 | 201 bps | 248 | $498 | 0.499% | **no** |
| 500 | 30 bps | 50 | $75 | 0.075% | yes |
| 20 | 30 bps | 1250 | $75 | 0.075% | yes |

The 25% notional cap binds for **every stop tighter than 200 bps**, and the 30 bps floor guarantees the stop
is at least 30 bps — so in shipped configuration the delivered risk is 0.075%, **6.7x smaller than the
configured 0.5%**, on every trade a 30bps-floored rule signal can produce.

**No test covers the shipped regime.** The two tests that touch this boundary both deliberately step outside it:
- `tests/test_exit_contract.py:533-534` (comment, verbatim):
  `# 100k at .05% is a $50 budget, small enough that the risk term rather than`
  `# the 25%-of-cash notional cap decides the share count on both sides.`
  It then runs `risk_pct=.05` — **one tenth of the shipped 0.5%, chosen specifically so the cap does not bind** —
  and on that basis asserts `test_research_and_runtime_agree_on_the_share_count` (:549).
- `tests/research/test_costs.py:596-621 NotionalCapAnchorTests` goes the other way: `risk_usd=1e9` and
  `risk_pct=50.0`, "a budget large enough that the notional cap, not the risk term, binds."

So the suite tests the risk-bound regime with a fabricated 0.05% and the cap-bound regime with a fabricated
50%/$1e9, and **never once tests 0.5% with a 25% cap**. There is no assertion anywhere of the form
"delivered risk_usd ≈ risk_per_trade_pct × equity" under the shipped config. The parity test at
test_exit_contract.py:549 certifies research/runtime agreement in a regime the deployment never enters;
in the shipped regime both sides are pinned by the cap so the parity holds trivially while the position is
6.7x under-risked and nothing reports it. **HIGH.**

---

## 6. THE FOUR CONFIRMED DEFECTS — why no test caught them (CONFIRMED)

### 6a. `deploy/backfill.py:222` — `observed = now.isoformat()`
`tests/test_backfill.py` has 18 tests and a module docstring (lines 1-9) claiming it pins
"the point-in-time semantics that stop replay seeing a bar before it closed" and that backfilled data is
"indistinguishable from recorded data."

**`observed_at` appears exactly once in the whole file — line 194 — where it is passed straight into
`normalize_underlying_bar` as a shape check.** No test asserts a value for it.
`test_as_of_is_the_completed_bar_boundary` (:168) asserts `as_of == timestamp + 1min` and stops there.

I reproduced the defect using the suite's own fixture:
```
timestamp   2026-03-17T09:30:00-04:00
as_of       2026-03-17T09:31:00-04:00
observed_at 2026-03-20T23:00:00+00:00   <- the wall clock passed as `now`
all 9 rows share one identical observed_at
record_available_at() -> 2026-03-20 23:00:00+00:00   (3 days after the bar)
```
**The test that should have caught it is the one that exists**: `test_as_of_is_the_completed_bar_boundary`
checks two of the three point-in-time fields and skips the third — the one `record_available_at`
(research/market_data.py:203-226) takes the `max()` over. A one-line addition
(`assertEqual(observed_at, as_of)`) inside the existing loop would have failed. Nothing in
`test_research_normalization_accepts_every_backfilled_row` (:183) helps either: it calls the normalizer for
shape and never calls `record_available_at`, so the visibility rule is never applied to a backfilled row.
**No test anywhere feeds a backfilled corpus into replay.** MISSING TEST: round-trip
backfill -> `record_available_at` -> replay visibility.

### 6b. `research/live_shadow.py:1665-1682` — the paired control never gets decision rows
The only test that carries a candidate to `validated` is
`tests/research/test_live_shadow_ingest.py:134 test_matched_tail_appends_shadow_proof_and_transitions`.
It **hand-writes the producer's output**:
```python
self.store.replay_diff(candidate_id=cid, session_date=session,
                       source_digest=..., shadow_digest=..., replay_digest=replay,
                       status=status,
                       details={"complete": status == "match",
                                "signature_match": status == "match"})   # line 99-100
```
`complete` and `signature_match` — the exact two fields the real `ShadowRunner._replay` path cannot set to
True because the control never receives decision rows — are **literals the test writes itself**. It then
asserts the *consumer* transitions to `validated`. The producer is never run. The producer/consumer seam,
which is where the defect lives, is not joined by any test in the repository.

Compounding it, `tests/research/test_live_shadow_ingest.py:25-48` monkeypatches every promotion floor to 1:
```python
patch.object(edge_discovery_core, "MIN_PROMOTION_CLUSTERS", 1)
patch.multiple(gates, PROTOCOL_BACKTEST_MIN_TRADES=1, ..._MIN_SESSIONS=1, ..._MIN_CLUSTERS=1,
               PROTOCOL_SHADOW_MIN_* = 1, PROTOCOL_QUALIFICATION_MIN_* = 1)
```
research/gates.py:66 calls these "Immutable authorizing evidence floors." The test comment concedes
"production code and the CLI never expose this patch seam" — i.e. the test knowingly runs a protocol that
cannot exist in production, on evidence it fabricated. **CRITICAL.**

### 6c. `research/edge_ledger.py:233-236` — forgeable authorization marker
See §"gaps" below: no test attempts to append run metrics or a trade payload carrying a fabricated
authorization marker. The tamper tests that do exist
(`tests/research/test_live_shadow_ingest.py:163 test_same_tail_or_tampered_window_digest_cannot_authorize`,
`tests/research/test_gates.py test_non_passing_source_tampering_fails_after_resigning`) tamper with
*digests and session windows*, i.e. the fields that ARE validated. **No adversarial test targets the
un-validated caller-supplied payload path.** The suite's threat model is "a field was corrupted", never
"the caller lied". SUSPECTED-to-CONFIRMED: I found no `append_evidence` test supplying a forged marker.

### 6d. The 25% notional cap — see §5. No test runs the shipped config.

### Common shape
All four defects live at a **seam between two components**, and in every case the suite tests each side of
the seam against a fixture the test authored rather than against the other side's real output. That is the
structural reason the 22k lines caught none of them.

---

## 7. END-TO-END COVERAGE (CONFIRMED)

**There is no test from recorded bars -> proved edge -> submitted order.** The chain breaks in three places
and each break is papered over by a fabricated fixture:

| Stage | Best test | What is faked |
|---|---|---|
| recorder/backfill -> corpus | tests/test_backfill.py | provider is `FakeProvider` (:31); corpus never replayed |
| corpus -> gate PASS | tests/research/test_factory_end_to_end.py | bars are a 33-bar hand-authored win repeated 1200x (§3a) |
| gate PASS -> validated | tests/research/test_live_shadow_ingest.py:134 | `signature_match`/`complete` hand-written; all 10 protocol floors patched to 1 |
| validated -> order submitted | tests/test_runtime_safety.py, test_broker_protection.py | broker is a fake that always accepts; SDK not installed |

`tests/research/test_factory_end_to_end.py:207 test_the_lifecycle_reaches_validated_and_a_champion` is the
one test that tries to span backtest->shadow->champion. Read its assertions: it asserts
`status == "shadow"` and **`self.assertIsNone(result["champion"])`** — i.e. it asserts that the pipeline
*stops* before producing a champion, on the stated grounds that "Only live-shadow ingestion can authorize
validation." Given defect 6b, live-shadow ingestion can never authorize anything in reality. **So the
end-to-end test asserts the exact non-outcome that the production bug guarantees, and is green.** The test
name promises a champion; the test body asserts there is none.

Every layer is tested only in isolation, against inputs its neighbour cannot actually produce.

---

## 8. WHY `test_drift_is_inapplicable_without_a_risk_normalized_reference` TAKES 58s (CONFIRMED)

The test body is 8 lines (tests/research/test_edge_discovery.py:1309-1316) and makes three assertions:
`heldout_reference is None`, `drift["applicable"] is False`, `status == "validated"`.

I profiled the setup. `self._deployed_candidate(ledger)` (:1220-1231) calls `_persist_gate` twice:
```
persist_gate backtest: 11.8 s
persist_gate shadow:    9.8 s
26.3 s total, 55.5M function calls
  gates.verify_gate_envelope          10.6 s   (re-runs the whole analysis a 3rd time)
  matched_cluster_test                 8.9 s
  moving_block_cluster_bootstrap_lcb   7.5 s   (26 calls)
  paired_cluster_sign_flip             6.5 s   (stats.py:75 genexpr evaluated 13,020,000 times)
  edge_ledger.append_trade             8.8 s   (450 calls)
  sqlite3.Connection.__exit__          5.3 s   (458 commits — one transaction per trade row)
```
The remaining ~30s is `_ingest` running 20 sequential `ingest_paper_outcome` calls, each re-deriving drift.

**Yes, the slowness means it is testing the wrong thing — three ways:**

1. **The 55 seconds buy no statistical content.** `_persist_gate` (:138-215) default-builds 150 held-out
   "trades" whose `net_pnl` is `1.0` for **every single one** (`score: float = 1.0`), across five symbols
   (SPY/QQQ/IWM/DIA/AAPL) that carry identical numbers, against a baseline that is `0.0` for every row
   (`_gate_evidence`, :33-51: "The zero baseline makes the matched deltas equal to the held-out P&L").
   So 10,000+ bootstrap draws and 13 million sign-flip evaluations are performed on a sample with **zero
   variance**. The bootstrap cannot produce anything but the same answer. The compute is pure ceremony.

2. **The expense is in minting an authorized candidate, not in the thing under test.** There is no cheap
   builder for "a validated candidate" anywhere in the suite, so every guard test pays ~22s of real gate
   machinery to reach the state it wants to test. Five sibling tests (:1243, :1275, :1289, :1300, :1309) each
   pay it — roughly 4-5 minutes of CPU to exercise five boolean branches of the drift guard. That cost is
   why the drift guard has five tests and not fifty.

3. **It certifies the plumbing, not the statistic.** What the 58s actually proves is that
   `verify_gate_envelope` can recompute an envelope it just wrote. That is a serialization round-trip
   dressed as a statistical proof — the same tautology as §3c, but expensive.

---

## 9. CI (.github/workflows/ci.yml) — CONFIRMED

Single workflow, single job, `timeout-minutes: 45`, on every push and PR:
```
python -m compileall -q agent research deploy main.py report.py research.py
python -m unittest discover -v          # no subset, no -k, no per-test timeout
cp .env.example .env && docker compose config/build
rg -i '\b(ccxt|usdt|funding|perpetual)\b' ... # fails the build on crypto vocabulary
```

**Good:** discovery is complete — I verified `unittest.TestLoader().discover('.')` collects **983 tests
with zero load errors**, the same count as `discover('tests')`. No test is silently excluded by selection,
and `alpaca-py==0.43.5` is installed in CI from requirements.lock.txt (it is not installed locally, which is
how I proved no test imports it).

**What CI does not gate — MEDIUM/HIGH:**
- **No coverage measurement and no coverage threshold.** Nothing detects the untested `vet_open` limit
  branches (§1) or `agent/alpaca_sdk.normalize_*` (§4).
- **No mutation testing, no assertion-density check.** The five surviving mutants in §1 would pass CI today.
- **No linter and no type checker.** `compileall` is a syntax check, not analysis. No ruff/flake8/mypy step.
- **No per-test or per-module timeout**, so one slow test degrades silently rather than failing.
- **No flake detection / no re-runs / no fixed `PYTHONHASHSEED`.**
- The only non-test quality gate in the entire pipeline is a **ripgrep for the words ccxt/usdt/funding/
  perpetual**. A repository that gates on vocabulary and not on coverage has its priorities inverted.

**Wall-clock risk — SUSPECTED:** the 45-minute job budget must cover pip install + compileall + the full
983-test suite (measured elsewhere at >900s and not finishing) + `docker compose build`. The suite has no
internal timeout, so if it grows past the remaining budget the job dies at 45 minutes with a
cancellation, not a test failure — and cancellations are easy to re-run away. I could not measure the full
suite end-to-end within this audit's budget, so I flag it rather than assert it.

**Tests that effectively never run in CI:** none by selection — but note that
`tests/research/test_edge_discovery.py` (1,906 lines) at >150s and `tests/research/test_strategy_factory.py`
(1,014 lines) at ~40s mean roughly a third of the wall clock is spent on the bootstrap ceremony of §8. The
practical consequence is that developers do not run the full suite locally; the 164-test 3s subset is what
gets run, and that subset is exactly the one where five of my eleven mutants survive.

---

## 10. THE FORGEABLE AUTHORIZATION MARKER — the tests depend on the hole (CONFIRMED, CRITICAL)

`research/edge_ledger.py:233-236`:
```python
def append_evidence(self, candidate_id, kind, payload, *, run_id=None):
    if str(kind) == "verified_gate":
        raise ValueError("verified_gate evidence must be recorded through record_verified_gate")
```
That is the entire validation. Any other `kind` — including `"shadow_ingestion"`, the marker deployment
authorization keys on — is stored verbatim. Likewise `append_run(metrics=...)` and `append_trade(trade)`
persist caller dicts with only numeric sanity checks (edge_ledger.py:187-231).

`research/edge_ledger_proof.py:655-700 _live_shadow_authorized` — the gate on live deployment — reads
`run["metrics"]["shadow_source"]` and checks it against **digests computed from that same caller-supplied
mapping**. Nothing binds it to an actual ShadowRunner execution.

**The suite cannot detect this because the suite's own fixtures exploit it.** Both promotion fixtures forge
the marker by hand:
- `tests/research/test_strategy_factory.py:280-345` builds a complete `shadow_source` mapping — schema,
  selection/confirmatory session lists, `session_digest`, `rows_digest`, `p_value_source:
  "live_shadow_confirmatory_gate"`, a fabricated BH/FDR block with `"significant": True` — passes it
  straight into `append_run(metrics=...)`, then reads it back out and re-posts it via
  `append_evidence(candidate_id, "shadow_ingestion", ...)` at :340.
- `tests/research/test_edge_discovery.py:428-436` does the same.

So the canonical way the test suite produces a promotable candidate **is** the forgery. A test that
detected forged authorization would fail every other test in those files. There is no adversarial test
anywhere that supplies a fabricated `shadow_source` or `shadow_ingestion` payload and asserts refusal —
the existing tamper tests (`test_same_tail_or_tampered_window_digest_cannot_authorize`,
test_live_shadow_ingest.py:163) only corrupt digests that ARE cross-checked, i.e. they test the checks that
exist and never probe for checks that are missing. The threat model is "a field got corrupted", never
"the caller lied".

---

## 11. SUMMARY OF RANKED FINDINGS

**CRITICAL**
1. No null-hypothesis test exists. Zero random/noisy market data in 22k lines (§2). The gates are never
   shown to reject a non-edge.
2. Five mutants survive the 164-test core subset with 0 kills — four of them are the shipped risk limits
   in `RiskEngine.vet_open` (agent/risk.py:527,546,597,612), one is the fit/held-out leakage check
   (research/gates.py:881) (§1).
3. `tests/test_risk.py:18-24` runs the risk engine under a config `validate_config` never produces, so the
   shipped caps are dead code in test (§1).
4. Promotion authorization is forged by the test fixtures themselves (§10), making the
   `edge_ledger.py:233` hole structurally untestable.
5. `tests/research/test_live_shadow_ingest.py:25-48` patches all ten "immutable" protocol floors to 1 and
   hand-writes `signature_match: True` — the exact field the real producer cannot set (§6b).

**HIGH**
6. Fixtures are engineered wins: 1200 near-identical sessions (max 4c deviation, 7 distinct values) in
   test_factory_end_to_end.py; one hardcoded 24-bar path across 4 translated "symbols" with a constant
   2-cent spread in test_edge_discovery.py `_sessions` (§3).
7. Nothing tests the shipped 0.5%/25% risk regime; the two tests near it deliberately step outside the cap
   in opposite directions (§5).
8. The Alpaca SDK is never imported; `agent/alpaca_sdk.normalize_quote/normalize_bar` is covered only by an
   alias-identity assertion; zero recorded broker payloads exist in the repo (§4).
9. The stop-distance floor is asserted against the constant imported from the module under test (§4).

**MEDIUM**
10. `_persist_gate`'s 22s of bootstrap runs on a zero-variance sample; it certifies serialization, not
    statistics, and its cost suppresses test density in the lifecycle guards (§8).
11. CI gates on tests and a crypto-vocabulary grep only: no coverage, no lint, no types, no mutation,
    no per-test timeout (§9).
12. Structural "facade identity" tests (test_risk.py:29, test_alpaca_runtime.py:56, test_edge_discovery.py:440)
    inflate the count while asserting no behaviour.

**LOW**
13. `test_the_lifecycle_reaches_validated_and_a_champion` (test_factory_end_to_end.py:207) asserts
    `champion is None` — the test name promises the opposite of what the body checks (§7).

**Verdict:** the suite is large, carefully written, and honest in its comments — several docstrings state
outright what is not covered. But its 983 tests prove that the code is *internally consistent with fixtures
the same authors wrote*. On the two questions a trading system's tests must answer — "does the risk engine
enforce its shipped limits?" and "do the research gates reject a non-edge?" — it currently answers neither.

---

## 12. THE PROTOCOL FLOORS ARE PATCHED AWAY IN 8 FILES (CONFIRMED, HIGH)

`research/gates.py:66-80` states the design intent verbatim:
> "Immutable authorizing evidence floors ... Keeping the protocol separate means **a compact diagnostic test
> cannot lower an authorizing proof by monkeypatching a helper default** or by forging the `minimums` object
> persisted in an envelope."
```
PROTOCOL_BACKTEST_MIN_TRADES = 100   PROTOCOL_SHADOW_MIN_TRADES = 150   PROTOCOL_QUALIFICATION_MIN_TRADES = 100
PROTOCOL_BACKTEST_MIN_SESSIONS = 30  PROTOCOL_SHADOW_MIN_SESSIONS = 30  PROTOCOL_QUALIFICATION_MIN_SESSIONS = 30
PROTOCOL_BACKTEST_MIN_CLUSTERS = 30  PROTOCOL_SHADOW_MIN_CLUSTERS = 30  PROTOCOL_QUALIFICATION_MIN_CLUSTERS = 30
```
Counted across `tests/`:
```
10 x PROTOCOL_BACKTEST_MIN_TRADES=1      10 x PROTOCOL_SHADOW_MIN_TRADES=1     10 x PROTOCOL_QUALIFICATION_MIN_TRADES=1
10 x ..._SESSIONS=1                       10 x ..._SESSIONS=1                    10 x ..._SESSIONS=1
10 x ..._CLUSTERS=1                       10 x ..._CLUSTERS=1                    10 x ..._CLUSTERS=1
 2 x each of the same nine set to 4
```
in 8 files: test_edge_discovery.py, test_factory_report.py, test_factory_worker_view.py,
test_live_shadow_ingest.py, test_llm_tuning.py, test_market_data_lane_boundaries.py,
test_quote_index_descriptor.py, test_strategy_factory.py.

The mechanism designed specifically to stop a test from lowering an authorizing proof is **defeated by
`patch.multiple` in ten places**, because `PROTOCOL_*` are plain module globals and enforcement reads them
by name at call time. Every lifecycle/promotion test therefore runs a 1-trade/1-session/1-cluster protocol.
The 100/150/30 floors that actually ship are exercised in exactly one place — the noiseless 1200-trade
fixture of `test_factory_end_to_end.py` (§3a).

---

## 13. ASSERTION DENSITY — the suite is not lazy, it is misaimed (CONFIRMED)

AST scan over all 983 tests: **3,147 assertions, mean 3.2, median 3 per test. Only 4 tests have zero
assertions.** Distribution: 1635 assertEqual, 328 assertTrue, 210 assertFalse, 153 assertRaisesRegex,
152 assertIn, 124 assertIsNone, 106 assertRaises, 91 assertAlmostEqual.

This matters for the verdict: the problem is **not** thin assertions or missing coverage of happy paths.
The problem is that ~3,100 precise assertions are aimed at fixtures the same authors constructed, so they
measure self-consistency at high resolution. Density without an independent oracle is what makes 22k lines
of tests survive five inverted risk limits.

The 4 zero-assertion tests are worth naming since one is directly implicated:
- `tests/test_backfill.py:183 test_research_normalization_accepts_every_backfilled_row` — calls
  `normalize_underlying_bar` on each backfilled row and asserts nothing; it passes if no exception is
  raised. This is the test standing closest to defect 6a (§6a) and it is incapable of failing on a value.
- `tests/research/test_rule_grammar_v2.py:270`, `tests/test_execution_lifecycle.py:728`, `:732`.

Also noted: `tests/research/test_paper_performance.py:31-40 _candidate()` forces a lifecycle status with raw
SQL — `db.execute("UPDATE candidate_state SET status=? WHERE candidate_id=?")` — bypassing every transition
guard. Another fixture that manufactures an authorized state the production path cannot reach on its own.
