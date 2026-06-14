"""Tests for the live InstanceResolver seam (Phase 6 Task 2).

``ba2_common.core.instance_resolver`` defines the ``InstanceResolver`` protocol
that the extracted interface bases use to turn an expert/account id (or a
transaction) into a live instance. ``ba2_trade_platform.core.instance_registry``
supplies the concrete ``LiveInstanceResolver`` that delegates to the RETAINED
live factory functions + singleton instance caches.

These tests prove:
- ``LiveInstanceResolver`` satisfies the runtime-checkable protocol,
- it resolves a real expert from the (test) DB through the live caches,
- it forwards account-id / transaction lookups to the live factory funcs
  (delegation verified without constructing a live broker), and
- the optional ``invalidate_instance`` hook is delegated to both caches and is
  safe on unknown / out-of-cache ids.
"""
from tests.factories import create_account_definition, create_expert_instance


def test_live_resolver_satisfies_protocol():
    from ba2_common.core.instance_resolver import InstanceResolver
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver

    r = LiveInstanceResolver()
    assert isinstance(r, InstanceResolver)  # runtime_checkable Protocol


def test_resolver_resolves_expert_from_db():
    """Create a real ExpertInstance row and resolve it through the live caches."""
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver

    acct = create_account_definition(provider="MockAccount")
    inst = create_expert_instance(account_id=acct.id, expert="FMPEarningsDrift")

    r = LiveInstanceResolver()
    obj = r.get_expert_instance(inst.id)
    assert obj is not None
    assert obj.id == inst.id


def test_resolver_caches_expert_instance():
    """Two resolutions of the same id return the same cached object (singleton)."""
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver

    acct = create_account_definition(provider="MockAccount")
    inst = create_expert_instance(account_id=acct.id, expert="FMPEarningsDrift")

    r = LiveInstanceResolver()
    first = r.get_expert_instance(inst.id)
    second = r.get_expert_instance(inst.id)
    assert first is second


def test_resolver_get_account_instance_delegates(monkeypatch):
    """get_account_instance forwards the id to the live factory func verbatim."""
    from ba2_trade_platform.core import utils as live_utils
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver

    sentinel = object()
    captured = {}

    def fake(account_id, *a, **kw):
        captured["account_id"] = account_id
        return sentinel

    monkeypatch.setattr(live_utils, "get_account_instance_from_id", fake)

    out = LiveInstanceResolver().get_account_instance(42)
    assert out is sentinel
    assert captured["account_id"] == 42


def test_resolver_from_transaction_accepts_object_and_id(monkeypatch):
    """get_account_instance_from_transaction unwraps a Transaction-shaped object
    to its ``id`` and also accepts a bare id (the live helper is keyed by id)."""
    from ba2_trade_platform.core import utils as live_utils
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver

    captured = {}

    def fake(transaction_id, *a, **kw):
        captured["txn_id"] = transaction_id
        return ("acct", transaction_id)

    monkeypatch.setattr(live_utils, "get_account_instance_from_transaction", fake)

    r = LiveInstanceResolver()

    class _Txn:
        id = 7

    # Object with .id -> unwrapped to the id
    assert r.get_account_instance_from_transaction(_Txn()) == ("acct", 7)
    assert captured["txn_id"] == 7

    # Bare id -> passed through unchanged
    assert r.get_account_instance_from_transaction(99) == ("acct", 99)
    assert captured["txn_id"] == 99


def test_invalidate_instance_delegates_to_both_caches(monkeypatch):
    """The optional duck-typed invalidate_instance(id) hook (called by
    ExtendableSettingsInterface after a settings write) invalidates the id from
    BOTH live caches, since the caller only knows the bare id."""
    from ba2_trade_platform.core import ExpertInstanceCache as eic_mod
    from ba2_trade_platform.core import AccountInstanceCache as aic_mod
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver

    expert_calls = []
    account_calls = []
    monkeypatch.setattr(
        eic_mod.ExpertInstanceCache, "invalidate_instance",
        classmethod(lambda cls, i: expert_calls.append(i)),
    )
    monkeypatch.setattr(
        aic_mod.AccountInstanceCache, "invalidate_instance",
        classmethod(lambda cls, i: account_calls.append(i)),
    )

    LiveInstanceResolver().invalidate_instance(123)
    assert expert_calls == [123]
    assert account_calls == [123]


def test_invalidate_instance_is_noop_safe_on_unknown_id():
    """Invalidating an id absent from both caches must not raise."""
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver

    # Should be a silent no-op (each cache's invalidate_instance is a no-op when
    # the id is not present).
    LiveInstanceResolver().invalidate_instance(2_000_000_001)


def test_invalidate_instance_exposed_as_duck_typed_hook():
    """ExtendableSettingsInterface looks up invalidate_instance via getattr; it
    must be present and callable on the resolver."""
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver

    r = LiveInstanceResolver()
    invalidate = getattr(r, "invalidate_instance", None)
    assert callable(invalidate)
