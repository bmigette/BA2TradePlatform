from sqlmodel import Field, Session, SQLModel, Column, Relationship
from sqlalchemy import String, Float, JSON, UniqueConstraint, Table, Integer, ForeignKey
from sqlalchemy.orm import relationship
from typing import Optional, Dict, Any, List
from .types import InstrumentType, MarketAnalysisStatus, OrderType, OrderRecommendation, OrderStatus, OrderDirection, OrderOpenType, ExpertEventRuleType, AnalysisUseCase, RiskLevel, TimeHorizon, TransactionStatus
from datetime import datetime as DateTime, timezone

# Association table for many-to-many relationship between Ruleset and EventAction
class RulesetEventActionLink(SQLModel, table=True):
    __tablename__ = "ruleset_eventaction_link"
    
    ruleset_id: int = Field(foreign_key="ruleset.id", primary_key=True, ondelete="CASCADE")
    eventaction_id: int = Field(foreign_key="eventaction.id", primary_key=True, ondelete="CASCADE")
    order_index: int = Field(default=0, description="Order of the rule in the ruleset (0-based)")

class AppSetting(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    key: str 
    value_str: str | None 
    value_json:  Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict)
    value_float: float | None 

class ExpertInstance(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", nullable=False, ondelete="CASCADE")
    expert: str     
    enabled: bool = Field(default=True)
    alias: str | None = Field(default=None, max_length=100, description="Short display name for the expert (max 100 chars)")
    user_description: str | None = Field(default=None, description="Detailed notes about this expert instance")
    virtual_equity_pct: float = Field(default=100.0)
    enter_market_ruleset_id: int | None = Field(default=None, foreign_key="ruleset.id", nullable=True, ondelete="SET NULL")
    open_positions_ruleset_id: int | None = Field(default=None, foreign_key="ruleset.id", nullable=True, ondelete="SET NULL")

class ExpertSetting(SQLModel, table=True):
    __table_args__ = (UniqueConstraint('instance_id', 'key', name='uix_expertsetting_instanceid_key'),)
    id: int | None = Field(default=None, primary_key=True)
    instance_id: int = Field(foreign_key="expertinstance.id", nullable=False, ondelete="CASCADE")
    key: str
    value_str: str | None 
    value_json:  Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict)
    value_float: float | None 

class AccountSetting(SQLModel, table=True):
    __table_args__ = (UniqueConstraint('account_id', 'key', name='uix_accountsetting_accountid_key'),)
    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", nullable=False, ondelete="CASCADE")
    key: str
    value_str: str | None 
    value_json:  Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict)
    value_float: float | None 

class AccountDefinition(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str 
    provider: str 
    description: str | None 


class Ruleset(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    description: str | None = Field(default=None)
    type: ExpertEventRuleType = Field(default=ExpertEventRuleType.TRADING_RECOMMENDATION_RULE)
    subtype: AnalysisUseCase | None = Field(default=None)
    # Many-to-many relationship with EventAction (ordered by order_index)
    event_actions: List["EventAction"] = Relationship(
        back_populates="rulesets", 
        link_model=RulesetEventActionLink,
        sa_relationship_kwargs={"order_by": "RulesetEventActionLink.order_index"}
    )


class EventAction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    type: ExpertEventRuleType
    subtype: AnalysisUseCase | None = Field(default=None)
    name: str
    triggers: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict)
    actions: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict) 
    extra_parameters: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict)
    continue_processing: bool = Field(default=False)
    # Many-to-many relationship with Ruleset
    rulesets: List["Ruleset"] = Relationship(
        back_populates="event_actions", 
        link_model=RulesetEventActionLink
    )

