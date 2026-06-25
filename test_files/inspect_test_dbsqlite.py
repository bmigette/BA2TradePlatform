"""What is in test/db.sqlite (ba2_common keys DB) vs what's actually needed (just keys)?"""
import sqlite3

DB = r"C:\Users\basti\Documents\ba2\test\db.sqlite"
c = sqlite3.connect(DB)
cur = c.cursor()
tabs = sorted(r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'"))
print(f"tables ({len(tabs)}):")
for t in tabs:
    try:
        n = cur.execute(f"SELECT count(*) FROM '{t}'").fetchone()[0]
    except Exception as e:
        n = f"err {e}"
    flag = "  <-- TRADE DATA" if t in ("tradingorder", "transaction", "position", "expertrecommendation", "marketanalysis", "expertinstance", "accountdefinition") else ""
    if n:
        print(f"  {t:32} {n} rows{flag}")
# the only thing the test platform needs from here = API keys in appsetting
print("\n--- appsetting (keys the test platform actually reads) ---")
try:
    for r in cur.execute("SELECT key FROM appsetting ORDER BY key"):
        print("   ", r[0])
except Exception as e:
    print("appsetting err:", e)
c.close()
