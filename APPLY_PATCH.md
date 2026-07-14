# Alpaca paper-trading reliability patch

This patch was prepared after reviewing the current repository and the July 14 Alpaca paper round trip.

## Files replaced

- `.github/workflows/alpaca_paper_trading_session.yml`
- `daytrading/broker/alpaca_paper.py`
- `alpaca_paper_strategy_entry_job.py`
- `alpaca_paper_position_monitor.py`

## Main changes

1. **cron-job.org becomes the scheduler**
   - Removes GitHub's native cron schedule from the consolidated Alpaca session workflow.
   - `workflow_dispatch` remains available for cron-job.org and manual runs.
   - A `session_mode=true` dispatch runs a bounded session loop.
   - Session runtime is capped at 220 minutes, below the reported four-hour limit.

2. **Actual fill reconciliation**
   - Submitted orders are polled until a terminal state or timeout.
   - Entry state is not marked open unless an actual filled quantity is confirmed.
   - Exit state is not marked closed while Alpaca still reports a remaining position.
   - Actual fill quantity, average fill price, fill timestamp, order status, and realized P/L are stored.

3. **Completed-bar exit evaluation**
   - The exit monitor ignores the currently forming five-minute candle.
   - It evaluates only completed candles from the current New York trading date.

4. **Separate safety switches**
   - `ALPACA_PAPER_ENTRY_SUBMISSION_ENABLED`
   - `ALPACA_PAPER_EXIT_SUBMISSION_ENABLED`
   - Both fall back to the existing `ALPACA_PAPER_ORDER_SUBMISSION_ENABLED`.
   - If no switch is configured, order submission fails closed.
   - This allows new entries to be disabled while protective exits remain enabled.

5. **Idempotency and partial-fill handling**
   - Deterministic client order IDs remain in use.
   - Exit-log rows are deduplicated by client order ID.
   - Partial exits keep the position state open with the remaining broker quantity.

## Recommended GitHub secrets

Keep:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_PAPER=true`

Add:

- `ALPACA_PAPER_ENTRY_SUBMISSION_ENABLED=true`
- `ALPACA_PAPER_EXIT_SUBMISSION_ENABLED=true`

The existing global secret can remain for backward compatibility:

- `ALPACA_PAPER_ORDER_SUBMISSION_ENABLED=true`

To stop new entries while allowing open positions to exit:

- Set `ALPACA_PAPER_ENTRY_SUBMISSION_ENABLED=false`
- Keep `ALPACA_PAPER_EXIT_SUBMISSION_ENABLED=true`

## cron-job.org dispatch

Continue dispatching the workflow:

`Alpaca Paper Trading Session`

Pass these workflow inputs:

```json
{
  "ref": "main",
  "inputs": {
    "session_mode": "true",
    "allow_outside_entry_window": "false",
    "confirm_entry": "",
    "confirm_exit": ""
  }
}
```

Suggested weekday dispatches in America/New_York:

- **08:00 ET** — starts the primary session and waits until the entry window.
- **11:40 ET** — handoff/backup monitor if the first run approaches its limit.
- **15:20 ET** — final backup monitor for end-of-day coverage.

The workflow concurrency group prevents simultaneous sessions. A dispatch made
while another session is running will queue and take over afterward.

## Watchlist

The expanded watchlist is left unchanged:

- SPY
- QQQ
- AAPL
- MSFT
- NVDA
- AMZN
- GOOGL
- META

This is reasonable for increasing forward-paper sample accumulation, but results
should be reported separately for:

1. the original validated universe (`SPY`, `AAPL`, `MSFT`), and
2. the expanded names.

Do not use the expanded-universe results as evidence that the original backtest
generalized until enough completed Alpaca paper trades exist.
