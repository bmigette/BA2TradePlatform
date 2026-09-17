"""The LIVE market-condition resolver is built from the expert SETTING, not the environment
(plan Task 12).

Three things are pinned here:

* ``BA2_MARKET_CONDITION_PROFILE`` is RETIRED and a set value fails startup. A deploy script that
  still exports it would otherwise keep a whole platform on a process-wide profile while every
  expert's setting says something else.
* ``resolver_for_profiles`` builds ONE reader PER PROFILE, unwrapped for a single profile and
  behind the composite for two -- the same join the backtest seam uses.
* the resolver's coverage check iterates the composite's ``mapped_readers``. The composite's
  ``mapped_reader`` raises TypeError ON PURPOSE (Task 10 review): a resolver that reached for it
  through ``getattr(..., None)`` would report a clean bill of health for a run whose snapshots it
  never opened.
"""
from __future__ import annotations

import pytest

from types import SimpleNamespace

from ba2_common.core import market_condition_live as live
from ba2_common.core.market_condition_readers import CompositeMarketConditionReader


class _FakeReader:
    """A reader with a profile and an optional mapped (pinned-snapshot) reader."""

    def __init__(self, profile, calc_version="x/calc-1", mapped=None):
        self.profile = profile
        self.calc_version = calc_version
        self.mapped_reader = mapped

    def observe(self, symbol, session):
        return None


class _FakeMapped:
    """A pinned-snapshot reader, as much of one as ``missing_coverage`` / ``coverage_detail`` ask."""

    def __init__(self, digest, symbols):
        self.manifest_digest = digest
        self._symbols = tuple(symbols)

    def symbols(self):
        return self._symbols

    def coverage(self):
        return {s: 1 for s in self._symbols}


# --------------------------------------------------------------------------- the retired env var
def test_a_set_profile_env_var_fails_loudly():
    with pytest.raises(RuntimeError) as e:
        live.assert_profile_env_retired({live.PROFILE_ENV_RETIRED: "ohlcv-v1"})
    msg = str(e.value)
    assert "expert setting" in msg and "market_condition_profile" in msg
    assert live.PROFILE_ENV_RETIRED in msg


@pytest.mark.parametrize("env", [{}, {live.PROFILE_ENV_RETIRED: ""}, {live.PROFILE_ENV_RETIRED: "  "}])
def test_an_unset_or_empty_profile_env_var_is_fine(env):
    assert live.assert_profile_env_retired(env) is None


def test_resolver_from_env_is_gone():
    assert not hasattr(live, "resolver_from_env")
    assert not hasattr(live, "PROFILE_ENV")


# --------------------------------------------------------------------------- resolver_for_profiles
def test_one_profile_is_served_by_its_own_reader_unwrapped(monkeypatch):
    built = []

    def fake_reader(profile, root=None, *, manifest_digest=None):
        built.append((profile, root, manifest_digest))
        return _FakeReader(profile)

    monkeypatch.setattr(live, "_fmp_cache_reader", fake_reader)
    resolver = live.resolver_for_profiles(("ohlcv-v1",), cache_root="R")
    assert resolver.profiles == ("ohlcv-v1",)
    assert resolver.profile == "ohlcv-v1"          # unchanged for the single-profile case
    assert not isinstance(resolver.reader, CompositeMarketConditionReader)
    assert built == [("ohlcv-v1", "R", None)]


def test_two_profiles_are_joined_by_the_composite(monkeypatch):
    monkeypatch.setattr(live, "_fmp_cache_reader",
                        lambda p, root=None, *, manifest_digest=None: _FakeReader(p, f"{p}/calc-1"))
    resolver = live.resolver_for_profiles(("ohlcv-v1", "ta-structure-v1"), cache_root="R")
    assert resolver.profiles == ("ohlcv-v1", "ta-structure-v1")
    assert isinstance(resolver.reader, CompositeMarketConditionReader)
    assert resolver.reader.profiles == ("ohlcv-v1", "ta-structure-v1")
    # The context's calc_version names every profile rather than pretending to be one of them.
    assert "ohlcv-v1=" in resolver.calc_version and "ta-structure-v1=" in resolver.calc_version


def test_each_profile_gets_its_own_manifest_digest(monkeypatch):
    built = []
    monkeypatch.setattr(live, "_fmp_cache_reader",
                        lambda p, root=None, *, manifest_digest=None:
                        built.append((p, manifest_digest)) or _FakeReader(p))
    live.resolver_for_profiles(("ohlcv-v1", "ta-structure-v1"),
                               manifest_digests={"ohlcv-v1": "a" * 64, "ta-structure-v1": "b" * 64})
    assert built == [("ohlcv-v1", "a" * 64), ("ta-structure-v1", "b" * 64)]


