"""
Utility functions for the BA2 Trade Platform core functionality.
"""

from typing import Optional, List, TYPE_CHECKING, Dict, Any
from datetime import datetime, timezone
import time
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


def calculate_transaction_pnl(transaction: Transaction) -> Optional[float]:
    """
    Calculate P&L for a transaction, correctly handling both long and short positions.

    For LONG (BUY): P&L = (close_price - open_price) * quantity
    For SHORT (SELL): P&L = (open_price - close_price) * quantity

    Args:
        transaction: Transaction with open_price, close_price, quantity, and side fields

    Returns:
        P&L as float, or None if required fields are missing
    """
    if not transaction.close_price or not transaction.open_price or not transaction.quantity:
        return None
    if transaction.side == OrderDirection.BUY:
        return (transaction.close_price - transaction.open_price) * transaction.quantity
    else:  # Short position
        return (transaction.open_price - transaction.close_price) * transaction.quantity


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
        transaction.close_reason = close_reason  # Store the close reason
        if not transaction.close_date:
            transaction.close_date = datetime.now(timezone.utc)
        
        # Calculate P&L if available
        profit_loss = calculate_transaction_pnl(transaction)
        
        # Build activity description
        description = f"Closed {transaction.symbol} transaction #{transaction.id}"
        
        # Add close reason to description with detailed explanations
        reason_descriptions = {
            "tp_sl_filled": "- Take Profit/Stop Loss limit order was filled by broker",
            "oco_leg_filled": "- Take Profit/Stop Loss OCO order was filled by broker",
            "all_orders_terminal": "- All orders reached terminal status (no active orders remaining)",
            "position_balanced": "- Position balanced (buy/sell orders equal)",
            "entry_orders_terminal_no_execution": "- Entry orders canceled/rejected before execution",
            "entry_orders_terminal_after_opening": "- Entry orders reached terminal status after opening",
            "position_not_at_broker": "- Position closed directly at broker (external close)",
            "manual_close": "- Manual close initiated by user",
            "smart_risk_manager": "- Automatically closed by Smart Risk Manager",
            "cleanup": "- Closed during database cleanup operation"
        }
        
        if close_reason in reason_descriptions:
            description += f" {reason_descriptions[close_reason]}"
        else:
            description += f" (reason: {close_reason})"
        
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


def log_trade_action_activity(
    action_type: str,
    symbol: str,
    account_id: int,
    expert_id: Optional[int],
    success: bool,
    message: str,
    is_open_position: bool = False,
    additional_data: Optional[Dict[str, Any]] = None
) -> None:
    """
    Log activity for trade action execution (success or failure).
    
    This centralized function should be used by all code paths that execute trade actions
    to ensure consistent activity logging.
    
    Args:
        action_type: The type of action (buy, sell, close, adjust_take_profit, etc.)
        symbol: The instrument symbol
        account_id: The account ID for activity logging
        expert_id: The expert ID that executed the action
        success: Whether the action execution was successful
        message: Status message from execution
        is_open_position: If True, logs as TRADE_ACTION_OPEN, else TRADE_ACTION_NEW
        additional_data: Optional additional data to include in activity log
    """
    try:
        from .db import log_activity
        
        # Determine activity type based on context
        activity_type = ActivityLogType.TRADE_ACTION_OPEN if is_open_position else ActivityLogType.TRADE_ACTION_NEW
        
        # Build description
        action_display = action_type.replace("_", " ").title()
        status = "✓ Successfully executed" if success else "✗ Failed to execute"
        context = "on open position" if is_open_position else "for new entry"
        description = f"{status} {action_display} for {symbol} {context}"
        
        # Build activity data
        activity_data = {
            "symbol": symbol,
            "action_type": action_type,
            "message": message
        }
        
        # Merge additional data if provided
        if additional_data:
            activity_data.update(additional_data)
        
        # Determine severity
        severity = ActivityLogSeverity.SUCCESS if success else ActivityLogSeverity.FAILURE
        
        # Log activity
        log_activity(
            severity=severity,
            activity_type=activity_type,
            description=description,
            data=activity_data,
            source_expert_id=expert_id,
            source_account_id=account_id
        )
        
    except Exception as e:
        logger.warning(f"Failed to log trade action activity: {e}")


