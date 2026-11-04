"""User data that LOOKS like a credential word must survive the scrub: a label "Private", a symbol "KEY", an expert alias
"Auth-bot-3". The scrub redacts a dict VALUE under a credential-like KEY, so no response may use user data as a dict key
(they are arrays of objects); and no GET that lists them may show ``[redacted]`` in their place."""
from datetime import datetime, timedelta

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import Instrument
from ba2_trade_platform.core.types import InstrumentType, OrderStatus, TransactionStatus
from ba2_trade_platform.services import accounts as accounts_svc
from ba2_trade_platform.services import account_growth, experts as experts_svc, open_pnl, portfolio
from tests.api_helpers import _AttrDict, _chartable_account, v1_client
from tests.factories import (create_account_definition, create_expert_instance, create_trading_order,
                             create_transaction)

pytestmark = pytest.mark.api_db
LABEL, SYMBOL, ALIAS = "Private", "KEY", "Auth-bot-3"
WINDOW = "from=" + (datetime.now() - timedelta(days=40)).date().isoformat()


@pytest.fixture
def world(monkeypatch):
    from ba2_trade_platform.services import _positions_cache
    _positions_cache.clear()
    account_growth.clear_cache()
    portfolio._DIVIDENDS_CACHE.clear()
    acct = create_account_definition(name="Paper")
    expert = create_expert_instance(account_id=acct.id, alias=ALIAS)
    add_instance(Instrument(name=SYMBOL, instrument_type=InstrumentType.STOCK, categories=[], labels=[LABEL]),
                 expunge_after_flush=True)

    def make(account_id):
        a = _chartable_account(account_id)
        a.positions = [_AttrDict(symbol=SYMBOL, qty=10.0, cost_basis=900.0, market_value=1000.0, side="BUY",
                                 current_price=110.0, unrealized_pl=100.0)]
        now = datetime.now()
        a.get_dividends = lambda symbol=None, start_date=None, end_date=None: [
            {"date": now - timedelta(days=2), "symbol": SYMBOL, "amount": 2.0, "gross_amount": 2.1,
             "tax_withheld": 0.1, "drip_quantity": None, "drip_price": None}]
        a.get_filled_trades = lambda symbol=None, start_date=None, end_date=None: [
            {"date": now - timedelta(days=20), "symbol": SYMBOL, "side": "BUY", "qty": 10.0, "price": 90.0}]
        return a
    factory = lambda i, session=None, use_cache=True: make(i)     # noqa: E731
    import ba2_trade_platform.core.utils as core_utils
    from ba2_trade_platform.services import floating_pl
    for module in (accounts_svc, open_pnl, floating_pl, core_utils):
        monkeypatch.setattr(module, "get_account_instance_from_id", factory)
    monkeypatch.setattr(portfolio, "get_account_instance_from_id", factory, raising=False)
    monkeypatch.setattr(account_growth, "fetch_closes",
                        lambda symbols, start: {s: {(datetime.now() - timedelta(days=n)).date().isoformat(): 100.0 + n
                                                    for n in range(35, 0, -1)} for s in symbols})
    monkeypatch.setattr(experts_svc, "get_expert_instance_from_id", lambda i: None, raising=False)
    closed = create_transaction(symbol=SYMBOL, quantity=5.0, status=TransactionStatus.CLOSED, open_price=100.0,
                                close_price=110.0, expert_id=expert.id, close_date=datetime.now() - timedelta(days=3))
    create_trading_order(acct.id, symbol=SYMBOL, transaction_id=closed.id, status=OrderStatus.FILLED)
    open_tx = create_transaction(symbol=SYMBOL, quantity=10.0, status=TransactionStatus.OPENED, open_price=100.0,
                                 expert_id=expert.id)
    create_trading_order(acct.id, symbol=SYMBOL, transaction_id=open_tx.id, status=OrderStatus.FILLED)
    return acct, expert


def _body(path):
    r = v1_client().get(path)
    assert r.status_code == 200, (path, r.status_code, r.text[:300])
    assert "[redacted]" not in r.text, (path, "user data was scrubbed")
    return r.text


def test_phase4_responses_show_label_symbol_and_alias_unredacted(world):
    acct, expert = world
    a = acct.id
    assert f'"name":"{LABEL}"' in _body(f"/accounts/{a}/growth/labels?{WINDOW}")
    assert f'"symbol":"{SYMBOL}"' in _body(f"/accounts/{a}/growth/labels/{LABEL}/positions?{WINDOW}")
    pnl_alias = _body(f"/accounts/{a}/pnl/closed?group_by=expert&{WINDOW}")
    assert f'"group":"{ALIAS}-{expert.id}"' in pnl_alias
    assert f'"group":"{SYMBOL}"' in _body(f"/accounts/{a}/pnl/closed?group_by=symbol&{WINDOW}")
    div = _body(f"/accounts/{a}/dividends")
    assert f'"symbol":"{SYMBOL}"' in div and f'"cash_by_symbol":[{{"symbol":"{SYMBOL}"' in div
    overview = _body(f"/stats/overview?account={a}")
    assert f'"expert":"{ALIAS}-{expert.id}"' in overview and f'"name":"{LABEL}"' in overview
    assert f'"name":"{ALIAS}-{expert.id}"' in overview          # floating P/L per expert row
    assert f'"symbol":"{SYMBOL}"' in _body("/transactions")


def test_other_get_endpoints_that_list_them_do_not_scrub_them(world):
    acct, expert = world
    assert ALIAS in _body("/experts")
    assert ALIAS in _body(f"/experts?account={acct.id}")
    assert f'"symbol":"{SYMBOL}"' in _body(f"/accounts/{acct.id}/positions")
    assert f'"symbol":"{SYMBOL}"' in _body(f"/transactions?symbol={SYMBOL}")


def test_portfolio_endpoint_shows_a_label_called_private(world):
    from ba2_trade_platform.core.portfolio_allocation_store import (
        set_allocation_config, set_managed_label, set_symbol_weight)
    acct, _ = world
    set_allocation_config(acct.id, valuation_mode="market")
    set_managed_label(acct.id, LABEL, target_pct=100.0, comment="core book")
    set_symbol_weight(acct.id, LABEL, SYMBOL, weight_pct=100.0, comment="x")
    text = _body(f"/accounts/{acct.id}/portfolio")
    assert f'"label":"{LABEL}"' in text and f'"symbol":"{SYMBOL}"' in text
