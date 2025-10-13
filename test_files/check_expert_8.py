"""Check expert 8 configuration and scheduled jobs"""
import sys
sys.path.insert(0, 'C:\\Users\\basti\\Documents\\BA2TradePlatform')

from ba2_trade_platform.core import db
from ba2_trade_platform.core.models import ExpertInstance, ExpertSetting
from ba2_trade_platform.core.JobManager import JobManager
from sqlmodel import select

# Get expert instance
expert = db.get_instance(ExpertInstance, 8)
print(f"Expert ID: {expert.id}")
print(f"Expert: {expert.expert}")
print(f"Enabled: {expert.enabled}")
print(f"Account ID: {expert.account_id}")

# Get settings
with db.get_db() as session:
    settings = session.exec(select(ExpertSetting).where(ExpertSetting.instance_id == 8)).all()
    print("\nSettings:")
    for s in settings:
        if s.value_str:
            print(f"  {s.key}: {s.value_str}")
        elif s.value_json:
            print(f"  {s.key}: {s.value_json}")
        elif s.value_float is not None:
            print(f"  {s.key}: {s.value_float}")

# Check JobManager
print("\n" + "="*60)
print("Checking JobManager scheduled jobs:")
print("="*60)

job_manager = JobManager()
scheduled_jobs = job_manager._scheduler.get_jobs()
print(f"\nTotal scheduled jobs: {len(scheduled_jobs)}")

expert_8_jobs = [j for j in scheduled_jobs if 'expert_8' in j.id or 'expert_instance_8' in j.id]
print(f"Jobs for expert 8: {len(expert_8_jobs)}")

if expert_8_jobs:
    print("\nExpert 8 jobs:")
    for job in expert_8_jobs:
        print(f"  Job ID: {job.id}")
        print(f"  Next run: {job.next_run_time}")
        print(f"  Args: {job.args}")
        print(f"  Kwargs: {job.kwargs}")
        print()
else:
    print("\n⚠️  NO JOBS FOUND FOR EXPERT 8")

# Check what instruments would be returned
print("\n" + "="*60)
print("Testing _get_enabled_instruments:")
print("="*60)

try:
    instruments = job_manager._get_enabled_instruments(expert)
    print(f"Instruments returned: {instruments}")
except Exception as e:
    print(f"Error getting instruments: {e}")
    import traceback
    traceback.print_exc()
