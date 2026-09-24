"""BT/live option parity C2: ONE entry record, identical in live and backtest but for its sources.

Plan: ``docs/plans/2026-09-22-bt-live-option-parity.md`` Part C (C1 + C2).

The REAL entry builders run against a REAL ``BacktestAccount`` reading a tiny parquet option
store at bar D, and against a REAL ``AlpacaAccount`` reading a raw Alpaca snapshot fixture at
N(D) 09:35 ET (no network: the raw client is a fake). The Alpaca fixture is built FROM the
backtest chain -- same bid/ask/last, same session volume, and the backtest's Black-Scholes
iv/greeks copied into the snapshot's ``greeks`` -- so every source value is identical and the
test isolates the PLUMBING: both paths must write the same ``entry_record`` through the shared
``_submit_option_order``, differing only in ``greeks_source`` (``broker`` vs ``bs_from_close``)
and ``quote_time`` (Alpaca's quote stamp vs None).

Run from testplatform/backend:
    python -m pytest tests/backtest/test_option_entry_record_parity.py -q
"""
from __future__ import annotations

import copy
import json
import os
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import ba2_common
from ba2_common.core.interfaces.OptionsDataProviderInterface import OptionEodBar
from ba2_common.core.option_trade_record import (
    LEG_SNAPSHOT_FIELDS, OPTION_TRADE_RECORD_VERSION,
)
from ba2_common.core.types import OptionRight, OrderRecommendation

from app.services.backtest.parquet_options_provider import (
    ParquetOptionsProvider,
    clear_worker_parquet_options_cache,
)

_UNDER = "ZZ"
_EXP = date(2023, 2, 17)
_SPOT = 100.0
_D = date(2023, 1, 10)             # bar D (Tuesday)
_N_D = date(2023, 1, 11)           # the live session it is
_LIVE_AT = datetime(2023, 1, 11, 14, 35, tzinfo=timezone.utc)   # 09:35 ET on N(D)
_QUOTE_T = "2023-01-11T14:34:59Z"
#: strike -> (bid, ask) on bar D; every contract trades 80 on D and 2 in N(D)'s partial bar.
_BOOK = {95.0: (6.6, 6.8), 100.0: (3.6, 3.8), 105.0: (1.6, 1.75), 110.0: (0.6, 0.7)}


def _occ(strike: float) -> str:
    return f"{_UNDER}230217C{int(round(strike * 1000)):08d}"


def test_worktree_code_is_under_test():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    assert os.path.abspath(ba2_common.__file__).startswith(root)
    import importlib
    alp = importlib.import_module("ba2_trade_platform.modules.accounts.AlpacaAccount")
    assert os.path.abspath(alp.__file__).startswith(root)


@pytest.fixture
def bt_account(tmp_path):
    from ba2_providers.options.parquet_store import OptionHistoryParquetStore
    from app.services.backtest.backtest_account import BacktestAccount

    rows = [OptionEodBar(occ_symbol=_occ(k), bar_date=_D, open=(b + a) / 2, high=a, low=b,
                         close=round((b + a) / 2, 4), volume=80, bid=b, ask=a,
                         open_interest=None)
            for k, (b, a) in _BOOK.items()]
    root = str(tmp_path / "ThetaDataOptionsProvider")
    OptionHistoryParquetStore(root=root).write_partition(
        _UNDER, _EXP, rows, start=date(2023, 1, 1), end=date(2023, 3, 31))
    clear_worker_parquet_options_cache()
    provider = ParquetOptionsProvider(root, spot_source=lambda u, on: _SPOT,
                                      risk_free_rate=0.045, spot_scope="test")
    acct = BacktestAccount.__new__(BacktestAccount)
    acct.id = 9101
    acct._options = provider
    acct._price = SimpleNamespace(now=lambda: datetime(2023, 1, 10, 16, 0))
    acct.get_instrument_current_price = lambda symbol, price_type=None: _SPOT
    yield acct
    clear_worker_parquet_options_cache()


def _bar(day: date, volume: int) -> dict:
    return {"t": f"{day.isoformat()}T05:00:00Z", "o": 3.0, "h": 3.2, "l": 2.9, "c": 3.1,
            "v": volume, "n": 5, "vw": 3.1}


