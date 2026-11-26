"""Overview "Heatmap" tab: the loader (open transactions -> lots, the account's prices, history, market caps, splits,
the broker's quantities).

DB rows go through the in-memory test DB (conftest); brokers, the history source and FMP are fakes. Nothing here
reaches a network."""
import threading
import time
from datetime import date, datetime, timezone

import pytest

from ba2_trade_platform.core.db import add_instance
from ba2_trade_platform.core.models import Instrument, Transaction
from ba2_trade_platform.core.types import AssetClass, OrderDirection, OrderStatus, OrderType, TransactionStatus
from ba2_trade_platform.services import position_heatmap as svc
from ba2_trade_platform.services.errors import PriceSourceUnreachable
from ba2_trade_platform.ui.utils.position_heatmap import NO_EXPERT
from tests.factories import create_account_definition, create_expert_instance, create_trading_order

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)        # Tuesday, 10:00 ET


class Broker:
    def __init__(self, positions, feed=None):
        self.positions = positions
        self.feed = feed or {}
        self.position_reads = 0
        self.feed_calls = 0
        self.raise_on_positions = None
        self.hang = None

    def get_positions(self):
        self.position_reads += 1
        if self.hang is not None:
            self.hang.wait(10)
        if self.raise_on_positions:
            raise self.raise_on_positions
        return self.positions

    def get_instrument_current_price(self, symbols, price_type=None):
        self.feed_calls += 1
        return {s: self.feed[s] for s in symbols if s in self.feed}


def _utc(y, m, d, hh=15):
    return datetime(y, m, d, hh, 0, tzinfo=timezone.utc)


def _open(account_id, symbol, qty=10.0, price=100.0, side=OrderDirection.BUY, expert_id=None,
          created=_utc(2026, 10, 1), asset_class=AssetClass.EQUITY, status=TransactionStatus.OPENED,
          order_type=OrderType.MARKET, with_order=True):
    # opened when its order filled (the factory's open_date is "now")
    tx = Transaction(symbol=symbol, quantity=qty, side=side, status=status, open_price=price, open_date=created,
                     expert_id=expert_id, asset_class=asset_class)
    add_instance(tx, expunge_after_flush=True)
    if with_order:
        create_trading_order(account_id, symbol=symbol, quantity=qty, side=side, transaction_id=tx.id,
                             status=OrderStatus.FILLED, filled_qty=qty, open_price=price, created_at=created,
                             asset_class=asset_class, order_type=order_type)
    return tx


@pytest.fixture
def world(monkeypatch):
    from ba2_trade_platform.services import _positions_cache
    _positions_cache.clear()
    svc.clear_caches()
    a1 = create_account_definition(name="One")
    a2 = create_account_definition(name="Two")
    brokers = {a1.id: Broker([{"symbol": "AAPL", "current_price": 110.0, "qty": 10.0, "asset_class": "us_equity"},
                              {"symbol": "AAPL250117C00100000", "current_price": 2.0, "qty": 1.0,
                               "asset_class": "us_option"}], feed={"MSFT": 305.0}),
               a2.id: Broker([{"symbol": "XOM", "current_price": 50.0, "qty": 7.0}])}
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda i: brokers.get(i))
    monkeypatch.setattr(svc, "_history_source", lambda account_id: "fmp")
    history_calls = []

    def fake_closes(by_account, start, *, deadline_s=None, errors=None):
        history_calls.append((dict(by_account), start))
        known = {"AAPL": {"2026-11-23": 100.0}, "XOM": {"2026-11-23": 55.0}, "MSFT": {"2026-11-23": 300.0}}
        out = svc.price_history.Closes({s: known[s] for syms in by_account.values() for s in syms if s in known})
        out.unfetched = []
        return out

    monkeypatch.setattr(svc.price_history, "fetch_closes", fake_closes)
    caps = {"AAPL": 3.5e12, "XOM": 4.5e11}
    monkeypatch.setattr(svc, "fetch_market_caps", lambda symbols, **kw: (
        {s: caps[s] for s in symbols if s in caps}, {s: "FMP returned no market cap" for s in symbols if s not in caps}))
    monkeypatch.setattr(svc, "fetch_split_days", lambda symbols, **kw: ({"AAPL": [date(2026, 11, 2)]}, {}))
    return a1, a2, brokers, history_calls


