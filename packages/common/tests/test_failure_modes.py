"""Broad handlers fail LOUD by default; only explicitly-named benign errors absorb (2026-07-28).

The ATR incident: ``get_latest_atr`` caught ``Exception``, logged, and returned ``None``, so a tz
naive/aware ``TypeError`` looked exactly like "this symbol has no ATR data". ATR was dead for
months and three GA genes were inert, with nothing in any log.

The first fix classified a fixed list of "defect" types and absorbed the rest — allow-by-default,
which still leaks every bug wearing a data-shaped exception (a typo'd settings key raises
KeyError). This is the inversion: propagate unless the call site NAMES the error as expected.
"""
import logging

import pytest

from ba2_common.core.failure_modes import (
    absorb_if_benign, is_benign, is_defect, raise_if_defect,
)


@pytest.fixture(autouse=True)
def _enforce(monkeypatch):
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")


# --------------------------------------------------------------------------- #
# deny by default
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc", [
    TypeError("Cannot compare tz-naive and tz-aware datetime-like objects"),  # THE incident
    AttributeError("'NoneType' object has no attribute 'dt'"),
    KeyError("max_stopp_loss"),        # typo'd settings key -- the old version ABSORBED this
    ValueError("invalid enum 'bearsh'"),
    IndexError("list index out of range"),
    RuntimeError("something nobody anticipated"),
    ZeroDivisionError("division by zero"),
])
def test_anything_not_named_benign_propagates(exc):
    with pytest.raises(type(exc)):
        absorb_if_benign(exc)


def test_the_bugs_the_old_allow_by_default_version_leaked():
    """KeyError/ValueError were absorbed by raise_if_defect. They must NOT be now."""
    assert not is_defect(KeyError("typo"))      # old classifier said "not a defect"
    with pytest.raises(KeyError):               # new default disagrees
        absorb_if_benign(KeyError("typo"))


# --------------------------------------------------------------------------- #
# the benign set
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc", [
    OSError("connection reset"),
    TimeoutError("read timed out"),            # subclass of OSError
    ConnectionError("peer closed"),
    FileNotFoundError("no such file"),
])
def test_world_being_uncooperative_is_globally_benign(exc):
    assert is_benign(exc)
    absorb_if_benign(exc)                      # must not raise


def test_a_site_may_name_its_own_expected_errors():
    """The point of the design: the CALL SITE states what it expects."""
    absorb_if_benign(KeyError("close"), KeyError)          # absent API field -> fine here
    absorb_if_benign(ValueError("bad row"), ValueError)
    with pytest.raises(KeyError):
        absorb_if_benign(KeyError("close"), ValueError)    # named the wrong one


def test_naming_is_per_site_not_global():
    """Naming KeyError at one site must not make it benign at the next."""
    absorb_if_benign(KeyError("x"), KeyError)
    with pytest.raises(KeyError):
        absorb_if_benign(KeyError("x"))


def test_subclasses_count():
    class MyOSError(OSError):
        pass
    absorb_if_benign(MyOSError("x"))


# --------------------------------------------------------------------------- #
# modes -- the migration safety valve
# --------------------------------------------------------------------------- #
class _Recorder(logging.Handler):
    """Capture records straight off the module's own logger.

    NOT caplog: ba2_common's logger tree sets propagate=False (worker file-logging isolation)
    AND the suite runs under a global logging.disable(), so caplog's root handler never sees
    these. That combination is ALSO exactly why this class of error stayed invisible in every
    grid log -- the test reproduces the real-world capture problem rather than dodging it.
    """
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _record(monkeypatch):
    lg = logging.getLogger("ba2_common.core.failure_modes")
    rec = _Recorder()
    lg.addHandler(rec)
    monkeypatch.setattr(logging, "disable", lambda *a, **k: None, raising=False)
    logging.disable(logging.NOTSET)          # undo any global suppression for this test
    monkeypatch.setattr(lg, "level", logging.ERROR, raising=False)
    return rec, lg


def test_observe_mode_absorbs_but_records(monkeypatch):
    """Lets a full grid run measure the REAL per-site benign set before anything fails."""
    monkeypatch.setenv("BA2_ERROR_MODE", "observe")
    rec, lg = _record(monkeypatch)
    try:
        absorb_if_benign(TypeError("boom"))       # must NOT raise in observe
        assert any("WOULD-RAISE" in m for m in rec.messages),             f"observe mode must leave a grep-able marker; got {rec.messages}"
    finally:
        lg.removeHandler(rec)


def test_observe_mode_is_silent_for_genuinely_benign(monkeypatch):
    monkeypatch.setenv("BA2_ERROR_MODE", "observe")
    rec, lg = _record(monkeypatch)
    try:
        absorb_if_benign(OSError("timeout"))
        assert not any("WOULD-RAISE" in m for m in rec.messages)
    finally:
        lg.removeHandler(rec)


def test_legacy_mode_absorbs_everything(monkeypatch):
    monkeypatch.setenv("BA2_ERROR_MODE", "legacy")
    absorb_if_benign(TypeError("boom"))        # escape hatch only


def test_default_mode_is_observe_during_the_staged_rollout(monkeypatch):
    """Deliberately NOT enforce yet -- see the module docstring. Enforcing immediately surfaced
    pre-existing test-order pollution that the old swallow was hiding, and would change live
    trading behaviour on guesses rather than on measurement."""
    monkeypatch.delenv("BA2_ERROR_MODE", raising=False)
    absorb_if_benign(TypeError("boom"))          # absorbed, but recorded as WOULD-RAISE


def test_enforce_is_one_env_var_away(monkeypatch):
    monkeypatch.setenv("BA2_ERROR_MODE", "enforce")
    with pytest.raises(TypeError):
        absorb_if_benign(TypeError("boom"))


# --------------------------------------------------------------------------- #
# traceback fidelity -- a loud failure with a useless stack is barely better
# --------------------------------------------------------------------------- #
def test_traceback_points_at_the_original_site():
    def _inner():
        return "a" + 1

    try:
        try:
            _inner()
        except Exception as e:
            absorb_if_benign(e)
            pytest.fail("defect was swallowed")
    except TypeError as e:
        frames = []
        tb = e.__traceback__
        while tb:
            frames.append(tb.tb_frame.f_code.co_name)
            tb = tb.tb_next
        assert "_inner" in frames, f"original frame lost; got {frames}"


def test_end_to_end_handler_usage():
    def guarded(exc, *benign):
        try:
            raise exc
        except Exception as e:
            absorb_if_benign(e, *benign)
            return None                        # legitimate fallback

    assert guarded(OSError("net")) is None
    assert guarded(KeyError("f"), KeyError) is None
    with pytest.raises(TypeError):
        guarded(TypeError("bug"))


# --------------------------------------------------------------------------- #
# the superseded helper still escalates rather than silently reverting
# --------------------------------------------------------------------------- #
def test_deprecated_raise_if_defect_still_escalates_defects():
    with pytest.raises(TypeError):
        raise_if_defect(TypeError("x"))
    raise_if_defect(KeyError("x"))             # documented allow-by-default weakness
