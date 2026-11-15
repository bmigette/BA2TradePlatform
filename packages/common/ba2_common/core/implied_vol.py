"""Pure implied-volatility maths for the ``iv-v1`` market-condition profile.

Design: ``docs/plans/2026-11-15-option-iv-conditions.md`` (feature definitions, IV method,
missing-value policy). Ported from the 821-symbol study script ``ivrv_signal.py`` (2026-11-15);
the vendor ``iv`` column of the ThetaData store is NEVER used (it is keyed without the expiry,
so one strike carries the same iv across every expiry).

This module is PURE: numpy/math only, no I/O, no logging, no other project module. It turns ONE
session's end-of-day option chain (``ChainQuotes``: expiry, call/put, strike, bid, ask) into the
ATM implied volatility interpolated to constant 30- and 60-calendar-day maturities.

METHOD (``IV_CALC_VERSION``), every step fixed:

1. Candidate expiries: ``dte = expiry - session`` in calendar days, ``DTE_MIN..DTE_MAX`` (7..130).
2. Forward per expiry (put-call parity, AS-TRADED basis, undiscounted): over strikes where both
   the call and the put carry a forward-usable quote (finite, ``bid > 0``, ``ask >= bid`` -- a
   zero bid is a one-sided market, not a mid; the study admitted ``bid >= 0``), ``K*`` = the strike minimising ``|C_mid - P_mid|`` (ties: the LOWEST strike)
   and ``F = K* + C_mid - P_mid``. ``mid = (bid + ask) / 2``. No forward -> the expiry is unusable.
3. Forward outliers: the median of the candidate expiries' forwards; an expiry whose forward is
   more than 10% from it is excluded (an adjusted root on another basis, a broken print).
4. Option IV: Black-76 on the forward, undiscounted, ``T = dte / 365``, bisection on
   ``[0.01, 5.0]``, 45 halvings, result = the final midpoint; a result pinned within 0.001 of a
   bound (``< 0.011`` or ``> 4.99``) is no inversion. Only a TWO-SIDED, sane quote is inverted:
   ``bid > 0``, ``ask >= bid``, ``ask - bid <= 0.5 * mid``, ``0.5 F <= K <= 1.5 F``, and the mid
   strictly inside the no-arbitrage band (``intrinsic + 1e-6 < mid < F`` for a call, ``< K`` for
   a put). The normal CDF is ``0.5 * erfc(-x / sqrt 2)`` (``math.erfc``).
5. ATM IV per expiry: a strike's IV is the mean of its call and put IVs (one side alone if only
   one inverts). ``k0`` = the largest valid strike below F, ``k1`` = the smallest valid strike at
   or above F; when both exist and ``(k1 - k0) / F <= 0.10`` the ATM IV is linear in strike
   between them, else the nearer of the two if within 2.5% of F (ties: the lower), else none.
   Evaluated LAZILY outward from F -- identical to inverting every strike and then bracketing,
   because only ``k0``/``k1`` can ever be read.
6. Constant maturity, total variance linear in calendar days: IV30 between the NEAREST usable
   expiry with dte in ``[7, 30]`` and the nearest in ``(30, 75]``; IV60 between ``[21, 60]`` and
   ``(60, 130]``. Both sides are required -- never extrapolated, never a flat single expiry --
   EXCEPT an expiry exactly ON the target day, which is the constant-maturity value itself.

DISCOUNTING IS IGNORED (D = 1), DELIBERATELY. Equity option quotes embed a discount factor
``D = exp(-r T)``; pricing them undiscounted biases the IV slightly LOW when rates are high: a
true 0.30 recovers 0.2987 at 30 days and 0.2974 at 60 days with r = 5.3% (2023-24), and ~0.3000 at
r = 0.05% (2020-21) -- under 0.5% of the level, against gate steps of ~5-8% of it. Estimating D
per expiry from the put-call-parity SLOPE across strikes was measured on real ThetaData chains
(AAPL/MSFT/JPM/XOM/KO/AMD, March 2021/2023/2024, 20-75 day expiries, strikes within 10% of F):
D error p10..p90 about -0.02..+0.04 and an implied rate whose median had the WRONG sign in 2023-24
(-8% / -12% against ~5.2%), i.e. far noisier than the bias it would remove (American early
exercise and dividends distort parity across strikes). There is no rate source in this pure
module, so D = 1 stays; see docs/plans/2026-11-15-option-iv-conditions.md.

Determinism: the chain is normalised (sorted, exact duplicates collapsed, conflicting duplicates
dropped) at construction, so neither the answer nor ``digest()`` depends on row order. Every
reduction over quotes is an argmin/compare/elementwise IEEE operation; the inversion is a scalar
Python loop. The same function serves the store build and any future live adapter.
"""
from __future__ import annotations

