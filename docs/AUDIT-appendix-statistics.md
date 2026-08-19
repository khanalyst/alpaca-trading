# Audit — Statistics domain (gates.py / stats.py / proof*.py / calibration.py / fit_diagnostics.py / protocol.md)
Branch: claude/trading-strategy-audit-zveg57. Findings appended as confirmed. No repo files modified.

---

## F1 [CRITICAL] CONFIRMED — Moving-block bootstrap is mathematically degenerate whenever cluster_count <= 5; the "95% lower confidence bound" is *identically the point estimate*, zero-width.

`research/stats.py:298-321`
```
size = cluster_count
block_count = (size + length - 1) // length     # ceil(n/L)
...
selected.extend(order[(start+offset) % size] for offset in range(length))
selected = selected[:size]
```
If `length >= size` then `block_count == 1`, and `selected` is `[order[(start+0..L-1)%n]][:n]` — i.e. a **cyclic rotation of the entire cluster list**. The multiset of clusters is the complete sample in *every* replicate, so `pooled/count` is the observed mean in every replicate. `means` is a constant vector; `lower_bound == upper_bound == mean`.

The callers *guarantee* this branch is hit for small samples, because they clamp L up to the cluster count:
- `research/gates.py:535-536` `resolved_block_length = min(SERIAL_BLOCK_LENGTH, max(1, session_count))`
- `research/gates.py:662`   `block_length = min(SERIAL_BLOCK_LENGTH, max(1, cluster_count))`
- `research/gates.py:1119-1120` same clamp on `clusters`
- `research/gates.py:2249-2250` same clamp
with `SERIAL_BLOCK_LENGTH = 5` (`research/gates.py:64`).

So for any candidate whose evidence spans <= 5 sessions, the bound gate `lower_bound >= threshold` reduces to `point_estimate >= threshold` — **the confidence requirement is silently deleted**, while the payload still reports `available: True`, `confidence: 0.95`, `draws: 4000`. This is a false-evidence path, not merely a loss of power.

Executed (stdlib repro, `scratchpad/t1_degenerate.py`), iid N(0,1), 8 obs/cluster, draws=2000, L=min(5,n):
```
n_clusters=  2 L=2 mean=+0.13546 lb=0.13546 ub=0.13546 width=0.0  avail=True
n_clusters=  3 L=3 mean=-0.11565 lb=-0.11565 ub=-0.11565 width=0.0 avail=True
n_clusters=  4 L=4 mean=-0.14521 lb=-0.14521 ub=-0.14521 width=0.0 avail=True
n_clusters=  5 L=5 mean=+0.06892 lb=0.06892 ub=0.06892 width=0.0  avail=True
n_clusters=  6 L=5 mean=+0.07495 lb=-0.04405 ub=0.19395 width=0.238
```
Nothing anywhere in `stats.py` or `gates.py` detects the zero-width case. `min_clusters` (default 2, `stats.py:21`) does not help: n=2..5 all pass `available`.

## F2 [HIGH] CONFIRMED — Effective bootstrap sample size is ceil(n/L) blocks, not n. With L=5 the interval is driven by ~n/5 independent draws.
`research/stats.py:299` `block_count = (size + length - 1) // length`. Each replicate is built from only `ceil(n/5)` random starts. For n=6..9 that is 2 blocks; n=10..14, 2-3. A "bootstrap" with 2 effective draws produces an interval whose width is essentially noise. Observed non-monotone widths across n in the same run (n=15 width 0.452, n=30 width 0.128, n=60 width 0.136) confirm the estimator is dominated by block-count granularity rather than by n.

## F3 [CRITICAL] CONFIRMED — Empirical coverage of the nominal 95% one-sided lower bound is 47%-92%, never 95%. The one-sided false-positive rate is 1.8x-10x nominal.
Monte-Carlo coverage study (`scratchpad/t2_coverage.py`, 400 replications per cell, 800 bootstrap draws, 8 obs/cluster, TRUE mean = 0). Coverage = P(lower_bound <= true mean); nominal 0.950. `L` is the block length actually produced by the `min(SERIAL_BLOCK_LENGTH, n)` clamp in `gates.py`.

