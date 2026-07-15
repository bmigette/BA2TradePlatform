"""``_parse_symbols_arg`` (comma list or @file) — shared by fetch-cache/prewarm's
--symbols so a large pinned universe file (e.g. senate_universe.txt, 498 symbols)
doesn't need a giant comma line. Mirrors fetch-options' pre-existing @file idiom.
"""
import importlib.util
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_launcher = os.path.normpath(os.path.join(_root, "..", "ba2test_launcher.py"))
if _root not in sys.path:
    sys.path.insert(0, _root)
_spec = importlib.util.spec_from_file_location("ba2test_launcher", _launcher)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_comma_list_uppercased_and_stripped():
    assert mod._parse_symbols_arg("aapl, msft ,GOOGL") == ["AAPL", "MSFT", "GOOGL"]


def test_comma_list_drops_empty_entries():
    assert mod._parse_symbols_arg("AAPL,,MSFT,") == ["AAPL", "MSFT"]


def test_at_file_reads_one_symbol_per_line(tmp_path):
    f = tmp_path / "universe.txt"
    f.write_text("aapl\nmsft\n\ngoogl\n", encoding="utf-8")
    assert mod._parse_symbols_arg(f"@{f}") == ["AAPL", "MSFT", "GOOGL"]


def test_at_file_whitespace_separated_also_works():
    """.split() (not splitlines()) so a file with trailing/blank whitespace variance
    still parses — matches fetch-options' @file behavior exactly."""
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("AAPL   MSFT\nGOOGL")
        path = f.name
    try:
        assert mod._parse_symbols_arg(f"@{path}") == ["AAPL", "MSFT", "GOOGL"]
    finally:
        os.unlink(path)
