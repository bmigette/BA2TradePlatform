"""Loop monitor: phase1 StrategyOptimization + TaskQueue state in the test DB."""
import sqlite3

DB = r"C:\Users\basti\Documents\ba2\test\db.sqlite"
c = sqlite3.connect(DB)
cur = c.cursor()
tabs = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def first(*cands):
    for x in cands:
        if x in tabs:
            return x
    return None


so = first("strategyoptimization", "strategy_optimization")
tq = first("taskqueue", "task_queue")
print("tables:", "opt=", so, "taskqueue=", tq)

if so:
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({so})")]
    print("opt cols:", cols)
    print("opt by status:", cur.execute(f"SELECT status, count(*) FROM {so} GROUP BY status").fetchall())
    for r in cur.execute(f"SELECT id, name, status FROM {so} ORDER BY id DESC LIMIT 12"):
        print("  opt", r)

if tq:
    print("task by status:", cur.execute(f"SELECT status, count(*) FROM {tq} GROUP BY status").fetchall())
    for r in cur.execute(
        f"SELECT id, task_type, status FROM {tq} WHERE status IN ('running','queued','pending') ORDER BY id DESC LIMIT 12"
    ):
        print("  task", r)
c.close()
