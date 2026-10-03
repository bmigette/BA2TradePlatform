"""Per-package versions + minimum-version comparison -- STDLIB ONLY.

Shared by the worker sync path (``self_update`` / ``worker_client``) and by
``tools/check_package_versions.py`` (which loads this file by path, so it must import nothing
outside the standard library and must not rely on ``app`` being importable).

Policy (docs/plans/2026-10-03-package-versioning-design.md): each shared package has its own
``PACKAGE_VERSION``; ``testplatform/required_package_versions.py`` declares the MINIMUM a worker
must run. Everything here is read by TEXT (regex / ``ast.literal_eval``), never by import, for the
same reason ``self_update._read_version_literal`` is: the files must be readable from any venv
and can never execute code.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

#: import name -> directory under ``packages/``.
PACKAGE_DIRS: Dict[str, str] = {
    "ba2_common": "common",
    "ba2_providers": "providers",
    "ba2_experts": "experts",
}
PACKAGES: Tuple[str, ...] = tuple(PACKAGE_DIRS)

# Package versions are exactly YYYY.MM.NNNNN (5-digit build). The comparison parser is more
# lenient (TEST_APP_VERSION / APP_VERSION are zero-padded to 4 digits) but ALWAYS numeric.
PACKAGE_VERSION_RE = re.compile(r"\d{4}\.(0[1-9]|1[0-2])\.\d{5}\Z")
_ANY_VERSION_RE = re.compile(r"(\d{4})\.(0[1-9]|1[0-2])\.(\d+)\Z")

#: What a worker that cannot (or does not) report a package is recorded as.
UNKNOWN = "unknown"


def parse_version(value) -> Tuple[int, int, int]:
    """``"2026.10.00099" -> (2026, 10, 99)``. Raises ValueError on anything else.

    Numeric, NOT lexicographic: ``"2026.10.00099" < "2026.10.00100"`` holds either way only
    because of the zero padding, but ``"2026.10.99" > "2026.10.100"`` as strings -- so we never
    compare the raw strings.
    """
    if not isinstance(value, str):
        raise ValueError(f"version must be a string, got {type(value).__name__}")
    m = _ANY_VERSION_RE.match(value)
    if not m:
        raise ValueError(f"not a YYYY.MM.NNNNN version (month 01-12): {value!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def try_parse(value) -> Optional[Tuple[int, int, int]]:
    try:
        return parse_version(value)
    except ValueError:
        return None


def literal_from_text(text: str, name: str) -> Optional[object]:
    """Value of the top-level ``name = <literal>`` assignment in source *text*, or None."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    return None


def _literal(path: Path, name: str) -> Optional[object]:
    """Value of the top-level ``name = <literal>`` assignment in the file *path*, or None."""
    try:
        return literal_from_text(path.read_text(encoding="utf-8"), name)
    except (OSError, UnicodeDecodeError):
        return None


def package_version_file(root: Path, package: str) -> Path:
    return Path(root) / "packages" / PACKAGE_DIRS[package] / package / "version.py"


def read_package_versions(root: Path) -> Dict[str, str]:
    """``{package: PACKAGE_VERSION}`` read from the checkout at *root*; ``"unknown"`` if absent."""
    out: Dict[str, str] = {}
    for pkg in PACKAGES:
        v = _literal(package_version_file(root, pkg), "PACKAGE_VERSION")
        out[pkg] = v if isinstance(v, str) else UNKNOWN
    return out


def read_required_package_versions(root: Path) -> Dict[str, str]:
    """The master's declared minimums (``testplatform/required_package_versions.py``).

    A missing/garbled file yields ``{}``, i.e. package gating switched off, because the caller
    cannot enforce a minimum that was never declared; ``check_package_versions`` fails CI for that.
    """
    v = _literal(Path(root) / "testplatform" / "required_package_versions.py",
                 "REQUIRED_PACKAGE_VERSIONS")
    if not isinstance(v, dict):
        return {}
    return {str(k): str(x) for k, x in v.items()}


