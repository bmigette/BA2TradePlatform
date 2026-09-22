"""Ordering transactions/orders must not care whether a timestamp is naive or aware.

THE BUG THIS PINS. `_oldest_entry_order` ordered transactions with

    min(txns, key=lambda t: t.open_date or t.created_at or datetime.max.replace(tzinfo=utc))

which mixes an AWARE fallback with values that may be NAIVE. Python refuses to compare the two,
so the expression raises `TypeError: can't compare offset-naive and offset-aware datetimes` --
but ONLY when one run happens to hold both shapes at once. SQLite through SQLAlchemy returns a
stored timestamp either way depending on how the row was written, so the mix is data-dependent,
not machine-dependent: the suite passed on two developer machines and failed hard in CI for five
consecutive runs with NO code change between the last green build and the first red one.

These tests construct the mix directly, so they fail on the old code on every machine.
"""
from datetime import datetime, timezone

import pytest

from ba2_common.core.utils import as_utc_key


class _Txn:
    """Only what the sort key touches."""
    def __init__(self, open_date=None, created_at=None, ident=0):
        self.open_date = open_date
        self.created_at = created_at
        self.id = ident


NAIVE = datetime(2026, 1, 2, 9, 30)
AWARE = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)          # EARLIER instant
PLUS1 = datetime(2026, 1, 1, 10, 30, tzinfo=timezone(__import__("datetime").timedelta(hours=1)))


def test_the_old_idiom_is_what_raised():
    """Guards the premise: mixing the two shapes really is a TypeError, so the fix is not
    cosmetic and a future 'simplification' back to `or` reintroduces a hard failure."""
    with pytest.raises(TypeError, match="offset-naive and offset-aware"):
        min([NAIVE, AWARE])


def test_min_over_mixed_naive_and_aware_transactions():
    txns = [_Txn(open_date=NAIVE, ident=1), _Txn(open_date=AWARE, ident=2)]
    oldest = min(txns, key=lambda t: as_utc_key(t.open_date or t.created_at))
    assert oldest.id == 2, "the earlier INSTANT must win regardless of which one carried a tzinfo"


def test_the_fallback_chain_is_normalised_too():
    """open_date missing -> created_at; both missing -> the fallback. Every branch must come
    back aware, or the fallback reintroduces the mix it was meant to avoid."""
    rows = [_Txn(created_at=NAIVE, ident=1), _Txn(open_date=AWARE, ident=2), _Txn(ident=3)]
    order = sorted(rows, key=lambda t: as_utc_key(t.open_date or t.created_at))
    assert [r.id for r in order] == [2, 1, 3], "unknown timestamps must sort LAST"


def test_offsets_other_than_utc_compare_by_instant():
    assert as_utc_key(PLUS1) == as_utc_key(AWARE)


def test_a_naive_value_is_read_as_utc_not_rejected():
    assert as_utc_key(NAIVE) == NAIVE.replace(tzinfo=timezone.utc)


def test_the_real_engine_helper_survives_the_mix():
    """End of the chain: the actual method, not a re-implementation of its key."""
    from app.services.backtest.daily_engine import DailyBacktestEngine

    class _Acct:
        def _entry_order_for_transaction(self, txn):
            return ("order-for", txn.id)

    eng = object.__new__(DailyBacktestEngine)
    eng.account = _Acct()
    txns = [_Txn(open_date=NAIVE, ident=1), _Txn(created_at=AWARE, ident=2)]
    assert eng._oldest_entry_order(txns) == ("order-for", 2)
    assert eng._oldest_entry_order([]) is None
