"""Backtest adapter for the market-condition gates (design 2026-09-15 sections 4, 4.1).

``AsOfPriceSource.window_before`` slices the columnar store; ``BacktestMarketConditionReader``
assembles/computes/memoises; ``BacktestMarketConditionResolver`` builds one frozen context per
simulated session; ``seam_wiring.install_backtest_market_conditions`` installs nothing for
profile ``none``.
"""
from __future__ import annotations

import sys
import threading
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from ba2_common.core import TradeConditions
from ba2_common.core import market_conditions as mc
from ba2_common.core.market_calendar import regular_sessions_ending_at
from ba2_common.core.market_condition_readers import MarketConditionVersionMismatch
from ba2_common.core.market_conditions import (
    OHLCV_V1,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_MISSING_SESSION,
    STATUS_VALID,
    WINDOW,
    FeatureRow,
    ProfileSpec,
)

from app.services.backtest import seam_wiring
from app.services.backtest.price_source import AsOfPriceSource

SESSION = date(2025, 6, 30)


def _rows(days, start=100.0):
    rng = np.random.default_rng(11)
    c = start * np.exp(np.cumsum(rng.normal(0, 0.012, len(days))))
    return [{"Date": d, "Open": float(x) * 1.001, "High": float(x) * 1.012, "Low": float(x) * 0.988,
             "Close": float(x), "Volume": 1e6} for d, x in zip(days, c)]


def _source(days_by_symbol):
    ps = AsOfPriceSource(ohlcv_provider=None, interval="1d")
    for sym, days in days_by_symbol.items():
        ps.load_bars(sym, _rows(days))
    return ps


@pytest.fixture
def ps():
    return _source({"AAA": regular_sessions_ending_at(date(2025, 7, 31), 200)})


@pytest.fixture(autouse=True)
def _restore_seam():
    saved = TradeConditions.get_market_condition_context_resolver()
    yield
    seam_wiring.clear_backtest_market_conditions()
    TradeConditions.set_market_condition_context_resolver(saved)


# --------------------------------------------------------------------------- window_before
def test_window_before_returns_exactly_n_bars_ending_at_the_session(ps):
    dates, o, h, l, c, v = ps.window_before("AAA", SESSION, WINDOW)
    assert len(dates) == WINDOW and all(len(a) == WINDOW for a in (o, h, l, c, v))
    assert dates.dtype == np.dtype("datetime64[D]")
    assert list(dates.astype(object)) == regular_sessions_ending_at(SESSION, WINDOW)
    assert c.dtype == np.float64 and not c.flags.writeable
    assert c[-1] == ps.close_at("AAA", datetime(2025, 6, 30))


def test_window_before_none_when_short_or_unknown(ps):
    first = regular_sessions_ending_at(date(2025, 7, 31), 200)[0]
    early = regular_sessions_ending_at(date(2025, 7, 31), 200)[WINDOW - 2]  # only 127 bars through it
    assert ps.window_before("AAA", early, WINDOW) is None
    assert ps.count_through("AAA", early) == WINDOW - 1
    assert ps.window_before("AAA", first, 1)[0][0] == np.datetime64(first)
    assert ps.window_before("ZZZ", SESSION, WINDOW) is None
    assert ps.count_through("ZZZ", SESSION) == 0


def test_window_before_on_a_non_session_date_ends_at_the_prior_bar(ps):
    dates, *_ = ps.window_before("AAA", date(2025, 6, 29), 3)  # a Sunday
    assert dates[-1] == np.datetime64("2025-06-27")


def test_window_before_refuses_an_intraday_store():
    with pytest.raises(ValueError, match="daily"):
        AsOfPriceSource(ohlcv_provider=None, interval="5m").window_before("AAA", SESSION, 5)


# --------------------------------------------------------------------------- reader
def _reader(ps):
    from app.services.backtest.market_condition_bt import BacktestMarketConditionReader
    return BacktestMarketConditionReader(ps, "ohlcv-v1")


