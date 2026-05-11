from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta
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
    update_open_paper_trades,
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


def today_market_close() -> datetime:
    tz = market_tz()
    now = datetime.now(tz)
    return datetime.combine(now.date(), dtime(16, 0), tzinfo=tz)


def seconds_until(target: datetime) -> float:
    return max(0.0, (target - datetime.now(target.tzinfo)).total_seconds())


def send_discord_message(message: str) -> None:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return

    payload = {"content": message[:1900]}
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


def send_entry_alert(active_signals: pd.DataFrame, journal_rows_added: int) -> None:
    if active_signals.empty or journal_rows_added <= 0:
        return

    lines = [
        "**Paper trade entry logged**",
        f"Rows added to journal: `{journal_rows_added}`",
        "",
    ]

    for _, row in active_signals.head(journal_rows_added).iterrows():
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

    send_discord_message("\n".join(lines))


def send_exit_alert(exit_updates: pd.DataFrame) -> None:
    if exit_updates.empty:
        return

    lines = ["**Paper trade exit recorded**", ""]

    for _, row in exit_updates.iterrows():
        lines.extend(
            [
                f"**{row['symbol']}**",
                f"Entry: `{row.get('paper_entry_price')}` at `{row.get('paper_entry_time')}`",
                f"Exit: `{row.get('paper_exit_price')}` at `{row.get('paper_exit_time')}`",
                f"Reason: `{row.get('paper_exit_reason')}`",
                f"P/L: `${row.get('paper_pnl')}`",
                "",
            ]
        )

    send_discord_message("\n".join(lines))


def write_github_step_summary(
    scan: pd.DataFrame | None,
    status: pd.DataFrame,
    output_scan_path: Path | None,
    exit_updates: pd.DataFrame | None = None,
) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return

    lines = ["# Paper Observation Scanner", ""]

    if scan is not None:
        active_count = int(scan["signal"].sum()) if "signal" in scan else 0
        raw_count = (
            int(scan["raw_signal_before_time_filter"].sum())
            if "raw_signal_before_time_filter" in scan
            else 0
        )
        lines.extend(
            [
                f"Active entry signals: **{active_count}**",
                f"Raw signals before time filter: **{raw_count}**",
                f"Output CSV: `{output_scan_path}`",
                "",
                "## Latest entry scan",
                "",
                scan.to_markdown(index=False),
                "",
            ]
        )

    if exit_updates is not None:
        lines.extend(
            [
                f"Exit updates: **{len(exit_updates)}**",
                "",
                "## Exit updates",
                "",
                exit_updates.to_markdown(index=False),
                "",
            ]
        )

    lines.extend(
        [
            "## Paper journal status",
            "",
            status.to_markdown(index=False),
        ]
    )

    Path(summary_file).write_text("\n".join(lines), encoding="utf-8")


def save_exit_updates(exit_updates: pd.DataFrame) -> Path:
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    path = scans_dir() / f"paper_exit_updates_{timestamp_tag}.csv"
    exit_updates.to_csv(path, index=False)
    return path


def run_exit_update(period: str) -> tuple[pd.DataFrame, pd.DataFrame, Path]:
    initialize_paper_journal(journal_path(), overwrite=False)

    exit_updates = update_open_paper_trades(
        journal_path=journal_path(),
        symbols=WATCHLIST,
        config=CONFIG,
        period=period,
    )
    exit_path = save_exit_updates(exit_updates)

    status = paper_trading_status_report(journal_path())
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    status_path = scans_dir() / f"paper_journal_status_{timestamp_tag}.csv"
    status.to_csv(status_path, index=False)

    print("Paper exit update completed.")
    print(f"Exit update output: {exit_path}")
    print(f"Exit updates: {len(exit_updates)}")
    print("\nExit updates:")
    print(exit_updates.to_string(index=False))
    print("\nPaper journal status:")
    print(status.to_string(index=False))

    if not exit_updates.empty:
        send_exit_alert(exit_updates)

    return exit_updates, status, exit_path


def run_entry_scan(period: str, notes: str) -> tuple[pd.DataFrame, pd.DataFrame, Path, int]:
    initialize_paper_journal(journal_path(), overwrite=False)

    # First update exits in case a prior open paper trade has already closed.
    exit_updates, _, _ = run_exit_update(period=period)

    scan = paper_scan(WATCHLIST, CONFIG, period=period, strategy_params=STRATEGY_PARAMS)
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    output_scan_path = scans_dir() / f"paper_signal_preview_{timestamp_tag}.csv"
    scan.to_csv(output_scan_path, index=False)

    active = scan[scan["signal"] == True].copy()  # noqa: E712
    rows_added = append_active_paper_signals_to_journal(
        active,
        journal_path(),
        notes=notes,
        config=CONFIG,
    )

    status = paper_trading_status_report(journal_path())
    status_path = scans_dir() / f"paper_journal_status_{timestamp_tag}.csv"
    status.to_csv(status_path, index=False)

    print("Paper entry scan completed.")
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

    if rows_added > 0:
        send_entry_alert(active, rows_added)

    write_github_step_summary(scan, status, output_scan_path, exit_updates=exit_updates)

    return scan, status, output_scan_path, rows_added


