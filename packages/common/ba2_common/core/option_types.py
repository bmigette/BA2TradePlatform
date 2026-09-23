"""Broker-agnostic option value objects (pure dataclasses, no DB/SDK deps)."""
from dataclasses import dataclass, field
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
    #: Contracts traded IN the decision's data session (``option_session.session_volume``):
    #: the bar dated exactly that session, else 0. Never an older session's volume.
    volume: Optional[int] = None
    #: Price change per 1 percentage point of the risk-free rate (the vega-per-vol-point
    #: convention). Appended LAST so positional constructions keep their meaning.
    rho: Optional[float] = None
    #: The instant the source stamped this row's QUOTE (zone-aware), when it publishes one:
    #: Alpaca's ``latestQuote.t`` live. None in a backtest -- the as-of bar is an end-of-session
    #: record with no quote instant, and inventing its close time would read as a measurement.
    #: Appended LAST (positional constructions unchanged). Read by the option trade record.
    quote_time: Optional[datetime] = None
    #: Where THIS ROW's iv/greeks were read from, when the source says: ``"broker"`` (the
    #: live Alpaca snapshot), ``"bs_from_close"`` (Black-Scholes inverted from the as-of bar's
    #: close), ``"chain_snapshot"`` (the sqlite store's build-time chain row, used when the bar
    #: has no computed iv). None = the row does not say; the option trade record then uses the
    #: account's declared ``OPTION_GREEKS_SOURCE``. Appended LAST.
    greeks_source: Optional[str] = None

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
    #: Appended LAST (positional constructions unchanged). Same meaning as on OptionContract.
    rho: Optional[float] = None
    volume: Optional[int] = None

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
    #: The chain contract this leg was CHOSEN from, when a builder chose one -- the quote and
    #: greeks the decision saw, which ``_submit_option_order`` writes into the order's
    #: ``entry_record`` (``option_trade_record``). IN-MEMORY ONLY: never persisted, never sent
    #: to a broker, excluded from equality and repr so a leg compares exactly as before. None
    #: for a leg built without a chain contract (closes, a roll's buy-back leg).
    quote: Optional[OptionContract] = field(default=None, compare=False, repr=False)


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
