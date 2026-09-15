"""Tests for the pure market-condition calculators (design §3/§3.1/§7).

Expected numbers come from independent, deliberately naive loops written in
this file straight from the design text -- never from the module under test.
The reference reductions use ``math.fsum`` exactly as the module's
reproducibility contract states, so values are compared with ``==``.
"""
import math

import numpy as np
import pytest

from ba2_common.core.market_conditions import (
    WINDOW, CALC_VERSION, Observation,
    compute_market_conditions, ema_sma_seeded, atr14_wilder, adx14_wilder, realized_vol_ratio,
    STATUS_VALID, STATUS_INSUFFICIENT_HISTORY, STATUS_INVALID_PRICES, STATUSES,
    FIELD_TREND_SLOPE, FIELD_ADX, FIELD_RV_RATIO, FIELDS,
)


def _bars(closes, spread=0.5):
    c = np.asarray(closes, dtype=np.float64)
    h = c + spread; l = c - spread; o = c.copy(); v = np.full(len(c), 1e6)
    return o, h, l, c, v


def _walk(n, seed, start=100.0, step=1.0):
    rng = np.random.default_rng(seed)
    return start + np.cumsum(rng.normal(0.0, step, n))


def _random_ohlc(seed_c, seed_hl):
    rng = np.random.default_rng(seed_hl)
    c = _walk(WINDOW, seed=seed_c)
    h = c + rng.uniform(0.1, 1.5, WINDOW)
    l = c - rng.uniform(0.1, 1.5, WINDOW)
    o = np.clip(c + rng.normal(0, 0.3, WINDOW), l, h)
    return o, h, l, c, np.full(WINDOW, 1e6)


# --------------------------------------------------------------------------
# Independent reference implementation (plain Python lists, design §3.1 text)
# --------------------------------------------------------------------------

def _ref_mean(xs):
    return math.fsum(xs) / len(xs)


def _ref_sstd(xs):
    m = _ref_mean(xs)
    return math.sqrt(math.fsum((x - m) * (x - m) for x in xs) / (len(xs) - 1))


def _ref_tr(H, L, C):
    tr = [None]
    for j in range(1, len(C)):
        tr.append(max(H[j] - L[j], abs(H[j] - C[j - 1]), abs(L[j] - C[j - 1])))
    return tr


def _ref_wilder14(x):
    """Seed at index 14 with mean(x[1..14]); ATR-style recurrence after."""
    out = [None] * len(x)
    out[14] = _ref_mean(x[1:15])
    for j in range(15, len(x)):
        out[j] = (13.0 * out[j - 1] + x[j]) / 14.0
    return out


def _ref_slope(H, L, C):
    H, L, C = list(map(float, H)), list(map(float, L)), list(map(float, C))
    ema = [None] * len(C)
    ema[49] = _ref_mean(C[0:50])
    for j in range(50, len(C)):
        ema[j] = (2.0 / 51.0) * C[j] + (49.0 / 51.0) * ema[j - 1]
    atr = _ref_wilder14(_ref_tr(H, L, C))
    return (ema[127] - ema[122]) / (5.0 * atr[127])


def _ref_adx(H, L, C):
    H, L, C = list(map(float, H)), list(map(float, L)), list(map(float, C))
    n = len(C)
    atr = _ref_wilder14(_ref_tr(H, L, C))
    pdm, mdm = [None], [None]
    for j in range(1, n):
        up = H[j] - H[j - 1]
        down = L[j - 1] - L[j]
        pdm.append(up if (up > down and up > 0) else 0.0)
        mdm.append(down if (down > up and down > 0) else 0.0)
    spdm, smdm = _ref_wilder14(pdm), _ref_wilder14(mdm)
    pdi, mdi, dx = [None] * n, [None] * n, [None] * n
    for j in range(14, n):
        pdi[j] = 100.0 * spdm[j] / atr[j]
        mdi[j] = 100.0 * smdm[j] / atr[j]
        s = pdi[j] + mdi[j]
        dx[j] = 0.0 if s == 0 else 100.0 * abs(pdi[j] - mdi[j]) / s
    adx = [None] * n
    adx[27] = _ref_mean(dx[14:28])
    for j in range(28, n):
        adx[j] = (13.0 * adx[j - 1] + dx[j]) / 14.0
    return pdi, mdi, dx, adx


