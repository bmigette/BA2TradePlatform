"""The two ENGINE facts a style expert's per-member isolation on one SYMBOL rests on.

Plan docs/plans/2026-09-25-option-stage2-build.md, Task 8, review note M4 ("Tasks 5-6 as
built"). A style expert is ONE expert: all its members trade the same symbols out of one
ENTER_MARKET ruleset and are managed by one OPEN_POSITIONS ruleset. Two members cannot tangle on
one symbol because of two engine rules, pinned here against the REAL ``DailyBacktestEngine``:

1. ONE POSITION PER EXPERT PER SYMBOL (``daily_engine._has_open_or_waiting_position``, the
   live-parity dup gate after the enter ruleset fires; live: TradeManager's enter_market SAFETY
   CHECK, the same OPENED+WAITING query). While member A holds a symbol, member B's entry rule
   may pass and B still never opens there -- whatever A's tag, since the gate is keyed on
   (expert, symbol) only. The member entry rules below deliberately carry NO ``has_no_position``
   leaf, so the engine gate is the only thing between them.
2. EXITS READ THE OLDEST TRANSACTION'S ENTRY ORDER (``daily_engine._oldest_entry_order``; live:
   ``oldest_transaction`` -> ``resolve_entry_order``). That order's ``entry_tag`` is what every
   member exit compares against, so member B's exit -- here one that is ALWAYS true for B --
   never closes A's position, and A's own exit does.

Run from the backend dir:
    python -m pytest tests/backtest/test_style_member_isolation_engine.py -q
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.backtest._no_atr import NoAtrIndicatorProvider
from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, OrderStatus, Recommendation

#: Member tags (arbitrary positive ints; the real table is the launcher's _STYLE_MEMBER_TAGS).
TAG_A, TAG_B = 7, 3
SYM = "ISOX"
START = datetime(2024, 1, 2)
END = datetime(2024, 1, 31)
#: A's rule fires while confidence >= 80 (the first bar only); B's while < 80 (every bar after).
FLIP = date(2024, 1, 3)
#: A's exit: days_opened > A_HOLD. B's exit: always true for B (days_opened >= 0).
A_HOLD = 5
EXPIRY = date(2024, 2, 16)                       # beyond END: nothing expires in the run
PUT = f"{SYM}{EXPIRY:%y%m%d}P00020000"

CFG = {**_LEGACY_ZERO_SPREAD, "starting_cash": 100_000.0, "commission_per_trade": 0.0,
       "slippage_bps": 0.0, "fill_model": "next_bar_open"}


def _sessions():
    """Weekdays minus the one NYSE holiday in the window (2024-01-15, MLK day)."""
    out, d = [], START.date()
    while d <= END.date():
        if d.weekday() < 5 and d != date(2024, 1, 15):
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _sessions()


class _FlipExpert(MarketExpertInterface):
    """BUY every bar; confidence 90 until FLIP, 60 from it (so A's rule, then B's)."""

    bypasses_classic_rm = False
    _gather_symbol = None

    def __init__(self, id: int):
        super().__init__(id)
        self._settings_cache = {}

    @classmethod
    def description(cls) -> str:
        return "Stub BUY expert whose confidence flips once (style member isolation test)."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        conf = 90.0 if as_of.date() < FLIP else 60.0
        return Recommendation(
            signal=OrderRecommendation.BUY, confidence=conf,
            current_price=context.account.get_instrument_current_price(self._gather_symbol),
            details="buy", expected_profit_percent=25.0, raw_outputs={})


def _csp(tag):
    return {"action_type": "sell_cash_secured_put", "option_strike_method": "percent_otm",
            "option_strike_param": 0.0, "option_dte_min": 1, "option_dte_max": 60,
            "option_sizing": 5.0, "option_entry_tag": tag}


def _entry_rules():
    """Two members, NO flat leaf: only the engine's dup gate keeps them apart."""
    return [
        {"id": "a_entry", "conditions": {"type": "AND", "conditions": [
            {"id": "a_conf", "field": "confidence", "op": ">=", "value": 80.0}]},
         "actions": [_csp(TAG_A)]},
        {"id": "b_entry", "conditions": {"type": "AND", "conditions": [
            {"id": "b_conf", "field": "confidence", "op": "<", "value": 80.0}]},
         "actions": [_csp(TAG_B)]},
    ]


