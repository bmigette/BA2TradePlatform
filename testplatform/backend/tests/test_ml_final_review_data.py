"""Final whole-branch review of the ML exploration v2, part A (data): holdout guard on the stores, the wizard holdout,
F / I coverage before the metric store starts, the code-version and membership refusals, parity of the ATR."""
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import ml_features as F
from app.services import ml_holdout as HO
from tests.test_ml_features import FIRST, LAST, SECTOR_OF, _bars, _sessions, _write, cache  # noqa: F401
from tests.test_ml_features_store import cli, work  # noqa: F401

LOCKED = "AAA"                                  # hash-locked (see test_ml_features_store)
DEV = ["BBB", "CCC"]


@pytest.fixture(autouse=True)
def _small_cross_sections(monkeypatch):
    monkeypatch.setattr(F, "X_MIN_MEMBERS", 2)
    monkeypatch.setattr(F, "X_MIN_SHARE", 0.0)


def _ensure(root, out, symbols, groups=("t", "mtf"), **kw):
    kw.setdefault("sector_overrides", {})
    kw.setdefault("sector_of", SECTOR_OF)
    kw.setdefault("chunk_size", 2)
    return F.ensure_feature_store(out, symbols, FIRST, LAST, list(groups), cache_root=root, **kw)


def _meta(out):
    return json.loads((out / "features.parquet.meta.json").read_text(encoding="utf-8"))


def test_the_fixture_symbols_are_what_the_tests_assume():
    assert HO.is_locked_largecap(LOCKED) and not any(HO.is_locked_largecap(s) for s in DEV)


# ------------------------------------------------------------------------------------------------ A1: stores
def test_a_store_of_a_locked_symbol_is_refused(work, tmp_path):
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        _ensure(work, tmp_path / "s", [LOCKED] + DEV)
    assert not (tmp_path / "s").exists()


def test_a_locked_symbol_in_the_universe_or_the_membership_is_refused(work, tmp_path):
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        _ensure(work, tmp_path / "u", DEV, groups=("t", "x"), universe=DEV + [LOCKED])
    sessions = pd.DatetimeIndex(_sessions())
    member = pd.DataFrame(True, index=sessions, columns=[LOCKED, "BBB"])
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        _ensure(work, tmp_path / "m", ["BBB"], groups=("t", "x"), membership=member)


def test_an_explicit_holdout_build_is_allowed_and_recorded_in_the_meta(work, tmp_path):
    out = tmp_path / "h"
    _ensure(work, out, [LOCKED] + DEV, holdout=True)
    assert _meta(out)["purpose"] == "holdout"
    dev = tmp_path / "d"
    _ensure(work, dev, DEV)
    assert _meta(dev)["purpose"] == "dev"


def test_the_purpose_is_part_of_the_rebuild_key(work, tmp_path):
    out = tmp_path / "s"
    assert _ensure(work, out, DEV)["status"] == "built"
    assert _ensure(work, out, DEV)["status"] == "up_to_date"
    assert _ensure(work, out, DEV, holdout=True)["status"] == "built"
    assert _meta(out)["purpose"] == "holdout"


def test_the_launcher_refuses_locked_symbols_unless_holdout_is_given(cli):
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        cli(["aaa", "bbb"], holdout=False)
    rc, out = cli(["aaa", "bbb"], holdout=True)
    assert rc == 0 and _meta(out)["purpose"] == "holdout"
    rc, out = cli(["bbb", "ccc"], holdout=False)
    assert rc == 0 and _meta(out)["purpose"] == "dev"


# ------------------------------------------------------------------------------------------------ A3: F / I coverage
from tests.test_ml_features_fund import _fbuild, fund_cache  # noqa: E402,F401
from tests.test_ml_features_insider import _ibuild, insider_cache  # noqa: E402,F401

MARKET_CAP_COLUMNS = {"f": {"f_fcf_yield", "f_buyback_yield"}, "i": {"i_net_buy_90"}}


