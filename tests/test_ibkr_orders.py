"""IBKRAccount order flow against FakeIB: submission, TIF, ticks, fractional, shorts, rejects,
cancel/modify, refresh, partial fills, OCO and the protective-leg template. No network."""
from datetime import datetime, timedelta, timezone

import pytest
from ib_async import Order, PriceIncrement, Stock

from ba2_common.core.ibkr_mapping import make_order_ref
from ba2_trade_platform.core.db import add_instance, get_instance, update_instance
from ba2_trade_platform.core.models import TradingOrder, Transaction
from ba2_trade_platform.core.types import (
    OrderDirection, OrderOpenType, OrderStatus, OrderType, TransactionStatus)
from tests.factories import create_transaction
from tests.ibkr_fakes import NAN
from tests.ibkr_helpers import ACCOUNT_ID, ibkr_logs, make_account  # noqa: F401


@pytest.fixture
def world(monkeypatch):
    account, fake = make_account(monkeypatch)
    aapl = fake.add_stock("AAPL", 265598)
    fake.add_stock("MSFT", 272093)
    yield account, fake, aapl
    account.close()


def new_order(account, symbol="AAPL", qty=10.0, side=OrderDirection.BUY, order_type=OrderType.MARKET,
              limit=None, stop=None, good_for=None, transaction_id=None, status=OrderStatus.PENDING,
              **kw):
    row = TradingOrder(account_id=account.id, symbol=symbol, quantity=qty, side=side,
                       order_type=order_type, limit_price=limit, stop_price=stop, good_for=good_for,
                       transaction_id=transaction_id, status=status, **kw)
    return get_instance(TradingOrder, add_instance(row))


def submit(account, row, **kw):
    """Straight to the broker leg (skips the template's validation and transactions)."""
    return account._submit_order_impl(row, **kw)


def ref(account, row):
    """The orderRef the adapter put on this row's IB order (it carries the row's nonce)."""
    return account._order_ref_for_row(get_instance(TradingOrder, row.id), account.id)


def last_placed(fake):
    return fake.placed[-1]


# ======================================================================= placement
class TestPlacement:
    def test_market_buy_is_day_and_carries_our_ref(self, world):
        account, fake, _ = world
        row = new_order(account)
        out = submit(account, row)
        p = last_placed(fake)
        assert (p["orderType"], p["action"], p["qty"], p["tif"]) == ("MKT", "BUY", 10.0, "DAY")
        assert p["ref"] == ref(account, row) and p["account"] == ACCOUNT_ID
        assert p["ref"].startswith(f"ba2:{account.id}:{row.id}:")
        assert p["outsideRth"] is False and p["transmit"] is True
        assert out.status == OrderStatus.ACCEPTED
        assert out.broker_order_id.isdigit() and int(out.broker_order_id) >= 1_000_000_000
        assert out.good_for == "day"

    def test_market_order_is_never_gtc_even_when_the_row_says_so(self, world, ibkr_logs):
        account, fake, _ = world
        submit(account, new_order(account, good_for="gtc"))
        assert last_placed(fake)["tif"] == "DAY"
        assert "never rests" in ibkr_logs.text()

    def test_limit_defaults_to_gtc_and_goes_out_as_lmt(self, world):
        account, fake, _ = world
        submit(account, new_order(account, order_type=OrderType.BUY_LIMIT, limit=150.0))
        p = last_placed(fake)
        assert (p["orderType"], p["tif"], p["lmt"]) == ("LMT", "GTC", 150.0)

    def test_day_limit_stays_day(self, world):
        account, fake, _ = world
        submit(account, new_order(account, order_type=OrderType.SELL_LIMIT, limit=150.0,
                                  side=OrderDirection.SELL, good_for="day"))
        assert last_placed(fake)["tif"] == "DAY"

    def test_stop_and_stop_limit_price_fields(self, world):
        account, fake, _ = world
        submit(account, new_order(account, side=OrderDirection.SELL, order_type=OrderType.SELL_STOP,
                                  stop=140.0))
        p = last_placed(fake)
        assert (p["orderType"], p["aux"], p["tif"]) == ("STP", 140.0, "GTC")
        submit(account, new_order(account, side=OrderDirection.SELL,
                                  order_type=OrderType.SELL_STOP_LIMIT, stop=140.0, limit=139.0))
        p = last_placed(fake)
        assert (p["orderType"], p["aux"], p["lmt"]) == ("STP LMT", 140.0, 139.0)

    def test_prices_are_rounded_to_the_contracts_market_rule(self, world):
        account, fake, _ = world
        fake.market_rules[27] = [PriceIncrement(0.0, 0.0001), PriceIncrement(1.0, 0.01)]
        fake.add_stock("PENNY", 77, rule="27")
        submit(account, new_order(account, symbol="PENNY", order_type=OrderType.BUY_LIMIT,
                                  limit=0.12347))
        assert last_placed(fake)["lmt"] == pytest.approx(0.1235)
        submit(account, new_order(account, order_type=OrderType.BUY_LIMIT, limit=150.126))
        assert last_placed(fake)["lmt"] == pytest.approx(150.13)

    def test_unrecognised_time_in_force_is_an_error_not_a_default(self, world):
        account, fake, _ = world
        row = new_order(account, order_type=OrderType.BUY_LIMIT, limit=150.0, good_for="whenever")
        assert submit(account, row) is None
        fresh = get_instance(TradingOrder, row.id)
        assert fresh.status == OrderStatus.ERROR and "whenever" in fresh.comment
        assert fake.placed == []

    def test_missing_prices_are_refused_before_the_broker_is_called(self, world):
        account, fake, _ = world
        row = new_order(account, order_type=OrderType.BUY_LIMIT)
        assert submit(account, row) is None
        assert get_instance(TradingOrder, row.id).status == OrderStatus.ERROR and fake.placed == []

    def test_already_sent_is_never_resent(self, world):
        account, fake, _ = world
        row = new_order(account, broker_order_id="123456789")
        assert submit(account, row).broker_order_id == "123456789"
        assert fake.placed == []

    def test_unknown_symbol_marks_the_row_invalid_symbol(self, world):
        account, fake, _ = world
        row = new_order(account, symbol="NOPE")
        assert submit(account, row) is None
        fresh = get_instance(TradingOrder, row.id)
        assert fresh.status == OrderStatus.ERROR and "[invalid_symbol]" in fresh.comment

    def test_complex_order_request_is_refused_loudly(self, world):
        account, *_ = world
        with pytest.raises(NotImplementedError):
            submit(account, new_order(account), use_complex_order=True)

    def test_no_wash_trade_lock(self, world):
        account, *_ = world
        assert account._is_washtrade_lock_candidate(new_order(account)) is False

    def test_option_rows_do_not_go_through_the_equity_path(self, world):
        account, fake, _ = world
        from ba2_trade_platform.core.types import AssetClass
        row = new_order(account, asset_class=AssetClass.OPTION)
        assert submit(account, row) is None and fake.placed == []


