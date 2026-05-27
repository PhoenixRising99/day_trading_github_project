from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from daytrading.broker import AlpacaPaperBroker


def logs_dir() -> Path:
    path = Path(__file__).resolve().parent / "data" / "logs" / "broker"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_csv(name: str, rows: list[dict] | dict, timestamp_tag: str) -> Path:
    path = logs_dir() / f"{name}_{timestamp_tag}.csv"
    df = pd.DataFrame([rows]) if isinstance(rows, dict) else pd.DataFrame(rows)
    df.to_csv(path, index=False)
    return path


def main() -> int:
    timestamp_tag = pd.Timestamp.now(tz="America/New_York").strftime("%Y%m%d_%H%M%S")
    broker = AlpacaPaperBroker.from_env()

    account = broker.account_snapshot()
    positions = broker.open_positions()
    orders = broker.open_orders()
    safety = broker.safety_snapshot(research_account_size=120.0)

    account_path = write_csv("alpaca_account_status", account, timestamp_tag)
    positions_path = write_csv("alpaca_open_positions", positions, timestamp_tag)
    orders_path = write_csv("alpaca_open_orders", orders, timestamp_tag)
    safety_path = write_csv("alpaca_safety_status", safety, timestamp_tag)

    summary = {
        "timestamp_et": pd.Timestamp.now(tz="America/New_York").isoformat(),
        "paper_connection_ok": True,
        "account_status": account.get("status", ""),
        "trading_blocked": account.get("trading_blocked", ""),
        "account_blocked": account.get("account_blocked", ""),
        "positions_count": len(positions),
        "open_orders_count": len(orders),
        "order_submission_enabled": False,
        "live_trading_enabled": False,
        "files": {
            "account": str(account_path),
            "positions": str(positions_path),
            "orders": str(orders_path),
            "safety": str(safety_path),
        },
    }

    summary_path = logs_dir() / f"alpaca_connection_summary_{timestamp_tag}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("Alpaca paper connection check completed.")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
