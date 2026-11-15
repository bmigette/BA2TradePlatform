"""Forbidden walk-forward optimize flags vs argparse abbreviations, measured against the REAL launcher
``optimize`` parser (``ba2test_launcher._optimize_parser``): every abbreviation of every forbidden flag
(the three seed-file flags included) is refused in both spellings, and no valid flag is -- ``--seed`` (the
RNG seed, a prefix of the seed-file flags) included, because argparse takes an exact match first."""
from __future__ import annotations

import pytest

from app.services import walk_forward as WF
from tests.test_walk_forward import _launcher_mod

BASE = ["--expert", "FMPRating", "--universe", "AAPL", "--start", "2020-01-01", "--end", "2020-12-31"]


@pytest.fixture(scope="module")
def parser():
    return _launcher_mod()._optimize_parser()


def _accepts(parser, tokens):
    try:
        return parser.parse_args(tokens)
    except SystemExit:
        return None


def test_the_real_parser_reads_abbreviations_of_the_seed_file_flags(parser):
    assert parser.allow_abbrev is True
    assert _accepts(parser, BASE + ["--seed-pop", "b.json"]).seed_population == "b.json"
    assert _accepts(parser, BASE + ["--seed-pop=b.json"]).seed_population == "b.json"
    assert _accepts(parser, BASE + ["--seed-gen", "g.json"]).seed_genomes == "g.json"
    assert _accepts(parser, BASE + ["--warm-start", "3"]).warm_start_from == 3
    assert _accepts(parser, BASE + ["--seed", "7"]).seed == 7          # exact match: the RNG seed


def test_every_abbreviation_of_every_forbidden_flag_is_refused(parser):
    resolved = 0
    for flag in WF.FORBIDDEN_OPTIMIZE_FLAGS:
        action = parser._option_string_actions[flag]
        for n in range(3, len(flag) + 1):
            abbr = flag[:n]
            if abbr in WF.EXACT_FLAGS_SHADOWING_FORBIDDEN:
                continue
            toks = [abbr] if action.nargs == 0 else [abbr, "2019-06-30" if flag in ("--start", "--end") else "7"]
            ns = _accepts(parser, BASE + toks)
            if ns is not None and getattr(ns, action.dest) not in (None, False, "2020-01-01", "2020-12-31"):
                resolved += 1
            with pytest.raises(WF.WalkForwardError, match="must not contain"):
                WF.check_user_optimize_args(toks)
            with pytest.raises(WF.WalkForwardError, match="must not contain"):
                WF.check_user_optimize_args([f"{abbr}=7"])
    assert resolved > 30


def test_the_only_exact_flags_shadowing_a_forbidden_one_are_the_allowed_exceptions(parser):
    flags = {s for a in parser._actions for s in a.option_strings if s.startswith("--")}
    shadowing = {f for f in flags for fb in WF.FORBIDDEN_OPTIMIZE_FLAGS if f != fb and fb.startswith(f)}
    assert shadowing == set(WF.EXACT_FLAGS_SHADOWING_FORBIDDEN) == {"--seed"}


def test_no_valid_optimize_flag_is_refused(parser):
    flags = sorted({s for a in parser._actions for s in a.option_strings if s.startswith("--")})
    allowed = [f for f in flags if f not in WF.FORBIDDEN_OPTIMIZE_FLAGS and f != "--help"]
    assert len(allowed) > 50 and "--seed" in allowed
    for f in allowed:
        WF.check_user_optimize_args([f, "x"])
        WF.check_user_optimize_args([f"{f}=x"])
