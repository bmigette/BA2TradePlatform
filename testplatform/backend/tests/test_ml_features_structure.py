"""Group S (market-condition structure features) of the daily feature store."""
import shutil
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ba2_common.core import market_conditions as mc
from ba2_common.core.market_condition_store import MarketConditionStore, month_of, status_code

from app.services import ml_features as F
from app.services import ml_panel as P
from tests.test_ml_features import (CUT_DAY, FIRST, LAST, _assert_cut_identical, _build, _sessions, cache)  # noqa: F401

ALPACA = "alpaca-daily-split-verified-v1"


def _value(sym_i, ordinal, j):
    """A deterministic value keyed by symbol, session ordinal and field: a one-session shift is visible."""
    return float(np.sin(ordinal * 0.013 + j + 3 * sym_i) + 0.001 * ordinal)


def write_mc_store(root, symbols, cut=None, source_profile=ALPACA, calc_version=None, invalid=None, first=date(2021, 1, 4),
                   last=date(2024, 3, 28), universe_tag="", skip=()):
    """Publish one manifest per profile into ``<root>/market_conditions`` over ``symbols``; returns {profile: digest}.
    ``invalid`` = {(profile, symbol, session): field_index} rows stored WITH a value but a non-valid status."""
    store = MarketConditionStore(root)
    sessions = _sessions(first, last)
    if cut is not None:
        sessions = sessions[sessions <= pd.Timestamp(cut)]
    out = {}
    for profile in ("ohlcv-v1", "ta-structure-v1"):
        spec = mc.PROFILES[profile]
        n_f = len(spec.fields)
        objects = []
        for si, sym in enumerate(symbols):
            by_month = {}
            for t in sessions:
                if (sym, t.date()) in set(skip):                       # a hole: no row for that symbol and session
                    continue
                ordinal = (t - pd.Timestamp("2020-01-01")).days
                vals = [_value(si, ordinal, j) for j in range(n_f)]
                status = [mc.STATUS_VALID] * n_f
                bad = (invalid or {}).get((profile, sym, t.date()))
                if bad is not None:
                    status[bad] = mc.STATUS_INSUFFICIENT_HISTORY
                row = {"session": t.date(), "values": vals, "status": status, "reasons": [""] * n_f,
                       "window_digest": "sha256:" + "0" * 64, "raw_shard_ref": "", "raw_row_lo": 0, "raw_row_hi": 0}
                by_month.setdefault(month_of(t.date()), []).append(row)
            for rows in by_month.values():
                entry, _ = store.write_feature_object(spec, sym, rows)
                objects.append(entry)
        manifest = store.make_manifest(
            spec, source_profile=source_profile, timing_policy="prior_session_v1", objects=objects, raw_objects=[],
            coverage={s: {"rows": len(sessions), "first_session": str(sessions[0].date()),
                          "last_session": str(sessions[-1].date()), "exceptions": []} for s in symbols},
            universe=list(symbols) + ([universe_tag] if universe_tag else []), sessions=[t.date() for t in sessions],
            window_start=sessions[0].date(), window_end=sessions[-1].date(), gap_fill_policy="previous")
        if calc_version is not None:
            manifest["calc_version"] = calc_version
        out[profile] = store.write_manifest(manifest)
    return out


@pytest.fixture(scope="module")
def mc_cache(cache, tmp_path_factory):
    root = tmp_path_factory.mktemp("mccache")
    shutil.copytree(cache, root, dirs_exist_ok=True)
    digests = write_mc_store(root, ("AAA", "BBB"))
    return root, digests


@pytest.fixture
def pinned(mc_cache, monkeypatch):
    root, digests = mc_cache
    monkeypatch.setattr(F, "S_MANIFESTS", {p: (d,) for p, d in digests.items()})
    return root


def _sbuild(root, symbols=("AAA", "BBB"), **kw):
    return _build(root, ["s"], symbols=symbols, **kw)


