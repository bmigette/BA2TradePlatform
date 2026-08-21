"""The test platform's reported ``app_version`` must come from its OWN version file.

Why this exists. ``worker_client.ensure_synced`` decides whether a GA worker must self-update
by comparing ONE string — the master's ``app_version`` — and deliberately not the git commit,
so that ordinary pushes don't churn every connected worker mid-run. That string used to be read
out of ``ba2_trade_platform/version.py``, the TRADE app's file, which coupled the two apps in
both directions:

  * a test-platform-only change could not be shipped to the workers without bumping the trade
    app's version (hit for real on 2026-08-20: ``ba2_trade_platform/version.py`` had to go
    1070 -> 1071 purely to ship mid-generation GA checkpointing, or master and workers would
    both have reported 1070 and every worker would have kept running stale ``genetic.py``);
  * a trade-only bump made every worker re-sync for nothing, and an *uncommitted* trade bump
    tripped the retry-and-exclude path that burns 300 s per worker per selection.

``testplatform/version.py`` (``TEST_APP_VERSION``) decouples them. The bump rule is:
``ba2_trade_platform/`` only -> bump the trade file; ``testplatform/`` **or ``packages/``**
-> bump the test file (``packages/`` counts because the workers import ``ba2_common``).

The second half of this module pins the MIGRATION WINDOW: a worker that has not yet pulled is
running pre-split code and reports the *trade* app's version under the same ``app_version`` key.
If that string happens to equal the master's ``TEST_APP_VERSION`` the old comparison would call
it converged and silently run stale code on the whole grid. See
``test_ensure_synced_forces_update_for_pre_split_worker_even_when_the_strings_match``.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services import self_update, worker_client


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------------------
# _app_version reads the TEST platform's file
# --------------------------------------------------------------------------------------

def test_app_version_reads_the_testplatform_file_not_the_trade_file(tmp_path):
    _write(tmp_path, "ba2_trade_platform/version.py", 'APP_VERSION = "2026.08.9999"\n')
    _write(tmp_path, "testplatform/version.py", 'TEST_APP_VERSION = "2026.08.0042"\n')

    assert self_update._app_version(tmp_path) == "2026.08.0042"


def test_app_version_ignores_a_trade_only_bump(tmp_path):
    _write(tmp_path, "testplatform/version.py", 'TEST_APP_VERSION = "2026.08.0042"\n')
    _write(tmp_path, "ba2_trade_platform/version.py", 'APP_VERSION = "2026.08.1067"\n')
    before = self_update._app_version(tmp_path)

    _write(tmp_path, "ba2_trade_platform/version.py", 'APP_VERSION = "2026.08.1068"\n')
    after = self_update._app_version(tmp_path)

    assert before == after == "2026.08.0042"


def test_app_version_is_unknown_when_the_testplatform_file_is_missing(tmp_path):
    assert self_update._app_version(tmp_path) == "unknown"


def test_app_version_reads_by_text_not_by_import(tmp_path):
    """The file must be PARSED, never imported: the test venv does not install the trade app,
    and ``testplatform`` is not an importable package from a worker's venv either. A file whose
    body would explode on import must still yield its version string."""
    _write(tmp_path, "testplatform/version.py",
           'raise SystemExit("importing this file must never happen")\n'
           'TEST_APP_VERSION = "2026.08.0042"\n')

    assert self_update._app_version(tmp_path) == "2026.08.0042"


def test_shipped_testplatform_version_file_is_parseable():
    root = self_update.resolve_repo_root()
    body = (root / "testplatform" / "version.py").read_text(encoding="utf-8")
    m = re.search(r"""TEST_APP_VERSION\s*=\s*["']([^"']+)["']""", body)
    assert m, "testplatform/version.py must define TEST_APP_VERSION"
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", m.group(1)), m.group(1)


def test_the_two_shipped_version_strings_never_collide():
    """Second line of defence for the migration window.

    ``ensure_synced`` detects a pre-split worker by the missing ``version_scheme``, so a
    collision is already harmless — but a collision would still make the logs unreadable
    ("worker 2026.08.1068 != master 2026.08.1068"). Keeping the test platform's counter
    zero-padded keeps it disjoint from the trade app's, which has only ever used an unpadded
    3-4 digit counter.
    """
    root = self_update.resolve_repo_root()
    test_v = self_update._app_version(root)
    trade_v = self_update._trade_app_version(root)
    assert test_v != trade_v, (
        f"testplatform/version.py and ba2_trade_platform/version.py both say {test_v!r}; "
        f"keep the test platform's build number zero-padded so the two sequences stay disjoint"
    )


def test_get_version_info_reports_both_versions_and_the_scheme(tmp_path):
    """``app_version`` is the TEST platform's (the sync key); the trade app's is carried
    alongside for diagnosis only, and ``version_scheme`` marks this build as post-split."""
    _write(tmp_path, "ba2_trade_platform/version.py", 'APP_VERSION = "2026.08.9999"\n')
    _write(tmp_path, "testplatform/version.py", 'TEST_APP_VERSION = "2026.08.0042"\n')

    info = self_update.get_version_info(tmp_path)

    assert info["app_version"] == "2026.08.0042"
    assert info["trade_app_version"] == "2026.08.9999"
    assert info["version_scheme"] == self_update.VERSION_SCHEME


# --------------------------------------------------------------------------------------
# unsyncable_reason guards the TEST platform's file
# --------------------------------------------------------------------------------------

def test_unsyncable_reason_git_statuses_the_testplatform_version_file():
    """The guard must watch the file whose string workers actually converge on. Watching the
    trade file would both miss a dirty test bump and false-alarm on a dirty trade bump."""
    from unittest.mock import patch

    class _R:
        returncode = 0
        stdout = ""

    with patch("app.services.self_update.subprocess.run", return_value=_R()) as mock_run:
        self_update.unsyncable_reason(Path("/fake/repo"))

    status_cmd = mock_run.call_args_list[0].args[0]
    assert status_cmd[:4] == ["git", "status", "--porcelain", "--"]
    assert status_cmd[4] == "testplatform/version.py"


# --------------------------------------------------------------------------------------
# Migration window: a worker still running PRE-SPLIT code
# --------------------------------------------------------------------------------------

class _FakeWorker:
    """Stands in for a remote worker's HTTP surface: ``/version`` payloads + an ``/update``
    that flips it to the next payload (i.e. the pull+restart landed)."""

    def __init__(self, before: dict, after: dict | None = None):
        self._before = before
        self._after = after
        self.updates = 0

    def version(self, worker, timeout=10.0):
        if self.updates and self._after is not None:
            return dict(self._after)
        return dict(self._before)

    def update(self, worker):
        self.updates += 1


def _install(monkeypatch, fake: _FakeWorker):
    monkeypatch.setattr(worker_client, "version", fake.version)
    monkeypatch.setattr(worker_client, "_post_update", fake.update)


_WORKER = {"id": 1, "name": "remote150", "url": "http://x", "password": "p"}


def test_ensure_synced_accepts_a_post_split_worker_on_the_same_version(monkeypatch):
    """The ordinary steady state: same version, same scheme -> usable, and NOT restarted."""
    fake = _FakeWorker({"app_version": "2026.08.0042",
                        "version_scheme": self_update.VERSION_SCHEME})
    _install(monkeypatch, fake)

    assert worker_client.ensure_synced(_WORKER, "2026.08.0042", log=lambda *_: None) is True
    assert fake.updates == 0


def test_ensure_synced_forces_update_for_pre_split_worker_even_when_the_strings_match(monkeypatch):
    """THE migration-window trap.

    A worker that has not pulled yet runs pre-split code: it has no ``testplatform/version.py``
    and reports ``ba2_trade_platform``'s ``APP_VERSION`` under ``app_version``. Both files use
    the same ``YYYY.MM.NNNNN`` shape and the same year/month, so the two strings CAN collide —
    and a collision under the old string-only comparison means ``ensure_synced`` returns True
    while the worker runs stale ``genetic.py``/``ba2_common``, which is exactly the silent
    divergence self-update exists to prevent.

    The absence of ``version_scheme`` in the payload identifies pre-split code positively, so
    the update is forced regardless of what the strings say. One pull brings both the new
    ``self_update.py`` and ``testplatform/version.py`` (same commit), so it self-heals.
    """
    colliding = "2026.08.1067"
    fake = _FakeWorker(
        before={"app_version": colliding},                      # pre-split: no version_scheme
        after={"app_version": colliding,
               "version_scheme": self_update.VERSION_SCHEME},   # pulled + restarted
    )
    _install(monkeypatch, fake)

    assert worker_client.ensure_synced(_WORKER, colliding, log=lambda *_: None,
                                       max_wait=10.0, poll_interval=0.01) is True
    assert fake.updates == 1, "the pre-split worker must be told to update, not waved through"


def test_ensure_synced_excludes_a_pre_split_worker_that_never_converges(monkeypatch):
    """If the pull can't reach the post-split commit the worker never gains the scheme, so it
    is EXCLUDED (and says why) rather than silently running stale code."""
    colliding = "2026.08.1067"
    fake = _FakeWorker(before={"app_version": colliding})  # /update never changes anything
    _install(monkeypatch, fake)
    lines: list[str] = []

    ok = worker_client.ensure_synced(_WORKER, colliding, log=lines.append,
                                     max_wait=0.05, poll_interval=0.01)

    assert ok is False
    assert any("pre-split" in ln.lower() for ln in lines), lines


def test_ensure_synced_still_updates_a_post_split_worker_on_a_stale_version(monkeypatch):
    """Unchanged behaviour: same scheme, different version -> update and converge."""
    fake = _FakeWorker(
        before={"app_version": "2026.08.0041", "version_scheme": self_update.VERSION_SCHEME},
        after={"app_version": "2026.08.0042", "version_scheme": self_update.VERSION_SCHEME},
    )
    _install(monkeypatch, fake)

    assert worker_client.ensure_synced(_WORKER, "2026.08.0042", log=lambda *_: None,
                                       max_wait=10.0, poll_interval=0.01) is True
    assert fake.updates == 1


def test_ensure_synced_does_not_gate_when_the_master_has_no_version(monkeypatch):
    """Unchanged escape hatch: no master version means the caller opted out of version gating,
    so even a pre-split worker is left alone rather than being restarted pointlessly."""
    fake = _FakeWorker({"app_version": "2026.08.1067"})  # pre-split
    _install(monkeypatch, fake)

    assert worker_client.ensure_synced(_WORKER, None, log=lambda *_: None) is True
    assert fake.updates == 0


def test_ensure_synced_excludes_an_unreachable_worker(monkeypatch):
    """Unchanged behaviour: /version raising means exclude, never crash the dispatch."""
    def boom(worker, timeout=10.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(worker_client, "version", boom)

    assert worker_client.ensure_synced(_WORKER, "2026.08.0042", log=lambda *_: None) is False
