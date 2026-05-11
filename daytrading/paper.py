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
    momentum_exit_signal,
)

PAPER_JOURNAL_COLUMNS = [
    "scan_timestamp",
    "data_timestamp",
    "signal_date",
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
    "trade_status",
    "manual_decision",
    "paper_entry_time",
    "paper_entry_price",
    "paper_exit_time",
    "paper_exit_price",
    "paper_exit_reason",
    "paper_pnl",
    "paper_return_pct",
    "notes",
]

EXIT_UPDATE_COLUMNS = [
    "symbol",
    "signal_date",
    "paper_entry_time",
    "paper_entry_price",
    "paper_exit_time",
    "paper_exit_price",
    "paper_exit_reason",
    "shares_preview",
    "paper_pnl",
    "paper_return_pct",
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


def _market_timestamp(value, timezone: str = CONFIG.timezone) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize(timezone)
    return ts.tz_convert(timezone)


def _signal_date(value, timezone: str = CONFIG.timezone) -> str:
    if pd.isna(value) or str(value).strip() == "":
        return ""
    return str(_market_timestamp(value, timezone).date())


def _latest_completed_rows(
    data: pd.DataFrame,
    config: TradingConfig,
    min_volume: float = 1,
) -> pd.DataFrame:
    """
    Select the latest completed candle for each symbol.

    yfinance can expose the just-opened/current 5-minute candle with zero
    volume or incomplete high/low data. Evaluating that row can cause false
    `missing_indicator` rejections.

    Example:
    - Scan runs at 10:20:02 ET
    - Current candle is timestamped 10:20
    - Latest completed candle is normally 10:15
    """
    if data.empty:
        return data

    now_et = pd.Timestamp.now(tz=config.timezone)
    interval_td = _interval_to_timedelta(config.interval)
    floor_freq = f"{int(interval_td.total_seconds() // 60)}min"
    current_bar_start = now_et.floor(floor_freq)

    candidates = data[data["timestamp"] < current_bar_start].copy()

    if "volume" in candidates.columns:
        candidates = candidates[candidates["volume"] >= min_volume].copy()

    if candidates.empty:
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


def _normalize_journal(journal: pd.DataFrame) -> pd.DataFrame:
    """
    Backward-compatible journal normalizer.

    Older journal files did not have signal_date, trade_status, or return columns.
    This function adds them without deleting prior data.
    """
    journal = journal.copy()

    for col in PAPER_JOURNAL_COLUMNS:
        if col not in journal.columns:
            journal[col] = ""

    if not journal.empty:
        missing_signal_date = journal["signal_date"].astype(str).str.strip().eq("")
        if missing_signal_date.any() and "data_timestamp" in journal.columns:
            journal.loc[missing_signal_date, "signal_date"] = journal.loc[
                missing_signal_date, "data_timestamp"
            ].map(lambda x: _signal_date(x) if pd.notna(x) and str(x).strip() else "")

        missing_status = journal["trade_status"].astype(str).str.strip().eq("")
        has_exit = journal["paper_exit_time"].astype(str).str.strip().ne("")
        has_entry = journal["paper_entry_time"].astype(str).str.strip().ne("") | journal[
            "paper_entry_price"
        ].astype(str).str.strip().ne("")

        journal.loc[missing_status & has_exit, "trade_status"] = "closed"
        journal.loc[missing_status & has_entry & ~has_exit, "trade_status"] = "open"
        journal.loc[missing_status & ~has_entry & ~has_exit, "trade_status"] = "pending_review"

    return journal[PAPER_JOURNAL_COLUMNS]


def read_paper_journal(journal_path: Path) -> pd.DataFrame:
    if not journal_path.exists():
        return pd.DataFrame(columns=PAPER_JOURNAL_COLUMNS)

    try:
        journal = pd.read_csv(journal_path)
    except pd.errors.EmptyDataError:
        journal = pd.DataFrame(columns=PAPER_JOURNAL_COLUMNS)

    return _normalize_journal(journal)


def save_paper_journal(journal: pd.DataFrame, journal_path: Path) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    _normalize_journal(journal).to_csv(journal_path, index=False)


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
                "signal_date": str(row["timestamp"].date()),
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
    else:
        # Upgrade older journal headers in place.
        save_paper_journal(read_paper_journal(journal_path), journal_path)
    return journal_path


def _has_existing_trade_for_date(journal: pd.DataFrame, signal_date: str) -> bool:
    if journal.empty:
        return False

    statuses_to_count = {"open", "closed", "pending_review"}
    status = journal["trade_status"].astype(str).str.lower().str.strip()
    same_date = journal["signal_date"].astype(str).eq(str(signal_date))
    return bool((same_date & status.isin(statuses_to_count)).any())


def _has_open_trade(journal: pd.DataFrame) -> bool:
    if journal.empty:
        return False
    return bool(journal["trade_status"].astype(str).str.lower().str.strip().eq("open").any())


def append_active_paper_signals_to_journal(
    paper_df: pd.DataFrame,
    journal_path: Path,
    notes: str = "",
    config: TradingConfig = CONFIG,
) -> int:
    """
    Append active paper signals and return number of appended rows.

    V12 behavior:
    - Converts an accepted paper signal into an open simulated paper trade.
    - Respects max_open_positions=1.
    - Respects max_trades_per_day=1 by not adding later same-day signals.
    - If multiple symbols signal in the same scan, only the top-ranked row is used.
    """
    initialize_paper_journal(journal_path, overwrite=False)

    active = paper_df[paper_df["signal"] == True].copy()  # noqa: E712
    if active.empty:
        return 0

    journal = read_paper_journal(journal_path)

    if _has_open_trade(journal):
        return 0

    active = active.sort_values(
        ["setup_score", "volume_ratio", "symbol"], ascending=[False, False, True]
    ).copy()

    rows_to_add = []
    for _, row in active.iterrows():
        signal_date = str(row.get("signal_date") or _signal_date(row.get("data_timestamp")))

        if _has_existing_trade_for_date(journal, signal_date):
            continue

        if len(rows_to_add) >= config.max_trades_per_day:
            break

        entry_price = float(row["entry_preview"])
        shares = float(row["shares_preview"])
        position_value = float(row["position_value_preview"])
        risk_dollars = float(row["risk_dollars_preview"])

        new_row = row.to_dict()
        new_row.update(
            {
                "signal_date": signal_date,
                "trade_status": "open",
                "manual_decision": "auto_paper_entry",
                "paper_entry_time": row["data_timestamp"],
                "paper_entry_price": entry_price,
                "paper_exit_time": "",
                "paper_exit_price": "",
                "paper_exit_reason": "",
                "paper_pnl": "",
                "paper_return_pct": "",
                "notes": notes,
                "shares_preview": shares,
                "position_value_preview": position_value,
                "risk_dollars_preview": risk_dollars,
            }
        )
        rows_to_add.append(new_row)

        # Enforce max one new paper trade per scan/day for current config.
        if len(rows_to_add) >= config.max_trades_per_day:
            break

    if not rows_to_add:
        return 0

    additions = pd.DataFrame(rows_to_add)
    for col in PAPER_JOURNAL_COLUMNS:
        if col not in additions.columns:
            additions[col] = ""

    additions = additions[PAPER_JOURNAL_COLUMNS]
    combined = pd.concat([journal, additions], ignore_index=True)
    save_paper_journal(combined, journal_path)
    return len(additions)


def _is_end_of_day_exit(ts: pd.Timestamp) -> bool:
    t = ts.time()
    return t >= pd.Timestamp("15:55").time()


def _calculate_paper_pnl(
    entry_price: float,
    exit_price: float,
    shares: float,
    config: TradingConfig = CONFIG,
) -> float:
    gross_pnl = (exit_price - entry_price) * shares
    net_pnl = gross_pnl - (2 * config.commission_per_trade)
    return round(float(net_pnl), 6)


def _evaluate_exit_for_trade(
    trade: pd.Series,
    symbol_data: pd.DataFrame,
    config: TradingConfig = CONFIG,
) -> dict | None:
    entry_time = _market_timestamp(trade["paper_entry_time"], config.timezone)
    entry_price = float(trade["paper_entry_price"])
    shares = float(trade["shares_preview"])
    stop_loss = float(trade["stop_loss_preview"])
    take_profit = float(trade["take_profit_preview"])

    rows = symbol_data[symbol_data["timestamp"] > entry_time].copy()
    rows = rows.sort_values("timestamp")

    if rows.empty:
        return None

    for _, row in rows.iterrows():
        exit_price = None
        exit_reason = None

        # Conservative intrabar assumption:
        # if both stop and target are touched in the same 5-minute candle,
        # count the stop first.
        if row["low"] <= stop_loss:
            exit_price = stop_loss * (1 - config.slippage_pct)
            exit_reason = "stop_loss"
        elif row["high"] >= take_profit:
            exit_price = take_profit * (1 - config.slippage_pct)
            exit_reason = "take_profit"
        else:
            exit_now, reason = momentum_exit_signal(row)
            if exit_now:
                exit_price = row["close"] * (1 - config.slippage_pct)
                exit_reason = reason
            elif _is_end_of_day_exit(row["timestamp"]):
                exit_price = row["close"] * (1 - config.slippage_pct)
                exit_reason = "end_of_day"

        if exit_price is not None:
            pnl = _calculate_paper_pnl(entry_price, float(exit_price), shares, config)
            position_value = entry_price * shares
            return_pct = pnl / position_value if position_value else np.nan

            return {
                "paper_exit_time": row["timestamp"],
                "paper_exit_price": round(float(exit_price), 4),
                "paper_exit_reason": exit_reason,
                "paper_pnl": pnl,
                "paper_return_pct": round(float(return_pct), 6)
                if not pd.isna(return_pct)
                else "",
            }

    return None


def update_open_paper_trades(
    journal_path: Path,
    symbols: Iterable[str] = WATCHLIST,
    config: TradingConfig = CONFIG,
    period: str = "5d",
) -> pd.DataFrame:
    """
    Check open simulated paper trades and close them if an exit condition has occurred.

    Exit rules mirror the V9/V10/V11 backtest:
    - stop-loss
    - take-profit
    - two-bar VWAP failure / EMA trend failure
    - end-of-day exit at or after 15:55 ET
    """
    initialize_paper_journal(journal_path, overwrite=False)
    journal = read_paper_journal(journal_path)

    if journal.empty:
        return pd.DataFrame(columns=EXIT_UPDATE_COLUMNS)

    open_mask = journal["trade_status"].astype(str).str.lower().str.strip().eq("open")
    if not open_mask.any():
        return pd.DataFrame(columns=EXIT_UPDATE_COLUMNS)

    open_symbols = sorted(set(journal.loc[open_mask, "symbol"].astype(str)).intersection(set(symbols)))
    if not open_symbols:
        open_symbols = sorted(set(journal.loc[open_mask, "symbol"].astype(str)))

    data = fetch_intraday_data(
        open_symbols,
        period=period,
        interval=config.interval,
        timezone=config.timezone,
    )
    data = add_indicators(data)

    updates = []

    for idx, trade in journal.loc[open_mask].iterrows():
        symbol = str(trade["symbol"])
        symbol_data = data[data["symbol"] == symbol].copy()

        if symbol_data.empty:
            continue

        exit_update = _evaluate_exit_for_trade(trade, symbol_data, config)
        if exit_update is None:
            continue

        for key, value in exit_update.items():
            journal.at[idx, key] = value

        journal.at[idx, "trade_status"] = "closed"
        journal.at[idx, "manual_decision"] = "auto_paper_closed"

        updates.append(
            {
                "symbol": symbol,
                "signal_date": trade.get("signal_date", ""),
                "paper_entry_time": trade.get("paper_entry_time", ""),
                "paper_entry_price": trade.get("paper_entry_price", ""),
                **exit_update,
                "shares_preview": trade.get("shares_preview", ""),
            }
        )

    if updates:
        save_paper_journal(journal, journal_path)
        return pd.DataFrame(updates, columns=EXIT_UPDATE_COLUMNS)

    # Persist any journal header normalization even if no exits happened.
    save_paper_journal(journal, journal_path)
    return pd.DataFrame(columns=EXIT_UPDATE_COLUMNS)


def paper_trading_status_report(journal_path: Path) -> pd.DataFrame:
    if not journal_path.exists():
        return pd.DataFrame(
            [
                {
                    "journal_exists": False,
                    "rows": 0,
                    "open_trades": 0,
                    "closed_trades": 0,
                    "completed_trades": 0,
                    "total_paper_pnl": 0.0,
                }
            ]
        )

    journal = read_paper_journal(journal_path)
    if journal.empty:
        return pd.DataFrame(
            [
                {
                    "journal_exists": True,
                    "rows": 0,
                    "open_trades": 0,
                    "closed_trades": 0,
                    "completed_trades": 0,
                    "total_paper_pnl": 0.0,
                }
            ]
        )

    status = journal["trade_status"].astype(str).str.lower().str.strip()
    pnl = pd.to_numeric(journal.get("paper_pnl", pd.Series(dtype=float)), errors="coerce")
    completed = pnl.notna().sum()

    return pd.DataFrame(
        [
            {
                "journal_exists": True,
                "rows": len(journal),
                "open_trades": int(status.eq("open").sum()),
                "closed_trades": int(status.eq("closed").sum()),
                "completed_trades": int(completed),
                "winning_paper_trades": int((pnl > 0).sum()),
                "losing_paper_trades": int((pnl <= 0).sum()),
                "paper_win_rate": float((pnl > 0).sum() / completed) if completed else np.nan,
                "total_paper_pnl": float(pnl.sum(skipna=True)) if completed else 0.0,
                "avg_paper_pnl": float(pnl.mean(skipna=True)) if completed else np.nan,
            }
        ]
    )
