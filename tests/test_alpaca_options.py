from datetime import date
from types import SimpleNamespace
import pytest

from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
from ba2_trade_platform.core.interfaces import OptionsAccountInterface
from ba2_trade_platform.core.types import OptionRight


def _make_alpaca():
    acct = AlpacaAccount.__new__(AlpacaAccount)        # bypass __init__/DB
    acct.id = 999
    acct._settings_cache = {"api_key": "k", "api_secret": "s", "paper_account": True,
                            "data_feed": "iex"}
    return acct


def test_alpaca_is_option_capable():
    assert issubclass(AlpacaAccount, OptionsAccountInterface)


def test_get_option_chain_maps_snapshot(monkeypatch):
    acct = _make_alpaca()
    greeks = SimpleNamespace(delta=0.55, gamma=0.02, theta=-0.04, vega=0.1, rho=0.01)
    quote = SimpleNamespace(bid_price=5.0, ask_price=5.4, bid_size=10, ask_size=12, timestamp=None)
    trade = SimpleNamespace(price=5.2, timestamp=None)
    snap = SimpleNamespace(symbol="AAPL260116C00150000", latest_quote=quote, latest_trade=trade,
                           implied_volatility=0.32, greeks=greeks)
    snapshots = {"AAPL260116C00150000": snap}

    class FakeOptClient:
        def get_option_chain(self, req):
            return snapshots
        def get_option_snapshot(self, req):
            return snapshots

    occ_meta = SimpleNamespace(symbol="AAPL260116C00150000", underlying_symbol="AAPL",
                               type=SimpleNamespace(value="call"), strike_price=150.0,
                               expiration_date=date(2026, 1, 16), open_interest="1200")
    acct._option_data_client = FakeOptClient()
    monkeypatch.setattr(acct, "_get_option_contracts_meta",
                        lambda *a, **k: {"AAPL260116C00150000": occ_meta}, raising=False)

    chain = acct.get_option_chain("AAPL", date(2026, 1, 1), date(2026, 3, 1), OptionRight.CALL)
    assert len(chain) == 1
    c = chain[0]
    assert c.symbol == "AAPL260116C00150000"
    assert c.underlying == "AAPL"
    assert c.option_type == OptionRight.CALL
    assert c.strike == 150.0
    assert c.expiry == date(2026, 1, 16)
    assert c.delta == 0.55 and c.implied_volatility == 0.32
    assert c.bid == 5.0 and c.ask == 5.4 and c.last == 5.2
    assert c.open_interest == 1200


def test_get_option_quote_maps_snapshot(monkeypatch):
    acct = _make_alpaca()
    greeks = SimpleNamespace(delta=0.4, gamma=0.01, theta=-0.02, vega=0.05, rho=0.0)
    quote = SimpleNamespace(bid_price=2.0, ask_price=2.2, bid_size=5, ask_size=7, timestamp=None)
    trade = SimpleNamespace(price=2.1, timestamp=None)
    snap = SimpleNamespace(symbol="AAPL260116C00150000", latest_quote=quote, latest_trade=trade,
                           implied_volatility=0.30, greeks=greeks)

    class FakeOptClient:
        def get_option_snapshot(self, req):
            return {"AAPL260116C00150000": snap}
    acct._option_data_client = FakeOptClient()

    q = acct.get_option_quote("AAPL260116C00150000")
    assert q is not None
    assert q.symbol == "AAPL260116C00150000"
    assert q.bid == 2.0 and q.ask == 2.2 and q.last == 2.1
    assert q.implied_volatility == 0.30 and q.delta == 0.4


