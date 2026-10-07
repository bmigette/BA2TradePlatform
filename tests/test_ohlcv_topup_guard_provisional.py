"""Stuck provisional daily bars (AMD/INTC/MU on 8082, 2026-10-05) and the guard that must NOT change.

A cached one-tick snapshot (O == H, L == C) of a recent session is REPLACED by the vendor's final bar;
every true disagreement (settled bar, old bar, split) still refuses / re-bases as before.
"""
from __future__ import annotations

import importlib
import os
import sys
from datetime import date, datetime, timedelta, timezone

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
    "AMD": ((635.068, 635.068, 623.78, 623.78), (624.9, 629.75, 596.07, 607.87)),
    "INTC": ((126.88, 126.88, 120.655, 120.655), (120.68, 121.71, 117.9, 118.4)),
    "MU": ((1095.47, 1095.47, 1074.73, 1076.77), (1075.98, 1084.81, 1060.2, 1068.3)),
    "FSLR": ((179.88, 179.88, 179.88, 179.88), (178.13, 178.23, 172.2, 172.97)),
    "CLS": ((380.51, 380.51, 361.295, 364.34), (363.93, 372.97, 349.52, 356.53)),   # close 0.8% above low
    "QCOM": ((191.61, 191.61, 180.56, 180.56), (180.61, 195.31, 179.23, 194.23)),   # stuck 15 days
}
STUCK_AT = {"QCOM": SESSIONS[-12]}       # 2026-09-21 -> refreshed 15 calendar days later


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

    def _unproven_tail_days(self, df, symbol, interval, provider_name):
        # These tests pin the in-tree provisional-bar WRAPPER (``prov``) and the guard as it was.
        # The base class's own mtime self-heal (ohlcv_final_bars) would repair the same files before
        # either is reached; it is pinned in packages/providers/tests/test_ohlcv_no_unfinished_bars.py.
        return []


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


class _Prod(_Fixed):
    """THE PRODUCTION COMBINATION: the in-tree wrapper installed (``wire_all_seams``) AND the base class's
    mtime self-heal enabled (``_Provider`` switches it off to pin the wrapper alone)."""

    def _unproven_tail_days(self, df, symbol, interval, provider_name):
        return MarketDataProviderInterface._unproven_tail_days(self, df, symbol, interval, provider_name)


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


def _ny_epoch(day, hour, minute=0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY_TZ).timestamp()


def _write(symbol, cached, stamp=(9, 31), stamp_day=None) -> str:
    """Write the cache and set its mtime (the provenance proof): ``stamp`` = NY (hour, minute) on the
    cache's newest bar date (or ``stamp_day``); None leaves the real (now) mtime."""
    out = cached.copy()
    out["effective_date"] = out["Date"]
    native_cache.write_timeseries(PROVIDER, symbol, "1d", out)
    path = native_cache.find_timeseries_path(PROVIDER, symbol, "1d")
    if stamp is not None:
        day = stamp_day or pd.Timestamp(cached["Date"].max()).date()
        t = _ny_epoch(day, *stamp)
        os.utime(path, (t, t))
    return path


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
    stuck = STUCK_AT.get(symbol, PROV_DAY)
    vendor, cached = _world(symbol, cbar, vbar, level=vbar[3], cache_end=stuck, day=stuck)
    path = _write(symbol, cached)
    before = pd.read_parquet(path)
    n_new = len([d for d in SESSIONS if d > stuck])

    # the guard as it was: refuses (the bug)
    with pytest.raises(OHLCVTopUpRefused, match="disagrees"):
        _topup(_Plain(vendor), symbol)
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    logged["error"].clear()
    activity.clear()

    out = _topup(_Fixed(vendor), symbol)
    after = pd.read_parquet(path)
    row = after[after["Date"] == pd.Timestamp(stuck)].iloc[0]
    assert (row.Open, row.High, row.Low, row.Close) == pytest.approx(vbar)
    # every other previously cached bar is byte-for-byte what it was
    others = before[before["Date"] != pd.Timestamp(stuck)]
    pd.testing.assert_frame_equal(
        after[after["Date"].isin(others["Date"])].reset_index(drop=True), others.reset_index(drop=True))
    assert after["Date"].max() == pd.Timestamp(LAST) and len(after) == len(before) + n_new
    assert len(out) == len(after)
    infos = [m for m in logged["info"] if "provisional" in m and symbol in m]
    assert len(infos) == 1                                # ONE line per symbol
    # the guard's own refusal line precedes the repair (it must, to save the extra call); the repair
    # then says so, and nothing is reported to the Activity Log because the retry succeeds
    assert len(logged["error"]) == 1 and "REFUSED" in logged["error"][0]
    assert not activity


