"""Engine seam generalizations for PremiumSeller (spec §3.3):
manager-class resolution (default FactorPortfolioManager — FactorRanker
byte-identical) and manage-cadence routing (default off)."""
from ba2_experts.FactorRanker.portfolio import FactorPortfolioManager
from ba2_experts.PremiumSeller import PremiumSeller
from ba2_experts.PremiumSeller.portfolio import OptionPortfolioManager


def test_resolve_default_is_factor_ranker_manager():
    from app.services.backtest.daily_engine import _resolve_bypass_manager_class
    from ba2_experts.FactorRanker import FactorRanker
    assert _resolve_bypass_manager_class(FactorRanker) is FactorPortfolioManager


def test_resolve_premium_seller_manager():
    from app.services.backtest.daily_engine import _resolve_bypass_manager_class
    assert _resolve_bypass_manager_class(PremiumSeller) is OptionPortfolioManager


def test_run_kind_factor_ranker_entry_only():
    from app.services.backtest.daily_engine import _bypass_run_kind
    from ba2_experts.FactorRanker import FactorRanker
    stub = type("E", (), {"bypasses_classic_rm": True})()   # no manages flag -> default False
    assert _bypass_run_kind(stub, entry_ok=True, manage_ok=True) == "entry"
    assert _bypass_run_kind(stub, entry_ok=False, manage_ok=True) is None
    assert getattr(FactorRanker, "manages_between_entries", False) is False


def test_run_kind_premium_seller_manage_bars():
    from app.services.backtest.daily_engine import _bypass_run_kind
    assert _bypass_run_kind(PremiumSeller, entry_ok=True, manage_ok=True) == "entry"
    assert _bypass_run_kind(PremiumSeller, entry_ok=False, manage_ok=True) == "manage"
    assert _bypass_run_kind(PremiumSeller, entry_ok=False, manage_ok=False) is None


def test_premium_seller_registered_in_handler_map():
    from app.services.backtest.daily_backtest_handler import _SUPPORTED_EXPERTS
    assert "PremiumSeller" in _SUPPORTED_EXPERTS


# --------------------------------------------------------------------------- #
# The _apply_bypass_stops capability guard (Task 5b) that used to be tested here is GONE.
#
# It defended PremiumSeller against the engine's per-bar bypass stop pass: OptionPortfolioManager
# owns its exits via manage_open and has no apply_stop_losses, so without the guard the pass
# AttributeErrored into a broad except every non-entry bar. On 2026-08-06 that whole pass was
# deleted — the engine no longer calls back into expert code to run stops, FactorRanker attaches a
# resting SELL_STOP at entry instead — so PremiumSeller is now unaffected BY CONSTRUCTION rather
# than by a capability probe. There is no longer a code path for these tests to exercise.
#
# PremiumSeller's own exits stay covered by its manage_open tests; the FactorRanker stop is covered
# by packages/experts/tests/test_factorranker_portfolio.py and tests/backtest/test_daily_engine_stop.py.
# --------------------------------------------------------------------------- #
