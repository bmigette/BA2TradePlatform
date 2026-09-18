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
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(fred_series, "CACHE_FOLDER", str(tmp_path), raising=False)
    monkeypatch.setattr(fred_series, "_MEM", {}, raising=False)
    os.makedirs(os.path.join(str(tmp_path), "fred"), exist_ok=True)
    yield


def _write(sid, rows):
    with open(fred_series.cache_path(sid), "w", encoding="utf-8") as fh:
        json.dump({"series_id": sid, "observations": rows}, fh)


def _rows():
    return [{"date": "2026-01-02", "value": "17.5"}]


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
