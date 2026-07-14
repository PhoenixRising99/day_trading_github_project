from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from daytrading.broker import AlpacaPaperBroker
from daytrading.config import CONFIG
from daytrading.data_fetch import fetch_intraday_data
from daytrading.indicators import add_indicators
from daytrading.position_state import load_open_position_state, save_open_position_state
from daytrading.strategy import momentum_exit_signal

END_OF_DAY_CUTOFF = pd.Timestamp("15:55").time()


def logs_dir() -> Path:
    path = Path(__file__).resolve().parent / "data" / "logs" / "broker"
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


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _interval_floor_frequency(interval: str) -> str:
    value = str(interval).strip().lower()
    if value.endswith("m"):
        return f"{int(value[:-1])}min"
    if value.endswith("h"):
        return f"{int(value[:-1])}h"
    raise ValueError(f"Unsupported intraday interval: {interval}")


def _latest_completed_row(symbol_data: pd.DataFrame, now_et: pd.Timestamp) -> pd.Series | None:
    """
    Return the latest completed candle only.

    Example: at 11:57 ET with 5-minute data, the 11:55 candle is still forming,
    so the monitor must evaluate 11:50 or earlier.
    """
    if symbol_data.empty:
        return None

    current_bar_start = now_et.floor(_interval_floor_frequency(CONFIG.interval))
    candidates = symbol_data[symbol_data["timestamp"] < current_bar_start].copy()
    candidates = candidates[candidates["volume"] > 0]
    candidates = candidates[candidates["timestamp"].dt.date == now_et.date()]

    if candidates.empty:
        return None

    return candidates.sort_values("timestamp").iloc[-1]


def _append_exit_log(row: dict[str, Any]) -> Path:
    path = logs_dir() / "alpaca_paper_exit_log.csv"
    frame = pd.DataFrame([row])
    if path.exists():
        try:
            existing = pd.read_csv(path)
            duplicate = (
                existing.get("client_order_id", pd.Series(dtype=str))
                .astype(str)
                .eq(str(row.get("client_order_id", "")))
            )
            if duplicate.any():
                return path
            frame = pd.concat([existing, frame], ignore_index=True)
        except pd.errors.EmptyDataError:
            pass
    frame.to_csv(path, index=False)
    return path


