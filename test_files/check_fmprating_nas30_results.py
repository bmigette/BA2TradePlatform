"""Confirm the FMPRating x S1-S4 NAS30 optimizations actually completed with real fitness."""
import sqlite3

DL = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"
c = sqlite3.connect(DL)
cur = c.cursor()
cols = [r[1] for r in cur.execute("PRAGMA table_info(strategy_optimizations)")]
print("opt cols:", cols)

# pull the phase1-FMPRating-S* rows with whatever fitness/result columns exist
fit_cols = [x for x in cols if any(k in x.lower() for k in ("fitness", "best", "score", "result", "calmar"))]
sel = ", ".join(["id", "name", "status"] + fit_cols)
for r in cur.execute(
    f"SELECT {sel} FROM strategy_optimizations WHERE name LIKE 'phase1-FMPRating-S%' ORDER BY id"
):
    print(r)

# how many backtests are tied to these (persisted top-N)?
print("\n-- backtests with FMPRating in name --")
print("count:", cur.execute("SELECT count(*) FROM backtests WHERE name LIKE '%FMPRating%'").fetchone()[0])
c.close()
