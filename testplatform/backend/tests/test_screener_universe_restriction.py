"""``--screener-universe-file``: the screen of an OPTION job is restricted to a fixed candidate list BEFORE its
max_stocks cut (stage 2: chain data and market-condition manifests exist for 752 names only).

Pinned here: the launch preparation (static universe is a subset of the list, the stamp on the run, every refusal), the
gate's valid mask (the cut picks the best N OF THE LIST), the handler wiring (trial runtime carries the list), the launcher
flag, the screener-gene seed fill for a style job, and the standalone-backtest refusal.  Live parity (StockScreener setting,
deploy export) is pinned in packages/providers/tests/test_stockscreener_symbol_universe.py and
packages/common/tests/test_symbol_universe_deploy.py.

Synthetic world of ``test_screener_launch_shared`` (no vendor call, tmp cache).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_screener_launch_shared as S  # noqa: E402

from app.services.backtest import screener_gate as sg  # noqa: E402
from app.services.backtest import screener_launch as sl  # noqa: E402
from ba2_providers.screener import live_sim as ls  # noqa: E402
from ba2_providers.screener import universe_superset as us  # noqa: E402

env = S.env  # the fixture (tmp cache + synthetic daily panel + cached bars for all but S.UNCACHED)

FULL = {"market_cap_min": 0, "market_cap_max": 0, "price_min": 0, "price_max": 0, "volume_min": 0, "volume_max": 0,
        "float_min": 0, "float_max": 0, "relative_volume_min": 0, "price_drop_pct": 0, "price_drop_days": 5,
        "max_stocks": 10000, "sort_metric": "market_cap", "weinstein_stage2_only": 0}


def _req(env, **kw):
    start, end = S._window(env)
    base = dict(store=env["store"], start=start, end=end, interval="1d", history_source="fmp", base_settings={},
                scr_opt=dict(us.SCREENER_OPT), cadence_days=7, bypass=False, panel=env["panel"],
                exclude_uncached=True)
    base.update(kw)
    return sl.ScreenerLaunchRequest(**base)


def _prep(env, **kw):
    return sl.prepare_screener_launch(_req(env, **kw), emit=lambda m: None)


def _symbols(env):
    return [str(s) for s in S.W.World(2, n_sym=40).panel().symbols]


# ------------------------------------------------------------------------------------------ launch preparation
def test_static_universe_is_a_subset_of_the_restriction_and_the_stamp_is_recorded(env):
    syms = _symbols(env)
    free = _prep(env)
    keep = syms[:15]
    got = _prep(env, universe_restriction=keep)
    assert set(got.enabled) <= set(keep)
    assert 0 < len(got.enabled) < len(free.enabled)
    stamp = got.screener_opt["universe_restriction"]
    assert stamp["symbols"] == sorted(keep) and stamp["uncovered"] == [] and len(stamp["sha256"]) == 64
    assert "universe_restriction" not in free.screener_opt          # an unrestricted job's stamp is unchanged


def test_the_restriction_is_case_and_duplicate_insensitive_and_sorted(env):
    syms = _symbols(env)
    got = _prep(env, universe_restriction=[syms[3].lower(), syms[1], syms[1], " " + syms[2] + " "])
    assert got.screener_opt["universe_restriction"]["symbols"] == sorted({syms[1], syms[2], syms[3]})


def test_an_empty_restriction_refuses(env):
    with pytest.raises(sl.ScreenerLaunchRefused, match="no symbols"):
        _prep(env, universe_restriction=["", "  "])


def test_a_symbol_the_panel_cannot_select_refuses_naming_it_unless_accepted(env):
    syms = _symbols(env)
    with pytest.raises(sl.ScreenerLaunchRefused, match="NOT_IN_PANEL") as e:
        _prep(env, universe_restriction=[*syms[:10], "NOT_IN_PANEL"])
    assert "--screener-universe-allow-uncovered" in e.value.text and e.value.problems == ["NOT_IN_PANEL"]
    got = _prep(env, universe_restriction=[*syms[:10], "NOT_IN_PANEL"], universe_allow_uncovered=True)
    assert got.screener_opt["universe_restriction"]["uncovered"] == ["NOT_IN_PANEL"]
    assert "NOT_IN_PANEL" not in got.enabled


def test_a_restriction_that_selects_nothing_refuses(env):
    syms = _symbols(env)
    # a name the panel holds but whose cap never reaches the band: the loosest screen finds nothing
    with pytest.raises(sl.ScreenerLaunchRefused):
        _prep(env, universe_restriction=syms[:2], cap_band="large", base_settings={"price_min": 1e9})


def test_a_bypass_expert_cannot_take_a_restriction(env):
    with pytest.raises(sl.ScreenerLaunchRefused, match="daily-panel gate"):
        _prep(env, bypass=True, universe_restriction=_symbols(env)[:5])


# ------------------------------------------------------------------------------------------ the gate
def test_valid_mask_keeps_only_the_restriction_and_refuses_an_empty_one(env):
    panel = S.W.World(2, n_sym=40).panel()
    syms = [str(s) for s in panel.symbols]
    m = sg.valid_mask(panel, None, syms[:7])
    assert m.sum() == 7 and set(np.asarray(panel.symbols)[m].astype(str)) == set(syms[:7])
    m2 = sg.valid_mask(panel, [syms[0]], syms[:7])                   # exclusions still apply inside the list
    assert m2.sum() == 6
    assert sg.valid_mask(panel, None, None).all()                    # no restriction = unchanged
    with pytest.raises(sg.ScreenerGateRefusal, match="empty universe_restriction"):
        sg.valid_mask(panel, None, [])


def test_the_cut_takes_the_best_n_of_the_list_not_of_the_market(env):
    panel = S.W.World(2, n_sym=40).panel()
    syms = [str(s) for s in panel.symbols]
    day = panel.sessions[-5]
    st = {**FULL, "max_stocks": 5}
    free = panel.select_bounds(day, st, ls.POST_FIX, cut=True, valid=sg.valid_mask(panel, None), daily_clock=False)
    keep = [s for s in syms if s not in free][:12]                   # a list that contains NONE of the market's own top 5
    got = panel.select_bounds(day, st, ls.POST_FIX, cut=True, valid=sg.valid_mask(panel, None, keep), daily_clock=False)
    assert free and set(free).isdisjoint(keep)
    assert len(got) == 5 and set(got) <= set(keep)                   # still a full cut, taken from the list


def test_prune_and_static_universe_honour_the_restriction(env):
    panel = S.W.World(2, n_sym=40).panel()
    syms = [str(s) for s in panel.symbols]
    start, end = S._window(env)
    keep = syms[:9]
    out = sg.prune_symbols(panel, start, end, FULL, None, intraday=False, restrict_to=keep)
    assert out and set(out) <= set(keep)
    ranges = us.gene_ranges_from_opt(us.SCREENER_OPT)
    sup = sg.static_universe(panel, start, end, FULL, ranges, intraday=False, restrict_to=keep)
    assert sup and set(sup) <= set(keep)


def test_the_panel_gate_applies_the_runtime_restriction(env):
    panel = S.W.World(2, n_sym=40).panel()
    keep = [str(s) for s in panel.symbols][:6]
    rt = {"panel": env["panel"], "panel_fingerprint": ls.read_manifest(env["panel"])["panel_fingerprint"],
          "settings": dict(FULL), "excluded_symbols": [], "criteria_version": ls.CRITERIA_VERSION,
          "universe_restriction": keep}
    class _PS:
        interval = "1d"
        def screener_now_price(self, sym, at): return 50.0
        def screener_session_high(self, sym, at): return 50.0
        def has_symbol(self, sym): return True
    gate = sg.PanelGate(rt, _PS(), intraday=False, history_source="fmp")
    assert gate.valid.sum() == 6


# ------------------------------------------------------------------------------------------ handler wiring
def test_handler_refuses_a_malformed_or_gateless_restriction(env):
    from app.services import strategy_optimization_handler as H
    so = {"store": env["store"], "base_settings": {}, "cadence_days": 7, "universe_restriction": {"symbols": []}}
    with pytest.raises(ValueError, match="non-empty list"):
        H._build_hoisted_state({"screener_opt": so, "start_date": "2024-01-02", "end_date": "2024-02-02"})


# ------------------------------------------------------------------------------------------ launcher flag
def _args(**kw):
    import argparse
    base = dict(screener_universe_file=None, screener=False, screener_universe_allow_uncovered=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_universe_file_flag_refusals(tmp_path):
    f = tmp_path / "u.txt"
    f.write_text("# comment\naapl\nMSFT\n\nAAPL\n", encoding="utf-8")
    assert S.L._screener_universe_restriction(_args()) is None
    with pytest.raises(SystemExit, match="needs --screener"):
        S.L._screener_universe_restriction(_args(screener_universe_file=str(f)))
    assert S.L._screener_universe_restriction(_args(screener_universe_file=str(f), screener=True)) == ["AAPL", "MSFT"]
    empty = tmp_path / "e.txt"
    empty.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="lists no symbols"):
        S.L._screener_universe_restriction(_args(screener_universe_file=str(empty), screener=True))
    with pytest.raises(SystemExit, match="is not a file"):
        S.L._screener_universe_restriction(_args(screener_universe_file=str(tmp_path / "nope.txt"), screener=True))
    with pytest.raises(SystemExit, match="needs --screener-universe-file"):
        S.L._screener_universe_restriction(_args(screener_universe_allow_uncovered=True))


# ------------------------------------------------------------------------------------------ seed fill
def test_seed_fill_is_the_loosest_screen_of_each_declared_range():
    space = {f"screener:{k}": {"min": v["min"], "max": v["max"], "step": v["step"], "type": v["type"]}
             for k, v in us.SCREENER_OPT.items()}
    space["model:x"] = {"min": 0, "max": 1, "step": 1, "type": "int"}
    fill = S.L._screener_seed_fill(space)
    assert set(fill) == {k for k in space if k.startswith("screener:")}
    assert fill["screener:screener_relative_volume_min"] == 0.0 and fill["screener:screener_price_drop_pct"] == 0.0
    assert fill["screener:screener_weinstein_stage2_only"] == 0
    assert fill["screener:screener_max_stocks"] == 50                 # the LARGEST cut = the loosest
    assert fill["screener:screener_market_cap_min"] == us.SCREENER_OPT["screener_market_cap_min"]["min"]


def test_seed_fill_refuses_a_screener_gene_with_no_rule():
    with pytest.raises(SystemExit, match="no loosest-value rule"):
        S.L._screener_seed_fill({"screener:screener_mystery": {"min": 0, "max": 1, "step": 1, "type": "int"}})


# ------------------------------------------------------------------------------------------ standalone refusal
def test_standalone_screener_backtest_refuses_a_restricted_universe():
    from app.services.backtest import daily_backtest_handler as D
    u = {"mode": "screener", "screener_store": "x", "screener_settings": {"screener_symbol_universe": "AAPL,MSFT"}}
    with pytest.raises(ValueError, match="screener_symbol_universe"):
        D._build_screener_runtime({"universe": u})
