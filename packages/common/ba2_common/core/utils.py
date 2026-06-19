"""
Utility functions for the BA2 Trade Platform core functionality.
"""

from typing import Optional, List, TYPE_CHECKING, Dict, Any
from datetime import datetime, timezone
import time
from ba2_common.core.db import get_instance, get_db
from ba2_common.core.models import ExpertInstance, TradingOrder, ExpertRecommendation, Transaction
from ba2_common.core.types import OrderStatus, TransactionStatus, ActivityLogSeverity, ActivityLogType, OrderDirection
from ba2_common.logger import logger
from sqlmodel import Session, select

if TYPE_CHECKING:
    from ba2_common.core.interfaces import MarketExpertInterface


def get_labels_by_symbol(symbols) -> Dict[str, List[str]]:
    """Return ``{symbol: [labels]}`` for symbols that have an Instrument row.

    Symbols without an Instrument (or with no labels) are simply omitted, so the
    caller can default to an empty list.
    """
    from ba2_common.core.models import Instrument
    syms = [s for s in {s for s in symbols} if s]
    if not syms:
        return {}
    out: Dict[str, List[str]] = {}
    with get_db() as session:
        rows = session.exec(select(Instrument).where(Instrument.name.in_(syms))).all()
        for inst in rows:
            out[inst.name] = list(inst.labels or [])
    return out


def get_all_instrument_labels() -> List[str]:
    """Return the sorted, de-duplicated set of all labels in use across instruments."""
    from ba2_common.core.models import Instrument
    labels = set()
    with get_db() as session:
        for inst in session.exec(select(Instrument)).all():
            for lbl in (inst.labels or []):
                labels.add(lbl)
    return sorted(labels)


def add_label_to_instruments(symbols, label: str) -> int:
    """Add ``label`` to each symbol's Instrument, creating a minimal Instrument row
    when one doesn't exist. No-op for a blank label or a label already present.

    The labels list is REASSIGNED (not mutated in place) so SQLAlchemy reliably
    detects the change on the JSON column. Returns the number of instruments
    created or updated.
    """
    from ba2_common.core.models import Instrument
    label = (label or "").strip()
    if not label:
        return 0
    changed = 0
    with get_db() as session:
        for sym in {s for s in symbols if s}:
            inst = session.exec(select(Instrument).where(Instrument.name == sym)).first()
            if inst is None:
                session.add(Instrument(name=sym, labels=[label]))
                changed += 1
            elif label not in (inst.labels or []):
                inst.labels = list(inst.labels or []) + [label]
                session.add(inst)
                changed += 1
        if changed:
            session.commit()
    return changed


def remove_label_from_instruments(symbols, label: str) -> int:
    """Remove ``label`` from each symbol's Instrument (if present). Returns the
    number of instruments updated. The labels list is reassigned for change
    detection on the JSON column."""
    from ba2_common.core.models import Instrument
    label = (label or "").strip()
    if not label:
        return 0
    changed = 0
    with get_db() as session:
        for sym in {s for s in symbols if s}:
            inst = session.exec(select(Instrument).where(Instrument.name == sym)).first()
            if inst and label in (inst.labels or []):
                inst.labels = [l for l in inst.labels if l != label]
                session.add(inst)
                changed += 1
        if changed:
            session.commit()
    return changed


def expert_uses_risk_manager(expert_class) -> bool:
    """Resolve whether an expert class relies on the platform's risk manager.

    Experts can opt out of the platform risk manager (classic/smart) when they
    self-execute their trades (e.g. FactorRanker via FactorPortfolioManager).
    The signal is read robustly: ``get_expert_properties()`` takes precedence,
    then the ``uses_risk_manager`` class attribute, defaulting to True. Any
    error resolving the value also defaults to True (safe / backward-compatible).

    Args:
        expert_class: The expert class (not instance) to inspect.

    Returns:
        bool: True if the platform risk manager should be triggered for this
        expert, False if the expert manages its own risk/execution.
    """
    class_attr_default = getattr(expert_class, "uses_risk_manager", True)
    try:
        return bool(expert_class.get_expert_properties().get("uses_risk_manager", class_attr_default))
    except Exception as e:
        logger.warning(f"expert_uses_risk_manager: failed to resolve for {expert_class!r}, defaulting to True: {e}")
        return True


def expert_schedules_open_positions(expert_class) -> bool:
    """Resolve whether an expert class schedules a separate open-positions job.

    Experts that handle entries and exits in a single batch run (e.g. FactorRanker)
    declare ``schedules_open_positions=False`` and have no OPEN_POSITIONS job — so
    UI views must not render an open-positions schedule for them. Read robustly:
    ``get_expert_properties()`` first, then the ``schedules_open_positions`` class
    attribute, defaulting to True (backward-compatible). Errors also default to True.

    Args:
        expert_class: The expert class (not instance) to inspect.

    Returns:
        bool: True if the expert has an open-positions schedule, False otherwise.
    """
    class_attr_default = getattr(expert_class, "schedules_open_positions", True)
    try:
        return bool(expert_class.get_expert_properties().get("schedules_open_positions", class_attr_default))
    except Exception as e:
        logger.warning(f"expert_schedules_open_positions: failed to resolve for {expert_class!r}, defaulting to True: {e}")
        return True


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
        from ba2_common.core.models import Transaction
        from ba2_common.core.types import TransactionStatus
        
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


