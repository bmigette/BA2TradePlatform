"""tools/check_package_versions.py on synthetic git repos (never the real repo's history).

Scenarios: GA-relevant change without a bump -> fail; allowlisted change with only a
PACKAGE_VERSION bump -> pass; GA-relevant change with the minimum raised -> pass; docs-only
change -> pass; shallow/no-base -> consistency-only, never a failure for lack of history.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from app.services import self_update

REPO = self_update.resolve_repo_root()
_spec = importlib.util.spec_from_file_location("check_package_versions_tool",
                                               REPO / "tools" / "check_package_versions.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

PKGS = {"ba2_common": "common", "ba2_providers": "providers", "ba2_experts": "experts"}
V1, V2 = "2026.10.00001", "2026.10.00002"
_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def _git(root, *a):
    subprocess.run(["git", *a], cwd=root, env=_ENV, check=True, capture_output=True)


def _w(root: Path, rel: str, body: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _set_version(root, pkg, v):
    d = PKGS[pkg]
    _w(root, f"packages/{d}/{pkg}/version.py", f'PACKAGE_VERSION = "{v}"\n')
    _w(root, f"packages/{d}/pyproject.toml", f'[project]\nname = "x"\nversion = "{v}"\ndescription = "d"\n')


def _set_required(root, **kw):
    req = {p: V1 for p in PKGS}
    req.update(kw)
    body = "REQUIRED_PACKAGE_VERSIONS = {\n" + "".join(f'    "{k}": "{v}",\n' for k, v in req.items()) + "}\n"
    _w(root, "testplatform/required_package_versions.py", body)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for pkg, d in PKGS.items():
        _set_version(root, pkg, V1)
        _w(root, f"packages/{d}/{pkg}/__init__.py", "")
        _w(root, f"packages/{d}/{pkg}/core/engine.py", "x = 1\n")
        _w(root, f"packages/{d}/{pkg}/core/ibkr_mapping.py", "y = 1\n")
        _w(root, f"packages/{d}/README.md", "docs\n")
    _set_required(root)
    _w(root, "testplatform/version.py", 'TEST_APP_VERSION = "2026.10.0010"\n')
    # the real allowlist, so the test exercises what ships
    _w(root, "testplatform/ga_neutral_package_paths.py",
       (REPO / "testplatform" / "ga_neutral_package_paths.py").read_text(encoding="utf-8"))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "work")
    return root


def _commit(root):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")


def _run(root, **kw):
    lines: list[str] = []
    rc = tool.run_check(root, base="main", out=lines.append, **kw)
    return rc, "\n".join(lines)


def test_clean_tree_with_no_changes_passes(repo):
    rc, out = _run(repo)
    assert rc == 0, out


def test_ga_relevant_change_without_any_bump_fails_rule_1_with_a_fix_hint(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1
    assert "[rule 1]" in out and "ba2_common" in out
    assert "packages/common/ba2_common/version.py" in out and "increase PACKAGE_VERSION" in out


def test_ga_relevant_change_with_bump_but_no_minimum_fails_rule_2(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")
    _set_version(repo, "ba2_common", V2)
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1
    assert "[rule 2]" in out and "[rule 1]" not in out
    assert "REQUIRED_PACKAGE_VERSIONS" in out or "required_package_versions.py" in out
    assert "GA_NEUTRAL_GLOBS" in out


def test_ga_relevant_change_with_bump_and_minimum_raised_passes(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")
    _set_version(repo, "ba2_common", V2)
    _set_required(repo, ba2_common=V2)
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 0, out


def test_ga_relevant_change_with_test_app_version_bump_passes_rule_2(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")
    _set_version(repo, "ba2_common", V2)
    _w(repo, "testplatform/version.py", 'TEST_APP_VERSION = "2026.10.0011"\n')
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 0, out


def test_allowlisted_change_with_only_a_package_bump_passes(repo):
    _w(repo, "packages/common/ba2_common/core/ibkr_mapping.py", "y = 2\n")
    _set_version(repo, "ba2_common", V2)
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 0, out


def test_allowlisted_change_without_a_package_bump_still_fails_rule_1(repo):
    _w(repo, "packages/common/ba2_common/core/ibkr_mapping.py", "y = 2\n")
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1 and "[rule 1]" in out and "[rule 2]" not in out


def test_mixed_allowlisted_and_relevant_change_needs_the_minimum(repo):
    _w(repo, "packages/common/ba2_common/core/ibkr_mapping.py", "y = 2\n")
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")
    _set_version(repo, "ba2_common", V2)
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1 and "[rule 2]" in out and "engine.py" in out and "ibkr_mapping.py" not in out.split("[rule 2]")[1].split("FIX")[0]


def test_docs_and_tests_only_change_passes(repo):
    _w(repo, "packages/common/README.md", "more docs\n")
    _w(repo, "packages/common/tests/test_x.py", "def test_x(): pass\n")
    _w(repo, "packages/common/ba2_common/tests/test_y.py", "def test_y(): pass\n")
    _w(repo, "packages/common/ba2_common/NOTES.md", "n\n")
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 0, out


def test_change_in_one_package_does_not_demand_a_bump_in_another(repo):
    _w(repo, "packages/experts/ba2_experts/core/engine.py", "x = 2\n")
    _set_version(repo, "ba2_experts", V2)
    _set_required(repo, ba2_experts=V2)
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 0, out


def test_pyproject_dependency_change_counts_but_a_version_only_edit_does_not(repo):
    d = repo / "packages/providers/pyproject.toml"
    d.write_text(d.read_text(encoding="utf-8") + 'dependencies = ["pandas"]\n', encoding="utf-8")
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1 and "[rule 1]" in out and "ba2_providers" in out


def test_pyproject_version_out_of_sync_with_version_py_fails_consistency(repo):
    _w(repo, "packages/common/pyproject.toml", '[project]\nversion = "2026.10.00009"\n')
    rc, out = _run(repo)
    assert rc == 1 and "pyproject" in out


def test_minimum_above_package_version_fails_consistency(repo):
    _set_required(repo, ba2_common="2026.10.00050")
    rc, out = _run(repo)
    assert rc == 1 and "ABOVE" in out


def test_malformed_package_version_fails_consistency(repo):
    _set_version(repo, "ba2_common", "2026.10.1")
    rc, out = _run(repo)
    assert rc == 1 and "YYYY.MM.NNNNN" in out


def test_explicit_base_that_does_not_resolve_is_an_error(repo):
    lines: list[str] = []
    rc = tool.run_check(repo, base="no-such-ref", out=lines.append)
    out = "\n".join(lines)
    assert rc == 1 and "does not resolve" in out and "fetch-depth: 0" in out


def test_explicit_base_from_the_environment_is_also_an_error(repo, monkeypatch):
    monkeypatch.setenv("BA2_VERSION_CHECK_BASE", "no-such-ref")
    lines: list[str] = []
    assert tool.run_check(repo, out=lines.append) == 1


def test_automatic_base_missing_degrades_to_a_consistency_check_and_says_so(repo, monkeypatch):
    monkeypatch.delenv("BA2_VERSION_CHECK_BASE", raising=False)
    _git(repo, "branch", "-m", "main", "trunk")          # no origin/dev, origin/main, dev, main
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")  # would fail rule 1 with a base
    _commit(repo)
    lines: list[str] = []
    rc = tool.run_check(repo, out=lines.append)
    out = "\n".join(lines)
    assert rc == 0, out
    assert "base ref unavailable" in out and "SKIPPED" in out


def test_not_a_git_repo_degrades_the_same_way_with_automatic_base(tmp_path, monkeypatch):
    monkeypatch.delenv("BA2_VERSION_CHECK_BASE", raising=False)
    for pkg in PKGS:
        _set_version(tmp_path, pkg, V1)
    _set_required(tmp_path)
    lines: list[str] = []
    assert tool.run_check(tmp_path, out=lines.append) == 0
    assert any("SKIPPED" in ln for ln in lines)


def test_minimum_raised_to_something_other_than_the_new_version_fails(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")
    _set_version(repo, "ba2_common", "2026.10.00005")
    _set_required(repo, ba2_common=V2)                  # raised, but not to 00005
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1 and "must EQUAL the new PACKAGE_VERSION" in out


def test_widening_the_allowlist_in_the_same_diff_does_not_exempt_the_change(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")
    _set_version(repo, "ba2_common", V2)
    n = repo / "testplatform/ga_neutral_package_paths.py"
    n.write_text(n.read_text(encoding="utf-8").replace(
        "GA_NEUTRAL_GLOBS = [", 'GA_NEUTRAL_GLOBS = [\n    "packages/common/ba2_common/core/engine.py",'), encoding="utf-8")
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1
    assert "[rule 3]" in out and "--allow-neutral-change" in out
    assert "[rule 2]" in out, "the base's allowlist must still judge engine.py as GA-relevant"


def test_allow_neutral_change_flag_accepts_a_reviewed_allowlist_edit(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")
    _set_version(repo, "ba2_common", V2)
    n = repo / "testplatform/ga_neutral_package_paths.py"
    n.write_text(n.read_text(encoding="utf-8").replace(
        "GA_NEUTRAL_GLOBS = [", 'GA_NEUTRAL_GLOBS = [\n    "packages/common/ba2_common/core/engine.py",'), encoding="utf-8")
    _commit(repo)
    rc, out = _run(repo, allow_neutral_change=True)
    assert rc == 0, out


def test_docstring_and_comment_only_change_is_neutral_but_still_needs_a_bump(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", '"""doc."""\nx = 1  # comment\n')
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1 and "[rule 1]" in out and "[rule 2]" not in out
    _set_version(repo, "ba2_common", V2)
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 0, out


def test_a_code_change_next_to_a_docstring_is_still_ga_relevant(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", '"""doc."""\nx = 2\n')
    _set_version(repo, "ba2_common", V2)
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1 and "[rule 2]" in out


def test_deleting_a_shipped_file_needs_bump_and_minimum(repo):
    (repo / "packages/common/ba2_common/core/engine.py").unlink()
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1 and "[rule 1]" in out
    _set_version(repo, "ba2_common", V2)
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1 and "[rule 2]" in out


def test_renaming_a_shipped_file_is_a_delete_plus_an_add(repo):
    _git(repo, "mv", "packages/common/ba2_common/core/engine.py", "packages/common/ba2_common/core/engine2.py")
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 1 and "[rule 1]" in out
    _set_version(repo, "ba2_common", V2)
    _set_required(repo, ba2_common=V2)
    _commit(repo)
    rc, out = _run(repo)
    assert rc == 0, out


def test_month_13_package_version_fails_consistency(repo):
    _set_version(repo, "ba2_common", "2026.13.00001")
    rc, out = _run(repo)
    assert rc == 1 and "YYYY.MM.NNNNN" in out


def test_include_worktree_sees_uncommitted_changes(repo):
    _w(repo, "packages/common/ba2_common/core/engine.py", "x = 2\n")  # uncommitted
    rc_head, _ = _run(repo)
    rc_wt, out = _run(repo, include_worktree=True)
    assert rc_head == 0 and rc_wt == 1 and "[rule 1]" in out


def test_the_real_repository_tree_is_consistent():
    """Rule 0 on this very checkout (no git history needed)."""
    assert tool.check_consistency(REPO) == []
