"""Groups AG (analyst grades) and AT (analyst price targets) of the daily feature store."""
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
FLOOR = pd.Timestamp("2021-04-12") + pd.Timedelta(days=90)     # price targets: the vendor's start + 90 days


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
    for name in ("price_target_consensus__AAA.json", "grades_historical__AAA.json", "grades_consensus__AAA.json",
                 "price_target_summary__AAA.json"):                      # decoys that must never be read
        (root / "fmp_history" / name).write_text(json.dumps([{"targetConsensus": 1e9, "strongBuy": 99}]))
    return root


def _abuild(root, groups=("ag", "at"), symbols=("AAA", "BBB"), **kw):
    kw.setdefault("first", date(2023, 1, 3))
    kw.setdefault("last", date(2023, 9, 29))
    return _build(root, list(groups), symbols=symbols, **kw)


def _reference(sessions, close, grades=GRADES, targets=TARGETS):
    """Brute force: every column from the rows, session by session (window = rows dated in [T - N days, T))."""
    g = [(pd.Timestamp(d), c, a) for d, c, a in grades]
    t = [(pd.Timestamp(d[:10]), c, v) for d, c, v in targets]
    t_cov = FLOOR

    def win(rs, T, n):
        return [r for r in rs if T - pd.Timedelta(days=n) <= r[0] < T]

    def mean_at(T):
        w = win(t, T, 90)
        return np.mean([r[2] for r in w]) if w else np.nan
    rows = []
    for S, px in zip(sessions, close):
        gc, tc = True, S >= t_cov                              # grades: uncapped, covered from the panel start
        score = {"upgrade": 1, "downgrade": -1}
        tw = win(t, S, 90)
        prev = mean_at(S - pd.Timedelta(days=30))
        cur = mean_at(S)
        rows.append({
            "ag_net_upgrades_30": sum(score.get(r[2], 0) for r in win(g, S, 30)) if gc else np.nan,
            "ag_net_upgrades_90": sum(score.get(r[2], 0) for r in win(g, S, 90)) if gc else np.nan,
            "ag_n_graders_90": len({r[1] for r in win(g, S, 90)}) if gc else np.nan,
            "ag_covered": 1.0 if gc else 0.0,
            "at_target_gap": cur / px - 1 if tc and tw else np.nan,
            "at_target_rev_30": cur / prev - 1 if tc and (S - pd.Timedelta(days=30)) >= t_cov and tw and prev == prev else np.nan,
            "at_n_analysts_90": len({r[1] for r in tw}) if tc else np.nan,
            "at_covered": 1.0 if tc else 0.0})
    return pd.DataFrame(rows, index=sessions).astype("float64")


def test_columns_and_registry(analyst_cache):
    assert "a" not in F.GROUPS
    assert F.GROUPS["ag"].prefix == "ag_" and F.GROUPS["at"].prefix == "at_"
    assert list(F.AG_COLUMNS) == ["ag_net_upgrades_30", "ag_net_upgrades_90", "ag_n_graders_90", "ag_covered"]
    assert list(F.AT_COLUMNS) == ["at_target_gap", "at_target_rev_30", "at_n_analysts_90", "at_covered"]
    assert F.GROUPS["ag"].coverage_columns == ("ag_net_upgrades_30", "ag_net_upgrades_90", "ag_n_graders_90")
    assert F.GROUPS["at"].coverage_columns == ("at_n_analysts_90",)                # availability, not the gap (NaN in a quiet period)
    f = _abuild(analyst_cache)
    assert F.feature_columns(f, ["ag"]) == list(F.AG_COLUMNS) and F.feature_columns(f, ["at"]) == list(F.AT_COLUMNS)
    only_g = _abuild(analyst_cache, groups=("ag",))
    assert F.feature_columns(only_g) == list(F.AG_COLUMNS)


def test_every_column_against_a_brute_force_reference(analyst_cache):
    f = _abuild(analyst_cache).loc["AAA"]
    close = _raw(analyst_cache, "AAA")["Close"].reindex(f.index).to_numpy()
    ref = _reference(f.index, close)
    cols = list(F.AG_COLUMNS) + list(F.AT_COLUMNS)
    pd.testing.assert_frame_equal(f[cols], ref[cols], check_exact=False, rtol=1e-12)
    assert f["at_target_rev_30"].notna().sum() > 5 and f["at_target_gap"].isna().sum() > 20     # both states exercised


