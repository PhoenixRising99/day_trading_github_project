from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Any
from urllib import request

import pandas as pd
from zoneinfo import ZoneInfo

from daytrading.config import (
    CONFIG,
    ENTRY_END_HOUR,
    ENTRY_END_MINUTE,
    ENTRY_START_HOUR,
    ENTRY_START_MINUTE,
    STRATEGY_PARAMS,
    WATCHLIST,
)
from daytrading.paper import (
    append_active_paper_signals_to_journal,
    initialize_paper_journal,
    paper_scan,
    paper_trading_status_report,
)
from daytrading.strategy import is_entry_window_now


def project_root() -> Path:
    return Path(__file__).resolve().parent


def logs_dir() -> Path:
    return project_root() / "data" / "logs"


def scans_dir() -> Path:
    p = logs_dir() / "scans"
    p.mkdir(parents=True, exist_ok=True)
    return p


def journal_path() -> Path:
    p = logs_dir() / "paper_trading" / "paper_trade_journal.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _json_default(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def market_tz() -> ZoneInfo:
    return ZoneInfo(CONFIG.timezone)


def today_entry_start_end() -> tuple[datetime, datetime]:
    tz = market_tz()
    now = datetime.now(tz)
    start = datetime.combine(
        now.date(),
        dtime(ENTRY_START_HOUR, ENTRY_START_MINUTE),
        tzinfo=tz,
    )
    end = datetime.combine(
        now.date(),
        dtime(ENTRY_END_HOUR, ENTRY_END_MINUTE),
        tzinfo=tz,
    )
    return start, end


def seconds_until(target: datetime) -> float:
    return max(0.0, (target - datetime.now(target.tzinfo)).total_seconds())


