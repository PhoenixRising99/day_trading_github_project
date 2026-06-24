from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from daytrading.broker import AlpacaPaperBroker
from daytrading.config import CONFIG, STRATEGY_PARAMS, WATCHLIST
from daytrading.paper import paper_scan
from daytrading.position_state import load_open_position_state, save_open_position_state
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


def _json_write(name: str, payload: dict[str, Any], timestamp_tag: str) -> Path:
    path = logs_dir() / f"{name}_{timestamp_tag}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _write_and_return(name: str, result: dict[str, Any], timestamp_tag: str) -> dict[str, Any]:
    path = _json_write(name, result, timestamp_tag)
    result["result_path"] = str(path)
    print(json.dumps(result, indent=2, default=str))
    return result


def _today_str() -> str:
    return str(pd.Timestamp.now(tz=CONFIG.timezone).date())


def _today_signal_only(active: pd.DataFrame) -> pd.DataFrame:
    """
    Prevent stale data from creating a trade on a holiday or data outage.

    A qualifying signal must have signal_date equal to today's New York date.
    """
    if active.empty:
        return active

    today = _today_str()

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
    """
    One quick, stateless entry check.

    IMPORTANT DESIGN CHANGE from the previous version of this job: this no
    longer queues early and sleeps/loops for hours waiting for the entry
    window. Instead, it is meant to be invoked frequently (every ~5 minutes)
    by a GitHub Actions cron schedule that already spans the entry window in
    UTC. Each invocation does a single check and exits immediately. This
    removes the DST-sensitive timeout risk a long-running wait job has, and
    keeps each run's logs short and easy to read. The script still checks
    the real US/Eastern wall-clock time itself (is_entry_window_now), so it
    is safe even if GitHub fires a run a few minutes early/late or outside
    the intended UTC range.
    """
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")

    if not allow_outside_entry_window and not is_entry_window_now(CONFIG.timezone):
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "outside_entry_window",
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        }
        return _write_and_return("alpaca_paper_strategy_entry", result, timestamp_tag)

    today = _today_str()
    existing_state = load_open_position_state()
    if existing_state and str(existing_state.get("signal_date", "")) == today:
        # Enforces max_trades_per_day = 1 for the whole day, not just while a
        # position is open -- once today's single trade has happened (open
        # OR already closed), do not enter a second one.
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "already_traded_today",
            "existing_state": existing_state,
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        }
        return _write_and_return("alpaca_paper_strategy_entry", result, timestamp_tag)

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
        return _write_and_return("alpaca_paper_strategy_entry", result, timestamp_tag)

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
        return _write_and_return("alpaca_paper_strategy_entry", result, timestamp_tag)

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
        return _write_and_return("alpaca_paper_strategy_entry", result, timestamp_tag)

    if qty <= 0:
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "blocked_non_positive_quantity",
            "scan_path": str(scan_path),
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        }
        return _write_and_return("alpaca_paper_strategy_entry", result, timestamp_tag)

    client_order_id = f"paper-entry-{signal['symbol']}-{today}"

    # SIMPLE market buy, not a bracket order: Alpaca rejects fractional
    # quantities on bracket/OCO orders, and this research account's position
    # sizing is intentionally fractional. See daytrading/broker/alpaca_paper.py
    # for the full explanation.
    order_result = broker.submit_market_buy(
        symbol=str(signal["symbol"]),
        qty=qty,
        confirm=confirm,
        client_order_id=client_order_id,
    )

    new_state = {
        "status": "open",
        "symbol": signal.get("symbol"),
        "qty": qty,
        "signal_date": today,
        "opened_at_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        "entry_preview": signal.get("entry_preview"),
        "stop_loss_preview": signal.get("stop_loss_preview"),
        "take_profit_preview": signal.get("take_profit_preview"),
        "setup_score": signal.get("setup_score"),
        "client_order_id": client_order_id,
        "entry_order": order_result.get("submitted_order", {}),
    }
    state_path = save_open_position_state(new_state)

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
        "position_state_path": str(state_path),
    }
    return _write_and_return("alpaca_paper_strategy_entry", result, timestamp_tag)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Alpaca paper strategy entry. Paper only. No live trading. "
            "Designed to be invoked frequently by a cron schedule that "
            "already spans the entry window; performs one quick check per run."
        )
    )
    parser.add_argument("--period", default=CONFIG.period)
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--allow-outside-entry-window",
        action="store_true",
        help="For testing only. Default requires current ET time to be inside the entry window.",
    )
    args = parser.parse_args()

    run_single_strategy_attempt(
        period=args.period,
        confirm=args.confirm,
        allow_outside_entry_window=args.allow_outside_entry_window,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