def test_s_columns_and_registry(pinned):
    assert F.GROUPS["s"].prefix == "s_" and not F.GROUPS["s"].needs_sector
    assert len(F.S_COLUMNS) == 16 and F.S_COLUMNS[:3] == ("s_trend_slope_atr", "s_adx", "s_rv_ratio") and F.S_COLUMNS[-1] == "s_covered"
    assert "s_covered" not in F.GROUPS["s"].coverage_columns
    f = _sbuild(pinned)
    assert F.feature_columns(f) == list(F.S_COLUMNS)
    assert f[list(F.S_COLUMNS)].notna().all().all()


def test_the_row_of_session_s_is_used_at_s_unshifted_and_scaled(pinned):
    """The store row of session T is computed from the 128 bars ending AT T (the window's last bar is T), so it is
    public at T's close and needs no shift."""
    f = _sbuild(pinned).loc["BBB"]
    t = pd.Timestamp("2023-05-17")
    ordinal = (t - pd.Timestamp("2020-01-01")).days
    assert f.loc[t, "s_trend_slope_atr"] == pytest.approx(_value(1, ordinal, 0))
    assert f.loc[t, "s_adx"] == pytest.approx(_value(1, ordinal, 1) / 100.0)                  # ADX / 100
    assert f.loc[t, "s_rv_ratio"] == pytest.approx(_value(1, ordinal, 2))
    names = [fl.name for fl in mc.PROFILES["ta-structure-v1"].fields]
    j = names.index("structure_bars_since_bos")
    assert f.loc[t, "s_bars_since_bos"] == pytest.approx(_value(1, ordinal, j) / 128.0)       # bars / window
    j = names.index("structure_dist_support_atr")
    assert f.loc[t, "s_dist_support_atr"] == pytest.approx(_value(1, ordinal, j))


def test_a_value_whose_status_is_not_valid_becomes_nan(mc_cache, tmp_path, monkeypatch):
    root = tmp_path / "c"
    shutil.copytree(mc_cache[0], root)
    bad_day = date(2023, 5, 17)
    names = [fl.name for fl in mc.PROFILES["ta-structure-v1"].fields]
    j = names.index("structure_dist_resistance_atr")
    digests = write_mc_store(root, ("AAA", "BBB"), invalid={("ta-structure-v1", "AAA", bad_day): j,
                                                              ("ohlcv-v1", "AAA", bad_day): 1})
    monkeypatch.setattr(F, "S_MANIFESTS", {p: (d,) for p, d in digests.items()})
    f = _sbuild(root).loc["AAA"]
    t = pd.Timestamp(bad_day)
    assert np.isnan(f.loc[t, "s_dist_resistance_atr"]) and np.isnan(f.loc[t, "s_adx"])
    assert f.loc[t, "s_dist_support_atr"] == f.loc[t, "s_dist_support_atr"] and f.loc[t, "s_rv_ratio"] == f.loc[t, "s_rv_ratio"]
    assert f["s_adx"].isna().sum() == 1 and f.loc[t, "s_covered"] == 1                         # the row EXISTS, one value is invalid


def test_uncovered_symbols_and_sessions_outside_the_manifest_are_nan(pinned):
    f = _sbuild(pinned, symbols=("AAA", "CCC"), last=LAST).loc["CCC"]                         # CCC is not in the manifests
    value_cols = [c for c in F.S_COLUMNS if c != "s_covered"]
    assert f[value_cols].isna().all().all() and (f["s_covered"] == 0).all()                  # not in the store: flag 0
    g = _sbuild(pinned, symbols=("AAA",), first=date(2024, 3, 20), last=LAST).loc["AAA"]
    assert g[list(F.S_COLUMNS)].notna().all().all()                                          # the manifest ends 2024-03-28
    meta = _sbuild(pinned, symbols=("AAA", "CCC")).attrs["meta"]["source"]["market_conditions"]
    assert meta["ohlcv-v1"]["uncovered"] == ["CCC"] and meta["ta-structure-v1"]["window"][1] == "2024-03-28"


