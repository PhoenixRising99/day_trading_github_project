from __future__ import annotations

import math
from datetime import time as dtime
from typing import Any

import numpy as np
import pandas as pd

from daytrading.config import (
    CONFIG,
    ENTRY_END_HOUR,
    ENTRY_END_MINUTE,
    ENTRY_START_HOUR,
    ENTRY_START_MINUTE,
    STRATEGY_PARAMS,
    StrategyParams,
    TradingConfig,
)


def is_regular_market_time(ts: pd.Timestamp) -> bool:
    t = ts.time()
    return dtime(9, 30) <= t <= dtime(16, 0)


def is_entry_window(ts: pd.Timestamp) -> bool:
    """Frozen V9/V10/V11 entry window: 10:15 AM to 10:59 AM ET."""
    t = ts.time()
    return dtime(ENTRY_START_HOUR, ENTRY_START_MINUTE) <= t <= dtime(
        ENTRY_END_HOUR, ENTRY_END_MINUTE
    )


def is_entry_window_now(timezone: str = CONFIG.timezone) -> bool:
    now = pd.Timestamp.now(tz=timezone)
    return is_entry_window(now)


def _missing_required(row: pd.Series, columns: list[str]) -> bool:
    return any(pd.isna(row.get(c)) for c in columns)


def evaluate_long_setup(
    row: pd.Series,
    params: StrategyParams = STRATEGY_PARAMS,
) -> dict[str, Any]:
    """
    Frozen V9/V10/V11 long setup.

    This is a VWAP-hold continuation setup, not a hard 20-bar breakout setup.
    It is research/paper-observation only.
    """
    required_columns = [
        "vwap",
        "ema_9",
        "ema_20",
        "rsi_14",
        "macd_hist",
        "macd_hist_slope",
        "volume_ratio",
        "resistance_20",
        "atr_14",
        "close_location",
        "vwap_distance_atr",
        "market_filter_ok",
        "above_vwap_3bar",
        "vwap_slope_3",
    ]

    if _missing_required(row, required_columns):
        return {
            "raw_signal": False,
            "score": 0,
            "max_score": 14,
            "passed": [],
            "failed": ["missing_indicator"],
            "reason": "missing_indicator",
        }

    checks = {
        "above_vwap": row["close"] > row["vwap"],
        "vwap_hold_3bar": bool(row["above_vwap_3bar"]) if params.require_vwap_hold else True,
        "vwap_slope_up": row["vwap_slope_3"] > 0 if params.require_vwap_slope else True,
        "vwap_distance_min": row["vwap_distance_atr"] >= params.min_vwap_distance_atr,
        "ema_trend": row["ema_9"] > row["ema_20"],
        "rsi_tradeable": params.rsi_min <= row["rsi_14"] <= params.rsi_max,
        "not_overextended": row["vwap_distance_atr"] <= params.max_vwap_distance_atr,
        "market_filter_ok": bool(row["market_filter_ok"]) if params.require_market_filter else True,
        "breakout": row["close"] > row["resistance_20"] * (1 + params.breakout_buffer),
        "ema_separation": ((row["ema_9"] - row["ema_20"]) / row["close"]) >= 0.0002,
        "macd_positive": row["macd_hist"] > 0 if params.require_macd_positive else True,
        "macd_improving": row["macd_hist_slope"] > 0 if params.require_macd_improving else True,
        "volume_confirmation": row["volume_ratio"] >= params.min_volume_ratio,
        "strong_close": row["close_location"] >= params.min_close_location,
    }

    core = [
        "above_vwap",
        "vwap_hold_3bar",
        "vwap_slope_up",
        "vwap_distance_min",
        "ema_trend",
        "rsi_tradeable",
        "not_overextended",
        "market_filter_ok",
    ]

    if params.require_breakout:
        core.append("breakout")

    passed = [name for name, ok in checks.items() if ok]
    failed = [name for name, ok in checks.items() if not ok]
    score = len(passed)
    core_ok = all(checks[c] for c in core)
    raw_signal = core_ok and score >= params.min_score

    return {
        "raw_signal": raw_signal,
        "score": score,
        "max_score": len(checks),
        "passed": passed,
        "failed": failed,
        "reason": "long_vwap_hold_morning_quality_v9" if raw_signal else "failed_" + ",".join(failed),
    }


def momentum_exit_signal(row: pd.Series) -> tuple[bool, str]:
    """V9/V10/V11 paper-exit logic for later manual review/backtesting."""
    if pd.isna(row.get("vwap")) or pd.isna(row.get("ema_9")) or pd.isna(row.get("ema_20")):
        return False, ""

    if bool(row.get("below_vwap_2bar", False)):
        return True, "lost_vwap_2bar"

    if row["ema_9"] < row["ema_20"]:
        return True, "ema_trend_failure"

    return False, ""


def calculate_stop_and_target(
    row: pd.Series,
    entry_price: float,
    config: TradingConfig = CONFIG,
) -> tuple[float, float]:
    atr = row["atr_14"] if not pd.isna(row["atr_14"]) else entry_price * 0.01

    min_stop_distance = entry_price * 0.010
    max_stop_distance = entry_price * 0.020
    atr_stop_distance = 1.0 * atr

    if not pd.isna(row.get("support_20")) and row["support_20"] < entry_price:
        support_stop_distance = entry_price - (row["support_20"] * 0.997)
        raw_stop_distance = min(atr_stop_distance, support_stop_distance)
    else:
        raw_stop_distance = atr_stop_distance

    final_stop_distance = min(max(raw_stop_distance, min_stop_distance), max_stop_distance)
    stop = entry_price - final_stop_distance
    target = entry_price + config.reward_risk_ratio * final_stop_distance
    return round(stop, 4), round(target, 4)


def calculate_position_size(
    account_equity: float,
    entry_price: float,
    stop_price: float,
    config: TradingConfig = CONFIG,
) -> float:
    dollar_risk = account_equity * config.risk_per_trade_pct
    risk_per_share = max(entry_price - stop_price, 0)

    if risk_per_share <= 0:
        return 0.0

    risk_based_shares = dollar_risk / risk_per_share
    max_position_dollars = account_equity * config.max_position_size_pct
    cap_based_shares = max_position_dollars / entry_price
    shares = min(risk_based_shares, cap_based_shares)

    if not config.allow_fractional_shares:
        shares = math.floor(shares)

    return round(max(shares, 0), 6)
