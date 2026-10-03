#!/usr/bin/env python
"""Guard: a change under ``packages/`` must bump the package version, and say whether GA cares.

Usage (stdlib only, fast; run from anywhere inside the repo):

    python tools/check_package_versions.py                  # diff vs origin/dev (or main) ... HEAD
    python tools/check_package_versions.py --base origin/main
    python tools/check_package_versions.py --include-worktree   # also uncommitted/staged changes
    python tools/check_package_versions.py --base HEAD~3 --root /path/to/repo

Rules (docs/plans/2026-10-03-package-versioning-design.md):

  0. Consistency of the CURRENT tree (always runs): each package's ``PACKAGE_VERSION`` is
     ``YYYY.MM.NNNNN``, equals its ``pyproject.toml`` version, and
     ``testplatform/required_package_versions.py`` declares every package with a minimum that is
     well-formed and not above the package's version.
  1. Any change to SHIPPED package code (``packages/<dir>/<pkg>/**`` minus tests/docs, and
     ``packages/<dir>/pyproject.toml`` beyond its ``version =`` line) must bump that package's
     ``PACKAGE_VERSION``.
  2. If any of that shipped code is NOT matched by ``GA_NEUTRAL_GLOBS``
     (``testplatform/ga_neutral_package_paths.py``), the package's entry in
     ``REQUIRED_PACKAGE_VERSIONS`` must ALSO be raised in the same diff (or ``TEST_APP_VERSION``
     bumped): the change can affect GA results, so every worker must take it.

If the base ref is not available (a shallow CI checkout, no git) only rule 0 runs and the tool
says so; it never fails CI for lack of history. Exit status: 0 ok, 1 violations.
"""
from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_PV_PATH = _HERE.parent / "testplatform" / "backend" / "app" / "services" / "package_versions.py"
_spec = importlib.util.spec_from_file_location("_ba2_package_versions", _PV_PATH)
pv = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = pv  # dataclass/typing helpers want the module registered
_spec.loader.exec_module(pv)

BASE_CANDIDATES = ("origin/dev", "origin/main", "dev", "main")
REQUIRED_FILE = "testplatform/required_package_versions.py"
NEUTRAL_FILE = "testplatform/ga_neutral_package_paths.py"
TEST_VERSION_FILE = "testplatform/version.py"


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=str(root),
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _resolve_base(root: Path, base: Optional[str]) -> Tuple[Optional[str], str]:
    """(merge-base sha, human label) or (None, reason it is unavailable)."""
    candidates = [base] if base else [os.environ.get("BA2_VERSION_CHECK_BASE"), *BASE_CANDIDATES]
    tried = []
    for c in filter(None, candidates):
        tried.append(c)
        if _git(root, "rev-parse", "--verify", "--quiet", f"{c}^{{commit}}") is None:
            continue
        mb = _git(root, "merge-base", c, "HEAD")
        if mb and mb.strip():
            return mb.strip(), c
    return None, f"none of {tried} resolves to a commit sharing history with HEAD"


