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

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_screener_launch_shared as S  # noqa: E402
from test_launcher_seed_population import (  # noqa: E402,F401
    _bundle, _fill_for, _no_style_leaks, _space_of, style_cfg)

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
def _gene_space():
    return {f"screener:{k}": {"min": v["min"], "max": v["max"], "step": v["step"], "type": v["type"]}
            for k, v in us.SCREENER_OPT.items()}


def test_seed_fill_is_the_loosest_screen_of_each_declared_range():
    space = _gene_space()
    space["model:x"] = {"min": 0, "max": 1, "step": 1, "type": "int"}
    fill, notes = S.L._screener_seed_fill(space)
    assert set(fill) == {k for k in space if k.startswith("screener:")} and notes == []
    assert fill["screener:screener_relative_volume_min"] == 0 and fill["screener:screener_price_drop_pct"] == 0
    assert fill["screener:screener_weinstein_stage2_only"] == 0
    assert fill["screener:screener_max_stocks"] == 50                 # the LARGEST cut = the loosest
    assert fill["screener:screener_market_cap_min"] == us.SCREENER_OPT["screener_market_cap_min"]["min"]


def test_seed_fill_upper_bound_genes_and_a_nonzero_drop_floor_note():
    space = {"screener:screener_price_max": {"min": 0, "max": 300, "step": 50, "type": "float"},
             "screener:screener_market_cap_max": {"min": 1e9, "max": 5e9, "step": 1e9, "type": "float"},
             "screener:screener_price_drop_pct": {"min": 5.0, "max": 25.0, "step": 1.0, "type": "float"}}
    fill, notes = S.L._screener_seed_fill(space)
    assert fill["screener:screener_price_max"] == 0                   # the range reaches 0 = no ceiling
    assert fill["screener:screener_market_cap_max"] == 5e9            # otherwise the largest ceiling
    assert fill["screener:screener_price_drop_pct"] == 5.0
    assert len(notes) == 1 and "screener_price_drop_pct" in notes[0] and "cannot be switched off" in notes[0]


def test_the_bundles_own_fill_wins_on_an_identical_key_and_on_a_glob():
    space = _gene_space()
    fill, _ = S.L._screener_seed_fill(space, {"screener:screener_max_stocks": 20, "screener:*weinstein*": 1})
    assert fill["screener:screener_max_stocks"] == 20 and fill["screener:*weinstein*"] == 1
    # the covered genes are not computed (so a categorical gene the bundle states is not refused), the rest are
    assert "screener:screener_weinstein_stage2_only" not in fill and fill["screener:screener_price_drop_pct"] == 0
    # ordering: the bundle's patterns are consulted first
    assert list(fill)[:2] == ["screener:screener_max_stocks", "screener:*weinstein*"]


@pytest.mark.parametrize("gene,cfg", [
    ("screener:screener_sort_metric", {"type": "choice", "choices": ["market_cap", "volume"]}),
    ("screener:screener_mystery", {"min": 0, "max": 1, "step": 1, "type": "int"}),
    ("screener:screener_market_cap_min", {"min": None, "max": 1, "step": 1, "type": "int"}),
])
def test_seed_fill_refuses_loudly_never_a_keyerror(gene, cfg):
    with pytest.raises(SystemExit, match="no loosest-value rule"):
        S.L._screener_seed_fill({gene: cfg})


def test_seed_fill_end_to_end_through_assemble_seed_population(style_cfg, tmp_path, monkeypatch, capsys):
    """Stage-1 style seeds carry no screener gene: the assembled seeds get the loosest screen, the bundle's fill wins."""
    fill = _fill_for(style_cfg, monkeypatch)
    path, space = _bundle(style_cfg, tmp_path, monkeypatch, fill={**fill, "screener:screener_max_stocks": 20})
    bundle = json.loads(path.read_text())
    seen = _space_of(style_cfg, monkeypatch)
    space2 = {**space, **_gene_space()}
    seeds, prov = S.L._assemble_seed_population(bundle, seen["strat"], space2, seen["anchor"], 10)
    assert len(seeds) == 3 and all(set(g) == set(space2) for g in seeds)
    for g in seeds:
        assert g["screener:screener_max_stocks"] == 20                # the bundle's value, not the seed fill's 50
        assert g["screener:screener_relative_volume_min"] == 0 and g["screener:screener_price_drop_pct"] == 0
        assert g["screener:screener_weinstein_stage2_only"] == 0
        assert g["screener:screener_market_cap_min"] == us.SCREENER_OPT["screener_market_cap_min"]["min"]
    assert "screener:screener_relative_volume_min" in prov[0]["fills"]
    out = capsys.readouterr().out
    assert "seed screener fill" in out and "screener:screener_relative_volume_min" in out


