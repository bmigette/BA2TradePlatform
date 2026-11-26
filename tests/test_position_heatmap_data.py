"""Overview "Heatmap" tab: the loader (open transactions -> lots, the account's prices, history, market caps, splits,
the broker's quantities).

DB rows go through the in-memory test DB (conftest); brokers, the history source and FMP are fakes. Nothing here
reaches a network."""
import threading
import time
from datetime import date, datetime, timedelta, timezone

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


def _reference_sessions():
    """The four reference sessions at NOW (today, week, month, year), oldest first."""
    from ba2_trade_platform.ui.utils import position_heatmap as hm
    return sorted({hm.reference_session(tf, NOW) for tf in hm.TIMEFRAMES})


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

    def fake_closes(by_account, start, *, deadline_s=None, errors=None, end=None):
        history_calls.append((dict(by_account), start))
        level = {"AAPL": 100.0, "XOM": 55.0, "MSFT": 300.0}       # every reference close a timeframe needs
        known = {s: {d.isoformat(): px for d in _reference_sessions()} for s, px in level.items()}
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
    _open(a1.id, "ZZZ", with_order=False)                # no broker holds it, so nothing else prices it
    data = svc.load_heatmap_data(None, NOW)
    assert data.lots[0].account_id is None
    assert "account is unknown" in data.market.price_missing["ZZZ"]


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
    a1, _a2, brokers, history_calls = world
    brokers[a1.id].positions = [p for p in brokers[a1.id].positions if p["asset_class"] == "us_option"]
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

    def slow(account_id, now, timing, progress=None):
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


# ---- the first load of the day: split calendar, step order, timing, progress ------------------------------------

def _split_payload(symbol):
    return {"symbol": symbol, "historical": [{"date": "2026-11-02", "numerator": 2, "denominator": 1}]}


def test_a_cold_split_calendar_for_150_symbols_finishes_inside_its_budget(fresh_fmp_cache, monkeypatch):
    from ba2_providers import symbol_info

    def slow(key, sym):
        time.sleep(0.3)                       # a cold FMP call
        return _split_payload(sym)

    monkeypatch.setattr(symbol_info, "fetch_splits", slow)
    symbols = [f"S{i:03d}" for i in range(150)]
    calls = {"split_calendar_reads": 0, "split_memo_hits": 0, "split_timed_out": 0}
    began = time.monotonic()
    splits, missing = svc.fetch_split_days(symbols, calls=calls)        # the real budget (20 s), the shared cap (4)
    assert time.monotonic() - began < svc.MARKET_DATA_DEADLINE_S        # one at a time this would take 45 s
    assert missing == {} and len(splits) == 150 and splits["S000"] == [date(2026, 11, 2)]
    assert calls["split_calendar_reads"] == 150 and calls["split_timed_out"] == 0


def test_a_split_calendar_too_slow_to_finish_keeps_what_arrived(fresh_fmp_cache, monkeypatch):
    from ba2_providers import symbol_info
    release = threading.Event()

    def mixed(key, sym):
        if sym.startswith("SLOW"):
            release.wait(5)
        return _split_payload(sym)

    monkeypatch.setattr(symbol_info, "fetch_splits", mixed)
    calls = {"split_calendar_reads": 0, "split_memo_hits": 0, "split_timed_out": 0}
    try:
        splits, missing = svc.fetch_split_days(["FAST1", "FAST2", "SLOW1", "SLOW2"], calls=calls, deadline_s=0.5)
    finally:
        release.set()
    assert set(splits) == {"FAST1", "FAST2"}
    assert set(missing) == {"SLOW1", "SLOW2"} and "did not answer within" in missing["SLOW1"]
    assert calls["split_timed_out"] == 2


def test_a_split_calendar_the_history_read_already_fetched_is_a_memo_hit(fresh_fmp_cache, monkeypatch):
    from ba2_providers import symbol_info
    requests = []

    def get_json(path, api_key, symbol, endpoint, extra=None, base=None):
        requests.append(symbol)
        return _split_payload(symbol)

    monkeypatch.setattr(symbol_info, "_get_json", get_json)
    symbol_info.fetch_splits("k", "AAPL")          # what the FMP history provider's refresh does
    calls = {"split_calendar_reads": 0, "split_memo_hits": 0, "split_timed_out": 0}
    splits, missing = svc.fetch_split_days(["AAPL", "MSFT"], calls=calls)
    assert requests == ["AAPL", "MSFT"]            # AAPL was not asked again
    assert calls["split_memo_hits"] == 1 and calls["split_calendar_reads"] == 1
    assert splits["AAPL"] == [date(2026, 11, 2)]


