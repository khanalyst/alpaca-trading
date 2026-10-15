# Review handoff: G2a diagnostic accounts, IBR diagnostic, trader research and trader diagnostics (coordinator review)

Method: done directly by the coordinator, with targeted reading, not line by line.

- `research/diagnostic_accounts.py` (1,169 lines): read in full the cost-model wiring (395 to 560), revaluation and close (643 to 760), and entry (990 to 1130).
- `research/costs.py`: read `execution_price` and `fees` (685 to 704) to confirm the sign conventions.
- `research/ibr_diagnostic.py` (1,014 lines): function inventory, and `_arm_result` (677 to 878) read in full.
- `research/trader_research.py` (756 lines): header, arm validation (52 to 116) and report assembly (600 to 700).
- `research/trader_diagnostics.py` (634 lines): function inventory, plus `_session_pnl`, `_pearson` and `_cross_arm`.

## Findings

### G2a-01 Diagnostic-account P&L, fees and quote sides are correct for longs and shorts (positive, VERIFIED)

**Entry.** Longs buy at the ask and shorts sell at the bid. Slippage is applied adversely on top (`research/costs.py:685-693`: the sign is `+1` when `(direction == "long") == entry`). The spread is not charged twice on an executable quote (`per_side_bps`, `:680-683`).

**Exit.** Mirrored: longs sell at the bid and shorts buy at the ask (`research/diagnostic_accounts.py:700-704`, `:655`).

**P&L.**

- Gross is `(exit - entry) * qty` for longs and `(entry - exit) * qty` for shorts (`:709-711`, `:670-672`).
- Fees are notional times `fee_bps`, per leg (`fees(p, p, q) / 2`, `:1041-1042`, `:706-707`).
- Net is gross minus both fees (`:712`).

**Order of operations.**

- Quotes must be forward-observed, on the policy's feed and provider, and available before the decision time (`_fill_matches_quote`, `:484-498`).
- One position per symbol is enforced (`has_open`, `:1002`).

### G2a-02 The IBR diagnostic reconciles realised P&L three ways and refuses ambiguous arms (positive, VERIFIED)

`_arm_result` refuses an arm when any of these holds:

- the sum over closed positions, the sum over emitted rows and the account's `realized_pnl` do not all agree to 1e-9 (`research/ibr_diagnostic.py:808-818`);
- one symbol-session holds more than one position (`:707-712`).

Every opportunity without a trade is kept as an explicit refused or no-signal row with a reason, rather than being dropped. This is the right way to keep censored opportunities visible.

### G2a-03 Diagnostic accounts inherit the runtime's bracket anchoring, which is faithful but distorts R (INFO, VERIFIED)

- Stop and target come from the plan, which is anchored to the signal bar's close.
- The entry price is the executable quote plus slippage (`research/diagnostic_accounts.py:1039-1054`).

This reproduces the live defect B-02 rather than hiding it, so diagnostic and live results stay comparable. But per-trade R multiples are measured from a stop distance the trade never had.

### G2a-04 Cross-arm dependence is almost never measurable on the available corpus (LOW, VERIFIED)

- Pairwise correlation of session P&L needs at least 30 shared complete, eligible sessions (`research/trader_diagnostics.py:25`, `:477-505`).
- Any session with a right-censored or incomplete row is dropped from an arm's series (`:299-335`).
- With 20 to 40 recorded sessions, most of them rejected by the zero-tolerance session acceptance (C2-02), nearly every pair among the 55 to 70 arms reports `insufficient_shared_sessions`.

The report says so honestly ("overlapping_variants_are_not_independent_or_a_portfolio"). In practice, though, the overlap between arms, which drives the multiple-testing burden, is unknown.

### G2a-05 `trader_research.py` hard-codes the cardinalities of one historical experiment (MEDIUM, over-engineering, VERIFIED)

Validation requires:

- exactly 55 sealed comparison arms, followed by 12 cost-compatible and 3 deadline-compatible arms, for totals of 67 or 70 (`research/trader_research.py:64-110`);
- a refresh of exactly 31 reports, split into 24 rule and 7 IBR (`:629-636`).

Any change to a cohort needs code edits in several places, and the module cannot run a new question without becoming another cohort generation. This is part of the pattern found elsewhere: rule schemas v1 to v7, and six cohort modules (`mechanism_cohort`, `mechanism_cohort_v2`, `mechanism_cost_cohort`, `mechanism_deadline_cohort`, `mechanism_refresh_cohort`, `diagnostic_cohort_contract`). Experiment history has been encoded as code paths rather than data.

## Summary

- The diagnostic accounting is correct and conservative: proper quote sides, adverse slippage, per-leg fees, three-way reconciliation, and explicit refused rows.
- The weaknesses are scope and power rather than arithmetic: cross-arm dependence is unmeasurable at this sample size, and the harness is welded to one experiment's arm counts.
