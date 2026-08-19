# 05 — Live-Shadow Lane, Edge Ledger, Proof & Authorization Chain

Repo: `/home/user/alpaca-trading` @ `claude/trading-strategy-audit-zveg57`
Scope: `research/live_shadow.py`, `research/live_shadow_ingest.py`, `research/edge_ledger*.py`,
`research/factory_ledger.py`, `agent/edge.py`, `agent/registry.py`, `agent/startup_edge_policy.py`,
`agent/governance.py`, `deploy/shadow.py`.
Method: static read + executed probes (stdlib only). Repo tree unmodified.

Taken as given (not re-derived): system has never run; backfilled rows invisible to replay
(`deploy/backfill.py:222`, `research/market_data.py:203-226`); floors 100/100/150 are ~40x
underpowered vs the declared 0.05R minimum useful edge.

---

## 1. THE EPISTEMIC QUESTION — what the shadow lane actually establishes

### 1.1 CONFIRMED: the shadow lane calls the offline backtester directly

`research/live_shadow.py:40`
```
from research.factory_core import simulate_account
```
`research/live_shadow.py:41`
```
from research.ibr import IBRConfig, replay_ibr
```

Call sites inside `ShadowRunner._replay`:
- `research/live_shadow.py:1444` — `account = simulate_account(...)` for rule candidates
- `research/live_shadow.py:1398` — `result = replay_ibr(...)` for IBR candidates

The offline factory backtest uses the identical function at `research/strategy_factory.py:1244`,
`:1282`, `:1310`, `:1321`, `:2360`, `:2377`. `research/factory_core.py:630` is the single
definition.

The repo's own test states it:
`tests/research/test_live_shadow.py:645` — `def test_rule_replay_uses_factory_account(self)`, which
patches `research.live_shadow.simulate_account` wrapping `factory_simulate_account` and asserts
`factory.called` (`:654-657`).

**There is no second simulator. The "live shadow" and the "backtest" are the same fill engine
run over the same recorded CSV rows.**

### 1.2 What the two sides of "parity" actually are

| side | producer | what it emits |
|---|---|---|
| "runtime shadow" | `ShadowRunner._evaluate` (`live_shadow.py:1140-1276`) | a *plan*: symbol, direction, stop/target/range, timestamps. **No fill, no exit, no P&L** (`live_shadow.py:7-8`, `:1274` `"virtual open; fills and P&L incomplete"`) |
| "replay" | `simulate_account` / `replay_ibr` (`live_shadow.py:1398,1444`) | full trades with entry/exit prices, costs, net_pnl, r_multiple |

`_evaluate` is fed from the recorder corpus (`bars`, `quotes`, `options` loaded by `_load_events`,
`live_shadow.py:1103-1127`), not from a broker. It calls the same primitives the trader would
(`generate_ibr_signal` / `generate_rule_signal` at `:1186-1188`, `build_setup_plan` at `:1248`,
`RiskEngine.vet_open` at `:1266`). There is no order, no venue, no queue, no reject.

So the "parity check" is: *incremental replay of recorded bars* vs *batch replay of the same
recorded bars*, comparing **only pre-trade geometry**.

`_signature_diffs` (`live_shadow.py:315-339`) compares exactly:
`symbol, session_date, direction, setup_type, signal_ts, decision_ts, entry_ts, stop_price,
target_price, stop_distance, range_high, range_low, target_r, vehicle, profile`.

It does **not** compare: quantity, entry fill price, exit price, exit reason, slippage, cost,
net_pnl, or r_multiple — because the runtime side never produces any of them.

### 1.3 What the gates then consume

`ShadowStore.record_replay_evidence(..., trades=evidence_rows, ...)` (`live_shadow.py:1509-1515`)
where `evidence_rows` is the `simulate_account` output (`:1452-1453`) or the IBR opportunity rows
(`:1403`). `ShadowStore.gate_rows` (`:916-939`) hands exactly those rows to
`ShadowIngestor._rows_for` (`live_shadow_ingest.py:205-219`), which feeds `_discover_gate`
(`live_shadow_ingest.py:529, 587`). Every p-value, expectancy, drawdown, lower confidence bound
and FDR decision in the authorizing proof is computed on **backtester output**.

### 1.4 Statement — CONFIRMED

**What the shadow lane DOES establish:**
1. Out-of-time-sample: the tail sessions are strictly after the fit/qualification boundary
   (`live_shadow_ingest.py:367,373` — see §4). Real and non-trivial.
2. Point-in-time signal availability for the *decision*: `_evaluate` gates every bar on
   `_row_visible` = `max(timestamp, as_of, observed_at) <= event_at`
   (`live_shadow.py:104-124, 1175-1181`). This catches look-ahead in feature construction.
3. Determinism/consistency: the incremental and batch code paths agree on the signal geometry.
4. Statistical machinery re-runs cleanly on the newer window.

**What it DOES NOT establish, at all:**
1. Fill realism — no queue position, no partial fills, no rejects, no cancels, no market impact,
   no adverse selection. `simulate_account` fills from bar OHLC + a recorded quote through a
   static cost model (`research/factory_core.py:630-720`, `research/costs.py`).
2. That the strategy makes money in a live market. Every P&L number in the "live" proof is a
   simulated number produced by the same engine that produced the backtest P&L.
3. Anything about the broker, the order lifecycle, or latency. `deploy/shadow.py` is explicitly
   broker-free, by design.

**Therefore: the "parity-matched live-shadow tail" is a walk-forward backtest with a
determinism assertion bolted on. It is a strictly weaker claim than "the strategy works
out-of-sample in a live market", and the parity component in particular proves only that two
implementations of the same replay agree on pre-trade geometry.**

Note: the parity assertion is not worthless — it is a real regression guard against the
incremental evaluator drifting from the batch one. It is simply not evidence about markets, and
it is not what the docs sell it as.

### 1.5 CONFIRMED overclaiming doc sentences

`README.md:22`
> "…and the exact parity-matched live-shadow run that **authorizes deployment**."

`README.md:93-97`
> "A backtest pass is still not deployable. Offline historical or forward replay may persist a
> passing `lane=shadow` candidate proof, but that status is **stability evidence only and never
> authorizes the runtime**. A strictly newer recorder tail must be evaluated by the broker-free
> ShadowRunner, then consumed by `edge ingest-shadow` as a complete parity-matched proof before the
> candidate can become `validated` or `champion`."

