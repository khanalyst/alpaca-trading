# Re-audit, 22 September 2026

A second pass over everything changed in this audit, after closing the two
remaining gaps: inference that treated intraday events as independent, and a
lead that had only ever been measured on the sessions that suggested it.

Read the reports in order: `FINDINGS.md`, `REMEDIATION.md`, `SIGNAL-VALUE.md`
(which carries a correction), then this.

---

## 1. What was closed

### Session-clustered inference (`1752ac9`)

`research/signal_quality.py` now reports, beside every control-adjusted
delta, a CR1 session-clustered standard error, t-statistic and degrees of
freedom, and a one-sided cluster sign-flip p using the repository's own
`sign_flip_null_statistics`. `research/stats.py` gains the CR1 estimator and
an exact Student-t tail (checked against textbook critical values from df 1 to
df 10,000). The operator report now prints the clustered t. The change is
additive and the schema is not bumped: the factory's screening decision does
not consume the t, and readers already use `.get()`.

### A preregistered forward test (`c674f2f`)

`vwap-reversion-control-adjusted.v1` fixes, in a content-hashed manifest, the
subject arm, the 60-minute primary endpoint, session-level inference, a sealed
window of sessions strictly after 2026-09-21, IEX `forward_observed` decision
data, two looks at exactly 30 and 60 sessions (alpha 0.025 each), and a pass
rule requiring both tests and a 3.0 bps economic hurdle. It was committed and
pushed **before any bar from its sealed window was fetched**; the commit
timestamp is 2026-09-22T21:27:48Z. The manifest hash is pinned in the suite.
Details: `docs/preregistered-hypotheses.md`.

One change was made to the evaluator after sealing, and it is stated here so
it can be checked: `look_outcome` now refuses to decide a look whose controls
used any tier other than the registered cross-session one. The sealed
manifest already specifies that control; the check enforces it. It can only
turn a pass into inconclusive, never the reverse, and the manifest hash is
unchanged. It was prompted by the first sealed session, where a single session
leaves no other sessions to match against and the instrument fell back to a
same-session tier.

---

## 2. What the re-audit found

### 2.1 A claim of mine was wrong

`SIGNAL-VALUE.md` said the event-level t overstated evidence "roughly 3x",
and that `vwap_reversion` was "not significant on its own". Measured, the
clustered t is a median **0.86** of the event-level value (range 0.50 to
1.28), and `vwap_reversion` holds at clustered **t = +4.34**, df 17. The error
came from extrapolating Q2's raw forward returns, which share each day's
market shock, to a signed control-adjusted delta, which largely does not. The
document is corrected inline and carries a correction section.

### 2.2 Signal semantics changed under unchanged ids; the epoch did not

`REMEDIATION.md` changed what several variant ids emit: the `volatility`
confirmation now measures the prior-window range width, the `trend_pullback`
proximity band is the authored threshold, the IBR width band is
horizon-scaled, and IBR replay applies the runtime filters. The ids did not
change, and neither did `REPLAY_ENGINE_EPOCH`.

That matters because the factory's `code_hash` is
`hash_file(strategy_factory.py)`: it covers that one file, not the evaluator
in `agent/contracts/rule.py` or the IBR contract. The repository's protocol
states the principle directly: evidence measured under a replay engine that
has since been corrected "still re-hashes and still recomputes, because the
recorded rows are exactly what that engine produced". The 18 September fixes
did not bump the epoch, but they were verified to leave outputs unchanged on
valid inputs. These changes deliberately alter outputs.

**Fixed:** `REPLAY_ENGINE_EPOCH` is now 7, with its rationale in
`research/edge_ledger_store.py`. Epoch-6 evidence is quarantined. There is no
authorized epoch-6 evidence, so the operational cost is nil. Every document
that named the current epoch is updated.

**Not fixed, recorded:** the narrow scope of the factory `code_hash` is
pre-existing. Hashing the evaluator modules into it would make evaluator
changes fail closed without relying on someone remembering the epoch. That is
a design change with its own identity consequences and is left for a separate
decision.

### 2.3 My scripts mislabelled public data

`step6b.py` normalized third-party consolidated-tape bars as provider
`alpaca`, feed `iex`, and left `source_mode` to its `forward_observed`
default. Labels do not enter the arithmetic, so its published numbers stand,
but the strict policy only admitted those rows because of the false labels.
Honestly labelled (`delayed_sip`, `historical_backfill`), the strict policy
admits **0** of those events and only the explicit diagnostic policy admits
them: the repository's safeguard works exactly as designed. `step6b.py`
carries a correction header and `step6c.py` is the honest version.

The underlying hazard is worth knowing: `EventIdentity.source_mode` defaults
to `forward_observed`, so any direct caller of `normalize_underlying_bar`
that omits the field gets that label. Authorizing paths are protected because
`research/source_validation.py` requires the label explicitly on raw rows;
the preregistered evaluator takes eligibility only from that preflight.

