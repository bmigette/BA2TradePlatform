"""A missing FRED series is fetched and cached on the LIVE path, and only there.

The guard ("experts must never fetch macro data on the hot path") is right for a backtest and
was wrong for live, because nothing ever filled the cache: the live platform has no prewarm
step -- tools/refresh_fred_cache.py says it should run "on a schedule for the live platform"
and nothing ever did -- so prod's cache/fred was EMPTY and every DeterministicScorer analysis
logged "FRED series VIXCLS is not in the cache". Live reaches the network for every other
provider; macro was the one that refused and had no other way to get the data.

A backtest still raises: fetching mid-run makes the run non-reproducible, un-syncable to a GA
worker, and dependent on FRED being up.
"""
import json
import os

import pytest

from ba2_providers.fmp_common import frozen_ttl_cache, hermetic_fmp_history
from ba2_providers.macro import fred_series


@pytest.fixture(autouse=True)
def _let_caplog_see_it():
    """``ba2_common`` sets propagate=False and owns its handlers, so caplog's handler on the
    root logger never sees its records. Lift that for the duration, and put it back."""
    import logging

    lg = logging.getLogger("ba2_common")
    was = lg.propagate
    lg.propagate = True
    try:
        yield
    finally:
        lg.propagate = was


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fred_series, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(fred_series, "_MEM", {}, raising=False)
    monkeypatch.setattr(fred_series, "_META", {}, raising=False)
    monkeypatch.setattr(fred_series, "_MEM_AT", {}, raising=False)
    monkeypatch.setattr(fred_series, "_FETCH_FAILED_AT", {}, raising=False)
    # THE LIVE CLOCK IS PINNED. Whether a file "covers today" compares its fetch day with the
    # live decision day; on the wall clock that answer moves with the hour the suite runs at
    # (review 2026-09-26, item 5). Every fetch time below is written against this instant.
    monkeypatch.setattr(fred_series, "_live_decision_instant", lambda: LIVE_NOW)
    os.makedirs(os.path.join(str(tmp_path), "fred"), exist_ok=True)
    yield


from datetime import datetime as _dt, timezone as _tz  # noqa: E402

#: The pinned live decision instant: 09:30 ET on Friday 2026-09-25.
LIVE_NOW = _dt(2026, 9, 25, 13, 30, tzinfo=_tz.utc)
#: A fetch earlier on the same New York day (09:00 ET, the pre-open refresh).
FETCHED_TODAY = "2026-09-25T13:00:00+00:00"


def _write(sid, rows, *, fetched_at=None, fmt=True):
    """A cache file as ``refresh_series`` writes it: fetched on the pinned live day
    (``FETCHED_TODAY``) unless told otherwise, in the
    first-release format unless ``fmt=False`` (the pre-2026-09-26 format)."""
    doc = {"series_id": sid,
           "fetched_at": fetched_at or FETCHED_TODAY,
           "observations": rows}
    if fmt:
        doc.update({"availability": fred_series.AVAIL_FIRST_RELEASE,
                    "format": fred_series.CACHE_FORMAT_FIRST_RELEASE,
                    "first_vintage": "2010-11-22"})
    with open(fred_series.cache_path(sid), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def _rows():
    return [{"date": "2026-01-02", "value": "17.5", "realtime_start": "2026-01-02"}]


class TestTheLivePath:
    def test_a_missing_series_is_fetched_and_written(self, monkeypatch):
        calls = []

        def _refresh(sid, api_key):
            calls.append((sid, api_key))
            _write(sid, _rows())
            return 1

        monkeypatch.setattr(fred_series, "refresh_series", _refresh)
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY" , raising=False)

        assert fred_series._load("VIXCLS") == _rows()
        assert calls == [("VIXCLS", "KEY")], "it must fetch exactly once, with the configured key"
        assert os.path.exists(fred_series.cache_path("VIXCLS")), "and leave the cache warm"

    def test_a_cached_series_is_used_and_nothing_is_fetched(self, monkeypatch):
        """CACHED WINS: a warm cache behaves exactly as before -- no network at all."""
        _write("VIXCLS", _rows())
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: pytest.fail("fetched despite a warm cache"))
        assert fred_series._load("VIXCLS") == _rows()

    def test_a_fetch_failure_still_raises_the_original_error(self, monkeypatch):
        """Macro is an overlay; a fetch problem must surface as the same missing-cache error
        the caller already absorbs, not as a new exception type out of a live analysis."""
        def _boom(sid, api_key):
            raise RuntimeError("FRED is down")

        monkeypatch.setattr(fred_series, "refresh_series", _boom)
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)
        with pytest.raises(FileNotFoundError, match="not in the cache"):
            fred_series._load("VIXCLS")

    def test_no_api_key_is_reported_not_guessed(self, monkeypatch):
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: pytest.fail("fetched with no key configured"))
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: None, raising=False)
        with pytest.raises(FileNotFoundError):
            fred_series._load("VIXCLS")