def send_discord_alert(active_signals: pd.DataFrame, journal_rows_added: int) -> None:
    """Send a Discord webhook alert only when active signals exist."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url or active_signals.empty:
        return

    lines = [
        "**Paper trade signal detected**",
        f"Rows added to journal: `{journal_rows_added}`",
        "",
    ]

    for _, row in active_signals.iterrows():
        lines.extend(
            [
                f"**{row['symbol']}**",
                f"Data timestamp: `{row.get('data_timestamp')}`",
                f"Setup score: `{row.get('setup_score')}/{row.get('setup_max_score')}`",
                f"Entry preview: `{row.get('entry_preview')}`",
                f"Stop: `{row.get('stop_loss_preview')}`",
                f"Target: `{row.get('take_profit_preview')}`",
                f"Shares preview: `{row.get('shares_preview')}`",
                f"Risk preview: `${row.get('risk_dollars_preview')}`",
                "",
            ]
        )

    payload = {"content": "\n".join(lines)[:1900]}
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=15) as resp:  # noqa: S310 - user-supplied webhook URL.
            if resp.status >= 300:
                print(f"Discord webhook returned status {resp.status}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to send Discord alert: {exc}", file=sys.stderr)


def write_github_step_summary(scan: pd.DataFrame, status: pd.DataFrame, output_scan_path: Path) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    active_count = int(scan["signal"].sum()) if "signal" in scan else 0
    raw_count = int(scan["raw_signal_before_time_filter"].sum()) if "raw_signal_before_time_filter" in scan else 0

    lines = [
        "# Paper Observation Scan",
        "",
        f"Active signals: **{active_count}**",
        f"Raw signals before time filter: **{raw_count}**",
        f"Output CSV: `{output_scan_path}`",
        "",
        "## Latest scan",
        "",
        scan.to_markdown(index=False),
        "",
        "## Paper journal status",
        "",
        status.to_markdown(index=False),
    ]

    Path(summary_file).write_text("\n".join(lines), encoding="utf-8")


def run_one_scan(period: str, notes: str) -> tuple[pd.DataFrame, pd.DataFrame, Path, int]:
    """Run one scan, save outputs, append active signals to the paper journal."""
    initialize_paper_journal(journal_path(), overwrite=False)

    scan = paper_scan(WATCHLIST, CONFIG, period=period, strategy_params=STRATEGY_PARAMS)
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    output_scan_path = scans_dir() / f"paper_signal_preview_{timestamp_tag}.csv"
    scan.to_csv(output_scan_path, index=False)

    active = scan[scan["signal"] == True].copy()  # noqa: E712
    rows_added = append_active_paper_signals_to_journal(active, journal_path(), notes=notes)

    status = paper_trading_status_report(journal_path())
    status_path = scans_dir() / f"paper_journal_status_{timestamp_tag}.csv"
    status.to_csv(status_path, index=False)

    print("Paper observation scan completed.")
    print(f"Current ET time: {pd.Timestamp.now(tz=CONFIG.timezone)}")
    print(f"Watchlist: {WATCHLIST}")
    print(f"Scan output: {output_scan_path}")
    print(f"Journal path: {journal_path()}")
    print(f"Active signals: {len(active)}")
    print(f"Rows added to journal: {rows_added}")
    print("\nLatest scan:")
    print(scan.to_string(index=False))
    print("\nPaper journal status:")
    print(status.to_string(index=False))

    if not active.empty:
        send_discord_alert(active, rows_added)

    write_github_step_summary(scan, status, output_scan_path)

    return scan, status, output_scan_path, rows_added


def run_waiting_scan_loop(period: str, notes: str, scan_every_seconds: int) -> tuple[int, int, str]:
    """
    Wait until the configured entry window, scan repeatedly during the window,
    then exit.

    This is more reliable than asking GitHub Actions to start every 5 minutes
    exactly inside a narrow trading window.
    """
    tz = market_tz()
    start, end = today_entry_start_end()
    now = datetime.now(tz)

    print(f"Workflow started at: {now:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Configured entry window: {start:%Y-%m-%d %H:%M:%S %Z} to {end:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Scan interval: {scan_every_seconds} seconds")

    if now > end:
        msg = "Started after the entry window closed. Exiting without scan."
        print(msg)
        return 0, 0, msg

    if now < start:
        wait_seconds = seconds_until(start)
        print(f"Waiting {wait_seconds:.0f} seconds until entry window opens.")
        time.sleep(wait_seconds)

    scans_completed = 0
    total_rows_added = 0

    while datetime.now(tz) <= end:
        scan, status, output_scan_path, rows_added = run_one_scan(period=period, notes=notes)
        scans_completed += 1
        total_rows_added += rows_added

        now = datetime.now(tz)
        next_run = now + timedelta(seconds=scan_every_seconds)

        if next_run > end:
            print("Next scan would occur after the entry window. Stopping.")
            break

        sleep_seconds = seconds_until(next_run)
        print(f"Sleeping {sleep_seconds:.0f} seconds until next scan.")
        time.sleep(sleep_seconds)

    msg = f"Finished entry-window scan loop. Scans completed: {scans_completed}. Rows added: {total_rows_added}."
    print(msg)
    return scans_completed, total_rows_added, msg


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run cloud-safe paper-observation scans. No brokerage connection. No live trading."
    )
    parser.add_argument("--period", default=CONFIG.period, help="yfinance period, e.g. 5d")
    parser.add_argument("--notes", default="github-actions paper observation", help="Journal note")
    parser.add_argument(
        "--require-current-entry-window",
        action="store_true",
        help="Skip scan if the GitHub job itself is not currently inside the entry window.",
    )
    parser.add_argument(
        "--wait-for-entry-window",
        action="store_true",
        help="Wait until the configured entry window, then scan repeatedly until the window closes.",
    )
    parser.add_argument(
        "--scan-every-seconds",
        type=int,
        default=300,
        help="Scan interval for --wait-for-entry-window mode. Default: 300 seconds.",
    )
    args = parser.parse_args()

    if args.wait_for_entry_window:
        scans_completed, rows_added, _ = run_waiting_scan_loop(
            period=args.period,
            notes=args.notes,
            scan_every_seconds=args.scan_every_seconds,
        )
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as fh:
                fh.write(f"scans_completed={scans_completed}\n")
                fh.write(f"rows_added={rows_added}\n")
        return 0

    if args.require_current_entry_window and not is_entry_window_now(CONFIG.timezone):
        print("Current time is outside the configured entry window. Exiting without scan.")
        return 0

    scan, status, output_scan_path, rows_added = run_one_scan(period=args.period, notes=args.notes)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        active = int(scan["signal"].sum()) if "signal" in scan else 0
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"active_signals={active}\n")
            fh.write(f"rows_added={rows_added}\n")
            fh.write(f"scan_path={output_scan_path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
