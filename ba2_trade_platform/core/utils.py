"""Live utils (Phase 6 split-shim).

The pure, registry-free helpers are re-exported from ``ba2_common.core.utils``
(single source of truth). The three instance-factory functions + the registry
glue STAY LIVE here, because they need the live registries (``get_expert_class`` /
``get_account_class``) and the live singleton instance caches
(``ExpertInstanceCache`` / ``AccountInstanceCache``) — which ba2_common
deliberately does NOT import. These three funcs back ``LiveInstanceResolver``
(core/instance_registry.py) and are still called by name across the live tree
(WorkerQueue, TradeManager, UI pages), so they keep their original signatures.
"""
from __future__ import annotations

from typing import Optional
from sqlmodel import Session, select

# Pure helpers now live in the package (single source of truth). ba2_common.core.utils
# has no __all__, so `*` re-exports every public name (the 24 shared pure functions).
from ba2_common.core.utils import *  # noqa: F401,F403

# Imports the live-only factory funcs need at module scope:
from .db import get_instance, get_db
from .models import ExpertInstance, TradingOrder, Transaction  # noqa: F401
from ..modules.experts import get_expert_class
from ..modules.accounts import get_account_class
from ..logger import logger

if False:  # TYPE_CHECKING-style hint without importing at runtime
    from .interfaces.MarketExpertInterface import MarketExpertInterface  # noqa: F401


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
