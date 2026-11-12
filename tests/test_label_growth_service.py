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

    def fake_fetch_closes(by_account, start, **kw):
        calls['closes'].append((by_account, start))
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