def test_a_digest_for_a_profile_the_resolver_does_not_serve_is_refused(monkeypatch):
    monkeypatch.setattr(live, "_fmp_cache_reader",
                        lambda p, root=None, *, manifest_digest=None: _FakeReader(p))
    with pytest.raises(ValueError, match="does not serve"):
        live.resolver_for_profiles(("ohlcv-v1",), manifest_digests={"ta-structure-v1": "b" * 64})


def test_no_profile_at_all_is_a_wiring_defect(monkeypatch):
    with pytest.raises(ValueError, match="at least one profile"):
        live.resolver_for_profiles(())


# --------------------------------------------------------------------------- coverage per profile
def test_coverage_is_checked_per_profile_not_through_a_single_mapped_reader():
    readers = [_FakeReader("ohlcv-v1", mapped=_FakeMapped("a" * 64, ["AAA", "BBB"])),
               _FakeReader("ta-structure-v1", mapped=_FakeMapped("b" * 64, ["AAA"]))]
    resolver = live.LiveMarketConditionResolver(
        ("ohlcv-v1", "ta-structure-v1"), reader=CompositeMarketConditionReader(readers))
    # The composite refuses the singular question; the resolver must never ask it.
    with pytest.raises(TypeError):
        resolver.reader.mapped_reader
    assert [m.manifest_digest for m in resolver.mapped_readers] == ["a" * 64, "b" * 64]
    missing = resolver.refresh_coverage(["AAA", "BBB"], force=True)
    # BBB is covered by ohlcv-v1 and NOT by ta-structure-v1: a gate on a ta-structure field
    # would read nothing, so the symbol is uncovered and the reason names the digest that misses it.
    assert missing == ["BBB"]
    assert "b" * 64 in resolver.no_context_reason_for("BBB")
    assert resolver.no_context_reason_for("AAA") is None


def test_a_research_mode_profile_contributes_no_coverage_question():
    resolver = live.LiveMarketConditionResolver(
        ("ohlcv-v1", "ta-structure-v1"),
        reader=CompositeMarketConditionReader([_FakeReader("ohlcv-v1"),
                                               _FakeReader("ta-structure-v1")]))
    assert resolver.mapped_readers == (None, None)
    assert resolver.refresh_coverage(["AAA"], force=True) == []


def test_a_single_profile_resolver_keeps_its_one_snapshot_question():
    resolver = live.LiveMarketConditionResolver(
        "ohlcv-v1", reader=_FakeReader("ohlcv-v1", mapped=_FakeMapped("c" * 64, ["AAA"])))
    assert [m.manifest_digest for m in resolver.mapped_readers] == ["c" * 64]
    assert resolver.refresh_coverage(["AAA", "ZZZ"], force=True) == ["ZZZ"]
    assert "c" * 64 in resolver.no_context_reason_for("zzz")


def test_the_reader_must_serve_exactly_the_resolvers_profiles():
    with pytest.raises(ValueError, match="serves profile"):
        live.LiveMarketConditionResolver("ohlcv-v1", reader=_FakeReader("ta-structure-v1"))


# --------------------------------------------------- certification that cannot run at all (I2)
def _gated(monkeypatch, profile="ohlcv-v1"):
    """Point the instance-resolver seam at one expert whose setting names ``profile``, so
    ``resolver_for`` gets past ``profiles_for`` and actually builds."""
    import ba2_common.core.instance_resolver as ir

    class _R:
        def get_expert_instance(self, expert_id):
            return SimpleNamespace(id=expert_id,
                                   settings={"market_condition_profile": profile})

    monkeypatch.setattr(ir, "get_instance_resolver", lambda: _R())


def _log_spy(monkeypatch):
    import ba2_common.logger as bl

    errors = []
    monkeypatch.setattr(bl, "logger", type("L", (), {
        "error": lambda self, msg, *a, **k: errors.append(msg % a if a else msg),
        "warning": lambda self, *a, **k: None,
        "info": lambda self, *a, **k: None,
        "debug": lambda self, *a, **k: None,
    })())
    return errors


#: The real shape of "certification could not run": ``read_fmp_daily_cache`` raises ValueError
#: for a ``Date`` column that is not midnight-aligned, and a corrupt parquet surfaces as
#: ArrowInvalid (a ValueError subclass) or OSError. A MISSING file is not in this set -- that
#: comes back as an ``unavailable`` VERDICT, which was already handled.
_CANNOT_CERTIFY = ValueError(
    "AAPL_1d.parquet: tz-aware bar date is not midnight; refusing to guess its session")


