# Review handoff: G1b_live_shadow_part2

Source: a review agent's final report, transcribed by the coordinator (the agent could not write files). It was re-run as a single agent after the workflow run hit the usage limit. The coordinator re-verified every HIGH finding against the code; see the end of each finding.

## Files read

| file | lines | read to end |
|---|---|---|
| research/live_shadow.py | 4150 to 8349 (4,200 of 8,349 lines), in 11 chunks | yes, through line 8349 |
| docs/review-2026-10-15/reviews/BRIEF.md, handoff-G1a_live_shadow_part1.md | full | yes |

Partial cross-check reads, cited where used:

- research/live_shadow.py (part 1, context only): 476-505, 618-690, 1153-1316, 2850-3092, 3219-3433, 3451-3498, 3911-4150
- research/factory_core.py: 482-494, 500-800, 1415-1545, 1619-1620
- research/ibr.py: 766-905, 1322-1443
- research/costs.py: 504-563
- research/live_shadow_ingest.py: 336-350, 876-925
- agent/engine_cycle.py: 715-840
- agent/contracts/ibr.py: 499-578, 712-780
- agent/startup_edge_policy.py: 518-528
- agent/market_entry_risk.py: 188-194
- agent/risk.py: 609-640, 784-812
- deploy/shadow.py: 160-249
- deploy/recorder_market.py: 125-128, 440-475, 505-540
- deploy/recorder.py: 1735-1800, 2528-2535
- compose.yaml: 306-375
- tests/research/test_shadow_replay_memo.py: 1-100, 296-340
- tests/research/test_shadow_poll_metrics.py: 1-90

Verification scripts are in [`verification/g1b/`](verification/g1b/). They use temporary directories, and nothing was written to the repository.

## What this part does (as implemented)

- **Replay memo and event cache (4163-4254).** The memo re-validates a process-local entry for each closed (candidate, session) pair. The cache holds decoded authorising event views under a fingerprint.
- **Diagnostic lane (4256-5159, 5298-5463, 6706-6908).** It is never gate-eligible. It:
  - preregisters a cohort and an activation watermark, and checks event provenance;
  - pages events per arm from a cursor, with sparse decisions;
  - keeps forward `DiagnosticAccountBook` accounts and runs close-only replays.
- **Overlays.** A stress overlay used only in the shadow (5161-5233), and a paired root-control arm `shadow:baseline:<id>` (5235-5270).
- **`_evaluate` (5617-6130), called once per bar event.**
  - The decision time is `event_at`, which is availability: `max(timestamp, as_of, observed_at)`.
  - Close-relative entry and force-flat cutoffs are applied (5701-5727).
  - The visible bar stream is bounded to the session open and close (5728-5745).
  - The rule or IBR signal is computed (5848-5889). Authorising arms keep no signal-session state.
  - A quote older than `max_market_data_age_seconds` gives 'unpriced' (5950-5960, 6066-6068).
  - `build_setup_plan` follows (6077-6082), then `RiskEngine.vet_open` on a fixed 100k equity with open-book state (6096-6118).
  - The result is 'open_incomplete' (6128-6130). There are no fills, exits or P&L.
- **`_replay` (6230-6626).**
  - The session window is built at 6170-6228. A session counts as complete once any bar ends at or after the close (6214-6217).
  - It runs `simulate_account` (rule arms) or `replay_ibr` plus `reprice_ibr_result` (IBR arms), each starting with 100k per session.
  - It runs a randomised null control and signs both sides.
  - It then writes, in this order: the replay diff (6555), the quarantine or repair (6568-6583), the evidence (6588), the null diff and evidence (6600-6620), and the book close (6625).
