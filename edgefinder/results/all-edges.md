# Every edge tested, data to 2026-11-06

All 443 daily candidates from `scan.json`, generated with [report.md](report.md). Discovery runs to 2015-12-31; the holdout is everything after it. Alpha is the yearly return beyond buy and hold of the same funds, net of costs and bills; t is its Newey-West t statistic. The verdict column is the five-test confirmation for the ten discovery leaders and a plain sign label for everything else.

| count | candidates |
| --- | --- |
| holdout excess over bills above zero | 348 of 443 |
| holdout alpha above zero | 243 of 443 |
| holdout alpha t at least 2 | 12 of 443 |
| alpha above zero in both windows | 198 of 443 |
| confirmed (all five tests) | 0 of 443 |

Excess over bills is mostly market exposure: a strategy that holds equities beat bills in a rising decade. Alpha is the edge.

## overnight (48)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `overnight:VB:filter=sma200` | 6.7% (3.54) | 8.6% (3.63) | 1.13 | 10.0% | 1.22 | -16.3% | 190 | 4.0x | weak |
| `overnight:VB:filter=none` | 12.1% (4.25) | 7.9% (2.68) | 0.81 | 11.8% | 0.93 | -29.5% | 252 | 3.7x | weak |
| `overnight:VTI:filter=sma200` | 7.0% (4.47) | 5.6% (2.63) | 0.80 | 7.6% | 1.00 | -13.5% | 207 | 2.9x | weak |
| `overnight:SMH:filter=sma200` | -0.5% (-0.21) | 11.0% (2.73) | 0.75 | 19.5% | 1.12 | -23.5% | 210 | 3.8x | positive holdout only |
| `overnight:VO:filter=sma200` | 9.3% (4.70) | 4.3% (2.04) | 0.64 | 5.8% | 0.79 | -15.9% | 201 | 2.2x | weak |
| `overnight:IWM:filter=sma200` | 2.3% (1.43) | 4.2% (1.59) | 0.49 | 5.7% | 0.62 | -18.4% | 176 | 2.3x | positive both windows |
| `overnight:IWM:filter=none` | 1.1% (0.46) | 3.3% (1.04) | 0.30 | 7.1% | 0.50 | -29.5% | 252 | 2.1x | positive both windows |
| `overnight:SMH:filter=none` | -6.6% (-1.91) | 4.5% (1.01) | 0.29 | 17.2% | 0.82 | -40.4% | 252 | 3.0x | positive holdout only |
| `overnight:VO:filter=none` | 9.3% (3.63) | 2.0% (0.77) | 0.23 | 6.1% | 0.53 | -29.4% | 252 | 2.0x | rejected |
| `overnight:SLV:filter=sma200` | 0.3% (0.06) | 3.3% (0.74) | 0.22 | 9.8% | 0.47 | -33.0% | 147 | 2.6x | positive both windows |
| `overnight:XLI:filter=sma200` | 4.1% (2.04) | 1.4% (0.59) | 0.19 | 3.5% | 0.42 | -14.2% | 204 | 1.7x | positive both windows |
| `overnight:QQQ:filter=sma200` | 3.9% (2.10) | 1.7% (0.63) | 0.19 | 5.6% | 0.53 | -20.7% | 208 | 2.1x | positive both windows |
| `overnight:VTI:filter=none` | 7.8% (3.61) | 1.6% (0.57) | 0.18 | 6.8% | 0.59 | -31.3% | 252 | 2.4x | positive both windows |
| `overnight:SPY:filter=sma200` | 0.2% (0.16) | 1.1% (0.55) | 0.16 | 3.2% | 0.43 | -15.4% | 210 | 1.7x | positive both windows |
| `overnight:DIA:filter=sma200` | 0.4% (0.28) | 1.1% (0.53) | 0.16 | 3.1% | 0.42 | -16.5% | 214 | 1.7x | positive both windows |
| `overnight:XLK:filter=sma200` | 4.1% (2.08) | 1.3% (0.42) | 0.12 | 6.4% | 0.52 | -23.7% | 212 | 2.1x | positive both windows |
| `overnight:SLV:filter=none` | -2.1% (-0.40) | 1.2% (0.29) | 0.08 | 10.3% | 0.42 | -38.6% | 252 | 2.0x | positive holdout only |
| `overnight:XLY:filter=sma200` | 0.9% (0.50) | 0.6% (0.22) | 0.07 | 2.4% | 0.26 | -22.0% | 195 | 1.5x | positive both windows |
| `overnight:XLE:filter=none` | -2.2% (-0.85) | -0.2% (-0.04) | -0.01 | 5.2% | 0.28 | -38.8% | 252 | 1.6x | negative holdout |
| `overnight:XLE:filter=sma200` | 3.3% (1.90) | -0.6% (-0.19) | -0.06 | 1.5% | 0.12 | -21.5% | 165 | 1.3x | negative holdout |
| `overnight:XLRE:filter=none` | n/a | -0.8% (-0.29) | -0.09 | 1.2% | 0.11 | -41.4% | 252 | 1.1x | short history |
| `overnight:XLB:filter=sma200` | 1.0% (0.50) | -0.8% (-0.33) | -0.11 | 0.8% | 0.09 | -33.0% | 188 | 1.1x | negative holdout |
| `overnight:XLF:filter=sma200` | 2.4% (1.30) | -1.3% (-0.46) | -0.16 | 0.7% | 0.07 | -23.4% | 183 | 1.1x | negative holdout |
| `overnight:QQQ:filter=none` | -0.5% (-0.19) | -2.4% (-0.76) | -0.22 | 4.2% | 0.31 | -31.5% | 252 | 1.7x | negative holdout |
| `overnight:GLD:filter=none` | -4.5% (-1.66) | -1.8% (-0.86) | -0.24 | 4.9% | 0.39 | -23.9% | 252 | 1.7x | negative holdout |
| `overnight:XLY:filter=none` | -0.0% (-0.01) | -2.7% (-0.82) | -0.25 | 1.9% | 0.13 | -31.6% | 252 | 1.3x | negative holdout |
| `overnight:XLI:filter=none` | 4.7% (1.67) | -2.6% (-0.87) | -0.28 | 2.3% | 0.18 | -35.3% | 252 | 1.4x | negative holdout |
| `overnight:GLD:filter=sma200` | -0.2% (-0.07) | -2.2% (-0.95) | -0.28 | 2.8% | 0.26 | -24.3% | 179 | 1.5x | negative holdout |
| `overnight:DIA:filter=none` | -3.8% (-2.03) | -2.5% (-0.99) | -0.30 | 2.4% | 0.21 | -29.1% | 252 | 1.5x | negative holdout |
| `overnight:SPY:filter=none` | -3.7% (-1.76) | -2.9% (-1.07) | -0.34 | 2.4% | 0.21 | -29.8% | 252 | 1.4x | negative holdout |
| `overnight:XLK:filter=none` | 0.8% (0.27) | -5.0% (-1.31) | -0.42 | 3.4% | 0.22 | -36.2% | 252 | 1.5x | negative holdout |
| `overnight:XLRE:filter=sma200` | n/a | -2.5% (-1.27) | -0.42 | -2.0% | -0.30 | -20.0% | 171 | 0.7x | short history |
| `overnight:EEM:filter=sma200` | 7.4% (2.82) | -4.5% (-1.63) | -0.46 | -1.9% | -0.16 | -34.5% | 170 | 0.6x | negative holdout |
| `overnight:TLT:filter=sma200` | -3.9% (-2.15) | -3.2% (-1.25) | -0.49 | -3.7% | -0.49 | -39.7% | 127 | -0.2x | negative holdout |
| `overnight:XLV:filter=sma200` | 4.8% (2.66) | -3.8% (-1.70) | -0.58 | -2.1% | -0.28 | -26.2% | 191 | 0.6x | negative holdout |
| `overnight:XLF:filter=none` | 0.9% (0.26) | -6.3% (-1.71) | -0.58 | -0.6% | -0.04 | -50.8% | 252 | 0.9x | negative holdout |
| `overnight:XLB:filter=none` | -1.5% (-0.57) | -6.6% (-1.85) | -0.63 | -2.4% | -0.18 | -41.7% | 252 | 0.7x | negative holdout |
| `overnight:EFA:filter=sma200` | 0.2% (0.08) | -5.5% (-2.08) | -0.68 | -3.2% | -0.33 | -24.5% | 187 | 0.2x | negative holdout |
| `overnight:EEM:filter=none` | 3.2% (0.95) | -8.2% (-3.07) | -0.84 | -3.0% | -0.17 | -50.1% | 252 | 0.6x | negative holdout |
| `overnight:XLU:filter=none` | 7.3% (2.74) | -6.3% (-2.41) | -0.84 | -4.1% | -0.47 | -52.9% | 252 | 0.5x | negative holdout |
| `overnight:XLV:filter=none` | 2.7% (1.11) | -7.7% (-2.82) | -0.98 | -4.6% | -0.48 | -40.3% | 252 | 0.3x | negative holdout |
| `overnight:XLU:filter=sma200` | 5.7% (2.94) | -5.6% (-2.64) | -0.99 | -4.6% | -0.77 | -46.8% | 188 | 0.3x | rejected |
| `overnight:XLP:filter=none` | -1.2% (-0.49) | -7.5% (-3.07) | -1.10 | -5.3% | -0.65 | -37.8% | 252 | 0.1x | negative holdout |
| `overnight:EFA:filter=none` | -8.2% (-2.88) | -9.4% (-4.04) | -1.12 | -4.6% | -0.33 | -42.9% | 252 | 0.1x | negative holdout |
| `overnight:XLP:filter=sma200` | -0.8% (-0.46) | -6.7% (-3.71) | -1.19 | -5.5% | -0.88 | -36.9% | 195 | -0.2x | negative holdout |
| `overnight:TLT:filter=none` | -6.6% (-3.61) | -9.2% (-3.32) | -1.22 | -10.2% | -0.94 | -67.4% | 252 | -0.7x | negative holdout |
| `overnight:HYG:filter=sma200` | 4.7% (3.81) | -5.1% (-4.58) | -1.50 | -4.6% | -1.23 | -23.9% | 205 | 0.1x | rejected |
| `overnight:HYG:filter=none` | 7.1% (3.13) | -7.6% (-5.22) | -1.81 | -6.1% | -1.00 | -35.2% | 252 | 0.0x | rejected |