This sentence draws a bright line between "offline replay" and "the live lane" that does not exist
in code: both sides of the line call `research/factory_core.py:630`. The only differences are
(a) which rows are in the window and (b) that the shadow lane additionally runs an incremental
consistency check on the *signal*, not the *outcome*.

`research/protocol.md:214-223`
> "Passing gates advance `candidate` -> `backtest_passed` -> `shadow` only; **runtime entries stay
> blocked because backtest or offline forward-shadow evidence cannot authorize paper deployment.**"

The evidence that *does* authorize paper deployment is produced by the same backtest engine.
"Cannot authorize" is a provenance rule, not an epistemic distinction.

`research/protocol.md:425-432`
> "`edge ingest-shadow` is **the sole authorization boundary**… requires strictly newer complete
> parity-matched rows…"

`ARCHITECTURE.md:438-439`
> "…a strictly later offline forward-shadow pass through the same gates as a deterministic one.
> That pass may leave the candidate at `shadow`; **live parity** [is what promotes it]."

`ARCHITECTURE.md:26`
> "…record whose latest shadow proof carries the **parity-matched live-ingestion marker**."

`research/protocol.md:440-442`
> "It **compares runtime shadow semantic signatures with factory/IBR replay signatures for
> parity**; only the research consumer can append the authorization marker."

This last one is the most accurate sentence in the docs — it says exactly what is compared
("semantic signatures") — but it sits inside a section that elsewhere calls the result a proof
that the edge is deployable. Nowhere do the docs say that the shadow lane's fills and P&L are
backtest fills and P&L. Recommended honest phrasing: *"walk-forward replay on newly recorded
bars, with an incremental/batch signal-consistency assertion."*

### 1.6 HIGH — the parity assertion does not cover the numbers it authorizes

Because parity compares only geometry, a bar that arrives late (or is backfilled) and changes the
*exit* but not the *entry signal* passes parity and silently contaminates the P&L that the gate
then tests.

Worse, the two sides use **different visibility rules**:

- incremental (`live_shadow.py:1175-1181`) filters bars by `_row_visible(row, event_at)` —
  the full `max(timestamp, as_of, observed_at)` point-in-time rule.
- batch replay (`live_shadow.py:1331-1340`) filters bars **only** by session-close:
  ```
  in_session_bars = [row for row in session_bars
                     if calendar_close is None or (
                         (_timestamp(row.get("timestamp")) or calendar_close) < calendar_close
                         and (_event_end(row) or calendar_close) <= calendar_close)]
  ```
  `observed_at` is never consulted.

`research/factory_core.py` does not import `record_is_available` at all (only `research/ibr.py`
and `research/costs.py` do — `costs.py:1114` applies it to *quotes*). So for the rule lane the
authorizing bar stream is not point-in-time gated, while the thing it is "parity-matched" against
is. **CONFIRMED.**

---

## 2. AUTHORIZATION CHAIN — trace and short-circuits

### 2.1 The chain

```
edge_lab / strategy_factory
  register_candidate                          edge_ledger.py:66      status=candidate
  append_run(lane="backtest") + trades
  record_verified_gate                        edge_ledger_proof.py:200
  transition -> backtest_passed               edge_ledger.py:273 (requires passing backtest gate)
  [offline forward replay]
  transition -> shadow                        (requires passing *shadow*-lane gate)
        |
ShadowRunner.run_once                         live_shadow.py:1545
  _evaluate  per bar per candidate            live_shadow.py:1140   -> decisions / virtual_books
  _replay    per session per candidate        live_shadow.py:1312   -> simulate_account / replay_ibr
       writes replay_diffs(status)            live_shadow.py:805
       writes shadow_accounts / shadow_trades live_shadow.py:822
  gate_rows exposes rows only when            live_shadow.py:916-930
       replay_diffs.status='match' AND shadow_trades.replay_status='match'
        |
ShadowIngestor.ingest / _one                  live_shadow_ingest.py:792 / :349
  boundary = _latest_boundary                 :367  (max fit_end/heldout_end/qualification sessions)
  available = sessions strictly > boundary    :373
  _split_sessions -> selection | confirmatory :377
  both windows must meet 150 trades/30 sess   :441-470
  selection gate -> raw p -> family+global BH :529-538, :826-848
  confirmatory gate on newer half             :587-595
  FactoryLedger.record_fdr_decision (LORD)    :614
  append_run(lane="shadow", metrics=...)      :708
  append_trade x confirmatory rows            :723
  record_verified_gate                        :728
  append_evidence(kind="shadow_ingestion")    :748   <-- THE MARKER
  transition -> shadow -> validated           :752-765
        |
EdgeLedger.transition("validated"/"champion") edge_ledger.py:301-315
  requires _live_shadow_authorized(run)       edge_ledger_proof.py:655-919
        |
agent/edge.py::_eligible                      agent/edge.py:156-200
  ledger.eligibility(cid, lane="shadow")      edge_ledger_proof.py:631-653
     -> latest_verified_run  (re-verifies gate from durable trades)
     -> _live_shadow_authorized
  + proof.config_hash == candidate.config_hash          agent/edge.py:190-195
  + _runtime_identity_matches (assumptions hash)        agent/edge.py:123-153, 196-198
        |
Engine._refresh_edge  every cycle             agent/startup_edge_policy.py:310-415
  (engine_cycle.py:246, :271)
        |
submitted order
```

### 2.2 Short-circuit enumeration

**(a) `edge promote` — BLOCKED. Doc claim holds.**
`research.py:415-423` `cmd_edge_promote` calls `ledger.transition(...)` with no bypass.
`edge_ledger.py:307-315` unconditionally applies `_live_shadow_authorized` for
`validated`/`champion`. `rollback=True` is explicitly refused at `:284-285`.
Verified by probe: `transition -> validated` on a candidate with a hand-written `shadow_source`
and a hand-written `shadow_ingestion` evidence row still fails.

**(b) Config pin — BLOCKED. Doc claim holds.**
`agent/edge.py:293-342` `resolve_pinned_variants` routes through the same `_eligible`
(`:325`), which routes through `ledger.eligibility(..., lane="shadow")` (`:174`).
`agent/governance.py:116-183` only validates the pin's *shape*. A pin selects; it does not
authorize. `README.md:249-253` is accurate here.

**(c) `append_evidence(kind="shadow_ingestion")` — NOT GATED. CRITICAL.**
`edge_ledger.py:233-236`:
```
if str(kind) == "verified_gate":
    raise ValueError("verified_gate evidence must be recorded through record_verified_gate")
```
Only `verified_gate` is protected. **Probe result:**
```
append_evidence('verified_gate')    -> blocked
append_evidence('shadow_ingestion') -> ALLOWED, evidence_id 60c4e9e75d96
```

