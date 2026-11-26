"""Live Trades "Live View" tab: the pure half (rows, totals, bars, weights, columns, sorting, display strings,
remembered choices, stale rows of an account whose read failed). No NiceGUI, no DB, no broker."""
from datetime import datetime, timezone

import pytest

from ba2_trade_platform.ui.utils import live_positions as lp

NOW = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 26, 15, 0, 10, tzinfo=timezone.utc)


def _raw(symbol="AAPL", qty=10.0, current_price=155.0, avg_entry_price=140.0, cost_basis=1400.0,
         market_value=1550.0, unrealized_pl=150.0, unrealized_plpc=150.0 / 1400.0, change_today=0.02,
         unrealized_intraday_pl=30.0, unrealized_intraday_plpc=0.0197, asset_class="us_equity", **extra):
    row = dict(symbol=symbol, qty=qty, current_price=current_price, avg_entry_price=avg_entry_price,
               cost_basis=cost_basis, market_value=market_value, unrealized_pl=unrealized_pl,
               unrealized_plpc=unrealized_plpc, change_today=change_today,
               unrealized_intraday_pl=unrealized_intraday_pl, unrealized_intraday_plpc=unrealized_intraday_plpc,
               asset_class=asset_class, side="long", exchange="NASDAQ", lastday_price=152.0)
    row.update(extra)
    return row


def _row(account_id=1, name="One", labels=(), **kw):
    return lp.position_row(account_id, name, _raw(**kw), labels)


# --------------------------------------------------------------------------- rows

def test_a_position_becomes_a_row_with_the_brokers_figures():
    r = _row(labels=("CORE",))
    assert r.symbol == "AAPL" and r.account == "One" and r.account_id == 1
    assert r.qty == 10.0 and r.price == 155.0 and r.avg_cost == 140.0
    assert r.cost_basis == 1400.0 and r.mkt_value == 1550.0
    assert r.pl_open == 150.0 and r.pl_open_pct == pytest.approx(150.0 / 1400.0)
    assert r.pl_day == 30.0 and r.pl_day_pct == pytest.approx(0.0197)
    assert r.day_chg_pct == pytest.approx(0.02)
    assert r.labels == ("CORE",) and r.asset_class == "us_equity"
    assert r.key == "1:AAPL" and r.stale is False


def test_an_object_position_reads_like_a_dict():
    class Pos:
        def __iter__(self):
            return iter(_raw(symbol="MSFT").items())
    assert lp.position_row(2, "Two", Pos(), ()).symbol == "MSFT"


@pytest.mark.parametrize("bad", [None, "12", float("nan"), float("inf"), True])
def test_a_missing_or_unusable_number_is_none_never_zero(bad):
    r = _row(unrealized_intraday_pl=bad)
    assert r.pl_day is None


def test_a_row_without_a_symbol_is_not_a_row():
    assert lp.position_row(1, "One", _raw(symbol=None), ()) is None
    assert lp.position_row(1, "One", _raw(symbol="  "), ()) is None


def test_a_short_keeps_the_brokers_signs():
    r = _row(qty=-10.0, cost_basis=-1400.0, market_value=-1550.0, unrealized_pl=-150.0,
             unrealized_plpc=-150.0 / 1400.0, unrealized_intraday_pl=-30.0)
    assert r.qty == -10.0 and r.cost_basis == -1400.0 and r.mkt_value == -1550.0
    assert r.pl_open == -150.0 and r.pl_day == -30.0


# --------------------------------------------------------------------------- totals

def test_totals_sum_the_money_columns():
    rows = [_row(symbol="A", unrealized_intraday_pl=10.0, unrealized_pl=100.0, cost_basis=1000.0, market_value=1100.0),
            _row(symbol="B", unrealized_intraday_pl=-4.0, unrealized_pl=-50.0, cost_basis=500.0, market_value=450.0)]
    t = lp.compute_totals(rows)
    assert t.pl_day.value == pytest.approx(6.0) and t.pl_day.missing == 0
    assert t.pl_open.value == pytest.approx(50.0)
    assert t.cost_basis.value == pytest.approx(1500.0)
    assert t.mkt_value.value == pytest.approx(1550.0)
    assert t.pl_open_pct.value == pytest.approx(50.0 / 1500.0)


def test_pl_open_pct_total_divides_by_absolute_cost_basis_so_a_short_does_not_cancel():
    rows = [_row(symbol="L", unrealized_pl=100.0, cost_basis=1000.0),
            _row(symbol="S", qty=-5.0, unrealized_pl=50.0, cost_basis=-1000.0, market_value=-950.0)]
    t = lp.compute_totals(rows)
    assert t.pl_open_pct.value == pytest.approx(150.0 / 2000.0)
    assert t.cost_basis.value == pytest.approx(0.0)          # the signed sum, as the broker reports it