## rsi2 (96)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `rsi2:XLY:filter=sma200,max_hold=10,threshold=10` | 3.4% (2.01) | 5.8% (3.03) | 0.85 | 7.0% | 0.97 | -9.9% | 9 | 31.8x | positive both windows |
| `rsi2:TLT:filter=sma200,max_hold=10,threshold=5` | -0.1% (-0.14) | 1.1% (1.59) | 0.51 | 1.1% | 0.48 | -4.4% | 3 | 17.2x | positive holdout only |
| `rsi2:VO:filter=sma200,max_hold=10,threshold=10` | 1.2% (0.54) | 2.5% (1.48) | 0.44 | 3.5% | 0.59 | -9.7% | 9 | 17.1x | positive both windows |
| `rsi2:EFA:filter=sma200,max_hold=10,threshold=5` | 1.1% (0.95) | 1.4% (1.77) | 0.41 | 1.7% | 0.49 | -5.0% | 4 | 19.9x | positive both windows |
| `rsi2:VTI:filter=sma200,max_hold=10,threshold=5` | 0.2% (0.19) | 1.8% (1.45) | 0.39 | 2.8% | 0.56 | -8.9% | 5 | 31.0x | positive both windows |
| `rsi2:VTI:filter=sma200,max_hold=10,threshold=10` | 2.0% (1.29) | 2.1% (1.36) | 0.37 | 3.5% | 0.58 | -9.8% | 9 | 21.0x | positive both windows |
| `rsi2:TLT:filter=none,max_hold=10,threshold=5` | 0.6% (0.66) | 1.6% (1.33) | 0.37 | 1.4% | 0.31 | -12.1% | 7 | 9.6x | positive both windows |
| `rsi2:XLY:filter=none,max_hold=10,threshold=5` | 4.1% (1.67) | 3.0% (1.46) | 0.37 | 4.7% | 0.54 | -15.7% | 6 | 30.5x | positive both windows |
| `rsi2:XLI:filter=sma200,max_hold=10,threshold=10` | 2.6% (1.60) | 2.3% (1.27) | 0.36 | 3.7% | 0.55 | -14.7% | 9 | 18.2x | positive both windows |
| `rsi2:QQQ:filter=none,max_hold=10,threshold=5` | 5.9% (2.53) | 2.9% (1.45) | 0.35 | 5.5% | 0.62 | -9.5% | 6 | 36.8x | positive both windows |
| `rsi2:XLY:filter=sma200,max_hold=10,threshold=5` | 0.2% (0.19) | 1.8% (1.26) | 0.33 | 2.5% | 0.45 | -9.6% | 4 | 25.1x | positive both windows |
| `rsi2:SMH:filter=sma200,max_hold=10,threshold=5` | 2.8% (2.56) | 2.7% (1.38) | 0.33 | 4.7% | 0.55 | -13.2% | 4 | 33.6x | positive both windows |
| `rsi2:TLT:filter=sma200,max_hold=10,threshold=10` | 0.3% (0.25) | 1.1% (1.15) | 0.32 | 1.0% | 0.28 | -6.4% | 5 | 8.5x | positive both windows |
| `rsi2:SPY:filter=sma200,max_hold=10,threshold=10` | 2.1% (1.49) | 1.8% (1.17) | 0.32 | 3.3% | 0.54 | -8.6% | 9 | 18.7x | positive both windows |
| `rsi2:VTI:filter=none,max_hold=10,threshold=5` | 1.3% (0.62) | 2.0% (1.19) | 0.31 | 3.9% | 0.55 | -10.7% | 6 | 34.0x | positive both windows |
| `rsi2:DIA:filter=sma200,max_hold=10,threshold=10` | 0.9% (0.65) | 1.8% (1.13) | 0.31 | 3.2% | 0.52 | -9.6% | 9 | 18.5x | positive both windows |
| `rsi2:GLD:filter=none,max_hold=10,threshold=5` | 0.0% (0.01) | 1.6% (1.04) | 0.30 | 2.8% | 0.50 | -12.2% | 6 | 17.8x | positive both windows |
| `rsi2:SPY:filter=sma200,max_hold=10,threshold=5` | 1.0% (0.86) | 1.4% (1.08) | 0.29 | 2.4% | 0.48 | -8.6% | 5 | 24.4x | positive both windows |
| `rsi2:XLY:filter=none,max_hold=10,threshold=10` | 5.5% (2.27) | 2.7% (0.86) | 0.26 | 6.2% | 0.50 | -27.1% | 12 | 21.2x | positive both windows |
| `rsi2:SLV:filter=sma200,max_hold=10,threshold=5` | 2.9% (1.44) | 1.5% (1.18) | 0.24 | 2.0% | 0.32 | -7.3% | 3 | 16.9x | positive both windows |
| `rsi2:QQQ:filter=sma200,max_hold=10,threshold=5` | 1.8% (1.40) | 1.4% (1.02) | 0.23 | 2.8% | 0.44 | -8.0% | 4 | 25.9x | positive both windows |
| `rsi2:GLD:filter=sma200,max_hold=10,threshold=5` | 0.8% (0.57) | 1.0% (0.88) | 0.23 | 1.9% | 0.40 | -7.5% | 3 | 20.3x | positive both windows |
| `rsi2:XLK:filter=none,max_hold=10,threshold=5` | 5.6% (2.33) | 2.1% (0.95) | 0.23 | 5.1% | 0.52 | -11.8% | 6 | 34.7x | positive both windows |
| `rsi2:IWM:filter=sma200,max_hold=10,threshold=5` | 0.9% (0.54) | 1.2% (0.67) | 0.22 | 1.8% | 0.31 | -12.4% | 4 | 17.7x | positive both windows |
| `rsi2:SMH:filter=none,max_hold=10,threshold=5` | 7.9% (2.60) | 2.5% (0.92) | 0.21 | 6.5% | 0.53 | -13.0% | 6 | 32.6x | positive both windows |
| `rsi2:VB:filter=sma200,max_hold=10,threshold=5` | -0.2% (-0.10) | 1.0% (0.66) | 0.19 | 1.7% | 0.31 | -10.0% | 4 | 23.5x | positive holdout only |
| `rsi2:SPY:filter=none,max_hold=10,threshold=5` | 1.6% (0.88) | 1.2% (0.73) | 0.19 | 3.2% | 0.45 | -10.2% | 6 | 26.3x | positive both windows |
| `rsi2:EEM:filter=sma200,max_hold=10,threshold=10` | 1.9% (0.96) | 1.1% (0.66) | 0.18 | 1.8% | 0.28 | -11.5% | 7 | 10.2x | positive both windows |
| `rsi2:XLB:filter=sma200,max_hold=10,threshold=10` | 0.2% (0.11) | 1.1% (0.58) | 0.17 | 2.2% | 0.32 | -10.1% | 8 | 9.4x | positive both windows |
| `rsi2:VB:filter=sma200,max_hold=10,threshold=10` | 1.8% (0.80) | 1.1% (0.55) | 0.16 | 2.2% | 0.31 | -9.7% | 8 | 15.9x | positive both windows |
| `rsi2:GLD:filter=sma200,max_hold=10,threshold=10` | 0.7% (0.34) | 0.9% (0.48) | 0.15 | 2.4% | 0.39 | -12.3% | 7 | 13.5x | positive both windows |
| `rsi2:IWM:filter=sma200,max_hold=10,threshold=10` | 2.8% (1.48) | 1.0% (0.51) | 0.15 | 2.0% | 0.28 | -16.2% | 7 | 11.9x | positive both windows |
| `rsi2:EEM:filter=sma200,max_hold=10,threshold=5` | 0.7% (0.49) | 0.7% (0.63) | 0.15 | 1.1% | 0.23 | -10.2% | 4 | 12.2x | positive both windows |
| `rsi2:XLRE:filter=none,max_hold=10,threshold=5` | n/a | 0.8% (0.49) | 0.13 | 1.5% | 0.23 | -10.5% | 6 | 8.1x | short history |
| `rsi2:HYG:filter=sma200,max_hold=10,threshold=10` | -0.1% (-0.10) | 0.3% (0.52) | 0.13 | 0.5% | 0.23 | -3.1% | 8 | 3.5x | positive holdout only |
| `rsi2:HYG:filter=sma200,max_hold=10,threshold=5` | 0.2% (0.36) | 0.2% (0.54) | 0.13 | 0.4% | 0.21 | -2.4% | 5 | 4.5x | positive both windows |
| `rsi2:XLF:filter=sma200,max_hold=10,threshold=10` | 4.5% (3.16) | 0.9% (0.46) | 0.13 | 2.0% | 0.29 | -11.4% | 8 | 10.0x | positive both windows |
| `rsi2:XLP:filter=sma200,max_hold=10,threshold=5` | 0.2% (0.26) | 0.5% (0.47) | 0.13 | 1.0% | 0.25 | -10.2% | 4 | 10.8x | positive both windows |
| `rsi2:SMH:filter=sma200,max_hold=10,threshold=10` | 4.4% (2.75) | 1.1% (0.43) | 0.10 | 4.5% | 0.40 | -18.7% | 8 | 18.7x | positive both windows |
| `rsi2:XLP:filter=none,max_hold=10,threshold=5` | 1.1% (0.84) | 0.5% (0.38) | 0.10 | 1.4% | 0.26 | -10.7% | 7 | 9.4x | positive both windows |
| `rsi2:XLI:filter=sma200,max_hold=10,threshold=5` | 2.2% (2.20) | 0.5% (0.34) | 0.09 | 1.4% | 0.26 | -12.8% | 5 | 13.4x | positive both windows |
| `rsi2:DIA:filter=sma200,max_hold=10,threshold=5` | 0.2% (0.23) | 0.5% (0.38) | 0.09 | 1.4% | 0.27 | -9.8% | 5 | 14.9x | positive both windows |
| `rsi2:XLB:filter=sma200,max_hold=10,threshold=5` | 0.2% (0.10) | 0.5% (0.32) | 0.09 | 1.2% | 0.22 | -10.1% | 4 | 10.2x | positive both windows |
| `rsi2:VO:filter=none,max_hold=10,threshold=5` | 2.4% (0.82) | 0.6% (0.31) | 0.09 | 1.9% | 0.27 | -13.2% | 6 | 14.1x | positive both windows |
| `rsi2:DIA:filter=none,max_hold=10,threshold=5` | 0.5% (0.27) | 0.5% (0.34) | 0.09 | 2.1% | 0.32 | -10.0% | 7 | 17.1x | positive both windows |
| `rsi2:VO:filter=sma200,max_hold=10,threshold=5` | -0.4% (-0.21) | 0.3% (0.27) | 0.08 | 0.9% | 0.21 | -9.8% | 4 | 10.5x | positive holdout only |
| `rsi2:XLV:filter=sma200,max_hold=10,threshold=10` | 2.3% (1.72) | 0.4% (0.24) | 0.07 | 1.5% | 0.26 | -11.2% | 8 | 8.4x | positive both windows |
| `rsi2:XLF:filter=none,max_hold=10,threshold=10` | 10.8% (2.74) | 0.7% (0.21) | 0.06 | 4.4% | 0.35 | -25.3% | 12 | 13.3x | positive both windows |
| `rsi2:XLB:filter=none,max_hold=10,threshold=5` | 2.5% (0.94) | 0.4% (0.22) | 0.06 | 1.8% | 0.23 | -10.1% | 6 | 10.1x | positive both windows |
| `rsi2:XLF:filter=sma200,max_hold=10,threshold=5` | 2.8% (2.88) | 0.3% (0.21) | 0.05 | 1.1% | 0.19 | -11.4% | 4 | 9.5x | positive both windows |
| `rsi2:GLD:filter=none,max_hold=10,threshold=10` | -0.8% (-0.33) | 0.3% (0.16) | 0.05 | 2.7% | 0.35 | -15.8% | 11 | 9.6x | positive holdout only |
| `rsi2:TLT:filter=none,max_hold=10,threshold=10` | 0.7% (0.59) | 0.2% (0.16) | 0.04 | -0.1% | -0.02 | -20.9% | 12 | 0.5x | positive both windows |
| `rsi2:XLK:filter=sma200,max_hold=10,threshold=10` | 1.8% (1.27) | 0.3% (0.12) | 0.03 | 3.0% | 0.33 | -12.1% | 8 | 15.9x | positive both windows |
| `rsi2:QQQ:filter=sma200,max_hold=10,threshold=10` | 3.7% (2.34) | 0.1% (0.04) | 0.01 | 2.4% | 0.29 | -8.8% | 8 | 12.6x | positive both windows |
| `rsi2:XLF:filter=none,max_hold=10,threshold=5` | 7.1% (2.20) | -0.0% (-0.00) | -0.00 | 2.9% | 0.26 | -25.3% | 8 | 13.6x | negative holdout |
| `rsi2:XLI:filter=none,max_hold=10,threshold=5` | 1.9% (0.69) | -0.0% (-0.01) | -0.00 | 1.6% | 0.21 | -18.8% | 6 | 11.7x | negative holdout |
| `rsi2:XLP:filter=sma200,max_hold=10,threshold=10` | 0.8% (0.73) | -0.1% (-0.10) | -0.03 | 0.7% | 0.14 | -11.9% | 8 | 4.7x | negative holdout |
| `rsi2:IWM:filter=none,max_hold=10,threshold=10` | 4.3% (1.42) | -0.3% (-0.09) | -0.03 | 2.6% | 0.21 | -34.8% | 12 | 9.9x | negative holdout |
| `rsi2:XLP:filter=none,max_hold=10,threshold=10` | 1.5% (0.89) | -0.2% (-0.12) | -0.03 | 1.5% | 0.20 | -12.2% | 11 | 6.4x | negative holdout |
| `rsi2:XLV:filter=none,max_hold=10,threshold=5` | 0.2% (0.09) | -0.3% (-0.17) | -0.05 | 1.0% | 0.16 | -12.6% | 6 | 7.2x | negative holdout |
| `rsi2:XLK:filter=sma200,max_hold=10,threshold=5` | 1.5% (1.68) | -0.3% (-0.19) | -0.05 | 1.6% | 0.21 | -11.8% | 4 | 14.7x | negative holdout |
| `rsi2:SLV:filter=none,max_hold=10,threshold=5` | -5.0% (-1.24) | -0.5% (-0.18) | -0.05 | 0.9% | 0.09 | -26.6% | 7 | 4.1x | negative holdout |
| `rsi2:XLV:filter=none,max_hold=10,threshold=10` | 2.3% (1.00) | -0.4% (-0.19) | -0.06 | 2.2% | 0.25 | -18.4% | 12 | 8.2x | negative holdout |
| `rsi2:XLRE:filter=sma200,max_hold=10,threshold=10` | n/a | -0.4% (-0.25) | -0.07 | 0.1% | 0.01 | -10.0% | 7 | 1.3x | short history |
| `rsi2:EFA:filter=sma200,max_hold=10,threshold=10` | 2.0% (1.52) | -0.6% (-0.35) | -0.10 | 0.3% | 0.06 | -14.7% | 7 | 3.1x | negative holdout |
| `rsi2:VTI:filter=none,max_hold=10,threshold=10` | 3.9% (1.53) | -0.9% (-0.34) | -0.10 | 3.8% | 0.34 | -29.4% | 12 | 17.3x | negative holdout |
| `rsi2:VO:filter=none,max_hold=10,threshold=10` | 3.8% (1.31) | -0.9% (-0.30) | -0.10 | 2.7% | 0.24 | -33.6% | 12 | 9.9x | negative holdout |
| `rsi2:XLK:filter=none,max_hold=10,threshold=10` | 5.0% (1.84) | -1.3% (-0.39) | -0.11 | 5.0% | 0.37 | -20.6% | 10 | 19.3x | negative holdout |
| `rsi2:EFA:filter=none,max_hold=10,threshold=5` | 2.6% (1.19) | -1.0% (-0.40) | -0.13 | 0.9% | 0.10 | -24.9% | 7 | 6.8x | negative holdout |
| `rsi2:VB:filter=none,max_hold=10,threshold=10` | 4.4% (1.35) | -1.4% (-0.40) | -0.13 | 2.0% | 0.17 | -36.0% | 12 | 10.8x | negative holdout |
| `rsi2:SPY:filter=none,max_hold=10,threshold=10` | 3.7% (1.50) | -1.3% (-0.49) | -0.15 | 3.6% | 0.33 | -28.2% | 11 | 15.5x | negative holdout |
| `rsi2:XLI:filter=none,max_hold=10,threshold=10` | 3.6% (1.24) | -1.4% (-0.46) | -0.15 | 2.8% | 0.24 | -44.0% | 12 | 10.8x | negative holdout |
| `rsi2:XLE:filter=sma200,max_hold=10,threshold=5` | 2.9% (1.79) | -1.0% (-0.48) | -0.15 | -0.3% | -0.04 | -16.8% | 4 | -1.1x | negative holdout |
| `rsi2:QQQ:filter=none,max_hold=10,threshold=10` | 6.4% (2.27) | -1.6% (-0.57) | -0.15 | 4.0% | 0.32 | -19.2% | 11 | 15.4x | negative holdout |
| `rsi2:XLB:filter=none,max_hold=10,threshold=10` | 2.4% (0.85) | -1.5% (-0.53) | -0.16 | 1.5% | 0.14 | -26.6% | 12 | 5.0x | negative holdout |
| `rsi2:XLV:filter=sma200,max_hold=10,threshold=5` | 1.5% (1.22) | -0.8% (-0.63) | -0.17 | -0.0% | -0.01 | -11.1% | 4 | 0.5x | negative holdout |
| `rsi2:EEM:filter=none,max_hold=10,threshold=5` | 1.8% (0.69) | -1.5% (-0.58) | -0.17 | 0.2% | 0.02 | -28.8% | 7 | 2.3x | negative holdout |
| `rsi2:IWM:filter=none,max_hold=10,threshold=5` | 3.4% (1.25) | -1.7% (-0.54) | -0.18 | 0.5% | 0.05 | -33.4% | 7 | 4.1x | negative holdout |
| `rsi2:EEM:filter=none,max_hold=10,threshold=10` | 4.8% (1.53) | -1.8% (-0.63) | -0.19 | 0.6% | 0.05 | -29.2% | 12 | 2.8x | negative holdout |
| `rsi2:XLE:filter=sma200,max_hold=10,threshold=10` | 3.5% (1.85) | -1.9% (-0.63) | -0.21 | -0.6% | -0.06 | -27.4% | 7 | -1.4x | negative holdout |
| `rsi2:SMH:filter=none,max_hold=10,threshold=10` | 8.7% (2.50) | -3.3% (-0.82) | -0.22 | 5.1% | 0.30 | -25.3% | 10 | 15.8x | negative holdout |
| `rsi2:XLU:filter=sma200,max_hold=10,threshold=10` | -0.2% (-0.20) | -1.3% (-0.78) | -0.23 | -0.4% | -0.06 | -18.2% | 7 | -0.5x | negative holdout |
| `rsi2:XLRE:filter=none,max_hold=10,threshold=10` | n/a | -2.3% (-0.79) | -0.24 | -0.2% | -0.02 | -33.3% | 11 | 0.5x | short history |
| `rsi2:XLU:filter=sma200,max_hold=10,threshold=5` | -0.2% (-0.21) | -1.3% (-0.88) | -0.25 | -0.6% | -0.11 | -19.3% | 4 | -3.0x | negative holdout |
| `rsi2:SLV:filter=sma200,max_hold=10,threshold=10` | -0.6% (-0.15) | -2.8% (-1.10) | -0.27 | -1.0% | -0.09 | -24.8% | 5 | -3.8x | negative holdout |
| `rsi2:DIA:filter=none,max_hold=10,threshold=10` | 1.4% (0.65) | -2.3% (-0.81) | -0.28 | 2.0% | 0.19 | -29.8% | 12 | 9.6x | negative holdout |
| `rsi2:VB:filter=none,max_hold=10,threshold=5` | 1.4% (0.45) | -2.6% (-0.82) | -0.28 | 0.0% | 0.00 | -34.7% | 6 | 1.3x | negative holdout |
| `rsi2:XLRE:filter=sma200,max_hold=10,threshold=5` | n/a | -1.1% (-0.95) | -0.29 | -0.9% | -0.24 | -9.2% | 3 | -7.9x | short history |
| `rsi2:XLU:filter=none,max_hold=10,threshold=5` | -2.4% (-1.18) | -2.5% (-1.31) | -0.33 | -0.7% | -0.08 | -23.4% | 6 | -2.0x | negative holdout |
| `rsi2:HYG:filter=none,max_hold=10,threshold=5` | -0.9% (-0.45) | -1.2% (-0.91) | -0.33 | -0.4% | -0.08 | -17.8% | 7 | -1.2x | negative holdout |
| `rsi2:HYG:filter=none,max_hold=10,threshold=10` | -2.1% (-1.03) | -1.4% (-1.03) | -0.35 | -0.4% | -0.08 | -19.3% | 11 | -0.3x | negative holdout |
| `rsi2:XLE:filter=none,max_hold=10,threshold=10` | 4.3% (1.39) | -5.1% (-1.23) | -0.38 | -1.5% | -0.10 | -54.2% | 13 | -2.5x | negative holdout |
| `rsi2:SLV:filter=none,max_hold=10,threshold=10` | -3.0% (-0.64) | -5.0% (-1.38) | -0.40 | -2.1% | -0.15 | -31.1% | 11 | -3.4x | negative holdout |
| `rsi2:XLE:filter=none,max_hold=10,threshold=5` | 2.8% (1.16) | -5.1% (-1.29) | -0.42 | -2.2% | -0.16 | -51.3% | 7 | -7.8x | negative holdout |
| `rsi2:XLU:filter=none,max_hold=10,threshold=10` | -0.7% (-0.32) | -3.7% (-1.76) | -0.44 | -1.0% | -0.10 | -23.5% | 10 | -1.8x | negative holdout |
| `rsi2:EFA:filter=none,max_hold=10,threshold=10` | 2.0% (0.80) | -4.1% (-1.65) | -0.50 | -1.7% | -0.17 | -27.6% | 11 | -6.2x | negative holdout |

