# Preregistered hypotheses

Everything else in this repository searches. Search finds hypotheses, and it
is also why a searched result cannot confirm itself: the sessions that
suggested an idea are the ones that would have to test it. A preregistered
hypothesis is fixed in code before its test data exist, and is decided by
rules written down in advance.

The source of truth is `research/preregistered.py`. This page explains it;
it does not override it.

## Status

| hypothesis | status |
| --- | --- |
| `vwap-reversion-control-adjusted.v1` | **compromised** (review finding D1-01); superseded by v2 on 2026-10-28 |
| `opening-range-fade-control-adjusted.v1` | **compromised** (review finding D1-01); superseded by v2 on 2026-10-28 |
| `vwap-reversion-control-adjusted.v2` | registered 2026-10-28, sealed from 2026-10-29, accruing |
| `opening-range-fade-control-adjusted.v2` | registered 2026-10-28, sealed from 2026-10-29, accruing |

Why v1 is compromised. A manifest hash freezes the manifest's text and
nothing else. For the VWAP hypothesis, committed at 21:27:48 UTC on
2026-09-22 (`c674f2f`), the control-tier decision rule was added to
`look_outcome` 53 minutes later (`09e43d9`, 22:20:02 UTC), in the same commit
that recorded the first sealed-session interim; the manifest text does not
contain that rule, so its hash did not change. For both hypotheses the
measuring code then changed while sealed sessions were being examined:
`research/signal_quality.py` and `agent/contracts/rule.py` changed on
2026-10-07 (`bba52bb`) and 2026-10-08 (`c3da2ba`), about 1,000 lines in all,
and every session up to 2026-10-23 has since been examined. A v1 look would
test whatever code is checked out on the day, partly chosen after unblinded
data, not the hypothesis as sealed.

The v1 manifests and their evaluation are deliberately left exactly as they
were (`tests/research/test_preregistered_reproducibility.py` pins the whole
v1 report on a synthetic corpus to fingerprints computed with the original
code), so stored v1 results still reproduce. A v1 result must not be read as
a confirmatory test. The command line prints a warning for v1.

## `vwap-reversion-control-adjusted.v1` (compromised)

Registered 2026-09-22; compromised by D1-01 (see Status). Manifest hash
`d2e56d80bdb0e367bcceebbe4792bf4a00aee815eb442e62f043ee2e40b7c48e`, pinned by
`tests/research/test_preregistered.py`.

**Claim.** On the configured 24-ETF universe, bars on which the frozen
session-VWAP reversion rule fires are followed over the next 60 minutes by a
return in the rule's own direction that beats a clock-matched control by at
least 3.0 bps on average.

**Why this one.** It led five in-sample measurements in the September 2026
audit (`docs/audit-2026-09-21/SIGNAL-VALUE.md`), and its mirror, `vwap_trend`,
was the worst arm in the catalogue. It was chosen *because* of those results,
which is exactly why it may only be confirmed on sessions they did not use.

**Population caveat** (added October 2026, review finding A-11). The
measurements that motivated this hypothesis were not taken on the population
the sealed test uses. All of them used public consolidated-tape bars,
labelled `historical_backfill` on `delayed_sip`
(`docs/audit-2026-09-21/FORWARD-2026-09-23.md`), whereas the sealed test uses
Alpaca IEX. The bracket replays among them also evaluated only bars from
index 60 of each session onwards (`docs/audit-2026-09-21/q1trades.py:31`,
`forward_costs.py:41`), about 10:30 on a complete session, whereas the
registered subject can signal from 09:50, once its 21-bar warm-up completes. The
control-adjusted signal-quality measurement (`step6c.py`) used the whole
session. The registered rule and its decision rule are unchanged; this only
states what the prior evidence does and does not describe.

