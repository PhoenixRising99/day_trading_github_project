from __future__ import annotations

import os
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


# Global kill switch, independent of ALPACA_PAPER. Set this GitHub
# Secret/Variable to "false" to immediately stop all order submission
# (entries AND exits) without touching API keys or paper mode. Defaults to
# enabled ("true") when unset so existing behavior is unaffected unless this
# is explicitly configured.
ORDER_SUBMISSION_ENABLED_ENV = "ALPACA_PAPER_ORDER_SUBMISSION_ENABLED"

ENTRY_CONFIRM_VALUE = "SUBMIT_ALPACA_PAPER_STRATEGY_ORDER"
EXIT_CONFIRM_VALUE = "SUBMIT_ALPACA_PAPER_EXIT_ORDER"
TEST_CONFIRM_VALUE = "SUBMIT_ALPACA_PAPER_TEST"


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
    """
    Normalize Alpaca SDK enum values and plain strings.

    alpaca-py may return account.status as AccountStatus.ACTIVE rather than
    the plain string "ACTIVE". Local safety checks should treat both as ACTIVE.
    """
    if value is None:
        return ""

    if hasattr(value, "value"):
        return str(value.value).strip()

    text = str(value).strip()

    # Handles strings like "AccountStatus.ACTIVE".
    if "." in text:
        maybe_enum_name = text.rsplit(".", 1)[-1]
        if maybe_enum_name:
            return maybe_enum_name.strip()

    return text


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if hasattr(value, "value"):
        value = value.value

    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _format_qty(value: float) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def _format_price(value: float) -> float:
    # Standard equity limit/stop prices need normal penny precision.
    return round(float(value), 2)


