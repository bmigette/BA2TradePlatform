"""``unsyncable_reason`` — detects when a master's reported app_version can never be reached by
a remote worker's ``git pull`` (uncommitted version.py changes, or unpushed commits), so the
resulting ~5min-per-worker retry-and-exclude loop gets a clear cause instead of a silent
timeout. See the function's own docstring for the real incident this guards against.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.services.self_update import unsyncable_reason


def _run(returncode: int, stdout: str = ""):
    class _Result:
        pass
    r = _Result()
    r.returncode = returncode
    r.stdout = stdout
    return r


def test_unsyncable_reason_none_when_clean_and_pushed():
    with patch("app.services.self_update.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _run(0, ""),   # git status --porcelain -- version.py -> clean
            _run(0, "0"),  # git rev-list --count @{u}..HEAD -> 0 ahead
        ]
        assert unsyncable_reason(Path("C:/fake/repo")) is None


def test_unsyncable_reason_flags_uncommitted_version_file():
    with patch("app.services.self_update.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _run(0, " M ba2_trade_platform/version.py\n"),  # dirty
        ]
        reason = unsyncable_reason(Path("C:/fake/repo"))
        assert reason is not None
        assert "UNCOMMITTED" in reason


def test_unsyncable_reason_flags_unpushed_commits():
    with patch("app.services.self_update.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _run(0, ""),   # clean working tree
            _run(0, "3"),  # 3 commits ahead of upstream
        ]
        reason = unsyncable_reason(Path("C:/fake/repo"))
        assert reason is not None
        assert "ahead of its upstream" in reason


def test_unsyncable_reason_none_when_no_upstream_configured():
    with patch("app.services.self_update.subprocess.run") as mock_run:
        mock_run.side_effect = [
            _run(0, ""),        # clean working tree
            _run(128, ""),      # no upstream configured -> non-zero, not itself an error
        ]
        assert unsyncable_reason(Path("C:/fake/repo")) is None
