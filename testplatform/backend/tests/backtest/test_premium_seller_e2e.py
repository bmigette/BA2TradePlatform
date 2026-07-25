"""E2E (spec §10): real BacktestAccount + seeded OPRA cache + real PremiumSeller
signal/structure/manager code (resolver bypassed via __new__ — the resolver-backed
__init__ is FactorPortfolioManager's proven pattern, seam-tested separately).

D1: a 38-DTE chain with a sellable put spread -> analyze_as_of emits the spec,
    rebalance opens it (limit SELL parent, option_strategy='put_credit_spread',
    expert-attributed transaction).
D2: the spread's net premium more than halves -> manage_open closes at the 50%
    capture rule.

Seeded-quote math (ZERO-SPREAD snapshot -> point-in-time bid == ask == bar close,
the same idiom as test_spread_pnl_condition.py; a wide snapshot spread would make
the D2 quotes close +/- half-spread and shift the capture boundary):
  * builder credit (structures._mid_credit: sell at short BID, buy at long ASK)
    = 1.50 - 0.90 = 0.60 -> rebalance records txn.open_price = -0.60, so the 50%
    capture threshold on the net spread value is 0.30.
  * qty = floor(risk_per_structure_pct% x balance / ((width - credit) x 100))
        = floor(10% x 10_000 / ((5.0 - 0.60) x 100)) = floor(1000 / 440) = 2.
  * D2 net close = 1.02 - 0.75 = 0.27 < 0.30 -> (0.60 - 0.27) / 0.60 = +55%
    captured — CLEARLY past the 50% rule (an exact 0.30 would be a float-boundary
    coin flip).
"""
from datetime import datetime

import pytest

AS_OF = datetime(2024, 1, 2)
D2 = datetime(2024, 1, 3)
EXP = "2024-02-09"
SHORT, LONG = "XYZ240209P00095000", "XYZ240209P00090000"

CFG = {"starting_cash": 10_000.0, "commission_per_trade": 0.0,
       "slippage_bps": 0.0, "fill_model": "next_bar_open"}

SETTINGS = {
    "static_universe": "XYZ",
    "iv_rank_enabled": False, "iv_rank_min": 50.0,
    "iv_hv_enabled": False, "iv_hv_min_pp": 2.0, "hv_lookback": 20,
    "trend_filter_enabled": False, "trend_sma": 200,
    "earnings_filter_enabled": False,
    "fmp_rating_floor_enabled": False, "fmp_rating_min": 3.0,
    "target_delta": 0.30, "target_dte": 38, "spread_width": 5.0, "min_credit_ratio": 0.05,
    "enable_put_credit_spread": True, "enable_short_put": False, "enable_short_strangle": False,
    "risk_per_structure_pct": 10.0,
    "profit_capture_pct": 50.0, "strangle_capture_pct": 25.0,
    "tested_delta_enabled": False, "tested_delta": 0.30, "roll_dte": 21,
    "dr_stop_enabled": False, "dr_stop_credit_mult": 2.0,
    "ur_stop_enabled": True, "ur_stop_credit_mult": 2.0,
    "max_deployment_pct": 40.0, "undefined_risk_max_pct": 20.0,
    "max_notional_leverage": 3.0, "max_concurrent_structures": 5,
    "circuit_breaker_pct": 50.0,
}


