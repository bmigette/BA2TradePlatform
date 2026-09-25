"""O_CC / O_PP buy their shares in WHOLE 100-share lots -- through ``DailyBacktestEngine.run()``.

The overlay both keys exist for sizes as ``floor(held_shares / 100)`` contracts, so the equity
entry has to be a whole lot or no contract is ever written. ``_with_round_lot_entry`` puts
``lot_size: 100`` on the entry BUY for exactly that, and from 37d207c4 (2026-07-25) it never
took effect: ``rules_convert`` dropped the key when the rule was seeded, and the RM candidate
was built from the recommendation, not from the fired ``BuyAction``. The engine bought whatever
the per-instrument cap allowed -- 537 shares here, 66 on a $150 name -- and the overlay wrote a
5-contract call against 537 shares at best and NOTHING at all on the 66.

(``test_covered_call_engine.py`` could not see it: its $20 spot under a $10k cap happens to
afford exactly 500, a whole lot by arithmetic, not by the constraint.)

Each case is O_CC's / O_PP's OWN launcher-built ruleset, reduced to the entry gates this fixture
can answer and the overlay pair (every dropped node carries its own on/off gene, so the reduced
ruleset is a real point in the searched space).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_equity_lot_size_engine.py -q
"""
from __future__ import annotations

import copy
import importlib.util
import logging
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderDirection, OrderRecommendation, OrderStatus, Recommendation

_LAUNCHER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "ba2test_launcher.py")

CFG = {
    **_LEGACY_ZERO_SPREAD, "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}
#: 10% of $100k: the per-instrument cap the RM sizes up to.
CAP_PCT = 10.0

SYMBOL = "LOTX"
START = datetime(2024, 1, 2)
END = datetime(2024, 1, 26)
EXPIRY = START.date() + timedelta(days=35)   # inside the [25, 45] window the overlays author

#: $10,000 / $18.60 = 537.6 -> 537 shares by the cap: an ODD lot.
ODD_LOT_SPOT = 18.60
#: $10,000 / $150 = 66 shares: less than ONE lot.
SUB_LOT_SPOT = 150.0

#: key -> (option right, strike as a fraction of spot, the overlay rule pair to keep)
_KEYS = {
    "O_CC": ("C", 1.05, {"cc_guard", "cc_sell"}),
    "O_PP": ("P", 0.92, {"pp_guard", "pp_buy"}),
}


def _launcher():
    spec = importlib.util.spec_from_file_location("lch_lot_engine", _LAUNCHER_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["lch_lot_engine"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def _weekdays(start: date, end: date):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(START.date(), END.date())


def _contract(right: str, strike: float) -> str:
    return f"{SYMBOL}{EXPIRY:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def _option_fixture(right: str, strike: float):
    occ = _contract(right, strike)
    kind = "call" if right == "C" else "put"
    chain = [{"occ_symbol": occ, "option_type": kind, "strike": strike,
              "expiry": EXPIRY.isoformat(), "bid": 0.60, "ask": 0.60, "last": 0.60,
              "iv": 0.30, "delta": 0.25 if right == "C" else -0.25, "open_interest": 5000}]
    bars = [{"occ_symbol": occ, "date": d.isoformat(), "open": 0.60, "high": 0.65,
             "low": 0.55, "close": 0.60, "volume": 500, "underlying": SYMBOL,
             "option_type": kind, "strike": strike, "expiry": EXPIRY.isoformat(),
             "iv": 0.30, "delta": 0.25 if right == "C" else -0.25}
            for d in DAYS]
    return occ, chain, bars


class _PlainBuyExpert(MarketExpertInterface):
    """A BUY expert with nothing else to say -- the overlays' entry gate is directional only."""

    bypasses_classic_rm = False

    def __init__(self, id: int, spot: float):
        super().__init__(id)
        self._settings_cache = {}
        self._spot = spot

    @classmethod
    def description(cls) -> str:
        return "Stub BUY expert for the round-lot engine test."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        return Recommendation(
            signal=OrderRecommendation.BUY, confidence=95.0, current_price=self._spot,
            details="buy", expected_profit_percent=25.0, raw_outputs={})


def _keep_only(rules, keep_fields):
    out = copy.deepcopy(rules)
    for rule in out:
        conds = (rule.get("conditions") or {}).get("conditions")
        if conds:
            rule["conditions"]["conditions"] = [c for c in conds if c.get("field") in keep_fields]
    return out


def _rules(key, *, lot_size="authored"):
    """``key``'s OWN launcher-built rules. ``lot_size`` = "authored" keeps the launcher's value;
    None strips it (the control: what the entry looks like with no round-lot constraint)."""
    m = _launcher()
    strat = m._build_strategy(key, f"lot-{key}", "FMPRating")
    entry = _keep_only(list(strat.entry_rules or []), {"has_no_position", "bullish"})
    buys = [a for r in entry for a in r["actions"] if a.get("action_type") == "buy"]
    assert buys and all(a.get("lot_size") == 100 for a in buys), (
        f"{key}'s entry no longer authors lot_size=100 -- this test pins the effect of it")
    if lot_size is None:
        for a in buys:
            a.pop("lot_size")
    keep = _KEYS[key][2] | {"exit_stoploss"}
    exits = [r for r in (strat.exit_rules or []) if r.get("id") in keep]
    return entry, exits


def _run(key, spot, account_id, *, lot_size="authored"):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_entry_ruleset_from_rules, seed_exit_ruleset_from_rules)
    from app.services.backtest.options_cache import OptionsHistoryCache
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.trade_store import orders_where

    right, k, _keep = _KEYS[key]
    occ, chain, bars = _option_fixture(right, round(spot * k, 1))
    tmpdir = tempfile.mkdtemp(prefix="lot-engine-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows(SYMBOL, START.date().isoformat(), chain)
    cache.write_bar_rows(bars)
    provider = HistoricalOptionsProvider(cache_db)

    entry_rules, exit_rules = _rules(key, lot_size=lot_size)
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"lot-{account_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, CFG)
        enter_id = seed_entry_ruleset_from_rules(entry_rules, name=f"lot-enter-{account_id}")
        open_id = seed_exit_ruleset_from_rules(exit_rules, name=f"lot-open-{account_id}")
        seed_expert_instance(account_id=account_id, expert_class_name="_LotExpert",
                             enter_market_ruleset_id=enter_id,
                             open_positions_ruleset_id=open_id, instance_id=account_id)

        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars(SYMBOL, [{"Date": d, "Open": spot, "High": spot + 0.2, "Low": spot - 0.2,
                               "Close": spot, "Volume": 1_000_000} for d in DAYS])
        ps.set_clock(START)
        account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
        resolver.register_account(account_id, account)
        expert = _PlainBuyExpert(account_id, spot)
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "allow_automated_trade_modification": (True, "bool"),
                              "enable_buy": (True, "bool"),
                              "max_virtual_equity_per_instrument_percent": (CAP_PCT, "float")})
        resolver.register_expert(account_id, expert)
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, account_id, expert.settings, enter_id)],
            price_source=ps,
            config={"start_date": START, "end_date": END, "enabled_instruments": [SYMBOL],
                    "seed": 42},
            indicator_provider=None)
        engine.run()
        executed = [o for o in orders_where(account_id=account_id)
                    if o.status in OrderStatus.get_executed_statuses()]
        equity = [o for o in executed if getattr(o, "contract_symbol", None) is None
                  and o.side is OrderDirection.BUY]
        options = [o for o in executed if getattr(o, "contract_symbol", None) == occ]
        return equity, options
    finally:
        ctx.__exit__(None, None, None)


