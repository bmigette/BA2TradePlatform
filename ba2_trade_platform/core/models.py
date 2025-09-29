from sqlmodel import  Field, Session, SQLModel, create_engine, Column, Relationship
from sqlalchemy import String, Float, JSON, UniqueConstraint, Table, Integer, ForeignKey
from sqlalchemy.orm import relationship
from typing import Optional, Dict, Any, List
from .types import InstrumentType, MarketAnalysisStatus, OrderType, OrderRecommendation, OrderStatus, OrderDirection, OrderOpenType, ExpertEventRuleType, AnalysisUseCase, RiskLevel, TimeHorizon, TransactionStatus
from datetime import datetime as DateTime, timezone

# Association table for many-to-many relationship between Ruleset and EventAction
class RulesetEventActionLink(SQLModel, table=True):
    __tablename__ = "ruleset_eventaction_link"
    
    ruleset_id: int = Field(foreign_key="ruleset.id", primary_key=True)
    eventaction_id: int = Field(foreign_key="eventaction.id", primary_key=True)
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
    user_description: str | None = Field(default=None)
    virtual_equity_pct: float = Field(default=100.0)

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
    
    # Relationship to transactions
    transactions: List["Transaction"] = Relationship(back_populates="expert_recommendation")
    
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


class AnalysisOutput(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc))
    market_analysis_id: int = Field(foreign_key="marketanalysis.id", nullable=False, ondelete="CASCADE")
    name: str
    type: str
    text: str | None = Field(default=None)
    blob: bytes | None = Field(default=None)
    
    # Relationship back to market analysis
    market_analysis: MarketAnalysis = Relationship(back_populates="analysis_outputs")



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
    
    # Relationship to expert recommendation
    expert_recommendation_id: int | None = Field(foreign_key="expertrecommendation.id", nullable=True, ondelete="SET NULL")
    expert_recommendation: Optional["ExpertRecommendation"] = Relationship(back_populates="transactions")
    
    # Relationship to trading orders (1:many - one transaction can have multiple orders)
    trading_orders: List["TradingOrder"] = Relationship(back_populates="transaction")
    
    # Relationship to trade action results (1:many - one transaction can have multiple action results)
    trade_action_results: List["TradeActionResult"] = Relationship(back_populates="transaction")

    def as_string(self) -> str:
        return f"Transaction(id={self.id}, symbol={self.symbol}, quantity={self.quantity}, status={self.status}, open_price={self.open_price}, close_price={self.close_price})"
    
    def __repr__(self) -> str:
        return self.as_string()
    
    def __str__(self) -> str:
        return self.as_string()


class TradingOrder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # REMOVED order_id: str | None
    symbol: str
    quantity: float
    side: OrderDirection 
    order_type: OrderType
    good_for: str | None
    status: OrderStatus = OrderStatus.UNKNOWN
    filled_qty: float | None
    comment: str | None
    created_at: DateTime | None = Field(default_factory=lambda: DateTime.now(timezone.utc))
    
    # New fields
    open_type: OrderOpenType = Field(default=OrderOpenType.MANUAL)
    broker_order_id: str | None = Field(default=None, description="Broker-specific order ID for tracking")
    order_recommendation_id: int | None = Field(default=None, foreign_key="expertrecommendation.id", description="Expert recommendation that generated this order")
    limit_price: float | None = Field(default=None, description="Limit price for limit orders")
    
    # Dependency fields for order chaining
    depends_on_order: int | None = Field(default=None, foreign_key="tradingorder.id", description="ID of another order this order depends on")
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

    def as_string(self) -> str:
        return f"Order(id={self.id}, symbol={self.symbol}, quantity={self.quantity}, side={self.side}, type={self.order_type}, status={self.status})"
    
    def __repr__(self) -> str:
        return self.as_string()
    
    def __str__(self) -> str:
        return self.as_string()



class Instrument(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str 
    company_name: str
    instrument_type: InstrumentType
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
    
    # Foreign key relationships
    transaction_id: int | None = Field(default=None, foreign_key="transaction.id")
    expert_recommendation_id: int | None = Field(default=None, foreign_key="expertrecommendation.id")
    
    # Relationships
    transaction: Optional["Transaction"] = Relationship(back_populates="trade_action_results")
    expert_recommendation: Optional["ExpertRecommendation"] = Relationship(back_populates="trade_action_results")