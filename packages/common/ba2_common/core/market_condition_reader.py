"""Host-shared MAPPED market-condition reader: the central store's rows as memory-mapped arrays.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` sections 4.3, 4.5 and
4.6; implementation plan Task 7.

WHAT THIS REPLACES. Task 5's readers (``market_condition_readers.WindowMarketConditionReader`` and
its BT/live subclasses) assemble a 128-session window and CALCULATE the row on a memo miss. With a
manifest pinned, the calculation is gone: every row was computed once by the warmup
(``ba2_providers.market_conditions.warmup``), published as immutable parquet objects, and is read
here through one host-wide mapped array set. The reader protocol, the ``FeatureRow`` it returns and
the ``MarketConditionVersionMismatch`` it raises are unchanged, so nothing downstream can tell
where the numbers came from -- only that they are never recomputed per trial.

TWO IDENTITIES, KEPT SEPARATE (design section 4.3).
  * The PORTABLE identity is the manifest digest: the sha256 of the canonical manifest JSON, with
    no machine path, mtime or job name in it. It is what a master pins into a trial config, what a
    worker verifies by re-hashing every object, and what the mapped-array key carries.
  * The LOCAL identity is ``DerivedArrayStore``'s filesystem signature over the source files. It is
    per host by construction (mtimes differ), which is exactly right for a per-host mapping, and
    is deliberately NOT used to decide whether two hosts hold the same data.
  The single source file the signature is taken over is the MANIFEST FILE. That is sufficient and
  not a shortcut: the manifest's name IS the hash of its content, every object it lists is
  content-addressed, and ``read_manifest`` re-checks the identity on every open -- so a manifest
  file that still hashes to its name pins exactly one set of object bytes. Hashing thousands of
  object files on every reader construction would buy nothing and cost a stat storm.

LAYOUT (``LAYOUT_VERSION``). Key ``mc_<profile>_<manifest digest[:16]>_v<LAYOUT_VERSION>`` under
``<cache_root>/_derived/market_conditions`` (``derived_root_for(<cache_root>/market_conditions)``
-- the derived tree is a sibling of the source tree, so ``cache_sync`` never carries a machine-
local mapping to a worker). FIVE arrays, hence FIVE file descriptors per manifest per process,
whatever the symbol count -- the symbol slices live INSIDE them:

  * ``session``      int32  (n,)    days since the epoch, ascending within each symbol's slice
  * ``values``       float64(n, F)  the profile's fields in registry order (a categorical field
                                    holds its integer code; NaN where the field is not valid)
  * ``status``       int8   (n, F)  index into ``market_conditions.STATUSES``
  * ``reason_codes`` int16  (n, F)  index into the manifest's small reason table
  * ``meta_json``    uint8  (m,)    UTF-8 JSON: layout/manifest/profile/calc version, the field
                                    list, the per-symbol ``[lo, hi)`` slice table and the reason
                                    table.

  The plan's sketch put the symbol and reason tables "in the marker JSON". ``DerivedArrayStore``
  writes its own done-marker (``{"arrays", "schema", "written_at", "pid"}``) and has no hook for
  caller metadata, and adding one would change shared-array semantics for the option/OHLCV readers
  this module deliberately does not touch. Carrying the same two tables as ONE uint8 array keeps
  them inside the atomically published, signature-checked directory, costs one descriptor instead
  of a separate sidecar file's lifecycle, and keeps the total at the five the plan budgets for.

WHAT A MISS MEANS. An absent (symbol, session) is ``None`` -- the same "no row" the condition
reports as ``missing_session``. It is never a silent zero and never a recomputation: a reader
holding a manifest NEVER falls back to calculating (design section 4.5: "It must not fetch, use
another snapshot or report a feature-cache miss as a zero-trade strategy result").

``BA2_SHARED_ARRAYS=0`` loads the same central rows into PRIVATE arrays (``build_or_open``'s escape
hatch returns ``build_fn()`` directly). Values are identical; nothing is recomputed.

THE LOCAL SIGNATURE IS A STAT, NOT A HASH (``DerivedArrayStore.signature``: path, size,
mtime_ns). A byte-identical re-copy of the manifest FILE -- a re-push, a restore, a
``cp`` -- therefore moves the signature and the next reader REBUILDS the mapping, while the
host's ``_prepared`` marker still claims the digest is ready. That is the safe direction (a
rebuild costs seconds and produces the same numbers; a stale mapping would not), but it means
"prepared" means "verified and mapped at some point", not "this exact mapping directory is
still the current one". ``prepare_host`` is idempotent and cheap on a warm host precisely so
re-running it is the answer whenever that matters.

MEASURED (2026-09-16, this host).
  * Fixture (100 symbols x 1000 sessions = 100k rows, 4700 objects): prepare-host cold
    verify+build 5.6 s, warm verify+open 2.7 s; mapped arrays 3.7 MB / 5 descriptors;
    ``observe()`` 12.8 us P50 / 15.1 us P95 on a memo miss, 0.60 us on a hit.
  * PRODUCTION warmup, this machine's real FMP cache, ohlcv-v1 over the option universe
    (2020-01-01..2025-12-31, 1508 decision sessions). 85 of the 98 symbols: the other 13
    (ASML BHP DELL GE HON IBM MRK NVS RTX SAN SCCO T WDC) carry a split whose basis the prices
    cannot settle and need a full provider re-fetch first -- the first-run refetch storm the
    runbook warns about, and the reason a cache-only build refuses them rather than warming an
    unproven basis.
      - plan: 55 s cold (98 split-calendar calls, 56 KB of provider traffic), 3.7 s warm
        (0 provider calls -- the calendars are cached).
      - build --cache-only: 143 s for 128,180 rows (49.6 s compute, 92.7 s publish), writing
        6205 feature objects + 6344 raw shards; bucket on disk 106 MB / 12,550 files.
      - verify (re-hash all 12,549 referenced files): 8 s.
      - prepare-host: 10.5 s cold (verify + build), 6.8 s warm (verify + open). Opening the
        prepared mapping in a fresh process: 0.05 s.
      - mapped arrays 4.88 MB for the whole universe, 5 descriptors, worker RSS 120 MB.
      - ``observe()``: 13.2 us P50 / 15.8 us P95 / 22.9 us P99 on a memo miss, 0.50 us / 0.60 us
        on a hit -- and every row equals the parquet reader's (checked over three symbols'
        full histories).
"""
from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field as dc_field
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from ba2_common.core.market_condition_readers import (
    MEMO_SIZE,
    MarketConditionVersionMismatch,
)
from ba2_common.core.market_condition_store import (
    MC_DIRNAME,
    ManifestError,
    MarketConditionStore,
)
from ba2_common.core.market_conditions import (
    PROFILES,
    STATUS_VALID,
    STATUSES,
    WINDOW,
    FeatureRow,
    Observation,
)
from ba2_common.core.shared_arrays import (
    DONE_MARKER,
    DerivedArrayStore,
    derived_root_for,
    ensure_fd_headroom,
)

