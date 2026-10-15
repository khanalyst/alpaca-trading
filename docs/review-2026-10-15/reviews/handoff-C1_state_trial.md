# Review handoff: C1_state_trial

Source: workflow structured result (reviewer could not write files; this file is generated from the journal).

## Files read

| file | lines | read to end |
|---|---|---|
| agent/state.py | 493 | yes |
| agent/state_store.py | 121 | yes |
| agent/journal.py | 419 | yes |
| agent/governance.py | 315 | yes |
| agent/paper_trial.py | 1136 | yes |
| agent/paper_trial_operator.py | 662 | yes |
| agent/runtime_control.py | 705 | yes |

## What this part does (as implemented)

PERSISTENCE AND CONCURRENCY
- Durable state is one JSON file per mode. state_store.py:17-41 defines the schema (state, operator_pause, active_trades, protection, orders, risk_day, signal_sessions, edge_outbox, paper_trial).
- Every read is fully validated (state_store.py:48-86). This includes paper_trial validation, which after an operator cancellation re-verifies the audit row in SQLite (paper_trial.py:434-441).
- A file missing operator_pause is treated as corrupt (state_store.py:116-117).
- Writes go through a temp file, fsync and os.replace (state_store.py:89-101). The directory is not fsynced.
- Read-modify-write is serialised with flock on state.lock (state.py:137-163). Readers read the atomically replaced file without the lock.
- The run lock is a non-blocking flock on agent.pid (state.py:467-489). The trader holds it for its lifetime (runtime_control.py:604). The watchdog and the paper-trial canceller must take it before any broker action (deploy/watchdog.py:169; paper_trial_operator.py:586-588).
- The account fingerprint is sha256(mode, api_key\0account_id) (state.py:86-90; call site startup_edge_policy.py:406-407). It is bound once per runtime directory (state.py:93-115).

JOURNAL
- The journal is SQLite in WAL mode with synchronous FULL (journal.py:202-203) and a busy_timeout of 5 s (journal.py:199, 394). Tables: events, orders, trades, equity, runs, schema_meta (journal.py:32-109).
- insert() silently drops payload keys that have no column (journal.py:395-399).
- Close rows are idempotent by trade_id, using a read-then-insert plus a partial unique index (state.py:182-205, 403-459). Order rows are append-only.

GOVERNANCE (governance.py)
- Validates operator pins: id, variant_id, vehicle (117-169). Used by config.py:279.
- Renders a paste-ready promotion snippet (180-198), used by research/trial.py:484.
- Keeps an immutable, content-addressed config audit (201-272) that redacts the allowed secret keys (43-46, 67-80).

RUN LOOP (runtime_control.py:603-705)
- Takes the persistent lock and sets RUNNING unless KILLED (606-617). Records the config version (618). Requires _ensure_order_ready (619-644), which refuses KILLED or operator_pause, then runs preflight, reconcile and a forced startup flatten (startup_edge_policy.py:429-481; B-07).
- Calls run_once, then sleeps _aligned_cycle_delay. That is the time to the next multiple of 60 s on the Unix epoch, with no offset for bar publication (44-52, 661), so every cycle starts at about hh:mm:00 and the newest-bar dependency is B-12's.
- Any AlpacaError stops the loop (653-658). The finally block always flattens with exit reason shutdown_reason or run_exit (665-683). If the flatten is incomplete it sets operator_pause (684-692). This is where B-03 and B-04 become liquidation plus a pause.

OPERATOR PAUSE
- request_shutdown, called on every SIGTERM or SIGINT (main.py:253-256), durably sets operator_pause (504-511).
- Other setters: incomplete flattens (244-245, 689-690), _fail_closed (market_entry_risk.py:528), startup cleanup (startup_edge_policy.py:508) and supervisor recovery (deploy/watchdog.py:184-186).
- The supervisor parks while the flag is set (deploy/trader_supervisor.py:112-127).
- Only resume() clears it (357-502). resume() requires PAUSED, operator_pause, no kill reason, matching mode, no daily limit hit, empty active_trades and protection, terminal local orders, two flat broker snapshots and a bound fingerprint (292-332, 385-429).

FLATTEN (_flatten_all_impl, 103-290)
- Reads the live protective legs per symbol from durable state (69-101).
- Bulk-cancels only when nothing is protected (123-129).
- Polls up to 6 times. Per symbol it cancels that symbol's legs and confirms them terminal (189-194; execution_lifecycle.py:1098-1157), then calls close_position with client id flatten-<symbol>-<reason>-attempt (195-207; B-01). It records the order with action 'flatten' (220-238) and reconciles.
- It reports success when the broker shows no positions and no working orders (163-170). It never marks the active trade as closing, which causes C1-01.

