"""Window-computing ``MarketConditionReader`` implementations, capture and replay.

Design: ``docs/plans/2026-09-15-option-market-condition-genes-design.md`` sections 4.1 and 4.2.

* :class:`WindowMarketConditionReader` -- the shared core: assemble ONE window per
  (symbol, session) through ``assemble_window``, compute every field of the profile with the
  profile's registered calculator (``COMPUTE_BY_PROFILE``), refuse a calculator-version mismatch
  (raise, never skip), and memoise the resulting ``FeatureRow`` in a bounded LRU so the three
  leaves of one entry read one computation. Subclasses only say where bars come from.
* :class:`FMPCacheMarketConditionReader` -- the live source: the FMP daily OHLCV parquet cache
  ONLY, read directly (never a provider call, so never a network fetch inside a condition).
* :class:`CapturingMarketConditionReader` -- wraps a window reader while a replay capture
  context is active: every served row is recorded ONCE per (symbol, session, window digest),
  with the normalized 128x5 float64 window retained as a content-addressed frame (a hash without
  retained bytes is not replayable).
* :class:`ReplayMarketConditionReader` -- serves exactly what was recorded, verifies the window
  digest, and raises ``ReplayMiss`` for anything not on the tape; it never touches a cache.

Task 7 replaces the compute path of the backtest/live readers with the mapped feature store; the
reader protocol, the memo contract and the capture payload stay.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Hashable, Iterable, Mapping, Optional, Tuple

import numpy as np

from ba2_common.core.market_condition_source import (
    FMP_OHLCV_PROVIDER_DIR,
    WindowResult,
    assemble_window,
    window_digest,
)
from ba2_common.core.market_conditions import (
    COMPUTE_BY_PROFILE,
    PROFILES,
    STATUS_VALID,
    WINDOW,
    FeatureRow,
    Observation,
)

__all__ = [
    "MEMO_SIZE",
    "MarketConditionVersionMismatch",
    "ObservedWindow",
    "WindowMarketConditionReader",
    "FMPCacheMarketConditionReader",
    "CapturingMarketConditionReader",
    "ReplayMarketConditionReader",
    "CAPTURE_PROVIDER",
    "CAPTURE_METHOD",
    "CAPTURE_SCHEMA",
    "capture_identity",
]

#: Bounded memo: (symbol, session) rows kept per reader. One backtest bar touches at most one
#: session per symbol, so 2000 covers a wide universe without eviction churn.
MEMO_SIZE = 2000

CAPTURE_PROVIDER = "market_conditions"
CAPTURE_METHOD = "window"
CAPTURE_SCHEMA = "market_condition_window/v1"
_WINDOW_COLUMNS = ("open", "high", "low", "close", "volume")

_ABSENT = object()


class MarketConditionVersionMismatch(RuntimeError):
    """A row's calculator version differs from the profile the reader serves. Raised, never
    skipped: comparing a threshold tuned on one calculator against another's value is silent
    corruption."""


@dataclass(frozen=True)
class ObservedWindow:
    """One memoised observation: the row plus the window it was computed from (``None`` when
    the window could not be assembled) and that window's digest."""

    row: FeatureRow
    window: Optional[WindowResult]
    digest: Optional[str]


def _check_row(profile: str, calc_version: str, row: Optional[FeatureRow]) -> None:
    spec = PROFILES[profile]
    if spec.calc_version != calc_version:
        raise MarketConditionVersionMismatch(
            f"profile {profile!r} is registered at calc version {spec.calc_version!r}, "
            f"but this reader serves {calc_version!r}")
    if row is None:
        return
    wanted = [f.name for f in spec.fields]
    if sorted(row.by_field()) != sorted(wanted):
        raise MarketConditionVersionMismatch(
            f"row fields {sorted(row.by_field())!r} do not match profile {profile!r} fields {sorted(wanted)!r}")
    wrong = {f: v for f, v in row.calc_versions.items() if v != calc_version}
    if wrong:
        raise MarketConditionVersionMismatch(
            f"profile {profile!r} expects calc version {calc_version!r}; row carries {wrong!r}")


