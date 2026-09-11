"""Plan a warm without touching the network (spec step 4, section 6 lifecycle step 1).

Given the typed requirements a configuration declares, inspect the production and
explicitly configured shared roots READ-ONLY and emit exactly what is missing or
stale, with estimated bytes. The result (:class:`WarmPlan`) is what an operator
reviews before any bandwidth is spent, what the budget reserves against, and what
the worker consumes.

**Read-only means read-only.** Not one function here creates a directory, writes a
file or touches an mtime -- which is why the parquet path is resolved by hand
rather than through ``native_cache.timeseries_path`` (that one ``makedirs``) and
why the interval-alias table is imported from ``native_cache`` instead of being
copied. A planner that materialises anything has already changed the thing it was
asked to measure.

**Four states, not two.** ``present`` / ``checked_empty`` / ``stale`` / ``missing``
are different answers:

* ``present`` -- an artifact that covers the requirement. No action.
* ``checked_empty`` -- the ``[]`` sentinel: FMP was asked and genuinely has nothing
  for this symbol. No action; treating it as missing re-asks forever.
* ``stale`` -- the artifact exists but its freshness condition has expired (an
  ``fmp_history`` file past ``_FMP_HISTORY_DISK_MAX_AGE_DAYS``, a price series whose
  tail stops before the requested window end, a FRED file past its own max age).
  Refresh.
* ``missing`` -- nothing on any root. Fetch.

Freshness policies are per provider and data type on purpose (spec section 6:
"there is no blanket seven-day 'fresh enough' rule"): the history age comes from
the constant ``fmp_history_disk_cached`` itself enforces, the price tail from the
requirement's own window, and the FRED age from an explicit argument.
"""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ba2_common.core.replay.dependencies import (
    KIND_HISTORY,
    KIND_INDICATOR,
    KIND_SERIES,
    KIND_STATE,
    KIND_TIMESERIES,
    KIND_UNSUPPORTED,
    Requirement,
)

#: Plan format version. A pinned root and a plan JSON are read by later runs; a
#: shape change gets a new number rather than a silently different meaning.
PLAN_VERSION = 1

STATUS_PRESENT = "present"
STATUS_CHECKED_EMPTY = "checked_empty"
STATUS_STALE = "stale"
STATUS_MISSING = "missing"
#: Platform-local state: real, required, and nothing a download can supply.
STATUS_LOCAL_STATE = "local_state"
#: The expert has no dependency adapter: its reads are undeclared.
STATUS_UNSUPPORTED = "unsupported"

ACTION_NONE = "none"
ACTION_FETCH = "fetch"
ACTION_REFRESH = "refresh"
#: Surface it in the report; there is nothing to fetch.
ACTION_REPORT = "report"

#: How old a FRED cache file may be before a plan calls it stale. The macro series
#: are published daily/monthly, and the existing prewarm refreshes them at 24h
#: (``data_build_handler._prewarm_fred``); it is a keyword argument so a caller can
#: state a different policy rather than inherit this one by accident.
FRED_MAX_AGE_HOURS = 24.0

#: A sentinel file is the two bytes ``[]``. Anything this small is parsed to tell a
#: sentinel from a real (tiny) payload; bigger files are never read, only stat'd.
_SENTINEL_PROBE_BYTES = 64


class WarmPlanError(RuntimeError):
    """The plan cannot be built as asked -- a requirement names something unresolvable.

    Distinct from "this requirement is missing on disk", which is a normal plan
    entry. This is a caller error (an unknown OHLCV provider name, a requirement
    kind with no inspector) and must not be reported as a data gap.
    """