def test_get_atm_iv_picks_nearest_strike(monkeypatch):
    acct = _make_alpaca()
    # Spot ~150; chain returns strikes 145/150/155 with different IVs; ATM=150 -> iv 0.31
    from ba2_trade_platform.core.option_types import OptionContract
    def fake_chain(underlying, emin, emax, option_type=None, strike_min=None, strike_max=None):
        return [
            OptionContract(symbol="c145", underlying="AAPL", option_type=OptionRight.CALL,
                           strike=145.0, expiry=date(2026, 1, 16), implied_volatility=0.40),
            OptionContract(symbol="c150", underlying="AAPL", option_type=OptionRight.CALL,
                           strike=150.0, expiry=date(2026, 1, 16), implied_volatility=0.31),
            OptionContract(symbol="c155", underlying="AAPL", option_type=OptionRight.CALL,
                           strike=155.0, expiry=date(2026, 1, 16), implied_volatility=0.35),
        ]
    monkeypatch.setattr(acct, "get_option_chain", fake_chain, raising=False)
    monkeypatch.setattr(acct, "get_instrument_current_price", lambda *a, **k: 150.0, raising=False)
    iv = acct.get_atm_implied_volatility("AAPL")
    assert iv == 0.31


def test_get_option_chain_none_guards(monkeypatch):
    acct = _make_alpaca()
    snap = SimpleNamespace(symbol="AAPL260116C00150000", latest_quote=None,
                           latest_trade=None, implied_volatility=None, greeks=None)
    class FakeOptClient:
        def get_option_chain(self, req): return {"AAPL260116C00150000": snap}
    occ_meta = SimpleNamespace(symbol="AAPL260116C00150000", underlying_symbol="AAPL",
                               type=SimpleNamespace(value="call"), strike_price=150.0,
                               expiration_date=date(2026, 1, 16), open_interest=None)
    acct._option_data_client = FakeOptClient()
    monkeypatch.setattr(acct, "_get_option_contracts_meta",
                        lambda *a, **k: {"AAPL260116C00150000": occ_meta}, raising=False)
    chain = acct.get_option_chain("AAPL", date(2026,1,1), date(2026,3,1), OptionRight.CALL)
    assert len(chain) == 1
    c = chain[0]
    assert c.bid is None and c.ask is None and c.last is None
    assert c.delta is None and c.implied_volatility is None
    assert c.strike == 150.0 and c.expiry == date(2026,1,16)
    assert c.option_type == OptionRight.CALL
    assert c.open_interest is None


def test_get_option_chain_join_asymmetry(monkeypatch):
    acct = _make_alpaca()
    def mk_snap(sym):
        return SimpleNamespace(symbol=sym,
            latest_quote=SimpleNamespace(bid_price=1.0, ask_price=1.2, bid_size=1, ask_size=1, timestamp=None),
            latest_trade=SimpleNamespace(price=1.1, timestamp=None),
            implied_volatility=0.3,
            greeks=SimpleNamespace(delta=0.5, gamma=0.0, theta=0.0, vega=0.0, rho=0.0))
    class FakeOptClient:
        def get_option_chain(self, req):
            return {"AAPL260116C00150000": mk_snap("AAPL260116C00150000"),
                    "AAPL260116C00160000": mk_snap("AAPL260116C00160000")}  # no meta for ...160000
    occ_meta = SimpleNamespace(symbol="AAPL260116C00150000", underlying_symbol="AAPL",
                               type=SimpleNamespace(value="call"), strike_price=150.0,
                               expiration_date=date(2026,1,16), open_interest="5")
    acct._option_data_client = FakeOptClient()
    monkeypatch.setattr(acct, "_get_option_contracts_meta",
                        lambda *a, **k: {"AAPL260116C00150000": occ_meta}, raising=False)  # no ...160000
    chain = acct.get_option_chain("AAPL", date(2026,1,1), date(2026,3,1), OptionRight.CALL)
    assert [c.symbol for c in chain] == ["AAPL260116C00150000"]


def test_parse_occ_symbol_call_and_put():
    acct = _make_alpaca()
    u, e, r, k = acct._parse_occ_symbol("AAPL260116C00150000")
    assert u == "AAPL" and e == date(2026, 1, 16) and r == OptionRight.CALL and k == 150.0
    u2, e2, r2, k2 = acct._parse_occ_symbol("SPY260320P00455500")
    assert u2 == "SPY" and e2 == date(2026, 3, 20) and r2 == OptionRight.PUT and k2 == 455.5


