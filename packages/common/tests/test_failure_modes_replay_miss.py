"""ReplayMiss is never absorbed: a missing recording must reach the replay harness in every mode."""
import pytest

from ba2_common.core.failure_modes import absorb_if_benign, is_never_absorbed
from ba2_common.core.replay import ReplayMiss


@pytest.mark.parametrize("mode", ["enforce", "observe", "legacy"])
def test_replay_miss_is_re_raised_in_every_mode(monkeypatch, mode):
    monkeypatch.setenv("BA2_ERROR_MODE", mode)
    exc = ReplayMiss("option_chain")
    assert is_never_absorbed(exc)
    with pytest.raises(ReplayMiss):
        try:
            raise exc
        except Exception as e:
            absorb_if_benign(e)


def test_replay_miss_is_not_absorbed_even_when_the_site_names_its_base(monkeypatch):
    monkeypatch.setenv("BA2_ERROR_MODE", "legacy")
    with pytest.raises(ReplayMiss):
        try:
            raise ReplayMiss("x")
        except Exception as e:
            absorb_if_benign(e, Exception)