class TestTheOfflinePathsStillRefuse:
    """A backtest reads what was prewarmed, or it fails -- it never fetches mid-run."""

    def test_a_frozen_ttl_run_does_not_fetch(self, monkeypatch):
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: pytest.fail("a backtest fetched macro data"))
        with frozen_ttl_cache():
            with pytest.raises(FileNotFoundError, match="before a backtest"):
                fred_series._load("VIXCLS")

    def test_a_hermetic_run_does_not_fetch(self, monkeypatch):
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: pytest.fail("a hermetic run fetched macro data"))
        with hermetic_fmp_history():
            with pytest.raises(FileNotFoundError, match="before a backtest"):
                fred_series._load("VIXCLS")

    def test_a_frozen_run_still_READS_a_warm_cache(self):
        """The refusal is about fetching, not about reading."""
        _write("VIXCLS", _rows())
        with frozen_ttl_cache():
            assert fred_series._load("VIXCLS") == _rows()


# --------------------------------------------------------------------------- #
# STALENESS: filling an empty cache is only right for a day. VIXCLS is a DAILY
# series, so a cache written once and never refreshed is correct on the day it
# was written and quietly wrong afterwards -- which is worse than obviously
# empty, because nothing complains.
# --------------------------------------------------------------------------- #
import time


def _age_file(sid, hours):
    """Backdate the cache file's mtime by *hours*."""
    path = fred_series.cache_path(sid)
    old = time.time() - hours * 3600.0
    os.utime(path, (old, old))


class TestTheLiveCacheIsRefreshedWhenStale:
    def test_a_stale_daily_series_is_refetched(self, monkeypatch):
        _write("VIXCLS", [{"date": "2026-01-01", "value": "1"}])
        _age_file("VIXCLS", 13)                      # daily window is 12h
        calls = []
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda sid, key: (calls.append(sid), _write(sid, _rows()), 1)[-1])
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)

        assert fred_series._load("VIXCLS") == _rows(), "the refreshed payload must be returned"
        assert calls == ["VIXCLS"]

    def test_a_fresh_daily_series_is_NOT_refetched(self, monkeypatch):
        _write("VIXCLS", _rows())
        _age_file("VIXCLS", 11)                      # inside the 12h window
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: pytest.fail("refetched a fresh series"))
        assert fred_series._load("VIXCLS") == _rows()

    def test_a_monthly_series_gets_the_longer_window(self, monkeypatch):
        """PAYEMS publishes monthly; refetching it every run is a call that cannot return
        anything new."""
        _write("PAYEMS", _rows())
        _age_file("PAYEMS", 13)                      # stale for daily, fresh for monthly
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: pytest.fail("refetched a monthly series at 13h"))
        assert fred_series._load("PAYEMS") == _rows()
        assert fred_series._max_age_hours("PAYEMS") == 24.0
        assert fred_series._max_age_hours("VIXCLS") == 12.0

    def test_a_failed_refresh_leaves_the_stale_rows_and_the_reader_REFUSES(self, monkeypatch,
                                                                        caplog):
        """A failed refresh leaves the old file, and ``_load`` still reads it (said out loud).
        But a first-release series fetched before today's decision lacks vintages a backtest
        of the same decision sees, so ``get_series_as_of`` refuses rather than decide on it."""
        stale = [{"date": "2020-01-01", "value": "9", "realtime_start": "2020-01-01"}]
        _write("VIXCLS", stale, fetched_at="2020-01-02T12:00:00+00:00")
        _age_file("VIXCLS", 99)
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("FRED down")))
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)

        assert fred_series._load("VIXCLS") == stale
        assert any("could not be refreshed" in r.getMessage() for r in caplog.records), \
            "a failed refresh must be said out loud, not silently"
        with pytest.raises(fred_series.MacroAvailabilityUnknown, match="before the decision"):
            fred_series.get_series_as_of("VIXCLS", None)

    def test_the_same_day_rate_series_still_serves_its_stale_copy(self, monkeypatch):
        """DGS3MO keeps the documented degrade (it is not a first-release signal input)."""
        stale = [{"date": "2020-01-01", "value": "1.5"}]
        with open(fred_series.cache_path("DGS3MO"), "w", encoding="utf-8") as fh:
            json.dump({"series_id": "DGS3MO", "vintage": False, "observations": stale}, fh)
        _age_file("DGS3MO", 99)
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("FRED down")))
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)
        assert list(fred_series.get_series_as_of("DGS3MO", None)) == [1.5]


