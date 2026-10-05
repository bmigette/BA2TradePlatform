"""The ONE exclusion mechanism: the operator's manual 'disable' of a symbol.

Semantics under test (both from the operator's brief and the design doc):
  1. the plan never emits an order for an excluded symbol (buy or sell);
  2. label calculations ignore it: its value is not in the label's value nor in the investable
     base, its share is ignored and the enabled symbols' shares are normalised over the enabled
     ones (a shortfall that is NOT an excluded share -- a freed TP/SL share -- stays unallocated);
  3. re-including restores everything;
  4. a TP/SL fill does NOT exclude (protection never does).
"""
from types import SimpleNamespace

import pytest
from sqlmodel import select

from ba2_trade_platform.core import allocator_exclusion as aex
from ba2_trade_platform.core import portfolio_allocation_service as svc
from ba2_trade_platform.core.allocator_protection_models import (
    EXCLUDED_DISABLED, AllocatorExclusion,
)
from ba2_trade_platform.core.db import add_instance, get_db
from ba2_trade_platform.core.models import PortfolioAllocationSymbol
from ba2_trade_platform.core.portfolio_allocation import (
    ALLOCATION_MODE_REBALANCE, VALUATION_MODE_MARKET, AllocationPlan, LabelTarget, SymbolTarget,
)
from ba2_trade_platform.core.portfolio_allocation_store import set_managed_label, set_symbol_weight
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.core.utils import add_label_to_instruments
from ba2_trade_platform.ui.pages import portfolio_allocation as page
from ba2_trade_platform.ui.utils import allocator_protection_view as view
from ba2_trade_platform.ui.utils.portfolio_allocation_view import (
    ManagedLabel, build_label_views, effective_symbol_weights, positions_by_symbol,
)
from tests.test_portfolio_allocation_page import (  # noqa: F401 -- fixtures + doubles reused
    _AllocAccount, _held, _pos, _use_account, account_id, run_to_thread_inline,
)
from tests.test_portfolio_allocation_submit import FakeAccount, make_base, make_row


@pytest.fixture(autouse=True)
def _silence_activity(monkeypatch):
    monkeypatch.setattr(svc, "log_activity", lambda *a, **k: None)


# ============================================================================ the store

def test_exclude_and_include_round_trip(account_id):
    assert aex.get_exclusions(account_id) == {}
    row = aex.exclude_symbol(account_id, "vst", note="bought manually")
    assert (row.symbol, row.excluded_reason, row.note) == ("VST", EXCLUDED_DISABLED, "bought manually")
    got = aex.get_exclusions(account_id)
    assert set(got) == {"VST"} and got["VST"].since is not None
    assert aex.excluded_symbols(account_id) == {"VST"}
    assert aex.include_symbol(account_id, "VST") is True
    assert aex.get_exclusions(account_id) == {}
    assert aex.include_symbol(account_id, "VST") is False          # nothing left to include


def test_excluding_twice_keeps_one_row_and_the_original_since(account_id):
    first = aex.exclude_symbol(account_id, "VST")
    second = aex.exclude_symbol(account_id, "vst", note="later note")
    assert second.since == first.since and second.note == "later note"
    with get_db() as session:
        assert len(session.exec(select(AllocatorExclusion)).all()) == 1


def test_exclusions_are_per_account(account_id):
    aex.exclude_symbol(account_id, "VST")
    assert aex.get_exclusions(account_id + 1) == {}


def test_a_blank_symbol_is_refused(account_id):
    with pytest.raises(ValueError):
        aex.exclude_symbol(account_id, "  ")


def test_split_included_drops_the_excluded_and_keeps_order():
    assert aex.split_included(["ABC", "vst", "XYZ"], ["VST"]) == ["ABC", "XYZ"]
    assert aex.split_included(["ABC"], []) == ["ABC"]


def test_the_only_reason_is_manual_disable():
    import ba2_trade_platform.core.allocator_protection_models as m
    assert m.EXCLUDED_DISABLED == "disabled"
    assert not hasattr(m, "EXCLUDED_HELD") and not hasattr(m, "HELD_AFTER_PROTECTIVE_FILL")


