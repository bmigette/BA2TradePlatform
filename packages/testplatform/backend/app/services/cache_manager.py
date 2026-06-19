"""Cache discovery + usage scanner for the cache-management UI.

Single source of truth for every cache root. Normalizes the CWD path
inconsistency across the codebase by resolving every backend-relative root
against the backend dir, NOT the process CWD:

  - dataproviders/base.py:78   Path("backend/datasets/cache")  (CWD-relative)
  - app/api/datasets.py:738    Path("datasets")                (CWD-relative)
  - app/services/news_cache.py NewsCacheService(cache_dir="datasets/cache/news")
  - app/api/tools.py:244       NEWS_EXPORTS_DIR = Path("news_exports")
  - dataproviders/interfaces/MarketDataProviderInterface.py:23
        CACHE_FOLDER = os.getenv("CACHE_FOLDER", <backend>/cache)  (env-overridable)

The ``asof`` provider cache is a DIFFERENT root than the backend tree: it lives
under ``ba2_common.config.CACHE_FOLDER`` (default ~/Documents/ba2_trade_platform/cache,
NOT env-driven), with the native parquet time-series at ``<CACHE_FOLDER>/<provider>/``
(now the live OHLCV cache wired into ``get_ohlcv_data``) and the provider_cache
spill/SQLite at ``<CACHE_FOLDER>/datasets/cache``. The native_cache substrate now
lives in ``ba2_common.core.native_cache`` (re-exported by
``ba2_providers.cache.native_cache``). We import that constant rather than guess.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# backend/ root = parents[2] from app/services/cache_manager.py
# (app/services/cache_manager.py -> app/services -> app -> backend)
BACKEND_DIR = Path(__file__).resolve().parents[2]

# CACHE_FOLDER as the backend provider layer sees it. Now defers to the shared
# ba2_common cache (default ~/Documents/ba2/common/cache), NOT the old
# <backend>/cache path — nothing is cached inside the repo anymore. (Unused for
# ohlcv now; kept resolved here to avoid confusion.)
try:
    from ba2_common.config import CACHE_FOLDER as _COMMON_CACHE_FOLDER
except Exception:  # pragma: no cover
    _COMMON_CACHE_FOLDER = str(BACKEND_DIR / "cache")
CACHE_FOLDER = Path(os.getenv("CACHE_FOLDER", str(_COMMON_CACHE_FOLDER)))

# Test-bucket artifact dirs (datasets/models/job+news caches/exports) live under
# ba2_common.config.TEST_DIR, resolved centrally in app.paths — NOT BACKEND_DIR.
from app import paths as _paths


def _asof_roots() -> List[Path]:
    """Resolve the ba2_providers as_of cache roots from ba2_common.config.

    Returns both the native parquet time-series root (<CACHE_FOLDER>/<provider>/)
    and the provider_cache spill root (<CACHE_FOLDER>/datasets/cache). These live
    under ba2_common.config.CACHE_FOLDER, a DIFFERENT root than the backend's
    own <backend>/cache. Imported defensively so a backend without ba2_common
    installed still loads the scanner (empty asof type)."""
    try:
        from ba2_common.config import CACHE_FOLDER as ASOF_CACHE_FOLDER
    except Exception:
        return []
    base = Path(ASOF_CACHE_FOLDER)
    return [base, base / "datasets" / "cache"]


def _fmp_history_root() -> List[Path]:
    """Resolve the backtest-only FMP-history disk cache dir from ba2_providers.

    Per-symbol JSON payloads (analyst grades / price targets / past earnings /
    insider / financial statements / finnhub reco trends) written under
    ``<ba2_common CACHE_FOLDER>/fmp_history`` by ba2_providers.fmp_common during
    a frozen (backtest) run. Owned by ``_fmp_history_cache_dir()``; imported
    defensively so a backend without ba2_providers still loads the scanner.

    This dir lives UNDER the ``asof`` base root, so it is excluded from the asof
    scan/clear (see ``_FMP_HISTORY_EXCLUDE``) to avoid double-counting."""
    try:
        from ba2_providers.fmp_common import _fmp_history_cache_dir
    except Exception:
        return []
    return [Path(_fmp_history_cache_dir())]


def _fmp_history_exclude() -> Optional[Path]:
    """The fmp_history dir to exclude from the asof scan/clear, or None."""
    roots = _fmp_history_root()
    return roots[0] if roots else None


_FMP_HISTORY_EXCLUDE = _fmp_history_exclude()


def _ohlcv_roots() -> List[Path]:
    """OHLCV price-bar parquet dirs INSIDE the as_of provider cache.

    OHLCV is no longer a separate per-provider CSV cache under ``<backend>/cache`` (that path
    is dead — nothing writes it, which is why the old ``ohlcv`` type read 0 B). The live OHLCV
    bars are the native parquet time-series at ``<ba2_common CACHE_FOLDER>/<Provider>/`` where
    the provider class name carries ``OHLCV`` (FMPOHLCVProvider, AlpacaOHLCVProvider,
    EODHDOHLCVProvider, PolygonOHLCVProvider, AlphaVantageOHLCVProvider) — plus the odd-named
    YFinanceDataProvider. We resolve those dirs so the ``ohlcv`` type reports the REAL bar cache,
    and EXCLUDE them from the ``asof`` total so the two don't double-count."""
    try:
        from ba2_common.config import CACHE_FOLDER as ASOF_CACHE_FOLDER
    except Exception:
        return []
    base = Path(ASOF_CACHE_FOLDER)
    if not base.exists():
        return []
    out: List[Path] = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and ("OHLCV" in d.name or d.name == "YFinanceDataProvider"):
            out.append(d)
    return out


