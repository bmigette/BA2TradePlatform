"""Record real option actions through the REAL recorder, for the option selection replay tests.

A fake broker-backed options account (a subclass of ``OptionsAccountInterface``, so the option
taps are applied to it exactly as they are to Alpaca / IBKR) is driven through
``TradeActions`` inside a real ``capture_scope`` over a real ``ReplayStore`` (sync writer) in a
tmp folder; the session is exported with the existing export service and loaded back with
``load_bundle``. Nothing about the bundle is hand-written.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from ba2_common.core.TradeActions import CloseOptionAction, create_action
from ba2_common.core.account_types import AccountSnapshot
from ba2_common.core.db import add_instance
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.interfaces.ReadOnlyAccountInterface import quote_identity
from ba2_common.core.models import (
    ExpertInstance, ExpertRecommendation, ExpertSetting, TradingOrder, Transaction,
)
from ba2_common.core.option_types import OptionContract, OptionQuote
from ba2_common.core.replay import ReplayStore, SessionRecord, capture_scope
from ba2_common.core.replay.observe import observe_provider
from ba2_common.core.types import (
    AssetClass, ExpertActionType, OptionCloseReason, OptionRight, OrderDirection,
    OrderRecommendation, OrderStatus, OrderType, TransactionStatus,
)

from app.services.replay.option_selection import _scratch_trading_db

SESSION_ID = "S-OPTION"
NOW = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)
TODAY = date(2026, 11, 5)
EXPIRY = date(2026, 11, 27)

BPS = dict(strike_method="percent_otm", strike_param=[5.0, 10.0], dte_min=10, dte_max=40,
           sizing=5.0)


class RecordingOptionsAccount(OptionsAccountInterface):
    """A live-shaped options account: chain, quotes and the sizing figures come from here.

    The class is a real ``OptionsAccountInterface`` subclass, so every tapped method it defines
    is wrapped by ``__init_subclass__`` -- the taps under test are the shipped ones.
    """

    OPTION_GREEKS_SOURCE = "broker"

    def __init__(self, spot=100.0, balance=100_000.0):
        self.id = 1
        self._spot = spot
        self._balance = balance
        self.submitted: List[Dict[str, Any]] = []
        self.closed: List[Dict[str, Any]] = []

    # -- account figures. ``decision_label`` is NOT overridden: like every live broker account
    # it reads the replay-aware clock, so a recorded action has clock reads to replay.
    def get_balance(self):
        return self._balance

    def get_tradable_balance(self):
        return self.get_balance()

    def get_option_tradable_balance(self):
        return self.get_balance()

    def get_account_snapshot(self):
        return AccountSnapshot(cash=self._balance, equity=self._balance,
                               net_liquidation=self._balance, buying_power=self._balance)

    def get_positions(self):
        return []

    def get_instrument_current_price(self, symbol, price_type=None):
        return self._spot

    # -- the option reads
    def get_option_chain(self, underlying, expiry_min, expiry_max, option_type,
                         strike_min=None, strike_max=None):
        rows = []
        for strike in range(80, 121, 5):
            if option_type == OptionRight.CALL:
                otm = max(float(strike) - self._spot, 0.0)
                intrinsic = max(self._spot - float(strike), 0.0)
            else:
                otm = max(self._spot - float(strike), 0.0)
                intrinsic = max(float(strike) - self._spot, 0.0)
            bid = max(0.2, 5.0 - 0.08 * otm) + intrinsic
            rows.append(OptionContract(
                symbol=f"{underlying}{strike}{'C' if option_type == OptionRight.CALL else 'P'}",
                underlying=underlying, option_type=option_type, strike=float(strike),
                expiry=date.today() + timedelta(days=20), bid=round(bid, 4), ask=round(bid + 0.2, 4), last=round(bid, 4),
                open_interest=1000, delta=None, implied_volatility=None))
        return rows

    def get_option_quote(self, contract_symbol):
        return OptionQuote(symbol=contract_symbol, bid=1.40, ask=1.60, last=1.50)

    def get_atm_implied_volatility(self, underlying):
        return 0.3

    def get_option_positions(self):
        return []

    # -- the writes (recorded by the action, not by the account)
    def _submit_option_order_impl(self, trading_order, legs, leg_orders=None):
        return trading_order

    def submit_option_order(self, *, legs, quantity, order_type, limit_price, option_strategy,
                            expert_recommendation_id=None, transaction_id=None):
        self.submitted.append(dict(legs=legs, quantity=quantity, limit_price=limit_price,
                                   strategy=option_strategy))
        order_id = add_instance(TradingOrder(
            account_id=1, symbol="XYZ", quantity=quantity, side=OrderDirection.SELL,
            order_type=OrderType.MARKET, status=OrderStatus.PENDING))
        return SimpleNamespace(id=order_id, transaction_id=None, data={})

    def close_option_position(self, position, order_type="limit", limit_price=None,
                              transaction_id=None):
        self.closed.append(dict(position=position, limit_price=limit_price))
        order_id = add_instance(TradingOrder(
            account_id=1, symbol=position.underlying, quantity=position.quantity,
            side=OrderDirection.SELL, order_type=OrderType.MARKET, status=OrderStatus.PENDING))
        return SimpleNamespace(id=order_id, transaction_id=transaction_id, data={})


# A real stock-quote tap on the fake, shaped exactly like ReadOnlyAccountInterface's.
RecordingOptionsAccount.get_instrument_current_price = observe_provider(
    "broker", "get_instrument_current_price", identity=quote_identity)(
        RecordingOptionsAccount.get_instrument_current_price)


def recommendation(**over):
    fields = dict(id=1, instance_id=None, symbol="XYZ", data=None, price_at_date=100.0,
                  expected_profit_percent=5.0, recommended_action=OrderRecommendation.BUY)
    fields.update(over)
    return SimpleNamespace(**fields)


def entry_action(account, action_type="open_bull_put_spread", rec=None, **config):
    action = create_action(ExpertActionType(action_type), "XYZ", account, OrderRecommendation.BUY,
                           None, rec or recommendation(), **(config or dict(BPS)))
    action.submit_to_broker = True
    return action


def seed_open_option(*, txn_status=TransactionStatus.OPENED) -> TradingOrder:
    """A held long put and its transaction, in the current scratch DB."""
    txn_id = add_instance(Transaction(symbol="XYZ", quantity=2, side=OrderDirection.BUY,
                                      status=txn_status))
    order_id = add_instance(TradingOrder(
        account_id=1, symbol="XYZ", underlying_symbol="XYZ", quantity=2, filled_qty=2,
        side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT, status=OrderStatus.FILLED,
        open_price=1.5, asset_class=AssetClass.OPTION, contract_symbol="XYZ261127P00095000",
        option_type=OptionRight.PUT, strike=95.0, expiry=EXPIRY, transaction_id=txn_id))
    from ba2_common.core.db import get_instance
    return get_instance(TradingOrder, order_id)


def close_action(account, order, rec=None):
    return CloseOptionAction(
        instrument_name="XYZ", account=account, order_recommendation=OrderRecommendation.SELL,
        existing_order=order, expert_recommendation=rec or recommendation(id=5),
        forced_exit=True, close_trigger=OptionCloseReason.MANUAL, rule_id=9, rule_name="r",
        close_target=None)


def meta(analysis_id="decision-1"):
    return {"analysis_id": analysis_id, "attempt_id": f"{analysis_id}-att",
            "session_id": SESSION_ID, "expert_class": "TradeManager",
            "expert_instance_id": 7, "symbol": "XYZ", "use_case": "enter_market",
            "scheduled_at": NOW, "started_at": NOW}


def record_session(store_root: Path, export_dir: Path,
                   scenario: Callable[[], Any], *, seed: Optional[Callable[[], None]] = None,
                   analysis_id: str = "decision-1") -> Dict[str, Any]:
    """Run ``scenario()`` inside a real capture scope, export the session, return the details.

    ``seed`` runs first, inside the recording's scratch trading DB (rows an action reads).
    ``scenario`` returns whatever the test wants back (action results, accounts).
    """
    store = ReplayStore(store_root, writer="sync")
    store.begin_session(SessionRecord(
        session_id=SESSION_ID, instance_id="option-test", started_at=NOW,
        exchange_tz="America/New_York", app_version="test",
        package_versions={"ba2_common": "test"}, source_revision="0" * 40, dirty=False))
    try:
        with _scratch_trading_db([]):
            if seed is not None:
                seed()
            with capture_scope(store, meta(analysis_id)) as capture:
                outcome = scenario()
                capture.set_outcome(skip_reason="option actions recorded")
        exported = store.export_session(SESSION_ID, export_dir)
    finally:
        store.close(timeout=5.0)
    return {"bundle_dir": exported, "outcome": outcome}
