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


def _append_exit_log(row: dict[str, Any]) -> Path:
    path = logs_dir() / "alpaca_paper_exit_log.csv"
    frame = pd.DataFrame([row])
    if path.exists():
        try:
            existing = pd.read_csv(path)
            frame = pd.concat([existing, frame], ignore_index=True)
        except pd.errors.EmptyDataError:
            pass
    frame.to_csv(path, index=False)
    return path


def _write_and_return(name: str, result: dict[str, Any], timestamp_tag: str) -> dict[str, Any]:
    path = _json_write(name, result, timestamp_tag)
    result["result_path"] = str(path)
    print(json.dumps(result, indent=2, default=str))
    return result


def run_position_monitor(*, period: str, confirm: str) -> dict[str, Any]:
    """
    One quick, stateless exit check.

    Designed to be invoked frequently (every ~5 minutes) throughout the
    trading day by a GitHub Actions cron schedule. Watches the single
    tracked open paper position (if any) for the same exit conditions used
    in the V9/V10/V11 backtest and the custom CSV paper journal: stop-loss,
    take-profit, two-bar VWAP failure, EMA trend failure, and end-of-day
    flatten at/after 15:55 ET. If nothing is currently tracked, this is a
    cheap no-op.

    This did not exist before. It is required because entries now use a
    simple market order (see alpaca_paper_strategy_entry_job.py /
    daytrading/broker/alpaca_paper.py) instead of an Alpaca bracket order,
    so nothing on Alpaca's side is watching the stop/target automatically --
    this job is what makes that watching happen.
    """
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    now_et = pd.Timestamp.now(tz=CONFIG.timezone)

    state = load_open_position_state()
    if not state or state.get("status") != "open":
        result = {
            "paper_only": True,
            "action": "none",
            "reason": "no_open_position_tracked",
            "timestamp_et": now_et.isoformat(),
        }
        return _write_and_return("alpaca_paper_position_monitor", result, timestamp_tag)

    symbol = str(state.get("symbol", "")).upper().strip()
    broker = AlpacaPaperBroker.from_env()
    open_positions = {str(p.get("symbol", "")).upper(): p for p in broker.open_positions()}

    if symbol not in open_positions:
        # Reconcile: our state says open, but Alpaca shows no matching
        # position. This can happen if a previous monitor run sold but the
        # commit step failed, or the position was changed outside this
        # workflow. Mark it closed locally so the entry job's
        # one-trade-per-day guard stays correct, and surface this clearly
        # rather than silently retrying a sell.
        state["status"] = "closed"
        state["closed_reason"] = "reconciled_no_broker_position"
        state["closed_at_et"] = now_et.isoformat()
        save_open_position_state(state)
        result = {
            "paper_only": True,
            "action": "reconciled",
            "reason": "broker_shows_no_matching_position",
            "symbol": symbol,
            "timestamp_et": now_et.isoformat(),
        }
        return _write_and_return("alpaca_paper_position_monitor", result, timestamp_tag)

    broker_qty = _safe_float(open_positions[symbol].get("qty")) or 0.0
    qty = broker_qty or float(state.get("qty") or 0)

    stop_loss = float(state.get("stop_loss_preview"))
    take_profit = float(state.get("take_profit_preview"))

    data = fetch_intraday_data(
        [symbol], period=period, interval=CONFIG.interval, timezone=CONFIG.timezone
    )
    data = add_indicators(data)
    symbol_data = data[data["symbol"] == symbol].sort_values("timestamp")

    if symbol_data.empty:
        result = {
            "paper_only": True,
            "action": "none",
            "reason": "no_market_data",
            "symbol": symbol,
            "timestamp_et": now_et.isoformat(),
        }
        return _write_and_return("alpaca_paper_position_monitor", result, timestamp_tag)

    latest = symbol_data.iloc[-1]

    exit_reason = None
    if latest["low"] <= stop_loss:
        exit_reason = "stop_loss"
    elif latest["high"] >= take_profit:
        exit_reason = "take_profit"
    else:
        exit_now, reason = momentum_exit_signal(latest)
        if exit_now:
            exit_reason = reason
        elif now_et.time() >= END_OF_DAY_CUTOFF:
            exit_reason = "end_of_day"

    if exit_reason is None:
        result = {
            "paper_only": True,
            "action": "none",
            "reason": "no_exit_condition_met",
            "symbol": symbol,
            "last_close": float(latest["close"]),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "timestamp_et": now_et.isoformat(),
        }
        return _write_and_return("alpaca_paper_position_monitor", result, timestamp_tag)

    # Deterministic client_order_id (one per symbol per signal day) so a
    # retried/duplicate run cannot submit two separate exit orders for the
    # same position.
    client_order_id = f"paper-exit-{symbol}-{state.get('signal_date', '')}"

    order_result = broker.submit_market_sell(
        symbol=symbol,
        qty=qty,
        confirm=confirm,
        client_order_id=client_order_id,
    )

    state["status"] = "closed"
    state["closed_reason"] = exit_reason
    state["closed_at_et"] = now_et.isoformat()
    state["exit_order"] = order_result.get("submitted_order", {})
    save_open_position_state(state)

    _append_exit_log(
        {
            "symbol": symbol,
            "signal_date": state.get("signal_date", ""),
            "opened_at_et": state.get("opened_at_et", ""),
            "closed_at_et": state["closed_at_et"],
            "qty": qty,
            "entry_preview": state.get("entry_preview"),
            "stop_loss_preview": stop_loss,
            "take_profit_preview": take_profit,
            "exit_reason": exit_reason,
            "exit_close_price": float(latest["close"]),
        }
    )

    result = {
        "paper_only": True,
        "action": "closed_position",
        "reason": exit_reason,
        "symbol": symbol,
        "qty": qty,
        "order_result": order_result,
        "timestamp_et": now_et.isoformat(),
    }
    return _write_and_return("alpaca_paper_position_monitor", result, timestamp_tag)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Alpaca paper position exit monitor. Paper only. No live trading. "
            "Designed to be invoked frequently by a cron schedule spanning "
            "market hours; performs one quick check per run."
        )
    )
    parser.add_argument("--period", default=CONFIG.period)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    run_position_monitor(period=args.period, confirm=args.confirm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
