"""Group A (analyst grades and price targets) of the daily feature store."""
import json
import shutil
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import ml_features as F
from app.services import ml_panel as P
from tests.test_ml_features import (FIRST, LAST, _assert_cut_identical, _build, _raw, _sessions, cache)  # noqa: F401

GRADES = [("2023-01-10", "Alpha", "upgrade"), ("2023-01-20", "Beta", "downgrade"), ("2023-02-15", "Alpha", "upgrade"),
          ("2023-03-01", "Gamma", "maintain"), ("2023-03-20", "Alpha", "upgrade")]
TARGETS = [("2023-02-01T10:00:00.000Z", "Alpha", 100.0), ("2023-02-10T15:00:00.000Z", "Alpha", 110.0),
           ("2023-02-20T00:00:00.000Z", "Beta", 120.0), ("2023-04-25T12:00:00.000Z", "Gamma", 130.0)]


def _grade_rows(sym, rows=GRADES):
    return [{"symbol": sym, "date": d, "gradingCompany": c, "previousGrade": "Hold", "newGrade": "Buy", "action": a}
            for d, c, a in rows]


def _target_rows(sym, rows=TARGETS):
    return [{"symbol": sym, "publishedDate": d, "analystCompany": c, "priceTarget": t, "adjPriceTarget": t,
             "priceWhenPosted": 90.0, "analystName": "", "newsTitle": "x"} for d, c, t in rows]


def write_fmp_rows(root, kind, sym, rows):
    d = root / "fmp_history"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{kind}__{sym}.json").write_text(json.dumps(rows), encoding="utf-8")


@pytest.fixture(scope="module")
def analyst_cache(cache, tmp_path_factory):
    root = tmp_path_factory.mktemp("analystcache")
    shutil.copytree(cache, root, dirs_exist_ok=True)
    for sym in ("AAA", "BBB"):
        write_fmp_rows(root, "analyst_grades", sym, _grade_rows(sym))
        write_fmp_rows(root, "price_target", sym, _target_rows(sym))
    # decoys that must never be read
    for name in ("price_target_consensus__AAA.json", "grades_historical__AAA.json", "grades_consensus__AAA.json",
                 "price_target_summary__AAA.json"):
        (root / "fmp_history" / name).write_text(json.dumps([{"targetConsensus": 1e9, "strongBuy": 99}]))
    return root


def _abuild(root, symbols=("AAA", "BBB"), **kw):
    return _build(root, ["a"], symbols=symbols, **kw)


def _reference(sessions, close, grades=GRADES, targets=TARGETS):
    """Brute force: every column from the rows, session by session (window = rows dated in [S - N days, S))."""
    g = [(pd.Timestamp(d), c, a) for d, c, a in grades]
    t = [(pd.Timestamp(d[:10]), c, v) for d, c, v in targets]
    g_cov = min(x[0] for x in g)
    t_cov = min(x[0] for x in t)
    rows = []
    means = []
    for S, px in zip(sessions, close):
        def win(rs, n):
            return [r for r in rs if S - pd.Timedelta(days=n) <= r[0] < S]
        row = {}
        gc = S > g_cov
        row["a_net_upgrades_30"] = sum({"upgrade": 1, "downgrade": -1}.get(r[2], 0) for r in win(g, 30)) if gc else np.nan
        row["a_net_upgrades_90"] = sum({"upgrade": 1, "downgrade": -1}.get(r[2], 0) for r in win(g, 90)) if gc else np.nan
        row["a_n_grades_90"] = len({r[1] for r in win(g, 90)}) if gc else np.nan
        tw = win(t, 90)
        tc = S > t_cov
        mean = np.mean([r[2] for r in tw]) if tw else np.nan
        means.append(mean if tc else np.nan)
        row["a_target_gap"] = mean / px - 1 if tw and tc else np.nan
        row["a_n_analysts_90"] = len({r[1] for r in tw}) if tc else np.nan
        rows.append(row)
    out = pd.DataFrame(rows, index=sessions)
    m = pd.Series(means, index=sessions)
    out["a_target_rev_30"] = m / m.shift(30) - 1
    return out[list(F.A_COLUMNS)]


def test_a_columns_and_registry(analyst_cache):
    assert F.GROUPS["a"].prefix == "a_" and not F.GROUPS["a"].needs_sector
    assert list(F.A_COLUMNS) == ["a_net_upgrades_30", "a_net_upgrades_90", "a_n_grades_90", "a_target_gap",
                                 "a_target_rev_30", "a_n_analysts_90"]
    f = _abuild(analyst_cache, first=date(2023, 1, 3), last=date(2023, 9, 29))
    assert F.feature_columns(f) == list(F.A_COLUMNS)


def test_every_column_against_a_brute_force_reference(analyst_cache):
    f = _abuild(analyst_cache, first=date(2023, 1, 3), last=date(2023, 9, 29)).loc["AAA"]
    raw = _raw(analyst_cache, "AAA")["Close"]
    ref = _reference(f.index, raw.reindex(f.index).to_numpy())
    pd.testing.assert_frame_equal(f[list(F.A_COLUMNS)], ref, check_exact=False, rtol=1e-12)
    assert f["a_target_rev_30"].notna().sum() > 20 and f["a_target_gap"].isna().sum() > 20     # both states are exercised


