"""``tools/rerun_stored_row.py --window`` is validated up front (two ISO dates, START < END)."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "rerun_stored_row.py"


@pytest.fixture(scope="module")
def tool():
    stub = types.ModuleType("ba2test_launcher")
    stub._enter_backend = lambda: None          # the real one re-points sys.path / the venv
    saved = sys.modules.get("ba2test_launcher")
    sys.modules["ba2test_launcher"] = stub
    try:
        spec = importlib.util.spec_from_file_location("rerun_stored_row_under_test", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        if saved is None:
            sys.modules.pop("ba2test_launcher", None)
        else:
            sys.modules["ba2test_launcher"] = saved


def test_no_window_is_none(tool):
    assert tool.parse_window(None) is None


def test_a_valid_window_is_returned_as_given(tool):
    assert tool.parse_window(["2026-01-01", "2026-06-30"]) == ("2026-01-01", "2026-06-30")


@pytest.mark.parametrize("bad", [["2026-06-30", "2026-01-01"], ["2026-01-01", "2026-01-01"]])
def test_start_must_be_before_end(tool, bad):
    with pytest.raises(ValueError, match="before END"):
        tool.parse_window(bad)


@pytest.mark.parametrize("bad", [["2026-13-01", "2026-06-30"], ["H1", "2026-06-30"], ["2026-01-01", "26/06/30"],
                                 ["20261005", "20261231"], ["2026-W41-1", "2026-12-31"], ["2026-1-5", "2026-12-31"]])
def test_a_non_iso_date_is_named(tool, bad):
    with pytest.raises(ValueError, match="not an ISO date"):
        tool.parse_window(bad)


def test_the_cli_exits_with_a_usage_error_before_touching_the_db(tool, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rerun_stored_row.py", "1", "--window", "2026-06-30", "2026-01-01"])
    with pytest.raises(SystemExit) as exit_info:
        tool.main()
    assert exit_info.value.code == 2
    assert "before END" in capsys.readouterr().err
