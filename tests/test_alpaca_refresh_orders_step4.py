"""refresh_orders Step 4: an order missing from the broker LISTING is asked about by id.

REGRESSION: Step 4 called ``get_order``, which swallows every exception and returns None,
so a transport error or an HTTP 429 on the by-id lookup read exactly like "the broker does
not know this order" and a recent order (a live protective stop included) was marked
CANCELED locally. Only a CONFIRMED absence may do that; an unknown outcome must leave the
row untouched and say so at WARNING.

No live API call: the listing and the TradingClient are fakes.
"""
import importlib
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from alpaca.common.exceptions import APIError

from ba2_trade_platform.core.db import get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from tests.factories import create_account_definition, create_trading_order

# The accounts package re-exports the CLASS under the module's name; import the module.
alpaca_mod = importlib.import_module("ba2_trade_platform.modules.accounts.AlpacaAccount")
AlpacaAccount = alpaca_mod.AlpacaAccount


def _api_error(http_status, body):
    """An APIError shaped as alpaca-py's REST client raises it (body + HTTPError)."""
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=http_status), request=None)
    return APIError(body, http_error)


class _RawOrder:
    """The attributes refresh_orders and alpaca_order_to_tradingorder read."""

    def __init__(self, broker_id, status, filled_qty="0", filled_avg_price=None):
        self.id = broker_id
        self.client_order_id = None
        self.symbol = "NVDA"
        self.qty = "10"
        self.side = "sell"
        self.type = "stop"
        self.order_class = None
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.limit_price = None
        self.stop_price = 100.0
        self.created_at = datetime.now(timezone.utc)
        self.legs = None


class _Client:
    """TradingClient stand-in: get_order_by_id only."""

    def __init__(self, reply):
        self.reply = reply
        self.asked = []

    def get_order_by_id(self, order_id):
        self.asked.append(order_id)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class _RecordingLogger:
    """Replaces the module logger: ba2 loggers do not propagate, so caplog sees nothing."""

    def __init__(self):
        self.records = []

    def _log(self, level, msg, *args, **kwargs):
        self.records.append((level, str(msg)))

    def debug(self, msg, *a, **k):
        self._log(logging.DEBUG, msg)

    def info(self, msg, *a, **k):
        self._log(logging.INFO, msg)

    def warning(self, msg, *a, **k):
        self._log(logging.WARNING, msg)

    def error(self, msg, *a, **k):
        self._log(logging.ERROR, msg)

    def critical(self, msg, *a, **k):
        self._log(logging.CRITICAL, msg)

    def exception(self, msg, *a, **k):
        self._log(logging.ERROR, msg)

    def at(self, level):
        return [m for lvl, m in self.records if lvl == level]


@pytest.fixture
def log(monkeypatch):
    rec = _RecordingLogger()
    monkeypatch.setattr(alpaca_mod, "logger", rec)
    # alpaca_api_retry backs off on a 429 with real sleeps; skip them.
    monkeypatch.setattr(alpaca_mod.time, "sleep", lambda s: None)
    return rec


def _setup(monkeypatch, reply, age_hours=2.0, status=OrderStatus.NEW):
    """A working protective stop the broker LISTING does not mention, plus an account
    whose by-id lookup answers ``reply``."""
    acct_def = create_account_definition()
    order = create_trading_order(
        account_id=acct_def.id, symbol="NVDA", side=OrderDirection.SELL,
        order_type=OrderType.SELL_STOP, status=status, broker_order_id="b-stop-1",
        created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours))

    acct = object.__new__(AlpacaAccount)
    acct.id = acct_def.id
    acct.client = _Client(reply)
    # The listing is non-empty (so Step 4 runs) but does not include b-stop-1.
    monkeypatch.setattr(acct, "_fetch_raw_alpaca_orders",
                        lambda *a, **k: [_RawOrder("b-unrelated", "filled")])
    monkeypatch.setattr(acct, "_check_and_submit_dependent_orders", lambda: 0)
    return acct, order