def is_transaction_orphaned(transaction_id: int, session: Optional[Session] = None) -> bool:
    """
    Check if a transaction is orphaned (has no associated orders).
    
    Args:
        transaction_id: The ID of the transaction to check
        session: Optional database session (if None, creates a new one)
        
    Returns:
        bool: True if the transaction has no orders, False otherwise
    """
    try:
        # Use provided session or get database connection
        if session is None:
            session = get_db()
        
        # Check if transaction has any orders
        orders_statement = select(TradingOrder).where(
            TradingOrder.transaction_id == transaction_id
        ).limit(1)
        first_order = session.exec(orders_statement).first()
        
        return first_order is None
        
    except Exception as e:
        logger.error(f"Error checking if transaction {transaction_id} is orphaned: {e}", exc_info=True)
        return False


def get_account_instance_from_transaction(transaction_id: int, session: Optional[Session] = None):
    """
    Get an account instance from a transaction ID by finding the first order's account.
    
    This centralized function should be used instead of duplicating the logic of finding
    the account through transaction orders.
    
    Args:
        transaction_id: The ID of the transaction
        session: Optional database session (if None, creates a new one)
        
    Returns:
        Optional[AccountInterface]: The account instance, or None if not found
        
    Note:
        Logs specific error messages to help debug transaction/order relationship issues.
        Use is_transaction_orphaned() to check if transaction has no orders before calling this.
        Returns None for FAILED transactions since they shouldn't be processed.
    """
    from .models import AccountDefinition, Transaction
    from .types import TransactionStatus
    
    try:
        # Use provided session or get database connection
        if session is None:
            session = get_db()
        
        # First check if this is a failed transaction - don't process those
        transaction = session.get(Transaction, transaction_id)
        if transaction and transaction.status == TransactionStatus.FAILED:
            logger.debug(f"Transaction {transaction_id} is FAILED - not processing for account lookup")
            return None
        
        # Get first order for this transaction
        orders_statement = select(TradingOrder).where(
            TradingOrder.transaction_id == transaction_id
        ).limit(1)
        first_order = session.exec(orders_statement).first()
        
        if not first_order:
            logger.error(f"No orders found for transaction {transaction_id}. This transaction may be orphaned or corrupted.")
            return None
        
        if not first_order.account_id:
            logger.error(f"Order {first_order.id} for transaction {transaction_id} has no account_id. Order may be corrupted.")
            return None
        
        # Get account instance using the existing helper
        account = get_account_instance_from_id(first_order.account_id, session=session)
        if not account:
            logger.error(f"Could not get account instance for account_id {first_order.account_id} from transaction {transaction_id}")
            return None
        
        return account
        
    except Exception as e:
        logger.error(f"Error getting account for transaction {transaction_id}: {e}", exc_info=True)
        return None


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


def convert_utc_to_local(utc_datetime: datetime) -> datetime:
    """
    Convert UTC datetime to local time.
    
    Args:
        utc_datetime: UTC datetime object (with timezone.utc)
        
    Returns:
        Local datetime object (without timezone, local time)
        
    Example:
        >>> utc_time = datetime.now(timezone.utc)
        >>> local_time = convert_utc_to_local(utc_time)
        >>> print(local_time)  # Displays in local timezone
    """
    try:
        if not utc_datetime:
            return utc_datetime
        
        # If naive datetime, assume UTC
        if utc_datetime.tzinfo is None:
            utc_datetime = utc_datetime.replace(tzinfo=timezone.utc)
        
        # Convert to local time
        local_time = utc_datetime.astimezone()
        
        # Remove timezone info for display (we want naive local time)
        return local_time.replace(tzinfo=None)
    except Exception as e:
        logger.warning(f"Failed to convert UTC time to local: {e}")
        return utc_datetime