```
gen       n   L   reps   cover   degen%  avgwidth      implied one-sided type-I
iid       4   4    400   0.482    100%    0.0000        0.518   (10.4x nominal)
iid       5   5    400   0.472    100%    0.0000        0.528   (10.6x)
iid       6   5    400   0.833      0%    0.2550        0.167   (3.3x)
iid      10   5    400   0.840      0%    0.2454        0.160   (3.2x)
iid      20   5    400   0.890      0%    0.2112        0.110   (2.2x)
iid      40   5    400   0.922      0%    0.1658        0.078   (1.6x)
iid      60   5    400   0.917      0%    0.1411        0.083   (1.7x)
iid      20   1    400   0.948      0%    0.2486        0.052   (ok)
iid      40   1    400   0.955      0%    0.1799        0.045   (ok)
iid      60   1    400   0.945      0%    0.1479        0.055   (ok)
R+clus    4   4    400   0.458    100%    0.0000        0.542
R+clus    6   5    400   0.823      0%    0.4351        0.177
R+clus   20   5    400   0.895      0%    0.3606        0.105
R+clus   60   5    400   0.912      0%    0.2381        0.088   (1.8x)
R+clus   60   1    400   0.940      0%    0.2500        0.060
```
(`R+clus` = realistic R-multiple generator: 40% +2R / 60% -1R with a session-level common shock, i.e. genuine intra-session clustering.)

Two independent conclusions:
1. At n <= 5 clusters coverage collapses to ~0.47 — exactly the coin-flip you get when the "bound" is the point estimate (F1).
2. **Even at n = 60 sessions, the L=5 block bootstrap under-covers: 0.912-0.917 vs 0.950**, i.e. the gate's actual one-sided alpha is ~0.085, not 0.05. Setting L=1 (plain cluster bootstrap, which is the *correct* choice if sessions are the independent unit, as `stats.py:139-145` itself asserts) restores 0.940-0.955. **The block structure is the direct cause of the under-coverage.**

The mechanism is F2: with ceil(n/L) blocks the resample has too few effective draws, and the percentile method (`stats.py:313-316`) is a plain percentile bound with no bias/skew correction (no BCa, no basic/reverse pivoting, no studentization), so the first-order coverage error is never corrected.

## F4 [HIGH] CONFIRMED — Block length 5 is a magic constant with zero empirical justification; no autocorrelation is ever estimated anywhere in the repo.
`research/gates.py:64` `SERIAL_BLOCK_LENGTH = 5`. `grep -rn "autocorr"` over `research/` returns **nothing**. There is no Politis-White automatic block-length selection, no ACF/variance-ratio estimate, no n^(1/3) or n^(1/5) growth rule (the block length is a *fixed* 5 whether n=6 or n=600, so the estimator is not even consistent as n grows — a valid block bootstrap requires L -> infinity with L/n -> 0). Clusters are calendar days (`gates.py:62 CLUSTER_SECONDS = 86_400`), so L=5 is presumably "one trading week", chosen by analogy rather than from the data. Per F3 it is strictly harmful: it destroys coverage that L=1 achieves.

## F5 [LOW] CONFIRMED — dead expression.
`research/gates.py:1096` `"block_length": (min(SERIAL_BLOCK_LENGTH, 1) if block_length is None else ...)` — `min(5, 1)` is the constant 1. The unavailable-branch payload advertises `block_length: 1` while the available branch (`gates.py:1119`) advertises up to 5, so the two payloads are not comparable and the recompute check at `gates.py:1714-1732` can be fed either.

## F6 [HIGH] CONFIRMED — EMPIRICAL NULL TEST. The 29-check gate has the operating characteristic of ONE one-sided randomization test. `heldout_p_significant` and `falsification` are the same test computed twice.
Harness `scratchpad/t3_null.py` drives the *real* gate functions (`G.matched_cluster_test`, `G.placebo_null_distribution`, `G.falsification_gate`, `G.walk_forward_report`) with constructed zero-edge matched rows (5 symbols x 40 sessions = 200 matched pairs, session-level common shock so clustering is real, candidate delta ~ N(0,1), baseline identically 0). 300 replications:
```
edge = 0.0, n_sessions = 40, reps = 300
  heldout_delta_positive              0.480
  heldout_delta_lcb_positive          0.063
  heldout_p_significant               0.033
  falsification                       0.033
  walk_forward_available              1.000
  walk_forward_adequate               1.000
  walk_forward_majority_positive      0.487
  ALL STATISTICAL CHECKS PASS         0.033
```
Observations:
- **The joint pass rate (0.033) equals the single falsification rate (0.033) exactly.** Every other statistical check is logically implied by it. Stacking checks buys nothing.
- `heldout_p_significant` (`gates.py:1489`, sourced from `matched_cluster_test` -> `paired_cluster_sign_flip`, `stats.py:737-767`) and `falsification` (`gates.py:1497`, sourced from `falsification_gate` -> `sign_flip_null_statistics`, `stats.py:85-131`) are **the same cluster sign-flip randomization test on the same deltas with the same day clustering**, one by enumeration/MC of cluster sums and the other by MC of cluster means. They are ~perfectly correlated (both 0.033/300, identical replication-by-replication). Listing both in `GATE_REQUIRED_CHECKS` (`gates.py:46-61`) presents one piece of evidence as two.
- `heldout_delta_positive` (0.480) and `walk_forward_majority_positive` (0.487) are **coin flips under the null** and contribute ~no evidence; `walk_forward_available`/`walk_forward_adequate` are 1.000 (pure sample-size assertions, no inference).
- `heldout_delta_lcb_positive` fires at 0.063 vs nominal 0.05 here (mild; the severe under-coverage of F3 applies at smaller cluster counts and to the L=5 regime generally).