# --------------------------------------------------------------------------- #
# Entries and the plan
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlanEntry:
    """One requirement, and what the roots say about it."""

    requirement: Requirement
    status: str
    action: str
    #: The root the artifact was found under, or ``None`` when nothing was found.
    source_root: Optional[str] = None
    #: The artifact's absolute path when it exists.
    path: Optional[str] = None
    #: Its size on disk, when it exists.
    size_bytes: Optional[int] = None
    #: What a fetch is expected to cost, measured from comparable files. ``None``
    #: when nothing comparable exists to measure -- never a guessed number.
    estimated_bytes: Optional[int] = None
    #: Parquet only: rows on disk and the newest date they cover.
    rows: Optional[int] = None
    max_date: Optional[str] = None
    detail: str = ""

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "requirement": self.requirement.to_mapping(),
            "status": self.status,
            "action": self.action,
            "source_root": self.source_root,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "estimated_bytes": self.estimated_bytes,
            "rows": self.rows,
            "max_date": self.max_date,
            "detail": self.detail,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PlanEntry":
        return cls(
            requirement=Requirement.from_mapping(data["requirement"]),
            status=data["status"],
            action=data["action"],
            source_root=data["source_root"],
            path=data["path"],
            size_bytes=data["size_bytes"],
            estimated_bytes=data["estimated_bytes"],
            rows=data["rows"],
            max_date=data["max_date"],
            detail=data["detail"],
        )


@dataclass(frozen=True)
class WarmPlan:
    """What a warm run would do, and what it would cost."""

    created_at: str
    roots: Tuple[str, ...]
    entries: Tuple[PlanEntry, ...]
    version: int = PLAN_VERSION

    def pending(self) -> List[PlanEntry]:
        """The entries a warm run would actually work on, in plan order.

        Required before optional: spec section 6 gives live requests priority and a
        declared-but-unused optional dependency is not a proven live input, so it
        must not consume the allowance ahead of something that is.
        """
        work = [e for e in self.entries if e.action in (ACTION_FETCH, ACTION_REFRESH)]
        return [e for e in work if not e.requirement.optional] + \
               [e for e in work if e.requirement.optional]

    def totals(self) -> Dict[str, int]:
        """Counts by status, plus ``pending`` and ``unknown_estimate``."""
        out: Dict[str, int] = {
            status: 0 for status in
            (STATUS_PRESENT, STATUS_CHECKED_EMPTY, STATUS_STALE, STATUS_MISSING,
             STATUS_LOCAL_STATE, STATUS_UNSUPPORTED)
        }
        for entry in self.entries:
            out[entry.status] = out.get(entry.status, 0) + 1
        pending = self.pending()
        out["pending"] = len(pending)
        out["unknown_estimate"] = sum(1 for e in pending if e.estimated_bytes is None)
        return out

    def estimated_bytes_total(self) -> int:
        """Measured estimate for the pending work. Entries with no measurement add 0.

        They are counted separately by :meth:`totals` (``unknown_estimate``); the
        budget reserves for them explicitly rather than having a guess folded in
        here where nobody can see it.
        """
        return sum(e.estimated_bytes or 0 for e in self.pending())

    def measured_median_bytes(self) -> Optional[int]:
        """The median of every byte figure this plan MEASURED.

        Both halves count: an artifact that exists contributes its own size, and a
        missing one contributes the median of its comparable files (itself measured).
        Every number here came off a real file.

        This is the honest basis for what to reserve for an unknown-size response.
        ``None`` when the roots held nothing at all -- in which case a caller must be
        told to warm from a seeded root rather than be handed an invented number.
        """
        sizes = [e.size_bytes for e in self.entries if e.size_bytes]
        sizes += [e.estimated_bytes for e in self.entries if e.estimated_bytes]
        if not sizes:
            return None
        return int(statistics.median(sizes))

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "roots": list(self.roots),
            "entries": [e.to_mapping() for e in self.entries],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_mapping(), indent=indent, sort_keys=True)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WarmPlan":
        version = int(data["version"])
        if version != PLAN_VERSION:
            raise WarmPlanError(
                f"warm plan version {version} cannot be read by this build (expects "
                f"{PLAN_VERSION}); re-plan rather than reinterpreting the old shape")
        return cls(
            created_at=data["created_at"],
            roots=tuple(data["roots"]),
            entries=tuple(PlanEntry.from_mapping(e) for e in data["entries"]),
            version=version,
        )

    @classmethod
    def from_json(cls, text: str) -> "WarmPlan":
        return cls.from_mapping(json.loads(text))

    def to_markdown(self) -> str:
        """A short operator-readable summary: totals, then every pending item."""
        totals = self.totals()
        lines = [
            f"# Warm plan ({self.created_at})",
            "",
            f"Roots: {', '.join(self.roots)}",
            "",
            "| status | count |",
            "|---|---:|",
        ]
        for status, count in totals.items():
            lines.append(f"| {status} | {count} |")
        estimate = self.estimated_bytes_total()
        lines += ["", f"Estimated download: {estimate / 1048576.0:.1f} MiB "
                      f"({totals['unknown_estimate']} item(s) with no measured estimate)", ""]
        if self.pending():
            lines += ["| action | kind | provider | namespace | symbol | est. bytes | why |",
                      "|---|---|---|---|---|---:|---|"]
            for entry in self.pending():
                req = entry.requirement
                estimate = "?" if entry.estimated_bytes is None else str(entry.estimated_bytes)
                lines.append(
                    f"| {entry.action} | {req.kind} | {req.provider} | {req.namespace} | "
                    f"{req.symbol or '-'} | {estimate} | {req.reason} |")
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# The plan itself
# --------------------------------------------------------------------------- #
def plan(requirements: Sequence[Requirement], roots: Sequence[str], *,
         as_of_now: datetime,
         fred_max_age_hours: float = FRED_MAX_AGE_HOURS) -> WarmPlan:
    """Inspect ``roots`` (in order, first match wins) and say what each requirement needs.

    ``roots[0]`` is by convention the writable root a warm would fill; the rest are
    read-only shared roots that can SATISFY a requirement but are never written to.
    Every root is inspected read-only here regardless.

    ``as_of_now`` is passed, never read from the wall clock: a plan is compared
    against a recorded session and its staleness answers have to be reproducible.
    """
    if as_of_now.tzinfo is None:
        raise WarmPlanError("as_of_now must be timezone-aware (UTC)")
    root_list = [str(r) for r in roots]
    measured = _SizeIndex(root_list)
    entries = [_inspect(req, root_list, as_of_now, fred_max_age_hours, measured)
               for req in requirements]
    return WarmPlan(
        created_at=as_of_now.astimezone(timezone.utc).isoformat(),
        roots=tuple(root_list),
        entries=tuple(entries),
    )


