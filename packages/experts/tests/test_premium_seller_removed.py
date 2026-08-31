"""PremiumSeller is REMOVED (operator decision 2026-08-31; option-model plan Task 12).

The expert was deleted WITHOUT waiting for the option_lifecycle.decide replacement —
its promoted capabilities (book rails, breaker, tested-delta, structure metrics) already
serve every option position from ``ba2_common.core.option_book`` / ``option_lifecycle``
and the option risk manager. These pins make an accidental resurrection (or a stale
import path somewhere) fail a named test instead of silently re-registering the expert.
"""
import importlib

import pytest

import ba2_experts


def test_registry_no_longer_offers_premium_seller():
    assert "PremiumSeller" not in {cls.__name__ for cls in ba2_experts.experts}


def test_get_expert_class_returns_none_for_premium_seller():
    assert ba2_experts.get_expert_class("PremiumSeller") is None


def test_the_module_itself_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ba2_experts.PremiumSeller")


def test_the_promoted_capabilities_survive_the_deletion():
    """The reason the deletion was safe: rails/breaker/lifecycle live in ba2_common now."""
    from ba2_common.core import option_book, option_lifecycle

    for name in ("check_rails", "book_totals", "update_breaker", "BreakerState"):
        assert hasattr(option_book, name), name
    for name in ("structure_metrics", "LifecycleDecision", "OptionStructure"):
        assert hasattr(option_lifecycle, name), name
