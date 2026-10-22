"""The run's split basis: adjusted (FMP) equity closes -> the AS-TRADED basis of option strikes.

BT/live option parity, ``docs/plans/2026-09-22-bt-live-option-parity.md`` Part E.

THE FINDING. Every option store (ThetaData, TastyTrade, the Alpaca sqlite) holds strikes and
premiums AS TRADED; the FMP daily cache a backtest prices its underlyings from is
BACK-ADJUSTED for every split up to its fetch. NFLX on 2024-05-01: chain put-call-parity spot
$553, FMP close $55.17. Live is unaffected (Alpaca's spot and strikes are both as traded), so
this was a BT/live parity break on 31 of the 97 stage-1 symbols.

THE RULE. The option path works in the as-traded basis on both paths. The backtest's equity
book stays split-adjusted, exactly as every equity backtest has always run -- NOTHING in this
module is consulted by an equity-only run. Conversion happens only on the option path and at
the option <-> stock boundary (assignment, cover), through ``BacktestAccount``.

WHAT THIS MODULE DOES. Builds, ONCE per run (memoised per worker process), one
``SymbolSplitBasis`` per universe symbol from what is ON DISK -- never the network:

  * the split calendar: ``CACHE_FOLDER/fmp_history/mc_stock_split__<SYM>.json``, the payload
    the market-condition warmup caches (``ba2_providers.market_conditions.fmp_source``).
    Missing -> REFUSE, naming every missing symbol and the command that fetches them;
  * the adjustment basis: the FMP daily parquet the run's price source reads, verified per
    split by ``split_basis.check_split_basis`` (see ``resolve_symbol_split_basis``). A mixed
    or unprovable basis -> REFUSE.

A factor of 1 is NEVER assumed for a symbol that could not be resolved: that is the exact
silent 10x/40x mis-strike this exists to remove.

THE ALPACA SOURCE (plan ``2026-10-20-alpaca-history-source`` Task 12). The ``alpaca`` history
cache is built by us from RAW Alpaca bars, back-adjusted with our own verified split table
(``alpaca_history.build.adjust``): prices are divided by the product of the factors of EVERY
accepted split-table event dated after the bar -- splits, reverse splits, unit splits, stock
dividends AND spin-offs (the table's ``spinoffs``; apart from volume they scale prices exactly
like splits). The as-traded factor of a day is therefore that same product, so
``adjusted_close x factor`` reproduces the raw close. ``build_alpaca_run_split_basis`` derives it
from what the BUILD recorded (the build manifest's per-symbol ``events``, cross-checked against
the split table the build read), with no FMP calendar, FMP overrides or FMP-file proof on this
path. A symbol the manifest lists with zero applied events has a PROVEN factor of 1; a symbol the
manifest or table does not know, or an uncertified cache, REFUSES.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ba2_common.core.split_basis import (
    CalendarSplit, SplitBasisRefused, SymbolSplitBasis, read_full_fetch_marker,
    resolve_symbol_split_basis,
)
from ba2_common.core.split_basis_overrides import overrides_for, overrides_version_for

#: The warmup's own namespace, imported rather than retyped (a hand-copied cache key is how a
#: reader drifts from its writer).
from ba2_providers.market_conditions.fmp_source import (
    DIVIDEND_CALENDAR_NAMESPACE, SPLIT_CALENDAR_NAMESPACE,
)

__all__ = ["RunSplitBasis", "build_run_split_basis", "build_alpaca_run_split_basis", "split_calendar_path",
           "load_cached_split_calendar", "clear_split_basis_memo", "dividend_calendar_path",
           "load_cached_dividend_calendar"]

_WARM_HINT = ("Fetch the calendars once (network) with the market-condition warmup's plan step, "
              "e.g. `python tools/warm_market_conditions.py plan --universe <SYMS> ...`, which "
              "caches CACHE_FOLDER/fmp_history/mc_stock_split__<SYM>.json for every symbol it "
              "plans; a backtest itself never fetches.")


def split_calendar_path(symbol: str) -> str:
    import ba2_common.config as cfg  # read at call time so tests that rebind CACHE_FOLDER win
    return os.path.join(cfg.CACHE_FOLDER, "fmp_history",
                        f"{SPLIT_CALENDAR_NAMESPACE}__{symbol.upper()}.json")


def load_cached_split_calendar(symbol: str) -> Optional[List[CalendarSplit]]:
    """The cached FMP split calendar for ``symbol`` (date-ascending), or None when absent.

    An unreadable file RAISES (a corrupt calendar is not an absent one). A row whose ratio
    FMP does not state is kept with ``ratio=nan``: ``as_traded_factor`` then refuses a day
    whose window crosses it, instead of that split silently vanishing."""
    from ba2_providers import symbol_info

    path = split_calendar_path(symbol)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return [CalendarSplit(e.date, float(e.ratio) if e.ratio else float("nan"))
            for e in symbol_info.parse_splits(payload)]


def dividend_calendar_path(symbol: str) -> str:
    import ba2_common.config as cfg  # read at call time so tests that rebind CACHE_FOLDER win
    return os.path.join(cfg.CACHE_FOLDER, "fmp_history",
                        f"{DIVIDEND_CALENDAR_NAMESPACE}__{symbol.upper()}.json")


def load_cached_dividend_calendar(symbol: str
                                  ) -> Optional[List[Tuple[date, Optional[float], Optional[date]]]]:
    """[(ex-date, adjDividend or None, declarationDate or None)] of the cached FMP dividend
    calendar (ex-date ascending), or None when no calendar is cached.

    ``adjDividend`` is split-adjusted like FMP's closes, so a caller converts it with the run's
    as-traded factor exactly as it does the spot. There is NO fallback to the unadjusted
    ``dividend``: multiplying an as-declared amount by a split factor would be wrong by the
    split, so a row without ``adjDividend`` keeps None and the guard refuses to use it, naming
    it. The declaration date lets the guard read the calendar AS OF a session (a dividend not
    yet declared was not known). An unreadable file RAISES (a corrupt calendar is not an absent
    one). Read by the option basis guard's carry separation only."""
    from ba2_providers import symbol_info

    path = dividend_calendar_path(symbol)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    def _day(v):
        try:
            return date.fromisoformat(str(v)[:10]) if v else None
        except ValueError:
            return None

    def _amount(v):
        try:
            a = float(v)
        except (TypeError, ValueError):
            return None
        return a if a == a and a >= 0 else None

    out = []
    for r in symbol_info._historical_rows(payload):
        ex = _day(r.get("date"))
        if ex is None:
            continue
        out.append((ex, _amount(r.get("adjDividend")), _day(r.get("declarationDate"))))
    return sorted(out, key=lambda t: t[0])