def test_the_split_step_runs_after_the_history_step(world, monkeypatch):
    a1, *_ = world
    _open(a1.id, "AAPL")
    order = []
    real_closes = svc.price_history.fetch_closes

    def closes(*a, **k):
        time.sleep(0.3)                    # a history read takes a moment: a split read in parallel would land first
        got = real_closes(*a, **k)
        order.append("history")
        return got

    monkeypatch.setattr(svc.price_history, "fetch_closes", closes)
    monkeypatch.setattr(svc, "fetch_split_days", lambda symbols, **kw: (order.append("splits") or ({}, {})))
    svc.load_heatmap_data(a1.id, NOW)
    assert order == ["history", "splits"]


def test_the_timing_line_carries_each_steps_real_time_and_counts(world, monkeypatch):
    import re
    from ba2_trade_platform.services.load_timing import LoadBreakdown
    a1, *_ = world
    _open(a1.id, "AAPL")
    _open(a1.id, "MSFT")

    def slow_caps(symbols, **kw):
        time.sleep(0.3)
        return {}, {s: "FMP returned no market cap" for s in symbols}

    monkeypatch.setattr(svc, "fetch_market_caps", slow_caps)
    timing = LoadBreakdown("Heatmap", "account=1")
    svc.load_heatmap_data(a1.id, NOW, timing=timing)
    line = timing.summary()
    caps_ms = int(re.search(r"market caps \(parallel\) (\d+)ms", line).group(1))
    assert caps_ms >= 250, line
    for piece in ("broker ", "history ", "splits ", "2 symbols", "positions read", "cache hits", "memo hits"):
        assert piece in line, (piece, line)


def test_progress_names_each_step_and_counts_symbols(world):
    a1, *_ = world
    _open(a1.id, "AAPL")
    _open(a1.id, "MSFT")
    seen = []
    svc.load_heatmap_data(a1.id, NOW, progress=seen.append)
    text = " | ".join(seen)
    for piece in ("broker", "history", "split", "of 2 symbols"):
        assert piece in text, (piece, text)


# ---- the history read: from disk first ---------------------------------------------------------------------------

def test_fetch_closes_can_read_up_to_a_pinned_end(monkeypatch):
    import pandas as pd
    asked = []

    class Provider:
        def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval="1d"):
            asked.append(end_date)
            return pd.DataFrame({"Date": pd.to_datetime(["2026-11-20"], utc=True), "Close": [10.0]})

    monkeypatch.setattr(svc.price_history, "_provider", lambda account_id: Provider())
    pinned = datetime(2026, 11, 23, 23, 59)
    got = svc.price_history.fetch_closes({1: ["AAPL"]}, date(2026, 11, 1), end=pinned)
    assert asked == [pinned] and got == {"AAPL": {"2026-11-20": 10.0}}
    svc.price_history.fetch_closes({1: ["AAPL"]}, date(2026, 11, 1))
    assert asked[1] > pinned                       # no end: "now", as before


def test_history_is_read_up_to_the_newest_reference_close_and_topped_up_only_where_one_is_missing(world, monkeypatch):
    a1, *_ = world
    _open(a1.id, "AAPL")
    _open(a1.id, "MSFT")
    needed = _reference_sessions()
    asked = []

    def closes(by_account, start, *, deadline_s=None, errors=None, end=None):
        syms = [s for v in by_account.values() for s in v]
        asked.append((syms, end))

        def dates(s):                              # MSFT's file on disk ends before the newest reference session
            return needed[:-1] if (end is not None and s == "MSFT") else needed

        out = svc.price_history.Closes({s: {d.isoformat(): 1.0 for d in dates(s)} for s in syms})
        out.unfetched = []
        return out

    monkeypatch.setattr(svc.price_history, "fetch_closes", closes)
    data = svc.load_heatmap_data(a1.id, NOW)
    assert asked == [(["AAPL", "MSFT"], datetime(2026, 11, 23, 23, 59)),   # pinned: the provider reads its disk
                     (["MSFT"], None)]                                     # only MSFT lacked a reference close
    assert set(data.market.closes["MSFT"]) == {d.isoformat() for d in needed}
    assert data.calls["history_topped_up"] == 1


