"""As-of-clamped reader over the TastyTrade/dxfeed PARQUET option store.

THE SECOND BACKEND, NOT A SECOND ENGINE. ``HistoricalOptionsProvider`` (options_provider.py)
reads the Alpaca-built ``OptionsHistoryCache`` sqlite. This class reads
``CACHE_FOLDER/TastyTradeOptionsProvider/<SYM>/exp=<YYYY-MM-DD>/<SYM>_<exp>_1d.parquet``
(written by ``tools/warm_options_history.py`` via
``ba2_providers.options.parquet_store.OptionHistoryParquetStore``) and exposes the SAME
methods with the SAME signatures and the SAME as-of clamp, so ``BacktestAccount`` cannot tell
them apart. The engine's contract is exactly:

    get_chain(underlying, as_of, *, expiry_min, expiry_max,
              option_type=None, strike_min=None, strike_max=None) -> List[OptionContract]
    get_quote(occ_symbol, as_of)  -> Optional[OptionQuote]
    get_bar(occ_symbol, as_of)    -> Optional[dict]   # EXACT bar on as_of, else None
    get_atm_iv(underlying, as_of) -> Optional[float]
    delta_at_entry(underlying, occ_symbol, when) -> Optional[float]   # results.py refinement

(the first four enumerated from ``backtest_account.py``: get_option_chain / get_option_quote /
get_atm_implied_volatility / ``_options.get_bar`` at the MTM, liquidation, fill,
round-trip-recorder and expiry-settlement sites; the fifth from
``results._build_refine_drawdown_fn`` — see DELTA-AT-ENTRY below.)

WHICH STORE A RUN READS IS AN EXPLICIT CHOICE THAT DEFAULTS TO SQLITE — see
``options_store.py``. Every backtest number on record was produced against the sqlite path
and nothing here may perturb it.

WHAT THE PARQUET HAS THAT THE SQLITE DOES NOT
  * ``open_interest`` — POPULATED (26,853 of 27,974 GOOG rows). The sqlite's is NULL on every
    one of its 1,440,782 chain rows (re-measured 2026-08-31; see
    ``option_selector._publishes_spread`` for the full record), so ``option_selector``'s
    ``min_open_interest`` gate is un-answerable there and becomes usable here. This is the
    ONE field that is genuinely dead in the sqlite -- its ``iv``/``delta`` are populated on
    46% of chain rows and 88% of BAR rows, so do not extend this bullet to them.
  * ``iv`` — the VENDOR's implied volatility, populated (26,840 of 27,974 GOOG rows). It is
    carried through on the bar dict as ``vendor_iv`` but is NOT what selection reads; see
    GREEKS below.

WHAT IT DOES NOT HAVE, AND WHY THAT IS NOT A REGRESSION
  * greeks (delta/gamma/theta/vega) are ABSENT. They are derived exactly the way the sqlite
    store's own bars were derived at BUILD time: one call to
    ``option_greeks.compute_iv_and_greeks`` per (contract, bar), Black-Scholes-inverting THAT
    bar's own close against the underlying's close on THAT date. Same function, same model,
    same convention (theta per calendar day, vega per vol point) — there is deliberately no
    second greeks path in this file.
    Consequence worth stating: ``implied_volatility`` reported here is the INVERTED iv, not
    the vendor's, so that it and ``delta`` are the same number's consequences. Measured on
    GOOG's 25,864 comparable rows the two differ by a median 0.034 / mean 0.060 / p90 0.117
    of a vol unit — close in kind, not equal. ``vendor_iv`` is preserved on the bar dict so a
    later change can prefer it without re-reading 205 MB.
  * bid/ask are ABSENT (dxfeed serves no historical NBBO for dead contracts) and no worse
    than sqlite, where ``bid == ask`` on every quoted row (0 of 1,083,571 have ask > bid;
    the other 357,211 of 1,440,782 chain rows carry no quote at all): both stores are a
    ZERO-SPREAD premium proxy and the tradeable spread is MODELLED downstream by
    ``option_spread_pct``. CONSEQUENCE FOR RANKING, since it reads backwards at a glance:
    a constant 0.0 spread is not "no signal", it is the BEST possible score --
    ``option_selection_policy._minimise`` maps a degenerate column to 0.0 and inverts it to
    1.0 for every candidate. ``w_spread`` therefore fails OPEN uniformly here, which is why
    the grid withholds it (see the launcher's ``_OPTION_SELECTION_WEIGHT_BANDS``).
    So bid = ask = last = the clamped bar's close, which is literally what
    ``fetch_options.contract_to_metadata_chain_row`` writes into the sqlite chain. Returning
    None instead would be "fail-loud" in name only: the option ENTRY action needs a non-None
    ``ask`` to size and price an order, so it would make the store unusable rather than
    honest.

SPOT IS INJECTED, NOT INVENTED. Black-Scholes needs the underlying price and NO option store
records one (the sqlite doesn't either — see ``options_provider.get_atm_iv``'s ATM PROXY
note). ``spot_source(underlying, bar_date) -> Optional[float]`` is supplied by the caller;
``options_store.build_options_provider`` wires it to the run's ``AsOfPriceSource``, whose
closes are the same FMP daily bars ``fetch_options`` inverted the sqlite store's greeks from.
It is only ever asked for the date of a bar that is ALREADY clamped to <= the engine clock,
so it cannot introduce lookahead. ``risk_free_rate`` is likewise a required constructor
argument — a pricing assumption, not data — with the single declared default living at the
wiring boundary.

THE SPOT SOURCE LIVES ON THE PROVIDER, NOT IN THE CACHE. It is a closure over the RUN's
``AsOfPriceSource`` (``options_store.price_source_spot``), and that price source owns the
run's whole OHLCV memo. Nothing on the run path clears the worker caches
(``clear_worker_parquet_options_cache`` has no production caller — it is test isolation), so
a cached object holding that closure would pin the finished run's price source, and its memo,
for the life of the pool worker. It is therefore threaded through
``greeks_tuple``/``bar_dict``/``contract`` as an argument instead of stored on the cached
overlay. What the overlay caches is the RESULT (a float close per bar date), which is inert.

AS-OF CLAMP (the whole point). The store is one row per contract per bar_date, so "the chain
on date D" is derived, not stored: the contract UNIVERSE on D is every contract with at least
one bar dated <= D, and each contract's row is its LATEST bar <= D. A contract whose first
bar is after D is not in the chain at all. That is the same shape as the sqlite reader
(``latest_as_of`` snapshot + per-contract ``latest_on_or_before`` overlay) with the snapshot
derived instead of stored, and like it, a stale-but-clamped bar is preferred to no row — the
fill engine still requires an EXACT bar on the fill day, so an untraded contract cannot fill.

DELTA-AT-ENTRY IS A NAMED SEAM METHOD, NOT AN INCIDENTAL ATTRIBUTE.
``results._build_refine_drawdown_fn`` needs one option-specific fact — the delta a contract
carried when the trade was entered — to refine intraday drawdown. It used to reach for
``options.cache.db_path``, an attribute only the sqlite reader has, so on this backend the
refinement silently switched itself off: no log, and a DIFFERENT ``max_drawdown`` (hence a
different ``option_consistent_annual_return`` fitness) for reasons invisible from the result.
Both readers now implement ``delta_at_entry`` and the refinement follows the READER.

CACHING — TWO CACHES, BECAUSE THE BYTES AND THE GREEKS HAVE DIFFERENT KEYS. A GA rebuilds the
provider once per trial from the same store and re-evaluates identical (symbol, date) pairs,
so reads are cached at WORKER-PROCESS level, not per instance:

  * ``_WORKER_RAW_CACHE`` — one ``_RawUnderlying`` per (root, underlying). The parquet bytes,
    parsed into columnar numpy: prices, volumes, the per-contract descriptors, and the
    ordinal/ISO/date lookups derived from them. NOTHING here depends on the run.
  * ``_WORKER_UNDERLYING_CACHE`` — one ``_Underlying`` overlay per (root, underlying, rate,
    spot_scope), holding a reference to the raw plus the BOUNDED greeks memo, the
    per-bar spot memo, and the materialised-bar-dict memo. These ARE a function of the run.
  * ``_WORKER_ATM_IV_CACHE`` — the get_atm_iv RESULT memo, mirroring the sqlite reader's.

WHY THE SPLIT IS NOT COSMETIC. ``spot_scope`` is derived from (universe, interval, window,
warmup) and ``_build_daily_trial_config`` sets ``enabled_instruments`` PER INDIVIDUAL, to the
screener candidates that trial's own genes selected — so in a screener GA the scope changes
between trials of the same job. Keyed as one object, a scope change re-read and re-parsed
BYTE-IDENTICAL parquet (measured: GOOG 145 ms cold, 4.5 us warm, 58 ms on a new scope) and
left two full copies in the 200-entry LRU. The expensive part — the I/O plus
``_iso_to_ordinal_array``'s per-row ``date.fromisoformat`` loop — is scope-INDEPENDENT, so it
belongs in a scope-independent cache. The scope key still guards exactly what it was
introduced to guard (see ``ParquetOptionsProvider.__init__``): two runs whose price sources
answer differently for the same (symbol, date) get different GREEKS, they just stop paying to
re-read the same bytes to find that out.

THE RAW CACHE IS NOW A CACHE OF VIEWS, NOT OF BYTES. ``_load_raw_underlying`` sources the
numeric half from the per-host derived array store (``ba2_common.core.shared_arrays``): the
first process to want an underlying parses the parquet once and publishes ``.npy`` files
beside the source tree, and every process after that memory-maps them, so the OS page cache
holds ONE copy of an underlying per HOST instead of one per worker. What a cached
``_RawUnderlying`` then owns privately is only the projections ``_bind`` derives. Two
consequences worth stating here rather than discovering:

  * ``clear_worker_parquet_options_cache()`` drops the VIEWS. The mapping closes when the
    last view of it dies; the files themselves are the host's and are never touched, so the
    next load re-opens them for free instead of re-parsing anything.
  * a re-read is no longer the thing to fear. A cold miss on a warm host is a mmap, not
    200 MB of parquet — which is why the LRU caps below are sized by the projections.

All three are bounded LRUs (a remote worker's pool is long-lived across jobs touching
different universes) and all three are dropped by ``clear_worker_parquet_options_cache()``,
which ``options_provider.clear_worker_options_cache()`` also calls so existing test isolation
covers this store too. An overlay holds a strong reference to its raw, so evicting a raw
while an overlay still uses it frees nothing and breaks nothing — the two caps are equal and
the keys are parallel, so they evict roughly together.

Columnar (numpy) rather than dict-per-bar because the cap has to clear a realistic universe:
686 underlyings x ~28k rows, and a run's ~100-symbol universe must fit in a worker alongside
the OHLCV memo. Measured on GOOG (27,974 rows / 1,374 contracts): 2.36 MB for the raw
(1.53 MB of numpy plus 0.43 MB of the python projections the hot paths index and
0.26 MB of the contract symbol list/index). The greeks are no longer per-row at all -- they
are a bounded row->tuple memo (``_GREEKS_MEMO_MAX``, 6.3 MB at the default cap however big
the underlying is); see ``_Underlying._fresh_run_fill`` for why the five dense float64
columns they replace could not be made to follow what a trial reads. The per-ROW
projection is an ``array('i')`` rather than a list precisely because that 0.43 MB is the part
that scales with the store — see ``_RawUnderlying._bind``. Bars are
materialised into dicts only when a caller actually reads one, and then memoised (see
``bar_dict``).
"""
from __future__ import annotations

