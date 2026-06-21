"""Self-update + restart core (monorepo-aware) — shared by the admin API and the worker CLI.

The platform was consolidated from 5 sibling repos into ONE monorepo
(``BA2TradePlatform/`` with ``packages/{common,providers,experts}`` + the two apps
``ba2_trade_platform/`` and ``testplatform/``). The old ``/api/admin/update`` assumed the
testplatform sub-dir WAS the git repo and rebuilt a ``-m uvicorn`` restart command from
``sys.argv`` — both wrong for the new layout/launcher. This module centralises the correct
behaviour so the FastAPI master (``app/api/admin.py``) AND a remote worker
(``ba2-test worker``) update + restart identically:

  * git root  = the monorepo root (the dir holding ``.git``), not the app sub-dir;
  * after a pull, if the shared packages are installed NON-editable (the install.sh default)
    their site-packages copies are STALE — so reinstall the ``common -> providers -> experts``
    chain into the current venv. Editable installs (``-e``) are live, so reinstall is skipped;
  * restart re-execs the SAME command that launched the process. ``ba2-test serve`` /
    ``ba2-test worker`` re-exec via ``python -m ba2test_launcher <args>`` (cross-platform —
    Windows console scripts are ``.exe`` and can't be run as ``python <script>``); a direct
    ``uvicorn app.main:app`` launch re-execs via ``python -m uvicorn <args>``.

Why a worker needs this (correctness, not just convenience): distributed GA trials must run
the IDENTICAL code as the master, or a trial's fitness depends on WHERE it ran and the
optimization is no longer reproducible. The worker compares its git commit to the master's
(``get_version_info``) and self-updates before claiming work.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

# The three shared packages, installed in dependency order (common <- providers <- experts).
_PACKAGE_DIRS = ("common", "providers", "experts")


def resolve_repo_root(start: Optional[Path] = None) -> Path:
    """Return the monorepo root: the nearest ancestor of *start* that contains ``.git``.

    Walks up from this file by default so it is correct regardless of the caller's cwd. Falls
    back to the known relative depth (this file lives at
    ``testplatform/backend/app/services/self_update.py`` -> 4 parents up is the repo root) if no
    ``.git`` is found (e.g. a tarball deploy without git — reinstall/restart still work; only the
    git pull is a no-op then).
    """
    here = (start or Path(__file__)).resolve()
    for cand in (here, *here.parents):
        if (cand / ".git").exists():
            return cand
    return Path(__file__).resolve().parents[4]


def _app_version(root: Path) -> str:
    """Read ``APP_VERSION`` from ``ba2_trade_platform/version.py`` by FILE (not import).

    The test venv does not install the trade app, so importing ``ba2_trade_platform.version``
    would fail there; reading the file works from either venv and from the monorepo checkout.
    """
    vf = root / "ba2_trade_platform" / "version.py"
    try:
        m = re.search(r"""APP_VERSION\s*=\s*["']([^"']+)["']""", vf.read_text(encoding="utf-8"))
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def _git_commit(root: Path) -> Optional[str]:
    """Short HEAD commit of the monorepo, or None if git is unavailable / not a repo."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def get_version_info(root: Optional[Path] = None) -> dict:
    """Identity of the running code: app version + git commit + repo root.

    The git commit is the authoritative equality check for "is this the same code?" — a worker
    compares it to the master's before claiming trials (determinism requirement).
    """
    root = root or resolve_repo_root()
    return {
        "app_version": _app_version(root),
        "git_commit": _git_commit(root),
        "editable": is_editable_install(root),
        "root": str(root),
    }


def is_editable_install(root: Path, package: str = "ba2_common") -> bool:
    """True iff *package* is installed editable (its source lives under ``<root>/packages``).

    Editable installs pick up a ``git pull`` immediately (no reinstall); a non-editable install
    copied the source into site-packages, so it is stale until reinstalled.
    """
    try:
        mod = importlib.import_module(package)
        mod_file = Path(getattr(mod, "__file__", "") or "").resolve()
        return mod_file.is_relative_to((root / "packages").resolve())
    except (ImportError, ValueError, OSError):
        return False


def _find_uv() -> Optional[str]:
    """Locate the ``uv`` bootstrapped into the current venv (install.sh puts it there)."""
    bindir = Path(sys.executable).parent
    for name in ("uv", "uv.exe"):
        cand = bindir / name
        if cand.is_file():
            return str(cand)
    return None


def git_pull(root: Path, timeout: int = 120) -> dict:
    """``git pull --ff-only`` in the monorepo root. Fast-forward only so a diverged/dirty
    checkout fails LOUDLY instead of creating a surprise merge commit on a deploy/worker box.
    Returns ``{returncode, output, stderr}``.
    """
    try:
        r = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(root), capture_output=True, text=True, timeout=timeout,
        )
        return {
            "returncode": r.returncode,
            "output": (r.stdout.strip() or r.stderr.strip()),
            "stderr": r.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "output": f"git pull timed out after {timeout}s", "stderr": "timeout"}