@pytest.mark.parametrize("group", ["f", "i"])
def test_the_market_cap_columns_do_not_define_the_coverage_of_f_and_i(group):
    cover = set(F.GROUPS[group].coverage_columns)
    assert cover and not cover & MARKET_CAP_COLUMNS[group]
    assert cover <= set({"f": F.F_COLUMNS, "i": F.I_COLUMNS}[group])
    assert MARKET_CAP_COLUMNS[group] < set({"f": F.F_COLUMNS, "i": F.I_COLUMNS}[group])


def test_f_is_not_masked_before_the_metric_store_starts(fund_cache):
    """The metric store's first scan (2021-06-02 in the synthetic cache) is the start of the market cap: before it only
    the two yields are NaN. The synthetic statements are too short for the year-over-year columns, so the other columns
    are filled in to isolate the market-cap effect."""
    f = _fbuild(fund_cache, first=date(2021, 1, 4), last=date(2021, 12, 30))
    pre = f.index.get_level_values("session") < pd.Timestamp("2021-06-02")
    assert 0.3 < pre.mean() < 0.5 and f.loc[pre, list(F.F_MARKET_CAP_COLUMNS)].isna().all().all()
    others = [c for c in F.F_COLUMNS if c not in F.F_MARKET_CAP_COLUMNS]
    f[others] = f[others].fillna(0.5)
    report = F.coverage_report(f, ["f"])
    assert report.loc[("f", 2021), "share"] == 1.0                      # judged on the other columns only
    masked = F.apply_coverage_mask(f, report, 0.7)
    assert masked[others].notna().all().all() and masked["f_missing"].eq(0.0).all()
    assert masked.loc[pre, list(F.F_MARKET_CAP_COLUMNS)].isna().all().all()    # the cap columns stay NaN there


def test_i_is_not_masked_before_the_metric_store_starts(insider_cache):
    i = _ibuild(insider_cache, first=date(2021, 1, 4), last=date(2021, 12, 30))
    pre = i.index.get_level_values("session") < pd.Timestamp("2021-06-02")
    assert 0.3 < pre.mean() < 0.5 and i.loc[pre, "i_net_buy_90"].isna().all() and i.loc[pre, "i_n_buyers_90"].notna().all()
    report = F.coverage_report(i, ["i"])
    assert report.loc[("i", 2021), "share"] == 1.0
    masked = F.apply_coverage_mask(i, report, 0.7)
    assert masked["i_n_buyers_90"].notna().all() and masked["i_missing"].eq(0.0).all()
    assert masked.loc[pre, "i_net_buy_90"].isna().all()


# ------------------------------------------------------------------------------------------------ A2: the wizard
def test_the_wizard_daily_source_refuses_locked_symbols_naming_them():
    from app.services import ml_dataset_source as S
    with pytest.raises(ValueError, match=r"holdout.*\b" + LOCKED + r"\b"):
        S.validate_daily_request("alpaca_hist", "1d", ["t"], None, LOCKED, None)
    with pytest.raises(ValueError, match="GGG") as ei:                          # the batch universe counts too
        S.validate_daily_request("alpaca_hist", "1d", ["t"], None, "BBB", ["BBB", "CCC", LOCKED, "GGG"])
    assert LOCKED in str(ei.value)
    with pytest.raises(ValueError, match=LOCKED):                               # a lower-case spelling does not slip past
        S.validate_daily_request("alpaca_hist", "1d", ["t"], None, LOCKED.lower(), None)
    ok = S.validate_daily_request("alpaca_hist", "1d", ["t"], None, "BBB", ["BBB", "CCC"])
    assert ok["daily_universe"] == ["BBB", "CCC"]


def test_the_wizard_daily_frame_refuses_locked_symbols_too():
    from app.services import ml_dataset_source as S
    req = {"daily_groups": ["t"], "daily_min_share": 0.7, "daily_universe": ["BBB", LOCKED]}
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        S.daily_frame("BBB", FIRST, LAST, req)
    with pytest.raises(HO.LockedSymbolError, match=LOCKED):
        S.daily_frame(LOCKED, FIRST, LAST, {**req, "daily_universe": [LOCKED]})


def test_the_intraday_wizard_sources_are_not_affected_by_the_holdout():
    from app.services import ml_dataset_source as S
    assert S.validate_daily_request("alpaca_hist", "1h", None, None, LOCKED, None) == {}