import array as _array
import logging
import os
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.types import OptionRight
from ba2_providers.options.tastytrade import parse_occ

from .option_greeks import compute_iv_and_greeks
from .options_cache import OptionsCacheMiss

logger = logging.getLogger(__name__)

#: Underlyings held per worker process. WHAT AN ENTRY COSTS THIS PROCESS is now the
#: PROJECTIONS plus the two bounded overlay memos, not the columns: the numeric arrays are mapped from
#: the host-shared derived cache (see CACHING), so they are one copy per host and reclaimable
#: page cache, while ``bar_ord_l`` (4 B/row), the per-contract lists and the ordinal/ISO
#: dicts are private per process. ON A LEGACY NO-QUOTE TREE ADD 16 B/ROW: ``_bind``
#: materialises ``bid``/``ask`` as private ``np.full(n_rows, nan)`` there (they are stored
#: zero-length precisely so that nothing but the reader that needs them pays for them), which
#: is four times what ``bar_ord_l`` costs and makes the TastyTrade tree, not the quoted
#: ThetaData one, the expensive case per process. On GOOG's 27,974 rows that is the 0.43 MB
#: per-row projection plus the 0.26 MB contract list/index measured in the module docstring,
#: against the 1.53 MB of columns that no longer count, plus at most 10.4 MB of bounded
#: greeks + bar memo per overlay whatever the row count. Sizing the
#: cap BELOW the run's universe is still the thing to avoid — that thrashes
#: (evict-then-reload) inside a single bar, exactly as the sqlite reader's bar-cache comment
#: warns — but the penalty for a reload is now a mmap rather than ~55 ms of parquet, so
#: lowering it on a memory-tight worker is a cheaper option than it was. The same cap bounds
#: BOTH the raw cache and the scope-keyed overlay cache.
_UNDERLYING_CACHE_MAX = int(os.getenv("BT_OPTION_PARQUET_CACHE_MAX", "200"))
#: Same generosity (and same reasoning) as the sqlite reader's ATM-IV memo: the values are a
#: float or None, and a GA re-asks the identical (symbol, date) pairs on every trial.
_ATM_IV_CACHE_MAX = int(os.getenv("BT_OPTION_ATM_IV_CACHE_MAX", "200000"))
#: Greeks memoised per OVERLAY, as row index -> the finished 5-tuple. THE NUMBER THAT SIZES
#: A WORKER: at a measured 315 B/entry this is 6.3 MB per underlying, so the 98-symbol
#: stage-1 option universe holds ~617 MB where the five dense float64 columns it replaces
#: held 7.1 GB. The cap is chosen from the WIDEST read pattern the seam admits (1-730 DTE,
#: every contract every bar: 751 new rows and 1,326 calls per bar on AAPL) — at 20,000 it
#: gives up 0.4% of the hits an unbounded memo gets and 98.5% of the residency. Lower it on a
#: memory-tight worker before touching anything else here; 5,000 still holds 41.1% of 43.6%.
#: See ``_Underlying._fresh_run_fill``.
_GREEKS_MEMO_MAX = int(os.getenv("BT_OPTION_GREEKS_MEMO_MAX", "20000"))
#: Materialised bar dicts held per OVERLAY (see ``_Underlying.bar_dict``). 821 B of resident
#: memory per entry, measured — 2.6x what a greeks entry costs, which is why its cap is
#: tighter even though it is asked for far less often. ``bar_dict`` is reached only through
#: ``get_bar``, i.e. only for HELD lots and order fills: the real-tree walk above produced 730
#: entries over 914 bar dates, so 5,000 is ~7x the measured need and 4.1 MB per underlying
#: (~400 MB across a 98-symbol universe in the worst case). Until 2026-09-15 it was unbounded
#: and counted at zero in ``memory_stats``. Eviction is insertion-ordered (see ``bar_dict``).
_BAR_MEMO_MAX = int(os.getenv("BT_OPTION_BAR_MEMO_MAX", "5000"))
#: What one ``_bar_memo`` entry costs this process, for ``memory_stats``. MEASURED, not
#: derived: 200,000 entries over a 5M-row synthetic moved the working set by 156.5 MB, i.e.
#: 821 B — the 17-key dict object (``getsizeof`` 464 B) plus the float/int objects its values
#: point at plus the holding dict's own slot. ``sys.getsizeof`` alone would under-report it by
#: ~40%, which is exactly the kind of number that makes a worker look healthy while it is not.
_BAR_MEMO_BYTES_PER_ENTRY = 821
#: Likewise for ``_g_memo``: 500,000 real greek tuples moved the working set by 150.2 MB —
#: 315 B for a 5-tuple, its five float objects and the dict slot holding it.
_GREEKS_MEMO_BYTES_PER_ENTRY = 315

#: SCOPE-INDEPENDENT. The parquet bytes and everything derived from them alone. See CACHING.
_WORKER_RAW_CACHE: "OrderedDict[Tuple[str, str], _RawUnderlying]" = OrderedDict()
# The overlay keys carry SPOT_SCOPE (see ParquetOptionsProvider.__init__): the cached greeks
# are a function of the underlying closes the run's price source serves, and two runs over
# different windows/universes do not serve the same ones. The RAW bars they sit on are the
# same bytes either way, which is why they are a separate cache.
_WORKER_UNDERLYING_CACHE: "OrderedDict[Tuple[str, str, float, str], _Underlying]" = OrderedDict()
_WORKER_ATM_IV_CACHE: "OrderedDict[Tuple[str, str, float, str, int], Optional[float]]" = OrderedDict()

#: Distinct from None, which is a VALID cached result. See options_provider._MISSING.
_MISSING = object()

#: ATM window, identical to the sqlite reader's so the two produce the same statistic.
_ATM_DTE_MIN = 20
_ATM_DTE_MAX = 45

#: greeks_tuple's shape, for readers of the hot paths. (iv, delta, gamma, theta, vega).
_NO_GREEKS: Tuple[Optional[float], ...] = (None, None, None, None, None)

#: ``bar_ord_l``'s buffer type. 'i' is C ``int`` — 4 bytes on every platform BA2 runs on, and
#: the same width as the ``bar_ord`` int32 whose bytes are copied straight into it. Checked at
#: import rather than trusted: a 2- or 8-byte ``int`` would not raise, it would silently
#: re-interpret the buffer and mis-clamp every as-of read.
_BAR_ORD_TYPECODE = "i"
if _array.array(_BAR_ORD_TYPECODE).itemsize != 4:  # pragma: no cover - not reachable on x86/ARM
    raise RuntimeError(
        f"array('{_BAR_ORD_TYPECODE}').itemsize is "
        f"{_array.array(_BAR_ORD_TYPECODE).itemsize}, not 4: bar_ord_l cannot be filled from "
        "an int32 buffer on this platform")