def _ref_rv(C):
    C = list(map(float, C))
    r = [math.log(C[j] / C[j - 1]) for j in range(1, len(C))]
    return _ref_sstd(r[-5:]) / _ref_sstd(r[-20:])


# --------------------------------------------------------------------------

def test_window_constant_is_128_and_field_names_are_canonical():
    assert WINDOW == 128
    assert CALC_VERSION == "ohlcv-v1/calc-1"
    assert FIELD_TREND_SLOPE == "underlying_trend_slope_50_atr14"
    assert FIELD_ADX == "underlying_adx_14"
    assert FIELD_RV_RATIO == "underlying_realized_vol_ratio_5_20"
    assert FIELDS == (FIELD_TREND_SLOPE, FIELD_ADX, FIELD_RV_RATIO)
    assert STATUSES == ("valid", "insufficient_history", "missing_session",
                        "invalid_prices", "no_context", "missing_replay_object")


def test_rising_path_has_positive_slope_and_falling_negative():
    up = compute_market_conditions(*_bars(np.linspace(50, 150, WINDOW)))
    down = compute_market_conditions(*_bars(np.linspace(150, 50, WINDOW)))
    assert up.trend_slope.status == STATUS_VALID and up.trend_slope.value > 0
    assert down.trend_slope.status == STATUS_VALID and down.trend_slope.value < 0


def test_flat_path_with_valid_atr_is_a_real_zero_not_unknown():
    o, h, l, c, v = _bars(np.full(WINDOW, 100.0), spread=1.0)
    atr = atr14_wilder(h, l, c)
    assert atr[127] == 2.0
    res = compute_market_conditions(o, h, l, c, v)
    assert res.trend_slope.status == STATUS_VALID
    assert res.trend_slope.value == 0.0


def test_trend_slope_matches_independent_reference():
    o, h, l, c, v = _bars(_walk(WINDOW, seed=7), spread=0.8)
    expected = _ref_slope(h, l, c)
    res = compute_market_conditions(o, h, l, c, v)
    assert res.trend_slope.status == STATUS_VALID
    assert res.trend_slope.value == expected
    # The EMA helper pins the seed and the recurrence too.
    ema = ema_sma_seeded(c, 50)
    assert np.all(np.isnan(ema[:49]))
    assert ema[49] == math.fsum(map(float, c[:50])) / 50.0
    assert ema[50] == (2.0 / 51.0) * float(c[50]) + (49.0 / 51.0) * float(ema[49])


def test_adx_intermediates_are_pinned_on_a_reference_path():
    o, h, l, c, v = _random_ohlc(seed_c=3, seed_hl=11)
    pdi_r, mdi_r, dx_r, adx_r = _ref_adx(h, l, c)
    pdi, mdi, dx, adx = adx14_wilder(h, l, c)
    for j in (14, 27):
        assert pdi[j] == pdi_r[j]
        assert mdi[j] == mdi_r[j]
        assert dx[j] == dx_r[j]
    assert adx[27] == adx_r[27]
    assert adx[127] == adx_r[127]
    assert np.all(np.isnan(pdi[:14])) and np.all(np.isnan(dx[:14]))
    assert np.all(np.isnan(adx[:27]))
    # a precomputed ATR gives the identical result
    pdi2, mdi2, dx2, adx2 = adx14_wilder(h, l, c, atr=atr14_wilder(h, l, c))
    assert adx2[127] == adx[127] and dx2[27] == dx[27]
    res = compute_market_conditions(o, h, l, c, v)
    assert res.adx.status == STATUS_VALID
    assert res.adx.value == adx_r[127]


def test_adx_with_atr_positive_and_both_dm_zero_is_zero_not_unknown():
    o, h, l, c, v = _bars(np.full(WINDOW, 100.0), spread=1.0)
    pdi, mdi, dx, adx = adx14_wilder(h, l, c)
    assert pdi[14] == 0.0 and mdi[14] == 0.0 and dx[14] == 0.0
    res = compute_market_conditions(o, h, l, c, v)
    assert res.adx.status == STATUS_VALID
    assert res.adx.value == 0.0


