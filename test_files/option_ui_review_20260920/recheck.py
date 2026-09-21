"""Offline second review at 80139b93. Uses actual UI method binding and ORM sessions.

Run from the repo with its trade venv. All accounts are mocks; the only database
used is an in-memory SQLite engine. Assertions document observed remaining bugs.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'testplatform/backend'))

from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus, TransactionStatus
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.option_payoff_chart import build_payoff_chart, chart_legs_from_rows
from ba2_trade_platform.core.option_positions import opening_legs
from ba2_trade_platform.ui.pages.option_trades import OptionTradesTab
from ba2_trade_platform.ui.pages import option_trades
from app.models.backtest import Backtest
from app.models.strategy_optimization import StrategyOptimization
from app.services.backtest_trade_chart import option_store_provenance
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

STAMP = datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc)
txn = NS(id=7, symbol='XYZ', side=OrderDirection.BUY, quantity=1,
         open_price=6, close_price=None, multiplier=100, expiry=None,
         option_strategy='long_call', status=TransactionStatus.OPENED,
         expert_id=None, take_profit=None, stop_loss=None,
         created_at=STAMP, open_date=STAMP, close_date=None)


def order(oid, side=OrderDirection.BUY, premium=8, strike=95):
    return NS(id=oid, account_id=1, transaction_id=7, contract_symbol=f'XYZ_C{strike}',
              side=side, option_type=NS(value='call'), strike=strike,
              open_price=premium, quantity=1, filled_qty=1, multiplier=100,
              underlying_symbol='XYZ', expiry=None, asset_class=AssetClass.OPTION,
              status=OrderStatus.FILLED, created_at=STAMP, position_intent=None)


class OrdersSession:
    def exec(self, _):
        return NS(all=lambda: [order(1)])
    def get(self, *_):
        return NS(name='Fixture account')


tab = OptionTradesTab()  # Real instance: no rebinding descriptors with MethodType.
account = MagicMock(spec=OptionsAccountInterface)
account.get_option_quote.return_value = NS(bid=13.3, ask=13.4, last=13.35)
with patch.object(option_trades, 'get_account_instance_from_id', return_value=account), \
     patch.object(option_trades, 'option_transaction_pnl', return_value=NS(available=False)):
    try:
        tab._build_rows([txn], {}, OrdersSession())
    except TypeError as error:
        assert "missing 1 required positional argument: 'order'" in str(error)
        print('SINGLE_LEG_LOADER:', str(error))
    else:
        raise AssertionError('Expected the real instance staticmethod binding error')

engine = create_engine('sqlite:///:memory:')
StrategyOptimization.__table__.create(engine)
with Session(engine) as session:
    session.add(StrategyOptimization(id=42, strategy_id=1, fitness_metric='return',
        optimization_type='genetic', optimization_config={'backtest': {
            'options_store': 'sqlite', 'options_cache_db': 'fixture.sqlite'}}))
    session.commit()
    assert session.get(StrategyOptimization, 42).optimization_config['backtest']['options_store'] == 'sqlite'
    found = option_store_provenance(Backtest(id=77, optimization_id=42), session)
    assert not found.resolved
    print('REAL_BACKEND_SESSION: persisted optimization exists, but provenance unresolved; SQLAlchemy Session has no exec().')
engine.dispose()

class CappedSession:
    def exec(self, statement):
        limit = statement._limit_clause.value if statement._limit_clause is not None else 501
        return NS(one=lambda: 501,
                  all=lambda: [(NS(id=i), None) for i in range(min(limit, 501))])
    def close(self):
        pass


tab = OptionTradesTab()
tab._build_rows = lambda txns, experts, session: [{'id': t.id} for t in txns]
with patch.object(option_trades, 'get_db', return_value=CappedSession()), \
     patch.object(option_trades, 'get_selected_account_id', return_value=None), \
     patch.object(option_trades, 'scope_transactions_to_account', side_effect=lambda query, _: query):
    rows, total = tab._collect_rows(26, 20, {}, 'id', False)
assert rows == [] and total == 501
print('ROW_CAP: page 26/26 is empty while total_count=501; the 500-row totals cap also caps browsable rows.')

long = order(1)
short = order(2, OrderDirection.SELL, premium=None, strike=105)
legs = opening_legs(txn, [long, short])
curve = build_payoff_chart(chart_legs_from_rows(legs.chart_rows()))
assert legs.count == 1 and curve.available and curve.max_profit.unlimited
print('INCOMPLETE_STRUCTURE: filled short leg with no recorded premium is dropped; spread becomes an available unlimited-profit call curve.')

# Bypass only the broken binding to independently test the cache's account key.
tab = OptionTradesTab()
second = MagicMock(spec=OptionsAccountInterface)
second.get_option_quote.return_value = NS(bid=20, ask=21, last=20.5)
a = OptionTradesTab._contract_quote(tab, account, long)
b = OptionTradesTab._contract_quote(tab, second, long)
assert a == b == 13.3 and second.get_option_quote.call_count == 0
print('QUOTE_SCOPE: second account receives first account quote 13.3, not its own 20; cache keyed only by contract.')
print('Five remaining defects reproduced using isolated fixtures; no production services accessed.')
