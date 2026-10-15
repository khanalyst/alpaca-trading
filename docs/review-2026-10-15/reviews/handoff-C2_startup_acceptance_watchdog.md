# Review handoff: C2_startup_acceptance_watchdog

Source: workflow structured result (reviewer could not write files; this file is generated from the journal).

## Files read

| file | lines | read to end |
|---|---|---|
| agent/runtime_inspection.py | 753 | yes |
| agent/startup_edge_policy.py | 724 | yes |
| report.py | 424 | yes |
| deploy/paper-orb.config.json | 158 | yes |
| deploy/paper-orf.config.json | 158 | yes |
| deploy/cancel_paper_trial.py | 68 | yes |
| deploy/session_acceptance.py | 1668 | yes |
| deploy/trader_supervisor.py | 241 | yes |
| deploy/watchdog.py | 311 | yes |

## What this part does (as implemented)

STARTUP GATE CHAIN (agent/startup_edge_policy.py). preflight() (318-427) runs once per process and is cached (320-321). It checks, in order: provider paper scope (325-328); credentials (329-333); endpoint exactly https://paper-api.alpaca.markets with no port or userinfo (334-348); feed names (349-360); account, clock, calendar and the list of all active US equities (363-373); account id, active status and equity > 0 (374-380); `_authorize_feeds` (385-388, 261-316: research or validated-equity lanes need the exact configured iex or sip feed, option lanes need OPRA); every universe symbol active and tradable (389-405); account fingerprint binding (406-408); and a journal write (415-426). _ensure_order_ready() (429-481), called at the top of every cycle (agent/engine_cycle.py:391), then requires: `_state_ready` (430-431, latched False by a single check_journal failure at 432-437); not KILLED and `operator_pause is False` (438-442); a flat local book during a paper-trial replacement (443-449); preflight (450-455); a one-time reconcile (456-462); a replacement edge refresh (463-472); and the forced startup cleanup `_enforce_intraday_cleanup(self.market.clock(), reason="startup_reconciliation", force=True)` (473-477). force=True skips the in-session early return (495-497), so flatten_all runs on every start (B-07 confirmed). The Engine constructor already does the same under a temporary lock (agent/engine.py:252-254), and success sets `_startup_cleanup_checked` (504). An incomplete cleanup commits operator_pause True (RUNNING to PAUSED), writes a degraded heartbeat and raises (507-516). run() then refuses to start (agent/runtime_control.py:635-644), and the supervisor's recovery parks the deployment. _latest_entry_allowed (518-528) is a fixed New York wall-clock cutoff (default 15:00), truncated to the minute. _refresh_edge (530-724): in the paper-trial lane, an eligible trial swaps self.cfg to the frozen trial config (555) and resumes; otherwise it pauses. Other lanes resolve validated variants. _paper_selection_status (21-201) is observability only.

SESSION ACCEPTANCE (deploy/session_acceptance.py). The deployed shadow loop calls record_poll (1477-1559) via deploy/shadow.py:115-125 at its interval (compose.yaml:330-331, default 30 s). Before the close it appends one fsynced sample; at or after the close it writes one report (finalize_session 1380-1432). A session is accepted only if the union of all reasons is empty (1112). Per-sample reasons (sample_reasons 461-658), every one required at every sample:
- sample schema, timestamp and Alpaca-calendar chronology (464-492); freshness capped at 30 s (172-178, 493-503); expected symbols (504-510);
- recorder status, fresh and session status known (515-521);
- for each of the 24 symbols: quote age known, >= 0 and <= 30 s (531-537); bar event-age consistency (544-549); completed-bar publication lag <= 30 s past the next-bar deadline, i.e. latest bar boundary no older than 90 s (550-562, 181-220);
- cadence telemetry and partition bytes present (563-576);
- shadow running, ok, fresh, no last_error, arm errors present-and-empty (577-598); health._shadow_diagnostic_summary ready (606-617);
- _diagnostic_reasons (699-844): diagnostic schema and authority contract; activation active and pre-session; source lag <= 30 s; 24- or 31-arm cohort contract; every family has a baseline and a variant; single code and cohort identity; every arm cursor valid, post-activation, not stale (> 30 s), all identical and all exactly at the WAL head (834-839); processed-event totals;
- warmup identity and chronology (621-631); deployment, code, cohort and activation identities, plus recorder-versus-shadow deployment parity (632-655); non-authorizing flags (656-657).
Session-level checks (summarize_session 858-1152): closed at report time; first sample <= open+60 s; last >= close-60 s; no gap > interval+5 s (912-923); every per-sample reason is added to the session reasons (945-963); one warmup; no identity or cohort drift; stable pre-session activation; no cursor regression; every arm progressed (1038-1079); cadence and duration evidence (1102-1105). The paper trial counts only accepted reports (agent/paper_trial.py:527-528). Its verdict stays running while accepted sessions are below min_sessions 20 (research/trial.py:92-97). A pass needs the 95% session-cluster lower bound to clear min_mean_r 0 (research/trial.py:118-149).