def read_ga_neutral_globs(root: Path) -> List[str]:
    v = _literal(Path(root) / "testplatform" / "ga_neutral_package_paths.py", "GA_NEUTRAL_GLOBS")
    return [str(x) for x in v] if isinstance(v, list) else []


def below_minimum(worker_packages, required: Mapping[str, str]) -> Tuple[List[str], List[str]]:
    """Compare a worker's reported package versions with the master's minimums.

    Returns ``(below, unknown)`` lists of package names:
      * ``unknown`` -- the worker did not report that package (an OLD worker reports none at all,
        so every required package lands here) or reported something unparseable. Treated as "must
        sync", never as "fine".
      * ``below``   -- reported and parseable but older than the minimum.
    """
    below: List[str] = []
    unknown: List[str] = []
    reported = worker_packages if isinstance(worker_packages, Mapping) else {}
    for pkg, minimum in required.items():
        have = try_parse(reported.get(pkg))
        if have is None:
            unknown.append(pkg)
            continue
        want = try_parse(minimum)
        if want is None:
            # A malformed MINIMUM is a master-side bug; surface it loudly rather than ignore it.
            raise ValueError(f"required version for {pkg} is malformed: {minimum!r}")
        if have < want:
            below.append(pkg)
    return below, unknown


def drift(worker_packages, master_packages: Mapping[str, str]) -> Dict[str, Tuple[str, str]]:
    """``{package: (master_version, worker_version)}`` for every package whose version differs.

    Only meaningful for a worker already known to be at/above the minimums: the differences are
    the "worker is allowed to run older code" cases that must be observable, never silent.
    """
    reported = worker_packages if isinstance(worker_packages, Mapping) else {}
    out: Dict[str, Tuple[str, str]] = {}
    for pkg, mv in master_packages.items():
        wv = reported.get(pkg, UNKNOWN)
        if wv != mv:
            out[pkg] = (mv, wv)
    return out


def worker_ahead(worker_required, master_required: Mapping[str, str]) -> List[str]:
    """Packages for which the WORKER declares a higher minimum than the master does.

    A worker's reported ``required_package_versions`` is the set of GA-relevant package changes it
    carries; a higher entry than the master's means the worker has GA-relevant code the master
    lacks (master rolled back, or the worker pulled a newer branch). It cannot be downgraded by
    ``/update`` (that only pulls), so the caller must EXCLUDE it. Unreported/unparseable entries
    are ignored (an older worker reports none; that is handled as "unknown", not "ahead").
    """
    reported = worker_required if isinstance(worker_required, Mapping) else {}
    out: List[str] = []
    for pkg, mine in master_required.items():
        theirs = try_parse(reported.get(pkg))
        want = try_parse(mine)
        if theirs is not None and want is not None and theirs > want:
            out.append(pkg)
    return out


def master_problems(packages: Mapping[str, str], required: Mapping[str, str]) -> List[str]:
    """Why the MASTER cannot enforce package minimums (empty list = it can).

    A missing, garbled or conflict-marked ``required_package_versions.py`` reads as ``{}``; that
    must refuse distributed mode loudly rather than silently switch gating off.
    """
    problems: List[str] = []
    for pkg in PACKAGES:
        own = packages.get(pkg) if isinstance(packages, Mapping) else None
        if not isinstance(own, str) or not PACKAGE_VERSION_RE.match(own):
            problems.append(f"{pkg}: own PACKAGE_VERSION {own!r} is missing or not YYYY.MM.NNNNN")
        minimum = required.get(pkg) if isinstance(required, Mapping) else None
        if not isinstance(minimum, str) or not PACKAGE_VERSION_RE.match(minimum):
            problems.append(f"{pkg}: required minimum {minimum!r} is missing or not YYYY.MM.NNNNN "
                            f"(testplatform/required_package_versions.py unreadable or incomplete?)")
        elif isinstance(own, str) and PACKAGE_VERSION_RE.match(own) and parse_version(minimum) > parse_version(own):
            problems.append(f"{pkg}: required minimum {minimum} is above the package's own version {own}")
    return problems
