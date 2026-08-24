from sqlmodel import Field, Session, SQLModel, Column, Relationship
from sqlalchemy import String, Float, JSON, UniqueConstraint, Table, Integer, ForeignKey
from sqlalchemy.orm import relationship
from typing import Optional, Dict, Any, List
from ba2_common.core.types import InstrumentType, MarketAnalysisStatus, OrderType, OrderRecommendation, OrderStatus, OrderDirection, OrderOpenType, ExpertEventRuleType, AnalysisUseCase, RiskLevel, TimeHorizon, TransactionStatus, ActivityLogSeverity, ActivityLogType, AssetClass, OptionRight
from datetime import datetime as DateTime, timezone, date

# Register the native as_of cache index table so init_db()'s create_all sees it
# (Amendment A4: model lives here, the DB is host-owned; no migrator in-package).
from ba2_common.core.provider_cache_model import ProviderCache  # noqa: F401

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
    instance_id: int = Field(foreign_key="expertinstance.id", nullable=False, ondelete="CASCADE", index=True)
    market_analysis_id: int | None = Field(foreign_key="marketanalysis.id", nullable=True, ondelete="CASCADE", index=True)
    symbol: str = Field(index=True)
    recommended_action: OrderRecommendation
    expected_profit_percent: float
    price_at_date: float
    target_price: float | None = Field(default=None, description="Expert's recommended target/take-profit price (None -> derive from expected_profit_percent)")
    min_take_profit_percent: float = Field(
        default=2.0,
        description="Floor for how close to entry a take-profit may be set, as %% of the REAL "
                    "fill price (not the signal-time price_at_date) -- enforced by "
                    "AdjustTakeProfitAction regardless of which reference_value produced the "
                    "computed TP. Mirrors min_stop_loss_pct's role on the SL side. Snapshotted "
                    "onto the recommendation (not read live) so backtest and live share the same "
                    "mechanism without a live-only expert-instance lookup from TradeActions.py.",
    )
    details: str | None
    confidence: float | None
    risk_level: RiskLevel = Field(description="LOW|MEDIUM|HIGH")
    time_horizon: TimeHorizon = Field(description="SHORT_TERM|MEDIUM_TERM|LONG_TERM")
    # Which analysis use-case produced this rec (ENTER_MARKET vs OPEN_POSITIONS). Nullable for
    # backward-compat: legacy rows + writers that don't stamp it read as None, so the live
    # OPEN_POSITIONS manage-pass selection MUST keep its all-rec fallback for those. Mirrors
    # MarketAnalysis.subtype (stored by enum NAME). See unification plan gap #5/#15.
    subtype: AnalysisUseCase | None = Field(default=None)
    data: Dict[str, Any] | None = Field(default=None, sa_column=Column(JSON), description="Expert-specific data (nested by expert name)")
    created_at: DateTime | None = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)

    # Relationship back to market analysis
    market_analysis: Optional["MarketAnalysis"] = Relationship(back_populates="expert_recommendations")
    
    # Relationship to trade action results
    trade_action_results: List["TradeActionResult"] = Relationship(back_populates="expert_recommendation")


