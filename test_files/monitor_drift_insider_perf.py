"""Watch the grid's EarningsDrift / InsiderClusterBuy opts and report per-individual timing —
flag if slow (0 individuals after >8 min, or >5 min/individual) so we can queue a perf pass."""
import sqlite3
import time
import json
import datetime

DB = r"C:\Users\basti\Documents\ba2\test\dl_forecasting.db"


def snap():
    c = sqlite3.connect(DB)
    r = c.execute(
        "SELECT id,name,status,best_fitness,all_results,started_at FROM strategy_optimizations "
        "WHERE name LIKE '%EarningsDrift%' OR name LIKE '%Insider%' ORDER BY id LIMIT 1"
    ).fetchone()
    c.close()
    if not r:
        return None
    n = len(json.loads(r[4])) if r[4] else 0
    el = 0.0
    try:
        st = datetime.datetime.fromisoformat(r[5])
        el = (datetime.datetime.now(st.tzinfo) - st).total_seconds() / 60
    except Exception:
        pass
    return {"id": r[0], "name": r[1], "status": r[2], "best": r[3], "ind": n, "elapsed_min": el}


for i in range(90):
    s = snap()
    if s is None:
        print(f"{datetime.datetime.now():%H:%M:%S} EarningsDrift/Insider not started yet")
        time.sleep(60)
        continue
    rate = s["elapsed_min"] / s["ind"] if s["ind"] else None
    print(f"{datetime.datetime.now():%H:%M:%S} {s['name']} status={s['status']} ind={s['ind']} "
          f"elapsed={s['elapsed_min']:.1f}min best={s['best']}"
          + (f" rate={rate:.1f}min/ind" if rate else ""))
    if s["status"] in ("completed", "failed", "cancelled"):
        print(f"VERDICT: {s['name']} {s['status']} best={s['best']} "
              f"({s['ind']} ind in {s['elapsed_min']:.0f}min)")
        break
    if s["ind"] >= 2:
        print(f"VERDICT: timing OK-ish — {rate:.1f} min/individual ({s['name']})")
        break
    if s["ind"] == 0 and s["elapsed_min"] > 8:
        print(f"VERDICT: SLOW — 0 individuals after {s['elapsed_min']:.0f} min ({s['name']}) -> perf pass")
        break
    time.sleep(60)
else:
    print("VERDICT: TIMEOUT (90 min) without a clear signal")
