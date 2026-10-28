"""Group I (insider filings) of the daily feature store."""
import json
import shutil
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services import ml_features as F
from app.services import ml_panel as P
from tests.test_ml_features import (STORE_SCANS, _assert_cut_identical, _build, _sessions, _store_rows, cache)  # noqa: F401
from tests.test_ml_features_analyst import write_fmp_rows

# (filingDate, type, shares, price, cik, formType)
FILINGS = [("2022-10-03 10:00:00", "A-Award", 10, 0.0, "0009", "4"),           # the first retained row: coverage from +90 days
           ("2023-02-01 09:00:00", "P-Purchase", 1000, 10.0, "0001", "4"),
           ("2023-02-10 16:30:00", "S-Sale", 500, 20.0, "0002", "4"),
           ("2023-02-12 08:00:00", "P-Purchase", 200, 10.0, "0001", "4"),          # a Sunday filing
           ("2023-03-01 10:00:00", "P-Purchase", 100, 50.0, "0003", "5"),          # not Form 4
           ("2023-03-05 10:00:00", "M-Exempt", 9999, 0.0, "0004", "4"),            # neither a purchase nor a sale
           ("2023-04-01 10:00:00", "P-Purchase", 300, 10.0, "0005", "4"),
           ("2023-04-03 10:00:00", "P-Purchase", 100, 10.0, "0006", "4/A")]        # an amendment


def _rows(sym, filings=FILINGS):
    return [{"symbol": sym, "filingDate": f, "transactionDate": f[:10], "reportingCik": cik, "reportingName": f"Person {cik}",
             "transactionType": t, "securitiesTransacted": n, "price": px, "formType": ft, "securityName": "Common Stock"}
            for f, t, n, px, cik, ft in filings]


@pytest.fixture(scope="module")
def insider_cache(cache, tmp_path_factory):
    root = tmp_path_factory.mktemp("insidercache")
    shutil.copytree(cache, root, dirs_exist_ok=True)
    for sym in ("AAA", "BBB"):
        write_fmp_rows(root, "insider_v2", sym, _rows(sym))
    (root / "fmp_history" / "insider__AAA.json").write_text(json.dumps([{"filingDate": "2020-01-01", "x": 1}]))   # v1: unused
    return root


def _ibuild(root, symbols=("AAA", "BBB"), **kw):
    kw.setdefault("first", date(2023, 1, 3))
    kw.setdefault("last", date(2023, 9, 29))
    return _build(root, ["i"], symbols=symbols, **kw)


def _mcap(sym, day):
    store = _store_rows().set_index(["date", "symbol"])
    scan = max(x for x in STORE_SCANS if x <= day)
    return float(store.loc[(scan, sym), "market_cap"])


def _reference(sessions, sym, filings=FILINGS):
    rows = [(pd.Timestamp(f[:10]), t, n * px, cik, ft) for f, t, n, px, cik, ft in filings]
    cov = min(r[0] for r in rows)
    out = []
    for S in sessions:
        w = [r for r in rows if S - pd.Timedelta(days=90) <= r[0] < S and r[4] == "4"]
        net = sum(r[2] for r in w if r[1].startswith("P-")) - sum(r[2] for r in w if r[1].startswith("S-"))
        buyers = len({r[3] for r in w if r[1].startswith("P-")})
        covered = S >= cov + pd.Timedelta(days=90)
        cap = _mcap(sym, str(S.date())) if str(S.date()) >= STORE_SCANS[0] else np.nan
        out.append({"i_net_buy_90": net / cap if covered else np.nan, "i_n_buyers_90": buyers if covered else np.nan,
                    "i_covered": 1.0 if covered else 0.0})
    return pd.DataFrame(out, index=sessions)[list(F.I_COLUMNS)].astype("float64")


def test_i_columns_and_registry(insider_cache):
    assert F.GROUPS["i"].prefix == "i_" and not F.GROUPS["i"].needs_sector
    assert list(F.I_COLUMNS) == ["i_net_buy_90", "i_n_buyers_90", "i_covered"]
    assert F.feature_columns(_ibuild(insider_cache)) == list(F.I_COLUMNS)


def test_every_column_against_a_brute_force_reference(insider_cache):
    f = _ibuild(insider_cache).loc["AAA"]
    pd.testing.assert_frame_equal(f[list(F.I_COLUMNS)], _reference(f.index, "AAA"), check_exact=False, rtol=1e-12)