**(d) Run metrics are entirely caller-supplied. CRITICAL.**
`edge_ledger.py:151` — `payload = dict(metrics or {})`. The ledger stamps only
`replay_engine_epoch` (`:156`). So `metrics["shadow_source"]`, `metrics["replay_digests"]`,
`metrics["selection_sessions"]`, `metrics["gate"]` are whatever the caller passes.
Probe: `append_run(lane="shadow", metrics={"shadow_source": <fabricated>})` succeeded and the
ledger stamped `replay_engine_epoch: 4` onto it.

**(e) Trades are entirely caller-supplied. CRITICAL.**
`edge_ledger.py:187-231` `append_trade` validates only that `net_pnl`/`return_value` are finite
numbers and the vehicle matches. Probe: `append_trade(run, {"net_pnl": 9999.0, "r_multiple": 50.0,
"symbol": "FAKE", ...})` accepted.

**=> Combined (c)+(d)+(e): the entire 265-line `_live_shadow_authorized` check
(`edge_ledger_proof.py:655-919`) and the entire `record_verified_gate` re-verification
(`edge_ledger_proof.py:200-578`) validate the *internal consistency of caller-supplied data
against other caller-supplied data*. There is no signature, no key, no external anchor, and no
binding to the recorder corpus or the shadow WAL. Anyone (or any buggy code path) that can write
`edge_lab.sqlite3` — which is the same file the research process writes — can mint a
fully-re-verifying live-shadow marker from invented trades by calling four public methods in
order: `append_run` → `append_trade` × N → `record_verified_gate` → `append_evidence`.**

The README sentence `README.md:257-259`
> "Manual `edge promote` and offline replay cannot manufacture the live-shadow marker."

is true of the `edge promote` **CLI**, and false of the **ledger API**. The docs present it as a
cryptographic-style guarantee; it is a call-ordering convention.

**(f) "Legacy" row migration — behaves as documented, with one wrinkle.**
`live_shadow_ingest.py:291-298`: `validated`/`champion` candidates that are *already* eligible are
skipped; ones that are not are re-ingested. But `_one` at `:750-765` only transitions from
`backtest_passed`/`demoted`/`shadow`. A legacy `validated` row therefore has a real marker appended
**without ever passing through `shadow`** — it goes from unauthorized-`validated` to
authorized-`validated` with no lifecycle event. Auditable via the run/evidence rows, but the
`events` table shows nothing. LOW.

**(g) Replay-epoch handling — sound but stateless.**
`edge_ledger_proof.py:12-41`: runs with no `replay_engine_epoch` report epoch 1; authorization
requires an exact match with `REPLAY_ENGINE_EPOCH = 4` (`edge_ledger_store.py:50`). This is a
correct fail-closed quarantine. **But** it has no lifecycle representation: bumping the constant
instantly de-authorizes every deployed edge while `EdgeLedger.status()` (`edge_ledger.py:364`)
still reports them as `champion`/`validated`. `ledger.eligibility()` is the only place the truth
appears (`edge_ledger_proof.py:646-653`). MEDIUM — see §6.

**(h) Hash re-verification comparing the wrong thing / being skipped.**
Two real instances:

- `edge_ledger_proof.py:282-284` — the cross-check against the run's immutable recorded gate is
  conditional:
  ```
  if isinstance(recorded_envelope, Mapping):
      if recorded_envelope.get("content_hash") != envelope.get("content_hash"):
          return "verified gate envelope does not match immutable run proof"
  ```
  If `metrics["gate"]` is absent, the check is silently skipped. It is only made mandatory at
  `:285-289` when `source_backed_v2 and run_engine_epoch_current(run)`. A run at a stale epoch —
  precisely the case where you most want the binding — skips it.

- `edge_ledger_proof.py:338-345` — the shadow-source→run identity check is conditional on the
  field existing at all:
  ```
  if shadow_source is not None:
      if (... shadow_source.get("candidate_id") != run.get("candidate_id")):
  ```
  Absent `shadow_source` passes. (`_live_shadow_authorized` catches it later, but
  `record_verified_gate` does not.)

**(i) `select_champion` can leave two champions. MEDIUM.**
`edge_ledger.py:462-472`: the new champion is transitioned first (`:464`), then previous champions
are demoted to `validated` in a loop (`:469-472`). That loop calls `transition(..., "validated")`,
which requires `_live_shadow_authorized` (`:312-313`). If a previous champion's proof has since
gone stale (epoch bump, corrupted evidence row), the loop raises `ValueError` **after** the new
champion was already committed → two `champion` rows and an exception out of `select_champion`.
No transaction wraps the two writes.

---

## 3. PROOF INTEGRITY

### 3.1 Coverage — GOOD, this part is sound

`research/gates.py:1600-1646` builds `body` containing every decision-relevant field: `counts`,
`fit_source`/`heldout_source`/`fit_baseline_source`/`heldout_baseline_source`/`null_source`
(the complete raw rows), `floors`, `fill_quality`, `authorization_projection`, `control`,
`statistics`, `performance`, `falsification`, `separation`, `walk_forward`, `retirement`,
`qualification`, `risk_unit_report`, `cost_stress`, `effective_breadth`, `null_control`,
`online_fdr`, `provenance`, `candidate_id`, `checks`, `passes`. Then
`gates.py:1647` — `return {**body, "content_hash": _content_hash(body)}`.

`verify_gate_envelope` recomputes it: `gates.py:2181` (in the final boolean)
`envelope.get("content_hash") == _content_hash(body)` where `body` excludes `content_hash`
(`gates.py:1734`). It additionally *rebuilds* the code-owned decision flags from the persisted
source rows (`gates.py:2008-2037`) and re-runs the control test, falsification, walk-forward and
bootstrap (`:2079-2176`). **The hash is not decorative and the checks are not summary-only.**

### 3.2 Serialization-dependent hashing — CONFIRMED, MEDIUM

`edge_ledger_store.py:55-65`:
```
def canonical_json(value): return json.dumps(value, sort_keys=True, separators=(",",":"),
                                             ensure_ascii=False, allow_nan=False, default=str)
def content_hash(value):
    streamed = getattr(value, "content_hash", None)
    if callable(streamed): return str(streamed())
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
```

**Executed probe results — two materially different payloads, one digest:**