_OHLCV_ROOTS = _ohlcv_roots()


def _under(path: Path, ancestor: Optional[Path]) -> bool:
    """True if ``path`` is ``ancestor`` or nested under it (best-effort)."""
    if ancestor is None:
        return False
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except (ValueError, OSError):
        return False


def _excluded(path: Path, exclude_under: Any) -> bool:
    """True if ``path`` is under any excluded subtree. ``exclude_under`` may be None, a single
    Path, or a list/tuple of Paths (asof excludes both fmp_history and the OHLCV provider dirs)."""
    if exclude_under is None:
        return False
    excs = exclude_under if isinstance(exclude_under, (list, tuple)) else [exclude_under]
    return any(_under(path, a) for a in excs if a is not None)


def _resolve(p: "str | Path") -> Path:
    """Resolve a backend-relative path against BACKEND_DIR (absolute passes through)."""
    p = Path(p)
    return p if p.is_absolute() else (BACKEND_DIR / p)


# Cache-type -> on-disk root(s) + metadata. Mirrors the cache_ui_scope contract.
# DESTRUCTIVE types (datasets, models) are excluded from "clean all".
CACHE_TYPES: Dict[str, Dict[str, Any]] = {
    # OHLCV price bars: the native parquet under the as_of cache (<ba2_common CACHE_FOLDER>/
    # <*OHLCV*Provider>/), NOT the dead legacy <backend>/cache path. Resolved by _ohlcv_roots.
    "ohlcv":    {"roots": _OHLCV_ROOTS,                        "destructive": False, "ttl_hours": 24},
    "jobs":     {"roots": [_paths.JOBS_CACHE_DIR],            "destructive": False, "ttl_hours": None},
    "news":     {"roots": [_paths.NEWS_CACHE_DIR],            "destructive": False, "ttl_hours": None, "db_backed": True},
    "datasets": {"roots": [_paths.DATASETS_DIR],              "destructive": True,  "ttl_hours": None},
    "models":   {"roots": [_paths.MODELS_DIR],               "destructive": True,  "ttl_hours": None},
    "exports":  {"roots": [_paths.NEWS_EXPORTS_DIR],          "destructive": False, "ttl_hours": None},
    # ba2_providers as_of cache: parquet time-series + provider_cache spill, under
    # ba2_common.config.CACHE_FOLDER (NOT <backend>/cache). Resolved lazily.
    # The fmp_history subtree AND the OHLCV provider parquet dirs are excluded here (each is
    # counted/cleared as its own type) so the asof total = the OTHER as_of providers only.
    "asof":     {"roots": _asof_roots(),                      "destructive": False, "ttl_hours": None,
                 "exclude_under": [p for p in ([_FMP_HISTORY_EXCLUDE] + _OHLCV_ROOTS) if p is not None]},
    # ba2_providers backtest-only FMP-history disk cache: per-symbol JSON payloads
    # under <ba2_common CACHE_FOLDER>/fmp_history (a subtree of the asof base root).
    "fmp_history": {"roots": _fmp_history_root(),             "destructive": False, "ttl_hours": None},
}


