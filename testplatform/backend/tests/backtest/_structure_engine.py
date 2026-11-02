"""Engine-driven option-structure fixture: the REAL entry path, not hand-built order dicts.

``run_structure`` wires the full ``DailyBacktestEngine`` with an ENTER_MARKET ruleset whose
action is an option action (``open_bull_call_spread``, ``buy_call``, ``open_iron_condor``, ...),
an always-BUY stub expert, and an AAPL call+put chain priced by Black-Scholes. The decision
bar is 2024-02-01, the fill bar 2024-02-02 (``next_bar_open``), and every contract has a
premium bar on each of the next sessions, so a test can drop GARBAGE prints into chosen
(contract, day) cells through ``overrides``.

The engine selects, sizes, submits and fills the structure itself; the test then reads the
account (orders, lots, results counters) to prove what the engine did.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.option_bs import bs_price
from ba2_common.core.types import OptionRight, OrderRecommendation, Recommendation
from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD

EXPIRY = date(2024, 3, 15)
START = datetime(2024, 2, 1)
END = datetime(2024, 2, 9)
SESSIONS = [date(2024, 2, 1), date(2024, 2, 2), date(2024, 2, 5), date(2024, 2, 6),
            date(2024, 2, 7), date(2024, 2, 8), date(2024, 2, 9)]
STRIKES = [160.0, 165.0, 170.0, 175.0, 180.0, 185.0, 190.0, 195.0, 200.0]
SPOT = 180.0
IV = 0.25
RATE = 0.04

BASE_CFG = {
    **LEGACY_ZERO_SPREAD, "starting_cash": 100_000.0,
    "commission_per_trade": 0.0, "slippage_bps": 0.0, "fill_model": "next_bar_open",
}


def occ(right: str, strike: float, sym: str = "AAPL") -> str:
    return f"{sym}240315{right}{int(round(strike * 1000)):08d}"


def model_price(right: str, strike: float, day: date, spot: float = SPOT) -> float:
    dte = (EXPIRY - day).days
    kind = OptionRight.CALL if right == "C" else OptionRight.PUT
    return round(bs_price(spot, strike, dte, IV, kind, r=RATE), 2)


def implied_iv(price: float, right: str, strike: float, day: date, spot: float = SPOT) -> float:
    """The iv whose Black-Scholes price at ``spot`` on ``day`` is ``price`` (bisection)."""
    kind = OptionRight.CALL if right == "C" else OptionRight.PUT
    dte = (EXPIRY - day).days
    lo, hi = 0.01, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, dte, mid, kind, r=RATE) < price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 6)


class _BuyExpert(MarketExpertInterface):
    """Deterministic always-BUY expert (the enter gate then fires on every flat bar)."""

    bypasses_classic_rm = False

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "stub always-BUY expert"

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        return Recommendation(signal=OrderRecommendation.BUY, confidence=1.0,
                              current_price=SPOT, details="buy", raw_outputs={}, )


def seed_cache(db_path: str, overrides: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
               volume: int = 5000, symbols=("AAPL",)) -> None:
    """A call+put chain (dated at the decision bar) and one premium bar per contract per
    session, every price Black-Scholes at that day's spot. ``overrides`` maps
    ``(occ_symbol, 'YYYY-MM-DD')`` to the bar fields to replace (open/high/low/close/volume)."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    overrides = overrides or {}
    cache = OptionsHistoryCache(db_path)
    for sym_u in symbols:
        chain, bars = [], []
        for right, kind in (("C", "call"), ("P", "put")):
            for k in STRIKES:
                sym = occ(right, k, sym_u)
                mid = model_price(right, k, START.date())
                delta = 0.5 if k == SPOT else (0.7 if (right == "C") == (k < SPOT) else 0.3)
                chain.append({"occ_symbol": sym, "option_type": kind, "strike": k,
                              "expiry": EXPIRY.isoformat(), "bid": round(mid * 0.98, 2),
                              "ask": round(mid * 1.02, 2), "last": mid, "iv": IV,
                              "delta": delta if right == "C" else -delta, "open_interest": 5000})
                for d in SESSIONS:
                    p = model_price(right, k, d)
                    row = {"occ_symbol": sym, "date": d.isoformat(), "open": p, "high": p,
                           "low": p, "close": p, "volume": volume, "underlying": sym_u,
                           "option_type": kind, "strike": k, "expiry": EXPIRY.isoformat(),
                           "iv": IV, "delta": delta if right == "C" else -delta}
                    row.update(overrides.get((sym, d.isoformat()), {}))
                    row["high"] = max(row["high"], row["open"], row["close"])
                    row["low"] = min(row["low"], row["open"], row["close"])
                    bars.append(row)
        cache.write_chain_rows(sym_u, START.date().isoformat(), chain)
        cache.write_bar_rows(bars)


