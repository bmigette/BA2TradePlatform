#!/usr/bin/env python
import sys
sys.path.insert(0, '.')

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import MarketAnalysis, AnalysisUseCase

session = get_db()

# Get OPEN_POSITIONS tasks for expert 13
tasks = session.query(MarketAnalysis).filter(
    MarketAnalysis.expert_instance_id == 13,
    MarketAnalysis.subtype == AnalysisUseCase.OPEN_POSITIONS
).order_by(MarketAnalysis.created_at).all()

print(f'OPEN_POSITIONS tasks for expert 13: {len(tasks)}')
for task in tasks:
    print(f'  ID {task.id}: symbol={task.symbol}, status={task.status}, created={task.created_at.strftime("%H:%M:%S")}')