def _scan_dir(root: Path, exclude_under: Optional[Any] = None) -> Dict[str, Any]:
    """Return total bytes, file count, oldest/newest mtime (ISO UTC) for a tree.

    ``exclude_under``: optional subtree(s) to skip — a single Path or a list of Paths (e.g.
    asof excludes fmp_history AND the OHLCV provider dirs, each counted as its own type)."""
    if exclude_under is None:
        excludes: List[Path] = []
    elif isinstance(exclude_under, (list, tuple)):
        excludes = [p for p in exclude_under if p is not None]
    else:
        excludes = [exclude_under]
    total = 0
    count = 0
    oldest: Optional[float] = None
    newest: Optional[float] = None
    if not root.exists():
        return {"bytes": 0, "files": 0, "oldest": None, "newest": None, "exists": False}
    for f in root.rglob("*"):
        if f.is_file():
            if any(_under(f, anc) for anc in excludes):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            total += st.st_size
            count += 1
            m = st.st_mtime
            oldest = m if oldest is None else min(oldest, m)
            newest = m if newest is None else max(newest, m)
    return {
        "bytes": total,
        "files": count,
        "exists": True,
        "oldest": datetime.fromtimestamp(oldest, tz=timezone.utc).isoformat() if oldest else None,
        "newest": datetime.fromtimestamp(newest, tz=timezone.utc).isoformat() if newest else None,
    }


def get_usage() -> Dict[str, Any]:
    """Per-type disk usage for every tracked cache (bytes, file count, oldest/newest
    mtime, destructive flag, TTL). News also reports DB row counts when available."""
    out: Dict[str, Any] = {}
    for name, cfg in CACHE_TYPES.items():
        agg: Dict[str, Any] = {
            "bytes": 0, "files": 0, "oldest": None, "newest": None, "exists": False,
            "destructive": cfg["destructive"], "ttl_hours": cfg["ttl_hours"],
        }
        for root in cfg["roots"]:
            s = _scan_dir(Path(root), exclude_under=cfg.get("exclude_under"))
            agg["bytes"] += s["bytes"]
            agg["files"] += s["files"]
            agg["exists"] = agg["exists"] or s["exists"]
            for k in ("oldest", "newest"):
                if s[k] and (
                    agg[k] is None
                    or (k == "oldest" and s[k] < agg[k])
                    or (k == "newest" and s[k] > agg[k])
                ):
                    agg[k] = s[k]
        if cfg.get("db_backed") and name == "news":
            try:
                from app.services.news_cache import NewsCacheService
                agg["db_stats"] = NewsCacheService().get_cache_stats()  # news_cache.py:550
            except Exception:
                agg["db_stats"] = None
        out[name] = agg
    return out


