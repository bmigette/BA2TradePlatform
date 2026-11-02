"""``--exclude-symbols`` (goal2027atr split-basis fix): a per-run symbol exclusion for
``ba2-test optimize``/``optimize-batch``, so the goal2027atr grid can drop IAC (FMP spin-off
split-basis drift -- docs/strategy_research/atr_grid/excluded_symbols.txt) from every -atr27
S1-S7 job before the market-condition coverage guard ever sees it.

Loaded BY FILE PATH (not ``import ba2test_launcher``): the shared venv's editable finder maps
that name to the MAIN checkout, so a plain import here would test the wrong tree -- see
test_launcher_equity_market_conditions.py, which documents the same trap.

What this pins, in the order the failures would bite:
  * ``_resolve_exclude_symbols_arg``: comma-list and ``@file`` parsing, ``#``-comment/blank-line
    skipping in the file form, uppercasing, first-occurrence de-dup, empty/None -> [].
  * ``_apply_exclude_symbols``: removes the requested symbols from ``enabled_instruments``
    case-insensitively, persists the FULL requested list on ``excluded_instruments`` (not just
    the subset that was present), refuses an exclusion that would empty the universe, and is a
    true no-op (no key added, nothing printed) when given nothing.
  * ordering: in both ``_cmd_optimize`` and ``_cmd_optimize_batch``, the exclusion is applied
    to ``enabled_instruments`` BEFORE ``_apply_market_conditions`` (whose coverage check reads
    that same key) -- pinned against the source so a future edit cannot silently reorder them
    and let an excluded symbol back through the coverage check.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # testplatform/backend
_LAUNCHER = os.path.normpath(os.path.join(_ROOT, "..", "ba2test_launcher.py"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location("ba2test_launcher_exclsyms", _LAUNCHER)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# --------------------------------------------------------------------------- #
# _resolve_exclude_symbols_arg
# --------------------------------------------------------------------------- #
def test_none_and_empty_are_a_no_op():
    assert mod._resolve_exclude_symbols_arg(None) == []
    assert mod._resolve_exclude_symbols_arg("") == []
    assert mod._resolve_exclude_symbols_arg("   ") == []


def test_comma_list_is_uppercased_stripped_and_deduped_first_occurrence():
    assert mod._resolve_exclude_symbols_arg(" aapl, MSFT ,aapl, Tsla") == \
        ["AAPL", "MSFT", "TSLA"]


def test_at_file_skips_comments_and_blank_lines(tmp_path):
    p = tmp_path / "excl.txt"
    p.write_text(
        "# header comment\n"
        "\n"
        "IAC\n"
        "   \n"
        "# another comment\n"
        "mara\n",
        encoding="utf-8",
    )
    assert mod._resolve_exclude_symbols_arg(f"@{p}") == ["IAC", "MARA"]


def test_at_file_is_one_symbol_per_line_not_comma_separated(tmp_path):
    """The file form's docstring is explicit: ONE SYMBOL PER LINE, not comma-separated -- a
    line with a comma in it is a single (unusual, but literal) token, exactly the comma-list
    form's line-splitting is NOT reused here."""
    p = tmp_path / "excl.txt"
    p.write_text("IAC,MARA\nSMCI\n", encoding="utf-8")
    assert mod._resolve_exclude_symbols_arg(f"@{p}") == ["IAC,MARA", "SMCI"]


def test_at_file_dedupes_case_insensitively_first_occurrence():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("IAC\niac\nMARA\n")
    try:
        assert mod._resolve_exclude_symbols_arg(f"@{path}") == ["IAC", "MARA"]
    finally:
        os.remove(path)


def test_the_committed_data_file_parses_to_exactly_iac():
    """docs/strategy_research/atr_grid/excluded_symbols.txt -- the actual file the goal2027atr
    grid launches with (@docs/strategy_research/atr_grid/excluded_symbols.txt)."""
    repo_root = os.path.normpath(os.path.join(_ROOT, "..", ".."))
    data_file = os.path.join(repo_root, "docs", "strategy_research", "atr_grid",
                             "excluded_symbols.txt")
    assert os.path.isfile(data_file)
    assert mod._resolve_exclude_symbols_arg(f"@{data_file}") == ["IAC"]


