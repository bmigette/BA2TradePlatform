"""Backtest borrow cost on open SHORT equity positions (plan 2026-09-24 equity short selling, S4).

``short_borrow_rate_pa`` (annual, default 0.005 = 0.5%/yr) is charged by ``BacktestAccount``
once per trading session, on the session's last bar, at ``|qty| x close x rate / 252`` for every
open short, debited from cash. Pinned here:

  * a held short accrues exactly rate/252 x market value per session over a known price path,
    in the REAL engine (a short opened from flat by a bearish rule and covered by a bullish
    ``buy`` exit);
  * covering stops the accrual (no charge on the sessions after the cover);
  * an explicit 0 charges nothing;
  * ``build_results`` echoes the rate and reports ``short_borrow_cost`` on its own line;
  * the knob survives every hop: payload -> ``_build_config`` -> ``_build_daily_trial_config``
    -> the trial's account (the whitelist trap);
  * a long-only run is byte-identical at the default rate and at 0 (orders, trades, equity);
  * the once-per-session rule on an intraday clock (only the session's last bar charges);
  * bad rates are refused.

Both trade-store modes for the engine runs; the SQLite mode forces the leaked in-memory flag off
and asserts it (``_store_mode``).

Run from the backend dir:
    python -m pytest tests/backtest/test_short_borrow_cost.py -v
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List

import pytest

from ba2_common.core.types import OrderRecommendation

from tests.backtest.test_max_loss_stop_engine import CFG, _store_mode
from tests.backtest.test_short_selling_engine import (
    BRACKET, DAY1, DRIFT, LONG_ONLY, LONG_ONLY_EXITS, _SignalStubExpert, _signal_rule)

BUY, SELL = OrderRecommendation.BUY, OrderRecommendation.SELL

# DRIFT: a bearish signal on 2024-01-02 opens the short (100 shares at the next open, 100); a
# bullish signal on 2024-01-05 fires the ``buy`` exit, which covers at the next open (95). Neither
# bracket leg (stop 108, target 90) is touched. The engine books a fill on the bar the order was
# worked (it fills at the NEXT bar's open while the clock is on the placing bar), so the ledger
# holds the short into the close of exactly three sessions -- the curve marks it at these
# closes -- and is flat from the 2024-01-05 snapshot on:
HELD_CLOSES = {date(2024, 1, 2): 100.0, date(2024, 1, 3): 98.0, date(2024, 1, 4): 96.0}
COVER_DAY = date(2024, 1, 5)
SHORT_SIGNALS = {DAY1: SELL, date(2024, 1, 5): BUY}
COVER_RULES = [_signal_rule("bullish", "buy")]


def _day(d: Any) -> date:
    return d.date() if isinstance(d, datetime) else d


def _run(bars, signals, *, run_id, inmem, enable_short, account_settings,
         exit_rules=None) -> Dict[str, Any]:
    """Full engine.run() with the given account settings; everything read before teardown."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import (
        seed_exit_ruleset_from_rules, seed_ruleset_from_tree)
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.results import build_results
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core import trade_store

    account_id = expert_id = run_id
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"short-borrow-{run_id}")
    ctx.__enter__()
    try:
        assert trade_store.inmem_trades_active() == (inmem == "1"), "store mode is not the one asked for"
        seed_account_definition(account_id, account_settings)
        enter_id = seed_ruleset_from_tree(None, name=f"borrow-enter-{run_id}",
                                          enable_short=enable_short, entry_actions=BRACKET)
        open_id = (seed_exit_ruleset_from_rules(exit_rules, name=f"borrow-open-{run_id}")
                   if exit_rules else None)
        seed_expert_instance(account_id=account_id, expert_class_name="_SignalStubExpert",
                             enter_market_ruleset_id=enter_id, open_positions_ruleset_id=open_id,
                             instance_id=expert_id)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c,
                               "Volume": 1000} for (d, o, h, low, c) in bars])
        account = BacktestAccount(account_id, ps, account_settings)
        resolver.register_account(account_id, account)
        expert = _SignalStubExpert(expert_id, ps, signals)
        expert.save_settings({
            "allow_automated_trade_opening": (True, "bool"),
            "allow_automated_trade_modification": (True, "bool"),
            "enable_buy": (True, "bool"),
            "enable_sell": (bool(enable_short), "bool"),
            "sizing_mode": ("risk_atr", "str"),
            "risk_per_trade_pct": (8.0, "float"),
            "min_stop_loss_pct": (8.0, "float"),
            "use_atr_stop": (False, "bool"),
        })
        resolver.register_expert(expert_id, expert)
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, {}, enter_id)], price_source=ps,
            config={"start_date": datetime.combine(bars[0][0], datetime.min.time()),
                    "end_date": datetime.combine(bars[-1][0], datetime.min.time()),
                    "enabled_instruments": ["AAPL"], "seed": 42, "enable_short": enable_short},
            indicator_provider=None)
        engine._indicator_provider = None
        engine.run()

        orders = sorted(
            (o.side.value, o.order_type.value, o.status.value, o.quantity, o.filled_qty,
             o.open_price, o.stop_price, o.limit_price, o.depends_on_order is None)
            for o in account.get_orders())
        results = build_results(account, {"initial_capital": account_settings["starting_cash"],
                                           "account_settings": account_settings})
        return {
            "orders": orders,
            "trades": account.get_round_trip_trades(),
            "equity": list(account.get_balance_history()),
            "cost": account.short_borrow_cost,
            "rate": account.short_borrow_rate_pa,
            "results": results,
        }
    finally:
        ctx.__exit__(None, None, None)


