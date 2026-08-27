"""The term vocabulary must be total, contiguous, meaningful, and loud about unknown input.

WHY THESE PROPERTIES. ``option_term`` becomes a categorical GA gene, and a categorical gene is
only well-behaved if every value it can take resolves to a distinct, usable window. A missing
window would crash mid-backtest. An inverted one would silently select nothing — the exact
failure ``TradeActions._expiry_window`` already has to raise for. Overlapping windows would let
two gene values produce identical behaviour, wasting search budget on a difference that isn't
one. And a GAP between windows makes some DTEs unnameable, which under a design that forbids
silent widening is a hole with no recovery: an earlier draft of this table had gaps, and one of
them swallowed the 20-DTE floor that ``O_STRD``/``O_STRG`` actually use.
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


def test_windows_are_contiguous_so_no_dte_is_unnameable():
    # The counterpart to the test above: non-overlapping alone permits gaps, and a gap means a
    # DTE that no term can express. Together the two pin the table to an exact partition.
    windows = [dte_window(t) for t in OptionTerm]
    for (_, hi_a), (lo_b, _) in zip(windows, windows[1:]):
        assert lo_b == hi_a + 1, f"gap between DTE {hi_a} and {lo_b}: no term covers it"


def test_coverage_starts_at_zero():
    assert dte_window(OptionTerm.ZERO_DTE)[0] == 0


@pytest.mark.parametrize("term, nominal", [
    (OptionTerm.ZERO_DTE, 0),
    (OptionTerm.ONE_WEEK, 7),
    (OptionTerm.TWO_WEEKS, 14),
    (OptionTerm.ONE_MONTH, 30),
    (OptionTerm.TWO_MONTHS, 60),
    (OptionTerm.THREE_MONTHS, 90),
    (OptionTerm.SIX_MONTHS, 180),
    (OptionTerm.LEAPS, 365),
])
def test_each_window_contains_the_duration_its_name_claims(term, nominal):
    # Without this, a typo turning SIX_MONTHS into (1500, 2100) passes every structural test
    # above. This makes the table's MEANING testable, not just its well-formedness.
    lo, hi = dte_window(term)
    assert lo <= nominal <= hi, f"{term.value} is {lo}-{hi}, which excludes {nominal} days"


@pytest.mark.parametrize("lo_default, hi_default, who", [
    (25, 45, "fifteen of the seventeen option strategies"),
    (20, 40, "O_STRD and O_STRG"),
])
def test_one_month_contains_every_live_grid_default(lo_default, hi_default, who):
    # Containment means no existing default becomes INEXPRESSIBLE. It does not mean migration
    # is a no-op — a 25-45 rule moved onto ONE_MONTH also gains 20-24.
    lo, hi = dte_window(OptionTerm.ONE_MONTH)
    assert lo <= lo_default and hi >= hi_default, (
        f"ONE_MONTH is {lo}-{hi}, which cannot express the {lo_default}-{hi_default} "
        f"default used by {who}")


def test_string_value_resolves():
    assert dte_window("1m") == dte_window(OptionTerm.ONE_MONTH)


def test_unknown_string_raises_and_names_the_valid_values():
    with pytest.raises(ValueError) as exc:
        dte_window("1 month")
    assert "1m" in str(exc.value)


@pytest.mark.parametrize("bad", [None, 0, 1.5, b"1m", object()])
def test_non_term_input_raises_rather_than_defaulting(bad):
    # 0 is the pointed one: ZERO_DTE's window is literally (0, 0), and the house rule is that
    # unknown is never zero. The integer 0 must NOT resolve to ZERO_DTE.
    with pytest.raises(ValueError):
        dte_window(bad)


@pytest.mark.parametrize("unhashable", [[1, 2], {"a": 1}, {1, 2}])
def test_unhashable_input_raises_ValueError_not_TypeError(unhashable):
    # Pins the `except (KeyError, TypeError)` arm. Narrowing it to KeyError alone leaves every
    # other test in this file green while these escape as a raw TypeError from the dict lookup.
    with pytest.raises(ValueError):
        dte_window(unhashable)
