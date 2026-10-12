"""The backtest's DAILY screener gate (``screener_gate`` on the live-simulation panel): one selection function for the
gate, the per-trial prune and the static superset; ties; out-of-range genomes; panel refusals; job identity.

Synthetic worlds from ``packages/providers/tests/test_live_sim_parity.py`` (the same ones that pin the simulation to
live's ``StockScreener``)."""
from __future__ import annotations

import importlib.util
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[3]


def _load_world_module():
    spec = importlib.util.spec_from_file_location("live_sim_parity_worlds",
                                                  REPO / "packages/providers/tests/test_live_sim_parity.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_sim_parity_worlds"] = mod
    spec.loader.exec_module(mod)
    return mod


W = _load_world_module()

from ba2_providers.screener import live_sim as ls                      # noqa: E402
from ba2_providers.screener import universe_superset as us             # noqa: E402
from app.services.backtest import screener_gate as sg                  # noqa: E402

BEH = ls.POST_FIX
FULL = {"market_cap_min": 0, "market_cap_max": 0, "price_min": 0, "price_max": 0, "volume_min": 0, "volume_max": 0,
        "float_min": 0, "float_max": 0, "relative_volume_min": 0, "price_drop_pct": 0, "price_drop_days": 5,
        "max_stocks": 10, "sort_metric": "market_cap", "weinstein_stage2_only": 0}
RANGES = {  # the launcher's genes, narrowed to the synthetic world
    "screener_market_cap_min": {"min": 2e9, "max": 2e10, "step": 1e9, "type": "float", "optimize": True},
    "screener_relative_volume_min": {"min": 0.0, "max": 3.0, "step": 0.1, "type": "float", "optimize": True},
    "screener_price_drop_pct": {"min": 0.0, "max": 25.0, "step": 1.0, "type": "float", "optimize": True},
    "screener_price_drop_days": {"min": 2, "max": 12, "step": 1, "type": "int", "optimize": True},
    "screener_max_stocks": {"min": 10, "max": 50, "step": 10, "type": "int", "optimize": True},
    "screener_weinstein_stage2_only": {"min": 0, "max": 1, "step": 1, "type": "int", "optimize": True},
}


def _panel(seed=3, n_sym=40):
    world = W.World(seed, n_sym=n_sym)
    return world, world.panel()


def _genome(rng: random.Random):
    return {
        "market_cap_min": rng.choice([2e9, 4e9, 8e9, 1.5e10]),
        "relative_volume_min": round(rng.choice([0, 0.5, 1.0, 1.5, 2.5]), 1),
        "price_drop_pct": float(rng.choice([0, 2, 5, 9, 14, 20])),
        "price_drop_days": rng.choice([2, 4, 7, 10, 12]),
        "max_stocks": rng.choice([10, 20, 30, 50]),
        "weinstein_stage2_only": rng.choice([0, 1]),
    }


def test_gate_is_always_inside_the_prune_and_the_static_superset():
    """THE property: for random genomes inside the declared lattice and random decision-time prices, the daily gate's
    output is a subset of the per-trial prune, which is a subset of the static superset, on every day."""
    world, panel = _panel()
    start, end = panel.sessions[-40], panel.sessions[-2]
    ranges = us.gene_ranges_from_opt(RANGES)
    static = set(sg.static_universe(panel, start, end, {}, ranges, intraday=True))
    assert static
    rng = random.Random(7)
    lo = np.asarray(panel.arrays["l"]); hi = np.asarray(panel.arrays["h"])
    checked = nonempty = 0
    for _ in range(25):
        g = _genome(rng)
        prune = set(sg.prune_symbols(panel, start, end, g, None, intraday=True))
        assert prune <= static, sorted(prune - static)[:5]
        for day in sg.screen_days(panel, start, end, intraday=True):
            p = panel.pos(day)
            # any price inside the session's [low, high] may be "now" at some decision time T
            now = lo[p] + np.random.default_rng(p).random(len(panel.symbols)) * (hi[p] - lo[p])
            now = np.where(np.isfinite(now), now, np.nan)
            got = panel.select(day, g, BEH, now=now)
            got_b = panel.select(day, g, BEH, now=now, band_at_now=True)  # band on the price at T (decisions after the first bar)
            assert set(got_b) <= prune, ("band_at_now", day, g, sorted(set(got_b) - prune))
            checked += 1
            nonempty += bool(got)
            assert set(got) <= prune, (day, g, sorted(set(got) - prune))
    assert checked > 500 and nonempty > 50


