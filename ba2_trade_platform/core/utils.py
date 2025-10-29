"""
Utility functions for the BA2 Trade Platform core functionality.
"""

from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone
from .db import get_instance, get_db
from .models import ExpertInstance, TradingOrder, ExpertRecommendation, MarketAnalysis, Transaction
from .types import OrderStatus, TransactionStatus, ActivityLogSeverity, ActivityLogType, OrderDirection
from ..modules.experts import get_expert_class
from ..modules.accounts import get_account_class
from ..logger import logger
from sqlmodel import Session, select

if TYPE_CHECKING:
    from .interfaces import MarketExpertInterface


def get_expert_instance_from_id(expert_instance_id: int, use_cache: bool = True) -> Optional["MarketExpertInterface"]:
    """
    Get an expert instance with the appropriate class instantiated from the database.
    
    This function:
    1. Retrieves the ExpertInstance from the database by ID
    2. Determines the expert type from the database record
    3. Dynamically imports and instantiates the appropriate expert class
    4. Returns the instantiated expert object ready to use
    
    By default, uses a singleton cache to ensure only one instance per expert_instance_id exists
    in memory. This dramatically reduces database calls for settings loading.
    
    Args:
        expert_instance_id (int): The ID of the expert instance in the database
        use_cache (bool, optional): If True (default), use singleton cache. If False, create new instance.
        
    Returns:
        Optional[MarketExpertInterface]: The instantiated expert instance, or None if not found
        
    Example:
        >>> expert = get_expert_instance_from_id(1)
        >>> if expert:
        ...     recommendations = expert.get_enabled_instruments()
        ...     analysis_result = expert.run_analysis("AAPL", market_analysis)
        
        >>> # Multiple calls return the same cached instance (with cached settings)
        >>> expert1 = get_expert_instance_from_id(1)
        >>> expert2 = get_expert_instance_from_id(1)
        >>> assert expert1 is expert2  # Same object in memory
    """
    from .ExpertInstanceCache import ExpertInstanceCache
    
    # Get the expert instance record from database
    expert_instance = get_instance(ExpertInstance, expert_instance_id)
    if not expert_instance:
        return None
    
    # Get the expert class based on the type stored in database
    expert_class = get_expert_class(expert_instance.expert)
    if not expert_class:
        raise ValueError(f"Unknown expert type: {expert_instance.expert}")
    
    # Use cache by default for singleton behavior
    if use_cache:
        return ExpertInstanceCache.get_instance(expert_instance_id, expert_class)
    else:
        # Create new instance without caching (for special cases)
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


def get_account_instance_from_id(account_id: int, session=None, use_cache: bool = True):
    """
    Get an account instance with the appropriate class instantiated from the database.
    
    This function:
    1. Retrieves the AccountDefinition from the database by ID
    2. Determines the account provider from the database record  
    3. Dynamically imports and instantiates the appropriate account class
    4. Returns the instantiated account object ready to use
    
    By default, uses a singleton cache to ensure only one instance per account_id exists
    in memory. This dramatically reduces database calls for settings loading.
    
    Args:
        account_id (int): The ID of the account definition in the database
        session (Session, optional): An existing database session to reuse. If not provided, creates a new one.
        use_cache (bool, optional): If True (default), use singleton cache. If False, create new instance.
        
    Returns:
        Optional[AccountInterface]: The instantiated account instance, or None if not found
        
    Example:
        >>> account = get_account_instance_from_id(1)
        >>> if account:
        ...     account_info = account.get_account_info()
        ...     orders = account.list_orders()
        
        >>> # Multiple calls return the same cached instance (with cached settings)
        >>> account1 = get_account_instance_from_id(1)
        >>> account2 = get_account_instance_from_id(1)
        >>> assert account1 is account2  # Same object in memory
    """
    from .models import AccountDefinition
    from .AccountInstanceCache import AccountInstanceCache
    
    # Get the account definition record from database (reuse session if provided)
    account_def = get_instance(AccountDefinition, account_id, session=session)
    if not account_def:
        return None
    
    # Get the account class based on the provider stored in database
    account_class = get_account_class(account_def.provider)
    if not account_class:
        raise ValueError(f"Unknown account provider: {account_def.provider}")
    
    # Use cache by default for singleton behavior
    if use_cache:
        return AccountInstanceCache.get_instance(account_id, account_class)
    else:
        # Create new instance without caching (for special cases)
        return account_class(account_id)


