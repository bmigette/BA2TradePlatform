"""Regression: Transaction.open_price for an OPTION order was seeded with the UNDERLYING's
stock price, not the option premium.

Root cause: AccountInterface._create_transaction_for_order called
get_instrument_current_price(trading_order.symbol) unconditionally, but
OptionsAccountInterface.submit_option_order sets an option order's `.symbol` to the
UNDERLYING ticker (`first.underlying or first.contract_symbol`), not the contract -- so that
call fetched the underlying's stock price (e.g. $497 for a META call whose real premium was a
few dollars). That corrupted value then fed MarketExpertInterface._calculate_used_balance
(open_price * quantity, no multiplier), overstating a single option position's "used" capital
by ~100x and tripping the entry equity-gate into rejecting nearly every other candidate for the
rest of a 2.25-year backtest.

Fixed via _estimate_transaction_open_price: options price off their own quote (mid of
bid/ask, else last/bid/ask) or, for a multi-leg parent with no single contract to quote, the
order's own limit_price (the net premium submit_option_order already computed).
"""
from types import SimpleNamespace

from ba2_common.core.interfaces.AccountInterface import AccountInterface
from ba2_common.core.option_types import OptionQuote
from ba2_common.core.types import AssetClass, OrderDirection, OrderType


class _StubAccount(AccountInterface):
    """Minimal concrete AccountInterface with a controllable get_option_quote -- only
    _estimate_transaction_open_price is under test."""
    def __init__(self, quote=None):
        self._quote = quote

    def get_option_quote(self, contract_symbol):
        return self._quote

    def get_instrument_current_price(self, symbol):
        return 563.29  # the underlying's stock price -- must NEVER be returned for an option

    # Unused abstracts -- never called by this test.
    def _get_instrument_current_price_impl(self, *a, **k): raise NotImplementedError
    def _submit_order_impl(self, *a, **k): raise NotImplementedError
    def adjust_sl(self, *a, **k): raise NotImplementedError
    def adjust_tp(self, *a, **k): raise NotImplementedError
    def adjust_tp_sl(self, *a, **k): raise NotImplementedError
    def cancel_order(self, *a, **k): raise NotImplementedError
    def get_account_info(self, *a, **k): raise NotImplementedError
    def get_balance(self, *a, **k): raise NotImplementedError
    def get_balance_history(self, *a, **k): raise NotImplementedError
    def get_dividends(self, *a, **k): raise NotImplementedError
    def get_filled_trades(self, *a, **k): raise NotImplementedError
    def get_order(self, *a, **k): raise NotImplementedError
    def get_orders(self, *a, **k): raise NotImplementedError
    def get_positions(self, *a, **k): raise NotImplementedError
    def modify_order(self, *a, **k): raise NotImplementedError
    def refresh_orders(self, *a, **k): raise NotImplementedError
    def refresh_positions(self, *a, **k): raise NotImplementedError
    def symbols_exist(self, *a, **k): raise NotImplementedError


def _bare_account(quote=None):
    acct = _StubAccount.__new__(_StubAccount)
    acct._quote = quote
    return acct


def _equity_order(symbol="AAPL"):
    return SimpleNamespace(symbol=symbol, asset_class=AssetClass.EQUITY,
                            contract_symbol=None, limit_price=None)


def _option_order(contract_symbol="META240517C00517500", limit_price=None):
    return SimpleNamespace(symbol="META", asset_class=AssetClass.OPTION,
                            contract_symbol=contract_symbol, limit_price=limit_price)


class TestEquityUnaffected:
    def test_equity_order_still_uses_instrument_current_price(self):
        acct = _bare_account()
        assert acct._estimate_transaction_open_price(_equity_order()) == 563.29


class TestOptionPricedOffItsOwnQuote:
    def test_uses_bid_ask_midpoint_when_both_present(self):
        quote = OptionQuote(symbol="X", bid=4.80, ask=5.20)
        acct = _bare_account(quote=quote)
        assert acct._estimate_transaction_open_price(_option_order()) == 5.00

    def test_falls_back_to_last_when_no_bid_or_ask(self):
        quote = OptionQuote(symbol="X", bid=None, ask=None, last=4.95)
        acct = _bare_account(quote=quote)
        assert acct._estimate_transaction_open_price(_option_order()) == 4.95

    def test_falls_back_to_bid_only_when_ask_missing(self):
        quote = OptionQuote(symbol="X", bid=4.80, ask=None, last=None)
        acct = _bare_account(quote=quote)
        assert acct._estimate_transaction_open_price(_option_order()) == 4.80

    def test_never_returns_the_underlying_stock_price(self):
        """The core regression: whatever the result, it must not be anywhere near the
        underlying's $563.29 stock price stubbed on get_instrument_current_price."""
        quote = OptionQuote(symbol="X", bid=4.80, ask=5.20)
        acct = _bare_account(quote=quote)
        result = acct._estimate_transaction_open_price(_option_order())
        assert result < 10.0


class TestMultiLegFallsBackToLimitPrice:
    def test_no_contract_symbol_uses_order_limit_price(self):
        acct = _bare_account(quote=None)
        order = _option_order(contract_symbol=None, limit_price=1.5)
        assert acct._estimate_transaction_open_price(order) == 1.5

    def test_negative_limit_price_credit_spread_returns_absolute_value(self):
        acct = _bare_account(quote=None)
        order = _option_order(contract_symbol=None, limit_price=-0.85)
        assert acct._estimate_transaction_open_price(order) == 0.85

    def test_no_limit_price_and_no_quote_returns_none(self):
        acct = _bare_account(quote=None)
        order = _option_order(contract_symbol=None, limit_price=None)
        assert acct._estimate_transaction_open_price(order) is None


class TestQuoteLookupFailure:
    def test_missing_quote_falls_back_to_limit_price(self):
        acct = _bare_account(quote=None)
        order = _option_order(limit_price=2.10)
        assert acct._estimate_transaction_open_price(order) == 2.10

    def test_account_without_get_option_quote_falls_back_to_limit_price(self):
        class _NoQuoteAccount(AccountInterface):
            def get_instrument_current_price(self, symbol):
                return 563.29
            def _get_instrument_current_price_impl(self, *a, **k): raise NotImplementedError
            def _submit_order_impl(self, *a, **k): raise NotImplementedError
            def adjust_sl(self, *a, **k): raise NotImplementedError
            def adjust_tp(self, *a, **k): raise NotImplementedError
            def adjust_tp_sl(self, *a, **k): raise NotImplementedError
            def cancel_order(self, *a, **k): raise NotImplementedError
            def get_account_info(self, *a, **k): raise NotImplementedError
            def get_balance(self, *a, **k): raise NotImplementedError
            def get_balance_history(self, *a, **k): raise NotImplementedError
            def get_dividends(self, *a, **k): raise NotImplementedError
            def get_filled_trades(self, *a, **k): raise NotImplementedError
            def get_order(self, *a, **k): raise NotImplementedError
            def get_orders(self, *a, **k): raise NotImplementedError
            def get_positions(self, *a, **k): raise NotImplementedError
            def modify_order(self, *a, **k): raise NotImplementedError
            def refresh_orders(self, *a, **k): raise NotImplementedError
            def refresh_positions(self, *a, **k): raise NotImplementedError
            def symbols_exist(self, *a, **k): raise NotImplementedError

        acct = _NoQuoteAccount.__new__(_NoQuoteAccount)
        order = _option_order(limit_price=3.30)
        assert acct._estimate_transaction_open_price(order) == 3.30
