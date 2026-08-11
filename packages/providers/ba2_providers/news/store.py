"""Shared on-disk news store: raw text and sentiment scores, physically separated.

WHY TWO TIERS. Export and sync consume the cache tree and, for the first time, need to
DISAGREE: the GDrive export wants the raw article text, the remote-worker sync must not
carry it. ``cache_sync.build_manifest`` recurses the ENTIRE cache tree on purpose ("no
allowlist to drift"), so the only way to keep raw text out of the manifest without
weakening that invariant is to put it outside ``CACHE_FOLDER`` altogether:

    RAW     <COMMON_DIR>/news_raw/<SYM>.parquet     master only, never synced
            url_hash, published_at, provider, source, title, summary, text

    SCORED  <CACHE_FOLDER>/news/<SYM>.parquet       synced, pruned, CRC-checked for free
            url_hash, published_at, provider, model, score, pos, neu, neg

Splitting by FILE rather than by column makes "a worker never loads article text" a
structural fact instead of a rule to remember at every read site. Experts and backtests
call ``read_sentiment`` only; nothing in the hot path can reach the raw tier by accident.

THE ``model`` COLUMN is what the split buys. A re-score appends rows under a new model
name without touching raw text, two models can coexist for comparison, and a file left
holding a stale mix is detectable rather than silent. It is also where the legacy
AlphaVantage rows are handled honestly: their vendor labels are a different scale from
FinBERT's and are excluded at migration rather than laundered into the same column.

NO LOOKAHEAD. ``read_sentiment`` filters ``published_at <= as_of``, the same contract the
rest of the cache enforces. Publication time is the only correct cut: an article about
Monday's close that a vendor backfilled on Friday must not be visible on Monday.

COVERAGE IS NOT DENSITY. These are different failures and the store reports them
differently (see ``read_sentiment``): a symbol absent from the store is a structural
misconfiguration, while a covered symbol having a quiet week is ordinary variation.
Collapsing the two is exactly the mistake that let the macro section run inert for months.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence

import pandas as pd

import ba2_common.config as _cfg
from ba2_common.logger import logger


# Both roots are resolved PER CALL through the config MODULE, never bound at import.
# `from ba2_common.config import CACHE_FOLDER` would snapshot the value, and the test
# harness rebinds `cfg.CACHE_FOLDER` after import (see packages/providers/tests/
# conftest.py, which makes the same point about native_cache) -- a snapshot would send
# test writes into the real ~/Documents cache tree.
def raw_folder() -> str:
    """Master-only raw text. OUTSIDE the cache root by design -- see the module
    docstring. Moving this under CACHE_FOLDER is what would ship article text to every
    remote worker."""
    return os.path.join(_cfg.COMMON_DIR, "news_raw")


def scored_folder() -> str:
    """Scores. UNDER the cache root, so they sync to workers with the rest of the cache."""
    return os.path.join(_cfg.CACHE_FOLDER, "news")

RAW_COLUMNS = ["url_hash", "published_at", "provider", "source", "title", "summary", "text"]
SCORED_COLUMNS = ["url_hash", "published_at", "provider", "model", "score", "pos", "neu", "neg"]

# Symbols become filenames. Anything outside this set could escape the store directory
# or collide across platforms, so it is rejected rather than sanitised -- a silently
# renamed symbol would read back as "no coverage", which now fails a backtest.
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")

_lock = threading.Lock()
_read_cache: dict = {}


class NewsStoreError(RuntimeError):
    """The store is unusable or a required symbol is absent from it."""


def _norm_symbol(symbol: str) -> str:
    sym = str(symbol).strip().upper()
    if not _SYMBOL_RE.match(sym):
        raise ValueError(f"Refusing to use {symbol!r} as a news store filename")
    return sym


def raw_path(symbol: str) -> str:
    return os.path.join(raw_folder(), f"{_norm_symbol(symbol)}.parquet")


def scored_path(symbol: str) -> str:
    return os.path.join(scored_folder(), f"{_norm_symbol(symbol)}.parquet")


def reset_cache() -> None:
    """Drop the in-process read cache. For tests and after a migration/rescore."""
    with _lock:
        _read_cache.clear()


def covered_symbols() -> List[str]:
    """Symbols with a scored file. Empty list if the store has never been built."""
    folder = scored_folder()
    if not os.path.isdir(folder):
        return []
    return sorted(f[:-8] for f in os.listdir(folder) if f.endswith(".parquet"))


def store_exists() -> bool:
    """True when the scored tier has been built at all.

    Distinct from per-symbol coverage: a missing directory means nobody ran the
    migration or the prewarm, which is a deployment fault, not a data gap.
    """
    return os.path.isdir(scored_folder()) and bool(covered_symbols())


def has_coverage(symbol: str) -> bool:
    return os.path.exists(scored_path(symbol))


def _atomic_write(df: pd.DataFrame, path: str, columns: Sequence[str]) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"Refusing to write {path}: missing columns {missing}")
    out = df.loc[:, list(columns)].copy()
    out["published_at"] = pd.to_datetime(out["published_at"], errors="coerce")
    if out["published_at"].isna().any():
        n = int(out["published_at"].isna().sum())
        raise ValueError(f"Refusing to write {path}: {n} rows have an unparseable published_at")
    out = out.sort_values("published_at").reset_index(drop=True)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Same tmp+replace dance as the rest of the cache: a worker syncing mid-write must
    # never see a half-written parquet, and .tmp is already skipped by build_manifest.
    tmp = f"{path}.tmp"
    out.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def write_raw(symbol: str, df: pd.DataFrame) -> int:
    _atomic_write(df, raw_path(symbol), RAW_COLUMNS)
    return len(df)


def write_scored(symbol: str, df: pd.DataFrame) -> int:
    _atomic_write(df, scored_path(symbol), SCORED_COLUMNS)
    with _lock:
        _read_cache.pop(_norm_symbol(symbol), None)
    return len(df)


def upsert_scored(symbol: str, df: pd.DataFrame) -> int:
    """Merge one model's rows into a symbol's scored file, leaving other models intact.

    ``write_scored`` replaces the whole file, which would delete ``finbert-legacy`` the
    first time a challenger is scored -- and the point of the ``model`` column is that
    models COEXIST so they can be compared on the same articles. This replaces only the
    rows whose model matches, so re-scoring is idempotent and never destructive.
    """
    models = set(df["model"].dropna().unique())
    if len(models) != 1:
        raise ValueError(f"upsert_scored expects exactly one model per call, got {models}")
    model = models.pop()

    if os.path.exists(scored_path(symbol)):
        existing = pd.read_parquet(scored_path(symbol))
        keep = existing[existing["model"] != model]
        merged = pd.concat([keep, df], ignore_index=True)
    else:
        merged = df
    return write_scored(symbol, merged)


def read_raw(symbol: str) -> pd.DataFrame:
    """Read the raw text tier. Scoring and export ONLY -- never an expert or a backtest.

    Deliberately not cached: the callers are batch jobs that stream symbol by symbol,
    and holding article text in a process-lifetime dict is precisely what the two-tier
    split exists to prevent.
    """
    path = raw_path(symbol)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No raw news for {symbol} at {path}. Raw text is master-only and is NOT "
            f"synced to workers -- if this is a remote worker, that is by design.")
    return pd.read_parquet(path)


def _load_scored(symbol: str) -> pd.DataFrame:
    sym = _norm_symbol(symbol)
    with _lock:
        hit = _read_cache.get(sym)
    if hit is not None:
        return hit
    df = pd.read_parquet(scored_path(sym))
    df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce")
    df = df.dropna(subset=["published_at"]).sort_values("published_at").reset_index(drop=True)
    with _lock:
        _read_cache[sym] = df
    return df


def read_sentiment(symbol: str, as_of: Optional[datetime] = None,
                   window_days: Optional[int] = None,
                   model: Optional[str] = None) -> pd.DataFrame:
    """Scored articles for ``symbol`` published in ``(as_of - window_days, as_of]``.

    ``as_of=None`` means "everything" (live, or a scoring pass). A returned frame that
    is EMPTY means the symbol is covered but had no qualifying articles in the window --
    a quiet week, which callers handle by dropping the section, not by failing.

    Raises rather than returning empty when the data is structurally unreachable:
      * ``NewsStoreError`` if the scored tier does not exist at all (nobody prewarmed);
      * ``NewsStoreError`` if this symbol has no file (outside the store's coverage).

    That distinction is the point. A silently empty result would let a GA tune a news
    weight on a universe where the section almost never fires -- a weight that looks
    harmless in-sample and transfers to nothing.
    """
    sym = _norm_symbol(symbol)
    if not store_exists():
        raise NewsStoreError(
            f"News is enabled but the scored store at {scored_folder()} is empty or missing. "
            f"Run tools/migrate_ml_news_cache.py (once) or the news prewarm for this run.")
    if not has_coverage(sym):
        raise NewsStoreError(
            f"News is enabled but {sym} is not in the news store. Coverage is limited to "
            f"{len(covered_symbols())} symbols; restrict the universe or disable news.")

    df = _load_scored(sym)
    if model is not None:
        df = df[df["model"] == model]
    if as_of is not None:
        cut = pd.Timestamp(as_of)
        if cut.tz is not None:
            cut = cut.tz_localize(None)
        df = df[df["published_at"] <= cut]
        if window_days is not None:
            df = df[df["published_at"] > cut - timedelta(days=int(window_days))]
    return df.reset_index(drop=True)


def aggregate_sentiment(df: pd.DataFrame, as_of: datetime,
                        half_life_days: float = 3.0) -> Optional[float]:
    """Half-life weighted mean of ``pos - neg`` over ``df``, in [-1, +1].

    Neutral mass is ignored rather than counted against the score: a neutral article is
    an absence of signal, not evidence of a negative one, so it should dilute nothing.

    Recency weighting is exponential with the given half-life -- a two-week-old headline
    should not carry the same weight as this morning's. Returns None for an empty frame;
    the ARTICLE-COUNT floor is the caller's decision, since it is a tunable setting.
    """
    if df is None or df.empty:
        return None
    cut = pd.Timestamp(as_of)
    if cut.tz is not None:
        cut = cut.tz_localize(None)
    age_days = (cut - df["published_at"]).dt.total_seconds() / 86400.0
    # Future-dated rows would get a weight > 1 and pull the mean toward whatever a
    # vendor mis-stamped. read_sentiment already cuts on as_of; clip is belt-and-braces.
    age_days = age_days.clip(lower=0.0)
    w = 0.5 ** (age_days / max(1e-9, float(half_life_days)))
    s = df["pos"].astype(float) - df["neg"].astype(float)
    total = float(w.sum())
    if total <= 0:
        return None
    return float((w * s).sum() / total)


def coverage_report() -> pd.DataFrame:
    """Per-symbol article counts and date ranges. For tools and acceptance checks."""
    rows = []
    for sym in covered_symbols():
        df = _load_scored(sym)
        rows.append({
            "symbol": sym,
            "articles": len(df),
            "first": df["published_at"].min() if len(df) else None,
            "last": df["published_at"].max() if len(df) else None,
            "models": ",".join(sorted(df["model"].dropna().unique())) if len(df) else "",
        })
    return pd.DataFrame(rows)
