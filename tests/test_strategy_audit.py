"""Unit tests for read-only counterfactual replay and its safety assumptions."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from audit.replay import replay_trade, summarize_ledger

ET = ZoneInfo("America/New_York")


def sample_data():
    start = datetime(2026, 11, 6, 9, 30, tzinfo=ET)
    rows = []
    for index in range(78):
        timestamp = start + timedelta(minutes=index * 5)
        recovering = index >= 17
        rows.append({
            "timestamp": timestamp,
            "open": 102.0 if recovering else 100.0,
            "high": 103.0 if recovering else 101.0,
            "low": 101.0 if recovering else 99.0,
            "close": 102.0 if recovering else (99.0 if index in (14, 15) else 100.0),
            "vwap": 100.0,
            "ema_9": 101.0,
            "ema_20": 100.0,
        })
    return pd.DataFrame(rows)


def sample_trade():
    return {
        "signal_date": "2026-11-06",
        "opened_at_et": "2026-11-06T10:16:00-05:00",
        "entry_fill_price": 100.0,
        "entry_preview": 100.0,
        "stop_loss_preview": 98.0,
        "take_profit_preview": 105.0,
        "qty": 1.0,
    }


class ReadOnlyReplayTests(unittest.TestCase):
    def test_vwap_confirmation_variants_share_identical_bars(self):
        candles = sample_data()
        vwap2 = replay_trade(sample_trade(), candles, vwap_bars=2, ema_bars=1)
        vwap3 = replay_trade(sample_trade(), candles, vwap_bars=3, ema_bars=1)
        self.assertEqual(vwap2["reason"], "lost_vwap_2bar")
        self.assertEqual(vwap3["reason"], "end_of_day")
        self.assertGreater(vwap3["modeled_pnl"], vwap2["modeled_pnl"])

    def test_stop_takes_priority_when_target_touched_same_bar(self):
        candles = sample_data()
        candles.loc[10, ["low", "high"]] = [97.0, 106.0]
        outcome = replay_trade(sample_trade(), candles)
        self.assertEqual(outcome["reason"], "stop_loss")
        self.assertLess(outcome["modeled_pnl"], 0)

    def test_never_evaluate_uncompleted_entry_bar(self):
        candles = sample_data()
        candles.loc[9, ["low", "high"]] = [97.0, 106.0]  # 10:15 candle
        outcome = replay_trade(sample_trade(), candles)
        self.assertNotEqual(outcome["reason"], "stop_loss")

    def test_ledger_summary_does_not_count_pending_rows(self):
        ledger = pd.DataFrame({
            "realized_pnl": [0.20, -0.10], "exit_reason": ["take_profit", "stop_loss"]
        })
        s = summarize_ledger(ledger)
        self.assertEqual(s["completed_trades"], 2)
        self.assertAlmostEqual(s["realized_pnl"], 0.1)
        self.assertAlmostEqual(s["profit_factor"], 2.0)


if __name__ == "__main__":
    unittest.main()
