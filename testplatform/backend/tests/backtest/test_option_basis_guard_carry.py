"""The E4 basis guard separates carry from a basis error (user decision 2026-09-26: fix, no
waiver).

When the nearest-expiry median is off, the guard now re-measures the same sessions with the
carry-free spot (``ba2_common.core.option_carry``: dividends added back, the two nearest
expiries extrapolated to T = 0). It refuses only when that is off too -- or cannot be computed,
which the refusal then names. So every run that passed before still passes (the check is only
consulted on what used to be a refusal), AMC July 2023 (borrow) and STLA April 2022 (dividend)
pass, and a x2 / x1.25 basis error is still refused, dividend or not.

The sessions below are the REAL parity / spot ratios of the ThetaData store (per expiry, the
guard's own pair gates), measured 2026-09-26.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from ba2_common.core.option_carry import DividendRow

#: As-traded spot per session and (days to expiry, parity/spot) of the usable expiries among
#: the nearest 8 (DTE >= 2; the first one is the nearest-expiry measure), from the store.
AMC = {
    date(2023, 6, 1): (4.55, [(8, 0.9879), (15, 0.9747), (22, 0.9615), (29, 0.9396)]),
    date(2023, 6, 2): (4.55, [(7, 0.9945), (14, 0.9824), (21, 0.9692), (28, 0.9527)]),
    date(2023, 6, 5): (4.63, [(4, 0.9914), (11, 0.9795), (18, 0.9633), (25, 0.9449)]),
    date(2023, 6, 6): (4.64, [(3, 0.9957), (10, 0.9838), (17, 0.9741), (24, 0.9483)]),
    date(2023, 6, 7): (4.73, [(2, 0.9979), (9, 0.981), (16, 0.963), (23, 0.9429)]),
    date(2023, 6, 8): (4.68, [(8, 0.9829), (15, 0.9637), (29, 0.937), (36, 0.9081)]),
    date(2023, 6, 9): (4.77, [(7, 0.9811), (14, 0.9665), (21, 0.9392), (28, 0.9161)]),
    date(2023, 6, 12): (4.79, [(4, 0.9875), (11, 0.9697), (18, 0.9457), (25, 0.9207)]),
    date(2023, 6, 13): (5.00, [(3, 0.985), (10, 0.967), (17, 0.948), (38, 0.87)]),
    date(2023, 6, 14): (4.90, [(2, 0.9898), (9, 0.9714), (16, 0.9551), (23, 0.9235)]),
    date(2023, 6, 15): (4.76, [(8, 0.9853), (15, 0.9716), (36, 0.8792)]),
    date(2023, 6, 16): (4.70, [(7, 0.983), (14, 0.9617), (21, 0.9404), (35, 0.8809)]),
    date(2023, 6, 20): (4.59, [(3, 0.9891), (10, 0.9641), (17, 0.9346), (24, 0.9183)]),
    date(2023, 6, 21): (4.53, [(2, 0.9923), (9, 0.9746), (16, 0.9492), (23, 0.9283)]),
    date(2023, 6, 22): (4.17, [(8, 0.9724), (15, 0.9496), (29, 0.8981), (36, 0.8669)]),
    date(2023, 6, 23): (4.01, [(7, 0.9751), (14, 0.9476), (28, 0.884), (56, 0.7893)]),
    date(2023, 6, 26): (4.03, [(4, 0.9814), (11, 0.9516), (18, 0.9169), (25, 0.871)]),
    date(2023, 6, 27): (4.10, [(3, 0.9817), (10, 0.9488), (17, 0.9146), (24, 0.8671)]),
    date(2023, 6, 28): (4.41, [(2, 0.9785), (9, 0.9388), (16, 0.898), (23, 0.8435)]),
    date(2023, 6, 29): (4.42, [(8, 0.9491), (15, 0.8914), (22, 0.8575)]),
    date(2023, 6, 30): (4.40, [(7, 0.9705), (14, 0.9159), (21, 0.8352), (28, 0.808)]),
    date(2023, 7, 3): (4.44, [(4, 0.9797), (11, 0.9437), (18, 0.8604)]),
    date(2023, 7, 5): (4.25, [(2, 0.9953), (9, 0.9388), (16, 0.8729)]),
    date(2023, 7, 6): (4.29, [(8, 0.9266), (15, 0.8555)]),
    date(2023, 7, 7): (4.20, [(7, 0.931), (14, 0.9024), (21, 0.8583)]),
    date(2023, 7, 10): (4.24, [(4, 0.9599), (11, 0.8679)]),
    date(2023, 7, 11): (4.39, [(3, 0.9636), (10, 0.8929)]),
    date(2023, 7, 12): (4.40, [(2, 0.9773), (9, 0.8841), (23, 0.825)]),
    date(2023, 7, 13): (4.44, [(8, 0.9178), (15, 0.8266), (36, 0.7568)]),
    date(2023, 7, 14): (4.33, [(7, 0.9076), (35, 0.7587)]),
    date(2023, 7, 17): (4.37, [(4, 0.9531), (11, 0.857)]),
    date(2023, 7, 18): (4.31, [(3, 0.9652), (10, 0.8921)]),
    date(2023, 7, 19): (4.37, [(2, 0.9703), (9, 0.8936), (16, 0.8375), (30, 0.7918)]),
    date(2023, 7, 20): (4.33, [(8, 0.9215), (15, 0.8441)]),
    date(2023, 7, 21): (4.40, [(7, 0.933), (14, 0.8705), (21, 0.8693)]),
    date(2023, 7, 24): (5.85, [(4, 0.9573), (11, 0.8991), (18, 0.859), (25, 0.8162)]),
    date(2023, 7, 25): (5.12, [(3, 0.9766), (10, 0.9307), (17, 0.8896), (24, 0.8535)]),
    date(2023, 7, 26): (5.07, [(2, 0.9822), (9, 0.9349), (16, 0.8955), (23, 0.856)]),
    date(2023, 7, 27): (4.52, [(8, 0.958), (15, 0.9292), (22, 0.8938), (29, 0.8551)]),
    date(2023, 7, 28): (4.64, [(7, 0.9623), (14, 0.9289), (21, 0.8998), (28, 0.875)]),
    date(2023, 7, 31): (4.97, [(4, 0.9789), (11, 0.9447), (18, 0.9145), (25, 0.8763)]),
}
STLA = {
    date(2022, 3, 1): (16.82, [(17, 1.0018), (108, 0.9572), (199, 0.9438)]),
    date(2022, 3, 2): (17.22, [(16, 1.0003), (107, 0.948), (198, 0.9437)]),
    date(2022, 3, 3): (16.47, [(15, 1.0049), (106, 0.9532), (197, 0.9457)]),
    date(2022, 3, 4): (15.04, [(14, 1.0023), (105, 0.9425), (196, 0.9342)]),
    date(2022, 3, 7): (13.79, [(11, 1.0116), (193, 0.9318)]),
    date(2022, 3, 8): (14.32, [(10, 1.0003), (101, 0.9497), (192, 0.941)]),
    date(2022, 3, 9): (15.97, [(9, 0.9987), (100, 0.9487), (191, 0.9408)]),
    date(2022, 3, 10): (14.95, [(8, 1.0017), (99, 0.9415), (190, 0.9365)]),
    date(2022, 3, 11): (14.59, [(7, 1.0058), (189, 0.9441)]),
    date(2022, 3, 14): (15.16, [(4, 0.996), (95, 0.9383), (186, 0.9334)]),
    date(2022, 3, 15): (15.49, [(3, 1.0023), (94, 0.9425), (185, 0.9377)]),
    date(2022, 3, 16): (16.66, [(2, 0.9934), (93, 0.9499), (184, 0.9394)]),
    date(2022, 3, 17): (16.41, [(92, 0.9476)]),
    date(2022, 3, 18): (16.30, [(63, 0.9571), (91, 0.9479), (182, 0.9371)]),
    date(2022, 3, 21): (16.19, [(60, 0.9543), (88, 0.9497), (179, 0.9358)]),
    date(2022, 3, 22): (16.38, [(59, 0.9631), (87, 0.9554), (178, 0.9417)]),
    date(2022, 3, 23): (15.81, [(58, 0.9519), (86, 0.9472), (177, 0.944)]),
    date(2022, 3, 24): (15.98, [(57, 0.9528), (85, 0.9496), (176, 0.9371)]),
    date(2022, 3, 25): (15.86, [(56, 0.9521), (84, 0.9458), (175, 0.941)]),
    date(2022, 3, 28): (15.92, [(53, 0.9516), (81, 0.9469), (172, 0.9391)]),
    date(2022, 3, 29): (17.08, [(52, 0.9426), (80, 0.9617), (171, 0.9441)]),
    date(2022, 3, 30): (16.79, [(51, 0.9649), (79, 0.9574), (170, 0.944)]),
    date(2022, 3, 31): (16.27, [(50, 0.9557), (78, 0.9542), (169, 0.9435)]),
    date(2022, 4, 1): (16.35, [(49, 0.9664), (77, 0.948), (168, 0.9404)]),
    date(2022, 4, 4): (16.35, [(46, 0.9587), (165, 0.9404)]),
    date(2022, 4, 5): (15.55, [(45, 0.9486), (73, 0.9421), (164, 0.9405)]),
    date(2022, 4, 6): (14.94, [(44, 0.9354), (72, 0.9321), (163, 0.9371)]),
    date(2022, 4, 7): (14.94, [(43, 0.9321), (71, 0.9304), (162, 0.9354)]),
    date(2022, 4, 8): (14.85, [(42, 0.9327), (70, 0.9327), (161, 0.9343)]),
    date(2022, 4, 11): (15.14, [(39, 0.933), (67, 0.9297), (158, 0.9379)]),
    date(2022, 4, 12): (14.90, [(38, 0.9329), (66, 0.9279), (157, 0.9312)]),
    date(2022, 4, 13): (15.22, [(37, 0.933), (65, 0.933), (156, 0.9313)]),
    date(2022, 4, 14): (15.00, [(36, 0.9317), (64, 0.9283), (155, 0.9383)]),
    date(2022, 4, 18): (15.10, [(32, 0.9321), (60, 0.9305), (151, 0.9371)]),
    date(2022, 4, 19): (14.47, [(31, 1.0003), (59, 1.0055), (150, 1.0021)]),
    date(2022, 4, 20): (14.45, [(30, 1.0), (58, 1.0017), (149, 1.0069)]),
    date(2022, 4, 21): (14.38, [(29, 1.0014), (57, 0.9997), (148, 1.0083)]),
    date(2022, 4, 22): (14.02, [(28, 1.0021), (56, 1.0021), (147, 1.0075)]),
    date(2022, 4, 25): (14.00, [(25, 1.0), (53, 0.9982), (144, 1.0054)]),
    date(2022, 4, 26): (13.08, [(24, 0.9996), (52, 1.0034), (143, 1.0054)]),
    date(2022, 4, 27): (13.29, [(23, 1.0008), (51, 1.0008), (142, 1.0064)]),
    date(2022, 4, 28): (13.65, [(22, 1.0), (50, 1.0), (141, 1.0073)]),
    date(2022, 4, 29): (13.37, [(21, 0.9966), (49, 1.0041), (140, 1.006)]),
    date(2022, 5, 2): (13.40, [(18, 0.9981), (46, 0.9981), (137, 1.0019)]),
    date(2022, 5, 3): (13.73, [(17, 0.9996), (45, 1.0015), (136, 1.0051)]),
    date(2022, 5, 4): (14.11, [(16, 0.9975), (44, 0.9993), (135, 1.0046)]),
    date(2022, 5, 5): (13.42, [(15, 0.9985), (43, 0.9985), (134, 1.0041)]),
    date(2022, 5, 6): (13.93, [(14, 0.9996), (42, 1.0014), (133, 1.0068)]),
}
#: FMP: ex 2022-04-19, adjDividend 1.1221, declared 2022-02-25 (7.5% of the spot: carry).
STLA_DIVS = [DividendRow(date(2022, 4, 19), 1.1221, date(2022, 2, 25))]
#: From 2022-03-17 every usable expiry is after the ex-date: only the calendar removes the step.
STLA_APRIL = {d: v for d, v in STLA.items() if d >= date(2022, 4, 4)}


def _guard(monkeypatch, table, dividends=None, scale=1.0):
    """A guard over ``table`` {session: (as-traded spot, [(dte, parity/spot), ...])}; the run's
    spot is ``scale`` x the as-traded one (scale != 1: a basis error)."""
    import numpy as np
    import app.services.backtest.option_basis_guard as g

    days = sorted(table)

    class _Raw:
        underlying, n_rows, c_occ = "X", len(days), []
        date_of_ord = {d.toordinal(): d for d in days}
        bar_ord = np.array([d.toordinal() for d in days])
        c_expiry_ord = np.array([], dtype=int)

    u = type("U", (), {"raw": _Raw()})()

    def term(u_, o):
        spot, pts = table[date.fromordinal(o)]
        return [(o + t, spot * r) for t, r in pts if t >= 2]

    monkeypatch.setattr(g, "parity_spot", lambda u_, o: (term(u_, o) or [(None, None)])[0][1])
    monkeypatch.setattr(g, "expiry_parities", term)
    g.clear_basis_guard_cache()
    spot = lambda s, d: table[d][0] * scale
    return g, u, g.BasisGuard(spot, dividends=dividends)


def _run(g, u, guard, days):
    for d in days:
        guard.check(u, "X", d)


def _phases(monkeypatch, table, dividends, carry=True):
    """Run the guard from each of 5 start offsets (a run starting on another day samples other
    sessions); returns the refusals. ``carry=False``: the guard as it was before the fix."""
    import app.services.backtest.option_basis_guard as g0
    refused = []
    days = sorted(table)
    for ph in range(5):
        g, u, guard = _guard(monkeypatch, table, dividends=dividends)
        if not carry:
            monkeypatch.setattr(g, "expiry_parities", lambda u_, o: [])
        try:
            _run(g, u, guard, days[ph:])
        except g0.OptionSpotBasisMismatch as e:
            refused.append((ph, str(e)))
    return refused


def test_amc_june_july_2023_was_refused_and_now_passes_from_every_start(monkeypatch):
    assert _phases(monkeypatch, AMC, lambda s, d: [], carry=False)       # the old verdict
    assert _phases(monkeypatch, AMC, lambda s, d: []) == []


def test_stla_s_7_5_percent_dividend_is_refused_by_name_not_cleared(monkeypatch):
    """Review round 3: an unverified add-back above 2.5% of the spot is a veto -- STLA's $1.12
    on $14.90 cannot be told from an unpriced special hiding an error of its size. (STLA never
    aborted under the REAL guard's session sampling; this table is the store's own sessions.)"""
    refusals = _phases(monkeypatch, STLA_APRIL, lambda s, d: STLA_DIVS)
    assert refusals and all("> 2.5%" in msg for _ph, msg in refusals)


def test_stla_without_a_dividend_calendar_is_refused_and_says_why(monkeypatch):
    g, u, guard = _guard(monkeypatch, STLA_APRIL, dividends=lambda s, d: None)
    with pytest.raises(g.OptionSpotBasisMismatch, match="no dividend calendar cached"):
        _run(g, u, guard, sorted(STLA_APRIL))


@pytest.mark.parametrize("scale", [2.0, 0.5, 1.25, 1.3])
@pytest.mark.parametrize("table,divs", [(AMC, []), (STLA, STLA_DIVS)])
def test_a_basis_error_is_still_refused_with_or_without_carry(monkeypatch, scale, table, divs):
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: divs, scale=scale)
    with pytest.raises(g.OptionSpotBasisMismatch, match="carry"):
        _run(g, u, guard, sorted(table))


def test_a_synthetic_x125_error_on_a_dividend_payer_is_refused(monkeypatch):
    s0 = date(2024, 5, 1)
    days, d = [], s0
    while len(days) < 12:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    ex = date(2024, 5, 20)
    table = {}
    for day in days:
        dv = 2.0 if day < ex else 0.0         # every expiry is after the ex-date
        table[day] = (100.0, [(30, (100.0 - dv) / 100.0), (58, (100.0 - dv - 0.2) / 100.0)])
    divs = [DividendRow(ex, 2.0 * 1.25, date(2024, 4, 1))]
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: divs, scale=1.25)
    with pytest.raises(g.OptionSpotBasisMismatch):
        _run(g, u, guard, days)


def _monthly(start, n=14):
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def test_an_occ_adjusted_special_cannot_explain_away_a_x125_error(monkeypatch):
    """The reviewer's composite: ACI-like $6.85 special on $27.50 (25%, OCC strike-adjusts it,
    so the chain past the ex-date is NOT lowered) and a run spot 1.25x off. Adding the special
    back used to clear it; an add-back above 2.5% of the spot is now a veto, refused by name."""
    days = _monthly(date(2022, 10, 17), n=9)          # declared 10-14: known on every session
    table = {}
    for day in days:
        t1, t2 = (date(2022, 11, 18) - day).days, (date(2022, 12, 16) - day).days
        table[day] = (27.5, [(t1, 1.0 - 0.0002 * t1), (t2, 1.0 - 0.0002 * t2)])
    special = [DividendRow(date(2022, 10, 28), 6.85 * 1.25, date(2022, 10, 14))]
    for scale in (1.25, 1.30):
        g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: special, scale=scale)
        with pytest.raises(g.OptionSpotBasisMismatch, match="OCC strike-adjusts"):
            _run(g, u, guard, days)


@pytest.mark.parametrize("frac", [0.09, 0.07, 0.05])
def test_an_unpriced_special_under_ten_percent_is_refused(monkeypatch, frac):
    """Review round 3 (N2): a 9% / 7% / 5% special the chain does not price, with a run spot off
    by the same factor -- the add-back would land exactly on the wrong spot."""
    days = _monthly(date(2022, 10, 17), n=9)
    table = {}
    for day in days:
        t1, t2 = (date(2022, 11, 18) - day).days, (date(2022, 12, 16) - day).days
        table[day] = (100.0, [(t1, 1.0 - 0.0002 * t1), (t2, 1.0 - 0.0002 * t2)])
    special = [DividendRow(date(2022, 10, 28), 100.0 * frac, date(2022, 10, 14))]
    for ph in range(3):
        g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: special, scale=1 + frac)
        with pytest.raises(g.OptionSpotBasisMismatch, match="> 2.5%"):
            _run(g, u, guard, days[ph:])


@pytest.mark.parametrize("row,match", [
    (DividendRow(date(2023, 3, 16), 21.6693, None), "declaration date unknown"),      # AMX garbage
    (DividendRow(date(2023, 3, 16), 21.6693, date(2023, 2, 1)), "> 2.5%"),
    (DividendRow(date(2023, 3, 20), 0.875 * 0.95, date(2023, 2, 10)), "> 2.5%"),      # HL-like
    (DividendRow(date(2023, 3, 16), None, date(2023, 2, 1)), "no split-adjusted amount"),
])
def test_garbage_calendar_rows_refuse_by_name(monkeypatch, row, match):
    days = _monthly(date(2023, 3, 1))
    table = {d: (5.0, [(30, 0.93), (58, 0.925)]) for d in days}
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [row])
    with pytest.raises(g.OptionSpotBasisMismatch, match=match):
        _run(g, u, guard, days)


def test_the_qgen_synthetic_repurchase_refuses_by_name(monkeypatch):
    """QGEN 2026-01-08: a 0.95 'split' plus a 2.29 cash row with no declaration date, the
    capital repayment depressing the Dec 2025 expiries' parity (0.94-0.95)."""
    days = _monthly(date(2025, 12, 1))
    table = {d: (45.0, [(20, 0.945), (48, 0.94)]) for d in days}
    rows = [DividendRow(date(2026, 1, 8), 2.29, None)]
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: rows)
    with pytest.raises(g.OptionSpotBasisMismatch, match="declaration date unknown"):
        _run(g, u, guard, days)


