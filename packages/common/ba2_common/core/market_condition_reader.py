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
"""
from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field as dc_field
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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
    "prepared_digests",
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
    manifest; the arrays are mapped on the first ``observe``, so a run whose gates are all off
    costs one JSON read.
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
        reason_codes = np.empty((total, n_f), dtype=np.int16)
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
            # The registry can move under a long-lived reader (a test registering a profile, a
            # reload): one dict lookup and one string compare per read.
            if PROFILES[self.profile].calc_version != self.calc_version:
                self._check_registry()
            return hit
        pos = self._row_position(symbol, session)
        row = None if pos is None else self._row_at(pos)
        if row is not None:
            self.served += 1
        with self._lock:
            self._memo[key] = row
            self._memo.move_to_end(key)
            while len(self._memo) > self._memo_size:
                self._memo.popitem(last=False)
        return row

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
            while len(self._rows_frames) > 8:
                self._rows_frames.popitem(last=False)
        return frame

    def window_for(self, symbol: str, session: date):
        """The exact ``(o, h, l, c, v)`` window the row was computed from, or ``None``.

        ``None`` for an absent row AND for a row whose window could NOT be assembled: such a row
        carries an ``unavailable_window_digest`` over the bars that happened to exist, which is
        evidence of absence rather than a window (see ``MarketConditionStore.retained_window``).
        A retained row's span is exactly ``WINDOW`` rows of a named raw shard list; anything else
        is the unavailable case. The bytes are re-hashed by ``retained_window``, so a raw shard
        that no longer matches raises rather than serving a different window.
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
        frame = self._frame(symbol)
        want = _days(session)
        days = frame.sessions.astype("datetime64[D]").astype("int64")
        at = int(np.searchsorted(days, want, side="left"))
        if at >= len(days) or int(days[at]) != want:
            return None
        if not frame.raw_shard_refs[at] or int(frame.raw_row_hi[at]) - int(frame.raw_row_lo[at]) != WINDOW:
            return None
        return frame.window_digests[at]

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
                "cache_root": self.cache_root, "ok": self.ok, "verified": self.verified,
                "objects_checked": self.objects_checked, "raw_checked": self.raw_checked,
                "symbols": self.symbols, "rows": self.rows,
                "built": self.built, "opened": (not self.built) and self.ok,
                "elapsed_s": round(self.elapsed_s, 3), "errors": list(self.errors),
                "warnings": list(self.warnings)}


def prepared_marker_path(cache_root: os.PathLike, profile: str, manifest_digest: str) -> str:
    return os.path.join(_derived_root(cache_root), PREPARED_DIRNAME,
                        f"{profile}.{_bare(manifest_digest)}.json")


def prepared_digests(cache_root: os.PathLike) -> List[str]:
    """Manifest digests this host has verified and mapped (from the ``_derived`` markers).

    Host-local by construction: the markers live under ``_derived``, which ``cache_sync`` never
    transfers, so a worker can never inherit another machine's claim of readiness."""
    d = os.path.join(_derived_root(cache_root), PREPARED_DIRNAME)
    out: List[str] = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        digest = rec.get("manifest")
        if digest and digest not in out:
            out.append(str(digest))
    return out


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
