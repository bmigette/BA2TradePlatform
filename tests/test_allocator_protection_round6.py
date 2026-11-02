"""Allocator TP/SL UI polish: the broker's margin check on stops (A11), the dialog's check, the
margin-refusal wording and the partial-placement repair. No broker is contacted (fake complex-order API)."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ba2_trade_platform.core import allocator_protection as ap
from ba2_trade_platform.core import allocator_protection_service as aps
from tests.allocator_protection_fakes import FakeTastyBroker, make_account, patch_equity
from tests.test_allocator_protection_round3 import _slices, activity  # noqa: F401

T = ap.TpTarget


@pytest.fixture
def broker():
    b = FakeTastyBroker()
    b.positions["ABC"] = Decimal(2)
    b.prices["ABC"] = 287.0
    b.bp_p0 = 215.0                      # the live finding: change = q x (stop - 215)
    b.available_bp = 70.0
    return b


@pytest.fixture
def acct(broker):
    with patch_equity(broker):
        a = make_account(broker)
        a.get_account_snapshot = lambda: SimpleNamespace(buying_power=broker.available_bp)
        yield a


# ---------------------------------------------------------------- the dry run
def test_a_dry_run_reports_the_buying_power_effect_and_places_nothing(acct, broker):
    verdict = acct.dry_run_protective("ABC", 1, sl_price=250.0)
    assert verdict.ok and verdict.bp_change == pytest.approx(35.0)
    assert all(dry for dry, _ in broker.single_place_calls) and broker.singles == {}


def test_a_dry_run_names_a_margin_refusal(acct, broker):
    verdict = acct.dry_run_protective("ABC", 2, sl_price=100.0)
    assert not verdict.ok and verdict.margin_failed and "margin_check_failed" in verdict.message
    assert verdict.bp_change is None


def test_a_dry_run_of_an_oco_uses_the_stop_leg(acct, broker):
    verdict = acct.dry_run_protective("ABC", 1, sl_price=200.0, tp_price=320.0)
    assert verdict.ok and verdict.bp_change == pytest.approx(-15.0)
    assert all(dry for dry, _ in broker.place_calls)


# ---------------------------------------------------------------- the check and the suggestion
def test_the_check_warns_before_save_with_a_suggested_stop(acct, broker):
    report = aps.check_with_broker(acct, "ABC", 100.0, [])
    assert not report.all_ok and report.available == 70.0
    assert report.needed > report.available                                  # "needs ~$X, available $Y"
    assert 178.0 <= report.suggested_stop <= 186.0                           # exact answer 180 (2 x (S-215) >= -70)
    assert "far below the market" in report.warning and "$70.00" in report.warning
    assert f"{report.suggested_stop:,.2f}" in report.warning and "approx" in report.warning.lower()


def test_the_check_is_quiet_when_the_stop_is_affordable(acct, broker):
    report = aps.check_with_broker(acct, "ABC", 250.0, [])
    assert report.all_ok and report.warning == "" and report.suggested_stop is None


def test_the_check_is_read_only(acct, broker):
    aps.check_with_broker(acct, "ABC", 100.0, [T(320.0, 0.5)])
    assert all(dry for dry, _ in broker.place_calls + broker.single_place_calls)
    assert broker.singles == {} and broker.complex == {}


def test_the_check_lists_one_verdict_per_slice(acct, broker):
    report = aps.check_with_broker(acct, "ABC", 250.0, [T(320.0, 0.5)])
    assert [(s.kind, s.quantity) for s in report.slices] == [("OCO", 1), ("STOP", 1)]
    assert all(s.ok and s.bp_change is not None for s in report.slices)


# ---------------------------------------------------------------- the refusal wording
def test_a_margin_refusal_says_so_and_keeps_the_raw_text(acct, broker):
    result = aps.save_protection(acct, "ABC", 100.0, [])
    assert not result.ok
    p = aps.get_protection(1, "ABC")
    assert p.alert_message.startswith("This stop is far below the market")
    assert "margin_check_failed" in p.alert_message                          # the raw text stays in the details


# ---------------------------------------------------------------- partial placement
def _partial(acct, broker):
    broker.available_bp = 120.0
    result = aps.save_protection(acct, "ABC", 150.0, [T(300.0, 0.5)])        # an OCO (1 sh) and a stop-only (1 sh)
    return result


def test_a_partial_placement_keeps_what_is_placed_and_says_what_is_bare(acct, broker):
    result = _partial(acct, broker)
    assert not result.ok
    live = [s for s in _slices() if s.state == "LIVE"]
    assert sum(s.quantity for s in live) == 1
    p = aps.get_protection(1, "ABC")
    assert "1 of 2" in p.alert_message and "1 share" in p.alert_message and "no stop" in p.alert_message
    assert p.sl_price == 150.0                                               # never changed silently


def test_use_a_stop_the_broker_accepts_is_an_explicit_replan(acct, broker):
    _partial(acct, broker)
    result = aps.change_stop_and_replace(acct, "ABC", 200.0)
    assert result.ok, result
    p = aps.get_protection(1, "ABC")
    assert p.sl_price == 200.0
    assert sum(s.quantity for s in _slices() if s.state == "LIVE") == 2


def test_changing_the_stop_refuses_a_stop_above_the_market(acct, broker):
    _partial(acct, broker)
    result = aps.change_stop_and_replace(acct, "ABC", 300.0)
    assert not result.ok and aps.get_protection(1, "ABC").sl_price == 150.0


# ---------------------------------------------------------------- the dialog
from tests.test_allocator_protection_ui import nicegui_client  # noqa: E402,F401


def test_the_dialog_has_a_check_with_broker_button_and_never_calls_the_broker_on_load(nicegui_client, monkeypatch):
    from tests.test_allocator_protection_ui import _data, _find, _noop
    from ba2_trade_platform.ui.pages import allocator_protection_dialog as dlg
    monkeypatch.setattr(aps, "check_with_broker", lambda *a, **k: pytest.fail("the broker was asked on load"))
    with nicegui_client:
        dlg._build_dialog(1, _data(quantity=10.0), _noop)
    assert _find(nicegui_client, dlg.MARKER_CHECK)


def test_a_margin_alert_offers_the_explicit_stop_the_broker_accepts(nicegui_client):
    from ba2_trade_platform.core.allocator_protection_models import AllocatorProtection
    from tests.test_allocator_protection_ui import _data, _find, _noop
    from ba2_trade_platform.ui.pages import allocator_protection_dialog as dlg
    p = AllocatorProtection(account_id=1, symbol="ABC", enabled=True, sl_price=100.0, tp_targets=[],
                            alert_code=ap.CODE_PLACEMENT_REFUSED,
                            alert_message=ap.margin_sentence(available=70.0) + " Details: margin_check_failed")
    with nicegui_client:
        dlg._build_dialog(1, _data(quantity=2.0, protection=p, price=287.0), _noop)
    assert _find(nicegui_client, dlg.MARKER_USE_ACCEPTED)


# ---------------------------------------------------------------- the label header's segmented badge
def _row(amount=None, total=None, excluded=False):
    return SimpleNamespace(excluded=excluded, pnl=SimpleNamespace(amount=amount, total_amount=total))


def test_the_segments_count_total_profit_loss_and_excluded():
    from ba2_trade_platform.ui.utils import allocator_protection_view as view
    rows = [_row(5.0), _row(1.0), _row(-2.0), _row(-1.0), _row(None), _row(0.0),
            _row(3.0, excluded=True)]
    seg = view.label_badge_segments(rows, excluded_value=69.4)
    assert [(s["key"], s["count"]) for s in seg] == [("total", 7), ("profit", 2), ("loss", 2), ("excluded", 1)]
    assert [s["color"] for s in seg] == ["grey-7", "green-8", "red-8", "orange-8"]
    assert seg[3]["tooltip"] == "1 excluded: $69"


def test_dividends_decide_the_sign_when_there_is_dividend_cash():
    from ba2_trade_platform.ui.utils import allocator_protection_view as view
    seg = view.label_badge_segments([_row(-1.0, total=4.0), _row(2.0, total=-1.0)])
    assert [(s["key"], s["count"]) for s in seg] == [("total", 2), ("profit", 1), ("loss", 1)]


def test_zero_segments_are_omitted_except_the_total():
    from ba2_trade_platform.ui.utils import allocator_protection_view as view
    assert [s["key"] for s in view.label_badge_segments([])] == ["total"]
    assert [s["key"] for s in view.label_badge_segments([_row(1.0)])] == ["total", "profit"]


def test_the_icon_helpers_are_total_over_every_status():
    from ba2_trade_platform.ui.utils import allocator_protection_view as view
    for code in ("OFF", "NO_POSITION", "PROTECTED", "PARTIAL", "REPLACING", "UNPROTECTED", "WHATEVER"):
        assert view.protection_icon_color(code) in view.ICON_COLORS
