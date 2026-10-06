"""Stuck provisional daily bars (AMD/INTC/MU on 8082, 2026-10-05) and the guard that must NOT change.

A cached one-tick snapshot (O == H, L == C) of a recent session is REPLACED by the vendor's final bar;
every true disagreement (settled bar, old bar, split) still refuses / re-bases as before.
"""
from __future__ import annotations

import importlib
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import ba2_common.config as bcfg
from ba2_common.core import native_cache
from ba2_common.core.interfaces.MarketDataProviderInterface import MarketDataProviderInterface
from ba2_common.core.market_calendar import NY_TZ, nyse_regular_sessions
from ba2_common.core.ohlcv_topup_guard import OHLCVTopUpRefused
from ba2_common.core.split_basis import CalendarSplit

from ba2_trade_platform.modules.dataproviders import ohlcv_provisional as prov

mdpi_mod = importlib.import_module("ba2_common.core.interfaces.MarketDataProviderInterface")
# importlib: the package attribute ``ba2_providers.ohlcv.FMPOHLCVProvider`` is the class, not the module
FMPOHLCVProvider = importlib.import_module("ba2_providers.ohlcv.FMPOHLCVProvider").FMPOHLCVProvider
PROVIDER = "FMPOHLCVProvider"

YESTERDAY = date.today() - timedelta(days=1)
SESSIONS = [o.astimezone(NY_TZ).date() for o, _c in nyse_regular_sessions(date(2023, 1, 3), YESTERDAY)]
LAST = SESSIONS[-1]
PROV_DAY = SESSIONS[-3]          # "2026-09-28": the stuck bar; two newer sessions are the new bars
CACHE_END = PROV_DAY

# symbol -> (cached O/H/L/C, vendor O/H/L/C) from the 8082 error log (vendor tails truncated there)
CASES = {
    "AMD": ((635.068, 635.068, 623.78, 623.78), (624.9, 629.75, 596.0, 600.0)),
    "INTC": ((126.88, 126.88, 120.655, 120.655), (120.68, 121.71, 117.9, 118.4)),
    "MU": ((1095.47, 1095.47, 1074.73, 1076.77), (1075.98, 1084.81, 1060.2, 1068.3)),
}


