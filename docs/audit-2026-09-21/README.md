# Measured audit, 21 September 2026

Five reports, in order:

- `FINDINGS.md`: the audit, and why the system had never produced a trade.
- `REMEDIATION.md`: steps 1, 3 and 4 applied, plus the IBR contract merge.
- `SIGNAL-VALUE.md`: steps 5, 6 and 2, covering exit geometry, signal quality
  and costs. **It overturns a conclusion in `FINDINGS.md`**, and carries a
  correction of its own at the end.
- `RE-AUDIT.md`: session-clustered inference, the preregistered forward test,
  and a second pass over everything changed.
- `FORWARD-2026-09-23.md`: all 24 rule arms net of fees, realistic and
  modelled costs, historical and on the first two sealed sessions, plus the
  walk-forward exit test. Read it last.

This directory holds the evidence.

## Data

44,993 one-minute bars: the exact 24-symbol `config.yaml` universe, 5 regular
sessions (2026-09-15, 09-16, 09-17, 09-18, 09-21), regular trading hours only
(09:30–16:00 New York), fetched from a public chart endpoint and normalised to
the `symbol/timestamp/open/high/low/close/volume/session/minute` shape the
repository's evaluator accepts.

This is a consolidated-tape source, not the deployment's IEX feed. It is used
for geometry and signal-reach measurement, where the consolidated tape is the
more conservative and more representative choice. It is not forward-observed
recorder evidence and cannot authorize anything.

## Scripts

| file | purpose |
| --- | --- |
| `harness.py` | replays one rule spec over the universe using `agent.contracts.rule.evaluate_rule_signal` |
| `runall.py` | runs all 36 rule + mechanism arms, writes `replay-results.json` |
| `survivors.py` | per-session breakdown for the arms that survive a realistic cost |
| `q1trades.py` | post-fix trade counts with the stressed-cost gate applied |
| `q2.py` | 19-session forward returns for the regime question |
| `step5b.py` | 81-point target/hold sweep with per-session values for a split test |
| `step6b.py` | `research.signal_quality` run as the primary instrument (carries a provenance correction) |
| `step6c.py` | the same, honestly labelled, with session-clustered inference, examined sessions only |
| `fetch60.py` | corpus fetch |

Result artifacts: `replay-results.json`, `q1-post-fix-summary.json`,
`q2-19session-summary.json`, `step5-geometry-grid.json`,
`step6-signal-quality.json`, `step6c-clustered-signal-quality.json`,
`prereg-interim-2026-09-22.json` (the first sealed session, descriptive and
`diagnostic_only`), and `spread-reference.json` (diagnostic, not a cost
source).

Signals are evaluated on the completed bar; entry is the next bar's open;
forward returns are signed by the signal's own direction. Outcome walks apply
stop-first tie-breaking, matching `completed_bar_exit_transition`.

## Caveats

Five sessions is four degrees of freedom. Nothing here establishes or refutes
an edge, and the report does not claim otherwise: the per-session table in
section 9 exists specifically to show that the pooled t-statistics are
artefacts of one trending day.

The clustered re-run (`step6c.py`) uses 18 examined sessions, 2026-08-26 to
2026-09-21, from the same public source. Sessions after 2026-09-21 are sealed
for `vwap-reversion-control-adjusted.v1` and are excluded from every
exploratory script.

What five sessions *is* sufficient for, because these are mechanical rather
than statistical facts, is everything in sections 1 through 8 and 10 through 12:
the ATR distribution, the stop-floor binding rate, the IBR width band, the
filter pass rates, the dead spec axes, the bracket-versus-path mismatch, and
the runtime/research config divergence. Those conclusions would not change with
five hundred sessions.

## Coverage

Read in full or in relevant part: all 8 root and `docs/` Markdown files,
`research/README.md`, `research/protocol.md`, `config.yaml`,
`research/variants.yaml`, and roughly 45 Python modules concentrated on the
signal, variant, cost, risk-geometry, gate and factory-search paths.

Not individually reviewed: most of `deploy/`, most of `tests/`, the Alpaca SDK
adapters, and the state/journal/dashboard layers. Those carry operational
rather than strategy logic, and no finding in this report depends on them.