class ExpertRecommendation(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    instance_id: int = Field(foreign_key="expertinstance.id", nullable=False, ondelete="CASCADE")
    market_analysis_id: int | None = Field(foreign_key="marketanalysis.id", nullable=True, ondelete="CASCADE")
    symbol: str
    recommended_action: OrderRecommendation
    expected_profit_percent: float
    price_at_date: float
    details: str | None
    confidence: float | None
    risk_level: RiskLevel = Field(description="LOW|MEDIUM|HIGH")
    time_horizon: TimeHorizon = Field(description="SHORT_TERM|MEDIUM_TERM|LONG_TERM")
    created_at: DateTime | None = Field(default_factory=lambda: DateTime.now(timezone.utc))
    
    # Relationship back to market analysis
    market_analysis: Optional["MarketAnalysis"] = Relationship(back_populates="expert_recommendations")
    
    # Relationship to trade action results
    trade_action_results: List["TradeActionResult"] = Relationship(back_populates="expert_recommendation")


class MarketAnalysis(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    symbol: str
    expert_instance_id: int = Field(foreign_key="expertinstance.id", nullable=False, ondelete="CASCADE")
    status: MarketAnalysisStatus = MarketAnalysisStatus.PENDING
    subtype: AnalysisUseCase = Field(default=AnalysisUseCase.ENTER_MARKET)
    state: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict)
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc))
    
    # Relationships
    analysis_outputs: List["AnalysisOutput"] = Relationship(back_populates="market_analysis")
    expert_recommendations: List["ExpertRecommendation"] = Relationship(back_populates="market_analysis")
    
    def __init__(self, **data):
        """Ensure state is always a dict, never None."""
        super().__init__(**data)
        if self.state is None:
            self.state = {}


class AnalysisOutput(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc))
    
    # Optional link to market analysis (nullable for standalone provider outputs)
    market_analysis_id: int | None = Field(default=None, foreign_key="marketanalysis.id", nullable=True, ondelete="CASCADE")
    
    # Provider identification
    provider_category: str | None = Field(default=None, description="Provider category (news, indicators, fundamentals, etc.)")
    provider_name: str | None = Field(default=None, description="Provider name (alpaca, yfinance, alphavantage, etc.)")
    
    # Data identification
    name: str = Field(description="Output name/identifier (e.g., 'AAPL_news_2025-10-08')")
    type: str = Field(description="Output type (news, fundamentals, indicator, report, etc.)")
    
    # Data content
    text: str | None = Field(default=None, description="Text content (markdown or JSON string)")
    blob: bytes | None = Field(default=None, description="Binary data if needed")
    
    # Metadata for caching and reuse
    symbol: str | None = Field(default=None, description="Stock symbol if applicable")
    start_date: DateTime | None = Field(default=None, description="Date range start")
    end_date: DateTime | None = Field(default=None, description="Date range end")
    format_type: str | None = Field(default=None, description="Format type: 'dict' or 'markdown'")
    
    # Additional provider metadata (renamed from 'metadata' to avoid SQLAlchemy conflict)
    provider_metadata: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict, description="Provider-specific metadata")
    
    # Relationship back to market analysis
    market_analysis: Optional[MarketAnalysis] = Relationship(back_populates="analysis_outputs")