def test_the_carry_path_is_one_sided(monkeypatch):
    """Borrow and dividends only LOWER the parity spot: a median above 1 + tolerance is never
    cleared, whatever the term structure says."""
    days = _monthly(date(2024, 2, 1))
    table = {d: (100.0, [(10, 1.08), (40, 1.07)]) for d in days}
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    with pytest.raises(g.OptionSpotBasisMismatch, match="ABOVE the as-traded spot"):
        _run(g, u, guard, days)


def test_too_few_expiries_keeps_the_old_refusal_and_says_so(monkeypatch):
    table = {d: (4.40, [(8, 0.90)]) for d in sorted(AMC)}
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    with pytest.raises(g.OptionSpotBasisMismatch, match="needs >= 2 expiries"):
        _run(g, u, guard, sorted(table))


def test_an_upward_term_structure_is_refused_by_name(monkeypatch):
    days = _monthly(date(2024, 2, 1))
    table = {d: (100.0, [(10, 0.92), (40, 0.94)]) for d in days}
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    with pytest.raises(g.OptionSpotBasisMismatch, match="slopes up"):
        _run(g, u, guard, days)


def test_a_stretch_basis_error_cannot_borrow_clean_sessions_from_before_it(monkeypatch):
    """Review round 3 (N1): a x1.057 / x1.10 error over a stretch with no split marking it
    (FLEX/CMCSA/QGEN-like), on a thin chain whose earlier sessions had short-dated expiries.
    There is no look-back any more: it is refused from every start."""
    days = _monthly(date(2022, 2, 1), n=30)
    stretch = [d for d in days if date(2022, 2, 22) <= d <= date(2022, 3, 15)]
    for err in (1.057, 1.10):
        table = {}
        for d in days:
            if d in stretch:
                table[d] = (100.0, [(60, 1.0)])                  # one usable expiry only
            else:
                table[d] = (100.0, [(4, 1.0), (60, 0.999)])
        for ph in range(5):
            import app.services.backtest.option_basis_guard as g0
            g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
            spot = guard.spot_source
            guard.spot_source = (lambda f: (lambda s, d: f(s, d) * (err if d in stretch else 1.0)))(spot)
            with pytest.raises(g0.OptionSpotBasisMismatch):
                _run(g, u, guard, [d for d in days if d >= stretch[0]][ph:])