@pytest.fixture
def live_account(monkeypatch, bt_account):
    """An AlpacaAccount whose raw snapshots carry the BACKTEST chain's own values."""
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount

    bt_chain = bt_account.get_option_chain(_UNDER, date(2023, 2, 1), date(2023, 3, 1),
                                           OptionRight.CALL)
    assert {c.symbol for c in bt_chain} == {_occ(k) for k in _BOOK}
    snapshots = {}
    for c in bt_chain:
        assert c.delta is not None and c.rho is not None     # BS greeks really computed
        snapshots[c.symbol] = {
            "latestQuote": {"t": _QUOTE_T, "bp": c.bid, "ap": c.ask, "bs": 10, "as": 10},
            "latestTrade": {"t": "2023-01-11T14:30:00Z", "p": c.last, "s": 1},
            "greeks": {"delta": c.delta, "gamma": c.gamma, "theta": c.theta,
                       "vega": c.vega, "rho": c.rho},
            "impliedVolatility": c.implied_volatility,
            "dailyBar": _bar(_N_D, 2), "prevDailyBar": _bar(_D, 80),
        }
    metas = {_occ(k): SimpleNamespace(symbol=_occ(k), underlying_symbol=_UNDER,
                                      root_symbol=_UNDER, type=SimpleNamespace(value="call"),
                                      strike_price=k, expiration_date=_EXP,
                                      open_interest=None, size="100")
             for k in _BOOK}

    class FakeRawClient:
        def get_option_chain(self, request):
            return snapshots

    acct = AlpacaAccount.__new__(AlpacaAccount)
    acct.id = 9102
    acct._settings_cache = {"api_key": "k", "api_secret": "s"}
    acct._option_data_client_raw = FakeRawClient()
    monkeypatch.setattr(acct, "_get_option_contracts_meta", lambda *a, **k: metas,
                        raising=False)
    monkeypatch.setattr(acct, "get_instrument_current_price",
                        lambda symbol, price_type=None: _SPOT, raising=False)
    from ba2_common.core import option_session
    monkeypatch.setattr(option_session, "live_decision_time", lambda: _LIVE_AT)
    return acct


def _rec():
    # A TradeActionResult row needs a recommendation id; nothing else of it is read here.
    return SimpleNamespace(id=1, instance_id=None, data=None, price_at_date=None,
                           expected_profit_percent=None, recommended_action=None)


def _long_call(account):
    from ba2_common.core.TradeActions import BuyCallAction
    return BuyCallAction(_UNDER, account, OrderRecommendation.BUY,
                         expert_recommendation=_rec(), strike_method="percent_otm", strike_param=5.0,
                         dte_min=20, dte_max=60, sizing=5.0)


def _bull_call_spread(account):
    from ba2_common.core.TradeActions import OpenBullCallSpreadAction
    return OpenBullCallSpreadAction(_UNDER, account, OrderRecommendation.BUY,
                                    expert_recommendation=_rec(), strike_method="percent_otm", strike_param=[0.0, 10.0],
                                    dte_min=20, dte_max=60, sizing=5.0)


def _record(action):
    """Resolve with the real builder, then run the SHARED submit path in preview mode."""
    resolved = action._resolve()
    assert not isinstance(resolved, dict), resolved
    action.submit_to_broker = False
    res = action._submit_option_order(resolved.legs, 2, resolved.limit_price,
                                      resolved.option_strategy)
    assert res["success"], res["message"]
    return res["data"]["entry_record"]


def _strip(rec):
    out = copy.deepcopy(rec)
    for leg in out["legs"]:
        leg.pop("greeks_source")
        leg.pop("quote_time")
    return out


def test_production_accounts_declare_their_greeks_source():
    from ba2_trade_platform.modules.accounts.AlpacaAccount import AlpacaAccount
    from app.services.backtest.backtest_account import BacktestAccount
    assert AlpacaAccount.OPTION_GREEKS_SOURCE == "broker"
    assert BacktestAccount.OPTION_GREEKS_SOURCE == "bs_from_close"


@pytest.mark.parametrize("builder,n_legs", [(_long_call, 1), (_bull_call_spread, 2)],
                         ids=["long_call", "bull_call_spread"])