__all__ = [
    "LAYOUT_VERSION",
    "SYMBOL_CACHE",
    "MappedMarketConditionReader",
    "PrepareReport",
    "mapped_key",
    "prepare_host",
    "prepare_host_job",
    "mapping_exists",
    "prepared_digests",
    "prepared_entries",
    "prune_prepared_markers",
    "prune_revoked_markers",
    "revoke_prepared",
]

#: Local mapped-array layout version. A change of arrays or meaning gets a new number, which moves
#: every key (old directories are then collected by ``build_shared_arrays.py --sweep``).
LAYOUT_VERSION = 1

#: Per-symbol session-slice views kept resident. A view costs no descriptor (the five mappings are
#: opened once); this bound only caps the little index dictionary.
SYMBOL_CACHE = 256

#: Where a host records the manifests it has verified and mapped. Under ``_derived`` -- host-local,
#: never synced, and discarded with the mappings it describes.
PREPARED_DIRNAME = "_prepared"
#: A revoked marker keeps its content under this suffix instead of vanishing: "this host WAS
#: ready for that digest and stopped being ready" is the diagnostic an operator needs after a
#: corrupt push, and an empty directory does not say it.
REVOKED_SUFFIX = ".revoked"
#: ...but not forever: the prewarm tool's sweep collects revoked markers older than this. The
#: diagnostic is worth keeping across the incident and the grid that follows it, not across a year.
REVOKED_MAX_AGE_DAYS = 30.0

#: Symbols whose full ``RowsFrame`` (window digests + raw-shard references) is kept for the
#: capture path. Live capture touches each symbol once per analysis and a backtest never comes
#: there at all, so this only has to stop an unbounded walk.
ROWS_FRAME_CACHE = 8

_ABSENT = object()


def _derived_root(cache_root: os.PathLike) -> str:
    """``<cache_root>/_derived/market_conditions`` (design section 4.3's local array store)."""
    return derived_root_for(os.path.join(os.fspath(cache_root), MC_DIRNAME))


def mapped_key(profile: str, manifest_digest: str) -> str:
    """The ``DerivedArrayStore`` key for one manifest's mapping on this host."""
    digest = manifest_digest.split(":", 1)[1] if manifest_digest.startswith("sha256:") else manifest_digest
    return f"mc_{profile}_{digest[:16]}_v{LAYOUT_VERSION}"


def _days(session: date) -> int:
    return int(np.datetime64(session, "D").astype("int64"))


