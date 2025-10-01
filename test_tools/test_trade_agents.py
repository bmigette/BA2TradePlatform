import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.graph.trading_graph import TradingAgentsGraph
from ba2_trade_platform.thirdparties.TradingAgents.tradingagents.default_config import DEFAULT_CONFIG
cfg = DEFAULT_CONFIG.copy()

ta = TradingAgentsGraph(debug=True, config=cfg)

# forward propagate
_, decision = ta.propagate("NVDA", "2025-05-10")
print(decision)