class WindowMarketConditionReader:
    """Base reader: bars -> window -> ``FeatureRow``, memoised. Thread-safe."""

    def __init__(self, profile: str, *, memo_size: int = MEMO_SIZE, retain_windows: bool = True):
        if profile not in PROFILES:
            raise KeyError(f"unknown market-condition profile {profile!r}; registered: {sorted(PROFILES)!r}")
        if profile not in COMPUTE_BY_PROFILE:
            raise KeyError(f"no calculator registered for profile {profile!r} in COMPUTE_BY_PROFILE")
        self.profile = profile
        self.calc_version = PROFILES[profile].calc_version
        self._memo_size = int(memo_size)
        #: Keep each memoised row's window arrays + digest (needed only for capture). A backtest
        #: reader turns it off: 2000 retained windows would be ~10 MB per run for nothing.
        self._retain_windows = bool(retain_windows)
        self._memo: "OrderedDict[Hashable, Any]" = OrderedDict()
        self._lock = threading.Lock()
        #: rows actually computed (memo misses that produced a row) -- test/benchmark visibility.
        self.computed = 0

    # -- subclass hooks
    def _bars(self, symbol: str, session: date) -> Optional[Tuple[Any, Any, Any, Any, Any, Any]]:
        """``(dates, o, h, l, c, v)`` covering at least the window ending at ``session`` (more is
        fine), or ``None`` when the source has no data at all for ``symbol``."""
        raise NotImplementedError

    def _memo_key(self, symbol: str, session: date) -> Hashable:
        return (symbol, session)

    # -- reading
    def observe(self, symbol: str, session: date) -> Optional[FeatureRow]:
        entry = self.observe_window(symbol, session)
        return None if entry is None else entry.row

    def observe_window(self, symbol: str, session: date) -> Optional[ObservedWindow]:
        key = self._memo_key(symbol, session)
        with self._lock:
            hit = self._memo.get(key, _ABSENT)
            if hit is not _ABSENT:
                self._memo.move_to_end(key)
        if hit is not _ABSENT:
            # The registry could have changed under a long-lived reader: one dict lookup and one
            # string compare per read (the full row check ran when the row was computed).
            if PROFILES[self.profile].calc_version != self.calc_version:
                _check_row(self.profile, self.calc_version, hit.row if hit is not None else None)
            return hit
        entry = self._compute(symbol, session)
        with self._lock:
            self._memo[key] = entry
            self._memo.move_to_end(key)
            while len(self._memo) > self._memo_size:
                self._memo.popitem(last=False)
        return entry

    def _compute(self, symbol: str, session: date) -> Optional[ObservedWindow]:
        bars = self._bars(symbol, session)
        if bars is None:
            return None
        window = assemble_window(*bars, session)
        spec = PROFILES[self.profile]
        if not window.ok:
            row = FeatureRow.uniform(spec, window.status, window.reason)
            entry = ObservedWindow(row=row, window=None, digest=None)
        else:
            row = COMPUTE_BY_PROFILE[self.profile](*window.arrays())
            if self._retain_windows:
                entry = ObservedWindow(row=row, window=window, digest=window_digest(*window.arrays()))
            else:
                entry = ObservedWindow(row=row, window=None, digest=None)
        _check_row(self.profile, self.calc_version, row)
        self.computed += 1
        return entry

    def memo_len(self) -> int:
        with self._lock:
            return len(self._memo)


