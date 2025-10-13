"""Test JobManager scheduling for expert 8"""
import sys
sys.path.insert(0, 'C:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core.JobManager import JobManager
from ba2_trade_platform.core.models import ExpertInstance
from ba2_trade_platform.core import db
from ba2_trade_platform.logger import logger

print("="*60)
print("Testing JobManager Scheduling for Expert 8")
print("="*60)

# Get expert instance from database
expert_instance = db.get_instance(ExpertInstance, 8)
if not expert_instance:
    print("❌ Expert instance 8 not found in database!")
    sys.exit(1)

print(f"\n✅ Expert Instance loaded from DB:")
print(f"   ID: {expert_instance.id}")
print(f"   Expert: {expert_instance.expert}")
print(f"   Enabled: {expert_instance.enabled}")
print(f"   Account ID: {expert_instance.account_id}")

# Create JobManager
print(f"\n🔧 Creating JobManager...")
job_manager = JobManager()

# Test _get_enabled_instruments directly
print(f"\n🧪 Testing _get_enabled_instruments({expert_instance.id})...")
try:
    instruments = job_manager._get_enabled_instruments(expert_instance.id)
    print(f"   ✅ Returned: {instruments}")
except Exception as e:
    print(f"   ❌ Error: {e}")
    import traceback
    traceback.print_exc()

# Test _schedule_expert_jobs
print(f"\n🧪 Testing _schedule_expert_jobs...")
try:
    job_manager._schedule_expert_jobs(expert_instance)
    print(f"   ✅ Method completed")
except Exception as e:
    print(f"   ❌ Error: {e}")
    import traceback
    traceback.print_exc()

# Check scheduled jobs
print(f"\n📊 Checking scheduled jobs...")
scheduled_jobs = job_manager._scheduler.get_jobs()
print(f"   Total jobs: {len(scheduled_jobs)}")

expert_8_jobs = [j for j in scheduled_jobs if f'expert_{expert_instance.id}' in j.id]
print(f"   Expert 8 jobs: {len(expert_8_jobs)}")

if expert_8_jobs:
    print(f"\n   📋 Expert 8 Jobs:")
    for job in expert_8_jobs:
        print(f"      - {job.id}")
        print(f"        Next run: {job.next_run_time}")
else:
    print(f"   ⚠️  No jobs found for expert 8!")
