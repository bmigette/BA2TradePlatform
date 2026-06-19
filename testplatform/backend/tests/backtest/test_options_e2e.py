"""Phase 1 Task 11: ENGINE end-to-end — the whole option lifecycle through ``DailyBacktestEngine.run()``.

This is the integration capstone for the options backtester. Unlike the focused
single-concern tests (``test_option_fills``: fills; ``test_option_expiry``: the
``_apply_option_expiry`` hook in isolation), this drives the REAL engine bar loop end to
end over a fixture options cache and proves the three pieces work TOGETHER:

  1. FILL  : a long call submitted before the run FILLS off the cached premium bar on the
             first bar's ``refresh_orders`` (next_bar_open) -> a held option position +
             a per-bar equity-curve point reflecting premium x 100.
  2. MARK  : while held, each bar's ``snapshot_equity`` values the option at the current
             premium close x qty x multiplier (the equity curve tracks the premium).
  3. EXPIRY: on the expiry bar the underlying closes ITM (200 > strike 180), so the engine's
             per-bar ``_apply_option_expiry`` exercises the call -> the option position is
             gone AND a LONG equity position of 100 AAPL shares at the strike (180) exists,
             with the final NLV reflecting the exercise.

Harness: modelled on ``test_daily_engine_stop.py`` (fresh trading DB + seam wiring + seeded
account/expert + ``AsOfPriceSource`` + ``DailyBacktestEngine(...).run()``). The expert is a
deterministic HOLD stub (it never stages equity orders), and the long call is submitted on the
account BEFORE ``.run()`` so the engine's first-bar ``refresh_orders`` fills it (the same path
``test_option_fills`` exercises, but here inside the real loop).

Run from the backend dir:
    ./venv/bin/python -m pytest tests/backtest/test_options_e2e.py -q
"""
from __future__ import annotations

import os
from datetime import date, datetime

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.option_types import OptionLeg
from ba2_common.core.types import (
    OptionRight,
    OrderDirection,
    OrderRecommendation,
    Recommendation,
)


# --------------------------------------------------------------------------- #
# Fixture data — ONE underlying (AAPL), ONE call contract (strike 180, exp 2024-02-07).
# --------------------------------------------------------------------------- #
_OCC = "AAPL240207C00180000"  # AAPL 2024-02-07 CALL strike 180
_STRIKE = 180.0
_EXPIRY = date(2024, 2, 7)
_MULTIPLIER = 100

START = datetime(2024, 2, 1)
END = datetime(2024, 2, 7)

# Underlying daily OHLCV. The clock starts 2024-02-01 (entry/submit bar); the call fills
# next_bar_open at 2024-02-02. The underlying climbs and ENDS the expiry day (2024-02-07)
# at close 200 > strike 180 -> the call is deep ITM and exercises.
#                       (date,            open, high, low,  close)  weekday
_AAPL_BARS = [
    (date(2024, 2, 1), 185, 186, 184, 185),   # Thu -> submit/entry bar (clock starts)
    (date(2024, 2, 2), 186, 188, 185, 187),   # Fri -> fill bar (call fills @ open premium 2.0)
    (date(2024, 2, 5), 188, 191, 187, 190),   # Mon -> held / marked
    (date(2024, 2, 6), 191, 196, 190, 195),   # Tue -> held / marked
    (date(2024, 2, 7), 198, 201, 197, 200),   # Wed -> EXPIRY: close 200 > 180 -> ITM exercise
]

# Per-contract premium bars across the holding period. The fill bar (2024-02-02) opens at
# 2.0 (the entry premium); the premium then RISES 2.0 -> 3.0 across the hold (marking tracks
# the close). No premium bar on the expiry day is needed (expiry resolves off the underlying).
#               (date,            open, high, low, close)
_PREMIUM_BARS = [
    (date(2024, 2, 2), 2.0, 2.3, 1.9, 2.2),   # fill bar: open 2.0 (entry premium)
    (date(2024, 2, 5), 2.2, 2.7, 2.1, 2.5),   # marked @ close 2.5
    (date(2024, 2, 6), 2.5, 3.2, 2.4, 3.0),   # marked @ close 3.0 (premium peaked)
]

CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,   # zero -> a clean, fully deterministic NLV check
    "slippage_bps": 0.0,           # zero -> fill premium == bar open exactly
    "fill_model": "next_bar_open",
}


# --------------------------------------------------------------------------- #
# A deterministic HOLD stub expert: it NEVER stages an equity order, so the only thing
# that moves the book is the long call we submit before the run. (We submit the option on
# the account directly rather than from the expert because the engine processes pre-existing
# working orders on the first bar's refresh_orders — the simplest deterministic injection.)
# --------------------------------------------------------------------------- #
class _HoldStubExpert(MarketExpertInterface):
    bypasses_classic_rm = False

    def __init__(self, id: int):
        super().__init__(id)
        # No entry schedule -> "every bar"; analyze_as_of returns HOLD so nothing is staged.
        self._settings_cache = {}
        self.seen_as_of: list = []

    @classmethod
    def description(cls) -> str:  # abstract
        return "Stub HOLD expert for the options engine e2e test."

    def render_market_analysis(self, market_analysis) -> str:  # abstract
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:  # abstract
        return None

    def analyze_as_of(self, as_of, context):
        self.seen_as_of.append(as_of.date() if hasattr(as_of, "date") else as_of)
        return Recommendation(
            signal=OrderRecommendation.HOLD,
            confidence=0.0,
            current_price=None,
            details="hold (no equity orders)",
            raw_outputs={},
        )