# ======================================================================= rejects / errors
class TestRejectsAndAcks:
    def test_margin_rejection_is_classified_and_kept_verbatim(self, world):
        account, fake, _ = world
        fake.behaviors.append(("reject", 201, "Order rejected - reason:Insufficient margin"))
        row = new_order(account)
        assert submit(account, row) is None
        fresh = get_instance(TradingOrder, row.id)
        assert fresh.status == OrderStatus.ERROR
        assert "[insufficient_funds]" in fresh.comment and "Insufficient margin" in fresh.comment
        assert fresh.broker_order_id is None

    def test_short_locate_rejection_is_insufficient_qty(self, world):
        account, fake, _ = world
        fake.behaviors.append(("reject", 201, "Order rejected - no shares available to short"))
        row = new_order(account)
        submit(account, row)
        assert "[insufficient_qty]" in get_instance(TradingOrder, row.id).comment

    def test_unrecognised_rejection_keeps_the_brokers_words(self, world):
        account, fake, _ = world
        fake.behaviors.append(("reject", 110, "The price does not conform to the minimum price variation"))
        row = new_order(account)
        submit(account, row)
        c = get_instance(TradingOrder, row.id).comment
        assert "[unknown]" in c and "minimum price variation" in c

    def test_no_ack_in_time_is_recorded_pending_and_never_resent(self, world, monkeypatch):
        account, fake, _ = world
        from ba2_trade_platform.modules.accounts.IBKRAccount import IBKRAccount
        monkeypatch.setattr(IBKRAccount, "_ORDER_ACK_TIMEOUT", 0.2)
        fake.behaviors.append("silent")
        row = new_order(account)
        out = submit(account, row)
        assert out.status == OrderStatus.PENDING_NEW
        assert out.broker_order_id.startswith("o")
        assert len(fake.placed) == 1
        # IB answers later; the next refresh upgrades the id to the permId and the status
        fake.simulate_status(ref(account, row), "Submitted")
        trade = fake.trade_by_ref(ref(account, row))
        trade.orderStatus.permId = 1_234_567_890
        assert account.refresh_orders() is True
        fresh = get_instance(TradingOrder, row.id)
        assert fresh.broker_order_id == "1234567890" and fresh.status == OrderStatus.ACCEPTED
        assert len(fake.placed) == 1

    def test_read_only_account_refuses_loudly_before_anything_is_written(self, monkeypatch):
        from ba2_trade_platform.modules.accounts.ibkr_runtime import IBKRReadOnlyError
        account, fake = make_account(monkeypatch, read_only=True)
        fake.add_stock("AAPL", 1)
        try:
            row = new_order(account)
            with pytest.raises(IBKRReadOnlyError, match="read-only"):
                submit(account, row)
            assert get_instance(TradingOrder, row.id).status == OrderStatus.PENDING
            assert fake.placed == []
            assert account.supports_trading is False      # TradeManager refuses to route to it
        finally:
            account.close()

    def test_gateway_down_marks_the_order_error_never_queues_it(self, world):
        account, fake, _ = world
        fake.connect_failure = ConnectionRefusedError("refused")
        row = new_order(account)
        assert submit(account, row) is None
        fresh = get_instance(TradingOrder, row.id)
        assert fresh.status == OrderStatus.ERROR and "cannot connect to IBKR" in fresh.comment
        assert fake.placed == []