def _inspect(req: Requirement, roots: Sequence[str], as_of_now: datetime,
             fred_max_age_hours: float, measured: "_SizeIndex") -> PlanEntry:
    if req.kind == KIND_HISTORY:
        return _inspect_history(req, roots, as_of_now, measured)
    if req.kind == KIND_SERIES:
        return _inspect_series(req, roots, as_of_now, fred_max_age_hours, measured)
    if req.kind in (KIND_TIMESERIES, KIND_INDICATOR):
        return _inspect_timeseries(req, roots, measured)
    if req.kind == KIND_STATE:
        return PlanEntry(requirement=req, status=STATUS_LOCAL_STATE, action=ACTION_REPORT,
                         detail="platform-local state; nothing to warm")
    if req.kind == KIND_UNSUPPORTED:
        return PlanEntry(requirement=req, status=STATUS_UNSUPPORTED, action=ACTION_REPORT,
                         detail=req.reason)
    raise WarmPlanError(f"no planner inspector for requirement kind {req.kind!r}")


# --------------------------------------------------------------------------- #
# fmp_history
# --------------------------------------------------------------------------- #
def _history_relpath(req: Requirement) -> str:
    """``fmp_history/<namespace>__<SYMBOL>.json`` -- the layout ``fmp_history_disk_cached`` writes.

    Built here rather than through ``_fmp_history_cache_dir`` because that reads the
    process's configured ``CACHE_FOLDER``, and a planner inspects roots it was
    HANDED (a shared root, a pinned root, a temp root in a test).
    """
    if not req.symbol:
        raise WarmPlanError(f"a {req.kind} requirement needs a symbol ({req.namespace})")
    return os.path.join("fmp_history", f"{req.namespace}__{req.symbol.upper()}.json")