# ---- the broker's book is the truth (data layer) ----------------------------------------------------------------

def test_a_symbol_the_broker_holds_without_any_transaction_is_read_priced_and_has_history(world):
    a1, a2, brokers, history_calls = world
    _open(a1.id, "AAPL")
    data = svc.load_heatmap_data(None, NOW)
    assert data.market.broker_qty[a2.id] == {"XOM": 7.0}           # a2 has no platform rows at all
    assert data.market.prices["XOM"] == 50.0
    assert "XOM" in data.market.closes
    assert brokers[a2.id].position_reads == 1


def test_an_account_with_no_platform_rows_is_still_read_when_selected(world):
    _a1, a2, brokers, _h = world
    data = svc.load_heatmap_data(a2.id, NOW)
    assert data.lots == [] and data.market.broker_qty == {a2.id: {"XOM": 7.0}}


def test_a_failed_positions_read_is_reported_for_that_account_only(world):
    a1, a2, brokers, _h = world
    brokers[a1.id].positions = None
    _open(a1.id, "AAPL")
    data = svc.load_heatmap_data(None, NOW)
    assert a1.id in data.market.broker_failed and a1.id not in data.market.broker_qty
    assert a2.id in data.market.broker_qty


def test_a_hung_broker_is_reported_as_failed_for_that_account(world, monkeypatch):
    a1, a2, brokers, _h = world
    monkeypatch.setattr(svc, "BROKER_DEADLINE_S", 0.3)
    release = threading.Event()
    brokers[a1.id].hang = release
    try:
        data = svc.load_heatmap_data(None, NOW)
    finally:
        release.set()
    assert "timed out" in data.market.broker_failed[a1.id]


def test_option_rows_in_the_broker_book_are_counted_not_held(world):
    a1, *_ = world
    data = svc.load_heatmap_data(a1.id, NOW)
    assert data.market.broker_option_rows == {a1.id: 1}
    assert "AAPL250117C00100000" not in data.market.broker_qty[a1.id]


class AlpacaAccount:
    """Named like the real adapter on purpose: the load may ask it for its positions, ONCE, and nothing else. Any other
    attribute (a trade-history read, an execution window, a price feed call) is recorded as a violation."""

    def __init__(self, positions):
        self.positions = positions
        self.calls = []
        self.violations = []

    def get_positions(self):
        self.calls.append("get_positions")
        return self.positions

    def __getattr__(self, name):
        if not name.startswith("__"):
            self.violations.append(name)
        raise AttributeError(name)


def test_a_load_asks_the_broker_for_its_positions_once_and_nothing_else(world, monkeypatch):
    a1, *_ = world
    strict = AlpacaAccount([{"symbol": "AAPL", "current_price": 110.0, "qty": 10.0, "asset_class": "us_equity"}])
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda i: strict)
    _open(a1.id, "AAPL")
    svc.load_heatmap_data(a1.id, NOW)
    assert strict.calls == ["get_positions"]
    assert strict.violations == []


def test_the_whole_account_scope_is_every_account_the_scheduled_refresh_reads(world):
    a1, a2, brokers, _h = world
    svc.load_heatmap_data(None, NOW)
    assert brokers[a1.id].position_reads == 1 and brokers[a2.id].position_reads == 1


# ---- the split calendar shares the history download slots ----------------------------------------------------

def test_split_reads_never_exceed_the_shared_download_cap(fresh_fmp_cache, monkeypatch):
    from contextlib import ExitStack
    from ba2_providers import symbol_info
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def tracked(key, sym):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return _split_payload(sym)

    monkeypatch.setattr(symbol_info, "fetch_splits", tracked)
    symbols = [f"S{i:02d}" for i in range(40)]
    calls = {"split_calendar_reads": 0, "split_memo_hits": 0, "split_timed_out": 0}
    with ExitStack() as held:                       # another page is downloading with two of the shared slots
        for _ in range(2):
            assert held.enter_context(svc.price_history.download_slot(1.0))
        splits, missing = svc.fetch_split_days(symbols, calls=calls, deadline_s=10.0)
    assert missing == {} and len(splits) == 40
    assert state["max"] <= svc.price_history.MAX_CONCURRENT - 2