def test_without_a_dividend_source_the_carry_path_refuses(monkeypatch):  # noqa: D401
    """A guard wired with no dividend source cannot tell a dividend from a basis error: it
    keeps the refusal and names the missing calendar (the warm tool covers the universe)."""
    g, u, guard = _guard(monkeypatch, AMC)
    assert _phases(monkeypatch, AMC, None)
    with pytest.raises(g.OptionSpotBasisMismatch, match="no dividend calendar cached"):
        for ph in range(5):
            g, u, guard = _guard(monkeypatch, AMC)
            _run(g, u, guard, sorted(AMC)[ph:])


#: A thin hard-to-borrow chain (IONQ March 2022 had monthlies only: 2022-03-28 carried ONE usable
#: expiry, 109d, while its neighbours had two). Synthetic borrow of 50%/yr, spot 100: parity =
#: 100 x exp(-0.5 T). The checked session has only its 108d expiry, so its own carry-free ratio
#: cannot be stated -- the four window sessions before it can.
def _borrow(t):
    import math
    return round(math.exp(-0.5 * t / 365.0), 4)


IONQ = {d: (100.0, [(45, _borrow(45)), (108, _borrow(108))])
        for d in (date(2022, 3, 21), date(2022, 3, 22), date(2022, 3, 23), date(2022, 3, 24))}