def test_prune_is_the_gate_function_without_the_cut():
    """The prune calls the SAME selection (bounds mode, no cut): on a day where 'now' is the day's low for every
    symbol (the most permissive drop input) the gate's uncut list equals the prune's contribution."""
    world, panel = _panel(5)
    g = {"market_cap_min": 3e9, "relative_volume_min": 0.5, "price_drop_pct": 4.0, "price_drop_days": 5,
         "max_stocks": 10, "weinstein_stage2_only": 0}
    day = panel.sessions[-5]
    p = panel.pos(day)
    low = np.asarray(panel.arrays["l"][p])
    uncut = panel.select(day, g, BEH, now=low, cut=False)
    assert set(uncut) == set(panel.select_bounds(day, g, BEH, cut=False)) or set(uncut) <= set(
        panel.select_bounds(day, g, BEH, cut=False))
    cut = panel.select(day, g, BEH, now=low, cut=True)
    assert cut == uncut[:g["max_stocks"]]


def test_tie_at_the_cut_gate_and_prune_agree():
    """Two names with EXACTLY equal market cap straddle the cut: the gate keeps the symbol-ascending one and the
    prune keeps both, so the gate's pick is always inside the prune (no fatal refusal)."""
    syms = ["BBB", "AAA", "CCC"]
    sessions = W._sessions(40)
    T = len(sessions)
    bars = {}
    for s in syms:
        idx = np.arange(T - 1)
        c = np.full(T - 1, 100.0)
        bars[s] = (idx, c, c * 1.01, c * 0.99, c, np.full(T - 1, 1e6))
    shares = np.full((3, T), 1e8)
    arrays = ls.build_panel_arrays(bars, sessions, shares, syms)
    panel = ls.DailyPanel(syms, sessions, arrays, {})
    day = sessions[-1]
    g = {"market_cap_min": 1e9, "max_stocks": 1}
    now = np.full(3, 100.0)
    assert panel.select(day, g, BEH, now=now) == ["AAA"]
    assert panel.select(day, g, BEH, now=now, cut=False) == ["AAA", "BBB", "CCC"]
    assert set(panel.select_bounds(day, g, BEH, cut=False)) == {"AAA", "BBB", "CCC"}


class FakePS:
    """The price-source surface the gate reads."""

    def __init__(self, prices, intraday=True):
        self.prices, self.is_intraday, self.calls = prices, intraday, []
        self.interval = "5min" if intraday else "1d"

    def has_symbol(self, symbol):
        return symbol in self.prices

    def screener_now_price(self, symbol, as_of):
        self.calls.append((symbol, as_of))
        return self.prices.get(symbol)

    def volume_so_far(self, symbol, as_of):
        return 0.0


def _runtime(panel_dir, settings):
    man = ls.read_manifest(panel_dir)
    return {"panel": panel_dir, "settings": {**FULL, **settings}, "criteria_version": ls.CRITERIA_VERSION,
            "panel_fingerprint": man["panel_fingerprint"], "excluded_symbols": []}


def test_gate_maps_the_decision_to_its_own_morning_intraday_and_daily(tmp_path):
    world, panel = _panel(2)
    d = tmp_path / "panel"
    ls.save_panel(str(d), list(panel.symbols), panel.sessions, {k: np.asarray(v) for k, v in panel.arrays.items()},
                  {"shares_vendor_snapshot": "x", "shares_lag_days": 45, "stale_listed_symbols": [], "fresh_fraction": 1.0,
                   "last_bar_date": panel.sessions[-2], "source_fingerprint": "t"})
    sg.clear_panel_memo()
    st = {"market_cap_min": 1e9, "max_stocks": 5}
    prices = dict(world.open_now)
    day = datetime.fromisoformat(W.DAY + "T10:00:00")
    gate = sg.PanelGate(_runtime(str(d), st), FakePS(prices), intraday=True)
    assert gate.screen_day(day) == W.DAY                                   # the decision's OWN morning
    got = gate.symbols(day)
    assert got is gate.symbols(day)                                        # memoised: the same list object
    assert got == panel.select(W.DAY, st, BEH, now=np.array([prices[s] for s in panel.symbols]))
    # daily clock: the decision stamped D screens the NEXT session's morning with data through D
    prev = panel.sessions[panel.pos(W.DAY) - 2]
    gate_d = sg.PanelGate(_runtime(str(d), st), FakePS(prices, intraday=False), intraday=False)
    assert gate_d.screen_day(datetime.fromisoformat(prev + "T00:00:00")) == panel.sessions[panel.pos(prev) + 1]