class MarketAnalysis(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    expert_instance_id: int = Field(foreign_key="expertinstance.id", nullable=False, ondelete="CASCADE", index=True)
    status: MarketAnalysisStatus = Field(default=MarketAnalysisStatus.PENDING, index=True)
    subtype: AnalysisUseCase = Field(default=AnalysisUseCase.ENTER_MARKET)
    state: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict)
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)
    
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
    symbol: str = Field(index=True)
    quantity: float  # Always positive - use side field to determine LONG/SHORT
    side: OrderDirection  # BUY for LONG positions, SELL for SHORT positions
    open_price: float | None = Field(default=None)
    close_price: float | None = Field(default=None)
    stop_loss: float | None = Field(default=None)
    take_profit: float | None = Field(default=None)
    # Manual override locks: when True, non-manual adjustment sources
    # (ruleset, smart_risk_manager, expert) must NOT change the corresponding
    # value. Cleared by an explicit "revert" action from the UI.
    tp_manual_override: bool = Field(default=False)
    sl_manual_override: bool = Field(default=False)
    open_date: DateTime | None = Field(default=None)
    close_date: DateTime | None = Field(default=None)
    close_reason: str | None = Field(default=None, description="Reason for closing (tp_sl_filled, manual_close, smart_risk_manager, broker_closed, etc.)")
    status: TransactionStatus = Field(default=TransactionStatus.WAITING, index=True)
    # Contract multiplier for P&L/value math: 100 for standard options, null (=1) for
    # equity. Populated from the originating order so option premium scales correctly.
    multiplier: int | None = Field(default=None, description="Contract multiplier (100 for standard options; null/1 for equity)")
    # --- The option INTENT. The transaction says WHAT was meant ("a bull call
    # spread on ACN, expiring 2026-08-21"); the TradingOrder rows underneath say
    # which contracts actually filled. `symbol` above stays the UNDERLYING ticker
    # and must never hold an OCC contract string -- JobManager selects
    # `distinct Transaction.symbol` and submits a market analysis per value.
    asset_class: AssetClass = Field(default=AssetClass.EQUITY, index=True,
                                    description="EQUITY or OPTION. Before this existed the only "
                                                "tell was multiplier=100.")
    option_strategy: str | None = Field(default=None,
                                        description="The INTENT: bull_call_spread, iron_condor, "
                                                    "covered_call... None for equity.")
    expiry: date | None = Field(default=None, index=True,
                                description="The structure's expiry. Valid as a single value ONLY "
                                            "because every supported structure is single-expiry "
                                            "(no calendars/diagonals). See Task 2.")
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)

    # JSON field for storing additional data (e.g., TradeConditionsData)
    # Named 'meta_data' to avoid conflict with SQLAlchemy's reserved 'metadata' attribute
    meta_data: dict | None = Field(default=None, sa_column=Column(JSON))

    # Optional reference to expert instance for tracking which expert initiated this transaction
    expert_id: int | None = Field(foreign_key="expertinstance.id", nullable=True, ondelete="SET NULL", index=True)
    
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
        from ba2_common.core.trade_store import orders_where

        total_qty = 0.0

        orders = orders_where(transaction_id=self.id)
        executed = OrderStatus.get_executed_statuses()
        for order in orders:
            # Only count filled orders
            if order.status not in executed:
                continue
            # ONE TRUTHINESS TEST USED TO DO TWO DIFFERENT JOBS. This was
            # ``if order.status in executed and order.filled_qty:``, which collapsed
            # "the broker said it filled but never said how much" (filled_qty NULL --
            # UNMEASURABLE) into "nothing filled" (filled_qty 0.0 -- a MEASUREMENT).
            # The total that came out looked like a number either way, and fed
            # AccountInterface.submit_close_order_for_transaction and Smart-RM close
            # sizing. The value cannot carry the tri-state (this returns a float and
            # ~10 call sites add/compare it), so the gap is made LOUD instead of
            # silent: skipped, and reported.
            if order.filled_qty is None:
                from ba2_common.logger import logger
                logger.error(
                    f"Transaction {self.id}.get_current_open_qty(): order {order.id} "
                    f"({order.symbol} {order.side}) is {order.status} but has NO "
                    f"filled_qty — the filled amount is UNMEASURABLE, not zero. "
                    f"Excluding it; the returned net open quantity is therefore "
                    f"incomplete and must not be treated as a measured position size."
                )
                continue
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
        from ba2_common.core.trade_store import orders_where

        total_qty = 0.0

        orders = orders_where(transaction_id=self.id)
        unfilled = OrderStatus.get_unfilled_statuses()
        for order in orders:
            # Only count unfilled orders (excluding take profit and stop loss orders)
            if order.status in unfilled:
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
        from ba2_common.core.trade_store import orders_where

        total_equity = 0.0

        orders = orders_where(transaction_id=self.id)
        for order in orders:
            # Only count filled orders
            if order.status in OrderStatus.get_executed_statuses() and order.filled_qty:
                # Use open_price for filled orders
                price = order.open_price

                if price:
                    # Calculate value: quantity × price × multiplier (options use 100)
                    equity = abs(order.filled_qty) * price * (order.multiplier or 1)
                    total_equity += equity

        return total_equity
    
    def get_pending_open_equity(self, account_interface=None) -> float:
        """
        Calculate the equity value of pending unfilled orders.

        Equity orders are valued at the underlying market price (so
        account_interface is needed for them); option orders are valued from
        their own premium (limit_price/open_price) x multiplier and do NOT
        require the underlying price, so account_interface may be None for
        option-only transactions.

        Args:
            account_interface: Account interface for the underlying market price
                (optional; only used to value equity orders).

        Returns:
            float: Total equity value of pending orders
        """
        from ba2_common.core.trade_store import orders_where
        from ba2_common.logger import logger
        from ba2_common.core.types import OrderType, AssetClass

        total_equity = 0.0

        # Get market price if account interface provided
        market_price = None
        if account_interface:
            try:
                market_price = account_interface.get_instrument_current_price(self.symbol)
            except Exception as e:
                logger.debug(f"Transaction {self.id}.get_pending_open_equity(): Could not get market price: {e}")

        orders = orders_where(transaction_id=self.id)
        for order in orders:
            # Only count unfilled orders (excluding TP/SL orders)
            if order.status in OrderStatus.get_unfilled_statuses():
                # Skip dependent orders (TP/SL legs)
                if order.depends_on_order is not None:
                    continue

                is_option = order.asset_class == AssetClass.OPTION
                # Equity TP/SL exit orders don't use buying power; option limit
                # orders ARE entries, so they must NOT be skipped.
                if (not is_option) and order.order_type in [
                    OrderType.SELL_LIMIT, OrderType.BUY_LIMIT, OrderType.OCO,
                    OrderType.SELL_STOP, OrderType.BUY_STOP,
                ]:
                    continue

                # Calculate remaining quantity
                remaining_qty = order.quantity
                if order.filled_qty:
                    remaining_qty -= order.filled_qty

                if remaining_qty > 0:
                    if is_option:
                        # Option premium risk = premium × multiplier × contracts.
                        premium = order.limit_price or order.open_price
                        if premium:
                            total_equity += abs(remaining_qty) * premium * (order.multiplier or 100)
                    elif market_price:
                        # Equity uses underlying market price for pending orders.
                        total_equity += abs(remaining_qty) * market_price

        return total_equity


