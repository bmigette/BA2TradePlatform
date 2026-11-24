"""The dividend forecast's FMP histories survive a restart (``AccountGrowthTab._provider_dividend_histories``).

Before: FMP's declared dividend history was cached in memory only, so the first Account Growth load after a restart
fetched every held symbol (12.7 s of a 16.3 s load on an 89-position account). Now the RAW payloads are stored: a
stored one is used whatever its age, a missing one is fetched and stored, a stale one is refreshed in ONE background
thread after the forecast has its answer. A FILE database: the refresh thread must see the same rows."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import SQLModel, create_engine

from ba2_trade_platform.services import growth_snapshot as gs

UTC = timezone.utc


def _payload(per_share=0.2, symbol='X'):
    return {'symbol': symbol, 'historical': [{'date': f'2026-0{m}-10', 'dividend': per_share,
                                              'paymentDate': f'2026-0{m}-25'} for m in range(1, 10)]}


@pytest.fixture
def file_db(tmp_path):
    import ba2_common.core.db as pkg_db
    engine = create_engine(f"sqlite:///{tmp_path / 'hist.sqlite'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    saved = pkg_db._engine
    pkg_db._engine = engine
    try:
        yield engine
    finally:
        gs.join_dividend_history_refresh(timeout=30)
        pkg_db._engine = saved
        engine.dispose()


@pytest.fixture
def fmp(monkeypatch, file_db):
    """A fake FMP: ``fmp.answers[symbol]`` is the payload (or an exception to raise); ``fmp.calls`` records asks."""
    import ba2_providers.symbol_info as si
    import ba2_trade_platform.config as cfg
    from types import SimpleNamespace
    state = SimpleNamespace(calls=[], answers={}, key='KEY')
    monkeypatch.setattr(cfg, 'get_app_setting', lambda k, d=None: state.key)

    def fetch(key, sym):
        state.calls.append(sym)
        answer = state.answers.get(sym, {})
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(si, 'fetch_dividends', fetch)
    return state


def _tab():
    import ba2_trade_platform.ui.pages.overview as ov
    return ov.AccountGrowthTab.__new__(ov.AccountGrowthTab)


def test_a_stored_payload_is_used_without_asking_fmp(fmp):
    gs.save_dividend_history('AAA', _payload(), datetime.now(UTC) - timedelta(hours=1))
    got = _tab()._provider_dividend_histories(['AAA'])
    assert fmp.calls == [] and set(got) == {'AAA'}
    hist, kind = got['AAA']
    assert kind == 'pay' and hist[0][1] == 0.2


def test_a_symbol_with_no_stored_row_is_fetched_and_stored(fmp):
    fmp.answers['BBB'] = _payload(0.3, 'BBB')
    got = _tab()._provider_dividend_histories(['BBB'])
    assert fmp.calls == ['BBB'] and set(got) == {'BBB'}
    payload, fetched_at = gs.load_dividend_histories(['BBB'])['BBB']
    assert payload == _payload(0.3, 'BBB')                                   # RAW, as FMP returned it
    assert (datetime.now(UTC) - fetched_at).total_seconds() < 60
    again = _tab()._provider_dividend_histories(['BBB'])                    # a new page, after a "restart"
    assert fmp.calls == ['BBB'] and set(again) == {'BBB'}


def test_a_non_payer_is_stored_empty_and_not_asked_again_after_a_restart(fmp):
    fmp.answers['NOPAY'] = {}
    assert _tab()._provider_dividend_histories(['NOPAY']) == {}
    assert gs.load_dividend_histories(['NOPAY'])['NOPAY'][0] == {}
    assert _tab()._provider_dividend_histories(['NOPAY']) == {}
    assert fmp.calls == ['NOPAY']


def test_a_stale_row_is_used_at_once_and_refreshed_by_the_background_thread(fmp):
    old = datetime.now(UTC) - timedelta(hours=30)
    gs.save_dividend_history('OLD', _payload(0.2, 'OLD'), old)
    gs.save_dividend_history('NEW', _payload(0.4, 'NEW'), datetime.now(UTC) - timedelta(hours=2))
    fmp.answers['OLD'] = _payload(0.25, 'OLD')
    tab = _tab()
    got = tab._provider_dividend_histories(['NEW', 'OLD'])
    assert got['OLD'][0][0][1] == 0.2                                       # the stored copy, not a wait for FMP
    thread = tab._dividend_history_refresh
    assert thread is not None and thread.daemon
    thread.join(10)
    assert fmp.calls == ['OLD']                                             # only the stale one
    payload, fetched_at = gs.load_dividend_histories(['OLD'])['OLD']
    assert payload == _payload(0.25, 'OLD') and fetched_at > old


def test_no_key_means_no_fetch_and_no_refresh_but_stored_rows_still_count(fmp):
    fmp.key = None
    gs.save_dividend_history('OLD', _payload(0.2, 'OLD'), datetime.now(UTC) - timedelta(hours=30))
    tab = _tab()
    got = tab._provider_dividend_histories(['OLD', 'MISSING'])
    assert set(got) == {'OLD'} and fmp.calls == []
    assert getattr(tab, '_dividend_history_refresh', None) is None


def test_a_provider_error_warns_once_stores_nothing_and_falls_back(fmp, monkeypatch):
    import ba2_trade_platform.ui.pages.overview as ov
    warned = []
    monkeypatch.setattr(ov.logger, 'warning', lambda msg, *a, **k: warned.append(str(msg)))
    fmp.answers['A'] = RuntimeError('429')
    fmp.answers['B'] = RuntimeError('429')
    assert _tab()._provider_dividend_histories(['A', 'B']) == {}
    assert len([m for m in warned if 'provider history unavailable' in m]) == 1
    assert gs.load_dividend_histories(['A', 'B']) == {}


def test_an_unstorable_payload_is_a_warning_and_the_forecast_still_has_it(fmp, monkeypatch):
    import ba2_trade_platform.ui.pages.overview as ov
    warned = []
    monkeypatch.setattr(ov.logger, 'warning', lambda msg, *a, **k: warned.append(str(msg)))
    fmp.answers['ERR'] = {'Error Message': 'Limit Reach'}
    fmp.answers['OK'] = _payload(0.1, 'OK')
    got = _tab()._provider_dividend_histories(['ERR', 'OK'])
    assert set(got) == {'OK'}
    assert any('ERR' in m and 'could not store' in m for m in warned), warned
    assert set(gs.load_dividend_histories(['ERR', 'OK'])) == {'OK'}