def memory_stats() -> Dict[str, Any]:
    """Cheap per-process snapshot of what THIS worker's option caches hold, PRIVATE vs MAPPED.

    The counterpart to ``price_source.memory_stats``'s ``bar_cache`` split, and for the same
    reason: with ``BA2_SHARED_ARRAYS`` on, the columns are views over ``.npy`` files the HOST
    owns — one copy however many workers map them, and clean pages the OS reclaims under
    pressure — while the projections every process must build for itself are real allocations.
    Reported as one number they are indistinguishable, and at the 2020 ThetaData window that is
    the difference between a worker holding 15.6 GB and a box holding 15.6 GB once.

      * ``private_mb`` — the per-process half: any column that is a real allocation (the
        ``BA2_SHARED_ARRAYS=0`` path), ``bar_ord_l`` (the ``array('i')`` the bisects read,
        4 B/row), the ``starts_l``/``stops_l`` lists (one pointer per CONTRACT, not per row),
        and the two overlay memos, which are always private: they are computed here from this
        run's spot/rate and depend on nothing on disk. Both are counted at their MEASURED
        per-entry cost, and both were counted WRONG until 2026-09-15 — the greeks nominally
        (five dense columns whose ``nbytes`` said 7.1 GB whatever a trial read) and the bar
        memo not at all.
      * ``greeks_entries`` / ``greeks_mb`` and ``bar_memo_entries`` / ``bar_memo_mb`` — what
        the two bounded memos actually hold, so the numbers ``_GREEKS_MEMO_MAX``,
        ``_BAR_MEMO_MAX`` and ``reset_run_overlays`` act on are visible rather than inferred.
        These are EXACT (an entry count times a measured constant), not a ceiling.
      * ``shared_mb`` — columns backed by an ``np.memmap``. ``isinstance(arr.base, np.memmap)``
        asks the real question rather than ``arr.base is not None``, which a private fancy-index
        view would also satisfy (see price_source.memory_stats).

    NOT counted: ``c_occ``/``c_index`` and the per-contract python objects. They are per
    CONTRACT (thousands), not per row (millions), and pricing a python string's true footprint
    is guesswork — a number that cannot be trusted is worse here than an absent one.

    O(entries x columns) with tiny constants — safe to call per trial or per release.
    """
    private = 0
    shared = 0
    for raw in list(_WORKER_RAW_CACHE.values()):
        for name in _RawUnderlying._DIRECT_ARRAYS:
            arr = getattr(raw, name, None)
            n = int(getattr(arr, "nbytes", 0) or 0)
            if isinstance(getattr(arr, "base", None), np.memmap):
                shared += n
            else:
                private += n
        bol = getattr(raw, "bar_ord_l", None)
        if bol is not None:
            private += bol.itemsize * len(bol)
        for lst in (getattr(raw, "starts_l", None), getattr(raw, "stops_l", None)):
            if lst is not None:
                private += 8 * len(lst)      # one pointer per entry; the ints themselves are small
    greeks_entries = 0
    memo_entries = 0
    for ov in list(_WORKER_UNDERLYING_CACHE.values()):
        greeks_entries += len(ov._g_memo)
        memo_entries += len(ov._bar_memo)
    greeks_bytes = greeks_entries * _GREEKS_MEMO_BYTES_PER_ENTRY
    memo_bytes = memo_entries * _BAR_MEMO_BYTES_PER_ENTRY
    private += greeks_bytes + memo_bytes
    return {"entries": len(_WORKER_RAW_CACHE),
            "private_mb": round(private / 1048576, 1),
            "shared_mb": round(shared / 1048576, 1),
            "greeks_entries": greeks_entries,
            "greeks_mb": round(greeks_bytes / 1048576, 1),
            "bar_memo_entries": memo_entries,
            "bar_memo_mb": round(memo_bytes / 1048576, 1)}


def reset_run_overlays() -> Dict[str, Any]:
    """Drop everything a RUN filled into the cached overlays; keep everything the STORE gave.

    THE MEMOS ARE PER-WORKER, THE RUNS ARE NOT. An overlay is cached per
    (root, underlying, rate, spot_scope) for the LIFE of the worker — 32 individuals, by
    ``BT_MAX_TASKS_PER_CHILD`` — and successive genomes read different contracts on different
    dates, so without this the memos hold the UNION of every trial the worker has run. Measured
    on a 5M-row synthetic: a second genome reuses 7.5% of the first's fill, and two genomes
    alone hold 1.93x one genome's rows. Called once per trial, this makes the ceiling ONE
    trial's working set — which the caps then bound in turn.

    WHAT IT DROPS — and all four are pure memoisation of pure functions of this run's spot
    source and rate, which is the only reason dropping them per trial is safe:
      * ``_g_memo`` — the greeks, 315 B/entry;
      * ``_bar_memo`` — the materialised bar dicts, 821 B/entry;
      * ``_spot_cache`` — one float per bar DATE, small, but it is run-scoped like the rest;
      * ``_WORKER_ATM_IV_CACHE`` — a run-scoped RESULT memo, and pointless to keep once the
        greeks it summarises are gone.

    SECONDARY, since the memos became bounded (see ``_Underlying._fresh_run_fill``): the caps
    are what stop a worker converging on the window, and this is what stops it carrying one
    genome's working set into the next. It is cheap — a few dict drops per underlying — so it
    stays on the per-trial path rather than being folded into the caps.

    WHAT IT KEEPS is the expensive half: ``_WORKER_RAW_CACHE`` and the overlay OBJECTS, so
    the mapped ``.npy`` columns stay open and the private projections ``_bind`` derives
    (``bar_ord_l``, the per-contract lists, the ordinal/ISO dicts) are not rebuilt. That is
    what makes this different from ``clear_worker_parquet_options_cache()`` and from the
    governor's ``_worker_release_memory``, both of which drop the lot: this runs on the happy
    path after EVERY trial, so it has to cost a re-computation and never a re-open.

    Returns what it dropped — the only visibility a worker has that the reset is still
    matching the overlays rather than quietly finding none.
    """
    overlays = list(_WORKER_UNDERLYING_CACHE.values())
    greeks_rows = 0
    bar_memo_entries = 0
    spot_entries = 0
    for ov in overlays:
        greeks_rows += len(ov._g_memo)
        bar_memo_entries += len(ov._bar_memo)
        spot_entries += len(ov._spot_cache)
        ov._fresh_run_fill()
    atm = len(_WORKER_ATM_IV_CACHE)
    _WORKER_ATM_IV_CACHE.clear()
    return {"overlays": len(overlays), "greeks_rows": greeks_rows,
            "bar_memo_entries": bar_memo_entries, "spot_entries": spot_entries,
            "atm_iv_entries": atm}


def clear_worker_parquet_options_cache() -> None:
    """Drop every cached underlying + ATM-IV result (test isolation / explicit reset)."""
    _WORKER_RAW_CACHE.clear()
    _WORKER_UNDERLYING_CACHE.clear()
    _WORKER_ATM_IV_CACHE.clear()
    _underlying_of.cache_clear()


def _iso_to_ordinal_array(series) -> np.ndarray:
    """'YYYY-MM-DD' strings -> proleptic-Gregorian ordinals (int32).

    Ordinals, not YYYYMMDD: they are monotone in date (so ``searchsorted`` is the as-of clamp)
    AND their difference is a day count, which is exactly what the Black-Scholes ``T`` needs.

    The per-row ``date.fromisoformat`` loop is the second-biggest cost of a cold load after
    the parquet read itself — and, like the read, it depends on nothing but the bytes, which
    is why ``_RawUnderlying`` (where it lands) is cached without the run's spot scope.
    """
    return np.array([date.fromisoformat(str(s)).toordinal() for s in series], dtype=np.int32)


