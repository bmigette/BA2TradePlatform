"""The term vocabulary must be total, ordered, and loud about unknown input.

WHY THESE PROPERTIES. ``option_term`` replaces the ``dte_min``/``dte_max`` pair as a GA gene,
and a categorical gene is only well-behaved if every value it can take resolves to a distinct,
usable window. A missing window would crash mid-backtest; an inverted one would silently select
nothing (the exact failure ``TradeActions._expiry_window`` already has to raise for); two
overlapping windows would let two distinct gene values produce identical behaviour, which wastes
GA budget on a difference that isn't one.
"""
import pytest

from ba2_common.core.option_terms import OptionTerm, dte_window


def test_every_term_has_a_window():
    for term in OptionTerm:
        lo, hi = dte_window(term)
        assert isinstance(lo, int) and isinstance(hi, int)


def test_no_window_is_inverted():
    for term in OptionTerm:
        lo, hi = dte_window(term)
        assert lo <= hi, f"{term} window [{lo}, {hi}] is inverted"


def test_windows_are_ordered_and_non_overlapping():
    windows = [dte_window(t) for t in OptionTerm]
    for (lo_a, hi_a), (lo_b, hi_b) in zip(windows, windows[1:]):
        assert hi_a < lo_b, f"[{lo_a},{hi_a}] overlaps or precedes [{lo_b},{hi_b}]"


def test_one_month_matches_the_existing_grid_default():
    # ba2test_launcher's option strategies all default to option_dte_min=25/max=45.
    # ONE_MONTH must contain that window or migrating the grid changes what it trades.
    lo, hi = dte_window(OptionTerm.ONE_MONTH)
    assert lo <= 25 and hi >= 45


def test_string_value_resolves():
    assert dte_window("1m") == dte_window(OptionTerm.ONE_MONTH)


def test_unknown_string_raises_and_names_the_valid_values():
    with pytest.raises(ValueError) as exc:
        dte_window("1 month")
    assert "1m" in str(exc.value)


def test_none_raises_rather_than_defaulting():
    with pytest.raises(ValueError):
        dte_window(None)
