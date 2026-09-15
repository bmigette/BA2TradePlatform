"""Plan / build / verify the central market-condition feature store (design section 4.4).

ONE orchestration for the CLI (``tools/warm_market_conditions.py``), a job queue and live
pre-analysis preparation; entry points contribute arguments and reporting only.

PHASES

1. :func:`plan` -- inventory, no feature computation. Decision sessions are every regular session
   in ``[start, end]``; the feature rows they need are their PRIOR sessions (``prior_session_v1``);
   each row needs the 128 sessions ending at it, so the earliest raw bar is 127 sessions before the
   first row. Per symbol: the FMP daily cache file versus that span (``missing_file`` /
   ``stale_tail`` / internal holes / a listing younger than the window / conflicting duplicate
   bars), the rows already published under the same (symbol, session, window digest) by any
   manifest of the profile (reusable, never recomputed), and the PREFLIGHT:

   * ``certify_source_columns`` must report the cache consistent (else the plan carries a
     preflight error and nothing is built);
   * the split calendar is consulted for every universe symbol; any split dated after the cached
     file's first bar is checked AT THAT DATE (``ba2_common.core.split_basis``) and a file not
     verifiably on one basis is ``refetch_required`` -- never silently kept. A split calendar that
     cannot be READ is a preflight error for that symbol: without it nothing can tell an appended
     mixed basis from a clean one, so the symbol is refused rather than warmed on an unproven
     basis.

EXCLUSIONS ARE NEVER SILENT. A symbol that ends up unusable (no split calendar, a basis still
unverifiable after a full re-fetch, no source data at all) blocks the publication: ``build``
refuses with the inventory unless the operator passes ``allow_exclusions``, and only then is the
manifest published with that symbol recorded in its coverage exceptions and in the report.

2. :func:`build` -- ``fetch_missing=False`` stops with the actionable inventory when raw coverage
   is missing or a symbol needs a re-fetch, and fetches NOTHING. ``fetch_missing=True`` fetches
   only the missing coverage through the provider cache path (bounded concurrency, one in-flight
   request per symbol) and performs a FULL re-fetch for ``refetch_required`` symbols. Then, per
   symbol under a claim file (lock + heartbeat, mirroring ``shared_arrays.DerivedArrayStore``) and
   a host-wide builder slot: one snapshot read of the cache file (mtime/size checked before and
   after; a file that changed mid-read is re-read, never published as a mixture), raw shards per
   month, every registered field of the profile per session from ONE window, reuse of every
   published row whose window digest is unchanged, one immutable object per (symbol, month) of
   rows that had to be (re)built, and a progress record per object (resume trusts only objects
   that re-hash to their name). The manifest is published last.

3. :func:`verify` -- re-hash every object the manifest references.

NEGATIVE ROWS. A row whose window cannot be assembled (young listing, hole, stale tail) is
published with its status and reason and an identity over the bars that DO exist in its span
(``unavailable_window_digest``): when the bar arrives the identity changes and the row is rebuilt.

Local coordination files (claims, host slots, progress records) live under
``<cache_root>/_derived/market_conditions_build``: machine-local, never mirrored by cache sync.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field as dc_field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

import numpy as np

from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions, prior_regular_session, \
    regular_sessions_ending_at
from ba2_common.core.market_condition_context import TIMING_POLICY_PRIOR_SESSION_V1
from ba2_common.core.market_condition_source import (
    SOURCE_PROFILE_FMP_DAILY,
    assemble_window,
    certify_source_columns,
    fmp_daily_cache_path,
    read_fmp_daily_cache,
    window_digest,
)
from ba2_common.core.market_condition_store import (
    SCHEMA_VERSION,
    MarketConditionStore,
    ObjectEntry,
    RawEntry,
    month_of,
    sha256_file,
    unavailable_window_digest,
)
from ba2_common.core.market_conditions import (
    COMPUTE_BY_PROFILE,
    PROFILES,
    STATUS_VALID,
    STATUSES,
    WINDOW,
    FeatureRow,
)
from ba2_common.core.split_basis import (
    REFETCH_VERDICTS,
    CalendarSplit,
    SplitBasisCheck,
    check_split_basis,
    read_full_fetch_marker,
)

__all__ = [
    "MC_PLAN_VERSION",
    "WarmupSource",
    "SymbolInventory",
    "MarketConditionWarmPlan",
    "BuildReport",
    "WarmupConfigError",
    "plan",
    "build",
    "verify",
    "local_build_dir",
]

MC_PLAN_VERSION = 1

RAW_PRESENT = "present"
RAW_MISSING_FILE = "missing_file"
RAW_STALE_TAIL = "stale_tail"

#: A (symbol, month) covered by more objects than this is compacted into one new object.
MAX_SEGMENTS_PER_MONTH = 8
#: Re-reads of a cache file that keeps changing while being read before the symbol fails.
SNAPSHOT_RETRIES = 3
#: How long a local progress record (a resume hint for an interrupted build) is kept.
PROGRESS_MAX_AGE_S = 24 * 3600.0
#: Claim / host-slot lock staleness (heartbeated while held) and wait poll.
CLAIM_STALE_S = float(os.getenv("BA2_MC_CLAIM_STALE_S", "900"))
CLAIM_POLL_S = 0.2
_BUILD_DIRNAME = "market_conditions_build"


class WarmupConfigError(ValueError):
    """An invalid profile / source profile / window / root (CLI exit code 2)."""


class WarmupSource(Protocol):
    """What the warmup needs from a provider. ``calls``/``bytes`` are monotonically increasing
    counters of provider requests and transferred bytes."""

    calls: int
    bytes: int

    def split_calendar(self, symbol: str) -> List[CalendarSplit]: ...

    def fetch_daily(self, symbol: str, start: date, end: date) -> None: ...

    def force_full_refetch(self, symbol: str) -> None: ...


def local_build_dir(cache_root: os.PathLike) -> Path:
    from ba2_common.core.shared_arrays import DERIVED_DIRNAME
    return Path(cache_root) / DERIVED_DIRNAME / _BUILD_DIRNAME


def _log_noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
@dataclass
class SymbolInventory:
    symbol: str
    cache_file: Optional[str] = None
    cache_rows: int = 0
    cache_first: Optional[str] = None
    cache_last: Optional[str] = None
    raw_state: str = RAW_MISSING_FILE
    missing_from: Optional[str] = None
    missing_to: Optional[str] = None
    missing_sessions: int = 0
    holes: int = 0
    first_hole: Optional[str] = None
    young_listing: bool = False
    source_conflicts: List[str] = dc_field(default_factory=list)
    split_events: List[List[Any]] = dc_field(default_factory=list)
    split_checks: List[Dict[str, Any]] = dc_field(default_factory=list)
    split_calendar_error: Optional[str] = None
    refetch_required: bool = False
    rows_required: int = 0
    rows_reusable: int = 0
    rows_missing: int = 0

    @property
    def fetch_needed(self) -> bool:
        return self.raw_state in (RAW_MISSING_FILE, RAW_STALE_TAIL)

    def split_calendar_preflight_message(self) -> Optional[str]:
        """The preflight error text for an unreadable split calendar (None when it was read)."""
        if not self.split_calendar_error:
            return None
        return (f"{self.symbol}: split calendar unavailable ({self.split_calendar_error}); the cached "
                "history cannot be proven to be on one split basis")

    def blocking_items(self) -> List[Dict[str, Any]]:
        items = []
        if self.split_calendar_error:
            items.append({"symbol": self.symbol, "kind": "split_calendar_unavailable",
                          "error": self.split_calendar_error, "fetchable": False})
        if self.raw_state == RAW_MISSING_FILE:
            items.append({"symbol": self.symbol, "kind": RAW_MISSING_FILE,
                          "missing_from": self.missing_from, "missing_to": self.missing_to,
                          "sessions": self.missing_sessions})
        elif self.raw_state == RAW_STALE_TAIL:
            items.append({"symbol": self.symbol, "kind": RAW_STALE_TAIL, "cache_last": self.cache_last,
                          "missing_from": self.missing_from, "missing_to": self.missing_to,
                          "sessions": self.missing_sessions})
        if self.refetch_required:
            items.append({"symbol": self.symbol, "kind": "refetch_required",
                          "splits": [c for c in self.split_checks if c["verdict"] in REFETCH_VERDICTS]})
        return items


@dataclass
class MarketConditionWarmPlan:
    profile: str
    calc_version: str
    source_profile: str
    timing_policy: str
    cache_root: str
    universe: List[str]
    start: str
    end: str
    decision_sessions: int
    first_row_session: str
    last_row_session: str
    earliest_raw_bar: str
    certification: Dict[str, Any]
    preflight_errors: List[str]
    symbols: List[SymbolInventory]
    created_at: str
    elapsed_s: float = 0.0
    #: Provider requests / bytes the PLAN itself spent (the split calendar).
    provider_calls: int = 0
    provider_bytes: int = 0
    plan_version: int = MC_PLAN_VERSION

    # -- views
    def symbol(self, sym: str) -> SymbolInventory:
        for s in self.symbols:
            if s.symbol == sym:
                return s
        raise KeyError(sym)

    def blocking_inventory(self) -> List[Dict[str, Any]]:
        return [item for s in self.symbols for item in s.blocking_items()]

    def waivable_preflight_errors(self) -> List[str]:
        """Preflight errors an operator may waive with ``allow_exclusions`` (the symbol is then
        excluded from the manifest and recorded): an unreadable split calendar."""
        return [m for m in (s.split_calendar_preflight_message() for s in self.symbols) if m]

    def fatal_preflight_errors(self) -> List[str]:
        """Preflight errors nothing can waive: the source certification itself failed."""
        waivable = set(self.waivable_preflight_errors())
        return [e for e in self.preflight_errors if e not in waivable]

    def summary(self) -> Dict[str, Any]:
        return {
            "profile": self.profile, "universe": len(self.universe), "start": self.start, "end": self.end,
            "decision_sessions": self.decision_sessions, "rows_required": sum(s.rows_required for s in self.symbols),
            "rows_reusable": sum(s.rows_reusable for s in self.symbols),
            "rows_missing": sum(s.rows_missing for s in self.symbols),
            "earliest_raw_bar": self.earliest_raw_bar, "preflight_ok": not self.preflight_errors,
            "blocking": len(self.blocking_inventory()),
            "fatal_preflight_errors": len(self.fatal_preflight_errors()),
            "refetch_required": [s.symbol for s in self.symbols if s.refetch_required],
            "split_calendar_errors": [s.symbol for s in self.symbols if s.split_calendar_error],
            "provider_calls": self.provider_calls, "provider_bytes": self.provider_bytes,
            "elapsed_s": self.elapsed_s,
        }

    # -- serialisation
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=1, sort_keys=True)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "MarketConditionWarmPlan":
        d = dict(d)
        if d.get("plan_version") != MC_PLAN_VERSION:
            raise WarmupConfigError(f"plan version {d.get('plan_version')!r} is not {MC_PLAN_VERSION}")
        d["symbols"] = [SymbolInventory(**s) for s in d["symbols"]]
        return cls(**d)

    def save(self, path: os.PathLike) -> None:
        tmp = f"{path}.{os.getpid()}.part"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(self.to_json())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: os.PathLike) -> "MarketConditionWarmPlan":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def _as_iso(ds: Iterable[date]) -> List[str]:
    return [d.isoformat() for d in ds]


def _decision_sessions(start: date, end: date) -> List[date]:
    return [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(start, end)]


@dataclass
class _Snapshot:
    path: str
    signature: Tuple[int, int]
    days: np.ndarray        # datetime64[D], sorted, unique
    bars: np.ndarray        # float64 (n, 5): o, h, l, c, v
    conflicts: List[date]


class SourceChangedError(RuntimeError):
    """A cache file kept changing while it was read."""


class ClaimLostError(RuntimeError):
    """This builder's claim was broken as stale and taken over by another builder mid-build."""


