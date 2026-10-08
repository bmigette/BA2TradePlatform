"""Review fixes of the daily screener gate: a missing "now" is NOT a candidate (no previous-close fallback); the daily-clock
superset; complete settings; panel identity."""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_screener_gate_daily import (BEH, FULL, W, FakePS, _genome, _panel, _runtime, ls, sg)  # noqa: E402


def _save(panel, d, ident, **extra):
    man = {"shares_vendor_snapshot": "x", "shares_lag_days": 45, "stale_listed_symbols": [],
           "last_bar_date": panel.sessions[-2], "source_fingerprint": ident, **extra}
    ls.save_panel(str(d), list(panel.symbols), panel.sessions, {k: np.asarray(v) for k, v in panel.arrays.items()}, man)
    sg.clear_panel_memo()


def _genome_with_prices(rng):
    g = _genome(rng)
    g["price_min"] = rng.choice([0, 0, 10.0, 30.0])
    g["price_max"] = rng.choice([0, 0, 120.0, 300.0])
    return g


@pytest.mark.parametrize("intraday", [True, False])
def test_gate_inside_prune_when_prices_exist_only_for_the_pruned_symbols(intraday):
    """THE ENGINE'S SITUATION: the trial's price source holds ONLY the pruned symbols.  Every other band candidate has
    no 'now'; the old code priced it at yesterday's close (possibly outside the day's [low, high]) and the gate selected
    names the prune had dropped -> job-fatal ScreenerUniverseRefusal.  Now such a candidate is not a candidate."""
    world, panel = _panel(11, n_sym=60)
    start, end = panel.sessions[-40], panel.sessions[-3]
    rng = random.Random(3)
    lo = np.asarray(panel.arrays["l"]); hi = np.asarray(panel.arrays["h"]); lc = np.asarray(panel.arrays["lc"])
    checked = 0
    for _ in range(30):
        g = {**FULL, **_genome_with_prices(rng)}
        prune = set(sg.prune_symbols(panel, start, end, g, None, intraday=intraday))
        in_prune = np.array([str(s) in prune for s in panel.symbols])
        for day in sg.screen_days(panel, start, end, intraday=intraday):
            p = panel.pos(day)
            if intraday:
                now = lo[p] + np.random.default_rng(p).random(len(panel.symbols)) * (hi[p] - lo[p])
                fh = hi[p]
            else:                            # the decision day's close = the previous close of the screened morning
                now = lc[p].copy()
                fh = None
            now = np.where(in_prune, now, np.nan)
            got = panel.select(day, g, BEH, now=now, forming_hi=(None if fh is None else (lambda idx, fh=fh: fh[idx])))
            checked += 1
            assert set(got) <= prune, (intraday, day, g, sorted(set(got) - prune))
    assert checked > 300


def test_a_candidate_without_a_price_is_dropped_and_counted_not_priced_at_yesterday_s_close():
    n = 3
    cols = dict(symbols=np.array(["A", "B", "C"]), shares=np.full(n, 1e8), last_close=np.array([50.0, 50.0, 50.0]),
                rvol=np.full(n, 2.0), last_vol=np.full(n, 1e6), avg20=np.full(n, 1e6), w2=np.ones(n, bool),
                fl=np.full(n, np.nan), peak=np.array([100.0, 100.0, 100.0]))
    st = {"market_cap_min": 1e9, "price_drop_pct": 10.0, "price_drop_days": 5, "max_stocks": 10}
    diag = {}
    got = ls.select_from_columns(**cols, now=np.array([40.0, np.nan, 40.0]), settings=st, beh=ls.POST_FIX, diag=diag)
    assert [str(cols["symbols"][i]) for i in got] == ["A", "C"] and diag["dropped_no_price"] == 1
    # the price is never read when no stage needs it (no price filter, no drop, no Weinstein)
    got2 = ls.select_from_columns(**cols, now=np.full(n, np.nan), settings={"market_cap_min": 1e9, "max_stocks": 10},
                                  beh=ls.POST_FIX)
    assert len(got2) == 3