def get_risk_manager_mode(settings: dict, default: str = "classic") -> str:
    """
    Get risk_manager_mode setting with fallback to default if missing or invalid.
    
    Args:
        settings: Dictionary of expert settings
        default: Default value if setting is missing or invalid (default: "classic")
        
    Returns:
        Risk manager mode: "smart" or "classic"
        
    Example:
        >>> mode = get_risk_manager_mode(expert.settings)
        >>> # Returns "classic" if missing or invalid
        >>> # Returns "smart" if explicitly set to "smart"
    """
    if not settings or not isinstance(settings, dict):
        return default
    
    risk_manager_mode = settings.get("risk_manager_mode", "") or ""
    risk_manager_mode = risk_manager_mode.strip().lower()
    
    # Validate the value
    valid_modes = ["classic", "smart"]
    if risk_manager_mode not in valid_modes:
        logger.debug(f"Invalid risk_manager_mode '{risk_manager_mode}', using default '{default}'")
        return default
    
    return risk_manager_mode


def get_order_status_color(status: OrderStatus) -> str:
    """
    Get color for order status badge for UI display.
    
    Used in both TransactionsTab and other UI components to ensure consistent
    status coloring across the application.
    
    Args:
        status: OrderStatus enum value
        
    Returns:
        Color string for NiceGUI badge (e.g., 'green', 'blue', 'red', etc.)
        
    Example:
        >>> from ba2_trade_platform.core.types import OrderStatus
        >>> color = get_order_status_color(OrderStatus.FILLED)
        >>> # Returns 'green'
    """
    color_map = {
        OrderStatus.FILLED: 'green',
        OrderStatus.OPEN: 'blue',
        OrderStatus.PENDING: 'orange',
        OrderStatus.WAITING_TRIGGER: 'purple',
        OrderStatus.CANCELED: 'grey',
        OrderStatus.REJECTED: 'red',
        OrderStatus.ERROR: 'red',
        OrderStatus.EXPIRED: 'grey',
        OrderStatus.PARTIALLY_FILLED: 'teal',
        OrderStatus.CLOSED: 'grey',
    }
    return color_map.get(status, 'grey')


# Cache for get_expert_options_for_ui (60-second TTL)
_expert_options_ui_cache: Dict[str, Any] = {
    'data': None,
    'timestamp': 0,
    'ttl': 60
}


def get_expert_options_for_ui() -> tuple[list[str], dict[str, int]]:
    """
    Get list of expert options and ID mapping for UI dropdowns.

    Uses caching to avoid redundant database queries (60-second TTL).

    Used in transaction filtering and other UI components that need to display
    expert selection dropdowns.

    Returns:
        Tuple of (expert_options_list, expert_id_map)
        - expert_options_list: List of display names like ['All', 'TradingAgents-1', 'SmartRisk-2']
        - expert_id_map: Dict mapping display names to expert instance IDs

    Example:
        >>> options, id_map = get_expert_options_for_ui()
        >>> # options = ['All', 'TradingAgents-1', 'SmartRisk-2']
        >>> # id_map = {'All': 'All', 'TradingAgents-1': 1, 'SmartRisk-2': 2}
    """
    global _expert_options_ui_cache
    current_time = time.time()

    # Check if cache is valid
    if (_expert_options_ui_cache['data'] is not None and
        current_time - _expert_options_ui_cache['timestamp'] < _expert_options_ui_cache['ttl']):
        return _expert_options_ui_cache['data']

    from .models import ExpertInstance

    session = get_db()
    try:
        # Get ALL expert instances
        expert_statement = select(ExpertInstance)
        experts = list(session.exec(expert_statement).all())

        # Build expert options list with shortnames
        expert_options = ['All']
        expert_map = {'All': 'All'}
        for expert in experts:
            # Create shortname: use alias, user_description, or fallback to "expert_name-id"
            shortname = expert.alias or expert.user_description or f"{expert.expert}-{expert.id}"
            expert_options.append(shortname)
            expert_map[shortname] = expert.id

        logger.debug(f"[GET_EXPERT_OPTIONS] Built {len(expert_options)} expert options")

        # Update cache
        _expert_options_ui_cache['data'] = (expert_options, expert_map)
        _expert_options_ui_cache['timestamp'] = current_time

        return expert_options, expert_map

    except Exception as e:
        logger.error(f"Error getting expert options: {e}", exc_info=True)
        return ['All'], {'All': 'All'}
    finally:
        session.close()


