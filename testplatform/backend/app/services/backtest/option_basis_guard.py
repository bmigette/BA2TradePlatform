"""E4 guard: refuse a backtest whose option spot is not in the chain's own basis.

BT/live option parity, ``docs/plans/2026-09-22-bt-live-option-parity.md`` Part E4.

WHY. The split-basis bug (option strikes as traded, FMP closes split-adjusted) mis-priced a
whole grid silently: nothing in a run compares the spot it selects strikes against with the
spot the chain itself implies. This does, on every chain read of a real options run: the
put-call-parity spot of the NEAREST expiry (DTE >= 2) the store quoted on that session --

    S_par = K + C_mid - P_mid     at the strike K where |C_mid - P_mid| is smallest

(the method of the 2026-09-22 measurement) against the as-traded spot the reader serves. A
basis error is a PERSISTENT multiple (x10, x40, x0.25, x1.06 for a missed spin-off), so the
test is the MEDIAN ratio of the checked session and up to ``WINDOW - 1`` earlier sessions the
store can evaluate: an isolated bad print (measured on the 97-symbol sample: MS 2020-03-25,
HSBC 2022-05-19, SAN 2020-03-19 at x2.0, ...) passes, a basis bug does not. Beyond
``TOLERANCE`` the run REFUSES with ``OptionSpotBasisMismatch`` naming symbol, date and ratio.

SAMPLED (perf gate, 2026-09-23: the every-read guard cost 1.1-1.7% of a trial). A symbol is
checked on (1) every session until its FIRST EVALUABLE one in the run, (2) the first session
on/after each split ex-date the run's split basis holds for it, and the session after that,
and (3) otherwise at most once every ``SAMPLE_EVERY`` sessions (a sampled session that is
unevaluable is retried on the next one). The median then runs over the LAST up-to-``WINDOW``
CHECKED sessions of the symbol (the current one included), not over consecutive store
sessions; only while fewer than ``WINDOW`` have been checked (the start of a run) is it filled
from earlier store sessions, as before. A persistent basis error is therefore refused by the
third sampled check that sees it at the latest (<= ~10 sessions), and it still aborts the run.

PURE FUNCTIONS OF THE STORE are memoised per worker (the parity spot of (underlying,
session) never changes); the comparison itself is per run (the spot depends on the run's
price source and split basis). Carry/dividend error is ignored: at the nearest expiry it is
far below the tolerance.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

#: Refuse when the median |S_par / S - 1| exceeds this. The plan's 5%.
TOLERANCE = 0.05
#: Sessions in the median: the checked one plus up to WINDOW-1 earlier evaluable ones. Five,
#: so up to two bad prints inside the window still pass while a basis error (every session
#: off by the same multiple) cannot.
WINDOW = 5
#: Sampling (module docstring): a symbol's routine check comes at most once per this many of
#: its sessions.
SAMPLE_EVERY = 5
#: How far back (store bar dates) the median may look for evaluable sessions.
_LOOKBACK_DATES = 15
#: Nearest expiries tried per session before the session counts as unevaluable.
_MAX_EXPIRIES = 4
#: Minimum days to expiry of the parity expiry (DTE 0/1 quotes are the noisiest).
_MIN_DTE = 2
#: MONEYNESS GATE (plan Part G2). The parity strike must lie within this fraction of the
#: parity spot it produced, else that expiry is skipped for the next one (up to
#: _MAX_EXPIRIES); with none usable the session is UNEVALUABLE (neither pass nor refuse). A
#: far-OTM single pair (a cheap ADR whose only two-sided pair is the $5 strike with the
#: underlying at $2.93 -- MUFG 2020-03-18, parity 2.925 vs 3.43 close) prices carry and
#: skew, not the spot: it produced the false refusals of the 2026-09-23 sweep (MUFG/SMFG/SAN).
#: Measured against the pair's OWN parity spot, so the gate is basis-independent: a real
#: basis error (x4, x10) moves the spot the run supplies, never |K / S_par - 1|.
MAX_STRIKE_DISTANCE = 0.10
#: QUOTE-WIDTH GATE (plan Part G2, from the 97-symbol replay). A pair is only used when the
#: most its mids can be wrong -- half the call spread plus half the put spread, which bounds
#: the parity error -- is within this fraction of the parity spot it gives: half the
#: tolerance, so quote noise alone can never reach a refusal. The replay's last two false
#: refusals (MUFG 2024-01-26, 2024-12-04) came from a $10 call quoted 0.10 x 1.30 on a $9
#: ADR: a 6% "basis error" that was only the width of the market. Basis-independent like the
#: moneyness gate (spreads and the pair's own parity spot are both in the chain's basis).
#: A store without quotes (close-only mids) has no spread and is not gated.
MAX_QUOTE_UNCERTAINTY = TOLERANCE / 2
#: A symbol the guard could not evaluate on more than this share of its checks is WARNED
#: about in the run's results: it traded without the protection (plan Part E4 review).
UNEVALUABLE_WARN_FRACTION = 0.90

_INDEX_CACHE_MAX = 256


class OptionSpotBasisMismatch(RuntimeError):
    """The spot a backtest's option path uses is not the basis its chain is quoted in."""


