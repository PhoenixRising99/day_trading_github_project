"""Durable, redacted audit events for the PAPER-only trading workflow.

No broker calls or order submission. Events are one JSON line each, stored in
UTC-date shards and committed by the existing workflow after each cycle.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
DIAGNOSTICS_DIR = ROOT / "data" / "logs" / "diagnostics"
GAP_THRESHOLD_MINUTES = 12
ALERT_ACTIONS = {
    "exit_blocked_unconfirmed_protective_cancel",
    "exit_blocked_untracked_sell_orders",
    "reconciliation_blocked_unconfirmed_stop",
    "exit_pending_or_partial",
}
SCAN_FIELDS = (
    "symbol", "data_timestamp", "signal", "raw_signal_before_time_filter",
    "setup_score", "setup_max_score", "reason", "rsi_14", "volume_ratio",
    "vwap", "vwap_distance_atr", "market_filter_ok", "entry_preview",
    "passed_checks", "failed_checks",
)
SELECTED_FIELDS = (
    "symbol", "data_timestamp", "signal_date", "setup_score", "setup_max_score",
    "entry_preview", "shares_preview", "position_value_preview",
    "stop_loss_preview", "take_profit_preview", "rsi_14", "volume_ratio",
    "vwap_distance_atr", "market_filter_ok", "passed_checks", "failed_checks",
)


def _simple(value: Any) -> Any:
    """Normalize pandas/scalar values without writing NaN or internal objects."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (value != value or abs(value) == float("inf")):
            return None
        return value
    if hasattr(value, "item"):
        return _simple(value.item())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def compact_scan(scan, alignment: dict | None = None) -> list[dict[str, Any]]:
    """Record every symbol, including rejected and stale candidates."""
    if scan is None or scan.empty:
        return []
    aligned = set((alignment or {}).get("aligned_symbols") or [])
    result = []
    for record in scan.to_dict(orient="records"):
        item = {key: _simple(record.get(key)) for key in SCAN_FIELDS if key in record}
        item["aligned_to_expected_bar"] = bool(str(record.get("symbol", "")) in aligned)
        result.append(item)
    return result


def selected_features(signal: dict[str, Any]) -> dict[str, Any]:
    return {key: _simple(signal.get(key)) for key in SELECTED_FIELDS if key in signal}


