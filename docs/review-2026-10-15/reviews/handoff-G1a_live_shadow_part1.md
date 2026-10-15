# Review handoff: G1a_live_shadow_part1

Source: workflow structured result (reviewer could not write files; this file is generated from the journal).

## Files read

| file | lines | read to end |
|---|---|---|
| research/live_shadow.py | 4200 | yes |

## What this part does (as implemented)

Scope: research/live_shadow.py lines 1 to 4200 (module helpers, ShadowConfig, ShadowStore, ledger readers, start of ShadowRunner). Call sites in 4150-8349 were read only where needed to verify behaviour, and are marked cross-range.

What it is (1-8): a broker-free shadow lane. It reads the recorder CSV corpus and EdgeLedger read-only and writes only its own WAL SQLite. A live "virtual open" is "never treated as a fill"; P&L comes only from a post-session replay.

Constants (86-135): equity 100,000; at most 32 candidates; 20,000 events per poll; 100,000 decisions; 200,000 events per replay session; 180-day retention; 64 MB pending-corpus cap.

Integrity helpers (178-422): canonical-JSON SHA-256 digests. `_replay_code_hash` (213-241) hashes every .py file under agent/ and research/ (85 files, 81,629 lines) plus four extras. Manifest, cohort and activation validators check self-digests and the fixed cohort shape (24 or 31 arms).

Causality (440-497, 668-673): `_availability_time` is max(timestamp, as_of, observed_at). Recorder bars carry as_of = bar start + 1 minute and observed_at = receipt time (deploy/recorder_market.py:458-469). Quotes carry as_of = timestamp and observed_at = receipt (507-512). So a bar or quote is usable only after receipt. `_event_end` = as_of, else timestamp + 1 minute. Session UTC ranges are built from NY midnights, so they are DST-correct (458-473). The calendar comes from the recorder index sidecar and is validated to NY dates (585-615). `_session_close` falls back to 16:00 only when require_exact_calendar is false (649-665); config.yaml:14 sets it to true. Events are immutable: a changed payload for an existing event_key raises InputConflict (1697-1702).

Policy (676-689, 3875-3883): close-relative entry and force-flat cutoffs via replay_policy_for_session. `_policy` falls back to a default ReplayPolicy on any exception.

Parity contract (974-1127):
- `_shadow_signature` projects live open_incomplete decisions; `_replay_signature` projects factory and IBR replay trades.
- The target_r back-out |T-S|/D - 1 (1064-1066) is correct for both long and short.
- `_signature_diffs` compares only signal geometry: timestamps, stop, target, distance, range, target_r, vehicle, profile, feed, provider and cross-sectional context.

Corpus reading (1153-1316): byte-offset tailing that consumes complete lines only. Compaction keeps all bars and options, the first and last quote per symbol-minute, and the latest visible quote at each bar availability. It runs before ingest (cross-range 7001).

ShadowStore (1430-3779):
- Tables for events, candidates, decisions, accounts and trades are append-only, enforced by triggers (1601-1632).
- virtual_books go from open_incomplete to closed_replay only after the session replay (3028-3064).
- replay_diffs are upserted per candidate and session (3434-3449). Evidence is written with INSERT OR IGNORE (3451-3498).
- gate_rows exposes trades only where the diff and trade status are both 'match' (3683-3708).
- prune deletes replay_diffs older than 180 days (3710-3763).
- Diagnostic-cohort persistence sits at 1829-2611.

Candidate intake (3781-3839): the 32 oldest ledger candidates with an eligible status, ibr.baseline first. ShadowRunner start (3911-4200): process-local event cache, plus a replay memo keyed by digests of the causal prefix, quarantine, book and policy.

