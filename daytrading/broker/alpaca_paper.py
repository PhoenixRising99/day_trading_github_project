from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from alpaca.trading.client import TradingClient


class AlpacaConfigError(RuntimeError):
    """Raised when Alpaca configuration is missing or unsafe."""


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


@dataclass(frozen=True)
class AlpacaPaperBroker:
    """
    Read-only Alpaca paper broker adapter for initial integration testing.

    This class intentionally does not submit orders. It only verifies that
    paper API credentials work and reads account/position/order state.
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
            raise AlpacaConfigError("ALPACA_PAPER is not true. This patch is intentionally paper-only.")

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
            from alpaca.trading.requests import GetOrdersRequest
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
                "symbol": data.get("symbol", ""),
                "side": data.get("side", ""),
                "order_type": data.get("order_type", data.get("type", "")),
                "time_in_force": data.get("time_in_force", ""),
                "status": data.get("status", ""),
                "qty": data.get("qty", ""),
                "notional": data.get("notional", ""),
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
            "order_submission_enabled": False,
            "live_trading_enabled": False,
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