def test_panel_refusals_list_what_is_missing(tmp_path):
    problems = ls.panel_problems(str(tmp_path / "nope"), "2024-01-01", "2024-12-31")
    assert problems and "missing" in problems[0]
    world, panel = _panel(2)
    d = tmp_path / "p"
    arrs = {k: np.asarray(v) for k, v in panel.arrays.items()}
    man = {"shares_vendor_snapshot": "x", "shares_lag_days": 45, "stale_listed_symbols": [], "fresh_fraction": 1.0,
           "last_bar_date": panel.sessions[-2], "source_fingerprint": "t"}
    ls.save_panel(str(d), list(panel.symbols), panel.sessions, arrs, man)
    assert ls.panel_problems(str(d), panel.sessions[300], panel.sessions[-5]) == []
    # window starts before the panel (no warm-up), ends after its bars
    probs = ls.panel_problems(str(d), panel.sessions[10], "2030-01-01")
    assert any("starts" in p for p in probs) and any("bars end" in p or "sessions end" in p for p in probs)
    # another criteria version
    m = json.loads((d / "manifest.json").read_text())
    m["criteria_version"] = "old-weekly"
    (d / "manifest.json").write_text(json.dumps(m))
    assert any("criteria_version" in p for p in ls.panel_problems(str(d), panel.sessions[300], panel.sessions[-5]))
    with pytest.raises(ls.PanelCoverageError, match="criteria_version"):
        ls.require_panel(str(d), panel.sessions[300], panel.sessions[-5])
    # an incomplete cache (<90 % of the symbols have a recent bar)
    m["criteria_version"] = ls.CRITERIA_VERSION
    m["stale_listed_symbols"] = ["AAA", "BBB"]
    (d / "manifest.json").write_text(json.dumps(m))
    assert any("CURRENT listing" in p and "AAA" in p for p in ls.panel_problems(str(d), panel.sessions[300], panel.sessions[-5]))
    # a different fingerprint than the job's
    m["stale_listed_symbols"] = []
    (d / "manifest.json").write_text(json.dumps(m))
    assert any("never switches panel" in p for p in
               ls.panel_problems(str(d), panel.sessions[300], panel.sessions[-5], expect_fp="other"))


def test_out_of_range_genome_and_seed_are_refused():
    declared = {k[len("screener_"):]: {"min": v["min"], "max": v["max"]} for k, v in RANGES.items()}
    us.check_genes_in_declared_ranges({"screener_market_cap_min": 2e10, "screener_price_drop_days": 12}, declared)
    with pytest.raises(us.ScreenerGenomeOutOfRange, match="market_cap_min"):
        us.check_genes_in_declared_ranges({"screener_market_cap_min": 3e10}, declared, where="pin")
    from app.services.strategy_optimization_handler import assert_population_screener_genes_in_range

    class Opt:
        param_ranges = {f"screener:{k}": {"min": v["min"], "max": v["max"], "type": v["type"]} for k, v in RANGES.items()}

    names = list(Opt.param_ranges)
    ok = [(v["min"] + v["max"]) / 2 for v in Opt.param_ranges.values()]
    assert_population_screener_genes_in_range(Opt, [ok])
    bad = list(ok)
    bad[names.index("screener:screener_market_cap_min")] = 3e10     # a seed from a higher cap band
    with pytest.raises(us.ScreenerGenomeOutOfRange, match="warm-start individual #1"):
        assert_population_screener_genes_in_range(Opt, [ok, bad])


def test_job_identity_carries_the_criteria(tmp_path):
    from app.services.strategy_optimization_handler import checkpoint_fingerprint
    ga = {"populationSize": 10, "generations": 3}
    space = {"x": {"min": 0, "max": 1, "step": 1, "type": "int"}}
    base = checkpoint_fingerprint(space, ga, None, None, "superset-v1")
    sim = checkpoint_fingerprint(space, ga, None, None, "superset-v1", ls.CRITERIA_VERSION)
    assert base != sim                                   # a weekly-gate job's checkpoint is never resumed by the sim
    assert checkpoint_fingerprint(space, ga, None, None, "superset-v1", None) == base       # unchanged when unstamped
    sys.path.insert(0, str(REPO / "tools"))
    import matrix_flags as mf
    assert mf.SCREENER_CRITERIA_NAME_TOKEN == ls.CRITERIA_NAME_TOKEN == "-lds2"
    assert mf.with_universe_rule_name("scr-mid-X-S1") == "scr-mid-X-S1-sup1-lds2"
    assert mf.with_universe_rule_name("scr-mid-X-S1-timegene") == "scr-mid-X-S1-timegene-sup1-lds2"
    assert mf.with_universe_rule_name("scr-mid-X-S1-sup1-lds2") == "scr-mid-X-S1-sup1-lds2"       # idempotent
    assert mf.with_universe_rule_name("scr-large-FactorRanker", simulated=False) == "scr-large-FactorRanker-sup1"