| construction | same digest? | Python `==`? |
|---|---|---|
| `{"sessions": "2026-01-05"}` vs `{"sessions": date(2026,1,5)}` | **YES** | no |
| `{"p_value": Decimal("0.04")}` vs `{"p_value": "0.04"}` | **YES** | no |
| `("a","b")` vs `["a","b"]` | **YES** | **no** |
| `{1:"x"}` vs `{"1":"x"}` | **YES** | no |
| any object with `__str__` returning the target string | **YES** | no |

`default=str` collapses every non-JSON type onto its string form. The tuple/list case is the
practically dangerous one: `content_hash(tuple) == content_hash(list)` while `tuple != list`, so
code that checks a digest **and** an equality on the same value can disagree with itself. The
codebase does exactly that in several places, e.g. `edge_ledger_proof.py:690-698`:
```
if (selection.get("sessions") != selection_sessions or
        source.get("selection_session_digest") != content_hash(selection_sessions) or ...
```

**Same payload, two digests (flakiness):**
- `content_hash(-0.0) != content_hash(0.0)` (`"-0.0"` vs `"0.0"`)
- `content_hash(0.1+0.2) != content_hash(0.3)`
- `content_hash(1) != content_hash(1.0)`

So a float that reaches `0.0` by one arithmetic path and `-0.0` by another (trivially achievable
for `net_pnl`, `mean_delta`, `realized_pnl` in a no-trade session) changes the proof hash. JSON
round-tripping through SQLite is stable (Python float `repr` round-trips), so this only bites when
a producer and a verifier compute the number differently — but `verify_gate_envelope` does exactly
that at `gates.py:2008-2037` and only tolerates it because `_close_number` is used for the
statistical fields, not for the hash.

**Digest escape hatch — `edge_ledger_store.py:62-64`.** Any object exposing a callable
`content_hash` attribute *returns its own digest*. Probe: an object returning
`"deadbeef"*8` yields exactly that as its "content hash". This is presumably for streamed
datasets, but it means the hashing primitive is not a pure function of the value.

### 3.3 What the hash does NOT bind

The gate hash binds the gate to its own contents and to the durable trades. It does **not** bind
either to the recorder corpus, the shadow WAL, or any external artifact. `source_digest` and
`shadow_digest` exist inside the shadow WAL (`live_shadow.py:1341-1355, 805-820`) but are only
carried forward as opaque strings — `_live_shadow_authorized` checks that
`metrics["replay_digests"]` equals the list inside `metrics["shadow_source"]["sessions"]`
(`edge_ledger_proof.py:710-714`), i.e. one caller-supplied copy against another caller-supplied
copy. Nothing recomputes a digest over recorded market data.

### 3.4 Redundancy without independence

Nine fields (`independent_confirmatory`, `disjoint_sessions`, `session_disjoint`,
`selection_sessions`, `confirmatory_sessions`, `selection_session_digest`,
`confirmatory_session_digest`, `p_value_source`, `selection_raw_p_value`/`confirmatory_raw_p_value`)
are written into **five** separate structures by `ShadowIngestor._one` — `source`
(`live_shadow_ingest.py:475-519`), `online` (`:618-633`), `run_provenance` (`:638-664`),
`hashes` (`:671-682`), and `evidence` (`:729-747`) — and then cross-checked five ways by
`_live_shadow_authorized` at `:670-673`, `:832-851`, `:852-870`, `:893-912`. Every comparison is
between two copies written by the same 400-line function in the same process. Grep counts
(non-test): `p_value_source` 13 sites, `selection_session_digest` 12, `independent_confirmatory`
12, `selection_raw_p_value` 11, `disjoint_sessions` 10, `session_disjoint` 10.

---

## 4. STRICTLY-NEWER TAIL

### 4.1 CONFIRMED: the ordering constraint is real and correct at session-date granularity

`live_shadow_ingest.py:149-162`:
```
def _latest_boundary(ledger, candidate_id):
    values = []
    for run in ledger.runs(candidate_id):
        for key in ("fit_end", "heldout_end"):
            if run.get(key): values.append(str(run[key]))
        gate = (run.get("metrics") or {}).get("gate")
        ... values.extend(qualification.get("sessions"))
    return max(values) if values else None
```
`live_shadow_ingest.py:367-376`:
```
boundary = _latest_boundary(self.ledger, candidate_id)
if prior is None or boundary is None: return {... "no_prior_proof" ...}
available = sorted({_session(row.get("session_date")) for row in metadata
                    if _session(row.get("session_date")) > boundary})
```

The boundary is the max over **every persisted run** of fit_end, heldout_end, and the sealed
qualification session list. Strict `>`. **No overlap with fit or with the sealed window.**

### 4.2 Boundary condition probes — executed

| case | session | boundary | accepted |
|---|---|---|---|
| same session date | `2026-01-05` | `2026-01-05` | **no** ✓ |
| next day | `2026-01-06` | `2026-01-05` | yes ✓ |
| same day vs ISO datetime boundary | `2026-01-05` | `2026-01-05T15:59:00+00:00` | **no** ✓ (prefix < full string) |
| next day vs ISO datetime boundary | `2026-01-06` | `2026-01-05T15:59:00+00:00` | yes ✓ |
| unpadded date | `2026-1-06` | `2026-01-05` | yes — **LOW**, lexicographic accident |

`append_run` derives the bounds from `session_date or entry_timestamp` (`edge_ledger.py:160-166`),
so a run whose rows carry only `entry_timestamp` produces an ISO-datetime boundary; the prefix
rule above still rejects the same day. **No leakage found. Partial sessions are excluded because
the boundary is a whole date and the comparison is strict.**

### 4.3 The one leak that is real: `qualification` is copied forward verbatim

`live_shadow_ingest.py:520-524`:
```
previous_run, previous_gate = prior
qualification = previous_gate.get("qualification")
if not isinstance(qualification, Mapping) or not qualification.get("available"): return ...
```
and it is passed straight into both new gates (`:534`, `:592`).

So six of the 29 `GATE_REQUIRED_CHECKS` in the *authorizing live-shadow proof* —
`qualification_available`, `qualification_net_positive`, `qualification_delta_positive`,
`qualification_floor_adequate`, `qualification_confidence_supported`,
`qualification_drawdown_supported` (`gates.py:55-60`) — are recomputed from the **backtest-era
sealed window**, not from the shadow tail. Two more are structurally free in the shadow lane:
- `gates.py:1487-1489` — `derived["fit_delta_positive"] = bool(lane == "shadow" or ...)` — hardcoded True.
- `edge_discovery_core.py:1037-1039` — for `shadow=True` the fit floor is built with
  `required=not shadow` = False, and `edge_ledger_proof.py:393` makes `adequate = True` when
  `required` is False. So `fit_floor_adequate` passes on **zero** fit trades
  (`edge_discovery_core.py:1012-1016` sets `fit = []` for shadow).

