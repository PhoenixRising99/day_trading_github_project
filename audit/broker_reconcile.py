"""Read-only audit of broker paper positions, open orders and tracked stop.

This command only calls Alpaca READ endpoints; it never submits/cancels orders.
The paper-only account adapter rejects live account configuration.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from daytrading.broker import AlpacaPaperBroker
from daytrading.position_state import load_open_position_state

TERMINAL = {"filled", "canceled", "cancelled", "expired", "rejected", "replaced", "stopped"}


def reconcile(broker: AlpacaPaperBroker, state: dict[str, Any]) -> dict:
    positions = broker.open_positions()
    # Direct get_orders: failures must not be silently converted into "no orders".
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    orders = broker.client().get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
    )
    open_orders = [broker._order_snapshot(order) for order in orders]
    tracked_symbol = str(state.get("symbol", "")).upper()
    tracked_open = state.get("status") == "open"
    matching = next((p for p in positions
                     if str(p.get("symbol", "")).upper() == tracked_symbol), None)
    strategy_orders = [
        o for o in open_orders
        if str(o.get("client_order_id", "")).startswith("paper-")
    ]
    stop_id = str(state.get("protective_stop_order_id") or "")
    stop = None
    stop_error = None
    if stop_id:
        try:
            stop = broker.get_order_snapshot(stop_id)
        except Exception as exc:
            stop_error = type(exc).__name__

    stop_status = str((stop or {}).get("status", "")).lower()
    concerns = []
    if tracked_open and not matching:
        concerns.append("tracked_open_but_broker_position_missing")
    if not tracked_open and strategy_orders:
        concerns.append("strategy_orders_resting_while_local_state_closed")
    if tracked_open and matching:
        try:
            q_broker = float(matching.get("qty") or 0)
            q_tracked = float(state.get("qty") or 0)
            if abs(q_broker - q_tracked) > 0.000001:
                concerns.append("tracked_vs_broker_quantity_mismatch")
        except (TypeError, ValueError):
            concerns.append("quantity_comparison_not_possible")
    if stop_id and not stop_status:
        concerns.append("protective_stop_final_status_unverified")
    if not tracked_open and stop_status and stop_status not in TERMINAL:
        concerns.append("closed_state_but_protective_order_not_terminal")
    if tracked_open and not stop_id:
        concerns.append("open_position_without_tracked_protective_id")
    return {
        "paper_only": True,
        "tracked_status": state.get("status") or "missing",
        "tracked_symbol": tracked_symbol,
        "tracked_signal_date": state.get("signal_date"),
        "tracked_qty": state.get("qty"),
        "broker_tracked_symbol_qty": matching.get("qty") if matching else None,
        # Avoid exposing unrelated paper-account holdings in public Actions logs.
        "unrelated_broker_position_count": len(positions) - int(bool(matching)),
        "open_strategy_orders": [
            {"symbol": o.get("symbol"), "side": o.get("side"),
             "order_type": o.get("order_type"), "status": o.get("status")}
            for o in strategy_orders
        ],
        "tracked_stop_order_id": stop_id,
        "tracked_stop_actual_status": stop_status or None,
        "tracked_stop_status_error_type": stop_error,
        "concerns": concerns,
        "reconciled": not concerns,
    }


def main() -> int:
    broker = AlpacaPaperBroker.from_env()
    state = load_open_position_state() or {}
    result = reconcile(broker, state)
    folder = Path("audit_outputs")
    folder.mkdir(exist_ok=True)
    (folder / "broker_reconciliation.json").write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print("BROKER_PAPER_RECONCILIATION=" + json.dumps(result, sort_keys=True))
    if result["concerns"]:
        for issue in result["concerns"]:
            print("::warning::Paper-account reconciliation concern: " + issue)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
