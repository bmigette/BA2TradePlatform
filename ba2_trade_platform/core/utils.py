"""
Utility functions for the BA2 Trade Platform core functionality.
"""

from typing import Optional, List, TYPE_CHECKING
from .db import get_instance, get_db
from .models import ExpertInstance, TradingOrder, ExpertRecommendation, MarketAnalysis
from .types import OrderStatus
from ..modules.experts import get_expert_class
from ..modules.accounts import get_account_class
from sqlmodel import Session, select

if TYPE_CHECKING:
    from .interfaces import MarketExpertInterface


def get_expert_instance_from_id(expert_instance_id: int) -> Optional["MarketExpertInterface"]:
    """
    Get an expert instance with the appropriate class instantiated from the database.
    
    This function:
    1. Retrieves the ExpertInstance from the database by ID
    2. Determines the expert type from the database record
    3. Dynamically imports and instantiates the appropriate expert class
    4. Returns the instantiated expert object ready to use
    
    Args:
        expert_instance_id (int): The ID of the expert instance in the database
        
    Returns:
        Optional[MarketExpertInterface]: The instantiated expert instance, or None if not found
        
    Example:
        >>> expert = get_expert_instance_from_id(1)
        >>> if expert:
        ...     recommendations = expert.get_enabled_instruments()
        ...     analysis_result = expert.run_analysis("AAPL", market_analysis)
    """
    # Get the expert instance record from database
    expert_instance = get_instance(ExpertInstance, expert_instance_id)
    if not expert_instance:
        return None
    
    # Get the expert class based on the type stored in database
    expert_class = get_expert_class(expert_instance.expert)
    if not expert_class:
        raise ValueError(f"Unknown expert type: {expert_instance.expert}")
    
    # Instantiate and return the expert with the database ID
    return expert_class(expert_instance_id)


