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

CARRY IS NOT A BASIS ERROR (user decision 2026-09-26: fix the guard, no dated waivers; review
rounds 2 and 3 the same day). A hard-to-borrow fee (AMC July 2023) lowers the parity spot more
the longer the expiry; a cash dividend lowers every expiry after its ex-date. Both could read as
a 5-8% "basis error". When the uncorrected window median is off, the guard consults
``ba2_common.core.option_carry``:

* ONE-SIDED: carry only lowers the parity spot, so only a median BELOW 1 can be cleared; above
  1 + tolerance is refused as before.
* TERM: every window session re-measured with the carry-free spot (declared dividends added
  back, two usable expiries extrapolated to T = 0 under a leverage cap, the term structure
  sloping the way carry does). Cleared when >= ``MIN_CARRY_SESSIONS`` (at least 2) are
  measurable and their median lies in [``CARRY_BAND_LOW``, 1 + tolerance] -- the low side
  tightened by the most that may be added back unverified (2.5%): 0.975 .. 1.05 today.
  Otherwise the refusal stands.
* A VETO refuses by name: no dividend calendar, dividends to add back above 2.5% of the spot
  that the session's own chain does not show priced (a special the chain does not price would
  hide an error of its size; see VERIFIED ADD-BACK), a dividend row of unknown declaration date
  or amount, a term structure sloping up, and -- on a dividend payer -- a drop between the two
  expiries that the calendar's dividends do not explain within 2.5% of the spot (an uncalendared
  dividend, not carry).
* VERIFIED ADD-BACK: a dividend over the 2.5% cap is added back anyway when an expiry before its
  ex-date and one on/after it show a step of 0.5-1.5x the declared dividends of that gap
  (``option_carry.VERIFY_MIN/VERIFY_MAX``): STLA 2022-04 ($1.12, 7.5%, ex 04-19) killed a GA job
  as an unverified veto, and 116 of the 752 stage-2 names have such a dividend. An OCC-adjusted
  special shows no step and stays a veto; a basis error stays visible because the carry-free
  spot keeps the multiple. Residual risks are in the ``option_carry`` docstring.

KNOWN RESIDUAL (measured 2026-09-26 after the unexplained-drop veto; the first item accepted by
the coordinator the same day, REPAIR_LOG):
* a special of up to 2.5% of the spot that the chain does not price (OCC strike-adjusted) can
  carry an error ~1% past the 5% line (x1.051 passes on 2 of 3 start offsets) -- the same order
  of softness as the 5% line itself given its 2.5%-wide quote pairs;
* on a dividend payer an uncalendared dividend hides nothing past the line (x1.055 refused on
  every offset, dividends of 1-2.5%; larger ones are vetoed);
* a NON-PAYER (no dividend in the horizon: read as borrow, which AMC needs) is cleared only on
  sessions whose third usable expiry -- at least ``t2 - t1`` beyond the second -- lies on the
  line through the first two within 2.5% of the spot (``option_carry.LINEARITY_TOLERANCE``; review
  r6, decision D). A dividend FMP's calendar lacks ENTIRELY is a step that pushes the third off
  the line: 3-15% missing x x1.055-x1.20 are refused on every offset (they hid an error of their
  own size before). What remains is a step within the tolerance: 2.5% at the largest
  extrapolation leverage allowed (1.43) carries an error to x1.06 (x1.065 refused). Such a
  window clears on ``MIN_CARRY_FLOOR`` (2) confirmed sessions -- each is three collinear
  expiries, a stronger test than the two-point measure -- not ``MIN_CARRY_SESSIONS``: the real
  AMC 2023-07-20 window has exactly 2 (06-21, 06-28) beside three that lack a third expiry or
  bend the way a fading borrow fee does, and requiring 3 aborted the real AMC on 1 of 10
  sampled runs.
The stricter design (add a dividend back only when an expiry before the ex-date shows the drop)
IS built for dividends over the cap (VERIFIED ADD-BACK above); the unverified cap keeps the
residuals listed here.

There is no look-back to other sessions (a short-dated TIME measure was removed in review
round 3: a stretch-type basis error that no split marks would let it borrow clean sessions from
before the stretch). A basis error scales every expiry of a session alike, so it survives the
TERM measure; and since that is only consulted where the old check refused, every run that
passed before passes.