def test_exact_match_is_untouched_and_costs_no_extra_vendor_call(logged):
    symbol = "AMD"
    vendor = _truth(600.0)
    cached = vendor[vendor["Date"] <= pd.Timestamp(CACHE_END)].copy()
    path = _write(symbol, cached, stamp=(20, 30))
    fixed, plain = _Fixed(vendor), _Plain(vendor)
    _topup(plain, symbol)
    expect = _bytes(path)
    _write(symbol, cached, stamp=(20, 30))
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


@pytest.mark.parametrize("stamp,stamp_day", [
    ((9, 31), SESSIONS[-30]),     # file last written mid-session on ANOTHER day
    ((20, 30), None),             # written after 20:00 ET: a settled bar
    (None, None),                 # real mtime (today), not the bar's session
])
def test_a_settled_or_unproven_bar_still_refuses(stamp, stamp_day, activity):
    symbol = "AMD"
    old_end = SESSIONS[-40]
    cbar, vbar = CASES[symbol]
    vendor, cached = _world(symbol, cbar, vbar, level=vbar[3], cache_end=old_end, day=old_end)
    path = _write(symbol, cached, stamp=stamp, stamp_day=stamp_day)
    expect = _bytes(path)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), symbol)
    assert _bytes(path) == expect and len(activity) == 1


def test_probe_rebase_straddling_the_window_refuses_and_never_appends(activity):
    """A 5% rebase (spin-off) inside the newest-5 window + a snapshot-shaped older bar: the older
    bar must not be rewritten into agreement (that would APPEND onto a mixed basis)."""
    symbol = "AMD"
    vendor = _truth(600.0)
    cached = vendor[vendor["Date"] <= pd.Timestamp(CACHE_END)].copy().reset_index(drop=True)
    first_in_window = pd.Timestamp(SESSIONS[-7])          # ex-date is the next session
    pre = cached["Date"] <= first_in_window
    for col in ("Open", "High", "Low", "Close"):
        cached.loc[pre, col] = (cached.loc[pre, col] * 1.05).round(3)
    k = cached.index[cached["Date"] == first_in_window][0]     # that bar: snapshot shape
    cached.loc[k, ["Open", "High"]] = cached.loc[k, "High"]
    cached.loc[k, ["Low", "Close"]] = cached.loc[k, "Low"]
    path = _write(symbol, cached)
    expect = _bytes(path)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), symbol)
    assert _bytes(path) == expect


def test_probe_settled_older_bar_three_percent_off_refuses(activity):
    symbol = "AMD"
    vendor = _truth(600.0)
    cached = vendor[vendor["Date"] <= pd.Timestamp(CACHE_END)].copy().reset_index(drop=True)
    i = cached.index[cached["Date"] == pd.Timestamp(SESSIONS[-5])][0]
    h, lo = cached.loc[i, "High"], cached.loc[i, "Low"]
    cached.loc[i, ["Open", "High"]] = [h * 1.03, h * 1.03]        # snapshot shape, 3% off the vendor
    cached.loc[i, "Close"] = lo * 1.03
    cached.loc[i, "Low"] = lo * 1.03
    path = _write(symbol, cached)                                  # newest bar equals the vendor's
    expect = _bytes(path)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), symbol)
    assert _bytes(path) == expect