def drill_down(cache_type: str) -> List[Dict[str, Any]]:
    """Per-item breakdown for one cache type (UI drill-down).

    ohlcv: per <SYMBOL>_<interval> file under each provider subfolder.
    news:  per-provider article counts from the DB stats.
    jobs/models: per task_id directory size.
    fmp_history: per-namespace rollup (files grouped by the <namespace> prefix
        before "__" in each <namespace>__<SYMBOL>.json filename).
    datasets/exports/asof: flat file listing (asof excludes the fmp_history subtree).
    """
    cfg = CACHE_TYPES.get(cache_type)
    if not cfg:
        raise KeyError(cache_type)
    items: List[Dict[str, Any]] = []
    if cache_type == "ohlcv":
        # provider subfolders contain <SYMBOL>_<interval>.{csv,parquet}
        # (MarketDataProviderInterface.py:50 per-class subfolder; base.py file naming).
        for root in cfg["roots"]:
            root = Path(root)
            if not root.exists():
                continue
            for f in root.rglob("*"):
                if f.is_file() and f.suffix in (".csv", ".parquet"):
                    stem = f.stem  # SYMBOL_interval
                    sym, _, interval = stem.rpartition("_")
                    st = f.stat()
                    items.append({
                        "provider": f.parent.name,
                        "symbol": sym or stem,
                        "interval": interval,
                        "bytes": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                        "stale": (datetime.now().timestamp() - st.st_mtime) > 24 * 3600,
                    })
    elif cache_type == "news":
        try:
            from app.services.news_cache import NewsCacheService
            stats = NewsCacheService().get_cache_stats()  # by_provider counts
        except Exception:
            stats = {}
        for prov, n in (stats.get("by_provider") or {}).items():
            items.append({"provider": prov, "articles": n})
    elif cache_type in ("jobs", "models"):
        for root in cfg["roots"]:
            root = Path(root)
            if not root.exists():
                continue
            for d in root.iterdir():
                if d.is_dir():
                    size = sum(x.stat().st_size for x in d.rglob("*") if x.is_file())
                    items.append({"task_id": d.name, "bytes": size})
    elif cache_type == "fmp_history":
        # Per-namespace rollup: group <namespace>__<SYMBOL>.json by the prefix.
        groups: Dict[str, Dict[str, Any]] = {}
        for root in cfg["roots"]:
            root = Path(root)
            if not root.exists():
                continue
            for f in root.rglob("*"):
                if not (f.is_file() and f.suffix == ".json"):
                    continue
                ns = f.name.split("__", 1)[0] if "__" in f.name else f.stem
                try:
                    st = f.stat()
                except OSError:
                    continue
                g = groups.setdefault(ns, {"namespace": ns, "files": 0, "bytes": 0, "newest": None})
                g["files"] += 1
                g["bytes"] += st.st_size
                m = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                if g["newest"] is None or m > g["newest"]:
                    g["newest"] = m
        items = sorted(groups.values(), key=lambda g: g["namespace"])
    else:  # datasets, exports, asof — flat file listing
        exclude_under = cfg.get("exclude_under")
        for root in cfg["roots"]:
            root = Path(root)
            if not root.exists():
                continue
            for f in root.rglob("*"):
                if f.is_file():
                    if exclude_under is not None and _under(f, exclude_under):
                        continue
                    st = f.stat()
                    items.append({
                        "name": str(f.relative_to(root)),
                        "bytes": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    })
    return items


# ---------------------------------------------------------------------------
# Deletion (clean-all / by-type / by-date) — lock-safe, .tmp-aware.
#
# Reuses the type's native clear API when one exists:
#   - news  -> NewsCacheService.clear_cache(provider, ticker)  (DB rows, news_cache.py:585)
#   - ohlcv (symbol+interval filter) -> MarketDataProviderInterface.clear_cache
#       semantics, applied as a targeted file match across provider subfolders
#       (no provider instantiation -> no API-key requirement).
# Everything else is a careful file-tree delete that NEVER touches a ``.tmp``
# atomic-write staging file (writers do temp+rename under a per-file lock at
# MarketDataProviderInterface.py:55, so a half-written ``.tmp`` must survive).
#
# DESTRUCTIVE guard: ``clear_all`` skips datasets (dataset CSVs) + models
# (trained_models). Those are irreplaceable and clear only via an explicit
# per-type request (contract ml_datasets.cache_ui_scope.cross_cutting; plan §3.3).
# ---------------------------------------------------------------------------


def _is_tmp(f: Path) -> bool:
    """True for atomic-write staging files that must never be deleted."""
    return f.suffix == ".tmp" or f.name.endswith(".tmp")


def _safe_unlink(f: Path) -> int:
    """Delete one file, skipping .tmp atomic-write staging files. Returns bytes freed."""
    if _is_tmp(f):
        return 0
    try:
        size = f.stat().st_size
    except OSError:
        return 0
    try:
        f.unlink()
    except OSError:
        return 0
    return size