def _normalize_bars(dates: np.ndarray, cols: Sequence[np.ndarray]) -> Tuple[np.ndarray, np.ndarray, List[date]]:
    order = np.argsort(dates, kind="stable")
    d = dates[order]
    mat = np.stack([np.asarray(c, dtype=np.float64)[order] for c in cols], axis=1)
    if len(d) < 2:
        return d, mat, []
    uniq, first_idx, counts = np.unique(d, return_index=True, return_counts=True)
    conflicts: List[date] = []
    if (counts > 1).any():
        keep = np.ones(len(uniq), dtype=bool)
        for k in np.flatnonzero(counts > 1):
            rows = mat[d == uniq[k]]
            if not all(np.array_equal(rows[0], r, equal_nan=True) for r in rows[1:]):
                keep[k] = False
                conflicts.append(uniq[k].astype(object))
        d, mat = uniq[keep], mat[first_idx[keep]]
    return d, mat, conflicts


def _read_snapshot(cache_root: str, symbol: str) -> Tuple[Optional[_Snapshot], int]:
    """(snapshot | None when there is no cache file, retries used)."""
    path = fmp_daily_cache_path(symbol, cache_root)
    if path is None:
        return None, 0
    for attempt in range(SNAPSHOT_RETRIES + 1):
        st1 = os.stat(path)
        dates, o, h, l, c, v = read_fmp_daily_cache(path)
        st2 = os.stat(path)
        sig1, sig2 = (st1.st_mtime_ns, st1.st_size), (st2.st_mtime_ns, st2.st_size)
        if sig1 == sig2:
            d, mat, conflicts = _normalize_bars(dates, (o, h, l, c, v))
            return _Snapshot(path=path, signature=sig2, days=d, bars=mat, conflicts=conflicts), attempt
    raise SourceChangedError(f"{path} changed while being read {SNAPSHOT_RETRIES + 1} times")


