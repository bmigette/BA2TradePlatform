"""Phase 6 GATE acceptance #1 — boot smoke.

Exercises the live startup path through ``wire_all_seams()`` + ``init_db()`` against
a throwaway DB, proving the package seams (DB engine, instance resolver, LLM service,
TradeConditions provider resolver, instrument-auto-adder hook, classic-RM ATR
indicator provider) are all installed FIRST and nothing raises a ``*NotConfigured``
error at runtime.

Why these assertions (and not the plan's literal ``get_engine().url``): the autouse
``patch_db_engine`` conftest fixture seeds ``ba2_common.core.db._engine`` with the
in-memory test engine for the duration of every test and restores it afterwards, so
asserting on ``get_engine().url`` would observe the test engine, not the configured
boot path. ``configure_db`` records the path in ``ba2_common.core.db._db_file`` (and
resets ``_engine`` to None for lazy rebuild); ``_db_file`` is therefore the
authoritative, path-independent record of what the DB seam was configured with.

The full out-of-process ``python main.py`` boot (JobManager/WorkerQueue/account
refresh/NiceGUI) is verified separately by the gate runner; this in-process harness
is the authoritative acceptance for the *wiring* (per the plan's re-plan checkpoint
on Task 8 Step 2).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def _fresh_wiring():
    """Reset the idempotency guard so this test wires from scratch, then restore the
    process-global seam state the conftest installed at import time afterwards."""
    from ba2_trade_platform.core import seam_wiring
    from ba2_common.core import instance_resolver, TradeConditions
    from ba2_common.core.interfaces import get_llm_service, set_llm_service
    import ba2_common.core.db as pkg_db
    import ba2_common.core.TradeRiskManagement as RM

    saved_wired = seam_wiring._wired
    saved_resolver = instance_resolver.get_instance_resolver()
    saved_provider_resolver = TradeConditions.get_provider_resolver()
    saved_llm = get_llm_service()
    saved_db_file = getattr(pkg_db, "_db_file", None)
    saved_rm = getattr(RM, "_risk_management", None)

    seam_wiring._wired = False
    try:
        yield seam_wiring
    finally:
        seam_wiring._wired = saved_wired
        instance_resolver.set_instance_resolver(saved_resolver)
        TradeConditions.set_provider_resolver(saved_provider_resolver)
        set_llm_service(saved_llm)
        pkg_db._db_file = saved_db_file
        RM._risk_management = saved_rm


def test_app_boots_through_wiring(tmp_path, monkeypatch, _fresh_wiring):
    """wire_all_seams() runs first, init_db() hits the configured engine, and every
    seam resolves without a *NotConfigured error."""
    import ba2_trade_platform.config as config

    target = str(tmp_path / "boot.sqlite")
    monkeypatch.setattr(config, "DB_FILE", target)

    seam_wiring = _fresh_wiring
    seam_wiring.wire_all_seams()
    # Idempotent: a second call is a no-op and must not raise.
    seam_wiring.wire_all_seams()

    # 1) DB seam configured to the live (here: throwaway) path.
    import ba2_common.core.db as pkg_db
    assert pkg_db._db_file == target

    # init_db() builds/uses the package engine the seam configured. Under the autouse
    # patch_db_engine fixture this runs against the in-memory test engine (the seam
    # path is recorded above); the call must not raise.
    from ba2_trade_platform.core.db import init_db
    init_db()

    # 2) Instance resolver is the live one (no InstanceResolverNotConfigured).
    from ba2_common.core.instance_resolver import get_instance_resolver
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver
    resolver = get_instance_resolver()
    assert isinstance(resolver, LiveInstanceResolver)

    # 3) LLM service is the live ModelFactory adapter (no LLMServiceNotConfigured).
    from ba2_common.core.interfaces import get_llm_service
    from ba2_trade_platform.core.llm_service import ModelFactoryLLMService
    assert isinstance(get_llm_service(), ModelFactoryLLMService)

    # 4) TradeConditions provider resolver wired (no "provider resolver not configured").
    from ba2_common.core import TradeConditions
    assert TradeConditions.get_provider_resolver() is not None

    # A provider resolves through the live merge-shim registry (package + AI overlay).
    from ba2_trade_platform.modules.dataproviders import get_provider
    assert get_provider("ohlcv", "yfinance") is not None

    # 5) Instrument-auto-adder hook installed on ba2_experts (Penny screening seam).
    import ba2_experts
    assert ba2_experts.get_instrument_auto_adder_hook() is not None

    # 6) Classic-RM singleton seeded with a provider-backed RM (ATR injection, pattern a).
    import ba2_common.core.TradeRiskManagement as RM
    assert RM._risk_management is not None
    assert RM._risk_management.indicator_provider is not None
