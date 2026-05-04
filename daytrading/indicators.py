from __future__ import annotations

import numpy as np
import pandas as pd


REQUIRED_OHLCV_COLUMNS = ["timestamp", "symbol", "open", "high", "low", "close", "volume"]


def _validate_input_frame(df: pd.DataFrame) -> None:
    """Fail early with a useful message if upstream data changed shape."""
    missing = [c for c in REQUIRED_OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            "Input market-data frame is missing required columns: "
            f"{missing}. Available columns: {list(df.columns)}"
        )



def _apply_per_symbol(df: pd.DataFrame, func) -> pd.DataFrame:
    """
    Apply a transformation to each symbol without relying on groupby.apply
    preserving grouping columns.

    This is intentionally written as an explicit concat because pandas 3.x
    changed groupby/apply behavior enough that the prior version could lose
    the `symbol` column after apply(), which later caused KeyError: 'symbol'.
    """
    parts: list[pd.DataFrame] = []

    for symbol, group in df.groupby("symbol", sort=False, group_keys=False):
        out = func(group.copy())
        if "symbol" not in out.columns:
            out["symbol"] = symbol
        parts.append(out)

    if not parts:
        return pd.DataFrame(columns=df.columns)

    return pd.concat(parts, ignore_index=False)



def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add V9/V10/V11 indicator set used by the frozen paper strategy."""
    df = df.copy()
    _validate_input_frame(df)

    df = df.sort_values(["symbol", "timestamp"])
    df["date"] = df["timestamp"].dt.date

    def per_symbol(g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy()

        g["ema_9"] = g["close"].ewm(span=9, adjust=False).mean()
        g["ema_20"] = g["close"].ewm(span=20, adjust=False).mean()
        g["sma_50"] = g["close"].rolling(50, min_periods=20).mean()

        delta = g["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        g["rsi_14"] = 100 - (100 / (1 + rs))

        ema_12 = g["close"].ewm(span=12, adjust=False).mean()
        ema_26 = g["close"].ewm(span=26, adjust=False).mean()
        g["macd"] = ema_12 - ema_26
        g["macd_signal"] = g["macd"].ewm(span=9, adjust=False).mean()
        g["macd_hist"] = g["macd"] - g["macd_signal"]
        g["macd_hist_prev"] = g["macd_hist"].shift(1)
        g["macd_hist_slope"] = g["macd_hist"] - g["macd_hist_prev"]

        prev_close = g["close"].shift(1)
        tr = pd.concat(
            [
                g["high"] - g["low"],
                (g["high"] - prev_close).abs(),
                (g["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        g["atr_14"] = tr.rolling(14, min_periods=14).mean()

        g["vol_avg_20"] = g["volume"].rolling(20, min_periods=10).mean()
        g["volume_ratio"] = g["volume"] / g["vol_avg_20"].replace(0, np.nan)

        g["resistance_20"] = g["high"].rolling(20, min_periods=10).max().shift(1)
        g["support_20"] = g["low"].rolling(20, min_periods=10).min().shift(1)

        g["candle_range"] = (g["high"] - g["low"]).replace(0, np.nan)
        g["close_location"] = (g["close"] - g["low"]) / g["candle_range"]

        g["close_prev_1"] = g["close"].shift(1)
        g["close_prev_2"] = g["close"].shift(2)
        return g

    df = _apply_per_symbol(df, per_symbol)
    _validate_input_frame(df)

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    df["tpv"] = typical_price * df["volume"]
    df["cum_tpv"] = df.groupby(["symbol", "date"], sort=False)["tpv"].cumsum()
    df["cum_vol"] = df.groupby(["symbol", "date"], sort=False)["volume"].cumsum()
    df["vwap"] = df["cum_tpv"] / df["cum_vol"].replace(0, np.nan)

    def add_vwap_structure(g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy()
        g["vwap_prev_1"] = g["vwap"].shift(1)
        g["vwap_prev_2"] = g["vwap"].shift(2)
        g["vwap_prev_3"] = g["vwap"].shift(3)

        g["above_vwap_3bar"] = (
            (g["close"] > g["vwap"])
            & (g["close_prev_1"] > g["vwap_prev_1"])
            & (g["close_prev_2"] > g["vwap_prev_2"])
        )
        g["below_vwap_2bar"] = (
            (g["close"] < g["vwap"]) & (g["close_prev_1"] < g["vwap_prev_1"])
        )
        g["vwap_slope_3"] = g["vwap"] - g["vwap_prev_3"]
        return g

    df = _apply_per_symbol(df, add_vwap_structure)
    _validate_input_frame(df)

    df["vwap_distance_atr"] = (df["close"] - df["vwap"]) / df["atr_14"].replace(0, np.nan)

    spy_context = (
        df[df["symbol"] == "SPY"][
            [
                "timestamp",
                "close",
                "vwap",
                "ema_9",
                "ema_20",
                "rsi_14",
                "macd_hist",
                "above_vwap_3bar",
                "vwap_slope_3",
            ]
        ]
        .rename(
            columns={
                "close": "spy_close",
                "vwap": "spy_vwap",
                "ema_9": "spy_ema_9",
                "ema_20": "spy_ema_20",
                "rsi_14": "spy_rsi_14",
                "macd_hist": "spy_macd_hist",
                "above_vwap_3bar": "spy_above_vwap_3bar",
                "vwap_slope_3": "spy_vwap_slope_3",
            }
        )
    )

    if not spy_context.empty:
        df = df.merge(spy_context, on="timestamp", how="left")
        df["market_filter_ok"] = (
            (df["spy_close"] > df["spy_vwap"])
            & (df["spy_ema_9"] > df["spy_ema_20"])
            & (df["spy_rsi_14"] >= 50)
            & (df["spy_macd_hist"] > 0)
            & (df["spy_above_vwap_3bar"] == True)  # noqa: E712
            & (df["spy_vwap_slope_3"] > 0)
        )
    else:
        df["market_filter_ok"] = True

    return df.drop(columns=["tpv", "cum_tpv", "cum_vol"])