def test_observe_memoises_one_compute_for_three_reads(ps, monkeypatch):
    calls = []
    real = mc.COMPUTE_BY_PROFILE["ohlcv-v1"]

    def spy(*arrays):
        calls.append(1)
        return real(*arrays)

    monkeypatch.setitem(mc.COMPUTE_BY_PROFILE, "ohlcv-v1", spy)
    reader = _reader(ps)
    rows = [reader.observe("AAA", SESSION) for _ in range(3)]
    assert len(calls) == 1 and reader.computed == 1
    assert rows[0] is rows[1] is rows[2]
    assert rows[0].by_field() is rows[1].by_field()
    assert all(o.status == STATUS_VALID for o in rows[0].by_field().values())


def test_observe_matches_the_pure_calculator(ps):
    dates, o, h, l, c, v = ps.window_before("AAA", SESSION, WINDOW)
    expected = mc.compute_market_conditions(o, h, l, c, v).to_feature_row()
    assert _reader(ps).observe("AAA", SESSION) == expected


def test_observe_statuses_for_short_missing_and_unknown():
    days = regular_sessions_ending_at(date(2025, 7, 31), 200)
    hole = regular_sessions_ending_at(SESSION, 40)[0]
    ps = _source({"YOUNG": days[-60:], "HOLE": [d for d in days if d != hole], "LATE": days[-5:]})
    reader = _reader(ps)
    status = lambda sym: {o.status for o in reader.observe(sym, SESSION).by_field().values()}  # noqa: E731
    assert status("YOUNG") == {STATUS_INSUFFICIENT_HISTORY}
    assert status("HOLE") == {STATUS_MISSING_SESSION}
    assert status("LATE") == {STATUS_INSUFFICIENT_HISTORY}
    assert reader.observe("NOPE", SESSION) is None


def test_calc_version_mismatch_raises(ps, monkeypatch):
    def wrong(*arrays):
        row = mc.compute_market_conditions(*arrays).to_feature_row()
        return FeatureRow(values=row.by_field(), calc_versions={f: "ohlcv-v1/calc-0" for f in row.by_field()})

    monkeypatch.setitem(mc.COMPUTE_BY_PROFILE, "ohlcv-v1", wrong)
    with pytest.raises(MarketConditionVersionMismatch):
        _reader(ps).observe("AAA", SESSION)


def test_registry_version_change_under_a_live_reader_raises(ps, monkeypatch):
    reader = _reader(ps)
    reader.observe("AAA", SESSION)
    monkeypatch.setitem(mc.PROFILES, "ohlcv-v1",
                        ProfileSpec(name="ohlcv-v1", calc_version="ohlcv-v1/calc-2", fields=OHLCV_V1.fields))
    with pytest.raises(MarketConditionVersionMismatch):
        reader.observe("AAA", SESSION)


# --------------------------------------------------------------------------- resolver
class _Account:
    def __init__(self, day):
        self.day = day

    def _as_of_date(self):
        return self.day


def test_resolver_builds_one_context_per_session(ps):
    from app.services.backtest.market_condition_bt import BacktestMarketConditionResolver

    resolver = BacktestMarketConditionResolver(_reader(ps))
    account = _Account(date(2025, 7, 1))
    a, b, c = (resolver(account, "AAA", None) for _ in range(3))
    assert a is b is c
    assert a.session_label == date(2025, 7, 1)
    assert a.prior_session == SESSION
    assert a.decision_time == datetime(2025, 7, 1, 20, 0, tzinfo=timezone.utc)
    assert a.calc_version == OHLCV_V1.calc_version
    assert a.source_profile == "fmp-daily-split-adjusted-v1"
    assert a.timing_policy == "prior_session_v1"

    account.day = date(2025, 7, 2)
    d = resolver(account, "AAA", None)
    assert d is not a and d.prior_session == date(2025, 7, 1)
    assert resolver(account, "BBB", None) is d


def test_resolver_non_session_date_is_no_context(ps):
    from app.services.backtest.market_condition_bt import BacktestMarketConditionResolver

    resolver = BacktestMarketConditionResolver(_reader(ps))
    assert resolver(_Account(date(2025, 7, 4)), "AAA", None) is None


