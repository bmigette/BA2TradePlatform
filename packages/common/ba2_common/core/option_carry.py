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
  tolerance) that the same session's chain does not SHOW priced (VERIFIED add-back, below).
  A special OCC adjusts the strikes for is NOT in the chain, so adding it back would hide a
  basis error of its own size (ACI 2022-10-21 $6.85 on $27.50 at x1.25; a 9% special at
  x1.09). Capped at half the tolerance, an unpriced add-back can shift the verdict by at most
  that much;
* a dividend row whose declaration date is unknown (as-of: was it known on the session?) or
  whose split-adjusted amount is missing;
* no dividend calendar at all;
* a term structure sloping UP (the later expiry above the earlier one beyond ``SLOPE_SLACK``,
  the interest allowance): not the shape of carry;
* for a DIVIDEND PAYER (the calendar lists a dividend in the source's horizon after the
  session), a drop between the two expiries that the calendar's dividends between them do not
  explain by more than ``MAX_ADDBACK_FRACTION`` of the spot. The extrapolation would add that
  drop back unverified, and on a payer the likeliest cause is a dividend the chain prices but the
  calendar lacks -- which would hide a basis error of its own size (review of 5178e625, item 3:
  8% -> x1.08 and 10% -> x1.10 passed on 4 of 5 start offsets). A symbol with NO dividend in the
  horizon is read as borrow, the only other carry (AMC July 2023: up to 15% between weeklies).

A NON-PAYER's session (the calendar lists no dividend in the horizon) must be CONFIRMED by a third
usable expiry (review r6, decision D): the first expiry at least ``t2 - t1`` beyond the second,
whose parity spot lies on the line through the first two within ``LINEARITY_TOLERANCE`` of the
spot. A borrow fee's term structure is near-linear; a dividend the calendar lacks ENTIRELY,
falling between the first two expiries, is a STEP that leaves the third at the second's level
-- off the line by at least the dividend. ``linear_confirmed`` reports it (None for a payer, whose
drops the unexplained-drop veto already polices); the guard counts only confirmed non-payer
sessions.

VERIFIED ADD-BACK. A dividend over the cap is not a veto when the session's own chain shows it
priced: the last usable expiry BEFORE the ex-date (it settles before the dividend) and the first
on/after it are adjacent across the ex-date, and the step ``p_pre - p_post`` must lie in
[``VERIFY_MIN``, ``VERIFY_MAX``] = [0.5, 1.5] x the sum D of the declared dividends of that gap.
An unpriced special shows a step ~0 and fails for every D, however close to the cap (the
tolerance is relative to D, never absolute); the real STLA 2022-03 chain steps 0.75-0.85 D, which
is why the bound is not tighter. Once verified the dividend is added back like a small one (at most as far as the chain shows it: min(1, step / D) of its amount); only
the UNVERIFIED remainder is judged by the cap, and a veto names why verification failed. A
basis error does not ride on the verification: it scales pre and post alike, the step still
matches, and the carry-free spot keeps the multiple, so the guard refuses it as before.
RESIDUAL RISK: (a) quote noise: an unpriced special of D passes only if the pair's quotes
disagree by 0.5 D in spot terms (>= 1.25% at the cap; each pair is gated to 2.5% uncertainty);
the add-back then overshoots by D and masks a basis error of that size; (b) a PARTIALLY priced
special (step 0.5-0.6 D) overshoots by up to 0.5 D (3.75% for 7.5%; 12% for a 25% special that
is half in the chain); (c) any other price move between the two expiries equal to the dividend
(an earnings gap in a thin chain) verifies a dividend the chain did not price. The guard's
CARRY_BAND_LOW (0.975) is unchanged.

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
           "MAX_LEVERAGE", "SLOPE_SLACK", "LINEARITY_TOLERANCE", "VERIFY_MIN", "VERIFY_MAX"]