def test_a_missing_value_is_excluded_and_the_total_is_marked_partial():
    rows = [_row(symbol="A", unrealized_intraday_pl=20.0), _row(symbol="B", unrealized_intraday_pl=10.11),
            _row(symbol="C", unrealized_intraday_pl=None), _row(symbol="D", unrealized_intraday_pl=None)]
    t = lp.compute_totals(rows)
    assert t.pl_day.value == pytest.approx(30.11) and t.pl_day.missing == 2
    assert lp.total_text(t.pl_day, "money") == "30.11 (2 n/a)"


def test_a_total_with_every_value_missing_is_na():
    t = lp.compute_totals([_row(unrealized_intraday_pl=None)])
    assert t.pl_day.value is None and lp.total_text(t.pl_day, "money") == "n/a"


def test_pl_open_pct_total_counts_a_row_missing_either_half_as_na():
    rows = [_row(symbol="A", unrealized_pl=100.0, cost_basis=1000.0), _row(symbol="B", cost_basis=None),
            _row(symbol="C", unrealized_pl=None)]
    t = lp.compute_totals(rows)
    assert t.pl_open_pct.value == pytest.approx(0.1) and t.pl_open_pct.missing == 2


def test_no_rows_is_a_genuine_zero():
    t = lp.compute_totals([])
    assert t.pl_day.value == 0.0 and t.pl_day.missing == 0 and t.pl_open_pct.value is None


def test_totals_count_stale_rows():
    rows = [_row(symbol="A"), lp.mark_stale(_row(symbol="B"))]
    assert lp.compute_totals(rows).stale_rows == 1


# --------------------------------------------------------------------------- bars and weights

def test_the_bar_is_relative_to_the_largest_absolute_pl_open():
    rows = [_row(symbol="A", unrealized_pl=50.0), _row(symbol="B", unrealized_pl=-200.0),
            _row(symbol="C", unrealized_pl=None), _row(symbol="D", unrealized_pl=0.0)]
    bars = lp.bar_fractions(rows)
    assert bars["1:A"] == pytest.approx(0.25) and bars["1:B"] == pytest.approx(1.0)
    assert bars["1:C"] is None and bars["1:D"] == 0.0


def test_no_bar_when_every_pl_open_is_zero_or_missing():
    bars = lp.bar_fractions([_row(symbol="A", unrealized_pl=0.0), _row(symbol="B", unrealized_pl=None)])
    assert bars == {"1:A": 0.0, "1:B": None}


def test_weight_is_the_share_of_total_absolute_market_value():
    rows = [_row(symbol="A", market_value=300.0), _row(symbol="S", qty=-1.0, market_value=-100.0),
            _row(symbol="N", market_value=None)]
    w = lp.weights(rows)
    assert w["1:A"] == pytest.approx(0.75) and w["1:S"] == pytest.approx(0.25) and w["1:N"] is None


# --------------------------------------------------------------------------- display

@pytest.mark.parametrize("value, text", [(1234.5, "1,234.50"), (-3.456, "-3.46"), (0.0, "0.00"), (None, "n/a")])
def test_money_text(value, text):
    assert lp.fmt_money(value) == text


def test_percent_text_from_a_fraction():
    assert lp.fmt_pct(0.063, 1) == "6.3%"
    assert lp.fmt_pct(-0.0125, 2) == "-1.25%"
    assert lp.fmt_pct(None, 1) == "n/a"


def test_quantity_and_price_text():
    assert lp.fmt_qty(10.0) == "10" and lp.fmt_qty(-2.5) == "-2.5" and lp.fmt_qty(None) == "n/a"
    assert lp.fmt_price(155.0) == "155.00" and lp.fmt_price(0.1234) == "0.1234"


def test_colours_green_red_and_grey_for_what_shows_as_zero():
    assert lp.sign_colour(1.0, 2) == lp.GREEN == "#4ade80"
    assert lp.sign_colour(-1.0, 2) == lp.RED == "#f87171"
    assert lp.sign_colour(0.004, 2) == lp.GREY          # shows as 0.00
    assert lp.sign_colour(None, 2) == lp.GREY


def test_table_rows_carry_raw_values_texts_colours_and_the_bar():
    rows = lp.table_rows([_row(symbol="A", unrealized_pl=-50.0, unrealized_plpc=-0.0357, labels=("X", "Y")),
                          _row(symbol="B", unrealized_pl=100.0)])
    a = rows[0]
    assert a["key"] == "1:A" and a["symbol"] == "A" and a["account"] == "One"
    assert a["pl_open"] == -50.0 and a["pl_open_text"] == "-50.00" and a["pl_open_color"] == lp.RED
    assert a["pl_open_pct_text"] == "-3.6%"
    assert a["pl_open_bar"] == pytest.approx(0.5)
    assert a["labels"] == "X, Y" and a["labels_list"] == ["X", "Y"]
    assert "a" in a["search"] and "x" in a["search"] and "one" in a["search"]
    assert a["weight_pct"] == pytest.approx(0.5) and a["weight_pct_text"] == "50.0%"
    assert a["qty_color"] == ""                          # an uncoloured column
    assert a["stale"] is False


