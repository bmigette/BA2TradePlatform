#!/usr/bin/env python3
"""Investigate runs 21 and 22 - dumping graph state."""

import sys
import json
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from ba2_trade_platform.core.db import get_db
from sqlalchemy import text

def investigate_runs():
    """Check run #21 and #22 for issues."""
    print("=" * 100)
    print("INVESTIGATING SMARTRISKMANAGERJOB RUNS #21 and #22")
    print("=" * 100)
    
    with get_db() as session:
        # Get run #22 detailed info
        print("\n" + "=" * 100)
        print("RUN #22 - TA-Dynamic-grok (7% vs 5% allocation issue)")
        print("=" * 100)
        
        result = session.execute(text(
            "SELECT id, actions_summary, graph_state FROM smartriskmanagerjob WHERE id = 22"
        ))
        row = result.fetchone()
        
        if row:
            run_id, summary, graph_state_json = row
            
            print(f"\nActions Summary:")
            print(summary)
            
            print(f"\n\nGraph State:")
            if graph_state_json:
                try:
                    graph_state = json.loads(graph_state_json)
                    print(json.dumps(graph_state, indent=2))
                except:
                    print("Could not parse graph state")
                    print(graph_state_json[:500])
        else:
            print("Run #22 not found")

if __name__ == "__main__":
    investigate_runs()
