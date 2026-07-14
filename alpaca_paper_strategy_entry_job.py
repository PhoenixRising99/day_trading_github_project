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
    if active.empty:
        return active

    today = _today_str()
    if "signal_date" in active.columns:
        return active[active["signal_date"].astype(str).eq(today)].copy()

    if "data_timestamp" in active.columns:
        timestamps = pd.to_datetime(active["data_timestamp"], errors="coerce")
        dates = timestamps.dt.tz_convert(CONFIG.timezone).dt.date.astype(str)
        return active[dates.eq(today)].copy()

    return active.iloc[0:0].copy()


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def run_single_strategy_attempt(
    *,
    period: str,
    confirm: str,
    allow_outside_entry_window: bool,
) -> dict[str, Any]:
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    now_et = pd.Timestamp.now(tz=CONFIG.timezone)

    if not allow_outside_entry_window and not is_entry_window_now(CONFIG.timezone):
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "outside_entry_window",
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    today = _today_str()
    existing_state = load_open_position_state()
    if existing_state and str(existing_state.get("signal_date", "")) == today:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "already_traded_today",
                "existing_state": existing_state,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    broker = AlpacaPaperBroker.from_env()
    open_positions = broker.open_positions()
    open_orders = broker.open_orders()
    if open_positions or open_orders:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "blocked_existing_open_position_or_order",
                "open_positions": open_positions,
                "open_orders": open_orders,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    scan = paper_scan(WATCHLIST, CONFIG, period=period, strategy_params=STRATEGY_PARAMS)
    scan_path = scans_dir() / f"alpaca_strategy_signal_preview_{timestamp_tag}.csv"
    scan.to_csv(scan_path, index=False)

    active = _today_signal_only(scan[scan["signal"] == True].copy())  # noqa: E712
    if active.empty:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "no_active_signal",
                "scan_path": str(scan_path),
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    active = active.sort_values(
        ["setup_score", "volume_ratio", "symbol"],
        ascending=[False, False, True],
    )
    signal = active.iloc[0].to_dict()

    position_value = _safe_float(signal.get("position_value_preview"))
    requested_qty = _safe_float(signal.get("shares_preview"))

    if position_value > MAX_RESEARCH_POSITION_VALUE + 0.01:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "blocked_position_value_exceeds_research_limit",
                "position_value": position_value,
                "max_position_value": MAX_RESEARCH_POSITION_VALUE,
                "scan_path": str(scan_path),
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    if requested_qty <= 0:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "blocked_non_positive_quantity",
                "scan_path": str(scan_path),
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    client_order_id = f"paper-entry-{signal['symbol']}-{today}"
    order_result = broker.submit_market_buy(
        symbol=str(signal["symbol"]),
        qty=requested_qty,
        confirm=confirm,
        client_order_id=client_order_id,
    )

    final_order = order_result.get("final_order", {})
    final_status = str(final_order.get("status", "")).lower()
    filled_qty = _safe_float(final_order.get("filled_qty"))
    fill_price = _safe_float(final_order.get("filled_avg_price"))

    if filled_qty <= 0:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": True,
                "position_opened": False,
                "reason": f"entry_not_filled_{final_status or 'unknown'}",
                "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
                "scan_path": str(scan_path),
                "selected_signal": signal,
                "order_result": order_result,
            },
            timestamp_tag,
        )

    new_state = {
        "status": "open",
        "symbol": signal.get("symbol"),
        "qty": filled_qty,
        "qty_requested": requested_qty,
        "signal_date": today,
        "opened_at_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        "entry_preview": signal.get("entry_preview"),
        "entry_fill_price": fill_price,
        "entry_filled_at": final_order.get("filled_at", ""),
        "entry_order_status": final_status,
        "stop_loss_preview": signal.get("stop_loss_preview"),
        "take_profit_preview": signal.get("take_profit_preview"),
        "setup_score": signal.get("setup_score"),
        "client_order_id": client_order_id,
        "entry_order": order_result,
    }
    state_path = save_open_position_state(new_state)

    return _write_and_return(
        "alpaca_paper_strategy_entry",
        {
            "paper_only": True,
            "submitted": True,
            "position_opened": True,
            "reason": "entry_filled",
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
        },
        timestamp_tag,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Alpaca paper strategy entry. Paper only. No live trading."
    )
    parser.add_argument("--period", default=CONFIG.period)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allow-outside-entry-window", action="store_true")
    args = parser.parse_args()

    run_single_strategy_attempt(
        period=args.period,
        confirm=args.confirm,
        allow_outside_entry_window=args.allow_outside_entry_window,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