Focus Q1, parity with the real trader (fact 10, B-02, B-08):
- Entry: the runtime posts a DAY limit at the IEX quote with legs anchored to the signal close. The live shadow plan uses the authored signal entry_price for authorising arms (cross-range 6036-6038), and the quote mid only for staleness. The replay that produces gate P&L fills at the next bar open or a visible quote (factory_core.py:606, 784).
- Fill certainty: runtime limits may never fill (B-08). The live shadow never fills. The replay always fills when a bar or quote is visible.
- Slippage guard: applies only to diagnostic equity arms (cross-range 5982-6002).
- Exits: none in the live lane. Replay exits are simulated in factory_core (stop/target ordering not traced here; F1 group).
- Deadlines and force flat: the live lane refuses entries at or after the latest entry time or force-flat (default 10 min, cross-range 5701-5727). The replay applies force_flat_time (factory_core.py:488-492, 657-681).
- Costs: live has none. Replay uses the cost_resolver_setup model.
- Sizing: RiskEngine on a fixed 100k in live; evolving equity from 100k in the replay. Runtime uses live equity.
- Concurrency: per-arm books are isolated across arms. Live books hold capacity all session; the replay releases capacity on exit (factory_core.py:1526-1536). See G1a-03.

Focus Q2, causality: sound for recorder-produced rows. A latent 60 s leak exists only for foreign rows lacking as_of and observed_at (G1a-08). Revised bars are not used: they halt ingest.

Focus Q3, bugs: no short-side sign, R, minute off-by-one or time-zone defect found in this range. The defects found are in evidence keying, bounds, concurrency and atomicity (findings below).

## Findings

