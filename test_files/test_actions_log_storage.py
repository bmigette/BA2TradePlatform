"""
Test script to verify actions_log is properly stored in SmartRiskManagerJob.graph_state
"""

from ba2_trade_platform.core.db import get_db
from ba2_trade_platform.core.models import SmartRiskManagerJob
from sqlmodel import select
import json

def test_actions_log_in_completed_jobs():
    """Check if completed jobs have actions_log in graph_state"""
    
    with get_db() as session:
        # Get all completed jobs
        jobs = session.exec(
            select(SmartRiskManagerJob)
            .where(SmartRiskManagerJob.status == "COMPLETED")
            .order_by(SmartRiskManagerJob.id.desc())
        ).all()
        
        print(f"\nFound {len(jobs)} completed Smart Risk Manager jobs\n")
        print("=" * 80)
        
        jobs_with_actions = 0
        jobs_without_actions = 0
        
        for job in jobs:
            has_actions_log = False
            actions_count_in_log = 0
            
            if job.graph_state:
                has_actions_log = "actions_log" in job.graph_state
                if has_actions_log:
                    actions_count_in_log = len(job.graph_state["actions_log"])
            
            status_icon = "✅" if has_actions_log else "❌"
            
            print(f"{status_icon} Job {job.id}:")
            print(f"   actions_taken_count: {job.actions_taken_count}")
            print(f"   has actions_log: {has_actions_log}")
            if has_actions_log:
                print(f"   actions in log: {actions_count_in_log}")
                jobs_with_actions += 1
            else:
                print(f"   graph_state keys: {list(job.graph_state.keys()) if job.graph_state else 'None'}")
                jobs_without_actions += 1
            print()
        
        print("=" * 80)
        print(f"\nSummary:")
        print(f"  Jobs with actions_log: {jobs_with_actions}")
        print(f"  Jobs WITHOUT actions_log: {jobs_without_actions}")
        
        if jobs_without_actions > 0:
            print(f"\n⚠️  Warning: {jobs_without_actions} job(s) are missing actions_log in graph_state")
            print("     This is expected for jobs that ran before the fix was applied.")
            print("     New jobs should have actions_log properly stored.")

if __name__ == "__main__":
    test_actions_log_in_completed_jobs()