## intraday (48)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `intraday:VB:direction=-1` | 11.8% (4.25) | 7.6% (2.60) | 0.78 | 1.3% | 0.08 | -25.0% | 252 | 1.3x | weak |
| `intraday:SMH:direction=-1` | -7.0% (-2.02) | 4.3% (0.96) | 0.27 | -13.5% | -0.54 | -86.3% | 252 | -0.6x | positive holdout only |
| `intraday:IWM:direction=-1` | 0.8% (0.33) | 2.9% (0.92) | 0.26 | -3.1% | -0.17 | -39.3% | 252 | 0.5x | positive both windows |
| `intraday:VO:direction=-1` | 9.0% (3.48) | 1.7% (0.65) | 0.20 | -4.2% | -0.30 | -38.6% | 252 | 0.3x | positive both windows |
| `intraday:VTI:direction=-1` | 7.5% (3.47) | 1.2% (0.44) | 0.14 | -6.0% | -0.44 | -49.8% | 252 | -0.2x | positive both windows |
| `intraday:SLV:direction=-1` | -2.1% (-0.38) | 1.2% (0.28) | 0.08 | -4.5% | -0.23 | -63.3% | 252 | 0.6x | positive holdout only |
| `intraday:XLE:direction=-1` | -2.2% (-0.85) | -0.4% (-0.12) | -0.03 | -7.6% | -0.35 | -72.7% | 252 | 0.1x | negative holdout |
| `intraday:XLRE:direction=-1` | n/a | -1.1% (-0.40) | -0.13 | -5.4% | -0.33 | -58.3% | 252 | 0.4x | short history |
| `intraday:EFA:direction=1` | -2.8% (-0.97) | -1.5% (-0.62) | -0.17 | 1.0% | 0.10 | -27.5% | 252 | 1.2x | negative holdout |
| `intraday:QQQ:direction=-1` | -0.8% (-0.32) | -2.8% (-0.89) | -0.26 | -13.4% | -0.77 | -80.0% | 252 | -1.1x | negative holdout |
| `intraday:GLD:direction=-1` | -4.6% (-1.70) | -2.2% (-1.01) | -0.28 | -6.6% | -0.65 | -51.7% | 252 | 0.1x | negative holdout |
| `intraday:XLY:direction=-1` | -0.7% (-0.22) | -3.2% (-0.97) | -0.29 | -9.9% | -0.58 | -68.5% | 252 | -0.6x | negative holdout |
| `intraday:XLI:direction=-1` | 4.1% (1.48) | -3.0% (-1.00) | -0.32 | -9.8% | -0.66 | -70.4% | 252 | -0.6x | negative holdout |
| `intraday:DIA:direction=-1` | -4.0% (-2.13) | -2.9% (-1.16) | -0.35 | -9.1% | -0.72 | -63.0% | 252 | -0.9x | negative holdout |
| `intraday:SPY:direction=-1` | -3.9% (-1.85) | -3.3% (-1.22) | -0.39 | -10.7% | -0.80 | -68.6% | 252 | -1.0x | negative holdout |
| `intraday:TLT:direction=1` | -5.9% (-3.26) | -3.2% (-1.16) | -0.42 | -4.1% | -0.40 | -33.4% | 252 | 0.3x | negative holdout |
| `intraday:XLK:direction=-1` | 0.2% (0.06) | -5.5% (-1.42) | -0.45 | -17.7% | -0.93 | -87.9% | 252 | -1.6x | negative holdout |
| `intraday:EEM:direction=1` | -17.6% (-5.29) | -5.5% (-2.06) | -0.56 | -3.1% | -0.26 | -40.1% | 252 | 0.6x | negative holdout |
| `intraday:XLV:direction=1` | -15.0% (-6.05) | -4.8% (-1.76) | -0.61 | 1.5% | 0.11 | -27.7% | 252 | 1.2x | negative holdout |
| `intraday:XLF:direction=-1` | 0.3% (0.10) | -6.7% (-1.84) | -0.61 | -13.0% | -0.82 | -75.5% | 252 | -0.7x | negative holdout |
| `intraday:XLB:direction=-1` | -1.8% (-0.66) | -7.1% (-1.95) | -0.67 | -12.8% | -0.80 | -77.3% | 252 | -0.6x | negative holdout |
| `intraday:XLP:direction=1` | -10.6% (-4.51) | -4.7% (-1.93) | -0.69 | -0.0% | -0.00 | -25.8% | 252 | 1.0x | negative holdout |
| `intraday:XLK:direction=1` | -13.9% (-4.66) | -8.5% (-2.21) | -0.70 | 3.8% | 0.20 | -26.0% | 252 | 1.6x | negative holdout |
| `intraday:XLF:direction=1` | -15.6% (-4.64) | -8.7% (-2.40) | -0.80 | -2.5% | -0.15 | -48.7% | 252 | 0.7x | negative holdout |
| `intraday:XLB:direction=1` | -14.4% (-5.36) | -9.2% (-2.52) | -0.86 | -3.5% | -0.22 | -41.8% | 252 | 0.6x | negative holdout |
| `intraday:EEM:direction=-1` | 3.6% (1.09) | -8.6% (-3.22) | -0.88 | -11.1% | -0.93 | -69.2% | 252 | -0.6x | negative holdout |
| `intraday:XLU:direction=-1` | 6.7% (2.54) | -6.6% (-2.58) | -0.89 | -13.5% | -0.83 | -79.6% | 252 | -0.5x | negative holdout |
| `intraday:XLY:direction=1` | -12.2% (-3.85) | -9.8% (-2.96) | -0.89 | -3.1% | -0.18 | -46.5% | 252 | 0.5x | negative holdout |
| `intraday:DIA:direction=1` | -6.1% (-3.25) | -7.4% (-2.97) | -0.90 | -1.2% | -0.09 | -31.7% | 252 | 0.8x | negative holdout |
| `intraday:SPY:direction=1` | -7.3% (-3.47) | -8.1% (-2.95) | -0.94 | -0.7% | -0.05 | -27.5% | 252 | 0.9x | negative holdout |
| `intraday:QQQ:direction=1` | -12.1% (-4.77) | -10.3% (-3.29) | -0.96 | 0.4% | 0.02 | -29.9% | 252 | 1.1x | negative holdout |
| `intraday:XLI:direction=1` | -16.6% (-5.93) | -9.6% (-3.21) | -1.02 | -2.8% | -0.19 | -39.4% | 252 | 0.5x | negative holdout |
| `intraday:XLV:direction=-1` | 2.2% (0.88) | -8.2% (-2.99) | -1.04 | -14.5% | -1.06 | -77.5% | 252 | -1.3x | negative holdout |
| `intraday:XLP:direction=-1` | -1.8% (-0.76) | -7.9% (-3.25) | -1.16 | -12.6% | -1.03 | -71.1% | 252 | -1.1x | negative holdout |
| `intraday:HYG:direction=1` | -19.7% (-8.67) | -4.9% (-3.32) | -1.16 | -3.5% | -0.62 | -26.5% | 252 | 0.4x | negative holdout |
| `intraday:EFA:direction=-1` | -8.4% (-2.93) | -9.9% (-4.23) | -1.17 | -12.3% | -1.20 | -72.3% | 252 | -1.3x | negative holdout |
| `intraday:XLE:direction=1` | -15.8% (-6.07) | -17.7% (-4.63) | -1.26 | -10.5% | -0.48 | -79.4% | 252 | -0.2x | negative holdout |
| `intraday:TLT:direction=-1` | -6.7% (-3.70) | -9.6% (-3.51) | -1.28 | -8.7% | -0.86 | -56.0% | 252 | -0.4x | negative holdout |
| `intraday:VTI:direction=1` | -17.8% (-8.19) | -11.7% (-4.19) | -1.33 | -4.4% | -0.33 | -41.6% | 252 | 0.1x | negative holdout |
| `intraday:SMH:direction=1` | -10.3% (-2.97) | -21.7% (-4.88) | -1.37 | -3.9% | -0.16 | -52.5% | 252 | 0.5x | negative holdout |
| `intraday:IWM:direction=1` | -13.6% (-5.54) | -15.9% (-5.03) | -1.44 | -9.9% | -0.56 | -72.2% | 252 | -0.6x | negative holdout |
| `intraday:SLV:direction=1` | -18.6% (-3.48) | -22.1% (-5.27) | -1.49 | -16.4% | -0.85 | -83.1% | 252 | -0.6x | negative holdout |
| `intraday:XLU:direction=1` | -25.1% (-9.47) | -11.9% (-4.63) | -1.59 | -5.1% | -0.31 | -60.1% | 252 | 0.4x | negative holdout |
| `intraday:VO:direction=1` | -21.6% (-8.36) | -14.5% (-5.58) | -1.67 | -8.6% | -0.61 | -59.2% | 252 | -0.4x | negative holdout |
| `intraday:GLD:direction=1` | -10.3% (-3.81) | -12.9% (-6.01) | -1.69 | -8.5% | -0.85 | -56.3% | 252 | -0.2x | negative holdout |
| `intraday:VB:direction=1` | -20.9% (-7.52) | -16.9% (-5.78) | -1.73 | -10.5% | -0.65 | -71.4% | 252 | -1.4x | negative holdout |
| `intraday:HYG:direction=-1` | 7.0% (3.09) | -8.1% (-5.48) | -1.91 | -9.5% | -1.66 | -56.8% | 252 | -0.5x | rejected |
| `intraday:XLRE:direction=1` | n/a | -17.1% (-6.12) | -1.96 | -12.8% | -0.79 | -73.2% | 252 | -0.4x | short history |