- **`_evaluate_arm_snapshot` (6644-6704).** The per-arm worker. It vetoes any symbol that already has an open book (6669-6675).
- **`run_once` (6910-8269).** One 1,360-line method. In order, it:
  1. snapshots the calendar;
  2. prepares the diagnostic cohort;
  3. reads the candidates;
  4. reads, compacts and ingests the corpus in one transaction, then saves the offsets (6946-7095);
  5. handles a separate no-candidate branch (7137-7304);
  6. decodes the whole WAL since the forward floor (7305-7345, 5465-5469);
  7. updates the session catalog (7351-7387);
  8. builds the gate pairs and validates the memo (7443-7515);
  9. applies the legacy and per-session bounds (7516-7606);
  10. writes the manifest (7646-7724);
  11. runs the diagnostic poll (7726);
  12. dispatches sessions one at a time: workers, then decisions, then replays (7755-8054);
  13. refreshes the memo (8055-8111);
  14. advances the floor, only if every condition holds (8127-8159);
  15. builds telemetry (8160-8269).
- **Module CLI (8272-8334).** Used only by tests.

### Answers to the assignment questions

1. **Parity.**
   - These match the runtime: bar availability, exclusion of partial bars, force-flat cutoffs and decision timestamps. The recorder's batch `observed_at` equals the replay's `signal_ready` (factory_core.py:482-486).
   - These differ: the first-signal rule (G1b-02), the tie-break between simultaneous signals (G1b-05), the cutoff minute (G1b-08) and capacity (G1a-03).
   - For historical sessions, the replay is not compared like with like. It is checked against fresh re-evaluations that start from later books (G1b-01).
2. **Timing.**
   - Live decisions have no look-ahead.
   - Session bounds use New York dates and the recorder's calendar, so DST and early closes are handled.
   - Look-ahead does enter the historical parity check, through future open books (G1b-01).
   - The cutoff minute differs from the runtime (G1b-08).
3. **Evidence integrity.**
   - Historical diffs are recomputed from hindsight state (G1b-01).
   - A failed null control is stored as a successful 'match' (G1b-06).
   - The diff, evidence and book close are still separate commits (G1a-05). In the current-session case this usually self-heals.
4. **Liveness.**
   - Ingestion wedges after a catch-up backlog (G1b-03).
   - The forward floor gets pinned, and per-poll work grows with history (G1b-04).
   - Every virtual open or close re-runs the candidate's history (G1b-01).
   - The diagnostic lane can stop the authorising lane (G1b-07).
5. **Over-engineering.** About 35% of this range serves a non-authorising lane (see below).

## Findings

### G1b-01 Historical gate sessions are re-checked against hindsight re-evaluations seeded with the candidate's current open books, and every open or close re-runs the candidate's whole history

- Severity: HIGH  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/live_shadow.py:4114-4119, 7466-7472, 7919-7921, 6669-6675, 7985-7986, 8013-8030, 3044-3055, 3941-3945, 121, 8106-8109`; `tests/research/test_shadow_replay_memo.py:301-336`
- Evidence:
  - 4114-4119: `# The legacy evaluator restores every current open book before` / `# processing a session. ...` / `"current_open_books": self.store.open_books(`
  - 7919-7921: `initial_states = { str(arm["candidate_id"]): self._portfolio_state( str(arm["candidate_id"])) for arm in active_arms}`
  - 7985-7986: `if is_replay_only:` / `replay_decisions[candidate_id] = decisions`
  - The test asserts this behaviour (329-331): `side_effect=AssertionError("future open book should suppress historical entry")`
- Why it matters:
  - **(a) The parity check does not use the live record.**
    - Every gate session is replay-only (7466-7472).
    - After a restart (the memo lives in process memory only, 3941-3945), or after a memo miss, `_replay` receives the decisions the worker has just recomputed (8013-8022). It does not receive the immutable decisions made live.
    - `store.decision` returns False for keys that already exist (2999-3002), so the recomputed decisions are not stored either.
    - The current code therefore re-certifies every historical session.
  - **(b) Look-ahead enters the parity check.**
    - The recomputation starts from `open_books(candidate)`, which is every open_incomplete book from any session (3052-3054).
    - A later session's book therefore vetoes the historical session's signals on the same symbol (6669-6675) and adds its notional to the gross cap. The replay knows nothing of that book.
    - A previously matched session becomes a 'mismatch'. It is quarantined (6570-6577), its diff is overwritten, the floor is pinned (G1b-04), and ingestion is blocked until the books close and another slow pass repairs it.
  - **(c) Cost.**
    - The memo key binds `current_open_books`, so every virtual open, and every book close after a session, invalidates the memo for all of that candidate's historical gate sessions.
    - Measured: one IBR arm re-evaluating a 390-bar symbol-session took 0.75 s (1.93 ms per event).
    - Estimated: about 18 s per 24-symbol session on a full core, and about 36 s at the 0.5 CPU limit (compose.yaml:369), before any replay. With 60 sessions of history, that is about 36 minutes per candidate per invalidation.
    - The memo holds at most 4,096 pairs, first in first out (121, 8106-8109). With up to 64 arms and more than 64 sessions of history, some sessions take the slow path on every poll.
    - The 03:00 research cycle reads the WAL and can see transient quarantines from slow passes that are still running.