def test_mod_a_real_rebase_with_a_flat_newest_bar_still_refuses(activity):
    """MOD 2026-10-05: every window bar ~9% above the vendor's, the newest a flat 195.1."""
    symbol = "MOD"
    vendor = _truth(165.0)
    cached = vendor[vendor["Date"] <= pd.Timestamp(CACHE_END)].copy().reset_index(drop=True)
    tail = cached.index[-5:]
    for col in ("Open", "High", "Low", "Close"):
        cached.loc[tail, col] = (cached.loc[tail, col] * 1.09).round(3)
    cached.loc[cached.index[-1], ["Open", "High", "Low", "Close"]] = 195.1
    path = _write(symbol, cached)
    expect = _bytes(path)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), symbol)
    assert _bytes(path) == expect


def test_a_refused_symbol_does_not_cost_an_extra_vendor_call_within_the_memo(activity):
    symbol = "AMD"
    vendor, cached = _settled_mismatch_world(symbol)
    _write(symbol, cached)
    p = _Fixed(vendor)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(p, symbol)
    calls = len(p.impl_calls)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(p, symbol)
    assert len(p.impl_calls) == calls


def test_only_daily_bars_are_wrapped(monkeypatch):
    # daily-labelled frames stand in for weekly/monthly ones here; with the clock moved on, every
    # bar is final under the finality rule too, so only the WRAPPER's interval gate is under test
    from ba2_common.core import ohlcv_final_bars
    monkeypatch.setattr(ohlcv_final_bars, "now_utc", lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    seen = []
    monkeypatch.setattr(prov, "repair_provisional_bars", lambda *a, **k: seen.append(1) or (a[1], []))
    vendor, cached = _settled_mismatch_world("AMD")           # the guard refuses -> repair would run
    p = _Fixed(vendor)
    for interval in ("1wk", "1mo"):
        with pytest.raises(Exception):
            p._verified_tail_topup(cached, "AMD", interval, PROVIDER, datetime.now())
    assert seen == []
    with pytest.raises(OHLCVTopUpRefused):
        p._verified_tail_topup(cached, "AMD", "1d", PROVIDER, datetime.now())
    assert seen == [1]


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
    # the flag is required for EVERY run, dry runs and any cache folder (dev / prod / opt included)
    for argv in (["--db-file", "x"], ["--cache-folder", bcfg.CACHE_FOLDER, "--db-file", "x"],
                 ["--cache-folder", bcfg.CACHE_FOLDER, "--apply"]):
        assert tool.main(argv, provider_factory=lambda: _Provider(_truth(1.0))) == 2
        assert "REFUSED" in capsys.readouterr().err
    vendor = _seed_two_symbols(tmp_path)
    paths = {s: native_cache.find_timeseries_path(PROVIDER, s, "1d") for s in ("AMD", "CLEAN")}
    before = {s: _bytes(p) for s, p in paths.items()}
    rc = tool.main(["--cache-folder", bcfg.CACHE_FOLDER, "--i-know-the-apps-are-stopped"], provider_factory=lambda: _Provider(vendor))
    out = capsys.readouterr().out
    assert rc == 0 and "AMD: 1 provisional" in out and "CLEAN" not in out and "DRY RUN" in out
    assert {s: _bytes(p) for s, p in paths.items()} == before


def test_tool_apply_replaces_only_the_provisional_bar(tmp_path, capsys):
    tool = _tool()
    vendor = _seed_two_symbols(tmp_path)
    paths = {s: native_cache.find_timeseries_path(PROVIDER, s, "1d") for s in ("AMD", "CLEAN")}
    clean_before = _bytes(paths["CLEAN"])
    amd_before = pd.read_parquet(paths["AMD"])
    rc = tool.main(["--cache-folder", bcfg.CACHE_FOLDER, "--apply", "--i-know-the-apps-are-stopped"], provider_factory=lambda: _Provider(vendor))
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


def test_tool_never_prints_the_api_key_and_survives_a_failing_write(tmp_path, capsys, monkeypatch):
    tool = _tool()
    vendor = _seed_two_symbols(tmp_path)
    secret = "SYNTH3T1CKEY0123456789"

    class _Boom(_Provider):
        def _get_ohlcv_data_impl(self, symbol, *a, **k):
            if symbol == "CLEAN":
                raise RuntimeError(f"401 Client Error for url: https://x.test/api/v3/hist?symbol=CLEAN&apikey={secret}")
            return super()._get_ohlcv_data_impl(symbol, *a, **k)

    real_write = native_cache.write_timeseries
    monkeypatch.setattr(native_cache, "write_timeseries",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("replace failed")))
    # CLEAN is not stamped mid-session -> no vendor call; stamp it so the vendor error path runs
    path = native_cache.find_timeseries_path(PROVIDER, "CLEAN", "1d")
    t = _ny_epoch(pd.Timestamp(pd.read_parquet(path)["Date"].max()).date(), 9, 31)
    os.utime(path, (t, t))
    rc = tool.main(["--cache-folder", bcfg.CACHE_FOLDER, "--apply", "--i-know-the-apps-are-stopped"],
                   provider_factory=lambda: _Boom(vendor))
    monkeypatch.setattr(native_cache, "write_timeseries", real_write)
    cap = capsys.readouterr()
    assert secret not in cap.out and secret not in cap.err
    assert "CLEAN: ERROR RuntimeError" in cap.out and "AMD: ERROR OSError" in cap.out   # scan continued
    assert rc == 1