## pre_holiday (24)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `pre_holiday:XLRE` | n/a | 2.4% (2.58) | 0.75 | 2.5% | 0.79 | -4.9% | 9 | 8.5x | short history |
| `pre_holiday:VB` | 1.2% (1.11) | 1.7% (2.13) | 0.62 | 1.9% | 0.68 | -5.9% | 9 | 12.6x | positive both windows |
| `pre_holiday:VO` | 1.1% (1.22) | 1.5% (2.11) | 0.61 | 1.7% | 0.68 | -5.6% | 9 | 8.5x | positive both windows |
| `pre_holiday:XLU` | 0.0% (0.01) | 1.6% (1.89) | 0.55 | 1.9% | 0.62 | -5.5% | 9 | 6.5x | positive both windows |
| `pre_holiday:IWM` | 0.2% (0.16) | 1.7% (1.89) | 0.55 | 1.9% | 0.60 | -6.1% | 9 | 9.1x | positive both windows |
| `pre_holiday:XLB` | 0.9% (1.06) | 1.6% (1.88) | 0.54 | 1.8% | 0.61 | -5.4% | 9 | 7.1x | positive both windows |
| `pre_holiday:XLP` | -0.3% (-0.54) | 1.1% (1.70) | 0.51 | 1.2% | 0.57 | -5.2% | 9 | 6.5x | positive holdout only |
| `pre_holiday:XLF` | 1.2% (0.83) | 1.5% (1.59) | 0.46 | 1.8% | 0.54 | -6.6% | 9 | 7.5x | positive both windows |
| `pre_holiday:GLD` | 1.1% (1.26) | 1.1% (1.51) | 0.45 | 1.4% | 0.55 | -2.5% | 9 | 6.1x | positive both windows |
| `pre_holiday:HYG` | 0.5% (0.79) | 1.0% (1.82) | 0.45 | 1.2% | 0.53 | -1.3% | 9 | 6.1x | positive both windows |
| `pre_holiday:EFA` | 0.9% (1.21) | 1.1% (1.46) | 0.44 | 1.2% | 0.50 | -3.7% | 9 | 7.0x | positive both windows |
| `pre_holiday:VTI` | 0.6% (0.82) | 1.1% (1.48) | 0.44 | 1.3% | 0.53 | -5.6% | 9 | 7.9x | positive both windows |
| `pre_holiday:SPY` | 0.5% (0.61) | 0.9% (1.22) | 0.37 | 1.1% | 0.46 | -5.7% | 9 | 6.4x | positive both windows |
| `pre_holiday:XLI` | 1.1% (1.30) | 0.9% (1.14) | 0.34 | 1.1% | 0.42 | -7.2% | 9 | 5.8x | positive both windows |
| `pre_holiday:XLY` | 1.5% (1.64) | 0.9% (1.02) | 0.30 | 1.1% | 0.37 | -5.4% | 9 | 5.8x | positive both windows |
| `pre_holiday:EEM` | 3.2% (2.70) | 0.9% (0.96) | 0.29 | 1.0% | 0.34 | -4.0% | 9 | 5.0x | positive both windows |
| `pre_holiday:SLV` | 2.8% (1.47) | 1.3% (0.87) | 0.27 | 1.7% | 0.34 | -11.3% | 9 | 5.4x | positive both windows |
| `pre_holiday:XLV` | -0.4% (-0.75) | 0.6% (0.85) | 0.25 | 0.7% | 0.32 | -5.0% | 9 | 4.2x | positive holdout only |
| `pre_holiday:XLK` | -0.1% (-0.10) | 0.6% (0.55) | 0.17 | 1.0% | 0.28 | -6.3% | 9 | 4.8x | positive holdout only |
| `pre_holiday:DIA` | 0.2% (0.24) | 0.4% (0.54) | 0.16 | 0.6% | 0.24 | -5.6% | 9 | 4.0x | positive both windows |
| `pre_holiday:QQQ` | 0.3% (0.27) | 0.5% (0.53) | 0.16 | 0.9% | 0.27 | -6.2% | 9 | 4.6x | positive both windows |
| `pre_holiday:XLE` | 1.8% (1.83) | 0.1% (0.10) | 0.03 | 0.4% | 0.09 | -9.5% | 9 | 2.2x | positive both windows |
| `pre_holiday:SMH` | 1.5% (1.07) | 0.2% (0.10) | 0.03 | 0.9% | 0.18 | -8.4% | 9 | 3.9x | positive both windows |
| `pre_holiday:TLT` | -1.7% (-2.50) | 0.1% (0.08) | 0.03 | 0.0% | 0.00 | -3.7% | 9 | 1.0x | positive holdout only |

## xs_momentum (18)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `xs_momentum:universe24:absolute=0,lookback=252,top=1` | 5.4% (0.90) | 17.5% (2.06) | 0.60 | 27.3% | 0.85 | -44.3% | 3 | 288.1x | positive both windows |
| `xs_momentum:universe24:absolute=1,lookback=252,top=1` | 5.4% (0.91) | 17.5% (2.06) | 0.60 | 27.3% | 0.85 | -44.3% | 3 | 288.1x | positive both windows |
| `xs_momentum:universe24:absolute=0,lookback=63,top=1` | -2.4% (-0.39) | 10.6% (1.28) | 0.37 | 19.0% | 0.61 | -44.3% | 7 | 91.4x | positive holdout only |
| `xs_momentum:universe24:absolute=1,lookback=63,top=1` | -1.4% (-0.22) | 10.4% (1.25) | 0.36 | 18.7% | 0.60 | -44.3% | 6 | 91.1x | positive holdout only |
| `xs_momentum:universe24:absolute=0,lookback=252,top=3` | 5.0% (1.41) | 2.9% (0.68) | 0.19 | 14.3% | 0.66 | -24.6% | 10 | 152.4x | positive both windows |
| `xs_momentum:universe24:absolute=1,lookback=252,top=3` | 6.0% (1.59) | 2.7% (0.63) | 0.18 | 13.9% | 0.64 | -24.7% | 9 | 151.3x | positive both windows |
| `xs_momentum:universe24:absolute=0,lookback=63,top=3` | 1.2% (0.36) | 2.0% (0.48) | 0.14 | 11.2% | 0.57 | -25.4% | 19 | 61.5x | positive both windows |
| `xs_momentum:universe24:absolute=1,lookback=63,top=3` | 3.6% (0.98) | 1.4% (0.33) | 0.09 | 10.2% | 0.52 | -25.4% | 18 | 57.7x | positive both windows |
| `xs_momentum:universe24:absolute=0,lookback=63,top=6` | 1.1% (0.54) | 0.6% (0.24) | 0.07 | 10.6% | 0.64 | -22.5% | 30 | 76.6x | positive both windows |
| `xs_momentum:universe24:absolute=1,lookback=63,top=6` | 3.8% (1.36) | 0.5% (0.18) | 0.05 | 8.7% | 0.56 | -17.8% | 29 | 64.9x | positive both windows |
| `xs_momentum:universe24:absolute=0,lookback=252,top=6` | 3.9% (1.77) | 0.2% (0.08) | 0.02 | 11.0% | 0.62 | -21.4% | 16 | 152.1x | positive both windows |
| `xs_momentum:universe24:absolute=0,lookback=126,top=1` | 7.0% (1.25) | -1.1% (-0.14) | -0.04 | 11.9% | 0.36 | -52.8% | 6 | 66.8x | negative holdout |
| `xs_momentum:universe24:absolute=1,lookback=252,top=6` | 5.3% (1.90) | -0.5% (-0.18) | -0.05 | 9.7% | 0.56 | -22.3% | 16 | 134.9x | negative holdout |
| `xs_momentum:universe24:absolute=0,lookback=126,top=3` | 2.3% (0.72) | -0.8% (-0.20) | -0.06 | 10.9% | 0.49 | -37.7% | 13 | 88.0x | negative holdout |
| `xs_momentum:universe24:absolute=1,lookback=126,top=3` | 5.1% (1.42) | -1.2% (-0.29) | -0.08 | 10.2% | 0.47 | -37.7% | 13 | 82.4x | negative holdout |
| `xs_momentum:universe24:absolute=1,lookback=126,top=1` | 7.7% (1.37) | -2.6% (-0.33) | -0.09 | 10.3% | 0.31 | -52.8% | 6 | 56.6x | negative holdout |
| `xs_momentum:universe24:absolute=0,lookback=126,top=6` | 2.2% (1.12) | -1.1% (-0.40) | -0.12 | 9.7% | 0.55 | -28.6% | 22 | 95.4x | negative holdout |
| `xs_momentum:universe24:absolute=1,lookback=126,top=6` | 5.0% (1.82) | -1.5% (-0.52) | -0.15 | 8.3% | 0.49 | -29.7% | 22 | 82.1x | negative holdout |

