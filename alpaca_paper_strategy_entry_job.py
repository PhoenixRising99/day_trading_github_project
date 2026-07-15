from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from daytrading.broker import AlpacaPaperBroker
from daytrading.config import CONFIG, STRATEGY_PARAMS, WATCHLIST
from daytrading.paper import paper_scan
from daytrading.position_state import load_open_position_state, save_open_position_state
from daytrading.strategy import is_entry_window_now

MAX_RESEARCH_POSITION_VALUE = 24.00

# yfinance sometimes exposes candidate bars before the matching SPY context bar,
# or returns a temporarily stale SPY response. Wait briefly after a five-minute
# boundary, then retry a mismatched market-context snapshot.
DATA_SETTLEMENT_SECONDS = 45
MARKET_DATA_MAX_ATTEMPTS = 4
MARKET_DATA_RETRY_SECONDS = 20


def logs_dir() -> Path:
    path = Path(__file__).resolve().parent / "data" / "logs" / "broker"
    path.mkdir(parents=True, exist_ok=True)
    return path


def scans_dir() -> Path:
    path = Path(__file__).resolve().parent / "data" / "logs" / "scans"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _json_write(name: str, payload: dict[str, Any], timestamp_tag: str) -> Path:
    path = logs_dir() / f"{name}_{timestamp_tag}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _write_and_return(name: str, result: dict[str, Any], timestamp_tag: str) -> dict[str, Any]:
    path = _json_write(name, result, timestamp_tag)
    result["result_path"] = str(path)
    print(json.dumps(result, indent=2, default=str))
    return result


def _today_str() -> str:
    return str(pd.Timestamp.now(tz=CONFIG.timezone).date())


def _today_signal_only(active: pd.DataFrame) -> pd.DataFrame:
    if active.empty:
        return active

    today = _today_str()
    if "signal_date" in active.columns:
        return active[active["signal_date"].astype(str).eq(today)].copy()

    if "data_timestamp" in active.columns:
        timestamps = pd.to_datetime(active["data_timestamp"], errors="coerce", utc=True)
        dates = timestamps.dt.tz_convert(CONFIG.timezone).dt.date.astype(str)
        return active[dates.eq(today)].copy()

    return active.iloc[0:0].copy()


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _interval_timedelta() -> pd.Timedelta:
    interval = str(CONFIG.interval).strip().lower()

    if interval.endswith("m"):
        return pd.Timedelta(minutes=int(interval[:-1]))
    if interval.endswith("h"):
        return pd.Timedelta(hours=int(interval[:-1]))

    raise ValueError(f"Unsupported intraday interval: {CONFIG.interval}")


def _interval_floor_frequency() -> str:
    interval = _interval_timedelta()
    seconds = int(interval.total_seconds())

    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}min"

    return f"{seconds}s"


def _wait_for_data_settlement() -> None:
    """
    Avoid querying yfinance immediately after a five-minute boundary.

    The July 15 miss occurred when candidate symbols exposed the 10:15 candle
    while SPY temporarily fell back to an older candle. Waiting until at least
    45 seconds into the interval gives the provider time to publish/cache the
    completed bar consistently.
    """
    now_et = pd.Timestamp.now(tz=CONFIG.timezone)
    bar_start = now_et.floor(_interval_floor_frequency())
    seconds_after_boundary = (now_et - bar_start).total_seconds()

    if seconds_after_boundary >= DATA_SETTLEMENT_SECONDS:
        return

    wait_seconds = DATA_SETTLEMENT_SECONDS - seconds_after_boundary
    print(
        "Waiting "
        f"{wait_seconds:.1f}s for completed market-data bars to settle "
        f"(current ET: {now_et.isoformat()})."
    )
    time.sleep(wait_seconds)


def _expected_completed_timestamp() -> pd.Timestamp:
    now_et = pd.Timestamp.now(tz=CONFIG.timezone)
    current_bar_start = now_et.floor(_interval_floor_frequency())
    return current_bar_start - _interval_timedelta()


def _parse_scan_timestamps(scan: pd.DataFrame) -> pd.Series:
    if "data_timestamp" not in scan.columns:
        return pd.Series(pd.NaT, index=scan.index, dtype=f"datetime64[ns, {CONFIG.timezone}]")

    timestamps = pd.to_datetime(scan["data_timestamp"], errors="coerce", utc=True)
    return timestamps.dt.tz_convert(CONFIG.timezone)