def test_open_equity_transactions_become_lots_with_their_expert_and_account(world):
    a1, *_ = world
    ex = create_expert_instance(a1.id, alias="Alpha")
    _open(a1.id, "AAPL", expert_id=ex.id)
    _open(a1.id, "MSFT", qty=2.0, price=300.0)
    data = svc.load_heatmap_data(a1.id, NOW)
    by_sym = {l.symbol: l for l in data.lots}
    assert by_sym["AAPL"].expert == f"Alpha-{ex.id}" and by_sym["AAPL"].account_id == a1.id
    assert by_sym["MSFT"].expert == NO_EXPERT
    assert by_sym["AAPL"].direction == "long" and by_sym["AAPL"].quantity == 10.0


def test_closed_and_waiting_transactions_are_not_lots(world):
    a1, *_ = world
    _open(a1.id, "AAPL", status=TransactionStatus.CLOSED)
    _open(a1.id, "MSFT", status=TransactionStatus.WAITING)
    assert svc.load_heatmap_data(a1.id, NOW).lots == []


def test_selected_account_scopes_the_lots(world):
    a1, a2, *_ = world
    _open(a1.id, "AAPL")
    _open(a2.id, "XOM")
    assert [l.symbol for l in svc.load_heatmap_data(a2.id, NOW).lots] == ["XOM"]
    assert sorted(l.symbol for l in svc.load_heatmap_data(None, NOW).lots) == ["AAPL", "XOM"]


def test_current_price_is_the_broker_position_then_the_account_feed(world):
    a1, _a2, brokers, _h = world
    _open(a1.id, "AAPL")
    _open(a1.id, "MSFT")
    _open(a1.id, "NOPE")
    data = svc.load_heatmap_data(a1.id, NOW)
    assert data.market.prices == {"AAPL": 110.0, "MSFT": 305.0}
    assert "NOPE" in data.market.price_missing
    assert brokers[a1.id].position_reads == 1 and brokers[a1.id].feed_calls == 1


def test_broker_quantities_are_read_from_the_same_positions_read_options_skipped(world):
    a1, *_ = world
    _open(a1.id, "AAPL")
    data = svc.load_heatmap_data(a1.id, NOW)
    assert data.market.broker_qty == {a1.id: {"AAPL": 10.0}}


# ---- I2: the positions read --------------------------------------------------------------------------------

def test_positions_read_returning_none_costs_one_call_and_falls_back_to_the_feed(world):
    a1, _a2, brokers, _h = world
    brokers[a1.id].positions = None
    brokers[a1.id].feed = {"AAPL": 111.0}
    _open(a1.id, "AAPL")
    data = svc.load_heatmap_data(a1.id, NOW)
    assert brokers[a1.id].position_reads == 1
    assert data.market.prices == {"AAPL": 111.0}
    assert a1.id not in data.market.broker_qty                 # nothing to compare against


def test_positions_read_raising_falls_back_to_the_feed(world):
    a1, _a2, brokers, _h = world
    brokers[a1.id].raise_on_positions = ConnectionError("gateway down")
    brokers[a1.id].feed = {"AAPL": 112.0}
    _open(a1.id, "AAPL")
    data = svc.load_heatmap_data(a1.id, NOW)
    assert data.market.prices == {"AAPL": 112.0}
    assert brokers[a1.id].position_reads == 1


# ---- C2: one bad account, a hung broker ---------------------------------------------------------------------

def test_one_failing_history_account_leaves_the_other_accounts_closes(world, monkeypatch):
    a1, a2, _b, _h = world
    _open(a1.id, "AAPL")
    _open(a2.id, "XOM")
    real = svc.price_history.fetch_closes

    def picky(by_account, start, **kw):
        if a1.id in by_account:
            raise ValueError("ohlcv_history_source 'alpaca' needs Alpaca market-data keys")
        return real(by_account, start, **kw)

    monkeypatch.setattr(svc.price_history, "fetch_closes", picky)
    data = svc.load_heatmap_data(None, NOW)
    assert "XOM" in data.market.closes
    assert "AAPL" not in data.market.closes and "Alpaca market-data keys" in data.market.closes_missing["AAPL"]