def test_a_none_value_shows_na_in_grey_and_has_no_bar():
    row = lp.table_rows([_row(unrealized_pl=None)])[0]
    assert row["pl_open"] is None and row["pl_open_text"] == "n/a" and row["pl_open_color"] == lp.GREY
    assert row["pl_open_bar"] is None


# --------------------------------------------------------------------------- columns

def test_phone_shows_symbol_then_the_three_pl_columns_first():
    names = lp.column_names(phone=True, show_account=True)
    assert names[:4] == ["symbol", "pl_day", "pl_open_pct", "pl_open"]
    assert set(names) == set(lp.column_names(phone=False, show_account=True))


def test_the_desktop_order():
    assert lp.column_names(phone=False, show_account=True) == [
        "symbol", "account", "qty", "price", "day_chg_pct", "pl_day", "pl_day_pct", "pl_open_pct", "pl_open",
        "avg_cost", "cost_basis", "mkt_value", "weight_pct", "asset_class", "labels"]


def test_the_account_column_only_when_all_accounts_are_shown():
    assert "account" not in lp.column_names(phone=False, show_account=False)
    assert "account" not in lp.column_names(phone=True, show_account=False)


def test_quasar_columns_are_sortable_tagged_and_carry_the_totals():
    totals = lp.compute_totals([_row(unrealized_intraday_pl=12.5)])
    cols = lp.quasar_columns(phone=False, show_account=True, totals=totals)
    by = {c["name"]: c for c in cols}
    assert all(c["sortable"] for c in cols)
    assert by["symbol"]["total_text"] == "Total"
    assert by["pl_day"]["total_text"] == "12.50" and by["pl_day"]["total_color"] == lp.GREEN
    assert by["qty"]["total_text"] == ""
    assert "lt-c-symbol" in by["symbol"]["classes"] and "lt-c-pl_open" in by["pl_open"]["headerClasses"]
    assert by["pl_day"]["field"] == "pl_day" and by["pl_day"]["align"] == "right"
    assert by["symbol"]["align"] == "left"


def test_the_total_label_says_partial_when_an_account_is_not_read():
    totals = lp.compute_totals([_row()], unread_accounts=1)
    by = {c["name"]: c for c in lp.quasar_columns(phone=True, show_account=False, totals=totals)}
    assert by["symbol"]["total_text"] == "Total (partial)"


def test_the_phone_pin_spec_is_valid():
    from ba2_trade_platform.ui.utils.responsive import check_pinned_columns
    check_pinned_columns(lp.LIVE_PINNED, lp.column_names(phone=True, show_account=True), lp.LIVE_MIN_WIDTHS)
    assert [p.name for p in lp.LIVE_PINNED] == ["symbol"]


def test_the_phone_sheet_is_phone_only_and_pins_the_symbol():
    css = lp.live_view_phone_css()
    assert css.startswith("@media (max-width: 639px)")
    assert f".{lp.TABLE_CLASS} td.lt-c-symbol" in css and "position: sticky" in css
    assert f".{lp.DESKTOP_ONLY_CLASS}" in css and "q-table--col-auto-width" in css


# --------------------------------------------------------------------------- sorting and filtering

def _dicts(*rows):
    return lp.table_rows(list(rows))


def test_sort_by_a_number_descending_puts_none_last_like_quasar():
    rows = _dicts(_row(symbol="A", unrealized_intraday_pl=1.0), _row(symbol="B", unrealized_intraday_pl=None),
                  _row(symbol="C", unrealized_intraday_pl=5.0))
    assert [r["symbol"] for r in lp.sort_rows(rows, "pl_day", True)] == ["C", "A", "B"]
    assert [r["symbol"] for r in lp.sort_rows(rows, "pl_day", False)] == ["B", "A", "C"]


def test_sort_by_text_ignores_case():
    rows = _dicts(_row(symbol="msft"), _row(symbol="AAPL"), _row(symbol="Brk.B"))
    assert [r["symbol"] for r in lp.sort_rows(rows, "symbol", False)] == ["AAPL", "Brk.B", "msft"]


def test_sort_key_orders_none_below_every_number():
    assert lp.sort_key(None) < lp.sort_key(-1e18) < lp.sort_key(0.0)