def _delete_tree(
    root: "str | Path",
    before: Optional[datetime] = None,
    name_match: Optional[Any] = None,
    exclude_under: Optional[Path] = None,
) -> Dict[str, int]:
    """Delete files under ``root`` (recursive), skipping .tmp staging files.

    ``before``       : only delete files whose mtime is strictly older than this.
    ``name_match``   : optional predicate(Path)->bool; only matching files deleted.
    ``exclude_under``: optional subtree to never delete (e.g. asof excludes the
                       fmp_history subtree, which is cleared as its own type).
    Empty directories left behind by deletions are pruned (best-effort).
    """
    root = Path(root)
    freed = 0
    removed = 0
    if not root.exists():
        return {"bytes_freed": 0, "files_removed": 0}
    for f in list(root.rglob("*")):
        if not f.is_file():
            continue
        if _is_tmp(f):
            continue
        if _excluded(f, exclude_under):
            continue
        if name_match is not None and not name_match(f):
            continue
        if before is not None:
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime >= before:
                continue
        b = _safe_unlink(f)
        if b or not f.exists():
            freed += b
            removed += 1
    # prune now-empty dirs (deepest first); never remove the root itself or the
    # excluded subtree.
    for d in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        if _excluded(d, exclude_under):
            continue
        try:
            next(d.iterdir())
        except StopIteration:
            try:
                d.rmdir()
            except OSError:
                pass
        except OSError:
            pass
    return {"bytes_freed": freed, "files_removed": removed}


def clear_type(
    cache_type: str,
    before: Optional[datetime] = None,
    symbol: Optional[str] = None,
    interval: Optional[str] = None,
    provider: Optional[str] = None,
    ticker: Optional[str] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Clear one cache type, optionally filtered.

    Routes through the type's native clear API when one exists (news DB rows),
    else lock-aware file deletion. Honors .tmp atomic-write staging files (never
    deleted). Destructive types (datasets/models) ARE cleared when named here
    explicitly — only ``clear_all`` skips them.
    """
    cfg = CACHE_TYPES.get(cache_type)
    if cfg is None:
        raise KeyError(cache_type)

    if cache_type == "news":
        # DB rows via the native service; orphaned content files via file delete.
        db_rows = 0
        try:
            from app.services.news_cache import NewsCacheService
            db_rows = NewsCacheService().clear_cache(provider=provider, ticker=ticker)
        except Exception as exc:  # keep endpoint usable even if DB unavailable
            logger_msg = f"news clear_cache skipped: {exc}"
            import logging as _logging
            _logging.getLogger(__name__).warning(logger_msg)
        file_res = {"bytes_freed": 0, "files_removed": 0}
        for root in cfg["roots"]:
            r = _delete_tree(root, before)
            file_res["bytes_freed"] += r["bytes_freed"]
            file_res["files_removed"] += r["files_removed"]
        return {"db_rows_deleted": db_rows, **file_res}

    # ohlcv symbol/interval filter: targeted file match across provider subfolders.
    name_match: Optional[Any] = None
    if cache_type == "ohlcv" and (symbol or interval):
        def name_match(f: Path) -> bool:  # noqa: E306
            if f.suffix not in (".csv", ".parquet"):
                return False
            stem = f.stem  # SYMBOL_interval
            f_sym, _, f_int = stem.rpartition("_")
            if symbol and f_sym.upper() != symbol.upper():
                return False
            if interval and f_int != interval:
                return False
            return True

    exclude_under = cfg.get("exclude_under")
    result = {"bytes_freed": 0, "files_removed": 0}
    for root in cfg["roots"]:
        target = Path(root)
        if cache_type in ("jobs", "models") and task_id:
            target = target / task_id
        r = _delete_tree(target, before=before, name_match=name_match,
                         exclude_under=exclude_under)
        result["bytes_freed"] += r["bytes_freed"]
        result["files_removed"] += r["files_removed"]
    return result


def clear_all(before: Optional[datetime] = None) -> Dict[str, Any]:
    """Clean every NON-destructive cache type.

    Excludes datasets (dataset CSVs) + models (trained_models): the cross_cutting
    rule means only an explicit per-type request may delete those.
    """
    out: Dict[str, Any] = {}
    for name, cfg in CACHE_TYPES.items():
        if cfg["destructive"]:
            out[name] = {"skipped": "destructive — clear explicitly by type"}
            continue
        out[name] = clear_type(name, before=before)
    return out