def close_transaction_with_logging(
    transaction: Transaction,
    account_id: int,
    close_reason: str,
    session: Optional[Session] = None,
    additional_data: Optional[dict] = None
) -> None:
    """
    Close a transaction and log the activity.
    
    This centralized function should be used by all code paths that close transactions
    to ensure consistent activity logging.
    
    Args:
        transaction: The transaction to close
        account_id: The account ID for activity logging
        close_reason: Reason for closing (e.g., "tp_sl_filled", "all_orders_terminal", "position_balanced")
        session: Optional database session (if None, transaction object should be managed externally)
        additional_data: Optional additional data to include in activity log
    """
    try:
        # Update transaction status
        transaction.status = TransactionStatus.CLOSED
        if not transaction.close_date:
            transaction.close_date = datetime.now(timezone.utc)
        
        # Calculate P&L if available
        profit_loss = None
        if transaction.close_price and transaction.open_price and transaction.quantity:
            if transaction.quantity > 0:  # Long position
                profit_loss = (transaction.close_price - transaction.open_price) * transaction.quantity
            else:  # Short position
                profit_loss = (transaction.open_price - transaction.close_price) * abs(transaction.quantity)
        
        # Build activity description
        description = f"Closed {transaction.symbol} transaction #{transaction.id}"
        
        # Add close reason to description
        reason_descriptions = {
            "tp_sl_filled": "(TP/SL filled)",
            "all_orders_terminal": "(all orders terminal)",
            "position_balanced": "(position balanced)",
            "entry_orders_terminal_no_execution": "(entry orders canceled/rejected)",
            "entry_orders_terminal_after_opening": "(entry orders terminal)",
            "manual_close": "(manual close)",
            "cleanup": "(cleanup)"
        }
        
        if close_reason in reason_descriptions:
            description += f" {reason_descriptions[close_reason]}"
        else:
            description += f" ({close_reason})"
        
        # Add P&L to description if available
        if profit_loss is not None:
            description += f" with P&L ${profit_loss:.2f}"
        
        # Build activity data
        activity_data = {
            "transaction_id": transaction.id,
            "symbol": transaction.symbol,
            "quantity": transaction.quantity,
            "open_price": transaction.open_price,
            "close_price": transaction.close_price,
            "profit_loss": profit_loss,
            "close_reason": close_reason
        }
        
        # Merge additional data if provided
        if additional_data:
            activity_data.update(additional_data)
        
        # Determine severity based on close reason
        severity = ActivityLogSeverity.SUCCESS
        if close_reason in ["entry_orders_terminal_no_execution", "entry_orders_terminal_after_opening"]:
            severity = ActivityLogSeverity.INFO
        
        # Log activity (best effort - don't fail transaction close if logging fails)
        try:
            from .db import log_activity
            
            log_activity(
                severity=severity,
                activity_type=ActivityLogType.TRANSACTION_CLOSED,
                description=description,
                data=activity_data,
                source_account_id=account_id,
                source_expert_id=transaction.expert_id
            )
        except Exception as log_error:
            # Log activity failed, but don't fail the transaction close
            logger.warning(f"Failed to log activity for transaction {transaction.id} closure: {log_error}")
        
        logger.info(f"Closed transaction {transaction.id} ({transaction.symbol}): {close_reason}")
        
    except Exception as e:
        logger.error(f"Error closing transaction {transaction.id} with logging: {e}", exc_info=True)


def log_close_order_activity(
    transaction: Transaction,
    account_id: int,
    success: bool,
    error_message: Optional[str] = None,
    close_order_id: Optional[int] = None,
    quantity: Optional[float] = None,
    side: Optional[OrderDirection] = None,
    canceled_count: int = 0,
    deleted_count: int = 0,
    is_retry: bool = False
) -> None:
    """
    Log activity for close order submission (success or failure).
    
    This centralized function should be used by all code paths that submit close orders
    to ensure consistent activity logging.
    
    Args:
        transaction: The transaction being closed
        account_id: The account ID for activity logging
        success: Whether the order submission was successful
        error_message: Error message if submission failed (optional)
        close_order_id: The ID of the submitted close order (if successful)
        quantity: The quantity of the close order
        side: The side of the close order (BUY/SELL)
        canceled_count: Number of orders canceled
        deleted_count: Number of orders deleted
        is_retry: Whether this is a retry of a previous order
    """
    try:
        from .db import log_activity
        
        # Build activity description
        action = "Retried" if is_retry else "Submitted"
        status = "closing order" if success else "to submit closing order"
        
        description = f"{action if not success else 'Submitted'} closing order for {transaction.symbol} transaction #{transaction.id}"
        if not success:
            description = f"Failed {action.lower()} closing order for {transaction.symbol} transaction #{transaction.id}"
        
        # Build activity data
        activity_data = {
            "transaction_id": transaction.id,
            "symbol": transaction.symbol,
            "canceled_count": canceled_count,
            "deleted_count": deleted_count
        }
        
        if is_retry:
            activity_data["retry"] = True
        
        if success and close_order_id:
            activity_data["close_order_id"] = close_order_id
            if quantity is not None:
                activity_data["quantity"] = quantity
            if side is not None:
                activity_data["side"] = side.value
        
        if not success and error_message:
            activity_data["error"] = error_message
        
        # Determine severity
        severity = ActivityLogSeverity.SUCCESS if success else ActivityLogSeverity.FAILURE
        
        # Log activity
        log_activity(
            severity=severity,
            activity_type=ActivityLogType.TRANSACTION_CLOSED,
            description=description,
            data=activity_data,
            source_account_id=account_id,
            source_expert_id=transaction.expert_id
        )
        
    except Exception as e:
        logger.warning(f"Failed to log close order activity: {e}")