class _RawUnderlying:
    """One underlying's whole parquet history, columnar. SCOPE-INDEPENDENT — see CACHING.

    Rows are sorted by (occ_symbol, bar_date); ``starts[i]:stops[i]`` is contract ``i``'s
    slice, so an as-of clamp is a ``searchsorted`` inside that slice.

    Everything a bar or a chain row needs that is NOT a greek is precomputed here once:
    ``iso_of_ord`` (there are ~60 distinct bar dates in a quarter, not 28,000) and the
    per-contract ``c_expiry_date`` / ``c_expiry_iso`` / ``c_type_str`` / ``c_strike_f``
    lists, all as native Python objects. ``date.fromordinal(...).isoformat()`` is 0.22 us and
    ``float(np.float64)`` is 0.023 us against 0.016 us for a list index — small individually,
    and the whole reason ``bar_dict`` used to cost 5.4 us.
    """

    __slots__ = (
        "underlying", "n_rows",
        "c_occ", "c_index", "c_strike", "c_strike_f", "c_expiry_ord", "c_expiry_ord_l",
        "c_is_call", "c_expiry_date", "c_expiry_iso", "c_type_str", "c_right",
        "starts", "stops", "starts_l", "stops_l",
        "bar_ord", "bar_ord_l", "open", "high", "low", "close", "volume", "open_interest",
        "vendor_iv", "iso_of_ord", "date_of_ord", "bid", "ask", "has_quotes",
    )

    #: THE SHAREABLE HALF, COMPLETE. An underlying splits cleanly in two: numeric columns that
    #: depend on nothing but the parquet bytes, and the python list/dict projections below,
    #: which every process has to own. Only the first half can be memory-mapped, so
    #: ``ARRAY_NAMES`` is the contract with a per-host derived array store: 1-D,
    #: numeric-or-bool, NEVER object. ``arrays_from_frame`` produces exactly these names,
    #: ``from_arrays`` consumes exactly these names, and the store in between knows nothing
    #: about this class. Two entries are ENCODINGS rather than columns, because the contract
    #: admits no others:
    #:   * ``c_occ_utf8`` -- the contract symbols, newline-joined and UTF-8 encoded to uint8.
    #:     They are strings (an object array, which a ``.npy`` mapping refuses) and each
    #:     process needs its own list + index dict anyway; the bytes are the cheap part.
    #:   * ``has_quotes`` -- a 1-element bool array, because a scalar is not an array.
    #:   * ``priceless_count`` -- a 1-element int64: the invariant is COUNTED at build (it is
    #:     a fact about the store) but has to be SAID in every process, so the count travels
    #:     with the arrays rather than the log line staying behind on the host that built them.
    #: ``bid``/``ask`` are ZERO-LENGTH when ``has_quotes`` is false; see ``_bind``.
    #:
    #: THE COLUMN SET NEEDS NO KEY COMPONENT OF ITS OWN. A tree re-warmed to ADD the bid/ask
    #: columns changes what these arrays contain and their very lengths -- but it can only do
    #: that by REWRITING the partitions, and a rewritten partition is a new (path, size,
    #: mtime) and therefore a new source signature. ``has_quotes`` travels IN the array set
    #: rather than in the key because it is an output of the build, not an input to it.
    ARRAY_NAMES = (
        "bar_ord", "open", "high", "low", "close", "volume", "open_interest", "vendor_iv",
        "bid", "ask", "starts", "stops", "c_strike", "c_expiry_ord", "c_is_call",
        "c_occ_utf8", "has_quotes", "priceless_count",
    )

    #: THE READER'S HALF OF THE DERIVED-CACHE CONTRACT, carried in the cache KEY. Bump it
    #: whenever ``ARRAY_NAMES`` changes, an encoding changes, or the MEANING of an array
    #: changes: a set published by an older reader then lives under a different key and can
    #: never be opened by a newer one (nor the reverse), whatever the sources look like.
    #: ``shared_arrays.SCHEMA_VERSION`` covers the on-disk FILE layout, which is the store's
    #: business; this covers what the bytes inside those files mean, which is ours. It is
    #: deliberately NOT reused for a column-set change -- see ARRAY_NAMES above, where a
    #: rewritten partition already moves the signature.
    #:
    #: A BUMP ORPHANS DISK, so it is a maintenance action and not just an edit. The version is
    #: part of the KEY, so ``<derived_root>/u_<SYM>.v<old>`` becomes a key nothing asks for --
    #: and ``sweep()`` only keeps the newest signature WITHIN a key, so it will never collect
    #: the old version's directories however long they sit there. Bumping means deleting the
    #: ``*.v<old>`` directories (Task 7's ``--sweep`` gains a stale-version pass). And do not
    #: re-warm a tree mid-grid on Windows: the running workers keep the old set MAPPED, so
    #: NTFS refuses to evict it and both sets occupy the disk until the grid exits.
    ARRAYS_VERSION = 1

    #: The ARRAY_NAMES that bind straight onto the identically-named slots the hot paths read.
    #: (The three above are decoded into ``c_occ`` / ``has_quotes`` / a log line instead.)
    _DIRECT_ARRAYS = tuple(
        n for n in ("bar_ord", "open", "high", "low", "close", "volume", "open_interest",
                    "vendor_iv", "bid", "ask", "starts", "stops", "c_strike", "c_expiry_ord",
                    "c_is_call", "c_occ_utf8", "has_quotes", "priceless_count")
        if n not in ("c_occ_utf8", "has_quotes", "priceless_count"))

    @staticmethod
    def arrays_from_frame(df) -> Dict[str, np.ndarray]:
        """The parquet frame -> the shareable numeric half, as a plain dict of 1-D arrays.

        This is the expensive part of a cold load (the parquet read plus
        ``_iso_to_ordinal_array``'s per-row ``date.fromisoformat`` loop) and it is a pure
        function of the bytes, which is what makes the result shareable at all.

        The ``priceless`` invariant is checked HERE rather than in the binder: it is a fact
        about the STORE, so it is answered once when the arrays are built, not once per
        process that opens them.
        """
        if df is None or not len(df):
            arrays: Dict[str, np.ndarray] = {
                name: np.empty(0, dtype="float64")
                for name in ("open", "high", "low", "close", "volume", "open_interest",
                             "vendor_iv", "bid", "ask", "c_strike")
            }
            # int32/bool deliberately, NOT np.empty(0)'s default float64: an empty underlying
            # must present the same dtypes as a populated one, or a rebuild through the store
            # would silently change an ordinal column's type.
            for name in ("bar_ord", "starts", "stops", "c_expiry_ord"):
                arrays[name] = np.empty(0, dtype=np.int32)
            arrays["c_is_call"] = np.empty(0, dtype=bool)
            arrays["c_occ_utf8"] = np.empty(0, dtype=np.uint8)
            arrays["has_quotes"] = np.array([False], dtype=bool)
            arrays["priceless_count"] = np.array([0], dtype=np.int64)
            return arrays

        df = df.sort_values(["occ_symbol", "bar_date"], kind="mergesort").reset_index(drop=True)
        occ = df["occ_symbol"].astype(str).to_numpy(dtype=object)
        n = len(occ)

        is_new = np.empty(n, dtype=bool)
        is_new[0] = True
        if n > 1:
            is_new[1:] = occ[1:] != occ[:-1]
        starts = np.flatnonzero(is_new).astype(np.int32)

        c_occ = [str(s) for s in occ[starts]]
        arrays = {
            "starts": starts,
            "stops": np.append(starts[1:], np.int32(n)).astype(np.int32),
            "c_occ_utf8": np.frombuffer("\n".join(c_occ).encode("utf-8"), dtype=np.uint8),
            "c_strike": df["strike"].to_numpy(dtype="float64")[starts],
            "c_expiry_ord": _iso_to_ordinal_array(df["expiry"].to_numpy(dtype=object)[starts]),
            "c_is_call": (df["option_type"].astype(str).to_numpy(dtype=object)[starts]
                          == OptionRight.CALL.value),
            "bar_ord": _iso_to_ordinal_array(df["bar_date"].to_numpy(dtype=object)),
            "vendor_iv": df["iv"].to_numpy(dtype="float64", na_value=np.nan),
            # volume/open_interest are pandas Int64 (nullable). float64 + nan keeps "absent"
            # distinguishable from a recorded 0, which is a fact about a strike nobody trades.
            "volume": df["volume"].to_numpy(dtype="float64", na_value=np.nan),
            "open_interest": df["open_interest"].to_numpy(dtype="float64", na_value=np.nan),
        }
        for col in ("open", "high", "low", "close"):
            arrays[col] = df[col].to_numpy(dtype="float64", na_value=np.nan)

        # REAL QUOTES, when the store has them. The TastyTrade tree predates the bid/ask
        # columns entirely and its partitions do not carry them; ThetaData's do. Absent
        # columns => all-NaN => `contract()` falls back to the historical zero-spread close
        # proxy, so a TastyTrade-backed run is byte-identical to before this existed.
        has_quotes = "bid" in df.columns and "ask" in df.columns
        arrays["has_quotes"] = np.array([has_quotes], dtype=bool)
        if has_quotes:
            arrays["bid"] = df["bid"].to_numpy(dtype="float64", na_value=np.nan)
            arrays["ask"] = df["ask"].to_numpy(dtype="float64", na_value=np.nan)
        else:
            # NOT two all-NaN columns. On a TastyTrade-style tree they would be 16 bytes a row
            # of recorded nothing (61 MB for TSLA's 7.6M rows) written to disk and mapped into
            # every worker. Absence is the fact; ``_bind`` materialises the NaN arrays the read
            # paths index, privately, at zero storage cost.
            arrays["bid"] = np.empty(0, dtype="float64")
            arrays["ask"] = np.empty(0, dtype="float64")

        # INVARIANT: every stored row has a price -- a trade close, or a quote, or both. A row
        # with neither cannot be priced, and (per option_selector.passes_liquidity) a contract
        # whose mark is None SKIPS the penny-contract gate instead of being rejected by it, so
        # it would survive selection unpriced. Providers drop such rows at ingest; this counts
        # them once per underlying rather than per contract, so a store that violates the
        # invariant says so loudly instead of quietly mis-selecting.
        no_quote = np.isnan(arrays["bid"]) & np.isnan(arrays["ask"]) if has_quotes else True
        priceless = int(np.count_nonzero(np.isnan(arrays["close"]) & no_quote))
        arrays["priceless_count"] = np.array([priceless], dtype=np.int64)
        if priceless:
            # The symbol comes from the FRAME (the store writes an ``underlying`` column,
            # already upper-cased) rather than from a constructor argument, because this
            # check belongs to the build and the build takes only the frame. Guarded so a
            # malformed store cannot turn the complaint about it into a KeyError.
            symbol = (str(df["underlying"].iloc[0]) if "underlying" in df.columns
                      else "<no underlying column>")
            logger.error(
                "%s: %d of %d option bar rows have NO price at all (no close, no bid, no "
                "ask). These cannot be marked or liquidity-gated. The store is malformed -- "
                "re-warm this underlying.", symbol, priceless, n)
        return arrays

    @classmethod
    def from_arrays(cls, underlying: str, arrays: Dict[str, np.ndarray]) -> "_RawUnderlying":
        """Rebuild an underlying from an ARRAY_NAMES dict the caller already holds.

        The dict may be memory-mapped and shared with other worker processes; everything this
        adds on top of it is per-process by construction (see ``_bind``).
        """
        self = cls.__new__(cls)
        self._bind(underlying, arrays)
        return self

    def __init__(self, underlying: str, df):
        self._bind(underlying, self.arrays_from_frame(df))

    def _bind(self, underlying: str, arrays: Dict[str, np.ndarray]) -> None:
        """Bind the shared arrays, then derive the per-process projections from them.

        WHY THE CLAMP DOES NOT BISECT THE NUMPY ARRAY. ``bisect_right(seq, x, lo, hi)`` is
        0.051 us against 0.73 us for ``np.searchsorted(arr[lo:hi], x)``, which allocates a
        view and pays numpy's call overhead; get_chain runs that clamp ONCE PER CONTRACT
        (1,374 times for GOOG) and get_atm_iv once per contract in the DTE band.

        WHY ``bar_ord_l`` IS AN ``array('i')`` AND NOT A LIST. It is per-row, and it is the
        only per-row projection, so it is the one that scales: a list of python ints costs
        8.2 B/row PRIVATE per process -- twice the 4 B/row the MAPPED ``bar_ord`` shares -- and
        the interned ``.tolist()`` + ``setdefault`` build spiked 243 MB of transient int
        objects. Measured on TSLA's 7.6M rows: 62.6 MB and 668 ms as a list against 32.3 MB
        and 13.8 ms filled from the int32 bytes, and the bisect over a contract's window (a
        few hundred rows) pays +0.023 us, +6%. ``starts_l``/``stops_l`` stay lists: they are
        per-CONTRACT, three orders of magnitude smaller, and indexed as scalars.
        (None of the three can be shared either way -- neither a list nor an ``array`` is a
        mappable buffer -- which is why they are rebuilt here rather than stored.)
        """
        self.underlying = underlying
        for name in self._DIRECT_ARRAYS:
            setattr(self, name, arrays[name])
        self.has_quotes = bool(arrays["has_quotes"][0])
        occ_utf8 = arrays["c_occ_utf8"]
        self.c_occ = bytes(occ_utf8).decode("utf-8").split("\n") if occ_utf8.size else []
        self.c_index = {s: i for i, s in enumerate(self.c_occ)}
        self.n_rows = int(len(self.bar_ord))

        # The encoding is newline-joined, so a symbol containing a newline -- or a mapping
        # paired with the wrong underlying's arrays -- shifts every contract index by one and
        # mis-prices silently from then on. One comparison per underlying buys that back.
        if len(self.c_occ) != len(self.starts):
            raise ValueError(
                f"{underlying}: c_occ_utf8 decoded to {len(self.c_occ)} symbols for "
                f"{len(self.starts)} contracts -- a symbol contains a newline, or the mapped "
                "arrays are inconsistent")

        # SAID HERE, counted at build. With a per-host derived store the arrays are built once
        # and opened by every later process, so a build-time-only log would report a malformed
        # store to exactly one run and leave the rest of them silent.
        priceless = int(arrays["priceless_count"][0])
        if priceless:
            logger.error(
                "%s: %d of %d option bar rows have NO price at all (no close, no bid, no "
                "ask). These cannot be marked or liquidity-gated. The store is malformed -- "
                "re-warm this underlying.", underlying, priceless, self.n_rows)

        # NOT PERSISTED, because absence is the fact and two all-NaN columns are not (see
        # arrays_from_frame). Materialised privately so every read path finds a full-length
        # array whether or not the tree carries quotes; only TastyTrade-style trees pay it.
        if not self.has_quotes:
            self.bid = np.full(self.n_rows, np.nan)
            self.ask = np.full(self.n_rows, np.nan)

        self.bar_ord_l = _array.array(_BAR_ORD_TYPECODE)
        self.bar_ord_l.frombytes(
            np.ascontiguousarray(self.bar_ord, dtype=np.int32).tobytes())
        self.starts_l = self.starts.tolist()
        self.stops_l = self.stops.tolist()
        date_of_ord = {int(o): date.fromordinal(int(o))
                       for o in np.unique(np.concatenate([self.bar_ord, self.c_expiry_ord]))}
        self.date_of_ord = date_of_ord
        self.iso_of_ord = {o: d.isoformat() for o, d in date_of_ord.items()}
        self.c_expiry_ord_l = self.c_expiry_ord.tolist()
        self.c_expiry_date = [date_of_ord[o] for o in self.c_expiry_ord_l]
        self.c_expiry_iso = [self.iso_of_ord[o] for o in self.c_expiry_ord_l]
        self.c_strike_f = self.c_strike.tolist()
        self.c_right = [OptionRight.CALL if c else OptionRight.PUT
                        for c in self.c_is_call.tolist()]
        self.c_type_str = [r.value for r in self.c_right]


