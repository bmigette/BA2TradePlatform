"""The rebalance tester's progressive download (``services/rebalance_data.py``): progress once per symbol, Cancel,
failed symbols named, the shared download slots, and -- with a spy, no network -- that every FMP request on the path
goes through ``ba2_providers.fmp_common.fmp_http_get`` (the shared 429/5xx backoff and global gate)."""
import threading
from datetime import date

import pandas as pd
import pytest

from ba2_providers import fmp_common, symbol_info
from ba2_trade_platform.services import label_growth, price_history
from ba2_trade_platform.services import rebalance_data as svc

AS_OF = date(2026, 11, 12)


class _Provider:
    """A history provider: a few daily bars per symbol, ``fail`` raises, ``empty`` answers nothing."""

    def __init__(self, fail=(), empty=()):
        self.fail, self.empty, self.calls = set(fail), set(empty), []

    def get_ohlcv_data(self, symbol, start_date, end_date, interval):
        self.calls.append(symbol)
        assert interval == '1d'
        if symbol in self.fail:
            raise ConnectionError(f"provider down for {symbol}?apikey=SECRET9")
        if symbol in self.empty:
            return pd.DataFrame(columns=['Date', 'Close'])
        return pd.DataFrame({'Date': pd.to_datetime(['2026-11-09', '2026-11-10']), 'Close': [10.0, 11.0]})


@pytest.fixture
def wired(monkeypatch):
    provider = _Provider()
    dividends = []
    monkeypatch.setattr(price_history, '_provider', lambda account_id: provider)
    monkeypatch.setattr(label_growth, '_history_source', lambda account_id: 'fmp')

    def fake_dividends(api_key, symbol):
        dividends.append(symbol)
        return {'symbol': symbol, 'historical': [{'date': '2026-06-01', 'adjDividend': 0.25, 'dividend': 0.25}]}

    monkeypatch.setattr(symbol_info, 'fetch_dividends', fake_dividends)
    return provider, dividends


def test_progress_is_reported_once_per_symbol_with_the_right_counts(wired):
    provider, dividends = wired
    seen = []
    result = svc.fetch_rebalance_inputs(1, ['msft', 'AAPL', 'JEPI', 'AAPL'], as_of=AS_OF, api_key='K',
                                        on_progress=lambda done, total, sym: seen.append((done, total, sym)))
    assert seen == [(0, 3, 'AAPL'), (1, 3, 'JEPI'), (2, 3, 'MSFT')]
    assert result.completed == ['AAPL', 'JEPI', 'MSFT'] and result.total == 3 and not result.cancelled
    assert result.closes['AAPL'] == {'2026-11-09': 10.0, '2026-11-10': 11.0}
    assert result.distributions['MSFT'].events == (('2026-06-01', 0.25),)
    assert provider.calls == dividends == ['AAPL', 'JEPI', 'MSFT']
    assert result.price_source == 'fmp' and result.failed == {}


def test_cancel_stops_further_fetches_and_keeps_what_finished(wired):
    provider, _dividends = wired
    cancel = threading.Event()

    def progress(done, total, sym):
        if sym == 'JEPI':
            cancel.set()                      # pressed while JEPI was about to start

    result = svc.fetch_rebalance_inputs(1, ['AAPL', 'JEPI', 'MSFT'], as_of=AS_OF, api_key='K',
                                        on_progress=progress, cancel=cancel)
    assert result.cancelled and result.completed == ['AAPL']
    assert provider.calls == ['AAPL']         # nothing after the cancel was asked for
    assert list(result.closes) == ['AAPL']


def test_a_failed_symbol_is_named_redacted_and_not_cached(wired):
    provider, dividends = wired
    provider.fail.add('JEPI')
    result = svc.fetch_rebalance_inputs(1, ['AAPL', 'JEPI', 'MSFT'], as_of=AS_OF, api_key='K')
    assert list(result.failed) == ['JEPI']
    assert 'ConnectionError' in result.failed['JEPI'] and 'SECRET9' not in result.failed['JEPI']
    assert result.completed == ['AAPL', 'MSFT'] and 'JEPI' not in result.closes
    assert dividends == ['AAPL', 'MSFT']


def test_a_failed_distribution_download_fails_the_symbol(wired, monkeypatch):
    monkeypatch.setattr(symbol_info, 'fetch_dividends',
                        lambda api_key, symbol: (_ for _ in ()).throw(symbol_info.FMPError('HTTP 503 x4')))
    result = svc.fetch_rebalance_inputs(1, ['AAPL'], as_of=AS_OF, api_key='K')
    assert 'HTTP 503' in result.failed['AAPL'] and result.completed == []


def test_an_empty_answer_is_complete_but_unpriced_and_asks_no_dividends(wired):
    provider, dividends = wired
    provider.empty.add('NEW')
    result = svc.fetch_rebalance_inputs(1, ['AAPL', 'NEW'], as_of=AS_OF, api_key='K')
    assert result.completed == ['AAPL', 'NEW'] and 'NEW' not in result.closes and result.failed == {}
    assert dividends == ['AAPL']


