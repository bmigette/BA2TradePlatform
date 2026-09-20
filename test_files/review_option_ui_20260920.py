"""Offline review probes. Never opens a broker, a production DB, or a data provider.

Run with the trade venv from the repo root. Assertions confirm the observed defects,
not desired behavior. Methods are extracted verbatim to avoid constructing live UI.
"""
from __future__ import annotations

import ast
import gc
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'testplatform/backend'))

from ba2_common.core import TradeConditions
from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
from ba2_common.core.types import AssetClass, OrderDirection, OrderStatus, TransactionStatus
from ba2_trade_platform.core.option_pnl_display import option_transaction_pnl
from ba2_trade_platform.ui.components.option_structure_chart import chart_inputs_from
from app.services.backtest_trade_chart import build_trade_chart_context, contract_detail


def method(relative, class_name, name, scope=None):
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(scope or {})
    namespace['__name__'] = 'ba2_trade_platform.ui.pages.review_probe'
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / relative), 'exec'), namespace)
    return namespace[name]


def order(oid, contract, side, strike, premium, **extra):
    values = dict(id=oid, account_id=1, transaction_id=7, contract_symbol=contract,
                  side=side, option_type=NS(value='call'), strike=strike,
                  open_price=premium, quantity=1, filled_qty=1, multiplier=100,
                  underlying_symbol='XYZ', expiry=date(2026, 9, 18),
                  asset_class=AssetClass.OPTION, status=OrderStatus.FILLED,
                  created_at=datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc))
    values.update(extra)
    return NS(**values)


txn = NS(id=7, symbol='XYZ', side=OrderDirection.BUY, quantity=1,
         open_price=6, close_price=None, multiplier=100, expiry=date(2026, 9, 18),
         option_strategy='bull_call_spread', status=TransactionStatus.OPENED,
         expert_id=None, take_profit=None, stop_loss=None,
         created_at=datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc),
         open_date=datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc), close_date=None)
entry = [order(1, 'XYZ_C95', OrderDirection.BUY, 95, 8),
         order(2, 'XYZ_C105', OrderDirection.SELL, 105, 2)]
closing = [order(3, 'XYZ_C95', OrderDirection.SELL, 95, 13.3),
           order(4, 'XYZ_C105', OrderDirection.BUY, 105, 3.6)]
payoff_for = method('ba2_trade_platform/ui/pages/live_trades.py', 'LiveTradesTab', '_payoff_for')
before = payoff_for(None, txn, entry)
after = payoff_for(None, txn, entry + closing)
assert before.payoff_at(90) == -600 and before.payoff_at(110) == 400
assert abs(after.payoff_at(90) - 370) < 1e-8
assert abs(after.payoff_at(110) - 370) < 1e-8
print('LIVE_PAYOFF: entry position [-600, +400]; after passing closing orders [+370, +370] (flat).')

account = MagicMock(spec=OptionsAccountInterface)
account.get_option_quote.side_effect = lambda symbol: (
    NS(bid=13.3, ask=13.4, last=13.35) if symbol == 'XYZ_C95'
    else NS(bid=3.5, ask=3.6, last=3.55))
parent = order(0, None, OrderDirection.BUY, None, 6)

class FakeSession:
    def exec(self, _):
        return NS(all=lambda: [parent] + entry)
    def get(self, *_):
        return NS(name='Fixture account')

tab = NS(_refresh_totals=lambda: None)
build_rows = method('ba2_trade_platform/ui/pages/option_trades.py', 'OptionTradesTab', '_build_rows', {
    'date': date, 'List': list, 'Dict': dict,
    'get_account_instance_from_id': lambda *args, **kwargs: account,
    'option_transaction_pnl': option_transaction_pnl,
    '_pnl_text': lambda amount, percent: str(amount),
})
with patch.object(TradeConditions, '_get_transaction_for_order', return_value=txn):
    rows = build_rows(tab, [txn], {}, FakeSession())
assert rows[0]['current_pnl'] == '730.0', rows
print('LIVE_PNL: Options tab reports +730; full spread executable mark is (13.3 - 3.6 - 6)*100 = +370.')

bars = [dict(date='2026-09-08', open=100, high=103, low=99, close=102),
        dict(date='2026-09-11', open=125, high=131, low=124, close=130)]
_, _, markers = chart_inputs_from(txn, entry, bars)
assert markers[0]['price'] == 130
print('LIVE_MARKERS: entry on Sep 8 placed at 130, outside its bar [99, 103]; takes latest close.')

reader = NS(db_path='fixture', latest_bar_on_or_before=lambda symbol, day: {
    'date': day, 'iv': .42, 'delta': .65})
detail = contract_detail(reader, 'XYZ_C95', datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc))
assert detail['asOf'] == '2026-09-08' and detail['quality'] == 'cache_bar'
print('BT_GREEKS: 13:30 UTC entry reads same-day daily option IV/delta, without intraday availability check.')

with tempfile.TemporaryDirectory(prefix='ba2-ui-review-') as folder:
    cache = Path(folder) / 'previously-absent.sqlite'
    bt = NS(id=77, engine_type='unknown', settings={'options_cache_db': str(cache)}, trades=[{
        'transaction_id': 7, 'option_type': 'call', 'contract_symbol': 'XYZ_C95',
        'underlying_symbol': 'XYZ', 'entry_time': '2026-09-08T13:30:00Z',
        'exit_time': '2026-09-11T19:45:00Z', 'entry_price': 8, 'exit_price': 13.3,
        'direction': 'buy', 'size': 1, 'strike': 95, 'expiry': '2026-09-18',
        'multiplier': 100, 'pnl': 530, 'pnl_pct': 5.3, 'exit_reason': 'exit'}])
    assert not cache.exists()
    build_trade_chart_context(bt, 1)
    assert cache.exists()
    print('BT_CACHE_WRITE: chart context creates a new SQLite option cache when saved path does not exist.')
    gc.collect()  # Release temporary sqlite connections before Windows removes the fixture.

print('All five offline defect probes reproduced; no live services accessed.')
