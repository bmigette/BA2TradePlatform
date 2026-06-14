"""Alias shim: this in-tree module IS ba2_common.core.interfaces.SocialMediaDataProviderInterface (Phase 6 migration).

The in-tree path is aliased to the package module object in sys.modules so
existing ``from ba2_trade_platform...`` imports resolve unchanged AND
``unittest.mock.patch`` / ``inspect.getsource`` targeting the in-tree path
operate on the real package module. Single source of truth: ba2_common.core.interfaces.SocialMediaDataProviderInterface."""
import importlib as _importlib
import sys as _sys

_pkg = _importlib.import_module("ba2_common.core.interfaces.SocialMediaDataProviderInterface")
_sys.modules[__name__] = _pkg