# --------------------------------------------------------------------------- #
# _apply_exclude_symbols
# --------------------------------------------------------------------------- #
def test_empty_list_is_a_true_no_op(capsys):
    block = {"enabled_instruments": ["AAPL", "MSFT"]}
    mod._apply_exclude_symbols("optimize", block, [])
    assert block == {"enabled_instruments": ["AAPL", "MSFT"]}  # no excluded_instruments key added
    assert capsys.readouterr().out == ""  # nothing printed


def test_removes_matches_case_insensitively_and_persists_full_requested_list(capsys):
    block = {"enabled_instruments": ["AAPL", "iac", "MSFT"]}
    mod._apply_exclude_symbols("optimize", block, ["IAC", "MARA"])
    assert block["enabled_instruments"] == ["AAPL", "MSFT"]
    # Persisted verbatim -- MARA was requested but never present in this universe; the full
    # requested list is kept anyway (see docstring: a re-run must reproduce the exclusion even
    # if the resolved universe differs next time).
    assert block["excluded_instruments"] == ["IAC", "MARA"]
    out = capsys.readouterr().out
    assert "excluded" in out and "IAC" in out


def test_a_requested_symbol_absent_from_the_universe_is_fine(capsys):
    block = {"enabled_instruments": ["AAPL", "MSFT"]}
    mod._apply_exclude_symbols("optimize", block, ["IAC"])
    assert block["enabled_instruments"] == ["AAPL", "MSFT"]
    assert block["excluded_instruments"] == ["IAC"]
    assert "none of the given symbols were in this universe" in capsys.readouterr().out


def test_emptying_the_universe_is_refused_loudly():
    block = {"enabled_instruments": ["IAC"]}
    with pytest.raises(SystemExit, match="refusing an empty universe"):
        mod._apply_exclude_symbols("optimize", block, ["IAC"])


def test_emptying_the_universe_via_case_insensitive_match_is_also_refused():
    block = {"enabled_instruments": ["iac", "Iac"]}
    with pytest.raises(SystemExit):
        mod._apply_exclude_symbols("optimize", block, ["IAC"])


# --------------------------------------------------------------------------- #
# Ordering: excludes must be applied to enabled_instruments BEFORE the market-condition
# coverage check can read it. Pinned against the SOURCE (not just each function's own
# behaviour), since the failure mode is a future edit reordering two calls that individually
# still work fine.
# --------------------------------------------------------------------------- #
def _cmd_source(name: str) -> str:
    src = open(_LAUNCHER, encoding="utf-8").read()
    m = re.search(rf"\ndef {name}\(args\) -> int:\n(.*?)\ndef ", src, re.S)
    assert m, f"could not isolate {name}'s body"
    return m.group(1)


def test_cmd_optimize_applies_exclusion_before_the_market_condition_check():
    body = _cmd_source("_cmd_optimize")
    i_excl = body.index("_apply_exclude_symbols(")
    i_mc = body.index("_apply_market_conditions(")
    assert i_excl < i_mc


def test_cmd_optimize_batch_applies_exclusion_before_the_market_condition_check():
    body = _cmd_source("_cmd_optimize_batch")
    i_excl = body.index("_apply_exclude_symbols(")
    i_mc = body.index('_apply_market_conditions("optimize-batch"')
    assert i_excl < i_mc


# --------------------------------------------------------------------------- #
# CLI wiring: the argparse flag exists on both subcommands.
# --------------------------------------------------------------------------- #
def test_optimize_and_optimize_batch_both_register_the_flag():
    src = open(_LAUNCHER, encoding="utf-8").read()
    # `op` is the `optimize` subparser, `ob` is `optimize-batch` (see main()'s subparsers setup).
    assert re.search(r'op\.add_argument\("--exclude-symbols"', src)
    assert re.search(r'ob\.add_argument\("--exclude-symbols"', src)