@dataclass
class _RowWindow:
    session: date
    ok: bool
    status: str
    reason: str
    digest: str
    lo: int                 # span [lo, hi) into the snapshot arrays
    hi: int


def _row_windows(snap: Optional[_Snapshot], cal: np.ndarray, n_rows: int) -> List[_RowWindow]:
    """One entry per required row (``cal`` = the ``n_rows + WINDOW - 1`` sessions ending at the
    last row). Valid windows take a direct slice; anything else goes through ``assemble_window``
    for its status/reason (the single definition of a failed window)."""
    out: List[_RowWindow] = []
    if snap is None:
        return out
    d = snap.days
    for k in range(n_rows):
        first, last = cal[k], cal[k + WINDOW - 1]
        session = last.astype(object)
        lo = int(np.searchsorted(d, first, side="left"))
        hi = int(np.searchsorted(d, last, side="right"))
        cols = snap.bars[lo:hi]
        if hi - lo == WINDOW and np.array_equal(d[lo:hi], cal[k:k + WINDOW]):
            dg = window_digest(cols[:, 0], cols[:, 1], cols[:, 2], cols[:, 3], cols[:, 4])
            out.append(_RowWindow(session, True, STATUS_VALID, "", dg, lo, hi))
            continue
        head = snap.bars[:hi]
        res = assemble_window(d[:hi], head[:, 0], head[:, 1], head[:, 2], head[:, 3], head[:, 4], session)
        if res.ok:  # cannot happen when the slice check failed, but never guess
            dg = window_digest(*res.arrays())
            out.append(_RowWindow(session, True, STATUS_VALID, "", dg, lo, hi))
            continue
        dg = unavailable_window_digest(d[lo:hi], cols[:, 0], cols[:, 1], cols[:, 2], cols[:, 3], cols[:, 4])
        out.append(_RowWindow(session, False, res.status, res.reason, dg, lo, hi))
    return out


def _calendar_span(first_row: date, last_row: date, n_rows: int) -> np.ndarray:
    cal = np.array(regular_sessions_ending_at(last_row, n_rows + WINDOW - 1), dtype="datetime64[D]")
    if cal[WINDOW - 1].astype(object) != first_row:
        raise WarmupConfigError(f"row sessions {first_row}..{last_row} are not {n_rows} consecutive sessions")
    return cal


class _ManifestIndex:
    """The profile's published objects for THIS build's universe, read once.

    TWO THINGS IT MUST GET RIGHT.

    *Compatibility.* A window digest is over BARS, so it says nothing about the calculator that
    turned them into values. Reusing a row across a ``calc_version`` bump would publish a manifest
    declaring the new version over values computed by the old one -- the exact silent corruption
    ``market_condition_readers._check_row`` refuses at read time. A manifest is therefore a reuse
    candidate only when its ``calc_version``, ``schema_version`` AND field list match the profile
    being built.

    *Cost.* Each manifest JSON is parsed ONCE per build (a 3 MB manifest costs ~9 ms) and only the
    entries of the plan's own symbols are kept, instead of re-reading every manifest for every
    symbol and retaining an ``ObjectEntry`` for every object of every manifest ever published."""

    def __init__(self, store: MarketConditionStore, profile: str, universe: Iterable[str]):
        self.store, self.profile = store, profile
        self.universe = {s.upper() for s in universe}
        spec = PROFILES[profile]
        self.calc_version = spec.calc_version
        self.fields = [f.name for f in spec.fields]
        self._by_symbol: Optional[Dict[str, Dict[str, ObjectEntry]]] = None
        self._lock = threading.Lock()

    def compatible(self, manifest: Mapping[str, Any]) -> bool:
        return (manifest.get("calc_version") == self.calc_version
                and manifest.get("schema_version") == SCHEMA_VERSION
                and list(manifest.get("fields", ())) == self.fields)

    def _load(self) -> Dict[str, Dict[str, ObjectEntry]]:
        by_symbol: Dict[str, Dict[str, ObjectEntry]] = {}
        for dg in self.store.list_manifests(self.profile):
            try:
                m = self.store.read_manifest(dg, self.profile)
            except Exception:
                continue  # an unreadable/tampered manifest offers nothing to reuse
            if not self.compatible(m):
                continue
            for o in m["objects"]:
                if o["symbol"] in self.universe:
                    by_symbol.setdefault(o["symbol"], {})[o["sha256"]] = ObjectEntry.from_dict(o)
        return by_symbol

    def entries(self, symbol: str) -> List[ObjectEntry]:
        with self._lock:
            if self._by_symbol is None:
                self._by_symbol = self._load()
            return list(self._by_symbol.get(symbol, {}).values())


def _progress_dir(cache_root: str, profile: str, symbol: str) -> Path:
    return local_build_dir(cache_root) / "progress" / profile / symbol.upper()


def _write_progress(cache_root: str, profile: str, entry: ObjectEntry, calc_version: str) -> None:
    """Record a published object so a resume (or a builder that waited on our claim) finds it
    before any manifest names it. Stamped with the versions it was built at."""
    d = _progress_dir(cache_root, profile, entry.symbol)
    d.mkdir(parents=True, exist_ok=True)
    payload = dict(entry.to_dict(), calc_version=calc_version, schema_version=SCHEMA_VERSION)
    tmp = d / f"{entry.sha256}.json.{os.getpid()}.{threading.get_ident()}.part"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, d / f"{entry.sha256}.json")


def _progress_entries(cache_root: str, profile: str, symbol: str, calc_version: str,
                      keep: Optional[set] = None) -> List[ObjectEntry]:
    """Progress records for ``symbol`` built at ``calc_version``/``SCHEMA_VERSION``.

    A record from another calculator version, one older than ``PROGRESS_MAX_AGE_S`` (a build that
    died and was never resumed) or one this build has superseded (``keep`` names the objects that
    are still current) is DELETED here: a record is a local resume hint, never evidence, and an
    unbounded pile of them would slow every later build's candidate scan."""
    d = _progress_dir(cache_root, profile, symbol)
    out = []
    if not d.exists():
        return out
    now = time.time()
    for p in d.glob("*.json"):
        drop = False
        try:
            with open(p, "r", encoding="utf-8") as f:
                payload = json.load(f)
            entry = ObjectEntry.from_dict(payload)
            drop = (payload.get("calc_version") != calc_version
                    or payload.get("schema_version") != SCHEMA_VERSION
                    or now - p.stat().st_mtime > PROGRESS_MAX_AGE_S
                    or (keep is not None and entry.sha256 not in keep))
        except (OSError, ValueError, KeyError):
            drop = True
            entry = None
        if drop:
            try:
                p.unlink()
            except OSError:
                pass
            continue
        out.append(entry)
    return out


@dataclass
class _Candidate:
    entry: ObjectEntry
    sessions: List[date]
    digests: List[str]
    table: Any = None