def test_reduce_symbol_weights_rejects_a_factor_outside_zero_to_one(account_id):
    with pytest.raises(ValueError):
        aex.reduce_symbol_weights(account_id, "ABC", 1.5, reason="tp_fill", detail="x")


def test_reduce_symbol_weights_without_a_stored_row_is_a_no_op(account_id):
    assert aex.reduce_symbol_weights(account_id, "ABC", 0.5, reason="tp_fill", detail="x") == []
    assert aex.get_weight_changes(account_id) == []


# ============================================================================ effective weights (pure)

def test_nothing_excluded_leaves_the_weights_alone():
    assert effective_symbol_weights({"A": 60.0, "B": 40.0}, []) == {"A": 60.0, "B": 40.0}
    assert effective_symbol_weights({"A": 60.0, "B": 40.0}, ["ZZZ"]) == {"A": 60.0, "B": 40.0}


def test_an_excluded_share_is_spread_over_the_enabled_symbols():
    eff = effective_symbol_weights({"A": 40.0, "B": 40.0, "VST": 20.0}, ["VST"])
    assert eff == {"A": pytest.approx(50.0), "B": pytest.approx(50.0)}


def test_a_freed_share_is_not_spread_only_the_excluded_one_is():
    """A=40 B=40 VST=10 sums to 90: the missing 10 was FREED and stays unallocated, the excluded
    10 is spread -- so the enabled symbols total 90, not 100."""
    eff = effective_symbol_weights({"A": 40.0, "B": 40.0, "VST": 10.0}, ["VST"])
    assert eff == {"A": pytest.approx(45.0), "B": pytest.approx(45.0)}
    assert sum(eff.values()) == pytest.approx(90.0)


def test_effective_weights_are_case_insensitive_and_drop_every_excluded_symbol():
    eff = effective_symbol_weights({"a": 50.0, "vst": 30.0, "xyz": 20.0}, ["VST", "xyz"])
    assert eff == {"A": pytest.approx(100.0)}


def test_nothing_to_scale_when_the_enabled_symbols_are_all_zero():
    assert effective_symbol_weights({"A": 0.0, "VST": 100.0}, ["VST"]) == {"A": 0.0}


def test_re_including_restores_the_original_weights():
    w = {"A": 40.0, "B": 40.0, "VST": 20.0}
    assert effective_symbol_weights(w, []) == w


# ============================================================================ the label maths on the page

def _views(excluded=None, freed=None, label_target=100.0, weights=None, base=10_000.0):
    book = [SimpleNamespace(symbol="ABC", qty=10.0, cost_basis=500.0, market_value=1000.0, side=None),
            SimpleNamespace(symbol="XYZ", qty=10.0, cost_basis=500.0, market_value=1000.0, side=None),
            SimpleNamespace(symbol="VST", qty=20.0, cost_basis=1000.0, market_value=2000.0, side=None)]
    return build_label_views(
        [ManagedLabel("L", label_target)], {"L": ["ABC", "XYZ", "VST"]},
        positions_by_symbol(book), {"ABC": 100.0, "XYZ": 100.0, "VST": 100.0}, {},
        valuation_mode=VALUATION_MODE_MARKET, base_notional=base,
        symbol_weights={"L": weights or {"ABC": 40.0, "XYZ": 40.0, "VST": 20.0}},
        excluded=excluded, freed_labels=freed)


def _exc(**kw):
    return {"VST": SimpleNamespace(excluded_reason="disabled", since=None, note=kw.get("note"))}


def test_without_exclusions_the_view_is_what_it_always_was():
    (v,) = _views()
    assert v.current_value == 4000.0 and v.excluded_value == 0.0 and v.excluded_count == 0
    assert all(not r.excluded and r.effective_weight_pct is None for r in v.rows)
    assert v.freed_pct == 0.0


