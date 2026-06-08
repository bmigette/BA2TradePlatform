#!/usr/bin/env python
"""Standalone end-to-end smoke test for the TradingAgents graph after the
pre-fetch analyst rework. Mirrors how the TradingAgents expert builds the config
and provider_map, but runs without the DB/account layer.

Usage:
    .venv/Scripts/python.exe test_files/test_tradingagents_e2e.py AAPL 2026-06-06

Uses the fast GPT-5.4 model (low reasoning) for both deep/quick think to keep cost
down. Social vendors are left empty to avoid extra LLM web-search cost; the social
analyst still runs (reports "no data"), which validates the node flow.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL = "native/gpt5.4{reasoning_effort:low}"   # "GPT-5.4 fast"


def build():
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.default_config import DEFAULT_CONFIG
    from ba2_trade_platform.modules.dataproviders import (
        OHLCV_PROVIDERS, INDICATORS_PROVIDERS, FUNDAMENTALS_OVERVIEW_PROVIDERS,
        FUNDAMENTALS_DETAILS_PROVIDERS, NEWS_PROVIDERS, MACRO_PROVIDERS, INSIDER_PROVIDERS,
    )

    def pick(reg, vendors):
        return [reg[v] for v in vendors if v in reg]

    provider_map = {
        "ohlcv": pick(OHLCV_PROVIDERS, ["yfinance"]),
        "indicators": pick(INDICATORS_PROVIDERS, ["pandas"]),
        "fundamentals_details": pick(FUNDAMENTALS_DETAILS_PROVIDERS, ["fmp", "yfinance"]),
        "fundamentals_overview": pick(FUNDAMENTALS_OVERVIEW_PROVIDERS, ["fmp"]),
        "news": pick(NEWS_PROVIDERS, ["fmp"]),
        "insider": pick(INSIDER_PROVIDERS, ["fmp"]),
        "macro": pick(MACRO_PROVIDERS, ["fred"]),
        "social_media": [],
    }

    config = DEFAULT_CONFIG.copy()
    config.update({
        "deep_think_llm": MODEL,
        "quick_think_llm": MODEL,
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "timeframe": "1d",
        "news_lookback_days": 7,
        "market_history_days": 90,
        "economic_data_days": 90,
        "social_sentiment_days": 3,
        "parallel_tool_calls": False,
        "enable_streaming": False,
        "use_memory": False,
    })
    provider_args = {
        "websearch_model": MODEL,
        "alpha_vantage_source": "alphavantage",
        "economic_data_days": 90,
        "news_lookback_days": 7,
        "social_sentiment_days": 3,
        "analyst_context_size": 128000,
    }
    return config, provider_map, provider_args


def gather_smoke(ticker, date):
    """Cheap check (no LLM): does the fundamentals gather produce data incl. ratios?"""
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.agent_utils_new import Toolkit
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.agents.utils.prefetch_context import gather_fundamentals_context
    _, provider_map, provider_args = build()
    tk = Toolkit(provider_map=provider_map, provider_args=provider_args)
    ctx = gather_fundamentals_context(tk, ticker, date)
    print("===== FUNDAMENTALS GATHER (first 1500 chars) =====")
    print(ctx[:1500])
    print(f"\n[gather length: {len(ctx)} chars; contains 'P/E': {'P/E' in ctx}]")


def full_run(ticker, date):
    from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph
    config, provider_map, provider_args = build()
    ta = TradingAgentsGraph(
        selected_analysts=["market", "social", "news", "fundamentals", "macro"],
        debug=False, config=config, provider_map=provider_map, provider_args=provider_args,
    )
    final_state, decision = ta.propagate(ticker, date)
    print("\n\n===== ANALYST REPORTS =====")
    for key in ("market_report", "sentiment_report", "news_report", "fundamentals_report", "macro_report"):
        rep = final_state.get(key) or ""
        print(f"\n----- {key} ({len(rep)} chars) -----\n{rep[:800]}")
    print("\n\n===== FINAL DECISION =====")
    print(decision)


if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    date = sys.argv[2] if len(sys.argv) > 2 else "2026-06-06"
    mode = sys.argv[3] if len(sys.argv) > 3 else "full"
    if mode == "gather":
        gather_smoke(ticker, date)
    else:
        gather_smoke(ticker, date)
        full_run(ticker, date)
