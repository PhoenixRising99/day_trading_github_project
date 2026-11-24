"""Read-only report of broker scanner coverage and accepted setup quality.

Uses only persisted per-cycle diagnostics. Missing data are classified as
unobserved, NEVER as confirmation of zero signals.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from daytrading.diagnostics import DIAGNOSTICS_DIR

ET = ZoneInfo("America/New_York")
SLOTS = [f"10:{m:02d}" for m in range(15, 60, 5)]


def load_events(directory: Path = DIAGNOSTICS_DIR) -> list[dict]:
    events = []
    if not directory.is_dir():
        return events
    for path in sorted(directory.glob("*.jsonl")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Corrupt audit row at {path}:{i}: {exc}") from exc
    return events


def summarize_coverage(events: list[dict], expected_sessions=None) -> dict:
    by_day = defaultdict(list)
    for event in events:
        if event.get("kind") != "entry":
            continue
        timestamp = event.get("timestamp_et")
        if not timestamp:
            continue
        stamp = datetime.fromisoformat(timestamp).astimezone(ET)
        by_day[stamp.date().isoformat()].append(event)
    days = []
    for day in expected_sessions or []:
        by_day.setdefault(day, [])
    for day, entries in sorted(by_day.items()):
        entries.sort(key=lambda e: e.get("timestamp_et", ""))
        scans = [e for e in entries if e.get("scan_summary") is not None]
        filled = [e for e in entries if e.get("outcome") == "entry_filled"]
        blocked = [e for e in entries if str(e.get("outcome", "")).startswith(
            ("blocked_", "market_context_stale", "entry_not_filled")
        )]
        # The scanner stops once a trade was placed: later slots are not expected.
        stop_time = datetime.fromisoformat(filled[0]["timestamp_et"]).astimezone(ET) if filled else None
        expected = [
            slot for slot in SLOTS
            if stop_time is None or slot <= stop_time.strftime("%H:%M")
        ]
        # Compact and unambiguous 5-minute slot floors.
        seen = {
            f"{datetime.fromisoformat(e['timestamp_et']).astimezone(ET).hour:02d}:"
            f"{datetime.fromisoformat(e['timestamp_et']).astimezone(ET).minute // 5 * 5:02d}"
            for e in entries if e.get("timestamp_et")
        }
        missed = [slot for slot in expected if slot not in seen]
        eligible_without_fill = []
        for e in scans:
            if e.get("outcome") == "entry_filled":
                continue
            for c in e.get("scan_summary") or []:
                if c.get("signal") is True and c.get("aligned_to_expected_bar"):
                    eligible_without_fill.append({
                        "timestamp_et": e.get("timestamp_et"),
                        "symbol": c.get("symbol"),
                        "reason": e.get("outcome"),
                    })
        unavailable = sum(bool((e.get("market_data_alignment") or {}).get(
            "missing_or_stale_symbols")) for e in scans)
        missing_context = sum(not (e.get("market_data_alignment") or {}).get(
            "market_context_aligned", False) for e in scans)
        coverage = (
            "missing_entire_session" if not entries else
            "complete_observed_no_signal" if not filled and not missed and not blocked
            and len(scans) >= len(expected) and all(e.get("outcome") == "no_active_signal" for e in entries)
            and not unavailable and not missing_context
            else "trade_recorded" if filled else "incomplete_or_unverified"
        )
        days.append({
            "date": day,
            "entry_decisions": len(entries),
            "candidate_scans": len(scans),
            "order_filled": bool(filled),
            "expected_slots_until_first_trade": expected,
            "observed_slots": sorted(seen.intersection(SLOTS)),
            "missing_slots": missed,
            "blocked_or_unfilled_evaluations": len(blocked),
            "scans_with_missing_or_stale_symbols": unavailable,
            "scans_with_unaligned_market_context": missing_context,
            "eligible_unfilled_candidates": eligible_without_fill,
            "coverage_classification": coverage,
            "outcomes": dict(Counter(e.get("outcome", "") for e in entries)),
        })
    return {
        "days_with_entry_records": sum(d["entry_decisions"] > 0 for d in days),
        "days_without_trades": sum(d["entry_decisions"] > 0 and not d["order_filled"] for d in days),
        "calendar_sessions_checked": len(expected_sessions) if expected_sessions is not None else None,
        "missing_entire_sessions": [d["date"] for d in days if d["coverage_classification"] == "missing_entire_session"],
        "fully_observed_no_signal_days": sum(
            d["coverage_classification"] == "complete_observed_no_signal" for d in days
        ),
        "days_unverified": sum(
            d["coverage_classification"] in {"incomplete_or_unverified", "missing_entire_session"} for d in days
        ),
        "eligible_unfilled_candidate_count": sum(
            len(d["eligible_unfilled_candidates"]) for d in days
        ),
        "historical_days_before_durable_logging": "not assessed; absence of events is unknown coverage",
        "days": days,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only paper scanner coverage report")
    parser.add_argument("--directory", type=Path, default=DIAGNOSTICS_DIR)
    parser.add_argument("--output", type=Path, default=Path("audit_outputs/entry_coverage.json"))
    parser.add_argument("--exchange-calendar", action="store_true",
                        help="Check every completed exchange session since durable logging began.")
    args = parser.parse_args()
    expected = None
    if args.exchange_calendar:
        from audit.exchange_calendar import completed_sessions, LOGGING_START
        expected = completed_sessions(LOGGING_START, datetime.now(ET).date())
    result = summarize_coverage(load_events(args.directory), expected)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("ENTRY_COVERAGE_JSON=" + json.dumps({
        key: val for key, val in result.items() if key != "days"
    }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