- Suggested fix:
  - Always compare the replay with the persisted decisions (`decisions_for_session`).
  - If a re-evaluation under new code is wanted, store it as a separate, labelled re-certification.
  - Seed any re-evaluation with `virtual_book_state_through(candidate, session)` as it stood at that session, never with the current books.
  - Drop `current_open_books` from the memo key, and persist completed-replay identities in the WAL.
- How verified by reviewer:
  - `python (locked requirements) -I verification/g1b/exp1_future_book.py`. Output: `persisted decisions: [('SPY', 'no_trade', 'memo seed')]`; `poll 1 decisions given to _replay: [('2026-01-02', [('SPY', 'open_incomplete', 'virtual open')])]`; then, after adding a SPY open book dated 2026-01-03, `poll 2 _evaluate calls: 0` and `poll 2 decisions given to _replay: [('2026-01-02', [('SPY', 'no_trade', 'virtual book has an incomplete open')])]`.
  - `verification/g1b/exp5_eval_cost.py`. Output: `events=390 seconds=0.75 ms_per_event=1.93`.
- Coordinator verification: re-read 4110-4120, 7915-7923 and 8008-8032. For replay-only sessions the rows passed to `_replay` are the freshly recomputed `replay_decisions`, not `decisions_for_session`. Read the test at `tests/research/test_shadow_replay_memo.py:301-336`, which pins the future-book veto. Confirmed.

### G1b-02 The authorising lane does not apply the runtime's first-signal-per-symbol-session rule

- Severity: HIGH  |  Category: incorrect  |  Confidence: VERIFIED (semantics), LIKELY (frequency)
- Location: `research/live_shadow.py:5848-5889, 6669-6676, 6066-6068, 6081-6082, 6117-6118`; cross-group `agent/engine_cycle.py:743-746, 785-797, 799-826`, `research/factory_core.py:753-759, 829-971`, `research/ibr.py:835-878`
- Evidence:
  - live_shadow 5856-5857: `else:` / `session_state = None`
  - live_shadow 5884-5886 (diagnostic IBR only): `# Match runtime ordering: the first qualifying signal` / `# consumes the arm/symbol session before setup, risk,` / `# or executable-pricing refusal can occur.`
  - engine_cycle 743-745: `if signal_sessions.get(signal_state_key) == current_session:` / `self._event("rule_signal_duplicate_blocked", {`
  - factory_core 753-756: `elif resolved_policy.strict_market_data:` / `return _unpriced(... "no fresh equity quote at entry",`
- Why it matters:
  - The runtime persists the first signal per edge and symbol before any quote, setup or risk check, and blocks later signals in that session.
  - The replays behave similarly. The IBR replay takes the first admitted breakout, and later refusals end the symbol-session (`return refuse`). The rule replay ends the symbol-session on a missing fresh quote or a risk-geometry refusal.
  - The authorising shadow lane keeps no signal-session state for rule or IBR arms. It vetoes only when an open book exists.
  - So, after a first signal is refused live ('unpriced' for a stale quote, or 'reject' at setup or risk), a later bar's signal can open a position the runtime would never take.
  - Where the replay consumed the session, that open has no counterpart: the result is a 'missing' diff and a quarantine. The replay is deterministic, so the quarantine cannot be repaired, and it pins the floor for every candidate (G1b-04).
  - With the default IBR `stale_minutes` of 0.5 (agent/config.py:85), the next minute's breakout bar is the one re-tried.