class _StoreIndex:
    """Per-underlying lookups derived from the raw store, built once per worker.

    NUMPY, AND NO REFERENCE TO THE RAW. The contracts sorted by expiry (``order``, int32) and
    the boundaries of each expiry's run in it (``exp_values`` / ``exp_starts``), found by
    ``searchsorted``; plus the store's distinct bar dates. Holding the raw here would keep an
    underlying's mapped columns alive after the worker's own caches let go of it
    (``_worker_release_memory`` / ``_release_option_overlays``). The index is cleared with the
    parquet reader's caches (``clear_worker_parquet_options_cache``) and by the worker
    release, and it is keyed on the raw's identity AND shape so a recycled ``id`` cannot serve
    another store's index."""

    __slots__ = ("order", "exp_values", "exp_starts", "bar_ords", "parity")

    def __init__(self, raw) -> None:
        exp = np.asarray(raw.c_expiry_ord)
        self.order = np.argsort(exp, kind="stable").astype(np.int32)
        values, starts = np.unique(exp[self.order], return_index=True)
        self.exp_values = values.astype(np.int64)
        self.exp_starts = np.append(starts, len(exp)).astype(np.int64)
        self.bar_ords = np.unique(np.asarray(raw.bar_ord)).astype(np.int64)
        #: session ordinal -> parity spot (or None: not evaluable). Pure in the store; at most
        #: one float per distinct bar date.
        self.parity: Dict[int, Optional[float]] = {}

    def contracts_of(self, j: int) -> np.ndarray:
        return self.order[self.exp_starts[j]:self.exp_starts[j + 1]]


_INDEX: "OrderedDict[tuple, _StoreIndex]" = OrderedDict()


def _index_key(raw) -> tuple:
    bo = raw.bar_ord
    return (raw.underlying, id(raw), int(raw.n_rows), len(raw.c_occ),
            int(bo[0]) if len(bo) else -1, int(bo[-1]) if len(bo) else -1)


def _index_for(raw) -> _StoreIndex:
    key = _index_key(raw)
    idx = _INDEX.get(key)
    if idx is not None:
        _INDEX.move_to_end(key)
        return idx
    idx = _StoreIndex(raw)
    _INDEX[key] = idx
    while len(_INDEX) > _INDEX_CACHE_MAX:
        _INDEX.popitem(last=False)
    return idx


def clear_basis_guard_cache() -> None:
    _INDEX.clear()


def basis_guard_cache_stats() -> Dict[str, Any]:
    """Entries and bytes held by the worker-level index (numpy buffers + parity floats)."""
    nbytes = sum(i.order.nbytes + i.exp_values.nbytes + i.exp_starts.nbytes + i.bar_ords.nbytes
                 for i in _INDEX.values())
    return {"indexes": len(_INDEX), "array_bytes": int(nbytes),
            "parity_entries": sum(len(i.parity) for i in _INDEX.values())}


def _half_spread(u, i: int) -> float:
    """Half the bid/ask spread of row ``i`` (0 for a close-only mid). Never negative: a
    crossed quote (ask < bid) must not subtract from the pair's uncertainty and loosen the
    width gate (``_mid`` already refuses a crossed quote; this does not rely on it)."""
    b, a = u.bid[i], u.ask[i]
    if b == b and a == a:
        return max(0.0, (float(a) - float(b)) / 2.0)
    return 0.0


