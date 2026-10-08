"""Launch- and job-start PREFLIGHT: every symbol an intraday-reading job touches must have an intraday
cache on the SAME price level as its daily cache.

WHY (2026-10-08). An intraday-clock backtest reads its decision price from the 5-minute bars and its
history (indicators, ATR, recent highs, TP/SL anchors) from the daily bars. When a symbol later splits,
the daily file is rewritten on the new basis and the 5-minute file keeps the old one; a reviewer measured
7 of 250 random symbols with a constant 0.10x .. 6.25x factor between the two for a whole year, and
nothing compared the two intervals' price LEVELS (``tools/cache_health_check.py`` checked CRC, date gaps,
month coverage and NaN rates only). The check is ``ba2_providers.ohlcv.cross_interval_basis`` (ONE
function; this module only decides WHO must pass it, WHEN, and what a refusal looks like).

WHO. Every job that reads intraday bars:

* an INTRADAY-CLOCK job (``execution_interval`` is sub-daily): the fill clock, the decision price and the
  TP/SL walk all read the intraday series;
* a DAILY-clock job that prices OPTIONS (``options_cache_db`` present): the post-hoc intraday drawdown
  refinement (``results._build_refine_drawdown_fn``) re-prices flagged trades on the underlying's
  5-minute bars. A daily-clock EQUITY job reads no intraday bar at all and is not checked.

WHEN. At launch (``ba2test_launcher._refuse_intraday_basis_mismatch``, on the master, early) and at the
start of every job on every process that runs one (``daily_backtest_handler.run_daily_backtest``, BEFORE
the first bar is loaded): a worker's cache can differ from the master's. A refusal raises
``IntradayBasisMismatch`` / ``IntradayBasisStale``; both are in ``job_fatal.JOB_FATAL_ERROR_TYPES``, so the
first trial that hits one ends the job instead of scoring 0 and carrying on.

WINDOW. ``cross_interval_basis.judged_window``: from ``start - warmup_days`` (what the engine preloads) and at
least ``MIN_JUDGED_DAYS`` before ``end`` (what the provider's read guard judges), so nothing the engine reads
lies outside what was judged.

COST. The per-symbol work (reading the intraday file and reducing it to one row per session) is memoised
by the two files' identity (size + mtime_ns), in the process AND on disk (``MemoDir``, next to the cache
folder): once per file, ever, until the file changes. On top of that this module keeps a JOB memo keyed on the
universe, the window AND the identity (``os.stat``) of every symbol's daily file, intraday file, stale marker
and rebase sidecar: a long-lived worker process sees a repair or a new defect at its next trial (a few thousand
``stat`` calls), and re-judges nothing while the files are untouched.

NO ESCAPE HATCH THAT SKIPS. There is no flag that turns the check off. The reviewed ways to run a job without
a refused symbol are RECORDED on the run: ``--exclude-symbols`` (``excluded_instruments``), and the reviewed
exclusion list ``ba2_providers/ohlcv/intraday_basis_exclusions.json`` (the launcher removes a listed symbol from
the universe with a printed line and a results entry; ``reviewed_by`` starting with "pending" is not in force).
The repairs are ``force_full_refetch(symbol, '5min')`` and, where the vendor serves the two endpoints on different
bases, ``tools/repair_intraday_basis.py`` (a rebase with provenance, surfaced here).

NOT REFUSED, BUT NEVER SILENT. ``no_intraday`` / ``no_daily`` (a missing file is a ``BacktestCacheMiss`` of its own
for an intraday clock) and symbols with fewer than ``MIN_COMMON_SESSIONS`` daily sessions are carried in the
report. An ``insufficient`` symbol whose window holds enough DAILY sessions to be judged is a COVERAGE problem
(the intraday file lacks the sessions) and REFUSES. For a daily-clock OPTIONS job a missing or thin intraday
file means the drawdown refinement silently skips that underlying (``refinement_status="failed:..."``): it is a
launch WARNING recorded in the job result with the symbol list and the number of affected sessions.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from ba2_common.core import native_cache, split_basis
from ba2_common.core.split_basis import IntradayBasisMismatch, IntradayBasisStale
from ba2_providers.ohlcv import cross_interval_basis as cib
from ba2_providers.ohlcv import intraday_exclusions

logger = logging.getLogger(__name__)

#: The vendor key every backtest read goes through (``get_provider("ohlcv", "fmp")`` in ``run_daily_backtest``,
#: ``results._build_refine_drawdown_fn`` and the seams). The cache provider NAME is derived from the registry, and
#: ``run_daily_backtest`` passes the name of the provider instance it actually built.
BACKTEST_OHLCV_VENDOR = "fmp"


def default_provider_name() -> str:
    import ba2_providers
    return ba2_providers.OHLCV_PROVIDERS[BACKTEST_OHLCV_VENDOR].__name__


#: The interval an options job's drawdown refinement reads (``results._get_5m_bars_cached``).
OPTION_REFINEMENT_INTERVAL = "5m"
#: A trial result carries at most this many symbols per list by name (counts are always exact).
_RECORDED_MAX = 100


@dataclass
class PreflightReport:
    interval: str
    window_start: str
    window_end: str
    symbols: int
    job_kind: str = "intraday"                    # "intraday" (intraday clock) | "options" (daily clock + options)
    provider: str = ""
    mismatched: List[cib.BasisResult] = field(default_factory=list)
    stale: List[Tuple[str, str]] = field(default_factory=list)       # (symbol, marker reason)
    coverage_refused: List[cib.BasisResult] = field(default_factory=list)
    unjudged: List[cib.BasisResult] = field(default_factory=list)
    option_uncovered: List[cib.BasisResult] = field(default_factory=list)
    rebased: List[Dict[str, Any]] = field(default_factory=list)      # universe symbols on REBASED intraday prices
    bursts: List[Tuple[str, int]] = field(default_factory=list)      # judged ok, but >= 3 sessions >10% off in the window
    excluded_listed: List[Dict[str, Any]] = field(default_factory=list)   # reviewed exclusions applied at launch
    counts: Dict[str, int] = field(default_factory=dict)
    pending_listed: List[str] = field(default_factory=list)          # mismatched, listed but NOT reviewed (pending)
    listed_in_universe: List[str] = field(default_factory=list)      # mismatched, listed and reviewed, still in the universe

    @property
    def refused(self) -> bool:
        return bool(self.mismatched or self.stale or self.coverage_refused)

    def refusal_message(self) -> str:
        lines = []
        for r in self.mismatched:
            note = ""
            if r.symbol in self.listed_in_universe:
                note = "   [on the reviewed exclusion list but still in this job's universe: remove it at launch]"
            elif r.symbol in self.pending_listed:
                note = "   [on the exclusion list but 'pending' review: not in force]"
            lines.append("  " + r.describe() + note)
        for sym, why in self.stale:
            lines.append(f"  {sym}: stale marker -- {why}")
        for r in self.coverage_refused:
            lines.append(f"  {r.symbol}: intraday COVERAGE -- {r.daily_sessions} daily sessions in the window but only "
                         f"{r.common_sessions} comparable intraday sessions ({r.reason})")
        n_bad = len(self.mismatched) + len(self.stale)
        head = (f"intraday/daily PRICE-LEVEL mismatch in the {self.interval} cache for {n_bad} of {self.symbols} "
                f"symbols" if n_bad else f"intraday COVERAGE too thin for {len(self.coverage_refused)} of {self.symbols} symbols")
        if n_bad and self.coverage_refused:
            head += f", and intraday coverage too thin for {len(self.coverage_refused)}"
        return (f"{head} over {self.window_start}..{self.window_end} (symbol, factor, class):\n" + "\n".join(lines) + "\n"
                f"A run on these symbols prices decisions from one basis and history from another (or has no intraday "
                f"price at all). Repair: force_full_refetch(<symbol>, '5min'); where the vendor serves the two endpoints "
                f"on different bases: tools/repair_intraday_basis.py plan --symbols <SYM> (rebase with provenance); "
                f"or run without them: --exclude-symbols, or the reviewed list "
                f"ba2_providers/ohlcv/intraday_basis_exclusions.json (both recorded on the run). "
                f"See tools/cache_health_check.py --basis-symbols <SYM>.")

    def to_dict(self) -> dict:
        """What a job result records: the verdict counts and every symbol that could NOT be judged, is on rebased
        prices, was excluded by the reviewed list, or sits in a remaining burst (never silent)."""
        def names(rows, f=lambda r: r):
            return [f(r) for r in rows[:_RECORDED_MAX]]
        return {
            "interval": self.interval, "window": [self.window_start, self.window_end], "job_kind": self.job_kind,
            "provider": self.provider, "symbols": self.symbols, "counts": dict(self.counts),
            "unjudged": names(self.unjudged, lambda r: {"symbol": r.symbol, "class": r.klass, "reason": r.reason}),
            "unjudged_count": len(self.unjudged),
            "rebased_intraday": {"count": len(self.rebased), "symbols": names(self.rebased, lambda r: r["symbol"]),
                                 "volume_unadjusted": [r["symbol"] for r in self.rebased if r.get("volume_unadjusted")],
                                 "sources": sorted({str(r.get("source")) for r in self.rebased})},
            "excluded_by_reviewed_list": list(self.excluded_listed),
            "remaining_bursts": {"count": len(self.bursts), "symbols": names(self.bursts, lambda r: {"symbol": r[0], "sessions_off_10pct": r[1]})},
            "option_refinement_uncovered": {
                "count": len(self.option_uncovered),
                "daily_sessions_without_intraday": sum(r.no_intraday_sessions if r.klass != cib.KLASS_NO_INTRADAY
                                                       else r.daily_sessions for r in self.option_uncovered),
                "symbols": names(self.option_uncovered, lambda r: {"symbol": r.symbol, "class": r.klass,
                                                                    "daily_sessions": r.daily_sessions,
                                                                    "sessions_without_intraday": (r.daily_sessions if r.klass == cib.KLASS_NO_INTRADAY else r.no_intraday_sessions)})},
        }

    def raise_if_refused(self) -> None:
        if not self.refused:
            return
        if self.mismatched or self.coverage_refused:
            raise IntradayBasisMismatch(self.refusal_message())
        raise IntradayBasisStale(self.refusal_message())


def scan(symbols: Iterable[str], interval: str, start: Any, end: Any, *, workers: int = 1,
         provider: Optional[str] = None, job_kind: str = "intraday") -> PreflightReport:
    """Judge ``symbols`` over ``[start, end]`` and return the report (never raises on a mismatch)."""
    provider = provider or default_provider_name()
    syms = sorted({str(s).upper() for s in symbols})
    results = cib.check_many(syms, interval, start, end, provider=provider, workers=workers)
    rep = PreflightReport(interval=interval, window_start=str(pd.Timestamp(start).date()),
                          window_end=str(pd.Timestamp(end).date()), symbols=len(syms), job_kind=job_kind,
                          provider=provider)
    for r in results:
        rep.counts[r.klass] = rep.counts.get(r.klass, 0) + 1
        path = native_cache.find_timeseries_path(provider, r.symbol, interval)
        marker = split_basis.read_intraday_stale(path) if path else None
        if marker is not None:
            rep.stale.append((r.symbol, str(marker.get("reason"))))
        rb = split_basis.read_intraday_rebase(path) if path else None
        if rb is not None:
            segs = rb.get("segments") or []
            rep.rebased.append({"symbol": r.symbol, "source": rb.get("source"), "applied_utc": rb.get("applied_utc"),
                                "volume_unadjusted": any(isinstance(s, dict) and s.get("volume_unadjusted") for s in segs)})
        if r.mismatched:
            rep.mismatched.append(r)
        elif r.klass == cib.KLASS_INSUFFICIENT and r.daily_sessions >= cib.MIN_COMMON_SESSIONS:
            rep.coverage_refused.append(r)
        elif r.unjudged:
            rep.unjudged.append(r)
        elif r.klass == cib.KLASS_OK and r.far_sessions >= cib.BURST_MIN_SESSIONS:
            rep.bursts.append((r.symbol, r.far_sessions))
        if job_kind == "options" and r.klass in (cib.KLASS_NO_INTRADAY, cib.KLASS_INSUFFICIENT):
            rep.option_uncovered.append(r)
    if rep.counts:
        rep.counts["stale_marker"] = len(rep.stale)
        rep.counts["rebased_intraday"] = len(rep.rebased)
    try:
        listed = intraday_exclusions.load_exclusions()
    except (OSError, ValueError, KeyError) as e:        # an unreadable/invalid list must be loud, not 'empty'
        raise RuntimeError(f"the intraday-basis exclusion list is unreadable: {e}") from e
    force = intraday_exclusions.in_force(listed)
    pend = intraday_exclusions.pending(listed)
    bad_syms = {r.symbol for r in rep.mismatched} | {s for s, _ in rep.stale}
    rep.listed_in_universe = sorted(s for s in bad_syms if s in force)
    rep.pending_listed = sorted(s for s in bad_syms if s in pend)
    return rep


def interval_to_check(config: Dict[str, Any]) -> Optional[str]:
    """The intraday interval this job's config reads, or None when it reads none (a daily-clock equity
    job). The absent-key reading ("1d") is the one ``run_daily_backtest`` itself applies."""
    from app.services.backtest.price_source import _is_intraday
    interval = config.get("execution_interval", "1d")
    if _is_intraday(interval):
        return interval
    if config.get("options_cache_db"):
        return OPTION_REFINEMENT_INTERVAL
    return None


def job_kind_of(config: Dict[str, Any]) -> str:
    from app.services.backtest.price_source import _is_intraday
    return "intraday" if _is_intraday(config.get("execution_interval", "1d")) else "options"


_JOB_MEMO: Dict[tuple, PreflightReport] = {}
_JOB_LOCK = threading.Lock()


def _stat_id(path: Optional[str]) -> Optional[Tuple[int, int]]:
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (int(st.st_size), int(st.st_mtime_ns))


def _state_digest(symbols: Sequence[str], interval: str, provider: str) -> str:
    """Identity of every file whose content decides the verdict: the symbol's daily file, intraday file, stale
    marker and rebase sidecar (``os.stat``: size + mtime_ns). Cheap, and it moves the moment a file is repaired,
    rewritten or newly defective."""
    folder = cib.ohlcv_cache_dir(provider)
    h = hashlib.sha1()
    for s in sorted(symbols):
        daily, intra = cib.symbol_files(s, interval, folder)
        h.update(repr((s, _stat_id(daily), _stat_id(intra),
                       _stat_id(split_basis.intraday_stale_marker_path(intra)) if intra else None,
                       _stat_id(split_basis.intraday_rebase_path(intra)) if intra else None)).encode())
    return h.hexdigest()


def _job_key(symbols: Sequence[str], interval: str, start: Any, end: Any, provider: str) -> tuple:
    digest = hashlib.sha1(",".join(sorted(str(s).upper() for s in symbols)).encode()).hexdigest()
    return (provider, interval, str(pd.Timestamp(start)), str(pd.Timestamp(end)), digest,
            _state_digest([str(s).upper() for s in symbols], interval, provider))


def require_for_config(config: Dict[str, Any], *, workers: int = 1,
                       provider: Optional[str] = None) -> Optional[PreflightReport]:
    """The job-start gate: REFUSES (raises ``IntradayBasisMismatch`` / ``IntradayBasisStale``) when a
    symbol of the job's universe fails the basis check over the window the job reads
    (``cross_interval_basis.judged_window``). Returns the report (also when it only carries unjudged symbols) or
    None when the job reads no intraday bar. Computed once per process per (universe, window, interval, file
    identities): a repaired or rewritten file is re-judged at the next call, an untouched universe is not.
    ``provider`` is the cache provider the job reads (default: the registry's backtest vendor)."""
    interval = interval_to_check(config)
    if interval is None:
        return None
    provider = provider or default_provider_name()
    symbols = list(config["enabled_instruments"])
    # a launcher config carries ISO strings, a handler config datetimes
    start, end = cib.judged_window(config["start_date"], config["end_date"], int(config["warmup_days"]))
    key = _job_key(symbols, interval, start, end, provider)
    with _JOB_LOCK:
        rep = _JOB_MEMO.get(key)
    if rep is None:
        rep = scan(symbols, interval, start, end, workers=workers, provider=provider, job_kind=job_kind_of(config))
        with _JOB_LOCK:
            _JOB_MEMO[key] = rep
            if len(_JOB_MEMO) > 64:                       # old keys of a repaired universe: bounded
                _JOB_MEMO.pop(next(iter(_JOB_MEMO)))
        if rep.unjudged:
            logger.warning(
                "intraday basis preflight: %d of %d symbols could NOT be judged (%s): %s", len(rep.unjudged),
                rep.symbols, ", ".join(f"{k}={v}" for k, v in sorted(rep.counts.items())),
                ", ".join(f"{r.symbol}[{r.klass}]" for r in rep.unjudged[:30]))
        if rep.option_uncovered:
            logger.warning(
                "intraday basis preflight (OPTIONS job): %d underlyings have no/too thin %s bars, so the intraday "
                "drawdown refinement SKIPS them (recorded in the result): %s", len(rep.option_uncovered), interval,
                ", ".join(f"{r.symbol}[{r.klass}]" for r in rep.option_uncovered[:30]))
        if rep.rebased:
            logger.warning("intraday basis preflight: %d universe symbols run on REBASED intraday prices "
                           "(volume unadjusted: %s)", len(rep.rebased),
                           ", ".join(r["symbol"] for r in rep.rebased if r.get("volume_unadjusted")) or "none")
    excluded = config.get("intraday_basis_exclusions")
    if excluded:
        rep.excluded_listed = list(excluded)
    rep.raise_if_refused()
    return rep


def reset_job_memo() -> None:
    """Tests only."""
    with _JOB_LOCK:
        _JOB_MEMO.clear()
