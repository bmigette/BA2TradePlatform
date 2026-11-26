"""Overview "Heatmap" tab: the pure rules (periods, reference closes, both colour metrics, both size modes,
aggregation, filters, colour clamp, remembered choices, the chart options).

No NiceGUI, no DB, no network: everything here is plain data in, plain data out. The NYSE calendar is the
platform's own offline schedule (``ba2_common.core.market_calendar``)."""
import json
import math
from datetime import date, datetime, timezone

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
           caps_missing=None, splits=None, splits_missing=None, broker_qty=None):
    return MarketInputs(prices=prices or {}, price_missing=price_missing or {},
                        closes=closes or {}, closes_missing=closes_missing or {},
                        market_caps=caps or {}, market_caps_missing=caps_missing or {},
                        labels=labels or {}, splits=splits or {}, splits_missing=splits_missing or {},
                        broker_qty=broker_qty or {})


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

def test_caption_says_tiles_are_platform_positions():
    assert "opened through the platform" in hm.caption(hm.build_view([lot()], market(), state(), TUE_OPEN))


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