## turn_of_month (48)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `turn_of_month:HYG:post=4,pre=2` | -0.5% (-0.26) | 1.9% (1.88) | 0.56 | 2.6% | 0.66 | -6.2% | 12 | 9.5x | positive holdout only |
| `turn_of_month:XLRE:post=4,pre=2` | n/a | 3.4% (1.38) | 0.38 | 5.0% | 0.49 | -12.3% | 12 | 12.8x | short history |
| `turn_of_month:EEM:post=3,pre=1` | 5.8% (1.87) | 2.9% (1.14) | 0.35 | 4.2% | 0.47 | -12.4% | 12 | 13.9x | positive both windows |
| `turn_of_month:GLD:post=3,pre=1` | -1.1% (-0.48) | 2.5% (1.13) | 0.35 | 5.2% | 0.64 | -14.2% | 12 | 16.0x | positive holdout only |
| `turn_of_month:XLF:post=4,pre=2` | 2.1% (0.61) | 3.2% (1.12) | 0.33 | 6.3% | 0.57 | -16.6% | 12 | 18.6x | positive both windows |
| `turn_of_month:XLV:post=3,pre=1` | 1.4% (0.84) | 2.1% (1.04) | 0.32 | 4.0% | 0.54 | -11.2% | 12 | 14.2x | positive both windows |
| `turn_of_month:GLD:post=4,pre=2` | 0.7% (0.28) | 2.3% (0.89) | 0.29 | 5.9% | 0.63 | -14.5% | 12 | 17.9x | positive both windows |
| `turn_of_month:XLV:post=4,pre=2` | 0.3% (0.16) | 2.2% (0.96) | 0.29 | 5.0% | 0.55 | -11.8% | 12 | 17.5x | positive both windows |
| `turn_of_month:XLI:post=4,pre=2` | 0.8% (0.33) | 2.3% (0.85) | 0.26 | 5.5% | 0.53 | -15.7% | 12 | 19.8x | positive both windows |
| `turn_of_month:DIA:post=4,pre=2` | 0.4% (0.20) | 1.9% (0.82) | 0.24 | 4.9% | 0.54 | -12.7% | 12 | 21.8x | positive both windows |
| `turn_of_month:XLB:post=4,pre=2` | 2.0% (0.70) | 2.2% (0.80) | 0.24 | 5.1% | 0.46 | -19.5% | 12 | 14.4x | positive both windows |
| `turn_of_month:HYG:post=3,pre=1` | 1.9% (1.22) | 0.7% (0.66) | 0.23 | 1.1% | 0.35 | -6.5% | 12 | 4.7x | positive both windows |
| `turn_of_month:VO:post=4,pre=2` | 0.6% (0.21) | 1.9% (0.77) | 0.23 | 4.5% | 0.47 | -14.4% | 12 | 16.2x | positive both windows |
| `turn_of_month:EEM:post=4,pre=2` | 5.2% (1.42) | 2.0% (0.68) | 0.21 | 4.2% | 0.38 | -13.9% | 12 | 13.7x | positive both windows |
| `turn_of_month:SPY:post=4,pre=2` | 1.1% (0.49) | 1.6% (0.65) | 0.20 | 5.1% | 0.55 | -17.9% | 12 | 20.5x | positive both windows |
| `turn_of_month:VTI:post=4,pre=2` | 0.9% (0.41) | 1.5% (0.63) | 0.19 | 5.0% | 0.52 | -17.7% | 12 | 21.8x | positive both windows |
| `turn_of_month:TLT:post=3,pre=1` | -3.9% (-2.63) | 1.1% (0.63) | 0.19 | 0.7% | 0.10 | -14.9% | 12 | 3.4x | positive holdout only |
| `turn_of_month:VB:post=4,pre=2` | -0.3% (-0.10) | 1.3% (0.46) | 0.14 | 4.0% | 0.37 | -20.0% | 12 | 19.9x | positive holdout only |
| `turn_of_month:XLB:post=3,pre=1` | 0.7% (0.28) | 1.1% (0.43) | 0.13 | 3.0% | 0.33 | -14.6% | 12 | 8.8x | positive both windows |
| `turn_of_month:XLY:post=4,pre=2` | 0.5% (0.21) | 1.2% (0.42) | 0.12 | 4.4% | 0.38 | -20.8% | 12 | 15.6x | positive both windows |
| `turn_of_month:TLT:post=4,pre=2` | -1.0% (-0.61) | 0.7% (0.37) | 0.11 | 0.2% | 0.02 | -25.4% | 12 | 1.6x | positive holdout only |
| `turn_of_month:DIA:post=3,pre=1` | 1.0% (0.57) | 0.7% (0.34) | 0.10 | 2.6% | 0.36 | -10.1% | 12 | 12.0x | positive both windows |
| `turn_of_month:QQQ:post=4,pre=2` | 2.1% (0.74) | 1.0% (0.32) | 0.10 | 6.1% | 0.50 | -22.5% | 12 | 21.1x | positive both windows |
| `turn_of_month:XLP:post=4,pre=2` | -0.0% (-0.02) | 0.6% (0.34) | 0.09 | 2.6% | 0.32 | -10.9% | 12 | 9.8x | positive holdout only |
| `turn_of_month:QQQ:post=3,pre=1` | 1.3% (0.53) | 0.7% (0.27) | 0.08 | 3.9% | 0.41 | -14.6% | 12 | 13.9x | positive both windows |
| `turn_of_month:VTI:post=3,pre=1` | 1.1% (0.59) | 0.5% (0.25) | 0.08 | 2.7% | 0.36 | -9.5% | 12 | 12.2x | positive both windows |
| `turn_of_month:SPY:post=3,pre=1` | 1.4% (0.76) | 0.5% (0.25) | 0.08 | 2.8% | 0.37 | -9.1% | 12 | 11.5x | positive both windows |
| `turn_of_month:XLU:post=4,pre=2` | -1.9% (-0.90) | 0.5% (0.20) | 0.06 | 3.2% | 0.31 | -17.4% | 12 | 8.3x | positive holdout only |
| `turn_of_month:XLE:post=4,pre=2` | -0.3% (-0.11) | 0.7% (0.17) | 0.05 | 3.9% | 0.26 | -29.0% | 12 | 10.1x | positive holdout only |
| `turn_of_month:XLI:post=3,pre=1` | 0.9% (0.43) | 0.2% (0.08) | 0.02 | 2.3% | 0.27 | -13.4% | 12 | 8.9x | positive both windows |
| `turn_of_month:EFA:post=4,pre=2` | 1.9% (0.74) | 0.2% (0.07) | 0.02 | 2.1% | 0.24 | -14.2% | 12 | 9.1x | positive both windows |
| `turn_of_month:XLE:post=3,pre=1` | 0.7% (0.27) | 0.2% (0.06) | 0.02 | 2.5% | 0.20 | -20.9% | 12 | 6.8x | positive both windows |
| `turn_of_month:SLV:post=3,pre=1` | 0.9% (0.19) | 0.2% (0.05) | 0.02 | 4.2% | 0.25 | -33.7% | 12 | 9.6x | positive both windows |
| `turn_of_month:VO:post=3,pre=1` | 1.3% (0.58) | 0.1% (0.04) | 0.01 | 1.8% | 0.23 | -13.5% | 12 | 7.0x | positive both windows |
| `turn_of_month:XLP:post=3,pre=1` | 0.6% (0.48) | 0.1% (0.04) | 0.01 | 1.3% | 0.21 | -9.9% | 12 | 5.6x | positive both windows |
| `turn_of_month:XLRE:post=3,pre=1` | n/a | 0.0% (0.02) | 0.01 | 1.1% | 0.13 | -13.9% | 12 | 3.6x | short history |
| `turn_of_month:IWM:post=4,pre=2` | -0.1% (-0.05) | 0.0% (0.00) | 0.00 | 2.7% | 0.23 | -21.7% | 12 | 9.9x | positive holdout only |
| `turn_of_month:XLU:post=3,pre=1` | -0.3% (-0.16) | -0.2% (-0.11) | -0.03 | 1.4% | 0.18 | -14.6% | 12 | 4.3x | negative holdout |
| `turn_of_month:XLY:post=3,pre=1` | 1.7% (0.76) | -0.5% (-0.18) | -0.05 | 1.6% | 0.17 | -13.4% | 12 | 6.4x | negative holdout |
| `turn_of_month:VB:post=3,pre=1` | 0.4% (0.15) | -0.5% (-0.20) | -0.07 | 1.2% | 0.14 | -17.7% | 12 | 6.9x | negative holdout |
| `turn_of_month:XLK:post=3,pre=1` | 2.4% (1.00) | -0.7% (-0.23) | -0.07 | 3.3% | 0.30 | -15.7% | 12 | 11.1x | negative holdout |
| `turn_of_month:EFA:post=3,pre=1` | 2.6% (1.19) | -0.6% (-0.26) | -0.09 | 0.7% | 0.10 | -11.9% | 12 | 3.6x | negative holdout |
| `turn_of_month:SMH:post=3,pre=1` | 3.0% (0.85) | -1.1% (-0.28) | -0.09 | 4.6% | 0.32 | -21.6% | 12 | 12.2x | negative holdout |
| `turn_of_month:XLK:post=4,pre=2` | 2.4% (0.89) | -1.3% (-0.37) | -0.12 | 4.9% | 0.36 | -31.9% | 12 | 16.3x | negative holdout |
| `turn_of_month:XLF:post=3,pre=1` | 1.3% (0.46) | -1.0% (-0.40) | -0.12 | 1.0% | 0.11 | -18.2% | 12 | 3.8x | negative holdout |
| `turn_of_month:SMH:post=4,pre=2` | 5.0% (1.36) | -2.0% (-0.40) | -0.13 | 7.0% | 0.39 | -36.7% | 12 | 18.3x | negative holdout |
| `turn_of_month:SLV:post=4,pre=2` | 3.3% (0.59) | -2.2% (-0.38) | -0.14 | 3.3% | 0.17 | -45.1% | 12 | 7.7x | negative holdout |
| `turn_of_month:IWM:post=3,pre=1` | -0.1% (-0.02) | -1.5% (-0.54) | -0.17 | 0.2% | 0.02 | -21.0% | 12 | 1.8x | negative holdout |

