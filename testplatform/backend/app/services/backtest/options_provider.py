"""As-of-clamped reader over OptionsHistoryCache. Returns ONLY data dated <= the engine
clock (no lookahead). Chain rows are mapped to OptionContract; bars stay dicts.

Worker-process-level in-memory cache (2026-07-20 fix, mirrors price_source.py's
_WORKER_BAR_CACHE and FMPSenateTraderWeight's _WORKER_SCORING_CACHE): before this fix,
OptionsHistoryCache opened a FRESH sqlite3 connection on EVERY single read (read_chain,
read_bar, latest_chain_as_of, latest_bar_on_or_before), and a fresh HistoricalOptionsProvider
was built once per GA trial from the SAME shared cache file -- so one trial issued thousands of
tiny DB round-trips (one per bar per contract), repeated identically by every trial in the job
and by every worker process. The options cache file is large (2.6GB, ~14M bar rows across ~100
underlyings as of 2026-07-20) so it can't be loaded wholesale into a worker's memory the way
FMPSenateTraderWeight's scoring caches are -- instead this caches PER (db_path, underlying)
chain history and PER (db_path, occ_symbol) bar history, lazily on first access, shared across
every HistoricalOptionsProvider built in the same worker process for the rest of its life.
get_chain (filtered by expiry/strike) touches only the contracts passing its filters,
get_bar/get_quote a specific held contract, and get_atm_iv only its 20-45 DTE band
(2026-07-22 bug B7 -- before that it scanned EVERY contract in the underlying's cached
chain snapshot unfiltered). A WIDE get_chain filter on a single liquid underlying can
still pull 10-16k bar histories in one call (measured 2026-07-20: MU 15882, SNDK 15204,
AMD 13576), so the bar-history cache cap must comfortably exceed one underlying's full
chain width, not just "a handful", or an LRU sized too small THRASHES (evicts and reloads)
within a single call and defeats the cache entirely (measured: a 3000-entry cap made two
back-to-back identical trials equally slow, ~460s each, against the real 2.6GB cache). Bounded (not unbounded) because a REMOTE worker's trial-serving
process pool is long-lived across many different optimization jobs (see worker_server.py's
module-level _POOL), so keys could otherwise accumulate across jobs touching different
universes over the process's lifetime -- the default just needs to clear the realistic
single-underlying ceiling with headroom for a few underlyings at once."""
from __future__ import annotations
from bisect import bisect_right
from collections import OrderedDict
from datetime import date, timedelta
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.types import OptionRight
from .options_cache import OptionsHistoryCache

_CHAIN_CACHE_MAX = int(os.getenv("BT_OPTION_CHAIN_CACHE_MAX", "300"))
# Must clear one underlying's full chain width (measured max 15882, MU) with headroom for a
# few underlyings cached at once -- a wide-filter get_chain can touch most of the chain's bar
# histories in a SINGLE call (and get_atm_iv its 20-45 DTE band, post-2026-07-22 bug B7), so
# a much smaller cap thrashes (evict-then-reload) within one read.
_BAR_CACHE_MAX = int(os.getenv("BT_OPTION_BAR_CACHE_MAX", "50000"))

_WORKER_CHAIN_CACHE: "OrderedDict[Tuple[str, str], _ChainHistory]" = OrderedDict()
_WORKER_BAR_CACHE: "OrderedDict[Tuple[str, str], _BarHistory]" = OrderedDict()