def _qty(o):
    return o.filled_qty or o.quantity


@pytest.mark.parametrize("key,account_id", [("O_CC", 871), ("O_PP", 872)])
def test_an_ODD_affordable_count_buys_the_whole_lots_and_the_overlay_covers_every_share(
        key, account_id):
    """537 affordable -> 500 bought, and the overlay writes 5 contracts against them."""
    equity, options = _run(key, ODD_LOT_SPOT, account_id)
    assert equity, f"{key}: no equity entry filled"
    entries = [_qty(o) for o in equity]
    assert all(q % 100 == 0 for q in entries), (
        f"{key}: the equity entry is not a whole 100-share lot ({entries}); the cap afforded "
        f"537, and lot_size=100 must floor that to 500")
    assert entries[0] == 500, f"{key}: expected 500 shares (537 floored to lots), got {entries}"
    # EVERY entry carries its overlay, one contract per 100 shares held.
    assert len(options) >= len(equity), (
        f"{key}: {len(equity)} equity entr(y/ies) but {len(options)} overlay order(s)")
    assert _qty(options[0]) == entries[0] // 100, (
        f"{key}: the overlay wrote {_qty(options[0])} contracts against {entries[0]} shares")


@pytest.mark.parametrize("key,account_id", [("O_CC", 873), ("O_PP", 874)])
def test_LESS_than_one_affordable_lot_is_refused_loudly_not_bought_as_an_odd_lot(
        key, account_id):
    """66 affordable -> nothing bought (a 66-share position can carry no contract), and the RM
    says why at WARNING."""
    from ba2_common.logger import logger as ba2_logger

    class _Grab(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.WARNING)
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    grab = _Grab()
    ba2_logger.addHandler(grab)
    try:
        equity, options = _run(key, SUB_LOT_SPOT, account_id)
    finally:
        ba2_logger.removeHandler(grab)
    assert equity == [], (
        f"{key}: bought {[_qty(o) for o in equity]} shares -- less than one 100-share lot "
        f"was affordable ($10k cap / $150), so the entry must be refused")
    assert options == []
    assert any("lot" in m.lower() and "refus" in m.lower() and SYMBOL in m
               for m in grab.messages), (
        f"{key}: the sub-lot refusal was not announced at WARNING: {grab.messages[-5:]}")


def test_WITHOUT_lot_size_the_same_run_buys_the_odd_lot():
    """The control: the constraint, not the fixture, is what makes the lot whole."""
    equity, _options = _run("O_CC", ODD_LOT_SPOT, 875, lot_size=None)
    assert [_qty(o) for o in equity][:1] == [537]


def test_the_round_lot_is_authored_on_the_BUY_entry_only():
    """``SellAction`` takes no lot size and ``rules_convert`` carries it on the buy alone, so a
    sell-side lot would be authored and then dropped without a word."""
    from types import SimpleNamespace
    m = _launcher()
    s = SimpleNamespace(entry_rules=[
        {"id": "b", "actions": [{"action_type": "buy"}]},
        {"id": "s", "actions": [{"action_type": "sell"}]}])
    m._with_round_lot_entry(s)
    assert s.entry_rules[0]["actions"][0]["lot_size"] == 100
    assert "lot_size" not in s.entry_rules[1]["actions"][0]