@dataclass
class _SymbolIndex:
    lo: int
    hi: int
    sessions: np.ndarray       # int32 view into the mapped ``session`` array


class MappedMarketConditionReader:
    """Serve one manifest's feature rows from the host's mapped arrays.

    ``observe(symbol, session)`` is a bisect over that symbol's session slice plus a
    ``FeatureRow`` construction, memoised per (symbol, session) so the three leaves of one entry
    decision share one object. No parquet is read, no DataFrame is built and no indicator is
    calculated on this path.

    Thread-safe. Built lazily: constructing the reader reads (and re-checks the identity of) the
    manifest and nothing else; the arrays are mapped on the first ``observe``. So a run that is
    pinned to a manifest but never evaluates a market-condition leaf (every mode gene decoded to
    ``off``) pays one JSON read and no mapping at all. A run with profile ``none`` never
    constructs this reader in the first place.

    ``memo_size=0`` turns the row memo off, for the case where this reader is WRAPPED by a
    ``WindowMarketConditionReader`` that already memoises the same (symbol, session) key: two
    memos over one lookup would double the residency and cache nothing extra.
    """

    def __init__(self, cache_root: os.PathLike, manifest_digest: str, profile: str, *,
                 store: Optional[MarketConditionStore] = None, memo_size: int = MEMO_SIZE,
                 symbol_cache: int = SYMBOL_CACHE, jobs: int = 1):
        if profile not in PROFILES:
            raise KeyError(f"unknown market-condition profile {profile!r}; "
                           f"registered: {sorted(PROFILES)!r}")
        self.cache_root = os.fspath(cache_root)
        self.store = store if store is not None else MarketConditionStore(self.cache_root)
        self.profile = profile
        self.manifest = self.store.read_manifest(manifest_digest, profile)
        self.manifest_digest = manifest_digest
        self.calc_version = str(self.manifest["calc_version"])
        self.fields: Tuple[str, ...] = tuple(self.manifest["fields"])
        self._check_registry()
        self._jobs = max(1, int(jobs))
        self._memo_size = int(memo_size)
        self._symbol_cache = int(symbol_cache)
        self._memo: "OrderedDict[Tuple[str, date], Any]" = OrderedDict()
        self._index: "OrderedDict[str, Optional[_SymbolIndex]]" = OrderedDict()
        self._rows_frames: "OrderedDict[str, Any]" = OrderedDict()
        self._arrays: Optional[Dict[str, np.ndarray]] = None
        self._meta: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._map_lock = threading.Lock()
        #: Rows served from the mapping (visibility for tests/benchmarks). A mapped reader never
        #: COMPUTES, so the computed counter its window-reading sibling keeps has no meaning here.
        self.served = 0

    # -- identity ---------------------------------------------------------------------------
    def _check_registry(self) -> None:
        """The manifest's calculator version and field list must be the ones THIS process's
        registry defines for the profile. A threshold tuned against one calculator compared to
        another's value is silent corruption -- so this raises, it never adapts."""
        spec = PROFILES[self.profile]
        if self.manifest["profile"] != self.profile:
            raise ManifestError(f"manifest {self.manifest_digest} carries profile "
                                f"{self.manifest['profile']!r}, not {self.profile!r}")
        if spec.calc_version != self.calc_version:
            raise MarketConditionVersionMismatch(
                f"manifest {self.manifest_digest} was built at calc version {self.calc_version!r}; "
                f"profile {self.profile!r} is registered at {spec.calc_version!r}")
        wanted = tuple(f.name for f in spec.fields)
        if self.fields != wanted:
            raise MarketConditionVersionMismatch(
                f"manifest {self.manifest_digest} carries fields {list(self.fields)!r}; "
                f"profile {self.profile!r} declares {list(wanted)!r}")

    # -- the mapping ------------------------------------------------------------------------
    def symbols(self) -> Tuple[str, ...]:
        """Every symbol the manifest carries rows for, sorted (read from the manifest, not the
        object directory -- a reader must never glob ``objects/``)."""
        return tuple(sorted({str(o["symbol"]) for o in self.manifest["objects"]}))

    def coverage(self) -> Mapping[str, Any]:
        """The manifest's per-symbol coverage record: ``{symbol: {rows, first_session,
        last_session, exceptions[]}}``.

        A symbol the warmup could not warm at all is simply ABSENT from it (and from
        ``symbols()``); one that was warmed with holes is present with its exceptions recorded.
        Both matter to a caller checking that a run's universe is served by this snapshot, and
        neither is visible from the rows alone."""
        return self.manifest.get("coverage") or {}

    def arrays(self) -> Dict[str, np.ndarray]:
        """The mapped (or, under ``BA2_SHARED_ARRAYS=0``, private) arrays; built once per host."""
        arrays = self._arrays
        if arrays is not None:
            return arrays
        with self._map_lock:
            if self._arrays is None:
                ensure_fd_headroom()
                das = DerivedArrayStore(_derived_root(self.cache_root))
                manifest_path = self.store.manifest_path(self.profile, _bare(self.manifest_digest))
                arrays = das.build_or_open(mapped_key(self.profile, self.manifest_digest),
                                           [manifest_path], self._build_arrays)
                meta = json.loads(bytes(arrays["meta_json"]).decode("utf-8"))
                if meta["manifest"] != _bare(self.manifest_digest) or meta["layout"] != LAYOUT_VERSION:
                    raise ManifestError(
                        f"mapped arrays under {das.root} carry manifest {meta['manifest']!r} "
                        f"layout {meta['layout']!r}, not {_bare(self.manifest_digest)!r} "
                        f"layout {LAYOUT_VERSION}")
                if tuple(meta["fields"]) != self.fields or meta["calc_version"] != self.calc_version:
                    raise MarketConditionVersionMismatch(
                        f"mapped arrays carry fields {meta['fields']!r} at calc version "
                        f"{meta['calc_version']!r}; manifest says {list(self.fields)!r} at "
                        f"{self.calc_version!r}")
                self._meta = meta
                self._arrays = arrays
        return self._arrays

    def open_descriptors(self) -> int:
        """Descriptors this reader's mapping costs: one per mapped ``.npy``, 0 before first use.

        Exactly the number the plan budgets (<= 5 per manifest per process) -- the per-symbol
        slices live inside the arrays rather than in a file each."""
        return 0 if self._arrays is None else len(self._arrays)

    def _build_arrays(self) -> Dict[str, np.ndarray]:
        """Read the manifest's objects ONCE (bounded concurrency) into the packed arrays.

        Runs at most once per (key, signature) per host: ``DerivedArrayStore`` takes an O_EXCL
        claim, so a second process -- another worker, or a second prepare-host -- waits for this
        one to publish instead of repeating the read.
        """
        symbols = self.symbols()
        frames = self._read_frames(symbols)
        n_f = len(self.fields)
        total = sum(len(f) for f in frames)
        session = np.empty(total, dtype=np.int32)
        values = np.empty((total, n_f), dtype=np.float64)
        status = np.empty((total, n_f), dtype=np.int8)
        # zeros, not empty: a symbol with no rows leaves its slice untouched, and uninitialised
        # int16 would index the reason table out of range if anything ever read it.
        reason_codes = np.zeros((total, n_f), dtype=np.int16)
        reason_table: Dict[str, int] = {}
        slices: Dict[str, List[int]] = {}
        pos = 0
        for symbol, frame in zip(symbols, frames):
            n = len(frame)
            slices[symbol] = [pos, pos + n]
            if not n:
                continue
            days = frame.sessions.astype("datetime64[D]").astype("int64")
            if np.any(np.diff(days) <= 0):
                raise ManifestError(f"manifest {self.manifest_digest}: {symbol} has duplicate or "
                                    f"unsorted sessions in its objects")
            session[pos:pos + n] = days.astype(np.int32)
            values[pos:pos + n] = frame.values
            status[pos:pos + n] = frame.status
            for i, reasons in enumerate(frame.reasons):
                for j, reason in enumerate(reasons):
                    code = reason_table.get(reason)
                    if code is None:
                        code = reason_table[reason] = len(reason_table)
                        if code > 32767:
                            raise ManifestError(
                                f"manifest {self.manifest_digest} carries more than 32768 distinct "
                                f"reason strings; the packed reason code is int16")
                    reason_codes[pos + i, j] = code
            pos += n
        meta = {
            "layout": LAYOUT_VERSION,
            "manifest": _bare(self.manifest_digest),
            "profile": self.profile,
            "calc_version": self.calc_version,
            "fields": list(self.fields),
            "rows": int(total),
            "symbols": slices,
            "reasons": [r for r, _c in sorted(reason_table.items(), key=lambda kv: kv[1])],
        }
        meta_json = np.frombuffer(json.dumps(meta, sort_keys=True).encode("utf-8"),
                                  dtype=np.uint8).copy()
        return {"session": session, "values": values, "status": status,
                "reason_codes": reason_codes, "meta_json": meta_json}

    def _read_frames(self, symbols: Sequence[str]) -> List[Any]:
        """One ``RowsFrame`` per symbol, in ``symbols`` order, with bounded concurrency.

        pyarrow releases the GIL while it reads, so threads (not processes) are what this wants:
        the arrays being filled are one shared result, and a process pool would pay to pickle
        every frame back."""
        if self._jobs <= 1 or len(symbols) <= 1:
            return [self.store.rows_frame(self.manifest, s) for s in symbols]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(self._jobs, len(symbols)),
                                thread_name_prefix="mc-map") as pool:
            return list(pool.map(lambda s: self.store.rows_frame(self.manifest, s), symbols))

    # -- lookup -----------------------------------------------------------------------------
    def _symbol_index(self, symbol: str) -> Optional[_SymbolIndex]:
        with self._lock:
            hit = self._index.get(symbol, _ABSENT)
            if hit is not _ABSENT:
                self._index.move_to_end(symbol)
                return hit
        arrays = self.arrays()
        bounds = (self._meta or {})["symbols"].get(symbol)
        idx = None
        if bounds is not None:
            lo, hi = int(bounds[0]), int(bounds[1])
            if hi > lo:
                idx = _SymbolIndex(lo=lo, hi=hi, sessions=arrays["session"][lo:hi])
        with self._lock:
            self._index[symbol] = idx
            self._index.move_to_end(symbol)
            while len(self._index) > self._symbol_cache:
                self._index.popitem(last=False)
        return idx

    def _row_position(self, symbol: str, session: date) -> Optional[int]:
        idx = self._symbol_index(symbol)
        if idx is None:
            return None
        want = _days(session)
        at = int(np.searchsorted(idx.sessions, want, side="left"))
        if at >= len(idx.sessions) or int(idx.sessions[at]) != want:
            return None
        return idx.lo + at

    def _row_at(self, pos: int) -> FeatureRow:
        arrays = self.arrays()
        reasons = (self._meta or {})["reasons"]
        values = arrays["values"][pos]
        status = arrays["status"][pos]
        codes = arrays["reason_codes"][pos]
        obs: Dict[str, Observation] = {}
        for j, field in enumerate(self.fields):
            st = STATUSES[int(status[j])]
            value = float(values[j]) if st == STATUS_VALID else None
            obs[field] = Observation(value, st, reasons[int(codes[j])])
        return FeatureRow(values=obs,
                          calc_versions={f: self.calc_version for f in self.fields})

    def observe(self, symbol: str, session: date) -> Optional[FeatureRow]:
        """The manifest's row for ``(symbol, session)``, or ``None`` when it carries none.

        Memoised: the same ``FeatureRow`` OBJECT comes back for a repeat call (the three leaves of
        one entry decision read one row), bounded by ``memo_size``."""
        key = (symbol, session)
        with self._lock:
            hit = self._memo.get(key, _ABSENT)
            if hit is not _ABSENT:
                self._memo.move_to_end(key)
        if hit is not _ABSENT:
            self._recheck_registry()
            return hit
        self._recheck_registry()
        pos = self._row_position(symbol, session)
        row = None if pos is None else self._row_at(pos)
        if row is not None:
            self.served += 1
        if self._memo_size > 0:
            with self._lock:
                self._memo[key] = row
                self._memo.move_to_end(key)
                while len(self._memo) > self._memo_size:
                    self._memo.popitem(last=False)
        return row

    def _recheck_registry(self) -> None:
        """One dict lookup and one string compare per read; the full check only when it moved.

        Deliberately on BOTH paths. The registry can change under a long-lived reader (a test
        registering a profile, a settings reload), and a check that fires only on a memo miss
        would serve a memoised row computed under the old calculator without a word -- the exact
        silent-corruption case this reader raises for.
        """
        if PROFILES[self.profile].calc_version != self.calc_version:
            self._check_registry()

    # -- retained evidence (capture) ---------------------------------------------------------
    def _frame(self, symbol: str):
        """The symbol's ``RowsFrame``, cached for a handful of symbols.

        Only the CAPTURE path needs it (the window digest and raw-shard references are not packed
        into the mapping -- they are ~100 bytes per row of pure I/O metadata that the decision path
        never reads). Live capture touches each symbol once per analysis, so a small cache is
        enough and a backtest never comes here at all."""
        with self._lock:
            hit = self._rows_frames.get(symbol, _ABSENT)
            if hit is not _ABSENT:
                self._rows_frames.move_to_end(symbol)
                return hit
        frame = self.store.rows_frame(self.manifest, symbol)
        with self._lock:
            self._rows_frames[symbol] = frame
            self._rows_frames.move_to_end(symbol)
            while len(self._rows_frames) > ROWS_FRAME_CACHE:
                self._rows_frames.popitem(last=False)
        return frame

    def _retained_at(self, symbol: str, session: date):
        """``(frame, index)`` of a row whose window IS retained, else ``None``.

        A row's window is retained only when its span is exactly ``WINDOW`` rows of a named raw
        shard list. Anything else carries an ``unavailable_window_digest`` over the bars that
        happened to exist, which is evidence of absence rather than a window (see
        ``MarketConditionStore.retained_window``) and is never served as one.
        """
        frame = self._frame(symbol)
        want = _days(session)
        days = frame.sessions.astype("datetime64[D]").astype("int64")
        at = int(np.searchsorted(days, want, side="left"))
        if at >= len(days) or int(days[at]) != want:
            return None
        lo, hi = int(frame.raw_row_lo[at]), int(frame.raw_row_hi[at])
        if not frame.raw_shard_refs[at] or hi - lo != WINDOW:
            return None
        return frame, at

    def window_for(self, symbol: str, session: date):
        """The exact ``(o, h, l, c, v)`` window the row was computed from, or ``None``.

        ``None`` for an absent row and for a row with no retained window (see ``_retained_at``).
        The bytes are re-hashed by ``retained_window``, so a raw shard that no longer matches
        raises rather than serving a different window.
        """
        hit = self._retained_at(symbol, session)
        if hit is None:
            return None
        frame, at = hit
        return self.store.retained_window(frame.window_digests[at], self.manifest)

    def window_result_for(self, symbol: str, session: date):
        """``window_for`` as a ``WindowResult`` (arrays + the window's session dates), for the
        capture recorder. ``None`` when no window is retained for the row."""
        arrays = self.window_for(symbol, session)
        if arrays is None:
            return None
        from ba2_common.core.market_calendar import regular_sessions_ending_at
        from ba2_common.core.market_condition_source import WindowResult

        o, h, l, c, v = arrays
        return WindowResult(status=STATUS_VALID, reason="",
                            dates=tuple(regular_sessions_ending_at(session, WINDOW)),
                            o=o, h=h, l=l, c=c, v=v)

    def window_digest_for(self, symbol: str, session: date) -> Optional[str]:
        """The stored window digest of a row whose window is retained, else ``None``."""
        hit = self._retained_at(symbol, session)
        return None if hit is None else hit[0].window_digests[hit[1]]

    def memo_len(self) -> int:
        with self._lock:
            return len(self._memo)