def test_get_option_positions_filters_and_maps():
    from ba2_trade_platform.core.types import OrderDirection
    acct = _make_alpaca()
    equity_pos = SimpleNamespace(symbol="AAPL", asset_class="us_equity", qty="10",
                                 avg_entry_price="150.0", current_price="151.0",
                                 market_value="1510.0", unrealized_pl="10.0", side="long")
    opt_pos = SimpleNamespace(symbol="AAPL260116C00150000", asset_class="us_option", qty="2",
                              avg_entry_price="5.2", current_price="6.0", market_value="1200.0",
                              unrealized_pl="160.0", side="long")
    short_opt = SimpleNamespace(symbol="AAPL260116C00160000", asset_class="us_option", qty="-1",
                                avg_entry_price="2.0", current_price="1.5", market_value="-150.0",
                                unrealized_pl="50.0", side="short")

    class FakeClient:
        def get_all_positions(self):
            return [equity_pos, opt_pos, short_opt]
    acct.client = FakeClient()

    positions = acct.get_option_positions()
    assert len(positions) == 2  # equity filtered out
    by_sym = {p.contract_symbol: p for p in positions}
    long_p = by_sym["AAPL260116C00150000"]
    assert long_p.underlying == "AAPL"
    assert long_p.option_type == OptionRight.CALL
    assert long_p.strike == 150.0
    assert long_p.expiry == date(2026, 1, 16)
    assert long_p.side == OrderDirection.BUY
    assert long_p.quantity == 2
    assert long_p.avg_entry_price == 5.2
    assert long_p.current_price == 6.0
    assert long_p.multiplier == 100
    short_p = by_sym["AAPL260116C00160000"]
    assert short_p.side == OrderDirection.SELL
    assert short_p.quantity == 1  # absolute value


def test_get_option_contracts_meta_paginates(monkeypatch):
    acct = _make_alpaca()
    calls = {"n": 0}
    def mk_contract(sym, strike):
        return SimpleNamespace(symbol=sym, underlying_symbol="AAPL",
            type=SimpleNamespace(value="call"), strike_price=strike,
            expiration_date=date(2026,1,16), open_interest="10")
    class FakeTradingClient:
        def get_option_contracts(self, req):
            calls["n"] += 1
            if getattr(req, "page_token", None) in (None, "",):
                return SimpleNamespace(option_contracts=[mk_contract("AAPL260116C00150000",150.0)],
                                       next_page_token="t2")
            return SimpleNamespace(option_contracts=[mk_contract("AAPL260116C00160000",160.0)],
                                   next_page_token=None)
    acct.client = FakeTradingClient()
    meta = acct._get_option_contracts_meta("AAPL", date(2026,1,1), date(2026,3,1), OptionRight.CALL)
    assert set(meta.keys()) == {"AAPL260116C00150000", "AAPL260116C00160000"}
    assert calls["n"] == 2


import pytest as _pytest

def test_parse_occ_symbol_rejects_invalid_right_char():
    acct = _make_alpaca()
    with _pytest.raises(ValueError):
        acct._parse_occ_symbol("AAPL260116X00150000")


