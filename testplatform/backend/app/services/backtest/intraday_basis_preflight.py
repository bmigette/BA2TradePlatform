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

COST. The per-symbol work (reading the intraday file and reducing it to one row per session) is memoised
by the two files' identity (size + mtime_ns), in the process AND on disk (``MemoDir``, next to the cache
folder): once per file, ever, until the file changes. On top of that this module keeps a JOB memo: the
verdict for one (universe, window, interval) is computed ONCE per process, so a GA does not even re-stat
the files per trial.

NO ESCAPE HATCH THAT SKIPS. There is no flag that turns the check off. The reviewed way to run a job
without a refused symbol is the one that already exists and is RECORDED on the run: ``--exclude-symbols``
(``excluded_instruments``), which removes the symbol from the universe this module checks. The repair is
``force_full_refetch(symbol, '5min')`` (it replaces the intraday file on the vendor's current basis and
re-fetches the daily file if that is the side that disagrees).

UNJUDGED symbols (too few comparable sessions, no intraday file, no daily file) are not refused here --
a symbol without intraday bars is already a ``BacktestCacheMiss`` of its own, and one with a handful of
sessions cannot distort a result by a basis it cannot show -- but they are NEVER reported as ok: the report
carries them, the launcher prints them, and the job result records them.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from ba2_common.core import native_cache, split_basis
from ba2_common.core.split_basis import IntradayBasisMismatch, IntradayBasisStale
from ba2_providers.ohlcv import cross_interval_basis as cib

logger = logging.getLogger(__name__)

#: The provider whose native cache the backtests read (``get_provider("ohlcv", "fmp")``).
PROVIDER = "FMPOHLCVProvider"
#: The interval an options job's drawdown refinement reads (``results._get_5m_bars_cached``).
OPTION_REFINEMENT_INTERVAL = "5m"
#: A trial result carries at most this many unjudged symbols by name (the count is always exact).
_RECORDED_UNJUDGED_MAX = 100


@dataclass
class PreflightReport:
    interval: str
    window_start: str
    window_end: str
    symbols: int
    mismatched: List[cib.BasisResult] = field(default_factory=list)
    stale: List[Tuple[str, str]] = field(default_factory=list)       # (symbol, marker reason)
    unjudged: List[cib.BasisResult] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        return bool(self.mismatched or self.stale)

    def refusal_message(self) -> str:
        lines = []
        for r in self.mismatched:
            lines.append("  " + r.describe())
        for sym, why in self.stale:
            lines.append(f"  {sym}: stale marker -- {why}")
        return (f"intraday/daily PRICE-LEVEL mismatch in the {self.interval} cache for "
                f"{len(self.mismatched) + len(self.stale)} of {self.symbols} symbols over "
                f"{self.window_start}..{self.window_end} (symbol, factor, class):\n" + "\n".join(lines) + "\n"
                f"A run on these symbols prices decisions from one basis and history from another. "
                f"Repair: force_full_refetch(<symbol>, '5min') per symbol (see "
                f"tools/cache_health_check.py --basis), or run without them via --exclude-symbols "
                f"(recorded on the run).")

    def to_dict(self) -> dict:
        """What a job result records: the verdict counts and every unjudged symbol (never silent)."""
        return {
            "interval": self.interval, "window": [self.window_start, self.window_end],
            "symbols": self.symbols, "counts": dict(self.counts),
            "unjudged": [{"symbol": r.symbol, "class": r.klass, "reason": r.reason}
                         for r in self.unjudged[:_RECORDED_UNJUDGED_MAX]],
            "unjudged_count": len(self.unjudged),
        }

    def raise_if_refused(self) -> None:
        if not self.refused:
            return
        if self.mismatched:
            raise IntradayBasisMismatch(self.refusal_message())
        raise IntradayBasisStale(self.refusal_message())


def scan(symbols: Iterable[str], interval: str, start: Any, end: Any, *, workers: int = 1,
         provider: str = PROVIDER) -> PreflightReport:
    """Judge ``symbols`` over ``[start, end]`` and return the report (never raises on a mismatch)."""
    syms = sorted({str(s).upper() for s in symbols})
    results = cib.check_many(syms, interval, start, end, provider=provider, workers=workers)
    rep = PreflightReport(interval=interval, window_start=str(pd.Timestamp(start).date()),
                          window_end=str(pd.Timestamp(end).date()), symbols=len(syms))
    for r in results:
        rep.counts[r.klass] = rep.counts.get(r.klass, 0) + 1
        if r.mismatched:
            rep.mismatched.append(r)
        elif r.unjudged:
            rep.unjudged.append(r)
        # stale marker: independent of the price verdict (a marker with consistent prices still means
        # "daily was rewritten, intraday not verified" until the file is replaced)
        path = native_cache.find_timeseries_path(provider, r.symbol, interval)
        marker = split_basis.read_intraday_stale(path) if path else None
        if marker is not None:
            rep.stale.append((r.symbol, str(marker.get("reason"))))
    if rep.counts:
        rep.counts["stale_marker"] = len(rep.stale)
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


_JOB_MEMO: Dict[tuple, PreflightReport] = {}
_JOB_LOCK = threading.Lock()


def _job_key(symbols: Sequence[str], interval: str, start: Any, end: Any) -> tuple:
    digest = hashlib.sha1(",".join(sorted(str(s).upper() for s in symbols)).encode()).hexdigest()
    return (PROVIDER, interval, str(pd.Timestamp(start)), str(pd.Timestamp(end)), digest)


def require_for_config(config: Dict[str, Any], *, workers: int = 1) -> Optional[PreflightReport]:
    """The job-start gate: REFUSES (raises ``IntradayBasisMismatch`` / ``IntradayBasisStale``) when a
    symbol of the job's universe fails the basis check over the window the job reads (start - warmup ..
    end). Returns the report (also when it only carries unjudged symbols) or None when the job reads no
    intraday bar. Computed once per process per (universe, window, interval)."""
    interval = interval_to_check(config)
    if interval is None:
        return None
    symbols = list(config["enabled_instruments"])
    # a launcher config carries ISO strings, a handler config datetimes
    start = pd.Timestamp(config["start_date"]) - timedelta(days=int(config["warmup_days"]))
    end = pd.Timestamp(config["end_date"])
    key = _job_key(symbols, interval, start, end)
    with _JOB_LOCK:
        rep = _JOB_MEMO.get(key)
    if rep is None:
        rep = scan(symbols, interval, start, end, workers=workers)
        with _JOB_LOCK:
            _JOB_MEMO[key] = rep
        if rep.unjudged:
            logger.warning(
                "intraday basis preflight: %d of %d symbols could NOT be judged (%s): %s", len(rep.unjudged),
                rep.symbols, ", ".join(f"{k}={v}" for k, v in sorted(rep.counts.items())),
                ", ".join(f"{r.symbol}[{r.klass}]" for r in rep.unjudged[:30]))
    rep.raise_if_refused()
    return rep


def reset_job_memo() -> None:
    """Tests only."""
    with _JOB_LOCK:
        _JOB_MEMO.clear()
