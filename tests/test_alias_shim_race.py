"""Phase 6 alias shims must survive a concurrent first import.

A shim that only does ``sys.modules[__name__] = _pkg`` leaves the ORIGINAL module object
permanently empty, so a second thread reaching a lazy ``from .X import Y`` while the first is
still executing the body gets that empty object and raises "cannot import name 'Y'". On
2026-08-17 that killed the live Monday enter-market run for expert 6 -- the batch never started,
so nothing in the trading UI showed a failure, and entry is Mondays-only. Full write-up:
docs/2026-08-17-alias-shim-race.md
"""
from __future__ import annotations

import importlib
import pathlib
import re
import sys
import threading

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SHIM_ROOT = REPO / "ba2_trade_platform"

# The lazily-imported shim that actually failed in prod (JobManager._execute_screener_analysis).
LAZY_SHIM = "ba2_trade_platform.core.StockScreener"
LAZY_NAME = "StockScreener"


def _shim_files():
    """Every in-tree alias shim, found by its swap line."""
    out = []
    for p in SHIM_ROOT.rglob("*.py"):
        try:
            t = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "_importlib.import_module(" in t and "_modules[_me] = _target" in t:
            out.append(p)
    return out


def test_shims_are_present_at_all():
    """Guard the guard: if the discovery pattern stops matching, the tests below go vacuous."""
    assert len(_shim_files()) > 50


def test_every_shim_populates_itself_before_the_swap():
    """The race guard must be ordered: names copied BEFORE sys.modules is swapped."""
    offenders = []
    for p in _shim_files():
        t = p.read_text(encoding="utf-8")
        copy_at = t.find("globals().update(")
        swap_at = t.find("_modules[_me] = _target")
        if copy_at == -1 or swap_at == -1 or copy_at > swap_at:
            offenders.append(str(p.relative_to(REPO)))
    assert not offenders, f"shims missing/misordering the race guard: {offenders}"


def test_no_shim_uses_the_bare_unguarded_swap():
    """The original one-liner must not come back (e.g. via a regenerated shim)."""
    bare = re.compile(r"^_sys\.modules\[__name__\] = _pkg\s*$", re.M)
    offenders = [str(p.relative_to(REPO)) for p in SHIM_ROOT.rglob("*.py")
                 if bare.search(p.read_text(encoding="utf-8", errors="ignore"))]
    assert not offenders, f"unguarded alias swap found in: {offenders}"


def test_generator_emits_the_guard():
    """tools/make_shims.py must not regenerate the unguarded form."""
    t = (REPO / "tools" / "make_shims.py").read_text(encoding="utf-8")
    assert "_modules[_me] = _target" in t
    assert "globals().update(" in t


def test_shim_module_resolves_the_package_symbol():
    mod = importlib.import_module(LAZY_SHIM)
    assert getattr(mod, LAZY_NAME, None) is not None


def test_prod_failure_mode_reproduces_without_the_guard():
    """Pin the exact prod symptom to the unpopulated module object.

    Standing in for the pre-swap window with an empty module reproduces the ImportError and its
    give-away message (it cites the IN-TREE path, not the package file). If this ever stops
    raising, the race described in the docs is no longer possible and these tests can go.
    """
    import types

    saved = sys.modules.get(LAZY_SHIM)
    sys.modules[LAZY_SHIM] = types.ModuleType(LAZY_SHIM)  # binds nothing, as the old shim did
    try:
        # `from X import Y` (IMPORT_FROM), NOT import_module + getattr: only this path raises
        # ImportError, and it is the form JobManager._execute_screener_analysis uses.
        with pytest.raises(ImportError) as ei:
            exec(f"from {LAZY_SHIM} import {LAZY_NAME}", {})
        assert f"cannot import name '{LAZY_NAME}'" in str(ei.value)
    finally:
        if saved is not None:
            sys.modules[LAZY_SHIM] = saved
        else:
            sys.modules.pop(LAZY_SHIM, None)


def test_concurrent_first_import_of_the_lazy_shim_never_fails():
    """THE REGRESSION. Two experts scheduled at the same minute raced this 4 ms apart in prod."""
    for k in [k for k in list(sys.modules) if k.startswith(LAZY_SHIM)]:
        del sys.modules[k]

    errors: list = []
    barrier = threading.Barrier(16)

    def go():
        barrier.wait()                      # maximise overlap on the first import
        try:
            mod = importlib.import_module(LAZY_SHIM)
            assert getattr(mod, LAZY_NAME) is not None
        except Exception as e:  # noqa: BLE001 -- collecting, not handling
            errors.append(repr(e))

    threads = [threading.Thread(target=go) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"concurrent first import failed: {errors[:3]}"