def test_one_interval_classifier():
    from app.services.backtest.price_source import _is_intraday
    from app.services.strategy_optimization_handler import _is_intraday_interval
    for iv in ("1min", "5min", "15min", "30min", "1m", "5m", "1h", "4h", "1hour", "4hour", "1d", "1wk", "1mo", "1day"):
        assert _is_intraday(iv) == _is_intraday_interval(iv) == us.interval_is_intraday(iv), iv
    assert us.interval_is_intraday("1hour") and not us.interval_is_intraday("1d")


def test_pre_fix_volume_min_is_refused_on_a_daily_clock(tmp_path):
    world, panel = _panel(2)
    d = tmp_path / "panel"
    ls.save_panel(str(d), list(panel.symbols), panel.sessions, {k: np.asarray(v) for k, v in panel.arrays.items()},
                  {"shares_vendor_snapshot": "x", "shares_lag_days": 45, "stale_listed_symbols": [], "fresh_fraction": 1.0,
                   "last_bar_date": panel.sessions[-2], "source_fingerprint": "t2"})
    sg.clear_panel_memo()
    with pytest.raises(sg.ScreenerGateRefusal, match="daily clock"):
        sg.PanelGate(_runtime(str(d), {"volume_min": 1e5}), FakePS({}, False), intraday=False, beh=ls.LEGACY_CURRENT)


def _five_min_source(skip_first_bar_of_jan3=False):
    from datetime import date, timezone
    from app.services.backtest.price_source import AsOfPriceSource
    ps = AsOfPriceSource(ohlcv_provider=None, interval="5min")
    rows, px = [], 100.0
    for d in (date(2024, 1, 2), date(2024, 1, 3)):
        for h, m in ((9, 30), (9, 35), (9, 40), (9, 45), (9, 50), (9, 55), (10, 0), (10, 5)):
            if skip_first_bar_of_jan3 and d == date(2024, 1, 3) and (h, m) == (9, 30):
                continue
            rows.append({"Date": datetime(d.year, d.month, d.day, h, m), "Open": px, "High": px + 0.5,
                         "Low": px - 0.5, "Close": px + 0.3, "Volume": 1000})
            px += 1.0
    ps.load_bars("AAA", rows)
    return ps, rows


def _wall(h, m, d=3):
    from datetime import timezone
    return datetime(2024, 1, d, h, m, tzinfo=timezone.utc)


def test_screener_now_price_is_the_opening_print_only_inside_the_first_bar():
    """The ONE owner-approved exception: at a first-bar decision the screener's "now" is the session's opening
    print; once a bar of the session has ended it is that bar's close (== decision_price); never later."""
    ps, rows = _five_min_source()
    opens = {(r["Date"].day, r["Date"].hour, r["Date"].minute): r for r in rows}
    jan2_last_close = rows[7]["Close"]
    first = opens[(3, 9, 30)]
    t = _wall(9, 30)
    assert ps.screener_now_price("AAA", t) == first["Open"]                   # opening print
    assert float(ps.decision_price("AAA", t)) == pytest.approx(jan2_last_close)   # the strict rule: yesterday's close
    assert ps.screener_now_price("AAA", t) != float(ps.decision_price("AAA", t))
    t2 = _wall(9, 35)                                                         # bar 09:30 has ended
    assert ps.screener_now_price("AAA", t2) == first["Close"] == float(ps.decision_price("AAA", t2))
    t3 = _wall(10, 0)                                                         # bar 09:55 ended
    assert ps.screener_now_price("AAA", t3) == opens[(3, 9, 55)]["Close"] == float(ps.decision_price("AAA", t3))
    assert ps.screener_now_price("AAA", _wall(9, 34)) == first["Open"]        # still inside the first bar
    assert ps.screener_now_price("ZZZ", t) is None


