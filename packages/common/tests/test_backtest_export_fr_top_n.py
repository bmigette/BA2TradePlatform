"""``--fr-top-n-below-pool`` export/deploy parity: a repaired trial's exported
``expert_params["top_n"]`` must carry the REPAIRED value (so a deploy gets what was actually
scored), and the repair must be visible on the payload (``fr_top_n_repaired``).

Mirrors ``test_backtest_export.py::test_bypass_overlay_only_when_bypass_check_true``'s block
shape -- this is the same bypass-screener branch, with ``fr_top_n_below_pool`` layered on.
"""
from datetime import datetime
from types import SimpleNamespace

from ba2_common.export import backtest_export as be


def _bt(**kw):
    base = dict(id=11, name="TOP1", expert_name="FactorRanker", engine_type="daily_expert",
                strategy_params={"model:top_n": 25, "screener:screener_max_stocks": 20},
                start_date=datetime(2024, 1, 1), end_date=datetime(2024, 6, 1),
                initial_capital=10_000.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _block(fr_top_n_below_pool=None):
    block = {"experts": [{"class": "FactorRanker", "settings": {"universe_source": "static"}}],
             "account_settings": {}, "screener_opt": {
                 "store": "sp500", "base_settings": {}, "cadence_days": 7,
                 "apply_to_expert_settings": True}}
    if fr_top_n_below_pool is not None:
        block["fr_top_n_below_pool"] = fr_top_n_below_pool
    return block


def _export(bt, block):
    return be.derive_export_payload(bt, "expert_settings", opt_backtest_block=block,
                                    bypass_check=lambda name: name == "FactorRanker")


def test_flag_absent_leaves_top_n_untouched_and_no_marker():
    p = _export(_bt(), _block())
    assert p["settings"]["expert_params"]["top_n"] == 25
    assert "fr_top_n_repaired" not in p


def test_flag_false_leaves_top_n_untouched_and_no_marker():
    p = _export(_bt(), _block(fr_top_n_below_pool=False))
    assert p["settings"]["expert_params"]["top_n"] == 25
    assert "fr_top_n_repaired" not in p


def test_flag_true_25_over_20_repairs_to_15_with_marker():
    p = _export(_bt(), _block(fr_top_n_below_pool=True))
    assert p["settings"]["expert_params"]["top_n"] == 15
    assert p["settings"]["expert_params"]["screener_max_stocks"] == 20
    assert p["fr_top_n_repaired"] is True


def test_flag_true_equal_20_over_20_repairs_to_15():
    bt = _bt(strategy_params={"model:top_n": 20, "screener:screener_max_stocks": 20})
    p = _export(bt, _block(fr_top_n_below_pool=True))
    assert p["settings"]["expert_params"]["top_n"] == 15
    assert p["fr_top_n_repaired"] is True


def test_flag_true_15_over_20_is_unchanged_no_marker():
    bt = _bt(strategy_params={"model:top_n": 15, "screener:screener_max_stocks": 20})
    p = _export(bt, _block(fr_top_n_below_pool=True))
    assert p["settings"]["expert_params"]["top_n"] == 15
    assert "fr_top_n_repaired" not in p


def test_flag_true_40_over_10_repairs_to_the_floor_of_5():
    bt = _bt(strategy_params={"model:top_n": 40, "screener:screener_max_stocks": 10})
    p = _export(bt, _block(fr_top_n_below_pool=True))
    assert p["settings"]["expert_params"]["top_n"] == 5
    assert p["fr_top_n_repaired"] is True


def test_flag_true_but_non_bypass_export_never_repairs():
    """bypass_check False -> the static (non-screener) overlay is exported, which carries no
    screener_max_stocks key at all -- the repair is a no-op regardless of the flag."""
    block = _block(fr_top_n_below_pool=True)
    p = be.derive_export_payload(_bt(), "expert_settings", opt_backtest_block=block,
                                 bypass_check=lambda name: False)
    assert p["settings"]["expert_params"]["top_n"] == 25
    assert "fr_top_n_repaired" not in p