def test_seed_fill_end_to_end_refuses_a_categorical_screener_gene(style_cfg, tmp_path, monkeypatch):
    path, space = _bundle(style_cfg, tmp_path, monkeypatch, fill=_fill_for(style_cfg, monkeypatch))
    seen = _space_of(style_cfg, monkeypatch)
    space2 = {**space, "screener:screener_sort_metric": {"type": "choice", "choices": ["a", "b"], "min": 0, "max": 1, "step": 1}}
    with pytest.raises(SystemExit, match="screener_sort_metric"):
        S.L._assemble_seed_population(json.loads(path.read_text()), seen["strat"], space2, seen["anchor"], 10)


# ------------------------------------------------------------------------------------------ standalone refusal
def test_standalone_screener_backtest_refuses_a_restricted_universe():
    from app.services.backtest import daily_backtest_handler as D
    u = {"mode": "screener", "screener_store": "x", "screener_settings": {"screener_symbol_universe": "AAPL,MSFT"}}
    with pytest.raises(ValueError, match="screener_symbol_universe"):
        D._build_screener_runtime({"universe": u})


# ------------------------------------------------------------------------------------------ replay paths
def test_a_stored_empty_restriction_is_refused_not_replayed_unrestricted(env):
    for stored in ({"symbols": []}, {}, {"uncovered": []}):
        syms = sl.stored_restriction_symbols({"universe_restriction": stored})
        assert syms == []                                              # present but unusable: NOT None
        with pytest.raises(sl.ScreenerLaunchRefused, match="no symbols"):
            _prep(env, universe_restriction=syms)
    assert sl.stored_restriction_symbols({}) is None                   # no key = an unrestricted job
    assert sl.stored_restriction_symbols({"universe_restriction": {"symbols": ["A", "B"]}}) == ["A", "B"]
    with pytest.raises(sl.ScreenerLaunchRefused, match="not an object"):
        sl.stored_restriction_symbols({"universe_restriction": ["A"]})


def _stored_block(env, prep):
    start, end = S._window(env)
    return {"backtest_id": 7, "start_date": start, "end_date": end, "enabled_instruments": list(prep.enabled),
            "execution_interval": "1d", "ohlcv_history_source": "fmp",
            "experts": [{"class": "FMPRating", "settings": {}}], "initial_capital": 100000.0,
            "account_settings": {"starting_cash": 100000.0}, "warmup_days": 30, "seed": 42,
            "screener_universe_rule": us.RULE_ID, "screener_opt": prep.screener_opt}


PARAMETER_RANGES = {f"screener:{k}": dict(v) for k, v in us.SCREENER_OPT.items()}


def test_recompute_static_universe_applies_the_stored_restriction(env):
    from app.services.backtest import rerun_handler as RH
    keep = _symbols(env)[:15]
    prep = _prep(env, universe_restriction=keep)
    out = RH.recompute_static_universe(_stored_block(env, prep), PARAMETER_RANGES, row_id=1)
    assert set(out["enabled_instruments"]) <= set(keep) and out["enabled_instruments"]
    free = RH.recompute_static_universe(_stored_block(env, _prep(env)) | {}, PARAMETER_RANGES, row_id=2)
    assert len(free["enabled_instruments"]) > len(out["enabled_instruments"])


def test_recompute_static_universe_refuses_an_empty_stored_restriction(env):
    from app.services.backtest import rerun_handler as RH
    prep = _prep(env, universe_restriction=_symbols(env)[:15])
    blk = _stored_block(env, prep)
    blk["screener_opt"] = {**blk["screener_opt"], "universe_restriction": {"symbols": []}}
    with pytest.raises(sg.ScreenerGateRefusal, match="empty universe_restriction"):
        RH.recompute_static_universe(blk, PARAMETER_RANGES, row_id=3)


