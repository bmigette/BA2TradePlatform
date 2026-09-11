"""Isolation: replay cannot leave the machine, and never quietly tries.

Spec section 8: "Replay runs in an isolated process with network disabled at the
transport layer [...] It cannot fall through to ``_get_current_price``, a
provider fetch, or production DB lookup. Every unexpected dependency is a typed
replay miss with analysis/request ID."

Two halves, and the second is the one that matters: the locks refuse, AND a
normal replay never reaches them. A run whose provider tape is incomplete must
raise a ``ReplayMiss`` at the tape -- not travel all the way down to a socket
that happens to be closed.
"""
from __future__ import annotations

import shutil
import socket
from pathlib import Path

import pytest

from ba2_common.core import TradeConditions, instance_resolver
from ba2_common.core.replay import ReplayMiss, ReplayStatus

from app.services.replay import expert_replay, gather_tape
from app.services.replay.isolation import (
    HERMETIC_ESCAPE_HATCH,
    ReplayIsolationBreach,
    replay_isolation,
)
from tests.replay import ALL_IDS, INSIDER_ID, SCORER_ID, capture_session, drop_observations


@pytest.fixture(scope="module")
def session(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("replay-isolation")
    return capture_session(root / "store", root / "export")


# --------------------------------------------------------------------------- #
# 1. The locks refuse
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("call", ["connect", "connect_ex"])
def test_the_transport_is_closed(call):
    """``connect_ex`` is the same syscall with an error code instead of a raise.

    Patching only ``connect`` would leave a caller that prefers ``connect_ex`` a
    quiet way out of the isolation.
    """
    with replay_isolation() as probe:
        with pytest.raises(ReplayMiss) as excinfo:
            getattr(socket.socket(), call)(("financialmodelingprep.com", 443))
    assert isinstance(excinfo.value, ReplayIsolationBreach)
    assert "financialmodelingprep.com" in str(excinfo.value)
    assert probe.network_attempts, "a blocked connect must be visible, not only refused"
    assert probe.network_attempts[0].startswith(call)


def test_isolation_refuses_to_start_while_the_fmp_escape_hatch_is_set(monkeypatch):
    """One lock disabled while the report still says "offline" is the worst outcome."""
    monkeypatch.setenv(HERMETIC_ESCAPE_HATCH, "1")
    with pytest.raises(ReplayIsolationBreach) as excinfo:
        with replay_isolation():
            pytest.fail("isolation started with its FMP lock reopened")
    assert HERMETIC_ESCAPE_HATCH in str(excinfo.value)


def test_the_escape_hatch_check_does_not_leave_the_seams_patched(monkeypatch):
    before = (socket.socket.connect,
              instance_resolver.get_instance_resolver(),
              TradeConditions.get_provider_resolver())
    monkeypatch.setenv(HERMETIC_ESCAPE_HATCH, "1")
    with pytest.raises(ReplayIsolationBreach):
        with replay_isolation():
            pass
    assert (socket.socket.connect,
            instance_resolver.get_instance_resolver(),
            TradeConditions.get_provider_resolver()) == before


def test_resolving_a_live_instance_or_provider_is_a_breach():
    with replay_isolation() as probe:
        resolver = instance_resolver.get_instance_resolver()
        with pytest.raises(ReplayMiss):
            resolver.get_account_instance(1)
        with pytest.raises(ReplayMiss):
            resolver.get_expert_instance(7)
        with pytest.raises(ReplayMiss):
            TradeConditions.get_provider_resolver()("ohlcv", "fmp")
    assert len(probe.instance_resolutions) == 2
    assert probe.provider_resolutions == ["ohlcv/fmp"]


def test_the_fmp_layer_is_hermetic_inside_the_isolation():
    from ba2_providers.fmp_common import _is_hermetic_fmp_history

    assert not _is_hermetic_fmp_history()
    with replay_isolation():
        assert _is_hermetic_fmp_history()
    assert not _is_hermetic_fmp_history()


def test_every_seam_is_restored_even_when_the_body_raises():
    before = (socket.socket.connect, socket.socket.connect_ex,
              instance_resolver.get_instance_resolver(),
              TradeConditions.get_provider_resolver())
    with pytest.raises(RuntimeError):
        with replay_isolation():
            raise RuntimeError("boom")
    assert (socket.socket.connect, socket.socket.connect_ex,
            instance_resolver.get_instance_resolver(),
            TradeConditions.get_provider_resolver()) == before


# --------------------------------------------------------------------------- #
# 2. A normal replay never reaches them
# --------------------------------------------------------------------------- #
def test_a_clean_replay_touches_nothing_outside_the_bundle(session, monkeypatch):
    probes = []
    original = replay_isolation

    from contextlib import contextmanager

    @contextmanager
    def watching():
        with original() as probe:
            probes.append(probe)
            yield probe

    monkeypatch.setattr(expert_replay, "replay_isolation", watching)
    report = expert_replay.run(session)

    assert report.counts()[ReplayStatus.COVERAGE_MATCH] == len(ALL_IDS)
    assert len(probes) == 1
    assert probes[0].clean, (
        f"replay reached outside the bundle: {probes[0].summary()}")


def test_a_missing_observation_misses_at_the_tape_not_at_the_socket(session, tmp_path,
                                                                    monkeypatch):
    """The important negative: an incomplete tape stops the comparison locally."""
    bundle = tmp_path / "bundle"
    shutil.copytree(session, bundle)
    assert drop_observations(bundle, INSIDER_ID, "insider_get") == 1

    probes = []
    original = replay_isolation

    from contextlib import contextmanager

    @contextmanager
    def watching():
        with original() as probe:
            probes.append(probe)
            yield probe

    monkeypatch.setattr(gather_tape, "replay_isolation", watching)
    report = gather_tape.run(bundle)

    result = next(r for r in report.results if r.analysis_id == INSIDER_ID)
    assert result.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert probes[0].network_attempts == [], (
        "the replay tried to reach the network instead of reporting a tape miss")
    assert probes[0].instance_resolutions == [], (
        "the replay tried to resolve a live instance")
    # DeterministicScorer's gather is not tape-serveable, and its miss must ALSO
    # be a local tape miss rather than an attempted fetch.
    scorer = next(r for r in report.results if r.analysis_id == SCORER_ID)
    assert scorer.status == ReplayStatus.COVERAGE_MISSING_CAPTURE
    assert probes[0].provider_resolutions == []
