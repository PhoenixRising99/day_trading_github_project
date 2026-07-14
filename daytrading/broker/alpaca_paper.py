from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)


class AlpacaConfigError(RuntimeError):
    """Raised when Alpaca configuration is missing or unsafe."""


class AlpacaSafetyError(RuntimeError):
    """Raised when an order is blocked by a local safety rule."""


GLOBAL_ORDER_SWITCH_ENV = "ALPACA_PAPER_ORDER_SUBMISSION_ENABLED"
ENTRY_ORDER_SWITCH_ENV = "ALPACA_PAPER_ENTRY_SUBMISSION_ENABLED"
EXIT_ORDER_SWITCH_ENV = "ALPACA_PAPER_EXIT_SUBMISSION_ENABLED"

ENTRY_CONFIRM_VALUE = "SUBMIT_ALPACA_PAPER_STRATEGY_ORDER"
EXIT_CONFIRM_VALUE = "SUBMIT_ALPACA_PAPER_EXIT_ORDER"
TEST_CONFIRM_VALUE = "SUBMIT_ALPACA_PAPER_TEST"

TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
    "replaced",
    "stopped",
    "suspended",
    "calculated",
}


def _model_to_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {"value": str(obj)}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except Exception:
        return None


def _value_text(value: Any) -> str:
    """Normalize Alpaca SDK enums and plain strings."""
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value).strip()
    text = str(value).strip()
    if "." in text:
        suffix = text.rsplit(".", 1)[-1].strip()
        if suffix:
            return suffix
    return text


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if hasattr(value, "value"):
        value = value.value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "on", "enabled"}


def _format_qty(value: float) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def _format_price(value: float) -> float:
    return round(float(value), 2)


