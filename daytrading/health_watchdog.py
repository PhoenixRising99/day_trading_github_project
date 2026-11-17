"""Read-only GitHub scheduled watchdog for the Alpaca PAPER position monitor.

No order submission, no direct Alpaca API calls. The watchdog is best-effort:
GitHub's native cron can be delayed, so a successful run is not a guaranteed
five-minute heartbeat. An optional Discord webhook provides alerts.
"""
import json
from datetime import datetime, timezone

from daytrading.diagnostics import _alert, watchdog_status


def main() -> int:
    status = watchdog_status(now=datetime.now(timezone.utc))
    print("PAPER_WATCHDOG=" + json.dumps(status, sort_keys=True), flush=True)
    if status["status"] == "stale":
        _alert("Paper monitor heartbeat is stale for "
               f"{status.get('symbol', '?')}; "
               f"minutes since last recorded monitor: {status.get('minutes_since_monitor')}. "
               "GitHub scheduling can be delayed; verify Actions and broker protection.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
