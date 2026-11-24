"""Tests for reliability changes; no network or order submission."""
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from requests.exceptions import Timeout

from audit.entry_coverage import summarize_coverage
from audit.shadow import run_session, summarize
from daytrading.broker.alpaca_paper import AlpacaPaperBroker, _read_with_retry
from daytrading.config import WATCHLIST
from daytrading.market_snapshots import capture_frame
from daytrading.strategy import evaluate_long_setup
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 23)


class ReliabilityTests(unittest.TestCase):
    def test_transient_read_retries_then_succeeds(self):
        call = MagicMock(side_effect=[Timeout(), Timeout(), ["valid"]])
        with patch("daytrading.broker.alpaca_paper.time.sleep"):
            self.assertEqual(_read_with_retry(call), ["valid"])
        self.assertEqual(call.call_count, 3)

    def test_unavailable_order_list_is_not_empty(self):
        broker = AlpacaPaperBroker("test", "test")
        client = MagicMock()
        client.get_orders.side_effect = Timeout()
        with patch.object(AlpacaPaperBroker, "client", return_value=client), \
             patch("daytrading.broker.alpaca_paper.time.sleep"):
            with self.assertRaises(Timeout):
                broker.open_orders()
            with self.assertRaises(Timeout):
                broker.all_orders()
        self.assertEqual(client.get_orders.call_count, 6)

    def test_unknown_order_lookup_propagates(self):
        broker = AlpacaPaperBroker("test", "test")
        client = MagicMock()
        client.get_order_by_client_id.side_effect = RuntimeError("unknown")
        with patch.object(AlpacaPaperBroker, "client", return_value=client):
            with self.assertRaises(RuntimeError):
                broker.get_order_by_client_order_id("paper-entry-AAPL-2026-11-23")

    def test_calendar_flags_wholly_missing_session(self):
        result = summarize_coverage([], ["2026-11-23"])
        self.assertEqual(result["missing_entire_sessions"], ["2026-11-23"])
        self.assertEqual(result["days_unverified"], 1)
        self.assertEqual(result["fully_observed_no_signal_days"], 0)

    def test_snapshot_round_trip_preserves_values(self):
        frame = pd.DataFrame({"symbol": ["AAPL"], "close": [123.45678901234567]})
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict("os.environ", {"PAPER_CAPTURE_MARKET_DATA": "1"}):
            capture_frame(frame, "entry", directory=Path(tmp))
            files = list(Path(tmp).glob("*.csv.gz"))
            self.assertEqual(len(files), 1)
            recovered = pd.read_csv(files[0], float_precision="round_trip")
            self.assertEqual(recovered["close"][0], frame["close"][0])


def complete_frame():
    stamps = pd.date_range(datetime.combine(DAY, time(9, 30), ET),
                           datetime.combine(DAY, time(15, 55), ET), freq="5min")
    return pd.DataFrame([
        {"timestamp": stamp, "symbol": symbol, "open": 100.0,
         "high": 100.4, "low": 99.9, "close": 100.2, "volume": 100,
         "vwap": 99.8, "ema_9": 100.1, "ema_20": 99.9,
         "rsi_14": 60., "macd_hist": 0.1, "macd_hist_slope": 0.01,
         "volume_ratio": 2.0 if symbol == "MSFT" else 0.9,
         "resistance_20": 99.9, "support_20": 99.5, "atr_14": 0.5,
         "close_location": 0.7, "vwap_distance_atr": 0.8, "market_filter_ok": True,
         "above_vwap_3bar": True, "below_vwap_2bar": False, "vwap_slope_3": 0.1}
        for stamp in stamps for symbol in WATCHLIST
    ])


class ShadowTests(unittest.TestCase):
    def test_four_variants_and_one_trade_each(self):
        frame = complete_frame()
        result = run_session(frame, DAY, datetime.combine(DAY, time(16), ET))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["policies"]), 4)
        self.assertEqual(result["policies"]["B_volume_confirmation"]["symbol"], "MSFT")
        self.assertEqual(result["policies"]["D_min_score_14"]["setup_score"], 14)
        self.assertEqual(result["policies"]["A_control"]["signal_bar"][11:16], "10:15")
        self.assertEqual(result["policies"]["A_control"]["exit_reason"], "end_of_day")
        self.assertFalse(summarize([result])["strategy_selection_authorized"])

    def test_missing_market_data_is_not_a_no_signal_day(self):
        frame = complete_frame().iloc[1:].copy()
        result = run_session(frame, DAY, datetime.combine(DAY, time(16), ET))
        self.assertEqual(result["status"], "incomplete_market_data")
        self.assertEqual(summarize([result])["complete_sessions"], 0)

    def test_volume_filter_can_choose_different_candidate(self):
        frame = complete_frame()
        # AAPL and MSFT tie at 13: AAPL fails volume, MSFT fails breakout.
        # Since MSFT ranks higher by volume, isolate a higher AAPL score
        # by dropping another noncore MSFT criterion.
        frame.loc[frame.symbol.eq("MSFT"), "resistance_20"] = 101.0
        frame.loc[frame.symbol.eq("MSFT"), "close_location"] = 0.4
        result = run_session(frame, DAY, datetime.combine(DAY, time(16), ET))
        self.assertEqual(result["policies"]["A_control"]["symbol"], "AAPL")
        self.assertEqual(result["policies"]["B_volume_confirmation"]["symbol"], "MSFT")


if __name__ == "__main__":
    unittest.main()