# --------------------------------------------------------------------------- F1 / F2
def test_normal_topup_is_one_vendor_call_and_a_repair_is_two(activity):
    symbol = "AMD"
    vendor = _truth(600.0)
    cached = vendor[vendor["Date"] <= pd.Timestamp(CACHE_END)].copy().reset_index(drop=True)
    _write(symbol, cached)                                       # mid-session stamp, bar equals vendor
    plain, fixed = _Plain(vendor), _Fixed(vendor)
    _topup(plain, symbol)
    assert len(plain.impl_calls) == 1
    _write(symbol, cached)
    _topup(fixed, symbol)
    assert len(fixed.impl_calls) == 1                             # the proof gate adds NO call
    cbar, vbar = CASES["AMD"]
    vendor2, cached2 = _world(symbol, cbar, vbar, level=vbar[3])
    _write(symbol, cached2)
    rep = _Fixed(vendor2)
    _topup(rep, symbol)
    assert len(rep.impl_calls) == 3                               # refused (1) + probe (1) + retry (1)


def test_refusal_of_a_settled_bar_costs_only_the_guards_own_call(activity):
    symbol = "AMD"
    vendor, cached = _settled_mismatch_world(symbol)
    _write(symbol, cached, stamp=(20, 30))
    p = _Fixed(vendor)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(p, symbol)
    assert len(p.impl_calls) == 1 and len(activity) == 1


def test_a_write_before_20_et_counts_as_provisional_after_20_et_does_not(activity):
    symbol = "FSLR"
    cbar, vbar = CASES[symbol]
    vendor, cached = _world(symbol, cbar, vbar, level=vbar[3])
    for stamp, ok in (((17, 0), True), ((19, 59), True), ((20, 0), False)):
        MarketDataProviderInterface._TOPUP_REFUSED.clear()
        prov._REFUSALS_LOGGED.clear()
        _write(symbol, cached, stamp=stamp)
        if ok:
            _topup(_Fixed(vendor), symbol)
        else:
            with pytest.raises(OHLCVTopUpRefused):
                _topup(_Fixed(vendor), symbol)


def test_tool_skips_a_bar_dated_today_in_new_york(capsys):
    tool = _tool()
    today = datetime.now(NY_TZ).date()
    days = [today - timedelta(days=i) for i in (3, 2, 1, 0)]
    vendor = pd.DataFrame({"Date": pd.to_datetime(days), "Open": [10.0, 10.0, 10.0, 10.0],
                           "High": [11.0, 11.0, 11.0, 11.0], "Low": [9.0, 9.0, 9.0, 9.0],
                           "Close": [10.5, 10.5, 10.5, 10.5], "Volume": [1000] * 4})
    cached = vendor.copy()
    cached.loc[3, ["Open", "High", "Low", "Close"]] = [10.9, 10.9, 9.5, 9.5]
    _write("TODAYX", cached)                                      # stamped 09:31 on today's bar
    rc = tool.main(["--cache-folder", bcfg.CACHE_FOLDER, "--apply", "--i-know-the-apps-are-stopped"],
                   provider_factory=lambda: _Provider(vendor))
    out = capsys.readouterr().out
    assert rc == 0 and "TODAYX" not in out and "0 with stuck" in out