class _Underlying:
    """A run-scoped greeks/spot/bar overlay over one cached ``_RawUnderlying``.

    Keyed on (root, underlying, rate, spot_scope) — everything in here is a function of the
    run's underlying closes and its Black-Scholes rate, and nothing in here is a function of
    the parquet bytes, which the raw already holds exactly once.

    The raw's columnar arrays are re-bound onto this object's own slots at construction (a
    handful of pointer copies) so every clamp/read stays a single attribute load rather than
    a two-hop ``self.raw.close[i]`` on paths that run per contract per bar.
    """

    __slots__ = (
        "raw", "underlying", "rate", "n_rows",
        "c_occ", "c_index", "c_strike", "c_expiry_ord", "c_is_call", "starts", "stops",
        "bar_ord", "bar_ord_l", "starts_l", "stops_l",
        "open", "high", "low", "close", "volume", "open_interest", "vendor_iv", "bid", "ask",
        "_g_memo", "_spot_cache", "_bar_memo",
    )

    def __init__(self, raw: "_RawUnderlying", rate: float):
        self.raw = raw
        self.underlying = raw.underlying
        self.rate = float(rate)

        n = raw.n_rows
        self.n_rows = n
        for name in ("c_occ", "c_index", "c_strike", "c_expiry_ord", "c_is_call",
                     "starts", "stops", "starts_l", "stops_l", "bar_ord", "bar_ord_l",
                     "open", "high", "low", "close", "volume", "open_interest", "vendor_iv",
                     "bid", "ask"):
            setattr(self, name, getattr(raw, name))

        # THE GREEKS ARE A BOUNDED MEMO, NOT A COLUMN. See ``_fresh_run_fill``.
        self._fresh_run_fill()

    def _fresh_run_fill(self) -> None:
        """(Re)build everything that is a function of the RUN rather than of the store.

        Called by ``__init__`` and by ``reset_run_overlays``; the two must not diverge, which
        is the whole reason it is one method.

        WHY THE GREEKS ARE NOT FIVE COLUMNS ANY MORE (2026-09-15). They were: five
        ``float64`` arrays plus a ``_g_done`` bool mask, 41 B for every ROW of the mapped raw,
        private to each worker. ``np.full`` made all of it resident at construction; ``np.empty``
        (commit c608ac05) made it lazy, which fixed the construction spike and nothing else.
        The field then showed why that was not enough — remote227, stage-1 option grid, 98
        underlyings = 177.8M rows, 30 workers: 5.4-7.2 GB anonymous per worker (median 6.6)
        after ONE OR TWO trials, cgroup anon 218 GB of a 232 GB cap, swap exhausted.

        LAZY DOES NOT HELP HERE, and the reason is page granularity against the store's own
        layout. Rows are sorted by (occ_symbol, bar_date), so one contract's rows are
        contiguous and ~2.4 KB per column — SMALLER THAN A 4 KB PAGE. Every contract passes
        through the DTE band a strategy reads, so every contract is touched, so every page of
        all five columns becomes resident. Measured on the real tree (AAPL, 711,559 rows, 914
        bar dates 2023-01..2026-08, a 20-60 DTE chain read + get_atm_iv every day + MTM
        re-reads of held lots): 24.5% of ROWS touched, and 38.8 B/row RESIDENT against a
        40 B/row nominal. One trial, and the columns are already all there.

        SO THE MEMO IS BOUNDED AND SPARSE INSTEAD: ``_g_memo`` maps row index -> the finished
        5-tuple, capped at ``_GREEKS_MEMO_MAX``. The cap works because the REUSE IS LOCAL —
        a bar's chain read, its ``get_atm_iv`` and its held-lot ``get_bar`` calls all land on
        the same few hundred rows, and the next bar moves on. Measured on the same AAPL walk,
        and on the WIDEST read pattern the seam admits (1-730 DTE, i.e. every contract every
        day, 751 new rows and 1,326 calls per bar):

            design                   hit rate   misses     resident     elapsed
            dense columns (before)     43.6%    683,342    28 MB (41.0 B/row)   134 s
            memo, cap 20,000           43.4%    686,097     0 MB ( 0.6 B/row)   139 s
            memo, cap  5,000           41.1%    713,820     2 MB ( 2.2 B/row)   137 s

        i.e. the cap buys back 98.5% of the residency for 0.4% of the hits. At 315 B/entry
        (measured) the cap is 6.3 MB per underlying, so a 98-symbol universe holds ~617 MB
        where the columns held 7.1 GB.

        FIFO, NOT LRU. A backtest walks its window forward; the row read longest ago is the
        one that will not be asked for again, and ``next(iter(memo))`` keeps the HIT path free
        of the ``move_to_end`` a true LRU would put on it. An evicted row recomputes
        identically — this is a memo of a pure function (see ``greeks_tuple``).

        Fresh objects rather than in-place clears, so the allocator can hand the pages back.
        """
        self._spot_cache: Dict[int, Optional[float]] = {}
        self._bar_memo: Dict[int, Dict[str, object]] = {}
        self._g_memo: Dict[int, Tuple[Optional[float], ...]] = {}

    # -- as-of clamp ----------------------------------------------------
    # ``bisect`` over the ``array('i')`` buffer rather than ``np.searchsorted`` over an array
    # SLICE: identical answers (the rows are sorted by (occ_symbol, bar_date), so contract
    # ci's bar ordinals ascend across starts_l[ci]:stops_l[ci]) at 0.05 us instead of 0.73 us,
    # and this runs once per contract inside every get_chain / get_atm_iv. ``array('i')``
    # yields plain python ints on indexing, so every comparison and subtraction below is
    # exactly what it was when this was a list (see _RawUnderlying._bind for why it is not).
    def latest_row_on_or_before(self, ci: int, as_of_ord: int) -> int:
        """Row index of contract ``ci``'s latest bar dated <= ``as_of_ord``, or -1."""
        lo = self.starts_l[ci]
        j = bisect_right(self.bar_ord_l, as_of_ord, lo, self.stops_l[ci])
        return j - 1 if j > lo else -1

    def exact_row(self, ci: int, as_of_ord: int) -> int:
        """Row index of contract ``ci``'s bar dated EXACTLY ``as_of_ord``, or -1."""
        hi = self.stops_l[ci]
        j = bisect_left(self.bar_ord_l, as_of_ord, self.starts_l[ci], hi)
        if j >= hi or self.bar_ord_l[j] != as_of_ord:
            return -1
        return j

    # -- greeks ---------------------------------------------------------
    def _spot_on(self, bar_ord: int, spot_source) -> Optional[float]:
        """The run's close for this bar's date, memoised per DATE (not per row).

        The memo holds a float, never the source: see the module docstring on why the spot
        source itself must not end up in a worker-lifetime cache.
        """
        v = self._spot_cache.get(bar_ord, _MISSING)
        if v is _MISSING:
            v = spot_source(self.underlying, self.raw.date_of_ord[bar_ord])
            self._spot_cache[bar_ord] = v
        return v

    def greeks_tuple(self, i: int, ci: int, spot_source) -> Tuple[Optional[float], ...]:
        """(iv, delta, gamma, theta, vega) for row ``i``, inverted from its own close.

        Memoised per row in a BOUNDED dict (``_GREEKS_MEMO_MAX``), because the reuse this
        memo exists for is LOCAL: a bar's chain read, its ``get_atm_iv`` DTE-band rescan and
        its held-lot ``get_bar`` calls all land on the same few hundred rows and the next bar
        moves on. Measured on the real tree, 52% of calls are hits on a 20-60 DTE walk and a
        5,000-entry cap captures every one of them (174,830 misses against an unbounded
        174,654). See ``_fresh_run_fill`` for why this is a dict and not five columns, and for
        the numbers that decided the cap.

        ``compute_iv_and_greeks`` is the ONE greeks path (11.2 us/call measured), the same one
        ``fetch_options.bar_to_row`` used to fill the sqlite store's bars. A miss is that call;
        an eviction therefore costs 11.2 us and CHANGES NO VALUE — the inputs (the row's close,
        its date's spot, the contract's strike/expiry/right, the run's rate) are all immutable
        for the life of the overlay, which is the whole licence for evicting at all.

        A TUPLE, not a dict, because every caller of this is per-contract-per-bar. Rebuilding
        a 5-key dict here cost 1.7 us on a MEMO HIT — 0.27 us of it per ``np.isnan`` on a
        numpy scalar, which is why ``_f`` now tests ``v != v`` instead (0.019 us). The tuple is
        stored FINISHED (``_f`` applied at fill), so a hit is a dict lookup and a return.
        """
        memo = self._g_memo
        t = memo.get(i)
        if t is None:
            px = self.close[i]
            bar_ord = self.bar_ord_l[i]
            spot = self._spot_on(bar_ord, spot_source)
            t_days = self.raw.c_expiry_ord_l[ci] - bar_ord
            out = compute_iv_and_greeks(
                None if px != px else float(px), spot, self.raw.c_strike_f[ci],
                t_days / 365.0, self.rate, self.raw.c_right[ci])
            # ``_f`` HERE, on the MISS branch, not on every return. It is what preserves the
            # old columns' semantics exactly: they stored NaN for a None greek and `_f` mapped
            # NaN back to None on the way out, so a greek that came back as a COMPUTED NaN
            # (rather than None) was reported as None too. Storing the raw dict values would
            # quietly start returning that NaN. 5 x 0.019 us against an 11.2 us compute.
            t = (_f(out["iv"]), _f(out["delta"]), _f(out["gamma"]),
                 _f(out["theta"]), _f(out["vega"]))
            memo[i] = t
            while len(memo) > _GREEKS_MEMO_MAX:
                del memo[next(iter(memo))]
        return t

    def delta_iv_of_row(self, i: int, ci: int, spot_source
                        ) -> Tuple[Optional[float], Optional[float]]:
        """(delta, iv) only — get_atm_iv's hot path."""
        g = self.greeks_tuple(i, ci, spot_source)
        return g[1], g[0]

    # -- materialisation ------------------------------------------------
    def bar_dict(self, i: int, ci: int, spot_source) -> Dict[str, object]:
        """One bar in the dict shape the engine reads off the sqlite store.

        Keys ``open/high/low/close/volume/underlying/option_type/strike/expiry/date`` plus the
        computed ``iv/delta/gamma/theta/vega`` are exactly ``options_cache._BAR_COLS``; the
        parquet-only ``open_interest`` and ``vendor_iv`` are additions nothing reads yet.

        MEMOISED PER ROW, and the memo is what makes this affordable: ``get_bar`` is called
        for every held lot on every bar (MTM, liquidation, fill, expiry settlement) and a row
        is IMMUTABLE once the underlying is cached, so the same 17-key dict was being rebuilt
        — two ``date.fromordinal().isoformat()`` conversions, seven ``np.isnan`` NaN tests and
        a fresh dict — every single time. Measured 5.4 us standalone; a memo hit plus the
        ``copy()`` below is ~0.1 us.

        A COPY, not the memo itself: callers get a dict and none of them currently mutate it,
        but handing out the cached object would make that a silent cross-call corruption
        rather than a local bug, and ``dict.copy()`` on 17 keys is 0.054 us.

        AND CAPPED, at ``_BAR_MEMO_MAX``. Unbounded it was 821 B of RESIDENT memory per row
        the run ever read a bar for — five times what the same row costs in the five greek
        columns (151 B measured), and the overlay's largest per-touched-row cost. Eviction is
        INSERTION-ORDERED, not LRU: a backtest walks its window forward and every one of the
        nine ``_options.get_bar`` call sites in ``backtest_account.py`` keys on
        ``self._as_of_date()``, so a row's re-reads all fall on one bar date and the oldest
        entry is exactly the one that will not be asked for again.

        A PLAIN DICT, not an ``OrderedDict``. Both preserve insertion order on every Python
        BA2 runs, so ``next(iter(memo))`` is the oldest key either way -- but ``OrderedDict``
        carries a linked-list node per entry and its ``get`` is measurably slower on the HIT
        path this method exists for: 311 ns against 245 ns, a 27% regression on a path taken
        once per held lot per bar per MTM/fill/liquidation/settlement site. The eviction is on
        the MISS branch, which already costs 5.4 us, so it can afford the ``next(iter(...))``
        scan of one entry. An evicted row rebuilds identically -- this is a memo of a pure
        function of immutable columns, so the cap costs 5.4 us and changes no value.
        """
        d = self._bar_memo.get(i)
        if d is None:
            raw = self.raw
            iv, delta, gamma, theta, vega = self.greeks_tuple(i, ci, spot_source)
            d = {
                "iv": iv, "delta": delta, "gamma": gamma, "theta": theta, "vega": vega,
                "occ_symbol": raw.c_occ[ci],
                "date": raw.iso_of_ord[self.bar_ord_l[i]],
                "open": _f(self.open[i]), "high": _f(self.high[i]),
                "low": _f(self.low[i]), "close": _f(self.close[i]),
                "volume": _i(self.volume[i]),
                "underlying": self.underlying,
                "option_type": raw.c_type_str[ci],
                "strike": raw.c_strike_f[ci],
                "expiry": raw.c_expiry_iso[ci],
                "open_interest": _i(self.open_interest[i]),
                "vendor_iv": _f(self.vendor_iv[i]),
            }
            memo = self._bar_memo
            memo[i] = d
            while len(memo) > _BAR_MEMO_MAX:
                del memo[next(iter(memo))]
        return d.copy()

    def contract(self, i: int, ci: int, spot_source) -> OptionContract:
        raw = self.raw
        iv, delta, gamma, theta, vega = self.greeks_tuple(i, ci, spot_source)
        close = _f(self.close[i])
        bid, ask = _f(self.bid[i]), _f(self.ask[i])
        if bid is None and ask is None:
            # ZERO-SPREAD PREMIUM PROXY -- the pre-2026-09 behaviour, still used for stores
            # with no quote columns (the whole TastyTrade tree) and identical in effect to the
            # sqlite store (bid == ask on every one of its quoted rows). It makes spread_pct a
            # constant 0.0, which RANKS BEST rather than not ranking, so max_spread_pct gates
            # nothing and w_spread scores every candidate alike -- see the module docstring's
            # bid/ask bullet. A store WITH real quotes (ThetaData) takes the branch above and
            # those two knobs start working.
            #
            # BOTH SIDES, NOT EITHER: the proxy answers "this store quotes nothing", which is a
            # property of the store, not of the row. A HALF-quoted row (ThetaData publishes one
            # side and not the other) keeps the side it really has and leaves the other None, so
            # spread_pct stays None and _minimise scores it _WORST. Substituting close for the
            # missing side instead would discard a real bid AND manufacture a 0.0 spread, which
            # normalises to the BEST rank -- turning a correctly-handled "unknown" into a
            # top-ranked fabrication, the exact fail-open _minimise's docstring exists to stop.
            bid = ask = close
        vol = _i(self.volume[i])
        return OptionContract(
            symbol=raw.c_occ[ci], underlying=self.underlying,
            option_type=raw.c_right[ci],
            strike=raw.c_strike_f[ci],
            expiry=raw.c_expiry_date[ci],
            # `last` stays the TRADE price and is None on a day the contract did not trade;
            # the mark for such a row is the quote mid, which OptionContract.mid derives from
            # the real bid/ask above. Never substitute the mid into `last` -- callers use the
            # two to tell an actual print from a quote.
            bid=bid, ask=ask, last=close,
            implied_volatility=iv, delta=delta, gamma=gamma, theta=theta, vega=vega,
            open_interest=_i(self.open_interest[i]),
            # NO BAR => impossible here (a clamped row is always a bar), but an absent volume
            # is still a KNOWN zero: a bar exists only for a contract that traded. Same rule
            # as options_provider._bar_volume.
            volume=0 if vol is None else vol)


