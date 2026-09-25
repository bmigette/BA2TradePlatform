"""CloseOptionAction books the close on the transaction it was DECIDED for (review 2026-09-25, I2).

``CloseOptionAction.execute`` resolves ONE option order -- the evaluated order, or its
transaction's option entry -- and used to hand the account only a contract-level position, so
the account re-derived "the" holder from the contract: the FIRST open transaction holding it.
With two transactions on one contract (two experts, a merged lot) that closed the wrong one --
a spread leg closed out of the wrong structure leaves its partner naked. The action now passes
the resolved order's ``transaction_id``; every account honours an explicit id (AlpacaAccount:
"an explicit transaction_id from a caller that has one still wins"). This pins the LIVE side:
a plain OptionsAccountInterface double receives the id.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.models import TradingOrder
from ba2_common.core.types import (
    AssetClass, OptionRight, OrderDirection, OrderRecommendation, OrderStatus, OrderType,
)


def _order(txn_id):
    return TradingOrder(
        id=11, account_id=1, symbol="AAPL", underlying_symbol="AAPL", quantity=2, filled_qty=2,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
        open_price=1.5, asset_class=AssetClass.OPTION, contract_symbol="AAPL261218P00200000",
        option_type=OptionRight.PUT, strike=200.0, expiry=date(2026, 12, 18),
        transaction_id=txn_id)


def _execute(txn_id):
    from ba2_common.core.TradeActions import CloseOptionAction
    account = MagicMock(spec=OptionsAccountInterface)
    account.get_option_quote.return_value = None           # close limit falls back to entry
    account.close_option_position.return_value = SimpleNamespace(id=99)
    action = CloseOptionAction(instrument_name="AAPL", account=account,
                               order_recommendation=OrderRecommendation.SELL,
                               existing_order=_order(txn_id))
    action.create_and_save_action_result = lambda **kw: SimpleNamespace(**kw)
    action._stamp_exit_record = lambda *a, **k: None       # no DB row to stamp here
    result = action.execute()
    return result, account.close_option_position


def test_close_option_passes_the_resolved_orders_transaction_id():
    result, close = _execute(txn_id=42)
    assert result.success, result.message
    close.assert_called_once()
    assert close.call_args.kwargs["transaction_id"] == 42
    position = close.call_args.args[0]
    assert (position.contract_symbol, position.quantity) == ("AAPL261218P00200000", 2.0)


def test_an_order_without_a_transaction_passes_none_and_the_account_resolves_it():
    result, close = _execute(txn_id=None)
    assert result.success, result.message
    assert close.call_args.kwargs["transaction_id"] is None
