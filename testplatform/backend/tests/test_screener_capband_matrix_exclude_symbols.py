"""``tools/run_screener_capband_matrix.py`` --exclude-symbols passthrough (goal2027atr split-basis
fix, docs/strategy_research/atr_grid/excluded_symbols.txt drops IAC from every -atr27 S1-S7 job).

Loaded BY ABSOLUTE PATH (the ``_LAUNCHER_PATH`` pattern other launcher-adjacent driver tests use,
e.g. test_options2_matrix_script.py) rather than imported as a package, since ``tools/`` is not a
package and the venv's editable finder can otherwise resolve a plain ``import`` to the MAIN
checkout instead of this worktree.

Pins:
  * ``exclude_symbols_passthrough`` is [] when the flag is absent (an ordinary invocation is
    untouched) and forwards ``--exclude-symbols <value>`` verbatim when given.
  * ``_job_name`` digest-suffixes the job name -- it does not simply pass tokens through, so a
    same-named job under a different exclusion is never confused with a prior run.
  * end-to-end via ``main() --dry-run``: an exclusion changes every printed job name (digest
    suffix appears) even with NONE of the five market-condition flags given, and an ordinary
    invocation (no --exclude-symbols) prints the plain, undigested names.

The decision-time gene is DEFAULT ON in this driver (2026-10-07), which by itself renames every
classic job (``-timegene-<digest>``). The exclusion tests therefore pass ``--decision-times fixed``
(the legacy fixed-time naming) so the digest they look for can only come from the exclusion; one
test per file pins the default (gene ON) naming.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from types import SimpleNamespace

import pytest

_DIGEST_RE = re.compile(r"-d[0-9a-f]{12}\b")

# tests/ -> backend/ -> testplatform/ -> repo root, then tools/ beside it.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_SCRIPT = os.path.join(_REPO, "tools", "run_screener_capband_matrix.py")


def _driver():
    spec = importlib.util.spec_from_file_location("run_screener_capband_matrix_excl", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["run_screener_capband_matrix_excl"] = m
    spec.loader.exec_module(m)
    return m


def _args(**over):
    base = dict(exclude_symbols=None, market_condition_profile=None,
                market_condition_manifest=None, market_exit=None,
                market_condition_mode=None, search_sl_loosen=False)
    base.update(over)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# exclude_symbols_passthrough
# --------------------------------------------------------------------------- #
def test_absent_flag_forwards_nothing():
    d = _driver()
    assert d.exclude_symbols_passthrough(_args()) == []


def test_given_flag_forwards_the_raw_value_verbatim():
    d = _driver()
    assert d.exclude_symbols_passthrough(_args(exclude_symbols="IAC")) == \
        ["--exclude-symbols", "IAC"]
    at_file = "@docs/strategy_research/atr_grid/excluded_symbols.txt"
    assert d.exclude_symbols_passthrough(_args(exclude_symbols=at_file)) == \
        ["--exclude-symbols", at_file]


# --------------------------------------------------------------------------- #
# _job_name
# --------------------------------------------------------------------------- #
def test_job_name_gets_a_digest_suffix_from_the_resolved_argv():
    d = _driver()
    cmd = ["ba2-test.exe", "optimize", "--expert", "FMPRating", "--universe", "AAPL",
          "--name", "scr-large-FMPRating-S1", "--exclude-symbols", "IAC"]
    name = d._job_name("scr-large-FMPRating-S1", cmd)
    assert name.startswith("scr-large-FMPRating-S1-d")
    assert name != "scr-large-FMPRating-S1"


def test_job_name_changes_when_the_exclusion_changes():
    """A different --exclude-symbols value must never collide with (resume) a different
    exclusion's row -- same rationale as the market-condition digest."""
    d = _driver()
    base = ["ba2-test.exe", "optimize", "--expert", "FMPRating", "--universe", "AAPL",
           "--name", "scr-large-FMPRating-S1"]
    n_iac = d._job_name("scr-large-FMPRating-S1", base + ["--exclude-symbols", "IAC"])
    n_other = d._job_name("scr-large-FMPRating-S1", base + ["--exclude-symbols", "IAC,MARA"])
    n_none = d._job_name("scr-large-FMPRating-S1", base)
    assert len({n_iac, n_other, n_none}) == 3


