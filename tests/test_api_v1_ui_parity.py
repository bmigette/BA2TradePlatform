"""One function, two callers: the Live Trades page and the management API make the SAME broker call sequence.

The page methods are driven directly (NiceGUI's ``ui.notify`` is replaced by a recorder); the API side calls the
service entry the routes call. Both run against the same call-recording fake account."""
import asyncio

import pytest

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import Transaction
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus
from ba2_trade_platform.services import trades as svc
from ba2_trade_platform.ui.pages import live_trades
from tests.api_write_fakes import RecordingAccount, install_account
from tests.factories import create_account_definition, create_trading_order, create_transaction

pytestmark = pytest.mark.api_db


class _Dialog:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def page(monkeypatch):
    notes = []
    monkeypatch.setattr(live_trades.ui, "notify", lambda message, **kw: notes.append((message, kw.get("type"))))
    tab = live_trades.LiveTradesTab.__new__(live_trades.LiveTradesTab)
    tab._refresh_transactions = lambda: None
    tab.notes = notes
    return tab


def _txn(tp=120.0, sl=90.0, **kw):
    a = create_account_definition()
    t = create_transaction(symbol="AAPL", status=TransactionStatus.OPENED, open_price=100.0, take_profit=tp,
                           stop_loss=sl, quantity=10.0, **kw)
    create_trading_order(a.id, symbol="AAPL", transaction_id=t.id, status=OrderStatus.FILLED)
    return t


def _both_callers(monkeypatch, page, *, ui_prices, api_kwargs, **txn_kw):
    """Run the page and the API on two identical transactions; return (ui_calls, api_calls, ui_notes, api_result)."""
    ui_acct, api_acct = RecordingAccount(), RecordingAccount()
    t_ui, t_api = _txn(**txn_kw), _txn(**txn_kw)
    install_account(monkeypatch, ui_acct)
    page._update_position(t_ui.id, t_ui.quantity, ui_prices[0], ui_prices[1], _Dialog())
    install_account(monkeypatch, api_acct)
    result = svc.set_tp_sl(t_api.id, dry_run=False, **api_kwargs)
    norm = lambda calls, tid: [(c[0], "TXN" if c[1] == tid else c[1]) + c[2:] for c in calls]
    return norm(ui_acct.calls, t_ui.id), norm(api_acct.calls, t_api.id), page.notes, result


@pytest.mark.parametrize("ui_prices,api_kwargs,expected", [
    ((130.0, 80.0), dict(take_profit=130.0, stop_loss=80.0), "adjust_tp_sl"),
    ((130.0, 90.0), dict(take_profit=130.0, stop_loss=None), "adjust_tp"),
    ((120.0, 80.0), dict(take_profit=None, stop_loss=80.0), "adjust_sl"),
])
def test_tp_sl_edit_makes_the_same_broker_call_with_the_lock(monkeypatch, page, ui_prices, api_kwargs, expected):
    ui_calls, api_calls, notes, result = _both_callers(
        monkeypatch, page, ui_prices=ui_prices, api_kwargs=dict(api_kwargs, lock=True))
    assert ui_calls == api_calls and ui_calls[0][0] == expected
    assert ui_calls[0][-1] == "manual"                       # the dialog's source: the account sets the lock
    assert notes[-1][0] == result["message"] and notes[-1][1] == "positive"


def test_without_lock_the_only_difference_is_the_source(monkeypatch, page):
    ui_calls, api_calls, _, _ = _both_callers(monkeypatch, page, ui_prices=(130.0, 90.0),
                                              api_kwargs=dict(take_profit=130.0, stop_loss=None, lock=False))
    assert [c[:-1] for c in ui_calls] == [c[:-1] for c in api_calls]
    assert ui_calls[0][-1] == "manual" and api_calls[0][-1] == "api"


def test_the_page_still_refuses_a_swapped_pair_before_any_call(monkeypatch, page):
    acct = RecordingAccount()
    install_account(monkeypatch, acct)
    t = _txn()
    page._update_position(t.id, t.quantity, 90.0, 120.0, _Dialog())
    assert acct.calls == [] and page.notes[-1][1] == "negative" and "swapped" in page.notes[-1][0]


def test_the_page_removal_path_cancels_legs_like_the_service(monkeypatch, page):
    acct = RecordingAccount()
    install_account(monkeypatch, acct)
    t = _txn()
    a = create_account_definition()
    leg = create_trading_order(a.id, transaction_id=t.id, order_type=OrderType.OCO, status=OrderStatus.OPEN,
                               broker_order_id="b-1")
    page._update_position(t.id, t.quantity, None, None, _Dialog())
    assert acct.calls == [("cancel_order", leg.id)] and page.notes[-1] == ("TP/SL removed", "positive")
    stored = get_instance(Transaction, t.id)
    assert stored.take_profit is None and stored.stop_loss is None


def test_revert_lock_page_and_api_clear_the_same_flags(monkeypatch, page):
    t_ui = _txn(tp_manual_override=True, sl_manual_override=True)
    t_api = _txn(tp_manual_override=True, sl_manual_override=True)
    dialog = _Dialog()
    page._revert_tpsl_manual_override(t_ui.id, dialog)
    out = svc.unlock_tp_sl(t_api.id, dry_run=False)
    for tid in (t_ui.id, t_api.id):
        stored = get_instance(Transaction, tid)
        assert not stored.tp_manual_override and not stored.sl_manual_override
    assert out["cleared"] == ["TP", "SL"] and dialog.closed
    assert page.notes[-1][0].startswith("TP and SL lock cleared")
    page._revert_tpsl_manual_override(t_ui.id, _Dialog())
    assert page.notes[-1] == ("TP/SL are not currently manually overridden", "info")