def run_waiting_entry_loop(period: str, notes: str, scan_every_seconds: int) -> tuple[int, int, str]:
    """
    Wait until the configured entry window, scan repeatedly during that window,
    then stop. Exit tracking is handled by later scheduled runs.
    """
    tz = market_tz()
    start, end = today_entry_start_end()
    now = datetime.now(tz)

    print(f"Workflow started at: {now:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Configured entry window: {start:%Y-%m-%d %H:%M:%S %Z} to {end:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Scan interval: {scan_every_seconds} seconds")

    if now > end:
        msg = "Started after the entry window closed. Running exit update only."
        print(msg)
        exit_updates, status, exit_path = run_exit_update(period=period)
        write_github_step_summary(None, status, None, exit_updates=exit_updates)
        return 0, 0, msg

    if now < start:
        wait_seconds = seconds_until(start)
        print(f"Waiting {wait_seconds:.0f} seconds until entry window opens.")
        time.sleep(wait_seconds)

    scans_completed = 0
    total_rows_added = 0

    while datetime.now(tz) <= end:
        _, _, _, rows_added = run_entry_scan(period=period, notes=notes)
        scans_completed += 1
        total_rows_added += rows_added

        now = datetime.now(tz)
        next_run = now + timedelta(seconds=scan_every_seconds)

        if next_run > end:
            print("Next scan would occur after the entry window. Stopping entry loop.")
            break

        sleep_seconds = seconds_until(next_run)
        print(f"Sleeping {sleep_seconds:.0f} seconds until next entry scan.")
        time.sleep(sleep_seconds)

    msg = f"Finished entry-window scan loop. Scans completed: {scans_completed}. Rows added: {total_rows_added}."
    print(msg)
    return scans_completed, total_rows_added, msg


def run_auto_mode(period: str, notes: str, scan_every_seconds: int) -> tuple[int, int]:
    """
    Decide what to do based on current New York time.

    - Before entry window: wait, then run entry scans until 10:59 ET.
    - During entry window: run one entry scan.
    - After entry window through market close: update open paper exits.
    - After market close: run one final exit update.
    """
    tz = market_tz()
    now = datetime.now(tz)
    start, end = today_entry_start_end()
    close = today_market_close()

    print(f"Auto mode current time: {now:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Entry window: {start:%Y-%m-%d %H:%M:%S %Z} to {end:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Market close reference: {close:%Y-%m-%d %H:%M:%S %Z}")

    if now < start:
        scans_completed, rows_added, _ = run_waiting_entry_loop(
            period=period,
            notes=notes,
            scan_every_seconds=scan_every_seconds,
        )
        return scans_completed, rows_added

    if start <= now <= end:
        _, _, _, rows_added = run_entry_scan(period=period, notes=notes)
        return 1, rows_added

    # After entry window: do not create new entries. Only manage exits.
    exit_updates, status, exit_path = run_exit_update(period=period)
    write_github_step_summary(None, status, None, exit_updates=exit_updates)
    return 0, len(exit_updates)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run cloud-safe paper-observation scans and simulated paper-exit tracking. "
            "No brokerage connection. No live trading."
        )
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "entry", "exit"],
        default="auto",
        help="auto: time-aware mode; entry: force an entry scan; exit: update open paper trades only.",
    )
    parser.add_argument("--period", default=CONFIG.period, help="yfinance period, e.g. 5d")
    parser.add_argument("--notes", default="github-actions paper observation", help="Journal note")
    parser.add_argument(
        "--require-current-entry-window",
        action="store_true",
        help="Skip forced entry scan if current time is not inside the entry window.",
    )
    parser.add_argument(
        "--scan-every-seconds",
        type=int,
        default=300,
        help="Entry-loop scan interval. Default: 300 seconds.",
    )
    args = parser.parse_args()

    if args.mode == "entry":
        if args.require_current_entry_window and not is_entry_window_now(CONFIG.timezone):
            print("Current time is outside the configured entry window. Exiting without entry scan.")
            return 0
        _, _, _, count = run_entry_scan(period=args.period, notes=args.notes)
    elif args.mode == "exit":
        exit_updates, status, exit_path = run_exit_update(period=args.period)
        write_github_step_summary(None, status, None, exit_updates=exit_updates)
        count = len(exit_updates)
    else:
        _, count = run_auto_mode(
            period=args.period,
            notes=args.notes,
            scan_every_seconds=args.scan_every_seconds,
        )

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"result_count={count}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
