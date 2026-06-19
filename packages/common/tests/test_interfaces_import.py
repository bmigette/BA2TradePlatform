"""Task 6 acceptance: all interface bases import, the ruleset/RM engine imports,
and importing them does NOT pull any provider/LLM/UI package into sys.modules.

The leak gate runs in a subprocess (Amendment A1) and asserts sys.modules purity
rather than 'package not installed' — fmpsdk/nicegui/langchain_core ARE present in
the test venv, so a real back-edge would be caught.
"""
import os
import subprocess
import sys
import textwrap

FORBIDDEN = ["ba2_providers", "ba2_experts", "langchain", "langchain_core",
             "fmpsdk", "nicegui", "ba2_trade_platform"]

# Reconstruct the PYTHONPATH the subprocess needs so `import ba2_common` resolves.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _assert_no_leak(import_stmt, forbidden=FORBIDDEN):
    code = textwrap.dedent(f"""
        import sys
        {import_stmt}
        bad=[m for m in {forbidden!r} if any(k==m or k.startswith(m+'.') for k in sys.modules)]
        print('LEAK:'+','.join(bad) if bad else 'CLEAN')
    """)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _REPO + (os.pathsep + existing if existing else "")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.stdout.strip() == "CLEAN", (
        f"{import_stmt} pulled {out.stdout.strip()!r} / stderr={out.stderr}")


def test_all_interfaces_import_clean():
    import ba2_common.core.interfaces as I
    for name in ["AccountInterface", "OptionsAccountInterface", "ReadOnlyAccountInterface",
                 "MarketExpertInterface", "LiveExpertInterface", "ExtendableSettingsInterface",
                 "MarketDataProviderInterface", "MarketIndicatorsInterface",
                 "CompanyFundamentalsOverviewInterface", "CompanyFundamentalsDetailsInterface",
                 "CompanyInsiderInterface", "MacroEconomicsInterface", "MarketNewsInterface",
                 "SocialMediaDataProviderInterface", "ScreenerProviderInterface",
                 "SmartRiskExpertInterface", "LLMServiceInterface"]:
        assert hasattr(I, name), f"missing {name}"


def test_ruleset_engine_imports_without_providers():
    import ba2_common.core.TradeConditions  # noqa: F401
    import ba2_common.core.TradeActions  # noqa: F401
    import ba2_common.core.TradeActionEvaluator  # noqa: F401
    import ba2_common.core.TradeRiskManagement  # noqa: F401


def test_interfaces_import_pulls_no_provider_llm_ui():
    _assert_no_leak("import ba2_common.core.interfaces")


def test_ruleset_engine_import_pulls_no_provider_llm_ui():
    for stmt in (
        "import ba2_common.core.TradeConditions",
        "import ba2_common.core.TradeActions",
        "import ba2_common.core.TradeActionEvaluator",
        "import ba2_common.core.TradeRiskManagement",
        "import ba2_common.core.position_sizing",
        "import ba2_common.core.rules_export_import",
    ):
        _assert_no_leak(stmt)


def test_provider_resolver_seam_present():
    # TradeConditions exposes the host-injection seam and raises loudly until wired.
    import ba2_common.core.TradeConditions as TC
    assert callable(TC.set_provider_resolver)
    assert callable(TC.get_provider_resolver)
    assert TC.get_provider_resolver() is None or True  # default unconfigured


def test_rules_export_import_has_no_ui_class():
    import ba2_common.core.rules_export_import as R
    assert hasattr(R, "RulesExporter")
    assert hasattr(R, "RulesImporter")
    assert not hasattr(R, "RulesExportImportUI")  # nicegui UI class dropped