def _bare(digest: str) -> str:
    return digest.split(":", 1)[1] if digest.startswith("sha256:") else digest


# ---------------------------------------------------------------------------
# prepare-host
# ---------------------------------------------------------------------------
@dataclass
class PrepareReport:
    """What one host's preparation of one manifest did."""

    manifest_digest: str
    profile: str
    cache_root: str
    verified: bool = False
    objects_checked: int = 0
    raw_checked: int = 0
    symbols: int = 0
    rows: int = 0
    built: bool = False
    elapsed_s: float = 0.0
    errors: List[str] = dc_field(default_factory=list)
    #: Non-fatal notes (e.g. the host-local readiness marker could not be written): the mapping
    #: IS usable, so these must not turn a good preparation into a refused worker.
    warnings: List[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verified and not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {"manifest": self.manifest_digest, "profile": self.profile,
                "key": mapped_key(self.profile, self.manifest_digest),
                "cache_root": self.cache_root, "ok": self.ok, "verified": self.verified,
                "objects_checked": self.objects_checked, "raw_checked": self.raw_checked,
                "symbols": self.symbols, "rows": self.rows,
                "built": self.built, "opened": (not self.built) and self.ok,
                "elapsed_s": round(self.elapsed_s, 3), "errors": list(self.errors),
                "warnings": list(self.warnings)}


def prepared_marker_path(cache_root: os.PathLike, profile: str, manifest_digest: str) -> str:
    return os.path.join(_derived_root(cache_root), PREPARED_DIRNAME,
                        f"{profile}.{_bare(manifest_digest)}.json")


def _prepared_records(cache_root: os.PathLike) -> List[Tuple[str, Dict[str, Any]]]:
    """``[(marker path, record)]`` for every live (non-revoked) readiness marker."""
    d = os.path.join(_derived_root(cache_root), PREPARED_DIRNAME)
    out: List[Tuple[str, Dict[str, Any]]] = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("manifest"):
            out.append((path, rec))
    return out


def _mapping_present(cache_root: os.PathLike, rec: Mapping[str, Any]) -> bool:
    """Does the mapping this marker describes still exist on disk?

    A marker outlives its arrays in two ordinary ways -- ``build_shared_arrays.py --sweep``
    collects an unused key, an operator clears ``_derived`` -- and a host that keeps claiming
    readiness for a mapping that is gone would accept trials and then rebuild under them, one
    worker at a time, which is the cold-start stampede the prewarm exists to prevent."""
    key = rec.get("key")
    if not key:
        return False
    key_dir = os.path.join(_derived_root(cache_root), str(key))
    if not os.path.isdir(key_dir):
        return False
    # A key directory is not a mapping. An eviction that could not finish, an interrupted build
    # and a swept key all leave the directory (or a signature dir inside it) behind; only a
    # ``_done.json`` means a set ``_try_open`` will actually serve. Checking for one is the same
    # question ``DerivedArrayStore`` asks itself.
    try:
        for sig in os.listdir(key_dir):
            if os.path.isfile(os.path.join(key_dir, sig, DONE_MARKER)):
                return True
    except OSError:
        return False
    return False


def mapping_exists(cache_root: os.PathLike, profile: str, manifest_digest: str) -> bool:
    """Is this host still holding the mapped arrays for (profile, digest)?

    The answer can go from True to False without anyone asking this process: the prewarm tool's
    ``--sweep`` collects an unused key, an operator clears ``_derived``. A host that keeps
    claiming readiness afterwards accepts trials and then rebuilds the mapping under them, one
    worker at a time -- the cold-start stampede the prewarm exists to prevent."""
    return os.path.isdir(os.path.join(_derived_root(cache_root),
                                      mapped_key(profile, manifest_digest)))


def prepared_entries(cache_root: os.PathLike) -> List[Dict[str, Any]]:
    """Live readiness records on this host (marker present AND mapping still there)."""
    out: List[Dict[str, Any]] = []
    for _path, rec in _prepared_records(cache_root):
        if _mapping_present(cache_root, rec):
            out.append(dict(rec))
    return out


def prepared_digests(cache_root: os.PathLike) -> List[str]:
    """Manifest digests this host has verified and mapped AND still holds the mapping for.

    Kept public for tools and diagnostics that only want the digests. The worker uses
    ``prepared_entries`` instead: it needs each record's ``profile``/``key`` to re-check the
    mapping later, which a bare digest cannot answer.

    Host-local by construction: the markers live under ``_derived``, which ``cache_sync`` never
    transfers, so a worker can never inherit another machine's claim of readiness."""
    out: List[str] = []
    for _path, rec in _prepared_records(cache_root):
        digest = str(rec["manifest"])
        if digest not in out and _mapping_present(cache_root, rec):
            out.append(digest)
    return out


def revoke_prepared(cache_root: os.PathLike, digests: Optional[Iterable[str]] = None) -> List[str]:
    """Withdraw this host's readiness claim for ``digests`` (all of them when None).

    The marker is RENAMED to ``<name>.revoked`` rather than deleted: the fact that this host was
    ready and stopped being ready is what an operator needs after a corrupt push, and a missing
    file does not say it. Returns the digests actually revoked.

    Clearing an in-memory set is NOT enough on its own -- the marker is exactly what a restarted
    (or not-yet-loaded) process reads to re-admit a digest, so a revoke that leaves it behind
    re-admits the very snapshot it just rejected."""
    wanted = None if digests is None else {_bare(str(d)) for d in digests}
    revoked: List[str] = []
    for path, rec in _prepared_records(cache_root):
        digest = _bare(str(rec["manifest"]))
        if wanted is not None and digest not in wanted:
            continue
        try:
            os.replace(path, path + REVOKED_SUFFIX)
        except OSError:
            try:
                os.unlink(path)
            except OSError:
                continue
        revoked.append(digest)
    return revoked


def prune_revoked_markers(cache_root: os.PathLike,
                          max_age_days: float = REVOKED_MAX_AGE_DAYS) -> List[str]:
    """Delete ``.revoked`` markers older than ``max_age_days``. Returns the file names removed.

    Age-based rather than immediate: the point of keeping a revoked marker is that somebody can
    still see, days later, that this host rejected a snapshot and when."""
    import time as _time

    removed: List[str] = []
    d = os.path.join(_derived_root(cache_root), PREPARED_DIRNAME)
    if not os.path.isdir(d):
        return removed
    cutoff = _time.time() - max_age_days * 86400
    for name in sorted(os.listdir(d)):
        if not name.endswith(REVOKED_SUFFIX):
            continue
        path = os.path.join(d, name)
        try:
            if os.stat(path).st_mtime > cutoff:
                continue
            os.unlink(path)
        except OSError:
            continue
        removed.append(name)
    return removed


def prune_prepared_markers(cache_root: os.PathLike) -> List[str]:
    """Delete readiness markers whose mapping is gone (swept, evicted, manually cleared).

    Called by the prewarm/GC tool after a sweep. Returns the marker file names removed."""
    removed: List[str] = []
    for path, rec in _prepared_records(cache_root):
        if _mapping_present(cache_root, rec):
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        removed.append(os.path.basename(path))
    return removed


def prepare_host(cache_root: os.PathLike, manifest_digest: str, profile: Optional[str] = None,
                 *, jobs: int = 4, log: Optional[Callable[[str], None]] = None) -> PrepareReport:
    """VERIFY a manifest on this host, then build its mapped arrays. The one preparation routine.

    Order is the contract, not a detail: ``store.verify`` re-hashes every feature object AND every
    raw shard first (a same-size corruption is exactly what ``(rel_path, size)`` sync cannot see),
    and the mapping is built ONLY from a manifest that passed. Building first would bake corrupt
    numbers into an immutable, atomically published array set that every worker on the host then
    maps.

    Returns a report; ``report.ok`` is False for a failed verification (the CLI exits 1 and the
    worker stays unready). Never raises for a data problem -- raises only for a manifest that is
    not there at all, which is a configuration error.
    """
    import time as _time

    t0 = _time.monotonic()
    say = log or (lambda _m: None)
    root = os.fspath(cache_root)
    store = MarketConditionStore(root)
    manifest = store.read_manifest(manifest_digest, profile)
    profile = profile or str(manifest["profile"])
    report = PrepareReport(manifest_digest=_bare(manifest_digest), profile=profile, cache_root=root)
    say(f"verifying manifest {_bare(manifest_digest)} ({len(manifest['objects'])} object(s), "
        f"{len(manifest['raw_objects'])} raw shard(s)) under {root}")
    verdict = store.verify(manifest, _bare(manifest_digest))
    report.objects_checked = verdict.objects_checked
    report.raw_checked = verdict.raw_checked
    report.verified = verdict.ok
    if not verdict.ok:
        for rel in verdict.missing:
            report.errors.append(f"missing object {rel}")
        for rel in verdict.corrupt:
            report.errors.append(f"corrupt object {rel} (hash does not match the manifest)")
        report.errors.extend(verdict.errors)
        report.elapsed_s = _time.monotonic() - t0
        say(f"verification FAILED: {report.errors}")
        return report

    reader = MappedMarketConditionReader(root, _bare(manifest_digest), profile, store=store, jobs=jobs)
    das = DerivedArrayStore(_derived_root(root))
    key = mapped_key(profile, manifest_digest)
    manifest_path = store.manifest_path(profile, _bare(manifest_digest))
    existed = (das.current_dir(key, [manifest_path]) / "_done.json").exists()
    try:
        arrays = reader.arrays()
    except Exception as e:  # noqa: BLE001 -- a build failure is reported, never a half-prepared host
        report.errors.append(f"mapping build failed: {e!r}")
        report.elapsed_s = _time.monotonic() - t0
        say(f"mapping FAILED: {e!r}")
        return report
    report.built = not existed
    report.symbols = len(reader.symbols())
    report.rows = int(len(arrays["session"]))
    report.elapsed_s = _time.monotonic() - t0
    _record_prepared(root, profile, manifest_digest, report)
    say(f"{'built' if report.built else 'opened'} mapped arrays for {report.symbols} symbol(s) / "
        f"{report.rows} row(s) in {report.elapsed_s:.1f}s")
    return report


def prepare_host_job(cache_root: os.PathLike, manifest_digest: str, profile: Optional[str] = None,
                     jobs: int = 4, ctl: Any = None) -> Dict[str, Any]:
    """``prepare_host`` as a picklable unit of work for a worker's trial pool, returning a dict.

    Top-level (not a closure or a method) because a ``ProcessPoolExecutor`` pickles the callable
    BY REFERENCE. ``ctl`` is the worker's optional per-job control block: the stage is written
    into it so ``/job-status`` can say what a slow preparation is doing instead of a bare
    "running" -- the same ambiguity the trial heartbeat exists to remove.
    """
    def _stage(msg: str) -> None:
        if ctl is not None:
            try:
                ctl["stage"] = msg[:200]
            except Exception:  # noqa: BLE001 -- a dead manager must never fail the preparation
                pass

    try:
        report = prepare_host(cache_root, manifest_digest, profile, jobs=jobs, log=_stage)
    except FileNotFoundError as e:
        return {"ok": False, "manifest": _bare(str(manifest_digest)), "errors": [str(e)]}
    except Exception as e:  # noqa: BLE001 -- an unready host is an answer, not a crashed job
        return {"ok": False, "manifest": _bare(str(manifest_digest)), "errors": [repr(e)]}
    return report.to_dict()


def _record_prepared(cache_root: str, profile: str, manifest_digest: str,
                     report: PrepareReport) -> None:
    """Write the host-local "this digest is verified and mapped" marker (best effort).

    A marker that cannot be written is NOT a failed preparation -- the mapping is published and
    usable -- but it costs the worker its in-memory-independent memory of readiness across a
    restart, so the failure is surfaced in the report rather than swallowed."""
    path = prepared_marker_path(cache_root, profile, manifest_digest)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"manifest": _bare(manifest_digest), "profile": profile,
                       "key": mapped_key(profile, manifest_digest),
                       "symbols": report.symbols, "rows": report.rows,
                       "objects_checked": report.objects_checked,
                       "raw_checked": report.raw_checked}, f)
        os.replace(tmp, path)
    except OSError as e:
        report.warnings.append(f"prepared marker not written: {e}")