def test_tool_help_documents_the_per_app_commands(capsys):
    tool = _tool()
    with pytest.raises(SystemExit):
        tool.main(["--help"])
    h = capsys.readouterr().out
    for needle in ("ba2_trade_platform-prod", "ba2_trade_platform-opt", "SHARED with the test platform",
                   "before 15:30", "--symbols"):
        assert needle in h


# --------------------------------------------------------------------------- two stale snapshots
# Real opt-cache (2026-09-28 15:3x Paris write) vs FMP numbers, field failure on APP 1231:
# the 09-28 top-up appended its bar without refreshing the 09-25 snapshot taken the session before.
# date -> (O, H, L, C, Volume)
REAL = {
    "AMD": {
        "cached": {"2026-09-25": (634.535, 635.24, 631.21, 634.885, 1396608),
                   "2026-09-28": (635.068, 635.068, 623.78, 623.78, 709375)},
        "vendor": {"2026-09-25": (634.54, 639.00, 625.52, 630.63, 17632100),
                   "2026-09-28": (624.90, 629.75, 596.07, 607.87, 22124100),
                   "2026-09-29": (616.74, 624.13, 605.25, 607.57, 16513900)}},
    "INTC": {
        "cached": {"2026-09-25": (126.83, 126.885, 125.96, 125.96, 4934944),
                   "2026-09-28": (126.88, 126.88, 120.655, 120.655, 4939383)},
        "vendor": {"2026-09-25": (126.83, 126.93, 122.84, 123.00, 97728429),
                   "2026-09-28": (120.68, 121.71, 114.71, 116.03, 112436918),
                   "2026-09-29": (116.70, 118.75, 113.97, 115.93, 88744533)}},
    "MU": {
        "cached": {"2026-09-25": (1095.83, 1095.83, 1091.24, 1093.13, 954807),
                   "2026-09-28": (1095.475, 1095.475, 1074.73, 1076.77, 1111995)},
        "vendor": {"2026-09-25": (1095.83, 1108.72, 1073.00, 1082.28, 20947931),
                   "2026-09-28": (1075.98, 1084.81, 1032.00, 1053.98, 22227200),
                   "2026-09-29": (1076.69, 1082.66, 1057.70, 1065.08, 19723107)}},
}
D25, D28 = pd.Timestamp("2026-09-25"), pd.Timestamp("2026-09-28")


def _real_world(symbol):
    r = REAL[symbol]
    vendor = _truth(r["vendor"]["2026-09-29"][3])
    cached = None
    for src, frame in (("vendor", vendor),):
        for d, row in r[src].items():
            i = frame.index[frame["Date"] == pd.Timestamp(d)][0]
            frame.loc[i, ["Open", "High", "Low", "Close", "Volume"]] = row
    cached = vendor[vendor["Date"] <= D28].copy().reset_index(drop=True)
    for d, row in r["cached"].items():
        i = cached.index[cached["Date"] == pd.Timestamp(d)][0]
        cached.loc[i, ["Open", "High", "Low", "Close", "Volume"]] = row
    return vendor, cached


