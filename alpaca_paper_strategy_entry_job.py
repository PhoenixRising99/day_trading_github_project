from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from zoneinfo import ZoneInfo

from daytrading.broker import AlpacaPaperBroker
from daytrading.config import (
    CONFIG,
    ENTRY_END_HOUR,
    ENTRY_END_MINUTE,
    ENTRY_START_HOUR,
    ENTRY_START_MINUTE,
    STRATEGY_PARAMS,
    WATCHLIST,
)
from daytrading.paper import paper_scan
from daytrading.strategy import is_entry_window_now


MAX_RESEARCH_POSITION_VALUE = 24.00


def logs_dir() -> Path:
    path = Path(__file__).resolve().parent / "data" / "logs" / "broker"
    path.mkdir(parents=True, exist_ok=True)
    return path


def scans_dir() -> Path:
    path = Path(__file__).resolve().parent / "data" / "logs" / "scans"
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def _json_write(name: str, payload: dict[str, Any], timestamp_tag: str) -> Path:
    path = logs_dir() / f"{name}_{timestamp_tag}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _today_signal_only(active: pd.DataFrame) -> pd.DataFrame:
    """
    Prevent stale data from creating a trade on a holiday or data outage.

    A qualifying signal must have signal_date equal to today's New York date.
    """
    if active.empty:
        return active

    today = str(pd.Timestamp.now(tz=CONFIG.timezone).date())

    if "signal_date" in active.columns:
        return active[active["signal_date"].astype(str).eq(today)].copy()

    if "data_timestamp" in active.columns:
        dates = pd.to_datetime(active["data_timestamp"], errors="coerce").dt.tz_convert(
            CONFIG.timezone
        ).dt.date.astype(str)
        return active[dates.eq(today)].copy()

    return active.iloc[0:0].copy()


def run_single_strategy_attempt(
    *,
    period: str,
    confirm: str,
    allow_outside_entry_window: bool,
) -> dict[str, Any]:
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")

    if not allow_outside_entry_window and not is_entry_window_now(CONFIG.timezone):
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "outside_entry_window",
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        }
        path = _json_write("alpaca_paper_strategy_entry", result, timestamp_tag)
        result["result_path"] = str(path)
        print(json.dumps(result, indent=2, default=str))
        return result

    scan = paper_scan(WATCHLIST, CONFIG, period=period, strategy_params=STRATEGY_PARAMS)
    scan_path = scans_dir() / f"alpaca_strategy_signal_preview_{timestamp_tag}.csv"
    scan.to_csv(scan_path, index=False)

    active = scan[scan["signal"] == True].copy()  # noqa: E712
    active = _today_signal_only(active)

    if active.empty:
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "no_active_signal",
            "scan_path": str(scan_path),
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        }
        path = _json_write("alpaca_paper_strategy_entry", result, timestamp_tag)
        result["result_path"] = str(path)
        print(json.dumps(result, indent=2, default=str))
        return result

    active = active.sort_values(
        ["setup_score", "volume_ratio", "symbol"],
        ascending=[False, False, True],
    )
    signal = active.iloc[0].to_dict()

    broker = AlpacaPaperBroker.from_env()
    open_positions = broker.open_positions()
    open_orders = broker.open_orders()

    if open_positions or open_orders:
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "blocked_existing_open_position_or_order",
            "open_positions_count": len(open_positions),
            "open_orders_count": len(open_orders),
            "scan_path": str(scan_path),
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        }
        path = _json_write("alpaca_paper_strategy_entry", result, timestamp_tag)
        result["result_path"] = str(path)
        print(json.dumps(result, indent=2, default=str))
        return result

    position_value = float(signal.get("position_value_preview", 0) or 0)
    qty = float(signal.get("shares_preview", 0) or 0)

    if position_value > MAX_RESEARCH_POSITION_VALUE + 0.01:
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "blocked_position_value_exceeds_research_limit",
            "position_value": position_value,
            "max_position_value": MAX_RESEARCH_POSITION_VALUE,
            "scan_path": str(scan_path),
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        }
        path = _json_write("alpaca_paper_strategy_entry", result, timestamp_tag)
        result["result_path"] = str(path)
        print(json.dumps(result, indent=2, default=str))
        return result

    client_order_id = f"paper-strategy-{signal['symbol']}-{timestamp_tag}"

    order_result = broker.submit_market_bracket_buy(
        symbol=str(signal["symbol"]),
        qty=qty,
        take_profit_price=float(signal["take_profit_preview"]),
        stop_loss_price=float(signal["stop_loss_preview"]),
        confirm=confirm,
        client_order_id=client_order_id,
    )

    result = {
        "paper_only": True,
        "submitted": True,
        "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        "scan_path": str(scan_path),
        "selected_signal": {
            "symbol": signal.get("symbol"),
            "data_timestamp": str(signal.get("data_timestamp")),
            "signal_date": signal.get("signal_date"),
            "setup_score": signal.get("setup_score"),
            "setup_max_score": signal.get("setup_max_score"),
            "entry_preview": signal.get("entry_preview"),
            "shares_preview": signal.get("shares_preview"),
            "position_value_preview": signal.get("position_value_preview"),
            "stop_loss_preview": signal.get("stop_loss_preview"),
            "take_profit_preview": signal.get("take_profit_preview"),
        },
        "order_result": order_result,
    }

    path = _json_write("alpaca_paper_strategy_entry", result, timestamp_tag)
    result["result_path"] = str(path)
    print(json.dumps(result, indent=2, default=str))
    return result