GATE CHAIN FOR THE ORF PAPER TRIAL
1. Config validation (config.py:559-607): paper only, IEX or SIP feed, shares, LLM off, trial enabled, strict data.
2. Engine construction builds a frozen descriptor and calls update_runtime (engine.py:49-63, 192-213). It raises on any identity or code change (C1-03).
3. The supervisor's operator_pause park.
4. _ensure_order_ready, as above.
5. Every cycle, _refresh_edge calls update_runtime and refresh_state, and entries need public_status.entry_eligible (startup_edge_policy.py:530-606; paper_trial.py:971-973). That requires activation_confirmed, state running or passed, no blockers (any report validation error is a blocker, paper_trial.py:753-755, 781), no pending replacement and no error.
6. The usual session, latest_entry_time and risk vetoes (B group).
- The trial is activated on the first refresh that sees an account-bound flat broker and local book; started_on is that New York date (paper_trial.py:1077-1081).
- The trial is non-authorising: 'passed' changes nothing automatically. 'failed' and 'review_required' (at 60 accepted sessions) stop entries.
- In the auto-lane the same research.trial._verdict turns 'passed' into a promotable row with a promotion snippet (research/trial.py:350-358, 460-488).

WHAT THE TRIAL MEASURES
- Sessions: shadow session-acceptance reports dated after started_on. They pass about 40 checks (coverage, zero failed samples, freshness max at most 30 s, one warmup session, arm cursors, identities; paper_trial.py:503-723) and are capped at max_review_sessions (772-776). Nothing ties a session to the trader's own activity.
- Outcomes: every closed trial trade booked by reconcile (execution_lifecycle.py:2737-2850), deduplicated by a digest of trial, candidate and entry order (paper_trial.py:877-936).
- R: (broker paper-fill P&L minus modelled fees) / immutable post-cap planned risk (execution_lifecycle.py:2766-2774, 2811). Slippage is reported but not subtracted (238-243, 288). With B-06 the denominator is about 0.075% of equity, and with B-02 it is measured from the live ask.
- exit_reason is stored but never filtered. Flatten exits (15:50 force flat, B-03/B-04/B-07, watchdog) are not booked at all (C1-01).
- Verdict (research/trial.py:71-173): sessions >= 20, outcomes >= 20 and at least 20 distinct outcome session dates. Then pass if total > 0, mean > 0 and the 5th percentile of a 1,000-draw moving-block bootstrap (block 5) > 0. Fail if total <= 0, mean <= 0 and the 95th percentile < 0. Otherwise inconclusive. It is re-evaluated every refresh, and 'passed' is not terminal (paper_trial.py:39-40, 856-867).

ZERO-EDGE ARITHMETIC (C1-05)
- With n fixed, total_r > 0 is the same event as mean_r > 0.
- ORF bracket (stop 1R, target 1.5R), zero edge: P(target) = 1/(1 + 1.5) = 0.4. S = 2.5K - 20 > 0 when K >= 9. P(Bin(20, 0.4) >= 9) = 0.4044.
- Target 2R: P(Bin(20, 1/3) >= 7) = 0.5207. Symmetric continuous R: 0.5.
- With the bootstrap condition, simulated through the real code, the false-pass rate is 5.7% / 9.2% / 10.5% at one look, and 21% across looks at sessions 20 to 60.

ORF ARM (deploy/paper-orf.config.json:131-137; spec printed with resolve_catalog_arm)
| field | value |
|---|---|
| variant_id | rule.opening-range-fade.d5785d9e70b56def (baseline) |
| family / schema | opening_range_fade, rule-strategy.v4 |
| range_minutes | 20 |
| threshold_bps | 8.0 |
| side | both |
| confirmation | none |
| stop_atr | 1.0 on a 14-bar one-minute ATR, so the 30 bps floor binds (coordinator fact 2) |
| target | fixed_r 1.5 (about 45 bps) |
| max_hold_bars | 90 |
| exit_before_minutes | null |
| entry_after_minutes / entry_before_minutes | 0 / 390 |
| latest_entry_time | 15:00 (config line 66) |
| force flat | 10 minutes before close (config lines 15, 67) |
| trial floors | min_sessions 20, min_trades 20, min_mean_r 0, min_total_r 0, max_review_sessions 60 (config lines 124-136) |

CALENDAR TIME
- Best case: if activated on 23 Sep 2026, as the trial id suggests (VM state not visible, coordinator fact 6), with every session accepted, at least one booked trade per day, no code change and no pause, the earliest terminal verdict follows the 20th accepted session on Wed 21 Oct 2026.
- Under zero edge about 80% of single looks are inconclusive. Most paths run to review_required at the 60th accepted session, Thu 17 Dec 2026. The 40th falls on 18 Nov.
- In practice that best case is not reachable without manual intervention:
  - any deploy that touches agent/ or research/ makes the trader unstartable (C1-03); there were 23 change days in 6 weeks;
  - every SIGTERM parks the trader until a manual resume;
  - every 15:50 force flat leaves a phantom trade that blocks resume (C1-01);
  - shadow sessions keep counting while the trader is parked (C1-04).

PAPER TRIAL OPERATOR
paper_trial_operator.py allows exactly one transition: cancelling a paused, account-bound, broker-flat and evidence-empty trial. It records an audit row first (563-655).

## Findings

