"""ACCURACY of the market-condition calculators against independent references.

``test_market_conditions_calculators.py`` pins the formulas of
``ba2_common.core.market_conditions`` against loops written from the same
design text (§3.1).  This file checks them against independent, widely used
code on REAL daily bars:

* **TA-Lib 0.8.0** (C library): ``EMA(50)``, ``ATR(14)``, ``PLUS_DI``/``MINUS_DI``/
  ``DX``/``ADX(14)``, ``PLUS_DM``/``MINUS_DM``.  Exactly the 128-bar window is passed,
  so the seed indices match ours.
* **pandas** ``rolling(n).std(ddof=1)`` for the realized-vol ratio.
* **DeterministicScorer** ``technical.adx_wilder`` (pandas ``ewm(alpha=1/14,
  adjust=False)``, seeded from the first bar) for convergence of a different
  ADX convention.

Real bars: the FMP daily parquet cache
(``~/Documents/ba2/common/cache/FMPOHLCVProvider/{AAPL,MSFT,NVDA,SPY}_1d.parquet``);
128-row windows ending at every 41st row over the last ~1500 rows.  Windows
the module itself rejects are skipped and counted.  Without the cache (CI)
the real-data tests skip; ``test_synthetic_windows_match_talib`` still runs
the TA-Lib comparison on 200 seeded random walks.

TA-Lib conventions (checked empirically in ``test_talib_conventions_are_what_we_assume``):

* ``EMA(c, 50)[49]`` is the SMA of closes 0..49 -> the same seed as ours.
* ``ATR(14)[14]`` is ``mean(TR[1..14])`` -> the same seed as ours.
* ``ADX`` is first defined at 27 and seeded with ``mean(DX[14..27])`` -> same as ours.
* ``PLUS_DI``/``MINUS_DI``/``DX`` (and therefore ``ADX``) do NOT use Wilder's DM/TR
  seed.  Their smoothed +DM/-DM/TR start as the SUM of 13 values (index 1..13,
  visible as ``PLUS_DM[13]``) and index 14 is already a recurrence step,
  ``S14 = S13*13/14 + x14``.  Ours (Wilder 1978, and TA-Lib's own ATR) uses the
  sum of 14 values at index 14.  In mean form the seed difference is exactly
  ``-sum(x[1..13])/196`` and it decays by ``(13/14)**(j-14)``.  On real bars this
  leaves ~1e-4 relative on DI and a few 1e-2 ADX points at index 127 -- a
  convention difference, not floating point, so a ``rel=1e-8`` assertion would be
  wrong.  The DI/ADX comparisons instead prove, per window:
  (a) a reference loop with TA-Lib's seed reproduces TA-Lib at every defined index
      (rtol 1e-9; measured <= 5.4e-14, relative to max(|value|, 1)),
  (b) the SAME loop with only the seed line switched to Wilder's reproduces ours
      (rtol 1e-12; measured 0), so ours and TA-Lib differ ONLY by that seed, and
  (c) the direct deviation at 127 lies below a closed-form seed-decay bound.

Tolerances: EMA/ATR/slope rel 1e-9 (measured <= ~2e-15 on EMA/ATR and ~4e-13 on
the slope on real bars, 1.7e-12 on synthetic ones, which amplifies the EMA difference by EMA/|EMA[127]-EMA[122]|);
RV ratio abs 1e-12 (pandas sums differently from ``math.fsum``; measured ~5e-14).

Run locally (TA-Lib installed outside the venv with ``pip install --target DIR ta-lib``)::

    cd packages/common
    BA2_TALIB_REF=DIR python -m pytest tests/test_market_conditions_accuracy.py -q -s

Without ``BA2_TALIB_REF`` the TA-Lib tests skip; the pandas RV, DeterministicScorer
and golden (ours-only part) tests still run when the parquet cache exists.
"""
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ba2_common.core.market_conditions import (
    WINDOW, STATUS_VALID, compute_market_conditions, ema_sma_seeded, atr14_wilder,
    adx14_wilder, true_range,
)

# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------
_TALIB_DIR = os.environ.get("BA2_TALIB_REF")
talib = None
if _TALIB_DIR:
    sys.path.insert(0, _TALIB_DIR)
    try:
        import talib  # noqa: F811
    except Exception:  # noqa: BLE001 -- any import failure means "reference unavailable"
        talib = None
requires_talib = pytest.mark.skipif(talib is None, reason="TA-Lib not available (set BA2_TALIB_REF)")

_EXPERTS_DIR = Path(__file__).resolve().parents[2] / "experts"
try:
    if str(_EXPERTS_DIR) not in sys.path:
        sys.path.insert(0, str(_EXPERTS_DIR))
    from ba2_experts.DeterministicScorer.technical import (
        adx_wilder as ds_adx_wilder, _wilder_smooth as ds_wilder_smooth,
    )
except Exception:  # noqa: BLE001
    ds_adx_wilder = ds_wilder_smooth = None
requires_ds = pytest.mark.skipif(ds_adx_wilder is None, reason="DeterministicScorer technical.py not importable")

_CACHE = Path.home() / "Documents" / "ba2" / "common" / "cache" / "FMPOHLCVProvider"
_SYMBOLS = ("AAPL", "MSFT", "NVDA", "SPY")
_STEP = 41
_SPAN = 1500

R = 13.0 / 14.0
LAST = WINDOW - 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load(symbol):
    path = _CACHE / f"{symbol}_1d.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path).sort_values("Date").reset_index(drop=True)
    assert not df["Date"].duplicated().any(), f"{symbol}: duplicate dates in cache"
    return df


_WINDOWS_CACHE = {}


def _real_windows():
    """[(label, o, h, l, c, v)] over all symbols; skips the test if no cache file exists."""
    if "w" not in _WINDOWS_CACHE:
        out, rejected, found = [], 0, 0
        for sym in _SYMBOLS:
            df = _load(sym)
            if df is None:
                continue
            found += 1
            n = len(df)
            for end in range(n - 1, max(WINDOW - 2, n - 1 - _SPAN), -_STEP):
                w = df.iloc[end - LAST:end + 1]
                arrs = [w[k].to_numpy(dtype=np.float64) for k in ("Open", "High", "Low", "Close", "Volume")]
                res = compute_market_conditions(*arrs)
                if not (res.trend_slope.status == res.adx.status == res.rv_ratio.status == STATUS_VALID):
                    rejected += 1
                    continue
                out.append((f"{sym}@{w['Date'].iloc[-1].date()}", *arrs))
        _WINDOWS_CACHE["w"] = (out, rejected, found)
    out, rejected, found = _WINDOWS_CACHE["w"]
    if not found:
        pytest.skip(f"FMP parquet cache absent at {_CACHE}")
    print(f"\n[real windows] symbols={found} compared={len(out)} rejected_by_validation={rejected}")
    assert out, "no valid real windows"
    return out


def _synthetic_windows(count=200, seed=20260915):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(count):
        c = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, WINDOW)))
        h = c * (1.0 + rng.uniform(0.001, 0.03, WINDOW))
        l = c * (1.0 - rng.uniform(0.001, 0.03, WINDOW))
        o = np.clip(c * (1.0 + rng.normal(0.0, 0.01, WINDOW)), l, h)
        v = rng.uniform(1e5, 1e7, WINDOW)
        out.append((f"synthetic#{i}", o, h, l, c, v))
    return out


def _rel(a, b):
    return abs(a - b) / abs(b) if b != 0 else abs(a - b)


def _dm(h, l):
    """+DM/-DM per Wilder as plain lists (NaN at index 0)."""
    p, m = [math.nan], [math.nan]
    for j in range(1, len(h)):
        up, dn = h[j] - h[j - 1], l[j - 1] - l[j]
        p.append(up if (up > dn and up > 0) else 0.0)
        m.append(dn if (dn > up and dn > 0) else 0.0)
    return p, m


