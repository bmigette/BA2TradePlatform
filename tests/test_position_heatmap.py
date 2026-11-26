"""Overview "Heatmap" tab: the pure rules (periods, reference closes, both colour metrics, both size modes,
aggregation, filters, colour clamp, remembered choices, the chart options).

No NiceGUI, no DB, no network: everything here is plain data in, plain data out. The NYSE calendar is the
platform's own offline schedule (``ba2_common.core.market_calendar``)."""
import json
import math
from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_trade_platform.ui.utils import position_heatmap as hm
from ba2_trade_platform.ui.utils.position_heatmap import (
    Fill, HeatmapState, Lot, MarketInputs, NO_EXPERT, UNLABELED,
)

THANKSGIVING = date(2026, 11, 26)        # Thursday, market closed
TUESDAY = date(2026, 11, 24)


def utc(y, m, d, hh=15, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def fill(side, qty, price, day, market=True, hour=15, seen_by=None):
    """A fill whose order was placed on ``day`` at ``hour`` UTC (15:00 UTC = 10:00 ET, inside the session)."""
    return Fill(side=side, qty=qty, price=price, placed=utc(day.year, day.month, day.day, hour),
                is_market=market, seen_by=seen_by)


def lot(symbol="AAPL", qty=10.0, direction="long", expert="Alpha-1", fills=(), tx=1, option=False, account=1):
    return Lot(transaction_id=tx, symbol=symbol, expert=expert, direction=direction, quantity=qty,
               fills=tuple(fills), is_option=option, account_id=account)


def market(prices=None, closes=None, caps=None, labels=None, price_missing=None, closes_missing=None,
           caps_missing=None, splits=None, splits_missing=None, broker_qty=None, broker_failed=None,
           broker_option_rows=None, broker_history=None, platform_orders=None):
    return MarketInputs(prices=prices or {}, price_missing=price_missing or {},
                        closes=closes or {}, closes_missing=closes_missing or {},
                        market_caps=caps or {}, market_caps_missing=caps_missing or {},
                        labels=labels or {}, splits=splits or {}, splits_missing=splits_missing or {},
                        broker_qty=broker_qty or {}, broker_failed=broker_failed or {},
                        broker_option_rows=broker_option_rows or {},
                        broker_history=broker_history or {}, platform_orders=platform_orders or {})


def state(timeframe="today", metric="price", size="exposure", experts=(), labels=()):
    return HeatmapState(timeframe=timeframe, metric=metric, size=size, experts=tuple(experts), labels=tuple(labels))


def tile_of(view, symbol):
    return next(t for t in view.tiles if t.symbol == symbol)


# ---- periods ------------------------------------------------------------------------------------------------

def test_today_on_a_session_day_is_measured_from_the_previous_session_close():
    assert hm.period_start("today", TUESDAY) == TUESDAY
    assert hm.reference_session("today", TUESDAY) == date(2026, 11, 23)


def test_today_on_a_holiday_shows_the_last_session_against_the_one_before():
    assert hm.period_start("today", THANKSGIVING) == date(2026, 11, 25)
    assert hm.reference_session("today", THANKSGIVING) == TUESDAY


def test_today_on_a_saturday_shows_friday_against_the_session_before_it():
    # Saturday 11-28: Friday 11-27 against Wednesday 11-25 (Thursday 11-26 is Thanksgiving)
    assert hm.period_start("today", date(2026, 11, 28)) == date(2026, 11, 27)
    assert hm.reference_session("today", date(2026, 11, 28)) == date(2026, 11, 25)


def test_week_reference_is_the_last_close_before_monday():
    assert hm.period_start("week", THANKSGIVING) == date(2026, 11, 23)
    assert hm.reference_session("week", THANKSGIVING) == date(2026, 11, 20)


def test_week_whose_monday_is_a_holiday_still_starts_on_monday():
    # Labor Day 2026-09-07: the week's reference is Friday 09-04's close
    assert hm.reference_session("week", date(2026, 9, 9)) == date(2026, 9, 4)


def test_month_reference_is_the_last_close_before_the_first():
    assert hm.period_start("month", THANKSGIVING) == date(2026, 11, 1)
    assert hm.reference_session("month", THANKSGIVING) == date(2026, 10, 30)


def test_year_reference_is_the_last_close_before_january_first():
    assert hm.reference_session("year", THANKSGIVING) == date(2025, 12, 31)


def test_history_start_covers_every_timeframe():
    assert hm.history_start(THANKSGIVING) == date(2025, 12, 31)
    # early January: the week can start in December, before the year's reference
    assert hm.history_start(date(2027, 1, 1)) <= hm.reference_session("week", date(2027, 1, 1))


def test_unknown_timeframe_is_refused():
    with pytest.raises(ValueError):
        hm.period_start("quarter", THANKSGIVING)


# ---- price change -------------------------------------------------------------------------------------------

def test_price_change_is_current_price_over_reference_close():
    view = hm.build_view([lot()], market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-24": 100.0}}),
                         state(), THANKSGIVING)
    t = tile_of(view, "AAPL")
    assert t.price_change_pct == pytest.approx(10.0)
    assert t.color_pct == pytest.approx(10.0)
    assert t.reference_close == 100.0 and t.current_price == 110.0


def test_reference_close_is_read_on_the_reference_session_never_a_later_bar():
    closes = {"AAPL": {"2026-11-24": 100.0, "2026-11-25": 200.0}}
    view = hm.build_view([lot()], market(prices={"AAPL": 110.0}, closes=closes), state(), THANKSGIVING)
    assert tile_of(view, "AAPL").reference_close == 100.0


def test_missing_reference_close_is_an_na_tile_listed_with_its_reason_not_zero():
    view = hm.build_view([lot()], market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-20": 99.0}}),
                         state(), THANKSGIVING)
    t = tile_of(view, "AAPL")
    assert t.color_pct is None and t.price_change_pct is None
    assert t.label_text.endswith("n/a")
    assert any(sym == "AAPL" and "2026-11-24" in why for sym, why in view.issues)


def test_missing_current_price_is_an_na_tile_with_the_price_reason():
    view = hm.build_view([lot()], market(closes={"AAPL": {"2026-11-24": 100.0}},
                                         price_missing={"AAPL": "broker down"}), state(), THANKSGIVING)
    t = tile_of(view, "AAPL")
    assert t.color_pct is None and t.current_price is None
    assert ("AAPL", "no current price: broker down") in view.issues


def test_history_failure_reason_is_carried_into_the_issue():
    view = hm.build_view([lot()], market(prices={"AAPL": 1.0}, closes_missing={"AAPL": "ConnectionError: x"}),
                         state(), THANKSGIVING)
    assert any("ConnectionError: x" in why for sym, why in view.issues if sym == "AAPL")


# ---- my P/L -------------------------------------------------------------------------------------------------

def test_pnl_of_a_position_held_through_the_period_is_quantity_times_the_move():
    m = market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-20": 100.0}})
    old = fill("BUY", 10.0, 90.0, date(2026, 10, 1))
    t = tile_of(hm.build_view([lot(fills=[old])], m, state("week", "pnl"), THANKSGIVING), "AAPL")
    assert t.pnl_amount == pytest.approx(100.0)
    assert t.pnl_pct == pytest.approx(10.0)
    assert t.color_pct == pytest.approx(10.0)


def test_pnl_of_a_position_opened_inside_the_period_is_measured_from_its_own_fill():
    m = market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-20": 50.0}})
    buy = fill("BUY", 10.0, 100.0, date(2026, 11, 24))
    t = tile_of(hm.build_view([lot(fills=[buy])], m, state("week", "pnl"), THANKSGIVING), "AAPL")
    assert t.pnl_amount == pytest.approx(100.0)          # 1100 - 1000, not 1100 - 500
    assert t.pnl_pct == pytest.approx(10.0)


def test_pnl_opened_inside_the_period_needs_no_reference_close():
    buy = fill("BUY", 10.0, 100.0, date(2026, 11, 24))
    m = market(prices={"AAPL": 110.0}, closes={})
    t = tile_of(hm.build_view([lot(fills=[buy])], m, state("week", "pnl"), THANKSGIVING), "AAPL")
    assert t.pnl_amount == pytest.approx(100.0)
    assert t.price_change_pct is None


def test_pnl_counts_a_top_up_and_a_partial_sell_inside_the_period():
    # held 10 from before; bought 5 @ 104 this week; sold 3 @ 108 this week; now holds 12 @ 110; ref 100
    fills = [fill("BUY", 10.0, 90.0, date(2026, 10, 1)), fill("BUY", 5.0, 104.0, date(2026, 11, 23)),
             fill("SELL", 3.0, 108.0, date(2026, 11, 24))]
    m = market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-20": 100.0}})
    t = tile_of(hm.build_view([lot(qty=12.0, fills=fills)], m, state("week", "pnl"), THANKSGIVING), "AAPL")
    # 12*110 - 10*100 - 5*104 + 3*108 = 1320 - 1000 - 520 + 324 = 124
    assert t.pnl_amount == pytest.approx(124.0)
    assert t.pnl_pct == pytest.approx(124.0 / (1000.0 + 520.0) * 100)


def test_short_pnl_is_sign_correct():
    # short 10 since before the week at ref 100, now 90: +100
    m = market(prices={"TSLA": 90.0}, closes={"TSLA": {"2026-11-20": 100.0}})
    old = fill("SELL", 10.0, 120.0, date(2026, 10, 1))
    t = tile_of(hm.build_view([lot("TSLA", direction="short", fills=[old])], m, state("week", "pnl"),
                              THANKSGIVING), "TSLA")
    assert t.pnl_amount == pytest.approx(100.0)
    assert t.pnl_pct == pytest.approx(10.0)
    assert t.quantity == pytest.approx(-10.0)
    assert t.price_change_pct == pytest.approx(-10.0)      # the instrument fell, whatever the side


def test_short_opened_inside_the_period_is_measured_from_its_sale():
    sale = fill("SELL", 10.0, 100.0, date(2026, 11, 24))
    m = market(prices={"TSLA": 110.0}, closes={"TSLA": {"2026-11-20": 80.0}})
    t = tile_of(hm.build_view([lot("TSLA", direction="short", fills=[sale])], m, state("week", "pnl"),
                              THANKSGIVING), "TSLA")
    assert t.pnl_amount == pytest.approx(-100.0)
    assert t.pnl_pct == pytest.approx(-10.0)


def test_pnl_is_na_when_the_fills_do_not_reconcile_with_the_open_quantity():
    # the fill history says 15 shares, the transaction holds 10: the in-period fills cannot be trusted
    fills = [fill("BUY", 10.0, 90.0, date(2026, 10, 1)), fill("BUY", 5.0, 104.0, date(2026, 11, 23))]
    m = market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-20": 100.0}})
    view = hm.build_view([lot(qty=10.0, fills=fills)], m, state("week", "pnl"), THANKSGIVING)
    t = tile_of(view, "AAPL")
    assert t.pnl_amount is None and t.color_pct is None
    assert any(sym == "AAPL" and "reconcile" in why for sym, why in view.issues)


def test_pnl_is_na_when_a_fill_inside_the_period_has_no_price():
    fills = [fill("BUY", 10.0, None, date(2026, 11, 24))]
    m = market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-20": 100.0}})
    view = hm.build_view([lot(fills=fills)], m, state("week", "pnl"), THANKSGIVING)
    assert tile_of(view, "AAPL").pnl_amount is None
    assert any("no fill price" in why for _s, why in view.issues)


def test_pnl_issue_does_not_appear_in_price_mode_but_the_tooltip_still_says_na():
    fills = [fill("BUY", 10.0, None, date(2026, 11, 24))]
    m = market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-24": 100.0}})
    view = hm.build_view([lot(fills=fills)], m, state("today", "price"), THANKSGIVING)
    assert tile_of(view, "AAPL").color_pct == pytest.approx(10.0)
    assert view.issues == []


# ---- aggregation and sizes ----------------------------------------------------------------------------------

def test_lots_of_one_symbol_are_aggregated_into_one_tile():
    m = market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-24": 100.0}})
    view = hm.build_view([lot(qty=10.0, tx=1), lot(qty=5.0, tx=2, expert="Beta-2")], m, state(), THANKSGIVING)
    assert len(view.tiles) == 1
    t = view.tiles[0]
    assert t.quantity == pytest.approx(15.0)
    assert t.market_value == pytest.approx(1650.0)
    assert t.experts == ("Alpha-1", "Beta-2")


def test_exposure_size_is_position_value():
    m = market(prices={"AAPL": 110.0, "MSFT": 10.0}, closes={}, caps={"AAPL": 1e12, "MSFT": 3e12})
    view = hm.build_view([lot(), lot("MSFT", qty=1.0, tx=2)], m, state(size="exposure"), THANKSGIVING)
    assert tile_of(view, "AAPL").size == pytest.approx(1100.0)
    assert tile_of(view, "MSFT").size == pytest.approx(10.0)


def test_company_size_is_market_cap():
    m = market(prices={"AAPL": 110.0, "MSFT": 10.0}, caps={"AAPL": 1e12, "MSFT": 3e12})
    view = hm.build_view([lot(), lot("MSFT", qty=1.0, tx=2)], m, state(size="company"), THANKSGIVING)
    assert tile_of(view, "AAPL").size == pytest.approx(1e12)
    assert tile_of(view, "MSFT").size == pytest.approx(3e12)
    assert tile_of(view, "AAPL").market_value == pytest.approx(1100.0)     # the tooltip still has both


def test_missing_market_cap_gets_a_small_minimum_tile_and_is_listed():
    m = market(prices={"AAPL": 110.0, "SPY": 500.0}, caps={"AAPL": 1e12},
               caps_missing={"SPY": "FMP has no market cap"})
    view = hm.build_view([lot(), lot("SPY", qty=1.0, tx=2)], m, state(size="company"), THANKSGIVING)
    spy = tile_of(view, "SPY")
    assert spy.size_known is False
    assert 0 < spy.size < 1e12
    assert spy.size == pytest.approx(hm.MIN_TILE_SHARE * 1e12)
    assert ("SPY", "no market cap: FMP has no market cap") in view.issues


def test_missing_price_in_exposure_mode_gets_a_minimum_tile():
    m = market(prices={"AAPL": 110.0}, price_missing={"ZZZ": "not quoted"})
    view = hm.build_view([lot(), lot("ZZZ", qty=1.0, tx=2)], m, state(size="exposure"), THANKSGIVING)
    z = tile_of(view, "ZZZ")
    assert z.size_known is False and z.size > 0


def test_every_size_unknown_still_draws_equal_tiles():
    view = hm.build_view([lot(), lot("MSFT", tx=2)], market(), state(size="company"), THANKSGIVING)
    assert [t.size for t in view.tiles] == [1.0, 1.0]


def test_option_lots_are_excluded_and_counted():
    m = market(prices={"AAPL": 110.0})
    view = hm.build_view([lot(), lot("AAPL", tx=2, option=True), lot("MSFT", tx=3, option=True)], m,
                         state(), THANKSGIVING)
    assert [t.symbol for t in view.tiles] == ["AAPL"]
    assert view.options_excluded == 2
    assert "2 option position" in hm.caption(view)


def test_a_lot_without_an_open_quantity_is_listed_not_drawn():
    view = hm.build_view([lot(qty=None), lot("MSFT", tx=2, qty=0.0)], market(), state(), THANKSGIVING)
    assert view.tiles == []
    assert {s for s, _ in view.issues} == {"AAPL", "MSFT"}


# ---- filters ------------------------------------------------------------------------------------------------

def _filter_world():
    lots = [lot("AAPL", expert="Alpha-1", tx=1), lot("MSFT", expert=NO_EXPERT, tx=2),
            lot("XOM", expert="Beta-2", tx=3)]
    m = market(prices={"AAPL": 1.0, "MSFT": 1.0, "XOM": 1.0},
               labels={"AAPL": ["tech"], "MSFT": ["tech", "core"], "XOM": []})
    return lots, m


def test_available_experts_include_no_expert_last():
    lots, _ = _filter_world()
    assert hm.available_experts(lots) == ["Alpha-1", "Beta-2", NO_EXPERT]


def test_available_labels_include_unlabeled():
    lots, m = _filter_world()
    assert hm.available_labels(lots, m.labels) == ["core", "tech", UNLABELED]


def test_expert_filter_keeps_only_the_chosen_experts_lots():
    lots, m = _filter_world()
    view = hm.build_view(lots, m, state(experts=[NO_EXPERT]), THANKSGIVING)
    assert [t.symbol for t in view.tiles] == ["MSFT"]


def test_label_filter_keeps_symbols_carrying_any_chosen_label():
    lots, m = _filter_world()
    assert sorted(t.symbol for t in hm.build_view(lots, m, state(labels=["tech"]), THANKSGIVING).tiles) == \
        ["AAPL", "MSFT"]
    assert [t.symbol for t in hm.build_view(lots, m, state(labels=[UNLABELED]), THANKSGIVING).tiles] == ["XOM"]


def test_empty_filters_mean_all():
    lots, m = _filter_world()
    assert len(hm.build_view(lots, m, state(), THANKSGIVING).tiles) == 3


def test_expert_filter_also_scopes_a_shared_symbol_to_that_experts_quantity():
    m = market(prices={"AAPL": 10.0})
    view = hm.build_view([lot(qty=10.0, tx=1), lot(qty=5.0, tx=2, expert="Beta-2")], m,
                         state(experts=["Beta-2"]), THANKSGIVING)
    assert view.tiles[0].quantity == pytest.approx(5.0)


# ---- colour ------------------------------------------------------------------------------------------------

def test_colour_is_neutral_at_zero_and_saturates_at_the_clamp():
    clamp = hm.CLAMP_PCT["today"]
    assert hm.color_for(0.0, clamp) == hm.NEUTRAL
    assert hm.color_for(clamp, clamp) == hm.GREEN
    assert hm.color_for(-clamp, clamp) == hm.RED
    assert hm.color_for(10 * clamp, clamp) == hm.GREEN          # an outlier does not stretch the scale
    assert hm.color_for(None, clamp) == hm.NA_COLOR


def test_colour_between_neutral_and_the_pole_is_interpolated():
    half = hm.color_for(1.5, 3.0)
    assert half not in (hm.NEUTRAL, hm.GREEN)


def test_clamp_widens_with_the_timeframe():
    c = hm.CLAMP_PCT
    assert c["today"] < c["week"] < c["month"] < c["year"]


# ---- remembered choices ------------------------------------------------------------------------------------

def test_nothing_stored_gives_the_defaults():
    s = hm.resolve_state(None, ["Alpha-1"], ["tech"])
    assert (s.timeframe, s.metric, s.size, s.experts, s.labels) == ("today", "price", "company", (), ())


def test_stored_choices_are_restored():
    stored = {"timeframe": "month", "metric": "pnl", "size": "exposure", "experts": ["Alpha-1"], "labels": ["tech"]}
    s = hm.resolve_state(stored, ["Alpha-1", NO_EXPERT], ["tech", "core"])
    assert (s.timeframe, s.metric, s.size, s.experts, s.labels) == ("month", "pnl", "exposure", ("Alpha-1",), ("tech",))


def test_a_stored_expert_that_no_longer_exists_is_dropped_and_the_rest_kept():
    s = hm.resolve_state({"experts": ["Gone-9", "Alpha-1"]}, ["Alpha-1"], [])
    assert s.experts == ("Alpha-1",)


def test_a_stored_selection_with_nothing_left_falls_back_to_all():
    s = hm.resolve_state({"experts": ["Gone-9"], "labels": ["gone"]}, ["Alpha-1"], ["tech"])
    assert s.experts == () and s.labels == ()


def test_garbage_stored_values_fall_back_to_defaults_without_raising():
    s = hm.resolve_state({"timeframe": "decade", "metric": 3, "size": None, "experts": "Alpha-1", "labels": {}},
                         ["Alpha-1"], ["tech"])
    assert (s.timeframe, s.metric, s.size, s.experts, s.labels) == ("today", "price", "company", (), ())
    assert hm.resolve_state("not a dict", [], []).timeframe == "today"


def test_state_round_trips_through_storage():
    s = state("year", "pnl", "company", ["Alpha-1"], ["tech"])
    assert hm.resolve_state(hm.state_to_storage(s), ["Alpha-1"], ["tech"]) == s


def test_saving_keeps_a_stored_expert_this_account_does_not_have():
    # chose X on account A; on account B (no X) the timeframe changes: X stays stored for A
    previous = {"experts": ["X-1"], "labels": ["gone"]}
    saved = hm.merge_for_storage(state("week", experts=["Y-2"]), previous, ["Y-2", "Z-3"], ["tech"])
    assert saved["timeframe"] == "week"
    assert saved["experts"] == ["Y-2", "X-1"]
    assert saved["labels"] == ["gone"]


def test_saving_drops_an_available_expert_the_user_unticked():
    saved = hm.merge_for_storage(state(experts=[]), {"experts": ["Y-2"]}, ["Y-2"], [])
    assert saved["experts"] == []


def test_saving_over_garbage_storage_writes_a_clean_state():
    assert hm.merge_for_storage(state(), "junk", [], [])["experts"] == []


def test_storage_key_is_namespaced_and_per_port():
    assert hm.storage_key(8080) == "overview_heatmap_state:8080"
    assert hm.storage_key(None) == "overview_heatmap_state"


# ---- chart options and caption -----------------------------------------------------------------------------

def test_chart_options_are_a_treemap_with_label_and_tooltip_per_tile():
    m = market(prices={"AAPL": 110.0}, closes={"AAPL": {"2026-11-24": 100.0}}, caps={"AAPL": 2e12})
    view = hm.build_view([lot()], m, state(size="company"), THANKSGIVING)
    opts = hm.chart_options(view)
    series = opts["series"][0]
    assert series["type"] == "treemap"
    node = series["data"][0]
    assert node["name"] == "AAPL" and node["value"] == pytest.approx(2e12)
    assert "+10.00%" in node["lbl"]
    for piece in ("AAPL", "2.00T", "$1,100.00", "10", "100.00", "110.00", "+10.00%"):
        assert piece in node["tip"]
    assert node["itemStyle"]["color"] == hm.GREEN


def test_tooltip_escapes_html_in_names():
    m = market(prices={"AAPL": 1.0}, labels={"AAPL": ["<b>x</b>"]})
    view = hm.build_view([lot(expert="<script>")], m, state(), THANKSGIVING)
    tip = hm.chart_options(view)["series"][0]["data"][0]["tip"]
    assert "<script>" not in tip and "&lt;script&gt;" in tip


def test_unknown_size_tile_carries_a_neutral_border():
    m = market(prices={"AAPL": 1.0, "SPY": 1.0}, caps={"AAPL": 1e9})
    view = hm.build_view([lot(), lot("SPY", tx=2)], m, state(size="company"), THANKSGIVING)
    spy = next(n for n in hm.chart_options(view)["series"][0]["data"] if n["name"] == "SPY")
    assert spy["itemStyle"]["borderColor"] == hm.UNKNOWN_SIZE_BORDER


def test_caption_names_size_mode_metric_period_and_reference():
    view = hm.build_view([lot()], market(prices={"AAPL": 1.0}), state("week", "pnl", "company"), THANKSGIVING)
    text = hm.caption(view)
    for piece in ("company market cap", "My P/L", "This week", "2026-11-20", "6%"):
        assert piece in text


# ---- C1: when did a fill happen? ---------------------------------------------------------------------------
# Tuesday 2026-11-24, 10:00 ET: the session is open.
TUE_OPEN = utc(2026, 11, 24, 15)


def _stop_leg_world():
    # bought 10 (market) on 10-01; a STOP leg placed last week (11-18) sold 4 at 95 -- the platform does not know
    # when; 6 still held, now 100; closes: Monday 11-23 = 98, 2025-12-31 = 80
    fills = [fill("BUY", 10.0, 90.0, date(2026, 10, 1)), fill("SELL", 4.0, 95.0, date(2026, 11, 18), market=False)]
    m = market(prices={"AAPL": 100.0}, closes={"AAPL": {"2026-11-23": 98.0, "2025-12-31": 80.0}})
    return [lot(qty=6.0, fills=fills)], m


def test_a_stop_leg_placed_before_the_period_makes_pnl_unknown_not_a_guess():
    lots, m = _stop_leg_world()
    view = hm.build_view(lots, m, state("today", "pnl"), TUE_OPEN)
    t = tile_of(view, "AAPL")
    assert t.pnl_amount is None and t.color_pct is None
    assert any("placed before the period filled at an unknown date" in why for _s, why in view.issues)


def test_the_same_stop_leg_is_inside_the_year_because_it_was_placed_inside_it():
    lots, m = _stop_leg_world()
    t = tile_of(hm.build_view(lots, m, state("year", "pnl"), TUE_OPEN), "AAPL")
    # q_start = 6 - (10 - 4) = 0: measured from its own fills -> 6*100 - 10*90 + 4*95 = 80
    assert t.pnl_amount == pytest.approx(80.0)
    assert t.pnl_pct == pytest.approx(80.0 / 900.0 * 100)


def test_a_market_sell_inside_the_period_stays_computed():
    fills = [fill("BUY", 10.0, 90.0, date(2026, 10, 1)), fill("SELL", 4.0, 99.0, date(2026, 11, 24))]
    m = market(prices={"AAPL": 100.0}, closes={"AAPL": {"2026-11-23": 98.0}})
    t = tile_of(hm.build_view([lot(qty=6.0, fills=fills)], m, state("today", "pnl"), TUE_OPEN), "AAPL")
    # 6*100 - 10*98 + 4*99 = 16
    assert t.pnl_amount == pytest.approx(16.0)


def test_a_market_order_placed_on_the_weekend_fills_at_the_next_open():
    # placed Saturday 11-21 (before the week), fills Monday 11-23 (inside the week)
    fills = [fill("BUY", 10.0, 95.0, date(2026, 11, 21))]
    m = market(prices={"AAPL": 100.0}, closes={"AAPL": {"2026-11-20": 50.0}})
    t = tile_of(hm.build_view([lot(fills=fills)], m, state("week", "pnl"), TUE_OPEN), "AAPL")
    assert t.pnl_amount == pytest.approx(50.0)                 # from the fill, not the reference close


def test_a_limit_entry_seen_filled_before_the_period_is_known_to_be_before_it():
    seen = utc(2026, 11, 19)
    fills = [fill("BUY", 10.0, 90.0, date(2026, 11, 2), market=False, seen_by=seen)]
    m = market(prices={"AAPL": 100.0}, closes={"AAPL": {"2026-11-20": 96.0}})
    t = tile_of(hm.build_view([lot(fills=fills)], m, state("week", "pnl"), TUE_OPEN), "AAPL")
    assert t.pnl_amount == pytest.approx(40.0)


def test_fills_are_reconciled_even_when_none_is_inside_the_period():
    fills = [fill("BUY", 15.0, 90.0, date(2026, 10, 1))]          # 15 recorded, 10 open
    m = market(prices={"AAPL": 100.0}, closes={"AAPL": {"2026-11-23": 98.0}})
    view = hm.build_view([lot(qty=10.0, fills=fills)], m, state("today", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAPL").pnl_amount is None
    assert any("reconcile" in why for _s, why in view.issues)


# ---- I3: splits --------------------------------------------------------------------------------------------

def test_a_split_inside_the_period_makes_both_metrics_na():
    fills = [fill("BUY", 10.0, 90.0, date(2026, 10, 1))]
    m = market(prices={"AAPL": 50.0}, closes={"AAPL": {"2026-10-30": 100.0}}, splits={"AAPL": [date(2026, 11, 10)]})
    for metric in ("price", "pnl"):
        view = hm.build_view([lot(fills=fills)], m, state("month", metric), TUE_OPEN)
        assert tile_of(view, "AAPL").color_pct is None
        assert any("split on 2026-11-10" in why for _s, why in view.issues)


def test_a_split_before_the_period_does_not_matter():
    m = market(prices={"AAPL": 50.0}, closes={"AAPL": {"2026-11-23": 49.0}}, splits={"AAPL": [date(2026, 11, 10)]})
    view = hm.build_view([lot()], m, state("today", "price"), TUE_OPEN)
    assert tile_of(view, "AAPL").color_pct == pytest.approx((50 / 49 - 1) * 100)


def test_an_unknown_split_calendar_is_listed_but_does_not_hide_the_number():
    m = market(prices={"AAPL": 50.0}, closes={"AAPL": {"2026-11-23": 49.0}}, splits_missing={"AAPL": "FMP down"})
    view = hm.build_view([lot()], m, state("today", "price"), TUE_OPEN)
    assert tile_of(view, "AAPL").color_pct is not None
    assert ("AAPL", "not checked for splits: FMP down") in view.issues


# ---- I4: "Today" before the open --------------------------------------------------------------------------

def test_today_before_the_open_shows_the_last_completed_session():
    pre_open = utc(2026, 11, 24, 13)                              # 08:00 ET Tuesday
    assert hm.period_start("today", pre_open) == date(2026, 11, 23)
    assert hm.reference_session("today", pre_open) == date(2026, 11, 20)
    view = hm.build_view([lot()], market(prices={"AAPL": 1.0}), state("today"), pre_open)
    text = hm.caption(view)
    assert "not opened yet" in text and "2026-11-23" in text


def test_today_during_the_session_is_today():
    assert hm.period_start("today", TUE_OPEN) == date(2026, 11, 24)
    assert "in progress" in hm.caption(hm.build_view([lot()], market(prices={"AAPL": 1.0}), state(), TUE_OPEN))


def test_a_naive_moment_is_refused():
    with pytest.raises(ValueError):
        hm.period_start("today", datetime(2026, 11, 24, 10))


# ---- I5: platform transactions vs the broker's book ---------------------------------------------------------

def test_caption_says_tiles_are_what_the_broker_holds():
    text = hm.caption(hm.build_view([lot()], market(broker_qty={1: {"AAPL": 10.0}}), state(), TUE_OPEN))
    assert "held at the broker" in text and "expert split from the platform's records" in text


def test_platform_and_broker_quantities_that_differ_are_listed():
    lots = [lot("AAPL", qty=10.0, tx=1), lot("AAPL", qty=5.0, tx=2, expert="Beta-2"), lot("XOM", qty=3.0, tx=3)]
    m = market(broker_qty={1: {"AAPL": 12.0, "XOM": 3.0, "MSFT": 5.0}})
    view = hm.build_view(lots, m, state(experts=["Beta-2"]), TUE_OPEN)
    assert view.quantity_mismatches == [("AAPL", 1, 15.0, 12.0), ("MSFT", 1, 0.0, 5.0)]


def test_no_comparison_for_an_account_whose_broker_book_was_not_read():
    view = hm.build_view([lot()], market(broker_qty={}), state(), TUE_OPEN)
    assert view.quantity_mismatches == []


# ---- M1 / M2 / M3 ------------------------------------------------------------------------------------------

def test_non_finite_inputs_never_reach_the_chart():
    m = market(prices={"AAPL": math.inf, "MSFT": 10.0, "XOM": 5.0}, caps={"MSFT": math.nan, "XOM": 1e9},
               closes={"XOM": {"2026-11-23": math.inf}})
    lots = [lot(), lot("MSFT", tx=2), lot("XOM", tx=3), lot("NAN", qty=math.nan, tx=4)]
    view = hm.build_view(lots, m, state(size="company"), TUE_OPEN)
    json.dumps(hm.chart_options(view), allow_nan=False)
    assert tile_of(view, "AAPL").current_price is None
    assert tile_of(view, "MSFT").market_cap is None
    assert tile_of(view, "XOM").reference_close is None
    assert any(s == "NAN" for s, _ in view.issues)


def test_rich_text_characters_in_a_symbol_cannot_break_the_tile_label():
    view = hm.build_view([lot("A{B|C}")], market(prices={"A{B|C}": 1.0}), state(), TUE_OPEN)
    lbl = hm.chart_options(view)["series"][0]["data"][0]["lbl"]
    assert lbl.count("{") == 2 and lbl.count("}") == 2 and lbl.count("|") == 2


def test_dropped_filters_are_named():
    stored = {"experts": ["Gone-9", "Alpha-1"], "labels": ["gone"]}
    assert hm.dropped_filters(stored, ["Alpha-1"], ["tech"]) == (["Gone-9"], ["gone"])
    assert hm.dropped_filters(None, [], []) == ([], [])


# ---- the broker's position is the truth ------------------------------------------------------------------------
# The owner's live case: AAOI is 10.6809 shares at the broker, the platform's transactions hold 1.38986.

AAOI_BROKER, AAOI_PLATFORM = 10.6809, 1.38986


def _aaoi(**extra):
    lots = [lot("AAOI", qty=AAOI_PLATFORM, expert="Alpha-1", account=2,
                fills=[fill("BUY", AAOI_PLATFORM, 30.0, date(2026, 10, 1))])]
    m = market(prices={"AAOI": 38.0, "GDXY": 15.0}, closes={"AAOI": {"2026-11-23": 36.0}},
               broker_qty={2: {"AAOI": AAOI_BROKER, "GDXY": 20.6593}}, **extra)
    return lots, m


def test_all_experts_tile_uses_the_brokers_quantity():
    lots, m = _aaoi()
    t = tile_of(hm.build_view(lots, m, state(size="exposure"), TUE_OPEN), "AAOI")
    assert t.quantity == pytest.approx(AAOI_BROKER)
    assert t.market_value == pytest.approx(AAOI_BROKER * 38.0)
    assert NO_EXPERT in t.experts and "Alpha-1" in t.experts


def test_no_expert_is_the_brokers_quantity_minus_the_experts_share():
    lots, m = _aaoi()
    t = tile_of(hm.build_view(lots, m, state(experts=[NO_EXPERT]), TUE_OPEN), "AAOI")
    assert t.quantity == pytest.approx(AAOI_BROKER - AAOI_PLATFORM)


def test_a_named_expert_keeps_its_platform_quantity():
    lots, m = _aaoi()
    t = tile_of(hm.build_view(lots, m, state(experts=["Alpha-1"]), TUE_OPEN), "AAOI")
    assert t.quantity == pytest.approx(AAOI_PLATFORM)


def test_a_symbol_only_the_broker_holds_gets_a_tile_and_belongs_to_no_expert():
    lots, m = _aaoi()
    assert tile_of(hm.build_view(lots, m, state(), TUE_OPEN), "GDXY").quantity == pytest.approx(20.6593)
    assert [t.symbol for t in hm.build_view(lots, m, state(experts=["Alpha-1"]), TUE_OPEN).tiles] == ["AAOI"]
    assert "GDXY" in [t.symbol for t in hm.build_view(lots, m, state(experts=[NO_EXPERT]), TUE_OPEN).tiles]
    assert hm.available_experts(lots, m) == ["Alpha-1", NO_EXPERT]
    assert hm.available_labels(lots, {}, m) == [UNLABELED]


def test_a_symbol_open_on_the_platform_but_not_held_at_the_broker_gets_no_tile_and_is_listed():
    lots = [lot("XYZ", qty=5.0, account=2)]
    view = hm.build_view(lots, market(prices={"XYZ": 1.0}, broker_qty={2: {}}), state(), TUE_OPEN)
    assert view.tiles == []
    assert any(sym == "XYZ" and "open on the platform" in why and "not held at the broker" in why
               for sym, why in view.issues)


def test_experts_records_exceeding_the_broker_are_listed_and_never_clamped():
    lots = [lot("AAOI", qty=8.0, expert="Alpha-1", account=2)]
    m = market(prices={"AAOI": 10.0}, broker_qty={2: {"AAOI": 5.0}})
    whole = hm.build_view(lots, m, state(), TUE_OPEN)
    assert tile_of(whole, "AAOI").quantity == pytest.approx(5.0)
    assert any("experts' records exceed the broker's position (platform 8, broker 5)" in why
               for _s, why in whole.issues)
    assert hm.build_view(lots, m, state(experts=[NO_EXPERT]), TUE_OPEN).tiles == []
    assert tile_of(hm.build_view(lots, m, state(experts=["Alpha-1"]), TUE_OPEN), "AAOI").quantity == 8.0


def test_a_failed_positions_read_falls_back_to_platform_quantities_and_says_so():
    lots, _m = _aaoi()
    m = market(prices={"AAOI": 38.0}, broker_failed={2: "account 2 timed out"})
    view = hm.build_view(lots, m, state(), TUE_OPEN)
    assert tile_of(view, "AAOI").quantity == pytest.approx(AAOI_PLATFORM)
    assert "account 2" in hm.caption(view) and "platform's transaction quantities" in hm.caption(view)


def test_option_rows_in_the_broker_book_are_excluded_and_counted():
    lots, m = _aaoi(broker_option_rows={2: 3})
    view = hm.build_view(lots, m, state(), TUE_OPEN)
    assert view.options_excluded == 3 and "3 option positions excluded" in hm.caption(view)


def test_the_label_filter_applies_to_broker_only_symbols():
    lots, _m = _aaoi()
    m = market(prices={"AAOI": 38.0, "GDXY": 15.0}, broker_qty={2: {"AAOI": AAOI_BROKER, "GDXY": 20.6593}},
               labels={"AAOI": ["tech"], "GDXY": ["gold"]})
    assert [t.symbol for t in hm.build_view(lots, m, state(labels=["gold"]), TUE_OPEN).tiles] == ["GDXY"]


def test_pnl_is_never_taken_from_anything_but_the_platforms_fills():
    # the broker bought 4 this week that the platform never saw: its records cover 1 of the broker's 10 -> n/a
    m = market(prices={"AAOI": 110.0}, closes={"AAOI": {"2026-11-20": 100.0}}, broker_qty={2: {"AAOI": 10.0}})
    lots = [lot("AAOI", qty=1.0, account=2, fills=[fill("BUY", 1.0, 90.0, date(2026, 10, 1))])]
    view = hm.build_view(lots, m, state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None
    assert any("platform records cover 1 of the broker's 10 shares" in why for _s, why in view.issues)
    assert not hasattr(hm, "BrokerFill")


def test_pnl_needs_the_platform_to_cover_the_brokers_position():
    lots, m = _aaoi()
    view = hm.build_view(lots, m, state("today", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None
    assert any("platform records cover 1.38986 of the broker's 10.6809 shares" in why for _s, why in view.issues)


def test_pnl_uses_the_platform_fills_when_they_cover_the_brokers_position():
    lots = [lot("AAOI", qty=10.0, account=2, fills=[fill("BUY", 10.0, 90.0, date(2026, 10, 1))])]
    m = market(prices={"AAOI": 110.0}, closes={"AAOI": {"2026-11-23": 100.0}}, broker_qty={2: {"AAOI": 10.0}})
    assert tile_of(hm.build_view(lots, m, state("today", "pnl"), TUE_OPEN), "AAOI").pnl_amount == pytest.approx(100.0)


def test_coverage_holds_within_the_tolerance_and_fails_beyond_it():
    def view_for(platform_qty):
        lots = [lot("AAOI", qty=platform_qty, account=2, fills=[fill("BUY", platform_qty, 90.0, date(2026, 10, 1))])]
        m = market(prices={"AAOI": 110.0}, closes={"AAOI": {"2026-11-23": 100.0}}, broker_qty={2: {"AAOI": 10.0}})
        return hm.build_view(lots, m, state("today", "pnl"), TUE_OPEN)
    assert tile_of(view_for(10.0005), "AAOI").pnl_amount is not None          # 0.005% off: fractional rounding
    off = view_for(10.01)                                                       # 0.1% off: another fill exists
    assert tile_of(off, "AAOI").pnl_amount is None
    assert any("platform records cover 10.01 of the broker's 10 shares" in why for _s, why in off.issues)


def test_with_an_expert_filter_pnl_stays_on_that_experts_platform_fills():
    lots = [lot("AAOI", qty=2.0, expert="Alpha-1", account=2, fills=[fill("BUY", 2.0, 90.0, date(2026, 10, 1))])]
    m = market(prices={"AAOI": 110.0}, closes={"AAOI": {"2026-11-23": 100.0}}, broker_qty={2: {"AAOI": 10.0}})
    t = tile_of(hm.build_view(lots, m, state("today", "pnl", experts=["Alpha-1"]), TUE_OPEN), "AAOI")
    assert t.pnl_amount == pytest.approx(20.0)


# ---- review fixes: orphans, mixed accounts, signs, dust, splits, caption ------------------------------------

def test_a_transaction_with_no_account_is_never_added_to_a_symbol_a_broker_holds():
    lots = [lot("AAOI", qty=3.0, account=None, tx=7)]
    m = market(prices={"AAOI": 10.0}, broker_qty={2: {"AAOI": 10.0}})
    view = hm.build_view(lots, m, state(), TUE_OPEN)
    assert tile_of(view, "AAOI").quantity == pytest.approx(10.0)
    assert ("AAOI", "transaction 7 has no account: not counted") in view.issues


def test_a_long_in_a_failed_account_and_a_short_in_a_read_account_are_not_double_counted():
    lots = [lot("XOM", qty=10.0, account=1, tx=1), lot("XOM", qty=5.0, direction="short", account=2, tx=2)]
    m = market(prices={"XOM": 100.0}, broker_qty={2: {"XOM": -5.0}}, broker_failed={1: "account 1 timed out"})
    view = hm.build_view(lots, m, state(size="exposure"), TUE_OPEN)
    t = tile_of(view, "XOM")
    assert t.quantity == pytest.approx(5.0)                  # +10 (account 1, platform) - 5 (account 2, broker)
    assert t.market_value == pytest.approx(1500.0)           # gross: 15 shares
    assert "Account 1" in hm.caption(view) and "Account 2" not in hm.caption(view)


def test_the_no_expert_remainder_of_a_short_keeps_its_sign():
    lots = [lot("TSLA", qty=3.0, direction="short", expert="Alpha-1", account=2, tx=1),
            lot("TSLA", qty=2.0, direction="short", expert="Beta-2", account=2, tx=2)]
    m = market(prices={"TSLA": 100.0}, broker_qty={2: {"TSLA": -10.0}})
    assert tile_of(hm.build_view(lots, m, state(experts=[NO_EXPERT]), TUE_OPEN), "TSLA").quantity == pytest.approx(-5.0)
    assert tile_of(hm.build_view(lots, m, state(experts=["Alpha-1"]), TUE_OPEN), "TSLA").quantity == pytest.approx(-3.0)
    whole = hm.build_view(lots, m, state(), TUE_OPEN)
    assert tile_of(whole, "TSLA").quantity == pytest.approx(-10.0)
    assert not any("exceed" in why for _s, why in whole.issues)


def test_a_dust_position_gets_no_tile():
    m = market(prices={"AAOI": 10.0, "DUST": 0.01, "TINY": 50.0},
               broker_qty={2: {"AAOI": 10.0, "DUST": 0.5, "TINY": 1e-7}})
    view = hm.build_view([], m, state(), TUE_OPEN)
    assert [t.symbol for t in view.tiles] == ["AAOI"]           # DUST worth half a cent, TINY 1e-7 shares


def test_an_unknown_split_calendar_makes_pnl_na_for_a_period_longer_than_a_session():
    lots = [lot("AAOI", qty=10.0, account=2, fills=[fill("BUY", 10.0, 90.0, date(2026, 10, 1))])]
    m = market(prices={"AAOI": 110.0}, closes={"AAOI": {"2026-11-20": 100.0, "2026-11-23": 105.0}},
               broker_qty={2: {"AAOI": 10.0}}, splits_missing={"AAOI": "FMP down"})
    week = hm.build_view(lots, m, state("week", "pnl"), TUE_OPEN)
    assert tile_of(week, "AAOI").pnl_amount is None
    assert any("split calendar" in why and "My P/L" in why for _s, why in week.issues)
    assert tile_of(hm.build_view(lots, m, state("today", "pnl"), TUE_OPEN), "AAOI").pnl_amount is not None
    price = hm.build_view(lots, m, state("week", "price"), TUE_OPEN)
    assert tile_of(price, "AAOI").color_pct is not None
    assert ("AAOI", "not checked for splits: FMP down") in price.issues


def test_the_pnl_caption_says_dividends_and_fees_are_not_counted():
    view = hm.build_view([lot()], market(prices={"AAPL": 1.0}), state(metric="pnl"), TUE_OPEN)
    assert "dividends and fees" in hm.caption(view)


# ---- "My P/L" from the broker's transaction history (TastyTrade) -----------------------------------------------
# Tuesday 2026-11-24, 10:00 ET. Reference closes: today = Mon 11-23 16:00 ET (21:00 UTC), week = Fri 11-20 21:00 UTC.
from ba2_trade_platform.ui.utils.position_heatmap import BrokerHistory, ShareMove  # noqa: E402

YEAR_START = utc(2025, 12, 31, 5)          # midnight New York, 2025-12-31: the read's first day


def move(qty, price, when, kind="trade", symbol="AAOI", detail=""):
    return ShareMove(symbol=symbol, quantity=qty, price=price, executed_at=when, kind=kind, detail=detail)


def history(*moves, covers_from=YEAR_START, positions_at=TUE_OPEN, failure=None, account=2):
    return {account: BrokerHistory(moves=tuple(moves), covers_from=covers_from, positions_at=positions_at,
                                   failure=failure)}


def _broker_market(hist, qty=10.0, closes=None, prices=None):
    return market(prices=prices or {"AAOI": 110.0}, broker_qty={2: {"AAOI": qty}}, broker_history=hist,
                  closes=closes or {"AAOI": {"2026-11-20": 100.0, "2026-11-23": 100.0, "2026-10-30": 90.0}})


def test_broker_history_pnl_counts_a_buy_and_a_partial_sell_inside_the_period():
    hist = history(move(4.0, 95.0, utc(2026, 11, 23, 15)), move(-1.0, 105.0, utc(2026, 11, 23, 16)))
    view = hm.build_view([], _broker_market(hist), state("week", "pnl"), TUE_OPEN)
    t = tile_of(view, "AAOI")
    # start 10 - 3 = 7 ; 10*110 - 7*100 - 4*95 + 1*105 = 125 ; at risk 700 + 380
    assert t.pnl_amount == pytest.approx(125.0)
    assert t.pnl_pct == pytest.approx(125.0 / 1080.0 * 100)
    assert t.pnl_source == "the broker's transaction history"
    assert "from the broker's transaction history" in hm.tooltip_html(t, view)


def test_the_period_starts_at_the_reference_sessions_close_not_at_midnight():
    after_close = history(move(2.0, 105.0, utc(2026, 11, 23, 21, 30)))       # 16:30 New York on the reference day
    before_close = history(move(2.0, 105.0, utc(2026, 11, 23, 20, 30)))      # 15:30 New York: still the old period
    inside = tile_of(hm.build_view([], _broker_market(after_close), state("today", "pnl"), TUE_OPEN), "AAOI")
    outside = tile_of(hm.build_view([], _broker_market(before_close), state("today", "pnl"), TUE_OPEN), "AAOI")
    assert inside.pnl_amount == pytest.approx(10 * 110 - 8 * 100 - 2 * 105)
    assert outside.pnl_amount == pytest.approx(10 * 110 - 10 * 100)


def test_a_transfer_inside_the_period_makes_pnl_na_and_says_so():
    hist = history(move(5.0, None, utc(2026, 11, 23, 15), kind="transfer", detail="Receive Deliver / ACAT"))
    view = hm.build_view([], _broker_market(hist), state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None
    assert any("transfer" in why and "Receive Deliver / ACAT" in why for _s, why in view.issues)


def test_a_trade_without_a_price_inside_the_period_makes_pnl_na():
    view = hm.build_view([], _broker_market(history(move(1.0, None, utc(2026, 11, 23, 15)))),
                         state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None
    assert any("no price" in why for _s, why in view.issues)


def test_a_history_that_starts_after_the_period_start_makes_pnl_na():
    hist = history(covers_from=utc(2026, 11, 1, 4))
    view = hm.build_view([], _broker_market(hist), state("month", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None
    assert any("does not reach back to the period start" in why for _s, why in view.issues)
    assert tile_of(hm.build_view([], _broker_market(hist), state("week", "pnl"), TUE_OPEN), "AAOI").pnl_amount \
        == pytest.approx(10 * 110 - 10 * 100)


def test_a_movement_newer_than_the_positions_read_makes_pnl_na_reload():
    hist = history(move(1.0, 109.0, TUE_OPEN))                    # filled as the positions were being read
    view = hm.build_view([], _broker_market(hist), state("today", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None
    assert any("position changing right now, reload" in why for _s, why in view.issues)


def test_a_failed_history_read_makes_pnl_na_with_the_failure():
    view = hm.build_view([], _broker_market(history(failure="the transaction history read timed out")),
                         state("today", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None
    assert any("timed out" in why for _s, why in view.issues)


def test_a_start_quantity_on_the_other_side_is_refused():
    hist = history(move(5.0, 100.0, utc(2026, 11, 23, 15)))          # holds 2 now, bought 5: was short 3?
    view = hm.build_view([], _broker_market(hist, qty=2.0), state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None
    assert any("other side" in why for _s, why in view.issues)


def test_the_aaoi_shape_gets_a_number_from_the_brokers_history():
    # pre-existing shares bought long before the read window; the allocator sold 2 (no platform transaction) and
    # bought 1.38986 (its one transaction) this week. The platform covers 1.38986 of 10.6809.
    lots = [lot("AAOI", qty=AAOI_PLATFORM, expert=NO_EXPERT, account=2,
                fills=[fill("BUY", AAOI_PLATFORM, 30.0, date(2026, 11, 23))])]
    hist = history(move(-2.0, 35.0, utc(2026, 11, 23, 15)), move(AAOI_PLATFORM, 30.0, utc(2026, 11, 23, 15, 5)))
    m = _broker_market(hist, qty=AAOI_BROKER, prices={"AAOI": 38.0},
                       closes={"AAOI": {"2026-11-20": 36.0, "2026-10-30": 33.0}})
    for timeframe, ref in (("week", 36.0), ("month", 33.0)):
        t = tile_of(hm.build_view(lots, m, state(timeframe, "pnl"), TUE_OPEN), "AAOI")
        start = AAOI_BROKER - (AAOI_PLATFORM - 2.0)
        assert t.pnl_amount == pytest.approx(AAOI_BROKER * 38.0 - start * ref - AAOI_PLATFORM * 30.0 + 2 * 35.0)
    no_expert = tile_of(hm.build_view(lots, m, state("week", "pnl", experts=[NO_EXPERT]), TUE_OPEN), "AAOI")
    assert no_expert.pnl_source == "the broker's transaction history"


def test_named_experts_stay_on_platform_fills_and_their_presence_keeps_no_expert_on_coverage():
    lots = [lot("AAOI", qty=2.0, expert="Alpha-1", account=2, fills=[fill("BUY", 2.0, 90.0, date(2026, 10, 1))])]
    hist = history(move(1.0, 105.0, utc(2026, 11, 23, 22)))
    m = _broker_market(hist)
    named = tile_of(hm.build_view(lots, m, state("today", "pnl", experts=["Alpha-1"]), TUE_OPEN), "AAOI")
    assert named.pnl_amount == pytest.approx(2 * 110 - 2 * 100) and named.pnl_source == "the platform's fills"
    rest = hm.build_view(lots, m, state("today", "pnl", experts=[NO_EXPERT]), TUE_OPEN)
    assert tile_of(rest, "AAOI").pnl_amount is None
    assert any("platform records cover 0 of the No-expert 8 shares" in why for _s, why in rest.issues)
    whole = tile_of(hm.build_view(lots, m, state("today", "pnl"), TUE_OPEN), "AAOI")
    assert whole.pnl_source == "the broker's transaction history"


def test_an_account_without_a_broker_history_keeps_the_platform_coverage_rule():
    lots = [lot("AAOI", qty=10.0, account=2, fills=[fill("BUY", 10.0, 90.0, date(2026, 10, 1))])]
    m = market(prices={"AAOI": 110.0}, closes={"AAOI": {"2026-11-23": 100.0}}, broker_qty={2: {"AAOI": 10.0}})
    t = tile_of(hm.build_view(lots, m, state("today", "pnl"), TUE_OPEN), "AAOI")
    assert t.pnl_amount == pytest.approx(100.0) and t.pnl_source == "the platform's fills"


# ---- review fixes: the invariant, platform fills, where a refusal lands, reversals ---------------------------------
from ba2_trade_platform.ui.utils.position_heatmap import PlatformOrder  # noqa: E402


def _hist(*moves, covers_from=YEAR_START):
    return {2: BrokerHistory(moves=tuple(moves), covers_from=covers_from, positions_at=TUE_OPEN)}


def _m(rid, qty, price, when, kind="trade", symbol="AAOI", underlying=None, reverses=None, detail="Trade / x"):
    return ShareMove(symbol=symbol, quantity=qty, price=price, executed_at=when, kind=kind, detail=detail, id=rid,
                     underlying=underlying, reverses_id=reverses)


def _two_symbols(hist, qty=12.0, orders=None):
    return market(prices={"AAOI": 110.0, "GDXY": 15.0}, broker_qty={2: {"AAOI": qty, "GDXY": 20.0}},
                  closes={"AAOI": {"2026-11-20": 100.0}, "GDXY": {"2026-11-20": 14.0}}, broker_history=hist,
                  platform_orders=orders or {})


OLD = utc(2026, 10, 1)
INSIDE = utc(2026, 11, 23, 15)


def test_a_platform_fill_missing_from_the_broker_history_refuses_pnl():
    order = {2: [PlatformOrder(symbol="AAOI", side="BUY", quantity=1.38986, created_at=utc(2026, 11, 23, 14, 59))]}
    missing = hm.build_view([], _two_symbols(_hist(), orders=order), state("week", "pnl"), TUE_OPEN)
    assert tile_of(missing, "AAOI").pnl_amount is None
    assert ("AAOI", "a platform fill is not in the broker history yet") in missing.issues
    matched = _hist(_m(7, 1.38986, 30.0, INSIDE))
    view = hm.build_view([], _two_symbols(matched, qty=12.0, orders=order), state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is not None


def test_an_unknown_instrument_row_refuses_its_underlyings_tile_only():
    hist = _hist(_m(8, 1.0, None, INSIDE, kind="other", symbol="AAOI  261218C00040000", underlying="AAOI",
                    detail="unrecognised transaction row (Receive Deliver / Journal)"))
    view = hm.build_view([], _two_symbols(hist), state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None and tile_of(view, "GDXY").pnl_amount is not None


def test_a_row_no_tile_can_hold_refuses_every_tile_of_the_account():
    hist = _hist(_m(9, 3.0, None, INSIDE, kind="other", symbol="ZZZ", detail="unrecognised transaction row (x)"))
    view = hm.build_view([], _two_symbols(hist), state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None and tile_of(view, "GDXY").pnl_amount is None
    assert not any(sym == "ZZZ" for sym, _why in view.issues)


def test_a_corporate_action_refuses_every_tile_for_the_periods_containing_it():
    hist = _hist(_m(10, 10.0, None, utc(2026, 11, 2, 15), kind="split", symbol="OLDCO",
                    detail="a corporate action (Receive Deliver / Symbol Change)"))
    month = hm.build_view([], _two_symbols(hist), state("month", "pnl"), TUE_OPEN)
    assert tile_of(month, "AAOI").pnl_amount is None and tile_of(month, "GDXY").pnl_amount is None
    week = hm.build_view([], _two_symbols(hist), state("week", "pnl"), TUE_OPEN)
    assert tile_of(week, "AAOI").pnl_amount is not None


def test_a_reversal_refuses_the_symbol_for_periods_containing_either_row():
    hist = _hist(_m(3, 2.0, 100.0, utc(2026, 11, 10, 15)), _m(5, -2.0, 100.0, OLD, reverses=3))
    assert tile_of(hm.build_view([], _two_symbols(hist), state("month", "pnl"), TUE_OPEN), "AAOI").pnl_amount is None
    assert tile_of(hm.build_view([], _two_symbols(hist), state("week", "pnl"), TUE_OPEN), "AAOI").pnl_amount \
        is not None


def test_a_platform_order_older_than_the_proven_coverage_is_not_cross_checked():
    order = {2: [PlatformOrder(symbol="AAOI", side="BUY", quantity=1.0, created_at=utc(2026, 11, 19, 15))]}
    hist = _hist(covers_from=utc(2026, 11, 20, 0))
    t = tile_of(hm.build_view([], _two_symbols(hist, orders=order), state("week", "pnl"), TUE_OPEN), "AAOI")
    assert t.pnl_amount is not None


def test_the_pnl_caption_names_the_known_delay_of_the_brokers_history():
    view = hm.build_view([], _two_symbols(_hist()), state("week", "pnl"), TUE_OPEN)
    assert "a trade placed directly at the broker can take a few minutes to reach the broker's history" \
        in hm.caption(view)
    plain = hm.build_view([lot()], market(prices={"AAPL": 1.0}), state(metric="pnl"), TUE_OPEN)
    assert "few minutes" not in hm.caption(plain)


# ---- option assignment / exercise, equity offering, warrant: refuse the held tile only ----------------------------

ASSIGN_WHY = "an option assignment or exercise in the period: the share delivery cannot be verified"


def _tile_only(rid, when, symbol="AAOI  261218C00040000", underlying="AAOI", detail=ASSIGN_WHY):
    return _m(rid, 100.0, None, when, kind="tile_only", symbol=symbol, underlying=underlying, detail=detail)


@pytest.mark.parametrize("label", ["assignment", "exercise"])
def test_an_option_assignment_or_exercise_refuses_its_held_underlying_for_the_period(label):
    hist = _hist(_tile_only(20, INSIDE))
    week = hm.build_view([], _two_symbols(hist), state("week", "pnl"), TUE_OPEN)
    assert tile_of(week, "AAOI").pnl_amount is None and tile_of(week, "GDXY").pnl_amount is not None
    assert any(sym == "AAOI" and ASSIGN_WHY in why for sym, why in week.issues)
    before = _hist(_tile_only(20, utc(2026, 11, 2, 15)))                   # before the week: the week stands
    assert tile_of(hm.build_view([], _two_symbols(before), state("week", "pnl"), TUE_OPEN), "AAOI").pnl_amount \
        is not None


def test_an_assignment_on_an_underlying_not_held_refuses_nothing():
    hist = _hist(_tile_only(21, INSIDE, symbol="ZZZ  261218P00010000", underlying="ZZZ"))
    view = hm.build_view([], _two_symbols(hist), state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is not None and tile_of(view, "GDXY").pnl_amount is not None


def test_an_equity_offering_row_on_a_held_symbol_refuses_that_tile():
    why = "an Equity Offering row in the period (Receive Deliver / Allocate): its effect on the shares cannot be verified"
    hist = _hist(_tile_only(22, INSIDE, symbol="GDXY", underlying=None, detail=why))
    view = hm.build_view([], _two_symbols(hist), state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "GDXY").pnl_amount is None and tile_of(view, "AAOI").pnl_amount is not None
    assert any(sym == "GDXY" and "Equity Offering" in w for sym, w in view.issues)


# ---- recent platform fills matched one by one --------------------------------------------------------------------

RECENT_WHY = "a recent platform fill is not in the broker history yet, reload"


def _mixed(*recent_moves, recent_age_min=5, recent_qty=1.0):
    """An older platform buy and a recent one; ONE older broker buy of 2 shares covers both in the aggregate."""
    orders = {2: [PlatformOrder(symbol="AAOI", side="BUY", quantity=1.0, created_at=utc(2026, 11, 23, 14, 59)),
                  PlatformOrder(symbol="AAOI", side="BUY", quantity=recent_qty,
                                created_at=TUE_OPEN - timedelta(minutes=recent_age_min))]}
    hist = _hist(_m(30, 2.0, 30.0, INSIDE), *recent_moves)
    return hm.build_view([], _two_symbols(hist, orders=orders), state("week", "pnl"), TUE_OPEN)


def test_a_recent_platform_fill_without_its_own_broker_row_is_n_a_although_the_aggregate_passes():
    view = _mixed()
    assert tile_of(view, "AAOI").pnl_amount is None and tile_of(view, "GDXY").pnl_amount is not None
    assert ("AAOI", RECENT_WHY) in view.issues


def test_a_recent_platform_fill_with_its_broker_row_has_a_number():
    view = _mixed(_m(31, 1.0, 31.0, TUE_OPEN - timedelta(minutes=4)))
    assert tile_of(view, "AAOI").pnl_amount is not None


def test_a_recent_order_filled_in_two_broker_executions_is_matched():
    view = _mixed(_m(31, 0.5, 31.0, TUE_OPEN - timedelta(minutes=4)),
                  _m(32, 0.5, 31.1, TUE_OPEN - timedelta(minutes=3)))
    assert tile_of(view, "AAOI").pnl_amount is not None


def test_each_broker_row_matches_one_recent_order_only():
    orders = {2: [PlatformOrder(symbol="AAOI", side="BUY", quantity=1.0, created_at=utc(2026, 11, 23, 14, 59)),
                  PlatformOrder(symbol="AAOI", side="BUY", quantity=1.0, created_at=TUE_OPEN - timedelta(minutes=6)),
                  PlatformOrder(symbol="AAOI", side="BUY", quantity=1.0, created_at=TUE_OPEN - timedelta(minutes=5))]}
    hist = _hist(_m(30, 3.0, 30.0, INSIDE), _m(31, 1.0, 31.0, TUE_OPEN - timedelta(minutes=4)))   # aggregate passes
    view = hm.build_view([], _two_symbols(hist, orders=orders), state("week", "pnl"), TUE_OPEN)
    assert tile_of(view, "AAOI").pnl_amount is None


def test_an_order_older_than_thirty_minutes_is_not_matched_one_by_one():
    assert tile_of(_mixed(recent_age_min=40), "AAOI").pnl_amount is not None