WATCHDOG (deploy/watchdog.py). Every 30 s (compose.yaml:138-139) it takes the mode-scoped non-blocking flock first (169; agent/state.py:467-475). If the lock is held it is inert (171-179). Otherwise it reads the heartbeat and broker positions (188-189). decide() (82-105) acts only when the heartbeat is older than 300 s, or the supervisor is parked, and positions exist. It then re-checks (197-204), binds the account fingerprint (206-210) and flattens through a light Engine with the transferred lock (213-242). It has no notion of the market close and never acts while a trader process holds the lock.

SUPERVISOR (deploy/trader_supervisor.py). It parks with no child while operator_pause is set (112-127, 148-152), writing the parked heartbeat the watchdog treats as ACT_PAUSED. Otherwise it launches `main.py run` (154-156). monitor() (77-101) counts progress only for heartbeats carrying the child pid with a strictly increasing timestamp; with no progress for 300 s it sends SIGTERM to its own process group, waits 15 s, sends SIGKILL and waits 15 s (53-74). recover() then runs run_once(terminated_child=True) (104-109), which commits operator_pause before broker I/O (watchdog.py:184-187) and flattens. It never relaunches (185-186). For a trader hung in an HTTP call with no timeout (B-10), detection is at most about 300 s after the last 60 s-cadence heartbeat plus up to 30 s of termination. Force-flat uses the calendar close (agent/alpaca_session.py:80-84), so a hang at or before the force-flat minute (15:50, or 12:50 on a 13:00 early close) is recovered by about close minus 4.5 minutes. A trader that is alive but held (finding C2-01) is never recovered.

B-01 INTERACTION. Every flatten reason in these files is a fixed string: startup_reconciliation (startup_edge_policy.py:476), outside_regular_session (engine_cycle.py:428), before_close (engine_cycle.py:447), and watchdog_stale_heartbeat, watchdog_paused_without_trader and supervisor_terminated_unresponsive_trader (watchdog.py:222-226). They become `edge-flatten-<SYMBOL>-<reason>` (agent/runtime_control.py:197-199). Per coordinator fact 7(a), the provider returns the old order, and `_flatten_all_impl` will not resubmit a non-failed close (runtime_control.py:182-185). So from the second incident per symbol and reason the recovery flatten is a no-op, and the watchdog reports degraded every 30 s.

INSPECTION, REPORTING, CANCEL. agent/runtime_inspection.py builds a read-only status. It copies the journal DB and WAL to a temp dir and checks the schema (167-247), validates state.json safety fields (123-140), local book flatness (323-341) and blockers (358-387), and checks trial identity against the configured descriptor (389-436). It takes a broker snapshot with fingerprint and clock-age checks (457-584) and derives paused_flat_verified for paused deployments (744-752). report.py summarises the trades journal: it groups open and close rows by parent_trade_id, setup_id or setup_key (92-123; both runtime open and close rows carry setup_id, agent/execution_lifecycle.py:878, 1925), counts completed parents (166-195) and keeps gross, fees and net with provenance (198-308, 316-375). deploy/cancel_paper_trial.py is a 68-line CLI requiring the trial id, incumbent identity and a reason (23-30), and delegates to agent.paper_trial_operator.cancel_paper_trial (41-46).