### G1a-01 Re-replay under an unchanged signature digest appends a second set of gate-eligible trade rows
- Severity: CRITICAL  |  Category: incorrect  |  Confidence: LIKELY
- Location: `research/live_shadow.py:3451-3498 (record_replay_evidence), 3683-3699 (gate_rows); cross-range research/live_shadow.py:6532, 7466-7510; cross-group research/live_shadow_ingest.py:336-350, 575-602`
- Evidence: 3488-3491:
                trade_id = _digest({"candidate_id": candidate_id,
                                    "session_date": session_date,
                                    "replay_digest": replay_digest,
                                    "trade_digest": trade_digest,
6532:
        replay_digest = _digest(replay_signatures) if details.get("complete") and replay_ok else None
- Why it matters: replay_digest hashes only the signal signatures: no exit, quantity, cost or P&L. The account row is keyed by (candidate, session, replay_digest) and written with INSERT OR IGNORE, but each trade row is keyed by its full content. The memo is process-local (3941-3945), so after every restart every historical gate session of every current arm is re-replayed (7466-7510, 7990-8030). Suppose something has changed since the first replay that alters trade rows but not signal geometry: factory_core exit or cost code, the cost model, the stress-calibration flag or artifact, --equity, or a new telemetry field. Then a second set of rows is inserted under the same digest. gate_rows returns both sets, the account keeps the old P&L, and live_shadow_ingest._rows_for appends every row to the selection and confirmatory sets and to ledger.append_trade (the trade id uses the row index). Each affected opportunity is then counted twice, with P&L from two different cost or exit models, inflating n for the confirmatory gates. Reproduced: two replays with the same digest and P&L 60 then 55 gave gate_rows count 2 [60.0, 55.0] and account [(60.0, 1)].
- Suggested fix: Bind evidence to its content: store an evidence digest (sorted full trade rows plus account summary) on replay_diffs and join gate_rows on it. On a re-replay whose evidence differs, either treat it as a mismatch (quarantine) or write it under a new evidence key so superseded rows are no longer joined. Persist completed replays keyed by a full input digest so that restarts do not re-replay history.
- How verified by reviewer: Ran python (locked requirements) against a scratchpad ShadowStore: replay_diff + record_replay_evidence twice with one digest and different net_pnl; output 'gate_rows count: 2 [60.0, 55.0]' and 'accounts: [(60.0, 1)]'. Read the restart re-replay path (7448-7510, 7990-8030, 6531-6594) and the ingest pass-through (live_shadow_ingest.py:336-350, 575-602, 889-897). Not traced: whether downstream gate statistics deduplicate by opportunity_id (D2/F3).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### G1a-02 Authorising lane persists one immutable decision per bar per arm against a global, never-pruned 100,000 bound
- Severity: HIGH  |  Category: broken  |  Confidence: VERIFIED
- Location: `research/live_shadow.py:92, 2993-3026, 1609-1612, 3710-3763; cross-range 5911, 6665-6688, 7399-7408, 7965-7975; deploy/shadow.py:72`
- Evidence: 3019-3021:
            if count >= int(max_decisions):
                lane = "diagnostic shadow" if diagnostic else "shadow"
                raise ShadowError(f"{lane} decision bound {max_decisions} exceeded")
92: DEFAULT_MAX_DECISIONS = 100_000
- Why it matters: For every authorising arm, every bar event of every symbol in the session is evaluated, and every outcome is written through store.decision, including 'no signal' (5911) and 'insufficient bars' (6665-6688, 7399-7408, 7965-7975). The count covers all non-diagnostic decisions in the WAL with no session or time scope. A trigger forbids deleting decisions, and prune() never touches them. With 24 ETFs x 390 one-minute bars = 9,360 bar events per session, one arm reaches 100,000 after about 10.7 sessions. Eleven or more arms exceed it within the first session (32 arms after about 130 minutes). From then on, the first new decision of every poll raises ShadowError in the parent write loop (no local handler at 7950-7986), so forward evidence for every authorising candidate stops until an operator raises --max-decisions. No override was found in deploy/ or docs/. Each insert also opens a new connection, fsyncs (synchronous=FULL), and runs a full-table count with LIKE over JSON text, so the cost grows quadratically. The diagnostic lane already avoids this with sparse persistence (2487-2514).
- Suggested fix: Persist only signal-bearing decisions (as record_diagnostic_batch does) and keep no-signal and no-data counts in a per-session rollup. Scope any bound to the poll or session. Maintain a counter instead of count(*) with LIKE.
- How verified by reviewer: Read 6644-6705, 7399-7408, 7900-7986, 2993-3026, 1601-1612 and 3710-3763. Ran a snippet: three decisions for c1 with max_decisions=3, then prune() reported 0 pruned, then a 4th decision for another candidate and session raised 'ShadowError shadow decision bound 3 exceeded'. grep of deploy/ and docs/ found no override of the default 100,000.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### G1a-03 Virtual books hold portfolio capacity for the whole session while the replay releases it on exit, causing systematic parity mismatches
- Severity: HIGH  |  Category: incorrect  |  Confidence: LIKELY
- Location: `research/live_shadow.py:3044-3064; cross-range 6132-6168, 6625, 6629-6693; research/factory_core.py:1526-1536, 1731-1739; agent/risk.py:784-786; config.yaml:47-48`
- Evidence: 3060-3062:
            cursor = db.execute("""UPDATE virtual_books SET status='closed_replay'
                WHERE candidate_id=? AND status='open_incomplete' AND decision_id IN
                    (SELECT decision_id FROM decisions WHERE candidate_id=? AND session_date=?)""",
factory_core.py:1526:
                   if datetime.fromisoformat(item["exit_timestamp"]) <= timestamp]
- Why it matters: A live virtual open stays open_incomplete until the post-close replay of its session (6625), and the worker accumulates every open's notional until the session boundary (6629-6693). RiskEngine therefore sees all of the session's entries as concurrently open. With max_position_notional_pct 25 binding on every 30 bps stop (fact 10, B-06) and max_gross_exposure_pct 50, the third entry of a session is refused with 'max gross exposure cap reached' (agent/risk.py:784-786), even if the first two exited within minutes. The worker also vetoes any further entry on a symbol for the rest of the session (6669-6675), and these vetoes carry no signal, so _signal_dispositions never counts them as refusals. simulate_account removes exited positions before applying the same caps, so it admits those entries. The replay then has rows the shadow lacks, _signature_diffs reports 'extra', and the session is quarantined (6570-6577). Ingestion then blocks the candidate's tail awaiting a repair that a deterministic replay cannot produce. The live admission also differs from the real trader, which frees capacity on bracket or time exits.
- Suggested fix: Give the live virtual book the replay's exit model: release each open at its simulated bracket, time or force-flat exit, using only data visible at that instant. Alternatively, compare pre-risk signal geometry in the parity contract and apply portfolio caps once, in the replay.
- How verified by reviewer: Read open_books/close_session_books, _portfolio_state, the worker loop, the replay write path, factory_core.py:1526-1536 and 1731-1739, agent/risk.py:784-786, and the config caps. How often it triggers depends on an arm producing three or more entries, or two on one symbol, in a session; not run end to end.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### G1a-04 Replay parity certifies signal geometry only; all gate P&L comes from the backtest simulator's fill model
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `research/live_shadow.py:1102-1127, 3683-3708; cross-range 6459-6470, 6588-6594; research/factory_core.py:606, 784`
- Evidence: 1105-1108:
    key_fields = ("symbol", "session_date", "direction", "setup_type")
    compare_fields = ("signal_ts", "decision_ts", "entry_ts", "stop_price", "target_price",
                      "stop_distance", "range_high", "range_low", "target_r",
                      "vehicle", "profile", "equity_feed", "equity_provider",
- Why it matters: The 'match' that makes rows gate-eligible never compares entry price, quantity, exit time, exit reason, costs or P&L, and the live lane produces no exits at all (lines 6-8). Gate P&L is simulate_account or replay_ibr output on recorder bars. A fill is assumed at the next bar's open or a visible quote (factory_core.py:606 entry_bar = session_bars[index + 1]; 784 entry_underlying = float(entry_bar.open)), using research costs. The trader instead posts a DAY limit at the IEX quote with legs anchored to the signal close (fact 10, B-02), and the limit may never fill (B-08). Shadow evidence is therefore a forward re-run of the backtest model on fresh data, not a test of executability, and it cannot surface B-02 or B-08 effects.
- Suggested fix: Label shadow evidence as forward-model evidence in gates and docs. Either add the runtime order path to the replay (limit at the decision-time quote, DAY expiry, runtime-anchored bracket legs, unfilled entries recorded as no-trade), or reserve execution claims for the paper-trial lane.
- How verified by reviewer: Read the compare fields, the gate_rows join, the replay calls (6459-6470, 6588-6594) and the factory entry selection. The factory exit simulation itself was not traced (F1 group).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### G1a-05 Replay diff, evidence and book closure are separate commits with the 'match' diff written first
- Severity: MEDIUM  |  Category: incomplete  |  Confidence: LIKELY
- Location: `research/live_shadow.py:3434-3449, 3451-3498, 3057-3064, 3396-3432; cross-range 6555-6625, 7919-7921; research/live_shadow_ingest.py:323-345`
- Evidence: 6555:         self.store.replay_diff(candidate_id=candidate_id, session_date=session,
6588:             self.store.record_replay_evidence(
6625:             self.store.close_session_books(candidate_id, session)
- Why it matters: Each store method opens and commits its own connection. A crash, OOM kill or deploy restart after the diff commit leaves a 'match' diff with no shadow_trades or account. gate_sessions joins shadow_trades (3412-3417), so the session never enters the replay-only set, and a closed session receives no new events, so it is never replayed again. Its virtual books stay open_incomplete and are loaded into every later session's initial portfolio (7919-7921), consuming capacity and vetoing those symbols. Meanwhile ingest rejects the session ('is not a complete parity match' or 'has no parity-matched gate rows'), blocking the candidate's tail.
- Suggested fix: Write diff, evidence and book closure in one transaction, passing a connection as _ingest_event does; or write evidence first and the diff last. On start-up, re-replay any session whose 'match' diff lacks a matching account.
- How verified by reviewer: Read the three store methods, the call order in _replay, gate_sessions, initial_states and the ingest checks. The crash was not simulated.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### G1a-06 Candidate intake silently keeps only the 32 oldest eligible candidates, including demoted ones
- Severity: MEDIUM  |  Category: incomplete  |  Confidence: VERIFIED
- Location: `research/live_shadow.py:3807-3814; deploy/shadow.py:64; research/edge_ledger.py:381`
- Evidence: 3809-3814:
                WHERE (s.status IN ('backtest_passed','shadow','demoted','validated','champion')
                       AND c.strategy_id IN ('ibr','rule'))
                   OR (c.strategy_id='ibr' AND c.variant_id='ibr.baseline')
                ORDER BY CASE WHEN c.strategy_id='ibr' AND c.variant_id='ibr.baseline'
- Why it matters: Ordering is created_at ascending with LIMIT 32 (the deploy default). Demoted candidates keep their slots until retired (edge_ledger.py:381 allows demoted to go only to shadow or retired). ibr.baseline rows are included whatever their status, even retired. Once 32 older candidates qualify, newer backtest_passed candidates are never shadowed, never accrue forward evidence and cannot progress to validated. Nothing reports the truncation, because the query cannot see rows beyond the LIMIT.
- Suggested fix: Rank by lifecycle need, with shadow and backtest_passed ahead of demoted so new passes are not starved. Exclude retired baselines. Record the total eligible count and any truncated IDs in the manifest and heartbeat.
- How verified by reviewer: Read the SQL and the CLI default. The impact depends on the VM ledger population, which is not available in this container.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### G1a-07 _policy silently discards a candidate's whole replay policy on any validation error
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `research/live_shadow.py:3875-3883; cross-range 5161-5163, 6271-6272, 6443`
- Evidence: 3875-3878:
def _policy(config: Mapping[str, Any]) -> ReplayPolicy:
    try:
        return replace(ReplayPolicy.from_config(config), strict_market_data=True)
    except Exception:
- Why it matters: Any CostError (for example max_market_data_age_seconds 45) yields a default ReplayPolicy: no latest entry time, no portfolio caps, default force-flat. The live lane and the replay then run under a policy the candidate did not declare, with no marker, which is inconsistent with the fail-closed design elsewhere. Latent: the shipped config.yaml parses (latest entry 15:00, force-flat 10 min, max age 30 s, feed iex).
- Suggested fix: Fail closed: raise ShadowError so the candidate is rejected with a recorded reason.
- How verified by reviewer: Ran: ReplayPolicy.from_config(bad) raised 'CostError max_market_data_age_seconds must be finite and between 0 and 30 seconds'; ls._policy(bad) returned latest_entry_time None, force_flat None; ls._policy(good).latest_entry_time was 10:30:00; the runtime config parsed cleanly.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### G1a-08 Availability falls back to the bar start when as_of and observed_at are absent, contradicting _event_end
- Severity: LOW  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `research/live_shadow.py:476-491, 668-673, 1703-1705; research/market_data.py:191-195`
- Evidence: 486-488:
    as_of = _timestamp(row.get("as_of") or row.get("timestamp"))
    observed = _timestamp(row.get("observed_at") or row.get("as_of") or
                          row.get("timestamp"))
672-673:
    stamp = _timestamp(row.get("timestamp"))
    return stamp + timedelta(minutes=1) if stamp is not None else None
- Why it matters: The normaliser accepts a bar with neither field and defaults both to the bar timestamp (market_data.py:191-195). Such a row is ingested with as_of equal to the bar start and is 'available' 60 s before the bar completes, so a live decision could use its close at its open time, while _event_end assumes the bar ends one minute later. Recorder rows always carry as_of = start + 1 minute and observed_at = receipt (deploy/recorder_market.py:458-469), so only foreign or legacy CSVs are exposed. The replay's next-bar entry would then mismatch and quarantine rather than leak into gates.
- Suggested fix: For bar events, require as_of >= timestamp + 1 minute at ingest, or compute availability as max(timestamp + 1 minute, as_of, observed_at).
- How verified by reviewer: Read both helpers, the ingest normalisation, market_data._identity defaults and the recorder bar and quote rows.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### G1a-09 Unreferenced and test-only store code plus a redundant branch (about 38 lines)
- Severity: LOW  |  Category: dead-code  |  Confidence: VERIFIED
- Location: `research/live_shadow.py:1248-1257, 3225-3234, 3039-3042, 3500-3509, 1919-1922`
- Evidence: 1919-1923:
        if existing is not None and _json(validated) != _json(value):
            # A cohort has exactly one activation. Restarting reuses it; a
            # different code/config epoch receives a different cohort key.
            return validated
        return validated
- Why it matters: _diagnostic_source_projection and events_after_cursor have no references in research, agent, deploy or tests. ShadowStore.has_open and replay_accounts are test-only (6862 calls DiagnosticAccountBook.has_open, not the store). The branch at 1919-1922 returns the same value as the fall-through. This is maintenance cost only.
- Suggested fix: Delete the unreferenced helpers, move the test-only helpers into tests, and drop the redundant branch.
- How verified by reviewer: grep -F for each symbol across research, agent, deploy and tests: events_after_cursor( src=0 tests=0; _diagnostic_source_projection( src=0 tests=0; replay_accounts( src=0 tests=1; .has_open( src hits are diagnostic_accounts.py:1002 and live_shadow.py:6862 only.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

## Over-engineering

Range composition:
- Helpers at 1-1316: about 1,316 lines.
- ShadowConfig at 1318-1428: 111 lines.
- ShadowStore at 1430-3779: 2,350 lines and 75 methods.
- Ledger readers at 3781-3908: 128 lines.
- ShadowRunner caches and memo at 3911-4200: about 290 lines.

Identity layers: there are 37 `_digest(` calls in the range, implementing at least eleven identity layers:
- manifest digest (1769-1793)
- replay epoch (244-253)
- candidate epoch (274-291)
- cohort and activation digests (325-422)
- authorising event fingerprint (3955-4013)
- replay memo key (4032-4120)
- quarantine identity projection (4015-4030)
- diagnostic batch identity (2371-2383)
- trace identity (2494-2499)
- calendar snapshot identity (3947-3953)
- details digest (187-199)

Replay code hash: the hash at 213-241 covers 85 files and 81,629 lines in agent/ and research/, so an edit anywhere invalidates every memo and manifest. Most of the memo and fingerprint machinery exists only because each restart re-replays every gate session (cross-range 7466-7510). Persisting completed replays keyed by a full input digest would remove that layer and also close G1a-01.

Duplicated persistence: diagnostic-lane persistence (1829-2611, 783 lines) re-implements decisions, books, accounts, positions, orders and fills alongside the authorising tables. Diagnostic status is detected three ways: by id prefix, by config marker (3189-3217), and by SQL LIKE over JSON text (3004-3018).

Repeated getter pattern: ten near-identical meta getters repeat the same JSON load-and-validate pattern (1747-1761, 1795-1808, 1810-1827, 1857-1868, 1925-1936, 2671-2682, 2702-2720, 2757-2775, 2820-2847, 3765-3778).

Redundant telemetry keys:
- _signal_dispositions (958-971) returns the opportunities count three times, and admitted and refused twice each.
- _opportunity_capacity (888-896) has opportunity_count identical to max_trade_opportunities, and required_trade_rate identical to required_rate_for_floor.

Connection cost: every store call opens a fresh connection with PRAGMA journal_mode=WAL and synchronous=FULL (1443-1467). Per-decision inserts therefore pay a connection plus an fsync each (see G1a-02).

Coupled constants: hard-coded cohort shape constants (341-344: 24 or 31 arms, 12 or 13 families) duplicate the cohort builder.

Dead or test-only code: about 38 lines (G1a-09).

## Cross-group questions

- G1b (research/live_shadow.py 4150-8349): after any restart every historical gate session is re-replayed (7466-7510; memo is process-local, 3941-3945). If any arm lacks session.require_exact_calendar, the aggregate legacy bound at 7574-7579 (replay_session_max_events 200,000) is exceeded after roughly seven sessions of history (a compacted session is about 30k rows). Does every restart then raise 'legacy replay context bound exceeded'? Is a whole-history re-replay acceptable within poll time?
- G1b: catch-up after downtime. _read_corpus_append (1159-1197) reads to EOF with no row paging. After compaction, more than max_events (20,000) rows raises at 7002-7004, unless pending bytes exceed 64 MB, in which case the skip-and-quarantine path at 6974-6988 runs. A gap of roughly 4.5 market hours (24 symbols, bars plus first and last quotes per minute) looks sufficient to wedge the lane until --max-events is raised or the backlog passes 64 MB. Please confirm.
- G1b: in _replay (6531-6625), the 'match' diff is committed before evidence and before close_session_books. Is there any start-up sweep that re-replays a session whose diff has no matching shadow_accounts row (see G1a-05)?
- F1 (research/factory_core.py simulate_account): can a rule arm take a second trade on the same symbol in one session after an exit? If yes, the live worker's same-symbol veto (live_shadow.py:6669-6675) guarantees 'extra' replay rows and quarantine (G1a-03). Also confirm the intrabar stop and target ordering (stop-first) used for gate P&L.
- D2/F3 (gates, EdgeLedger): do gate statistics built from live_shadow_ingest._rows_for (research/live_shadow_ingest.py:336-350, 889-897), or the ledger trades appended at 575-602 (trade id derived from the row index), deduplicate by opportunity_id? If not, the G1a-01 duplicates inflate n and mix two cost or exit models.
- H1a (deploy/recorder.py, deploy/recorder_market.py): can the recorder re-emit a bar_1m row with the same event_key (_event_key('bar_1m', symbol, timestamp)) but revised OHLCV, for example from Alpaca late corrections or a backfill repair? ShadowStore._ingest_event raises InputConflict on a changed digest (live_shadow.py:1697-1702), which would stop every poll.
- A_signals (agent/contracts/rule.py generate_rule_signal): which families can emit more than one signal per symbol per session? This determines how often G1a-03 mismatches occur.

## Summary

- CRITICAL G1a-01: after any restart, every historical gate session is re-replayed. If trade rows changed but signal geometry did not (code, cost model, stress calibration or equity), a second set of rows is stored under the same replay_digest. gate_rows and the ingest then pass both to the gates, so trades are double-counted with conflicting P&L. Reproduced at store level: two rows (P&L 60 and 55); the account keeps 60.
- HIGH G1a-02: the authorising lane writes one immutable decision per bar per arm, 'no signal' included, against a global 100,000 cap that is never pruned. One arm halts after about 10.7 sessions, and 11 or more arms halt in the first session. Each insert also does a full count scan.
- HIGH G1a-03: live virtual books hold capacity all session, while the replay releases it on exit. With a 25% notional and 50% gross cap, the third entry of a session (or a same-symbol re-entry) is refused live but admitted in the replay. This causes shadow/replay mismatches and permanent quarantine, and live admission also diverges from the trader.
- MEDIUM G1a-04: 'parity match' checks signal geometry only. All gate P&L comes from the factory simulator's next-bar-open fill model with research costs, so the shadow cannot reveal B-02 (signal-close-anchored legs) or B-08 (unfilled DAY limits).
- MEDIUM G1a-05: diff, evidence and book closure are separate commits with the diff first. A crash between them wedges the candidate and leaves its books open in every later session.
- MEDIUM G1a-06: intake keeps only the 32 oldest eligible ledger candidates (demoted included, ibr.baseline whatever its status) and silently starves newer backtest passes.
- Causality in this range holds for recorder rows: availability is max(timestamp, as_of, observed_at), with bar as_of = start + 1 minute and observed_at = receipt. Only foreign rows without those fields would leak 60 s (G1a-08, LOW). The R back-out (1064-1066) is correct for long and short, and session bounds are DST-correct (458-473).
- LOW: _policy fails open to a default policy on any config error (G1a-07; latent, since config.yaml parses). About 38 lines of dead or test-only code (G1a-09). The heavy identity machinery (37 digest calls, 75 store methods, an 81,629-line code hash) mostly exists to make whole-history re-replays safe.