### C1-01 Flatten-driven closes are never booked: the trade stays in active_trades, no journal close or outcome is written, resume deadlocks, and the next trade on that symbol is closed as protection_missing
- Severity: CRITICAL  |  Category: broken  |  Confidence: VERIFIED
- Location: `agent/runtime_control.py:220-238 (cross-group: agent/execution_lifecycle.py:2302-2304, 2572-2592, 2221-2236; agent/engine_cycle.py:446-447)`
- Evidence: runtime_control.py:225  "qty": str(qty), "action": "flatten", "reason": reason,
execution_lifecycle.py:2303  if action != "close" and order_id != str(trade.get("closing_order_id") or ""):
execution_lifecycle.py:2587-2591  if close_filled <= 0: ... active[symbol] = dict(trade)
- Why it matters: The flatten path stores its close order with action 'flatten'. It never sets closing_order_id, closing_reason or closing_price on the active trade, unlike the monitor's close path (execution_lifecycle.py:1789-1795). Reconcile's close_entries accepts only action 'close' or the trade's closing_order_id, so when the position disappears it finds no close evidence and keeps the trade active.

What the run through the repository's own ProtectionHarness showed:
- flatten returned True with the broker flat;
- after 3 reconciles and a monitor pass, SPY was still 'open';
- the journal held only the open row, and no outcome was queued;
- resume() was blocked with active_trades_present;
- a second SPY entry inherited the phantom's cancelled legs, because reconcile copies protective_legs from the previous row, and the monitor closed it at once as protection_missing although its own broker legs were 'held'.