def _reference_di_adx(h, l, c, seed):
    """Independent DI/DX/ADX loop (mean form).

    ``seed='wilder'``: smoothed TR/+DM/-DM at 14 = mean of x[1..14].
    ``seed='talib'`` : sum of x[1..13], then one recurrence step at 14 (TA-Lib).
    Everything else (TR, DM, DI, DX, ADX seed at 27, recurrences) is shared.
    """
    n = len(c)
    h, l, c = h.tolist(), l.tolist(), c.tolist()
    tr = [math.nan] + [max(h[j] - l[j], abs(h[j] - c[j - 1]), abs(l[j] - c[j - 1])) for j in range(1, n)]
    pdm, mdm = _dm(h, l)

    def smooth(x):
        out = [math.nan] * n
        if seed == "wilder":
            s = math.fsum(x[1:15]) / 14
        elif seed == "talib":
            s = (math.fsum(x[1:14]) * 13 / 14 + x[14]) / 14
        else:
            raise ValueError(seed)
        out[14] = s
        for j in range(15, n):
            s = (13 * s + x[j]) / 14
            out[j] = s
        return out

    a, p, m = smooth(tr), smooth(pdm), smooth(mdm)
    pdi, mdi, dx, adx = ([math.nan] * n for _ in range(4))
    for j in range(14, n):
        pdi[j] = 100 * p[j] / a[j]
        mdi[j] = 100 * m[j] / a[j]
        dx[j] = 100 * abs(pdi[j] - mdi[j]) / (pdi[j] + mdi[j])
    s = math.fsum(dx[14:28]) / 14
    adx[27] = s
    for j in range(28, n):
        s = (13 * s + dx[j]) / 14
        adx[j] = s
    return tuple(np.array(v) for v in (pdi, mdi, dx, adx))


def _adx_bound(d_p14, d_m14, s_other, adx27_bound):
    """Upper bound on |ADX_ours[127] - ADX_other[127]| for two conventions whose smoothed
    +DM/-DM (mean form) differ by ``d_p14``/``d_m14`` at index 14 and then follow the
    same Wilder recurrence, so the difference at j is exactly (13/14)^(j-14) times that.

    DX = 100|sP-sM|/(sP+sM) (ATR cancels), and with u=sP-sM, s=sP+sM:
    |DX_other - DX_ours| <= 100(|du|+|ds|)/s_other <= 200(|dP|+|dM|)/s_other (capped at 100).
    After 27 both ADX follow the same recurrence, so for either convention
    ADX[127] = r^100*ADX[27] + sum_{j=28..127} r^(127-j)/14 * DX[j], giving
    |dADX127| <= r^100*|dADX27| + sum r^(127-j)/14 * |dDX_j|.
    ``s_other[j]`` is sP+sM of the other convention; ``adx27_bound(dx_bounds)`` bounds |dADX27|.
    """
    dx_b = {j: min(100.0, 200.0 * R ** (j - 14) * (abs(d_p14) + abs(d_m14)) / float(s_other[j]))
            for j in range(14, WINDOW)}
    return (R ** (LAST - 27) * adx27_bound(dx_b)
            + math.fsum(R ** (LAST - j) / 14 * dx_b[j] for j in range(28, WINDOW)))


def _talib_di_adx_bounds(h, l, c):
    """Closed-form seed-decay bounds for ours vs TA-Lib at 127: (DI+ bound, DI- bound, ADX bound)."""
    tr = true_range(h, l, c).tolist()
    pdm, mdm = _dm(h.tolist(), l.tolist())
    # mean-form seed differences, TA-Lib minus ours, at index 14: -sum(x[1..13])/196
    d_tr, d_p, d_m = (-math.fsum(x[1:14]) / 196 for x in (tr, pdm, mdm))
    atr_ours = atr14_wilder(h, l, c)
    pdi, mdi, _, _ = adx14_wilder(h, l, c)
    decay = R ** (LAST - 14)
    str_talib = atr_ours[LAST] + d_tr * decay  # TA-Lib's internal smoothed TR at 127 (mean form)

    def di_bound(di_ours, d_dm):
        # DI_t - DI_o = 100*(dDM - DI_o/100*dTR) / sTR_t   (exact)
        return 100 * (abs(d_dm) * decay + di_ours / 100 * abs(d_tr) * decay) / str_talib

    s_talib = (talib.PLUS_DM(h, l, 14) + talib.MINUS_DM(h, l, 14)) / 14  # sum form -> mean form
    # identical ADX seed (mean of DX[14..27]) -> |dADX27| <= mean of the DX bounds there
    adx_b = _adx_bound(d_p, d_m, s_talib, lambda dx_b: math.fsum(dx_b[j] for j in range(14, 28)) / 14)
    return di_bound(pdi[LAST], d_p), di_bound(mdi[LAST], d_m), adx_b