def _read(root: Path, ref: Optional[str], rel: str) -> Optional[str]:
    """Text of *rel* at git *ref*, or from disk when *ref* is None. None if absent."""
    if ref is None:
        try:
            return (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    return _git(root, "show", f"{ref}:{rel}")


def _lit(root, ref, rel, name):
    text = _read(root, ref, rel)
    return None if text is None else pv.literal_from_text(text, name)


def _pyproject_version(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else None


def is_shipped(rel: str, pkg: str) -> bool:
    """True if *rel* (repo-relative, posix) is code/metadata that ships in package *pkg*."""
    d = pv.PACKAGE_DIRS[pkg]
    if rel == f"packages/{d}/pyproject.toml":
        return True
    prefix = f"packages/{d}/{pkg}/"
    if not rel.startswith(prefix):
        return False
    parts = rel[len(prefix):].split("/")
    name = parts[-1]
    if "tests" in parts[:-1] or "test_files" in parts[:-1] or "docs" in parts[:-1]:
        return False
    if name.endswith(".md") or name.startswith("test_") or name == "conftest.py":
        return False
    return True


def _is_neutral(rel: str, globs: List[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, g) for g in globs)


def check_consistency(root: Path) -> List[str]:
    """Rule 0: internal consistency of the tree at *root*."""
    problems: List[str] = []
    versions = pv.read_package_versions(root)
    required = pv.read_required_package_versions(root)
    for pkg in pv.PACKAGES:
        d = pv.PACKAGE_DIRS[pkg]
        vfile = f"packages/{d}/{pkg}/version.py"
        v = versions[pkg]
        if not pv.PACKAGE_VERSION_RE.match(v):
            problems.append(f"{vfile}: PACKAGE_VERSION {v!r} is not YYYY.MM.NNNNN (5-digit build).")
        try:
            ppv = _pyproject_version((root / f"packages/{d}/pyproject.toml").read_text(encoding="utf-8"))
        except OSError:
            ppv = None
        if ppv != v:
            problems.append(f"packages/{d}/pyproject.toml: version {ppv!r} != {vfile} PACKAGE_VERSION "
                            f"{v!r}. Set the pyproject `version` to the same string (one number, two files).")
        if pkg not in required:
            problems.append(f"{REQUIRED_FILE}: no REQUIRED_PACKAGE_VERSIONS entry for {pkg!r}.")
            continue
        rv = pv.try_parse(required[pkg])
        have = pv.try_parse(v)
        if rv is None or not pv.PACKAGE_VERSION_RE.match(required[pkg]):
            problems.append(f"{REQUIRED_FILE}: {pkg} minimum {required[pkg]!r} is not YYYY.MM.NNNNN.")
        elif have is not None and rv > have:
            problems.append(f"{REQUIRED_FILE}: {pkg} minimum {required[pkg]} is ABOVE the package's own "
                            f"PACKAGE_VERSION {v}; no worker could ever satisfy it.")
    return problems


def check_diff(root: Path, base_sha: str, include_worktree: bool) -> List[str]:
    """Rules 1 and 2 for the diff base_sha..HEAD (or ..worktree)."""
    head_ref: Optional[str] = None if include_worktree else "HEAD"
    spec = [base_sha] if include_worktree else [f"{base_sha}...HEAD"]
    out = _git(root, "diff", "--name-only", "--no-renames", "-z", *spec)
    if out is None:
        return [f"git diff against {base_sha} failed; cannot evaluate rules 1 and 2."]
    changed = [p for p in out.split("\0") if p]
    globs = pv.read_ga_neutral_globs(root)
    problems: List[str] = []

    base_test = _lit(root, base_sha, TEST_VERSION_FILE, "TEST_APP_VERSION")
    head_test = _lit(root, head_ref, TEST_VERSION_FILE, "TEST_APP_VERSION")
    test_bumped = pv.try_parse(head_test) is not None and (
        pv.try_parse(base_test) is None or pv.try_parse(head_test) > pv.try_parse(base_test))
    base_req = _lit(root, base_sha, REQUIRED_FILE, "REQUIRED_PACKAGE_VERSIONS") or {}
    head_req = _lit(root, head_ref, REQUIRED_FILE, "REQUIRED_PACKAGE_VERSIONS") or {}

    for pkg in pv.PACKAGES:
        d = pv.PACKAGE_DIRS[pkg]
        vfile = f"packages/{d}/{pkg}/version.py"
        shipped = []
        for rel in changed:
            if rel == vfile or not is_shipped(rel, pkg):
                continue
            if rel == f"packages/{d}/pyproject.toml":
                diff = _git(root, "diff", "-U0", "--no-renames", *spec, "--", rel) or ""
                body = [ln for ln in diff.splitlines()
                        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
                if all(re.match(r"[+-]version\s*=", ln) for ln in body):
                    continue  # only the version line (the bump itself)
            shipped.append(rel)
        if not shipped:
            continue
        sample = ", ".join(shipped[:4]) + (f" (+{len(shipped) - 4} more)" if len(shipped) > 4 else "")

        bv = pv.try_parse(_lit(root, base_sha, vfile, "PACKAGE_VERSION"))
        hv = pv.try_parse(_lit(root, head_ref, vfile, "PACKAGE_VERSION"))
        bumped = hv is not None and (bv is None or hv > bv)
        if not bumped:
            problems.append(
                f"[rule 1] {pkg}: shipped code changed ({sample}) but PACKAGE_VERSION was not bumped.\n"
                f"    FIX: increase PACKAGE_VERSION in {vfile} (and the same string as `version` in "
                f"packages/{d}/pyproject.toml). Every package change bumps its own version.")
        non_neutral = [r for r in shipped if not _is_neutral(r, globs)]
        if non_neutral:
            b = pv.try_parse(base_req.get(pkg))
            h = pv.try_parse(head_req.get(pkg))
            raised = h is not None and (b is None or h > b)
            if not (raised or test_bumped):
                nsample = ", ".join(non_neutral[:4]) + (
                    f" (+{len(non_neutral) - 4} more)" if len(non_neutral) > 4 else "")
                problems.append(
                    f"[rule 2] {pkg}: change not declared GA-neutral ({nsample}) and the required "
                    f"minimum was not raised.\n"
                    f"    FIX, one of: (a) this CAN affect GA/backtest results: set {pkg!r} in "
                    f"{REQUIRED_FILE} to the new PACKAGE_VERSION (workers will then re-sync);\n"
                    f"    (b) it CANNOT (e.g. broker-only code the backtest engine never imports): "
                    f"add a narrow glob for these paths to GA_NEUTRAL_GLOBS in {NEUTRAL_FILE} and "
                    f"say why in the commit message.")
    return problems


def run_check(root: Path, base: Optional[str] = None, include_worktree: bool = False,
              out=print) -> int:
    root = Path(root).resolve()
    problems = check_consistency(root)
    base_sha, label = _resolve_base(root, base)
    if base_sha is None:
        out(f"NOTE: base ref unavailable ({label}); checked this tree's internal consistency only. "
            f"Rules 1-2 (diff vs base) were SKIPPED.")
    else:
        out(f"Checking packages/ changes in {base_sha[:10]} ({label}) ... "
            f"{'worktree' if include_worktree else 'HEAD'}")
        problems += check_diff(root, base_sha, include_worktree)
    if problems:
        out("FAIL: package version policy violated:")
        for p in problems:
            out(" - " + p)
        out("See CLAUDE.md 'Versioning' and docs/plans/2026-10-03-package-versioning-design.md.")
        return 1
    out("OK: package versions consistent" + ("" if base_sha is None else " and bumped as required") + ".")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="base ref (default: $BA2_VERSION_CHECK_BASE, origin/dev, origin/main, dev, main)")
    ap.add_argument("--root", default=str(_HERE.parent), help="repository root (default: this repo)")
    ap.add_argument("--include-worktree", action="store_true",
                    help="compare the working tree (uncommitted + staged changes) instead of HEAD")
    a = ap.parse_args(argv)
    return run_check(Path(a.root), a.base, a.include_worktree)


if __name__ == "__main__":
    sys.exit(main())