def _alignment_details(
    scan: pd.DataFrame,
    expected_timestamp: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Require SPY and candidate rows to reference the same expected completed bar.

    A stale SPY row is a data-availability problem, not a legitimate failed
    market filter. The caller retries instead of silently converting the stale
    context into `market_filter_ok=False`.
    """
    # Annotate the original scan so the saved CSV contains alignment diagnostics.
    parsed = _parse_scan_timestamps(scan).dt.floor("min")
    expected = expected_timestamp.floor("min")

    spy_mask = scan.get("symbol", pd.Series("", index=scan.index)).astype(str).eq("SPY")
    spy_timestamps = parsed[spy_mask].dropna()
    spy_timestamp = spy_timestamps.max() if not spy_timestamps.empty else pd.NaT

    row_aligned = parsed.eq(expected)
    aligned_symbols = sorted(
        scan.loc[row_aligned, "symbol"].astype(str).unique().tolist()
        if "symbol" in scan.columns
        else []
    )
    missing_symbols = sorted(set(str(symbol) for symbol in WATCHLIST) - set(aligned_symbols))

    market_context_aligned = bool(
        not pd.isna(spy_timestamp) and spy_timestamp.floor("min") == expected
    )

    scan["expected_data_timestamp"] = expected.isoformat()
    scan["market_context_timestamp"] = (
        spy_timestamp.isoformat() if not pd.isna(spy_timestamp) else ""
    )
    scan["row_aligned_to_expected"] = row_aligned
    scan["market_context_aligned"] = market_context_aligned

    details = {
        "expected_data_timestamp": expected.isoformat(),
        "spy_context_timestamp": (
            spy_timestamp.isoformat() if not pd.isna(spy_timestamp) else None
        ),
        "market_context_aligned": market_context_aligned,
        "aligned_symbols": aligned_symbols,
        "missing_or_stale_symbols": missing_symbols,
    }

    # Once SPY is aligned, only exact-timestamp candidate rows are eligible.
    aligned_scan = scan.loc[row_aligned].copy()
    return aligned_scan, details


def _run_aligned_scan(
    *,
    period: str,
) -> tuple[pd.DataFrame, pd.DataFrame, Path, dict[str, Any], int]:
    """
    Fetch/retry until SPY's context bar matches the expected completed bar.

    Returns:
        full_scan, aligned_scan, final_scan_path, alignment_details, attempts
    """
    last_full_scan = pd.DataFrame()
    last_aligned_scan = pd.DataFrame()
    last_scan_path: Path | None = None
    last_details: dict[str, Any] = {}

    for attempt in range(1, MARKET_DATA_MAX_ATTEMPTS + 1):
        _wait_for_data_settlement()
        expected_timestamp = _expected_completed_timestamp()

        print(
            f"Market-data alignment attempt {attempt}/{MARKET_DATA_MAX_ATTEMPTS}; "
            f"expected completed bar: {expected_timestamp.isoformat()}"
        )

        full_scan = paper_scan(
            WATCHLIST,
            CONFIG,
            period=period,
            strategy_params=STRATEGY_PARAMS,
        )
        aligned_scan, details = _alignment_details(full_scan, expected_timestamp)

        attempt_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
        scan_path = (
            scans_dir()
            / f"alpaca_strategy_signal_preview_{attempt_tag}_attempt{attempt}.csv"
        )
        full_scan.to_csv(scan_path, index=False)

        last_full_scan = full_scan
        last_aligned_scan = aligned_scan
        last_scan_path = scan_path
        last_details = details

        if details["market_context_aligned"]:
            print(
                "Market context aligned: "
                f"SPY={details['spy_context_timestamp']}, "
                f"expected={details['expected_data_timestamp']}."
            )
            return (
                full_scan,
                aligned_scan,
                scan_path,
                details,
                attempt,
            )

        print(
            "SPY market context is stale/missing; "
            f"SPY={details['spy_context_timestamp']}, "
            f"expected={details['expected_data_timestamp']}."
        )

        if attempt < MARKET_DATA_MAX_ATTEMPTS:
            print(f"Retrying fresh yfinance downloads in {MARKET_DATA_RETRY_SECONDS}s.")
            time.sleep(MARKET_DATA_RETRY_SECONDS)

    if last_scan_path is None:
        timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
        last_scan_path = scans_dir() / f"alpaca_strategy_signal_preview_{timestamp_tag}.csv"
        last_full_scan.to_csv(last_scan_path, index=False)

    return (
        last_full_scan,
        last_aligned_scan,
        last_scan_path,
        last_details,
        MARKET_DATA_MAX_ATTEMPTS,
    )


def run_single_strategy_attempt(
    *,
    period: str,
    confirm: str,
    allow_outside_entry_window: bool,
) -> dict[str, Any]:
    timestamp_tag = pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S")
    now_et = pd.Timestamp.now(tz=CONFIG.timezone)

    if not allow_outside_entry_window and not is_entry_window_now(CONFIG.timezone):
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "outside_entry_window",
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    today = _today_str()
    existing_state = load_open_position_state()
    if existing_state and str(existing_state.get("signal_date", "")) == today:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "already_traded_today",
                "existing_state": existing_state,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    broker = AlpacaPaperBroker.from_env()
    open_positions = broker.open_positions()
    open_orders = broker.open_orders()
    if open_positions or open_orders:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "blocked_existing_open_position_or_order",
                "open_positions": open_positions,
                "open_orders": open_orders,
                "timestamp_et": now_et.isoformat(),
            },
            timestamp_tag,
        )

    (
        full_scan,
        aligned_scan,
        scan_path,
        alignment,
        alignment_attempts,
    ) = _run_aligned_scan(period=period)

    if not alignment.get("market_context_aligned", False):
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "market_context_stale_after_retries",
                "scan_path": str(scan_path),
                "market_data_alignment": alignment,
                "alignment_attempts": alignment_attempts,
                "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
            },
            pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S"),
        )

    active = _today_signal_only(
        aligned_scan[aligned_scan["signal"] == True].copy()  # noqa: E712
    )

    if active.empty:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "no_active_signal",
                "scan_path": str(scan_path),
                "market_data_alignment": alignment,
                "alignment_attempts": alignment_attempts,
                "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
            },
            pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S"),
        )

    active = active.sort_values(
        ["setup_score", "volume_ratio", "symbol"],
        ascending=[False, False, True],
    )
    signal = active.iloc[0].to_dict()

    position_value = _safe_float(signal.get("position_value_preview"))
    requested_qty = _safe_float(signal.get("shares_preview"))

    if position_value > MAX_RESEARCH_POSITION_VALUE + 0.01:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "blocked_position_value_exceeds_research_limit",
                "position_value": position_value,
                "max_position_value": MAX_RESEARCH_POSITION_VALUE,
                "scan_path": str(scan_path),
                "market_data_alignment": alignment,
                "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
            },
            pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S"),
        )

    if requested_qty <= 0:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": False,
                "position_opened": False,
                "reason": "blocked_non_positive_quantity",
                "scan_path": str(scan_path),
                "market_data_alignment": alignment,
                "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
            },
            pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S"),
        )

    client_order_id = f"paper-entry-{signal['symbol']}-{today}"
    order_result = broker.submit_market_buy(
        symbol=str(signal["symbol"]),
        qty=requested_qty,
        confirm=confirm,
        client_order_id=client_order_id,
    )

    final_order = order_result.get("final_order", {})
    final_status = str(final_order.get("status", "")).lower()
    filled_qty = _safe_float(final_order.get("filled_qty"))
    fill_price = _safe_float(final_order.get("filled_avg_price"))

    if filled_qty <= 0:
        return _write_and_return(
            "alpaca_paper_strategy_entry",
            {
                "paper_only": True,
                "submitted": True,
                "position_opened": False,
                "reason": f"entry_not_filled_{final_status or 'unknown'}",
                "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
                "scan_path": str(scan_path),
                "market_data_alignment": alignment,
                "selected_signal": signal,
                "order_result": order_result,
            },
            pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S"),
        )

    new_state = {
        "status": "open",
        "symbol": signal.get("symbol"),
        "qty": filled_qty,
        "qty_requested": requested_qty,
        "signal_date": today,
        "opened_at_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
        "entry_preview": signal.get("entry_preview"),
        "entry_fill_price": fill_price,
        "entry_filled_at": final_order.get("filled_at", ""),
        "entry_order_status": final_status,
        "stop_loss_preview": signal.get("stop_loss_preview"),
        "take_profit_preview": signal.get("take_profit_preview"),
        "setup_score": signal.get("setup_score"),
        "client_order_id": client_order_id,
        "entry_order": order_result,
        "market_data_alignment": alignment,
    }
    state_path = save_open_position_state(new_state)

    return _write_and_return(
        "alpaca_paper_strategy_entry",
        {
            "paper_only": True,
            "submitted": True,
            "position_opened": True,
            "reason": "entry_filled",
            "timestamp_et": pd.Timestamp.now(tz=CONFIG.timezone).isoformat(),
            "scan_path": str(scan_path),
            "market_data_alignment": alignment,
            "alignment_attempts": alignment_attempts,
            "selected_signal": {
                "symbol": signal.get("symbol"),
                "data_timestamp": str(signal.get("data_timestamp")),
                "signal_date": signal.get("signal_date"),
                "setup_score": signal.get("setup_score"),
                "setup_max_score": signal.get("setup_max_score"),
                "entry_preview": signal.get("entry_preview"),
                "shares_preview": signal.get("shares_preview"),
                "position_value_preview": signal.get("position_value_preview"),
                "stop_loss_preview": signal.get("stop_loss_preview"),
                "take_profit_preview": signal.get("take_profit_preview"),
            },
            "order_result": order_result,
            "position_state_path": str(state_path),
        },
        pd.Timestamp.now(tz=CONFIG.timezone).strftime("%Y%m%d_%H%M%S"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Alpaca paper strategy entry. Paper only. No live trading."
    )
    parser.add_argument("--period", default=CONFIG.period)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allow-outside-entry-window", action="store_true")
    args = parser.parse_args()

    run_single_strategy_attempt(
        period=args.period,
        confirm=args.confirm,
        allow_outside_entry_window=args.allow_outside_entry_window,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