def _exit_rules():
    """B's exit comes FIRST and is always true for a B position; A's needs A_HOLD days."""
    def tagged(rid, tag, days_leaf):
        return {"id": rid, "action_type": "close_option", "conditions": {"type": "AND",
                "conditions": [days_leaf, {"id": f"{rid}-tag", "field": "entry_tag",
                                           "op": "==", "value": float(tag)}]}}
    return [tagged("b_close", TAG_B, {"id": "b_td", "field": "days_opened", "op": ">=",
                                      "value": 0.0}),
            tagged("a_close", TAG_A, {"id": "a_td", "field": "days_opened", "op": ">",
                                      "value": float(A_HOLD)})]


def _run(account_id, monkeypatch):
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
    from ba2_common.core.trade_store import orders_where, transactions_where
    from tests.backtest.fixtures.e2e_support import hermetic_providers

    logs = []
    monkeypatch.setattr(DailyBacktestEngine, "_log", staticmethod(logs.append))

    tmpdir = tempfile.mkdtemp(prefix="style-iso-")
    cache_db = os.path.join(tmpdir, "options_cache.sqlite")
    cache = OptionsHistoryCache(cache_db)
    cache.write_chain_rows(SYM, START.date().isoformat(), [
        {"occ_symbol": PUT, "option_type": "put", "strike": 20.0,
         "expiry": EXPIRY.isoformat(), "bid": 0.50, "ask": 0.50, "last": 0.50, "iv": 0.30,
         "delta": -0.30, "open_interest": 5000}])
    cache.write_bar_rows([
        {"occ_symbol": PUT, "date": d.isoformat(), "open": 0.50, "high": 0.55, "low": 0.45,
         "close": 0.50, "volume": 500, "underlying": SYM, "option_type": "put", "strike": 20.0,
         "expiry": EXPIRY.isoformat(), "iv": 0.30, "delta": -0.30} for d in DAYS])
    provider = HistoricalOptionsProvider(cache_db)

    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"style-iso-{account_id}")
    ctx.__enter__()
    try:
        seed_account_definition(account_id, CFG)
        enter_id = seed_entry_ruleset_from_rules(normalize_trade_rules(_entry_rules()),
                                                 name=f"iso-enter-{account_id}")
        open_id = seed_exit_ruleset_from_rules(normalize_trade_rules(_exit_rules()),
                                               name=f"iso-open-{account_id}")
        seed_expert_instance(account_id=account_id, expert_class_name="_FlipExpert",
                             enter_market_ruleset_id=enter_id,
                             open_positions_ruleset_id=open_id, instance_id=account_id)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars(SYM, [{"Date": d, "Open": 20.0, "High": 20.1, "Low": 19.9, "Close": 20.0,
                            "Volume": 1_000_000} for d in DAYS])
        ps.set_clock(START)
        account = BacktestAccount(account_id, ps, CFG, options_provider=provider)
        resolver.register_account(account_id, account)
        expert = _FlipExpert(account_id)
        expert.save_settings({"allow_automated_trade_opening": (True, "bool"),
                              "allow_automated_trade_modification": (True, "bool"),
                              "enable_buy": (True, "bool")})
        resolver.register_expert(account_id, expert)
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, account_id, expert.settings, enter_id)],
            price_source=ps, config={"start_date": START, "end_date": END,
                                     "enabled_instruments": [SYM], "seed": 42},
            indicator_provider=NoAtrIndicatorProvider())
        with hermetic_providers():
            engine.run()
        orders = list(orders_where(account_id=account_id))
        txns = list(transactions_where(expert_id=account_id))
        rows = []
        for t in sorted(txns, key=lambda t: t.id):
            entry = account._entry_order_for_transaction(t)
            rows.append(SimpleNamespace(
                id=t.id, status=t.status, open_date=t.open_date, close_date=t.close_date,
                tag=(entry.data or {}).get("entry_tag") if entry is not None else None))
        return orders, rows, logs
    finally:
        ctx.__exit__(None, None, None)


@pytest.fixture(scope="module")
def iso_run():
    mp = pytest.MonkeyPatch()
    try:
        yield _run(9401, mp)
    finally:
        mp.undo()