class Transaction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    symbol: str
    quantity: float
    open_price: float | None = Field(default=None)
    close_price: float | None = Field(default=None)
    stop_loss: float | None = Field(default=None)
    take_profit: float | None = Field(default=None)
    open_date: DateTime | None = Field(default=None)
    close_date: DateTime | None = Field(default=None)
    status: TransactionStatus = Field(default=TransactionStatus.WAITING)
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc))
    
    # Optional reference to expert instance for tracking which expert initiated this transaction
    expert_id: int | None = Field(foreign_key="expertinstance.id", nullable=True, ondelete="SET NULL")
    
    # Relationship to trading orders (1:many - one transaction can have multiple orders)
    trading_orders: List["TradingOrder"] = Relationship(back_populates="transaction")

    def as_string(self) -> str:
        return f"Transaction(id={self.id}, symbol={self.symbol}, quantity={self.quantity}, status={self.status}, open_price={self.open_price}, close_price={self.close_price})"
    
    def __repr__(self) -> str:
        return self.as_string()
    
    def __str__(self) -> str:
        return self.as_string()
    
    def get_current_open_qty(self) -> float:
        """
        Get the total quantity of filled orders (BUY/SELL) for this transaction.
        Does not include limit/stop orders that haven't been filled yet.
        
        Returns:
            float: Total filled quantity (positive for longs, negative for shorts)
        """
        from .db import get_db
        from sqlmodel import Session, select
        
        total_qty = 0.0
        
        with Session(get_db().bind) as session:
            # Get all orders for this transaction
            statement = select(TradingOrder).where(
                TradingOrder.transaction_id == self.id
            )
            orders = session.exec(statement).all()
            
            for order in orders:
                # Only count filled orders
                if order.status in OrderStatus.get_executed_statuses() and order.filled_qty:
                    # Add to total based on side (BUY is positive, SELL is negative)
                    if order.side == OrderDirection.BUY:
                        total_qty += order.filled_qty
                    elif order.side == OrderDirection.SELL:
                        total_qty -= order.filled_qty
        
        return total_qty
    
    def get_pending_open_qty(self) -> float:
        """
        Get the total quantity of pending unfilled orders (BUY/SELL) for this transaction.
        Includes orders that are open but not yet filled.
        
        Returns:
            float: Total pending quantity (positive for buy orders, negative for sell orders)
        """
        from .db import get_db
        from sqlmodel import Session, select
        
        total_qty = 0.0
        
        with Session(get_db().bind) as session:
            # Get all orders for this transaction
            statement = select(TradingOrder).where(
                TradingOrder.transaction_id == self.id
            )
            orders = session.exec(statement).all()
            
            for order in orders:
                # Only count unfilled orders (excluding take profit and stop loss orders)
                if order.status in OrderStatus.get_unfilled_statuses():
                    # Skip dependent orders (TP/SL orders)
                    if order.depends_on_order is not None:
                        continue
                    
                    # Calculate remaining quantity
                    remaining_qty = order.quantity
                    if order.filled_qty:
                        remaining_qty -= order.filled_qty
                    
                    if remaining_qty > 0:
                        # Add to total based on side (BUY is positive, SELL is negative)
                        if order.side == OrderDirection.BUY:
                            total_qty += remaining_qty
                        elif order.side == OrderDirection.SELL:
                            total_qty -= remaining_qty
        
        return total_qty
    
    def get_current_open_equity(self, account_interface=None) -> float:
        """
        Calculate the equity value of currently filled orders.
        Uses the open_price from filled orders.
        
        Args:
            account_interface: Optional account interface for getting current market price
        
        Returns:
            float: Total equity value of filled positions
        """
        from .db import get_db
        from sqlmodel import Session, select
        from ..logger import logger
        
        total_equity = 0.0
        
        with Session(get_db().bind) as session:
            # Get all orders for this transaction
            statement = select(TradingOrder).where(
                TradingOrder.transaction_id == self.id
            )
            orders = session.exec(statement).all()
            
            # logger.debug(f"Transaction {self.id}.get_current_open_equity(): Found {len(orders)} orders")
            
            for order in orders:
                # Only count filled orders
                if order.status in OrderStatus.get_executed_statuses() and order.filled_qty:
                    # Use open_price for filled orders
                    price = order.open_price
                    
                    if price:
                        # Calculate value: quantity × price
                        equity = abs(order.filled_qty) * price
                        total_equity += equity
                        # logger.debug(f"  Order {order.id}: filled_qty={order.filled_qty}, price={price}, equity=${equity:.2f}")
                    # else:
                    #     logger.debug(f"  Order {order.id}: No price available (open_price={order.open_price})")
                # else:
                #     logger.debug(f"  Order {order.id}: Skipped (status={order.status}, filled_qty={order.filled_qty})")
            
            # logger.debug(f"Transaction {self.id}.get_current_open_equity(): Total = ${total_equity:.2f}")
        
        return total_equity
    
    def get_pending_open_equity(self, account_interface=None) -> float:
        """
        Calculate the equity value of pending unfilled orders.
        Uses market price for pending orders.
        
        Args:
            account_interface: Account interface for getting current market price (required)
        
        Returns:
            float: Total equity value of pending orders
        """
        from .db import get_db
        from sqlmodel import Session, select
        from ..logger import logger
        
        total_equity = 0.0
        
        # Get market price if account interface provided
        market_price = None
        if account_interface:
            try:
                market_price = account_interface.get_instrument_current_price(self.symbol)
                # logger.debug(f"Transaction {self.id}.get_pending_open_equity(): Market price for {self.symbol} = ${market_price}")
            except Exception as e:
                logger.debug(f"Transaction {self.id}.get_pending_open_equity(): Could not get market price: {e}")
        
        # If we don't have market price, we can't calculate pending equity accurately
        if not market_price:
            # logger.debug(f"Transaction {self.id}.get_pending_open_equity(): No market price available, returning 0")
            return 0.0
        
        with Session(get_db().bind) as session:
            # Get all orders for this transaction
            statement = select(TradingOrder).where(
                TradingOrder.transaction_id == self.id
            )
            orders = session.exec(statement).all()
            
            # logger.debug(f"Transaction {self.id}.get_pending_open_equity(): Found {len(orders)} orders")
            
            for order in orders:
                # Only count unfilled orders (excluding TP/SL orders)
                if order.status in OrderStatus.get_unfilled_statuses():
                    # Skip dependent orders (TP/SL orders)
                    if order.depends_on_order is not None:
                        # logger.debug(f"  Order {order.id}: Skipped (dependent order)")
                        continue
                    
                    # Skip limit orders (SELL_LIMIT, BUY_LIMIT) - these are take profit orders
                    from .types import OrderType
                    if order.order_type in [OrderType.SELL_LIMIT, OrderType.BUY_LIMIT]:
                        # logger.debug(f"  Order {order.id}: Skipped (take profit order, type={order.order_type})")
                        continue
                    
                    # Calculate remaining quantity
                    remaining_qty = order.quantity
                    if order.filled_qty:
                        remaining_qty -= order.filled_qty
                    
                    if remaining_qty > 0:
                        # Use market price for pending orders
                        equity = abs(remaining_qty) * market_price
                        total_equity += equity
                        # logger.debug(f"  Order {order.id}: remaining_qty={remaining_qty}, market_price=${market_price}, equity=${equity:.2f}")
                # else:
                #     logger.debug(f"  Order {order.id}: Skipped (status={order.status})")
            
            # logger.debug(f"Transaction {self.id}.get_pending_open_equity(): Total = ${total_equity:.2f}")
        
        return total_equity