### 2.4 Living documentation had gone stale

The change of admission scenario made one sentence false in nine files: each
described the 25 bps arithmetic as shipped behaviour. All are corrected to
distinguish the shipped 9 bps **admission** scenario from the unchanged 25 bps
**proof-time** stress in `research/gates.py`. Also corrected:
`ARCHITECTURE.md` (said a binding floor widens the stop, and named a stale
epoch), `docs/edge-diagnostics.md` (IBR parity), `docs/strategy-variant-review.md`
(three arm ids and their values, the IBR width unit),
`docs/strategy-variant-deltas.json` (regenerated from code; all twelve pairs
remain single-coordinate), and `docs/current-findings.md`, which now opens
with the source state and states that **none of it is deployed**.

One distinction is kept deliberately: the built-in code default for a config
that omits the key remains 25 bps, a fail-safe, and `README.md` now says so
rather than "by default".

### 2.5 Adversarial review of the IBR admission merge

Checked line by line against `agent.contracts.ibr.evaluate_ibr_breakout`: the
ATR history includes the candidate bar and nothing later; the relative-volume
denominator is the opening-range mean; the extension is anchored on the
candidate close; the width band uses the same horizon scaling; staleness and
spread reject only a known breach, matching the contract's polarity. No
defect found.

### 2.6 CI

GitHub Actions ran all four test shards and the container build on each
branch commit, on Python 3.13 rather than the 3.11 used locally. All passed:
runs 35642923209, 35714908661 and 35739356781.

---

## 3. Measured results

### Clustered signal quality, 18 examined sessions

| arm, 60 minutes | delta (bps) | clustered t | df | sign-flip p |
| --- | --- | --- | --- | --- |
| `vwap_reversion` baseline | +9.44 | **+4.34** | 17 | 0.0010 |
| `vwap_reversion` variant | +9.07 | +3.23 | 17 | 0.0027 |
| `opening_range_fade` baseline | +19.51 | +2.95 | 17 | 0.0036 |
| `vwap_trend` baseline | -13.12 | **-6.36** | 17 | 1.0000 |

Eleven continuation arms are individually significant and negative (clustered
t -2.45 to -6.36). The reversion/continuation split now holds arm by arm, not
only as a pattern across arms.

Against the multiplicity actually incurred (120 one-sided tests), the
`vwap_reversion` baseline passes on the clustered t (p = 0.00022, threshold
0.00042) and fails on the sign-flip (p = 0.0010). The preregistered rule
requires both. These are the sessions that selected the hypothesis, so this is
motivation, not confirmation.

### First sealed session, 22 September

Descriptive only, and `diagnostic_only` because the data is public
consolidated tape, not the registered IEX feed. The subject fired 19 times with
a +6.95 bps control-adjusted delta at 60 minutes; the mirror, `vwap_trend`,
came in at -3.11 bps. Both signs match the claim. One session is one
observation: the clustered error term is correctly absent, and the control
used the fallback tier because no other sealed session yet exists. Nothing
can be concluded from it, and nothing is claimed.

---

## 4. Decisions, and why

| question | decision | reason |
| --- | --- | --- |
| Retire the continuation families? | **No** | In-sample and one regime. The preregistered test spends its own alpha outside the factory's denominator, and the diagnostic cohort spends none, so keeping them is free and their forward data feeds the mirror endpoint. |
| Add exit-side variant axes? | **No** | Measured at +0.44 bps held out (`SIGNAL-VALUE.md`). |
| Bump the schema for clustered fields? | **No** | Additive; nothing consumes the t for a decision. |
| Bump the replay epoch? | **Yes** | Output semantics changed under unchanged ids, and the code hash cannot see it. |
| Change the built-in 25 bps default? | **No** | It is a fail-safe for a config that omits the key; both shipped configs set 9 bps. |
| Deploy? | **Not from here** | The VM is pinned to `79c48c9`; deploying moves the cohort, config and epoch identities and needs the audited trial transition first. |

---

## 5. What remains open

1. **Costs (step 2).** Still blocked on the recorder's quote corpus. It is now
   the question that decides tradeability: +9.44 bps at 60 minutes is
   comfortably above a measured ~1.2 bps round trip and well below the
   modelled 17 bps.
2. **The forward test.** Run `python -m research.preregistered` on the VM's
   forward corpus. With every session recorded and accepted, look 1 falls on
   2 November 2026 and look 2 on 15 December 2026.
3. **Feed.** The decision runs on IEX, whose volume is roughly 2% of the
   consolidated tape and whose session VWAP therefore differs from the
   consolidated VWAP every measurement here used. A failure on IEX where
   consolidated data passed would be a finding about the feed, not a reason to
   change the test.
4. **Factory `code_hash` scope** (section 2.2).