class TradingOrder(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # REMOVED order_id: str | None
    account_id: int = Field(foreign_key="accountdefinition.id", nullable=False, ondelete="CASCADE")
    symbol: str = Field(index=True)
    quantity: float
    side: OrderDirection
    order_type: OrderType
    good_for: str | None
    status: OrderStatus = Field(default=OrderStatus.UNKNOWN, index=True)
    filled_qty: float | None
    open_price: float | None = Field(default=None, description="Price at which the order opened (for filled orders)")
    comment: str | None
    created_at: DateTime | None = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)

    # New fields
    open_type: OrderOpenType = Field(default=OrderOpenType.AUTOMATIC)
    broker_order_id: str | None = Field(default=None, description="Broker-specific order ID for tracking")
    expert_recommendation_id: int | None = Field(default=None, foreign_key="expertrecommendation.id", ondelete="SET NULL", index=True, description="Expert recommendation that generated this order")
    limit_price: float | None = Field(default=None, description="Limit price for limit orders")
    stop_price: float | None = Field(default=None, description="Stop price for stop orders")

    # Dependency fields for order chaining
    depends_on_order: int | None = Field(default=None, foreign_key="tradingorder.id", ondelete="SET NULL", description="ID of another order this order depends on")
    depends_order_status_trigger: OrderStatus | None = Field(default=None, description="Status that the depends_on_order must reach to trigger this order")
    
    # Many:1 relationship with Transaction (many orders can belong to one transaction)
    transaction_id: int | None = Field(foreign_key="transaction.id", nullable=True, ondelete="CASCADE", index=True)
    transaction: Optional["Transaction"] = Relationship(back_populates="trading_orders")
    
    # Self-referencing relationship for order dependencies - uses depends_on_order FK
    dependent_orders: List["TradingOrder"] = Relationship(
        back_populates="depends_on_order_rel",
        sa_relationship_kwargs={
            "remote_side": "TradingOrder.id",
            "foreign_keys": "[TradingOrder.depends_on_order]"
        }
    )
    depends_on_order_rel: Optional["TradingOrder"] = Relationship(
        back_populates="dependent_orders",
        sa_relationship_kwargs={
            "foreign_keys": "[TradingOrder.depends_on_order]"
        }
    )
    
    # Optional JSON data field for storing order metadata
    # For WAITING_TRIGGER TP/SL orders: stores {"tp_percent": float, "parent_filled_price": float}
    # Allows recalculation of TP/SL from parent order fill price when order is triggered
    data: dict | None = Field(sa_column=Column(JSON), default=None, description="Optional order metadata (e.g., TP/SL percent for WAITING_TRIGGER orders)")
    
    # OCO leg broker IDs - only set for OCO orders
    # List of broker order IDs (strings) for the TP and SL legs of an OCO order
    # Format: ["broker_id_1", "broker_id_2", ...] where each is a UUID from Alpaca
    # Upstream AccountInterface uses this to look up and insert the leg orders into database
    legs_broker_ids: list[str] | None = Field(sa_column=Column(JSON), default=None, description="Broker order IDs of OCO leg orders (TP and SL) for upstream processing")
    
    # Parent OCO order ID - set when this order is a leg (TP/SL) of an OCO order
    # Links leg orders back to their parent OCO order
    parent_order_id: int | None = Field(default=None, foreign_key="tradingorder.id", description="ID of parent OCO order if this is a leg")
    
    # Self-referencing relationship for OCO parent-child - uses parent_order_id FK
    oco_child_orders: List["TradingOrder"] = Relationship(
        back_populates="oco_parent_order",
        sa_relationship_kwargs={
            "remote_side": "TradingOrder.id",
            "foreign_keys": "[TradingOrder.parent_order_id]"
        }
    )
    oco_parent_order: Optional["TradingOrder"] = Relationship(
        back_populates="oco_child_orders",
        sa_relationship_kwargs={
            "foreign_keys": "[TradingOrder.parent_order_id]"
        }
    )

    # --- Options fields (nullable; equity orders leave these unset) ---
    asset_class: AssetClass = Field(default=AssetClass.EQUITY, index=True, description="equity | option")
    contract_symbol: str | None = Field(default=None, index=True, description="OCC option contract symbol (single-leg)")
    option_type: OptionRight | None = Field(default=None, description="call | put for option legs")
    strike: float | None = Field(default=None, description="Option strike price")
    expiry: date | None = Field(default=None, description="Option expiration date")
    underlying_symbol: str | None = Field(default=None, index=True, description="Underlying equity symbol for options")
    multiplier: int | None = Field(default=None, description="Contract multiplier (100 for standard equity options)")
    position_intent: str | None = Field(default=None, description="Alpaca position intent: buy_to_open/sell_to_open/buy_to_close/sell_to_close")
    option_strategy: str | None = Field(default=None, description="Strategy tag on parent order: long_call/bull_call_spread/covered_call/...")

    def as_string(self) -> str:
        return f"Order(id={self.id}, symbol={self.symbol}, quantity={self.quantity}, side={self.side}, type={self.order_type}, status={self.status})"
    
    def __repr__(self) -> str:
        return self.as_string()
    
    def __str__(self) -> str:
        return self.as_string()
    
    def get_expert_id(self, session=None) -> int | None:
        """
        Get the expert instance ID for this order, checking both linkage paths.
        
        Orders can be linked to experts via two paths:
        1. Transaction path: TradingOrder -> Transaction -> ExpertInstance (Smart Risk Manager)
        2. Recommendation path: TradingOrder -> ExpertRecommendation -> ExpertInstance (traditional experts)
        
        Args:
            session: Optional database session. If not provided, creates a new one.
        
        Returns:
            int | None: Expert instance ID if found, None otherwise
        """
        # Import here to avoid circular dependency
        from ba2_common.core.db import get_db, get_instance
        from ba2_common.core.trade_store import inmem_trades_active

        # BT store: Transaction/ExpertRecommendation are in-mem models (see trade_store.
        # IN_MEM_MODELS) — a raw session.get() bypasses that store and silently returns
        # None even when the row exists, so route through get_instance() (which the store
        # intercepts) instead. Same bug class TradeRiskManagement._get_orders_with_
        # recommendations was fixed for (2026-07-18 senate-basket-dispatch changeset).
        if inmem_trades_active():
            try:
                # Path 1: Check transaction-based linkage (Smart Risk Manager)
                if self.transaction_id:
                    transaction = get_instance(Transaction, self.transaction_id)
                    if transaction and transaction.expert_id:
                        return transaction.expert_id
            except Exception:  # noqa: BLE001 — a missing transaction just falls through
                pass
            try:
                # Path 2: Check recommendation-based linkage (traditional experts)
                if self.expert_recommendation_id:
                    recommendation = get_instance(ExpertRecommendation, self.expert_recommendation_id)
                    if recommendation and recommendation.instance_id:
                        return recommendation.instance_id
            except Exception:  # noqa: BLE001 — a missing recommendation just falls through
                pass
            return None

        # Use provided session or create new one
        close_session = False
        if session is None:
            session = get_db()
            close_session = True

        try:
            # Path 1: Check transaction-based linkage (Smart Risk Manager)
            if self.transaction_id:
                transaction = session.get(Transaction, self.transaction_id)
                if transaction and transaction.expert_id:
                    return transaction.expert_id

            # Path 2: Check recommendation-based linkage (traditional experts)
            if self.expert_recommendation_id:
                from sqlmodel import select
                recommendation = session.get(ExpertRecommendation, self.expert_recommendation_id)
                if recommendation and recommendation.instance_id:
                    return recommendation.instance_id

            # No expert found via either path
            return None

        finally:
            if close_session:
                session.close()



