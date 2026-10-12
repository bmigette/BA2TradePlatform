"""Round-2 review guards: the API key never reaches a log record; an explicit None in a deploy payload is resolved, not written;
the unreviewed exclusions refuse the launch; a relative panel path; the intraday coverage threshold; the no-double-count of a
priceless candidate in the rank step; the exact band bound (no hard-coded tolerance)."""
import logging
import os

import numpy as np
import pytest

from ba2_common.core.deploy_parity import SCREENER_OFF_VALUES, SCREENER_SELECTION_KEYS, complete_screener_settings
from ba2_providers.fmp_common import redact
from ba2_providers.screener import intraday_coverage as ic
from ba2_providers.screener import live_sim as ls

KEY = "SECRETKEY1234567890abcdef"


def test_redact_scrubs_urls_params_dicts_and_exception_texts():
    for t in (f"https://x/api/v3/stock-screener?apikey={KEY}&limit=10", f"params={{'apikey': '{KEY}', 'a': 1}}",
              f"HTTPSConnectionPool(host='x'): url: /q?a=1&apikey={KEY} (Caused by ...)", f'{{"apikey": "{KEY}"}}'):
        out = redact(t)
        assert KEY not in out and "***" in out


def test_the_key_never_appears_in_a_log_record_on_the_screen_path(monkeypatch, caplog):
    import requests
    import sys
    import ba2_providers.screener  # noqa: F401
    mod = sys.modules["ba2_providers.screener.FMPScreenerProvider"]
    from ba2_providers import fmp_common
    prov_cls = mod.FMPScreenerProvider
    monkeypatch.setattr(mod, "get_app_setting", lambda k, *a, **kw: KEY if k == "FMP_API_KEY" else None, raising=False)
    p = prov_cls.__new__(prov_cls)
    p.api_key = KEY

    def boom(url, params=None, **kw):
        raise requests.HTTPError(f"500 Server Error for url: {url}?apikey={KEY}")
    monkeypatch.setattr(fmp_common, "fmp_http_get", boom)
    caplog.set_level(logging.DEBUG)
    from ba2_common.logger import logger as app_logger
    app_logger.addHandler(caplog.handler)
    try:
        with pytest.raises(Exception):
            p.screen_stocks({})
    finally:
        app_logger.removeHandler(caplog.handler)
    assert "FMP screener request" in caplog.text and "apikey" in caplog.text    # the DEBUG line really ran
    assert KEY not in caplog.text
    # the retry loop's own lines (request error + final FMPError) are scrubbed too
    monkeypatch.undo()
    calls = []

    def getter(url, params=None, timeout=None):
        calls.append(1)
        raise requests.ConnectionError(f"HTTPSConnectionPool: url: /x?apikey={KEY}")
    caplog.clear()
    app_logger.addHandler(caplog.handler)
    try:
        with pytest.raises(fmp_common.FMPError) as ei:
            fmp_common.fmp_http_get("https://x/y", params={"apikey": KEY}, endpoint="t", timeout=1, getter=getter, delays=(0, 0))
    finally:
        app_logger.removeHandler(caplog.handler)
    assert KEY not in caplog.text and KEY not in str(ei.value)


def test_an_explicit_none_in_the_deploy_payload_is_resolved_to_the_table_value():
    base = {f"screener_{k}": 1 for k in SCREENER_SELECTION_KEYS}
    got = complete_screener_settings({**base, "screener_price_min": None, "price_min": 25.0, "screener_float_min": None})
    assert got["screener_price_min"] == 25.0                       # the unprefixed value is not shadowed by a prefixed None
    assert got["screener_float_min"] == SCREENER_OFF_VALUES["float_min"]
    merged = {**{"screener_float_min": None}, **got}               # what tools/import_deploy_payload now writes
    assert merged["screener_float_min"] is not None


def _man(*reviewers):
    return {"excluded_unusable": [{"symbol": f"S{i}", "reason": "r", "added": "2026-10-08", "reviewed_by": r}
                                  for i, r in enumerate(reviewers)]}


def test_pending_exclusions_are_found_and_the_reviewed_ones_kept():
    m = _man("pending owner review", "Bastien", "Pending something")
    assert [e["symbol"] for e in ls.pending_exclusions(m)] == ["S0", "S2"]
    assert [e["symbol"] for e in ls.reviewed_exclusions(m)] == ["S1"]