def test_no_split_request_starts_after_the_deadline(fresh_fmp_cache, monkeypatch):
    from ba2_providers import symbol_info
    release = threading.Event()
    started = []

    def stuck(key, sym):
        started.append(sym)
        release.wait(5)
        return _split_payload(sym)

    monkeypatch.setattr(symbol_info, "fetch_splits", stuck)
    calls = {"split_calendar_reads": 0, "split_memo_hits": 0, "split_timed_out": 0}
    splits, missing = svc.fetch_split_days([f"S{i:02d}" for i in range(20)], calls=calls, deadline_s=0.3)
    in_flight = len(started)
    release.set()
    time.sleep(0.3)
    assert in_flight <= svc.price_history.MAX_CONCURRENT
    assert len(started) == in_flight                # nothing new started once the deadline had passed
    assert len(missing) == 20 - len(splits)


# ---- one symbol identity -----------------------------------------------------------------------------------------

def test_a_class_share_is_one_symbol_for_the_broker_the_platform_and_the_history(world, monkeypatch):
    a1, _a2, brokers, history_calls = world
    brokers[a1.id].positions = [{"symbol": "BRK.B", "current_price": 470.0, "qty": 3.0, "asset_class": "us_equity"}]
    _open(a1.id, "brk-b")
    data = svc.load_heatmap_data(a1.id, NOW)
    assert [l.symbol for l in data.lots] == ["BRK-B"]
    assert data.market.broker_qty == {a1.id: {"BRK-B": 3.0}}
    assert data.market.prices["BRK-B"] == 470.0
    assert history_calls[0][0] == {a1.id: ["BRK-B"]}
    from ba2_trade_platform.ui.utils import position_heatmap as hm
    view = hm.build_view(data.lots, data.market, hm.HeatmapState(), NOW)
    assert [t.symbol for t in view.tiles] == ["BRK-B"]
    assert not any("not held at the broker" in why for _s, why in view.issues)


def test_a_slash_class_share_from_the_broker_is_the_same_symbol(world):
    a1, _a2, brokers, _h = world
    brokers[a1.id].positions = [{"symbol": "BRK/B", "current_price": 470.0, "qty": 3.0}]
    _open(a1.id, "BRK.B")
    data = svc.load_heatmap_data(a1.id, NOW)
    assert data.market.broker_qty == {a1.id: {"BRK-B": 3.0}} and [l.symbol for l in data.lots] == ["BRK-B"]


# ---- a busy history source ------------------------------------------------------------------------------------

def test_a_busy_history_source_is_retried_within_the_budget(world, monkeypatch):
    a1, *_ = world
    _open(a1.id, "AAPL")
    real = svc.price_history.fetch_closes
    tries = []

    def busy_then_free(*a, **k):
        tries.append(1)
        if len(tries) < 3:
            raise PriceSourceUnreachable("The price source is busy: every download slot is in use", {"busy": True})
        return real(*a, **k)

    monkeypatch.setattr(svc.price_history, "fetch_closes", busy_then_free)
    monkeypatch.setattr(svc, "HISTORY_BUSY_RETRY_S", 0.05)
    data = svc.load_heatmap_data(a1.id, NOW)
    assert "AAPL" in data.market.closes and len(tries) == 3


def test_a_history_source_busy_for_the_whole_budget_says_reload_and_the_tab_still_draws(world, monkeypatch):
    a1, a2, _b, _h = world
    _open(a1.id, "AAPL")

    def busy(*a, **k):
        raise PriceSourceUnreachable("The price source is busy: every download slot is in use", {"busy": True})

    monkeypatch.setattr(svc.price_history, "fetch_closes", busy)
    monkeypatch.setattr(svc, "HISTORY_BUSY_RETRY_S", 0.05)
    monkeypatch.setattr(svc, "HISTORY_BUDGET_S", 0.5)
    data = svc.load_heatmap_data(None, NOW)
    assert data.market.closes_missing["AAPL"] == "history busy, reload"
    assert data.market.closes_missing["XOM"] == "history busy, reload"
    assert data.market.prices["AAPL"] == 110.0                   # the rest of the load is intact