class Instrument(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # unique+index emits `CREATE UNIQUE INDEX ix_instrument_name ON instrument (name)`,
    # byte-identical to what Alembic revision f1a7c2e9b4d0 creates -- so a migrated
    # database and a fresh create_all() one end up with the same schema.
    name: str = Field(unique=True, index=True)
    company_name: str | None = Field(default=None)
    instrument_type: InstrumentType | None = Field(default=None)
    categories: list[str] = Field(sa_column=Column(JSON), default_factory=list)
    labels: list[str] = Field(sa_column=Column(JSON), default_factory=list)

    def __setattr__(self, key, value):
        """Normalise ``name`` on the model, so no writer can skip it.

        The unique index is BINARY: it holds 'AAPL', 'aapl' and ' AAPL' side by
        side quite happily, so uniqueness is only as real as every call site's
        memory to call ``normalize_symbol`` first. One new writer that forgets
        silently reintroduces the duplicate groups in lowercase. Normalising here
        makes it a guarantee instead of a convention, and covers construction too:
        SQLModel's ``__init__`` assigns every field through ``__setattr__``.

        ``None`` is deliberately left alone rather than normalised to ``''`` --
        the column is NOT NULL, and that loud failure beats silently storing a
        nameless row. A COLLATE NOCASE index was the alternative; it would need a
        table rebuild and would still let ``' AAPL'`` through.
        """
        if key == "name" and value is not None:
            # Local import: ba2_common.core.utils imports this module.
            from ba2_common.core.utils import normalize_symbol
            value = normalize_symbol(value)
        super().__setattr__(key, value)

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
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True, description="When the action was executed")

    # Foreign key relationships (expert_recommendation_id is required - all actions come from recommendations)
    expert_recommendation_id: int = Field(foreign_key="expertrecommendation.id", nullable=False, ondelete="CASCADE", index=True, description="Expert recommendation that triggered this action")
    
    # Relationships
    expert_recommendation: "ExpertRecommendation" = Relationship(back_populates="trade_action_results")


