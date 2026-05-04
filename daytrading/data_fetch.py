from __future__ import annotations

from typing import Iterable

import pandas as pd
import yfinance as yf


def _flatten_yfinance_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Normalize one yfinance download result into timestamp/OHLCV rows."""
    if raw.empty:
        return pd.DataFrame()

    df = raw.copy()

    # yfinance may return a MultiIndex when called with group_by/tickers.
    if isinstance(df.columns, pd.MultiIndex):
        # Try to select the symbol level if present.
        if symbol in df.columns.get_level_values(0):
            df = df[symbol]
        elif symbol in df.columns.get_level_values(-1):
            df = df.xs(symbol, axis=1, level=-1)
        else:
            # Fall back to dropping the outer level if only one ticker came back.
            df.columns = df.columns.get_level_values(-1)

    df = df.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "Volume": "volume",
    })

    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing yfinance columns for {symbol}: {missing}")

    df = df[required].reset_index()
    timestamp_col = df.columns[0]
    df = df.rename(columns={timestamp_col: "timestamp"})
    df["symbol"] = symbol

    return df[["timestamp", "symbol", "open", "high", "low", "close", "volume"]]


def fetch_intraday_data(
    symbols: Iterable[str],
    period: str = "5d",
    interval: str = "5m",
    timezone: str = "America/New_York",
) -> pd.DataFrame:
    """
    Pull intraday OHLCV data using yfinance.

    Free market data can be delayed, unavailable, or rate limited. This function
    is suitable for research/paper observation only.
    """
    frames: list[pd.DataFrame] = []

    for symbol in symbols:
        try:
            raw = yf.download(
                tickers=symbol,
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
                prepost=False,
                threads=False,
                group_by="column",
            )
            frame = _flatten_yfinance_frame(raw, symbol)
            if not frame.empty:
                frames.append(frame)
        except Exception as exc:  # noqa: BLE001 - keep batch scan alive for other symbols.
            print(f"Failed to fetch {symbol}: {exc}")

    if not frames:
        raise RuntimeError(
            "No market data returned. Try fewer symbols, a shorter period, "
            "or rerun later if the data provider is rate-limiting."
        )

    out = pd.concat(frames, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])

    # Intraday yfinance timestamps are usually timezone-aware. Normalize to ET.
    if out["timestamp"].dt.tz is None:
        out["timestamp"] = out["timestamp"].dt.tz_localize("UTC").dt.tz_convert(timezone)
    else:
        out["timestamp"] = out["timestamp"].dt.tz_convert(timezone)

    out = out.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    return out
