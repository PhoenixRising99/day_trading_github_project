# Day-Trading Research / Alpaca Paper Execution

This repository runs a **paper-only** implementation of the frozen V9/V10/V11
morning VWAP-hold strategy. Alpaca paper trading is the canonical forward-test
execution record.

## Safety boundary

- Alpaca is always initialized with `paper=True`.
- Live trading is intentionally unsupported.
- Entries and exits require explicit paper-order enable switches.
- Maximum research position value is derived from the $120 research account and
  20% position cap (currently $24).
- Maximum one strategy trade per day.
- Maximum one tracked open strategy position.
- A broker-side fractional DAY stop order protects each filled position.
- Software logic still handles take-profit, VWAP/EMA exits, and end-of-day exits.
- Broker state is reconciled on every trading-session cycle.

Required GitHub secrets:

```text
ALPACA_API_KEY
ALPACA_SECRET_KEY
ALPACA_PAPER_ENTRY_SUBMISSION_ENABLED=true
ALPACA_PAPER_EXIT_SUBMISSION_ENABLED=true
```

The legacy `ALPACA_PAPER_ORDER_SUBMISSION_ENABLED` switch remains supported as
a fallback.

## Frozen strategy

Current research configuration:

- Watchlist: `SPY, QQQ, AAPL, MSFT, NVDA, AMZN, GOOGL, META`
- Candle interval: 5 minutes
- Entry window: 10:15-10:59 AM America/New_York
- Minimum setup score: 12/14
- Long-only VWAP-hold continuation
- One trade per day
- No shorting, options, or margin strategy logic
- Entry strategy parameters remain frozen while the forward sample accumulates

See `daytrading/config.py` and `daytrading/strategy.py` for the exact rules.

## Active execution architecture

### `.github/workflows/alpaca_paper_trading_session.yml`

The primary paper-execution workflow. Scheduling is externally dispatched by
cron-job.org with `session_mode=true` because GitHub's native scheduled starts
were too inconsistent for the entry window.

Each cycle:

1. Pulls current repository state.
2. Runs an entry check only during 10:15-10:59 AM ET.
3. Reconciles the tracked position against Alpaca.
4. Repairs broker-side stop protection if needed.
5. Evaluates exits using completed 5-minute candles only.
6. Uses Alpaca's market clock for the five-minutes-before-close flattening rule.
7. Commits broker state and the completed-trade ledger when either changes.

State persistence failures are fatal; the workflow no longer silently ignores a
failed push.

### `alpaca_paper_strategy_entry_job.py`

- Verifies candidate and SPY context timestamps are aligned.
- Retries temporarily stale yfinance snapshots.
- Checks repository state **and Alpaca order history** to enforce one trade/day.
- Waits for the entry market order result.
- Re-centers the strategy's stop/target distances around the actual fill.
- Submits a fractional Alpaca DAY stop order after the fill.
- Persists the filled position and protection metadata.

### `alpaca_paper_position_monitor.py`

- Reconciles local state against Alpaca every cycle.
- Detects a filled broker-side protective stop.
- Repairs missing/expired protective stops.
- Rejects stale completed-bar data for software exits.
- Cancels the protective stop before a discretionary market exit to avoid an
  accidental oversell.
- Re-protects residual shares after a partial exit.
- Detects stale overnight strategy positions.
- Uses Alpaca's exchange clock so early-close days do not rely on a hard-coded
  4:00 PM close.
- Will only auto-recover an orphaned broker position when a very recent
  `paper-entry-...` order proves that this strategy created it; unrelated
  manual paper positions are left alone.

## Canonical forward-test records

Current broker state:

```text
data/logs/broker/alpaca_open_position_state.json
```

Completed Alpaca paper trades:

```text
data/logs/broker/alpaca_paper_exit_log.csv
```

The exit log uses actual Alpaca fills and is the canonical completed-trade
ledger.

The old CSV simulator remains available only as a **manual diagnostic**:

```text
.github/workflows/paper_scan.yml
data/logs/paper_trading/paper_trade_journal.csv
```

It is intentionally no longer scheduled because an independently fetched
yfinance simulation can diverge from actual Alpaca execution and should not be
treated as a second source of truth.

## Market data

Strategy calculations use yfinance. It is free research data and can be stale,
incomplete, or rate-limited. Entry execution therefore requires exact
candidate/SPY timestamp alignment. Exit logic ignores stale completed-bar data;
the resting Alpaca stop remains the downside fail-safe while software data is
unavailable.

## Continuous checks

`.github/workflows/ci.yml` runs on main, pull requests, and ChatGPT hardening
branches. It:

- installs the pinned dependency ranges,
- compiles the Python sources,
- imports the critical execution modules,
- checks key frozen strategy invariants.

## Historical files

Files such as `PATCH_NOTES.md`, `COMPLETED_BAR_PATCH_NOTES.md`,
`APPLY_PATCH.md`, and `ALPACA_FRACTIONAL_BRACKET_FIX_NOTES.md` document
earlier migration stages. They are historical context; this README describes
the current architecture.

## Current research goal

Continue paper execution until the clean Alpaca sample is large enough for
meaningful analysis. Do not tune the strategy from a handful of trades. Review
at roughly 25-30 completed trades, with a stronger decision point around 50.
