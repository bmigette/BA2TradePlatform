"""Live top-up of AlpacaHistoryOHLCVProvider with account keys (plan 2026-10-24-history-source-task11, Task 5).

The cache-only behaviour without keys stays pinned by test_alpaca_history_provider.py."""
import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from ba2_common.core import native_cache, ohlcv_final_bars
from ba2_common.core.market_calendar import regular_session_dates

FOLDER = "AlpacaHistoryOHLCVProvider"


class FakeBars:
    """Stands in for AlpacaLiveBars: RAW native frames, a call log, an optional failure."""

    def __init__(self, daily=None, intraday=None, error=None):
        self._daily, self._intraday, self.error, self.calls = daily, intraday, error, []

    @staticmethod
    def _slice(frame, start, end):
        d = pd.to_datetime(frame["Date"]).dt.normalize()
        return frame[(d >= pd.Timestamp(start)) & (d < pd.Timestamp(end))]

    def daily(self, symbol, start, end):
        self.calls.append(("1d", symbol, start, end))
        if self.error:
            raise self.error
        return self._slice(self._daily, start, end)

    def intraday_5min(self, symbol, start, end):
        self.calls.append(("5min", symbol, start, end))
        if self.error:
            raise self.error
        return self._slice(self._intraday, start, end)


def _raw_daily(days, px=10.0):
    d = pd.to_datetime(list(days))
    return pd.DataFrame({"Date": d, "Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 1000.0})


def _raw_5min(days, px=10.0, per_day=78):
    t = [pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=30 + 5 * i) for d in days for i in range(per_day)]
    return pd.DataFrame({"Date": t, "Open": px, "High": px + 1, "Low": px - 1, "Close": px, "Volume": 100.0})


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A cache ending Fri 2026-10-16 plus a manifest listing AAA; the clock is Sat 2026-10-24 (sessions 19..23 are final)."""
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))
    import ba2_common.config as cfg
    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    root = tmp_path / FOLDER
    root.mkdir()
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(ohlcv_final_bars, "now_utc", lambda: now)
    daily = pd.DataFrame({"Date": pd.bdate_range(end="2026-10-16", periods=5)})
    daily = daily.assign(Open=10.0, High=11.0, Low=9.0, Close=10.0, Volume=1000, effective_date=daily["Date"])
    daily.to_parquet(root / "AAA_1d.parquet", index=False)
    five = _raw_5min(["2026-10-16"])
    five.assign(effective_date=five["Date"]).to_parquet(root / "AAA_5min.parquet", index=False)
    (root / "_meta").mkdir()
    (root / "_meta" / "build_manifest.json").write_text(
        json.dumps({"symbols": {"AAA": {"events": {"splits": [], "spinoffs": []}}}}))
    (root / "_meta" / "corporate_actions_live.json").write_text(json.dumps(       # the daily job ran on the previous session
        {"run_date": "2026-10-23", "symbols_checked": ["AAA"], "applied": [], "pending": [], "review": []}))
    return root, now


def _provider(bars, now, **kw):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import AlpacaHistoryOHLCVProvider
    return AlpacaHistoryOHLCVProvider("k", "s", fetcher=bars, clock=lambda: now, **kw)


WEEK = [d.isoformat() for d in regular_session_dates(date(2026, 10, 19), date(2026, 10, 23))]
START = datetime(2026, 10, 1)


def test_no_credentials_is_still_cache_only(live):
    from ba2_providers.ohlcv.AlpacaHistoryOHLCVProvider import (
        AlpacaHistoryOHLCVProvider, HistoryTopUpUnavailable)
    with pytest.raises(HistoryTopUpUnavailable):
        AlpacaHistoryOHLCVProvider().get_ohlcv_data("AAA", start_date=START, interval="1d")


def test_latest_daily_read_with_credentials_appends_the_finished_sessions(live):
    root, now = live
    bars = FakeBars(daily=_raw_daily(WEEK))
    df = _provider(bars, now).get_ohlcv_data("AAA", start_date=START, interval="1d")
    assert [str(d.date()) for d in df["Date"].iloc[-5:]] == WEEK
    on_disk = pd.read_parquet(root / "AAA_1d.parquet")
    assert len(on_disk) == 10 and on_disk["Date"].is_monotonic_increasing
    assert bars.calls == [("1d", "AAA", date(2026, 10, 19), date(2026, 10, 24))]


def test_a_second_latest_read_does_not_fetch_again(live):
    root, now = live
    bars = FakeBars(daily=_raw_daily(WEEK))
    p = _provider(bars, now)
    p.get_ohlcv_data("AAA", start_date=START, interval="1d")
    p.get_ohlcv_data("AAA", start_date=START, interval="1d")
    assert len(bars.calls) == 1


def test_latest_5min_read_appends_whole_sessions(live):
    root, now = live
    _provider(FakeBars(intraday=_raw_5min(WEEK)), now).get_ohlcv_data("AAA", start_date=START, interval="5min")
    assert len(pd.read_parquet(root / "AAA_5min.parquet")) == 78 * 6


def test_pinned_read_does_not_fetch_and_old_rows_stay_identical(live):
    root, now = live
    bars = FakeBars(daily=_raw_daily(WEEK))
    p = _provider(bars, now)
    pinned = dict(start_date=START, end_date=datetime(2026, 10, 16), interval="1d")
    before = p.get_ohlcv_data("AAA", **pinned)
    assert bars.calls == []
    p.get_ohlcv_data("AAA", start_date=START, interval="1d")          # a latest read tops up
    after = p.get_ohlcv_data("AAA", **pinned)
    pd.testing.assert_frame_equal(before, after)
    assert len(bars.calls) == 1


def test_fetch_error_propagates_and_cache_is_untouched(live):
    from ba2_providers.ohlcv.alpaca_history.live_fetch import AlpacaLiveFetchError
    root, now = live
    snapshot = (root / "AAA_1d.parquet").read_bytes()
    with pytest.raises(AlpacaLiveFetchError):
        _provider(FakeBars(error=AlpacaLiveFetchError("HTTP 500")), now).get_ohlcv_data(
            "AAA", start_date=START, interval="1d")
    assert (root / "AAA_1d.parquet").read_bytes() == snapshot


def test_a_gap_in_the_vendor_data_leaves_the_file_untouched_and_the_lock_free(live):
    from ba2_providers.ohlcv.alpaca_history.live_topup import TopUpGap
    root, now = live
    snapshot = (root / "AAA_1d.parquet").read_bytes()
    with pytest.raises(TopUpGap):
        _provider(FakeBars(daily=_raw_daily(WEEK[:2] + WEEK[3:])), now).get_ohlcv_data(
            "AAA", start_date=START, interval="1d")
    assert (root / "AAA_1d.parquet").read_bytes() == snapshot
    assert not (root / "_meta" / "locks" / "AAA.lock").exists()


def test_symbol_not_in_the_build_is_refused_without_a_fetch(live):
    from ba2_providers.ohlcv.alpaca_history.live_topup import SymbolNotInHistory
    root, now = live
    pd.read_parquet(root / "AAA_1d.parquet").to_parquet(root / "ZZZ_1d.parquet", index=False)   # file, no manifest entry
    bars = FakeBars(daily=_raw_daily(WEEK))
    with pytest.raises(SymbolNotInHistory):
        _provider(bars, now).get_ohlcv_data("ZZZ", start_date=START, interval="1d")
    assert bars.calls == []


def test_split_in_new_bars_triggers_one_refresh_then_retry(live):
    from ba2_providers.ohlcv.alpaca_history import live_state
    root, now = live
    seen = []

    def refresher(symbol):
        seen.append(symbol)
        live_state.rebase_symbol(symbol, [("2026-10-19", 2.0, "split")], now=now)
    p = _provider(FakeBars(daily=_raw_daily(WEEK, px=5.0)), now, corporate_actions_refresher=refresher)
    df = p.get_ohlcv_data("AAA", start_date=START, interval="1d")
    assert seen == ["AAA"] and set(df["Close"]) == {5.0}


def test_second_failure_after_refresh_raises(live):
    from ba2_providers.ohlcv.alpaca_history.live_topup import PendingCorporateAction
    root, now = live
    seen = []
    p = _provider(FakeBars(daily=_raw_daily(WEEK, px=5.0)), now, corporate_actions_refresher=seen.append)
    with pytest.raises(PendingCorporateAction):
        p.get_ohlcv_data("AAA", start_date=START, interval="1d")
    assert seen == ["AAA"]                                  # one refresh, one retry, no loop


def test_pending_action_without_a_refresher_raises_at_once(live):
    from ba2_providers.ohlcv.alpaca_history.live_topup import PendingCorporateAction
    root, now = live
    with pytest.raises(PendingCorporateAction):
        _provider(FakeBars(daily=_raw_daily(WEEK, px=5.0)), now).get_ohlcv_data(
            "AAA", start_date=START, interval="1d")


def test_stale_corporate_actions_run_blocks_the_topup(live):
    root, now = live
    (root / "_meta" / "corporate_actions_live.json").write_text(json.dumps(
        {"run_date": "2026-10-20", "symbols_checked": [], "applied": [], "pending": [], "review": []}))
    bars = FakeBars(daily=_raw_daily(WEEK))
    from ba2_providers.ohlcv.alpaca_history.corporate_actions import CorporateActionsStale
    with pytest.raises(CorporateActionsStale):
        _provider(bars, now).get_ohlcv_data("AAA", START, None, "1d")
    assert bars.calls == []


def test_symbol_under_review_is_refused_without_a_fetch(live):
    root, now = live
    (root / "_meta" / "corporate_actions_live.json").write_text(json.dumps(
        {"run_date": "2026-10-23", "symbols_checked": ["AAA"], "applied": [], "pending": [],
         "review": [{"symbol": "AAA", "row": {"event_date": "2026-10-20", "verdict": "both_contradicted"}}]}))
    bars = FakeBars(daily=_raw_daily(WEEK))
    from ba2_providers.ohlcv.alpaca_history.live_topup import PendingCorporateAction
    with pytest.raises(PendingCorporateAction, match="under review"):
        _provider(bars, now).get_ohlcv_data("AAA", START, None, "1d")
    assert bars.calls == []