Consequence: the effective per-candidate one-sided alpha of the whole gate is ~0.05, not the ~0.05^k the 29-check facade implies. With the ~16k evaluations/year search surface, that is ~800 null candidates/year reaching the FDR stage on the statistical checks alone.

## F7 [HIGH] CONFIRMED — `walk_forward_majority_positive` is a required check that a zero-edge candidate passes ~50% of the time, and 3 folds cannot produce a p-value below 0.125 even in the best case.
`research/gates.py:1019` `"majority_positive": bool(... positive*2 > len(adequate_results))`, with `folds=3` the default (`gates.py:927`). Under the null each fold is positive with prob ~1/2, so P(>=2 of 3) = 1/2. Measured 0.487 over 300 reps (F6). Even the strongest possible walk-forward outcome (3/3 positive) is a sign test with null probability 1/8 = 0.125 — it can never be significant at 0.05. This check is decorative.

## F8 [MEDIUM] CONFIRMED — `matched_pairs` silently deletes every observation whose (vehicle,symbol,session) key is duplicated.
`research/gates.py:601-617` (`_unique_by_match_key`): a repeated `_match_key` is added to `duplicates` and then **both/all copies are popped**. `_match_key` (`gates.py:834-843`) is `f"{vehicle}:{symbol}:{session_date}"` unless a `comparison_id` is present. Any strategy taking more than one trade per symbol per day therefore contributes **zero** matched pairs for those symbol-days to `matched_cluster_test`, `placebo_null_distribution`, `walk_forward_report` and `qualification_report`. The deletion is silent (no counter, no reason code in the returned payload) and its rate is a function of trade frequency, so the effective sample of the authorizing paired test is not the sample the floors (`sample_counts`, `gates.py:912-922`) counted.

