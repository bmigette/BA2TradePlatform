"""Prewarm (and garbage-collect) the PER-HOST derived ``.npy`` cache that GA workers map.

WHY THIS IS A PRECONDITION FOR A COLD GRID, NOT AN OPTIMISATION
---------------------------------------------------------------
``ba2_common.core.shared_arrays.build_or_open`` serialises cold builders per KEY. There is no
host-wide cap. So 24 workers that start cold on 24 DIFFERENT underlyings run 24 concurrent
builds, and a build's transient peak is ~2.3x the pandas frame it parses (measured 2026-09-14:
~835 B/row -- TastyTrade TSLA at 1.48M rows peaks 1.24 GB; ThetaData TSLA at 7.6M rows is
~7-8 GB). That is 150-190 GB of transient on remote227 and 12-42 GB on a 32 GB / 6-worker box:
the very OOM the shared-array work exists to remove, merely relocated to cold start. Workers
that DO collide on one key instead serialise into a 10-20 minute cold start with 23 of them
idling.

Run this to completion, at ``--jobs 3-4``, on EVERY host that will run trials, BEFORE launching
a cold grid. A second run must report "0 built / N opened" -- that, not the first run's exit
code, is the signal that the host is warm.

OHLCV builds are not the concern (~4 MB transient per symbol); they are here so one command
warms everything a run touches. The precondition above is about the OPTION tree.

NOTHING CALLS ``sweep()`` AT RUNTIME. This tool is the only collector in the system, and
``--sweep`` is the only thing that ever removes an obsolete KEY:

  * ``sweep()`` collects superseded signatures WITHIN a key, marker-less orphans and dead
    ``.tmp`` dirs. It cannot collect a key, because from inside the store "nothing asks for this
    key any more" is indistinguishable from "nothing has asked yet on this host".
  * A KEY goes obsolete two ways this tool CAN judge. (1) A consumer's ``ARRAYS_VERSION`` bump
    moves every key, so every ``u_<SYM>.v<old>`` / ``…_v<old>_<winsig>`` directory becomes a key
    nothing will ever ask for again. (2) The OHLCV key carries the WINDOW, so a long-lived
    worker accumulates one full set (~48 B/bar; 5.6 GB for a 116M-bar band) per window it has
    ever run, and nothing removes the windows that are done with.

So: schedule ``--sweep`` between grids on every worker host. Derived sets are per-host and are
never synced (``cache_sync`` skips ``_derived`` entirely), so this is per-host housekeeping.

NEVER RE-WARM A TREE MID-GRID ON WINDOWS. Running workers keep the old set MAPPED; NTFS then
refuses to evict it and both sets occupy the disk until the grid exits. Eviction here is
all-or-nothing (``DerivedArrayStore.evict_key`` -> ``_evict_dir``): a directory holding a mapped
file is left byte-for-byte intact and reported as skipped, never half-deleted.

``BA2_SHARED_ARRAYS=1`` is FORCED for the duration of this process: with the escape hatch on,
``build_or_open`` returns private arrays and writes nothing, so a prewarm run under an ambient
``BA2_SHARED_ARRAYS=0`` would burn an hour and warm nothing.

Usage
-----
    python tools/build_shared_arrays.py --options-store thetadata \\
        --universe-file tools/options_universe_top100.txt --jobs 4
    python tools/build_shared_arrays.py --ohlcv-provider FMPOHLCVProvider --interval 1d \\
        --symbols AAPL,MSFT --start 2020-01-01 --end 2025-12-31 --warmup-days 60
    python tools/build_shared_arrays.py --options-store thetadata --sweep --sweep-max-age-days 14

Exits non-zero if any symbol's build raised (the others still run, and the exception is printed
per symbol) -- a partially warm tree must not look like a warm one.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

#: repo root: this file is <repo>/tools/build_shared_arrays.py
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Options keys are ``u_<SYM>.v<n>``; OHLCV keys are
#: ``u_<SYM>_<interval>_<from>_<to>_v<n>_<winsig>``. Each root is swept with ITS OWN consumer's
#: spelling and version -- sweeping one root with the other's rule would either spare garbage or
#: delete a live set.
_OPTIONS_KEY_VERSION = re.compile(r"\.v(\d+)$")
_OHLCV_KEY_VERSION = re.compile(r"_v(\d+)_[0-9a-f]+$")

#: One symbol's outcome: (symbol, rows, status, seconds, error). status is one of
#: built / opened / empty / error.
Result = Tuple[str, int, str, float, Optional[str]]

_BOOTSTRAPPED = False


# --------------------------------------------------------------------------------------------
# Bootstrap. Every POOL CHILD runs this too (spawn re-imports this module and gets nothing from
# the parent), which is why it is a function and not module-level code.
# --------------------------------------------------------------------------------------------
def _bootstrap() -> None:
    """Put the backend on the path and silence logging, once per process.

    ``logging.disable`` comes BEFORE the heavy imports and is deliberately skipped when the
    backend is ALREADY importable -- that case is pytest (or a second call), where disabling the
    root logger would reach out of this tool and into the rest of the session. A standalone run
    is the one that needs it: a direct backtest/reader call that keeps logging is 10x+ slower
    (memory ``standalone-backtest-scripts-need-logging-disable``).
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True
    import importlib.util

    try:
        already = importlib.util.find_spec("app.models.database") is not None
    except (ImportError, ValueError):
        already = False
    if already:
        return
    import logging

    logging.disable(logging.WARNING)
    sys.path.insert(0, os.path.join(REPO, "testplatform"))
    import ba2test_launcher as L  # noqa: E402

    L._enter_backend()


