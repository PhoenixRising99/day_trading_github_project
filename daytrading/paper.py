from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from daytrading.config import CONFIG, STRATEGY_PARAMS, WATCHLIST, StrategyParams, TradingConfig
from daytrading.data_fetch import fetch_intraday_data
from daytrading.indicators import add_indicators
from daytrading.strategy import (
    calculate_position_size,
    calculate_stop_and_target,
    evaluate_long_setup,
    is_entry_window,
)

PAPER_JOURNAL_COLUMNS = [
    "scan_timestamp",
    "data_timestamp",
    "symbol",
    "signal",
    "raw_signal_before_time_filter",
    "entry_window_open",
    "setup_score",
    "setup_max_score",
    "reason",
    "entry_preview",
    "shares_preview",
    "position_value_preview",
    "stop_loss_preview",
    "take_profit_preview",
    "risk_dollars_preview",
    "last_price",
    "rsi_14",
    "volume_ratio",
    "vwap",
    "vwap_distance_atr",
    "market_filter_ok",
    "manual_decision",
    "paper_entry_time",
    "paper_entry_price",
    "paper_exit_time",
    "paper_exit_price",
    "paper_exit_reason",
    "paper_pnl",
    "notes",
]


def _interval_to_timedelta(interval: str) -> pd.Timedelta:
    interval = str(interval).strip().lower()
    if interval.endswith("m"):
        return pd.Timedelta(minutes=int(interval[:-1]))
    if interval.endswith("h"):
        return pd.Timedelta(hours=int(interval[:-1]))
    if interval.endswith("d"):
        return pd.Timedelta(days=int(interval[:-1]))
    raise ValueError(f"Unsupported interval for paper scan timing: {interval}")


def _latest_completed_rows(
    data: pd.DataFrame,
    config: TradingConfig,
    min_volume: float = 1,
) -> pd.DataFrame:
    """
    Select the latest completed candle for each symbol.

    Why this exists:
    yfinance often exposes the just-opened/current 5-minute candle with zero
    volume or a zero high/low range. Evaluating that row creates false
    `missing_indicator` rejections because close_location/volume-derived
    indicators can be invalid.

    For 5-minute data at 10:20:02, the current candle is timestamped 10:20.
    The latest completed candle is usually 10:15, so this function filters out
    bars with timestamp >= current 5-minute floor.
    """
    if data.empty:
        return data

    now_et = pd.Timestamp.now(tz=config.timezone)
    interval_td = _interval_to_timedelta(config.interval)
    floor_freq = f"{int(interval_td.total_seconds() // 60)}min"
    current_bar_start = now_et.floor(floor_freq)

    candidates = data[data["timestamp"] < current_bar_start].copy()

    # Avoid zero-volume placeholder bars when the free data source exposes them.
    if "volume" in candidates.columns:
        candidates = candidates[candidates["volume"] >= min_volume].copy()

    if candidates.empty:
        # Return the latest available rows so the CSV still explains what happened.
        return (
            data.sort_values(["symbol", "timestamp"])
            .groupby("symbol", as_index=False)
            .tail(1)
            .copy()
        )

    return (
        candidates.sort_values(["symbol", "timestamp"])
        .groupby("symbol", as_index=False)
        .tail(1)
        .copy()
    )


