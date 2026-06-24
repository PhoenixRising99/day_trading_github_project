# Alpaca fractional/bracket fix + position-monitor patch

## What was broken

`daytrading/broker/alpaca_paper.py` submitted entries as an Alpaca
`OrderClass.BRACKET` order (market entry + attached take-profit + stop-loss)
using a fractional `qty`. Alpaca does not support fractional share
quantities on bracket/OCO orders -- fractional qty is only accepted on
simple market/limit/stop orders with `time_in_force=Day`.

This research account's position sizing (`calculate_position_size` in
`daytrading/strategy.py`) is fractional by design: the $24 max position
value (20% of the $120 account) is smaller than one share of any watchlist
symbol (SPY/AAPL/MSFT), so `qty` is always a fraction of a share. That means
every entry attempt was submitting a fractional-qty bracket order, which
Alpaca rejects every time. This is almost certainly why "the Alpaca part
isn't working."

## What changed

1. **`daytrading/broker/alpaca_paper.py`**
   - Removed `submit_market_bracket_buy` (the broken bracket method).
   - Added `submit_market_buy` (simple market buy, fractional-safe) and
     `submit_market_sell` (simple market sell, used to close a position).
   - Added a global order-submission kill switch:
     `ALPACA_PAPER_ORDER_SUBMISSION_ENABLED`. Set this GitHub
     Secret/Variable to `"false"` to immediately stop all entry AND exit
     order submission without touching API keys or paper mode. Defaults to
     enabled if unset, so nothing changes unless you set it.

2. **`daytrading/position_state.py`** (new)
   - Small shared module that reads/writes
     `data/logs/broker/alpaca_open_position_state.json`. This is how the
     entry job and the (new) exit monitor share state across separate
     scheduled runs, since each GitHub Actions run gets a fresh checkout.

3. **`alpaca_paper_strategy_entry_job.py`** (rewritten)
   - No longer waits/loops inside one job for the entry window to open and
     close. Instead it does ONE quick check per invocation, meant to be
     called every ~5 minutes by a cron schedule that already spans the
     window (see workflow changes below). It still double-checks the real
     ET wall-clock time itself, so it's safe even if a run lands a few
     minutes outside the intended UTC range.
   - Submits a simple market buy instead of a bracket order.
   - Saves entry details (symbol, qty, stop/target preview, signal date) to
     the open-position-state file for the exit monitor to use.
   - Blocks a second entry for the rest of the day once today's one trade
     has happened (whether still open or already closed), enforcing
     `max_trades_per_day = 1` for the full day, not just while a position is
     open.

4. **`alpaca_paper_position_monitor.py`** (new)
   - This did not exist before and is required now that entries are simple
     orders: nothing on Alpaca's side is watching the stop/target
     automatically anymore, so this job does it. Every ~5 minutes during
     market hours it checks the one tracked open position (if any) against
     the same exit rules as the V9/V10/V11 strategy: stop-loss, take-profit,
     two-bar VWAP failure, EMA trend failure, end-of-day flatten at/after
     15:55 ET. If triggered, it submits a simple market sell and logs the
     result to `data/logs/broker/alpaca_paper_exit_log.csv`.
   - Reconciles itself against Alpaca's actual open positions before acting,
     and uses a deterministic `client_order_id` per symbol/day so a
     retried run cannot submit a duplicate exit order.

5. **`.github/workflows/alpaca_paper_strategy_entry.yml`** (rewritten)
   - Cron changed from a single `0 12 * * 1-5` fire (followed by an
     in-process wait/loop) to `*/5 14-15 * * 1-5` -- a 5-minute cadence
     across a UTC range that covers the real 10:15-10:59 AM ET window in
     both daylight time and standard time. This removes the DST-driven
     timeout risk the old design had: in standard time, the old job's wait
     time plus scan loop could exceed its own 235-minute timeout.
   - `timeout-minutes` dropped from 235 to 15, since there's no wait loop.
   - Added a step to commit the position-state file back to the repo.

6. **`.github/workflows/alpaca_paper_position_monitor.yml`** (new)
   - Same frequent-cron pattern, covering 9:30 AM-4:00 PM ET market hours
     across both DST states, calling the new monitor script.

## What this does NOT do

- Does not enable live trading.
- Does not connect to SoFi.
- Does not submit anything outside Alpaca paper trading.
- Does not change the frozen V9/V10/V11 strategy rules, position sizing, or
  research-account caps ($120 account / $24 max position / 1 trade per day).
- Does not touch the separate custom-CSV paper-trading system
  (`paper_scan.yml` / `daytrading/paper.py`). That remains a distinct,
  unreconciled system, as already noted in the project handoff.

## Suggested cleanup (not included in this patch)

- `daytrading/alpaca_paper.py` (at the package root, NOT inside
  `daytrading/broker/`) is an old, unused, read-only version of the broker
  adapter from an earlier patch stage. Nothing imports it -- only
  `daytrading/broker/alpaca_paper.py` is live. Safe to delete; left alone
  here to keep this patch focused.

## Recommended next step

Let the entry + monitor workflows run for several mornings/days and review:
- `data/logs/broker/alpaca_paper_strategy_entry_*.json` (entry attempts)
- `data/logs/broker/alpaca_paper_position_monitor_*.json` (exit checks)
- `data/logs/broker/alpaca_open_position_state.json` (current tracked state)
- `data/logs/broker/alpaca_paper_exit_log.csv` (closed-trade history)

to confirm entries now actually fill and exits actually trigger, before
treating Alpaca paper execution as a faithful stand-in for the strategy.