class TradingOrder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # REMOVED order_id: str | None
    account_id: int = Field(foreign_key="accountdefinition.id", nullable=False, ondelete="CASCADE")
    symbol: str
    quantity: float
    side: OrderDirection 
    order_type: OrderType
    good_for: str | None
    status: OrderStatus = OrderStatus.UNKNOWN
    filled_qty: float | None
    open_price: float | None = Field(default=None, description="Price at which the order opened (for filled orders)")
    comment: str | None
    created_at: DateTime | None = Field(default_factory=lambda: DateTime.now(timezone.utc))
    
    # New fields
    open_type: OrderOpenType = Field(default=OrderOpenType.AUTOMATIC)
    broker_order_id: str | None = Field(default=None, description="Broker-specific order ID for tracking")
    expert_recommendation_id: int | None = Field(default=None, foreign_key="expertrecommendation.id", ondelete="SET NULL", description="Expert recommendation that generated this order")
    limit_price: float | None = Field(default=None, description="Limit price for limit orders")
    stop_price: float | None = Field(default=None, description="Stop price for stop orders")

    # Dependency fields for order chaining
    depends_on_order: int | None = Field(default=None, foreign_key="tradingorder.id", ondelete="SET NULL", description="ID of another order this order depends on")
    depends_order_status_trigger: OrderStatus | None = Field(default=None, description="Status that the depends_on_order must reach to trigger this order")
    
    # Many:1 relationship with Transaction (many orders can belong to one transaction)
    transaction_id: int | None = Field(foreign_key="transaction.id", nullable=True, ondelete="CASCADE")
    transaction: Optional["Transaction"] = Relationship(back_populates="trading_orders")
    
    # Self-referencing relationship for order dependencies
    dependent_orders: List["TradingOrder"] = Relationship(
        back_populates="depends_on_order_rel",
        sa_relationship_kwargs={"remote_side": "TradingOrder.id"}
    )
    depends_on_order_rel: Optional["TradingOrder"] = Relationship(
        back_populates="dependent_orders"
    )
    
    # Optional JSON data field for storing order metadata
    # For WAITING_TRIGGER TP/SL orders: stores {"tp_percent": float, "parent_filled_price": float}
    # Allows recalculation of TP/SL from parent order fill price when order is triggered
    data: dict | None = Field(sa_column=Column(JSON), default=None, description="Optional order metadata (e.g., TP/SL percent for WAITING_TRIGGER orders)")

    def as_string(self) -> str:
        return f"Order(id={self.id}, symbol={self.symbol}, quantity={self.quantity}, side={self.side}, type={self.order_type}, status={self.status})"
    
    def __repr__(self) -> str:
        return self.as_string()
    
    def __str__(self) -> str:
        return self.as_string()



