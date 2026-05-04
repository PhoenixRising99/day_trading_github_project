from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TradingConfig:
    account_size: float = 120.00
    risk_per_trade_pct: float = 0.010
    max_position_size_pct: float = 0.20
    max_daily_loss_pct: float = 0.020
    max_trades_per_day: int = 1
    max_open_positions: int = 1
    reward_risk_ratio: float = 1.35
    interval: str = "5m"
    period: str = "5d"
    allow_fractional_shares: bool = True
    slippage_pct: float = 0.0005
    commission_per_trade: float = 0.00
    timezone: str = "America/New_York"


@dataclass(frozen=True)
class StrategyParams:
    # Frozen V9/V10/V11 strategy parameters.
    min_score: int = 12
    rsi_min: float = 52
    rsi_max: float = 70
    min_volume_ratio: float = 1.25
    breakout_buffer: float = 0.0005
    min_vwap_distance_atr: float = 0.10
    max_vwap_distance_atr: float = 2.25
    min_close_location: float = 0.55
    require_macd_positive: bool = True
    require_macd_improving: bool = False
    require_market_filter: bool = True
    require_vwap_hold: bool = True
    require_vwap_slope: bool = True
    require_breakout: bool = False


CONFIG = TradingConfig()
STRATEGY_PARAMS = StrategyParams()
WATCHLIST = ["SPY", "AAPL", "MSFT"]

# Frozen strategy entry window: 10:15 AM to 10:59 AM ET.
ENTRY_START_HOUR = 10
ENTRY_START_MINUTE = 15
ENTRY_END_HOUR = 10
ENTRY_END_MINUTE = 59