class RunSplitBasis:
    """The verified per-symbol adjustment basis of one run's universe.

    ``factor(symbol, day)`` multiplies an adjusted close of ``day`` into the as-traded basis.
    A symbol outside the verified universe REFUSES; it never answers 1."""

    def __init__(self, bases: Dict[str, SymbolSplitBasis]):
        self._bases = {k.upper(): v for k, v in bases.items()}

    def factor(self, symbol: str, day: date) -> float:
        b = self._bases.get(str(symbol).upper())
        if b is None:
            raise SplitBasisRefused(
                f"{symbol}: not in this run's verified split basis (universe "
                f"{sorted(self._bases)[:10]}{'...' if len(self._bases) > 10 else ''}); the "
                f"as-traded factor for {day} is unknown")
        return b.factor(day)

    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted(self._bases))

    def basis_of(self, symbol: str) -> Optional[SymbolSplitBasis]:
        return self._bases.get(str(symbol).upper())

    def digest(self) -> str:
        """What every factor this object answers is a pure function of -- for cache keys over
        as-traded values (the parquet reader's greeks overlay)."""
        blob = repr(tuple(self._bases[s].identity() for s in sorted(self._bases)))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


#: (symbol, parquet path, parquet mtime_ns, parquet size, calendar mtime_ns, marker, overrides) ->
#: SymbolSplitBasis. A GA worker resolves each symbol once, not once per trial; a re-fetched
#: file or calendar changes the key, so a stale basis is never served.
_MEMO: Dict[tuple, SymbolSplitBasis] = {}
_MEMO_LOCK = threading.Lock()


def clear_split_basis_memo() -> None:
    with _MEMO_LOCK:
        _MEMO.clear()