# ---- TastyTrade: the stored broker transaction history -----------------------------------------------------------

class TastyLike:
    """Answers exactly two calls -- the positions read and the share-movement read -- and records their ORDER and how
    many history reads ran at once; anything else asked of it is a violation."""

    def __init__(self, positions, rows=(), fail=None, delay=0.0):
        self.positions = positions
        self.rows = list(rows)
        self.fail = fail
        self.delay = delay
        self.order = []
        self.violations = []
        self.in_flight = 0
        self.max_in_flight = 0

    def get_positions(self):
        self.order.append("positions")
        return self.positions

    def read_share_movements(self, start_date, *, stop=None):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.order.append(("history", start_date))
            if self.delay:
                time.sleep(self.delay)
            if self.fail is not None:
                raise self.fail
            return {"rows": [dict(r) for r in self.rows], "pages": 1, "total_items": len(self.rows),
                    "requested_from": start_date}
        finally:
            self.in_flight -= 1

    def __getattr__(self, name):
        if not name.startswith("__"):
            self.violations.append(name)
        raise AttributeError(name)


def _tasty(monkeypatch, account_id, **kw):
    fake = TastyLike([{"symbol": "AAOI", "current_price": 38.0, "qty": 10.6809}], **kw)
    monkeypatch.setattr(svc, "get_account_instance_from_id", lambda i: fake if i == account_id else None)
    return fake


def _row(rid=1, symbol="aaoi", qty=-2.0, when=datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)):
    return {"id": rid, "symbol": symbol, "underlying": symbol, "quantity": qty, "price": 35.0, "executed_at": when,
            "kind": "trade", "detail": "Trade / Sell to Close", "transaction_type": "Trade",
            "sub_type": "Sell to Close", "instrument_type": "Equity", "action": "Sell to Close", "reverses_id": None}


@pytest.mark.api_db
def test_a_tastytrade_account_is_asked_for_its_positions_then_its_history_and_nothing_else(world, monkeypatch):
    a1, *_ = world
    fake = _tasty(monkeypatch, a1.id, rows=[_row()])
    data = svc.load_heatmap_data(a1.id, NOW)
    assert fake.order == ["positions", ("history", date(2025, 12, 28))]       # 3 days before the year's reference
    assert fake.violations == []
    hist = data.market.broker_history[a1.id]
    assert hist.failure is None
    assert [(m.symbol, m.quantity, m.price, m.kind, m.id) for m in hist.moves] == [("AAOI", -2.0, 35.0, "trade", 1)]
    assert hist.covers_from == datetime(2025, 12, 30, tzinfo=timezone.utc)
    assert hist.positions_at is not None


@pytest.mark.api_db
def test_after_a_restart_the_stored_copy_is_used_and_one_small_window_is_read(world, monkeypatch):
    from ba2_common.core.market_calendar import NY_TZ
    a1, *_ = world
    fake = _tasty(monkeypatch, a1.id, rows=[_row()])
    svc.load_heatmap_data(a1.id, NOW)
    svc.clear_caches()                                        # "a new process": only the database remains
    data = svc.load_heatmap_data(a1.id, NOW)
    today_ny = datetime.now(NY_TZ).date()
    assert [step for step in fake.order if step != "positions"] == [
        ("history", date(2025, 12, 28)), ("history", today_ny - timedelta(days=7))]
    assert [m.id for m in data.market.broker_history[a1.id].moves] == [1]


def test_an_account_without_the_share_read_gets_no_broker_history(world):
    a1, *_ = world
    _open(a1.id, "AAPL")
    assert svc.load_heatmap_data(a1.id, NOW).market.broker_history == {}


@pytest.mark.api_db
def test_a_failed_history_read_is_redacted_and_the_positions_stand(world, monkeypatch):
    a1, *_ = world
    _tasty(monkeypatch, a1.id, fail=ConnectionError("GET /accounts/5WX00000/transactions?apikey=SECRET9 failed"))
    data = svc.load_heatmap_data(a1.id, NOW)
    assert data.market.broker_qty == {a1.id: {"AAOI": 10.6809}}
    why = data.market.broker_history[a1.id].failure
    assert "ConnectionError" in why and "/accounts/***" in why
    assert "5WX00000" not in why and "SECRET9" not in why


