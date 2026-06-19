"""Rewrite imports for the package extraction.

Pass A: absolutize relative imports (using each file's full module name).
Pass B: remap ba2_trade_platform.* roots to the new package roots.

Usage:
    python tools/codemod_imports.py <root_dir> <old_pkg_for_relative_base>
Example (run from inside the repo, files already copied in):
    python tools/codemod_imports.py ba2_common      ba2_trade_platform
    python tools/codemod_imports.py ba2_providers    ba2_trade_platform.modules.dataproviders
    python tools/codemod_imports.py ba2_experts      ba2_trade_platform.modules.experts
The second arg is the ORIGINAL fully-qualified package that the copied files
came from, used to reconstruct each file's original module name so relative
imports resolve to the correct absolute target before remapping.
"""
import sys, pathlib
import libcst as cst

# Pass B mapping, longest prefix first.
REMAP = [
    ("ba2_trade_platform.core", "ba2_common.core"),
    ("ba2_trade_platform.config", "ba2_common.config"),
    ("ba2_trade_platform.logger", "ba2_common.logger"),
    ("ba2_trade_platform.modules.dataproviders", "ba2_providers"),
    ("ba2_trade_platform.modules.experts", "ba2_experts"),
]

def remap(mod: str) -> str:
    for old, new in REMAP:
        if mod == old or mod.startswith(old + "."):
            return new + mod[len(old):]
    return mod

class Rewriter(cst.CSTTransformer):
    def __init__(self, current_module: str):
        # current_module = original absolute module name of this file
        self.pkg = current_module.rsplit(".", 1)[0] if "." in current_module else current_module

    def _abs_from_relative(self, dots: int, tail: str) -> str:
        base = self.pkg.split(".")
        # `from . import x` -> dots=1 keeps current package; each extra dot pops one.
        pops = dots - 1
        if pops > len(base):
            return tail  # cannot resolve; leave as-is (will fail import gate -> visible)
        prefix = base[: len(base) - pops] if pops else base
        parts = prefix + ([tail] if tail else [])
        return ".".join(p for p in parts if p)

    def leave_ImportFrom(self, node, updated):
        dots = len(updated.relative)
        if dots == 0:
            # absolute import: just remap the root
            if updated.module is None:
                return updated
            absmod = cst_module_to_str(updated.module)
            new = remap(absmod)
            return updated.with_changes(module=str_to_cst_attr(new)) if new != absmod else updated
        # relative -> absolutize -> remap
        tail = cst_module_to_str(updated.module) if updated.module else ""
        absmod = self._abs_from_relative(dots, tail)
        absmod = remap(absmod)
        return updated.with_changes(relative=[], module=str_to_cst_attr(absmod))

def cst_module_to_str(mod) -> str:
    if isinstance(mod, cst.Name):
        return mod.value
    if isinstance(mod, cst.Attribute):
        return cst_module_to_str(mod.value) + "." + mod.attr.value
    raise TypeError(type(mod))

def str_to_cst_attr(dotted: str):
    parts = dotted.split(".")
    node = cst.Name(parts[0])
    for p in parts[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(p))
    return node

def main():
    root = pathlib.Path(sys.argv[1])           # e.g. ba2_common
    orig_base = sys.argv[2]                     # e.g. ba2_trade_platform  (or ...modules.dataproviders)
    pkg_root_name = root.name                   # ba2_common / ba2_providers / ba2_experts
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).with_suffix("")
        rel_mod = ".".join(rel.parts)
        rel_mod = rel_mod[: -len(".__init__")] if rel_mod.endswith(".__init__") else rel_mod
        # original module name = orig_base + (path relative to the NEW root, which mirrors the old subtree)
        original = orig_base + ("." + rel_mod if rel_mod and rel_mod != "__init__" else "")
        src = path.read_text(encoding="utf-8")
        tree = cst.parse_module(src)
        new = tree.visit(Rewriter(original))
        if new.code != src:
            path.write_text(new.code, encoding="utf-8")
            print(f"rewrote {path}")

if __name__ == "__main__":
    main()
