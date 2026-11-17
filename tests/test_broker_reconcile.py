"""Broker reconciliation regression tests (read-only, fully mocked)."""
import unittest
from unittest.mock import MagicMock

from audit.broker_reconcile import reconcile


class PaperBrokerReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.broker = MagicMock()
        self.broker.open_positions.return_value = []
        self.broker.client.return_value.get_orders.return_value = []
        self.broker.get_order_snapshot.return_value = {"status": "canceled"}
        self.closed = {
            "status": "closed", "symbol": "NVDA", "signal_date": "2026-11-11",
            "protective_stop_order_id": "stop-11", "qty": 0.137215,
        }

    def test_fully_closed_broker_is_consistent(self):
        result = reconcile(self.broker, self.closed)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["tracked_stop_actual_status"], "canceled")
        self.assertEqual(result["open_strategy_orders"], [])

    def test_open_stop_with_closed_state_is_flagged(self):
        self.broker.client.return_value.get_orders.return_value = [object()]
        self.broker._order_snapshot.return_value = {
            "id": "orphan-stop", "symbol": "NVDA", "side": "sell",
            "status": "new", "client_order_id": "paper-protective-stop-NVDA-2026-11-11",
        }
        result = reconcile(self.broker, self.closed)
        self.assertFalse(result["reconciled"])
        self.assertIn("strategy_orders_resting_while_local_state_closed", result["concerns"])

    def test_open_quantity_mismatch_is_flagged(self):
        self.broker.open_positions.return_value = [{"symbol": "NVDA", "qty": "0.25"}]
        state = {**self.closed, "status": "open"}
        result = reconcile(self.broker, state)
        self.assertIn("tracked_vs_broker_quantity_mismatch", result["concerns"])

    def test_broker_order_api_errors_must_not_be_treated_as_no_orders(self):
        self.broker.client.return_value.get_orders.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            reconcile(self.broker, self.closed)


if __name__ == "__main__":
    unittest.main()