def _load_candidates(store: MarketConditionStore, index: _ManifestIndex, cache_root: str, profile: str,
                     symbol: str, *, verify_hashes: bool, fields: Sequence[str]) -> List[_Candidate]:
    entries: Dict[str, ObjectEntry] = {}
    # Progress records FIRST, manifests second: the previous holder of this symbol's claim
    # publishes its manifest BEFORE deleting its progress records, so this order can never miss an
    # object THAT holder published. (A builder still running elsewhere may of course publish more
    # after this read; its objects are simply not candidates yet.)
    progress = _progress_entries(cache_root, profile, symbol, index.calc_version)
    for e in progress + index.entries(symbol):
        if e.symbol == symbol:
            entries.setdefault(e.sha256, e)
    out = []
    want_cols = ["symbol", "session", *fields]
    for e in sorted(entries.values(), key=lambda x: (x.month, x.sha256)):
        p = store.abspath(e.path)
        if not p.exists():
            continue
        if verify_hashes:
            try:
                if sha256_file(p) != e.sha256:
                    continue  # corrupt / partial: never trusted
            except OSError:
                continue
        try:
            t = store.read_table(e.path)
        except Exception:
            continue
        if not all(c in t.schema.names for c in want_cols) or t.num_rows != e.rows:
            continue
        out.append(_Candidate(entry=e, sessions=t.column("session").to_pylist(),
                              digests=t.column("window_digest").to_pylist(), table=t))
    return out


def _split_checks_for(snap: Optional[_Snapshot], events: Sequence[CalendarSplit], symbol: str) -> List[SplitBasisCheck]:
    if snap is None or not len(snap.days):
        return []
    b = snap.bars
    return check_split_basis(snap.days, b[:, 0], b[:, 1], b[:, 2], b[:, 3], events, symbol=symbol,
                             marker=read_full_fetch_marker(snap.path))


def _inventory_symbol(inv: SymbolInventory, snap: Optional[_Snapshot], cal: np.ndarray, n_rows: int,
                      candidates: List[_Candidate]) -> List[_RowWindow]:
    earliest, last_row = cal[0], cal[-1]
    inv.rows_required = n_rows
    inv.source_conflicts = _as_iso(snap.conflicts) if snap else []
    if snap is None or not len(snap.days):
        inv.cache_file, inv.cache_rows, inv.cache_first, inv.cache_last = (snap.path if snap else None), 0, None, None
        inv.raw_state = RAW_MISSING_FILE
        inv.missing_from, inv.missing_to = str(earliest), str(last_row)
        inv.missing_sessions = len(cal)
        inv.holes, inv.first_hole, inv.young_listing = 0, None, False
        inv.rows_reusable, inv.rows_missing = 0, n_rows
        return []
    d = snap.days
    inv.cache_file, inv.cache_rows = snap.path, int(len(d))
    inv.cache_first, inv.cache_last = str(d[0]), str(d[-1])
    covered = cal[(cal >= d[0]) & (cal <= d[-1])]
    present = np.isin(covered, d, assume_unique=True)
    inv.holes = int((~present).sum())
    inv.first_hole = str(covered[~present][0]) if inv.holes else None
    inv.young_listing = bool(d[0] > earliest)
    tail = cal[cal > d[-1]]
    if len(tail):
        inv.raw_state = RAW_STALE_TAIL
        inv.missing_from, inv.missing_to, inv.missing_sessions = str(tail[0]), str(tail[-1]), int(len(tail))
    else:
        inv.raw_state = RAW_PRESENT
        inv.missing_from = inv.missing_to = None
        inv.missing_sessions = 0
    windows = _row_windows(snap, cal, n_rows)
    known = {(s, dg) for cand in candidates for s, dg in zip(cand.sessions, cand.digests)}
    inv.rows_reusable = sum(1 for w in windows if (w.session, w.digest) in known)
    inv.rows_missing = n_rows - inv.rows_reusable
    return windows


def plan(profile: str, universe: Sequence[str], start: date, end: date,
         source_profile: str = SOURCE_PROFILE_FMP_DAILY, cache_root: Optional[os.PathLike] = None, *,
         source: Optional[WarmupSource] = None, log: Callable[[str], None] = _log_noop) -> MarketConditionWarmPlan:
    """Inventory + preflight for warming ``profile`` over ``universe`` and decisions in ``[start, end]``.

    ``source`` supplies the split calendar (default: the FMP source for ``cache_root``)."""
    t0 = time.monotonic()
    if profile not in PROFILES or profile not in COMPUTE_BY_PROFILE:
        raise WarmupConfigError(f"unknown market-condition profile {profile!r}; registered: {sorted(PROFILES)}")
    if source_profile != SOURCE_PROFILE_FMP_DAILY:
        raise WarmupConfigError(f"unsupported source profile {source_profile!r}; only {SOURCE_PROFILE_FMP_DAILY!r}")
    if end < start:
        raise WarmupConfigError(f"end {end} is before start {start}")
    if cache_root is None:
        from ba2_common import config
        cache_root = config.CACHE_FOLDER
    cache_root = os.path.abspath(os.fspath(cache_root))
    symbols = sorted({s.strip().upper() for s in universe if s and s.strip()})
    if not symbols:
        raise WarmupConfigError("the universe is empty")
    decisions = _decision_sessions(start, end)
    if not decisions:
        raise WarmupConfigError(f"no regular session in [{start}, {end}]")
    first_row, last_row = prior_regular_session(decisions[0]), prior_regular_session(decisions[-1])
    n_rows = len(decisions)
    cal = _calendar_span(first_row, last_row, n_rows)
    if source is None:
        from ba2_providers.market_conditions.fmp_source import FMPWarmupSource
        source = FMPWarmupSource(cache_root)
    calls0, bytes0 = source.calls, source.bytes

    preflight: List[str] = []
    cert = certify_source_columns(cache_root)
    cert_dict = {"source_profile": cert.source_profile, "consistent": cert.consistent,
                 "symbols": [{"symbol": s.symbol, "split_date": s.split_date.isoformat(), "basis": s.basis,
                              "consistent": s.consistent, "reason": s.reason} for s in cert.symbols]}
    if not cert.consistent:
        bad = [f"{s.symbol} {s.split_date}: {s.basis} ({s.reason})" for s in cert.symbols if not s.consistent]
        preflight.append(f"source certification failed for {source_profile}: {bad}")

    store = MarketConditionStore(cache_root)
    index = _ManifestIndex(store, profile, symbols)
    fields = [f.name for f in PROFILES[profile].fields]
    inventories = []
    for sym in symbols:
        inv = SymbolInventory(symbol=sym)
        snap, _retries = _read_snapshot(cache_root, sym)
        cands = _load_candidates(store, index, cache_root, profile, sym, verify_hashes=False, fields=fields)
        _inventory_symbol(inv, snap, cal, n_rows, cands)
        try:
            events = list(source.split_calendar(sym))
            inv.split_events = [[e.date.isoformat(), float(e.ratio)] for e in events]
            checks = _split_checks_for(snap, events, sym)
            inv.split_checks = [c.to_dict() for c in checks]
            inv.refetch_required = any(c.verdict in REFETCH_VERDICTS for c in checks)
        except Exception as e:  # noqa: BLE001 -- a preflight error for this symbol, never a note
            inv.split_calendar_error = f"{type(e).__name__}: {e}"
            preflight.append(inv.split_calendar_preflight_message())
        inventories.append(inv)
        log(f"plan {sym}: raw={inv.raw_state} rows={inv.rows_required} reusable={inv.rows_reusable} "
            f"refetch_required={inv.refetch_required}"
            + (f" split_calendar_error={inv.split_calendar_error}" if inv.split_calendar_error else ""))
    return MarketConditionWarmPlan(
        profile=profile, calc_version=PROFILES[profile].calc_version, source_profile=source_profile,
        timing_policy=TIMING_POLICY_PRIOR_SESSION_V1, cache_root=cache_root, universe=symbols,
        start=start.isoformat(), end=end.isoformat(), decision_sessions=n_rows,
        first_row_session=first_row.isoformat(), last_row_session=last_row.isoformat(),
        earliest_raw_bar=str(cal[0]), certification=cert_dict, preflight_errors=preflight,
        symbols=inventories, created_at=datetime.now(timezone.utc).isoformat(),
        elapsed_s=round(time.monotonic() - t0, 4), provider_calls=int(source.calls - calls0),
        provider_bytes=int(source.bytes - bytes0))


