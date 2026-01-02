import sys
sys.path.insert(0, r'C:\Users\basti\Documents\BA2TradePlatform')

import sqlite3

conn = sqlite3.connect(r'C:\Users\basti\Documents\ba2_trade_platform\db.sqlite')
cursor = conn.cursor()

print("CHECKING SMART RISK MANAGER JOBS FOR GROK-4 USAGE")
print("=" * 100)

# Check all Smart Risk Manager jobs with grok model
query = """
SELECT 
    srm.id,
    srm.expert_instance_id,
    srm.status,
    srm.run_date,
    srm.duration_seconds,
    srm.model_used,
    srm.iteration_count
FROM smartriskmanagerjob srm
WHERE srm.model_used LIKE '%grok%' OR srm.model_used LIKE '%Grok%'
ORDER BY srm.run_date DESC
LIMIT 100
"""

cursor.execute(query)
results = cursor.fetchall()

print(f"\nFound {len(results)} Smart Risk Manager jobs using Grok models (all time):\n")

grok4_jobs = 0
grok4_fast_jobs = 0

for job_id, expert_id, status, run_date, duration, model_used, iterations in results:
    is_grok4_non_fast = 'grok' in model_used.lower() and '4' in model_used and 'fast' not in model_used.lower() and '4.1' not in model_used
    
    if is_grok4_non_fast:
        grok4_jobs += 1
        print(f"⚠️ Job {job_id}: Expert {expert_id} - {model_used}")
        print(f"   Status: {status}, Date: {run_date}, Duration: {duration:.1f}s, Iterations: {iterations}")
    else:
        grok4_fast_jobs += 1

print(f"\n" + "=" * 100)
print(f"SUMMARY:")
print(f"  Non-fast Grok-4 jobs: {grok4_jobs}")
print(f"  Fast Grok-4 jobs: {grok4_fast_jobs}")

# Now check how many analysis runs Expert 11 has had
print(f"\n" + "=" * 100)
print(f"CHECKING MARKET ANALYSIS FOR EXPERT 11:")
print(f"=" * 100)

analysis_query = """
SELECT 
    COUNT(*) as total,
    status,
    date(date) as day
FROM marketanalysis
WHERE expert_instance_id = 11
GROUP BY status, date(date)
ORDER BY day DESC
LIMIT 30
"""

cursor.execute(analysis_query)
analysis_results = cursor.fetchall()

print(f"\nMarket Analysis runs by day:")
total_analyses = 0
for count, status, day in analysis_results:
    total_analyses += count
    print(f"  {day}: {status} = {count}")

print(f"\nTotal Market Analysis runs: {total_analyses}")

# Check if Expert 11 has Smart Risk Manager enabled
settings_query = """
SELECT key, value_str
FROM expertsetting
WHERE instance_id = 11
    AND key IN ('enable_smart_risk_manager', 'risk_manager_model', 'smart_risk_manager_user_instructions')
"""

cursor.execute(settings_query)
settings = cursor.fetchall()

print(f"\n" + "=" * 100)
print(f"EXPERT 11 SMART RISK MANAGER SETTINGS:")
print(f"=" * 100)

for key, value in settings:
    print(f"  {key}: {value}")

conn.close()

print(f"\n" + "=" * 100)
print(f"CONCLUSION:")
print(f"=" * 100)
print(f"\nIf Smart Risk Manager is disabled for Expert 11, then the high Grok-4 usage")
print(f"is coming from TradingAgents deep_think_llm usage during market analysis.")
print(f"\nThe deep_think_llm is used for:")
print(f"  - Research Manager (debate arbitration) - 1 call per analysis")
print(f"  - Risk Judge (final risk assessment) - 1 call per analysis") 
print(f"  - Possibly other reasoning-intensive tasks")
print(f"\nWith {total_analyses} analyses, this could easily explain 3,944 API calls")
print(f"if the model is called more frequently than expected (e.g., during retries,")
print(f"web searches, or other internal operations).")
