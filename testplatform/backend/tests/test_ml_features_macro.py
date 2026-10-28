"""Group MAC (FRED first-release macro series) of the daily feature store."""
import json
import shutil
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ba2_providers.macro import fred_series as fs

from app.services import ml_features as F
from app.services import ml_panel as P
from tests.test_ml_features import _build, _sessions, cache  # noqa: F401  (cache is a fixture)

SIDS = ("VIXCLS", "DGS10", "DGS3MO", "T10Y3M", "BAA10Y", "CPIAUCSL", "PAYEMS", "UNRATE")


def _doc(sid, rows, first_vintage="2014-01-27"):
    if sid == "DGS3MO":                                   # the same-day series keeps the plain observation format
        return {"series_id": sid, "vintage": False, "observations": [{"date": d, "value": str(v)} for d, v, _ in rows]}
    return {"series_id": sid, "fetched_at": pd.Timestamp.now(tz="UTC").isoformat(), "availability": fs.AVAIL_FIRST_RELEASE,
            "format": fs.CACHE_FORMAT_FIRST_RELEASE, "first_vintage": first_vintage,
            "observations": [{"date": d, "value": str(v), "realtime_start": rt} for d, v, rt in rows]}


def write_fred(root, sid, rows, first_vintage="2014-01-27"):
    (root / "fred").mkdir(parents=True, exist_ok=True)
    (root / "fred" / f"{sid}.json").write_text(json.dumps(_doc(sid, rows, first_vintage)), encoding="utf-8")


def point_fred_at(monkeypatch, root):
    monkeypatch.setattr(fs, "CACHE_FOLDER", str(root))
    fs.reset_cache()


@pytest.fixture(autouse=True)
def _reset_fred():
    fs.reset_cache()
    yield
    fs.reset_cache()


def _synthetic_fred(root, seed=11):
    """All eight series over 2019-2024 with a realistic publication lag (daily: next day, VIX / T10Y3M: same day,
    monthly: mid next month)."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2019-06-03", "2024-03-29")
    for sid, lag in (("VIXCLS", 0), ("T10Y3M", 0), ("DGS10", 1), ("BAA10Y", 1), ("DGS3MO", 0)):
        level = {"VIXCLS": 18.0, "T10Y3M": 1.0, "DGS10": 3.0, "BAA10Y": 2.0, "DGS3MO": 2.0}[sid]
        vals = level + np.cumsum(rng.normal(0, 0.15, len(days)))
        rows = [(str(d.date()), round(float(v), 4), str((d + pd.offsets.BDay(lag)).date())) for d, v in zip(days, vals)]
        write_fred(root, sid, rows)
    months = pd.date_range("2018-01-01", "2024-02-01", freq="MS")
    for sid, base, growth, day in (("CPIAUCSL", 250.0, 0.003, 14), ("PAYEMS", 150000.0, 0.0012, 5), ("UNRATE", 4.0, 0.0, 5)):
        v = base * (1 + growth) ** np.arange(len(months)) + (rng.normal(0, 0.1, len(months)) if sid == "UNRATE" else 0)
        rows = [(str(m.date()), round(float(x), 3), str((m + pd.DateOffset(months=1, days=day - 1)).date()))
                for m, x in zip(months, v)]
        write_fred(root, sid, rows)


@pytest.fixture(scope="module")
def mac_cache(cache, tmp_path_factory):
    root = tmp_path_factory.mktemp("maccache")
    shutil.copytree(cache, root, dirs_exist_ok=True)
    _synthetic_fred(root)
    return root


@pytest.fixture
def fred_at_mac(mac_cache, monkeypatch):
    point_fred_at(monkeypatch, mac_cache)
    return mac_cache


def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the FRED reader went to the network")
    monkeypatch.setattr(fs, "_fill_cache_on_the_live_path", boom)
    monkeypatch.setattr(fs, "refresh_series", boom)


# ------------------------------------------------------------------------------------------ visibility
def _sessions_index(first, last):
    return _sessions(first, last)


def test_a_value_is_invisible_before_its_realtime_start_and_visible_from_that_session_on(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    write_fred(tmp_path, "UNRATE", [("2023-11-01", 3.8, "2023-12-08"), ("2023-12-01", 3.7, "2024-01-05"),
                                    ("2024-01-01", 3.9, "2024-02-02")])
    point_fred_at(monkeypatch, tmp_path)
    sess = _sessions_index(date(2024, 1, 31), date(2024, 2, 7))
    lvl = F.macro_levels("UNRATE", sess)
    assert lvl.loc["2024-01-31"] == 3.7 and lvl.loc["2024-02-01"] == 3.7           # released 2024-02-02 08:30: not yet
    assert lvl.loc["2024-02-02"] == 3.9 and lvl.loc["2024-02-05"] == 3.9           # visible at that day's close, on
    early = F.macro_levels("UNRATE", _sessions_index(date(2024, 1, 3), date(2024, 1, 5)))
    assert early.loc["2024-01-04"] == 3.8 and early.loc["2024-01-05"] == 3.7       # the January 5 release lands on the 5th


def test_nothing_visible_is_nan(tmp_path, monkeypatch):
    """Before the only observation's first release there is no value, not a stale or a default one."""
    write_fred(tmp_path, "UNRATE", [("2024-01-01", 3.9, "2024-02-02")])
    point_fred_at(monkeypatch, tmp_path)
    lvl = F.macro_levels("UNRATE", _sessions_index(date(2024, 2, 1), date(2024, 2, 6)))
    assert np.isnan(lvl.loc["2024-02-01"]) and lvl.loc["2024-02-02"] == 3.9