def test_atr_zero_makes_slope_and_adx_unknown_but_rv_ratio_still_computed():
    # h == l == c == constant on every bar: TR == 0 so ATR == 0.  (ATR[127] == 0
    # forces every TR to 0, so closes cannot move; the RV ratio is still
    # computed on its own terms and reports its own reason, not the ATR one.)
    flat = np.full(WINDOW, 100.0)
    o, h, l, c = flat.copy(), flat.copy(), flat.copy(), flat.copy()
    res = compute_market_conditions(o, h, l, c, np.full(WINDOW, 1e6))
    assert atr14_wilder(h, l, c)[127] == 0.0
    assert res.trend_slope.status == STATUS_INVALID_PRICES and res.trend_slope.value is None
    assert "atr<=0" in res.trend_slope.reason
    assert res.adx.status == STATUS_INVALID_PRICES and res.adx.value is None
    assert "atr<=0" in res.adx.reason
    # computed, and flat closes give a zero denominator -> unknown (never 1)
    assert res.rv_ratio.status == STATUS_INVALID_PRICES and res.rv_ratio.value is None
    assert "atr" not in res.rv_ratio.reason


def test_zero_atr_inside_adx_warmup_is_unknown_not_zero():
    # Documented edge (module docstring): all OHLC identical over the first 20
    # bars, so ATR is exactly 0 at indices 14..19; prices then move so
    # ATR[127] > 0.  DX is undefined where ATR <= 0, the seeded ADX mean over
    # DX[14..27] is therefore unknown, and so is ADX[127].  The slope, whose
    # denominator is only ATR[127], stays valid.
    c = np.concatenate([np.full(20, 100.0), _walk(WINDOW - 20, seed=5, start=100.0)])
    o, h, l = c.copy(), c.copy(), c.copy()
    h[20:] += 0.5; l[20:] -= 0.5
    res = compute_market_conditions(o, h, l, c, np.full(WINDOW, 1e6))
    assert atr14_wilder(h, l, c)[14] == 0.0
    assert res.trend_slope.status == STATUS_VALID
    assert res.adx.status == STATUS_INVALID_PRICES and res.adx.value is None
    assert res.adx.reason == "adx undefined: atr<=0 at index 14"


def test_rv_ratio_uses_ddof1_and_no_annualization():
    c = _walk(WINDOW, seed=21)
    obs = realized_vol_ratio(c)
    assert obs.status == STATUS_VALID
    assert obs.value == _ref_rv(c)
    # cross-check against numpy's ddof=1 std (different summation, so approx only)
    r = np.log(c[1:] / c[:-1])
    assert obs.value == pytest.approx(np.std(r[-5:], ddof=1) / np.std(r[-20:], ddof=1), abs=1e-12)
    res = compute_market_conditions(*_bars(c))
    assert res.rv_ratio.value == obs.value


def test_rv_ratio_zero_numerator_is_valid_zero_and_zero_denominator_is_unknown():
    c = _walk(WINDOW, seed=9)
    flat20 = c.copy(); flat20[-21:] = flat20[-21]   # last 20 returns all zero
    res = compute_market_conditions(*_bars(flat20))
    assert res.rv_ratio.status == STATUS_INVALID_PRICES
    assert res.rv_ratio.value is None
    flat5 = c.copy(); flat5[-6:] = flat5[-6]        # last 5 returns zero only
    res = compute_market_conditions(*_bars(flat5))
    assert res.rv_ratio.status == STATUS_VALID
    assert res.rv_ratio.value == 0.0


def test_short_window_is_insufficient_history_for_all_three():
    res = compute_market_conditions(*_bars(_walk(100, seed=1)))
    for obs in (res.trend_slope, res.adx, res.rv_ratio):
        assert obs.status == STATUS_INSUFFICIENT_HISTORY
        assert obs.value is None
        assert obs.reason == "insufficient history: 100 of 128 bars"


def test_nonfinite_or_nonpositive_price_is_invalid_prices_never_substituted():
    o, h, l, c, v = _bars(_walk(WINDOW, seed=2))
    c[60] = np.nan
    res = compute_market_conditions(o, h, l, c, v)
    for obs in (res.trend_slope, res.adx, res.rv_ratio):
        assert obs.status == STATUS_INVALID_PRICES and obs.value is None
        assert obs.reason.startswith("index 60 (")
        assert "close non-finite" in obs.reason
    o, h, l, c, v = _bars(_walk(WINDOW, seed=2))
    c[5] = 0.0
    res = compute_market_conditions(o, h, l, c, v)
    for obs in (res.trend_slope, res.adx, res.rv_ratio):
        assert obs.status == STATUS_INVALID_PRICES and obs.value is None
        assert obs.reason.startswith("index 5 (")
        assert "close non-positive" in obs.reason