def _f(v) -> Optional[float]:
    """numpy scalar -> float, NaN -> None.

    ``v != v`` rather than ``np.isnan(v)``: identical on every float and 14x cheaper
    (0.019 us vs 0.272 us measured on a np.float64), and this runs 12x per materialised bar.
    """
    return None if v is None or v != v else float(v)


def _i(v) -> Optional[int]:
    return None if v is None or v != v else int(v)


def derived_key_for_underlying(underlying: str) -> str:
    """The derived-cache key one underlying's arrays live under.

    ``u_`` PREFIX, and it is not cosmetic. A bare symbol goes through
    ``shared_arrays._safe_key``, which REFUSES the Windows reserved device stems -- PRN and AUX
    are real tickers, the parquet store stores them happily, and a ValueError out of here kills
    the trial (on Linux too, since the refusal is in the key sanitiser, not in the filesystem).
    ``u_PRN`` is an ordinary name. Prefixing at the call site is exactly what shared_arrays' own
    docstring asks of a caller whose keys are symbols.

    NOT claimed: this does not separate two symbols the sanitiser would merge (``u_BRK/B`` still
    cleans to ``u_BRK_B``). It cannot arise through this store -- the symbol is a DIRECTORY name
    in the parquet tree, so a symbol with a separator in it has no partitions to read at all.

    MODULE-LEVEL AND PUBLIC because ``tools/build_shared_arrays.py`` reports which underlyings a
    prewarm BUILT and which it merely opened by looking these keys up on disk. A second spelling
    of the format there would report on directories the reader does not use, and the prewarm
    would look complete while every trial still rebuilt.
    """
    return f"u_{underlying.upper()}.v{_RawUnderlying.ARRAYS_VERSION}"