def _compare_talib(windows):
    """Every TA-Lib comparison over ``windows``; returns the max deviations."""
    mx = {k: 0.0 for k in ("ema_rel", "atr_rel", "slope_rel", "pdi_rel", "mdi_rel", "adx_abs",
                           "adx_rel", "ref_talib_seed_vs_talib", "ref_wilder_seed_vs_ours",
                           "max_dev_over_bound")}
    for label, o, h, l, c, v in windows:
        ema, atr = ema_sma_seeded(c, 50), atr14_wilder(h, l, c)
        t_ema, t_atr = talib.EMA(c, 50), talib.ATR(h, l, c, 14)
        assert ema[LAST] == pytest.approx(t_ema[-1], rel=1e-9, abs=0), label
        assert atr[LAST] == pytest.approx(t_atr[-1], rel=1e-9, abs=0), label
        mx["ema_rel"] = max(mx["ema_rel"], _rel(ema[LAST], t_ema[-1]))
        mx["atr_rel"] = max(mx["atr_rel"], _rel(atr[LAST], t_atr[-1]))

        slope = compute_market_conditions(o, h, l, c, v).trend_slope.value
        t_slope = (t_ema[-1] - t_ema[-6]) / (5 * t_atr[-1])
        assert slope == pytest.approx(t_slope, rel=1e-9, abs=0), label
        mx["slope_rel"] = max(mx["slope_rel"], _rel(slope, t_slope))

        pdi, mdi, dx, adx = adx14_wilder(h, l, c)
        t = (talib.PLUS_DI(h, l, c, 14), talib.MINUS_DI(h, l, c, 14), talib.DX(h, l, c, 14),
             talib.ADX(h, l, c, 14))
        # (a) the TA-Lib-seeded reference reproduces TA-Lib at every defined index
        ref_t = _reference_di_adx(h, l, c, "talib")
        for k, name in enumerate(("PLUS_DI", "MINUS_DI", "DX", "ADX")):
            first = 27 if name == "ADX" else 14
            np.testing.assert_allclose(ref_t[k][first:], t[k][first:], rtol=1e-9, atol=1e-9,
                                       err_msg=f"{label}: TA-Lib-seed reference != talib.{name}")
            dev = np.abs(ref_t[k][first:] - t[k][first:]) / np.maximum(np.abs(t[k][first:]), 1.0)
            mx["ref_talib_seed_vs_talib"] = max(mx["ref_talib_seed_vs_talib"], float(dev.max()))
        # (b) the same loop with only Wilder's seed reproduces ours
        ref_w = _reference_di_adx(h, l, c, "wilder")
        for k, ours in enumerate((pdi, mdi, dx, adx)):
            first = 27 if k == 3 else 14
            np.testing.assert_allclose(ref_w[k][first:], ours[first:], rtol=1e-12, atol=1e-12,
                                       err_msg=f"{label}: Wilder-seed reference != ours")
            dev = np.abs(ref_w[k][first:] - ours[first:]) / np.maximum(np.abs(ours[first:]), 1.0)
            mx["ref_wilder_seed_vs_ours"] = max(mx["ref_wilder_seed_vs_ours"], float(dev.max()))
        # (c) the direct deviation at 127 lies within the seed-decay bound
        b_p, b_m, b_adx = _talib_di_adx_bounds(h, l, c)
        d_p, d_m, d_adx = abs(pdi[LAST] - t[0][-1]), abs(mdi[LAST] - t[1][-1]), abs(adx[LAST] - t[3][-1])
        assert d_p <= b_p * (1 + 1e-9) + 1e-12, f"{label}: |dDI+|={d_p:.3e} > bound {b_p:.3e}"
        assert d_m <= b_m * (1 + 1e-9) + 1e-12, f"{label}: |dDI-|={d_m:.3e} > bound {b_m:.3e}"
        assert d_adx <= b_adx * (1 + 1e-9) + 1e-12, f"{label}: |dADX|={d_adx:.3e} > bound {b_adx:.3e}"
        mx["pdi_rel"] = max(mx["pdi_rel"], _rel(pdi[LAST], t[0][-1]))
        mx["mdi_rel"] = max(mx["mdi_rel"], _rel(mdi[LAST], t[1][-1]))
        mx["adx_abs"] = max(mx["adx_abs"], d_adx)
        mx["adx_rel"] = max(mx["adx_rel"], _rel(adx[LAST], t[3][-1]))
        for d, b in ((d_p, b_p), (d_m, b_m), (d_adx, b_adx)):
            if b > 0:
                mx["max_dev_over_bound"] = max(mx["max_dev_over_bound"], d / b)
    return mx