def _run_id(base, inmem):
    return base + (100 if inmem == "0" else 0)


def _cash_by_day(equity: List[Dict[str, Any]]) -> Dict[date, float]:
    return {_day(s["date"]): s["cash_balance"] for s in equity}


# --------------------------------------------------------------------------- #
# The accrual, in the real engine
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_held_short_accrues_rate_over_252_times_market_value_each_session(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    rate = 0.10  # 10%/yr: large enough that every session's charge is far above float noise
    out = _run(DRIFT, SHORT_SIGNALS, run_id=_run_id(910, inmem), inmem=inmem, enable_short=True,
               account_settings={**CFG, "short_borrow_rate_pa": rate}, exit_rules=COVER_RULES)

    [trade] = out["trades"]
    assert trade["direction"] == "sell" and trade["exit_price"] == pytest.approx(95.0)
    qty = float(trade["size"])
    assert qty == 100.0
    # The charge is on the value the curve records: the short is marked at these closes.
    marks = {_day(s["date"]): s["equity_value"] for s in out["equity"]}
    assert {d: marks[d] for d in HELD_CLOSES} == {d: -qty * c for d, c in HELD_CLOSES.items()}
    assert all(v == 0.0 for d, v in marks.items() if d >= COVER_DAY)

    per_session = {d: qty * close * rate / 252 for d, close in HELD_CLOSES.items()}
    assert out["cost"] == pytest.approx(sum(per_session.values()), rel=1e-12)
    assert out["rate"] == rate

    # Session by session: on the two held sessions with no fill, the cash moves by exactly the
    # borrow charge (the resting bracket legs move no cash).
    cash = _cash_by_day(out["equity"])
    days = sorted(cash)
    prev = {d: days[i - 1] for i, d in enumerate(days) if i}
    for d in (date(2024, 1, 3), date(2024, 1, 4)):
        assert cash[prev[d]] - cash[d] == pytest.approx(per_session[d], rel=1e-9), d


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_covering_stops_the_accrual(monkeypatch, inmem):
    """After the cover fills (booked on the 2024-01-05 bar) nothing more is charged: the cash on
    the last session is the cash right after the cover, and the total is the three held
    sessions only."""
    _store_mode(monkeypatch, inmem)
    rate = 0.10
    out = _run(DRIFT, SHORT_SIGNALS, run_id=_run_id(920, inmem), inmem=inmem, enable_short=True,
               account_settings={**CFG, "short_borrow_rate_pa": rate}, exit_rules=COVER_RULES)
    qty = float(out["trades"][0]["size"])
    cash = _cash_by_day(out["equity"])
    last = max(cash)
    assert last > COVER_DAY
    assert cash[last] == cash[COVER_DAY], "a covered short must not accrue"
    assert out["cost"] == pytest.approx(qty * sum(HELD_CLOSES.values()) * rate / 252, rel=1e-12)


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_an_explicit_zero_rate_charges_nothing(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    zero = _run(DRIFT, SHORT_SIGNALS, run_id=_run_id(930, inmem), inmem=inmem, enable_short=True,
                account_settings={**CFG, "short_borrow_rate_pa": 0.0}, exit_rules=COVER_RULES)
    charged = _run(DRIFT, SHORT_SIGNALS, run_id=_run_id(931, inmem), inmem=inmem,
                   enable_short=True, account_settings={**CFG, "short_borrow_rate_pa": 0.10},
                   exit_rules=COVER_RULES)
    assert zero["cost"] == 0.0
    assert zero["results"]["short_borrow_cost"] == 0.0
    assert zero["results"]["short_borrow_rate_pa"] == 0.0
    # Same trade (pnl_pct aside: it is P&L over the ENTRY bar's equity, which already carries
    # that session's borrow charge), and the charged run ends poorer by exactly its borrow cost.
    strip = lambda o: [{k: v for k, v in t.items() if k != "pnl_pct"}  # noqa: E731
                       for t in o["trades"]]
    assert strip(zero) == strip(charged)
    assert zero["orders"] == charged["orders"]
    final = lambda o: o["equity"][-1]["net_liquidating_value"]  # noqa: E731
    assert final(zero) - final(charged) == pytest.approx(charged["cost"], rel=1e-9)


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_the_default_rate_applies_when_the_config_states_none(monkeypatch, inmem):
    """A config without the key (every stored pre-S4 config) is charged the 0.5%/yr default."""
    from app.services.backtest.backtest_account import DEFAULT_SHORT_BORROW_RATE_PA

    _store_mode(monkeypatch, inmem)
    assert "short_borrow_rate_pa" not in CFG
    out = _run(DRIFT, SHORT_SIGNALS, run_id=_run_id(940, inmem), inmem=inmem, enable_short=True,
               account_settings=dict(CFG), exit_rules=COVER_RULES)
    qty = float(out["trades"][0]["size"])
    assert DEFAULT_SHORT_BORROW_RATE_PA == 0.005
    assert out["rate"] == 0.005
    assert out["cost"] == pytest.approx(qty * sum(HELD_CLOSES.values()) * 0.005 / 252, rel=1e-12)


# --------------------------------------------------------------------------- #
# build_results
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_build_results_echoes_the_rate_and_reports_the_cost_on_its_own_line(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    out = _run(DRIFT, SHORT_SIGNALS, run_id=_run_id(950, inmem), inmem=inmem, enable_short=True,
               account_settings={**CFG, "short_borrow_rate_pa": 0.10}, exit_rules=COVER_RULES)
    res = out["results"]
    assert res["short_borrow_rate_pa"] == 0.10
    assert res["short_borrow_cost"] == round(out["cost"], 2)
    assert res["short_borrow_cost"] > 0


def test_build_results_on_a_stub_account_resolves_the_rate_from_the_config():
    """A lightweight account stub (no borrow attributes) still gets both keys: the rate the
    config states (or the default) and a zero cost."""
    from app.services.backtest.results import build_results

    class _Stub:
        def get_balance_history(self):
            return [{"date": datetime(2024, 1, 2), "net_liquidating_value": 100.0},
                    {"date": datetime(2024, 1, 3), "net_liquidating_value": 101.0}]

        def get_filled_trades(self):
            return []

    stated = build_results(_Stub(), {"initial_capital": 100.0,
                                     "account_settings": {"short_borrow_rate_pa": 0.02}})
    assert stated["short_borrow_rate_pa"] == 0.02 and stated["short_borrow_cost"] == 0.0
    bare = build_results(_Stub(), {"initial_capital": 100.0})
    assert bare["short_borrow_rate_pa"] == 0.005 and bare["short_borrow_cost"] == 0.0


# --------------------------------------------------------------------------- #
# No impact on a long-only run
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_long_only_run_is_byte_identical_at_the_default_rate_and_at_zero(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    signals = {d: BUY for (d, *_rest) in LONG_ONLY}
    default = _run(LONG_ONLY, signals, run_id=_run_id(960, inmem), inmem=inmem,
                   enable_short=False, account_settings=dict(CFG), exit_rules=LONG_ONLY_EXITS)
    zero = _run(LONG_ONLY, signals, run_id=_run_id(961, inmem), inmem=inmem, enable_short=False,
                account_settings={**CFG, "short_borrow_rate_pa": 0.0},
                exit_rules=LONG_ONLY_EXITS)

    assert len(default["trades"]) >= 2 and all(t["direction"] == "buy" for t in default["trades"])
    assert default["rate"] == 0.005 and zero["rate"] == 0.0
    assert default["orders"] == zero["orders"]
    assert default["trades"] == zero["trades"]
    assert default["equity"] == zero["equity"]
    assert default["cost"] == 0.0 and zero["cost"] == 0.0


# --------------------------------------------------------------------------- #
# The account method on its own: once per session, the assignment exemption
# --------------------------------------------------------------------------- #
class _Prices:
    """The two marks the accrual reads, with no clock."""

    def __init__(self, closes):
        self._closes = closes

    def close_at(self, symbol):
        return self._closes.get(symbol)

    def close_asof(self, symbol):
        return self._closes.get(symbol)


def _bare_account(rate, closes):
    """A BacktestAccount skeleton holding only what ``accrue_short_borrow`` reads."""
    from app.services.backtest.backtest_account import BacktestAccount, _Position

    acct = BacktestAccount.__new__(BacktestAccount)
    acct._price = _Prices(closes)
    acct._cash = 10_000.0
    acct._positions = {}
    acct._pending_assignment_sells = {}
    acct._short_borrow_rate_pa = rate
    acct._short_borrow_cost = 0.0
    acct._borrow_session = None

    def hold(symbol, qty, avg=100.0):
        acct._positions[symbol] = _Position(symbol=symbol, qty=qty, avg_price=avg)

    return acct, hold


def test_only_one_charge_per_session_even_when_called_on_every_intraday_bar():
    acct, hold = _bare_account(0.252, {"AAPL": 50.0, "MSFT": 10.0})
    hold("AAPL", -10.0)
    hold("MSFT", 5.0)  # a long pays no borrow
    first = acct.accrue_short_borrow(datetime(2024, 1, 3, 20, 55))
    assert first == pytest.approx(10 * 50.0 * 0.252 / 252)  # = 0.5
    assert acct.accrue_short_borrow(datetime(2024, 1, 3, 21, 0)) == 0.0
    assert acct.accrue_short_borrow(datetime(2024, 1, 4, 21, 0)) == pytest.approx(0.5)
    assert acct._cash == pytest.approx(10_000.0 - 1.0)
    assert acct.short_borrow_cost == pytest.approx(1.0)


def test_the_engine_charges_on_the_last_bar_of_each_session_only():
    from app.services.backtest.daily_engine import _is_session_close

    bars = [datetime(2024, 1, 3, 14, 30), datetime(2024, 1, 3, 20, 55),
            datetime(2024, 1, 4, 14, 30), datetime(2024, 1, 4, 20, 55)]
    assert [_is_session_close(bars, i) for i in range(4)] == [False, True, False, True]
    daily = [date(2024, 1, 3), date(2024, 1, 4)]
    assert [_is_session_close(daily, i) for i in range(2)] == [True, True]


def test_no_short_no_charge_and_no_cash_touch():
    acct, hold = _bare_account(0.005, {"AAPL": 50.0})
    hold("AAPL", 10.0)
    assert acct.accrue_short_borrow(date(2024, 1, 3)) == 0.0
    assert acct._cash == 10_000.0 and acct.short_borrow_cost == 0.0


def test_assignment_short_queued_for_liquidation_is_not_charged():
    """Short stock from an assigned naked call, queued for next-open liquidation, exists for one
    overnight under the no-orphaned-stock policy and is exempt (charging it would move stored
    option backtests). A strategy short held beside it is charged on its own shares only."""
    acct, hold = _bare_account(0.252, {"AAPL": 50.0})
    hold("AAPL", -100.0)
    acct._pending_assignment_sells["AAPL"] = -100.0
    assert acct.accrue_short_borrow(date(2024, 1, 3)) == 0.0
    acct._positions["AAPL"].qty = -130.0  # 30 strategy-short shares on top
    assert acct.accrue_short_borrow(date(2024, 1, 4)) == pytest.approx(30 * 50.0 * 0.252 / 252)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf"), True, "abc"])
def test_a_bad_rate_is_refused(bad):
    from app.services.backtest.backtest_account import resolve_short_borrow_rate_pa

    with pytest.raises((TypeError, ValueError)):
        resolve_short_borrow_rate_pa({"short_borrow_rate_pa": bad})


def test_the_account_refuses_a_bad_rate_at_construction():
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource

    with pytest.raises(ValueError):
        BacktestAccount(1, AsOfPriceSource(ohlcv_provider=None),
                        {**CFG, "short_borrow_rate_pa": -0.5})


# --------------------------------------------------------------------------- #
# Plumbing: payload -> run config -> trial config -> the trial's account
# --------------------------------------------------------------------------- #
def _payload(**extra):
    return {"backtest_id": 1, "start_date": "2024-01-02", "end_date": "2024-02-01",
            "experts": ["FMPRating"], "enabled_instruments": ["AAPL"],
            "initial_capital": 10_000.0, "commission": 0.0, "slippage": 0.0,
            "fill_model": "next_bar_open", "seed": 42, "warmup_days": 0, **extra}


def test_the_run_config_states_the_rate_default_or_given():
    from app.services.backtest.daily_backtest_handler import _build_config

    assert _build_config(_payload())["account_settings"]["short_borrow_rate_pa"] == 0.005
    given = _build_config(_payload(short_borrow_rate_pa=0.03))
    assert given["account_settings"]["short_borrow_rate_pa"] == 0.03
    assert _build_config(_payload(short_borrow_rate_pa=0))["account_settings"][
        "short_borrow_rate_pa"] == 0.0
    with pytest.raises(ValueError):
        _build_config(_payload(short_borrow_rate_pa=-1))


def test_the_rate_survives_the_trial_config_whitelist_and_reaches_the_account():
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.strategy_optimization_handler import _build_daily_trial_config

    backtest_cfg = {
        "backtest_id": 1, "name": "t", "start_date": "2024-01-01", "end_date": "2024-02-01",
        "enabled_instruments": ["AAPL"], "initial_capital": 10_000.0, "warmup_days": 0,
        "seed": 42, "account_settings": {**CFG, "short_borrow_rate_pa": 0.0375},
        "experts": [{"class": "FMPRating", "settings": {}}],
    }
    decoded = {"expert_overrides": {}, "screener_overrides": {}, "schedule_days": None,
               "entry_rules": None, "exit_rules": None}
    trial = _build_daily_trial_config(backtest_cfg, decoded, None, option_trade_records=False)
    assert trial["account_settings"]["short_borrow_rate_pa"] == 0.0375
    # run_daily_backtest builds the account from exactly this block.
    account = BacktestAccount(1, AsOfPriceSource(ohlcv_provider=None), trial["account_settings"])
    assert account.short_borrow_rate_pa == 0.0375