class SmartRiskManagerJob(SQLModel, table=True):
    """
    Tracks Smart Risk Manager execution sessions.
    All trading actions are stored in graph_state under 'actions_log' key.
    """
    __tablename__ = "smartriskmanagerjob"
    
    # Primary Key
    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Relations
    expert_instance_id: int = Field(foreign_key="expertinstance.id", index=True, ondelete="CASCADE")
    account_id: int = Field(foreign_key="accountdefinition.id", index=True, ondelete="CASCADE")
    
    # Execution Context
    run_date: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)
    model_used: str = Field(description="Snapshot of risk_manager_model at execution time")
    user_instructions: str = Field(description="Snapshot of smart_risk_manager_user_instructions at execution time")
    
    # State Preservation
    graph_state: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict, description="Complete LangGraph state as JSON (includes actions_log, research_findings, final_summary)")
    
    # Execution Metrics
    duration_seconds: float = Field(default=0.0)
    iteration_count: int = Field(default=0)
    
    # Portfolio Snapshot (equity/balance tracking)
    initial_portfolio_equity: Optional[float] = Field(default=None, description="Account virtual equity at start of execution")
    final_portfolio_equity: Optional[float] = Field(default=None, description="Account virtual equity at end of execution")
    initial_available_balance: Optional[float] = Field(default=None, description="Available balance at start of execution")
    final_available_balance: Optional[float] = Field(default=None, description="Available balance at end of execution")
    
    # Results
    actions_taken_count: int = Field(default=0, description="Number of trading actions executed")
    actions_summary: str = Field(default="", description="Human-readable summary of actions taken")
    
    # Status & Error Handling
    status: str = Field(default="RUNNING", description="RUNNING, COMPLETED, FAILED, INTERRUPTED, TIMEOUT")
    error_message: Optional[str] = Field(default=None)

class ActivityLog(SQLModel, table=True):
    """
    Comprehensive activity log for tracking all significant system operations.
    Records transaction lifecycle, TP/SL modifications, risk manager runs, analysis execution, etc.
    """
    id: int | None = Field(default=None, primary_key=True)
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True, description="When this activity occurred")

    # Classification
    severity: ActivityLogSeverity = Field(description="SUCCESS, INFO, WARNING, FAILURE, DEBUG")
    type: ActivityLogType = Field(index=True, description="Type of activity (e.g., TRANSACTION_CREATED, ANALYSIS_COMPLETED)")

    # Source context
    source_expert_id: int | None = Field(default=None, foreign_key="expertinstance.id", nullable=True, ondelete="CASCADE", index=True, description="Expert that generated this activity (if applicable)")
    source_account_id: int | None = Field(default=None, foreign_key="accountdefinition.id", nullable=True, ondelete="CASCADE", index=True, description="Account associated with this activity (if applicable)")
    
    # Content
    description: str = Field(description="Human-readable description of the activity")
    data: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict, description="Structured data related to the activity (transaction IDs, prices, recommendations, etc.)")


class PersistedQueueTask(SQLModel, table=True):
    """
    Persists worker queue tasks across app restarts.
    Tasks are saved when added to the queue and removed when completed/failed.
    On restart, pending/running tasks can be restored via a manual resume action.
    """
    __tablename__ = "persistedqueuetask"
    
    id: int | None = Field(default=None, primary_key=True)
    
    # Task identification
    task_id: str = Field(index=True, unique=True, description="Unique task ID from WorkerQueue (e.g., 'analysis_1', 'smart_risk_2')")
    task_type: str = Field(description="Task type: 'analysis', 'smart_risk_manager', 'instrument_expansion'")
    
    # Task state at time of persistence
    status: str = Field(default="pending", description="Task status: pending, running, completed, failed")
    priority: int = Field(default=0, description="Task priority (lower = higher priority)")
    
    # Expert/account context
    expert_instance_id: int = Field(foreign_key="expertinstance.id", nullable=False, ondelete="CASCADE")
    account_id: int | None = Field(default=None, foreign_key="accountdefinition.id", nullable=True, ondelete="CASCADE")
    
    # Analysis-specific fields
    symbol: str | None = Field(default=None, description="Symbol for analysis tasks")
    subtype: str | None = Field(default=None, description="Analysis use case: ENTER_MARKET, OPEN_POSITIONS")
    market_analysis_id: int | None = Field(default=None, description="Reference to MarketAnalysis record if created")
    batch_id: str | None = Field(default=None, description="Batch ID for grouping related tasks")
    
    # Expansion task fields
    expansion_type: str | None = Field(default=None, description="For expansion tasks: DYNAMIC, EXPERT, OPEN_POSITIONS")
    
    # Task options
    bypass_balance_check: bool = Field(default=False, description="Skip balance verification")
    bypass_transaction_check: bool = Field(default=False, description="Skip existing transaction checks")
    
    # Timestamps
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), description="When task was created")
    started_at: DateTime | None = Field(default=None, description="When task started executing")
    
    # Metadata for restoration
    queue_counter: int = Field(default=0, description="Original queue counter for ordering restoration")


