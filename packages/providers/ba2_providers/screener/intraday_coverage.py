"""Launch-time report: per-symbol-day INTRADAY coverage of the symbols a screened intraday job trades.

Of the symbol-days with a daily bar in the job window, the share without any intraday bar must be at most
``MAX_MISSING_COVERAGE`` (5 %; 1.7 % was measured on a 2023 sample, and 8 panel symbols have no 5-minute file at all).  A
candidate without an intraday price is dropped by the screener gate (counted, never an error), so a thin cache silently
shrinks the screen: the launch REFUSES above the threshold and prints the worst symbols either way.

The PRICE-BASIS cross-check (intraday vs daily prices) is NOT here: it is the shared cross-interval basis function of
``fix/ohlcv-cross-interval-basis``.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

MAX_MISSING_COVERAGE = 0.05
MIN_DAILY_SESSIONS = 20          # a symbol with fewer daily sessions in the window is not ranked among the worst


def _path(cache_dir: str, sym: str, interval: str) -> Optional[str]:
    for c in (sym, sym.replace("-", "_"), sym.replace("-", ".")):
        p = os.path.join(cache_dir, f"{c}_{interval}.parquet")
        if os.path.exists(p):
            return p
    return None


def _sessions(path: str, lo: int, hi: int) -> np.ndarray:
    import pyarrow.parquet as pq
    t = pq.read_table(path, columns=["Date"])
    d = t.column("Date").to_numpy(zero_copy_only=False).astype("datetime64[D]").astype(np.int64) + 719163
    return np.unique(d[(d >= lo) & (d <= hi)])


def _one(sym: str, cache_dir: str, interval: str, lo: int, hi: int) -> Dict[str, Any]:
    pd_, pi_ = _path(cache_dir, sym, "1d"), _path(cache_dir, sym, interval)
    dd = _sessions(pd_, lo, hi) if pd_ else np.zeros(0, dtype=np.int64)
    if pi_ is None:
        return {"symbol": sym, "daily": int(dd.size), "covered": 0, "no_file": True}
    ii = _sessions(pi_, lo, hi)
    return {"symbol": sym, "daily": int(dd.size), "covered": int(np.intersect1d(dd, ii).size), "no_file": False}


def scan(symbols: Iterable[str], cache_dir: str, interval: str, start: str, end: str, *, workers: int = 8) -> Dict[str, Any]:
    lo, hi = date.fromisoformat(start[:10]).toordinal(), date.fromisoformat(end[:10]).toordinal()
    syms = sorted(set(symbols))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(lambda s: _one(s, cache_dir, interval, lo, hi), syms))
    tot, cov = sum(r["daily"] for r in rows), sum(r["covered"] for r in rows)
    worst = sorted((r for r in rows if r["daily"] >= MIN_DAILY_SESSIONS), key=lambda r: r["covered"] / r["daily"])[:15]
    return {"interval": interval, "start": start, "end": end, "n_symbols": len(syms),
            "no_intraday_file": [r["symbol"] for r in rows if r["no_file"]], "daily_sessions": tot, "covered_sessions": cov,
            "missing_coverage_share": (1.0 - cov / tot) if tot else 0.0,
            "worst_coverage": [{"symbol": r["symbol"], "covered": r["covered"], "daily": r["daily"]} for r in worst]}


def problems(report: Dict[str, Any]) -> List[str]:
    if report["missing_coverage_share"] > MAX_MISSING_COVERAGE:
        return [f"{report['missing_coverage_share']:.1%} of the symbol-days with a daily bar have no {report['interval']} bar "
                f"(limit {MAX_MISSING_COVERAGE:.0%}); worst: {report['worst_coverage'][:8]}"]
    return []


def format_report(report: Dict[str, Any]) -> str:
    worst = [(w["symbol"], "%d/%d" % (w["covered"], w["daily"])) for w in report["worst_coverage"][:5]]
    return ("intraday coverage %s %s..%s: %d symbols, %d without an intraday file, %.1f%% of %d symbol-days covered "
            "(missing %.1f%%, limit %.0f%%); worst %s"
            % (report["interval"], report["start"], report["end"], report["n_symbols"], len(report["no_intraday_file"]),
               100 * (1 - report["missing_coverage_share"]), report["daily_sessions"], 100 * report["missing_coverage_share"],
               100 * MAX_MISSING_COVERAGE, worst))