def test_hand_checked_values(analyst_cache):
    f = _abuild(analyst_cache, first=date(2023, 1, 3), last=date(2023, 9, 29)).loc["AAA"]
    close = _raw(analyst_cache, "AAA")["Close"]
    t = pd.Timestamp
    assert f.loc[t("2023-01-10"), list(F.A_COLUMNS)].isna().all()                              # not visible, no coverage yet
    assert f.loc[t("2023-01-11"), "a_net_upgrades_30"] == 1                              # visible the session after its date
    assert f.loc[t("2023-01-20"), "a_net_upgrades_30"] == 1 and f.loc[t("2023-01-23"), "a_net_upgrades_30"] == 0
    day = t("2023-03-21")
    assert f.loc[day, "a_net_upgrades_90"] == 2 and f.loc[day, "a_net_upgrades_30"] == 1 and f.loc[day, "a_n_grades_90"] == 3
    assert np.isnan(f.loc[t("2023-02-01"), "a_n_analysts_90"])                           # target coverage starts 02-02
    assert f.loc[t("2023-02-02"), "a_target_gap"] == pytest.approx(100.0 / close.loc["2023-02-02"] - 1)
    d2 = t("2023-02-21")                                                                  # two Alpha targets count once
    assert f.loc[d2, "a_n_analysts_90"] == 2 and f.loc[d2, "a_target_gap"] == pytest.approx(110.0 / close.loc[d2] - 1)
    late = t("2023-08-01")                                                                # empty windows after coverage
    assert f.loc[late, "a_net_upgrades_30"] == 0 and f.loc[late, "a_net_upgrades_90"] == 0 and f.loc[late, "a_n_grades_90"] == 0
    assert np.isnan(f.loc[late, "a_target_gap"]) and f.loc[late, "a_n_analysts_90"] == 0 and np.isnan(f.loc[late, "a_target_rev_30"])


def test_a_pre_market_publish_time_is_still_visible_only_the_session_after(tmp_path, analyst_cache):
    root = tmp_path / "c"
    shutil.copytree(analyst_cache, root)
    write_fmp_rows(root, "price_target", "AAA", _target_rows("AAA", [("2023-05-10T07:00:00.000Z", "Zeta", 140.0)]))
    f = _abuild(root, symbols=("AAA",), first=date(2023, 5, 1), last=date(2023, 5, 31)).loc["AAA"]
    assert np.isnan(f.loc["2023-05-10", "a_target_gap"]) and np.isnan(f.loc["2023-05-10", "a_n_analysts_90"])
    assert f.loc["2023-05-11", "a_n_analysts_90"] == 1


def test_only_the_two_per_analyst_files_are_opened(analyst_cache, monkeypatch):
    """FMP's consensus / summary files are windowed (about the last quarter): reading one is look-ahead before it."""
    import builtins
    from pathlib import Path
    opened = []
    real_open, real_read = builtins.open, Path.read_text
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (opened.append(str(self)), real_read(self, *a, **k))[1])
    f = _abuild(analyst_cache, first=date(2023, 1, 3), last=date(2023, 3, 31))
    fmp = sorted({Path(p).name for p in opened if "fmp_history" in p})
    assert fmp == ["analyst_grades__AAA.json", "analyst_grades__BBB.json", "price_target__AAA.json", "price_target__BBB.json"]
    assert f.attrs["meta"]["source"]["fmp_history"].keys() == set(fmp)


def test_a_missing_file_and_an_unknown_action_refuse_an_empty_file_is_uncovered(analyst_cache, tmp_path):
    with pytest.raises(P.PanelDataError, match="CCC"):
        _abuild(analyst_cache, symbols=("AAA", "CCC"), first=date(2023, 1, 3), last=date(2023, 3, 31))
    root = tmp_path / "c"
    shutil.copytree(analyst_cache, root)
    write_fmp_rows(root, "analyst_grades", "AAA", _grade_rows("AAA", [("2023-01-10", "Alpha", "reiterate")]))
    with pytest.raises(P.PanelDataError, match=r"AAA.*unknown action 'reiterate'"):
        _abuild(root, symbols=("AAA",), first=date(2023, 1, 3), last=date(2023, 3, 31))
    write_fmp_rows(root, "analyst_grades", "AAA", [])
    write_fmp_rows(root, "price_target", "AAA", [])
    f = _abuild(root, symbols=("AAA",), first=date(2023, 1, 3), last=date(2023, 3, 31))
    assert f[list(F.A_COLUMNS)].isna().all().all()                                        # never covered: NaN, not zero


def _truncate_analyst(trunc, day):
    """Keep only the rows visible at the close of ``day``: dated strictly before it."""
    for p in (trunc / "fmp_history").glob("*.json"):
        if p.name.startswith(("analyst_grades", "price_target__")):
            key = "date" if p.name.startswith("analyst_grades") else "publishedDate"
            rows = json.loads(p.read_text())
            p.write_text(json.dumps([r for r in rows if str(r[key])[:10] < str(day)]))


@pytest.mark.parametrize("day", [date(2023, 11, 15), date(2023, 2, 10), date(2023, 2, 13), date(2023, 3, 21)])
def test_cut_test_a(analyst_cache, tmp_path, day):
    """Includes cuts ON a row's date (not yet visible) and the day after (visible)."""
    full = _assert_cut_identical(analyst_cache, tmp_path, ["a"], day=day, truncate_extra=_truncate_analyst, nan_ok=True, min_valid=0.02)
    assert full["a_n_grades_90"].notna().sum() > 100
