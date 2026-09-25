"""E1: the backtest routes EACH ENTRY by its own actions, not once per run.

Plan 2026-09-25 option-stage2-build, "Revisions after the user's answers", Task E1. Until E1 the
engine decided ONE entry path per run from ``config["entry_action"]`` (``_entry_is_option``):
every entry self-sized as an option, or every entry was staged for the equity RM. A style
expert that mixes an equity-entry member (O_CC / O_PP: a stock buy, then an option overlay)
with option-entry members (O_CSP, O_LC, ...) cannot run on that. Now every fired entry goes
through the SHARED ``trade_cycle.entry_route`` -- the same function live ``TradeManager``
calls -- so:

* an all-option rule self-sizes (the option action submits itself);
* an all-equity rule is staged for the RM sizing pass;
* a rule mixing the two is REFUSED, loudly, and places nothing.

Every test here runs the REAL ``DailyBacktestEngine.run()`` over a fixture options cache, with
TWO symbols whose recommendations fire DIFFERENT entry rules in the SAME run, and NO
``entry_action`` in the run config (the run-level flag must not be what routes).

Run from the backend dir:
    python -m pytest tests/backtest/test_per_entry_routing.py -q
"""
from __future__ import annotations

from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
import logging
import os
import tempfile
from datetime import date, datetime, timedelta

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderDirection, OrderRecommendation, OrderStatus, Recommendation

CFG = {
    **_LEGACY_ZERO_SPREAD, "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

START = datetime(2024, 1, 2)
END = datetime(2024, 1, 26)
EXPIRY = START.date() + timedelta(days=35)

#: The OPTION-entry symbol (the expert is confident on it -> the option rule fires).
OPX = "OPX"
OPX_SPOT = 50.0
OPX_CALL = f"OPX{EXPIRY:%y%m%d}C00050000"
OPX_PUT = f"OPX{EXPIRY:%y%m%d}P00048000"
#: The EQUITY-entry symbol (the expert is lukewarm on it -> the equity rule fires). Cheap, so a
#: whole 100-share lot fits any per-instrument cap on $100k (the covered-call overlay needs it).
CCX = "CCX"
CCX_SPOT = 20.0
CCX_CALL = f"CCX{EXPIRY:%y%m%d}C00021000"

CONFIDENCE = {OPX: 90.0, CCX: 60.0}

#: Style-member tags used by the fixture rules (arbitrary positive ints; the real table is
#: the launcher's append-only _STYLE_MEMBER_TAGS).
TAG_OPTION, TAG_EQUITY = 1, 21


def _weekdays(start: date, end: date):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(START.date(), END.date())


def _bars(spot):
    return [{"Date": d, "Open": spot, "High": spot + 0.2, "Low": spot - 0.2, "Close": spot,
             "Volume": 1_000_000} for d in DAYS]


def _contract(occ, underlying, right, strike, px):
    return {"occ_symbol": occ, "option_type": right, "strike": strike,
            "expiry": EXPIRY.isoformat(), "bid": px, "ask": px, "last": px,
            "iv": 0.30, "delta": 0.50 if right == "call" else -0.30, "open_interest": 5000}


_CONTRACTS = [(OPX_CALL, OPX, "call", 50.0, 2.0), (OPX_PUT, OPX, "put", 48.0, 1.0),
              (CCX_CALL, CCX, "call", 21.0, 0.60)]


def _bar_rows():
    rows = []
    for occ, und, right, strike, px in _CONTRACTS:
        for d in DAYS:
            rows.append({"occ_symbol": occ, "date": d.isoformat(), "open": px,
                         "high": px + 0.05, "low": px - 0.05, "close": px, "volume": 5000,
                         "underlying": und, "option_type": right, "strike": strike,
                         "expiry": EXPIRY.isoformat(), "iv": 0.30,
                         "delta": 0.50 if right == "call" else -0.30})
    return rows


class _TwoSymbolExpert(MarketExpertInterface):
    """BUY on both symbols, with a per-symbol confidence so each fires its own entry rule.
    The engine pins the symbol on ``_gather_symbol`` before each per-symbol call."""

    bypasses_classic_rm = False
    _gather_symbol = None

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "Stub two-symbol BUY expert for the per-entry routing test."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        symbol = self._gather_symbol
        assert symbol in CONFIDENCE, f"unexpected symbol {symbol!r}"
        price = context.account.get_instrument_current_price(symbol)
        return Recommendation(
            signal=OrderRecommendation.BUY, confidence=CONFIDENCE[symbol],
            current_price=price, details="buy", expected_profit_percent=25.0,
            raw_outputs={})


def _gate(op, value):
    return {"type": "AND", "conditions": [
        {"id": "conf", "field": "confidence", "op": op, "value": value},
        {"id": "flat", "field": "has_no_position"}]}


def _option_rule(action):
    return {"id": "opt", "conditions": _gate(">=", 80.0), "actions": [action]}


def _equity_rule(actions):
    return {"id": "eq", "conditions": _gate("<", 80.0), "actions": actions}


BUY_CALL = {"action_type": "buy_call", "option_strike_method": "percent_otm",
            "option_strike_param": 0.0, "option_dte_min": 20, "option_dte_max": 60,
            "option_sizing": 5.0, "option_entry_tag": TAG_OPTION}
SELL_CSP = {"action_type": "sell_cash_secured_put", "option_strike_method": "percent_otm",
            "option_strike_param": 4.0, "option_dte_min": 20, "option_dte_max": 60,
            "option_sizing": 25.0, "option_entry_tag": TAG_OPTION}
BUY_STOCK = {"action_type": "buy", "lot_size": 100, "entry_tag": TAG_EQUITY}


def _run(account_id, entry_rules, exit_rules=()):
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
    from ba2_common.core.rule_models import normalize_trade_rules
    from ba2_common.core.trade_store import orders_where

    tmpdir = tempfile.mkdtemp(prefix="per-entry-routing-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    for sym in (OPX, CCX):
        cache.write_chain_rows(sym, START.date().isoformat(),
                               [_contract(o, u, r, k, p) for (o, u, r, k, p) in _CONTRACTS
                                if u == sym])
    cache.write_bar_rows(_bar_rows())
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"routing-{account_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, CFG)
        enter_id = seed_entry_ruleset_from_rules(normalize_trade_rules(list(entry_rules)),
                                                 name=f"routing-enter-{account_id}")
        open_id = seed_exit_ruleset_from_rules(normalize_trade_rules(list(exit_rules)),
                                               name=f"routing-open-{account_id}")
        seed_expert_instance(account_id=account_id, expert_class_name="_RoutingExpert",
                             enter_market_ruleset_id=enter_id,
                             open_positions_ruleset_id=open_id, instance_id=account_id)

        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars(OPX, _bars(OPX_SPOT))
        ps.load_bars(CCX, _bars(CCX_SPOT))
        ps.set_clock(START)

        account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
        resolver.register_account(account_id, account)
        expert = _TwoSymbolExpert(account_id)
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "allow_automated_trade_modification": (True, "bool"),
                              "enable_buy": (True, "bool")})
        resolver.register_expert(account_id, expert)

        # NO entry_action: the run-level flag must not be what routes an entry.
        config = {"start_date": START, "end_date": END, "enabled_instruments": [OPX, CCX],
                  "seed": 42}
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, account_id, expert.settings, enter_id)],
            price_source=ps, config=config, indicator_provider=None)
        engine.run()
        return list(orders_where(account_id=account_id)), account
    finally:
        ctx.__exit__(None, None, None)