def test_a_history_source_that_cannot_be_resolved_marks_only_that_account(world, monkeypatch):
    a1, a2, _b, _h = world
    _open(a1.id, "AAPL")
    _open(a2.id, "XOM")

    def source(account_id):
        if account_id == a1.id:
            raise ValueError("unknown history source")
        return "fmp"

    monkeypatch.setattr(svc, "_history_source", source)
    data = svc.load_heatmap_data(None, NOW)
    assert "XOM" in data.market.closes and "unknown history source" in data.market.closes_missing["AAPL"]


def test_a_hung_broker_times_out_only_its_own_symbols(world, monkeypatch):
    a1, a2, brokers, _h = world
    monkeypatch.setattr(svc, "BROKER_DEADLINE_S", 0.3)
    release = threading.Event()
    brokers[a1.id].hang = release
    _open(a1.id, "AAPL")
    _open(a2.id, "XOM")
    try:
        began = time.monotonic()
        data = svc.load_heatmap_data(None, NOW)
        assert time.monotonic() - began < 5
    finally:
        release.set()
    assert data.market.prices == {"XOM": 50.0}
    assert "timed out" in data.market.price_missing["AAPL"]


def test_the_history_source_down_marks_its_symbols_with_the_reason(world, monkeypatch):
    a1, *_ = world
    _open(a1.id, "AAPL")

    def down(*a, **k):
        raise PriceSourceUnreachable("The price source failed for every symbol", {})

    monkeypatch.setattr(svc.price_history, "fetch_closes", down)
    data = svc.load_heatmap_data(a1.id, NOW)
    assert data.market.closes == {}
    assert "failed for every symbol" in data.market.closes_missing["AAPL"]


# ---- I1: the closes cache ------------------------------------------------------------------------------------

def test_reference_closes_are_cached_between_loads(world):
    a1, _a2, _b, history_calls = world
    _open(a1.id, "AAPL")
    svc.load_heatmap_data(a1.id, NOW)
    data = svc.load_heatmap_data(a1.id, NOW)
    assert len(history_calls) == 1
    assert data.market.closes["AAPL"]["2026-11-23"] == 100.0
    assert data.calls["history_symbols"] == 0


def test_cached_closes_expire_when_a_session_closes(world):
    a1, _a2, _b, history_calls = world
    _open(a1.id, "AAPL")
    svc.load_heatmap_data(a1.id, NOW)
    svc.load_heatmap_data(a1.id, datetime(2026, 11, 24, 21, 30, tzinfo=timezone.utc))   # after the 16:00 ET close
    assert len(history_calls) == 2


def test_history_is_fetched_from_the_earliest_reference_close(world):
    a1, _a2, _b, history_calls = world
    _open(a1.id, "AAPL")
    svc.load_heatmap_data(a1.id, NOW)
    by_account, start = history_calls[0]
    assert start == date(2025, 12, 31)
    assert by_account == {a1.id: ["AAPL"]}


# ---- I5: transactions without an order ----------------------------------------------------------------------

def test_a_transaction_without_an_order_is_priced_through_its_experts_account(world):
    a1, *_ = world
    ex = create_expert_instance(a1.id, alias="Alpha")
    _open(a1.id, "AAPL", expert_id=ex.id, with_order=False)
    data = svc.load_heatmap_data(a1.id, NOW)              # also in the single-account scope
    assert [l.account_id for l in data.lots] == [a1.id]
    assert data.market.prices == {"AAPL": 110.0}


def test_a_transaction_with_no_order_and_no_expert_is_listed_with_the_reason(world):
    a1, *_ = world
    _open(a1.id, "AAPL", with_order=False)
    data = svc.load_heatmap_data(None, NOW)
    assert data.lots[0].account_id is None
    assert "account is unknown" in data.market.price_missing["AAPL"]


# ---- fills ---------------------------------------------------------------------------------------------------

