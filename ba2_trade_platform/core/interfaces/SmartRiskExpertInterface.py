"""Alias shim: this in-tree module IS ba2_common.core.interfaces.SmartRiskExpertInterface (Phase 6 migration).

The in-tree path is aliased to the package module object in sys.modules so
existing ``from ba2_trade_platform...`` imports resolve unchanged AND
``unittest.mock.patch`` / ``inspect.getsource`` targeting the in-tree path
operate on the real package module. Single source of truth: ba2_common.core.interfaces.SmartRiskExpertInterface."""
import importlib as _importlib
import sys as _sys

_pkg = _importlib.import_module("ba2_common.core.interfaces.SmartRiskExpertInterface")
# RACE GUARD: mirror the package's names onto THIS module BEFORE swapping it out of
# sys.modules. The swap alone leaves the original module object permanently empty, so a
# second thread reaching a LAZY ``from .X import Y`` while the first is still executing
# this body gets that empty object and raises "cannot import name 'Y'". That silently
# killed a live Monday enter-market run on 2026-08-17; see
# docs/2026-08-17-alias-shim-race.md. Locals are captured first because the update copies
# the package namespace wholesale -- a package binding _sys/_pkg must not break the swap.
_modules, _me, _target = _sys.modules, __name__, _pkg
globals().update({k: v for k, v in vars(_pkg).items() if not k.startswith('__')})
_modules[_me] = _target
