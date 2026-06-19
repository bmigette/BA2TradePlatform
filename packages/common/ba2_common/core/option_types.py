"""Broker-agnostic option value objects (pure dataclasses, no DB/SDK deps)."""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from ba2_common.core.types import OptionRight, OrderDirection


@dataclass
class OptionContract:
    """One row of an option chain (quote + Greeks + liquidity)."""
    symbol: str                       # OCC contract symbol
    underlying: str
    option_type: OptionRight
    strike: float
    expiry: date
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    implied_volatility: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    open_interest: Optional[int] = None
    volume: Optional[int] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return round((self.bid + self.ask) / 2, 4)
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        m = self.mid
        if not m:
            return None
        return (self.ask - self.bid) / m * 100


@dataclass
class OptionQuote:
    """Latest quote + Greeks for a single contract."""
    symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    implied_volatility: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    timestamp: Optional[datetime] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return round((self.bid + self.ask) / 2, 4)
        return None


@dataclass
class OptionLeg:
    """One leg of an option order. ratio_qty multiplies the order quantity."""
    contract_symbol: str
    side: OrderDirection
    ratio_qty: int = 1
    position_intent: Optional[str] = None     # buy_to_open / sell_to_open / ...
    option_type: Optional[OptionRight] = None
    strike: Optional[float] = None
    expiry: Optional[date] = None
    underlying: Optional[str] = None


@dataclass
class OptionPosition:
    """A held option position (broker-agnostic)."""
    contract_symbol: str
    underlying: str
    option_type: OptionRight
    strike: float
    expiry: date
    side: OrderDirection                       # BUY = long, SELL = short
    quantity: float                            # number of contracts (positive)
    avg_entry_price: float                     # premium per share
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pl: Optional[float] = None
    multiplier: int = 100