IONQ[date(2022, 3, 25)] = (100.0, [(108, _borrow(108))])


def test_a_session_without_its_own_carry_ratio_passes_on_its_window(monkeypatch):
    """The window median decides, as for the uncorrected ratio: >= 3 window sessions with a
    carry-free ratio, their median within 5%."""
    g, u, guard = _guard(monkeypatch, IONQ, dividends=lambda s, d: [])
    guard.check(u, "X", date(2022, 3, 25))            # first check: window filled backwards
    assert guard.stats()["carry_passed"] == 1


def test_the_same_window_with_a_basis_error_is_refused(monkeypatch):
    g, u, guard = _guard(monkeypatch, IONQ, dividends=lambda s, d: [], scale=1.25)
    with pytest.raises(g.OptionSpotBasisMismatch, match="carry-free"):
        guard.check(u, "X", date(2022, 3, 25))


def test_fewer_than_three_carry_sessions_in_a_full_window_is_refused(monkeypatch):
    thin = {d: (v[0], v[1][-1:]) for d, v in IONQ.items()}
    thin[date(2022, 3, 23)] = IONQ[date(2022, 3, 23)]
    thin[date(2022, 3, 24)] = IONQ[date(2022, 3, 24)]    # 2 of 5 measurable
    g, u, guard = _guard(monkeypatch, thin, dividends=lambda s, d: [])
    with pytest.raises(g.OptionSpotBasisMismatch, match="2 of 5"):
        guard.check(u, "X", date(2022, 3, 25))