class TestTheLiveCacheMustCoverTodaysDecision:
    """A first-release file must hold every vintage published before today's decision label,
    so live decides on exactly the rows a backtest of the same decision sees."""

    def test_a_file_fetched_before_today_is_refetched_though_young(self, monkeypatch):
        yesterday = "2026-09-24T21:00:00+00:00"          # 17:00 ET the day before
        _write("VIXCLS", [{"date": "2026-01-01", "value": "1", "realtime_start": "2026-01-01"}],
               fetched_at=yesterday)
        _age_file("VIXCLS", 1)                       # young by mtime: the age rule would pass
        calls = []
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda sid, key: (calls.append(sid), _write(sid, _rows()), 1)[-1])
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)
        assert fred_series._load("VIXCLS") == _rows()
        assert calls == ["VIXCLS"]

    def test_an_old_format_file_is_refetched_on_the_live_path(self, monkeypatch):
        """Deploying the fix must not leave live reading (and refusing) the old files."""
        _write("VIXCLS", [{"date": "2026-01-01", "value": "1"}], fmt=False)
        calls = []
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda sid, key: (calls.append(sid), _write(sid, _rows()), 1)[-1])
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)
        assert list(fred_series.get_series_as_of("VIXCLS", None)) == [17.5]
        assert calls == ["VIXCLS"]

    def test_a_memo_loaded_before_today_is_not_served(self, monkeypatch):
        _write("VIXCLS", _rows())
        assert fred_series._load("VIXCLS") == _rows()
        fred_series._META["VIXCLS"]["fetched_at"] = "2026-09-23T13:00:00+00:00"
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: pytest.fail("the file underneath is today's"))
        # The memo no longer covers today, so the file is re-read -- and it does.
        assert fred_series._load("VIXCLS") == _rows()
        assert fred_series._covers_live_today("VIXCLS", fred_series._META["VIXCLS"])

    def test_the_MEMO_expires_too(self, monkeypatch):
        """The memo short-circuits every file check, so without its own clock a live process
        would serve its startup payload for the life of the process."""
        first = [{"date": "2026-01-01", "value": "1"}]
        _write("VIXCLS", first)
        assert fred_series._load("VIXCLS") == first          # populates the memo

        _write("VIXCLS", _rows())                            # the file moves underneath it
        _age_file("VIXCLS", 13)
        fred_series._MEM_AT["VIXCLS"] = time.time() - 13 * 3600.0
        monkeypatch.setattr(fred_series, "refresh_series", lambda sid, key: 1)
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)

        assert fred_series._load("VIXCLS") == _rows(), "the memo served a payload past its window"

    def test_a_fresh_memo_is_still_served_without_touching_the_disk(self, monkeypatch):
        _write("VIXCLS", _rows())
        assert fred_series._load("VIXCLS") == _rows()
        monkeypatch.setattr(fred_series, "cache_path",
                            lambda sid: pytest.fail("went to disk for a fresh memo"))
        assert fred_series._load("VIXCLS") == _rows()


