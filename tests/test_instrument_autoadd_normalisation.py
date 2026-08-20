"""Background instrument creation must store one normalised row per symbol.

Both writers are exercised for real against the in-memory test DB: the
InstrumentAutoAdder coroutine (with Yahoo lookup stubbed, so the test is offline)
and JobManager.ensure_instrument_exists.
"""
import asyncio

from sqlmodel import select

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Instrument
from ba2_trade_platform.core.InstrumentAutoAdder import InstrumentAutoAdder
from ba2_trade_platform.core.JobManager import ensure_instrument_exists


def _names():
    with get_db() as session:
        return sorted(i.name for i in session.exec(select(Instrument)).all())


def _run_auto_add(symbol):
    """Drive one auto-add with the network call replaced by a canned payload."""
    adder = InstrumentAutoAdder()

    async def fake_fetch(sym):
        return {'name': sym, 'category': 'Technology', 'company_name': 'Fake Corp'}

    adder._fetch_instrument_data = fake_fetch
    asyncio.run(adder._add_instrument_if_missing(symbol, 'expert-1', 'expert', []))


def test_auto_added_instrument_is_stored_under_the_normalised_name():
    _run_auto_add('  aapl  ')
    assert _names() == ['AAPL']


def test_auto_add_of_a_case_variant_updates_the_existing_row():
    _run_auto_add('AAPL')
    _run_auto_add('aapl')
    assert _names() == ['AAPL']
    with get_db() as session:
        inst = session.exec(select(Instrument).where(Instrument.name == 'AAPL')).first()
    assert inst.labels.count('expert-1') == 1


def test_auto_add_of_a_blank_symbol_creates_nothing():
    _run_auto_add('   ')
    assert _names() == []


def test_ensure_instrument_exists_creates_the_normalised_row_and_returns_it():
    assert ensure_instrument_exists(' tsla ') == 'TSLA'
    assert _names() == ['TSLA']


def test_ensure_instrument_exists_of_a_blank_symbol_creates_nothing():
    assert ensure_instrument_exists('  ') == ''
    assert _names() == []


def test_ensure_instrument_exists_is_idempotent_across_case():
    ensure_instrument_exists('TSLA')
    ensure_instrument_exists('tsla')
    assert _names() == ['TSLA']
