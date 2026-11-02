"""ipo-dates step: the FMP profile ipoDate cache that is the evidence for reused tickers (build.py, REUSED TICKERS)."""
import json

import pytest

from ba2_providers.ohlcv.alpaca_history import ipo_dates as ipo
from ba2_providers.ohlcv.alpaca_history.split_build import SplitTableBuildError


class FakeFmp:
    def __init__(self, profiles):
        self.profiles, self.calls = profiles, []

    def __call__(self, symbols):
        self.calls.append(list(symbols))
        return [{"symbol": s, "ipoDate": self.profiles[s]} for s in symbols if s in self.profiles]


def _read(out):
    return json.loads((out / "_meta" / "ipo_dates.json").read_text())


def test_fetch_writes_dates_and_nulls_for_missing_profiles_and_bad_dates(tmp_path):
    get = FakeFmp({"VG": "2025-01-24", "BAD": "0000-00-00", "EMPTY": "", "NONE": None})
    r = ipo.fetch_ipo_dates(["VG", "BAD", "EMPTY", "NONE", "NOPROFILE"], str(tmp_path), get)
    assert _read(tmp_path)["symbols"] == {"BAD": None, "EMPTY": None, "NONE": None, "NOPROFILE": None, "VG": "2025-01-24"}
    assert r["fetched"] == 5 and r["with_ipo_date"] == 1 and r["without_ipo_date"] == 4


def test_a_rerun_over_cached_symbols_is_offline_and_leaves_the_file_alone(tmp_path):
    get = FakeFmp({"VG": "2025-01-24"})
    ipo.fetch_ipo_dates(["VG"], str(tmp_path), get)
    before = (tmp_path / "_meta" / "ipo_dates.json").read_bytes()
    again = FakeFmp({})
    r = ipo.fetch_ipo_dates(["VG"], str(tmp_path), again)
    assert again.calls == [] and r["fetched"] == 0 and r["cached"] == 1
    assert (tmp_path / "_meta" / "ipo_dates.json").read_bytes() == before


def test_only_new_symbols_are_fetched_and_batches_are_capped(tmp_path, monkeypatch):
    ipo.fetch_ipo_dates(["VG"], str(tmp_path), FakeFmp({"VG": "2025-01-24"}))
    monkeypatch.setattr(ipo, "BATCH", 2)
    get = FakeFmp({s: "2020-01-02" for s in "ABCDE"})
    ipo.fetch_ipo_dates(["VG", "A", "B", "C", "D", "E"], str(tmp_path), get)
    assert get.calls == [["A", "B"], ["C", "D"], ["E"]]
    assert _read(tmp_path)["symbols"]["VG"] == "2025-01-24" and len(_read(tmp_path)["symbols"]) == 6


def test_refresh_fetches_everything_again(tmp_path):
    ipo.fetch_ipo_dates(["VG"], str(tmp_path), FakeFmp({"VG": "2025-01-24"}))
    get = FakeFmp({"VG": "2025-01-27"})
    ipo.fetch_ipo_dates(["VG"], str(tmp_path), get, refresh=True)
    assert get.calls == [["VG"]] and _read(tmp_path)["symbols"]["VG"] == "2025-01-27"


def test_a_malformed_cache_file_raises(tmp_path):
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "ipo_dates.json").write_text(json.dumps({"symbols": {"VG": "soon"}}))
    with pytest.raises(SplitTableBuildError, match="malformed"):
        ipo.load_ipo_dates(str(tmp_path / "_meta" / "ipo_dates.json"))
    assert ipo.load_ipo_dates(str(tmp_path / "nope.json")) is None


def test_the_profile_getter_redacts_the_key_from_request_errors():
    import requests

    class Resp:                                    # a non-retryable 401: raise_for_status carries the URL with the key
        status_code = 401
        headers = {}
        content = b""

        def raise_for_status(self):
            raise requests.HTTPError("401 for url: https://x/api/v3/profile/VG?apikey=SECRETKEY")
    get = ipo.make_profile_getter("SECRETKEY", getter=lambda url, **kw: Resp())
    with pytest.raises(SplitTableBuildError) as e:
        get(["VG"])
    assert "SECRETKEY" not in str(e.value) and "***" in str(e.value)