class TestABacktestNeverAgesAnything:
    def test_a_stale_file_is_read_as_is_under_a_frozen_run(self, monkeypatch):
        """Determinism: a run reads what was prewarmed, however old it is."""
        stale = [{"date": "2020-01-01", "value": "9"}]
        _write("VIXCLS", stale)
        _age_file("VIXCLS", 24 * 365)
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: pytest.fail("a backtest refetched macro data"))
        with frozen_ttl_cache():
            assert fred_series._load("VIXCLS") == stale

    def test_a_frozen_memo_never_expires(self, monkeypatch):
        """One payload from first bar to last -- the memo is what makes a per-bar expert cheap."""
        first = [{"date": "2026-01-01", "value": "1"}]
        _write("VIXCLS", first)
        with frozen_ttl_cache():
            assert fred_series._load("VIXCLS") == first
            fred_series._MEM_AT["VIXCLS"] = time.time() - 24 * 3600.0
            _write("VIXCLS", _rows())
            assert fred_series._load("VIXCLS") == first, "a frozen run re-read the file mid-run"


def test_a_seeded_memo_with_no_recorded_time_is_served_not_expired(monkeypatch):
    """Seeding ``_MEM`` is how the replay-tap tests read with no disk and no network.

    Treating an entry whose age is unknown as EXPIRED turned that seed into a real read of
    the operator's own cache -- 9,245 live observations where the fixture asked for two.
    ``_load`` always stamps what it stores, so an unstamped entry is a deliberate seed.
    """
    seeded = [{"date": "2026-06-11", "value": "14.5"}]
    fred_series._MEM["VIXCLS"] = seeded
    fred_series._MEM_AT.pop("VIXCLS", None)
    monkeypatch.setattr(fred_series, "cache_path",
                        lambda sid: pytest.fail("a seeded memo went to disk"))
    assert fred_series._load("VIXCLS") == seeded


# --------------------------------------------------------------------------- #
# Review 2026-09-26 (item 5): the fetch day is compared on the NEW YORK calendar, like the
# decision label it is compared with.
# --------------------------------------------------------------------------- #
def _header_of(sid):
    with open(fred_series.cache_path(sid), encoding="utf-8") as fh:
        return fred_series._header(json.load(fh))


def test_a_fetch_just_after_new_york_midnight_covers_that_new_york_day():
    """00:30 ET on 09-25 is still 09-24 in Chicago; the label is a New York date, so the fetch
    day must be one too: this file WAS fetched on the decision's day."""
    _write("VIXCLS", _rows(), fetched_at="2026-09-25T04:30:00+00:00")   # 00:30 EDT
    assert fred_series._covers_live_today("VIXCLS", _header_of("VIXCLS"))


def test_a_fetch_just_before_new_york_midnight_does_not_cover_the_next_day():
    _write("VIXCLS", _rows(), fetched_at="2026-09-25T03:30:00+00:00")   # 23:30 EDT on 09-24
    assert not fred_series._covers_live_today("VIXCLS", _header_of("VIXCLS"))


# --------------------------------------------------------------------------- #
# Review 2026-09-26 (I3): a live refetch is ~21 ALFRED requests. One fetch per series at a
# time (no stampede from the worker threads) and a short backoff after a failure -- while
# every read inside that window still REFUSES loudly.
# --------------------------------------------------------------------------- #
import threading  # noqa: E402

YESTERDAY = "2026-09-24T13:00:00+00:00"


