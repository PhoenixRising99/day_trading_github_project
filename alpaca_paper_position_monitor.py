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


def _interval_timedelta() -> pd.Timedelta:
    value = str(CONFIG.interval).strip().lower()
    if value.endswith("m"):
        return pd.Timedelta(minutes=int(value[:-1]))
    if value.endswith("h"):
        return pd.Timedelta(hours=int(value[:-1]))
    raise ValueError(f"Unsupported intraday interval: {CONFIG.interval}")


def _interval_floor_frequency() -> str:
    delta = _interval_timedelta()
    seconds = int(delta.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}min"
    return f"{seconds}s"


def _expected_completed_timestamp(now_et: pd.Timestamp) -> pd.Timestamp:
    return now_et.floor(_interval_floor_frequency()) - _interval_timedelta()


def _latest_completed_row(symbol_data: pd.DataFrame, now_et: pd.Timestamp) -> pd.Series | None:
    if symbol_data.empty:
        return None

    current_bar_start = now_et.floor(_interval_floor_frequency())
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


def _market_cutoff_et(broker: AlpacaPaperBroker, now_et: pd.Timestamp) -> pd.Timestamp | None:
    """
    Use Alpaca's market clock rather than a hard-coded 15:55 cutoff.

    The strategy exits five minutes before the actual exchange close, including
    early-close sessions.
    """
    clock = broker.clock_snapshot()
    raw_close = clock.get("next_close")
    if not raw_close:
        return None

    close_ts = pd.Timestamp(raw_close)
    if close_ts.tzinfo is None:
        close_ts = close_ts.tz_localize(CONFIG.timezone)
    else:
        close_ts = close_ts.tz_convert(CONFIG.timezone)

    if close_ts.date() != now_et.date():
        return None
    return close_ts - pd.Timedelta(minutes=5)


def _protective_snapshot(broker: AlpacaPaperBroker, state: dict[str, Any]) -> dict:
    order_id = str(state.get("protective_stop_order_id", "") or "")
    if order_id:
        try:
            return broker.get_order_snapshot(order_id)
        except Exception:
            pass

    client_id = str(state.get("protective_stop_client_order_id", "") or "")
    if client_id:
        return broker.get_order_by_client_order_id(client_id)
    return {}


def _cancel_protective_stop(broker: AlpacaPaperBroker, state: dict[str, Any]) -> dict:
    snap = _protective_snapshot(broker, state)
    if not snap:
        return {}

    status = str(snap.get("status", "")).lower()
    if status in {"filled", "canceled", "cancelled", "expired", "rejected"}:
        return snap

    return broker.cancel_order(str(snap.get("id", "")))


def _ensure_protective_stop(
    broker: AlpacaPaperBroker,
    state: dict[str, Any],
    *,
    qty: float,
    confirm: str,
) -> dict:
    stop_price = _safe_float(state.get("stop_loss") or state.get("stop_loss_preview"))
    if qty <= 0 or stop_price <= 0:
        return {}

    existing = _protective_snapshot(broker, state)
    existing_status = str(existing.get("status", "")).lower()
    existing_qty = _safe_float(existing.get("qty"))

    if existing and existing_status not in {
        "filled", "canceled", "cancelled", "expired", "rejected", "replaced", "stopped"
    } and abs(existing_qty - qty) < 1e-6:
        return existing

    base_client_id = str(
        state.get("protective_stop_client_order_id")
        or f"paper-protective-stop-{state.get('symbol','')}-{state.get('signal_date','')}"
    )
    result = broker.submit_protective_stop_sell(
        symbol=str(state.get("symbol", "")),
        qty=qty,
        stop_price=stop_price,
        confirm=confirm,
        client_order_id=base_client_id,
    )
    order = result.get("final_order", {})
    state["protective_stop_client_order_id"] = order.get("client_order_id") or base_client_id
    state["protective_stop_order_id"] = order.get("id", "")
    state["protective_stop_status"] = order.get("status", "")
    state["protective_stop_price"] = stop_price
    state["protective_stop_order"] = result
    state.pop("protective_stop_error", None)
    save_open_position_state(state)
    return order


