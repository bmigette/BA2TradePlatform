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
from types import SimpleNamespace

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


def _run_basket(run_id, expert_cls=_BasketExpert, universe=None, extra_bars=None, return_db_state=False):
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
        for sym, rows in (extra_bars or {}).items():
            ps.load_bars(sym, rows)
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
                        "enabled_instruments": universe or [LIVE, THIN], "seed": 42,
                        "run_schedule_override": {"days": {d: True for d in (
                            "monday", "tuesday", "wednesday", "thursday", "friday")},
                            "times": ["09:40"]}},
                indicator_provider=None)
            engine._indicator_provider = None
            from ba2_common.core.knowability import intraday_decisions
            with intraday_decisions(True, scan_cutoff=ps.scan_cutoff_date):   # what the handler does
                engine.run()
        finally:
            BacktestAccount.submit_order = real
            set_backtest_ohlcv_override(None)
        state = None
        if return_db_state:
            from ba2_common.core.trade_store import orders_where, transactions_where
            state = {"transactions": [(t.symbol, t.status) for t in transactions_where()],
                     "orders": [(o.symbol, o.status) for o in orders_where(account_id=account.id)]}
            state["cash"] = account.get_balance()
        return (expert, engine, account, placed) if not return_db_state else (expert, engine, account, placed, state)
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
    from ba2_common.core.knowability import intraday_decisions
    with intraday_decisions(True):
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


# --------------------------------------------------------------------------- E2: StaleAnchorPrice ends the run
def test_every_named_broad_handler_in_the_engine_reraises_the_run_ending_refusals():
    """Structural: an ``except Exception as e`` in daily_engine.py either re-raises or goes through
    ``_reraise_option_basis_refusal`` (split-basis refusals AND StaleAnchorPrice)."""
    import ast, pathlib
    import app.services.backtest.daily_engine as de
    tree = ast.parse(pathlib.Path(de.__file__).read_text(encoding="utf-8"))
    lacking = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ExceptHandler) and n.name and ast.unparse(n.type) == "Exception":
            body = ast.unparse(ast.Module(body=n.body, type_ignores=[]))
            if "_reraise_option_basis_refusal" not in body and "raise" not in body:
                lacking.append(n.lineno)
    assert lacking == [], f"broad handlers that would swallow a run-ending refusal: lines {lacking}"


def test_the_shared_helper_reraises_stale_anchor_price_and_nothing_ordinary():
    from ba2_common.core.knowability import StaleAnchorPrice
    from app.services.backtest.daily_engine import _reraise_option_basis_refusal
    with pytest.raises(StaleAnchorPrice):
        _reraise_option_basis_refusal(StaleAnchorPrice("plain float anchor"))
    _reraise_option_basis_refusal(ValueError("an ordinary failure is still absorbed"))


class _StaleBasket(_BasketExpert):
    def analyze_as_of(self, as_of, context):
        from ba2_common.core.knowability import StaleAnchorPrice
        raise StaleAnchorPrice("anchor 1.0 is not the decision price")


def test_a_stale_anchor_in_a_basket_analysis_fails_the_run_loudly():
    from ba2_common.core.knowability import StaleAnchorPrice
    with pytest.raises(StaleAnchorPrice):
        _run_basket(932, expert_cls=_StaleBasket)


def test_an_unresolvable_decision_account_on_an_intraday_clock_raises_instead_of_falling_back():
    from ba2_common.core.knowability import intraday_decisions
    e = _BasketExpert.__new__(_BasketExpert)
    e.id = 987654                                   # no such instance -> no account
    with intraday_decisions(True):
        with pytest.raises(RuntimeError, match="intraday clock"):
            e._decision_account(object())
    e._decision_account_cache = None
    with intraday_decisions(False):                  # off the intraday clock: the replay fallback, as before
        assert e._decision_account(object()) is None


# --------------------------------------------------------------------------- I3: state after an expiry
class _BuyThin(MarketExpertInterface):
    def __init__(self, id, price_source):
        super().__init__(id)

    @classmethod
    def description(cls) -> str:
        return "buys the thin name"

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        px = self._decision_price(context.providers, THIN, as_of)
        return Recommendation(signal=OrderRecommendation.BUY, confidence=80.0, current_price=px,
                              details="buy thin", expected_profit_percent=4.0)