def test_an_excluded_symbols_value_is_outside_the_label_and_shown_separately():
    (v,) = _views(excluded=_exc())
    assert v.current_value == 2000.0                       # ABC + XYZ only
    assert v.excluded_value == 2000.0 and v.excluded_count == 1
    assert v.pct_of_total == 100.0                         # the distinct managed total excludes VST too
    vst = next(r for r in v.rows if r.symbol == "VST")
    assert vst.excluded and vst.current_value == 2000.0    # still shown for information
    assert vst.quantity == 20.0 and vst.market_value == 2000.0
    assert (vst.pct_of_label, vst.pct_of_total, vst.target_value, vst.pct_of_label_target) == (0.0, 0.0, None, 0.0)


def test_enabled_rows_get_the_effective_share_and_target_money():
    (v,) = _views(excluded=_exc())
    abc = next(r for r in v.rows if r.symbol == "ABC")
    assert abc.weight_pct == 40.0                          # the stored box is untouched
    assert abc.effective_weight_pct == pytest.approx(50.0)
    assert abc.target_value == pytest.approx(10_000.0 * 100.0 / 100.0 * 0.5)    # label target $ x effective %
    assert abc.pct_of_label == pytest.approx(50.0)         # of the ENABLED label value


def test_the_label_target_money_applies_to_the_enabled_symbols_only():
    (v,) = _views(excluded=_exc(), label_target=60.0)
    shares = {r.symbol: r.target_value for r in v.rows if not r.excluded}
    assert sum(shares.values()) == pytest.approx(10_000.0 * 0.6)          # the whole 60% label target
    assert next(r for r in v.rows if r.symbol == "VST").target_value is None


def test_a_freed_share_is_reported_only_for_a_label_with_a_weight_change_record():
    (v,) = _views(weights={"ABC": 40.0, "XYZ": 40.0, "VST": 10.0}, excluded=_exc(), freed={"L"})
    assert v.freed_pct == pytest.approx(10.0)
    abc = next(r for r in v.rows if r.symbol == "ABC")
    assert abc.effective_weight_pct == pytest.approx(45.0)                  # freed 10 NOT spread
    (typed,) = _views(weights={"ABC": 40.0, "XYZ": 40.0, "VST": 10.0}, excluded=_exc())
    assert typed.freed_pct == 0.0                                           # a hand-typed 90 is not "freed"


def test_re_including_restores_the_label_maths():
    (before,) = _views()
    (excluded,) = _views(excluded=_exc())
    (after,) = _views(excluded={})
    assert excluded.current_value != before.current_value
    assert after.current_value == before.current_value
    assert [(r.symbol, r.target_value) for r in after.rows] == [(r.symbol, r.target_value) for r in before.rows]


def test_exclusion_keys_are_matched_case_insensitively():
    (v,) = _views(excluded={"vst": SimpleNamespace(excluded_reason="disabled", since=None, note=None)})
    assert v.excluded_count == 1


def test_the_label_pnl_ignores_the_excluded_symbol():
    (without,) = _views(excluded=_exc())
    (everything,) = _views()
    assert without.pnl.amount != everything.pnl.amount
    assert without.cost_basis == 1000.0                      # ABC + XYZ cost only


# ============================================================================ the dry-run inputs

def _setup_label(account_id, weights=None):
    set_managed_label(account_id, "L", target_pct=100.0)
    add_label_to_instruments(["ABC", "XYZ", "VST"], "L")
    for sym, w in (weights or {"ABC": 40.0, "XYZ": 40.0, "VST": 20.0}).items():
        set_symbol_weight(account_id, "L", sym, weight_pct=w)


def _account(account_id, monkeypatch):
    account = _AllocAccount(
        account_id, {"manual_trading_enabled": True},
        positions=[_held("ABC", 10, 500.0, 1000.0), _held("XYZ", 10, 500.0, 1000.0),
                   _held("VST", 20, 1000.0, 2000.0)],
        prices={"ABC": 100.0, "XYZ": 100.0, "VST": 100.0})
    _use_account(monkeypatch, account)
    return account


