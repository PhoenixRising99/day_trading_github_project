"""Test coverage classification without broker accounts or external market data."""
import unittest

from audit.entry_coverage import summarize_coverage


def event(minute, *, outcome="no_active_signal", scanned=True, candidate=None):
    return {
        "kind": "entry",
        "timestamp_et": f"2026-11-16T10:{minute:02d}:51-05:00",
        "outcome": outcome,
        "scan_summary": ([candidate] if candidate else []) if scanned else None,
        "market_data_alignment": {
            "market_context_aligned": True, "missing_or_stale_symbols": [],
        } if scanned else None,
    }


class ScannerCoverageTests(unittest.TestCase):
    def test_complete_no_signal_day_requires_all_nine_slots(self):
        entries = [event(m) for m in range(15, 60, 5)]
        info = summarize_coverage(entries)
        self.assertEqual(info["fully_observed_no_signal_days"], 1)
        self.assertEqual(info["days"][0]["missing_slots"], [])

    def test_missing_cycle_is_not_certified_no_signal(self):
        entries = [event(m) for m in range(15, 60, 5) if m != 35]
        info = summarize_coverage(entries)
        self.assertEqual(info["days_unverified"], 1)
        self.assertEqual(info["days"][0]["missing_slots"], ["10:35"])

    def test_trading_day_does_not_expect_scans_after_fill(self):
        entries = [event(15), event(20, outcome="entry_filled")]
        info = summarize_coverage(entries)
        self.assertTrue(info["days"][0]["order_filled"])
        self.assertEqual(info["days"][0]["missing_slots"], [])
        self.assertEqual(info["days"][0]["coverage_classification"], "trade_recorded")

    def test_flags_eligible_but_not_filled_signal(self):
        entries = [event(15), event(20, outcome="entry_not_filled_rejected",
                         candidate={"symbol": "QQQ", "signal": True,
                                    "aligned_to_expected_bar": True})]
        info = summarize_coverage(entries)
        self.assertEqual(info["eligible_unfilled_candidate_count"], 1)
        self.assertEqual(info["days"][0]["eligible_unfilled_candidates"][0]["symbol"], "QQQ")

    def test_pre_scan_block_is_not_an_empty_candidate_scan(self):
        entries = [event(15, outcome="blocked_existing_open_position_or_order", scanned=False)]
        info = summarize_coverage(entries)
        self.assertEqual(info["days"][0]["candidate_scans"], 0)
        self.assertEqual(info["days_unverified"], 1)

    def test_no_events_means_unknown_not_zero_missed_signals(self):
        info = summarize_coverage([])
        self.assertEqual(info["days_with_entry_records"], 0)
        self.assertIn("unknown", info["historical_days_before_durable_logging"])


if __name__ == "__main__":
    unittest.main()