@dataclass(frozen=True)
class AlpacaPaperBroker:
    """
    Alpaca paper broker adapter.

    Safety scope:
    - Paper trading only.
    - No live endpoint support.
    - Order methods require explicit confirm strings from the caller.
    - Order submission can be globally disabled via ORDER_SUBMISSION_ENABLED_ENV
      without touching API keys or paper mode.

    Order design note (important):
    Alpaca does not support fractional share quantities on bracket/OCO
    orders -- fractional qty is only supported on simple market/limit/stop
    orders with time_in_force=Day. Because this research account's position
    sizing is intentionally fractional (see daytrading/strategy.py
    calculate_position_size, driven by a $24 max position value against
    multi-hundred-dollar stocks), entries here use a SIMPLE market order,
    never a bracket order. Stop-loss / take-profit / EMA-VWAP / end-of-day
    exits are handled separately by alpaca_paper_position_monitor.py, which
    also submits simple orders to close the position.
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
            raise AlpacaConfigError("Missing GitHub secret/environment variable: ALPACA_API_KEY")
        if not secret_key:
            raise AlpacaConfigError("Missing GitHub secret/environment variable: ALPACA_SECRET_KEY")

        paper = paper_raw not in {"false", "0", "no", "live"}
        if not paper:
            raise AlpacaConfigError("ALPACA_PAPER is not true. This integration is paper-only.")

        return cls(api_key=api_key, secret_key=secret_key, paper=True)

    def client(self) -> TradingClient:
        return TradingClient(self.api_key, self.secret_key, paper=True)

    def account_snapshot(self) -> dict:
        account = self.client().get_account()
        data = _model_to_dict(account)
        wanted_keys = [
            "status", "currency", "cash", "buying_power", "regt_buying_power",
            "daytrading_buying_power", "non_marginable_buying_power",
            "portfolio_value", "equity", "last_equity", "long_market_value",
            "short_market_value", "initial_margin", "maintenance_margin",
            "trading_blocked", "transfers_blocked", "account_blocked",
            "pattern_day_trader", "daytrade_count", "multiplier",
        ]
        return {key: data.get(key, "") for key in wanted_keys}

    def open_positions(self) -> list[dict]:
        positions = self.client().get_all_positions()
        rows = []
        for pos in positions:
            data = _model_to_dict(pos)
            rows.append({
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
            })
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

        rows = []
        for order in orders:
            data = _model_to_dict(order)
            rows.append({
                "id": str(data.get("id", "")),
                "symbol": data.get("symbol", ""),
                "side": data.get("side", ""),
                "order_type": data.get("order_type", data.get("type", "")),
                "order_class": data.get("order_class", ""),
                "time_in_force": data.get("time_in_force", ""),
                "status": data.get("status", ""),
                "qty": data.get("qty", ""),
                "notional": data.get("notional", ""),
                "limit_price": data.get("limit_price", ""),
                "stop_price": data.get("stop_price", ""),
                "filled_qty": data.get("filled_qty", ""),
                "submitted_at": data.get("submitted_at", ""),
                "filled_at": data.get("filled_at", ""),
                "client_order_id": data.get("client_order_id", ""),
            })
        return rows

    def safety_snapshot(self, research_account_size: float = 120.0) -> dict:
        account = self.account_snapshot()
        return {
            "paper_only": True,
            "live_trading_enabled": False,
            "order_submission_enabled": self.order_submission_enabled(),
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

    def order_submission_enabled(self) -> bool:
        raw = os.environ.get(ORDER_SUBMISSION_ENABLED_ENV, "true").strip().lower()
        return raw not in {"false", "0", "no"}

    def assert_order_submission_enabled(self) -> None:
        if not self.order_submission_enabled():
            raise AlpacaSafetyError(
                f"{ORDER_SUBMISSION_ENABLED_ENV} is set to disable order submission. "
                "No entry or exit orders will be sent until it is re-enabled."
            )

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

        blocked = any(_is_true(value) for value in blocked_values.values())
        if blocked:
            raise AlpacaSafetyError(f"Alpaca paper account has a block flag: {blocked_values}")

    def submit_cancel_limit_order_test(
        self,
        *,
        symbol: str = "SPY",
        qty: float = 1.0,
        limit_price: float = 1.00,
        confirm: str,
    ) -> dict:
        """
        Submit a low buy limit order in the paper account, then cancel it.

        This tests order submission/cancellation without intentionally creating
        a fill. The confirmation string must be exactly SUBMIT_ALPACA_PAPER_TEST.
        """
        if confirm != TEST_CONFIRM_VALUE:
            raise AlpacaSafetyError(f"Confirmation string did not match {TEST_CONFIRM_VALUE}.")

        self.assert_order_submission_enabled()
        self.assert_account_can_trade()

        order_request = LimitOrderRequest(
            symbol=symbol.upper().strip(),
            qty=_format_qty(qty),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=_format_price(limit_price),
        )

        client = self.client()
        submitted = client.submit_order(order_data=order_request)
        submitted_dict = _model_to_dict(submitted)

        order_id = submitted_dict.get("id")
        cancelled = None

        if order_id:
            try:
                cancelled = client.cancel_order_by_id(order_id)
            except Exception as exc:
                cancelled = {"cancel_error": str(exc)}

        return {
            "test_type": "submit_cancel_low_limit_buy",
            "paper_only": True,
            "symbol": symbol.upper().strip(),
            "qty": _format_qty(qty),
            "limit_price": _format_price(limit_price),
            "submitted_order": submitted_dict,
            "cancel_result": _model_to_dict(cancelled),
        }

    def submit_market_buy(
        self,
        *,
        symbol: str,
        qty: float,
        confirm: str,
        client_order_id: str | None = None,
    ) -> dict:
        """
        Submit a paper-only SIMPLE market buy (no bracket/OCO legs).

        Fractional qty is supported here because this is a simple order.
        Requires confirmation string SUBMIT_ALPACA_PAPER_STRATEGY_ORDER.
        Stop-loss/take-profit/strategy exits are handled by a separate
        position-monitor job, not by this order.
        """
        if confirm != ENTRY_CONFIRM_VALUE:
            raise AlpacaSafetyError(f"Confirmation string did not match {ENTRY_CONFIRM_VALUE}.")

        if qty <= 0:
            raise AlpacaSafetyError(f"Quantity must be positive. Received: {qty}")

        self.assert_order_submission_enabled()
        self.assert_account_can_trade()

        request_kwargs: dict[str, Any] = {
            "symbol": symbol.upper().strip(),
            "qty": _format_qty(qty),
            "side": OrderSide.BUY,
            "time_in_force": TimeInForce.DAY,
        }
        if client_order_id:
            request_kwargs["client_order_id"] = client_order_id

        order_request = MarketOrderRequest(**request_kwargs)
        submitted = self.client().submit_order(order_data=order_request)

        return {
            "paper_only": True,
            "order_kind": "simple_market_buy",
            "symbol": symbol.upper().strip(),
            "qty": _format_qty(qty),
            "submitted_order": _model_to_dict(submitted),
        }

    def submit_market_sell(
        self,
        *,
        symbol: str,
        qty: float,
        confirm: str,
        client_order_id: str | None = None,
    ) -> dict:
        """
        Submit a paper-only SIMPLE market sell to close (all or part of) a
        long position. Used by the position monitor for stop/target/EMA-VWAP/
        end-of-day exits. Requires confirmation string
        SUBMIT_ALPACA_PAPER_EXIT_ORDER.
        """
        if confirm != EXIT_CONFIRM_VALUE:
            raise AlpacaSafetyError(f"Confirmation string did not match {EXIT_CONFIRM_VALUE}.")

        if qty <= 0:
            raise AlpacaSafetyError(f"Quantity must be positive. Received: {qty}")

        self.assert_order_submission_enabled()
        self.assert_account_can_trade()

        request_kwargs: dict[str, Any] = {
            "symbol": symbol.upper().strip(),
            "qty": _format_qty(qty),
            "side": OrderSide.SELL,
            "time_in_force": TimeInForce.DAY,
        }
        if client_order_id:
            request_kwargs["client_order_id"] = client_order_id

        order_request = MarketOrderRequest(**request_kwargs)
        submitted = self.client().submit_order(order_data=order_request)

        return {
            "paper_only": True,
            "order_kind": "simple_market_sell",
            "symbol": symbol.upper().strip(),
            "qty": _format_qty(qty),
            "submitted_order": _model_to_dict(submitted),
        }
