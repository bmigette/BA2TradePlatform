"""``services/label_growth.py``: the fetch behind the label growth simulator. No network: the price source and the FMP
dividend fetcher are replaced."""
from datetime import date

import pytest

from ba2_trade_platform.services import label_growth as svc
from ba2_trade_platform.services import price_history
from ba2_trade_platform.services.errors import PriceSourceUnreachable
from ba2_trade_platform.ui.utils.label_growth_sim import history_start

AS_OF = date(2026, 11, 12)


def _closes(data, *, timed_out=False, unfetched=()):
    out = price_history.Closes(data)
    out.timed_out = timed_out
    out.unfetched = list(unfetched)
    return out


@pytest.fixture
def wired(monkeypatch):
    """Record the price-history call and serve canned dividend payloads."""
    calls = {'closes': [], 'dividends': []}
    state = {'closes': _closes({'AAA': {'2025-11-12': 10.0}, 'BBB': {'2025-11-12': 20.0}}),
             'payloads': {}}

    def fake_fetch_closes(by_account, start, *, errors=None, **kw):
        calls['closes'].append((by_account, start))
        if errors is not None:
            errors.update(state.get('price_errors', {}))
        return state['closes']

    def fake_fetch_dividends(api_key, symbol):
        calls['dividends'].append((api_key, symbol))
        payload = state['payloads'].get(symbol, {})
        if isinstance(payload, Exception):
            raise payload
        return payload

    import ba2_providers.symbol_info as symbol_info
    monkeypatch.setattr(price_history, 'fetch_closes', fake_fetch_closes)
    monkeypatch.setattr(symbol_info, 'fetch_dividends', fake_fetch_dividends)
    monkeypatch.setattr(svc, '_history_source', lambda account_id: 'fmp')
    return calls, state


def test_the_closes_come_from_the_accounts_history_source_from_before_the_5y_start(wired):
    calls, _state = wired
    out = svc.fetch_label_growth_inputs(7, ['bbb', 'AAA', 'AAA'], as_of=AS_OF, api_key='K')
    assert calls['closes'] == [({7: ['AAA', 'BBB']}, history_start(AS_OF))]
    assert out.closes == {'AAA': {'2025-11-12': 10.0}, 'BBB': {'2025-11-12': 20.0}}
    assert out.price_source == 'fmp'
    assert sorted(s for _k, s in calls['dividends']) == ['AAA', 'BBB']


def test_distributions_use_adjDividend_never_the_as_declared_amount(wired):
    _calls, state = wired
    state['payloads'] = {'AAA': {'symbol': 'AAA', 'historical': [
        {'date': '2020-08-07', 'adjDividend': 0.205, 'dividend': 0.82},
        {'date': '2024-08-12', 'adjDividend': 0.25, 'dividend': 0.25},
        {'date': '2024-11-08', 'dividend': 0.25},                       # no adjDividend: unknown, not 0.25
    ]}}
    out = svc.fetch_label_growth_inputs(1, ['AAA'], as_of=AS_OF, api_key='K')
    assert out.distributions['AAA'].events == (('2020-08-07', 0.205), ('2024-08-12', 0.25), ('2024-11-08', None))


def test_a_symbol_that_pays_nothing_has_an_empty_history_not_an_unknown_one(wired):
    out = svc.fetch_label_growth_inputs(1, ['AAA'], as_of=AS_OF, api_key='K')
    assert out.distributions['AAA'].events == ()
    assert out.distributions['AAA'].error is None


def test_a_failed_dividend_fetch_is_recorded_per_symbol_and_logged(wired, monkeypatch):
    from ba2_providers.symbol_info import FMPError
    _calls, state = wired
    state['payloads'] = {'BBB': FMPError('HTTP 503 after retries')}
    errors = []
    monkeypatch.setattr(svc.logger, 'error', lambda msg, *a, **k: errors.append(str(msg)))
    out = svc.fetch_label_growth_inputs(1, ['AAA', 'BBB'], as_of=AS_OF, api_key='K')
    assert out.distributions['AAA'].events == ()
    assert out.distributions['BBB'].events is None
    assert 'HTTP 503' in out.distributions['BBB'].error
    assert any('BBB' in m for m in errors)


