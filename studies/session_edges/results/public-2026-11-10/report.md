# Session-edge study

Source: public index one-minute archives (diagnostic proxy, no volume); public adjusted daily bars. Costs: 1 bps per round trip (stress 2 bps). Design period before 2020-01-01, holdout from that date. Returns are arithmetic, on notional, unlevered. Statistics are per session.

## Registered rules on one-minute sessions

| instrument | rule | sessions | Sharpe | t | net bps/session | holdout Sharpe | holdout t | since 2024 Sharpe | positive years | max drawdown % | deflated Sharpe | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| QQQ (index proxy) | noise_band_momentum | 3866 | 0.81 | 3.62 | 2.54 | 0.77 | 2.38 | 0.43 | 14/17 | -9.7 | 0.99 | paper_candidate |
| QQQ (index proxy) | close_weakness_reversion | 3866 | 0.76 | 3.22 | 3.49 | 0.82 | 2.22 | 0.91 | 13/17 | -24.0 | 1.00 | paper_candidate |
| QQQ (index proxy) | opening_range_breakout | 3866 | 0.58 | 2.40 | 2.05 | 0.54 | 1.48 | 0.26 | 13/17 | -14.0 | 0.95 | shadow_only |
| QQQ (index proxy) | first_last_half_hour | 3866 | -0.68 | -2.68 | -1.21 | -1.13 | -2.99 | -0.55 | 6/17 | -56.8 | n/a | rejected |
| SPY (index proxy) | noise_band_momentum | 3826 | 0.37 | 1.58 | 0.94 | 0.35 | 0.90 | -0.40 | 10/17 | -15.0 | 0.79 | rejected |
| SPY (index proxy) | close_weakness_reversion | 3826 | 0.77 | 3.21 | 2.93 | 0.92 | 2.55 | 0.80 | 15/17 | -16.5 | 0.99 | paper_candidate |
| SPY (index proxy) | opening_range_breakout | 3826 | 0.23 | 0.96 | 0.60 | 0.08 | 0.24 | 0.09 | 12/17 | -14.5 | 0.57 | rejected |
| SPY (index proxy) | first_last_half_hour | 3826 | -0.95 | -3.61 | -1.53 | -1.38 | -3.56 | -0.85 | 2/17 | -64.2 | n/a | rejected |

Failed checks:

- QQQ (index proxy) opening_range_breakout: all-period t below 3.0; holdout t below 2.0; deflated Sharpe below 0.95
- QQQ (index proxy) first_last_half_hour: all-period t below 3.0; holdout t below 2.0; not positive at 2x cost; fewer than 70% of neighbours positive in the holdout; deflated Sharpe below 0.95
- SPY (index proxy) noise_band_momentum: all-period t below 3.0; holdout t below 2.0; deflated Sharpe below 0.95
- SPY (index proxy) opening_range_breakout: all-period t below 3.0; holdout t below 2.0; not positive at 2x cost; fewer than 70% of neighbours positive in the holdout; deflated Sharpe below 0.95
- SPY (index proxy) first_last_half_hour: all-period t below 3.0; holdout t below 2.0; not positive at 2x cost; fewer than 70% of neighbours positive in the holdout; deflated Sharpe below 0.95

## Cost sensitivity (annualised Sharpe by round-trip cost in bps)

| instrument | rule | 0 | 0.5 | 1 | 2 | 3 | 5 |
|---|---|---|---|---|---|---|---|
| QQQ (index proxy) | noise_band_momentum | 1.08 | 0.94 | 0.81 | 0.54 | 0.28 | -0.25 |
| QQQ (index proxy) | close_weakness_reversion | 0.79 | 0.78 | 0.76 | 0.73 | 0.71 | 0.65 |
| QQQ (index proxy) | opening_range_breakout | 0.86 | 0.72 | 0.58 | 0.30 | 0.02 | -0.53 |
| QQQ (index proxy) | first_last_half_hour | -0.12 | -0.40 | -0.68 | -1.24 | -1.81 | -2.93 |
| SPY (index proxy) | noise_band_momentum | 0.73 | 0.55 | 0.37 | 0.02 | -0.33 | -1.01 |
| SPY (index proxy) | close_weakness_reversion | 0.80 | 0.79 | 0.77 | 0.74 | 0.71 | 0.64 |
| SPY (index proxy) | opening_range_breakout | 0.61 | 0.42 | 0.23 | -0.14 | -0.52 | -1.27 |
| SPY (index proxy) | first_last_half_hour | -0.33 | -0.64 | -0.95 | -1.56 | -2.17 | -3.40 |

