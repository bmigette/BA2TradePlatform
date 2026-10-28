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
        return {"series_id": sid, "fetched_at": pd.Timestamp.now(tz="UTC").isoformat(), "vintage": False, "observations": [{"date": d, "value": str(v)} for d, v, _ in rows]}
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
def _reset_fred(monkeypatch):
    """The synthetic caches are not the pinned real ones: the pins are off here and tested on their own below."""
    monkeypatch.setattr(F, "FRED_PINS", {})
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


# ------------------------------------------------------------------------------------------ review fixes
def _monthly(first, last, f, drop=(), lag_days=13):
    months = [m for m in pd.date_range(first, last, freq="MS") if str(m.date()) not in drop]
    return [(str(m.date()), f(i, m), str((m + pd.DateOffset(months=1, days=lag_days)).date()))
            for i, m in enumerate(months)]


def test_a_missing_month_does_not_create_nan_the_last_valid_transform_is_held(tmp_path, monkeypatch):
    """The real cache lacks the 2025-10 CPI: the year-over-year of a month whose year-ago month is absent is not
    computable, so the last valid transformed value is held until a computable one arrives."""
    rows = _monthly("2022-01-01", "2024-04-01", lambda i, m: 100.0 + 0.5 * i, drop=("2023-03-01",))
    value = {r[0]: r[1] for r in rows}
    write_fred(tmp_path, "CPIAUCSL", rows)
    point_fred_at(monkeypatch, tmp_path)
    # 2024-03 CPI is released 2024-04-14: its YoY needs 2023-03, which is missing
    lvl = F.macro_levels("CPIAUCSL", _sessions_index(date(2024, 4, 10), date(2024, 5, 20)))
    feb = value["2024-02-01"] / value["2023-02-01"] - 1
    apr = value["2024-04-01"] / value["2023-04-01"] - 1
    assert lvl.notna().all()                                            # no NaN anywhere across the gap
    assert lvl.loc["2024-04-12"] == pytest.approx(feb)                  # February's YoY is the latest ...
    assert lvl.loc["2024-04-15"] == pytest.approx(feb)                  # ... still held when March (uncomputable) is public
    assert lvl.loc["2024-05-15"] == pytest.approx(apr)                  # April (released 2024-05-14) is computable again


def test_a_flat_series_gives_nan_z_not_inf(tmp_path, monkeypatch):
    flat = pd.Series(15.0, index=_sessions_index(date(2022, 1, 3), date(2023, 6, 30)))
    z = F._z252(flat)
    assert z.isna().all() and not np.isinf(z).any()
    moving = flat.copy()
    moving.iloc[100:] += np.arange(len(moving) - 100) % 7
    assert F._z252(moving).notna().sum() > 100
    days = pd.bdate_range("2019-06-03", "2024-03-29")                    # and through the whole group
    write_fred(tmp_path, "VIXCLS", [(str(d.date()), 15.0, str(d.date())) for d in days])
    point_fred_at(monkeypatch, tmp_path)
    lvl = F.macro_levels("VIXCLS", _sessions_index(date(2022, 1, 3), date(2023, 1, 3)))
    assert (lvl == 15.0).all() and F._z252(lvl).isna().all()


def test_a_value_held_longer_than_the_maximum_age_refuses(tmp_path, monkeypatch):
    # monthly: the last release was 2023-06-05; more than 100 days later it is stale
    assert F.MAX_HOLD_DAYS_MONTHLY == 100
    write_fred(tmp_path, "UNRATE", [("2023-04-01", 3.4, "2023-05-05"), ("2023-05-01", 3.6, "2023-06-05")])
    point_fred_at(monkeypatch, tmp_path)
    ok = F.macro_levels("UNRATE", _sessions_index(date(2023, 6, 6), date(2023, 9, 13)))      # up to 100 days after the release
    assert ok.notna().all()
    with pytest.raises(P.PanelDataError, match=r"UNRATE.*older than 100 days"):
        F.macro_levels("UNRATE", _sessions_index(date(2023, 9, 14), date(2023, 9, 18)))
    # daily: the VIX stalls after 2023-06-01; more than 10 weekdays later it is stale
    days = pd.bdate_range("2023-05-01", "2023-06-01")
    write_fred(tmp_path, "VIXCLS", [(str(d.date()), 15.0 + i * 0.1, str(d.date())) for i, d in enumerate(days)])
    fs.reset_cache()
    assert F.macro_levels("VIXCLS", _sessions_index(date(2023, 6, 1), date(2023, 6, 15))).notna().all()
    with pytest.raises(P.PanelDataError, match=r"VIXCLS.*older than 10"):
        F.macro_levels("VIXCLS", _sessions_index(date(2023, 6, 16), date(2023, 6, 20)))