def _stat_key(path: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    if not path or not os.path.exists(path):
        return (None, None)
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def _read_daily(path: str):
    import pandas as pd

    df = pd.read_parquet(path, columns=["Date", "Open", "High", "Low", "Close"])
    d = pd.to_datetime(df["Date"])
    if getattr(d.dt, "tz", None) is not None:
        d = d.dt.tz_localize(None)
    days = d.dt.normalize().to_numpy(dtype="datetime64[ns]")
    return days, df["Open"].to_numpy(), df["High"].to_numpy(), df["Low"].to_numpy(), df["Close"].to_numpy()


def _resolve_one(symbol: str, parquet_path: str) -> SymbolSplitBasis:
    cal_path = split_calendar_path(symbol)
    marker = read_full_fetch_marker(parquet_path)
    # The operator overrides (``split_basis_overrides``, plan Part G1b) are part of the basis:
    # an edited override set must not be served from a basis resolved under the old one.
    key = (symbol, os.path.normcase(os.path.abspath(parquet_path)), *_stat_key(parquet_path),
           _stat_key(cal_path)[0], json.dumps(marker, sort_keys=True) if marker else None,
           overrides_version_for(overrides_for(symbol)))
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
    if hit is not None:
        return hit
    splits = load_cached_split_calendar(symbol)
    if splits is None:
        raise SplitBasisRefused(f"{symbol}: no cached split calendar at {cal_path}")
    days, o, h, l, c = _read_daily(parquet_path)
    basis = resolve_symbol_split_basis(symbol, days, o, h, l, c, splits, marker=marker)
    with _MEMO_LOCK:
        _MEMO[key] = basis
    return basis


def _alpaca_symbol(symbol: str) -> str:
    """The cache's spelling: upper-case, '.' written '-' (BRK.B -> BRK-B), like the build."""
    return str(symbol).strip().upper().replace(".", "-")


#: (cache_dir, manifest sha, table stat) -> (manifest, SplitTable): the 8 MB manifest is parsed once per process; a rebuilt
#: cache changes the key.
_ALPACA_MEMO: Dict[tuple, Tuple[dict, Any]] = {}


def _load_alpaca_build(cache_dir: str) -> Tuple[dict, Any, dict]:
    """``(manifest, SplitTable, certificate)`` of a CERTIFIED alpaca cache, else ``SplitBasisRefused``."""
    from ba2_providers.ohlcv.alpaca_history.build import BuildError, load_split_table
    from .history_source import AlpacaCacheNotCertified, verify_alpaca_cache_certified

    try:
        cert = verify_alpaca_cache_certified(cache_dir)
    except AlpacaCacheNotCertified as e:
        raise SplitBasisRefused(f"the as-traded split basis of the alpaca history cache cannot be stated: {e}") from e
    meta = os.path.join(cache_dir, "_meta")
    key = (os.path.normcase(os.path.abspath(cache_dir)), cert["build_manifest_sha256"],
           *_stat_key(os.path.join(meta, "split_table.json")))
    with _MEMO_LOCK:
        hit = _ALPACA_MEMO.get(key)
    if hit is not None:
        return hit[0], hit[1], cert
    try:
        table = load_split_table(cache_dir)
    except (BuildError, OSError, ValueError, KeyError) as e:
        raise SplitBasisRefused(
            f"the alpaca split table under {meta} cannot be read ({e}); the as-traded split basis is unknown") from e
    with open(os.path.join(meta, "build_manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    built = manifest["split_table"]
    if built["sha256"] != table.sha256 or built["run_id"] != table.run_id:
        raise SplitBasisRefused(
            f"the split table {table.run_id} (sha256 {table.sha256[:12]}) is not the one the cache was built with "
            f"({built['run_id']}, {built['sha256'][:12]}): rebuild the cache or restore the table")
    with _MEMO_LOCK:
        _ALPACA_MEMO.clear()
        _ALPACA_MEMO[key] = (manifest, table)
    return manifest, table, cert


def build_alpaca_run_split_basis(symbols: Iterable[str], cache_dir: str, *, interval: str = "1d") -> RunSplitBasis:
    """The as-traded basis of an ``alpaca``-history run, from the verified split table the cache was built with.

    ``cache_dir`` is ``CACHE_FOLDER/AlpacaHistoryOHLCVProvider``. factor(D) = the product of the factors of every
    price-scaling event the build applied dated after D (``build.adjust``: splits AND spin-offs), so adjusted x factor = the
    raw as-traded close. Refuses ALL problems at once: an uncertified cache, a missing/unreadable table, a symbol the manifest
    or the table does not carry, a symbol with no daily file, an event list that disagrees with the one the build recorded."""
    if str(interval) != "1d":
        raise SplitBasisRefused(
            f"an options run prices its underlyings at execution_interval={interval!r}; the as-traded split basis is only "
            f"stated for the daily clock ('1d'). Refusing rather than converting a series whose adjustment nobody checked.")
    manifest, table, cert = _load_alpaca_build(cache_dir)
    msyms = manifest["symbols"]
    version = f"alpaca-split-table:{table.run_id}:{table.sha256[:12]}:build:{cert['build_manifest_sha256'][:12]}"
    asof = date.fromisoformat(table.asof)
    bases: Dict[str, SymbolSplitBasis] = {}
    problems: List[str] = []
    for raw in sorted({str(s).strip().upper() for s in symbols if s and str(s).strip()}):
        sym = _alpaca_symbol(raw)
        entry = msyms[sym] if sym in msyms else None
        if entry is None:
            problems.append(f"{raw}: not in the alpaca build manifest (no history was built for it)")
            continue
        if "1d" not in entry or "events" not in entry:
            problems.append(f"{raw}: the alpaca build has no daily file for it")
            continue
        first = entry["1d"]["first"]
        ts, to = table.events(sym)
        from_table = {"splits": [[d, f] for d, f in ts if d > first], "spinoffs": [[d, f] for d, f in to if d > first]}
        applied = {k: [[d, float(f)] for d, f in entry["events"][k]] for k in ("splits", "spinoffs")}
        if from_table != applied:
            problems.append(f"{raw}: the split table's events {from_table} differ from those the build applied {applied}")
            continue
        events = sorted((date.fromisoformat(d), float(f)) for d, f in applied["splits"] + applied["spinoffs"])
        bad = [e for e in events if not (math.isfinite(e[1]) and e[1] > 0)]
        if bad:
            problems.append(f"{raw}: unusable factor(s) {bad}")
            continue
        bases[raw] = SymbolSplitBasis(
            symbol=raw, splits=tuple(CalendarSplit(d, f) for d, f in events),
            basis_date=max([asof] + [d for d, _ in events]),
            evidence=f"alpaca split table {table.run_id}: {len(events)} applied price event(s)", overrides_version=version)
    if problems:
        raise SplitBasisRefused(
            "Refusing the options run: the as-traded split basis of the alpaca history cache (option strikes are as traded, "
            "the cache is split-adjusted by our verified table) cannot be stated for every symbol -- " + " | ".join(problems))
    return RunSplitBasis(bases)


def build_run_split_basis(symbols: Iterable[str], ohlcv_provider: Any, *,
                          interval: str = "1d", history_source: str = "fmp",
                          cache_dir: Optional[str] = None) -> RunSplitBasis:
    """Resolve the split basis of every symbol a run's option path may price.

    ``ohlcv_provider`` is the run's hermetic OHLCV reader (``MemoizedOHLCVProvider``): its
    ``cached_path`` names the very file the price source's closes come from, so the basis is
    verified on the bytes being converted and nothing else.

    Refuses ALL problems at once (every symbol, every reason) rather than the first, so one
    run tells the operator the whole repair list.

    ``history_source`` selects the basis: ``fmp`` (the default, unchanged) reads the FMP calendar
    and proves the FMP parquet; ``alpaca`` reads the verified split table of ``cache_dir`` (the
    alpaca cache folder) and ignores ``ohlcv_provider``. Any other source refuses."""
    if history_source == "alpaca":
        if not cache_dir:
            raise SplitBasisRefused("history_source='alpaca' needs the alpaca cache folder (cache_dir)")
        return build_alpaca_run_split_basis(symbols, cache_dir, interval=interval)
    if history_source != "fmp":
        raise SplitBasisRefused(f"no as-traded split basis is defined for OHLCV history source {history_source!r}")
    if str(interval) != "1d":
        raise SplitBasisRefused(
            f"an options run prices its underlyings at execution_interval={interval!r}; the "
            f"as-traded split basis is only verified for the daily FMP cache ('1d'). Refusing "
            f"rather than converting a series whose adjustment nobody checked.")
    cached_path = getattr(ohlcv_provider, "cached_path", None)
    if cached_path is None:
        raise SplitBasisRefused(
            f"the run's OHLCV provider ({type(ohlcv_provider).__name__}) names no on-disk file "
            f"(no cached_path), so the adjustment basis of its closes cannot be verified")
    bases: Dict[str, SymbolSplitBasis] = {}
    problems: List[str] = []
    missing_cal: List[str] = []
    for sym in sorted({str(s).strip().upper() for s in symbols if s and str(s).strip()}):
        path = cached_path(sym, interval)
        if not path:
            problems.append(f"{sym}: no cached {interval} OHLCV file to verify")
            continue
        try:
            bases[sym] = _resolve_one(sym, path)
        except SplitBasisRefused as e:
            if "no cached split calendar" in str(e):
                missing_cal.append(sym)
            else:
                problems.append(str(e))
    if missing_cal:
        problems.insert(0, f"no cached split calendar for {len(missing_cal)} symbol(s): "
                           f"{', '.join(missing_cal)}. {_WARM_HINT}")
    if problems:
        raise SplitBasisRefused(
            "Refusing the options run: the as-traded split basis (option strikes are as "
            "traded, the FMP closes are split-adjusted) cannot be stated for every symbol -- "
            + " | ".join(problems))
    return RunSplitBasis(bases)
