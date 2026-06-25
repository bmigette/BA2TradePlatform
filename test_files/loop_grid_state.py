"""Loop monitor: phase1 grid state + NAS30 5min cache coverage at the new path."""
import os
import sqlite3

DL = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"
NEW = r"C:\Users\basti\Documents\ba2\common\cache\ohlcv\fmp"
NAS30 = ("AAPL,MSFT,NVDA,AMZN,META,GOOGL,AVGO,TSLA,COST,NFLX,AMD,PEP,ADBE,CSCO,TMUS,"
         "INTC,QCOM,INTU,AMAT,TXN,AMGN,ISRG,BKNG,HON,VRTX,ADP,SBUX,GILD,MU,LRCX").split(",")

c = sqlite3.connect(DL)
cur = c.cursor()
print("=== strategy_optimizations by status ===")
print(cur.execute("SELECT status, count(*) FROM strategy_optimizations GROUP BY status").fetchall())
print("=== recent opts (name, status) ===")
for r in cur.execute("SELECT id, name, status FROM strategy_optimizations ORDER BY id DESC LIMIT 15"):
    print("  ", r)
print("=== task_queue active (running/queued/pending) ===")
for r in cur.execute("SELECT id, task_type, status FROM task_queue WHERE status IN ('running','queued','pending')"):
    print("  ", r)
print("active task count:", cur.execute(
    "SELECT count(*) FROM task_queue WHERE status IN ('running','queued','pending')").fetchone()[0])
c.close()

have = {f[:-13] for f in os.listdir(NEW) if f.endswith("_5min.parquet")} if os.path.isdir(NEW) else set()
miss = [s for s in NAS30 if s not in have]
print(f"\n=== NAS30 5min cache (new path) === cached={len(NAS30)-len(miss)}/30 missing={miss}")