def test_a_late_release_is_aged_from_its_release_not_its_observation(tmp_path, monkeypatch):
    """A shutdown delays a release: a September CPI first public on 2023-10-24 is 54 days old on 12-17 although its
    observation date is 107 days back; the age is the time since the value became public."""
    rows = _monthly("2022-01-01", "2023-08-01", lambda i, m: 100.0 + 0.5 * i) + [("2023-09-01", 110.0, "2023-10-24")]
    write_fred(tmp_path, "CPIAUCSL", rows)
    point_fred_at(monkeypatch, tmp_path)
    lvl = F.macro_levels("CPIAUCSL", _sessions_index(date(2023, 10, 24), date(2023, 12, 15)))
    assert lvl.notna().all()


def test_an_unknown_series_is_a_clear_error(tmp_path, monkeypatch):
    point_fred_at(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match=r"FEDFUNDS.*not a MAC series"):
        F.macro_levels("FEDFUNDS", _sessions_index(date(2024, 2, 1), date(2024, 2, 6)))


def test_the_pins_are_the_agreed_ones():
    assert F.FRED_PIN_CUTOFF == "2026-09-30"
    assert dict(F.REAL_FRED_PINS) == {
        "BAA10Y": (10186, "7c375266b7540404"), "CPIAUCSL": (955, "3202c9fa1f77020e"), "DGS10": (16171, "b26e35debc223db1"),
        "PAYEMS": (1052, "6b869176a1b63918"), "T10Y3M": (11189, "aa1f40ccafe7a168"), "UNRATE": (943, "faa617ca44da8c14"),
        "VIXCLS": (9285, "a9c65696dbec8602"), "DGS3MO": (11270, "d8133ea13e3d18a2")}
    assert set(F.REAL_FRED_PINS) == set(F.MAC_SERIES)


def test_the_real_cache_matches_the_pins():
    import os
    real = fs.CACHE_FOLDER
    if not os.path.exists(os.path.join(real, "fred", "VIXCLS.json")):
        pytest.skip("no real FRED cache on this host")
    for sid, (n, digest) in F.REAL_FRED_PINS.items():
        assert F.fred_digest(sid, real) == (n, digest), sid


def test_the_digest_follows_the_first_release_rows(tmp_path, monkeypatch):
    write_fred(tmp_path, "UNRATE", [("2023-04-01", 3.4, "2023-05-05"), ("2023-05-01", 3.6, "2023-06-05"),
                                    ("2026-10-01", 4.0, "2026-10-20")])         # a row first released after the cutoff
    point_fred_at(monkeypatch, tmp_path)
    n, d = F.fred_digest("UNRATE", tmp_path)
    assert n == 2 and len(d) == 16
    write_fred(tmp_path, "UNRATE", [("2023-04-01", 3.4, "2023-05-05"), ("2023-05-01", 3.6, "2023-06-05"),
                                    ("2026-11-01", 4.1, "2026-11-20")])         # a different later row: same pinned digest
    assert F.fred_digest("UNRATE", tmp_path) == (n, d)
    write_fred(tmp_path, "UNRATE", [("2023-04-01", 3.4, "2023-05-05"), ("2023-05-01", 3.7, "2023-06-05")])
    assert F.fred_digest("UNRATE", tmp_path)[1] != d                             # a changed value is a different digest