- Suggested fix: keep a (candidate, symbol, session) marker in the authorising worker for both rule and IBR arms, as 5869-5887 already does for diagnostic IBR. Set it on the first generated signal, before quote, setup or risk checks, and seed it from the persisted decisions. Record later signals as 'no_trade: duplicate signal'.
- How verified by reviewer: read all three lanes. Not reproduced end to end, because that needs a re-firing family and a stale first quote. A-04 confirms that breakout predicates re-fire on consecutive bars.
- Coordinator verification: re-read live_shadow 5846-5892. Rule signals carry no session state, and IBR signals get `session_state = None` unless the candidate is diagnostic. Re-read engine_cycle 738-800: the runtime persists `signal_sessions[signal_state_key]` as soon as a signal is generated, before planning and risk. Confirmed.

### G1b-03 A catch-up backlog above 20,000 compacted rows wedges ingestion until it passes 64 MB, and the backlog is then discarded

- Severity: HIGH  |  Category: broken  |  Confidence: VERIFIED
- Location: `research/live_shadow.py:6973-7004, 6976-6988, 7086-7095, 1159-1197, 1260-1315`; `deploy/shadow.py:65`; `compose.yaml:314-337`
- Evidence:
  - 7001-7004: `selected = _compact_shadow_rows(pending)` / `if len(selected) > self.config.max_events:` / `raise ShadowError(` / `f"shadow event batch bound {self.config.max_events} exceeded")`
  - 7095: `self.store.save_source_offsets(next_offsets)`
- Why it matters:
  - `_read_corpus_append` reads from the saved offset to the end of the file, with no paging.
  - The bound check raises before any event or offset is committed, so every following poll re-reads the same bytes and raises again. Compose does not override `--max-events` (default 20,000).
  - At the recorder's 30-second cadence (deploy/recorder.py:2862, 2881-2882), compaction keeps every row: 72 rows per regular-hours minute. Any shadow outage longer than about 278 market minutes (4.6 hours) therefore leaves a lane that fails on every poll.
  - The wedge clears only when the pending bytes pass 64 MB, which takes about 11.5 trading days at roughly 15 KB per market minute (estimated from recorder-shaped rows). The skip path then sets the offsets to the end of the file, drops the backlog and quarantines through the current New York session (6981-6988).
  - A five-hour outage therefore becomes about two weeks without forward evidence, plus a lost backlog.
- Suggested fix: page the catch-up. Ingest at most `max_events` rows per poll, stop at a row boundary and commit that offset. Make the 64 MB skip an explicit operator action only.
- How verified by reviewer:
  - `verification/g1b/exp2_batch_wedge.py`, with max_events 100 and 150 bars. Output: `poll 1: ShadowError: shadow event batch bound 100 exceeded` and `offsets={} events=0`; polls 2 and 3 gave the same.
  - A synthetic 24-symbol run at 2 quotes per minute: `raw=4320 compacted=4320 per_minute=72.0 minutes_to_20000=278`.
  - `verification/g1b/exp6_row_bytes.py`: `bytes_per_rth_minute=15000 minutes_to_64MB=4474 trading_days=11.5`.
  - UNVERIFIED: the real VM row width.
- Coordinator verification: re-read 6970-7006 and 7086-7096. The raise comes before `save_source_offsets`. `DEFAULT_MAX_EVENTS = 20_000` (`:91`), `MAX_PENDING_CORPUS_BYTES = 64 * 1024 * 1024` (`:108`), and neither `compose.yaml` nor `deploy/shadow.py` overrides them. Confirmed.

### G1b-04 Any unresolved session pins the forward floor, after which per-poll work and memory grow with history