Paths affected: the daily 15:50 force flat (engine_cycle.py:446-447 runs flatten_all before the monitor), B-03 fail-closed, B-04 run_exit, B-07 startup cleanup, and watchdog or supervisor flattens.
- Trial evidence: trades closed by these paths never reach the outcomes, so the R sample is censored by exit path. B-03's premise that infrastructure exits are booked does not hold.
- Operations: resume() and trial cancellation, activation and replacement all need an empty local book, so after the next pause the runtime cannot resume without editing state.json.
- Real broker: the protection_missing close would be sent while the untracked bracket legs still hold the quantity, likely a 403 insufficient-qty rejection that feeds B-04.
- Suggested fix: In _flatten_all_impl, before submitting, durably mark active_trades[symbol] as closing (closing_order_id = the flatten client id, closing_reason = reason, closing_attempt), as the monitor close path does. Alternatively, let close_entries accept 'flatten' rows bound to the entry order id. When a new entry activates, never copy protective_legs or closing_* from a stale row whose order_id differs. Add tests: after a flatten, a journal close row, an outcome (and trial outcome) exist and active_trades is empty; a later same-symbol trade keeps its own legs.
- How verified by reviewer: Ran verification/c1/flatten_book.py and flatten_phantom.py. Both reuse ProtectionHarness from tests/test_broker_protection.py; command: cd /home/user/alpaca-trading && python (locked requirements) -I <script>. Output lines:
- 'flatten returned: True | broker positions: []'
- 'after flatten + 3 reconciles: active_trades = ['SPY'] status = {'SPY': 'open'}'
- 'journal trades = [('SPY', 'open', 10.0, 101.5, None, None)]'
- 'edge_outbox rows = 0'
- 'resume invariants: _ResumeBlocked operator resume blocked: active_trades_present'
- 'monitor result: {'closed': [], 'failed': [], 'force_flat': False}'
- 'active SPY after second fill: order_id=order-3 entry_price=110.0 qty=5 ... legs=[('order-1-stop', 'canceled'), ('order-1-target', 'canceled')]'
- 'broker legs for second entry: [('order-3-stop', 'held'), ('order-3-target', 'held')]'
- 'monitor on second trade: {'closed': [{'symbol': 'SPY', 'reason': 'protection_missing'}], ...} | new close requests: [('SPY', '5', 'protection-close-spy-protection_missing')]'
The trial path calls the same _record_edge_outcome, which is never reached for these closes. The real-broker rejection was not run (LIKELY).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C1-02 Flatten cancels a symbol's broker bracket legs before its close is accepted and never re-arms protection when the close fails, leaving the position naked
- Severity: HIGH  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/runtime_control.py:176-207, 256-290 (cross-group: agent/alpaca_provider.py:659-703; agent/market_entry_risk.py:514-519; agent/startup_edge_policy.py:499-509)`
- Evidence: runtime_control.py:189-194  legs = protected.get(symbol) or []
                if legs and not self._cancel_protective_legs(symbol, legs):
                    ...
                protected.pop(symbol, None)
runtime_control.py:200-203  try:
                        order = close(symbol, qty=qty, client_order_id=client_order_id,
                                      order_type="market", time_in_force="day")
                    except TypeError:
- Why it matters: The legs are cancelled and confirmed terminal before close_position runs. close_position performs a positions() GET and then submit_order (alpaca_provider.py:673, 703), and only TypeError is caught. Any AlpacaError escapes with the legs already gone, for example:
- a B-01 IdempotencyConflict on a reused flatten id with a different quantity;
- a 403 or 422 rejection;
- a failed positions() call;
- a hang with no HTTP timeout (B-10).

In B-01's same-quantity case the provider returns an old filled order. The loop then treats status 'filled' as non-retryable (184-185) and ends flatten_incomplete after 6 polls, again with the legs cancelled.

The callers swallow the failure and pause: run()'s finally (678-683), _fail_closed (market_entry_risk.py:516-519) and startup cleanup (startup_edge_policy.py:501-509). The position then has no stop or target until the watchdog's paused-without-trader flatten acts (deploy/watchdog.py:95-105, 221-226). That flatten uses its own deterministic id (B-01), and its Engine cannot be built during a trial identity mismatch (C1-03).
- Suggested fix: Treat cancel-then-close per symbol as one unit. Catch exceptions per symbol. If the close is not accepted, immediately resubmit a protective DAY stop at the stored stop price before returning, and record the residual. Alternatively, close by replacing the stop leg with a marketable price, so the quantity is never unprotected. Make flatten ids unique per exposure (B-01).
- How verified by reviewer: Read runtime_control.py:103-290 end to end, alpaca_provider.py:531-550 and 659-703, execution_lifecycle.py:1098-1157, and the three callers. The IdempotencyConflict and stale-order behaviour of reused ids was established by B-01's fake-broker run (handoff-B_execution_broker.md). The naked end state was traced by reading, not run.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C1-03 A running paper trial freezes every .py file under agent/ and research/: any deploy makes every Engine construction fail (trader, watchdog flatten, supervisor recovery), and a trial with evidence cannot be cancelled
- Severity: HIGH  |  Category: over-engineered  |  Confidence: VERIFIED
- Location: `agent/paper_trial.py:160-179, 449-457, 1059-1064; agent/paper_trial_operator.py:620-622 (cross-group: agent/engine.py:196-213, 207-210; deploy/watchdog.py:108-113, 213)`
- Evidence: paper_trial.py:163-166  files = {
        path.relative_to(repo).as_posix()
        for name in ("agent", "research")
        for path in (repo / name).rglob("*.py")
paper_trial.py:1062-1064  elif existing.get("state") not in TERMINAL_STATES:
            raise PaperTrialError(
                "active paper trial identity/config changed; keep the frozen incumbent")
paper_trial_operator.py:620-622  if trial.get("accepted_sessions") or trial.get("outcomes"):
                raise PaperTrialOperatorError(
                    "paper cancellation requires an evidence-empty trial")
- Why it matters: code_identity hashes all agent/ and research/ sources plus requirements.lock.txt and deploy/recorder.py, and _same_incumbent compares it. After any such change, update_runtime raises for a running trial, and Engine.__init__ converts that into AlpacaError for every construction, light=True included (engine.py:212-213). As a result:
- the trader cannot start;
- the watchdog's flatten engine (deploy/watchdog.py:108-113, 213) and the supervisor's recover() cannot be built, so residual exposure loses its automated backstop;
- disabling the trial is also refused (engine.py:207-210).

The only exits are an evidence-empty cancellation, waiting for 'failed' or 'review_required' (20 to 60 or more accepted sessions), or editing state.json.

The repository changed agent/ or research/ .py files on 23 distinct days between 30 Aug and 11 Oct 2026 (longest gap 6 calendar days), while a trial needs at least 21 trading days. Fixes for B-01 (CRITICAL), C1-01 or B-10 therefore cannot be deployed during a trial without discarding its evidence.
- Suggested fix: Freeze only what defines the incumbent's behaviour (rule_spec, the rule contract module, and the risk, execution and cost config). Record the full code hash as provenance, not as an identity gate. Add an audited abandon-with-evidence terminal state that keeps the outcomes. Never let a trial-state mismatch block a light Engine whose only use is flattening.
- How verified by reviewer: Read the chain paper_trial.py:160-244, 449-457 and 1052-1088, then engine.py:49-63 and 185-218 (the trial update runs before the 'if not light' branch), then deploy/watchdog.py:108-113 and 210-213. Ran: git log --since=2026-08-15 --format=%ad --date=short -- 'agent/*.py' 'agent/**/*.py' 'research/*.py' 'research/**/*.py' | sort -u. Output: 23 dates from 2026-08-30 to 2026-10-11 (42 commits including the lock and recorder files); the longest gap is 2026-10-01 to 2026-10-07.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C1-04 Trial evidence is not joined to what it qualifies: sessions come from shadow reports regardless of trader activity, outcomes count regardless of session acceptance, and infrastructure exits are not excluded
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `agent/paper_trial.py:816-833, 503-528, 744-781 (cross-group: agent/execution_lifecycle.py:2766-2817, 234-301)`
- Evidence: paper_trial.py:824-826  "sessions": len(state_value.get("accepted_sessions", ())),
        "outcomes": len(outcomes),
        "total_r": sum(finite_r) if len(finite_r) == len(outcomes) and outcomes else None,
execution_lifecycle.py:2766-2767  # Paper R is intentionally based on the immutable post-cap planned
        # risk, which is the same nominal denominator used by research.
- Why it matters: Sessions and outcomes are counted independently:
- The 20-session floor and the 60-session review cap count diagnostic-shadow acceptance reports. These check coverage, freshness, arm cursors and identities only, not whether the trader ran, could trade, or was parked by operator_pause. Parked days therefore consume the review budget.
- Every closed trial outcome counts, including trades on sessions whose report was rejected or missing, and on the activation day itself (reports dated on or before started_on are skipped at 513-515; outcomes are not).
- Outcomes carry exit_reason, but nothing excludes fail-closed, run_exit, startup or watchdog exits; flatten exits are missing entirely per C1-01.

How R is defined:
- R = (broker paper-fill P&L minus modelled fees) / immutable post-cap planned risk (execution_lifecycle.py:2769-2774, 2811; attribution 238-243, 288).
- The denominator is about 0.075% of equity (B-06) and is measured from the live ask, not the replay anchor (B-02).
- A partially filled entry is divided by the full planned risk, which compresses its R toward 0.
- Paper fills have no queue or impact.

The verdict therefore does not measure the replayed hypothesis on the sessions that the data gate accepted.
- Suggested fix: Count a session only when its report was accepted and the trader was running, unpaused and eligible for the whole entry window. Restrict outcomes to entries made on accepted sessions. Tag infrastructure exits and report R with and without them. Scale planned risk by the entry fill fraction. Publish the R definition alongside the verdict.
- How verified by reviewer: Read paper_trial.py:503-874 end to end: _performance takes len(accepted_sessions) and all outcomes, with no join on session_date or exit_reason. Also read execution_lifecycle.py:234-301 and 2737-2856.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C1-05 Trial verdict is anti-conservative: about 6 to 10% false passes at one look and about 21% across the sequential looks the code performs; the total-R floor is redundant
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/paper_trial.py:785-813, 852-867, 39-40 (cross-group: research/trial.py:142-168, 350-358, 460-488); agent/governance.py:180-198`
- Evidence: research/trial.py:161-164  positive_economics = (total_value > min_total_r and
                          mean_value > min_mean_r)
    if positive_economics and lower_bound > min_mean_r:
        return {"state": "passed", **common}