def paper_scan(
    symbols: Iterable[str] = WATCHLIST,
    config: TradingConfig = CONFIG,
    period: str = "5d",
    strategy_params: StrategyParams = STRATEGY_PARAMS,
) -> pd.DataFrame:
    data = fetch_intraday_data(
        symbols,
        period=period,
        interval=config.interval,
        timezone=config.timezone,
    )
    data = add_indicators(data)

    latest_rows = _latest_completed_rows(data, config).copy()

    previews = []
    scan_timestamp = pd.Timestamp.now(tz=config.timezone)

    for _, row in latest_rows.iterrows():
        setup = evaluate_long_setup(row, strategy_params)
        raw_signal = setup["raw_signal"]
        entry_window_open = is_entry_window(row["timestamp"])
        signal = raw_signal and entry_window_open

        if raw_signal and not entry_window_open:
            reason = "blocked_outside_entry_window"
        else:
            reason = setup["reason"]

        entry_price = row["close"] * (1 + config.slippage_pct)

        if signal:
            stop_loss, take_profit = calculate_stop_and_target(row, entry_price, config)
            shares = calculate_position_size(config.account_size, entry_price, stop_loss, config)
            position_value = shares * entry_price
            risk_dollars = shares * (entry_price - stop_loss)
        else:
            stop_loss = np.nan
            take_profit = np.nan
            shares = 0
            position_value = 0
            risk_dollars = 0

        previews.append(
            {
                "scan_timestamp": scan_timestamp,
                "data_timestamp": row["timestamp"],
                "symbol": row["symbol"],
                "last_price": round(row["close"], 4),
                "signal": bool(signal),
                "raw_signal_before_time_filter": bool(raw_signal),
                "entry_window_open": bool(entry_window_open),
                "setup_score": setup["score"],
                "setup_max_score": setup["max_score"],
                "reason": reason,
                "entry_preview": round(entry_price, 4) if signal else np.nan,
                "shares_preview": shares,
                "position_value_preview": round(position_value, 2),
                "stop_loss_preview": stop_loss,
                "take_profit_preview": take_profit,
                "risk_dollars_preview": round(risk_dollars, 2),
                "rsi_14": round(row["rsi_14"], 2) if not pd.isna(row["rsi_14"]) else np.nan,
                "volume_ratio": round(row["volume_ratio"], 2)
                if not pd.isna(row["volume_ratio"])
                else np.nan,
                "vwap": round(row["vwap"], 4) if not pd.isna(row["vwap"]) else np.nan,
                "vwap_distance_atr": round(row["vwap_distance_atr"], 2)
                if not pd.isna(row["vwap_distance_atr"])
                else np.nan,
                "market_filter_ok": bool(row["market_filter_ok"])
                if not pd.isna(row.get("market_filter_ok"))
                else False,
            }
        )

    return pd.DataFrame(previews).sort_values(
        ["signal", "setup_score", "volume_ratio"], ascending=[False, False, False]
    )


def initialize_paper_journal(journal_path: Path, overwrite: bool = False) -> Path:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not journal_path.exists():
        pd.DataFrame(columns=PAPER_JOURNAL_COLUMNS).to_csv(journal_path, index=False)
    return journal_path


def append_active_paper_signals_to_journal(
    paper_df: pd.DataFrame,
    journal_path: Path,
    notes: str = "",
) -> int:
    """Append active paper signals and return number of appended rows."""
    initialize_paper_journal(journal_path, overwrite=False)

    active = paper_df[paper_df["signal"] == True].copy()  # noqa: E712
    if active.empty:
        return 0

    active["manual_decision"] = "pending_review"
    active["paper_entry_time"] = ""
    active["paper_entry_price"] = ""
    active["paper_exit_time"] = ""
    active["paper_exit_price"] = ""
    active["paper_exit_reason"] = ""
    active["paper_pnl"] = ""
    active["notes"] = notes

    for col in PAPER_JOURNAL_COLUMNS:
        if col not in active.columns:
            active[col] = ""

    active = active[PAPER_JOURNAL_COLUMNS]
    existing = pd.read_csv(journal_path)

    # Avoid duplicate journal rows for the same latest bar/symbol/setup.
    if not existing.empty:
        existing_keys = set(
            zip(
                existing.get("data_timestamp", pd.Series(dtype=str)).astype(str),
                existing.get("symbol", pd.Series(dtype=str)).astype(str),
                existing.get("setup_score", pd.Series(dtype=str)).astype(str),
            )
        )
        active_keys = list(
            zip(
                active["data_timestamp"].astype(str),
                active["symbol"].astype(str),
                active["setup_score"].astype(str),
            )
        )
        active = active[[key not in existing_keys for key in active_keys]]

    if active.empty:
        return 0

    combined = pd.concat([existing, active], ignore_index=True)
    combined.to_csv(journal_path, index=False)
    return len(active)


def paper_trading_status_report(journal_path: Path) -> pd.DataFrame:
    if not journal_path.exists():
        return pd.DataFrame(
            [{"journal_exists": False, "rows": 0, "completed_trades": 0, "total_paper_pnl": 0.0}]
        )

    journal = pd.read_csv(journal_path)
    if journal.empty:
        return pd.DataFrame(
            [{"journal_exists": True, "rows": 0, "completed_trades": 0, "total_paper_pnl": 0.0}]
        )

    pnl = pd.to_numeric(journal.get("paper_pnl", pd.Series(dtype=float)), errors="coerce")
    completed = pnl.notna().sum()
    return pd.DataFrame(
        [
            {
                "journal_exists": True,
                "rows": len(journal),
                "completed_trades": int(completed),
                "winning_paper_trades": int((pnl > 0).sum()),
                "losing_paper_trades": int((pnl <= 0).sum()),
                "paper_win_rate": float((pnl > 0).sum() / completed) if completed else np.nan,
                "total_paper_pnl": float(pnl.sum(skipna=True)) if completed else 0.0,
                "avg_paper_pnl": float(pnl.mean(skipna=True)) if completed else np.nan,
            }
        ]
    )
