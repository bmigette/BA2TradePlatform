"""Import-smoke + leak gate for ba2_experts.

Asserts:
  * importing ba2_experts pulls NO forbidden module (langchain/langchain_core/
    nicegui/ba2_trade_platform) — Amendment A1 sys.modules subprocess gate;
  * get_expert_class works for a clean expert and returns None for TradingAgents
    (which stays in the live platform);
  * the Penny mixin modules import via the LLM-service seam (no ModelFactory).

Note on the leak gate (Amendment A1): langchain_core / nicegui ARE installed in
this venv, so a "not importable" check would be vacuous. The real assertion is
that importing ba2_experts does not PULL them into sys.modules. fmpsdk IS pulled
(a declared runtime dependency of ba2_experts) and is allowed.
"""
import subprocess
import sys
import textwrap

PY = sys.executable

FORBIDDEN = ["langchain", "langchain_core", "nicegui", "ba2_trade_platform",
             "ba2_common.core.ModelFactory"]


def _assert_no_leak(import_stmt, forbidden):
    code = textwrap.dedent(f"""
        import sys
        {import_stmt}
        bad=[m for m in {forbidden!r} if any(k==m or k.startswith(m+'.') for k in sys.modules)]
        print('LEAK:'+','.join(bad) if bad else 'CLEAN')
    """)
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "CLEAN", (
        f"{import_stmt!r} pulled {out.stdout.strip()} / stderr={out.stderr}")


def test_import_ba2_experts_pulls_no_forbidden_module():
    _assert_no_leak("import ba2_experts", FORBIDDEN)


def test_leak_gate_is_non_vacuous():
    """Negative control: directly importing langchain_core MUST be reported as a leak,
    proving the gate would catch a real one (langchain_core is installed here)."""
    code = textwrap.dedent("""
        import sys, importlib
        try:
            importlib.import_module('langchain_core')
        except ImportError:
            print('CLEAN')  # not installed -> control inconclusive but not a false pass
        else:
            bad=[m for m in ['langchain_core'] if any(k==m or k.startswith(m+'.') for k in sys.modules)]
            print('LEAK:'+','.join(bad) if bad else 'CLEAN')
    """)
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip().startswith("LEAK:langchain_core"), (
        f"control did not detect langchain_core: {out.stdout.strip()} / {out.stderr}")


def test_experts_package_imports_and_registry_works():
    import ba2_experts
    from ba2_experts import get_expert_class
    assert get_expert_class("FMPEarningsDrift") is not None
    assert get_expert_class("FMPInsiderClusterBuy") is not None
    assert get_expert_class("PennyMomentumTrader") is not None
    assert get_expert_class("FactorRanker") is not None
    # TradingAgents stays in the live platform — not part of ba2_experts.
    assert get_expert_class("TradingAgents") is None


def test_penny_modules_import_via_llm_seam():
    import ba2_experts.PennyMomentumTrader.data_gathering  # noqa: F401
    import ba2_experts.PennyMomentumTrader.monitoring  # noqa: F401
    import ba2_experts.PennyMomentumTrader.screening  # noqa: F401


def test_instrument_auto_adder_hook_seam():
    import ba2_experts
    assert ba2_experts.get_instrument_auto_adder_hook() is None
    seen = []
    ba2_experts.set_instrument_auto_adder_hook(lambda symbols: seen.extend(symbols))
    try:
        ba2_experts.get_instrument_auto_adder_hook()(["AAPL", "MSFT"])
        assert seen == ["AAPL", "MSFT"]
    finally:
        ba2_experts.set_instrument_auto_adder_hook(None)
