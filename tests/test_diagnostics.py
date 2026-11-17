"""Tests for durable, redacted paper-trading observability (no broker)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from daytrading.diagnostics import (
    compact_scan, last_event, selected_features, watchdog_status, write_event,
)

NOW = datetime(2026, 11, 16, 16, 0, tzinfo=timezone.utc)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.state = {
            "status": "open",
            "symbol": "NVDA",
            "signal_date": "2026-11-16",
            "opened_at_et": "2026-11-16T10:20:00-05:00",
            "protective_stop_status": "new",
        }

    def test_scan_captures_rejections_and_stale_candidates(self):
        scan = pd.DataFrame([
            {"symbol": "AAPL", "setup_score": 11, "reason": "failed_breakout", "signal": False,
             "passed_checks": "above_vwap", "failed_checks": "breakout"},
            {"symbol": "SPY", "setup_score": 14, "reason": "pass", "signal": True},
        ])
        rows = compact_scan(scan, {"aligned_symbols": ["SPY"]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["failed_checks"], "breakout")
        self.assertFalse(rows[0]["aligned_to_expected_bar"])
        self.assertTrue(rows[1]["aligned_to_expected_bar"])

    def test_selected_features_are_allowlisted(self):
        features = selected_features({"symbol": "NVDA", "setup_score": 12,
                                      "secret": "never", "failed_checks": "breakout"})
        self.assertEqual(features["setup_score"], 12)
        self.assertNotIn("secret", features)

    def test_events_are_durable_and_redacted(self):
        result = {"reason": "no_active_signal", "api_key": "never-write",
                  "scan_summary": [{"symbol": "SPY", "setup_score": 11,
                                   "secret": "never-write"}]}
        event = write_event("entry", result, directory=self.directory,
                            now=NOW, state={}, send_alert=False)
        stored = last_event("entry", directory=self.directory, now=NOW)
        self.assertEqual(event["id"], stored["id"])
        self.assertEqual(stored["outcome"], "no_active_signal")
        self.assertEqual(stored["scan_summary"][0]["setup_score"], 11)
        self.assertNotIn("never-write", json.dumps(stored))

    def test_gap_detected_between_open_position_monitor_cycles(self):
        first = write_event("monitor", {"reason": "no_exit_condition_met", "symbol": "NVDA"},
                            directory=self.directory, now=NOW, state=self.state,
                            send_alert=False)
        later = write_event("monitor", {"reason": "no_exit_condition_met", "symbol": "NVDA"},
                            directory=self.directory, now=NOW + timedelta(minutes=18),
                            state=self.state, send_alert=False)
        self.assertNotIn("monitoring_gap_minutes", first)
        self.assertEqual(later["monitoring_gap_minutes"], 18)
        self.assertNotEqual(first["id"], later["id"])

    def test_gap_not_reported_after_flat_position(self):
        write_event("monitor", {"reason": "no_open_position_tracked"},
                    directory=self.directory, now=NOW,
                    state={"status": "closed"}, send_alert=False)
        result = write_event("monitor", {"reason": "no_exit_condition_met", "symbol": "NVDA"},
                             directory=self.directory, now=NOW + timedelta(minutes=32),
                             state=self.state, send_alert=False)
        self.assertNotIn("monitoring_gap_minutes", result)

    def test_monitor_watchdog_alerts_on_staleness(self):
        previous = {
            "timestamp_utc": (NOW - timedelta(minutes=40)).isoformat(),
            "state_signal_date": "2026-11-16",
        }
        value = watchdog_status(now=NOW, state=self.state, previous=previous)
        self.assertEqual(value["status"], "stale")
        self.assertEqual(value["minutes_since_monitor"], 40)

    def test_monitor_watchdog_skips_flat_positions(self):
        value = watchdog_status(now=NOW, state={"status": "closed"})
        self.assertEqual(value["status"], "not_applicable")

    def test_monitor_watchdog_grace_period_before_first_heartbeat(self):
        fresh = {**self.state, "opened_at_et": "2026-11-16T10:58:00-05:00"}
        value = watchdog_status(now=NOW, state=fresh, previous={})
        self.assertEqual(value["status"], "grace_period")

    def test_audit_write_error_is_not_silently_ignored(self):
        not_directory = self.directory / "block"
        not_directory.write_text("file")
        with self.assertRaises(OSError):
            write_event("entry", {"reason": "no_active_signal"},
                        directory=not_directory, now=NOW,
                        state={}, send_alert=False)


if __name__ == "__main__":
    unittest.main()
