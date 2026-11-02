"""Review-driven pins for per-package versioning (docs/plans/2026-10-03-package-versioning-design.md).

* a worker NEWER than the master is excluded (no /update), including on the re-admission path;
* an unreadable master minimums file refuses distributed mode at every entry point;
* the three entry points and the evaluator really pass the new kwargs (delete them -> red);
* strict version parsing (month 13, trailing whitespace/newline);
* the GA-neutral allowlist stays honest: the backtest engine never imports a listed module.
"""
from __future__ import annotations

import ast
import fnmatch
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app.services.distributed_eval as de
from app.services import package_versions as pv
from app.services import self_update, worker_client
from app.services.distributed_eval import DistributedEvaluator

ROOT = self_update.resolve_repo_root()
MASTER = "2026.10.0010"
SCHEME = self_update.VERSION_SCHEME
REQ = {k: "2026.10.00005" for k in pv.PACKAGES}
_W = {"id": 1, "name": "remote150", "url": "http://x", "password": "p"}


class _Fake:
    def __init__(self, before, after=None):
        self.before, self.after, self.updates = before, after, 0

    def version(self, worker, timeout=10.0):
        return dict(self.after if (self.updates and self.after is not None) else self.before)

    def update(self, worker):
        self.updates += 1


def _info(pkgs=None, reqs=None, app=MASTER):
    return {"app_version": app, "version_scheme": SCHEME,
            "package_versions": dict(pkgs or REQ), "required_package_versions": dict(reqs or REQ)}


def _install(mp, fake):
    mp.setattr(worker_client, "version", fake.version)
    mp.setattr(worker_client, "_post_update", fake.update)


# ------------------------------------------------------------ worker ahead of the master


def test_worker_with_a_higher_minimum_is_excluded_without_update(monkeypatch):
    ahead = dict(REQ, ba2_common="2026.10.00009")
    fake = _Fake(_info(pkgs=ahead, reqs=ahead))
    _install(monkeypatch, fake)
    lines: list[str] = []
    ok = worker_client.ensure_synced(_W, MASTER, log=lines.append, max_wait=1.0, poll_interval=0.01,
                                     required_packages=REQ)
    assert ok is False
    assert fake.updates == 0, "cannot downgrade by updating; must not call /update"
    assert any(ln.startswith("WARNING") and "NEWER than the master" in ln and "ba2_common" in ln
               for ln in lines), lines


def test_worker_ahead_warning_is_deduplicated_per_job(monkeypatch):
    ahead = dict(REQ, ba2_experts="2026.10.00009")
    _install(monkeypatch, _Fake(_info(pkgs=ahead, reqs=ahead)))
    lines: list[str] = []
    seen: set = set()
    for _ in range(3):
        assert worker_client.ensure_synced(_W, MASTER, log=lines.append, required_packages=REQ,
                                           drift_seen=seen) is False
    assert sum("NEWER than the master" in ln for ln in lines) == 1


def test_worker_with_higher_package_version_but_same_minimum_is_allowed_drift(monkeypatch):
    newer_pkgs = {k: "2026.10.00050" for k in REQ}      # neutral changes ahead of the master
    _install(monkeypatch, _Fake(_info(pkgs=newer_pkgs, reqs=REQ)))
    assert worker_client.ensure_synced(_W, MASTER, log=lambda *_: None, required_packages=REQ) is True


def test_ahead_worker_is_readmitted_once_the_master_catches_up(monkeypatch):
    """Re-admission path (distributed_eval._recheck_down_workers -> _preflight_worker): a worker
    excluded as NEWER stays down while ahead and is re-admitted when the master's minimums catch up."""
    monkeypatch.setattr(de, "_DOWN_WORKER_RECHECK_S", 0.05)
    ahead = dict(REQ, ba2_common="2026.10.00009")
    fake = _Fake(_info(pkgs=ahead, reqs=ahead))
    _install(monkeypatch, fake)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 2})
    monkeypatch.setattr(de.worker_client, "run_trial",
                        lambda w, config, metric, **kw: {"ok": True, "fitness": 1.0, "trades": 1,
                                                         "error": None})
    required = dict(REQ)
    ev = DistributedEvaluator(None, "sharpe", n_consumers=0, optimization_id="t", workers=[dict(_W)],
                              master_version=MASTER, log=lambda *_: None, required_packages=required,
                              master_packages=dict(REQ))
    ev.start()
    try:
        assert ev._down_workers and not ev._active_workers
        assert fake.updates == 0
        required["ba2_common"] = "2026.10.00009"          # the master catches up
        jobs = [(i, {"idx": i}, f"k{i}", {"v": i}) for i in range(4)]
        assert len(list(ev.execute_jobs(jobs))) == 4
    finally:
        ev.stop()
    assert ev._active_workers and not ev._down_workers
    assert fake.updates == 0


