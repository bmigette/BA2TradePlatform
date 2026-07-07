"""Live-parity RM-routing consistency (gap #4 of the live↔backtest unification): the backtest routes
the classic-RM-skip decision off ``bypasses_classic_rm`` while LIVE (WorkerQueue) keys off
``expert_uses_risk_manager``. For the LOCKED parity scope (classic-RM + bypass only) these two signals
MUST agree for every in-scope expert, else backtest and live would run different policies. daily_backtest
_handler._build_experts fails loud when they diverge; this locks the invariant on the real experts.
"""
import importlib

import pytest

from ba2_common.core.utils import expert_uses_risk_manager

_EXPERTS = ["FactorRanker", "FMPRating", "FMPEarningsDrift", "FMPInsiderClusterBuy"]


@pytest.mark.parametrize("cls_name", _EXPERTS)
def test_bypass_and_uses_risk_manager_agree(cls_name):
    cls = getattr(importlib.import_module("ba2_experts"), cls_name)
    bypass = bool(getattr(cls, "bypasses_classic_rm", False))
    self_executes = not expert_uses_risk_manager(cls)
    # A bypass expert self-executes (uses_risk_manager=False); a classic expert uses the RM.
    assert bypass == self_executes, (
        f"{cls_name}: bypasses_classic_rm={bypass} but expert_uses_risk_manager="
        f"{not self_executes} — backtest/live RM routing would diverge")


def test_factor_ranker_is_the_only_bypass():
    # Sanity: the in-scope bypass set is exactly FactorRanker (both flags set); the FMP signal
    # experts are classic (both flags cleared).
    flags = {c: bool(getattr(getattr(importlib.import_module("ba2_experts"), c),
                             "bypasses_classic_rm", False)) for c in _EXPERTS}
    assert flags == {"FactorRanker": True, "FMPRating": False,
                     "FMPEarningsDrift": False, "FMPInsiderClusterBuy": False}