# ---- the carry band (final review, R1) ---------------------------------------------------------
def _verdicts(monkeypatch, table, divs, scale, phases=3):
    import app.services.backtest.option_basis_guard as g0
    out = []
    for ph in range(phases):
        g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: divs, scale=scale)
        try:
            _run(g, u, guard, sorted(table)[ph:])
            out.append("PASS")
        except g0.OptionSpotBasisMismatch:
            out.append("REFUSE")
    return out


def _unpriced_special_table():
    """Flat monthly chain, both usable expiries past the special's ex-date, which the chain
    does NOT price (OCC strike-adjusted)."""
    table = {}
    for day in _monthly(date(2022, 10, 17), n=9):
        t1, t2 = (date(2022, 11, 18) - day).days, (date(2022, 12, 16) - day).days
        table[day] = (27.5, [(t1, 1 - 0.0002 * t1), (t2, 1 - 0.0002 * t2)])
    return table


#: A dividend payer's calendar as seen from the sessions of ``_missing_dividend_table``: a later
#: regular dividend (after both usable expiries, not yet declared -- nothing to add back). The
#: dividend the chain prices between the two expiries is MISSING from it.
PAYER_ROWS = [DividendRow(date(2024, 8, 15), 0.9, date(2024, 7, 25))]


def _missing_dividend_table(frac):
    """A dividend of ``frac`` NOT in the calendar, between the two usable expiries."""
    table = {}
    for day in _monthly(date(2024, 3, 25), n=10):
        t1, t2 = (date(2024, 4, 19) - day).days, (date(2024, 5, 17) - day).days
        table[day] = (100.0, [(t1, 1.0), (t2, 1.0 - frac)])
    return table