def test_get_option_positions_skips_malformed_and_keeps_valid():
    from ba2_trade_platform.core.types import OrderDirection
    acct = _make_alpaca()
    good = SimpleNamespace(symbol="AAPL260116C00150000", asset_class="us_option", qty="2",
                           avg_entry_price="5.2", current_price="6.0", market_value="1200.0",
                           unrealized_pl="160.0", side="long")
    bad_occ = SimpleNamespace(symbol="GARBAGE", asset_class="us_option", qty="1",
                              avg_entry_price="1.0", current_price="1.0", market_value="1.0",
                              unrealized_pl="0.0", side="long")
    bad_price = SimpleNamespace(symbol="AAPL260116C00170000", asset_class="us_option", qty="1",
                                avg_entry_price=None, current_price="1.0", market_value="1.0",
                                unrealized_pl="0.0", side="long")
    class FakeClient:
        def get_all_positions(self):
            return [good, bad_occ, bad_price]
    acct.client = FakeClient()
    positions = acct.get_option_positions()
    # only the good row survives; the two malformed rows are skipped, not fatal
    assert [p.contract_symbol for p in positions] == ["AAPL260116C00150000"]


def test_get_option_positions_short_with_positive_qty():
    from ba2_trade_platform.core.types import OrderDirection
    acct = _make_alpaca()
    short = SimpleNamespace(symbol="AAPL260116C00160000", asset_class="us_option", qty="1",
                            avg_entry_price="2.0", current_price="1.5", market_value="-150.0",
                            unrealized_pl="50.0", side="short")   # positive qty + side=short
    class FakeClient:
        def get_all_positions(self):
            return [short]
    acct.client = FakeClient()
    positions = acct.get_option_positions()
    assert len(positions) == 1
    assert positions[0].side == OrderDirection.SELL
    assert positions[0].quantity == 1


# ---------------------------------------------------------------------------
# Task 9: option order submission (request building + writeback)
# ---------------------------------------------------------------------------

from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from ba2_trade_platform.core.option_types import OptionLeg
from ba2_trade_platform.core.models import TradingOrder
from ba2_trade_platform.core.types import (
    OrderDirection, OrderType, AssetClass, OrderStatus,
)
from ba2_trade_platform.core.db import add_instance, get_instance


def _opt_order(**kw):
    base = dict(account_id=1, symbol="AAPL", quantity=2, side=OrderDirection.BUY,
                order_type=OrderType.BUY_LIMIT, status=OrderStatus.PENDING, limit_price=5.2,
                asset_class=AssetClass.OPTION, multiplier=100, id=12345)
    base.update(kw)
    return TradingOrder(**base)


def test_build_single_leg_limit_request():
    acct = _make_alpaca()
    order = _opt_order()
    leg = OptionLeg(contract_symbol="AAPL260116C00150000", side=OrderDirection.BUY,
                    position_intent="buy_to_open")
    req = acct._build_option_order_request(order, [leg])
    assert isinstance(req, LimitOrderRequest)
    assert req.symbol == "AAPL260116C00150000"
    assert req.side == OrderSide.BUY
    assert float(req.qty) == 2
    assert float(req.limit_price) == 5.2
    assert req.time_in_force == TimeInForce.DAY
    assert getattr(req, "order_class", None) in (None,)  # not an MLEG
    assert not getattr(req, "legs", None)


def test_build_single_leg_market_request():
    acct = _make_alpaca()
    order = _opt_order(order_type=OrderType.MARKET, limit_price=None)
    leg = OptionLeg(contract_symbol="AAPL260116C00150000", side=OrderDirection.BUY,
                    position_intent="buy_to_open")
    req = acct._build_option_order_request(order, [leg])
    assert isinstance(req, MarketOrderRequest)
    assert req.symbol == "AAPL260116C00150000"
    assert req.time_in_force == TimeInForce.DAY


def test_build_multi_leg_mleg_request():
    acct = _make_alpaca()
    order = _opt_order(quantity=1, limit_price=4.0, side=OrderDirection.BUY,
                       order_type=OrderType.BUY_LIMIT, symbol="AAPL", contract_symbol=None)
    legs = [
        OptionLeg(contract_symbol="AAPL260116C00150000", side=OrderDirection.BUY,
                  position_intent="buy_to_open"),
        OptionLeg(contract_symbol="AAPL260116C00160000", side=OrderDirection.SELL,
                  position_intent="sell_to_open"),
    ]
    req = acct._build_option_order_request(order, legs)
    assert req.order_class == OrderClass.MLEG
    assert float(req.qty) == 1
    assert float(req.limit_price) == 4.0       # positive => debit
    assert getattr(req, "symbol", None) in (None,)   # no top-level symbol on MLEG
    assert len(req.legs) == 2
    syms = {l.symbol for l in req.legs}
    assert syms == {"AAPL260116C00150000", "AAPL260116C00160000"}