def get_expert_instance_id_from_order_id(order_id: int) -> Optional[int]:
    """
    Get the expert instance ID associated with an order via its linked recommendation.
    
    Args:
        order_id (int): The ID of the trading order
        
    Returns:
        Optional[int]: The expert instance ID, or None if no link exists
    """
    try:
        with Session(get_db().bind) as session:
            # Query order with joined recommendation
            statement = (
                select(ExpertRecommendation.instance_id)
                .join(TradingOrder, TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
                .where(TradingOrder.id == order_id)
            )
            result = session.exec(statement).first()
            return result
    except Exception:
        return None


def get_expert_from_order_id(order_id: int) -> Optional["MarketExpertInterface"]:
    """
    Get the expert instance associated with an order via its linked recommendation.
    
    Args:
        order_id (int): The ID of the trading order
        
    Returns:
        Optional[MarketExpertInterface]: The expert instance, or None if no link exists
    """
    expert_instance_id = get_expert_instance_id_from_order_id(order_id)
    if expert_instance_id:
        return get_expert_instance_from_id(expert_instance_id)
    return None


def get_market_analysis_id_from_order_id(order_id: int) -> Optional[int]:
    """
    Get the market analysis ID associated with an order via its linked recommendation.
    
    Args:
        order_id (int): The ID of the trading order
        
    Returns:
        Optional[int]: The market analysis ID, or None if no link exists
    """
    try:
        with Session(get_db().bind) as session:
            # Query order with joined recommendation
            statement = (
                select(ExpertRecommendation.market_analysis_id)
                .join(TradingOrder, TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
                .where(TradingOrder.id == order_id)
            )
            result = session.exec(statement).first()
            return result
    except Exception:
        return None


def has_existing_orders_for_expert_and_symbol(expert_instance_id: int, symbol: str, 
                                             statuses: List[OrderStatus] = None) -> bool:
    """
    Check if there are existing orders for a specific expert and symbol in given statuses.
    
    Args:
        expert_instance_id (int): The expert instance ID
        symbol (str): The trading symbol
        statuses (List[OrderStatus], optional): List of order statuses to check. 
                                              Defaults to [PENDING, OPEN, FILLED]
        
    Returns:
        bool: True if orders exist, False otherwise
    """
    if statuses is None:
        statuses = [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.FILLED]
    
    try:
        with Session(get_db().bind) as session:
            # Query orders linked to the expert via recommendations
            statement = (
                select(TradingOrder.id)
                .join(ExpertRecommendation, TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
                .where(
                    ExpertRecommendation.instance_id == expert_instance_id,
                    TradingOrder.symbol == symbol,
                    TradingOrder.status.in_(statuses)
                )
                .limit(1)  # We only need to know if any exist
            )
            result = session.exec(statement).first()
            return result is not None
    except Exception:
        return False


def has_existing_transactions_for_expert_and_symbol(expert_instance_id: int, symbol: str) -> bool:
    """
    Check if there are existing OPENED or WAITING transactions for a specific expert and symbol.
    
    Args:
        expert_instance_id (int): The expert instance ID
        symbol (str): The trading symbol
        
    Returns:
        bool: True if transactions exist in OPENED or WAITING status, False otherwise
    """
    try:
        from .models import Transaction
        from .types import TransactionStatus
        
        with Session(get_db().bind) as session:
            statement = (
                select(Transaction.id)
                .where(
                    Transaction.expert_id == expert_instance_id,
                    Transaction.symbol == symbol,
                    Transaction.status.in_([TransactionStatus.WAITING, TransactionStatus.OPENED])
                )
                .limit(1)  # We only need to know if any exist
            )
            result = session.exec(statement).first()
            return result is not None
    except Exception:
        return False


def get_orders_for_expert_and_symbol(expert_instance_id: int, symbol: str = None, 
                                   statuses: List[OrderStatus] = None) -> List[TradingOrder]:
    """
    Get all orders for a specific expert instance, optionally filtered by symbol and statuses.
    
    Args:
        expert_instance_id (int): The expert instance ID
        symbol (str, optional): Filter by trading symbol
        statuses (List[OrderStatus], optional): Filter by order statuses
        
    Returns:
        List[TradingOrder]: List of matching orders
    """
    try:
        with Session(get_db().bind) as session:
            # Query orders linked to the expert via recommendations
            statement = (
                select(TradingOrder)
                .join(ExpertRecommendation, TradingOrder.expert_recommendation_id == ExpertRecommendation.id)
                .where(ExpertRecommendation.instance_id == expert_instance_id)
            )
            
            # Add optional filters
            if symbol:
                statement = statement.where(TradingOrder.symbol == symbol)
            if statuses:
                statement = statement.where(TradingOrder.status.in_(statuses))
            
            return list(session.exec(statement).all())
    except Exception:
        return []


def get_account_instance_from_id(account_id: int, session=None):
    """
    Get an account instance with the appropriate class instantiated from the database.
    
    This function:
    1. Retrieves the AccountDefinition from the database by ID
    2. Determines the account provider from the database record  
    3. Dynamically imports and instantiates the appropriate account class
    4. Returns the instantiated account object ready to use
    
    Args:
        account_id (int): The ID of the account definition in the database
        session (Session, optional): An existing database session to reuse. If not provided, creates a new one.
        
    Returns:
        Optional[AccountInterface]: The instantiated account instance, or None if not found
        
    Example:
        >>> account = get_account_instance_from_id(1)
        >>> if account:
        ...     account_info = account.get_account_info()
        ...     orders = account.list_orders()
        
        >>> # Better: Reuse existing session in a loop
        >>> with get_db() as session:
        ...     for account_id in account_ids:
        ...         account = get_account_instance_from_id(account_id, session=session)
    """
    from .models import AccountDefinition
    
    # Get the account definition record from database (reuse session if provided)
    account_def = get_instance(AccountDefinition, account_id, session=session)
    if not account_def:
        return None
    
    # Get the account class based on the provider stored in database
    account_class = get_account_class(account_def.provider)
    if not account_class:
        raise ValueError(f"Unknown account provider: {account_def.provider}")
    
    # Instantiate and return the account with the database ID
    return account_class(account_id)
