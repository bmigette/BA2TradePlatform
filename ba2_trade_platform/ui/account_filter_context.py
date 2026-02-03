"""
Account Filter Context Module

Provides a global context for filtering data by account across the UI.
Uses NiceGUI's app.storage.user for per-session persistence.
"""
from typing import Optional, List, Tuple, Dict, Any
import time
from nicegui import app
from ..core.db import get_all_instances
from ..core.models import AccountDefinition
from ..logger import logger


# Storage key for the selected account filter
ACCOUNT_FILTER_KEY = 'selected_account_id'

# Cache for accounts list (60-second TTL)
_accounts_cache: Dict[str, Any] = {
    'data': None,
    'timestamp': 0,
    'ttl': 60
}

# Cache for expert IDs per account (60-second TTL)
_expert_ids_cache: Dict[str, Any] = {
    'data': {},  # {account_id: [expert_ids]}
    'timestamp': 0,
    'ttl': 60
}


def get_accounts_for_filter() -> List[Tuple[str, int]]:
    """
    Get list of accounts for filter dropdown (cached for 60 seconds).

    Returns:
        List of tuples: [(display_label, account_id), ...]
        First item is always ("All", None) for showing all accounts.
    """
    global _accounts_cache
    current_time = time.time()

    # Check if cache is valid
    if (_accounts_cache['data'] is not None and
        current_time - _accounts_cache['timestamp'] < _accounts_cache['ttl']):
        return _accounts_cache['data']

    options = [("All", None)]

    try:
        accounts = get_all_instances(AccountDefinition)
        for account in accounts:
            label = f"{account.name} ({account.provider})"
            options.append((label, account.id))

        # Update cache
        _accounts_cache['data'] = options
        _accounts_cache['timestamp'] = current_time
    except Exception as e:
        logger.error(f"Error fetching accounts for filter: {e}", exc_info=True)

    return options


def get_selected_account_id() -> Optional[int]:
    """
    Get the currently selected account ID from session storage.
    
    Returns:
        The selected account ID, or None if "All" is selected.
    """
    try:
        account_id = app.storage.user.get(ACCOUNT_FILTER_KEY, None)
        # Handle string "None" or empty string
        if account_id is None or account_id == "None" or account_id == "":
            return None
        return int(account_id)
    except Exception as e:
        logger.warning(f"Error getting selected account ID: {e}")
        return None


def set_selected_account_id(account_id: Optional[int]) -> None:
    """
    Set the selected account ID in session storage.
    
    Args:
        account_id: The account ID to filter by, or None for "All".
    """
    try:
        app.storage.user[ACCOUNT_FILTER_KEY] = account_id
        logger.debug(f"Set account filter to: {account_id}")
    except Exception as e:
        logger.warning(f"Error setting selected account ID: {e}")


def get_expert_ids_for_account(account_id: Optional[int]) -> Optional[List[int]]:
    """
    Get list of expert instance IDs belonging to a specific account (cached for 60 seconds).

    Args:
        account_id: The account ID to filter by, or None for all experts.

    Returns:
        List of expert instance IDs, or None if account_id is None (meaning all).
    """
    if account_id is None:
        return None

    global _expert_ids_cache
    current_time = time.time()

    # Check if cache is valid and has this account
    if (current_time - _expert_ids_cache['timestamp'] < _expert_ids_cache['ttl'] and
        account_id in _expert_ids_cache['data']):
        return _expert_ids_cache['data'][account_id]

    try:
        from ..core.models import ExpertInstance
        from ..core.db import get_db
        from sqlmodel import select

        with get_db() as session:
            statement = select(ExpertInstance.id).where(ExpertInstance.account_id == account_id)
            expert_ids = list(session.exec(statement).all())
            result = expert_ids if expert_ids else []

            # Update cache (reset if TTL expired)
            if current_time - _expert_ids_cache['timestamp'] >= _expert_ids_cache['ttl']:
                _expert_ids_cache['data'] = {}
                _expert_ids_cache['timestamp'] = current_time

            _expert_ids_cache['data'][account_id] = result
            return result
    except Exception as e:
        logger.error(f"Error fetching expert IDs for account {account_id}: {e}", exc_info=True)
        return None
