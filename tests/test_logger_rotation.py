"""Log rotation must not be permanently wedged by a second handle on the same file.

2026-07-28 incident. Every message after app.log crossed 10MB produced:

    --- Logging error ---
    PermissionError: [WinError 32] ... used by another process:
        '...\\logs\\app.log' -> '...\\logs\\app.log.1'

TWO causes, both real:

1. ``ba2_trade_platform/logger.py`` and ``packages/common/ba2_common/logger.py`` are
   near-identical duplicates and EACH constructed its own RotatingFileHandler for the
   SAME four paths (app.log, app.debug.log, all.debug.log, all.error.log). Two OS handles
   on one file inside one process: on Windows os.rename fails while any other handle is
   open, so rotation could never succeed and the files grew past maxBytes forever.

2. Other processes legitimately write the same tree (ad-hoc scripts run from the repo,
   backtest/grid workers), so even with (1) fixed a rotation can lose the race.

(1) is fixed by sharing one handler instance per path; (2) by tolerating the failure and
retrying later instead of raising through logging.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

import pytest


# ---------------------------------------------------------------------------
# (1) one handler object per file path, across BOTH logger trees
# ---------------------------------------------------------------------------

def test_no_file_is_opened_by_two_handlers(tmp_path):
    """The duplicate-handle bug: same path, two RotatingFileHandler objects, one process.

    Must mirror STARTUP, not import: the two modules' LOGS_DIR differ at import
    (ba2\\test\\logs vs ba2\\trade\\logs), and they only converge once
    ``main.initialize_system`` points both at ``<db folder>/logs`` via
    reconfigure_file_logging. Asserting on import-time state passes vacuously.
    """
    import ba2_common.logger as common_log
    import ba2_trade_platform.logger as app_log

    original_common, original_app = common_log.LOGS_DIR, app_log.LOGS_DIR
    shared = str(tmp_path / "logs")
    try:
        common_log.reconfigure_file_logging(shared)
        app_log.reconfigure_file_logging(shared)

        by_path = {}
        for lg in (app_log.logger, common_log.logger):
            for h in lg.handlers:
                if isinstance(h, RotatingFileHandler):
                    path = os.path.normcase(os.path.abspath(h.baseFilename))
                    by_path.setdefault(path, set()).add(id(h))

        assert by_path, "no file handlers found -- the assertion below would be vacuous"

        duplicated = {p: ids for p, ids in by_path.items() if len(ids) > 1}
        assert not duplicated, (
            "these log files are opened by more than one handler in this process, which "
            "permanently blocks os.rename-based rotation on Windows: "
            f"{sorted(os.path.basename(p) for p in duplicated)}"
        )
    finally:
        # Put the real logging tree back so later tests / this session are unaffected.
        common_log.reconfigure_file_logging(original_common)
        app_log.reconfigure_file_logging(original_app)


# ---------------------------------------------------------------------------
# (2) a lost rotation race must degrade, not raise
# ---------------------------------------------------------------------------

def _make_handler(path, monkeypatch=None):
    from ba2_common.logger import SharedRotatingFileHandler
    return SharedRotatingFileHandler(str(path), maxBytes=200, backupCount=2, encoding="utf-8")


def _record(msg="x" * 80):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)


def test_rotation_failure_does_not_raise_and_keeps_writing(tmp_path, monkeypatch):
    log = tmp_path / "app.log"
    h = _make_handler(log)
    try:
        h.emit(_record())  # get over maxBytes so the next emit tries to roll

        def _locked(src, dst):
            raise PermissionError(32, "used by another process")

        monkeypatch.setattr(os, "rename", _locked)
        monkeypatch.setattr(os, "replace", _locked, raising=False)

        h.emit(_record("second"))   # must not raise
        h.emit(_record("third"))
        h.flush()
    finally:
        h.close()

    body = log.read_text(encoding="utf-8")
    assert "second" in body and "third" in body, (
        "records were dropped when rotation failed; they must still be appended"
    )


def test_failed_rotation_is_not_retried_on_every_record(tmp_path, monkeypatch):
    """Without a cooldown, shouldRollover stays True and every subsequent record retries
    the rename + stream close/reopen — a hot-path cost on an already-oversized file."""
    log = tmp_path / "app.log"
    h = _make_handler(log)
    attempts = {"n": 0}

    def _locked(src, dst):
        attempts["n"] += 1
        raise PermissionError(32, "used by another process")

    try:
        h.emit(_record())
        monkeypatch.setattr(os, "rename", _locked)
        monkeypatch.setattr(os, "replace", _locked, raising=False)
        for _ in range(25):
            h.emit(_record())
    finally:
        h.close()

    assert attempts["n"] <= 2, (
        f"rotation was retried {attempts['n']} times for 25 records -- expected a cooldown"
    )


def test_rotation_still_works_when_the_file_is_not_locked(tmp_path):
    """The tolerance must not disable rotation in the normal case."""
    log = tmp_path / "app.log"
    h = _make_handler(log)
    try:
        for _ in range(6):
            h.emit(_record())
        h.flush()
    finally:
        h.close()

    assert (tmp_path / "app.log.1").exists(), "an unlocked file should still rotate"