def _truth(level: float, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(SESSIONS)
    c = level * np.exp(np.cumsum(rng.normal(0.0002, 0.006, n)))
    o = c * (1 + rng.normal(0, 0.002, n))
    return pd.DataFrame({"Date": pd.to_datetime(SESSIONS), "Open": o.round(3),
                         "High": (np.maximum(o, c) * 1.004).round(3),
                         "Low": (np.minimum(o, c) * 0.996).round(3), "Close": c.round(3),
                         "Volume": rng.integers(8_000_000, 20_000_000, n)})


class _Provider(FMPOHLCVProvider):
    def __new__(cls, *a, **k):
        # tests/test_factorranker_*.py patch FMPOHLCVProvider.__new__ and leave it broken for the session
        return object.__new__(cls)

    def __init__(self, vendor, splits=()):
        super().__init__(api_key="test-key")
        self.vendor, self.splits, self.impl_calls = vendor, list(splits), []

    def _get_ohlcv_data_impl(self, symbol, start_date, end_date, interval="1d"):
        self.impl_calls.append((symbol, start_date.date(), end_date.date()))
        d = self.vendor
        out = d[(d["Date"] >= pd.Timestamp(start_date.date()))
                & (d["Date"] <= pd.Timestamp(end_date.date()))].reset_index(drop=True).copy()
        out["Date"] = pd.to_datetime(out["Date"]).dt.tz_localize("UTC")
        return out

    def _split_calendar(self, symbol, interval):
        return list(self.splits)


class _Fixed(_Provider):
    """Provider with the provisional-bar repair installed (as ``wire_all_seams`` does live)."""


class _Plain(_Provider):
    """Provider WITHOUT the repair: the guard as it was."""


prov.install(_Fixed)
# other tests call wire_all_seams(), which wraps the BASE class for the whole process: unwrap for _Plain
_orig = MarketDataProviderInterface._verified_tail_topup
while hasattr(_orig, "__wrapped__"):
    _orig = _orig.__wrapped__
_Plain._verified_tail_topup = _orig


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    cache = str(tmp_path / "cache")
    monkeypatch.setattr(bcfg, "CACHE_FOLDER", cache)
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", cache)
    monkeypatch.setattr(native_cache, "_CACHE_ROOT", os.path.join(cache, "datasets", "cache"))
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    MarketDataProviderInterface._SPLIT_BASIS_REPORTED.clear()
    prov._REFUSALS_LOGGED.clear()
    yield
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    prov._REFUSALS_LOGGED.clear()


@pytest.fixture
def activity(monkeypatch):
    rows = []
    monkeypatch.setattr(prov, "_write_activity", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture
def logged(monkeypatch):
    seen = {"info": [], "error": [], "warning": []}
    for level in seen:
        real = getattr(mdpi_mod.logger, level)
        monkeypatch.setattr(mdpi_mod.logger, level,
                            lambda msg, *a, _l=level, _r=real, **k: (seen[_l].append(str(msg)), _r(msg, *a, **k))[1])
    return seen


def _world(symbol: str, cached_bar, vendor_bar, *, level: float, cache_end=CACHE_END, day=PROV_DAY):
    """(vendor frame, cached frame): the same truth, except ``day`` (cached snapshot vs vendor final)."""
    truth = _truth(level)
    vendor = truth.copy()
    i = vendor.index[vendor["Date"] == pd.Timestamp(day)][0]
    for k, col in enumerate(("Open", "High", "Low", "Close")):
        vendor.loc[i, col] = vendor_bar[k]
    cached = vendor[vendor["Date"] <= pd.Timestamp(cache_end)].copy()
    for k, col in enumerate(("Open", "High", "Low", "Close")):
        cached.loc[i, col] = cached_bar[k]
    return vendor, cached.reset_index(drop=True)


def _write(symbol, cached) -> str:
    out = cached.copy()
    out["effective_date"] = out["Date"]
    native_cache.write_timeseries(PROVIDER, symbol, "1d", out)
    return native_cache.find_timeseries_path(PROVIDER, symbol, "1d")


def _topup(provider, symbol):
    path = native_cache.find_timeseries_path(PROVIDER, symbol, "1d")
    return provider._refresh_parquet_if_stale(pd.read_parquet(path), symbol, "1d", PROVIDER)


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- the three live cases
@pytest.mark.parametrize("symbol", sorted(CASES))
def test_provisional_bar_is_replaced_and_nothing_else_touched(symbol, logged, activity):
    cbar, vbar = CASES[symbol]
    vendor, cached = _world(symbol, cbar, vbar, level=vbar[3])
    path = _write(symbol, cached)
    before = pd.read_parquet(path)

    # the guard as it was: refuses (the bug)
    with pytest.raises(OHLCVTopUpRefused, match="disagrees"):
        _topup(_Plain(vendor), symbol)
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    logged["error"].clear()
    activity.clear()

    out = _topup(_Fixed(vendor), symbol)
    after = pd.read_parquet(path)
    row = after[after["Date"] == pd.Timestamp(PROV_DAY)].iloc[0]
    assert (row.Open, row.High, row.Low, row.Close) == pytest.approx(vbar)
    # every other previously cached bar is byte-for-byte what it was
    others = before[before["Date"] != pd.Timestamp(PROV_DAY)]
    pd.testing.assert_frame_equal(
        after[after["Date"].isin(others["Date"])].reset_index(drop=True), others.reset_index(drop=True))
    assert after["Date"].max() == pd.Timestamp(LAST) and len(after) == len(before) + 2
    assert len(out) == len(after)
    infos = [m for m in logged["info"] if "provisional" in m and symbol in m]
    assert len(infos) == 1                                # ONE line per symbol
    assert not logged["error"]
    assert not activity                                   # no refusal -> no failure entry


def test_exact_match_is_untouched_and_costs_no_extra_vendor_call(logged):
    symbol = "AMD"
    vendor = _truth(600.0)
    cached = vendor[vendor["Date"] <= pd.Timestamp(CACHE_END)].copy()
    path = _write(symbol, cached)
    fixed, plain = _Fixed(vendor), _Plain(vendor)
    _topup(plain, symbol)
    expect = _bytes(path)
    _write(symbol, cached)
    _topup(fixed, symbol)
    assert _bytes(path) == expect
    assert len(fixed.impl_calls) == len(plain.impl_calls)
    assert not any("provisional" in m for m in logged["info"])


# --------------------------------------------------------------------------- what must still refuse
def _settled_mismatch_world(symbol="AMD"):
    """Last bar a provisional snapshot AND an older settled bar (4 sessions back) that disagrees."""
    cbar, vbar = CASES[symbol]
    vendor, cached = _world(symbol, cbar, vbar, level=vbar[3])
    older = cached.index[cached["Date"] == pd.Timestamp(SESSIONS[-5])][0]
    cached.loc[older, "Close"] = round(cached.loc[older, "Close"] * 1.06, 3)   # not split-like
    return vendor, cached


def test_settled_mismatch_refuses_loudly_and_leaves_the_cache_byte_identical(logged, activity):
    symbol = "AMD"
    vendor = _truth(600.0)
    cached = vendor[vendor["Date"] <= pd.Timestamp(CACHE_END)].copy().reset_index(drop=True)
    day = SESSIONS[-4]
    i = cached.index[cached["Date"] == pd.Timestamp(day)][0]
    cached.loc[i, "Close"] = round(cached.loc[i, "Close"] * 1.06, 3)
    path = _write(symbol, cached)
    expect = _bytes(path)
    c = cached.loc[i]
    v = vendor[vendor["Date"] == pd.Timestamp(day)].iloc[0]

    with pytest.raises(OHLCVTopUpRefused) as e:
        _topup(_Fixed(vendor), symbol)
    assert _bytes(path) == expect
    msg = str(e.value)
    assert symbol in msg and day.isoformat() in msg
    assert f"{c.Open:g}/{c.High:g}/{c.Low:g}/{c.Close:g}" in msg
    assert f"{v.Open:g}/{v.High:g}/{v.Low:g}/{v.Close:g}" in msg
    assert any("REFUSED" in m and day.isoformat() in m for m in logged["error"])
    # Activity Log: one FAILURE entry with symbol + date + values
    assert len(activity) == 1
    a = activity[0]
    assert a["severity"].value == "failure"
    assert a["data"]["symbol"] == symbol and a["data"]["bars"][0]["date"] == day.isoformat()
    assert a["data"]["bars"][0]["cached_ohlc"] == f"{c.Open:g}/{c.High:g}/{c.Low:g}/{c.Close:g}"
    assert a["data"]["bars"][0]["vendor_ohlc"] == f"{v.Open:g}/{v.High:g}/{v.Low:g}/{v.Close:g}"
    # a repeated pass raises again but does not flood the Activity Log
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), symbol)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), symbol)
    assert len(activity) == 1
    # another day writes a new entry
    prov._REFUSALS_LOGGED.clear()
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), symbol)
    assert len(activity) == 2