def test_an_fmp_error_body_is_an_error_not_an_empty_history(wired):
    _calls, state = wired
    state['payloads'] = {'AAA': {'Error Message': 'Limit Reach'}}
    out = svc.fetch_label_growth_inputs(1, ['AAA'], as_of=AS_OF, api_key='K')
    assert out.distributions['AAA'].events is None
    assert 'Limit Reach' in out.distributions['AAA'].error


def test_a_deadline_with_symbols_unfetched_is_refused_not_reported_as_no_history(wired):
    _calls, state = wired
    state['closes'] = _closes({'AAA': {'2025-11-12': 10.0}}, timed_out=True, unfetched=['BBB'])
    with pytest.raises(PriceSourceUnreachable, match='BBB'):
        svc.fetch_label_growth_inputs(1, ['AAA', 'BBB'], as_of=AS_OF, api_key='K')


def test_no_history_for_any_symbol_is_refused(wired):
    _calls, state = wired
    state['closes'] = _closes({})
    with pytest.raises(PriceSourceUnreachable):
        svc.fetch_label_growth_inputs(1, ['AAA', 'BBB'], as_of=AS_OF, api_key='K')


def test_one_symbol_without_history_is_absent_and_the_rest_is_served(wired):
    _calls, state = wired
    state['closes'] = _closes({'AAA': {'2025-11-12': 10.0}})
    out = svc.fetch_label_growth_inputs(1, ['AAA', 'BBB'], as_of=AS_OF, api_key='K')
    assert 'BBB' not in out.closes and 'AAA' in out.closes


def test_an_unreachable_price_source_propagates(wired, monkeypatch):
    def boom(by_account, start, **kw):
        raise PriceSourceUnreachable('The price source is busy', {'busy': True})
    monkeypatch.setattr(price_history, 'fetch_closes', boom)
    with pytest.raises(PriceSourceUnreachable, match='busy'):
        svc.fetch_label_growth_inputs(1, ['AAA'], as_of=AS_OF, api_key='K')


def test_without_an_fmp_key_nothing_is_fetched(wired):
    calls, _state = wired
    with pytest.raises(ValueError, match='FMP'):
        svc.fetch_label_growth_inputs(1, ['AAA'], as_of=AS_OF, api_key='')
    assert calls['closes'] == [] and calls['dividends'] == []


def test_only_symbols_with_a_positive_share_are_fetched():
    assert svc.symbols_to_fetch({'A': 50.0, 'B': 0.0, 'C': 50.0}) == ['A', 'C']


# ---------------------------------------------------------------------------
# review 2026-11-12
# ---------------------------------------------------------------------------

def test_one_failed_price_download_refuses_the_whole_result_naming_it(wired):
    """A transient 5xx / timeout on one symbol is NOT 'no history': its slice would sit in cash and dilute every
    horizon's headline under a false label. The fetch refuses, naming the symbol and the (redacted) reason."""
    _calls, state = wired
    state['closes'] = _closes({'AAA': {'2025-11-12': 10.0}})
    state['price_errors'] = {'BBB': 'HTTPError: 503 for url https://x/y?apikey=SECRET123'}
    with pytest.raises(PriceSourceUnreachable) as err:
        svc.fetch_label_growth_inputs(1, ['AAA', 'BBB'], as_of=AS_OF, api_key='K')
    text = str(err.value)
    assert 'BBB' in text and 'price fetch failed' in text and '503' in text
    assert 'SECRET123' not in text