## trend_sma (48)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `trend_sma:SMH:window=200` | -0.5% (-0.15) | 7.1% (1.71) | 0.45 | 27.1% | 1.01 | -30.0% | 2 | 316.7x | positive holdout only |
| `trend_sma:XLK:window=50` | 0.2% (0.06) | 5.1% (1.42) | 0.41 | 13.7% | 0.86 | -20.5% | 8 | 64.1x | positive both windows |
| `trend_sma:VTI:window=200` | 3.6% (1.50) | 3.3% (1.16) | 0.37 | 8.8% | 0.73 | -23.7% | 2 | 181.4x | positive both windows |
| `trend_sma:XLK:window=200` | 1.5% (0.54) | 4.4% (1.40) | 0.36 | 16.9% | 0.88 | -24.6% | 2 | 334.2x | positive both windows |
| `trend_sma:QQQ:window=50` | 0.9% (0.32) | 3.8% (1.17) | 0.34 | 10.7% | 0.76 | -20.8% | 8 | 56.7x | positive both windows |
| `trend_sma:SPY:window=200` | 3.4% (1.53) | 2.9% (1.02) | 0.32 | 8.7% | 0.72 | -22.0% | 2 | 160.2x | positive both windows |
| `trend_sma:XLY:window=50` | 0.3% (0.09) | 3.1% (0.96) | 0.29 | 7.6% | 0.55 | -21.9% | 10 | 31.2x | positive both windows |
| `trend_sma:QQQ:window=200` | 3.5% (1.21) | 3.2% (1.08) | 0.29 | 13.2% | 0.78 | -22.8% | 2 | 216.2x | positive both windows |
| `trend_sma:EFA:window=50` | 3.0% (1.19) | 2.1% (0.83) | 0.25 | 5.1% | 0.46 | -17.5% | 9 | 26.7x | positive both windows |
| `trend_sma:EFA:window=200` | 5.8% (2.18) | 2.0% (0.70) | 0.23 | 5.4% | 0.45 | -14.9% | 4 | 61.5x | positive both windows |
| `trend_sma:XLF:window=50` | -0.3% (-0.07) | 2.4% (0.73) | 0.22 | 7.1% | 0.52 | -22.5% | 9 | 26.5x | positive holdout only |
| `trend_sma:VTI:window=50` | 1.4% (0.64) | 1.9% (0.68) | 0.21 | 6.6% | 0.59 | -25.3% | 8 | 42.5x | positive both windows |
| `trend_sma:DIA:window=50` | -0.2% (-0.08) | 1.7% (0.67) | 0.21 | 5.8% | 0.56 | -19.3% | 7 | 44.4x | positive holdout only |
| `trend_sma:SMH:window=50` | -3.6% (-0.91) | 3.3% (0.71) | 0.20 | 18.4% | 0.79 | -32.1% | 9 | 59.3x | positive holdout only |
| `trend_sma:SPY:window=50` | 1.1% (0.52) | 1.6% (0.61) | 0.19 | 6.5% | 0.59 | -18.7% | 8 | 36.1x | positive both windows |
| `trend_sma:XLF:window=200` | 2.6% (0.92) | 2.1% (0.60) | 0.19 | 7.2% | 0.50 | -28.3% | 4 | 58.7x | positive both windows |
| `trend_sma:GLD:window=50` | -1.2% (-0.41) | 1.4% (0.64) | 0.18 | 8.9% | 0.65 | -23.6% | 8 | 38.1x | positive holdout only |
| `trend_sma:VO:window=50` | 1.4% (0.45) | 1.5% (0.50) | 0.16 | 5.2% | 0.45 | -19.4% | 8 | 27.1x | positive both windows |
| `trend_sma:DIA:window=200` | 1.3% (0.58) | 1.3% (0.44) | 0.15 | 6.6% | 0.55 | -19.7% | 4 | 90.3x | positive both windows |
| `trend_sma:VO:window=200` | 4.5% (1.47) | 1.2% (0.41) | 0.13 | 5.4% | 0.44 | -25.2% | 3 | 87.3x | positive both windows |
| `trend_sma:XLI:window=200` | 4.1% (1.62) | 1.1% (0.34) | 0.11 | 6.9% | 0.50 | -21.2% | 3 | 104.1x | positive both windows |
| `trend_sma:EEM:window=200` | 3.3% (0.87) | 1.2% (0.40) | 0.11 | 5.1% | 0.34 | -23.2% | 4 | 43.9x | positive both windows |
| `trend_sma:HYG:window=200` | 4.1% (1.95) | 0.3% (0.31) | 0.09 | 1.4% | 0.29 | -10.3% | 4 | 16.3x | positive both windows |
| `trend_sma:VB:window=200` | 2.3% (0.71) | 0.9% (0.26) | 0.08 | 5.3% | 0.38 | -28.9% | 4 | 74.9x | positive both windows |
| `trend_sma:IWM:window=200` | 0.6% (0.19) | 0.8% (0.23) | 0.07 | 5.2% | 0.34 | -30.6% | 4 | 50.0x | positive both windows |
| `trend_sma:XLE:window=200` | 4.9% (1.46) | 1.0% (0.20) | 0.07 | 6.9% | 0.34 | -49.8% | 5 | 41.3x | positive both windows |
| `trend_sma:VB:window=50` | -1.2% (-0.36) | 0.5% (0.15) | 0.04 | 4.8% | 0.35 | -32.9% | 9 | 32.1x | positive holdout only |
| `trend_sma:IWM:window=50` | 0.0% (0.01) | 0.3% (0.10) | 0.03 | 4.7% | 0.31 | -35.8% | 9 | 21.9x | positive both windows |
| `trend_sma:HYG:window=50` | 3.7% (1.76) | 0.1% (0.06) | 0.02 | 1.1% | 0.23 | -11.9% | 8 | 6.1x | positive both windows |
| `trend_sma:XLY:window=200` | 4.3% (1.59) | 0.2% (0.05) | 0.02 | 5.2% | 0.36 | -30.3% | 4 | 51.8x | positive both windows |
| `trend_sma:TLT:window=50` | -1.6% (-0.91) | 0.1% (0.03) | 0.01 | -0.9% | -0.09 | -29.7% | 10 | -2.7x | positive holdout only |
| `trend_sma:EEM:window=50` | 5.4% (1.50) | -0.2% (-0.06) | -0.02 | 3.1% | 0.23 | -30.6% | 9 | 13.4x | negative holdout |
| `trend_sma:XLV:window=50` | 0.6% (0.26) | -0.3% (-0.13) | -0.04 | 3.6% | 0.33 | -22.4% | 9 | 17.3x | negative holdout |
| `trend_sma:TLT:window=200` | -0.8% (-0.51) | -0.5% (-0.25) | -0.07 | -1.5% | -0.15 | -38.7% | 6 | -9.8x | negative holdout |
| `trend_sma:SLV:window=50` | 0.0% (0.00) | -1.7% (-0.33) | -0.11 | 7.6% | 0.29 | -50.1% | 9 | 22.0x | negative holdout |
| `trend_sma:XLI:window=50` | 2.4% (0.93) | -1.1% (-0.36) | -0.12 | 3.8% | 0.29 | -24.6% | 9 | 19.1x | negative holdout |
| `trend_sma:XLB:window=200` | -2.4% (-0.75) | -1.4% (-0.46) | -0.14 | 3.2% | 0.23 | -29.4% | 5 | 20.0x | negative holdout |
| `trend_sma:XLU:window=200` | 2.5% (1.06) | -1.4% (-0.55) | -0.15 | 3.3% | 0.24 | -33.1% | 5 | 20.7x | negative holdout |
| `trend_sma:GLD:window=200` | 1.7% (0.55) | -1.2% (-0.55) | -0.17 | 7.2% | 0.50 | -27.0% | 4 | 60.7x | negative holdout |
| `trend_sma:XLU:window=50` | 1.2% (0.53) | -1.6% (-0.56) | -0.17 | 2.0% | 0.17 | -20.4% | 9 | 7.2x | negative holdout |
| `trend_sma:XLE:window=50` | -0.3% (-0.08) | -2.7% (-0.57) | -0.18 | 2.8% | 0.14 | -50.6% | 10 | 9.2x | negative holdout |
| `trend_sma:SLV:window=200` | 1.0% (0.18) | -3.4% (-0.72) | -0.24 | 7.4% | 0.27 | -50.4% | 5 | 38.8x | negative holdout |
| `trend_sma:XLRE:window=50` | n/a | -2.8% (-0.91) | -0.28 | -0.3% | -0.02 | -25.9% | 11 | 0.2x | short history |
| `trend_sma:XLP:window=50` | -0.0% (-0.01) | -2.3% (-1.00) | -0.31 | 0.7% | 0.07 | -20.8% | 10 | 4.1x | negative holdout |
| `trend_sma:XLV:window=200` | 0.6% (0.27) | -3.4% (-1.50) | -0.41 | 1.9% | 0.15 | -24.3% | 7 | 12.4x | negative holdout |
| `trend_sma:XLB:window=50` | -1.7% (-0.57) | -4.5% (-1.45) | -0.45 | -0.4% | -0.03 | -35.0% | 10 | -0.4x | negative holdout |
| `trend_sma:XLRE:window=200` | n/a | -4.8% (-1.54) | -0.48 | -2.5% | -0.19 | -28.1% | 7 | -9.8x | short history |
| `trend_sma:XLP:window=200` | 0.6% (0.37) | -4.1% (-2.11) | -0.57 | -0.0% | -0.00 | -23.2% | 6 | 0.8x | negative holdout |

## weekday_overnight (15)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `weekday_overnight:IWM:weekday=1` | 2.3% (1.55) | 2.6% (1.37) | 0.44 | 3.2% | 0.53 | -10.6% | 52 | 3.5x | positive both windows |
| `weekday_overnight:QQQ:weekday=2` | 0.8% (0.53) | 2.1% (1.26) | 0.39 | 3.1% | 0.56 | -11.1% | 52 | 3.4x | positive both windows |
| `weekday_overnight:SPY:weekday=1` | 0.4% (0.31) | 1.7% (1.12) | 0.37 | 2.6% | 0.55 | -5.6% | 52 | 3.3x | positive both windows |
| `weekday_overnight:IWM:weekday=2` | -0.8% (-0.60) | 2.0% (1.13) | 0.34 | 2.6% | 0.44 | -13.6% | 52 | 3.1x | positive holdout only |
| `weekday_overnight:QQQ:weekday=1` | 0.4% (0.32) | 1.9% (1.04) | 0.33 | 3.1% | 0.53 | -9.2% | 52 | 3.4x | positive both windows |
| `weekday_overnight:SPY:weekday=2` | -0.8% (-0.71) | 1.0% (0.70) | 0.22 | 1.8% | 0.39 | -13.0% | 52 | 2.6x | positive holdout only |
| `weekday_overnight:IWM:weekday=4` | -0.1% (-0.04) | 0.7% (0.36) | 0.11 | 1.4% | 0.22 | -10.4% | 51 | 2.1x | positive holdout only |
| `weekday_overnight:IWM:weekday=0` | -0.5% (-0.37) | 0.2% (0.09) | 0.03 | 1.2% | 0.16 | -18.8% | 47 | 2.0x | positive holdout only |
| `weekday_overnight:SPY:weekday=4` | -1.0% (-0.70) | -0.5% (-0.33) | -0.10 | 0.5% | 0.11 | -7.7% | 51 | 1.5x | negative holdout |
| `weekday_overnight:QQQ:weekday=4` | -1.1% (-0.67) | -1.7% (-0.99) | -0.30 | -0.5% | -0.08 | -12.8% | 51 | 0.6x | negative holdout |
| `weekday_overnight:QQQ:weekday=3` | 0.2% (0.12) | -2.1% (-1.10) | -0.35 | -0.7% | -0.11 | -25.5% | 51 | 0.5x | negative holdout |
| `weekday_overnight:IWM:weekday=3` | 0.2% (0.17) | -2.1% (-1.16) | -0.36 | -1.4% | -0.23 | -22.9% | 51 | -0.1x | negative holdout |
| `weekday_overnight:QQQ:weekday=0` | -0.8% (-0.51) | -2.6% (-1.15) | -0.40 | -0.9% | -0.13 | -21.9% | 47 | 0.2x | negative holdout |
| `weekday_overnight:SPY:weekday=0` | -1.7% (-1.24) | -2.6% (-1.26) | -0.45 | -1.1% | -0.17 | -22.0% | 47 | -0.1x | negative holdout |
| `weekday_overnight:SPY:weekday=3` | -0.6% (-0.53) | -2.5% (-1.66) | -0.54 | -1.5% | -0.30 | -22.2% | 51 | -0.3x | negative holdout |