def _load_raw_underlying(root: str, underlying: str) -> "_RawUnderlying":
    """One underlying's arrays, through the PER-HOST derived cache.

    The parquet is parsed by the first process on the host that wants this underlying at this
    source signature; every later process (and every later cold cache in this one) memory-maps
    what that build published. That is why the parquet read sits behind ``build_or_open``
    rather than in front of it: at the 2020 ThetaData window the private arrays were ~15.6 GB
    PER WORKER of byte-identical data, which is what OOM-killed a 251 GB host at 16 workers.

    The SOURCES are the partition files themselves, enumerated ONCE and then both signed and
    read: ``partition_paths`` produces the list, ``read_underlying`` is handed that same list
    rather than globbing again. The tree is written by a warm-up that runs for hours, so two
    globs are not the same set, and arrays built from the second while signed with the first
    would never be invalidated. Any re-warm of this underlying therefore moves the signature
    and the stale arrays become unreachable rather than merely old.

    MEMORY, WHICH IS THE POINT AND ALSO THE TRAP. ``build_or_open`` serialises cold builders
    per KEY, not per host: 24 workers starting cold on 24 DIFFERENT underlyings run 24
    concurrent builds. A build's transient peak is ~2.3x the frame (~835 B/row measured:
    TSLA's 1.48M rows peak at 1.24 GB; the ThetaData TSLA at 7.6M rows is ~7-8 GB), so a cold
    grid can OOM a host that the steady state fits in comfortably. Prewarming the tree with
    ``tools/build_shared_arrays.py`` at ``--jobs 3-4`` before launching a grid is a
    precondition, not an optimisation.

    ``BA2_SHARED_ARRAYS=0`` restores the private path: the same frame, parsed privately,
    writing nothing. The one thing it does not restore is the ORDER of the two store calls —
    the no-partition early return below now happens before any read, on both paths.
    """
    from ba2_common.core import shared_arrays as _sa
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    store = OptionHistoryParquetStore(root=root)
    parts = store.partition_paths(underlying)
    if not parts:
        # No sources means no signature, so this never reaches the derived store at all.
        # It also covers every empty case: the store writes NO parquet for a partition with
        # no bars (write_partition removes the file and records EMPTY in the manifest), so a
        # zero-row partition file cannot exist and "has partitions but no rows" is
        # unreachable. That is why the coverage log below needs no `else` branch.
        logger.warning("[backtest] parquet option store: NO partitions for %s under %s — "
                       "every chain read for it will be empty.", underlying, root)
        return _RawUnderlying(underlying, None)

    def _build() -> Dict[str, np.ndarray]:
        # `parts`, not another glob: the files that were SIGNED are the files that are read.
        return _RawUnderlying.arrays_from_frame(store.read_underlying(underlying, parts))

    derived = _sa.DerivedArrayStore(_sa.derived_root_for(root))
    u = _RawUnderlying.from_arrays(
        underlying, derived.build_or_open(derived_key_for_underlying(underlying), parts, _build))
    # COVERAGE, STATED ONCE PER UNDERLYING PER WORKER. The vendor's history FLOOR bounds what
    # COULD have been downloaded; it says nothing about what this tree actually holds, and a
    # run outside the downloaded window reads an empty store and reports the resulting
    # zero-trade result as a result. That is the failure the floor seam exists to prevent, one
    # level down, and it is not detectable from the floor. One log line per underlying is the
    # cheapest honest signal: a 2024 run against a 2023-only tree says so in the first
    # screenful instead of at the post-mortem. Said per PROCESS, not per build: the arrays are
    # mapped by workers that never ran the build and are owed the same statement.
    if u.n_rows:
        logger.info("[backtest] parquet option store: %s %d bars / %d contracts, %s..%s",
                    underlying, u.n_rows, len(u.c_occ),
                    date.fromordinal(int(u.bar_ord.min())).isoformat(),
                    date.fromordinal(int(u.bar_ord.max())).isoformat())
    return u


def _raw_underlying(root: str, underlying: str) -> "_RawUnderlying":
    """The parquet bytes for (root, underlying), read at most once per worker.

    NO spot scope and NO rate in this key — see the module docstring's CACHING section. This
    is the cache that stops a screener GA (whose ``enabled_instruments``, and therefore whose
    ``spot_scope``, changes per individual) from re-reading identical bytes per trial.
    """
    key = (root, underlying)
    raw = _WORKER_RAW_CACHE.get(key)
    if raw is not None:
        _WORKER_RAW_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return raw
    raw = _load_raw_underlying(root, underlying)
    _WORKER_RAW_CACHE[key] = raw
    while len(_WORKER_RAW_CACHE) > _UNDERLYING_CACHE_MAX:
        _WORKER_RAW_CACHE.popitem(last=False)
    return raw


def _underlying(root: str, underlying: str, rate: float, spot_scope: str) -> _Underlying:
    """The run-scoped greeks/bar overlay for (root, underlying, rate, spot_scope)."""
    key = (root, underlying, rate, spot_scope)
    hist = _WORKER_UNDERLYING_CACHE.get(key)
    if hist is not None:
        _WORKER_UNDERLYING_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return hist
    hist = _Underlying(_raw_underlying(root, underlying), rate)
    _WORKER_UNDERLYING_CACHE[key] = hist
    while len(_WORKER_UNDERLYING_CACHE) > _UNDERLYING_CACHE_MAX:
        _WORKER_UNDERLYING_CACHE.popitem(last=False)
    return hist


def _as_date(when: Any) -> Optional[date]:
    """A date/datetime/ISO string -> date. The refinement seam is handed all three."""
    if when is None:
        return None
    if isinstance(when, datetime):
        return when.date()
    if isinstance(when, date):
        return when
    try:
        return datetime.fromisoformat(str(when)[:19].replace(" ", "T")).date()
    except ValueError:
        return None


