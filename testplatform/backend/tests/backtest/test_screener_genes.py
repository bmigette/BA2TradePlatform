from app.services.strategy_param_space import collect_param_space, decode_params
import app.services.strategy_optimization_handler as H


class _Strat:  # minimal stand-in
    initial_tp_percent = 5.0
    initial_sl_percent = 5.0
    buy_entry_conditions = None
    sell_entry_conditions = None
    exit_conditions = []


def test_collect_screener_adds_namespaced_genes():
    space = collect_param_space(
        _Strat(), expert_cfg={"params": {}}, bypass=True,
        screener_cfg={
            "screener_market_cap_min": {"min": 1e9, "max": 5e9, "step": 1e9, "type": "float", "optimize": True},
            "screener_relative_volume_min": {"min": 1.0, "max": 2.0, "step": 0.1, "type": "float", "optimize": True},
        })
    assert "screener:screener_market_cap_min" in space
    assert "screener:screener_relative_volume_min" in space


def test_decode_screener_overrides():
    out = decode_params(_Strat(), {
        "tp": 6.0, "sl": 4.0,
        "screener:screener_market_cap_min": 2e9,
        "screener:screener_relative_volume_min": 1.4,
    })
    assert out["screener_overrides"] == {
        "screener_market_cap_min": 2e9, "screener_relative_volume_min": 1.4}
    assert out["tp"] == 6.0  # existing fields still present


def test_trial_config_carries_screener_runtime(tmp_path):
    """End-to-end seam: a run with a ``screener_opt`` block hoists the store and each trial's
    config carries a ``screener_runtime`` whose settings = base overlaid with the individual's
    decoded screener overrides. Non-screener runs leave ``screener_runtime`` None."""
    import pandas as pd
    from ba2_providers.screener import metric_store as ms

    store = str(tmp_path / "s")
    ms.write_partitions(store, pd.DataFrame({
        "symbol": ["AAA"], "date": ["2023-01-31"], "close": [10.0],
        "market_cap": [3e9], "relative_volume": [1.6], "price_drop_pct": [20.0],
        "sector": ["T"], "volume": [2e6], "price": [10.0]}))
    ms.clear_store_memo()  # ensure load_store reads fresh from disk

    backtest_cfg = {
        "backtest_id": 99,
        "start_date": "2023-01-02",
        "end_date": "2023-02-28",
        "enabled_instruments": ["AAA"],
        "experts": [{"class": "FMPRating", "settings": {}}],
        "initial_capital": 100000.0,
        "account_settings": {"starting_cash": 100000.0},
        "warmup_days": 30,
        "seed": 7,
        # The screener-settings optimization config option (cadence is a config option, weekly).
        "screener_opt": {
            "store": store,
            "base_settings": {"screener_relative_volume_min": 1.2, "screener_max_stocks": 10},
            "cadence_days": 7,
        },
    }
    hoisted = H._build_hoisted_state(backtest_cfg)
    assert hoisted["screener_store"] == store
    assert hoisted["screener_cadence_days"] == 7

    decoded = {
        "tp": 5.0, "sl": 5.0,
        "expert_overrides": {},
        "screener_overrides": {"screener_market_cap_min": 2.5e9, "screener_relative_volume_min": 1.5},
        "buy_tree": None, "sell_tree": None, "exit_rules": [],
    }
    cfg = H._build_daily_trial_config(backtest_cfg, decoded, hoisted)
    rt = cfg["screener_runtime"]
    assert rt["store"] == store
    assert rt["cadence_days"] == 7
    # The per-individual override wins over the base; base-only keys survive.
    assert rt["settings"]["screener_market_cap_min"] == 2.5e9
    assert rt["settings"]["screener_relative_volume_min"] == 1.5   # override beats base 1.2
    assert rt["settings"]["screener_max_stocks"] == 10             # base-only, preserved

    # A run WITHOUT screener_opt -> hoisted has no store -> screener_runtime is None (no-op).
    plain = {k: v for k, v in backtest_cfg.items() if k != "screener_opt"}
    plain_hoisted = H._build_hoisted_state(plain)
    plain_cfg = H._build_daily_trial_config(plain, decoded, plain_hoisted)
    assert plain_cfg["screener_runtime"] is None