# --------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------
def _symbols(universe_file: Optional[str], symbols: Optional[str]) -> List[str]:
    """The symbol list, from a universe file (one per line, ``#`` comments) or ``--symbols``.

    Upper-cased and de-duplicated in order: the parquet tree keys on the upper-cased symbol, and
    a universe file that lists one symbol twice must not build it twice.
    """
    raw: List[str] = []
    if universe_file:
        for line in Path(universe_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                raw.append(line)
    if symbols:
        raw += [s.strip() for s in symbols.split(",") if s.strip()]
    out: List[str] = []
    for s in raw:
        s = s.upper()
        if s not in out:
            out.append(s)
    return out


def _ohlcv_inner(provider_class_name: str):
    """The RAW OHLCV provider the backtest wraps, resolved from its CLASS name.

    The class name is the identity that matters here: the native cache directory is
    ``CACHE_FOLDER/<ProviderClassName>/`` and therefore so is the derived root. Resolved through
    ``get_provider`` (never constructed directly) so this is the same instance
    ``daily_backtest_handler`` builds.
    """
    _bootstrap()
    from ba2_providers import OHLCV_PROVIDERS, get_provider

    for key, cls in OHLCV_PROVIDERS.items():
        if cls.__name__ == provider_class_name:
            return get_provider("ohlcv", key)
    known = ", ".join(sorted(c.__name__ for c in OHLCV_PROVIDERS.values()))
    raise SystemExit(f"Unknown OHLCV provider class {provider_class_name!r}. Known: {known}")


def _dir_bytes(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} TB"


# --------------------------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------------------------
def _option_key(symbol: str) -> str:
    """The derived-cache key ``_load_raw_underlying`` uses. Mirrored here for REPORTING only --
    the build itself goes through the reader, so the two cannot disagree about what is built."""
    _bootstrap()
    from app.services.backtest.parquet_options_provider import _RawUnderlying

    return f"u_{symbol.upper()}.v{_RawUnderlying.ARRAYS_VERSION}"


def build_option_symbol(root: str, symbol: str, dry_run: bool = False) -> Result:
    """Build-or-open one underlying's arrays. Runs in the parent (``--jobs 1``) or a pool child.

    Never raises: a corrupt partition on one of 98 underlyings must not abandon the other 97.
    The exception travels back in the result and the run exits non-zero.
    """
    _bootstrap()
    t0 = time.monotonic()
    try:
        from ba2_common.core import shared_arrays as SA
        from ba2_providers.options.parquet_store import OptionHistoryParquetStore
        from app.services.backtest import parquet_options_provider as pq

        store = OptionHistoryParquetStore(root=root)
        parts = store.partition_paths(symbol)
        if not parts:
            # No partitions is a COVERAGE fact (the warm-up has not reached this underlying),
            # not a failure: nothing is written and nothing can be.
            return (symbol, 0, "empty", time.monotonic() - t0, None)
        derived = SA.DerivedArrayStore(SA.derived_root_for(root))
        # built vs opened, decided BEFORE the call: the marker for the CURRENT signature is the
        # only thing that distinguishes "this run paid for it" from "it was already here".
        existed = (derived.current_dir(_option_key(symbol), parts) / SA.DONE_MARKER).is_file()
        if dry_run:
            return (symbol, 0, "would open" if existed else "would build",
                    time.monotonic() - t0, None)
        raw = pq._load_raw_underlying(root, symbol)
        return (symbol, int(raw.n_rows), "opened" if existed else "built",
                time.monotonic() - t0, None)
    except Exception as exc:  # noqa: BLE001 -- reported per symbol, see the docstring
        return (symbol, 0, "error", time.monotonic() - t0, f"{type(exc).__name__}: {exc}")


def build_options(root: str, symbols: List[str], jobs: int, dry_run: bool) -> List[Result]:
    """Every symbol through ``build_option_symbol``, at most ``jobs`` at a time.

    ``--jobs 1`` runs IN THIS PROCESS (no pool): it is the honest single-builder case, it is what
    the tests drive, and spawning one child to do one thing serially only adds an interpreter
    start per symbol. Above that it is a spawn pool -- never fork: the children must not inherit
    a parent that has already mapped arrays or opened a DB handle.
    """
    if jobs <= 1 or dry_run:
        return [build_option_symbol(root, s, dry_run) for s in symbols]
    import multiprocessing as mp

    with ProcessPoolExecutor(max_workers=jobs, mp_context=mp.get_context("spawn")) as pool:
        return list(pool.map(build_option_symbol, [root] * len(symbols), symbols))


# --------------------------------------------------------------------------------------------
# OHLCV
# --------------------------------------------------------------------------------------------
def _ohlcv_derived_root(provider_class_name: str) -> Path:
    _bootstrap()
    from ba2_common.core import shared_arrays as SA
    import ba2_common.config as cfg

    return Path(SA.derived_root_for(os.path.join(str(cfg.CACHE_FOLDER), provider_class_name)))


def _ohlcv_key_prefix(symbol: str, interval: str, fetch_start: datetime,
                      end: datetime) -> str:
    """The key ``AsOfPriceSource._shared_or_private_arrays`` builds, minus its window hash.

    Mirrored here for REPORTING only (built vs opened is read off the derived directory before
    and after the preload). The hash suffix is left off deliberately: it is a function of the
    exact ISO timestamps, and this only needs to identify THIS symbol at THIS window.
    """
    _bootstrap()
    from app.services.backtest.price_source import ARRAYS_VERSION

    return (f"u_{symbol.upper()}_{interval}_{fetch_start.isoformat()[:10]}"
            f"_{end.isoformat()[:10]}_v{ARRAYS_VERSION}_")


def _published_keys(derived_root: Path, prefix: str) -> bool:
    """True if a key starting with ``prefix`` holds a PUBLISHED signature (one with a marker)."""
    _bootstrap()
    from ba2_common.core import shared_arrays as SA

    try:
        children = list(derived_root.iterdir())
    except OSError:
        return False
    for key_dir in children:
        if not key_dir.name.startswith(prefix):
            continue
        try:
            if any((sig / SA.DONE_MARKER).is_file() for sig in key_dir.iterdir() if sig.is_dir()):
                return True
        except OSError:
            continue
    return False


def build_ohlcv(provider_class_name: str, symbols: List[str], interval: str, start: datetime,
                end: datetime, warmup_days: int, dry_run: bool) -> Tuple[List[Result], List[str]]:
    """One ``AsOfPriceSource.preload`` for the whole universe, exactly as a trial runs it.

    ONE CALL, IN THE PARENT, NO POOL. A bar build's transient is ~4 MB per symbol -- the
    per-key OOM argument that governs the option path does not apply -- and preload is the only
    place that decides the window, the warmup and the missing-symbol tolerance. Re-implementing
    any of that here would build entries a trial then declines to open.

    Returns (per-symbol results, symbols preload DROPPED under its tolerance rules). A miss
    beyond that tolerance raises ``BacktestCacheMiss`` out of preload, which is reported as an
    error for every symbol not yet built and exits the run non-zero -- a grid on that universe
    would fail the same way.
    """
    _bootstrap()
    from app.services.backtest.price_source import (AsOfPriceSource, MemoizedOHLCVProvider,
                                                    clear_worker_bar_cache)

    fetch_start = start - timedelta(days=warmup_days)
    derived_root = _ohlcv_derived_root(provider_class_name)
    before = {s: _published_keys(derived_root, _ohlcv_key_prefix(s, interval, fetch_start, end))
              for s in symbols}
    if dry_run:
        return ([(s, 0, "would open" if before[s] else "would build", 0.0, None)
                 for s in symbols], [])

    inner = _ohlcv_inner(provider_class_name)
    # The construction daily_backtest_handler.py does per run: cached_only=True (a backtest is
    # HERMETIC -- bars come from the on-disk cache or the run fails loudly), bounds = the widest
    # window the run needs, and the price source over it.
    ohlcv = MemoizedOHLCVProvider(inner, fetch_start, end, interval=interval, cached_only=True)
    src = AsOfPriceSource(ohlcv_provider=ohlcv, interval=interval)
    # This process's own bar cache would serve a second preload from memory and report every
    # symbol "opened" without touching the derived store at all.
    clear_worker_bar_cache()
    t0 = time.monotonic()
    error: Optional[str] = None
    try:
        src.preload(symbols, start, end, warmup_days=warmup_days)
    except Exception as exc:  # noqa: BLE001 -- reported per symbol; see the docstring
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - t0
    dropped = list(getattr(src, "_dropped_symbols", []))
    results: List[Result] = []
    for s in symbols:
        published = _published_keys(derived_root, _ohlcv_key_prefix(s, interval, fetch_start, end))
        if published:
            results.append((s, 0, "opened" if before[s] else "built", elapsed / len(symbols), None))
        elif s in dropped:
            results.append((s, 0, "empty", 0.0, None))
        else:
            results.append((s, 0, "error", 0.0, error or "no derived entry was published"))
    return results, dropped


# --------------------------------------------------------------------------------------------
# Sweep
# --------------------------------------------------------------------------------------------
def sweep_root(label: str, derived_root: Path, version_re, current_version: int,
               max_age_days: float, dry_run: bool) -> int:
    """``sweep()`` then the KEY-level pass over one derived root. Returns keys removed.

    Two passes because they answer different questions. ``sweep()`` is the store's own: within a
    key, which signature is superseded, which directory is an orphan, which ``.tmp`` is dead.
    The key pass is this tool's: which whole KEY is obsolete -- below the consumer's current
    ``ARRAYS_VERSION`` (an orphan by construction: nothing will ever ask for that key again), or
    untouched for ``max_age_days`` (a window nothing runs any more).

    Eviction goes through ``evict_key`` -> ``_evict_dir``: a key holding a file another process
    still maps is left byte-for-byte intact and counted as skipped. That is expected and benign
    on a host with a grid running -- the next sweep gets it.
    """
    _bootstrap()
    from ba2_common.core import shared_arrays as SA

    if not derived_root.is_dir():
        print(f"[{label}] sweep: nothing at {derived_root}")
        return 0
    store = SA.DerivedArrayStore(derived_root)
    swept = 0 if dry_run else store.sweep()
    now = time.time()
    removed = reclaimed = 0
    skipped: List[str] = []
    would: List[str] = []
    for key_dir in sorted(derived_root.iterdir()):
        try:
            if not key_dir.is_dir():
                continue
            sigs = [d for d in key_dir.iterdir() if d.is_dir()]
        except OSError:
            continue
        m = version_re.search(key_dir.name)
        version = int(m.group(1)) if m else None
        mtimes = []
        for sig in sigs:
            try:
                mtimes.append((sig / SA.DONE_MARKER).stat().st_mtime)
            except OSError:
                pass
        newest = max(mtimes) if mtimes else None
        if version is not None and version < current_version:
            reason = f"ARRAYS_VERSION v{version} < v{current_version}"
        elif newest is not None and now - newest > max_age_days * 86400:
            reason = f"unused for {(now - newest) / 86400:.1f}d"
        else:
            # A key with no published signature at all is left alone ON PURPOSE: sweep() above
            # has already collected whatever was inside it, and an empty key directory costs
            # nothing. Removing it would race a builder that has just created it.
            continue
        size = _dir_bytes(key_dir)
        if dry_run:
            would.append(f"{key_dir.name} ({reason}, {_human(size)})")
            continue
        if store.evict_key(key_dir):
            removed += 1
            reclaimed += size
        else:
            skipped.append(key_dir.name)
    if dry_run:
        print(f"[{label}] DRY RUN sweep: would run sweep() and remove {len(would)} key(s)")
        for w in would:
            print(f"    would remove {w}")
        return 0
    print(f"[{label}] sweep: {swept} superseded/orphan dir(s), {removed} key(s) removed, "
          f"{_human(reclaimed)} reclaimed, {len(skipped)} skipped (still mapped)")
    for name in skipped:
        print(f"    skipped {name} (a process on this host still maps a file inside it)")
    return removed


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def _report(label: str, results: List[Result], derived_root: Path, dry_run: bool) -> int:
    """Print one line per symbol plus the summary. Returns the number of errors."""
    total_s = 0.0
    counts = {"built": 0, "opened": 0, "empty": 0, "error": 0,
              "would build": 0, "would open": 0}
    for sym, rows, status, seconds, error in results:
        total_s += seconds
        counts[status] = counts.get(status, 0) + 1
        detail = f" {rows:,} rows" if rows else ""
        line = f"[{label}] {sym:<8} {status}{detail} {seconds:.1f}s"
        print(line if error is None else f"{line}  !! {error}")
    built = counts["built"] + counts["would build"]
    opened = counts["opened"] + counts["would open"]
    prefix = "DRY RUN " if dry_run else ""
    size = "" if dry_run else f", derived {_human(_dir_bytes(derived_root))}"
    errors = counts["error"]
    err = f", {errors} error(s)" if errors else ""
    print(f"[{label}] {prefix}{built} built / {opened} opened / {counts['empty']} empty{err}, "
          f"{total_s:.1f}s{size} at {derived_root}")
    return errors


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="build_shared_arrays.py",
        description="Prewarm and garbage-collect the per-host derived .npy array cache.")
    p.add_argument("--options-store", choices=["thetadata", "tastytrade", "sqlite", "parquet"],
                   help="Option parquet tree to warm ('parquet' is the tastytrade alias).")
    p.add_argument("--universe-file", help="One symbol per line; '#' starts a comment.")
    p.add_argument("--symbols", help="Comma-separated symbols (combined with --universe-file).")
    p.add_argument("--ohlcv-provider",
                   help="OHLCV provider CLASS name, e.g. FMPOHLCVProvider (it names the cache "
                        "directory and therefore the derived root).")
    p.add_argument("--interval", default="1d")
    p.add_argument("--start", help="Backtest start date, YYYY-MM-DD.")
    p.add_argument("--end", help="Backtest end date, YYYY-MM-DD.")
    p.add_argument("--warmup-days", type=int, default=60)
    p.add_argument("--jobs", type=int, default=4,
                   help="Concurrent option builds. KEEP IT AT 3-4: a build peaks at ~2.3x the "
                        "frame it parses (~7-8 GB for ThetaData TSLA).")
    p.add_argument("--sweep", action="store_true",
                   help="Also collect superseded signatures and obsolete KEYS (stale "
                        "ARRAYS_VERSION, or unused for --sweep-max-age-days).")
    p.add_argument("--sweep-max-age-days", type=float, default=14.0)
    p.add_argument("--dry-run", action="store_true", help="Print what would happen; touch nothing.")
    args = p.parse_args(argv)

    if args.options_store in ("sqlite",):
        p.error("--options-store sqlite has no parquet tree and therefore no derived array "
                "cache; there is nothing to prewarm or sweep. Choose thetadata or tastytrade.")
    if not args.options_store and not args.ohlcv_provider:
        p.error("nothing to do: pass --options-store and/or --ohlcv-provider.")
    building = bool(_symbols(args.universe_file, args.symbols))
    if not building and not args.sweep:
        p.error("no symbols: pass --universe-file or --symbols (or --sweep to only collect).")
    if building and args.ohlcv_provider and not (args.start and args.end):
        p.error("--ohlcv-provider needs --start and --end (the window is part of the cache key).")
    if args.jobs < 1:
        p.error("--jobs must be >= 1")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse(argv)
    # The escape hatch makes build_or_open return private arrays and write NOTHING; a prewarm
    # under it would run for an hour and warm nothing at all. Set before any build, and
    # inherited by the pool children.
    os.environ["BA2_SHARED_ARRAYS"] = "1"
    _bootstrap()
    from ba2_common.core import shared_arrays as SA

    symbols = _symbols(args.universe_file, args.symbols)
    errors = 0

    options_root = None
    if args.options_store:
        from app.services.backtest.options_store import default_options_parquet_root

        options_root = default_options_parquet_root(args.options_store)
        if symbols:
            derived = Path(SA.derived_root_for(options_root))
            print(f"[options:{args.options_store}] {len(symbols)} symbol(s) from {options_root} "
                  f"at --jobs {args.jobs}")
            results = build_options(options_root, symbols, args.jobs, args.dry_run)
            errors += _report(f"options:{args.options_store}", results, derived, args.dry_run)

    if args.ohlcv_provider and symbols:
        start = datetime.fromisoformat(args.start)
        end = datetime.fromisoformat(args.end)
        results, dropped = build_ohlcv(args.ohlcv_provider, symbols, args.interval, start, end,
                                       args.warmup_days, args.dry_run)
        if dropped:
            print(f"[ohlcv:{args.ohlcv_provider}] preload DROPPED {len(dropped)} uncached "
                  f"symbol(s) under its missing-symbol tolerance: {', '.join(dropped)}")
        errors += _report(f"ohlcv:{args.ohlcv_provider}", results,
                          _ohlcv_derived_root(args.ohlcv_provider), args.dry_run)

    if args.sweep:
        from app.services.backtest.parquet_options_provider import _RawUnderlying
        from app.services.backtest.price_source import ARRAYS_VERSION as BAR_ARRAYS_VERSION

        if options_root is not None:
            sweep_root(f"options:{args.options_store}", Path(SA.derived_root_for(options_root)),
                       _OPTIONS_KEY_VERSION, _RawUnderlying.ARRAYS_VERSION,
                       args.sweep_max_age_days, args.dry_run)
        if args.ohlcv_provider:
            sweep_root(f"ohlcv:{args.ohlcv_provider}", _ohlcv_derived_root(args.ohlcv_provider),
                       _OHLCV_KEY_VERSION, BAR_ARRAYS_VERSION, args.sweep_max_age_days,
                       args.dry_run)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