def test_recompute_static_universe_refuses_a_restriction_without_the_panel_gate(env):
    from app.services.backtest import rerun_handler as RH
    prep = _prep(env, universe_restriction=_symbols(env)[:15])
    blk = _stored_block(env, prep)
    so = {k: v for k, v in blk["screener_opt"].items() if k not in ("criteria_version", "panel", "panel_fingerprint")}
    blk["screener_opt"] = {**so, "store": env["store"]}
    with pytest.raises(ValueError, match="needs the daily-panel gate"):
        RH.recompute_static_universe(blk, PARAMETER_RANGES, row_id=4)


# ------------------------------------------------------------------------------------------ trial config
def _trial_cfg(env, restriction):
    prep = _prep(env, universe_restriction=restriction)
    cfg = _stored_block(env, prep)
    decoded = {"tp": 8.0, "sl": 3.0, "expert_overrides": {}, "buy_tree": None, "sell_tree": None,
               "exit_rules": [], "entry_rules": [],
               "screener_overrides": {"screener_market_cap_min": 2e9, "screener_relative_volume_min": 0,
                                      "screener_price_drop_pct": 0, "screener_price_drop_days": 5,
                                      "screener_max_stocks": 10, "screener_weinstein_stage2_only": 0}}
    from app.services import strategy_optimization_handler as H
    return H._build_daily_trial_config(cfg, decoded, H._build_hoisted_state(cfg), option_trade_records=False), prep


def test_universe_restriction_reaches_the_trial_runtime_and_the_panel_gate(env):
    keep = _symbols(env)[:15]
    trial, prep = _trial_cfg(env, keep)
    rt = trial["screener_runtime"]
    assert rt["universe_restriction"] == sorted(keep)
    assert set(trial["enabled_instruments"]) <= set(keep)               # the per-trial candidate bound is restricted too

    class _PS:
        interval = "1d"
        def screener_now_price(self, sym, at): return 50.0
        def screener_session_high(self, sym, at): return 50.0
        def has_symbol(self, sym): return True
    gate = sg.PanelGate(rt, _PS(), intraday=False, history_source="fmp")
    assert gate.valid.sum() == len(keep)
    day = datetime.fromisoformat(S._window(env)[0]) + timedelta(days=7)
    picked = gate.symbols(day)
    assert picked and set(picked) <= set(keep) and len(picked) <= 10


def test_an_unrestricted_trial_runtime_has_no_restriction_key(env):
    trial, _ = _trial_cfg(env, None)
    assert "universe_restriction" not in trial["screener_runtime"]


# ------------------------------------------------------------------------------------------ one parser, both sides
def test_universe_file_parsing_comments_blanks_and_share_class_spelling(tmp_path):
    from ba2_providers.screener import symbol_universe as su
    f = tmp_path / "u.txt"
    f.write_text("# header\n\naapl   # trailing comment\nBRK.B\nbf-b, msft\n   \n#MSFT2\nAAPL\n", encoding="utf-8")
    assert su.read_universe_file(str(f)) == ["AAPL", "BF-B", "BRK-B", "MSFT"]
    assert su.parse_symbols("brk.b,AAPL\n# x\nZZZ # y") == ["AAPL", "BRK-B", "ZZZ"]
    with pytest.raises(ValueError):
        su.parse_symbols(5)
    # the launcher flag and the live setting use these very functions
    ns = _args(screener_universe_file=str(f), screener=True)
    assert S.L._screener_universe_restriction(ns) == ["AAPL", "BF-B", "BRK-B", "MSFT"]
    assert su.parse_symbol_universe("brk.b, AAPL # c") == frozenset({"BRK-B", "AAPL"})


def test_the_restriction_stamp_is_canonical_so_the_exported_list_matches_the_panel(env):
    syms = _symbols(env)
    got = _prep(env, universe_restriction=[syms[0].lower(), syms[1]])
    assert got.screener_opt["universe_restriction"]["symbols"] == sorted({syms[0], syms[1]})
