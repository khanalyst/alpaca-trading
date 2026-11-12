# Edge discovery

3861 hypotheses tested on the design period (before 2020-01-01); 124 champions have ever been taken to the holdout and all of them are in the correction. Costs are per round trip, per instrument, and every check is repeated at 2x.

## Funnel

| family | tested | passed the design screen | champions |
|---|---|---|---|
| close_weakness_reversion | 60 | 0 | 0 |
| close_weakness_reversion_daily | 1410 | 367 | 25 |
| down_streak_daily | 423 | 28 | 10 |
| expiration_week_daily | 188 | 1 | 1 |
| gap_open | 24 | 0 | 0 |
| last_hour | 8 | 0 | 0 |
| midday_trend | 24 | 0 | 0 |
| noise_band_momentum | 72 | 18 | 2 |
| opening_range_breakout | 24 | 0 | 0 |
| overnight_hold | 6 | 0 | 0 |
| prior_day_break | 24 | 0 | 0 |
| rsi_reversion_daily | 564 | 42 | 14 |
| turn_of_month_daily | 188 | 2 | 1 |
| vix_spike_daily | 564 | 1 | 1 |
| vix_term_structure_daily | 282 | 11 | 7 |

## Holdout results of every promoted hypothesis

| tier | family | instrument | parameters | design t | holdout t | holdout Sharpe | holdout bps/session | holdout beta | holdout alpha t | since recent start bps/session |
|---|---|---|---|---|---|---|---|---|---|---|
| confirmed | close_weakness_reversion_daily | SOXL | enter_below=0.25, exit_above=0.5, max_sessions=3 | 3.21 | 3.88 | 1.34 | 37.63 | 0.37 | 2.93 | 31.26 |
| confirmed | close_weakness_reversion_daily | SPY | enter_below=0.1, exit_above=0.6, max_sessions=5 | 4.58 | 3.76 | 1.30 | 5.48 | 0.28 | 2.60 | 4.04 |
| confirmed | close_weakness_reversion_daily | XLK | enter_below=0.1, exit_above=0.6, max_sessions=5 | 3.61 | 3.70 | 1.25 | 7.20 | 0.27 | 2.47 | 4.92 |
| confirmed | close_weakness_reversion_daily | SSO | enter_below=0.2, exit_above=0.6, max_sessions=5 | 3.21 | 3.69 | 1.33 | 13.14 | 0.39 | 2.50 | 7.79 |
| confirmed | rsi_reversion_daily | IVW | enter_below=15.0, exit_above=50.0, period=2 | 3.34 | 3.66 | 1.27 | 6.29 | 0.28 | 2.48 | 7.47 |
| confirmed | down_streak_daily | SPY | max_sessions=5, streak=3 | 3.86 | 3.65 | 1.18 | 3.45 | 0.13 | 2.68 | 3.83 |
| confirmed | down_streak_daily | QQQ | max_sessions=1, streak=3 | 4.27 | 3.61 | 1.33 | 3.38 | 0.07 | 3.03 | 3.64 |
| confirmed | close_weakness_reversion_daily | IVW | enter_below=0.2, exit_above=0.6, max_sessions=3 | 4.04 | 3.53 | 1.33 | 6.95 | 0.32 | 2.54 | 4.19 |
| confirmed | down_streak_daily | IVW | max_sessions=3, streak=3 | 3.24 | 3.34 | 1.20 | 3.23 | 0.08 | 2.70 | 2.74 |
| provisional | close_weakness_reversion_daily | SMH | enter_below=0.25, exit_above=0.6, max_sessions=3 | 3.03 | 3.19 | 1.12 | 9.85 | 0.35 | 1.79 | 4.07 |
| provisional | down_streak_daily | SMH | max_sessions=5, streak=2 | 2.07 | 3.18 | 1.06 | 7.73 | 0.24 | 1.88 | 7.55 |
| provisional | close_weakness_reversion_daily | UPRO | enter_below=0.15, exit_above=0.5, max_sessions=5 | 2.87 | 3.16 | 1.23 | 15.28 | 0.27 | 2.27 | 9.82 |
| provisional | close_weakness_reversion_daily | QLD | enter_below=0.15, exit_above=0.5, max_sessions=3 | 3.30 | 3.11 | 1.21 | 11.75 | 0.24 | 2.22 | 4.92 |
| provisional | close_weakness_reversion_daily | QQQ | enter_below=0.1, exit_above=0.6, max_sessions=3 | 4.89 | 3.07 | 1.18 | 5.44 | 0.22 | 2.13 | 2.74 |
| provisional | close_weakness_reversion_daily | VB | enter_below=0.15, exit_above=0.6, max_sessions=5 | 3.30 | 3.02 | 1.04 | 5.07 | 0.26 | 2.54 | 3.86 |
| provisional | close_weakness_reversion_daily | GDX | enter_below=0.3, exit_above=0.5, max_sessions=5 | 2.45 | 2.93 | 1.07 | 11.19 | 0.41 | 2.39 | 16.51 |
| provisional | close_weakness_reversion_daily | DIA | enter_below=0.15, exit_above=0.5, max_sessions=5 | 4.07 | 2.93 | 1.07 | 4.45 | 0.30 | 1.97 | 2.98 |
| provisional | close_weakness_reversion_daily | VTI | enter_below=0.2, exit_above=0.5, max_sessions=5 | 3.64 | 2.87 | 1.10 | 5.02 | 0.32 | 1.78 | 2.60 |
| provisional | close_weakness_reversion_daily | EEM | enter_below=0.2, exit_above=0.6, max_sessions=5 | 4.68 | 2.82 | 0.97 | 4.96 | 0.33 | 2.43 | 4.99 |
| provisional | close_weakness_reversion_daily | TQQQ | enter_below=0.15, exit_above=0.5, max_sessions=3 | 3.72 | 2.82 | 1.09 | 15.25 | 0.23 | 1.98 | 6.92 |
| unconfirmed | down_streak_daily | TQQQ | max_sessions=5, streak=2 | 2.24 | 2.72 | 0.88 | 12.18 | 0.23 | 1.77 | 10.27 |
| unconfirmed | close_weakness_reversion_daily | XRT | enter_below=0.25, exit_above=0.6, max_sessions=5 | 4.62 | 2.67 | 0.94 | 7.18 | 0.41 | 2.09 | 3.38 |
| unconfirmed | close_weakness_reversion_daily | XLI | enter_below=0.25, exit_above=0.5, max_sessions=5 | 2.66 | 2.67 | 0.87 | 4.69 | 0.38 | 1.62 | 4.21 |
| unconfirmed | close_weakness_reversion_daily | KRE | enter_below=0.25, exit_above=0.5, max_sessions=5 | 3.44 | 2.61 | 0.94 | 8.23 | 0.38 | 2.43 | 0.77 |
| unconfirmed | rsi_reversion_daily | XLY | enter_below=10.0, exit_above=50.0, period=2 | 3.29 | 2.54 | 0.83 | 4.03 | 0.24 | 1.77 | 5.53 |
| unconfirmed | down_streak_daily | QLD | max_sessions=5, streak=2 | 2.21 | 2.52 | 0.82 | 7.78 | 0.23 | 1.54 | 6.63 |
| unconfirmed | down_streak_daily | VTI | max_sessions=3, streak=2 | 2.67 | 2.50 | 0.82 | 3.67 | 0.30 | 1.27 | 4.35 |
| unconfirmed | rsi_reversion_daily | QQQ | enter_below=10.0, exit_above=50.0, period=3 | 4.07 | 2.41 | 0.77 | 2.50 | 0.11 | 1.57 | 3.94 |
| unconfirmed | close_weakness_reversion_daily | VO | enter_below=0.1, exit_above=0.6, max_sessions=5 | 2.42 | 2.39 | 0.89 | 3.92 | 0.27 | 1.56 | 3.36 |
| unconfirmed | close_weakness_reversion_daily | IVE | enter_below=0.2, exit_above=0.5, max_sessions=5 | 3.55 | 2.39 | 0.83 | 3.50 | 0.33 | 1.38 | 2.57 |
| unconfirmed | close_weakness_reversion_daily | XLP | enter_below=0.3, exit_above=0.6, max_sessions=5 | 4.88 | 2.38 | 0.74 | 3.17 | 0.44 | 1.57 | 4.41 |
| unconfirmed | rsi_reversion_daily | XLK | enter_below=15.0, exit_above=50.0, period=3 | 3.74 | 2.37 | 0.66 | 3.06 | 0.17 | 1.10 | 5.24 |
| unconfirmed | close_weakness_reversion_daily | EFA | enter_below=0.25, exit_above=0.5, max_sessions=5 | 3.74 | 2.33 | 0.79 | 3.66 | 0.38 | 1.62 | 3.57 |
| unconfirmed | midday_trend | QQQ (index proxy) | decision_minute=240, sides=long, threshold=1.0 | 2.42 | 2.26 | 0.91 | 1.26 | 0.03 | 1.81 | 1.11 |
| unconfirmed | close_weakness_reversion_daily | XBI | enter_below=0.3, exit_above=0.5, max_sessions=5 | 2.29 | 2.26 | 0.78 | 6.56 | 0.40 | 1.93 | 6.93 |
| unconfirmed | rsi_reversion_daily | UPRO | enter_below=15.0, exit_above=50.0, period=2 | 2.80 | 2.24 | 0.68 | 9.34 | 0.34 | 1.09 | 15.16 |
| unconfirmed | vix_term_structure_daily | XLK | mode=hold_contango, threshold=0.95 | 3.22 | 2.21 | 0.81 | 6.73 | 0.55 | 0.84 | 5.64 |
| unconfirmed | vix_term_structure_daily | QQQ | mode=hold_contango, threshold=0.95 | 3.44 | 2.17 | 0.80 | 5.88 | 0.55 | 0.82 | 4.52 |
| unconfirmed | close_weakness_reversion_daily | IWM | enter_below=0.2, exit_above=0.6, max_sessions=5 | 4.28 | 2.16 | 0.75 | 4.23 | 0.30 | 1.52 | 2.57 |
| unconfirmed | noise_band_momentum | QQQ (index proxy) | band_multiple=1.5, decision_step_minutes=30, lookback_sessions=30, sides=long | 3.66 | 2.15 | 0.78 | 1.57 | 0.05 | 1.64 | 1.18 |
| unconfirmed | rsi_reversion_daily | SSO | enter_below=15.0, exit_above=50.0, period=2 | 2.85 | 2.14 | 0.65 | 5.93 | 0.33 | 0.93 | 9.82 |
| unconfirmed | vix_term_structure_daily | SMH | mode=hold_contango, threshold=1.0 | 2.32 | 2.14 | 0.78 | 10.31 | 0.77 | -0.33 | 11.02 |
| unconfirmed | down_streak_daily | XLK | max_sessions=5, streak=4 | 3.04 | 2.13 | 0.61 | 1.16 | 0.03 | 1.61 | 1.32 |
| unconfirmed | rsi_reversion_daily | XLV | enter_below=10.0, exit_above=70.0, period=2 | 2.56 | 2.10 | 0.64 | 2.41 | 0.28 | 1.01 | 3.20 |
| unconfirmed | rsi_reversion_daily | QLD | enter_below=15.0, exit_above=70.0, period=2 | 2.75 | 2.06 | 0.67 | 8.18 | 0.39 | 0.74 | 11.78 |
| unconfirmed | close_weakness_reversion_daily | XLV | enter_below=0.3, exit_above=0.6, max_sessions=5 | 3.06 | 2.06 | 0.76 | 3.53 | 0.42 | 0.99 | 1.88 |
| unconfirmed | down_streak_daily | SSO | max_sessions=3, streak=2 | 2.21 | 2.04 | 0.62 | 5.54 | 0.31 | 0.82 | 8.22 |
| unconfirmed | turn_of_month_daily | SPY | enter_days_before=1, hold_days_after=3 | 3.07 | 1.97 | 0.74 | 2.10 | 0.13 | 1.26 | 0.81 |
| unconfirmed | rsi_reversion_daily | VTI | enter_below=15.0, exit_above=70.0, period=2 | 2.42 | 1.92 | 0.63 | 3.26 | 0.41 | 0.54 | 5.21 |
| unconfirmed | vix_term_structure_daily | QLD | mode=hold_contango, threshold=0.95 | 3.25 | 1.90 | 0.70 | 10.30 | 0.55 | 0.68 | 7.03 |
| unconfirmed | turn_of_month_daily | EEM | enter_days_before=2, hold_days_after=3 | 2.44 | 1.88 | 0.70 | 2.59 | 0.17 | 1.58 | 2.41 |
| unconfirmed | close_weakness_reversion | QQQ (index proxy) | enter_below=0.15, exit_above=0.5, max_sessions=3 | 2.95 | 1.86 | 0.70 | 3.43 | 0.21 | 0.94 | 2.94 |
| unconfirmed | close_weakness_reversion | SPY (index proxy) | enter_below=0.25, exit_above=0.6, max_sessions=3 | 2.32 | 1.83 | 0.68 | 3.28 | 0.34 | 0.71 | 0.94 |
| unconfirmed | close_weakness_reversion_daily | XLB | enter_below=0.1, exit_above=0.5, max_sessions=5 | 2.70 | 1.79 | 0.67 | 2.99 | 0.25 | 1.19 | 2.93 |
| unconfirmed | close_weakness_reversion_daily | XLE | enter_below=0.1, exit_above=0.5, max_sessions=5 | 3.12 | 1.79 | 0.62 | 4.18 | 0.26 | 0.96 | -0.31 |
| unconfirmed | close_weakness_reversion_daily | IBB | enter_below=0.25, exit_above=0.5, max_sessions=5 | 2.21 | 1.78 | 0.64 | 3.64 | 0.35 | 1.15 | 4.41 |
| unconfirmed | close_weakness_reversion_daily | XLU | enter_below=0.15, exit_above=0.5, max_sessions=1 | 3.25 | 1.77 | 0.62 | 2.44 | 0.21 | 1.20 | 2.01 |
| unconfirmed | vix_term_structure_daily | XLP | mode=hold_contango, threshold=1.05 | 3.51 | 1.75 | 0.62 | 3.39 | 0.70 | 0.86 | 4.36 |
| unconfirmed | rsi_reversion_daily | SPY | enter_below=15.0, exit_above=70.0, period=3 | 4.22 | 1.71 | 0.51 | 2.36 | 0.34 | 0.20 | 4.71 |
| unconfirmed | rsi_reversion_daily | XLRE | enter_below=15.0, exit_above=50.0, period=2 | 2.81 | 1.71 | 0.59 | 2.72 | 0.26 | 1.21 | 1.23 |
| unconfirmed | overnight_hold | QQQ (index proxy) | condition=any | 2.81 | 1.69 | 0.63 | 3.87 | 0.37 | 0.42 | 4.59 |
| unconfirmed | close_weakness_reversion_daily | TNA | enter_below=0.2, exit_above=0.5, max_sessions=5 | 3.31 | 1.67 | 0.60 | 9.58 | 0.28 | 1.21 | 4.44 |
| unconfirmed | close_weakness_reversion_daily | TLT | enter_below=0.15, exit_above=0.5, max_sessions=5 | 2.24 | 1.66 | 0.59 | 1.98 | 0.27 | 2.08 | -1.56 |
| unconfirmed | down_streak_daily | XLP | max_sessions=3, streak=2 | 3.07 | 1.63 | 0.55 | 1.82 | 0.26 | 0.81 | 1.43 |
| unconfirmed | turn_of_month_daily | DIA | enter_days_before=1, hold_days_after=4 | 2.51 | 1.63 | 0.58 | 1.95 | 0.19 | 0.90 | -0.83 |
| unconfirmed | down_streak_daily | XLV | max_sessions=5, streak=2 | 2.22 | 1.62 | 0.52 | 1.98 | 0.28 | 0.52 | 1.43 |
| unconfirmed | rsi_reversion_daily | TQQQ | enter_below=15.0, exit_above=70.0, period=2 | 2.53 | 1.60 | 0.51 | 9.25 | 0.38 | 0.25 | 17.12 |
| unconfirmed | close_weakness_reversion_daily | EWZ | enter_below=0.1, exit_above=0.5, max_sessions=1 | 2.17 | 1.54 | 0.60 | 2.64 | 0.09 | 1.34 | 0.15 |
| unconfirmed | rsi_reversion_daily | IWM | enter_below=15.0, exit_above=70.0, period=2 | 2.67 | 1.52 | 0.55 | 3.37 | 0.35 | 0.91 | 2.63 |
| unconfirmed | rsi_reversion_daily | TNA | enter_below=15.0, exit_above=70.0, period=2 | 2.35 | 1.50 | 0.55 | 10.21 | 0.38 | 1.13 | 5.63 |
| unconfirmed | rsi_reversion_daily | DIA | enter_below=15.0, exit_above=70.0, period=2 | 2.77 | 1.42 | 0.48 | 2.45 | 0.44 | 0.21 | 4.04 |
| unconfirmed | vix_term_structure_daily | XLY | mode=hold_contango, threshold=0.95 | 2.83 | 1.39 | 0.52 | 3.89 | 0.55 | 0.50 | 1.51 |
| unconfirmed | rsi_reversion_daily | SMH | enter_below=15.0, exit_above=50.0, period=3 | 3.01 | 1.36 | 0.48 | 2.55 | 0.13 | 0.41 | 3.21 |
| unconfirmed | down_streak_daily | XLI | max_sessions=1, streak=4 | 2.03 | 1.34 | 0.52 | 0.89 | 0.04 | 1.01 | 2.55 |
| unconfirmed | rsi_reversion_daily | XBI | enter_below=15.0, exit_above=70.0, period=2 | 2.26 | 1.32 | 0.46 | 3.66 | 0.34 | 0.81 | 2.42 |
| unconfirmed | down_streak_daily | IWM | max_sessions=3, streak=2 | 2.62 | 1.32 | 0.44 | 2.23 | 0.24 | 0.67 | 4.62 |
| unconfirmed | down_streak_daily | EFA | max_sessions=5, streak=2 | 2.56 | 1.23 | 0.42 | 1.72 | 0.29 | 0.39 | 3.09 |
| unconfirmed | rsi_reversion_daily | KRE | enter_below=5.0, exit_above=50.0, period=2 | 2.92 | 1.20 | 0.39 | 1.95 | 0.12 | 0.85 | 3.65 |
| unconfirmed | turn_of_month_daily | XLV | enter_days_before=1, hold_days_after=4 | 2.60 | 1.19 | 0.44 | 1.43 | 0.21 | 0.37 | -0.78 |
| unconfirmed | rsi_reversion_daily | XLF | enter_below=15.0, exit_above=70.0, period=2 | 3.51 | 1.17 | 0.38 | 2.35 | 0.40 | 0.04 | 2.25 |
| unconfirmed | down_streak_daily | DIA | max_sessions=3, streak=2 | 2.87 | 1.17 | 0.39 | 1.66 | 0.30 | 0.12 | 2.25 |
| unconfirmed | down_streak_daily | IVE | max_sessions=5, streak=2 | 2.14 | 1.14 | 0.38 | 1.61 | 0.33 | -0.03 | 2.34 |
| unconfirmed | down_streak_daily | TLT | max_sessions=5, streak=4 | 4.19 | 1.14 | 0.38 | 0.51 | 0.04 | 1.28 | 0.04 |
| unconfirmed | close_weakness_reversion_daily | FXI | enter_below=0.3, exit_above=0.6, max_sessions=5 | 5.41 | 1.11 | 0.44 | 3.36 | 0.39 | 1.20 | 4.69 |
| unconfirmed | noise_band_momentum | SPY (index proxy) | band_multiple=1.5, decision_step_minutes=60, lookback_sessions=30, sides=long | 2.41 | 1.07 | 0.44 | 0.52 | 0.03 | 0.73 | 0.10 |
| unconfirmed | rsi_reversion_daily | EEM | enter_below=15.0, exit_above=50.0, period=2 | 3.50 | 1.05 | 0.38 | 1.69 | 0.25 | 0.51 | 4.66 |
| unconfirmed | rsi_reversion_daily | IVE | enter_below=15.0, exit_above=70.0, period=2 | 2.06 | 1.04 | 0.35 | 1.66 | 0.42 | -0.35 | 2.51 |
| unconfirmed | rsi_reversion_daily | XRT | enter_below=10.0, exit_above=70.0, period=2 | 2.19 | 0.96 | 0.34 | 2.12 | 0.27 | 0.20 | 2.82 |
| unconfirmed | turn_of_month_daily | QQQ | enter_days_before=1, hold_days_after=4 | 2.08 | 0.95 | 0.36 | 1.62 | 0.21 | -0.05 | -0.92 |
| unconfirmed | down_streak_daily | SOXL | max_sessions=5, streak=4 | 2.30 | 0.91 | 0.32 | 2.72 | 0.03 | 0.52 | -1.28 |
| unconfirmed | down_streak_daily | XLF | max_sessions=3, streak=4 | 2.16 | 0.90 | 0.32 | 0.78 | 0.06 | 0.49 | 1.35 |
| unconfirmed | down_streak_daily | TNA | max_sessions=3, streak=2 | 2.09 | 0.89 | 0.31 | 4.67 | 0.25 | 0.39 | 12.23 |
| unconfirmed | close_weakness_reversion_daily | IEF | enter_below=0.15, exit_above=0.6, max_sessions=5 | 2.01 | 0.88 | 0.32 | 0.52 | 0.31 | 1.05 | -0.01 |
| unconfirmed | rsi_reversion_daily | XLP | enter_below=15.0, exit_above=70.0, period=2 | 3.60 | 0.88 | 0.28 | 1.09 | 0.37 | -0.27 | 1.44 |
| unconfirmed | rsi_reversion_daily | VB | enter_below=10.0, exit_above=50.0, period=2 | 2.53 | 0.83 | 0.28 | 1.37 | 0.26 | 0.04 | 3.58 |
| unconfirmed | rsi_reversion_daily | FXI | enter_below=5.0, exit_above=50.0, period=2 | 3.52 | 0.82 | 0.30 | 1.56 | 0.18 | 0.72 | -0.60 |
| unconfirmed | down_streak_daily | FXI | max_sessions=3, streak=2 | 2.91 | 0.80 | 0.28 | 1.74 | 0.26 | 0.73 | -2.23 |
| unconfirmed | close_weakness_reversion_daily | XLF | enter_below=0.1, exit_above=0.5, max_sessions=3 | 3.22 | 0.77 | 0.28 | 1.20 | 0.19 | 0.08 | 0.14 |
| unconfirmed | turn_of_month_daily | XLY | enter_days_before=2, hold_days_after=3 | 2.15 | 0.76 | 0.27 | 1.13 | 0.18 | 0.12 | -0.09 |
| unconfirmed | down_streak_daily | GDX | max_sessions=1, streak=2 | 2.19 | 0.75 | 0.28 | 1.71 | 0.14 | 0.14 | 5.95 |
| unconfirmed | turn_of_month_daily | XLB | enter_days_before=2, hold_days_after=4 | 2.37 | 0.71 | 0.24 | 1.06 | 0.24 | -0.11 | -1.86 |
| unconfirmed | noise_band_momentum | SPY (index proxy) | band_multiple=1.25, decision_step_minutes=60, lookback_sessions=7, sides=both | 2.36 | 0.66 | 0.25 | 0.59 | -0.01 | 0.73 | -0.44 |
| unconfirmed | rsi_reversion_daily | IBB | enter_below=15.0, exit_above=70.0, period=2 | 2.70 | 0.61 | 0.21 | 1.21 | 0.34 | -0.24 | 0.59 |
| unconfirmed | turn_of_month_daily | XLK | enter_days_before=1, hold_days_after=4 | 2.40 | 0.58 | 0.23 | 1.16 | 0.21 | -0.45 | -1.64 |
| unconfirmed | down_streak_daily | XRT | max_sessions=5, streak=2 | 2.13 | 0.53 | 0.18 | 1.09 | 0.26 | -0.30 | 2.58 |
| unconfirmed | rsi_reversion_daily | VO | enter_below=15.0, exit_above=70.0, period=3 | 2.09 | 0.49 | 0.16 | 0.71 | 0.27 | -0.50 | 2.52 |
| unconfirmed | vix_term_structure_daily | KWEB | mode=buy_backwardation, threshold=0.95 | 2.55 | 0.39 | 0.14 | 1.15 | 0.24 | 0.37 | 2.99 |
| unconfirmed | turn_of_month_daily | EWZ | enter_days_before=2, hold_days_after=3 | 3.08 | 0.25 | 0.10 | 0.58 | 0.17 | 0.05 | 1.55 |
| unconfirmed | rsi_reversion_daily | IEF | enter_below=15.0, exit_above=70.0, period=2 | 3.00 | 0.25 | 0.08 | 0.14 | 0.31 | 0.30 | -0.27 |
| unconfirmed | rsi_reversion_daily | SOXL | enter_below=15.0, exit_above=70.0, period=3 | 2.32 | 0.24 | 0.08 | 1.71 | 0.23 | -0.93 | 10.10 |
| unconfirmed | close_weakness_reversion_daily | XLY | enter_below=0.3, exit_above=0.6, max_sessions=3 | 2.96 | 0.21 | 0.08 | 0.45 | 0.36 | -0.84 | -0.26 |
| unconfirmed | down_streak_daily | IEF | max_sessions=5, streak=2 | 2.18 | 0.12 | 0.04 | 0.06 | 0.21 | 0.13 | 0.40 |
| unconfirmed | rsi_reversion_daily | EFA | enter_below=15.0, exit_above=70.0, period=2 | 2.37 | 0.04 | 0.01 | 0.06 | 0.38 | -1.17 | 1.88 |
| unconfirmed | midday_trend | SPY (index proxy) | decision_minute=240, sides=long, threshold=1.5 | 2.31 | -0.04 | -0.01 | -0.01 | 0.02 | -0.28 | 0.30 |
| unconfirmed | down_streak_daily | EWZ | max_sessions=1, streak=2 | 2.23 | -0.07 | -0.02 | -0.14 | 0.16 | -0.30 | -0.01 |
| unconfirmed | rsi_reversion_daily | XLE | enter_below=15.0, exit_above=50.0, period=2 | 2.37 | -0.11 | -0.04 | -0.31 | 0.36 | -1.39 | -0.72 |
| unconfirmed | rsi_reversion_daily | TLT | enter_below=15.0, exit_above=50.0, period=2 | 2.93 | -0.18 | -0.06 | -0.18 | 0.21 | 0.09 | 0.10 |
| unconfirmed | close_weakness_reversion_daily | LQD | enter_below=0.3, exit_above=0.6, max_sessions=5 | 3.92 | -0.31 | -0.10 | -0.30 | 0.52 | -0.81 | -0.07 |
| unconfirmed | rsi_reversion_daily | XLU | enter_below=15.0, exit_above=50.0, period=2 | 2.21 | -0.32 | -0.11 | -0.55 | 0.31 | -1.24 | 1.52 |
| unconfirmed | rsi_reversion_daily | EWZ | enter_below=10.0, exit_above=50.0, period=3 | 3.44 | -0.39 | -0.11 | -0.70 | 0.20 | -0.76 | 1.28 |
| unconfirmed | expiration_week_daily | KWEB | enter=monday, leave=friday | 2.44 | -0.46 | -0.17 | -1.50 | 0.24 | -0.58 | -3.72 |
| unconfirmed | down_streak_daily | EEM | max_sessions=1, streak=2 | 3.65 | -0.54 | -0.19 | -0.73 | 0.18 | -1.07 | 1.56 |
| unconfirmed | down_streak_daily | XLE | max_sessions=5, streak=2 | 3.23 | -1.44 | -0.50 | -3.46 | 0.27 | -2.64 | 0.49 |
| unconfirmed | vix_spike_daily | XLP | max_sessions=3, spike=0.15 | 3.04 | -1.66 | -0.52 | -1.92 | 0.32 | -3.13 | 0.10 |