def test_live_and_backtest_write_the_same_entry_record(bt_account, live_account,
                                                       builder, n_legs):
    bt = _record(builder(bt_account))
    live = _record(builder(live_account))

    for rec in (bt, live):
        assert rec["version"] == OPTION_TRADE_RECORD_VERSION
        assert rec["legs_without_quote"] == []
        assert len(rec["legs"]) == n_legs
        for leg in rec["legs"]:
            assert tuple(leg) == LEG_SNAPSHOT_FIELDS
            assert leg["data_session"] == _D.isoformat()
            assert leg["dte"] == (_EXP - _N_D).days
            assert leg["spot"] == _SPOT
            assert leg["volume"] == 80
        json.dumps(rec, allow_nan=False)

    # THE PARITY: identical keys, identical values, but for the two provenance fields.
    assert _strip(bt) == _strip(live)
    assert {l["greeks_source"] for l in bt["legs"]} == {"bs_from_close"}
    assert {l["greeks_source"] for l in live["legs"]} == {"broker"}
    assert {l["quote_time"] for l in bt["legs"]} == {None}
    assert {l["quote_time"] for l in live["legs"]} == {"2023-01-11T14:34:59+00:00"}


def test_the_backtest_order_row_keeps_the_record_in_its_data(bt_account):
    """Where Task 8 (C4) will read it: the parent TradingOrder's ``data``. The shared path
    merges it there through the same entry-facts write every other stamp uses."""
    from ba2_common.core.db import add_instance, get_instance
    from ba2_common.core.models import TradingOrder
    from ba2_common.core.types import OrderDirection, OrderStatus, OrderType

    row_id = add_instance(TradingOrder(account_id=bt_account.id, symbol=_UNDER, quantity=1,
                                       side=OrderDirection.BUY, order_type=OrderType.MARKET,
                                       status=OrderStatus.PENDING))
    bt_account.submit_option_order = lambda **kw: SimpleNamespace(id=row_id,
                                                                  transaction_id=None)
    action = _long_call(bt_account)
    resolved = action._resolve()
    res = action._submit_option_order(resolved.legs, 1, resolved.limit_price,
                                      resolved.option_strategy)
    assert res["success"], res["message"]
    stored = get_instance(TradingOrder, row_id)
    assert stored.data["entry_record"] == res["data"]["entry_record"]
    assert stored.data["entry_record"]["legs"][0]["greeks_source"] == "bs_from_close"


def test_a_real_engine_run_keeps_the_record_on_the_entry_order():
    """End to end through ``DailyBacktestEngine.run()`` (the O_LEAP golden fixture, in-memory
    trade store): the record survives on the entry order that ``get_orders`` -- the source
    ``get_round_trip_trades`` groups -- returns at run end."""
    from tests.backtest import test_option_golden_run as G
    from tests.backtest.test_grid2_engine_paths import (
        _PlainBuyExpert, _harness, _launcher, _leap_rules)

    entry_rules, exit_rules = _leap_rules(_launcher(), dte_floor=G._DTE_FLOOR)
    engine, account, ctx = _harness(
        symbol=G._SYMBOL, underlying_rows=G._underlying_rows(), chain_rows=G._chain_rows(),
        bar_rows=G._bar_rows(), entry_rules=entry_rules, exit_rules=exit_rules,
        entry_action=entry_rules[0]["actions"][0],
        expert_factory=lambda eid: _PlainBuyExpert(eid, G._SYMBOL),
        start=G._START, end=G._END, account_id=G._ACCOUNT_ID)
    try:
        engine.run()
        stamped = [o for o in account.get_orders() if "entry_record" in (o.data or {})]
        assert stamped, "no order carries an entry_record after a real option run"
        rec = stamped[0].data["entry_record"]
        assert rec["version"] == OPTION_TRADE_RECORD_VERSION
        assert [leg["contract_symbol"] for leg in rec["legs"]] == [G._CONTRACT]
        assert rec["legs"][0]["greeks_source"] == "bs_from_close"
        assert rec["legs"][0]["quote_time"] is None
        assert account.get_round_trip_trades()
        json.dumps(rec, allow_nan=False)
    finally:
        ctx.__exit__(None, None, None)


