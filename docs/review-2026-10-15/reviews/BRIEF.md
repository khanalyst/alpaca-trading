# Review brief (shared by every review agent)

Repository: /home/user/alpaca-trading (branch claude/laughing-fermat-83p4gd). Read-only review: do NOT edit, commit, or push anything in the repository. The only file you write is your own report.

## What the system is

An intraday (same-day, flat by close) US ETF trading agent for the Alpaca broker API (paper by default), universe of 24 ETFs in config.yaml, one-minute bars on the free IEX feed. It has: a recorder that writes a market-data corpus, a research "strategy factory" that searches a bounded rule grammar of ~12 families (opening-range breakout/fade, momentum, mean reversion, trend pullback, volatility breakout, volume breakout, VWAP reversion, VWAP trend, range expansion, opening drive, cross-sectional residual) plus an "IBR" (inside-bar/initial balance range) family, statistical gates (FDR, clustered inference, LORD++), a broker-free live shadow runner, an edge ledger, preregistered hypotheses, a paper-trial lane, and a deterministic trader with risk limits. Signals are evaluated on completed one-minute bars; entry is market order; exits are bracket stop/target, time stops, and force-flat before close.

## Your two lenses

1. Quant researcher: is each hypothesis well-posed, is the statistics correct, is there look-ahead, survivorship, selection bias, overfitting, wrong cost model, wrong inference unit, wrong data assumption (IEX vs SIP, bar timestamps, partial bars), etc.
2. Veteran intraday trader with a long, profitable record: would this actually make money after costs on Alpaca? Are entries/exits/stops/sizing sensible, are order types right, what about fills, slippage, spreads, partial fills, halts, opening auction, closing, PDT rule, short-selling borrow/locate and hard-to-borrow, uptick rule (Reg SHO 201), buying power/margin, Alpaca API specifics (bracket order rules, OCO, order replace, websocket vs polling, rate limits 200 req/min, clock/calendar endpoints, IEX feed only ~2-3% of volume, data delay rules), market-on-close, time zones, DST, early closes, etc.

## Evidence rules (strict, non-negotiable)

- Read every file assigned to you IN FULL (use Read with offset/limit for large files; keep going until the last line). In your report, list every assigned file with its line count and confirm you read it to the end. If you could not finish a file, say so explicitly; do not pretend.
- Every finding MUST cite `path:line` (or a line range) AND include a short verbatim quote (1 to 4 lines) copied from the file. No quote, no finding.
- Do not invent behaviour. If something depends on a file outside your assignment, you may open that file to check, but mark the finding as cross-group and cite the external line too. If you did not check it, write "UNVERIFIED: needs check in <file>".
- If you run code to verify a claim, include the exact command and the relevant output lines. Use the interpreter python (locked requirements) (all requirements are installed there). Do NOT run the whole test suite (it is already being run separately); running a single test module or a small snippet is fine. Do not make network calls to brokers.
- Confidence labels: VERIFIED (you read the code path end to end, or ran it), LIKELY (strong reading, one link not traced), SPECULATIVE (plausible concern, not traced). Prefer fewer VERIFIED findings over many speculative ones.
- Be calibrated: do not inflate severity. A cosmetic issue is LOW. Reserve CRITICAL for things that lose money, place wrong orders, break risk limits, corrupt evidence, or make research conclusions invalid.
- Use British English. No emojis. No em dashes (use commas, colons or parentheses instead).

## Report format (write to the path given in your task, Markdown)

```
# Review: <group name>

## Files read
| file | lines | read to end? |

## What this part of the system does (as implemented, with citations)
Short, factual. For strategy groups: list each hypothesis/edge/signal, its exact entry/exit/stop/filter logic and parameters as coded, with path:line.

## Findings
For each finding:
### <ID e.g. B-07> <one-line title>
- Severity: CRITICAL | HIGH | MEDIUM | LOW
- Category: incorrect | broken | missing | incomplete | stale | over-engineered | wrong-assumption | dead-code | test-gap | doc-drift
- Location: path:line[-line]
- Evidence: (verbatim quote)
- Why it matters (trading or research impact):
- Suggested fix:
- Confidence: VERIFIED | LIKELY | SPECULATIVE (+ how verified)

## Over-engineering / simplification opportunities
(with citations; quantify where possible, e.g. lines of code that serve no decision)

## Cross-group questions
Things someone reviewing other files must check, with the exact symbol and file.

## Summary
5 to 10 bullets, most important first.
```