| item | fixed value |
| --- | --- |
| subject | `rule.vwap-reversion.ab97bfe87ed566de` (diagnostic-shadow slot 7 baseline) |
| primary endpoint | 60-minute control-adjusted forward return, `research.signal_quality` |
| inference unit | the session: CR1 clustered t (Student-t, G-1 df) and a cluster sign-flip |
| sealed window | sessions strictly after **2026-09-21** |
| decision data | Alpaca IEX, explicitly `forward_observed` (the deployment's own feed) |
| look 1 | exactly the first 30 sealed sessions, one-sided alpha 0.025 |
| look 2 | exactly the first 60 sealed sessions, one-sided alpha 0.025 |
| economic hurdle | 3.0 bps, the kill threshold written at commit `dc156ca`; a necessary condition only (see below) |

**Decision at each look.**

- **pass**: clustered t test *and* sign-flip test both below alpha, *and* the
  mean is at least 3.0 bps. Significance alone never passes.

  The 3.0 bps is a **necessary condition only**, never evidence that an
  effect is worth trading (review finding D1-03). It is the audit's kill
  floor, applied to the *point estimate* of a *pre-cost*, signal-close
  anchored, control-adjusted return. It never binds: at the looks a pass
  already needs a mean of roughly 10.5 bps (look 1) and 7.2 bps (look 2), and
  the repository's own static round trip is 17 bps
  (2 x (2 + 6 + 0.5) bps in `research/costs.py`). A v1 pass is therefore
  compatible with an effect that loses money after costs. v2 replaces it
  with an after-cost hurdle on a confidence bound.
- **futility**: the mean is credibly below 3.0 bps. Retire the hypothesis.
- **inconclusive**: neither. Continue to look 2; at look 2 the claim is not
  established and the hypothesis is retired. There is no third look.
- Fewer than 30 matched controls, or coverage below 80%, is inconclusive.
- Any control from a same-session fallback tier makes the look
  inconclusive. This rule was added after sealing (D1-01) and runs before
  the futility branch, so one thin symbol can void a look and turn a
  futility into inconclusive (D1-02). It is kept so v1 reproduces.
- Before look 1: descriptive only. No decision and no early stop.

**What a pass buys.** Entry to the existing proof pipeline: held-out replay,
controls, false-discovery correction, live-shadow parity and the paper trial.
It never authorizes trading, sizing or a configuration change.

**No new runtime arm is needed.** The subject is already in the deployed
diagnostic shadow cohort, and a test fails if it ever leaves it, so the
recorder corpus on the VM is already accumulating the data this needs.

## `opening-range-fade-control-adjusted.v1` (compromised)

Registered 2026-09-23, while that day's session was trading and before any of
its bars were fetched; compromised by D1-01 (see Status). Manifest hash
`a0ae21f5ffd765142f2f4b92c47e33f652e0fb3213f1fd8d5a6d82c3915c2b9e`, pinned by
`tests/research/test_preregistered.py`.

**Claim.** Bars on which the frozen opening-range fade rule fires are followed
over the next 60 minutes by a return in the rule's own direction that beats a
clock-matched control by at least 3.0 bps on average.

**Why this one.** It was the only catalogue arm still positive after
realistic costs in the audit's bracket replay (+10.2 bps per trade over 59
trades, session-clustered t +2.57), with a +19.5 bps control-adjusted delta at
60 minutes (clustered t +2.95). It also lost on 2026-09-22. All of that is
in-sample, which is why it needs its own sealed test.

**Population caveat** (added October 2026, review findings A-11 and I2-01).
The +10.2 bps bracket replay evaluated only bars from index 60 of each
session onwards (`docs/audit-2026-09-21/forward_costs.py:41`), about 10:30 on
a complete session, on public consolidated-tape backfill (`delayed_sip`). The
registered subject has `entry_after_minutes` 0 and a 20-minute range, so it
can fire from 09:50, and the sealed test uses Alpaca IEX. The 09:50 to 10:30
window, where an opening fade is most likely to fire, was therefore never in
the trade-level sample, and the feed differs. The +19.5 bps control-adjusted
delta (`step6c.py`) covered the whole session, but on the same consolidated
backfill. The review later measured the gap on 22 fresh sessions: 35% of the
subject's first trades signal before 10:30 and performed about the same as
later ones (+5.9 bps against +5.7 bps net;
`docs/review-2026-10-15/reviews/handoff-I2_audit_coordinator.md`). The
registered rule and its decision rule are unchanged.

| item | fixed value |
| --- | --- |
| subject | `rule.opening-range-fade.d5785d9e70b56def` (diagnostic-shadow baseline) |
| mirror | `rule.opening-range-breakout.0eb200d3136d80ee`, expected negative |
| sealed window | sessions strictly after **2026-09-23** |
| everything else | identical to the VWAP hypothesis above: endpoint, data, looks, alpha, hurdle, decision rule |

