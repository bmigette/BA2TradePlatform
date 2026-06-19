"""ba2_providers import-smoke + LLM/live-leak gate.

The 3 AI providers (AINewsProvider, AICompanyOverviewProvider,
AISocialMediaSentiment) import ModelFactory/langchain at module top and STAY in
the live BA2TradePlatform for Phase 0. Importing ba2_providers must therefore
pull none of langchain / ModelFactory / the live platform / experts.

Per Amendment A1 the leak gate uses sys.modules in a fresh subprocess (NOT
"not installed" — langchain_core/fmpsdk ARE present in this venv). fmpsdk is a
legitimate declared runtime dependency of ba2_providers and is allowed.
"""
import subprocess
import sys
import textwrap

import pytest

PY = sys.executable


def _assert_no_leak(import_stmt, forbidden):
    code = textwrap.dedent(f"""
        import sys; {import_stmt}
        bad=[m for m in {forbidden!r} if any(k==m or k.startswith(m+'.') for k in sys.modules)]
        print('LEAK:'+','.join(bad) if bad else 'CLEAN')""")
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "CLEAN", (
        f"{import_stmt} pulled {out.stdout.strip()} / stderr:\n{out.stderr}"
    )


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
    _assert_no_leak(
        "import ba2_providers",
        ["langchain", "langchain_core", "ba2_trade_platform", "ba2_experts", "nicegui"],
    )


def test_modelfactory_module_not_loaded():
    # Negative control: no ModelFactory module anywhere in sys.modules after import.
    code = textwrap.dedent("""
        import sys, ba2_providers
        print('HAS_MF' if any('ModelFactory' in k for k in sys.modules) else 'NO_MF')""")
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "NO_MF", f"{out.stdout} / {out.stderr}"
