"""Compare legacy backend/dl_forecasting.db vs test/dl_forecasting.db (phase1 grid data)."""
import os
import sqlite3

PATHS = {
    "backend (legacy)": r"C:\Users\basti\Documents\dev\BA2TestPlatform\backend\dl_forecasting.db",
    "test (new)": r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db",
}

for label, p in PATHS.items():
    if not os.path.exists(p):
        print(f"{label}: MISSING ({p})")
        continue
    size = os.path.getsize(p) / 1e6
    c = sqlite3.connect(p)
    cur = c.cursor()
    tabs = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    so = "strategyoptimization" if "strategyoptimization" in tabs else None
    tq = "taskqueue" if "taskqueue" in tabs else None
    nopt = cur.execute(f"SELECT count(*) FROM {so}").fetchone()[0] if so else "no-table"
    print(f"{label}: {size:.1f} MB | tables={len(tabs)} | strategyoptimization rows={nopt}")
    if so:
        for r in cur.execute(f"SELECT status, count(*) FROM {so} GROUP BY status"):
            print("    ", r)
        for r in cur.execute(f"SELECT id, name, status FROM {so} ORDER BY id DESC LIMIT 12"):
            print("     opt", r)
    if tq:
        run = cur.execute(
            f"SELECT count(*) FROM {tq} WHERE status IN ('running','queued','pending')"
        ).fetchone()[0]
        print("     taskqueue active(running/queued/pending):", run)
    c.close()