class FMPCacheMarketConditionReader(WindowMarketConditionReader):
    """Live reader over the FMP daily OHLCV parquet cache ONLY.

    Reads ``<cache_root>/FMPOHLCVProvider/<SYM>_1d.parquet`` directly with the provider cache's
    column layout (``Date, Open, High, Low, Close, Volume``); with ``cache_root=None`` the path is
    resolved by ``native_cache.find_timeseries_path`` -- the same resolution the provider's own
    cache read uses. No provider method is called, so no network request can happen inside a
    condition; a symbol whose file is absent observes ``None`` (``missing_session``).

    The memo key carries the file's ``(mtime_ns, size)``: a cache the warmup completes or
    corrects later is re-read instead of being hidden behind an earlier negative row.
    """

    def __init__(self, profile: str, cache_root: Optional[str] = None, *, memo_size: int = MEMO_SIZE):
        super().__init__(profile, memo_size=memo_size)
        self._cache_root = cache_root

    def _path(self, symbol: str) -> Optional[str]:
        if self._cache_root is None:
            from ba2_common.core import native_cache
            return native_cache.find_timeseries_path(FMP_OHLCV_PROVIDER_DIR, symbol, "1d")
        path = os.path.join(self._cache_root, FMP_OHLCV_PROVIDER_DIR, f"{symbol.upper()}_1d.parquet")
        return path if os.path.exists(path) else None

    def _memo_key(self, symbol: str, session: date) -> Hashable:
        path = self._path(symbol)
        if path is None:
            return (symbol, session, None)
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return (symbol, session, None)
        return (symbol, session, path, st.st_mtime_ns, st.st_size)

    def _bars(self, symbol: str, session: date):
        import pandas as pd

        path = self._path(symbol)
        if path is None:
            return None
        df = pd.read_parquet(path, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        dates = pd.to_datetime(df["Date"])
        if getattr(dates.dt, "tz", None) is not None:
            dates = dates.dt.tz_convert("UTC").dt.tz_localize(None)
        return (dates.to_numpy(dtype="datetime64[ns]").astype("datetime64[D]"),
                df["Open"].to_numpy(np.float64), df["High"].to_numpy(np.float64),
                df["Low"].to_numpy(np.float64), df["Close"].to_numpy(np.float64),
                df["Volume"].to_numpy(np.float64))


# ---------------------------------------------------------------------------
# Capture / replay
# ---------------------------------------------------------------------------
def capture_identity(symbol: str, session: date, *, profile: str, source_profile: str,
                     timing_policy: str, calc_version: str) -> Dict[str, Any]:
    """The request identity of one recorded window. No wall clock in it (replay identity rule)."""
    return {
        "symbol": symbol,
        "session": session.isoformat(),
        "profile": profile,
        "source_profile": source_profile,
        "timing_policy": timing_policy,
        "calc_version": calc_version,
    }


def _window_frame(window: WindowResult):
    import pandas as pd

    return pd.DataFrame({name: np.asarray(arr, dtype=np.float64)
                         for name, arr in zip(_WINDOW_COLUMNS, window.arrays())})


class CapturingMarketConditionReader:
    """Record every row served by ``inner`` into a capture context, once per
    (symbol, session, window digest).

    The capture context is bound at construction (on the coordinating thread) and written through
    directly, so a leaf evaluated in a pool thread -- where the capture ContextVar is not set --
    still records. ``record`` has the ``MarketConditionContext.recorder`` signature; the conditions'
    per-valid-read recorder calls land on the same dedup set and add nothing.
    """

    def __init__(self, inner: WindowMarketConditionReader, capture: Any, *,
                 source_profile: str, timing_policy: str):
        if not inner._retain_windows:
            raise ValueError("capture needs a reader that retains its windows (retain_windows=True)")
        self.inner = inner
        self._capture = capture
        self._source_profile = source_profile
        self._timing_policy = timing_policy
        self._recorded: set = set()
        self._lock = threading.Lock()

    @property
    def profile(self) -> str:
        return self.inner.profile

    @property
    def calc_version(self) -> str:
        return self.inner.calc_version

    def observe(self, symbol: str, session: date) -> Optional[FeatureRow]:
        entry = self.inner.observe_window(symbol, session)
        self._record_entry(symbol, session, entry)
        return None if entry is None else entry.row

    def record(self, symbol: str, session: date, row: Any = None) -> None:
        self._record_entry(symbol, session, self.inner.observe_window(symbol, session))

    def _record_entry(self, symbol: str, session: date, entry: Optional[ObservedWindow]) -> None:
        key = (symbol, session, None if entry is None else entry.digest,
               None if entry is None else tuple(o.status for o in entry.row.by_field().values()))
        with self._lock:
            if key in self._recorded:
                return
            self._recorded.add(key)
        identity = capture_identity(symbol, session, profile=self.inner.profile,
                                    source_profile=self._source_profile,
                                    timing_policy=self._timing_policy,
                                    calc_version=self.inner.calc_version)
        payload: Dict[str, Any] = {
            "schema": CAPTURE_SCHEMA,
            **identity,
            "row_present": entry is not None,
            "window_digest": None,
            "window_first_session": None,
            "window": None,
            "values": {},
            "statuses": {},
            "reasons": {},
        }
        if entry is not None:
            fields = entry.row.by_field()
            payload["values"] = {f: o.value for f, o in fields.items()}
            payload["statuses"] = {f: o.status for f, o in fields.items()}
            payload["reasons"] = {f: o.reason for f, o in fields.items()}
            if entry.window is not None:
                payload["window_digest"] = entry.digest
                payload["window_first_session"] = entry.window.dates[0]
                payload["window"] = _window_frame(entry.window)
        from ba2_common.core.replay.observe import sanitize_identity
        from ba2_common.core.replay.schemas import ReplayStatus

        self._capture.record_observation(
            provider=CAPTURE_PROVIDER, method=CAPTURE_METHOD,
            request_identity=sanitize_identity(identity), payload=payload,
            provenance=ReplayStatus.PROVENANCE_DISK_CACHE)


class ReplayMarketConditionReader:
    """Serve recorded market-condition rows; anything unrecorded is a ``ReplayMiss``."""

    def __init__(self, recorded: Mapping[Tuple[str, date], Mapping[str, Any]], *, profile: str,
                 source_profile: str, timing_policy: str):
        if profile not in PROFILES:
            raise KeyError(f"unknown market-condition profile {profile!r}")
        self.profile = profile
        self.calc_version = PROFILES[profile].calc_version
        self._source_profile = source_profile
        self._timing_policy = timing_policy
        self._recorded = dict(recorded)
        self._rows: Dict[Tuple[str, date], Optional[FeatureRow]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_payloads(cls, payloads: Iterable[Mapping[str, Any]], **kwargs) -> "ReplayMarketConditionReader":
        recorded: Dict[Tuple[str, date], Mapping[str, Any]] = {}
        for payload in payloads:
            if payload.get("schema") != CAPTURE_SCHEMA:
                continue
            key = (payload["symbol"], date.fromisoformat(payload["session"]))
            previous = recorded.get(key)
            if previous is not None and previous.get("window_digest") != payload.get("window_digest"):
                from ba2_common.core.replay.context import ReplayMiss
                raise ReplayMiss("market_condition_window_conflict", request_identity=list(key),
                                 detail="two different windows were recorded for one symbol/session")
            recorded[key] = payload
        return cls(recorded, **kwargs)

    @classmethod
    def from_bundle(cls, bundle: Any, analysis_id: str, **kwargs) -> "ReplayMarketConditionReader":
        payloads = [bundle.decode(o.payload_object) for o in bundle.observations_for(analysis_id)
                    if o.provider == CAPTURE_PROVIDER and o.method == CAPTURE_METHOD
                    and o.payload_object is not None]
        return cls.from_payloads(payloads, **kwargs)

    def observe(self, symbol: str, session: date) -> Optional[FeatureRow]:
        key = (symbol, session)
        with self._lock:
            if key in self._rows:
                return self._rows[key]
        row = self._build(symbol, session)
        with self._lock:
            self._rows[key] = row
        return row

    def _build(self, symbol: str, session: date) -> Optional[FeatureRow]:
        from ba2_common.core.replay.context import ReplayMiss

        identity = capture_identity(symbol, session, profile=self.profile,
                                    source_profile=self._source_profile,
                                    timing_policy=self._timing_policy, calc_version=self.calc_version)
        payload = self._recorded.get((symbol, session))
        if payload is None:
            raise ReplayMiss("market_condition_window", request_identity=identity,
                             detail="no recorded market-condition window for this symbol/session")
        for name in ("profile", "source_profile", "timing_policy", "calc_version"):
            if payload[name] != identity[name]:
                if name == "calc_version":
                    raise MarketConditionVersionMismatch(
                        f"recorded calc version {payload[name]!r} != profile {self.profile!r} "
                        f"calc version {self.calc_version!r}")
                raise ReplayMiss("market_condition_window", request_identity=identity,
                                 detail=f"recorded {name} {payload[name]!r} differs")
        if not payload["row_present"]:
            return None
        frame = payload["window"]
        if payload["window_digest"] is not None:
            if frame is None or len(frame) != WINDOW:
                raise ReplayMiss("market_condition_window_bytes", request_identity=identity,
                                 detail="the recorded window bytes are not retained")
            digest = window_digest(*(frame[c].to_numpy(np.float64) for c in _WINDOW_COLUMNS))
            if digest != payload["window_digest"]:
                raise ReplayMiss("market_condition_window_digest", request_identity=identity,
                                 detail=f"retained window hashes to {digest}, recorded {payload['window_digest']}")
        values = {f: Observation(payload["values"][f] if payload["statuses"][f] == STATUS_VALID else None,
                                 payload["statuses"][f], payload["reasons"][f])
                  for f in payload["statuses"]}
        row = FeatureRow(values=values, calc_versions={f: payload["calc_version"] for f in values})
        _check_row(self.profile, self.calc_version, row)
        return row

    def recorded_window(self, symbol: str, session: date) -> Optional[Tuple[np.ndarray, ...]]:
        """The retained window arrays ``(o, h, l, c, v)`` for a recorded key (for recomputation
        comparisons), ``None`` when the recorded row had no window."""
        payload = self._recorded.get((symbol, session))
        if payload is None or payload["window"] is None:
            return None
        frame = payload["window"]
        return tuple(frame[c].to_numpy(np.float64) for c in _WINDOW_COLUMNS)