def test_submit_option_order_impl_writeback(mock_account_def):
    from types import SimpleNamespace
    import threading
    acct = _make_alpaca()
    acct.id = mock_account_def.id
    acct._balance_cache_lock = threading.RLock()
    acct._balance_cache_time = 0.0
    # persist a parent option order
    parent = _opt_order(account_id=mock_account_def.id, id=None)
    parent.contract_symbol = "AAPL260116C00150000"
    pid = add_instance(parent, expunge_after_flush=True)
    parent = get_instance(TradingOrder, pid)
    leg = OptionLeg(contract_symbol="AAPL260116C00150000", side=OrderDirection.BUY,
                    position_intent="buy_to_open")

    captured = {}
    # Realistic alpaca order shape: the equity-path mapper reads side/type/status.
    fake_order = SimpleNamespace(id="broker-xyz", side="buy", type="limit",
                                 status="filled", qty="2", filled_qty="2", legs=None)

    class FakeClient:
        def submit_order(self, req):
            captured["req"] = req
            return fake_order
    acct.client = FakeClient()

    result = acct._submit_option_order_impl(parent, [leg], None)
    assert result.broker_order_id == "broker-xyz"
    # re-fetch from DB to confirm persistence
    db_parent = get_instance(TradingOrder, pid)
    assert db_parent.broker_order_id == "broker-xyz"
    assert db_parent.status == OrderStatus.FILLED


def test_submit_option_order_impl_multileg_writeback(mock_account_def):
    from types import SimpleNamespace
    import threading
    acct = _make_alpaca()
    acct.id = mock_account_def.id
    acct._balance_cache_lock = threading.RLock()
    acct._balance_cache_time = 0.0

    parent = _opt_order(account_id=mock_account_def.id, id=None, quantity=1,
                        symbol="AAPL", contract_symbol=None, limit_price=4.0)
    pid = add_instance(parent, expunge_after_flush=True)
    parent = get_instance(TradingOrder, pid)

    child_buy = TradingOrder(account_id=mock_account_def.id, symbol="AAPL260116C00150000",
                             quantity=1, side=OrderDirection.BUY, order_type=OrderType.BUY_LIMIT,
                             status=OrderStatus.PENDING, asset_class=AssetClass.OPTION,
                             multiplier=100, contract_symbol="AAPL260116C00150000",
                             position_intent="buy_to_open", parent_order_id=pid)
    child_sell = TradingOrder(account_id=mock_account_def.id, symbol="AAPL260116C00160000",
                              quantity=1, side=OrderDirection.SELL, order_type=OrderType.SELL_LIMIT,
                              status=OrderStatus.PENDING, asset_class=AssetClass.OPTION,
                              multiplier=100, contract_symbol="AAPL260116C00160000",
                              position_intent="sell_to_open", parent_order_id=pid)
    cb_id = add_instance(child_buy, expunge_after_flush=True)
    cs_id = add_instance(child_sell, expunge_after_flush=True)
    child_buy = get_instance(TradingOrder, cb_id)
    child_sell = get_instance(TradingOrder, cs_id)

    legs = [
        OptionLeg(contract_symbol="AAPL260116C00150000", side=OrderDirection.BUY,
                  position_intent="buy_to_open"),
        OptionLeg(contract_symbol="AAPL260116C00160000", side=OrderDirection.SELL,
                  position_intent="sell_to_open"),
    ]
    fake_legs = [SimpleNamespace(id="leg-buy", symbol="AAPL260116C00150000",
                                 side="buy", type="limit", status="accepted",
                                 qty="1", filled_qty="0"),
                 SimpleNamespace(id="leg-sell", symbol="AAPL260116C00160000",
                                 side="sell", type="limit", status="accepted",
                                 qty="1", filled_qty="0")]
    fake_order = SimpleNamespace(id="parent-broker", side="buy", type="limit",
                                 status="accepted", qty="1", filled_qty="0",
                                 legs=fake_legs)

    class FakeClient:
        def submit_order(self, req):
            return fake_order
    acct.client = FakeClient()

    result = acct._submit_option_order_impl(parent, legs, [child_buy, child_sell])
    assert result.broker_order_id == "parent-broker"
    db_parent = get_instance(TradingOrder, pid)
    assert db_parent.broker_order_id == "parent-broker"
    assert db_parent.legs_broker_ids == ["leg-buy", "leg-sell"]
    # children matched by contract_symbol
    db_buy = get_instance(TradingOrder, cb_id)
    db_sell = get_instance(TradingOrder, cs_id)
    assert db_buy.broker_order_id == "leg-buy"
    assert db_sell.broker_order_id == "leg-sell"