def test_the_band_is_derived_from_the_constants():
    import app.services.backtest.option_basis_guard as g
    from ba2_common.core.option_carry import MAX_ADDBACK_FRACTION
    assert g.CARRY_BAND_LOW == pytest.approx(1.0 - (g.TOLERANCE - MAX_ADDBACK_FRACTION))
    assert g.CARRY_BAND_LOW == pytest.approx(0.975)


@pytest.mark.parametrize("scale", [1.06, 1.07, 1.075, 1.08, 1.09, 1.10, 1.25])
def test_an_unpriced_2_5_percent_special_cannot_hide_an_error_past_the_line(monkeypatch, scale):
    divs = [DividendRow(date(2022, 10, 28), 27.5 * 0.025 * scale, date(2022, 10, 14))]
    assert _verdicts(monkeypatch, _unpriced_special_table(), divs, scale) == ["REFUSE"] * 3


@pytest.mark.parametrize("frac,scale", [(0.05, 1.08), (0.05, 1.10), (0.05, 1.25), (0.03, 1.06),
                                        (0.03, 1.08), (0.01, 1.06)])
def test_a_dividend_missing_from_the_calendar_cannot_hide_an_error(monkeypatch, frac, scale):
    assert _verdicts(monkeypatch, _missing_dividend_table(frac), [], scale) == ["REFUSE"] * 3


