"""Tradable balance = balance x min(margin_factor, broker multiplier) with margin on;
balance with it off. Plus the over-exposure warning threshold.

Worked example the operator gave: balance 10k, broker multiplier 2 (20k gross
capacity), factor 1.8 (platform deploys at most 18k). Once the broker's REMAINING
buying power drops under 20k - 18k = 2k, gross exposure has passed the platform's
own ceiling -- something outside the experts (allocator, manual trade) consumed
it -- and that is the WARNING.

The log pins use the ``records`` fixture below rather than ``caplog``; see its
docstring for why caplog cannot work against this package's logger.
"""
import logging

import pytest

from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.interfaces.ReadOnlyAccountInterface import (
    ReadOnlyAccountInterface, tradable_balance_for, over_exposure_threshold,
)


# ----- pure math -----------------------------------------------------------

def test_margin_off_is_the_balance():
    assert tradable_balance_for(10_000.0, margin_enabled=False, factor=1.8, multiplier=2.0) == 10_000.0


def test_margin_on_is_balance_times_factor():
    assert tradable_balance_for(10_000.0, margin_enabled=True, factor=1.8, multiplier=2.0) == 18_000.0


def test_broker_multiplier_below_factor_wins():
    assert tradable_balance_for(10_000.0, margin_enabled=True, factor=1.8, multiplier=1.5) == 15_000.0


def test_non_marginable_account_is_the_balance():
    assert tradable_balance_for(10_000.0, margin_enabled=True, factor=1.8, multiplier=1.0) == 10_000.0


def test_threshold_is_balance_times_multiplier_minus_factor():
    assert over_exposure_threshold(10_000.0, multiplier=2.0, factor=1.8) == pytest.approx(2_000.0)


def test_threshold_is_negative_when_factor_exceeds_multiplier():
    # then no remaining-BP figure can ever be below it: the warning cannot fire
    assert over_exposure_threshold(10_000.0, multiplier=1.5, factor=1.8) < 0


# ----- the account methods --------------------------------------------------

class _Stub(ReadOnlyAccountInterface):
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

    # Remaining abstract methods, stubbed exactly as test_margin_accessors._Stub does.
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


@pytest.fixture
def records(monkeypatch):
    """``(levelno, message)`` for every line ReadOnlyAccountInterface logs.

    NOT ``caplog``. ``ba2_common``'s logger sets ``propagate = False``
    (packages/common/ba2_common/logger.py:19) so pytest's ROOT handler never sees a
    record: every log assertion in this file would pass vacuously against caplog
    while the operator's log stayed empty -- the exact failure these pins exist to
    stop. Patching the module-under-test's own ``logger`` is the established idiom
    here (test_account_seams._capture_errors, test_covered_call_decline_reasons).
    """
    import sys

    # sys.modules, not a `from ... import`: the interfaces package re-exports the
    # CLASS under this name, and the class has no `.logger`.
    module = sys.modules["ba2_common.core.interfaces.ReadOnlyAccountInterface"]
    seen = []
    for name, level in (("debug", logging.DEBUG), ("info", logging.INFO),
                        ("warning", logging.WARNING), ("error", logging.ERROR)):
        monkeypatch.setattr(
            module.logger, name,
            lambda msg, *a, _lvl=level, **k: seen.append((_lvl, str(msg))))
    return seen


ON = {"margin_enabled": True, "margin_factor": 1.8}
OFF = {"margin_enabled": False, "margin_factor": 1.8}
UNSET = {"margin_enabled": None, "margin_factor": None}   # never saved: defaults apply
LEVERED = AccountSnapshot(margin_multiplier=2.0, buying_power=20_000.0)


def test_off_returns_balance_and_never_touches_the_snapshot():
    acct = _Stub(balance=10_000.0, snapshot=LEVERED, settings=OFF)
    assert acct.get_tradable_balance() == 10_000.0
    assert acct.get_option_tradable_balance() == 10_000.0
    assert acct.snapshot_calls == 0


