"""Entry points must ACT on the master policy (a refusal propagates), and the GA-neutral allowlist
must stay honest against lazy imports too."""
from __future__ import annotations

import ast
import fnmatch
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import package_versions as pv
from app.services import self_update

ROOT = self_update.resolve_repo_root()

# ------------------------------------------------------------ the refusal is acted on


def _fake_db(worker):
    q = SimpleNamespace(filter=lambda *a, **k: SimpleNamespace(first=lambda: worker))
    return SimpleNamespace(query=lambda *a, **k: q)


def test_workers_api_update_maps_a_refusal_to_409_and_never_syncs(monkeypatch):
    from app.api import workers as api
    from app.services import worker_client

    def refuse():
        raise self_update.PackageGatingError("refusing distributed mode: x")

    monkeypatch.setattr(self_update, "master_sync_policy", refuse)
    called = []
    monkeypatch.setattr(worker_client, "ensure_synced", lambda *a, **k: called.append(1) or True)
    worker = SimpleNamespace(is_local=False, id=1, name="w", url="http://x", password="p")
    monkeypatch.setattr(api, "_worker_dict", lambda w: {"id": 1, "name": "w", "url": "http://x", "password": "p"})
    with pytest.raises(HTTPException) as e:
        api.update_worker_code(1, db=_fake_db(worker))
    assert e.value.status_code == 409 and "refusing distributed mode" in e.value.detail
    assert not called


def test_workers_api_update_passes_the_policy_minimums_through(monkeypatch):
    from app.api import workers as api
    from app.services import worker_client
    info = {"app_version": "2026.10.0010", "required_package_versions": {"ba2_common": "2026.10.00001"},
            "package_versions": {"ba2_common": "2026.10.00002"}}
    monkeypatch.setattr(self_update, "master_sync_policy", lambda: info)
    got = {}
    monkeypatch.setattr(worker_client, "ensure_synced", lambda w, v, **k: got.update(k, v=v) or True)
    monkeypatch.setattr(api, "_worker_dict", lambda w: {"id": 1, "name": "w", "url": "http://x", "password": "p"})
    api.update_worker_code(1, db=_fake_db(SimpleNamespace(is_local=False)))
    assert got["required_packages"] == info["required_package_versions"]
    assert got["master_packages"] == info["package_versions"]


_SWALLOWING = {"Exception", "BaseException", "RuntimeError", "PackageGatingError"}


def _enclosing_swallowers(tree: ast.AST, target: ast.Call):
    """try/except blocks around *target* whose handlers would swallow a PackageGatingError."""
    out = []

    def visit(node, stack):
        if node is target:
            out.extend(stack)
            return
        new = stack
        if isinstance(node, ast.Try) and any(target in ast.walk(b) for b in node.body):
            for h in node.handlers:
                names = set()
                if h.type is None:
                    names.add("BaseException")
                else:
                    for t in ast.walk(h.type):
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                        elif isinstance(t, ast.Attribute):
                            names.add(t.attr)
                # Loud = re-raises, or marks the optimization row failed with the message (`_fail`).
                reraises = any(isinstance(n, ast.Raise) or (
                    isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_fail")
                    for n in ast.walk(h))
                if names & _SWALLOWING and not reraises:
                    new = stack + [h]
        for child in ast.iter_child_nodes(node):
            visit(child, new)

    visit(tree, [])
    return out


@pytest.mark.parametrize("rel", [
    "testplatform/backend/app/services/strategy_optimization_handler.py",
    "testplatform/ba2test_launcher.py",
    "tools/rerun_dev_deployed_on_worker.py",
])
def test_master_policy_refusal_is_not_swallowed_at_the_entry_point(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and (getattr(n.func, "attr", None) or getattr(n.func, "id", None)) == "master_sync_policy"]
    assert calls, f"{rel} must call master_sync_policy()"
    for c in calls:
        assert not _enclosing_swallowers(tree, c), (
            f"{rel}: master_sync_policy() sits inside a handler that would swallow the refusal")


# ------------------------------------------------------------ allowlist honesty (static + runtime)


def _globs():
    return pv.read_ga_neutral_globs(ROOT)


def _matches(rel: str, globs) -> bool:
    return any(fnmatch.fnmatchcase(rel, g) for g in globs)


def _all_py_under_packages():
    for pkg, d in pv.PACKAGE_DIRS.items():
        base = ROOT / "packages" / d
        for f in (base / pkg).rglob("*.py"):
            yield pkg, base, f


def _neutral_modules() -> list[str]:
    globs = _globs()
    mods = []
    for _pkg, base, f in _all_py_under_packages():
        rel = f.relative_to(ROOT).as_posix()
        if "/tests/" in rel or f.name.startswith("test_"):
            continue
        if _matches(rel, globs):
            mods.append(".".join(f.relative_to(base).with_suffix("").parts))
    return mods


def _imports_anywhere(path: Path) -> set[str]:
    """Every module a file imports, INCLUDING function-level (lazy) imports."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            out.add(n.module)
            out.update(f"{n.module}.{a.name}" for a in n.names)
    return out


def test_allowlist_globs_match_at_least_something_or_say_so():
    globs = _globs()
    any_file = any(_matches(f.relative_to(ROOT).as_posix(), globs)
                   for d in (ROOT / "packages").iterdir() if d.is_dir() for f in d.rglob("*") if f.is_file())
    if not any_file:
        pytest.skip("no file in packages/ matches any GA_NEUTRAL_GLOB; nothing to keep honest")


def test_no_ga_relevant_shipped_or_backtest_code_imports_a_neutral_module_even_lazily():
    mods = set(_neutral_modules())
    globs = _globs()
    if not mods:
        print("note: no GA-neutral python modules exist yet; this scan becomes active when they land")
        return
    scan = [f for _p, _b, f in _all_py_under_packages()
            if not _matches(f.relative_to(ROOT).as_posix(), globs)
            and "/tests/" not in f.relative_to(ROOT).as_posix()]
    scan += list((ROOT / "testplatform" / "backend" / "app").rglob("*.py"))
    bad = []
    for f in scan:
        hit = _imports_anywhere(f) & mods
        if hit:
            bad.append((f.relative_to(ROOT).as_posix(), sorted(hit)))
    assert not bad, (f"GA-relevant code imports modules declared GA-neutral (lazy imports included): "
                     f"{bad}. Either the import is wrong or the allowlist entry is.")


def test_backtest_engine_runtime_never_loads_a_neutral_module():
    mods = _neutral_modules()
    if not mods:
        print("note: no GA-neutral python modules exist yet; runtime check becomes active when they land")
        return
    code = (
        "import sys\n"
        "import app.services.backtest.daily_engine, app.services.backtest.daily_backtest_handler\n"
        "import app.services.strategy_optimization_handler, app.services.backtest.rerun_handler\n"
        f"mods = {mods!r}\n"
        "bad = [m for m in mods if m in sys.modules]\n"
        "print('LEAK', bad) if bad else print('CLEAN')\n"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT / "testplatform" / "backend"),
                       env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "CLEAN" in r.stdout, r.stdout
