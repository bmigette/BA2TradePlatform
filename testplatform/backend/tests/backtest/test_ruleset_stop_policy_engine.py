"""B4 in the real backtest engine: one stop-loss policy for every ruleset path.

  * No-impact: with the default setting (``allow_ruleset_sl_loosen`` off) a fixed small run gives
    identical orders, trades and equity with the policy as it is now and with the pre-B4 decision
    logic patched back in (the SL-only ratchet, and NO ratchet on the merged TP+SL branch). The
    fixture exercises both branches: its entry bracket carries TP+SL (the merged branch, on a
    transaction with no stop yet -- the live common case) and an open-positions rule keeps asking
    for a looser stop (the SL-only ratchet).
  * Setting on: a rule loosening the stop moves it to AT MOST the recorded max-loss stop, through
    the SL-only branch and through the merged branch; with the setting off both branches keep the
    tighter stop and the merged branch still applies its take-profit (the backtest account's
    ``adjust_tp_sl`` receives ``None`` for the SL).
  * The setting is an ordinary genome setting: it survives the per-trial config whitelist, is not
    pinned by INERT_RM_TOGGLES and is not a forced backtest setting.

Run from the backend dir:
    python -m pytest tests/backtest/test_ruleset_stop_policy_engine.py -v
"""
from __future__ import annotations

import importlib
from datetime import date, datetime

import pytest

from ba2_common.core.position_sizing import max_loss_stop_of

from tests.backtest.test_max_loss_stop_engine import (
    CFG, CHOPPY, _MaxLossStubExpert, _store_mode,
)

TA = importlib.import_module("ba2_common.core.TradeActions")
TAE = importlib.import_module("ba2_common.core.TradeActionEvaluator")

SETTING = "allow_ruleset_sl_loosen"

# Entry on 2024-01-02 (close 100) fills at the next open (100); price then rises and never comes
# near a stop, so every stop move the manage pass makes is visible at the end of the run.
RISING = [
    (date(2024, 1, 2), 100, 101, 99, 100),
    (date(2024, 1, 3), 100, 112, 100, 110),
    (date(2024, 1, 4), 110, 122, 109, 120),
    (date(2024, 1, 5), 120, 125, 118, 122),
    (date(2024, 1, 8), 122, 126, 120, 124),
]

_ALWAYS = {"id": "g", "operator": "AND", "conditions": [
    {"id": "c", "field": "profit_loss_percent", "op": ">=", "value": -1000}]}


def _adjust(action_type, value):
    return {"id": f"{action_type}-{value:g}", "action_type": action_type,
            "reference_value": "order_open_price", "action_value": value}


def _exit_rule(*actions, rid="manage"):
    return {"id": rid, "name": rid, "conditions": _ALWAYS, "actions": list(actions)}