def test_on_reads_the_snapshot_exactly_once():
    """TastyTrade's snapshot is an uncached REST call: one read per tradable balance,
    so multiplier and buying power come from the SAME broker instant."""
    acct = _Stub(balance=10_000.0, snapshot=LEVERED, settings=ON)
    acct.get_tradable_balance()
    assert acct.snapshot_calls == 1


def test_unset_settings_read_as_off():
    acct = _Stub(balance=10_000.0, snapshot=LEVERED, settings=UNSET)
    assert acct.get_tradable_balance() == 10_000.0


def test_on_returns_balance_times_factor():
    acct = _Stub(balance=10_000.0, snapshot=LEVERED, settings=ON)
    assert acct.get_tradable_balance() == 18_000.0


def test_on_option_side_uses_the_option_multiplier_default_one():
    acct = _Stub(balance=10_000.0, snapshot=LEVERED, settings=ON)
    assert acct.get_option_tradable_balance() == 10_000.0


def test_string_true_from_the_settings_table_reads_as_on():
    # the deploy-parity trap: bool settings can come back as "1"/"true"
    acct = _Stub(balance=10_000.0, snapshot=LEVERED, settings={"margin_enabled": "true", "margin_factor": "1.8"})
    assert acct.get_tradable_balance() == 18_000.0


def test_on_with_cash_account_returns_balance_and_warns(records):
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=1.0, buying_power=4_000.0), settings=ON)
    assert acct.get_tradable_balance() == 10_000.0
    assert any(lvl == logging.WARNING and "non-marginable" in msg and "Account 3" in msg
               for lvl, msg in records)


def test_on_raises_when_balance_unknown():
    acct = _Stub(balance=None, snapshot=LEVERED, settings=ON)
    with pytest.raises(ValueError, match="balance"):
        acct.get_tradable_balance()


def test_on_raises_when_multiplier_unknown():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(buying_power=1.0), settings=ON)
    with pytest.raises(ValueError, match="multiplier"):
        acct.get_tradable_balance()


def test_on_raises_when_buying_power_unknown():
    acct = _Stub(balance=10_000.0, snapshot=AccountSnapshot(margin_multiplier=2.0), settings=ON)
    with pytest.raises(ValueError, match="buying power"):
        acct.get_tradable_balance()


def test_on_raises_on_a_bad_factor():
    acct = _Stub(balance=10_000.0, snapshot=LEVERED, settings={"margin_enabled": True, "margin_factor": 0.5})
    with pytest.raises(ValueError, match="margin_factor"):
        acct.get_tradable_balance()


def test_over_exposure_warns_below_threshold_and_not_at_it(records):
    snap_at = AccountSnapshot(margin_multiplier=2.0, buying_power=2_000.0)     # exactly 20k-18k
    snap_below = AccountSnapshot(margin_multiplier=2.0, buying_power=1_999.0)
    _Stub(balance=10_000.0, snapshot=snap_at, settings=ON).get_tradable_balance()
    assert not any("past the margin ceiling" in msg for _, msg in records)
    records.clear()
    _Stub(balance=10_000.0, snapshot=snap_below, settings=ON).get_tradable_balance()
    hits = [msg for lvl, msg in records
            if lvl == logging.WARNING and "past the margin ceiling" in msg]
    assert len(hits) == 1 and "1,999.00" in hits[0] and "2,000.00" in hits[0]


def test_option_over_exposure_is_skipped_with_a_debug_line_when_option_bp_unknown(records):
    class _Levered(_Stub):
        def get_option_margin_multiplier(self):
            return 2.0
    acct = _Levered(balance=10_000.0, snapshot=LEVERED, settings=ON)   # option_buying_power None
    assert acct.get_option_tradable_balance() == 18_000.0
    assert not any(lvl >= logging.WARNING for lvl, _ in records)
    assert any(lvl == logging.DEBUG and "option buying power" in msg for lvl, msg in records)