def run_position_monitor(*, period: str, confirm: str) -> dict[str, Any]:
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    now_et = pd.Timestamp.now(tz=CONFIG.timezone)

    state = load_open_position_state()
    if not state or state.get("status") != "open":
        return _write_and_return(
            "alpaca_paper_position_monitor",
            {
                "paper_only": True,
                "action": "none",
                "reason": "no_open_position_tracked",
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    symbol = str(state.get("symbol", "")).upper().strip()
    broker = AlpacaPaperBroker.from_env()
    open_positions = {
        str(position.get("symbol", "")).upper(): position
        for position in broker.open_positions()
    }

    if symbol not in open_positions:
        state["status"] = "closed"
        state["closed_reason"] = "reconciled_no_broker_position"
        state["closed_at_et"] = now_et.isoformat()
        save_open_position_state(state)
        return _write_and_return(
            "alpaca_paper_position_monitor",
            {
                "paper_only": True,
                "action": "reconciled",
                "reason": "broker_shows_no_matching_position",
                "symbol": symbol,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    broker_qty = _safe_float(open_positions[symbol].get("qty"))
    qty = broker_qty or _safe_float(state.get("qty"))
    if qty <= 0:
        return _write_and_return(
            "alpaca_paper_position_monitor",
            {
                "paper_only": True,
                "action": "none",
                "reason": "non_positive_broker_quantity",
                "symbol": symbol,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    stop_loss = _safe_float(state.get("stop_loss_preview"))
    take_profit = _safe_float(state.get("take_profit_preview"))

    data = fetch_intraday_data(
        [symbol],
        period=period,
        interval=CONFIG.interval,
        timezone=CONFIG.timezone,
    )
    data = add_indicators(data)
    symbol_data = data[data["symbol"] == symbol].sort_values("timestamp")
    latest = _latest_completed_row(symbol_data, now_et)

    if latest is None:
        return _write_and_return(
            "alpaca_paper_position_monitor",
            {
                "paper_only": True,
                "action": "none",
                "reason": "no_completed_current_session_bar",
                "symbol": symbol,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    exit_reason = None
    if stop_loss and latest["low"] <= stop_loss:
        exit_reason = "stop_loss"
    elif take_profit and latest["high"] >= take_profit:
        exit_reason = "take_profit"
    else:
        exit_now, reason = momentum_exit_signal(latest)
        if exit_now:
            exit_reason = reason
        elif now_et.time() >= END_OF_DAY_CUTOFF:
            exit_reason = "end_of_day"

    if exit_reason is None:
        return _write_and_return(
            "alpaca_paper_position_monitor",
            {
                "paper_only": True,
                "action": "none",
                "reason": "no_exit_condition_met",
                "symbol": symbol,
                "evaluated_bar": str(latest["timestamp"]),
                "last_close": float(latest["close"]),
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    client_order_id = f"paper-exit-{symbol}-{state.get('signal_date', '')}"
    order_result = broker.submit_market_sell(
        symbol=symbol,
        qty=qty,
        confirm=confirm,
        client_order_id=client_order_id,
    )
    final_order = order_result.get("final_order", {})
    final_status = str(final_order.get("status", "")).lower()
    filled_qty = _safe_float(final_order.get("filled_qty"))
    exit_fill_price = _safe_float(final_order.get("filled_avg_price"))

    remaining_positions = {
        str(position.get("symbol", "")).upper(): position
        for position in broker.open_positions()
    }
    remaining_qty = _safe_float(remaining_positions.get(symbol, {}).get("qty"))

    if remaining_qty > 0:
        state["qty"] = remaining_qty
        state["last_exit_attempt"] = {
            "reason": exit_reason,
            "timestamp_et": now_et.isoformat(),
            "order": order_result,
            "filled_qty": filled_qty,
            "remaining_qty": remaining_qty,
        }
        save_open_position_state(state)
        return _write_and_return(
            "alpaca_paper_position_monitor",
            {
                "paper_only": True,
                "action": "exit_pending_or_partial",
                "reason": exit_reason,
                "symbol": symbol,
                "order_status": final_status,
                "filled_qty": filled_qty,
                "remaining_qty": remaining_qty,
                "order_result": order_result,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    entry_fill_price = _safe_float(state.get("entry_fill_price")) or _safe_float(
        state.get("entry_preview")
    )
    realized_pnl = (
        round((exit_fill_price - entry_fill_price) * filled_qty, 6)
        if exit_fill_price and entry_fill_price and filled_qty
        else None
    )

    state["status"] = "closed"
    state["closed_reason"] = exit_reason
    state["closed_at_et"] = now_et.isoformat()
    state["exit_fill_price"] = exit_fill_price or None
    state["exit_filled_qty"] = filled_qty
    state["exit_filled_at"] = final_order.get("filled_at", "")
    state["exit_order_status"] = final_status
    state["realized_pnl"] = realized_pnl
    state["exit_order"] = order_result
    save_open_position_state(state)

    _append_exit_log(
        {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "signal_date": state.get("signal_date", ""),
            "opened_at_et": state.get("opened_at_et", ""),
            "closed_at_et": state["closed_at_et"],
            "qty": filled_qty or qty,
            "entry_preview": state.get("entry_preview"),
            "entry_fill_price": entry_fill_price or None,
            "exit_fill_price": exit_fill_price or None,
            "realized_pnl": realized_pnl,
            "stop_loss_preview": stop_loss,
            "take_profit_preview": take_profit,
            "exit_reason": exit_reason,
            "evaluated_bar": str(latest["timestamp"]),
            "evaluated_close": float(latest["close"]),
            "order_status": final_status,
        }
    )

    return _write_and_return(
        "alpaca_paper_position_monitor",
        {
            "paper_only": True,
            "action": "closed_position",
            "reason": exit_reason,
            "symbol": symbol,
            "qty": filled_qty or qty,
            "entry_fill_price": entry_fill_price or None,
            "exit_fill_price": exit_fill_price or None,
            "realized_pnl": realized_pnl,
            "order_result": order_result,
            "timestamp_et": now_et.isoformat(),
        },
        timestamp_tag,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Alpaca paper position monitor. Paper only. No live trading."
    )
    parser.add_argument("--period", default=CONFIG.period)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    run_position_monitor(period=args.period, confirm=args.confirm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
