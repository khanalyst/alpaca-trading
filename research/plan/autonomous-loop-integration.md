# Autonomous loop integration

Merging the G2/evidence/promotion work and the audit-remediation work into one
branch that can run unattended for weeks and arrive at a decision worth a
human's attention.

Integration branch: `integration/autonomous-loop`, cut from `main` at
`c017bd1` and tagged `pre-integration-20260805`. Source branches are read-only
inputs and are never modified.

## The governing constraint

`agent/state.py::code_fingerprint` hashes every `.py` file under `agent/` plus
`main.py`. Attempt pooling breaks its lineage on any `code_identity_json`
mismatch, and `forward_feed_version` must fork whenever executable provenance
changes because evidence from different feed versions is never pooled.

Any merge that touches `agent/` or `main.py` during a collection window
therefore resets accumulated forward evidence. The batch order below settles
all runtime code first, then opens one collection window and leaves it alone.

G2 is the exception that makes this workable: its commit touches only
`research/`, `research.py`, docs and tests, so it can be merged early and
fixed repeatedly at no cost to evidence identity.

## What each source supplies

| Layer | Function | Source |
| --- | --- | --- |
| 0 | Execute and journal | main + batch2 exchange hardening + audit liquidation feed |
| 1 | Replay fidelity (G2) | batch2 `e941a4d` |
| 2 | Counterfactual research | main + audit conditioning/breadth axes |
| 3 | Forward paired evidence | main |
| 4 | Learning and hypothesis selection | main + audit dual lanes and substance validator |
| 5 | Qualification to promotion | batch2 artifact + batch3 demo gate |

## Batches

Each batch ends at a gate. A batch that cannot clear its gate stops the
sequence rather than proceeding on an assumption.

### Batch 1 - foundation and G2

- [x] B1.1 Cut `integration/autonomous-loop` from `main`; tag the baseline
- [x] B1.2 Record the pre-merge suite result as the regression reference
- [x] B1.3 Merge `e941a4d` (G2 replay fidelity); confirm no `agent/` files change
- [x] B1.4 Full suite green
- [x] B1.5 Bite-test: an unrecorded extra proposal must fail the gate closed
- [x] B1.6 Bite-test: an empty corpus must report `reproduction_rate: 0.0`
- [x] B1.7 Confirm readiness reports G2 as COLLECTING rather than PASS

Gate: suite green and both bite-tests fail closed when the gate is bypassed.

### Batch 2 - audit remediation and feed fork

- [x] B2.1 Merge `claude/algo-trading-audit-jlllr9`
- [x] B2.2 Bump `forward_feed_version` 6 -> 7 for the new snapshot fields
- [x] B2.3 Full suite green
- [x] B2.4 Confirm the liquidation endpoint returns real data from OKX
- [x] B2.5 Confirm docs state the current feed version

Gate: suite green and the liquidation feed confirmed live. Flush-fade now
requires `liq_notional_1h_usd`; if OKX does not supply it the strategy is
permanently contract-incomplete and that must be known before collection.

Gate evidence, 2026-08-05, `public/liquidation-orders` against production OKX:

| Instrument | Fills returned | Span | `liq_count_1h` | `liq_notional_1h_usd` |
| --- | --- | --- | --- | --- |
| `BTC-USDT-SWAP` | 660 | 23.7h | 0 | 0.00 |
| `ETH-USDT-SWAP` | 643 | 23.7h | 5 | 16,942.70 |
| `SOL-USDT-SWAP` | 71 | 23.7h | 0 | 0.00 |