# ------------------------------------------------------------ master cannot enforce minimums


def _tree(tmp_path: Path, required_body: str | None, pkg_version="2026.10.00003") -> Path:
    def w(rel, body):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    w("testplatform/version.py", 'TEST_APP_VERSION = "2026.10.0010"\n')
    w("ba2_trade_platform/version.py", 'APP_VERSION = "2026.10.1300"\n')
    for pkg, d in pv.PACKAGE_DIRS.items():
        w(f"packages/{d}/{pkg}/version.py", f'PACKAGE_VERSION = "{pkg_version}"\n')
    if required_body is not None:
        w("testplatform/required_package_versions.py", required_body)
    return tmp_path


_GOOD = 'REQUIRED_PACKAGE_VERSIONS = {"ba2_common": "2026.10.00001", "ba2_providers": "2026.10.00001", "ba2_experts": "2026.10.00001"}\n'


def test_master_policy_accepts_a_consistent_tree(tmp_path):
    info = self_update.master_sync_policy(_tree(tmp_path, _GOOD))
    assert set(info["required_package_versions"]) == set(pv.PACKAGES)


@pytest.mark.parametrize("body", [
    None,                                                      # missing file
    "<<<<<<< HEAD\nREQUIRED_PACKAGE_VERSIONS = {}\n=======\n>>>>>>> x\n",   # conflict markers
    "REQUIRED_PACKAGE_VERSIONS = {}\n",                        # empty
    'REQUIRED_PACKAGE_VERSIONS = {"ba2_common": "2026.10.00001"}\n',        # incomplete
    _GOOD.replace('"2026.10.00001", "ba2_providers"', '"oops", "ba2_providers"'),   # malformed
    _GOOD.replace("2026.10.00001", "2026.10.00099"),           # above the package's own version
])
def test_master_policy_refuses_unreadable_or_inconsistent_minimums(tmp_path, body):
    with pytest.raises(self_update.PackageGatingError) as e:
        self_update.master_sync_policy(_tree(tmp_path, body))
    assert "refusing distributed mode" in str(e.value)


def test_ensure_synced_refuses_an_empty_minimum_mapping_instead_of_gating_off(monkeypatch):
    _install(monkeypatch, _Fake(_info()))
    with pytest.raises(ValueError):
        worker_client.ensure_synced(_W, MASTER, log=lambda *_: None, required_packages={})