Finish by replying with: the report path, the number of findings by severity, and your top five findings in one line each. The report file is the handoff; make it self-contained.

## Resilience (added after usage-limit interruptions)

- Write your Markdown report to disk EARLY and update it after every two or three files, marking unfinished sections "IN PROGRESS". If the report file already exists when you start, read it first and continue from where it stops instead of re-reading files it already covers.
- Your final answer is a structured object (schema enforced). The Markdown report on disk and the structured object must agree.

## Coordinator facts already verified (do not re-derive; cite them if relevant)

1. Full suite on the current head (Python 3.13, requirements.lock.txt): 2,359 tests discovered in 164 modules; runtime 994 OK, edge 61 OK, factory 6 OK, research 1,298 with ONE failure: tests/research/test_llm_strategy.py:156 test_vehicle_provider_schema_excludes_v3_and_v4_for_options (equity schemas now include rule-strategy.v6 and v7; test expects v1 to v5). docs/current-findings.md claims "719 tests across 49 modules".
2. agent/contracts/rule.py:76-77 MIN_STOP_DISTANCE_BPS = 30.0; rule.py:2681 `distance = max(atr * spec["stop_atr"], close * floor_fraction)`. ATR is a 14-bar ONE-MINUTE ATR (rule.py:1921-1931). On fresh consolidated one-minute data for the 24 ETFs (14 Sep to 13 Oct 2026), median absolute one-minute return is 0.6 to 3.0 bps (HYG 0.64, SPY 1.11, SLV 2.97), so the stop is almost always the 30 bps floor and the target (target_r 2.0) is 60 bps.
3. The cost-compatible, deadline-compatible and refresh mechanism cohorts (research/mechanism_cost_cohort.py, mechanism_deadline_cohort.py, mechanism_refresh_cohort.py) use schema rule-strategy.v7 with min_stop_distance_bps 100.0, i.e. 100 bps stop and 200 bps target with 15 to 60 bar holds.
4. The September audit scripts (docs/audit-2026-09-21/forward_costs.py, harness.py) loop `for i in range(60, len(sr) - 1)`, i.e. they skip the first 60 one-minute bars of every session, while the ORB/ORF diagnostic baselines have entry_after_minutes 0 and range_minutes 15.
5. Running the test suite created runtime/research/edge_lab.sqlite3 inside the repository working tree (gitignored).
6. This container has no Alpaca credentials and no access to the VM's recorder corpus.
7. Coordinator-verified since: (a) close and flatten client_order_ids are `edge-close-<symbol>-<reason>` / `edge-flatten-<symbol>-<reason>` with no trade or date component (agent/execution_lifecycle.py:1051-1052, agent/runtime_control.py:197-199, agent/market_entry_risk.py:856-880) and agent/alpaca_provider.py:546-550 returns a previously used order instead of submitting; (b) exit_before_minutes means minutes BEFORE 16:00 (agent/contracts/rule.py:1103-1107), so 360 is a 10:00 deadline.
8. FINRA retired the Pattern Day Trader rule effective 4 June 2026 and Alpaca removed pattern_day_trader, daytrade_count and daytrading_buying_power from the API on 6 July 2026; the repository already treats pattern_day_trader as optional. Do not report missing PDT handling as a defect (easy_to_borrow checks for shorts remain relevant).
9. SEC Section 31 fee: $20.60 per million on sales from 4 April 2026 (about 0.21 bps on the sell leg only). Alpaca charges no commission on API equity trades.
10. Runtime facts already established by the B_execution_broker review (do not re-derive): market entries are converted to DAY limit orders at the IEX quote with broker-resident bracket legs; size = min(risk/stop, 25% notional) so the notional cap binds on every 30 bps stop (planned risk about 0.075% of equity); REST polling once per minute; every transient read failure or submit rejection flattens the whole book.
11. Completed review handoffs exist for A_signals and B_execution_broker at reviews/handoff-A_signals.md and handoff-B_execution_broker.md. Consult them only for cross-group context; do not re-review those files.