**8 of 29 required checks in the "live" proof carry zero live-shadow information.** HIGH.

### 4.4 Timezone / session-key inconsistency — MEDIUM

- `live_shadow.py:1149-1152` (`_evaluate`): `market_at = _timestamp(event.get("timestamp") or event.get("as_of"))`, session = NY date of that.
- `live_shadow.py:1625-1628` (`run_once.row_session`): `stamp = _timestamp(row.get("as_of") or row.get("timestamp"))`, session = NY date of that.

Opposite precedence. For any event whose `timestamp` and `as_of` fall on different NY dates
(a bar recorded across the 00:00 ET boundary, or a delayed `as_of`), the decision is filed under
one session and the replay input under another → guaranteed parity mismatch for that session →
the whole tail is blocked (§4.5).

### 4.5 One bad session blocks the entire tail, permanently — HIGH

`live_shadow_ingest.py:392-398`:
```
# One incomplete/mismatched session blocks the complete tail.
rows, reason = _rows_for(self.store, candidate_id, available, vehicle)
if reason: return {... "status": "incomplete" ...}
```
`available` is *every* session strictly after the boundary. There is no quarantine-and-skip. A
single session with `status != 'match'` (`_meta_by_session`, `live_shadow_ingest.py:192-200`)
blocks ingestion forever, because the boundary never advances and the bad session never leaves
`available` — unless it is pruned (§4.6), which also removes the good sessions.

---

## 4b. CRITICAL — tuned rule candidates can never be ingested

`_paired_id` (`live_shadow_ingest.py:302-337`) returns `f"shadow:baseline:{candidate_id}"` as the
paired root control for any non-root rule candidate. That control is produced by
`ShadowRunner._rule_root_control` (`live_shadow.py:1066-1101`), which returns a synthetic record
with `candidate_id = f"shadow:baseline:{cid}"` (`:1098`).

`run_once` replays it at `live_shadow.py:1679-1683`:
```
root_control = self._rule_root_control(candidate)
if root_control is not None:
    self._replay(root_control, session, session_bars, session_quotes,
                 self.store.decisions(str(root_control["candidate_id"])), session_options)
```

**`store.decisions("shadow:baseline:...")` is always empty** — `_evaluate` is only ever called for
real ledger candidates (`live_shadow.py:1651-1673`), never for the synthetic control. So inside
`_replay`, `shadow_signatures = []` while `replay_signatures` = the root rule's replay trades.

`_signature_diffs([], [<one trade>])` returns one `"extra"` difference (verified by probe), so
`status = "mismatch"` (`live_shadow.py:1491-1492`). `gate_rows` filters on `d.status='match'`
(`live_shadow.py:928`), so `_rows_for(baseline_id, ...)` returns
`"session X has no parity-matched gate rows"` → `_one` returns `status: "control_incomplete"`
(`live_shadow_ingest.py:407-410`).

**The paired root control can only be "parity matched" on sessions where the root rule takes no
trade at all. Any session in which the baseline trades poisons the whole tail. The autonomous
rule-factory lane — the main strategy discovery path — therefore has no reachable route to
`validated`.** CRITICAL.

Contrast the null arm, which is special-cased to always pass:
`live_shadow.py:1523-1529` writes the null diff with `status="match"` and
`shadow_digest=_digest([])` **unconditionally**. The null arm's parity requirement is vacuous;
the baseline arm's is impossible. HIGH (both).

IBR is unaffected: `ibr.baseline` is a real ledger candidate that `_read_candidates` includes
unconditionally (`live_shadow.py:961`), so it gets real decisions and a real parity check.
However when the candidate under test *is* `ibr.baseline`, `_paired_id` returns itself
(`live_shadow_ingest.py:311-314`) → `_discover_gate(rows, rows)` → all deltas zero →
`heldout_delta_positive` false → the baseline can never validate but still consumes a slot in the
family/global BH correction (`live_shadow_ingest.py:826-848`). MEDIUM.

---

## 4c. HIGH — the shadow lane cannot accumulate a 60-session tail

Three independent mechanisms:

**(1) `_split_sessions` needs 60 distinct sessions at once.**
`live_shadow_ingest.py:117-133` — `if len(ordered) < 2 * required: return [], []` with
`required = min_sessions = 30` (`DEFAULT_MIN_SESSIONS = 30`, `:35`). Probe:

| tail sessions | selection | confirmatory | usable |
|---|---|---|---|
| 29 | 0 | 0 | no |
| 30 | 0 | 0 | no |
| 59 | 0 | 0 | no |
| 60 | 30 | 30 | **yes** |

Plus 150 trades in **each** window (`live_shadow_ingest.py:441-470`) → ≥300 shadow trades.
60 trading sessions ≈ 84 calendar days.

**(2) Retention deletes the evidence.**
`live_shadow.py:941-946`:
```
def prune(self):
    floor = time.time() - max(1, self.retention_days) * 86400
    db.execute("DELETE FROM replay_diffs WHERE created_at < ?", (floor,))
```
`DEFAULT_RETENTION_DAYS = 14` (`live_shadow.py:57`, `deploy/shadow.py:37`). `prune()` runs at the
end of **every** `run_once` (`live_shadow.py:1684`). Both `replay_metadata` (`:890-898`) and
`gate_rows` (`:924-930`) read through `replay_diffs`, so a pruned session is invisible to ingest.
14 days of retention vs 84 calendar days of required tail — **a 70-day deficit.**

The deficit is masked while the process runs continuously, because `run_once` re-`_replay`s every
session still present in the `events` table on every 60-second cycle and `replay_diff` UPSERTs
`created_at=time.time()` (`live_shadow.py:812-820`). But:

**(3) `quarantine_through_session` permanently removes sessions from re-replay.**
`live_shadow.py:1579-1590` — if pending corpus bytes exceed `MAX_PENDING_CORPUS_BYTES` (64 MB),
the runner baselines the offsets and writes `quarantine_through_session = today`. Then
`live_shadow.py:1636-1638`:
```
if session is not None and (quarantine_through is None or session > quarantine_through):
    session_events.setdefault(session, []).append(event)
```
Every session at or before the quarantine date is excluded from `session_events` forever, so it is
never re-replayed, its `created_at` never refreshes, and 14 days later `prune` deletes it. **One
oversized recorder backlog permanently truncates the tail below the 60-session floor.**