def test_fills_carry_side_quantity_price_order_time_and_kind(world):
    a1, *_ = world
    tx = _open(a1.id, "AAPL", qty=10.0, created=_utc(2026, 10, 1))
    create_trading_order(a1.id, symbol="AAPL", quantity=3.0, side=OrderDirection.SELL, transaction_id=tx.id,
                         status=OrderStatus.FILLED, filled_qty=3.0, open_price=108.0, order_type=OrderType.SELL_STOP,
                         created_at=_utc(2026, 11, 18))
    create_trading_order(a1.id, symbol="AAPL", quantity=5.0, side=OrderDirection.SELL, transaction_id=tx.id,
                         status=OrderStatus.PENDING, created_at=_utc(2026, 11, 20))      # not a fill
    lot = svc.load_heatmap_data(a1.id, NOW).lots[0]
    assert [(f.side, f.qty, f.price, f.placed, f.is_market) for f in lot.fills] == [
        ("BUY", 10.0, 100.0, _utc(2026, 10, 1), True), ("SELL", 3.0, 108.0, _utc(2026, 11, 18), False)]


def test_the_entry_fill_is_known_to_have_happened_by_the_transaction_open_date(world):
    a1, *_ = world
    tx = Transaction(symbol="AAPL", quantity=10.0, side=OrderDirection.BUY, status=TransactionStatus.OPENED,
                     open_price=100.0, open_date=_utc(2026, 11, 23))
    add_instance(tx, expunge_after_flush=True)
    create_trading_order(a1.id, symbol="AAPL", quantity=10.0, transaction_id=tx.id, status=OrderStatus.FILLED,
                         filled_qty=10.0, open_price=100.0, created_at=_utc(2026, 11, 20),
                         order_type=OrderType.BUY_LIMIT)
    f = svc.load_heatmap_data(a1.id, NOW).lots[0].fills[0]
    assert f.is_market is False and f.seen_by == _utc(2026, 11, 23)


def test_a_cancelled_order_that_partly_filled_counts_its_fill(world):
    a1, *_ = world
    tx = _open(a1.id, "AAPL")
    create_trading_order(a1.id, symbol="AAPL", quantity=5.0, side=OrderDirection.SELL, transaction_id=tx.id,
                         status=OrderStatus.CANCELED, filled_qty=2.0, open_price=107.0, created_at=_utc(2026, 11, 24))
    lot = svc.load_heatmap_data(a1.id, NOW).lots[0]
    assert ("SELL", 2.0) in [(f.side, f.qty) for f in lot.fills]


def test_option_transactions_are_lots_marked_as_options_and_not_priced(world):
    a1, _a2, _b, history_calls = world
    _open(a1.id, "AAPL", asset_class=AssetClass.OPTION, qty=1.0, price=2.5)
    data = svc.load_heatmap_data(a1.id, NOW)
    assert [l.is_option for l in data.lots] == [True]
    assert history_calls == []


def test_labels_splits_and_market_caps_are_passed_through(world):
    a1, *_ = world
    add_instance(Instrument(name="AAPL", labels=["tech", "core"]))
    _open(a1.id, "AAPL")
    _open(a1.id, "MSFT")
    data = svc.load_heatmap_data(a1.id, NOW)
    assert sorted(data.market.labels["AAPL"]) == ["core", "tech"]
    assert data.market.splits == {"AAPL": [date(2026, 11, 2)]}
    assert data.market.market_caps == {"AAPL": 3.5e12}
    assert data.market.market_caps_missing["MSFT"] == "FMP returned no market cap"


def test_load_reports_what_it_cost(world):
    a1, *_ = world
    _open(a1.id, "AAPL")
    _open(a1.id, "MSFT")
    data = svc.load_heatmap_data(a1.id, NOW)
    assert data.calls["positions"] == 1 and data.calls["feed_price_calls"] == 1
    assert data.calls["history_symbols"] == 2


# ---- I6: one load per scope at a time ---------------------------------------------------------------------------

def test_concurrent_loads_of_the_same_scope_share_one_load(world, monkeypatch):
    a1, *_ = world
    gate = threading.Event()
    runs = []

    def slow(account_id, now, timing):
        runs.append(account_id)
        gate.wait(5)
        return "the data"

    monkeypatch.setattr(svc, "_load", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(svc.load_heatmap_data(a1.id, NOW)))
               for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)
    assert runs == [a1.id] and results == ["the data", "the data"]