def last_event(kind: str, *, directory: Path = DIAGNOSTICS_DIR,
               now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(timezone.utc)
    # UTC date shard boundaries can occur in the middle of a US market session.
    for day in (now.date(), (now - timedelta(days=1)).date()):
        path = directory / f"{day.isoformat()}.jsonl"
        if not path.is_file():
            continue
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if line.strip():
                record = json.loads(line)  # Malformed audit logs are not silently ignored.
                if record.get("kind") == kind:
                    return record
    return None


def _safe_state() -> dict:
    from daytrading.position_state import load_open_position_state
    return load_open_position_state() or {}


def _alert(message: str) -> None:
    """Warn in Actions logs; optional Discord alert does not contain secrets."""
    clean = " ".join(str(message).splitlines())[:1500]
    print("::warning::" + clean, flush=True)
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return
    payload = json.dumps({"content": "[PAPER TRADING ALERT] " + clean}).encode("utf-8")
    req = urllib_request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib_request.urlopen(req, timeout=4) as response:
            response.read()
    except Exception as exc:
        # Never echo the webhook URL, which may be present in a transport error.
        print(f"::warning::Discord alert delivery failed ({type(exc).__name__}).", flush=True)


def write_event(kind: str, result: dict[str, Any], *,
                directory: Path = DIAGNOSTICS_DIR,
                now: datetime | None = None,
                state: dict | None = None,
                send_alert: bool = True) -> dict[str, Any]:
    """Append once per completed call; fail visibly if audit persistence fails."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Audit timestamps must be timezone aware.")
    now = now.astimezone(timezone.utc)
    state = state if state is not None else _safe_state()
    detail = result.get("selected_signal") or {}
    record = {
        "version": 1,
        "id": str(uuid.uuid4()),
        "kind": kind,
        "timestamp_utc": now.isoformat(),
        "timestamp_et": now.astimezone(ET).isoformat(),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "outcome": result.get("reason") or result.get("action") or "",
        "action": result.get("action"),
        "submitted": result.get("submitted"),
        "position_opened": result.get("position_opened"),
        "symbol": result.get("symbol") or detail.get("symbol") or state.get("symbol"),
        "state_open_after": state.get("status") == "open",
        "state_signal_date": state.get("signal_date"),
        "state_protective_stop_status": state.get("protective_stop_status"),
        "market_data_alignment": result.get("market_data_alignment"),
        "alignment_attempts": result.get("alignment_attempts"),
        "scan_summary": (
            [
                {k: _simple(row.get(k)) for k in (*SCAN_FIELDS, "aligned_to_expected_bar") if k in row}
                for row in result["scan_summary"] if isinstance(row, dict)
            ] if result.get("scan_summary") is not None else None
        ),
        "selected_signal": selected_features(detail),
        "evaluated_bar": result.get("evaluated_bar"),
        "expected_bar": result.get("expected_bar"),
        "market_cutoff_et": result.get("market_cutoff_et"),
        "exit_reason": result.get("exit_reason"),
        "evaluated_close": _simple(result.get("evaluated_close")),
        "exit_fill_price": _simple(result.get("exit_fill_price")),
        "exit_order_submitted_at": result.get("exit_order_submitted_at"),
        "exit_order_filled_at": result.get("exit_order_filled_at"),
        "data_error_type": result.get("data_error_type"),
        "stop_status": result.get("protective_stop_status"),
        "exit_order_status": result.get("order_status"),
    }
    previous = last_event(kind, directory=directory, now=now) if kind == "monitor" else None
    if (kind == "monitor" and previous and previous.get("state_open_after")
            and record["state_open_after"]
            and previous.get("state_signal_date") == record["state_signal_date"]
            and previous.get("symbol") == record["symbol"]):
        age = (now - datetime.fromisoformat(previous["timestamp_utc"])).total_seconds() / 60
        if age >= GAP_THRESHOLD_MINUTES:
            record["monitoring_gap_minutes"] = round(age, 2)

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now.date().isoformat()}.jsonl"
    # JSONL is append-only, and the workflow commits these files every cycle.
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, allow_nan=False, separators=(",", ":"), default=str) + "\n")
        file.flush()
        os.fsync(file.fileno())

    if send_alert:
        if record.get("monitoring_gap_minutes"):
            _alert(f"Paper monitoring gap {record['monitoring_gap_minutes']} min "
                   f"for {record['symbol']} (run {record['run_id']}).")
        elif record.get("action") in ALERT_ACTIONS:
            _alert(f"Paper monitor requires attention: {record['action']} "
                   f"for {record['symbol']} (run {record['run_id']}).")
        elif (kind == "monitor" and record["state_open_after"]
              and str(record["state_protective_stop_status"]).startswith("repair_failed")):
            _alert(f"Broker protective stop repair failed for {record['symbol']}.")
    return record


def watchdog_status(*, now: datetime | None = None, state: dict | None = None,
                    previous: dict | None = None, threshold_minutes: float = 23) -> dict:
    """Detect persistently missing heartbeats; read-only, no broker API calls."""
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    state = state if state is not None else _safe_state()
    if (state.get("status") != "open" or now.weekday() >= 5
            or not (9 * 60 + 30 <= now.hour * 60 + now.minute <= 16 * 60)):
        return {"status": "not_applicable", "reason": "no_active_regular_session_strategy_position"}
    previous = previous if previous is not None else last_event("monitor", now=now.astimezone(timezone.utc))
    age = None
    if previous and previous.get("state_signal_date") == state.get("signal_date"):
        age = (now.astimezone(timezone.utc)
               - datetime.fromisoformat(previous["timestamp_utc"]).astimezone(timezone.utc)
               ).total_seconds() / 60
    # If an entry happened recently, allow the monitor to start before alerting.
    opened = state.get("opened_at_et")
    time_since_open = None
    if opened:
        try:
            time_since_open = (now - datetime.fromisoformat(opened).astimezone(ET)).total_seconds() / 60
        except (ValueError, TypeError):
            pass
    if age is None and time_since_open is not None and time_since_open <= threshold_minutes:
        return {"status": "grace_period", "minutes_since_entry": round(time_since_open, 1)}
    if age is None or age > threshold_minutes:
        return {"status": "stale", "symbol": state.get("symbol"),
                "minutes_since_monitor": round(age, 1) if age is not None else None,
                "threshold_minutes": threshold_minutes}
    return {"status": "healthy", "symbol": state.get("symbol"),
            "minutes_since_monitor": round(age, 1)}