def test_one_shared_slot_per_symbol_released_and_cancel_while_waiting(wired, monkeypatch):
    held = []
    real_daily = price_history.daily_closes

    def spying_daily(provider, symbol, start, **kw):
        # while a symbol downloads, exactly one of the shared slots is taken by this path
        free = sum(1 for _ in range(price_history.MAX_CONCURRENT) if price_history._SLOTS.acquire(blocking=False))
        for _ in range(free):
            price_history._SLOTS.release()
        held.append(price_history.MAX_CONCURRENT - free)
        return real_daily(provider, symbol, start, **kw)

    monkeypatch.setattr(price_history, 'daily_closes', spying_daily)
    svc.fetch_rebalance_inputs(1, ['AAPL', 'MSFT'], as_of=AS_OF, api_key='K')
    assert held == [1, 1]
    # every slot back afterwards
    got = sum(1 for _ in range(price_history.MAX_CONCURRENT) if price_history._SLOTS.acquire(blocking=False))
    for _ in range(got):
        price_history._SLOTS.release()
    assert got == price_history.MAX_CONCURRENT

    # all slots busy (other users of the price path): it WAITS, and Cancel ends the wait cleanly
    monkeypatch.setattr(svc, 'SLOT_POLL_S', 0.01)
    for _ in range(price_history.MAX_CONCURRENT):
        assert price_history._SLOTS.acquire(blocking=False)
    try:
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        result = svc.fetch_rebalance_inputs(1, ['AAPL'], as_of=AS_OF, api_key='K', cancel=cancel)
        timer.join()
    finally:
        for _ in range(price_history.MAX_CONCURRENT):
            price_history._SLOTS.release()
    assert result.cancelled and result.completed == []


def test_no_fmp_key_is_refused():
    with pytest.raises(ValueError, match='FMP_API_KEY'):
        svc.fetch_rebalance_inputs(1, ['AAPL'], as_of=AS_OF, api_key='')


def test_every_fmp_request_on_the_path_goes_through_the_shared_backoff(monkeypatch):
    """The REAL FMP history provider and the REAL dividend fetch, with ``requests.get`` faked (no network) and
    ``fmp_http_get`` spied: each raw GET must happen INSIDE ``fmp_http_get``. Endpoints: historical-price-full (closes)
    and stock_dividend (distributions). Disk cache of the OHLCV provider bypassed (no files written)."""
    from ba2_providers.ohlcv.FMPOHLCVProvider import FMPOHLCVProvider

    provider = FMPOHLCVProvider(api_key='KEY-NOT-REAL')
    monkeypatch.setattr(provider, 'get_ohlcv_data', provider._get_ohlcv_data_impl)
    monkeypatch.setattr(price_history, '_provider', lambda account_id: provider)
    monkeypatch.setattr(label_growth, '_history_source', lambda account_id: 'fmp')

    depth = {'n': 0}
    through_helper, raw_gets = [], []
    real_http_get = fmp_common.fmp_http_get

    def spy(url, params=None, **kw):
        through_helper.append(kw.get('endpoint'))
        depth['n'] += 1
        try:
            return real_http_get(url, params=params, **kw)
        finally:
            depth['n'] -= 1

    class _Resp:
        status_code, headers = 200, {}

        def __init__(self, payload):
            self._payload, self.content = payload, b'{}'

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    def fake_get(url, params=None, timeout=None):
        raw_gets.append((url.rsplit('/', 2)[-2], depth['n']))
        sym = url.rsplit('/', 1)[-1]
        if '/stock_dividend/' in url:
            return _Resp({'symbol': sym, 'historical': [{'date': '2026-03-02', 'adjDividend': 0.1}]})
        return _Resp({'symbol': sym, 'historical': [
            {'date': '2026-11-10', 'open': 1, 'high': 1, 'low': 1, 'close': 21.0, 'volume': 5},
            {'date': '2026-11-09', 'open': 1, 'high': 1, 'low': 1, 'close': 20.0, 'volume': 5}]})

    monkeypatch.setattr(fmp_common, 'fmp_http_get', spy)          # the OHLCV provider imports it at call time
    monkeypatch.setattr(symbol_info, 'fmp_http_get', spy)         # symbol_info bound it at import
    monkeypatch.setattr(fmp_common.requests, 'get', fake_get)
    symbols = ['ZZRBTA', 'ZZRBTB']                                 # unique: never in the 24 h dividend cache
    result = svc.fetch_rebalance_inputs(1, symbols, as_of=AS_OF, api_key='KEY-NOT-REAL')

    assert result.failed == {} and result.completed == symbols
    assert result.closes['ZZRBTA'] == {'2026-11-09': 20.0, '2026-11-10': 21.0}
    assert result.distributions['ZZRBTB'].events == (('2026-03-02', 0.1),)
    assert sorted(through_helper) == ['historical-price-full', 'historical-price-full', 'stock_dividend',
                                      'stock_dividend']
    assert len(raw_gets) == 4 and all(d == 1 for _ep, d in raw_gets)    # every raw GET happened inside the helper