# ---- market caps (FMP /quote, batched, cached a day) ---------------------------------------------------------

@pytest.fixture
def fresh_fmp_cache(monkeypatch):
    from ba2_providers import fmp_common
    monkeypatch.setattr(fmp_common, "_LIVE_BULK_CACHES", {})
    monkeypatch.setattr(svc, "get_app_setting", lambda key: "k")
    yield


def test_market_caps_are_batched_and_cached_for_a_day(fresh_fmp_cache):
    calls = []

    def quote(api_key, symbols):
        calls.append(list(symbols))
        return [{"symbol": s, "marketCap": 1e9 * (i + 1)} for i, s in enumerate(symbols)]

    caps, missing = svc.fetch_market_caps(["AAPL", "BRK.B"], quote_fn=quote)
    assert caps == {"AAPL": 1e9, "BRK.B": 2e9} and missing == {}
    assert calls == [["AAPL", "BRK-B"]]
    caps2, _ = svc.fetch_market_caps(["AAPL", "BRK.B", "MSFT"], quote_fn=quote)
    assert calls[1] == ["MSFT"]
    assert caps2["AAPL"] == 1e9


def test_market_cap_zero_absent_or_non_finite_is_missing_not_a_number(fresh_fmp_cache):
    caps, missing = svc.fetch_market_caps(
        ["SPY", "GONE", "INF"], quote_fn=lambda k, s: [{"symbol": "SPY", "marketCap": 0},
                                                       {"symbol": "INF", "marketCap": float("inf")}])
    assert caps == {}
    assert set(missing) == {"SPY", "GONE", "INF"}


def test_market_cap_chunk_failure_is_reported_per_symbol(fresh_fmp_cache):
    def boom(api_key, symbols):
        raise ConnectionError("down")

    caps, missing = svc.fetch_market_caps(["AAPL"], quote_fn=boom)
    assert caps == {} and "ConnectionError" in missing["AAPL"]


def test_market_caps_without_an_fmp_key_say_so(monkeypatch):
    monkeypatch.setattr(svc, "get_app_setting", lambda key: None)
    caps, missing = svc.fetch_market_caps(["AAPL"], quote_fn=lambda k, s: pytest.fail("must not call"))
    assert caps == {} and "FMP_API_KEY" in missing["AAPL"]


def test_split_days_come_from_the_fmp_split_calendar(fresh_fmp_cache, monkeypatch):
    from ba2_providers import symbol_info
    payload = {"historical": [{"date": "2026-11-02", "numerator": 2, "denominator": 1}]}
    monkeypatch.setattr(symbol_info, "fetch_splits", lambda key, sym: payload if sym == "AAPL" else {"historical": []})
    splits, missing = svc.fetch_split_days(["AAPL", "MSFT"])
    assert splits == {"AAPL": [date(2026, 11, 2)], "MSFT": []} and missing == {}


def test_split_calendar_failure_is_reported_per_symbol(fresh_fmp_cache, monkeypatch):
    from ba2_providers import symbol_info

    def boom(key, sym):
        raise ConnectionError("down")

    monkeypatch.setattr(symbol_info, "fetch_splits", boom)
    splits, missing = svc.fetch_split_days(["AAPL"])
    assert splits == {} and "ConnectionError" in missing["AAPL"]


def test_the_fmp_quote_helpers_used_here_are_public():
    from ba2_providers.fmp_common import FMPError
    from ba2_trade_platform.core import fmp_live_price
    assert fmp_live_price.to_fmp_symbol("brk.b") == "BRK-B"
    rows = fmp_live_price.fetch_fmp_quote_rows("k", ["AAPL"], quote_fn=lambda k, s: [{"symbol": "AAPL"}])
    assert rows == [{"symbol": "AAPL"}]
    with pytest.raises(FMPError):
        fmp_live_price.fetch_fmp_quote_rows("k", ["AAPL"], quote_fn=lambda k, s: {"Error Message": "Limit Reach"})
