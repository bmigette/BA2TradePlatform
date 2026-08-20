"""ba2_providers import-smoke + LLM/live-leak gate.

The 3 AI providers (AINewsProvider, AICompanyOverviewProvider,
AISocialMediaSentiment) import ModelFactory/langchain at module top and STAY in
the live BA2TradePlatform for Phase 0. Importing ba2_providers must therefore
pull none of langchain / ModelFactory / the live platform / experts.

Per Amendment A1 the leak gate uses sys.modules in a fresh subprocess (NOT
"not installed" — langchain_core/fmpsdk ARE present in this venv). fmpsdk is a
legitimate declared runtime dependency of ba2_providers and is allowed.
"""
import pytest

from ._leakgate import MARK, assert_no_leak, check_leak, probe_verdict

FORBIDDEN = ["langchain", "langchain_core", "ba2_trade_platform", "ba2_experts",
             "nicegui"]


def test_providers_import_without_llm():
    import ba2_providers
    from ba2_providers import get_provider
    assert callable(get_provider)


def test_no_ai_providers_registered():
    import ba2_providers as p
    # AI providers stay in the live platform for Phase 0; get_provider must raise
    # ValueError for "ai" rather than importing ModelFactory.
    for cat in ["news", "fundamentals_overview", "socialmedia"]:
        with pytest.raises(ValueError):
            p.get_provider(cat, "ai")


def test_socialmedia_has_stocktwits_not_ai():
    import ba2_providers as p
    keys = set(p.SOCIALMEDIA_PROVIDERS.keys())
    assert "ai" not in keys
    assert "stocktwits" in keys
    assert "stocktwits_trending" in keys


def test_import_pulls_no_langchain_or_modelfactory():
    # The real assertion (Amendment A1): importing ba2_providers must not PULL
    # langchain / ModelFactory / the live platform / experts / nicegui.
    assert_no_leak("ba2_providers", FORBIDDEN)


def test_modelfactory_module_not_loaded():
    # Negative control: no ModelFactory module anywhere in sys.modules after import.
    verdict = probe_verdict(
        "import sys, ba2_providers\n"
        f"print({MARK!r} + ('HAS_MF' if any('ModelFactory' in k for k in sys.modules)"
        " else 'NO_MF'))\n"
    )
    assert verdict == "NO_MF", verdict


def test_leak_gate_catches_a_real_live_platform_leak():
    """Non-vacuity control: the SAME gate pointed at a module that really does have
    the back-edges (`ba2_trade_platform.core.utils`) must report them. If this ever
    goes CLEAN the gate has stopped discriminating and the CLEAN above is worthless."""
    verdict = check_leak("ba2_trade_platform.core.utils", FORBIDDEN)
    assert verdict.startswith("LEAK:"), (
        f"gate reported a known-leaky module as clean: {verdict!r}")
    assert "ba2_trade_platform" in verdict and "langchain_core" in verdict, verdict