class TestTheLiveRefetchIsGuarded:
    def test_concurrent_readers_share_ONE_fetch(self, monkeypatch):
        _write("VIXCLS", _rows(), fetched_at=YESTERDAY)
        calls = []

        def _slow_refresh(sid, key):
            calls.append(sid)
            time.sleep(0.3)
            _write(sid, _rows())
            return 1

        monkeypatch.setattr(fred_series, "refresh_series", _slow_refresh)
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)
        out, errors = [], []

        def _read():
            try:
                out.append(list(fred_series.get_series_as_of("VIXCLS", None)))
            except Exception as e:  # noqa: BLE001 - collected and asserted below
                errors.append(e)

        threads = [threading.Thread(target=_read) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert not errors, errors
        assert calls == ["VIXCLS"], f"{len(calls)} fetches for one stale series"
        assert out == [[17.5]] * 6

    def test_a_failed_fetch_backs_off_and_every_read_still_refuses(self, monkeypatch, caplog):
        _write("VIXCLS", _rows(), fetched_at=YESTERDAY)
        calls = []

        def _down(sid, key):
            calls.append(sid)
            raise RuntimeError("FRED is down")

        monkeypatch.setattr(fred_series, "refresh_series", _down)
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)
        clock = [1000.0]
        monkeypatch.setattr(fred_series, "_monotonic", lambda: clock[0])

        for _ in range(5):                        # five symbols of one analysis batch
            fred_series.reset_cache()
            with pytest.raises(fred_series.MacroAvailabilityUnknown):
                fred_series.get_series_as_of("VIXCLS", None)
        assert calls == ["VIXCLS"], "every read re-ran the full ALFRED fetch"
        assert any("backing off" in r.getMessage() for r in caplog.records)

        clock[0] += fred_series.LIVE_REFETCH_BACKOFF_SECONDS + 1
        fred_series.reset_cache()
        with pytest.raises(fred_series.MacroAvailabilityUnknown):
            fred_series.get_series_as_of("VIXCLS", None)
        assert calls == ["VIXCLS", "VIXCLS"], "the backoff never expires"

    def test_a_successful_fetch_clears_the_backoff(self, monkeypatch):
        fred_series._FETCH_FAILED_AT["VIXCLS"] = 0.0
        monkeypatch.setattr(fred_series, "_monotonic",
                            lambda: fred_series.LIVE_REFETCH_BACKOFF_SECONDS + 5.0)
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda sid, key: (_write(sid, _rows()), 1)[-1])
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)
        assert list(fred_series.get_series_as_of("VIXCLS", None)) == [17.5]
        assert "VIXCLS" not in fred_series._FETCH_FAILED_AT


class TestThePreOpenRefresh:
    """``refresh_for_live_decision`` is what the 09:00 ET job runs, so the 09:30 analysis reads
    a file fetched that morning instead of depending on FRED at that instant."""

    def test_it_refetches_only_what_does_not_cover_today(self, monkeypatch):
        _write("VIXCLS", _rows(), fetched_at=YESTERDAY)
        _write("BAA10Y", _rows())
        calls = []
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda sid, key: (calls.append(sid), _write(sid, _rows()), 1)[-1])
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)

        result = fred_series.refresh_for_live_decision(["VIXCLS", "BAA10Y"])

        assert calls == ["VIXCLS"]
        assert result == {"refreshed": ["VIXCLS"], "current": ["BAA10Y"], "failed": []}

    def test_a_failure_is_reported_at_error_not_swallowed(self, monkeypatch, caplog):
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("FRED down")))
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)

        result = fred_series.refresh_for_live_decision(["VIXCLS"])

        assert result["failed"] == ["VIXCLS"]
        assert any(r.levelname == "ERROR" and "VIXCLS" in r.getMessage()
                   for r in caplog.records)

    def test_it_ignores_the_backoff_a_failed_read_left(self, monkeypatch):
        """The scheduled refresh is the retry: a backoff armed by a failed read must not
        make it skip the fetch."""
        fred_series._FETCH_FAILED_AT["VIXCLS"] = 1000.0
        monkeypatch.setattr(fred_series, "_monotonic", lambda: 1001.0)
        calls = []
        monkeypatch.setattr(fred_series, "refresh_series",
                            lambda sid, key: (calls.append(sid), _write(sid, _rows()), 1)[-1])
        monkeypatch.setattr("ba2_common.config.get_app_setting", lambda k: "KEY", raising=False)
        assert fred_series.refresh_for_live_decision(["VIXCLS"])["refreshed"] == ["VIXCLS"]

    def test_the_same_day_rate_series_is_refused(self):
        with pytest.raises(ValueError, match="first-release"):
            fred_series.refresh_for_live_decision(["DGS3MO"])