- Severity: HIGH  |  Category: broken  |  Confidence: VERIFIED (mechanism), LIKELY (time and memory extrapolation)
- Location: `research/live_shadow.py:8127-8159, 5465-5469, 3219-3223, 7323-7344, 7399-7408, 7466-7472, 7889-7891, 7990-8030, 8185-8190, 6214-6217`
- Evidence:
  - 8133-8138: `floor_eligible = bool(` / `frozen_session_events and not replay_only_sessions and` / `not replay_blocked and` / `... not invalid_sessions and not unknown_quarantine and` / `not pending_repairs)`
  - 7889-7891: `session_candidates = ({candidate_id for candidate_id, value in gate_pairs` / `if value == session}` / `if is_replay_only else arm_ids)`
- Why it matters:
  - The floor advances only when every loaded session is complete, replayed and unquarantined. Any of the following pins it for every candidate:
    - a pending replay quarantine, from G1a-03, G1b-01, G1b-02 or G1b-05 (the deterministic ones never clear);
    - a session that never gets a bar ending at the close (for example, a recorder outage spanning a close);
    - a failed arm.
  - While the floor is pinned:
    1. `_load_events()` re-decodes the whole WAL since the pin on every poll that ingested rows, because the cache fingerprint includes the event watermark. Measured: 56 µs and 2.6 KB per row, about 1.6 s and 73 MB per 28k-row session on a full core. Estimated under the compose limits (0.5 CPU, 2 GB): over 30 s per poll after ten sessions, and out of memory after about 25.
    2. Every complete, non-replay-only session since the pin is replayed on every poll, for every arm: candidates, root controls and the null.
    3. Lines 7469-7472 scan the event list once per gate pair, testing a condition that is always true for sessions in `session_inputs`. This costs O(sessions²) at about 2 µs per row.
    4. Separately, `store.decisions()` (8185-8190) loads every decision row on every poll only to compute dispositions.
- Suggested fix: advance a floor per settled session and track unresolved sessions separately. Never reload settled sessions, and replay only sessions that have new rows. Delete the redundant scan, and keep dispositions as an incremental rollup.
- How verified by reviewer:
  - `verification/g1b/exp3_floor_pin.py`, with 2026-01-02 incomplete and 2026-01-05 and 2026-01-06 complete. Output: `poll 1: replays=['2026-01-05'] events_loaded=13 floor=0.0`; `poll 2 (no new rows): replays=['2026-01-05'] events_loaded=13 floor=0.0`; `poll 3 (one more session): replays=['2026-01-05', '2026-01-06'] events_loaded=15 floor=0.0`; `poll 4 (no new rows): replays=['2026-01-05', '2026-01-06'] events_loaded=15 floor=0.0`.
  - `verification/g1b/exp4_load_memory.py` and `exp4b_load_time.py`: `retained_bytes_per_row=2610`, `per_row_us=55.7`, and `full membership scan ... (2.07 us/row)`.
- Coordinator verification: re-read 8125-8160. Any replay-only session, blocked replay, error, failed arm, invalid session, unknown quarantine or pending repair keeps the global floor in place. Confirmed.

### G1b-05 Simultaneous signals are admitted in hash order live, symbol order in the replay and universe order in the runtime

- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED (ordering), LIKELY (frequency)
- Location: `research/live_shadow.py:3222, 7399-7408, 7911-7913, 6665-6691`; cross-group `deploy/recorder_market.py:125-128, 440-442`, `research/factory_core.py:1619-1620`, `agent/market_entry_risk.py:188-194`
- Evidence:
  - 3222: `"SELECT * FROM events WHERE inserted_at>=? ORDER BY timestamp,event_key"`
  - recorder_market 127-128: `raw = "|".join((str(event_type), str(symbol).upper(), _value(timestamp)))` / `return hashlib.sha256(raw.encode("utf-8")).hexdigest()`
  - factory_core 1619-1620: `for raw in sorted(candidates, key=lambda item: (` / `item["entry_timestamp"], item["_symbol"], item["_day"])):`
- Why it matters:
  - The recorder stamps one `observed_at` for a whole bar batch (440-442), so signals in the same minute have identical decision and entry times.
  - Each lane breaks those ties differently:
    - the live worker books each open in SHA-256 `event_key` order;
    - the replay uses symbol order;
    - the runtime uses the configured universe order.
  - When the caps bind, the lanes pick different symbols. The 25% notional cap binds on every 30 bps stop, so the 50% gross cap admits two (fact 10). The result is 'missing' plus 'extra' rows and a quarantine (which feeds G1b-04), typically for same-minute breakouts across correlated ETFs.