def log_analysis_batch_start(batch_id: str, expert_instance_id: int, total_jobs: int, analysis_type: str = "ENTER_MARKET", is_scheduled: bool = True, account_id: int = None) -> None:
    """
    Log the start of an analysis batch to the activity log.

    Used for both scheduled and manual analysis batches. For scheduled jobs, batch_id format is
    expertid_HHmm_YYYYMMDD. For manual batches, batch_id is a timestamp-based identifier.

    Args:
        batch_id: Unique batch identifier
        expert_instance_id: ID of the expert instance performing the analysis
        total_jobs: Total number of jobs in this batch
        analysis_type: Type of analysis ("ENTER_MARKET" or "OPEN_POSITIONS")
        is_scheduled: True for scheduled batches, False for manual batches
        account_id: Optional account ID for the expert instance

    Example:
        >>> log_analysis_batch_start("3_0930_20251030", 3, 50, "ENTER_MARKET", is_scheduled=True)
        >>> # Logs: "Analysis batch started: 50 jobs for expert 3, scheduled at 09:30"

        >>> log_analysis_batch_start("manual_1730302000_batch1", 3, 2, "ENTER_MARKET", is_scheduled=False)
        >>> # Logs: "Manual analysis batch started: 2 jobs for expert 3 (AAPL, GOOGL)"
    """
    try:
        from .db import log_activity

        batch_source = "scheduled" if is_scheduled else "manual"

        description = f"Analysis batch started: {total_jobs} jobs ({analysis_type})"
        data = {
            "batch_id": batch_id,
            "expert_id": expert_instance_id,
            "total_jobs": total_jobs,
            "analysis_type": analysis_type,
            "batch_source": batch_source
        }

        log_activity(
            severity=ActivityLogSeverity.INFO,
            activity_type=ActivityLogType.ANALYSIS_STARTED,
            description=description,
            data=data,
            source_expert_id=expert_instance_id,
            source_account_id=account_id
        )
        logger.info(f"Logged batch START for {batch_id}: {total_jobs} jobs")
    except Exception as e:
        logger.warning(f"Failed to log analysis batch start for {batch_id}: {e}")


def log_analysis_batch_end(batch_id: str, expert_instance_id: int, total_jobs: int, elapsed_seconds: int, analysis_type: str = "ENTER_MARKET", is_scheduled: bool = True, account_id: int = None) -> None:
    """
    Log the end of an analysis batch to the activity log with elapsed time.

    Called when the last job in a batch completes.

    Args:
        batch_id: Unique batch identifier (must match the start log batch_id)
        expert_instance_id: ID of the expert instance that performed the analysis
        total_jobs: Total number of jobs in this batch
        elapsed_seconds: Total elapsed time for the entire batch in seconds
        analysis_type: Type of analysis ("ENTER_MARKET" or "OPEN_POSITIONS")
        is_scheduled: True for scheduled batches, False for manual batches
        account_id: Optional account ID for the expert instance

    Example:
        >>> log_analysis_batch_end("3_0930_20251030", 3, 50, 1245, "ENTER_MARKET", is_scheduled=True)
        >>> # Logs: "Analysis batch completed: 50 jobs in 20 minutes 45 seconds"

        >>> log_analysis_batch_end("manual_1730302000_batch1", 3, 2, 45, "ENTER_MARKET", is_scheduled=False)
        >>> # Logs: "Manual analysis batch completed: 2 jobs in 45 seconds"
    """
    try:
        from .db import log_activity

        batch_source = "scheduled" if is_scheduled else "manual"
        minutes = elapsed_seconds // 60
        seconds = elapsed_seconds % 60

        time_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
        description = f"Analysis batch completed: {total_jobs} jobs in {time_str} ({analysis_type})"

        data = {
            "batch_id": batch_id,
            "expert_id": expert_instance_id,
            "total_jobs": total_jobs,
            "elapsed_seconds": elapsed_seconds,
            "elapsed_formatted": time_str,
            "analysis_type": analysis_type,
            "batch_source": batch_source
        }

        log_activity(
            severity=ActivityLogSeverity.SUCCESS,
            activity_type=ActivityLogType.ANALYSIS_COMPLETED,
            description=description,
            data=data,
            source_expert_id=expert_instance_id,
            source_account_id=account_id
        )
        logger.info(f"Logged batch END for {batch_id}: {total_jobs} jobs in {time_str}")
    except Exception as e:
        logger.warning(f"Failed to log analysis batch end for {batch_id}: {e}")


