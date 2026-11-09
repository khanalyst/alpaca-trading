# Opening-range breakout on stocks in play

Diagnostic study. Its question: is the intraday program's failure a property of intraday trading itself, or of what that program trades? Every one of its 82 arms trades 24 broad ETFs. Day traders who make money trade *stocks in play*: names with news and unusual volume that morning.

## Rules

As summarised from Zarattini, Barbon and Aziz (2024), "A Profitable Day Trading Strategy for the U.S. Equity Market", which reports a Sharpe ratio of 2.81 over 2016 to 2023 (QuantConnect's replication: 2.4):

- **eligible:** open above $5, 14-day average volume of at least 1,000,000 shares, 14-day ATR above $0.50;
- **in play:** the first five-minute volume divided by its 14-day average for those same five minutes; at least 1.0, and the top 20;
- **direction:** that of the first five-minute candle (a doji is skipped);
- **entry:** a stop order at the candle's high (long) or low (short);
- **stop:** 10% of the daily ATR (the paper's rule), or the other side of the first candle (the classic breakout stop);
- **exit:** flat at the close;
- **sizing:** 1% of equity at risk per trade, at most 4x leverage across the 20 names.

The same code runs three universes: stocks in play, 20 eligible stocks drawn at random each day, and the repository's 24 ETFs.

## Result

From [results/report.md](results/report.md): 2,269 symbols, 46 test sessions (3 September to 6 November 2026), five-minute bars.

| universe, stop at the other side of the first candle, no cost | trades | mean R | daily t |
| --- | --- | --- | --- |
| stocks in play | 743 | -0.06 | -0.79 |
| 20 random liquid stocks | 764 | -0.05 | -0.04 |
| the 24 ETFs | 1,006 | -0.03 | +0.09 |

Every variant is negative once realistic costs are charged.

**Stocks in play did not beat random stocks or the ETFs in this window.** If the paper's Sharpe ratio of 2.8 held today, 46 sessions would show a t of about +1.2; the observed -0.8 is about two standard errors below that. That is mild evidence against a strong effect now. It is not a refutation, because the test has these limits:

- 46 sessions;
- five-minute bars, which cannot resolve the paper's 10%-of-ATR stop (85 to 95% of trades stop out, depending on the fill-order assumption);
- no news filter;
- today's listing, so stocks delisted in the window are missing.

## Decide it properly

Use Alpaca's SIP one-minute bars, which go back to 2016 and are free for history older than 15 minutes. Run the same rules over 2016 to 2026, with a walk-forward split and the same multiple-testing discipline as `edgefinder`.

## Run

```sh
python -m studies.stocks_in_play.study --cache runtime/stocks-in-play --out studies/stocks_in_play/results
```

About 5 minutes the first time (it fetches about 2,300 symbols), seconds after that from the cache.
