"""``get_symbols_by_label`` must match the labels ``get_all_instrument_labels`` offers.

The two helpers scan the same JSON column and are used as a pair: the UI lists the
labels in use with ``get_all_instrument_labels()`` and then resolves the chosen one
with ``get_symbols_by_label()``. ``get_all_instrument_labels`` STRIPS what it finds,
so a legacy row holding ``' tech '`` is offered as ``'tech'`` -- but the resolver
compared the stored value RAW, so the label the user picked matched nothing at all
and the basket silently rendered empty.

Case is deliberately NOT folded: 'ark26' and 'ARK26' really are two different
baskets (see ``diff_managed_labels``). Only the padding is normalised, on both
sides, which is what makes the pair consistent.
"""
from ba2_common.core.db import get_db
from ba2_common.core.models import Instrument
from ba2_common.core.utils import get_all_instrument_labels, get_symbols_by_label


def _instrument(name, labels):
    """Write an Instrument row with EXACTLY these labels, padding and all.

    ``add_label_to_instruments`` strips as it writes, so the padded rows this test
    is about cannot be produced through it -- they are legacy data.
    """
    with get_db() as session:
        session.add(Instrument(name=name, labels=list(labels)))
        session.commit()


def test_a_padded_stored_label_resolves_under_the_name_the_ui_offers():
    _instrument('PADDED1', ['  padtest-alpha  '])
    assert 'padtest-alpha' in get_all_instrument_labels()
    assert get_symbols_by_label(['padtest-alpha']) == {'padtest-alpha': ['PADDED1']}


def test_a_padded_request_resolves_against_a_clean_stored_label():
    _instrument('PADDED2', ['padtest-beta'])
    assert get_symbols_by_label([' padtest-beta ']) == {'padtest-beta': ['PADDED2']}


def test_padded_and_clean_spellings_collapse_into_one_basket():
    _instrument('PADDED3', ['padtest-gamma'])
    _instrument('PADDED4', [' padtest-gamma'])
    assert get_symbols_by_label(['padtest-gamma']) == {
        'padtest-gamma': ['PADDED3', 'PADDED4']}


def test_label_matching_is_still_case_sensitive():
    """'ark26' and 'ARK26' are two different baskets, not one typed two ways."""
    _instrument('PADDED5', ['PadTest-Delta'])
    assert get_symbols_by_label(['padtest-delta']) == {'padtest-delta': []}
    assert get_symbols_by_label(['PadTest-Delta']) == {'PadTest-Delta': ['PADDED5']}


def test_a_requested_label_with_no_instruments_is_still_a_key():
    """'managed but empty' has to be distinguishable from 'not managed'."""
    assert get_symbols_by_label(['padtest-nothing']) == {'padtest-nothing': []}