def test_flow_inputs_never_hand_the_engine_an_excluded_symbol(monkeypatch, account_id):
    _setup_label(account_id)
    _account(account_id, monkeypatch)
    aex.exclude_symbol(account_id, "VST")
    base, labels, _frac, _reserve = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)
    assert [st.symbol for st in labels[0].symbols] == ["ABC", "XYZ"]
    weights = {st.symbol: st.weight_pct for st in labels[0].symbols}
    assert weights == {"ABC": pytest.approx(50.0), "XYZ": pytest.approx(50.0)}     # normalised over enabled
    # the base is buying power + the ENABLED managed value only
    assert base.managed_value == pytest.approx(2000.0)


def test_flow_inputs_with_a_freed_share_keep_it_unallocated(monkeypatch, account_id):
    _setup_label(account_id, {"ABC": 40.0, "XYZ": 40.0, "VST": 10.0})
    _account(account_id, monkeypatch)
    aex.exclude_symbol(account_id, "VST")
    _base, labels, *_ = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)
    assert sum(st.weight_pct for st in labels[0].symbols) == pytest.approx(90.0)


def test_re_including_restores_the_flow_inputs(monkeypatch, account_id):
    _setup_label(account_id)
    _account(account_id, monkeypatch)
    aex.exclude_symbol(account_id, "VST")
    page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)
    aex.include_symbol(account_id, "VST")
    base, labels, *_ = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)
    assert sorted(st.symbol for st in labels[0].symbols) == ["ABC", "VST", "XYZ"]
    assert {st.symbol: st.weight_pct for st in labels[0].symbols} == {"ABC": 40.0, "XYZ": 40.0, "VST": 20.0}
    assert base.managed_value == pytest.approx(4000.0)


def test_the_plan_emits_no_order_for_an_excluded_symbol(monkeypatch, account_id):
    """End to end through the real solve: VST is held (2000) and would be SOLD down to its 20%
    share of 100% of (bp + managed) -- excluded, it is not even in the plan."""
    _setup_label(account_id)
    account = _account(account_id, monkeypatch)
    aex.exclude_symbol(account_id, "VST")
    _base, labels, *_ = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)
    _b, plan, current, _hours = page._solve_plan(
        account_id, mode=ALLOCATION_MODE_REBALANCE, labels=labels, scope_label=None, amount=0.0,
        allow_fractional=True, valuation_mode=VALUATION_MODE_MARKET)
    assert "VST" not in {r.symbol for r in plan.rows}
    assert "VST" not in current
    assert {r.symbol for r in plan.rows} == {"ABC", "XYZ"}


def test_the_view_payload_excludes_the_symbols_value_from_the_base(monkeypatch, account_id):
    _setup_label(account_id)
    _account(account_id, monkeypatch)
    everything = page._load_view_payload(account_id, VALUATION_MODE_MARKET)
    aex.exclude_symbol(account_id, "VST", note="bought manually")
    excluded = page._load_view_payload(account_id, VALUATION_MODE_MARKET)
    assert excluded["base_notional"] == pytest.approx(everything["base_notional"] - 2000.0)
    assert set(excluded["exclusions"]) == {"VST"} and excluded["exclusions"]["VST"].note == "bought manually"
    assert excluded["views"][0].excluded_value == 2000.0
    assert everything["exclusions"] == {}


# ============================================================================ the boundary guarantee

def _plan(rows):
    return AllocationPlan(rows=rows, available_buying_power=10_000.0)


def _row(symbol, delta, price=100.0):
    row = make_row(symbol, OrderDirection.SELL if delta < 0 else OrderDirection.BUY, delta,
                   abs(delta) * price, 0.0 if delta < 0 else abs(delta) * price, price=price)
    row.target_quantity = 50.0 + delta
    return row


def test_run_allocation_never_trades_an_excluded_symbol_even_from_a_stale_plan():
    account = FakeAccount(account_id=61)
    account.positions = []
    aex.exclude_symbol(61, "VST", note="bought manually")
    plan = _plan([_row("VST", 5.0), _row("AAPL", 2.0, 160.0)])
    result = svc.run_allocation(account, plan, {}, make_base(), mode=ALLOCATION_MODE_REBALANCE,
                                scope_label=None)
    assert [s[0] for s in account.submitted] == ["AAPL"]                 # no order for VST, buy or sell
    outcomes = {o.symbol: o for o in result["outcomes"]}
    assert outcomes["VST"].status == svc.OUTCOME_SKIPPED
    assert "excluded from allocation" in outcomes["VST"].message and "bought manually" in outcomes["VST"].message
    from ba2_trade_platform.core.models import PortfolioAllocationRun
    with get_db() as session:
        run = session.get(PortfolioAllocationRun, result["run_id"])
    assert [r["symbol"] for r in run.plan_json["rows"]] == ["AAPL"]      # the RECORDED plan excludes it