@pytest.mark.parametrize("symbol", sorted(REAL))
def test_two_stale_snapshots_are_both_repaired(symbol, activity):
    vendor, cached = _real_world(symbol)
    path = _write(symbol, cached, stamp_day=D28.date())
    before = pd.read_parquet(path)
    with pytest.raises(OHLCVTopUpRefused):                                # the field failure
        _topup(_Plain(vendor), symbol)
    MarketDataProviderInterface._TOPUP_REFUSED.clear()
    activity.clear()
    _topup(_Fixed(vendor), symbol)
    after = pd.read_parquet(path)
    for d in (D25, D28):
        got = after[after["Date"] == d].iloc[0]
        want = vendor[vendor["Date"] == d].iloc[0]
        assert (got.Open, got.High, got.Low, got.Close, got.Volume) == pytest.approx(
            (want.Open, want.High, want.Low, want.Close, want.Volume))
    keep = ~before["Date"].isin([D25, D28])
    pd.testing.assert_frame_equal(after[after["Date"].isin(before.loc[keep, "Date"])].reset_index(drop=True),
                                  before[keep].reset_index(drop=True))
    assert after["Date"].max() == pd.Timestamp(LAST) and not activity


def test_an_older_low_volume_bar_that_is_not_contained_refuses_and_lists_every_bar(activity):
    vendor, cached = _real_world("AMD")
    i = cached.index[cached["Date"] == D25][0]
    cached.loc[i, "High"] = 650.0                         # low volume, but above the vendor's high
    path = _write("AMD", cached, stamp_day=D28.date())
    expect = _bytes(path)
    with pytest.raises(OHLCVTopUpRefused) as e:
        _topup(_Fixed(vendor), "AMD")
    assert _bytes(path) == expect
    msg = str(e.value)
    assert "2026-09-25" in msg and "2026-09-28" in msg and "every bar of the newest" in msg
    assert len(activity) == 1 and {b["date"] for b in activity[0]["data"]["bars"]} == {"2026-09-25", "2026-09-28"}


def test_an_older_contained_bar_with_normal_volume_refuses(activity):
    vendor, cached = _real_world("AMD")
    i = cached.index[cached["Date"] == D25][0]
    cached.loc[i, "Volume"] = vendor.loc[i, "Volume"]     # a settled bar that merely differs
    path = _write("AMD", cached, stamp_day=D28.date())
    expect = _bytes(path)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), "AMD")
    assert _bytes(path) == expect


def test_a_rebased_low_volume_older_bar_refuses(activity):
    vendor, cached = _real_world("AMD")
    i = cached.index[cached["Date"] == D25][0]
    for col in ("Open", "High", "Low", "Close"):
        cached.loc[i, col] = round(vendor.loc[i, col] * 1.05, 3)    # rebased: scaled, volume tiny
    path = _write("AMD", cached, stamp_day=D28.date())
    expect = _bytes(path)
    with pytest.raises(OHLCVTopUpRefused):
        _topup(_Fixed(vendor), "AMD")
    assert _bytes(path) == expect


@pytest.mark.parametrize("symbol", sorted(CASES))
def test_production_combination_wrapper_plus_heal_gives_the_same_file_as_the_wrapper_alone(symbol, logged, activity):
    """F6: with BOTH defences active the stuck bar is replaced by the vendor's final bar exactly once,
    every other bar is byte-for-byte what it was, and the result equals the wrapper-only result."""
    cbar, vbar = CASES[symbol]
    stuck = STUCK_AT.get(symbol, PROV_DAY)
    vendor, cached = _world(symbol, cbar, vbar, level=vbar[3], cache_end=stuck, day=stuck)
    path = _write(symbol, cached)
    before = pd.read_parquet(path)
    _topup(_Fixed(vendor), symbol)
    wrapper_only = pd.read_parquet(path)

    path = _write(symbol, cached)                           # back to the contaminated file, mtime mid-session
    logged["error"].clear()
    activity.clear()
    _topup(_Prod(vendor), symbol)
    both = pd.read_parquet(path)

    pd.testing.assert_frame_equal(both, wrapper_only)
    row = both[both["Date"] == pd.Timestamp(stuck)].iloc[0]
    assert (row.Open, row.High, row.Low, row.Close) == pytest.approx(vbar)
    others = before[before["Date"] != pd.Timestamp(stuck)]
    pd.testing.assert_frame_equal(both[both["Date"].isin(others["Date"])].reset_index(drop=True), others.reset_index(drop=True))
    assert not any("REFUSED" in m for m in logged["error"]) and not activity      # healed: the guard never refused
