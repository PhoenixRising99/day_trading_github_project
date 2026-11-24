"""Read-only shadow policy comparison. No broker order calls.

Daily prospective research protocol: reconstruct the just-completed session
from yfinance OHLCV (same source as production, not the original snapshots).
All four policies evaluate identical completed bars and use identical modeled
fills. This is a daily retrospective simulation, NOT actual execution or
point-in-time observation; live decision snapshots are captured separately.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from audit.exchange_calendar import completed_sessions
from daytrading.config import CONFIG, STRATEGY_PARAMS, WATCHLIST
from daytrading.data_fetch import fetch_intraday_data
from daytrading.indicators import add_indicators
from daytrading.strategy import (
    calculate_position_size, calculate_stop_and_target, evaluate_long_setup,
    is_entry_window, momentum_exit_signal,
)

ET = ZoneInfo(CONFIG.timezone)
POLICIES = {
    "A_control": (12, False),
    "B_volume_confirmation": (12, True),
    "C_min_score_13": (13, False),
    "D_min_score_14": (14, False),
}


def run_session(frame: pd.DataFrame, session_date: date, close: datetime) -> dict:
    """One independent simulated trade per policy, same bar data and friction."""
    cutoff = pd.Timestamp(close).tz_convert(ET) - pd.Timedelta(minutes=5)
    session = frame[frame["timestamp"].dt.date.eq(session_date)].copy()
    session = session.sort_values(["timestamp", "symbol"])
    # Match actual scheduler: wall-clock scans at 10:15–10:55 ET see the
    # preceding 5-minute bar. The original strategy rejects 10:10 as outside
    # its candle-time entry window. Do not silently extend the control window.
    stamps = pd.date_range(
        pd.Timestamp(datetime.combine(session_date, time(10, 10), ET)),
        pd.Timestamp(datetime.combine(session_date, time(10, 50), ET)),
        freq="5min",
    )
    rows = {ts: group for ts, group in session.groupby("timestamp")}
    # Refuse to label incomplete data a no-signal day.
    required = pd.date_range(
        pd.Timestamp(datetime.combine(session_date, time(9, 30), ET)),
        cutoff, freq="5min",
    )
    missing = []
    for ts in required:
        group = rows.get(ts)
        available = set() if group is None else set(group.loc[group["volume"] > 0, "symbol"])
        absent = sorted(set(WATCHLIST) - available)
        if absent:
            missing.append({"bar": ts.isoformat(), "symbols": absent})
    if missing:
        return {"date": session_date.isoformat(), "status": "incomplete_market_data",
                "missing_bars": missing, "policies": {}}
    results = {}
    for name, (min_score, require_volume) in POLICIES.items():
        selected = None
        for ts in stamps:
            if not is_entry_window(ts):
                continue
            candidates = []
            for _, row in rows[ts].iterrows():
                setup = evaluate_long_setup(row, STRATEGY_PARAMS)
                if not setup["raw_signal"] or setup["score"] < min_score:
                    continue
                if require_volume and "volume_confirmation" not in setup["passed"]:
                    continue
                candidates.append((row, setup))
            if not candidates:
                continue
            candidates.sort(key=lambda item: (-item[1]["score"],
                                               -round(float(item[0]["volume_ratio"]), 2),
                                               str(item[0]["symbol"])))
            row, setup = candidates[0]
            symbol = str(row["symbol"])
            execution_time = ts + pd.Timedelta(minutes=5)
            execution = rows[execution_time].loc[rows[execution_time]["symbol"].eq(symbol)].iloc[0]
            # Signal bar is complete; next bar open plus 5 bps is the modeled
            # entry, with the original preview stop/target distances preserved.
            preview = float(row["close"]) * (1 + CONFIG.slippage_pct)
            stop_preview, target_preview = calculate_stop_and_target(row, preview, CONFIG)
            qty = calculate_position_size(CONFIG.account_size, preview, stop_preview, CONFIG)
            if qty <= 0:
                continue
            entry = float(execution["open"]) * (1 + CONFIG.slippage_pct)
            stop = entry - (preview - stop_preview)
            target = entry + (target_preview - preview)
            selected = {"symbol": symbol, "setup_score": setup["score"],
                        "volume_confirmed": "volume_confirmation" in setup["passed"],
                        "signal_bar": ts.isoformat(), "entry_at": execution_time.isoformat(),
                        "entry_price": entry, "qty": qty, "stop": stop, "target": target}
            break
        if selected is None:
            results[name] = {"status": "no_signal", "modeled_pnl": 0.0}
            continue

        symbol = selected["symbol"]
        entry_time = pd.Timestamp(selected["entry_at"])
        exit_price, reason, exit_time = None, None, None
        bars = session.loc[session["symbol"].eq(symbol)].sort_values("timestamp")
        for _, candle in bars.loc[bars["timestamp"].ge(entry_time)].iterrows():
            ts = candle["timestamp"]
            if ts >= cutoff:
                # At the actual strategy's cutoff, execute at that bar's open.
                reason, exit_price, exit_time = "end_of_day", float(candle["open"]), ts
                break
            if float(candle["low"]) <= selected["stop"]:
                reason, exit_price, exit_time = (
                    "modeled_protective_stop", min(float(candle["open"]), selected["stop"]), ts
                )
                break
            if float(candle["high"]) >= selected["target"]:
                reason = "take_profit"
            else:
                exit_now, exit_reason = momentum_exit_signal(candle)
                reason = exit_reason if exit_now else None
            if reason:
                following = bars.loc[bars["timestamp"].eq(ts + pd.Timedelta(minutes=5))]
                if following.empty:
                    return {"date": session_date.isoformat(), "status": "incomplete_exit_data",
                            "policies": {}}
                exit_price = float(following.iloc[0]["open"])
                exit_time = following.iloc[0]["timestamp"]
                break
        if exit_price is None:
            return {"date": session_date.isoformat(), "status": "incomplete_exit_data", "policies": {}}
        exit_price *= 1 - CONFIG.slippage_pct
        results[name] = {"status": "simulated_trade", **selected,
                         "exit_reason": reason, "exit_at": exit_time.isoformat(),
                         "exit_price": round(exit_price, 5),
                         "modeled_pnl": round((exit_price - selected["entry_price"])
                                              * selected["qty"], 6)}
    return {"date": session_date.isoformat(), "status": "complete", "policies": results}


def summarize(days: list[dict]) -> dict:
    complete = [d for d in days if d["status"] == "complete"]
    summaries = {}
    for name in POLICIES:
        trades = [d["policies"][name] for d in complete]
        summaries[name] = {
            "simulated_trades": sum(t["status"] == "simulated_trade" for t in trades),
            "no_signal_days": sum(t["status"] == "no_signal" for t in trades),
            "modeled_pnl": round(sum(t["modeled_pnl"] for t in trades), 6),
        }
    return {"complete_sessions": len(complete), "incomplete_sessions": len(days)-len(complete),
            "policies": summaries, "model": "retrospective yfinance five-minute bars; "
            "5 bps each way; fixed one trade per policy per day; no orders",
            "strategy_selection_authorized": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 11, 24))
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(ET).date())
    parser.add_argument("--output", type=Path, default=Path("audit_outputs/shadow.json"))
    args = parser.parse_args()
    # Keep the comparison window fixed and recover recently missed scheduled
    # runs. yfinance's intraday history is limited; never claim missing dates
    # were evaluated. The workflow runs weekly as well as daily.
    sessions = completed_sessions(args.start, args.end)
    days = []
    if sessions:
        raw = fetch_intraday_data(WATCHLIST, period="60d", interval=CONFIG.interval,
                                  timezone=CONFIG.timezone)
        available_days = sorted(raw["timestamp"].dt.date.unique())
        for day, close in sessions.items():
            d = date.fromisoformat(day)
            warmup = [x for x in available_days if x <= d][-5:]
            if len(warmup) < 5 or d not in warmup:
                days.append({"date": day, "status": "missing_warmup_or_session", "policies": {}})
                continue
            frame = add_indicators(raw[raw["timestamp"].dt.date.isin(warmup)].copy())
            days.append(run_session(frame, d, datetime.fromisoformat(close)))
    report = {"summary": summarize(days), "sessions": days}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print("SHADOW_RESEARCH=" + json.dumps(report["summary"]), flush=True)
    if any(d["status"] != "complete" for d in days):
        print("::warning::Some shadow sessions lack complete data; excluded from comparison.")


if __name__ == "__main__":
    main()