def test_hand_checked_values(analyst_cache):
    f = _abuild(analyst_cache).loc["AAA"]
    close = _raw(analyst_cache, "AAA")["Close"]
    t = pd.Timestamp
    # grades: an uncapped file is covered from the panel start; before its first row the windows are real zeros
    assert f.loc[t("2023-01-03"), "ag_covered"] == 1 and f.loc[t("2023-01-03"), "ag_net_upgrades_90"] == 0
    assert f.loc[t("2023-01-03"), "ag_n_graders_90"] == 0
    d = t("2023-04-10")
    assert f.loc[d, "ag_covered"] == 1 and f.loc[d, "ag_net_upgrades_90"] == 2 and f.loc[d, "ag_net_upgrades_30"] == 1
    assert f.loc[d, "ag_n_graders_90"] == 3                                                  # Alpha counted once
    # targets: covered from the vendor floor (2021-07-11) on; a quiet period before the first row is real (no gap, 0 analysts)
    assert f.loc[t("2023-01-03"), "at_covered"] == 1 and np.isnan(f.loc[t("2023-01-03"), "at_target_gap"])
    assert f.loc[t("2023-01-03"), "at_n_analysts_90"] == 0
    d = t("2023-05-02")
    assert f.loc[d, "at_covered"] == 1 and f.loc[d, "at_target_gap"] == pytest.approx(115.0 / close.loc[d] - 1)
    assert f.loc[d, "at_n_analysts_90"] == 3                                                # Alpha twice counts once
    # the 30-CALENDAR-day revision: window mean now (only the 2023-04-25 target) over the mean 30 days earlier
    d = t("2023-06-01")
    assert f.loc[d, "at_target_rev_30"] == pytest.approx(130.0 / 115.0 - 1)
    late = t("2023-08-01")                                                                  # empty windows after coverage
    assert f.loc[late, "ag_net_upgrades_30"] == 0 and f.loc[late, "ag_n_graders_90"] == 0
    assert np.isnan(f.loc[late, "at_target_gap"]) and f.loc[late, "at_n_analysts_90"] == 0 and np.isnan(f.loc[late, "at_target_rev_30"])


def test_the_revision_is_30_calendar_days_not_30_sessions(analyst_cache):
    f = _abuild(analyst_cache).loc["AAA"]
    # on 2023-06-01 the "30 days ago" instant is 2023-05-02, 21 sessions earlier; a 30-session lag would reach 2023-04-18
    sessions_back = f.index.get_loc(pd.Timestamp("2023-06-01")) - f.index.get_loc(pd.Timestamp("2023-05-02"))
    assert sessions_back < 30
    assert f.loc["2023-06-01", "at_target_rev_30"] == pytest.approx(130.0 / 115.0 - 1)


def test_a_pre_market_publish_time_is_still_visible_only_the_session_after(tmp_path, analyst_cache):
    root = tmp_path / "c"
    shutil.copytree(analyst_cache, root)
    write_fmp_rows(root, "price_target", "AAA", _target_rows("AAA", [("2023-01-03T10:00:00.000Z", "Old", 90.0),
                                                                    ("2023-05-10T07:00:00.000Z", "Zeta", 140.0)]))
    f = _abuild(root, symbols=("AAA",), first=date(2023, 5, 1), last=date(2023, 5, 31)).loc["AAA"]
    assert np.isnan(f.loc["2023-05-10", "at_target_gap"]) and f.loc["2023-05-10", "at_n_analysts_90"] == 0
    assert f.loc["2023-05-11", "at_n_analysts_90"] == 1 and not np.isnan(f.loc["2023-05-11", "at_target_gap"])


def test_only_the_per_analyst_files_are_opened_each_group_its_own(analyst_cache, monkeypatch):
    """FMP's consensus / summary files are windowed (about the last quarter): reading one is look-ahead before it."""
    import builtins
    from pathlib import Path
    opened = []
    real_open, real_read = builtins.open, Path.read_text
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (opened.append(str(self)), real_read(self, *a, **k))[1])

    def names():
        out = sorted({Path(p).name for p in opened if "fmp_history" in p})
        opened.clear()
        return out
    f = _abuild(analyst_cache, first=date(2023, 1, 3), last=date(2023, 3, 31))
    both = names()
    assert both == ["analyst_grades__AAA.json", "analyst_grades__BBB.json", "price_target__AAA.json", "price_target__BBB.json"]
    assert set(f.attrs["meta"]["source"]["fmp_history"]) == set(both)
    _abuild(analyst_cache, groups=("ag",), first=date(2023, 1, 3), last=date(2023, 3, 31))
    assert names() == ["analyst_grades__AAA.json", "analyst_grades__BBB.json"]
    _abuild(analyst_cache, groups=("at",), first=date(2023, 1, 3), last=date(2023, 3, 31))
    assert names() == ["price_target__AAA.json", "price_target__BBB.json"]


