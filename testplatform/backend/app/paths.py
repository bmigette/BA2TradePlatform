"""Centralized on-disk paths for BA2TestPlatform artifacts.

Single source of truth for the BA2TestPlatform "test" bucket so NOTHING is
cached inside the code repo. Every artifact dir resolves under
``ba2_common.config.TEST_DIR`` (default ``~/Documents/ba2/test``), NOT the
process CWD or the backend tree (which was the bug — caches landed inside the
git checkout).

Each dir is individually env-overridable (``BA2_DATASETS_DIR`` etc.) and the
single root is overridable via ``BA2_HOME`` (see ba2_common.config). The dirs
are created on import so callers can write to them immediately.

Layout (under ``TEST_DIR``):
    datasets/            generated dataset CSVs                (DATASETS_DIR)
    trained_models/      saved model artifacts                (MODELS_DIR)
    cache/jobs/          per-job cache                        (JOBS_CACHE_DIR)
    cache/news/          news content files                   (NEWS_CACHE_DIR)
    news_exports/        exported news JSON                   (NEWS_EXPORTS_DIR)
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from ba2_common.config import TEST_DIR as _TEST_DIR
except Exception:  # pragma: no cover - ba2_common always installed in practice
    # Fallback keeps imports working in a minimal env; mirrors the locked default.
    _TEST_DIR = os.path.join(os.path.expanduser("~"), "Documents", "ba2", "test")

TEST_DIR = Path(_TEST_DIR)

DATASETS_DIR = Path(os.getenv("BA2_DATASETS_DIR", str(TEST_DIR / "datasets")))
MODELS_DIR = Path(os.getenv("BA2_MODELS_DIR", str(TEST_DIR / "trained_models")))
JOBS_CACHE_DIR = Path(os.getenv("BA2_JOBS_CACHE_DIR", str(TEST_DIR / "cache" / "jobs")))
NEWS_CACHE_DIR = Path(os.getenv("BA2_NEWS_CACHE_DIR", str(TEST_DIR / "cache" / "news")))
NEWS_EXPORTS_DIR = Path(os.getenv("BA2_NEWS_EXPORTS_DIR", str(TEST_DIR / "news_exports")))

# Create the artifact dirs on import so first-run writes never fail.
for _d in (DATASETS_DIR, MODELS_DIR, JOBS_CACHE_DIR, NEWS_CACHE_DIR, NEWS_EXPORTS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Never fail an import over a dir we can't create (e.g. read-only env).
        pass