def _record_closed_state(
    state: dict[str, Any],
    *,
    reason: str,
    now_et: pd.Timestamp,
    client_order_id: str,
    filled_qty: float,
    exit_fill_price: float,
    order_status: str,
    order_result: dict[str, Any],
    evaluated_bar: str = "",
    evaluated_close: float | None = None,
) -> dict[str, Any]:
    entry_fill_price = _safe_float(state.get("entry_fill_price")) or _safe_float(
        state.get("entry_preview")
    )
    realized_pnl = (
        round((exit_fill_price - entry_fill_price) * filled_qty, 6)
        if exit_fill_price and entry_fill_price and filled_qty
        else None
    )

    final_order = order_result.get("final_order", {})
    state["status"] = "closed"
    state["closed_reason"] = reason
    state["closed_at_et"] = now_et.isoformat()
    state["exit_fill_price"] = exit_fill_price or None
    state["exit_filled_qty"] = filled_qty
    state["exit_filled_at"] = final_order.get("filled_at", "")
    state["exit_order_status"] = order_status
    state["realized_pnl"] = realized_pnl
    state["exit_order"] = order_result
    save_open_position_state(state)

    _append_exit_log(
        {
            "client_order_id": client_order_id,
            "symbol": state.get("symbol", ""),
            "signal_date": state.get("signal_date", ""),
            "opened_at_et": state.get("opened_at_et", ""),
            "closed_at_et": state["closed_at_et"],
            "qty": filled_qty,
            "entry_preview": state.get("entry_preview"),
            "entry_fill_price": entry_fill_price or None,
            "exit_fill_price": exit_fill_price or None,
            "realized_pnl": realized_pnl,
            "stop_loss_preview": state.get("stop_loss_preview"),
            "take_profit_preview": state.get("take_profit_preview"),
            "exit_reason": reason,
            "evaluated_bar": evaluated_bar,
            "evaluated_close": evaluated_close,
            "order_status": order_status,
        }
    )
    return state


