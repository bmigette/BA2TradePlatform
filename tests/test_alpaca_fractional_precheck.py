"""The fractionability pre-check: what the dry run promises is what submission does.

`plan_fractional_submission()` is the ONE place the rule lives. `_submit_order_impl`
calls it and performs the side effects; `preview_fractional_submission()` calls it with
the broker's per-symbol `Asset.fractionable` flag and returns the answer. The last two
tests here pin that the two agree, which is the whole point -- at ~25% non-fractionable
symbols, a wizard that reports success and then leaves CANCELED rows behind is worse
than one that refuses to size the row.

All three symbols are ALPACA-INTERNAL (contract 1.10). Nothing outside AlpacaAccount
imports them; the cross-broker eligibility channel is MarginInfo.fractionable.

No live call: client is a MagicMock returning real alpaca-py Asset objects.
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from alpaca.trading.enums import AssetClass, AssetExchange, AssetStatus
from alpaca.trading.models import Asset

from ba2_trade_platform.core.account_types import (
    FRACTIONAL_OUTCOME_FLOORED, FRACTIONAL_OUTCOME_KEPT, FRACTIONAL_OUTCOME_REJECTED,
    FRACTIONAL_OUTCOME_SKIPPED, FRACTIONAL_OUTCOME_WHOLE,
)
from ba2_trade_platform.core.db import add_instance, get_instance
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import OrderDirection, OrderStatus, OrderType
from ba2_trade_platform.modules.accounts.AlpacaAccount import (
    AlpacaAccount, plan_fractional_submission,
)


def _asset(symbol="AAPL", fractionable=True):
    return Asset(
        id=uuid4(), **{"class": AssetClass.US_EQUITY}, exchange=AssetExchange.NASDAQ,
        symbol=symbol, status=AssetStatus.ACTIVE, tradable=True, marginable=True,
        shortable=True, easy_to_borrow=True, fractionable=fractionable,
        min_order_size=0.001, min_trade_increment=0.001,
        maintenance_margin_requirement=30.0)


def _alpaca_response(order_id="brk-1", order_type="market"):
    """SimpleNamespace, not MagicMock: alpaca_order_to_tradingorder reads the response
    with getattr(..., None) into a pydantic TradingOrder, and a MagicMock attribute
    would fail validation instead of falling back to None."""
    return SimpleNamespace(
        id=order_id, symbol="AAPL", qty="1", side="buy", type=order_type,
        status="new", time_in_force="day", order_class=None, legs=None,
        filled_qty="0", filled_avg_price=None, created_at=None,
        limit_price=None, stop_price=None)


def _bare_account(fractionable=True):
    acct = object.__new__(AlpacaAccount)
    acct.id = 1
    acct.client = MagicMock()
    acct._authentication_error = None
    acct._asset_cache = {}
    acct._margin_info_cache = {}
    acct._balance_cache_lock = threading.Lock()
    acct._balance_cache_time = 0.0
    acct.client.get_asset.side_effect = lambda s: _asset(s, fractionable=fractionable)
    acct.client.submit_order.return_value = _alpaca_response()
    return acct


def _saved_order(**kwargs):
    defaults = dict(account_id=1, symbol="AAPL", quantity=1.5, side=OrderDirection.BUY,
                    order_type=OrderType.MARKET, status=OrderStatus.PENDING, good_for=None)
    defaults.update(kwargs)
    return get_instance(TradingOrder, add_instance(TradingOrder(**defaults)))


# ---------------------------------------------------------------------------
# The pure rule
# ---------------------------------------------------------------------------

def test_a_whole_quantity_needs_no_adjustment_at_all():
    preview = plan_fractional_submission("AAPL", 3.0, "market")

    assert preview.outcome == FRACTIONAL_OUTCOME_WHOLE
    assert preview.submit_quantity == 3.0
    assert preview.requires_day_tif is False
    assert preview.is_adjusted is False


def test_a_fractionable_symbol_keeps_its_fraction_on_a_plain_market_order():
    preview = plan_fractional_submission("AAPL", 2.5, "market", fractionable=True)

    assert preview.outcome == FRACTIONAL_OUTCOME_KEPT
    assert preview.submit_quantity == 2.5
    assert preview.requires_day_tif is True     # Alpaca refuses fractional on any other TIF
    assert preview.is_adjusted is False


def test_a_non_fractionable_symbol_is_reported_as_a_broker_rejection():
    """Alpaca accepts a fraction only where Asset.fractionable is true. Saying so up
    front is the difference between "size this whole" and an ERROR row after the fact."""
    preview = plan_fractional_submission("BRK.A", 2.5, "market", fractionable=False)

    assert preview.outcome == FRACTIONAL_OUTCOME_REJECTED
    assert preview.fractionable is False
    assert "fractionable" in preview.reason
    assert "whole shares" in preview.reason
    # REJECTED is the one outcome where the quantity DOES reach the wire -- and is
    # refused there. Reporting submit_quantity=None would contradict will_submit=True
    # and leave a caller reading "nothing is sent" about an order that is.
    assert preview.submit_quantity == 2.5
    assert preview.will_submit is True
    assert preview.is_adjusted is False     # nothing was silently re-sized


def test_an_unknown_fractionability_stays_unknown_rather_than_becoming_ineligible():
    """Absence of the flag is not evidence of ineligibility -- coercing it to False
    would make the dry run claim a rounding that never happens."""
    preview = plan_fractional_submission("AAPL", 2.5, "market", fractionable=None)

    assert preview.outcome == FRACTIONAL_OUTCOME_KEPT
    assert preview.fractionable is None
    assert "unknown" in preview.reason


def test_a_fraction_on_a_limit_order_is_reported_as_floored_before_submission():
    preview = plan_fractional_submission("AAPL", 2.5, "buy_limit", fractionable=True)

    assert preview.outcome == FRACTIONAL_OUTCOME_FLOORED
    assert preview.submit_quantity == 2.0
    assert preview.is_adjusted is True
    assert "floored to 2.0 whole shares" in preview.reason


def test_a_fraction_that_floors_to_zero_is_reported_as_skipped_not_as_a_silent_cancel():
    preview = plan_fractional_submission("AAPL", 0.4, "buy_limit", fractionable=True)

    assert preview.outcome == FRACTIONAL_OUTCOME_SKIPPED
    assert preview.submit_quantity is None
    assert preview.will_submit is False
    assert "flooring leaves 0 whole shares" in preview.reason


def test_the_order_type_rule_wins_over_fractionability():
    """A non-fractionable symbol on a LIMIT order is floored, not rejected: the floor
    happens before anything reaches the wire, so the broker never sees a fraction."""
    preview = plan_fractional_submission("BRK.A", 2.5, "buy_limit", fractionable=False)

    assert preview.outcome == FRACTIONAL_OUTCOME_FLOORED


def test_the_wash_trade_escape_counts_as_a_complex_order():
    """use_complex_order re-classes a MARKET request as BRACKET/OTO, which Alpaca
    refuses fractionally just like a limit order."""
    preview = plan_fractional_submission("AAPL", 2.5, "market", use_complex_order=True,
                                         fractionable=True)

    assert preview.outcome == FRACTIONAL_OUTCOME_FLOORED
    assert "BRACKET/OTO" in preview.reason


# ---------------------------------------------------------------------------
# The account-level pre-check
# ---------------------------------------------------------------------------

def test_the_precheck_reads_the_brokers_own_fractionable_flag():
    acct = _bare_account(fractionable=False)

    preview = acct.preview_fractional_submission("BRK.A", 2.5)

    assert preview.fractionable is False
    assert preview.outcome == FRACTIONAL_OUTCOME_REJECTED


def test_the_precheck_normalises_the_symbol():
    acct = _bare_account()

    preview = acct.preview_fractional_submission("  aapl ", 2.5)

    assert preview.symbol == "AAPL"
    assert acct.client.get_asset.call_args[0][0] == "AAPL"


def test_the_precheck_reports_unknown_when_the_asset_lookup_fails():
    acct = _bare_account()
    acct.client.get_asset.side_effect = RuntimeError("404 asset not found")

    preview = acct.preview_fractional_submission("NOSUCH", 2.5)

    assert preview.fractionable is None
    assert preview.outcome == FRACTIONAL_OUTCOME_KEPT


def test_the_precheck_accepts_the_core_order_type_enum():
    acct = _bare_account()

    preview = acct.preview_fractional_submission("AAPL", 2.5, OrderType.BUY_LIMIT)

    assert preview.outcome == FRACTIONAL_OUTCOME_FLOORED


# ---------------------------------------------------------------------------
# Preview and submission cannot drift apart
# ---------------------------------------------------------------------------

def test_the_preview_predicts_the_exact_quantity_the_submission_sends():
    acct = _bare_account()
    acct.client.submit_order.return_value = _alpaca_response(order_type="limit")
    order = _saved_order(quantity=4.25, order_type=OrderType.SELL_LIMIT,
                         side=OrderDirection.SELL, limit_price=100.0, good_for='day')

    preview = acct.preview_fractional_submission("AAPL", 4.25, OrderType.SELL_LIMIT)
    acct._submit_order_impl(order)

    sent_qty = float(acct.client.submit_order.call_args[0][0].qty)
    assert preview.submit_quantity == 4.0
    assert sent_qty == preview.submit_quantity


def test_submission_never_spends_an_asset_round_trip_on_the_hot_order_path():
    """`_submit_order_impl` passes `fractionable` as UNKNOWN on purpose.

    The allocation engine already gated sizing on the broker's per-symbol flag via
    MarginInfo.fractionable, so re-asking here would buy nothing and cost one HTTP call
    per order submitted -- an invisible per-order latency tax on every basket. This
    pins the absence of that lookup, which is otherwise the sort of thing a later
    "make submission consistent with the preview" change adds without noticing.
    """
    acct = _bare_account()

    acct._submit_order_impl(_saved_order(quantity=1.5, order_type=OrderType.MARKET))

    acct.client.get_asset.assert_not_called()
    acct.client.get_all_assets.assert_not_called()


def test_the_preview_of_a_skip_matches_the_row_the_submission_actually_cancels():
    """The failure this whole task exists to prevent: the wizard reporting a 0.4-share
    buy as submitted, and a CANCELED row appearing afterwards with no warning."""
    acct = _bare_account()
    order = _saved_order(quantity=0.4, order_type=OrderType.BUY_LIMIT, limit_price=100.0)

    preview = acct.preview_fractional_submission("AAPL", 0.4, OrderType.BUY_LIMIT)
    result = acct._submit_order_impl(order)

    assert preview.outcome == FRACTIONAL_OUTCOME_SKIPPED
    assert preview.will_submit is False
    acct.client.submit_order.assert_not_called()
    assert result is None
    stored = get_instance(TradingOrder, order.id)
    assert stored.status == OrderStatus.CANCELED
    # The dry run's sentence and the persisted comment are the SAME string.
    assert stored.comment == preview.reason


# ---------------------------------------------------------------------------
# No notional, anywhere (contract 1.12)
# ---------------------------------------------------------------------------

def test_no_alpaca_request_is_ever_built_with_a_notional_field():
    """Fractional means a fractional share QUANTITY (qty=2.5431), never a dollar
    amount. Alpaca's requests DO expose a `notional=` field and an OrderType.NOTIONAL
    family; this module must never reach for either."""
    import inspect
    import sys

    source = inspect.getsource(sys.modules[AlpacaAccount.__module__])

    assert "notional=" not in source
    assert "NOTIONAL" not in source