def _fmt(mx):
    return ", ".join(f"{k}={v:.3e}" for k, v in mx.items())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@requires_talib
def test_talib_conventions_are_what_we_assume():
    _, o, h, l, c, v = _synthetic_windows(1, seed=7)[0]
    tr = true_range(h, l, c).tolist()
    pdm, _ = _dm(h.tolist(), l.tolist())

    ema = talib.EMA(c, 50)
    assert np.flatnonzero(~np.isnan(ema))[0] == 49
    assert ema[49] == pytest.approx(math.fsum(c[:50].tolist()) / 50, rel=1e-14)

    atr = talib.ATR(h, l, c, 14)
    assert np.flatnonzero(~np.isnan(atr))[0] == 14
    assert atr[14] == pytest.approx(math.fsum(tr[1:15]) / 14, rel=1e-14)

    adx, dx, pdi = talib.ADX(h, l, c, 14), talib.DX(h, l, c, 14), talib.PLUS_DI(h, l, c, 14)
    assert np.flatnonzero(~np.isnan(adx))[0] == 27
    assert np.flatnonzero(~np.isnan(dx))[0] == 14
    assert np.flatnonzero(~np.isnan(pdi))[0] == 14
    assert adx[27] == pytest.approx(math.fsum(dx[14:28].tolist()) / 14, rel=1e-13)  # same ADX seed as ours

    # DIFFERS from the assumption: TA-Lib's smoothed DM starts as the sum of 13 values ...
    p_dm = talib.PLUS_DM(h, l, 14)
    assert np.flatnonzero(~np.isnan(p_dm))[0] == 13
    assert p_dm[13] == pytest.approx(math.fsum(pdm[1:14]), rel=1e-14)
    assert p_dm[14] == pytest.approx(p_dm[13] * 13 / 14 + pdm[14], rel=1e-14)
    # ... which is not Wilder's 14-value sum, so DI[14] differs from ours
    assert p_dm[14] != pytest.approx(math.fsum(pdm[1:15]), rel=1e-6)
    assert pdi[14] != pytest.approx(adx14_wilder(h, l, c)[0][14], rel=1e-6)
    # DX depends only on the smoothed DMs (ATR cancels): TA-Lib's DX from its PLUS_DM/MINUS_DM
    m_dm = talib.MINUS_DM(h, l, 14)
    dx_from_dm = 100 * np.abs(p_dm - m_dm) / (p_dm + m_dm)
    np.testing.assert_allclose(dx[14:], dx_from_dm[14:], rtol=1e-12, atol=1e-10)