paper_trial.py:804-806  result = moving_block_cluster_bootstrap_lower_bound(
        values, clusters, confidence=.95, draws=1000,
        block_length=max(1, min(5, cluster_count - 1)), min_clusters=2)
- Why it matters: With n fixed, total_r > 0 and mean_r > 0 are the same event, so min_total_r adds nothing.

Point gate alone, zero edge:
- ORF bracket (stop 1R, target 1.5R): P(target) = 1/(1 + 1.5) = 0.4. S = 1.5K - (20 - K) = 2.5K - 20 > 0 when K >= 9. P(Bin(20, 0.4) >= 9) = 0.4044.
- Target 2R: P = 1/3. S = 3K - 20 > 0 when K >= 7. P(Bin(20, 1/3) >= 7) = 0.5207.
- Any symmetric continuous R: 0.5.

The code also requires the 5th percentile of a 1,000-draw moving-block bootstrap (block 5, 20 session clusters) to be above 0. Simulated through the real _performance, _confidence and _verdict, a zero-edge strategy passes at one look in 5.7% (1.5R bracket), 9.2% (2R bracket) and 10.5% (normal R) of cases, against a nominal 5%.

refresh_state re-evaluates on every refresh, and 'passed' is not terminal (TERMINAL_STATES excludes it). Across looks at sessions 20 to 60, 21.0% of zero-edge paths show 'passed' at least once, and 23.0% end 'failed'.