# get_atm_iv RESULT memo, keyed (db_path, underlying, as_of.isoformat()) -> iv or None.
#
# The chain/bar caches above stop the SQL re-reads, but get_atm_iv still re-did its whole
# scan on every call: for one (underlying, as_of) it walks every CALL in the 20-45 DTE band
# and does a per-contract `_bar_history(...).latest_on_or_before(...)`. Measured at 56 ms per
# call, which is fine for a signal-driven expert (FMPRating only touches chains when a signal
# fires) but fatal for a PORTFOLIO SCANNER: PremiumSeller evaluates all ~98 universe symbols
# on every entry bar, so one trial costs ~98 x 440 bars x 56 ms ~= 40 min of pure IV lookups
# (plus a 4.7 min cold-start seed of 98 x 52 weekly samples). A GA run of ~640 trials never
# finished — an 80x8 job produced ZERO completed individuals in 34 minutes and every remote
# trial hit the 1800s timeout.
#
# Every GA trial re-evaluates the IDENTICAL (symbol, date) pairs — same universe, same window
# — so the memo is near-100% hit rate after the first trial in a worker process. Values are
# tiny (a float or None), so the cap is generous: 98 symbols x ~500 bars ~= 49k entries fits
# comfortably. None is cached too — a symbol with no usable chain returns None every time, and
# re-deriving that is exactly as expensive as a hit.
_ATM_IV_CACHE_MAX = int(os.getenv("BT_OPTION_ATM_IV_CACHE_MAX", "200000"))
_WORKER_ATM_IV_CACHE: "OrderedDict[Tuple[str, str, str], Optional[float]]" = OrderedDict()
# Distinct from None, which is a VALID cached result ("no usable chain for this symbol/date").
# A plain `.get(key) is None` miss-check would re-scan those every call — the worst case, since
# a symbol absent from the cache is scanned in full before returning None.
_MISSING = object()


def clear_worker_options_cache() -> None:
    """Drop every cached chain/bar history + ATM-IV result (test isolation / explicit reset).

    Also clears the PARQUET backend's worker caches. There are two readers behind one seam
    (see ``options_store.py``) and one reset entry point: every existing caller of this
    function means "forget everything the option readers cached", and leaving the second
    store's caches alive would make that promise quietly false. Imported at call time — this
    module must stay importable without pandas/numpy for the sqlite path.
    """
    _WORKER_CHAIN_CACHE.clear()
    _WORKER_BAR_CACHE.clear()
    _WORKER_ATM_IV_CACHE.clear()
    from .parquet_options_provider import clear_worker_parquet_options_cache

    clear_worker_parquet_options_cache()


class _ChainHistory:
    """One underlying's full chain history: every cached (as_of -> rows) snapshot."""
    __slots__ = ("by_asof", "dates", "_row_index")

    def __init__(self, by_asof: Dict[str, List[dict]]):
        self.by_asof = by_asof
        self.dates = sorted(by_asof)  # ascending, for bisect
        self._row_index: Dict[str, Dict[str, dict]] = {}  # as_of -> {occ_symbol: row}, lazy

    def latest_as_of(self, on_or_before: str) -> Optional[str]:
        i = bisect_right(self.dates, on_or_before)
        return self.dates[i - 1] if i else None

    def row_for(self, occ_symbol: str, on_or_before: str) -> Optional[dict]:
        """The chain row for one contract at the latest snapshot <= on_or_before, or None.

        O(1) after the first lookup per snapshot (the occ->row dict is built lazily and
        memoized). get_quote needs the snapshot row's bid/ask spread to synthesize
        point-in-time quotes around a bar close (see _pit_quotes)."""
        snap = self.latest_as_of(on_or_before)
        if snap is None:
            return None
        idx = self._row_index.get(snap)
        if idx is None:
            idx = {r["occ_symbol"]: r for r in self.by_asof[snap]}
            self._row_index[snap] = idx
        return idx.get(occ_symbol)


class _BarHistory:
    """One contract's full daily-bar history."""
    __slots__ = ("by_date", "dates")

    def __init__(self, by_date: Dict[str, dict]):
        self.by_date = by_date
        self.dates = sorted(by_date)  # ascending, for bisect

    def latest_on_or_before(self, on_or_before: str) -> Optional[dict]:
        i = bisect_right(self.dates, on_or_before)
        return self.by_date[self.dates[i - 1]] if i else None


def _load_chain_history(db_path: str, underlying: str) -> _ChainHistory:
    cx = sqlite3.connect(db_path)
    cx.row_factory = sqlite3.Row
    try:
        by_asof: Dict[str, List[dict]] = {}
        for r in cx.execute(
            "SELECT * FROM option_chain WHERE underlying=? ORDER BY as_of", (underlying,)
        ):
            by_asof.setdefault(r["as_of"], []).append(dict(r))
    finally:
        cx.close()
    return _ChainHistory(by_asof)


