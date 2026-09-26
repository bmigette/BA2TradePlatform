"""Carry-separated put-call-parity spot: tell a basis error from carry.

The option basis guard (``app.services.backtest.option_basis_guard``) compares the
put-call-parity spot of the nearest expiry, S_par = K + C_mid - P_mid, with the as-traded spot
the run uses. It exists to catch SPLIT-BASIS errors -- a constant multiple that scales every
expiry of a session alike (x0.5, x2, x1.25, a missed ~6% spin-off). Two kinds of CARRY also move
S_par away from the spot, and neither looks like that:

* a BORROW fee (hard-to-borrow names: AMC around the APE conversion, de-SPACs) lowers S_par by
  an amount that GROWS with time to expiry -- measured AMC 2023-07-13: 1d 0.9865, 8d 0.9178,
  15d 0.8266, 36d 0.7568 of the spot; interest does the same the other way, much smaller;
* a cash DIVIDEND lowers S_par of every expiry after its ex-date by the (present value of the)
  dividend -- STLA 2022-04-12, ex 2022-04-19 of $1.12 on $14.90: 38d 0.9329, 66d 0.9279,
  157d 0.9312, and 1.000 from the ex-date on. A step shared by every expiry cannot be
  extrapolated away; only the dividend calendar can remove it.

``carry_free_spot`` therefore (1) adds back the known dividends with session < ex-date <=
expiry to each expiry's S_par, and (2) extrapolates the two nearest expiries linearly in days to
T = 0. A basis error is a multiple of every S_par, so it survives both steps: the carry-free
spot of a x2 error is still x2. It needs >= 2 expiries; with fewer it answers ``spot=None`` and
says why, so the caller keeps its uncorrected verdict (no silent pass).

Pure and shared: the backtest guard is its only caller today; live has no guard (Alpaca's
spot and strikes are both as traded).

Dividends are the REALIZED calendar (FMP stock_dividend), as a market prices an expected
dividend before it is declared. The guard is a data-consistency check, not a trading input, so
no decision reads a future value through it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence, Tuple

__all__ = ["CarryFreeSpot", "carry_free_spot"]


@dataclass(frozen=True)
class CarryFreeSpot:
    #: The carry-free spot, or None when it cannot be stated (``reason`` says why).
    spot: Optional[float]
    reason: str
    expiries_used: int = 0


def carry_free_spot(session: date, points: Sequence[Tuple[date, float]],
                    dividends: Optional[Sequence[Tuple[date, float]]]) -> CarryFreeSpot:
    """The parity spot of ``session`` with carry removed (module docstring).

    ``points``: (expiry, parity spot) of the usable expiries, any order; expiries on or before
    ``session`` are ignored. ``dividends``: (ex-date, amount per share in the SAME basis as the
    parity spots), or None when the calendar is unavailable (then no dividend is added back and
    the reason says so)."""
    pts = sorted((e, float(p)) for e, p in points if e > session and p == p and p > 0)
    note = "" if dividends is not None else "; dividend calendar unavailable (none added back)"
    if len(pts) < 2:
        return CarryFreeSpot(None, f"carry separation needs >= 2 expiries with a usable "
                                   f"call/put pair; {len(pts)} on {session.isoformat()}{note}")
    divs = [(d, float(a)) for d, a in (dividends or ()) if a == a and a > 0]
    adj = []
    for e, p in pts[:2]:
        adj.append(((e - session).days, p + sum(a for d, a in divs if session < d <= e)))
    (t1, s1), (t2, s2) = adj
    spot = s1 - t1 * (s2 - s1) / (t2 - t1)
    return CarryFreeSpot(spot, f"extrapolated to T=0 from {t1}d and {t2}d, dividends added "
                               f"back{note}", expiries_used=2)
