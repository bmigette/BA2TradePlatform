"""``--fr-top-n-below-pool`` (operator decision 2026-09-29, the "ranking inert" trap):
FactorRanker's ``long_only_top_n`` slices ``ranked[:top_n]``, which discards nothing when
``top_n >= screener_max_stocks`` -- the factor weights have zero effect on selection.

Pins the repair at the ONE place every trial config is assembled
(``strategy_optimization_handler._build_daily_trial_config``), so GA trials, top-N persist,
re-runs and robustness variants all see it:

  * flag ABSENT/False: the decoded top_n gene reaches the trial's expert settings UNCHANGED --
    byte-identical to before this flag existed.
  * flag True: 25/20 -> 15, 20/20 -> 15, 15/20 unchanged (already below the pool), 40/10 -> 5.
  * re-run: rebuilding the trial config twice from the SAME stored backtest_cfg (round-tripped
    through json to mimic a DB read-back) reproduces the identical repair both times.

Run from testplatform/backend:
    python -m pytest tests/backtest/test_fr_top_n_below_pool.py -v
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict

import pytest

RUN_START = datetime(2024, 3, 4)
RUN_END = datetime(2024, 3, 8)
SYMBOLS = ["FRTA", "FRTB", "FRTC", "FRTD"]


def _strategy():
    from app.models.strategy import Strategy
    return Strategy(name="fr-topn-below-pool", entry_rules=[], exit_rules=[])


def _bt_block(*, fr_top_n_below_pool: bool = None) -> Dict[str, Any]:
    """The run-level ``optimization_config['backtest']`` block a --screener FactorRanker
    ``optimize`` job builds (launcher shape) -- a bypass expert with
    ``screener_opt.apply_to_expert_settings=True``."""
    block: Dict[str, Any] = {
        "backtest_id": "fr-topn", "name": "fr-topn-trial",
        "start_date": RUN_START.isoformat(), "end_date": RUN_END.isoformat(),
        "enabled_instruments": list(SYMBOLS),
        "experts": [{"class": "FactorRanker", "settings": {"universe_source": "static"}}],
        "initial_capital": 100_000.0,
        "account_settings": {"starting_cash": 100_000.0, "commission_per_trade": 0.0,
                             "slippage_bps": 0.0, "fill_model": "next_bar_open"},
        "warmup_days": 30, "seed": 7, "subtype": "daily_expert",
        "execution_interval": "1d",
        "screener_opt": {"store": "fake-store", "base_settings": {},
                         "cadence_days": 7, "apply_to_expert_settings": True},
    }
    if fr_top_n_below_pool is not None:
        block["fr_top_n_below_pool"] = fr_top_n_below_pool
    return block


def _hoisted() -> Dict[str, Any]:
    """The subset of ``_build_hoisted_state``'s output ``_build_daily_trial_config`` reads for
    a bypass-expert screener trial -- built by hand so this test needs no real parquet store
    (the store itself is never touched on the bypass path; see
    ``_build_daily_trial_config``'s ``bypass_screener_settings``)."""
    return {"screener_store": "fake-store", "screener_base": {},
            "screener_cadence_days": 7, "screener_apply_to_expert_settings": True}


def _decoded_flat(top_n: int, screener_max_stocks: int) -> Dict[str, Any]:
    return {"model:top_n": top_n, "model:weighting": "equal",
            "screener:screener_max_stocks": screener_max_stocks}


def _trial_settings(top_n: int, screener_max_stocks: int, *, fr_top_n_below_pool=None):
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    strat = _strategy()
    decoded = decode_params(strat, _decoded_flat(top_n, screener_max_stocks))
    bt_block = _bt_block(fr_top_n_below_pool=fr_top_n_below_pool)
    trial = _build_daily_trial_config(bt_block, decoded, _hoisted(), option_trade_records=False)
    return trial["experts"][0]["settings"]


# --------------------------------------------------------------------------------------------- #
# flag absent / False: byte-identical to before the flag existed
# --------------------------------------------------------------------------------------------- #
def test_flag_absent_top_n_reaches_the_trial_unchanged():
    settings = _trial_settings(25, 20)  # fr_top_n_below_pool omitted entirely
    assert settings["top_n"] == 25
    assert settings["screener_max_stocks"] == 20


def test_a_stored_config_from_before_this_flag_existed_decodes_unchanged():
    """Operator decision 2026-09-29: the launcher's default flipped to ON, but a run's STORED
    config (e.g. goal2020, goal2027atr -atr27-fr/-fr2/-fr3, or any pre-flag row) carries no
    fr_top_n_below_pool key at all -- the handler must read that as off regardless of what any
    NEW run's CLI default is, so every historical FactorRanker row re-runs byte-identically."""
    stored_block = json.loads(json.dumps(_bt_block()))  # no fr_top_n_below_pool key, ever
    assert "fr_top_n_below_pool" not in stored_block
    settings = _trial_settings(25, 20)
    assert settings["top_n"] == 25


def test_flag_false_top_n_reaches_the_trial_unchanged():
    settings = _trial_settings(25, 20, fr_top_n_below_pool=False)
    assert settings["top_n"] == 25


def test_flag_on_vs_off_trial_config_differs_only_in_top_n():
    off = _trial_settings(25, 20, fr_top_n_below_pool=False)
    on = _trial_settings(25, 20, fr_top_n_below_pool=True)
    assert off["top_n"] == 25
    assert on["top_n"] == 15
    diff_keys = {k for k in set(off) | set(on) if off.get(k) != on.get(k)}
    assert diff_keys == {"top_n"}


# --------------------------------------------------------------------------------------------- #
# flag on: the four spec'd repair cases
# --------------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("top_n,max_stocks,expected", [
    (25, 20, 15),
    (20, 20, 15),
    (15, 20, 15),   # already below the pool -- no change
    (40, 10, 5),
])
def test_flag_on_repairs(top_n, max_stocks, expected):
    settings = _trial_settings(top_n, max_stocks, fr_top_n_below_pool=True)
    assert settings["top_n"] == expected
    assert settings["screener_max_stocks"] == max_stocks  # the pool size itself is untouched


# --------------------------------------------------------------------------------------------- #
# re-run: rebuilding from the SAME stored (json round-tripped) config reproduces the repair
# --------------------------------------------------------------------------------------------- #
def test_rerun_from_a_stored_config_reproduces_the_repair():
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from app.services.strategy_param_space import decode_params

    strat = _strategy()
    decoded = decode_params(strat, _decoded_flat(25, 20))
    stored_block = json.loads(json.dumps(_bt_block(fr_top_n_below_pool=True)))
    hoisted = _hoisted()

    first = _build_daily_trial_config(stored_block, decoded, hoisted,
                                      option_trade_records=False)
    second = _build_daily_trial_config(stored_block, decoded, hoisted,
                                       option_trade_records=False)
    assert first["experts"][0]["settings"]["top_n"] == 15
    assert second["experts"][0]["settings"]["top_n"] == 15
