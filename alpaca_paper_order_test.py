from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from daytrading.broker import AlpacaPaperBroker


def logs_dir() -> Path:
    path = Path(__file__).resolve().parent / "data" / "logs" / "broker"
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual Alpaca paper order smoke test.")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--qty", type=float, default=1.0)
    parser.add_argument("--limit-price", type=float, default=1.00)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    timestamp_tag = pd.Timestamp.now(tz="America/New_York").strftime("%Y%m%d_%H%M%S")

    broker = AlpacaPaperBroker.from_env()
    result = broker.submit_cancel_limit_order_test(
        symbol=args.symbol,
        qty=args.qty,
        limit_price=args.limit_price,
        confirm=args.confirm,
    )

    output_path = logs_dir() / f"alpaca_paper_order_test_{timestamp_tag}.json"
    output_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    print("Alpaca paper order smoke test completed.")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