# ======================================================================= fractional
class TestFractional:
    def test_ineligible_symbol_is_floored_to_whole_shares(self, world):
        account, fake, _ = world
        submit(account, new_order(account, qty=3.7))
        assert last_placed(fake)["qty"] == 3.0

    def test_floor_to_zero_is_a_skip_not_an_error(self, world):
        account, fake, _ = world
        row = new_order(account, qty=0.4)
        assert submit(account, row) is None
        fresh = get_instance(TradingOrder, row.id)
        assert fresh.status == OrderStatus.CANCELED and "skipped" in fresh.comment
        assert fake.placed == []

    def test_eligible_symbol_keeps_its_fraction_on_the_brokers_grid_rounded_down(self, world):
        account, fake, _ = world
        fake.add_stock("SCHD", 5, fractional=True)
        submit(account, new_order(account, symbol="SCHD", qty=2.123456))
        assert last_placed(fake)["qty"] == pytest.approx(2.1234)
        assert last_placed(fake)["tif"] == "DAY"

    def test_fractional_limit_is_forced_day(self, world):
        account, fake, _ = world
        fake.add_stock("SCHD", 5, fractional=True)
        submit(account, new_order(account, symbol="SCHD", qty=1.5, order_type=OrderType.BUY_LIMIT,
                                  limit=30.0, good_for="gtc"))
        assert last_placed(fake)["tif"] == "DAY"

    def test_fractional_stop_is_refused(self, world):
        account, fake, _ = world
        fake.add_stock("SCHD", 5, fractional=True)
        row = new_order(account, symbol="SCHD", qty=1.5, side=OrderDirection.SELL,
                        order_type=OrderType.SELL_STOP, stop=30.0)
        assert submit(account, row) is None
        assert "fractional" in get_instance(TradingOrder, row.id).comment and fake.placed == []


# ======================================================================= shorts
class TestShorting:
    def sell_txn(self, side=OrderDirection.SELL):
        return create_transaction(symbol="AAPL", side=side, status=TransactionStatus.WAITING)

    def test_short_open_needs_shortable_and_easy_to_borrow(self, world):
        account, fake, aapl = world
        fake.shortable_shares[aapl.conId] = 5.0
        txn = self.sell_txn()
        submit(account, new_order(account, side=OrderDirection.SELL, transaction_id=txn.id))
        assert last_placed(fake)["action"] == "SELL"

    def test_hard_to_borrow_is_refused(self, world):
        account, fake, aapl = world
        fake.shortable_shares[aapl.conId] = 1.0
        txn = self.sell_txn()
        row = new_order(account, side=OrderDirection.SELL, transaction_id=txn.id)
        assert submit(account, row) is None
        assert "SHORT" in get_instance(TradingOrder, row.id).comment and fake.placed == []

    def test_unknown_shortability_is_refused_not_assumed(self, world):
        account, fake, aapl = world
        txn = self.sell_txn()
        row = new_order(account, side=OrderDirection.SELL, transaction_id=txn.id)
        assert submit(account, row) is None
        assert "unknown" in get_instance(TradingOrder, row.id).comment

    def test_a_longs_protective_sell_is_never_gated(self, world):
        account, fake, _ = world
        txn = self.sell_txn(side=OrderDirection.BUY)
        submit(account, new_order(account, side=OrderDirection.SELL, transaction_id=txn.id))
        assert len(fake.placed) == 1

    def test_closing_orders_are_never_gated(self, world):
        account, fake, _ = world
        txn = self.sell_txn()
        submit(account, new_order(account, side=OrderDirection.SELL, transaction_id=txn.id),
               is_closing_order=True)
        assert len(fake.placed) == 1
