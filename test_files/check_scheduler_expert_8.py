"""Check if expert 8 jobs are actually in the scheduler"""
import sys
sys.path.insert(0, 'C:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.JobManager import get_job_manager
from ba2_trade_platform.logger import logger

print("="*60)
print("Checking Scheduler for Expert 8 Jobs")
print("="*60)

# Get the global job manager instance (same one used by the app)
job_manager = get_job_manager()

# Get all scheduled jobs from the scheduler
all_jobs = job_manager._scheduler.get_jobs()
print(f"\nTotal jobs in scheduler: {len(all_jobs)}")

# Filter for expert 8 jobs
expert_8_jobs = [j for j in all_jobs if 'expert_8' in j.id]
print(f"Expert 8 jobs found: {len(expert_8_jobs)}")

if expert_8_jobs:
    print("\n✅ Expert 8 Jobs:")
    for job in expert_8_jobs:
        print(f"\n  Job ID: {job.id}")
        print(f"  Name: {job.name}")
        print(f"  Next run time: {job.next_run_time}")
        print(f"  Trigger: {job.trigger}")
        print(f"  Args: {job.args}")
        print(f"  Kwargs: {job.kwargs}")
else:
    print("\n❌ No expert 8 jobs found in scheduler!")

# Check _scheduled_jobs dict
print(f"\n" + "="*60)
print("Checking _scheduled_jobs dictionary")
print("="*60)

expert_8_in_dict = {k: v for k, v in job_manager._scheduled_jobs.items() if 'expert_8' in k}
print(f"Expert 8 jobs in _scheduled_jobs: {len(expert_8_in_dict)}")

if expert_8_in_dict:
    print("\n✅ Expert 8 in _scheduled_jobs:")
    for job_id, job in expert_8_in_dict.items():
        print(f"  - {job_id}")
else:
    print("\n❌ No expert 8 jobs in _scheduled_jobs dictionary!")
    
# Show sample of what IS in _scheduled_jobs
print(f"\nSample of jobs in _scheduled_jobs (first 5):")
for i, (job_id, job) in enumerate(list(job_manager._scheduled_jobs.items())[:5]):
    print(f"  - {job_id}")