class LLMUsageLog(SQLModel, table=True):
    """
    Tracks token usage for all LLM API calls across the platform.
    Captures usage from LangChain, data providers, smart risk manager, etc.
    """
    __tablename__ = "llmusagelog"
    
    # Primary Key
    id: int | None = Field(default=None, primary_key=True)
    
    # Context
    expert_instance_id: int | None = Field(default=None, foreign_key="expertinstance.id", index=True, nullable=True, ondelete="SET NULL")
    account_id: int | None = Field(default=None, foreign_key="accountdefinition.id", index=True, nullable=True, ondelete="SET NULL")
    use_case: str = Field(index=True, description="Use case: Market Analysis, Smart Risk Manager, Data Provider, Dynamic Instrument Selection, etc.")
    
    # Model Information
    model_selection: str = Field(description="Full model selection string (e.g., 'openai/gpt4o', 'xai/grok4_fast')")
    provider: str = Field(index=True, description="Provider name (openai, xai, google, etc.)")
    provider_model_name: str = Field(description="Native provider model name (e.g., 'gpt-4o-2024-08-06', 'grok-4-fast-reasoning')")
    
    # Token Usage
    input_tokens: int = Field(default=0, description="Number of input tokens")
    output_tokens: int = Field(default=0, description="Number of output tokens")
    total_tokens: int = Field(default=0, description="Total tokens (input + output)")
    
    # Cost (optional - calculated if pricing available)
    estimated_cost_usd: float | None = Field(default=None, description="Estimated cost in USD based on current pricing")
    
    # Timing
    timestamp: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True, description="When the API call was made")
    duration_ms: int | None = Field(default=None, description="Duration of the API call in milliseconds")
    
    # Additional Context (optional)
    symbol: str | None = Field(default=None, index=True, description="Associated trading symbol if applicable")
    market_analysis_id: int | None = Field(default=None, foreign_key="marketanalysis.id", nullable=True, ondelete="SET NULL")
    smart_risk_manager_job_id: int | None = Field(default=None, foreign_key="smartriskmanagerjob.id", nullable=True, ondelete="SET NULL")
    
    # Error tracking
    error: str | None = Field(default=None, description="Error message if the call failed")


class OptionIVSnapshot(SQLModel, table=True):
    """Trailing ATM implied-volatility sample for an underlying.

    Brokers (e.g. Alpaca) expose no IV history, so we persist our own series
    and compute IV-rank as a percentile over the stored window.
    """
    __tablename__ = "option_iv_snapshot"
    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    underlying: str = Field(index=True)
    atm_iv: float
    recorded_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)


class OptionActivity(SQLModel, table=True):
    """Audit + idempotency record for a processed broker option lifecycle event.

    One row is written per broker activity (assignment / exercise / expiry /
    cash-settle) that reconciliation has seen. The (account_id, activity_id)
    pair is the idempotency key: if a row already exists for an activity_id on
    an account, reconciliation skips it so effects are applied exactly once.
    """
    __tablename__ = "option_activity"
    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    activity_id: str = Field(index=True, description="Broker activity id (idempotency key)")
    activity_type: str
    symbol: str | None = Field(default=None)
    qty: float | None = Field(default=None)
    price: float | None = Field(default=None)
    processed_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)
    result: str | None = Field(default=None, description="What reconciliation did")


class PortfolioAllocationConfig(SQLModel, table=True):
    """Per-account Portfolio Allocation page state that CHANGES MONEY.

    ``valuation_mode`` selects what "current value" means everywhere at once --
    the allocatable base, the ``% of label`` / ``% of total`` columns, and every
    delta -- so it belongs in a table rather than in session storage: a mode the
    user cannot see would silently reinterpret every number on the page.

    ``valuation_mode`` is a PLAIN str column (matching OptionActivity.activity_type):
    "cost" or "market" -- use ``VALUATION_MODE_COST`` / ``VALUATION_MODE_MARKET``
    from ``ba2_common.core.portfolio_allocation``, never a bare literal. One row
    per account, created on first use with the defaults "market" and
    allow_fractional=True.

    MARKET is the default because the requirement is to allocate by VALUE. Cost mode
    measures the allocatable base as ``buying power + what you PAID``, which
    understates it by the entire unrealised P&L and therefore buys MORE of a winner
    instead of trimming it: on a book of 100 WIN (basis 2,000, now worth 12,000) and
    100 FLAT (basis 2,000, now 2,000) with 6,000 of buying power, a 50/50 target
    gives a 10,000 base and BUYS 25 more WIN in cost mode, against a 20,000 base that
    SELLS 17 WIN at market. Cost basis stays a first-class choice -- it is the escape
    hatch when a held symbol's quote fails.

    Fractional defaults ON because roughly three quarters of this book IS
    fractionable; the quarter that is not falls back to whole shares per symbol
    inside the engine (``MarginInfo.fractionable``), so the default costs nothing on
    the ineligible names. Neither column has a server default -- these Python
    defaults are what every new row gets, which is why changing one needs no Alembic
    revision.

    ``unallocated_pct`` is the deliberate CASH RESERVE: the share of the allocatable
    base, 0-100, that a REBALANCE must NOT invest. It scales the investable base
    once (``ba2_common.core.portfolio_allocation.investable_notional``), leaving the
    label targets as pure relative weights that always total 100 among themselves --
    so raising the reserve rewrites none of them and the user never does the
    arithmetic. Stored rather than derived from a label shortfall, which is the one
    place it could otherwise live: with both, "labels sum to 90" would mean either
    "hold 10% in cash" or "you mistyped a box".

    It defaults to 0.0 and is NOT NULL, because "no reserve" is a real answer and is
    what every account that predates the column meant. A NULL would leave the
    wizard's box unfillable and every reader guessing.
    """
    __tablename__ = "portfolio_allocation_config"

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE",
                            index=True, unique=True)
    valuation_mode: str = Field(default="market", description="cost | market (plain str, see core.portfolio_allocation)")
    allow_fractional: bool = Field(default=True, description="Last fractional-shares choice, pre-filled into the wizard (defaults ON)")
    unallocated_pct: float = Field(default=0.0, description="0-100: share of the base deliberately held as cash (scales the investable base)")
    updated_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)


