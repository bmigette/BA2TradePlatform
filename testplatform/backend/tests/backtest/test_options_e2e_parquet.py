"""THE SAME engine end-to-end run as ``test_options_e2e.py``, served by the PARQUET store.

``test_options_e2e.py`` drives ``DailyBacktestEngine.run()`` through fill -> mark -> ITM
expiry settlement over a fixture ``OptionsHistoryCache`` sqlite and pins a final NLV of
101,350. This file rebuilds the IDENTICAL scenario as parquet partitions and asserts the
IDENTICAL numbers.

That equality is the point of the whole change: the engine is NOT forked. ``BacktestAccount``
holds one reader behind a bare attribute, and swapping which reader it holds must not move a
single cash flow when the two stores contain the same prices. If this ever diverges from
``test_options_e2e.py``'s constants, one of the two backends has grown a behaviour the other
does not have.

Run:
    ./venv/bin/python -m pytest tests/backtest/test_options_e2e_parquet.py -q
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import (
    OptionRight, OrderDirection, OrderRecommendation, Recommendation,
)
from app.services.backtest.parquet_options_provider import clear_worker_parquet_options_cache

# --- verbatim from test_options_e2e.py -------------------------------------- #
_OCC = "AAPL240207C00180000"
_STRIKE = 180.0
_EXPIRY = date(2024, 2, 7)
_MULTIPLIER = 100
START = datetime(2024, 2, 1)
END = datetime(2024, 2, 7)

_AAPL_BARS = [
    (date(2024, 2, 1), 185, 186, 184, 185),
    (date(2024, 2, 2), 186, 188, 185, 187),
    (date(2024, 2, 5), 188, 191, 187, 190),
    (date(2024, 2, 6), 191, 196, 190, 195),
    (date(2024, 2, 7), 198, 201, 197, 200),
]
_PREMIUM_BARS = [
    (date(2024, 2, 2), 6.5, 6.8, 6.4, 6.7),
    (date(2024, 2, 5), 10.2, 10.7, 10.1, 10.5),
    (date(2024, 2, 6), 15.2, 15.7, 15.1, 15.5),
]

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}


class _HoldStubExpert(MarketExpertInterface):
    bypasses_classic_rm = False

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "Stub HOLD expert for the parquet options engine e2e test."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        return Recommendation(signal=OrderRecommendation.HOLD, confidence=0.0,
                              current_price=None, details="hold (no equity orders)",
                              raw_outputs={})


def _underlying_rows(rows):
    return [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
            for (d, o, h, low, c) in rows]


def _write_parquet_store(root: str) -> None:
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore

    OptionHistoryParquetStore(root=root).write_partition(
        "AAPL", _EXPIRY,
        [OptionEodBar(occ_symbol=_OCC, bar_date=d, open=o, high=h, low=lo, close=c,
                      volume=400, open_interest=1000, iv=0.25)
         for (d, o, h, lo, c) in _PREMIUM_BARS],
        start=START.date(), end=END.date())


@pytest.fixture
def engine_run(tmp_path):
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.options_store import build_options_provider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    account_id = expert_id = 72
    root = str(tmp_path / "TastyTradeOptionsProvider")
    _write_parquet_store(root)
    clear_worker_parquet_options_cache()

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db("options-e2e-parquet")
    ctx.__enter__()

    seed_account_definition(account_id, CFG)
    ruleset_id = seed_enter_long_ruleset(name=f"options-e2e-parquet-{account_id}")
    seed_expert_instance(account_id=account_id, expert_class_name="_HoldStubExpert",
                         enter_market_ruleset_id=ruleset_id, instance_id=expert_id)

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _underlying_rows(_AAPL_BARS))
    ps.set_clock(START)

    # THE SELECTION SEAM, through the same factory run_daily_backtest uses. The spot source
    # is ``ps`` itself, so the greeks are inverted against the run's own underlying closes.
    provider = build_options_provider(
        {"options_cache_db": str(tmp_path / "unused.sqlite"),
         "options_store": "parquet", "options_parquet_root": root},
        price_source=ps)

    account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
    resolver.register_account(account_id, account)
    expert = _HoldStubExpert(expert_id)
    resolver.register_expert(expert_id, expert)

    account.submit_option_order(
        legs=[OptionLeg(contract_symbol=_OCC, side=OrderDirection.BUY,
                        position_intent="buy_to_open", option_type=OptionRight.CALL,
                        strike=_STRIKE, expiry=_EXPIRY, underlying="AAPL")],
        quantity=1, order_type="market", option_strategy="long_call")

    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, expert.settings, ruleset_id)],
        price_source=ps,
        config={"start_date": START, "end_date": END,
                "enabled_instruments": ["AAPL"], "seed": 42},
        indicator_provider=object())
    try:
        yield engine, account, expert, ps
    finally:
        ctx.__exit__(None, None, None)
        clear_worker_parquet_options_cache()


def test_parquet_backed_run_matches_the_sqlite_backed_run_exactly(engine_run):
    """Same fill (6.5), same marks (10.5 / 15.5 x 100), same ITM settlement (intrinsic 20),
    same final NLV (101,350) as ``test_options_e2e.py`` on the sqlite store."""
    engine, account, expert, ps = engine_run

    results = engine.run()

    history = account.get_balance_history()
    assert history, "expected per-bar equity snapshots"
    marked = {round(s["equity_value"], 6) for s in history}
    assert 10.5 * _MULTIPLIER in marked, marked
    assert 15.5 * _MULTIPLIER in marked, marked

    assert account.get_option_positions() == []
    assert [p for p in account.get_positions() if p["symbol"] == "AAPL"] == []

    assert account.get_balance() == pytest.approx(101_350.0)
    assert account.equity() == pytest.approx(101_350.0)
    assert results["final_equity"] == pytest.approx(101_350.0)
    assert results["initial_capital"] == pytest.approx(100_000.0)


def test_a_run_end_dated_before_a_premium_bar_cannot_see_it(engine_run):
    """The clamp holds through the ACCOUNT, not just the reader: on the fill bar the engine
    must price at 6.5 (that bar's open), never at the 15.5 close three bars later."""
    engine, account, expert, ps = engine_run
    ps.set_clock(datetime(2024, 2, 2))
    assert account._options.get_bar(_OCC, date(2024, 2, 2))["open"] == pytest.approx(6.5)
    assert account._options.get_bar(_OCC, date(2024, 2, 3)) is None
    assert account._options.get_bar(_OCC, date(2024, 2, 4)) is None
