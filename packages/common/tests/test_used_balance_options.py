"""Regression: MarketExpertInterface._calculate_used_balance mispriced OPTION transactions.

Two compounding bugs, both traced from a live 2.25-year backtest where the entry equity-gate
blocked nearly every candidate entry after just one or two option positions opened:

1. It read the bulk-fetched STOCK price for transaction.symbol (the underlying ticker for
   options -- see submit_option_order) as if it were the option's own current premium.
2. It never multiplied by the contract multiplier (100 for standard options), so even a
   correctly-priced premium would still be sized ~100x too small.

Confirmed live: a single META call transaction reported Used=$24,868.50 (open_price=$497.37,
the underlying's stock price, times a handful of contracts) against a ~$23,744 account -- one
position alone exceeded the WHOLE account's virtual balance, tripping the equity-gate for the
rest of the backtest. Fixed: option transactions are now priced via get_option_quote (long at
bid, short at ask, last fallback) and multiplied by transaction.multiplier throughout.
"""
from types import SimpleNamespace

import pytest

from ba2_common.core import trade_store as ts
from ba2_common.core.db import add_instance
from ba2_common.core.interfaces import MarketExpertInterface
from ba2_common.core.models import Transaction, TradingOrder
from ba2_common.core.option_types import OptionQuote
from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus


class _StubExpert(MarketExpertInterface):
    def __init__(self, expert_id):
        self.id = expert_id

    @classmethod
    def description(cls):
        return "stub"

    def render_market_analysis(self, ma):
        return ""

    def run_analysis(self, symbol, market_analysis):
        return None


class _StubAccount:
    def __init__(self, stock_price, quote=None):
        self._stock_price = stock_price
        self._quote = quote

    def get_instrument_current_price(self, symbols):
        return {s: self._stock_price for s in symbols}

    def get_option_quote(self, contract_symbol):
        return self._quote


def _option_txn(expert_id, open_price, quantity, multiplier=100.0, side=OrderDirection.BUY):
    txn = Transaction(symbol="META", quantity=quantity, side=side, status=TransactionStatus.OPENED,
                       expert_id=expert_id, open_price=open_price, multiplier=multiplier)
    txn_id = add_instance(txn)
    add_instance(TradingOrder(
        account_id=1, symbol="META240517C00517500", quantity=quantity, side=side,
        order_type=OrderType.MARKET, status=OrderStatus.FILLED, transaction_id=txn_id,
        asset_class=AssetClass.OPTION, contract_symbol="META240517C00517500", multiplier=100,
    ))
    return txn_id


def _equity_txn(expert_id, open_price, quantity, side=OrderDirection.BUY):
    txn = Transaction(symbol="AAPL", quantity=quantity, side=side, status=TransactionStatus.OPENED,
                       expert_id=expert_id, open_price=open_price, multiplier=None)
    return add_instance(txn)


class TestOptionUsedBalance:
    def test_prices_off_option_quote_not_underlying_stock_price(self):
        """The core regression: a $563 underlying stock price must never leak into the
        used-balance math for an option position whose real premium is a few dollars."""
        with ts.inmem_trades():
            quote = OptionQuote(symbol="X", bid=5.00, ask=5.20)
            account = _StubAccount(stock_price=563.29, quote=quote)
            expert = _StubExpert(expert_id=1)
            _option_txn(expert_id=1, open_price=4.90, quantity=3, multiplier=100.0)

            used = expert._calculate_used_balance(account)

            # Long BUY marks current at bid=5.00. transaction_used = open_price * qty *
            # multiplier (profitable side, since 5.00 > 4.90) = 4.90 * 3 * 100 = 1470.
            assert used == pytest.approx(1470.0)
            # The old bug would have produced ~563.29 * 3 = 1689.87 (no multiplier, wrong
            # price) or, worse, orders of magnitude more with a multi-contract position.
            assert used < 563.29 * 3 * 100  # sanity: nowhere near stock-price-based sizing

    def test_applies_the_contract_multiplier(self):
        """Without the multiplier fix, a correctly-priced $4.90 premium would still size
        the position ~100x too small."""
        with ts.inmem_trades():
            quote = OptionQuote(symbol="X", bid=4.90, ask=5.10)
            account = _StubAccount(stock_price=563.29, quote=quote)
            expert = _StubExpert(expert_id=1)
            _option_txn(expert_id=1, open_price=4.90, quantity=2, multiplier=100.0)

            used = expert._calculate_used_balance(account)
            assert used == pytest.approx(980.0)  # 4.90 * 2 * 100, not 4.90 * 2 = 9.80

    def test_short_option_marks_at_ask(self):
        with ts.inmem_trades():
            quote = OptionQuote(symbol="X", bid=2.00, ask=2.30)
            account = _StubAccount(stock_price=563.29, quote=quote)
            expert = _StubExpert(expert_id=1)
            _option_txn(expert_id=1, open_price=2.10, quantity=1, multiplier=100.0,
                       side=OrderDirection.SELL)

            used = expert._calculate_used_balance(account)
            # SELL: profit_loss = (open_price - current) * qty * mult = (2.10-2.30)*1*100 = -20
            # (losing) -> transaction_used = open_price*qty*mult + loss = 210 + 20 = 230.
            assert used == pytest.approx(230.0)

    def test_falls_back_to_open_price_when_quote_unavailable(self):
        with ts.inmem_trades():
            account = _StubAccount(stock_price=563.29, quote=None)
            expert = _StubExpert(expert_id=1)
            _option_txn(expert_id=1, open_price=4.90, quantity=1, multiplier=100.0)

            used = expert._calculate_used_balance(account)
            # current_price falls back to open_price -> flat P&L -> used = 4.90*1*100 = 490.
            assert used == pytest.approx(490.0)


class TestEquityUsedBalanceUnaffected:
    def test_equity_transaction_still_uses_bulk_stock_price_no_multiplier(self):
        with ts.inmem_trades():
            account = _StubAccount(stock_price=150.0)
            expert = _StubExpert(expert_id=1)
            _equity_txn(expert_id=1, open_price=140.0, quantity=10)

            used = expert._calculate_used_balance(account)
            # Profitable (150 > 140) -> used = open_price * qty = 140 * 10 = 1400 (multiplier
            # defaults to 1.0 for equities -- transaction.multiplier is None/falsy).
            assert used == pytest.approx(1400.0)
