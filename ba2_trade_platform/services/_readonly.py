"""Read-only mode for the live buying-power read, so a management-API GET cannot change trading state.

``MarketExpertInterface._get_actual_available_balance`` (reached by ``expert.describe_capital_mapping()``) WRITES
the class-level, per-account buying-power failure memory that live sizing reads (``_remember_bp_failure`` /
``_clear_bp_failure``) and sleeps up to 0.75 s between retries. An API read must do neither: a monitoring
client polling a broker that is down would otherwise keep live sizing in "remembered failure" mode, and a
read that succeeds would clear a failure live sizing is still entitled to see.

The fix lives here, in-tree (``packages/`` is not touched): ``install()`` wraps that staticmethod once. The
wrapper delegates to the original UNLESS the calling thread is inside ``readonly_balance_reads()``, where it does
exactly ONE attempt (``_read_actual_buying_power``), touches no memory, never sleeps, and turns "unreadable" into
a ``ValueError`` -- which the balance breakdown's own handler already turns into "expert balance unavailable"
(-> ``Unmeasurable`` 503), instead of silently skipping the clamp and reporting an overstated figure.
Threads outside the scope (live sizing, the schedulers) keep the original behaviour byte for byte."""
import threading
from contextlib import contextmanager
from typing import Optional

import importlib

_mei_module = importlib.import_module("ba2_common.core.interfaces.MarketExpertInterface")
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface

_local = threading.local()
_install_lock = threading.Lock()


def _readonly_actual_buying_power(account, expert_id: Optional[int]) -> Optional[float]:
    mandatory = getattr(account, "buying_power_is_mandatory", False) is True
    status, figure, error = MarketExpertInterface._read_actual_buying_power(account, snapshot_only=mandatory)
    if status == _mei_module._BP_FIGURE:
        return figure
    if status == _mei_module._BP_UNREADABLE or mandatory:
        why = type(error).__name__ if error is not None else "empty answer"
        raise ValueError(f"account {getattr(account, 'id', None)}: buying power could not be read ({why})")
    return None        # the broker answered and publishes no buying power: no clamp, exactly as live


def install() -> None:
    """Wrap ``_get_actual_available_balance`` once (idempotent)."""
    with _install_lock:
        current = MarketExpertInterface.__dict__["_get_actual_available_balance"]
        if getattr(current.__func__, "_ba2_readonly_wrapper", False):
            return
        original = current.__func__

        def wrapper(account, expert_id=None):
            if getattr(_local, "on", False):
                return _readonly_actual_buying_power(account, expert_id)
            return original(account, expert_id)

        wrapper._ba2_readonly_wrapper = True
        MarketExpertInterface._get_actual_available_balance = staticmethod(wrapper)


@contextmanager
def readonly_balance_reads():
    """Within this block, on THIS thread, the buying-power read is side-effect free (see the module docstring)."""
    install()
    previous = getattr(_local, "on", False)
    _local.on = True
    try:
        yield
    finally:
        _local.on = previous
