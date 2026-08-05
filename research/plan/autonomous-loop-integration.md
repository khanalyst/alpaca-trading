# Autonomous loop integration

Merging the G2/evidence/promotion work and the audit-remediation work into one
branch that can run unattended for weeks and arrive at a decision worth a
human's attention.

Integration branch: `integration/autonomous-loop`, cut from `main` at
`c017bd1` and tagged `pre-integration-20260805`. Source branches are read-only
inputs and are never modified.

## The governing constraint

`agent/state.py::code_fingerprint` hashes every `.py` file under `agent/` plus
`main.py`, and it is one of three fields in an assignment's `code_identity`
alongside `forward_model_id` and `forward_model_assumptions_hash`. The paired
`config_identity` carries `variant_definition_hash`, `strategy_config_version`
and `experiment_config`.

What a runtime-code change actually costs is narrower than "resets the
evidence", and the distinction decides the batch order:

- completed outcomes are unaffected. `WORKED`, `FAILED` and `INCONCLUSIVE`
  rows are protected by SQL immutability triggers and no code change can
  alter them;
- an assignment still collecting is rejected. On restart the coordinator
  compares both bundles and calls `reject_experiment_assignment` with
  "code/config identity changed before assignment completed", so its partial
  paired observations stop counting toward a verdict;
- variants the loop generates for itself change nothing. A new hypothesis, a
  newly registered variant, and a candidate rotating to the next setting are
  all new immutable identities by construction.

The ceiling is therefore one in-flight assignment per realtime lane, once:
at the configured floors, up to ten elapsed days and 100 paired observations.
Only a human editing runtime source triggers it; the autonomous loop never
does.

The batch order below still settles runtime code before opening a long
collection window, because paying that cost once at the start is free while
paying it on day nine is not.

G2 is the exception that makes this workable: its commit touches only
`research/`, `research.py`, docs and tests, so it can be merged early and
fixed repeatedly at no cost to evidence identity.

### Fork scope: decided, not narrowed

`strategy_config_version` is already scenario-scoped. It hashes only `llm`,
`strategy`, `universe`, `cycle`, `risk`, `execution` and `trading_costs`, so
an alerts webhook or a findings-store path deliberately does not fork it.

`code_version` is deliberately coarser than it needs to be: editing a log
message forks identity exactly as rewriting the risk engine does. That
over-inclusion is retained on purpose. A narrower hash would need an
allowlist, and a wrong exclusion pools incomparable evidence silently, which
is the one failure mode this system refuses everywhere else. Over-inclusion
costs an occasional assignment; under-inclusion costs a conclusion that looks
sound and is not.

The operating rule is a cadence rather than a prohibition: land runtime
changes in the window just after a rotation completes, so a fork costs a fresh
assignment instead of a nearly finished one, and accept the one-lap cost
deliberately when a real fix cannot wait. Splitting the hash into a
decision-bearing set and a recorded-but-not-identity-bearing set stays
available if that cadence ever proves insufficient; it is not worth building
before then.

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

- [x] B4.1 Cherry-pick `ca4c734` and `af86ee7`
- [x] B4.2 Fix `agent/deployment.py` `allow_shadow_strategy=True` on the
      exchange-facing path; every other caller of that flag is a research
      path that never reaches an exchange
- [x] B4.3 Remove the mock-based receipt test superseded by the real-journal one
- [x] B4.4 Extract the cross-module test fixture instead of instantiating
      another test class by name
- [x] B4.5 Full suite green with the stricter flag
- [x] B4.6 `--candidate-demo` fails closed and comprehensibly against an
      empty findings store

Gate: suite green with the stricter validation, and the failure path readable.

B4.2 evidence. The relaxation disables three checks: the analyst-contract
requirement, `cycle.timeframes` covering the strategy's required timeframes,
and the signal timeframe appearing in that list. Applying a variant that drops
`15m` from `cycle.timeframes` is rejected by ordinary validation with
"cycle.timeframes must include 15m, 1h, 4h ... (missing: 15m)" and was
accepted under the relaxation, which then handed that config to a real
order-placing Engine. The reviewed-candidate path now validates exactly as
strictly as `main.py run`.

B4.6 evidence. Missing arguments exit 2 naming each one. With all four
supplied and no findings store, authorization stops before any exchange or
model client is constructed: first on the account fingerprint, then, once that
matches, on "authoritative findings DB is missing: ...". Both exit 1 with the
reason on the final line.

### Batch 5 - integrate to main, then G2 first contact

Operational, not a coding batch.

The merge to `main` leads this batch rather than closing the sequence. A G2
pass has to be produced by the tree that will keep running, so the corpus must
come from the merged code; collecting it from the integration branch and
merging afterwards would invalidate the pass it was collected for.

- [x] B5.1 Merge `integration/autonomous-loop` into `main`
- [ ] B5.2 Deploy `main` and start the demo agent
- [ ] B5.3 Collect at least 100 `setup_proposed` events. Risk vetoes roughly
      four fifths of proposals, so this is a matter of days
- [ ] B5.4 `research.py replay --check-fidelity`
- [ ] B5.5 Resolve mismatches in `research/replay.py` where possible; a fix
      that needs `agent/` costs the in-flight assignments and is cheap now
      precisely because nothing has accumulated yet
- [ ] B5.6 One clean PASS: zero missing, extra, malformed, duplicate; non-vacuous

Gate: a clean non-vacuous G2 PASS. This is the moment the stack becomes real;
nothing above layer 1 means anything until it happens.

### Batch 6 - collection and first promotion

- [ ] B6.1 Run `research/nightly.sh` unattended
- [ ] B6.2 Land runtime changes in the window after a rotation completes
      rather than mid-assignment
- [ ] B6.3 Collect until arms reach the paired-observation floor
- [ ] B6.4 `forward-qualify` reaches QUALIFIED
- [ ] B6.5 `prepare-review-artifacts` produces a draft packet
- [ ] B6.6 Human signs the packet with a reviewer and registry change ref
- [ ] B6.7 `run --candidate-demo` against a real demo account
- [ ] B6.8 Confirm the authorization receipt in the journal

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
