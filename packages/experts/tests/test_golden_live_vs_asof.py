"""Phase 1 Task 12 — THE GOLDEN TEST (the Phase-1 acceptance gate).

For EVERY backtestable expert, the live decision and the backtest decision must be
the SAME logic:

    rec_live = expert._process(expert._gather(live_providers, as_of=None), settings)
    rec_asof = expert.analyze_as_of(now, BacktestContext(providers=live_providers,
                                                         settings=settings, as_of=now, ...))
    rec_live.almost_equals(rec_asof)   # (signal, confidence, expected_profit_percent,
                                       #  details, skip, skip_reason), float-tolerant

``current_price`` is pinned identically in both paths (it is the as_of close in both,
but the harness re-pins ``rec_asof.current_price = rec_live.current_price`` so a
price-source difference can NEVER mask a logic drift — Decision 1).

Host seams are wired here exactly as a real host would (the plan's Task 12 Step 1):
  * configure_db(temp sqlite) + init_db()       -> the providers/experts conftest
  * set_provider_resolver(get_provider)         -> so any expert path that resolves
                                                   providers via TradeConditions works
  * set_instance_resolver(stub)                 -> none of these 8 experts hit it in
                                                   _gather/_process, but a real host
                                                   always wires it; a stub fails loud
                                                   only if the seam is actually used
  * set_llm_service(stub)                        -> same, for any LLM-touching expert
                                                   (none of the 8 backtestable experts
                                                   call the LLM; TradingAgents is excluded)

The fixtures are TIME-INVARIANT, so the only thing the as_of=NOW vs as_of=None
comparison exercises is the ``as_of`` plumbing — exactly what the gate must prove.
For FMPRating, the live (as_of=None) path uses FMP's CURRENT consensus snapshots
while the backtest (as_of) path RECONSTRUCTS the same inputs no-lookahead from FMP's
dated grades-historical + v4/price-target history; the golden fixture is built so the
NOW reconstruction reproduces the live snapshot EXACTLY, so live == analyze_as_of(NOW)
proves the reconstruction plumbing too. FMPRating is now a real backtestable expert
(the former latest-snapshot-only "Decision 4" caveat is resolved).
"""
from datetime import datetime, timezone

import pytest

from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_common.core.interfaces.LLMServiceInterface import LLMServiceInterface
from ba2_common.core.types import Recommendation

from tests.golden_fixtures import FIXTURE_BUILDERS, NOW, freeze_now_for_live_path


# --------------------------------------------------------------------------- #
# Host-seam wiring (the plan's Task 12 Step 1)
# --------------------------------------------------------------------------- #
class _StubInstanceResolver:
    """A loud stub: the golden experts never resolve an instance in _gather/_process,
    so any call here is a contract violation worth surfacing."""
    def get_expert_instance(self, expert_id):
        raise AssertionError("golden test must not resolve an expert instance")

    def get_account_instance(self, account_id):
        raise AssertionError("golden test must not resolve an account instance")

    def get_account_instance_from_transaction(self, transaction):
        raise AssertionError("golden test must not resolve an account from a transaction")


class _StubLLMService(LLMServiceInterface):
    def create_llm(self, *a, **k):
        raise AssertionError("golden test must not create an LLM")

    def do_llm_call_with_websearch(self, *a, **k):
        raise AssertionError("golden test must not call the LLM with websearch")


@pytest.fixture(scope="module", autouse=True)
def _host_seams():
    """Wire all four host seams the way a real host startup would, then restore."""
    from ba2_common.core import instance_resolver
    from ba2_common.core import TradeConditions
    # NB: the attribute ``ba2_common.core.interfaces.LLMServiceInterface`` resolves to
    # the re-exported CLASS (the package attr shadows the submodule), so the get/set
    # seam FUNCTIONS must be imported from the interfaces package namespace directly.
    from ba2_common.core.interfaces import set_llm_service, get_llm_service

    # provider resolver -> the real ba2_providers.get_provider (so TradeConditions-routed
    # provider resolution works for any expert path that uses it).
    import ba2_providers
    prev_provider = TradeConditions.get_provider_resolver()
    prev_instance = instance_resolver.get_instance_resolver()
    prev_llm = get_llm_service()

    TradeConditions.set_provider_resolver(ba2_providers.get_provider)
    instance_resolver.set_instance_resolver(_StubInstanceResolver())
    set_llm_service(_StubLLMService())
    try:
        yield
    finally:
        TradeConditions.set_provider_resolver(prev_provider)
        instance_resolver.set_instance_resolver(prev_instance)
        set_llm_service(prev_llm)


# --------------------------------------------------------------------------- #
# The 8 golden cases (the Phase-1 gate)
# --------------------------------------------------------------------------- #
CASES = list(FIXTURE_BUILDERS.keys())


def _assert_equal(name, rec_live, rec_asof):
    """Pin current_price identically, then assert the golden tuple matches."""
    if isinstance(rec_live, list):
        assert isinstance(rec_asof, list), f"{name}: live is a list but as_of is not"
        assert len(rec_live) == len(rec_asof), (
            f"{name}: basket size drift live={len(rec_live)} as_of={len(rec_asof)}")
        for a, b in zip(rec_live, rec_asof):
            b.current_price = a.current_price
            assert a.almost_equals(b), f"{name} drift: {a} != {b}"
    else:
        assert isinstance(rec_asof, Recommendation), f"{name}: as_of is not a Recommendation"
        rec_asof.current_price = rec_live.current_price
        assert rec_live.almost_equals(rec_asof), (
            f"{name} drift:\n  live ={rec_live}\n  as_of={rec_asof}")


@pytest.mark.parametrize("name", CASES)
def test_live_equals_analyze_as_of_now(name):
    expert, settings, get_provider, opts = FIXTURE_BUILDERS[name]()
    patch_factory = opts.get("patch")
    live_settings = opts.get("live_settings") or settings
    subtype = opts.get("subtype")

    # FactorRanker installs its data-module fetcher fakes for BOTH paths.
    patch_ctx = patch_factory() if patch_factory else None

    if patch_ctx is not None:
        patch_ctx.__enter__()
    try:
        # ---- Live path: _gather(live, None) + _process ----
        # Freeze the wall clock to the pinned NOW so the live ``as_of=None``
        # branch (which falls back to ``datetime.now()`` for day-count math)
        # reads the SAME instant analyze_as_of() is handed below. This is what
        # makes the gate date-INDEPENDENT — without it the two paths diverge on
        # any run date != the pinned NOW (e.g. FMPEarningsDrift "days ago").
        with freeze_now_for_live_path():
            bundle_live = expert._gather(LiveProviderBundle(get_provider), as_of=None)
            rec_live = expert._process(bundle_live, live_settings, as_of=None)

        # ---- Backtest path: analyze_as_of(now) ----
        ctx = BacktestContext(
            providers=LiveProviderBundle(get_provider),
            settings=settings, as_of=NOW, subtype=subtype,
            extra={"symbol": getattr(expert, "_gather_symbol", None)})
        rec_asof = expert.analyze_as_of(NOW, ctx)
    finally:
        if patch_ctx is not None:
            patch_ctx.__exit__(None, None, None)

    _assert_equal(name, rec_live, rec_asof)


def test_all_eight_cases_present():
    """Guard: the gate must cover all 8 backtestable expert cases."""
    assert len(CASES) == 8, f"expected 8 golden cases, got {len(CASES)}: {CASES}"