## 4d. HIGH — the shadow loop does not scale to its own floor

`run_once` (`live_shadow.py:1651-1683`) is:
```
for candidate in candidates:            # up to max_candidates = 32
    for session in sorted(session_events):   # every session ever ingested
        for event in session_events[session]:
            ... self._evaluate(candidate, event, bars, quotes, options)
        rows = self.store.decisions(cid)     # loads ALL decisions, inside the session loop
        self._replay(candidate, session, ...)         # full simulate_account
        self._replay(root_control, session, ...)      # another full simulate_account
```
`_load_events` (`:1103-1127`) loads **every event ever ingested** into memory each cycle;
`events` is append-only with immutability triggers (`:589-592`) and `ingest_event`'s `max_events`
parameter (`:611`) is accepted and **never used** in the body — the table is unbounded.

At the required tail size (60 sessions × 32 candidates × 2 replay arms) that is 3,840 full-session
`simulate_account`/`replay_ibr` calls per 60-second poll, plus `_evaluate` over all events for all
candidates, plus an O(sessions × decisions) `store.decisions(cid)` reload. The loop cannot complete
within its own poll interval well before the 60-session floor is reachable.

---

## 5. PROMOTION vs DEMOTION ASYMMETRY

### 5.1 Promotion cost

- ≥60 shadow sessions, ≥300 shadow trades, ~84 calendar days
  (`live_shadow_ingest.py:34-36, 117-133, 441-470`)
- Capital at risk: **zero** (broker-free).

### 5.2 The four demotion paths

| path | code | requirement |
|---|---|---|
| rolling-R guard | `edge_ledger.py:719-722` | 20 paper outcomes, `sum(last 20 R) <= -2.0` (`PAPER_DEMOTION_MIN_OUTCOMES=20`, `PAPER_DEMOTION_R_FLOOR=-2.0`, `edge_ledger_store.py:51-52`) |
| SPRT drift | `edge_ledger.py:520-546` | ≥20 outcomes, log-LR ≥ `PAPER_DRIFT_THRESHOLD = 4.0` (`edge_ledger.py:31`) |
| trial park | `research/trial.py:174-181` | ≥30 sessions **and** ≥100 paper trades before any verdict (`trial.py:38-39`), then `total_r <= 0` |
| retirement | `edge_ledger.py:324-343` | a **new failing** verified gate with adequate fit *and* heldout floors, `heldout_net_pnl <= 0`, `heldout_expectancy <= 0`, `rejects_minimum_useful_edge`, `multi_window_negative` |

### 5.3 Quantified — sessions and equity at delivered risk 0.075%/trade

Implied trade rate from the shadow floor is 150 trades / 30 sessions = 5 trades/session.

| true mean R | first guard | trades to stop | sessions to stop | equity loss |
|---|---|---|---|---|
| −0.50 | rolling-R | 20 | 4 | −0.75% |
| −0.25 | rolling-R | 20 | 4 | −0.38% |
| −0.10 | rolling-R | 20 | 4 | −0.15% |
| **−0.05** | trial park (rolling-R never fires: 20×−0.05 = −1.0 > −2.0) | **100** | **20** | **−0.38%** |
| −0.02 | trial park | 150 | 30 | −0.23% |
| 0.00 | trial park | 150 | 30 | 0.00% |

SPRT drift alone (reference mean 0.10R, sd 0.50R) needs ~200 zero-edge trades; at reference 0.05R
it needs ~800. It is never the binding constraint.

**Honest verdict on the framing:** the *unpinned* demotion is not catastrophic — 4 to 30 sessions,
≤0.75% of equity. Demotion is in fact **faster in sessions than promotion** (4–30 vs 60). The
prompt's framing that demotion is far harder than promotion is **not supported** for the
`demoted` transition. Three real asymmetries do exist:

**(a) CRITICAL — the documented promotion path disables every automatic demotion.**
`README.md:245-255` instructs the operator to paste a `strategy.pinned` block after a successful
trial. Once pinned:
- rolling-R guard → `guard_alert` event only, **no transition** (`edge_ledger.py:741-748`)
- SPRT drift → same branch, notify only
- trial park → skipped entirely (`research/trial.py:159-163`, `review["action"] = "none_pinned"`)

**A pinned losing edge has no automatic stop at any loss rate.** The only remaining controls are
the runtime risk limits (`daily_loss_limit_pct: 2.0`, `max_open_risk_pct: 2.0`, `config.yaml:42-43`),
which are per-day/per-book caps, not an edge-level kill switch. The docs are explicit that this is
intentional (`README.md:232` — "**Pinned** … **you only** … **no**"), but the risk is real: the
recommended way to promote an edge is also the way to remove its safety net.

**(b) HIGH — `retired` is far harder than `demoted`, and `demoted` is reversible.**
`edge_ledger.py:286-298`: `demoted -> {shadow, retired}`. A demoted edge can re-enter
`shadow -> validated` on a newer proof (`edge_ledger.py:316-323` only requires the shadow run to
post-date the demotion event). Retirement requires ≥200 trades and ≥60 sessions of fresh replay
producing a *powered failing* gate whose 95% clustered upper bound is ≤ 0.05R
(`gates.py:498-567`, `RETIREMENT_MIN_USEFUL_R = .05`, `RETIREMENT_MIN_SESSIONS = 30`) plus ≥2
negative walk-forward folds (`gates.py:1953-1960`). So a losing edge is parked, not killed, and
can cycle back into deployment. The factory slot it occupies is never freed.

**(c) MEDIUM — a failing offline replay silently blinds the paper guards.**
Appending a newer failing shadow run makes `_trial_epoch_state` return `(None, True)`
(`edge_ledger.py:596-602`), which makes `_paper_r_history` and `paper_performance` return **zero
rows** (`edge_ledger.py:551-552`, `:772-774`). The candidate then reports `outcomes: 0` and the
trial verdict is permanently `"running"` (`trial.py:70-75`). It also becomes ineligible, so it
stops trading — fail-closed on the trading side, but the dashboard shows a healthy idle edge
rather than a de-authorized one.

---

## 6. STATE MACHINE

### 6.1 Statuses — from code

`research/edge_ledger_store.py:17-19`:
```
LIFECYCLE = ("candidate", "backtest_passed", "shadow", "validated", "champion",
             "retired", "demoted")
```
That is the complete set. **`parked` and `pinned` are not ledger statuses.**
- "parked" is a `research/trial.py` verb that performs `transition(..., "demoted")` (`trial.py:177`).
- "pinned" is a `config.yaml` `strategy.pinned` entry (`agent/governance.py:116-183`); it is a
  *selection* mechanism plus a `frozen` flag passed into `ingest_paper_outcome`
  (`agent/edge.py:468`, `edge_ledger.py:631, 741`).