@requires_talib
def test_ema50_atr14_match_talib_on_real_windows():
    windows = _real_windows()
    mx = {"ema_rel": 0.0, "ema_abs": 0.0, "atr_rel": 0.0, "atr_abs": 0.0}
    for label, o, h, l, c, v in windows:
        e, te = ema_sma_seeded(c, 50)[LAST], talib.EMA(c, 50)[-1]
        a, ta = atr14_wilder(h, l, c)[LAST], talib.ATR(h, l, c, 14)[-1]
        assert e == pytest.approx(te, rel=1e-9, abs=0), f"{label}: EMA {e!r} vs {te!r}"
        assert a == pytest.approx(ta, rel=1e-9, abs=0), f"{label}: ATR {a!r} vs {ta!r}"
        mx["ema_rel"] = max(mx["ema_rel"], _rel(e, te))
        mx["ema_abs"] = max(mx["ema_abs"], abs(e - te))
        mx["atr_rel"] = max(mx["atr_rel"], _rel(a, ta))
        mx["atr_abs"] = max(mx["atr_abs"], abs(a - ta))
    print(f"[EMA50/ATR14 vs TA-Lib] windows={len(windows)} {_fmt(mx)}")


@requires_talib
def test_adx14_and_di_match_talib_on_real_windows():
    windows = _real_windows()
    mx = _compare_talib(windows)
    print(f"[DI/ADX vs TA-Lib] windows={len(windows)} {_fmt(mx)}")


@requires_talib
def test_trend_slope_matches_talib_derived_value_on_real_windows():
    windows = _real_windows()
    worst = 0.0
    for label, o, h, l, c, v in windows:
        te, ta = talib.EMA(c, 50), talib.ATR(h, l, c, 14)
        expected = (te[-1] - te[-6]) / (5 * ta[-1])
        got = compute_market_conditions(o, h, l, c, v).trend_slope.value
        assert got == pytest.approx(expected, rel=1e-9, abs=0), f"{label}: slope {got!r} vs {expected!r}"
        worst = max(worst, _rel(got, expected))
    print(f"[trend slope vs TA-Lib] windows={len(windows)} max_rel={worst:.3e}")


def test_rv_ratio_matches_pandas_rolling_std_on_real_windows():
    windows = _real_windows()
    worst = 0.0
    for label, o, h, l, c, v in windows:
        lr = pd.Series(np.log(c)).diff()
        expected = lr.rolling(5).std(ddof=1).iloc[-1] / lr.rolling(20).std(ddof=1).iloc[-1]
        got = compute_market_conditions(o, h, l, c, v).rv_ratio.value
        assert got == pytest.approx(expected, rel=0, abs=1e-12), f"{label}: rv {got!r} vs {expected!r}"
        worst = max(worst, abs(got - expected))
    print(f"[RV ratio vs pandas] windows={len(windows)} max_abs={worst:.3e}")