def _mid(u, i: int) -> Optional[float]:
    b, a = u.bid[i], u.ask[i]
    if b == b and a == a:                  # both quoted (not NaN)
        b, a = float(b), float(a)
        if b > 0 and a >= b:
            return (a + b) / 2.0
        return None
    if b != b and a != a:                  # a store with no quotes at all: the close
        c = u.close[i]
        return float(c) if c == c and c > 0 else None
    return None                            # half-quoted: no honest mid


def parity_spot(u, session_ord: int) -> Optional[float]:
    """The put-call-parity spot of ``session_ord`` from the nearest expiry with a call/put
    pair quoted that session whose strike is within ``MAX_STRIKE_DISTANCE`` of the spot it
    implies (a far-OTM best pair moves on to the next expiry), or None when the store cannot
    say. Pairs quoted wider than ``MAX_QUOTE_UNCERTAINTY`` are never used. Memoised per
    worker."""
    idx = _index_for(u.raw)
    hit = idx.parity.get(session_ord, idx)
    if hit is not idx:
        return hit
    out: Optional[float] = None
    j = int(np.searchsorted(idx.exp_values, session_ord + _MIN_DTE, side="left"))
    tried = 0
    strike_f = u.raw.c_strike_f
    is_call = u.c_is_call
    while j < len(idx.exp_values) and tried < _MAX_EXPIRIES:
        calls: Dict[float, Tuple[float, float]] = {}
        puts: Dict[float, Tuple[float, float]] = {}
        for ci in idx.contracts_of(j).tolist():
            i = u.exact_row(ci, session_ord)
            if i < 0:
                continue
            m = _mid(u, i)
            if m is None:
                continue
            (calls if is_call[ci] else puts)[strike_f[ci]] = (m, _half_spread(u, i))
        best = None
        for k in calls.keys() & puts.keys():
            (cm, ch), (pm, ph) = calls[k], puts[k]
            par = k + cm - pm
            if not (par > 0) or ch + ph > MAX_QUOTE_UNCERTAINTY * par:
                continue
            gap = abs(cm - pm)
            if best is None or gap < best[0]:
                best = (gap, par, k)
        j += 1
        tried += 1
        # The nearest expiry whose best pair is near its own parity spot decides the session;
        # a far-OTM best pair (MAX_STRIKE_DISTANCE) hands over to the next expiry, and the
        # session is unevaluable only when none of the _MAX_EXPIRIES tried has a usable pair.
        if best is not None and best[1] > 0 and abs(best[2] / best[1] - 1.0) <= MAX_STRIKE_DISTANCE:
            out = float(best[1])
            break
    idx.parity[session_ord] = out
    return out


class _SymbolSampling:
    __slots__ = ("events", "next_event", "forced", "since", "evaluated", "history")

    def __init__(self, events: List[int]):
        self.events = events
        self.next_event = 0
        self.forced = 0
        self.since = 0
        self.evaluated = False
        #: the last WINDOW-1 checked (session, ratio), newest last
        self.history: "deque" = deque(maxlen=WINDOW - 1)