def log_manual_analysis(expert_instance_id: int, symbols: List[str], analysis_type: str = "ENTER_MARKET", is_batch: bool = False) -> None:
    """
    Log a manually-triggered analysis (single symbol or batch).
    
    For single-symbol analysis, logs one entry per symbol.
    For batch analysis (multiple symbols selected at once), logs one entry for the batch.
    
    Args:
        expert_instance_id: ID of the expert instance performing the analysis
        symbols: List of symbols being analyzed
        analysis_type: Type of analysis ("ENTER_MARKET" or "OPEN_POSITIONS")
        is_batch: True if this is a batch selection (multiple symbols), False for single symbol
        
    Example:
        >>> log_manual_analysis(3, ["AAPL"], "ENTER_MARKET", is_batch=False)
        >>> # Logs: "Manual analysis triggered: AAPL"
        
        >>> log_manual_analysis(3, ["AAPL", "GOOGL", "MSFT"], "ENTER_MARKET", is_batch=True)
        >>> # Logs: "Manual batch analysis triggered: 3 symbols"
    """
    try:
        from .db import log_activity
        
        if is_batch:
            description = f"Manual batch analysis triggered: {len(symbols)} symbols ({analysis_type})"
            data = {
                "expert_id": expert_instance_id,
                "symbols": symbols,
                "symbol_count": len(symbols),
                "analysis_type": analysis_type,
                "batch": True
            }
        else:
            symbol = symbols[0] if symbols else "UNKNOWN"
            description = f"Manual analysis triggered: {symbol} ({analysis_type})"
            data = {
                "expert_id": expert_instance_id,
                "symbol": symbol,
                "analysis_type": analysis_type,
                "batch": False
            }
        
        log_activity(
            severity=ActivityLogSeverity.INFO,
            activity_type=ActivityLogType.ANALYSIS_STARTED,
            description=description,
            data=data,
            source_expert_id=expert_instance_id
        )
        logger.info(f"Logged manual analysis for expert {expert_instance_id}: {symbols}")
    except Exception as e:
        logger.warning(f"Failed to log manual analysis for expert {expert_instance_id}: {e}")