def test_a_candidate_without_a_price_at_T_is_counted_and_a_defective_cache_is_an_outage(tmp_path):
    """Nothing knowable at T for a candidate = not a candidate (the engine's own undecidable rule), COUNTED in the run's
    results; more than live's 10 % outage share of the candidates = ScreenerDataOutage (job-fatal), naming them."""
    world, panel = _panel(2)
    d = tmp_path / "panel"
    _save(panel, d, "noprice")
    prices = dict(world.open_now)
    victim = max(world.symbols, key=lambda s: world.prev_close(s) * world.shares[s])
    prices[victim] = None
    st = {"market_cap_min": 1e9, "price_min": 1.0, "max_stocks": 50}
    gate = sg.PanelGate(_runtime(str(d), st), FakePS(prices), intraday=True)
    got = gate.symbols(datetime.fromisoformat(W.DAY + "T10:00:00"))
    assert victim not in got and gate.diagnostics()["no_price_symbols"] == {victim: 1}
    assert gate.diagnostics()["no_price_candidate_decisions"] == 1
    for s_ in list(prices)[:20]:
        prices[s_] = None                                   # a broken intraday cache: most candidates lack a price
    gate2 = sg.PanelGate(_runtime(str(d), st), FakePS(prices), intraday=True)
    with pytest.raises(ls.ScreenerDataOutage, match="no knowable price"):
        gate2.symbols(datetime.fromisoformat(W.DAY + "T10:00:00"))
    from app.services.job_fatal import JOB_FATAL_ERROR_TYPES
    assert "ScreenerDataOutage" in JOB_FATAL_ERROR_TYPES


def test_daily_clock_superset_covers_a_gap_up_the_gate_never_sees():
    """close(D)=100, D+1 gaps to 110; price_max=105: the daily gate reads D's close (100) and picks; the old bounds were
    the NEXT session's [109, 112] and dropped it from the prune."""
    syms = ["GAP"]
    sessions = W._sessions(40)
    T = len(sessions)
    idx = np.arange(T - 2)
    c = np.full(T - 2, 100.0)
    bars = {"GAP": (np.append(idx, T - 1), np.append(c, 110.0), np.append(c * 1.01, 112.0), np.append(c * 0.99, 109.0),
                    np.append(c, 111.0), np.full(T - 1, 1e6))}
    arrays = ls.build_panel_arrays(bars, sessions, np.full((1, T), 1e8), syms)
    panel = ls.DailyPanel(syms, sessions, arrays, {})
    morning = sessions[-2]                       # decision day D = sessions[-3]; this morning's previous close is 100
    st = {**FULL, "market_cap_min": 1e9, "price_max": 105.0}
    assert panel.select(morning, st, BEH, now=np.array([100.0])) == ["GAP"]
    assert panel.select_bounds(morning, st, BEH, cut=False, daily_clock=True) == ["GAP"]
    gap_morning = sessions[-1]                   # the gap session itself: previous close 100 (bar T-2 absent), bounds [109,112]
    assert panel.select_bounds(gap_morning, st, BEH, cut=False, daily_clock=False) in ([], ["GAP"])


def test_incomplete_settings_reach_the_gate_as_a_refusal(tmp_path):
    world, panel = _panel(2)
    d = tmp_path / "panel"
    _save(panel, d, "full")
    rt = _runtime(str(d), {})
    rt["settings"] = {"market_cap_min": 1e9}                       # incomplete: no floors stated
    with pytest.raises(ls.SimulationRefusal, match="missing selection key"):
        sg.PanelGate(rt, FakePS({}), intraday=True)


def test_panel_identity_never_switches_within_a_process(tmp_path):
    world, panel = _panel(2)
    d = tmp_path / "panel"
    _save(panel, d, "idA")
    pa = sg.get_panel(str(d), "idA")
    assert sg.get_panel(str(d), "idA") is pa
    with pytest.raises(sg.ScreenerGateRefusal, match="never switches panel"):
        sg.get_panel(str(d), "idB")
    arr = {k: np.asarray(v) for k, v in panel.arrays.items()}
    man = {"shares_vendor_snapshot": "x", "shares_lag_days": 45, "stale_listed_symbols": [],
           "last_bar_date": panel.sessions[-2], "source_fingerprint": "idA"}
    bad = tmp_path / "bad"
    ls.save_panel(str(bad), list(panel.symbols), panel.sessions, arr, man)
    np.save(str(bad / "rvol.npy"), np.zeros((3, 3)))
    sg.clear_panel_memo()
    with pytest.raises(sg.ScreenerGateRefusal, match="corrupt or half-synced"):
        sg.get_panel(str(bad))
    root = tmp_path / "cache"
    pd_ = ls.panel_dir_for(str(root), "idA")
    ls.save_panel(pd_, list(panel.symbols), panel.sessions, arr, man)
    rel = ls.panel_rel(str(root), pd_)
    assert rel == "screener/daily_panel/idA" and ls.resolve_panel_path(rel, str(root)) == pd_
    assert ls.latest_panel(str(root)) == pd_
    assert not any(n.endswith((".tmp", ".building")) for n in os.listdir(pd_))


def test_the_gate_never_selects_a_symbol_of_the_reviewed_exclusion_list(tmp_path):
    world, panel = _panel(2)
    d = tmp_path / "panel"
    _save(panel, d, "excl", excluded_unusable=[{"symbol": str(panel.symbols[0]), "reason": "r", "added": "2026-10-08", "reviewed_by": "o"}])
    pan = sg.get_panel(str(d))
    v = sg.valid_mask(pan, None)
    assert not v[0] and v[1:].all()