def _recover_orphaned_strategy_position(
    broker: AlpacaPaperBroker,
    *,
    open_positions: dict[str, dict],
    confirm: str,
    now_et: pd.Timestamp,
    timestamp_tag: str,
) -> dict[str, Any] | None:
    """
    If repository state was lost but Alpaca still holds a position created by
    this strategy, flatten it rather than leaving an unmanaged overnight risk.

    Manual/non-strategy positions are never touched.
    """
    strategy_orders = [
        order
        for order in broker.all_orders()
        if str(order.get("client_order_id", "")).startswith("paper-entry-")
        and str(order.get("status", "")).lower() == "filled"
    ]

    for symbol, position in open_positions.items():
        matching = [
            order for order in strategy_orders
            if str(order.get("symbol", "")).upper() == symbol
        ]
        if not matching:
            continue

        matching.sort(key=lambda x: str(x.get("filled_at", "")), reverse=True)
        entry = matching[0]
        client_entry_id = str(entry.get("client_order_id", ""))
        signal_date = client_entry_id.rsplit("-", 1)[-1]

        # Do not touch an unrelated/manual position merely because this symbol
        # was traded by the strategy months ago. Recovery is limited to a very
        # recent strategy entry, allowing for a weekend.
        try:
            entry_date = pd.Timestamp(signal_date).date()
            age_days = (now_et.date() - entry_date).days
        except Exception:
            continue
        if age_days < 0 or age_days > 4:
            continue

        qty = _safe_float(position.get("qty"))
        if qty <= 0:
            continue

        exit_client_id = f"paper-exit-recovery-{symbol}-{signal_date}"
        order_result = broker.submit_market_sell(
            symbol=symbol,
            qty=qty,
            confirm=confirm,
            client_order_id=exit_client_id,
        )
        final = order_result.get("final_order", {})
        return _write_and_return(
            "alpaca_paper_position_monitor",
            {
                "paper_only": True,
                "action": "recovery_exit",
                "reason": "orphaned_strategy_position_without_state",
                "symbol": symbol,
                "qty": qty,
                "order_result": order_result,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    return None


def run_position_monitor(*, period: str, confirm: str) -> dict[str, Any]:
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    now_et = pd.Timestamp.now(tz=CONFIG.timezone)
    broker = AlpacaPaperBroker.from_env()

    open_positions = {
        str(position.get("symbol", "")).upper(): position
        for position in broker.open_positions()
    }
    state = load_open_position_state()

    if not state or state.get("status") != "open":
        if open_positions:
            recovered = _recover_orphaned_strategy_position(
                broker,
                open_positions=open_positions,
                confirm=confirm,
                now_et=now_et,
                timestamp_tag=timestamp_tag,
            )
            if recovered is not None:
                return recovered

            return _write_and_return(
                "alpaca_paper_position_monitor",
                {
                    "paper_only": True,
                    "action": "none",
                    "reason": "unmanaged_non_strategy_broker_position",
                    "open_positions": list(open_positions.values()),
                    "timestamp_et": now_et.isoformat(),
                },
                timestamp_tag,
            )

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
    protective = _protective_snapshot(broker, state)

    if symbol not in open_positions:
        protective_status = str(protective.get("status", "")).lower()
        if protective_status == "filled":
            filled_qty = _safe_float(protective.get("filled_qty"))
            exit_fill_price = _safe_float(protective.get("filled_avg_price"))
            client_id = str(protective.get("client_order_id", "")) or str(
                state.get("protective_stop_client_order_id", "")
            )
            result = {
                "paper_only": True,
                "order_kind": "protective_stop_sell",
                "final_order": protective,
            }
            _record_closed_state(
                state,
                reason="stop_loss_broker",
                now_et=now_et,
                client_order_id=client_id,
                filled_qty=filled_qty,
                exit_fill_price=exit_fill_price,
                order_status=protective_status,
                order_result=result,
            )
            return _write_and_return(
                "alpaca_paper_position_monitor",
                {
                    "paper_only": True,
                    "action": "reconciled_closed_position",
                    "reason": "protective_stop_filled",
                    "symbol": symbol,
                    "exit_fill_price": exit_fill_price,
                    "realized_pnl": state.get("realized_pnl"),
                    "timestamp_et": now_et.isoformat(),
                },
                timestamp_tag,
            )

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
                "protective_order": protective,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    broker_qty = _safe_float(open_positions[symbol].get("qty"))
    if broker_qty <= 0:
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

    # Never carry a strategy position into a later trading date.
    signal_date = str(state.get("signal_date", ""))
    stale_overnight = bool(signal_date and signal_date != str(now_et.date()))

    try:
        protective = _ensure_protective_stop(
            broker,
            state,
            qty=broker_qty,
            confirm=confirm,
        )
    except Exception as exc:  # noqa: BLE001
        state["protective_stop_status"] = "repair_failed"
        state["protective_stop_error"] = str(exc)
        save_open_position_state(state)
        protective = {}

    cutoff = _market_cutoff_et(broker, now_et)
    eod_due = cutoff is not None and now_et >= cutoff

    latest = None
    data_stale = False
    data_error = ""
    try:
        data = fetch_intraday_data(
            [symbol],
            period=period,
            interval=CONFIG.interval,
            timezone=CONFIG.timezone,
        )
        data = add_indicators(data)
        symbol_data = data[data["symbol"] == symbol].sort_values("timestamp")
        latest = _latest_completed_row(symbol_data, now_et)
        if latest is not None:
            expected = _expected_completed_timestamp(now_et).floor("min")
            actual = pd.Timestamp(latest["timestamp"]).floor("min")
            data_stale = actual != expected
    except Exception as exc:  # noqa: BLE001
        data_error = str(exc)
        data_stale = True

    stop_loss = _safe_float(state.get("stop_loss") or state.get("stop_loss_preview"))
    take_profit = _safe_float(state.get("take_profit") or state.get("take_profit_preview"))

    exit_reason = None
    if stale_overnight:
        exit_reason = "stale_overnight_position"
    elif eod_due:
        exit_reason = "end_of_day"
    elif latest is not None and not data_stale:
        if stop_loss and float(latest["low"]) <= stop_loss:
            exit_reason = "stop_loss"
        elif take_profit and float(latest["high"]) >= take_profit:
            exit_reason = "take_profit"
        else:
            exit_now, reason = momentum_exit_signal(latest)
            if exit_now:
                exit_reason = reason

    if exit_reason is None:
        return _write_and_return(
            "alpaca_paper_position_monitor",
            {
                "paper_only": True,
                "action": "none",
                "reason": "stale_market_data" if data_stale else "no_exit_condition_met",
                "symbol": symbol,
                "evaluated_bar": str(latest["timestamp"]) if latest is not None else None,
                "expected_bar": str(_expected_completed_timestamp(now_et)),
                "last_close": float(latest["close"]) if latest is not None else None,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "protective_stop_status": protective.get("status"),
                "market_cutoff_et": cutoff.isoformat() if cutoff is not None else None,
                "data_error": data_error or None,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    # Cancel the resting protective stop before a discretionary market exit to
    # avoid a later oversell. If it filled during cancellation, reconciliation
    # below will observe no remaining broker position.
    protective_cancel = _cancel_protective_stop(broker, state)

    remaining_before_exit = {
        str(position.get("symbol", "")).upper(): position
        for position in broker.open_positions()
    }
    qty = _safe_float(remaining_before_exit.get(symbol, {}).get("qty"))

    if qty <= 0:
        protective_after = _protective_snapshot(broker, state)
        if str(protective_after.get("status", "")).lower() == "filled":
            filled_qty = _safe_float(protective_after.get("filled_qty"))
            exit_fill_price = _safe_float(protective_after.get("filled_avg_price"))
            client_id = str(protective_after.get("client_order_id", ""))
            result = {
                "paper_only": True,
                "order_kind": "protective_stop_sell",
                "final_order": protective_after,
            }
            _record_closed_state(
                state,
                reason="stop_loss_broker",
                now_et=now_et,
                client_order_id=client_id,
                filled_qty=filled_qty,
                exit_fill_price=exit_fill_price,
                order_status="filled",
                order_result=result,
                evaluated_bar=str(latest["timestamp"]) if latest is not None else "",
                evaluated_close=float(latest["close"]) if latest is not None else None,
            )
        return _write_and_return(
            "alpaca_paper_position_monitor",
            {
                "paper_only": True,
                "action": "reconciled_closed_position",
                "reason": "position_closed_during_protective_cancel",
                "symbol": symbol,
                "protective_cancel": protective_cancel,
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

        # Re-protect any residual shares after a partial/pending market exit.
        try:
            _ensure_protective_stop(
                broker,
                state,
                qty=remaining_qty,
                confirm=confirm,
            )
        except Exception as exc:  # noqa: BLE001
            state["protective_stop_status"] = "repair_failed_after_partial_exit"
            state["protective_stop_error"] = str(exc)
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

    _record_closed_state(
        state,
        reason=exit_reason,
        now_et=now_et,
        client_order_id=client_order_id,
        filled_qty=filled_qty or qty,
        exit_fill_price=exit_fill_price,
        order_status=final_status,
        order_result=order_result,
        evaluated_bar=str(latest["timestamp"]) if latest is not None else "",
        evaluated_close=float(latest["close"]) if latest is not None else None,
    )

    return _write_and_return(
        "alpaca_paper_position_monitor",
        {
            "paper_only": True,
            "action": "closed_position",
            "reason": exit_reason,
            "symbol": symbol,
            "qty": filled_qty or qty,
            "entry_fill_price": state.get("entry_fill_price"),
            "exit_fill_price": exit_fill_price or None,
            "realized_pnl": state.get("realized_pnl"),
            "protective_cancel": protective_cancel,
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