class PortfolioAllocationLabel(SQLModel, table=True):
    """A label the user has chosen to MANAGE for an account's portfolio allocation.

    The row's EXISTENCE is the "managed" flag, so label selection needs no
    separate table -- deleting the row unmanages the label. ``target_pct`` is
    1-100 and, summed across all rows of one account, must total no more than 100
    before a REBALANCE run may be submitted; what is left over is deliberate free
    buying power.

    ``previous_target_pct`` is the target this label RAN WITH before the current
    one -- one generation, shifted by ``save_allocation_targets`` and by nothing
    else. It is NULLABLE and is never back-filled: NULL means "there is no last",
    which is a different answer from a stored 0.0 ("the last run allocated nothing
    to this"), and it is what makes the wizard's Load-last button's disabled state
    a fact rather than a guess.

    ``color`` is the label's swatch, one of the seven hexes in
    ``ba2_trade_platform.ui.utils.portfolio_allocation_view.LABEL_COLOR_PALETTE``
    (Okabe & Ito's colour-universal-design set). NULLABLE and never back-filled, on
    exactly ``previous_target_pct``'s terms: NULL means "the user has not chosen a
    colour", which is a different fact from a stored default -- a default would make
    every label that predates the column claim a colour nobody picked, and the
    picker could then never show "No colour" truthfully. Nothing in the platform
    reads it for money; it is a display key only, and the RENDER whitelists it
    against the palette, so a hand-edited value falls back to neutral grey rather
    than reaching a CSS ``style`` attribute.
    """
    __tablename__ = "portfolio_allocation_label"
    __table_args__ = (
        UniqueConstraint('account_id', 'label', name='uix_pf_alloc_label_account_label'),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    label: str = Field(index=True, description="Instrument label being managed (e.g. 'ARK26')")
    target_pct: float = Field(default=0.0, description="Target % of the base notional (1-100)")
    previous_target_pct: float | None = Field(default=None, description="The target this label ran with before the current one; None means there is no last")
    sort_order: int = Field(default=0, description="Display order of the label expansion on the page")
    comment: str | None = Field(default=None, description="Free-text note shown on the label header")
    color: str | None = Field(default=None, description="Palette hex for this label's icon (e.g. '#56B4E9'); None means no colour chosen")
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)


class PortfolioAllocationSymbol(SQLModel, table=True):
    """A symbol's weight WITHIN a managed label.

    Rows are created LAZILY: a symbol with no row uses the even-split default, so
    absence is meaningful and must never be backfilled for every symbol. A symbol
    may legitimately appear under several labels (its targets then SUM; the page
    shows a warning icon).

    ``previous_weight_pct`` is the weight this symbol RAN WITH before the current
    one, shifted only by ``save_allocation_targets``. NULLABLE and never
    back-filled, for the same reason as ``PortfolioAllocationLabel``: NULL is "there
    is no last", 0.0 is "last time this got nothing". Emphatically NOT written by
    ``set_symbol_weight`` -- the comment-save path re-writes ``weight_pct`` on every
    debounced keystroke, so a shift there would destroy the real previous weight
    one character at a time.
    """
    __tablename__ = "portfolio_allocation_symbol"
    __table_args__ = (
        UniqueConstraint('account_id', 'label', 'symbol',
                         name='uix_pf_alloc_symbol_account_label_symbol'),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    label: str = Field(index=True)
    symbol: str = Field(index=True, description="Normalised (.strip().upper()) instrument symbol")
    weight_pct: float = Field(default=0.0, description="Weight % WITHIN the label (1-100)")
    previous_weight_pct: float | None = Field(default=None, description="The weight this symbol ran with before the current one; None means there is no last")
    comment: str | None = Field(default=None, description="Free-text note shown on the symbol row")
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)


class PortfolioIncomeEvent(SQLModel, table=True):
    """One deposit or dividend, consumed oldest-first by allocation runs.

    ``event_type`` is a PLAIN str column (matching OptionActivity.activity_type):
    "DEPOSIT" or "DIVIDEND" -- use ``CASH_TRANSFER_DEPOSIT`` /
    ``CASH_TRANSFER_DIVIDEND`` from ``ba2_common.core.account_types``, never a
    bare literal. Withdrawals are NOT income and are never persisted here.

    ``(account_id, external_id)`` is the idempotency key: re-syncing the broker
    ledger upserts instead of duplicating, exactly as ``OptionActivity`` does.
    An event can be PARTIALLY consumed; the remainder stays open.
    """
    __tablename__ = "portfolio_income_event"
    __table_args__ = (
        UniqueConstraint('account_id', 'external_id', name='uix_pf_income_account_externalid'),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    external_id: str = Field(index=True, description="Broker activity id (idempotency key)")
    event_date: date = Field(index=True, description="Broker settlement / pay date")
    event_type: str = Field(description="DEPOSIT | DIVIDEND (plain str, see core.account_types)")
    symbol: str | None = Field(default=None, index=True, description="Payer symbol for DIVIDEND; None for DEPOSIT")
    amount: float = Field(description="Positive cash amount in the account currency")
    consumed_amount: float = Field(default=0.0, description="How much of `amount` allocation runs have already spent")
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)

    @property
    def open_amount(self) -> float:
        """Un-consumed remainder of this event; never negative."""
        return max(0.0, (self.amount or 0.0) - (self.consumed_amount or 0.0))


