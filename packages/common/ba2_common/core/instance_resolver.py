"""Instance-resolution seam.

The interface bases need to turn an expert/account *id* into a live instance, but
the registries + instance caches that do that are live-platform runtime
(BA2TradePlatform). ba2_common defines the protocol; the host app injects a
concrete resolver at startup via set_instance_resolver(). Until then, calling a
resolver method raises InstanceResolverNotConfigured (loud, not silent)."""
from __future__ import annotations
from typing import Any, Optional, Protocol, runtime_checkable


class InstanceResolverNotConfigured(RuntimeError):
    """Raised when interface code needs an instance resolver but none is injected."""


@runtime_checkable
class InstanceResolver(Protocol):
    def get_expert_instance(self, expert_id: int) -> Any: ...
    def get_account_instance(self, account_id: int) -> Any: ...
    def get_account_instance_from_transaction(self, transaction: Any) -> Any: ...


class _UnconfiguredResolver:
    def _fail(self, *_a, **_k):
        raise InstanceResolverNotConfigured(
            "No InstanceResolver injected. The host app must call "
            "ba2_common.core.instance_resolver.set_instance_resolver(<resolver>) at startup."
        )
    get_expert_instance = _fail
    get_account_instance = _fail
    get_account_instance_from_transaction = _fail


_resolver: InstanceResolver = _UnconfiguredResolver()  # type: ignore[assignment]


def set_instance_resolver(resolver: InstanceResolver) -> None:
    global _resolver
    _resolver = resolver


def get_instance_resolver() -> InstanceResolver:
    return _resolver