def log_transaction_created_activity(
    trading_order: TradingOrder,
    account_id: int,
    transaction_id: Optional[int] = None,
    expert_id: Optional[int] = None,
    current_price: Optional[float] = None,
    success: bool = True,
    error_message: Optional[str] = None
) -> None:
    """
    Log activity for transaction creation (success or failure).
    
    This centralized function should be used by all code paths that create transactions
    to ensure consistent activity logging.
    
    Args:
        trading_order: The trading order for which transaction was created
        account_id: The account ID for activity logging
        transaction_id: The ID of the created transaction (if successful)
        expert_id: The expert ID that created the transaction
        current_price: The current price at transaction creation
        success: Whether the transaction creation was successful
        error_message: Error message if creation failed (optional)
    """
    try:
        from .db import log_activity
        
        if success:
            description = f"Created {trading_order.side.value} transaction for {trading_order.symbol} (quantity: {trading_order.quantity})"
            activity_data = {
                "transaction_id": transaction_id,
                "symbol": trading_order.symbol,
                "side": trading_order.side.value,
                "quantity": trading_order.quantity,
                "order_id": trading_order.id
            }
            if current_price is not None:
                activity_data["open_price"] = current_price
            
            severity = ActivityLogSeverity.SUCCESS
        else:
            description = f"Failed to create transaction for {trading_order.symbol}: {error_message or 'Unknown error'}"
            activity_data = {
                "symbol": trading_order.symbol,
                "side": trading_order.side.value,
                "quantity": trading_order.quantity,
                "order_id": trading_order.id
            }
            if error_message:
                activity_data["error"] = error_message
            
            severity = ActivityLogSeverity.FAILURE
        
        # Log activity
        log_activity(
            severity=severity,
            activity_type=ActivityLogType.TRANSACTION_CREATED,
            description=description,
            data=activity_data,
            source_expert_id=expert_id,
            source_account_id=account_id
        )
        
    except Exception as e:
        logger.warning(f"Failed to log transaction creation activity: {e}")


def log_tp_sl_adjustment_activity(
    trading_order: TradingOrder,
    account_id: int,
    adjustment_type: str,  # "tp" or "sl"
    new_price: Optional[float] = None,
    percent: Optional[float] = None,
    order_id: Optional[int] = None,
    success: bool = True,
    error_message: Optional[str] = None
) -> None:
    """
    Log activity for TP/SL adjustment (success or failure).
    
    This centralized function should be used by all code paths that adjust TP/SL
    to ensure consistent activity logging.
    
    Args:
        trading_order: The trading order whose TP/SL is being adjusted
        account_id: The account ID for activity logging
        adjustment_type: Type of adjustment ("tp" or "sl")
        new_price: The new TP/SL price
        percent: The TP/SL percentage
        order_id: The ID of the TP/SL order
        success: Whether the adjustment was successful
        error_message: Error message if adjustment failed (optional)
    """
    try:
        from .db import log_activity, get_instance
        from .models import Transaction
        
        # Get transaction for expert_id
        transaction = None
        if trading_order and trading_order.transaction_id:
            transaction = get_instance(Transaction, trading_order.transaction_id)
        
        adjustment_name = "TP" if adjustment_type == "tp" else "SL"
        
        if success:
            description = f"Changed {adjustment_name} for {trading_order.symbol}"
            if new_price is not None:
                description += f" to ${new_price:.2f}"
            if percent is not None:
                description += f" ({percent:.2f}%)"
            
            activity_data = {
                "order_id": order_id,
                "symbol": trading_order.symbol,
                "transaction_id": trading_order.transaction_id
            }
            if new_price is not None:
                activity_data[f"new_{adjustment_type}"] = new_price
            if percent is not None:
                activity_data[f"{adjustment_type}_percent"] = percent
            
            severity = ActivityLogSeverity.SUCCESS
            activity_type = ActivityLogType.TRANSACTION_TP_CHANGED if adjustment_type == "tp" else ActivityLogType.TRANSACTION_SL_CHANGED
        else:
            description = f"Failed to set {adjustment_name} for {trading_order.symbol}: {error_message or 'Unknown error'}"
            activity_data = {
                "order_id": trading_order.id if trading_order else None,
                "symbol": trading_order.symbol if trading_order else None,
                "transaction_id": trading_order.transaction_id if trading_order else None
            }
            if error_message:
                activity_data["error"] = error_message
            
            severity = ActivityLogSeverity.FAILURE
            activity_type = ActivityLogType.TRANSACTION_TP_CHANGED if adjustment_type == "tp" else ActivityLogType.TRANSACTION_SL_CHANGED
        
        # Log activity
        log_activity(
            severity=severity,
            activity_type=activity_type,
            description=description,
            data=activity_data,
            source_expert_id=transaction.expert_id if transaction else None,
            source_account_id=account_id
        )
        
    except Exception as e:
        logger.warning(f"Failed to log {adjustment_type.upper()} adjustment activity: {e}")