# ---------------------------------------------------------------------------
# Claims (lock + heartbeat) and host builder slots
# ---------------------------------------------------------------------------
class _FileClaim:
    """An O_EXCL lock file owned by a token, heartbeated (mtime) while held; a lock whose mtime is
    older than ``CLAIM_STALE_S`` belongs to a dead builder and is broken. Mirrors
    ``shared_arrays.DerivedArrayStore``'s build lock."""

    def __init__(self, path: Path):
        self.path = path
        self.token = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: Set when the heartbeat finds the lock is no longer ours (a waiter broke it as stale and
        #: took over). Whatever we were doing must not count as done.
        self.lost = False

    def _stale(self) -> bool:
        """True when the lock is older than ``CLAIM_STALE_S`` OR has vanished -- the same rule as
        ``shared_arrays.DerivedArrayStore._lock_is_stale``. A vanished lock read as "held" would
        park every waiter on a lock nobody owns."""
        try:
            return time.time() - self.path.stat().st_mtime > CLAIM_STALE_S
        except OSError:
            return True

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._stale():
                    return False
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "w") as f:
                f.write(self.token)
            self._start_heartbeat()
            return True

    def acquire(self, on_wait: Optional[Callable[[], None]] = None) -> bool:
        """Block until held. Returns True when this call had to WAIT for another holder."""
        waited = False
        t0 = time.monotonic()
        while not self.try_acquire():
            if not waited and on_wait is not None:
                on_wait()
            waited = True
            if time.monotonic() - t0 > 2 * CLAIM_STALE_S:
                raise TimeoutError(f"waited {2 * CLAIM_STALE_S:.0f}s for {self.path}")
            time.sleep(CLAIM_POLL_S)
        return waited

    def _start_heartbeat(self) -> None:
        interval = max(0.05, min(30.0, CLAIM_STALE_S / 4))

        def beat():
            while not self._stop.wait(interval):
                # Refresh ONLY while the lock is still ours. A builder that overran
                # CLAIM_STALE_S has had its lock broken and re-taken; touching that file would
                # keep the NEW owner's lock alive (and, if the new owner dies, keep a dead lock
                # fresh forever) while we went on believing we held it.
                try:
                    if self.path.read_text(encoding="utf-8") != self.token:
                        self.lost = True
                        return
                    os.utime(self.path, None)
                except FileNotFoundError:
                    self.lost = True
                    return
                except OSError:
                    pass

        self._stop.clear()
        self._thread = threading.Thread(target=beat, name=f"mc-claim-{self.path.name}", daemon=True)
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            if self.path.read_text(encoding="utf-8") == self.token:
                self.path.unlink()
        except OSError:
            pass