def test_panel_problems_refuses_a_panel_with_unreviewed_exclusions(tmp_path, monkeypatch):
    man = {"criteria_version": ls.CRITERIA_VERSION, "panel_format": ls.PANEL_FORMAT, "first_session": "2000-01-03",
           "last_session": "2030-01-01", "last_bar_date": "2030-01-01", "shares_vendor_snapshot": "x", "shares_lag_days": 45,
           "stale_listed_symbols": [], "panel_fingerprint": "f", **_man("pending owner review")}
    monkeypatch.setattr(ls, "read_manifest", lambda p: man)
    monkeypatch.setattr(ls, "_sessions_of", lambda p: ["2025-01-02", "2025-01-03"])
    probs = ls.panel_problems("x", "2025-01-02", "2025-01-03")
    assert any("NOT reviewed" in p and "S0" in p for p in probs)
    man["excluded_unusable"][0]["reviewed_by"] = "Bastien"
    assert not any("NOT reviewed" in p for p in ls.panel_problems("x", "2025-01-02", "2025-01-03"))


def test_panel_rel_handles_relative_and_outside_paths(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    (cache / "screener" / "daily_panel" / "fp").mkdir(parents=True)
    assert ls.panel_rel(str(cache), str(cache / "screener" / "daily_panel" / "fp")) == "screener/daily_panel/fp"
    monkeypatch.chdir(cache)
    assert ls.panel_rel(str(cache), os.path.join("screener", "daily_panel", "fp")) == "screener/daily_panel/fp"
    outside = tmp_path / "elsewhere" / "p"
    outside.mkdir(parents=True)
    got = ls.panel_rel(str(cache), str(outside))
    assert os.path.isabs(got) and got.endswith("elsewhere/p")


def _write_pq(path, dates, intraday=False):
    import pandas as pd
    if intraday:
        ts = [pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=30 + 5 * i) for d in dates for i in range(3)]
    else:
        ts = [pd.Timestamp(d) for d in dates]
    pd.DataFrame({"Date": ts, "Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}).to_parquet(path)


def test_intraday_coverage_report_and_threshold(tmp_path):
    days = [f"2023-03-{d:02d}" for d in range(1, 29)]
    _write_pq(tmp_path / "AAA_1d.parquet", days)
    _write_pq(tmp_path / "AAA_5min.parquet", days, intraday=True)
    _write_pq(tmp_path / "BBB_1d.parquet", days)
    _write_pq(tmp_path / "BBB_5min.parquet", days[:14], intraday=True)           # half missing
    _write_pq(tmp_path / "CCC_1d.parquet", days)                                  # no intraday file at all
    rep = ic.scan(["AAA", "BBB", "CCC"], str(tmp_path), "5min", "2023-03-01", "2023-03-31")
    assert rep["no_intraday_file"] == ["CCC"] and rep["daily_sessions"] == 84 and rep["covered_sessions"] == 42
    assert ic.problems(rep)                                                       # 50 % missing > 5 %
    ok = ic.scan(["AAA"], str(tmp_path), "5min", "2023-03-01", "2023-03-31")
    assert ok["missing_coverage_share"] == 0 and ic.problems(ok) == [] and "100.0%" in ic.format_report(ok)


def test_the_priceless_candidate_is_priced_once_per_decision_when_only_the_rank_needs_the_price():
    n = 3
    cols = dict(symbols=np.array(["A", "B", "C"]), shares=np.full(n, 1e8), last_close=np.full(n, 50.0), rvol=np.full(n, 2.0),
                last_vol=np.full(n, 1e6), avg20=np.full(n, 1e6), w2=np.ones(n, bool), fl=np.full(n, np.nan), peak=None)
    calls = []

    def now(idx):
        calls.append(list(idx))
        v = np.array([40.0, np.nan, 60.0])[idx]
        return v, v
    got = ls.select_from_columns(**cols, now=now, settings={"market_cap_min": 1e9, "max_stocks": 10}, beh=ls.POST_FIX)
    assert len(calls) == 1                                  # not re-read by the rank step
    assert [str(cols["symbols"][i]) for i in got] == ["C", "B", "A"]       # B (no price) ranks on its previous-close cap (5e9) between C (6e9) and A (4e9)


def test_the_band_bound_is_exact_not_a_tolerance_window():
    n = 1
    day_lo, day_hi = np.array([95.0]), np.array([400.0])                    # a 4x intraday move
    cols = dict(symbols=np.array(["A"]), shares=np.full(n, 1e8), last_close=np.array([100.0]), rvol=np.full(n, 2.0),
                last_vol=np.full(n, 1e6), avg20=np.full(n, 1e6), w2=np.ones(n, bool), fl=np.full(n, np.nan), peak=None)
    st = {"market_cap_min": 3.5e10, "max_stocks": 5}                        # needs a price >= 350
    got = ls.select_from_columns(**cols, now=np.array([380.0]), settings=st, beh=ls.POST_FIX, band_at_now=True,
                                 day_lo=day_lo, day_hi=day_hi)
    assert len(got) == 1
    with pytest.raises(ls.SimulationRefusal):
        ls.select_from_columns(**cols, now=np.array([380.0]), settings=st, beh=ls.POST_FIX, band_at_now=True)