## Forward review bands (cumulative net % over N sessions, from the backtest itself)

| instrument | rule | 20 sessions: 1% / 5% / median | 60 sessions: 1% / 5% / median | 120 sessions: 1% / 5% / median | 250 sessions: 1% / 5% / median |
|---|---|---|---|---|---|
| QQQ (index proxy) | noise_band_momentum | -3.6 / -2.2 / 0.3 | -4.9 / -3.2 / 1.1 | -5.0 / -2.7 / 2.4 | -5.5 / -2.5 / 4.6 |
| QQQ (index proxy) | close_weakness_reversion | -8.4 / -3.2 / 0.8 | -12.0 / -5.2 / 2.2 | -12.2 / -8.1 / 4.6 | -17.5 / -6.4 / 9.1 |
| QQQ (index proxy) | opening_range_breakout | -4.7 / -3.0 / 0.2 | -7.4 / -5.0 / 1.0 | -8.5 / -5.9 / 1.9 | -10.4 / -5.5 / 4.3 |
| SPY (index proxy) | close_weakness_reversion | -7.1 / -2.8 / 0.6 | -8.3 / -3.9 / 1.6 | -7.7 / -3.7 / 3.0 | -5.3 / -1.5 / 6.3 |

## Daily bars (long history)

| symbol | rule | sessions | from | Sharpe | t | holdout Sharpe | since 2024 Sharpe | positive years | max drawdown % |
|---|---|---|---|---|---|---|---|---|---|
| SPY | close_weakness_reversion_at_close | 8504 | 1993-01-29 | 0.86 | 5.49 | 1.21 | 0.83 | 30/34 | -21.3 |
| SPY | overnight_long | 8504 | 1993-01-29 | 0.72 | 4.55 | 0.55 | 1.21 | 29/34 | -43.0 |
| QQQ | close_weakness_reversion_at_close | 6962 | 1999-03-10 | 0.96 | 5.70 | 1.26 | 0.74 | 26/28 | -44.4 |
| QQQ | overnight_long | 6962 | 1999-03-10 | 0.80 | 4.57 | 0.64 | 1.21 | 23/28 | -41.2 |
| IWM | close_weakness_reversion_at_close | 6654 | 2000-05-26 | 0.79 | 4.48 | 0.68 | 0.45 | 21/27 | -23.0 |
| IWM | overnight_long | 6654 | 2000-05-26 | 0.83 | 4.50 | 0.89 | 0.99 | 22/27 | -33.0 |
| DIA | close_weakness_reversion_at_close | 7248 | 1998-01-20 | 0.84 | 5.05 | 0.97 | 1.06 | 27/29 | -18.0 |
| DIA | overnight_long | 7248 | 1998-01-20 | 0.49 | 2.91 | 0.41 | 0.77 | 22/29 | -42.3 |
| VTI | close_weakness_reversion_at_close | 6389 | 2001-06-15 | 0.91 | 4.72 | 1.11 | 0.84 | 24/26 | -29.3 |
| VTI | overnight_long | 6389 | 2001-06-15 | 1.10 | 5.98 | 0.88 | 1.58 | 24/26 | -35.0 |

## Combined book of paper candidates

Legs: noise_band_momentum:QQQ (index proxy), close_weakness_reversion:QQQ (index proxy), close_weakness_reversion:SPY (index proxy). each rule at 100% of equity, split equally across its instruments.

- Annual return 14.53%, volatility 13.31%, Sharpe 1.09 (t 4.66), max drawdown -21.9%.
- Holdout Sharpe 1.11.
- Bootstrap 95% Sharpe interval: 0.70 to 1.48.
- Correlation noise_band_momentum:QQQ (index proxy) ~ close_weakness_reversion:QQQ (index proxy): 0.10
- Correlation noise_band_momentum:QQQ (index proxy) ~ close_weakness_reversion:SPY (index proxy): 0.09
- Correlation close_weakness_reversion:QQQ (index proxy) ~ close_weakness_reversion:SPY (index proxy): 0.75

## Reading the verdicts

`paper_candidate` needs every check: all-period t of at least 3.0, holdout t of at least 2.0, positive at 2x cost in both periods, at least 70% of one-at-a-time neighbours positive in the holdout, and a deflated Sharpe ratio of at least 0.95. `shadow_only` is positive in both periods but fails a check; it is recorded forward and never traded. `rejected` is everything else.
