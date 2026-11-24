"""Stored snapshots of the slow reads (``services.growth_snapshot``): an exact codec, and loaders that never raise.

The growth tab paints from the ``"growth"`` snapshot, the allocator reads the ``"dividends"`` one, and the dividend
forecast keeps FMP's raw dividend histories across restarts. A copy that is not exact is never stored: the codec
refuses what it cannot round-trip. A row that cannot be read is absent (logged), never an exception in the page."""
import threading
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.growth_snapshot_models import (
    DATASET_DIVIDENDS, DATASET_GROWTH, GROWTH_SNAPSHOT_SCHEMA_VERSION, AccountGrowthSnapshot)
from ba2_trade_platform.core.models import Position
from ba2_trade_platform.core.types import OrderDirection
from ba2_trade_platform.services import growth_snapshot as gs

UTC = timezone.utc


def _position(symbol="AAA", qty=10.0, side=OrderDirection.BUY, **kw):
    fields = dict(asset_class="Equity", avg_entry_price=10.0, avg_entry_swap_rate=None, change_today=0.01,
                  cost_basis=100.0, current_price=11.0, exchange="", lastday_price=10.9, market_value=110.0,
                  qty=qty, qty_available=qty, side=side, swap_rate=None, symbol=symbol,
                  unrealized_intraday_pl=1.0, unrealized_intraday_plpc=0.009, unrealized_pl=10.0,
                  unrealized_plpc=0.1)
    fields.update(kw)
    return Position(**fields)


@pytest.fixture
def warnings_seen(monkeypatch):
    seen = []
    monkeypatch.setattr(gs.logger, "warning", lambda msg, *a, **k: seen.append(str(msg)))
    return seen


# ---- codec ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    None, True, False, 0, -7, 3.25, 1e-12, "text", "", "2026-01-02",
    date(2026, 1, 2),
    datetime(2026, 1, 2, 3, 4, 5, 678901),
    datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-5))),
    datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=1), "CET")),
    datetime(2026, 7, 2, 3, 4, 5, tzinfo=ZoneInfo("America/New_York")),
])
def test_every_supported_value_round_trips_with_its_exact_type_and_tzinfo(value):
    back = gs.decode_rows(gs.encode_rows([{"k": value}]))[0]["k"]
    assert back == value and type(back) is type(value)
    if isinstance(value, datetime):
        assert type(back.tzinfo) is type(value.tzinfo)
        assert back.tzinfo == value.tzinfo or back.tzinfo is value.tzinfo
        assert back.utcoffset() == value.utcoffset() and back.tzname() == value.tzname()


def test_the_second_of_two_repeated_wall_times_keeps_its_fold():
    value = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=1)   # EST, not EDT
    back = gs.decode_rows(gs.encode_rows([{"k": value}]))[0]["k"]
    assert back.fold == 1 and back.utcoffset() == value.utcoffset() == timedelta(hours=-5)


def test_a_date_and_a_midnight_datetime_stay_different_types():
    rows = gs.decode_rows(gs.encode_rows([{"d": date(2026, 1, 2), "t": datetime(2026, 1, 2)}]))
    assert type(rows[0]["d"]) is date and type(rows[0]["t"]) is datetime


def test_a_string_that_looks_like_a_tag_is_still_a_string():
    row = {"a": "{'d': '2026-01-02'}", "b": "d:2026-01-02"}
    assert gs.decode_rows(gs.encode_rows([row])) == [row]


def test_the_pydantic_utc_tzinfo_the_brokers_return_round_trips():
    from pydantic_core import TzInfo
    value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=TzInfo(0))
    back = gs.decode_rows(gs.encode_rows([{"filled_at": value}]))[0]["filled_at"]
    assert back == value and type(back.tzinfo) is TzInfo and back.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value, type_name", [
    ({"nested": 1}, "dict"), ([1, 2], "list"), ((1,), "tuple"), (object(), "object"),
    (OrderDirection.BUY, "OrderDirection"),
])
def test_anything_else_is_refused_naming_the_key_and_the_type(value, type_name):
    with pytest.raises(TypeError) as err:
        gs.encode_rows([{"ok": 1, "bad_key": value}])
    assert "bad_key" in str(err.value) and type_name in str(err.value)


