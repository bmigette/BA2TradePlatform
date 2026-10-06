"""Selection parity: the provider vs (a) the backtest's own ``_compute_atm_iv`` and (b) the saved
study results, replayed offline from raw Alpaca bars (fixture: tests/fixtures/atm_iv_study)."""
import os
import re
import sys
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ba2_trade_platform.modules.dataproviders.options import atm_iv_history as H

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "atm_iv_study")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TP_BACKEND = os.path.join(REPO, "testplatform", "backend")


@pytest.fixture(scope="module")
def study():
    pts = pd.read_csv(os.path.join(FIX, "points.csv"), parse_dates=["date"])
    bars = pd.read_csv(os.path.join(FIX, "bars.csv.gz"), parse_dates=["date", "expiry"])
    lst = pd.read_csv(os.path.join(FIX, "listing.csv.gz"), parse_dates=["expiry"])
    return pts, bars, lst


def _cands(bars_for_day):
    return [H.Candidate(r.occ, float(r.strike), r.expiry.date(), float(r.close), int(r.volume))
            for r in bars_for_day.itertuples()]


def test_fixture_is_at_least_200_points(study):
    pts, bars, _ = study
    assert len(pts) >= 200 and pts.symbol.nunique() == 8


def test_provider_selection_reproduces_the_study_derived_values_exactly(study):
    """Same candidate bars -> same contract and the same IV as derived_C_close.csv (study mode:
    every fetched bar of the session is a candidate)."""
    pts, bars, _ = study
    g = {k: v for k, v in bars.groupby(["symbol", "date"])}
    n = 0
    for p in pts.itertuples():
        sel = H.select_atm(p.date.date(), p.spot, p.rate, _cands(g[(p.symbol, p.date)]))
        assert sel is not None
        assert sel.candidate.occ == p.study_occ, (p.symbol, p.date)
        assert abs(sel.iv - p.study_iv) <= 1e-12, (p.symbol, p.date)
        n += 1
    assert n >= 200


def test_provider_selection_equals_the_backtests_compute_atm_iv(study):
    """Feed the SAME bars to ``ParquetOptionsProvider._compute_atm_iv`` (stubbed reader whose
    per-bar greeks come from the backtest's own ``option_greeks``) and to the provider."""
    gf = os.path.join(_TP_BACKEND, "app", "services", "backtest", "parquet_options_provider.py")
    if not os.path.exists(gf):
        pytest.skip(f"testplatform parquet_options_provider.py genuinely absent ({gf})")
    if _TP_BACKEND not in sys.path:
        sys.path.insert(0, _TP_BACKEND)
    from app.services.backtest import option_greeks as og
    from app.services.backtest import parquet_options_provider as pop
    from ba2_common.core.types import OptionRight

    pts, bars, _ = study
    g = {k: v for k, v in bars.groupby(["symbol", "date"])}
    checked = 0
    for p in pts.itertuples():
        day = g[(p.symbol, p.date)].reset_index(drop=True)
        asof = p.date.date()
        n = len(day)
        raw = SimpleNamespace(c_expiry_ord_l=[e.toordinal() for e in day.expiry.dt.date],
                              c_strike_f=[float(k) for k in day.strike])

        class U:
            n_rows = n
            c_is_call = np.ones(n, dtype=bool)
            c_expiry_ord = np.array(raw.c_expiry_ord_l)

            def latest_row_on_or_before(self, ci, ordinal):
                return ci            # one bar per contract, dated the as-of session

            def delta_iv_of_row(self, i, ci, spot_source):
                r = day.iloc[ci]
                T = (r.expiry.date() - asof).days / 365.0
                out = og.compute_iv_and_greeks(float(r.close), float(p.spot), float(r.strike), T,
                                               float(p.rate), OptionRight.CALL)
                return out["delta"], out["iv"]
        u = U()
        u.raw = raw
        stub = SimpleNamespace(_u=lambda underlying, _u=u: _u, spot_source=None)
        bt_iv = pop.ParquetOptionsProvider._compute_atm_iv(stub, p.symbol, asof)
        sel = H.select_atm(asof, p.spot, p.rate, _cands(day))
        assert bt_iv is not None and sel is not None
        assert abs(bt_iv - sel.iv) <= 1e-12, (p.symbol, asof)
        checked += 1
    assert checked >= 200


def test_membership_rule_matches_the_study_and_literal_6_strikes_is_measurably_worse(study):
    """The provider admits a contract to a session's candidate set only if it is one of the
    ``N_STRIKES`` listed strikes nearest spot for its expiry (deterministic, independent of how
    sessions are chunked). On the fixture that reproduces the study's contract on ~every point; a
    literal 6 does not (the |delta|-0.5 strike of a high-vol name sits several strikes OTM)."""
    pts, bars, lst = study
    g = {k: v for k, v in bars.groupby(["symbol", "date"])}
    strikes = {}
    for (sym, exp), grp in lst.groupby(["symbol", "expiry"]):
        strikes[(sym, exp.date())] = np.sort(grp.strike.unique())

    def replay(n):
        same, d_bt = 0, []
        for p in pts.itertuples():
            d = p.date.date()
            allowed = set()
            for (sym, exp), ks in strikes.items():
                if sym == p.symbol and H.DTE_MIN <= (exp - d).days <= H.DTE_MAX:
                    allowed.update((exp, float(k)) for k in H.nearest_strikes(ks, p.spot, n))
            day = g[(p.symbol, p.date)]
            day = day[[(r.expiry.date(), float(r.strike)) in allowed for r in day.itertuples()]]
            sel = H.select_atm(d, p.spot, p.rate, _cands(day))
            assert sel is not None
            same += sel.candidate.occ == p.study_occ
            d_bt.append(abs(sel.iv - p.bt_iv) * 100)
        return same / len(pts), float(np.quantile(d_bt, 0.95))

    frac, p95 = replay(H.N_STRIKES)
    frac6, p95_6 = replay(6)
    assert frac >= 0.99, frac                    # same contract as the study
    assert p95 < 2.0                              # vs the ThetaData backtest, vol points
    assert p95_6 > p95 and frac6 < frac           # the literal 6 is worse


def test_study_values_agree_with_thetadata_backtest_values(study):
    """Ground truth in the fixture: derived (Alpaca bars) vs the backtest's ThetaData IV."""
    pts, _, _ = study
    d = (pts.study_iv - pts.bt_iv).abs() * 100
    assert d.median() < 0.05
    assert d.quantile(0.95) < 2.0     # (whole study: 0.9; this fixture over-samples high-vol names)


def test_no_testplatform_or_package_file_imports_the_live_module():
    """GA neutrality: nothing under packages/ or testplatform/ may reach this live-only code."""
    pat = re.compile(r"atm_iv_history|options\.bs_inversion|dataproviders\.options")
    offenders = []
    for top in ("packages", "testplatform"):
        for root, dirs, files in os.walk(os.path.join(REPO, top)):
            dirs[:] = [x for x in dirs if x not in ("node_modules", ".git", "__pycache__", ".venv")]
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(root, f)
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        if pat.search(fh.read()):
                            offenders.append(os.path.relpath(path, REPO))
    assert offenders == [], offenders
