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
STLA_DIVS = [(date(2022, 4, 19), 1.1221)]
#: From 2022-04-05 every usable expiry is after the ex-date: only the calendar removes the step.
STLA_APRIL = {d: v for d, v in STLA.items() if d >= date(2022, 4, 5)}


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


def test_stla_march_may_2022_was_refused_and_now_passes_with_its_dividend(monkeypatch):
    assert _phases(monkeypatch, STLA, lambda s, d: STLA_DIVS, carry=False)
    assert _phases(monkeypatch, STLA, lambda s, d: STLA_DIVS) == []


def test_stla_without_a_dividend_calendar_is_still_refused_and_says_why(monkeypatch):
    g, u, guard = _guard(monkeypatch, STLA_APRIL, dividends=lambda s, d: None)
    with pytest.raises(g.OptionSpotBasisMismatch, match="dividend calendar unavailable"):
        _run(g, u, guard, sorted(STLA_APRIL))
    g, u, guard = _guard(monkeypatch, STLA_APRIL, dividends=lambda s, d: STLA_DIVS)
    _run(g, u, guard, sorted(STLA_APRIL))


@pytest.mark.parametrize("scale", [2.0, 0.5, 1.25])
@pytest.mark.parametrize("table,divs", [(AMC, []), (STLA, STLA_DIVS)])
def test_a_basis_error_is_still_refused_with_or_without_carry(monkeypatch, scale, table, divs):
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: divs, scale=scale)
    with pytest.raises(g.OptionSpotBasisMismatch, match="carry-free"):
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
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [(ex, 2.0)], scale=1.25)
    with pytest.raises(g.OptionSpotBasisMismatch):
        _run(g, u, guard, days)


def test_too_few_expiries_keeps_the_old_refusal_and_says_so(monkeypatch):
    table = {d: (4.40, [(8, 0.90)]) for d in sorted(AMC)}
    g, u, guard = _guard(monkeypatch, table, dividends=lambda s, d: [])
    with pytest.raises(g.OptionSpotBasisMismatch, match="needs >= 2 expiries"):
        _run(g, u, guard, sorted(table))


def test_without_a_dividend_source_the_guard_behaves_as_before(monkeypatch):
    """A caller that wires no dividend source (the contract-detail view) still gets the carry
    separation for borrow; dividends are simply not added back, and the reason says so."""
    g, u, guard = _guard(monkeypatch, AMC)
    _run(g, u, guard, sorted(AMC))
    g2, u2, guard2 = _guard(monkeypatch, STLA_APRIL)
    with pytest.raises(g2.OptionSpotBasisMismatch, match="dividend calendar unavailable"):
        _run(g2, u2, guard2, sorted(STLA_APRIL))


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