#: The most (of the spot) the carry measure will add back UNVERIFIED: half the guard's 5%.
MAX_ADDBACK_FRACTION = 0.025
#: t1 / (t2 - t1) above this amplifies quote noise too much (2.5%-off pairs at 30/37 days give
#: a carry-free spot 24% off); a further expiry is tried instead.
MAX_LEVERAGE = 1.5
#: How far the later expiry's (dividend-corrected) parity spot may sit ABOVE the earlier one's
#: and still be carry (interest: ~0.5% over a few months).
SLOPE_SLACK = 0.005
#: How far (of the spot) a non-payer's third expiry may sit off the line through the first two
#: and still confirm it: the quote uncertainty the guard's pair gate allows
#: (``option_basis_guard.MAX_QUOTE_UNCERTAINTY``, half its 5% tolerance).
LINEARITY_TOLERANCE = 0.025
#: VERIFIED add-back (module docstring): the chain's step across an ex-date must lie in
#: [VERIFY_MIN, VERIFY_MAX] x the declared dividends of that gap. RELATIVE to the dividend, not
#: to the spot: an unpriced special of size D (> 2.5% of spot) shows a step ~0, which clears a
#: lower bound of VERIFY_MIN * D (>= 1.25% of spot) only if the two quotes are off by that much
#: in opposite directions; a spot-sized absolute tolerance would pass a special just above the
#: cap. 0.5 and not tighter because the REAL STLA 2022-03 chain shows 0.75-0.85 of the declared
#: amount (a step of 5-6% of spot for a 7.4-7.8% dividend, measured 2026-11-05). VERIFY_MAX only
#: rejects a step the calendar badly understates.
VERIFY_MIN = 0.5
VERIFY_MAX = 1.5


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
    #: Non-payers only (None for a payer): a third expiry confirms the line (module docstring).
    linear_confirmed: Optional[bool] = None


def _verify(ex: date, rows: Sequence[Tuple[date, float]], pts: Sequence[Tuple[date, float]]
            ) -> Tuple[Optional[float], str]:
    """(add-back factor or None, why not) for the dividend ex ``ex``: does THIS session's chain
    show it priced? The factor is ``min(1, step / D)``: the chain's own evidence caps the add-back.

    The last usable expiry before the ex-date (``e_pre``, it settles before the dividend and
    carries none of it) and the first on/after it (``e_post``, it carries it) are adjacent across
    the ex-date; every declared row with ``e_pre < ex <= e_post`` is in the step between them, so
    the expected step is their sum ``D``. Verified when ``VERIFY_MIN * D <= p_pre - p_post <=
    VERIFY_MAX * D`` (prices, so no spot is needed). A verified row is added back at most as
    far as the chain shows it: the REAL STLA 2022-03 chain steps 0.67-0.85 D, and the full D
    would overshoot into a term structure sloping up."""
    pre = [(e, p) for e, p in pts if e < ex]
    post = [(e, p) for e, p in pts if e >= ex]
    if not pre:
        return None, f"no usable expiry before the ex-date {ex.isoformat()} to show the drop"
    if not post:
        return None, f"no usable expiry on/after the ex-date {ex.isoformat()}"
    (e_pre, p_pre), (e_post, p_post) = pre[-1], post[0]
    expected = sum(a for x, a in rows if e_pre < x <= e_post)
    step = p_pre - p_post
    if VERIFY_MIN * expected <= step <= VERIFY_MAX * expected:
        return min(1.0, step / expected), ""
    return None, (f"the chain step across the ex-date {ex.isoformat()} ({e_pre.isoformat()} "
                   f"{p_pre:.4g} -> {e_post.isoformat()} {p_post:.4g} = {step:.4g}) is not "
                   f"{VERIFY_MIN:g}-{VERIFY_MAX:g}x the dividends of that gap ({expected:.4g}): "
                   f"not priced")