def test_a_decimal_is_refused_rather_than_stored_as_a_lossy_float():
    from decimal import Decimal
    with pytest.raises(TypeError, match="amount.*Decimal"):
        gs.encode_rows([{"amount": Decimal("1.10")}])


def test_an_unsupported_tzinfo_is_refused():
    class OddTz(tzinfo):                     # a tzinfo subclass the codec does not know
        def utcoffset(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return "ODD"

        def dst(self, dt):
            return None
    with pytest.raises(TypeError, match="when.*OddTz"):
        gs.encode_rows([{"when": datetime(2026, 1, 2, tzinfo=OddTz())}])


def test_a_row_that_is_not_a_dict_is_refused():
    with pytest.raises(TypeError, match="list"):
        gs.encode_rows([[1, 2]])


def test_positions_round_trip_as_real_position_objects_with_the_side_enum():
    pos = [_position("AAA"), _position("BBB", qty=-3.0, side=OrderDirection.SELL, swap_rate=1.5)]
    back = gs.decode_positions(gs.encode_positions(pos))
    assert [type(p) for p in back] == [Position, Position]
    assert [p.model_dump() for p in back] == [p.model_dump() for p in pos]
    assert back[1].side is OrderDirection.SELL and back[0].side is OrderDirection.BUY


def test_an_alpaca_position_with_its_enum_exchange_and_asset_class_round_trips():
    from alpaca.trading.enums import AssetClass, AssetExchange
    pos = _position("AAA", exchange=AssetExchange.NASDAQ, asset_class=AssetClass.US_EQUITY)
    back = gs.decode_positions(gs.encode_positions([pos]))[0]
    assert back.exchange is AssetExchange.NASDAQ and back.asset_class is AssetClass.US_EQUITY


def test_a_list_item_that_is_not_a_position_is_refused():
    from types import SimpleNamespace
    with pytest.raises(TypeError, match="SimpleNamespace"):
        gs.encode_positions([SimpleNamespace(symbol="AAA", qty=1)])


# ---- growth snapshot -------------------------------------------------------------------------------------------------

def _save(account_id=1, fetched_at=None, **kw):
    data = dict(dividends=[{"symbol": "AAA", "amount": 5.0, "date": date(2026, 3, 1), "drip_quantity": None,
                            "account_id": account_id, "account_name": "A1"}],
                trades=[{"symbol": "AAA", "side": "BUY", "qty": 1.0, "price": 10.0,
                         "date": datetime(2025, 1, 2, 15, 30, tzinfo=UTC), "account_id": account_id}],
                positions=[_position("AAA")],
                balance=[{"date": date(2026, 3, 1), "net_liquidating_value": 1000.0, "cash_balance": 10.0,
                          "account_id": account_id, "account_name": "A1"}],
                balance_floor=None,
                fetched_at=fetched_at or datetime(2026, 11, 24, 9, 0, tzinfo=UTC))
    data.update(kw)
    gs.save_growth_snapshot(account_id, **data)
    return data


def test_a_saved_growth_snapshot_loads_back_exactly():
    data = _save(balance_floor=date(2023, 1, 1))
    snap = gs.load_growth_snapshot(1)
    assert snap.account_id == 1 and snap.fetched_at == data["fetched_at"]
    assert snap.dividends == data["dividends"] and snap.trades == data["trades"]
    assert snap.balance == data["balance"] and snap.balance_floor == date(2023, 1, 1)
    assert [p.model_dump() for p in snap.positions] == [p.model_dump() for p in data["positions"]]


def test_a_balance_floor_of_none_means_the_default_window_and_stays_none():
    _save(balance_floor=None)
    assert gs.load_growth_snapshot(1).balance_floor is None


def test_saving_the_growth_snapshot_also_writes_the_dividends_dataset():
    data = _save()
    stored = gs.load_dividends(1)
    assert stored.rows == data["dividends"] and stored.fetched_at == data["fetched_at"]


def test_a_second_save_replaces_the_first():
    _save(fetched_at=datetime(2026, 11, 24, 9, 0, tzinfo=UTC))
    _save(fetched_at=datetime(2026, 11, 24, 10, 0, tzinfo=UTC),
          dividends=[{"symbol": "BBB", "amount": 1.0, "date": date(2026, 4, 1)}])
    snap = gs.load_growth_snapshot(1)
    assert snap.fetched_at.hour == 10 and [d["symbol"] for d in snap.dividends] == ["BBB"]
    assert [d["symbol"] for d in gs.load_dividends(1).rows] == ["BBB"]


# ---- an empty broker answer never replaces stored rows ----------------------------------------------------------------
# The lenient broker getters return [] on a FAILED fetch, and the tab cannot always tell (no previous rows in the page).

@pytest.mark.parametrize("dataset", ["dividends", "trades", "balance"])
def test_an_empty_list_over_stored_rows_is_refused_and_the_snapshot_kept(dataset):
    first = _save(fetched_at=datetime(2026, 11, 24, 9, 0, tzinfo=UTC))
    with pytest.raises(ValueError) as err:
        _save(fetched_at=datetime(2026, 11, 24, 10, 0, tzinfo=UTC), **{dataset: []})
    assert dataset in str(err.value) and "account 1" in str(err.value)
    snap = gs.load_growth_snapshot(1)
    assert snap.fetched_at == first["fetched_at"] and getattr(snap, dataset) == first[dataset]
    assert gs.load_dividends(1).rows == first["dividends"]


@pytest.mark.parametrize("dataset", ["dividends", "trades", "balance"])
def test_a_confirmed_empty_dataset_may_replace_stored_rows(dataset):
    _save(fetched_at=datetime(2026, 11, 24, 9, 0, tzinfo=UTC))
    _save(fetched_at=datetime(2026, 11, 24, 10, 0, tzinfo=UTC), confirmed_empty={dataset}, **{dataset: []})
    snap = gs.load_growth_snapshot(1)
    assert snap.fetched_at.hour == 10 and getattr(snap, dataset) == []


def test_confirming_one_dataset_does_not_confirm_another():
    _save()
    with pytest.raises(ValueError, match="trades"):
        _save(dividends=[], trades=[], confirmed_empty={"dividends"})


def test_positions_may_become_empty_a_flat_account_is_real():
    _save(fetched_at=datetime(2026, 11, 24, 9, 0, tzinfo=UTC))
    _save(fetched_at=datetime(2026, 11, 24, 10, 0, tzinfo=UTC), positions=[])
    assert gs.load_growth_snapshot(1).positions == []


def test_an_empty_list_where_nothing_was_stored_is_saved():
    _save(dividends=[], trades=[], balance=[])
    snap = gs.load_growth_snapshot(1)
    assert snap.dividends == [] and snap.trades == [] and snap.balance == []


def test_the_dividends_row_is_never_written_empty():
    _save(dividends=[])                                            # nothing stored before
    assert gs.load_growth_snapshot(1) is not None and gs.load_dividends(1) is None


def test_a_confirmed_empty_dividends_list_still_leaves_the_dividends_row_alone():
    first = _save(fetched_at=datetime(2026, 11, 24, 9, 0, tzinfo=UTC))
    _save(fetched_at=datetime(2026, 11, 24, 10, 0, tzinfo=UTC), dividends=[], confirmed_empty={"dividends"})
    assert gs.load_growth_snapshot(1).dividends == []
    stored = gs.load_dividends(1)
    assert stored.rows == first["dividends"] and stored.fetched_at == first["fetched_at"]


def test_snapshots_are_per_account():
    _save(account_id=1)
    assert gs.load_growth_snapshot(2) is None
    assert gs.load_growth_snapshot(1) is not None


def test_a_refused_value_saves_nothing():
    from types import SimpleNamespace
    with pytest.raises(TypeError):
        _save(positions=[SimpleNamespace(symbol="AAA", qty=1)])
    assert gs.load_growth_snapshot(1) is None and gs.load_dividends(1) is None


def test_a_naive_fetched_at_is_refused():
    with pytest.raises(ValueError, match="UTC"):
        _save(fetched_at=datetime(2026, 11, 24, 9, 0))


def _tamper(account_id, dataset, **changes):
    with get_db() as session:
        row = session.get(AccountGrowthSnapshot, (account_id, dataset))
        for k, v in changes.items():
            setattr(row, k, v)
        session.add(row)
        session.commit()


def test_a_row_of_another_schema_version_is_absent_and_says_why(warnings_seen):
    _save()
    _tamper(1, DATASET_GROWTH, schema_version=GROWTH_SNAPSHOT_SCHEMA_VERSION + 1)
    assert gs.load_growth_snapshot(1) is None
    assert any("schema" in w and "account 1" in w for w in warnings_seen), warnings_seen


@pytest.mark.parametrize("payload", [{"garbage": True}, {"dividends": [{"x": {"zz": 1}}], "trades": [],
                                                          "positions": [], "balance": [], "balance_floor": None},
                                     "not even a dict"])
def test_a_corrupt_payload_is_absent_and_says_why(warnings_seen, payload):
    _save()
    _tamper(1, DATASET_GROWTH, payload=payload)
    assert gs.load_growth_snapshot(1) is None
    assert any("account 1" in w for w in warnings_seen), warnings_seen


def test_a_corrupt_dividends_row_is_absent_and_says_why(warnings_seen):
    _save()
    _tamper(1, DATASET_DIVIDENDS, payload={"rows": "nope"})
    assert gs.load_dividends(1) is None
    assert warnings_seen


def test_a_database_failure_on_load_is_absent_and_says_why(monkeypatch, warnings_seen):
    def broken():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(gs, "get_db", broken)
    assert gs.load_growth_snapshot(1) is None and gs.load_dividends(1) is None
    assert gs.load_dividend_histories(["AAA"]) == {}
    assert len([w for w in warnings_seen if "database is locked" in w]) == 3


def test_no_row_is_absent_without_a_warning(warnings_seen):
    assert gs.load_growth_snapshot(1) is None and gs.load_dividends(1) is None
    assert warnings_seen == []


# ---- dividends dataset -----------------------------------------------------------------------------------------------

def test_dividends_save_and_load():
    rows = [{"symbol": "O", "amount": 1.25, "date": datetime(2026, 1, 15), "drip_quantity": None, "id": "x1"}]
    at = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
    assert gs.save_dividends(7, rows, at) is True
    stored = gs.load_dividends(7)
    assert stored.rows == rows and stored.fetched_at == at
    assert gs.load_growth_snapshot(7) is None          # the dividends dataset alone is no growth snapshot


def test_an_empty_dividends_list_is_never_written():
    assert gs.save_dividends(7, [], datetime(2026, 11, 24, 8, 0, tzinfo=UTC)) is False
    assert gs.load_dividends(7) is None
    rows = [{"symbol": "O", "amount": 1.25, "date": date(2026, 1, 15)}]
    at = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
    gs.save_dividends(7, rows, at)
    assert gs.save_dividends(7, [], datetime(2026, 11, 24, 9, 0, tzinfo=UTC)) is False
    stored = gs.load_dividends(7)
    assert stored.rows == rows and stored.fetched_at == at


# ---- dividend histories ----------------------------------------------------------------------------------------------

def test_dividend_histories_are_keyed_by_upper_case_symbol_and_keep_the_raw_payload():
    payload = {"symbol": "AAA", "historical": [{"date": "2026-01-02", "dividend": 0.5, "paymentDate": "2026-01-20"}]}
    at = datetime(2026, 11, 20, tzinfo=UTC)
    gs.save_dividend_history("aaa", payload, at)
    gs.save_dividend_history("BBB", {}, at)                      # a non-payer is stored too
    got = gs.load_dividend_histories(["AAA", "bbb", "CCC"])
    assert got == {"AAA": (payload, at), "BBB": ({}, at)}


def test_a_dividend_history_save_replaces_the_previous_payload():
    gs.save_dividend_history("AAA", {}, datetime(2026, 11, 1, tzinfo=UTC))
    gs.save_dividend_history("AAA", {"historical": []}, datetime(2026, 11, 2, tzinfo=UTC))
    assert gs.load_dividend_histories(["AAA"])["AAA"][0] == {"historical": []}


@pytest.mark.parametrize("payload", [
    [], [{"date": "2026-01-02", "dividend": 0.5}], {}, {"historical": []},
    {"symbol": "AAA", "historical": [{"date": "2026-01-02", "dividend": 0.5}]},
])
def test_the_shapes_fmp_answers_with_are_stored(payload):
    assert gs.save_dividend_history("AAA", payload, datetime(2026, 11, 2, tzinfo=UTC)) is True
    assert gs.load_dividend_histories(["AAA"])["AAA"][0] == payload


@pytest.mark.parametrize("payload", [
    {"Error Message": "Limit Reach"}, {"message": "Upgrade your plan"}, "Limit Reach", None, 42,
    {"symbol": "AAA"}, {"historical": "nope"}, {"historical": None},
])
def test_any_other_shape_is_refused_and_nothing_stored(payload):
    with pytest.raises(ValueError, match="AAA"):
        gs.save_dividend_history("AAA", payload, datetime(2026, 11, 2, tzinfo=UTC))
    assert gs.load_dividend_histories(["AAA"]) == {}


def test_a_history_that_parses_to_payments_is_never_replaced_by_one_that_parses_to_none(warnings_seen):
    paying = {"symbol": "AAA", "historical": [{"date": "2026-01-02", "dividend": 0.5, "paymentDate": "2026-01-20"}]}
    at = datetime(2026, 11, 1, tzinfo=UTC)
    gs.save_dividend_history("AAA", paying, at)
    assert gs.save_dividend_history("AAA", {}, datetime(2026, 11, 2, tzinfo=UTC)) is False
    assert gs.save_dividend_history("AAA", {"historical": [{"date": "x", "dividend": None}]},
                                    datetime(2026, 11, 2, tzinfo=UTC)) is False
    assert gs.load_dividend_histories(["AAA"])["AAA"] == (paying, at)
    assert len([w for w in warnings_seen if "AAA" in w]) == 2, warnings_seen


def test_a_paying_history_may_replace_another_paying_history():
    gs.save_dividend_history("AAA", {"historical": [{"date": "2026-01-02", "dividend": 0.5}]},
                             datetime(2026, 11, 1, tzinfo=UTC))
    newer = {"historical": [{"date": "2026-04-02", "dividend": 0.6}]}
    assert gs.save_dividend_history("AAA", newer, datetime(2026, 11, 2, tzinfo=UTC)) is True
    assert gs.load_dividend_histories(["AAA"])["AAA"][0] == newer


def test_the_history_refresh_runs_one_thread_at_a_time_and_stores_what_it_fetched(monkeypatch):
    saved, gate = [], threading.Event()
    monkeypatch.setattr(gs, "save_dividend_history", lambda sym, payload, at: saved.append((sym, payload)))

    def fetch(sym):
        gate.wait(5)
        return {"historical": [], "symbol": sym}
    first = gs.start_dividend_history_refresh(["AAA", "BBB"], fetch)
    second = gs.start_dividend_history_refresh(["CCC"], fetch)
    assert first is not None and first.daemon and second is None
    gate.set()
    first.join(5)
    assert not first.is_alive()
    assert saved == [("AAA", {"historical": [], "symbol": "AAA"}), ("BBB", {"historical": [], "symbol": "BBB"})]
    third = gs.start_dividend_history_refresh(["CCC"], fetch)       # the first one finished: a new one may start
    third.join(5)
    assert saved[-1][0] == "CCC"


def test_the_history_refresh_warns_and_carries_on_when_one_symbol_fails(monkeypatch, warnings_seen):
    saved = []
    monkeypatch.setattr(gs, "save_dividend_history", lambda sym, payload, at: saved.append(sym))

    def fetch(sym):
        if sym == "BAD":
            raise RuntimeError("FMP 500")
        return {}
    gs.start_dividend_history_refresh(["BAD", "OK"], fetch).join(5)
    assert saved == ["OK"]
    assert any("BAD" in w and "FMP 500" in w for w in warnings_seen), warnings_seen