Separate, non-overlapping state sets exist in `research/factory_ledger.py:19-23`
(`ACTIVE_HYPOTHESIS_STATES | {"validated","retired"}`) for the hypothesis slots — different
namespace, no direct coupling to the candidate lifecycle except a one-way `event()` write at
`live_shadow_ingest.py:778-782`.

### 6.2 Real transition graph (edge_ledger.py:286-323)

```
                    +--- (failing powered gate) ---> retired [TERMINAL, no exit]
                    |
candidate ----------+--- (passing BACKTEST gate) ---> backtest_passed
                                                          |
                          (passing SHADOW-lane gate) -----+---> shadow ---> retired
                                                                  |
                          (passing shadow gate                    |
                           + _live_shadow_authorized) ------------+---> validated
                                                                  |         |
                                                                  +-> demoted
                                                                            |
   validated <---> champion      (both require _live_shadow_authorized)     |
        |              |                                                    |
        +-> demoted <--+                                                    |
             |                                                              |
             +--- (newer verified shadow run) ---> shadow  <----------------+
             +--- (failing powered gate) -------> retired
```

Guards (`edge_ledger.py:301-323`):
- `-> backtest_passed` : latest verified passing **backtest** gate
- `-> shadow`          : latest verified passing **shadow**-lane gate; if from `demoted`, the run must post-date the demotion event
- `-> validated`       : latest verified passing shadow gate **+ `_live_shadow_authorized`**
- `-> champion`        : same
- `-> retired`         : latest gate `passes is False` + fit/heldout floors adequate + `heldout_net_pnl <= 0` + `heldout_expectancy <= 0` + `rejects_minimum_useful_edge` + `multi_window_negative`

### 6.3 Findings

**Terminal state with no exit — `retired`.** `edge_ledger.py:297` — `"retired": set()`. Combined
with `register_candidate` returning the *existing* row for a repeated `(variant_id, vehicle)`
(`edge_ledger.py:92-101`) and refusing a changed config (`:100`), a retired variant identity is
permanently unusable. There is no rehabilitation path even if the retirement was caused by a data
bug. MEDIUM.

**Transitions that skip evidence:** none found. Every promoting transition is gated, `rollback` is
explicitly refused (`edge_ledger.py:284-285`), and `demoted` (the only guard-driven transition)
correctly has no evidence requirement. This part is well built.

**Effectively unreachable in the shipped code:**
- `shadow -> validated` for rule candidates — blocked by §4b (control always mismatched).
- `shadow -> validated` for anything — blocked by §4c (60-session tail unreachable under the
  14-day retention + quarantine truncation, and the loop does not scale to it).
- `-> retired` from `validated`/`champion`: reachable in principle
  (`strategy_factory.py:2645-2647`, `edge_lab.py:762-764`) but only through
  `edge_lab.py:763` which maps `validated`/`champion` to **`demoted`**, not `retired`. The only
  producers of `-> retired` target `candidate`/`backtest_passed`. **No code path retires a
  deployed edge.**

**States with no representation:** epoch quarantine (§2.2g). A candidate whose proof was written
under `REPLAY_ENGINE_EPOCH < 4` stays `validated`/`champion` in `candidate_state` and in
`EdgeLedger.status()` while being unauthorized everywhere that matters. `eligibility()` surfaces it
as `{"quarantined": true}` (`edge_ledger_proof.py:652-653`) but nothing reconciles the status row.

**Partial-failure hole:** `select_champion` (§2.2i) — two `champion` rows are reachable.

---

## 7. OVER-ENGINEERING VERDICT

~6,500 lines of ledger/proof/identity have authorized zero trades, and — per §4b and §4c — cannot
authorize one as shipped.

### 7.1 Ceremony: deletable with no behavioral loss

| lines | location | what it is |
|---|---|---|
| **265** | `research/edge_ledger_proof.py:655-919` `_live_shadow_authorized` | ~40 field-equality assertions comparing five copies of the same nine facts, all written by `ShadowIngestor._one` moments earlier in the same process. Lines **832-912** (81 lines) are literally the same nine assertions three times over `gate["provenance"]`, `gate["online_fdr"]`, and `evidence["run_provenance"]`. Without a signature or an external anchor this is a spell-check, not a proof. **A single `if evidence_row_exists_and_hash_matches` plus the session-disjointness check (`:678-689`) would have identical security properties in ~25 lines.** |
| **112** | `research/live_shadow.py:228-339` `_shadow_signature` / `_replay_signature` / `_signature_diffs` | the parity machinery. Real value as a regression guard, **zero** value as the market evidence the docs claim. Keep, but stop calling it authorization. |
| **22** | `research/live_shadow.py:1516-1537` | the null-control duplicate write, which hardcodes `status="match"`. Actively harmful: it makes one of the two control arms unfalsifiable. **Delete.** |
| **161** | `research/live_shadow_ingest.py:475-519` + `:634-749` | five parallel serializations of the same nine fields (`source`, `online`, `run_provenance`, `hashes`, `evidence`). One canonical dict + one hash would do. |
| **101** | `research/edge_ledger.py:759-859` `paper_performance` + `paper_report` | pure reporting; 1 non-test caller (`research.py:456`). Not wrong, just not infrastructure. |
| **10** | `research/live_shadow.py:871-880` `ShadowStore.replay_accounts` | **dead** — 0 non-test callers. |
| **20** | `agent/edge.py:83-102` `_latest_passing_proof` | **dead** — 0 non-test callers; superseded by `_eligible`/`eligibility`. |
| **76** | `deploy/shadow.py:21-96` | duplicates `research/live_shadow.py:1697-1720` `main()` plus a heartbeat file. |
| **~651** | | non-nested total |

### 7.2 What is *not* ceremony (keep)

- `research/gates.py:1400-1647` `verified_gate_envelope` and `:1654-2183` `verify_gate_envelope` —
  genuinely recompute the decision from persisted source rows. This is the one part of the proof
  system that would catch a real regression.
- `edge_ledger.py:273-356` `transition` — the guard table is correct and rollback is refused.
- `edge_ledger_proof.py:98-194` `_trade_rows_match` / `_durable_trade_columns_match` — real
  multiset comparison between envelope sources and durable rows.
- `research/edge_identity.py` (135 lines) — one function, load-bearing for the `config_hash`
  identity that binds runtime config to the proof.