## F9 [MEDIUM] CONFIRMED — `preselected` is a self-declared boolean the producer always sets True; verification only checks that the producer said so.
`research/gates.py:1753` requires `post_selection.preselected is True` for a passing envelope. The sole authorizing producer hardcodes it: `research/strategy_factory.py:2387` `preselected=True`, unconditionally, in the qualification call. Nothing cross-checks that selection actually happened before the seal (no ordering token, no selection digest bound to the sealed window's digest). The docstring's contract (`gates.py:1057-1061`) is honour-system.

## F10 [HIGH] CONFIRMED — Measured power. At the protocol's own "minimum useful edge" the gate is a lottery: ~12% power. It only becomes a real test above ~0.2 sigma.
Same harness, `scratchpad/t4_power.py`, 120 replications per effect, 40 sessions x 5 symbols = 200 matched pairs, per-pair delta sd = 1.0 plus a 0.3 session shock (i.e. the effect is quoted in per-trade standard deviations):
```
true edge   delta_positive  lcb_positive  p_signif  falsif  wf_majority  ALL-PASS
   0.05          0.725         0.167       0.133    0.125     0.667       0.117
   0.10          0.875         0.342       0.258    0.292     0.825       0.250
   0.20          0.992         0.808       0.767    0.758     0.975       0.742
   0.30          1.000         0.975       0.967    0.975     1.000       0.967
   0.50          1.000         1.000       1.000    1.000     1.000       1.000
```
`RETIREMENT_MIN_USEFUL_R = .05` (`gates.py:45`) is the preregistered minimum economically useful edge. In per-trade-sd units a 0.05R edge is <=0.05 sigma for any realistic R-multiple dispersion, i.e. **power ~0.12 at best in this deliberately generous configuration** (200 pairs, 40 clusters, no missing data, no duplicate-key deletion). The gate resolves 0.2-0.3 sigma edges and nothing smaller. Combined with F6 (effective alpha ~0.05 per candidate) and a ~16k/year evaluation surface, the *prior odds* of a passing candidate being real are dominated by the null arm: at 0.05 alpha and 0.12 power, even a 1-in-100 true-edge base rate gives a posterior of 0.12*0.01 / (0.12*0.01 + 0.05*0.99) = 2.4% true.

The system also never uses the power number it computes: `clustered_mde_power_report` (`stats.py:330-471`) is hardcoded `"diagnostic_only": True, "authorizing": False` (`stats.py:411-412`) and no gate consumes `mde` or `estimated_power` — grep shows no `GATE_REQUIRED_CHECKS` entry for power/MDE (`gates.py:46-61`). A candidate whose own MDE report says the study cannot detect the minimum useful edge still passes.

## F11 [CRITICAL] CONFIRMED — Cluster assignment is (a) silently destroyed by an unparseable timestamp and (b) dependent on the host machine's local timezone. Both inflate significance and both break the determinism the proof system claims.
`research/gates.py:640-646` in `matched_pairs`:
```
stamp = row.get("entry_timestamp") or row.get("session_date")
try:
    timestamp = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
except (TypeError, ValueError):
    timestamp = float(index * CLUSTER_SECONDS)      # <-- index of enumerate(sorted(left))
clusters.append(int(timestamp // CLUSTER_SECONDS))
```
(a) **Malformed-stamp fallback assigns every observation its own cluster.** Executed (`scratchpad/t5_cluster.py`), 8 matched pairs over 4 sessions with an unparseable `entry_timestamp`:
```
clusters: [0, 1, 2, 3, 4, 5, 6, 7]  -> 8 distinct clusters from 4 sessions
```
The cluster sign-flip test and the cluster bootstrap then treat every trade as an independent unit — exactly the "correlated intraday trades masquerading as independent evidence" the docstrings (`gates.py:512-515`, `stats.py:140-142`) say the design prevents. There is no error, no reason code, and no field in the returned payload recording that the fallback fired. `sample_counts` (`gates.py:912-922`) counts clusters from `session_date` instead, so the floors will still report the correct 4 clusters while the inference runs on 8.

(b) **A naive (tz-less) `entry_timestamp` is interpreted in the host's local time**, so cluster boundaries move with `TZ`. Executed (`scratchpad/t5b.py`), two trades on the *same* `session_date` 2025-01-06 at 09:30 and 20:00:
```
TZ=UTC                    clusters=[20094, 20094]  n_distinct=1
TZ=America/New_York       clusters=[20094, 20095]  n_distinct=2
TZ=Pacific/Midway         clusters=[20094, 20095]  n_distinct=2
TZ=Pacific/Kiritimati     clusters=[20093, 20094]  n_distinct=2
```
The same evidence yields 1 or 2 "independent" clusters depending on the machine. This defeats `recompute_gate_statistics` / `verify_gate_envelope` (`gates.py:2203`, `gates.py:1654`) as a reproducibility check — a proof verified on one host can fail or change conclusion on another — and it silently doubles the apparent independent sample.

(c) Even with correct UTC stamps, `CLUSTER_SECONDS = 86_400` (`gates.py:62`) buckets by **UTC calendar day, not by trading session**. A US session crossing 00:00 UTC (19:00 ET, i.e. any extended-hours or overnight leg) is split into two clusters from one session.

## F12 [CRITICAL] CONFIRMED — The online-FDR alpha-wealth uses gamma_j = 1/(j(j+1)), which decays as 1/j^2. The allocated alpha becomes unreachable after ~40-300 confirmatory tests and the pipeline permanently self-terminates; the only coded escape is bumping a scope string, which resets the wealth to full.
`research/factory_ledger.py:54-59`
```
def _fdr_gamma(index): return 1.0 / (int(index) * (int(index) + 1))
```
`research/factory_ledger.py:61-69` `_fdr_allocation`: `allocated = alpha*gamma(t) + alpha*sum_{discoveries j} gamma(t - tau_j)`, capped at alpha.

Executed (`scratchpad/t7_lord.py`), alpha = .05, no prior discovery:
```
 t=    1  alpha_t = 2.500e-02   required randomization iterations >=        40
 t=   10  alpha_t = 4.545e-04                                    >=     2,200
 t=   50  alpha_t = 1.961e-05                                    >=    51,000
 t=  100  alpha_t = 4.950e-06                                    >=   202,000
 t=  250  alpha_t = 7.968e-07                                    >= 1,255,000
 t=  500  alpha_t = 1.996e-07                                    >= 5,010,000
 t= 1000  alpha_t = 4.995e-08                                    >=20,020,000
```
`live_shadow_ingest.py:55-61` `_confirmatory_iterations` returns `ceil(batch_size / allocated_alpha)`, and `live_shadow_ingest.py:38` caps it at `MAX_CONFIRMATORY_ITERATIONS = 2_000_000`. Exceeding the cap returns `confirmatory_resolution_exhausted` for **every** candidate (`live_shadow_ingest.py:807-819`) — a terminal state, since the allocation only ever shrinks. Solving `batch * t(t+1)/alpha = 2e6`:
- batch = 1 candidate/night  -> dies at t ~ 316 confirmatory tests
- batch = 10                 -> dies at t ~ 99
- batch = 50                 -> dies at t ~ 44
- batch = 100                -> dies at t ~ 31
The standard LORD gamma sequence (Javanmard-Montanari / Ramdas et al.) is ~ log(max(j,2)) / (j * exp(sqrt(log j))) precisely so the allocation decays sub-polynomially and the procedure stays usable indefinitely. 1/(j(j+1)) sums to 1 but is far too aggressive; it makes the online procedure a one-year fuse.

The only escape present in the code is the **scope version string**: `live_shadow_ingest.py:47` `CONFIRMATORY_SCOPE_VERSION = "shadow-confirmation-v4"`, used as the SQL partition key (`factory_ledger.py:977-981`, `1006-1008`). Changing that string starts a fresh row set, so the accumulated alpha spend and discovery history vanish and the wealth resets to the full alpha. The repo comment at `live_shadow_ingest.py:44-46` shows this has already been done twice ("v4 starts a fresh durable LORD sequence. The prior v2 sequence spent..."). **Multiplicity accounting that is reset by editing a constant is not multiplicity accounting.**

## F13 [MEDIUM] CONFIRMED — The wealth recursion is not a valid LORD++ update: it grants a full `alpha` payout for the FIRST discovery on top of an initial wealth of `W0 = alpha`.
LORD++ is `alpha_t = gamma_t*W0 + (alpha - W0)*gamma_{t-tau_1} + alpha*sum_{j>=2} gamma_{t-tau_j}` with `W0 <= alpha`. `factory_ledger.py:64-68` implements `alpha_t = alpha*gamma_t + alpha*sum_{j>=1} gamma_{t-tau_j}` — i.e. `W0 = alpha` **and** a full-`alpha` first payout, where LORD++ requires `alpha - W0 = 0`. Total wealth granted over the stream is `alpha*(1+R)` instead of `alpha*max(R,1)`: an additive excess of exactly one full `alpha` unit.

Simulated global null (`scratchpad/t7_lord.py`, 20,000 replications, T=200, p ~ U(0,1)):
```
CODE (_fdr_allocation)     E[false rejections] = 0.0515
LORD++ W0 = alpha          E[false rejections] = 0.0493
LORD++ W0 = alpha/2        E[false rejections] = 0.0253
```
The inflation is real but modest under the global null (~4%); it matters more in the regime with discoveries, where the excess is a whole extra alpha of budget. Rank MEDIUM only because F12 dominates.

## F14 [HIGH] CONFIRMED — The LORD stream is fed a *minimum-of-K selected* candidate, and only 1-2 tests per night reach it, while ~16k evaluations/year are screened.
`live_shadow_ingest.py:846-848` picks `selected_id = min(selectable, key=p_adjusted)`; `live_shadow_ingest.py:568-579` returns `not_selected` for everyone else with `"online_allocation_spent": False`. Only the argmin consumes an allocation (`live_shadow_ingest.py:613-617`). Online FDR procedures require the decision *to test* to be independent of the p-value being tested; here it is `argmin`. The mitigating fact is that the LORD p comes from the **disjoint newer half** (`_split_sessions`, `live_shadow_ingest.py:117-133`; confirmatory gate at `live_shadow_ingest.py:585-593`), so under session independence the confirmatory p is still uniform. That mitigation fails whenever candidate performance is serially persistent across the split (regime persistence), which is the normal case in market data — selection on window 1 then correlates with window 2. SUSPECTED severity, CONFIRMED mechanism.

Independently: because only the argmin is ever confirmed, **the FDR ledger records at most ~250 tests/year against a search surface of ~16,000 evaluations/year**. Every non-selected evaluation is a look at the data that leaves no trace in any multiplicity account.
