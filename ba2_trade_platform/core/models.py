from sqlmodel import  Field, Session, SQLModel, create_engine, Column, Relationship
from sqlalchemy import String, Float, JSON, UniqueConstraint, Table, Integer, ForeignKey
from typing import Optional, Dict, Any, List
from .types import InstrumentType, OrderStatus, OrderDirection, ExpertEventRuleType
from datetime import datetime, timezone

# Association table for many-to-many relationship between Ruleset and EventAction
class RulesetEventActionLink(SQLModel, table=True):
    __tablename__ = "ruleset_eventaction_link"
    
    ruleset_id: int = Field(foreign_key="ruleset.id", primary_key=True)
    eventaction_id: int = Field(foreign_key="eventaction.id", primary_key=True)

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
    virtual_equity: float = Field(default=100.0)

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
    # Many-to-many relationship with EventAction
    event_actions: List["EventAction"] = Relationship(
        back_populates="rulesets", 
        link_model=RulesetEventActionLink
    )


class EventAction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    type: ExpertEventRuleType
    subtype: str | None 
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
    symbol: str
    recommended_action: OrderDirection
    expected_profit_percent: float
    price_at_date: float
    details: str | None
    confidence: float | None
    created_at: datetime | None = Field(default_factory=lambda: datetime.now(timezone.utc))



class TradingOrder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    order_id: str | None
    symbol: str
    quantity: float
    side: str 
    order_type: str
    good_for: str | None
    limit_price: float | None
    stop_price: float | None
    status: OrderStatus = OrderStatus.UNKNOWN
    filled_qty: float | None
    client_order_id: str | None
    created_at: datetime | None = Field(default_factory=datetime.utcnow)

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