## vol_target (48)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `vol_target:QQQ:band=0.1,lookback=20,target=0.1` | 2.4% (1.97) | 2.0% (1.45) | 0.43 | 9.8% | 0.88 | -14.8% | 0 | 294.3x | positive both windows |
| `vol_target:GLD:band=0.1,lookback=63,target=0.1` | 0.5% (0.42) | 1.3% (1.34) | 0.41 | 8.0% | 0.76 | -15.1% | 0 | 1000.8x | positive both windows |
| `vol_target:GLD:band=0.1,lookback=20,target=0.1` | 0.4% (0.29) | 1.4% (1.17) | 0.36 | 8.1% | 0.76 | -17.0% | 0 | 218.7x | positive both windows |
| `vol_target:XLK:band=0.1,lookback=20,target=0.1` | 2.2% (1.71) | 1.7% (1.16) | 0.34 | 10.3% | 0.90 | -13.1% | 0 | 291.8x | positive both windows |
| `vol_target:SPY:band=0.1,lookback=20,target=0.1` | 1.5% (1.26) | 1.5% (0.99) | 0.31 | 8.2% | 0.78 | -13.0% | 0 | 327.4x | positive both windows |
| `vol_target:XLK:band=0.1,lookback=63,target=0.1` | 1.5% (1.31) | 1.2% (1.05) | 0.29 | 10.0% | 0.89 | -14.2% | 0 | 1075.1x | positive both windows |
| `vol_target:SMH:band=0.1,lookback=20,target=0.1` | 1.0% (0.87) | 1.2% (0.98) | 0.27 | 11.0% | 0.95 | -12.8% | 0 | 362.8x | positive both windows |
| `vol_target:DIA:band=0.1,lookback=20,target=0.1` | 1.1% (0.92) | 1.3% (0.80) | 0.26 | 7.1% | 0.69 | -13.6% | 0 | 305.0x | positive both windows |
| `vol_target:VTI:band=0.1,lookback=20,target=0.1` | 1.5% (1.20) | 1.2% (0.84) | 0.26 | 7.7% | 0.73 | -15.1% | 0 | 331.2x | positive both windows |
| `vol_target:SMH:band=0.1,lookback=63,target=0.1` | 0.6% (0.55) | 0.8% (0.83) | 0.23 | 11.1% | 0.95 | -14.6% | 0 | 1592.1x | positive both windows |
| `vol_target:XLY:band=0.1,lookback=20,target=0.1` | 1.1% (0.86) | 1.1% (0.75) | 0.23 | 6.2% | 0.57 | -16.1% | 0 | 216.0x | positive both windows |
| `vol_target:HYG:band=0.1,lookback=20,target=0.1` | 1.1% (0.75) | 0.5% (0.75) | 0.22 | 2.6% | 0.41 | -14.4% | 0 | 357.1x | positive both windows |
| `vol_target:DIA:band=0.1,lookback=63,target=0.1` | 1.3% (1.11) | 0.9% (0.66) | 0.20 | 7.2% | 0.67 | -20.0% | 0 | 973.8x | positive both windows |
| `vol_target:TLT:band=0.1,lookback=63,target=0.1` | 1.0% (1.61) | 0.5% (0.62) | 0.18 | -0.8% | -0.08 | -31.9% | 0 | -149.4x | positive both windows |
| `vol_target:XLV:band=0.1,lookback=20,target=0.1` | 0.5% (0.55) | 0.6% (0.49) | 0.15 | 6.1% | 0.58 | -13.3% | 0 | 197.7x | positive both windows |
| `vol_target:QQQ:band=0.1,lookback=63,target=0.1` | 1.9% (1.66) | 0.5% (0.48) | 0.14 | 8.7% | 0.78 | -16.5% | 0 | 880.2x | positive both windows |
| `vol_target:XLU:band=0.1,lookback=63,target=0.1` | 2.5% (2.37) | 0.5% (0.45) | 0.13 | 5.2% | 0.49 | -18.2% | 0 | 511.2x | positive both windows |
| `vol_target:EEM:band=0.1,lookback=63,target=0.1` | -0.4% (-0.27) | 0.4% (0.40) | 0.11 | 4.2% | 0.38 | -22.7% | 0 | 540.7x | positive holdout only |
| `vol_target:SLV:band=0.1,lookback=63,target=0.1` | -1.0% (-0.87) | 0.4% (0.28) | 0.08 | 5.1% | 0.45 | -19.2% | 0 | 989.9x | positive holdout only |
| `vol_target:SPY:band=0.1,lookback=63,target=0.1` | 1.3% (1.12) | 0.3% (0.27) | 0.08 | 7.5% | 0.70 | -19.2% | 0 | 786.0x | positive both windows |
| `vol_target:XLV:band=0.1,lookback=63,target=0.1` | 1.1% (1.19) | 0.3% (0.26) | 0.08 | 5.9% | 0.56 | -15.3% | 0 | 703.6x | positive both windows |
| `vol_target:SLV:band=0.1,lookback=20,target=0.1` | -1.3% (-0.84) | 0.4% (0.25) | 0.08 | 5.1% | 0.44 | -21.6% | 0 | 134.7x | positive holdout only |
| `vol_target:XLF:band=0.1,lookback=63,target=0.1` | 1.7% (1.17) | 0.3% (0.26) | 0.08 | 6.0% | 0.53 | -21.2% | 0 | 544.7x | positive both windows |
| `vol_target:XLY:band=0.1,lookback=63,target=0.1` | 0.7% (0.64) | 0.2% (0.16) | 0.05 | 5.5% | 0.50 | -18.8% | 0 | 702.0x | positive both windows |
| `vol_target:XLU:band=0.1,lookback=20,target=0.1` | 2.1% (1.95) | 0.1% (0.10) | 0.03 | 4.7% | 0.44 | -17.5% | 0 | 89.2x | positive both windows |
| `vol_target:XLP:band=0.1,lookback=20,target=0.1` | 1.1% (1.36) | 0.1% (0.09) | 0.03 | 4.4% | 0.43 | -12.6% | 0 | 176.5x | positive both windows |
| `vol_target:XLF:band=0.1,lookback=20,target=0.1` | 1.4% (0.89) | 0.1% (0.08) | 0.02 | 5.6% | 0.49 | -16.5% | 0 | 118.1x | positive both windows |
| `vol_target:VTI:band=0.1,lookback=63,target=0.1` | 1.3% (1.11) | 0.1% (0.04) | 0.01 | 6.9% | 0.64 | -19.9% | 0 | 807.1x | positive both windows |
| `vol_target:TLT:band=0.1,lookback=20,target=0.1` | -0.1% (-0.17) | 0.0% (0.03) | 0.01 | -1.3% | -0.12 | -33.4% | 0 | -53.2x | positive holdout only |
| `vol_target:HYG:band=0.1,lookback=63,target=0.1` | 0.7% (0.50) | -0.0% (-0.04) | -0.01 | 2.3% | 0.34 | -16.3% | 0 | 949.8x | negative holdout |
| `vol_target:XLP:band=0.1,lookback=63,target=0.1` | 1.9% (2.50) | -0.0% (-0.04) | -0.01 | 4.5% | 0.43 | -13.3% | 0 | 535.4x | negative holdout |
| `vol_target:XLRE:band=0.1,lookback=20,target=0.1` | n/a | -0.1% (-0.06) | -0.02 | 3.0% | 0.27 | -19.0% | 0 | 61.6x | short history |
| `vol_target:XLI:band=0.1,lookback=20,target=0.1` | 1.5% (1.26) | -0.2% (-0.12) | -0.04 | 5.7% | 0.52 | -15.5% | 0 | 172.5x | negative holdout |
| `vol_target:XLI:band=0.1,lookback=63,target=0.1` | 1.0% (0.90) | -0.3% (-0.24) | -0.07 | 5.8% | 0.53 | -21.3% | 0 | 686.3x | negative holdout |
| `vol_target:XLRE:band=0.1,lookback=63,target=0.1` | n/a | -0.3% (-0.27) | -0.08 | 3.0% | 0.28 | -18.3% | 0 | 256.4x | short history |
| `vol_target:VO:band=0.1,lookback=20,target=0.1` | 2.3% (1.47) | -0.6% (-0.36) | -0.11 | 4.5% | 0.42 | -16.4% | 0 | 151.0x | negative holdout |
| `vol_target:EEM:band=0.1,lookback=20,target=0.1` | 0.5% (0.32) | -0.5% (-0.39) | -0.11 | 3.3% | 0.29 | -23.8% | 0 | 92.4x | negative holdout |
| `vol_target:VO:band=0.1,lookback=63,target=0.1` | 1.3% (0.89) | -0.5% (-0.41) | -0.12 | 4.8% | 0.44 | -22.2% | 0 | 459.8x | negative holdout |
| `vol_target:VB:band=0.1,lookback=20,target=0.1` | 1.2% (0.79) | -1.1% (-0.74) | -0.22 | 3.7% | 0.34 | -17.0% | 0 | 170.2x | negative holdout |
| `vol_target:XLB:band=0.1,lookback=63,target=0.1` | 0.3% (0.24) | -1.0% (-0.88) | -0.26 | 3.9% | 0.36 | -21.2% | 0 | 422.1x | negative holdout |
| `vol_target:EFA:band=0.1,lookback=20,target=0.1` | 1.0% (0.83) | -1.1% (-0.89) | -0.26 | 3.0% | 0.28 | -20.1% | 0 | 104.4x | negative holdout |
| `vol_target:EFA:band=0.1,lookback=63,target=0.1` | 1.0% (0.89) | -1.0% (-0.92) | -0.27 | 3.2% | 0.30 | -21.6% | 0 | 406.4x | negative holdout |
| `vol_target:IWM:band=0.1,lookback=20,target=0.1` | 0.5% (0.40) | -1.3% (-0.97) | -0.28 | 3.1% | 0.28 | -19.1% | 0 | 111.9x | negative holdout |
| `vol_target:XLE:band=0.1,lookback=20,target=0.1` | 1.1% (0.83) | -1.5% (-0.89) | -0.29 | 2.8% | 0.25 | -36.0% | 0 | 79.9x | negative holdout |
| `vol_target:XLB:band=0.1,lookback=20,target=0.1` | 0.4% (0.32) | -1.6% (-1.14) | -0.34 | 3.2% | 0.29 | -22.0% | 0 | 72.7x | negative holdout |
| `vol_target:XLE:band=0.1,lookback=63,target=0.1` | 1.1% (1.02) | -1.6% (-1.12) | -0.36 | 2.8% | 0.25 | -37.0% | 0 | 362.6x | negative holdout |
| `vol_target:VB:band=0.1,lookback=63,target=0.1` | 1.0% (0.78) | -1.5% (-1.39) | -0.41 | 3.4% | 0.31 | -24.5% | 0 | 622.7x | negative holdout |
| `vol_target:IWM:band=0.1,lookback=63,target=0.1` | 0.7% (0.68) | -1.9% (-1.80) | -0.53 | 2.6% | 0.23 | -26.7% | 0 | 339.7x | negative holdout |

