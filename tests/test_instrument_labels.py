"""Instrument label helpers used by the Account Overview positions table:
read labels per symbol, and add/remove a label across multiple instruments
(creating a minimal Instrument row when one doesn't exist yet)."""
from sqlmodel import select

from ba2_trade_platform.core.utils import (
    add_label_to_instruments, remove_label_from_instruments,
    get_labels_by_symbol, get_all_instrument_labels,
)
from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import Instrument


def _labels(sym):
    with get_db() as s:
        inst = s.exec(select(Instrument).where(Instrument.name == sym)).first()
        return list(inst.labels) if inst else None


class TestInstrumentLabels:
    def test_add_creates_instrument_with_label(self):
        assert add_label_to_instruments(['AAPL'], 'tech') == 1
        assert _labels('AAPL') == ['tech']

    def test_add_to_existing_appends_without_dup(self):
        add_label_to_instruments(['MSFT'], 'tech')
        add_label_to_instruments(['MSFT'], 'megacap')
        add_label_to_instruments(['MSFT'], 'tech')  # duplicate -> no change
        assert sorted(_labels('MSFT')) == ['megacap', 'tech']

    def test_add_multiple_symbols(self):
        add_label_to_instruments(['NVDA', 'AMD'], 'semis')
        assert _labels('NVDA') == ['semis']
        assert _labels('AMD') == ['semis']

    def test_remove_label(self):
        add_label_to_instruments(['TSLA'], 'ev')
        add_label_to_instruments(['TSLA'], 'volatile')
        assert remove_label_from_instruments(['TSLA'], 'ev') == 1
        assert _labels('TSLA') == ['volatile']

    def test_remove_nonexistent_label_noop(self):
        add_label_to_instruments(['GOOG'], 'tech')
        assert remove_label_from_instruments(['GOOG'], 'nope') == 0
        assert _labels('GOOG') == ['tech']

    def test_get_labels_by_symbol(self):
        add_label_to_instruments(['META'], 'social')
        m = get_labels_by_symbol(['META', 'NOSUCHSYM'])
        assert m.get('META') == ['social']
        assert 'NOSUCHSYM' not in m

    def test_blank_label_ignored(self):
        assert add_label_to_instruments(['IBM'], '   ') == 0
        assert _labels('IBM') is None

    def test_get_all_labels_deduped_sorted(self):
        add_label_to_instruments(['LBLX1'], 'zeta')
        add_label_to_instruments(['LBLX2'], 'alpha')
        all_labels = get_all_instrument_labels()
        assert 'zeta' in all_labels and 'alpha' in all_labels
        assert all_labels == sorted(all_labels)
