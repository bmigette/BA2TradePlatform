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
    os.makedirs(os.path.join(str(tmp_path), "fred"), exist_ok=True)
    yield


def _write(sid, rows, *, fetched_at=None, fmt=True):
    """A cache file as ``refresh_series`` writes it: fetched NOW unless told otherwise, in the
    first-release format unless ``fmt=False`` (the pre-2026-09-26 format)."""
    from datetime import datetime, timezone

    doc = {"series_id": sid,
           "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
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
        from datetime import datetime, timedelta, timezone

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1, hours=1)).isoformat()
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
        from datetime import datetime, timedelta, timezone

        _write("VIXCLS", _rows())
        assert fred_series._load("VIXCLS") == _rows()
        fred_series._META["VIXCLS"]["fetched_at"] = (
            datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
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