def get_latest_recommendation_id_for_symbol(symbol: str,
                                            expert_instance_ids: Optional[List[int]] = None) -> Optional[int]:
    """Return the id of the most recent ExpertRecommendation for a symbol.

    Used by the per-symbol "Place Order" action so a manually-created order can be
    linked back to an expert. The order stores this id in
    ``TradingOrder.expert_recommendation_id``; ``_create_transaction_for_order``
    then resolves ``Transaction.expert_id`` from ``ExpertRecommendation.instance_id``.

    Recency is by ``ExpertRecommendation.id`` (monotonic), matching how the Trade
    Recommendations summary picks each symbol's latest row.

    Args:
        symbol: The instrument symbol to look up.
        expert_instance_ids: Optional scope of expert instance ids. ``None`` means
            no expert restriction (overall latest). An explicit empty list means
            "no experts in scope" and yields ``None`` (no match).

    Returns:
        Optional[int]: The latest recommendation id, or None if none match.
    """
    if expert_instance_ids is not None and not expert_instance_ids:
        return None
    try:
        with get_db() as session:
            statement = (
                select(ExpertRecommendation.id)
                .where(ExpertRecommendation.symbol == symbol)
            )
            if expert_instance_ids is not None:
                statement = statement.where(ExpertRecommendation.instance_id.in_(expert_instance_ids))
            statement = statement.order_by(ExpertRecommendation.id.desc()).limit(1)
            return session.exec(statement).first()
    except Exception as e:
        logger.error(f"Error resolving latest recommendation for symbol {symbol}: {e}", exc_info=True)
        return None


def get_account_id_for_recommendation(recommendation_id: Optional[int]) -> Optional[int]:
    """Return the account id that owns the expert behind a recommendation.

    Resolves ``ExpertRecommendation.instance_id`` -> ``ExpertInstance.account_id``
    so a manually-placed order can be submitted to the recommending expert's own
    account rather than defaulting to the first configured account.

    Args:
        recommendation_id: The recommendation id, or None.

    Returns:
        Optional[int]: The owning account id, or None if it can't be resolved.
    """
    if recommendation_id is None:
        return None
    try:
        recommendation = get_instance(ExpertRecommendation, recommendation_id)
        if not recommendation or recommendation.instance_id is None:
            return None
        expert_instance = get_instance(ExpertInstance, recommendation.instance_id)
        if not expert_instance:
            return None
        return expert_instance.account_id
    except Exception as e:
        logger.error(f"Error resolving account for recommendation {recommendation_id}: {e}", exc_info=True)
        return None


def calculate_transaction_pnl(transaction: Transaction) -> Optional[float]:
    """
    Calculate P&L for a transaction, correctly handling both long and short positions.

    For LONG (BUY): P&L = (close_price - open_price) * quantity * multiplier
    For SHORT (SELL): P&L = (open_price - close_price) * quantity * multiplier

    The contract multiplier (100 for standard options, null/1 for equity) scales the
    per-share premium to actual cash so option positions are not understated ~100x.

    Args:
        transaction: Transaction with open_price, close_price, quantity, side and
            (optional) multiplier fields

    Returns:
        P&L as float, or None if required fields are missing
    """
    if not transaction.close_price or not transaction.open_price or not transaction.quantity:
        return None
    multiplier = getattr(transaction, "multiplier", None) or 1
    if transaction.side == OrderDirection.BUY:
        return (transaction.close_price - transaction.open_price) * transaction.quantity * multiplier
    else:  # Short position
        return (transaction.open_price - transaction.close_price) * transaction.quantity * multiplier


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
            from ba2_common.core.db import log_activity
            
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
        from ba2_common.core.db import log_activity
        
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
        from ba2_common.core.db import log_activity
        
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
        from ba2_common.core.db import log_activity
        
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
        >>> from ba2_common.core.types import OrderStatus
        >>> color = get_order_status_color(OrderStatus.FILLED)
        >>> # Returns 'green'
    """
    color_map = {
        OrderStatus.FILLED: 'green',
        OrderStatus.OPEN: 'blue',
        OrderStatus.PENDING: 'orange',
        OrderStatus.WAITING_TRIGGER: 'purple',
        OrderStatus.WASHTRADE_LOCKED: 'amber',
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

    from ba2_common.core.models import ExpertInstance

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
        from ba2_common.core.db import log_activity

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
        from ba2_common.core.db import log_activity

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
        from ba2_common.core.db import log_activity
        
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


def parse_fmp_amount_range(amount_str) -> float:
    """Parse an FMP congressional-trade amount string to a numeric dollar value.

    FMP reports trade sizes as ranges like ``"$15,001 - $50,000"`` (returns the
    midpoint) or a single value like ``"$1,000"``. Non-numeric/empty input returns
    0.0. Extracted (EX-2) to replace the same block duplicated 4x in
    FMPSenateTraderWeight.
    """
    if not amount_str:
        return 0.0
    amount_str = str(amount_str)
    try:
        if '-' in amount_str:
            parts = amount_str.split('-')
            low = ''.join(c for c in parts[0] if c.isdigit() or c == '.')
            high = ''.join(c for c in parts[1] if c.isdigit() or c == '.')
            return (float(low) + float(high)) / 2 if low and high else 0.0
        digits = ''.join(c for c in amount_str if c.isdigit() or c == '.')
        return float(digits) if digits else 0.0
    except (ValueError, IndexError):
        return 0.0


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
