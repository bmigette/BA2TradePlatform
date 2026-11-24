"""The two export capture tools write to the DB they read (WAL pragmas, SQLite journals), so
they must refuse the REAL default DBs: tools/live_export_capture.py the live trade DB
(<BA2_HOME>/trade/db.sqlite), tools/export_golden_capture.py the test app's DB
(<BA2_HOME>/test/dl_forecasting.db), where BA2_HOME defaults to ~/Documents/ba2.

Each tool runs in a subprocess with HOME (and BA2_HOME, when set) pointed at tmp_path, so the
"real" paths below are throwaway files and no run can ever reach the user's actual DBs.
"""
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_TOOL = os.path.join(REPO, "tools", "live_export_capture.py")
BACKTEST_TOOL = os.path.join(REPO, "tools", "export_golden_capture.py")


def _run(tool, tmp_path, *, home, ba2_home=None, db_file=None, database_url=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("BA2_HOME", "DB_FILE", "DATABASE_URL", "HOMEDRIVE", "HOMEPATH")}
    # ``~`` is HOME on POSIX but USERPROFILE on Windows (Python ignores HOME there since 3.8): with HOME
    # alone the tool's "~/Documents/ba2" was the REAL user profile on Windows, so it did not refuse the
    # throwaway file and ran against it instead.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if ba2_home is not None:
        env["BA2_HOME"] = str(ba2_home)
    if db_file is not None:
        env["DB_FILE"] = str(db_file)
    if database_url is not None:
        env["DATABASE_URL"] = database_url
    return subprocess.run([sys.executable, tool, str(tmp_path / "out.json")], env=env,
                          capture_output=True, text=True, timeout=120)


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _assert_refused(proc, tmp_path):
    assert proc.returncode != 0
    assert "Refusing to run against the real DB" in proc.stderr, proc.stderr[-2000:]
    assert not (tmp_path / "out.json").exists()


def test_live_tool_refuses_the_default_trade_db(tmp_path):
    home = tmp_path / "home"
    real = _touch(home / "Documents" / "ba2" / "trade" / "db.sqlite")
    _assert_refused(_run(LIVE_TOOL, tmp_path, home=home, db_file=real), tmp_path)


def test_live_tool_refuses_a_symlink_to_the_default_trade_db(tmp_path):
    home = tmp_path / "home"
    real = _touch(home / "Documents" / "ba2" / "trade" / "db.sqlite")
    link = tmp_path / "link.sqlite"
    try:
        link.symlink_to(real)
    except OSError as e:  # Windows without Developer Mode / SeCreateSymbolicLinkPrivilege: WinError 1314
        pytest.skip(f"this OS refused to create a symlink ({e}); the symlink refusal cannot be exercised here")
    _assert_refused(_run(LIVE_TOOL, tmp_path, home=home, db_file=link), tmp_path)


def test_live_tool_refuses_the_ba2_home_trade_db(tmp_path):
    ba2_home = tmp_path / "custom-home"
    real = _touch(ba2_home / "trade" / "db.sqlite")
    _assert_refused(_run(LIVE_TOOL, tmp_path, home=tmp_path / "home", ba2_home=ba2_home,
                         db_file=real), tmp_path)


def test_backtest_tool_refuses_the_default_test_db(tmp_path):
    home = tmp_path / "home"
    real = _touch(home / "Documents" / "ba2" / "test" / "dl_forecasting.db")
    _assert_refused(_run(BACKTEST_TOOL, tmp_path, home=home,
                         database_url=f"sqlite:///{real}"), tmp_path)


def test_backtest_tool_refuses_the_ba2_home_test_db(tmp_path):
    ba2_home = tmp_path / "custom-home"
    real = _touch(ba2_home / "test" / "dl_forecasting.db")
    _assert_refused(_run(BACKTEST_TOOL, tmp_path, home=tmp_path / "home", ba2_home=ba2_home,
                         database_url=f"sqlite:///{real}"), tmp_path)

