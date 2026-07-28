"""Code defects must escape broad handlers; data problems may still be absorbed (2026-07-28).

The ATR incident in one sentence: ``get_latest_atr`` caught ``Exception``, logged a warning and
returned ``None``, so a tz naive/aware ``TypeError`` looked exactly like "this symbol has no ATR
data" — a legitimate outcome the caller handles by falling back to ``min_stop_loss_pct``. ATR
was dead for months, ``use_atr_stop``/``atr_multiplier``/``atr_period`` were inert GA genes, and
no log anywhere said so.
"""
import pytest

from ba2_common.core.failure_modes import is_defect, raise_if_defect


# --------------------------------------------------------------------------- #
# defects escape
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc", [
    TypeError("Cannot compare tz-naive and tz-aware datetime-like objects"),  # THE incident
    AttributeError("'NoneType' object has no attribute 'dt'"),
    NameError("name 'foo' is not defined"),
    ImportError("no module named x"),
    ZeroDivisionError("division by zero"),
])
def test_defects_are_reraised(exc):
    assert is_defect(exc)
    with pytest.raises(type(exc)):
        raise_if_defect(exc)


def test_the_actual_atr_exception_would_now_escape():
    """Regression-guard the exact exception that hid for months."""
    with pytest.raises(TypeError):
        raise_if_defect(TypeError("Cannot compare tz-naive and tz-aware datetime-like objects"))


# --------------------------------------------------------------------------- #
# data problems are still absorbable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc", [
    KeyError("close"),                 # field absent from an API payload
    IndexError("list index out of range"),
    ValueError("could not convert string to float: ''"),
    OSError("connection reset"),
    TimeoutError("read timed out"),
])
def test_data_conditions_are_not_defects(exc):
    """These have ordinary bad-data causes. Escalating them would fire constantly on healthy
    bad-feed paths and train people to bypass the check."""
    assert not is_defect(exc)
    raise_if_defect(exc)               # must NOT raise


# --------------------------------------------------------------------------- #
# behaviour inside a real handler
# --------------------------------------------------------------------------- #
def test_traceback_points_at_the_original_site():
    """A defect must keep its original traceback, or the loud failure is useless for debugging."""
    def _inner():
        return "a" + 1                 # TypeError, deep

    try:
        try:
            _inner()
        except Exception as e:
            raise_if_defect(e)
            pytest.fail("defect was swallowed")
    except TypeError as e:
        tb = e.__traceback__
        frames = []
        while tb:
            frames.append(tb.tb_frame.f_code.co_name)
            tb = tb.tb_next
        assert "_inner" in frames, f"original frame lost; got {frames}"


def test_absorbing_path_still_works_end_to_end():
    """The intended usage: defect escapes, data problem falls through to the fallback."""
    def guarded(exc):
        try:
            raise exc
        except Exception as e:
            raise_if_defect(e)
            return None                # legitimate "no data" fallback

    assert guarded(ValueError("bad row")) is None
    with pytest.raises(TypeError):
        guarded(TypeError("bug"))


def test_subclasses_of_defects_are_defects():
    class MyTypeError(TypeError):
        pass
    assert is_defect(MyTypeError("x"))