def test_a_market_leaf_evaluates_through_the_installed_resolver(ps):
    seam_wiring.install_backtest_market_conditions({"market_condition_profile": "ohlcv-v1"}, ps)
    from ba2_common.core.types import ExpertEventType

    account = SimpleNamespace(_as_of_date=lambda: date(2025, 7, 1))
    row = _reader(ps).observe("AAA", SESSION)
    adx = row.by_field()["underlying_adx_14"].value
    leaf = TradeConditions.create_condition(ExpertEventType.N_UNDERLYING_ADX, account, "AAA", None,
                                            operator_str=">", value=adx - 1.0)
    assert leaf.evaluate() is True and leaf.calculated_value == adx


# --------------------------------------------------------------------------- wiring
def test_profile_none_installs_nothing_and_imports_nothing(ps, monkeypatch):
    TradeConditions.set_market_condition_context_resolver(None)
    monkeypatch.delitem(sys.modules, "app.services.backtest.market_condition_bt", raising=False)
    assert seam_wiring.install_backtest_market_conditions({"market_condition_profile": "none"}, ps) is None
    assert TradeConditions.get_market_condition_context_resolver() is None
    assert "app.services.backtest.market_condition_bt" not in sys.modules


def test_profile_key_is_required(ps):
    with pytest.raises(KeyError):
        seam_wiring.install_backtest_market_conditions({}, ps)


def test_unknown_profile_raises(ps):
    with pytest.raises(ValueError, match="not registered"):
        seam_wiring.install_backtest_market_conditions({"market_condition_profile": "ohlcv-v9"}, ps)


def test_profile_installs_a_thread_local_run_resolver(ps):
    resolver = seam_wiring.install_backtest_market_conditions({"market_condition_profile": "ohlcv-v1"}, ps)
    assert resolver is not None
    dispatch = TradeConditions.get_market_condition_context_resolver()
    assert dispatch is seam_wiring._dispatch_market_condition_context
    account = _Account(date(2025, 7, 1))
    assert dispatch(account, "AAA", None) is resolver(account, "AAA", None)

    seen = []
    t = threading.Thread(target=lambda: seen.append(dispatch(account, "AAA", None)))
    t.start()
    t.join()
    assert seen == [None], "another thread's run must not see this run's resolver"

    seam_wiring.clear_backtest_market_conditions()
    assert dispatch(account, "AAA", None) is None


def test_profile_none_with_market_leaves_in_the_rules_raises(ps):
    import json

    tree = {"op": "AND", "children": [
        {"id": "o_lc-entry-iv", "field": "iv_rank", "op": "<", "value": 30},
        {"id": "o_lc-market-adx", "field": "underlying_adx_14", "op": "<", "value": 25},
    ]}
    with pytest.raises(ValueError, match="o_lc-market-adx"):
        seam_wiring.install_backtest_market_conditions(
            {"market_condition_profile": "none", "entry_rules": [{"conditions": tree}]}, ps)
    # A tree held as a JSON string inside expert settings is found too.
    cfg = {"market_condition_profile": "none",
           "experts": [{"class": "X", "settings": {"entry_condition": json.dumps(tree)}}]}
    with pytest.raises(ValueError, match="o_lc-market-adx"):
        seam_wiring.install_backtest_market_conditions(cfg, ps)
    # A leaf without an id is named by its config path.
    no_id = {"market_condition_profile": "none",
             "exit_rules": [{"field": "underlying_realized_vol_ratio_5_20", "op": ">", "value": 1}]}
    with pytest.raises(ValueError, match=r"config\.exit_rules\[0\]"):
        seam_wiring.install_backtest_market_conditions(no_id, ps)


def test_profile_none_without_market_leaves_is_fine(ps):
    cfg = {"market_condition_profile": "none", "enabled_instruments": ["AAA"],
           "entry_rules": [{"conditions": {"id": "x", "field": "iv_rank", "op": "<", "value": 30}}],
           "experts": [{"class": "X", "settings": {"note": "[not json", "tree": "{\"field\": \"iv_rank\"}"}}]}
    assert seam_wiring.install_backtest_market_conditions(cfg, ps) is None
    assert seam_wiring.market_condition_leaves_in(cfg) == []
