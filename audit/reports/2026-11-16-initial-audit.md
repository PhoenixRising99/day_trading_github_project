# Initial strategy audit — 30 Alpaca paper trades
**Conducted:** November 16, 2026 (US Pacific time)
**Data window:** July 30–November 11, 2026
**Status:** Research-only; no strategy changes recommended.

## Ground-truth actual-fill ledger

All 30 completed broker trades were recovered from
`data/logs/broker/alpaca_paper_exit_log.csv`.

| Metric | Actual broker fills |
|---|---:|
| Completed | 30 |
| Winners / losers | 13 / 17 |
| Win rate | 43.33% |
| Cumulative realized P/L | +$0.229967 |
| Mean P/L per trade | +$0.007666 |
| Gross winners | +$1.824593 |
| Gross losers | -$1.594626 |
| Profit factor | 1.1442 |

Largest win: November 11 NVDA, +$0.330414. Removing that trade alone
makes the remaining sample negative (-$0.100447). This is a robustness
check, **not** a reason to remove large winners from a momentum strategy.

Exit reason | Trades | Total realized P/L
:--|--:|--:
End of day | 8 | +$1.253606
Two-bar VWAP failure | 13 | -$1.093572
EMA trend failure | 7 | +$0.003125
Stop loss | 1 | -$0.263606
Take profit | 1 | +$0.330414

The VWAP result is a conditional outcome *after* a losing-momentum event, not
evidence that removing the VWAP exit improves performance. All eight trades
that survived until end-of-day were winners; that is likewise subject to
survivorship bias.

## Historical SIP-bar matched counterfactuals

Read-only workflow [run 44005430206](https://github.com/PhoenixRising99/day_trading_github_project/actions/runs/44005430206)
replayed **all 30** trades with **zero missing historical sessions**, using
consolidated Alpaca SIP 5-minute candles and a 14-calendar-day warmup.
Entry prices and times were fixed to actual broker entries. Stop/target
distances were preserved; normal exits were modeled at the next 5-minute
bar open with assumed 5-bp friction. The variants used identical market
data, initial entries, and cost assumptions.

| Model | Hypothetical total P/L | Delta vs. reconstructed baseline |
|---|---:|---:|
| Existing two-bar VWAP + one-bar EMA | -$0.467515 | — |
| Three-bar VWAP + one-bar EMA | -$0.453541 | **+$0.013974** |
| Two-bar VWAP + two-bar EMA | -$0.510102 | **-$0.042587** |

The three-bar VWAP variant changed 8 modeled outcomes: 3 improved, 5
worsened. The two-bar EMA variant changed 14 outcomes: 6 improved, 8
worsened. Neither demonstrated a robust improvement.

**Critical fidelity caveat:** The reconstructed baseline matched the
historical live exit *reason* on 21/30 trades, but matched both reason and
approximate timing (within 15 minutes) on only **18/30 (60%)**. It
modeled **-$0.467515**, whereas the actual executed strategy realized
**+$0.229967**. These are not directly comparable P/L series. The gap is
consistent with changing market-data feeds, delayed execution, monitoring
interruptions, and the assumptions of five-minute OHLC replay. Consequently,
the modeled variant deltas are exploratory, **not valid evidence to deploy
a new exit rule**.

Notable mismatches include:
- August 6 MSFT: actual end-of-day exit, reconstructed EMA exit about
  226 minutes earlier; the modeled result differs materially.
- October 15 MSFT, October 27 QQQ, and September 21 AAPL: actual end-of-day
  exits reconstructed as earlier EMA exits.
- November 6 META: notable discrepancy between signal candle price and
  actual later fill, demonstrating why OHLC next-open modeling is not a
  substitute for order-level execution analysis.

## Process audit and next actions

1. **Keep current V9/V10/V11 entry and exit parameters frozen.**
   There is no statistically or operationally persuasive evidence yet for a
   3-bar VWAP or 2-bar EMA policy. Both were assessed in sample only.
2. **Verify the newly deployed session-handoff fix in a real paper session.**
   Prior runs had 100+ minute intraday monitoring gaps while positions were
   open; tests passed but a real handoff is not yet validated here.
3. **Improve audit trail completeness.** Record entry-time full setup
   scores, indicator inputs, timestamp alignment, and a reason for each scan
   cycle, including no-trade and stale-data cases. Currently the exit ledger
   does not retain setup score, and the independent paper journal is
   manual-only; no reliable score-12 vs score-13 vs score-14 comparison can
   be inferred from these 30 completed trades.
4. **Capture fill friction separately from the last evaluated candle.**
   Signal detection-to-order-submit latency, bid/ask quotes, and fill prices
   should be reported separately; differences from a 5-minute candle close
   should not automatically be labeled execution slippage.
5. **Run a longer, non-overlapping walk-forward study** covering market
   sessions, including skipped/no-trade days, before comparing a small
   prespecified set of entry and exit improvements. Preserve a holdout period,
   model spread/fees/delays, and do not select variants by these same 30
   in-sample trades.

**Decision:** Continue *paper* only with no strategy-parameter changes.
The audit code and its risk controls are read-only. This audit cannot
establish the strategy's true out-of-sample expectancy, and the 30-trade
sample is not sufficient to support live-capital deployment.
