"""Remote worker HTTP server — the MASTER pushes work to THIS (push model).

A DB-less FastAPI app the master dispatches to. It runs the SAME deterministic backtest code as
the master (``_trial_worker``) in its own process pool, mirrors the master's cache on demand
(tar push), and self-updates on request so distributed trials run identical code.

Run it with: ``ba2-test worker --port 8100 --password <secret> [--workers N]``.

Endpoints (every one bearer-checked against the worker password):
  GET  /health         -> {ok, capacity, cpu, gpu, version}
  GET  /version        -> {app_version, git_commit, ...}
  GET  /cache/manifest -> {files:[{rel_path,size,...}], ...}   (what this worker already has)
  POST /cache/push     -> accept a tar STREAM, extract into CACHE_FOLDER
  POST /run-trial      -> {config, fitness_metric} -> {ok, fitness, trades, error, fatal}
  POST /update         -> git pull + reinstall + restart (self_update)
"""

from __future__ import annotations

import hmac
import logging
import os
import tempfile
from concurrent.futures import ProcessPoolExecutor
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from app.services import cache_sync, self_update

logger = logging.getLogger(__name__)

# docs/openapi disabled: this is a headless worker, not a browsable API — don't expose its
# schema unauthenticated (the functional endpoints are all password-gated regardless).
worker_app = FastAPI(title="BA2 Remote Worker", docs_url=None, redoc_url=None, openapi_url=None)

# Set by run_worker_server() before uvicorn starts.
_PASSWORD: Optional[str] = None
_CAPACITY: int = 1
_POOL: Optional[ProcessPoolExecutor] = None


def _verify(authorization: Optional[str]) -> None:
    """Bearer-check against the worker password (constant-time)."""
    if not _PASSWORD:
        raise HTTPException(status_code=503, detail="Worker password not configured.")
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header.")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Expected 'Bearer <password>'.")
    if not hmac.compare_digest(parts[1], _PASSWORD):
        raise HTTPException(status_code=403, detail="Invalid worker password.")


class RunTrialReq(BaseModel):
    config: dict
    fitness_metric: str
    cache_root: Optional[str] = None  # the MASTER's CACHE_FOLDER, for path localization


class SecretsReq(BaseModel):
    settings: dict  # {app_setting_key: value_str}, e.g. {"FMP_API_KEY": "...", "finnhub_api_key": "..."}


def _localize_paths(obj, master_root: str, local_root: str):
    """Recursively rewrite any string under the MASTER's cache root to THIS worker's cache root.

    Trial configs embed absolute cache paths computed on the master (screener_store,
    screener_runtime.store, options_cache_db, ...). The master and worker don't share a filesystem
    layout, so a verbatim path would miss the locally-synced cache. This remaps the master prefix
    to the local one; non-cache strings pass through unchanged. OS-separator tolerant.
    """
    mr = master_root.replace("\\", "/").rstrip("/")
    if isinstance(obj, str):
        v = obj.replace("\\", "/")
        if v == mr or v.startswith(mr + "/"):
            rel = v[len(mr):].lstrip("/")
            return os.path.join(local_root, *rel.split("/")) if rel else local_root
        return obj
    if isinstance(obj, dict):
        return {k: _localize_paths(val, master_root, local_root) for k, val in obj.items()}
    if isinstance(obj, list):
        return [_localize_paths(x, master_root, local_root) for x in obj]
    return obj


def _hardware() -> dict:
    """cpu/gpu info, reusing the master's helper so the master's worker UI shows the same shape."""
    try:
        from app.api.workers import get_local_hardware_info
        cpu, gpu = get_local_hardware_info()
        return {"cpu": cpu, "gpu": gpu}
    except Exception:  # noqa: BLE001
        return {"cpu": {"cores": os.cpu_count(), "model": "unknown"}, "gpu": None}


@worker_app.get("/health")
def health(authorization: str = Header(default=None)):
    _verify(authorization)
    return {"ok": True, "capacity": _CAPACITY, "version": self_update.get_version_info(), **_hardware()}


@worker_app.get("/version")
def version(authorization: str = Header(default=None)):
    _verify(authorization)
    return self_update.get_version_info()


@worker_app.get("/cache/manifest")
def cache_manifest(authorization: str = Header(default=None)):
    _verify(authorization)
    return cache_sync.build_manifest()


