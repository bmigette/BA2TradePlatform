"""A funded entry the account refuses ends the same way in a backtest as live, in both trade stores;
and the account's per-instrument cap is the cap the risk manager sized against (regime overlay).

THE DEFECTS (measured 2026-11-17 on a 4-year backtest):

1. With the regime overlay on (``regime_overlay_enabled``, ``regime_risk_scale`` != 1) the classic RM
   scales the per-instrument cap in a stressed bar, but ``AccountInterface._validate_single_position_size``
   enforced the UNSCALED setting. At scale 1.5 the order the RM funded was refused at submit
   ("Order validation failed"), 44-46 times in one run.
2. After a refusal the backtest only LOGGED it (``daily_engine._size_and_submit_candidates``). Live
   cancels the order (``TradeManager._fail_unsent_entry``). With the in-memory trade store the refused
   order WAS the stored one, kept its sized quantity and was filled later (NVDA 373 shares); with the
   SQL store the stored order stayed at quantity 0 and was "filled" as a zero-share trade whose
   transaction then sat in CLOSING forever. GA trial != persisted re-run != live: one genome scored
   204 trades / +47.67% in memory and 195 / +30.18% on SQLite.

Run from the backend dir:
    python -m pytest tests/backtest/test_refused_entry_and_regime_cap.py -v
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.interfaces.MarketExpertInterface import MarketExpertInterface
from ba2_common.core.types import OrderRecommendation, Recommendation

from tests.backtest._spread_cfg import LEGACY_ZERO_SPREAD as _LEGACY_ZERO_SPREAD
from tests.backtest._no_atr import NoAtrIndicatorProvider

CFG = {
    **_LEGACY_ZERO_SPREAD, "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
}

#: Flat at $100 so the cap arithmetic is exact: $100k equity x 10% = a $10,000 base cap = 100 shares.
FLAT = [(date(2024, 1, d), 100.0, 100.5, 99.5, 100.0) for d in (2, 3, 4, 5, 8, 9, 10)]
PRICE = 100.0
BASE_CAP_SHARES = 100


class _StubExpert(MarketExpertInterface):
    """BUY on every bar in ``buy_on`` (all bars when None), HOLD otherwise. No providers."""

    def __init__(self, id: int, price_source, buy_on=None):
        super().__init__(id)
        self._ps = price_source
        self._buy_on = buy_on

    @classmethod
    def description(cls) -> str:
        return "Stub expert for the refused-entry / regime-cap engine tests."

    def render_market_analysis(self, market_analysis) -> str:
        return ""

    def run_analysis(self, symbol: str, market_analysis) -> None:
        return None

    def analyze_as_of(self, as_of, context):
        close = float(self._ps.close_at("AAPL", as_of))
        day = as_of.date() if hasattr(as_of, "date") else as_of
        buy = self._buy_on is None or day in self._buy_on
        return Recommendation(
            signal=OrderRecommendation.BUY if buy else OrderRecommendation.HOLD,
            confidence=80.0 if buy else 50.0,
            current_price=close,
            details="stub",
            expected_profit_percent=10.0 if buy else 0.0,
        )


class _AlwaysStressed:
    """A regime calendar whose every day is stressed (the engine publishes ``.at(day)`` per bar)."""

    def at(self, _day):
        return True


def _store_mode(monkeypatch, inmem: str) -> None:
    """Select the trade-store mode and make it TRUE on this thread (see test_max_loss_stop_engine)."""
    from ba2_common.core import trade_store
    monkeypatch.setenv("BT_INMEM_TRADES", inmem)
    if inmem == "0":
        monkeypatch.setattr(trade_store._tls, "active", False, raising=False)


def _run(*, run_id, inmem, settings, buy_on=None, regime_calendar=None, refuse_on=frozenset(),
         probe=None, entry_sl_pct=-20.0):
    """Full ``engine.run()``. ``refuse_on``: decision dates on which the account's validation
    refuses every entry (a refusal at submit, whatever its cause). ``probe(account)`` runs inside
    the run's DB context after the run. ``entry_sl_pct``: a ruleset entry stop (never reached on the
    flat bars) -- with it the entry's Transaction exists BEFORE the submit, which is what makes the
    account's per-instrument check run on it (as on every real ruleset with an entry bracket); None
    runs the bare enter-long ruleset (the transaction is created by the submit itself).
    Returns the outcome, read BEFORE teardown."""
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.backtest_db import (
        backtest_trading_db, seed_account_definition, seed_expert_instance)
    from app.services.backtest.daily_engine import DailyBacktestEngine
    from app.services.backtest.default_rulesets import seed_enter_long_ruleset, seed_ruleset_from_tree
    from app.services.backtest.price_source import AsOfPriceSource
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction

    account_id = expert_id = run_id
    resolver = wire_backtest_seams()
    ctx = backtest_trading_db(f"refused-entry-{run_id}")
    ctx.__enter__()
    try:
        from ba2_common.core import trade_store
        assert trade_store.inmem_trades_active() == (inmem == "1"), "store mode is not the one asked for"
        seed_account_definition(account_id, CFG)
        if entry_sl_pct is None:
            ruleset_id = seed_enter_long_ruleset()
        else:
            ruleset_id = seed_ruleset_from_tree(None, entry_actions=[
                {"id": "e_sl", "action_type": "adjust_stop_loss",
                 "reference_value": "order_open_price", "action_value": entry_sl_pct}])
        seed_expert_instance(account_id=account_id, expert_class_name="_StubExpert",
                             enter_market_ruleset_id=ruleset_id, instance_id=expert_id)
        ps = AsOfPriceSource(ohlcv_provider=None)
        ps.load_bars("AAPL", [{"Date": d, "Open": o, "High": h, "Low": low, "Close": c,
                               "Volume": 1000} for (d, o, h, low, c) in FLAT])
        account = BacktestAccount(account_id, ps, CFG)
        refusals = []
        if refuse_on:
            real_validate = account._validate_trading_order

            def _validate(order, is_closing_order=False):
                if not is_closing_order and ps.now().date() in refuse_on:
                    refusals.append((ps.now().date(), order.symbol, order.quantity))
                    return {"is_valid": False, "errors": ["refused by the test"], "warnings": []}
                return real_validate(order, is_closing_order=is_closing_order)

            account._validate_trading_order = _validate
        resolver.register_account(account_id, account)
        expert = _StubExpert(expert_id, ps, buy_on=buy_on)
        expert.save_settings({
            "allow_automated_trade_opening": (True, "bool"),
            "enable_buy": (True, "bool"),
            "use_atr_stop": (False, "bool"),
            **settings,
        })
        resolver.register_expert(expert_id, expert)
        engine = DailyBacktestEngine(
            account=account, experts=[(expert, expert_id, {}, ruleset_id)], price_source=ps,
            config={"start_date": datetime.combine(FLAT[0][0], datetime.min.time()),
                    "end_date": datetime.combine(FLAT[-1][0], datetime.min.time()),
                    "enabled_instruments": ["AAPL"], "seed": 42},
            indicator_provider=NoAtrIndicatorProvider(),
            regime_calendar=regime_calendar)
        engine.run()

        orders = account.get_orders()
        entries = sorted((o for o in orders if o.depends_on_order is None
                          and o.side.value == "BUY"), key=lambda o: o.id)
        from ba2_common.core.trade_cycle import row_or_none
        # A transaction that never opened is DELETED by the shared never-opened cleanup
        # (ReadOnlyAccountInterface.refresh_transactions) on the next roll. SQLite then nulls the
        # order's transaction_id; the in-memory store leaves the dangling id on the order (a
        # pre-existing bookkeeping difference that moves no trade). Both read here as None.
        txn_rows = {o.id: row_or_none(Transaction, o.transaction_id) for o in entries}
        txn_status = {oid: (t.status.value if t is not None else None)
                      for oid, t in txn_rows.items()}
        outcome = {
            "entries": [(o.status.value, o.quantity, o.filled_qty, txn_status[o.id]) for o in entries],
            "orders": sorted((o.side.value, o.order_type.value, o.status.value, o.quantity,
                              o.filled_qty, o.open_price) for o in orders),
            "trades": account.get_round_trip_trades(),
            "equity": list(account.get_balance_history()),
            "transactions": sorted(t.status.value for t in trade_store.transactions_where()),
            "refusals": refusals,
            "refused_entries": [(r["symbol"], r["cancelled"]) for r in engine.refused_entries],
        }
        if probe is not None:
            outcome["probe"] = probe(account, expert_id)
        return outcome
    finally:
        ctx.__exit__(None, None, None)


NOTIONAL = {"sizing_mode": ("notional", "str"),
            "max_virtual_equity_per_instrument_percent": (10.0, "float")}


def _overlay(scale):
    return {**NOTIONAL, "regime_overlay_enabled": (True, "bool"),
            "regime_risk_scale": (float(scale), "float")}


# --------------------------------------------------------------------------- #
# (a) the account enforces the cap the risk manager sized against
# --------------------------------------------------------------------------- #

def _probe_validation(quantities):
    """Inside the run (clock on its last bar, AAPL at $100): validate a fresh BUY of each quantity
    against the account in a stressed bar. Each probe gets its own WAITING transaction, failed again
    afterwards so it never counts against the next probe's balance."""
    def probe(account, expert_id):
        from ba2_common.core.db import add_instance, get_instance, update_instance
        from ba2_common.core.models import Transaction, TradingOrder
        from ba2_common.core.regime_overlay import reset_stressed, set_stressed
        from ba2_common.core.types import OrderDirection, OrderStatus, OrderType, TransactionStatus

        assert account.get_instrument_current_price("AAPL") == PRICE
        out = {}
        set_stressed(True)
        try:
            for qty in quantities:
                txn_id = add_instance(Transaction(
                    symbol="AAPL", quantity=0.0, side=OrderDirection.BUY,
                    status=TransactionStatus.WAITING, expert_id=expert_id))
                order = TradingOrder(account_id=account.id, symbol="AAPL", quantity=float(qty),
                                     side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                     status=OrderStatus.PENDING, transaction_id=txn_id)
                out[qty] = [e for e in account._validate_position_size_limits(order)
                            if "max allowed" in e or "Cannot validate" in e]
                txn = get_instance(Transaction, txn_id)
                txn.status = TransactionStatus.FAILED
                update_instance(txn)
        finally:
            reset_stressed()
        return out
    return probe


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_an_order_the_rm_sized_at_regime_scale_1_5_passes_validation(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    scaled = int(BASE_CAP_SHARES * 1.5)
    out = _run(run_id=610 + (100 if inmem == "0" else 0), inmem=inmem, settings=_overlay(1.5),
               buy_on={FLAT[0][0]}, regime_calendar=_AlwaysStressed(),
               probe=_probe_validation([scaled, scaled + 1, BASE_CAP_SHARES + 1]))
    [(status, qty, filled, _txn)] = out["entries"]
    assert qty == scaled, "the RM did not size against the regime-scaled cap"
    assert status == "filled" and filled == scaled
    # The account accepts exactly what the RM may fund, and refuses one share more.
    assert out["probe"][scaled] == []
    assert out["probe"][BASE_CAP_SHARES + 1] == [], "the account still enforces the UNSCALED cap"
    assert any("exceeds expert's max allowed" in e for e in out["probe"][scaled + 1])


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_regime_scale_0_5_refuses_what_the_unscaled_cap_would_allow(monkeypatch, inmem):
    _store_mode(monkeypatch, inmem)
    half = BASE_CAP_SHARES // 2
    out = _run(run_id=620 + (100 if inmem == "0" else 0), inmem=inmem, settings=_overlay(0.5),
               buy_on={FLAT[0][0]}, regime_calendar=_AlwaysStressed(),
               probe=_probe_validation([half, half + 1, BASE_CAP_SHARES]))
    [(status, qty, filled, _txn)] = out["entries"]
    assert (status, qty, filled) == ("filled", half, half)
    assert out["probe"][half] == []
    assert any("exceeds expert's max allowed" in e for e in out["probe"][half + 1])
    assert any("exceeds expert's max allowed" in e for e in out["probe"][BASE_CAP_SHARES]), (
        "a stressed bar at scale 0.5 must refuse what the unscaled cap would have allowed")


def test_unstressed_bars_keep_the_unscaled_cap(monkeypatch):
    _store_mode(monkeypatch, "1")
    out = _run(run_id=630, inmem="1", settings=_overlay(1.5), buy_on={FLAT[0][0]},
               regime_calendar=type("_Calm", (), {"at": lambda self, _d: False})())
    [(status, qty, filled, _txn)] = out["entries"]
    assert (status, qty, filled) == ("filled", BASE_CAP_SHARES, BASE_CAP_SHARES)


# --------------------------------------------------------------------------- #
# (b) a refused submit is cancelled, never filled -- identically in both stores
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
@pytest.mark.parametrize("entry_sl_pct", [-20.0, None], ids=["txn-before-submit", "bare-ruleset"])
def test_a_refused_entry_is_cancelled_and_never_filled(monkeypatch, inmem, entry_sl_pct):
    _store_mode(monkeypatch, inmem)
    out = _run(run_id=640 + (100 if inmem == "0" else 0) + (0 if entry_sl_pct else 5), inmem=inmem,
               settings=NOTIONAL, buy_on={FLAT[0][0]}, refuse_on={FLAT[0][0]},
               entry_sl_pct=entry_sl_pct)
    assert out["refusals"], "the fixture must actually refuse the entry"
    assert out["refused_entries"] == [("AAPL", True)], "the refusal must be recorded for the results"
    [(status, _qty, filled, txn_status)] = out["entries"]
    assert status == "canceled", f"a refused entry must be cancelled, not left {status}"
    assert not filled
    # The backtest account creates the transaction only once validation passed; whatever exists
    # behind a refused entry must be terminal, never WAITING/OPENED/CLOSING.
    assert txn_status == ("FAILED" if entry_sl_pct else None)
    assert out["trades"] == [], "a refused entry was filled"
    assert "CLOSING" not in out["transactions"] and "WAITING" not in out["transactions"]
    assert not any(o[2] == "filled" for o in out["orders"])


@pytest.mark.parametrize("inmem", ["1", "0"], ids=["inmem-store", "sqlite"])
def test_a_refused_entry_does_not_block_the_next_one(monkeypatch, inmem):
    """The FAILED transaction releases the symbol: the next day's signal enters normally."""
    _store_mode(monkeypatch, inmem)
    out = _run(run_id=650 + (100 if inmem == "0" else 0), inmem=inmem, settings=NOTIONAL,
               buy_on={FLAT[0][0], FLAT[1][0]}, refuse_on={FLAT[0][0]})
    statuses = [s for (s, _q, _f, _t) in out["entries"]]
    assert statuses == ["canceled", "filled"]


def test_both_trade_stores_produce_the_same_trades_after_a_refusal(monkeypatch):
    runs = {}
    for inmem, run_id in (("1", 660), ("0", 760)):
        _store_mode(monkeypatch, inmem)
        runs[inmem] = _run(run_id=run_id, inmem=inmem, settings=NOTIONAL,
                           buy_on={FLAT[0][0], FLAT[2][0]}, refuse_on={FLAT[0][0]})
    assert runs["1"]["entries"] == runs["0"]["entries"]
    assert runs["1"]["orders"] == runs["0"]["orders"]
    assert runs["1"]["trades"] == runs["0"]["trades"]
    assert runs["1"]["equity"] == runs["0"]["equity"]
