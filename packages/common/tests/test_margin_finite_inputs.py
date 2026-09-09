"""A broker figure that is NaN or infinite is UNKNOWN, and unknown must refuse.

2026-09-09 review, finding 5: ``_stock_multiplier_from`` rejected None/0/negative but
let ``float('nan')`` through, and NaN then survives ``effective_factor_for``'s
``min(factor, max(multiplier, 1.0))`` untouched (every NaN comparison is False), so
the CONFIGURED factor came back as if the broker had blessed it. A $10,000 account
with factor 1.8 and a NaN broker multiplier reported an $18,000 tradable balance --
leverage granted by a missing number. The stored ``margin_factor`` already had a
finite check (``margin_factor_error``); the broker-published figures did not.

A measured 0.0 is NOT unknown and stays a legal answer: an account really can have
zero buying power or zero balance, and that is a refusal to size, not a refusal to
answer. Only non-finite is unknown here.

The fixture is the ``_Stub`` of test_margin_tradable_balance.py (same shape, same
stubbed abstracts): no DB and no broker, every read served from a hand-built
AccountSnapshot.
"""
import math

import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import ReadOnlyAccountInterface


class _Stub(ReadOnlyAccountInterface):
    """Concrete ReadOnlyAccountInterface over one canned snapshot and balance."""

    def __init__(self, *, balance, snapshot, settings):
        self.id = 3
        self._balance = balance
        self._snap = snapshot
        self._stored = settings
        self.snapshot_calls = 0

    @property
    def settings(self):
        return self._stored

    @classmethod
    def get_settings_definitions(cls):
        return {}

    def get_account_snapshot(self):
        self.snapshot_calls += 1
        return self._snap

    def get_balance(self):
        return self._balance

    def get_account_info(self):
        return {}

    def get_positions(self):
        return []

    def get_balance_history(self, start_date=None, end_date=None):
        return []

    # Remaining abstract methods, stubbed exactly as test_margin_tradable_balance does.
    def get_orders(self, status=None):
        return []

    def get_order(self, order_id):
        return None

    def symbols_exist(self, symbols):
        return {s: True for s in symbols}

    def _get_instrument_current_price_impl(self, symbol_or_symbols, price_type='bid'):
        return None

    def refresh_positions(self):
        return True

    def refresh_orders(self):
        return True

    def get_dividends(self, symbol=None, start_date=None, end_date=None):
        return []

    def get_filled_trades(self, symbol=None, start_date=None, end_date=None):
        return []


ON = {"margin_enabled": True, "margin_factor": 1.8}
OFF = {"margin_enabled": False, "margin_factor": 1.8}
NAN = float("nan")
INF = float("inf")


# ----- the broker's stock multiplier ---------------------------------------

def test_nan_multiplier_is_refused():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=NAN,
                                                            buying_power=20_000.0),
                 settings=ON)
    with pytest.raises(ValueError, match="finite"):
        acct._stock_multiplier_from(acct.get_account_snapshot())


def test_infinite_multiplier_is_refused():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=INF,
                                                            buying_power=20_000.0),
                 settings=ON)
    with pytest.raises(ValueError, match="finite"):
        acct._stock_multiplier_from(acct.get_account_snapshot())


# ----- the broker's buying power -------------------------------------------

def test_nan_buying_power_is_refused():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0,
                                                            buying_power=NAN),
                 settings=ON)
    with pytest.raises(ValueError, match="finite"):
        acct._buying_power_from(acct.get_account_snapshot())


def test_infinite_buying_power_is_refused():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0,
                                                            buying_power=INF),
                 settings=ON)
    with pytest.raises(ValueError, match="finite"):
        acct._buying_power_from(acct.get_account_snapshot())


def test_zero_buying_power_is_a_measured_zero_not_unknown():
    """A fully-deployed account really has $0 left. That is an answer, not a gap."""
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0,
                                                            buying_power=0.0),
                 settings=ON)
    assert acct._buying_power_from(acct.get_account_snapshot()) == 0.0


# ----- the account balance --------------------------------------------------

def test_nan_balance_is_refused():
    acct = _Stub(balance=NAN, snapshot=AccountSnapshot(margin_multiplier=2.0,
                                                       buying_power=20_000.0),
                 settings=ON)
    with pytest.raises(ValueError, match="finite"):
        acct._plain_balance()


def test_zero_balance_is_a_measured_zero_not_unknown():
    acct = _Stub(balance=0.0, snapshot=AccountSnapshot(margin_multiplier=2.0,
                                                       buying_power=0.0),
                 settings=ON)
    assert acct._plain_balance() == 0.0


def test_margin_off_refuses_a_nan_balance_instead_of_returning_it():
    """With margin off the tradable balance IS the balance, so a NaN would be handed
    straight to the sizers as a dollar figure."""
    acct = _Stub(balance=NAN, snapshot=AccountSnapshot(margin_multiplier=1.0,
                                                       buying_power=10_000.0),
                 settings=OFF)
    with pytest.raises(ValueError, match="finite"):
        acct.get_tradable_balance()


def test_margin_off_still_returns_a_zero_balance():
    acct = _Stub(balance=0.0, snapshot=AccountSnapshot(margin_multiplier=1.0,
                                                       buying_power=0.0),
                 settings=OFF)
    assert acct.get_tradable_balance() == 0.0


def test_option_tradable_balance_refuses_a_nan_balance():
    acct = _Stub(balance=NAN, snapshot=AccountSnapshot(margin_multiplier=2.0,
                                                       buying_power=20_000.0,
                                                       option_buying_power=5_000.0),
                 settings=ON)
    with pytest.raises(ValueError, match="finite"):
        acct.get_option_tradable_balance()


# ----- the review's reproduction --------------------------------------------

def test_nan_multiplier_cannot_grant_the_configured_factor():
    """The review's $18,000: balance 10k, factor 1.8, broker multiplier NaN.

    ``min(1.8, max(nan, 1.0))`` is 1.8 -- NaN loses every comparison -- so the
    unpublished multiplier used to read as full permission to lever.
    """
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=NAN,
                                                            buying_power=20_000.0),
                 settings=ON)
    with pytest.raises(ValueError, match="finite"):
        acct.get_tradable_balance()


def test_nan_multiplier_is_refused_by_the_scaling_factor_too():
    """``effective_margin_factor`` is the same read for callers that SCALE a figure
    they already hold; it must refuse where the ceiling refuses."""
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=NAN,
                                                            buying_power=20_000.0),
                 settings=ON)
    with pytest.raises(ValueError, match="finite"):
        acct.effective_margin_factor()


def test_nan_buying_power_refuses_the_tradable_balance():
    """Buying power only feeds the over-exposure WARNING, whose ``<`` a NaN would
    silently lose -- the check would skip itself without saying so."""
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0,
                                                            buying_power=NAN),
                 settings=ON)
    with pytest.raises(ValueError, match="finite"):
        acct.get_tradable_balance()


def test_finite_figures_are_untouched():
    """The guard adds a refusal, it does not move any number that was already good."""
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0,
                                                            buying_power=20_000.0),
                 settings=ON)
    tradable = acct.get_tradable_balance()
    assert tradable == 18_000.0 and math.isfinite(tradable)
