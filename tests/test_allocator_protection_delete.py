"""Delete TP/SL from the dialog: cancel-only, config forgotten, history kept, refusals keep the config."""
from decimal import Decimal

import pytest
from sqlmodel import select

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from ba2_trade_platform.core.allocator_protection_models import (
    AllocatorProtection, AllocatorProtectionOrder, AllocatorWeightChange,
)
from ba2_trade_platform.core.db import add_instance, get_db
from ba2_trade_platform.core.portfolio_allocation_service import _submission_lock
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity
from tests.test_allocator_protection_round3 import _slices, activity  # noqa: F401
from tests.test_allocator_protection_ui import nicegui_client  # noqa: F401

T = ap.TpTarget


@pytest.fixture
def broker():
    b = FakeTastyBroker()
    b.positions["ABC"] = Decimal(10)
    b.prices["ABC"] = 50.0
    return b


@pytest.fixture
def acct(broker):
    with patch_equity(broker):
        yield make_account(broker)


def _count(model):
    with get_db() as session:
        return len(session.exec(select(model)).all())


def test_delete_cancels_forgets_the_config_and_never_places_a_sell(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 0.5)]).ok
    placed = (len(broker.place_calls), len(broker.single_place_calls))
    add_instance(AllocatorWeightChange(account_id=1, label="L", symbol="ABC", reason="tp_fill",
                                       before_pct=10.0, after_pct=5.0, detail="TP1"))
    result = aps.delete_protection(acct, "ABC")
    assert result.ok
    assert aps.get_protection(1, "ABC") is None and _count(AllocatorProtectionOrder) == 0
    assert (len(broker.place_calls), len(broker.single_place_calls)) == placed        # cancel-only
    assert broker.delete_calls and broker.single_delete_calls
    assert _count(AllocatorWeightChange) == 1                                         # the audit trail stays
    assert broker.positions["ABC"] == Decimal(10)                                     # the position is untouched


def test_an_unconfirmed_cancel_keeps_the_config_and_raises_the_alert(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)]).ok
    broker.never_confirm_cancel = True
    result = aps.delete_protection(acct, "ABC")
    assert not result.ok
    p = aps.get_protection(1, "ABC")
    assert p is not None and p.alert_code == ap.CODE_CANCEL_UNCONFIRMED and _slices()


def test_delete_is_refused_while_a_run_is_in_flight(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)]).ok
    import threading
    lock = _submission_lock(1)
    held, release = threading.Event(), threading.Event()

    def hold():                       # a run in ANOTHER thread holds the submission lock
        with lock:
            held.set()
            release.wait(10)
    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(5)
    cancels = len(broker.delete_calls)
    try:
        result = aps.delete_protection(acct, "ABC")
    finally:
        release.set()
        t.join()
    assert not result.ok and "run is in flight" in result.message
    assert aps.get_protection(1, "ABC") is not None and len(broker.delete_calls) == cancels


def test_an_unresolved_unknown_slice_refuses_the_delete(acct, broker):
    broker.single_raise_on_place = TimeoutError("connect timeout")
    aps.save_protection(acct, "ABC", 45.0, [])
    broker.single_raise_on_place = None
    result = aps.delete_protection(acct, "ABC")
    assert not result.ok and aps.get_protection(1, "ABC") is not None


def test_a_switched_off_config_can_be_deleted_too(acct, broker):
    assert aps.save_protection(acct, "ABC", 45.0, [T(60.0, 1.0)]).ok
    assert aps.disable_protection(acct, "ABC").ok
    assert aps.delete_protection(acct, "ABC").ok and aps.get_protection(1, "ABC") is None


def _dialog(client, protection):
    from tests.test_allocator_protection_ui import _data, _marked, _noop
    from ba2_trade_platform.ui.pages import allocator_protection_dialog as dlg
    with client:
        dlg._build_dialog(1, _data(quantity=10.0, protection=protection), _noop)
    return dlg, _marked(client, dlg.MARKER_DELETE)


def test_the_dialog_offers_delete_when_a_config_exists_enabled_or_not(nicegui_client):
    on = AllocatorProtection(account_id=1, symbol="ABC", enabled=True, sl_price=45.0, tp_targets=[])
    dlg, found = _dialog(nicegui_client, on)
    assert len(found) == 1 and "negative" in found[0]._props.get("color", "") + str(found[0]._props)
    off = AllocatorProtection(account_id=1, symbol="ABC", enabled=False, sl_price=45.0, tp_targets=[])
    assert len(_dialog(nicegui_client, off)[1]) == 2                    # one more than before: this dialog's own


def test_the_dialog_has_no_delete_without_a_config(nicegui_client):
    assert _dialog(nicegui_client, None)[1] == []