def test_a_certification_that_RAISES_degrades_instead_of_aborting_the_pass(monkeypatch):
    """``certify_source_columns`` reports ``unavailable`` for a MISSING file but RAISES for one
    it cannot read (a non-midnight ``Date`` label, a corrupt parquet).

    Task 12 moved certification out of startup and into the live entry pass, so an escaping
    exception would abort ``process_expert_recommendations_after_analysis`` for that expert on
    EVERY pass, uncached and unsummarised. It must degrade exactly like a failed verdict.
    """
    errors = _log_spy(monkeypatch)
    calls = []

    def boom(cache_root=None):
        calls.append(cache_root)
        raise _CANNOT_CERTIFY

    _gated(monkeypatch)
    monkeypatch.setattr(live, "certify_cache_root", boom)
    dispatcher = live.PerInstanceMarketConditionResolver(cache_root="R", environ={})
    resolver = dispatcher.resolver_for(1)

    assert isinstance(resolver, live.UnreadableSourceResolver)
    assert "midnight" in resolver.no_context_reason
    assert "ValueError" in resolver.no_context_reason
    # Resolves no context, ever, and answers the per-symbol question the dispatcher asks every
    # resolver it holds.
    assert resolver(object(), "AAA", None) is None
    assert resolver.no_context_reason_for("AAA") is None

    # CACHED like any other answer: one certification attempt and one ERROR for the process,
    # not one per leaf per pass.
    assert dispatcher.resolver_for(1) is resolver
    assert calls == ["R"]
    assert len(errors) == 1 and "could not be certified at all" in errors[0]


def test_an_unreadable_source_gets_no_decision_scope_and_never_passes_a_gate(monkeypatch):
    """The whole point: exits keep running. The scope is a no-op (no clock read) and
    ``resolver_for_expert_instance`` reports None, exactly as for an uncertified cache."""
    import ba2_common.core.TradeConditions as TC

    _log_spy(monkeypatch)
    _gated(monkeypatch)

    def boom(cache_root=None):
        raise _CANNOT_CERTIFY

    monkeypatch.setattr(live, "certify_cache_root", boom)
    dispatcher = live.PerInstanceMarketConditionResolver(cache_root="R", environ={})
    saved = TC.get_market_condition_context_resolver()
    TC.set_market_condition_context_resolver(dispatcher)
    try:
        assert live.resolver_for_expert_instance(1) is None
        with live.market_condition_decision_scope(expert_instance_id=1) as state:
            assert state is None
        assert dispatcher(object(), "AAA", SimpleNamespace(instance_id=1)) is None
        assert "midnight" in dispatcher.no_context_reason_for("AAA")
    finally:
        TC.set_market_condition_context_resolver(saved)


def test_an_unexpected_certification_failure_still_propagates(monkeypatch):
    """The convention working, not a gap. ``absorb_if_benign`` NAMES the shapes a cache read
    legitimately produces (ValueError/LookupError, plus OSError globally); anything else is a
    defect, and turning a defect into "the gates are off" is how one would ship unnoticed."""
    _log_spy(monkeypatch)
    _gated(monkeypatch)

    def boom(cache_root=None):
        raise RuntimeError("something nobody characterised")

    monkeypatch.setattr(live, "certify_cache_root", boom)
    dispatcher = live.PerInstanceMarketConditionResolver(cache_root="R", environ={})
    with pytest.raises(RuntimeError, match="nobody characterised"):
        dispatcher.resolver_for(1)


def test_a_seam_defect_in_the_settings_path_still_propagates(monkeypatch):
    """Same convention on the settings read. A TypeError from the resolver seam is a defect and
    must not be downgraded to "this expert has no profile", which would silently un-gate it."""
    import ba2_common.core.instance_resolver as ir

    class _Broken:
        def get_expert_instance(self, expert_id):
            raise TypeError("resolver seam returned the wrong shape")

    monkeypatch.setattr(ir, "get_instance_resolver", lambda: _Broken())
    dispatcher = live.PerInstanceMarketConditionResolver(environ={})
    with pytest.raises(TypeError, match="wrong shape"):
        dispatcher.profiles_for(1)


@pytest.mark.parametrize("error,why", [
    (ValueError("nope-v1 is not a registered market-condition profile"),
     "an unregistered profile name -- the case this path exists to report"),
    (LookupError("instance 4 not found"),
     "an instance deleted between the recommendation and the pass"),
])
def test_the_settings_faults_this_path_expects_are_absorbed(monkeypatch, error, why):
    import ba2_common.core.instance_resolver as ir

    errors = _log_spy(monkeypatch)

    class _Raises:
        def get_expert_instance(self, expert_id):
            raise error

    monkeypatch.setattr(ir, "get_instance_resolver", lambda: _Raises())
    dispatcher = live.PerInstanceMarketConditionResolver(environ={})
    assert dispatcher.profiles_for(1) == (), why
    assert len(errors) == 1 and "refuse every entry" in errors[0]