def test_an_equity_run_writes_no_entry_record(monkeypatch):
    """The record belongs to the option submit path only: the equity golden run's orders
    (entries, TP/SL brackets, exits) carry none."""
    from tests.backtest import test_equity_golden_run as E

    seen = {}

    def capture(account):
        seen["orders"] = list(account.get_orders())
        seen["data"] = [dict(o.data or {}) for o in seen["orders"]]
        return {}

    monkeypatch.setattr(E, "fingerprint", capture)
    E.run_golden_backtest()
    assert seen["orders"], "the equity golden placed no orders"
    assert not any("entry_record" in d for d in seen["data"])


def test_greeks_source_is_stated_per_row():
    """Live rows say "broker"; parquet rows "bs_from_close"; a sqlite-store row says which of
    its two branches it took -- the bar's BS inversion, or the build-time chain snapshot when
    the bar has no computed iv."""
    from app.services.backtest.options_provider import _to_contract

    chain_row = {"occ_symbol": _occ(100.0), "underlying": _UNDER, "option_type": "call",
                 "strike": 100.0, "expiry": _EXP.isoformat(), "bid": 3.6, "ask": 3.8,
                 "last": 3.7, "iv": 0.5, "delta": 0.55, "open_interest": None}
    bar = {"close": 3.7, "iv": 0.41, "delta": 0.52, "gamma": 0.04, "theta": -0.02,
           "vega": 0.1}
    assert _to_contract(chain_row, bar, 80).greeks_source == "bs_from_close"
    assert _to_contract(chain_row, dict(bar, iv=None), 80).greeks_source == "chain_snapshot"
    assert _to_contract(chain_row, None, 0).greeks_source == "chain_snapshot"


def test_chain_rows_carry_their_source_on_both_paths(bt_account, live_account):
    window = (date(2023, 2, 1), date(2023, 3, 1))
    assert {c.greeks_source for c in bt_account.get_option_chain(
        _UNDER, *window, OptionRight.CALL)} == {"bs_from_close"}
    assert {c.greeks_source for c in live_account.get_option_chain(
        _UNDER, *window, OptionRight.CALL)} == {"broker"}


def test_every_production_options_account_declares_its_greeks_source():
    """Every concrete OptionsAccountInterface subclass shipped in the live tree or the
    backtest declares a source. QuoteCachingAccount (the Options-tab quote cache) is a
    display wrapper that FORWARDS every attribute to the account it wraps, so it reports
    that account's declaration rather than one of its own."""
    import importlib
    import inspect

    from ba2_common.core.interfaces.OptionsAccountInterface import OptionsAccountInterface
    for mod in ("ba2_trade_platform.modules.accounts.AlpacaAccount",
                "ba2_trade_platform.modules.accounts.TastyTradeAccount",
                "ba2_trade_platform.modules.accounts.IBKRAccount",
                "ba2_trade_platform.core.option_pnl_display",
                "app.services.backtest.backtest_account"):
        importlib.import_module(mod)

    def walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from walk(sub)

    production = {c for c in walk(OptionsAccountInterface)
                  if c.__module__.startswith(("ba2_trade_platform.", "app."))
                  and not inspect.isabstract(c)}
    names = {c.__name__ for c in production}
    assert {"AlpacaAccount", "BacktestAccount", "QuoteCachingAccount"} <= names, names
    from ba2_trade_platform.core.option_pnl_display import QuoteCachingAccount
    for cls in production - {QuoteCachingAccount}:
        assert cls.OPTION_GREEKS_SOURCE, f"{cls.__module__}.{cls.__name__} declares none"
    wrapped = QuoteCachingAccount(SimpleNamespace(OPTION_GREEKS_SOURCE="broker"), {}, 1)
    assert wrapped.OPTION_GREEKS_SOURCE == "broker"


def test_live_record_with_a_missing_greek_and_no_quote_timestamp(bt_account, live_account):
    """A live snapshot that omits a greek and stamps no quote time: the record keeps both
    UNKNOWN (None), never 0 and never an invented instant."""
    for snap in live_account._option_data_client_raw.get_option_chain(None).values():
        snap["greeks"]["delta"] = None
        snap["latestQuote"].pop("t")
    rec = _record(_long_call(live_account))
    leg = rec["legs"][0]
    assert leg["delta"] is None and leg["quote_time"] is None
    assert leg["gamma"] is not None and leg["greeks_source"] == "broker"