def test_sessions_before_the_first_vintage_are_nan(tmp_path, monkeypatch):
    write_fred(tmp_path, "VIXCLS", [("2023-06-05", 15.0, "2023-06-05"), ("2023-06-06", 16.0, "2023-06-06")],
               first_vintage="2023-06-01")
    point_fred_at(monkeypatch, tmp_path)
    lvl = F.macro_levels("VIXCLS", _sessions_index(date(2023, 5, 25), date(2023, 6, 8)))
    assert lvl.loc[:"2023-06-01"].isna().all()                          # on / before the first vintage: nothing provable
    assert np.isnan(lvl.loc["2023-06-02"]) and lvl.loc["2023-06-05"] == 15.0 and lvl.loc["2023-06-06"] == 16.0


def test_dgs3mo_same_day_series_is_lagged_one_session(tmp_path, monkeypatch):
    """DGS3MO has no publication date in its cache (observation-date format): the observation of day S is only
    usable from the next session."""
    write_fred(tmp_path, "DGS3MO", [("2024-03-04", 5.30, ""), ("2024-03-05", 5.31, ""), ("2024-03-06", 5.33, "")])
    point_fred_at(monkeypatch, tmp_path)
    lvl = F.macro_levels("DGS3MO", _sessions_index(date(2024, 3, 4), date(2024, 3, 7)))
    assert np.isnan(lvl.loc["2024-03-04"])
    assert lvl.loc["2024-03-05"] == 5.30 and lvl.loc["2024-03-06"] == 5.31 and lvl.loc["2024-03-07"] == 5.33


def test_cpi_is_year_over_year_and_payems_three_month_change(tmp_path, monkeypatch):
    months = pd.date_range("2022-01-01", "2023-06-01", freq="MS")
    cpi = [(str(m.date()), 100.0 + 0.5 * i, str((m + pd.DateOffset(months=1, days=13)).date())) for i, m in enumerate(months)]
    pay = [(str(m.date()), 1000.0 + 10 * i, str((m + pd.DateOffset(months=1, days=4)).date())) for i, m in enumerate(months)]
    write_fred(tmp_path, "CPIAUCSL", cpi)
    write_fred(tmp_path, "PAYEMS", pay)
    point_fred_at(monkeypatch, tmp_path)
    sess = _sessions_index(date(2023, 6, 1), date(2023, 7, 20))
    cpi_l, pay_l = F.macro_levels("CPIAUCSL", sess), F.macro_levels("PAYEMS", sess)
    # May 2023 CPI (index 16) is released 2023-06-14; the year-ago value is May 2022 (index 4)
    v_may23, v_may22 = 100.0 + 0.5 * 16, 100.0 + 0.5 * 4
    assert cpi_l.loc["2023-06-13"] == pytest.approx((100.0 + 0.5 * 15) / (100.0 + 0.5 * 3) - 1)     # April still the latest
    assert cpi_l.loc["2023-06-14"] == pytest.approx(v_may23 / v_may22 - 1)
    # May payrolls (released 2023-06-05): level change over three months = May / February - 1
    assert pay_l.loc["2023-06-05"] == pytest.approx((1000.0 + 10 * 16) / (1000.0 + 10 * 13) - 1)
    assert pay_l.loc["2023-06-02"] == pytest.approx((1000.0 + 10 * 15) / (1000.0 + 10 * 12) - 1)


def test_missing_cache_file_and_an_old_cache_refuse_loudly(tmp_path, monkeypatch):
    point_fred_at(monkeypatch, tmp_path)
    with pytest.raises(fs.MacroAvailabilityUnknown):
        F.macro_levels("UNRATE", _sessions_index(date(2024, 2, 1), date(2024, 2, 6)))
    write_fred(tmp_path, "UNRATE", [("2024-01-01", 3.9, "2024-02-02")])
    doc = json.loads((tmp_path / "fred" / "UNRATE.json").read_text())
    doc["fetched_at"] = "2024-02-03T12:00:00+00:00"                      # fetched before the sessions asked for
    (tmp_path / "fred" / "UNRATE.json").write_text(json.dumps(doc))
    fs.reset_cache()
    with pytest.raises(fs.MacroAvailabilityUnknown):
        F.macro_levels("UNRATE", _sessions_index(date(2024, 2, 1), date(2024, 2, 9)))