def _inspect_history(req: Requirement, roots: Sequence[str], as_of_now: datetime,
                     measured: "_SizeIndex") -> PlanEntry:
    from ba2_providers.fmp_common import _FMP_HISTORY_DISK_MAX_AGE_DAYS

    relpath = _history_relpath(req)
    for root in roots:
        path = os.path.join(root, relpath)
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        age_days = (as_of_now.timestamp() - os.path.getmtime(path)) / 86400.0
        if age_days > _FMP_HISTORY_DISK_MAX_AGE_DAYS:
            return PlanEntry(
                requirement=req, status=STATUS_STALE, action=ACTION_REFRESH,
                source_root=root, path=path, size_bytes=size,
                estimated_bytes=size,
                detail=(f"age {age_days:.1f}d exceeds the {_FMP_HISTORY_DISK_MAX_AGE_DAYS:g}d "
                        f"fmp_history freshness window"))
        if _is_empty_sentinel(path, size):
            return PlanEntry(
                requirement=req, status=STATUS_CHECKED_EMPTY, action=ACTION_NONE,
                source_root=root, path=path, size_bytes=size,
                detail="empty-list sentinel: FMP was asked and has no data for this symbol")
        return PlanEntry(requirement=req, status=STATUS_PRESENT, action=ACTION_NONE,
                         source_root=root, path=path, size_bytes=size,
                         detail=f"age {age_days:.1f}d")
    return PlanEntry(requirement=req, status=STATUS_MISSING, action=ACTION_FETCH,
                     estimated_bytes=measured.median_for_namespace(req.namespace),
                     detail=f"no {relpath} under any configured root")


def _is_empty_sentinel(path: str, size: int) -> bool:
    """Whether the file is the ``[]`` prewarm sentinel. Only tiny files are opened."""
    if size > _SENTINEL_PROBE_BYTES:
        return False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) == []
    except Exception:  # noqa: BLE001 - unreadable/corrupt is not a sentinel; the age check stands
        return False


# --------------------------------------------------------------------------- #
# FRED series
# --------------------------------------------------------------------------- #
def _inspect_series(req: Requirement, roots: Sequence[str], as_of_now: datetime,
                    fred_max_age_hours: float, measured: "_SizeIndex") -> PlanEntry:
    relpath = os.path.join("fred", f"{req.namespace.upper()}.json")
    for root in roots:
        path = os.path.join(root, relpath)
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        age_hours = (as_of_now.timestamp() - os.path.getmtime(path)) / 3600.0
        if age_hours > fred_max_age_hours:
            return PlanEntry(requirement=req, status=STATUS_STALE, action=ACTION_REFRESH,
                             source_root=root, path=path, size_bytes=size,
                             estimated_bytes=size,
                             detail=(f"age {age_hours:.1f}h exceeds the configured "
                                     f"{fred_max_age_hours:g}h macro freshness window"))
        return PlanEntry(requirement=req, status=STATUS_PRESENT, action=ACTION_NONE,
                         source_root=root, path=path, size_bytes=size,
                         detail=f"age {age_hours:.1f}h")
    return PlanEntry(requirement=req, status=STATUS_MISSING, action=ACTION_FETCH,
                     estimated_bytes=measured.median_for_dir("fred"),
                     detail=f"no {relpath} under any configured root")


# --------------------------------------------------------------------------- #
# Parquet time series (and the indicators derived from them)
# --------------------------------------------------------------------------- #
def _interval_spellings(interval: str) -> Tuple[str, ...]:
    """Every on-disk spelling of ``interval``, canonical first.

    Imported from ``native_cache`` rather than restated: a new alias added there
    must not leave the planner unable to see files the readers can.
    """
    from ba2_common.core import native_cache

    canon = native_cache.normalize_interval(interval)
    return tuple(native_cache._INTERVAL_ALIASES.get(canon, [canon]))  # noqa: SLF001


