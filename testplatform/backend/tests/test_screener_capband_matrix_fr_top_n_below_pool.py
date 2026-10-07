"""``tools/run_screener_capband_matrix.py`` --no-fr-top-n-below-pool passthrough (operator
decision 2026-09-29, the "ranking inert" trap -- see ``ba2_common.core.factor_ranker_topn``).

--fr-top-n-below-pool is DEFAULT ON in the launcher as of 2026-09-29: no passthrough is needed
for the default (every FactorRanker job this driver launches gets the repair automatically).
Only the OPT-OUT (--no-fr-top-n-below-pool) is ever forwarded.

Loaded BY ABSOLUTE PATH (the ``_LAUNCHER_PATH`` pattern other launcher-adjacent driver tests use,
e.g. test_screener_capband_matrix_exclude_symbols.py) rather than imported as a package, since
``tools/`` is not a package and the venv's editable finder can otherwise resolve a plain
``import`` to the MAIN checkout instead of this worktree.

Pins:
  * ``fr_top_n_below_pool_passthrough`` is [] when the flag is absent/True (the default: an
    ordinary invocation is untouched) and forwards ``--no-fr-top-n-below-pool`` when opted out.
  * ``_job_name`` digest-suffixes the job name on its own (no market-condition/exclude-symbols
    flag needed) -- an opt-out change must get a new job identity.
  * end-to-end via ``main() --dry-run``: --no-fr-top-n-below-pool changes every printed job name
    (digest suffix appears) even with none of the other passthrough sources given, and an
    ordinary invocation (no flag) prints the plain, undigested names -- even though the LAUNCHED
    FactorRanker job now behaves differently under that plain name (the repair is default-on
    inside the launcher, invisible to this driver's argv/name).

The decision-time gene is DEFAULT ON in this driver (2026-10-07) and renames every CLASSIC job
(``-timegene-<digest>``); these tests pass ``--decision-times fixed`` (legacy fixed-time naming)
so the digest they look for can only come from the opt-out. FactorRanker is a BYPASS expert: it
must never get the time gene, default or not (pinned below).
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from types import SimpleNamespace

_DIGEST_RE = re.compile(r"-d[0-9a-f]{12}\b")

# tests/ -> backend/ -> testplatform/ -> repo root, then tools/ beside it.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_SCRIPT = os.path.join(_REPO, "tools", "run_screener_capband_matrix.py")


def _driver():
    spec = importlib.util.spec_from_file_location("run_screener_capband_matrix_frtopn", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["run_screener_capband_matrix_frtopn"] = m
    spec.loader.exec_module(m)
    return m


def _args(**over):
    base = dict(exclude_symbols=None, market_condition_profile=None,
                market_condition_manifest=None, market_exit=None,
                market_condition_mode=None, search_sl_loosen=False,
                fr_top_n_below_pool=True)
    base.update(over)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# fr_top_n_below_pool_passthrough
# --------------------------------------------------------------------------- #
def test_default_true_forwards_nothing():
    d = _driver()
    assert d.fr_top_n_below_pool_passthrough(_args()) == []
    assert d.fr_top_n_below_pool_passthrough(_args(fr_top_n_below_pool=True)) == []


def test_missing_attr_defaults_to_the_launcher_default_and_forwards_nothing():
    """getattr(..., True): an argv namespace that never declared this dest (e.g. a caller not
    yet updated) is treated as the default (on), not as an opt-out."""
    d = _driver()
    ns = _args()
    del ns.fr_top_n_below_pool
    assert d.fr_top_n_below_pool_passthrough(ns) == []


def test_opt_out_forwards_the_no_flag_token():
    d = _driver()
    assert d.fr_top_n_below_pool_passthrough(_args(fr_top_n_below_pool=False)) == \
        ["--no-fr-top-n-below-pool"]


# --------------------------------------------------------------------------- #
# _job_name
# --------------------------------------------------------------------------- #
def test_job_name_changes_when_opted_out():
    d = _driver()
    base = ["ba2-test.exe", "optimize", "--expert", "FactorRanker", "--universe", "AAPL",
           "--name", "scr-large-FactorRanker"]
    n_default = d._job_name("scr-large-FactorRanker", base)
    n_opt_out = d._job_name("scr-large-FactorRanker", base + ["--no-fr-top-n-below-pool"])
    assert n_default != n_opt_out
    assert n_opt_out.startswith("scr-large-FactorRanker-d")
    assert _DIGEST_RE.search(n_opt_out)


def test_job_name_ignores_name_parallel_workers_tokens():
    d = _driver()
    cmd1 = ["optimize", "--no-fr-top-n-below-pool", "--name", "job-a", "--parallel", "4"]
    cmd2 = ["optimize", "--no-fr-top-n-below-pool", "--name", "job-b", "--parallel", "8",
           "--workers", "remote150"]
    assert d._job_name("job-a", cmd1) == d._job_name("job-b", cmd2).replace("job-b", "job-a")


# --------------------------------------------------------------------------- #
# End-to-end via main() --dry-run (no subprocess is ever launched under --dry-run)
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


def test_dry_run_default_prints_plain_undigested_names(monkeypatch, capsys):
    """No flag given -- the driver's argv/job names are byte-identical to before this change,
    even though the LAUNCHED FactorRanker job now runs with the repair by default (the launcher's
    default, invisible to this driver)."""
    rc, out = _run_dry(monkeypatch, capsys, _FIXED_TIME)
    assert rc == 0
    assert "scr-large-FMPRating-S1" in out
    assert "scr-large-FactorRanker" in out
    assert not _DIGEST_RE.search(out)


def test_dry_run_default_gene_on_never_reaches_the_factorranker_job(monkeypatch, capsys):
    """Default naming (gene ON): the classic job is renamed ``-timegene-<digest>``, the FactorRanker
    bypass job keeps its plain name (job_dt_times is None for it, so no --decision-times
    token is built) and the driver prints its NOTE (a time gene on a bypass expert would be dead
    and the launcher refuses it)."""
    rc, out = _run_dry(monkeypatch, capsys, [])
    assert rc == 0
    todo = [ln for ln in out.splitlines() if ln.strip().startswith("TODO")]
    fr = [ln for ln in todo if "FactorRanker" in ln]
    classic = [ln for ln in todo if "FMPRating" in ln]
    assert len(fr) == 1 and len(classic) == 1
    assert "timegene" not in fr[0] and "scr-large-FactorRanker " in fr[0]
    assert re.search(r"-timegene-d[0-9a-f]{12}\b", classic[0]), classic[0]
    assert "NOTE scr-large-FactorRanker: bypass expert, decision time stays fixed" in out


def test_dry_run_with_opt_out_digests_every_job_name(monkeypatch, capsys):
    """Given alone (no market-condition/exclude-symbols flags), --no-fr-top-n-below-pool must
    still fold into the digest."""
    rc, out = _run_dry(monkeypatch, capsys, _FIXED_TIME + ["--no-fr-top-n-below-pool"])
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip().startswith(("TODO", "DONE"))]
    assert len(lines) == 2  # FMPRating S1 + FactorRanker, per --skip-experts above
    for ln in lines:
        assert _DIGEST_RE.search(ln), f"job line missing opt-out digest suffix: {ln!r}"
