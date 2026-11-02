"""Per-package versions + the worker sync rule (docs/plans/2026-10-03-package-versioning-design.md).

Pins: the shipped version files, numeric comparison semantics, the ``ensure_synced`` decision for
every combination of (TEST version equal/different) x (package at/above/below minimum, or the
worker reporting none) x (worker unreachable), drift observability, and the ``/version`` payload
in both skew directions (new master + old worker, old master + new worker).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import package_versions as pv
from app.services import self_update, worker_client

ROOT = self_update.resolve_repo_root()
MASTER = "2026.10.0010"
SCHEME = self_update.VERSION_SCHEME
REQ = {"ba2_common": "2026.10.00005", "ba2_providers": "2026.10.00005", "ba2_experts": "2026.10.00005"}
_WORKER = {"id": 1, "name": "remote150", "url": "http://x", "password": "p"}


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------- shipped files


@pytest.mark.parametrize("pkg", pv.PACKAGES)
def test_shipped_package_version_is_well_formed_and_matches_pyproject_and_dunder_version(pkg):
    d = pv.PACKAGE_DIRS[pkg]
    v = pv.read_package_versions(ROOT)[pkg]
    assert pv.PACKAGE_VERSION_RE.match(v), f"{pkg} PACKAGE_VERSION {v!r} must be YYYY.MM.NNNNN"
    pyproject = (ROOT / "packages" / d / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    assert m and m.group(1) == v, "pyproject version and PACKAGE_VERSION must be the same string"
    mod = __import__(pkg)
    assert Path(mod.__file__).resolve().is_relative_to(ROOT / "packages"), (
        f"{pkg} imported from {mod.__file__}, not this checkout")
    assert mod.__version__ == v


def test_shipped_required_versions_cover_every_package_and_never_exceed_it():
    required = pv.read_required_package_versions(ROOT)
    have = pv.read_package_versions(ROOT)
    assert set(required) == set(pv.PACKAGES)
    for pkg in pv.PACKAGES:
        assert pv.PACKAGE_VERSION_RE.match(required[pkg]), required[pkg]
        assert pv.parse_version(required[pkg]) <= pv.parse_version(have[pkg])


def test_shipped_ga_neutral_globs_parse():
    globs = pv.read_ga_neutral_globs(ROOT)
    assert "packages/common/ba2_common/core/ibkr_*.py" in globs


def test_required_and_version_files_are_read_by_text_not_import(tmp_path):
    _write(tmp_path, "packages/common/ba2_common/version.py",
           'raise SystemExit("never import")\nPACKAGE_VERSION = "2026.10.00007"\n')
    _write(tmp_path, "testplatform/required_package_versions.py",
           'raise SystemExit("never import")\nREQUIRED_PACKAGE_VERSIONS = {"ba2_common": "2026.10.00003"}\n')
    assert pv.read_package_versions(tmp_path)["ba2_common"] == "2026.10.00007"
    assert pv.read_package_versions(tmp_path)["ba2_experts"] == pv.UNKNOWN
    assert pv.read_required_package_versions(tmp_path) == {"ba2_common": "2026.10.00003"}


# ---------------------------------------------------------------- comparison


def test_comparison_is_numeric_not_lexicographic():
    assert pv.parse_version("2026.10.00099") < pv.parse_version("2026.10.00100")
    # The classic trap: as raw strings "99" > "100", numerically it is the other way round.
    assert "2026.10.99" > "2026.10.100"
    assert pv.parse_version("2026.10.99") < pv.parse_version("2026.10.100")
    assert pv.parse_version("2026.09.99999") < pv.parse_version("2026.10.00001")
    assert pv.parse_version("2026.12.00001") < pv.parse_version("2027.01.00001")
    assert pv.parse_version("2026.09.0126") == (2026, 9, 126)  # 4-digit TEST_APP_VERSION style


@pytest.mark.parametrize("bad", ["", "unknown", "1.2", "2026.10", "2026.10.x", None, 5, "v2026.10.00001"])
def test_malformed_versions_are_rejected(bad):
    with pytest.raises(ValueError):
        pv.parse_version(bad)
    assert pv.try_parse(bad) is None


def test_package_version_regex_requires_five_digit_build():
    assert pv.PACKAGE_VERSION_RE.match("2026.10.00001")
    assert not pv.PACKAGE_VERSION_RE.match("2026.10.0001")
    assert not pv.PACKAGE_VERSION_RE.match("2026.10.000001")


def test_below_minimum_and_unknown_classification():
    req = {"a": "2026.10.00100", "b": "2026.10.00100", "c": "2026.10.00100", "d": "2026.10.00100"}
    below, unknown = pv.below_minimum(
        {"a": "2026.10.00099", "b": "2026.10.00100", "c": "2026.10.00101", "d": "garbage"}, req)
    assert below == ["a"]          # 99 < 100 numerically
    assert unknown == ["d"]        # unparseable -> unknown, not fine
    assert pv.below_minimum(None, req) == ([], ["a", "b", "c", "d"])   # old worker: all unknown
    assert pv.below_minimum({}, req)[1] == ["a", "b", "c", "d"]


def test_malformed_minimum_is_a_loud_master_side_error():
    with pytest.raises(ValueError):
        pv.below_minimum({"a": "2026.10.00001"}, {"a": "oops"})


def test_drift_lists_only_differing_packages():
    d = pv.drift({"ba2_common": "2026.10.00001", "ba2_experts": "2026.10.00009"},
                 {"ba2_common": "2026.10.00002", "ba2_providers": "2026.10.00001",
                  "ba2_experts": "2026.10.00009"})
    assert d == {"ba2_common": ("2026.10.00002", "2026.10.00001"),
                 "ba2_providers": ("2026.10.00001", pv.UNKNOWN)}


# ---------------------------------------------------------------- ensure_synced


class _FakeWorker:
    def __init__(self, before: dict, after: dict | None = None):
        self._before, self._after, self.updates = before, after, 0

    def version(self, worker, timeout=10.0):
        return dict(self._after if (self.updates and self._after is not None) else self._before)

    def update(self, worker):
        self.updates += 1


def _install(monkeypatch, fake):
    monkeypatch.setattr(worker_client, "version", fake.version)
    monkeypatch.setattr(worker_client, "_post_update", fake.update)


def _info(app=MASTER, pkgs="ok", **extra):
    d = {"app_version": app, "version_scheme": SCHEME}
    if pkgs == "ok":
        d["package_versions"] = dict(REQ)
    elif pkgs is not None:
        d["package_versions"] = pkgs
    d.update(extra)
    return d


def _sync(monkeypatch, fake, lines=None, master=MASTER, **kw):
    _install(monkeypatch, fake)
    sink = (lines if lines is not None else []).append
    return worker_client.ensure_synced(_WORKER, master, log=sink, max_wait=5.0, poll_interval=0.01,
                                       required_packages=REQ, **kw)


def test_equal_test_version_and_packages_at_minimum_is_not_synced(monkeypatch):
    fake = _FakeWorker(_info())
    assert _sync(monkeypatch, fake) is True and fake.updates == 0


def test_equal_test_version_and_packages_above_minimum_is_not_synced(monkeypatch):
    above = {k: "2026.10.00050" for k in REQ}
    fake = _FakeWorker(_info(pkgs=above))
    assert _sync(monkeypatch, fake) is True and fake.updates == 0


def test_package_version_difference_alone_does_not_resync_a_worker_within_the_minimum(monkeypatch):
    """The point of the change: master packages are ahead but the minimums are met -> no churn."""
    master_pkgs = {k: "2026.10.00099" for k in REQ}
    fake = _FakeWorker(_info())  # worker at exactly the minimum
    assert _sync(monkeypatch, fake, master_packages=master_pkgs) is True
    assert fake.updates == 0


def test_package_below_minimum_forces_a_sync_and_converges(monkeypatch):
    low = dict(REQ, ba2_experts="2026.10.00004")
    fake = _FakeWorker(_info(pkgs=low), after=_info())
    lines: list[str] = []
    assert _sync(monkeypatch, fake, lines) is True
    assert fake.updates == 1
    assert any("below the required minimum" in ln and "ba2_experts" in ln for ln in lines), lines


def test_numeric_not_string_comparison_decides_below_minimum(monkeypatch):
    req_hi = {k: "2026.10.00100" for k in REQ}
    fake = _FakeWorker(_info(pkgs={k: "2026.10.00099" for k in REQ}),
                       after=_info(pkgs=req_hi))
    _install(monkeypatch, fake)
    ok = worker_client.ensure_synced(_WORKER, MASTER, log=lambda *_: None, max_wait=5.0,
                                     poll_interval=0.01, required_packages=req_hi)
    assert ok is True and fake.updates == 1


def test_package_below_minimum_that_never_converges_is_excluded_with_a_reason(monkeypatch):
    low = dict(REQ, ba2_common="2026.10.00001")
    fake = _FakeWorker(_info(pkgs=low))  # /update changes nothing
    lines: list[str] = []
    _install(monkeypatch, fake)
    ok = worker_client.ensure_synced(_WORKER, MASTER, log=lines.append, max_wait=0.05,
                                     poll_interval=0.01, required_packages=REQ)
    assert ok is False
    assert any("still failing package minimums" in ln for ln in lines), lines


def test_old_worker_without_package_versions_is_synced_once_and_warned_loudly(monkeypatch):
    fake = _FakeWorker(_info(pkgs=None), after=_info())  # no package_versions key at all
    lines: list[str] = []
    assert _sync(monkeypatch, fake, lines) is True
    assert fake.updates == 1
    assert any(ln.startswith("WARNING") and "NO version for package" in ln for ln in lines), lines


def test_old_worker_that_stays_old_is_excluded_not_crashing(monkeypatch):
    fake = _FakeWorker(_info(pkgs=None))
    _install(monkeypatch, fake)
    ok = worker_client.ensure_synced(_WORKER, MASTER, log=lambda *_: None, max_wait=0.05,
                                     poll_interval=0.01, required_packages=REQ)
    assert ok is False


def test_package_versions_of_wrong_shape_do_not_crash(monkeypatch):
    for bogus in ("2026.10.00001", ["x"], 7):
        fake = _FakeWorker(_info(pkgs=bogus), after=_info())
        assert _sync(monkeypatch, fake) is True and fake.updates == 1


def test_different_test_version_syncs_even_when_packages_are_fine(monkeypatch):
    fake = _FakeWorker(_info(app="2026.10.0009"), after=_info())
    assert _sync(monkeypatch, fake) is True and fake.updates == 1


def test_different_test_version_and_package_below_minimum_is_one_update(monkeypatch):
    fake = _FakeWorker(_info(app="2026.10.0009", pkgs=dict(REQ, ba2_common="2026.10.00001")),
                       after=_info())
    assert _sync(monkeypatch, fake) is True and fake.updates == 1


def test_unreachable_worker_is_excluded(monkeypatch):
    def boom(worker, timeout=10.0):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(worker_client, "version", boom)
    assert worker_client.ensure_synced(_WORKER, MASTER, log=lambda *_: None,
                                       required_packages=REQ) is False


def test_required_packages_none_keeps_the_previous_behaviour(monkeypatch):
    """Callers that do not pass minimums (and every pre-existing test) get exactly the old rule,
    even for a worker that reports no package versions."""
    fake = _FakeWorker(_info(pkgs=None))
    _install(monkeypatch, fake)
    assert worker_client.ensure_synced(_WORKER, MASTER, log=lambda *_: None) is True
    assert fake.updates == 0


def test_no_master_version_never_gates_on_packages(monkeypatch):
    fake = _FakeWorker(_info(pkgs=None))
    _install(monkeypatch, fake)
    assert worker_client.ensure_synced(_WORKER, None, log=lambda *_: None,
                                       required_packages=REQ) is True
    assert fake.updates == 0


# ---------------------------------------------------------------- drift observability


def test_drift_is_warned_once_per_job_not_silent(monkeypatch):
    master_pkgs = {k: "2026.10.00099" for k in REQ}
    fake = _FakeWorker(_info())
    seen: set = set()
    lines: list[str] = []
    for _ in range(3):  # three pre-flights of the same job
        assert _sync(monkeypatch, fake, lines, master_packages=master_pkgs, drift_seen=seen) is True
    drift_lines = [ln for ln in lines if "DRIFT" in ln]
    assert len(drift_lines) == 1, lines
    assert drift_lines[0].startswith("WARNING")
    for pkg in REQ:
        assert pkg in drift_lines[0]
    # a NEW job (fresh set) warns again
    lines2: list[str] = []
    _sync(monkeypatch, fake, lines2, master_packages=master_pkgs, drift_seen=set())
    assert any("DRIFT" in ln for ln in lines2)


def test_no_drift_line_when_versions_match(monkeypatch):
    fake = _FakeWorker(_info())
    lines: list[str] = []
    _sync(monkeypatch, fake, lines, master_packages=dict(REQ), drift_seen=set())
    assert not any("DRIFT" in ln for ln in lines)


# ---------------------------------------------------------------- /version payload skew


def test_get_version_info_carries_package_versions_and_minimums(tmp_path):
    _write(tmp_path, "testplatform/version.py", 'TEST_APP_VERSION = "2026.10.0010"\n')
    _write(tmp_path, "ba2_trade_platform/version.py", 'APP_VERSION = "2026.10.1300"\n')
    for pkg, d in pv.PACKAGE_DIRS.items():
        _write(tmp_path, f"packages/{d}/{pkg}/version.py", 'PACKAGE_VERSION = "2026.10.00003"\n')
    _write(tmp_path, "testplatform/required_package_versions.py",
           'REQUIRED_PACKAGE_VERSIONS = {"ba2_common": "2026.10.00002"}\n')
    info = self_update.get_version_info(tmp_path)
    assert info["package_versions"] == {k: "2026.10.00003" for k in pv.PACKAGES}
    assert info["required_package_versions"] == {"ba2_common": "2026.10.00002"}
    # additive: every pre-existing key is still there
    for k in ("app_version", "trade_app_version", "version_scheme", "git_commit", "editable", "root"):
        assert k in info


def test_new_master_old_worker_syncs_once_old_master_new_worker_is_untouched(monkeypatch):
    # new master (passes minimums) + old worker (payload without the new keys)
    old = {"app_version": MASTER, "version_scheme": SCHEME, "git_commit": "abc"}
    fake = _FakeWorker(old, after=_info())
    assert _sync(monkeypatch, fake) is True and fake.updates == 1
    # old master (no minimums, so it never passes required_packages) + new worker (extra keys)
    fake2 = _FakeWorker(_info(required_package_versions=dict(REQ)))
    _install(monkeypatch, fake2)
    assert worker_client.ensure_synced(_WORKER, MASTER, log=lambda *_: None) is True
    assert fake2.updates == 0


def test_real_checkout_payload_satisfies_its_own_minimums():
    """A master must never exclude a worker that is an exact copy of itself."""
    info = self_update.get_version_info(ROOT)
    assert worker_client.sync_reasons(info, info["app_version"], info["required_package_versions"]) == []