- Suggested fix: use one explicit tie-break everywhere, preferably the runtime's universe order, and sort live events by (availability, tie-break) before admission.
- How verified by reviewer: read all three orderings and the recorder's stamping. Not reproduced.

### G1b-06 A failed null control is stored as a completed 'match' null session with no rows, which later blocks ingestion while health reports a clear tail

- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED (path), SPECULATIVE (frequency)
- Location: `research/live_shadow.py:6316-6317, 6431-6452, 6507-6528, 6600-6620, 3451-3498, 8161-8163`; cross-group `research/live_shadow_ingest.py:336-350, 884-887`
- Evidence:
  - 6448-6451: `except Exception as exc:` / `details["null_control"] = False` / `details["null_error"] = (`
  - 6606-6610: `replay_digest=null_digest, status="match",` / `details={"complete": True, "signature_match": True,` / `... "null_control": True, "replay_signatures": [],`
  - ingest 344-345: `if not session_rows:` / `return [], f"session {day} has no parity-matched gate rows"`
- Why it matters:
  - When `null_control_account` raises, `null_rows` stays empty while `replay_ok` remains True.
  - `_replay` then writes a 'match' diff for `shadow:null:<id>` that claims `null_control: True`, with an account showing zero P&L and no trade rows.
  - No quarantine is written, so `stale_tail` stays 'clear'.
  - Ingestion reports 'null_incomplete' for the candidate on every cycle, and nothing retries the null until the session is replayed again.
  - A failed control is therefore recorded as a successful one, and no repair item is shown.
- Suggested fix: on failure, write the null diff as 'incomplete' with the error and quarantine the session, so that the repair path and the health check both report it.
- How verified by reviewer: read both branches, the store write and the ingest reader. An exception was not forced.

### G1b-07 Uncapped diagnostic pending sessions share one 200k context bound, and its error stops the authorising lane

- Severity: MEDIUM  |  Category: broken  |  Confidence: LIKELY
- Location: `research/live_shadow.py:4558-4570, 4840-4868, 2441-2443, 3259-3278, 6271-6280, 7726-7730`
- Evidence:
  - 4568-4570: `context_rows = self.store.events_for_sessions(` / `sorted(sessions),` / `max_events=self.config.diagnostic_session_max_events)`
  - 3275-3277: `if len(rows) > int(max_events):` / `raise ShadowError(`
- Why it matters:
  - A diagnostic session leaves 'pending' only when its `_replay` returns complete.
  - Exceptions raised before `_replay`'s try block (in `cost_resolver_setup` or the shadow policy) are caught at 4866, which leaves the session pending. Pending sets merge with no cap (2441-2443).
  - All pending sessions load in one query against one 200,000-row bound, so about seven 28k-row sessions make it raise.
  - `_run_diagnostic_poll` runs before the authorising dispatch, with no handler (7726-7730). A non-authorising arm can therefore stop the authorising evidence for every candidate.
  - UNVERIFIED: whether any diagnostic arm raises deterministically. That depends on the measured-cost configuration (research/quote_costs.py:490-530).
- Suggested fix: bound the context per session, cap the pending sessions, and wrap the diagnostic poll in a handler that degrades only the diagnostic report.
- How verified by reviewer: read the code. Not reproduced.

### G1b-08 The runtime admits the cutoff minute, but the shadow and both replays refuse it

- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/live_shadow.py:5706-5718`; cross-group `agent/startup_edge_policy.py:518-528`, `research/factory_core.py:648-649`, `research/ibr.py:876-878`
- Evidence:
  - 5710: `latest_entry_reached = event_at >= latest_at`
  - 5713-5714 (diagnostic IBR only): `# The live IBR gate compares at minute precision and permits` / `# the configured cutoff minute itself.`
  - startup_edge_policy 528: `return local.time().replace(second=0, microsecond=0) <= cutoff`