def test_ohlc_ordering_violation_is_invalid_prices():
    o, h, l, c, v = _bars(_walk(WINDOW, seed=4))
    h[30] = l[30] - 1
    res = compute_market_conditions(o, h, l, c, v)
    for obs in (res.trend_slope, res.adx, res.rv_ratio):
        assert obs.status == STATUS_INVALID_PRICES and obs.value is None
        assert obs.reason.startswith("index 30 (")
        assert "high < low" in obs.reason


def _break_high_below_open(o, h, l, c, v):
    o[40] = h[40] + 0.1

def _break_low_above_open(o, h, l, c, v):
    o[41] = l[41] - 0.1

def _nan_volume(o, h, l, c, v):
    v[42] = np.nan

def _negative_volume(o, h, l, c, v):
    v[43] = -1.0

def _nan_open(o, h, l, c, v):
    o[44] = np.nan

def _nan_high(o, h, l, c, v):
    h[45] = np.nan

def _nan_low(o, h, l, c, v):
    l[46] = np.nan


@pytest.mark.parametrize("mutate, index, label", [
    (_break_high_below_open, 40, "high < max(open, close)"),
    (_break_low_above_open, 41, "low > min(open, close)"),
    (_nan_volume, 42, "volume non-finite"),
    (_negative_volume, 43, "volume negative"),
    (_nan_open, 44, "open non-finite"),
    (_nan_high, 45, "high non-finite"),
    (_nan_low, 46, "low non-finite"),
])
def test_validation_branches_report_status_and_index(mutate, index, label):
    o, h, l, c, v = _bars(_walk(WINDOW, seed=12))
    mutate(o, h, l, c, v)
    res = compute_market_conditions(o, h, l, c, v)
    for obs in (res.trend_slope, res.adx, res.rv_ratio):
        assert obs.status == STATUS_INVALID_PRICES and obs.value is None
        assert f"index {index} (" in obs.reason
        assert label in obs.reason


def test_unequal_array_lengths_raise():
    o, h, l, c, v = _bars(_walk(WINDOW, seed=12))
    with pytest.raises(ValueError):
        compute_market_conditions(o, h, l, c[:-1], v)


def test_invalid_price_reason_reports_lowest_index_with_every_problem_there():
    o, h, l, c, v = _bars(_walk(WINDOW, seed=14))
    c[70] = np.nan            # later index, different field
    v[50] = -1.0              # lower index: two problems at the same bar
    o[50] = np.nan            # NaN does not also trip the ordering checks
    res = compute_market_conditions(o, h, l, c, v)
    assert res.trend_slope.reason == "index 50 (open non-finite, volume negative)"
    assert res.adx.reason == res.rv_ratio.reason == res.trend_slope.reason
    # order of discovery does not matter: swapping which field is bad first
    o, h, l, c, v = _bars(_walk(WINDOW, seed=14))
    o[90] = np.nan
    h[33] = l[33] - 1.0
    res = compute_market_conditions(o, h, l, c, v)
    assert res.trend_slope.reason == \
        "index 33 (high < low, high < max(open, close))"


def test_window_invariance_extra_history_does_not_change_values():
    o, h, l, c, v = _bars(_walk(300, seed=13), spread=0.7)
    base = compute_market_conditions(o[-WINDOW:], h[-WINDOW:], l[-WINDOW:], c[-WINDOW:], v[-WINDOW:])
    assert all(obs.status == STATUS_VALID for obs in base.by_field().values())
    for start in (0, 50, 172):
        # a history of different length ending at the same session, pre-sliced
        oo, hh, ll, cc, vv = (a[start:][-WINDOW:] for a in (o, h, l, c, v))
        assert compute_market_conditions(oo, hh, ll, cc, vv) == base
    with pytest.raises(ValueError):
        compute_market_conditions(o, h, l, c, v)