@requires_ds
def test_adx_agrees_with_deterministic_scorer_after_seed_decay():
    """DS smooths with pandas ewm(alpha=1/14, adjust=False) from bar 0; ours seeds DM/TR at 14
    and ADX at 27.  From 14 on both follow the same recurrence, so the smoothed-DM difference
    decays by (13/14)^(j-14), and the ADX-seed difference (at most 100 points, both ADX lie in
    [0, 100]) by (13/14)^100 = 6.05e-4 -> 0.0605 points on its own.  ``_adx_bound`` adds the
    decayed DX term per window."""
    windows = _real_windows()
    seed_term = 100 * R ** (LAST - 27)
    worst_abs = worst_ratio = worst_bound = 0.0
    for label, o, h, l, c, v in windows:
        hs, ls, cs = pd.Series(h), pd.Series(l), pd.Series(c)
        ds_val = ds_adx_wilder(hs, ls, cs)
        ours = adx14_wilder(h, l, c)
        # DS's smoothed DMs via DS's own smoother on DS's DM definition; the reconstruction
        # is checked to reproduce ds_adx_wilder's result.
        up, down = hs.diff(), -ls.diff()
        p_dm = up.where((up > down) & (up > 0), 0.0)
        m_dm = down.where((down > up) & (down > 0), 0.0)
        tr = pd.concat([hs - ls, (hs - cs.shift(1)).abs(), (ls - cs.shift(1)).abs()], axis=1).max(axis=1)
        s_p, s_m, s_tr = ds_wilder_smooth(p_dm, 14), ds_wilder_smooth(m_dm, 14), ds_wilder_smooth(tr, 14)
        p_di, m_di = 100 * s_p / s_tr, 100 * s_m / s_tr
        recon = float(ds_wilder_smooth(100 * (p_di - m_di).abs() / (p_di + m_di), 14).iloc[-1])
        assert recon == pytest.approx(ds_val, rel=1e-12), f"{label}: DS reconstruction drifted"

        atr = atr14_wilder(h, l, c)
        d_p14 = float(s_p.iloc[14]) - ours[0][14] * atr[14] / 100
        d_m14 = float(s_m.iloc[14]) - ours[1][14] * atr[14] / 100
        bound = _adx_bound(d_p14, d_m14, (s_p + s_m).to_numpy(), lambda _: 100.0)
        d = abs(ours[3][LAST] - ds_val)
        assert d <= bound * (1 + 1e-9) + 1e-12, f"{label}: |ADX_ours-ADX_DS|={d:.4e} > bound {bound:.4e}"
        worst_abs = max(worst_abs, d)
        worst_ratio = max(worst_ratio, d / bound)
        worst_bound = max(worst_bound, bound)
    print(f"[ADX vs DeterministicScorer] windows={len(windows)} max_abs_dev={worst_abs:.4e} ADX points; "
          f"seed term 100*(13/14)^100={seed_term:.4e}; max per-window bound={worst_bound:.4e}; "
          f"max dev/bound={worst_ratio:.3f}")


# AAPL, the 128 rows ending 2025-06-30 (first row 2024-12-23).
_GOLDEN_END = "2025-06-30"
_GOLDEN_OURS = {
    "trend_slope": float.fromhex("-0x1.4b537402d6d91p-6"),
    "adx": float.fromhex("0x1.e20e2bd8aecb3p+3"),
    "rv_ratio": float.fromhex("0x1.ca10851d5bb49p-1"),
}
_GOLDEN_TALIB = {
    "EMA50": float.fromhex("0x1.97e0337ed7836p+7"),
    "ATR14": float.fromhex("0x1.1e0a8d7a1b80dp+2"),
    "ADX14": float.fromhex("0x1.e2021289e6acfp+3"),  # TA-Lib seed convention -- differs from ours by design
}


def test_golden_real_window_pinned():
    df = _load("AAPL")
    if df is None:
        pytest.skip(f"FMP parquet cache absent at {_CACHE}")
    end = int(np.flatnonzero(df["Date"].dt.strftime("%Y-%m-%d").to_numpy() == _GOLDEN_END)[0])
    w = df.iloc[end - LAST:end + 1]
    assert len(w) == WINDOW and str(w["Date"].iloc[0].date()) == "2024-12-23"
    o, h, l, c, v = (w[k].to_numpy(dtype=np.float64) for k in ("Open", "High", "Low", "Close", "Volume"))
    res = compute_market_conditions(o, h, l, c, v)
    assert res.trend_slope.value == _GOLDEN_OURS["trend_slope"]
    assert res.adx.value == _GOLDEN_OURS["adx"]
    assert res.rv_ratio.value == _GOLDEN_OURS["rv_ratio"]
    if talib is None:
        return
    assert talib.EMA(c, 50)[-1] == pytest.approx(_GOLDEN_TALIB["EMA50"], rel=1e-9)
    assert talib.ATR(h, l, c, 14)[-1] == pytest.approx(_GOLDEN_TALIB["ATR14"], rel=1e-9)
    assert talib.ADX(h, l, c, 14)[-1] == pytest.approx(_GOLDEN_TALIB["ADX14"], rel=1e-9)


@requires_talib
def test_synthetic_windows_match_talib():
    windows = [w for w in _synthetic_windows(200)
               if compute_market_conditions(*w[1:]).adx.status == STATUS_VALID]
    assert len(windows) == 200
    mx = _compare_talib(windows)
    print(f"\n[synthetic vs TA-Lib] windows={len(windows)} {_fmt(mx)}")