PAPER PROFILES. deploy/paper-orb.config.json and paper-orf.config.json are identical except trial_id and variant_id (133-134: paper-orb-baseline-20260914-v1 / rule.opening-range-breakout.0eb200d3136d80ee versus paper-orf-baseline-20260923-v1 / rule.opening-range-fade.d5785d9e70b56def) and int-versus-float formatting. Shared settings:
- paper, IEX, shares (2-8, 57); force-flat 10 minutes before the close, entry rejection 5 minutes before it, latest entry 15:00 (15-16, 66-67);
- risk 0.5% per trade, 2% daily loss, 25% notional, 50% gross (69-75); stress 9 bps with ratio 0.3 (79-80; see B-05); costs 4+6+0.5 bps (94-96);
- trial floors of 20 sessions and 20 trades, mean R 0 (124-130); a shared acceptance root /app/shadow/session-acceptance with max_review_sessions 60 (131-137);
- strategy_llm enabled (138-148); 60 s cycle (155-157).

## Findings

### C2-01 A paused or not-ready trader skips the force-flat while keeping a fresh heartbeat, so neither the supervisor nor the watchdog intervenes
- Severity: HIGH  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/startup_edge_policy.py:430-442; agent/engine_cycle.py:391-398, 446-449 (cross-group); agent/market_entry_risk.py:520-531, 544-549 (cross-group); deploy/trader_supervisor.py:94-99; deploy/watchdog.py:169-179`
- Evidence: agent/startup_edge_policy.py:439-442: `if (runtime.get("state") == state.KILLED or
        runtime.get("operator_pause") is not False):
    self._preflight_error = runtime.get("kill_reason") or "operator_pause"
    return False` | agent/engine_cycle.py:391 `if not self._ensure_order_ready():` ... 398 `return {"action": "hold", "reason": reason}`, and only later at 446-447 `if self.market.should_force_flat(now):
    closed = self.flatten_all("before_close")` | agent/market_entry_risk.py:525-528 `if (not complete or position_error is not None or
        reason in pause_reasons):
    try:
        state.commit({"operator_pause": True},` | deploy/trader_supervisor.py:94-95 `if (payload.get("pid") == child.pid and math.isfinite(stamp) and
        latest_stamp < stamp <= wall_time() + 1.0):`
- Why it matters: `_fail_closed` is the B-03/B-04 path. It latches operator_pause whenever its whole-book flatten is incomplete or the reason is in pause_reasons (for example account_unavailable). It then returns normally, so the run loop keeps cycling (agent/runtime_control.py:650-661). Every later cycle fails `_ensure_order_ready` and returns hold before three exposure controls: `_monitor_positions` (engine_cycle.py:466), the daily loss stop (473-495) and `should_force_flat` (446). What that leaves depends on how the flatten failed. If the flatten's own broker read failed (runtime_control.py:134-138), positions keep their DAY bracket legs only until the close and are then carried overnight by an intraday system. If it failed after `_cancel_protective_legs` (runtime_control.py:189-194), the position is unprotected at once. The held cycle writes a degraded heartbeat with the trader's own pid every cycle (engine_cycle.py:394; agent/state.py:319-320). The supervisor counts that as progress, and the trader keeps its persistent run lock, so the watchdog stays inert (watchdog.py:171-179). Nothing automated flattens before the close; the supervisor's stated purpose of closing the hung-owner gap does not cover an alive-but-held owner. A single transient check_journal() failure has the same effect without a durable pause. `_state_ready=False` (startup_edge_policy.py:435) short-circuits lines 430-431 for the life of the process, and `_preflight` is reset only by an operator resume (runtime_control.py:379).
- Suggested fix: Gate only new entries on `_ensure_order_ready`. Always run exposure management (protection monitoring, the daily loss stop and should_force_flat) while the broker is reachable, even when paused. Alternatively, when operator_pause latches with residual exposure, leave the run loop so run()'s finally block flattens and the supervisor recovers. Another option: publish residual exposure in the heartbeat and let the supervisor treat 'held with positions within N minutes of the close' as unresponsive. Retry check_journal instead of latching `_state_ready=False`.
- How verified by reviewer: Read the call chain end to end:
- run loop: agent/runtime_control.py:650-661; run_once wrapper: agent/engine_cycle.py:161-178;
- `_run_once_impl` ordering: 391-449, 466, 473-495;
- `_fail_closed` normal return: agent/market_entry_risk.py:502-549;
- `_flatten_all_impl` early returns and per-symbol leg cancellation: agent/runtime_control.py:103-215;
- heartbeat payload: agent/state.py:313-325;
- supervisor progress rule: deploy/trader_supervisor.py:77-101;
- watchdog lock gate: deploy/watchdog.py:169-179, with acquire_run_lock at agent/state.py:467-475.
Not executed against a fake broker.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C2-02 Session acceptance is zero-tolerance per sample and per symbol, so one IEX minute without a trade rejects the whole session and the paper trial cannot accrue sessions
- Severity: HIGH  |  Category: wrong-assumption  |  Confidence: LIKELY
- Location: `deploy/session_acceptance.py:172-178, 531-537, 550-562, 945-963, 1112; cross-group: deploy/recorder.py:141, 1954-1955, 2369-2371; agent/paper_trial.py:527-528; research/trial.py:92-97; compose.yaml:35-36, 330-331`
- Evidence: deploy/session_acceptance.py:561-562 `elif publication_lag > freshness:
    reasons.append(f"symbol:{symbol}:bar_publication_stale")`; 962-963 `reason_counts[reason] += 1
            reasons.append(reason)`; 1112 `"accepted": not reasons,` | versus deploy/recorder.py:141 `DEFAULT_BAR_GAP_MINUTES = 5` and 1954-1955 `if current - previous <= maximum:
        return False`
- Why it matters: Every reason from every sample joins the session's reasons, and the session is accepted only when that list is empty. The deployed shadow loop samples every 30 s, about 780 samples per full session. Each sample checks, for all 24 symbols, quote age <= 30 s and completed-bar publication lag <= 30 s.
Alpaca IEX one-minute bars exist only for minutes with an IEX print. If one symbol has a single empty IEX minute, its bar watermark stops advancing, and every sample from 90 s after the last bar boundary until the next bar fails. The recorder itself treats bar holes of up to 5 minutes as normal and defaults to the non-strict observe policy (recorder.py:2369-2371), so the two components disagree about what IEX data looks like.
Rejected reports are silently not counted (paper_trial.py:527-528). The verdict stays running while accepted sessions are below min_sessions 20 (research/trial.py:92-97; profiles line 126), so the trial can neither pass nor fail and paper trades accrue with no stopping rule. docs/current-findings.md:3 reports 'zero accepted sessions and outcomes' for the frozen ORB trial, which is consistent with this.
The quote check has no margin either: its 30 s cap (session_acceptance.py:178) equals the recorder's default 30 s polling interval (compose.yaml:35-36).
- Suggested fix: Judge acceptance against what trading needs:
- require bar continuity only within the recorder's own gap bound (5 minutes), or count a symbol-minute as healthy when the recorder polled successfully even if IEX printed nothing;
- accept a session when, for example, at least 98% of samples are healthy and no outage exceeds N minutes;
- limit the symbol checks to the incumbent's tradable universe;
- set the quote-age cap clearly above the recorder interval (for example twice the interval);
- surface the dominant rejection reasons in status.
- How verified by reviewer: The aggregation and thresholds are VERIFIED by reading lines 172-178, 461-658 and 858-1152 to the end.
The bar rule is VERIFIED by running `python (locked requirements) -I -c "...from deploy.session_acceptance import _completed_bar_publication_lag, _freshness_threshold..."` with the last completed-bar boundary at 14:01:00Z (the 14:01 minute missing). Output: `2026-10-14T14:02:30+00:00 lag 30.0 ok`, `2026-10-14T14:02:31+00:00 lag 31.0 stale`, `2026-10-14T14:02:59+00:00 lag 59.0 stale`.
The frequency of empty IEX minutes for the thinner ETFs (VO, VB, XLRE, XLB, XLU) was not measured, because this container has no IEX corpus or credentials (coordinator fact 6).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C2-03 Acceptance for a single-strategy paper trial requires the whole 24 or 31-arm research cohort and its telemetry to be flawless
- Severity: MEDIUM  |  Category: over-engineered  |  Confidence: VERIFIED
- Location: `deploy/session_acceptance.py:570-576, 589-598, 606-620, 649-655, 684, 740-743, 775-780, 809-813, 834-839, 1074-1079, 1102-1105`
- Evidence: deploy/session_acceptance.py:684 `if not expected_ids or len(expected_ids) not in (24, 31):`; 834 `if (wal_head is None or caught_up is not True or` ... 839 `reasons.append("arm_cursor_behind_wal")`; 654-655 `elif identities.get("deployment_recorder") != identities.get("deployment_shadow"):
                reasons.append("deployment_identity_drift")`; 575-576 `if _finite(partition_bytes) is None or float(partition_bytes) < 0:
        reasons.append("partition_bytes_missing")`
- Why it matters: The frozen ORB or ORF incumbent trades one variant, but its sessions count only if all of the following hold:
- every arm of the diagnostic shadow cohort (all research families plus IBR) is error-free and advances during the session;
- every arm sits exactly at the WAL head at every sample, and all arms share one code identity and one cohort identity;
- recorder and shadow carry identical deployment identities all day, so a recorder-only redeploy rejects the session;
- partition-size, cadence and poll-duration telemetry are present.
None of these bear on whether the incumbent's own signals and fills were valid, so a fault in an unrelated research arm deletes forward evidence for the incumbent and compounds C2-02.
- Suggested fix: Split the check in two: (a) an incumbent acceptance check covering market data for its symbols, its own arm's progress and identity, and runtime and broker health, which alone gates the trial; (b) a separate research-cohort completeness report that never gates the paper trial.
- How verified by reviewer: Read sample_reasons (461-658), _diagnostic_reasons (699-844) and summarize_session (858-1152) to the end. Each listed reason is appended unconditionally to the session reasons, which alone decide `accepted` (1112).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C2-04 Watchdog and supervisor recovery hold the exclusive run lock across broker calls that have no timeout (B-10 interaction)
- Severity: MEDIUM  |  Category: incorrect  |  Confidence: LIKELY
- Location: `deploy/watchdog.py:169, 184-189, 197-198, 210-226; deploy/trader_supervisor.py:104-109, 179-186; cross-group agent/runtime_control.py:604-605, agent/state.py:467-475`
- Evidence: deploy/watchdog.py:169 `handle = agent_state.acquire_run_lock()` followed, with the lock held, by 188-189 `heartbeat = _read_json(agent_state.HEARTBEAT_FILE)
        positions = provider.positions()` | agent/runtime_control.py:604-605 `if not self._acquire_lock(persistent=True):
            raise AlpacaError(f"another {self.mode} runtime process already holds the run lock")`
- Why it matters: When no trader holds the lock (supervisor parked under operator pause, or between a child exit and its restart), each 30 s watchdog tick takes the exclusive lock first and then calls the broker. It does so even when the heartbeat is fresh and no action can follow. With no HTTP timeout (B-10), one hung call keeps the lock indefinitely, and this sequence repeats:
1. The next trader child exits with 'already holds the run lock'.
2. The supervisor's recover() gets the inert verdict, so there is no flatten and `ok` is False (trader_supervisor.py:181), and it returns 1.
3. The container restarts and the loop starts again.
The supervisor's own recover() runs in-process, holding the lock, with the same un-timed client. If it hangs, the documented fallback (the watchdog) cannot take the lock either. The supervisor does recover a hung trader correctly (SIGTERM then SIGKILL to its own process group, trader_supervisor.py:53-74). So B-10 is covered for the trader but not for the recovery processes themselves.
- Suggested fix: Take the decision snapshot without the lock, and acquire the lock only to act (then re-check). Bound every recovery broker call with a timeout (B-10), and bound run_once and recover with an overall deadline, for example by running recovery in a child process the supervisor can kill.
- How verified by reviewer: Lock semantics VERIFIED:
- non-blocking flock held until release: agent/state.py:467-489;
- watchdog lock scope: deploy/watchdog.py:168-246;
- trader start refusal: agent/runtime_control.py:604-605;
- supervisor recovery path: deploy/trader_supervisor.py:104-109, 179-186.
The hang itself rests on B-10 (no HTTP timeouts), which I did not re-derive.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### C2-05 latest_entry_time is a fixed 15:00 wall clock, so on 13:00 early closes entries run until one minute before the force-flat
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/startup_edge_policy.py:518-528; cross-group agent/engine_cycle.py:446-449, 528, 536; agent/alpaca_session.py:72, 84`
- Evidence: agent/startup_edge_policy.py:520 `raw = str(strategy.get("latest_entry_time", "15:00"))`; 528 `return local.time().replace(second=0, microsecond=0) <= cutoff`
- Why it matters: On a normal day the last entry is at 15:00, 50 minutes before the 15:50 force-flat. On early-close days (13:00 close, about three a year) the 15:00 cutoff never binds. Entries are then stopped only by the force-flat branch at 12:50, which returns before the entry code (engine_cycle.py:446-449). An entry at 12:49 is therefore flattened a minute later, paying spread and slippage both ways for no holding period. The truncation to the minute also admits entries until 15:00:59.
- Suggested fix: Express the cutoff relative to the calendar close (for example the earlier of 15:00 and close minus 60 minutes), and compare full timestamps rather than truncated minutes.
- How verified by reviewer: Read _latest_entry_allowed and the cycle ordering:
- agent/engine_cycle.py:446-449: the force-flat branch returns early;
- 528: can_enter, close minus 5 minutes per agent/alpaca_session.py:66-73;
- 536: latest entry.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### C2-06 report.py headline P&L reads 0.0 'no_closed_trades' when closed trades exist but any of them lacks P&L
- Severity: LOW  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `report.py:311-313, 346-350`
- Evidence: report.py:346-349 `"realized_pnl_usd": (net if net is not None else gross
                             if gross is not None else 0.0),
        "realized_pnl_basis": ("net_pnl" if net is not None else
                                "gross_legacy_unknown_cost" if gross is not None`
- Why it matters: _total returns None if any parent lacks the field. A single legacy or partially attributed close row therefore turns realized_pnl_usd into 0.0, with basis 'no_closed_trades', while closed_trades is positive. The CSV path (csv_report, 384-393) exists for cron artifacts, where a false zero reads as a flat P&L.
- Suggested fix: When parents exist but neither total is available, emit None with a basis such as 'economics_unavailable'. Keep 0.0 only when there are no closed parents.
- How verified by reviewer: Read _total (311-313) and _summary (316-375).
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### C2-07 Paper profiles carry inert or misleading content, and ORB and ORF differ only in trial and variant ids
- Severity: LOW  |  Category: stale  |  Confidence: LIKELY
- Location: `deploy/paper-orf.config.json:58-64, 133-134; deploy/paper-orb.config.json:79, 98, 138-141`
- Evidence: deploy/paper-orf.config.json:59 `"breakout_buffer_bps": 5,` and 63 `"min_ibr_width_atr": 0.25,` | deploy/paper-orb.config.json:79 `"stressed_cost_scenario_bps": 9,` and 98 `"provenance": "shipped_conservative_v1_plus_25bps_stress",`
- Why it matters: The two profiles differ only in trial_id and variant_id (lines 133-134) and int-versus-float formatting.
- Both carry the generic ORB/IBR knobs from agent/config.py:85, such as the breakout buffer and IBR widths. The rule lane takes its parameters from the frozen variant spec (for example agent/contracts/rule.py:2325 reads spec["range_minutes"]), so the ORF (fade) profile shows breakout and IBR parameters that do not govern it.
- The cost provenance label names 25 bps stress while the profile's admission scenario is 9 bps. README.md is itself inconsistent: line 346 says the shipped runtime uses 25 bps, while lines 360 and 367 say 9 bps.
- Both profiles enable strategy_llm (OpenAI model gpt-5.6-terra) in a trader config.
- Suggested fix: Drop or annotate the keys the paper-trial lane ignores, and align the provenance label with the configured stress. Keep one profile parameterised by trial id and variant id. Disable strategy_llm in trader profiles unless the trader uses it.
- How verified by reviewer: Ran `diff deploy/paper-orb.config.json deploy/paper-orf.config.json`: only trial_id, variant_id and number formatting differ. Ran `grep -rn plus_25bps_stress`: it appears only in config.yaml:71, agent/config.py:90 and the two profiles. Did not trace every consumer of the top-level strategy keys or of research.strategy_llm.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

## Over-engineering

Of the 4,505 lines in this group, about 460 make trading-safety decisions: the startup gate chain, cleanup and entry cutoff (agent/startup_edge_policy.py:318-528, about 210 lines) and the watchdog and supervisor core (deploy/watchdog.py:82-246 and deploy/trader_supervisor.py:53-211, about 250 lines). The rest is evidence and status plumbing.

deploy/session_acceptance.py (1,668 lines):
- It produces one boolean, consumed only by agent/paper_trial.py:527-572. In the deployed setup (30 s samples) every full session must pass about 780 samples x (24 symbols x 3 market-data checks + 31 arms x about 6 cursor checks), roughly 200,000 conditions, with zero failures.
- Session-calendar validation is implemented three times (252-278, 469-492, 873-890), and `_epoch` duplicates `_aware_datetime` (70-103).
- There are two collectors with different session-selection rules: record_poll with _selected_session_date (1435-1559), and the CLI monitor (1577-1622, own selection at 1588-1599). Compose uses only record_poll, via deploy/shadow.py:121.
- Report-only statistics (_stats 847-855, _storage_growth 1155-1169, _latest_arm_progress 1172-1189, _partition_info 322-334, the freshness and cadence blocks at 1132-1143) are about 70 lines that no decision reads.

agent/runtime_inspection.py (753 lines):
- Every status call copies the whole SQLite journal and WAL into a temp directory (167-191, 203-210). A concurrent trader write becomes the fatal blocker runtime_journal_snapshot_unstable (line 47), so a status read during a live session can report status_ok false (LIKELY).
- It re-implements the paper-selection contract (587-664, 78 lines) with a different blocker precedence from agent/startup_edge_policy.py:_paper_selection_status (21-201, 181 lines, heartbeat observability only).

report.py (424 lines) keeps legacy row-shape shims: four close-action aliases (84), legacy parent keys (104-123), close-risk fallbacks (159-163). It also has a dead `when` timestamp fallback (302-305), because load_trade_events never selects `when` (36-46).

The deploy/watchdog.py docstring (2-9) justifies the module by the option profile's software stop. Compose deploys it for the shares profile, whose protection is broker-resident, so only its stale-heartbeat path is relevant there.

agent/startup_edge_policy.py preflight downloads every active US equity (agent/alpaca_provider.py:256-272) on each start to validate 24 symbols. That call is large, un-timed (B-10), and runs on every start, and every start also flattens (B-07).

## Cross-group questions

- agent/market_entry_risk.py:_fail_closed (502-549) and agent/engine_cycle.py:_run_once_impl (391-449): is it intended that a latched operator_pause with residual positions disables should_force_flat, _monitor_positions and the daily loss stop for the rest of the process? (See C2-01; the runtime and execution reviewers should confirm.)
- agent/alpaca_provider.py:546-550 plus agent/runtime_control.py:182-185 (B-01): confirm that when close_position returns the previously filled order for a reused `edge-flatten-<SYMBOL>-<reason>` id, _flatten_all_impl never resubmits and returns False after its 6 polls. If so, watchdog and supervisor recovery are permanent no-ops after the first use per symbol and reason, because the reasons are fixed (deploy/watchdog.py:222-226).
- deploy/recorder.py: does a quote row's `as_of` carry the poll time or the IEX quote time? With the default 30 s recorder interval (compose.yaml:35-36) and the 30 s acceptance cap (deploy/session_acceptance.py:178), how often does `symbol:<SYM>:quote_stale` fire?
- VM evidence needed (no access here): which reasons dominate `reason_counts` in /app/shadow/session-acceptance/session-*.report.json for the sessions since 2026-09-14? This would confirm or refute C2-02 as the cause of the zero accepted sessions in docs/current-findings.md:3.
- agent/paper_trial.py:_performance and _confidence (785-833): outcomes from rejected sessions count toward trades and toward bootstrap session clusters but not toward accepted sessions. Is that mismatch intended?
- agent/contracts/rule.py and agent/strategy.py: confirm that the top-level strategy keys (range_minutes, breakout_buffer_bps, min_relative_volume, target_r, min_ibr_width_atr, max_ibr_width_atr) are ignored when strategy id is 'rule' and the paper-trial lane sets self.cfg = trial_runtime.config (agent/startup_edge_policy.py:555).
- README.md:346 says the shipped runtime stress is 25 bps; README.md:360 and 367 say 9 bps (the docs group should reconcile this with B-05).

## Summary

- HIGH C2-01: when operator_pause latches mid-session, the trader stops force-flattening, monitoring positions and enforcing the daily loss stop. This happens, for example, when the B-03/B-04 fail-closed flatten is incomplete; a transient check_journal failure (`_state_ready` latch) does the same. The trader still writes fresh degraded heartbeats, so the supervisor sees progress and the watchdog stays inert behind the run lock. Positions can be carried past the close after their DAY bracket legs expire (startup_edge_policy.py:430-442, engine_cycle.py:391-398 vs 446-449).
- HIGH C2-02: session acceptance needs every 30 s sample to be perfect for all 24 symbols. One IEX minute without a trade in one symbol fails the bar-publication rule (verified by running _completed_bar_publication_lag) and rejects the session. The recorder itself tolerates 5-minute holes. Rejected sessions are not counted, and the trial verdict stays running below 20 accepted sessions, so there is no pass and no failure stop. This is consistent with the zero accepted sessions in docs/current-findings.md:3.
- MEDIUM C2-03: acceptance also requires the whole 24/31-arm research cohort, deployment-identity parity and storage and cadence telemetry to be flawless. None of these bear on the ORB/ORF incumbent's trading decisions.
- MEDIUM C2-04: the watchdog takes the exclusive run lock before un-timed broker reads, and the supervisor's recovery runs in-process with the lock and the same client (B-10). One hung call blocks the trader from starting and leaves no process able to flatten.
- Q3 answer: the supervisor does recover a trader hung in HTTP (B-10) by SIGTERM then SIGKILL of its own process group, within about 5.5 minutes of the last heartbeat. Force-flat uses the calendar close, so 13:00 early closes are covered when the hang starts at or before the force-flat minute. The watchdog has no close awareness at all. All recovery flatten ids are fixed reasons, so B-01 makes recovery a no-op from the second incident per symbol.
- B-07 confirmed: _ensure_order_ready calls _enforce_intraday_cleanup(..., force=True), and force bypasses the in-session return (startup_edge_policy.py:473-477, 495-497). The Engine constructor does the same (engine.py:252-254). An incomplete startup flatten latches operator_pause and stops the start.
- LOW C2-05: latest_entry_time is a fixed 15:00 wall clock, so on early-close days entries are allowed until 12:49, one minute before the force-flat. LOW C2-06: report.py shows 0.0 'no_closed_trades' when closed trades lack P&L. LOW C2-07: the ORB and ORF profiles differ only in trial and variant ids, carry inert ORB/IBR knobs and a 25 bps stress label against a 9 bps setting, and enable strategy_llm.
- Over-engineering: about 460 of the 4,505 lines make trading-safety decisions. session_acceptance.py spends 1,668 lines on one boolean, with triplicated calendar validation and two collectors. runtime_inspection.py copies the full journal on every status call and duplicates the selection-status logic of startup_edge_policy.py.