def test_job_name_ignores_name_parallel_workers_tokens():
    """The digest must be stable under a resubmit that only changes --name/--parallel/--workers
    (the three the function explicitly excludes), or a resumed job would get a new identity for
    no real config change."""
    d = _driver()
    cmd1 = ["optimize", "--exclude-symbols", "IAC", "--name", "job-a", "--parallel", "4"]
    cmd2 = ["optimize", "--exclude-symbols", "IAC", "--name", "job-b", "--parallel", "8",
           "--workers", "remote150"]
    assert d._job_name("job-a", cmd1) == d._job_name("job-b", cmd2).replace("job-b", "job-a")


# --------------------------------------------------------------------------- #
# End-to-end via main() --dry-run (no subprocess is ever launched under --dry-run, and
# _completed_names() swallows a DB-connect failure and returns an empty set, so this needs no
# fixture/DB setup -- see tools/run_screener_capband_matrix.py's own dry-run/_completed_names
# docstrings).
# --------------------------------------------------------------------------- #
#: the legacy fixed-time naming (the decision-time gene is default-ON in this driver)
_FIXED_TIME = ["--decision-times", "fixed"]


def _run_dry(monkeypatch, capsys, extra_argv):
    d = _driver()
    argv = ["run_screener_capband_matrix.py", "--dry-run", "--bands", "large",
           "--strategies", "S1",
           "--skip-experts", "FMPEarningsDrift,FMPInsiderClusterBuy,DeterministicScorer",
           ] + extra_argv
    monkeypatch.setattr(sys, "argv", argv)
    rc = d.main()
    out = capsys.readouterr().out
    return rc, out


def test_dry_run_without_the_flag_prints_plain_undigested_names(monkeypatch, capsys):
    rc, out = _run_dry(monkeypatch, capsys, _FIXED_TIME)
    assert rc == 0
    assert "scr-large-FMPRating-S1" in out
    assert "scr-large-FactorRanker" in out
    assert not _DIGEST_RE.search(out)


def test_dry_run_default_decision_time_gene_renames_classic_jobs_only(monkeypatch, capsys):
    """Default (no --decision-times): the gene is ON, so the classic job carries the
    ``-timegene-<digest>`` suffix; the exclusion flag is absent so nothing else digests. The
    FactorRanker bypass job keeps its plain name (it gets no schedule genes)."""
    rc, out = _run_dry(monkeypatch, capsys, [])
    assert rc == 0
    fmp = [ln for ln in out.splitlines() if "scr-large-FMPRating-S1" in ln and "TODO" in ln]
    # (every --screener job name also carries the static-universe rule token, ``-sup1``)
    assert len(fmp) == 1 and re.search(r"-timegene-sup1-lds2-d[0-9a-f]{12}\b", fmp[0]), fmp
    fr = [ln for ln in out.splitlines() if ln.strip().startswith("TODO") and "FactorRanker" in ln]
    assert len(fr) == 1 and "scr-large-FactorRanker-sup1 " in fr[0] and "timegene" not in fr[0], fr


def test_dry_run_with_exclude_symbols_digests_every_job_name(monkeypatch, capsys):
    """Given alone (no market-condition flags), --exclude-symbols must still fold into the
    digest -- this is the case the operator actually runs for goal2027atr (dropping IAC from
    every job, treatment AND the all-off control)."""
    rc, out = _run_dry(monkeypatch, capsys, _FIXED_TIME + ["--exclude-symbols", "IAC"])
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip().startswith(("TODO", "DONE"))]
    assert len(lines) == 2  # FMPRating S1 + FactorRanker, per --skip-experts above
    for ln in lines:
        assert _DIGEST_RE.search(ln), f"job line missing exclude-symbols digest suffix: {ln!r}"
