"""Read-only trade-chart context for ONE saved row (spec 2026-09-20, step 2).

These tests deliberately install NO network fixture. The route must answer from the
saved trade array and the ON-DISK cache alone, so if the implementation ever reached
for a provider the absence of credentials here is what would surface it. The three
states the popup has to render -- complete history, a cold cache, and a source that
cannot be resolved -- are each asserted as a first-class outcome.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from ba2_common.core import native_cache

URL = "/api/backtests/{backtest_id}/trade-chart"

#: A call debit spread on ACN: two legs sharing transaction 7.
SPREAD_LEGS = [
    {
        "symbol": "ACN", "underlying_symbol": "ACN",
        "contract_symbol": "ACN260918C00095000", "option_type": "call",
        "strike": 95.0, "expiry": "2026-09-18", "multiplier": 100,
        "direction": "buy", "size": 1,
        "entry_time": "2026-09-08T13:30:00", "exit_time": "2026-09-11T19:45:00",
        "entry_price": 8.0, "exit_price": 13.3, "pnl": 530.0, "pnl_pct": 1.06,
        "bars_held": 3, "exit_reason": "take_profit", "transaction_id": 7,
    },
    {
        "symbol": "ACN", "underlying_symbol": "ACN",
        "contract_symbol": "ACN260918C00105000", "option_type": "call",
        "strike": 105.0, "expiry": "2026-09-18", "multiplier": 100,
        "direction": "sell", "size": 1,
        "entry_time": "2026-09-08T13:30:00", "exit_time": "2026-09-11T19:45:00",
        "entry_price": 2.0, "exit_price": 3.6, "pnl": -160.0, "pnl_pct": -0.32,
        "bars_held": 3, "exit_reason": "take_profit", "transaction_id": 7,
    },
]

#: One plain equity round trip, no transaction id -- it stands alone.
EQUITY_ROW = {
    "symbol": "MSFT", "direction": "buy", "size": 10,
    "entry_time": "2026-09-09T14:00:00", "exit_time": "2026-09-10T18:00:00",
    "entry_price": 400.0, "exit_price": 410.0, "pnl": 100.0, "pnl_pct": 0.5,
    "bars_held": 1, "exit_reason": "take_profit",
}

#: Session bars used by the cache fixture. 2026-09-08 is the entry session: a
#: 13:30 entry must NOT be quoted that day's close.
SESSIONS = [
    ("2026-09-03", 99.0), ("2026-09-04", 100.0), ("2026-09-08", 101.0),
    ("2026-09-09", 102.5), ("2026-09-10", 105.0), ("2026-09-11", 108.0),
    ("2026-09-15", 109.0),
]


def _seed(db, trades, engine_type="daily_expert"):
    from app.models.backtest import Backtest

    backtest = Backtest(
        name="opt-chart", expert_name="PremiumSeller", engine_type=engine_type,
        start_date=datetime(2026, 9, 1), end_date=datetime(2026, 10, 1),
        initial_capital=20000.0, status="completed", trades=trades,
    )
    db.add(backtest)
    db.commit()
    db.refresh(backtest)
    return backtest


@pytest.fixture
def empty_cache(tmp_path, monkeypatch):
    """An EMPTY cache root.

    Required by every test that asserts an absence: without it the module reads the
    developer's real cache (26k parquet files on this machine, ACN among them) and a
    "cold cache" test silently passes for the wrong reason.
    """
    monkeypatch.setattr(native_cache, "CACHE_FOLDER", str(tmp_path))


@pytest.fixture
def cache(empty_cache, tmp_path):
    """Point the native cache at a temp dir and let a test write bars into it."""
    _ = empty_cache

    def write(symbol, sessions=SESSIONS):
        frame = pd.DataFrame(
            [
                {
                    "Date": pd.Timestamp(session, tz="UTC"),
                    "Open": close - 1.0, "High": close + 1.0, "Low": close - 2.0,
                    "Close": close, "Volume": 1_000_000,
                    "effective_date": pd.Timestamp(session, tz="UTC"),
                }
                for session, close in sessions
            ]
        )
        native_cache.write_timeseries("FMPOHLCVProvider", symbol, "1d", frame)

    return write


def test_option_row_resolves_the_whole_transaction_and_reads_cached_bars(client, db, cache):
    cache("ACN")
    backtest = _seed(db, [*SPREAD_LEGS, EQUITY_ROW])

    response = client.get(URL.format(backtest_id=backtest.id), params={"trade_id": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["schemaVersion"] == 1
    assert body["selectedTradeId"] == 1
    assert body["transactionId"] == "7"
    # BOTH legs, resolved by transaction id against the saved array.
    assert [leg["id"] for leg in body["legs"]] == [1, 2]
    assert [leg["strike"] for leg in body["legs"]] == [95.0, 105.0]
    assert [leg["direction"] for leg in body["legs"]] == ["long", "short"]
    assert body["underlying"]["symbol"] == "ACN"
    assert body["underlying"]["cacheStatus"] == "complete"
    assert [bar["date"] for bar in body["underlying"]["bars"]] == [
        session for session, _ in SESSIONS
    ]
    assert body["resultDigest"]


def test_equity_row_stands_alone_and_is_not_given_option_terms(client, db, cache):
    cache("MSFT")
    backtest = _seed(db, [*SPREAD_LEGS, EQUITY_ROW])

    response = client.get(URL.format(backtest_id=backtest.id), params={"trade_id": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["transactionId"] is None
    assert len(body["legs"]) == 1
    assert body["legs"][0]["optionType"] is None
    assert body["underlying"]["symbol"] == "MSFT"
    assert "no_option_terms" in [notice["code"] for notice in body["notices"]]


def test_a_morning_entry_never_gets_its_own_session_close(client, db, cache):
    cache("ACN")
    backtest = _seed(db, SPREAD_LEGS)

    body = client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 1}
    ).json()

    entry = body["legs"][0]["entryUnderlying"]
    # The 09:30 ET entry cannot know the 2026-09-08 close, so the reference is the
    # last COMPLETED session -- an estimate with an age, not a fill-time quote.
    assert entry["quality"] == "last_known_bar"
    assert entry["price"] == 100.0
    assert entry["observedAt"].startswith("2026-09-04")
    assert entry["availableAt"] is not None

    exit_reference = body["legs"][0]["exitUnderlying"]
    assert exit_reference["quality"] == "last_known_bar"
    assert exit_reference["price"] == 105.0


def test_a_date_only_event_is_a_daily_reference(client, db, cache):
    cache("ACN")
    rows = [{**SPREAD_LEGS[0], "entry_time": "2026-09-08"}]
    backtest = _seed(db, rows)

    body = client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 1}
    ).json()

    entry = body["legs"][0]["entryUnderlying"]
    assert entry["quality"] == "daily_reference"
    assert entry["price"] == 101.0
    assert entry["availableAt"] is None


def test_a_cold_cache_is_a_notice_not_an_error(client, db, empty_cache):
    # An empty cache root: nothing on disk for this symbol.
    backtest = _seed(db, SPREAD_LEGS)

    response = client.get(URL.format(backtest_id=backtest.id), params={"trade_id": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["underlying"]["cacheStatus"] == "missing"
    assert body["underlying"]["bars"] == []
    assert "cache_missing" in [notice["code"] for notice in body["notices"]]
    # The leg table still carries everything the payoff needs.
    assert [leg["strike"] for leg in body["legs"]] == [95.0, 105.0]
    assert body["legs"][0]["entryUnderlying"]["quality"] == "unavailable"


def test_an_unresolvable_source_says_so_instead_of_guessing_a_provider(client, db):
    backtest = _seed(db, SPREAD_LEGS, engine_type="ml")

    body = client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 1}
    ).json()

    assert body["underlying"]["cacheStatus"] == "unavailable"
    assert body["underlying"]["provider"] is None
    assert "source_unresolved" in [notice["code"] for notice in body["notices"]]


def test_option_rows_without_an_underlying_are_not_charted_as_a_contract(client, db, cache):
    cache("ACN260918C00095000")
    row = {key: value for key, value in SPREAD_LEGS[0].items() if key != "underlying_symbol"}
    backtest = _seed(db, [row])

    body = client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 1}
    ).json()

    # The OCC string is present and is deliberately NOT used as the underlying.
    assert body["legs"][0]["contractSymbol"] == "ACN260918C00095000"
    assert body["underlying"]["symbol"] is None
    assert body["underlying"]["cacheStatus"] == "unavailable"
    assert "underlying_unresolved" in [notice["code"] for notice in body["notices"]]


def test_unrecorded_terms_stay_null_and_the_multiplier_is_not_certified(client, db, cache):
    cache("ACN")
    row = {
        key: value for key, value in SPREAD_LEGS[0].items()
        if key not in ("multiplier", "strike", "expiry")
    }
    backtest = _seed(db, [row])

    leg = client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 1}
    ).json()["legs"][0]

    assert leg["multiplier"] is None
    assert leg["multiplierRecorded"] is False
    assert leg["strike"] is None
    assert leg["expiry"] is None
    assert set(leg["unavailableFields"]) >= {"multiplier", "strike", "expiry"}


def test_a_recorded_multiplier_is_certified(client, db, cache):
    cache("ACN")
    backtest = _seed(db, [SPREAD_LEGS[0]])

    leg = client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 1}
    ).json()["legs"][0]

    assert leg["multiplier"] == 100.0
    assert leg["multiplierRecorded"] is True


def test_unknown_row_is_404_and_a_nonpositive_id_is_422(client, db):
    backtest = _seed(db, SPREAD_LEGS)

    assert client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 9}
    ).status_code == 404
    assert client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 0}
    ).status_code == 422
    assert client.get(
        URL.format(backtest_id=999999), params={"trade_id": 1}
    ).status_code == 404


def test_the_digest_tracks_the_saved_array(client, db, cache):
    cache("ACN")
    backtest = _seed(db, SPREAD_LEGS)
    first = client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 1}
    ).json()["resultDigest"]

    backtest.trades = [*SPREAD_LEGS, EQUITY_ROW]
    db.add(backtest)
    db.commit()

    second = client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 1}
    ).json()["resultDigest"]

    assert first != second


def test_open_at_end_is_not_reported_as_closed(client, db, cache):
    cache("ACN")
    row = {**SPREAD_LEGS[0], "exit_time": None, "exit_price": None, "exit_reason": "open_at_end"}
    backtest = _seed(db, [row])

    leg = client.get(
        URL.format(backtest_id=backtest.id), params={"trade_id": 1}
    ).json()["legs"][0]

    assert leg["positionStatus"] == "open_at_end"
    assert leg["exitUnderlying"]["quality"] == "unavailable"


# --------------------------------------------------------------------- step 11 ----
# Contract detail: the greeks/IV/OI the option CACHE recorded for a contract at an event.


def _seed_option_store(path, rows):
    """A real OptionsHistoryCache sqlite with the given option_bar rows."""
    import sqlite3

    from app.services.backtest.options_cache import OptionsHistoryCache

    OptionsHistoryCache(path)  # creates the tables (idempotent)
    connection = sqlite3.connect(path)
    for row in rows:
        connection.execute(
            "INSERT OR REPLACE INTO option_bar"
            "(occ_symbol,date,open,high,low,close,volume,underlying,option_type,strike,expiry,"
            " iv,delta,gamma,theta,vega) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    connection.commit()
    connection.close()
    return path


CONTRACT = 'ACN260918C00095000'


def _bar(date, iv=0.42, delta=0.55, gamma=0.02, theta=-0.08, vega=0.11):
    return (CONTRACT, date, 8.0, 9.0, 7.5, 8.5, 120, 'ACN', 'call', 95.0, '2026-09-18',
            iv, delta, gamma, theta, vega)


@pytest.fixture
def option_store(tmp_path):
    return _seed_option_store(str(tmp_path / 'options.sqlite'), [
        _bar('2026-09-04', iv=0.40, delta=0.50),
        _bar('2026-09-08', iv=0.44, delta=0.58),
        _bar('2026-09-10', iv=0.50, delta=0.70),
    ])


class TestContractDetail:
    def test_a_leg_gets_the_cached_greeks_at_its_event(self, client, db, cache, option_store,
                                                      monkeypatch):
        monkeypatch.setattr('app.services.backtest_trade_chart.options_store_path',
                            lambda backtest: option_store)
        cache('ACN')
        backtest = _seed(db, [SPREAD_LEGS[0]])

        leg = client.get(URL.format(backtest_id=backtest.id),
                         params={'trade_id': 1}).json()['legs'][0]

        entry = leg['entryContract']
        assert entry['quality'] == 'cache_bar'
        assert entry['asOf'] == '2026-09-08'
        assert entry['iv'] == pytest.approx(0.44)
        assert entry['delta'] == pytest.approx(0.58)
        assert entry['gamma'] is not None and entry['theta'] is not None and entry['vega'] is not None
        assert entry['source'] == option_store

    def test_the_lookup_never_looks_forward(self, client, db, cache, tmp_path, monkeypatch):
        # Only a LATER bar exists: it is not evidence about the entry.
        store = _seed_option_store(str(tmp_path / 'later.sqlite'), [_bar('2026-09-09')])
        monkeypatch.setattr('app.services.backtest_trade_chart.options_store_path',
                            lambda backtest: store)
        cache('ACN')
        backtest = _seed(db, [SPREAD_LEGS[0]])

        entry = client.get(URL.format(backtest_id=backtest.id),
                           params={'trade_id': 1}).json()['legs'][0]['entryContract']

        assert entry['quality'] == 'unavailable'
        assert entry['iv'] is None
        assert 'at or before' in entry['reason']

    def test_a_bar_without_fetched_greeks_stays_null(self, client, db, cache, tmp_path, monkeypatch):
        store = _seed_option_store(str(tmp_path / 'nulls.sqlite'),
                                   [_bar('2026-09-04', iv=None, delta=None, gamma=None,
                                         theta=None, vega=None)])
        monkeypatch.setattr('app.services.backtest_trade_chart.options_store_path',
                            lambda backtest: store)
        cache('ACN')
        backtest = _seed(db, [SPREAD_LEGS[0]])

        entry = client.get(URL.format(backtest_id=backtest.id),
                           params={'trade_id': 1}).json()['legs'][0]['entryContract']

        assert entry['quality'] == 'partial'
        assert entry['iv'] is None and entry['delta'] is None
        assert 'never fetched' in entry['reason']

    def test_an_unresolvable_store_says_so_and_leaves_the_rest_working(self, client, db, cache):
        cache('ACN')
        backtest = _seed(db, [SPREAD_LEGS[0]])

        body = client.get(URL.format(backtest_id=backtest.id), params={'trade_id': 1}).json()

        assert body['legs'][0]['entryContract']['quality'] == 'unavailable'
        assert 'option_store_unresolved' in [notice['code'] for notice in body['notices']]
        # The terms, the bars and the money are all still there.
        assert body['legs'][0]['strike'] == 95.0
        assert body['legs'][0]['entryPrice'] == 8.0
        assert body['underlying']['bars']


class TestStoreResolution:
    def test_a_path_on_the_row_is_used(self):
        from app.services.backtest_trade_chart import options_store_path

        assert options_store_path(SimpleNamespace(options_cache_db=' C:/x/o.sqlite ')) == 'C:/x/o.sqlite'

    def test_a_path_inside_a_saved_config_block_is_used(self):
        from app.services.backtest_trade_chart import options_store_path

        assert options_store_path(
            SimpleNamespace(settings={'options_cache_db': 'C:/y/o.sqlite'})) == 'C:/y/o.sqlite'

    def test_no_path_anywhere_is_none_rather_than_a_platform_default(self):
        from app.services.backtest_trade_chart import options_store_path

        assert options_store_path(SimpleNamespace()) is None
        assert options_store_path(SimpleNamespace(settings={'other': 1})) is None

    def test_contract_detail_without_a_reader_explains_itself(self):
        from app.services.backtest_trade_chart import contract_detail

        detail = contract_detail(None, CONTRACT, datetime(2026, 9, 8, 13, 30))
        assert detail['quality'] == 'unavailable'
        assert detail['iv'] is None