def _day(dt):
    return dt.date() if hasattr(dt, "date") else dt


def test_member_A_opened_first_and_member_B_only_after_A_closed(iso_run):
    _orders, txns, _logs = iso_run
    assert txns and txns[0].tag == TAG_A, [(t.id, t.tag) for t in txns]
    a = txns[0]
    assert a.close_date is not None, "A's position never closed"
    later = [t for t in txns[1:] if t.open_date is not None]   # (a last-bar entry may wait)
    assert later and all(t.tag == TAG_B for t in later), [(t.id, t.tag) for t in txns]
    # the dup gate: no B transaction was opened while A's was open
    for t in later:
        assert _day(t.open_date) >= _day(a.close_date), (t.open_date, a.close_date)


def test_the_dup_gate_is_what_kept_B_out(iso_run):
    """B's rule passed on the bars A held the symbol (confidence had flipped), and the engine's
    dup gate -- not the rule -- refused it."""
    import re
    _orders, txns, logs = iso_run
    a = txns[0]
    assert _day(a.close_date) > FLIP
    gated = [date.fromisoformat(m_.group(1)) for m in logs
             if m.startswith(f"entry dup-gate: {SYM} ")
             for m_ in [re.search(r"@ (\d{4}-\d{2}-\d{2})", m)] if m_]
    during_a = [d for d in gated if FLIP <= d <= _day(a.close_date)]
    assert during_a, (f"B's rule never reached the dup gate while A held the symbol "
                      f"({_day(a.open_date)}..{_day(a.close_date)}); gate hits: {gated}")


def test_Bs_always_true_exit_never_closed_As_position(iso_run):
    """B's exit is first in the ruleset and true for ANY B position on its first managed bar;
    A's position read A's tag off its entry order and stayed open until A's OWN exit fired."""
    _orders, txns, _logs = iso_run
    a = txns[0]
    held_days = (_day(a.close_date) - _day(a.open_date)).days
    assert held_days > A_HOLD, (a.open_date, a.close_date)
    # ...while every B position was closed by B's exit almost at once
    for t in txns[1:]:
        if t.open_date is not None and t.close_date is not None:
            assert (_day(t.close_date) - _day(t.open_date)).days <= 3, (t.open_date,
                                                                        t.close_date)


def test_the_dup_gate_is_keyed_on_expert_and_symbol_not_on_the_member_tag():
    """The gate itself: ANY open or waiting transaction of the expert on the symbol blocks,
    whichever member opened it (so one member per symbol in a style expert)."""
    from app.services.backtest.backtest_db import backtest_trading_db
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from ba2_common.core.db import add_instance
    from ba2_common.core.models import Transaction
    from ba2_common.core.types import OrderDirection, TransactionStatus

    ctx = backtest_trading_db("style-iso-gate")
    ctx.__enter__()
    try:
        eng = DailyBacktestEngine.__new__(DailyBacktestEngine)
        add_instance(Transaction(expert_id=1, symbol=SYM, quantity=-1.0,
                                 side=OrderDirection.SELL, status=TransactionStatus.OPENED))
        assert eng._has_open_or_waiting_position(1, SYM) is True
        assert eng._has_open_or_waiting_position(2, SYM) is False
    finally:
        ctx.__exit__(None, None, None)


def test_exits_read_the_OLDEST_transactions_entry_order():
    """With several transactions on one symbol (an overlay or an assignment lot beside its
    position), the exit pass reads the OLDEST one's entry order -- whose tag every member exit
    compares -- whatever order the transactions are listed in."""
    from app.services.backtest.daily_engine import DailyBacktestEngine

    class _Acct:
        def _entry_order_for_transaction(self, txn):
            return SimpleNamespace(id=txn.id, data={"entry_tag": txn.tag})

    old = SimpleNamespace(id=1, tag=TAG_A, open_date=datetime(2024, 1, 3, tzinfo=timezone.utc),
                          created_at=None)
    new = SimpleNamespace(id=2, tag=TAG_B, open_date=datetime(2024, 1, 9, tzinfo=timezone.utc),
                          created_at=None)
    eng = object.__new__(DailyBacktestEngine)
    eng.account = _Acct()
    for txns in ([old, new], [new, old]):
        assert eng._oldest_entry_order(txns).data["entry_tag"] == TAG_A