def test_filter_matches_symbol_account_asset_class_and_labels():
    rows = [_row(symbol="AAPL", labels=("CORE",)), _row(symbol="XOM", account_id=2, name="Tasty")]
    assert [r.symbol for r in lp.filter_rows(rows, "core")] == ["AAPL"]
    assert [r.symbol for r in lp.filter_rows(rows, " tasty ")] == ["XOM"]
    assert [r.symbol for r in lp.filter_rows(rows, "")] == ["AAPL", "XOM"]


def test_the_browser_filter_uses_the_same_search_text():
    assert "r.search" in lp.FILTER_METHOD_JS and "toLowerCase" in lp.FILTER_METHOD_JS


# --------------------------------------------------------------------------- stale rows

def _read(account_id, name, status, rows=(), reason=None):
    return lp.AccountRead(account_id=account_id, name=name, status=status, reason=reason, rows=tuple(rows))


def test_a_failed_account_keeps_its_last_good_rows_marked_stale():
    book = lp.merge_reads({}, [_read(1, "One", lp.STATUS_OK, [_row(symbol="A")]),
                               _read(2, "Two", lp.STATUS_OK, [_row(2, "Two", symbol="B")])], NOW)
    book = lp.merge_reads(book, [_read(1, "One", lp.STATUS_OK, [_row(symbol="A")]),
                                 _read(2, "Two", lp.STATUS_FAILED, reason="the positions read failed")], LATER)
    rows = lp.book_rows(book)
    assert [(r.symbol, r.stale) for r in rows] == [("A", False), ("B", True)]
    warning = lp.book_warnings(book)
    assert len(warning) == 1 and "Two" in warning[0] and "the positions read failed" in warning[0]
    assert "15:00:00" in warning[0] or "stale" in warning[0]


def test_a_failed_account_on_first_load_shows_a_warning_and_no_rows():
    book = lp.merge_reads({}, [_read(1, "One", lp.STATUS_OK, [_row(symbol="A")]),
                               _read(2, "Two", lp.STATUS_TIMED_OUT, reason="no answer within 15s")], NOW)
    assert [r.symbol for r in lp.book_rows(book)] == ["A"]
    warning = lp.book_warnings(book)
    assert len(warning) == 1 and "Two" in warning[0] and "not shown" in warning[0]
    assert lp.unread_accounts(book) == 1


def test_a_flat_account_is_not_a_failure():
    book = lp.merge_reads({}, [_read(1, "One", lp.STATUS_OK, [])], NOW)
    assert lp.book_rows(book) == [] and lp.book_warnings(book) == [] and lp.unread_accounts(book) == 0


def test_an_account_that_left_the_scope_is_dropped():
    book = lp.merge_reads({}, [_read(1, "One", lp.STATUS_OK, [_row(symbol="A")])], NOW)
    book = lp.merge_reads(book, [_read(2, "Two", lp.STATUS_OK, [_row(2, "Two", symbol="B")])], LATER)
    assert [r.symbol for r in lp.book_rows(book)] == ["B"]


def test_a_recovered_account_is_fresh_again():
    book = lp.merge_reads({}, [_read(1, "One", lp.STATUS_OK, [_row(symbol="A")])], NOW)
    book = lp.merge_reads(book, [_read(1, "One", lp.STATUS_FAILED, reason="x")], LATER)
    book = lp.merge_reads(book, [_read(1, "One", lp.STATUS_OK, [_row(symbol="A")])], LATER)
    assert [r.stale for r in lp.book_rows(book)] == [False] and lp.book_warnings(book) == []


# --------------------------------------------------------------------------- remembered choices

def test_storage_key_is_per_port():
    assert lp.storage_key(8080) == "live_trades_page_state:8080"
    assert lp.storage_key(None) == "live_trades_page_state"


def test_resolve_state_defaults():
    s = lp.resolve_state(None, ["Stocks", "Options", "Live View"])
    assert s == lp.PageState(tab="Stocks", interval="10", sort_by="pl_day", descending=True)


def test_resolve_state_keeps_valid_choices_and_drops_invalid_ones():
    tabs = ["Stocks", "Options", "Live View"]
    s = lp.resolve_state({"tab": "Live View", "interval": "5", "sort_by": "symbol", "descending": False}, tabs)
    assert s == lp.PageState(tab="Live View", interval="5", sort_by="symbol", descending=False)
    s = lp.resolve_state({"tab": "Gone", "interval": "7", "sort_by": "nope", "descending": "yes"}, tabs)
    assert s == lp.PageState(tab="Stocks", interval="10", sort_by="pl_day", descending=True)


def test_state_round_trips_through_storage():
    tabs = ["Stocks", "Live View"]
    s = lp.PageState(tab="Live View", interval="off", sort_by="pl_open", descending=False)
    assert lp.resolve_state(lp.state_for_storage(s), tabs) == s


def test_intervals():
    assert list(lp.INTERVALS) == ["5", "10", "30", "off"]
    assert lp.interval_seconds("5") == 5.0 and lp.interval_seconds("off") is None