def _load_bar_history(db_path: str, occ_symbol: str) -> _BarHistory:
    cx = sqlite3.connect(db_path)
    cx.row_factory = sqlite3.Row
    try:
        by_date = {
            r["date"]: dict(r)
            for r in cx.execute(
                "SELECT * FROM option_bar WHERE occ_symbol=? ORDER BY date", (occ_symbol,)
            )
        }
    finally:
        cx.close()
    return _BarHistory(by_date)


def _chain_history(db_path: str, underlying: str) -> _ChainHistory:
    key = (db_path, underlying)
    hist = _WORKER_CHAIN_CACHE.get(key)
    if hist is not None:
        _WORKER_CHAIN_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return hist
    hist = _load_chain_history(db_path, underlying)
    _WORKER_CHAIN_CACHE[key] = hist
    while len(_WORKER_CHAIN_CACHE) > _CHAIN_CACHE_MAX:
        _WORKER_CHAIN_CACHE.popitem(last=False)
    return hist


def _bar_history(db_path: str, occ_symbol: str) -> _BarHistory:
    key = (db_path, occ_symbol)
    hist = _WORKER_BAR_CACHE.get(key)
    if hist is not None:
        _WORKER_BAR_CACHE.move_to_end(key)  # LRU: mark most-recently-used
        return hist
    hist = _load_bar_history(db_path, occ_symbol)
    _WORKER_BAR_CACHE[key] = hist
    while len(_WORKER_BAR_CACHE) > _BAR_CACHE_MAX:
        _WORKER_BAR_CACHE.popitem(last=False)
    return hist


