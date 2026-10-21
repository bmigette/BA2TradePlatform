"""suspension_events: the loader (absent / valid / malformed) and the one pure suspended-on-session rule."""
import json
from datetime import date

import pytest

from ba2_common.core.suspension_events import is_suspended, load_suspension_events, symbols_to_liquidate


def _write(tmp_path, symbols, **extra):
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "suspensions.json").write_text(json.dumps({"build": "R built t", "rule": {}, "symbols": symbols, **extra}))
    return str(tmp_path)


def test_absent_file_means_no_events(tmp_path):
    assert load_suspension_events(str(tmp_path)) == {}


def test_valid_file_loads_as_date_tuples(tmp_path):
    d = _write(tmp_path, {"NESR": [["2023-04-28", "2024-10-23", "otc_suspension", 372, 0.97]], "GONE": [["2024-05-31", None, "delisted", 90, None]]})
    ev = load_suspension_events(d)
    assert ev["NESR"] == ((date(2023, 4, 28), date(2024, 10, 23)),) and ev["GONE"] == ((date(2024, 5, 31), None),)


@pytest.mark.parametrize("bad", [
    {"X": [["2024-05-31", None, "otc_suspension", 90, None]]},          # kind and resume disagree
    {"X": [["2024-05-31", "2024-05-30", "otc_suspension", 90, 1.0]]},    # resume before last
    {"X": [["2024-05-31", None, "weird", 90, None]]},
    {"X": [["not-a-date", None, "delisted", 90, None]]},
    {"X": [["2024-05-31", None, "delisted"]]},
    {"X": []},
])
def test_malformed_file_raises(tmp_path, bad):
    with pytest.raises(ValueError, match="malformed"):
        load_suspension_events(_write(tmp_path, bad))


def test_missing_keys_raise(tmp_path):
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "suspensions.json").write_text(json.dumps({"symbols": {}}))
    with pytest.raises(ValueError, match="malformed"):
        load_suspension_events(str(tmp_path))


def test_suspended_strictly_after_last_session_and_before_resume():
    ev = {"A": ((date(2024, 3, 1), date(2024, 5, 16)),), "B": ((date(2024, 3, 1), None),)}
    assert not is_suspended("A", date(2024, 3, 1), ev)           # the last real session is still tradable
    assert is_suspended("A", date(2024, 3, 4), ev) and is_suspended("A", date(2024, 5, 15), ev)
    assert not is_suspended("A", date(2024, 5, 16), ev)          # the resume session (first real bar) is tradable
    assert is_suspended("B", date(2030, 1, 1), ev) and not is_suspended("C", date(2024, 4, 1), ev)


def test_symbols_to_liquidate_keeps_order_and_is_empty_without_events():
    ev = {"A": ((date(2024, 3, 1), None),), "C": ((date(2024, 3, 1), date(2024, 6, 3)),)}
    assert symbols_to_liquidate(["C", "B", "A"], date(2024, 4, 1), ev) == ["C", "A"]
    assert symbols_to_liquidate(["A"], date(2024, 4, 1), {}) == []


def test_undecided_block_is_ignored_by_the_loader(tmp_path):
    d = _write(tmp_path, {"GONE": [["2024-05-31", None, "delisted", 90, None]]},
               undecided={"THIN": [["2024-02-29", "2024-05-16", 55, "no_fmp_file"]]})
    assert sorted(load_suspension_events(d)) == ["GONE"]


def test_loader_still_validates_symbols_when_an_undecided_block_is_present(tmp_path):
    d = _write(tmp_path, {"X": [["2024-05-31", None, "weird", 90, None]]}, undecided={})
    with pytest.raises(ValueError, match="malformed"):
        load_suspension_events(d)
