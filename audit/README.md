# Strategy audit (read-only)

This directory is deliberately separate from the Alpaca trading workflow. Running
the audit **does not submit orders, cancel orders, or edit trading parameters**.

## Data and reproducibility

- Completed trades: data/logs/broker/alpaca_paper_exit_log.csv (actual fills).
- Replay market data: Alpaca's historical **SIP** 5-minute candles (not IEX-only).
- Original live strategy data: yfinance. Historical SIP bars can differ, so the
  replay's original-rule **fidelity must be reported before** judging alternatives.
- Historical warmup: 14 calendar days of regular-hours bars for EMA indicators.
- Historical entries: fixed to the recorded Alpaca entry fill price and timestamp.
- Effective stop and target: preserve original distances around actual entry fill.
- Entry bar excluded from exit simulation to avoid applying pre-fill price extremes.
- Original model: protective stop, target, two consecutive closes below VWAP,
  one EMA9<EMA20 failure, then end-of-day.
- Exploratory alternatives: **three** consecutive closes below VWAP; or **two**
  consecutive EMA9<EMA20 failures. The other rules stay unchanged.
- Next-bar-open modeled exits after a completed signal, with a **hypothetical
  5 bps exit cost** (not a measured execution cost). Stops assume conservative
  gap-through pricing and stop-first priority if both stop/target are touched.
- The one-trade-per-day entry list is held fixed; this does NOT test alternative
  entry signals, missed entries, dynamic sizing, commissions, or broker order
  timing. It is *not* a full backtest of a new strategy.

## Run

Without API keys, for the actual-fill ledger assessment:

    python -m audit.replay --no-market --output audit_outputs

With ALPACA_API_KEY / ALPACA_SECRET_KEY provided:

    python -m audit.replay --output audit_outputs

The program writes summary.json and, when historical data are available,
replay_trades.json. It also prints a machine-readable AUDIT_SUMMARY_JSON line.
There is a separate manual or branch-triggered GitHub Actions workflow,
Strategy Audit (Read Only). It has no trade submission environment variables,
and has contents:read permission.

## Interpretation protocol

1. **Never compare alternative P/L directly to actual trade P/L.** Compare
   alternatives against the reconstructed baseline on **identical** bars and
   with identical execution-cost assumptions.
2. Compare reconstructed baseline exit **reason** and timing with live Alpaca
   trades. Divergences can reflect feed differences, missing monitoring periods,
   market movement, and execution latency. If fidelity is poor, the variant
   results are only exploratory and should not guide changes.
3. Inspect each changed trade for outliers and event-specific market conditions.
4. Do not optimize by choosing the largest in-sample P/L. Conduct separate
   walk-forward / out-of-sample tests with more market sessions and a
   non-overlapping validation window before recommending a parameter change.
5. Run an independent scanner-entry audit before proposing score filters.
   Historic accepted trades alone cannot tell whether an alternate score
   threshold would have replaced a trade with a different candidate.
6. Keep existing paper strategy and broker protection unchanged through audit.

## Known limitations

The default consolidated SIP bar history is an excellent diagnostic but not
a point-in-time snapshot of the original yfinance feed. Intrabar stop/target
ordering, spread, slippage, bid/ask depth, and schedule interruptions cannot be
recovered from 5-minute candles alone. Stop fills in the replay are modeled,
not actual Alpaca broker stop fills. Report missing/incomplete days as errors
rather than inventing data.

## New forward diagnostics and checks

Each regular entry evaluation now produces a single durable, credential-free
`entry` event and each position-monitor cycle produces a `monitor` event
at `data/logs/diagnostics/YYYY-MM-DD.jsonl`. Candidate records preserve
their score, passed/failed conditions, data timestamp and freshness status.
No-signal days are classified with:

    python -m audit.entry_coverage --output audit_outputs/entry_coverage.json

A day is counted as a *complete observed no-signal day* only when all expected
five-minute decision slots are represented, SPY context aligns, and candidates
are not missing or stale. Recorded qualified but unfilled signals are flagged
for review, not labeled automatically as missed trades. Before the deployment
of durable logging, coverage is unknown.

To inspect actual paper broker positions and orders **without modifying them**,
run the `Paper Broker Reconciliation` GitHub workflow manually, or read its
after-market artifacts. The job uses read endpoints and never sets submission
enable switches. It checks the latest tracked protective stop against its
actual Alpaca order status and flags unexpected resting strategy orders.

Independent GitHub cron watchdog checks for a stale monitor heartbeat while
the repository says a strategy position is open; it reports failures through
GitHub Actions and, optionally, the configured Discord webhook. It is not
guaranteed real-time protection if GitHub scheduling itself is delayed.

Historical trade score coverage is reported explicitly by `audit.replay`
rather than retroactively imputing values. The SIP replay additionally
reports reason/timing mismatches, paired wins/losses on variant outcomes, and
evaluated-candle-to-fill differences without labeling those differences
pure slippage. **These diagnostics do not authorize strategy tuning.**