def test_fetch_closes_reports_per_symbol_errors_only_to_a_caller_that_asks(monkeypatch):
    """The Account Growth callers pass no ``errors``: what they receive is unchanged (the failed symbol absent)."""
    import pandas as pd

    class _Provider:
        def get_ohlcv_data(self, symbol, start_date=None, end_date=None, interval='1d'):
            if symbol == 'BAD':
                raise RuntimeError('HTTP 502 at https://h/p?apikey=SECRET123')
            return pd.DataFrame({'Date': [pd.Timestamp('2026-11-11')], 'Close': [5.0]})

    monkeypatch.setattr(price_history, '_provider', lambda account_id: _Provider())
    plain = price_history.fetch_closes({1: ['BAD', 'OK']}, date(2026, 11, 1))
    assert dict(plain) == {'OK': {'2026-11-11': 5.0}}
    errors = {}
    asked = price_history.fetch_closes({1: ['BAD', 'OK']}, date(2026, 11, 1), errors=errors)
    assert dict(asked) == dict(plain)
    assert list(errors) == ['BAD']
    assert 'RuntimeError' in errors['BAD'] and '502' in errors['BAD'] and 'SECRET123' not in errors['BAD']


def test_a_failed_dividend_reason_is_redacted(wired):
    from ba2_providers.symbol_info import FMPError
    _calls, state = wired
    state['payloads'] = {'AAA': FMPError('403 for https://fmp/x?apikey=SECRET123')}
    out = svc.fetch_label_growth_inputs(1, ['AAA'], as_of=AS_OF, api_key='K')
    assert 'SECRET123' not in out.distributions['AAA'].error and '403' in out.distributions['AAA'].error


def test_the_dividend_fetch_has_a_deadline_and_a_late_symbol_is_unknown_not_zero(wired, monkeypatch):
    import threading
    _calls, state = wired
    release = threading.Event()
    import ba2_providers.symbol_info as symbol_info

    def slow(api_key, symbol):
        if symbol == 'BBB':
            release.wait(5)
        return {}

    monkeypatch.setattr(symbol_info, 'fetch_dividends', slow)
    monkeypatch.setattr(svc, 'DIVIDEND_DEADLINE_S', 0.3)
    try:
        out = svc.fetch_label_growth_inputs(1, ['AAA', 'BBB'], as_of=AS_OF, api_key='K')
    finally:
        release.set()
    assert out.distributions['AAA'].events == ()
    assert out.distributions['BBB'].events is None
    assert 'within' in out.distributions['BBB'].error


def test_the_dividend_fetch_shares_the_price_paths_concurrency_cap(wired, monkeypatch):
    import threading
    import time
    _calls, _state = wired
    lock, live = threading.Lock(), {'now': 0, 'max': 0}
    import ba2_providers.symbol_info as symbol_info

    def counting(api_key, symbol):
        with lock:
            live['now'] += 1
            live['max'] = max(live['max'], live['now'])
        time.sleep(0.05)
        with lock:
            live['now'] -= 1
        return {}

    monkeypatch.setattr(symbol_info, 'fetch_dividends', counting)
    syms = [f'S{i}' for i in range(12)]
    _state['closes'] = _closes({s: {'2025-11-12': 1.0} for s in syms})
    out = svc.fetch_label_growth_inputs(1, syms, as_of=AS_OF, api_key='K')
    assert all(out.distributions[s].events == () for s in syms)
    assert 1 <= live['max'] <= price_history.MAX_CONCURRENT


def test_with_every_download_slot_taken_the_distributions_are_unknown_and_say_busy(wired):
    _calls, _state = wired
    held = 0
    while price_history._SLOTS.acquire(blocking=False):
        held += 1
    try:
        out = svc.fetch_label_growth_inputs(1, ['AAA'], as_of=AS_OF, api_key='K')
    finally:
        for _ in range(held):
            price_history._SLOTS.release()
    assert out.distributions['AAA'].events is None
    assert 'busy' in out.distributions['AAA'].error


def test_redacted_reason_masks_credentials_in_any_provider_message():
    text = svc.redacted_reason(RuntimeError('GET https://a/b?apikey=K1&token=T2 Authorization: Bearer abcdefgh123'))
    for secret in ('K1', 'T2', 'abcdefgh123'):
        assert secret not in text
    assert text.startswith('RuntimeError: ')
