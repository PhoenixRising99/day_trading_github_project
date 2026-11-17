"""Verify the frozen scanner exposes decision criteria without changing signals."""
import unittest
from unittest.mock import patch

import pandas as pd

from daytrading.paper import paper_scan


class ScannerCriteriaTests(unittest.TestCase):
    def test_scan_persists_frozen_passed_and_failed_checks(self):
        today = pd.Timestamp.now(tz="America/New_York").normalize()
        candle = today + pd.Timedelta(hours=10, minutes=20)
        frame = pd.DataFrame([{
            "timestamp": candle,
            "symbol": "AAPL",
            "close": 101.0, "vwap": 100.0,
            "ema_9": 101.0, "ema_20": 100.0, "rsi_14": 60,
            "macd_hist": 0.3, "macd_hist_slope": 0.2,
            "volume_ratio": 2.0, "resistance_20": 99.0,
            "atr_14": 1.0, "close_location": 0.9,
            "vwap_distance_atr": 1.0, "market_filter_ok": True,
            "above_vwap_3bar": True, "vwap_slope_3": 0.2,
            "support_20": 99.0,
        }])
        with (
            patch("daytrading.paper.fetch_intraday_data", return_value=frame),
            patch("daytrading.paper.add_indicators", side_effect=lambda x: x),
            patch("daytrading.paper._latest_completed_rows", return_value=frame),
        ):
            full = paper_scan(symbols=["AAPL"])
        self.assertTrue(bool(full.iloc[0]["signal"]))
        self.assertEqual(full.iloc[0]["setup_score"], 14)
        self.assertIn("vwap_hold_3bar", full.iloc[0]["passed_checks"])
        self.assertEqual(full.iloc[0]["failed_checks"], "")

        reduced = frame.copy()
        reduced["volume_ratio"] = 0.5
        with (
            patch("daytrading.paper.fetch_intraday_data", return_value=reduced),
            patch("daytrading.paper.add_indicators", side_effect=lambda x: x),
            patch("daytrading.paper._latest_completed_rows", return_value=reduced),
        ):
            alternate = paper_scan(symbols=["AAPL"])
        self.assertEqual(alternate.iloc[0]["setup_score"], 13)
        self.assertIn("volume_confirmation", alternate.iloc[0]["failed_checks"])


if __name__ == "__main__":
    unittest.main()
