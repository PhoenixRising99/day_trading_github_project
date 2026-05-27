from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from daytrading.broker import AlpacaPaperBroker
from daytrading.config import CONFIG, STRATEGY_PARAMS, WATCHLIST
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


def _to_bool(value) -> bool:
    return str(value).lower().strip() in {"true", "1", "yes", "y"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manual Alpaca paper strategy entry submission. Paper only."
    )
    parser.add_argument("--period", default=CONFIG.period)
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--allow-outside-entry-window",
        action="store_true",
        help="For testing only. Default requires the current ET time to be inside the entry window.",
    )
    args = parser.parse_args()

    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")

    if not args.allow_outside_entry_window and not is_entry_window_now(CONFIG.timezone):
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "outside_entry_window",
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        }
        path = logs_dir() / f"alpaca_paper_strategy_entry_{timestamp_tag}.json"
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0

    scan = paper_scan(WATCHLIST, CONFIG, period=args.period, strategy_params=STRATEGY_PARAMS)
    scan_path = scans_dir() / f"alpaca_strategy_signal_preview_{timestamp_tag}.csv"
    scan.to_csv(scan_path, index=False)

    active = scan[scan["signal"] == True].copy()  # noqa: E712
    if active.empty:
        result = {
            "paper_only": True,
            "submitted": False,
            "reason": "no_active_signal",
            "scan_path": str(scan_path),
        }
        path = logs_dir() / f"alpaca_paper_strategy_entry_{timestamp_tag}.json"
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0

    active = active.sort_values(["setup_score", "volume_ratio", "symbol"], ascending=[False, False, True])
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
        }
        path = logs_dir() / f"alpaca_paper_strategy_entry_{timestamp_tag}.json"
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0

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
        }
        path = logs_dir() / f"alpaca_paper_strategy_entry_{timestamp_tag}.json"
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0

    client_order_id = f"paper-strategy-{signal['symbol']}-{timestamp_tag}"

    order_result = broker.submit_market_bracket_buy(
        symbol=str(signal["symbol"]),
        qty=qty,
        take_profit_price=float(signal["take_profit_preview"]),
        stop_loss_price=float(signal["stop_loss_preview"]),
        confirm=args.confirm,
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

    path = logs_dir() / f"alpaca_paper_strategy_entry_{timestamp_tag}.json"
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