def run_strategy_entry_loop(
    *,
    period: str,
    confirm: str,
    scan_every_seconds: int,
) -> dict[str, Any]:
    tz = market_tz()
    start, end = today_entry_start_end()
    now = datetime.now(tz)

    print(f"Workflow started at: {now:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Strategy entry window: {start:%Y-%m-%d %H:%M:%S %Z} to {end:%Y-%m-%d %H:%M:%S %Z}")
    print(f"Scan interval: {scan_every_seconds} seconds")

    if now > end:
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "started_after_entry_window",
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        }
        timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
        path = _json_write("alpaca_paper_strategy_entry_loop", result, timestamp_tag)
        result["result_path"] = str(path)
        print(json.dumps(result, indent=2, default=str))
        return result

    if now < start:
        wait_seconds = seconds_until(start)
        print(f"Waiting {wait_seconds:.0f} seconds until entry window opens.")
        time.sleep(wait_seconds)

    attempts = 0
    last_result: dict[str, Any] | None = None

    while datetime.now(tz) <= end:
        attempts += 1
        print(f"Starting Alpaca paper strategy attempt #{attempts}.")
        last_result = run_single_strategy_attempt(
            period=period,
            confirm=confirm,
            allow_outside_entry_window=False,
        )

        if last_result.get("submitted") is True:
            print("Paper broker order submitted. Stopping entry loop.")
            break

        if last_result.get("reason") == "blocked_existing_open_position_or_order":
            print("Existing Alpaca paper position/order found. Stopping entry loop.")
            break

        now = datetime.now(tz)
        next_run = now + timedelta(seconds=scan_every_seconds)

        if next_run > end:
            print("Next scan would occur after the entry window. Stopping entry loop.")
            break

        sleep_seconds = seconds_until(next_run)
        print(f"Sleeping {sleep_seconds:.0f} seconds until next attempt.")
        time.sleep(sleep_seconds)

    summary = {
        "paper_only": True,
        "loop_complete": True,
        "attempts": attempts,
        "submitted": bool(last_result and last_result.get("submitted") is True),
        "last_result": last_result or {},
        "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
    }

    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    path = _json_write("alpaca_paper_strategy_entry_loop", summary, timestamp_tag)
    summary["result_path"] = str(path)
    print(json.dumps(summary, indent=2, default=str))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Alpaca paper strategy entry. Paper only. No live trading."
    )
    parser.add_argument("--period", default=CONFIG.period)
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--mode",
        choices=["single", "loop"],
        default="single",
        help="single runs one attempt; loop waits/scans until the entry window closes.",
    )
    parser.add_argument(
        "--allow-outside-entry-window",
        action="store_true",
        help="For testing only. Default requires current ET time to be inside the entry window.",
    )
    parser.add_argument(
        "--scan-every-seconds",
        type=int,
        default=300,
        help="Loop scan interval. Default: 300 seconds.",
    )
    args = parser.parse_args()

    if args.mode == "loop":
        run_strategy_entry_loop(
            period=args.period,
            confirm=args.confirm,
            scan_every_seconds=args.scan_every_seconds,
        )
    else:
        run_single_strategy_attempt(
            period=args.period,
            confirm=args.confirm,
            allow_outside_entry_window=args.allow_outside_entry_window,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