def _underlying_rows(rows):
    return [
        {"Date": d, "Open": o, "High": h, "Low": low, "Close": c, "Volume": 1000}
        for (d, o, h, low, c) in rows
    ]


def _seed_cache(db_path: str) -> None:
    """Seed the CALL chain (dated at run start) + per-contract premium bars over the hold."""
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL",
        START.date().isoformat(),  # chain snapshot dated at run start (2024-02-01)
        [
            {
                "occ_symbol": _OCC,
                "option_type": "call",
                "strike": _STRIKE,
                "expiry": _EXPIRY.isoformat(),
                "bid": 1.9,
                "ask": 2.1,
                "last": 2.0,
                "iv": 0.25,
            },
        ],
    )
    cache.write_bar_rows(
        [
            {
                "occ_symbol": _OCC,
                "date": d.isoformat(),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": 400,
                "underlying": "AAPL",
                "option_type": "call",
                "strike": _STRIKE,
                "expiry": _EXPIRY.isoformat(),
            }
            for (d, o, h, low, c) in _PREMIUM_BARS
        ]
    )


@pytest.fixture
def engine_run(tmp_path):
    """Wire the full engine over the fixture cache and submit one long call before the run.

    Returns (engine, account, expert, price_source). The trading-DB context is torn down
    on fixture exit.
    """
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db,
        seed_account_definition,
        seed_expert_instance,
    )
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams

    account_id = 71
    expert_id = 71

    cache_db = str(tmp_path / "options_cache.sqlite")
    _seed_cache(cache_db)
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db("options-e2e")
    ctx.__enter__()

    seed_account_definition(account_id, CFG)
    ruleset_id = seed_enter_long_ruleset(name=f"options-e2e-{account_id}")
    seed_expert_instance(
        account_id=account_id,
        expert_class_name="_HoldStubExpert",
        enter_market_ruleset_id=ruleset_id,
        instance_id=expert_id,
    )

    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _underlying_rows(_AAPL_BARS))
    ps.set_clock(START)  # clock on the submit bar so the order is staged ACCEPTED

    account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
    resolver.register_account(account_id, account)

    expert = _HoldStubExpert(expert_id)
    resolver.register_expert(expert_id, expert)

    # Submit the long call BEFORE the run. It is staged ACCEPTED (working) and the engine's
    # first-bar refresh_orders fills it off the cached premium bar (next_bar_open -> 2024-02-02).
    leg = OptionLeg(
        contract_symbol=_OCC,
        side=OrderDirection.BUY,
        position_intent="buy_to_open",
        option_type=OptionRight.CALL,
        strike=_STRIKE,
        expiry=_EXPIRY,
        underlying="AAPL",
    )
    account.submit_option_order(
        legs=[leg], quantity=1, order_type="market", option_strategy="long_call"
    )

    config = {
        "start_date": START,
        "end_date": END,
        "enabled_instruments": ["AAPL"],
        "seed": 42,
    }
    engine = DailyBacktestEngine(
        account=account,
        experts=[(expert, expert_id, expert.settings, ruleset_id)],
        price_source=ps,
        config=config,
        indicator_provider=object(),
    )
    try:
        yield engine, account, expert, ps
    finally:
        ctx.__exit__(None, None, None)


def test_engine_e2e_option_fill_mark_and_exercise(engine_run):
    """End-to-end: the long call FILLS, is MARKED on the equity curve, and at expiry (ITM)
    converts to a 100-share AAPL position at the strike, with the final NLV reflecting it."""
    engine, account, expert, ps = engine_run

    results = engine.run()

    # --- Assertion 1: the call FILLED and at least one equity point reflects premium x 100 ---
    # (The held option was marked while open; the curve carries an equity_value at premium x 100.)
    history = account.get_balance_history()
    assert history, "expected per-bar equity snapshots"
    # The premium-close marks over the hold are 2.5 (02-05) and 3.0 (02-06): 250 and 300.
    marked_values = {round(s["equity_value"], 6) for s in history}
    assert 2.5 * _MULTIPLIER in marked_values, (
        f"expected an equity point marking the option at premium 2.5 x 100, got {marked_values}"
    )
    assert 3.0 * _MULTIPLIER in marked_values, (
        f"expected an equity point marking the option at premium 3.0 x 100, got {marked_values}"
    )

    # --- Assertion 2: at expiry the option is GONE and a 100-share LONG AAPL @ strike exists ---
    assert account.get_option_positions() == [], (
        "expected the call exercised/closed at expiry, none held"
    )
    positions = account.get_positions()
    aapl = [p for p in positions if p["symbol"] == "AAPL"]
    assert len(aapl) == 1, f"expected exactly one AAPL equity position, got {aapl}"
    assert aapl[0]["qty"] == 100, f"expected 100 shares from exercise, got {aapl[0]['qty']}"
    assert aapl[0]["avg_price"] == pytest.approx(_STRIKE), (
        f"expected shares settled at the strike (180), got {aapl[0]['avg_price']}"
    )

    # --- Assertion 3: the final NLV reflects the exercise (deterministic, computed below) ---
    #   start cash 100,000; buy 1 call @ 2.0 x 100 = -200 -> cash 99,800;
    #   exercise: buy 100 AAPL @ strike 180 = -18,000 -> cash 81,800;
    #   shares marked at the expiry-day close 200 -> equity_value 100 x 200 = 20,000;
    #   final NLV = 81,800 + 20,000 = 101,800 (net +1,800 = 100 x (200-180) - 200 premium).
    assert account.get_balance() == pytest.approx(99_800.0 - 18_000.0)  # 81,800 cash
    final_nlv = account.equity()
    assert final_nlv == pytest.approx(101_800.0), f"final NLV {final_nlv} != 101,800"
    assert results["final_equity"] == pytest.approx(101_800.0)
    assert results["initial_capital"] == pytest.approx(100_000.0)
    # Deep-ITM call -> the run ENDED above the starting capital.
    assert final_nlv > results["initial_capital"]


