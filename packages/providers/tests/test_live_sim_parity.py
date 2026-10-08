"""PARITY of the backtest's screener simulation (``ba2_providers.screener.live_sim``) with the LIVE screener.

Part (i) of the permanent parity test: hermetic, synthetic worlds.  The live ``StockScreener.screen()`` (``as_of``
None) is run end to end against a FAKE VENDOR (HTTP faked at ``fmp_http_get``: the screener endpoint, ``/quote``,
``historical-price-full``, the bulk float table), and the simulation is run on the SAME data; the two ordered lists
must be identical.  Below that, each criterion is pinned on its own: live's ``_quotes_from_bars`` /
``_filter_by_price_drop`` / ``_filter_by_weinstein_stage2`` against the scalar equivalents, and the vectorised panel
against the scalar equivalents.  Live is not modified by this work: any change to live's criteria fails here.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import ba2_providers
import ba2_providers.StockScreener as S
from ba2_providers.screener import float_filter as ff
from ba2_providers.screener import live_sim as ls

DAY = "2026-03-12"          # the morning being screened (a Thursday)


# ----------------------------------------------------------------------------------- synthetic world
def _sessions(n=330, end=DAY):
    days = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    return [d.strftime("%Y-%m-%d") for d in days]


class World:
    def __init__(self, seed: int, n_sym: int = 45, holes: bool = False):
        rng = np.random.default_rng(seed)
        self.rng = rng
        self.sessions = _sessions()                       # last one = DAY (no bar for DAY)
        self.symbols = [f"S{i:02d}" for i in range(n_sym)]
        T = len(self.sessions)
        self.bars = {}
        self.shares = {}
        self.floats = {}
        self.open_now = {}
        for sym in self.symbols:
            drift = rng.normal(0.0004, 0.0006)
            ret = rng.normal(drift, rng.uniform(0.01, 0.035), T - 1)
            close = 20 * np.exp(np.cumsum(ret)) * rng.uniform(0.5, 6)
            op = close * (1 + rng.normal(0, 0.004, T - 1))
            hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.01, T - 1)))
            lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.01, T - 1)))
            vol = np.round(rng.lognormal(13, 0.6, T - 1) * (1 + 3 * (rng.random(T - 1) < 0.04))).astype(float)
            idx = np.arange(T - 1)
            if holes:
                keep = rng.random(T - 1) > 0.03
                idx, op, hi, lo, close, vol = idx[keep], op[keep], hi[keep], lo[keep], close[keep], vol[keep]
            self.bars[sym] = (idx, op, hi, lo, close, vol)
            self.shares[sym] = float(rng.uniform(2e7, 4e8))
            self.floats[sym] = float(self.shares[sym] * rng.uniform(0.3, 0.95)) if rng.random() < 0.8 else None
            last_close = close[-1]
            self.open_now[sym] = round(float(last_close * (1 + rng.normal(-0.01, 0.025))), 2)
        # tick-round the bars like a vendor (2-4 decimals) so the borderline paths are exercised
        for sym in self.symbols:
            i, o, h, l, c, v = self.bars[sym]
            self.bars[sym] = (i, np.round(o, 2), np.round(h, 2), np.round(l, 2), np.round(c, 2), v)

    def history(self, sym):
        i, o, h, l, c, v = self.bars[sym]
        return [{"date": self.sessions[k], "open": float(o[j]), "high": float(h[j]), "low": float(l[j]),
                 "close": float(c[j]), "volume": int(v[j])} for j, k in enumerate(i)]

    def forming(self, sym):
        px = self.open_now[sym]
        return {"date": DAY, "open": px, "high": px, "low": px, "close": px, "volume": 12345}

    def prev_close(self, sym):
        return float(self.bars[sym][4][-1])

    def panel(self):
        S_, T = len(self.symbols), len(self.sessions)
        sh = np.zeros((S_, T)); fl = np.full((S_, T), np.nan)
        for s, sym in enumerate(self.symbols):
            sh[s] = self.shares[sym]
            if self.floats[sym]:
                fl[s] = self.floats[sym]
        arrays = ls.build_panel_arrays(self.bars, self.sessions, sh, self.symbols, fl=fl)
        return ls.DailyPanel(self.symbols, self.sessions, arrays, {})

    def now_fn(self):
        arr = np.array([self.open_now[s] for s in self.symbols])
        return lambda idx: (arr[idx], arr[idx])


# --------------------------------------------------------------------------------------- fake vendor
class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def install_vendor(monkeypatch, world: World):
    import ba2_providers.fmp_common as fc
    import importlib, sys
    importlib.import_module('ba2_providers.screener.FMPScreenerProvider')
    prov_mod = sys.modules['ba2_providers.screener.FMPScreenerProvider']   # the package re-exports the CLASS under that name

    def fake_get(url, params=None, endpoint=None, timeout=None, **kw):
        params = params or {}
        if url.endswith("/stock-screener"):
            lo = params.get("marketCapMoreThan", 0)
            hi = params.get("marketCapLowerThan", float("inf"))
            plo = params.get("priceMoreThan", 0)
            phi = params.get("priceLowerThan", float("inf"))
            assert "volumeMoreThan" not in params and "floatSharesUnder" not in params
            rows = []
            for sym in world.symbols:
                cap = world.prev_close(sym) * world.shares[sym]
                px = world.open_now[sym]
                if lo <= cap <= hi and plo <= px <= phi:
                    rows.append({"symbol": sym, "companyName": sym, "marketCap": cap, "price": px,
                                 "volume": 1234, "exchangeShortName": "NYSE", "isActivelyTrading": True})
            rows.sort(key=lambda r: -r["marketCap"])
            return _Resp(rows)
        if "/quote/" in url:
            out = []
            for sym in url.rsplit("/", 1)[1].split(","):
                if sym in world.open_now:
                    out.append({"symbol": sym, "price": world.open_now[sym],
                                "marketCap": world.prev_close(sym) * world.shares[sym]})
            return _Resp(out)
        if "/historical-price-full/" in url:
            syms = url.rsplit("/", 1)[1].split(",")
            lo, hi = params["from"], params["to"]
            # FMP returns the session's FORMING bar (dated today, close = the current price) at ~09:31
            lst = [{"symbol": s, "historical": list(reversed([b for b in world.history(s) + [world.forming(s)]
                                                              if lo <= b["date"] <= hi]))}
                   for s in syms if s in world.bars]
            return _Resp({"historicalStockList": lst} if len(lst) != 1 else lst[0])
        raise AssertionError(f"unexpected vendor call {url}")

    monkeypatch.setattr(fc, "fmp_http_get", fake_get)
    monkeypatch.setattr(fc, "fmp_live_cache_enabled", lambda: False)
    # the live history cache is keyed by (symbol, window) and would serve a PREVIOUS synthetic world
    monkeypatch.setattr(fc, "fmp_live_cache_get", lambda key: fc.FMP_LIVE_CACHE_MISS)
    monkeypatch.setattr(fc, "fmp_live_cache_put", lambda key, value: None)
    monkeypatch.setattr(fc, "fmp_live_cached", lambda key, fn, ttl_seconds=0: fn())
    monkeypatch.setattr(prov_mod, "get_app_setting", lambda k: "dummy", raising=False)
    provider = prov_mod.FMPScreenerProvider()
    provider.api_key = "dummy"
    monkeypatch.setattr(ba2_providers, "get_provider", lambda cat, name, **kw: provider)
    monkeypatch.setattr(S, "get_app_setting", lambda k: "dummy", raising=False)
    monkeypatch.setattr(ff, "get_app_setting", lambda k: "dummy", raising=False)
    table = {s: f for s, f in world.floats.items() if f}
    monkeypatch.setattr(ff, "load_float_table", lambda: table)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 3, 12, 14, 31, tzinfo=timezone.utc)

    monkeypatch.setattr(S, "datetime", _DT)


def run_live(world: World, settings: dict):
    return [r["symbol"] for r in S.StockScreener({f"screener_{k}": v for k, v in settings.items()}).screen()["results"]]


def sim_settings(settings: dict) -> dict:
    return {k: v for k, v in settings.items()}


def random_settings(rng: random.Random, world: World) -> dict:
    caps = sorted(world.prev_close(s) * world.shares[s] for s in world.symbols)
    lo = rng.choice(caps[: len(caps) // 2])
    st = {
        "market_cap_min": lo,
        "market_cap_max": rng.choice([0, caps[-1] * 1.01, rng.choice(caps[len(caps) // 2:])]),
        "price_min": rng.choice([0, 0, 10.0, 25.0]),
        "price_max": rng.choice([0, 0, 150.0, 400.0]),
        "volume_min": rng.choice([0, 0, 300_000.0, 1_000_000.0]),
        "volume_max": rng.choice([0, 0, 1_500_000.0, 6_000_000.0]),
        "float_min": rng.choice([0, 0, 2e7, 8e7]),
        "float_max": rng.choice([0, 0, 1.5e8, 3e8]),
        "relative_volume_min": rng.choice([0, 0.0, 0.6, 1.0, 1.3, 1.5, 2.0]),
        "price_drop_pct": rng.choice([0, 3.0, 6.0, 10.0, 13.0, 20.0]),
        "price_drop_days": rng.choice([2, 3, 5, 8, 10, 13, 18, 22, 30]),
        "weinstein_stage2_only": rng.choice([0, 0, 1]),
        "max_stocks": rng.choice([3, 5, 10, 20, 40]),
    }
    return st


# ------------------------------------------------------------------ end to end: StockScreener.screen()
@pytest.mark.parametrize("seed", range(60))
def test_simulation_equals_live_screen_end_to_end(seed, monkeypatch):
    world = World(seed)
    install_vendor(monkeypatch, world)
    rng = random.Random(seed)
    settings = random_settings(rng, world)
    live = run_live(world, settings)
    panel = world.panel()
    sim = panel.select(DAY, settings, ls.POST_FIX, now=world.now_fn())
    assert sim == live, f"seed {seed}: {settings}\n live {live}\n  sim {sim}"
    test_simulation_equals_live_screen_end_to_end.total = getattr(test_simulation_equals_live_screen_end_to_end, "total", 0) + len(live)


def test_end_to_end_is_not_vacuous(monkeypatch):
    """The random worlds must select something often, or the test above proves nothing."""
    nonempty = 0
    for seed in range(60):
        world = World(seed)
        install_vendor(monkeypatch, world)
        settings = random_settings(random.Random(seed), world)
        if run_live(world, settings):
            nonempty += 1
    assert nonempty >= 15, nonempty


# ------------------------------------------------------------------------- criterion by criterion
def _live_history_patch(monkeypatch, world: World):
    install_vendor(monkeypatch, world)


@pytest.mark.parametrize("seed", range(12))
def test_rvol_scalar_equals_live_quotes_from_bars(seed, monkeypatch):
    world = World(seed, n_sym=25, holes=True)
    install_vendor(monkeypatch, world)
    sc = S.StockScreener({})
    q = sc._quotes_from_bars(world.symbols)
    for sym in world.symbols:
        dates = [b["date"] for b in world.history(sym)]
        vols = [b["volume"] for b in world.history(sym)]
        res = ls.rvol_scalar(dates, vols, DAY)
        live = q.get(sym)
        if live is None:
            assert res is None
            continue
        last_vol, avg, rvol = res
        assert (last_vol, avg) == (float(live["volume"]), live["avgVolume"])
        assert rvol == (round(live["volume"] / live["avgVolume"], 2) if live["avgVolume"] > 0 else 0.0)


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("n", [2, 5, 9, 13, 22, 30])
def test_drop_scalar_equals_live_filter_by_price_drop(seed, n, monkeypatch):
    world = World(seed, n_sym=25, holes=True)
    install_vendor(monkeypatch, world)
    sc = S.StockScreener({"screener_price_drop_days": n})
    cands = [{"symbol": s, "price": world.open_now[s]} for s in world.symbols]
    out, _ = sc._filter_by_price_drop(cands, min_drop_pct=-1e9, max_results=10 ** 6)
    live = {c["symbol"]: c["price_drop_pct"] for c in out}
    for sym in world.symbols:
        h = world.history(sym)
        res = ls.drop_scalar([b["date"] for b in h], [b["high"] for b in h], [b["low"] for b in h],
                             [b["close"] for b in h], DAY, n, world.open_now[sym], forming=world.open_now[sym])
        assert res == live.get(sym)


@pytest.mark.parametrize("seed", range(10))
def test_weinstein_scalar_equals_live_filter(seed, monkeypatch):
    world = World(seed, n_sym=30, holes=True)
    install_vendor(monkeypatch, world)
    sc = S.StockScreener({})
    out, _ = sc._filter_by_weinstein_stage2([{"symbol": s} for s in world.symbols])
    live = {c["symbol"] for c in out}
    for sym in world.symbols:
        h = world.history(sym)
        assert ls.weinstein_scalar([b["date"] for b in h], [b["close"] for b in h], DAY,
                                   forming=world.open_now[sym]) == (sym in live)


# ---------------------------------------------------- the vectorised panel equals the scalar definitions
@pytest.mark.parametrize("seed", range(8))
def test_panel_columns_equal_scalar_definitions(seed):
    world = World(seed, n_sym=20, holes=True)
    panel = world.panel()
    p = panel.pos(DAY)
    # also a few earlier mornings
    for pp in (p, p - 1, p - 7, p - 40, p - 100):
        day = panel.sessions[pp]
        for s, sym in enumerate(world.symbols):
            h = world.history(sym)
            dates = [b["date"] for b in h]
            res = ls.rvol_scalar(dates, [b["volume"] for b in h], day)
            if res is None:
                assert np.isnan(panel.arrays["rvol"][pp, s])
            else:
                assert (panel.arrays["lv"][pp, s], panel.arrays["avg20"][pp, s], panel.arrays["rvol"][pp, s]) == res
            assert bool(panel.arrays["w2"][pp, s]) == ls.weinstein_scalar(dates, [b["close"] for b in h], day)
            # the forming-bar variant at several prices (around the SMA too)
            for x in (world.open_now[sym], world.open_now[sym] * 1.1, world.open_now[sym] * 0.9, 25.0, 60.0, 120.0):
                got = bool(ls.weinstein_forming_pass(panel.arrays["wa"][pp, s:s + 1], panel.arrays["wp"][pp, s:s + 1],
                                                     np.array([x]))[0])
                assert got == ls.weinstein_scalar(dates, [b["close"] for b in h], day, forming=x), (sym, day, x)


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("n", [2, 4, 7, 10, 12, 13, 18, 22, 30])
def test_panel_peak_equals_scalar_drop(seed, n):
    world = World(seed, n_sym=20, holes=False)        # on the common grid the trim is exact
    panel = world.panel()
    pk = panel.peak(n)
    for pp in (panel.pos(DAY), panel.pos(DAY) - 3, panel.pos(DAY) - 50):
        day = panel.sessions[pp]
        for s, sym in enumerate(world.symbols):
            h = world.history(sym)
            now = world.open_now[sym]
            res = ls.drop_scalar([b["date"] for b in h], [b["high"] for b in h], [b["low"] for b in h],
                                 [b["close"] for b in h], day, n, now, forming=now)
            peak = np.fmax(pk[s, pp], now)          # the forming bar's high is part of the peak
            assert res is not None
            assert round(((peak - now) / peak) * 100, 2) == res


def test_float_mask_equals_live_apply_float_filter():
    rng = np.random.default_rng(0)
    for _ in range(200):
        fl = np.where(rng.random(40) < 0.25, np.nan, rng.uniform(1e6, 5e8, 40))
        fl[rng.random(40) < 0.1] = 0.0
        fmin = float(rng.choice([0, 5e7, 1e8])); fmax = float(rng.choice([0, 2e8, 4e8]))
        cands = [{"symbol": f"X{i}"} for i in range(40)]
        table = {f"X{i}": float(v) for i, v in enumerate(fl) if np.isfinite(v) and v > 0}
        kept, _ = ff.apply_float_filter(cands, table, fmin, fmax)
        mask = ls.float_mask(ls.POST_FIX, fl, fmin, fmax)
        assert [c["symbol"] for c in kept] == [f"X{i}" for i in np.flatnonzero(mask)]


def test_py_round2_matches_python_round():
    rng = np.random.default_rng(1)
    x = np.concatenate([rng.uniform(-50, 200, 20000), (rng.integers(0, 20000, 5000) + 0.5) / 100,
                        rng.integers(0, 100000, 5000) / 1000])
    assert [round(float(v), 2) for v in x] == list(ls.py_round2(x))


# ---------------------------------------------------------------- order, ties, cut, edge behaviours
def _cols(n, **over):
    base = dict(symbols=np.array([f"T{i}" for i in range(n)]), shares=np.full(n, 1e6), last_close=np.full(n, 100.0),
                rvol=np.full(n, 2.0), last_vol=np.full(n, 1e6), avg20=np.full(n, 1e6), w2=np.ones(n, bool),
                fl=np.full(n, np.nan), peak=None)
    base.update(over)
    return base


def test_tie_at_the_cut_is_symbol_ascending_and_stable():
    n = 8
    # five names with EXACTLY equal market cap straddle a cut of 3, plus larger ones
    syms = np.array(["Z", "A", "M", "B", "Q", "BIG1", "BIG2", "C"])
    close = np.array([100, 100, 100, 100, 100, 150, 140, 100.0])
    sel = ls.select_from_columns(**_cols(n, symbols=syms, last_close=close), now=close,
                                 settings={"max_stocks": 4, "relative_volume_min": 1.0}, beh=ls.POST_FIX)
    assert [str(syms[i]) for i in sel] == ["BIG1", "BIG2", "A", "B"]
    # the superset/prune call (cut=False) lists the SAME order, so a cut of any size is a prefix of it
    full = ls.select_from_columns(**_cols(n, symbols=syms, last_close=close), now=close,
                                  settings={"max_stocks": 4, "relative_volume_min": 1.0}, beh=ls.POST_FIX, cut=False)
    assert [str(syms[i]) for i in full][:4] == ["BIG1", "BIG2", "A", "B"] and len(full) == 8


def test_rvol_zero_runs_volume_filters_post_fix_and_skips_them_pre_fix():
    n = 4
    avg = np.array([100.0, 5_000.0, 50_000.0, 900_000.0])
    cols = _cols(n, avg20=avg, last_vol=np.full(n, 10.0))
    st = {"relative_volume_min": 0, "volume_min": 1_000.0, "volume_max": 100_000.0}
    post = ls.select_from_columns(**cols, now=np.full(n, 100.0), settings=st, beh=ls.POST_FIX)
    assert sorted(post) == [1, 2]                                     # avg floor 1k, ceiling 100k
    pre = ls.select_from_columns(**cols, now=np.full(n, 100.0), settings={**st, "volume_min": 0},
                                 beh=ls.LEGACY_CURRENT, vol_today=lambda idx: np.zeros(idx.size))
    assert sorted(pre) == [0, 1, 2, 3]                                # stage skipped at rvol_min 0: volume_max not applied


def test_legacy_volume_min_is_the_session_volume_so_far():
    n = 3
    cols = _cols(n)
    got = ls.select_from_columns(**cols, now=np.full(n, 100.0), settings={"volume_min": 50_000.0},
                                 beh=ls.LEGACY_CURRENT, vol_today=lambda idx: np.array([0.0, 60_000.0, 10.0])[idx])
    assert list(got) == [1]
    with pytest.raises(ls.SimulationRefusal):
        ls.select_from_columns(**cols, now=np.full(n, 100.0), settings={"volume_min": 50_000.0}, beh=ls.LEGACY_CURRENT)


def test_unsupported_settings_refuse():
    cols = _cols(2)
    for bad in ({"sort_metric": "composite"}, {"dollar_volume_min": 1e6}):
        with pytest.raises(ls.SimulationRefusal):
            ls.select_from_columns(**cols, now=np.full(2, 1.0), settings=bad, beh=ls.POST_FIX)


def test_split_basis_market_cap_and_price_thresholds_use_the_as_traded_figures():
    """The OHLCV cache is split-adjusted as of its fetch, the vendor's share count is the RAW figure as of the day:
    a 10:1 split LATER than the morning makes the raw price 10x the cached one and the raw cap = adjusted close x raw
    shares x 10.  ``fac`` (the as-traded factor, NaN = unknown calendar) carries it."""
    syms = np.array(["SPL", "PLAIN", "UNK"])
    cols = _cols(3, symbols=syms, shares=np.array([1e7, 1e8, 1e8]), last_close=np.array([100.0, 100.0, 100.0]),
                 fac=np.array([10.0, 1.0, np.nan]))
    now = np.array([100.0, 100.0, 100.0])
    # SPL: raw cap 100 x 10 x 1e7 = 1e10 (inside 5-10B inclusive); PLAIN: 1e10; UNK: unknown calendar -> not a candidate
    got = ls.select_from_columns(**cols, now=now, settings={"market_cap_min": 5e9, "market_cap_max": 1e10, "max_stocks": 9},
                                 beh=ls.POST_FIX)
    assert [str(syms[i]) for i in got] == ["PLAIN", "SPL"]
    # without the factor SPL would be a 1e9 name outside the band (the pre-fix bug)
    nofac = {k: v for k, v in cols.items() if k != "fac"}
    got0 = ls.select_from_columns(**nofac, now=now, settings={"market_cap_min": 5e9, "market_cap_max": 1e10, "max_stocks": 9},
                                  beh=ls.POST_FIX)
    assert [str(syms[i]) for i in got0] == ["PLAIN", "UNK"]
    # price ceiling compares the AS-TRADED price: SPL traded at 1000 raw, PLAIN at 100
    got2 = ls.select_from_columns(**cols, now=now, settings={"market_cap_min": 5e9, "price_max": 500.0, "max_stocks": 9},
                                  beh=ls.POST_FIX)
    assert [str(syms[i]) for i in got2] == ["PLAIN"]
