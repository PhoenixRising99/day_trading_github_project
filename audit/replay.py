"""Read-only, matched-data exit replay for the Alpaca paper trade ledger.

Uses historical *consolidated SIP* 5-minute bars, not the original yfinance
snapshots. This is a counterfactual diagnostic and must NOT be used to infer
actual broker fills or to place orders. It does not import the trading client.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

import pandas as pd

from daytrading.indicators import add_indicators

ET = ZoneInfo("America/New_York")
FRICTION_BPS = 5  # Explicit scenario assumption, not measured trading slippage.
COLUMNS = (
    "signal_date", "symbol", "opened_at_et", "closed_at_et", "qty",
    "entry_preview", "entry_fill_price", "stop_loss_preview",
    "take_profit_preview", "exit_fill_price", "exit_reason",
    "realized_pnl", "order_status",
)


def load_ledger(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = set(COLUMNS).difference(df.columns)
    if missing:
        raise ValueError(f"Trade ledger is missing columns: {sorted(missing)}")
    df["realized_pnl"] = pd.to_numeric(df["realized_pnl"], errors="coerce")
    df = df.loc[df["realized_pnl"].notna() & df["order_status"].eq("filled")].copy()
    df = df.sort_values(["signal_date", "opened_at_et"]).reset_index(drop=True)
    return df


def summarize_ledger(ledger: pd.DataFrame) -> dict:
    profits = ledger["realized_pnl"].astype(float)
    winners = profits[profits > 0]
    losers = profits[profits < 0]
    gross_win = float(winners.sum())
    gross_loss = float(-losers.sum())
    return {
        "completed_trades": int(len(ledger)),
        "winners": int(len(winners)),
        "losers": int(len(losers)),
        "realized_pnl": round(float(profits.sum()), 6),
        "average_pnl": round(float(profits.mean()), 6) if len(profits) else None,
        "win_rate": round(float(len(winners) / len(profits)), 5) if len(profits) else None,
        "profit_factor": round(gross_win / gross_loss, 5) if gross_loss else None,
        "by_exit": [
            {
                "reason": str(reason),
                "trades": int(len(group)),
                "wins": int((group["realized_pnl"] > 0).sum()),
                "pnl": round(float(group["realized_pnl"].sum()), 6),
            }
            for reason, group in ledger.groupby("exit_reason", dropna=False)
        ],
    }


def fetch_session_bars(client, symbol: str, signal_date: date, feed) -> pd.DataFrame:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    beginning = datetime.combine(signal_date - timedelta(days=14), time(9, 30), tzinfo=ET)
    ending = datetime.combine(signal_date, time(16, 1), tzinfo=ET)
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=beginning,
        end=ending,
        feed=feed,
        adjustment="raw",
    )
    data = client.get_stock_bars(request).df
    if data.empty:
        raise ValueError("No historical bars returned")
    data = data.reset_index().rename(columns={"index": "timestamp"})
    for name in ("timestamp", "open", "high", "low", "close", "volume"):
        if name not in data.columns:
            raise ValueError(f"Market data is missing {name}")
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True).dt.tz_convert(ET)
    # Regular trading hours only: match the strategy's use of prepost=False.
    clock = data["timestamp"].dt.time
    data = data.loc[(clock >= time(9, 30)) & (clock <= time(15, 55))].copy()
    data["symbol"] = symbol
    if data.empty:
        raise ValueError("No regular-session bars")
    return add_indicators(data.sort_values("timestamp").reset_index(drop=True))


def replay_trade(trade: dict, bars: pd.DataFrame, *, vwap_bars: int = 2,
                 ema_bars: int = 1, friction_bps: float = FRICTION_BPS) -> dict:
    """Decide using completed candles; model fills at following bar's open.

    The broker protective stop is approximated as a stop-price fill, with
    conservative gap-through pricing. Bar OHLC cannot resolve intrabar ordering:
    stop wins over target when both are touched in one bar.
    """
    day = date.fromisoformat(str(trade["signal_date"]))
    session = bars.loc[bars["timestamp"].dt.date.eq(day)].copy()
    session = session.sort_values("timestamp").reset_index(drop=True)
    if len(session) < 72:
        raise ValueError(f"Incomplete session: only {len(session)} bars")
    signal_time = pd.Timestamp(trade["opened_at_et"]).tz_convert(ET)
    start = signal_time.floor("5min")
    eligible = session.index[session["timestamp"] > start].tolist()
    if not eligible:
        raise ValueError("Entry has no subsequent complete candles")

    # Recenter stop and target distances around actual Alpaca entry fill.
    entry = float(trade["entry_fill_price"])
    preview = float(trade["entry_preview"])
    stop = entry - (preview - float(trade["stop_loss_preview"]))
    target = entry + (float(trade["take_profit_preview"]) - preview)
    qty = float(trade["qty"])
    if not (entry > 0 and qty > 0 and 0 < stop < entry < target):
        raise ValueError("Invalid entry, stop, target, or quantity")

    below = session["close"] < session["vwap"]
    ema_fail = session["ema_9"] < session["ema_20"]
    below_confirmed = below.copy()
    ema_confirmed = ema_fail.copy()
    for lag in range(1, vwap_bars):
        below_confirmed &= below.shift(lag, fill_value=False)
    for lag in range(1, ema_bars):
        ema_confirmed &= ema_fail.shift(lag, fill_value=False)

    for idx in eligible:
        candle = session.iloc[idx]
        next_candle = session.iloc[idx + 1] if idx + 1 < len(session) else None
        reason = None
        model_fill = None

        if float(candle["low"]) <= stop:
            reason = "stop_loss"
            model_fill = min(stop, float(candle["open"])) * (1 - friction_bps / 10000)
        elif float(candle["high"]) >= target:
            reason = "take_profit"
        elif bool(below_confirmed.iloc[idx]):
            reason = f"lost_vwap_{vwap_bars}bar"
        elif bool(ema_confirmed.iloc[idx]):
            reason = "ema_trend_failure"
        elif candle["timestamp"].time() >= time(15, 50):
            reason = "end_of_day"

        if reason:
            if model_fill is None:
                if next_candle is None:
                    raise ValueError("Exit trigger without following execution bar")
                model_fill = float(next_candle["open"]) * (1 - friction_bps / 10000)
            return {
                "reason": reason,
                "trigger_bar": candle["timestamp"].isoformat(),
                "modeled_exit_price": round(model_fill, 5),
                "modeled_pnl": round((model_fill - entry) * qty, 6),
                "stop": round(stop, 4),
                "target": round(target, 4),
            }
    raise ValueError("Replay did not find an exit")


def evaluate(ledger: pd.DataFrame, client, feed) -> tuple[list[dict], list[dict]]:
    results, errors = [], []
    for _, trade in ledger.iterrows():
        row = trade.to_dict()
        key = {"date": str(row["signal_date"]), "symbol": str(row["symbol"])}
        try:
            bars = fetch_session_bars(client, key["symbol"], date.fromisoformat(key["date"]), feed)
            baseline = replay_trade(row, bars, vwap_bars=2, ema_bars=1)
            vwap3 = replay_trade(row, bars, vwap_bars=3, ema_bars=1)
            ema2 = replay_trade(row, bars, vwap_bars=2, ema_bars=2)
            actual_exit = pd.Timestamp(row["closed_at_et"]).tz_convert(ET)
            trigger = pd.Timestamp(baseline["trigger_bar"])
            time_diff = round(abs((actual_exit - trigger).total_seconds()) / 60, 1)
            results.append({
                **key,
                "actual_reason": str(row["exit_reason"]),
                "actual_pnl": round(float(row["realized_pnl"]), 6),
                "baseline": baseline,
                "vwap3": vwap3,
                "ema2": ema2,
                "baseline_reason_matches": baseline["reason"] == row["exit_reason"],
                "baseline_exit_timing_difference_minutes": time_diff,
                "vwap3_delta": round(vwap3["modeled_pnl"] - baseline["modeled_pnl"], 6),
                "ema2_delta": round(ema2["modeled_pnl"] - baseline["modeled_pnl"], 6),
            })
        except Exception as exc:
            errors.append({**key, "error": str(exc)})
    return results, errors


def summarize_replay(results: list[dict], errors: list[dict]) -> dict:
    matches = [r for r in results if r["baseline_reason_matches"]]
    matched_15 = [
        r for r in matches if r["baseline_exit_timing_difference_minutes"] <= 15
    ]
    return {
        "source": "historical Alpaca SIP 5-minute consolidated feed",
        "modeled_friction_bps_per_exit": FRICTION_BPS,
        "model_limitations": [
            "Historical SIP prices differ from original live yfinance decisions.",
            "Modeled next-bar open is not an Alpaca executed quote or fill.",
            "Protective stop slippage and gaps cannot be reproduced from OHLC bars.",
            "No alternative-entry policy is evaluated; all historical entries are held fixed.",
            "Winner/loser exit comparisons suffer survivor and post-selection bias.",
        ],
        "replayed": len(results),
        "errors": errors,
        "baseline_reason_matches": len(matches),
        "baseline_reason_and_15min_matches": len(matched_15),
        "baseline_modeled_pnl": round(sum(r["baseline"]["modeled_pnl"] for r in results), 6),
        "vwap3_modeled_pnl": round(sum(r["vwap3"]["modeled_pnl"] for r in results), 6),
        "ema2_modeled_pnl": round(sum(r["ema2"]["modeled_pnl"] for r in results), 6),
        "vwap3_vs_baseline_delta": round(sum(r["vwap3_delta"] for r in results), 6),
        "ema2_vs_baseline_delta": round(sum(r["ema2_delta"] for r in results), 6),
        "baseline_fidelity_rate": round(len(matched_15) / len(results), 4) if results else None,
        "replay_sufficient_for_strategy_choice": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Alpaca trade audit")
    parser.add_argument("--ledger", type=Path, default=Path("data/logs/broker/alpaca_paper_exit_log.csv"))
    parser.add_argument("--output", type=Path, default=Path("audit_outputs"))
    parser.add_argument("--no-market", action="store_true", help="Only audit the completed-trade ledger")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    ledger = load_ledger(args.ledger)
    summary = {"ledger": summarize_ledger(ledger), "replay": None}
    if not args.no_market:
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.enums import DataFeed
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("Missing market-data credentials; pass --no-market for an offline ledger audit")
        client = StockHistoricalDataClient(key, secret)
        rows, errors = evaluate(ledger, client, DataFeed.SIP)
        summary["replay"] = summarize_replay(rows, errors)
        (args.output / "replay_trades.json").write_text(
            json.dumps(rows, indent=2, default=str) + "\n", encoding="utf-8"
        )
        if not rows:
            raise RuntimeError(f"No trades could be replayed: {errors[:3]}")
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print("AUDIT_SUMMARY_JSON=" + json.dumps(summary, separators=(",", ":"), default=str))


if __name__ == "__main__":
    main()
