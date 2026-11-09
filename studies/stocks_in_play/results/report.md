# Opening-range breakout: stocks in play against ETFs

46 test sessions, 2026-09-03 to 2026-11-06, 2269 symbols loaded, five-minute bars. Diagnostic only: about two months of data, today's listing, no news filter. Rules in `study.py`.

Each trade risks 1% of equity, capped at 4x leverage across the day's 20 names. `random` draws 20 eligible stocks a day without the in-play ranking; `etf` runs the 24 ETFs the intraday program trades. Two fill orders bound what one-minute bars would show: `stop_first` (a bar that triggers the entry and touches the stop stops out) and `entry_first` (the stop is checked from the next bar).

## Stop at 10% of daily ATR (the paper's rule)

| universe | fill order | cost a side | trades | win rate | mean R | mean net bps | stopped | daily t | daily mean | Sharpe | total | max drawdown |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| in_play | stop_first | 0.0 bps | 743 | 5% | -0.722 | -30.1 | 95% | -9.77 | -0.97% | -22.87 | -36.3% | -35.5% |
| random | stop_first | 0.0 bps | 764 | 7% | -0.681 | -25.4 | 92% | -8.73 | -0.84% | -20.44 | -32.4% | -32.5% |
| etf | stop_first | 0.0 bps | 1006 | 10% | -0.429 | -6.3 | 89% | -2.75 | -0.28% | -6.43 | -12.1% | -14.1% |
| in_play | stop_first | 2.5 bps | 743 | 5% | -0.876 | -35.1 | 95% | -11.40 | -1.13% | -26.68 | -40.9% | -40.0% |
| random | stop_first | 2.5 bps | 764 | 7% | -0.840 | -30.4 | 92% | -10.42 | -1.01% | -24.39 | -37.4% | -37.4% |
| etf | stop_first | 2.5 bps | 1006 | 10% | -0.850 | -11.3 | 89% | -4.87 | -0.50% | -11.40 | -20.5% | -21.9% |
| in_play | stop_first | 5.0 bps | 743 | 5% | -1.031 | -40.1 | 95% | -13.02 | -1.30% | -30.46 | -45.2% | -44.2% |
| random | stop_first | 5.0 bps | 764 | 7% | -0.999 | -35.4 | 92% | -12.09 | -1.18% | -28.30 | -42.0% | -42.0% |
| etf | stop_first | 5.0 bps | 1006 | 10% | -1.271 | -16.3 | 89% | -6.96 | -0.71% | -16.29 | -28.2% | -28.9% |
| in_play | entry_first | 0.0 bps | 743 | 14% | -0.234 | -11.8 | 85% | -1.94 | -0.38% | -4.53 | -16.4% | -20.2% |
| random | entry_first | 0.0 bps | 764 | 17% | -0.123 | -3.3 | 82% | -0.45 | -0.11% | -1.04 | -5.5% | -12.1% |
| etf | entry_first | 0.0 bps | 1006 | 19% | +0.020 | +0.3 | 81% | +0.07 | +0.01% | 0.16 | +0.2% | -6.6% |
| in_play | entry_first | 2.5 bps | 743 | 14% | -0.388 | -16.8 | 85% | -2.75 | -0.54% | -6.44 | -22.4% | -24.7% |
| random | entry_first | 2.5 bps | 764 | 17% | -0.282 | -8.3 | 82% | -1.12 | -0.28% | -2.62 | -12.4% | -17.9% |
| etf | entry_first | 2.5 bps | 1006 | 18% | -0.401 | -4.7 | 81% | -0.99 | -0.20% | -2.32 | -9.4% | -15.0% |
| in_play | entry_first | 5.0 bps | 743 | 14% | -0.543 | -21.8 | 85% | -3.57 | -0.70% | -8.35 | -28.0% | -29.3% |
| random | entry_first | 5.0 bps | 764 | 17% | -0.442 | -13.3 | 82% | -1.79 | -0.44% | -4.20 | -18.9% | -23.4% |
| etf | entry_first | 5.0 bps | 1006 | 17% | -0.822 | -9.7 | 81% | -2.04 | -0.42% | -4.78 | -18.1% | -22.7% |

## Stop at the other side of the first candle

| universe | fill order | cost a side | trades | win rate | mean R | mean net bps | stopped | daily t | daily mean | Sharpe | total | max drawdown |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| in_play | stop_first | 0.0 bps | 743 | 42% | -0.056 | -8.3 | 35% | -0.79 | -0.23% | -1.85 | -11.1% | -22.6% |
| random | stop_first | 0.0 bps | 764 | 40% | -0.045 | +0.0 | 44% | -0.04 | -0.01% | -0.09 | -1.4% | -13.3% |
| etf | stop_first | 0.0 bps | 1006 | 35% | -0.025 | +0.4 | 57% | +0.09 | +0.02% | 0.22 | +0.5% | -6.2% |
| in_play | stop_first | 2.5 bps | 743 | 41% | -0.086 | -13.3 | 35% | -1.31 | -0.39% | -3.07 | -17.2% | -25.9% |
| random | stop_first | 2.5 bps | 764 | 38% | -0.092 | -5.0 | 44% | -0.60 | -0.18% | -1.41 | -8.6% | -18.9% |
| etf | stop_first | 2.5 bps | 1006 | 33% | -0.205 | -4.5 | 57% | -0.95 | -0.20% | -2.22 | -9.2% | -14.5% |
| in_play | stop_first | 5.0 bps | 743 | 40% | -0.116 | -18.3 | 35% | -1.83 | -0.54% | -4.29 | -22.9% | -29.0% |
| random | stop_first | 5.0 bps | 764 | 37% | -0.140 | -10.0 | 44% | -1.16 | -0.34% | -2.72 | -15.4% | -24.1% |
| etf | stop_first | 5.0 bps | 1006 | 31% | -0.385 | -9.5 | 57% | -1.98 | -0.42% | -4.65 | -17.9% | -22.3% |
| in_play | entry_first | 0.0 bps | 743 | 43% | -0.022 | -3.0 | 34% | -0.22 | -0.06% | -0.51 | -3.6% | -17.6% |
| random | entry_first | 0.0 bps | 764 | 40% | -0.026 | +1.2 | 43% | +0.09 | +0.03% | 0.22 | +0.4% | -11.4% |
| etf | entry_first | 0.0 bps | 1006 | 35% | +0.003 | +0.8 | 56% | +0.15 | +0.03% | 0.36 | +1.1% | -6.2% |
| in_play | entry_first | 2.5 bps | 743 | 42% | -0.052 | -8.0 | 34% | -0.76 | -0.22% | -1.78 | -10.3% | -20.7% |
| random | entry_first | 2.5 bps | 764 | 39% | -0.074 | -3.8 | 43% | -0.47 | -0.14% | -1.09 | -7.0% | -17.2% |
| etf | entry_first | 2.5 bps | 1006 | 33% | -0.177 | -4.2 | 56% | -0.82 | -0.18% | -1.92 | -8.6% | -14.7% |
| in_play | entry_first | 5.0 bps | 743 | 41% | -0.082 | -13.0 | 34% | -1.30 | -0.37% | -3.04 | -16.4% | -23.7% |
| random | entry_first | 5.0 bps | 764 | 38% | -0.121 | -8.8 | 43% | -1.02 | -0.30% | -2.40 | -13.9% | -22.5% |
| etf | entry_first | 5.0 bps | 1006 | 32% | -0.357 | -9.2 | 56% | -1.79 | -0.40% | -4.19 | -17.4% | -22.4% |