class PortfolioAllocationRun(SQLModel, table=True):
    """Audit row for one SUBMITTED allocation run.

    ``mode`` is a PLAIN str column: "REBALANCE" or "INVEST_LABEL" -- use
    ``ALLOCATION_MODE_REBALANCE`` / ``ALLOCATION_MODE_INVEST_LABEL`` from
    ``ba2_common.core.portfolio_allocation``.

    ``plan_json`` is ``AllocationPlan.to_dict()`` captured at SUBMIT time, which
    keeps a dry-run reproducible after the weights change. Income consumption is
    driven by the NET buy value (``net_buy_value`` below): a rebalance funded
    entirely by its own sells consumes no income.

    ``filled_buy_value`` / ``filled_sell_value`` are FILLED money, never intended
    money: ``filled_qty * open_price`` summed over the run's own MARKET orders
    AFTER ``account.refresh_orders()`` has brought the broker's truth back. An
    order that was submitted and then rejected, or that never filled, is worth
    exactly 0 here. They were called ``submitted_*`` until the rename, and the old
    spelling was a money bug: consuming the income ledger against value that was
    only ever INTENDED marks a dividend spent when nothing was bought, and the
    ``income_consumed_at`` stamp makes that permanent.

    ``base_notional`` mirrors ``AllocationPlan.base_notional`` and so carries TWO
    meanings depending on ``mode``: in a REBALANCE it is the ALLOCATABLE BASE
    (buying power plus the current value of managed positions, at plan time); in
    an INVEST_LABEL run it is simply THE BUDGET being spent. Read it together
    with ``mode``.

    ``income_consumed_at`` is the IDEMPOTENCY GUARD for the income ledger. NULL
    means this run has never spent from ``portfolio_income_event``; a timestamp
    means it has, exactly once. ``portfolio_allocation_store.finalise_allocation_run``
    writes the ledger takes, ``income_consumed_events`` and this stamp in ONE
    transaction, so a crash cannot leave money spent-but-unrecorded (or
    recorded-but-unspent). A run row whose totals are written but whose stamp is
    NULL is a run that died mid-submit -- see ``get_unconsumed_runs``.

    One transaction buys CRASH atomicity and nothing else: reading this column
    and then writing it is a check-then-act, and on pysqlite a ``SELECT`` starts
    no transaction and takes no snapshot, so two concurrent finalisers would both
    read NULL and both spend. What makes the guard hold is that
    ``finalise_allocation_run`` opens with ``BEGIN IMMEDIATE`` and so serialises
    them. Any other writer of this column owes the same lock -- the column cannot
    defend itself.
    """
    __tablename__ = "portfolio_allocation_run"

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="accountdefinition.id", ondelete="CASCADE", index=True)
    mode: str = Field(index=True, description="REBALANCE | INVEST_LABEL (plain str)")
    scope_label: str | None = Field(default=None, description="Label targeted by an INVEST_LABEL run; None for REBALANCE")
    base_notional: float = Field(default=0.0, description="REBALANCE: buying_power + current value of managed positions, at plan time. INVEST_LABEL: the budget being spent")
    available_buying_power: float = Field(default=0.0, description="Broker buying power snapshotted at plan time")
    allow_fractional: bool = Field(default=False, description="Whether fractional shares were opted in for this run")
    plan_json: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict, description="AllocationPlan.to_dict() at submit time")
    filled_buy_value: float = Field(default=0.0, description="Sum of filled_qty * open_price over this run's BUY orders, measured after refresh_orders")
    filled_sell_value: float = Field(default=0.0, description="Sum of filled_qty * open_price over this run's SELL orders, measured after refresh_orders")
    order_ids: List[int] = Field(sa_column=Column(JSON), default_factory=list, description="TradingOrder ids created by this run")
    income_consumed_at: DateTime | None = Field(default=None, description="When this run consumed the income ledger; NULL means it never has (idempotency guard)")
    income_consumed_events: List[Any] = Field(sa_column=Column(JSON), default_factory=list, description="[[income_event_id, amount], ...] this run actually took from the ledger")
    created_at: DateTime = Field(default_factory=lambda: DateTime.now(timezone.utc), index=True)

    @property
    def net_buy_value(self) -> float:
        """``max(0, filled buys - filled sells)`` -- what this run consumes from the ledger."""
        return max(0.0, (self.filled_buy_value or 0.0) - (self.filled_sell_value or 0.0))

    @property
    def is_income_consumed(self) -> bool:
        """Has this run's income-ledger consumption already been applied?

        The guard the store checks before spending: ``income_consumed_at`` is set
        in the SAME transaction as the ledger writes, so True means the money was
        taken and False means it was not -- there is no half-way state.
        """
        return self.income_consumed_at is not None

    @property
    def income_consumed_amount(self) -> float:
        """Total taken from the income ledger by this run; 0.0 when it took nothing.

        Derived from ``income_consumed_events`` rather than stored, so the total
        and the per-event breakdown can never disagree. Reads 0.0 both for a run
        that has not consumed yet and for one that legitimately consumed nothing
        (a rebalance funded by its own sells) -- use ``is_income_consumed`` to
        tell those apart.
        """
        return float(sum(amount for _, amount in (self.income_consumed_events or [])))
