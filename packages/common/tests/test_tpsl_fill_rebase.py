"""The shared fill-time re-base: stamps, the reference chain, tolerance, refusals (pure, no I/O)."""
from types import SimpleNamespace

import pytest

from ba2_common.core.tpsl_fill_rebase import (
    ANCHOR_KEY, FillRebaseRefused, REBASE_TOLERANCE, read_anchor, rebase_levels_at_fill,
    resolve_tpsl_reference_price, stamp_anchor)

A = pytest.approx


def test_stamp_and_read_keep_each_levels_own_anchor():
    m = stamp_anchor({"max_loss_stop": 92.0}, stop=110.0, tp=100.0)
    assert m["max_loss_stop"] == 92.0                      # everything else is carried forward
    assert read_anchor(m, "stop") == 110.0 and read_anchor(m, "tp") == 100.0
    m2 = stamp_anchor(m, stop=105.0)                       # tp untouched when omitted
    assert read_anchor(m2, "stop") == 105.0 and read_anchor(m2, "tp") == 100.0
    assert read_anchor(m, "stop") == 110.0                 # the input is never mutated


def test_none_clears_a_level_anchor_and_the_last_clear_removes_the_key():
    m = stamp_anchor({}, stop=110.0, tp=100.0)
    m = stamp_anchor(m, stop=None)
    assert read_anchor(m, "stop") is None and read_anchor(m, "tp") == 100.0
    m = stamp_anchor(m, tp=None)
    assert ANCHOR_KEY not in m


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), "x"])
def test_an_unusable_anchor_price_is_refused(bad):
    with pytest.raises(FillRebaseRefused):
        stamp_anchor({}, stop=bad)


def test_read_anchor_tolerates_missing_or_foreign_meta():
    assert read_anchor(None, "stop") is None and read_anchor({}, "stop") is None
    assert read_anchor("not a dict", "stop") is None


def _resolve(open_price=None, limit=None, rec=None, live=None, stamp=None):
    return resolve_tpsl_reference_price(open_price, limit, lambda: rec, lambda: live,
                                        stamped_stop_anchor=stamp)


def test_reference_chain_order_fill_then_stamp_then_limit_then_rec_then_live():
    assert _resolve(100, 99, 98, 97, stamp=110) == 100          # a realised fill: nothing to re-base
    assert _resolve(None, 99, 98, 97, stamp=110) == 110         # the stamp beats the limit price
    assert _resolve(None, 99, 98, 97) == 99                     # unstamped: the legacy chain
    assert _resolve(None, None, 98, 97) == 98
    assert _resolve(None, None, None, 97) == 97
    assert _resolve() is None


def test_the_limit_entry_scenario_the_stamp_fixes():
    """Price 110, stop 104.5 (5% under), limit entry 100 filling at 100."""
    chain_only = _resolve(None, 100.0, 110.0)                   # legacy: limit price = 100
    stamped = _resolve(None, 100.0, 110.0, stamp=110.0)
    old = rebase_levels_at_fill(is_long=True, fill_price=100.0, reference_price=chain_only,
                                take_profit=None, stop_loss=104.5, apply_tp_floor=False)
    new = rebase_levels_at_fill(is_long=True, fill_price=100.0, reference_price=stamped,
                                take_profit=None, stop_loss=104.5, apply_tp_floor=False)
    assert old.stop_loss == A(104.5) and not old.stop_rebased   # ABOVE the fill: stopped out at once
    assert new.stop_loss == A(95.0) and new.stop_rebased


def test_a_change_of_rounding_only_is_not_a_rebase():
    # reference == fill: the product is the stop itself; only the 4-dp rounding differs
    r = rebase_levels_at_fill(is_long=True, fill_price=100.0, reference_price=100.0,
                              take_profit=None, stop_loss=94.123456789, apply_tp_floor=False)
    assert r.stop_rebased is False and r.changed is False
    assert r.stop_loss == 94.123456789                          # the level is not rewritten
    assert REBASE_TOLERANCE >= 5e-5


def test_a_real_rebase_counts():
    r = rebase_levels_at_fill(is_long=True, fill_price=103.0, reference_price=100.0,
                              take_profit=None, stop_loss=92.0, apply_tp_floor=False)
    assert r.stop_rebased and r.stop_loss == A(94.76)


def test_refuses_loudly_without_a_reference_for_a_stop():
    with pytest.raises(FillRebaseRefused):
        rebase_levels_at_fill(is_long=True, fill_price=100.0, reference_price=None,
                              take_profit=None, stop_loss=92.0, apply_tp_floor=False)
    # the caller's explicit, logged decision to skip is the only way not to raise
    r = rebase_levels_at_fill(is_long=True, fill_price=100.0, reference_price=None,
                              take_profit=None, stop_loss=92.0, rebase_stop=False, apply_tp_floor=False)
    assert r.stop_loss == 92.0


def test_the_ibkr_mixin_resolves_through_the_same_chain():
    from ba2_common.core.protective_legs import ProtectiveLegsMixin

    class _Acct(ProtectiveLegsMixin):
        def get_instrument_current_price(self, symbol):
            return 97.0

    entry = SimpleNamespace(open_price=None, limit_price=99.0, expert_recommendation_id=None, symbol="X")
    acct = _Acct.__new__(_Acct)
    assert acct._tpsl_reference_price(entry) == 99.0
    assert acct._tpsl_reference_price(entry, SimpleNamespace(meta_data=stamp_anchor({}, stop=110.0))) == 110.0
    filled = SimpleNamespace(open_price=101.0, limit_price=99.0, expert_recommendation_id=None, symbol="X")
    assert acct._tpsl_reference_price(filled, SimpleNamespace(meta_data=stamp_anchor({}, stop=110.0))) == 101.0
    bare = SimpleNamespace(open_price=None, limit_price=None, expert_recommendation_id=None, symbol="X")
    assert acct._tpsl_reference_price(bare) == 97.0