def _ohlcv_provider_dirs(req: Requirement, roots: Sequence[str]) -> List[str]:
    """The parquet directory name(s) a requirement's series could live under.

    A ``timeseries`` requirement names its provider by REGISTRY key ("fmp"), and the
    parquet directory is that provider CLASS's name -- resolved through the registry
    so a rename cannot desync the two. An ``indicator`` requirement names the
    registry CATEGORY instead, because which OHLCV source backs the host's indicator
    provider is host wiring: every provider directory present on the roots is
    searched rather than one being invented.
    """
    if req.kind == KIND_TIMESERIES:
        from ba2_providers import OHLCV_PROVIDERS

        cls = OHLCV_PROVIDERS.get(req.provider)
        if cls is None:
            raise WarmPlanError(
                f"unknown OHLCV provider {req.provider!r}; known: "
                f"{', '.join(sorted(OHLCV_PROVIDERS))}")
        return [cls.__name__]

    from ba2_providers import OHLCV_PROVIDERS

    known = [cls.__name__ for cls in OHLCV_PROVIDERS.values()]
    present = []
    for root in roots:
        for name in known:
            if name not in present and os.path.isdir(os.path.join(root, name)):
                present.append(name)
    return present or known


def _inspect_timeseries(req: Requirement, roots: Sequence[str],
                        measured: "_SizeIndex") -> PlanEntry:
    if not req.symbol:
        raise WarmPlanError(f"a {req.kind} requirement needs a symbol ({req.namespace})")
    provider_dirs = _ohlcv_provider_dirs(req, roots)
    spellings = _interval_spellings(req.interval)
    looked: List[str] = []
    for root in roots:
        for provider_dir in provider_dirs:
            for spelling in spellings:
                relpath = os.path.join(provider_dir, f"{req.symbol.upper()}_{spelling}.parquet")
                path = os.path.join(root, relpath)
                looked.append(relpath)
                if not os.path.exists(path):
                    continue
                rows, max_date = _parquet_coverage(path)
                size = os.path.getsize(path)
                if not rows:
                    return PlanEntry(
                        requirement=req, status=STATUS_MISSING, action=ACTION_FETCH,
                        source_root=root, path=path, size_bytes=size, rows=rows,
                        estimated_bytes=measured.median_for_dir(provider_dir),
                        detail="the parquet holds no bars (a broken cache, not coverage)")
                needed_end = req.window.end if req.window else None
                if needed_end is not None and not _covers_end(max_date, needed_end, req.interval):
                    return PlanEntry(
                        requirement=req, status=STATUS_STALE, action=ACTION_REFRESH,
                        source_root=root, path=path, size_bytes=size, rows=rows,
                        max_date=max_date.isoformat() if max_date else None,
                        estimated_bytes=measured.median_for_dir(provider_dir),
                        detail=(f"tail stops at "
                                f"{max_date.date().isoformat() if max_date else 'unknown'}, "
                                f"before the requested window end "
                                f"{needed_end.date().isoformat()}"))
                return PlanEntry(
                    requirement=req, status=STATUS_PRESENT, action=ACTION_NONE,
                    source_root=root, path=path, size_bytes=size, rows=rows,
                    max_date=max_date.isoformat() if max_date else None,
                    detail=f"{rows} bars")
    return PlanEntry(
        requirement=req, status=STATUS_MISSING, action=ACTION_FETCH,
        estimated_bytes=measured.median_for_dir(provider_dirs[0]) if provider_dirs else None,
        detail=f"no series file under any configured root (looked for {', '.join(sorted(set(looked)))})")


#: Canonical intervals whose bars are stamped at a DAY (or coarser) boundary. Their
#: coverage is compared by DATE: a daily series whose newest bar is today's does
#: cover a window that ends at 20:00 today, and comparing the timestamps would
#: report every complete daily cache in the platform as stale forever.
_DAY_OR_COARSER = ("1d", "1wk", "1mo")


def _covers_end(max_date: Optional[datetime], needed_end: datetime, interval: str) -> bool:
    """Whether a series whose newest bar is ``max_date`` reaches ``needed_end``."""
    if max_date is None:
        return False
    from ba2_common.core import native_cache

    if native_cache.normalize_interval(interval) in _DAY_OR_COARSER:
        return max_date.date() >= needed_end.date()
    return max_date >= needed_end