def test_split_case_is_the_source_contracts_responsibility():
    # An unadjusted 2:1 split inside the window: the calculator does NOT detect
    # it; it computes the arithmetic.  Certification lives in a later task.
    closes = _walk(WINDOW, seed=17, start=200.0)
    closes[80:] = closes[80:] / 2.0
    o, h, l, c, v = _bars(closes)
    res = compute_market_conditions(o, h, l, c, v)
    assert res.trend_slope.status == STATUS_VALID
    assert res.adx.status == STATUS_VALID
    assert res.trend_slope.value == _ref_slope(h, l, c)
    assert res.adx.value == _ref_adx(h, l, c)[3][127]


def _golden_window(k):
    if k == 0:
        return _bars(_walk(WINDOW, seed=101), spread=0.6)
    if k == 1:
        return _random_ohlc(seed_c=202, seed_hl=203)
    # math.sin (not np.sin) so the golden INPUT is platform-independent too
    return _bars([80.0 + 0.15 * t + 3.0 * math.sin(t / 6.0) for t in range(WINDOW)], spread=1.1)


# (slope, adx, rv) as float.hex(), computed once from the reference loops above.
_GOLDEN = [
    ("-0x1.7f8ece5543d79p-4", "0x1.1a2945d330d16p+4", "0x1.e81888fe2fa5fp-1"),
    ("-0x1.3d26f004274a8p-7", "0x1.627e75c6d4b4fp+3", "0x1.0dc591f02b364p+0"),
    ("0x1.bf6c5d9ab0dfap-4", "0x1.fb5456ce2131fp+5", "0x1.c672467b6c70dp-2"),
]


@pytest.mark.parametrize("k", [0, 1, 2])
def test_golden_windows_are_bit_exact(k):
    o, h, l, c, v = _golden_window(k)
    slope_hex, adx_hex, rv_hex = _GOLDEN[k]
    expected = (float.fromhex(slope_hex), float.fromhex(adx_hex), float.fromhex(rv_hex))
    # the reference loops still produce the stored values ...
    assert (_ref_slope(h, l, c), _ref_adx(h, l, c)[3][127], _ref_rv(c)) == expected
    # ... and so does the module, bit for bit
    res = compute_market_conditions(o, h, l, c, v)
    assert (res.trend_slope.value, res.adx.value, res.rv_ratio.value) == expected


def test_as_row_and_by_field_shapes():
    res = compute_market_conditions(*_bars(_walk(WINDOW, seed=8)))
    row = res.as_row()
    assert set(row) == {*FIELDS, *(f"{f}_status" for f in FIELDS), "calc_version"}
    assert row["calc_version"] == CALC_VERSION
    assert row[FIELD_TREND_SLOPE] == res.trend_slope.value
    assert row[FIELD_ADX] == res.adx.value
    assert row[FIELD_RV_RATIO] == res.rv_ratio.value
    assert row[f"{FIELD_ADX}_status"] == STATUS_VALID
    assert res.by_field() == {FIELD_TREND_SLOPE: res.trend_slope, FIELD_ADX: res.adx,
                              FIELD_RV_RATIO: res.rv_ratio}
    short = compute_market_conditions(*_bars(_walk(50, seed=8)))
    srow = short.as_row()
    assert all(srow[f] is None for f in FIELDS)
    assert all(srow[f"{f}_status"] == STATUS_INSUFFICIENT_HISTORY for f in FIELDS)


def test_observation_invariants():
    assert Observation(1.5, STATUS_VALID).value == 1.5
    coerced = Observation(np.float64(2.5), STATUS_VALID)
    assert type(coerced.value) is float and coerced.value == 2.5
    assert Observation(None, STATUS_INVALID_PRICES, "x").value is None
    with pytest.raises(ValueError):
        Observation(None, STATUS_VALID)
    with pytest.raises(ValueError):
        Observation(1.0, STATUS_INSUFFICIENT_HISTORY)
    with pytest.raises(ValueError):
        Observation(None, "bogus")
    with pytest.raises(ValueError):
        Observation(True, STATUS_VALID)
    with pytest.raises(ValueError):
        Observation("1.0", STATUS_VALID)
    with pytest.raises(ValueError):
        Observation(float("nan"), STATUS_VALID)