def test_hand_checked_values(insider_cache):
    f = _ibuild(insider_cache).loc["AAA"]
    t = pd.Timestamp
    assert f.loc[t("2023-02-01"), "i_covered"] == 1 and f.loc[t("2023-02-01"), "i_net_buy_90"] == 0       # filed that day: not yet
    assert f.loc[t("2023-02-02"), "i_covered"] == 1                                                         # the session after
    assert f.loc[t("2023-02-02"), "i_net_buy_90"] == pytest.approx(10000.0 / _mcap("AAA", "2023-02-02"))
    assert f.loc[t("2023-02-10"), "i_net_buy_90"] == pytest.approx(10000.0 / _mcap("AAA", "2023-02-10"))   # the 16:30 sale: not yet
    assert f.loc[t("2023-02-13"), "i_net_buy_90"] == pytest.approx(2000.0 / _mcap("AAA", "2023-02-13"))    # Friday sale AND Sunday buy
    assert f.loc[t("2023-02-21"), "i_n_buyers_90"] == 1                                                     # the same person twice
    d = t("2023-04-10")
    assert f.loc[d, "i_net_buy_90"] == pytest.approx(5000.0 / _mcap("AAA", "2023-04-10")) and f.loc[d, "i_n_buyers_90"] == 2
    late = t("2023-09-01")                                                                                  # an empty window after coverage
    assert f.loc[late, "i_net_buy_90"] == 0 and f.loc[late, "i_n_buyers_90"] == 0 and f.loc[late, "i_covered"] == 1


def test_coverage_starts_90_days_after_the_first_retained_filing(insider_cache, tmp_path):
    root = tmp_path / "c"
    shutil.copytree(insider_cache, root)
    write_fmp_rows(root, "insider_v2", "AAA", _rows("AAA", [("2023-01-05 10:00:00", "P-Purchase", 100, 10.0, "0001", "4")]))
    f = _ibuild(root, symbols=("AAA",), first=date(2023, 3, 1), last=date(2023, 6, 30)).loc["AAA"]
    t = pd.Timestamp
    assert f.loc[t("2023-04-04"), "i_covered"] == 0 and f.loc[t("2023-04-04"), ["i_net_buy_90", "i_n_buyers_90"]].isna().all()
    assert f.loc[t("2023-04-05"), "i_covered"] == 1 and f.loc[t("2023-04-05"), "i_n_buyers_90"] == 1      # 01-05 + 90 days


def test_net_buy_is_nan_where_the_market_cap_is_unknown(insider_cache, tmp_path):
    """Before the store's first scan (2021-06-02 in the fixture) there is no market cap, but the flag and the buyers are there."""
    root = tmp_path / "c"
    shutil.copytree(insider_cache, root)
    write_fmp_rows(root, "insider_v2", "AAA", _rows("AAA", [("2021-02-01 10:00:00", "P-Purchase", 100, 10.0, "0001", "4")]))
    f = _ibuild(root, symbols=("AAA",), first=date(2021, 3, 1), last=date(2021, 8, 31)).loc["AAA"]
    early = f.index < pd.Timestamp(STORE_SCANS[0])
    covered = f["i_covered"] == 1
    assert covered.iloc[0] == False and covered.loc[pd.Timestamp("2021-05-03"):].all()                     # 2021-02-01 + 90 days
    gap = early & covered                                                                                   # covered, but no scan yet
    assert gap.sum() > 10 and f.loc[gap, "i_net_buy_90"].isna().all() and f.loc[gap, "i_n_buyers_90"].notna().all()
    assert f.loc[~early, "i_net_buy_90"].notna().any()


def test_missing_file_refuses_an_empty_file_is_never_covered(insider_cache, tmp_path):
    with pytest.raises(P.PanelDataError, match="CCC"):
        _ibuild(insider_cache, symbols=("AAA", "CCC"))
    root = tmp_path / "c"
    shutil.copytree(insider_cache, root)
    write_fmp_rows(root, "insider_v2", "AAA", [])
    f = _ibuild(root, symbols=("AAA",)).loc["AAA"]
    assert (f["i_covered"] == 0).all() and f[["i_net_buy_90", "i_n_buyers_90"]].isna().all().all()


def test_only_insider_v2_is_opened(insider_cache, monkeypatch):
    import builtins
    from pathlib import Path
    opened = []
    real_open, real_read = builtins.open, Path.read_text
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (opened.append(str(self)), real_read(self, *a, **k))[1])
    _ibuild(insider_cache)
    assert sorted({Path(p).name for p in opened if "fmp_history" in p}) == ["insider_v2__AAA.json", "insider_v2__BBB.json"]


def _truncate_insider(trunc, day):
    for p in (trunc / "fmp_history").glob("insider_v2__*.json"):
        rows = json.loads(p.read_text())
        p.write_text(json.dumps([r for r in rows if str(r["filingDate"])[:10] < str(day)]))


@pytest.mark.parametrize("day", [date(2023, 11, 15), date(2023, 2, 10), date(2023, 2, 13), date(2023, 4, 3)])
def test_cut_test_i(insider_cache, tmp_path, day):
    full = _assert_cut_identical(insider_cache, tmp_path, ["i"], day=day, truncate_extra=_truncate_insider,
                                 nan_ok=True, min_valid=0.02)
    assert full["i_covered"].sum() > 100