def test_provisional_replace_never_swallows_a_settled_mismatch_in_the_same_topup(activity):
    symbol = "AMD"
    vendor, cached = _settled_mismatch_world(symbol)
    path = _write(symbol, cached)
    expect = _bytes(path)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), symbol)
    assert _bytes(path) == expect                       # the provisional bar was NOT replaced either
    assert len(activity) == 1


def test_an_old_provisional_shaped_bar_still_refuses(activity):
    symbol = "AMD"
    old_end = SESSIONS[-40]                              # ~8 weeks back: outside the age window
    cbar, vbar = CASES[symbol]
    vendor, cached = _world(symbol, cbar, vbar, level=vbar[3], cache_end=old_end, day=old_end)
    path = _write(symbol, cached)
    expect = _bytes(path)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), symbol)
    assert _bytes(path) == expect and len(activity) == 1


def test_a_split_rescaled_snapshot_is_not_a_provisional_bar():
    cbar, vbar = CASES["AMD"]
    vendor, cached = _world("AMD", tuple(x * 2 for x in cbar), vbar, level=vbar[3])
    assert prov.find_provisional_days(cached, vendor) == []


def test_without_an_anchor_nothing_is_replaced():
    cbar, vbar = CASES["AMD"]
    vendor, cached = _world("AMD", cbar, vbar, level=vbar[3])
    only = cached[cached["Date"] == pd.Timestamp(PROV_DAY)]
    assert prov.find_provisional_days(only, vendor) == []