import hashlib
import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

IV_CALC_VERSION = "iv-v1/calc-1"

DTE_MIN = 7
DTE_MAX = 130
YEAR_DAYS = 365.0
#: (target days, low-side window [lo, target], high-side window (target, hi]) per tenor.
TENOR_30 = (30, 7, 75)
TENOR_60 = (60, 21, 130)

FORWARD_OUTLIER = 0.10
BRACKET_MAX_WIDTH = 0.10
NEAREST_MAX_DIST = 0.025
STRIKE_BAND = (0.5, 1.5)
MAX_SPREAD_OF_MID = 0.5
VOL_LO, VOL_HI = 0.01, 5.0
VOL_PINNED_LO, VOL_PINNED_HI = 0.011, 4.99
BISECTION_STEPS = 45
INTRINSIC_EPS = 1e-6

_SQRT2 = math.sqrt(2.0)
_EPOCH = date(1970, 1, 1)


def date_to_days(d: date) -> int:
    """Days since 1970-01-01 (the ``datetime64[D]`` integer)."""
    return (d - _EPOCH).days


def _to_days(values: Any) -> np.ndarray:
    arr = np.asarray(values)
    if arr.dtype.kind == "M":
        return arr.astype("datetime64[D]").astype(np.int64)
    if arr.dtype.kind in "iu":
        return arr.astype(np.int64)
    out = np.empty(len(arr), dtype=np.int64)
    for i, v in enumerate(arr.tolist()):
        if isinstance(v, date):
            out[i] = date_to_days(v)
        else:
            out[i] = date_to_days(date.fromisoformat(str(v)[:10]))
    return out


def _to_is_call(values: Any) -> np.ndarray:
    arr = np.asarray(values)
    if arr.dtype == bool:
        return arr.copy()
    out = np.empty(len(arr), dtype=bool)
    for i, v in enumerate(arr.tolist()):
        if isinstance(v, (bool, np.bool_)):
            out[i] = bool(v)
            continue
        s = str(v).strip().lower()
        if s in ("call", "c"):
            out[i] = True
        elif s in ("put", "p"):
            out[i] = False
        else:
            raise ValueError(f"option type {v!r} is neither call nor put")
    return out


