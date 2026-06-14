"""Replace an in-tree extracted module with a re-export shim of its package twin.

Phase 6 (consume-by-shim, Model A): each in-tree
``ba2_trade_platform/{core,modules/dataproviders,modules/experts}`` module that
has a package twin (in ``ba2_common`` / ``ba2_providers`` / ``ba2_experts``)
becomes a thin re-export shim of that twin, so the live
``from ba2_trade_platform...`` call sites keep working unchanged.

This generator REFUSES to shim a module whose package twin does not import
(prevents silently replacing a real implementation with a broken one). It is the
plain ``from <pkg> import *`` generator used for the simple whole-extracted
modules; the SPLIT shims (``core/utils.py``, ``core/rules_export_import.py``) and
the MERGE shims (the dataproviders + experts registries and the AI-bearing
provider sub-packages) are hand-written, because they keep live-only names.

Usage::

    python tools/make_shims.py core/types.py                ba2_common.core.types
    python tools/make_shims.py modules/dataproviders/ohlcv/FMPOHLCVProvider.py \
                               ba2_providers.ohlcv.FMPOHLCVProvider

Writes the shim ONLY after confirming ``import <pkg_module>`` succeeds.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def make_shim(rel_arg: str, pkg_module: str) -> pathlib.Path:
    """Write an ALIAS shim at ``ba2_trade_platform/<rel_arg>`` pointing at
    ``pkg_module``. Refuses if ``pkg_module`` won't import.

    The shim makes the in-tree module path an ALIAS of the package module object
    in ``sys.modules`` (instead of a ``from pkg import *`` re-export). Aliasing is
    the faithful technique for leaf modules because:
      * the in-tree path and the package path become the SAME module object, so
        ``unittest.mock.patch("ba2_trade_platform...X.name")`` and
        ``inspect.getsource(...X)`` operate on the real package implementation
        (the live test suite patches/inspects many of these in-tree paths);
      * every public AND private name + ``__all__`` is preserved exactly;
      * there is genuinely one source of truth (no duplicated namespace).

    The package module is resolved via ``importlib.import_module`` rather than a
    plain ``import pkg.sub`` because a package ``__init__`` may bind a same-named
    *class* onto its namespace and shadow the submodule on a plain import;
    ``import_module`` returns the actual module object from ``sys.modules``.
    """
    rel_path = REPO_ROOT / "ba2_trade_platform" / rel_arg
    try:
        importlib.import_module(pkg_module)
    except Exception as e:  # noqa: BLE001 - we want to surface ANY import failure
        raise SystemExit(
            f"REFUSING to shim {rel_path}: package module {pkg_module} "
            f"failed to import: {type(e).__name__}: {e}"
        )
    if not rel_path.exists():
        raise SystemExit(f"REFUSING to shim: in-tree module {rel_path} does not exist")

    shim = (
        f'"""Alias shim: this in-tree module IS {pkg_module} (Phase 6 migration).\n'
        f"\n"
        f"The in-tree path is aliased to the package module object in sys.modules so\n"
        f"existing ``from ba2_trade_platform...`` imports resolve unchanged AND\n"
        f"``unittest.mock.patch`` / ``inspect.getsource`` targeting the in-tree path\n"
        f'operate on the real package module. Single source of truth: {pkg_module}."""\n'
        f"import importlib as _importlib\n"
        f"import sys as _sys\n"
        f"\n"
        f'_pkg = _importlib.import_module("{pkg_module}")\n'
        f"_sys.modules[__name__] = _pkg\n"
    )
    rel_path.write_text(shim, encoding="utf-8")
    return rel_path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: make_shims.py <rel_path_under_ba2_trade_platform> <pkg_module>")
    rel_arg = sys.argv[1]
    pkg_module = sys.argv[2]
    written = make_shim(rel_arg, pkg_module)
    print(f"shimmed {written} -> {pkg_module}")


if __name__ == "__main__":
    main()