def test_price_target_coverage_starts_90_days_after_the_vendor_floor_not_the_files_first_row(analyst_cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(analyst_cache, root)
    write_fmp_rows(root, "price_target", "AAA", _target_rows("AAA", [("2021-04-12T10:00:00.000Z", "Alpha", 100.0),
                                                                    ("2023-02-01T10:00:00.000Z", "Beta", 100.0)]))
    f = _abuild(root, groups=("at",), symbols=("AAA",), first=date(2021, 6, 1), last=date(2021, 9, 30)).loc["AAA"]
    t = pd.Timestamp
    assert F.PRICE_TARGET_VENDOR_START == date(2021, 4, 12)
    assert f.loc[t("2021-07-09"), "at_covered"] == 0 and f.loc[t("2021-07-09"), ["at_target_gap", "at_n_analysts_90"]].isna().all()
    assert f.loc[t("2021-07-12"), "at_covered"] == 1                                       # 2021-04-12 + 90 days = 07-11 (Sunday)
    # an uncapped file whose first row is years later is covered from the same floor: its quiet months are real
    g = _abuild(root, groups=("at",), symbols=("AAA",), first=date(2021, 9, 1), last=date(2021, 9, 30)).loc["AAA"]
    assert (g["at_covered"] == 1).all() and (g["at_n_analysts_90"] >= 0).all() and g["at_target_gap"].isna().sum() > 0


def test_a_target_gap_beyond_the_limit_refuses_instead_of_clipping(analyst_cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(analyst_cache, root)
    close = _raw(analyst_cache, "AAA")["Close"]
    write_fmp_rows(root, "price_target", "AAA", _target_rows("AAA", [("2023-02-01T10:00:00.000Z", "Alpha", float(close.iloc[100]) * 20)]))
    with pytest.raises(P.PanelDataError, match=r"AAA.*at_target_gap \+\d.*on 2023-02-0\d.*adjustment mismatch"):
        _abuild(root, groups=("at",), symbols=("AAA",), first=date(2023, 1, 3), last=date(2023, 3, 31))
    assert F.MAX_TARGET_GAP == 5.0


def test_neutral_actions_count_zero_and_an_unknown_one_refuses(analyst_cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(analyst_cache, root)
    rows = [("2022-10-03", "Alpha", "initialize"), ("2022-10-04", "Beta", "reiterate"), ("2022-10-05", "Gamma", "initiate"),
            ("2022-12-01", "Delta", "upgrade")]
    write_fmp_rows(root, "analyst_grades", "AAA", _grade_rows("AAA", rows))
    f = _abuild(root, groups=("ag",), symbols=("AAA",), first=date(2023, 1, 3), last=date(2023, 1, 31)).loc["AAA"]
    assert f.loc["2023-01-03", "ag_covered"] == 1                                            # 2022-10-03 + 90 days
    assert f.loc["2023-01-03", "ag_net_upgrades_90"] == 1 and f.loc["2023-01-03", "ag_n_graders_90"] == 2   # window starts 2022-10-05
    assert set(F.NEUTRAL_ACTIONS) >= {"initialize", "initiate", "reiterate", "maintain", ""}
    write_fmp_rows(root, "analyst_grades", "AAA", _grade_rows("AAA", [("2023-01-10", "Alpha", "frobnicate")]))
    with pytest.raises(P.PanelDataError, match=r"AAA.*unknown action 'frobnicate'"):
        _abuild(root, groups=("ag",), symbols=("AAA",), first=date(2023, 1, 3), last=date(2023, 3, 31))


def test_a_missing_file_refuses_and_an_empty_file_is_never_covered(analyst_cache, tmp_path):
    with pytest.raises(P.PanelDataError, match="CCC"):
        _abuild(analyst_cache, symbols=("AAA", "CCC"), first=date(2023, 1, 3), last=date(2023, 3, 31))
    root = tmp_path / "c"
    shutil.copytree(analyst_cache, root)
    write_fmp_rows(root, "analyst_grades", "AAA", [])
    write_fmp_rows(root, "price_target", "AAA", [])
    f = _abuild(root, symbols=("AAA",), first=date(2023, 1, 3), last=date(2023, 3, 31)).loc["AAA"]
    cols = [c for c in list(F.AG_COLUMNS) + list(F.AT_COLUMNS) if not c.endswith("covered")]
    assert f[cols].isna().all().all() and (f[["ag_covered", "at_covered"]] == 0).all().all()   # never covered: NaN, not zero


def _truncate_analyst(trunc, day):
    """Keep only the rows visible at the close of ``day``: dated strictly before it."""
    for p in (trunc / "fmp_history").glob("*.json"):
        if p.name.startswith(("analyst_grades", "price_target__")):
            key = "date" if p.name.startswith("analyst_grades") else "publishedDate"
            rows = json.loads(p.read_text())
            p.write_text(json.dumps([r for r in rows if str(r[key])[:10] < str(day)]))


@pytest.mark.parametrize("day", [date(2023, 11, 15), date(2023, 4, 10), date(2023, 5, 2), date(2023, 6, 1)])
def test_cut_test_ag_at(analyst_cache, tmp_path, day):
    """Includes cuts ON the day coverage starts and the day a window first moves."""
    full = _assert_cut_identical(analyst_cache, tmp_path, ["ag", "at"], day=day, truncate_extra=_truncate_analyst,
                                 nan_ok=True, min_valid=0.02)
    assert full["ag_covered"].sum() > 100