@pytest.mark.api_db
def test_a_history_not_ready_in_time_says_still_loading_and_is_stored_for_the_next_load(world, monkeypatch):
    from ba2_trade_platform.services import broker_share_history as bsh
    a1, *_ = world
    monkeypatch.setattr(bsh, "WAIT_S", 0.05)
    _tasty(monkeypatch, a1.id, rows=[_row()], delay=0.5)
    began = time.monotonic()
    data = svc.load_heatmap_data(a1.id, NOW)
    assert time.monotonic() - began < 0.5
    assert data.market.broker_qty == {a1.id: {"AAOI": 10.6809}}
    assert data.market.broker_history[a1.id].failure == "broker history still loading, reload"
    for _ in range(40):
        if bsh.load_store(a1.id) is not None:
            break
        time.sleep(0.05)
    assert bsh.load_store(a1.id) is not None


@pytest.mark.api_db
def test_concurrent_loads_never_issue_concurrent_history_requests(world, monkeypatch):
    a1, *_ = world
    fake = _tasty(monkeypatch, a1.id, rows=[_row()], delay=0.2)
    loads = [threading.Thread(target=svc.load_heatmap_data, args=(scope, NOW)) for scope in (a1.id, None)]
    for t in loads:
        t.start()
    for t in loads:
        t.join(10)
    assert fake.max_in_flight == 1
    # one sync, plus at most one queued behind it for the load whose positions came after it started
    assert 1 <= len([step for step in fake.order if step != "positions"]) <= 2


@pytest.mark.api_db
def test_the_platforms_filled_orders_reach_the_cross_check(world, monkeypatch):
    a1, *_ = world
    _tasty(monkeypatch, a1.id)
    create_trading_order(a1.id, symbol="AAOI", quantity=1.38986, side=OrderDirection.BUY, transaction_id=None,
                         status=OrderStatus.FILLED, filled_qty=1.38986, open_price=30.0,
                         created_at=datetime(2026, 11, 23, 14, 59, tzinfo=timezone.utc))
    data = svc.load_heatmap_data(a1.id, NOW)
    [order] = data.market.platform_orders[a1.id]
    assert (order.symbol, order.side, order.quantity) == ("AAOI", "BUY", 1.38986)


# ---- the tiles never wait on, or fail because of, the history ------------------------------------------------------

@pytest.mark.api_db
@pytest.mark.parametrize("target,error", [("platform_orders", RuntimeError("database is locked")),
                                          ("request_sync", AttributeError("no attribute 'read_share_movements'")),
                                          ("history_from_store", KeyError("executed_at"))])
def test_a_failure_in_the_history_path_leaves_every_tile_standing(world, monkeypatch, target, error):
    from ba2_trade_platform.services import broker_share_history as bsh
    a1, _a2, _b, history_calls = world
    _tasty(monkeypatch, a1.id, rows=[_row()])

    def boom(*a, **k):
        raise error

    if target == "platform_orders":
        monkeypatch.setattr(svc, "_platform_orders", boom)
    else:
        monkeypatch.setattr(bsh, target, boom)
    data = svc.load_heatmap_data(a1.id, NOW)
    assert data.market.broker_qty == {a1.id: {"AAOI": 10.6809}}
    assert data.market.prices["AAOI"] == 38.0
    assert history_calls and "AAOI" in history_calls[0][0][a1.id]          # the closes were read
    assert data.market.splits == {"AAPL": [date(2026, 11, 2)]}             # and the split calendar
    assert data.market.broker_history[a1.id].failure


@pytest.mark.api_db
def test_the_history_is_read_while_the_tile_inputs_are_read(world, monkeypatch):
    a1, _a2, _b, _h = world
    _tasty(monkeypatch, a1.id, rows=[_row()], delay=1.5)
    real = svc.price_history.fetch_closes

    slept = []

    def slow_closes(*a, **k):                # the first closes read is slow (a top-up read follows it)
        if not slept:
            slept.append(1)
            time.sleep(1.5)
        return real(*a, **k)

    monkeypatch.setattr(svc.price_history, "fetch_closes", slow_closes)
    began = time.monotonic()
    data = svc.load_heatmap_data(a1.id, NOW)
    assert time.monotonic() - began < 2.6                       # side by side (1.5 s), not one after the other (3 s)
    assert data.market.broker_history[a1.id].failure is None