SOFT vs HARD REFUSAL (owner decision 2026-11-05; STLA 2022-04-08 ratio 0.93 with an unverifiable
7.6% dividend aborted a whole GA job). A would-be refusal is SOFT -- NO raise, NEW ENTRIES on that
underlying blocked instead (``check`` returns False; the provider's ``get_chain`` returns an empty
chain, the one place option entries and rolls pick contracts; marks and exits read quotes, never
the chain, so open positions are marked and closed normally) -- only when ALL hold:

* the window median lies in [``SOFT_BAND_LOW``, 1 - TOLERANCE): below 1 is the only side carry
  can explain, and the band is the size of the dividends / borrow seen in the universe;
* the symbol HAS a dividend calendar in the window (a carry explanation exists to be ambiguous);
* the carry separation failed only for want of PROOF: a veto (unverifiable/uncalendared dividend,
  ...) or an unmeasurable carry-free ratio. A carry-free median that WAS measured and still lies
  outside the band means the multiple survives the carry removal -- a real basis error, HARD.

Everything else stays HARD (``OptionSpotBasisMismatch``, JOB-FATAL exactly as before): median
above 1 + TOLERANCE, below ``SOFT_BAND_LOW``, no dividend calendar, a measured carry-free
mismatch. (The refused split basis is raised elsewhere and is untouched.) The block lasts until a
later SAMPLED check finds the window clear (or a clean ratio); it is re-evaluated like any check.

PERSISTENCE: a SOFT episode (first soft evaluation until the window clears) longer than
``SOFT_MAX_DAYS`` (60 calendar days) is escalated to the HARD refusal. A dividend or borrow carry
is transient; a basis error is not (constant docstring: measured episode lengths).

KNOWN RESIDUAL: a missed 5-15% spin-off on a dividend payer whose carry is unmeasurable is soft
and skips entries for up to ``SOFT_MAX_DAYS`` before the run aborts. That is accepted because it is recorded:
``stats()["soft_skips"]`` (per symbol: sessions skipped, sessions seen, share, first/last date,
reason, evaluations) lands in the trial's ``option_basis_guard`` result, and ``stats()`` logs one
WARNING listing every soft-skipped symbol, so a symbol skipped for most of a run is visible.

BT-ONLY by nature: live has no guard (it trades the broker's own chain and price); the BT/live
parity rule is untouched because the guard never changes a price, only withholds entries.

PURE FUNCTIONS OF THE STORE are memoised per worker (the parity spot of (underlying,
session) never changes); the comparison itself is per run (the spot depends on the run's
price source and split basis).
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
#: The carry separation (module docstring) clears a would-be refusal only when at least this
#: many sessions of the window (all of them, for a shorter window) have a carry-free ratio and
#: their median is within ``TOLERANCE`` -- the same window rule as the uncorrected ratio. A thin
#: chain's checked session may lack a second usable expiry while its neighbours have one (IONQ
#: 2022-03-28, monthlies only).
MIN_CARRY_SESSIONS = 3
#: ... and never fewer than this many, whatever the window (one session is not a median).
MIN_CARRY_FLOOR = 2


def _carry_band_low() -> float:
    from ba2_common.core.option_carry import MAX_ADDBACK_FRACTION
    return 1.0 - (TOLERANCE - MAX_ADDBACK_FRACTION)


#: The carry-free window median is only cleared inside [CARRY_BAND_LOW, 1 + TOLERANCE]. The low
#: side is tightened by the most the carry measure may add back UNVERIFIED
#: (``option_carry.MAX_ADDBACK_FRACTION``): an unpriced special can then carry an error at most
#: about that far past the 5% line (module docstring, KNOWN RESIDUAL).
CARRY_BAND_LOW = _carry_band_low()
#: A symbol the guard could not evaluate on more than this share of its checks is WARNED
#: about in the run's results: it traded without the protection (plan Part E4 review).
UNEVALUABLE_WARN_FRACTION = 0.90
#: Lower edge of the SOFT band (module docstring): a window median in [SOFT_BAND_LOW, 1 -
#: TOLERANCE) with a dividend calendar and an unprovable carry blocks entries instead of aborting
#: the job. WHY 0.85: the dividends / borrow fees seen in the universe reach ~13% of spot (STLA
#: 7.6%, special dividends up to ~13%) except data errors; a missed spin-off or split multiple
#: (x0.5, x0.25, x0.80 ...) lies below it and stays FATAL. Dividends are the only thing that
#: lowers the parity spot by that much, so the band stops where dividends stop explaining.
SOFT_BAND_LOW = 0.85
#: The longest a SOFT episode may last (calendar days from its first soft evaluation to the
#: session being checked) before it is escalated to the job-fatal refusal. WHY: an unprovable
#: dividend or borrow carry is TRANSIENT -- it ends at the ex-date or when the fee fades -- while
#: a basis error persists. Measured 2026-11-05 (tools/option_universe_preflight.py --sections
#: guard, alpaca, 752 stage-2 names, 72 monthly starts x 5 phases): every dividend/event-shaped
#: episode lasted <= 34 days (CIB 2025-03-21..04-24, 24 sessions; LW 15; STLA 10); the one
#: persistent episode, SMMT 2022-12-29..2023-06-23 (121 sessions, around its rights offering), is
#: the shape a missed corporate action has. 60 days (~42 sessions) leaves headroom above the
#: longest dividend episode and still stops a hidden 5-15% basis error within two months of it.
SOFT_MAX_DAYS = 60

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

    __slots__ = ("order", "exp_values", "exp_starts", "bar_ords", "parity", "term")

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
        #: session ordinal -> [(expiry ordinal, parity spot)] of every usable expiry among the
        #: nearest ``_CARRY_MAX_EXPIRIES`` (``expiry_parities``); only filled on a would-be
        #: refusal.
        self.term: Dict[int, List[Tuple[int, float]]] = {}

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
            "parity_entries": sum(len(i.parity) for i in _INDEX.values()),
            "term_entries": sum(len(i.term) for i in _INDEX.values())}


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
    # The nearest expiry whose best pair passes the gates (``_best_pair``: quote width,
    # moneyness) decides the session; a far-OTM best pair hands over to the next expiry, and the
    # session is unevaluable only when none of the _MAX_EXPIRIES tried has a usable pair.
    for jj in range(j, min(j + _MAX_EXPIRIES, len(idx.exp_values))):
        best = _best_pair(u, idx, jj, session_ord)
        if best is not None:
            out = best[0]
            break
    idx.parity[session_ord] = out
    return out