def build_account(*, overrides=None, cfg: Optional[Dict[str, Any]] = None, volume: int = 5000,
                  account_id: int = 90, sessions=None, symbols=("AAPL",)):
    """The seeded chain on an options ``BacktestAccount`` with its price source clock at the
    decision bar -- no engine. Returns ``(account, price_source, ctx, resolver)``; the caller
    MUST ``ctx.__exit__(None, None, None)``."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    config_cfg = {**BASE_CFG, **(cfg or {})}
    tmpdir = tempfile.mkdtemp(prefix="opt-structure-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    seed_cache(cache_db, overrides, volume, symbols)
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db("structure-engine")
    ctx.__enter__()
    seed_account_definition(account_id, config_cfg)
    ps = AsOfPriceSource(ohlcv_provider=None)
    for sym_u in symbols:
        ps.load_bars(sym_u, [{"Date": d, "Open": SPOT, "High": SPOT + 1, "Low": SPOT - 1,
                              "Close": SPOT, "Volume": 1000} for d in (sessions or SESSIONS)])
    ps.set_clock(START)
    account = BacktestAccount(
        account_id, ps, config_cfg,
        options_provider=HistoricalOptionsProvider(cache_db, risk_free_rate=RATE))
    resolver.register_account(account_id, account)
    return account, ps, ctx, resolver


def run_structure(action_type: str, *, strike_method: str = "percent_otm",
                  strike_param: Any = 2.0, sizing: float = 5.0, dte_min: int = 30,
                  dte_max: int = 50, overrides=None, cfg: Optional[Dict[str, Any]] = None,
                  volume: int = 5000, run: bool = True, account_id: int = 90,
                  extra_action: Optional[Dict[str, Any]] = None, symbols=("AAPL",)):
    """Wire (and by default run) the engine. Returns ``(engine, account, ctx, results)``;
    the caller MUST ``ctx.__exit__(None, None, None)``. ``results`` is None when not run."""
    from app.services.backtest.backtest_db import seed_expert_instance
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_ruleset_from_tree

    account, ps, ctx, resolver = build_account(overrides=overrides, cfg=cfg, volume=volume,
                                               account_id=account_id, symbols=symbols)
    entry_action = {
        "action_type": action_type, "option_strike_method": strike_method,
        "option_strike_param": strike_param, "option_dte_min": dte_min,
        "option_dte_max": dte_max, "option_sizing": sizing, **(extra_action or {}),
    }
    ruleset_id = seed_ruleset_from_tree(buy_tree=None, name=f"structure-{account_id}",
                                        entry_action=entry_action)
    seed_expert_instance(account_id=account_id, expert_class_name="_BuyExpert",
                         enter_market_ruleset_id=ruleset_id, open_positions_ruleset_id=None,
                         instance_id=account_id)
    expert = _BuyExpert(account_id)
    try:
        expert.settings["allow_automated_trade_opening"] = True
        expert.settings["enable_buy"] = True
    except Exception:  # noqa: BLE001
        pass
    resolver.register_expert(account_id, expert)
    engine = DailyBacktestEngine(
        account=account, experts=[(expert, account_id, expert.settings, ruleset_id)],
        price_source=ps,
        config={"start_date": START, "end_date": END, "enabled_instruments": list(symbols),
                "seed": 42, "entry_action": entry_action},
        indicator_provider=object())
    results = engine.run() if run else None
    return engine, account, ctx, results