def max_host_builders() -> int:
    raw = os.getenv("BA2_MC_MAX_HOST_BUILDERS")
    if raw:
        return max(1, int(raw))
    return max(1, (os.cpu_count() or 2) // 2)


class _HostSlot:
    """One of ``max_host_builders()`` host-wide builder slots (files shared by every process)."""

    def __init__(self, cache_root: str):
        self.dir = local_build_dir(cache_root) / "slots"
        self.claim: Optional[_FileClaim] = None

    def __enter__(self):
        n = max_host_builders()
        t0 = time.monotonic()
        while True:
            for i in range(n):
                c = _FileClaim(self.dir / f"slot-{i}.lock")
                if c.try_acquire():
                    self.claim = c
                    return self
            if time.monotonic() - t0 > 2 * CLAIM_STALE_S:
                raise TimeoutError(f"no free market-condition builder slot in {self.dir}")
            time.sleep(CLAIM_POLL_S)

    def __exit__(self, *exc):
        if self.claim is not None:
            self.claim.release()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
@dataclass
class BuildReport:
    ok: bool
    exit_code: int
    profile: str
    manifest_digest: Optional[str] = None
    counters: Dict[str, int] = dc_field(default_factory=dict)
    elapsed_s: Dict[str, float] = dc_field(default_factory=dict)
    inventory: List[Dict[str, Any]] = dc_field(default_factory=list)
    #: Symbols left OUT of the manifest and why (empty unless ``allow_exclusions`` was passed).
    excluded: Dict[str, List[Dict[str, Any]]] = dc_field(default_factory=dict)
    exceptions: Dict[str, List[Dict[str, Any]]] = dc_field(default_factory=dict)
    errors: List[str] = dc_field(default_factory=list)
    waited_symbols: List[str] = dc_field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_COUNTER_KEYS = ("provider_calls", "provider_bytes", "rows_total", "rows_computed", "rows_reused",
                 "objects_written", "objects_reused", "raw_objects_written", "raw_objects_reused",
                 "symbols_built", "symbols_excluded", "snapshot_retries", "full_refetches", "range_fetches")


class _Counters:
    def __init__(self):
        self._lock = threading.Lock()
        self.values = {k: 0 for k in _COUNTER_KEYS}

    def add(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.values[key] += int(n)


def _fetch_phase(plan_: MarketConditionWarmPlan, source: WarmupSource, concurrency: int, counters: _Counters,
                 log: Callable[[str], None]) -> Dict[str, str]:
    """Fetch missing coverage / full re-fetches. Returns {symbol: error} for failed fetches."""
    jobs: Dict[str, Tuple[str, Optional[str], Optional[str]]] = {}
    for inv in plan_.symbols:
        if inv.refetch_required:
            jobs[inv.symbol] = ("full", None, None)          # a full re-fetch also covers the tail
        elif inv.fetch_needed:
            jobs[inv.symbol] = ("range", plan_.earliest_raw_bar if inv.raw_state == RAW_MISSING_FILE
                                else inv.missing_from, inv.missing_to)
    errors: Dict[str, str] = {}

    def run(sym: str) -> None:
        kind, frm, to = jobs[sym]
        try:
            if kind == "full":
                source.force_full_refetch(sym)
                counters.add("full_refetches")
            else:
                source.fetch_daily(sym, date.fromisoformat(frm), date.fromisoformat(to))
                counters.add("range_fetches")
            log(f"fetch {sym}: {kind}" + ("" if kind == "full" else f" {frm}..{to}"))
        except Exception as e:  # noqa: BLE001 -- recorded per symbol and reported
            errors[sym] = f"{type(e).__name__}: {e}"
            log(f"fetch {sym}: FAILED {errors[sym]}")

    if not jobs:
        return errors
    # ``jobs`` is keyed by symbol, so a symbol is submitted exactly once: one in-flight request
    # per symbol, at most ``concurrency`` at a time.
    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as ex:
        for fut in [ex.submit(run, sym) for sym in sorted(jobs)]:
            fut.result()
    return errors


@dataclass
class _SymbolResult:
    symbol: str
    objects: List[ObjectEntry]
    raw_objects: Dict[str, RawEntry]
    coverage: Dict[str, Any]
    waited: bool = False


def _raw_shards(store: MarketConditionStore, snap: _Snapshot, cal: np.ndarray, counters: _Counters
                ) -> Tuple[List[Tuple[int, int, RawEntry]], Dict[str, RawEntry]]:
    """Publish the snapshot's bars as monthly shards over the months the plan spans. Returns
    [(start_idx, end_idx, entry)] in chronological order and {sha: entry}."""
    d = snap.days
    lo_month = np.datetime64(month_of(cal[0].astype(object)), "M")
    hi_month = np.datetime64(month_of(cal[-1].astype(object)), "M")
    months = d.astype("datetime64[M]")
    sel = np.flatnonzero((months >= lo_month) & (months <= hi_month))
    shards: List[Tuple[int, int, RawEntry]] = []
    by_sha: Dict[str, RawEntry] = {}
    if not len(sel):
        return shards, by_sha
    start = int(sel[0])
    end = int(sel[-1]) + 1
    i = start
    while i < end:
        m = months[i]
        j = int(np.searchsorted(months, m, side="right"))
        j = min(j, end)
        b = snap.bars[i:j]
        entry, reused = store.write_raw_shard(d[i:j], b[:, 0], b[:, 1], b[:, 2], b[:, 3], b[:, 4])
        counters.add("raw_objects_reused" if reused else "raw_objects_written")
        shards.append((i, j, entry))
        by_sha[entry.sha256] = entry
        i = j
    return shards, by_sha


def _parquet_rows(path: Path) -> int:
    import pyarrow.parquet as pq
    return int(pq.ParquetFile(path).metadata.num_rows)


def _raw_ref(shards: List[Tuple[int, int, RawEntry]], lo: int, hi: int) -> Tuple[str, int, int]:
    if hi <= lo:
        return "", 0, 0
    used = [(s, e, entry) for s, e, entry in shards if s < hi and e > lo]
    base = used[0][0]
    return ";".join(entry.sha256 for _s, _e, entry in used), lo - base, hi - base


def _row_record(window: _RowWindow, row: FeatureRow, fields: Sequence[str], ref: Tuple[str, int, int]) -> Dict[str, Any]:
    obs = row.by_field()
    return {"session": window.session, "values": [obs[f].value for f in fields],
            "status": [obs[f].status for f in fields], "reasons": [obs[f].reason for f in fields],
            "window_digest": window.digest, "raw_shard_ref": ref[0], "raw_row_lo": ref[1], "raw_row_hi": ref[2]}


def _copied_record(cand: _Candidate, idx: int, fields: Sequence[str], window: _RowWindow,
                   ref: Tuple[str, int, int]) -> Dict[str, Any]:
    t = cand.table
    return {"session": window.session,
            "values": [None if STATUSES[t.column(f"{f}_status")[idx].as_py()] != STATUS_VALID
                       else t.column(f)[idx].as_py() for f in fields],
            "status": [STATUSES[t.column(f"{f}_status")[idx].as_py()] for f in fields],
            "reasons": [t.column(f"{f}_reason")[idx].as_py() for f in fields],
            "window_digest": window.digest, "raw_shard_ref": ref[0], "raw_row_lo": ref[1], "raw_row_hi": ref[2]}


def _coverage_from(statuses: List[Tuple[date, List[str]]], fields: Sequence[str],
                   extra: List[Dict[str, Any]]) -> Dict[str, Any]:
    statuses = sorted(statuses, key=lambda x: x[0])
    exceptions: List[Dict[str, Any]] = list(extra)
    agg: Dict[Tuple[str, str], List[date]] = {}
    for session, sts in statuses:
        for f, st in zip(fields, sts):
            if st != STATUS_VALID:
                agg.setdefault((f, st), []).append(session)
    for (f, st), sessions in sorted(agg.items()):
        exceptions.append({"kind": "status", "field": f, "status": st, "rows": len(sessions),
                           "first_session": sessions[0].isoformat(), "last_session": sessions[-1].isoformat()})
    return {"first_session": statuses[0][0].isoformat() if statuses else None,
            "last_session": statuses[-1][0].isoformat() if statuses else None,
            "rows": len(statuses), "exceptions": exceptions}


def _build_symbol(store: MarketConditionStore, index: _ManifestIndex, plan_: MarketConditionWarmPlan,
                  inv: SymbolInventory, cal: np.ndarray, counters: _Counters,
                  extra_exceptions: List[Dict[str, Any]], log: Callable[[str], None],
                  claim: Optional["_FileClaim"] = None) -> _SymbolResult:
    profile = PROFILES[plan_.profile]
    fields = [f.name for f in profile.fields]
    compute = COMPUTE_BY_PROFILE[plan_.profile]
    sym = inv.symbol
    n_rows = plan_.decision_sessions

    snap, retries = _read_snapshot(plan_.cache_root, sym)
    counters.add("snapshot_retries", retries)
    if snap is None or not len(snap.days):
        counters.add("symbols_excluded")
        return _SymbolResult(sym, [], {}, {"first_session": None, "last_session": None, "rows": 0,
                                            "exceptions": extra_exceptions + [{"kind": "no_source_data"}]})
    extra = list(extra_exceptions)
    if snap.conflicts:
        extra.append({"kind": "source_conflict", "sessions": _as_iso(snap.conflicts)})
    tail = cal[cal > snap.days[-1]]
    if len(tail):
        extra.append({"kind": "raw_unavailable", "first_session": str(tail[0]), "last_session": str(tail[-1]),
                      "sessions": int(len(tail))})

    shards, raw_by_sha = _raw_shards(store, snap, cal, counters)
    windows = _row_windows(snap, cal, n_rows)
    candidates = _load_candidates(store, index, plan_.cache_root, profile.name, sym, verify_hashes=True,
                                  fields=fields)
    reusable: Dict[date, Tuple[_Candidate, int]] = {}
    current = {w.session: w for w in windows}
    for cand in candidates:
        for i, (s, dg) in enumerate(zip(cand.sessions, cand.digests)):
            w = current.get(s)
            if w is not None and w.digest == dg and s not in reusable:
                reusable[s] = (cand, i)

    by_month: Dict[str, List[_RowWindow]] = {}
    for w in windows:
        by_month.setdefault(month_of(w.session), []).append(w)

    objects: List[ObjectEntry] = []
    statuses: List[Tuple[date, List[str]]] = []
    used_raw: Dict[str, RawEntry] = {}

    def _note_raw(refs: Iterable[str]) -> None:
        for ref in refs:
            for sha in filter(None, ref.split(";")):
                if sha not in raw_by_sha:
                    raw_by_sha[sha] = RawEntry(store.raw_rel(sha), sha,
                                               _parquet_rows(store.abspath(store.raw_rel(sha))))
                used_raw[sha] = raw_by_sha[sha]
    for month in sorted(by_month):
        ws = by_month[month]
        wanted = {w.session for w in ws}
        whole = [c for c in candidates if c.entry.month == month
                 and all(s in wanted and current[s].digest == dg for s, dg in zip(c.sessions, c.digests))]
        whole.sort(key=lambda c: (-c.entry.rows, c.entry.sha256))
        chosen: List[_Candidate] = []
        covered: set = set()
        for c in whole:
            if covered.isdisjoint(c.sessions):
                chosen.append(c)
                covered.update(c.sessions)
        remaining = [w for w in ws if w.session not in covered]
        if remaining and len(chosen) + 1 > MAX_SEGMENTS_PER_MONTH:
            chosen, covered, remaining = [], set(), list(ws)
        for c in chosen:
            objects.append(c.entry)
            counters.add("objects_reused")
            counters.add("rows_reused", c.entry.rows)
            t = c.table
            st_cols = [t.column(f"{f}_status").to_pylist() for f in fields]
            for i, s in enumerate(c.sessions):
                statuses.append((s, [STATUSES[col[i]] for col in st_cols]))
            _note_raw(set(t.column("raw_shard_ref").to_pylist()))
        if not remaining:
            continue
        records = []
        for w in remaining:
            ref = _raw_ref(shards, w.lo, w.hi)
            hit = reusable.get(w.session)
            if hit is not None:
                rec = _copied_record(hit[0], hit[1], fields, w, ref)
                counters.add("rows_reused")
            else:
                if w.ok:
                    b = snap.bars[w.lo:w.hi]
                    row = compute(b[:, 0], b[:, 1], b[:, 2], b[:, 3], b[:, 4])
                else:
                    row = FeatureRow.uniform(profile, w.status, w.reason)
                rec = _row_record(w, row, fields, ref)
                counters.add("rows_computed")
            records.append(rec)
            statuses.append((w.session, list(rec["status"])))
        if claim is not None and claim.lost:
            raise ClaimLostError(f"{sym}: the build claim was taken over mid-build; not publishing "
                                 "this symbol's progress")
        entry, reused = store.write_feature_object(profile, sym, records)
        counters.add("objects_reused" if reused else "objects_written")
        objects.append(entry)
        _note_raw({rec["raw_shard_ref"] for rec in records})
        # Progress at the shard boundary: a later build (a resume, or a builder that waited on our
        # claim) finds this object before any manifest names it -- after re-hashing it.
        _write_progress(plan_.cache_root, profile.name, entry, index.calc_version)

    counters.add("rows_total", len(windows))
    counters.add("symbols_built")
    # Records for objects this build superseded are no longer resume hints: drop them -- but
    # ONLY while the claim is still ours. A builder whose claim was broken as stale is looking at
    # the NEW owner's records, and deleting those would throw away the resume hints of a build
    # that is still running.
    if claim is None or not claim.lost:
        _progress_entries(plan_.cache_root, profile.name, sym, index.calc_version,
                          keep={e.sha256 for e in objects})
    return _SymbolResult(sym, objects, used_raw, _coverage_from(statuses, fields, extra))


def build(plan_: MarketConditionWarmPlan, *, fetch_missing: bool, concurrency: int = 4,
          log: Callable[[str], None] = _log_noop, source: Optional[WarmupSource] = None,
          allow_exclusions: bool = False) -> BuildReport:
    """Fetch (optionally), build and publish the manifest for ``plan_``. See the module docstring.

    ``allow_exclusions`` authorises publishing a manifest that leaves symbols OUT (an unreadable
    split calendar, a basis still unverifiable after a full re-fetch, no source data): without it
    such a symbol is an actionable inventory item and nothing is published.

    Exit codes in the report: 0 published; 1 actionable inventory / preflight / build failure."""
    t_total = time.monotonic()
    counters = _Counters()
    report = BuildReport(ok=False, exit_code=1, profile=plan_.profile)
    if plan_.profile not in PROFILES or PROFILES[plan_.profile].calc_version != plan_.calc_version:
        raise WarmupConfigError(f"plan profile {plan_.profile!r} at {plan_.calc_version!r} is not registered "
                                f"(registered: {[(p, s.calc_version) for p, s in PROFILES.items()]})")
    fatal = plan_.fatal_preflight_errors()
    if fatal:
        report.errors = fatal
        report.elapsed_s = {"total": round(time.monotonic() - t_total, 4)}
        log(f"build refused: preflight failed: {fatal}")
        return report
    blocking = plan_.blocking_inventory()
    unwaived = [i for i in blocking if not i.get("fetchable", True)]
    if unwaived and not allow_exclusions:
        report.inventory = unwaived
        report.errors = plan_.waivable_preflight_errors()
        report.elapsed_s = {"total": round(time.monotonic() - t_total, 4)}
        log(f"build refused: {len(unwaived)} symbol(s) cannot be warmed and exclusions were not "
            f"authorised (pass allow_exclusions): {unwaived}")
        return report
    blocking = [i for i in blocking if i.get("fetchable", True)]
    if blocking and not fetch_missing:
        report.inventory = blocking
        report.counters = dict(counters.values)
        report.elapsed_s = {"total": round(time.monotonic() - t_total, 4)}
        log(f"build stopped (cache-only): {len(blocking)} missing/refetch item(s); nothing fetched")
        return report

    if source is None and (blocking and fetch_missing):
        from ba2_providers.market_conditions.fmp_source import FMPWarmupSource
        source = FMPWarmupSource(plan_.cache_root)
    calls0 = source.calls if source is not None else 0
    bytes0 = source.bytes if source is not None else 0
    first_row = date.fromisoformat(plan_.first_row_session)
    last_row = date.fromisoformat(plan_.last_row_session)
    cal = _calendar_span(first_row, last_row, plan_.decision_sessions)

    # -- fetch
    t_fetch = time.monotonic()
    fetch_errors: Dict[str, str] = {}
    excluded: Dict[str, List[Dict[str, Any]]] = {}
    if blocking and fetch_missing:
        fetch_errors = _fetch_phase(plan_, source, concurrency, counters, log)
        for inv in plan_.symbols:
            if inv.refetch_required:
                snap, _ = _read_snapshot(plan_.cache_root, inv.symbol)
                events = [CalendarSplit(date.fromisoformat(d), float(r)) for d, r in inv.split_events]
                checks = _split_checks_for(snap, events, inv.symbol)
                inv.split_checks = [c.to_dict() for c in checks]
                inv.refetch_required = any(c.verdict in REFETCH_VERDICTS for c in checks)
                if inv.refetch_required:
                    excluded[inv.symbol] = [{"kind": "split_basis_unverified", "checks": inv.split_checks}]
    for inv in plan_.symbols:
        if inv.split_calendar_error:
            excluded[inv.symbol] = [{"kind": "split_calendar_unavailable", "error": inv.split_calendar_error}]
    fetch_elapsed = time.monotonic() - t_fetch

    # -- build
    t_build = time.monotonic()
    store = MarketConditionStore(plan_.cache_root)
    index = _ManifestIndex(store, plan_.profile, plan_.universe)
    results: Dict[str, _SymbolResult] = {}
    errors: List[str] = []
    waited: List[str] = []
    claims_dir = local_build_dir(plan_.cache_root) / "claims" / plan_.profile

    def one(inv: SymbolInventory) -> None:
        sym = inv.symbol
        extra = [{"kind": "fetch_failed", "error": fetch_errors[sym]}] if sym in fetch_errors else []
        if sym in excluded:
            counters.add("symbols_excluded")
            results[sym] = _SymbolResult(sym, [], {}, {"first_session": None, "last_session": None, "rows": 0,
                                                       "exceptions": excluded[sym] + extra})
            log(f"build {sym}: EXCLUDED {excluded[sym]}")
            return
        claim = _FileClaim(claims_dir / f"{sym}.lock")
        was_waiting = claim.acquire(on_wait=lambda: log(f"build {sym}: waiting for another builder"))
        try:
            with _HostSlot(plan_.cache_root):
                res = _build_symbol(store, index, plan_, inv, cal, counters, extra, log, claim=claim)
            if claim.lost:
                # Another builder broke our claim as stale and owns this symbol now; whatever we
                # produced is not ours to publish. Re-take the claim and rebuild from what the new
                # owner published (which is what waiting for it would have done).
                log(f"build {sym}: claim lost mid-build; rebuilding behind the new owner")
                raise ClaimLostError(sym)
            res.waited = was_waiting
            results[sym] = res
            if was_waiting:
                waited.append(sym)
            log(f"build {sym}: {len(res.objects)} object(s), {res.coverage['rows']} row(s)")
        finally:
            claim.release()

    def guarded(inv: SymbolInventory) -> None:
        try:
            try:
                one(inv)
            except ClaimLostError:
                # One retry: the takeover means somebody else built this symbol, so the retry is
                # a reuse pass, and it counts as having waited.
                waited.append(inv.symbol)
                one(inv)
        except Exception as e:  # noqa: BLE001 -- the symbol fails the build, loudly
            errors.append(f"{inv.symbol}: {type(e).__name__}: {e}")
            log(f"build {inv.symbol}: FAILED {type(e).__name__}: {e}")

    workers = max(1, min(int(concurrency), max_host_builders()))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(guarded, plan_.symbols))
    build_elapsed = time.monotonic() - t_build

    report.counters = dict(counters.values)
    if source is not None:
        report.counters["provider_calls"] = int(source.calls - calls0)
        report.counters["provider_bytes"] = int(source.bytes - bytes0)
    report.waited_symbols = sorted(set(waited))
    report.exceptions = {s: r.coverage["exceptions"] for s, r in sorted(results.items()) if r.coverage["exceptions"]}
    # A symbol that produced NO rows is an exclusion: publishing a manifest that silently leaves it
    # out is exactly the "generic success for partial work" design section 4.4 forbids.
    report.excluded = {s: r.coverage["exceptions"] for s, r in sorted(results.items()) if not r.coverage["rows"]}
    if report.excluded and not allow_exclusions:
        report.inventory = [{"symbol": s, "kind": exc[0]["kind"] if exc else "no_rows", "detail": exc}
                            for s, exc in report.excluded.items()]
        report.errors = errors + [f"{item['symbol']}: {item['kind']} -- no rows could be built"
                                  for item in report.inventory]
        report.elapsed_s = {"fetch": round(fetch_elapsed, 4), "build": round(build_elapsed, 4),
                            "total": round(time.monotonic() - t_total, 4)}
        log(f"build refused: {sorted(report.excluded)} produced no rows and exclusions were not "
            f"authorised (pass allow_exclusions)")
        return report
    if errors:
        report.errors = errors
        report.elapsed_s = {"fetch": round(fetch_elapsed, 4), "build": round(build_elapsed, 4),
                            "total": round(time.monotonic() - t_total, 4)}
        return report

    # -- publish (manifest last)
    t_pub = time.monotonic()
    objects = [o for r in results.values() for o in r.objects]
    raw: Dict[str, RawEntry] = {}
    for r in results.values():
        raw.update(r.raw_objects)
    manifest = store.make_manifest(
        PROFILES[plan_.profile], source_profile=plan_.source_profile, timing_policy=plan_.timing_policy,
        objects=objects, raw_objects=raw.values(), coverage={s: r.coverage for s, r in results.items()},
        universe=plan_.universe, sessions=[c.astype(object) for c in cal[WINDOW - 1:]],
        window_start=date.fromisoformat(plan_.start), window_end=date.fromisoformat(plan_.end))
    digest = store.write_manifest(manifest)
    for r in results.values():
        d = _progress_dir(plan_.cache_root, plan_.profile, r.symbol)
        for o in r.objects:
            try:
                (d / f"{o.sha256}.json").unlink()
            except OSError:
                pass
    report.manifest_digest = digest
    report.ok, report.exit_code = True, 0
    report.elapsed_s = {"plan": plan_.elapsed_s, "fetch": round(fetch_elapsed, 4), "build": round(build_elapsed, 4),
                        "publish": round(time.monotonic() - t_pub, 4), "total": round(time.monotonic() - t_total, 4)}
    log(f"published manifest {digest}: {report.counters}")
    return report


def verify(manifest_digest: str, cache_root: Optional[os.PathLike] = None):
    """The store's verify report for a published manifest (re-hashes every object)."""
    if cache_root is None:
        from ba2_common import config
        cache_root = config.CACHE_FOLDER
    store = MarketConditionStore(cache_root)
    bare = manifest_digest.split(":", 1)[1] if manifest_digest.startswith("sha256:") else manifest_digest
    return store.verify(store.read_manifest(bare), bare)