It is also the incumbent of the paper profile `deploy/paper-orf.config.json`
(trial `paper-orf-baseline-20260923-v1`). The paper trial's real fills are
trade-level evidence under the trial's own floors; they never enter this
decision, and this decision never grants the trial anything.

Each hypothesis spends its own alpha and is reported separately.

## `vwap-reversion-control-adjusted.v2` and `opening-range-fade-control-adjusted.v2`

Registered 2026-10-28; the window starts at the first full session after
the registration was published. Same frozen subjects and mirrors as v1. The
registration date, the first sealed session, the last examined session and
the instrument digest are one block of literals at the top of
`research/preregistered.py`; `v2_registration_literals()` prints the
instrument literals and the manifest hashes for the running tree.
Manifest hashes, pinned by `tests/research/test_preregistered_v2.py`:

- VWAP: `d0003f117e8e47a5b15653f645d0fa2ab72463e1d289ef118406c77774eb6b93`
- ORF: `22b7ca355aa42b72c766c5d56d829ba5f14618a010d29aceb4ce75caeab7c13f`

**Claim.** Entering at the open of the bar after the frozen rule fires is
followed over the next 60 minutes by a return, in the rule's direction, that
beats a clock-matched control by more than the repository's own 17.0 bps
round-trip cost, as a one-sided lower confidence bound.

| item | fixed value |
| --- | --- |
| sealed window | complete sessions on or after **2026-10-29**; every session up to 2026-10-23 had been examined |
| instrument | digest `1dced3ad98f1aa089696c929773d5388e5ecd2993d2821cbd190108eb8325e31` of the running code (below) |
| primary endpoint | 60-minute control-adjusted return from the **next bar's open**, horizon fixed on the wall clock |
| sensitivity | the same from the open one bar later (one-bar skip), reported, never decisive |
| diagnostic | the v1-style signal-close anchored figure on the same clock, reported only |
| control | same symbol, same session minute, other complete sessions of the look window, registered tier only |
| countable session | exact Alpaca calendar, regular 09:30 to 16:00, closed and dated before the run, coverage floors met |
| looks | first 30 and first 60 countable sessions, one-sided alpha 0.025 each, each decided once |
| hurdle | 17.0 bps round trip, on the one-sided lower confidence bound |

**Instrument identity (D1-01).** The manifest records one sha256 per module
over the exact source of every module that `research.preregistered` and
`research.signal_quality` can import, at any depth and including imports
inside function bodies, within the `research`, `agent` and `deploy`
packages, together with each package `__init__`, and one digest over those
(46 modules at registration). The only text left out is the registration
literal block of `research/preregistered.py`, which holds the identity
itself. Whole modules are hashed on purpose: the review of this registration
showed that a definition-level closure missed a module-level statement that
changes an anchor, an `__init__` that changes a default, and the corpus
readers imported inside `_load`. `evaluate()` recomputes the identity of the
running code and the manifest hash, and refuses any decision when either
differs (`instrument_digest_mismatch`, `manifest_hash_mismatch`).

Because every byte of those 46 modules counts, the repository's own tree
stops matching as soon as any of them is edited. **v2 is therefore always
evaluated from a checkout of the registration commit**, never from the
current tree:

```sh
git worktree add /opt/prereg-v2 <registration commit>
cd /opt/prereg-v2 && python -m research.preregistered ...   # as below
```

The unit test that compares the running tree with the registration skips
itself once the tree has moved on.

**Disclosure.** No statistic computed on sealed sessions is reported before a
look is decided and recorded. An interim run (fewer than 30 sessions), a
refused run, a diagnostic run (a corpus that is not decision-eligible) and a
run that meets a look-record conflict report session counts only, and a
later look is not evaluated until the earlier one is recorded. This closes
the route the review found: see sealed results in a refused run, change the
rule, re-register.

**Decision at each look.**

- **pass**: the one-sided 97.5% CR1 lower confidence bound of the mean
  control-adjusted delta is above 17.0 bps, *and* a one-sided session
  sign-flip test of the deltas less 17.0 bps has p < 0.025, *and* the
  candidate's own mean return less 17.0 bps has a CR1 t statistic above the
  same critical value. The last condition means a pass cannot coexist with
  a candidate that loses money after costs while beating a falling control.
- **futility**: on a look that meets the control floor, the one-sided 97.5%
  upper bound is below 17.0 bps. Retire. An underpowered look (fewer than 30
  matched controls, or coverage below 80%) is inconclusive whatever its
  bounds.