def _best_pair(u, idx, j: int, session_ord: int) -> Optional[Tuple[float, float]]:
    """(parity spot, strike) of expiry ``j``'s best call/put pair on the session -- the gates of
    ``parity_spot`` (quote width, moneyness) applied -- or None."""
    calls: Dict[float, Tuple[float, float]] = {}
    puts: Dict[float, Tuple[float, float]] = {}
    strike_f = u.raw.c_strike_f
    is_call = u.c_is_call
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
    if best is not None and best[1] > 0 and abs(best[2] / best[1] - 1.0) <= MAX_STRIKE_DISTANCE:
        return float(best[1]), float(best[2])
    return None


#: Expiries the CARRY separation scans for its two usable ones (wider than ``_MAX_EXPIRIES``: a
#: thin chain's second usable expiry is often the 5th-8th listed, AMC 2023-07-14's the 35-day).
_CARRY_MAX_EXPIRIES = 8


def expiry_parities(u, session_ord: int) -> List[Tuple[int, float]]:
    """[(expiry ordinal, parity spot)] of EVERY usable expiry among the nearest
    ``_CARRY_MAX_EXPIRIES`` with DTE >= ``_MIN_DTE``, same pair gates as ``parity_spot``. The
    carry separation's input; memoised per worker."""
    idx = _index_for(u.raw)
    hit = idx.term.get(session_ord)
    if hit is not None:
        return hit
    out: List[Tuple[int, float]] = []
    j = int(np.searchsorted(idx.exp_values, session_ord + _MIN_DTE, side="left"))
    for jj in range(j, min(j + _CARRY_MAX_EXPIRIES, len(idx.exp_values))):
        best = _best_pair(u, idx, jj, session_ord)
        if best is not None:
            out.append((int(idx.exp_values[jj]), best[0]))
    idx.term[session_ord] = out
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
                 split_dates: Optional[Callable[[str], Any]] = None,
                 dividends: Optional[Callable[[str, date], Any]] = None):
        """``split_dates(symbol)`` -> the ex-dates of the splits in the run's verified basis
        for ``symbol`` (forced checks, see the module docstring); None = no forced checks.
        ``dividends(symbol, session)`` -> [(ex-date, amount per share in the session's
        as-traded basis)], or None when the symbol's dividend calendar is unavailable; the
        carry separation (module docstring) adds them back. No source = unavailable."""
        self.spot_source = spot_source
        self.split_dates = split_dates
        self.dividends = dividends
        #: would-be refusals cleared by the carry separation
        self.carry_passed = 0
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
        #: symbol -> reason text while its entries are blocked (soft refusal), else absent
        self._soft_reason: Dict[str, str] = {}
        #: symbol -> first session of its CURRENT soft episode (``SOFT_MAX_DAYS``)
        self._soft_since: Dict[str, date] = {}
        #: symbol -> record of its soft skips (stats()["soft_skips"])
        self._soft: Dict[str, Dict[str, Any]] = {}
        #: symbol -> distinct sessions the guard was asked about (denominator of the skip share)
        self._sessions_seen: Dict[str, int] = {}
        self._soft_summary_warned = False
        self._soft_warned_keys: set = set()

    def _soft_stats(self) -> Dict[str, Any]:
        out = {}
        for sym, rec in sorted(self._soft.items()):
            seen = self._sessions_seen.get(sym, 0)
            out[sym] = {**rec, "sessions_seen": seen,
                        "skipped_share": round(rec["sessions_skipped"] / seen, 4) if seen else None}
        return out

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
        soft = self._soft_stats()
        soft = {k: v for k, v in soft.items() if v["sessions_skipped"]}
        if soft and not self._soft_summary_warned:
            self._soft_summary_warned = True
            logger.warning(
                "[backtest] option basis guard: NEW ENTRIES were skipped on %d symbol(s) (soft "
                "refusal, dividend/carry unprovable): %s", len(soft),
                "; ".join(f"{s_} {r['sessions_skipped']}/{r['sessions_seen']} sessions "
                          f"({r['first_skipped']}..{r['last_skipped']})" for s_, r in soft.items()))
        return {"checks": self.checks, "unevaluable": self.unevaluable,
                "soft_skips": soft,
                "sampled_out": self.sampled_out,
                "outliers_passed": self.outliers_passed, "carry_passed": self.carry_passed,
                "seconds": round(self.seconds, 6),
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

    def _session_date(self, u, session_ord: int) -> date:
        return (u.raw.date_of_ord[session_ord] if session_ord in u.raw.date_of_ord
                else date.fromordinal(session_ord))

    def _dividends_at(self, underlying: str, day: date):
        return self.dividends(underlying, day) if self.dividends is not None else None

    def _carry_ratio(self, u, underlying: str, session_ord: int):
        """(carry-free parity spot / as-traded spot or None, reason, veto or None): the TERM
        measure of one session."""
        from ba2_common.core.option_carry import carry_free_spot

        day = self._session_date(u, session_ord)
        spot = self.spot_source(underlying, day)
        if spot is None or not (spot > 0):
            return None, f"no as-traded spot on {day.isoformat()}", None
        pts = [(date.fromordinal(e), p) for e, p in expiry_parities(u, session_ord)]
        res = carry_free_spot(day, pts, self._dividends_at(underlying, day), spot=float(spot))
        if res.linear_confirmed is False:
            # A non-payer session no third expiry confirms: not measurable (module docstring).
            return None, res.reason, None
        return (None if res.spot is None else res.spot / float(spot)), res.reason, res.veto

    def _non_payer(self, underlying: str, ratios) -> bool:
        """True when the calendar lists no dividend in the horizon of any window session."""
        for d_, _r in ratios:
            divs = self._dividends_at(underlying, d_)
            if divs is None or len(divs):
                return False
        return True

    def _carry_clears(self, u, underlying: str, ratios, med: float):
        """(cleared, text for a refusal, kind). ``kind`` is "" when cleared, else "not_applicable",
        "veto", "outside" (measured, still off) or "unmeasurable". See the module docstring."""
        if med > 1.0 + TOLERANCE:
            return False, ("carry separation not applicable: the parity spot is ABOVE the "
                           "as-traded spot, and borrow or dividends only ever lower it"), "not_applicable"
        carry = [self._carry_ratio(u, underlying, d_.toordinal()) for d_, _ in ratios]
        vetoes = [v for _r, _w, v in carry if v]
        if vetoes:
            return False, f"carry separation refused: {vetoes[0]}", "veto"
        avail = [r for r, _w, _v in carry if r is not None]
        need = max(MIN_CARRY_FLOOR, min(MIN_CARRY_SESSIONS, len(carry)))
        if self._non_payer(underlying, ratios):
            # Every counted session of a non-payer carries its own third-expiry confirmation,
            # a stronger test than the two-point measure: MIN_CARRY_FLOOR of them clear.
            need = MIN_CARRY_FLOOR
        if len(avail) >= need:
            medc = float(np.median(avail))
            if CARRY_BAND_LOW <= medc <= 1.0 + TOLERANCE:
                self.carry_passed += 1
                return True, "", ""
            return False, (f"carry-free median {medc:.4f} over {len(avail)} of {len(carry)} "
                           f"window sessions (declared dividends added back, extrapolated to "
                           f"T=0) is outside [{CARRY_BAND_LOW:.3f}, {1.0 + TOLERANCE:.3f}]"), "outside"
        return False, (f"carry-free ratio measurable on {len(avail)} of {len(carry)} window "
                       f"sessions (>= {need} needed; checked session: {carry[0][1]})"), "unmeasurable"

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

    def check(self, u, underlying: str, session: date) -> bool:
        """Refuse (raise) when ``underlying``'s spot is off the chain's basis on ``session``.
        Returns True when NEW ENTRIES are allowed, False under a SOFT refusal (module docstring).
        Sampled: see the module docstring."""
        key = (underlying, session.toordinal())
        if key in self._checked:
            return underlying not in self._soft_reason
        self._checked.add(key)
        self._sessions_seen[underlying] = self._sessions_seen.get(underlying, 0) + 1
        self._check_session(u, underlying, session)
        blocked = self._soft_reason.get(underlying)
        if blocked is None:
            return True
        rec = self._soft[underlying]
        rec["sessions_skipped"] += 1
        rec["last_skipped"] = session.isoformat()
        if rec["first_skipped"] is None:
            rec["first_skipped"] = session.isoformat()
        if (underlying, key[1]) not in self._soft_warned_keys:
            self._soft_warned_keys.add((underlying, key[1]))
            logger.warning("[backtest] option basis guard: NEW ENTRIES on %s skipped on %s (soft "
                           "refusal; open positions keep being marked/closed): %s",
                           underlying, session.isoformat(), blocked)
        return False

    def _clear_soft(self, underlying: str) -> None:
        """The window cleared: entries resume and the soft episode (``SOFT_MAX_DAYS``) ends."""
        self._soft_reason.pop(underlying, None)
        self._soft_since.pop(underlying, None)

    def _check_session(self, u, underlying: str, session: date) -> None:
        key = (underlying, session.toordinal())
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
                self._clear_soft(underlying)
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
                self._clear_soft(underlying)
                return
            # Carry separation (module docstring): only ever clears a would-be refusal.
            cleared, carry_txt, kind = self._carry_clears(u, underlying, ratios, med)
            if cleared:
                self._clear_soft(underlying)
                return
            soft_ok = (SOFT_BAND_LOW <= med < 1.0 - TOLERANCE and kind in ("veto", "unmeasurable")
                       and all(self._dividends_at(underlying, d_) is not None for d_, _ in ratios))
            since = self._soft_since.get(underlying)
            if soft_ok and since is not None and (session - since).days > SOFT_MAX_DAYS:
                carry_txt = (f"{carry_txt}; SOFT episode since {since.isoformat()} has lasted "
                             f"{(session - since).days} days (> SOFT_MAX_DAYS {SOFT_MAX_DAYS}): a "
                             f"dividend/borrow carry is transient, a basis error persists")
                soft_ok = False
            if soft_ok:
                self._soft_since.setdefault(underlying, session)
                reason = (f"{underlying} {session.isoformat()}: parity/as-traded median "
                          f"{med:.4f} (checked {r0:.4f}) is below the {TOLERANCE:.0%} line but "
                          f"inside the soft band [{SOFT_BAND_LOW}, {1.0 - TOLERANCE:.2f}) and "
                          f"the dividend/borrow carry could not be proven: {carry_txt}")
                self._soft_reason[underlying] = reason
                rec = self._soft.setdefault(underlying, {
                    "sessions_skipped": 0, "first_skipped": None, "last_skipped": None,
                    "evaluations": 0, "reason": ""})
                rec["evaluations"] += 1
                rec["reason"] = reason
                return
            raise OptionSpotBasisMismatch(
                f"{underlying} {session.isoformat()}: the option path's spot is not in the "
                f"basis the chain is quoted in -- put-call-parity spot / as-traded spot = "
                f"{r0:.4f} (median {med:.4f} over "
                f"{[(d_.isoformat(), round(r_, 4)) for d_, r_ in ratios]}), tolerance "
                f"{TOLERANCE:.0%}; {carry_txt}. A split / spin-off missing from (or wrong in) "
                f"the split calendar, a mixed-basis FMP cache or a mis-mapped option ticker; "
                f"every strike, greek and intrinsic value of this run would be wrong. Refusing.")
        finally:
            self.seconds += time.perf_counter() - t0