def reinstall_packages(root: Path, timeout: int = 900) -> dict:
    """Reinstall the shared chain (common -> providers -> experts) into the current venv.

    Mirrors ``install.sh:install_chain`` (uv with ``--no-sources``, falling back to pip). Needed
    after a pull when the packages are NON-editable, so source changes in ``packages/`` actually
    take effect on restart. Returns ``{ok, logs}``.
    """
    uv = _find_uv()
    logs: List[dict] = []
    for name in _PACKAGE_DIRS:
        pkg = root / "packages" / name
        if not pkg.is_dir():
            logs.append({"pkg": name, "returncode": 1, "output": f"missing package dir: {pkg}"})
            return {"ok": False, "logs": logs}
        if uv:
            cmd = [uv, "pip", "install", "--python", sys.executable, "--no-sources", str(pkg)]
        else:
            cmd = [sys.executable, "-m", "pip", "install", str(pkg)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            logs.append({"pkg": name, "returncode": 124, "output": f"reinstall timed out after {timeout}s"})
            return {"ok": False, "logs": logs}
        out = (r.stdout or "") + (r.stderr or "")
        logs.append({"pkg": name, "returncode": r.returncode, "output": out[-2000:]})
        if r.returncode != 0:
            return {"ok": False, "logs": logs}
    return {"ok": True, "logs": logs}


def perform_update(reinstall: str | bool = "auto", root: Optional[Path] = None) -> dict:
    """Pull latest code (+ reinstall the package chain when needed). Does NOT restart.

    *reinstall*: ``"auto"`` (default) reinstalls only when the packages are non-editable; ``True``
    always reinstalls; ``False`` never. Returns a report dict with ``ok`` and per-step detail.
    """
    root = root or resolve_repo_root()
    pull = git_pull(root)
    report: dict = {"ok": pull["returncode"] == 0, "root": str(root),
                    "git_pull": pull["output"], "version": get_version_info(root)}
    if pull["returncode"] != 0:
        report["step"] = "git_pull"
        return report

    editable = is_editable_install(root)
    do_reinstall = (reinstall is True) or (reinstall == "auto" and not editable)
    report["editable"] = editable
    report["reinstalled"] = False
    if do_reinstall:
        res = reinstall_packages(root)
        report["reinstalled"] = res["ok"]
        report["reinstall_logs"] = res["logs"]
        if not res["ok"]:
            report["ok"] = False
            report["step"] = "reinstall"
    # Refresh version AFTER the pull so callers see the new commit.
    report["version"] = get_version_info(root)
    return report


def build_restart_command() -> List[str]:
    """The command to re-exec for an in-place restart, matching how THIS process was launched.

    * ``ba2-test serve`` / ``ba2-test worker`` (the ``ba2test_launcher`` is imported) ->
      ``python -m ba2test_launcher <subcommand args>`` (cross-platform; Windows console scripts
      are ``.exe`` and can't be run as ``python <script>``);
    * direct ``uvicorn app.main:app ...`` -> ``python -m uvicorn <args>``;
    * a ``*.py`` script launch -> re-run that script;
    * otherwise best-effort re-run of argv under the interpreter.
    """
    argv = sys.argv
    if "ba2test_launcher" in sys.modules:
        return [sys.executable, "-m", "ba2test_launcher", *argv[1:]]
    base = os.path.basename(argv[0]).lower()
    if "uvicorn" in base:
        return [sys.executable, "-m", "uvicorn", *argv[1:]]
    if argv[0].endswith(".py") and os.path.isfile(argv[0]):
        return [sys.executable, *argv]
    return [sys.executable, *argv]


def _kill_children() -> None:
    """Terminate child processes (process-pool/uvicorn workers) so they don't orphan on re-exec."""
    try:
        import psutil
    except ImportError:
        logger.warning("psutil not available — child processes may survive restart")
        return
    try:
        current = psutil.Process()
        children = current.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.Error:
                pass
        psutil.wait_procs(children, timeout=5)
        for child in current.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
    except psutil.Error as e:
        logger.warning(f"child process cleanup error: {e}")


def restart_now(on_before_restart: Optional[Callable[[], None]] = None) -> None:
    """Replace the current process with a fresh copy (same launch command). Does NOT return."""
    logger.info("Restarting process...")
    if on_before_restart is not None:
        try:
            on_before_restart()
        except Exception as e:  # noqa: BLE001 — cleanup must never block the restart
            logger.warning(f"pre-restart cleanup failed (continuing): {e}")
    _kill_children()
    cmd = build_restart_command()
    logger.info("Restart command: %s", " ".join(cmd))
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, cmd)


def schedule_restart(delay: float = 2.0,
                     on_before_restart: Optional[Callable[[], None]] = None) -> None:
    """Restart after *delay* seconds in a daemon thread (so an HTTP response can flush first)."""
    def _run() -> None:
        time.sleep(delay)
        restart_now(on_before_restart)

    threading.Thread(target=_run, daemon=True, name="self-update-restart").start()


def stop_task_queues() -> None:
    """Best-effort stop of the FastAPI task queues (server-side pre-restart cleanup)."""
    try:
        from app.services.task_queue import (
            get_task_queue, get_training_task_queue,
            get_backtest_task_queue, get_ohlcv_task_queue,
        )
    except ImportError:
        return
    for getter in (get_training_task_queue, get_backtest_task_queue, get_task_queue, get_ohlcv_task_queue):
        try:
            getter().stop()
        except Exception:  # noqa: BLE001 — a queue that was never started is fine
            pass
    logger.info("All task queues stopped")