class ParquetOptionsProvider:
    """The parquet backend of the option-reader seam. See the module docstring."""

    def __init__(self, root: str, *, spot_source: Callable[[str, date], Optional[float]],
                 risk_free_rate: float, spot_scope: str):
        """``spot_scope`` — the identity of what ``spot_source`` will answer.

        REQUIRED, and it is the one non-obvious argument. The worker-level cache holds the
        greeks overlay, and those greeks are a function of the underlying closes the run's
        price source serves. Two runs in the same long-lived pool worker can hold price
        sources over DIFFERENT windows/universes: run A preloaded Jan-Feb, run B Jan-Mar, and
        run B reusing run A's cached overlay would silently invert every March bar against a
        FEBRUARY spot (``close_asof`` forward-fills past the end of what it has). The scope
        makes that a cache miss instead of a wrong number.

        It keys the GREEKS ONLY. The parquet bytes underneath are the same bytes for every
        scope and are cached separately on (root, underlying); see the module docstring.

        ``build_options_provider`` derives it from the same (universe, interval, window,
        warmup) tuple ``price_source.evict_memo_if_working_set_changed`` keys the OHLCV memo
        on — precisely the tuple over which ``close_asof`` is a pure function. A GA's trials
        share it when the universe is fixed; a screener GA varies ``enabled_instruments`` per
        individual and so varies the scope too, which is exactly the case the byte-level cache
        below now absorbs.
        """
        if not os.path.isdir(root):
            raise OptionsCacheMiss(
                f"No TastyTrade option parquet store at {root}. Build it with "
                f"`python tools/warm_options_history.py` (or point "
                f"BACKTEST_OPTIONS_PARQUET_ROOT at an existing tree). Refusing to run an "
                f"options backtest against an absent store — it would trade nothing and "
                f"report it as a result.")
        self.root = root
        #: HELD HERE, NOT IN THE WORKER CACHE — it closes over the run's AsOfPriceSource (and
        #: therefore that run's whole OHLCV memo), and this object dies with the run while the
        #: caches outlive it. See the module docstring.
        self.spot_source = spot_source
        self.spot_scope = str(spot_scope)
        self.risk_free_rate = float(risk_free_rate)
        #: Parallel to HistoricalOptionsProvider.db_path: the identity this store's worker
        #: caches are keyed on.
        self.store_path = root

    # -- the engine-facing methods --------------------------------------
    def get_chain(self, underlying: str, as_of: date, *, expiry_min: date, expiry_max: date,
                  option_type: Optional[OptionRight] = None, strike_min: Optional[float] = None,
                  strike_max: Optional[float] = None) -> List[OptionContract]:
        u = self._u(underlying)
        if not u.n_rows:
            return []
        as_of_ord = as_of.toordinal()
        keep = ((u.c_expiry_ord >= expiry_min.toordinal())
                & (u.c_expiry_ord <= expiry_max.toordinal()))
        if option_type is not None:
            keep &= (u.c_is_call if option_type == OptionRight.CALL else ~u.c_is_call)
        if strike_min is not None:
            keep &= (u.c_strike >= strike_min)
        if strike_max is not None:
            keep &= (u.c_strike <= strike_max)
        spot_source = self.spot_source
        out: List[OptionContract] = []
        for ci in np.flatnonzero(keep):
            ci = int(ci)
            i = u.latest_row_on_or_before(ci, as_of_ord)
            if i < 0:
                continue  # the contract had not traded yet on/before the clock: not in the chain
            out.append(u.contract(i, ci, spot_source))
        return out

    def get_quote(self, occ_symbol: str, as_of: date) -> Optional[OptionQuote]:
        u = self._u(_underlying_of(occ_symbol))
        ci = u.c_index.get(occ_symbol)
        if ci is None:
            return None
        i = u.exact_row(ci, as_of.toordinal())
        if i < 0:
            return None
        close = _f(u.close[i])
        bid, ask = _f(u.bid[i]), _f(u.ask[i])
        if bid is None and ask is None:
            bid = ask = close
        # Same pricing rule get_chain's ``contract()`` uses, and it MUST stay a twin: entry
        # actions price off chain rows while close actions price off quotes, and the two must
        # agree (options_provider bug B4). Real quotes when the store has them, the
        # zero-spread close proxy when it does not; `last` is the trade print either way.
        #
        # GREEKS TOO (plan Task 6), from the SAME per-row inversion ``get_chain`` uses -- the
        # sqlite reader's twin change, and it must be a twin or a rule that reads a held
        # contract's delta answers differently on the two backends. Memoised per row by
        # ``greeks_tuple``, so a quote costs no extra inversion once the chain has priced it.
        delta, iv = u.delta_iv_of_row(i, ci, self.spot_source)
        return OptionQuote(symbol=occ_symbol, bid=bid, ask=ask, last=close,
                           delta=delta, implied_volatility=iv)

    def get_bar(self, occ_symbol: str, as_of: date) -> Optional[dict]:
        u = self._u(_underlying_of(occ_symbol))
        ci = u.c_index.get(occ_symbol)
        if ci is None:
            return None
        i = u.exact_row(ci, as_of.toordinal())
        return None if i < 0 else u.bar_dict(i, ci, self.spot_source)

    def get_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        """NEAR-ATM implied volatility (0-1), by the SAME rule as the sqlite reader.

        CALLS only, |delta| nearest 0.50 over a 20-45 DTE window, tie-broken by nearest
        expiry then lowest strike; iv and delta both come from the AS-OF-CLAMPED bar's own
        Black-Scholes inversion, with no fallback to anything whose date is unknown. See
        ``options_provider.get_atm_iv`` for why that rule (and its divergence from live) is
        what it is — this reader must not answer a DIFFERENT question from the other backend.
        """
        cache_key = (self.root, underlying, self.risk_free_rate, self.spot_scope,
                     as_of.toordinal())
        cached = _WORKER_ATM_IV_CACHE.get(cache_key, _MISSING)
        if cached is not _MISSING:
            _WORKER_ATM_IV_CACHE.move_to_end(cache_key)  # LRU: mark most-recently-used
            return cached
        result = self._compute_atm_iv(underlying, as_of)
        _WORKER_ATM_IV_CACHE[cache_key] = result
        while len(_WORKER_ATM_IV_CACHE) > _ATM_IV_CACHE_MAX:
            _WORKER_ATM_IV_CACHE.popitem(last=False)
        return result

    def delta_at_entry(self, underlying: str, occ_symbol: str, when: Any) -> Optional[float]:
        """The contract's delta AS OF ``when`` — the intraday-drawdown refinement's one
        option-specific input (``results._build_refine_drawdown_fn``).

        Same as-of discipline as ``get_chain``: the contract's LATEST bar on or before that
        date, never a later one. ``when`` may be a datetime (what the refinement holds), a
        date, or an ISO string; an unparseable one is None rather than an exception, because
        this is a refinement and must never fail a finished run.

        Deliberately NOT ``get_chain(...)``-then-search: the refinement asks about ONE named
        contract, and building a whole chain (and every greek in it) to read one delta is what
        makes a refinement expensive enough to be worth switching off.
        """
        d = _as_date(when)
        if d is None:
            return None
        u = self._u(underlying)
        ci = u.c_index.get(occ_symbol)
        if ci is None:
            return None
        # STRICTLY BEFORE the entry date (`- 1`), never the entry day's own bar. A daily bar is
        # dated at the CLOSE, so including it returned a delta that had already absorbed the
        # whole session -- and this refinement only ASKS about trades flagged because the
        # underlying moved, so that delta embeds the very move whose drawdown is being
        # estimated. Calling it "delta at entry" is circular, and it feeds
        # strategy_fitness.option_consistent_annual_return.
        #
        # The prior session's delta is stale, not wrong: it describes a real market state that
        # preceded the entry. Staleness is a bounded approximation; lookahead is not. A later
        # refinement can reprice delta causally (prior-snapshot IV + the underlying price at
        # entry + remaining time to expiry) to close the staleness gap without reintroducing
        # the close.
        i = u.latest_row_on_or_before(ci, d.toordinal() - 1)
        if i < 0:
            # No PRIOR snapshot. None, never 0.0 -- a zero delta claims the premium does not
            # move with the underlying, which would silently report a refined drawdown of
            # exactly the daily one. The caller counts this as uncovered.
            return None
        return u.greeks_tuple(i, ci, self.spot_source)[1]

    # -- internals ------------------------------------------------------
    def _u(self, underlying: str) -> _Underlying:
        return _underlying(self.root, underlying, self.risk_free_rate, self.spot_scope)

    def _compute_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        u = self._u(underlying)
        if not u.n_rows:
            return None
        as_of_ord = as_of.toordinal()
        lo = (as_of + timedelta(days=_ATM_DTE_MIN)).toordinal()
        hi = (as_of + timedelta(days=_ATM_DTE_MAX)).toordinal()
        keep = u.c_is_call & (u.c_expiry_ord >= lo) & (u.c_expiry_ord <= hi)
        spot_source = self.spot_source
        best: Optional[Tuple[Tuple[float, int, float], float]] = None
        for ci in np.flatnonzero(keep):
            ci = int(ci)
            i = u.latest_row_on_or_before(ci, as_of_ord)
            if i < 0:
                continue
            delta, iv = u.delta_iv_of_row(i, ci, spot_source)
            if delta is None or iv is None:
                continue
            key = (abs(abs(delta) - 0.5), u.raw.c_expiry_ord_l[ci], u.raw.c_strike_f[ci])
            if best is None or key < best[0]:
                best = (key, float(iv))
        return best[1] if best is not None else None


@lru_cache(maxsize=1 << 16)
def _underlying_of(occ_symbol: str) -> str:
    """The underlying encoded in an OCC symbol.

    ``get_bar``/``get_quote`` are handed a CONTRACT and the parquet store is partitioned by
    UNDERLYING, so the root has to be recovered from the symbol. The sqlite reader never
    needed this (its ``option_bar`` table carries an ``underlying`` column and is indexed on
    the contract), which is the one place the two backends genuinely differ in shape.

    ``parse_occ`` is the single OCC parser in the codebase and is used verbatim. A symbol it
    refuses is not an OCC contract, so it cannot be in this store: fall back to the
    fixed-width read (everything before the trailing 6 date digits + C/P + 8 strike digits)
    so a synthetic test symbol still routes to a plausible key rather than raising.

    MEMOISED because ``get_bar`` runs it for every held lot on every bar and the symbol set a
    run touches is small and repeats; the regex is otherwise pure overhead on a hot path.
    """
    try:
        return parse_occ(occ_symbol).underlying
    except ValueError:
        s = str(occ_symbol).strip().upper()
        return s[:-15] if len(s) > 15 else s
