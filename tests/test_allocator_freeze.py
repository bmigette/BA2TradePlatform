"""The allocator FREEZE (live side): store, solve wiring, boundary guarantee, label maths on the page
and the UI controls. The pure maths is in ``packages/common/tests/test_allocator_freeze_engine.py``."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlmodel import select

from ba2_trade_platform.core import allocator_freeze as afz
from ba2_trade_platform.core import portfolio_allocation_service as svc
from ba2_trade_platform.core.allocator_protection_models import AllocatorFrozen
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.portfolio_allocation import (
    ALLOCATION_MODE_REBALANCE, VALUATION_MODE_MARKET, AllocationPlan,
)
from ba2_trade_platform.core.portfolio_allocation_store import set_managed_label, set_symbol_weight
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.core.utils import add_label_to_instruments
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.utils import allocator_protection_view as view
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    ManagedLabel, build_label_views, positions_by_symbol,
)
from tests.test_portfolio_allocation_page import (  # noqa: F401 -- fixtures + doubles reused
    _AllocAccount, _held, _use_account, account_id, run_to_thread_inline,
)
from tests.test_portfolio_allocation_submit import FakeAccount, make_base, make_row


@pytest.fixture(autouse=True)
def _silence_activity(monkeypatch):
    monkeypatch.setattr(svc, "log_activity", lambda *a, **k: None)


# ============================================================================ the store

def test_freeze_and_unfreeze_round_trip_persists(account_id):
    assert afz.get_frozen(account_id) == {}
    row = afz.freeze_symbol(account_id, "vst", note="waiting for earnings")
    assert (row.symbol, row.note) == ("VST", "waiting for earnings")
    got = afz.get_frozen(account_id)           # a fresh read from the DB
    assert set(got) == {"VST"} and got["VST"].since is not None
    assert afz.frozen_symbols(account_id) == {"VST"}
    assert afz.unfreeze_symbol(account_id, "VST") is True
    assert afz.get_frozen(account_id) == {}
    assert afz.unfreeze_symbol(account_id, "VST") is False


def test_freezing_twice_keeps_one_row_and_the_original_since(account_id):
    first = afz.freeze_symbol(account_id, "VST")
    second = afz.freeze_symbol(account_id, "vst", note="later")
    assert second.since == first.since and second.note == "later"
    with get_db() as session:
        assert len(session.exec(select(AllocatorFrozen)).all()) == 1


def test_freezes_are_per_account_and_a_blank_symbol_is_refused(account_id):
    afz.freeze_symbol(account_id, "VST")
    assert afz.get_frozen(account_id + 1) == {}
    with pytest.raises(ValueError):
        afz.freeze_symbol(account_id, " ")


def test_freezing_is_independent_of_exclusion(account_id):
    from ba2_trade_platform.core import allocator_exclusion as aex
    afz.freeze_symbol(account_id, "VST")
    assert aex.get_exclusions(account_id) == {}


# ============================================================================ the solve path

def _setup_label(account_id):
    set_managed_label(account_id, "L", target_pct=100.0)
    add_label_to_instruments(["ABC", "XYZ", "VST"], "L")
    for sym, w in {"ABC": 50.0, "XYZ": 30.0, "VST": 20.0}.items():
        set_symbol_weight(account_id, "L", sym, weight_pct=w)


def _account(account_id, monkeypatch):
    # 4000 held + the broker's buying power; VST is worth 3000 (30 sh x 100)
    account = _AllocAccount(
        account_id, {"manual_trading_enabled": True},
        positions=[_held("ABC", 5, 250.0, 500.0), _held("XYZ", 5, 250.0, 500.0),
                   _held("VST", 30, 1500.0, 3000.0)],
        prices={"ABC": 100.0, "XYZ": 100.0, "VST": 100.0})
    _use_account(monkeypatch, account)
    return account


def _solve(account_id):
    _base, labels, *_ = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)
    return page._solve_plan(
        account_id, mode=ALLOCATION_MODE_REBALANCE, labels=labels, scope_label=None, amount=0.0,
        allow_fractional=True, valuation_mode=VALUATION_MODE_MARKET)


def test_solve_leaves_the_frozen_symbol_alone_and_shares_the_rest(monkeypatch, account_id):
    _setup_label(account_id)
    _account(account_id, monkeypatch)
    base, plan, _current, _hours = _solve(account_id)
    normal = {r.symbol: r for r in plan.rows}
    afz.freeze_symbol(account_id, "VST")
    base, plan, current, _hours = _solve(account_id)
    rows = {r.symbol: r for r in plan.rows}
    assert rows["VST"].frozen and rows["VST"].delta_quantity == 0.0 and rows["VST"].side is None
    assert rows["VST"] in plan.rows and "VST" in current                       # still in the plan and the base
    money = base.base_notional - base.available_buying_power * 0 - 3000.0      # investable minus frozen
    assert rows["ABC"].target_notional == pytest.approx(money * 50 / 80)
    assert rows["XYZ"].target_notional == pytest.approx(money * 30 / 80)
    assert plan.frozen_notional == pytest.approx(3000.0)
    assert normal["VST"].frozen is False
    afz.unfreeze_symbol(account_id, "VST")                                      # unfreeze restores
    _b, again, *_ = _solve(account_id)
    assert {r.symbol: r.target_notional for r in again.rows} == \
        {s: r.target_notional for s, r in normal.items()}


def test_the_stored_weights_are_never_rewritten_by_a_freeze(monkeypatch, account_id):
    _setup_label(account_id)
    _account(account_id, monkeypatch)
    afz.freeze_symbol(account_id, "VST")
    _base, labels, *_ = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)
    assert {st.symbol: st.weight_pct for st in labels[0].symbols} == {"ABC": 50.0, "XYZ": 30.0, "VST": 20.0}
    assert page.labels_for_persist(account_id, labels) is labels


# ============================================================================ the boundary guarantee

def _plan(rows):
    return AllocationPlan(rows=rows, available_buying_power=10_000.0)


def _row(symbol, delta, price=100.0):
    row = make_row(symbol, OrderDirection.SELL if delta < 0 else OrderDirection.BUY, delta,
                   abs(delta) * price, 0.0 if delta < 0 else abs(delta) * price, price=price)
    row.target_quantity = 50.0 + delta
    return row


def test_run_allocation_never_orders_a_frozen_symbol_even_from_a_stale_plan():
    account = FakeAccount(account_id=71)
    account.positions = []
    afz.freeze_symbol(71, "VST", note="earnings")
    result = svc.run_allocation(account, _plan([_row("VST", 5.0), _row("AAPL", 2.0, 160.0)]), {},
                                make_base(), mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert [s[0] for s in account.submitted] == ["AAPL"]
    outcomes = {o.symbol: o for o in result["outcomes"]}
    assert outcomes["VST"].status == svc.OUTCOME_SKIPPED
    assert "is frozen" in outcomes["VST"].message and "earnings" in outcomes["VST"].message


def test_a_sell_of_a_frozen_symbol_is_refused_too():
    account = FakeAccount(account_id=72)
    account.positions = []
    afz.freeze_symbol(72, "VST")
    result = svc.run_allocation(account, _plan([_row("VST", -5.0)]), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert account.submitted == [] and account.closed == []
    assert result["outcomes"][0].status == svc.OUTCOME_SKIPPED


def test_a_frozen_no_op_row_is_not_an_order_and_unfreezing_lets_the_next_run_trade_it():
    account = FakeAccount(account_id=73)
    account.positions = []
    afz.freeze_symbol(73, "AAPL")
    afz.unfreeze_symbol(73, "AAPL")
    svc.run_allocation(account, _plan([_row("AAPL", 2.0, 160.0)]), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert [s[0] for s in account.submitted] == ["AAPL"]


# ============================================================================ the label maths on the page

def _views(frozen=None, base=10_000.0, weights=None):
    book = [SimpleNamespace(symbol="ABC", qty=10.0, cost_basis=500.0, market_value=1000.0, side=None),
            SimpleNamespace(symbol="XYZ", qty=10.0, cost_basis=500.0, market_value=1000.0, side=None),
            SimpleNamespace(symbol="VST", qty=30.0, cost_basis=1500.0, market_value=3000.0, side=None)]
    return build_label_views(
        [ManagedLabel("L", 100.0)], {"L": ["ABC", "XYZ", "VST"]},
        positions_by_symbol(book), {"ABC": 100.0, "XYZ": 100.0, "VST": 100.0}, {},
        valuation_mode=VALUATION_MODE_MARKET, base_notional=base,
        symbol_weights={"L": weights or {"ABC": 50.0, "XYZ": 30.0, "VST": 20.0}}, frozen=frozen)


def _frozen(**kw):
    return {"VST": SimpleNamespace(since=datetime(2026, 11, 1), note=kw.get("note"))}


def test_nothing_frozen_the_view_is_what_it_always_was():
    rows = {r.symbol: r for r in _views()[0].rows}
    assert rows["ABC"].target_value == pytest.approx(5000.0)
    assert rows["ABC"].frozen is False and rows["ABC"].effective_weight_pct is None
    assert [r.target_value for r in _views(frozen={})[0].rows] == [r.target_value for r in _views()[0].rows]


def test_frozen_row_shows_its_current_value_and_the_rest_share_what_is_left():
    rows = {r.symbol: r for r in _views(frozen=_frozen())[0].rows}
    assert rows["VST"].frozen and rows["VST"].target_value == pytest.approx(3000.0)
    assert rows["ABC"].target_value == pytest.approx(7000.0 * 50 / 80)
    assert rows["XYZ"].target_value == pytest.approx(7000.0 * 30 / 80)
    # the displayed effective weights add up to 100% of the label
    assert sum(r.effective_weight_pct for r in rows.values()) == pytest.approx(100.0)
    assert rows["VST"].effective_weight_pct == pytest.approx(30.0)   # its CURRENT share, not its 20% weight
    assert rows["VST"].weight_pct == 20.0                            # the stored weight is untouched


def test_frozen_above_the_money_clamps_the_others_to_zero():
    rows = {r.symbol: r for r in _views(frozen=_frozen(), base=2_000.0)[0].rows}
    assert rows["ABC"].target_value == 0.0 and rows["XYZ"].target_value == 0.0
    assert all(r.target_value >= 0 for r in rows.values())


# ============================================================================ the UI

def test_freeze_fields_for_both_states():
    off = view.freeze_fields(None)
    assert off["frozen"] is False and off["frozen_badge"] == "" and off["frz_icon"] == "ac_unit"
    on = view.freeze_fields(SimpleNamespace(since=datetime(2026, 11, 1), note="x"))
    assert on["frozen"] is True and on["frozen_badge"] == "frozen"
    assert "since 2026-11-01" in on["frz_tip"] and "Click to unfreeze" in on["frz_tip"]


def test_the_row_template_has_the_freeze_button_and_badge_and_aria_labels():
    chips = page.SYMBOL_CHIPS_TEMPLATE
    assert "freezeToggle" in chips and ":icon=\"props.row.frz_icon\"" in chips
    assert "'Unfreeze' : 'Freeze'" in chips and "props.row.frozen_badge" in chips
    assert "pf-sym-card--frozen" in page.symbol_card_template()


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page("/test-pf-freeze"), request=None)
    yield client
    client.remove_elements(client.elements.values())


async def _noop():
    return None


def _texts(client):
    return [getattr(el, "text", "") or getattr(el, "_props", {}).get("label", "") or ""
            for el in client.layout.descendants()]


def test_freeze_dialog_says_what_freezing_does(nicegui_client):
    with nicegui_client:
        page._open_freeze_dialog(1, "vst", {}, _noop)
    texts = _texts(nicegui_client)
    assert "Freeze VST?" in texts and any("NO order" in t and "TP/SL" in t for t in texts)
    assert any("Note (optional)" in t for t in texts)


def test_unfreeze_dialog_asks_to_unfreeze(nicegui_client):
    with nicegui_client:
        page._open_freeze_dialog(1, "VST", {"VST": object()}, _noop)
    texts = _texts(nicegui_client)
    assert "Unfreeze VST?" in texts and not any("Note (optional)" in t for t in texts)


def test_the_table_is_wired_to_the_freeze_handler_and_marks_frozen_rows(nicegui_client):
    from nicegui import ui
    views = _views(frozen=_frozen())
    frozen = _frozen()
    payload = {"views": views, "symbols_by_label": {}, "valuation_mode": VALUATION_MODE_MARKET,
               "base_notional": 10_000.0, "available_buying_power": 1_000.0, "account_value": None,
               "unallocated_pct": 0.0, "exclusions": {}, "frozen": frozen}
    with nicegui_client:
        page._render_labels(1, payload, _noop)
    table = next(el for el in nicegui_client.layout.descendants() if isinstance(el, ui.table))
    assert any(l.type == "freezeToggle" for l in table._event_listeners.values())
    assert "pf-row-frozen" in table._props[":table-row-class-fn"]
    rows = {r["symbol"]: r for r in table.rows}
    assert rows["VST"]["frozen"] is True and rows["VST"]["frozen_badge"] == "frozen"
    assert rows["ABC"]["frozen"] is False


def test_confirming_writes_the_freeze_and_refreshes(account_id, monkeypatch):
    import asyncio
    from nicegui import ui as nicegui_ui
    sent = []
    monkeypatch.setattr(nicegui_ui, 'notify', lambda message, **kw: sent.append((str(message), kw.get('type'))))
    refreshed = []

    async def refresh():
        refreshed.append(True)
    assert asyncio.run(page.apply_freeze_change(account_id, "vst", False, "earnings", refresh)) is True
    assert set(afz.get_frozen(account_id)) == {"VST"} and afz.get_frozen(account_id)["VST"].note == "earnings"
    assert asyncio.run(page.apply_freeze_change(account_id, "VST", True, None, refresh)) is True
    assert afz.get_frozen(account_id) == {} and refreshed == [True, True]
    assert sent == [('VST frozen', 'positive'), ('VST unfrozen', 'positive')]


def test_a_failed_write_is_reported_not_swallowed_and_does_not_refresh(account_id, monkeypatch):
    import asyncio
    from nicegui import ui as nicegui_ui
    sent = []
    monkeypatch.setattr(nicegui_ui, 'notify', lambda message, **kw: sent.append((str(message), kw.get('type'))))
    refreshed = []

    async def refresh():
        refreshed.append(True)

    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(afz, "freeze_symbol", boom)
    monkeypatch.setattr(page.logger, "error", lambda *a, **k: None)
    assert asyncio.run(page.apply_freeze_change(account_id, "VST", False, None, refresh)) is False
    assert refreshed == [] and sent and sent[0][1] == 'negative' and 'db locked' in sent[0][0]
