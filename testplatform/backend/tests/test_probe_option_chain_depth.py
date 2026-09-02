"""tools/probe_option_chain_depth.py -- chain-depth preflight over the options parquet tree.

Loads the tool by ABSOLUTE path (the ``_LAUNCHER_PATH`` pattern from
test_option_grid_foundations.py), not a bare ``import``, so a shadowed PYTHONPATH can never
silently resolve it to a copy in the main tree.

Fixture: a tmp parquet tree with three symbols --
  * DEEP    -- one expiry, bars going back 400 days before it (LEAPS-depth, design Section 1's
               measured 745-858d range scaled down for a fast fixture; 400 is comfortably past
               the 365 LEAPS threshold used in these tests).
  * SHALLOW -- one expiry, bars going back only 60 days before it (the split-universe case
               design Section 1 measured: lists options, never reaches LEAPS depth).
  * EMPTY   -- no partitions at all (directory absent).
"""
import glob
import importlib.util
import os
import sys
from datetime import date, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


# tests/ -> backend/ -> testplatform/, then tools/ beside testplatform/, at the repo root.
# Resolved off __file__ per the ground rules -- a CWD-relative string resolves differently
# depending on whether the suite runs from the repo root or from testplatform/backend.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_TOOL_PATH = os.path.join(_REPO_ROOT, "tools", "probe_option_chain_depth.py")