## Edge book

| trade | weight | tier | rule | symbol | parameters | evidence | note |
|---|---|---|---|---|---|---|---|
| record only | 0.0% | confirmed | close_weakness_reversion | SOXL | decision_minute=380, enter_below=0.25, exit_above=0.5, max_sessions=3 | close_weakness_reversion_daily on SOXL | leveraged fund: recorded only |
| yes | 16.7% | confirmed | close_weakness_reversion | SPY | decision_minute=380, enter_below=0.1, exit_above=0.6, max_sessions=5 | close_weakness_reversion_daily on SPY |  |
| yes | 16.7% | confirmed | close_weakness_reversion | XLK | decision_minute=380, enter_below=0.1, exit_above=0.6, max_sessions=5 | close_weakness_reversion_daily on XLK |  |
| record only | 0.0% | confirmed | close_weakness_reversion | SSO | decision_minute=380, enter_below=0.2, exit_above=0.6, max_sessions=5 | close_weakness_reversion_daily on SSO | leveraged fund: recorded only |
| yes | 16.7% | confirmed | close_weakness_reversion | IVW | decision_minute=380, enter_below=0.2, exit_above=0.6, max_sessions=3 | close_weakness_reversion_daily on IVW |  |
| yes | 25.0% | registered | noise_band_momentum | QQQ | lookback_sessions=14, decision_step_minutes=30, first_decision_minute=30, last_decision_minute=360, band_multiple=1.0, sides=['long', 'short'] | noise_band_momentum on QQQ (index proxy) |  |
| record only | 0.0% | registered | close_weakness_reversion | QQQ | decision_minute=380, enter_below=0.2, exit_above=0.5, max_sessions=5 | close_weakness_reversion on QQQ (index proxy) | symbol already owned by a stronger leg |
| record only | 0.0% | provisional | close_weakness_reversion | SMH | decision_minute=380, enter_below=0.25, exit_above=0.6, max_sessions=3 | close_weakness_reversion_daily on SMH | evidence tier does not trade |
| record only | 0.0% | provisional | close_weakness_reversion | UPRO | decision_minute=380, enter_below=0.15, exit_above=0.5, max_sessions=5 | close_weakness_reversion_daily on UPRO | evidence tier does not trade |
| record only | 0.0% | provisional | close_weakness_reversion | QLD | decision_minute=380, enter_below=0.15, exit_above=0.5, max_sessions=3 | close_weakness_reversion_daily on QLD | evidence tier does not trade |
| record only | 0.0% | provisional | close_weakness_reversion | VB | decision_minute=380, enter_below=0.15, exit_above=0.6, max_sessions=5 | close_weakness_reversion_daily on VB | evidence tier does not trade |
| record only | 0.0% | provisional | close_weakness_reversion | GDX | decision_minute=380, enter_below=0.3, exit_above=0.5, max_sessions=5 | close_weakness_reversion_daily on GDX | evidence tier does not trade |
| record only | 0.0% | provisional | close_weakness_reversion | DIA | decision_minute=380, enter_below=0.15, exit_above=0.5, max_sessions=5 | close_weakness_reversion_daily on DIA | evidence tier does not trade |
| record only | 0.0% | provisional | close_weakness_reversion | VTI | decision_minute=380, enter_below=0.2, exit_above=0.5, max_sessions=5 | close_weakness_reversion_daily on VTI | evidence tier does not trade |
| record only | 0.0% | provisional | close_weakness_reversion | EEM | decision_minute=380, enter_below=0.2, exit_above=0.6, max_sessions=5 | close_weakness_reversion_daily on EEM | evidence tier does not trade |
| record only | 0.0% | provisional | close_weakness_reversion | TQQQ | decision_minute=380, enter_below=0.15, exit_above=0.5, max_sessions=3 | close_weakness_reversion_daily on TQQQ | evidence tier does not trade |

`confirmed` passed Holm at 5% across every promoted hypothesis, stayed positive at twice the cost, has been positive since the recent start, and keeps a holdout alpha t of at least 2 once its exposure to the instrument is removed. `provisional` passed Benjamini-Yekutieli at 10% and the cost stress with a positive alpha. `registered` is a paper candidate of the fixed-rule study. Only `confirmed` and `registered` legs trade.

Beta is the slope of the rule's daily return on the instrument's own daily return; the alpha t tests what is left, so a rule that only holds the market scores near zero.

## Families not searched in this run

* `vwap_trend`: needs traded volume