class Instrument(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str 
    company_name: str | None = Field(default=None)
    instrument_type: InstrumentType | None = Field(default=None)
    categories: list[str] = Field(sa_column=Column(JSON), default_factory=list)
    labels: list[str] = Field(sa_column=Column(JSON), default_factory=list)
    def __str__(self):
        return f"{self.name} ({self.instrument_type})"

class Position(SQLModel, table=True):
    """A class representing a trading position.

    This class models a financial position with attributes tracking various aspects of the position
    including price, quantity, profit/loss metrics, and other trading-related information.

    Attributes:
        id (int | None): Primary key for database record identification.
        asset_class (str): Name of the asset's asset class.
        avg_entry_price (float): The average entry price of the position.
        avg_entry_swap_rate (float | None): The average exchange rate the price was converted into the local currency at.
        change_today (float): Percent change from last day's price.
        cost_basis (float): Total cost basis in dollars.
        current_price (float): Current asset price per share.
        exchange (str): Exchange name of the asset.
        lastday_price (float): Last day's asset price per share based on the closing value of the last trading day.
        market_value (float): Total dollar amount of the position.
        qty (float): The number of shares of the position.
        qty_available (float): Total number of shares available minus open orders.
        side (OrderDirection): "long" or "short" representing the side of the position.
        swap_rate (float | None): Exchange rate (without mark-up) used to convert the price into local currency or crypto asset.
        symbol (str): Symbol of the asset.
        unrealized_intraday_pl (float): Unrealized profit/loss in dollars for the day.
        unrealized_intraday_plpc (float): Unrealized profit/loss percent for the day.
        unrealized_pl (float): Unrealized profit/loss in dollars.
        unrealized_plpc (float): Unrealized profit/loss percent.
    """
    id: int | None = Field(default=None, primary_key=True)
    asset_class: str
    avg_entry_price: float
    avg_entry_swap_rate: float | None
    change_today: float
    cost_basis: float
    current_price: float
    exchange: str
    lastday_price: float
    market_value: float
    qty: float
    qty_available: float
    side: OrderDirection
    swap_rate: float | None
    symbol: str
    unrealized_intraday_pl: float
    unrealized_intraday_plpc: float
    unrealized_pl: float
    unrealized_plpc: float


class TradeActionResult(SQLModel, table=True):
    """Model to store the results of TradeAction executions."""
    __tablename__ = "trade_action_result"
    
    id: int | None = Field(default=None, primary_key=True)
    
    # Action details
    action_type: str = Field(description="Type of action executed (buy, sell, close, etc.)")
    success: bool = Field(description="Whether the action was successful")
    message: str = Field(description="Human-readable message about the action result")
    data: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict, description="Additional data from the action")
    
    # Timestamps
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), description="When the action was executed")
    
    # Foreign key relationships (expert_recommendation_id is required - all actions come from recommendations)
    expert_recommendation_id: int = Field(foreign_key="expertrecommendation.id", nullable=False, ondelete="CASCADE", description="Expert recommendation that triggered this action")
    
    # Relationships
    expert_recommendation: "ExpertRecommendation" = Relationship(back_populates="trade_action_results")