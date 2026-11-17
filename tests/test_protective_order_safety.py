"""Regression tests for safe Alpaca paper protective-order reconciliation."""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import alpaca_paper_position_monitor as monitor


class ProtectiveOrderSafetyTests(unittest.TestCase):
    def test_cancel_persists_terminal_status(self):
        state = {
            "protective_stop_order_id": "stop-123",
            "protective_stop_status": "new",
            "protective_stop_order": {"final_order": {"status": "new"}},
        }
        broker = MagicMock()
        broker.get_order_snapshot.return_value = {"id": "stop-123", "status": "new"}
        broker.cancel_order.return_value = {"id": "stop-123", "status": "canceled"}

        with patch.object(monitor, "save_open_position_state") as save:
            result = monitor._cancel_protective_stop(broker, state)

        self.assertEqual(result["status"], "canceled")
        self.assertEqual(state["protective_stop_status"], "canceled")
        self.assertEqual(state["protective_stop_order"]["final_order"]["status"], "canceled")
        save.assert_called()

    def test_failed_cancel_does_not_fabricate_canceled_status(self):
        state = {"protective_stop_order_id": "stop-123", "protective_stop_status": "new"}
        broker = MagicMock()
        broker.get_order_snapshot.return_value = {"id": "stop-123", "status": "new"}
        broker.cancel_order.return_value = {
            "id": "stop-123", "cancel_error": "timed out"
        }
        with patch.object(monitor, "save_open_position_state"):
            result = monitor._cancel_protective_stop(broker, state)
        self.assertIn("cancel_error", result)
        self.assertEqual(state["protective_stop_status"], "new")

    def test_unconfirmed_cancel_blocks_market_exit(self):
        state = {
            "status": "open",
            "symbol": "NVDA",
            "signal_date": "2026-11-16",
            "qty": 0.1,
            "protective_stop_order_id": "stop-123",
        }
        broker = MagicMock()
        broker.open_positions.return_value = [{"symbol": "NVDA", "qty": "0.1"}]
        broker.get_order_snapshot.return_value = {"id": "stop-123", "status": "new"}
        broker.cancel_order.return_value = {"id": "stop-123", "cancel_error": "timeout"}
        now = pd.Timestamp.now(tz="America/New_York")
        with (
            patch.object(monitor.AlpacaPaperBroker, "from_env", return_value=broker),
            patch.object(monitor, "load_open_position_state", return_value=state),
            patch.object(monitor, "_ensure_protective_stop", return_value={"status": "new"}),
            patch.object(monitor, "_market_cutoff_et", return_value=now - pd.Timedelta(minutes=1)),
            patch.object(monitor, "fetch_intraday_data", side_effect=RuntimeError("unavailable")),
            patch.object(monitor, "save_open_position_state"),
            patch.object(monitor, "_write_and_return", side_effect=lambda _name, value, _tag: value),
        ):
            result = monitor.run_position_monitor(period="5d", confirm="test")

        self.assertEqual(result["action"], "exit_blocked_unconfirmed_protective_cancel")
        broker.submit_market_sell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
