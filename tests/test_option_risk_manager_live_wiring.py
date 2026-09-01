"""The LIVE half of the option risk manager wiring: dispatch, and the run record.

Design ``docs/superpowers/specs/2026-08-27-option-risk-manager-design.md`` §11 plus the §4
operator decision. Two live-only facts are pinned here because they cannot be pinned in
``ba2_common``:

1. ``WorkerQueue`` dispatches a ``classic_options`` expert down the CLASSIC branch — the
   rule engine still decides *whether*, the shared option RM decides whether the sleeve can
   carry it. A mode that fell through to the ``else`` would still work by accident today;
   one that fell into the SMART branch would silently replace the rule engine with an LLM.
2. That branch, and only that branch, writes the option run record.

The decision arithmetic itself is shared code and is tested in
``packages/common/tests/test_option_risk_manager_wiring.py``; nothing here re-tests it.
"""
from __future__ import annotations

import inspect
import re

import pytest


def _dispatch_source() -> str:
    from ba2_trade_platform.core.WorkerQueue import WorkerQueue

    return inspect.getsource(WorkerQueue._check_and_process_expert_recommendations)


def test_classic_options_is_dispatched_down_the_classic_branch():
    """It must reach ``TradeManager`` and the enter ruleset, never the smart risk manager:
    ``classic_options`` is CLASSIC plus the sleeve rails, not a different decider."""
    source = _dispatch_source()
    assert 'risk_manager_mode in ("classic", "classic_options")' in source
    # And the smart branch is still an exact match, so classic_options cannot leak into it.
    assert 'if risk_manager_mode == "smart":' in source


def test_the_option_run_record_is_written_from_that_branch():
    """The runs table reads ``SmartRiskManagerJob`` rows and filters by expert and status,
    so an option run needs no UI work — it needed a writer. This is the writer."""
    source = _dispatch_source()
    assert "flush_option_rm_run" in source
    assert 'if risk_manager_mode == "classic_options":' in source


def test_recording_the_run_never_unwinds_the_trades_it_describes():
    """A bookkeeping failure after orders are on the wire must not propagate: the trades
    have already happened, and losing the record of them is strictly better than losing the
    trades' own error handling."""
    source = _dispatch_source()
    block = source[source.index('if risk_manager_mode == "classic_options":'):]
    guarded = block[:block.index("===== END")]
    assert "try:" in guarded and "except Exception" in guarded


def test_the_live_tree_reaches_the_shared_option_risk_manager_not_a_copy():
    """The in-tree modules are Phase 6 re-export shims; the option RM must be the package's.
    A live-only reimplementation is the divergence §4 exists to prevent."""
    import ba2_common.core.OptionRiskManagement as shared
    import ba2_trade_platform.core.option_lifecycle_service as svc

    assert svc._rm is shared
    # The exit pass and the entry gate must read ONE breaker latch.
    assert svc.get_breaker_state is shared.get_breaker_state
    assert svc.reset_breaker_states is shared.reset_breaker_states


def test_the_live_lifecycle_pass_reaches_the_breaker_through_the_SHARED_transition():
    """The stand-down is decided in one place and read in another, and both places must be
    the shared module's.

    ``update_sleeve_breaker`` reads the sleeve equity, ratchets the peak, tests the drawdown
    and STORES the latch that ``check_rails`` declines against; ``daily_engine`` calls the
    same function once per bar. Two implementations — or a live-only store — would mean a
    breaker that flattens the book here and gates nothing there, which is exactly what
    review finding F5 recorded, and a backtest whose breaker never trips at all, which is
    what the 2026-09-01 parity ruling was about.

    MUTATION KILL: inline the transition back into this pass (``update_breaker(...)`` plus a
    ``set_breaker_state``) and the shared function is no longer the one the live pass calls
    — the identity assertions below fail even though the pass still behaves correctly, which
    is the point: the live behaviour was never the thing at risk.
    """
    import ba2_common.core.OptionRiskManagement as shared
    import ba2_trade_platform.core.option_lifecycle_service as svc

    assert svc.update_sleeve_breaker is shared.update_sleeve_breaker
    source = inspect.getsource(svc.run_option_lifecycle_pass)
    assert "update_sleeve_breaker(" in source
    # The pass must not keep a transition or a store of its own beside it.
    assert "update_breaker(" not in source
    assert "set_breaker_state(" not in source
    assert not re.search(r"_BREAKER_STATE\[", source)
    # And the shared function is what writes the latch the entry gate reads.
    assert "set_breaker_state(" in inspect.getsource(shared.update_sleeve_breaker)