def _load_tool():
    assert os.path.isfile(_TOOL_PATH), _TOOL_PATH
    spec = importlib.util.spec_from_file_location("probe_option_chain_depth", _TOOL_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["probe_option_chain_depth"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


def _write_partition(root: str, symbol: str, expiry: date, bar_dates) -> None:
    d = os.path.join(root, symbol, f"exp={expiry.isoformat()}")
    os.makedirs(d, exist_ok=True)
    table = pa.table({"bar_date": pa.array([b.isoformat() for b in bar_dates],
                                           type=pa.string())})
    pq.write_table(table, os.path.join(d, f"{symbol}_{expiry.isoformat()}_1d.parquet"))


EXPIRY = date(2025, 1, 17)
DEEP_BAR = EXPIRY - timedelta(days=400)       # 400d before expiry -- well past a 365 threshold
SHALLOW_BAR = EXPIRY - timedelta(days=60)     # 60d before expiry -- short of a 365 threshold
WINDOW_START = date(2023, 1, 1)
WINDOW_END = date(2025, 12, 31)


@pytest.fixture
def tree(tmp_path):
    root = str(tmp_path / "opt")
    _write_partition(root, "DEEP", EXPIRY, [DEEP_BAR, EXPIRY - timedelta(days=200)])
    _write_partition(root, "SHALLOW", EXPIRY, [SHALLOW_BAR, EXPIRY - timedelta(days=30)])
    # EMPTY: no directory written at all -- the "never fetched" case, not "fetched and empty".
    return root


# --------------------------------------------------------------------------------------- #
# classification: kept / dropped + the reason each dropped symbol carries
# --------------------------------------------------------------------------------------- #
def test_deep_symbol_is_kept(tool, tree):
    r = tool.probe_symbol(tree, "DEEP", 365, WINDOW_START, WINDOW_END)
    assert r.kept is True
    assert r.reason is None
    assert r.best_dte == 400


def test_shallow_symbol_is_dropped_for_depth_not_absence(tool, tree):
    r = tool.probe_symbol(tree, "SHALLOW", 365, WINDOW_START, WINDOW_END)
    assert r.kept is False
    assert r.reason == tool.REASON_NO_DEPTH
    assert r.best_dte == 60


def test_empty_symbol_is_dropped_for_no_partitions_not_depth(tool, tree):
    r = tool.probe_symbol(tree, "EMPTY", 365, WINDOW_START, WINDOW_END)
    assert r.kept is False
    assert r.reason == tool.REASON_NO_PARTITIONS
    assert r.best_dte is None


# --------------------------------------------------------------------------------------- #
# the DTE boundary convention: >= is KEPT, not >
# --------------------------------------------------------------------------------------- #
def test_bar_at_exactly_min_dte_is_kept(tool, tree):
    """A DEEP bar sits at exactly 400 days. Probing with min_dte=400 must still KEEP it --
    the documented ">=" convention, not ">"."""
    r = tool.probe_symbol(tree, "DEEP", 400, WINDOW_START, WINDOW_END)
    assert r.kept is True
    assert r.best_dte == 400


def test_bar_one_day_short_of_min_dte_is_dropped(tool, tree):
    r = tool.probe_symbol(tree, "DEEP", 401, WINDOW_START, WINDOW_END)
    assert r.kept is False
    assert r.reason == tool.REASON_NO_DEPTH
    assert r.best_dte == 400


# --------------------------------------------------------------------------------------- #
# bar_date, not expiry, gates the window -- a LEAPS expiry can sit after --end
# --------------------------------------------------------------------------------------- #
def test_bar_outside_window_is_not_counted(tool, tree):
    # Window ends before the DEEP bar's date -> no qualifying in-window bar remains, even
    # though the shallower 200d-out bar exists on disk too (also excluded, same reason).
    r = tool.probe_symbol(tree, "DEEP", 365, date(2020, 1, 1), DEEP_BAR - timedelta(days=1))
    assert r.kept is False
    assert r.reason == tool.REASON_NO_DEPTH
    assert r.best_dte is None


def test_expiry_after_window_end_does_not_disqualify_a_bar_inside_the_window(tool, tree):
    # EXPIRY (2025-01-17) is after WINDOW_END would need EXPIRY > WINDOW_END; construct that
    # directly so the LEAPS shape (bar in-window, expiry beyond it) is proven, not assumed.
    root = tree
    expiry = date(2026, 3, 20)
    bar = expiry - timedelta(days=400)  # 2025-02-14, inside WINDOW
    _write_partition(root, "LATEEXP", expiry, [bar])
    r = tool.probe_symbol(root, "LATEEXP", 365, WINDOW_START, WINDOW_END)
    assert r.kept is True
    assert r.best_dte == 400


# --------------------------------------------------------------------------------------- #
# probe_symbols / main(): summary, out-file, sampling reproducibility
# --------------------------------------------------------------------------------------- #
def test_min_dte_is_a_genuine_runtime_parameter_not_hardcoded_at_365(tool, tree):
    """Plan Task 13 / design (convex-harvest) Section 4 item 4: the convex-harvest grid needs
    DTE >= 270 (broader than grid 2's LEAPS threshold of 365) from the SAME probe tool. This
    proves --min-dte is a real CLI parameter the tool reads at call time, not a value fixed
    anywhere in the module -- 270 sits strictly between SHALLOW's deepest bar (60d) and DEEP's
    (400d), so DEEP is kept and SHALLOW is dropped at 270 exactly as it is at 365."""
    results = tool.probe_symbols(tree, ["DEEP", "SHALLOW", "EMPTY"], 270,
                                 WINDOW_START, WINDOW_END)
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["DEEP"].kept is True
    assert by_symbol["SHALLOW"].kept is False
    assert by_symbol["EMPTY"].kept is False


def test_probe_symbols_covers_the_whole_input_list(tool, tree):
    results = tool.probe_symbols(tree, ["DEEP", "SHALLOW", "EMPTY"], 365,
                                  WINDOW_START, WINDOW_END)
    by_symbol = {r.symbol: r for r in results}
    assert set(by_symbol) == {"DEEP", "SHALLOW", "EMPTY"}
    assert by_symbol["DEEP"].kept is True
    assert by_symbol["SHALLOW"].kept is False
    assert by_symbol["EMPTY"].kept is False


def test_main_writes_the_kept_list_to_out_file(tool, tree, tmp_path, capsys):
    out = str(tmp_path / "kept.txt")
    rc = tool.main(["--symbols", "DEEP,SHALLOW,EMPTY", "--min-dte", "365",
                    "--start", WINDOW_START.isoformat(), "--end", WINDOW_END.isoformat(),
                    "--out", out, "--root", tree])
    assert rc == 0
    with open(out, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    assert lines == ["DEEP"]


def test_main_prints_every_dropped_symbol_with_its_reason(tool, tree, tmp_path, capsys):
    out = str(tmp_path / "kept.txt")
    tool.main(["--symbols", "DEEP,SHALLOW,EMPTY", "--min-dte", "365",
              "--start", WINDOW_START.isoformat(), "--end", WINDOW_END.isoformat(),
              "--out", out, "--root", tree])
    printed = capsys.readouterr().out
    assert "SHALLOW" in printed and tool.REASON_NO_DEPTH in printed
    assert "EMPTY" in printed and tool.REASON_NO_PARTITIONS in printed
    assert "kept 1/3" in printed
    assert "dropped 2" in printed


def test_main_returns_nonzero_when_nothing_survives(tool, tree, tmp_path):
    out = str(tmp_path / "kept.txt")
    rc = tool.main(["--symbols", "SHALLOW,EMPTY", "--min-dte", "365",
                    "--start", WINDOW_START.isoformat(), "--end", WINDOW_END.isoformat(),
                    "--out", out, "--root", tree])
    assert rc == 1
    with open(out, encoding="utf-8") as f:
        assert f.read().strip() == ""


def test_sample_with_seed_is_reproducible(tool, tree, tmp_path):
    symbols = ",".join(f"S{i}" for i in range(20))
    for i in range(20):
        _write_partition(tree, f"S{i}", EXPIRY, [DEEP_BAR])

    def _run():
        out = str(tmp_path / f"kept_{id(object())}.txt")
        tool.main(["--symbols", symbols, "--min-dte", "365",
                  "--start", WINDOW_START.isoformat(), "--end", WINDOW_END.isoformat(),
                  "--out", out, "--root", tree, "--sample", "5", "--seed", "7"])
        with open(out, encoding="utf-8") as f:
            return sorted(ln.strip() for ln in f if ln.strip())

    first = _run()
    second = _run()
    assert first == second
    assert len(first) == 5


def test_sample_prints_the_seed_used(tool, tree, tmp_path, capsys):
    for i in range(20):
        _write_partition(tree, f"P{i}", EXPIRY, [DEEP_BAR])
    out = str(tmp_path / "kept.txt")
    tool.main(["--symbols", ",".join(f"P{i}" for i in range(20)), "--min-dte", "365",
              "--start", WINDOW_START.isoformat(), "--end", WINDOW_END.isoformat(),
              "--out", out, "--root", tree, "--sample", "5", "--seed", "42"])
    printed = capsys.readouterr().out
    assert "seed=42" in printed
    assert "sampling 5 of 20" in printed


# --------------------------------------------------------------------------------------- #
# symbol-list parsing: @file dual format, comma list, dedup/upper
# --------------------------------------------------------------------------------------- #
def test_parse_symbols_comma_list_dedups_and_uppercases(tool):
    assert tool._parse_symbols("aapl, msft,AAPL") == ["AAPL", "MSFT"]


def test_parse_symbols_at_file_accepts_newline_separated(tool, tmp_path):
    p = tmp_path / "syms.txt"
    p.write_text("aapl\nmsft\n\naapl\n", encoding="utf-8")
    assert tool._parse_symbols(f"@{p}") == ["AAPL", "MSFT"]


def test_parse_symbols_at_file_accepts_one_comma_joined_line(tool, tmp_path):
    p = tmp_path / "syms.csv"
    p.write_text("aapl,msft,tsla", encoding="utf-8")
    assert tool._parse_symbols(f"@{p}") == ["AAPL", "MSFT", "TSLA"]


# --------------------------------------------------------------------------------------- #
# default root: resolved from CACHE_FOLDER + the provider's own PROVIDER_DIR constant
# --------------------------------------------------------------------------------------- #
def test_default_root_matches_provider_dir_constant(tool, monkeypatch, tmp_path):
    import ba2_common.config as cfg
    from ba2_providers.options.parquet_store import PROVIDER_DIR

    monkeypatch.setattr(cfg, "CACHE_FOLDER", str(tmp_path))
    assert tool._default_root() == os.path.join(str(tmp_path), PROVIDER_DIR)