- `live_shadow_ingest.py:117-133` `_split_sessions` and `:367-376` boundary — the actual
  independence mechanism.

### 7.3 The structural diagnosis

The system spends ~1,500 lines proving that data it just wrote is consistent with itself, and
zero lines proving that the data came from anywhere real. There is no signature over the recorder
corpus, no key, no append-only external log, no cross-process attestation. `edge_lab.sqlite3` is
writable by the same process that produces the evidence, so the threat model the proof chain
defends against (a caller assembling an inconsistent proof) is strictly weaker than the threat
model it appears to defend against (a caller assembling a *false* proof), and the latter is
trivially achievable with four public method calls.

Meanwhile the parts that would actually make the shadow lane mean something — a separate fill
model, queue simulation, reject/partial handling, or an actual broker paper connection — do not
exist, and the docs describe the absence as a virtue ("broker-free").

---

## FINDINGS BY SEVERITY

### CRITICAL
1. **§4b** Tuned rule candidates can never be ingested. `shadow:baseline:{cid}` control has no
   decisions (`live_shadow.py:1679-1683` vs `:1651-1673`) → `_signature_diffs([], trades)` always
   mismatches (`:315-339, 1491`) → `gate_rows` empty (`:928`) → `control_incomplete`
   (`live_shadow_ingest.py:407-410`). CONFIRMED by probe.
2. **§2.2c-e** The live-shadow marker is forgeable through the public ledger API.
   `append_evidence` gates only `verified_gate` (`edge_ledger.py:235`); `append_run` metrics
   (`:151`) and `append_trade` payloads (`:187-231`) are caller-controlled. CONFIRMED by probe.
   `README.md:257-259` is true of the CLI, false of the API.
3. **§5.3a** Pinning — the documented promotion path (`README.md:245-255`) — disables the rolling-R
   guard (`edge_ledger.py:741-748`), the SPRT drift guard (same branch) and the trial park
   (`trial.py:159-163`). A pinned losing edge has no automatic stop.
4. **§1** The shadow lane is the backtester. `live_shadow.py:40, 1398, 1444` +
   `strategy_factory.py:1244` + `tests/research/test_live_shadow.py:645`. "Parity" is a determinism
   assertion over pre-trade geometry, not evidence of live performance.

### HIGH
5. **§4c** 60-session tail unreachable: 14-day `replay_diffs` retention (`live_shadow.py:57,
   941-946, 1684`) vs ~84 calendar days required (`live_shadow_ingest.py:35, 117-133`);
   `quarantine_through_session` (`live_shadow.py:1589, 1636-1638`) permanently truncates sessions.
6. **§4d** `run_once` re-evaluates and re-replays every session × every candidate every poll
   (`live_shadow.py:1651-1683`); `_load_events` loads all events each cycle (`:1103-1127`);
   `ingest_event`'s `max_events` is never applied (`:611`). Does not scale to its own floor.
7. **§1.6** Parity covers only geometry; the batch replay ignores `observed_at` for bars
   (`live_shadow.py:1331-1340`) while the incremental side enforces it (`:1175-1181`).
   `factory_core.py` never imports `record_is_available`.
8. **§4b** Null-control diff hardcoded `status="match"` (`live_shadow.py:1523-1529`) — the null
   arm's parity requirement is vacuous.
9. **§4.3** 8 of 29 `GATE_REQUIRED_CHECKS` carry no live-shadow information: `fit_delta_positive`
   hardcoded (`gates.py:1487`), `fit_floor_adequate` free (`edge_discovery_core.py:1037-1039` +
   `edge_ledger_proof.py:393`), 6 `qualification_*` copied from the prior backtest gate
   (`live_shadow_ingest.py:520-524, 534, 592`).
10. **§4.5** One mismatched session blocks the entire tail forever
    (`live_shadow_ingest.py:392-398`); no skip/quarantine.
11. **§5.3b** No code path retires a deployed edge; `edge_lab.py:763` maps
    `validated`/`champion` → `demoted`, and `demoted -> shadow -> validated` is a live cycle.

### MEDIUM
12. **§2.2i** `select_champion` can leave two champions and raise mid-loop (`edge_ledger.py:462-472`).
13. **§2.2h** Conditional hash cross-checks: `edge_ledger_proof.py:282-289` skips the
    immutable-run-proof binding when `metrics["gate"]` is absent or the epoch is stale;
    `:338-345` skips the shadow-source identity check when the field is absent.
14. **§3.2** `content_hash` type-collapse via `default=str` (`edge_ledger_store.py:55-58`):
    tuple≡list, `Decimal("0.04")`≡`"0.04"`, `date`≡its ISO string, `{1:x}`≡`{"1":x}`. Digest-and-
    equality checks on the same value can disagree (`edge_ledger_proof.py:690-698`). Plus the
    `content_hash` duck-typing escape hatch at `edge_ledger_store.py:62-64`. All CONFIRMED by probe.
15. **§4.4** Session-key precedence inconsistency: `timestamp or as_of` (`live_shadow.py:1149`)
    vs `as_of or timestamp` (`:1626`).
16. **§2.2g / §6.3** Epoch quarantine has no lifecycle representation; `status()` still reports
    `champion` for de-authorized candidates.
17. **§4b** `ibr.baseline` is its own paired control (`live_shadow_ingest.py:311-314`) → zero
    deltas → can never validate, but still consumes a BH family slot.
18. **§6.3** `retired` is terminal and `register_candidate` refuses a changed config
    (`edge_ledger.py:100`) → a retired variant identity is permanently dead.
19. **§5.3c** A failing offline replay zeroes `paper_performance` (`edge_ledger.py:596-602,
    772-774`) and freezes the trial verdict at `"running"`.
20. Repeated selection-window testing: `ingest()` recomputes `_discover_gate` on a growing
    selection window every research cycle (`live_shadow_ingest.py:820-865`) with fresh BH each
    time. LORD covers only the single confirmatory spend (`:614`); the adaptive selection stage
    is an unbounded garden of forking paths.

### LOW
21. Lexicographic boundary compare accepts unpadded dates (`live_shadow_ingest.py:373`).
22. Dead code: `ShadowStore.replay_accounts` (`live_shadow.py:871-880`),
    `agent/edge.py::_latest_passing_proof` (`:83-102`).
23. `deploy/shadow.py:21-96` duplicates `live_shadow.py:1697-1720`.
24. Legacy `validated` rows gain the marker without a lifecycle event
    (`live_shadow_ingest.py:750-765`).