class BasisGuard:
    """Per-run state: which (underlying, session) were checked, and what it cost."""

    def __init__(self, spot_source: Callable[[str, date], Optional[float]],
                 split_dates: Optional[Callable[[str], Any]] = None):
        """``split_dates(symbol)`` -> the ex-dates of the splits in the run's verified basis
        for ``symbol`` (forced checks, see the module docstring); None = no forced checks."""
        self.spot_source = spot_source
        self.split_dates = split_dates
        self._checked: set = set()
        #: symbol -> _SymbolSampling
        self._sampling: Dict[str, "_SymbolSampling"] = {}
        #: sessions seen but not checked (sampled out)
        self.sampled_out = 0
        self.checks = 0
        self.unevaluable = 0
        self.outliers_passed = 0
        self.seconds = 0.0
        #: symbol -> [checks, unevaluable]
        self._per_symbol: Dict[str, List[int]] = {}
        self._warned: set = set()

    def stats(self) -> Dict[str, Any]:
        """Run totals, plus every symbol whose checks were > ``UNEVALUABLE_WARN_FRACTION``
        unevaluable -- each WARNED once per run with its symbol and fraction, because the
        guard gave that symbol no protection at all."""
        blind = {}
        for sym, (n, u) in sorted(self._per_symbol.items()):
            if n and u / n > UNEVALUABLE_WARN_FRACTION:
                blind[sym] = round(u / n, 4)
                if sym not in self._warned:
                    self._warned.add(sym)
                    logger.warning(
                        "[backtest] option basis guard: %s was UNEVALUABLE on %d of %d chain "
                        "reads (%.1f%%) -- no call/put pair quoted at the nearest expiry, so "
                        "the split-basis check never ran for it this run.", sym, u, n, 100 * u / n)
        return {"checks": self.checks, "unevaluable": self.unevaluable,
                "sampled_out": self.sampled_out,
                "outliers_passed": self.outliers_passed, "seconds": round(self.seconds, 6),
                "unevaluable_symbols": blind}

    def _ratio(self, u, underlying: str, session_ord: int) -> Optional[float]:
        par = parity_spot(u, session_ord)
        if par is None:
            return None
        spot = self.spot_source(underlying, u.raw.date_of_ord[session_ord]
                                if session_ord in u.raw.date_of_ord else date.fromordinal(session_ord))
        if spot is None or not (spot > 0):
            return None
        return par / float(spot)

    def _due(self, underlying: str, d: int) -> bool:
        """Advance ``underlying``'s sampling state by one session; True when it is checked."""
        st = self._sampling.get(underlying)
        if st is None:
            events = sorted({e.toordinal() for e in (self.split_dates(underlying) or ())}) \
                if self.split_dates is not None else []
            st = self._sampling[underlying] = _SymbolSampling(events)
            # ex-dates on/before the run's first session are covered by the first check
            while st.next_event < len(st.events) and st.events[st.next_event] <= d:
                st.next_event += 1
        else:
            passed = False
            while st.next_event < len(st.events) and st.events[st.next_event] <= d:
                st.next_event += 1
                passed = True
            if passed:
                st.forced = 2              # the ex-date session and the one after it
        st.since += 1
        if st.forced > 0:
            st.forced -= 1
            return True
        return (not st.evaluated) or st.since >= SAMPLE_EVERY

    def check(self, u, underlying: str, session: date) -> None:
        """Refuse (raise) when ``underlying``'s spot is off the chain's basis on ``session``.
        Sampled: see the module docstring."""
        key = (underlying, session.toordinal())
        if key in self._checked:
            return
        self._checked.add(key)
        t0 = time.perf_counter()
        try:
            if not self._due(underlying, key[1]):
                self.sampled_out += 1
                return
            st = self._sampling[underlying]
            self.checks += 1
            per = self._per_symbol.setdefault(underlying, [0, 0])
            per[0] += 1
            d = session.toordinal()
            r0 = self._ratio(u, underlying, d)
            if r0 is None:
                self.unevaluable += 1
                per[1] += 1
                return                     # st.since not reset: retried next session
            st.evaluated = True
            st.since = 0
            history = list(st.history)     # earlier CHECKED sessions, newest last
            st.history.append((session, r0))
            if abs(r0 - 1.0) <= TOLERANCE:
                return
            ratios: List[Tuple[date, float]] = [(session, r0)] + history[::-1]
            if len(ratios) < WINDOW:
                # start of the run: fill from the store sessions before the oldest checked one
                idx = _index_for(u.raw)
                oldest = ratios[-1][0].toordinal()
                k = int(np.searchsorted(idx.bar_ords, oldest - 1, side="right"))
                scanned = 0
                while k > 0 and len(ratios) < WINDOW and scanned < _LOOKBACK_DATES:
                    k -= 1
                    scanned += 1
                    prev = int(idx.bar_ords[k])
                    r = self._ratio(u, underlying, prev)
                    if r is not None:
                        ratios.append((date.fromordinal(prev), r))
            med = float(np.median([r for _, r in ratios]))
            if abs(med - 1.0) <= TOLERANCE:
                self.outliers_passed += 1
                return
            raise OptionSpotBasisMismatch(
                f"{underlying} {session.isoformat()}: the option path's spot is not in the "
                f"basis the chain is quoted in -- put-call-parity spot / as-traded spot = "
                f"{r0:.4f} (median {med:.4f} over "
                f"{[(d_.isoformat(), round(r_, 4)) for d_, r_ in ratios]}), tolerance "
                f"{TOLERANCE:.0%}. A split / spin-off missing from (or wrong in) the split "
                f"calendar, a mixed-basis FMP cache or a mis-mapped option ticker; every strike, "
                f"greek and intrinsic value of this run would be wrong. Refusing.")
        finally:
            self.seconds += time.perf_counter() - t0