@dataclass(frozen=True, eq=False)
class ChainQuotes:
    """ONE session's end-of-day quotes, normalised at construction.

    Arrays (equal length): ``expiry_days`` int64 (days since epoch), ``is_call`` bool, ``strike``,
    ``bid``, ``ask`` float64. Normalisation: rows sorted by (expiry, put/call, strike, bid, ask);
    exact duplicate rows collapse to one; a (expiry, type, strike) with CONFLICTING quotes is
    dropped entirely (no "first row wins": that would make the answer depend on file order).
    NaN bid/ask are kept (they fail every filter) so the digest still sees them.
    """

    expiry_days: np.ndarray
    is_call: np.ndarray
    strike: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    _index: Dict[int, Tuple[int, int]] = field(default=None, repr=False)  # type: ignore[assignment]
    #: rows dropped because their (expiry, type, strike) carried CONFLICTING quotes (a quality signal
    #: the store build reports; set at construction).
    conflicts_dropped: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        e = np.asarray(self.expiry_days, dtype=np.int64)
        c = np.asarray(self.is_call, dtype=bool)
        k = np.asarray(self.strike, dtype=np.float64)
        b = np.asarray(self.bid, dtype=np.float64)
        a = np.asarray(self.ask, dtype=np.float64)
        n = len(e)
        if not (len(c) == len(k) == len(b) == len(a) == n):
            raise ValueError("ChainQuotes arrays must have equal lengths")
        if n:
            # NaN sorts last in lexsort; equal_nan comparison below treats NaN == NaN.
            order = np.lexsort((a, b, k, c, e))
            e, c, k, b, a = e[order], c[order], k[order], b[order], a[order]
            same_key = np.zeros(n, dtype=bool)
            same_key[1:] = (e[1:] == e[:-1]) & (c[1:] == c[:-1]) & (k[1:] == k[:-1])
            if same_key.any():
                same_quote = np.zeros(n, dtype=bool)
                same_quote[1:] = (((b[1:] == b[:-1]) | (np.isnan(b[1:]) & np.isnan(b[:-1])))
                                  & ((a[1:] == a[:-1]) | (np.isnan(a[1:]) & np.isnan(a[:-1]))))
                keep = np.ones(n, dtype=bool)
                keep[same_key & same_quote] = False          # exact duplicate -> one row
                # a key that has ANY conflicting duplicate is dropped whole
                group = np.cumsum(~same_key)                 # group id per (e, c, k)
                bad_groups = np.unique(group[same_key & ~same_quote])
                if len(bad_groups):
                    conflicting = np.isin(group, bad_groups)
                    object.__setattr__(self, "conflicts_dropped", int(conflicting.sum()))
                    keep &= ~conflicting
                e, c, k, b, a = e[keep], c[keep], k[keep], b[keep], a[keep]
        for name, arr in (("expiry_days", e), ("is_call", c), ("strike", k), ("bid", b), ("ask", a)):
            arr.setflags(write=False)
            object.__setattr__(self, name, arr)
        index: Dict[int, Tuple[int, int]] = {}
        if len(e):
            uniq, first = np.unique(e, return_index=True)
            bounds = list(first) + [len(e)]
            for i, d in enumerate(uniq.tolist()):
                index[int(d)] = (int(bounds[i]), int(bounds[i + 1]))
        object.__setattr__(self, "_index", index)

    # -- construction helpers
    @classmethod
    def from_columns(cls, expiry: Sequence[Any], option_type: Sequence[Any], strike: Sequence[float],
                     bid: Sequence[Any], ask: Sequence[Any]) -> "ChainQuotes":
        """From plain columns: expiry as dates / ISO strings / datetime64; option type as
        bool (True = call) or 'call'/'put'; bid/ask may hold None (-> NaN)."""
        def f8(x):
            return np.array([np.nan if v is None else float(v) for v in x], dtype=np.float64) \
                if not isinstance(x, np.ndarray) else x.astype(np.float64)
        return cls(_to_days(expiry), _to_is_call(option_type), f8(strike), f8(bid), f8(ask))

    @classmethod
    def empty(cls) -> "ChainQuotes":
        z = np.empty(0)
        return cls(np.empty(0, np.int64), np.empty(0, bool), z, z, z)

    @classmethod
    def concat(cls, parts: Sequence["ChainQuotes"]) -> "ChainQuotes":
        parts = list(parts)
        if not parts:
            return cls.empty()
        return cls(np.concatenate([p.expiry_days for p in parts]), np.concatenate([p.is_call for p in parts]),
                   np.concatenate([p.strike for p in parts]), np.concatenate([p.bid for p in parts]),
                   np.concatenate([p.ask for p in parts]))

    def select(self, mask_or_index: Any) -> "ChainQuotes":
        return ChainQuotes(self.expiry_days[mask_or_index], self.is_call[mask_or_index],
                           self.strike[mask_or_index], self.bid[mask_or_index], self.ask[mask_or_index])

    def __len__(self) -> int:
        return int(len(self.expiry_days))

    def expiries(self) -> List[int]:
        return sorted(self._index)

    def rows_for(self, expiry_day: int) -> Tuple[int, int]:
        return self._index.get(int(expiry_day), (0, 0))

    def digest(self) -> str:
        """``sha256:<hex>`` over the normalised arrays (little-endian), order-independent."""
        h = hashlib.sha256(b"ba2-chain-quotes/v1\x00")
        for arr in (self.expiry_days.astype("<i8"), self.is_call.astype("u1"), self.strike.astype("<f8"),
                    self.bid.astype("<f8"), self.ask.astype("<f8")):
            h.update(np.ascontiguousarray(arr).tobytes())
        return "sha256:" + h.hexdigest()


