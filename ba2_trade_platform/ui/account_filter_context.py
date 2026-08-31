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


# Base storage key for the selected account filter. The key ACTUALLY used is per-instance
# -- see ``account_filter_storage_key``.
ACCOUNT_FILTER_KEY = 'selected_account_id'


def account_filter_storage_key() -> str:
    """The ``app.storage.user`` key for THIS instance's account filter.

    WHY THE PORT IS IN THE KEY. Two instances of this app on different ports share one
    account filter, and changing it in either moved both. That is not a NiceGUI bug and
    it is not fixable by choosing a different storage backend:

      * HTTP cookies are NOT scoped by port (RFC 6265 §8.5 -- "cookies do not provide
        isolation by port"). A cookie set by ``localhost:8080`` is sent to
        ``localhost:9090`` and vice versa.
      * ``app.storage.user`` is keyed off that cookie, and both instances sign it with
        the same hardcoded ``STORAGE_SECRET``, so each accepts the other's cookie and
        resolves it to the SAME user identity.
      * Run from one working directory they then read and write one
        ``.nicegui/storage-user-<id>.json``.

    Namespacing the KEY is what actually separates them, because it is the only part of
    that chain this app controls. (Per-instance storage secrets would also work, but
    would log the operator out of both instances on every change and would still leave
    one shared storage file.)

    THE PORT, specifically, because it is the one property two concurrently running
    instances cannot share -- the OS guarantees it. This matters more than tidiness:
    ``cli.py`` documents running a second instance with its own ``--db-file``, and
    account IDS ARE DATABASE-SCOPED. Sharing the filter across two databases does not
    merely reset the dropdown, it silently selects a DIFFERENT account -- id 2 in dev is
    not id 2 in prod. The existing deleted-account check does not catch that case,
    because id 2 exists in both.

    Read LAZILY, never imported at module scope: ``main.py`` assigns
    ``config.HTTP_PORT = args.port`` at runtime, so a ``from ..config import HTTP_PORT``
    here would capture the 8080 default at import time and both instances would agree
    again -- reintroducing the bug in a form that only shows up with ``--port``.
    """
    from .. import config
    port = getattr(config, 'HTTP_PORT', None)
    if not port:
        # No port to namespace by. Fall back to the bare key rather than inventing one:
        # a made-up suffix would silently orphan a selection that is already stored.
        return ACCOUNT_FILTER_KEY
    return f'{ACCOUNT_FILTER_KEY}:{port}'

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


def refresh_accounts_for_filter() -> Optional[List[Tuple[str, int]]]:
    """
    Re-read the accounts list from the database, BYPASSING the 60-second cache.

    Callers use "this id is not in the list" to decide an account was deleted, and
    the cached list can be up to a minute out of date -- an account created moments
    ago is legitimately missing from it. So that decision needs a listing read now,
    not a possibly-stale one.

    Returns:
        The fresh options (same shape as ``get_accounts_for_filter``), or ``None``
        if the database could not be read. ``None`` is NOT an empty list on
        purpose: "could not tell" and "there are no accounts" must not be the same
        answer, or one failed query would look like every account was deleted.
    """
    global _accounts_cache

    try:
        accounts = get_all_instances(AccountDefinition)
    except Exception as e:
        logger.error(f"Error fetching accounts for filter: {e}", exc_info=True)
        return None

    options = [("All", None)]
    for account in accounts:
        label = f"{account.name} ({account.provider})"
        options.append((label, account.id))

    _accounts_cache['data'] = options
    _accounts_cache['timestamp'] = time.time()
    return options


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

    options = refresh_accounts_for_filter()
    if options is not None:
        return options

    # The read failed. Serve the last listing we know was real rather than an
    # empty dropdown: the alternative reads to callers as "every account is gone".
    if _accounts_cache['data'] is not None:
        return _accounts_cache['data']
    return [("All", None)]


# Process-wide mirror of the per-session selection. app.storage.user is per-session and ONLY
# accessible inside a UI/client context, but several dashboard widgets compute their data in
# asyncio.to_thread (no UI context) and need the filter too. We mirror the last value seen/set
# in a UI context here so those threaded callers fall back to it instead of dropping the filter
# (which made them aggregate ALL accounts). This is a single-user app, so a process-global
# mirror is correct; app.storage.user remains the source of truth that persists across restarts.
_last_known_account_id: Optional[int] = None


def _coerce_account_id(account_id) -> Optional[int]:
    """Normalize a stored value to int id or None ('All'). Handles "None"/"" strings."""
    if account_id is None or account_id == "None" or account_id == "":
        return None
    return int(account_id)


def get_selected_account_id() -> Optional[int]:
    """
    Get the currently selected account ID from session storage.

    Falls back to the last value seen in a UI context when called outside one
    (e.g. from asyncio.to_thread), so background widgets still honor the filter.

    Returns:
        The selected account ID, or None if "All" is selected.
    """
    global _last_known_account_id
    try:
        account_id = _coerce_account_id(app.storage.user.get(account_filter_storage_key(), None))
        _last_known_account_id = account_id  # keep the thread-readable mirror fresh
        return account_id
    except Exception as e:
        # Outside a UI context app.storage.user is unavailable. Use the cached mirror rather
        # than silently returning None (which dropped the account filter). DEBUG, not WARNING:
        # this is the expected path for threaded/background callers.
        logger.debug(f"get_selected_account_id: storage unavailable ({e}); using cached {_last_known_account_id}")
        return _last_known_account_id


def set_selected_account_id(account_id: Optional[int]) -> None:
    """
    Set the selected account ID in session storage.

    Args:
        account_id: The account ID to filter by, or None for "All".
    """
    global _last_known_account_id
    # Coerce ONCE and store the same value in both places. Mirroring the coerced id while
    # persisting the raw one let the two disagree about the same choice ("2" vs 2), and
    # app.storage.user is serialised to JSON on every write, so a stray string would have
    # outlived the session it came from.
    coerced = _coerce_account_id(account_id)
    _last_known_account_id = coerced  # mirror first (always succeeds)
    try:
        app.storage.user[account_filter_storage_key()] = coerced
        logger.debug(f"Set account filter to: {coerced}")
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