def test_the_mac_build_refuses_on_a_count_or_digest_mismatch_naming_the_series(fred_at_mac, monkeypatch):
    pins = {sid: F.fred_digest(sid, fred_at_mac) for sid in F.MAC_SERIES}
    monkeypatch.setattr(F, "FRED_PINS", dict(pins))
    _build(fred_at_mac, ["mac"])                                          # matching pins: fine
    monkeypatch.setattr(F, "FRED_PINS", dict(pins, DGS10=(pins["DGS10"][0] + 1, pins["DGS10"][1])))
    with pytest.raises(F.FredPinMismatch, match="DGS10.*count"):
        _build(fred_at_mac, ["mac"])
    monkeypatch.setattr(F, "FRED_PINS", dict(pins, UNRATE=(pins["UNRATE"][0], "0" * 16)))
    with pytest.raises(F.FredPinMismatch, match="UNRATE.*digest"):
        _build(fred_at_mac, ["mac"])


def test_fred_provenance_is_recorded(fred_at_mac):
    f = _build(fred_at_mac, ["mac"])
    fred = f.attrs["meta"]["source"]["fred"]
    assert set(fred) == set(F.MAC_SERIES)
    for sid, rec in fred.items():
        n, d = F.fred_digest(sid, fred_at_mac)
        assert rec["count"] == n and rec["digest"] == d and rec["fetched_at"] and rec["last_vintage"]
    assert fred["UNRATE"]["last_vintage"] == "2024-03-05"


def test_a_known_stall_is_nan_while_any_other_stall_refuses(tmp_path, monkeypatch):
    """FRED published no BAA10Y between 2016-10-07 and 2017-03-22: inside that reviewed window the value is NaN."""
    days = pd.bdate_range("2023-05-01", "2023-06-01")
    rows = [(str(d.date()), 2.0, str(d.date())) for d in days] +            [(str(d.date()), 2.1, "2023-08-01") for d in pd.bdate_range("2023-06-02", "2023-07-31")]
    write_fred(tmp_path, "BAA10Y", rows)
    point_fred_at(monkeypatch, tmp_path)
    sess = _sessions_index(date(2023, 6, 14), date(2023, 7, 31))                 # more than 10 weekdays after 2023-06-01
    with pytest.raises(P.PanelDataError, match="BAA10Y.*older than 10"):
        F.macro_levels("BAA10Y", sess)
    monkeypatch.setattr(F, "KNOWN_STALLS", {"BAA10Y": [("2023-06-01", "2023-08-01")]})
    lvl = F.macro_levels("BAA10Y", sess)
    assert lvl.loc["2023-06-14":"2023-06-15"].eq(2.0).all()                                # within 10 weekdays: held
    assert lvl.loc["2023-06-16":"2023-07-31"].isna().all()                                 # past the age limit: NaN
    ok = F.macro_levels("BAA10Y", _sessions_index(date(2023, 8, 1), date(2023, 8, 4)))      # the backfill release ends it
    assert ok.notna().all()


def test_a_shutdown_sized_release_gap_passes_with_margin(tmp_path, monkeypatch):
    """The 2025 shutdown left a 76-day gap between two payroll / unemployment releases: the hold limit has room."""
    write_fred(tmp_path, "PAYEMS", _monthly("2022-01-01", "2023-07-01", lambda i, m: 1000.0 + 10 * i)[:-1] +
               [("2023-07-01", 1180.0, "2023-08-04"), ("2023-08-01", 1190.0, "2023-10-19")])        # 76 days apart
    point_fred_at(monkeypatch, tmp_path)
    lvl = F.macro_levels("PAYEMS", _sessions_index(date(2023, 8, 4), date(2023, 10, 18)))
    assert lvl.notna().all() and F.MAX_HOLD_DAYS_MONTHLY - 76 >= 20


def test_fred_provenance_carries_the_sha256_of_the_whole_file(fred_at_mac):
    import hashlib
    f = _build(fred_at_mac, ["mac"])
    for sid, rec in f.attrs["meta"]["source"]["fred"].items():
        raw = (fred_at_mac / "fred" / f"{sid}.json").read_bytes()
        assert rec["file"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert rec["file"]["size"] == len(raw) and rec["file"]["mtime_ns"] > 0