def _executed(orders):
    return [o for o in orders if o.status in OrderStatus.get_executed_statuses()]


def _option_orders(orders, symbol):
    return [o for o in orders if getattr(o, "contract_symbol", None)
            and str(o.contract_symbol).startswith(symbol)]


def _stock_orders(orders, symbol):
    return [o for o in orders if o.symbol == symbol and not getattr(o, "contract_symbol", None)
            and not getattr(o, "option_strategy", None)]


@pytest.fixture
def rm_spy(monkeypatch):
    """Record every symbol the equity RM is asked to size, then size it for real."""
    from ba2_common.core.TradeRiskManagement import TradeRiskManagement
    seen = []
    real = TradeRiskManagement.size_candidate_orders

    def spy(self, expert_id, candidates):
        seen.extend(c.symbol for c, _rec in candidates)
        return real(self, expert_id, candidates)

    monkeypatch.setattr(TradeRiskManagement, "size_candidate_orders", spy)
    return seen


# --------------------------------------------------------------------------- #
# 1. two entry rules, two paths, ONE run
# --------------------------------------------------------------------------- #
def test_an_option_rule_and_an_equity_rule_each_take_their_own_path_in_one_run(rm_spy):
    orders, _account = _run(8701, [_option_rule(BUY_CALL), _equity_rule([BUY_STOCK])])

    # the OPTION entry self-sized: contracts on OPX, and OPX never reached the equity RM
    calls = _executed(_option_orders(orders, OPX))
    assert calls, "the option rule fired no self-sized option order on OPX"
    assert all(o.side is OrderDirection.BUY for o in calls)
    assert OPX not in rm_spy, f"an option entry was staged for the equity RM: {rm_spy}"
    assert not _stock_orders(orders, OPX), "OPX got a stock order"

    # the EQUITY entry went through the RM and was sized there
    assert CCX in rm_spy, "the equity rule's entry never reached the RM sizing pass"
    shares = _executed(_stock_orders(orders, CCX))
    assert shares and (shares[0].filled_qty or shares[0].quantity) > 0
    assert not _option_orders(orders, CCX), "CCX (an equity entry) got an option order"


