#!/usr/bin/env python
"""One-off script: set analysis_strategy_notes on PROD TradingAgents instance 5
(TA-Dynamic-GPT5) so the Research Manager, Trader, and Risk Manager understand
this account runs a buy-the-dip / mean-reversion strategy on broken charts.

Usage:
    .venv/Scripts/python.exe test_files/set_strategy_notes_instance5.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ba2_trade_platform.config as config
config.DB_FILE = r"C:\Users\basti\Documents\ba2_trade_platform-prod\db.sqlite"

from ba2_trade_platform.modules.experts.TradingAgents import TradingAgents

STRATEGY_NOTES = """This expert intentionally screens for stocks that have dropped sharply (e.g. >=15% over ~7 days) -- a buy-the-dip / mean-reversion strategy. A bearish-looking technical picture (downtrend, oversold momentum, broken support) is the EXPECTED entry condition for almost every candidate you'll see, not new information that should by itself drive a SELL/UNDERWEIGHT decision.

When evaluating these candidates:
- Don't treat "the chart is broken" alone as a reason to pass -- that's the screener's selection criterion and is true for nearly every name.
- DO still weigh technicals normally: a stock still making new lows on heavy distribution volume with no stabilization is a weaker buy-the-dip candidate than one showing early reversal signs (RSI divergence, slowing downside momentum, support holding, narrowing range, capitulation volume). Use technicals to judge WHETHER/WHEN a reversal looks likely, not whether a drop happened.
- Past memory/reflections may cite recent losses on other tickers (e.g. SYM, FLS, TOST). Before generalizing, consider WHY those trades lost: if the thesis was sound but the position was stopped out by a too-tight stop during normal post-drop volatility, that's a risk-management/sizing lesson, not evidence that buy-the-dip entries on broken charts are bad. Don't let unrelated past losses bias you toward blanket SELL.
- Fundamentals and news matter, but don't swing to the other extreme either: a name with strong fundamentals but a chart still in freefall with zero reversal signal may warrant waiting rather than buying immediately.

In short: a broken chart is the expected entry condition, not an automatic SELL. Look for reversal/stabilization signals to time entries, and weigh fundamentals and technicals together rather than picking one to the exclusion of the other."""


def main():
    expert = TradingAgents(5)
    print(f"Instance: id={expert.instance.id}, alias={expert.instance.alias}, expert={expert.instance.expert}")
    current = expert.settings.get("analysis_strategy_notes")
    print(f"Current analysis_strategy_notes: {current!r}")

    expert.save_setting("analysis_strategy_notes", STRATEGY_NOTES)

    expert2 = TradingAgents(5)
    new_val = expert2.settings.get("analysis_strategy_notes")
    print(f"New analysis_strategy_notes ({len(new_val)} chars):\n{new_val}")


if __name__ == "__main__":
    main()
