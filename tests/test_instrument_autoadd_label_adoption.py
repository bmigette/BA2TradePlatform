"""What the auto-adder is allowed to write onto an instrument it did NOT create.

Two separate concerns, both exercised against the real ``_add_instrument_if_missing``
coroutine with the Yahoo lookup stubbed out (so the tests are offline):

1. The write must actually persist. ``Instrument.labels`` is a plain
   ``Column(JSON)`` with no ``MutableList`` wrapper, so an in-place
   ``existing.labels.append(...)`` leaves SQLAlchemy no attribute history: the
   commit emits no UPDATE and the label is silently lost. The list has to be
   REASSIGNED, exactly as the four helpers in ``ba2_common.core.utils`` do.

2. The write must be narrow. ``auto_added`` / ``expert_selected`` / ``ai_selected``
   describe how a ROW WAS CREATED, and the create path stamps them itself. Putting
   them on a row the user added by hand is simply false, so they are filtered out
   of what gets adopted onto a pre-existing row.

Every assertion re-reads the row in a FRESH session. Asserting on the object the
auto-adder touched would pass even with the bug present, because the lost append
is still visible on the in-memory instance.
"""
import asyncio

from sqlmodel import select

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Instrument
from ba2_trade_platform.core.InstrumentAutoAdder import InstrumentAutoAdder


def _seed(name, labels):
    """Insert an instrument the way a user would: by hand, with its own labels."""
    with get_db() as session:
        session.add(Instrument(name=name, labels=list(labels)))
        session.commit()


def _labels(name):
    """Re-read the row's labels from the database in a brand-new session."""
    with get_db() as session:
        row = session.exec(select(Instrument).where(Instrument.name == name)).first()
        return list(row.labels or []) if row else None


def _run_auto_add(symbol, expert_shortname='', source='expert', extra_labels=None):
    adder = InstrumentAutoAdder()

    async def fake_fetch(sym):
        return {'name': sym, 'category': 'Technology', 'company_name': 'Fake Corp'}

    adder._fetch_instrument_data = fake_fetch
    asyncio.run(adder._add_instrument_if_missing(
        symbol, expert_shortname, source, extra_labels or []))


# ---------------------------------------------------------------------------
# 1. The write must persist
# ---------------------------------------------------------------------------

def test_expert_label_is_persisted_onto_a_pre_existing_instrument():
    """The regression: the expert shortname used to be appended in place and lost."""
    _seed('AAPL', ['ARK26'])

    _run_auto_add('AAPL', expert_shortname='tradingagents-16')

    assert _labels('AAPL') == ['ARK26', 'tradingagents-16']


def test_a_user_label_passed_as_an_extra_is_persisted_too():
    _seed('MSFT', ['ARK26'])

    _run_auto_add('MSFT', expert_shortname='', extra_labels=['NASDAQ30'])

    assert _labels('MSFT') == ['ARK26', 'NASDAQ30']


def test_adoption_survives_a_case_variant_lookup():
    """' aapl ' normalises onto the existing AAPL row, and still persists there."""
    _seed('AAPL', ['ARK26'])

    _run_auto_add('  aapl  ', expert_shortname='penny-17')

    assert _labels('AAPL') == ['ARK26', 'penny-17']


def test_a_second_expert_adds_a_second_label_rather_than_replacing_the_first():
    _seed('AAPL', ['ARK26'])

    _run_auto_add('AAPL', expert_shortname='penny-17')
    _run_auto_add('AAPL', expert_shortname='fmprating-18')

    assert _labels('AAPL') == ['ARK26', 'penny-17', 'fmprating-18']


def test_a_label_already_present_is_not_duplicated():
    _seed('AAPL', ['ARK26', 'penny-17'])

    _run_auto_add('AAPL', expert_shortname='penny-17', extra_labels=['ARK26'])

    assert _labels('AAPL') == ['ARK26', 'penny-17']


def test_a_label_repeated_within_one_call_is_written_once():
    _seed('AAPL', [])

    _run_auto_add('AAPL', expert_shortname='penny-17', extra_labels=['penny-17'])

    assert _labels('AAPL') == ['penny-17']


# ---------------------------------------------------------------------------
# 2. The write must be narrow: no creation provenance on a row we did not create
# ---------------------------------------------------------------------------

def test_auto_added_is_not_stamped_onto_a_pre_existing_instrument():
    """The penny screener hook passes extra_labels=['auto_added'] for every
    candidate it screens. On a hand-curated row that tag is a lie, and it is the
    one label the overview charts key off, so it must not be adopted."""
    _seed('AAPL', ['ARK26'])

    _run_auto_add('AAPL', expert_shortname='', extra_labels=['auto_added'])

    assert _labels('AAPL') == ['ARK26']


def test_the_other_creation_provenance_tags_are_not_stamped_either():
    _seed('AAPL', ['ARK26'])

    _run_auto_add('AAPL', expert_shortname='',
                  extra_labels=['expert_selected', 'ai_selected'])

    assert _labels('AAPL') == ['ARK26']


def test_provenance_is_filtered_case_insensitively():
    _seed('AAPL', ['ARK26'])

    _run_auto_add('AAPL', expert_shortname='', extra_labels=['Auto_Added'])

    assert _labels('AAPL') == ['ARK26']


def test_a_useful_label_still_lands_when_a_provenance_tag_travels_with_it():
    _seed('AAPL', ['ARK26'])

    _run_auto_add('AAPL', expert_shortname='penny-17',
                  extra_labels=['auto_added', 'NASDAQ30'])

    assert _labels('AAPL') == ['ARK26', 'penny-17', 'NASDAQ30']


def test_the_create_path_still_stamps_its_own_provenance():
    """Filtering applies only to rows we did not create. A brand-new row keeps the
    full ['auto_added', <expert>, <source tag>] stamp it has always had."""
    _run_auto_add('NVDA', expert_shortname='penny-17', source='expert')

    assert _labels('NVDA') == ['auto_added', 'penny-17', 'expert_selected']


def test_the_create_path_still_stamps_ai_selected_for_a_dynamic_source():
    _run_auto_add('NVDA', expert_shortname='ai_selector', source='ai_dynamic')

    assert _labels('NVDA') == ['auto_added', 'ai_selector', 'ai_selected']