def calculate_fmp_trade_metrics(trades: List[dict], all_trader_trades: List[dict] | None = None) -> dict:
    """
    Calculate metrics for FMP Senate/House trades including total money spent
    and percentage of yearly trading.
    
    Analyzes the provided trades to extract financial metrics about the trader's activity.
    
    Args:
        trades: List of trade dictionaries for a specific symbol from FMP API. Each trade should contain:
                - 'amount' (str or int): The amount range of the trade (e.g., "$1,000,001 - $5,000,000")
                - 'transactionDate' (str): The date of the trade (YYYY-MM-DD format)
        all_trader_trades: Optional list of ALL trades by the same trader(s) across all symbols.
                          When provided, percent_of_yearly is calculated as symbol volume / total volume.
                          When omitted, percent_of_yearly is 0.0 (cannot be computed without total).

    Returns:
        Dictionary with metrics:
        - 'total_money_spent': float - Sum of all trade amounts in dollars
        - 'percent_of_yearly': float - Percentage this trade represents of yearly trading volume
        - 'num_trades': int - Number of trades analyzed
        - 'avg_trade_amount': float - Average trade amount
        - 'min_trade_amount': float - Minimum trade amount
        - 'max_trade_amount': float - Maximum trade amount
        - 'trade_value_breakdown': dict - Breakdown of amount ranges and their counts
        - 'error': str (optional) - Error message if calculation failed
    
    Example:
        >>> trades = [
        ...     {'amount': '$1,000,001 - $5,000,000', 'transactionDate': '2025-10-30'},
        ...     {'amount': '$500,001 - $1,000,000', 'transactionDate': '2025-10-25'}
        ... ]
        >>> metrics = calculate_fmp_trade_metrics(trades)
        >>> print(f"Total spent: ${metrics['total_money_spent']:,.0f}")
        >>> print(f"Percent of yearly: {metrics['percent_of_yearly']:.2f}%")
    """
    try:
        if not trades:
            return {
                'total_money_spent': 0.0,
                'percent_of_yearly': 0.0,
                'num_trades': 0,
                'avg_trade_amount': 0.0,
                'min_trade_amount': 0.0,
                'max_trade_amount': 0.0,
                'trade_value_breakdown': {}
            }
        
        from datetime import datetime, timezone, timedelta
        
        # FMP amount ranges and their midpoints for calculation
        AMOUNT_RANGES = {
            '$1,000 - $15,000': 8_000,
            '$15,001 - $50,000': 32_500,
            '$50,001 - $100,000': 75_000,
            '$100,001 - $250,000': 175_000,
            '$250,001 - $500,000': 375_000,
            '$500,001 - $1,000,000': 750_000,
            '$1,000,001 - $5,000,000': 3_000_000,
            '$5,000,001 - $25,000,000': 15_000_000,
            '$25,000,001 - $50,000,000': 37_500_000,
            '$50,000,001 - $100,000,000': 75_000_000,
            '$100,000,001+': 150_000_000,
        }
        
        trade_amounts = []
        value_breakdown = {}
        
        now = datetime.now(timezone.utc)
        year_ago = now - timedelta(days=365)
        
        for trade in trades:
            try:
                amount_range = trade.get('amount', '').strip()
                
                # Track value breakdown
                if amount_range not in value_breakdown:
                    value_breakdown[amount_range] = 0
                value_breakdown[amount_range] += 1
                
                # Get midpoint of the range
                if amount_range in AMOUNT_RANGES:
                    trade_amount = AMOUNT_RANGES[amount_range]
                    trade_amounts.append(trade_amount)
                else:
                    logger.debug(f"Unknown FMP amount range: {amount_range}")
                    # Default to 0 for unknown ranges
                    trade_amounts.append(0)
                    
            except Exception as e:
                logger.debug(f"Error processing individual trade: {e}")
                continue
        
        if not trade_amounts:
            return {
                'total_money_spent': 0.0,
                'percent_of_yearly': 0.0,
                'num_trades': len(trades),
                'avg_trade_amount': 0.0,
                'min_trade_amount': 0.0,
                'max_trade_amount': 0.0,
                'trade_value_breakdown': value_breakdown
            }
        
        # Calculate metrics
        total_spent = sum(trade_amounts)
        avg_amount = total_spent / len(trade_amounts) if trade_amounts else 0.0
        min_amount = min(trade_amounts)
        max_amount = max(trade_amounts)
        
        # Calculate percentage of yearly trading volume
        # Requires all_trader_trades to know the total volume across all symbols
        percent_of_yearly = 0.0
        if all_trader_trades and total_spent > 0:
            total_all_volume = 0.0
            for t in all_trader_trades:
                amount_range = t.get('amount', '').strip()
                if amount_range in AMOUNT_RANGES:
                    total_all_volume += AMOUNT_RANGES[amount_range]
            if total_all_volume > 0:
                percent_of_yearly = (total_spent / total_all_volume) * 100
        
        return {
            'total_money_spent': total_spent,
            'percent_of_yearly': min(percent_of_yearly, 100.0),  # Cap at 100%
            'num_trades': len(trades),
            'avg_trade_amount': avg_amount,
            'min_trade_amount': min_amount,
            'max_trade_amount': max_amount,
            'trade_value_breakdown': value_breakdown
        }
        
    except Exception as e:
        logger.error(f"Error calculating FMP trade metrics: {e}", exc_info=True)
        return {
            'total_money_spent': 0.0,
            'percent_of_yearly': 0.0,
            'num_trades': len(trades) if trades else 0,
            'avg_trade_amount': 0.0,
            'min_trade_amount': 0.0,
            'max_trade_amount': 0.0,
            'trade_value_breakdown': {},
            'error': str(e)
        }