- Why it matters:
  - The runtime trades the bar that ends at the cutoff, which it observes seconds later. The authorising shadow and both replays refuse that bar.
  - The shadow and the replay agree, so no quarantine results. But forward evidence systematically omits the runtime's last eligible bar in each symbol-session.
  - The special case exists only for diagnostic IBR arms, and there it disagrees with ibr.py:876-878.
- Suggested fix: choose one rule, either minute-inclusive or instant-exclusive, and apply it in the runtime, in `_evaluate` and in both replays.
- How verified by reviewer: read all four checks.

## Over-engineering

Range totals: 4,200 lines, 56 definitions, 15 `_digest(` calls, 15 broad `except Exception` handlers and 45 `time.monotonic()` calls.

| item | lines | notes |
|---|---|---|
| Diagnostic (non-authorising) lane inside ShadowRunner | 4256-5159 (904, including `_diagnostic_coverage` at 276), 5298-5463 (166), 6706-6908 (203), plus about 190 in `_evaluate` branches | About 1,460 lines, roughly 35% of this range. Every output says `gate_eligible: False` (4354-4356, 5065-5066). It runs first in each poll and can stop the authorising lane (G1b-07) |
| `run_once` | one method, 6910-8269 | Includes a 168-line no-candidate branch (7137-7304) that duplicates the manifest, telemetry and result building (7646-7724, 8160-8269) |
| Memo and cache machinery | about 313 lines: 4163-4254, 7305-7345, 7443-7515, 7839-7888, 8055-8111 | Exists to avoid re-replaying history that the design itself re-replays. Invalidated by every open or close, and capped at 4,096 (G1b-01) |
| ThreadPoolExecutor | 4756-4785, 7924-7945 | Used for CPU-bound pure-Python work under the GIL with a 0.5 CPU limit, so it gives no speed-up. It needs a thread-local `_worker_state` with six attributes, set and deleted in three places (6661-6702, 6750-6906) |
| Per-event recomputation in `_evaluate` | 5637-5663, 5893-5895, 5728-5745 | Rebuilds the config, session close, `ReplayPolicy.from_config` and stress telemetry for every bar event, and filters the symbol's whole bar list each time. That is O(bars²) per symbol-session (measured at 1.93 ms per event) |
| `_compact_shadow_rows` | in part 1 | Has no effect at the production 30-second cadence (exp2: 4,320 rows in, 4,320 out) |
| Module CLI and `run_shadow_once` | 8272-8334 | Used only by tests/research/test_live_shadow_cadence.py. It diverges from deploy/shadow.py: default corpus `data.csv` instead of `market.csv`, an interval of 60 s instead of 30 s, and no `--max-events`, `--max-decisions` or `--retention-days`. It builds a new ShadowRunner on every poll (8319), which would empty the memo and cache each time |
| Duplicate telemetry keys | 8245-8246, 5131-5135, 8172/8257, 8192-8193/8259-8260 | `candidates` equals `authorizing_candidates`, and `health_cursor_matches` equals `all_arms_caught_up`. `replay_quarantine`, `signal_dispositions` and `stress_calibration` appear both in `stale_tail` and at the top level |

## Cross-group questions

Answers to G1a's questions:

1. **The legacy bound is confirmed.**
   - `legacy_replay_context` is true if any arm lacks `session.require_exact_calendar` (7537-7538), not only the replayed arms.
   - The aggregate over every replay-only session is checked against 200,000 before any work (7574-7579).
   - With an empty memo after a restart, every poll raises until `--replay-session-max-events` is raised (the maximum is 1,000,000, about 35 sessions). Because of G1b-01 this also happens after every open or close.
   - UNVERIFIED: whether configs stored in the ledger carry the flag. agent/config.py:214-215 defaults it to true when a config is normalised.
2. **The catch-up wedge is confirmed** (G1b-03).
3. **There is no start-up sweep.** `gate_pairs` come from the shadow_trades join (3410-3417).
   - A crash between the diff and evidence commits during a current-session replay usually self-heals: the floor advances only after a successful poll (8149-8159), so the session is reloaded and replayed.
   - G1a-05's permanent wedge remains for one case: a historical re-replay that produces a new replay_digest (after a code change) and crashes after the diff commit.