def test_a_manifest_must_be_alpaca_sourced_and_consistent(mc_cache, tmp_path, monkeypatch):
    root = tmp_path / "c"
    shutil.copytree(mc_cache[0], root)
    fmp = write_mc_store(root, ("AAA", "BBB"), source_profile="fmp-daily-split-adjusted-v1")
    monkeypatch.setattr(F, "S_MANIFESTS", {p: (d,) for p, d in fmp.items()})
    with pytest.raises(P.PanelDataError, match=r"fmp-daily-split-adjusted-v1.*Alpaca"):
        _sbuild(root)
    monkeypatch.setattr(F, "S_MANIFESTS", {"ohlcv-v1": ("0" * 64,), "ta-structure-v1": (fmp["ta-structure-v1"],)})
    with pytest.raises(P.PanelDataError, match="ohlcv-v1"):
        _sbuild(root)


def _cut_store(trunc, day):
    """Replace the market-condition store of a truncated copy by one that holds rows only through ``day``."""
    shutil.rmtree(trunc / "market_conditions", ignore_errors=True)
    return write_mc_store(trunc, ("AAA", "BBB"), cut=day)


def test_cut_test_s(mc_cache, tmp_path, monkeypatch):
    root, digests = mc_cache
    monkeypatch.setattr(F, "S_MANIFESTS", {p: (d,) for p, d in digests.items()})
    full = _sbuild(root)
    from tests.test_ml_features import _truncate
    trunc = _truncate(root, tmp_path, CUT_DAY)
    cut_digests = _cut_store(trunc, CUT_DAY)
    monkeypatch.setattr(F, "S_MANIFESTS", {p: (d,) for p, d in cut_digests.items()})
    cut = _sbuild(trunc, last=CUT_DAY)
    common = full[full.index.get_level_values("session") <= pd.Timestamp(CUT_DAY)]
    pd.testing.assert_frame_equal(common.drop(columns="label_bar_filled"), cut.drop(columns="label_bar_filled"), check_exact=True)
    assert cut[list(F.S_COLUMNS)].notna().all().all() and len(cut) > 700


@pytest.mark.parametrize("profile", ["ohlcv-v1", "ta-structure-v1"])
def test_the_real_store_row_of_a_session_is_computed_through_that_session(profile):
    """The evidence for the no-shift rule, on the real store, for BOTH profiles: the retained window of the row of session T
    ends at T."""
    import os
    from ba2_common.config import CACHE_FOLDER
    if not os.path.isdir(os.path.join(CACHE_FOLDER, "market_conditions", "ohlcv-v1", "manifests")):
        pytest.skip("no real market_conditions store on this host")
    store = MarketConditionStore(CACHE_FOLDER)
    digest = F.S_MANIFESTS[profile][0]
    man = store.read_manifest(digest, profile)
    assert man["source_profile"].startswith("alpaca") and man["timing_policy"] == "prior_session_v1"
    sym = sorted(man["coverage"])[0]
    rf = store.rows_frame(man, sym)
    for i in (len(rf) // 3, len(rf) // 2, len(rf) - 5):
        if not rf.raw_shard_refs[i]:
            continue
        sessions, bars = store.read_raw_concat(rf.raw_shard_refs[i])
        lo, hi = int(rf.raw_row_lo[i]), int(rf.raw_row_hi[i])
        assert hi - lo == mc.WINDOW and sessions[hi - 1] == rf.sessions[i]
        return
    pytest.fail("no row with a retained window found")


def test_s_covered_is_one_where_the_symbol_has_a_row_and_zero_at_a_hole(mc_cache, tmp_path, monkeypatch):
    root = tmp_path / "c"
    shutil.copytree(mc_cache[0], root)
    hole = date(2023, 5, 17)
    digests = write_mc_store(root, ("AAA", "BBB"), skip={("BBB", hole)})
    monkeypatch.setattr(F, "S_MANIFESTS", {p: (d,) for p, d in digests.items()})
    f = _sbuild(root)
    t = pd.Timestamp(hole)
    assert f.loc[("BBB", t), "s_covered"] == 0 and f.loc[("BBB", t), [c for c in F.S_COLUMNS if c != "s_covered"]].isna().all()
    assert f.loc[("AAA", t), "s_covered"] == 1 and f.loc["BBB", "s_covered"].sum() == len(f.loc["BBB"]) - 1
    assert f["s_covered"].notna().all()                                                         # a flag, never NaN
