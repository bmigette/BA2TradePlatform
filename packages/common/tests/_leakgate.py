"""Shared plumbing for the Amendment-A1 import-leak gates.

Each gate proves a purity property by importing a module in a FRESH interpreter
and inspecting that interpreter's ``sys.modules``. The child process does **not**
inherit pytest's ``pythonpath`` ini setting -- pytest applies it to its own
``sys.path``, not to the environment -- and this repo's editable installs of
ba2_common / ba2_providers / ba2_experts point at sibling checkouts that need not
exist on a given machine. Left to itself the child therefore dies with
``ModuleNotFoundError: No module named 'ba2_common'``, the gate reads the empty
stdout as "not CLEAN", and it fails for a reason that has nothing to do with
purity. That is how these gates rotted into ignorable noise.

``run_probe`` fixes it at the source: it hands the child an explicit
``PYTHONPATH`` built from the repo root, derived from *this file* so it holds no
matter which directory pytest was started in, and runs the child with ``-P`` so
its import surface is exactly that path plus site-packages -- nothing accidental
from the cwd.

Two properties are deliberately preserved:

* **The gate still fails on a real leak.** The child is given *more* importable
  code, never less: the repo root is on the path too, so a genuine back-edge into
  ``ba2_trade_platform`` resolves and gets reported as ``LEAK:...`` instead of
  crashing the probe.
* **A broken environment is never silently a pass.** A child that cannot import
  what it was told to import exits non-zero, and ``run_probe`` raises a distinct
  "probe failed to run" failure rather than letting empty stdout masquerade as a
  leak (or, worse, as a pass).

An identical copy of this module lives in each of ``packages/{common,providers,
experts}/tests/``. That is deliberate: the three are separately installable
distributions whose suites must stay runnable from inside the package (where a
helper hoisted to ``packages/`` would sit above the rootdir and never load).
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

# .../<repo>/packages/<pkg>/tests/_leakgate.py  ->  <repo>
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# Every in-repo source root a probe may need: the three package roots (so
# `import ba2_common` etc. resolve to the code in THIS checkout) and the repo
# root itself (so `ba2_trade_platform` is genuinely importable -- a forbidden
# module that cannot be imported would make that entry of the gate vacuous).
# Entries that do not exist are ignored by Python, so this stays correct if a
# package is ever vendored on its own.
PROBE_PATH = [
    str(REPO_ROOT / "packages" / name) for name in ("common", "providers", "experts")
] + [str(REPO_ROOT)]

# The child prints its verdict behind this marker. A leaking module frequently
# logs to stdout as it imports (the live platform certainly does), so the verdict
# has to be extracted rather than read off as "the whole of stdout".
MARK = "__LEAKGATE__:"


def probe_env() -> dict:
    """A copy of ``os.environ`` with the in-repo source roots on ``PYTHONPATH``."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    entries = list(PROBE_PATH) + ([existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def run_probe(code: str) -> str:
    """Run ``code`` in a fresh interpreter; return its stripped stdout.

    Fails loudly and *distinctly* -- never as a leak, never as a pass -- if the
    child could not run at all (e.g. the package under test is not importable).
    """
    out = subprocess.run(
        [sys.executable, "-P", "-c", code],
        capture_output=True,
        text=True,
        env=probe_env(),
    )
    assert out.returncode == 0, (
        "import-leak probe FAILED TO RUN -- this is a broken environment, not a "
        f"leak (exit={out.returncode}).\n"
        f"PYTHONPATH={probe_env()['PYTHONPATH']}\n"
        f"--- probe stdout ---\n{out.stdout}\n--- probe stderr ---\n{out.stderr}"
    )
    return out.stdout


def probe_verdict(code: str) -> str:
    """Run ``code`` (which must print ``MARK + verdict``) and return the verdict.

    Pulling the verdict out of a marked line keeps the gate readable when the
    module under test logs to stdout while importing.
    """
    stdout = run_probe(code)
    verdicts = [ln.split(MARK, 1)[1].strip()
                for ln in stdout.splitlines() if MARK in ln]
    assert verdicts, (
        "import-leak probe produced NO verdict -- this is a broken probe, not a "
        f"leak.\n--- probe stdout ---\n{stdout}"
    )
    return verdicts[-1]


def check_leak(modules, forbidden) -> str:
    """Import every name in ``modules`` in ONE fresh interpreter.

    Returns ``'CLEAN'`` or ``'LEAK:<comma-separated forbidden modules present>'``.
    """
    if isinstance(modules, str):
        modules = [modules]
    code = (
        "import importlib, sys\n"
        f"for _m in {list(modules)!r}:\n"
        "    importlib.import_module(_m)\n"
        f"_bad = [m for m in {list(forbidden)!r}\n"
        "        if any(k == m or k.startswith(m + '.') for k in sys.modules)]\n"
        f"print({MARK!r} + ('LEAK:' + ','.join(_bad) if _bad else 'CLEAN'))\n"
    )
    return probe_verdict(code)


def assert_no_leak(modules, forbidden) -> None:
    """Assert that importing ``modules`` pulls none of ``forbidden``."""
    result = check_leak(modules, forbidden)
    assert result == "CLEAN", (
        f"importing {modules!r} pulled forbidden module(s): {result}"
    )