def test_the_equity_entry_order_carries_its_member_tag_and_the_condition_reads_it(rm_spy):
    from ba2_common.core.TradeConditions import EntryTagCondition
    from ba2_common.core.entry_tag import ORDER_ENTRY_TAG_KEY, order_entry_tag

    orders, _ = _run(8702, [_option_rule(BUY_CALL), _equity_rule([BUY_STOCK])])
    (entry,) = [o for o in _stock_orders(orders, CCX) if o.side is OrderDirection.BUY
                and o.parent_order_id is None and o.depends_on_order is None]
    assert order_entry_tag(entry) == TAG_EQUITY and entry.data[ORDER_ENTRY_TAG_KEY] == TAG_EQUITY
    own = EntryTagCondition(account=None, instrument_name=CCX, expert_recommendation=None,
                            operator_str="==", value=TAG_EQUITY, existing_order=entry)
    other = EntryTagCondition(account=None, instrument_name=CCX, expert_recommendation=None,
                              operator_str="==", value=TAG_OPTION, existing_order=entry)
    assert own.evaluate() is True and other.evaluate() is False

    # and the option member's parent order carries ITS tag
    tagged = [o for o in orders if o.symbol == OPX and order_entry_tag(o) == TAG_OPTION]
    assert tagged, "the option entry's parent order row carries no member tag"


# --------------------------------------------------------------------------- #
# 2. a mixed rule is refused
# --------------------------------------------------------------------------- #
def test_a_rule_mixing_option_and_equity_actions_is_refused_loudly(rm_spy, caplog):
    mixed = {"id": "mixed", "conditions": _gate(">=", 0.0),
             "actions": [dict(BUY_CALL), {"action_type": "buy"}]}
    from ba2_common.logger import logger as ba2_logger
    ba2_logger.addHandler(caplog.handler)       # the ba2 logger does not propagate to root
    try:
        with caplog.at_level(logging.ERROR):
            orders, _ = _run(8703, [mixed])
    finally:
        ba2_logger.removeHandler(caplog.handler)
    assert not orders, f"a mixed entry rule placed orders: {[(o.symbol, o.side) for o in orders]}"
    assert not rm_spy, "a mixed entry reached the equity RM"
    refusals = [r for r in caplog.records
                if r.levelno >= logging.ERROR and "mixes option and equity" in r.getMessage()]
    assert refusals, "the refusal must be logged at ERROR, not dropped quietly"


# --------------------------------------------------------------------------- #
# 3. an O_CC-style member and a CSP member in one run
# --------------------------------------------------------------------------- #
def _tag_leaf(tag):
    return {"id": f"tag-{tag}", "field": "entry_tag", "op": "==", "value": float(tag)}


def test_a_covered_call_member_and_a_csp_member_coexist(rm_spy):
    """The premium-style shape E1 exists for: the CSP member sells a put on OPX (option
    entry, self-sized) while the O_CC-style member buys a round lot of CCX (equity entry, RM
    sized) and its tag-scoped overlay writes a call against those shares."""
    cc_guard = {"id": "cc_guard", "conditions": {"type": "AND", "conditions": [
        {"id": "has_cc", "field": "has_covered_call"}]},
        "actions": [{"action_type": "stop_processing"}]}
    cc_sell = {"id": "cc_sell", "conditions": {"type": "AND", "conditions": [
        {"id": "held", "field": "has_position"}, _tag_leaf(TAG_EQUITY)]},
        "actions": [{"action_type": "sell_covered_call", "option_strike_method": "percent_otm",
                     "option_strike_param": 5.0, "option_dte_min": 20, "option_dte_max": 60}],
        "continue_processing": True}

    orders, _ = _run(8704, [_option_rule(SELL_CSP), _equity_rule([BUY_STOCK])],
                     [cc_guard, cc_sell])

    puts = _executed(_option_orders(orders, OPX))
    assert puts and all(o.side is OrderDirection.SELL for o in puts), (
        "the CSP member sold no put on OPX")
    assert OPX not in rm_spy

    shares = _executed(_stock_orders(orders, CCX))
    assert shares and (shares[0].filled_qty or shares[0].quantity) % 100 == 0
    written = [o for o in _executed(_option_orders(orders, CCX)) if o.side is OrderDirection.SELL]
    assert len(written) == 1, (
        f"the covered-call overlay should write exactly one call on CCX, got {len(written)}")
    # the overlay is scoped to the equity member: it never tried to cover the CSP position
    assert not [o for o in _option_orders(orders, OPX)
                if o.side is OrderDirection.SELL and "C" in o.contract_symbol[len(OPX) + 6:]]