# ---------------------------------------------------------------------------
# Task 10: close_option_position
# ---------------------------------------------------------------------------
from ba2_trade_platform.core.option_types import OptionPosition
from ba2_trade_platform.core.types import OrderDirection as _OrderDirection


def test_close_long_option_builds_sell_to_close(monkeypatch):
    acct = _make_alpaca()
    captured = {}
    def fake_submit(legs, quantity, order_type="limit", limit_price=None,
                    option_strategy=None, expert_recommendation_id=None, transaction_id=None):
        captured.update(legs=legs, quantity=quantity, order_type=order_type,
                        limit_price=limit_price, option_strategy=option_strategy)
        return "SUBMITTED"
    monkeypatch.setattr(acct, "submit_option_order", fake_submit, raising=False)

    pos = OptionPosition(contract_symbol="AAPL260116C00150000", underlying="AAPL",
                         option_type=OptionRight.CALL, strike=150.0, expiry=date(2026, 1, 16),
                         side=_OrderDirection.BUY, quantity=2, avg_entry_price=5.2)
    result = acct.close_option_position(pos, order_type="limit", limit_price=6.0)
    assert result == "SUBMITTED"
    assert len(captured["legs"]) == 1
    leg = captured["legs"][0]
    assert leg.contract_symbol == "AAPL260116C00150000"
    assert leg.side == _OrderDirection.SELL                  # opposite of long
    assert leg.position_intent == "sell_to_close"
    assert leg.option_type == OptionRight.CALL
    assert leg.strike == 150.0
    assert leg.expiry == date(2026, 1, 16)
    assert leg.underlying == "AAPL"
    assert captured["quantity"] == 2
    assert captured["order_type"] == "limit"
    assert captured["limit_price"] == 6.0
    assert captured["option_strategy"] == "close"


def test_close_short_option_builds_buy_to_close(monkeypatch):
    acct = _make_alpaca()
    captured = {}
    def fake_submit(legs, quantity, order_type="limit", limit_price=None,
                    option_strategy=None, expert_recommendation_id=None, transaction_id=None):
        captured.update(legs=legs, quantity=quantity)
        return "SUBMITTED"
    monkeypatch.setattr(acct, "submit_option_order", fake_submit, raising=False)

    pos = OptionPosition(contract_symbol="AAPL260116C00160000", underlying="AAPL",
                         option_type=OptionRight.CALL, strike=160.0, expiry=date(2026, 1, 16),
                         side=_OrderDirection.SELL, quantity=1, avg_entry_price=2.0)
    acct.close_option_position(pos)
    leg = captured["legs"][0]
    assert leg.side == _OrderDirection.BUY                   # opposite of short
    assert leg.position_intent == "buy_to_close"
    assert captured["quantity"] == 1
