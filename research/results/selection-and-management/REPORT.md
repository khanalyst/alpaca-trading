# Can selection or trade management produce better trades?

The entry signal has no measurable directional edge, so nothing here can
create one. But the agent makes two other decisions every cycle that were
never tested: **which** of the available candidates to take when there are
more than slots, and **how** to manage a position once it is open. Those are
real levers, and some settings are demonstrably better than others.

Tested on 27 instruments, 2024-07 to 2026-07, point-in-time universe,
99,682 candidate setups, at the current 48h/3R configuration and ordinary
taker costs.

Headline: **trade management does not help, the shipped selection rule is
already the best of eight tested, and signal breadth is a genuine warning
sign that the agent could not previously see.**

---

## A. Trade management: every overlay is neutral or harmful

| Variant | Win % | Expectancy % | PF | Stopped % | Hit target % |
| --- | ---: | ---: | ---: | ---: | ---: |
| **shipped (fixed stop + 3R target)** | 30.18 | **-0.2267** | **0.884** | 65.5 | 19.9 |
| breakeven stop at +1R | 22.42 | -0.2376 | 0.855 | 53.1 | 16.0 |
| breakeven stop at +1.5R | 26.21 | -0.2334 | 0.871 | 59.4 | 18.0 |
| trail 2.0 ATR | 31.24 | **-0.3119** | 0.756 | **90.7** | 9.0 |
| trail 3.0 ATR | 29.95 | -0.2935 | 0.820 | 81.6 | 15.3 |
| half off at +1.5R | **37.18** | -0.2388 | 0.867 | 65.5 | 19.9 |
| half off at +1.5R + breakeven at +1R | 32.22 | -0.2477 | 0.848 | 53.1 | 16.0 |
| give up after 8h if not green | 17.80 | **-0.2046** | 0.847 | 35.5 | 15.0 |
| give up after 16h if not green | 21.87 | -0.2112 | 0.868 | 49.1 | 17.8 |

Three things worth internalising:

**Trailing stops are actively destructive here.** A 2-ATR trail raises the
stop-out rate from 65% to **91%** and cuts the target-hit rate from 20% to 9%.
Crypto's intrabar noise takes out the trail long before the move develops.
This is the single worst change available, and it is the one most often
recommended as "protecting profits".

**Partial profit-taking buys feelings, not money.** Taking half off at +1.5R
lifts the win rate from 30% to **37%** — the largest win-rate improvement on
the table — while making expectancy *worse* (-0.239 vs -0.227). More trades
end green; the account ends smaller. If a higher win rate is what you want for
psychological reasons, this is how to buy it, and this is the price.

**Only "give up early" improves expectancy, and it is not free.** Closing
anything not in profit after 8 hours is the sole variant that beats shipped
on expectancy (-0.205 vs -0.227), but it drops the win rate to 17.8% and the
profit factor to 0.847. It trades a better average for a much uglier
distribution. Not applied.

**Nothing was changed as a result of this section.** Every overlay redistributes
the payoff; none adds information, and the one that helps expectancy hurts
everything else.

---

## B. Selection: the shipped rule is already the best tested

On 22,552 bars, more than one candidate fired. If the ranking carried no
information, picking one would score the same as taking all of them
(-0.2265%). Anything better than that is genuine selection value.

| Rule | Win % | Expectancy % | PF |
| --- | ---: | ---: | ---: |
| **shipped: breakout first, then relative volume** | 31.41 | **-0.1274** | 0.936 |
| highest relative volume | 31.24 | -0.1301 | 0.934 |
| widest stop (cost is a smaller share) | 31.05 | -0.1448 | 0.944 |
| most liquid (24h turnover) | 30.91 | -0.1476 | 0.906 |
| lowest volatility | 30.04 | -0.1545 | 0.917 |
| highest volatility | 30.48 | -0.1843 | 0.908 |
| tightest stop | 29.91 | -0.1856 | 0.860 |
| *take every candidate (no selection)* | 30.13 | *-0.2265* | — |
| least extended from EMA20 | 29.04 | -0.2395 | 0.867 |

The shipped ordering beats no-selection by **+0.099% per trade** and beats
every alternative tested. Relative volume — which carried no *directional*
information in the earlier search — turns out to be a useful *tiebreaker*.
Those are different questions, and it is only useful for the second one.

Note the bottom row: preferring the least-extended candidate is *worse* than
random. Buying the one closest to its moving average sounds disciplined and is
the worst rule on the table.

**No change made.** The incumbent won.

---

## C. Signal breadth: a real warning sign, confirmed out-of-sample

Skip rules were tested by asking whether taking no trade beats taking the best
available one.

| Filter | Share of all | Win % | Expectancy % | PF |
| --- | ---: | ---: | ---: | ---: |
| only when 1-2 candidates fire (quiet) | 33.2% | 30.53 | **-0.1627** | 0.921 |
| skip high_volatility regime | 97.6% | 30.22 | -0.2090 | 0.892 |
| only trend_up / trend_down regimes | 87.0% | 29.50 | -0.2140 | 0.888 |
| *all candidates* | 100% | 30.18 | *-0.2267* | 0.884 |
| skip cheapest-stop quartile | 75.0% | 30.68 | -0.2601 | 0.888 |
| only stops wider than the median | 50.0% | 31.43 | -0.3291 | 0.881 |
| **only when 5+ fire (market-wide move)** | 39.2% | 29.62 | **-0.3475** | 0.824 |

The spread between quiet and crowded bars is **0.185% per trade** — larger
than any other effect measured in this project. And unlike most things tested
here, it survives the walk-forward split with the same sign and a similar
magnitude:

| Half | Quiet (1-2 fire) | Crowded (5+ fire) | Spread |
| --- | ---: | ---: | ---: |
| in-sample | -0.2925% | -0.4772% | **+0.1847%** |
| out-of-sample | -0.0265% | -0.1505% | **+0.1240%** |

It also has a mechanism, which most of the discarded candidates did not: when
five or more instruments qualify simultaneously, they are not five independent
setups. They are one market-wide move expressed five ways — and simultaneous
breakouts are exactly the ones that mean-revert. Taking three of them is one
correlated bet at triple size.

Two hypotheses were **refuted** in the same table, which is worth stating
because both sounded reasonable. Preferring wider stops on the theory that
fixed costs are a smaller share of a bigger stop makes things clearly worse
(-0.329%). So does skipping the tightest-stop quartile (-0.260%).

### What was changed

`_market_context` now reports `instruments_scanned`,
`instruments_with_a_valid_setup` and `setup_breadth_pct`, and the analyst
prompt explains that breadth is a warning rather than an opportunity.

This is deliberately **information, not a rule**. The evidence is consistent
in sign across both halves but the t-statistics are weak, and seven skip rules
were tested — which, by this project's own measured noise floor, is not enough
to justify a deterministic refusal to trade. The exposure caps already limit
correlated risk by notional; what was missing was any visibility into the
*breadth* of the signal, which the agent could not previously see from a
per-symbol snapshot.

---

## Verdict

The best available improvement in selection behaviour is worth roughly +0.10%
per trade (selection) plus up to +0.06% (avoiding crowded bars). Against a
per-trade expectancy of -0.227%, that closes most but not all of the gap — and
the shipped configuration already captures the selection half of it.

None of this creates an edge. What it does establish is that the agent's
existing selection ordering is sound, that the popular trade-management
overlays would all make it worse, and that the one genuinely new piece of
information — how many setups fired at once — is now visible to the layer that
decides.

## Reproduce

```bash
python research/selection_study.py --data runtime/research/data
```