# --------------------------------------------------------------------------- Black-76
def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


def black76_price(F: float, K: float, T: float, sigma: float, is_call: bool) -> float:
    """Undiscounted Black-76 price; the put by parity (``C - F + K``), as the study does."""
    sq = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    c = F * norm_cdf(d1) - K * norm_cdf(d2)
    return c if is_call else c - F + K


def implied_vol_black76(F: float, K: float, T: float, price: float, is_call: bool) -> Optional[float]:
    """Bisection on ``[VOL_LO, VOL_HI]``; None outside the no-arbitrage band or when pinned."""
    if not (math.isfinite(F) and math.isfinite(K) and math.isfinite(price) and F > 0 and K > 0 and T > 0):
        return None
    intrinsic = max(F - K, 0.0) if is_call else max(K - F, 0.0)
    upper = F if is_call else K
    if not (price > intrinsic + INTRINSIC_EPS and price < upper):
        return None
    lo, hi = VOL_LO, VOL_HI
    for _ in range(BISECTION_STEPS):
        m = 0.5 * (lo + hi)
        if black76_price(F, K, T, m, is_call) > price:
            hi = m
        else:
            lo = m
    v = 0.5 * (lo + hi)
    if v < VOL_PINNED_LO or v > VOL_PINNED_HI:
        return None
    return v


def quote_iv(F: float, K: float, T: float, bid: float, ask: float, is_call: bool) -> Optional[float]:
    """The IV of one EOD quote's mid, or None when the quote is not a sane two-sided market."""
    if not (math.isfinite(bid) and math.isfinite(ask)):
        return None
    mid = (bid + ask) / 2.0
    if not (bid > 0 and ask >= bid and (ask - bid) <= MAX_SPREAD_OF_MID * mid):
        return None
    if not (STRIKE_BAND[0] * F <= K <= STRIKE_BAND[1] * F):
        return None
    return implied_vol_black76(F, K, T, mid, is_call)


def parity_forward(strikes: np.ndarray, call_mid: np.ndarray, put_mid: np.ndarray) -> Optional[float]:
    """``K* + C_mid(K*) - P_mid(K*)`` with ``K* = argmin |C - P|`` (ties: lowest strike).
    Inputs: strikes ascending, the mids of strikes where BOTH sides are forward-usable."""
    if len(strikes) == 0:
        return None
    diff = np.asarray(call_mid, dtype=np.float64) - np.asarray(put_mid, dtype=np.float64)
    i = int(np.argmin(np.abs(diff)))           # first minimum == lowest strike (ascending input)
    F = float(strikes[i]) + float(diff[i])
    return F if (math.isfinite(F) and F > 0) else None


# --------------------------------------------------------------------------- one expiry
@dataclass(frozen=True)
class ExpiryPoint:
    dte: int
    forward: Optional[float]
    atm_iv: Optional[float]
    how: str          # "bracket" | "nearest" | "far" | "no_iv" | "no_fwd"


def _side_quotes(chain: ChainQuotes, lo: int, hi: int) -> Tuple[Dict[float, Tuple[float, float]],
                                                                  Dict[float, Tuple[float, float]]]:
    calls: Dict[float, Tuple[float, float]] = {}
    puts: Dict[float, Tuple[float, float]] = {}
    ks, cs, bs, as_ = (chain.strike[lo:hi].tolist(), chain.is_call[lo:hi].tolist(),
                       chain.bid[lo:hi].tolist(), chain.ask[lo:hi].tolist())
    for k, c, b, a in zip(ks, cs, bs, as_):
        (calls if c else puts)[k] = (b, a)     # unique per (type, strike) after normalisation
    return calls, puts