def test_amc_still_clears_inside_the_band(monkeypatch):
    assert _phases(monkeypatch, AMC, lambda s, d: [], carry=False)
    assert _phases(monkeypatch, AMC, lambda s, d: []) == []


# ---- the unexplained-drop veto (review of 5178e625..90980f18, item 3) --------------------------
@pytest.mark.parametrize("frac,scale", [(0.08, 1.08), (0.10, 1.10), (0.15, 1.15), (0.20, 1.15),
                                        (0.05, 1.06), (0.03, 1.06)])
def test_a_payers_uncalendared_dividend_cannot_hide_an_error_of_its_size(monkeypatch, frac, scale):
    """A dividend payer whose calendar lacks the dividend the chain prices between the two
    expiries: the drop between them is not carry the calendar explains, so the extrapolation
    may not add it back. Before the veto 8%->x1.08 and 10%->x1.10 passed on 4 of 5 offsets."""
    assert _verdicts(monkeypatch, _missing_dividend_table(frac), PAYER_ROWS, scale,
                     phases=5) == ["REFUSE"] * 5


def test_the_unexplained_drop_veto_names_itself(monkeypatch):
    import app.services.backtest.option_basis_guard as g0
    table = _missing_dividend_table(0.08)
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: PAYER_ROWS, scale=1.08)
    with pytest.raises(g0.OptionSpotBasisMismatch, match="not explained by the dividend calendar"):
        _run(g, u, guard, sorted(table))


