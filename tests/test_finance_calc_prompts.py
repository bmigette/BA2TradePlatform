"""Prompt methodology guards: the FH practitioner discipline is present and names
the exact tools/blocks the analysts actually have (anti-revert tripwires)."""


def test_fundamentals_prompt_methodology():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.prompts import (
        FUNDAMENTALS_ANALYST_SYSTEM_PROMPT as P)
    for marker in (
        "compute_valuation_dcf",          # intrinsic-value discipline
        "compute_valuation_wacc",
        "DEFAULT assumptions",            # the injected snapshot's framing
        "terminal value",                 # terminal-value share flag
        "disagreement is the finding",    # triangulation rule
        "operating cash flow",            # accrual test (earnings quality)
        "stock-based compensation",       # SBC skepticism
        "dispersion",                     # consensus framing
    ):
        assert marker in P, f"fundamentals prompt lost its methodology marker: {marker}"


def test_market_prompt_methodology():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.prompts import (
        MARKET_ANALYST_SYSTEM_PROMPT as P)
    for marker in (
        "Pre-computed risk statistics",   # the injected block
        "drawdown",
        "skew",
        "kurtosis",
        "Sharpe",
    ):
        assert marker in P, f"market prompt lost its methodology marker: {marker}"