def _calls(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            fn = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if fn == name:
                out.append(n)
    return out


@pytest.mark.parametrize("rel,callee,kwargs", [
    ("testplatform/backend/app/services/strategy_optimization_handler.py", "DistributedEvaluator",
     {"required_packages", "master_packages"}),
    ("testplatform/backend/app/services/distributed_eval.py", "ensure_synced",
     {"required_packages", "master_packages", "drift_seen"}),
    ("testplatform/backend/app/api/workers.py", "ensure_synced", {"required_packages", "master_packages"}),
    ("testplatform/ba2test_launcher.py", "ensure_synced",
     {"required_packages", "master_packages", "drift_seen"}),
    ("tools/rerun_dev_deployed_on_worker.py", "ensure_synced", {"required_packages", "master_packages"}),
])
def test_every_sync_entry_point_passes_the_package_minimums(rel, callee, kwargs):
    calls = _calls(ROOT / rel, callee)
    assert calls, f"{rel} no longer calls {callee}"
    for c in calls:
        assert kwargs <= {k.arg for k in c.keywords}, f"{rel}: {callee}() lacks {kwargs}"


@pytest.mark.parametrize("rel", [
    "testplatform/backend/app/services/strategy_optimization_handler.py",
    "testplatform/backend/app/api/workers.py",
    "testplatform/ba2test_launcher.py",
    "tools/rerun_dev_deployed_on_worker.py",
])
def test_entry_points_use_the_validating_master_policy(rel):
    assert _calls(ROOT / rel, "master_sync_policy"), f"{rel} must call master_sync_policy()"


def test_evaluator_preflight_forwards_the_minimums(monkeypatch):
    got = {}
    monkeypatch.setattr(de.worker_client, "ensure_synced", lambda w, c, **k: got.update(k) or True)
    monkeypatch.setattr(de.worker_client, "push_cache", lambda w, **k: {"pushed": 0})
    monkeypatch.setattr(de.worker_client, "push_secrets", lambda w, s, **k: {"set": 0})
    monkeypatch.setattr(de.worker_client, "health", lambda w, **k: {"capacity": 1})
    ev = DistributedEvaluator(None, "sharpe", n_consumers=0, optimization_id="t", workers=[dict(_W)],
                              master_version=MASTER, log=lambda *_: None,
                              required_packages=dict(REQ), master_packages={"ba2_common": "x"})
    ev.start()
    ev.stop()
    assert got["required_packages"] == REQ and got["master_packages"] == {"ba2_common": "x"}
    assert isinstance(got["drift_seen"], set)


# ------------------------------------------------------------ leniencies that must not be silent


def test_worker_reporting_no_app_version_is_accepted_but_warned(monkeypatch):
    _install(monkeypatch, _Fake({"version_scheme": SCHEME}))
    lines: list[str] = []
    assert worker_client.ensure_synced(_W, MASTER, log=lines.append, required_packages=REQ) is True
    assert any(ln.startswith("WARNING") and "NO app_version" in ln for ln in lines), lines


def test_unhashable_package_values_from_a_worker_do_not_raise(monkeypatch):
    info = _info()
    info["package_versions"] = {"ba2_common": ["x"], "ba2_providers": {"a": 1}, "ba2_experts": REQ["ba2_experts"]}
    _install(monkeypatch, _Fake(info, after=_info()))
    # drift path (minimums satisfied by the other fields is impossible here, so exercise it directly)
    lines: list[str] = []
    worker_client.package_drift(_W, info, dict(REQ), lines.append, set())
    assert any("DRIFT" in ln for ln in lines)


# ------------------------------------------------------------ strict parsing


@pytest.mark.parametrize("bad", ["2026.13.00001", "2026.00.00001", " 2026.10.00001", "2026.10.00001 ",
                                 "2026.10.00001\n", "2026.10.0001\n"])
def test_parser_rejects_bad_month_and_stray_whitespace(bad):
    assert pv.try_parse(bad) is None
    assert not pv.PACKAGE_VERSION_RE.match(bad)


def test_parser_accepts_months_01_to_12():
    for m in range(1, 13):
        assert pv.try_parse(f"2026.{m:02d}.00001") == (2026, m, 1)


def test_ahead_detected_after_update_stops_waiting_immediately(monkeypatch):
    """/update pulled the worker onto a branch that is NEWER than the master: do not wait out
    max_wait, warn with commits and the git commands."""
    ahead = dict(REQ, ba2_common="2026.10.00009")
    fake = _Fake(_info(app="2026.10.0009"), after={**_info(pkgs=ahead, reqs=ahead), "git_commit": "wwww111"})
    _install(monkeypatch, fake)
    lines: list[str] = []
    t0 = time.time()
    ok = worker_client.ensure_synced(_W, MASTER, log=lines.append, max_wait=60.0, poll_interval=0.01,
                                     required_packages=REQ)
    assert ok is False and fake.updates == 1
    assert time.time() - t0 < 5, "must not wait for max_wait"
    joined = "\n".join(lines)
    assert "NEWER than the master" in joined and "did not converge" not in joined
    assert "wwww111" in joined and "git checkout" in joined and "RESTART" in joined