def test_an_expired_cross_session_entry_leaves_no_waiting_row_and_no_reserved_cash():
    from ba2_common.core.types import OrderStatus
    from tests.backtest.test_intraday_daily_knowability import _intraday_rows
    prior = [dict(r, Date=r["Date"].replace(year=2023, month=12, day=29)) for r in _intraday_rows()[:4]]
    far = [dict(r, Date=r["Date"].replace(year=2024, month=1, day=12)) for r in _intraday_rows()[:4]]
    expert, engine, account, placed, state = _run_basket(
        933, expert_cls=_BuyThin, universe=[THIN], extra_bars={THIN: prior + far}, return_db_state=True)
    assert account.intraday_counters["entries_refused_next_bar_other_session"] >= 1
    assert state["orders"] and all(st in (OrderStatus.EXPIRED, OrderStatus.CANCELED, OrderStatus.REJECTED)
                                   for _s, st in state["orders"]), state["orders"]
    assert all(str(getattr(st, "value", st)).upper() not in ("WAITING", "OPENED", "OPEN")
               for _s, st in state["transactions"]), state["transactions"]
    from tests.backtest.test_max_loss_stop_engine import CFG
    assert state["cash"] == pytest.approx(CFG["starting_cash"])             # nothing reserved, nothing spent


# --------------------------------------------------------------------------- unpriced recommendations
def _bare_engine():
    from app.services.backtest.daily_engine import DailyBacktestEngine
    eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
    eng.analysis_failures = {}
    eng.intraday_counters = {"undecidable_symbol_days": 0, "manage_skipped_no_price_symbol_ticks": 0,
                             "entry_skipped_no_price_symbol_ticks": 0}
    eng._log = lambda *a, **k: None
    eng.account = SimpleNamespace(id=1)
    eng.config = {}
    return eng


def _rec(price, signal=OrderRecommendation.HOLD):
    return Recommendation(signal=signal, confidence=70.0, current_price=price, details="d",
                          expected_profit_percent=1.0)


def test_the_converter_never_floats_a_missing_price_and_a_skip_stays_a_skip():
    from app.services.backtest.daily_engine import _recommendation_to_expert_recommendation, rec_is_unpriced
    assert rec_is_unpriced(_rec(None)) and not rec_is_unpriced(_rec(10.0))
    skip = _rec(None)
    skip.skip, skip.skip_reason = True, "no_price"
    assert not rec_is_unpriced(skip)                                     # a declared skip is its own contract
    assert _recommendation_to_expert_recommendation(
        _rec(None), expert_instance_id=1, symbol="X", as_of=datetime(2024, 1, 2), allow_hold=True) is None


def test_a_held_symbol_without_a_decision_price_skips_its_manage_step_and_the_others_are_managed(monkeypatch):
    """Crash of row 1363: float(None) in the converter on the OPEN_POSITIONS pass."""
    import app.services.backtest.daily_engine as de
    eng = _bare_engine()
    monkeypatch.setattr(eng, "_held_transactions", lambda expert_id: {"HELD": [object()], "OTHER": [object()]})
    monkeypatch.setattr(eng, "_provider_bundle", lambda: object())
    import ba2_common.core.db as dbmod
    monkeypatch.setattr(dbmod, "get_instance",
                        lambda model, i: SimpleNamespace(open_positions_ruleset_id=5))
    converted = []
    monkeypatch.setattr(de, "_recommendation_to_expert_recommendation",
                        lambda rec, **kw: converted.append(kw["symbol"]) or None)

    class Exp:
        def analyze_as_of(self, as_of, ctx):
            return _rec(None if ctx.extra["symbol"] == "HELD" else 11.0)

    eng._manage_open_positions(Exp(), 1, {}, datetime(2024, 1, 2, 10, 0))
    assert eng.intraday_counters["manage_skipped_no_price_symbol_ticks"] == 1
    assert converted == ["OTHER"]                                         # HELD never reached persistence
    assert eng.analysis_failures_record()["failed"] == 0                  # a decision of "nothing", not a failure


def test_a_basket_item_without_a_price_is_not_staged_and_is_counted():
    eng = _bare_engine()
    staged = eng._stage_recommendation_candidate(
        _rec(None, OrderRecommendation.BUY), expert=SimpleNamespace(settings={}), expert_id=1, symbol="X",
        ruleset_id=1, as_of=datetime(2024, 1, 2, 10, 0), equity_candidates=[])
    assert staged is False
    assert eng.intraday_counters["entry_skipped_no_price_symbol_ticks"] == 1


def test_live_a_none_quote_fails_that_one_analysis_cleanly_not_with_a_float_crash():
    """Live run_analysis of the experts that need a price validates the bundle BEFORE persisting: a held
    symbol whose quote is unavailable fails that ONE analysis loudly (ValueError, caught by the worker),
    never a TypeError from float(None) and never a row with a fake price."""
    from ba2_experts.FMPEarningsDrift import FMPEarningsDrift
    from ba2_experts.FMPInsiderClusterBuy import FMPInsiderClusterBuy
    for cls in (FMPEarningsDrift, FMPInsiderClusterBuy):
        with pytest.raises(ValueError):
            cls._require_current_price({"current_price": None, "symbol": "X"})