def test_amc_still_clears_with_the_veto(monkeypatch):
    """AMC pays no dividend in the horizon: its term drop (up to 15% between two weeklies) is the
    borrow fee, and the TERM measure stays the one that clears it."""
    assert _phases(monkeypatch, AMC, lambda s, d: []) == []


@pytest.mark.parametrize("case", ["special 2.5% + x1.051", "payer, missing 2.5% + x1.055",
                                  "no dividend in the horizon, missing 8% + x1.08"])
def test_the_residual_after_the_veto(monkeypatch, case):
    """THE RESIDUAL as it stands after the unexplained-drop veto (measured 2026-09-26 over
    missing dividends of 1-20% and errors of x1.051-x1.20, 5 start offsets):
      * a special of up to 2.5% of the spot that the chain does not price (OCC strike-adjusted)
        can carry an error ~1% past the 5% line (x1.051 passes on 2 of 3 offsets) -- accepted
        2026-09-26 (coordinator decision, REPAIR_LOG);
      * on a dividend PAYER an uncalendared dividend no longer hides anything past the line:
        x1.055 is refused on every offset for every size;
      * a symbol whose calendar lists NO dividend in the horizon is read as borrow (AMC must
        clear): a dividend the chain prices but FMP's calendar lacks ENTIRELY hides an error of
        about its own size (8% -> x1.08, 10% -> x1.10, 15-20% -> x1.15 pass on 4 of 5 offsets).
    Pinned so that any change to it is seen."""
    if case.startswith("special"):
        divs = [DividendRow(date(2022, 10, 28), 27.5 * 0.025 * 1.051, date(2022, 10, 14))]
        assert _verdicts(monkeypatch, _unpriced_special_table(), divs, 1.051) ==             ["REFUSE", "PASS", "PASS"]
    elif case.startswith("payer"):
        assert _verdicts(monkeypatch, _missing_dividend_table(0.025), PAYER_ROWS, 1.055,
                         phases=5) == ["REFUSE"] * 5
    else:
        assert _verdicts(monkeypatch, _missing_dividend_table(0.08), [], 1.08, phases=5) ==             ["REFUSE", "PASS", "PASS", "PASS", "PASS"]