def test_screener_now_price_is_the_decision_price_when_the_first_bar_did_not_trade():
    ps, rows = _five_min_source(skip_first_bar_of_jan3=True)
    assert ps.screener_now_price("AAA", _wall(9, 30)) == float(ps.decision_price("AAA", _wall(9, 30)))   # no opening print: the DecisionPrice rule (yesterday last bar)
    assert ps.screener_now_price("AAA", _wall(9, 40)) is not None    # the 09:35 bar ended


def test_screener_now_price_daily_clock_is_the_bar_close():
    from datetime import date
    from app.services.backtest.price_source import AsOfPriceSource
    ps = AsOfPriceSource(ohlcv_provider=None, interval="1d")
    ps.load_bars("AAA", [{"Date": datetime(2024, 1, d), "Open": 10.0 + d, "High": 20.0, "Low": 5.0, "Close": 11.0 + d,
                          "Volume": 1} for d in (2, 3, 4)])
    t = datetime(2024, 1, 3)
    assert ps.screener_now_price("AAA", t) == 14.0                 # D's close (the daily clock's "now")


def test_the_gate_never_fetches_anything(monkeypatch, tmp_path):
    """HERMETIC: static universe, prune and the per-decision gate read the panel and the price source only."""
    import ba2_providers.fmp_common as fc
    import requests

    def boom(*a, **k):
        raise AssertionError("the screener gate went to the network")
    monkeypatch.setattr(fc, "fmp_http_get", boom)
    monkeypatch.setattr(requests, "get", boom)
    world, panel = _panel(6)
    start, end = panel.sessions[-30], panel.sessions[-2]
    sg.static_universe(panel, start, end, {}, us.gene_ranges_from_opt(RANGES), intraday=True)
    sg.prune_symbols(panel, start, end, {"market_cap_min": 2e9, "max_stocks": 10}, None, intraday=True)
    d = tmp_path / "panel"
    ls.save_panel(str(d), list(panel.symbols), panel.sessions, {k: np.asarray(v) for k, v in panel.arrays.items()},
                  {"shares_vendor_snapshot": "x", "shares_lag_days": 45, "stale_listed_symbols": [], "fresh_fraction": 1.0,
                   "last_bar_date": panel.sessions[-2], "source_fingerprint": "hermetic"})
    sg.clear_panel_memo()
    gate = sg.PanelGate(_runtime(str(d), {"market_cap_min": 2e9, "max_stocks": 10}), FakePS(dict(world.open_now)),
                        intraday=True)
    assert isinstance(gate.symbols(datetime.fromisoformat(W.DAY + "T10:00:00")), list)


# ----------------------------------------------------------------------------------------------------------
# "now" and the FORMING daily bar at every decision time T (09:30 opening print; T >= 09:35 the ended-bar rule)
# ----------------------------------------------------------------------------------------------------------
def _full_session_source():
    """One symbol, two sessions of 78 five-minute bars. Jan-3 climbs to a SPIKE HIGH at 14:00 (after any early T)."""
    from datetime import date
    from app.services.backtest.price_source import AsOfPriceSource
    ps = AsOfPriceSource(ohlcv_provider=None, interval="5min")
    rows = []
    for d in (date(2024, 1, 2), date(2024, 1, 3)):
        for i in range(78):
            h, m = divmod(9 * 60 + 30 + 5 * i, 60)
            base = 100.0 + (0.01 * i if d.day == 2 else 0.02 * i)
            spike = 40.0 if (d.day == 3 and (h, m) == (14, 0)) else 0.0
            rows.append({"Date": datetime(d.year, d.month, d.day, h, m), "Open": base, "High": base + 0.5 + spike,
                         "Low": base - 0.5, "Close": base + 0.2, "Volume": 1000})
    ps.load_bars("AAA", rows)
    return ps, rows