# --------------------------------------------------------------------------- #
# Task 12: GATED live-Alpaca smoke test.
#
# Unlike the fixture-driven engine e2e above (which never touches Alpaca), this exercises
# the REAL ``build_cache`` fetch path against the live Alpaca options API for one liquid
# underlying over a short 2024 window, then asserts the sqlite cache got populated:
#   - a chain snapshot keyed at ``start.isoformat()`` (matches fetch_options.write_chain_rows),
#   - at least one per-contract ``option_bar`` row.
#
# GATING (must SKIP cleanly so keyless / CI runs never fail):
#   * No Alpaca creds in the environment -> SKIP. We resolve creds via the same env names
#     ``fetch_options._alpaca_keys()`` reads (ALPACA_MARKET_API_KEY/_SECRET, falling back to
#     ALPACA_API_KEY/ALPACA_SECRET_KEY). The codebase loads these from ``.env`` via
#     ``app.models.database`` -> ``load_dotenv()`` at import; collection order decides whether
#     that has happened yet, so we call ``load_dotenv()`` ourselves first to make the gate
#     DETERMINISTIC whether this file is run alone or inside the full suite.
#   * Creds present but Alpaca rejects them (401 unauthorized) or the account is not entitled
#     to the options endpoints -> SKIP, not fail. A present-but-invalid ``.env`` key is an
#     environment problem, not a defect in ``build_cache``; the task requires "all else green".
#     A genuine population bug (creds valid, request succeeds, but nothing cached) still FAILS
#     the row assertions below.
#
# The ``build_cache`` import is kept INSIDE the test so the lazy ``alpaca`` dependency
# (imported within build_cache) cannot break collection; running it live requires the editable
# venv ``~/ba2-venvs/test/bin/python`` which has alpaca-py installed.
# --------------------------------------------------------------------------- #
def _alpaca_keys_present() -> bool:
    """True iff usable Alpaca creds resolve. Loads .env first so the gate is order-independent."""
    try:
        from dotenv import load_dotenv

        load_dotenv()  # idempotent; populates os.environ from .env if not already loaded
    except Exception:
        pass
    return bool(
        (os.environ.get("ALPACA_MARKET_API_KEY") or os.environ.get("ALPACA_API_KEY")) and
        (os.environ.get("ALPACA_MARKET_API_SECRET") or os.environ.get("ALPACA_SECRET_KEY"))
    )


@pytest.mark.skipif(not _alpaca_keys_present(), reason="no Alpaca API keys in env")
def test_fetch_options_smoke(tmp_path):
    """Live end-to-end Alpaca fetch: build_cache populates chain + bars for one underlying."""
    from app.services.backtest.fetch_options import build_cache
    from app.services.backtest.options_cache import OptionsHistoryCache

    db = str(tmp_path / "smoke_opt.db")
    start, end = date(2024, 3, 1), date(2024, 3, 15)

    try:
        build_cache(db, ["AAPL"], start, end, feed="indicative")
    except Exception as exc:  # noqa: BLE001 - inspect for auth/entitlement, re-raise real bugs
        msg = str(exc).lower()
        if "unauthorized" in msg or "forbidden" in msg or "401" in msg or "403" in msg:
            pytest.skip(f"Alpaca creds present but not entitled for options endpoints: {exc}")
        raise

    cache = OptionsHistoryCache(db)
    # Chain is written keyed at start.isoformat() (fetch_options.write_chain_rows(u, start, ...)).
    rows = cache.read_chain("AAPL", start.isoformat())
    assert len(rows) >= 1, "expected at least one chain contract cached"

    # At least one per-contract premium bar should have landed too.
    import sqlite3

    n_bars = sqlite3.connect(db).execute("SELECT COUNT(*) FROM option_bar").fetchone()[0]
    assert n_bars >= 1, "expected at least one option_bar row cached"