def _pit_quotes(chain_row: Optional[dict],
                bar: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Derive POINT-IN-TIME (bid, ask, last) from a contract's daily bar (2026-07-22, bug B4).

    The chain row's bid/ask/last are a single snapshot written ONLY for the cache build's
    start date (see fetch_options.build_cache), so weeks into a backtest they are stale —
    sizing and limit prices must instead track the per-date bar. ``last`` = bar close.
    bid/ask = close ± half the SNAPSHOT's ABSOLUTE spread (ask − bid) when the chain row
    has both — modeling assumption: the historical cache has no as-of quote spreads
    (Alpaca exposes no historical NBBO), so the start-date spread is carried forward
    unchanged around each day's close (for fetch_options-built caches that spread is
    zero — bid=ask=last=close — making this an identity). When the chain row lacks
    bid/ask they stay None (fail-loud: never fabricate a spread) and only ``last`` is set.
    """
    close = bar.get("close")
    bid = chain_row.get("bid") if chain_row else None
    ask = chain_row.get("ask") if chain_row else None
    if close is None or bid is None or ask is None:
        return None, None, close
    half_spread = (ask - bid) / 2.0
    return close - half_spread, close + half_spread, close


def _bar_volume(chain_row: dict, bar: Optional[dict]) -> int:
    """Contracts traded on the as-of date: the bar's volume, else the chain column, else 0
    ("no bar" == "did not trade" == a known zero). See the note at the call site."""
    if bar is not None and bar.get("volume") is not None:
        return bar["volume"]
    v = chain_row.get("volume")
    return v if v is not None else 0


def _to_contract(r: dict, greeks_row: Optional[dict] = None) -> OptionContract:
    # greeks_row (the AS-OF-CLAMPED daily bar for this contract, when available) carries the
    # POINT-IN-TIME iv/greeks computed by fetch_options.py's Black-Scholes inversion of that
    # day's close (see option_greeks.py) — preferred over the chain row's, which is a single
    # snapshot fixed at the build's start date and goes stale as the backtest clock advances.
    # Only prefer it when its OWN iv actually computed (non-None) — a bar can exist with
    # iv/greeks still None (missing underlying close that day, or a pre-existing cache built
    # before this feature), in which case fall back to the chain row rather than lose greeks.
    g = greeks_row if (greeks_row and greeks_row.get("iv") is not None) else r
    # Quotes (bug B4): the chain row's bid/ask/last are the build's START-DATE snapshot and go
    # stale as the clock advances — when this contract has a bar on/before the as-of date,
    # derive point-in-time quotes from the bar close (see _pit_quotes). With no bar (or a
    # close-less bar) keep the chain row's snapshot quotes exactly as before.
    if greeks_row is not None and greeks_row.get("close") is not None:
        bid, ask, last = _pit_quotes(r, greeks_row)
    else:
        bid, ask, last = r.get("bid"), r.get("ask"), r.get("last")
    return OptionContract(
        symbol=r["occ_symbol"], underlying=r.get("underlying") or "",
        option_type=OptionRight(r["option_type"]), strike=r["strike"],
        expiry=date.fromisoformat(r["expiry"]), bid=bid, ask=ask,
        last=last, implied_volatility=g.get("iv"), delta=g.get("delta"),
        gamma=g.get("gamma"), theta=g.get("theta"), vega=g.get("vega"),
        open_interest=r.get("open_interest"),
        # VOLUME comes from the BAR, not the chain row (2026-07-25). option_chain.volume is
        # NULL for every row a fetch_options build writes (verified: 0 of 958,024 populated) —
        # Alpaca exposes no as-of volume for a past date, so the chain snapshot never gets one.
        # option_bar.volume IS populated for every bar (13,729,262 of 13,729,262), because a
        # bar only exists for a contract that actually traded that day. Reading the chain
        # column left OptionContract.volume permanently None, so any selector-side liquidity
        # gate keyed on volume silently passed EVERYTHING while the fill engine's
        # participation cap (_OPTION_FILL_MAX_VOLUME_PARTICIPATION) — which reads the bar
        # directly — rejected the resulting orders. Prefer the bar; fall back to the chain
        # column so a differently-built cache that does populate it still works.
        #
        # NO BAR => VOLUME 0, NOT None (2026-08-23). A bar exists only for a contract that
        # actually traded, so "no bar on or before the as-of date" is a KNOWN zero, not an
        # unknown. Reporting None conflated the two, and once
        # option_selector.check_liquidity_data_available started treating "no contract in the
        # chain publishes this field" as a configuration error (see
        # OptionLiquidityDataUnavailable), that conflation would have raised on any underlying
        # whose whole DTE window happened to be untraded — a real 100% rejection misreported
        # as a broken config. It also states, rather than accidentally implies, the second job
        # min_volume has been doing: no-bar rows are exactly the ones still carrying the cache
        # build's start-date quotes (weeks stale), so gating them out is deliberate.
        # Selection is unchanged — any min_volume >= 1 rejects 0 exactly as it rejected None.
        volume=_bar_volume(r, greeks_row))

class HistoricalOptionsProvider:
    def __init__(self, cache_db: str):
        self.cache = OptionsHistoryCache(cache_db)
        self.db_path = self.cache.db_path

    def get_chain(self, underlying: str, as_of: date, *, expiry_min: date, expiry_max: date,
                  option_type: Optional[OptionRight] = None, strike_min: Optional[float] = None,
                  strike_max: Optional[float] = None) -> List[OptionContract]:
        hist = _chain_history(self.db_path, underlying)
        snap = hist.latest_as_of(as_of.isoformat())
        if snap is None:
            return []
        out: List[OptionContract] = []
        for r in hist.by_asof[snap]:
            r = {**r, "underlying": underlying}
            exp = date.fromisoformat(r["expiry"])
            if exp < expiry_min or exp > expiry_max:
                continue
            if option_type is not None and r["option_type"] != option_type.value:
                continue
            if strike_min is not None and r["strike"] < strike_min:
                continue
            if strike_max is not None and r["strike"] > strike_max:
                continue
            greeks_row = _bar_history(self.db_path, r["occ_symbol"]).latest_on_or_before(as_of.isoformat())
            out.append(_to_contract(r, greeks_row))
        return out

    def get_quote(self, occ_symbol: str, as_of: date) -> Optional[OptionQuote]:
        bar = _bar_history(self.db_path, occ_symbol).by_date.get(as_of.isoformat())
        if bar is None:
            return None
        # Synthesize bid/ask the same way get_chain does (bug B4): entry actions price off
        # chain rows while close actions price off quotes, so both must see the same
        # point-in-time premium (bar close ± half the snapshot spread) or the two paths
        # disagree. A bar row always carries its underlying; if it (or the chain row) is
        # missing, bid/ask stay None and only last is set — never fabricated.
        chain_row = (_chain_history(self.db_path, bar["underlying"]).row_for(
            occ_symbol, as_of.isoformat()) if bar.get("underlying") else None)
        bid, ask, last = _pit_quotes(chain_row, bar)
        return OptionQuote(symbol=occ_symbol, bid=bid, ask=ask, last=last)

    def get_bar(self, occ_symbol: str, as_of: date) -> Optional[dict]:
        return _bar_history(self.db_path, occ_symbol).by_date.get(as_of.isoformat())

    def delta_at_entry(self, underlying: str, occ_symbol: str, when: Any) -> Optional[float]:
        """The contract's delta AS OF ``when`` — the ONE option-specific input
        ``results._build_refine_drawdown_fn`` needs for the intraday-drawdown refinement.

        A NAMED SEAM METHOD, not an attribute reach. This body used to live inside
        ``results.py`` as a closure over ``options.cache.db_path``, which only THIS reader
        has: on the parquet backend the whole refinement silently returned None, so
        ``max_drawdown`` (and therefore ``option_consistent_annual_return``) differed between
        the two stores for a reason invisible from the result. Both readers implement this
        now and the refinement follows the reader.

        Routes through the worker-cached chain history (bisect over a structure loaded once
        per underlying) rather than ``OptionsHistoryCache``'s raw methods, which open a fresh
        sqlite3 connection per call. The backtest's own pricing/entry path has already
        populated it for every underlying the trial touched.
        """
        as_of = when.strftime("%Y-%m-%d") if hasattr(when, "strftime") else str(when)
        hist = _chain_history(self.db_path, underlying)
        snapshot = hist.latest_as_of(as_of)
        if snapshot is None:
            return None
        for row in hist.by_asof.get(snapshot, []):
            if row.get("occ_symbol") == occ_symbol:
                return row.get("delta")
        return None

    def get_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        """NEAR-ATM implied volatility (0-1) for ``underlying`` as of ``as_of`` — feeds
        ``IVRankCondition``'s rolling history.

        Same SHAPE as live (AlpacaAccount.get_atm_implied_volatility): pick the ONE
        contract nearest the money in a 20-45 DTE expiry window and return ITS iv. The
        pre-2026-07-22 chain-wide MEAN (bug B7) averaged iv across all 10-16k cached
        contracts — skew- and expiry-contaminated — and fixing that restored the KIND of
        number, which is as far as the claim goes.

        NOT THE SAME STATISTIC, and no docstring here should imply otherwise. Live and
        this reader choose a DIFFERENT contract:

        * live: ``min(candidates, key=|strike - spot|)`` over the whole window, BOTH
          rights, all in-window expiries, first minimum wins in broker chain order — so
          which of the tied call/put/expiry it lands on is not even deterministic;
        * here: CALLS only, ``|delta|`` nearest 0.50, tie-broken by nearest expiry then
          lowest strike — fully deterministic.

        They agree closely for a liquid name with a strike near spot and diverge on wide
        strike ladders and steep skew. Consequence to hold on to: an ``iv_rank`` threshold
        tuned in a backtest transfers to live approximately, not exactly. Reconciling
        would mean giving live the deterministic rule (it is the one with the arbitrary
        tie-break), not giving this reader a spot it does not have.

        ATM PROXY (why the difference exists): this layer has no point-in-time underlying SPOT — the
        options cache stores no underlying-price column (see options_cache._CHAIN_DDL /
        _BAR_DDL) and the backtest's OHLCV store lives in price_source.py, not the
        provider — so "nearest the money" is proxied by the contract whose |delta| is
        nearest 0.50 among CALLS in the window (live picks the nearest strike to spot;
        |delta| ≈ 0.5 is the options-native definition of at-the-money). Calls only, for
        determinism — put iv at the same strike/expiry is near-identical by put-call
        parity, and live returns a single contract's iv, not a smoothed pair.

        iv/delta come ONLY from the as-of-clamped daily bar's Black-Scholes inversion
        (2026-08-26, OPT-C8). There is no fallback to the chain-snapshot row: its greeks
        carry no record of the date they were inverted from and can be LOOKAHEAD. See the
        note at the read site. Returns None when no cached snapshot exists, or when no
        in-window call has a clamped bar carrying both delta and iv — a genuine "not
        measurable today", which is what the callers already handle."""
        # Memoized on (db_path, underlying, as_of): the scan below is pure w.r.t. those three
        # (the cache file is immutable during a run), and a GA re-evaluates the same
        # (symbol, date) pairs on every trial. See _WORKER_ATM_IV_CACHE for why this matters.
        cache_key = (self.db_path, underlying, as_of.isoformat())
        cached = _WORKER_ATM_IV_CACHE.get(cache_key, _MISSING)
        if cached is not _MISSING:
            _WORKER_ATM_IV_CACHE.move_to_end(cache_key)  # LRU: mark most-recently-used
            return cached

        result = self._compute_atm_iv(underlying, as_of)
        _WORKER_ATM_IV_CACHE[cache_key] = result
        while len(_WORKER_ATM_IV_CACHE) > _ATM_IV_CACHE_MAX:
            _WORKER_ATM_IV_CACHE.popitem(last=False)
        return result

    def _compute_atm_iv(self, underlying: str, as_of: date) -> Optional[float]:
        """Uncached body of get_atm_iv (see its docstring for the selection rule)."""
        hist = _chain_history(self.db_path, underlying)
        snap = hist.latest_as_of(as_of.isoformat())
        if snap is None:
            return None
        expiry_min = as_of + timedelta(days=20)
        expiry_max = as_of + timedelta(days=45)
        best: Optional[Tuple[Tuple[float, date, float], float]] = None
        for r in hist.by_asof[snap]:
            if r["option_type"] != OptionRight.CALL.value:
                continue
            exp = date.fromisoformat(r["expiry"])
            if exp < expiry_min or exp > expiry_max:
                continue
            bar = _bar_history(self.db_path, r["occ_symbol"]).latest_on_or_before(as_of.isoformat())
            # NO FALLBACK TO THE CHAIN ROW (2026-08-26, OPT-C8). This used to read
            #     g = bar if (bar and bar.get("iv") is not None) else r
            # and the `else r` had no as-of guarantee. The BAR is clamped
            # (latest_on_or_before); the chain SNAPSHOT is clamped (latest_as_of); the
            # snapshot ROW's greeks are not clamped by either. fetch_options.build_cache
            # stamps every chain row `as_of = <build start>` but inverts its IV from
            # `(bar on start) or bar_rows[0]` -- the first bar ANYWHERE in the fetch window
            # when the contract did not trade on the build's start date, which can be months
            # later. The row records no trace of which date its IV came from.
            #
            # And the fallback was at its worst exactly where it fired: with no bar on or
            # before the clock, every bar the contract has is LATER than the clock, so the
            # chain row's IV is inverted from a future price BY CONSTRUCTION.
            #
            # Why not stamp the row with its inversion date and refuse it when that date is
            # after `as_of`? That needs a new option_chain column, and no existing cache has
            # one (the shared 10.9 GB file predates even the iv/delta columns), so every row
            # would read "provenance unknown" and be refused anyway -- the same behaviour as
            # this, plus a migration and a second thing to keep correct. The provenance is
            # not recoverable retrospectively; absent is the honest reading.
            #
            # Fails CLOSED, which the stack already copes with: this returns None when no
            # in-window call has a usable clamped iv+delta, and IVRankCondition treats an
            # unmeasurable IV as a refusal rather than as a zero. It costs precision (a
            # contract whose bar exists but whose own inversion failed is now skipped) and
            # buys the one cross-sectionally comparable option statistic being causal --
            # which matters more since iv_rank became a searched gene on 2026-08-26.
            #
            # `_to_contract` still carries the same fallback for the SELECTION path. That is
            # a wider behavioural change (it moves which contract every delta-method entry
            # picks, in every backtest) and is deliberately not made here.
            if bar is None:
                continue
            delta, iv = bar.get("delta"), bar.get("iv")
            if delta is None or iv is None:
                continue
            key = (abs(abs(delta) - 0.5), exp, r["strike"])
            if best is None or key < best[0]:
                best = (key, float(iv))
        return best[1] if best is not None else None