- **inconclusive**: neither. Continue to look 2; at look 2 not established.
- A candidate with no control in the registered cross-session tier is left
  out of the statistics and counts as unmatched (D1-02).
- No look follows a pass or a futility.

The 17.0 bps hurdle is the static round trip of `research/costs.py` at
registration. The per-symbol model added in the same change (`per_symbol_v2`)
puts the true round trip for these ETFs at about 1.75 to 4.07 bps, so the
hurdle is deliberately conservative: it makes a false pass unlikely and costs
power.

**Countable sessions (D1-04).** Bars of symbols outside the registered
24-ETF universe are dropped before anything else, so a change to the
recorder's symbol list cannot change the tested population. A date counts
only if every bar carries the same exact Alpaca `session_open`/`session_close`,
the session is a regular 09:30 to 16:00 New York session (early closes are
excluded), its date is before the evaluation date and its close is at or
before the evaluation instant, at least 95% of its minutes carry a bar for
some universe symbol, and at least 18 of the 24 universe symbols have a bar
in at least half of its minutes. An evaluation instant later than the wall
clock is refused (`as_of_in_future`), so a session still trading can never be
counted. Excluded dates are dropped entirely and listed with a reason. These
floors were set before any v2 session existed; if IEX coverage makes them
unreachable, register v3 rather than editing v2.

**Each look is decided once (D1-04).** A decision-bearing run appends one
record per newly decided look to an append-only JSONL file: look number,
session list and its digest, the instrument digest, the result and its
digest. Every later run recomputes the look and must reproduce that record;
it never decides it again. A mismatch (different sessions, code or result),
a record whose result does not match its own digest, or a second record for
the same look is a conflict, and the run decides nothing. A final line
without its newline (an append interrupted before it was acknowledged) is
dropped under the lock and the look is decided again, deterministically.

**Amendments before the window opened.** The registration was published at
13:05 UTC on 28 October with manifest hashes `65e38c51...` (VWAP) and an
instrument digest `a809497f...`. It was amended twice on 28 October, both
times before any sealed session existed: once for an opt-in change to
`agent/contracts/rule.py` (`232a0b94...`, digest `7162116b...`), and once for
the review fixes above (the hashes in this section). The sealed window starts
at the first full session after the final amendment was published. No
session on or after that date had been examined by anyone when it was set.

### Running v2

From a checkout of the registration commit:

```sh
python -m research.preregistered \
  --hypothesis vwap-reversion-control-adjusted.v2 \
  --data /app/runtime/research/recorded-forward-2026-09-16 \
  --look-records /app/runtime/research/preregistered/looks.jsonl \
  --out /app/runtime/research/preregistered/vwap-reversion-v2-$(date -u +%F).json
```

`--look-records` is required for v2; the file is locked while a run reads,
evaluates and appends. Do not delete or edit it. `--as-of` sets the
evaluation instant (default now; never later than now).

## Running it

On the VM, against the recorder's forward corpus (a JSONL file or a directory
of session partitions):

```sh
python -m research.preregistered \
  --data /app/runtime/research/recorded-forward-2026-09-16 \
  --out /app/runtime/research/preregistered/vwap-reversion-$(date -u +%F).json
python -m research.preregistered \
  --hypothesis opening-range-fade-control-adjusted.v1 \
  --data /app/runtime/research/recorded-forward-2026-09-16 \
  --out /app/runtime/research/preregistered/opening-range-fade-$(date -u +%F).json
```

A report is never replaced; each run writes a new file. The run is
decision-bearing only if `research.source_validation.validate_source` accepts
the corpus as authorizing *and* every bar carries the Alpaca IEX feed. Any
other corpus is evaluated and labelled `diagnostic_only`, with the reasons, and
`rule_outcome_if_eligible` shows what the rule would have said.

A bar that does not state its `source_mode` is normalized as
`forward_observed` by default. The evaluator never trusts that default: it
takes eligibility from the source preflight only.

## Changing a hypothesis

Don't. Any edit to a manifest changes its hash and fails the suite. For v2,
the in-tree instrument is expected to drift as the repository moves on; that
is why decisions are made from the registration commit, where the digest and
the manifest hashes are checked at run time. Register a new version
(`...v3`) with its own sealed window starting after the last session anyone
has examined, and leave earlier versions and their reports intact. Do not
commit unblinded interim results before look 1.