def get_setting_safe(settings: dict, key: str, default, as_type=None):
    """
    Safely get a setting value, handling None values stored in settings dict.
    
    When settings are stored in the database, they might be stored as None if not explicitly set.
    This helper ensures that None values are replaced with the default, and optionally converts
    the result to a specific type.
    
    Args:
        settings: The settings dictionary (from expert.settings)
        key: The setting key to retrieve
        default: The default value to use if key is missing or value is None
        as_type: Optional type to convert the value to (e.g., int, float, str)
        
    Returns:
        The setting value, the default if missing/None, optionally converted to as_type
        
    Examples:
        >>> get_setting_safe(settings, 'max_instruments', 30, int)
        30
        
        >>> get_setting_safe({'max_instruments': None}, 'max_instruments', 30, int)
        30
        
        >>> get_setting_safe({'max_instruments': 50}, 'max_instruments', 30, int)
        50
        
        >>> get_setting_safe({'max_instruments': '50'}, 'max_instruments', 30, int)
        50
    """
    value = settings.get(key, default)
    
    # If value is None (stored as None in DB), use default
    if value is None:
        value = default
    
    # Convert to specified type if requested
    # This also handles case where value was stored as string (e.g., '50') instead of int/float
    if as_type is not None and value is not None:
        try:
            value = as_type(value)
        except (ValueError, TypeError) as e:
            # If conversion fails, use the default
            logger.warning(f"Could not convert setting '{key}' value '{value}' to type {as_type.__name__}: {e}, using default {default}")
            value = default
            if as_type is not None:
                value = as_type(value) if default is not None else None
    
    return value


def get_setting_default_from_interface(interface_class, setting_key: str):
    """
    Get the default value for a setting from an interface class definition.
    
    This function retrieves the default value defined in the interface's builtin settings
    (via get_settings_definitions() or _builtin_settings). This ensures we always use
    the official default from the interface definition, not hardcoded values scattered
    in the codebase.
    
    Args:
        interface_class: The interface class (MarketExpertInterface, AccountInterface, etc.)
        setting_key: The setting key to look up (e.g., "max_virtual_equity_per_instrument_percent")
        
    Returns:
        The default value from the interface definition, or None if not found
        
    Raises:
        ValueError: If the setting is not found in the interface definition
        
    Example:
        >>> from ba2_trade_platform.core.interfaces import MarketExpertInterface
        >>> default = get_setting_default_from_interface(MarketExpertInterface, "max_virtual_equity_per_instrument_percent")
        >>> print(default)  # 10.0
    """
    try:
        # Get merged settings definitions (builtin + implementation-specific)
        settings_defs = interface_class.get_merged_settings_definitions()
        
        if not settings_defs or setting_key not in settings_defs:
            raise ValueError(f"Setting '{setting_key}' not found in {interface_class.__name__} interface definition")
        
        setting_def = settings_defs[setting_key]
        default_value = setting_def.get("default")
        
        if default_value is None:
            logger.warning(f"Setting '{setting_key}' in {interface_class.__name__} has no default value defined")
        
        return default_value
        
    except Exception as e:
        logger.error(f"Error getting default for setting '{setting_key}' from {interface_class.__name__}: {e}", exc_info=True)
        raise