In the auto-lane the same verdict produces a 'promotable' row carrying a paste-ready promotion_snippet (research/trial.py:460-488).
- Suggested fix: Fix the analysis point in advance, or apply alpha spending across looks. Use a studentised or t-based bound with at least 30 to 40 session clusters. Test against a cost-adjusted floor (for example mean R > 0.1). Drop the redundant total-R floor or replace it with a different statistic such as drawdown. Treat 'passed' as final only at the pre-registered look.
- How verified by reviewer: Ran verification/c1/zero_edge.py with python (locked requirements) -I zero_edge.py 2000 300. Output lines:
- 'point gate only, n=20: bracket 1.5R (K>=9 of Bin(20,0.4)) = 0.4044 | bracket 2R (K>=7 of Bin(20,1/3)) = 0.5207'
- 'bracket_1.5R: point-gate pass 0.402; verdict {'failed': 155, 'inconclusive': 1732, 'passed': 113} -> passed 0.057'
- 'bracket_2R: ... passed 0.092'
- 'normal: ... passed 0.105'
- 'sequential looks 20..60 sessions, bracket_1.5R zero edge, 300 paths: ever 'passed' 0.210, terminal 'failed' 0.230'
The sequential rate has a standard error of about 2.4 percentage points.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C1-06 The run loop sleeps through SIGTERM for up to 60 s while the supervisor kills after 15 s, so the trader's own shutdown flatten usually never runs
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/runtime_control.py:504-511, 660-661 (cross-group: deploy/trader_supervisor.py:53-74, 162-186; main.py:253-256; deploy/watchdog.py:211-222)`
- Evidence: runtime_control.py:660-661  if self.running and (max_cycles is None or cycles < max_cycles):
                    time.sleep(_aligned_cycle_delay(time.time(), interval))
trader_supervisor.py:62-67  child.wait(timeout=grace)
        return True
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
- Why it matters: request_shutdown only sets running=False and persists operator_pause; it does not raise. Since PEP 475, time.sleep resumes after a non-raising handler, so the loop re-checks running only at the next minute boundary. terminate_child sends SIGTERM and SIGKILLs the process group after 15 s.

For a stop at a random moment, with a cycle taking a few seconds, roughly 70 to 75% of stops land in a sleep with more than 15 s left. In those cases the cooperative finally-flatten (665-705) and close() never run. The supervisor then runs recover(), which flattens under 'supervisor_terminated_unresponsive_trader'. So:
- every routine deploy or reboot is recorded as an unresponsive trader;
- it reuses one deterministic flatten id per symbol (B-01);
- a SIGTERM during a long cycle can kill the flatten between leg cancel and close (C1-02).
- Suggested fix: Sleep on a threading.Event that request_shutdown sets (Event.wait(timeout)), or sleep in short slices that check self.running. Set the supervisor grace above one cycle plus a flatten, for example 90 s.
- How verified by reviewer: Ran verification/c1/sleep_sig.py: a SIGTERM handler that only sets a flag, with SIGTERM sent at 1.0 s during time.sleep(6.0). Output: 'stop flag=True after SIGTERM at ~1.0 s; sleep returned after 6.00 s' and 'aligned delay examples (interval 60): [59.5, 20.1, 60.0]'. The supervisor timing was read end to end in deploy/trader_supervisor.py:53-74 and 130-186. The combined kill was not run.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C1-07 Config audit trail cannot represent a revert, and the dashboard then labels the wrong version current; actor is always 'operator'
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/governance.py:233-272; agent/runtime_control.py:558-560 (cross-group: deploy/dashboard.py:876)`
- Evidence: governance.py:252-256  existing = db.execute(
            "SELECT * FROM config_versions WHERE config_hash=?",
            (config_hash,)).fetchone()
        if existing is not None:
            return {**dict(existing), "recorded": False,
dashboard.py:876  "current": history[0]["config_version_id"] if history else None,
- Why it matters: config_hash is UNIQUE and the table is immutable, so returning to an earlier configuration records nothing. The docstring's promise that 'which settings were in force' can be answered fails for any revert. config_version_id is not stamped on order, trade or event rows; only a config_version_recorded event is written, and only for a new version. actor is never passed (runtime_control.py:558-560). A risk or cost revert during a trial is therefore invisible in the audit.
- Suggested fix: Keep content addressing, but append a config_activations(run_id, config_version_id, ts) row on every start. Stamp config_version_id on order and trade rows. Derive 'current' from the latest activation. Pass the real actor.
- How verified by reviewer: Ran record_config_version for configs A, B, then A again, in a temporary database. Output:
- 'A -> cfg-f6e7edbf0e672b51 recorded = True'
- 'B -> cfg-fbd7a1b6541cbb04 recorded = True'
- 'A again -> cfg-f6e7edbf0e672b51 recorded = False'
- 'dashboard-style current (history[0]): cfg-fbd7a1b6541cbb04 | actually in force: cfg-f6e7edbf0e672b51'
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### C1-08 Every journal row costs 6 SQLite connections and 5 commits because each insert re-runs the schema migration twice
- Severity: LOW  |  Category: over-engineered  |  Confidence: VERIFIED
- Location: `agent/state.py:215-245, 253-268; agent/journal.py:312-317`
- Evidence: state.py:256  ensure_ready()
state.py:244  if not journal_ready():
state.py:220  initialize_journal()
journal.py:313-315  "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                    ("runtime_schema", "2"),
- Why it matters: Each log_event, log_order, log_equity or log_trade runs ensure_ready, which:
1. runs initialize_journal: 6 CREATE TABLE IF NOT EXISTS statements, table_info scans, an INSERT OR REPLACE into schema_meta and a BEGIN IMMEDIATE index transaction;
2. runs journal_ready, which runs initialize_journal again;
3. parses and validates the whole state file;
4. only then performs the single data INSERT.

That is 5 durable commits and 5 write-lock acquisitions instead of 1, measured at about 10.7 ms per row here. It sits inside a 60 s cycle that writes many events. Lock waits beyond busy_timeout (5 s) surface as JournalNotReady, which order-bearing callers treat as fail-closed (runtime_control.py:239-248).
- Suggested fix: Initialise and migrate once per configure_runtime, cache readiness, and make _journal_insert a single INSERT.
- How verified by reviewer: Ran verification/c1/journal_cost.py, which wraps sqlite3.connect with a trace callback. Output:
- 'one state.log_event -> sqlite connections opened: 6'
- statements include 2 x 'INSERT OR REPLACE', 2 x 'BEGIN IMMEDIATE' / 'CREATE UNIQUE INDEX', and 'INSERT INTO events'
- 'COMMITs: 5'
- 'mean wall time per log_event on this disk: 10.7 ms'
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### C1-09 One outcome without a fill price makes the whole trial permanently inconclusive
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/paper_trial.py:826-829 (cross-group: agent/execution_lifecycle.py:2510-2518; research/trial.py:101-116)`
- Evidence: paper_trial.py:826-828  "total_r": sum(finite_r) if len(finite_r) == len(outcomes) and outcomes else None,
        "mean_r": (sum(finite_r) / len(finite_r)
                   if len(finite_r) == len(outcomes) and outcomes else None),
- Why it matters: When any broker close fragment lacks filled_avg_price, the close path records an unmeasurable outcome with r_multiple None. From then on total_r and mean_r are None, _verdict returns 'inconclusive' at every look, and the trial can only end as review_required after 60 accepted sessions. The bootstrap drops non-finite values and is computed, but it cannot rescue the decision. One broker data glitch discards up to 59 sessions of evidence.
- Suggested fix: Exclude unmeasurable outcomes from the R statistics but count them, and fail closed only above a small share (for example 5%). Alternatively, value them conservatively at -1R.
- How verified by reviewer: Read paper_trial.py:816-874 together with research/trial.py:98-116 and execution_lifecycle.py:2510-2518.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

## Over-engineering

All figures are approximate, counted by line ranges across the 3,851 assigned lines.

How the lines divide:
- About 595 lines (15%) can change a trade decision:
  - flatten (runtime_control.py:56-290);
  - the run loop and aligned delay (44-52, 603-705);
  - effective_config (paper_trial.py:247-261);
  - the flat-book checks (466-500);
  - the lifecycle in refresh_state (836-874);
  - entry eligibility in public_status (939-999);
  - update_runtime (1052-1088);
  - pin validation (governance.py:117-177).
- About 1,950 lines (51%) are evidence, identity and audit validation:
  - descriptor, validate_state, report re-validation, outcomes and audit in paper_trial.py (about 780);
  - all of paper_trial_operator.py (662);
  - the governance config audit and snippet (about 245);
  - resume() and config audit in runtime_control.py (about 265).
- About 1,030 lines (27%) are persistence plumbing (state.py, state_store.py, journal.py).

Specific items:
1. paper_trial_operator.py is 662 lines (17% of the group) for one transition that is allowed only when the trial has zero evidence (620-622). After a cancellation it also adds a SQLite read and several SHA-256 digests of the whole trial to every state read (paper_trial.py:434-441 via state_store.py:79-85).
2. paper_trial._read_report (503-723, about 220 lines) re-implements the producer's acceptance contract field by field. Any divergence becomes a permanent entry blocker (753-755, 781, 971-973) rather than a skipped session.
3. Ten identities are compared by _same_incumbent (449-457), and the code hash covers the whole tree (160-179). This makes the trial undeployable (C1-03), yet none of it detects C1-01.
4. Journal amplification: 6 connections and 5 commits per row (C1-08). The migration column lists (journal.py:208-290, about 85 lines) and alias plumbing duplicated in log_order and log_trade (state.py:353-367, 422-433) carry 80 or more telemetry columns per trade.
5. Dead or unused code:
   - governance.config_version_at (304-307, journal_path parameter unused);
   - governance.config_history (275-301; only OPERATIONS.md uses it, and the dashboard re-queries at deploy/dashboard.py:856-859);
   - CONFIG_AUDIT_SCHEMA and PROMOTION_SCHEMA (governance.py:35-36);
   - journal.journal_insert (419);
   - unused parameters: _confidence(policy) (paper_trial.py:785), new_state(now) (275), cancel_paper_trial(now), deleted at 582;
   - seam arguments deleted at journal.py:342 and 387;
   - a no-op re-raise wrapper (state_store.py:80-85);
   - duplicated constants: paper_trial.OPERATOR_CANCELLATION_SCHEMA and _AUDIT_KIND versus paper_trial_operator.AUDIT_SCHEMA and AUDIT_KIND.
6. The 'passed' verdict has no automated effect in the paper-trial lane (it stays in ACTIVE_STATES), so the bootstrap machinery only serves display and stop rules.

Simplification: freeze only the rule spec and its contract, and key evidence on session plus trader eligibility. That would remove most of items 1 to 3 and fix C1-03 and C1-04.

## Cross-group questions

- B_execution_broker / owner of agent/execution_lifecycle.py, about C1-01:
- Please confirm that close_entries (2291-2317) ignores rows with action 'flatten', so every flatten close leaves the trade active and unbooked. B-03's statement that 'infrastructure exits are booked as paper outcomes' does not hold for flatten paths; the effect is omission, not contamination.
- Also check the reconcile merge at 2221-2236, which copies protective_legs and closing_* from the previous row onto a newly activated trade with a different order_id.
- B_execution_broker / Alpaca semantics: when the monitor's protection_missing close (execution_lifecycle.py around 1741-1795) runs on a position whose real bracket legs are untracked and still held, does Alpaca reject the market sell with 403 'insufficient qty available', which then feeds B-04?
- C2_startup_acceptance_watchdog (deploy/session_acceptance.py): does the producer set accepted=True under exactly the checks that paper_trial._read_report re-applies? Those checks are:
- strict_threshold_cap_seconds == 30.0;
- p50 <= p95 <= max <= 30 s for quote_event_age_seconds, bar_publication_deadline_lag_seconds and shadow_source_lag_seconds;
- failed == 0;
- exactly one warmup session;
- identical arm cursors;
- activation watermark before the open.
Any accepted report the trial rejects becomes a permanent entry blocker (paper_trial.py:753-755, 781, 971-973). What fraction of real IEX sessions for the 24 ETFs meet max quote age <= 30 s?
- C2 / H2 (deploy/trader_supervisor.py:53-74, 130-186; deploy/watchdog.py:108-113, 158-235): confirm that a supervisor stop usually SIGKILLs the trader during its 60 s sleep (C1-06). Confirm that Engine construction in watchdog run_once and in recover() raises AlpacaError while a trial's code identity mismatches (C1-03). How is that failure surfaced to the operator?
- D2_gates / F3 (research/trial.py:71-173 _verdict, 308-395 review_trials, 440-490 promotable_report): the verdict is evaluated sequentially with no alpha spending, uses a percentile moving-block bootstrap on 20 clusters, and has a redundant total-R floor. Measured false-pass rates are 5.7 to 10.5% at one look and 21% across looks (C1-05). Is the same helper used elsewhere with similarly few clusters?
- E1_costs (research/costs.py CostModel.fees via static_cost_config, called from execution_lifecycle.py:267-277): is fee_bps 0.5 charged per side on both legs? Alpaca's real cost is the SEC fee, about 0.21 bps on sells only (coordinator fact 9), so trial R would be penalised by about 0.8 bps per round trip, roughly 0.03R on a 30 bps stop.
- J1 tests: no test asserts that a flatten books the close (journal close row, outcome or trial outcome, empty active_trades), or that a later same-symbol trade keeps its own legs. tests/test_broker_protection.py:821-856 asserts only the call order. Please add both.
- H2b (deploy/dashboard.py:856-878): 'current' config version is history[0], the newest created row, which is wrong after a revert (C1-07).

## Summary

- Report not written to disk; this structured result is the full handoff. Findings: CRITICAL 1, HIGH 2, MEDIUM 3, LOW 3 (9 total). All files were read to the end (3,851 lines).
- C1-01 CRITICAL (VERIFIED by running the repository's own test harness): flatten closes are never booked. After a flatten that fills and leaves the broker flat, the trade stays 'open' in active_trades, no journal close row or outcome is written, and resume() is blocked with active_trades_present. The next SPY trade then inherits the cancelled legs and is closed at once as protection_missing. This hits every 15:50 force flat and every B-03, B-04 and B-07 flatten, and contradicts B-03's premise that such exits are booked.
- C1-02 HIGH (VERIFIED by reading): flatten cancels a symbol's bracket legs before its close is accepted and has no rollback. Any close error (B-01 IdempotencyConflict or stale filled order, a 403 or 422, a hang under B-10) leaves the position with no stop or target until the watchdog acts.
- C1-03 HIGH (VERIFIED): a running trial hashes every agent/ and research/ .py file. After any deploy, every Engine construction fails: the trader, the watchdog flatten and supervisor recovery. A trial with evidence cannot be cancelled. The code changed on 23 days in 6 weeks, so B-01 cannot be fixed mid-trial without discarding evidence.
- C1-05 MEDIUM (VERIFIED by simulation through the real code): for a zero-edge strategy, mean>0 and total>0 is one test that passes 40 to 52% of the time. The bootstrap bound brings this to 5.7 to 10.5% at one look, against a nominal 5%. Across the code's sequential looks from 20 to 60 sessions, 21% of zero-edge paths show 'passed', and in the auto-lane 'passed' produces a paste-ready promotion snippet.
- C1-04 MEDIUM: the trial counts shadow-accepted sessions independently of trader activity, counts all outcomes regardless of session acceptance, and does not exclude infrastructure exits. R is broker paper-fill P&L minus modelled fees over post-cap planned risk (about 0.075% of equity, measured from the live ask).
- Calendar time for the ORF trial, assuming activation on 23 Sep 2026: the earliest terminal verdict follows the 20th accepted session on 21 Oct 2026. Most zero-edge paths run to review_required at the 60th, about 17 Dec 2026. In practice C1-01, C1-03 and the sticky operator_pause (set on every SIGTERM; cleared only by a manual resume that needs an empty local book) mean neither date is reachable without manual state edits.
- C1-06 MEDIUM (VERIFIED): the run loop sleeps up to 60 s through SIGTERM while the supervisor kills after 15 s. About three quarters of routine stops skip the trader's own flatten and go through the 'unresponsive trader' recovery path, with its B-01 id reuse.
- LOW: the config audit cannot represent a revert and the dashboard then shows the wrong 'current' (C1-07, VERIFIED). Each journal row costs 6 connections and 5 commits, about 10.7 ms (C1-08, VERIFIED). One outcome without a fill price voids the whole verdict (C1-09).
- Roughly 15% of the group's lines can change a trade decision, about 51% are evidence, identity and audit validation (all 662 lines of paper_trial_operator serve an evidence-empty cancellation), and about 27% are persistence plumbing. Several helpers are dead (config_version_at, config_history, journal_insert, two schema constants).
