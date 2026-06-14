"""Live InstanceResolver implementation for the seam defined in
``ba2_common.core.instance_resolver``.

The extracted interface bases (``ba2_common.core.interfaces.*``,
``ba2_common.core.Trade*``) need to turn an expert/account *id* (or a
transaction) into a live instance, but the registries + singleton instance
caches that do that are live-platform runtime and ``ba2_common`` must never
import them. ``ba2_common`` defines the ``InstanceResolver`` protocol; this host
module supplies the concrete implementation, injected once at startup via
``ba2_common.core.instance_resolver.set_instance_resolver(LiveInstanceResolver())``
(wired in ``core/seam_wiring.py``).

``LiveInstanceResolver`` delegates to the RETAINED live factory functions in
``core/utils.py`` (``get_expert_instance_from_id`` /
``get_account_instance_from_id`` / ``get_account_instance_from_transaction``),
which in turn are backed by the live ``ExpertInstanceCache`` /
``AccountInstanceCache`` singletons. It also exposes the optional, duck-typed
``invalidate_instance(id)`` hook that
``ExtendableSettingsInterface._invalidate_settings_cache`` calls after a settings
write so the host caches drop the stale instance.
"""
from __future__ import annotations

from typing import Any

from ..logger import logger


class LiveInstanceResolver:
    """Concrete ``ba2_common`` ``InstanceResolver`` backed by the live caches +
    registry factory functions.

    Satisfies the ``ba2_common.core.instance_resolver.InstanceResolver``
    runtime-checkable protocol (``get_expert_instance`` / ``get_account_instance``
    / ``get_account_instance_from_transaction``) and additionally provides the
    optional ``invalidate_instance`` hook.
    """

    def get_expert_instance(self, expert_id: int) -> Any:
        # Import lazily to avoid import-time cycles during startup wiring
        # (utils -> modules.experts/accounts registries -> back into core).
        from .utils import get_expert_instance_from_id

        return get_expert_instance_from_id(expert_id)

    def get_account_instance(self, account_id: int) -> Any:
        from .utils import get_account_instance_from_id

        return get_account_instance_from_id(account_id)

    def get_account_instance_from_transaction(self, transaction: Any) -> Any:
        from .utils import get_account_instance_from_transaction

        # ba2_common's protocol passes a Transaction-shaped value; the live helper
        # is keyed by transaction_id. Accept either a Transaction row (or any
        # object with an ``id``) or a bare id.
        txn_id = getattr(transaction, "id", transaction)
        return get_account_instance_from_transaction(txn_id)

    def invalidate_instance(self, instance_id: int) -> None:
        """Optional duck-typed hook called by
        ``ExtendableSettingsInterface._invalidate_settings_cache`` after a
        settings write.

        The caller only knows the instance ``id`` (it does not know whether it
        belongs to an expert or an account), so invalidate the id from BOTH live
        caches. ``invalidate_instance`` on each cache is a no-op when the id is
        not present, so invalidating the wrong cache is harmless.
        """
        try:
            from .ExpertInstanceCache import ExpertInstanceCache

            ExpertInstanceCache.invalidate_instance(instance_id)
        except Exception as e:  # pragma: no cover - defensive, never break a write
            logger.warning(
                f"ExpertInstanceCache.invalidate_instance failed for id={instance_id}: {e}"
            )
        try:
            from .AccountInstanceCache import AccountInstanceCache

            AccountInstanceCache.invalidate_instance(instance_id)
        except Exception as e:  # pragma: no cover - defensive, never break a write
            logger.warning(
                f"AccountInstanceCache.invalidate_instance failed for id={instance_id}: {e}"
            )
