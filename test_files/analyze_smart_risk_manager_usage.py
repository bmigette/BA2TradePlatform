import sys
sys.path.insert(0, r'C:\Users\basti\Documents\BA2TradePlatform')

import sqlite3
from datetime import datetime, timedelta

# Connect to database
conn = sqlite3.connect(r'C:\Users\basti\Documents\ba2_trade_platform\db.sqlite')
cursor = conn.cursor()

# Get Smart Risk Manager jobs from the last 7 days
seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')

query = """
SELECT 
    srm.id,
    srm.expert_instance_id,
    srm.status,
    srm.run_date,
    srm.duration_seconds,
    ei.expert,
    srm.model_used
FROM smartriskmanagerjob srm
JOIN expertinstance ei ON srm.expert_instance_id = ei.id
WHERE srm.run_date >= ?
ORDER BY srm.run_date DESC
LIMIT 100
"""

cursor.execute(query, (seven_days_ago,))
results = cursor.fetchall()

print(f"Smart Risk Manager Jobs (Last 7 Days): {len(results)} total\n")
print("=" * 100)

# Count by expert
expert_counts = {}
expert_11_jobs = []
expert_9_jobs = []

for job_id, expert_id, status, run_date, duration, expert_type, model_used in results:
    if expert_id not in expert_counts:
        expert_counts[expert_id] = {'total': 0, 'expert_type': expert_type, 'statuses': {}, 'models': {}}
    
    expert_counts[expert_id]['total'] += 1
    
    if status not in expert_counts[expert_id]['statuses']:
        expert_counts[expert_id]['statuses'][status] = 0
    expert_counts[expert_id]['statuses'][status] += 1
    
    # Track models used
    if model_used:
        if model_used not in expert_counts[expert_id]['models']:
            expert_counts[expert_id]['models'][model_used] = 0
        expert_counts[expert_id]['models'][model_used] += 1
    
    # Track expert 11 and 9 specifically (the ones with grok-4)
    if expert_id == 11:
        expert_11_jobs.append((job_id, status, run_date, duration, model_used))
    elif expert_id == 9:
        expert_9_jobs.append((job_id, status, run_date, duration, model_used))

print("\nJobs by Expert:")
print("-" * 100)
for expert_id in sorted(expert_counts.keys()):
    info = expert_counts[expert_id]
    print(f"\nExpert {expert_id} ({info['expert_type']}): {info['total']} jobs")
    for status, count in info['statuses'].items():
        print(f"  Status {status}: {count}")
    if info['models']:
        print(f"  Models used:")
        for model, count in info['models'].items():
            is_grok4 = 'grok' in model.lower() and '4' in model and 'fast' not in model.lower()
            warning = " ⚠️ NON-FAST GROK-4!" if is_grok4 else ""
            print(f"    {model}: {count} jobs{warning}")

print("\n" + "=" * 100)
print(f"EXPERT 11 DETAILS (risk_manager_model=xai/grok4):")
print("=" * 100)
print(f"Total jobs: {len(expert_11_jobs)}")

if expert_11_jobs:
    print("\nRecent 10 jobs:")
    for job_id, status, run_date, duration, model_used in expert_11_jobs[:10]:
        duration_str = f" (duration: {duration:.1f}s)" if duration else ""
        print(f"  Job {job_id}: {status} at {run_date}{duration_str} - Model: {model_used}")

print("\n" + "=" * 100)
print(f"EXPERT 9 DETAILS (DISABLED, also has grok-4):")
print("=" * 100)
print(f"Total jobs: {len(expert_9_jobs)}")

if expert_9_jobs:
    print("\nRecent jobs:")
    for job_id, status, run_date, duration, model_used in expert_9_jobs[:5]:
        print(f"  Job {job_id}: {status} at {run_date} - Model: {model_used}")

# Calculate total jobs and estimate API calls
print("\n" + "=" * 100)
print("ESTIMATED GROK-4 API CALLS:")
print("=" * 100)

# Expert 11 uses grok-4 for both deep_think_llm and risk_manager_model
# Each TradingAgents analysis uses deep_think 2-3 times
# Each Smart Risk Manager job uses risk_manager_model multiple times (one per position + portfolio analysis)

# Get number of analysis jobs for expert 11
analysis_query = """
SELECT COUNT(*) 
FROM marketanalysis 
WHERE expert_instance_id = 11 
    AND date >= ?
"""
cursor.execute(analysis_query, (seven_days_ago,))
expert_11_analyses = cursor.fetchone()[0]

print(f"\nExpert 11 (last 7 days):")
print(f"  Market Analyses: {expert_11_analyses}")
print(f"  Smart Risk Manager Jobs: {len(expert_11_jobs)}")
print(f"\nEstimated Grok-4 API Calls:")
print(f"  From TradingAgents deep_think_llm: ~{expert_11_analyses * 2.5:.0f} (analyses × 2.5 calls/analysis)")
print(f"  From Smart Risk Manager risk_manager_model: ~{len(expert_11_jobs) * 5:.0f} (jobs × ~5 calls/job)")
print(f"  TOTAL ESTIMATED: ~{expert_11_analyses * 2.5 + len(expert_11_jobs) * 5:.0f} calls")

conn.close()
