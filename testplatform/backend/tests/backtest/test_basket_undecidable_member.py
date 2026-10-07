"""A BASKET expert with ONE member that has no decidable price still trades the others.

Row 1645 (FMPSenateTraderWeight, 5-minute clock, 10:00 decisions) produced 0 trades over six years:
``MarketExpertInterface._decision_price`` asks the account for ONE symbol's price; the account RAISED
for a member with no ended bar (thin name / no 5-minute file); the Senate gather only caught cache-miss
errors, so the exception aborted the whole basket analysis, the engine logged a WARNING and dropped the
day. Every day had such a member -> no trade in six years, and a normal-looking result.

The rule pinned here (the same one live follows when a quote is None): a member without a decision
price is EXCLUDED for that decision, counted (``undecidable_price_reads``), and the rest of the basket
is analysed and traded. Plus the result-level refusal: a run whose analysis passes mostly raised is
refused, never scored as "no edge".
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.knowability import NoDecisionPrice
from ba2_common.core.types import OrderRecommendation, Recommendation

from app.services.backtest import price_source as ps_mod
from app.services.backtest.price_source import AsOfPriceSource, MemoizedOHLCVProvider
from tests.backtest import test_intraday_daily_knowability as k

LIVE = k.SYMBOL            # has bars
THIN = "TDY"               # never printed a 5-minute bar (no file / thin name)


class _BasketExpert(MarketExpertInterface):
    analyzes_as_basket = True

    def __init__(self, id, price_source):
        super().__init__(id)
        self.prices = []

    @classmethod
    def description(cls) -> str:
        return "basket with one undecidable member"

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        recs = []
        for sym in (THIN, LIVE):                 # the undecidable member comes FIRST, as in row 1645
            px = self._decision_price(context.providers, sym, as_of)
            self.prices.append((sym, px))
            if not px:
                continue                          # excluded for this decision
            recs.append(Recommendation(signal=OrderRecommendation.BUY, confidence=80.0,
                                       current_price=float(px), details="basket buy",
                                       expected_profit_percent=4.0, raw_outputs={"symbol": sym}))
        return recs


def _run_basket(run_id, expert_cls=_BasketExpert):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.seam_wiring import set_backtest_ohlcv_override, wire_backtest_seams
    from tests.backtest.test_max_loss_stop_engine import CFG

    ps_mod.clear_ohlcv_memo()
    account_id = expert_id = run_id
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"basket-undecidable-{run_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, CFG)
        ruleset_id = seed_enter_long_ruleset()
        seed_expert_instance(account_id=account_id, expert_class_name=expert_cls.__name__,
                             enter_market_ruleset_id=ruleset_id, instance_id=expert_id)
        memo = MemoizedOHLCVProvider(k._FakeDaily(), datetime(2023, 12, 1), datetime(2024, 1, 31),
                                     interval="5min")
        ps = AsOfPriceSource(ohlcv_provider=None, interval="5min")
        memo.bind_price_source(ps)
        ps.load_bars(LIVE, k._intraday_rows())            # THIN is deliberately never loaded
        account = BacktestAccount(account_id, ps, CFG)
        resolver.register_account(account_id, account)
        expert = expert_cls(expert_id, ps)
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "enable_buy": (True, "bool")})
        resolver.register_expert(expert_id, expert)
        set_backtest_ohlcv_override(memo)
        placed = []
        real = BacktestAccount.submit_order

        def spy(self, trading_order, *a, **kw):
            placed.append(trading_order.symbol)
            return real(self, trading_order, *a, **kw)

        BacktestAccount.submit_order = spy
        try:
            engine = DailyBacktestEngine(
                account=account, experts=[(expert, expert_id, {}, ruleset_id)], price_source=ps,
                config={"start_date": datetime(2024, 1, 2), "end_date": datetime(2024, 1, 4, 23, 59),
                        "enabled_instruments": [LIVE, THIN], "seed": 42,
                        "run_schedule_override": {"days": {d: True for d in (
                            "monday", "tuesday", "wednesday", "thursday", "friday")},
                            "times": ["09:40"]}},
                indicator_provider=None)
            engine._indicator_provider = None
            engine.run()
        finally:
            BacktestAccount.submit_order = real
            set_backtest_ohlcv_override(None)
        return expert, engine, account, placed
    finally:
        ctx.__exit__(None, None, None)


def test_a_basket_with_one_undecidable_member_still_trades_the_others():
    expert, engine, account, placed = _run_basket(931)
    assert any(s == THIN and p is None for s, p in expert.prices), expert.prices
    assert any(s == LIVE and p for s, p in expert.prices), expert.prices
    assert LIVE in placed and THIN not in placed, placed          # the others trade; the member does not
    assert account.intraday_counters["undecidable_price_reads"] > 0   # ...and it is counted
    assert engine.analysis_failures_record()["failed"] == 0           # not an analysis failure


def test_the_single_symbol_read_still_refuses_but_with_a_typed_refusal():
    from app.services.backtest.backtest_account import BacktestAccount
    acct = BacktestAccount.__new__(BacktestAccount)

    class PS:
        is_intraday = True
        def now(self): return datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
        def decision_price(self, s, now): return None

    acct._price = PS()
    acct.intraday_counters = {"undecidable_price_reads": 0}
    with pytest.raises(NoDecisionPrice) as ei:
        acct._get_instrument_current_price_impl("TDY")
    assert isinstance(ei.value, ValueError)                     # every old caller still sees a ValueError
    assert acct._get_instrument_current_price_impl(["TDY"]) == {"TDY": None}
    assert acct.intraday_counters["undecidable_price_reads"] == 1


def test_the_seam_turns_an_undecidable_symbol_into_none_like_a_live_quote_that_is_none():
    e = _BasketExpert.__new__(_BasketExpert)

    class Acct:
        def get_instrument_current_price(self, s):
            if s == "TDY":
                raise NoDecisionPrice("no bar")
            return 12.5

    bundle = object()
    e._decision_account_cache = (bundle, Acct())
    now = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    assert e._decision_price(bundle, "TDY", now) is None
    assert e._decision_price(bundle, "AAPL", now) == 12.5


# --------------------------------------------------------------------------- result-level refusal
def _engine_with(passes, failed, expert_id=1):
    from app.services.backtest.daily_engine import DailyBacktestEngine
    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.analysis_failures = {}
    for i in range(passes):
        eng._analysis_pass(expert_id)
    for i in range(failed):
        eng._analysis_failed(expert_id, f"boom {i}")
    return eng


def test_a_run_whose_analysis_passes_mostly_failed_is_refused(caplog):
    from app.services.backtest.daily_engine import AnalysisFailureRefusal
    eng = _engine_with(100, 90)
    with pytest.raises(AnalysisFailureRefusal, match="boom 0"):
        eng.refuse_if_analysis_failing()
    rec = eng.analysis_failures_record()
    assert (rec["passes"], rec["failed"], rec["first_error"]) == (100, 90, "boom 0")


def test_a_few_failures_warn_once_and_do_not_refuse():
    eng = _engine_with(100, 3)
    eng.refuse_if_analysis_failing()                       # 3% <= 5%: a warning, not a refusal


def test_an_expert_that_analyses_fine_and_emits_nothing_is_not_a_failure():
    eng = _engine_with(500, 0)
    eng.refuse_if_analysis_failing()
    assert eng.analysis_failures_record()["failed"] == 0


def test_too_few_passes_are_not_a_rate():
    eng = _engine_with(4, 4)
    eng.refuse_if_analysis_failing()