def _dividends(session: date, until: date, dividends: Sequence[DividendRow], spot: float,
               pts: Sequence[Tuple[date, float]] = ()) -> Tuple[float, Optional[str], int]:
    """(total to add back for session < ex <= until, veto or None, rows not yet declared)."""
    total, pending, rows = 0.0, 0, []
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
            rows.append((r.ex_date, float(r.amount)))
    if total > 0 and (not (spot > 0) or total / spot > MAX_ADDBACK_FRACTION):
        # Over the cap: a row stays in the add-back only when the same session's chain shows it
        # priced; the UNVERIFIED remainder is what the cap judges. The verification reads the
        # whole declared calendar (rows beyond ``until`` can share a gap), not just ``rows``.
        every = [(x.ex_date, float(x.amount)) for x in dividends
                 if x.declared is not None and x.declared <= session and x.amount is not None
                 and x.amount == x.amount and x.amount > 0 and x.ex_date > session]
        unverified, why, added = [], [], 0.0
        for ex, a in rows:
            factor, reason = _verify(ex, every, pts)
            if factor is None:
                unverified.append((ex, a))
                why.append(reason)
                added += a
            else:
                added += a * factor
        un = sum(a for _, a in unverified)
        if un > 0 and (not (spot > 0) or un / spot > MAX_ADDBACK_FRACTION):
            used = ", ".join(f"{x.isoformat()} {a:g}" for x, a in unverified)
            spot_txt = f"{un / spot:.1%}" if spot > 0 else "n/a"
            return added, (f"dividends to add back ({used}) total {spot_txt} of "
                           f"the spot {spot:g} (> {MAX_ADDBACK_FRACTION:.1%}): unverified, an "
                           f"add-back that large could hide a basis error of its size (a special "
                           f"OCC strike-adjusts is not priced by the chain); verification "
                           f"failed: {'; '.join(why)}"), pending
        return added, None, pending
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
    _, veto, _ = _dividends(session, (pick or pts[1])[0], dividends, spot, pts)
    if veto:
        return CarryFreeSpot(None, veto, veto=veto)
    if pick is None:
        return CarryFreeSpot(None, f"no second expiry keeps the extrapolation leverage t1/(t2-t1) "
                                   f"<= {MAX_LEVERAGE} (nearest {t1}d)")
    e2, p2 = pick
    t2 = (e2 - session).days
    d1, veto, _ = _dividends(session, e1, dividends, spot, pts)
    if veto:
        return CarryFreeSpot(None, veto, veto=veto)
    d2, veto, pending = _dividends(session, e2, dividends, spot, pts)
    if veto:
        return CarryFreeSpot(None, veto, veto=veto)
    s1, s2 = p1 + d1, p2 + d2
    if dividends and spot > 0 and (s1 - s2) / spot > MAX_ADDBACK_FRACTION:
        why = (f"the parity drop between {t1}d and {t2}d ({(p1 - p2) / spot:.1%} of the spot "
               f"{spot:g}) is not explained by the dividend calendar ({(d2 - d1) / spot:.1%}) "
               f"within {MAX_ADDBACK_FRACTION:.1%}: on a dividend payer that is an uncalendared "
               f"dividend, not carry")
        return CarryFreeSpot(None, why, veto=why)
    if s2 > s1 * (1.0 + SLOPE_SLACK):
        why = (f"the term structure slopes up ({t1}d {s1:.4g} -> {t2}d {s2:.4g}): not the "
               f"shape of carry")
        return CarryFreeSpot(None, why, veto=why)
    est = s1 - t1 * (s2 - s1) / (t2 - t1)
    note = f"; {pending} dividend(s) not yet declared, not added" if pending else ""
    why = f"extrapolated to T=0 from {t1}d and {t2}d, declared dividends added back{note}"
    if dividends:
        return CarryFreeSpot(est, why, expiries_used=2)
    # A NON-PAYER: a third expiry at least as far again must confirm the line.
    third = next(((e, p) for e, p in pts if (e - session).days - t2 >= t2 - t1), None)
    if third is None:
        return CarryFreeSpot(est, f"{why}; non-payer: no third usable expiry >= {2 * t2 - t1}d to "
                                  f"confirm the line (a step would be an uncalendared dividend)",
                             expiries_used=2, linear_confirmed=False)
    e3, p3 = third
    t3 = (e3 - session).days
    pred = p1 + (p2 - p1) * (t3 - t1) / (t2 - t1)
    dev = (p3 - pred) / spot if spot > 0 else float("inf")
    if not abs(dev) <= LINEARITY_TOLERANCE:
        return CarryFreeSpot(est, f"{why}; non-payer: the {t3}d expiry is {dev:+.1%} of the spot "
                                  f"off the line through {t1}d and {t2}d (> "
                                  f"{LINEARITY_TOLERANCE:.1%}): a step, not a borrow slope",
                             expiries_used=2, linear_confirmed=False)
    return CarryFreeSpot(est, f"{why}; non-payer: the {t3}d expiry confirms the line "
                              f"({dev:+.1%})", expiries_used=3, linear_confirmed=True)