@dataclass(frozen=True)
class AlpacaPaperBroker:
    """
    Alpaca paper-only broker adapter.

    Entries and exits use simple fractional market orders. The strategy-level
    stop/target/VWAP/EMA/end-of-day logic is handled by the position monitor.
    """

    api_key: str
    secret_key: str
    paper: bool = True

    @classmethod
    def from_env(cls) -> "AlpacaPaperBroker":
        api_key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        paper_raw = os.environ.get("ALPACA_PAPER", "true").strip().lower()

        if not api_key:
            raise AlpacaConfigError("Missing environment variable: ALPACA_API_KEY")
        if not secret_key:
            raise AlpacaConfigError("Missing environment variable: ALPACA_SECRET_KEY")

        paper = paper_raw not in {"false", "0", "no", "live"}
        if not paper:
            raise AlpacaConfigError("ALPACA_PAPER is not true. This integration is paper-only.")

        return cls(api_key=api_key, secret_key=secret_key, paper=True)

    def client(self) -> TradingClient:
        return TradingClient(self.api_key, self.secret_key, paper=True)

    def account_snapshot(self) -> dict:
        data = _model_to_dict(self.client().get_account())
        wanted_keys = [
            "status",
            "currency",
            "cash",
            "buying_power",
            "regt_buying_power",
            "daytrading_buying_power",
            "non_marginable_buying_power",
            "portfolio_value",
            "equity",
            "last_equity",
            "long_market_value",
            "short_market_value",
            "initial_margin",
            "maintenance_margin",
            "trading_blocked",
            "transfers_blocked",
            "account_blocked",
            "pattern_day_trader",
            "daytrade_count",
            "multiplier",
        ]
        return {key: data.get(key, "") for key in wanted_keys}

    def open_positions(self) -> list[dict]:
        rows: list[dict] = []
        for pos in self.client().get_all_positions():
            data = _model_to_dict(pos)
            rows.append(
                {
                    "symbol": data.get("symbol", ""),
                    "asset_class": data.get("asset_class", ""),
                    "side": data.get("side", ""),
                    "qty": data.get("qty", ""),
                    "market_value": data.get("market_value", ""),
                    "cost_basis": data.get("cost_basis", ""),
                    "avg_entry_price": data.get("avg_entry_price", ""),
                    "current_price": data.get("current_price", ""),
                    "unrealized_pl": data.get("unrealized_pl", ""),
                    "unrealized_plpc": data.get("unrealized_plpc", ""),
                }
            )
        return rows

    def open_orders(self) -> list[dict]:
        client = self.client()
        try:
            from alpaca.trading.enums import QueryOrderStatus

            request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100)
            orders = client.get_orders(filter=request)
        except Exception:
            try:
                orders = client.get_orders()
            except Exception:
                orders = []

        return [self._order_snapshot(order) for order in orders]

    def _order_snapshot(self, order: Any) -> dict:
        data = _model_to_dict(order)
        return {
            "id": str(data.get("id", "")),
            "client_order_id": data.get("client_order_id", ""),
            "symbol": data.get("symbol", ""),
            "side": _value_text(data.get("side", "")),
            "order_type": _value_text(data.get("order_type", data.get("type", ""))),
            "order_class": _value_text(data.get("order_class", "")),
            "time_in_force": _value_text(data.get("time_in_force", "")),
            "status": _value_text(data.get("status", "")).lower(),
            "qty": data.get("qty", ""),
            "notional": data.get("notional", ""),
            "limit_price": data.get("limit_price", ""),
            "stop_price": data.get("stop_price", ""),
            "filled_qty": data.get("filled_qty", ""),
            "filled_avg_price": data.get("filled_avg_price", ""),
            "submitted_at": data.get("submitted_at", ""),
            "filled_at": data.get("filled_at", ""),
            "canceled_at": data.get("canceled_at", ""),
            "failed_at": data.get("failed_at", ""),
        }

    def get_order_snapshot(self, order_id: str) -> dict:
        if not order_id:
            return {}
        return self._order_snapshot(self.client().get_order_by_id(order_id))

    def wait_for_order_terminal(
        self,
        order_id: str,
        *,
        timeout_seconds: int = 30,
        poll_seconds: float = 1.0,
    ) -> dict:
        """
        Poll Alpaca until an order is terminal or the timeout expires.

        Market orders normally fill quickly, but local state must not be marked
        open/closed merely because Alpaca accepted the submission.
        """
        deadline = time.monotonic() + max(1, timeout_seconds)
        latest: dict = {}

        while time.monotonic() < deadline:
            latest = self.get_order_snapshot(order_id)
            status = str(latest.get("status", "")).lower()
            if status in TERMINAL_ORDER_STATUSES:
                return latest
            time.sleep(max(0.25, poll_seconds))

        if not latest:
            latest = self.get_order_snapshot(order_id)
        latest["poll_timed_out"] = True
        return latest

    def _switch_enabled(self, specific_env: str) -> bool:
        specific_raw = os.environ.get(specific_env)
        if specific_raw is not None and specific_raw.strip() != "":
            return _is_true(specific_raw)

        # Backward-compatible fallback to the existing global secret.
        global_raw = os.environ.get(GLOBAL_ORDER_SWITCH_ENV, "false")
        return _is_true(global_raw)

    def entry_submission_enabled(self) -> bool:
        return self._switch_enabled(ENTRY_ORDER_SWITCH_ENV)

    def exit_submission_enabled(self) -> bool:
        return self._switch_enabled(EXIT_ORDER_SWITCH_ENV)

    def assert_submission_enabled(self, side: str) -> None:
        if side == "entry":
            enabled = self.entry_submission_enabled()
            env_name = ENTRY_ORDER_SWITCH_ENV
        elif side == "exit":
            enabled = self.exit_submission_enabled()
            env_name = EXIT_ORDER_SWITCH_ENV
        else:
            enabled = self._switch_enabled(GLOBAL_ORDER_SWITCH_ENV)
            env_name = GLOBAL_ORDER_SWITCH_ENV

        if not enabled:
            raise AlpacaSafetyError(
                f"{env_name} is not enabled. No {side} order was submitted. "
                f"Set the specific switch, or the legacy {GLOBAL_ORDER_SWITCH_ENV}, to true."
            )

    def safety_snapshot(self, research_account_size: float = 120.0) -> dict:
        account = self.account_snapshot()
        return {
            "paper_only": True,
            "live_trading_enabled": False,
            "entry_submission_enabled": self.entry_submission_enabled(),
            "exit_submission_enabled": self.exit_submission_enabled(),
            "research_account_size": research_account_size,
            "max_position_value_research": round(research_account_size * 0.20, 2),
            "account_status": account.get("status", ""),
            "trading_blocked": account.get("trading_blocked", ""),
            "transfers_blocked": account.get("transfers_blocked", ""),
            "account_blocked": account.get("account_blocked", ""),
            "pattern_day_trader": account.get("pattern_day_trader", ""),
            "daytrade_count": account.get("daytrade_count", ""),
            "alpaca_buying_power_visible": _safe_float(account.get("buying_power")),
            "alpaca_cash_visible": _safe_float(account.get("cash")),
            "alpaca_portfolio_value_visible": _safe_float(account.get("portfolio_value")),
        }

    def assert_account_can_trade(self) -> None:
        account = self.account_snapshot()
        status_text = _value_text(account.get("status", "")).upper()
        if status_text != "ACTIVE":
            raise AlpacaSafetyError(
                f"Alpaca paper account status is not ACTIVE: {account.get('status')}"
            )

        blocked_values = {
            "trading_blocked": account.get("trading_blocked"),
            "account_blocked": account.get("account_blocked"),
            "transfers_blocked": account.get("transfers_blocked"),
        }
        if any(_is_true(value) for value in blocked_values.values()):
            raise AlpacaSafetyError(f"Alpaca paper account has a block flag: {blocked_values}")

    def submit_cancel_limit_order_test(
        self,
        *,
        symbol: str = "SPY",
        qty: float = 1.0,
        limit_price: float = 1.00,
        confirm: str,
    ) -> dict:
        if confirm != TEST_CONFIRM_VALUE:
            raise AlpacaSafetyError(f"Confirmation string did not match {TEST_CONFIRM_VALUE}.")

        self.assert_submission_enabled("test")
        self.assert_account_can_trade()

        request = LimitOrderRequest(
            symbol=symbol.upper().strip(),
            qty=_format_qty(qty),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=_format_price(limit_price),
        )
        client = self.client()
        submitted = client.submit_order(order_data=request)
        submitted_snapshot = self._order_snapshot(submitted)
        order_id = submitted_snapshot.get("id", "")

        cancel_result: dict = {}
        if order_id:
            try:
                client.cancel_order_by_id(order_id)
                cancel_result = self.wait_for_order_terminal(order_id, timeout_seconds=20)
            except Exception as exc:
                cancel_result = {"cancel_error": str(exc)}

        return {
            "test_type": "submit_cancel_low_limit_buy",
            "paper_only": True,
            "symbol": symbol.upper().strip(),
            "qty": _format_qty(qty),
            "limit_price": _format_price(limit_price),
            "submitted_order": submitted_snapshot,
            "final_order": cancel_result,
        }

    def submit_market_buy(
        self,
        *,
        symbol: str,
        qty: float,
        confirm: str,
        client_order_id: str | None = None,
    ) -> dict:
        if confirm != ENTRY_CONFIRM_VALUE:
            raise AlpacaSafetyError(f"Confirmation string did not match {ENTRY_CONFIRM_VALUE}.")
        if qty <= 0:
            raise AlpacaSafetyError(f"Quantity must be positive. Received: {qty}")

        self.assert_submission_enabled("entry")
        self.assert_account_can_trade()

        kwargs: dict[str, Any] = {
            "symbol": symbol.upper().strip(),
            "qty": _format_qty(qty),
            "side": OrderSide.BUY,
            "time_in_force": TimeInForce.DAY,
        }
        if client_order_id:
            kwargs["client_order_id"] = client_order_id

        submitted = self.client().submit_order(order_data=MarketOrderRequest(**kwargs))
        submitted_snapshot = self._order_snapshot(submitted)
        final_snapshot = self.wait_for_order_terminal(submitted_snapshot.get("id", ""))

        return {
            "paper_only": True,
            "order_kind": "simple_market_buy",
            "symbol": symbol.upper().strip(),
            "qty_requested": _format_qty(qty),
            "submitted_order": submitted_snapshot,
            "final_order": final_snapshot,
        }

    def submit_market_sell(
        self,
        *,
        symbol: str,
        qty: float,
        confirm: str,
        client_order_id: str | None = None,
    ) -> dict:
        if confirm != EXIT_CONFIRM_VALUE:
            raise AlpacaSafetyError(f"Confirmation string did not match {EXIT_CONFIRM_VALUE}.")
        if qty <= 0:
            raise AlpacaSafetyError(f"Quantity must be positive. Received: {qty}")

        self.assert_submission_enabled("exit")
        self.assert_account_can_trade()

        kwargs: dict[str, Any] = {
            "symbol": symbol.upper().strip(),
            "qty": _format_qty(qty),
            "side": OrderSide.SELL,
            "time_in_force": TimeInForce.DAY,
        }
        if client_order_id:
            kwargs["client_order_id"] = client_order_id

        submitted = self.client().submit_order(order_data=MarketOrderRequest(**kwargs))
        submitted_snapshot = self._order_snapshot(submitted)
        final_snapshot = self.wait_for_order_terminal(submitted_snapshot.get("id", ""))

        return {
            "paper_only": True,
            "order_kind": "simple_market_sell",
            "symbol": symbol.upper().strip(),
            "qty_requested": _format_qty(qty),
            "submitted_order": submitted_snapshot,
            "final_order": final_snapshot,
        }
