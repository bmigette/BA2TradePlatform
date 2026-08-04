"""Prefetch injection: valuation snapshot + market risk-stats block."""
from unittest.mock import MagicMock


def _toolkit():
    tk = MagicMock()
    tk.get_valuation_snapshot.return_value = "# Valuation snapshot — AAA\n\nIntrinsic value/share: $13.32"
    tk.get_risk_stats.return_value = "# Risk statistics — AAA\n\nRealized vol: 21.0%"
    # Existing fundamentals sections return simple strings
    for m in ("get_company_profile", "get_financial_ratios", "get_income_statement",
              "get_balance_sheet", "get_cashflow_statement", "get_past_earnings",
              "get_earnings_estimates", "get_insider_sentiment", "get_insider_transactions"):
        getattr(tk, m).return_value = f"stub {m}"
    return tk


def test_fundamentals_context_includes_valuation_snapshot():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.prefetch_context import (
        gather_fundamentals_context)
    ctx = gather_fundamentals_context(_toolkit(), "AAA", "2026-01-15")
    assert "# Valuation Snapshot (default assumptions)" in ctx
    assert "Intrinsic value/share" in ctx


def test_market_context_is_risk_stats_block():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.prefetch_context import (
        gather_market_context)
    ctx = gather_market_context(_toolkit(), "AAA", "2026-01-15")
    assert "Risk statistics" in ctx


def test_gatherers_survive_provider_failure():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.prefetch_context import (
        gather_fundamentals_context, gather_market_context)
    tk = _toolkit()
    tk.get_valuation_snapshot.side_effect = RuntimeError("boom")
    tk.get_risk_stats.side_effect = RuntimeError("boom")
    # The _section guard: a failed compute section never blanks the whole context.
    assert "stub get_company_profile" in gather_fundamentals_context(tk, "AAA", "2026-01-15")
    assert gather_market_context(tk, "AAA", "2026-01-15") == ""


def test_internal_toolkit_dict_results_are_unwrapped():
    """Toolkit compute methods return the standard
    {"_internal": True, "text_for_agent", "json_for_storage"} dict — the prefetch
    path must unwrap it to text_for_agent instead of silently dropping it."""
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.prefetch_context import (
        gather_market_context)
    tk = _toolkit()
    tk.get_risk_stats.return_value = {
        "_internal": True,
        "text_for_agent": "# Risk statistics — AAA",
        "json_for_storage": {},
    }
    ctx = gather_market_context(tk, "AAA", "2026-01-15")
    assert "# Risk statistics — AAA" in ctx


def test_error_strings_never_become_sections():
    """Toolkit 'Error: No ... providers configured' strings (unset vendor
    settings) are not data — they must not leak into the LLM context."""
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.prefetch_context import (
        gather_fundamentals_context, gather_market_context)
    tk = _toolkit()
    tk.get_risk_stats.return_value = "Error: No risk-stats providers configured"
    tk.get_valuation_snapshot.return_value = "Error: No valuation providers configured"
    assert gather_market_context(tk, "AAA", "2026-01-15") == ""
    ctx = gather_fundamentals_context(tk, "AAA", "2026-01-15")
    assert "Error:" not in ctx
    assert "Valuation Snapshot" not in ctx
    assert "stub get_company_profile" in ctx   # other sections survive