def test_band_is_tested_on_the_price_at_T_after_the_first_bar_and_on_the_previous_close_inside_it(tmp_path):
    """A name whose previous-close cap is just BELOW the floor but whose price at T lifts it above: admitted at 10:00, not at 09:30
    (where the vendor's cap is still the previous close for the names that have not printed)."""
    syms = ["UP"]
    sessions = W._sessions(40)
    T = len(sessions)
    idx = np.arange(T)                                                                  # the decision day's own bar exists: low 99, high 300
    c = np.full(T, 100.0)
    hi_bar = c * 1.01
    hi_bar[-1] = 300.0
    bars = {"UP": (idx, c, hi_bar, c * 0.99, c, np.full(T, 1e6))}
    arrays = ls.build_panel_arrays(bars, sessions, np.full((1, T), 5.0e7), syms)       # prev cap = 100 x 5e7 = 5.0e9
    panel = ls.DailyPanel(syms, sessions, arrays, {})
    day = sessions[-1]
    st = {**FULL, "market_cap_min": 5.2e9, "max_stocks": 5}
    price = np.array([106.0])                                                           # cap at T = 5.3e9
    assert panel.select(day, st, BEH, now=price, band_at_now=False) == []
    assert panel.select(day, st, BEH, now=price, band_at_now=True) == ["UP"]
    # the cap CEILING uses the session LOW in the bounds, the FLOOR the session HIGH: a superset of every price between
    st2 = {**FULL, "market_cap_min": 5.2e9, "max_stocks": 5}
    lo = np.array([99.0]); hi = np.array([106.0])
    # a 2.4x intraday move (beyond the old [0.5 x min, 2 x max] tolerance) is evaluated: the exact bound is the session high
    big = np.array([240.0])
    assert panel.select(day, {**FULL, "market_cap_min": 1.1e10, "max_stocks": 5}, BEH, now=big, band_at_now=True) == ["UP"]
    assert panel.select(day, st2, BEH, now=lambda i: (lo[i], hi[i]), band_at_now=True) == ["UP"]
    st3 = {**FULL, "market_cap_max": 4.9e9, "max_stocks": 5}
    assert panel.select(day, st3, BEH, now=lambda i: (lo[i], hi[i]), band_at_now=True) == []       # low 99 x 5e7 = 4.95e9 > 4.9e9: no


def test_job_end_alarm_for_a_selectable_name_that_was_never_loaded(tmp_path):
    """An 'outside the preload' candidate that the bounds-based selection (the prune's function) returns for that day is a missing
    cache file, not a name the prune dropped: assert_complete() is job-fatal and names it; a clean run passes."""
    world, panel = _panel(2)
    d = tmp_path / "panel"
    _save(panel, d, "alarm")
    prices = dict(world.open_now)
    victim = max(world.symbols, key=lambda s: world.prev_close(s) * world.shares[s])
    del prices[victim]                                   # not loaded at all (has_symbol False)
    st = {"market_cap_min": 1e9, "price_min": 1.0, "max_stocks": 50}
    gate = sg.PanelGate(_runtime(str(d), st), FakePS(prices), intraday=True)
    gate.symbols(datetime.fromisoformat(W.DAY + "T10:00:00"))
    assert gate.diagnostics()["outside_preload_candidate_decisions"] >= 1
    with pytest.raises(sg.ScreenerGateRefusal, match=victim):
        gate.assert_complete()
    clean = sg.PanelGate(_runtime(str(d), st), FakePS(dict(world.open_now)), intraday=True)
    clean.symbols(datetime.fromisoformat(W.DAY + "T10:00:00"))
    clean.assert_complete()


def test_the_first_bar_rule_reads_the_price_source_interval_and_requires_it(tmp_path):
    world, panel = _panel(2)
    _save(panel, tmp_path / "p", "fb")
    gate = sg.PanelGate(_runtime(str(tmp_path / "p"), {"market_cap_min": 1e9, "max_stocks": 5}),
                        FakePS(dict(world.open_now)), intraday=True)
    assert gate._in_first_bar(datetime.fromisoformat(W.DAY + "T09:32:00"))
    assert not gate._in_first_bar(datetime.fromisoformat(W.DAY + "T09:35:00"))
    gate.ps.interval = "15min"
    assert gate._in_first_bar(datetime.fromisoformat(W.DAY + "T09:44:00"))
    del gate.ps.interval
    with pytest.raises(AttributeError):                 # no default: a price source without an interval is a bug
        gate._in_first_bar(datetime.fromisoformat(W.DAY + "T09:32:00"))
    gate.ps.interval = "weekly-ish"
    with pytest.raises(sg.ScreenerGateRefusal):
        gate._in_first_bar(datetime.fromisoformat(W.DAY + "T09:32:00"))
