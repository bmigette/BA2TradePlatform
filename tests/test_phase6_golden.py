"""Phase 6 GATE acceptance #3 — the golden test re-run through the WIRED LIVE HOST.

This re-runs the (clock-independent) Phase-1 golden gate, but instead of the package
test's STUB seams it wires the **live** host seams via ``wire_all_seams()`` — so the
clean experts execute through exactly the resolver / provider-resolver / LLM-service /
DB / instrument-auto-adder wiring the production app installs at startup. It proves the
Phase-6 shim+wiring migration preserved live decision behaviour: for every clean,
backtestable expert,

    rec_live  = expert._process(expert._gather(LiveProviderBundle(get_provider),
                                               as_of=None), settings)        # the live path
    rec_asof  = expert.analyze_as_of(NOW, BacktestContext(...))              # the backtest path
    rec_live.almost_equals(rec_asof)   # on (signal, confidence, expected_profit_percent,
                                       #     details, skip, skip_reason), float-tolerant,
                                       #     current_price re-pinned so a price-source diff
                                       #     can never mask a logic drift.

The fixtures (deterministic, TIME-INVARIANT provider/fetcher fakes + resolved settings)
are REUSED from the experts package's own golden harness
(``BA2TradeExperts/tests/golden_fixtures.py``) — the same source of truth Phase 1
built and the FIXED clock-independent version. We do NOT reinvent them. They are
imported by locating the experts package on disk and adding its ``tests`` dir to
``sys.path`` (it is a sibling of the ``ba2_experts`` package, not importable as
``ba2_experts.tests``). ``golden_fixtures`` itself only imports ``ba2_common`` + stdlib
+ pandas, so it loads cleanly outside the package's pytest rootdir.

Clean experts covered (8 cases): FMPEarningsDrift, FMPInsiderClusterBuy, FinnHubRating,
FMPSenateTraderCopy (ENTER_MARKET + OPEN_POSITIONS subtypes), FMPSenateTraderWeight,
FactorRanker, FMPRating (documented consensus-lookahead caveat).
"""
from __future__ import annotations

import os
import sys

import pytest

from ba2_common.core.backtest_context import BacktestContext, LiveProviderBundle
from ba2_common.core.types import Recommendation


# --------------------------------------------------------------------------- #
# Locate + import the experts package's golden harness (reuse, do not reinvent).
# --------------------------------------------------------------------------- #
def _import_golden_fixtures():
    import ba2_experts

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(ba2_experts.__file__)))
    tests_dir = os.path.join(repo_root, "tests")
    fixtures_file = os.path.join(tests_dir, "golden_fixtures.py")
    if not os.path.isfile(fixtures_file):
        pytest.skip(
            f"experts golden_fixtures.py not found at {fixtures_file} "
            "(install BA2TradeExperts editable to run the Phase-6 golden gate)"
        )
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import golden_fixtures  # noqa: E402  (path-dependent import by design)

    return golden_fixtures


_golden = _import_golden_fixtures()
FIXTURE_BUILDERS = _golden.FIXTURE_BUILDERS
NOW = _golden.NOW
freeze_now_for_live_path = _golden.freeze_now_for_live_path

CASES = list(FIXTURE_BUILDERS.keys())


# --------------------------------------------------------------------------- #
# Wire the LIVE host seams (wire_all_seams), then restore the prior process-global
# seam state the conftest installed at import time.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", autouse=True)
def _live_wiring(tmp_path_factory):
    import ba2_trade_platform.config as config
    from ba2_trade_platform.core import seam_wiring
    from ba2_common.core import instance_resolver, TradeConditions
    from ba2_common.core.interfaces import get_llm_service, set_llm_service
    import ba2_common.core.db as pkg_db
    import ba2_common.core.TradeRiskManagement as RM

    saved_wired = seam_wiring._wired
    saved_db_file = getattr(config, "DB_FILE", None)
    saved_resolver = instance_resolver.get_instance_resolver()
    saved_provider_resolver = TradeConditions.get_provider_resolver()
    saved_llm = get_llm_service()
    saved_pkg_db_file = getattr(pkg_db, "_db_file", None)
    saved_rm = getattr(RM, "_risk_management", None)

    db_path = str(tmp_path_factory.mktemp("phase6_golden") / "golden.sqlite")
    config.DB_FILE = db_path
    seam_wiring._wired = False
    seam_wiring.wire_all_seams()

    # init_db on the wired engine. (Under the autouse patch_db_engine conftest fixture
    # this runs against the in-memory test engine; the golden cases never touch the DB
    # — they use the fixtures' deterministic fakes — but a real host always inits it.)
    from ba2_trade_platform.core.db import init_db
    init_db()
    try:
        yield
    finally:
        seam_wiring._wired = saved_wired
        config.DB_FILE = saved_db_file
        instance_resolver.set_instance_resolver(saved_resolver)
        TradeConditions.set_provider_resolver(saved_provider_resolver)
        set_llm_service(saved_llm)
        pkg_db._db_file = saved_pkg_db_file
        RM._risk_management = saved_rm


def _assert_equal(name, rec_live, rec_asof):
    """Re-pin current_price identically, then assert the golden tuple matches."""
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
def test_live_equals_analyze_as_of_now_through_wired_host(name):
    """Through the WIRED LIVE host, the live decision == analyze_as_of(NOW) for each
    clean expert (the Phase-6 acceptance gate)."""
    expert, settings, get_provider, opts = FIXTURE_BUILDERS[name]()
    patch_factory = opts.get("patch")
    live_settings = opts.get("live_settings") or settings
    subtype = opts.get("subtype")

    # FactorRanker installs its data-module fetcher fakes for BOTH paths.
    patch_ctx = patch_factory() if patch_factory else None
    if patch_ctx is not None:
        patch_ctx.__enter__()
    try:
        # ---- Live path: _gather(live, None) + _process, wall clock frozen to NOW ----
        with freeze_now_for_live_path():
            bundle_live = expert._gather(LiveProviderBundle(get_provider), as_of=None)
            rec_live = expert._process(bundle_live, live_settings, as_of=None)

        # ---- Backtest path: analyze_as_of(NOW) ----
        ctx = BacktestContext(
            providers=LiveProviderBundle(get_provider),
            settings=settings, as_of=NOW, subtype=subtype,
            extra={"symbol": getattr(expert, "_gather_symbol", None)})
        rec_asof = expert.analyze_as_of(NOW, ctx)
    finally:
        if patch_ctx is not None:
            patch_ctx.__exit__(None, None, None)

    _assert_equal(name, rec_live, rec_asof)


def test_all_clean_expert_cases_present():
    """Guard: the Phase-6 golden gate must cover all 8 clean backtestable cases
    (the same set the Phase-1 package gate covers)."""
    assert len(CASES) == 8, f"expected 8 golden cases, got {len(CASES)}: {CASES}"