def test_a_sell_of_an_excluded_symbol_is_refused_too():
    account = FakeAccount(account_id=62)
    account.positions = []
    aex.exclude_symbol(62, "VST")
    result = svc.run_allocation(account, _plan([_row("VST", -5.0)]), {}, make_base(),
                                mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert account.submitted == [] and account.closed == []
    assert result["outcomes"][0].status == svc.OUTCOME_SKIPPED


def test_including_it_again_lets_the_next_run_trade_it():
    account = FakeAccount(account_id=63)
    account.positions = []
    aex.exclude_symbol(63, "AAPL")
    aex.include_symbol(63, "AAPL")
    svc.run_allocation(account, _plan([_row("AAPL", 2.0, 160.0)]), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert [s[0] for s in account.submitted] == ["AAPL"]


def test_the_dry_run_shows_an_excluded_row_as_skipped_with_the_totals_following():
    account = FakeAccount(account_id=64)
    aex.exclude_symbol(64, "VST")
    out = svc.mark_excluded_rows_skipped(account, _plan([_row("VST", -5.0), _row("XYZ", 3.0, 20.0)]))
    by = {r.symbol: r for r in out.rows}
    assert by["VST"].skipped and by["VST"].delta_quantity == 0 and by["VST"].side is None
    assert any("excluded from allocation" in r for r in by["VST"].reasons)
    assert not by["XYZ"].skipped and out.total_sell_value == 0.0 and out.total_buy_value == pytest.approx(60.0)


def test_the_dry_run_is_untouched_when_nothing_is_excluded():
    account = FakeAccount(account_id=65)
    plan = _plan([_row("XYZ", 3.0, 20.0)])
    assert svc.mark_excluded_rows_skipped(account, plan) is plan


def test_every_broker_gets_the_exclusion_not_only_tastytrade():
    """FakeAccount has no protection capability at all and the exclusion still holds."""
    account = FakeAccount(account_id=66)
    assert getattr(account, "supports_allocator_protection", False) is False
    aex.exclude_symbol(66, "VST")
    svc.run_allocation(account, _plan([_row("VST", 5.0)]), {}, make_base(),
                       mode=ALLOCATION_MODE_REBALANCE, scope_label=None)
    assert account.submitted == []


# ============================================================================ persisting what a run went out with

def test_persisting_never_rewrites_the_stored_shares_with_scaled_ones(monkeypatch, account_id):
    _setup_label(account_id)
    _account(account_id, monkeypatch)
    aex.exclude_symbol(account_id, "VST")
    _base, labels, *_ = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)
    assert {st.symbol: st.weight_pct for st in labels[0].symbols}["ABC"] == pytest.approx(50.0)   # scaled
    persisted = page.labels_for_persist(account_id, labels)
    assert {st.symbol: st.weight_pct for st in persisted[0].symbols} == {"ABC": 40.0, "XYZ": 40.0}
    page.save_allocation_targets(account_id, persisted)
    with get_db() as session:
        stored = {r.symbol: r.weight_pct for r in session.exec(select(PortfolioAllocationSymbol)).all()}
    assert stored == {"ABC": 40.0, "XYZ": 40.0, "VST": 20.0}          # re-including it totals 100 again


def test_labels_without_an_excluded_symbol_pass_through_untouched(monkeypatch, account_id):
    _setup_label(account_id)
    _account(account_id, monkeypatch)
    _base, labels, *_ = page._load_flow_inputs(account_id, VALUATION_MODE_MARKET)
    assert page.labels_for_persist(account_id, labels) is labels


# ============================================================================ the UI

def test_exclusion_fields_for_an_included_symbol():
    f = view.exclusion_fields(None)
    assert f["excluded"] is False and "excl_badge" not in f
    assert f["excl_tip"] == "Click to exclude from allocation" and f["excl_hex"] == view.INCLUDED_HEX
    assert f["excl_icon"] == "visibility"


def test_exclusion_fields_for_an_excluded_symbol_name_the_reason_and_the_way_back():
    from datetime import datetime
    f = view.exclusion_fields(SimpleNamespace(since=datetime(2026, 10, 3), note="bought manually"))
    assert f["excluded"] and "excl_badge" not in f and f["excl_hex"] == view.EXCLUDED_HEX      # orange eye, no text
    assert f["excl_tip"].startswith("Excluded from allocation (manual) since 2026-10-03")
    assert "bought manually" in f["excl_tip"] and f["excl_tip"].endswith("Click to include again.")


def test_the_label_header_extras():
    assert view.label_extras_text(0.0, 0, 0.0) == ""
    assert view.label_extras_text(1234.4, 1, 0.0) == ""                      # excluded is the orange segment now
    assert view.label_extras_text(0.0, 0, 6.0) == "freed 6.00% from TP/SL fills"
    assert view.label_extras_text(500.0, 2, 3.5) == "freed 3.50% from TP/SL fills"


def test_the_effective_weight_note():
    assert view.effective_weight_text(40.0, 50.0) == "eff. 50.00%"
    assert view.effective_weight_text(40.0, None) == ""
    assert view.effective_weight_text(40.0, 40.001) == ""


@pytest.fixture
def nicegui_client():
    from nicegui.client import Client
    from nicegui.page import page as nicegui_page
    client = Client(nicegui_page("/test-pf-exclude"), request=None)
    yield client
    client.remove_elements(client.elements.values())


async def _noop():
    return None


def _render(client, exclusions, freed=None, weights=None):
    from nicegui import ui
    views = _views(excluded=exclusions, freed=freed, weights=weights)
    payload = {"views": views, "symbols_by_label": {}, "valuation_mode": VALUATION_MODE_MARKET,
               "base_notional": 10_000.0, "available_buying_power": 1_000.0, "account_value": None,
               "unallocated_pct": 0.0, "exclusions": exclusions or {}}
    with client:
        page._render_labels(1, payload, _noop)
    return next(el for el in client.layout.descendants() if isinstance(el, ui.table))


def test_every_broker_gets_the_exclude_handler_and_the_grey_row_class_but_no_column(nicegui_client):
    table = _render(nicegui_client, {})
    assert "exclude" not in [c["name"] for c in table.columns]
    assert "body-cell-exclude" not in table.slots
    assert any(l.type == "excludeToggle" for l in table._event_listeners.values())
    assert "table-row-class-fn" in " ".join(table._props)
    rsp = __import__("ba2_trade_platform.ui.utils.responsive", fromlist=["x"])
    rsp.check_card_columns(page.SYMBOL_CARD, [c["name"] for c in page.symbol_table_columns()])


def test_an_excluded_row_carries_the_badge_the_flag_and_no_deltas(nicegui_client):
    from datetime import datetime
    table = _render(nicegui_client, {"VST": SimpleNamespace(excluded_reason="disabled",
                                                            since=datetime(2026, 10, 3), note=None)})
    rows = {r["symbol"]: r for r in table.rows}
    vst, abc = rows["VST"], rows["ABC"]
    assert vst["excluded"] is True and "excl_badge" not in vst and vst["excl_hex"] == view.EXCLUDED_HEX
    assert vst["value_delta"] == "" and vst["qty_delta"] == "" and vst["share_delta"] == ""
    assert vst["quantity"] == 20.0 and vst["current_value"] == 2000.0          # information still shown
    assert abc["excluded"] is False and abc["eff_weight"] == "eff. 50.00%"


def test_the_label_header_has_no_excluded_text_but_an_orange_segment_with_the_value(nicegui_client):
    from datetime import datetime
    from nicegui import ui
    _render(nicegui_client, {"VST": SimpleNamespace(excluded_reason="disabled",
                                                    since=datetime(2026, 10, 3), note=None)})
    texts = [getattr(el, "text", "") for el in nicegui_client.layout.descendants()]
    assert not any("excluded" in t and t.startswith("+$") for t in texts)
    seg = [el for el in nicegui_client.layout.descendants()
           if page.MARKER_SEGMENT_PREFIX + "excluded" in getattr(el, "_markers", [])]
    assert len(seg) == 1 and seg[0].text == "1" and seg[0]._props.get("color") == "orange-8"
    tips = [el._text for el in seg[0].descendants() if isinstance(el, ui.tooltip)]
    assert tips == ["1 excluded: $2,000"]


def test_the_label_header_shows_freed_share(nicegui_client):
    _render(nicegui_client, {}, freed={"L"}, weights={"ABC": 40.0, "XYZ": 40.0, "VST": 10.0})
    texts = [getattr(el, "text", "") for el in nicegui_client.layout.descendants()]
    assert "freed 10.00% from TP/SL fills" in texts


def test_the_card_template_carries_the_toggle_the_badge_and_the_grey_class():
    template = page.symbol_card_template()
    assert "$emit('excludeToggle', props.row.symbol)" in template
    assert "props.row.excl_hex" in template and "excl_badge" not in template and "pf-sym-card--excl" in template
    assert "props.row.eff_weight" in template                       # the effective share under the box


def test_the_stylesheet_greys_an_excluded_row():
    css = page.page_phone_css()
    assert ".pf-row-excluded td" in css and ".pf-sym-card--excl" in css and ".pf-b-excl" in css
    assert page.page_css_path().read_text(encoding="utf-8") == css


def _dialog_texts(client):
    return [getattr(el, "text", "") or getattr(el, "_props", {}).get("label", "") or ""
            for el in client.layout.descendants()]


def test_the_exclude_dialog_says_what_excluding_does_and_that_tpsl_orders_stay(nicegui_client):
    with nicegui_client:
        page._open_exclusion_dialog(1, "vst", {}, _noop)
    texts = _dialog_texts(nicegui_client)
    assert "Exclude VST from allocation?" in texts
    assert any("Protective TP/SL orders stay exactly as they are" in t for t in texts)
    assert any("Note (optional)" in t for t in texts)


def test_the_include_dialog_asks_to_include_again(nicegui_client):
    with nicegui_client:
        page._open_exclusion_dialog(1, "VST", {"VST": object()}, _noop)
    texts = _dialog_texts(nicegui_client)
    assert "Include VST in allocation again?" in texts and not any("Note (optional)" in t for t in texts)


def test_confirming_writes_the_exclusion_and_refreshes(account_id, monkeypatch):
    import asyncio
    from nicegui import ui as nicegui_ui
    sent = []
    monkeypatch.setattr(nicegui_ui, 'notify', lambda message, **kw: sent.append((str(message), kw.get('type'))))
    refreshed = []

    async def refresh():
        refreshed.append(True)
    assert asyncio.run(page.apply_exclusion_change(account_id, "vst", False, "bought manually", refresh)) is True
    assert set(aex.get_exclusions(account_id)) == {"VST"} and refreshed == [True]
    assert aex.get_exclusions(account_id)["VST"].note == "bought manually"
    assert asyncio.run(page.apply_exclusion_change(account_id, "VST", True, None, refresh)) is True
    assert aex.get_exclusions(account_id) == {} and refreshed == [True, True]
    assert sent == [('VST excluded from allocation', 'positive'), ('VST included in allocation', 'positive')]


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
    monkeypatch.setattr(aex, "exclude_symbol", boom)
    errors = []
    monkeypatch.setattr(page.logger, "error", lambda msg, *a, **k: errors.append(str(msg)))
    assert asyncio.run(page.apply_exclusion_change(account_id, "VST", False, None, refresh)) is False
    assert refreshed == [] and any("Toggling exclusion of VST failed" in e for e in errors)
    assert sent and sent[0][1] == 'negative' and 'db locked' in sent[0][0]
