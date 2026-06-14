"""Phase 6 Task 7 -- ``core/seam_wiring.wire_all_seams()`` tests.

Verifies the single startup wiring entry point installs every package seam and is
idempotent. ``wire_all_seams()`` mutates *process-global* package state
(``ba2_common.core.db._db_file``, the instance resolver, the LLM service, the
``TradeConditions`` provider resolver, the ``ba2_experts`` auto-adder hook, and the
classic-RM singleton). ``tests/conftest.py`` already wires a subset of these at
import time for the rest of the suite; these tests therefore SAVE and RESTORE every
piece of global state they touch so they leave the conftest wiring intact for
sibling tests.
"""
from __future__ import annotations

import contextlib

import pytest


@contextlib.contextmanager
def _isolated_seam_state():
    """Save/restore all process-global seam state ``wire_all_seams`` mutates.

    Lets a test force a fresh ``wire_all_seams()`` run (by resetting the idempotency
    flag) and assert on its effects without leaking into the conftest-wired state the
    rest of the suite depends on.
    """
    import ba2_common.core.db as _db
    import ba2_common.core.instance_resolver as _ir
    import ba2_common.core.interfaces as _ifaces
    import ba2_common.core.TradeConditions as _tc
    import ba2_common.core.TradeRiskManagement as _rm
    import ba2_experts as _exp
    from ba2_trade_platform.core import seam_wiring

    saved = {
        "wired": seam_wiring._wired,
        "db_file": _db._db_file,
        "engine": _db._engine,
        "resolver": _ir.get_instance_resolver(),
        "llm": _ifaces.get_llm_service() if _has_llm(_ifaces) else None,
        "provider_resolver": getattr(_tc, "_provider_resolver", None),
        "auto_adder_hook": _exp.get_instrument_auto_adder_hook(),
        "rm_singleton": getattr(_rm, "_risk_management", None),
    }
    try:
        yield seam_wiring
    finally:
        seam_wiring._wired = saved["wired"]
        _db._db_file = saved["db_file"]
        _db._engine = saved["engine"]
        _ir.set_instance_resolver(saved["resolver"])
        if saved["llm"] is not None:
            _ifaces.set_llm_service(saved["llm"])
        if saved["provider_resolver"] is not None:
            _tc.set_provider_resolver(saved["provider_resolver"])
        _exp.set_instrument_auto_adder_hook(saved["auto_adder_hook"])
        _rm._risk_management = saved["rm_singleton"]


def _has_llm(ifaces) -> bool:
    try:
        ifaces.get_llm_service()
        return True
    except Exception:
        return False


def test_wire_all_seams_configures_db_path(tmp_path):
    """The DB seam points ba2_common's lazy engine at the live (configured) path."""
    import ba2_common.core.db as _db

    with _isolated_seam_state() as seam_wiring:
        import ba2_trade_platform.config as config

        target = str(tmp_path / "wired.sqlite")
        # Reset the idempotency flag so this call actually re-wires against `target`.
        seam_wiring._wired = False
        _orig_db_file = config.DB_FILE
        config.DB_FILE = target
        try:
            seam_wiring.wire_all_seams()
        finally:
            config.DB_FILE = _orig_db_file

        # configure_db records the path and clears the memoized engine; get_engine
        # builds lazily, so assert on the recorded path (engine not built here).
        assert _db._db_file == target


def test_wire_all_seams_installs_instance_resolver():
    """The instance resolver seam holds a live LiveInstanceResolver."""
    from ba2_common.core.instance_resolver import get_instance_resolver
    from ba2_trade_platform.core.instance_registry import LiveInstanceResolver

    with _isolated_seam_state() as seam_wiring:
        seam_wiring._wired = False
        seam_wiring.wire_all_seams()
        assert isinstance(get_instance_resolver(), LiveInstanceResolver)


def test_wire_all_seams_installs_llm_service():
    """The LLM-service seam (package-level set/get_llm_service) holds the live impl."""
    from ba2_common.core.interfaces import get_llm_service
    from ba2_trade_platform.core.llm_service import ModelFactoryLLMService

    with _isolated_seam_state() as seam_wiring:
        seam_wiring._wired = False
        seam_wiring.wire_all_seams()
        assert isinstance(get_llm_service(), ModelFactoryLLMService)


def test_wire_all_seams_installs_provider_resolver_and_auto_adder_hook():
    """TradeConditions provider resolver -> live get_provider; auto-adder hook set."""
    from ba2_trade_platform.modules.dataproviders import get_provider as live_get_provider
    from ba2_trade_platform.core.seam_helpers import auto_add_instruments_hook

    with _isolated_seam_state() as seam_wiring:
        import ba2_common.core.TradeConditions as _tc
        import ba2_experts as _exp

        seam_wiring._wired = False
        seam_wiring.wire_all_seams()

        assert _tc._provider_resolver is live_get_provider
        assert _exp.get_instrument_auto_adder_hook() is auto_add_instruments_hook


def test_wire_all_seams_seeds_classic_rm_with_indicator_provider():
    """ATR pattern (a): the classic-RM singleton is seeded with an indicator provider."""
    with _isolated_seam_state() as seam_wiring:
        import ba2_common.core.TradeRiskManagement as _rm

        # Force the seed branch: singleton must be unset for wire_all_seams to seed it.
        _rm._risk_management = None
        seam_wiring._wired = False
        seam_wiring.wire_all_seams()

        rm = _rm._risk_management
        assert rm is not None
        assert isinstance(rm, _rm.TradeRiskManagement)
        # The provider was threaded through the constructor (pattern (a)).
        assert getattr(rm, "indicator_provider", None) is not None


def test_wire_all_seams_is_idempotent():
    """A second call is a no-op: it must not re-run wiring (and not clobber state)."""
    with _isolated_seam_state() as seam_wiring:
        from ba2_common.core.instance_resolver import get_instance_resolver

        seam_wiring._wired = False
        seam_wiring.wire_all_seams()
        first_resolver = get_instance_resolver()
        assert seam_wiring._wired is True

        # Second call returns early; the resolver object is unchanged.
        seam_wiring.wire_all_seams()
        assert get_instance_resolver() is first_resolver