def _forward_from(calls: Dict[float, Tuple[float, float]], puts: Dict[float, Tuple[float, float]]) -> Optional[float]:
    def usable(q):
        b, a = q
        return math.isfinite(b) and math.isfinite(a) and b > 0 and a >= b
    both = sorted(k for k in calls if k in puts and usable(calls[k]) and usable(puts[k]))
    if not both:
        return None
    strikes = np.array(both, dtype=np.float64)
    cm = np.array([(calls[k][0] + calls[k][1]) / 2.0 for k in both], dtype=np.float64)
    pm = np.array([(puts[k][0] + puts[k][1]) / 2.0 for k in both], dtype=np.float64)
    return parity_forward(strikes, cm, pm)


def _strike_iv(k: float, F: float, T: float, calls, puts) -> Optional[float]:
    vals = []
    for book, is_call in ((calls, True), (puts, False)):
        q = book.get(k)
        if q is None:
            continue
        v = quote_iv(F, k, T, q[0], q[1], is_call)
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return sum(vals) / len(vals)


def _atm_from(F: float, T: float, calls, puts) -> Tuple[Optional[float], str]:
    strikes = sorted(set(calls) | set(puts))
    j = bisect_left(strikes, F)                 # strikes[j-1] < F <= strikes[j]
    lo_band, hi_band = STRIKE_BAND[0] * F, STRIKE_BAND[1] * F
    k0 = v0 = None
    for i in range(j - 1, -1, -1):              # largest VALID strike below F
        k = strikes[i]
        if k < lo_band:
            break
        v = _strike_iv(k, F, T, calls, puts)
        if v is not None:
            k0, v0 = k, v
            break
    k1 = v1 = None
    for i in range(j, len(strikes)):            # smallest VALID strike at or above F
        k = strikes[i]
        if k > hi_band:
            break
        v = _strike_iv(k, F, T, calls, puts)
        if v is not None:
            k1, v1 = k, v
            break
    if k0 is None and k1 is None:
        return None, "no_iv"
    if k0 is not None and k1 is not None and (k1 - k0) / F <= BRACKET_MAX_WIDTH:
        w = (F - k0) / (k1 - k0)
        return v0 * (1 - w) + v1 * w, "bracket"
    cands = [(abs(k - F), k, v) for k, v in ((k0, v0), (k1, v1)) if k is not None]
    cands.sort(key=lambda t: (t[0], t[1]))      # nearer first; a tie goes to the LOWER strike
    dist, _k, v = cands[0]
    if dist / F <= NEAREST_MAX_DIST:
        return v, "nearest"
    return None, "far"


def expiry_point(chain: ChainQuotes, session: date, expiry: date) -> ExpiryPoint:
    """Forward and ATM IV of one expiry (the forward alone when ``forward`` is all you need)."""
    return _expiry_point(chain, date_to_days(session), date_to_days(expiry), with_iv=True)


def _expiry_point(chain: ChainQuotes, sday: int, eday: int, *, with_iv: bool,
                  forward: Optional[float] = None) -> ExpiryPoint:
    dte = eday - sday
    lo, hi = chain.rows_for(eday)
    calls, puts = _side_quotes(chain, lo, hi)
    F = forward if forward is not None else _forward_from(calls, puts)
    if F is None:
        return ExpiryPoint(dte, None, None, "no_fwd")
    if not with_iv:
        return ExpiryPoint(dte, F, None, "")
    v, how = _atm_from(F, dte / YEAR_DAYS, calls, puts)
    return ExpiryPoint(dte, F, v, how)


# --------------------------------------------------------------------------- constant maturity
def interp_total_variance(points: Sequence[Tuple[int, float]], target: int, lo_min: int,
                          hi_max: int) -> Tuple[Optional[float], Optional[Tuple[int, int]]]:
    """Total variance linear in days between the nearest point with dte in ``[lo_min, target]``
    and the nearest in ``(target, hi_max]``. ``points``: (dte, iv) with a usable iv. A point EXACTLY
    on ``target`` is the answer by itself (bracket ``(target, target)``), with or without a high
    side."""
    lo = [p for p in points if lo_min <= p[0] <= target and p[1] is not None]
    hi = [p for p in points if target < p[0] <= hi_max and p[1] is not None]
    if not lo:
        return None, None
    t1, v1 = max(lo, key=lambda p: p[0])
    if t1 == target:
        return float(v1), (target, target)
    if not hi:
        return None, None
    t2, v2 = min(hi, key=lambda p: p[0])
    w1, w2 = v1 * v1 * t1, v2 * v2 * t2
    w = w1 + (w2 - w1) * (target - t1) / (t2 - t1)
    if not (w > 0 and math.isfinite(w)):
        return None, None
    return math.sqrt(w / target), (t1, t2)


