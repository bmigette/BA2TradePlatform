"""max_virtual_equity_per_instrument_percent reused as a supplementary ceiling on option
sizing (_OptionEntryAction._size / _size_by_reserve).

Before this, an option entry's dollar budget was ONLY option_sizing% of total virtual
equity -- a fixed, non-GA-tuned per-structure constant with no ceiling at all: a single
expensive/illiquid contract could consume the whole option_sizing budget (or, for a
generously-funded account, far more in absolute terms) with nothing to stop it. This adds
the SAME per-instrument cap the classic equity RM path already enforces
(TradeRiskManagement.py) as a second, tighter-wins ceiling.
"""
import math
import os
import tempfile
from types import SimpleNamespace

import pytest

from ba2_common.core import db
from ba2_common.core.db import add_instance
from ba2_common.core.models import ExpertInstance
from ba2_common.core.TradeActions import BuyCallAction
from ba2_common.core.types import OrderRecommendation


class _FakeAccount:
    def __init__(self, balance):
        self._balance = balance

    def get_balance(self):
        return self._balance


def _setup_db():
    db_path = os.path.join(tempfile.mkdtemp(), "opt_sizing_cap.sqlite")
    db.configure_db(db_path)
    db.init_db()


def _make_action(balance, instance_id, virtual_equity_pct=100.0):
    _setup_db()
    inst = ExpertInstance(account_id=1, expert="MockExpert", virtual_equity_pct=virtual_equity_pct)
    real_id = add_instance(inst)
    action = BuyCallAction.__new__(BuyCallAction)
    action.instrument_name = "AAPL"
    action.account = _FakeAccount(balance)
    action.expert_recommendation = SimpleNamespace(instance_id=real_id, id=None)
    action.existing_order = None
    return action, real_id


def _patch_resolver(monkeypatch, settings: dict):
    """Monkeypatch get_instance_resolver().get_expert_instance(id) to return a stub expert
    exposing only `.settings` (all _max_equity_per_instrument_cap needs)."""
    import ba2_common.core.instance_resolver as ir_mod

    class _StubResolver:
        def get_expert_instance(self, instance_id):
            return SimpleNamespace(settings=settings)

    monkeypatch.setattr(ir_mod, "get_instance_resolver", lambda: _StubResolver())


class TestMaxEquityPerInstrumentCap:
    def test_returns_none_when_setting_unset(self, monkeypatch):
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        _patch_resolver(monkeypatch, {})
        assert action._max_equity_per_instrument_cap(100_000.0) is None

    def test_returns_dollar_cap_from_percent(self, monkeypatch):
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        _patch_resolver(monkeypatch, {"max_virtual_equity_per_instrument_percent": 20.0})
        assert action._max_equity_per_instrument_cap(100_000.0) == pytest.approx(20_000.0)

    def test_returns_none_when_resolver_finds_no_expert(self, monkeypatch):
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        import ba2_common.core.instance_resolver as ir_mod

        class _EmptyResolver:
            def get_expert_instance(self, instance_id):
                return None

        monkeypatch.setattr(ir_mod, "get_instance_resolver", lambda: _EmptyResolver())
        assert action._max_equity_per_instrument_cap(100_000.0) is None

    def test_returns_none_when_resolver_raises(self, monkeypatch):
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        import ba2_common.core.instance_resolver as ir_mod

        class _BoomResolver:
            def get_expert_instance(self, instance_id):
                raise RuntimeError("resolver unavailable")

        monkeypatch.setattr(ir_mod, "get_instance_resolver", lambda: _BoomResolver())
        assert action._max_equity_per_instrument_cap(100_000.0) is None

    def test_returns_none_when_no_expert_recommendation(self):
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        action.expert_recommendation = None
        assert action._max_equity_per_instrument_cap(100_000.0) is None


class TestSizeCappedByMaxEquityPerInstrument:
    def test_cap_tighter_than_option_sizing_wins(self, monkeypatch):
        """$100k account, option_sizing=20% -> $20k budget for a $50 premium ($5,000/contract)
        would normally buy 4 contracts. A 5% max_virtual_equity_per_instrument_percent cap
        ($5,000) must win instead -> only 1 contract."""
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        _patch_resolver(monkeypatch, {"max_virtual_equity_per_instrument_percent": 5.0})
        assert action._size(premium=50.0, sizing_pct=20.0) == 1

    def test_option_sizing_tighter_than_cap_wins(self, monkeypatch):
        """The reverse: a generous 50% cap must not loosen a tighter 5% option_sizing budget."""
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        _patch_resolver(monkeypatch, {"max_virtual_equity_per_instrument_percent": 50.0})
        # 5% of 100k = $5,000 budget / ($50*100=$5,000 per contract) = 1 contract.
        assert action._size(premium=50.0, sizing_pct=5.0) == 1

    def test_expensive_contract_rejected_by_cap_even_if_option_sizing_would_allow_it(self, monkeypatch):
        """The motivating example: a $100 premium ($10,000/contract) against a generous 80%
        option_sizing budget ($80,000, i.e. affordable) must still be rejected once a 5% cap
        ($5,000) makes even ONE contract unaffordable."""
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        _patch_resolver(monkeypatch, {"max_virtual_equity_per_instrument_percent": 5.0})
        assert action._size(premium=100.0, sizing_pct=80.0) == 0

    def test_no_cap_setting_falls_back_to_option_sizing_only(self, monkeypatch):
        """Unset setting -> _max_equity_per_instrument_cap returns None -> unchanged
        pre-existing behavior (option_sizing alone governs)."""
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        _patch_resolver(monkeypatch, {})
        assert action._size(premium=50.0, sizing_pct=20.0) == 4  # 20k / 5k = 4, uncapped

    def test_resolver_failure_falls_back_to_option_sizing_only(self, monkeypatch):
        """A cap-resolution hiccup must not block an otherwise-valid, already-approved entry."""
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        import ba2_common.core.instance_resolver as ir_mod

        class _BoomResolver:
            def get_expert_instance(self, instance_id):
                raise RuntimeError("boom")

        monkeypatch.setattr(ir_mod, "get_instance_resolver", lambda: _BoomResolver())
        assert action._size(premium=50.0, sizing_pct=20.0) == 4


class TestSizeByReserveCappedByMaxEquityPerInstrument:
    def test_cap_tighter_than_option_sizing_wins(self, monkeypatch):
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        _patch_resolver(monkeypatch, {"max_virtual_equity_per_instrument_percent": 5.0})
        # 20% sizing -> $20k budget / $2,000 reserve = 10 contracts, but 5% cap ($5,000) -> 2.
        assert action._size_by_reserve(reserve_per_contract=2_000.0, sizing_pct=20.0) == 2

    def test_no_cap_setting_falls_back_to_option_sizing_only(self, monkeypatch):
        action, _id = _make_action(balance=100_000.0, instance_id=1)
        _patch_resolver(monkeypatch, {})
        assert action._size_by_reserve(reserve_per_contract=2_000.0, sizing_pct=20.0) == 10