def parse_model_config(model_string: str) -> dict:
    """
    Parse model string to extract provider, model name, and parameters.
    
    Format: Provider/ModelName{key=subkey:value} or Provider/ModelName{key=value} or Provider/ModelName{key:value}
    Examples: 
        - "OpenAI/gpt-5-mini" (no parameters)
        - "NagaAC/gpt-5.1-2025-11-13{reasoning=effort:low}" (nested parameter)
        - "OpenAI/gpt-4{temperature=0.7}" (simple key=value)
        - "OpenAI/gpt-4{temperature:0.7}" (legacy key:value)
    
    Args:
        model_string: Model configuration string
        
    Returns:
        dict with keys: 'provider', 'model', 'base_url', 'api_key_setting', 'model_kwargs'
        
    Example:
        >>> config = parse_model_config("NagaAC/gpt-5.1-2025-11-13{reasoning=effort:low}")
        >>> config['model']
        'gpt-5.1-2025-11-13'
        >>> config['model_kwargs']
        {'reasoning': {'effort': 'low'}}
        
        >>> config = parse_model_config("OpenAI/gpt-4{temperature=0.7}")
        >>> config['model_kwargs']
        {'temperature': '0.7'}
    """
    import re
    
    # Extract parameters from model string (e.g., {reasoning=effort:low})
    model_kwargs = {}
    param_match = re.search(r'\{([^}]+)\}', model_string)
    if param_match:
        params_str = param_match.group(1)
        # Remove parameters from model string to get clean model name
        model_string = model_string[:param_match.start()]
        # Parse parameters (format: key=subkey:value, key=value, or key:value)
        for param in params_str.split(','):
            if '=' in param:
                # Format with equals: key=subkey:value or key=value
                key, nested_part = param.split('=', 1)
                key = key.strip()
                if ':' in nested_part:
                    # Nested format: key=subkey:value (e.g., reasoning=effort:low)
                    subkey, value = nested_part.split(':', 1)
                    model_kwargs[key] = {subkey.strip(): value.strip()}
                else:
                    # Simple key=value format (e.g., temperature=0.7)
                    model_kwargs[key] = nested_part.strip()
            elif ':' in param:
                # Flat format: key:value (legacy support)
                key, value = param.split(':', 1)
                model_kwargs[key.strip()] = value.strip()
        
        # Log parsed parameters for visibility
        if model_kwargs:
            logger.info(f"Parsed model parameters from '{model_string}': {model_kwargs}")
    
    # Handle legacy format (no provider prefix)
    if '/' not in model_string:
        # Default to OpenAI for backward compatibility
        result = {
            'provider': 'OpenAI',
            'model': model_string,
            'base_url': 'https://api.openai.com/v1',
            'api_key_setting': 'openai_api_key',
            'model_kwargs': model_kwargs
        }
        logger.debug(f"Parsed legacy model string '{model_string}' -> provider={result['provider']}, model={result['model']}")
        return result
    
    # Parse Provider/Model format
    provider, model = model_string.split('/', 1)
    
    # Normalize provider to lowercase for consistent matching
    provider_lower = provider.lower()
    
    # Provider configuration mapping (supports both legacy and new formats)
    PROVIDER_CONFIGS = {
        'openai': {
            'provider': 'OpenAI',
            'base_url': 'https://api.openai.com/v1',
            'api_key_setting': 'openai_api_key',
        },
        'nagaai': {
            'provider': 'NagaAI',
            'base_url': 'https://api.naga.ac/v1',
            'api_key_setting': 'naga_ai_api_key',
        },
        'nagaac': {
            'provider': 'NagaAC',
            'base_url': 'https://api.naga.ac/v1',
            'api_key_setting': 'naga_ai_api_key',
        },
        'native': {
            # Native provider - uses the new model registry format
            # Model should be friendly_name, which ModelFactory resolves
            'provider': 'native',
            'base_url': None,  # Will be resolved by ModelFactory
            'api_key_setting': None,  # Will be resolved by ModelFactory
        },
        'google': {
            'provider': 'Google',
            'base_url': None,  # Google uses direct API
            'api_key_setting': 'google_api_key',
        },
        'anthropic': {
            'provider': 'Anthropic',
            'base_url': 'https://api.anthropic.com',
            'api_key_setting': 'anthropic_api_key',
        },
        'openrouter': {
            'provider': 'OpenRouter',
            'base_url': 'https://openrouter.ai/api/v1',
            'api_key_setting': 'openrouter_api_key',
        },
        'xai': {
            'provider': 'xAI',
            'base_url': 'https://api.x.ai/v1',
            'api_key_setting': 'xai_api_key',
        },
        'deepseek': {
            'provider': 'DeepSeek',
            'base_url': 'https://api.deepseek.com',
            'api_key_setting': 'deepseek_api_key',
        },
        'moonshot': {
            'provider': 'Moonshot',
            'base_url': 'https://api.moonshot.ai/v1',  # International endpoint
            'api_key_setting': 'moonshot_api_key',
        },
    }
    
    if provider_lower in PROVIDER_CONFIGS:
        config = PROVIDER_CONFIGS[provider_lower]
        result = {
            'provider': config['provider'],
            'model': model,
            'base_url': config['base_url'],
            'api_key_setting': config['api_key_setting'],
            'model_kwargs': model_kwargs
        }
    else:
        # Unknown provider, default to OpenAI and use just the model name
        logger.warning(f"Unknown provider '{provider}' in model string '{model_string}', defaulting to OpenAI")
        result = {
            'provider': 'OpenAI',
            'model': model,  # Use just the model part, not the full string
            'base_url': 'https://api.openai.com/v1',
            'api_key_setting': 'openai_api_key',
            'model_kwargs': model_kwargs
        }
    
    logger.debug(f"Parsed model string '{model_string}' -> provider={result['provider']}, model={result['model']}, kwargs={result['model_kwargs']}")
    return result
