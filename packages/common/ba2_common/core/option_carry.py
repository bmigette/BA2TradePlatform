"""Carry-separated put-call-parity spot: tell a basis error from carry.

The option basis guard (``app.services.backtest.option_basis_guard``) compares the
put-call-parity spot of the nearest expiry, S_par = K + C_mid - P_mid, with the as-traded spot
the run uses. It exists to catch SPLIT-BASIS errors -- a constant multiple that scales every
expiry of a session alike (x0.5, x2, x1.25, a missed ~6% spin-off). Two kinds of CARRY also move
S_par away from the spot, and neither looks like that:

* a BORROW fee (hard-to-borrow names: AMC around the APE conversion, de-SPACs) lowers S_par by
  an amount that GROWS with time to expiry and fades as the expiry nears -- measured AMC
  2023-07-13: 1d 0.9865, 8d 0.9178, 15d 0.8266, 36d 0.7568 of the spot;
* a cash DIVIDEND lowers S_par of every expiry after its ex-date by the dividend -- STLA
  2022-04-12, ex 2022-04-19 of $1.12 on $14.90: 38d 0.9329, 66d 0.9279, 157d 0.9312, and 1.000
  from the ex-date on. A step shared by every expiry cannot be extrapolated away; only the
  dividend calendar can remove it.

Both only ever LOWER S_par. So carry can explain a parity spot BELOW the run's spot, never above
it (the guard applies that one-sided rule), and every correction here only raises S_par back.

ONE MEASURE, shared and pure: ``carry_free_spot`` -- the known dividends added back to the two
nearest usable expiries, then a linear extrapolation in days to T = 0. A basis error is a
multiple of every S_par, so it survives it. (A short-dated TIME measure existed briefly; review
round 3 removed it: its lookback could reach clean sessions from before a stretch-type basis
error that no split marks.)

WHAT IS NEVER CARRY (a ``veto``: the guard refuses, naming it, rather than clearing):

* dividends to add back totalling more than ``MAX_ADDBACK_FRACTION`` (2.5%, half the guard's
  tolerance) of the spot. Nothing verifies that the chain priced them: a special OCC adjusts
  the strikes for is NOT in the chain, so adding it back would hide a basis error of its own
  size (ACI 2022-10-21 $6.85 on $27.50 at x1.25; a 9% special at x1.09). Capped at half the
  tolerance, an unpriced add-back can shift the verdict by at most that much -- the simpler of
  the two sound rules (the other: add back only when an expiry before the ex-date shows the
  drop), and it costs no current name;
* a dividend row whose declaration date is unknown (as-of: was it known on the session?) or
  whose split-adjusted amount is missing;
* no dividend calendar at all;
* a term structure sloping UP (the later expiry above the earlier one beyond ``SLOPE_SLACK``,
  the interest allowance): not the shape of carry.

A dividend DECLARED after the session is not added back (it was not known yet), so the
correction is causal. The two-point extrapolation is only used when its noise LEVERAGE
t1 / (t2 - t1) is at most ``MAX_LEVERAGE`` (a further expiry is tried otherwise).

Pure and shared: the backtest guard is its only caller today; live has no guard (Alpaca's
spot and strikes are both as traded).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence, Tuple

__all__ = ["CarryFreeSpot", "DividendRow", "carry_free_spot", "MAX_ADDBACK_FRACTION",
           "MAX_LEVERAGE", "SLOPE_SLACK"]

#: The most (of the spot) the carry measure will add back UNVERIFIED: half the guard's 5%.
MAX_ADDBACK_FRACTION = 0.025
#: t1 / (t2 - t1) above this amplifies quote noise too much (2.5%-off pairs at 30/37 days give
#: a carry-free spot 24% off); a further expiry is tried instead.
MAX_LEVERAGE = 1.5
#: How far the later expiry's (dividend-corrected) parity spot may sit ABOVE the earlier one's
#: and still be carry (interest: ~0.5% over a few months).
SLOPE_SLACK = 0.005


@dataclass(frozen=True)
class DividendRow:
    ex_date: date
    #: Per share, in the SAME basis as the parity spots; None = the vendor gave no adjusted amount.
    amount: Optional[float]
    #: Declaration date; None = unknown.
    declared: Optional[date]


@dataclass(frozen=True)
class CarryFreeSpot:
    #: The carry-free spot, or None when it cannot be stated (``reason`` says why).
    spot: Optional[float]
    reason: str
    expiries_used: int = 0
    #: Set when the session must NOT be cleared as carry (module docstring); names the row.
    veto: Optional[str] = None


def _dividends(session: date, until: date, dividends: Sequence[DividendRow], spot: float
               ) -> Tuple[float, Optional[str], int]:
    """(total to add back for session < ex <= until, veto or None, rows not yet declared)."""
    total, pending, used = 0.0, 0, []
    for r in dividends:
        if not (session < r.ex_date <= until):
            continue
        tag = f"dividend ex {r.ex_date.isoformat()}"
        if r.declared is None:
            return total, f"{tag}: declaration date unknown, so it cannot be known whether it " \
                          f"was priced on {session.isoformat()}", pending
        if r.declared > session:
            pending += 1
            continue
        if r.amount is None or not (r.amount == r.amount):
            return total, f"{tag}: no split-adjusted amount", pending
        if r.amount > 0:
            total += r.amount
            used.append(f"{r.ex_date.isoformat()} {r.amount:g}")
    if total > 0 and (not (spot > 0) or total / spot > MAX_ADDBACK_FRACTION):
        return total, (f"dividends to add back ({', '.join(used)}) total {total / spot:.1%} of "
                       f"the spot {spot:g} (> {MAX_ADDBACK_FRACTION:.1%}): unverified, an "
                       f"add-back that large could hide a basis error of its size (a special "
                       f"OCC strike-adjusts is not priced by the chain)"), pending
    return total, None, pending


def carry_free_spot(session: date, points: Sequence[Tuple[date, float]],
                    dividends: Optional[Sequence[DividendRow]], *, spot: float) -> CarryFreeSpot:
    """The TERM measure of ``session`` (module docstring). ``points``: (expiry, parity spot) of
    the usable expiries, any order; ``spot``: the run's as-traded spot (for the special-
    dividend test only)."""
    if dividends is None:
        return CarryFreeSpot(None, "dividend calendar unavailable",
                             veto="no dividend calendar cached (tools/warm_dividend_calendars.py)")
    pts = sorted((e, float(p)) for e, p in points if e > session and p == p and p > 0)
    if len(pts) < 2:
        return CarryFreeSpot(None, f"carry separation needs >= 2 expiries with a usable "
                                   f"call/put pair; {len(pts)} on {session.isoformat()}")
    (e1, p1) = pts[0]
    t1 = (e1 - session).days
    pick = next(((e, p) for e, p in pts[1:] if t1 / ((e - session).days - t1) <= MAX_LEVERAGE),
                None)
    # Vetoes first, over every row up to the expiry the measure would read (the second nearest
    # when none qualifies): a special or a row of unknown standing there disqualifies the session.
    _, veto, _ = _dividends(session, (pick or pts[1])[0], dividends, spot)
    if veto:
        return CarryFreeSpot(None, veto, veto=veto)
    if pick is None:
        return CarryFreeSpot(None, f"no second expiry keeps the extrapolation leverage t1/(t2-t1) "
                                   f"<= {MAX_LEVERAGE} (nearest {t1}d)")
    e2, p2 = pick
    t2 = (e2 - session).days
    d1, veto, _ = _dividends(session, e1, dividends, spot)
    if veto:
        return CarryFreeSpot(None, veto, veto=veto)
    d2, veto, pending = _dividends(session, e2, dividends, spot)
    if veto:
        return CarryFreeSpot(None, veto, veto=veto)
    s1, s2 = p1 + d1, p2 + d2
    if s2 > s1 * (1.0 + SLOPE_SLACK):
        why = (f"the term structure slopes up ({t1}d {s1:.4g} -> {t2}d {s2:.4g}): not the "
               f"shape of carry")
        return CarryFreeSpot(None, why, veto=why)
    est = s1 - t1 * (s2 - s1) / (t2 - t1)
    note = f"; {pending} dividend(s) not yet declared, not added" if pending else ""
    return CarryFreeSpot(est, f"extrapolated to T=0 from {t1}d and {t2}d, declared dividends "
                              f"added back{note}", expiries_used=2)