4. **Whole-history re-replays do not fit within poll time** (see the measurements in G1b-01).

Questions for other groups:

- **F1 (factory_core.py).** Confirm the first-admissible semantics: `continue` at 612-711 against `return` at 753-759 and 829-971, compared with the runtime's consume-on-first-signal behaviour (engine_cycle.py:743-797).
- **A_signals.** Which rule families re-fire on consecutive bars in a session? This sets the frequency of G1b-02.
- **H1a (recorder).**
  - Confirm the real row width and quote cadence behind the 64 MB estimate in G1b-03.
  - The single `observed_at` per bar batch (recorder_market.py:440-442) is what makes the G1b-05 ties certain.
- **D2/F3 (ingest).** The 'null_incomplete' handling in G1b-06. Can the 03:00 ingest see transient G1b-01 quarantines?
- **Deploy (SPECULATIVE).** deploy/README.md:484 repairs with `docker compose run --rm --no-deps shadow --once` without stopping the running service.
  - That puts two writers on one WAL. The replay-quarantine metadata is updated by read-modify-write without a spanning transaction (2902-2927), so updates can be lost.
  - A fresh `--once` run also starts with an empty memo, so it re-evaluates all history against the current books (G1b-01).
- **B (runtime).** The shadow passes empty cooldowns and no `daily_pnl` to `vet_open` (6111-6114), so post-loss cooldowns and the daily loss limit never bind in the shadow. Confirm whether this is moot under first-signal-per-session and about 0.075% risk per trade.

## Summary

- **HIGH G1b-01.** Historical sessions are re-checked against fresh re-evaluations, not against the persisted live decisions. Those re-evaluations start from the candidate's current, later open books.
  - Reproduced: a SPY book dated 2026-01-03 turned the 2026-01-02 decision passed to the replay from 'open_incomplete' into 'no_trade: virtual book has an incomplete open'.
  - Every open or close re-runs the candidate's whole history, at an estimated 36 s per arm-session at 0.5 CPU.
- **HIGH G1b-02.** The authorising lane lacks the runtime's first-signal-per-symbol-session rule.
  - A refused 'unpriced' or 'reject' signal does not consume the session, so the shadow can open trades the runtime never takes.
  - Where the replay consumed the session, the result is a deterministic quarantine that cannot be repaired.
- **HIGH G1b-03.** The catch-up ingest is unpaged.
  - A backlog of more than 20,000 compacted rows (about 4.6 market hours) raises on every poll until the pending bytes pass 64 MB (about 11.5 trading days). The backlog is then discarded.
  - Reproduced at small scale.
- **HIGH G1b-04.** Any quarantine, never-completing session or failed arm pins the forward floor for everyone.
  - Per-poll decoding (2.6 KB and 56 µs per row), replays of every session for every arm, and an O(sessions²) scan then grow with history.
  - Reproduced: the replay list grows by one session per day.
- **MEDIUM G1b-05.** Simultaneous signals are admitted in SHA-256 key order live, symbol order in the replay and universe order in the runtime. When caps bind, the lanes choose different symbols and mismatch.
- **MEDIUM G1b-06.** A failed null control is stored as a 'match' null session with no rows. Ingestion then blocks with 'null_incomplete' while the health check reports a clear tail.
- **MEDIUM G1b-07.** Uncapped diagnostic pending sessions share one 200k bound, and its error escapes `run_once`, stopping the authorising lane.
- **LOW G1b-08.** The runtime admits the cutoff minute, but the shadow and both replays refuse it.
- **Sound in this range.**
  - Live decisions are causal: availability-based streams and no partial bars.
  - Force-flat and close-relative cutoffs match the replay.
  - Session handling is correct for DST and early closes.
- **Over-engineering.** About 35% of the range (about 1,460 lines) serves the non-authorising diagnostic lane, and about 313 lines of memo machinery exist mainly to make the design's history re-replays affordable.