@dataclass(frozen=True)
class IvTerm:
    """IV30/IV60 of one session (None + a reason when the chain cannot define it)."""

    iv30: Optional[float]
    iv60: Optional[float]
    iv30_reason: str = ""
    iv60_reason: str = ""
    iv30_bracket: Optional[Tuple[int, int]] = None
    iv60_bracket: Optional[Tuple[int, int]] = None
    empty: bool = False          # the chain had no rows at all for this session
    candidates: int = 0          # expiries with dte in DTE_MIN..DTE_MAX
    outliers: int = 0            # candidate expiries dropped by the forward-outlier rule


def atm_iv_term(chain: ChainQuotes, session: date) -> IvTerm:
    """IV30 and IV60 of ``session`` from its end-of-day chain (see the module docstring)."""
    if len(chain) == 0:
        reason = "no option chain rows for this session"
        return IvTerm(None, None, reason, reason, empty=True)
    sday = date_to_days(session)
    cand = [e for e in chain.expiries() if DTE_MIN <= e - sday <= DTE_MAX]
    if not cand:
        reason = f"no expiry in {DTE_MIN}..{DTE_MAX} days"
        return IvTerm(None, None, reason, reason)
    fwd: Dict[int, ExpiryPoint] = {e: _expiry_point(chain, sday, e, with_iv=False) for e in cand}
    valid_f = sorted(p.forward for p in fwd.values() if p.forward is not None)
    if not valid_f:
        reason = "no expiry with a put-call-parity forward (no strike quoted on both sides)"
        return IvTerm(None, None, reason, reason, candidates=len(cand))
    n = len(valid_f)
    med = valid_f[n // 2] if n % 2 else (valid_f[n // 2 - 1] + valid_f[n // 2]) / 2.0
    usable = {e for e, p in fwd.items()
              if p.forward is not None and abs(p.forward / med - 1.0) <= FORWARD_OUTLIER}
    outliers = sum(1 for p in fwd.values() if p.forward is not None) - len(usable)
    cache: Dict[int, Optional[float]] = {}

    def atm(e: int) -> Optional[float]:
        if e not in cache:
            cache[e] = _expiry_point(chain, sday, e, with_iv=True, forward=fwd[e].forward).atm_iv
        return cache[e]

    def tenor(spec: Tuple[int, int, int]) -> Tuple[Optional[float], str, Optional[Tuple[int, int]]]:
        target, lo_min, hi_max = spec
        lo_side = sorted((e for e in usable if lo_min <= e - sday <= target), reverse=True)
        hi_side = sorted(e for e in usable if target < e - sday <= hi_max)
        t1 = next(((e - sday, atm(e)) for e in lo_side if atm(e) is not None), None)
        if t1 is None:
            return None, f"no expiry with a usable ATM implied volatility in {lo_min}..{target} days", None
        if t1[0] == target:
            return float(t1[1]), "", (target, target)
        t2 = next(((e - sday, atm(e)) for e in hi_side if atm(e) is not None), None)
        if t2 is None:
            return None, f"no expiry with a usable ATM implied volatility in {target + 1}..{hi_max} days", None
        v, br = interp_total_variance([t1, t2], target, lo_min, hi_max)
        if v is None:
            return None, "non-positive interpolated total variance", None
        return v, "", br

    v30, r30, b30 = tenor(TENOR_30)
    v60, r60, b60 = tenor(TENOR_60)
    return IvTerm(v30, v60, r30, r60, b30, b60, empty=False, candidates=len(cand), outliers=outliers)