# ------------------------------------------------------------------------------------------ the group
def _expected_columns():
    cols = []
    for sid in SIDS:
        base = {"CPIAUCSL": "yoy", "PAYEMS": "3m"}.get(sid, "lvl")
        cols += [f"mac_{sid.lower()}_{base}", f"mac_{sid.lower()}_chg20", f"mac_{sid.lower()}_z252"]
    return cols


def test_mac_columns_and_registry(fred_at_mac):
    assert list(F.MAC_COLUMNS) == _expected_columns() and len(F.MAC_COLUMNS) == 24
    assert F.GROUPS["mac"].prefix == "mac_" and not F.GROUPS["mac"].needs_sector
    f = _build(fred_at_mac, ["mac"])
    assert F.feature_columns(f) == list(F.MAC_COLUMNS)
    assert f[list(F.MAC_COLUMNS)].notna().all().all()


def test_mac_is_the_same_for_every_symbol_on_a_session(fred_at_mac):
    f = _build(fred_at_mac, ["mac"], symbols=("AAA", "BBB", "CCC"))
    a, b = f.loc["AAA", list(F.MAC_COLUMNS)], f.loc["BBB", list(F.MAC_COLUMNS)]
    np.testing.assert_array_equal(a.to_numpy(), b.to_numpy())


def test_mac_change_and_z_follow_the_per_session_levels(fred_at_mac, monkeypatch):
    _no_network(monkeypatch)
    f = _build(fred_at_mac, ["mac"]).loc["AAA"]
    # the same levels the group itself reads, session by session, on the whole calendar
    sess = _sessions_index(date(2021, 1, 4), LAST_DAY)
    lvl = F.macro_levels("VIXCLS", sess)
    exp_chg = lvl - lvl.shift(20)
    exp_z = (lvl - lvl.rolling(252).mean()) / lvl.rolling(252).std()
    np.testing.assert_allclose(f["mac_vixcls_lvl"], lvl.reindex(f.index), rtol=1e-12)
    np.testing.assert_allclose(f["mac_vixcls_chg20"], exp_chg.reindex(f.index), rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(f["mac_vixcls_z252"], exp_z.reindex(f.index), rtol=1e-9, atol=1e-12)


LAST_DAY = date(2024, 3, 28)


def test_mac_reads_first_release_not_the_observation_date(fred_at_mac):
    """DGS10 obs d is published d+1 in the synthetic cache: the feature at d is the observation of d-1."""
    f = _build(fred_at_mac, ["mac"]).loc["AAA"]
    raw = pd.DataFrame(json.loads((fred_at_mac / "fred" / "DGS10.json").read_text())["observations"])
    raw["date"] = pd.to_datetime(raw["date"])
    raw = raw.set_index("date")["value"].astype(float)
    day = pd.Timestamp("2023-09-13")
    assert f.loc[day, "mac_dgs10_lvl"] == pytest.approx(raw.loc["2023-09-12"])
    assert f.loc[day, "mac_dgs10_lvl"] != pytest.approx(raw.loc["2023-09-13"])
    # VIX (same-day release) is visible the same day
    vix = pd.DataFrame(json.loads((fred_at_mac / "fred" / "VIXCLS.json").read_text())["observations"])
    vix = vix.assign(date=pd.to_datetime(vix["date"])).set_index("date")["value"].astype(float)
    assert f.loc[day, "mac_vixcls_lvl"] == pytest.approx(vix.loc["2023-09-13"])


def _truncate_fred(root, day):
    """Cut every FRED file to what had been published by the end of ``day`` (physically removing later rows)."""
    for p in (root / "fred").glob("*.json"):
        doc = json.loads(p.read_text())
        key = (lambda o: o.get("realtime_start") or o["date"])
        doc["observations"] = [o for o in doc["observations"] if key(o) <= str(day)]
        p.write_text(json.dumps(doc))


def test_cut_test_mac(fred_at_mac, tmp_path, monkeypatch):
    from tests import test_ml_features as T
    full = _build(fred_at_mac, ["mac"])
    day = T.CUT_DAY
    trunc = T._truncate(fred_at_mac, tmp_path, day)
    _truncate_fred(trunc, day)
    point_fred_at(monkeypatch, trunc)
    cut = _build(trunc, ["mac"], last=day)
    point_fred_at(monkeypatch, fred_at_mac)
    common = full[full.index.get_level_values("session") <= pd.Timestamp(day)]
    assert len(common) == len(cut) > 400
    pd.testing.assert_frame_equal(common.drop(columns="label_bar_filled"), cut.drop(columns="label_bar_filled"),
                                  check_exact=True)
    assert cut[list(F.MAC_COLUMNS)].notna().all().all()


def test_mac_refuses_a_fred_cache_that_is_not_under_cache_root(mac_cache, tmp_path, monkeypatch):
    point_fred_at(monkeypatch, tmp_path)                               # FRED reads elsewhere than the build's cache
    with pytest.raises(P.PanelDataError, match="FRED"):
        _build(mac_cache, ["mac"])


def test_mac_never_uses_the_network(fred_at_mac, monkeypatch):
    _no_network(monkeypatch)
    _build(fred_at_mac, ["mac"])                                        # would raise AssertionError if it did