The endpoint rejects `instId` alone with `50015` ("Either parameter uly or
instFamily is required"), which is why the caller passes `uly` and filters the
response back down to one instrument.

Liquidations are sparse: two of three instruments had none in the trailing
hour despite hundreds over the day. A quiet hour reads `0.0`, and
`missing_fields` treats `0.0` as present while rejecting `None` and `NaN`, so
flush-fade stays contract-complete through quiet periods and degrades only
when the feed genuinely says nothing.

### Batch 3 - exchange hardening and artifact identity

`7a3c5ee` mixed wired exchange hardening and artifact identity with roughly
3,550 lines of subsystem that no production path reaches
(`research/demo_evidence.py`, `research/exchange_envelope.py`,
`agent/exchange_observer.py`) plus an irreversible findings schema 16 -> 19
migration. `_migrate` refuses a downgrade and the tree carries no downgrade
path, so a store opened once at schema 19 can no longer be opened by schema-16
code.

- [x] B3.1 Decision: split requested and delivered on
      `codex/batch2-split-ledger`. The ledger is future post-deployment
      infrastructure with no production driver today, and the T3 plus PAPER
      promotion path does not require it. Commit A is taken now; commit B
      (`e017784`) stays unmerged and available
- [x] B3.2 Cherry-pick `b120f0d`
- [x] B3.3 Full suite green
- [x] B3.4 Bite-test the order-ambiguity paths: a malformed status response
      must pause rather than assume a fill
- [x] B3.5 Confirm no new unreachable module enters the tree undecided

Gate: suite green and the subsystem decision recorded.

Split verified before use rather than accepted on description: commit A plus
commit B produces a tree byte-identical to `7a3c5ee`, so the split is
lossless. Commit A alone carries `SCHEMA_VERSION = 16`, no migrations 17-19,
none of the three dormant modules, and a single residual observer mention that
is a comment. The 51 fail-closed order-ambiguity constructs remain.

B3.4 evidence. `verify_fill` was driven with eight malformed or unrecoverable
exchange responses: a non-string status, an unstructured response, a missing
order id, an id that does not match the request, a terminal state with no fill
quantity, a malformed fill quantity, an unstructured info block, and a
`fetch_order` that raises. All eight raise `OrderSubmissionAmbiguousError`
with `outcome=fill_ambiguous`; none returns a fill. The engine converts that
error into `state.set_state(PAUSED, operator_pause=True)`, so the trading loop
stops and requires an explicit operator resume rather than assuming an
exchange-side result.

### Batch 4 - demo promotion gate

- [ ] B4.1 Merge `ca4c734` and `af86ee7`
- [ ] B4.2 Fix `agent/deployment.py` `allow_shadow_strategy=True` on the
      exchange-facing path; every other caller of that flag is a research
      path that never reaches an exchange
- [ ] B4.3 Remove the mock-based receipt test superseded by the real-journal one
- [ ] B4.4 Extract the cross-module test fixture instead of instantiating
      another test class by name
- [ ] B4.5 Full suite green with the stricter flag
- [ ] B4.6 `--candidate-demo` fails closed and comprehensibly against an
      empty findings store

Gate: suite green with the stricter validation, and the failure path readable.

### Batch 5 - code freeze and G2 first contact

Operational, not a coding batch.

- [ ] B5.1 Freeze `agent/` and `main.py`
- [ ] B5.2 Run the demo agent until at least 100 `setup_proposed` events
- [ ] B5.3 `research.py replay --check-fidelity`
- [ ] B5.4 Resolve mismatches in `research/replay.py` only; a fix requiring
      `agent/` restarts the window
- [ ] B5.5 One clean PASS: zero missing, extra, malformed, duplicate; non-vacuous

Gate: a clean non-vacuous G2 PASS. This is the moment the stack becomes real;
nothing above layer 1 means anything until it happens.

### Batch 6 - collection and first promotion

- [ ] B6.1 Merge the integration branch to `main`
- [ ] B6.2 Run `research/nightly.sh` unattended
- [ ] B6.3 Hold the code freeze; any `agent/` change costs a feed fork
- [ ] B6.4 Collect until arms reach the paired-observation floor
- [ ] B6.5 `forward-qualify` reaches QUALIFIED
- [ ] B6.6 `prepare-review-artifacts` produces a draft packet
- [ ] B6.7 Human signs the packet with a reviewer and registry change ref
- [ ] B6.8 `run --candidate-demo` against a real demo account
- [ ] B6.9 Confirm the authorization receipt in the journal

Gate: a reviewed packet and a demo candidate run whose receipt is attributable.

## Human decision points that stay human

The loop is autonomous from journalling through hypothesis selection. Four
steps stay manual because an agent able to perform them is also able to
convince itself it should: signing the T3 packet, invoking `--candidate-demo`,
editing the registry or configuration, and promoting to live.

## Stop conditions

- G2 cannot pass after two honest attempts. That is a design question about
  exact symmetric matching, not a debugging one.
- The unreachable subsystem gains a writer before its keep/drop decision.
- Any `agent/` change becomes necessary after collection opens; the reset cost
  must be weighed deliberately rather than absorbed silently.