class TestStep4UnknownOutcomeLeavesTheRowAlone:
    @pytest.mark.parametrize("failure", [
        _api_error(429, '{"code":42910000,"message":"rate limit exceeded"}'),
        _api_error(500, '{"code":50010000,"message":"internal server error"}'),
        requests.exceptions.ConnectionError("connection reset by peer"),
        requests.exceptions.Timeout("read timed out"),
    ], ids=["http429", "http500", "connection-error", "timeout"])
    def test_lookup_failure_does_not_cancel_and_warns(self, monkeypatch, log, failure):
        acct, order = _setup(monkeypatch, failure)

        assert acct.refresh_orders() is True

        assert acct.client.asked, "Step 4 must ask the broker about the missing order"
        assert get_instance(TradingOrder, order.id).status == OrderStatus.NEW
        warnings = [m for m in log.at(logging.WARNING) if "b-stop-1" in m]
        assert warnings, f"no WARNING naming the order; got {log.records}"
        assert not any("marking as CANCELED" in m for _, m in log.records)

    def test_a_server_error_mentioning_not_found_is_not_absence(self, monkeypatch, log):
        """Absence is the HTTP status, not a substring of the body."""
        acct, order = _setup(monkeypatch, _api_error(
            503, '{"code":50310000,"message":"upstream 404 not found"}'))

        acct.refresh_orders()

        assert get_instance(TradingOrder, order.id).status == OrderStatus.NEW


class TestStep4ConfirmedAbsence:
    def test_http_404_on_a_recent_order_marks_it_canceled(self, monkeypatch, log):
        acct, order = _setup(monkeypatch, _api_error(
            404, '{"code":40410000,"message":"order not found for b-stop-1"}'))

        acct.refresh_orders()

        assert get_instance(TradingOrder, order.id).status == OrderStatus.CANCELED

    def test_http_404_on_an_old_order_is_left_alone(self, monkeypatch, log):
        acct, order = _setup(monkeypatch, _api_error(
            404, '{"code":40410000,"message":"order not found"}'), age_hours=24 * 40)

        acct.refresh_orders()

        assert get_instance(TradingOrder, order.id).status == OrderStatus.NEW


class TestStep4BrokerAnswersWithAStatus:
    def test_filled_at_broker(self, monkeypatch, log):
        acct, order = _setup(monkeypatch, _RawOrder(
            "b-stop-1", "filled", filled_qty="10", filled_avg_price="99.5"))

        acct.refresh_orders()

        row = get_instance(TradingOrder, order.id)
        assert row.status == OrderStatus.FILLED
        assert float(row.filled_qty) == 10.0
        assert float(row.open_price) == 99.5

    def test_canceled_at_broker(self, monkeypatch, log):
        acct, order = _setup(monkeypatch, _RawOrder("b-stop-1", "canceled"))

        acct.refresh_orders()

        assert get_instance(TradingOrder, order.id).status == OrderStatus.CANCELED

    def test_other_status_at_broker_is_adopted(self, monkeypatch, log):
        acct, order = _setup(monkeypatch, _RawOrder("b-stop-1", "accepted"),
                             status=OrderStatus.NEW)

        acct.refresh_orders()

        assert get_instance(TradingOrder, order.id).status == OrderStatus.ACCEPTED

    def test_still_working_at_broker_is_unchanged(self, monkeypatch, log):
        acct, order = _setup(monkeypatch, _RawOrder("b-stop-1", "new"))

        acct.refresh_orders()

        assert get_instance(TradingOrder, order.id).status == OrderStatus.NEW


class TestGetBrokerOrderOrAbsent:
    def _acct(self, reply):
        acct = object.__new__(AlpacaAccount)
        acct.id = 1
        acct.client = _Client(reply)
        return acct

    def test_http_404_is_absent_whatever_the_body(self, log):
        assert self._acct(_api_error(404, ""))._get_broker_order_or_absent("b") == (None, True)

    def test_non_404_status_raises_even_if_the_body_says_not_found(self, log):
        with pytest.raises(APIError):
            self._acct(_api_error(500, '{"code":50010000,"message":"not found 404"}')) \
                ._get_broker_order_or_absent("b")

    def test_no_http_response_uses_the_alpaca_error_code(self, log):
        assert self._acct(APIError('{"code":40410000,"message":"order not found"}')) \
            ._get_broker_order_or_absent("b") == (None, True)
        with pytest.raises(APIError):
            self._acct(APIError('{"code":50010000,"message":"order not found"}')) \
                ._get_broker_order_or_absent("b")

    def test_unparseable_error_without_a_response_raises(self, log):
        with pytest.raises(APIError):
            self._acct(APIError("not found"))._get_broker_order_or_absent("b")
