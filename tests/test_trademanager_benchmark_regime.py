"""Live publisher of the SPY benchmark regime (plan 2026-10-24 Task 10).

``TradeManager._publish_benchmark_regime`` classifies the SPY DAILY closes of FINISHED sessions with the
SAME pure function the backtest calendar is asserted against, and publishes the result on the
``benchmark_regime`` seam before the entry and exit passes. Owner decision 2026-10-24: SPY daily
closes come from FMP, "the same source as the stock price" of the three live instances.

* an unfinished TODAY bar is never used; the cache key is the newest FINISHED session;
* a failure publishes NOTHING usable (None + a reason), logs ERROR, retries after 300 s, never caches;
* a platform with no gated ruleset makes no provider call at all;
* a WARNING (not a refusal) once a day when an Alpaca account that trades prices from another feed.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from ba2_common.core import benchmark_regime as br
from ba2_common.core.market_calendar import regular_sessions_ending_at

from ba2_trade_platform.core import TradeManager as tm_mod
from ba2_trade_platform.core.TradeManager import TradeManager
from tests.factories import (
    create_account_definition, create_event_action, create_expert_instance, create_ruleset,
    link_rule_to_ruleset)

NY = ZoneInfo("America/New_York")
THU = date(2026, 10, 22)


def _capture_logs(monkeypatch):
    """ba2 loggers set propagate=False, so caplog never sees them: record the TradeManager module logger's
    calls instead. Returns a list of (levelno, message)."""
    records = []
    for name, level in (("warning", logging.WARNING), ("error", logging.ERROR), ("critical", logging.CRITICAL)):
        monkeypatch.setattr(tm_mod.logger, name,
                            lambda msg, *a, _l=level, **k: records.append((_l, str(msg) % a if a else str(msg))))
    return records


def at_ny(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=NY).astimezone(timezone.utc)


def series(last: date, n: int = 1000):
    dates = regular_sessions_ending_at(last, n)
    rng = np.random.default_rng(11)
    closes = (400.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, len(dates))))).tolist()
    return list(dates), closes


class Fetcher:
    """Stands in for ``_fetch_benchmark_daily``: the provider edge, call-counting."""

    def __init__(self, last: date, n: int = 1000, raises: Exception | None = None):
        self.dates, self.closes = series(last, n)
        self.calls = 0
        self.raises = raises

    def __call__(self, now):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return list(self.dates), list(self.closes)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    br.reset_benchmark_regime()
    tm_mod._reset_benchmark_regime_cache()
    # a gated platform unless a test says otherwise
    monkeypatch.setattr(TradeManager, "_benchmark_regime_needed", lambda self: True)
    yield
    br.reset_benchmark_regime()
    tm_mod._reset_benchmark_regime_cache()


def make(monkeypatch, fetcher):
    tm = TradeManager()
    monkeypatch.setattr(tm, "_fetch_benchmark_daily", fetcher)
    return tm


def test_unfinished_today_bar_is_not_used(monkeypatch):
    f = Fetcher(THU)                                           # the provider already has a THU row
    make(monkeypatch, f)._publish_benchmark_regime(now=at_ny(THU, 11))
    prior = f.dates[-2]
    assert prior == date(2026, 10, 21)
    assert br.get_benchmark_regime() == br.classify_benchmark_regime(f.closes[:-1], session=prior)
    assert br.get_benchmark_regime_failure() is None


def test_finished_today_bar_is_used_after_close(monkeypatch):
    f = Fetcher(THU)
    make(monkeypatch, f)._publish_benchmark_regime(now=at_ny(THU, 17))
    assert br.get_benchmark_regime() == br.classify_benchmark_regime(f.closes, session=THU)


def test_cache_is_keyed_by_newest_finished_session(monkeypatch):
    f = Fetcher(THU)
    tm = make(monkeypatch, f)
    tm._publish_benchmark_regime(now=at_ny(THU, 9, 45))
    tm._publish_benchmark_regime(now=at_ny(THU, 15, 0))        # same newest finished session: cached
    assert f.calls == 1
    tm._publish_benchmark_regime(now=at_ny(THU, 16, 30))       # THU finished: new key, new fetch
    assert f.calls == 2
    assert br.get_benchmark_regime().session == THU
    tm._publish_benchmark_regime(now=at_ny(THU, 20, 0))
    assert f.calls == 2


def test_failure_publishes_none_logs_error_and_retries(monkeypatch):
    f = Fetcher(THU, raises=RuntimeError("fmp down"))
    tm = make(monkeypatch, f)
    t0 = at_ny(THU, 10)
    records = _capture_logs(monkeypatch)
    tm._publish_benchmark_regime(now=t0)
    assert br.get_benchmark_regime() is None
    assert "fmp down" in br.get_benchmark_regime_failure()
    assert any(lvl == logging.ERROR and "benchmark regime" in m.lower() for lvl, m in records)
    assert f.calls == 1
    tm._publish_benchmark_regime(now=t0 + timedelta(seconds=120))      # inside the retry window
    assert f.calls == 1 and br.get_benchmark_regime() is None
    assert br.get_benchmark_regime_failure() is not None               # still says why
    f.raises = None
    tm._publish_benchmark_regime(now=t0 + timedelta(seconds=301))      # retry succeeds
    assert f.calls == 2
    assert br.get_benchmark_regime() is not None
    assert br.get_benchmark_regime_failure() is None
    tm._publish_benchmark_regime(now=t0 + timedelta(seconds=400))      # and is now cached
    assert f.calls == 2


@pytest.mark.parametrize("what", ["no_bars", "only_unfinished"])
def test_empty_results_are_failures(monkeypatch, what):
    f = Fetcher(THU)
    if what == "no_bars":
        f.dates, f.closes = [], []
    else:
        f.dates, f.closes = f.dates[-1:], f.closes[-1:]               # only the unfinished THU row
    make(monkeypatch, f)._publish_benchmark_regime(now=at_ny(THU, 11))
    assert br.get_benchmark_regime() is None
    assert br.get_benchmark_regime_failure()


def test_stale_data_is_a_failure(monkeypatch):
    f = Fetcher(date(2026, 10, 19))                                    # Monday; it is Thursday 11:00
    make(monkeypatch, f)._publish_benchmark_regime(now=at_ny(THU, 11))
    assert br.get_benchmark_regime() is None
    assert "stale" in br.get_benchmark_regime_failure().lower()


def test_one_session_of_provider_lag_is_published_but_not_cached(monkeypatch):
    f = Fetcher(date(2026, 10, 20))                                    # Tue; Thu 11:00 wants Wed
    tm = make(monkeypatch, f)
    tm._publish_benchmark_regime(now=at_ny(THU, 11))
    assert br.get_benchmark_regime() == br.classify_benchmark_regime(f.closes, session=date(2026, 10, 20))
    tm._publish_benchmark_regime(now=at_ny(THU, 11, 2))                # inside the retry window
    assert f.calls == 1
    tm._publish_benchmark_regime(now=at_ny(THU, 11) + timedelta(seconds=301))
    assert f.calls == 2                                                 # re-fetched: the Wed row may exist now


def test_no_gated_rulesets_means_no_fetch(monkeypatch):
    f = Fetcher(THU)
    monkeypatch.setattr(TradeManager, "_benchmark_regime_needed", lambda self: False)
    br.set_benchmark_regime(br.classify_benchmark_regime([1.0] * 5))   # a leftover must not survive
    make(monkeypatch, f)._publish_benchmark_regime(now=at_ny(THU, 11))
    assert f.calls == 0
    assert br.get_benchmark_regime() is None


def test_insufficient_history_is_published_as_unknown_with_reason(monkeypatch):
    f = Fetcher(THU, n=150)
    make(monkeypatch, f)._publish_benchmark_regime(now=at_ny(THU, 17))
    regime = br.get_benchmark_regime()
    assert regime is not None and regime.trend is None and regime.vol is None
    assert "insufficient history" in regime.trend_reason
    assert br.get_benchmark_regime_failure() is None


def test_entry_and_exit_passes_publish_first(monkeypatch):
    import ba2_trade_platform.core.utils as core_utils

    order = []
    tm = TradeManager()
    monkeypatch.setattr(tm, "_publish_benchmark_regime", lambda now=None: order.append("publish"))
    monkeypatch.setattr(core_utils, "get_expert_instance_from_id",
                        lambda expert_id, use_cache=True: order.append("expert") or None)
    tm._process_expert_recommendations_after_analysis(987)
    assert order[:2] == ["publish", "expert"]
    order.clear()
    tm.process_open_positions_recommendations(987)
    assert order[:2] == ["publish", "expert"]


def test_refresh_accounts_publishes_once_after_the_market_regime(monkeypatch):
    order = []
    tm = TradeManager()
    monkeypatch.setattr(tm, "_publish_market_regime", lambda: order.append("market"))
    monkeypatch.setattr(tm, "_publish_benchmark_regime", lambda now=None: order.append("benchmark"))
    monkeypatch.setattr(tm_mod, "get_all_instances", lambda model: [])
    tm.refresh_accounts()
    assert order[:2] == ["market", "benchmark"]


# ---------------------------------------------------------------------------------------- detection
def _gated_ruleset(event_type="spy_trend_regime"):
    rs = create_ruleset(name=f"gated-{event_type}")
    ea = create_event_action(name="gate", triggers={"cond_0": {"event_type": event_type,
                                                              "operator": "==", "value": 1}})
    link_rule_to_ruleset(rs.id, ea.id)
    return rs.id


def _plain_ruleset():
    rs = create_ruleset(name="plain")
    ea = create_event_action(name="plain", triggers={"cond_0": {"event_type": "rsi"}})
    link_rule_to_ruleset(rs.id, ea.id)
    return rs.id


@pytest.fixture
def real_needed(monkeypatch):
    monkeypatch.undo()          # drop the autouse "needed" stub for these tests


def test_needed_is_false_with_no_experts_and_with_plain_rulesets(real_needed):
    assert TradeManager()._benchmark_regime_needed() is False
    acct = create_account_definition(provider="AlpacaAccount")
    create_expert_instance(account_id=acct.id, enter_market_ruleset_id=_plain_ruleset())
    assert TradeManager()._benchmark_regime_needed() is False


@pytest.mark.parametrize("slot", ["enter_market_ruleset_id", "open_positions_ruleset_id"])
@pytest.mark.parametrize("event", ["spy_trend_regime", "spy_vol_regime"])
def test_needed_when_an_enabled_expert_carries_a_benchmark_leaf(real_needed, slot, event):
    acct = create_account_definition(provider="AlpacaAccount")
    create_expert_instance(account_id=acct.id, **{slot: _gated_ruleset(event)})
    assert TradeManager()._benchmark_regime_needed() is True


def test_a_disabled_expert_does_not_make_it_needed(real_needed):
    acct = create_account_definition(provider="AlpacaAccount")
    create_expert_instance(account_id=acct.id, enabled=False,
                           enter_market_ruleset_id=_gated_ruleset())
    assert TradeManager()._benchmark_regime_needed() is False


# ---------------------------------------------------------------------------- source consistency
def _alpaca_with_feed(name, feed):
    from ba2_trade_platform.core.db import add_instance
    from ba2_trade_platform.core.models import AccountSetting

    acct = create_account_definition(name=name, provider="AlpacaAccount")
    if feed is not None:
        add_instance(AccountSetting(account_id=acct.id, key="data_feed", value_str=feed))
    create_expert_instance(account_id=acct.id)
    return acct


def test_warns_once_a_day_when_an_alpaca_account_prices_from_another_feed(monkeypatch):
    _alpaca_with_feed("acct-fmp", "fmp")
    _alpaca_with_feed("acct-iex", "iex")
    _alpaca_with_feed("acct-unset", None)
    tm = make(monkeypatch, Fetcher(THU))
    records = _capture_logs(monkeypatch)
    tm._publish_benchmark_regime(now=at_ny(THU, 17))
    tm._publish_benchmark_regime(now=at_ny(THU, 18))
    warned = [m for lvl, m in records if lvl == logging.WARNING and "SPY regime reads FMP" in m]
    assert len(warned) == 1
    assert "acct-iex" in warned[0] and "iex" in warned[0]
    # an unset data_feed is the DECLARED default, which has been fmp since the fmp price feed shipped (APP 1249):
    # such an account matches the regime source and is not named (this asserted delayed_sip, the old default)
    assert "acct-unset" not in warned[0]
    assert "acct-fmp" not in warned[0]
    assert br.get_benchmark_regime() is not None                       # a warning, never a refusal
    tm._publish_benchmark_regime(now=at_ny(date(2026, 10, 23), 17))
    assert sum("SPY regime reads FMP" in m for _, m in records) == 2


def test_no_warning_when_every_alpaca_account_prices_from_fmp(monkeypatch):
    _alpaca_with_feed("acct-fmp", "fmp")
    tm = make(monkeypatch, Fetcher(THU))
    records = _capture_logs(monkeypatch)
    tm._publish_benchmark_regime(now=at_ny(THU, 17))
    assert br.get_benchmark_regime() is not None                       # it ran, so the absence below is not vacuous
    assert not any("SPY regime reads FMP" in m for _, m in records)


# ------------------------------------------------------------------- parity with the backtest calendar
# (local date, hh, mm, the session the backtest calendar reads at that instant). The backtest half of the
# same table is testplatform/backend/tests/test_benchmark_regime_bt_live_parity.py.
PARITY_CASES = [
    (date(2026, 10, 23), 9, 30, date(2026, 10, 22)),        # Friday open
    (date(2026, 10, 23), 16, 5, date(2026, 10, 23)),        # Friday after the close
    (date(2026, 10, 24), 12, 0, date(2026, 10, 23)),        # Saturday
    (date(2026, 10, 26), 9, 30, date(2026, 10, 23)),        # Monday open
    (date(2026, 11, 26), 10, 0, date(2026, 11, 25)),        # Thanksgiving holiday
    (date(2026, 11, 27), 12, 59, date(2026, 11, 25)),       # half day, before its 13:00 close
    (date(2026, 11, 27), 13, 5, date(2026, 11, 27)),        # half day, after it
]


@pytest.mark.parametrize("day,hh,mm,session", PARITY_CASES)
def test_live_publisher_equals_the_backtest_calendar_at_the_same_instant(monkeypatch, day, hh, mm, session):
    f = Fetcher(date(2026, 12, 31))
    cal = br.BenchmarkRegimeCalendar(f.dates, f.closes)
    tm_mod._reset_benchmark_regime_cache()
    make(monkeypatch, f)._publish_benchmark_regime(now=at_ny(day, hh, mm))
    assert br.get_benchmark_regime_failure() is None
    assert br.get_benchmark_regime() == cal.at(session)