def _parquet_coverage(path: str) -> Tuple[Optional[int], Optional[datetime]]:
    """``(rows, newest effective date)`` from a parquet, read-only.

    The row count comes from the footer (O(1)). The max date needs the column, so
    only the date column is read -- never the whole frame.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as e:  # pragma: no cover - pyarrow is a hard dependency of the cache
        raise WarmPlanError(f"cannot inspect parquet coverage: {e}") from e
    try:
        pf = pq.ParquetFile(path)
        rows = int(pf.metadata.num_rows)
        if rows == 0:
            return 0, None
        names = set(pf.schema_arrow.names)
        column = "effective_date" if "effective_date" in names else (
            "Date" if "Date" in names else None)
        if column is None:
            return rows, None
        import pandas as pd

        values = pd.to_datetime(pf.read(columns=[column]).column(column).to_pandas(), utc=True)
        if values.empty:
            return rows, None
        return rows, values.max().to_pydatetime()
    except Exception:  # noqa: BLE001 - an unreadable cache file is a gap, not a crash
        return None, None


# --------------------------------------------------------------------------- #
# Size measurement
# --------------------------------------------------------------------------- #
class _SizeIndex:
    """Measured file sizes per cache sub-directory, for estimating a fetch.

    Spec section 6 asks for estimated bytes; an estimate has to come from somewhere
    real, so it is the MEDIAN size of comparable files already on the roots (same
    ``fmp_history`` namespace, or same provider/``fred`` directory). When there is
    nothing comparable the answer is ``None`` -- a made-up number would make the
    whole reservation fiction.

    Lazy and cached: a plan for 400 symbols lists one directory once.
    """

    def __init__(self, roots: Sequence[str]) -> None:
        self._roots = list(roots)
        self._dirs: Dict[str, List[int]] = {}
        self._namespaces: Dict[str, List[int]] = {}

    def _scan_dir(self, name: str) -> List[int]:
        if name in self._dirs:
            return self._dirs[name]
        sizes: List[int] = []
        for root in self._roots:
            directory = os.path.join(root, name)
            if not os.path.isdir(directory):
                continue
            with os.scandir(directory) as it:
                for entry in it:
                    if entry.is_file():
                        sizes.append(entry.stat().st_size)
        self._dirs[name] = sizes
        return sizes

    def median_for_dir(self, name: str) -> Optional[int]:
        sizes = [s for s in self._scan_dir(name) if s > 0]
        return int(statistics.median(sizes)) if sizes else None

    def median_for_namespace(self, namespace: str) -> Optional[int]:
        """Median size of the ``fmp_history`` files in ONE namespace.

        Per namespace, not per directory: a ``price_target`` payload and an
        ``income_statement_annual`` payload differ by an order of magnitude, and an
        estimate that mixes them tells the budget nothing.
        """
        if namespace not in self._namespaces:
            prefix = f"{namespace}__"
            sizes: List[int] = []
            for root in self._roots:
                directory = os.path.join(root, "fmp_history")
                if not os.path.isdir(directory):
                    continue
                with os.scandir(directory) as it:
                    for entry in it:
                        if entry.is_file() and entry.name.startswith(prefix):
                            size = entry.stat().st_size
                            if size > 0:
                                sizes.append(size)
            self._namespaces[namespace] = sizes
        sizes = self._namespaces[namespace]
        return int(statistics.median(sizes)) if sizes else None


__all__ = [
    "ACTION_FETCH",
    "ACTION_NONE",
    "ACTION_REFRESH",
    "ACTION_REPORT",
    "FRED_MAX_AGE_HOURS",
    "PLAN_VERSION",
    "PlanEntry",
    "STATUS_CHECKED_EMPTY",
    "STATUS_LOCAL_STATE",
    "STATUS_MISSING",
    "STATUS_PRESENT",
    "STATUS_STALE",
    "STATUS_UNSUPPORTED",
    "WarmPlan",
    "WarmPlanError",
    "plan",
]
