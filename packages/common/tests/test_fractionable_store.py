"""``fractionable_store``: the on-disk answer a backtest uses instead of a broker.

Hermetic by construction -- no network anywhere. A missing or broken file must degrade to
"unknown" (whole shares), never raise into a trial and never invent an answer.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from ba2_common.core import fractionable_store as fs


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    from ba2_common import config
    monkeypatch.setattr(config, "CACHE_FOLDER", str(tmp_path), raising=False)
    fs.clear_memo()
    yield
    fs.clear_memo()


def test_no_file_is_an_empty_map_not_an_error():
    """The normal cold state: every symbol sizes in whole shares, as before the feature."""
    assert fs.load_fractionable_map() == {}


def test_a_written_map_reads_back():
    fs.save_fractionable_map({"AAPL": True, "BRK.A": False}, source="test")

    assert fs.load_fractionable_map() == {"AAPL": True, "BRK.A": False}


def test_symbols_are_normalised_on_both_sides():
    fs.save_fractionable_map({" aapl ": True}, source="test")

    assert fs.load_fractionable_map() == {"AAPL": True}


def test_an_empty_answer_is_refused_rather_than_written_over_a_good_file():
    """Every broker lists thousands of equities; an empty answer is a failed fetch, and
    writing it would silently regress a warm cache to whole shares everywhere."""
    fs.save_fractionable_map({"AAPL": True}, source="test")

    with pytest.raises(ValueError, match="empty"):
        fs.save_fractionable_map({}, source="test")
    assert fs.load_fractionable_map() == {"AAPL": True}


def test_non_boolean_values_are_dropped_not_coerced():
    """``bool("false")`` is True. A hand-edited string must read as unknown."""
    path = fs.store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(),
                   "symbols": {"AAPL": True, "MSFT": "false", "TSLA": 1, "SPY": None}}, fh)

    assert fs.load_fractionable_map() == {"AAPL": True}


def test_a_corrupt_file_degrades_to_empty():
    path = fs.store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("{not json")

    assert fs.load_fractionable_map() == {}


def test_a_rewrite_is_picked_up_without_a_restart():
    """Memoised on mtime: thousands of trials parse once, but a prewarm mid-campaign still
    reaches the next trial."""
    fs.save_fractionable_map({"AAPL": True}, source="test")
    assert fs.load_fractionable_map() == {"AAPL": True}

    path = fs.store_path()
    fs.save_fractionable_map({"AAPL": False}, source="test")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    assert fs.load_fractionable_map() == {"AAPL": False}


def test_the_write_leaves_no_temp_file_behind():
    fs.save_fractionable_map({"AAPL": True}, source="test")

    folder = os.path.dirname(fs.store_path())
    assert [n for n in os.listdir(folder) if ".tmp." in n] == []


def test_age_comes_from_the_payload_not_the_mtime():
    """A cache sync stamps a fresh mtime on a stale answer; the payload's own timestamp is
    what says how old the broker's answer really is."""
    fs.save_fractionable_map({"AAPL": True}, source="test")
    later = datetime.now(timezone.utc) + timedelta(hours=30)

    assert fs.store_age_hours(now=later) == pytest.approx(30.0, abs=0.05)


def test_age_of_no_file_is_none():
    assert fs.store_age_hours() is None


def test_the_path_follows_CACHE_FOLDER_at_call_time(tmp_path, monkeypatch):
    """Read at call time, never at import: a worker that repoints its cache after importing
    must get the folder it configured."""
    from ba2_common import config
    other = tmp_path / "elsewhere"
    monkeypatch.setattr(config, "CACHE_FOLDER", str(other), raising=False)

    assert fs.store_path().startswith(str(other))