def test_forming_bar_through_T_never_sees_a_bar_that_has_not_ended():
    ps, rows = _full_session_source()
    day3 = [r for r in rows if r["Date"].day == 3]
    spike_high = max(r["High"] for r in day3)
    assert spike_high > 140
    for (h, m) in ((9, 30), (9, 35), (10, 0), (12, 0), (15, 30), (15, 45)):
        t = _wall(h, m)
        ended = [r for r in day3 if r["Date"] + __import__("datetime").timedelta(minutes=5) <= datetime(2024, 1, 3, h, m)]
        hi = ps.screener_session_high("AAA", t)
        px = ps.screener_now_price("AAA", t)
        if not ended:                                              # T = 09:30: the opening print
            assert px == day3[0]["Open"] and hi == day3[0]["Open"], (h, m)
        else:                                                      # T >= 09:35: bars ended <= T only
            assert px == ended[-1]["Close"] == float(ps.decision_price("AAA", t)), (h, m)
            assert hi == max(r["High"] for r in ended), (h, m)
            assert px <= hi
    # the 14:00 spike (bar ends 14:05) is invisible at 10:00, 12:00, 15:30 sees it only once it has ended
    assert ps.screener_session_high("AAA", _wall(10, 0)) < 105 and ps.screener_session_high("AAA", _wall(12, 0)) < 106
    assert ps.screener_session_high("AAA", _wall(14, 0)) < 110                  # the 14:00 bar has not ended at 14:00
    assert ps.screener_session_high("AAA", _wall(14, 5)) == spike_high         # ... and has at 14:05
    assert ps.screener_session_high("AAA", _wall(15, 45)) == spike_high


def test_the_day_bounds_are_a_valid_superset_for_every_decision_time():
    """The prune / superset passes the session's [low, high] as 'now' and the day high as the forming high: every
    price and every high-so-far the gate can read at ANY T lies inside them."""
    ps, rows = _full_session_source()
    day3 = [r for r in rows if r["Date"].day == 3]
    lo, hi = min(r["Low"] for r in day3), max(r["High"] for r in day3)
    for hh in range(9, 16):
        for mm in range(0, 60, 5):
            if (hh, mm) < (9, 30) or (hh, mm) > (15, 55):
                continue
            t = _wall(hh, mm)
            px, fh = ps.screener_now_price("AAA", t), ps.screener_session_high("AAA", t)
            assert lo <= px <= hi and px <= fh <= hi, (hh, mm, px, fh)


def test_peak_uses_the_forming_high_not_only_the_price_now():
    """forming_hi joins the drop window's peak: with a high-so-far above the price now the drop is measured from it."""
    n = 2
    cols = dict(symbols=np.array(["A", "B"]), shares=np.full(n, 1e8), last_close=np.array([100.0, 100.0]),
                rvol=np.full(n, 2.0), last_vol=np.full(n, 1e6), avg20=np.full(n, 1e6), w2=np.ones(n, bool),
                fl=np.full(n, np.nan), peak=np.array([101.0, 101.0]))
    st = {"market_cap_min": 1e9, "price_drop_pct": 10.0, "price_drop_days": 5, "max_stocks": 10}
    now = np.array([95.0, 95.0])
    # high so far 110 (A) vs 101 (B): drop A = 13.6 % passes, B = 5.9 % fails
    got = ls.select_from_columns(**cols, now=now, settings=st, beh=ls.POST_FIX,
                                 forming_hi=lambda idx: np.array([110.0, 101.0])[idx])
    assert [str(cols["symbols"][i]) for i in got] == ["A"]
    # without forming_hi the single price now is the forming bar (high = now): neither passes
    assert list(ls.select_from_columns(**cols, now=now, settings=st, beh=ls.POST_FIX)) == []


def test_gate_t_1000_does_not_leak_a_later_session_high(tmp_path):
    """End to end through PanelGate with a real 5-minute price source: a stock whose session high comes AFTER
    10:00 is not screened as a deep dip from that future high; at 15:45 (high ended) it is."""
    ps, rows = _full_session_source()
    sessions = ["2024-01-02", "2024-01-03", "2024-01-04"]
    T = len(sessions)
    bars = {"AAA": (np.arange(1), np.array([100.0]), np.array([101.0]), np.array([99.0]), np.array([100.0]), np.array([1e6]))}
    shares = np.full((1, T), 1e8)
    arrays = ls.build_panel_arrays(bars, sessions, shares, ["AAA"])
    d = tmp_path / "panel"
    ls.save_panel(str(d), ["AAA"], sessions, arrays, {"shares_vendor_snapshot": "x", "shares_lag_days": 45,
                  "stale_listed_symbols": [], "fresh_fraction": 1.0, "last_bar_date": "2024-01-02", "source_fingerprint": "leak"})
    sg.clear_panel_memo()
    st = {"market_cap_min": 1e9, "price_drop_pct": 20.0, "price_drop_days": 2, "max_stocks": 5}
    gate = sg.PanelGate(_runtime(str(d), st), ps, intraday=True)
    # peak(2) from the finished sessions is 101; the 14:00 spike (high ~141.5) would make 'now' ~102 a 28 % drop
    assert gate.symbols(_wall(10, 0)) == []
    assert gate.symbols(_wall(15, 45)) == ["AAA"]
