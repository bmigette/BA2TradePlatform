"""OPT-S7 and OPT-B3 — the two things ``_fill_multi_leg_parent`` never read.

OPT-S7 — the combo's NET LIMIT is never enforced.
``submit_option_order`` puts the net limit on the PARENT and builds the children with no
``limit_price`` of their own, so ``_option_fill_price``'s two limit branches cannot fire
for a child and every leg falls through to the market-style ``_option_slip``.
``_fill_multi_leg_parent`` then prices each leg off its own bar, applies only the debit
cash cap, and writes ``parent.open_price = net`` with no comparison against
``parent.limit_price`` — a grep confirmed the field was never read in the multi-leg path.
Live Alpaca cannot fill a combo through its net limit. Single-leg option orders already
enforce theirs (``test_option_limit_spread_cost.py``), so the gap was specific to the 12
multi-leg structures the GA searches. The fills it invented are the ones WORSE than the
limit, so mean credit was understated while the TRADE COUNT was inflated — it changed
which trades exist, the worst distortion class for a fitness function.

OPT-B3 — the debit cash guard cannot tell an open from a close.
``_fill_multi_leg_parent`` never read ``position_intent``, though its single-leg sibling
``_cap_single_leg_option_entry`` explicitly does (``if intent and "open" not in intent:
return True``). Closing a credit structure is a net DEBIT, so on a cash trough the close
was either cancelled outright ("entry NOT opened") or silently rescaled — and the rescale
called ``_sync_transaction_quantity``, overwriting ``Transaction.quantity`` with the number
of structures CLOSED. Nothing repairs that value, and it is the divisor for
``spread_pnl_percent`` take-profits. The partial branch is worse still: after a 2-of-3 cap
the close parent is FILLED, ``has_pending_closing_order`` stops blocking, and the next close
attempt reads the ENTRY parent's ``filled_qty=3`` and over-closes, flipping the position.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from ba2_common.core.types import (
    OptionRight, OrderDirection, OrderStatus, TransactionStatus,
)


CFG = {
    "starting_cash": 100_000.0,
    "commission_per_trade": 0.0,   # exact net arithmetic
    "slippage_bps": 0.0,
    "fill_model": "next_bar_open",
    # option_spread_pct is left unset (0) so a leg fills at its bar price exactly.
}

_C180 = "AAPL240315C00180000"
_C190 = "AAPL240315C00190000"
_P180 = "AAPL240315P00180000"
_P170 = "AAPL240315P00170000"
_EXPIRY = date(2024, 3, 15)

_D_SUBMIT = datetime(2024, 3, 5)
_D_ENTRY_FILL = datetime(2024, 3, 6)
_D_CLOSE_SUBMIT = datetime(2024, 3, 7)
_D_CLOSE_FILL = datetime(2024, 3, 8)

_AAPL_BARS = [
    {"Date": _D_SUBMIT, "Open": 180, "High": 182, "Low": 178, "Close": 181, "Volume": 1000},
    {"Date": _D_ENTRY_FILL, "Open": 181, "High": 183, "Low": 180, "Close": 182, "Volume": 1000},
    {"Date": _D_CLOSE_SUBMIT, "Open": 178, "High": 180, "Low": 175, "Close": 176, "Volume": 1000},
    {"Date": _D_CLOSE_FILL, "Open": 172, "High": 174, "Low": 170, "Close": 173, "Volume": 1000},
]

# Premium bars, keyed (occ, day) -> open. Every bar is flat (open == close) so the
# fill model cannot matter, and liquid (volume 500 vs orders of <= 3 contracts).
_PREMIUMS = {
    # entry day
    (_C180, "2024-03-06"): 5.00,
    (_C190, "2024-03-06"): 2.00,     # bull call spread net debit 3.00
    (_P180, "2024-03-06"): 5.00,
    (_P170, "2024-03-06"): 2.00,     # bull put spread net credit 3.00
    # close day — the underlying fell, so buying the 180 put back is expensive
    (_P180, "2024-03-08"): 9.00,
    (_P170, "2024-03-08"): 3.00,     # closing net DEBIT 6.00 per structure
}

_TERMS = {
    _C180: ("call", 180.0),
    _C190: ("call", 190.0),
    _P180: ("put", 180.0),
    _P170: ("put", 170.0),
}


def _seed_cache(db_path: str) -> None:
    from app.services.backtest.options_cache import OptionsHistoryCache

    cache = OptionsHistoryCache(db_path)
    cache.write_chain_rows(
        "AAPL", "2024-03-01",
        [{"occ_symbol": occ, "option_type": kind, "strike": strike,
          "expiry": "2024-03-15", "bid": 1.0, "ask": 1.2, "last": 1.1, "iv": 0.25}
         for occ, (kind, strike) in _TERMS.items()],
    )
    cache.write_bar_rows([
        {"occ_symbol": occ, "date": day, "open": px, "high": px, "low": px, "close": px,
         "volume": 500, "underlying": "AAPL", "option_type": _TERMS[occ][0],
         "strike": _TERMS[occ][1], "expiry": "2024-03-15"}
        for (occ, day), px in _PREMIUMS.items()
    ])


def _acct(tmp_path, account_id):
    from app.services.backtest.backtest_db import backtest_trading_db, seed_account_definition
    from app.services.backtest.seam_wiring import wire_backtest_seams
    from app.services.backtest.backtest_account import BacktestAccount
    from app.services.backtest.options_provider import HistoricalOptionsProvider
    from app.services.backtest.price_source import AsOfPriceSource

    cache_db = str(tmp_path / "options_cache.sqlite")
    _seed_cache(cache_db)
    wire_backtest_seams()
    ctx = backtest_trading_db(f"mleg-{account_id}")
    ctx.__enter__()
    seed_account_definition(account_id, CFG)
    ps = AsOfPriceSource(ohlcv_provider=None)
    ps.load_bars("AAPL", _AAPL_BARS)
    ps.set_clock(_D_SUBMIT)
    acct = BacktestAccount(account_id, ps, CFG,
                           options_provider=HistoricalOptionsProvider(cache_db))
    wire_backtest_seams().register_account(account_id, acct)
    return acct, ps, ctx


def _leg(occ, side, intent):
    from ba2_common.core.option_types import OptionLeg

    kind, strike = _TERMS[occ]
    return OptionLeg(
        contract_symbol=occ, side=side, position_intent=intent,
        option_type=OptionRight.CALL if kind == "call" else OptionRight.PUT,
        strike=strike, expiry=_EXPIRY, underlying="AAPL")


def _debit_spread_legs():
    """Bull call spread: BUY 180C / SELL 190C. Fills to a net debit of 3.00."""
    return [_leg(_C180, OrderDirection.BUY, "buy_to_open"),
            _leg(_C190, OrderDirection.SELL, "sell_to_open")]


def _credit_spread_legs():
    """Bull put spread: SELL 180P / BUY 170P. Fills to a net credit of 3.00."""
    return [_leg(_P180, OrderDirection.SELL, "sell_to_open"),
            _leg(_P170, OrderDirection.BUY, "buy_to_open")]


def _parent(acct):
    parents = [o for o in acct.get_orders()
               if o.parent_order_id is None and not o.contract_symbol
               and o.option_strategy]
    assert len(parents) >= 1
    return parents[-1]


def _parents(acct):
    return [o for o in acct.get_orders()
            if o.parent_order_id is None and not o.contract_symbol and o.option_strategy]


# =========================================================================== #
# OPT-S7 — the net limit
# =========================================================================== #
def test_a_debit_combo_does_not_fill_through_its_net_limit(tmp_path):
    """Legs price to a 3.00 net debit; the order said it would pay at most 2.00."""
    acct, ps, ctx = _acct(tmp_path, 61)
    try:
        acct.submit_option_order(legs=_debit_spread_legs(), quantity=1,
                                 order_type="limit", limit_price=2.00,
                                 option_strategy="bull_call_spread")
        acct.refresh_orders()
        acct.refresh_transactions()

        assert acct.get_option_positions() == [], (
            "the combo filled at a 3.00 net debit against a 2.00 net limit — live Alpaca "
            "cannot do that, and the fills the simulator invents this way are exactly the "
            "ones WORSE than the limit (OPT-S7)."
        )
        parent = _parent(acct)
        assert parent.status not in (OrderStatus.FILLED,)
        assert acct._cash == pytest.approx(CFG["starting_cash"])
    finally:
        ctx.__exit__(None, None, None)


def test_a_debit_combo_fills_when_the_net_clears_its_limit(tmp_path):
    """The mirror: at a 3.00 limit the same legs fill, at the achieved net."""
    acct, ps, ctx = _acct(tmp_path, 62)
    try:
        acct.submit_option_order(legs=_debit_spread_legs(), quantity=1,
                                 order_type="limit", limit_price=3.00,
                                 option_strategy="bull_call_spread")
        acct.refresh_orders()
        acct.refresh_transactions()

        assert len(acct.get_option_positions()) == 2
        parent = _parent(acct)
        assert parent.status == OrderStatus.FILLED
        assert float(parent.open_price) == pytest.approx(3.00)
    finally:
        ctx.__exit__(None, None, None)


def test_a_combo_that_beats_its_limit_still_fills_at_the_better_net(tmp_path):
    """A limit is a worst case, not a target: 3.00 achieved against a 3.50 limit fills."""
    acct, ps, ctx = _acct(tmp_path, 63)
    try:
        acct.submit_option_order(legs=_debit_spread_legs(), quantity=1,
                                 order_type="limit", limit_price=3.50,
                                 option_strategy="bull_call_spread")
        acct.refresh_orders()
        acct.refresh_transactions()

        parent = _parent(acct)
        assert parent.status == OrderStatus.FILLED
        assert float(parent.open_price) == pytest.approx(3.00)
    finally:
        ctx.__exit__(None, None, None)


def test_a_credit_combo_does_not_fill_below_its_net_credit_limit(tmp_path):
    """Legs price to a 3.00 credit; the order demanded at least 4.00 (limit -4.00).

    The credit side is the one the GA lives on, and the sign convention (negative limit
    == credit) is where a one-sided comparison silently inverts.
    """
    acct, ps, ctx = _acct(tmp_path, 64)
    try:
        acct.submit_option_order(legs=_credit_spread_legs(), quantity=1,
                                 order_type="limit", limit_price=-4.00,
                                 option_strategy="bull_put_spread")
        acct.refresh_orders()
        acct.refresh_transactions()

        assert acct.get_option_positions() == [], (
            "a 3.00 credit filled an order that demanded 4.00 — the simulator collected a "
            "premium the market never offered (OPT-S7)."
        )
        assert acct._cash == pytest.approx(CFG["starting_cash"])
    finally:
        ctx.__exit__(None, None, None)


def test_a_credit_combo_fills_when_the_credit_meets_its_limit(tmp_path):
    """At a -2.00 limit the 3.00 credit is more than good enough."""
    acct, ps, ctx = _acct(tmp_path, 65)
    try:
        acct.submit_option_order(legs=_credit_spread_legs(), quantity=1,
                                 order_type="limit", limit_price=-2.00,
                                 option_strategy="bull_put_spread")
        acct.refresh_orders()
        acct.refresh_transactions()

        assert len(acct.get_option_positions()) == 2
        parent = _parent(acct)
        assert parent.status == OrderStatus.FILLED
        assert float(parent.open_price) == pytest.approx(-3.00)
        assert acct._cash == pytest.approx(CFG["starting_cash"] + 300.0)
    finally:
        ctx.__exit__(None, None, None)


def test_a_market_combo_is_unaffected_by_the_limit_check(tmp_path):
    """No limit price, nothing to enforce — a market combo must still fill."""
    acct, ps, ctx = _acct(tmp_path, 66)
    try:
        acct.submit_option_order(legs=_debit_spread_legs(), quantity=1,
                                 order_type="market",
                                 option_strategy="bull_call_spread")
        acct.refresh_orders()
        acct.refresh_transactions()

        assert len(acct.get_option_positions()) == 2
        assert float(_parent(acct).open_price) == pytest.approx(3.00)
    finally:
        ctx.__exit__(None, None, None)


# =========================================================================== #
# OPT-B3 — the cash guard vs. a close
# =========================================================================== #
def _open_credit_spread(acct, ps, quantity):
    acct.submit_option_order(legs=_credit_spread_legs(), quantity=quantity,
                             order_type="limit", limit_price=-3.00,
                             option_strategy="bull_put_spread")
    acct.refresh_orders()
    acct.refresh_transactions()
    assert len(acct.get_option_positions()) == 2
    entry = _parent(acct)
    return entry


def _submit_close(acct, ps, quantity, txn_id):
    """Reverse the credit spread: BUY back the 180P, SELL the 170P. Net debit 6.00."""
    ps.set_clock(_D_CLOSE_SUBMIT)
    legs = [_leg(_P180, OrderDirection.BUY, "buy_to_close"),
            _leg(_P170, OrderDirection.SELL, "sell_to_close")]
    acct.submit_option_order(legs=legs, quantity=quantity, order_type="limit",
                             limit_price=6.00, option_strategy="close",
                             transaction_id=txn_id)
    acct.refresh_orders()
    acct.refresh_transactions()


def test_a_close_is_not_cancelled_for_want_of_cash(tmp_path):
    """Closing a credit structure is a net DEBIT. The cash guard used to refuse it.

    A refused close leaves the short leg open with its assignment risk intact — the
    account cannot get flat by running out of money, and the broker would not stop it.
    """
    acct, ps, ctx = _acct(tmp_path, 71)
    try:
        entry = _open_credit_spread(acct, ps, quantity=1)
        acct._cash = 300.0                      # a post-assignment cash trough
        _submit_close(acct, ps, 1, entry.transaction_id)

        closes = [p for p in _parents(acct) if p.option_strategy == "close"]
        assert len(closes) == 1
        assert closes[0].status == OrderStatus.FILLED, (
            f"the close was {closes[0].status} — the debit cash guard cancelled a CLOSE "
            f"because it never read position_intent (OPT-B3). Its single-leg sibling "
            f"_cap_single_leg_option_entry has always exempted closes."
        )
        assert acct.get_option_positions() == []
        assert acct._cash == pytest.approx(300.0 - 600.0)   # the debit really was paid
    finally:
        ctx.__exit__(None, None, None)


def test_a_close_is_not_silently_rescaled_to_what_cash_affords(tmp_path):
    """3 structures held, cash for 2. Closing 2 of 3 is the worse outcome, not the safer.

    The parent goes FILLED, ``has_pending_closing_order`` stops blocking, and the next
    close attempt reads the ENTRY parent's ``filled_qty=3`` — over-closing and flipping
    the position by 2.
    """
    acct, ps, ctx = _acct(tmp_path, 72)
    try:
        entry = _open_credit_spread(acct, ps, quantity=3)
        acct._cash = 1_300.0                    # 2 structures' worth of the 600 debit
        _submit_close(acct, ps, 3, entry.transaction_id)

        closes = [p for p in _parents(acct) if p.option_strategy == "close"]
        assert len(closes) == 1
        assert float(closes[0].quantity) == pytest.approx(3.0), (
            f"the close was rescaled to {closes[0].quantity} of 3 structures"
        )
        assert acct.get_option_positions() == [], "all three structures must be flat"
    finally:
        ctx.__exit__(None, None, None)


def test_a_rescaled_close_does_not_rewrite_the_transaction_quantity(tmp_path):
    """``_sync_transaction_quantity`` on a close overwrites the POSITION size with the
    number of structures closed — and that value is the divisor for
    ``spread_pnl_percent`` take-profits. Nothing anywhere repairs it."""
    from ba2_common.core.db import get_instance
    from ba2_common.core.models import Transaction

    acct, ps, ctx = _acct(tmp_path, 73)
    try:
        entry = _open_credit_spread(acct, ps, quantity=3)
        txn_id = entry.transaction_id
        assert float(get_instance(Transaction, txn_id).quantity) == pytest.approx(3.0)

        acct._cash = 1_300.0
        _submit_close(acct, ps, 3, txn_id)

        assert float(get_instance(Transaction, txn_id).quantity) == pytest.approx(3.0), (
            "the close rewrote the transaction's structure count to what cash afforded"
        )
    finally:
        ctx.__exit__(None, None, None)


def test_the_cash_guard_still_refuses_a_debit_ENTRY(tmp_path):
    """The exemption is for closes only. An unaffordable OPEN must still be refused —
    otherwise B3's fix hands the GA an unlimited credit line on debit structures."""
    acct, ps, ctx = _acct(tmp_path, 74)
    try:
        acct._cash = 100.0                      # a 3.00 x 100 = $300 debit is unaffordable
        acct.submit_option_order(legs=_debit_spread_legs(), quantity=1,
                                 order_type="limit", limit_price=3.00,
                                 option_strategy="bull_call_spread")
        acct.refresh_orders()
        acct.refresh_transactions()

        assert acct.get_option_positions() == []
        assert _parent(acct).status == OrderStatus.CANCELED
        assert acct._cash == pytest.approx(100.0)
    finally:
        ctx.__exit__(None, None, None)