def _run(bars, *, run_id, inmem, entry_actions, exit_rules, loosen=None, buy_on=None):
    """Full engine.run() with deterministic risk_atr sizing (ATR off, 8% risk and 8% floor -> the
    safeguard, and so the max-loss stop, is exactly 8% under the signal close). ``loosen`` None
    leaves the setting unset (the interface default). Returns the outcome and every entry
    transaction's (stop_loss, take_profit, max_loss_stop), read BEFORE teardown."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_exit_ruleset_from_rules, seed_ruleset_from_tree)
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection

    account_id = expert_id = run_id
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"ruleset-stop-policy-{run_id}")
    ctx.__enter__()
    try:
        from ba2_common.core import trade_store
        assert trade_store.inmem_trades_active() == (inmem == "1"), "store mode is not the one asked for"
        seed_account_definition(account_id, CFG)
        enter_id = seed_ruleset_from_tree(None, entry_actions=entry_actions)
        open_id = seed_exit_ruleset_from_rules(exit_rules, name=f"stop-policy-open-{run_id}")
        seed_expert_instance(account_id=account_id, expert_class_name="_MaxLossStubExpert",
                             enter_market_ruleset_id=enter_id, open_positions_ruleset_id=open_id,
                             instance_id=expert_id)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c,
                               "Volume": 1000} for (d, o, h, low, c) in bars])
        account = BacktestAccount(account_id, ps, CFG)
        resolver.register_account(account_id, account)
        expert = _MaxLossStubExpert(expert_id, ps, buy_on=buy_on)
        settings = {
            "allow_automated_trade_opening": (True, "bool"),
            "allow_automated_trade_modification": (True, "bool"),
            "enable_buy": (True, "bool"),
            "sizing_mode": ("risk_atr", "str"),
            "risk_per_trade_pct": (8.0, "float"),
            "min_stop_loss_pct": (8.0, "float"),
            "use_atr_stop": (False, "bool"),
        }
        if loosen is not None:
            # The GA's own encoding: an integer gene, written the way _build_experts writes it.
            settings[SETTING] = (loosen, "int")
        expert.save_settings(settings)
        resolver.register_expert(expert_id, expert)
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, {}, enter_id)], price_source=ps,
            config={"start_date": datetime.combine(bars[0][0], datetime.min.time()),
                    "end_date": datetime.combine(bars[-1][0], datetime.min.time()),
                    "enabled_instruments": ["AAPL"], "seed": 42},
            indicator_provider=None)
        engine._indicator_provider = None
        engine.run()

        entries = [o for o in account.get_orders()
                   if o.symbol == "AAPL" and o.side == OrderDirection.BUY and o.depends_on_order is None]
        stops = []
        for o in sorted(entries, key=lambda o: o.id):
            txn = get_instance(Transaction, o.transaction_id)
            stops.append((txn.stop_loss, txn.take_profit, max_loss_stop_of(txn)))
        orders = sorted(
            (o.side.value, o.order_type.value, o.status.value, o.quantity, o.filled_qty,
             o.open_price, o.stop_price, o.limit_price, o.depends_on_order is None)
            for o in account.get_orders())
        outcome = {
            "orders": orders,
            "trades": account.get_round_trip_trades(),
            "equity": list(account.get_balance_history()),
        }
        return outcome, stops
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# No-impact with the default setting
# --------------------------------------------------------------------------- #

def _legacy_sl_only(transaction, requested, is_long, expert, **_):
    """The pre-B4 ``AdjustStopLossAction._call_broker`` decision, verbatim in effect."""
    existing = getattr(transaction, "stop_loss", None)
    if existing and existing > 0 and requested:
        if (requested < existing) if is_long else (requested > existing):
            return existing, "ratchet"
    return requested, "legacy"


def _legacy_merged(transaction, requested, is_long, expert, **_):
    """The pre-B4 merged TP+SL branch: no policy at all -- the computed SL was sent as is."""
    return requested, "legacy"


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_default_setting_changes_no_trade(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    run_id = 610 + (100 if inmem == "0" else 0)
    entry = [_adjust("adjust_take_profit", 30.0), _adjust("adjust_stop_loss", -3.0)]
    exits = [_exit_rule(_adjust("adjust_stop_loss", -15.0))]    # always asks to LOOSEN

    seen = []
    real = TA.ruleset_stop_policy

    def recording(site):
        def policy(*a, **k):
            result = real(*a, **k)
            seen.append((site, result[1]))
            return result
        return policy

    monkeypatch.setattr(TA, "ruleset_stop_policy", recording("sl_only"))
    monkeypatch.setattr(TAE, "ruleset_stop_policy", recording("merged"))
    now, stops = _run(CHOPPY, run_id=run_id, inmem=inmem, entry_actions=entry, exit_rules=exits)
    assert len(stops) >= 2 and len(now["trades"]) >= 2, "the fixture must re-enter after a stop-out"
    assert ("sl_only", "ratchet") in seen, "the fixture must exercise the SL-only ratchet"
    assert ("merged", "no_existing_stop") in seen, "the fixture must exercise the merged entry bracket"

    monkeypatch.setattr(TA, "ruleset_stop_policy", _legacy_sl_only)
    monkeypatch.setattr(TAE, "ruleset_stop_policy", _legacy_merged)
    before, stops_before = _run(CHOPPY, run_id=run_id + 1, inmem=inmem,
                                entry_actions=entry, exit_rules=exits)

    assert now["orders"] == before["orders"]
    assert now["trades"] == before["trades"]
    assert now["equity"] == before["equity"]
    assert stops == stops_before


# --------------------------------------------------------------------------- #
# The loosen setting in the real engine
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
@pytest.mark.parametrize("merged", [False, True], ids=["sl-only", "merged-tp-sl"])
@pytest.mark.parametrize("loosen, expected_sl", [
    (None, 97.0),   # setting unset (interface default off): the ratchet keeps the entry stop
    (0, 97.0),      # explicitly off
    (1, 92.0),      # on: the -15% request (85) is clamped at the max-loss stop (92)
], ids=["unset", "off", "on"])
def test_a_loosening_rule_stops_at_the_max_loss_stop(monkeypatch, inmem, merged, loosen, expected_sl):
    _store_mode(monkeypatch, inmem)
    run_id = (620 + (10 if merged else 0) + {None: 0, 0: 1, 1: 2}[loosen]
              + (100 if inmem == "0" else 0))
    entry = [_adjust("adjust_stop_loss", -3.0)]     # entry stop 97 (tighter than the 92 safeguard)
    actions = [_adjust("adjust_stop_loss", -15.0)]
    if merged:
        actions = [_adjust("adjust_take_profit", 50.0)] + actions
    _, stops = _run(RISING, run_id=run_id, inmem=inmem, entry_actions=entry,
                    exit_rules=[_exit_rule(*actions)], loosen=loosen, buy_on={date(2024, 1, 2)})
    assert len(stops) == 1, "exactly one entry expected"
    stop_loss, take_profit, max_loss = stops[0]
    assert max_loss == pytest.approx(92.0), "B3 fixture drifted: the max-loss stop is not the safeguard"
    assert stop_loss == pytest.approx(expected_sl)
    assert stop_loss >= max_loss, "a ruleset stop must never be looser than the max-loss stop"
    if merged:
        assert take_profit == pytest.approx(150.0), "the merged branch's TP half must still apply"


# --------------------------------------------------------------------------- #
# The setting is an ordinary genome setting
# --------------------------------------------------------------------------- #

def test_the_setting_is_neither_pinned_nor_forced():
    import ba2test_launcher as L
    from app.services.strategy_param_space import INERT_RM_TOGGLES
    from ba2_common.core.deploy_parity import BacktestRunFacts, forced_expert_settings

    assert SETTING not in INERT_RM_TOGGLES
    assert SETTING not in L._INERT_RM_TOGGLES
    facts = BacktestRunFacts(enable_short=False, hold_assigned_stock=False, entry_action=None)
    assert SETTING not in forced_expert_settings(facts)


@pytest.mark.parametrize("gene", [1, 0])
def test_a_genome_value_survives_the_trial_config_whitelist(gene):
    """``model:allow_ruleset_sl_loosen`` decodes into expert_overrides; the per-trial config must
    carry it to the expert, and the INERT_RM_TOGGLES merged last must not overwrite it."""
    from app.services.backtest.daily_backtest_handler import _expert_decision_settings
    from app.services.strategy_optimization_handler import _build_daily_trial_config
    from ba2_experts.FMPRating import FMPRating

    backtest_cfg = {
        "backtest_id": "stop-policy-trial", "name": "unit",
        "start_date": "2024-01-01", "end_date": "2024-06-30",
        "initial_capital": 100000.0, "account_settings": {}, "warmup_days": 0, "seed": 1,
        "experts": [{"class": "FMPRating", "settings": {}}],
        "enabled_instruments": ["AAPL"],
    }
    cfg = _build_daily_trial_config(backtest_cfg, {"expert_overrides": {SETTING: gene}},
                                    option_trade_records=False)
    settings = cfg["experts"][0]["settings"]
    assert settings[SETTING] == gene
    assert _expert_decision_settings(FMPRating, settings)[SETTING] == gene