## tsmom (48)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `tsmom:QQQ:lookback=126` | 3.8% (1.32) | 4.5% (1.52) | 0.41 | 14.7% | 0.86 | -25.4% | 3 | 216.8x | positive both windows |
| `tsmom:QQQ:lookback=252` | 5.4% (1.86) | 3.5% (1.23) | 0.35 | 15.7% | 0.84 | -25.4% | 3 | 235.5x | positive both windows |
| `tsmom:SMH:lookback=126` | 2.1% (0.56) | 5.2% (1.22) | 0.33 | 25.3% | 0.94 | -32.5% | 4 | 171.7x | positive both windows |
| `tsmom:HYG:lookback=252` | 3.3% (1.51) | 1.1% (1.02) | 0.28 | 2.0% | 0.43 | -7.4% | 2 | 39.5x | positive both windows |
| `tsmom:DIA:lookback=126` | 3.0% (1.46) | 2.3% (0.82) | 0.27 | 7.0% | 0.62 | -14.6% | 4 | 95.4x | positive both windows |
| `tsmom:HYG:lookback=126` | 2.7% (1.28) | 1.0% (0.93) | 0.26 | 1.8% | 0.41 | -9.3% | 5 | 14.7x | positive both windows |
| `tsmom:XLF:lookback=126` | 4.7% (1.67) | 2.8% (0.85) | 0.26 | 7.9% | 0.55 | -29.0% | 5 | 56.7x | positive both windows |
| `tsmom:VTI:lookback=252` | 3.8% (1.50) | 1.8% (0.77) | 0.20 | 8.7% | 0.65 | -27.2% | 3 | 154.5x | positive both windows |
| `tsmom:EEM:lookback=252` | 2.8% (0.71) | 2.1% (0.67) | 0.20 | 5.7% | 0.39 | -20.8% | 3 | 77.8x | positive both windows |
| `tsmom:IWM:lookback=252` | 3.0% (1.01) | 2.2% (0.65) | 0.19 | 6.0% | 0.42 | -19.7% | 3 | 71.9x | positive both windows |
| `tsmom:XLI:lookback=126` | 3.4% (1.33) | 1.8% (0.57) | 0.18 | 7.0% | 0.53 | -24.6% | 4 | 83.0x | positive both windows |
| `tsmom:XLK:lookback=126` | 2.0% (0.76) | 1.2% (0.35) | 0.10 | 14.5% | 0.73 | -27.3% | 3 | 175.6x | positive both windows |
| `tsmom:VO:lookback=126` | 5.3% (1.79) | 0.9% (0.31) | 0.10 | 4.6% | 0.40 | -18.7% | 4 | 49.8x | positive both windows |
| `tsmom:SPY:lookback=252` | 4.4% (1.91) | 0.7% (0.30) | 0.08 | 8.3% | 0.61 | -27.8% | 3 | 138.3x | positive both windows |
| `tsmom:SMH:lookback=252` | 0.9% (0.26) | 1.1% (0.26) | 0.07 | 23.1% | 0.82 | -33.6% | 3 | 232.5x | positive both windows |
| `tsmom:VB:lookback=126` | 1.6% (0.51) | 0.6% (0.18) | 0.06 | 4.6% | 0.35 | -24.4% | 5 | 53.2x | positive both windows |
| `tsmom:TLT:lookback=126` | -2.4% (-1.52) | 0.4% (0.20) | 0.06 | -0.5% | -0.05 | -24.3% | 4 | -3.6x | positive holdout only |
| `tsmom:SPY:lookback=126` | 3.0% (1.39) | 0.5% (0.18) | 0.05 | 6.5% | 0.53 | -24.1% | 4 | 85.0x | positive both windows |
| `tsmom:VTI:lookback=126` | 4.9% (2.16) | 0.5% (0.17) | 0.05 | 6.0% | 0.50 | -25.3% | 4 | 77.3x | positive both windows |
| `tsmom:EFA:lookback=126` | 5.0% (1.94) | 0.4% (0.16) | 0.05 | 3.4% | 0.31 | -23.1% | 4 | 38.0x | positive both windows |
| `tsmom:DIA:lookback=252` | 3.7% (1.68) | 0.4% (0.16) | 0.05 | 6.2% | 0.50 | -20.6% | 3 | 105.3x | positive both windows |
| `tsmom:EEM:lookback=126` | 0.1% (0.03) | 0.5% (0.18) | 0.05 | 4.1% | 0.29 | -31.0% | 5 | 32.9x | positive both windows |
| `tsmom:XLK:lookback=252` | 5.1% (1.85) | 0.4% (0.15) | 0.04 | 16.4% | 0.76 | -32.8% | 2 | 302.6x | positive both windows |
| `tsmom:XLF:lookback=252` | 4.7% (1.76) | 0.0% (0.01) | 0.00 | 5.0% | 0.35 | -36.6% | 3 | 60.1x | positive both windows |
| `tsmom:XLU:lookback=126` | 3.0% (1.30) | 0.0% (0.00) | 0.00 | 4.3% | 0.33 | -25.3% | 4 | 29.7x | positive both windows |
| `tsmom:VO:lookback=252` | 2.9% (0.88) | -0.0% (-0.00) | -0.00 | 4.6% | 0.36 | -27.5% | 4 | 52.5x | negative holdout |
| `tsmom:XLE:lookback=252` | 5.7% (1.71) | -0.3% (-0.07) | -0.02 | 4.7% | 0.25 | -36.2% | 5 | 29.9x | negative holdout |
| `tsmom:IWM:lookback=126` | 2.2% (0.80) | -0.3% (-0.09) | -0.03 | 3.6% | 0.25 | -31.2% | 6 | 25.0x | negative holdout |
| `tsmom:XLI:lookback=252` | 4.5% (1.68) | -0.3% (-0.09) | -0.03 | 5.2% | 0.38 | -27.6% | 4 | 62.9x | negative holdout |
| `tsmom:XLB:lookback=252` | 1.8% (0.59) | -0.4% (-0.15) | -0.04 | 3.7% | 0.28 | -26.3% | 3 | 45.9x | negative holdout |
| `tsmom:XLY:lookback=126` | 4.3% (1.62) | -0.5% (-0.16) | -0.05 | 4.1% | 0.29 | -30.5% | 6 | 28.5x | negative holdout |
| `tsmom:EFA:lookback=252` | 3.0% (1.11) | -0.6% (-0.24) | -0.07 | 2.3% | 0.21 | -21.3% | 3 | 37.6x | negative holdout |
| `tsmom:VB:lookback=252` | 1.4% (0.41) | -0.8% (-0.23) | -0.07 | 3.3% | 0.25 | -26.9% | 4 | 48.3x | negative holdout |
| `tsmom:GLD:lookback=126` | 2.8% (0.91) | -0.5% (-0.24) | -0.08 | 7.9% | 0.55 | -24.8% | 4 | 73.0x | negative holdout |
| `tsmom:XLRE:lookback=126` | n/a | -0.8% (-0.26) | -0.08 | 1.4% | 0.11 | -23.3% | 5 | 8.0x | short history |
| `tsmom:TLT:lookback=252` | -1.7% (-1.28) | -0.6% (-0.32) | -0.09 | -1.4% | -0.15 | -22.3% | 4 | -14.5x | negative holdout |
| `tsmom:XLY:lookback=252` | 4.4% (1.61) | -1.0% (-0.34) | -0.09 | 5.7% | 0.34 | -27.3% | 4 | 63.0x | negative holdout |
| `tsmom:SLV:lookback=252` | 5.2% (0.90) | -2.7% (-0.67) | -0.20 | 9.0% | 0.31 | -59.1% | 4 | 59.8x | negative holdout |
| `tsmom:XLE:lookback=126` | 2.4% (0.71) | -3.0% (-0.62) | -0.21 | 2.5% | 0.13 | -50.3% | 7 | 11.1x | negative holdout |
| `tsmom:SLV:lookback=126` | 3.0% (0.52) | -3.0% (-0.63) | -0.21 | 7.7% | 0.28 | -48.8% | 6 | 33.3x | negative holdout |
| `tsmom:XLP:lookback=252` | 2.5% (1.50) | -1.8% (-0.98) | -0.28 | 3.5% | 0.27 | -19.7% | 4 | 33.6x | negative holdout |
| `tsmom:XLP:lookback=126` | 0.4% (0.24) | -2.3% (-1.04) | -0.31 | 1.0% | 0.10 | -20.9% | 7 | 7.2x | negative holdout |
| `tsmom:XLV:lookback=252` | 2.7% (1.21) | -2.5% (-1.13) | -0.31 | 3.3% | 0.25 | -22.8% | 5 | 27.9x | negative holdout |
| `tsmom:XLB:lookback=126` | -1.3% (-0.40) | -3.6% (-1.19) | -0.35 | 0.6% | 0.05 | -35.3% | 6 | 4.2x | negative holdout |
| `tsmom:XLRE:lookback=252` | n/a | -3.7% (-1.25) | -0.36 | -0.8% | -0.06 | -23.0% | 6 | -2.6x | short history |
| `tsmom:GLD:lookback=252` | 2.5% (0.87) | -2.5% (-1.36) | -0.40 | 6.7% | 0.44 | -28.3% | 4 | 61.8x | negative holdout |
| `tsmom:XLV:lookback=126` | -1.1% (-0.51) | -4.6% (-1.94) | -0.56 | 0.7% | 0.06 | -29.9% | 6 | 5.4x | negative holdout |
| `tsmom:XLU:lookback=252` | 4.4% (1.88) | -5.8% (-2.16) | -0.63 | -0.4% | -0.02 | -36.1% | 5 | -0.9x | negative holdout |

## dual_momentum (2)

| candidate | discovery alpha (t) | holdout alpha (t) | holdout alpha IR | holdout excess | holdout Sharpe | holdout max drawdown | trades a year | gross / cost | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `dual_momentum:SPY+EFA+TLT:lookback=252` | 5.0% (1.33) | -0.1% (-0.04) | -0.01 | 6.8% | 0.39 | -34.0% | 2 | 174.8x | negative holdout |
| `dual_momentum:SPY+EFA+TLT:lookback=126` | 7.2% (1.90) | -1.4% (-0.45) | -0.12 | 4.5% | 0.27 | -38.4% | 3 | 75.4x | negative holdout |

## Hourly last-half-hour families (50, 2024-11-11 to 2026-11-06)

| candidate | first half: bps a day (t) | second half: bps a day (t) |
| --- | --- | --- |
| `first_hour_momentum:XLP` | 0.05 (0.05) | -0.01 (-0.01) |
| `first_hour_momentum:XLRE` | -3.07 (-1.78) | -0.20 (-0.17) |
| `first_hour_momentum:GLD` | -2.28 (-3.78) | -0.51 (-0.44) |
| `last_half_hour_momentum:XLP` | 0.69 (0.56) | -0.68 (-0.55) |
| `last_half_hour_momentum:XLV` | 1.14 (0.70) | -0.75 (-0.66) |
| `first_hour_momentum:SLV` | -5.72 (-3.90) | -1.91 (-0.75) |
| `last_half_hour_momentum:XLRE` | -3.93 (-1.91) | -0.87 (-0.76) |
| `first_hour_momentum:EFA` | -1.30 (-1.17) | -0.88 (-0.95) |
| `last_half_hour_momentum:EEM` | -4.57 (-4.97) | -1.22 (-0.97) |
| `first_hour_momentum:SMH` | -6.48 (-1.96) | -3.20 (-1.00) |
| `first_hour_momentum:EEM` | -3.87 (-4.18) | -1.37 (-1.03) |
| `last_half_hour_momentum:SLV` | -6.71 (-4.82) | -2.64 (-1.04) |
| `last_half_hour_momentum:XLI` | -3.67 (-2.40) | -1.42 (-1.13) |
| `last_half_hour_momentum:GLD` | -1.77 (-3.23) | -1.51 (-1.19) |
| `first_hour_momentum:XLV` | -1.96 (-1.54) | -1.65 (-1.36) |
| `last_half_hour_momentum:XLE` | -8.24 (-4.32) | -2.22 (-1.51) |
| `first_hour_momentum:VB` | -1.82 (-1.05) | -1.69 (-1.55) |
| `last_half_hour_momentum:XLK` | -7.25 (-3.33) | -2.80 (-1.56) |
| `first_hour_momentum:XLI` | -3.90 (-2.24) | -1.89 (-1.63) |
| `first_hour_momentum:VO` | -1.60 (-0.96) | -1.58 (-1.64) |
| `first_hour_momentum:XLU` | -4.13 (-2.54) | -2.16 (-1.65) |
| `first_hour_momentum:XLK` | -3.57 (-1.44) | -3.41 (-1.68) |
| `last_half_hour_momentum:XLF` | -4.82 (-3.05) | -1.88 (-1.72) |
| `last_half_hour_momentum:VB` | -3.17 (-2.11) | -2.03 (-1.75) |
| `first_hour_momentum:QQQ` | -3.58 (-1.55) | -2.67 (-1.82) |
| `last_half_hour_momentum:QQQ` | -5.94 (-2.96) | -2.60 (-1.95) |
| `last_half_hour_momentum:XLU` | -3.15 (-1.84) | -2.53 (-1.95) |
| `last_half_hour_momentum:SMH` | -10.87 (-3.16) | -5.80 (-1.97) |
| `last_half_hour_momentum:VO` | -2.56 (-1.76) | -2.10 (-1.98) |
| `first_hour_momentum:XLF` | -3.56 (-1.77) | -1.97 (-1.98) |
| `last_half_hour_momentum:EFA` | -2.97 (-2.85) | -1.85 (-2.17) |
| `last_half_hour_momentum:XLB` | -2.22 (-1.45) | -3.12 (-2.50) |
| `first_hour_momentum:XLE` | -4.66 (-2.62) | -4.47 (-2.51) |
| `first_hour_momentum:VTI` | -1.01 (-0.57) | -2.68 (-2.57) |
| `last_half_hour_momentum:DIA` | -2.64 (-2.22) | -2.19 (-2.61) |
| `first_hour_momentum:IWM` | -2.62 (-1.51) | -3.42 (-2.78) |
| `last_half_hour_momentum:IWM` | -6.87 (-4.21) | -3.98 (-2.94) |
| `first_hour_momentum:XLB` | -2.63 (-1.96) | -3.80 (-2.97) |
| `first_hour_momentum:SPY` | -2.66 (-1.45) | -3.24 (-3.07) |
| `first_hour_momentum:DIA` | -3.14 (-2.02) | -2.64 (-3.24) |
| `last_half_hour_momentum:XLY` | -3.42 (-1.98) | -4.34 (-3.43) |
| `last_half_hour_momentum:VTI` | -3.83 (-2.47) | -3.24 (-3.44) |
| `first_hour_momentum:universe24` | -2.97 (-2.71) | -2.28 (-3.50) |
| `first_hour_momentum:XLY` | -3.22 (-1.68) | -4.41 (-3.68) |
| `first_hour_momentum:TLT` | -2.23 (-3.27) | -2.30 (-3.97) |
| `last_half_hour_momentum:SPY` | -3.75 (-2.42) | -3.67 (-4.10) |
| `last_half_hour_momentum:universe24` | -3.97 (-4.31) | -2.44 (-4.33) |
| `last_half_hour_momentum:TLT` | -2.50 (-3.43) | -2.78 (-4.66) |
| `last_half_hour_momentum:HYG` | -2.20 (-7.05) | -2.24 (-6.78) |
| `first_hour_momentum:HYG` | -2.32 (-7.83) | -2.66 (-9.90) |