@worker_app.post("/cache/push")
async def cache_push(request: Request, authorization: str = Header(default=None)):
    """Accept a tar STREAM from the master and extract it into CACHE_FOLDER.

    Spools the upload to a temp file (disk, not memory) so an arbitrarily large tar streams safely,
    then extracts (traversal-guarded). Returns ``{extracted, bytes, skipped}``.
    """
    _verify(authorization)
    tmp = tempfile.NamedTemporaryFile(prefix="ba2-cache-push-", suffix=".tar", delete=False)
    try:
        async for chunk in request.stream():
            tmp.write(chunk)
        tmp.close()
        with open(tmp.name, "rb") as fh:
            result = cache_sync.extract_tar(fh)
        logger.info("cache push: %s", result)
        return result
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@worker_app.post("/run-trial")
def run_trial(req: RunTrialReq, authorization: str = Header(default=None)):
    """Run ONE deterministic trial in the worker pool and return its summary (synchronous).

    Defined as a sync endpoint so FastAPI runs it in its threadpool — blocking on the process
    future here ties up one threadpool thread (fine), not the event loop. The master sends up to
    ``capacity`` concurrent calls to saturate the pool.
    """
    _verify(authorization)
    from app.services.strategy_optimization_handler import _trial_worker
    if _POOL is None:
        raise HTTPException(status_code=503, detail="Worker pool not initialized.")
    config = req.config
    if req.cache_root:
        from ba2_common.config import CACHE_FOLDER
        config = _localize_paths(req.config, req.cache_root, CACHE_FOLDER)
    try:
        return _POOL.submit(_trial_worker, config, req.fitness_metric).result()
    except Exception as e:  # noqa: BLE001 — surface as a failed trial, never 500 the dispatcher
        return {"ok": False, "fitness": 0.0, "trades": 0, "error": repr(e), "fatal": False}


@worker_app.post("/secrets")
def set_secrets(req: SecretsReq, authorization: str = Header(default=None)):
    """Upsert credential app-settings (FMP_API_KEY, finnhub_api_key) into THIS worker's ba2_common
    DB so its hermetic trials resolve them via get_app_setting.

    The worker is otherwise DB-less for app data, but ``_enter_backend`` configured ba2_common at the
    worker's on-disk default DB at startup; trial pool workers (``_worker_init``) point at the SAME
    file. Writing the keys here persists them across restarts (unlike env, which a self-update drops
    — the recurring 'FMP API key not configured' on remote trials). Idempotent upsert; values are
    never logged.
    """
    _verify(authorization)
    from sqlmodel import Session, select
    from ba2_common.core.db import get_engine, init_db
    from ba2_common.core.models import AppSetting
    n = 0
    try:
        init_db()  # ensure the AppSetting table exists in the worker's (possibly fresh) DB
        with Session(get_engine()) as s:
            for k, v in (req.settings or {}).items():
                if not v:
                    continue
                row = s.exec(select(AppSetting).where(AppSetting.key == k)).first()
                if row:
                    row.value_str = v
                    s.add(row)
                else:
                    s.add(AppSetting(key=k, value_str=v))
                n += 1
            s.commit()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"set_secrets failed: {e!r}")
    return {"set": n, "keys": sorted((req.settings or {}).keys())}


@worker_app.post("/update")
def update(authorization: str = Header(default=None)):
    """git pull + reinstall (if non-editable) + restart this worker process."""
    _verify(authorization)
    report = self_update.perform_update()
    if not report.get("ok"):
        raise HTTPException(status_code=500, detail=f"update failed: {report.get('git_pull')}")
    self_update.schedule_restart(delay=2.0)
    return {"restart": "scheduled", "version": report.get("version"), "git_pull": report.get("git_pull")}


def run_worker_server(host: str, port: int, password: str, n_workers: int) -> None:
    """Initialise the trial pool + start the uvicorn server. Called by ``ba2-test worker``."""
    import multiprocessing as _mp

    import uvicorn

    from app.services.strategy_optimization_handler import (
        _BACKEND_DIR, _WORKER_ENV_KEYS, _worker_init,
    )

    global _PASSWORD, _CAPACITY, _POOL
    if not password:
        raise SystemExit("ba2-test worker: --password (or $BA2_WORKER_PASSWORD) is required.")
    _PASSWORD = password
    _CAPACITY = max(1, n_workers)
    # Hermetic trials run cache-only, so provider keys aren't required here; mirror any that
    # happen to be set (harmless) so a non-hermetic edge still resolves them.
    env = {k: os.environ[k] for k in _WORKER_ENV_KEYS if os.environ.get(k)}
    _POOL = ProcessPoolExecutor(
        max_workers=_CAPACITY, mp_context=_mp.get_context("spawn"),
        initializer=_worker_init, initargs=(_BACKEND_DIR, env),
    )
    logger.info("worker server: %d trial slots, listening on %s:%d", _CAPACITY, host, port)
    print(f">> BA2 worker server: {_CAPACITY} slots, http://{host}:{port}  "
          f"(version {self_update.get_version_info().get('git_commit')})")
    try:
        uvicorn.run(worker_app, host=host, port=port, log_level="info")
    finally:
        if _POOL is not None:
            _POOL.shutdown(wait=False, cancel_futures=True)