def test_a_real_split_is_still_rebased_by_a_full_refetch():
    symbol = "XSPL"
    split_day = SESSIONS[-8]
    vendor = _truth(80.0)
    cached = vendor[vendor["Date"] <= pd.Timestamp(SESSIONS[-12])].copy()
    pre = cached["Date"] < pd.Timestamp(split_day)
    for col in ("Open", "High", "Low", "Close"):
        cached.loc[pre, col] = (cached.loc[pre, col] * 2.0).round(3)
    path = _write(symbol, cached.reset_index(drop=True))
    p = _Fixed(vendor, [CalendarSplit(split_day, 2.0)])
    out = _topup(p, symbol)
    after = pd.read_parquet(path)
    assert np.allclose(after["Close"].to_numpy(float), vendor["Close"].to_numpy(float))
    assert len(after) == len(vendor) and len(out) == len(vendor)


# --------------------------------------------------------------------------- the repair tool
def _tool():
    tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return importlib.import_module("repair_provisional_bars")


def _seed_two_symbols(tmp_path):
    cbar, vbar = CASES["AMD"]
    vendor, cached = _world("AMD", cbar, vbar, level=vbar[3])
    _write("AMD", cached)
    clean = vendor[vendor["Date"] <= pd.Timestamp(CACHE_END)].copy()
    clean = clean.reset_index(drop=True)
    _write("CLEAN", clean)
    return vendor


def test_tool_refuses_a_live_cache_and_dry_run_writes_nothing(tmp_path, capsys):
    tool = _tool()
    assert tool.main(["--db-file", "x"]) == 2                                   # no --cache-folder = live
    assert tool.main(["--cache-folder", tool.live_cache_folder(), "--db-file", "x"]) == 2
    assert "REFUSED" in capsys.readouterr().err
    vendor = _seed_two_symbols(tmp_path)
    paths = {s: native_cache.find_timeseries_path(PROVIDER, s, "1d") for s in ("AMD", "CLEAN")}
    before = {s: _bytes(p) for s, p in paths.items()}
    rc = tool.main(["--cache-folder", bcfg.CACHE_FOLDER], provider_factory=lambda: _Provider(vendor))
    out = capsys.readouterr().out
    assert rc == 0 and "AMD: 1 provisional" in out and "CLEAN" not in out and "DRY RUN" in out
    assert {s: _bytes(p) for s, p in paths.items()} == before


def test_tool_apply_replaces_only_the_provisional_bar(tmp_path, capsys):
    tool = _tool()
    vendor = _seed_two_symbols(tmp_path)
    paths = {s: native_cache.find_timeseries_path(PROVIDER, s, "1d") for s in ("AMD", "CLEAN")}
    clean_before = _bytes(paths["CLEAN"])
    amd_before = pd.read_parquet(paths["AMD"])
    rc = tool.main(["--cache-folder", bcfg.CACHE_FOLDER, "--apply"], provider_factory=lambda: _Provider(vendor))
    assert rc == 0 and "APPLIED" in capsys.readouterr().out
    assert _bytes(paths["CLEAN"]) == clean_before
    amd = pd.read_parquet(paths["AMD"])
    row = amd[amd["Date"] == pd.Timestamp(PROV_DAY)].iloc[0]
    assert (row.Open, row.High, row.Low, row.Close) == pytest.approx(CASES["AMD"][1])
    pd.testing.assert_frame_equal(amd[amd["Date"] != pd.Timestamp(PROV_DAY)].reset_index(drop=True),
                                  amd_before[amd_before["Date"] != pd.Timestamp(PROV_DAY)].reset_index(drop=True))
    # and the live top-up now goes through
    _topup(_Plain(vendor), "AMD")
    assert pd.read_parquet(paths["AMD"])["Date"].max() == pd.Timestamp(LAST)