def _seed(cache_db):
    from app.services.backtest.options_cache import OptionsHistoryCache
    cache = OptionsHistoryCache(cache_db)
    # Zero-spread chain snapshot (bid == ask == last) as of the day before D1.
    cache.write_chain_rows("XYZ", "2024-01-01", [
        {"occ_symbol": SHORT, "option_type": "put", "strike": 95.0, "expiry": EXP,
         "bid": 1.50, "ask": 1.50, "last": 1.50, "iv": 0.30, "delta": -0.30,
         "open_interest": 500, "volume": 100},
        {"occ_symbol": LONG, "option_type": "put", "strike": 90.0, "expiry": EXP,
         "bid": 0.90, "ask": 0.90, "last": 0.90, "iv": 0.30, "delta": -0.20,
         "open_interest": 500, "volume": 100},
    ])
    # D2 premium bars: net spread 1.02 - 0.75 = 0.27 < 50% of the 0.60 credit (0.30)
    # -> +55% captured, the profit-capture rule fires (clear of the float boundary).
    cache.write_bar_rows([
        {"occ_symbol": SHORT, "date": "2024-01-03", "open": 1.0, "high": 1.1, "low": 0.9,
         "close": 1.02, "volume": 100, "underlying": "XYZ", "option_type": "put",
         "strike": 95.0, "expiry": EXP},
        {"occ_symbol": LONG, "date": "2024-01-03", "open": 0.7, "high": 0.8, "low": 0.6,
         "close": 0.75, "volume": 100, "underlying": "XYZ", "option_type": "put",
         "strike": 90.0, "expiry": EXP},
    ])


@pytest.fixture
def env(tmp_path):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from ba2_common.core.backtest_context import BacktestContext
    from ba2_experts.PremiumSeller import PremiumSeller
    from ba2_experts.PremiumSeller.portfolio import OptionPortfolioManager

    cache_db = str(tmp_path / "opt_cache.sqlite")
    _seed(cache_db)
    wire_backtest_seams()
    ctx = backtest_trading_db("premium-seller-e2e")
    ctx.__enter__()
    seed_account_definition(1, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.set_clock(AS_OF)
    account = BacktestAccount(1, ps, CFG,
                              options_provider=HistoricalOptionsProvider(cache_db))
    wire_backtest_seams().register_account(1, account)

    expert = PremiumSeller.__new__(PremiumSeller)
    expert._iv_history = {}
    # Manager/expert settings accessor — the same dict the BacktestContext carries.
    expert.get_setting_with_interface_default = lambda name, log_warning=False: SETTINGS[name]
    bt_ctx = BacktestContext(providers=None, settings=dict(SETTINGS), as_of=AS_OF,
                             account=account, subtype=None)

    pm = OptionPortfolioManager.__new__(OptionPortfolioManager)
    pm.expert_instance_id = 1
    pm.expert = expert
    pm.account = account
    pm._peak_equity = None
    pm._halted = False
    yield account, expert, bt_ctx, pm
    ctx.__exit__(None, None, None)


def test_open_then_capture_close(env):
    account, expert, bt_ctx, pm = env
    rec = expert.analyze_as_of(AS_OF, bt_ctx)
    specs = rec.raw_outputs["targets"]["structures"]
    assert len(specs) == 1 and specs[0].strategy == "put_credit_spread"
    assert specs[0].qty == 2                    # floor(1000 / ((5.0 - 0.60) x 100)) = 2

    opened = pm.rebalance(rec.raw_outputs["targets"])
    assert len(opened) == 1
    from ba2_common.core.trade_store import orders_where
    parents = [o for o in orders_where(account_id=account.id)
               if getattr(o, "option_strategy", None) == "put_credit_spread"]
    assert len(parents) == 1

    # Simulate the fill + mark the position OPENED with entry fills so manage_open
    # sees a held structure (fill engine is covered elsewhere; this test owns exits).
    from ba2_common.core.db import get_instance, update_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderStatus, TransactionStatus
    parent = parents[0]
    parent.status = OrderStatus.FILLED
    parent.filled_qty = parent.quantity
    parent.open_price = -0.60
    update_instance(parent)
    for leg in orders_where(parent_order_id=parent.id):
        leg.status = OrderStatus.FILLED
        leg.filled_qty = leg.quantity
        leg.open_price = 1.50 if leg.contract_symbol == SHORT else 0.90
        update_instance(leg)
    txn = get_instance(Transaction, parent.transaction_id)
    txn.status = TransactionStatus.OPENED
    update_instance(txn)

    ps_clock = account._price
    ps_clock.set_clock(D2)                       # quotes now more than halve the spread value
    closed = pm.manage_open(D2)
    assert len(closed) == 1
    closes = [o for o in orders_where(transaction_id=txn.id)
              if getattr(o, "option_strategy", None) == "close"]
    assert closes, "expected an offsetting close order on the structure's transaction"
